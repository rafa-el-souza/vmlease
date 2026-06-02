"""Battery loader — probes as data, NOT hardcoded.

A battery is a declarative list of probes living with the project/change that
needs it (e.g. ``openspec/explorations/<slug>/probes/*.json``), so the harness
stays project-agnostic: any change supplies its own battery file. This module
parses that file into typed :class:`~vmlease.model.Battery` / ``Probe``
objects and validates the shape (fail loud on a malformed battery).

Format (JSON): ``{"name": "...", "probes": [{"id","title","command","tag",
"classifies"?}, ...]}``. ``tag`` is one of the :class:`~vmlease.model.ProbeTag`
values.
"""

from __future__ import annotations

import json
from pathlib import Path

from vmlease.model import Battery, Probe, ProbeTag


class BatteryError(ValueError):
    """The battery file is malformed (bad JSON, missing field, unknown tag)."""


def parse_battery(text: str) -> Battery:
    """Parse a battery JSON document into a :class:`Battery`. Raises on any defect."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BatteryError(f"battery is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise BatteryError("battery root must be a JSON object")
    name = doc.get("name")
    if not isinstance(name, str) or not name:
        raise BatteryError("battery requires a non-empty string 'name'")
    raw_probes = doc.get("probes")
    if not isinstance(raw_probes, list) or not raw_probes:
        raise BatteryError("battery requires a non-empty 'probes' array")
    probes = tuple(_parse_probe(i, p) for i, p in enumerate(raw_probes))
    _assert_unique_ids(probes)
    return Battery(name=name, probes=probes)


def load_battery(path: Path) -> Battery:
    """Read + parse a battery file. Raises :class:`BatteryError` on a bad file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BatteryError(f"cannot read battery {path}: {exc}") from exc
    return parse_battery(text)


def _parse_probe(index: int, raw: object) -> Probe:
    if not isinstance(raw, dict):
        raise BatteryError(f"probe #{index} is not an object")
    missing = [k for k in ("id", "title", "command", "tag") if k not in raw]
    if missing:
        raise BatteryError(f"probe #{index} missing field(s): {missing}")
    tag_raw = raw["tag"]
    try:
        tag = ProbeTag(tag_raw)
    except ValueError as exc:
        valid = [t.value for t in ProbeTag]
        raise BatteryError(f"probe #{index} has unknown tag {tag_raw!r}; valid: {valid}") from exc
    return Probe(
        id=str(raw["id"]),
        title=str(raw["title"]),
        command=str(raw["command"]),
        tag=tag,
        classifies=str(raw.get("classifies", "")),
    )


def _assert_unique_ids(probes: tuple[Probe, ...]) -> None:
    seen: set[str] = set()
    for p in probes:
        if p.id in seen:
            raise BatteryError(f"duplicate probe id {p.id!r}")
        seen.add(p.id)
