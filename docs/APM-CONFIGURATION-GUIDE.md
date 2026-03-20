# BIG-IP APM Configuration Guide
## MCP Gateway with OAuth Authorization Server + Credential Translation

---

## Overview

This guide configures BIG-IP APM to act as:

1. **OAuth Authorization Server** — Issues access tokens to the AI agent
2. **MCP Gateway** — Proxies MCP traffic (Streamable HTTP/SSE) to the backend MCP server
3. **Credential Translator** — Converts OAuth Bearer tokens to Basic Auth for the legacy HR API

The OAuth AS and MCP Gateway run on **separate Virtual Servers**:
- `vs-oauth-as` (10.1.10.110:443) — Token issuance
- `vs-mcp-gateway` (10.1.10.100:443) — MCP traffic with token validation

---

## Important: OAuth Grant Type Limitations

BIG-IP APM does **not** natively support the `client_credentials` OAuth grant type.
APM supports three grant types:
- **Authorization Code / Hybrid**
- **Implicit**
- **Resource Owner Password Credentials (ROPC)**

For this lab (machine-to-machine, no human user), we use **ROPC** as a workaround.
This requires creating a local user on BIG-IP for the agent to authenticate with,
even though there's no real human involved.

### Access Token vs JWT

APM can issue two types of tokens:

| Type | Format | Validation Method | Use Case |
|------|--------|-------------------|----------|
| **Opaque Token** | Random hex string (e.g., `2ef7db...`) | Introspection — the resource server calls back to the OAuth AS to validate | Simpler setup, requires network call for each validation |
| **JWT Token** | Base64-encoded JSON with signature (e.g., `eyJhbG...`) | Local validation — the resource server verifies the signature using the AS's public key (JWKS) | No callback needed, but requires JWKS endpoint access |

This lab uses **opaque tokens with introspection** for simplicity. To use JWT
tokens instead, enable "Support JWT Token" on the OAuth AS profile and configure
the JWKS endpoint for local validation on the MCP Gateway VIP.

### Production Recommendation

In production, you would **not** use this pattern. Instead:

1. Use an **external Identity Provider** (Azure AD, Okta, Ping Identity) that
   natively supports `client_credentials` grant for machine-to-machine auth
2. BIG-IP APM acts as the **Resource Server only** — validating tokens issued
   by the external IdP, not issuing them itself
3. No local users on BIG-IP, no ROPC workaround needed
4. The per-request policy validates the external JWT using the IdP's JWKS endpoint

This lab uses BIG-IP as both OAuth AS and gateway to keep the environment
self-contained with no external dependencies.

---

## Prerequisites

- BIG-IP VE 17.1+ (APM provisioned and licensed)
- Network access to the MCP server (10.1.20.50:8080)
- SSL certificate for the VIPs (self-signed OK for lab)
- Steps 1-9 of the [Deployment Guide](../DEPLOYMENT_GUIDE.md) completed

---

## Step 1: Create the JWT Key

Before creating the OAuth profile, you need a JSON Web Key for token signing.

Navigate to: **Access → Federation → JSON Web Token → Key Configuration**

| Setting | Value |
|---------|-------|
| Name | `mcp-jwt-key` |
| ID | `mcp-jwt-key` |
| JWT Type | `JWS` |
| Type | `RSA` |
| Signing Algorithm | `256` (RS256) |
| Certificate File | Select your self-signed certificate |
| Certificate Key | Select the matching private key |

> **Note:** You can use the BIG-IP's existing self-signed cert. If you need to
> create one: System → Certificate Management → Traffic Certificate Management →
> SSL Certificate List → Create.

---

## Step 2: Create the OAuth Scope

Navigate to: **Access → Federation → OAuth Authorization Server → Scope**

| Setting | Value |
|---------|-------|
| Name | `mcp-tools-scope` |
| Scope Name | `mcp:tools` |
| Scope Value | `mcp:tools` |
| Caption | `MCP Tools Access` |
| Detailed Description | `Access to MCP tools on the HR system` |

---

## Step 3: Create the OAuth Authorization Server Profile

Navigate to: **Access → Federation → OAuth Authorization Server → OAuth Profile**

| Setting | Value |
|---------|-------|
| Name | `mcp-oauth-as` |
| Issuer | `https://10.1.10.110` |
| JWT Key Type | `JWS` |
| JWT Primary Key | `mcp-jwt-key` |
| JWT Refresh Token Encryption Secret | `mcp-lab-refresh-secret-2024` |
| JWT Access Token Lifetime | `30` (minutes) |
| JWT Generate Refresh Token | Unchecked |
| Audience | `https://10.1.10.100` |

After saving, edit the profile again and add:
- **Client Application** — move `mcp-agent` to Selected (create this in Step 4 first, then come back)
- **Scope** — should already be available if created in Step 2

