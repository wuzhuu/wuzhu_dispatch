# wuzhu-dispatch — 异构机器资源池调度平台

**Private heterogeneous machine resource pool scheduling platform.**

将多台不同特性的物理机/云服务器融合成统一资源池，由中心控制面根据节点画像（tags、runtime、带宽、区域）和实时状态（CPU、内存、磁盘、网络）智能分发任务。

---

## 架构概览

```
┌──────────────┐     POST /api/v1/client/tasks      ┌──────────────────────┐
│              │  ──────────────────────────────►    │                      │
│   Client     │     GET  /api/v1/client/tasks       │    Dispatcher        │
│  (CLI / SDK  │  ◄──────────────────────────────    │   (FastAPI 主控)     │
│   / Web /    │                                      │   香港公网 IPv4      │
│   脚本 /     │     POST /api/v1/admin/*            │   控制面 | 调度面    │
│   其他系统)  │  ──────────────►                     │   状态面 | 安全边界  │
│              │                                      │                      │
└──────────────┘                                      │  ┌────────────────┐  │
                                                      │  │   MySQL        │  │
                                                      │  │  (唯一访问者)   │  │
                                                      │  └────────────────┘  │
                                                      │                      │
                                                      │  ┌────────────────┐  │
                                                      │  │  调度器         │  │
                                                      │  │  FOR UPDATE     │  │
                                                      │  │  原子拉取       │  │
                                                      │  │  长轮询 25s     │  │
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
                                              │ 自适应间隔  │  │ 自适应间隔  │
                                              │ 长轮询拉取  │  │ 长轮询拉取  │
                                              └────────────┘  └────────────┘
```

### 核心原则

1. **Client → Dispatcher → Compute Server → Dispatcher → Client** — 所有通信经过 Dispatcher
2. **Client 不连接 Compute Server** — Client 不知道 Compute Server 的存在
3. **Compute Server 不连接 Client** — Compute Server 不持有 client token
4. **Dispatcher 是唯一的中心协调者、控制面、调度面、状态面和安全边界**
5. **MySQL 只允许 Dispatcher 访问** — Client 和 Compute Server 不持有 MySQL 凭据
6. **Client 和 Compute Server 可部署在同一台机器** — 使用完全独立的身份和配置文件

---

## 三角色配置

### 1. Dispatcher（分发端 — 中心控制面）

**职责**：接收客户端请求、管理节点、调度任务、存储状态、安全边界

**唯一**：直接访问 MySQL 的组件

**部署依赖**：
- Python 3.11+
- MySQL 8.0+（仅 Dispatcher 需要）
- 公网 IPv4 服务器（建议香港）

#### 部署步骤

```bash
# 1. 系统依赖
sudo apt update && sudo apt install -y python3 python3-venv python3-pip mysql-server

# 2. 创建数据库和用户（不要使用 MySQL root 作为应用连接用户）
sudo mysql -e "CREATE DATABASE IF NOT EXISTS wuzhu_dispatch CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
sudo mysql -e "CREATE USER IF NOT EXISTS 'wuzhu_dispatch'@'localhost' IDENTIFIED BY 'your_strong_password';"
sudo mysql -e "GRANT ALL PRIVILEGES ON wuzhu_dispatch.* TO 'wuzhu_dispatch'@'localhost';"
sudo mysql -e "FLUSH PRIVILEGES;"

# 3. 复制并编辑配置
cd /opt/wuzhu-dispatch
cp .env.example .env
# 编辑 .env：
#   MYSQL_PASSWORD=your_strong_password
#   DISPATCH_SERVER_SECRET=<64-char-random>
#   SESSION_SECRET=<64-char-random>
#   REGISTRATION_TOKEN=<64-char-random>
#   CORS_ALLOWED_ORIGINS=https://admin.your-domain.com
#   CSRF_ALLOWED_ORIGINS=https://admin.your-domain.com

# 4. 安装依赖
cd dispatcher
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 5. 初始化数据库
python3 ../scripts/init_db.py

# 6. 启动
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 7. 健康检查
curl http://localhost:8000/health
# → {"status": "ok"}
```

#### systemd 自启

