# Event Ledger

Two-service event ledger implementation for processing financial transaction events with idempotency, out-of-order tolerance, trace propagation, observability, and graceful downstream failure handling.

## Architecture

The system has two independently runnable FastAPI services.

- **Event Gateway API** runs on port `8000`. It validates client event submissions, enforces idempotency with a unique `event_id`, stores local event records, and calls the Account Service over REST.
- **Account Service** runs on port `8001`. It owns account transaction state and computes balances from its own SQLite database.

Each service uses its own SQLite database. They do not share in-process state or database tables.

```text
Client
  |
  | REST
  v
Event Gateway API
  |
  | REST with X-Trace-Id
  v
Account Service
```

## Key Design Choices

- **Idempotency:** `event_id` is the primary key in both services. Duplicate event submissions return the original event and do not apply the transaction again.
- **Out-of-order events:** event queries are sorted by `event_timestamp`, not arrival time. Balance is computed from all applied credits and debits, so arrival order does not change correctness.
- **Resiliency:** Gateway calls to Account Service use timeout plus bounded exponential backoff. If Account Service remains unavailable, Gateway returns `503 Service Unavailable` instead of hanging or returning an unhandled `500`.
- **Trace propagation:** Gateway creates or accepts an `X-Trace-Id` header and forwards it to Account Service. Both services include the trace ID in JSON logs.
- **Observability:** both services expose `/health` and `/metrics`, and emit structured JSON logs.

## Endpoints

Gateway:

```text
POST /events
GET /events/{eventId}
GET /events?account={accountId}
GET /accounts/{accountId}/balance
GET /health
GET /metrics
```

Account Service:

```text
POST /accounts/{accountId}/transactions
GET /accounts/{accountId}/balance
GET /accounts/{accountId}
GET /health
GET /metrics
```

## Run With Docker Compose

```bash
docker compose up --build
```

Gateway will be available at:

```text
http://localhost:8000
```

Account Service will be available at:

```text
http://localhost:8001
```

## Run Locally

Create and activate a virtual environment:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Start Account Service:

```bash
uvicorn account_service.app.main:app --host 0.0.0.0 --port 8001
```

Start Gateway in another terminal:

```bash
set ACCOUNT_SERVICE_URL=http://localhost:8001
uvicorn gateway.app.main:app --host 0.0.0.0 --port 8000
```

## Example Request

```bash
curl -X POST http://localhost:8000/events ^
  -H "Content-Type: application/json" ^
  -H "X-Trace-Id: demo-trace-001" ^
  -d "{\"eventId\":\"evt-001\",\"accountId\":\"acct-123\",\"type\":\"CREDIT\",\"amount\":150.00,\"currency\":\"USD\",\"eventTimestamp\":\"2026-05-15T14:02:11Z\",\"metadata\":{\"source\":\"mainframe-batch\"}}"
```

## Run Tests

```bash
pytest
```

The test suite covers:

- idempotency
- out-of-order event listing
- credit/debit balance computation
- validation failures
- trace propagation
- Gateway behavior when Account Service is unavailable
- full Gateway to Account Service integration flow

## Tradeoffs

This implementation keeps the exercise intentionally focused. In a production system, a durable outbox or queue would be a stronger pattern for accepting events while the Account Service is unavailable. Here, the Gateway returns `503` and does not retain unapplied events, which keeps the client contract clear and prevents partial idempotency state from blocking a later retry.
