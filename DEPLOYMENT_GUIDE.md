# Deployment Guide — BIG-IP APM as MCP Gateway

> **Complete all steps BEFORE attempting the BIG-IP APM configuration.**
> The standalone components (HR App, MCP Server, Agent, Ollama) must be
> verified working before introducing BIG-IP into the traffic path.

---

## Hardware Requirements

### Three-VM Lab Environment

| VM | Role | CPU | RAM | Disk | GPU | OS |
|---|---|---|---|---|---|---|
| GPU Server | Agent + Ollama + MCP Server | 8 cores | 32 GB | 60 GB free | NVIDIA T4 (16GB VRAM) | Ubuntu 22.04 LTS |
| HR App VM | Legacy HR REST API | 2 cores | 4 GB | 20 GB free | None | Ubuntu 22.04 LTS |
| BIG-IP VE | APM Gateway + OAuth AS | 4 cores | 16 GB | 40 GB | None | BIG-IP 17.1+ |

> **Why T4?** The llama3.1:8b model requires ~6GB VRAM. A T4 (16GB) runs it
> comfortably with room for KV cache during longer agent conversations. Without
> GPU, Ollama falls back to CPU — expect 30-60 second response times per
> reasoning step instead of 2-5 seconds.

### GPU VRAM Usage by Model

| Model | VRAM Required | T4 (16GB) | Notes |
|---|---|---|---|
| llama3.1:8b | ~6 GB | ✅ Comfortable | **Recommended for this lab** |
| llama3.2:3b | ~3 GB | ✅ Very fast | Lighter reasoning, faster demo |
| phi3:mini | ~3 GB | ✅ Very fast | Good alternative for quick demos |
| llama3.1:70b | ~42 GB | ❌ Too large | Needs multi-GPU setup |

To use a different model, change the `OLLAMA_MODEL` environment variable in
`docker-compose.yml` and ensure the model-puller service pulls the correct model.

### Network Requirements

| Subnet | CIDR | Purpose |
|---|---|---|
| External | 10.1.10.0/24 | Agent-facing, BIG-IP VIPs |
| Internal | 10.1.20.0/24 | Backend services (MCP Server, HR App) |

### IP Addressing

| Host | External (10.1.10.0/24) | Internal (10.1.20.0/24) |
|---|---|---|
| BIG-IP VE | Self: 10.1.10.10 | Self: 10.1.20.100 |
| MCP Gateway VIP | 10.1.10.100:443 | — |
| OAuth AS VIP | 10.1.10.110:443 | — |
| GPU Server | 10.1.10.60 | 10.1.20.50 |
| HR App VM | 10.1.10.50 | 10.1.20.60 |

> **GPU Server has two NICs.** The external NIC (10.1.10.60) is where the agent
> reaches the BIG-IP VIPs. The internal NIC (10.1.20.50) is where BIG-IP pools
> to the MCP server. Both are required.

### Manual Interface Configuration

In some environments (e.g., AWS EC2 with multiple ENIs), secondary interfaces
may come up without IP addresses assigned. If `ip addr show` reveals interfaces
in a `DOWN` state or missing IPv4 addresses, assign them manually.

**GPU Server (10.1.1.5):**

```bash
# Identify unconfigured interfaces
ip -br addr show

# Assign external IP to the appropriate interface (e.g., ens7)
ip addr add 10.1.10.60/24 dev ens7
ip link set ens7 up

# Assign internal IP (e.g., ens6)
ip addr add 10.1.20.50/24 dev ens6
ip link set ens6 up
```

**HR App VM:**

```bash
# Identify unconfigured interfaces
ip -br addr show

# Assign external IP (e.g., ens6)
ip addr add 10.1.10.50/24 dev ens6
ip link set ens6 up

# Assign internal IP (e.g., ens7)
ip addr add 10.1.20.60/24 dev ens7
ip link set ens7 up
```

Verify connectivity between the two VMs:

```bash
# From GPU Server
ping -c 3 10.1.20.60

# From HR App VM
ping -c 3 10.1.20.50
```

> **Note:** These assignments are non-persistent and will be lost on reboot.
> To make them permanent, configure them in `/etc/netplan/` (Ubuntu) or
> `/etc/network/interfaces` (Debian).

---

## Step 1: System Updates (Both GPU Server and HR App VM)

Run on both Ubuntu VMs:

```bash
sudo apt-get update && sudo apt-get upgrade -y
```

---

## Step 2: Install Core Utilities (Both VMs)

```bash
sudo apt-get install -y \
    curl \
    wget \
    git \
    ca-certificates \
    gnupg \
    lsb-release \
    software-properties-common \
    apt-transport-https \
    unzip \
    jq
```

---

## Step 3: Install NVIDIA Drivers (GPU Server Only)

