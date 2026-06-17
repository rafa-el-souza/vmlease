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
label and ``timeout`` (seconds), and **exactly one of**:

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
it printed). A probe MAY instead declare ``[probe.assert]`` predicates: then
``ok`` is whether every declared assertion holds over the probe's outcome (exit
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
from types import MappingProxyType

from vmlease.assertions import _ASSERTIONS
from vmlease.capabilities import known_capabilities
from vmlease.distro import _SYSTEM_UPDATE_BY_MANAGER, FAMILIES
from vmlease.model import (
    Assertion,
    Battery,
    Prep,
    PrepStep,
    Probe,
    ProbeTag,
)

_PROBE_KEYS = frozenset(
    {"id", "title", "tag", "classifies", "timeout", "run", "script", "assert"}
)
_ROOT_KEYS = frozenset({"name", "probe", "requires", "prep"})
_PREP_KEYS = frozenset({"packages", "setup"})
_PREP_STEP_KEYS = frozenset({"id", "run", "script", "distros", "required", "title", "timeout"})

# The prep-phase ``timeout`` default (seconds) — longer than the probe-runner
# default, since prep work includes source builds (e.g. debian-13's ~1800s tlog
# source build). Applied at resolve time when a setup step omits ``timeout``, and
# reused by the runtime package-install pass (workload). Public: it's the single
# source of the prep-phase bound, shared across the loader and the runner.
PREP_STEP_DEFAULT_TIMEOUT = 1800.0


def _known_managers() -> frozenset[str]:
    """The known package-manager selector set (the system-update map's keys)."""
    return frozenset(_SYSTEM_UPDATE_BY_MANAGER)


