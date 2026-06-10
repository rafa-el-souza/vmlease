"""Battery loader — probes as data, NOT hardcoded.

A battery is a declarative list of probes living with the project/change that
needs it (e.g. ``openspec/explorations/<slug>/probes/*.json``), so the harness
stays project-agnostic: any change supplies its own battery file. This module
parses that file into typed :class:`~vmlease.model.Battery` / ``Probe``
objects and validates the shape (fail loud on a malformed battery).

Format (JSON): ``{"name": "...", "probes": [{"id","title","command","tag",
"classifies"?}, ...]}``. ``tag`` is one of the :class:`~vmlease.model.ProbeTag`
values.

**Authoring caveat** (:func:`lint_battery` warns about it): probes EXECUTE in
**authoring order** — the order they appear in the ``probes`` array is the order
they run and are recorded; ``tag`` records what a probe touches and governs sudo
escalation but does NOT reorder execution. A probe's ``ok`` is its command's
**exit code**, so gate assertions with ``exit $rc`` (a command ending in
``echo OK`` / ``echo FAIL`` always exits 0 → a vacuous ``ok`` that ignores what
it printed).
"""

from __future__ import annotations

import json
import re
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
    timeout = _parse_timeout(index, raw)
    return Probe(
        id=str(raw["id"]),
        title=str(raw["title"]),
        command=str(raw["command"]),
        tag=tag,
        classifies=str(raw.get("classifies", "")),
        timeout=timeout,
    )


def _parse_timeout(index: int, raw: dict[object, object]) -> float | None:
    """Parse the optional per-probe ``timeout`` (seconds).

    Absent means ``None`` (use the runner's run-wide default — back-compatible).
    A present value must be a positive number; a bool, a non-number, or a
    non-positive value is a malformed battery and raises :class:`BatteryError`.
    """
    if "timeout" not in raw:
        return None
    value = raw["timeout"]
    # ``bool`` is an ``int`` subclass — reject it explicitly so ``true``/``false``
    # is not silently read as ``1``/``0``.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BatteryError(f"probe #{index} 'timeout' must be a positive number, got {value!r}")
    if value <= 0:
        raise BatteryError(f"probe #{index} 'timeout' must be positive, got {value!r}")
    return float(value)


def _assert_unique_ids(probes: tuple[Probe, ...]) -> None:
    seen: set[str] = set()
    for p in probes:
        if p.id in seen:
            raise BatteryError(f"duplicate probe id {p.id!r}")
        seen.add(p.id)


def lint_battery(battery: Battery) -> tuple[str, ...]:
    """Non-fatal authoring warnings for a battery (never raises; ``()`` = clean).

    Probes execute in **authoring order**, so there is no reorder to surprise an
    author; the surviving footgun is ``ok`` semantics:

    - **vacuous-ok** — a probe's ``ok`` is its command's exit code. A command that
      prints OK/FAIL tokens but is not ``exit``-gated always exits 0, so ``ok`` is
      meaningless regardless of what it printed. Gate with ``exit $rc``.

    The vacuous-ok check is a best-effort heuristic (the shell is not parsed); the
    real guarantee is still the author's ``exit $rc``. Warnings are advisory — the
    run and ``ok`` are unaffected.
    """
    warnings: list[str] = []
    for probe in battery.probes:
        if _looks_vacuously_ok(probe.command):
            warnings.append(
                f"probe {probe.id!r}: ok reflects the command's exit code only, but the command "
                f"prints tokens without an explicit exit -- gate it with 'exit $rc'"
            )
    return tuple(warnings)


def _looks_vacuously_ok(command: str) -> bool:
    """True iff ``command`` prints tokens but won't exit-gate its ``ok`` (heuristic).

    Flags the token-printing footgun shape — a conditional echo tail (``&& echo`` /
    ``|| echo``) or a trailing ``echo`` segment — when there is no ``exit`` to make
    the status reflect the assertion. A plain command (``uname -a``) or an
    ``exit``-gated one is not flagged.

    The ``exit`` check is **statement-level** (after a separator / at the start), not
    a bare substring: the word "exit" inside an echo string (e.g.
    ``echo "setup exit: $RC"``) does NOT gate the status — matching it there was a
    false negative that hid genuinely-vacuous probes.
    """
    if re.search(r"(?:^|[;&|{}()\n])\s*exit\b", command):
        return False
    if "&& echo" in command or "|| echo" in command:
        return True
    last = re.split(r"[;\n]", command.strip())[-1].strip()
    return last == "echo" or last.startswith("echo ")
