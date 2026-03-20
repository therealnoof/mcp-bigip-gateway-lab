# BIG-IP APM as MCP Gateway — Demo Lab

## The Story

An AI agent needs to query a **legacy HR system** that only speaks Basic Auth.
The agent speaks **OAuth 2.1** (as required by the MCP specification for remote servers).
**BIG-IP APM** sits in the middle, acting as:

1. **OAuth 2.1 Authorization Server** — Issues and validates tokens
2. **MCP Gateway** — Proxies MCP protocol traffic (SSE/Streamable HTTP)
3. **Credential Translator** — Converts Bearer tokens → Basic Auth headers

The legacy app is **never modified**. The agent uses **modern, standards-based auth**.
APM bridges the gap.

## Architecture

```
┌──────────────┐   OAuth 2.1    ┌──────────────────┐   Basic Auth   ┌──────────────┐
│   AI Agent   │ ─────────────► │    BIG-IP APM    │ ─────────────► │  Legacy HR   │
│  (Ollama +   │   Bearer JWT   │  OAuth AS +      │   (injected    │  REST API    │
│   MCP Client)│ ◄───────────── │  MCP Gateway +   │    by APM)     │  (Flask)     │
│              │   Tool results │  Cred Translation│ ◄───────────── │              │
└──────────────┘                └──────────────────┘                └──────────────┘
```

## Quick Start (Standalone — No BIG-IP)

Test the app components without BIG-IP first:

```bash
# Start all services
docker compose up -d

# Wait for Ollama to pull the model (~2-3 min on first run)
docker compose logs -f model-puller

# Test the MCP tools work
docker compose run --rm agent python agent.py test

# Ask the agent a question
docker compose run --rm agent python agent.py "Who has TS/SCI clearance in the cybersecurity department?"
```

## Adding BIG-IP APM

See [docs/APM-CONFIGURATION-GUIDE.md](docs/APM-CONFIGURATION-GUIDE.md) for the
full step-by-step guide to configure APM as the OAuth AS and MCP gateway.

## Project Structure

```
mcp-bigip-demo/
├── docker-compose.yml          # Wires all services together
├── legacy-hr-app/
│   ├── app.py                  # Flask REST API with Basic Auth
│   ├── requirements.txt
│   └── Dockerfile
├── mcp-server/
│   ├── server.py               # FastMCP server wrapping the HR API
│   ├── requirements.txt
│   └── Dockerfile
├── agent/
│   ├── agent.py                # AI agent (MCP client + Ollama)
│   ├── requirements.txt
│   └── Dockerfile
└── docs/
    └── APM-CONFIGURATION-GUIDE.md  # BIG-IP APM setup guide
```

## Sample Queries

- "List all employees in the cybersecurity department"
- "Who has TS/SCI clearance and works in Arlington?"
- "What departments does the agency have and who leads them?"
- "Find information about Sarah Chen"
- "How many people are on leave right now?"
- "Who reports to the CISO?"

## Components

| Component | Port | Auth | Role |
|-----------|------|------|------|
| Legacy HR API | 5001 | Basic Auth | The "old" app |
| MCP Server | 8080 | None (BIG-IP handles it) | Tool wrapper |
| Ollama | 11434 | None | Local LLM |
| BIG-IP APM | 443 | OAuth 2.1 | Gateway + AuthZ |
