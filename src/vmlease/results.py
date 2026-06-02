"""Serialize a run's results to a timestamped JSON file (ship-the-log-back).

A self-describing artifact per run (matrix + per-host detail snapshot + per-probe
outcomes) that a reviewer reads without re-deriving anything. The timestamp is
**passed in** (not read from the clock — the library avoids wall-clock reads so
runs stay reproducible and tests pin the filename).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

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
                "detail": hr.detail,
                "probes": [
                    {
                        "id": r.probe_id,
                        "tag": r.tag.value,
                        "exit_code": r.exit_code,
                        "ok": r.ok,
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