---

## Step 4: Register the Agent as an OAuth Client

Navigate to: **Access → Federation → OAuth Authorization Server → Client Application**

| Setting | Value |
|---------|-------|
| Name | `mcp-agent` |
| Application Name | `MCP Agent` |
| Caption | `MCP Agent Client` |
| Grant Type | `Resource Owner Password Credentials` |
| Authentication Type | `Secret` |
| Redirect URI(s) | `https://localhost/callback` (placeholder, not used by ROPC) |
| Scope | Select `mcp-tools-scope` |

> **Important:** The Client ID and Client Secret are **auto-generated** by BIG-IP
> after saving. Note them down — you'll need them for the agent configuration.
> They will NOT match any values you might have pre-planned.

After saving, go back to the OAuth Profile (Step 3) and add this client application.

---

## Step 5: Create the Local User for ROPC Authentication

Since we're using the ROPC grant type, APM needs a user to authenticate against.

### 5.1 Create a Local DB Instance

Navigate to: **Access → Authentication → Local User DB → Instances**

| Setting | Value |
|---------|-------|
| Name | `mcp-local-db` |

### 5.2 Create the Agent User

Navigate to: **Access → Authentication → Local User DB → Users**

| Setting | Value |
|---------|-------|
| User Name | `mcp-agent` |
| Password | `AgentPass2024!` |
| Instance | `mcp-local-db` |

---

## Step 6: Create the OAuth AS Access Profile and Virtual Server

### 6.1 Access Profile

Navigate to: **Access → Profiles/Policies → Access Profiles → Create**

| Setting | Value |
|---------|-------|
| Name | `ap-oauth-as` |
| Profile Type | `All` or `LTM-APM` |
| OAuth Profile | `mcp-oauth-as` |
| Languages | `English` |

> **Critical:** The OAuth Profile **must** be selected on the access profile
> properties. Without this, APM will not intercept `/f5-oauth2/v1/*` requests
> and you'll get 302 redirects to `/my.policy` instead of token responses.

### 6.2 Access Policy Configuration

Edit the per-session policy: **Access → Profiles/Policies → Access Profiles → `ap-oauth-as` → Edit**

**Policy Flow:**

```
Start
  │
  └─► [OAuth Logon Page]
        │
        ├─► F5 ROPC ──► Deny
        ├─► Okta ROPC ──► Deny
        ├─► Ping ROPC ──► Deny
        │
        └─► fallback ──► [LocalDB Auth] ──► [OAuth Authorization] ──► Allow
```

**Key configuration details:**

1. **OAuth Logon Page** — Add from the Logon tab. This agent extracts the
   `username` and `password` from the ROPC token request body and populates
   them into session variables.

2. **Branch routing** — The F5 ROPC branch expects specific session variables
   (`session.logon.last.oauthprovidertype == "ROPC"`) that are not set when
   BIG-IP is its own OAuth AS. The **fallback** branch is the one that fires.
   Route the **fallback** branch to LocalDB Auth. Set all named provider
   branches (F5 ROPC, Okta ROPC, Ping ROPC, etc.) to **Deny**.

3. **LocalDB Auth** — Add from the Authentication tab. Configure it to use
   the `mcp-local-db` instance created in Step 5.

4. **OAuth Authorization** — Add from the Authentication tab. Configure:
   - Audience: `https://10.1.10.100`
   - Scope/Claim Assign: add `mcp:tools`

5. Set the final ending to **Allow**.

6. Click **Apply Access Policy**.

> **Why the fallback branch?** This is a known quirk when BIG-IP acts as its
> own OAuth AS with ROPC. The OAuth Logon Page agent's named branches (F5 ROPC,
> Okta ROPC, etc.) check for provider type session variables that are only
> populated when using an external OAuth provider. Since BIG-IP is both the
> provider and the authenticator, these variables aren't set, and the request
> falls through to the fallback branch. This works correctly — it's just not
> the most intuitive policy layout.

### 6.3 Virtual Server

Navigate to: **Local Traffic → Virtual Servers → Create**

| Setting | Value |
|---------|-------|
| Name | `vs-oauth-as` |
| Destination Address | `10.1.10.110` |
| Service Port | `443` |
| HTTP Profile (Client) | `http` |
| SSL Profile (Client) | Select your self-signed client SSL profile |
| Source Address Translation | `Auto Map` |
| Access Profile | `ap-oauth-as` |

> **No pool needed** on this VIP. APM handles the OAuth endpoints internally —
> there's no backend server to proxy to.

### 6.4 Test Token Issuance

From the GPU Server:

```bash
curl -sk -X POST https://10.1.10.110/f5-oauth2/v1/token \
  -d 'grant_type=password&client_id=YOUR_CLIENT_ID&client_secret=YOUR_CLIENT_SECRET&username=mcp-agent&password=AgentPass2024!&scope=mcp:tools'
```

