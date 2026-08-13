# stackAnalys

本项目用于每天定时采集 A 股市场数据，写入本地 DuckDB，并用 Parquet 做分区备份，供后续股票分析模型使用。第一版遵循“零积分、零付费优先”：使用 AKShare、BaoStock、公开披露入口和 RSS，不依赖 Tushare 积分或付费 API。

## 推荐 Python 版本

当前项目推荐使用 Python 3.11。

原因：AKShare、BaoStock、pandas、pyarrow、duckdb 等依赖在 Python 3.11 上的 wheel 和生态兼容性更稳；Python 3.14 属于较新的解释器版本，部分第三方数据源库或底层网络依赖可能存在兼容性边缘问题。

项目已添加：

```text
.python-version
```

内容为：

```text
3.11
```

## 安装

requirements 兼容性检查已使用 Python 3.11 临时虚拟环境完成，依赖可解析安装；当前运行环境如仍是 Python 3.14，建议重建为 Python 3.11。


```bash
cd /path/to/stackAnalys
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## API 与扩展

第一阶段不需要填写任何 API key。AKShare、BaoStock 和 RSS 都是零 token 入口。

所有真实密钥和私有配置都放在 `stackAnalys/` 工程目录外，不提交 Git。默认通用 key 文件路径为仓库根目录下：

```text
stock/key/api_keys.yaml
```

也可以由 hermes-agent 使用环境变量指向 `~/key/`：

```bash
export STOCK_API_KEYS_PATH=~/key/api_keys.yaml
```

通用 key 文件示例：

```yaml
future_extensions:
  tushare:
    enabled: false
    token: ""
  newsapi:
    enabled: false
    api_key: ""
akshare:
  enabled: false
```

LLM 独立配置推荐放在：

```text
stock/key/stackAnalys_llm.yaml
```

或由 hermes-agent 放在 `~/key/stackAnalys_llm.yaml`，运行脚本时通过 `--config ~/key/stackAnalys_llm.yaml` 指定。


## 股票数据层重构执行方案

本节是给执行服务器使用的实施 runbook。执行服务器必须按本节顺序改造和运行，目标是支持 5000+ A 股日频分析、北交所、MA/因子/回测/日报，并降低对单一免费接口的依赖。

### 1. 数据源路由

统一入口必须保持在：

```text
src/collectors/market_provider.py
```

历史日线必须按市场自动路由：

- 沪深 A 股：BaoStock `query_history_k_data_plus` 优先，东方财富历史接口 `akshare.stock_zh_a_hist` 仅作备用。
- 北交所：使用独立 `src/providers/bse_daily.py`，主源为 `sina_history_direct`，备用源为 `tencent_history_direct`；不再通过旧 `MarketDataProvider` 的 AKShare 腾讯历史源作为 BSE 主源。
- 实际主备源以仓库根 `config/settings.yaml` 的 `data_source` 为准；`MarketDataProvider` 必须继承该配置，不得在脚本里硬编码主备源。
- 股票代码必须统一识别 `sh.600000`、`600000.SH`、`sz.000001`、`000001.SZ`、`bj.830799`、`830799.BJ` 和纯 6 位代码。
- `4/8/9` 开头的纯 6 位代码按北交所处理，`6` 开头按沪市处理，`0/3` 开头按深市处理。

当日快照主源使用新浪实时行情接口 `sina_spot_daily`。每日 18:30 之后采集一次全市场 spot 快照，写入本地缓存，并在收盘后补齐当天日线。15:10 前不得把盘中快照写成正式日线，只能写入 `daily_price_temp`。

统一输出字段至少包含：

```text
trade_date, ts_code, open, high, low, close, preclose, volume, amount, pct_chg, turn, adjust_type, source, updated_at
```

所有数据源必须有 retry + timeout。单股票失败只能写入日志和失败状态，不得中断全市场任务；主源失败必须自动切换备用源。

> **限速原则**：新浪 host 级限速默认 5 QPS；BaoStock 历史补全必须串行执行，不进入 aiohttp 并发框架。

### 2. 本地缓存策略

当前存储方式是 **DuckDB + Parquet 数据湖**。执行服务器不要新建一套独立缓存；必须复用：

```text
src/storage/lake_store.py
```

缓存要求：

- 历史 K 线、当日实时快照、指标输入数据都必须落本地数据湖或 DuckDB 元数据。
- 查询优先读取本地视图，只有缺口区间才增量请求远端。
- 禁止重复全量拉取。全市场任务每只股票都要先查本地最大 `trade_date`，从下一交易日开始补。
- `daily_price` 主键固定为 `ts_code + trade_date + adjust_type`。
- 备用源写入同一主键时，必须按 `source_priority` 和 `updated_at` 去重。
- 分区替换只能替换受影响年月，不能删除整个数据湖。
- 显式传入 `--db-path` 时，数据湖默认使用该 DuckDB 所在数据根下的 `lake/`，例如 `../stock_local_ai_data/stock_data_v2.duckdb` 对应 `../stock_local_ai_data/lake/`，避免与默认 `data.root` 错位。

实时快照需要新增或复用一个本地数据集，建议命名：

```text
lake/realtime_snapshot/snapshot_date=YYYY-MM-DD/part-*.parquet
```

快照主键建议为：

```text
ts_code + snapshot_date
```

### 3. 服务器执行顺序

第一次部署：

```bash
cd /path/to/stackAnalys
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export STOCK_DATA_ROOT=/path/to/stock_local_ai_data
export STOCK_DB_PATH="$STOCK_DATA_ROOT/stock_data_v2.duckdb"

python scripts/init_db.py --db-path "$STOCK_DB_PATH"
python scripts/backfill_history.py --db-path "$STOCK_DB_PATH" --all --start 2018-01-01
python scripts/check_data_quality.py --db-path "$STOCK_DB_PATH"
```

每日交易日执行：

```bash
cd /path/to/stackAnalys
source .venv/bin/activate
export STOCK_DATA_ROOT=/path/to/stock_local_ai_data
export STOCK_DB_PATH="$STOCK_DATA_ROOT/stock_data_v2.duckdb"

# 18：30 后采集东方财富全市场实时快照，并补当天 daily_price
python scripts/run_daily.py --db-path "$STOCK_DB_PATH"

# 更新后运行分析任务
python scripts/daily_analysis_job.py --db-path "$STOCK_DB_PATH"
```

失败重跑：

```bash
python scripts/run_daily.py --db-path "$STOCK_DB_PATH"
python scripts/rescan_history.py --db-path "$STOCK_DB_PATH" --start 2026-06-01 --end 2026-06-01 --all --include-index
```

重跑必须保持幂等：同一主键不重复插入，只更新缺失或更高优先级数据。

重复键维修：

```bash
# 只检查，不改数据
python scripts/repair_price_duplicates.py --db-path "$STOCK_DB_PATH" --dedupe-partitions

# 就地重写有重复键的 daily_price 分区；旧分区会先备份到 lake/backups/
python scripts/repair_price_duplicates.py --db-path "$STOCK_DB_PATH" --dedupe-partitions --execute

