"""Battery loader — probes as data, NOT hardcoded.

A battery is a declarative list of probes living with the project/change that
needs it (e.g. the in-repo ``examples/compose-plugin-check/battery.toml``), so
the harness stays project-agnostic: any change supplies its own battery bundle.
This module parses that bundle into typed :class:`~vmlease.model.Battery` /
``Probe`` objects and validates the shape (fail loud on a malformed battery).

Format — a **TOML bundle**: a ``battery.toml`` manifest plus optional sibling
shell scripts, parsed with the standard-library ``tomllib`` (no third-party
dependency). The manifest carries a non-empty string ``name`` and a non-empty
``[[probe]]`` array. Each probe declares a stable ``id``, a ``title``, a ``tag``
(one of the :class:`~vmlease.model.ProbeTag` values), optional ``classifies``
label, ``timeout`` (seconds) and ``success_when`` token (see the authoring
caveat below), and **exactly one of**:

- ``run`` — a literal inline shell block (TOML's ``'''…'''`` multi-line strings
  need no escaping), used verbatim as the command; provenance ``"<inline>"``.
- ``script`` — a reference to a co-located ``.sh`` file (resolved relative to
  the manifest directory); the command is the file's contents, provenance is the
  script path as written.

The schema is **strict**: an unrecognized key — at the root or on a probe — is
rejected naming the key (so a ``timout`` typo fails loud rather than silently
falling back to the default timeout). A probe declaring neither / both of
``run`` / ``script`` is malformed and named.

``script`` paths are **contained to the bundle** (symlink-safe): an absolute
path, a ``..`` escape, or a symlink whose real target lies outside the manifest
directory is rejected — a manifest can never reach a file beyond its own bundle
(mirroring the ``upload_dir`` transport's ``--safe-links`` posture). The
resolved command text is also required **non-empty** — an empty ``run`` block or
empty/whitespace-only script file is a vacuous always-pass probe and is refused.

The loader is two passes: :func:`parse_battery` is the **pure** shape pass
(``tomllib`` + strictness checks → internal ``_ProbeSpec`` records, no
filesystem); :func:`load_battery` then resolves those specs against the
manifest directory via :func:`_resolve`, the **only** constructor of a
:class:`~vmlease.model.Battery`. A ``Battery`` therefore carries the invariant
that **every** ``Probe.command`` is non-empty resolved shell.

**Authoring caveat** (:func:`lint_battery` warns about it): probes EXECUTE in
**authoring order** — the order they appear in the ``[[probe]]`` array is the
order they run and are recorded; ``tag`` records what a probe touches but does
NOT reorder execution. A probe's ``ok`` is its command's **exit code by
default**, so gate assertions with ``exit $rc`` (a command ending in
``echo OK`` / ``echo FAIL`` always exits 0 → a vacuous ``ok`` that ignores what
it printed). A probe MAY instead declare an optional ``success_when`` token:
then ``ok`` is whether that token appears as a complete line of stdout (exit
code ignored), so such a probe needs no ``exit $rc`` and is exempt from the
vacuous-ok warning. On sudo: the resolved command runs **verbatim** — ``tag``
does not inject, strip, or enforce ``sudo``; the ``mutating:host-root`` tag
**authorizes and records** escalation, and :func:`lint_battery` emits an
**advisory** warning when a non-host-root probe invokes ``sudo`` (a mislabel
surfaced, not a blocked run). The results document this feeds remains JSON.

**Bash authoring contract**: probe commands are authored as **bash** — the
dialect ``vmlease lint`` checks via ``--shell=bash`` — and all four shipped
distro profiles provide bash for the operator. The transport-level bash
guarantee (executing each probe via ``bash -s`` over stdin) is a queued
follow-up change; until then this is an **authoring contract**, not a transport
guarantee.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from vmlease.assertions import _ASSERTIONS
from vmlease.model import Assertion, Battery, Probe, ProbeTag

_PROBE_KEYS = frozenset(
    {"id", "title", "tag", "classifies", "timeout", "run", "script", "success_when", "assert"}
)
_ROOT_KEYS = frozenset({"name", "probe"})


class BatteryError(ValueError):
    """The battery bundle is malformed.

    Raised for any defect in the TOML manifest or its scripts: invalid TOML, a
    missing required field, an unknown tag, an unrecognized key (root or probe), a
    probe declaring neither / both of ``run`` / ``script``, a ``script`` path that
    escapes the bundle (absolute, ``..``, or an out-of-tree symlink), a missing /
    unreadable script file, or an empty resolved command.
    """


@dataclass(frozen=True)
class _BatterySpec:
    """The output of the pure shape pass: a battery name + its pre-resolution probe specs.

    Not a :class:`Battery` (no resolved commands, no filesystem touched) and not a
    public type — :func:`_resolve` turns it into a :class:`Battery`.
    """

    name: str
    probes: tuple[_ProbeSpec, ...]


@dataclass(frozen=True)
class _ProbeSpec:
    """An internal, pre-resolution probe record — the output of the pure shape pass.

    Carries exactly one of ``run`` (an inline block) or ``script`` (a path string,
    not yet resolved); :func:`_resolve` turns a spec into a :class:`Probe` whose
    ``command`` is the resolved shell. Not a public type.
    """

    id: str
    title: str
    tag: ProbeTag
    classifies: str
    timeout: float | None
    run: str | None
    script: str | None
    success_when: str = ""
    assertions: tuple[Assertion, ...] = ()


def parse_battery(text: str) -> _BatterySpec:
    """Parse a battery TOML manifest into an internal spec (the pure shape pass).

    Runs ``tomllib.loads`` plus all shape / strictness checks and returns a frozen
    :class:`_BatterySpec` (name + :class:`_ProbeSpec` records) — **no** filesystem
    access and **not** a :class:`Battery`. Use :func:`load_battery` to resolve a
    spec into a battery. Raises :class:`BatteryError` on any defect.
    """
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise BatteryError(f"battery is not valid TOML: {exc}") from exc
    # ``tomllib.loads`` always returns a dict for a valid document; a root that is
    # not a table is not expressible in TOML, so no non-dict check is needed.
    unknown_root = set(doc) - _ROOT_KEYS
    if unknown_root:
        raise BatteryError(f"unrecognized root key(s): {sorted(unknown_root)}")
    name = doc.get("name")
    if not isinstance(name, str) or not name:
        raise BatteryError("battery requires a non-empty string 'name'")
    raw_probes = doc.get("probe")
    if not isinstance(raw_probes, list) or not raw_probes:
        raise BatteryError("battery requires a non-empty 'probe' array ([[probe]])")
    specs = tuple(_parse_probe(i, p) for i, p in enumerate(raw_probes))
    _assert_unique_ids(specs)
    return _BatterySpec(name=name, probes=specs)


def load_battery(path: Path) -> Battery:
    """Read + parse + resolve a battery bundle. Raises :class:`BatteryError` on any defect.

    The one public battery-producing entry point: ``parse_battery`` over the
    manifest text, then :func:`_resolve` against the manifest's directory (where
    ``script`` files live). The returned :class:`Battery` carries the invariant
    that every ``Probe.command`` is non-empty resolved shell.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BatteryError(f"cannot read battery {path}: {exc}") from exc
    spec = parse_battery(text)
    return _resolve(spec, path.parent)


