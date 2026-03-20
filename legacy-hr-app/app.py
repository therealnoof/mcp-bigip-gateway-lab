"""
Legacy HR REST API
==================
Simulates an old enterprise HR system that only speaks Basic Auth.
This is the kind of app you find in every government agency and
Fortune 500 — works fine, nobody wants to rewrite it, but it can't
do modern auth (OAuth, SAML, etc.).

BIG-IP APM will sit in front of this and translate OAuth 2.1 Bearer
tokens into the Basic Auth credentials this app expects.

Endpoints:
  GET  /api/employees          - List all employees
  GET  /api/employees/<id>     - Get employee by ID
  GET  /api/employees/search   - Search by name or department
  GET  /api/departments        - List departments
  GET  /api/health             - Health check (no auth required)
"""

from flask import Flask, jsonify, request, abort
from functools import wraps
import base64
import os

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────
# CONFIGURATION
# The Basic Auth credentials this legacy app accepts.
# In a real environment, this might be an LDAP bind or a hardcoded
# service account — the point is it's NOT modern auth.
# ─────────────────────────────────────────────────────────────────
VALID_USERNAME = os.environ.get("HR_API_USER", "hr_service")
VALID_PASSWORD = os.environ.get("HR_API_PASS", "legacy_pass_2024")

# ─────────────────────────────────────────────────────────────────
# SAMPLE DATA
# Realistic enough for a demo. Mix of departments, clearance levels,
# and locations that resonate with federal/DoD customers.
# ─────────────────────────────────────────────────────────────────
EMPLOYEES = [
    {
        "id": "EMP001",
        "first_name": "Sarah",
        "last_name": "Chen",
        "email": "sarah.chen@agency.gov",
        "department": "Cybersecurity",
        "title": "Senior Security Engineer",
        "location": "Arlington, VA",
        "clearance": "TS/SCI",
        "hire_date": "2019-03-15",
        "manager_id": "EMP005",
        "status": "Active",
    },
    {
        "id": "EMP002",
        "first_name": "Marcus",
        "last_name": "Johnson",
        "email": "marcus.johnson@agency.gov",
        "department": "Network Operations",
        "title": "Network Administrator",
        "location": "Fort Meade, MD",
        "clearance": "Secret",
        "hire_date": "2021-07-01",
        "manager_id": "EMP005",
        "status": "Active",
    },
    {
        "id": "EMP003",
        "first_name": "Aisha",
        "last_name": "Patel",
        "email": "aisha.patel@agency.gov",
        "department": "Cloud Engineering",
        "title": "Cloud Architect",
        "location": "Reston, VA",
        "clearance": "TS/SCI",
        "hire_date": "2020-11-10",
        "manager_id": "EMP006",
        "status": "Active",
    },
    {
        "id": "EMP004",
        "first_name": "David",
        "last_name": "Kim",
        "email": "david.kim@agency.gov",
        "department": "Cybersecurity",
        "title": "SOC Analyst II",
        "location": "Arlington, VA",
        "clearance": "Secret",
        "hire_date": "2022-01-20",
        "manager_id": "EMP001",
        "status": "Active",
    },
    {
        "id": "EMP005",
        "first_name": "Rachel",
        "last_name": "Torres",
        "email": "rachel.torres@agency.gov",
        "department": "Cybersecurity",
        "title": "CISO",
        "location": "Arlington, VA",
        "clearance": "TS/SCI",
        "hire_date": "2017-06-01",
        "manager_id": None,
        "status": "Active",
    },
    {
        "id": "EMP006",
        "first_name": "James",
        "last_name": "Mitchell",
        "email": "james.mitchell@agency.gov",
        "department": "Cloud Engineering",
        "title": "Director of Cloud Services",
        "location": "Reston, VA",
        "clearance": "TS/SCI",
        "hire_date": "2018-02-14",
        "manager_id": None,
        "status": "Active",
    },
    {
        "id": "EMP007",
        "first_name": "Lisa",
        "last_name": "Nakamura",
        "email": "lisa.nakamura@agency.gov",
        "department": "Network Operations",
        "title": "Senior Network Engineer",
        "location": "Fort Meade, MD",
        "clearance": "TS/SCI",
        "hire_date": "2019-09-30",
        "manager_id": "EMP005",
        "status": "Active",
    },
    {
        "id": "EMP008",
        "first_name": "Robert",
        "last_name": "Okonkwo",
        "email": "robert.okonkwo@agency.gov",
        "department": "Identity & Access Management",
        "title": "IAM Engineer",
        "location": "Arlington, VA",
        "clearance": "Secret",
        "hire_date": "2023-04-10",
        "manager_id": "EMP001",
        "status": "Active",
    },
    {
        "id": "EMP009",
        "first_name": "Maria",
        "last_name": "Gonzalez",
        "email": "maria.gonzalez@agency.gov",
        "department": "Human Resources",
        "title": "HR Director",
        "location": "Washington, DC",
        "clearance": "Secret",
        "hire_date": "2016-08-22",
        "manager_id": None,
        "status": "Active",
    },
    {
        "id": "EMP010",
        "first_name": "Alex",
        "last_name": "Petrov",
        "email": "alex.petrov@agency.gov",
        "department": "Cybersecurity",
        "title": "Penetration Tester",
        "location": "Arlington, VA",
        "clearance": "TS/SCI",
        "hire_date": "2021-12-05",
        "manager_id": "EMP001",
        "status": "On Leave",
    },
]

