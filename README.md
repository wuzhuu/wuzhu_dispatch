# wuzhu-dispatch

**异构服务器任务分发与计算服务平台** — 一个三端架构，支持 Client、Dispatcher、Compute Server 三种角色。

---

## 架构总览

```
┌──────────────┐     POST /api/v1/client/tasks      ┌──────────────────────┐
│              │  ───────────────────────────────►   │                      │
│   Client     │     GET  /api/v1/client/tasks       │    Dispatcher        │
│  (CLI / SDK  │  ◄───────────────────────────────   │   (FastAPI 主控)     │
│   / Web /    │                                      │   香港公网 IPv4      │
│   脚本 /     │     POST /api/v1/admin/*            │                      │
│   其他系统)  │  ───────────────►                    │  ┌────────────────┐  │
│              │                                      │  │   MySQL       │  │
└──────────────┘                                      │  │  (唯一访问者)   │  │
                                                      │  └────────────────┘  │
                                                      │                      │
                                                      │  ┌────────────────┐  │
                                                      │  │  调度器         │  │
                                                      │  │ 租约超时释放/   │  │
                                                      │  │ 节点离线检测    │  │
                                                      │  │ 任务智能匹配    │  │
                                                      │  └────────────────┘  │
                                                      └──────────┬───────────┘
                                            POST /api/v1/compute/*    │
                                            X-Node-Id + Bearer Token  │
                                                      │               │
                                                      ▼               ▼
                                              ┌────────────┐  ┌────────────┐
                                              │ Compute    │  │ Compute    │
                                              │ Server     │  │ Server     │
                                              │ (香港大带宽)│  │ (美国节点)  │
                                              │            │  │            │
                                              │ Shell/     │  │ Shell/     │
                                              │ Hermes/    │  │ Hermes/    │
                                              │ Docker     │  │ Docker     │
                                              └────────────┘  └────────────┘
```

### 核心架构原则

1. **Client → Dispatcher → Compute Server → Dispatcher → Client** — 所有通信经过 Dispatcher
2. **Client 不直接连接 Compute Server** — Client 不知道 Compute Server 的存在
3. **Compute Server 不直接连接 Client** — Compute Server 不持有 client token
4. **Dispatcher 是唯一的中心协调者** — 所有 API 请求必须经过 Dispatcher
5. **MySQL 只允许 Dispatcher 访问** — Client 和 Compute Server 不直接访问数据库
6. **Client 和 Compute Server 可部署在同一台机器** — 但它们使用完全独立的身份

---

## 目录结构