def _parse_probe(index: int, raw: object) -> _ProbeSpec:
    if not isinstance(raw, dict):
        raise BatteryError(f"probe #{index} is not a table")
    unknown = set(raw) - _PROBE_KEYS
    if unknown:
        raise BatteryError(f"probe #{index} has unrecognized key(s): {sorted(unknown)}")
    missing = [k for k in ("id", "title", "tag") if k not in raw]
    if missing:
        raise BatteryError(f"probe #{index} missing field(s): {missing}")
    tag_raw = raw["tag"]
    try:
        tag = ProbeTag(tag_raw)
    except ValueError as exc:
        valid = [t.value for t in ProbeTag]
        raise BatteryError(f"probe #{index} has unknown tag {tag_raw!r}; valid: {valid}") from exc
    timeout = _parse_timeout(index, raw)
    success_when = _parse_success_when(index, raw)
    assertions = _parse_assertions(index, raw)
    run, script = _parse_command_form(index, raw)
    return _ProbeSpec(
        id=str(raw["id"]),
        title=str(raw["title"]),
        tag=tag,
        classifies=str(raw.get("classifies", "")),
        timeout=timeout,
        run=run,
        script=script,
        success_when=success_when,
        assertions=assertions,
    )


def _parse_command_form(index: int, raw: dict[object, object]) -> tuple[str | None, str | None]:
    """Enforce **exactly one of** ``run`` / ``script`` and return ``(run, script)``.

    Neither (vacuous) and both (ambiguous) are malformed and named.
    """
    has_run = "run" in raw
    has_script = "script" in raw
    if has_run and has_script:
        raise BatteryError(f"probe #{index} declares both 'run' and 'script'; exactly one is required")
    if not has_run and not has_script:
        raise BatteryError(f"probe #{index} declares neither 'run' nor 'script'; exactly one is required")
    if has_run:
        run = raw["run"]
        if not isinstance(run, str):
            raise BatteryError(f"probe #{index} 'run' must be a string")
        return run, None
    script = raw["script"]
    if not isinstance(script, str) or not script:
        raise BatteryError(f"probe #{index} 'script' must be a non-empty string")
    return None, script


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


