"""shellcheck driver — severity-graded findings over every probe (D5/D6/D7).

Feeds each probe's resolved command to ``shellcheck --shell=bash --format=gcc -``
over stdin and parses the gcc-format output into located
:class:`ShellcheckFinding` records. The runner seam is injected for tests; the
default uses :func:`subprocess.run` bounded by a fixed timeout. A boundary
outcome (binary absent or wedged) yields the :data:`SHELLCHECK_UNAVAILABLE`
sentinel — a skip, never an exception.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from vmlease.model import Battery, PrepStep, Probe

Severity = Literal["error", "warning", "note", "style"]

# Severity ranking: ``style < note < warning < error`` (D5/Risks). shellcheck's
# ``--format=gcc`` emits exactly these four; ``style`` sits below ``note``.
_SEVERITY_RANK: dict[Severity, int] = {"style": 0, "note": 1, "warning": 2, "error": 3}

# The four gcc-format severities as proper :data:`Severity` literals, keyed by the
# matched string so the regex group narrows to ``Severity`` without a cast (the
# ``_GCC_LINE`` alternation guarantees the key is present, so direct indexing has
# no unreachable fallback branch).
_SEVERITIES: dict[str, Severity] = {"error": "error", "warning": "warning", "note": "note", "style": "style"}

# House discipline — every subprocess is bounded. shellcheck over a single probe
# is sub-second in practice; 60s is a generous ceiling that still bounds a wedged
# binary (mirrors ssh/provider-destroy's bound-every-subprocess posture).
_SHELLCHECK_TIMEOUT = 60.0

# The runner seam: ``(argv, stdin_text_or_None) -> CompletedProcess``. Injected so
# tests drive the driver with fakes — no real shellcheck is ever invoked in unit
# tests (mirrors ``ssh.SshSubprocessRunner``).
ShellcheckRunner = Callable[[list[str], "str | None"], "subprocess.CompletedProcess[str]"]


class _ShellcheckUnavailable:
    """Distinct sentinel for "shellcheck could not be consulted" (D5/D6).

    A module-level singleton (:data:`SHELLCHECK_UNAVAILABLE`) returned by
    :func:`shellcheck_battery` when the binary is absent (``FileNotFoundError``)
    or wedged (``subprocess.TimeoutExpired``) — distinct from an empty findings
    tuple (which means "consulted, nothing flagged"). The CLI (next milestone)
    surfaces it as a skip, never an exception.
    """

    __slots__ = ()


SHELLCHECK_UNAVAILABLE = _ShellcheckUnavailable()


@dataclass(frozen=True)
class ShellcheckFinding:
    """One shellcheck finding, located back to the probe that produced it.

    ``location`` is the probe's ``source`` — the script path for a ``script``
    probe (so gcc-format line numbers, which index the script content == the file
    content, still align with the file), or the probe id label for a ``run``
    probe (whose block is fed via stdin and so has no filename of its own).
    """

    probe_id: str
    location: str
    line: int
    column: int
    severity: Severity
    code: str
    message: str


# ``<file>:<line>:<col>: <severity>: <message>`` — shellcheck ``--format=gcc``. The
# SC code is appended in brackets at the end of the message (``... [SC2015]``); it
# is pulled out separately so ``code`` and ``message`` are clean.
_GCC_LINE = re.compile(
    r"^(?P<file>[^:]*):(?P<line>\d+):(?P<col>\d+):\s*(?P<severity>error|warning|note|style):\s*(?P<message>.*)$"
)
_CODE_SUFFIX = re.compile(r"\s*\[(SC\d+)\]\s*$")


def shellcheck_battery(
    battery: Battery, *, runner: ShellcheckRunner | None = None
) -> tuple[ShellcheckFinding, ...] | _ShellcheckUnavailable:
    """Shellcheck every probe in ``battery``; return findings or the skip sentinel.

    Each probe's command is fed to ``shellcheck --shell=bash --format=gcc -`` over
    **stdin** (both probe kinds, uniformly). Feeding via stdin is deliberate: since
    ``Probe.command`` already holds the resolved file contents, stdin sidesteps
    BOTH the flag-like-path question (there is no path on the argv, so the ``--``
    guard has nothing to guard) AND the path-resolution question (the driver only
    ever receives a resolved :class:`Battery`, never the manifest directory).
    shellcheck's gcc-format line numbers index the stdin content, which for a
    ``script`` probe IS the file's content — so line numbers still align with the
    file. Each finding is labelled with the probe's ``source`` (the script path for
    a ``script`` probe, the probe id for a ``run`` probe) so it stays locatable.

    The runner seam ``(argv, stdin_text) -> CompletedProcess`` is injected for
    tests; the default uses :func:`subprocess.run` bounded by
    :data:`_SHELLCHECK_TIMEOUT` (never ``shell=True``). Two boundary outcomes both
    yield :data:`SHELLCHECK_UNAVAILABLE` (a skip, never an exception): a
    ``FileNotFoundError`` (binary not installed) and a
    :class:`subprocess.TimeoutExpired` (wedged binary). Treating a timeout as
    unavailable — rather than as a finding-less error — keeps the contract simple:
    a driver that could not get a verdict from shellcheck reports exactly that, and
    the caller decides (skip vs. ``--require-shellcheck`` fail) uniformly.

    Kept **per-script-ref**-shaped (one shellcheck call per item) so it covers BOTH
    the battery's probes AND its ``[[prep.setup]]`` steps (D7) — a prep step carries
    the same ``id`` / ``command`` / ``source`` shape, so each is fed to shellcheck
    identically and located back the same way. (The structural verdict rules stay
    probes-only; this driver only lints shell text.)
    """
    run = runner if runner is not None else _default_shellcheck_runner
    findings: list[ShellcheckFinding] = []
    items: list[Probe | PrepStep] = list(battery.probes)
    if battery.prep is not None:
        items.extend(battery.prep.setup)
    for item in items:
        argv = ["shellcheck", "--shell=bash", "--format=gcc", "-"]
        try:
            proc = run(argv, item.command)
        except FileNotFoundError:
            return SHELLCHECK_UNAVAILABLE
        except subprocess.TimeoutExpired:
            return SHELLCHECK_UNAVAILABLE
        findings.extend(_parse_gcc_output(item, proc.stdout))
    return tuple(findings)


def _default_shellcheck_runner(argv: list[str], stdin_text: str | None) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` feeding ``stdin_text``, bounded by :data:`_SHELLCHECK_TIMEOUT`.

    Never ``shell=True``. ``subprocess.run`` drains the pipes concurrently and, on
    expiry, kills the process and raises :class:`subprocess.TimeoutExpired`;
    :func:`shellcheck_battery` turns both that and a ``FileNotFoundError`` into the
    unavailable sentinel. A non-zero shellcheck exit (findings present) is normal —
    ``check=False`` keeps it as data, not an exception.
    """
    return subprocess.run(
        argv,
        input=stdin_text,
        capture_output=True,
        text=True,
        check=False,
        timeout=_SHELLCHECK_TIMEOUT,
    )


