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
git clone https://github.com/YOUR_USERNAME/meta-mcp-connector.git
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
| `get_integration_status` | Token / app health check |
| `get_ads_summary` | KPI totals for a date range |
| `get_daily_trend` | Day-by-day spend & metrics |
| `get_performance_breakdown` | Breakdown by campaign / ad set / placement |
| `get_top_ads` | Top ads by spend or other metric |

## Example prompts

- "List my Meta ad accounts"
- "Meta ads summary from 2026-08-01 to 2026-08-15"
- "Daily spend last 7 days for WOW Ad Account"
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

If you see *"Couldn't register with … sign-in service"* (`ofid_…`):

- Confirm the URL ends with `/mcp` (not the bare Cloud Run host)
- Confirm the service is up: open `https://YOUR-CLOUD-RUN-URL/.well-known/oauth-authorization-server` — you should see JSON with `registration_endpoint`
- Confirm `MCP_PUBLIC_URL` matches the service origin (redeploy if you changed the URL)
- Do **not** paste a random OAuth Client ID unless you set up an external IdP

### Verify deployment

| Check | URL |
|-------|-----|
| MCP endpoint | `https://YOUR-SERVICE-URL/mcp` |
| OAuth discovery | `https://YOUR-SERVICE-URL/.well-known/oauth-authorization-server` |
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
3. Committing `.env` or secret files
4. Forgetting to redeploy after OAuth code changes
5. Deploying without `FACEBOOK_ACCESS_TOKEN` → Graph API returns permission errors

## Push to GitHub

```powershell
# Sign in to your new GitHub account first
git credential-manager github login --force

# Create repo on GitHub (browser or gh), then:
git remote add origin https://github.com/YOUR_USERNAME/meta-mcp-connector.git
git add .
git commit -m "Initial commit: Meta MCP connector"
git push -u origin main
```

## Security

- Never commit `.env` — it is gitignored
- `FACEBOOK_ACCESS_TOKEN` is a secret; rotate if exposed
- Cloud Run deploy uses `--allow-unauthenticated` for MCP URL reachability; OAuth protects tool access when `MCP_PUBLIC_URL` is set
