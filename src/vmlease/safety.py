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

import os
import re
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from vmlease.model import Host
    from vmlease.providers import Provider

LABEL_KEY = "vmlease"

# Cheap, hourly-billed instances only. A matrix that asks for anything else is
# refused by the cost guard — a guard against an accidental fleet of big boxes.
DEFAULT_ALLOWED_SERVER_TYPES: frozenset[str] = frozenset({"cpx11", "cpx21", "cpx22", "cx23"})

# Hard cap on hosts per run (a runaway matrix backstop, well above any real
# 4-distro battery).
DEFAULT_MAX_HOSTS = 8

# Self-runaway cap on cached images vmlease keeps. This is vmlease's OWN tidiness
# limit — NOT the provider account-wide snapshot ceiling, which is project-blind
# and enforced separately by ``ProviderQuotaError`` raised inside the provider.
DEFAULT_MAX_IMAGES = 10

_RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,38}$")


class CostGuardError(ValueError):
    """A matrix would exceed the host cap or use a non-allowlisted server type."""


class ImageQuotaError(ValueError):
    """Caching another image would exceed vmlease's self-imposed image cap.

    Distinct from the provider account-wide ceiling (a ``ProviderQuotaError``
    raised inside the provider): this is vmlease refusing to let its OWN cached
    image set grow without bound.
    """


class UploadError(ValueError):
    """An upload source or remote destination is problematic and is refused.

    Raised fail-closed *before any provider call* (in ``plan`` and at the top of
    ``execute``), so a bad ``--upload`` aborts before spend, never on a
    half-built billable host.
    """


# Conservative allowlist for an upload's remote destination: alnum plus a few
# path-safe punctuation chars. Deliberately excludes whitespace and every shell
# metacharacter (`;|&$()<>` backtick quotes), so an older scp that shell-expands
# the target cannot be steered. Tilde stays in for the default `~/<basename>`.
_REMOTE_DEST_RE = re.compile(r"^[A-Za-z0-9._/~@,=+-]+$")


def validate_upload_source(local: Path) -> None:
    """Refuse a problematic upload source fail-closed (footgun-prevention).

    Checks in order, *without following a symlink* (``lstat``), so a symlink's
    target is never read or shipped:

    1. missing → "does not exist";
    2. the final component is a symlink → "is a symlink";
    3. any component in the resolved chain is a symlink
       (``realpath`` != ``abspath``) → "path contains a symlink component";
    4. not a regular file (dir, FIFO, socket, device) → "is not a regular file";
    5. not readable → "is not readable".

    Raises :class:`UploadError` on the first failing check; returns ``None`` when
    the source is a plain, readable, non-symlinked regular file.
    """
    try:
        info = os.lstat(local)
    except FileNotFoundError as exc:
        raise UploadError(f"upload source {local} does not exist") from exc
    if stat.S_ISLNK(info.st_mode):
        raise UploadError(f"upload source {local} is a symlink (refused — its target is not read or shipped)")
    if os.path.realpath(local) != os.path.abspath(local):
        raise UploadError(f"upload source {local} path contains a symlink component (refused)")
    if not stat.S_ISREG(info.st_mode):
        raise UploadError(f"upload source {local} is not a regular file")
    if not os.access(local, os.R_OK):
        raise UploadError(f"upload source {local} is not readable")


def validate_upload_dir_source(local: Path) -> None:
    """Refuse a problematic **directory** upload source fail-closed.

    Mirrors :func:`validate_upload_source`'s posture for a directory entry point,
    inspected without following a symlink (``lstat``):

    1. missing → "does not exist";
    2. the final component is a symlink → "is a symlink";
    3. any component in the resolved chain is a symlink
       (``realpath`` != ``abspath``) → "path contains a symlink component";
    4. not a directory → "is not a directory";
    5. not readable → "is not readable".

    This guards the **entry-point** directory only. Within-tree symlink safety
    during transfer is the recursive push's job (``rsync --safe-links`` ships
    in-tree symlinks but drops ones pointing outside the tree), so a source tree
    cannot exfiltrate an out-of-tree file. Raises :class:`UploadError` on the first
    failing check; returns ``None`` for a plain, readable, non-symlinked directory.
    """
    try:
        info = os.lstat(local)
    except FileNotFoundError as exc:
        raise UploadError(f"upload source {local} does not exist") from exc
    if stat.S_ISLNK(info.st_mode):
        raise UploadError(f"upload source {local} is a symlink (refused — its target is not read or shipped)")
    if os.path.realpath(local) != os.path.abspath(local):
        raise UploadError(f"upload source {local} path contains a symlink component (refused)")
    if not stat.S_ISDIR(info.st_mode):
        raise UploadError(f"upload source {local} is not a directory")
    if not os.access(local, os.R_OK):
        raise UploadError(f"upload source {local} is not readable")


def validate_remote_dest(remote: str) -> None:
    """Refuse a problematic upload remote destination fail-closed.

    Refuses: an empty string; any ``/``-split segment equal to ``..`` (path
    traversal); a leading ``-`` (scp option-injection); any character outside a
    conservative allowlist (no spaces, no shell metacharacters). Raises
    :class:`UploadError` on the first failing check.
    """
    if not remote:
        raise UploadError("upload remote destination is empty")
    if remote.startswith("-"):
        raise UploadError(f"upload remote destination {remote!r} begins with '-' (refused — scp option injection)")
    if any(seg == ".." for seg in remote.split("/")):
        raise UploadError(f"upload remote destination {remote!r} contains a '..' path segment (refused)")
    if not _REMOTE_DEST_RE.match(remote):
        raise UploadError(
            f"upload remote destination {remote!r} contains a disallowed character "
            f"(allowed: letters, digits, and ._/~@,=+-)"
        )


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


@dataclass(frozen=True)
class ImageQuotaGuard:
    """Caps how many cache images vmlease keeps — count-only, no allowlist.

    The guard answers exactly one question — *is there headroom to create one
    more image?* — and nothing else: prune/supersession/reclaim decisions belong
    to ``build-image``'s orchestration, not here.
    """

    max_images: int = DEFAULT_MAX_IMAGES

    def check(self, current_count: int) -> None:
        """Refuse when there is no headroom to create one more cache image.

        Raises :class:`ImageQuotaError` iff ``current_count >= self.max_images``
        (no room for another); returns ``None`` when within bounds.
        """
        if current_count >= self.max_images:
            raise ImageQuotaError(
                f"{current_count} cache image(s) already exist; the image quota guard "
                f"caps at {self.max_images}. Run reap-images to reclaim space, or raise "
                f"--max-images deliberately."
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
