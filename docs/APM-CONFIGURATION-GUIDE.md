# BIG-IP APM Configuration Guide
## MCP Gateway with OAuth 2.1 Authorization Server + Credential Translation

---

## Overview

This guide configures BIG-IP APM to act as:

1. **OAuth 2.1 Authorization Server** — Issues access tokens to the AI agent
2. **MCP Gateway** — Proxies MCP traffic (Streamable HTTP/SSE) to the backend MCP server
3. **Credential Translator** — Converts OAuth Bearer tokens to Basic Auth for the legacy HR API

All three functions run on a single Virtual Server.

---

## Prerequisites

- BIG-IP VE 17.1+ (APM provisioned)
- APM licensed (OAuth Server requires APM)
- Network access to the MCP server (port 8080) and Legacy HR API (port 5001)
- SSL certificate for the VIP (self-signed OK for lab)

---

## Step 1: Create the OAuth Provider (Authorization Server)

APM's built-in OAuth Authorization Server will issue tokens to the AI agent.

### 1.1 Create an OAuth Profile

Navigate to: **Access > Federation > OAuth Authorization Server > OAuth Profile**

| Setting | Value |
|---------|-------|
| Name | `mcp-oauth-profile` |
| Type | `Provider` |
| Token Lifetime | `3600` (1 hour) |
| Token Type | `JWT` |
| Issuer | `https://bigip.lab.local` |
| Audience | `mcp-hr-tools` |
| Signing Algorithm | `RS256` |

### 1.2 Register the Agent as an OAuth Client

Navigate to: **Access > Federation > OAuth Authorization Server > Client Application**

| Setting | Value |
|---------|-------|
| Name | `mcp-agent-client` |
| Application Name | `MCP HR Agent` |
| Client ID | `mcp-agent` |
| Client Secret | `agent-secret-2024` |
| Grant Type | `Client Credentials` (for machine-to-machine) |
| Scopes | `mcp:tools` |
| Redirect URI | (not needed for client_credentials) |

> **Note:** For a demo with a human-in-the-loop, add `Authorization Code` grant
> type with PKCE support. APM supports this natively as of 17.1.

### 1.3 Create a Scope

Navigate to: **Access > Federation > OAuth Authorization Server > Scope**

| Setting | Value |
|---------|-------|
| Name | `mcp:tools` |
| Description | `Access to MCP tools on the HR system` |

---

## Step 2: Create the Access Policy (Per-Request Policy)

We need two policies:
- **Access Policy** — Handles the OAuth token issuance endpoints
- **Per-Request Policy** — Validates Bearer tokens on MCP requests and translates credentials

### 2.1 Access Policy for OAuth Endpoints

Navigate to: **Access > Profiles / Policies > Access Profiles**

Create a new profile:

| Setting | Value |
|---------|-------|
| Name | `mcp-gateway-ap` |
| Profile Type | `OAuth-AuthZ Server` |
| OAuth Profile | `mcp-oauth-profile` |
| Languages | `English` |

The built-in OAuth-AuthZ Server profile type handles:
- `/.well-known/oauth-authorization-server` (metadata discovery)
- `/f5-oauth2/v1/authorize` (authorization endpoint)
- `/f5-oauth2/v1/token` (token endpoint)

### 2.2 Per-Request Policy for MCP Traffic

Navigate to: **Access > Profiles / Policies > Per-Request Policies**

Create: `mcp-gateway-prp`

**Policy Flow:**

```
Start
  │
  ├─► [URL Branch] ─── Path starts with /.well-known/ ──► Allow (OAuth metadata)
  │
  ├─► [URL Branch] ─── Path starts with /f5-oauth2/ ───► Allow (OAuth endpoints)
  │
  └─► [OAuth Scope Check] ─── Validate Bearer Token
        │
        ├─► Token Valid + scope "mcp:tools" present
        │     │
        │     └─► [Variable Assign] ─── Set Basic Auth credentials
        │           │
        │           └─► [HTTP Headers] ─── Inject Authorization: Basic header
        │                 │
        │                 └─► Allow (proxy to MCP server)
        │
        └─► Token Invalid / Missing
              │
              └─► Reject (401)
```

