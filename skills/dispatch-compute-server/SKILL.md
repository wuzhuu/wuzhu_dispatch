---
name: dispatch-compute-server
description: wuzhu-dispatch 服务端（计算节点）管理 skill — 节点注册、任务执行、清理维护、配置验证
---

# dispatch-compute-server Skill

管理 wuzhu-dispatch 的计算节点（Compute Server）。适用于所有注册为计算节点的机器。

## 前提

- 已在节点部署 Compute Server（systemd `wuzhu-compute-server.service`）
- 节点配置文件 `node.yaml` 在 `~/.config/wuzhu-dispatch/node.yaml` 或 `~/wuzhu-dispatch/node.yaml`
- dispatch_client 包已安装：`pip install -e ~/wuzhu-dispatch/client/`

## 配置验证

```python
from dispatch_client.config_skill import validate_node_yaml, check_dispatcher_connectivity

# 验证 node.yaml 配置
report = validate_node_yaml("~/wuzhu-dispatch/node.yaml")
print(report.summary)
for check in report.checks:
    print(f"  {'✅' if check.ok else '❌'} {check.name}: {check.message}")
for warn in report.warnings:
    print(f"  ⚠️ {warn}")

# 检查 Dispatcher 连通性
health = check_dispatcher_connectivity("http://100.105.186.31:8000")
print(f"Dispatcher: {'✅' if health.ok else '❌'} {health.summary}")
```

## 服务管理

```bash
# 查看服务状态
sudo systemctl status wuzhu-compute-server --no-pager -l

# 查看日志
sudo journalctl -u wuzhu-compute-server -n 50 --no-pager -l

# 重启
sudo systemctl restart wuzhu-compute-server

# 启动/停止
sudo systemctl start wuzhu-compute-server
sudo systemctl stop wuzhu-compute-server
```

## 工作目录管理

```bash
# 查看工作目录大小
du -sh ~/wuzhu-dispatch/work/

# 查看各任务目录
ls -la ~/wuzhu-dispatch/work/tasks/

# 手动清理过期任务目录
python3 -c "
from dispatch_compute_server.cleanup import cleanup_expired_task_dirs
cleaned = cleanup_expired_task_dirs(
    work_dir='~/wuzhu-dispatch/work',
    keep_success_seconds=3600,
    keep_failed_seconds=86400,
)
print(f'Cleaned {cleaned} expired directories')
"
```

## 任务执行

计算节点通过长轮询主动从 Dispatcher 拉取任务。无需手动操作。

```bash
# 查看当前正在执行的任务
ps aux | grep dispatch_compute_server
```

## 节点注册

新节点注册到 Dispatcher：

```python
from dispatch_client.config_skill import generate_node_yaml, register_node_via_api

# 生成 node.yaml
yaml_str = generate_node_yaml(
    profile="general",          # general / hermes-worker / bandwidth-node / small-probe
    node_id="my-node",
    agent_token="CHANGE_ME",
    dispatcher_url="http://100.105.186.31:8000",
    name="My Node Name",
    region="HK",
    provider="MyProvider",
)
print(yaml_str)

# 通过 API 注册（需要 DISPATCH_SERVER_SECRET 或 admin token）
result = register_node_via_api(
    dispatcher_url="http://100.105.186.31:8000",
    admin_token="<DISPATCH_SERVER_SECRET>",
    node_config={"node_id": "my-node", "agent_token": "...", ...},
)
print(result)
```

## 节点画像

配置 `node.yaml` 中的 `static_profile`：

```yaml
static_profile:
  cpu_cores: 4
  memory_mb: 8192
  disk_gb: 100
  bandwidth_mbps: 1000
  public_ipv4: true
  public_ipv6: false
  has_ipv4: true
  has_ipv6: false
  nat_type: "none"        # none / CGNAT / symmetric
  cn_reachable: "native"  # native / via-proxy / no
  foreign_reachable: "direct"
  runtime:
    shell: true
    docker: false
    hermes: true
    python: true
  limits:
    max_parallel_tasks: 3
    allow_heavy_compute: true
    allow_heavy_download: false
```
