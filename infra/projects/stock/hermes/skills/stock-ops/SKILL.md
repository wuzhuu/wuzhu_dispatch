---
name: stock-ops
description: "Operate and recover the containerized Stock production scheduler through the authenticated host operations broker."
version: 1.2.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [stock, operations, scheduler, recovery, backfill, backup, sync]
---

# Stock Operations

This skill is for the `stock-ops` Hermes gateway. It provides real operational
capability through the host-owned fixed-action broker; it is not a substitute
for the broker and must never bypass it.

## Mandatory boundary

- Never run `docker`, `systemctl`, `mount`, `ssh`, `rclone`, or arbitrary host
  shell commands directly from the gateway.
- Never request or use `/var/run/docker.sock`.
- Never edit the role marker, credentials, database, or host configuration
  directly.
- Use only `/opt/hermes-stock/ops/client.py` and the operations listed below.
- Report the returned `success`, `stage`, and verification status truthfully;
  do not call a logged warning a successful repair.

## Broker client

The client is mounted at `/opt/hermes-stock/ops/client.py` and communicates
with `/run/stock-ops/broker.sock`.

```bash
python /opt/hermes-stock/ops/client.py status
python /opt/hermes-stock/ops/client.py monitor
```

The client returns JSON and exits non-zero when the broker rejects a request or
when an operation is not verified successful.

## Approved operations

- `status`: inspect Docker containers, scheduler health, database presence,
  NAS writability, and current role.
- `monitor`: perform a health observation and send a transition-deduplicated
  alert if the overall state changes.
- `repair`: bounded repair: attempt the NAS mount, verify the production
  database, restart the scheduler only when needed, then verify again.
- `restart_scheduler`: restart and verify `hermes-stock-scheduler`.
- `restart_gateway`: recreate and verify `hermes-stock-gateway` through the
  fixed host wrapper.
- `backfill YYYY-MM-DD`: start one real `run_daily.py` backfill under the NAS
  singleton lease. If a production job is already active, do not start a
  second one; return its process evidence instead.
- `switch_node NAT88` or `switch_node HK299`: invoke the fixed, audited
  primary/standby cutover workflow. Do not invent node names.
- `backup`: run the host Restic backup unit synchronously and inspect its
  returned result.
- `sync`: run the fixed Stock data sync and require a zero exit status.

## Backfill workflow

1. Run `status` and confirm the active role is NAT88 primary or a verified
   future primary; confirm no conflicting backfill is running.
2. Run `backfill YYYY-MM-DD` only when data is missing and there is no active
   production job.
3. Poll `status`; do not start a second process because the first one is slow.
4. After completion, run `status` and `sync`. Report database evidence and any
   remaining blocker. Never call an accepted background job completed.

## Failure workflow

1. Run `monitor` and capture its JSON result.
2. If unhealthy, run `repair` once.
3. Run `monitor` again.
4. If still unhealthy, send a concise failure explanation containing the
   failed stage and returned evidence. Do not retry indefinitely and do not
   run data collection against an unverified database path.
5. If the broker socket is unavailable, report `broker_unreachable`; this is a
   host deployment failure, not permission to use a fallback shell command.

## Data-path contract

The canonical production DuckDB is:

- Host/broker path: `/opt/hermes-stock/runtime/data/stock_data_v2.duckdb`.
- Container/scheduler path: `/opt/hermes-stock/app/stock_local_ai_data/stock_data_v2.duckdb`.

These are the same file through the scheduler's bind mount. A process command
using the container path is therefore not evidence of a local-old database.
Use broker `status` for the canonical host path and compare database evidence
(existence, inode/size/mtime when available, and target-date rows) before
reporting a path mismatch. Never invent `/var/lib/stock/data` or
`/opt/hermes-stock/data` as the production path.

## Reporting contract

For any repair, restart, switch, backup, or sync request, return:

- requested operation;
- result and exit status;
- verification result;
- remaining blocker, if any;
- whether an alert was emitted.

Health checks with no state change are silent. A first unhealthy observation,
a transition to failure, and a transition back to healthy are alert-worthy.
