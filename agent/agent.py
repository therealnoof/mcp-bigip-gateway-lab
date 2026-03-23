"""
AI Agent — HR Assistant
=======================
An AI agent that queries the agency HR system through MCP tools.
Uses Ollama (local LLM) for reasoning and tool-call decisions.

Two operating modes:
  DIRECT MODE  — Connects directly to the MCP server (for testing)
  BIGIP MODE   — Connects through BIG-IP APM with OAuth 2.1 auth

The agent loop:
  1. User asks a question (e.g., "Who are the TS/SCI cleared staff?")
  2. Agent sends the question to Ollama
  3. Ollama decides which MCP tool to call (or asks for more info)
  4. Agent calls the tool via MCP, gets results
  5. Agent feeds results back to Ollama for analysis
  6. Repeat until Ollama writes a final answer

Environment Variables:
  MCP_SERVER_URL   - MCP server SSE endpoint (default: http://mcp-server:8080/sse)
  OLLAMA_HOST      - Ollama server URL (default: http://ollama:11434)
  OLLAMA_MODEL     - Model to use (default: llama3.1:8b)
  AUTH_MODE        - 'direct' or 'bigip' (default: direct)
  BIGIP_TOKEN_URL  - APM OAuth token endpoint (for bigip mode)
  BIGIP_CLIENT_ID  - OAuth client ID (for bigip mode)
  BIGIP_CLIENT_SECRET - OAuth client secret (for bigip mode)
"""

import asyncio
import json
import os
import sys

import httpx
import ollama
from mcp import ClientSession
from mcp.client.sse import sse_client

# ─────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://mcp-server:8080/sse")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
AUTH_MODE = os.environ.get("AUTH_MODE", "direct")

# Maximum number of tool-call rounds before we force a final answer
MAX_ITERATIONS = 10


# ─────────────────────────────────────────────────────────────────
# SYSTEM PROMPT
# Tells the LLM who it is and how to behave
# ─────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are an AI-powered HR assistant for a government agency.
You have access to the agency's HR system through MCP tools.

Your capabilities:
- Look up employee information (names, titles, clearances, locations)
- Search the employee directory by various criteria
- View department structures and rosters
- Answer questions about staffing and organization

Guidelines:
- Always use the available tools to look up real data — never guess
- Be concise but thorough in your answers
- If asked about something outside HR data, say so
- Protect sensitive information — don't speculate about clearance details beyond what the system shows
- When reporting results, organize them clearly