Check if drivers are already installed:

```bash
nvidia-smi
```

If you see a GPU status table, skip to Step 4. If you get `command not found`:

```bash
sudo apt-get install -y ubuntu-drivers-common
sudo ubuntu-drivers autoinstall
sudo reboot
```

After reboot, verify:

```bash
nvidia-smi
```

Expected output includes `Tesla T4` and a CUDA version.

---

## Step 4: Install Docker Engine (Both VMs)

```bash
# Remove old Docker versions
sudo apt-get remove -y docker docker-engine docker.io containerd runc 2>/dev/null

# Add Docker's official GPG key
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Add Docker repository
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker Engine and Compose plugin
sudo apt-get update
sudo apt-get install -y \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin

# Start and enable
sudo systemctl start docker
sudo systemctl enable docker

# Allow your user to run Docker without sudo
sudo usermod -aG docker $USER
newgrp docker
```

Verify:

```bash
docker run hello-world
docker compose version
```

---

## Step 5: Install NVIDIA Container Toolkit (GPU Server Only)

Without this, Ollama won't see the T4 and falls back to CPU.

```bash
# Add NVIDIA Container Toolkit repository
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

# Configure Docker to use NVIDIA runtime
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Verify GPU passthrough works inside Docker:

```bash
docker run --rm --gpus all nvidia/cuda:12.3.0-base-ubuntu22.04 nvidia-smi
```

You should see the same T4 GPU table, but running inside a container.

---

## Step 6: Deploy the Legacy HR App (HR App VM — 10.1.20.60)

```bash
# Clone the repo (or scp the legacy-hr-app/ folder)
git clone https://github.com/therealnoof/mcp-bigip-gateway-lab.git
cd mcp-bigip-gateway-lab/legacy-hr-app

# Build the image
docker build -t legacy-hr-app .

# Run the container
docker run -d --restart unless-stopped \
    --name legacy-hr-app \
    -p 5001:5001 \
    -e HR_API_USER=hr_service \
    -e HR_API_PASS=legacy_pass_2024 \
    legacy-hr-app
```

Verify:

```bash
# Health check (no auth required)
curl http://localhost:5001/api/health | jq .

# Authenticated request
curl -u hr_service:legacy_pass_2024 http://localhost:5001/api/employees | jq .count
# Expected: 10

# Verify it rejects bad credentials
curl -u wrong:creds http://localhost:5001/api/employees
# Expected: 403
```

---

## Step 7: Deploy the GPU Server Stack (GPU Server — 10.1.10.60 / 10.1.20.50)

> **Note:** The GPU Server compose file runs the MCP Server, Ollama, and Agent
> only. The Legacy HR App runs separately on the HR App VM (Step 6). The MCP
> Server connects to the HR App over the internal network (10.1.20.60:5001).

```bash
# Clone the repo
git clone https://github.com/therealnoof/mcp-bigip-gateway-lab.git
cd mcp-bigip-gateway-lab

# Build and start services
docker compose up -d --build

# Watch the model download (~4.7GB, takes 2-5 min)
docker compose logs -f model-puller
# Wait for: "Model pull complete!"
```

> **GPU passthrough** is enabled by default in `docker-compose.yml`. If you do
> NOT have an NVIDIA GPU, comment out the `deploy.resources` section under the
> `ollama` service or `docker compose up` will fail.

Verify the HR App is reachable from the GPU Server before proceeding:

```bash
curl -u hr_service:legacy_pass_2024 http://10.1.20.60:5001/api/health | jq .
```

If this fails, check that the internal interface (10.1.20.50) is configured
on the GPU Server — see [Manual Interface Configuration](#manual-interface-configuration) above.

---

## Step 8: Test MCP Tools (Direct Mode — No BIG-IP)

This verifies the full chain: Agent → MCP Server → Legacy HR API.
Always test tools in isolation BEFORE involving the LLM.

```bash
docker compose run --rm agent python agent.py test
```

Expected output:

```
══════════════════════════════════════════════════════════════
  MCP Tool Test Suite
  Server: http://mcp-server:8080/sse
══════════════════════════════════════════════════════════════

Discovered 5 tools:

─── Test 1: list_all_employees ───
  Active employees: 9
─── Test 2: get_employee_details ───
  Employee: Sarah Chen
  Title:    Senior Security Engineer
─── Test 3: search_employees (cybersecurity dept) ───
  Found: 4 employees
─── Test 4: list_departments ───
  Departments: 5
─── Test 5: get_department_roster ───
  Cybersecurity team: 4 members

══════════════════════════════════════════════════════════════
  All tests passed!
