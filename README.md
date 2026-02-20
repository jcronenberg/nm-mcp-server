# NetworkManager MCP Server

A Model Context Protocol (MCP) server for NetworkManager.

## Usage

```bash
uv run main.py
```

The transport protocol can be controlled via the `MCP_TRANSPORT` environment variable (`stdio` or `http/streamable-http`).