def _parse_gcc_output(probe: Probe | PrepStep, output: str) -> list[ShellcheckFinding]:
    """Parse ``shellcheck --format=gcc`` ``output`` into findings for ``probe``.

    ``probe`` is any linted item — a :class:`~vmlease.model.Probe` or a
    :class:`~vmlease.model.PrepStep` (both carry ``id`` / ``source``). Non-matching
    lines (blank lines, banners) are tolerated and skipped. The SC code is split off
    the trailing ``[SCnnnn]`` bracket; a line with no code keeps an empty ``code``.
    """
    findings: list[ShellcheckFinding] = []
    for raw in output.splitlines():
        match = _GCC_LINE.match(raw)
        if match is None:
            continue
        message = match["message"].strip()
        code = ""
        code_match = _CODE_SUFFIX.search(message)
        if code_match is not None:
            code = code_match.group(1)
            message = message[: code_match.start()].strip()
        severity = _SEVERITIES[match["severity"]]
        findings.append(
            ShellcheckFinding(
                probe_id=probe.id,
                location=probe.source,
                line=int(match["line"]),
                column=int(match["col"]),
                severity=severity,
                code=code,
                message=message,
            )
        )
    return findings


def findings_at_or_above(
    findings: tuple[ShellcheckFinding, ...], threshold: Severity
) -> tuple[ShellcheckFinding, ...]:
    """Findings whose severity is ``>= threshold`` in ``style < note < warning < error``."""
    floor = _SEVERITY_RANK[threshold]
    return tuple(f for f in findings if _SEVERITY_RANK[f.severity] >= floor)
