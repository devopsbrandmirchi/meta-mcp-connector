# VDP MCP Connector

Plain-language MCP for **Smart Analytics V2**  
Supabase: https://rllwmeqingvuohyctddg.supabase.co

Users ask in simple English. They do **not** need table names or client IDs.

## What it maps (behind the scenes)

| User asks about | Tables used |
|---|---|
| Dealers | `smart_hoot_config` |
| VDP KPIs / daily chart / make·model·location | `smart_final_data` |
| All page views / channels / titles | `smart_ga4_page_data` |
| Locations | `smart_dealer_locations` |
| GA4 sync | `smart_ga4_config` |
| VDP URL rules | `smart_vdp_logic` |
| Inventory | `smart_hoot_inventory`, `smart_scrap_inventory` |
| Who has access | `smart_user_roles` (+ related) |

## Setup (local)

```bash
cd vdp-mcp-connector
python -m venv .venv
# Windows:
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

1. Open [API settings](https://supabase.com/dashboard/project/rllwmeqingvuohyctddg/settings/api)
2. Copy **service_role** secret into `.env` as `SUPABASE_SERVICE_ROLE_KEY`
3. Start the server:

```bash
python vdp_mcp_server.py --http
```

Listens on `http://127.0.0.1:8001/mcp`.

### Cursor (local)

```json
"vdp": {
  "url": "http://127.0.0.1:8001/mcp"
}
```

1. Keep this terminal running: `python vdp_mcp_server.py --http`  
2. Cursor **Settings → MCP** → refresh / reload  
3. **vdp** should show green with tools like `list_dealers`, `get_vdp_summary`, …

Then ask: “List dealers” or “VDP views for Moix RV last 7 days”.

## Google Cloud Run

Host the same HTTP MCP endpoint on Cloud Run (same pattern as the DV360 MCP connector). The image does **not** bake in `.env` or the service_role key.

### Prerequisites

- [Google Cloud SDK (`gcloud`)](https://cloud.google.com/sdk/docs/install) installed and logged in (`gcloud auth login`)
- A GCP project with **billing enabled**
- Your GCP **Project ID**
- Supabase **service_role** key ready (from [API settings](https://supabase.com/dashboard/project/rllwmeqingvuohyctddg/settings/api))

### Deploy

From this repo folder:

```powershell
# Option A: key already in local .env (not committed)
.\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID"

# Option B: key in a one-line file (gitignored)
.\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID" -SecretFile ".\service-role.key"

# Optional overrides
.\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID" -Region "us-central1" -Service "vdp-mcp" -SecretName "vdp-supabase-service-role"
```

What the script does:

1. Enables Cloud Run, Cloud Build, Secret Manager, Artifact Registry  
2. Creates/updates Secret Manager secret `vdp-supabase-service-role` from `.env`, `-SecretFile`, or a prompt  
3. Deploys with `--source .` (Dockerfile), env vars, and secret injection  
4. Prints the connector URL: `https://SERVICE-URL/mcp`

Deploy flags (mirrors DV360):

- `--set-env-vars MCP_TRANSPORT=http,HOST=0.0.0.0,SUPABASE_URL=https://rllwmeqingvuohyctddg.supabase.co`
- `--set-secrets SUPABASE_SERVICE_ROLE_KEY=vdp-supabase-service-role:latest`
- `--allow-unauthenticated` (needed for a working MCP URL on first deploy)
- `--session-affinity`, `--max-instances 1`, `--timeout 300`, `--port 8080`

### Cursor (Cloud Run)

After deploy, put the printed URL in `%USERPROFILE%\.cursor\mcp.json`:

```json
"vdp": {
  "url": "https://YOUR-CLOUD-RUN-URL/mcp"
}
```

Example (replace with your real service URL from `gcloud run services describe`):

```json
"vdp": {
  "url": "https://vdp-mcp-xxxxx-uc.a.run.app/mcp"
}
```

Refresh MCP in Cursor until **vdp** is green. You do **not** need a local `python vdp_mcp_server.py` process when using Cloud Run.

### Claude custom connector

**Settings → Connectors → Add custom connector** → paste the same `https://YOUR-CLOUD-RUN-URL/mcp` URL.

### Local vs Cloud

| | Local | Cloud Run |
|---|---|---|
| Start | `python vdp_mcp_server.py --http` | `.\deploy-cloudrun.ps1 -ProjectId ...` |
| URL | `http://127.0.0.1:8001/mcp` | `https://…run.app/mcp` |
| Secrets | `.env` file | Secret Manager → `SUPABASE_SERVICE_ROLE_KEY` |
| Host / port | `127.0.0.1:8001` | `0.0.0.0` + `PORT=8080` |
| Tools | Same | Same |

### Security note

`--allow-unauthenticated` makes the MCP endpoint **publicly reachable**. Anyone with the URL can call tools that use your Supabase **service_role** key. Use this for a first working connector only; **add authentication** (IAM, IAP, API gateway, or shared secret) before sharing widely.

## Example prompts

- "List RV dealers"
- "VDP views for Moix RV from 2026-08-01 to 2026-08-17"
- "Daily VDP trend for Zoomers RV last week"
- "Break down VDP by make for Beaver Coach Sales"
- "Which channels drove VDP traffic for A&L RV?"
- "Top VDP pages for Gerzeny’s RV"
- "Search inventory for Forest River at Moix"
- "Show VDP URL rules for Beaver Coach"
- "Is GA4 sync OK for Moix RV?"

## Tools

| Tool | Purpose |
|---|---|
| `help_vdp` | What you can ask |
| `list_dealers` | Find dealers by name/category |
| `list_locations` | Rooftops / locations |
| `get_vdp_summary` | VDP KPI totals |
| `get_daily_trend` | Day-by-day VDP chart data |
| `get_vdp_breakdown` | By location / make / model / year / type / condition |
| `get_channel_mix` | Channel / traffic mix |
| `get_top_pages` | Top paths & titles |
| `search_inventory` | Hoot + scrap inventory |
| `get_page_rules` | VDP/SRP filtration logic |
| `get_ga4_sync_status` | Sync health |
| `list_user_access` | Role assignments |