# 验证 daily_price 主键和基础价格质量
python scripts/validate_price_data.py --db-path "$STOCK_DB_PATH" --no-persist
```

### 4. 验收标准

执行服务器完成后必须通过以下检查：

```bash
python scripts/test_market_provider.py
python scripts/test_lake_store.py
python scripts/check_data_quality.py --db-path "$STOCK_DB_PATH"
python scripts/market_overview.py --db-path "$STOCK_DB_PATH"
python scripts/rank_stocks.py --db-path "$STOCK_DB_PATH" --start 2025-01-01 --top 20
python scripts/backtest_factor.py --db-path "$STOCK_DB_PATH" --start 2025-01-01 --top-n 20
```

DuckDB 验收 SQL：

```sql
SELECT COUNT(DISTINCT ts_code) AS stock_count FROM v_daily_price;
SELECT exchange, COUNT(*) FROM v_stock_basic_latest GROUP BY exchange ORDER BY exchange;
SELECT ts_code, MIN(trade_date), MAX(trade_date), COUNT(*) FROM v_daily_price GROUP BY ts_code ORDER BY COUNT(*) DESC LIMIT 20;
SELECT task_name, status, COUNT(*) FROM daily_update_log GROUP BY task_name, status ORDER BY task_name, status;
```

最低验收口径：

- `v_daily_price` 能覆盖沪深和北交所。
- 全市场日频股票数量目标为 5000+，以当日股票列表为准。
- 单股票失败记录在 `daily_update_log`，全市场任务继续运行。
- MA、因子、回测、日报脚本只读本地 DuckDB/Parquet，不触发远端行情采集。

## 行情数据源优先级

当前正式行情链路使用统一入口：

```text
src/collectors/market_provider.py
```

优先级：

1. 当日行情：新浪 `sina_spot_daily` 作为日采主源
2. 历史补全：BaoStock 作为主补全/校验源
3. AKShare 腾讯历史源仅作为可选补全源，当前默认不作为主源

当前配置位于仓库根 `config/settings.yaml`：

```yaml
data_source:
  daily_price_current_primary: sina_spot_daily
  daily_price_history_primary: baostock
  daily_price_history_fallback: eastmoney
  daily_price_bse_primary: eastmoney
  daily_price_bse_fallback: baostock
  index_daily_primary: baostock
  index_daily_fallback: eastmoney
  stock_basic_primary: akshare
  stock_basic_fallback: baostock
```

`MarketDataProvider.get_daily_price()` 和 `MarketDataProvider.get_index_daily()` 会返回统一字段：

```text
trade_date, ts_code, open, high, low, close, volume, amount, source
```

如果主源失败，会记录异常并自动 fallback 到备用源。fallback 源带断路器：同一 fallback 源连续失败达到 `collector.fallback_circuit_breaker_failures` 后，会在 `collector.fallback_circuit_breaker_cooldown_seconds` 内跳过后续 fallback 调用，避免 EastMoney 等外部接口断连时把全市场串行任务放大到数小时。

AKShare/Tencent 历史请求前会按配置限速；requests 类失败会按 `collector.retry_times` / `collector.retry_sleep_seconds` 重试。近期缺口回补只合并日历连续日期，周五到周一会拆成两个请求；返回数据仍按缺失交易日过滤后写入。

测试命令：

```bash
python scripts/test_market_provider.py
```

报告输出：

```text
data/samples/market_provider_test.md
```

## 免费数据源

项目已预置这些免费源入口：

- AKShare：免费聚合源，不需要 token。
- 东方财富：通过 AKShare 及 RSS 配置接入。
- 新浪财经：通过 AKShare 及 RSS 配置接入。
- 腾讯行情：通过 AKShare 入口预留。
- 网易行情：通过 AKShare 入口预留。
- BaoStock：免费登录式 A 股数据源，不需要 token。
- Yahoo Finance：通过 `yfinance` 采集 `.SS` / `.SZ` 等符号。
- Stooq：通过公开 CSV 接口采集轻量行情。
- RSS 新闻：从 `settings.yaml` 的 `free_sources.rss_news.feeds` 配置。

检查本机免费源依赖状态：

```bash
python scripts/check_free_sources.py
```

第一阶段数据源策略：

| 数据类型 | 主接口 | 备用接口 | 说明 |
| --- | --- | --- | --- |
| 股票列表 | AKShare `stock_info_a_code_name` | BaoStock `query_all_stock` | 不用积分、不用 token |
| 日线行情（历史补全） | BaoStock `query_history_k_data_plus` | AKShare 东方财富历史接口，可选 AKShare 腾讯历史源 | 历史补全串行执行，不进入 aiohttp |
| 北交所日线 | `sina_history_direct` | `tencent_history_direct` | 独立 provider，timeout + retry + fallback |
| 股票行业映射 | BaoStock `query_stock_industry` | 旧映射保留 | 写入 `stock_industry_map` 维表，不覆盖 `stock_basic.industry` |
| 当日实时快照 | 新浪 `hq.sinajs.cn/list=...` | `missing_daily_price` 后续修复 | 18:30 后落正式日线，盘中只写临时表 |
| 复权行情 | AKShare `stock_zh_a_hist(adjust="qfq"/"hfq")` | BaoStock `adjustflag` | 前复权/后复权都能做 |
| 指数行情 | AKShare `index_zh_a_hist` | BaoStock 指数 K 线 | 主流指数够用 |
| 财务指标 | AKShare 财务类接口 | BaoStock 季度财务 | 免费接口可用，字段完整性弱于 Tushare |
| 行业板块数据 | 本地聚合 `stock_industry_map + daily_price` | AKShare 申万指数 optional | 生产主链路读取 `v_industry_board`，不依赖东方财富板块接口 |
| 新闻 | AKShare 东方财富/央视/全球财经新闻 | RSS | 免费新闻够做事件库雏形 |
| 公告 | 巨潮资讯 | 上交所/深交所/北交所 | 公开披露数据，后续加严格限频 |
| 机构持仓 | AKShare `stock_institute_hold` 等 | 公告/年报解析 | 只能看公开披露 |
| 历史长数据 | BaoStock | AKShare | BaoStock 更适合长周期回填 |

不同免费源的数据口径、复权规则、字段命名和稳定性不完全一致。核心数据保留 `source` 和 `source_priority` 字段，方便后续对账、去重和标准化。

## 存储架构

当前存储方式是 **DuckDB + Parquet 数据湖**：

- Parquet 是主存储，默认根目录为 `lake/`。
- DuckDB 只保存 `dataset_manifest`、`ingestion_job`、`ingestion_record`、`symbol_coverage`、`unique_key_index`、日志、质量检查和查询视图。
- 已有 DuckDB 实体表和旧 Parquet 文件不会被初始化脚本删除；新采集流程写入数据湖。

分区路径：

```text
lake/daily_price/adjust_type=qfq/year=YYYY/month=MM/part-*.parquet
lake/index_daily/year=YYYY/month=MM/part-*.parquet
lake/stock_basic/snapshot_date=YYYY-MM-DD/part-*.parquet
lake/stock_industry_map/effective_date=YYYY-MM-DD/part-*.parquet
lake/industry_board_local/year=YYYY/month=MM/part-*.parquet
lake/sw_industry_index_daily/year=YYYY/month=MM/part-*.parquet  # optional
lake/industry_board_validation/year=YYYY/month=MM/part-*.parquet
lake/backups/...
```

主键与去重规则：

- `daily_price`: `ts_code + trade_date + adjust_type`
- `index_daily`: `index_code + trade_date`
- `stock_basic`: 按 `snapshot_date` 存快照，`v_stock_basic_latest` 查询最新快照。
- `stock_industry_map`: DuckDB 行业维表，用 `effective_date/is_current` 管理版本，不改写 `stock_basic.industry`。
- `industry_board_local`: 本地聚合行业板块，主键为 `trade_date + industry_source + industry_level + industry_code`，统一通过 `v_industry_board` 读取。
- 同一主键按数据源优先级去重：BaoStock `100`、AKShare `80`、Yahoo `60`、Stooq `50`。
- 数据源优先级相同时，保留 `updated_at` 最新的记录。
- Upsert 只替换受影响的年月或快照分区，替换前会先复制旧分区到 `lake/backups/`。

常用视图：

```sql
SELECT * FROM v_daily_price;
SELECT * FROM v_index_daily;
SELECT * FROM v_stock_basic_latest;
SELECT * FROM v_stock_industry_map;
SELECT * FROM v_industry_board;
SELECT * FROM data_source_status;
```

行业读取推荐 SQL：

```sql
SELECT b.ts_code, b.name, m.industry_name, m.sw_code_2021
FROM v_stock_basic_latest AS b
LEFT JOIN v_stock_industry_map AS m USING(ts_code);
```

## 初始化数据库

```bash
python scripts/init_db.py
```

默认数据根目录在项目外部的 `stock_local_ai_data/`，DuckDB、Parquet 数据湖、日志和分析报告都应放在这个外部目录中，不写入仓库目录。当前默认 DuckDB 为 `stock_data_v2.duckdb`。

如果数据库文件已经存在，初始化脚本会补齐元数据表并刷新视图，不会删除旧库、旧表或旧 Parquet。需要手动强制重建 manifest/刷新结构时运行：

```bash
python scripts/init_db.py --force
```

### 指定数据库位置

数据库位置的优先级：**`--db-path` 命令行参数 > `STOCK_DB_PATH` 环境变量 > 仓库根 `config/settings.yaml` 中的配置**。

```bash
# 命令行参数（最高优先级）
python scripts/init_db.py --db-path "$STOCK_DB_PATH"

