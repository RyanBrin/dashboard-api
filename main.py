"""Dashboard API — personal data backend for the dashboard Android app."""
from __future__ import annotations
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
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
            );

            -- Manual portfolio accounts (Fidelity, Schwab, etc.)
            CREATE TABLE IF NOT EXISTS portfolio_accounts (
                id           SERIAL PRIMARY KEY,
                account_name TEXT NOT NULL,
                institution  TEXT NOT NULL DEFAULT 'Fidelity',
                account_type TEXT NOT NULL DEFAULT 'brokerage',
                cash_balance NUMERIC(16,4) NOT NULL DEFAULT 0,
                created_at   TIMESTAMPTZ DEFAULT NOW(),
                updated_at   TIMESTAMPTZ DEFAULT NOW()
            );

            -- Manual holdings within a portfolio account
            CREATE TABLE IF NOT EXISTS portfolio_holdings (
                id               SERIAL PRIMARY KEY,
                account_id       INTEGER NOT NULL REFERENCES portfolio_accounts(id) ON DELETE CASCADE,
                symbol           TEXT NOT NULL,
                description      TEXT,
                quantity         NUMERIC(20,8) NOT NULL DEFAULT 0,
                average_cost     NUMERIC(16,4),
                current_price    NUMERIC(16,4),
                current_value    NUMERIC(16,4) NOT NULL DEFAULT 0,
                cost_basis       NUMERIC(16,4),
                unrealized_pnl   NUMERIC(16,4),
                updated_at       TIMESTAMPTZ DEFAULT NOW()
            );
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

# Serve logo and other static assets from ./static/
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# ── PIN session gate ──────────────────────────────────────────────────────────
_DASHBOARD_PIN   = os.getenv("DASHBOARD_PIN", "")      # set in Railway env vars
_PIN_SESSION_KEY = "nx_session"
_PIN_SESSIONS: set[str] = set()   # in-memory; cleared on restart (intentional)

def _is_authed(request: Request) -> bool:
    """Return True if the request carries a valid session cookie or no PIN is set."""
    if not _DASHBOARD_PIN:
        return True
    return request.cookies.get(_PIN_SESSION_KEY, "") in _PIN_SESSIONS

