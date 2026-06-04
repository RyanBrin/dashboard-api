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

app = FastAPI(title="Nexus API", lifespan=lifespan)
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
        "service": "nexus-api",
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
  <title>Nexus · Banking</title>
  <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #080F1A; color: #e2e8f0; padding: 24px; }}
    h1 {{ font-size: 1.5rem; font-weight: 800; color: #f8fafc; margin-bottom: 4px; }}
    .sub {{ color: #475569; font-size: 0.85rem; margin-bottom: 24px; }}
    .btn {{ background: #0AAAFF; color: #080F1A; border: none; padding: 10px 20px; border-radius: 8px; font-size: 0.9rem; font-weight: 600; cursor: pointer; }}
    .btn:hover {{ background: #2563eb; }}
    .btn:disabled {{ background: #334155; cursor: not-allowed; }}
    .section {{ background: #111827; border: 1px solid #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 16px; }}
    .section h2 {{ font-size: 0.8rem; font-weight: 700; color: #64748b; text-transform: uppercase; letter-spacing: .07em; margin-bottom: 14px; }}
    .account {{ display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #1e293b; }}
    .account:last-child {{ border-bottom: none; }}
    .acct-name {{ font-size: 0.9rem; font-weight: 600; }}
    .acct-sub {{ font-size: 0.75rem; color: #64748b; margin-top: 2px; }}
    .acct-bal {{ font-size: 1rem; font-weight: 700; color: #0AAAFF; }}
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
  <h1>Nexus <span style="color:#475569;font-weight:400;font-size:0.9rem">/ banking</span> <span class="env-badge">{env}</span></h1>
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
        if (!data.link_token) {{
          const raw = data.detail || '';
          let msg = 'Could not start bank connection.';
          if (raw.includes('redirect_uri') || raw.includes('INVALID_REDIRECT_URI'))
            msg = 'OAuth redirect URI not registered in Plaid dashboard.';
          else if (raw.includes('INVALID_API_KEYS') || raw.includes('403'))
            msg = 'Invalid Plaid credentials. Check Railway environment variables.';
          throw new Error(msg);
        }}

        const handler = Plaid.create({{
          token: data.link_token,
          onSuccess: async (public_token, metadata) => {{
            document.getElementById('status').textContent = 'Saving connection...';
            try {{
              const ex = await fetch(API + '/plaid/exchange_token', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{public_token}})
              }});
              if (!ex.ok) {{
                const errData = await ex.json().catch(() => ({{}}));
                const raw = errData.detail || '';
                // Translate common Plaid errors into human-readable messages
                let msg = 'Could not save connection. Try again.';
                if (raw.includes('redirect_uri') || raw.includes('INVALID_REDIRECT_URI'))
                  msg = 'OAuth redirect URI not configured. Check Plaid dashboard settings.';
                else if (raw.includes('INVALID_PUBLIC_TOKEN'))
                  msg = 'Connection expired — please try again.';
                else if (raw.includes('INVALID_API_KEYS') || raw.includes('403'))
                  msg = 'Invalid Plaid API credentials. Check Railway PLAID_SECRET.';
                else if (raw.includes('DATABASE_URL') || raw.includes('503'))
                  msg = 'Database not configured. Check Railway DATABASE_URL.';
                throw new Error(msg);
              }}
              const exData = await ex.json();
              if (!exData.ok) throw new Error('Connection could not be saved. Try again.');
              document.getElementById('status').textContent = '✓ ' + (exData.institution || 'Account') + ' connected!';
              document.getElementById('connectBtn').disabled = false;
              loadData();
            }} catch(exErr) {{
              document.getElementById('status').textContent = '✗ ' + exErr.message;
              document.getElementById('connectBtn').disabled = false;
            }}
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

        # redirect_uri required for OAuth institutions (Chase, BofA, Wells Fargo, etc.)
        redirect_uri = os.getenv("PLAID_REDIRECT_URI", "")
        kwargs: dict = dict(
            products=[Products("transactions"), Products("balance")],
            additional_consented_products=[Products("investments")],
            client_name="Nexus",
            country_codes=[CountryCode("US")],
            language="en",
            user=LinkTokenCreateRequestUser(client_user_id="ryan-nexus"),
        )
        if redirect_uri:
            kwargs["redirect_uri"] = redirect_uri

        resp = client.link_token_create(LinkTokenCreateRequest(**kwargs))
        import logging; logging.getLogger(__name__).info("Plaid link token created")
        return {"link_token": resp["link_token"]}
    except Exception as e:
        import logging; logging.getLogger(__name__).error("Plaid link token creation failed: %s", type(e).__name__)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/plaid/exchange_token")
async def exchange_token(body: ExchangeTokenBody):
    import logging
    log = logging.getLogger(__name__)
    try:
        from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
        from plaid.model.item_get_request import ItemGetRequest
        from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
        from plaid.model.country_code import CountryCode

        log.info("Plaid token exchange started")
        client = _plaid_client()

        resp = client.item_public_token_exchange(
            ItemPublicTokenExchangeRequest(public_token=body.public_token)
        )
        access_token = resp["access_token"]
        item_id      = resp["item_id"]
        log.info("Plaid token exchange complete — item stored")

        # Lookup institution name using proper SDK model (not a dict)
        institution_name = None
        try:
            item_resp = client.item_get(ItemGetRequest(access_token=access_token))
            inst_id = item_resp["item"].get("institution_id")
            if inst_id:
                inst_resp = client.institutions_get_by_id(
                    InstitutionsGetByIdRequest(
                        institution_id=inst_id, country_codes=[CountryCode("US")]
                    )
                )
                institution_name = inst_resp["institution"]["name"]
                log.info("Institution identified")
        except Exception as inst_err:
            log.warning("Could not look up institution name: %s", type(inst_err).__name__)

        # Persist access token server-side — never returned to client
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO plaid_items (item_id, access_token, institution)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (item_id) DO UPDATE SET access_token=$2, institution=$3""",
                item_id, access_token, institution_name
            )
        log.info("Plaid item saved to database. Institution: %s", institution_name or "unknown")
        return {"ok": True, "item_id": item_id, "institution": institution_name}

    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Plaid exchange failed: %s", type(e).__name__)
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

        import logging, json as _json
        log = logging.getLogger(__name__)

        def _parse_accounts(resp_accounts, institution):
            result = []
            for acct in resp_accounts:
                result.append({
                    "account_id":        acct["account_id"],
                    "name":              acct["name"],
                    "official_name":     acct.get("official_name"),
                    "type":              str(acct["type"]),
                    "subtype":           str(acct.get("subtype", "")),
                    "current_balance":   acct["balances"]["current"],
                    "available_balance": acct["balances"]["available"],
                    "currency":          acct["balances"].get("iso_currency_code", "USD"),
                    "institution":       institution,
                })
            return result

        all_accounts = []
        for row in rows:
            try:
                # Try real-time balance first (requires balance product)
                resp = client.accounts_balance_get(
                    AccountsBalanceGetRequest(access_token=row["access_token"])
                )
                all_accounts.extend(_parse_accounts(resp["accounts"], row["institution"]))
                log.info("Fetched %d accounts (balance) from %s", len(resp["accounts"]), row["institution"])
            except Exception as e:
                # INVALID_PRODUCT: item was connected before balance product was added.
                # Fall back to accounts_get which works with transactions product alone.
                plaid_code = None
                try:
                    body = getattr(e, "body", None)
                    if body:
                        parsed = _json.loads(body) if isinstance(body, str) else body
                        plaid_code = parsed.get("error_code")
                except Exception:
                    pass

                if plaid_code == "INVALID_PRODUCT":
                    try:
                        from plaid.model.accounts_get_request import AccountsGetRequest
                        resp2 = client.accounts_get(
                            AccountsGetRequest(access_token=row["access_token"])
                        )
                        all_accounts.extend(_parse_accounts(resp2["accounts"], row["institution"]))
                        log.info("Fetched %d accounts (fallback) from %s", len(resp2["accounts"]), row["institution"])
                    except Exception as e2:
                        log.error("Fallback accounts_get also failed for %s: %s", row["institution"], type(e2).__name__)
                else:
                    log.error("Plaid accounts fetch failed for %s: %s (code: %s)", row["institution"], type(e).__name__, plaid_code)

        log.info("Total accounts fetched: %d", len(all_accounts))
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


@app.get("/finance/summary")
async def finance_summary():
    """Single aggregated finance summary for the Android Home screen.
    Calculates totals from Plaid accounts and current-month transactions.
    No Plaid tokens are returned — server-side only.
    """
    import datetime as dt

    # ── accounts ─────────────────────────────────────────────────────────────
    try:
        from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
        client = _plaid_client()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT access_token, institution FROM plaid_items")
    except Exception:
        rows = []

    accounts = []
    for row in (rows or []):
        try:
            resp = client.accounts_balance_get(
                AccountsBalanceGetRequest(access_token=row["access_token"])
            )
            for acct in resp["accounts"]:
                accounts.append({
                    "account_id":        acct["account_id"],
                    "name":              acct["name"],
                    "official_name":     acct.get("official_name"),
                    "type":              str(acct["type"]),
                    "subtype":           str(acct.get("subtype", "")),
                    "current_balance":   acct["balances"]["current"] or 0.0,
                    "available_balance": acct["balances"]["available"],
                    "currency":          acct["balances"].get("iso_currency_code", "USD"),
                    "institution":       row["institution"],
                })
        except Exception:
            continue

    # ── categorise accounts ───────────────────────────────────────────────────
    credit_cards = [a for a in accounts if a["type"] in ("credit",)]
    depository   = [a for a in accounts if a["type"] in ("depository",)]
    investment   = [a for a in accounts if a["type"] in ("investment",)]
    loans        = [a for a in accounts if a["type"] in ("loan",)]

    # Build a set of depository account IDs for spending filter below
    depository_ids = {a["account_id"] for a in depository}

    # Definitions:
    # total_cash          = sum of depository (checking/savings) current balances
    # total_credit_debt   = sum of credit card current balances (what is owed, a positive number)
    # total_investments   = sum of investment account balances
    # total_loans         = sum of loan balances (mortgages, auto, student)
    # net_worth           = cash + investments − credit_debt − loans
    total_cash        = sum(a["current_balance"] for a in depository)
    total_credit_debt = sum(a["current_balance"] for a in credit_cards)
    total_investments = sum(a["current_balance"] for a in investment)
    total_loans       = sum(a["current_balance"] for a in loans)
    net_worth         = total_cash + total_investments - total_credit_debt - total_loans

    # ── current-month transactions ────────────────────────────────────────────
    # monthly_spending = sum of positive (debit) transactions on DEPOSITORY accounts only,
    # excluding pending and excluding Transfer/Payment categories.
    # Rationale: credit card purchases appear on the credit account; counting them here
    # would double-count with the depository payment. Transfer/Payment categories are
    # balance moves (credit card payments, inter-account transfers), not real spending.
    EXCLUDED_CATEGORIES = {"transfer", "payment", "credit card", "payroll", "interest"}

    transactions = []
    monthly_spending = 0.0
    today = dt.date.today()
    month_start = today.replace(day=1)

    try:
        from plaid.model.transactions_get_request import TransactionsGetRequest
        from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
        for row in (rows or []):
            try:
                resp = client.transactions_get(
                    TransactionsGetRequest(
                        access_token=row["access_token"],
                        start_date=month_start,
                        end_date=today,
                        options=TransactionsGetRequestOptions(count=250),
                    )
                )
                for t in resp["transactions"]:
                    amt      = t["amount"]
                    cat_raw  = t["category"][0] if t.get("category") else "Other"
                    acct_id  = t["account_id"]
                    pending  = t["pending"]
                    txn = {
                        "transaction_id": t["transaction_id"],
                        "date":           str(t["date"]),
                        "name":           t["name"],
                        "amount":         amt,
                        "category":       cat_raw,
                        "account_id":     acct_id,
                        "pending":        pending,
                    }
                    transactions.append(txn)

                    # Only count as spending: depository, positive, non-pending, non-transfer
                    is_depository_txn  = acct_id in depository_ids
                    is_excluded_cat    = cat_raw.lower() in EXCLUDED_CATEGORIES
                    if amt > 0 and not pending and is_depository_txn and not is_excluded_cat:
                        monthly_spending += amt
            except Exception:
                continue
    except Exception:
        pass

    transactions.sort(key=lambda x: x["date"], reverse=True)

    # Sandbox flag so the Android app can show an appropriate disclaimer
    plaid_env = os.getenv("PLAID_ENV", "sandbox")
    has_cash_or_investments = total_cash > 0 or total_investments > 0

    return {
        "total_cash":              round(total_cash, 2),
        "total_credit_card_debt":  round(total_credit_debt, 2),
        "total_investments":       round(total_investments, 2),
        "total_loans":             round(total_loans, 2),
        "net_worth":               round(net_worth, 2),
        "monthly_spending":        round(monthly_spending, 2),
        "accounts":                accounts,
        "credit_cards":            credit_cards,
        "recent_transactions":     transactions[:25],
        "has_connected_accounts":  len(accounts) > 0,
        "has_cash_or_investments": has_cash_or_investments,
        "plaid_env":               plaid_env,
        "last_synced":             dt.datetime.utcnow().isoformat() + "Z",
    }


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
