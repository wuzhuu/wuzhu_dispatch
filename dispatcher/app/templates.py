"""Built-in task templates for client-safe task creation.

Templates allow external Clients / Agents / Skills to request network
capabilities without directly submitting shell commands.  Each template
defines a schema for its parameters, security constraints, and a
generator that produces the ``execution`` payload.

All shell commands embed user parameters via ``json.dumps()`` to prevent
shell injection — even a URL containing quotes or special chars cannot
break out of the ``python3 -c`` string.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ── Safe shell command builder ───────────────────────────────────


def _py(code: str) -> str:
    """Wrap *code* in ``python3 -c <json-encoded>``.

    ``json.dumps`` handles all escaping (quotes, backslashes, newlines)
    so user-supplied parameter values embedded in *code* via ``{var}``
    are safe against shell injection.
    """
    return f"python3 -c {json.dumps(code)}"


def _j(data: Any) -> str:
    """Shortcut for ``json.dumps`` (used inside f-strings)."""
    return json.dumps(data)


# ── Parameter validation ──────────────────────────────────────────


def validate_params(schema: dict, params: dict, caps: dict | None = None) -> dict:
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

        if field_type == "str":
            # ... existing str validation ...
            if not isinstance(value, str):
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
                # allow_internal: template rules first, then caps override
                allow_internal = rules.get("allow_internal", False)
                if not allow_internal and caps and caps.get("allow_internal_network", False):
                    allow_internal = True
                if not allow_internal:
                    _deny_private_url(value)
            else:
                raise ValueError(f"'{field_name}' must be a string URL")

        elif field_type == "domain":
            if isinstance(value, str):
                if not value.strip() or "." not in value:
                    raise ValueError(f"'{field_name}' is not a valid domain")
                import re as _re
                if not _re.match(r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", value.strip()):
                    raise ValueError(f"'{field_name}' is not a valid domain")
            else:
                raise ValueError(f"'{field_name}' must be a string")

        elif field_type == "host":
            if isinstance(value, str):
                import re as _re
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
        try:
            addr = socket.getaddrinfo(host, None)[0][4][0]
        except Exception:
            return
        ip = ipaddress.ip_address(addr)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
            raise ValueError(f"URL resolves to private/internal address: {addr}")
    except ValueError:
        raise
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════
# Template generators
# ═══════════════════════════════════════════════════════════════════


def _generate_http_probe(params: dict) -> dict:
    url = params["url"]
    timeout = params.get("timeout", 10)
    max_bytes = params.get("max_bytes", 65536)
    code = (
        f"import urllib.request, time, json\n"
        f"start = time.time()\n"
        f"try:\n"
        f"  r = urllib.request.urlopen({_j(url)}, timeout={timeout})\n"
        f"  latency = int((time.time()-start)*1000)\n"
        f"  body = r.read({max_bytes}).decode('utf-8', errors='replace')\n"
        f"  print(json.dumps({{'status_code': r.status, 'latency_ms': latency, "
        f"'body_length': len(body), 'headers': dict(r.headers.items())}}))\n"
        f"except Exception as e:\n"
        f"  print(json.dumps({{'error': str(e)}}))\n"
    )
    return {"execution": {"mode": "shell", "command": _py(code)}, "_template": "http_probe"}


def _generate_dns_probe(params: dict) -> dict:
    domain = params["domain"]
    record_type = params.get("record_type", "A")
    code = (
        f"import socket, json, time\n"
        f"start = time.time()\n"
        f"try:\n"
        f"  info = socket.getaddrinfo({_j(domain)}, None)\n"
        f"  ips = list(set(i[4][0] for i in info))\n"
        f"  latency = int((time.time()-start)*1000)\n"
        f"  print(json.dumps({{'domain': {_j(domain)}, 'record_type': {_j(record_type)}, "
        f"'ips': ips, 'latency_ms': latency}}))\n"
        f"except Exception as e:\n"
        f"  print(json.dumps({{'error': str(e)}}))\n"
    )
    return {"execution": {"mode": "shell", "command": _py(code)}, "_template": "dns_probe"}


def _generate_ping_probe(params: dict) -> dict:
    host = params["host"]
    count = params.get("count", 4)
    code = (
        f"import subprocess, json, re\n"
        f"try:\n"
        f"  r = subprocess.run(['ping', '-c', '{count}', {_j(host)}], "
        f"capture_output=True, text=True, timeout=30)\n"
        f"  output = r.stdout + r.stderr\n"
        f"  m = re.search(r'received, (\\\\d+)%', output)\n"
        f"  loss = int(m.group(1)) if m else -1\n"
        f"  m2 = re.search(r'min/avg/max/[\\\\w]+=(\\\\S+)', output)\n"
        f"  rtt = m2.group(1) if m2 else ''\n"
        f"  print(json.dumps({{'host': {_j(host)}, 'packet_loss_pct': loss, "
        f"'rtt': rtt, 'output': output[:2000]}}))\n"
        f"except Exception as e:\n"
        f"  print(json.dumps({{'error': str(e)}}))\n"
    )
    return {"execution": {"mode": "shell", "command": _py(code)}, "_template": "ping_probe"}


def _generate_small_fetch(params: dict) -> dict:
    url = params["url"]
    max_bytes = params.get("max_bytes", 1048576)
    code = (
        f"import urllib.request, json\n"
        f"try:\n"
        f"  r = urllib.request.urlopen({_j(url)}, timeout=30)\n"
        f"  data = r.read({max_bytes})\n"
        f"  print(json.dumps({{'status_code': r.status, "
        f"'content_length': len(data), "
        f"'content_preview': data[:1024].decode('utf-8', errors='replace'), "
        f"'headers': dict(r.headers.items())}}))\n"
        f"except Exception as e:\n"
        f"  print(json.dumps({{'error': str(e)}}))\n"
    )
    return {"execution": {"mode": "shell", "command": _py(code)}, "_template": "small_fetch"}


def _generate_stock_collect_light(params: dict) -> dict:
    symbols = params.get("symbols", "000001,600000")
    source = params.get("source", "sina")
    endpoint = params.get("endpoint", "overview")
    code = (
        f"import urllib.request, json\n"
        f"sources = {{\n"
        f"  'sina': 'https://hq.sinajs.cn/list=' + {_j(symbols)},\n"
        f"  'eastmoney': 'https://push2.eastmoney.com/api/qt/ulist.np/get',\n"
        f"}}\n"
        f"url = sources.get({_j(source)}, sources['sina'])\n"
        f"try:\n"
        f"  req = urllib.request.Request(url, "
        f"headers={{'Referer': 'https://finance.sina.com.cn'}})\n"
        f"  r = urllib.request.urlopen(req, timeout=30)\n"
        f"  data = r.read(65536).decode('gbk', errors='replace')\n"
        f"  print(json.dumps({{'source': {_j(source)}, "
        f"'endpoint': {_j(endpoint)}, "
        f"'data_length': len(data), 'preview': data[:2000]}}))\n"
        f"except Exception as e:\n"
        f"  print(json.dumps({{'error': str(e)}}))\n"
    )
    return {"execution": {"mode": "shell", "command": _py(code)}, "_template": "stock_collect_light"}


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


def generate_task_payload(template_id: str, params: dict, caps: dict | None = None) -> dict:
    """Validate params against a template and generate the execution payload.

    Returns the full task payload (including execution).  Raises
    ``ValueError`` if template not found or params invalid.
    """
    template = get_template(template_id)
    if not template:
        raise ValueError(f"Unknown template: {template_id!r}")

    schema = template.get("schema", {})
    validated = validate_params(schema, params, caps=caps)
    generator: Callable = template["generate"]
    execution_payload = generator(validated)
    execution_payload["_template"] = template_id
    execution_payload["_template_params"] = validated
    return execution_payload
