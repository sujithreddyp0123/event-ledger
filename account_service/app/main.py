import json
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .database import connect, migrate
from .logging_config import configure_logging

logger = configure_logging("account-service")
metrics = {"requests_total": 0, "errors_total": 0, "transactions_applied_total": 0}


class TransactionIn(BaseModel):
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


def row_to_transaction(row: Any) -> dict[str, Any]:
    return {
        "eventId": row["event_id"],
        "accountId": row["account_id"],
        "type": row["type"],
        "amount": cents_to_amount(row["amount_cents"]),
        "currency": row["currency"],
        "eventTimestamp": row["event_timestamp"],
        "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
    }


def create_app(db_path: str | None = None) -> FastAPI:
    conn = connect(db_path)
    migrate(conn)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        conn.close()

    app = FastAPI(title="Account Service", version="1.0.0", lifespan=lifespan)
    app.state.db = conn
    app.state.last_trace_id = None

    @app.middleware("http")
    async def request_logging(request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id")
        app.state.last_trace_id = trace_id
        metrics["requests_total"] += 1
        logger.info(
            f"{request.method} {request.url.path}",
            extra={"trace_id": trace_id},
        )
        try:
            return await call_next(request)
        except Exception:
            metrics["errors_total"] += 1
            logger.exception("unhandled request error", extra={"trace_id": trace_id})
            raise

    def db():
        return app.state.db

    @app.get("/health")
    def health(database=Depends(db)):
        database.execute("SELECT 1").fetchone()
        return {"status": "ok", "service": "account-service", "database": "ok"}

    @app.get("/metrics")
    def get_metrics():
        return metrics

    @app.post("/accounts/{account_id}/transactions")
    def apply_transaction(
        account_id: str,
        payload: TransactionIn,
        response: Response,
        x_trace_id: str | None = Header(default=None, alias="X-Trace-Id"),
        database=Depends(db),
    ):
        if payload.accountId != account_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="accountId in path and payload must match",
            )
        if payload.type not in {"CREDIT", "DEBIT"}:
            raise HTTPException(status_code=400, detail="type must be CREDIT or DEBIT")

        existing = database.execute(
            "SELECT * FROM transactions WHERE event_id = ?", (payload.eventId,)
        ).fetchone()
        if existing:
            response.status_code = status.HTTP_200_OK
            return {"transaction": row_to_transaction(existing), "duplicate": True}

        database.execute(
            """
            INSERT INTO transactions
                (event_id, account_id, type, amount_cents, currency, event_timestamp, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.eventId,
                payload.accountId,
                payload.type,
                amount_to_cents(payload.amount),
                payload.currency,
                payload.eventTimestamp.isoformat().replace("+00:00", "Z"),
                json.dumps(payload.metadata) if payload.metadata else None,
            ),
        )
        database.commit()
        metrics["transactions_applied_total"] += 1
        logger.info("transaction applied", extra={"trace_id": x_trace_id})
        row = database.execute(
            "SELECT * FROM transactions WHERE event_id = ?", (payload.eventId,)
        ).fetchone()
        response.status_code = status.HTTP_201_CREATED
        return {"transaction": row_to_transaction(row), "duplicate": False}

    @app.get("/accounts/{account_id}/balance")
    def get_balance(account_id: str, database=Depends(db)):
        rows = database.execute(
            "SELECT type, amount_cents, currency FROM transactions WHERE account_id = ?",
            (account_id,),
        ).fetchall()
        balance_cents = sum(
            row["amount_cents"] if row["type"] == "CREDIT" else -row["amount_cents"]
            for row in rows
        )
        currency = rows[0]["currency"] if rows else "USD"
        return {
            "accountId": account_id,
            "balance": cents_to_amount(balance_cents),
            "currency": currency,
        }

    @app.get("/accounts/{account_id}")
    def get_account(account_id: str, database=Depends(db)):
        transactions = database.execute(
            "SELECT * FROM transactions WHERE account_id = ? ORDER BY event_timestamp DESC LIMIT 10",
            (account_id,),
        ).fetchall()
        balance = get_balance(account_id, database)
        return {
            "accountId": account_id,
            "balance": balance["balance"],
            "currency": balance["currency"],
            "recentTransactions": [row_to_transaction(row) for row in transactions],
        }

    return app


app = create_app()
