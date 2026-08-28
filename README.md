# Meta MCP Connector

Plain-language MCP server for **Meta / Facebook Ads** via the Graph API.

Ask about ad accounts, campaigns, ad sets, spend, and performance in natural language — no table names or raw API paths required.

## Features

- Live data from the Facebook Marketing API (Graph API)
- List ad accounts with parent Business Manager
- Campaign / ad set / ad performance (spend, impressions, clicks, purchases)
- Account-level spend dashboard (`account-spend.html`)
- Claude.ai OAuth when deployed to Cloud Run (`MCP_PUBLIC_URL`)

## Project structure

```
meta-mcp-connector/
├── meta_mcp_server.py    # MCP server + HTTP routes + tools
├── meta_graph.py         # Facebook Graph API client
├── meta_oauth.py         # Claude-compatible OAuth provider
├── account-spend.html    # Spend-by-account dashboard
├── test-accounts.html    # Ad account list test page
├── get-token.html        # Helper to obtain FACEBOOK_ACCESS_TOKEN
├── requirements.txt
├── Dockerfile
├── deploy-cloudrun.ps1   # Deploy to Google Cloud Run
├── .env.example
└── .cursor/mcp.json      # Local Cursor MCP config (example)
```

## Setup (local)

### 1. Clone and install

```bash
git clone https://github.com/devopsbrandmirchi/meta-mcp-connector.git
cd meta-mcp-connector
python -m venv .venv

# Windows
.venv\Scripts\activate

pip install -r requirements.txt
copy .env.example .env
```

### 2. Configure `.env`

```env
FACEBOOK_APP_ID=your_app_id
FACEBOOK_APP_SECRET=your_app_secret
FACEBOOK_ACCESS_TOKEN=your_user_token_with_ads_read
FACEBOOK_AD_ACCOUNT_ID=114810198697538
```

**Token requirements**

