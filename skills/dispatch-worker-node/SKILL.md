---
name: dispatch-worker-node
description: wuzhu-dispatch 计算节点（Worker）管理 skill — 节点部署、服务维护、配置验证、任务清理
---

# dispatch-worker-node Skill

管理 wuzhu-dispatch 集群中的计算节点（Worker）。**一份代码，所有节点统一使用。**

## 统一调用约定

所有节点遵循以下规则，新设备部署后无需额外适配：

| 约定 | 说明 |
|------|------|
| **Python** 始终用 `python3` | 所有节点可用，不依赖 `python` 别名或 venv 路径 |
| **Shell 命令** 写 POSIX sh 兼容语法 | Compute Server 的 Shell Executor 使用 `/bin/sh` 而非 bash |
| 如需 bash 特性 | 命令开头加 `bash -c '...'` |
| **dispatch-client 包** 必须 pip 安装 | 否则 `from dispatch_client import ...` 不可用 |
| **node.yaml** 放 `~/.config/wuzhu-dispatch/node.yaml` | 各节点统一路径 |
| **~** 始终指向当前用户 HOME | 各节点 home 不同，但 `~` 自动解析正确 |

## 新设备部署（从 GitHub 开始的一键流）

```bash
# 1. 克隆仓库
git clone https://github.com/wuzhuu/wuzhu_dispatch.git ~/wuzhu-dispatch
cd ~/wuzhu-dispatch

# 2. 安装 dispatch-client 包（worker 也需要，config_skill 依赖它）
pip3 install --break-system-packages -e client/

# 3. 安装 dispatch-worker-node Hermes skill
mkdir -p ~/.hermes/skills/software-development/dispatch-worker-node
cp skills/dispatch-worker-node/SKILL.md ~/.hermes/skills/software-development/dispatch-worker-node/

# 4. 配置 node.yaml
mkdir -p ~/.config/wuzhu-dispatch
cat > ~/.config/wuzhu-dispatch/node.yaml << 'EOF'
dispatcher_url: "http://100.105.186.31:8000"
node_id: "my-new-node"
agent_token: "CHANGE_ME"
name: "My Worker Node"
region: "HK"
provider: "MyProvider"
tags:
  - shell
  - python
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
EOF
chmod 600 ~/.config/wuzhu-dispatch/node.yaml

# 5. 安装 compute-server
pip3 install --break-system-packages -e compute-server/

# 6. 验证
python3 -c "from dispatch_client.config_skill import validate_node_yaml; print('✅ node.yaml valid')"
python3 -c "from dispatch_compute_server.executors.shell_executor import ShellExecutor; print('✅ compute-server OK')"
```

## 前置条件

- Compute Server 已安装：`pip3 install -e ~/wuzhu-dispatch/compute-server/`
- 节点配置文件 `~/.config/wuzhu-dispatch/node.yaml`
- 节点已在 Dispatcher 注册（admin 预注册或自动注册）

## 配置验证

```python
from dispatch_client.config_skill import validate_node_yaml, check_dispatcher_connectivity

# 验证 node.yaml 配置 — 统一用 python3 执行
report = validate_node_yaml("~/.config/wuzhu-dispatch/node.yaml")
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

# 重启（更新配置后）
sudo systemctl restart wuzhu-compute-server

# 启动/停止
sudo systemctl start wuzhu-compute-server
sudo systemctl stop wuzhu-compute-server
```

## 工作目录清理

```bash
# 查看工作目录大小
du -sh /opt/wuzhu-dispatch/work/

# 列出各任务目录
ls -la /opt/wuzhu-dispatch/work/tasks/

# 手动清理 — python3 统一调用
python3 << 'EOF'
from dispatch_compute_server.cleanup import cleanup_expired_task_dirs
cleaned = cleanup_expired_task_dirs(
    work_dir='/opt/wuzhu-dispatch/work',
    keep_success_seconds=3600,
    keep_failed_seconds=86400,
)
print(f'Cleaned {cleaned} expired directories')
EOF
```

## 节点更新（从 GitHub 拉取最新代码）

```bash
cd ~/wuzhu-dispatch && git pull origin master
pip3 install --break-system-packages -e client/
pip3 install --break-system-packages -e compute-server/
sudo systemctl restart wuzhu-compute-server
```

## 节点注册

新节点注册到 Dispatcher：

```python
from dispatch_client.config_skill import generate_node_yaml, register_node_via_api

# 生成 node.yaml 内容
yaml_str = generate_node_yaml(
    profile="general",
    node_id="my-node",
    agent_token="CHANGE_ME",
    dispatcher_url="http://100.105.186.31:8000",
    name="My Node Name",
    region="HK",
    provider="MyProvider",
)
print(yaml_str)

# 通过 API 注册（需要 admin token）
result = register_node_via_api(
    dispatcher_url="http://100.105.186.31:8000",
    admin_token="<DISPATCH_SERVER_SECRET>",
    node_config={"node_id": "my-node", "agent_token": "...", ...},
)
print(result)
```
