# Nexus API

> Personal banking and investment data backend — part of the Nexus ecosystem.

[![Live](https://img.shields.io/badge/deployed-Railway-6366f1?style=flat-square)](https://dashboard-api-production-ebee.up.railway.app)
[![Python](https://img.shields.io/badge/python-3.11+-3b82f6?style=flat-square)](https://python.org)

---

## Overview

Nexus API is the backend service that connects the Nexus Android app to real financial data via **Plaid**. It handles bank account linking, balance retrieval, transaction history, and investment holdings — serving as the data layer between Plaid's financial network and the Nexus dashboard.

---

## Features

- **Plaid Link token generation** — creates secure link tokens for the Android app's bank connection flow
- **Token exchange** — converts Plaid's public tokens to persistent access tokens, stored in Supabase
- **Account balances** — live checking, savings, and credit card balances across all linked institutions
- **Transaction history** — recent transactions from all connected accounts
- **Investment holdings** — Fidelity brokerage positions and portfolio data
- **Web dashboard** — browser-accessible banking view at the live URL
- **Lazy DB connection** — connects to Supabase on first request, graceful startup without DB

---

## Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Web banking dashboard (HTML) |
| `POST` | `/create_link_token` | Generate a Plaid Link token for account connection |
| `POST` | `/exchange_token` | Exchange public token → access token (stored in Supabase) |
| `GET` | `/accounts` | All linked account balances |
| `GET` | `/transactions` | Recent transaction history |
| `GET` | `/investments` | Investment holdings and portfolio positions |

---

## Architecture

```
main.py
├── Plaid client setup (sandbox / production via PLAID_ENV)
├── Supabase connection pool (asyncpg)
├── plaid_items table — stores access tokens per institution
└── FastAPI routes — thin wrappers over Plaid SDK calls
```

The service is intentionally minimal — no business logic, just a clean API surface over Plaid's SDK to avoid CORS issues and keep credentials server-side.

---

## Getting Started

```bash
git clone https://github.com/RyanBrin/dashboard-api
cd dashboard-api
pip install -r requirements.txt

# Required environment variables
export PLAID_CLIENT_ID=...
export PLAID_SECRET=...
export PLAID_ENV=sandbox          # or production
export DATABASE_URL=postgresql://...   # Supabase connection string

uvicorn main:app --reload
```

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `PLAID_CLIENT_ID` | Plaid application client ID |
| `PLAID_SECRET` | Plaid secret (sandbox or production) |
| `PLAID_ENV` | `sandbox` or `production` |
| `DATABASE_URL` | Supabase Postgres connection string |

---

## Deployment

Deployed on **Railway** with automatic deploys from the `main` branch.

**Live:** [dashboard-api-production-ebee.up.railway.app](https://dashboard-api-production-ebee.up.railway.app)

> **Note:** Plaid production approval is pending. Currently running in sandbox mode with test credentials.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.11+ |
| Web framework | FastAPI + uvicorn |
| Banking data | Plaid SDK |
| Database | Supabase (asyncpg) |
| Deployment | Railway |