def _parse_success_when(index: int, raw: dict[object, object]) -> str:
    """Parse the optional per-probe ``success_when`` literal token.

    Absent means ``""`` (exit-code reading — back-compatible). A *declared* value
    must be a non-empty, non-whitespace-only string; a non-string or a
    whitespace-only value is a malformed battery and raises :class:`BatteryError`
    naming the probe.
    """
    if "success_when" not in raw:
        return ""
    value = raw["success_when"]
    if not isinstance(value, str) or not value.strip():
        raise BatteryError(
            f"probe #{index} 'success_when' must be a non-empty string, got {value!r}"
        )
    return value


def _parse_assertions(index: int, raw: dict[object, object]) -> tuple[Assertion, ...]:
    """Compile the optional ``[probe.assert]`` table into bound :class:`Assertion`s.

    Absent → ``()`` (back-compatible). A present ``assert`` must be a TOML table
    (dict); each key must name a kind in the ``vmlease.assertions._ASSERTIONS``
    registry (the registry key set IS the schema — an unknown key is rejected
    naming it, mirroring the ``_PROBE_KEYS`` unknown-key message). For each known
    key the kind's ``validate`` then ``build`` run **here, in the pure pass** —
    so a malformed regex (compiled inside ``validate``/``build`` per D10(I))
    surfaces as a :class:`BatteryError` at load, not at evaluation. A ``ValueError``
    from either is re-raised as :class:`BatteryError` naming the probe AND the
    assertion key (D10(J)).
    """
    if "assert" not in raw:
        return ()
    table = raw["assert"]
    if not isinstance(table, dict):
        raise BatteryError(f"probe #{index} 'assert' must be a table")
    unknown = set(table) - set(_ASSERTIONS)
    if unknown:
        raise BatteryError(
            f"probe #{index} has unrecognized assertion key(s): {sorted(unknown)}"
        )
    built: list[Assertion] = []
    for key, value in table.items():
        kind = _ASSERTIONS[key]
        try:
            kind.validate(value)
            built.append(kind.build(value))
        except ValueError as exc:
            raise BatteryError(
                f"probe #{index} assertion {key!r} is malformed: {exc}"
            ) from exc
    return tuple(built)


def _assert_unique_ids(specs: tuple[_ProbeSpec, ...]) -> None:
    seen: set[str] = set()
    for s in specs:
        if s.id in seen:
            raise BatteryError(f"duplicate probe id {s.id!r}")
        seen.add(s.id)


def _resolve(spec: _BatterySpec, base_dir: Path) -> Battery:
    """Resolve a battery spec into a :class:`Battery` — the ONLY place one is built.

    A ``run`` probe spec's command is its block verbatim (provenance
    ``"<inline>"``); a ``script`` probe spec's command is the contents of the
    contained file at :func:`_resolve_script_ref` (provenance the script path as
    written). The resolved command is required **non-empty**. Reading scripts
    against ``base_dir`` (the manifest directory) is the only filesystem access in
    the loader.
    """
    probes = tuple(_resolve_probe(s, base_dir) for s in spec.probes)
    return Battery(name=spec.name, probes=probes)


