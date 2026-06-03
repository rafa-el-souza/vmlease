"""The ``RescueImageSpec`` seam — per-distro rescue-image acquisition + trust gate.

A rescue-write distro (one with no native provider image, e.g. Arch) gets its
OS by writing a verified disk image onto a cheap base host's disk via the rescue
system (the spine lives in :mod:`vmlease.archbuild`). *Which* image, *where it
comes from*, and *how it is trusted* used to be hardcoded to Arch. This module
extracts that into an injected ``RescueImageSpec``:

- **resolution** — resolve-latest from a mirror (Arch), a fixed pinned URL, or a
  local qcow2 file (golden);
- **trust gate** — SHA256 always, **plus** a detached signature against a pinned
  key when the spec defines one (Arch); SHA256-only for a golden image. The gate
  runs operator-side, BEFORE any host mutation — a failing image is never written.

``resolve_and_verify`` returns a :class:`ResolvedRescueImage` carrying the
verified digest + a source union (:class:`RemoteUrl` the rescue side fetches, or
:class:`LocalFile` the orchestrator pushes). The two real instances are
:class:`ArchRescueImageSpec` (re-homing :mod:`vmlease.archimage`, byte-faithful)
and :class:`GoldenRescueImageSpec` (fixed URL | local file, sha-only, no GPG).

The IO seams (``text_fetcher``/``fetcher``/``gpg_runner``/``write_temp``) are
injected at CALL time via :class:`ResolveDeps`, not held on the instance — so a
static :class:`~vmlease.distro.DistroProfile` can carry a spec while the whole
resolve+verify stays unit-testable with no network/gpg. A golden image ignores
the Arch-only seams (``text_fetcher``/``gpg_runner``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable

    from vmlease.archimage import GpgRunner


@dataclass(frozen=True)
class RemoteUrl:
    """A rescue image to be fetched on the rescue side (``curl``)."""

    url: str


@dataclass(frozen=True)
class LocalFile:
    """A rescue image on the orchestrator's disk, pushed to the rescue system (scp)."""

    path: Path


@dataclass(frozen=True)
class ResolvedRescueImage:
    """A resolved + operator-side-verified rescue image.

    Produced by :meth:`RescueImageSpec.resolve_and_verify` only after the trust
    gate passes (SHA256 always; pinned-key signature for specs that define one).
    ``source`` is a closed two-member union matching the two delivery modes.
    """

    expected_sha256: str
    source: RemoteUrl | LocalFile


@dataclass(frozen=True)
class ResolveDeps:
    """Injected IO seams for :meth:`RescueImageSpec.resolve_and_verify`.

    Bundled at call time (not held on the spec) so a static ``DistroProfile`` can
    carry a spec while resolve+verify stays unit-testable without network/gpg:

    - ``text_fetcher``: ``url -> str`` (the mirror index + ``.SHA256`` — Arch only).
    - ``fetcher``: ``url -> bytes`` (the qcow2 + ``.sig`` — Arch + golden-URL).
    - ``gpg_runner``: the detached-signature verify seam (Arch only).
    - ``write_temp``: stage bytes to a temp path for gpg ``--verify`` (Arch only).
    - ``keyring_path``: the gpg keyring holding the pinned signing key — a runtime
      path (set up alongside the throwaway keypair), so it is a call-time seam, not
      static spec config (Arch only).

    A :class:`GoldenRescueImageSpec` ignores ``text_fetcher``/``gpg_runner``/
    ``keyring_path`` (it has no index to resolve and no signature to check); the
    wider bundle is the cost of a uniform Protocol method.
    """

    text_fetcher: Callable[[str], str]
    fetcher: Callable[[str], bytes]
    gpg_runner: GpgRunner
    write_temp: Callable[[bytes], str]
    keyring_path: str


@runtime_checkable
class RescueImageSpec(Protocol):
    """Resolve + operator-side-verify a rescue image to a :class:`ResolvedRescueImage`.

    The trust gate: an image that fails verification raises here, before any host
    mutation, so nothing destructive runs against an untrusted image. Mirrors the
    project's other behavior seams (``SshRunner``, ``Workload``) — a Protocol, with
    config on the instance and IO injected at call time.
    """

    def resolve_and_verify(self, deps: ResolveDeps, /) -> ResolvedRescueImage:
        """Resolve the image, verify it operator-side, and return the result."""
        ...


@dataclass(frozen=True)
class ArchRescueImageSpec:
    """Arch's rescue image: resolve-latest from the pkgbuild mirror + SHA256 + pinned-GPG.

    Byte-faithful to the pre-extraction Arch path: ``resolve_and_verify`` delegates
    to :func:`vmlease.archimage.resolve_and_verify` (resolve-latest → fetch →
    ``verify_sha256`` → ``verify_signature`` against the pinned fingerprint) and
    returns a :class:`RemoteUrl` of the resolved qcow2 (the rescue side curls it).
    This instance's only config is the pinned signing-key ``fingerprint`` (the
    sole per-instance variation + the security pin); the mirror base + qcow2 name
    remain :mod:`vmlease.archimage`'s constants, as the Arch resolver. The gpg
    keyring path is a runtime IO seam (on :class:`ResolveDeps`), not static config.
    """

    fingerprint: str

    def resolve_and_verify(self, deps: ResolveDeps, /) -> ResolvedRescueImage:
        from vmlease.archimage import qcow2_url, resolve_and_verify

        resolved = resolve_and_verify(
            text_fetcher=deps.text_fetcher,
            fetcher=deps.fetcher,
            gpg_runner=deps.gpg_runner,
            keyring_path=deps.keyring_path,
            expected_fingerprint=self.fingerprint,
            write_temp=deps.write_temp,
        )
        return ResolvedRescueImage(
            expected_sha256=resolved.expected_sha256,
            source=RemoteUrl(qcow2_url(resolved.version)),
        )


@dataclass(frozen=True)
class GoldenRescueImageSpec:
    """A pinned golden image: a fixed URL OR a local qcow2, verified by SHA256 alone.

    No resolve-latest, no GPG — the operator pins the exact ``sha256`` of a known
    bootable qcow2. Exactly one of ``url`` / ``path`` is set: a ``url`` is fetched
    (via ``deps.fetcher``) and yields a :class:`RemoteUrl`; a ``path`` is read off
    the local disk and yields a :class:`LocalFile` the orchestrator pushes to the
    rescue system. Either way the bytes are SHA256-checked against the pinned
    digest before the image is accepted. ``deps.text_fetcher`` / ``deps.gpg_runner``
    are ignored (golden has no index to resolve and no signature to verify).
    """

    sha256: str
    url: str = ""
    path: Path | None = None

    def resolve_and_verify(self, deps: ResolveDeps, /) -> ResolvedRescueImage:
        from vmlease.archimage import ArchImageError, verify_sha256

        if (self.url == "") == (self.path is None):
            raise ArchImageError(
                "GoldenRescueImageSpec requires exactly one of url / path "
                f"(url={self.url!r}, path={self.path!r})"
            )
        if self.path is not None:
            data = self.path.read_bytes()
            verify_sha256(data, self.sha256)
            return ResolvedRescueImage(expected_sha256=self.sha256, source=LocalFile(self.path))
        data = deps.fetcher(self.url)
        verify_sha256(data, self.sha256)
        return ResolvedRescueImage(expected_sha256=self.sha256, source=RemoteUrl(self.url))