Expected response:

```json
{
  "access_token": "2ef7db604223fb64c315ad68efcc405f92d9c62440c94e708c01b9c1198637c9",
  "expires_in": 300,
  "token_type": "Bearer",
  "scope": "mcp:tools",
  "refresh_token": "9c2ceb7816941fbab08c69d29378a124c712397f48ba14a01970bf722afc729d"
}
```

> **Note:** The access token is an opaque token (hex string), not a JWT. See
> the "Access Token vs JWT" section at the top of this guide for details.

**Do not proceed until token issuance works.**

---

## Step 7: Create the MCP Gateway Virtual Server

This VIP handles the actual MCP traffic — SSE connections from the agent to the
MCP server, with Bearer token validation and credential translation.

### 7.1 Create the SSE HTTP Profile

MCP uses Server-Sent Events (SSE) which requires unbuffered streaming responses.

Navigate to: **Local Traffic → Profiles → Services → HTTP → Create**

| Setting | Value |
|---------|-------|
| Name | `http-mcp-sse` |
| Parent Profile | `http` |
| Response Chunking | `Preserve` |
| OneConnect Transformations | `Disabled` |

> **Critical for SSE:** Without this profile, BIG-IP may buffer SSE responses,
> causing the agent to hang waiting for MCP tool results. Also remove any
> response compression profiles from the VIP. If issues persist, disable the
> OneConnect profile entirely on the VIP.

### 7.2 Create the Pool

Navigate to: **Local Traffic → Pools → Create**

| Setting | Value |
|---------|-------|
| Name | `pool-mcp-server` |
| Monitor | `http` |
| Members | `10.1.20.50:8080` |

### 7.3 Create the iRule for Protected Resource Metadata

Navigate to: **Local Traffic → iRules → Create**

```tcl
# ──────────────────────────────────────────────────────────
# iRule: irule-mcp-prm
# Serves the OAuth Protected Resource Metadata document
# per RFC 9728, as required by the MCP Authorization spec.
# ──────────────────────────────────────────────────────────

when HTTP_REQUEST {
    if { [HTTP::uri] eq "/.well-known/oauth-protected-resource" } {
        set response_body \{
  "resource": "https://[HTTP::host]/mcp",
  "authorization_servers": \["https://10.1.10.110"\],
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
}
```

### 7.4 Per-Request Policy for Token Validation

Navigate to: **Access → Profiles/Policies → Per-Request Policies → Create**

Name: `prp-mcp-gateway`

**Policy Flow:**

