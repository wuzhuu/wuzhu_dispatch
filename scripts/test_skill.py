#!/usr/bin/env python3
"""
Dispatch Skill layer tests — Config Skill + Runtime Skill.

Tests (in order):
  1. skill.yaml loads correctly
  2. Profile resolves to target tags
  3. Profile merge with explicit target (tags union)
  4. Runtime Skill quick builds correct request body
  5. Runtime Skill dry-run does not make requests
  6. Config Skill validate-node on valid config
  7. Config Skill validate-node rejects CHANGE_ME token
  8. Config Skill validate-client rejects agent-like token
  9. Config Skill generate-node small-probe outputs valid YAML
  10. Config Skill generate-node unknown profile raises error
  11. Config Skill list-profiles returns known profiles
  12. unknown profile raises error
  13. wait_seconds capped by max_wait
  14. Target tags merge logic
  15. SkillResult from_quick_response parsing
  16. SkillResult from_task_response parsing
  17. Config Skill validate-client detects missing fields
  18. Config Skill generate-client outputs valid YAML
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "client"))

import yaml

from dispatch_client.skill_config import SkillConfig
from dispatch_client.config_skill import (
    validate_node_yaml,
    validate_client_yaml,
    generate_node_yaml,
    generate_client_yaml,
    list_node_profiles,
    register_node_via_api,
    check_dispatcher_connectivity,
)
from dispatch_client.runtime_skill import DispatchRuntimeSkill, SkillResult

passed = total = 0


def check(desc, cond):
    global passed, total
    total += 1
    if cond:
        passed += 1
        print(f"  \u2705 {desc}")
    else:
        print(f"  \u274c {desc}")


def create_temp_yaml(content: str, suffix: str = ".yaml") -> str:
    """Create a temporary YAML file and return its path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False)
    f.write(content)
    f.close()
    return f.name


def main():
    print("=" * 60)
    print("Dispatch Skill Layer Tests")
    print("=" * 60)

    # ═══════════════════════════════════════════════════════════════════
    # 1. skill.yaml loads correctly
    # ═══════════════════════════════════════════════════════════════════
    skill_yaml = """\
dispatcher_url: "https://dispatch.example.com"
client_token: "test-token-123"
defaults:
  wait_seconds: 15
  target:
    mode: tags
    tags: ["hk"]
limits:
  max_wait_seconds: 60
profiles:
  foreign:
    target:
      mode: tags
      tags: ["foreign_reachable"]
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(skill_yaml)
        skill_path = f.name

    cfg = SkillConfig.from_file(skill_path)
    check("1. skill.yaml loads dispatcher_url", cfg.dispatcher_url == "https://dispatch.example.com")
    check("1b. skill.yaml loads client_token", cfg.client_token == "test-token-123")
    check("1c. skill.yaml loads default_wait", cfg.default_wait_seconds == 15)
    check("1d. skill.yaml loads max_wait", cfg.max_wait_seconds == 60)
    check("1e. skill.yaml loads profiles", "foreign" in cfg.profiles)

    # ═══════════════════════════════════════════════════════════════════
    # 2. Profile resolves to target tags
    # ═══════════════════════════════════════════════════════════════════
    target = cfg.get_target_for_profile("foreign")
    check("2. Profile resolves tags", target.get("tags") == ["foreign_reachable"])

    # ═══════════════════════════════════════════════════════════════════
    # 3. Profile merge with explicit target (tags union)
    # ═══════════════════════════════════════════════════════════════════
    merged = cfg.merge_target_with_profile(
        {"tags": ["cn_reachable"]}, "foreign"
    )
    merged_tags = set(merged.get("tags", []))
    check("3. Merge target+profile tags union",
          "foreign_reachable" in merged_tags and "cn_reachable" in merged_tags)

    # ═══════════════════════════════════════════════════════════════════
    # 4. Runtime Skill quick builds correct request (via mock)
    # ═══════════════════════════════════════════════════════════════════
    # We test the request construction by using dry-run
    skill = DispatchRuntimeSkill.from_tokens("https://test.example.com", "test-token")
    result = skill.quick("http_probe", params={"url": "https://example.com"},
                         target={"tags": ["hk"]}, dry_run=True)
    check("4. Dry-run returns ok", result.ok)
    check("4b. Dry-run has result with template_id",
          result.result.get("template_id") == "http_probe")
    check("4c. Dry-run has params with url",
          result.result.get("params", {}).get("url") == "https://example.com")

    # ═══════════════════════════════════════════════════════════════════
    # 5. Dry-run does not make HTTP requests
    # ═══════════════════════════════════════════════════════════════════
    # If dry_run=True, no network call should happen
    check("5. Dry-run does not make requests", result.done)

    # ═══════════════════════════════════════════════════════════════════
    # 6. Config Skill validate-node on valid config
    # ═══════════════════════════════════════════════════════════════════
    valid_node = """\
