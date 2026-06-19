# Event Ledger

Two-service FastAPI event ledger for processing financial transaction events with idempotency, out-of-order handling, trace propagation, resiliency, observability, and integration tests.

## Architecture

```text
Client
  |
  | REST
  v
Event Gateway API  :8000
  |
  | REST + X-Trace-Id
  v
Account Service    :8001
```

The services are independently runnable processes:

- **Event Gateway API** validates client events, stores event records, enforces idempotency, lists events by event timestamp, and calls the Account Service.
- **Account Service** stores applied account transactions, computes balances, and returns account details.

Each service owns its own SQLite database. They do not share tables, process memory, or repository objects.

## Design Choices

- **Idempotency:** `event_id` is the primary key in both services. A duplicate `eventId` returns the original record with `200 OK` and does not apply the transaction again.
- **Out-of-order tolerance:** event listings sort by `eventTimestamp`, not arrival time. Balances are computed from all account transactions, so arrival order does not affect correctness.
- **Money handling:** amounts are converted to integer cents before storage to avoid floating-point precision issues.
- **Trace propagation:** Gateway accepts or generates `X-Trace-Id`, logs it, forwards it to Account Service, and returns it on the response.
- **Resiliency:** Gateway uses timeout + retry with bounded exponential backoff for Account Service calls. I chose this over a circuit breaker because it is stateless, simple to reason about, and deterministic to test for this small synchronous system.
- **Graceful degradation:** if Account Service is unavailable, `POST /events` returns `503` and removes the local pending event so a later retry is not incorrectly treated as a duplicate. Gateway-local event reads still work.

## API

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

Services:

```text
Gateway:         http://localhost:8000
Account Service: http://localhost:8001
Swagger docs:    http://localhost:8000/docs and http://localhost:8001/docs
```

Stop:

```bash
docker compose down
```

## Run Locally

Create a virtual environment and install dependencies.

PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

macOS/Linux:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Start Account Service:

```bash
uvicorn account_service.app.main:app --host 0.0.0.0 --port 8001
```

Start Gateway in another terminal:

PowerShell:

```powershell
$env:ACCOUNT_SERVICE_URL = "http://localhost:8001"
uvicorn gateway.app.main:app --host 0.0.0.0 --port 8000
```

macOS/Linux:

```bash
ACCOUNT_SERVICE_URL=http://localhost:8001 uvicorn gateway.app.main:app --host 0.0.0.0 --port 8000
```

## Example Request

PowerShell:

```powershell
$body = @{
  eventId = "evt-001"
  accountId = "acct-123"
  type = "CREDIT"
  amount = 150.00
  currency = "USD"
  eventTimestamp = "2026-05-15T14:02:11Z"
  metadata = @{ source = "mainframe-batch" }
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8000/events" `
  -Headers @{ "X-Trace-Id" = "demo-trace-001" } `
  -ContentType "application/json" `
  -Body $body
```

macOS/Linux:

```bash
curl -X POST http://localhost:8000/events \
  -H "Content-Type: application/json" \
  -H "X-Trace-Id: demo-trace-001" \
  -d '{"eventId":"evt-001","accountId":"acct-123","type":"CREDIT","amount":150.00,"currency":"USD","eventTimestamp":"2026-05-15T14:02:11Z","metadata":{"source":"mainframe-batch"}}'
```

Useful follow-up calls:

```bash
curl "http://localhost:8000/events?account=acct-123"
curl "http://localhost:8000/accounts/acct-123/balance"
curl "http://localhost:8000/metrics"
```

## Observability

Both services expose:

- `GET /health` for service and database health
- `GET /metrics` for simple JSON counters
- JSON structured logs with timestamp, level, service name, message, and trace ID

The metrics are intentionally lightweight for the exercise. A production version would likely use Prometheus and OpenTelemetry exporters.

## Tests

Run:

```bash
pytest
```

The test suite covers:

- idempotency
- out-of-order event ordering
- credit/debit balance computation
- validation failures
- `GET /events/{eventId}`
- Account Service account details
- trace propagation
- Account Service outage behavior
- full Gateway to Account Service integration flow

The tests use `httpx.ASGITransport` to exercise the actual FastAPI apps without needing to start real network servers.

## Project Structure

```text
account_service/
  app/
gateway/
  app/
tests/
docker-compose.yml
requirements.txt
pytest.ini
README.md
```

## Tradeoffs

This implementation keeps the scope focused for the take-home. When Account Service is down, the Gateway fails fast with `503` instead of accepting events into a queue. In production, I would consider a durable outbox or message queue, OpenTelemetry Collector with Jaeger or Zipkin, Prometheus metrics, and contract tests between services.
