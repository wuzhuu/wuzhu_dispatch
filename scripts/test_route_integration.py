#!/usr/bin/env python3
"""
Route-level FastAPI integration tests for wuzhu-dispatch.

Uses httpx.AsyncClient so tests stay in the same async event loop
as the database engine (required for SQLite + aiosqlite).
"""

import sys
import os
import json
import tempfile
from contextlib import asynccontextmanager

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "dispatcher"))

_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db_path}"
os.environ["DISPATCH_SERVER_SECRET"] = "test-secret-12345"
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["CORS_ALLOWED_ORIGINS"] = "https://admin.dispatch.example.com"
os.environ["CSRF_ALLOWED_ORIGINS"] = "https://admin.dispatch.example.com"
os.environ["REGISTRATION_TOKEN"] = "reg-token-test"

# Must import models BEFORE create_app to register all tables
from app.models import (  # noqa: F401
    User, Session, ClientApiToken, ComputeNode, ComputeNodeStatus,
    Task, TaskLog, Artifact, AuditLog,
)
from app.database import engine, Base, async_session_factory
from app.config import settings
from app.auth import hash_password, sha256_hash
from app.services.node_service import _hash_token

import httpx

total = passed = 0


def check(desc: str, cond: bool):
    global total, passed
    total += 1
    if cond:
        passed += 1
        print(f"  ✅ {desc}")
    else:
        print(f"  ❌ {desc}")