══════════════════════════════════════════════════════════════
```

**Do not proceed until all 5 tests pass.**

---

## Step 9: Test the Agent with Ollama (Direct Mode)

```bash
docker compose run --rm agent python agent.py "Who has TS/SCI clearance in the cybersecurity department?"
```

The agent should:
1. Connect to the MCP server
2. Discover 5 tools
3. Call `search_employees` with cybersecurity + TS/SCI parameters
4. Feed results to Ollama
5. Return a structured answer listing the matching employees

If Ollama is slow (>30 seconds per iteration), verify GPU passthrough:

```bash
docker compose exec ollama nvidia-smi
```

If no GPU is visible, redo Step 5 and ensure the `deploy.resources` section
is uncommented in `docker-compose.yml` under the `ollama` service.

---

## Step 10: Configure BIG-IP APM

At this point, all three application components are verified working.
Proceed to the [APM Configuration Guide](docs/APM-CONFIGURATION-GUIDE.md)
for the full BIG-IP setup.

Build order:
1. Pool + HTTP profile (Step 1-2 in APM guide)
2. SSL profile (Step 3)
3. OAuth AS VIP — test token issuance (Step 4)
4. MCP Gateway VIP — test PRM + token validation (Step 5)
5. Switch agent to BIG-IP mode (Step 6)

---

## Step 11: Switch Agent to BIG-IP Mode

Once the APM configuration is complete and verified, update the agent:

Edit `docker-compose.yml` on the GPU Server:

```yaml
agent:
  environment:
    - AUTH_MODE=bigip
    - MCP_SERVER_URL=https://10.1.10.100/mcp/sse
    - BIGIP_TOKEN_URL=https://10.1.10.110/f5-oauth2/v1/token
    - BIGIP_CLIENT_ID=mcp-agent
    - BIGIP_CLIENT_SECRET=agent-secret-2024
    - OLLAMA_HOST=http://ollama:11434
    - OLLAMA_MODEL=llama3.1:8b
```

Restart and test:

```bash
docker compose up -d agent
docker compose run --rm agent python agent.py "List all employees in the cybersecurity department"
```

---

## Pre-Demo Verification Checklist

Run this before any demo or walkthrough. All checks must pass.

```bash
echo "=== 1. NVIDIA Driver ==="
nvidia-smi | head -5
echo ""
echo "=== 2. Docker ==="
docker --version
echo ""
echo "=== 3. Docker Compose ==="
docker compose version
echo ""
echo "=== 4. GPU in Docker ==="
docker run --rm --gpus all nvidia/cuda:12.3.0-base-ubuntu22.04 nvidia-smi | head -5
echo ""
echo "=== 5. Ollama Model ==="
docker compose exec ollama ollama list
echo ""
echo "=== 6. HR App (10.1.20.60) ==="
curl -s http://10.1.20.60:5001/api/health | jq .status
echo ""
echo "=== 7. MCP Server ==="
docker compose ps mcp-server --format "{{.Status}}"
echo ""
echo "=== 8. Disk Space ==="
df -h / | tail -1
echo ""
echo "=== 9. BIG-IP PRM Discovery ==="
curl -sk https://10.1.10.100/.well-known/oauth-protected-resource | jq .
echo ""
echo "=== 10. BIG-IP OAuth Token ==="
curl -sk https://10.1.10.110/f5-oauth2/v1/token \
  -d "grant_type=client_credentials" \
  -d "client_id=mcp-agent" \
  -d "client_secret=agent-secret-2024" \
  -d "scope=mcp:tools" | jq .token_type
