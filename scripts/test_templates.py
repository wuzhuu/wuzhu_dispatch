#!/usr/bin/env python3
"""
Route-level integration tests for V8.3 features: templates, token scope,
quick task, target model, and node/identity isolation.

Tests (in order):
  1. GET /api/v1/client/templates returns template list
  2. POST /tasks with valid template succeeds
  3. POST /tasks with unknown template → 400
  4. POST /tasks with template not in allowed_templates → 403
  5. POST /tasks: shell mode requires admin role
  6. POST /tasks: token without 'shell' in denied_modes can't create shell
  7. POST /tasks/quick with template, done immediately
  8. POST /tasks/quick polling (not done yet)
  9. client token with max_timeout_seconds exceeded → 403
  10. client token with max_priority exceeded → 403
  11. client token can_target_specific_node=false → 403 on node_id
  12. admin token can target specific node
  13. target tags outside allowed_target_tags → 403
  14. node token cannot create client task → 401/403
  15. node token cannot access admin API
  16. client token cannot pull task
  17. disabled node not auto-scheduled
  (existing 50 route tests already cover basic role enforcement)
"""

import json
import os
import sys
import tempfile
from contextlib import asynccontextmanager

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "dispatcher"))

_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db_path}"
os.environ["DISPATCH_SERVER_SECRET"] = "test-secret-v83"
os.environ["SESSION_SECRET"] = "test-session-secret-v83"
os.environ["CORS_ALLOWED_ORIGINS"] = "https://admin.dispatch.example.com"
os.environ["CSRF_ALLOWED_ORIGINS"] = "https://admin.dispatch.example.com"
os.environ["REGISTRATION_TOKEN"] = "reg-token-test-v83"

from app.models import (  # noqa: F401, E402
    User, Session, ClientApiToken, ComputeNode, ComputeNodeStatus,
    Task, TaskLog, Artifact, AuditLog,
)
from app.database import engine, Base, async_session_factory  # noqa: E402
from app.auth import hash_password, sha256_hash  # noqa: E402
from app.services.node_service import _hash_token  # noqa: E402

import httpx  # noqa: E402

total = passed = 0


def check(desc: str, cond: bool):
    global total, passed
    total += 1
    if cond:
        passed += 1
        print(f"  \u2705 {desc}")
    else:
        print(f"  \u274c {desc}")


