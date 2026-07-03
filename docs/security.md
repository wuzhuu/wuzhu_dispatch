# 安全模型

## 架构安全

### 双身份系统

wuzhu-dispatch 的核心安全设计是 **Client 身份** 和 **Compute Server 身份** 的严格分离。

| | Client 身份 | Compute Server 身份 |
|--|-------------|-------------------|
| 凭证 | user password / client_api_token | node_id + agent_token |
| 存储 | users 表 (bcrypt) / client_api_tokens 表 (SHA-256) | compute_nodes 表 (SHA-256) |
| API 端点 | `/api/v1/client/*`, `/api/v1/admin/*` | `/api/v1/compute/*` |
| 可创建任务 | ✅ | ❌ |
| 可拉取任务 | ❌ | ✅ |
| 可管理节点 | ✅ (admin+) | ❌ |

### MySQL 隔离

- 只有 Dispatcher 知道 MySQL 连接信息
- Client 和 Compute Server 永远不持有 MySQL 密码
- 数据库切换（MySQL → PostgreSQL）不影响已部署的 Compute Server

### 最小权限

- 每个 Compute Server 只持有自己的 `node_id` + `agent_token`
- 每个 Client 只持有自己的 `client_api_token`
- Compute Server 不知道其他节点的存在
- Client 不知道哪些 Compute Server 执行了任务

## 传输安全

- 生产环境必须使用 HTTPS (nginx/Caddy 反代)
- `Strict-Transport-Security: max-age=31536000; includeSubDomains`
- Compute Server API 不使用 Cookie，不依赖 CSRF

## CSRF 保护

- 所有 POST/PUT/DELETE 请求校验 `X-CSRF-Token` 头
- CSRF token 通过 HMAC(session_id, secret) 派生 (不额外存储)
- 校验 Origin / Referer 头
- Compute Server API 路径豁免 CSRF

## 安全响应头

```http
Content-Security-Policy: default-src 'self'; script-src 'self'; ...
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
Referrer-Policy: same-origin
Permissions-Policy: geolocation=(), microphone=(), camera=()
```

## 限流

| 接口 | 限制 |
|------|------|
| 登录 | 同 IP 5 次 / 5 分钟 |
| Compute Server 心跳 | 30 次 / 60 秒 |
| 任务拉取 | 20 次 / 60 秒 |
| 日志上传 | 60 次 / 60 秒 |
| 任务创建 | 30 次 / 60 秒 |

## RBAC

| 角色 | 权限 | 可分配给 |
|------|------|----------|
| `viewer` | 查看节点、任务、状态 | 普通操作员 |
| `operator` | 创建普通任务、取消、重试 | 任务提交者 |
| `admin` | 创建 shell/hermes 任务、管理节点、禁用/启用节点 | 系统管理员 |
| `owner` | 管理用户、密钥、审计日志 | 超级管理员 |

## Compute Server 执行安全

- **非 root 运行**：systemd 使用专用低权限用户
- **工作目录隔离**：shell 任务限制在 `work_dir/<task_id>/` 下
- **Hermes workspace 限制**：只能使用配置中指定的目录
- **进程组清理**：超时后 kill 整个进程组
- **敏感环境变量清洗**：执行子进程前清除 `DISPATCH_SERVER_SECRET`、`MYSQL_PASSWORD` 等

## 审计日志

所有敏感操作写入 `audit_logs` 表：
- 登录成功/失败
- 创建/取消/重试任务
- 注册/禁用/启用节点
- 创建 shell/hermes 高危任务
- 修改用户/权限