def _resolve_probe(spec: _ProbeSpec, base_dir: Path) -> Probe:
    if spec.script is not None:
        command = _resolve_script_ref(spec.id, spec.script, base_dir)
        source = spec.script
    else:
        # ``run`` is guaranteed non-None here (exactly-one-of enforced at parse).
        assert spec.run is not None
        command = spec.run
        source = "<inline>"
    if not command.strip():
        raise BatteryError(f"probe {spec.id!r} has an empty command")
    return Probe(
        id=spec.id,
        title=spec.title,
        command=command,
        tag=spec.tag,
        classifies=spec.classifies,
        timeout=spec.timeout,
        source=source,
        success_when=spec.success_when,
        assertions=spec.assertions,
    )


def _resolve_script_ref(probe_id: str, script: str, base_dir: Path) -> str:
    """Read a co-located script's contents, contained to ``base_dir`` (symlink-safe).

    Per-script-ref (not probe-bound) so a future ``[[prep]]`` section can reuse
    it. Rejects an absolute path; resolves the real (symlink-followed) path and
    rejects it unless it is contained in the real ``base_dir`` — so a manifest
    cannot reach a file beyond its own bundle by any means, including a symlink. An
    ``OSError`` reading the file is a clear :class:`BatteryError` naming the probe
    and the path.
    """
    ref = Path(script)
    if ref.is_absolute():
        raise BatteryError(f"probe {probe_id!r} script path {script!r} must be relative to the bundle")
    real_base = base_dir.resolve()
    resolved = (base_dir / ref).resolve()
    if not resolved.is_relative_to(real_base):
        raise BatteryError(
            f"probe {probe_id!r} script {script!r} escapes the bundle directory {base_dir}"
        )
    try:
        return resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise BatteryError(f"probe {probe_id!r} cannot read script {script!r}: {exc}") from exc


def lint_battery(battery: Battery) -> tuple[str, ...]:
    """Non-fatal authoring warnings for a battery (never raises; ``()`` = clean).

    Probes execute in **authoring order**, so there is no reorder to surprise an
    author; two footguns are checked, both advisory:

    - **vacuous-ok** — a probe **without** a ``success_when`` declaration whose
      ``ok`` is its command's exit code. A command that prints OK/FAIL tokens but is
      not ``exit``-gated always exits 0, so ``ok`` is meaningless regardless of what
      it printed. Gate with ``exit $rc``. A probe **with** a ``success_when``
      declaration is **exempt**: its ``ok`` is read from the declared token, not the
      exit code, so an un-gated token-printing tail is exactly the intended
      authoring style, not a footgun.

    - **non-host-root sudo** — a probe whose tag is not ``MUTATING_HOST_ROOT`` but
      whose command invokes ``sudo``. The escalation authoring contract reserves
      ``sudo`` for host-root-tagged probes, so the mismatch means the tag is lying
      about what the probe does. The command still runs verbatim; the warning
      surfaces the mislabel.

    Both checks are best-effort heuristics (the shell is not parsed); the real
    guarantee is still the author's ``exit $rc`` / tag. Warnings are advisory — the
    run and ``ok`` are unaffected, and a single probe may trigger both rules.
    """
    warnings: list[str] = []
    for probe in battery.probes:
        if not probe.success_when and _looks_vacuously_ok(probe.command):
            warnings.append(
                f"probe {probe.id!r}: ok reflects the command's exit code only, but the command "
                f"prints tokens without an explicit exit -- gate it with 'exit $rc'"
            )
        if probe.tag is not ProbeTag.MUTATING_HOST_ROOT and _invokes_sudo(probe.command):
            warnings.append(
                f"probe {probe.id!r}: command invokes sudo but the probe is not tagged "
                f"{ProbeTag.MUTATING_HOST_ROOT.value!r} -- escalation belongs to host-root probes; "
                f"the command still runs verbatim, so re-tag the probe or drop the sudo"
            )
    return tuple(warnings)


def _invokes_sudo(command: str) -> bool:
    """True iff ``command`` invokes ``sudo`` (word-boundary heuristic).

    Best-effort, same character as :func:`_looks_vacuously_ok`: the shell is not
    parsed, so this matches a ``sudo`` word anywhere in the command text. The
    word-boundary anchor keeps it from firing on substrings like ``pseudo`` or
    ``sudoers``. No run behavior depends on the result — it only feeds an advisory
    warning.
    """
    return re.search(r"\bsudo\b", command) is not None


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