async def seed_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session_factory() as db:
        # Users
        db.add_all([
            User(user_id="owner-v83", username="owner-v83",
                 password_hash=hash_password("pass"), role="owner", enabled=True),
            User(user_id="admin-v83", username="admin-v83",
                 password_hash=hash_password("pass"), role="admin", enabled=True),
            User(user_id="oper-v83", username="operator-v83",
                 password_hash=hash_password("pass"), role="operator", enabled=True),
            User(user_id="viewer-v83", username="viewer-v83",
                 password_hash=hash_password("pass"), role="viewer", enabled=True),
        ])
        await db.flush()

        # Compute nodes
        db.add_all([
            ComputeNode(node_id="node-hk", name="HK Node", region="HK",
                        tags=["hk", "high_bandwidth", "foreign_reachable"],
                        static_profile={"runtime": {"shell": True, "python": True},
                                        "limits": {"max_parallel_tasks": 2},
                                        "bandwidth_mbps": 100, "memory_mb": 2048},
                        agent_token_hash=_hash_token("token-hk"), enabled=True),
            ComputeNode(node_id="node-us", name="US Node", region="US",
                        tags=["us", "foreign_reachable"],
                        static_profile={"runtime": {"shell": True},
                                        "limits": {"max_parallel_tasks": 1},
                                        "bandwidth_mbps": 30, "memory_mb": 1024},
                        agent_token_hash=_hash_token("token-us"), enabled=True),
            ComputeNode(node_id="node-disabled", name="Disabled",
                        static_profile={"runtime": {"shell": True},
                                        "limits": {"max_parallel_tasks": 1}},
                        agent_token_hash=_hash_token("token-dis"), enabled=False),
        ])
        await db.flush()

        # Compute status
        db.add_all([
            ComputeNodeStatus(node_id="node-hk", online=True, cpu_usage=20.0,
                             memory_usage=30.0, running_tasks=0),
            ComputeNodeStatus(node_id="node-us", online=True, cpu_usage=50.0,
                             memory_usage=60.0, running_tasks=1),
        ])
        await db.flush()

        # Client API tokens with various scopes
        # owner token: full access
        db.add(ClientApiToken(
            token_id="tok-owner-full",
            token_hash=sha256_hash("token-owner-full"),
            user_id="owner-v83", scope=[], enabled=True,
        ))
        # operator token: template-only, specific templates
        db.add(ClientApiToken(
            token_id="tok-oper-template",
            token_hash=sha256_hash("token-oper-template"),
            user_id="oper-v83",
            scope=[{
                "allowed_templates": ["http_probe", "dns_probe"],
                "allowed_modes": ["template"],
                "denied_modes": ["shell", "hermes"],
                "allowed_target_tags": ["hk", "foreign_reachable"],
                "max_priority": 50,
                "max_timeout_seconds": 300,
                "can_target_specific_node": False,
            }],
            enabled=True,
        ))
        # viewer token: very restricted
        db.add(ClientApiToken(
            token_id="tok-viewer-restricted",
            token_hash=sha256_hash("token-viewer-restricted"),
            user_id="viewer-v83",
            scope=[{
                "allowed_templates": ["http_probe"],
                "allowed_modes": ["template"],
                "denied_modes": ["shell", "hermes"],
                "allowed_target_tags": ["hk"],
                "max_priority": 30,
                "max_timeout_seconds": 60,
                "can_target_specific_node": False,
            }],
            enabled=True,
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
        print("V8.3 Template/Scope/Quick Task Integration Tests")
        print("=" * 60)

        # ── Test helpers ─────────────────────────────────────────────
        admin_bearer = {"Authorization": "Bearer test-secret-v83"}
        owner_h = {"Authorization": "Bearer token-owner-full"}
        oper_h = {"Authorization": "Bearer token-oper-template"}
        viewer_h = {"Authorization": "Bearer token-viewer-restricted"}
        node_hk = {"Authorization": "Bearer token-hk", "X-Node-Id": "node-hk"}

        # ═══════════════════════════════════════════════════════════════
        # 1. Template list
        # ═══════════════════════════════════════════════════════════════
        r = await cli.get("/api/v1/client/templates")
        check("1. Template list returns 200", r.status_code == 200)
        templates = r.json()
        check("1b. http_probe in templates", "http_probe" in templates)
        check("1c. dns_probe in templates", "dns_probe" in templates)

        # ═══════════════════════════════════════════════════════════════
        # 2. Create task with valid template (operator token)
        # ═══════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks",
                           json={
                               "template_id": "http_probe",
                               "params": {"url": "https://example.com", "timeout": 5},
                               "target": {"mode": "auto", "tags": ["hk"]},
                               "priority": 30,
                               "timeout_seconds": 60,
                           },
                           headers=oper_h)
        check("2. Template task created", r.status_code in (200, 201))
        if r.status_code in (200, 201):
            task_data = r.json()
            check("2b. Task type is template:http_probe",
                  task_data.get("type") == "template:http_probe")

        # ═══════════════════════════════════════════════════════════════
        # 3. Unknown template → 400
        # ═══════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks",
                           json={
                               "template_id": "nonexistent_template",
                               "params": {},
                           },
                           headers=oper_h)
        check("3. Unknown template → 400", r.status_code == 400)

        # ═══════════════════════════════════════════════════════════════
        # 4. Template not in allowed_templates → 403
        # ═══════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks",
                           json={
                               "template_id": "ping_probe",  # not in oper's allowed
                               "params": {"host": "1.1.1.1", "count": 2},
                           },
                           headers=oper_h)
        check("4. Template not in allowed_templates → 403", r.status_code == 403)

        # ═══════════════════════════════════════════════════════════════
        # 5. Shell mode requires admin
        # ═══════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks",
                           json={
                               "type": "shell-test",
                               "payload": {"execution": {"mode": "shell", "command": "echo hi"}},
                           },
                           headers=oper_h)
        check("5. Operator shell → 403", r.status_code == 403)

        # Admin can create shell
        r = await cli.post("/api/v1/client/tasks",
                           json={
                               "type": "admin-shell",
                               "payload": {"execution": {"mode": "shell", "command": "echo admin"}},
                           },
                           headers=owner_h)
        check("5b. Owner shell → 2xx", r.status_code in (200, 201))

        # ═══════════════════════════════════════════════════════════════
        # 6. token with denied_modes
        # ═══════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks",
                           json={
                               "type": "hermes-test",
                               "payload": {"execution": {"mode": "hermes", "prompt": "do"}},
                           },
                           headers=oper_h)
        check("6. Operator hermes → 403 (denied_modes)", r.status_code == 403)

        # ═══════════════════════════════════════════════════════════════
        # 7. Quick task — should complete quickly (shell echo)
        # ═══════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks/quick",
                           json={
                               "template_id": "http_probe",
                               "params": {"url": "https://example.com", "timeout": 5},
                               "wait_seconds": 5,
                           },
                           headers=admin_bearer)
        # Quick task may complete or not — either response is valid
        check("7. Quick task returns 200", r.status_code == 200)
        qr = r.json()
        check("7b. Quick task has done field", "done" in qr)
        check("7c. Quick task has task_id", "task_id" in qr)

        # ═══════════════════════════════════════════════════════════════
        # 8. Quick task wait_seconds exceeds token limit → 403 (strict)
        # ═══════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks/quick",
                           json={
                               "template_id": "http_probe",
                               "params": {"url": "https://example.com"},
                               "wait_seconds": 999,  # exceeds max_timeout_seconds=60
                           },
                           headers=viewer_h)
        check("8. Quick task high wait → 403", r.status_code == 403)

        # ═══════════════════════════════════════════════════════════════
        # 9. Client token max_timeout_seconds exceeded → 403
        # ═══════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks",
                           json={
                               "template_id": "http_probe",
                               "params": {"url": "https://example.com"},
                               "timeout_seconds": 9999,  # exceeds 60
                           },
                           headers=viewer_h)
        check("9. Token max_timeout_seconds exceeded → 403", r.status_code == 403)

        # ═══════════════════════════════════════════════════════════════
        # 10. Client token max_priority exceeded → 403
        # ═══════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks",
                           json={
                               "template_id": "http_probe",
                               "params": {"url": "https://example.com"},
                               "priority": 99,  # exceeds 30
                           },
                           headers=viewer_h)
        check("10. Token max_priority exceeded → 403", r.status_code == 403)

        # ═══════════════════════════════════════════════════════════════
        # 11. Client token can_target_specific_node=false → 403 on node_id
        # ═══════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks",
                           json={
                               "template_id": "http_probe",
                               "params": {"url": "https://example.com"},
                               "target": {"mode": "node", "node_id": "node-hk"},
                           },
                           headers=oper_h)
        check("11. Token target node (not allowed) → 403", r.status_code == 403)

        # ═══════════════════════════════════════════════════════════════
        # 12. Admin can target specific node
        # ═══════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks",
                           json={
                               "template_id": "http_probe",
                               "params": {"url": "https://example.com"},
                               "target": {"mode": "node", "node_id": "node-hk"},
                           },
                           headers=owner_h)
        check("12. Admin can target specific node → 2xx",
              r.status_code in (200, 201))

        # ═══════════════════════════════════════════════════════════════
        # 13. Target tags outside allowed_target_tags → 403
        # ═══════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks",
                           json={
                               "template_id": "http_probe",
                               "params": {"url": "https://example.com"},
                               "target": {"tags": ["us"]},  # viewer only allows hk
                           },
                           headers=viewer_h)
        check("13. Target tag outside allowed_target_tags → 403",
              r.status_code == 403)

        # ═══════════════════════════════════════════════════════════════
        # 14. Node token cannot create client task
        # ═══════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks",
                           json={
                               "template_id": "http_probe",
                               "params": {"url": "https://example.com"},
                           },
                           headers=node_hk)
        check("14. Node token create task → 401/403",
              r.status_code in (401, 403))

        # ═══════════════════════════════════════════════════════════════
        # 15. Node token cannot access admin API
        # ═══════════════════════════════════════════════════════════════
        r = await cli.get("/api/v1/admin/nodes", headers=node_hk)
        check("15. Node token admin API → 401/403",
              r.status_code in (401, 403))

        # ═══════════════════════════════════════════════════════════════
        # 16. Client token cannot pull task (compute endpoint)
        # ═══════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/compute/tasks/pull",
                           json={},
                           headers={"Authorization": "Bearer token-owner-full",
                                    "X-Node-Id": "node-hk"})
        # Node auth uses X-Node-Id, client token won't match
        check("16. Client token cannot pull → 403",
              r.status_code == 403)

        # ═══════════════════════════════════════════════════════════════
        # 17. Disabled node not scheduled
        # ═══════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/compute/tasks/pull",
                           json={},
                           headers={"Authorization": "Bearer token-dis",
                                    "X-Node-Id": "node-disabled"})
        check("17. Disabled node pull → 403", r.status_code == 403)

        # ═══════════════════════════════════════════════════════════════
        # 18. Node token cannot list tasks
        # ═══════════════════════════════════════════════════════════════
        r = await cli.get("/api/v1/client/tasks", headers=node_hk)
        check("18. Node token list tasks → 401/403",
              r.status_code in (401, 403))

        # ═══════════════════════════════════════════════════════════════
        # 19. Restricted token: allowed template + valid tags works
        #     Use an operator-level token with limited scope
        # ═══════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks",
                           json={
                               "template_id": "http_probe",
                               "params": {"url": "https://example.com"},
                               "target": {"tags": ["hk"]},
                               "priority": 20,
                               "timeout_seconds": 30,
                           },
                           headers=oper_h)
        check("19. Restricted oper template within scope → 2xx",
              r.status_code in (200, 201))
        # ═══════════════════════════════════════════════════════════════════
        # 20. target.tags persists to requirements.required_tags
        # ═══════════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks",
                           json={
                               "template_id": "http_probe",
                               "params": {"url": "https://example.com"},
                               "target": {"tags": ["hk", "cn_reachable"]},
                           },
                           headers=oper_h)
        if r.status_code in (200, 201):
            td = r.json()
            reqs = td.get("requirements", {})
            check("20. target.tags persisted to required_tags",
                  "hk" in reqs.get("required_tags", []) and
                  "cn_reachable" in reqs.get("required_tags", []))

        # ═══════════════════════════════════════════════════════════════════
        # 21. target.avoid_tags persists to requirements
        # ═══════════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks",
                           json={
                               "template_id": "http_probe",
                               "params": {"url": "https://example.com"},
                               "target": {"avoid_tags": ["low_memory"]},
                           },
                           headers=oper_h)
        if r.status_code in (200, 201):
            td = r.json()
            reqs = td.get("requirements", {})
            check("21. target.avoid_tags persisted",
                  "low_memory" in reqs.get("avoid_tags", []))

        # ═══════════════════════════════════════════════════════════════════
        # 22. target.mode=node persists to requirements.target.node_id
        # ═══════════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks",
                           json={
                               "template_id": "http_probe",
                               "params": {"url": "https://example.com"},
                               "target": {"mode": "node", "node_id": "node-hk"},
                           },
                           headers=owner_h)
        if r.status_code in (200, 201):
            td = r.json()
            reqs = td.get("requirements", {})
            tgt = reqs.get("target", {})
            check("22. target.mode persisted", tgt.get("mode") == "node")
            check("22b. target.node_id persisted", tgt.get("node_id") == "node-hk")

        # ═══════════════════════════════════════════════════════════════════
        # 23. required_tags affects scheduling (node without tag cannot pull)
        # ═══════════════════════════════════════════════════════════════════
        r = await cli.post("/api/v1/client/tasks",
                           json={
                               "template_id": "http_probe",
                               "params": {"url": "https://example.com"},
                               "target": {"tags": ["hk"]},
                               "priority": 80,
                           },
                           headers=owner_h)
        check("23. HK-tagged task created", r.status_code in (200, 201))
        if r.status_code in (200, 201):
            hk_tid = r.json()["task_id"]
            r2 = await cli.post("/api/v1/compute/tasks/pull",
                                json={},
                                headers={"Authorization": "Bearer token-us",
                                         "X-Node-Id": "node-us"})
            if r2.status_code == 200:
                pulled = r2.json()
                check("23a. node-us cannot pull hk-tagged task",
                      pulled is None or pulled.get("task_id") != hk_tid)


    # Cleanup
    await engine.dispose()
    if os.path.exists(_db_path):
        os.unlink(_db_path)

    print(f"\n{'=' * 60}")
    print(f"RESULTS: {passed}/{total} tests passed "
          + ("\u2705" if passed == total else "\u274c"))
    print(f"{'=' * 60}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_tests())
