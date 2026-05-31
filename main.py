"""Dashboard API — personal data backend for the dashboard Android app."""
from __future__ import annotations
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── DB pool ───────────────────────────────────────────────────────────────────

_pool = None

async def get_pool():
    global _pool
    if _pool:
        return _pool
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise HTTPException(status_code=503, detail="DATABASE_URL not configured")
    _pool = await asyncpg.create_pool(url, min_size=1, max_size=3, ssl="require")
    await _init_schema(_pool)
    return _pool

async def _init_schema(pool):
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS plaid_items (
                item_id      TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                institution  TEXT,
                created_at   TIMESTAMPTZ DEFAULT NOW()
            )
        """)

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await get_pool()
    except Exception:
        pass  # DB connects lazily on first request if not available at startup
    yield

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="dashboard-api", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Plaid helpers ─────────────────────────────────────────────────────────────

def _plaid_client():
    import plaid
    from plaid.api import plaid_api

    env_map = {
        "sandbox":     plaid.Environment.Sandbox,
        "development": plaid.Environment.Development,
        "production":  plaid.Environment.Production,
    }
    cfg = plaid.Configuration(
        host=env_map.get(os.getenv("PLAID_ENV", "sandbox"), plaid.Environment.Sandbox),
        api_key={
            "clientId": os.getenv("PLAID_CLIENT_ID", ""),
            "secret":   os.getenv("PLAID_SECRET", ""),
        }
    )
    return plaid_api.PlaidApi(plaid.ApiClient(cfg))


class ExchangeTokenBody(BaseModel):
    public_token: str

# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "dashboard-api",
        "plaid_env": os.getenv("PLAID_ENV", "NOT SET"),
        "plaid_client_id_set": bool(os.getenv("PLAID_CLIENT_ID")),
        "plaid_secret_set": bool(os.getenv("PLAID_SECRET")),
        "database_url_set": bool(os.getenv("DATABASE_URL")),
    }

# ── Plaid endpoints ───────────────────────────────────────────────────────────

@app.post("/plaid/create_link_token")
async def create_link_token():
    try:
        from plaid.model.link_token_create_request import LinkTokenCreateRequest
        from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
        from plaid.model.products import Products
        from plaid.model.country_code import CountryCode

        client = _plaid_client()
        req = LinkTokenCreateRequest(
            products=[Products("transactions"), Products("investments")],
            client_name="Dashboard App",
            country_codes=[CountryCode("US")],
            language="en",
            user=LinkTokenCreateRequestUser(client_user_id="ryan-dashboard"),
        )
        resp = client.link_token_create(req)
        return {"link_token": resp["link_token"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/plaid/exchange_token")
async def exchange_token(body: ExchangeTokenBody):
    try:
        from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
        from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
        from plaid.model.country_code import CountryCode

        client = _plaid_client()
        resp = client.item_public_token_exchange(
            ItemPublicTokenExchangeRequest(public_token=body.public_token)
        )
        access_token = resp["access_token"]
        item_id = resp["item_id"]

        # Try to get institution name
        institution_name = None
        try:
            item_resp = client.item_get({"access_token": access_token})
            inst_id = item_resp["item"]["institution_id"]
            if inst_id:
                inst_resp = client.institutions_get_by_id(
                    InstitutionsGetByIdRequest(institution_id=inst_id, country_codes=[CountryCode("US")])
                )
                institution_name = inst_resp["institution"]["name"]
        except Exception:
            pass

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO plaid_items (item_id, access_token, institution)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (item_id) DO UPDATE SET access_token=$2, institution=$3""",
                item_id, access_token, institution_name
            )
        return {"ok": True, "item_id": item_id, "institution": institution_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/plaid/accounts")
async def get_accounts():
    try:
        from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest

        client = _plaid_client()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT item_id, access_token, institution FROM plaid_items")

        if not rows:
            return {"accounts": [], "message": "No accounts connected yet"}

        all_accounts = []
        for row in rows:
            try:
                resp = client.accounts_balance_get(
                    AccountsBalanceGetRequest(access_token=row["access_token"])
                )
                for acct in resp["accounts"]:
                    all_accounts.append({
                        "account_id":        acct["account_id"],
                        "name":              acct["name"],
                        "official_name":     acct.get("official_name"),
                        "type":              str(acct["type"]),
                        "subtype":           str(acct.get("subtype", "")),
                        "current_balance":   acct["balances"]["current"],
                        "available_balance": acct["balances"]["available"],
                        "currency":          acct["balances"].get("iso_currency_code", "USD"),
                        "institution":       row["institution"],
                    })
            except Exception:
                continue
        return {"accounts": all_accounts}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/plaid/transactions")
async def get_transactions(days: int = 30):
    try:
        from plaid.model.transactions_get_request import TransactionsGetRequest
        from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
        import datetime as dt

        client = _plaid_client()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT access_token FROM plaid_items")

        if not rows:
            return {"transactions": [], "message": "No accounts connected yet"}

        end = dt.date.today()
        start = end - dt.timedelta(days=days)
        all_txns = []

        for row in rows:
            try:
                resp = client.transactions_get(
                    TransactionsGetRequest(
                        access_token=row["access_token"],
                        start_date=start,
                        end_date=end,
                        options=TransactionsGetRequestOptions(count=250),
                    )
                )
                for t in resp["transactions"]:
                    all_txns.append({
                        "transaction_id": t["transaction_id"],
                        "date":           str(t["date"]),
                        "name":           t["name"],
                        "amount":         t["amount"],
                        "category":       t["category"][0] if t.get("category") else "Other",
                        "account_id":     t["account_id"],
                        "pending":        t["pending"],
                    })
            except Exception:
                continue

        all_txns.sort(key=lambda x: x["date"], reverse=True)
        return {"transactions": all_txns, "count": len(all_txns)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/plaid/investments")
async def get_investments():
    """Returns investment holdings — works with Fidelity, Schwab, etc."""
    try:
        from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest

        client = _plaid_client()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT access_token, institution FROM plaid_items")

        if not rows:
            return {"holdings": [], "message": "No accounts connected yet"}

        all_holdings = []
        for row in rows:
            try:
                resp = client.investments_holdings_get(
                    InvestmentsHoldingsGetRequest(access_token=row["access_token"])
                )
                securities = {s["security_id"]: s for s in resp["securities"]}
                for h in resp["holdings"]:
                    sec = securities.get(h["security_id"], {})
                    all_holdings.append({
                        "account_id":      h["account_id"],
                        "security_id":     h["security_id"],
                        "ticker":          sec.get("ticker_symbol") or sec.get("name", "—"),
                        "name":            sec.get("name", ""),
                        "quantity":        h["quantity"],
                        "cost_basis":      h.get("cost_basis"),
                        "institution_price": h["institution_price"],
                        "institution_value": h["institution_value"],
                        "currency":        h.get("iso_currency_code", "USD"),
                        "institution":     row["institution"],
                    })
            except Exception:
                continue

        return {"holdings": all_holdings, "total_value": sum(h["institution_value"] for h in all_holdings)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/plaid/disconnect/{item_id}")
async def disconnect_account(item_id: str):
    try:
        from plaid.model.item_remove_request import ItemRemoveRequest

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT access_token FROM plaid_items WHERE item_id=$1", item_id)
            if not row:
                raise HTTPException(status_code=404, detail="Account not found")
            client = _plaid_client()
            client.item_remove(ItemRemoveRequest(access_token=row["access_token"]))
            await conn.execute("DELETE FROM plaid_items WHERE item_id=$1", item_id)
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