```
Start
  │
  ├─► [URL Branch] ─── Path starts with /.well-known/ ──► Allow (metadata)
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

**Variable Assign — Credential Translation:**

This is the key action that converts the OAuth Bearer token into Basic Auth
credentials for the legacy HR app.

Custom Variable:
```
Name:  perflow.basic_auth_header
Value: (custom expression)
```

TCL Expression:
```tcl
set username "hr_service"
set password "legacy_pass_2024"
set credentials [b64encode "${username}:${password}"]
return "Basic ${credentials}"
```

**HTTP Headers Modify:**

| Action | Header Name | Value |
|--------|-------------|-------|
| Replace | `Authorization` | `%{perflow.basic_auth_header}` |

This strips the incoming Bearer token and replaces it with Basic Auth
before the request hits the backend MCP server.

### 7.5 Access Profile for MCP Gateway

Navigate to: **Access → Profiles/Policies → Access Profiles → Create**

| Setting | Value |
|---------|-------|
| Name | `ap-mcp-gateway` |
| Profile Type | `All` or `LTM-APM` |
| Languages | `English` |

The per-session policy for this profile can be simple — just `Start → Allow`.
The actual token validation happens in the per-request policy.

### 7.6 Virtual Server

Navigate to: **Local Traffic → Virtual Servers → Create**

| Setting | Value |
|---------|-------|
| Name | `vs-mcp-gateway` |
| Destination Address | `10.1.10.100` |
| Service Port | `443` |
| HTTP Profile (Client) | `http-mcp-sse` |
| SSL Profile (Client) | Select your self-signed client SSL profile |
| Source Address Translation | `Auto Map` |
| Default Pool | `pool-mcp-server` |
| Access Profile | `ap-mcp-gateway` |
| Per-Request Policy | `prp-mcp-gateway` |
| iRule | `irule-mcp-prm` |

---

## Step 8: Test the Full Flow

### 8.1 Test Protected Resource Metadata

```bash
curl -sk https://10.1.10.100/.well-known/oauth-protected-resource | python3 -m json.tool
```

### 8.2 Get a Token and Test MCP Access

```bash
# Get a token
TOKEN=$(curl -sk -X POST https://10.1.10.110/f5-oauth2/v1/token \
  -d 'grant_type=password&client_id=YOUR_CLIENT_ID&client_secret=YOUR_CLIENT_SECRET&username=mcp-agent&password=AgentPass2024!&scope=mcp:tools' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo "Token: $TOKEN"

# Test MCP endpoint with Bearer token
curl -sk -H "Authorization: Bearer $TOKEN" https://10.1.10.100/mcp/sse
```

### 8.3 Switch the Agent to BIG-IP Mode

Update `docker-compose.yml` on the GPU Server:

```yaml
agent:
  environment:
    - AUTH_MODE=bigip
    - MCP_SERVER_URL=https://10.1.10.100/mcp/sse
    - BIGIP_TOKEN_URL=https://10.1.10.110/f5-oauth2/v1/token
    - BIGIP_CLIENT_ID=YOUR_CLIENT_ID
    - BIGIP_CLIENT_SECRET=YOUR_CLIENT_SECRET
    - BIGIP_USERNAME=mcp-agent
    - BIGIP_PASSWORD=AgentPass2024!
    - BIGIP_GRANT_TYPE=password
    - OLLAMA_HOST=http://ollama:11434
    - OLLAMA_MODEL=llama3.1:8b
```

> **Note:** The agent code will need to be updated to use ROPC grant type
> (`grant_type=password`) instead of `client_credentials`, and to include
> `username` and `password` in the token request.

Restart and test:

```bash
docker compose up -d agent
docker compose run --rm agent python agent.py "List all employees in the cybersecurity department"
```

---

## Architecture Validation

When working end-to-end, the flow is:

```
1. Agent starts → no token
2. Agent POSTs to 10.1.10.110/f5-oauth2/v1/token with ROPC credentials
3. APM authenticates user against LocalDB
4. APM OAuth AS issues opaque access token
5. Agent connects to 10.1.10.100/mcp/sse with Bearer token
6. APM Per-Request Policy validates token via introspection
7. APM strips Bearer token, injects Basic Auth header
8. Request proxied to MCP server (10.1.20.50:8080)
9. MCP server calls Legacy HR API (10.1.20.60:5001) with Basic Auth
10. Results flow back: HR API → MCP Server → BIG-IP → Agent
11. Agent feeds results to Ollama for reasoning
```

---

## Troubleshooting

### Token request returns 302 redirect to /my.policy

The OAuth Profile is not linked to the Access Profile. Edit the access profile
properties (not the visual policy editor) and ensure the OAuth Profile field
is set to `mcp-oauth-as`.

### Token request returns "Unsupported value for field (grant_type)"

BIG-IP APM does not support `client_credentials`. Use `grant_type=password`
with the ROPC flow instead.

### LocalDB Auth fails with "authenticate with '' failed"

The OAuth Logon Page agent is missing from the access policy. Without it,
the username and password from the ROPC request are not extracted into session
variables. Add the OAuth Logon Page agent before the LocalDB Auth agent.

### OAuth Logon Page follows fallback instead of F5 ROPC branch

This is expected when BIG-IP is its own OAuth AS. The F5 ROPC branch checks
for provider type session variables that aren't populated in this configuration.
Route the fallback branch to LocalDB Auth — this is the correct path.

### SSE connections hang through BIG-IP

The default HTTP profile buffers responses. Use the custom `http-mcp-sse` profile
with Response Chunking set to Preserve and OneConnect Transformations disabled.

### Agent gets "SSL certificate verify failed"

The agent's `httpx` client rejects BIG-IP's self-signed cert. The agent code
uses `verify=False` for lab environments. For production, mount the CA cert
into the agent container.

---

## Demo Talking Points

1. **"The legacy app doesn't change."** The HR API has been running for years.
   Nobody wants to rewrite it. Nobody needs to. APM handles the translation.

2. **"OAuth for agents is the future."** The MCP spec mandates OAuth for
   remote servers. BIG-IP APM is already an OAuth AS — this is day-one
   capability, not a roadmap item.

3. **"Single pane of control."** APM gives you visibility into every agent
   request: who authenticated, what tokens were issued, what tools were
   called, and what data flowed.

4. **"Protocol translation is what APM was built for."** We've been doing
   SAML-to-Kerberos, OIDC-to-NTLM, cert-to-header for 15 years. OAuth-to-
   Basic-Auth for AI agents is just the latest translation.

5. **"Zero Trust for AI agents."** Every tool call is authenticated,
   authorized, and logged. The agent gets least-privilege scoped tokens.

6. **"Production-ready path."** In production, swap the local OAuth AS for
   Azure AD or Okta with `client_credentials` grant. BIG-IP becomes the
   resource server validating external tokens — no ROPC workaround needed.
   The MCP Gateway VIP and credential translation stay exactly the same.