```ini
# /etc/systemd/system/wuzhu-dispatcher.service
[Unit]
Description=wuzhu-dispatch dispatcher (central control plane)
After=network-online.target mysql.service
Wants=network-online.target

[Service]
Type=simple
User=wuzhu
WorkingDirectory=/opt/wuzhu-dispatch/dispatcher
EnvironmentFile=/opt/wuzhu-dispatch/.env
ExecStart=/opt/wuzhu-dispatch/dispatcher/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10

# ── Security hardening ─────────────────────────────────────────
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/opt/wuzhu-dispatch /var/log/wuzhu-dispatch

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wuzhu-dispatcher

# 配置文件权限加固
sudo chown -R wuzhu:wuzhu /opt/wuzhu-dispatch
sudo chmod 600 /opt/wuzhu-dispatch/.env
```

#### HTTPS（Caddy 推荐，含安全头）

```caddy
# /etc/caddy/Caddyfile
dispatch.example.com {
    reverse_proxy localhost:8000

    # Security headers
    header {
        Content-Security-Policy "default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; connect-src 'self'; img-src 'self' data:; style-src 'self'; form-action 'self'"
        X-Frame-Options "DENY"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "same-origin"
        Permissions-Policy "geolocation=(), microphone=(), camera=()"
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
    }
}
```

或 Nginx：

```nginx
server {
    listen 443 ssl;
    server_name dispatch.example.com;
    ssl_certificate /etc/ssl/certs/example.crt;
    ssl_certificate_key /etc/ssl/private/example.key;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Security headers
    add_header Content-Security-Policy "default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; connect-src 'self'; img-src 'self' data:; style-src 'self'; form-action 'self'" always;
    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "same-origin" always;
    add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
}
```

> ⚠️ 反代层安全头与 Dispatcher 应用层安全头共同提供纵深防御。即使应用层 middleware 已设置，反代层也不应省略，防止静态文件、错误页或未来改动漏掉安全头。

---

### 2. Compute Server（计算服务端）

**职责**：提供计算、采集、执行、Hermes 调用等能力

**不需要**：MySQL

**仅需要**：能 HTTPS 访问 Dispatcher

#### 配置文件

```yaml
# /etc/wuzhu-dispatch/node.yaml
dispatcher_url: "https://dispatch.example.com"

node_id: "example-hk-highbw"            # 全局唯一标识
agent_token: "CHANGE_ME"                # 节点认证 token (不是 client token)

name: "Example HK High Bandwidth"
region: "HK"
provider: "ExampleProvider"

tags:
  - hk
  - high_bandwidth
  - cn_reachable
  - public_ipv4
  - hermes_worker

static_profile:
  cpu_cores: 2
  memory_mb: 2048
  bandwidth_mbps: 300
  public_ipv4: true
  cn_reachable: good
  foreign_reachable: excellent
  runtime:
    shell: true
    python: true
    docker: true
    hermes: true
  limits:
    max_parallel_tasks: 3
    allow_heavy_download: true
    allow_heavy_compute: false

agent:
  # 自适应拉取间隔
  heartbeat_interval_seconds: 20
  lightweight_heartbeat_interval_seconds: 30
  active_pull_interval_seconds: 3       # 有任务时
  warm_idle_pull_interval_seconds: 10   # 刚完成任务
  cold_idle_pull_interval_seconds: 30   # 长期空闲
  max_idle_pull_interval_seconds: 60
  long_poll_wait_seconds: 25            # 长轮询等待秒数
  pull_error_backoff_max_seconds: 120

  work_dir: "/opt/wuzhu-dispatch/work"
  log_dir: "/opt/wuzhu-dispatch/logs"
  allowed_hermes_workspaces:
    - /home/example/workspace
```

#### 安装与启动