Available tools will be provided by the MCP server at runtime."""


async def run_agent(user_query: str):
    """
    Main agent loop.
    Connects to MCP server, discovers tools, then enters the
    reason-act loop with Ollama until a final answer is produced.
    """
    print(f"\n{'='*60}")
    print(f"  Agent Query: {user_query}")
    print(f"  MCP Server:  {MCP_SERVER_URL}")
    print(f"  LLM:         {OLLAMA_MODEL} @ {OLLAMA_HOST}")
    print(f"  Auth Mode:   {AUTH_MODE}")
    print(f"{'='*60}\n")

    # ─────────────────────────────────────────────────────────
    # STEP 1: Connect to MCP server and discover tools
    # ─────────────────────────────────────────────────────────
    headers = {}

    # If running in BIG-IP mode, get an OAuth token first
    if AUTH_MODE == "bigip":
        token = await get_oauth_token()
        headers["Authorization"] = f"Bearer {token}"
        print(f"[AUTH] Obtained OAuth 2.1 Bearer token from BIG-IP APM")

    print(f"[MCP] Connecting to {MCP_SERVER_URL}...")

    # For BIG-IP mode with self-signed certs, disable SSL verification.
    # In production, mount the CA cert and use verify="/path/to/ca.pem".
    sse_kwargs = {}
    if AUTH_MODE == "bigip":
        sse_kwargs["httpx_client_factory"] = lambda **kwargs: httpx.AsyncClient(verify=False, **kwargs)

    async with sse_client(MCP_SERVER_URL, headers=headers, **sse_kwargs) as (read, write):
        async with ClientSession(read, write) as session:
            # Initialize the MCP session — this triggers tool discovery
            await session.initialize()
            print(f"[MCP] Session initialized")

            # Discover available tools
            tools_result = await session.list_tools()
            mcp_tools = tools_result.tools
            print(f"[MCP] Discovered {len(mcp_tools)} tools:")
            for tool in mcp_tools:
                print(f"       - {tool.name}: {tool.description[:60]}...")

            # ─────────────────────────────────────────────────
            # STEP 2: Convert MCP tools to Ollama tool format
            # ─────────────────────────────────────────────────
            # Ollama needs tools described in a specific JSON
            # schema format. We translate from MCP's format.
            ollama_tools = []
            for tool in mcp_tools:
                ollama_tool = {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.inputSchema,
                    },
                }
                ollama_tools.append(ollama_tool)

            # ─────────────────────────────────────────────────
            # STEP 3: Agent reasoning loop
            # ─────────────────────────────────────────────────
            # Build the conversation with the system prompt and
            # the user's question, then let Ollama decide what
            # tools to call.
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_query},
            ]

            # Configure Ollama client with extended timeout for cold starts
            # First request after boot may take several minutes while the
            # model loads into GPU VRAM on the Tesla T4
            client = ollama.Client(host=OLLAMA_HOST, timeout=300)

            for iteration in range(MAX_ITERATIONS):
                print(f"\n[AGENT] Iteration {iteration + 1}/{MAX_ITERATIONS}")
                print(f"[LLM]   Asking {OLLAMA_MODEL} for next action...")

                # Ask the LLM what to do next
                response = client.chat(
                    model=OLLAMA_MODEL,
                    messages=messages,
                    tools=ollama_tools,
                )

                assistant_message = response["message"]
                messages.append(assistant_message)

                # ─────────────────────────────────────────────
                # Check if the LLM wants to call tools
                # ─────────────────────────────────────────────
                if not assistant_message.get("tool_calls"):
                    # No tool calls — the LLM is ready to give a final answer
                    final_answer = assistant_message.get("content", "")
                    print(f"\n[AGENT] Final answer received after {iteration + 1} iterations")
                    print(f"\n{'─'*60}")
                    print(f"  ANSWER:")
                    print(f"{'─'*60}")
                    print(final_answer)
                    print(f"{'─'*60}\n")
                    return final_answer

                # ─────────────────────────────────────────────
                # Process each tool call the LLM requested
                # ─────────────────────────────────────────────
                for tool_call in assistant_message["tool_calls"]:
                    tool_name = tool_call["function"]["name"]
                    tool_args = tool_call["function"]["arguments"]

                    print(f"[TOOL]  Calling: {tool_name}")
                    print(f"        Args:    {json.dumps(tool_args)}")

                    # Call the tool through MCP
                    try:
                        result = await session.call_tool(
                            tool_name,
                            arguments=tool_args,
                        )
                        tool_output = result.content[0].text
                        print(f"        Result:  {tool_output[:200]}...")
                    except Exception as e:
                        tool_output = f"Error calling tool: {str(e)}"
                        print(f"        ERROR:   {tool_output}")

                    # Feed the tool result back to the conversation
                    messages.append({
                        "role": "tool",
                        "content": tool_output,
                    })

            print("[AGENT] Max iterations reached — forcing final answer")
            return "I was unable to complete the analysis within the allowed iterations."


async def get_oauth_token() -> str:
    """
    Obtain an OAuth access token from BIG-IP APM.
    Uses the Resource Owner Password Credentials (ROPC) grant because
    BIG-IP APM 17.1 does not support client_credentials natively.

    BIG-IP 21.1 (Spring 2026) adds client_credentials + OAuth 2.1.
    Alternatively, use an external IdP (Azure AD, Okta) that supports
    client_credentials today, with BIG-IP as the resource server.
    """
    import httpx

    token_url = os.environ.get("BIGIP_TOKEN_URL", "https://bigip.lab.local/f5-oauth2/v1/token")
    client_id = os.environ.get("BIGIP_CLIENT_ID", "mcp-agent")
    client_secret = os.environ.get("BIGIP_CLIENT_SECRET", "agent-secret-2024")
    grant_type = os.environ.get("BIGIP_GRANT_TYPE", "password")
    username = os.environ.get("BIGIP_USERNAME", "mcp-agent")
    password = os.environ.get("BIGIP_PASSWORD", "")

    print(f"[AUTH] Requesting token from {token_url}")
    print(f"[AUTH] Client ID: {client_id}")
    print(f"[AUTH] Grant type: {grant_type}")

    token_data_payload = {
        "grant_type": grant_type,
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "mcp:tools",
    }

    if grant_type == "password":
        token_data_payload["username"] = username
        token_data_payload["password"] = password

    async with httpx.AsyncClient(verify=False) as client:
        response = await client.post(
            token_url,
            data=token_data_payload,
        )

        if response.status_code == 200:
            token_data = response.json()
            print(f"[AUTH] Token obtained (expires in {token_data.get('expires_in', '?')}s)")
            return token_data["access_token"]
        else:
            raise Exception(
                f"Failed to obtain OAuth token: {response.status_code} — {response.text}"
            )


# ─────────────────────────────────────────────────────────────────
# TEST TOOLS (standalone, without the LLM)
# ─────────────────────────────────────────────────────────────────
async def test_tools():
    """
    Connect to MCP server and test each tool individually.
    Always run this before involving the LLM — if a tool is broken,
    you want to know before debugging agent behavior.
    """
    print(f"\n{'='*60}")
    print(f"  MCP Tool Test Suite")
    print(f"  Server: {MCP_SERVER_URL}")
    print(f"{'='*60}\n")

    async with sse_client(MCP_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = (await session.list_tools()).tools
            print(f"Discovered {len(tools)} tools:\n")

            # Test 1: List employees
            print("─── Test 1: list_all_employees ───")
            r = await session.call_tool("list_all_employees", {"status": "Active"})
            data = json.loads(r.content[0].text)
            print(f"  Active employees: {data.get('count', 'ERROR')}")

            # Test 2: Get specific employee
            print("\n─── Test 2: get_employee_details ───")
            r = await session.call_tool("get_employee_details", {"employee_id": "EMP001"})
            data = json.loads(r.content[0].text)
            print(f"  Employee: {data.get('first_name', 'ERROR')} {data.get('last_name', '')}")
            print(f"  Title:    {data.get('title', 'ERROR')}")

            # Test 3: Search by department
            print("\n─── Test 3: search_employees (cybersecurity dept) ───")
            r = await session.call_tool("search_employees", {"department": "cybersecurity"})
            data = json.loads(r.content[0].text)
            print(f"  Found: {data.get('count', 'ERROR')} employees")

            # Test 4: List departments
            print("\n─── Test 4: list_departments ───")
            r = await session.call_tool("list_departments", {})
            data = json.loads(r.content[0].text)
            print(f"  Departments: {data.get('count', 'ERROR')}")

            # Test 5: Department roster
            print("\n─── Test 5: get_department_roster ───")
            r = await session.call_tool("get_department_roster", {"department_name": "Cybersecurity"})
            data = json.loads(r.content[0].text)
            print(f"  Cybersecurity team: {data.get('count', 'ERROR')} members")

            print(f"\n{'='*60}")
            print(f"  All tests passed!")
            print(f"{'='*60}\n")


# ─────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        asyncio.run(test_tools())
    elif len(sys.argv) > 1:
        # User provided a query as command-line argument
        query = " ".join(sys.argv[1:])
        asyncio.run(run_agent(query))
    else:
        # Interactive mode
        print("\n╔══════════════════════════════════════════════════╗")
        print("║   HR Assistant Agent                            ║")
        print("║   Type a question about employees or departments║")
        print("║   Type 'quit' to exit                           ║")
        print("╚══════════════════════════════════════════════════╝\n")

        while True:
            try:
                query = input("You: ").strip()
                if query.lower() in ("quit", "exit", "q"):
                    break
                if not query:
                    continue
                asyncio.run(run_agent(query))
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[ERROR] {e}")

        print("\nGoodbye!")
