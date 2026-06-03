"""Arch Linux image bootstrap — resolve-latest + verify (the safe half).

Hetzner has no Arch system image, but cannot boot an arbitrary ``.qcow2`` URL
either. The path is: download the latest official Arch cloud image, **verify it
(SHA256 + GPG signature)**, write it onto a Hetzner disk via the rescue system,
and snapshot the result once for reuse. This module owns the **resolve-latest +
download + verify** half — fully unit-testable with the network/gpg behind
injected runners — and stops short of the billable rescue-write (a separate,
live step). The "rebuild only if the latest version changed" decision lives here.

This is the **Arch instance** of the generic ``RescueImageSpec`` seam
(:mod:`vmlease.rescue_image`): :class:`~vmlease.rescue_image.ArchRescueImageSpec`
re-homes these functions (resolve-latest + sha + pinned-GPG) as one trust model;
a pinned golden image (sha-only) is the other. This module is kept, not deleted —
its Arch logic is the spec's body.

Security note: the image is trusted ONLY after BOTH checks pass — the SHA256
(integrity) and the detached GPG signature against the **pinned** Arch
release-engineering signing key (authenticity). The key fingerprint is pinned in
a small state file on first run (operator-confirmed), so a later mirror/key
swap is detected rather than silently trusted.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

MIRROR_BASE = "https://fastly.mirror.pkgbuild.com/images/"
# The CLOUDIMG variant (NOT basic): it ships cloud-init, which reads Hetzner's
# datasource and applies vmlease's --user-data prep + injected key on first
# boot — so Arch is a zero-special-case distro (verified on a real host 2026-06-01). The
# basic variant has no cloud-init and was rejected.
QCOW2_NAME = "Arch-Linux-x86_64-cloudimg.qcow2"

# The pinned Arch cloud-image signing key — ``arch-boxes <arch-boxes@archlinux.org>``
# (verified live 2026-06-01 against keyserver.ubuntu.com; the ed25519 [C] primary
# whose [S] subkey signs the images). This is the AUTHORITY for the signature
# check, re-run FRESH on every Arch build (no snapshot reuse — verify-every-run).
DEFAULT_ARCH_KEY_FINGERPRINT = "1B9A16984A4E8CB448712D2AE0B78BF4326C6F8F"
DEFAULT_ARCH_KEY_UID = "arch-boxes <arch-boxes@archlinux.org>"

# A version directory is ``vYYYYMMDD.NNNNNN/`` (date + a build serial).
_VERSION_RE = re.compile(r"v(\d{8})\.(\d+)")

# A run of bytes -> a CompletedProcess (the gpg/subprocess seam). Fetching is a
# separate injected callable (url -> bytes) so the network is mockable too.
Fetcher = Callable[[str], bytes]
TextFetcher = Callable[[str], str]
GpgRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class ArchImageError(RuntimeError):
    """Resolve/download/verify failed (no versions, sha mismatch, bad signature)."""


@dataclass(frozen=True)
class ArchVersion:
    """One Arch image version: the ``vYYYYMMDD.NNNNNN`` tag, sortable."""

    date: int
    serial: int

    @property
    def tag(self) -> str:
        return f"v{self.date}.{self.serial}"

    def as_sort_key(self) -> tuple[int, int]:
        return (self.date, self.serial)


def parse_versions(index_html: str) -> list[ArchVersion]:
    """Extract every ``vYYYYMMDD.NNNNNN`` version from a mirror directory listing.

    Tolerant of the listing format (Apache/nginx autoindex, an ``<a href>`` list,
    or plain text) — it scans for the version token anywhere. Deduplicated.
    """
    seen: dict[tuple[int, int], ArchVersion] = {}
    for m in _VERSION_RE.finditer(index_html):
        v = ArchVersion(date=int(m.group(1)), serial=int(m.group(2)))
        seen[v.as_sort_key()] = v
    return sorted(seen.values(), key=ArchVersion.as_sort_key)


def latest_version(index_html: str) -> ArchVersion:
    """Return the newest :class:`ArchVersion` in a listing, or raise."""
    versions = parse_versions(index_html)
    if not versions:
        raise ArchImageError(
            f"no Arch image versions found in the mirror index ({MIRROR_BASE}); "
            f"the listing format may have changed"
        )
    return versions[-1]


def version_dir_url(version: ArchVersion) -> str:
    """The mirror URL of a version's directory (trailing slash)."""
    return f"{MIRROR_BASE}{version.tag}/"


def qcow2_url(version: ArchVersion) -> str:
    return f"{version_dir_url(version)}{QCOW2_NAME}"


def sha256_url(version: ArchVersion) -> str:
    return f"{qcow2_url(version)}.SHA256"


def sig_url(version: ArchVersion) -> str:
    return f"{qcow2_url(version)}.sig"


def parse_expected_sha256(sha_file_text: str) -> str:
    """Extract the 64-hex digest from a ``.SHA256`` file (``<digest>  <name>``)."""
    m = re.search(r"\b([0-9a-fA-F]{64})\b", sha_file_text)
    if not m:
        raise ArchImageError(f"no sha256 digest found in SHA256 file: {sha_file_text[:120]!r}")
    return m.group(1).lower()