# 环境变量（中优先级）
export STOCK_DATA_ROOT="/path/to/stock_local_ai_data"
export STOCK_DB_PATH="$STOCK_DATA_ROOT/stock_data_v2.duckdb"
python scripts/init_db.py

# 配置文件（默认）
# stock/config/settings.yaml:
#   database:
#     duckdb_path: ../../stock_local_ai_data/stock_data_v2.duckdb
#   data:
#     root: ../../stock_local_ai_data
#     lake_root: lake
#     log_root: logs
#     report_root: reports
```

路径支持 `~`、绝对路径和相对路径（相对于项目根目录）。推荐运行环境显式设置 `STOCK_DATA_ROOT` 和 `STOCK_DB_PATH`；所有脚本仍可通过 `--db-path` 临时指定数据库文件。新增脚本统一使用 `Path(...).expanduser().resolve()` 解析路径。

数据湖、日志和原始数据目录可在仓库根 `config/settings.yaml` 的 `data` 段配置；也可以通过 `STOCK_DATA_ROOT` 统一指定相对数据目录的根路径。

~~~~
## 从旧库重建（已废除）

旧版 DuckDB 实体表可以重建为当前的 DuckDB 元数据 + Parquet 数据湖结构：

```bash
python scripts/rebuild_from_legacy_db.py --legacy-db /path/to/old/stock_data.duckdb --db-path /data/stackAnalys/stock_data.duckdb --lake-root /data/stackAnalys/lake --replace
```
~~~~

脚本会导入 `daily_price`、`index_daily`、`stock_basic`，复制已有日志表，并刷新 manifest、覆盖范围和查询视图。`--replace` 只会清理目标库和目标数据湖，不会删除旧库。

## 手动运行每日更新

当天：

```bash
python scripts/run_daily.py
```

指定日期：

```bash
python scripts/run_daily.py --date 2026-05-29
```

指定数据库位置：

```bash
python scripts/run_daily.py --db-path /data/stackAnalys/stock_data.duckdb
```

第一阶段不需要 Tushare token。每日更新会使用 AKShare/BaoStock/RSS 免费入口，并通过 `LakeStore` 写入 Parquet 数据湖。

## 行业板块本地聚合

行业/板块类字段从 `stock_basic` 主基础表逻辑中隔离。`stock_basic.industry` 保留为历史兼容字段，不删除、不覆盖；新分析逻辑统一读取 `v_stock_industry_map` 和 `v_industry_board`：

```sql
SELECT b.*, m.industry_name, m.sw_code_2021
FROM v_stock_basic_latest AS b
LEFT JOIN v_stock_industry_map AS m USING(ts_code);
```

行业板块生产链路不再依赖东方财富行业板块接口：

```text
stock_industry_map + daily_price
→ industry_board_local
→ v_industry_board
```

`stock_industry_map` 维表字段包含行业来源、层级、版本和当前标记：

```sql
CREATE TABLE IF NOT EXISTS stock_industry_map (
    ts_code TEXT,
    name TEXT,
    industry_source TEXT,
    industry_code_l1 TEXT,
    industry_name_l1 TEXT,
    industry_code_l2 TEXT,
    industry_name_l2 TEXT,
    industry_code_l3 TEXT,
    industry_name_l3 TEXT,
    industry_name TEXT,
    sw_code_2021 TEXT,
    source TEXT,
    effective_date DATE,
    is_current BOOLEAN,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    fetched_at TIMESTAMP
);
```

BaoStock 行业映射更新：

```bash
python scripts/update_stock_industry_map.py --db-path "$STOCK_DB_PATH" --dry-run
python scripts/update_stock_industry_map.py --db-path "$STOCK_DB_PATH"
```

脚本使用 `baostock.query_stock_industry()`，标准化为 `daily_price.ts_code` 格式，BaoStock 失败时只输出 WARNING，不阻塞其他任务；不会修改 `stock_basic`，也不会删除旧行业映射。

本地行业板块回填/增量：

```bash
python scripts/build_industry_board_local.py --db-path "$STOCK_DB_PATH" --start 2025-01-01 --end 2026-06-05
python scripts/build_industry_board_local.py --db-path "$STOCK_DB_PATH" --date 2026-06-05
python scripts/build_industry_board_local.py --db-path "$STOCK_DB_PATH" --latest
python scripts/build_industry_board_local.py --db-path "$STOCK_DB_PATH" --start 2025-01-01 --end 2026-06-05 --dry-run
```

聚合逻辑按 `ts_code + trade_date + adjust_type` 去重，默认使用 `adjust_type=qfq`；计算行业成员数、未映射数、1/5/20 日收益、成交额、宽度、流动性和强度分数。5 日或 20 日窗口不足时对应字段保持 `NULL`，不会填 0。结果写入 `DATA_ROOT/lake/industry_board_local/`，推荐系统、特征系统、日报和可视化统一读取 `v_industry_board`。

如果 `v_industry_board` 为空或某只股票缺少行业映射：

- 行业强度使用 `NULL`/中性处理，推荐系统继续运行。
- 推荐明细写入 `industry_data_source='missing'`、`industry_missing=true` 和 `industry_warning`。
- 报告和验证脚本输出 WARNING，不整体失败。

AKShare 申万行业指数只是 optional 补充，不是生产主依赖：

```bash
python scripts/backfill_sw_industry_index.py --db-path "$STOCK_DB_PATH" --start 2025-01-01 --end 2026-06-05 --dry-run
python scripts/backfill_sw_industry_index.py --db-path "$STOCK_DB_PATH" --start 2025-01-01 --end 2026-06-05
```

失败时只输出 WARNING，不影响 `industry_board_local` 和推荐系统。

验证行业板块质量：

```bash
python scripts/validate_industry_board.py --db-path "$STOCK_DB_PATH"
```

验证会检查 `v_stock_industry_map`、映射覆盖率、`industry_board_local`、`v_industry_board`、日期范围、每日行业数量、未映射数量、成员数分布、收益范围、成交额非负、重复主键和推荐系统降级语义。验证结果写入 `DATA_ROOT/lake/industry_board_validation/`。

迁移旧 `stock_basic.industry` 到维表仍可作为一次性兼容动作：

```bash
python scripts/migrate_industry_map.py --db-path "$STOCK_DB_PATH" --dry-run
python scripts/migrate_industry_map.py --db-path "$STOCK_DB_PATH"
```

脚本幂等，可重复执行；只读取 `stock_basic` 非空行业值并 upsert 到 `stock_industry_map`，`source='legacy_stock_basic'`，不改写原表。

北交所历史行情当前推荐源：

- 主源：`sina_history_direct`
- 备用：`tencent_history_direct`

验证 SQL：

```sql
SELECT COUNT(*) AS mapped_rows, COUNT(DISTINCT ts_code) AS mapped_stocks
FROM stock_industry_map;

SELECT
    COUNT(*) AS stock_count,
    COUNT(m.industry_name) AS mapped_count,
    ROUND(COUNT(m.industry_name) * 100.0 / NULLIF(COUNT(*), 0), 2) AS coverage_pct
FROM v_stock_basic_latest AS b
LEFT JOIN stock_industry_map AS m USING(ts_code);

