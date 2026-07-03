# API 文档

## 基础信息

- **Base URL**: `https://dispatch.example.com`
- **文档**: `/api/docs` (Swagger), `/api/redoc` (ReDoc)
- **健康检查**: `GET /health`

## 认证方式

### 1. Client API Token (推荐)

```
Authorization: Bearer <client_api_token>
```

适用于 CLI、脚本、SDK 等程序化调用。

### 2. Session Cookie (Web 管理后台)

登录后获取 `dispatch_session` HttpOnly Cookie 和 `csrf_token` JS Cookie。
所有 POST/PUT/DELETE 请求需要带 `X-CSRF-Token` 头。

### 3. DISPATCH_SERVER_SECRET (bootstrap / 管理 token)

```
Authorization: Bearer <dispatch_server_secret>
```

系统级 bootstrap token，仅用于初始化、测试或受控管理场景。
生产环境应优先使用用户登录或 `client_api_token`。

### 4. Compute Server 认证

```
Authorization: Bearer <agent_token>
X-Node-Id: <node_id>
```

## 角色权限

| 角色 | 权限 |
|------|------|
| `viewer` | 查看节点、任务、状态 |
| `operator` | 创建普通任务、取消任务、重试任务 |
| `admin` | 创建 shell/hermes 任务、管理节点 |
| `owner` | 管理用户、密钥、系统配置、查看审计日志 |

## 完整 API 参考

参见 Swagger UI: `/api/docs`

或 ReDoc: `/api/redoc`
