"""Serialize a run's results to a timestamped JSON file (ship-the-log-back).

A self-describing artifact per run (matrix + per-host detail snapshot + per-probe
outcomes) that a reviewer reads without re-deriving anything. The timestamp is
**passed in** (not read from the clock — the library avoids wall-clock reads so
runs stay reproducible and tests pin the filename).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vmlease.model import HostRun


def results_filename(run_id: str, timestamp: str) -> str:
    """The deterministic results filename for a run (timestamp is caller-supplied)."""
    return f"vmlease-{run_id}-{timestamp}.json"


def serialize_run(run_id: str, timestamp: str, host_runs: list[HostRun]) -> str:
    """Render the full results document as pretty JSON text."""
    doc = {
        "run_id": run_id,
        "timestamp": timestamp,
        "hosts": [
            {
                "name": hr.host_spec.name,
                "distro": hr.host_spec.distro_key,
                "image": hr.host_spec.image,
                "restored_image": hr.restored_image,
                "kept_host": (
                    {
                        "name": hr.kept_host.name,
                        "id": hr.kept_host.id,
                        "ipv4": hr.kept_host.ipv4,
                        "family": hr.kept_host.family,
                        "version": hr.kept_host.version,
                        "operator": hr.kept_host.operator,
                        "key_path": hr.kept_host.key_path,
                    }
                    if hr.kept_host is not None
                    else None
                ),
                "detail": hr.detail,
                "prep_phase": [
                    {
                        "id": ps.id,
                        "exit": ps.exit,
                        "required": ps.required,
                        "stderr": ps.stderr,
                    }
                    for ps in hr.prep_phase
                ],
                "probes": [
                    {
                        "id": r.probe_id,
                        "tag": r.tag.value,
                        "exit_code": r.exit_code,
                        "has_assertions": r.has_assertions,
                        "assertion_failures": list(r.assertion_failures),
                        "ok": r.ok,
                        "timed_out": r.timed_out,
                        "stdout": r.stdout,
                        "stderr": r.stderr,
                    }
                    for r in hr.results
                ],
            }
            for hr in host_runs
        ],
    }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def write_results(
    results_dir: Path, run_id: str, timestamp: str, host_runs: list[HostRun]
) -> Path:
    """Write the results JSON under ``results_dir`` and return its path.

    Creates ``results_dir`` if absent. The filename is deterministic given
    ``(run_id, timestamp)``.
    """
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / results_filename(run_id, timestamp)
    path.write_text(serialize_run(run_id, timestamp, host_runs), encoding="utf-8")
    return path


class IncrementalResultsWriter:
    """A per-host results sink: rewrites the full results file as each host lands.

    Stateful I/O sink for the runner's ``on_host_complete`` hook (D6). Its target
    :attr:`path` is deterministic and available *before* any write, so the caller
    can announce it up front. Each :meth:`add` appends the host to an internal
    accumulator and rewrites the **whole** file via the existing
    :func:`serialize_run` / :func:`write_results` path — so an aborted run leaves
    a file holding every host that finished before the abort, in the order they
    completed. The accumulator grows over the run; the :class:`HostRun` values it
    holds stay frozen.
    """

    def __init__(self, results_dir: Path, run_id: str, timestamp: str) -> None:
        self._results_dir = results_dir
        self._run_id = run_id
        self._timestamp = timestamp
        self._host_runs: list[HostRun] = []

    @property
    def path(self) -> Path:
        """The deterministic results path — known before the first :meth:`add`."""
        return self._results_dir / results_filename(self._run_id, self._timestamp)

    def add(self, host_run: HostRun) -> Path:
        """Record ``host_run`` and rewrite the full results file; return its path."""
        self._host_runs.append(host_run)
        return write_results(self._results_dir, self._run_id, self._timestamp, self._host_runs)