```bash
# 1. 安装
cd /opt/wuzhu-dispatch/compute-server
python3 -m venv venv
source venv/bin/activate
pip install -e .

# 2. systemd 自启
# /etc/systemd/system/wuzhu-compute-server.service
[Unit]
Description=wuzhu-dispatch compute server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=wuzhu-agent
WorkingDirectory=/opt/wuzhu-dispatch/compute-server
ExecStart=/opt/wuzhu-dispatch/compute-server/venv/bin/dispatch-compute-server -c /etc/wuzhu-dispatch/node.yaml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

#### 注册模式

Compute Server 支持两种注册方式：

**方式 A：自动注册**（需要 `registration_token`）
```yaml
# node.yaml
registration_token: "your-token-from-dispatcher-env"
```
启动时 compute-server 自动调用 `POST /api/v1/compute/register`。

**方式 B：Admin 预注册**（推荐）
```bash
# 由管理员在 Dispatcher 端注册
curl -X POST https://dispatch.example.com/api/v1/admin/nodes/register \
  -H "Authorization: Bearer $DISPATCH_SERVER_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "node_id": "example-hk-highbw",
    "agent_token": "CHANGE_ME",
    "name": "Example HK High Bandwidth",
    "region": "HK",
    "provider": "ExampleProvider",
    "tags": ["hk", "high_bandwidth"],
    "static_profile": {
      "cpu_cores": 2,
      "memory_mb": 2048,
      "bandwidth_mbps": 300,
      "runtime": {"shell": true, "python": true, "hermes": true},
      "limits": {"max_parallel_tasks": 3}
    }
  }'
```
注意：`agent_token` 是 **node token**（计算节点身份），不是 client token。

#### 工作目录清理 (Cleanup)

每个 Shell 任务在 `work_dir/tasks/<task_id>/` 下创建独立目录：

```
<work_dir>/
  tasks/
    <task_id>/
      work/           # 命令 cwd — 任务执行主目录
      tmp/            # TMPDIR/TEMP/TMP — 临时文件
      artifacts/      # 任务希望保留的结果文件
      logs/           # 本地日志缓存
      meta.json       # 任务元信息（状态、时间、清理策略）
  cache/              # 全局缓存（不会被自动清理）
  quarantine/         # 异常文件隔离（可选）
```

`work/` 和 `tmp/` 可以安全删除；`artifacts/` 默认保留到任务成功保留时间。
Hermes workspace（`allowed_hermes_workspaces`）**永远不会**被自动清理。

**默认清理策略：**

| 任务状态 | 默认行为 | 保留时间 |
|---------|---------|---------|
| `success` | ✅ 删除 | 1 小时 (`keep_success_seconds: 3600`) |
| `failed` | ✅ 删除 | 24 小时 (`keep_failed_seconds: 86400`) |
| `timeout` | ✅ 删除 | 24 小时 (`keep_timeout_seconds: 86400`) |
| `cancelled` | ✅ 删除 | 1 小时 (`keep_cancelled_seconds: 3600`) |

**配置方式（`node.yaml` 中 `cleanup` 块）：**

```yaml
cleanup:
  enabled: true                    # 全局开关
  cleanup_success: true            # 清理成功任务目录
  cleanup_failed: true             # 清理失败任务目录
  cleanup_timeout: true            # 清理超时任务目录
  cleanup_cancelled: true          # 清理取消任务目录
  keep_success_seconds: 3600       # 成功任务保留时间
  keep_failed_seconds: 86400       # 失败任务保留时间
  keep_timeout_seconds: 86400      # 超时任务保留时间
  keep_cancelled_seconds: 3600     # 取消任务保留时间
  cleanup_interval_seconds: 300    # 后台扫描间隔（秒）
  max_work_dir_size_mb: 4096       # 工作目录总大小上限
  max_task_dir_size_mb: 1024       # 单任务大小上限（可选）
  delete_empty_dirs: true          # 删除空目录
  legacy_cleanup: false            # 是否清理旧版扁平布局（work_dir/<id>/）
