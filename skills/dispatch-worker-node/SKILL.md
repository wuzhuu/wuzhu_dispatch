---
name: dispatch-worker-node
description: wuzhu-dispatch 计算节点（Worker）管理 skill — 节点注册、服务维护、配置验证、任务清理
---

# dispatch-worker-node Skill

管理 wuzhu-dispatch 集群中的计算节点（Worker）。适用于所有安装了 Compute Server 的机器。

## 前提

- 已安装 Compute Server（systemd `wuzhu-compute-server.service`）
- 节点配置文件 `~/.config/wuzhu-dispatch/node.yaml`
- `dispatch_client` 包已安装：`pip install -e ~/wuzhu-dispatch/client/`

## 配置验证

```python
from dispatch_client.config_skill import validate_node_yaml, check_dispatcher_connectivity

# 验证 node.yaml 配置
report = validate_node_yaml("~/.config/wuzhu-dispatch/node.yaml")
print(report.summary)
for check in report.checks:
    print(f"  {'✅' if check.ok else '❌'} {check.name}: {check.message}")
for warn in report.warnings:
    print(f"  ⚠️ {warn}")

# 检查 Dispatcher 连通性
health = check_dispatcher_connectivity("<dispatcher_url>")
print(f"Dispatcher: {'✅' if health.ok else '❌'} {health.summary}")
```

## 服务管理

```bash
# 查看服务状态
sudo systemctl status wuzhu-compute-server --no-pager -l

# 查看日志
sudo journalctl -u wuzhu-compute-server -n 50 --no-pager -l

# 重启服务
sudo systemctl restart wuzhu-compute-server

# 启动/停止
sudo systemctl start wuzhu-compute-server
sudo systemctl stop wuzhu-compute-server
```

## 工作目录清理

```bash
# 查看工作目录大小
du -sh ~/wuzhu-dispatch/work/

# 列出各任务目录
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

计算节点通过长轮询主动从 Dispatcher 拉取任务，无需手动操作。

```bash
# 查看当前进程
ps aux | grep dispatch_compute_server

# 查看实时日志
tail -f ~/wuzhu-dispatch/logs/agent.log
```

## 节点更新

拉取最新代码并重启：

```bash
cd ~/wuzhu-dispatch && git pull origin master
pip install -e client/
sudo systemctl restart wuzhu-compute-server
```

## 配置示例

```yaml
# ~/.config/wuzhu-dispatch/node.yaml
dispatcher_url: "http://<dispatcher_ip>:8000"

node_id: "my-node"
agent_token: "<agent_token>"

name: "My Worker Node"
region: "HK"
provider: "MyProvider"

tags:
  - hk
  - python
  - shell

static_profile:
  cpu_cores: 2
  memory_mb: 2048
  runtime:
    shell: true
    python: true
    docker: false
    hermes: false
  limits:
    max_parallel_tasks: 3
```
