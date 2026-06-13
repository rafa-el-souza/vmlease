"""Read-only summarizer over a raw vmlease results file — ONE canonical reader.

A raw results file (``results.py``) is a per-host x per-probe transcript whose
``ok`` is only the probe's exit code (the *vacuous-ok* footgun): the real
"did it pass?" lives in ``*_OK`` / ``*_FAIL`` assertion tokens a probe prints to
stdout. Every consumer that re-derives a verdict from those tokens invents its
own rule, so the same file yields different answers. This module is the single
canonical reader: it computes one deterministic verdict per probe and emits a
versioned ``.summary.json`` companion. The raw file is the source of truth and
is never mutated.

Like the rest of the library this module is pure (no wall-clock / RNG reads):
the timestamp and run-id come from the raw document, never the clock. The split
mirrors ``results.py`` — a pure :func:`summarize_results` builder + a thin
:func:`write_summary` I/O wrapper + a deterministic :func:`summary_filename`.

Summary shape (``schema_version`` ``"2"``)::

    {
      "schema_version": "2",
      "source_raw": "<path or name of the raw file, informational>",
      "run_id": "<from the raw doc>",
      "timestamp": "<from the raw doc>",
      "battery": "<battery name, or null when --battery not supplied>",
      "hosts": [
        {
          "name": "...", "distro": "...", "image": "...", "detail": "...",
          "probes": [
            {
              "id": "start",
              "command": "sandbox start",   # via battery overlay or PROBE_COMMAND_MAP
              "tag": "read-only",
              "exit_code": 0,
              "ok": true,                    # exit_code == 0 (raw, may be vacuous)
              "timed_out": false,
              "verdict": "PASS",             # the canonical computed verdict
              "assertion_failures": [],      # describe() of each failed assertion
              "ok_tokens": ["SETUP_EXIT0_OK"],
              "fail_tokens": [], "info_tokens": [], "review_tokens": [],
              "stdout_tail": "<last TAIL_LEN chars>",
              "stderr_tail": "<last TAIL_LEN chars>"
            }
          ],
          "not_run": ["destroy"]             # only when --battery supplied
        }
      ],
      "matrix": {"sandbox start": {"ubuntu": "FAIL", "fedora": "PASS"}},
      "totals": {"PASS": 3, "FAIL": 1, "TIMEOUT": 0, "PASS_NO_ASSERTIONS": 2}
    }

Verdict rule (per probe, deterministic precedence):

1. ``timed_out`` true → ``TIMEOUT``;
2. else, when the probe declared ≥1 declarative assertion (``has_assertions``),
   the runner-stored ``ok`` (the AND of those assertions) is authoritative →
   ``PASS`` iff ``ok`` holds, else ``FAIL`` (overriding the exit-code and token
   rules both ways);
3. else any ``fail_tokens`` OR ``exit_code != 0`` → ``FAIL``;
4. else ``exit_code == 0`` with ≥1 ``ok_token`` → ``PASS``;
5. else (``exit_code == 0``, no assertion tokens) → ``PASS_NO_ASSERTIONS``.

A pre-schema raw file (no ``has_assertions`` key) reads ``has_assertions=False``
for every probe and falls to the token path unchanged (M5).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vmlease.model import Battery

# --------------------------------------------------------------------------- #
# Constants — single source of truth for verdict strings + the contract knobs
# --------------------------------------------------------------------------- #
SCHEMA_VERSION = "2"

PASS = "PASS"
FAIL = "FAIL"
TIMEOUT = "TIMEOUT"
PASS_NO_ASSERTIONS = "PASS_NO_ASSERTIONS"

#: Worst-of collapse order (most severe first) for matrix-cell reduction.
VERDICT_SEVERITY: tuple[str, ...] = (TIMEOUT, FAIL, PASS_NO_ASSERTIONS, PASS)

#: All verdicts, in a stable order, so ``totals`` always carries every key.
ALL_VERDICTS: tuple[str, ...] = (PASS, FAIL, TIMEOUT, PASS_NO_ASSERTIONS)

#: Verdicts that make the overall run (and the CLI exit code) non-zero.
FAILING_VERDICTS: frozenset[str] = frozenset({FAIL, TIMEOUT})

#: Last N chars of each stream kept in the summary (full streams stay in raw).
TAIL_LEN = 2000

#: Generic assertion-token harvest: a SHOUTY identifier ending in a known suffix.
#: The trailing ``\b`` pins the suffix to the token END, so a longer word like
#: ``X_FAILED`` does not mis-match as a ``_FAIL`` token. (A compound such as
#: ``FOO_OK_info`` still resolves to its true terminal suffix ``info`` — the
#: greedy prefix consumes ``FOO_OK`` and the suffix is ``info``.)
TOKEN_RE = re.compile(r"[A-Z][A-Z0-9_]*_(OK|FAIL|info|review)\b")

#: Suffix → token-bucket key.
_SUFFIX_BUCKET = {
    "OK": "ok_tokens",
    "FAIL": "fail_tokens",
    "info": "info_tokens",
    "review": "review_tokens",
}
_BUCKET_KEYS: tuple[str, ...] = ("ok_tokens", "fail_tokens", "info_tokens", "review_tokens")

#: Built-in probe-id → canonical command label (the no-``--battery`` fallback).
PROBE_COMMAND_MAP: dict[str, str] = {
    "start": "sandbox start",
    "stop": "sandbox stop",
    "attach": "sandbox attach",
    "destroy": "sandbox destroy",
    "doctor": "sandbox doctor",
    "init": "sandbox init",
    "setup": "sandbox setup",
    "status-stopped": "sandbox status",
    "status-running": "sandbox status",
    "ws-list-1": "sandbox workspace list",
    "ws-list-2": "sandbox workspace list",
    "ws-add": "sandbox workspace add",
    "ws-remove": "sandbox workspace remove",
    "ws-rename": "sandbox workspace rename",
    "ws-restore": "sandbox workspace restore",
    "PREP": "prep",
    "setup-polkit-refused": "sandbox setup",
    "polkit-bypass-install": "sandbox setup",
}


# --------------------------------------------------------------------------- #
# Pure logic
# --------------------------------------------------------------------------- #
def harvest_tokens(stdout: str) -> dict[str, list[str]]:
    """Bucket assertion tokens from ``stdout`` by suffix (order-preserving, deduped).

    Returns a dict with all four bucket keys (``ok_tokens`` / ``fail_tokens`` /
    ``info_tokens`` / ``review_tokens``), each a list in first-seen order with
    duplicates removed.
    """
    buckets: dict[str, list[str]] = {k: [] for k in _BUCKET_KEYS}
    for match in TOKEN_RE.finditer(stdout):
        bucket = buckets[_SUFFIX_BUCKET[match.group(1)]]
        token = match.group(0)
        if token not in bucket:
            bucket.append(token)
    return buckets


def verdict(
    exit_code: int,
    timed_out: bool,
    fail_tokens: list[str],
    ok_tokens: list[str],
    ok: bool = False,
    has_assertions: bool = False,
) -> str:
    """Compute the one canonical verdict for a probe (see the module docstring).

    Precedence: ``timed_out`` dominates. When the probe declared ≥1 declarative
    assertion (``has_assertions``) the runner-stored ``ok`` (the AND of those
    assertions) is authoritative — ``PASS`` iff ``ok`` holds, else ``FAIL`` —
    overriding the exit-code AND token rules BOTH ways (a stray ``*_FAIL`` token
    cannot flip a passing assertion probe, nor a stray ``*_OK`` token a failing
    one). A probe without assertions falls through to the exact exit-code/token
    precedence unchanged.
    """
    if timed_out:
        return TIMEOUT
    if has_assertions:
        return PASS if ok else FAIL
    if fail_tokens or exit_code != 0:
        return FAIL
    if ok_tokens:
        return PASS
    return PASS_NO_ASSERTIONS


def _worst_of(verdicts: list[str]) -> str:
    """Collapse a non-empty list of verdicts to the most severe (worst-of) one.

    Severity is the index in :data:`VERDICT_SEVERITY` (lower = worse); callers
    only pass verdicts drawn from that ordering, so ``min`` always resolves.
    """
    return min(verdicts, key=VERDICT_SEVERITY.index)


def _command_for(probe_id: str, command_map: dict[str, str]) -> str:
    """Resolve a probe id to its command label; unknown ids degrade to the id."""
    return command_map.get(probe_id, probe_id)


def _battery_command_map(battery: Battery) -> dict[str, str]:
    """Build a probe-id → command label map from a battery.

    Prefers each probe's ``classifies`` (the design action it classifies); falls
    back to its ``title`` when ``classifies`` is empty, and to the id otherwise.
    """
    mapping: dict[str, str] = {}
    for probe in battery.probes:
        label = probe.classifies or probe.title or probe.id
        mapping[probe.id] = label
    return mapping


def _summarize_probe(raw_probe: dict[str, Any], command_map: dict[str, str]) -> dict[str, Any]:
    """Build one probe record from a raw probe dict (pure)."""
    probe_id = str(raw_probe.get("id", ""))
    exit_code = int(raw_probe.get("exit_code", 0))
    timed_out = bool(raw_probe.get("timed_out", False))
    stdout = str(raw_probe.get("stdout", ""))
    stderr = str(raw_probe.get("stderr", ""))
    ok = bool(raw_probe.get("ok", exit_code == 0))
    has_assertions = bool(raw_probe.get("has_assertions", False))
    assertion_failures = list(raw_probe.get("assertion_failures", []))
    tokens = harvest_tokens(stdout)
    return {
        "id": probe_id,
        "command": _command_for(probe_id, command_map),
        "tag": str(raw_probe.get("tag", "")),
        "exit_code": exit_code,
        "ok": ok,
        "timed_out": timed_out,
        "verdict": verdict(
            exit_code,
            timed_out,
            tokens["fail_tokens"],
            tokens["ok_tokens"],
            ok,
            has_assertions,
        ),
        "assertion_failures": assertion_failures,
        **tokens,
        "stdout_tail": stdout[-TAIL_LEN:],
        "stderr_tail": stderr[-TAIL_LEN:],
    }


def summarize_results(
    raw_doc: dict[str, Any], *, battery: Battery | None = None, source_raw: str = ""
) -> dict[str, Any]:
    """Build the full summary dict from an already-parsed raw results doc (no I/O).

    Command labels come from the built-in :data:`PROBE_COMMAND_MAP` for known
    probe ids; a supplied ``battery`` only fills labels for ids the builtin does
    not know (so matrix keys stay clean) AND enables declared-but-not-run
    detection (each host's ``not_run`` list). Without a battery, ``not_run`` is
    omitted.
    """
    if not isinstance(raw_doc, dict) or not isinstance(raw_doc.get("hosts"), list):
        raise ValueError("raw results doc must be an object with a 'hosts' array")

    # The built-in map owns the canonical command LABELS (clean strings like
    # "sandbox start"); a battery's per-probe text (classifies/title) is a
    # description, not a label, so the builtin WINS for known ids and the battery
    # only fills ids the builtin doesn't know. This keeps matrix keys clean even
    # when --battery is supplied (which is also what enables not-run detection).
    command_map = dict(PROBE_COMMAND_MAP)
    if battery is not None:
        for pid, label in _battery_command_map(battery).items():
            command_map.setdefault(pid, label)
    declared_ids = [p.id for p in battery.probes] if battery is not None else []

    hosts: list[dict[str, Any]] = []
    totals = dict.fromkeys(ALL_VERDICTS, 0)
    # matrix[command][distro] -> list of verdicts, collapsed worst-of at the end.
    matrix_acc: dict[str, dict[str, list[str]]] = {}

    for raw_host in raw_doc["hosts"]:
        distro = str(raw_host.get("distro", ""))
        probes = [_summarize_probe(p, command_map) for p in raw_host.get("probes", [])]
        observed_ids = {p["id"] for p in probes}

        for probe in probes:
            totals[probe["verdict"]] += 1
            cell = matrix_acc.setdefault(probe["command"], {}).setdefault(distro, [])
            cell.append(probe["verdict"])

        host_record: dict[str, Any] = {
            "name": str(raw_host.get("name", "")),
            "distro": distro,
            "image": str(raw_host.get("image", "")),
            "detail": str(raw_host.get("detail", "")),
            "probes": probes,
        }
        if battery is not None:
            host_record["not_run"] = [pid for pid in declared_ids if pid not in observed_ids]
        hosts.append(host_record)

    matrix = {
        command: {distro: _worst_of(verdicts) for distro, verdicts in by_distro.items()}
        for command, by_distro in matrix_acc.items()
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "source_raw": source_raw,
        "run_id": str(raw_doc.get("run_id", "")),
        "timestamp": str(raw_doc.get("timestamp", "")),
        "battery": battery.name if battery is not None else None,
        "hosts": hosts,
        "matrix": matrix,
        "totals": totals,
    }


def overall_exit_code(summary: dict[str, Any]) -> int:
    """``1`` iff any probe verdict is FAIL/TIMEOUT (per ``totals``), else ``0``."""
    totals = summary.get("totals", {})
    if any(totals.get(v, 0) for v in FAILING_VERDICTS):
        return 1
    return 0


# --------------------------------------------------------------------------- #
# Thin I/O wrappers
# --------------------------------------------------------------------------- #
def summary_filename(raw_path: Path) -> str:
    """Derive the ``<stem>.summary.json`` companion name beside a raw results file."""
    return f"{raw_path.stem}.summary.json"


def write_summary(summary: dict[str, Any], out_path: Path) -> Path:
    """Serialize ``summary`` to ``out_path`` as pretty JSON; never touches the raw file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_path