DEPARTMENTS = [
    {"name": "Cybersecurity", "head_id": "EMP005", "headcount": 4, "location": "Arlington, VA"},
    {"name": "Network Operations", "head_id": "EMP005", "headcount": 2, "location": "Fort Meade, MD"},
    {"name": "Cloud Engineering", "head_id": "EMP006", "headcount": 2, "location": "Reston, VA"},
    {"name": "Identity & Access Management", "head_id": "EMP001", "headcount": 1, "location": "Arlington, VA"},
    {"name": "Human Resources", "head_id": "EMP009", "headcount": 1, "location": "Washington, DC"},
]


# ─────────────────────────────────────────────────────────────────
# BASIC AUTH DECORATOR
# This is the "legacy" auth mechanism. The whole point of the demo
# is that BIG-IP APM translates modern OAuth into this.
# ─────────────────────────────────────────────────────────────────
def require_basic_auth(f):
    """
    Decorator that enforces HTTP Basic Authentication.
    Returns 401 with WWW-Authenticate header if credentials are
    missing or invalid — standard Basic Auth behavior.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization

        # Check if Basic Auth header is present and valid
        if not auth or not auth.username or not auth.password:
            return jsonify({"error": "Authentication required"}), 401, {
                "WWW-Authenticate": 'Basic realm="Legacy HR System"'
            }

        if auth.username != VALID_USERNAME or auth.password != VALID_PASSWORD:
            return jsonify({"error": "Invalid credentials"}), 403

        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────────────────────────
# API ENDPOINTS
# ─────────────────────────────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health_check():
    """Health check endpoint — no auth required."""
    return jsonify({
        "status": "healthy",
        "service": "Legacy HR REST API",
        "version": "1.0.0",
        "auth_type": "Basic",
        "note": "This system does NOT support OAuth, SAML, or OIDC."
    })


@app.route("/api/employees", methods=["GET"])
@require_basic_auth
def list_employees():
    """Return all employees. Supports ?status= filter."""
    status_filter = request.args.get("status")
    results = EMPLOYEES

    if status_filter:
        results = [e for e in results if e["status"].lower() == status_filter.lower()]

    return jsonify({
        "count": len(results),
        "employees": results
    })


@app.route("/api/employees/<employee_id>", methods=["GET"])
@require_basic_auth
def get_employee(employee_id):
    """Return a single employee by ID."""
    employee = next((e for e in EMPLOYEES if e["id"] == employee_id), None)

    if not employee:
        return jsonify({"error": f"Employee {employee_id} not found"}), 404

    return jsonify(employee)


@app.route("/api/employees/search", methods=["GET"])
@require_basic_auth
def search_employees():
    """
    Search employees by name, department, or clearance level.
    Query params: ?name=, ?department=, ?clearance=, ?location=
    """
    name = request.args.get("name", "").lower()
    dept = request.args.get("department", "").lower()
    clearance = request.args.get("clearance", "").lower()
    location = request.args.get("location", "").lower()

    results = EMPLOYEES

    if name:
        results = [e for e in results
                   if name in e["first_name"].lower()
                   or name in e["last_name"].lower()]

    if dept:
        results = [e for e in results
                   if dept in e["department"].lower()]

    if clearance:
        results = [e for e in results
                   if clearance in e["clearance"].lower()]

    if location:
        results = [e for e in results
                   if location in e["location"].lower()]

    return jsonify({
        "query": {
            "name": name or None,
            "department": dept or None,
            "clearance": clearance or None,
            "location": location or None,
        },
        "count": len(results),
        "employees": results
    })


@app.route("/api/departments", methods=["GET"])
@require_basic_auth
def list_departments():
    """Return all departments with headcount."""
    return jsonify({
        "count": len(DEPARTMENTS),
        "departments": DEPARTMENTS
    })


@app.route("/api/departments/<dept_name>/employees", methods=["GET"])
@require_basic_auth
def get_department_employees(dept_name):
    """Return all employees in a specific department."""
    employees = [e for e in EMPLOYEES
                 if e["department"].lower() == dept_name.lower()]

    if not employees:
        return jsonify({"error": f"No employees found in department '{dept_name}'"}), 404

    return jsonify({
        "department": dept_name,
        "count": len(employees),
        "employees": employees
    })


# ─────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("HR_API_PORT", 5001))
    print(f"╔══════════════════════════════════════════════════╗")
    print(f"║   Legacy HR REST API                            ║")
    print(f"║   Auth: Basic (user: {VALID_USERNAME})")
    print(f"║   Port: {port}                                   ║")
    print(f"║   NOTE: No OAuth/SAML/OIDC support              ║")
    print(f"╚══════════════════════════════════════════════════╝")
    app.run(host="0.0.0.0", port=port, debug=False)