SELECT COUNT(*) FROM v_stock_basic_latest;
SELECT COUNT(*) FROM stock_basic;
```

## 每日数据监视

每日监视脚本会先判断目标日期是否交易日、检查本地是否已有 `daily_price` 数据，再用少量 `probe_symbols` 低频探测免费源是否已经更新。只有远端已经出现目标日期数据，才会触发一次 `run_daily_update()`；如果远端未更新或探测失败，会写入 `data_source_status` 和日报，不启动全市场采集。

```bash
python scripts/watch_daily_update.py --db-path "$STOCK_DB_PATH"
python scripts/watch_daily_update.py --date 2026-05-29 --db-path "$STOCK_DB_PATH"
```

状态表：

```sql
SELECT source, dataset_name, latest_remote_date, latest_local_date, status, message, checked_at
FROM data_source_status;
```

监视器遵循低频、限速、有限退避和失败记录策略，不提高并发，不绕过反爬，不删除旧数据，不重复写已有主键。配置入口：

```yaml
watcher:
  enabled: true
  check_times:
    - "18:30"
    - "19:00"
    - "20:00"
  probe_symbols:
    - "sh.000300"
    - "sz.000001"
  request_sleep_min: 0.8
  request_sleep_max: 2.5
  max_daily_attempts: 3
  retry_backoff_seconds:
    - 30
    - 120
    - 300
  stop_if_source_unstable: true
```

## 回填历史数据

默认从 `settings.yaml` 的 `collector.backfill_start_date` 回填到今天：

```bash
python scripts/backfill_history.py
```

指定区间：

```bash
python scripts/backfill_history.py --start 2015-01-01 --end 2026-05-29
```

指定数据库位置：

```bash
python scripts/backfill_history.py --db-path /data/stackAnalys/stock_data.duckdb --all --start 2020-01-01
```

重扫指定范围和股票，不会重复插入同一主键：

```bash
python scripts/rescan_history.py --start 2024-01-01 --end 2024-12-31 --codes sz.000001,sh.600000
python scripts/rescan_history.py --start 2024-01-01 --end 2024-12-31 --all --include-index
```

## 启动 Scheduler

默认调度时间由仓库根 `config/settings.yaml` 的 `schedule.daily_update_time` 控制。当前日采目标要求交易日 `18:30 Asia/Shanghai` 后执行，以便使用新浪实时行情 spot 快照补当天日线。执行服务器在启用 `scripts/scheduler.py` 前必须确认配置为：

```yaml
schedule:
  daily_update_time: "18:30"
```

手工运行日采入口：

```bash
python scripts/run_daily.py --date 2026-06-02 --batch-size 80 --sina-qps 5 --max-concurrency 10 --timeout 10 --retries 2
```

新浪日采默认启用断点续传：每完成 `collector.sina_write_flush_batches` 个请求批次，会批量写入行情/缺失记录，并写入 `daily_collection_checkpoint`。同一交易日重跑时会跳过已完成批次，只补未完成批次。

```yaml
collector:
  sina_write_flush_batches: 10
  sina_resume_enabled: true
```

强制重跑全部新浪批次：

```bash
python scripts/run_daily.py --date 2026-06-02 --no-resume
```

盘中只写临时表：

```bash
python scripts/run_daily.py --date 2026-06-02 --write-temp-before-close
```

收盘后修复新浪缺失记录：

```bash
python scripts/run_daily.py --date 2026-06-02 --repair-missing
```

```bash
python scripts/scheduler.py
```

指定数据库位置：

```bash
python scripts/scheduler.py --db-path "$STOCK_DB_PATH"
```

同一时间只允许一个任务实例运行。

## Cron 示例

```cron
TZ=Asia/Shanghai
STOCK_DATA_ROOT=/path/to/stock_local_ai_data
STOCK_DB_PATH=/path/to/stock_local_ai_data/stock_data_v2.duckdb
5 15 * * 1-5 cd /path/to/stackAnalys && .venv/bin/python scripts/run_daily.py --db-path "$STOCK_DB_PATH" >> "$STOCK_DATA_ROOT/logs/cron.log" 2>&1
20 15 * * 1-5 cd /path/to/stackAnalys && .venv/bin/python scripts/daily_analysis_job.py --db-path "$STOCK_DB_PATH" >> "$STOCK_DATA_ROOT/logs/analysis_cron.log" 2>&1
```

Watcher 低频增量示例：

```cron
TZ=Asia/Shanghai
STOCK_DATA_ROOT=/path/to/stock_local_ai_data
STOCK_DB_PATH=/path/to/stock_local_ai_data/stock_data_v2.duckdb
5 15 * * 1-5 cd /path/to/stackAnalys && .venv/bin/python scripts/watch_daily_update.py --db-path "$STOCK_DB_PATH" >> "$STOCK_DATA_ROOT/logs/watcher_cron.log" 2>&1
30 15 * * 1-5 cd /path/to/stackAnalys && .venv/bin/python scripts/watch_daily_update.py --db-path "$STOCK_DB_PATH" >> "$STOCK_DATA_ROOT/logs/watcher_cron.log" 2>&1
0 16 * * 1-5 cd /path/to/stackAnalys && .venv/bin/python scripts/watch_daily_update.py --db-path "$STOCK_DB_PATH" >> "$STOCK_DATA_ROOT/logs/watcher_cron.log" 2>&1
```

## DuckDB 查询

```python
import duckdb