```
wuzhu-dispatch/
│
├── README.md                                         # 本文档
├── pyproject.toml                                    # 项目元数据
├── .env.example                                      # 环境变量模板 (Dispatcher 用)
│
├── common/                                           # 共享包 (三端共用)
│   └── dispatch_common/
│       ├── __init__.py                               # 聚合导出
│       ├── auth_types.py                             # TokenType, ClientAuthScope, ComputeNodeAuth
│       ├── task_types.py                             # TaskStatus, ExecutionMode, TaskRequirements
│       ├── schemas.py                                # 跨组件 Pydantic 模型
│       └── errors.py                                 # ErrorCode, DispatchError
│
├── dispatcher/                                       # 分发端 / Controller
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py                                   # FastAPI 入口 (三组路由注册)
│   │   ├── config.py                                 # 环境变量配置
│   │   ├── database.py                               # Async SQLAlchemy (唯一连 MySQL 的地方)
│   │   ├── models.py                                 # ORM 模型 (users, compute_nodes, tasks 等)
│   │   ├── schemas.py                                # Pydantic 请求/响应
│   │   ├── auth.py                                   # Client 认证 + Compute 认证 (双身份系统)
│   │   ├── scheduler.py                              # 后台调度器 (租约超时 + 离线检测)
│   │   ├── middleware/
│   │   │   ├── security.py                           # CSRF + 安全响应头
│   │   │   └── ratelimit.py                          # 内存限流器
│   │   ├── routes/
│   │   │   ├── auth.py                               # /api/v1/auth/* (login/logout/me)
│   │   │   ├── client.py                             # /api/v1/client/* (Client API)
│   │   │   ├── admin.py                              # /api/v1/admin/* (Admin API)
│   │   │   └── compute.py                            # /api/v1/compute/* (Compute Server API)
│   │   └── services/
│   │       ├── client_task_service.py                # 客户端任务业务逻辑
│   │       ├── compute_task_service.py               # 计算端任务业务逻辑 + 调度
│   │       ├── node_service.py                       # 节点注册/心跳/画像
│   │       └── audit_service.py                      # 审计日志
│
├── compute-server/                                   # 计算服务端 / Worker
│   ├── requirements.txt
│   ├── setup.py                                      # pip install -e .
│   └── dispatch_compute_server/
│       ├── main.py                                   # 主循环 (注册/心跳/拉取/执行/上报)
│       ├── config.py                                 # YAML 配置 (node.yaml)
│       ├── client.py                                 # HTTP 客户端 (调用 /api/v1/compute/*)
│       ├── metrics.py                                # psutil 系统指标采集
│       ├── executor.py                               # 执行器分发
│       └── executors/
│           ├── shell_executor.py                     # Shell 命令执行
│           ├── hermes_executor.py                    # Hermes CLI 执行
│           └── docker_executor.py                    # Docker 执行 (MVP 桩)
│
├── client/                                           # 客户端 CLI
│   ├── requirements.txt
│   ├── setup.py                                      # pip install -e .
│   └── dispatch_client/
│       ├── main.py                                   # Click CLI (task/node 命令)
│       └── client.py                                 # HTTP 客户端 (调用 /api/v1/client/*)
│
├── examples/
│   ├── client.yaml                                   # 客户端配置文件示例
│   ├── compute/
│   │   ├── node.hk99-300m.yaml                      # 香港大带宽节点配置
│   │   ├── node.lacloud-us.yaml                     # 美国节点配置
│   │   └── node.cheaphk-ipv6.yaml                   # 香港 IPv6 节点配置
│   └── tasks/
│       ├── probe_dns.json                            # DNS 探测任务
│       ├── collect_cn_stock.json                     # 国内股票采集
│       ├── collect_global.json                       # 全球采集
│       └── hermes_task.json                          # Hermes 自愈任务
│
├── scripts/
│   ├── init_db.py                                    # MySQL 建表
│   ├── run_dispatcher.sh                             # Dispatcher 启动脚本
│   └── run_compute_server.sh                         # Compute Server 启动脚本
│
├── systemd/
│   ├── wuzhu-dispatcher.service                     # Dispatcher systemd 单元
│   └── wuzhu-compute-server.service                 # Compute Server systemd 单元
│
└── docs/
    ├── architecture.md                               # 架构详解
    ├── api.md                                        # API 文档
    ├── security.md                                   # 安全模型
    └── deployment.md                                 # 部署指南
```

---

## 三类角色定义

### 1. 客户端 Client

**职责：** 提出任务需求

**可以是：**
- CLI 工具
- Web 管理后台
- SDK / 库
- 定时脚本
- 另一个系统
- 某台服务器上的任务提交器
- 也可以和 Compute Server 部署在同一台机器上

**能力：**
- 登录或使用 API Token 认证
- 创建任务请求
- 查询任务状态 / 日志 / 结果
- 取消 / 重试任务
- 查看自己有权限的任务

**不负责：**
- 不直接调度节点
- 不直接连接 MySQL
- 不直接连接 Compute Server
- 不决定任务去哪台机器执行
- 不持有 Compute Server 的 node token
- 不持有 MySQL 密码
- 不绕过 Dispatcher 创建任务

**身份：** `user` / `session` / `client_api_token`

> **⚠️ MVP 说明 — Client API Token Scope**
> `client_api_tokens` 表中的 `scope` 字段是预留字段，当前版本**尚未实现 scope 级别的细粒度校验**。
> Token 的实际权限继承自其所属用户的 `role`（viewer / operator / admin / owner）。
> 未来版本会启用 scope 校验，届时 token 权限 = user.role ∩ token.scope。