async def seed_db():
    """Create tables and seed initial data."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session_factory() as db:
        # Users
        db.add_all([
            User(user_id="owner-rt", username="owner",
                 password_hash=hash_password("pass"), role="owner", enabled=True),
            User(user_id="admin-rt", username="admin",
                 password_hash=hash_password("pass"), role="admin", enabled=True),
            User(user_id="oper-rt", username="operator",
                 password_hash=hash_password("pass"), role="operator", enabled=True),
            User(user_id="viewer-rt", username="viewer",
                 password_hash=hash_password("pass"), role="viewer", enabled=True),
        ])
        await db.flush()

        # Compute nodes
        db.add_all([
            ComputeNode(node_id="node-a", name="Node A", region="HK",
                        tags=["hk", "cn_reachable"],
                        static_profile={"runtime": {"shell": True}, "limits": {"max_parallel_tasks": 2}},
                        agent_token_hash=_hash_token("token-a"), enabled=True),
            ComputeNode(node_id="node-b", name="Node B", region="US",
                        tags=["us", "foreign_reachable"],
                        static_profile={"runtime": {"shell": True}, "limits": {"max_parallel_tasks": 2}},
                        agent_token_hash=_hash_token("token-b"), enabled=True),
            ComputeNode(node_id="node-disabled", name="Disabled",
                        static_profile={"runtime": {"shell": True}, "limits": {"max_parallel_tasks": 1}},
                        agent_token_hash=_hash_token("token-dis"), enabled=False),
        ])
        await db.flush()

        # Client API tokens
        for uid, uname in [("viewer-rt", "viewer"), ("oper-rt", "operator"),
                           ("admin-rt", "admin"), ("owner-rt", "owner")]:
            db.add(ClientApiToken(
                token_id=f"tok-{uname}",
                token_hash=sha256_hash(f"token-{uname}"),
                user_id=uid, scope=["task:create"], enabled=True,
            ))
        await db.commit()


async def run_tests():
    global total, passed
    await seed_db()

    from app.main import create_app
    app = create_app()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:

        print("=" * 60)
        print("Route-Level Integration Tests")
        print("=" * 60)

        # ═══════════════════════════════════════════════════════════
        # 1. Unauthenticated → 401
        # ═══════════════════════════════════════════════════════════
        r = await cli.get("/api/v1/admin/nodes")
        check("1. Unauthenticated /admin/nodes → 401", r.status_code == 401)

        r = await cli.post("/api/v1/client/tasks",
                           json={"type": "t", "payload": {"execution": {"mode": "shell", "command": "echo"}}})
        check("1b. No-auth create task → 401", r.status_code == 401)

        # ═══════════════════════════════════════════════════════════
        # 2-6. Role-based task creation
        # ═══════════════════════════════════════════════════════════
        admin_bearer = {"Authorization": "Bearer test-secret-12345"}

        # System token creates shell task
        r = await cli.post("/api/v1/client/tasks",
                           json={"type": "sys-shell", "priority": 50,
                                 "payload": {"execution": {"mode": "shell", "command": "echo"}}},
                           headers=admin_bearer)
        check("2. System token creates shell task -> 2xx", r.status_code in (200, 201))

        # Token-based role tests
        viewer_h = {"Authorization": "Bearer token-viewer"}
        oper_h = {"Authorization": "Bearer token-operator"}
        admin_h = {"Authorization": "Bearer token-admin"}

        # Viewer creates normal task → 403
        r = await cli.post("/api/v1/client/tasks",
                           json={"type": "vt", "priority": 10,
                                 "payload": {"execution": {"mode": "shell", "command": "echo"}}},
                           headers=viewer_h)
        check("3. Viewer creates task → 403 (needs operator+)", r.status_code == 403)

        # Operator creates normal task → 200
        r = await cli.post("/api/v1/client/tasks",
                           json={"type": "op-task", "priority": 20,
                                 "payload": {"execution": {"mode": "", "command": "echo hi"}}},
                           headers=oper_h)
        check("4. Operator creates normal task → 2xx", r.status_code in (200, 201))

        # Operator creates shell task → 403
        r = await cli.post("/api/v1/client/tasks",
                           json={"type": "op-shell", "priority": 20,
                                 "payload": {"execution": {"mode": "shell", "command": "danger"}}},
                           headers=oper_h)
        check("5. Operator creates shell task → 403", r.status_code == 403)

        # Operator creates hermes task → 403
        r = await cli.post("/api/v1/client/tasks",
                           json={"type": "op-hermes", "priority": 20,
                                 "payload": {"execution": {"mode": "hermes", "prompt": "do"}}},
                           headers=oper_h)
        check("5b. Operator creates hermes task → 403", r.status_code == 403)

        # Admin creates shell → 200
        r = await cli.post("/api/v1/client/tasks",
                           json={"type": "adm-shell", "priority": 50,
                                 "payload": {"execution": {"mode": "shell", "command": "admin"}}},
                           headers=admin_h)
        check("6. Admin creates shell task → 2xx", r.status_code in (200, 201))

        # Admin creates hermes → 200
        r = await cli.post("/api/v1/client/tasks",
                           json={"type": "adm-hermes", "priority": 50,
                                 "payload": {"execution": {"mode": "hermes", "prompt": "admin"}}},
                           headers=admin_h)
        check("6b. Admin creates hermes task → 2xx", r.status_code in (200, 201))

        # ═══════════════════════════════════════════════════════════
        # 7-9. CSRF token tests (via login + session cookie)
        # ═══════════════════════════════════════════════════════════
        login_r = await cli.post("/api/v1/auth/login",
                                 json={"username": "owner", "password": "pass"})
        check("7. Login succeeds", login_r.status_code == 200)

        # After login, extract cookies for explicit passing
        # (httpx ASGITransport doesn't auto-send cookie jar cookies)
        _cookies = {}
        for k, v in cli.cookies.items():
            _cookies[k] = v
        csrf = _cookies.get("csrf_token", "")
        has_session = bool(_cookies.get("dispatch_session", ""))
        check("7b. Session + CSRF cookies received", csrf != "")

        # Without CSRF token header → 403 (CSRF blocks it)
        r = await cli.post("/api/v1/client/tasks",
                           json={"type": "csrf-bad", "priority": 10,
                                 "payload": {"execution": {"mode": "", "command": "echo"}}},
                           cookies=_cookies)
        check("8. POST without CSRF header → 403", r.status_code == 403)

        # With CSRF token → 200
        if csrf:
            r = await cli.post("/api/v1/client/tasks",
                               json={"type": "csrf-good", "priority": 10,
                                     "payload": {"execution": {"mode": "", "command": "echo"}}},
                               cookies=_cookies,
                               headers={"X-CSRF-Token": csrf})
            check("8b. POST with CSRF token → 2xx", r.status_code in (200, 201))
            task_id = r.json().get("task_id", "")
            if task_id:
                r = await cli.post(f"/api/v1/client/tasks/{task_id}/cancel",
                                   cookies=_cookies,
                                   headers={"X-CSRF-Token": csrf})
                check("8c. Cancel with CSRF → 2xx", r.status_code in (200, 201))

        # Evil origin → 403
        if csrf:
            r = await cli.post("/api/v1/client/tasks",
                               json={"type": "evil", "priority": 10,
                                     "payload": {"execution": {"mode": "", "command": "echo"}}},
                               cookies=_cookies,
                               headers={
                                   "X-CSRF-Token": csrf,
                                   "Origin": "https://admin.dispatch.example.com.evil.com",
                               })
            check("9. Evil origin https://admin.example.com.evil.com → 403",
                  r.status_code == 403)

            # Legitimate origin → 200
            r = await cli.post("/api/v1/client/tasks",
                               json={"type": "good-origin", "priority": 10,
                                     "payload": {"execution": {"mode": "", "command": "echo"}}},
                               cookies=_cookies,
                               headers={
                                   "X-CSRF-Token": csrf,
                                   "Origin": "https://admin.dispatch.example.com",
                               })
            check("9b. Valid origin https://admin.dispatch.example.com → 2xx",
                  r.status_code in (200, 201))

        # ═══════════════════════════════════════════════════════════
        # 10. Compute node isolation
        # ═══════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks",
                           json={"type": "isolate", "priority": 99,
                                 "payload": {"execution": {"mode": "shell", "command": "echo"}}},
                           headers=admin_bearer)
        check("10. Task created for isolation test", r.status_code in (200, 201))
        task_id = r.json()["task_id"]

        # Node A pulls
        r = await cli.post("/api/v1/compute/tasks/pull",
                           json={},
                           headers={"Authorization": "Bearer token-a",
                                    "X-Node-Id": "node-a"})
        check("10a. Node A pulls task", r.status_code == 200 and r.json() is not None)

        # Node B tries log → 403
        r = await cli.post(f"/api/v1/compute/tasks/{task_id}/log",
                           json={"level": "INFO", "message": "fake"},
                           headers={"Authorization": "Bearer token-b", "X-Node-Id": "node-b"})
        check("10b. Node B cannot log → 403", r.status_code == 403)

        # Node B tries renew → 403
        r = await cli.post(f"/api/v1/compute/tasks/{task_id}/renew",
                           json={"lease_seconds": 300},
                           headers={"Authorization": "Bearer token-b", "X-Node-Id": "node-b"})
        check("10c. Node B cannot renew → 403", r.status_code == 403)

        # Node B tries finish → 4xx
        r = await cli.post(f"/api/v1/compute/tasks/{task_id}/finish",
                           json={"result": {"hacked": True}},
                           headers={"Authorization": "Bearer token-b", "X-Node-Id": "node-b"})
        check("10d. Node B cannot finish → 4xx", r.status_code in (400, 403))

        # Node B tries fail → 4xx
        r = await cli.post(f"/api/v1/compute/tasks/{task_id}/fail",
                           json={"error": "fake"},
                           headers={"Authorization": "Bearer token-b", "X-Node-Id": "node-b"})
        check("10e. Node B cannot fail → 4xx", r.status_code in (400, 403))

        # ═══════════════════════════════════════════════════════════
        # 11. Disabled node cannot pull
        # ═══════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/compute/tasks/pull",
                           json={},
                           headers={"Authorization": "Bearer token-dis",
                                    "X-Node-Id": "node-disabled"})
        check("11. Disabled node cannot pull → 403", r.status_code == 403)

        # ═══════════════════════════════════════════════════════════
        # 12. max_parallel_tasks capacity (node-a has limit=2)
        # ═══════════════════════════════════════════════════════════
        # Create 3 tasks, pull max_parallel=2, 3rd pull should be None
        t_ids = []
        for i in range(3):
            r = await cli.post("/api/v1/client/tasks",
                               json={"type": f"cap-test-{i}", "priority": 50,
                                     "payload": {"execution": {"mode": "", "command": "echo"}}},
                               headers=admin_bearer)
            t_ids.append(r.json()["task_id"])
        check("12. 3 capacity test tasks created", len(t_ids) == 3)

        # Create 3 tasks. Node-a already has 1 running from test 10,
        # so with max_parallel=2 it can pull exactly 1 more.
        pulls_ok = 0
        for i in range(3):
            r = await cli.post("/api/v1/compute/tasks/pull",
                               json={},
                               headers={"Authorization": "Bearer token-a",
                                        "X-Node-Id": "node-a"})
            if r.status_code == 200 and r.json() is not None:
                pulls_ok += 1
        check("12a. Node-A pulled 1 more task (max_parallel=2, 1 already running)",
              pulls_ok == 1)
        check("12b. 3rd/4th pull returned None (at capacity)", pulls_ok == 1)

        # ═══════════════════════════════════════════════════════════
        # 13. /api/v1/admin/tasks permission: viewer/operator → 403
        # ═══════════════════════════════════════════════════════════
        r = await cli.get("/api/v1/admin/tasks", headers=viewer_h)
        check("13. viewer cannot access /admin/tasks → 403", r.status_code == 403)
        r = await cli.get("/api/v1/admin/tasks", headers=oper_h)
        check("13b. operator cannot access /admin/tasks → 403", r.status_code == 403)
        r = await cli.get("/api/v1/admin/tasks", headers=admin_h)
        check("13c. admin can access /admin/tasks → 200", r.status_code == 200)
        r = await cli.get("/api/v1/admin/tasks",
                           headers={"Authorization": "Bearer token-owner"})
        check("13d. owner can access /admin/tasks → 200", r.status_code == 200)

        # ═══════════════════════════════════════════════════════════
        # 14. execution.mode hard filter
        # ═══════════════════════════════════════════════════════════
        # Create a docker-only task — no node has docker=true, so it stays pending
        r = await cli.post("/api/v1/client/tasks",
                           json={"type": "docker-test", "priority": 99,
                                 "payload": {"execution": {"mode": "docker", "command": "run"}}},
                           headers=admin_bearer)
        task_id_docker = r.json()["task_id"]
        check("14. Docker task created", r.status_code in (200, 201))

        # Neither node-a (saturated) nor node-b (docker=false) can pull it
        # Verify it stays pending
        r = await cli.get(f"/api/v1/client/tasks/{task_id_docker}",
                          headers=admin_bearer)
        check("14a. Docker task stays pending (no capable node)",
              r.status_code == 200 and r.json().get("status") == "pending" and
              r.json().get("assigned_node_id") is None)

        # Create a shell task, verify it gets assigned (node-b has shell=true)
        r = await cli.post("/api/v1/client/tasks",
                           json={"type": "shell-test", "priority": 90,
                                 "payload": {"execution": {"mode": "shell", "command": "echo"}}},
                           headers=admin_bearer)
        check("14b. Shell task created", r.status_code in (200, 201))

        # Try to pull with node-b (has shell=true) — should succeed
        # First drain any existing pending task from node-b's queue
        # by checking if it's at capacity
        pulled_shell = False
        for _ in range(3):
            r = await cli.post("/api/v1/compute/tasks/pull",
                               json={},
                               headers={"Authorization": "Bearer token-b",
                                        "X-Node-Id": "node-b"})
            if r.status_code == 200 and r.json() is not None:
                pulled_shell = True
                break
        check("14c. Shell task gets assigned (node has shell=true)", pulled_shell)

        # ═══════════════════════════════════════════════════════════
        # 15. ComputeTaskPullResponse has lease_seconds
        # ═══════════════════════════════════════════════════════════
        # Add common to path for this import
        import sys as _sys
        _common_path = os.path.join(PROJECT_ROOT, "common")
        if _common_path not in _sys.path:
            _sys.path.insert(0, _common_path)
        from dispatch_common.schemas import ComputeTaskPullResponse
        fields = ComputeTaskPullResponse.__fields__
        check("15. ComputeTaskPullResponse has lease_seconds",
              "lease_seconds" in fields)

        # ═══════════════════════════════════════════════════════════
        # 16. Security headers on CSRF 403
        # ═══════════════════════════════════════════════════════════
        if _cookies:
            r = await cli.post("/api/v1/client/tasks",
                               json={"type": "csrf-headers", "priority": 10,
                                     "payload": {"execution": {"mode": "", "command": "echo"}}},
                               cookies=_cookies)
            # No CSRF token → 403 with security headers
            check("16. CSRF 403 has X-Content-Type-Options",
                  r.status_code == 403 and
                  r.headers.get("x-content-type-options") == "nosniff")

        # ═══════════════════════════════════════════════════════════
        # 17. Referer origin validation
        # ═══════════════════════════════════════════════════════════
        if _cookies and csrf:
            # Valid Referer (full URL with path) should pass
            r = await cli.post("/api/v1/client/tasks",
                               json={"type": "referer-good", "priority": 10,
                                     "payload": {"execution": {"mode": "", "command": "echo"}}},
                               cookies=_cookies,
                               headers={
                                   "X-CSRF-Token": csrf,
                                   "Referer": "https://admin.dispatch.example.com/tasks/123",
                               })
            check("17. Referer with full URL path passes origin check",
                  r.status_code in (200, 201))

            # Evil Referer should be rejected
            r = await cli.post("/api/v1/client/tasks",
                               json={"type": "referer-evil", "priority": 10,
                                     "payload": {"execution": {"mode": "", "command": "echo"}}},
                               cookies=_cookies,
                               headers={
                                   "X-CSRF-Token": csrf,
                                   "Referer": "https://evil.example.com/tasks/123",
                               })
            check("17b. Evil Referer rejected",
                  r.status_code == 403)

        # ═══════════════════════════════════════════════════════════
        # 18. RateLimit 429 has security headers
        # ═══════════════════════════════════════════════════════════
        from app.main import RATE_LIMIT_RULES as _base_rules
        _tight_rules = dict(_base_rules)
        _tight_rules["task_create"] = (1, 3600)  # 1 per hour
        from app.middleware.ratelimit import RateLimitMiddleware as _RLM
        from app.middleware.ratelimit import _rate_limiter as _rl
        _orig_rules = _RLM.RULES
        _RLM.RULES = _tight_rules
        # Clear existing buckets so rate limit starts fresh
        _rl._buckets.clear()

        # Make two rapid task creation requests; second should be 429
        r1 = await cli.post("/api/v1/client/tasks",
                            json={"type": "ratelimit-1", "priority": 10,
                                  "payload": {"execution": {"mode": "", "command": "echo"}}},
                            headers=admin_bearer)
        r2 = await cli.post("/api/v1/client/tasks",
                            json={"type": "ratelimit-2", "priority": 10,
                                  "payload": {"execution": {"mode": "", "command": "echo"}}},
                            headers=admin_bearer)
        _RLM.RULES = _orig_rules  # restore

        # First request succeeds (rate limit not hit)
        check("18. First request succeeds",
              r1.status_code in (200, 201))
        # Second should be 429 with security headers
        is_429 = r2.status_code == 429
        has_xfo = r2.headers.get("x-frame-options") == "DENY"
        has_csp = "content-security-policy" in r2.headers
        has_xcto = r2.headers.get("x-content-type-options") == "nosniff"
        check("18b. Rate limit 429 triggered", is_429)
        check("18c. 429 has X-Frame-Options: DENY", has_xfo)
        check("18d. 429 has Content-Security-Policy", has_csp)
        check("18e. 429 has X-Content-Type-Options: nosniff", has_xcto)

    # Cleanup
    await engine.dispose()
    if os.path.exists(_db_path):
        os.unlink(_db_path)

    print(f"\n{'=' * 60}")
    print(f"RESULTS: {passed}/{total} tests passed "
          + ("✅" if passed == total else "❌"))
    print(f"{'=' * 60}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_tests())