conn = duckdb.connect("/path/to/stock_local_ai_data/stock_data_v2.duckdb")
df = conn.execute("""
    SELECT ts_code, trade_date, close
    FROM v_daily_price
    WHERE trade_date = '2026-05-29'
    LIMIT 10
""").df()
print(df)
```

查看 manifest 和覆盖范围：

```sql
SELECT * FROM dataset_manifest ORDER BY dataset_name, partition_path;
SELECT * FROM symbol_coverage ORDER BY dataset_name, symbol;
SELECT * FROM unique_key_index LIMIT 20;
```

## AKShare 东方财富接口连接问题说明

当前环境中，AKShare 的部分东方财富后端接口可能出现：

```text
Connection aborted
RemoteDisconnected
```

可能原因：

- Python 3.14 兼容问题：部分第三方库、网络库或数据源库对最新 Python 版本适配不如 3.11 稳定。
- WSL 网络问题：WSL 的 DNS、IPv6、NAT、证书或连接复用在访问部分站点时可能不稳定。
- 代理问题：系统代理环境变量可能影响 requests/curl_cffi 行为；`smoke_test_fetch.py` 已在脚本运行期间临时禁用系统代理。
- 东方财富风控问题：短时间多次访问、异常网络出口或接口变动都可能触发连接关闭。**对同一域名请求间隔必须 ≥1 秒。**

因此正式行情链路拆分为：当日日采以新浪 spot 为主源，历史补全以 BaoStock 为主源；AKShare 腾讯历史源仅作为可选补全源，当前 IP 存在地区/CDN 限制时不要配置为主源。

## 常见问题

- 第一阶段不需要 token：AKShare、BaoStock、RSS 均可直接使用。
- Tushare/NewsAPI：已移到未来扩展，默认不参与采集。
- 非交易日没有数据：脚本会用 `trade_cal` 判断，非交易日跳过行情表。
- 网络失败：接口调用使用 tenacity 重试，多次失败后写日志并抛错。
- WSL 定时任务时区问题：cron 中显式设置 `TZ=Asia/Shanghai`。
- 不想形成单个超大数据库文件：行情数据主存储在按月分区的 Parquet 中，DuckDB 只做元数据、日志和视图入口。
- 已有数据库很大：不会要求删除旧库；新流程会继续写 `lake/`，旧实体表可作为历史兼容数据保留。

## 下一步可扩展

- 复权因子 `adj_factor`
- 财务报表
- 个股资金流
- 龙虎榜
- 分钟线行情
- AKShare 备用数据源完整实现
- 更多新闻源和公告源

## 分析功能使用说明

分析脚本只读取本地 DuckDB 和数据湖视图，不修改采集数据，不重新采集，不生成买入/卖出建议。读取优先级为 `v_daily_price`、`v_index_daily`、`v_stock_basic_latest` 和 `stock_industry_map`；如果行情或基础表视图不可用，会自动 fallback 到旧表 `daily_price`、`index_daily`、`stock_basic`。行业字段优先来自 `stock_industry_map.industry_name`，旧 `stock_basic.industry` 仅作为兼容 fallback。

### 分析结果存储说明

分析结果是数据资产，不写入工程目录。脚本可以在终端打印摘要，但持久化结果必须写入数据根目录下的数据湖：

1. 优先使用 `STOCK_DATA_ROOT`
2. 否则根据 `--db-path` 推断数据根目录
3. 仍无法推断时使用 `~/stock_local_ai_data`

新增分析数据集：

```text
lake/factor_daily/
lake/score_daily/
lake/risk_flag_daily/
lake/market_state_daily/
lake/analysis_universe/
lake/ranking_change_daily/
lake/backtest_result/
lake/backtest_holdings/
lake/stock_report/
lake/daily_analysis_report/
lake/analysis_validation/
lake/news_clean/
lake/news_tag_daily/
lake/news_industry_link/
lake/news_stock_link/
lake/daily_news_summary/
lake/news_factor_daily/
```

对应 DuckDB 视图为 `v_factor_daily`、`v_score_daily`、`v_risk_flag_daily`、`v_market_state_daily`、`v_analysis_universe`、`v_ranking_change_daily`、`v_backtest_result`、`v_backtest_holdings`、`v_stock_report`、`v_daily_analysis_report`、`v_analysis_validation`、`v_news_clean`、`v_news_tag_daily`、`v_news_industry_link`、`v_news_stock_link`、`v_daily_news_summary`、`v_news_factor_daily`。

查询最新股票排名：

```sql
SELECT *
FROM v_score_daily
ORDER BY trade_date DESC, rank ASC
LIMIT 20;
```

查询每日分析报告：

```sql
SELECT report_markdown
FROM v_daily_analysis_report
ORDER BY trade_date DESC
LIMIT 1;
```

查询个股报告：

```sql
SELECT report_markdown
FROM v_stock_report
WHERE ts_code = 'sz.000001'
ORDER BY trade_date DESC
LIMIT 1;
```

查询回测摘要：

```sql
SELECT *
FROM v_backtest_result
ORDER BY created_at DESC
LIMIT 5;
```

运行完整分析流水线：

```bash
python scripts/run_analysis_pipeline.py --db-path "$STOCK_DB_PATH"
python scripts/validate_analysis_outputs.py --db-path "$STOCK_DB_PATH"
python scripts/compare_rank_change.py --db-path "$STOCK_DB_PATH" --top-n 50
```

单股分析：

```bash
python scripts/analyze_stock.py sz.000001 --db-path "$STOCK_DB_PATH"
```

全市场排名：

```bash
python scripts/rank_stocks.py --db-path "$STOCK_DB_PATH" --start 2025-01-01 --top 20
```

市场总览：

```bash
python scripts/market_overview.py --db-path "$STOCK_DB_PATH"
```

因子回测：

```bash
python scripts/backtest_factor.py --db-path "$STOCK_DB_PATH" --start 2025-01-01 --top-n 20
```

生成个股 Markdown 报告：

```bash
python scripts/generate_stock_report.py sz.000001 --db-path "$STOCK_DB_PATH"
```

每日数据更新后，可独立运行每日分析任务：

```bash
python scripts/daily_analysis_job.py --db-path "$STOCK_DB_PATH"
```

### RSS 新闻解释层

新闻解释层只读取本地 `rss_news`，不修改原始新闻表，不调用付费 API，不调用大模型，不生成买入/卖出建议，也不直接并入 `total_score`。所有结果追加写入 `STOCK_DATA_ROOT/lake/` 或由 `--db-path` 推断的数据根目录。

构建全部新闻解释数据集：

```bash
python scripts/build_news_analysis.py --db-path "$STOCK_DB_PATH"
python scripts/build_news_analysis.py --db-path "$STOCK_DB_PATH" --date 2026-06-05
```

校验新闻解释层：

```bash
python scripts/validate_news_analysis.py --db-path "$STOCK_DB_PATH"
```

每日分析入口可选先刷新新闻解释层；如果 `rss_news` 不存在或为空，只打印 WARNING，不影响行情分析：

```bash
python scripts/daily_analysis_job.py --db-path "$STOCK_DB_PATH" --with-news-analysis
python scripts/run_analysis_pipeline.py --db-path "$STOCK_DB_PATH" --with-news-analysis
```

查询最新新闻摘要：

```sql
SELECT *
FROM v_daily_news_summary
ORDER BY trade_date DESC
LIMIT 5;
```

查询行业新闻热度：

```sql
SELECT *
FROM v_news_factor_daily
ORDER BY trade_date DESC, news_heat_score DESC
LIMIT 20;
```

报告文本写入数据湖表，不再作为 Markdown/CSV 文件写入工程目录。

### 可视化评估层使用说明

可视化层只读取本地分析数据和行情视图，不调用网络，不重新采集，不生成买入/卖出建议。图像文件写入：

```text
<data_root>/artifacts/charts/
```

图表元数据和关键指标写入：

```text
lake/visualization_snapshot/
lake/visualization_metric/
```

运行：

```bash
python scripts/build_visual_dashboard.py --db-path "$STOCK_DB_PATH"
python scripts/build_visual_dashboard.py --db-path "$STOCK_DB_PATH" --date 2026-06-01
```

每日分析后也可以在分析层内生成图表：

```bash
python scripts/daily_analysis_job.py --db-path "$STOCK_DB_PATH" --with-visuals
python scripts/run_analysis_pipeline.py --db-path "$STOCK_DB_PATH" --with-visuals
```

查询最新图表：

```sql
SELECT chart_type, chart_title, image_path, created_at
FROM v_visualization_snapshot
ORDER BY trade_date DESC, chart_type;
```

查询策略关键指标：

```sql
SELECT metric_name, group_name, metric_value
FROM v_visualization_metric
WHERE metric_name IN ('strategy_total_return', 'strategy_annual_return', 'strategy_max_drawdown', 'excess_return')
ORDER BY trade_date DESC, metric_name;
```

图表和结构化指标都写入数据根目录，不写入工程目录下的 `data/reports/`、`data/samples/` 或项目根目录。

## 数据库维护脚本体系

维护脚本只维护数据根目录中的 DuckDB 和 Parquet 数据湖，不修改采集接口，不改变每日采集方式，不删除数据库，不删除 Parquet，不重新全量回填。默认以检查和报告为主；任何实际修复都必须显式传参。

路径边界：

```text
PROJECT_ROOT = stock/stackAnalys
DATA_ROOT    = stock/stock_local_ai_data
STOCK_DB_PATH = DATA_ROOT/stock_data_v2.duckdb
```

数据库路径解析优先级：

1. `--db-path`
2. `STOCK_DB_PATH`
3. `STOCK_DATA_ROOT`
4. 自动搜索 `stock_local_ai_data/stock_data_v2.duckdb`、`stock_data.duckdb`、`db/metadata.duckdb`

### 维护数据集

所有维护结果写入 `DATA_ROOT/lake/`：

```text
lake/database_inspection/
lake/database_repair_log/
lake/data_quality_price/
lake/analysis_validation/
lake/backtest_validation/
lake/visualization_validation/
lake/maintenance_log/
```

对应视图：

```sql
SELECT * FROM v_database_inspection;
SELECT * FROM v_database_repair_log;
SELECT * FROM v_data_quality_price;
SELECT * FROM v_analysis_validation;
SELECT * FROM v_backtest_validation;
SELECT * FROM v_visualization_validation;
SELECT * FROM v_maintenance_log;
```

### 脚本说明

全局数据库体检，只读检查并记录结果：

```bash
python scripts/inspect_database_state.py --db-path "$STOCK_DB_PATH"
```

检查内容包括核心视图是否存在、行数、日期范围、股票覆盖、`daily_price` 重复主键和重复行数、`stock_basic` 字段缺失率、回测/可视化是否入湖、是否存在工程目录报告产物。

价格数据校验：

```bash
python scripts/validate_price_data.py --db-path "$STOCK_DB_PATH"
```

检查 `daily_price / v_daily_price` 的重复主键、重复行数、非正价格、`high < low`、收盘价越界、异常收益。当前分析读取层会按 `ts_code + trade_date + adjust_type` 去重，但不会删除原始行情数据。

重复行情修复报告，默认 dry-run：

```bash
python scripts/repair_price_duplicates.py --db-path "$STOCK_DB_PATH"
```

只报告重复情况并写入 `database_repair_log`。如需创建去重视图，必须显式执行：

```bash
python scripts/repair_price_duplicates.py --db-path "$STOCK_DB_PATH" --create-dedup-view --execute
```

这只创建 `v_daily_price_dedup`，不删除原始 `daily_price` 或 Parquet。物理去重必须另行明确确认。

分析结果校验：

```bash
python scripts/validate_analysis_outputs.py --db-path "$STOCK_DB_PATH"
```

校验 `factor_daily`、`score_daily`、`risk_flag_daily`、`market_state_daily`、`daily_analysis_report` 是否存在、行数是否正常、主键是否重复。

新闻解释层校验：

```bash
python scripts/validate_news_analysis.py --db-path "$STOCK_DB_PATH"
```

校验 `news_clean`、`news_tag_daily`、`news_industry_link`、`news_stock_link`、`daily_news_summary`、`news_factor_daily` 行数、覆盖率、摘要建议词、视图和工程目录写入边界。

### LLM 新闻结构化分析层

LLM 新闻层建立在规则新闻层之上。规则层仍作为低成本初筛和 fallback；LLM 失败、未启用或缺少 API Key 时，只输出 WARNING，不影响行情分析，也不修改采集接口。所有 LLM 分析、使用日志和日报都写入 `DATA_ROOT/lake/`，不写入工程目录报告目录。

日报摘要提示词版本为 `daily_news_digest_v2`。系统提示词和用户提示词都要求 Provider 只返回纯 JSON 对象，不带 Markdown 代码块、前缀或后缀文字；客户端解析层也会在 Provider 偶发包裹说明文字或代码块时抽取第一个完整 JSON 对象，降低 `INVALID_JSON` fallback。

推荐由 hermes-agent 在工程外部维护真实 LLM 配置和密钥，避免修改 `stackAnalys/`。在仓库根目录 `stock/` 下可使用：

```bash
cd /path/to/stock/stackAnalys
mkdir -p ../key
cp ../config/llm.example.yaml ../key/stackAnalys_llm.yaml
printf '%s' "实际密钥" > ../key/opencode_go_api_key
chmod 600 ../key/opencode_go_api_key ../key/stackAnalys_llm.yaml
```

`../key/stackAnalys_llm.yaml` 中保留：

```yaml
extraction:
  api_key_file: opencode_go_api_key

