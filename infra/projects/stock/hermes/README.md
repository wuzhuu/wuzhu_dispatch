# Portable Stock Hermes gateway

This directory contains the non-secret configuration contract for the
project-local Hermes Email gateway bundled with Stock. It is intentionally
separate from the Stock scheduler configuration.

## Effective routing

- Main model: `gpt-5.6-luna`
- Main provider: `trankey`
- Main API: `codex_responses`
- Main endpoint: `https://transfer.31411414.xyz/v1`
- Vision helper: model `naive` through the `trankey` provider
- Hermes sender/login: `stock-ops@31411414.xyz`
- Default reply target: `mail@31411414.xyz`
- Allowed inbound senders: `1521045234@qq.com`, `mail@31411414.xyz`

## Provisioning

1. Copy `config.yaml.example` to the target's persistent Hermes home:
   `/opt/hermes-stock/runtime/hermes-home/config.yaml`.
2. Copy `email.env.example` to:
   `/opt/hermes-stock/runtime/hermes/email.env`.
3. Replace only the `secret://...` placeholders through the target secret
   provisioning path. Do not put passwords or API keys in Git.
4. Set the environment file mode to `0600`.
5. Start/recreate `hermes-stock-gateway` through the host wrapper, never by
   changing the image or by running a second mailbox consumer.

## Required verification

After provisioning, verify the target runtime without printing secrets:

```bash
docker inspect hermes-stock-gateway \
  --format 'image={{.Config.Image}} state={{.State.Status}} health={{.State.Health.Status}} restart={{.RestartCount}}'
docker logs --since 5m hermes-stock-gateway 2>&1 \
  | grep -E 'Gateway running|Email|IMAP|SMTP|error|Error|ERROR' \
  | tail -80
```

Then perform a real IMAP login, SMTP authentication, and one controlled
message to `mail@31411414.xyz`; check the mailbox readback. A healthy Docker
status alone is not sufficient evidence.

## Portability boundary

Portable project contents include source, image/build descriptors, lifecycle
hooks, and these redacted configuration templates. They do **not** include
mailbox passwords, API keys, OAuth state, runtime sessions, logs, DuckDB,
parquet/lake data, reports, or NAS lease state. Those are provisioned or
restored separately on the target.