- Must be a **User Token** (`EAA…`), not an App Token (`app_id|secret`)
- Generated in [Graph API Explorer](https://developers.facebook.com/tools/explorer/) for **your** `FACEBOOK_APP_ID`
- Permissions: `ads_read`, `ads_management`, `business_management`

Use `get-token.html` or run the server and open `http://127.0.0.1:8001/get-token`.

### 3. Start the server

```bash
python meta_mcp_server.py --http
```

| Endpoint | URL |
|----------|-----|
| MCP | `http://127.0.0.1:8001/mcp` |
| Account spend UI | `http://127.0.0.1:8001/account-spend` |
| Account list API | `http://127.0.0.1:8001/api/accounts` |
| Account spend API | `http://127.0.0.1:8001/api/account-spend?date=YYYY-MM-DD` |

### 4. Cursor MCP config

`.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "meta": {
      "url": "http://127.0.0.1:8001/mcp"
    }
  }
}
```

Reload MCP in Cursor settings after starting the server.

## MCP tools

| Tool | Purpose |
|------|---------|
| `help_meta` | What you can ask |
| `list_accounts` | Ad accounts + parent Business Manager |
| `list_campaigns` | Campaigns in an ad account |
| `list_adsets` | Ad sets in an ad account |
| `list_creatives` | Ads + creative thumbnails / preview URLs |
| `get_integration_status` | Token / app health check |
| `get_ads_summary` | KPI totals for a date range |
| `get_daily_trend` | Day-by-day spend & metrics |
| `get_hourly_performance` | Hourly spend / clicks / purchases |
| `get_performance_breakdown` | By campaign / adset / placement / region / age / gender / … |
| `get_demographics_breakdown` | Age / gender / age×gender |
| `get_conversions_by_region` | Purchases & spend by state/region |
| `get_reach_frequency` | Reach & frequency (delivery pressure) |
| `get_top_ads` | Top ads by spend or other metric |
| `get_campaign_budgets` | Campaign budgets + status |
| `set_object_status` | Pause / activate campaign, ad set, or ad (`ads_management`) |
| `get_adset_targeting` | Who each ad set targets |
| `get_multi_account_spend` | Spend rollup across all accounts |

## Example prompts

- "List my Meta ad accounts"
- "Spend across all accounts yesterday"
- "Meta ads summary from 2026-08-01 to 2026-08-15"
- "Daily spend last 7 days for WOW Ad Account"
- "Hourly performance yesterday"
- "Age and gender breakdown last week"
- "Conversions by state for yesterday"
- "Show creatives with preview URLs"
- "Who does the Prospecting ad set target?"
- "Campaign budgets and status"
- "Pause campaign 120…" / "Activate campaign 120…"
- "Reach and frequency by campaign this month"
- "Top ads by spend yesterday"
- "Break down spend by campaign last week"

## Google Cloud Run

Host the same HTTP MCP endpoint on Cloud Run (same pattern as [vdp-connector](https://github.com/devopsbrandmirchi/vdp-connector)). The Docker image does **not** bake in `.env` or secrets.

### Prerequisites

- [Google Cloud SDK (`gcloud`)](https://cloud.google.com/sdk/docs/install) installed and logged in (`gcloud auth login`)
- A GCP project with **billing enabled**
- Your GCP **Project ID**
- Facebook credentials ready in local `.env` (see `.env.example`)

### Deploy

From this repo folder:

```powershell
# App secret read from .env
.\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID"

# Optional overrides
.\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID" -Region "us-central1" -Service "meta-mcp"

# App secret from a one-line file (gitignored)
.\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID" -SecretFile ".\facebook-app-secret.key"
```

What the script does:

1. Enables Cloud Run, Cloud Build, Secret Manager, Artifact Registry
2. Uploads `FACEBOOK_APP_SECRET` → Secret Manager (`meta-facebook-app-secret`)
3. Uploads `FACEBOOK_ACCESS_TOKEN` → Secret Manager (`meta-facebook-access-token`) if set in `.env`
4. Deploys with `--source .` (Dockerfile), env vars, and secret injection
5. Sets `MCP_PUBLIC_URL` to the service URL (required for Claude OAuth)
6. Prints the connector URL: `https://SERVICE-URL/mcp`

Deploy flags:

- `--set-env-vars MCP_TRANSPORT=http,HOST=0.0.0.0,FACEBOOK_APP_ID=…,FACEBOOK_AD_ACCOUNT_ID=…`
- `--set-secrets FACEBOOK_APP_SECRET=meta-facebook-app-secret:latest,FACEBOOK_ACCESS_TOKEN=meta-facebook-access-token:latest`
- `--allow-unauthenticated` (needed for a working MCP URL on first deploy)
- `--session-affinity`, `--max-instances 1`, `--timeout 300`, `--port 8080`

### Cursor (Cloud Run)

After deploy, put the printed URL in `.cursor/mcp.json`:

```json
"meta": {
  "url": "https://YOUR-CLOUD-RUN-URL/mcp"
}
```

You do **not** need a local `python meta_mcp_server.py` process when using Cloud Run.

### Claude custom connector

Claude.ai **requires OAuth** for remote custom connectors. This server embeds a Claude-compatible OAuth provider (Dynamic Client Registration) when `MCP_PUBLIC_URL` is set (the deploy script sets it automatically).

1. Deploy with `.\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID"`
2. Remove any old broken connector
3. **Settings → Connectors → Add custom connector**
4. Paste **exactly**: `https://YOUR-CLOUD-RUN-URL/mcp` (must include `/mcp`)
5. Leave **OAuth Client ID** empty in Advanced settings (DCR is enabled)
6. Click Connect — Claude registers itself and authorizes briefly

After a successful Connect, Claude should **stay connected** when you reopen the app.
The connector persists OAuth refresh tokens on disk (`MCP_OAUTH_STATE_PATH`, default
`/tmp/meta-mcp-oauth-state.json` on Cloud Run) and Cloud Run is set to `--min-instances 1`
so the warm instance keeps that state. You only need to reconnect after a full redeploy
or if you remove the connector.

If you see *"Couldn't register with … sign-in service"* (`ofid_…`):

- Confirm the URL ends with `/mcp` (not the bare Cloud Run host)
- Confirm the service is up: open `https://YOUR-CLOUD-RUN-URL/.well-known/oauth-authorization-server` — you should see JSON with `registration_endpoint`
- Confirm `MCP_PUBLIC_URL` matches the service origin (redeploy if you changed the URL)
- Do **not** paste a random OAuth Client ID unless you set up an external IdP

If you see *"This connector has a server configuration issue"*:

- **Redeploy** the latest code (fixes OAuth metadata for Claude: public-client DCR + `/mcp` protected-resource metadata)
- **Remove** the old connector in Claude, then add it again (clears stale OAuth registration)
- Verify health: `https://YOUR-CLOUD-RUN-URL/health` → `oauth_enabled: true`
- Verify protected resource: `https://YOUR-CLOUD-RUN-URL/.well-known/oauth-protected-resource/mcp` → JSON (not 404)
- Verify auth metadata includes `"none"` in `token_endpoint_auth_methods_supported`

### Verify deployment

| Check | URL |
|-------|-----|
| Health | `https://YOUR-SERVICE-URL/health` |
| MCP endpoint | `https://YOUR-SERVICE-URL/mcp` |
| OAuth discovery | `https://YOUR-SERVICE-URL/.well-known/oauth-authorization-server` |
| Protected resource | `https://YOUR-SERVICE-URL/.well-known/oauth-protected-resource/mcp` |
| Account spend UI | `https://YOUR-SERVICE-URL/account-spend` |

### Local vs Cloud

| | Local | Cloud Run |
|---|---|---|
| Start | `python meta_mcp_server.py --http` | `.\deploy-cloudrun.ps1 -ProjectId …` |
| URL | `http://127.0.0.1:8001/mcp` | `https://…run.app/mcp` |
| Secrets | `.env` file | Secret Manager → env injection |
| Host / port | `127.0.0.1:8001` | `0.0.0.0` + `PORT=8080` |
| Claude OAuth | Off (no `MCP_PUBLIC_URL`) | On (`MCP_PUBLIC_URL` = service URL) |
| Tools | Same | Same |

### Security note

`--allow-unauthenticated` makes the Cloud Run URL publicly reachable. OAuth means Claude must complete registration/authorize before tools work; random callers without a token get `401`. Still treat the URL as sensitive and add stronger auth before sharing widely.

### Common pitfalls

1. Claude URL missing `/mcp` → "Couldn't connect to the server"
2. `MCP_PUBLIC_URL` not set → OAuth 404 → "Couldn't register with sign-in service"
3. OAuth metadata missing `/mcp` protected resource or `"none"` auth method → "server configuration issue"
4. Committing `.env` or secret files
5. Forgetting to redeploy after OAuth code changes
6. Deploying without `FACEBOOK_ACCESS_TOKEN` → Graph API returns permission errors

## Push to GitHub

Repo: https://github.com/devopsbrandmirchi/meta-mcp-connector

```powershell
# Sign in to the devopsbrandmirchi GitHub account if needed
git credential-manager github login --force

# Point origin at the org repo (replaces any old personal remote)
git remote remove origin 2>$null
git remote add origin https://github.com/devopsbrandmirchi/meta-mcp-connector.git
git push -u origin main
```

For a **new Cloud Run** service (fresh URL, no old `meta-mcp-connector-git` fallback):

```powershell
.\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID" -Region "us-central1" -Service "meta-mcp"
```

Then in Claude use exactly: `https://YOUR-NEW-SERVICE-URL/mcp`

## Security

- Never commit `.env` — it is gitignored
- `FACEBOOK_ACCESS_TOKEN` is a secret; rotate if exposed
- Cloud Run deploy uses `--allow-unauthenticated` for MCP URL reachability; OAuth protects tool access when `MCP_PUBLIC_URL` is set