digest:
  api_key_file: opencode_go_api_key
```

`api_key_file` 使用相对路径时，相对于这份外部 YAML 所在目录解析。hermes-agent 如果统一使用 `~/key/`，则可以保存为 `~/key/stackAnalys_llm.yaml` 和 `~/key/opencode_go_api_key`，并在 YAML 中写：

```yaml
extraction:
  api_key_file: ~/key/opencode_go_api_key

digest:
  api_key_file: ~/key/opencode_go_api_key
```

脚本通过外部配置运行，例如仓库内 `../key`：

```bash
python scripts/test_llm_provider.py --config ../key/stackAnalys_llm.yaml --profile extraction
```

真实 API Key 不写入 Git。配置支持两种密钥来源：优先读取 `api_key_env` 指定的环境变量；若环境变量不存在，再读取 `api_key_file` 指定的外部文件。OpenCode Go 默认环境变量是：

```bash
export OPENCODE_GO_API_KEY="实际密钥"
```

Provider 测试：

```bash
python scripts/test_llm_provider.py --config ../key/stackAnalys_llm.yaml --profile extraction
```

小批量 dry-run，不调用 Provider：

```bash
python scripts/analyze_news_with_llm.py --db-path "$STOCK_DB_PATH" --config ../key/stackAnalys_llm.yaml --limit 20 --dry-run
```

小批量真实分析：

```bash
python scripts/analyze_news_with_llm.py --db-path "$STOCK_DB_PATH" --config ../key/stackAnalys_llm.yaml --limit 10
```

生成每日 LLM 新闻日报：

```bash
python scripts/generate_daily_news_digest.py --db-path "$STOCK_DB_PATH" --config ../key/stackAnalys_llm.yaml
python scripts/generate_daily_news_digest.py --db-path "$STOCK_DB_PATH" --config ../key/stackAnalys_llm.yaml --date 2026-06-05
```

校验 LLM 新闻层：

```bash
python scripts/test_llm_json_parser.py
python scripts/validate_news_llm_analysis.py --db-path "$STOCK_DB_PATH"
```

分析流水线可选接入，不改变每日采集方式：

```bash
python scripts/daily_analysis_job.py --db-path "$STOCK_DB_PATH" --with-news-analysis --with-llm-news --llm-config ../key/stackAnalys_llm.yaml --llm-limit 20
python scripts/run_analysis_pipeline.py --db-path "$STOCK_DB_PATH" --with-news-analysis --with-llm-news --llm-config ../key/stackAnalys_llm.yaml --llm-limit 20
```

查询结构化结果：

```sql
SELECT *
FROM v_news_llm_analysis
ORDER BY trade_date DESC, importance_score DESC
LIMIT 20;

SELECT *
FROM v_news_fused_analysis
ORDER BY trade_date DESC
LIMIT 20;

SELECT report_markdown
FROM v_daily_news_llm_digest
ORDER BY trade_date DESC
LIMIT 1;

SELECT *
FROM v_llm_usage_log
ORDER BY created_at DESC
LIMIT 50;
```

切换 Provider 或模型：修改外部 `../key/stackAnalys_llm.yaml` 或 `~/key/stackAnalys_llm.yaml` 中 `extraction` / `digest` 的 `provider`、`protocol`、`base_url`、`endpoint`、`api_key_env`、`api_key_file`、`model`。支持 OpenAI-compatible `/chat/completions`，并预留 Anthropic-compatible `/messages` 请求格式。OpenCode Go 的 HTTP API `model` 字段使用实际模型 ID，例如 `deepseek-v4-flash`，不要加 `opencode-go/` 前缀。

禁用 LLM：

```yaml
llm:
  enabled: false
