"""Skill configuration — loaded from ~/.config/wuzhu-dispatch/skill.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class SkillConfig:
    """Typed wrapper around skill.yaml."""

    def __init__(self, data: dict[str, Any]):
        self.dispatcher_url: str = (data.get("dispatcher_url", "") or "").rstrip("/")
        self.client_token: str = data.get("client_token", "")

        defaults = data.get("defaults", {})
        self.default_wait_seconds: int = defaults.get("wait_seconds", 10)
        self.default_target: dict = defaults.get("target", {"mode": "auto", "tags": []})

        limits = data.get("limits", {})
        self.max_wait_seconds: int = limits.get("max_wait_seconds", 30)
        self.max_result_bytes: int = limits.get("max_result_bytes", 1048576)

        self.profiles: dict[str, dict] = data.get("profiles", {})

    @property
    def is_valid(self) -> bool:
        return bool(self.dispatcher_url) and bool(self.client_token)

    @classmethod
    def from_file(cls, path: str | Path | None = None) -> "SkillConfig":
        """Load skill.yaml from the given path or default locations."""
        if path:
            p = Path(path).expanduser().resolve()
        else:
            # Default paths
            candidates = [
                Path("~/.config/wuzhu-dispatch/skill.yaml").expanduser(),
                Path("~/.wuzhu-dispatch/skill.yaml").expanduser(),
                Path("./skill.yaml"),
            ]
            p = next((c for c in candidates if c.exists()), None)

        if p is None or not p.exists():
            return cls({})

        with open(p) as f:
            data = yaml.safe_load(f) or {}
        return cls(data)

    @classmethod
    def from_dict(cls, data: dict) -> "SkillConfig":
        return cls(data)

    def get_target_for_profile(self, profile_name: str | None) -> dict:
        """Resolve target from a named profile, or return the default target."""
        if profile_name and profile_name in self.profiles:
            return self.profiles[profile_name].get("target", self.default_target)
        return dict(self.default_target)

    def merge_target_with_profile(self, target: dict, profile_name: str | None) -> dict:
        """Merge an explicit target with a profile (profile tags augment)."""
        profile_target = self.get_target_for_profile(profile_name)
        merged = dict(profile_target)
        # Explicit target fields override profile
        for key in ("mode", "tags", "avoid_tags", "node_id", "requirements"):
            if key in target and target[key]:
                if key == "tags" and isinstance(merged.get(key), list):
                    # Merge tags (union)
                    merged_tags = set(merged.get(key, []))
                    merged_tags.update(target[key])
                    merged[key] = list(merged_tags)
                else:
                    merged[key] = target[key]
        return merged


def default_skill_yaml() -> str:
    """Return the default skill.yaml content as a string."""
    return """\
# wuzhu-dispatch Skill configuration
# Path: ~/.config/wuzhu-dispatch/skill.yaml
dispatcher_url: "https://dispatch.example.com"
client_token: "your-client-api-token"

defaults:
  wait_seconds: 10
  target:
    mode: auto
    tags: []

limits:
  max_wait_seconds: 30
  max_result_bytes: 1048576

# Named deployment profiles
profiles:
  foreign:
    target:
      mode: tags
      tags: ["foreign_reachable"]
  hk:
    target:
      mode: tags
      tags: ["hk", "cn_reachable"]
  us:
    target:
      mode: tags
      tags: ["us", "foreign_reachable"]
  small:
    target:
      mode: tags
      tags: ["probe", "lightweight"]
"""