```

**安全限制：**
- cleanup 只扫描 `work_dir/tasks/`，不扫 `/`、`~`、`/home`、`/opt`
- task_id 必须匹配 `[A-Za-z0-9_.-]{1,128}`，包含 `..`、`/`、`\0` 等的被拒绝
- 路径穿越（如 `../../etc`）被 `resolve_under()` 严格拒绝
- `allowed_hermes_workspaces` 永远不会被自动清理
- 当前 `running_tasks` 中的任务目录永远不会被删除
- 缺失 `meta.json` 的目录按保守策略处理（最长保留时间或 7 天孤儿阈值）

**磁盘压力上报：**
当 `work_dir/tasks/` 总大小超过 `max_work_dir_size_mb` 时，Compute Server 会在 heartbeat
的 `status_json` 中上报 `disk_pressure: true` 和 `cleanup_warning` 消息。

**手动清理：**
```bash
# 清理单个任务目录
rm -rf /opt/wuzhu-dispatch/work/tasks/<task_id>

# 清理所有超过 24h 的任务目录
find /opt/wuzhu-dispatch/work/tasks -maxdepth 1 -type d -mtime +1 \
  -name "[A-Za-z0-9_.-]*" -exec rm -rf {} +

# 查看当前占用
du -sh /opt/wuzhu-dispatch/work
```

**禁用清理：**
```yaml
cleanup:
  enabled: false
```

---

### 3. Client（客户端）

**职责**：提交任务请求、查询状态、获取结果

```bash
# 安装
cd /opt/wuzhu-dispatch/client
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

**方式 A：YAML 配置文件（推荐）**
```yaml
# ~/.config/wuzhu-dispatch/client.yaml
dispatcher_url: "https://dispatch.example.com"
client_token: "your-client-api-token-here"
```

```bash
dispatch-client -c ~/.config/wuzhu-dispatch/client.yaml task list
```

**方式 B：环境变量**
```bash
export DISPATCH_URL=https://dispatch.example.com
export DISPATCH_CLIENT_TOKEN=your-token

dispatch-client task list
dispatch-client node list
dispatch-client task create --type probe_dns --payload examples/tasks/probe_dns.json
```

CLI 仅支持 Bearer Client API Token 认证，不支持 Cookie/CSRF 登录。
如需 Web 管理，使用 `/admin` Dashboard 登录。

---

## 管理员入门

### 创建 owner 用户

```bash
curl -X POST https://dispatch.example.com/api/v1/admin/users \
  -H "Authorization: Bearer $DISPATCH_SERVER_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "your-strong-password", "role": "owner"}'
```

### 创建第一条任务

```bash
# probe_echo 测试
dispatch-client task create \
  --type probe_echo \
  --data '{"execution": {"mode": "shell", "command": "echo hello world && hostname"}}'

# 查看任务状态
dispatch-client task list
dispatch-client task show <task_id>

# 查看日志
dispatch-client task logs <task_id>
```

---

## 排错指南

| 现象 | 检查点 |
|------|--------|
| Dispatcher 启动失败 | `.env` 配置是否正确？MySQL 能否连接？`ENVIRONMENT=production` 时密钥是否修改？先设 `ENVIRONMENT=development` 测试 |
| Compute Server 不上线 | `dispatcher_url` 写对了？`node_id` 已注册？`agent_token` 匹配？DISPATCHER 的 `verify_compute_node` 要求 token 和 node_id 匹配 |
| 任务一直 pending | 有在线且匹配的节点？节点的 `runtime` 是否支持任务的 `execution.mode`？任务的 `requirements.tags` 是否匹配？节点 `max_parallel_tasks` 是否已满？ |
| 任务 running 后 timeout/retry | Compute Server 是否还在线？lease 是否续租？`lease_seconds` 是否合理？节点日志查看 `journalctl -u wuzhu-compute-server` |
| Dashboard 登录失败 | 用户名密码正确？`dispatch_session` cookie 存在？服务端日志有提示 |
| Dashboard 空白页 / JS 被拦截 | 浏览器开 DevTools → Console 看 CSP 错误。确保 dashboard.js 放在 `/static/dashboard.js` 且 CSP 允许 `script-src 'self'` |
| CORS 错误 | `CORS_ALLOWED_ORIGINS` 是否包含 Dashboard 的域名？ |
| 429 Too Many Requests | 频率过高，检查 `rate_limit_*` 配置 |

---

## 安全说明