```

禁用后系统继续使用规则版 `daily_news_summary` 和关键词标签，不生成买入、卖出、目标价或保证收益建议。

回测结果校验：

```bash
python scripts/validate_backtest_outputs.py --db-path "$STOCK_DB_PATH"
```

校验 `backtest_result`、`backtest_monthly_returns`、`backtest_holdings`。会检查 `equity_final - 1` 是否等于 `total_return`，回撤曲线最小值是否等于 `max_drawdown`，月收益最大/最小值是否等于 `best_month` / `worst_month`。

可视化结果校验：

```bash
python scripts/validate_visual_outputs.py --db-path "$STOCK_DB_PATH"
```

校验 `visualization_snapshot`、`visualization_metric`、PNG 路径是否存在、WARNING 状态是否可追踪、工程目录下是否错误写入图表或报告。

总控修复脚本，默认 dry-run：

```bash
python scripts/repair_database_state.py --db-path "$STOCK_DB_PATH" --dry-run
```

可选项：

```bash
python scripts/repair_database_state.py --db-path "$STOCK_DB_PATH" --dry-run --fix-views --fix-dedup-view --clean-engine-outputs
python scripts/repair_database_state.py --db-path "$STOCK_DB_PATH" --execute --fix-views
```

`--fix-views` 只刷新 DuckDB 视图到当前 `DATA_ROOT/lake`。`--fix-dedup-view` 优先生成去重视图，不删除原始行情。`--clean-engine-outputs` 当前只报告旧产物，不删除文件。

每日维护入口：

```bash
python scripts/daily_maintenance_job.py --db-path "$STOCK_DB_PATH"
python scripts/daily_maintenance_job.py --db-path "$STOCK_DB_PATH" --with-backtest
```

默认串行执行：

1. `inspect_database_state.py`
2. `validate_price_data.py`
3. `run_analysis_pipeline.py`
4. `validate_analysis_outputs.py`
5. `build_visual_dashboard.py`
6. `validate_visual_outputs.py`

加 `--with-backtest` 后会额外运行回测和回测校验。维护流水线结果写入 `lake/maintenance_log/`。

### 可视化维护规则

- `industry` 是行业分类，如银行、半导体、电力、医药、通信设备；新逻辑来自 `stock_industry_map.industry_name`，不要把行业源写回 `stock_basic`。
- `market` 是市场/板块字段，如主板、创业板、科创板、北交所，不能替代 `industry`。
- 当 `industry` 缺失率过高时，行业分布图必须跳过并在 `visualization_snapshot` 写 WARNING。
- `hs300_trend` 依赖 `index_daily` 历史数据；若指数历史不足，不能画 MA20/MA60，也不能画假基准线，必须写 WARNING。
- `score_decile_forward_return` 使用历史 `score_daily.total_score` 分组，并观察未来收益；若没有足够未来价格数据，必须写 WARNING。

### Git 提交注意

当前 Git 仓库根目录是 `stock/`，工程代码在 `stock/stackAnalys/`，数据在 `stock/stock_local_ai_data/`。提交代码时只加入工程目录：

```bash
git add stackAnalys/README.md stackAnalys/scripts stackAnalys/src
```

不要把 `stock_local_ai_data/` 的 DuckDB、Parquet、图表或维护日志加入代码提交，除非明确要同步数据资产。

## 每日观察池与 30 日跟踪

推荐系统只生成算法观察池、研究信号和影子模拟跟踪，不接入实盘，不改变每日行情采集入口。Codex 维护 `stackAnalys/` 内的代码、脚本、配置模板和 schema；Hermes 维护真实 DuckDB、Parquet 数据湖、外部配置、API Key、虚拟环境、定时任务和生产运行。

真实推荐配置独立于 LLM 配置，模板为 `config/recommendation.example.yaml`。生产配置由 Hermes 管理，建议放在 `~/key/recommendation.yaml` 或工程外路径，并通过 `--config` 指定；Codex 不直接修改仓库根 `config/settings.yaml` 里的真实运行配置。参数变化必须升级 `recommendation.recommendation_version`，历史版本结果不会被覆盖。

推荐数量配置：

```yaml
recommendation:
  top_n: 10
```

`recommendation.top_n` 默认值为 10，可由外部真实配置覆盖；非法、非整数或小于 1 会直接报错。批次表同时保存 `target_top_n`、`selected_count` 和 `fill_ratio = selected_count / target_top_n`。风险、流动性、停牌、新股、行业集中度等硬过滤后不足 10 只时，不会为凑数放宽硬标准；报告和批次会记录实际数量、短缺原因，`fill_ratio < 0.8` 时标记 WARNING。`universe.max_industry_count` 表示每个行业最多入选数量，不是总推荐数上限。

推荐链路：

```text
行情/行业/因子/评分/风险/新闻
-> recommendation_universe
-> recommendation_batch + recommendation_item
-> recommendation_tracking_daily
-> recommendation_item_result + recommendation_batch_result
-> recommendation_live_portfolio_daily
-> recommendation_rolling_return_daily
-> recommendation_evaluation_daily
-> recommendation_stability_daily + recommendation_list_change_detail
```

默认在 `signal_date` 收盘后生成观察池，下一有效交易日按 `next_open` 模拟建仓，跟踪 30 个实际交易日。新闻和 LLM 只作为辅助解释，不直接增加、删除或重排股票名单；推荐解释禁止输出必涨、保证收益、确定买入、满仓、梭哈和目标价保证等表述。

运行命令：

```bash
export STOCK_DB_PATH=~/stock/stock_local_ai_data/stock_data_v2.duckdb
export STOCK_DATA_ROOT=~/stock/stock_local_ai_data
export RECOMMENDATION_CONFIG=~/key/recommendation.yaml

python scripts/generate_stock_recommendations.py --db-path "$STOCK_DB_PATH" --config "$RECOMMENDATION_CONFIG" --dry-run
python scripts/generate_stock_recommendations.py --db-path "$STOCK_DB_PATH" --config "$RECOMMENDATION_CONFIG"
python scripts/backfill_recommendation_history.py --db-path "$STOCK_DB_PATH" --config "$RECOMMENDATION_CONFIG" --start 2025-01-01 --end 2026-04-30
python scripts/update_recommendation_tracking.py --db-path "$STOCK_DB_PATH" --config "$RECOMMENDATION_CONFIG"
python scripts/finalize_recommendation_results.py --db-path "$STOCK_DB_PATH" --config "$RECOMMENDATION_CONFIG"
python scripts/finalize_recommendation_results.py --db-path "$STOCK_DB_PATH" --config "$RECOMMENDATION_CONFIG" --recompute-costs --start 2026-04-21 --end 2026-06-05 --dry-run
python scripts/evaluate_recommendation_system.py --db-path "$STOCK_DB_PATH" --config "$RECOMMENDATION_CONFIG"
python scripts/evaluate_recommendation_stability.py --db-path "$STOCK_DB_PATH" --config "$RECOMMENDATION_CONFIG"
python scripts/generate_recommendation_charts.py --db-path "$STOCK_DB_PATH"
python scripts/validate_recommendation_system.py --db-path "$STOCK_DB_PATH"
python scripts/show_latest_recommendations.py --db-path "$STOCK_DB_PATH"
```

`scripts/daily_analysis_job.py` 默认在每日分析后接入推荐评测：更新未完成跟踪、完成到期结果、更新滚动影子组合、计算滚动月度/年度收益、计算名单变动、更新评估汇总并写入图表元数据。推荐评测失败只输出 WARNING，不阻塞行情采集或基础分析；可用 `--skip-recommendation-evaluation` 临时跳过。

所有推荐结果写入 `DATA_ROOT/lake/`，对应视图：

```text
v_recommendation_batch
v_recommendation_item
v_recommendation_universe
v_recommendation_tracking_daily
v_recommendation_item_result
v_recommendation_batch_result
v_recommendation_live_portfolio_daily
v_recommendation_rolling_return_daily
v_recommendation_evaluation_daily
v_recommendation_explanation
v_recommendation_stability_daily
v_recommendation_list_change_detail
```

推荐 PNG 图表写入 `DATA_ROOT/artifacts/charts/recommendation/`，图表元数据写入 `v_visualization_snapshot` 和 `v_visualization_metric`。当前图表覆盖批次 30 日组合收益、个股 30 日收益分布、滚动模拟账户净值、滚动月度收益、滚动年度收益、名单变动、正收益命中率、TopN 重叠率、换手率、新进/退出数量、连续持榜天数分布，以及 `raw_top_n` / `confirmed_top_n` / `low_turnover_top_n` 策略对比；数据不足时写 WARNING 快照，不画假 0 值或假基准线。

交易成本口径：

- `position_notional` 是单只股票模拟建仓名义金额，优先读取 `recommendation.portfolio.position_notional`。
- 若未配置 `position_notional`，可由 `batch_capital * weight` 推导；仍缺失时默认 10000。
- 买入佣金和卖出佣金各扣一次，均使用 `max(notional * commission_rate, minimum_commission)`。
- 印花税只在卖出时扣一次，滑点按买入和卖出各一次扣除。
- `recommendation_item_result` 保存 `position_notional`、`buy_commission`、`sell_commission`、`buy_cost_rate`、`sell_cost_rate`、`stamp_duty_cost_rate`、`slippage_cost_rate`、`total_cost_rate` 和 `cost_warning`，用于审计。

结果状态口径：

- `PARTIAL` 表示仍在 30 个实际交易日跟踪中，使用 `current_gross_return` / `current_net_return` 查看当前表现。
- `COMPLETED` 表示 `actual_trading_days >= 30`，才写入正式 `gross_return_30d` / `net_return_30d` 和 `final_*_30d`。
- 正式 30D 个股收益分布和批次 30D 组合收益图只统计 `COMPLETED`；`PARTIAL` 单独使用 Current Return 图表。

批次收益口径：

- `weighted_gross_return` / `weighted_net_return` 使用 `sum(weight * return)`，权重和不为 1 时归一化。
- `simple_mean_gross_return` / `simple_mean_net_return` 保留简单平均，便于和旧口径对照。
- 图表和长期评估默认使用 weighted 指标。

滚动收益口径：

- `rolling_monthly_return` 是滚动影子组合最近 20 个有效交易日净值收益率：`equity_t / equity_(t-20) - 1`。
- `rolling_annual_return` 是滚动影子组合最近 252 个有效交易日实际区间收益率：`equity_t / equity_(t-252) - 1`。
- 月度和年度均按 `run_mode + recommendation_version + selector_name` 分组计算，不把 `backfill` 与 `live_shadow` 拼成同一条净值曲线。
- 正式月度/年度收益使用净收益净值；字段同时保存 gross、net、benchmark、excess、窗口起止日期、observation_count 和状态。
- 不足 20 或 252 个有效交易日时，正式收益为 NULL，状态为 `INSUFFICIENT_HISTORY`；报告显示 `N/A（当前仅有 X 个有效交易日，需要252个）`，不显示 0。
- `annualized_return_to_date` 是不足 252 日时的辅助区间年化估算，最低需要 60 个有效交易日；它不是正式年度收益。

历史推荐回填：

```bash
python scripts/backfill_recommendation_history.py \
  --db-path "$STOCK_DB_PATH" \
  --config "$RECOMMENDATION_CONFIG" \
  --start 2025-01-01 \
  --end 2026-04-30
