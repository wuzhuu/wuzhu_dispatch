---
name: dispatch-client
description: wuzhu-dispatch 分布式调度 skill — 向任意节点派发 shell/hermes 任务，查询结果，按标签调度
---

# dispatch-client Skill

通过 wuzhu-dispatch 向集群中任意节点派发任务。

## 前置

```bash
pip install -e ~/wuzhu-dispatch/client/
```

配置文件 `~/.config/wuzhu-dispatch/client.yaml`：
```yaml
dispatcher_url: "http://100.105.186.31:8000"
client_token: "<你的 admin/operator 级别 token>"
```

## 调度节点执行命令

```python
from dispatch_client.runtime_skill import DispatchRuntimeSkill
import json

skill = DispatchRuntimeSkill.from_tokens(
    dispatcher_url="http://100.105.186.31:8000",
    client_token="你的token",
)

# 提交 + 等待结果
body = {
    "type": "shell",
    "payload": {
        "execution": {
            "mode": "shell",
            "command": "hostname && uptime"
        }
    },
    "priority": 50,
    "timeout_seconds": 30,
    "target": {"mode": "node", "node_id": "hk99"}
}

import requests
r = requests.post(
    f"{skill.base_url}/api/v1/client/tasks",
    json=body,
    headers=skill._headers(),
)
task = r.json()
task_id = task["task_id"]

# 轮询等结果
import time
for _ in range(15):
    r2 = requests.get(
        f"{skill.base_url}/api/v1/client/tasks/{task_id}",
        headers=skill._headers(),
    )
    t = r2.json()
    if t["status"] in ("success", "failed", "timeout"):
        print(t["result"]["stdout"])
        break
    time.sleep(1)
```

## 按标签调度

```python
# 调度到所有有 hermes 的节点
body["target"] = {"mode": "tags", "tags": ["hermes_worker"]}

# 调度到国外节点
body["target"] = {"mode": "tags", "tags": ["foreign_reachable"]}

# 调度到香港节点
body["target"] = {"mode": "tags", "tags": ["hk"]}
```

## 快速函数封装

```python
def dispatch_shell(node, command, wait=True):
    """调度 shell 命令到指定节点"""
    body = {
        "type": "shell",
        "payload": {"execution": {"mode": "shell", "command": command}},
        "target": {"mode": "node", "node_id": node},
        "timeout_seconds": 30,
    }
    r = requests.post("http://100.105.186.31:8000/api/v1/client/tasks",
        json=body, headers={"Authorization": "Bearer 你的token"})
    task = r.json()
    if not wait:
        return task["task_id"]
    import time
    for _ in range(30):
        r2 = requests.get(f"http://100.105.186.31:8000/api/v1/client/tasks/{task['task_id']}",
            headers={"Authorization": "Bearer 你的token"})
        t = r2.json()
        if t["status"] == "success":
            return t["result"]["stdout"]
        if t["status"] in ("failed", "timeout"):
            return f"❌ {t['status']}: {t.get('result',{}).get('error','')}"
        time.sleep(1)
    return "⏰ timeout"

# 用法
print(dispatch_shell("hk99", "free -h"))
print(dispatch_shell("lacloud", "df -h /"))
```

## Hermes 任务模式

```python
# 调用目标节点的 Hermes 执行任务
body = {
    "type": "hermes",
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

## 查看可用节点

```bash
curl -s http://100.105.186.31:8000/api/v1/admin/dashboard/nodes \
  -H "Authorization: Bearer ***
```

当前节点：
- **hk99** — 香港, hermes_worker, 300M带宽
- **lacloud** — 美国, exit-node
- **gecloud** — 欧洲, 1G带宽
- **wuzhuserver** — 家宽, dual-stack
