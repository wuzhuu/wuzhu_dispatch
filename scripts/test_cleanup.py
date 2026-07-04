#!/usr/bin/env python3
"""
V8.2 Cleanup module unit tests for wuzhu-dispatch compute-server.

Tests (in order):
  1. is_valid_task_dir_name — rejects malicious names
  2. resolve_under — path safety
  3. task directory helpers return correct paths
  4. Shell executor creates tasks/<id>/{work,tmp,artifacts,logs}
  5. Shell cwd is task_work_dir
  6. Environment TMPDIR/TEMP/TMP -> task_tmp_dir
  7. meta.json is written with correct status
  8. meta.json status is updated on finish
  9. cleanup disabled does nothing
  10. cleanup removes expired success dirs
  11. cleanup keeps fresh success dirs
  12. cleanup keeps failed dirs when meta says failed -> cleanup_failed=False
  13. cleanup removes failed dirs with meta failed + cleanup_failed=True
  14. orphan dir (no meta) uses longest retention
  15. running task dir is NOT removed
  16. task_id=../../etc rejected by cleanup
  17. Hermes workspace excluded from cleanup
  18. Size eviction removes oldest when over limit
  19. disk_pressure status reported correctly
  20. meta.json missing -> orphan conservative handling
  21. Legacy flat dirs not removed unless legacy_cleanup enabled
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "compute-server"))

from dispatch_compute_server.cleanup import (
    is_valid_task_dir_name,
    resolve_under,
    is_safe_child_path,
    task_root_dir,
    task_work_dir,
    task_tmp_dir,
    task_artifact_dir,
    task_logs_dir,
    task_meta_path,
    write_task_meta,
    read_task_meta,
    update_task_meta_status,
    cleanup_task_dir,
    cleanup_expired_task_dirs,
    get_work_dir_size,
    get_tasks_dir_size,
    _get_tasks_base,
    TASKS_DIR,
    WORK_DIR_SUB,
    TMP_DIR_SUB,
    ARTIFACT_DIR_SUB,
    LOGS_DIR_SUB,
)
from dispatch_compute_server.config import CleanupConfig


def _make_meta(work_dir, task_id, status="success", age=0):
    """Create a task dir with meta.json and set mtime."""
    tdir = Path(task_root_dir(work_dir, task_id))
    (tdir / WORK_DIR_SUB).mkdir(parents=True, exist_ok=True)
    write_task_meta(work_dir, task_id, status=status)
    (tdir / WORK_DIR_SUB / "output.txt").write_text("done")
    old = time.time() - age
    os.utime(str(tdir), (old, old))
    for sub in tdir.iterdir():
        if sub.is_dir():
            os.utime(str(sub), (old, old))
            for f in sub.iterdir():
                if f.is_file():
                    os.utime(str(f), (old, old))
    return tdir


passed = total = 0


def check(desc, cond):
    global passed, total
    total += 1
    if cond:
        passed += 1
        print(f"  \u2705 {desc}")
    else:
        print(f"  \u274c {desc}")


def main():
    print("=" * 60)
    print("V8.2 Cleanup Module Unit Tests")
    print("=" * 60)

    # ═══════════════════════════════════════════════════════════
    # 1. is_valid_task_dir_name
    # ═══════════════════════════════════════════════════════════
    check("UUID is valid", is_valid_task_dir_name("a1b2c3d4-e5f6-7890-abcd-ef1234567890"))
    check("Slug is valid", is_valid_task_dir_name("task_001"))
    check("Dotted name is valid", is_valid_task_dir_name("my.task.1"))
    check("Hyphen name is valid", is_valid_task_dir_name("test-run-3"))
    check("Empty name invalid", not is_valid_task_dir_name(""))
    check("Path traversal '..' invalid", not is_valid_task_dir_name(".."))
    check("Slash invalid", not is_valid_task_dir_name("../etc"))
    check("Null byte invalid", not is_valid_task_dir_name("task\x00id"))
    check("Whitespace prefix invalid", not is_valid_task_dir_name(" task"))
    check("Long name over 128 chars invalid",
          not is_valid_task_dir_name("a" * 129))

    # ═══════════════════════════════════════════════════════════
    # 2. resolve_under path safety
    # ═══════════════════════════════════════════════════════════
    base = "/tmp/test-base"
    resolve_under(base, "/tmp/test-base/mydir")  # should not raise
    check("Direct child resolves OK", True)

    try:
        resolve_under(base, "/tmp/test-base/../etc")
        check("Path traversal raises ValueError", False)
    except ValueError:
        check("Path traversal raises ValueError", True)

    try:
        resolve_under(base, "/tmp/other")
        check("Unrelated path raises ValueError", False)
    except ValueError:
        check("Unrelated path raises ValueError", True)

    try:
        resolve_under(base, "/")
        check("Root raises ValueError", False)
    except ValueError:
        check("Root raises ValueError", True)

    # Symlink test
    with tempfile.TemporaryDirectory() as td:
        real = Path(td) / "real"
        real.mkdir()
        link = Path(td) / "link"
        link.symlink_to("/etc")
        check("Symlink to /etc not under tmpdir",
              not is_safe_child_path(td, str(link)))

    # ═══════════════════════════════════════════════════════════
    # 3. Task directory helpers
    # ═══════════════════════════════════════════════════════════
    wd = "/opt/work"
    tid = "test-task"
    assert task_root_dir(wd, tid) == f"{wd}/tasks/{tid}"
    assert task_work_dir(wd, tid) == f"{wd}/tasks/{tid}/work"
    assert task_tmp_dir(wd, tid) == f"{wd}/tasks/{tid}/tmp"
    assert task_artifact_dir(wd, tid) == f"{wd}/tasks/{tid}/artifacts"
    assert task_logs_dir(wd, tid) == f"{wd}/tasks/{tid}/logs"
    assert task_meta_path(wd, tid) == f"{wd}/tasks/{tid}/meta.json"
    check("Task dir helpers return correct paths", True)

    # ═══════════════════════════════════════════════════════════
    # 4-6. Shell executor creates task dir tree + env vars
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        from dispatch_compute_server.executors.shell_executor import ShellExecutor

        executor = ShellExecutor()
        task = {
            "task_id": "exec-test-1",
            "timeout_seconds": 30,
            "payload": {"execution": {"mode": "shell", "command": "echo hello && env | grep TMPDIR"}},
        }
        result = asyncio_run(executor.run(task, task["payload"]["execution"], td, td))

        check("Executor returns success", result.get("success") is True)
        check("Executor returns task_root", "task_root" in result)
        check("Executor returns task_work_dir", "task_work_dir" in result)
        check("Executor returns task_artifact_dir", "task_artifact_dir" in result)

        task_root = Path(result["task_root"])
        cwd = Path(result["task_work_dir"])
        check(f"task_root exists: {task_root.name}", task_root.is_dir())
        check(f"work/ exists under task_root", (task_root / WORK_DIR_SUB).is_dir())
        check(f"tmp/ exists under task_root", (task_root / TMP_DIR_SUB).is_dir())
        check(f"artifacts/ exists under task_root", (task_root / ARTIFACT_DIR_SUB).is_dir())
        check(f"logs/ exists under task_root", (task_root / LOGS_DIR_SUB).is_dir())
        check(f"meta.json exists", (task_root / "meta.json").is_file())

        # Verify cwd was set correctly
        check("cwd was task_work_dir", result.get("task_work_dir") ==
              task_work_dir(td, "exec-test-1"))

        # Verify TMPDIR points to task_tmp_dir
        output_stdout = result.get("output", {}).get("stdout", "")
        tmp_path = task_tmp_dir(td, "exec-test-1")
        check("TMPDIR in env", tmp_path in output_stdout)

        # Verify meta.json content
        meta = read_task_meta(td, "exec-test-1")
        check("meta.json has task_id", meta and meta["task_id"] == "exec-test-1")
        check("meta.json status is success", meta and meta["status"] == "success")
        check("meta.json has execution_mode", meta and meta.get("execution_mode") == "shell")

    # ═══════════════════════════════════════════════════════════
    # 7-8. meta.json lifecycle
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        write_task_meta(td, "meta-lifecycle", status="running")
        meta = read_task_meta(td, "meta-lifecycle")
        check("meta.json created with running status", meta and meta["status"] == "running")

        update_task_meta_status(td, "meta-lifecycle", "success")
        meta2 = read_task_meta(td, "meta-lifecycle")
        check("meta.json updated to success", meta2 and meta2["status"] == "success")
        check("meta.json has finished_at", meta2 and meta2.get("finished_at") is not None)

    # ═══════════════════════════════════════════════════════════
    # 9. Cleanup disabled does nothing
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        _make_meta(td, "task-keep", status="success", age=7200)
        cfg = CleanupConfig({"enabled": False})
        removed, remaining, dp = cleanup_expired_task_dirs(td, cfg)
        check("Cleanup disabled: removed=0", removed == 0)
        check("Cleanup disabled: dir exists", Path(task_root_dir(td, "task-keep")).is_dir())

    # ═══════════════════════════════════════════════════════════
    # 10. Success dir older than retention is removed
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        _make_meta(td, "old-succ", status="success", age=7200)
        cfg = CleanupConfig({"keep_success_seconds": 3600})
        removed, remaining, dp = cleanup_expired_task_dirs(td, cfg)
        check("Old success dir removed", removed >= 1)

    # ═══════════════════════════════════════════════════════════
    # 11. Fresh success dir kept
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        _make_meta(td, "fresh-succ", status="success", age=600)
        cfg = CleanupConfig({"keep_success_seconds": 3600})
        removed, remaining, dp = cleanup_expired_task_dirs(td, cfg)
        check("Fresh success dir kept", removed == 0)
        check("Fresh dir still exists", Path(task_root_dir(td, "fresh-succ")).is_dir())

    # ═══════════════════════════════════════════════════════════
    # 12. Failed dir kept when cleanup_failed=False
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        _make_meta(td, "old-fail", status="failed", age=7200)
        cfg = CleanupConfig({"cleanup_failed": False, "cleanup_success": True,
                             "keep_success_seconds": 3600})
        removed, remaining, dp = cleanup_expired_task_dirs(td, cfg)
        check("Failed dir NOT removed when cleanup_failed=False", removed == 0)

    # ═══════════════════════════════════════════════════════════
    # 13. Failed dir removed when cleanup_failed=True + expired
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        _make_meta(td, "exp-fail", status="failed", age=90000)
        cfg = CleanupConfig({"cleanup_failed": True, "keep_failed_seconds": 86400})
        removed, remaining, dp = cleanup_expired_task_dirs(td, cfg)
        check("Expired failed dir removed when cleanup_failed=True", removed >= 1)

    # ═══════════════════════════════════════════════════════════
    # 14. Orphan dir (no meta) uses longest retention
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        # Create a dir without meta.json
        orphan = Path(td) / TASKS_DIR / "orphan-task"
        orphan.mkdir(parents=True, exist_ok=True)
        (orphan / "data.txt").write_text("some data")
        old = time.time() - 90000  # > 7 days orphan threshold
        os.utime(str(orphan), (old, old))

        cfg = CleanupConfig({"cleanup_success": True, "keep_success_seconds": 3600})
        removed, remaining, dp = cleanup_expired_task_dirs(td, cfg)
        check("Old orphan removed (age > ORPHAN_MAX_AGE)", removed >= 1)

    # ═══════════════════════════════════════════════════════════
    # 15. Running task dir NOT removed
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        _make_meta(td, "running-task", status="success", age=7200)
        cfg = CleanupConfig({"keep_success_seconds": 3600})
        removed, remaining, dp = cleanup_expired_task_dirs(
            td, cfg, running_task_ids={"running-task"},
        )
        check("Running task dir NOT removed", removed == 0)
        check("Running task dir still exists",
              Path(task_root_dir(td, "running-task")).is_dir())

    # ═══════════════════════════════════════════════════════════
    # 16. task_id=../../etc rejected
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        result = cleanup_task_dir(td, "../../etc", reason="test")
        check("Path traversal task_id rejected", not result)

        result = cleanup_task_dir(td, "valid-unknown", reason="test")
        check("Non-existent valid task_id returns False", not result)

    # ═══════════════════════════════════════════════════════════
    # 17. Hermes workspace excluded from cleanup
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        # Create what looks like a task dir but is a hermes workspace
        hermes_root = Path(td) / TASKS_DIR / "hermes-ws"
        hermes_root.mkdir(parents=True, exist_ok=True)
        (hermes_root / "important.txt").write_text("data")
        os.utime(str(hermes_root), (time.time() - 7200, time.time() - 7200))

        # Real task dir
        _make_meta(td, "real-task", status="success", age=7200)

        cfg = CleanupConfig({"keep_success_seconds": 3600})
        removed, remaining, dp = cleanup_expired_task_dirs(
            td, cfg, allowed_workspaces=[str(hermes_root.resolve())],
        )
        check("Hermes workspace dir NOT removed", hermes_root.is_dir())
        check("Real task dir was removed", not (Path(td) / TASKS_DIR / "real-task").exists())

    # ═══════════════════════════════════════════════════════════
    # 18. Size eviction
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        for i in range(3):
            tdir = _make_meta(td, f"size-task-{i}", status="success", age=7200 + i * 10)
            # Add 10KB to each
            (Path(tdir) / WORK_DIR_SUB / "data.bin").write_bytes(b"x" * 10_000)

        cfg = CleanupConfig({
            "cleanup_success": True,
            "cleanup_failed": False,
            "cleanup_timeout": False,
            "keep_success_seconds": 3600,
            "max_work_dir_size_mb": 0.015,  # ~15KB
        })
        removed, remaining, dp = cleanup_expired_task_dirs(td, cfg)
        check("Size eviction removed some dirs", removed >= 1)

    # ═══════════════════════════════════════════════════════════
    # 19. Disk pressure status reporting (with size eviction)
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        # Create TWO dirs, each 50MB, both within retention.
        # Even after eviction removes the oldest, the other stays over limit.
        for suffix, age_minutes in [("old", 200), ("young", 50)]:
            tdir = Path(task_root_dir(td, f"big-task-{suffix}"))
            (tdir / WORK_DIR_SUB).mkdir(parents=True, exist_ok=True)
            write_task_meta(td, f"big-task-{suffix}", status="success")
            (tdir / WORK_DIR_SUB / "big.bin").write_bytes(b"x" * 50_000_000)
            recent = time.time() - age_minutes
            os.utime(str(tdir), (recent, recent))
            for f in tdir.rglob("*"):
                if f.is_file():
                    os.utime(str(f), (recent, recent))

        cfg = CleanupConfig({
            "cleanup_success": True,
            "cleanup_failed": False,
            "cleanup_timeout": False,
            "keep_success_seconds": 86400,  # keep 24h — both are young
            "max_work_dir_size_mb": 0.001,  # ~1KB — max 1KB allowed
        })
        removed, remaining, dp = cleanup_expired_task_dirs(td, cfg)
        # Time-based should keep both (young), size-based should evict oldest
        # But even after evicting one, the other still exceeds the limit
        check("Disk pressure detected when over limit", dp["disk_pressure"] is True)
        check("Cleanup warning message present",
              dp["cleanup_warning"] is not None and "exceeds" in dp["cleanup_warning"])
        check("Work dir size reported > 0", dp["work_dir_size_mb"] > 0)

    # ═══════════════════════════════════════════════════════════
    # 20. get_work_dir_size / get_tasks_dir_size
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        sz0 = get_work_dir_size(td)
        check("Empty work_dir size is 0", sz0 == 0.0)

        _make_meta(td, "sizer", status="success", age=100)
        sz1 = get_work_dir_size(td)
        check("Non-empty work_dir size > 0", sz1 > 0)

        sz_tasks = get_tasks_dir_size(td)
        check("tasks/ dir size > 0", sz_tasks > 0)

    # ═══════════════════════════════════════════════════════════
    # 21. meta.json with cleanup_after field
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        write_task_meta(td, "clean-later", status="success",
                        cleanup_after=time.time() - 10)
        tdir = Path(task_root_dir(td, "clean-later"))
        (tdir / WORK_DIR_SUB).mkdir(parents=True, exist_ok=True)
        (tdir / WORK_DIR_SUB / "x.txt").write_text("data")
        old = time.time() - 100
        os.utime(str(tdir), (old, old))

        cfg = CleanupConfig({"cleanup_success": True, "keep_success_seconds": 86400})
        removed, remaining, dp = cleanup_expired_task_dirs(td, cfg)
        check("cleanup_after overrides keep_success_seconds", removed >= 1)

    # ═══════════════════════════════════════════════════════════
    # 22. Size eviction does NOT delete ALL eligible dirs
    #     (Bug fix: _dir_size_bytes must be computed BEFORE deletion)
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        # Create 5 success dirs, each ~100KB, all within retention
        tdirs = []
        for i in range(5):
            tdir = Path(task_root_dir(td, f"big-{i}"))
            (tdir / WORK_DIR_SUB).mkdir(parents=True, exist_ok=True)
            write_task_meta(td, f"big-{i}", status="success")
            (tdir / WORK_DIR_SUB / "data.bin").write_bytes(b"x" * 100_000)
            old = time.time() - 2000 + i * 10
            os.utime(str(tdir), (old, old))
            tdirs.append(tdir)

        cfg = CleanupConfig({
            "cleanup_success": True,
            "keep_success_seconds": 86400,  # all within retention
            "max_work_dir_size_mb": 0.3,    # ~300KB — should keep ~2-3 dirs
            "delete_empty_dirs": False,
        })
        removed, remaining, dp = cleanup_expired_task_dirs(td, cfg)

        check("Size eviction: removed some but NOT all dirs",
              0 < removed < 5)
        check("Size eviction: remaining dirs still exist",
              remaining > 0 and remaining == 5 - removed)

    # ═══════════════════════════════════════════════════════════
    # 23. Size eviction priority: success deleted before failed
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        # One success dir (100KB), one failed dir (100KB)
        tdir_s = Path(task_root_dir(td, "succ-task"))
        (tdir_s / WORK_DIR_SUB).mkdir(parents=True, exist_ok=True)
        write_task_meta(td, "succ-task", status="success")
        (tdir_s / WORK_DIR_SUB / "data.bin").write_bytes(b"x" * 100_000)

        tdir_f = Path(task_root_dir(td, "fail-task"))
        (tdir_f / WORK_DIR_SUB).mkdir(parents=True, exist_ok=True)
        write_task_meta(td, "fail-task", status="failed")
        (tdir_f / WORK_DIR_SUB / "data.bin").write_bytes(b"x" * 100_000)

        old = time.time() - 2000
        os.utime(str(tdir_s), (old, old))
        os.utime(str(tdir_f), (old, old))

        # Only allow 150KB total — 200KB total, need to drop one
        cfg = CleanupConfig({
            "cleanup_success": True,
            "cleanup_failed": False,  # don't cleanup failed
            "keep_success_seconds": 86400,
            "max_work_dir_size_mb": 0.15,  # 150KB
            "delete_empty_dirs": False,
        })
        removed, remaining, dp = cleanup_expired_task_dirs(td, cfg)
        # Success dir should be removed first; failed kept
        check("Size eviction: success dir removed before failed when cleanup_failed=False",
              removed >= 1)
        check("Size eviction: failed dir still exists (cleanup_failed=False)",
              (Path(task_root_dir(td, "fail-task")) / WORK_DIR_SUB / "data.bin").exists())

    # ═══════════════════════════════════════════════════════════
    # 24. ShellExecutor refuses invalid task_id
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        from dispatch_compute_server.executors.shell_executor import ShellExecutor
        executor = ShellExecutor()

        # ../evil
        task = {
            "task_id": "../evil",
            "timeout_seconds": 30,
            "payload": {"execution": {"mode": "shell", "command": "echo hacked"}},
        }
        result = asyncio_run(executor.run(task, task["payload"]["execution"], td, td))
        check("ShellExecutor: ../evil task_id rejected", not result.get("success"))
        check("ShellExecutor: ../evil error message mentions invalid or rejected",
              "invalid" in result.get("error", "").lower() or
              "rejected" in result.get("error", "").lower())

        # Verify no directory was created outside tasks/
        check("ShellExecutor: ../evil didn't create dir outside tasks/",
              not (Path(td) / ".." / "evil").exists())

        # task_id with slash
        task2 = {
            "task_id": "a/b",
            "timeout_seconds": 30,
            "payload": {"execution": {"mode": "shell", "command": "echo test"}},
        }
        result2 = asyncio_run(executor.run(task2, task2["payload"]["execution"], td, td))
        check("ShellExecutor: a/b task_id rejected", not result2.get("success"))

    # ═══════════════════════════════════════════════════════════
    # 25. Size eviction: only expired success dirs deleted when
    #     cleanup_failed/cleanup_timeout are false
    # ═══════════════════════════════════════════════════════════
    with tempfile.TemporaryDirectory() as td:
        # Two dirs: expired success (2h old, keep=1h), old failed (2h old)
        tdir_s = Path(task_root_dir(td, "old-succ"))
        (tdir_s / WORK_DIR_SUB).mkdir(parents=True, exist_ok=True)
        write_task_meta(td, "old-succ", status="success")
        (tdir_s / WORK_DIR_SUB / "data.bin").write_bytes(b"x" * 100_000)
        old = time.time() - 7200
        os.utime(str(tdir_s), (old, old))

        tdir_f = Path(task_root_dir(td, "old-fail"))
        (tdir_f / WORK_DIR_SUB).mkdir(parents=True, exist_ok=True)
        write_task_meta(td, "old-fail", status="failed")
        (tdir_f / WORK_DIR_SUB / "data.bin").write_bytes(b"x" * 100_000)
        os.utime(str(tdir_f), (old, old))

        # Strict limit to trigger size eviction
        cfg = CleanupConfig({
            "cleanup_success": True,
            "cleanup_failed": False,  # failed NOT eligible
            "keep_success_seconds": 3600,
            "max_work_dir_size_mb": 0.05,  # 50KB — both dirs are 100KB each
            "delete_empty_dirs": False,
        })
        removed, remaining, dp = cleanup_expired_task_dirs(td, cfg)
        check("Size priority: expired success removed (beyond retention)",
              not (Path(task_root_dir(td, "old-succ")) / WORK_DIR_SUB / "data.bin").exists())
        check("Size priority: failed kept (cleanup_failed=False)",
              (Path(task_root_dir(td, "old-fail")) / WORK_DIR_SUB / "data.bin").exists())

    # ═══════════════════════════════════════════════════════════
    # Summary
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'=' * 60}")
    print(f"RESULTS: {passed}/{total} tests passed "
          + ("\u2705" if passed == total else "\u274c"))
    print(f"{'=' * 60}")
    return passed == total


def asyncio_run(coro):
    """Run a coroutine synchronously (for test helpers)."""
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
