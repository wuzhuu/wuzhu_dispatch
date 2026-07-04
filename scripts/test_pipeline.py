#!/usr/bin/env python3
"""
Full pipeline simulation test for wuzhu-dispatch (three-role architecture).

Tests (in order):
  1. Create admin/owner user
  2. Admin registers a compute node
  3. Compute-server heartbeat
  4. Client creates tasks with ownership
  5. Compute-server pulls task (atomic claim)
  6. Lease renewal
  7. Log upload
  8. Task finish / fail with retry logic
  9. Client queries results
  10. Permission isolation viewer/operator/admin
  11. Lease expiry & recovery
  12. Cross-node isolation (node A cannot touch node B's tasks)
  13. Registration token modes
  14. System token tasks (no FK to users)

Uses SQLite — no MySQL required.
"""

import sys
import os
import json
import asyncio
import tempfile
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'dispatcher'))

# ── Force SQLite BEFORE any import ──────────────────────────────
os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
os.environ["DISPATCH_SERVER_SECRET"] = "test-secret-12345"
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["CORS_ALLOWED_ORIGINS"] = "http://localhost:3000"
os.environ["CSRF_ALLOWED_ORIGINS"] = "http://localhost:3000"
os.environ["REGISTRATION_TOKEN"] = "reg-token-test"

# Use a temp file for the DB
_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db_path}"

import app.database as db_mod
from app.config import settings
from app.models import (
    Base, User, ClientApiToken, Session,
    ComputeNode, ComputeNodeStatus,
    Task, TaskLog, Artifact, AuditLog,
)
from app.schemas import (
    NodeRegisterRequest, HeartbeatRequest, TaskCreateRequest,
    TaskLogRequest, TaskFinishRequest, TaskFailRequest, TaskRenewRequest,
    NodeUpdateRequest,
)
from app.services.node_service import (
    register_node, process_heartbeat, get_all_nodes, get_node_by_id,
)
from app.services.client_task_service import (
    create_task, list_tasks_for_client, get_task_by_id,
    cancel_task, retry_task, get_task_logs,
)
from app.services.compute_task_service import (
    pull_task_for_node, append_task_log, finish_task, fail_task,
    release_expired_leases, renew_task_lease, get_all_tasks,
)
from app.services.audit_service import log_audit, get_audit_logs
from app.auth import (
    ClientAuthContext, hash_password, verify_password,
    _check_role,
)


