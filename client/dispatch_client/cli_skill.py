"""dispatch-skill: runtime task submission CLI for wuzhu-dispatch.

Commands:
  quick     Submit a template task and wait for result
  submit    Submit a template task (async, returns task_id)
  status    Check task status
  logs      Get task logs
  result    Get task result
  cancel    Cancel a pending/running task
"""

from __future__ import annotations

import json
import sys

import click
import yaml

from .runtime_skill import DispatchRuntimeSkill
from .skill_config import SkillConfig


@click.group()
@click.option("--config", "-c", "config_path", default=None,
              help="Path to skill.yaml config file")
@click.option("--dispatcher-url", "-d", default=None,
              help="Dispatcher URL (overrides config)")
@click.option("--client-token", "-t", default=None,
              help="Client API token (overrides config)")
@click.pass_context
def skill_cli(ctx, config_path, dispatcher_url, client_token):
    """wuzhu-dispatch Runtime Skill — call the compute network."""
    ctx.ensure_object(dict)

    # Load from config file or env
    if config_path:
        skill_cfg = SkillConfig.from_file(config_path)
    else:
        skill_cfg = SkillConfig.from_file()

    # Override from CLI flags
    if dispatcher_url:
        skill_cfg.dispatcher_url = dispatcher_url
    if client_token:
        skill_cfg.client_token = client_token

    if not skill_cfg.dispatcher_url:
        click.echo("Error: dispatcher_url not set. Use --dispatcher-url, "
                   "skill.yaml, or DISPATCH_URL env var.", err=True)
        sys.exit(1)
    if not skill_cfg.client_token:
        click.echo("Error: client_token not set. Use --client-token, "
                   "skill.yaml, or DISPATCH_CLIENT_TOKEN env var.", err=True)
        sys.exit(1)

    ctx.obj["skill_cfg"] = skill_cfg


def _get_skill(ctx) -> DispatchRuntimeSkill:
    cfg = ctx.obj["skill_cfg"]
    return DispatchRuntimeSkill.from_config(cfg)


def _resolve_target(target_mode: str | None, tags: tuple[str, ...],
                    node_id: str | None, profile: str | None,
                    ctx) -> tuple[dict, str | None]:
    """Build target dict from CLI options, merging with profile."""
    target: dict = {"mode": target_mode or "auto"}
    if tags:
        target["tags"] = list(tags)
    if node_id:
        target["mode"] = "node"
        target["node_id"] = node_id

    cfg: SkillConfig = ctx.obj["skill_cfg"]
    merged = cfg.merge_target_with_profile(target, profile)
    # Determine effective profile (for dry-run display)
    eff_profile = profile if profile else None
    return merged, eff_profile


@skill_cli.command()
@click.argument("template_id")
@click.option("--param", "-p", "params", multiple=True, nargs=2,
              metavar="KEY VALUE", help="Template parameter (can be repeated)")
@click.option("--url", help="Shorthand for --param url VALUE")
@click.option("--domain", help="Shorthand for --param domain VALUE")
@click.option("--host", help="Shorthand for --param host VALUE")
@click.option("--tag", "tags", multiple=True, help="Target tag (can be repeated)")
@click.option("--node", "node_id", default=None, help="Target specific node")
@click.option("--profile", default=None, help="Named profile from skill.yaml")
@click.option("--wait", "wait_seconds", default=None, type=int,
              help="Seconds to wait for completion")
@click.option("--priority", default=50, type=int, help="Task priority (0-100)")
@click.option("--timeout", "timeout_seconds", default=300, type=int,
              help="Execution timeout in seconds")