### 2.3 Variable Assign — Credential Translation

This is the key APM action that converts the authenticated OAuth identity
into the Basic Auth credentials the legacy HR app expects.

In the **Variable Assign** action, create a custom variable:

**Custom Variable:**
```
Name:  perflow.basic_auth_header
Value: (custom expression)
```

**TCL Expression:**
```tcl
# Base64 encode the service account credentials for the legacy HR API
# In production, these could come from a credential vault or
# be mapped per-user from the OAuth token claims
set username "hr_service"
set password "legacy_pass_2024"
set credentials [b64encode "${username}:${password}"]
return "Basic ${credentials}"
```

### 2.4 HTTP Headers Action

After the Variable Assign, add an **HTTP Headers Modify** action:

| Action | Header Name | Value |
|--------|-------------|-------|
| Replace | `Authorization` | `%{perflow.basic_auth_header}` |

This strips the incoming Bearer token and replaces it with Basic Auth
before the request hits the backend MCP server.

---

## Step 3: Protected Resource Metadata (MCP Discovery)

The MCP spec requires a `/.well-known/oauth-protected-resource` endpoint
that tells agents where to authenticate. We'll serve this from an iRule.

### 3.1 Create the iRule

Navigate to: **Local Traffic > iRules**

```tcl
# ──────────────────────────────────────────────────────────
# iRule: mcp-protected-resource-metadata
# Serves the OAuth Protected Resource Metadata document
# per RFC 9728, as required by the MCP Authorization spec.
# ──────────────────────────────────────────────────────────

when HTTP_REQUEST {
    # Serve the Protected Resource Metadata (RFC 9728)
    if { [HTTP::uri] eq "/.well-known/oauth-protected-resource" } {
        set response_body \{
  "resource": "https://[HTTP::host]/mcp",
  "authorization_servers": \["https://[HTTP::host]"\],
  "scopes_supported": \["mcp:tools"\],
  "bearer_methods_supported": \["header"\],
  "resource_documentation": "https://[HTTP::host]/docs/mcp-api"
\}
        HTTP::respond 200 content $response_body \
            "Content-Type" "application/json" \
            "Access-Control-Allow-Origin" "*" \
            "Cache-Control" "max-age=3600"
        return
    }

    # Serve OAuth Authorization Server Metadata
    # (APM handles this natively, but this ensures the discovery URL works)
    if { [HTTP::uri] eq "/.well-known/oauth-authorization-server" } {
        # Let APM handle this — it auto-generates the metadata
        return
    }
}
```

---

## Step 4: Virtual Server Configuration

### 4.1 Create the Virtual Server

Navigate to: **Local Traffic > Virtual Servers**

| Setting | Value |
|---------|-------|
| Name | `vs-mcp-gateway` |
| Destination | `<VIP IP>:443` |
| Service Port | `443` |
| HTTP Profile | `http` |
| SSL Profile (Client) | `mcp-gateway-clientssl` |
| Source Address Translation | `Auto Map` |
| Access Profile | `mcp-gateway-ap` |
| Per-Request Policy | `mcp-gateway-prp` |
| iRule | `mcp-protected-resource-metadata` |

### 4.2 Create the Pool

Navigate to: **Local Traffic > Pools**

| Setting | Value |
|---------|-------|
| Name | `pool-mcp-server` |
| Monitor | `http` |
| Members | `<MCP Server IP>:8080` |

### 4.3 HTTP Profile Settings

Ensure the HTTP profile supports:
- **Server-Sent Events (SSE)** — MCP uses SSE for streaming responses
- **WebSocket passthrough** (if using Streamable HTTP with WebSocket upgrade)

Create a custom HTTP profile:

| Setting | Value |
|---------|-------|
| Name | `http-mcp` |
| Proxy Type | `Reverse` |
| Response Chunking | `Preserve` |
| Insert X-Forwarded-For | `Enabled` |

> **Important for SSE:** Ensure no response buffering that could interfere
> with SSE streaming. The default HTTP profile should work, but verify by
> testing tool calls through the VIP.

---

## Step 5: Test the Configuration

### 5.1 Test OAuth Metadata Discovery

```bash
# Should return the authorization server metadata
curl -sk https://bigip.lab.local/.well-known/oauth-authorization-server | jq .

# Should return the protected resource metadata
curl -sk https://bigip.lab.local/.well-known/oauth-protected-resource | jq .
```

### 5.2 Test Token Issuance

```bash
# Request a token using client_credentials grant
TOKEN=$(curl -sk https://bigip.lab.local/f5-oauth2/v1/token \
  -d "grant_type=client_credentials" \
  -d "client_id=mcp-agent" \
  -d "client_secret=agent-secret-2024" \
  -d "scope=mcp:tools" | jq -r '.access_token')

echo "Token: $TOKEN"
```

### 5.3 Test MCP Through BIG-IP

```bash
# Test the MCP SSE endpoint with the Bearer token
curl -sk -H "Authorization: Bearer $TOKEN" \
  https://bigip.lab.local/mcp/sse
```

### 5.4 Run the Agent Through BIG-IP

Update the agent environment variables in docker-compose.yml:

```yaml
agent:
  environment:
    - AUTH_MODE=bigip
    - MCP_SERVER_URL=https://bigip.lab.local/mcp/sse
    - BIGIP_TOKEN_URL=https://bigip.lab.local/f5-oauth2/v1/token
    - BIGIP_CLIENT_ID=mcp-agent
    - BIGIP_CLIENT_SECRET=agent-secret-2024
```

Then run:

```bash
docker compose run --rm agent python agent.py "Who in the cybersecurity department has TS/SCI clearance?"
```

---

## Architecture Validation

When working end-to-end, the flow should be:

```
1. Agent starts → no token
2. Agent POSTs to /f5-oauth2/v1/token → APM issues JWT
3. Agent connects to /mcp/sse with Bearer token
4. APM Per-Request Policy validates JWT
5. APM strips Bearer, injects Basic Auth header
6. Request proxied to MCP server (port 8080)
7. MCP server calls Legacy HR API with Basic Auth (already present)
8. Results flow back: HR API → MCP Server → BIG-IP → Agent
9. Agent feeds results to Ollama for reasoning
```

**What the customer sees:**
- The AI agent uses standard OAuth 2.1 — modern, secure, auditable
- The legacy HR app is untouched — still running Basic Auth
- BIG-IP APM bridges the gap with zero application changes
- Full audit trail: who authenticated, what tokens were issued, what tools were called

---

## Demo Talking Points

1. **"The legacy app doesn't change."** The HR API has been running for years.
   Nobody wants to rewrite it. Nobody needs to. APM handles the translation.

2. **"OAuth 2.1 for agents is the future."** The MCP spec mandates OAuth 2.1
   for remote servers. BIG-IP APM is already an OAuth AS — this is day-one
   capability, not a roadmap item.

3. **"Single pane of control."** APM gives you visibility into every agent
   request: who authenticated, what tokens were issued, what tools were
   called, and what data flowed. Try getting that from a DIY OAuth stack.

4. **"Protocol translation is what APM was built for."** We've been doing
   SAML-to-Kerberos, OIDC-to-NTLM, cert-to-header for 15 years. OAuth-to-
   Basic-Auth for AI agents is just the latest translation.

5. **"Zero Trust for AI agents."** Every tool call is authenticated,
   authorized, and logged. The agent gets least-privilege scoped tokens.
   APM can enforce additional policies: time-of-day, source IP,
   rate limiting, etc.