### 2. 分发端 Dispatcher / Controller

**职责：** 系统中心控制面（运行在香港公网 IPv4 主控节点上）

**能力：**
- 接收客户端任务请求
- 管理用户、session、API Token
- 管理 Compute Server 节点
- 存储节点静态画像
- 接收节点动态心跳
- 存储任务队列
- 根据任务需求和节点能力调度任务
- 给 Compute Server 分配任务
- 维护任务 lease
- 接收任务日志和任务结果
- 管理 artifact 元数据
- 执行 RBAC 权限控制
- 执行 CSRF / 安全响应头 / 限流等 Web 安全策略

**唯一：** 直接访问 MySQL

**身份体系分两套：**
- **Client 身份：** `user` / `session` / `client_api_token` / `RBAC role`
- **Compute Server 身份：** `node_id` + `agent_token`

**这两套身份必须严格分离。**

### 3. 计算服务端 Compute Server / Worker Server

**职责：** 提供计算服务

**可以是：**
- 香港大带宽服务器
- 美国大流量服务器
- 高性能服务器
- IPv6-only 小节点
- NAT 后面的家庭机器
- 校园网机器
- NAS
- 能跑 Hermes 的机器
- 只能跑普通 shell/python 的机器

**能力：**
- 向分发端注册节点
- 上报静态能力画像
- 周期性上报动态状态
- 主动从分发端拉取任务
- 根据任务 payload 执行任务 (shell / hermes / docker)
- 执行期间续租 lease
- 分段上传日志
- 上传成功结果 / 失败原因
- 遵守本地资源限制

**不负责：**
- 不直接访问 MySQL
- 不直接接受客户端请求
- 不暴露公网任务执行接口
- 不自己决定执行谁的任务
- 不信任来自外部的直接命令
- 不持有客户端用户 token

**身份：** `node_id` + `agent_token`

---

## 权限边界

| 操作 | 使用身份 | API 路径 |
|------|----------|----------|
| 创建任务 | Client 身份 | `/api/v1/client/tasks` |
| 查询任务 | Client 身份 | `/api/v1/client/tasks` |
| 管理节点 | Admin Client 身份 | `/api/v1/admin/nodes` |
| 拉取任务 | Compute Server 身份 | `/api/v1/compute/tasks/pull` |
| 上传心跳 | Compute Server 身份 | `/api/v1/compute/heartbeat` |
| 上传日志 | Compute Server 身份 | `/api/v1/compute/tasks/{id}/log` |
| finish/fail/renew | Compute Server 身份 | `/api/v1/compute/tasks/{id}/*` |
| 访问 MySQL | Dispatcher 内部身份 | — |

### 禁止的操作

- ❌ 用 node token 创建客户端任务
- ❌ 用 client token 拉取计算任务
- ❌ 用一个 token 同时代表用户和节点
- ❌ Compute Server 直接访问 MySQL
- ❌ Client 直接连接 Compute Server

### 同机部署

某台服务器可以同时运行 Client 和 Compute Server，但必须使用不同的配置文件：

```yaml
# client_config.yaml
dispatcher_url: https://dispatch.example.com
client_token: xxxxx  # 这是 client API token
```
```yaml
# node.yaml (compute server config)
dispatcher_url: https://dispatch.example.com
node_id: hk99-300m
agent_token: yyyyy  # 这是 node agent token (与 client token 完全不同)
```

---

## API 分层

### 1. Client API (`/api/v1/client/*`)

| 方法 | 路径 | 说明 | 最低角色 |
|------|------|------|----------|
| POST | `/api/v1/client/tasks` | 创建任务 | operator |
| GET | `/api/v1/client/tasks` | 列任务 (自己创建) | viewer |
| GET | `/api/v1/client/tasks/{id}` | 任务详情 | viewer |
| POST | `/api/v1/client/tasks/{id}/cancel` | 取消任务 | operator |
| POST | `/api/v1/client/tasks/{id}/retry` | 重试任务 | operator |
| GET | `/api/v1/client/tasks/{id}/logs` | 任务日志 | viewer |

