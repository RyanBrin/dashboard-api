"""Dashboard API — personal data backend for the dashboard Android app."""
from __future__ import annotations
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
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

    env_name = os.getenv("PLAID_ENV", "sandbox")
    host = "https://production.plaid.com" if env_name == "production" else "https://sandbox.plaid.com"

    cfg = plaid.Configuration(
        host=host,
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


@app.get("/", response_class=HTMLResponse)
async def bank_dashboard():
    env = os.getenv("PLAID_ENV", "sandbox")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Bank Dashboard</title>
  <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; padding: 24px; }}
    h1 {{ font-size: 1.5rem; font-weight: 800; color: #f8fafc; margin-bottom: 4px; }}
    .sub {{ color: #475569; font-size: 0.85rem; margin-bottom: 24px; }}
    .btn {{ background: #3b82f6; color: white; border: none; padding: 10px 20px; border-radius: 8px; font-size: 0.9rem; font-weight: 600; cursor: pointer; }}
    .btn:hover {{ background: #2563eb; }}
    .btn:disabled {{ background: #334155; cursor: not-allowed; }}
    .section {{ background: #111827; border: 1px solid #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 16px; }}
    .section h2 {{ font-size: 0.8rem; font-weight: 700; color: #64748b; text-transform: uppercase; letter-spacing: .07em; margin-bottom: 14px; }}
    .account {{ display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #1e293b; }}
    .account:last-child {{ border-bottom: none; }}
    .acct-name {{ font-size: 0.9rem; font-weight: 600; }}
    .acct-sub {{ font-size: 0.75rem; color: #64748b; margin-top: 2px; }}
    .acct-bal {{ font-size: 1rem; font-weight: 700; color: #60a5fa; }}
    .txn {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #0f172a; font-size: 0.85rem; }}
    .txn:last-child {{ border-bottom: none; }}
    .txn-name {{ color: #cbd5e1; max-width: 60%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .txn-meta {{ color: #475569; font-size: 0.75rem; }}
    .debit {{ color: #f87171; font-weight: 600; }}
    .credit {{ color: #4ade80; font-weight: 600; }}
    #status {{ color: #94a3b8; font-size: 0.85rem; margin-top: 10px; }}
    .env-badge {{ display: inline-block; background: {'#1e3a5f' if env == 'sandbox' else '#1a2e1a'}; color: {'#60a5fa' if env == 'sandbox' else '#4ade80'}; font-size: 0.72rem; font-weight: 700; padding: 3px 8px; border-radius: 10px; margin-left: 8px; text-transform: uppercase; }}
    .total {{ font-size: 1.2rem; font-weight: 800; color: #f8fafc; }}
  </style>
</head>
<body>
  <h1>Bank Dashboard <span class="env-badge">{env}</span></h1>
  <p class="sub">Connect your bank to see live balances and transactions.</p>

  <div style="margin-bottom:20px; display:flex; gap:10px; align-items:center;">
    <button class="btn" id="connectBtn" onclick="connectBank()">Connect Bank</button>
    <button class="btn" id="refreshBtn" onclick="loadData()" style="background:#1e293b">Refresh</button>
    <span id="status"></span>
  </div>

  <div id="accountsSection" style="display:none">
    <div class="section">
      <h2>Accounts</h2>
      <div id="accounts"></div>
      <div style="margin-top:12px; padding-top:12px; border-top:1px solid #1e293b; display:flex; justify-content:space-between;">
        <span style="color:#64748b">Total balance</span>
        <span class="total" id="totalBal">—</span>
      </div>
    </div>
  </div>

  <div id="txnSection" style="display:none">
    <div class="section">
      <h2>Recent Transactions (30 days)</h2>
      <div id="transactions"></div>
    </div>
  </div>

  <script>
    const API = '';

    async function connectBank() {{
      document.getElementById('status').textContent = 'Getting link token...';
      document.getElementById('connectBtn').disabled = true;
      try {{
        const r = await fetch(API + '/plaid/create_link_token', {{method:'POST'}});
        const data = await r.json();
        if (!data.link_token) throw new Error(data.detail || 'No token');

        const handler = Plaid.create({{
          token: data.link_token,
          onSuccess: async (public_token, metadata) => {{
            document.getElementById('status').textContent = 'Linking account...';
            const ex = await fetch(API + '/plaid/exchange_token', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{public_token}})
            }});
            const exData = await ex.json();
            document.getElementById('status').textContent = '✓ ' + (exData.institution || 'Account') + ' connected!';
            loadData();
          }},
          onExit: (err) => {{
            document.getElementById('status').textContent = err ? 'Error: ' + err.display_message : '';
            document.getElementById('connectBtn').disabled = false;
          }}
        }});
        handler.open();
      }} catch(e) {{
        document.getElementById('status').textContent = 'Error: ' + e.message;
        document.getElementById('connectBtn').disabled = false;
      }}
    }}

    async function loadData() {{
      document.getElementById('status').textContent = 'Loading...';
      try {{
        const [acctR, txnR] = await Promise.all([
          fetch(API + '/plaid/accounts').then(r => r.json()),
          fetch(API + '/plaid/transactions?days=30').then(r => r.json())
        ]);

        const accounts = acctR.accounts || [];
        const transactions = txnR.transactions || [];

        if (accounts.length === 0) {{
          document.getElementById('status').textContent = 'No accounts connected yet.';
          return;
        }}

        // Render accounts
        let total = 0;
        document.getElementById('accounts').innerHTML = accounts.map(a => {{
          const bal = a.current_balance || 0;
          total += bal;
          return `<div class="account">
            <div><div class="acct-name">${{a.name}}</div><div class="acct-sub">${{a.institution || ''}} · ${{a.type}} · ${{a.subtype}}</div></div>
            <div class="acct-bal">$${{bal.toLocaleString('en-US', {{minimumFractionDigits:2, maximumFractionDigits:2}})}}</div>
          </div>`;
        }}).join('');
        document.getElementById('totalBal').textContent = '$' + total.toLocaleString('en-US', {{minimumFractionDigits:2}});
        document.getElementById('accountsSection').style.display = 'block';

        // Render transactions
        document.getElementById('transactions').innerHTML = transactions.slice(0,50).map(t => {{
          const amt = t.amount;
          const cls = amt > 0 ? 'debit' : 'credit';
          const sign = amt > 0 ? '-' : '+';
          return `<div class="txn">
            <div><div class="txn-name">${{t.name}}</div><div class="txn-meta">${{t.category}} · ${{t.date}}</div></div>
            <span class="${{cls}}">${{sign}}$${{Math.abs(amt).toLocaleString('en-US', {{minimumFractionDigits:2}})}}</span>
          </div>`;
        }}).join('');
        document.getElementById('txnSection').style.display = 'block';
        document.getElementById('connectBtn').disabled = false;
        document.getElementById('status').textContent = '';
      }} catch(e) {{
        document.getElementById('status').textContent = 'Error: ' + e.message;
      }}
    }}

    // Auto-load on page open
    loadData();
  </script>
</body>
</html>"""
    return html

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
            products=[Products("transactions")],
            additional_consented_products=[Products("investments")],
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