```

| Check | Expected | If Failing |
|---|---|---|
| 1. nvidia-smi | Tesla T4 visible | Redo Step 3 |
| 2. Docker | Version 24+ | Redo Step 4 |
| 3. Docker Compose | Version 2.x | Redo Step 4 |
| 4. GPU in Docker | T4 visible in container | Redo Step 5, restart Docker |
| 5. Ollama model | llama3.1:8b listed | `docker compose exec ollama ollama pull llama3.1:8b` |
| 6. HR App health | `"healthy"` | Check container on 10.1.20.60 |
| 7. MCP Server | `Up` or `running` | `docker compose up -d mcp-server` |
| 8. Disk space | >10GB free | `docker system prune` |
| 9. PRM discovery | JSON with authorization_servers | Check BIG-IP iRule + VIP |
| 10. OAuth token | `"Bearer"` | Check APM OAuth profile + client registration |

---

## Known Fixes and Lessons Learned

These issues were discovered during development and testing. They are already
fixed in the current codebase but documented here for troubleshooting.

### 1. FastMCP `.run()` API — host/port as Constructor Args

**Problem:** Calling `mcp.run(host="0.0.0.0", port=8080)` fails on some MCP SDK
versions that expect `host` and `port` as `FastMCP()` constructor arguments.

**Fix (already applied):** The current `server.py` passes `host` and `port` to
the `FastMCP()` constructor, and only `transport="sse"` to `mcp.run()`:
```python
mcp = FastMCP(name="hr-tools", host="0.0.0.0", port=8080)
mcp.run(transport="sse")
```

### 2. Model-Puller Entrypoint Override

**Problem:** The Ollama Docker image sets its entrypoint to `ollama`, so passing
a shell command (`sh -c "..."`) results in `ollama sh -c "..."` instead of the
intended shell execution.

**Fix:** The `model-puller` service in `docker-compose.yml` uses `entrypoint: sh -c`
to override the Ollama image's default entrypoint.

### 3. SSE Responses Buffered Through BIG-IP

**Problem:** BIG-IP's default HTTP profile may buffer Server-Sent Events (SSE)
responses, causing the agent to hang waiting for MCP tool results.

**Fix:** Create a custom HTTP profile (`http-mcp-sse`) with:
- Response Chunking: `Preserve`
- OneConnect Transformations: `Disabled`

Also remove any response compression profiles from the MCP Gateway VIP.
If issues persist, disable the OneConnect profile entirely on the VIP.

### 4. Agent SSL Verification Fails Against BIG-IP Self-Signed Cert

**Problem:** The agent's `httpx` client rejects the BIG-IP self-signed SSL
certificate when requesting OAuth tokens.

**Fix:** The `get_oauth_token()` function in `agent.py` uses `verify=False`
for lab environments. For production, mount the CA cert into the agent container
and set `verify="/path/to/ca.pem"`.

### 5. LLM Describes Tools Instead of Calling Them

**Problem:** `llama3.1:8b` sometimes writes out tool call JSON in its text
response instead of using the Ollama tool-calling API. The agent sees no
`tool_calls` in the response and stops with an empty answer.

**Fix:** The system prompt in `agent.py` explicitly instructs the model to use
the tool-calling mechanism. If this still occurs, try:
- Adding "You MUST use the provided tool functions. Do NOT write JSON manually."
  to the system prompt
- Switching to `llama3.1:13b` which has better tool-calling compliance
- Using a smaller `temperature` value (0.1) in the Ollama chat call

### 6. MCP Server Can't Reach HR App Across Subnets

**Problem:** The MCP server container on the GPU Server (10.1.20.50) can't
reach the HR App VM (10.1.20.60) even though they're on the same subnet.

**Fix:** Docker's default bridge network isolates containers from the host's
secondary NIC. Either:
- Use `network_mode: host` on the mcp-server service (simplest for lab)
- Or add the internal network as an extra Docker network with the host NIC's IP

### 7. BIG-IP APM Per-Request Policy — OAuth Scope Check Requires JWKS

**Problem:** The OAuth Scope Check action in the per-request policy fails with
"unable to verify token" even with a valid JWT from the APM OAuth AS.

**Fix:** When using Local validation mode, the MCP Gateway VIP needs access to
the OAuth AS's JWKS endpoint. Ensure the JWKS URI (`https://10.1.10.110/f5-oauth2/v1/jwks`)
is reachable from the BIG-IP itself (loopback to its own VIP). If this causes
issues, switch to Remote validation mode which uses token introspection instead.

### 8. Docker Compose `depends_on` Doesn't Wait for Ollama Model

**Problem:** The agent container starts before Ollama has finished pulling the
model. The agent crashes because the model isn't available yet.

**Fix:** The `agent.py` startup logic should poll Ollama for model availability
before entering the agent loop. The current codebase handles this in the
interactive mode, but if running one-shot queries immediately after `docker compose up`,
wait for the model-puller to complete first:
```bash
docker compose logs -f model-puller
# Wait for "Model pull complete!" before running agent queries
```

---

## Post-Lab Teardown

### GPU Server

```bash
cd mcp-bigip-gateway-lab
docker compose down
docker volume rm mcp-bigip-gateway-lab_ollama_data
docker compose down --rmi local
```

### HR App VM

```bash
docker stop legacy-hr-app && docker rm legacy-hr-app
docker rmi legacy-hr-app
```

### BIG-IP

Remove in reverse order:
1. Delete Virtual Servers (vs-mcp-gateway, vs-oauth-as)
2. Delete Access Profiles (ap-mcp-gateway, ap-oauth-as)
3. Delete Per-Request Policy (prp-mcp-gateway)
4. Delete OAuth Profile, Client Application, and Scope
5. Delete Pool (pool-mcp-server)
6. Delete iRule (irule-mcp-prm)
7. Delete HTTP Profile (http-mcp-sse)
8. Delete SSL Profile (clientssl-mcp-lab)

### Full Cleanup (removes ALL Docker data on the VMs)

```bash
docker system prune -a --volumes -f
```

> **Warning:** This removes ALL unused Docker data, not just lab resources.
> Only run on dedicated lab machines.