**认证方式：** Cookie session 或 Client API Token (`Authorization: Bearer <token>`)

### 2. Admin API (`/api/v1/admin/*`)

| 方法 | 路径 | 说明 | 最低角色 |
|------|------|------|----------|
| GET | `/api/v1/admin/nodes` | 列节点 | viewer |
| GET | `/api/v1/admin/nodes/{id}` | 节点详情 | viewer |
| POST | `/api/v1/admin/nodes/{id}/disable` | 禁用节点 | admin |
| POST | `/api/v1/admin/nodes/{id}/enable` | 启用节点 | admin |
| PATCH | `/api/v1/admin/nodes/{id}` | 更新节点 | admin |
| GET | `/api/v1/admin/tasks` | 所有任务 (admin 视角) | viewer |
| GET | `/api/v1/admin/audit-logs` | 审计日志 | owner |
| POST | `/api/v1/admin/users` | 创建用户 | owner |
| GET | `/api/v1/admin/users` | 列用户 | owner |
| PATCH | `/api/v1/admin/users/{id}` | 更新用户 | owner |

### 3. Compute Server API (`/api/v1/compute/*`)

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|------|
| POST | `/api/v1/compute/register` | 注册/更新节点 | Registration Token |
| POST | `/api/v1/compute/heartbeat` | 上报动态状态 | X-Node-Id + Bearer |
| POST | `/api/v1/compute/tasks/pull` | 拉取任务 | X-Node-Id + Bearer |
| POST | `/api/v1/compute/tasks/{id}/renew` | 续租 | X-Node-Id + Bearer |
| POST | `/api/v1/compute/tasks/{id}/log` | 上传日志 | X-Node-Id + Bearer |
| POST | `/api/v1/compute/tasks/{id}/finish` | 完成 | X-Node-Id + Bearer |
| POST | `/api/v1/compute/tasks/{id}/fail` | 失败 | X-Node-Id + Bearer |

**Compute Server API 设计原则：**
- 不使用浏览器 Cookie
- 不使用 CSRF
- 必须强校验 `node_id` 和 `agent_token`
- 不能创建任务，只能拉取分配给自己的任务
- 不返回管理信息

---

## 任务模型

```json
{
  "task_id": "task_xxx",
  "created_by_user_id": "user_xxx",
  "created_by_client_token_id": "token_xxx",
  "type": "web_collect_global",
  "priority": 60,
  "status": "pending",
  "requirements": {
    "required_tags": ["foreign_reachable"],
    "avoid_tags": ["low_bandwidth"],
    "runtime": { "python": true },
    "min_cpu_cores": 2,
    "min_memory_mb": 1024,
    "min_bandwidth_mbps": 10
  },
  "payload": {
    "execution": {
      "mode": "shell",
      "command": "python collect_global.py"
    }
  },
  "assigned_node_id": null,
  "lease_until": null,
  "timeout_seconds": 1800,
  "max_retries": 2,
  "retry_count": 0,
  "result": null,
  "created_at": "...",
  "started_at": null,
  "finished_at": null
}
```

### 任务状态流转

```
pending → assigned → running → success
                              → failed (retry_count >= max_retries)
                              → timeout (lease expired)
         retrying → pending (re-enters scheduling)
                  → failed (retry_count >= max_retries)
cancelled (any non-terminal state → terminal)
```

### 任务归属

- `created_by_user_id` — 创建任务的用户 (Client 用户)
- `created_by_client_token_id` — 创建任务的 API token
- `assigned_node_id` — 当前执行该任务的 Compute Server

这三个字段不混淆。

---

## 节点模型

### 静态画像 (注册时上报)