@click.option("--dry-run", is_flag=True, help="Print request without sending")
@click.pass_context
def quick(ctx, template_id, params, url, domain, host, tags,
          node_id, profile, wait_seconds, priority, timeout_seconds, dry_run):
    """Submit a template task and wait for the result."""
    skill = _get_skill(ctx)

    # Build params dict
    template_params = dict(params)
    if url:
        template_params["url"] = url
    if domain:
        template_params["domain"] = domain
    if host:
        template_params["host"] = host

    target, eff_profile = _resolve_target("auto", tags, node_id, profile, ctx)

    if dry_run:
        click.echo(json.dumps({
            "template_id": template_id,
            "params": template_params,
            "target": target,
            "profile": eff_profile,
            "wait_seconds": wait_seconds,
            "priority": priority,
            "timeout_seconds": timeout_seconds,
        }, indent=2, ensure_ascii=False))
        return

    result = skill.quick(
        template_id=template_id,
        params=template_params,
        target=target,
        wait_seconds=wait_seconds,
        priority=priority,
        timeout_seconds=timeout_seconds,
    )

    _print_skill_result(result)


@skill_cli.command()
@click.argument("template_id")
@click.option("--param", "-p", "params", multiple=True, nargs=2,
              metavar="KEY VALUE", help="Template parameter")
@click.option("--url", help="Shorthand for --param url VALUE")
@click.option("--domain", help="Shorthand for --param domain VALUE")
@click.option("--host", help="Shorthand for --param host VALUE")
@click.option("--tag", "tags", multiple=True, help="Target tag")
@click.option("--node", "node_id", default=None, help="Target specific node")
@click.option("--profile", default=None, help="Named profile from skill.yaml")
@click.option("--priority", default=50, type=int, help="Task priority")
@click.option("--timeout", "timeout_seconds", default=300, type=int,
              help="Execution timeout")
@click.option("--dry-run", is_flag=True, help="Print request without sending")
@click.pass_context
def submit(ctx, template_id, params, url, domain, host, tags,
           node_id, profile, priority, timeout_seconds, dry_run):
    """Submit a template task without waiting (async)."""
    skill = _get_skill(ctx)

    template_params = dict(params)
    if url:
        template_params["url"] = url
    if domain:
        template_params["domain"] = domain
    if host:
        template_params["host"] = host

    target, eff_profile = _resolve_target("auto", tags, node_id, profile, ctx)

    if dry_run:
        click.echo(json.dumps({
            "template_id": template_id,
            "params": template_params,
            "target": target,
            "profile": eff_profile,
            "priority": priority,
            "timeout_seconds": timeout_seconds,
        }, indent=2, ensure_ascii=False))
        return

    result = skill.submit(
        template_id=template_id,
        params=template_params,
        target=target,
        priority=priority,
        timeout_seconds=timeout_seconds,
    )

    _print_skill_result(result)


@skill_cli.command()
@click.argument("task_id")
@click.pass_context
def status(ctx, task_id):
    """Check task status."""
    skill = _get_skill(ctx)
    result = skill.status(task_id)
    _print_skill_result(result)


@skill_cli.command()
@click.argument("task_id")
@click.pass_context
def logs(ctx, task_id):
    """Get task logs."""
    skill = _get_skill(ctx)
    entries = skill.logs(task_id)
    for entry in entries:
        click.echo(f"[{entry.get('log_time', '?')}] "
                   f"{entry.get('level', 'INFO'):6s} {entry.get('message', '')}")


@skill_cli.command("result")
@click.argument("task_id")
@click.pass_context
def get_result(ctx, task_id):
    """Get task result."""
    skill = _get_skill(ctx)
    result = skill.status(task_id)
    if result.result:
        click.echo(json.dumps(result.result, indent=2, ensure_ascii=False))
    elif result.error:
        click.echo(f"Error: {result.error}")
    else:
        click.echo("No result yet (task may still be running)")


@skill_cli.command()
@click.argument("task_id")
@click.pass_context
def cancel(ctx, task_id):
    """Cancel a pending/running task."""
    skill = _get_skill(ctx)
    result = skill.cancel(task_id)
    _print_skill_result(result)


def _print_skill_result(result):
    """Pretty-print a SkillResult."""
    output = result.to_dict()
    click.echo(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    skill_cli()
