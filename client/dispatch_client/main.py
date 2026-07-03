"""dispatch-client: command-line interface for wuzhu-dispatch.

Configuration priority:
  1. `-c/--config <yaml_file>` — YAML with `dispatcher_url` and `client_token`
  2. Environment variables DISPATCH_URL + DISPATCH_CLIENT_TOKEN

Uses the client API (/api/v1/client/*, /api/v1/admin/*).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click
import yaml

from .client import DispatchClient


def _load_config(config_path: str | None = None) -> dict:
    """Load client config from YAML or env vars."""
    if config_path:
        path = Path(config_path).expanduser().resolve()
        if not path.exists():
            click.echo(f"Error: config file not found: {path}", err=True)
            sys.exit(1)
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        return {
            "url": cfg.get("dispatcher_url", ""),
            "token": cfg.get("client_token", ""),
        }

    return {
        "url": os.environ.get("DISPATCH_URL", ""),
        "token": os.environ.get("DISPATCH_CLIENT_TOKEN", ""),
    }


def _get_client(config_path: str | None = None) -> DispatchClient:
    cfg = _load_config(config_path)
    url = cfg["url"]
    token = cfg["token"]

    if not url:
        click.echo("Error: dispatcher_url not set. Use -c/--config or DISPATCH_URL env var.", err=True)
        sys.exit(1)
    if not token:
        click.echo("Error: client_token not set. Use -c/--config or DISPATCH_CLIENT_TOKEN env var.", err=True)
        sys.exit(1)

    return DispatchClient(url, token)


@click.group()
@click.option("-c", "--config", "config_path", default=None,
              help="Path to client YAML config file")
@click.pass_context
def cli(ctx, config_path):
    """wuzhu-dispatch client CLI."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


# ── Task commands ──────────────────────────────────────────────────


@cli.group()
def task():
    """Manage tasks."""
    pass


@task.command("create")
@click.option("--type", "-t", "task_type", required=True, help="Task type identifier")
@click.option("--payload", "-p", "payload_file", type=click.Path(exists=True),
              help="JSON file with task payload and requirements")
@click.option("--priority", default=50, type=int, help="Task priority (0-100)")
@click.option("--timeout", default=3600, type=int, help="Timeout in seconds")
@click.option("--max-retries", default=3, type=int, help="Max retry count")
@click.option("--data", "-d", "inline_payload", help="Inline JSON payload as string")
@click.pass_context
def create_task(ctx, task_type, payload_file, priority, timeout, max_retries, inline_payload):
    """Create a new task."""
    client = _get_client(ctx.obj["config_path"])

    if payload_file:
        with open(payload_file) as f:
            data = json.load(f)
    elif inline_payload:
        data = json.loads(inline_payload)
    else:
        data = {}

    data.setdefault("type", task_type)
    data.setdefault("priority", priority)
    data.setdefault("timeout_seconds", timeout)
    data.setdefault("max_retries", max_retries)

    result = client.create_task(data)
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


@task.command("list")
@click.option("--status", "-s", default="",
              help="Filter by status (pending/running/success/failed)")
@click.pass_context
def list_tasks(ctx, status):
    """List tasks."""
    client = _get_client(ctx.obj["config_path"])
    tasks = client.list_tasks(status)
    if not tasks:
        click.echo("No tasks found.")
        return
    for t in tasks:
        click.echo(
            f"{t['task_id']:36s}  {t['type']:24s}  {t['status']:10s}  "
            f"priority={t['priority']:3d}  node={t['assigned_node_id'] or '-':16s}  "
            f"retry={t['retry_count']}/{t['max_retries']}"
        )


@task.command("show")
@click.argument("task_id")
@click.pass_context
def show_task(ctx, task_id):
    """Show task details."""
    client = _get_client(ctx.obj["config_path"])
    task = client.get_task(task_id)
    click.echo(json.dumps(task, indent=2, ensure_ascii=False))


@task.command("cancel")
@click.argument("task_id")
@click.pass_context
def cancel_task(ctx, task_id):
    """Cancel a task."""
    client = _get_client(ctx.obj["config_path"])
    result = client.cancel_task(task_id)
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


@task.command("retry")
@click.argument("task_id")
@click.pass_context
def retry_task(ctx, task_id):
    """Retry a failed/cancelled task."""
    client = _get_client(ctx.obj["config_path"])
    result = client.retry_task(task_id)
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


@task.command("logs")
@click.argument("task_id")
@click.pass_context
def task_logs(ctx, task_id):
    """Show task logs."""
    client = _get_client(ctx.obj["config_path"])
    logs = client.get_task_logs(task_id)
    for entry in logs:
        click.echo(f"[{entry['log_time']}] {entry['level']:6s} {entry['message']}")


# ── Node commands ──────────────────────────────────────────────────


@cli.group()
def node():
    """Manage compute nodes."""
    pass


@node.command("list")
@click.pass_context
def list_nodes(ctx):
    """List registered compute nodes."""
    client = _get_client(ctx.obj["config_path"])
    nodes = client.list_nodes()
    if not nodes:
        click.echo("No nodes registered.")
        return
    for n in nodes:
        tags = ",".join(n.get("tags", [])[:3])
        click.echo(
            f"{n['node_id']:24s}  {n.get('name', '-'):24s}  "
            f"enabled={n['enabled']}  region={n.get('region', '-'):8s}  "
            f"tags=[{tags}]"
        )


if __name__ == "__main__":
    cli()