def _pin_page(redirect: str = "/") -> HTMLResponse:
    """Return the PIN entry HTML page."""
    logo = '/static/nexus.png' if os.path.isdir(_STATIC_DIR) else ''
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nexus — Secure Access</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#080F1A;display:flex;align-items:center;justify-content:center;
       min-height:100vh;font-family:'Inter',system-ui,sans-serif;color:#E2E8F0}}
  .card{{background:#111827;border:1px solid #1E293B;border-radius:20px;
         padding:40px 36px;width:340px;text-align:center;box-shadow:0 0 40px #0AAAFF18}}
  .logo{{width:84px;height:84px;border-radius:20px;margin:0 auto 20px;object-fit:contain}}
  .logo-placeholder{{width:84px;height:84px;border-radius:20px;margin:0 auto 20px;
    background:radial-gradient(circle,#0AAAFF22,#8B5CF612,transparent);
    display:flex;align-items:center;justify-content:center;font-size:36px}}
  h1{{font-size:26px;font-weight:800;letter-spacing:.5px;margin-bottom:4px}}
  .sub{{font-size:12px;color:#475569;margin-bottom:32px}}
  .dots{{display:flex;gap:14px;justify-content:center;margin-bottom:24px}}
  .dot{{width:13px;height:13px;border-radius:50%;background:#1E293B;
        transition:background .15s,box-shadow .15s}}
  .dot.filled{{background:#0AAAFF;box-shadow:0 0 8px #0AAAFF88}}
  .dot.error{{background:#EF4444;box-shadow:0 0 8px #EF444488}}
  .pad{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}
  .key{{background:#1E293B;border:none;border-radius:50%;width:68px;height:68px;
        font-size:22px;font-weight:500;color:#E2E8F0;cursor:pointer;
        transition:background .12s,transform .08s;margin:auto}}
  .key:hover{{background:#334155;transform:scale(1.05)}}
  .key:active{{background:#0AAAFF22;transform:scale(.97)}}
  .key.del{{font-size:16px;color:#94A3B8}}
  .key.empty{{visibility:hidden}}
  .err{{color:#EF4444;font-size:12px;height:16px;margin-top:8px}}
  @keyframes shake{{0%,100%{{transform:translateX(0)}}20%,60%{{transform:translateX(-8px)}}
                    40%,80%{{transform:translateX(8px)}}}}
  .shake{{animation:shake .35s ease}}
</style>
</head>
<body>
<div class="card">
  {'<img class="logo" src="'+logo+'" alt="Nexus logo">' if logo else '<div class="logo-placeholder">◈</div>'}
  <h1>Nexus</h1>
  <p class="sub">Personal Command Center</p>
  <div class="dots" id="dots">
    <div class="dot" id="d0"></div>
    <div class="dot" id="d1"></div>
    <div class="dot" id="d2"></div>
    <div class="dot" id="d3"></div>
  </div>
  <div class="pad">
    {''.join(f'<button class="key" onclick="tap({i})">{i}</button>' for i in range(1,10))}
    <button class="key empty"></button>
    <button class="key" onclick="tap(0)">0</button>
    <button class="key del" onclick="del_()">⌫</button>
  </div>
  <div class="err" id="err"></div>
</div>
<script>
let pin='';
const LEN=4;
function tap(n){{
  if(pin.length>=LEN) return;
  pin+=n;
  render();
  if(pin.length===LEN) submit();
}}
function del_(){{pin=pin.slice(0,-1);render();}}
function render(){{
  for(let i=0;i<LEN;i++){{
    const d=document.getElementById('d'+i);
    d.className='dot'+(i<pin.length?' filled':'');
  }}
}}
async function submit(){{
  const r=await fetch('/pin-auth',{{method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{pin,redirect:'{redirect}'}})
  }});
  const j=await r.json();
  if(j.ok){{window.location.href=j.redirect;}}
  else{{
    for(let i=0;i<LEN;i++) document.getElementById('d'+i).className='dot error';
    document.getElementById('dots').classList.add('shake');
    document.getElementById('err').textContent='Incorrect PIN';
    setTimeout(()=>{{
      pin='';render();
      document.getElementById('dots').classList.remove('shake');
      document.getElementById('err').textContent='';
    }},600);
  }}
}}
document.addEventListener('keydown',e=>{{
  if(e.key>='0'&&e.key<='9') tap(parseInt(e.key));
  else if(e.key==='Backspace') del_();
}});
</script>
</body>
</html>"""
    return HTMLResponse(content=html)

@app.post("/pin-auth")
async def pin_auth(body: dict, response: Response):
    """Verify PIN and issue a session cookie."""
    if not _DASHBOARD_PIN or body.get("pin") == _DASHBOARD_PIN:
        import secrets
        token = secrets.token_hex(24)
        _PIN_SESSIONS.add(token)
        response.set_cookie(_PIN_SESSION_KEY, token, httponly=True, samesite="strict", max_age=43200)
        return {"ok": True, "redirect": body.get("redirect", "/")}
    return {"ok": False}

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
async def bank_dashboard(request: Request):
    if not _is_authed(request):
        return _pin_page(redirect="/")
    env = os.getenv("PLAID_ENV", "sandbox")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Nexus · Banking</title>
  <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #080F1A; color: #e2e8f0;
      padding: 16px; max-width: 680px; margin: 0 auto;
    }}
    h1 {{ font-size: 1.4rem; font-weight: 800; color: #f8fafc; margin-bottom: 4px; }}
    .sub {{ color: #475569; font-size: 0.82rem; margin-bottom: 20px; }}
    .btn {{ background: #0AAAFF; color: #080F1A; border: none; padding: 9px 18px;
            border-radius: 8px; font-size: 0.88rem; font-weight: 600; cursor: pointer;
            white-space: nowrap; flex-shrink: 0; }}
    .btn:hover {{ background: #2563eb; }}
    .btn:disabled {{ background: #334155; color: #64748b; cursor: not-allowed; }}
    .btn-secondary {{ background: #1e293b; color: #94a3b8; }}
    .btn-secondary:hover {{ background: #273548; color: #e2e8f0; }}
    .actions {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 20px; }}
    #status {{ color: #94a3b8; font-size: 0.82rem; min-height: 1.2em; }}
    .section {{ background: #111827; border: 1px solid #1e293b; border-radius: 12px;
                padding: 16px; margin-bottom: 14px; overflow: hidden; }}
    .section-title {{ font-size: 0.72rem; font-weight: 700; color: #64748b;
                      text-transform: uppercase; letter-spacing: .07em; margin-bottom: 12px; }}
    .group-label {{ font-size: 0.68rem; font-weight: 700; color: #475569;
                    text-transform: uppercase; letter-spacing: .06em;
                    padding: 8px 0 4px; margin-top: 4px; }}
    /* Account row — flex with min-width:0 prevents overflow */
    .account {{ display: flex; align-items: center; justify-content: space-between;
                gap: 12px; padding: 9px 0; border-bottom: 1px solid #1e293b; }}
    .account:last-child {{ border-bottom: none; }}
    .acct-info {{ min-width: 0; flex: 1; }}
    .acct-name {{ font-size: 0.88rem; font-weight: 600; white-space: nowrap;
                  overflow: hidden; text-overflow: ellipsis; }}
    .acct-sub {{ font-size: 0.72rem; color: #64748b; margin-top: 2px; }}
    .acct-bal {{ font-size: 0.95rem; font-weight: 700; white-space: nowrap; flex-shrink: 0; }}
    .bal-asset {{ color: #0AAAFF; }}
    .bal-debt  {{ color: #f87171; }}
    /* Summary rows */
    .summary-row {{ display: flex; justify-content: space-between; align-items: center;
                    padding: 7px 0; border-top: 1px solid #1e293b; margin-top: 4px; }}
    .summary-label {{ font-size: 0.8rem; color: #64748b; }}
    .summary-val   {{ font-size: 1rem; font-weight: 800; color: #f8fafc; }}
    .summary-val.green {{ color: #4ade80; }}
    .summary-val.red   {{ color: #f87171; }}
    .summary-val.blue  {{ color: #0AAAFF; }}
    /* Transaction row — two-column, no overflow */
    .txn {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
      padding: 9px 0;
      border-bottom: 1px solid #0f172a;
    }}
    .txn:last-child {{ border-bottom: none; }}
    .txn-left {{ min-width: 0; }}
    .txn-name {{
      font-size: 0.85rem; color: #cbd5e1; font-weight: 500;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }}
    .txn-meta {{ font-size: 0.72rem; color: #475569; margin-top: 2px;
                 white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .txn-amt {{ font-size: 0.88rem; font-weight: 700; white-space: nowrap; text-align: right; }}
    .debit  {{ color: #f87171; }}
    .credit {{ color: #4ade80; }}
    .env-badge {{
      display: inline-block;
      background: {'#1e3a5f' if env == 'sandbox' else '#1a2e1a'};
      color: {'#60a5fa' if env == 'sandbox' else '#4ade80'};
      font-size: 0.68rem; font-weight: 700; padding: 2px 7px;
      border-radius: 8px; margin-left: 8px; text-transform: uppercase;
    }}
    .pending {{ font-size: 0.65rem; color: #64748b; margin-left: 4px; }}
    @media (max-width: 480px) {{
      body {{ padding: 12px; }}
      .section {{ padding: 12px; }}
    }}
  </style>
</head>
<body>
  <h1>Nexus <span style="color:#475569;font-weight:400;font-size:0.88rem">/ banking</span>
      <span class="env-badge">{env}</span></h1>
  <p class="sub">Your connected accounts and recent transactions.</p>

  <div class="actions">
    <button class="btn" id="connectBtn" onclick="connectBank()">+ Connect Bank</button>
    <button class="btn btn-secondary" id="refreshBtn" onclick="loadData()">Refresh</button>
    <span id="status"></span>
  </div>

  <div id="accountsSection" style="display:none">
    <div class="section">
      <div class="section-title">Accounts</div>
      <div id="accounts"></div>
      <div id="summaryRows"></div>
    </div>
  </div>

  <div id="txnSection" style="display:none">
    <div class="section">
      <div class="section-title">Recent Transactions <span style="color:#334155;font-weight:400">(30 days)</span></div>
      <div id="transactions"></div>
    </div>
  </div>

  <script>
    const API = '';
    const usd = n => '$' + Math.abs(n).toLocaleString('en-US', {{minimumFractionDigits:2, maximumFractionDigits:2}});

    // Clean raw bank transaction names for display
    function cleanName(raw) {{
      if (!raw) return 'Unknown';
      return raw
        .replace(/^[A-Z]{{2,4}}\\d+\\s+/g, '')      // remove card prefix like XX3698
        .replace(/\\bPOS\\s+PURCHASE\\b/gi, '')       // remove POS PURCHASE
        .replace(/\\bPOS\\s+DEBIT\\b/gi, '')          // remove POS DEBIT
        .replace(/\\bCHECK\\s+CARD\\b/gi, '')
        .replace(/\\b\\d{{2}}\\/\\d{{2}}\\s+\\d{{2}}:\\d{{2}}\\b/g, '') // remove 06/02 08:49
        .replace(/\\s{{2,}}/g, ' ')
        .trim() || raw.trim();
    }}

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
            document.getElementById('status').textContent = err ? err.display_message || '' : '';
            document.getElementById('connectBtn').disabled = false;
          }}
        }});
        handler.open();
      }} catch(e) {{
        document.getElementById('status').textContent = '✗ ' + e.message;
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
        const accounts     = acctR.accounts || [];
        const transactions = txnR.transactions || [];

        if (accounts.length === 0) {{
          document.getElementById('status').textContent = 'No accounts connected yet.';
          return;
        }}

        // ── Group accounts ────────────────────────────────────────────────
        const groups = {{
          depository: accounts.filter(a => a.type === 'depository'),
          credit:     accounts.filter(a => a.type === 'credit'),
          investment: accounts.filter(a => a.type === 'investment'),
          loan:       accounts.filter(a => a.type === 'loan'),
          other:      accounts.filter(a => !['depository','credit','investment','loan'].includes(a.type)),
        }};

        const groupTitles = {{
          depository: 'Cash Accounts',
          credit:     'Credit Cards',
          investment: 'Investments',
          loan:       'Loans',
          other:      'Other',
        }};

        let html = '';
        let cashTotal = 0, creditTotal = 0, investTotal = 0, loanTotal = 0;

        for (const [key, list] of Object.entries(groups)) {{
          if (!list.length) continue;
          html += `<div class="group-label">${{groupTitles[key]}}</div>`;
          list.forEach(a => {{
            const bal = a.current_balance || 0;
            const isDebt = key === 'credit' || key === 'loan';
            const balClass = isDebt ? 'bal-debt' : 'bal-asset';
            const balLabel = isDebt ? usd(bal) : usd(bal);
            if (key === 'depository') cashTotal   += bal;
            if (key === 'credit')     creditTotal += bal;
            if (key === 'investment') investTotal += bal;
            if (key === 'loan')       loanTotal   += bal;
            html += `<div class="account">
              <div class="acct-info">
                <div class="acct-name">${{a.name}}</div>
                <div class="acct-sub">${{a.institution || ''}}${{a.subtype ? ' · ' + a.subtype : ''}}</div>
              </div>
              <div class="acct-bal ${{balClass}}">${{isDebt ? '-' : ''}}${{balLabel}}</div>
            </div>`;
          }});
        }}

        document.getElementById('accounts').innerHTML = html;

        // ── Summary rows ──────────────────────────────────────────────────
        const netWorth = cashTotal + investTotal - creditTotal - loanTotal;
        let sumHtml = '';
        if (cashTotal   > 0) sumHtml += `<div class="summary-row"><span class="summary-label">Cash</span><span class="summary-val blue">${{usd(cashTotal)}}</span></div>`;
        if (creditTotal > 0) sumHtml += `<div class="summary-row"><span class="summary-label">Credit card debt</span><span class="summary-val red">-${{usd(creditTotal)}}</span></div>`;
        if (investTotal > 0) sumHtml += `<div class="summary-row"><span class="summary-label">Investments</span><span class="summary-val blue">${{usd(investTotal)}}</span></div>`;
        if (loanTotal   > 0) sumHtml += `<div class="summary-row"><span class="summary-label">Loans</span><span class="summary-val red">-${{usd(loanTotal)}}</span></div>`;
        if (cashTotal + investTotal + creditTotal + loanTotal > 0) {{
          const nwColor = netWorth >= 0 ? 'green' : 'red';
          const nwSign  = netWorth < 0 ? '-' : '';
          sumHtml += `<div class="summary-row" style="border-top:1px solid #334155;margin-top:6px;padding-top:10px">
            <span class="summary-label" style="font-weight:700;color:#94a3b8">Net worth</span>
            <span class="summary-val ${{nwColor}}">${{nwSign}}${{usd(Math.abs(netWorth))}}</span>
          </div>`;
        }}
        document.getElementById('summaryRows').innerHTML = sumHtml;
        document.getElementById('accountsSection').style.display = 'block';

        // ── Transactions ──────────────────────────────────────────────────
        if (transactions.length > 0) {{
          document.getElementById('transactions').innerHTML = transactions.slice(0,50).map(t => {{
            const amt     = t.amount;
            const isDebit = amt > 0;
            const display = cleanName(t.merchant_name || t.name || '');
            const amtStr  = (isDebit ? '-' : '+') + usd(amt);
            const pending = t.pending ? '<span class="pending">pending</span>' : '';
            return `<div class="txn">
              <div class="txn-left">
                <div class="txn-name">${{display}}${{pending}}</div>
                <div class="txn-meta">${{t.category || 'Other'}} · ${{t.date}}</div>
              </div>
              <div class="txn-amt ${{isDebit ? 'debit' : 'credit'}}">${{amtStr}}</div>
            </div>`;
          }}).join('');
          document.getElementById('txnSection').style.display = 'block';
        }}

        document.getElementById('connectBtn').disabled = false;
        document.getElementById('status').textContent = '';
      }} catch(e) {{
        document.getElementById('status').textContent = '✗ ' + e.message;
      }}
    }}

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
            products=[Products("transactions")],
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


# ── Budget categorization engine ──────────────────────────────────────────────

# Budget categories — aligned with BudgetGoalViewModel defaults in the Android app
BUDGET_CATS = [
    "Food / Drinks",
    "Gas / Car",
    "Shopping / Lifestyle",
    "Apps / Subscriptions",
    "Entertainment",
    "Bills / Utilities",
    "Gas / Car",
    "Health",
    "School",
    "Income",
    "Transfers / Unclear",
    "Credit Card Payments",
    "Other",
]

# Map Plaid personal_finance_category → budget category
_PFC_MAP: dict[str, str] = {
    "FOOD_AND_DRINK_RESTAURANTS":                 "Food / Drinks",
    "FOOD_AND_DRINK_FAST_FOOD":                   "Food / Drinks",
    "FOOD_AND_DRINK_COFFEE":                      "Food / Drinks",
    "FOOD_AND_DRINK_GROCERIES":                   "Food / Drinks",
    "FOOD_AND_DRINK_ALCOHOL_AND_BARS":            "Food / Drinks",
    "FOOD_AND_DRINK_VENDING_MACHINES":            "Food / Drinks",
    "FOOD_AND_DRINK_OTHER_FOOD_AND_DRINK":        "Food / Drinks",
    "TRANSPORTATION_GAS":                         "Gas / Car",
    "TRANSPORTATION_PARKING":                     "Gas / Car",
    "TRANSPORTATION_TAXIS_AND_RIDESHARES":        "Gas / Car",
    "TRANSPORTATION_PUBLIC_TRANSIT":              "Gas / Car",
    "TRANSPORTATION_OTHER_TRANSPORTATION":        "Gas / Car",
    "TRAVEL_FLIGHTS":                             "Gas / Car",
    "TRAVEL_LODGING":                             "Entertainment",
    "SHOPPING_GENERAL_MERCHANDISE":              "Shopping / Lifestyle",
    "SHOPPING_CLOTHING_AND_ACCESSORIES":          "Shopping / Lifestyle",
    "SHOPPING_SPORTING_GOODS":                   "Shopping / Lifestyle",
    "SHOPPING_HOME_IMPROVEMENT":                 "Shopping / Lifestyle",
    "SHOPPING_ELECTRONICS":                      "Tech / Best Buy",
    "SHOPPING_GROCERIES":                        "Food / Drinks",
    "SHOPPING_OTHER_SHOPPING":                   "Shopping / Lifestyle",
    "ENTERTAINMENT_CASINOS_AND_GAMBLING":        "Entertainment",
    "ENTERTAINMENT_MUSIC_AND_AUDIO":             "Apps / Subscriptions",
    "ENTERTAINMENT_STREAMING_SERVICES":          "Apps / Subscriptions",
    "ENTERTAINMENT_SPORTING_EVENTS":             "Entertainment",
    "ENTERTAINMENT_TV_AND_MOVIES":               "Apps / Subscriptions",
    "ENTERTAINMENT_VIDEO_GAMES":                 "Entertainment",
    "ENTERTAINMENT_OTHER_ENTERTAINMENT":         "Entertainment",
    "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS":    "Health",
    "PERSONAL_CARE_HAIR_AND_BEAUTY":             "Shopping / Lifestyle",
    "PERSONAL_CARE_OTHER_PERSONAL_CARE":         "Health",
    "MEDICAL_DOCTOR_VISITS":                     "Health",
    "MEDICAL_PHARMACIES_AND_SUPPLEMENTS":        "Health",
    "MEDICAL_DENTAL_CARE":                       "Health",
    "MEDICAL_EYE_CARE":                          "Health",
    "MEDICAL_OTHER_MEDICAL":                     "Health",
    "HOME_IMPROVEMENT_FURNITURES":               "Shopping / Lifestyle",
    "HOME_IMPROVEMENT_HARDWARE":                 "Shopping / Lifestyle",
    "GENERAL_SERVICES_AUTOMOTIVE":               "Gas / Car",
    "GENERAL_SERVICES_SUBSCRIPTION":             "Apps / Subscriptions",
    "GENERAL_SERVICES_INSURANCE":                "Bills / Utilities",
    "GENERAL_SERVICES_EDUCATION":                "School",
    "RENT_AND_UTILITIES_RENT":                   "Bills / Utilities",
    "RENT_AND_UTILITIES_UTILITIES":              "Bills / Utilities",
    "RENT_AND_UTILITIES_INTERNET_AND_CABLE":     "Bills / Utilities",
    "RENT_AND_UTILITIES_TELEPHONE":              "Bills / Utilities",
    "RENT_AND_UTILITIES_GAS_AND_ELECTRICITY":    "Bills / Utilities",
    "INCOME_PAYROLL":                            "Income",
    "INCOME_DIVIDENDS":                          "Income",
    "INCOME_OTHER_INCOME":                       "Income",
    "TRANSFER_IN_ACCOUNT_TRANSFER":              "Transfers / Unclear",
    "TRANSFER_OUT_ACCOUNT_TRANSFER":             "Transfers / Unclear",
    "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT":         "Credit Card Payments",
    "LOAN_PAYMENTS_MORTGAGE_PAYMENT":            "Bills / Utilities",
    "LOAN_PAYMENTS_CAR_PAYMENT":                 "Gas / Car",
    "LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT":        "School",
}

# Map Plaid legacy category[0] → budget category
_LEGACY_CAT_MAP: dict[str, str] = {
    "food and drink":        "Food / Drinks",
    "restaurants":           "Food / Drinks",
    "fast food":             "Food / Drinks",
    "coffee shop":           "Food / Drinks",
    "groceries":             "Food / Drinks",
    "travel":                "Gas / Car",
    "gas stations":          "Gas / Car",
    "taxi":                  "Gas / Car",
    "ride share":            "Gas / Car",
    "shops":                 "Shopping / Lifestyle",
    "supermarkets":          "Food / Drinks",
    "clothing":              "Shopping / Lifestyle",
    "electronics":           "Shopping / Lifestyle",
    "sporting goods":        "Shopping / Lifestyle",
    "service":               "Bills / Utilities",
    "internet services":     "Apps / Subscriptions",
    "subscription":          "Apps / Subscriptions",
    "gyms and fitness":      "Health",
    "healthcare":            "Health",
    "medical":               "Health",
    "insurance":             "Bills / Utilities",
    "utilities":             "Bills / Utilities",
    "telecom":               "Bills / Utilities",
    "entertainment":         "Entertainment",
    "music":                 "Apps / Subscriptions",
    "video streaming":       "Apps / Subscriptions",
    "video games":           "Entertainment",
    "education":             "School",
    "payroll":               "Income",
    "deposit":               "Income",
    "transfer":              "Transfers / Unclear",
    "payment":               "Credit Card Payments",
    "credit card":           "Credit Card Payments",
    "automotive":            "Gas / Car",
}

# Merchant keyword → budget category (applied to lowercased name/merchant)
_KEYWORD_RULES: list[tuple[list[str], str]] = [
    (["mcdonald", "taco bell", "subway", "chipotle", "wendy", "burger king",
      "doordash", "grubhub", "ubereats", "domino", "pizza", "sushi",
      "restaurant", "cafe", "diner", "kitchen", "grill", "steakhouse",
      "qdoba", "panda express", "arby", "popeyes", "chick-fil"],   "Food / Drinks"),
    (["hy-vee", "hy vee", "hyvee", "walmart grocery", "kroger", "safeway",
      "whole foods", "trader joe", "aldi", "sam's club", "costco"],  "Food / Drinks"),
    (["starbucks", "dunkin", "caribou", "dutch bros"],               "Food / Drinks"),
    (["shell", "exxon", "bp gas", "casey's", "cenex", "holiday gas",
      "kwik trip", "kwiktrip", "kum & go", "speedway", "marathon",
      "chevron", "pilot", "loves travel"],                           "Gas / Car"),
    (["uber", "lyft", "taxi"],                                        "Gas / Car"),
    (["best buy", "newegg", "micro center", "b&h photo",
      "apple store", "samsung"],                                      "Tech / Best Buy"),
    (["amazon", "amazon.com", "amazon prime"],                       "Shopping / Lifestyle"),
    (["walmart", "target", "home depot", "lowe's",
      "costco", "sam's", "tj maxx", "ross", "marshalls"],           "Shopping / Lifestyle"),
    (["spotify", "apple music", "youtube premium", "pandora",
      "netflix", "hulu", "disney+", "paramount", "peacock",
      "apple.com", "google play", "discord", "adobe",
      "microsoft 365", "dropbox", "icloud"],                        "Apps / Subscriptions"),
    (["geico", "state farm", "progressive", "allstate",
      "usaa insurance", "nationwide"],                               "Bills / Utilities"),
    (["xfinity", "comcast", "at&t", "verizon", "t-mobile",
      "dish network", "centurylink", "spectrum", "directv",
      "midco", "midcontinent"],                                      "Bills / Utilities"),
    (["electric", "water bill", "gas bill", "xcel energy",
      "otter tail", "excel energy"],                                 "Bills / Utilities"),
    (["jiffy lube", "valvoline", "o'reilly", "autozone",
      "napa auto", "firestone", "midas", "car wash",
      "auto parts", "meineke"],                                      "Gas / Car"),
    (["cvs", "walgreens", "rite aid", "pharmacy", "clinic",
      "hospital", "dental", "optometrist", "urgent care",
      "doctor", "medical"],                                          "Health"),
    (["planet fitness", "anytime fitness", "ymca", "gym"],           "Health"),
    (["university", "college", "tuition", "student loan",
      "navient", "great lakes", "edfinancial"],                     "School"),
]

# ── Income / paycheck detection ───────────────────────────────────────────────

# Keyword signals that strongly suggest income regardless of merchant name
_INCOME_KEYWORDS = {
    "payroll", "direct deposit", "paycheck", "salary", "wages",
    "employer deposit", "ach credit", "dd", "dir dep",
}

# Keyword signals that suggest a POS/debit purchase (spending)
_PURCHASE_SIGNALS = {
    "pos purchase", "pos debit", "check card", "debit card",
    "card purchase", "card transaction", "purchase", "debit purchase",
}


def _is_income_transaction(txn: dict) -> tuple[bool, str]:
    """
    Determine if a transaction is income (paycheck, deposit, etc.).

    Returns (is_income, income_type) where income_type is one of:
        "paycheck"     – payroll / direct deposit
        "deposit"      – other cash deposit / bank credit
        "refund"       – merchant refund (credit back)
        ""             – not income

    Decision priority:
      1. Pending → never income (not settled)
      2. Plaid personal_finance_category INCOME/PAYROLL → paycheck
      3. Plaid legacy category "payroll" → paycheck
      4. Name contains strong income keywords → paycheck
      5. Best Buy + depository credit context → paycheck
      6. Negative amount on depository account with "deposit" in name → deposit
      7. Refund signals → refund (excluded from spending but not tracked as income)
    """
    if txn.get("pending"):
        return False, ""

    pfc      = (txn.get("personal_finance_category") or "").upper()
    cat      = (txn.get("category") or "").lower()
    name     = (txn.get("name") or "").lower()
    amount   = float(txn.get("amount", 0))
    acct_type = (txn.get("account_type") or "").lower()  # populated in _fetch_budget_transactions

    # In Plaid's sign convention: negative amount = money arriving (credit to account)
    is_credit_to_account = amount < 0

    # ── 1. Plaid PFC: most authoritative ─────────────────────────────────────
    if any(k in pfc for k in ("INCOME_PAYROLL", "INCOME_WAGES")):
        return True, "paycheck"
    if any(k in pfc for k in ("INCOME_DIVIDENDS", "INCOME_OTHER_INCOME", "INCOME_")):
        return True, "deposit"

    # ── 2. Plaid legacy category ──────────────────────────────────────────────
    if "payroll" in cat:
        return True, "paycheck"
    if cat in ("deposit",) and is_credit_to_account:
        return True, "deposit"

    # ── 3. Strong income keyword in name (runs before merchant keywords) ──────
    for kw in _INCOME_KEYWORDS:
        if kw in name:
            return True, "paycheck"

    # ── 4. Best Buy payroll vs purchase disambiguation ────────────────────────
    # This must run BEFORE the generic keyword table that would tag BB as spending.
    if "best buy" in name:
        # Check for purchase signals first — if present, it is a purchase
        if any(sig in name for sig in _PURCHASE_SIGNALS):
            return False, ""
        # Depository credit (negative amount) = incoming deposit → paycheck
        if is_credit_to_account and acct_type in ("depository", "checking", "savings", "cash", ""):
            return True, "paycheck"
        # Plaid already said it's income (belt-and-suspenders)
        if any(k in pfc for k in ("INCOME", "PAYROLL")):
            return True, "paycheck"
        # Positive amount on depository that contains payroll signals → purchase
        # (falls through to normal expense path)

    # ── 5. Generic depository credits that look like deposits ─────────────────
    if is_credit_to_account and acct_type in ("depository", "checking", "savings", "cash", ""):
        if any(k in name for k in ("deposit", "credit", "refund", "return")):
            # Distinguish refunds from true deposits
            if any(k in name for k in ("refund", "return", "adjustment", "reversal")):
                return True, "refund"
            return True, "deposit"

    return False, ""


def _classify_transaction(txn: dict) -> dict:
    """
    Full classification pass for a single transaction.

    Returns the txn dict augmented with:
        is_income          bool
        income_type        str  ("paycheck" | "deposit" | "refund" | "")
        excluded_from_budget  bool
        exclusion_reason   str
        budget_category    str  (empty when excluded)
    """
    # ── Step 1: income detection (runs before everything else) ────────────────
    is_income, income_type = _is_income_transaction(txn)

    if is_income:
        txn["is_income"]            = True
        txn["income_type"]          = income_type
        txn["excluded_from_budget"] = True
        txn["exclusion_reason"]     = "income"
        txn["budget_category"]      = ""
        return txn

    # ── Step 2: pending ───────────────────────────────────────────────────────
    if txn.get("pending"):
        txn["is_income"]            = False
        txn["income_type"]          = ""
        txn["excluded_from_budget"] = True
        txn["exclusion_reason"]     = "pending"
        txn["budget_category"]      = ""
        return txn

    pfc  = (txn.get("personal_finance_category") or "").upper()
    cat  = (txn.get("category") or "").lower()
    name = (txn.get("name") or "").lower()

    # ── Step 3: transfers ────────────────────────────────────────────────────
    if any(k in pfc for k in ("TRANSFER_IN", "TRANSFER_OUT", "ACCOUNT_TRANSFER")):
        txn.update(is_income=False, income_type="", excluded_from_budget=True,
                   exclusion_reason="transfer", budget_category="")
        return txn
    if "transfer" in cat:
        txn.update(is_income=False, income_type="", excluded_from_budget=True,
                   exclusion_reason="transfer", budget_category="")
        return txn
    # Zelle/Venmo/CashApp are transfers unless they match income keywords (already handled above)
    if any(k in name for k in ("zelle", "venmo", "cash app", "cashapp")):
        txn.update(is_income=False, income_type="", excluded_from_budget=True,
                   exclusion_reason="transfer", budget_category="")
        return txn

    # ── Step 4: credit card payments ─────────────────────────────────────────
    if "CREDIT_CARD_PAYMENT" in pfc or "LOAN_PAYMENTS_CREDIT_CARD" in pfc:
        txn.update(is_income=False, income_type="", excluded_from_budget=True,
                   exclusion_reason="credit_card_payment", budget_category="")
        return txn
    if "payment" in cat and "credit" in name:
        txn.update(is_income=False, income_type="", excluded_from_budget=True,
                   exclusion_reason="credit_card_payment", budget_category="")
        return txn

    # ── Step 5: negative amounts are credits/income/refunds already handled ──
    # If a negative amount slipped through (not already caught as income), exclude it
    # rather than subtracting from spending totals.
    if float(txn.get("amount", 0)) < 0:
        txn.update(is_income=False, income_type="", excluded_from_budget=True,
                   exclusion_reason="credit_to_account", budget_category="")
        return txn

    # ── Step 6: assign spending category ────────────────────────────────────
    txn["is_income"]            = False
    txn["income_type"]          = ""
    txn["excluded_from_budget"] = False
    txn["exclusion_reason"]     = ""
    txn["budget_category"]      = _assign_category(txn)
    return txn


def _exclude_from_budget(txn: dict) -> tuple[bool, str]:
    """Legacy shim — delegates to _classify_transaction.
    Callers that only need (excluded, reason) can keep using this."""
    result = _classify_transaction(dict(txn))  # operate on a copy
    return result["excluded_from_budget"], result["exclusion_reason"]


def _assign_category(txn: dict) -> str:
    """Assign a spending budget category.
    NOTE: income detection must have already run (via _classify_transaction).
          This function only categorises genuine expense transactions.
    """
    pfc   = (txn.get("personal_finance_category") or "").upper().replace(" ", "_")
    cat   = (txn.get("category") or "").lower()
    name  = ((txn.get("merchant_name") or txn.get("name") or "")).lower()

    # 0. High-priority merchant overrides — run BEFORE legacy category map
    #    so that e.g. "Best Buy" on a "shops" legacy category goes to Tech/Best Buy
    #    rather than Shopping/Lifestyle.
    if "best buy" in name:
        return "Tech / Best Buy"

    # 1. Plaid personal_finance_category (most specific)
    for key, budget_cat in _PFC_MAP.items():
        if key in pfc:
            return budget_cat

    # 2. Plaid legacy category
    for key, budget_cat in _LEGACY_CAT_MAP.items():
        if key in cat:
            return budget_cat

    # 3. Merchant keyword rules
    for keywords, budget_cat in _KEYWORD_RULES:
        if any(kw in name for kw in keywords):
            return budget_cat

    return "Other"


def _clean_display_name(name: str | None, merchant: str | None) -> str:
    """Return a clean display name — prefer merchant_name, clean raw bank strings."""
    if merchant:
        return merchant.strip()
    if not name:
        return "Unknown"
    import re
    cleaned = re.sub(r'^[A-Z]{2,4}\d+\s+', '', name)   # XX3698 prefix
    cleaned = re.sub(r'\bPOS\s+PURCHASE\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\bPOS\s+DEBIT\b',    '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\bCHECK\s+CARD\b',   '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b\d{2}/\d{2}\s+\d{2}:\d{2}\b', '', cleaned)
    return re.sub(r'\s{2,}', ' ', cleaned).strip() or name.strip()


async def _fetch_budget_transactions(month: str | None = None) -> list[dict]:
    """Fetch and classify transactions for a given month (YYYY-MM) or current month.

    Each transaction dict includes:
        is_income, income_type, excluded_from_budget, exclusion_reason, budget_category
    """
    import datetime as dt
    from plaid.model.transactions_get_request import TransactionsGetRequest
    from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
    from plaid.model.accounts_get_request import AccountsGetRequest

    today = dt.date.today()
    if month:
        year, mon = int(month[:4]), int(month[5:7])
        start = dt.date(year, mon, 1)
        if mon == 12:
            end = dt.date(year + 1, 1, 1) - dt.timedelta(days=1)
        else:
            end = dt.date(year, mon + 1, 1) - dt.timedelta(days=1)
        end = min(end, today)
    else:
        start = today.replace(day=1)
        end   = today

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT access_token FROM plaid_items")
    if not rows:
        return []

    client = _plaid_client()
    all_txns: list[dict] = []

    for row in rows:
        try:
            # Build account_id → account_type map so income detection has context
            acct_type_map: dict[str, str] = {}
            try:
                acct_resp = client.accounts_get(AccountsGetRequest(access_token=row["access_token"]))
                for a in acct_resp["accounts"]:
                    acct_type_map[a["account_id"]] = str(a.get("type", "")).lower()
            except Exception:
                pass

            resp = client.transactions_get(
                TransactionsGetRequest(
                    access_token=row["access_token"],
                    start_date=start,
                    end_date=end,
                    options=TransactionsGetRequestOptions(count=500),
                )
            )
            for t in resp["transactions"]:
                pfc = ""
                try:
                    pfc_obj = t.get("personal_finance_category")
                    if pfc_obj:
                        pfc = getattr(pfc_obj, "primary", "") or str(pfc_obj)
                except Exception:
                    pass

                acct_id   = t["account_id"]
                acct_type = acct_type_map.get(acct_id, "")

                txn = {
                    "transaction_id":            t["transaction_id"],
                    "date":                      str(t["date"]),
                    "display_name":              _clean_display_name(t.get("name"), t.get("merchant_name")),
                    "raw_name":                  t.get("name", ""),
                    "amount":                    float(t["amount"]),
                    # account_id NOT returned to clients — used only for classification
                    "account_type":              acct_type,
                    "plaid_category":            t["category"][0] if t.get("category") else "",
                    "personal_finance_category": pfc,
                    "pending":                   bool(t["pending"]),
                    # Classification defaults (overwritten by _classify_transaction)
                    "is_income":                 False,
                    "income_type":               "",
                    "excluded_from_budget":      False,
                    "exclusion_reason":          "",
                    "budget_category":           "",
                }
                _classify_transaction(txn)   # mutates txn in-place
                all_txns.append(txn)
        except Exception:
            continue

    all_txns.sort(key=lambda x: x["date"], reverse=True)

    # Strip internal fields before returning to callers
    for t in all_txns:
        t.pop("account_type", None)   # never expose raw account metadata

    return all_txns


@app.get("/budget/transactions")
async def budget_transactions(month: str = ""):
    """Categorized transactions for a month. month=YYYY-MM, default=current month."""
    txns = await _fetch_budget_transactions(month or None)
    return {"transactions": txns, "count": len(txns), "month": month or "current"}


def _build_income_summary(txns: list[dict]) -> dict:
    """Extract income/paycheck summary from a classified transaction list.
    Returns only safe display fields — no raw account IDs or Plaid tokens.
    """
    income_txns = [t for t in txns if t.get("is_income") and t["amount"] < 0]
    # Plaid income = negative amount (money arriving). Display as positive.
    paychecks = [
        {
            "date":         t["date"],
            "display_name": t["display_name"],
            "amount":       round(abs(t["amount"]), 2),
            "income_type":  t.get("income_type", "deposit"),
        }
        for t in income_txns
        if t.get("income_type") == "paycheck"
    ]
    other_income = [
        {
            "date":         t["date"],
            "display_name": t["display_name"],
            "amount":       round(abs(t["amount"]), 2),
            "income_type":  t.get("income_type", "deposit"),
        }
        for t in income_txns
        if t.get("income_type") != "paycheck"
    ]

    monthly_income = round(sum(abs(t["amount"]) for t in income_txns), 2)
    return {
        "monthly_income": monthly_income,
        "paycheck_count": len(paychecks),
        "paychecks":      paychecks,
        "other_income":   other_income,
    }


@app.get("/budget/summary")
async def budget_summary(month: str = ""):
    """Budget summary with spending by category and income for a month."""
    txns = await _fetch_budget_transactions(month or None)
    import datetime as dt
    today = dt.date.today()
    display_month = month or today.strftime("%Y-%m")

    spending      = [t for t in txns if not t["excluded_from_budget"] and t["amount"] > 0]
    excluded      = [t for t in txns if t["excluded_from_budget"]]
    uncategorized = [t for t in spending if t["budget_category"] == "Other"]

    # Spending by category
    by_cat: dict[str, float] = {}
    for t in spending:
        cat = t["budget_category"]
        by_cat[cat] = round(by_cat.get(cat, 0.0) + t["amount"], 2)

    total_spending = round(sum(by_cat.values()), 2)
    income_data    = _build_income_summary(txns)
    monthly_income = income_data["monthly_income"]
    net_cash_flow  = round(monthly_income - total_spending, 2)
    savings_rate   = round(net_cash_flow / monthly_income * 100, 1) if monthly_income > 0 else None

    return {
        "month":                 display_month,
        "total_spending":        total_spending,
        "monthly_income":        monthly_income,
        "paycheck_count":        income_data["paycheck_count"],
        "paychecks":             income_data["paychecks"],
        "other_income":          income_data["other_income"],
        "net_cash_flow":         net_cash_flow,
        "savings_rate":          savings_rate,
        "spending_by_category": [
            {"category": cat, "amount": amt}
            for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1])
        ],
        "uncategorized_count":   len(uncategorized),
        "excluded_count":        len(excluded),
        "transaction_count":     len(txns),
        "spending_transactions": spending[:50],
    }


# ── Portfolio (manual — Fidelity/Schwab fallback) ─────────────────────────────

from typing import Optional, List

class PortfolioAccountIn(BaseModel):
    account_name: str
    institution:  str = "Fidelity"
    account_type: str = "brokerage"   # brokerage | roth_ira | traditional_ira | 401k | other
    cash_balance: float = 0.0

class HoldingIn(BaseModel):
    symbol:         str
    description:    Optional[str] = None
    quantity:       float
    average_cost:   Optional[float] = None
    current_price:  Optional[float] = None
    current_value:  float
    cost_basis:     Optional[float] = None
    unrealized_pnl: Optional[float] = None

class HoldingUpdate(BaseModel):
    quantity:       Optional[float] = None
    average_cost:   Optional[float] = None
    current_price:  Optional[float] = None
    current_value:  Optional[float] = None
    cost_basis:     Optional[float] = None
    unrealized_pnl: Optional[float] = None


@app.get("/portfolio/manual")
async def get_portfolio():
    """Return all manual portfolio accounts with holdings and summary totals."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        accounts = await conn.fetch(
            "SELECT id, account_name, institution, account_type, cash_balance, updated_at "
            "FROM portfolio_accounts ORDER BY id"
        )
        all_holdings = await conn.fetch(
            "SELECT id, account_id, symbol, description, quantity, average_cost, "
            "current_price, current_value, cost_basis, unrealized_pnl, updated_at "
            "FROM portfolio_holdings ORDER BY account_id, symbol"
        )

    holdings_by_account: dict = {}
    for h in all_holdings:
        holdings_by_account.setdefault(h["account_id"], []).append(dict(h))

    result = []
    total_cash = total_holdings = total_cost = total_pnl = 0.0
    has_cost = has_pnl = False

    for a in accounts:
        aid  = a["id"]
        hdgs = holdings_by_account.get(aid, [])
        acct_holdings_val = sum(h["current_value"] or 0 for h in hdgs)
        acct_cost         = sum(h["cost_basis"] or 0 for h in hdgs if h["cost_basis"])
        acct_pnl          = sum(h["unrealized_pnl"] or 0 for h in hdgs if h["unrealized_pnl"])
        has_cost = has_cost or any(h["cost_basis"] for h in hdgs)
        has_pnl  = has_pnl  or any(h["unrealized_pnl"] for h in hdgs)
        total_cash     += float(a["cash_balance"] or 0)
        total_holdings += acct_holdings_val
        total_cost     += acct_cost
        total_pnl      += acct_pnl
        result.append({
            "id":            aid,
            "account_name":  a["account_name"],
            "institution":   a["institution"],
            "account_type":  a["account_type"],
            "cash_balance":  float(a["cash_balance"] or 0),
            "holdings_value":round(acct_holdings_val, 2),
            "total_value":   round(float(a["cash_balance"] or 0) + acct_holdings_val, 2),
            "cost_basis":    round(acct_cost, 2) if has_cost else None,
            "unrealized_pnl":round(acct_pnl, 2) if has_pnl else None,
            "holdings":      hdgs,
            "updated_at":    str(a["updated_at"]),
        })

    total_value = total_cash + total_holdings
    return {
        "accounts":            result,
        "total_cash":          round(total_cash, 2),
        "total_holdings_value":round(total_holdings, 2),
        "total_portfolio_value":round(total_value, 2),
        "total_cost_basis":    round(total_cost, 2) if has_cost else None,
        "total_unrealized_pnl":round(total_pnl, 2) if has_pnl else None,
        "account_count":       len(result),
    }


@app.post("/portfolio/manual/account", status_code=201)
async def create_portfolio_account(body: PortfolioAccountIn):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO portfolio_accounts (account_name, institution, account_type, cash_balance) "
            "VALUES ($1,$2,$3,$4) RETURNING id, account_name, institution, account_type, cash_balance",
            body.account_name, body.institution, body.account_type, body.cash_balance
        )
    import logging; logging.getLogger(__name__).info("Portfolio account created: %s at %s", body.account_name, body.institution)
    return dict(row)


@app.put("/portfolio/manual/account/{account_id}")
async def update_portfolio_account(account_id: int, body: PortfolioAccountIn):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE portfolio_accounts SET account_name=$1, institution=$2, account_type=$3, "
            "cash_balance=$4, updated_at=NOW() WHERE id=$5 "
            "RETURNING id, account_name, institution, account_type, cash_balance",
            body.account_name, body.institution, body.account_type, body.cash_balance, account_id
        )
    if not row:
        raise HTTPException(status_code=404, detail="Account not found")
    return dict(row)


@app.delete("/portfolio/manual/account/{account_id}")
async def delete_portfolio_account(account_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM portfolio_accounts WHERE id=$1", account_id)
    return {"ok": True}


@app.post("/portfolio/manual/holding", status_code=201)
async def create_holding(account_id: int, body: HoldingIn):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify account exists
        exists = await conn.fetchval("SELECT id FROM portfolio_accounts WHERE id=$1", account_id)
        if not exists:
            raise HTTPException(status_code=404, detail="Account not found")
        row = await conn.fetchrow(
            "INSERT INTO portfolio_holdings "
            "(account_id, symbol, description, quantity, average_cost, current_price, "
            " current_value, cost_basis, unrealized_pnl) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id, symbol, current_value",
            account_id, body.symbol.upper(), body.description, body.quantity,
            body.average_cost, body.current_price, body.current_value,
            body.cost_basis, body.unrealized_pnl
        )
    return dict(row)


@app.put("/portfolio/manual/holding/{holding_id}")
async def update_holding(holding_id: int, body: HoldingUpdate):
    pool = await get_pool()
    fields, vals = [], []
    if body.quantity        is not None: fields.append("quantity=$%d"        % (len(fields)+1,)); vals.append(body.quantity)
    if body.average_cost    is not None: fields.append("average_cost=$%d"    % (len(fields)+1,)); vals.append(body.average_cost)
    if body.current_price   is not None: fields.append("current_price=$%d"   % (len(fields)+1,)); vals.append(body.current_price)
    if body.current_value   is not None: fields.append("current_value=$%d"   % (len(fields)+1,)); vals.append(body.current_value)
    if body.cost_basis      is not None: fields.append("cost_basis=$%d"      % (len(fields)+1,)); vals.append(body.cost_basis)
    if body.unrealized_pnl  is not None: fields.append("unrealized_pnl=$%d"  % (len(fields)+1,)); vals.append(body.unrealized_pnl)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    fields.append(f"updated_at=NOW()")
    vals.append(holding_id)
    async with (await get_pool()).acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE portfolio_holdings SET {', '.join(fields)} WHERE id=${len(vals)} "
            "RETURNING id, symbol, current_value, updated_at",
            *vals
        )
    if not row:
        raise HTTPException(status_code=404, detail="Holding not found")
    return dict(row)


@app.delete("/portfolio/manual/holding/{holding_id}")
async def delete_holding(holding_id: int):
    async with (await get_pool()).acquire() as conn:
        await conn.execute("DELETE FROM portfolio_holdings WHERE id=$1", holding_id)
    return {"ok": True}


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
    # Use the full classification engine (same as /budget/summary) so monthly_spending,
    # monthly_income, and spending_by_category are always consistent.
    transactions = []
    monthly_spending = 0.0
    monthly_income   = 0.0
    spending_by_cat: dict[str, float] = {}
    income_txns_for_summary: list[dict] = []
    today      = dt.date.today()
    month_start = today.replace(day=1)

    try:
        from plaid.model.transactions_get_request import TransactionsGetRequest
        from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
        from plaid.model.accounts_get_request import AccountsGetRequest as _AGetRequest
        for row in (rows or []):
            try:
                # account_type map for income classification (depository vs credit)
                acct_type_map: dict[str, str] = {}
                try:
                    ar = client.accounts_get(_AGetRequest(access_token=row["access_token"]))
                    for a in ar["accounts"]:
                        acct_type_map[a["account_id"]] = str(a.get("type", "")).lower()
                except Exception:
                    pass

                resp = client.transactions_get(
                    TransactionsGetRequest(
                        access_token=row["access_token"],
                        start_date=month_start,
                        end_date=today,
                        options=TransactionsGetRequestOptions(count=250),
                    )
                )
                for t in resp["transactions"]:
                    pfc = ""
                    try:
                        pfc_obj = t.get("personal_finance_category")
                        if pfc_obj:
                            pfc = getattr(pfc_obj, "primary", "") or str(pfc_obj)
                    except Exception:
                        pass

                    acct_id   = t["account_id"]
                    acct_type = acct_type_map.get(acct_id, "")
                    cat_raw   = t["category"][0] if t.get("category") else "Other"
                    amt       = float(t["amount"])

                    txn = {
                        "transaction_id":            t["transaction_id"],
                        "date":                      str(t["date"]),
                        "name":                      t["name"],
                        "display_name":              _clean_display_name(t.get("name"), t.get("merchant_name")),
                        "amount":                    amt,
                        "category":                  cat_raw,
                        "account_type":              acct_type,
                        "personal_finance_category": pfc,
                        "pending":                   bool(t["pending"]),
                        # will be filled by _classify_transaction
                        "is_income": False, "income_type": "",
                        "excluded_from_budget": False, "exclusion_reason": "", "budget_category": "",
                    }
                    _classify_transaction(txn)

                    # Strip internal fields before storing for the response
                    safe_txn = {k: v for k, v in txn.items()
                                if k not in ("account_type",)}
                    transactions.append(safe_txn)

                    if not txn["pending"]:
                        if txn["is_income"] and amt < 0:
                            monthly_income += abs(amt)
                            income_txns_for_summary.append(txn)
                        elif not txn["excluded_from_budget"] and amt > 0:
                            monthly_spending += amt
                            cat = txn["budget_category"] or "Other"
                            spending_by_cat[cat] = round(spending_by_cat.get(cat, 0.0) + amt, 2)
            except Exception:
                continue
    except Exception:
        pass

    transactions.sort(key=lambda x: x["date"], reverse=True)

    # Build safe income summary
    paychecks = [
        {"date": t["date"], "display_name": t["display_name"],
         "amount": round(abs(t["amount"]), 2), "income_type": t.get("income_type", "paycheck")}
        for t in income_txns_for_summary if t.get("income_type") == "paycheck"
    ]
    net_cash_flow = round(monthly_income - monthly_spending, 2)
    savings_rate  = round(net_cash_flow / monthly_income * 100, 1) if monthly_income > 0 else None

    # Manual portfolio (Fidelity/Schwab fallback)
    manual_portfolio_value = 0.0
    manual_portfolio_accounts: list = []
    try:
        pool2 = await get_pool()
        async with pool2.acquire() as conn:
            port_accts = await conn.fetch(
                "SELECT id, account_name, institution, account_type, cash_balance FROM portfolio_accounts"
            )
            port_hdgs = await conn.fetch(
                "SELECT account_id, SUM(current_value) AS total FROM portfolio_holdings GROUP BY account_id"
            )
        hdg_by_acct = {r["account_id"]: float(r["total"] or 0) for r in port_hdgs}
        for a in port_accts:
            val = float(a["cash_balance"] or 0) + hdg_by_acct.get(a["id"], 0)
            manual_portfolio_value += val
            manual_portfolio_accounts.append({
                "id": a["id"], "account_name": a["account_name"],
                "institution": a["institution"], "account_type": a["account_type"],
                "total_value": round(val, 2),
            })
    except Exception:
        pass

    # Sandbox flag so the Android app can show an appropriate disclaimer
    plaid_env = os.getenv("PLAID_ENV", "sandbox")
    has_cash_or_investments = total_cash > 0 or total_investments > 0 or manual_portfolio_value > 0

    return {
        "total_cash":                round(total_cash, 2),
        "total_credit_card_debt":    round(total_credit_debt, 2),
        "total_investments":         round(total_investments + manual_portfolio_value, 2),
        "total_investments_plaid":   round(total_investments, 2),
        "total_portfolio_manual":    round(manual_portfolio_value, 2),
        "total_loans":               round(total_loans, 2),
        "net_worth":                 round(net_worth + manual_portfolio_value, 2),
        "monthly_spending":          round(monthly_spending, 2),
        "monthly_income":            round(monthly_income, 2),
        "paycheck_count":            len(paychecks),
        "paychecks":                 paychecks,
        "net_cash_flow":             net_cash_flow,
        "savings_rate":              savings_rate,
        "spending_by_category": [
            {"category": cat, "amount": amt}
            for cat, amt in sorted(spending_by_cat.items(), key=lambda x: -x[1])
        ],
        "accounts":                  accounts,
        "credit_cards":              credit_cards,
        "manual_portfolio_accounts": manual_portfolio_accounts,
        "recent_transactions":       transactions[:25],
        "has_connected_accounts":    len(accounts) > 0,
        "has_cash_or_investments":   has_cash_or_investments,
        "plaid_env":                 plaid_env,
        "last_synced":               dt.datetime.utcnow().isoformat() + "Z",
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
