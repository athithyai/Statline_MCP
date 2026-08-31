# mcp-statline over Streamable HTTP.
#
# FastMCP Cloud does not use this file - it builds from requirements.txt and
# imports `server.py:mcp` directly. This image is for running the server
# anywhere else (Render, Fly, Railway, a plain VM, or locally).
#
#   docker build -t mcp-statline .
#   docker run --rm -p 8000:8000 mcp-statline
#
# The server is then at http://localhost:8000/mcp

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Dependencies first, so edits to the server do not invalidate this layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cbs.py server.py ./
COPY scripts/ ./scripts/

# Run unprivileged: the server only makes outbound HTTPS calls to CBS.
RUN useradd --create-home --uid 10001 statline
USER statline

EXPOSE 8000

# Verifies the MCP endpoint answers a real protocol handshake, not just a port.
HEALTHCHECK --interval=30s --timeout=15s --start-period=15s --retries=3 \
    CMD ["python", "scripts/health_check.py", "--transport-only"]

CMD ["python", "server.py"]