```json
{
  "node_id": "hk99-300m",
  "name": "HK99 High Bandwidth Compute Server",
  "region": "HK",
  "provider": "HK99",
  "tags": ["hk", "high_bandwidth", "cn_reachable", "public_ipv4", "hermes_worker"],
  "static_profile": {
    "cpu_cores": 2,
    "memory_mb": 2048,
    "bandwidth_mbps": 300,
    "public_ipv4": true,
    "public_ipv6": false,
    "cn_reachable": "good",
    "foreign_reachable": "good",
    "runtime": { "shell": true, "python": true, "docker": true, "hermes": true },
    "limits": {
      "max_parallel_tasks": 3,
      "allow_heavy_download": true,
      "allow_heavy_compute": false
    }
  }
}
```

### 动态状态 (心跳上报)

```json
{
  "node_id": "hk99-300m",
  "online": true,
  "last_heartbeat": "...",
  "cpu_usage": 15.2,
  "memory_usage": 48.1,
  "disk_usage": 35.5,
  "running_tasks": 1,
  "rx_mbps": 3.4,
  "tx_mbps": 1.2
}
```

---

## 数据库表

| 表 | 说明 | 备注 |
|----|------|------|
| `users` | Client 用户 (RBAC) | |
| `sessions` | Web session cookie | |
| `client_api_tokens` | Client API 长令牌 | 与 agent_token 不同 |
| `compute_nodes` | Compute Server 注册信息 | 旧 `nodes` 表 |
| `compute_node_status` | Compute Server 动态状态 | 旧 `node_status` 表 |
| `tasks` | 任务队列 | |
| `task_logs` | 任务日志 | |
| `artifacts` | 任务产出物引用 | |
| `audit_logs` | 审计日志 | |

> **注意：** `client_api_tokens` 和 `compute_nodes.agent_token_hash` 是两个完全不同的令牌体系，分别对应 Client 身份和 Compute Server 身份。

---

## 快速开始

### 1. 部署 Dispatcher (香港主控)

```bash
cd /opt/wuzhu-dispatch
cp .env.example .env
# 编辑 .env 填入 MySQL 连接信息和安全密钥

cd dispatcher
pip install -r requirements.txt

# 初始化数据库
python ../scripts/init_db.py

# 启动
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 2. 部署 Compute Server (Worker 节点)

```bash
cd /opt/wuzhu-dispatch/compute-server
pip install -e .

# 编辑配置文件
cp ../examples/compute/node.hk99-300m.yaml /etc/wuzhu-dispatch/node.yaml

# 启动
dispatch-compute-server -c /etc/wuzhu-dispatch/node.yaml
```

### 3. 使用 Client CLI

CLI 支持两种配置方式：

**方式 A：YAML 配置文件（推荐）**
```bash
# examples/client.yaml
# dispatcher_url: "https://dispatch.example.com"
# client_token: "your-client-api-token-here"
dispatch-client -c ~/.config/wuzhu-dispatch/client.yaml task list
```

**方式 B：环境变量**
```bash
export DISPATCH_URL=https://dispatch.example.com
export DISPATCH_CLIENT_TOKEN=your-client-token