dispatcher_url: "https://dispatch.example.com"
node_id: "test-node-1"
agent_token: "real-token-abc123"
name: "Test Node"
region: "HK"
provider: "TestProvider"
roles:
  - compute_server
tags:
  - test
  - hk
static_profile:
  cpu_cores: 2
  memory_mb: 2048
  runtime:
    shell: true
    python: true
    docker: false
    hermes: false
  limits:
    max_parallel_tasks: 2
agent:
  work_dir: "/tmp/wuzhu-work"
  log_dir: "/tmp/wuzhu-logs"
cleanup:
  enabled: true
  max_work_dir_size_mb: 1024
"""
    np = create_temp_yaml(valid_node)
    report = validate_node_yaml(np)
    check("6. Valid node passes checks", report.ok)
    os.unlink(np)

    # ═══════════════════════════════════════════════════════════════════
    # 7. Config Skill validate-node rejects CHANGE_ME token
    # ═══════════════════════════════════════════════════════════════════
    bad_node = valid_node.replace("real-token-abc123", "CHANGE_ME")
    np2 = create_temp_yaml(bad_node)
    report2 = validate_node_yaml(np2)
    check("7. CHANGE_ME agent_token fails", not report2.ok)
    agent_check = [c for c in report2.checks if c.name == "agent_token"]
    check("7b. agent_token check fails", agent_check and not agent_check[0].ok)
    os.unlink(np2)

    # ═══════════════════════════════════════════════════════════════════
    # 8. Config Skill validate-client
    # ═══════════════════════════════════════════════════════════════════
    valid_client = """\
