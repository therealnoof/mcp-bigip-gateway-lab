"""
MCP Server — HR Tools
=====================
This MCP server wraps the Legacy HR REST API, exposing its
endpoints as MCP tools that an AI agent can discover and call.

In the demo architecture, this server sits BEHIND BIG-IP APM.
The agent never talks to this directly in production — all
requests come through BIG-IP which handles:
  1. OAuth 2.1 token validation
  2. Credential translation (Bearer → Basic Auth)
  3. MCP protocol proxying

For local development/testing, the agent CAN connect directly
to this server (no auth required on the MCP server itself —
that's BIG-IP's job).

Transport: Streamable HTTP (SSE) on port 8080
"""

import os
import httpx
import json
from typing import Optional
from mcp.server.fastmcp import FastMCP

# ─────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────

# Where the legacy HR API lives (behind us in the network)
HR_API_BASE = os.environ.get("HR_API_BASE", "http://legacy-hr-app:5001")

# Credentials for the legacy HR API (Basic Auth)
# In the BIG-IP demo, APM injects these. For standalone testing,
# the MCP server uses them directly.
HR_API_USER = os.environ.get("HR_API_USER", "hr_service")
HR_API_PASS = os.environ.get("HR_API_PASS", "legacy_pass_2024")

# Create the MCP server instance
# ─────────────────────────────────────────────────────────────────
# The name and description help the agent understand what this
# server provides. The agent sees this during tool discovery.
# ─────────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="hr-tools",
    host="0.0.0.0",
    port=int(os.environ.get("MCP_SERVER_PORT", 8080)),
    instructions=(
        "You are connected to the Agency HR System. "
        "Use these tools to look up employee information, "
        "search the directory, and view department structures. "
        "All data is from the agency's authoritative HR database."
    ),
)

# ─────────────────────────────────────────────────────────────────
# HELPER: Call the legacy HR API
# ─────────────────────────────────────────────────────────────────
async def call_hr_api(path: str, params: dict = None) -> dict:
    """
    Makes an authenticated request to the legacy HR REST API.
    Uses Basic Auth — this is the 'old' auth the legacy app needs.

    In the full BIG-IP demo, APM handles injecting these credentials.
    Here we do it directly for standalone testing.
    """
    async with httpx.AsyncClient() as client:
        url = f"{HR_API_BASE}{path}"
        response = await client.get(
            url,
            auth=(HR_API_USER, HR_API_PASS),  # Basic Auth
            params=params,
            timeout=10.0,
        )

        if response.status_code == 200:
            return response.json()
        else:
            return {
                "error": f"HR API returned {response.status_code}",
                "detail": response.text
            }


# ─────────────────────────────────────────────────────────────────
# MCP TOOLS
# Each tool maps to a legacy HR API endpoint. The agent discovers
# these at runtime via MCP's tools/list method.
# ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_all_employees(status: Optional[str] = "") -> str:
    """
    List all employees in the HR system.
    Optionally filter by status (e.g., 'Active', 'On Leave').
    Returns employee names, titles, departments, and locations.
    """
    params = {}
    if status:
        params["status"] = status

    result = await call_hr_api("/api/employees", params)
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_employee_details(employee_id: str) -> str:
    """
    Get detailed information about a specific employee by their ID.
    Employee IDs are in the format 'EMP001', 'EMP002', etc.
    Returns full profile: name, title, department, clearance level,
    hire date, manager, location, and status.
    """
    result = await call_hr_api(f"/api/employees/{employee_id}")
    return json.dumps(result, indent=2)


@mcp.tool()
async def search_employees(
    name: Optional[str] = "",
    department: Optional[str] = "",
    clearance: Optional[str] = "",
    location: Optional[str] = "",
) -> str:
    """
    Search the employee directory by one or more criteria.
    All filters are case-insensitive partial matches.

    Parameters:
      name        - Search by first or last name (e.g., 'chen', 'sarah')
      department  - Filter by department (e.g., 'cybersecurity', 'cloud')
      clearance   - Filter by clearance level (e.g., 'TS/SCI', 'secret')
      location    - Filter by work location (e.g., 'arlington', 'fort meade')
    """
    params = {}
    if name:
        # The HR API matches name against first_name and last_name separately,
        # so "sarah chen" won't match. Use only the last word (likely last name)
        # for more reliable matching when the LLM passes a full name.
        name_parts = name.strip().split()
        params["name"] = name_parts[-1] if name_parts else name
    if department:
        params["department"] = department
    if clearance:
        params["clearance"] = clearance
    if location:
        params["location"] = location

    result = await call_hr_api("/api/employees/search", params)
    return json.dumps(result, indent=2)


@mcp.tool()
async def list_departments() -> str:
    """
    List all departments in the agency with headcount,
    department head, and primary location.
    """
    result = await call_hr_api("/api/departments")
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_department_roster(department_name: str) -> str:
    """
    Get all employees in a specific department.
    Use the exact department name (e.g., 'Cybersecurity',
    'Network Operations', 'Cloud Engineering',
    'Identity & Access Management', 'Human Resources').
    """
    result = await call_hr_api(f"/api/departments/{department_name}/employees")
    return json.dumps(result, indent=2)


# ─────────────────────────────────────────────────────────────────
# RUN THE MCP SERVER
# Uses SSE transport so agents can connect over HTTP.
# In the BIG-IP demo, this listens on the internal network and
# BIG-IP's VIP is the agent-facing endpoint.
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"╔══════════════════════════════════════════════════╗")
    print(f"║   MCP Server — HR Tools                         ║")
    print(f"║   Transport: SSE (HTTP)                         ║")
    print(f"║   Port: {os.environ.get('MCP_SERVER_PORT', 8080)}                                   ║")
    print(f"║   Backend: {HR_API_BASE}             ║")
    print(f"║   Tools: 5 (list, get, search, depts, roster)   ║")
    print(f"╚══════════════════════════════════════════════════╝")
    mcp.run(transport="sse")
