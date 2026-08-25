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

```powershell
.\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID"
```

Secrets: `FACEBOOK_APP_SECRET` in Secret Manager.  
Env vars: `FACEBOOK_APP_ID`, `FACEBOOK_AD_ACCOUNT_ID`, `MCP_PUBLIC_URL` (set automatically).

After deploy, use `https://YOUR-SERVICE-URL/mcp` in Cursor or Claude custom connectors.

## Push to a new GitHub repo

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
