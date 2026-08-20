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

## Setup

```bash
cd vdp_mcp_connector
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

### Cursor

Your global Cursor MCP config (`%USERPROFILE%\.cursor\mcp.json`) should include:

```json
"vdp": {
  "url": "http://127.0.0.1:8001/mcp"
}
```

1. Keep this terminal running: `python vdp_mcp_server.py --http`  
   (Uvicorn serves the MCP endpoint — that is expected.)
2. Cursor **Settings → MCP** → refresh / reload
3. **vdp** should show green with tools like `list_dealers`, `get_vdp_summary`, …

Then ask: “List dealers” or “VDP views for Moix RV last 7 days”.

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

Existing DV360 MCP files in the parent folder are unchanged.
