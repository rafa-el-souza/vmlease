"""Shared test helpers for building ``battery.toml`` manifest fixtures.

Plain module (this is a stdlib-unittest project — no ``conftest``); imported by
the test modules that build inline battery manifests. The single ``_battery_toml``
here is the union of what the test suites need (the full variant), so both
``test_vmlease`` and ``test_summary`` share one implementation.
"""
from __future__ import annotations


def _toml_str(value: str) -> str:
    """Render a Python string as a TOML literal multi-line string (no escaping)."""
    return f"'''{value}'''"


def _battery_toml(name: str, probes: tuple[dict[str, object], ...]) -> str:
    """Build a ``battery.toml`` manifest from probe dicts.

    Each probe dict carries ``id``/``title``/``tag`` plus exactly one of ``run``
    (an inline block) or ``script`` (a path); ``classifies``/``timeout`` optional.
    Probes are written in the given order — that order IS the execution order, so
    callers list probes in their intended authoring (formerly tag-rank) order.
    """
    lines = [f"name = {_toml_str(name)}", ""]
    for p in probes:
        lines.append("[[probe]]")
        lines.append(f"id = {_toml_str(str(p['id']))}")
        lines.append(f"title = {_toml_str(str(p['title']))}")
        lines.append(f"tag = {_toml_str(str(p['tag']))}")
        if "classifies" in p:
            lines.append(f"classifies = {_toml_str(str(p['classifies']))}")
        if "timeout" in p:
            lines.append(f"timeout = {p['timeout']!r}")
        if "script" in p:
            lines.append(f"script = {_toml_str(str(p['script']))}")
        else:
            lines.append(f"run = {_toml_str(str(p['run']))}")
        lines.append("")
    return "\n".join(lines)