| 项目 | 要求 |
|------|------|
| **HTTPS** | Dispatcher 必须通过 HTTPS 暴露（nginx/Caddy 反代） |
| **MySQL** | 只允许 Dispatcher 所在机器访问（`bind-address=127.0.0.1` 或防火墙） |
| **Compute Server** | 不需要 MySQL，只需要 HTTPS 连接 Dispatcher |
| **Client** | 不持有 node token，不知道 MySQL 密码 |
| **DISPATCH_SERVER_SECRET** | 仅用于 bootstrap / admin / system 操作，生产环境优先使用用户登录或 `client_api_token` |
| **Dashboard** | 使用 HttpOnly + Secure + SameSite Cookie，不使用 localStorage |
| **日志** | Dashboard 中所有用户可控数据通过 `textContent` 渲染，禁止 `innerHTML`；所有 inline `style` 已替换为 CSS class |
| **配置文件权限** | `.env`、`node.yaml`、`client.yaml` 设为 `600` |
| **agent_token** | Compute Server 的 node token，不是 client token，不要混用 |
| **CSP** | `script-src 'self'` — 无 inline script；`style-src 'self'` — 无 unsafe-inline |
| **client_api_token.scope** | 已参与权限判断。支持 allowed_templates / allowed_modes / denied_modes / allowed_target_tags / max_priority / max_timeout_seconds / can_target_specific_node 等细粒度字段，详见 `docs/security.md` |

### 公开仓库注意事项

> 本仓库为 **public** 仓库，请遵守以下规则：
> - 不要提交真实配置（`.env`、`node.yaml`、`client.yaml`、`*.pem`、`*.key`）
> - 示例文件必须使用占位符（`CHANGE_ME`、`your-*`）
> - 生产环境密钥必须修改默认值
> - 如果历史提交过真实 token/密码，必须立即轮换（详见 `docs/security.md`）

---

## 调度器关键设计

- **DB 中的 running 任务数** — 而非仅信 heartbeat 中的 `running_tasks`
- **FOR UPDATE 行锁** — `pull_task_for_node` 锁定 node 行，同一节点并发 pull 不会突破 `max_parallel_tasks`
- **execution.mode 硬过滤** — 自动检查 `payload.execution.mode` 是否匹配节点 `runtime`，即使 `requirements.runtime` 没写也能过滤
- **原子拉取** — 条件 UPDATE + rowcount 检查
- **长轮询** — `?wait_seconds=25`，无任务时 Dispatcher 保持连接最多 25 秒
- **硬过滤 + 打分** — 先按 tags/runtime/CPU/内存/带宽 硬过滤，再按空闲率/优先级/网络适配度打分
- **租约续期** — lease 边界保护 `[task_lease_seconds, max_lease_seconds]`
- **超时回收** — 后台调度器每 30s 扫描租约过期任务，自动释放并重试

### MVP 限制

- **取消是软取消**：`cancel_task` 只改数据库状态，不会 kill Compute Server 上的进程
- **Artifact 暂不支持**：任务结果通过 `result` JSON 和日志查询，文件下载后续版本实现
- **Dashboard 无历史曲线**：`node_metrics_history` 表已创建，后续通过 Chart.js 实现

---

## 测试

```bash
pip install -r dispatcher/requirements.txt
python3 -m compileall -q dispatcher compute-server client common scripts
python3 scripts/test_pipeline.py          # 48 项单元测试
python3 scripts/test_cleanup.py           # 66 项清理模块测试
python3 scripts/test_templates.py         # 37 项模板/权限/调度/quick 测试
python3 scripts/test_skill.py             # 48 项 Skill 层测试
python3 scripts/test_route_integration.py  # 50 项路由集成测试
```

---

## 后续扩展

| 方向 | 说明 |
|------|------|
| Redis/NATS | 消息队列解耦任务调度，替代 DB 轮询 |
| Docker 隔离 | 完善 Docker Executor，容器化任务隔离 |
| Dashboard 图表 | node_metrics_history + Chart.js 历史曲线 |
| 对象存储 | S3/MinIO 中转 artifact 文件 |
| mTLS | 双向 TLS 替代 Bearer Token |
| 任务血缘 DAG | 任务依赖图，A 完成后自动触发 B |
| 多云伸缩 | 根据队列深度自动扩缩容 Worker 节点 |
