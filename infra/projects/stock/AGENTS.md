# Stock project maintenance guide for the bundled Hermes gateway

## Runtime layout

- Project code (writable): `/opt/hermes-stock/app`
- Host persistence for that code: `/opt/hermes-stock/runtime/code`
- Read-only runtime secrets: `/opt/hermes-stock/key`
- Read-only config: `/opt/hermes-stock/app/config`
- Production data is owned by the scheduler; do not edit DuckDB or parquet files unless the user explicitly requests data repair.

The gateway is the project-local Hermes container `hermes-stock-gateway`. It is bundled with Stock and has project-code write access only. It has no Docker socket and no host SSH access.

## Safe change procedure

1. Work only under `/opt/hermes-stock/app`.
2. Before editing, run bounded checks:
   - `git -C /opt/hermes-stock/app status --short -- <target>`
   - `git -C /opt/hermes-stock/app diff -- <target>`
3. Use the smallest targeted edit. Never run `find /` or recursive `grep` across `/nas/stock`; use an explicit file/path and `timeout 30s` for diagnostics.
4. Run the narrowest relevant syntax/test check with a timeout.
5. Re-read the changed lines and verify the intended behavior.
6. Commit only the intended source files with a descriptive message. Do not add `stock_local_ai_data`, reports, secrets, OAuth state, passwords, or private keys.
7. Push source changes to the configured Git remote when credentials are available. The scheduler pulls the repository on its normal update cycle; do not restart Docker from inside the gateway.

## Email/report recipients

There are two separate mail paths and they must not be confused:

### Native Hermes Email gateway

- Container: `hermes-stock-gateway`
- Sender/login: `stock-ops@31411414.xyz`
- IMAP/SMTP host: `mail.31411414.xyz`
- Default reply/home recipient: `mail@31411414.xyz`
- Allowed inbound senders: `1521045234@qq.com,mail@31411414.xyz`
- Runtime environment file on the host: `/opt/hermes-stock/runtime/hermes/email.env` (mode 0600; never print or commit it)
- Hermes config: `/opt/hermes-stock/runtime/hermes-home/config.yaml`
- Portable templates: `hermes/config.yaml.example` and `hermes/email.env.example`.
- The portable model contract is `trankey` + `codex_responses` with main model
  `gpt-5.6-luna` and vision helper `naive`; credentials are runtime secrets.
- The native gateway is the only mailbox consumer.

### Stock reports

- SMTP credentials and report `to_addr` are in `/opt/hermes-stock/key/email_smtp.yaml`; this file is read-only to Hermes and must never be printed or modified.
- Daily report code: `scripts/send_report_email.py`.
- Morning news report code: `scripts/send_morning_reports.sh`.
- Health alert recipient: `stackAnalys/scripts/daily_health_check.py`.
- Reports currently go to `mail@31411414.xyz`. Do not restore the QQ address unless the user explicitly asks.

Do not change the native gateway sender or password from inside an email maintenance request. If the mailbox credential must be rotated, do it on the mail host/control plane, update the host runtime secret, recreate the gateway container, then verify IMAP and SMTP authentication.

## Operational boundaries

- The native Hermes Email gateway is the only mailbox consumer. Do not recreate a legacy mailbox poller or a second email processor.
- Do not change Docker images, container mounts, credentials, role markers, NAS leases, or production scheduler state from an email request.
- For long operations, send an acknowledgement first and use a bounded/asynchronous task. Never make an email response wait on a full NAS scan, data pipeline, or report generation.