async def main():
    print("=" * 60)
    print("wuzhu-dispatch V6: Three-Role Pipeline Test")
    print("=" * 60)
    passed = total = 0

    def check(desc, cond):
        nonlocal passed, total
        total += 1
        if cond:
            passed += 1
            print(f"  ✅ {desc}")
        else:
            print(f"  ❌ {desc}")

    # Create tables
    async with db_mod.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("[OK] Tables created\n")

    async with db_mod.async_session_factory() as db:
        # ═══════════════════════════════════════════════════════════
        # 1. Create owner user
        # ═══════════════════════════════════════════════════════════
        owner = User(
            user_id="owner-001",
            username="admin",
            password_hash=hash_password("admin123!"),
            role="owner", enabled=True,
        )
        db.add(owner)
        operator = User(
            user_id="operator-001",
            username="operator",
            password_hash=hash_password("pass123!"),
            role="operator", enabled=True,
        )
        db.add(operator)
        viewer = User(
            user_id="viewer-001",
            username="viewer",
            password_hash=hash_password("pass123!"),
            role="viewer", enabled=True,
        )
        db.add(viewer)
        await db.commit()

        owner_ctx = ClientAuthContext(user=owner)
        operator_ctx = ClientAuthContext(user=operator)
        viewer_ctx = ClientAuthContext(user=viewer)
        system_ctx = ClientAuthContext(is_system=True)

        check("Owner user created", True)
        check("Password hashing works", verify_password("admin123!", owner.password_hash))
        check("Wrong password rejected", not verify_password("wrong", owner.password_hash))

        # ═══════════════════════════════════════════════════════════
        # 2. Register compute node (admin registers)
        # ═══════════════════════════════════════════════════════════
        node1_req = NodeRegisterRequest(
            node_id="us-node-1", agent_token="token-us-1",
            name="US Worker", region="US", provider="TestProvider",
            roles=["compute_server"],
            tags=["us", "foreign_reachable", "public_ipv4"],
            static_profile={
                "cpu_cores": 4, "memory_mb": 8192, "bandwidth_mbps": 100,
                "public_ipv4": True, "public_ipv6": False,
                "cn_reachable": "poor", "foreign_reachable": "excellent",
                "runtime": {"shell": True, "python": True, "docker": True, "hermes": False},
                "limits": {"max_parallel_tasks": 3, "allow_heavy_download": True, "allow_heavy_compute": True},
            },
        )
        await register_node(db, node1_req)
        check("Compute node 1 registered", True)

        node2_req = NodeRegisterRequest(
            node_id="hk-node-2", agent_token="token-hk-2",
            name="HK Small", region="HK", provider="TestProvider",
            roles=["compute_server"],
            tags=["hk", "cn_reachable", "low_bandwidth", "ipv6_only"],
            static_profile={
                "cpu_cores": 1, "memory_mb": 512, "bandwidth_mbps": 5,
                "public_ipv4": False, "public_ipv6": True,
                "cn_reachable": "good", "foreign_reachable": "fair",
                "runtime": {"shell": True, "python": True, "docker": False, "hermes": False},
                "limits": {"max_parallel_tasks": 1, "allow_heavy_download": False, "allow_heavy_compute": False},
            },
        )
        await register_node(db, node2_req)
        check("Compute node 2 registered", True)

        # Verify token_hash was stored
        from app.services.node_service import _hash_token
        node1_db = await get_node_by_id(db, "us-node-1")
        check("Agent token stored as hash",
              node1_db.agent_token_hash == _hash_token("token-us-1"))

        # ═══════════════════════════════════════════════════════════
        # 3. Heartbeat
        # ═══════════════════════════════════════════════════════════
        from sqlalchemy import select
        n1_result = await db.execute(select(ComputeNode).where(ComputeNode.node_id == "us-node-1"))
        n1 = n1_result.scalar_one()
        n2_result = await db.execute(select(ComputeNode).where(ComputeNode.node_id == "hk-node-2"))
        n2 = n2_result.scalar_one()

        await process_heartbeat(db, n1, HeartbeatRequest(
            cpu_usage=30.0, memory_usage=45.0, running_tasks=0,
        ))
        await process_heartbeat(db, n2, HeartbeatRequest(
            cpu_usage=10.0, memory_usage=30.0, running_tasks=0,
        ))
        check("Heartbeat works", True)

        # ═══════════════════════════════════════════════════════════
        # 4. Client creates tasks (with ownership)
        # ═══════════════════════════════════════════════════════════
        t1 = await create_task(db, TaskCreateRequest(
            type="web_collect", priority=60, timeout_seconds=1800, max_retries=2,
            requirements={"required_tags": ["foreign_reachable"], "runtime": {"python": True}, "min_memory_mb": 512},
            payload={"execution": {"mode": "shell", "command": "python collect.py"}},
        ), created_by_user_id=owner.user_id, created_by_client_token_id=None)
        check(f"Task 1 created by owner: {t1.task_id[:8]}", t1.status == "pending")

        t2 = await create_task(db, TaskCreateRequest(
            type="collect_cn", priority=80, timeout_seconds=600, max_retries=1,
            requirements={"required_tags": ["cn_reachable"], "runtime": {"python": True}, "min_memory_mb": 256},
            payload={"execution": {"mode": "shell", "command": "python collect_cn.py"}},
        ), created_by_user_id=operator.user_id)
        check(f"Task 2 created by operator: {t2.task_id[:8]}", t2.status == "pending")

        # System token task (created_by_user_id=None, no FK issue)
        t_sys = await create_task(db, TaskCreateRequest(
            type="system_task", priority=90, timeout_seconds=300, max_retries=0,
            payload={"execution": {"mode": "shell", "command": "echo sys"}},
        ), created_by_user_id=None, created_by_client_token_id="system")
        check(f"System task created (no FK to users): {t_sys.task_id[:8]}", t_sys.status == "pending")
        check("System task has created_by_user_id=None", t_sys.created_by_user_id is None)

        # ═══════════════════════════════════════════════════════════
        # 5. Atomic task pull
        # ═══════════════════════════════════════════════════════════
        pulled, lease_dur = await pull_task_for_node(db, n1)
        check(f"Node 1 pulled task: {pulled.task_id[:8] if pulled else 'None'}",
              pulled is not None and lease_dur > 0)

        pulled_check = await get_task_by_id(db, pulled.task_id)
        check("Pulled task is running", pulled_check.status == "running")
        check("Assigned to correct node", pulled_check.assigned_node_id == "us-node-1")
        check("Lease > 0s", lease_dur >= 300)

        # ═══════════════════════════════════════════════════════════
        # 6. Lease renewal
        # ═══════════════════════════════════════════════════════════
        old_lease = pulled_check.lease_until
        await asyncio.sleep(0.1)
        renewed = await renew_task_lease(db, pulled.task_id, "us-node-1",
                                         TaskRenewRequest(lease_seconds=3600))
        check("Lease extended",
              renewed.lease_until and old_lease and
              renewed.lease_until.timestamp() > old_lease.timestamp())

        try:
            await renew_task_lease(db, pulled.task_id, "hk-node-2",
                                   TaskRenewRequest(lease_seconds=300))
            check("Wrong node renewal rejected", False)
        except Exception:
            check("Wrong node cannot renew lease", True)

        # ═══════════════════════════════════════════════════════════
        # 7. Log upload
        # ═══════════════════════════════════════════════════════════
        await append_task_log(db, pulled.task_id, "us-node-1",
                              TaskLogRequest(level="INFO", message="Task started"))
        await append_task_log(db, pulled.task_id, "us-node-1",
                              TaskLogRequest(level="INFO", message="Collecting data..."))
        logs = await get_task_logs(db, pulled.task_id)
        check(f"Logs uploaded: {len(logs)} entries", len(logs) == 2)

        # ═══════════════════════════════════════════════════════════
        # 8. Finish task
        # ═══════════════════════════════════════════════════════════
        await finish_task(db, pulled.task_id, "us-node-1",
                          TaskFinishRequest(result={"stdout": "OK", "exit_code": 0}))
        finished = await get_task_by_id(db, pulled.task_id)
        check("Task finished successfully", finished.status == "success")

        # ═══════════════════════════════════════════════════════════
        # 9. Fail with retry
        # ═══════════════════════════════════════════════════════════
        pulled2, _ = await pull_task_for_node(db, n2)
        check(f"Node 2 pulled task: {pulled2.task_id[:8]}", pulled2 is not None)

        await fail_task(db, pulled2.task_id, "hk-node-2",
                        TaskFailRequest(error="Timeout"))
        failed = await get_task_by_id(db, pulled2.task_id)
        check(f"Task retrying ({failed.retry_count}/{failed.max_retries})",
              failed.status == "retrying" and failed.retry_count == 1)

        # Fail again — max retries → failed
        pulled_retry, _ = await pull_task_for_node(db, n2)
        check("Retry task pulled", pulled_retry is not None)
        await fail_task(db, pulled_retry.task_id, "hk-node-2",
                        TaskFailRequest(error="Still broken"))
        failed_final = await get_task_by_id(db, pulled2.task_id)
        check("Task failed permanently", failed_final.status == "failed")
        check("Error info in result", "failed_node" in (failed_final.result or {}))

        # ═══════════════════════════════════════════════════════════
        # 10. Client query results — ownership isolation
        # ═══════════════════════════════════════════════════════════
        # Owner sees all tasks
        owner_tasks = await list_tasks_for_client(db, owner_ctx)
        check("Owner sees all tasks", len(owner_tasks) >= 2)

        # Viewer sees only their own (0 since viewer created none)
        viewer_tasks = await list_tasks_for_client(db, viewer_ctx)
        check("Viewer sees only their own tasks", len(viewer_tasks) == 0)

        # Operator sees only their own tasks
        operator_tasks = await list_tasks_for_client(db, operator_ctx)
        # t2 was created by operator
        has_own = any(t.task_id == t2.task_id for t in operator_tasks)
        check("Operator sees their own tasks", has_own)
        has_others = any(t.task_id == t1.task_id for t in operator_tasks)
        check("Operator does NOT see others' tasks", not has_others)

        # System sees all
        sys_tasks = await list_tasks_for_client(db, system_ctx)
        check("System sees all tasks", len(sys_tasks) >= 3)

        # ═══════════════════════════════════════════════════════════
        # 11. Lease expiry
        # ═══════════════════════════════════════════════════════════
        # Create a fresh task, pull it, expire
        tx = await create_task(db, TaskCreateRequest(
            type="expiry_test", priority=99, timeout_seconds=60, max_retries=0,
            payload={"execution": {"mode": "shell", "command": "echo test"}},
        ), created_by_user_id=owner.user_id)

        tx_pulled, _ = await pull_task_for_node(db, n1)
        check("Expiry test task pulled", tx_pulled is not None)

        # Force lease past
        tx_db = await get_task_by_id(db, tx.task_id)
        tx_db.lease_until = datetime.utcnow() - timedelta(seconds=10)
        tx_db.status = "running"
        await db.commit()

        expired = await release_expired_leases(db)
        check("Lease expiry detected", len(expired) > 0)
        tx_check = await get_task_by_id(db, tx.task_id)
        check("Expired task recorded last_node",
              tx_check.result and tx_check.result.get("last_node") == "us-node-1")

        # ═══════════════════════════════════════════════════════════
        # 12. Cross-node isolation
        # ═══════════════════════════════════════════════════════════
        # Node 2 tries to finish node 1's task
        try:
            await finish_task(db, t1.task_id, "hk-node-2",
                              TaskFinishRequest(result={"hacked": True}))
            check("Node B cannot finish Node A's task", False)
        except ValueError:
            check("Node B cannot finish Node A's task", True)

        try:
            await fail_task(db, t1.task_id, "hk-node-2",
                            TaskFailRequest(error="fake"))
            check("Node B cannot fail Node A's task", False)
        except ValueError:
            check("Node B cannot fail Node A's task", True)

        # ═══════════════════════════════════════════════════════════
        # 13. RBAC role hierarchy
        # ═══════════════════════════════════════════════════════════
        check("viewer can view", _check_role("viewer", "viewer"))
        check("viewer < operator", not _check_role("operator", "viewer"))
        check("admin >= operator", _check_role("operator", "admin"))
        check("owner >= admin", _check_role("admin", "owner"))
        check("owner can create shell", _check_role("admin", "owner"))
        check("viewer cannot create shell", not _check_role("admin", "viewer"))

        # ═══════════════════════════════════════════════════════════
        # 14. Audit logging
        # ═══════════════════════════════════════════════════════════
        await log_audit(db, "test.action", user_id="owner-001",
                        target_type="test", target_id="t-001",
                        detail={"key": "value"})
        logs, total_count = await get_audit_logs(db)
        check(f"Audit log entries: {total_count}", total_count >= 1)

        # ═══════════════════════════════════════════════════════════
        # 15. Node list
        # ═══════════════════════════════════════════════════════════
        nodes = await get_all_nodes(db)
        check(f"Total nodes: {len(nodes)}", len(nodes) == 2)

        # ═══════════════════════════════════════════════════════════
        # 16. Finish/fail on already-terminal task is rejected
        # ═══════════════════════════════════════════════════════════
        try:
            await finish_task(db, t1.task_id, "us-node-1",
                              TaskFinishRequest(result={"should": "fail"}))
            check("Cannot finish already-success task", False)
        except ValueError:
            check("Cannot finish already-success task", True)

        # ═══════════════════════════════════════════════════════════
        # 17. Disable/enable node
        # ═══════════════════════════════════════════════════════════
        from app.services.node_service import disable_node, enable_node
        await disable_node(db, "us-node-1")
        n1_check = await get_node_by_id(db, "us-node-1")
        check("Node can be disabled", n1_check is not None and not n1_check.enabled)
        await enable_node(db, "us-node-1")
        n1_check = await get_node_by_id(db, "us-node-1")
        check("Node can be re-enabled", n1_check.enabled)

        # ═══════════════════════════════════════════════════════════
        # 18. CSRF token independence
        # ═══════════════════════════════════════════════════════════
        from app.middleware.security import csrf_token_hmac
        test_sid = "test-session"
        csrf_val = csrf_token_hmac(test_sid, "test-secret")
        check("CSRF token ≠ session_id", csrf_val != test_sid)
        check("CSRF token deterministic",
              csrf_val == csrf_token_hmac(test_sid, "test-secret"))
        check("CSRF differs with different secret",
              csrf_val != csrf_token_hmac(test_sid, "other-secret"))

    # ═══════════════════════════════════════════════════════════════
    # Summary
    # ═══════════════════════════════════════════════════════════════
    await db_mod.engine.dispose()
    if os.path.exists(_db_path):
        os.unlink(_db_path)

    print(f"\n{'=' * 60}")
    print(f"RESULTS: {passed}/{total} tests passed "
          + ("✅" if passed == total else "❌"))
    print(f"{'=' * 60}")
    return passed == total


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