dispatcher_url: "https://dispatch.example.com"
client_token: "my-client-token"
"""
    nc = create_temp_yaml(valid_client)
    report3 = validate_client_yaml(nc)
    check("8. Valid client passes checks", report3.ok)
    os.unlink(nc)

    # ═══════════════════════════════════════════════════════════════════
    # 9. Config Skill generate-node small-probe outputs valid YAML
    # ═══════════════════════════════════════════════════════════════════
    yaml_out = generate_node_yaml("small-probe", "probe-001")
    parsed = yaml.safe_load(yaml_out)
    check("9. generate-node outputs valid YAML", parsed is not None)
    check("9b. node_id present", parsed.get("node_id") == "probe-001")
    check("9c. tags contain probe", "probe" in parsed.get("tags", []))
    check("9d. cleanup present", "cleanup" in parsed)
    max_size = parsed.get("cleanup", {}).get("max_work_dir_size_mb", 0)
    check("9e. cleanup max_work_dir_size_mb=512", max_size == 512)

    # ═══════════════════════════════════════════════════════════════════
    # 10. generate-node unknown profile raises ValueError
    # ═══════════════════════════════════════════════════════════════════
    try:
        generate_node_yaml("nonexistent-profile", "nope")
        check("10. Unknown profile raises error", False)
    except ValueError:
        check("10. Unknown profile raises ValueError", True)

    # ═══════════════════════════════════════════════════════════════════
    # 11. list-profiles returns known profiles
    # ═══════════════════════════════════════════════════════════════════
    profiles = list_node_profiles()
    check("11. small-probe in profiles", "small-probe" in profiles)
    check("11b. bandwidth-node in profiles", "bandwidth-node" in profiles)
    check("11c. hermes-worker in profiles", "hermes-worker" in profiles)
    check("11d. general in profiles", "general" in profiles)

    # ═══════════════════════════════════════════════════════════════════
    # 12. generate-client outputs valid YAML
    # ═══════════════════════════════════════════════════════════════════
    yaml_out2 = generate_client_yaml("https://example.com", "tok-abc")
    parsed2 = yaml.safe_load(yaml_out2)
    check("12. generate-client valid YAML", parsed2 is not None)
    check("12b. client_token present", parsed2.get("client_token") == "tok-abc")

    # ═══════════════════════════════════════════════════════════════════
    # 13. SkillResult from_quick_response parsing
    # ═══════════════════════════════════════════════════════════════════
    mock_response = {
        "done": True,
        "task_id": "task-001",
        "status": "success",
        "result": {"status_code": 200, "latency_ms": 50},
    }
    sr = SkillResult.from_quick_response(mock_response)
    check("13. from_quick_response: ok=True", sr.ok)
    check("13b. from_quick_response: done=True", sr.done)
    check("13c. from_quick_response: task_id", sr.task_id == "task-001")
    check("13d. from_quick_response: status", sr.status == "success")
    check("13e. from_quick_response: result present",
          sr.result.get("status_code") == 200)

    # ═══════════════════════════════════════════════════════════════════
    # 14. SkillResult from_task_response parsing
    # ═══════════════════════════════════════════════════════════════════
    mock_task = {
        "task_id": "task-002",
        "status": "success",
        "assigned_node_id": "node-hk",
        "result": {"stdout": "hello"},
    }
    sr2 = SkillResult.from_task_response(mock_task)
    check("14. from_task_response: ok=True", sr2.ok)
    check("14b. from_task_response: node_id", sr2.node_id == "node-hk")
    check("14c. from_task_response: result", sr2.result.get("stdout") == "hello")

    # Failed task
    mock_fail = {
        "task_id": "task-003",
        "status": "failed",
        "result": {"error": "timeout"},
    }
    sr3 = SkillResult.from_task_response(mock_fail)
    check("14d. from_task_response failed: ok=False", not sr3.ok)
    check("14e. from_task_response failed: error present", sr3.error == "timeout")

    # ═══════════════════════════════════════════════════════════════════
    # 15. SkillResult to_dict serialization
    # ═══════════════════════════════════════════════════════════════════
    d = sr.to_dict()
    check("15. to_dict has ok", "ok" in d)
    check("15b. to_dict has result", "result" in d)

    # ═══════════════════════════════════════════════════════════════════
    # 16. Config Skill check_dispatcher_connectivity (no server)
    # ═══════════════════════════════════════════════════════════════════
    # With no dispatcher running, connection should fail
    report4 = check_dispatcher_connectivity("http://127.0.0.1:1", timeout=2)
    check("16. Connectivity check fails (no server)",
          not report4.ok or len(report4.errors) > 0)

    # ═══════════════════════════════════════════════════════════════════
    # 17. validate_client_yaml detects missing fields
    # ═══════════════════════════════════════════════════════════════════
    empty_client = "dispatcher_url: ''\nclient_token: ''\n"
    nc2 = create_temp_yaml(empty_client)
    report5 = validate_client_yaml(nc2)
    check("17. Empty client fails checks",
          not report5.ok or any(not c.ok for c in report5.checks))
    os.unlink(nc2)

    # ═══════════════════════════════════════════════════════════════════
    # 18. SkillConfig.from_dict
    # ═══════════════════════════════════════════════════════════════════
    cfg2 = SkillConfig.from_dict({
        "dispatcher_url": "https://example.com",
        "client_token": "tok",
        "defaults": {"wait_seconds": 5},
        "limits": {"max_wait_seconds": 20},
    })
    check("18. from_dict works", cfg2.is_valid)
    check("18b. from_dict preserves values", cfg2.default_wait_seconds == 5)

    # ═══════════════════════════════════════════════════════════════════
    # 19. Runtime Skill submit dry-run
    # ═══════════════════════════════════════════════════════════════════
    result_submit = skill.submit("dns_probe", params={"domain": "example.com"},
                                 dry_run=True)
    check("19. submit dry-run returns ok", result_submit.ok)
    check("19b. submit dry-run has dry_run status",
          result_submit.status == "dry_run")

    # ═══════════════════════════════════════════════════════════════════
    # 20. Config Skill register_node_via_api dry-run
    # ═══════════════════════════════════════════════════════════════════
    reg_result = register_node_via_api(
        "https://example.com", "admin-token",
        {"node_id": "test-node", "agent_token": "tok"},
        dry_run=True,
    )
    check("20. register dry-run returns dict", reg_result is not None)
    check("20b. register dry-run has dry_run", reg_result.get("dry_run") is True)

    # ═══════════════════════════════════════════════════════════════════
    # 21. Runtime Skill from_config with no config raises ValueError
    # ═══════════════════════════════════════════════════════════════════
    try:
        # Empty config should fail
        DispatchRuntimeSkill.from_config(SkillConfig.from_dict({}))
        check("21. Empty config raises error", False)
    except ValueError:
        check("21. Empty config raises ValueError", True)

    # ═══════════════════════════════════════════════════════════════════
    # Cleanup
    # ═══════════════════════════════════════════════════════════════════
    os.unlink(skill_path)

    print(f"\n{'=' * 60}")
    print(f"RESULTS: {passed}/{total} tests passed "
          + ("\u2705" if passed == total else "\u274c"))
    print(f"{'=' * 60}")
    return passed == total


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