dispatch-client task create --type probe_dns --payload ../examples/tasks/probe_dns.json
dispatch-client task list
dispatch-client node list
```

> CLI 仅支持 Bearer Client API Token 认证，不支持 Cookie/CSRF 登录模式。
> 如需 Web 管理，请使用 Dispatcher 的 `/api/v1/auth/login` 接口获取 session cookie。

### 4. systemd 自启

```bash
sudo cp systemd/wuzhu-dispatcher.service /etc/systemd/system/
sudo cp systemd/wuzhu-compute-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wuzhu-dispatcher
sudo systemctl enable --now wuzhu-compute-server
```

---

## 安全模型

详见 [docs/security.md](docs/security.md)。核心要点：

- **双身份系统**：Client 身份和 Compute Server 身份完全分离
- **最低权限原则**：每个组件只持有完成自身任务所需的最小令牌
- **MySQL 隔离**：只有 Dispatcher 持有数据库凭据
- **RBAC**：Client 端细粒度角色权限 (viewer / operator / admin / owner)
- **CSRF 保护**：Session cookie 认证的请求需要 CSRF token
- **安全响应头**：CSP / HSTS / X-Frame-Options 等
- **限流**：登录 5 次/5 分钟，心跳 30 次/60 秒等
- **Agent 执行安全**：非 root 运行，工作目录隔离，敏感环境变量清洗

---

## 调度逻辑

调度器属于 **Dispatcher**，不属于 Client 也不属于 Compute Server。

### 调度流程

1. **Client** 创建任务 → Dispatcher 写入 MySQL，状态 `pending`
2. **Compute Server** 主动调用 `POST /api/v1/compute/tasks/pull`
3. **Dispatcher** 校验 Compute Server 身份
4. **Dispatcher** 根据节点画像、DB 中 running 任务数、任务 requirements 选择适合任务
5. **Dispatcher** 使用条件 UPDATE 把任务改为 `running`
6. **Dispatcher** 返回任务 payload 和 lease
7. **Compute Server** 执行任务，定期 renew，上传日志
8. **Compute Server** finish/fail
9. **Client** 查询结果

### 调度器关键设计

- **DB 中的 running 任务数** 而非仅信 heartbeat 中的 `running_tasks`
- **原子拉取**：条件 UPDATE + rowcount 检查，避免并发拉取
- **硬过滤 + 打分**：先按 tags/runtime/CPU/内存/带宽 硬过滤，再按空闲率/优先级/网络适配度打分
- **租约续期**：长时间任务通过定期 renew 防止被回收
- **超时回收**：后台调度器每 30s 扫描租约过期任务，自动释放并重试

### ⚠️ MVP 限制：取消是软取消

当前 `cancel_task` **只修改数据库状态**，不会主动 kill Compute Server 上正在执行的进程。
这意味着：
- 取消一个 `running` 状态的任务后，Compute Server 上的子进程仍会继续运行直到超时或自行结束
- 结果最终会被忽略（Dispatcher 会拒绝 finish/fail）
- **后续计划**：引入 Dispatcher → Compute Server 的主动取消通道（如任务控制消息队列），实现硬取消

### ⚠️ MVP 限制：Artifact 暂不支持

当前版本暂时不提供 artifact（任务产出文件）下载 API。
任务执行结果通过 `result` JSON 字段和日志查询。
后续版本会通过 `artifacts` 表 + 安全路径映射 + `Content-Disposition: attachment` 实现文件下载。

### ⚠️ 生产部署前置检查

在生产环境启动前，必须：

1. **修改默认密钥**：设置环境变量 `ENVIRONMENT=production`
   并确保 `DISPATCH_SERVER_SECRET` 和 `SESSION_SECRET` 不是默认值。
   生产模式下，任何默认密钥都会导致 Dispatcher 拒绝启动。

2. **配置 CORS/CSRF 域名**：设置 `CORS_ALLOWED_ORIGINS` 和 `CSRF_ALLOWED_ORIGINS`
   为实际的 Web 管理后台域名，逗号分隔。

3. **配置注册令牌**：如果使用 compute-server 自动注册功能，
   必须设置 `REGISTRATION_TOKEN`；否则需要 admin 在 Dispatcher 端预注册节点。

---

## 后续扩展

| 方向 | 说明 |
|------|------|
| Redis / NATS | 消息队列解耦任务调度，实时推送而非轮询 |
| Docker 隔离 | 完善 Docker Executor，容器化任务隔离 |
| Web 面板 | Vue/React 可视化节点地图、任务热力图 |
| 对象存储 | S3/MinIO 中转大文件 |
| mTLS | 双向 TLS 替代 Bearer Token |
| 任务血缘 DAG | 任务依赖图，A 完成后自动触发 B |
| Webhook | 任务完成推送到飞书/Telegram/企微 |
| 多云伸缩 | 根据队列深度自动扩缩容 Worker 节点 |

---

## 许可证

MIT