```

回填对每个历史交易日按配置中的 `selector_names` 生成批次，默认包含 `raw_top_n`、`confirmed_top_n`、`low_turnover_top_n`。回填批次写 `run_mode=backfill`；实时影子跟踪写 `run_mode=live_shadow`。`score_daily` 缺失时使用 `signal_date` 及之前的历史行情回算规则评分，禁止未来信息泄漏；不会修改原始行情。默认跳过已存在的 `signal_date + recommendation_version`，使用 `--force` 可重算同版本历史结果。

稳定 selector：

- `raw_top_n`：直接取当日综合分 Top N。
- `confirmed_top_n`：连续 2 天进入 Top15 才正式推荐；风险变为 HIGH 立即退出；连续 2 天跌出 Top30 退出。
- `low_turnover_top_n`：优先保留已有推荐，只有候选分数高于当前最低持仓 `replacement_score_gap` 时替换。

稳定性评估写入 `v_recommendation_stability_daily`，名单变动明细写入 `v_recommendation_list_change_detail`，并输出收益、风险、稳定性三维策略对比，包括 30 日平均/中位收益、正收益率、跑赢基准率、最大回撤、换手、交易成本、重叠率、连续持榜天数和行业 HHI。

稳定性指标口径：

- `overlap_ratio_current = overlap_count / selected_count_current`
- `jaccard_overlap = overlap_count / union(previous, current)`
- `new_entry_ratio = new_entry_count / selected_count_current`
- `dropout_ratio = dropout_count / selected_count_previous`
- `weight_turnover_1d = 0.5 * sum(abs(w_current - w_previous))`
- 兼容字段 `turnover_1d` 等于 `new_entry_ratio`。
- 首个可比较日期 `is_first_observation=true`，重叠率、Jaccard、新增率、退出率和权重换手均为 NULL，不参与均值统计。
- 名单变动按相同 `recommendation_version + selector_name + run_mode` 的当前有效推荐日与上一有效推荐日比较。明细显示股票代码、名称、当前/上一排名、`final_score`、行业、`NEW_ENTRY` / `DROPOUT` / `RETAINED` 和变动原因。

常用查询：

```sql
-- 最新推荐批次
SELECT *
FROM v_recommendation_batch
ORDER BY signal_date DESC, created_at DESC
LIMIT 1;

-- 最新推荐明细
SELECT i.*
FROM v_recommendation_item AS i
JOIN (
  SELECT batch_id
  FROM v_recommendation_batch
  ORDER BY signal_date DESC, created_at DESC
  LIMIT 1
) AS b USING(batch_id)
ORDER BY rank;

-- 推荐中的单只股票
SELECT *
FROM v_recommendation_item
WHERE ts_code = 'sh.600000'
ORDER BY signal_date DESC;

-- 30 日个股结果
SELECT *
FROM v_recommendation_item_result
ORDER BY signal_date DESC, net_return_30d DESC;

-- 批次组合结果
SELECT *
FROM v_recommendation_batch_result
ORDER BY signal_date DESC;

-- 滚动模拟账户
SELECT *
FROM v_recommendation_live_portfolio_daily
ORDER BY trading_date DESC
LIMIT 60;

-- 最新滚动月度/年度收益
SELECT trade_date, run_mode, recommendation_version, selector_name,
       rolling_monthly_net_return, rolling_monthly_benchmark_return, rolling_monthly_excess_return,
       rolling_annual_net_return, rolling_annual_benchmark_return, rolling_annual_excess_return,
       annualized_return_to_date, monthly_observation_count, annual_observation_count,
       monthly_return_status, annual_return_status
FROM v_recommendation_rolling_return_daily
ORDER BY trade_date DESC, run_mode, recommendation_version, selector_name
LIMIT 20;

-- 长期推荐评估
SELECT *
FROM v_recommendation_evaluation_daily
ORDER BY evaluation_date DESC;

-- 推荐稳定性
SELECT *
FROM v_recommendation_stability_daily
ORDER BY trade_date DESC, run_mode, recommendation_version, selector_name;

-- 最新名单变动明细
SELECT *
FROM v_recommendation_list_change_detail
ORDER BY trade_date DESC, run_mode, recommendation_version, selector_name, change_type, current_rank NULLS LAST;
```

推荐模块失败不得阻塞行情采集。每日分析完成后，Hermes 可按顺序可选执行生成、跟踪、结算、评估和校验脚本；如数据库缺少核心行情或评分视图，脚本会输出 `NO_INPUT` / `NO_PRICE_DATA` / `NO_TRACKING_DATA` 等状态，不伪造结果。

---

## 变更记录

### 2026-06-08

#### 修复：`_next_day()` / `_previous_day()` 跳过非交易日

- 原 `_next_day(date)` 返回日历次日，周五→周六→BaoStock 返回空→触发 EastMoney fallback→EastMoney 挂死→全市场 5500 只串行失败
- 改为 `while weekday() >= 5` 跳过周末（纯 CPU，无网络调用）
- 改动：`src/jobs/daily_update_job.py`

#### 修复：`is_trade_day()` Sina API 无 timeout

- `ak.tool_trade_date_hist_sina()` 内部 `requests.get(url)` 无 timeout，网络断开时 SSL 握手无限卡死
- 改为：先用 weekday 快速判断（99% 场景），再用 ThreadPoolExecutor + 5s timeout 包裹 Sina 请求
- 超时或异常 → 默认工作日为交易日（节假日误判无害，BaoStock 返回空即可跳过）
- 改动：`src/utils/calendar.py`

#### 新增：`daily_run.sh` 入口交易日检查 + 分支

- 每次运行先调用 `is_trade_day()` 判断当日是否开盘（联网检查，15s timeout）
- 交易日 → 主管线（采集 + 分析 + 同步 + 邮件）
- 非交易日 → 周末分支（`weekend_rss_news.sh` + `daily_news_analysis.sh` → 退出）
- crontab 改为每日 `30 18 * * *` 运行，不再依赖 `1-5` 限制
- 改动：`scripts/daily_run.sh`、crontab

#### EastMoney 全面不可达状态

- `push2his.eastmoney.com`、`push2.eastmoney.com`、新浪部分 API 均无法连接（SSL 连接中断）
- `industry_board` step 失败（依赖 EastMoney）
- `sina_spot_daily` step 返回 0 行
- 主数据流（BaoStock 日线 + RSS 新闻）正常工作
- 已提交 ISSUES.md 给 codex：建议实现 fallback 断路器和重试降级
