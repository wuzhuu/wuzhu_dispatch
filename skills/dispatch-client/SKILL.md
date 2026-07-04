---
name: dispatch-client
description: wuzhu-dispatch 客户端 skill — 任务提交、结果查询、模板调用、profile 管理
---

# dispatch-client Skill

向 wuzhu-dispatch 分布式计算网络提交任务、查询状态、获取结果。

## 前提

```bash
pip install -e ~/wuzhu-dispatch/client/
```

配置文件 `~/.config/wuzhu-dispatch/client.yaml`：
```yaml
dispatcher_url: "http://100.105.186.31:8000"
client_token: "<your-client-token>"
```

## Python 方式

```python
from dispatch_client.runtime_skill import DispatchRuntimeSkill
from dispatch_client.skill_config import SkillConfig

# 从配置文件加载
skill = DispatchRuntimeSkill.from_config()

# 或直接指定
skill = DispatchRuntimeSkill.from_tokens(
    dispatcher_url="http://100.105.186.31:8000",
    client_token="<token>",
)
```

### 快速任务（提交并等结果）

```python
result = skill.quick(
    template_id="http_probe",
    params={"url": "https://example.com"},
    wait_seconds=10,
)
print(f"状态: {result.status}")
print(f"结果: {result.result}")
```

### 异步提交

```python
result = skill.submit(
    template_id="shell",
    params={"command": "uname -a"},
    priority=50,
    timeout_seconds=300,
)
task_id = result.task_id

# 稍后轮询
final = skill.wait(task_id, timeout=60)
print(final.result.get("stdout", ""))
```

### 指定目标节点

```python
# 按节点 ID
skill.quick("shell", params={"command": "uptime"},
            target={"mode": "node", "node_id": "hk99"})

# 按标签
skill.quick("http_probe", params={"url": "https://google.com"},
            target={"mode": "tags", "tags": ["foreign_reachable"]})

# 使用 profile（预先定义的标签组合）
skill.quick("http_probe", params={"url": "https://google.com"},
            profile="foreign")
```

### 查看任务状态和日志

```python
status = skill.status(task_id)
logs = skill.logs(task_id)
for entry in logs:
    print(f"[{entry['log_time']}] {entry['level']}: {entry['message']}")
```

### 取消任务

```python
skill.cancel(task_id)
```

## CLI 方式

```bash
# 快速执行
dispatch-skill quick http_probe --param url https://google.com --wait 10

# 指定目标
dispatch-skill quick shell --param command "uptime" --node hk99

# 异步提交
dispatch-skill submit shell --param command "sleep 30"

# 查看状态
dispatch-skill status <task_id>

# 查看日志
dispatch-skill logs <task_id>

# 获取结果
dispatch-skill result <task_id>

# 取消
dispatch-skill cancel <task_id>

# 使用 profile
dispatch-skill quick http_probe --param url https://google.com --profile foreign
```

## Profile 管理

在 `~/.config/wuzhu-dispatch/skill.yaml` 中定义 profile：

```yaml
profiles:
  foreign:
    target:
      mode: tags
      tags: ["foreign_reachable"]
  hk:
    target:
      mode: tags
      tags: ["hk", "cn_reachable"]
  cn:
    target:
      mode: tags
      tags: ["cn_reachable"]
```

## 可用模板

```bash
curl -s http://100.105.186.31:8000/api/v1/client/templates \
  -H "Authorization: Bearer *** 常用模板: http_probe, shell, dns_lookup, ping, hermes_task
```
