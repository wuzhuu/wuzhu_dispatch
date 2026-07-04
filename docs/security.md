# 安全模型

## 架构安全

### 三端边界

| 组件 | 对外暴露 | 访问 MySQL | 持有凭证类型 | 可创建任务 |
|------|---------|-----------|-------------|-----------|
| **Client** | 否（仅发起请求） | ❌ | `client_api_token` / user session | ✅ |
| **Dispatcher** | HTTPS（公网） | ✅ 唯一访问者 | `DISPATCH_SERVER_SECRET` + session secret | ✅ |
| **Compute Server** | 否（仅回连 Dispatcher） | ❌ | `node_id` + `agent_token` | ❌ |

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

### Client API Token Scope（细粒度权限控制）

`client_api_token.scope` 现在已经参与权限判断。scope 是一个 JSON 列表，可以在其中嵌入一个 dict 来声明 token 的能力。

**默认语义（空 scope / 未设置 capability dict）：**
空 scope 不限制 template/mode/tag，但仍应用全局安全默认上限（`max_priority=100`, `max_timeout_seconds=3600`, `max_concurrent_tasks=10`, `max_payload_bytes=65536` 等）。这些上限可通过显式设置对应字段放宽或收紧。

**显式设置 capability 后才启用细粒度限制。**

支持以下能力字段（仅在 scope 中显式设置时生效）：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `allowed_templates` | `list[str]` | `[]` | 允许的模板 ID，空=全部允许 |
| `allowed_modes` | `list[str]` | `[]` | 允许的执行模式，空=不限制 |
| `denied_modes` | `list[str]` | `[]` | 禁止的执行模式，空=不额外禁止 |
| `allowed_target_tags` | `list[str]` | `[]` | 允许指定的 target tag，空=全部允许 |
| `allowed_node_ids` | `list[str]` | `[]` | 允许指定的 node_id，空=全部允许；admin/owner 角色绕过此限制和 `can_target_specific_node` |
| `max_priority` | `int` | `100` | 最大任务优先级 |
| `max_timeout_seconds` | `int` | `3600` | 最大超时秒数 |
| `max_concurrent_tasks` | `int` | `10` | 最大并发任务数 |
| `max_payload_bytes` | `int` | `65536` | 最大 payload 字节数 |
| `can_target_specific_node` | `bool` | `false` | 是否允许指定 node_id |
| `allow_internal_network` | `bool` | `false` | 是否允许 URL 解析到内网地址 |

**兼容模式 token（scope 为空）：**
```json
[]
```
行为：不限制 template/mode/tag/node 范围，但仍应用全局安全默认上限，
例如 `max_priority`、`max_timeout_seconds`、`max_concurrent_tasks`、`max_payload_bytes`。
admin/owner 仍按文档规则拥有更高角色权限。

**最小权限 Skill token：**
```json
[{
  "allowed_templates": ["http_probe", "dns_probe", "ping_probe"],
  "allowed_modes": ["template"],
  "denied_modes": ["shell", "hermes"],
  "allowed_target_tags": ["hk", "foreign_reachable"],
  "allowed_node_ids": [],
  "max_priority": 50,
  "max_timeout_seconds": 300,
  "max_concurrent_tasks": 3,
  "max_payload_bytes": 65536,
  "can_target_specific_node": false,
  "allow_internal_network": false
}]
```

**指定节点 Skill token：**
```json
[{
  "allowed_templates": ["http_probe"],
  "allowed_modes": ["template"],
  "allowed_target_tags": ["hk"],
  "allowed_node_ids": ["node-hk"],
  "can_target_specific_node": true,
  "max_priority": 50,
  "max_timeout_seconds": 300
}]
```

**所有字段已 enforcement。**

## 传输安全

- 生产环境必须使用 HTTPS (nginx/Caddy 反代)
- `Strict-Transport-Security: max-age=31536000; includeSubDomains`
- Compute Server API 不使用 Cookie，不依赖 CSRF

## 安全响应头

```http
Content-Security-Policy: default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; connect-src 'self'; img-src 'self' data:; style-src 'self'; form-action 'self'
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
Referrer-Policy: same-origin
Permissions-Policy: geolocation=(), microphone=(), camera=()
Strict-Transport-Security: max-age=31536000; includeSubDomains
```

> ℹ️ CSP 说明：
> - `script-src 'self'` — 无 inline script，仅加载 `/static/dashboard.js`
> - `style-src 'self'` — 无 inline style，使用 CSS class `.hidden` 替代 `style="display:none"`
> - 所有用户数据通过 `textContent` 渲染，无 `innerHTML`

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
- **工作目录清理**：自动清理 `work_dir/tasks/<task_id>/` 中的临时文件
  （详见 README 清理策略 — 支持按状态保留、大小上限、磁盘压力上报）
- **Hermes workspace 限制**：只能使用配置中指定的目录
- **Hermes workspace 不受 cleanup 影响**：`allowed_hermes_workspaces` 不会被自动清理
- **进程组清理**：超时后 kill 整个进程组
- **敏感环境变量清洗**：执行子进程前清除 `DISPATCH_SERVER_SECRET`、`MYSQL_PASSWORD` 等

## 审计日志

所有敏感操作写入 `audit_logs` 表：
- 登录成功/失败
- 创建/取消/重试任务
- 注册/禁用/启用节点
- 创建 shell/hermes 高危任务
- 修改用户/权限

## 公开仓库自查

> 本仓库为 **public** 仓库，即使当前 HEAD 没有泄露，也要防历史提交泄露。

### 使用 gitleaks 检查

```bash
# 安装 gitleaks
# macOS: brew install gitleaks
# Linux: 从 https://github.com/gitleaks/gitleaks/releases 下载

# 检查当前仓库
gitleaks detect --source . --no-git=false --redact

# 如果发现泄露，使用 --verbose 查看详情
gitleaks detect --source . --no-git=false --verbose
```

### 使用 git grep 扫描历史

```bash
git grep -nE \
  "BEGIN .*PRIVATE KEY|ghp_|sk-|AIza|MYSQL_PASSWORD=|DISPATCH_SERVER_SECRET=|REGISTRATION_TOKEN=|SESSION_SECRET=|agent_token:|client_token:" \
  $(git rev-list --all)
```

允许出现在仓库中的占位符：
- `.env.example` 中的 `your_mysql_password`、`change-this-*`
- README/docs 中的占位符说明
- example YAML 中的 `CHANGE_ME` 占位符
- 测试脚本中的测试 token（如 `"test-secret-12345"`）

不允许出现在仓库中的内容：
- 真实密码、真实 token、私钥
- 真实数据库 URL（含密码）
- 真实生产域名/IP
- 真实 `node.yaml`/`client.yaml` 配置

### 如果发现历史泄露

1. **立即轮换**所有可能暴露的密钥（不仅仅是删除提交）
2. 使用 `git filter-branch` 或 `git filter-repo` 清理历史
3. 强制推送修复后的历史：`git push --force --all`
4. 通知所有已克隆仓库的协作者重新 clone

### GitHub 推荐设置

在仓库 Settings → Code security and analysis 中开启：
- **Secret scanning** — GitHub 自动扫描已知 secret 格式
- **Push protection** — 阻止包含 secret 的推送
- **Dependabot alerts** — 依赖漏洞提醒
- **Code scanning** — CodeQL 安全分析
