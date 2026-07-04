"""Built-in task templates for client-safe task creation.

Templates allow external Clients / Agents / Skills to request network
capabilities without directly submitting shell commands.  Each template
defines a schema for its parameters, security constraints, and a
generator that produces the ``execution`` payload.

Adding a new template:
  1. Define a ``_generate_<name>(params) -> dict`` function.
  2. Register it in ``BUILTIN_TEMPLATES``.
  3. The template ID becomes usable in ``POST /api/v1/client/tasks``
     via the ``template_id`` + ``params`` fields.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ── Parameter validation ──────────────────────────────────────────


def validate_params(schema: dict, params: dict) -> dict:
    """Validate *params* against a template *schema*.

    Returns cleaned params with defaults filled in.
    Raises ``ValueError`` on invalid input.
    """
    result: dict[str, Any] = {}
    for field_name, rules in schema.items():
        required = rules.get("required", False)
        default = rules.get("default")
        field_type = rules.get("type", "str")
        value = params.get(field_name, default)

        if value is None:
            if required:
                raise ValueError(f"Missing required parameter '{field_name}'")
            continue

        # Type coercion / validation
        if field_type == "str":
            if not isinstance(value, str):
                # Attempt str conversion
                value = str(value)
            min_len = rules.get("min_length", 0)
            max_len = rules.get("max_length", 4096)
            if len(value) < min_len:
                raise ValueError(f"'{field_name}' too short ({len(value)} < {min_len})")
            if len(value) > max_len:
                raise ValueError(f"'{field_name}' too long ({len(value)} > {max_len})")
            if "pattern" in rules:
                import re
                if not re.match(rules["pattern"], value):
                    raise ValueError(f"'{field_name}' does not match pattern {rules['pattern']}")

        elif field_type == "int":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"'{field_name}' must be an integer")
            min_v = rules.get("min")
            max_v = rules.get("max")
            if min_v is not None and value < min_v:
                raise ValueError(f"'{field_name}' must be >= {min_v}")
            if max_v is not None and value > max_v:
                raise ValueError(f"'{field_name}' must be <= {max_v}")

        elif field_type == "float":
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"'{field_name}' must be a number")
            min_v = rules.get("min")
            max_v = rules.get("max")
            if min_v is not None and value < min_v:
                raise ValueError(f"'{field_name}' must be >= {min_v}")
            if max_v is not None and value > max_v:
                raise ValueError(f"'{field_name}' must be <= {max_v}")

        elif field_type == "bool":
            if isinstance(value, bool):
                pass
            elif isinstance(value, str):
                value = value.lower() in ("true", "1", "yes")
            else:
                value = bool(value)

        elif field_type == "url":
            # URL validation
            from urllib.parse import urlparse
            if isinstance(value, str):
                parsed = urlparse(value)
                if parsed.scheme not in ("http", "https"):
                    raise ValueError(f"'{field_name}' must be http/https URL, got {parsed.scheme!r}")
                if not parsed.netloc:
                    raise ValueError(f"'{field_name}' invalid URL")
                # Deny internal / private IPs unless internal_network allowed
                if not rules.get("allow_internal", False):
                    _deny_private_url(value)
            else:
                raise ValueError(f"'{field_name}' must be a string URL")

        elif field_type == "domain":
            # Domain validation
            if isinstance(value, str):
                if not value.strip() or "." not in value:
                    raise ValueError(f"'{field_name}' is not a valid domain")
                import re as _re
                if not _re.match(r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", value.strip()):
                    raise ValueError(f"'{field_name}' is not a valid domain")
            else:
                raise ValueError(f"'{field_name}' must be a string")

        elif field_type == "host":
            # Host (IP or domain) validation
            if isinstance(value, str):
                import re as _re
                # IP or domain
                if not (_re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", value.strip())
                        or _re.match(r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", value.strip())
                        or _re.match(r"^[a-fA-F0-9:]+$", value.strip())):
                    raise ValueError(f"'{field_name}' is not a valid host")
            else:
                raise ValueError(f"'{field_name}' must be a string")

        result[field_name] = value
    return result


def _deny_private_url(url: str):
    """Raise ``ValueError`` if *url* points to a private / internal IP."""
    from urllib.parse import urlparse
    import ipaddress
    import socket
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        # Resolve if domain
        try:
            addr = socket.getaddrinfo(host, None)[0][4][0]
        except Exception:
            return  # Can't resolve, let it through (runtime will fail)
        ip = ipaddress.ip_address(addr)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
            raise ValueError(f"URL resolves to private/internal address: {addr}")
    except ValueError:
        raise
    except Exception:
        pass  # Best effort


# ═══════════════════════════════════════════════════════════════════
# Template generators
# ═══════════════════════════════════════════════════════════════════


def _generate_http_probe(params: dict) -> dict:
    """Generate shell command for HTTP probe."""
    url = params["url"]
    timeout = params.get("timeout", 10)
    command = (
        f"python3 -c \""
        f"import urllib.request, time, json; "
        f"start=time.time(); "
        f"try: "
        f"  r=urllib.request.urlopen('{url}', timeout={timeout}); "
        f"  latency=int((time.time()-start)*1000); "
        f"  body=r.read({params.get('max_bytes', 65536)}).decode('utf-8',errors='replace'); "
        f"  print(json.dumps({{'status_code':r.status,'latency_ms':latency,"
        f"'body_length':len(body),'headers':dict(r.headers.items())}}))"
        f"except Exception as e: "
        f"  print(json.dumps({{'error':str(e)}}))"
        f"\""
    )
    return {
        "execution": {"mode": "shell", "command": command},
        "_template": "http_probe",
    }


def _generate_dns_probe(params: dict) -> dict:
    """Generate shell command for DNS probe."""
    domain = params["domain"]
    record_type = params.get("record_type", "A")
    command = (
        f"python3 -c \""
        f"import socket, json, time; "
        f"start=time.time(); "
        f"try: "
        f"  info=socket.getaddrinfo('{domain}', None); "
        f"  ips=list(set(i[4][0] for i in info)); "
        f"  latency=int((time.time()-start)*1000); "
        f"  print(json.dumps({{'domain':'{domain}','record_type':'{record_type}',"
        f"'ips':ips,'latency_ms':latency}}))"
        f"except Exception as e: "
        f"  print(json.dumps({{'error':str(e)}}))"
        f"\""
    )
    return {
        "execution": {"mode": "shell", "command": command},
        "_template": "dns_probe",
    }


def _generate_ping_probe(params: dict) -> dict:
    """Generate shell command for ping probe."""
    host = params["host"]
    count = params.get("count", 4)
    command = (
        f"python3 -c \""
        f"import subprocess, json, re; "
        f"try: "
        f"  r=subprocess.run(['ping','-c','{count}','{host}'],"
        f"capture_output=True,text=True,timeout=30); "
        f"  output=r.stdout+r.stderr; "
        f"  m=re.search(r'received, (\\\\d+)%', output); "
        f"  loss=int(m.group(1)) if m else -1; "
        f"  m2=re.search(r'min/avg/max/[\\\\w]+=(\\\\S+)', output); "
        f"  rtt=m2.group(1) if m2 else ''; "
        f"  print(json.dumps({{'host':'{host}','packet_loss_pct':loss,"
        f"'rtt':rtt,'output':output[:2000]}}))"
        f"except Exception as e: "
        f"  print(json.dumps({{'error':str(e)}}))"
        f"\""
    )
    return {
        "execution": {"mode": "shell", "command": command},
        "_template": "ping_probe",
    }


def _generate_small_fetch(params: dict) -> dict:
    """Generate shell command for small file fetch."""
    url = params["url"]
    max_bytes = params.get("max_bytes", 1048576)
    command = (
        f"python3 -c \""
        f"import urllib.request, json; "
        f"try: "
        f"  r=urllib.request.urlopen('{url}', timeout=30); "
        f"  data=r.read({max_bytes}); "
        f"  print(json.dumps({{'status_code':r.status,"
        f"'content_length':len(data),"
        f"'content_preview':data[:1024].decode('utf-8',errors='replace'),"
        f"'headers':dict(r.headers.items())}}))"
        f"except Exception as e: "
        f"  print(json.dumps({{'error':str(e)}}))"
        f"\""
    )
    return {
        "execution": {"mode": "shell", "command": command},
        "_template": "small_fetch",
    }


def _generate_stock_collect_light(params: dict) -> dict:
    """Generate shell command for lightweight stock/API data collection."""
    endpoint = params.get("endpoint", "overview")
    symbols = params.get("symbols", "000001,600000")
    source = params.get("source", "sina")
    command = (
        f"python3 -c \""
        f"import urllib.request, json; "
        f"sources={{\"sina\":\"https://hq.sinajs.cn/list={symbols}\","
        f"\"eastmoney\":\"https://push2.eastmoney.com/api/qt/ulist.np/get\""
        f"}}; "
        f"url=sources.get('{source}', sources['sina']); "
        f"try: "
        f"  req=urllib.request.Request(url,headers={{'Referer':'https://finance.sina.com.cn'}}); "
        f"  r=urllib.request.urlopen(req,timeout=30); "
        f"  data=r.read(65536).decode('gbk',errors='replace'); "
        f"  print(json.dumps({{'source':'{source}','endpoint':'{endpoint}',"
        f"'data_length':len(data),'preview':data[:2000]}}))"
        f"except Exception as e: "
        f"  print(json.dumps({{'error':str(e)}}))"
        f"\""
    )
    return {
        "execution": {"mode": "shell", "command": command},
        "_template": "stock_collect_light",
    }


# ═══════════════════════════════════════════════════════════════════
# Template registry
# ═══════════════════════════════════════════════════════════════════

TemplateEntry = dict[str, Any]

BUILTIN_TEMPLATES: dict[str, TemplateEntry] = {
    "http_probe": {
        "description": "Check HTTP/HTTPS URL reachability from a compute node",
        "allowed_modes": ["template"],
        "schema": {
            "url": {"type": "url", "required": True, "max_length": 2048},
            "timeout": {"type": "int", "default": 10, "min": 1, "max": 60},
            "max_bytes": {"type": "int", "default": 65536, "min": 1024, "max": 1048576},
        },
        "generate": _generate_http_probe,
    },
    "dns_probe": {
        "description": "Resolve DNS records for a domain from a compute node",
        "allowed_modes": ["template"],
        "schema": {
            "domain": {"type": "domain", "required": True, "max_length": 256},
            "record_type": {"type": "str", "default": "A", "max_length": 8},
        },
        "generate": _generate_dns_probe,
    },
    "ping_probe": {
        "description": "ICMP ping test from a compute node",
        "allowed_modes": ["template"],
        "schema": {
            "host": {"type": "host", "required": True, "max_length": 256},
            "count": {"type": "int", "default": 4, "min": 1, "max": 20},
        },
        "generate": _generate_ping_probe,
    },
    "small_fetch": {
        "description": "Fetch a small file/URL content from a compute node",
        "allowed_modes": ["template"],
        "schema": {
            "url": {"type": "url", "required": True, "max_length": 2048},
            "max_bytes": {"type": "int", "default": 1048576, "min": 1024, "max": 10485760},
        },
        "generate": _generate_small_fetch,
    },
    "stock_collect_light": {
        "description": "Lightweight stock market data collection from a compute node",
        "allowed_modes": ["template"],
        "schema": {
            "endpoint": {"type": "str", "default": "overview", "max_length": 64},
            "symbols": {"type": "str", "default": "000001,600000", "max_length": 512},
            "source": {"type": "str", "default": "sina", "max_length": 32},
        },
        "generate": _generate_stock_collect_light,
    },
}


def list_templates() -> dict[str, dict]:
    """List available templates (without the generator functions)."""
    result = {}
    for tid, entry in BUILTIN_TEMPLATES.items():
        result[tid] = {
            "description": entry["description"],
            "allowed_modes": entry.get("allowed_modes", []),
            "schema": {k: {kk: vv for kk, vv in v.items() if kk != "type"}
                       for k, v in entry.get("schema", {}).items()},
        }
    return result


def get_template(template_id: str) -> TemplateEntry | None:
    """Get a template entry by ID, or ``None`` if not found."""
    return BUILTIN_TEMPLATES.get(template_id)


def generate_task_payload(template_id: str, params: dict) -> dict:
    """Validate params against a template and generate the execution payload.

    Returns the full task payload (including execution).  Raises
    ``ValueError`` if template not found or params invalid.
    """
    template = get_template(template_id)
    if not template:
        raise ValueError(f"Unknown template: {template_id!r}")

    schema = template.get("schema", {})
    validated = validate_params(schema, params)
    generator: Callable = template["generate"]
    execution_payload = generator(validated)
    execution_payload["_template"] = template_id
    execution_payload["_template_params"] = validated
    return execution_payload
