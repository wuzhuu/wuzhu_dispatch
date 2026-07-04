"""dispatch-config: configuration and diagnostics CLI for wuzhu-dispatch.

Commands:
  validate-node      Validate a node.yaml file
  validate-client    Validate a client.yaml file
  check-dispatcher   Check dispatcher connectivity
  generate-node      Generate a node.yaml from a profile
  generate-client    Generate a client.yaml
  diagnose           Full diagnostic of a node config
  register-node      Register a compute node via admin API
  inspect-node       Inspect node registration status
  list-profiles      List available node profiles
"""

from __future__ import annotations

import json
import sys

import click
import yaml

from .config_skill import (
    check_dispatcher_connectivity,
    generate_client_yaml,
    generate_node_yaml,
    list_node_profiles,
    register_node_via_api,
    validate_client_yaml,
    validate_node_yaml,
)
from .skill_config import default_skill_yaml


@click.group()
def config_cli():
    """wuzhu-dispatch Config Skill — configuration and diagnostics."""


@config_cli.command("validate-node")
@click.argument("config_path", type=click.Path(exists=True))
@click.option("--json-output", "-j", is_flag=True, help="Output as JSON")
def validate_node(config_path, json_output):
    """Validate a node.yaml file."""
    report = validate_node_yaml(config_path)
    if json_output:
        click.echo(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_report(report)


@config_cli.command("validate-client")
@click.argument("config_path", type=click.Path(exists=True))
@click.option("--json-output", "-j", is_flag=True, help="Output as JSON")
def validate_client(config_path, json_output):
    """Validate a client.yaml file."""
    report = validate_client_yaml(config_path)
    if json_output:
        click.echo(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_report(report)


@config_cli.command("check-dispatcher")
@click.argument("url")
@click.option("--timeout", default=10, help="Connection timeout in seconds")
@click.option("--json-output", "-j", is_flag=True, help="Output as JSON")
def check_dispatcher(url, timeout, json_output):
    """Check dispatcher connectivity at URL."""
    report = check_dispatcher_connectivity(url, timeout)
    if json_output:
        click.echo(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_report(report)


@config_cli.command("generate-node")
@click.option("--profile", "-p", required=True, help="Node profile name")
@click.option("--node-id", "-n", required=True, help="Unique node identifier")
@click.option("--agent-token", "-t", default="CHANGE_ME", help="Agent token")
@click.option("--dispatcher-url", "-d", default="https://dispatch.example.com",
              help="Dispatcher base URL")
@click.option("--name", help="Human-readable node name")
@click.option("--region", "-r", help="Region code (e.g. HK, US)")
@click.option("--provider", help="Provider name")
@click.option("--output", "-o", type=click.Path(), help="Output file path")
def gen_node(profile, node_id, agent_token, dispatcher_url, name, region, provider, output):
    """Generate a node.yaml from a profile template."""
    try:
        yaml_str = generate_node_yaml(
            profile=profile,
            node_id=node_id,
            agent_token=agent_token,
            dispatcher_url=dispatcher_url,
            name=name or "",
            region=region or "",
            provider=provider or "",
        )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if output:
        with open(output, "w") as f:
            f.write(yaml_str)
        click.echo(f"Written to {output}")
    else:
        click.echo(yaml_str)


@config_cli.command("generate-client")
@click.option("--dispatcher-url", "-d", default="https://dispatch.example.com",
              help="Dispatcher base URL")
@click.option("--client-token", "-t", default="your-client-api-token",
              help="Client API token")
@click.option("--output", "-o", type=click.Path(), help="Output file path")
def gen_client(dispatcher_url, client_token, output):
    """Generate a client.yaml."""
    yaml_str = generate_client_yaml(dispatcher_url, client_token)
    if output:
        with open(output, "w") as f:
            f.write(yaml_str)
        click.echo(f"Written to {output}")
    else:
        click.echo(yaml_str)


@config_cli.command("list-profiles")
def list_profiles():
    """List available node profiles."""
    profiles = list_node_profiles()
    for name, info in profiles.items():
        click.echo(f"{name:20s}  {info['description']}")


@config_cli.command("register-node")
@click.option("--config", "-c", "config_path", type=click.Path(exists=True),
              help="Path to node.yaml")
@click.option("--dispatcher-url", "-d", help="Dispatcher URL (overrides config)")
@click.option("--admin-token", "-t", required=True,
              help="Admin token or DISPATCH_SERVER_SECRET")
@click.option("--dry-run", is_flag=True, help="Print payload without sending")
def reg_node(config_path, dispatcher_url, admin_token, dry_run):
    """Register a compute node via admin API."""
    if config_path:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        url = dispatcher_url or cfg.get("dispatcher_url", "")
        if not url:
            click.echo("Error: dispatcher_url not set", err=True)
            sys.exit(1)
    else:
        click.echo("Error: provide --config path to node.yaml", err=True)
        sys.exit(1)

    result = register_node_via_api(url, admin_token, cfg, dry_run=dry_run)
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


@config_cli.command("diagnose")
@click.option("--config", "-c", "config_path", type=click.Path(exists=True),
              help="Path to node.yaml to diagnose")
@click.option("--json-output", "-j", is_flag=True, help="Output as JSON")
def diagnose(config_path, json_output):
    """Full diagnostic of a node config file."""
    report = validate_node_yaml(config_path)
    # Also check dispatcher connectivity if configured
    if report.ok:
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            url = cfg.get("dispatcher_url", "")
            if url:
                conn_report = check_dispatcher_connectivity(url)
                report.checks.extend(conn_report.checks)
                report.warnings.extend(conn_report.warnings)
                report.errors.extend(conn_report.errors)
        except Exception:
            pass

    if json_output:
        click.echo(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_report(report)


@config_cli.command("generate-skill-yaml")
@click.option("--output", "-o", type=click.Path(), help="Output file path")
def gen_skill_yaml(output):
    """Generate a default skill.yaml."""
    content = default_skill_yaml()
    if output:
        with open(output, "w") as f:
            f.write(content)
        click.echo(f"Written to {output}")
    else:
        click.echo(content)


def _print_report(report):
    """Print a human-readable ConfigReport."""
    click.echo(f"Summary: {report.summary}")
    for c in report.checks:
        icon = "\u2705" if c.ok else "\u274c"
        click.echo(f"  {icon} {c.name}: {c.message}")
    for w in report.warnings:
        click.echo(f"  \u26a0\ufe0f  Warning: {w}")
    for e in report.errors:
        click.echo(f"  \u274c Error: {e}")


if __name__ == "__main__":
    config_cli()