def _known_distros() -> frozenset[str]:
    """The known **family** selector / allowlist set (the registry's family names).

    A prep selector keys on the distro **family** (D-11) — version-agnostic — so a
    ``distros = ["ubuntu"]`` step or an ``[prep.packages]`` ``ubuntu`` key matches
    every ubuntu host regardless of its version.
    """
    return FAMILIES


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
    public type — :func:`_resolve` turns it into a :class:`Battery`. ``requires``
    is already validated against the capability registry; ``prep`` carries the
    validated ``[prep.packages]`` mapping plus the pre-resolution prep-step specs
    (``None`` when the manifest declares no ``[prep]``).
    """

    name: str
    probes: tuple[_ProbeSpec, ...]
    requires: tuple[str, ...]
    prep: _PrepSpec | None


@dataclass(frozen=True)
class _PrepSpec:
    """The validated, pre-resolution prep phase — packages + prep-step specs.

    ``packages`` is the validated ``{selector: tuple[str, ...]}`` mapping (keys
    are known managers/distros); ``setup`` holds :class:`_PrepStepSpec` records
    whose ``script`` is not yet resolved. :func:`_resolve` turns it into a
    :class:`~vmlease.model.Prep`.
    """

    packages: dict[str, tuple[str, ...]]
    setup: tuple[_PrepStepSpec, ...]


@dataclass(frozen=True)
class _PrepStepSpec:
    """An internal, pre-resolution prep-step record (the pure shape pass output).

    Carries exactly one of ``run`` (an inline block) or ``script`` (a path
    string, not yet resolved); :func:`_resolve` turns a spec into a
    :class:`~vmlease.model.PrepStep` whose ``command`` is resolved shell.
    """

    id: str
    distros: tuple[str, ...]
    required: bool
    title: str
    timeout: float | None
    run: str | None
    script: str | None


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
    requires = _parse_requires(doc)
    prep = _parse_prep(doc)
    return _BatterySpec(name=name, probes=specs, requires=requires, prep=prep)


def _parse_requires(doc: dict[str, object]) -> tuple[str, ...]:
    """Parse + validate the root ``requires`` list (opt-in, default-off).

    Absent → ``()``. Each entry must be a string naming a known capability
    (``known_capabilities()``); a non-list, a non-string entry, or an unknown
    capability is a malformed battery and raises :class:`BatteryError` naming the
    offender. The order is preserved here (the canonicalizer normalizes it
    downstream); validation is membership-only.
    """
    if "requires" not in doc:
        return ()
    raw = doc["requires"]
    if not isinstance(raw, list):
        raise BatteryError("'requires' must be a list of capability names")
    known = known_capabilities()
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry:
            raise BatteryError(f"'requires' entry must be a non-empty string, got {entry!r}")
        if entry not in known:
            raise BatteryError(
                f"unknown capability {entry!r} in 'requires'; known: {sorted(known)}"
            )
        out.append(entry)
    return tuple(out)


def _parse_prep(doc: dict[str, object]) -> _PrepSpec | None:
    """Parse + validate the root ``[prep]`` section (``None`` when absent).

    Validates the strict shape: unknown keys at ``[prep]`` are rejected;
    ``[prep.packages]`` (:func:`_parse_prep_packages`) and ``[[prep.setup]]``
    (:func:`_parse_prep_setup`) are each validated. ``script`` steps are resolved
    later (:func:`_resolve`) — this pure pass does no filesystem access.
    """
    if "prep" not in doc:
        return None
    raw = doc["prep"]
    if not isinstance(raw, dict):
        raise BatteryError("'prep' must be a table ([prep])")
    unknown = set(raw) - _PREP_KEYS
    if unknown:
        raise BatteryError(f"[prep] has unrecognized key(s): {sorted(unknown)}")
    packages = _parse_prep_packages(raw.get("packages"))
    setup = _parse_prep_setup(raw.get("setup"))
    return _PrepSpec(packages=packages, setup=setup)


def _parse_prep_packages(raw: object) -> dict[str, tuple[str, ...]]:
    """Validate ``[prep.packages]`` — a flat ``{selector: [pkg, ...]}`` table.

    Absent → ``{}``. Every key must be a known package-manager OR a known distro
    (the two name-sets are disjoint and closed); a key that is neither raises
    :class:`BatteryError` naming it. Each value must be a list of non-empty
    strings. Resolution to a per-host union is the runner's job, not the loader's.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise BatteryError("[prep.packages] must be a table")
    valid = _known_managers() | _known_distros()
    out: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        if key not in valid:
            raise BatteryError(
                f"[prep.packages] has unrecognized selector key {key!r}; "
                f"must be a known package-manager or distro: {sorted(valid)}"
            )
        if not isinstance(value, list) or not all(
            isinstance(p, str) and p for p in value
        ):
            raise BatteryError(
                f"[prep.packages] {key!r} must be a list of non-empty package names"
            )
        out[key] = tuple(value)
    return out