def verify_sha256(qcow2_bytes: bytes, expected_hex: str) -> None:
    """Raise :class:`ArchImageError` unless ``qcow2_bytes`` hashes to ``expected_hex``."""
    actual = hashlib.sha256(qcow2_bytes).hexdigest()
    if actual != expected_hex.lower():
        raise ArchImageError(
            f"qcow2 SHA256 mismatch: expected {expected_hex.lower()}, got {actual}"
        )


def build_gpg_verify_argv(sig_path: str, qcow2_path: str, keyring_path: str) -> list[str]:
    """argv for a detached-signature gpg verify against a dedicated keyring.

    Uses an explicit ``--keyring`` (the pinned Arch signing key) + ``--no-default-keyring``
    so verification does not depend on the operator's ambient gpg trust db.
    """
    return [
        "gpg",
        "--no-default-keyring",
        "--keyring", keyring_path,
        "--status-fd", "1",
        "--verify", sig_path, qcow2_path,
    ]


def verify_signature(
    sig_path: str,
    qcow2_path: str,
    keyring_path: str,
    expected_fingerprint: str,
    *,
    gpg_runner: GpgRunner,
) -> None:
    """Verify the detached signature AND that it was made by the pinned key.

    A ``gpg --verify`` exit 0 alone is insufficient — it only says *some* key in
    the keyring signed it. We additionally require the ``VALIDSIG`` status line
    to carry the **expected fingerprint**, so a different key in the keyring
    cannot satisfy the check. Raises :class:`ArchImageError` on any failure.
    """
    proc = gpg_runner(build_gpg_verify_argv(sig_path, qcow2_path, keyring_path))
    if proc.returncode != 0:
        raise ArchImageError(f"gpg signature verification failed ({proc.returncode}): {proc.stderr}")
    fp = expected_fingerprint.replace(" ", "").upper()
    # A gpg exit 0 only says SOME keyring key signed it; require the VALIDSIG
    # status line to carry the pinned fingerprint, so a different key cannot pass.
    if not _status_has_validsig(proc.stdout or "", fp):
        raise ArchImageError(
            f"signature is valid but NOT from the pinned key {expected_fingerprint!r}; "
            f"refusing the image (gpg status: {(proc.stdout or '')[:200]!r})"
        )


def _status_has_validsig(status: str, expected_fp_upper: str) -> bool:
    """``True`` iff a gpg ``VALIDSIG`` line carries ``expected_fp_upper``.

    The status line is ``VALIDSIG <signing-key-fpr> <date> <ts> … <primary-key-fpr>``:
    the field right after ``VALIDSIG`` is the (sub)key that actually made the
    signature, and the LAST field is the primary key's fingerprint. The Arch
    ``arch-boxes`` key signs with a ``[S]`` subkey under a certify-only ``[C]``
    primary, so a caller may legitimately pin EITHER the primary or the signing
    subkey fingerprint. Accept a match in either position so the pinned primary
    fingerprint validates a subkey-made signature (and vice-versa).
    """
    for line in status.splitlines():
        parts = line.split()
        if "VALIDSIG" not in parts:
            continue
        idx = parts.index("VALIDSIG")
        candidates = parts[idx + 1 :]
        if not candidates:
            continue
        signing_fp = candidates[0].upper()
        primary_fp = candidates[-1].upper()
        if expected_fp_upper in (signing_fp, primary_fp):
            return True
    return False


@dataclass(frozen=True)
class ResolvedImage:
    """A resolved + verified Arch image, ready for the (separate) rescue-write."""

    version: ArchVersion
    qcow2_bytes: bytes
    expected_sha256: str


def resolve_and_verify(
    *,
    text_fetcher: TextFetcher,
    fetcher: Fetcher,
    gpg_runner: GpgRunner,
    keyring_path: str,
    expected_fingerprint: str,
    write_temp: Callable[[bytes], str],
) -> ResolvedImage:
    """Resolve latest, download, and verify (SHA256 + pinned-key signature).

    Every external interaction is injected (``text_fetcher``/``fetcher`` for the
    mirror, ``gpg_runner`` for verification, ``write_temp`` to stage bytes for
    gpg) so this whole flow is unit-tested without network/gpg. Returns a
    :class:`ResolvedImage` only when BOTH checks pass; raises otherwise.
    """
    version = latest_version(text_fetcher(MIRROR_BASE))
    qcow2_bytes = fetcher(qcow2_url(version))
    expected_sha = parse_expected_sha256(text_fetcher(sha256_url(version)))
    verify_sha256(qcow2_bytes, expected_sha)

    sig_bytes = fetcher(sig_url(version))
    qcow2_path = write_temp(qcow2_bytes)
    sig_path = write_temp(sig_bytes)
    verify_signature(sig_path, qcow2_path, keyring_path, expected_fingerprint, gpg_runner=gpg_runner)
    return ResolvedImage(version=version, qcow2_bytes=qcow2_bytes, expected_sha256=expected_sha)
