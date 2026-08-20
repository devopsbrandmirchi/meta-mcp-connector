# VDP MCP server for Google Cloud Run / Docker
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=http \
    HOST=0.0.0.0 \
    PORT=8080

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY vdp_mcp_server.py vdp_oauth.py .

# SUPABASE_SERVICE_ROLE_KEY is injected at runtime (Secret Manager), not baked in.
# MCP_PUBLIC_URL must be set to the Cloud Run service URL for Claude OAuth.
# SUPABASE_URL is set via Cloud Run --set-env-vars.

EXPOSE 8080

CMD ["python", "vdp_mcp_server.py"]
