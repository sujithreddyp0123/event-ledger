import httpx
import pytest

from account_service.app.main import create_app as create_account_app
from gateway.app.main import create_app as create_gateway_app


def event_payload(event_id: str, amount: str = "100.00", event_timestamp: str = "2026-05-15T14:02:11Z", event_type: str = "CREDIT"):
    return {
        "eventId": event_id,
        "accountId": "acct-123",
        "type": event_type,
        "amount": amount,
        "currency": "USD",
        "eventTimestamp": event_timestamp,
        "metadata": {"source": "pytest"},
    }


@pytest.fixture
async def clients(tmp_path):
    account_app = create_account_app(str(tmp_path / "account.db"))
    account_transport = httpx.ASGITransport(app=account_app)
    account_client = httpx.AsyncClient(transport=account_transport, base_url="http://account-service")

    gateway_app = create_gateway_app(
        str(tmp_path / "gateway.db"),
        account_client=account_client,
    )
    gateway_transport = httpx.ASGITransport(app=gateway_app)
    gateway_client = httpx.AsyncClient(transport=gateway_transport, base_url="http://gateway")

    try:
        yield gateway_client, account_client, account_app
    finally:
        await gateway_client.aclose()
        await account_client.aclose()


@pytest.mark.asyncio
async def test_full_flow_updates_balance_and_preserves_idempotency(clients):
    gateway_client, _, _ = clients

    first = await gateway_client.post("/events", json=event_payload("evt-001", "150.00"))
    duplicate = await gateway_client.post("/events", json=event_payload("evt-001", "150.00"))
    balance = await gateway_client.get("/accounts/acct-123/balance")

    assert first.status_code == 201
    assert first.json()["duplicate"] is False
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert balance.status_code == 200
    assert balance.json()["balance"] == "150.00"


@pytest.mark.asyncio
async def test_out_of_order_events_are_listed_chronologically(clients):
    gateway_client, _, _ = clients

    await gateway_client.post(
        "/events",
        json=event_payload("evt-late", "25.00", "2026-05-15T15:00:00Z"),
    )
    await gateway_client.post(
        "/events",
        json=event_payload("evt-early", "10.00", "2026-05-15T10:00:00Z"),
    )

    response = await gateway_client.get("/events", params={"account": "acct-123"})

    assert response.status_code == 200
    assert [event["eventId"] for event in response.json()["events"]] == ["evt-early", "evt-late"]


@pytest.mark.asyncio
async def test_get_event_by_id_returns_gateway_record(clients):
    gateway_client, _, _ = clients

    await gateway_client.post("/events", json=event_payload("evt-read"))
    response = await gateway_client.get("/events/evt-read")

    assert response.status_code == 200
    assert response.json()["eventId"] == "evt-read"
    assert response.json()["status"] == "APPLIED"


@pytest.mark.asyncio
async def test_debits_reduce_balance_regardless_of_arrival_order(clients):
    gateway_client, _, _ = clients

    await gateway_client.post(
        "/events",
        json=event_payload("evt-debit", "30.00", "2026-05-15T15:00:00Z", "DEBIT"),
    )
    await gateway_client.post(
        "/events",
        json=event_payload("evt-credit", "100.00", "2026-05-15T10:00:00Z", "CREDIT"),
    )

    response = await gateway_client.get("/accounts/acct-123/balance")

    assert response.status_code == 200
    assert response.json()["balance"] == "70.00"


@pytest.mark.asyncio
async def test_validation_rejects_bad_events(clients):
    gateway_client, _, _ = clients

    response = await gateway_client.post(
        "/events",
        json=event_payload("evt-bad", "0.00"),
    )
    bad_type = await gateway_client.post(
        "/events",
        json=event_payload("evt-bad-type", "5.00", event_type="TRANSFER"),
    )

    assert response.status_code == 422
    assert bad_type.status_code == 400


@pytest.mark.asyncio
async def test_trace_id_is_propagated_to_account_service(clients):
    gateway_client, _, account_app = clients

    response = await gateway_client.post(
        "/events",
        headers={"X-Trace-Id": "trace-test-123"},
        json=event_payload("evt-trace"),
    )

    assert response.status_code == 201
    assert response.headers["X-Trace-Id"] == "trace-test-123"
    assert account_app.state.last_trace_id == "trace-test-123"


@pytest.mark.asyncio
async def test_account_service_returns_account_details(clients):
    gateway_client, account_client, _ = clients

    await gateway_client.post("/events", json=event_payload("evt-account-credit", "80.00"))
    await gateway_client.post(
        "/events",
        json=event_payload("evt-account-debit", "15.50", event_type="DEBIT"),
    )

    response = await account_client.get("/accounts/acct-123")

    assert response.status_code == 200
    assert response.json()["accountId"] == "acct-123"
    assert response.json()["balance"] == "64.50"
    assert len(response.json()["recentTransactions"]) == 2


@pytest.mark.asyncio
async def test_gateway_gracefully_handles_account_service_outage(tmp_path):
    class FailingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("account service down", request=request)

    failing_client = httpx.AsyncClient(
        transport=FailingTransport(),
        base_url="http://account-service",
    )
    gateway_app = create_gateway_app(str(tmp_path / "gateway.db"), account_client=failing_client)
    gateway_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app),
        base_url="http://gateway",
    )

    try:
        post_response = await gateway_client.post("/events", json=event_payload("evt-outage"))
        list_response = await gateway_client.get("/events", params={"account": "acct-123"})
        balance_response = await gateway_client.get("/accounts/acct-123/balance")
    finally:
        await gateway_client.aclose()
        await failing_client.aclose()

    assert post_response.status_code == 503
    assert post_response.json()["detail"] == "Account Service is unavailable; event was not applied"
    assert list_response.status_code == 200
    assert list_response.json()["events"] == []
    assert balance_response.status_code == 503
    assert balance_response.json()["detail"] == "Account Service is unreachable"
