# NetworkManager MCP Server

A Model Context Protocol (MCP) server for NetworkManager.

## Usage

```bash
uv run main.py
```

The server can be configured via environment variables:
- `MCP_TRANSPORT`: Control the transport protocol (default: `stdio` but there is also `http/streamable-http`).
- `MCP_HOST`: The host to bind to (default: `0.0.0.0`).
- `MCP_PORT`: The port to bind to (default: `8000`).
