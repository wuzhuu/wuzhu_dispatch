---
name: dispatch-client
description: wuzhu-dispatch 分布式调度 skill — 向任意节点派发 shell/hermes 任务，查询结果，按标签调度
---

# dispatch-client Skill

向 wuzhu-dispatch 集群中任意节点派发任务。**一份代码，所有节点统一使用。**

## 统一调用约定

所有节点遵循以下规则，新设备部署后无需额外适配：

| 约定 | 说明 |
|------|------|
| **Python** 始终用 `python3` | 所有节点可用，不依赖 `python` 别名或 venv 路径 |
| **Shell 命令** 写 POSIX sh 兼容语法 | Compute Server 的 Shell Executor 使用 `/bin/sh` 而非 bash |
| 如需 bash 特性 | 命令开头加 `bash -c '...'` |
| **dispatch-client 包** 必须 pip 安装 | 否则 `from dispatch_client import ...` 不可用 |
| **~** 始终指向当前用户 HOME | 各节点 home 不同，但 `~` 自动解析正确 |
| **client.yaml** 放 `~/.config/wuzhu-dispatch/client.yaml` | 各节点统一路径 |

## 新设备部署（从 GitHub 开始的一键流）

```bash
# 1. 克隆仓库
git clone https://github.com/wuzhuu/wuzhu_dispatch.git ~/wuzhu-dispatch
cd ~/wuzhu-dispatch

# 2. 安装 dispatch-client 包
pip3 install --break-system-packages -e client/

# 3. 配置 client.yaml
mkdir -p ~/.config/wuzhu-dispatch
cat > ~/.config/wuzhu-dispatch/client.yaml << 'EOF'
dispatcher_url: "http://100.105.186.31:8000"
client_token: "your-admin-token-here"
EOF
chmod 600 ~/.config/wuzhu-dispatch/client.yaml

# 4. 安装 Hermes skill
mkdir -p ~/.hermes/skills/software-development/dispatch-client
cp skills/dispatch-client/SKILL.md ~/.hermes/skills/software-development/dispatch-client/

# 5. 验证
python3 -c "from dispatch_client.client import DispatchClient; print('✅ dispatch-client OK')"
```

## 前置条件

- `dispatch-client` 包已安装：`pip3 install -e ~/wuzhu-dispatch/client/`
- `~/.config/wuzhu-dispatch/client.yaml` 配置文件存在
- client token 对应的用户角色为 **admin**（shell/hermes 任务需要）

## 权限要求

| 任务类型 | 所需角色 |
|----------|---------|
| template 类 | operator+ |
| **shell** | **admin+** |
| **hermes** | **admin+** |

## 调度示例

### Shell 任务到指定节点

```python
import requests, time

TOKEN = open("/root/.config/wuzhu-dispatch/client.yaml").read().split(":")[1].strip().strip('"')
BASE = "http://100.105.186.31:8000"
HDR = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

body = {
    "type": "shell",
    "payload": {
        "execution": {"mode": "shell", "command": "hostname && uptime"}
    },
    "priority": 50,
    "timeout_seconds": 30,
    "target": {"mode": "node", "node_id": "hk99"}
}

r = requests.post(f"{BASE}/api/v1/client/tasks", json=body, headers=HDR)
task_id = r.json()["task_id"]

for _ in range(15):
    r2 = requests.get(f"{BASE}/api/v1/client/tasks/{task_id}", headers=HDR)
    t = r2.json()
    if t["status"] == "success":
        print(t["result"]["stdout"])
        break
    if t["status"] in ("failed", "timeout"):
        print(f"❌ {t['result'].get('error','')}")
        break
    time.sleep(1)
```

### 按标签调度

```python
# 调度到香港节点
body["target"] = {"mode": "tags", "tags": ["hk"]}
# 调度到有 hermes 的节点
body["target"] = {"mode": "tags", "tags": ["hermes_worker"]}
# 调度到国外节点
body["target"] = {"mode": "tags", "tags": ["foreign_reachable"]}
```

### 快速函数封装（统一调用）

```python
import requests, time

def sh(node, command, timeout=30):
    """统一 shell 调度函数 — 在所有节点行为一致"""
    TOKEN = open("/root/.config/wuzhu-dispatch/client.yaml").read().split(":")[1].strip().strip('"')
    HDR = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    body = {
        "payload": {"execution": {"mode": "shell", "command": command}},
        "target": {"mode": "node", "node_id": node},
        "timeout_seconds": timeout,
    }
    r = requests.post("http://100.105.186.31:8000/api/v1/client/tasks",
                      json=body, headers=HDR)
    task_id = r.json()["task_id"]
    for _ in range(timeout):
        r2 = requests.get(f"http://100.105.186.31:8000/api/v1/client/tasks/{task_id}", headers=HDR)
        t = r2.json()
        if t["status"] == "success":
            return t["result"]["stdout"]
        if t["status"] in ("failed", "timeout"):
            return f"❌ {t['status']}: {t.get('result',{}).get('error','')}"
        time.sleep(1)
    return "⏰ timeout"

# 统一用法 — 任何节点都这样调用
print(sh("hk99", "free -h"))
print(sh("lacloud", "df -h /"))
print(sh("gecloud", "hostname && python3 --version"))
```

### Hermes 任务模式

```python
body = {
    "payload": {
        "execution": {
            "mode": "hermes",
            "prompt": "列出 /root 目录的前10个文件",
        }
    },
    "target": {"mode": "node", "node_id": "hk99"},
    "timeout_seconds": 120,
}
```

## 查看在线节点

```bash
curl -s http://100.105.186.31:8000/api/v1/admin/nodes \
  -H "Authorization: Bearer $(python3 -c "import yaml; print(yaml.safe_load(open('/root/.config/wuzhu-dispatch/client.yaml'))['client_token'])")"
```

## 可用模板

```bash
curl -s http://100.105.186.31:8000/api/v1/client/templates \
  -H "Authorization: Bearer $(python3 -c "import yaml; print(yaml.safe_load(open('/root/.config/wuzhu-dispatch/client.yaml'))['client_token'])")"
```
