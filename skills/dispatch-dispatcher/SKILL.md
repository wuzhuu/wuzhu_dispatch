---
name: dispatch-dispatcher
description: wuzhu-dispatch 分发端（中心控制面）管理 skill — 节点管理、任务监控、用户管理、健康检查
---

# dispatch-dispatcher Skill

管理 wuzhu-dispatch 的 Dispatcher 中心控制面。适用于部署 Dispatcher 的机器（如 wuzhucloud）。

## 前提

已在目标机器部署 Dispatcher，且 `DISPATCH_SERVER_SECRET` 或 admin 凭据可用。

## API 基础

```
Dispatcher: http://100.105.186.31:8000
```

## 常用操作

### 健康检查

```bash
curl -s http://100.105.186.31:8000/health
```

### 查看在线节点

```bash
# 列出所有节点及状态
curl -s http://100.105.186.31:8000/api/v1/admin/dashboard/nodes \
  -H "Authorization: Bearer $DISOC" | python3 -m json.tool
```

### 节点管理

```bash
# 禁用节点（停止分配新任务）
curl -s -X POST http://100.105.186.31:8000/api/v1/admin/nodes/<node_id>/disable \
  -H "Authorization: Bearer $DISOC"

# 启用节点
curl -s -X POST http://100.105.186.31:8000/api/v1/admin/nodes/<node_id>/enable \
  -H "Authorization: Bearer $DISOC"

# 更新节点配置
curl -s -X PATCH http://100.105.186.31:8000/api/v1/admin/nodes/<node_id> \
  -H "Authorization: Bearer $DISOC" \
  -H "Content-Type: application/json" \
  -d '{"name":"newname","tags":["hk","high_bandwidth"]}'
```

### 任务管理

```bash
# 查看所有任务
curl -s http://100.105.186.31:8000/api/v1/admin/tasks \
  -H "Authorization: Bearer $DISOC"

# 按状态过滤
curl -s "http://100.105.186.31:8000/api/v1/admin/tasks?status=running" \
  -H "Authorization: Bearer $DISOC"

# 查看任务详情
curl -s http://100.105.186.31:8000/api/v1/client/tasks/<task_id> \
  -H "Authorization: Bearer $DISOC"

# 取消任务
curl -s -X POST http://100.105.186.31:8000/api/v1/client/tasks/<task_id>/cancel \
  -H "Authorization: Bearer $DISOC" \
  -H "Content-Type: application/json" -d '{}'
```

### 仪表盘

浏览器访问 Dashboard：
```
http://100.105.186.31:8000/admin
```

默认管理员：
- 用户名: `admin`
- 密码: 从管理员获取

### 用户管理

```bash
# 查看审计日志（owner only）
curl -s http://100.105.186.31:8000/api/v1/admin/audit-logs \
  -H "Authorization: Bearer $DISOC"

# 创建用户
curl -s -X POST http://100.105.186.31:8000/api/v1/admin/users \
  -H "Authorization: Bearer $DISOC" \
  -H "Content-Type: application/json" \
  -d '{"username":"newuser","password":"strongpass","role":"operator"}'
```

## 配置文件

Dispatcher 配置在 `/home/hermes/wuzhu-dispatch/.env`，通过 systemd 加载：
```bash
sudo systemctl status wuzhu-dispatcher
sudo journalctl -u wuzhu-dispatcher -n 50 --no-pager -l
```