def _parse_prep_setup(raw: object) -> tuple[_PrepStepSpec, ...]:
    """Validate ``[[prep.setup]]`` — an ordered array of setup-step specs.

    Absent → ``()``. Each element is parsed by :func:`_parse_prep_step`; step
    ``id``s must be unique across the array (a duplicate raises naming it).
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise BatteryError("[[prep.setup]] must be an array of tables")
    specs = tuple(_parse_prep_step(i, s) for i, s in enumerate(raw))
    seen: set[str] = set()
    for s in specs:
        if s.id in seen:
            raise BatteryError(f"duplicate prep setup id {s.id!r}")
        seen.add(s.id)
    return specs


def _parse_prep_step(index: int, raw: object) -> _PrepStepSpec:
    """Validate one ``[[prep.setup]]`` step into a pre-resolution spec.

    Enforces: a table; only recognized keys; a required string ``id``; exactly
    one of ``run``/``script``; an optional ``distros`` allowlist whose every value
    is a known distro (a typo guard — fail loud, do not silently skip on every
    host); an optional ``required`` bool (default ``True``); an optional string
    ``title``; an optional positive-number ``timeout``.
    """
    if not isinstance(raw, dict):
        raise BatteryError(f"prep setup step #{index} is not a table")
    unknown = set(raw) - _PREP_STEP_KEYS
    if unknown:
        raise BatteryError(
            f"prep setup step #{index} has unrecognized key(s): {sorted(unknown)}"
        )
    step_id = raw.get("id")
    if not isinstance(step_id, str) or not step_id:
        raise BatteryError(f"prep setup step #{index} requires a non-empty string 'id'")
    run, script = _parse_prep_command_form(step_id, raw)
    distros = _parse_prep_distros(step_id, raw)
    required = _parse_prep_required(step_id, raw)
    title = raw.get("title", "")
    if not isinstance(title, str):
        raise BatteryError(f"prep setup step {step_id!r} 'title' must be a string")
    timeout = _parse_prep_timeout(step_id, raw)
    return _PrepStepSpec(
        id=step_id,
        distros=distros,
        required=required,
        title=title,
        timeout=timeout,
        run=run,
        script=script,
    )


def _parse_prep_command_form(
    step_id: str, raw: dict[object, object]
) -> tuple[str | None, str | None]:
    """Enforce **exactly one of** ``run`` / ``script`` on a prep step.

    Neither (vacuous) and both (ambiguous) are malformed and named by ``id``.
    """
    has_run = "run" in raw
    has_script = "script" in raw
    if has_run and has_script:
        raise BatteryError(
            f"prep setup step {step_id!r} declares both 'run' and 'script'; exactly one is required"
        )
    if not has_run and not has_script:
        raise BatteryError(
            f"prep setup step {step_id!r} declares neither 'run' nor 'script'; exactly one is required"
        )
    if has_run:
        run = raw["run"]
        if not isinstance(run, str):
            raise BatteryError(f"prep setup step {step_id!r} 'run' must be a string")
        return run, None
    script = raw["script"]
    if not isinstance(script, str) or not script:
        raise BatteryError(f"prep setup step {step_id!r} 'script' must be a non-empty string")
    return None, script


def _parse_prep_distros(step_id: str, raw: dict[object, object]) -> tuple[str, ...]:
    """Validate a step's optional ``distros`` allowlist (default ``()`` = all).

    Each value must be a known distro key; an unknown distro is a typo and raises
    naming it, rather than silently excluding the step on every host (D13.6).
    """
    if "distros" not in raw:
        return ()
    value = raw["distros"]
    if not isinstance(value, list) or not all(isinstance(d, str) for d in value):
        raise BatteryError(f"prep setup step {step_id!r} 'distros' must be a list of strings")
    known = _known_distros()
    for d in value:
        if d not in known:
            raise BatteryError(
                f"prep setup step {step_id!r} 'distros' names unknown distro {d!r}; "
                f"known: {sorted(known)}"
            )
    return tuple(value)


def _parse_prep_required(step_id: str, raw: dict[object, object]) -> bool:
    """Validate a step's optional ``required`` flag (default ``True``)."""
    if "required" not in raw:
        return True
    value = raw["required"]
    if not isinstance(value, bool):
        raise BatteryError(f"prep setup step {step_id!r} 'required' must be a boolean")
    return value


