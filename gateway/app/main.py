import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .database import connect, migrate
from .logging_config import configure_logging, trace_id_ctx

ACCOUNT_SERVICE_URL = os.getenv("ACCOUNT_SERVICE_URL", "http://localhost:8001")

logger = configure_logging("event-gateway")
metrics = {
    "requests_total": 0,
    "errors_total": 0,
    "events_accepted_total": 0,
    "duplicate_events_total": 0,
    "account_service_failures_total": 0,
}


class EventIn(BaseModel):
    eventId: str = Field(min_length=1)
    accountId: str = Field(min_length=1)
    type: str
    amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=1)
    eventTimestamp: datetime
    metadata: dict[str, Any] | None = None


def amount_to_cents(amount: Decimal) -> int:
    return int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_amount(cents: int) -> str:
    return f"{Decimal(cents) / Decimal('100'):.2f}"


def row_to_event(row: Any) -> dict[str, Any]:
    return {
        "eventId": row["event_id"],
        "accountId": row["account_id"],
        "type": row["type"],
        "amount": cents_to_amount(row["amount_cents"]),
        "currency": row["currency"],
        "eventTimestamp": row["event_timestamp"],
        "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
        "status": row["status"],
    }


async def call_with_backoff(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    trace_id: str,
    attempts: int = 3,
    **kwargs,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await client.request(
                method,
                url,
                headers={**kwargs.pop("headers", {}), "X-Trace-Id": trace_id},
                timeout=1.5,
                **kwargs,
            )
            if response.status_code < 500:
                return response
            last_error = httpx.HTTPStatusError(
                "account service returned server error",
                request=response.request,
                response=response,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc

        # Bounded exponential backoff keeps the client responsive during downstream outages.
        await asyncio.sleep(0.1 * (2**attempt))

    raise RuntimeError("account service unavailable") from last_error


def create_app(
    db_path: str | None = None,
    account_client: httpx.AsyncClient | None = None,
    account_base_url: str = ACCOUNT_SERVICE_URL,
) -> FastAPI:
    conn = connect(db_path)
    migrate(conn)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if account_client is None:
            app.state.account_client = httpx.AsyncClient(base_url=account_base_url)
            app.state.owns_account_client = True
        else:
            app.state.account_client = account_client
            app.state.owns_account_client = False
        yield
        if app.state.owns_account_client:
            await app.state.account_client.aclose()
        conn.close()

    app = FastAPI(title="Event Gateway API", version="1.0.0", lifespan=lifespan)
    app.state.db = conn
    app.state.account_client = account_client
    app.state.owns_account_client = False

    @app.middleware("http")
    async def trace_and_log(request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())
        token = trace_id_ctx.set(trace_id)
        metrics["requests_total"] += 1
        logger.info(f"{request.method} {request.url.path}", extra={"trace_id": trace_id})
        try:
            response = await call_next(request)
            response.headers["X-Trace-Id"] = trace_id
            if response.status_code >= 400:
                metrics["errors_total"] += 1
            return response
        except Exception:
            metrics["errors_total"] += 1
            logger.exception("unhandled request error", extra={"trace_id": trace_id})
            raise
        finally:
            trace_id_ctx.reset(token)

    def db():
        return app.state.db

    def account_http_client():
        if app.state.account_client is None:
            app.state.account_client = httpx.AsyncClient(base_url=account_base_url)
        return app.state.account_client

    @app.get("/health")
    def health(database=Depends(db)):
        database.execute("SELECT 1").fetchone()
        return {"status": "ok", "service": "event-gateway", "database": "ok"}

    @app.get("/metrics")
    def get_metrics():
        return metrics

    @app.post("/events")
    async def submit_event(
        payload: EventIn,
        response: Response,
        x_trace_id: str | None = Header(default=None, alias="X-Trace-Id"),
        database=Depends(db),
        client: httpx.AsyncClient = Depends(account_http_client),
    ):
        if payload.type not in {"CREDIT", "DEBIT"}:
            raise HTTPException(status_code=400, detail="type must be CREDIT or DEBIT")

        existing = database.execute(
            "SELECT * FROM events WHERE event_id = ?", (payload.eventId,)
        ).fetchone()
        if existing:
            metrics["duplicate_events_total"] += 1
            response.status_code = status.HTTP_200_OK
            return {"event": row_to_event(existing), "duplicate": True}

        timestamp = payload.eventTimestamp.isoformat().replace("+00:00", "Z")
        database.execute(
            """
            INSERT INTO events
                (event_id, account_id, type, amount_cents, currency, event_timestamp, metadata_json, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """,
            (
                payload.eventId,
                payload.accountId,
                payload.type,
                amount_to_cents(payload.amount),
                payload.currency,
                timestamp,
                json.dumps(payload.metadata) if payload.metadata else None,
            ),
        )
        database.commit()

        trace_id = x_trace_id or trace_id_ctx.get() or str(uuid.uuid4())
        try:
            downstream = await call_with_backoff(
                client,
                "POST",
                f"/accounts/{payload.accountId}/transactions",
                trace_id=trace_id,
                json=payload.model_dump(mode="json"),
            )
        except RuntimeError:
            database.execute("DELETE FROM events WHERE event_id = ?", (payload.eventId,))
            database.commit()
            metrics["account_service_failures_total"] += 1
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Account Service is unavailable; event was not applied",
            )

        if downstream.status_code >= 400:
            database.execute("DELETE FROM events WHERE event_id = ?", (payload.eventId,))
            database.commit()
            raise HTTPException(
                status_code=downstream.status_code,
                detail=downstream.json().get("detail", "Account Service rejected event"),
            )

        database.execute(
            "UPDATE events SET status = 'APPLIED' WHERE event_id = ?", (payload.eventId,)
        )
        database.commit()
        metrics["events_accepted_total"] += 1
        row = database.execute(
            "SELECT * FROM events WHERE event_id = ?", (payload.eventId,)
        ).fetchone()
        response.status_code = status.HTTP_201_CREATED
        return {"event": row_to_event(row), "duplicate": False}

    @app.get("/events/{event_id}")
    def get_event(event_id: str, database=Depends(db)):
        row = database.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="event not found")
        return row_to_event(row)

    @app.get("/events")
    def list_events(account: str, database=Depends(db)):
        rows = database.execute(
            "SELECT * FROM events WHERE account_id = ? ORDER BY event_timestamp ASC",
            (account,),
        ).fetchall()
        return {"accountId": account, "events": [row_to_event(row) for row in rows]}

    @app.get("/accounts/{account_id}/balance")
    async def get_balance(
        account_id: str,
        client: httpx.AsyncClient = Depends(account_http_client),
    ):
        trace_id = trace_id_ctx.get() or str(uuid.uuid4())
        try:
            downstream = await call_with_backoff(
                client,
                "GET",
                f"/accounts/{account_id}/balance",
                trace_id=trace_id,
            )
        except RuntimeError:
            metrics["account_service_failures_total"] += 1
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Account Service is unreachable",
            )
        return downstream.json()

    return app


app = create_app()
