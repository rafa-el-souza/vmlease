"""Safety layer — run-id/label generation, cost guard, teardown bookkeeping.

The guarantees that keep a throwaway-VM harness from leaking billable
resources or surprising the operator:

- every resource is labelled ``vmlease=<run-id>`` so a crash can still
  ``reap`` it by label;
- a **cost guard** caps host count and restricts server types to a cheap
  allowlist (a runaway matrix cannot spin 100 VMs);
- ``confirm-before-create`` text the CLI prints before any real spend.

Pure / deterministic where possible: the run-id is derived from a caller-
supplied token (NOT ``Date.now``/``random`` — the library avoids nondeterministic
time/rng so runs stay reproducible and testable), so tests pin it exactly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vmlease.model import Host
    from vmlease.providers import Provider

LABEL_KEY = "vmlease"

# Cheap, hourly-billed instances only. A matrix that asks for anything else is
# refused by the cost guard — a guard against an accidental fleet of big boxes.
DEFAULT_ALLOWED_SERVER_TYPES: frozenset[str] = frozenset({"cpx11", "cpx21", "cpx22", "cx23"})

# Hard cap on hosts per run (a runaway matrix backstop, well above any real
# 4-distro battery).
DEFAULT_MAX_HOSTS = 8

_RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,38}$")


class CostGuardError(ValueError):
    """A matrix would exceed the host cap or use a non-allowlisted server type."""


def make_run_id(token: str) -> str:
    """Derive a stable, label-safe run-id from a caller-supplied ``token``.

    The token is the determinism seam (a parameter, not module state / a
    wall-clock read): the caller passes a slug or timestamp
    string; this normalizes it to ``[a-z0-9-]`` and validates the shape. Same
    token in -> same run-id out, so a resumed/reaped run targets the same label.
    """
    norm = re.sub(r"[^a-z0-9-]+", "-", token.strip().lower()).strip("-")
    if not _RUN_ID_RE.match(norm):
        raise ValueError(
            f"token {token!r} does not yield a valid run-id (got {norm!r}; "
            f"need 3-39 chars of [a-z0-9-] starting alphanumeric)"
        )
    return norm


def run_label(run_id: str) -> dict[str, str]:
    """The single label every resource in a run carries (the reap key)."""
    return {LABEL_KEY: run_id}


def label_selector(run_id: str) -> str:
    """The provider label-selector string for ``list``/``reap`` by run."""
    return f"{LABEL_KEY}={run_id}"


@dataclass(frozen=True)
class CostGuard:
    """Bounds a run: max host count + an allowlist of cheap server types."""

    max_hosts: int = DEFAULT_MAX_HOSTS
    allowed_server_types: frozenset[str] = DEFAULT_ALLOWED_SERVER_TYPES

    def check(self, server_types: list[str]) -> None:
        """Refuse a matrix that exceeds the cap or uses a non-allowlisted type.

        ``server_types`` is one entry per requested host (so its length is the
        host count). Raises :class:`CostGuardError` on violation; returns
        ``None`` when the matrix is within bounds.
        """
        if len(server_types) > self.max_hosts:
            raise CostGuardError(
                f"matrix requests {len(server_types)} hosts; cost guard caps at "
                f"{self.max_hosts}. Narrow the matrix or raise --max-hosts deliberately."
            )
        bad = sorted({s for s in server_types if s not in self.allowed_server_types})
        if bad:
            raise CostGuardError(
                f"server type(s) {bad} not in the cheap allowlist "
                f"{sorted(self.allowed_server_types)}; refuse to provision."
            )


def reap(provider: Provider, run_id: str) -> list[Host]:
    """Destroy every live host carrying this run's label; return what was reaped.

    The orphan backstop: after a crash that skipped teardown, ``reap`` finds the
    run's hosts by their ``vmlease=<run-id>`` label and destroys each. Idempotent
    (``provider.destroy`` tolerates an already-gone host), so a re-reap is safe.
    """
    hosts = provider.list_labeled(run_id)
    for host in hosts:
        provider.destroy(host)
    return hosts