def _parse_prep_timeout(step_id: str, raw: dict[object, object]) -> float | None:
    """Validate a step's optional ``timeout`` (seconds); absent → ``None``.

    Absent means ``None`` (the prep-step default applies at resolve time). A
    present value must be a positive number; a bool, a non-number, or a
    non-positive value raises :class:`BatteryError` (mirrors probe ``timeout``).
    """
    if "timeout" not in raw:
        return None
    value = raw["timeout"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BatteryError(
            f"prep setup step {step_id!r} 'timeout' must be a positive number, got {value!r}"
        )
    if value <= 0:
        raise BatteryError(
            f"prep setup step {step_id!r} 'timeout' must be positive, got {value!r}"
        )
    return float(value)


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
    prep = _resolve_prep(spec.prep, base_dir) if spec.prep is not None else None
    return Battery(name=spec.name, probes=probes, requires=spec.requires, prep=prep)


def _resolve_prep(spec: _PrepSpec, base_dir: Path) -> Prep:
    """Resolve a prep spec into a :class:`~vmlease.model.Prep`.

    Each setup step's ``script`` is read via :func:`_resolve_script_ref` (the same
    bundle-containment + symlink-safe reader probes use), the ``run`` form is the
    block verbatim; the resolved command is required **non-empty**. The packages
    mapping is frozen (immutable). A step omitting ``timeout`` takes the prep-step
    default (:data:`PREP_STEP_DEFAULT_TIMEOUT`).
    """
    setup = tuple(_resolve_prep_step(s, base_dir) for s in spec.setup)
    return Prep(
        packages=MappingProxyType(dict(spec.packages)),
        setup=setup,
    )


def _resolve_prep_step(spec: _PrepStepSpec, base_dir: Path) -> PrepStep:
    if spec.script is not None:
        command = _resolve_script_ref(spec.id, spec.script, base_dir)
        source = spec.script
    else:
        # ``run`` is guaranteed non-None here (exactly-one-of enforced at parse).
        assert spec.run is not None
        command = spec.run
        source = "<inline>"
    if not command.strip():
        raise BatteryError(f"prep setup step {spec.id!r} has an empty command")
    timeout = spec.timeout if spec.timeout is not None else PREP_STEP_DEFAULT_TIMEOUT
    return PrepStep(
        id=spec.id,
        command=command,
        distros=spec.distros,
        required=spec.required,
        title=spec.title,
        timeout=timeout,
        source=source,
    )


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


def structural_violations(battery: Battery) -> tuple[str, ...]:
    """No-verdict-source structural findings (the fatal subset of ``lint_battery``).

    A probe that **declares no assertions** whose command prints tokens without an
    explicit ``exit`` (see :func:`_looks_vacuously_ok`) has no source of truth for
    its ``ok`` other than the command's exit code — which an un-gated token tail
    leaves at ``0`` regardless of what it printed. There is no verdict source.

    This is the same detection as the vacuous-ok advisory in :func:`lint_battery`
    (single-sourced here so the advisory set and the fatal set are identical); a
    probe that **declares assertions** (``[probe.assert]``) or **exit-gates** its
    command is exempt. The ``vmlease lint`` command gates a non-zero exit on a
    non-empty result; at load it stays advisory via :func:`lint_battery`.
    """
    violations: list[str] = []
    for probe in battery.probes:
        if not probe.assertions and _looks_vacuously_ok(probe.command):
            violations.append(
                f"probe {probe.id!r}: ok reflects the command's exit code only, but the command "
                f"prints tokens without an explicit exit -- gate it with 'exit $rc'"
            )
    return tuple(violations)


def lint_battery(battery: Battery) -> tuple[str, ...]:
    """Non-fatal authoring warnings for a battery (never raises; ``()`` = clean).

    Probes execute in **authoring order**, so there is no reorder to surprise an
    author; two footguns are checked, both advisory:

    - **vacuous-ok** — a probe that **declares no assertions** whose ``ok`` is its
      command's exit code. A command that prints OK/FAIL tokens but is not
      ``exit``-gated always exits 0, so ``ok`` is meaningless regardless of what it
      printed. Gate with ``exit $rc``. A probe that **declares assertions**
      (``[probe.assert]``) is **exempt**: its ``ok`` is read from those declared
      predicates, not the exit code, so an un-gated token-printing tail is exactly
      the intended authoring style, not a footgun. These findings are
      single-sourced from :func:`structural_violations` (the advisory set is the
      same set ``vmlease lint`` gates on as fatal).

    - **non-host-root sudo** — a probe whose tag is not ``MUTATING_HOST_ROOT`` but
      whose command invokes ``sudo``. The escalation authoring contract reserves
      ``sudo`` for host-root-tagged probes, so the mismatch means the tag is lying
      about what the probe does. The command still runs verbatim; the warning
      surfaces the mislabel.

    Both checks are best-effort heuristics (the shell is not parsed); the real
    guarantee is still the author's ``exit $rc`` / tag. Warnings are advisory — the
    run and ``ok`` are unaffected, and a single probe may trigger both rules.
    """
    warnings: list[str] = list(structural_violations(battery))
    for probe in battery.probes:
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
