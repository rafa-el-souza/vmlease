"""Content-addressed cache keys, labels, and supersession — pure data + hashing.

The cache contract lives here (D4 / D5 / D10): a snapshot image is named by a
**content key** that is a pure function of ``(distro, arch, recipe, upstream)``,
carries a **rich label set** emitted from one function so the keys can't drift,
and a group's **superseded** predecessors are the ones whose key differs from
that group's current key.

This module does **no** provider or network I/O of its own. The only variable
input — the base-image fingerprint — comes through an injected
:class:`~vmlease.rescue_image.ResolveDeps` seam, so the whole key/label/supersede
surface stays unit-testable with no network/gpg. Determinism is load-bearing:
``content_key`` MUST produce identical output for ``build-image`` (to label) and
``run`` (to look up), so it reads no clock and no rng.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from vmlease.cloudinit import render_cloudinit

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from vmlease.distro import DistroProfile
    from vmlease.model import Image
    from vmlease.rescue_image import ResolveDeps

# The pinned per-run-pubkey sentinel (D4): the canonical cloud-init render uses
# the *real* operator name and THIS fixed value in place of the throwaway probe
# pubkey, so the per-run key is excluded from the content key while the operator
# (which affects the baked user) is included. The value is arbitrary but PINNED —
# changing it silently invalidates every existing cache image's key.
_CACHE_KEY_CANONICAL_PUBKEY = "vmlease-cache-key-canonical-pubkey"

# Hash-truncation width: the first 32 lowercase-hex chars of the SHA-256 digest,
# sized so ``v1-<distro>-<32hex>`` fits the provider's ≤63-char label limit.
_KEY_HASH_HEX_WIDTH = 32

# The cache label keys (D5). One source of truth so the query index can't drift
# from what ``build-image`` emits and ``reap-images`` / ``run`` query.
LABEL_PURPOSE = "vmlease-purpose"
LABEL_CACHE_KEY = "vmlease-cache-key"
LABEL_SCHEMA = "vmlease-schema"
LABEL_DISTRO = "vmlease-distro"
LABEL_ARCH = "vmlease-arch"
LABEL_SOURCE_FP = "vmlease-source-fp"
LABEL_BUILT = "vmlease-built"

# Fixed label values.
PURPOSE_IMAGE_CACHE = "image-cache"
SCHEMA_V1 = "v1"

# Provider label values are capped at 63 chars; a 64-hex sha source-fp would
# overflow, so values are truncated to this width (matching the key's hash
# width — a sha folds to its first 32 hex either way).
_LABEL_VALUE_MAX = 63


def base_fingerprint(profile: DistroProfile, arch: str, deps: ResolveDeps) -> str:
    """Resolve the base-image fingerprint for ``(profile, arch)`` (D4).

    The single per-run-variable hashed input. Two sources, one uniform call:

    - **native** (``not profile.needs_rescue_write``) → the arch-blind provider
      image slug ``profile.default_image``; the probe-confirmed Hetzner system
      image does not drift under its slug, so the slug *is* the fingerprint.
    - **rescue-write** (Arch *and* golden — both carry a ``rescue_image``) → the
      digest from ``profile.rescue_image.resolve_and_verify(deps).expected_sha256``
      (Arch resolves the latest qcow2 sha → auto-freshness; golden returns its
      pinned sha). This is the cheap *resolve*, never the costly *write*.

    ``arch`` is accepted for a uniform signature (and so a future arch-specific
    base source is a local change); native's slug is arch-blind, so ``arch`` is
    folded into the key by :func:`content_key`, not here.

    ``deps`` is the injected IO seam — unit tests pass a fake spec / fake resolve
    with no network/gpg. A rescue-write resolve that raises (mirror down) is NOT
    caught here; the caller decides the policy (the run path fails on the cold
    path it shares; supersession is fail-safe — see :func:`resolve_current_keys`).
    """
    if not profile.needs_rescue_write:
        return profile.default_image
    # mypy: needs_rescue_write is True ⇒ rescue_image is not None.
    assert profile.rescue_image is not None
    return profile.rescue_image.resolve_and_verify(deps).expected_sha256


def content_key(
    profile: DistroProfile,
    arch: str,
    operator: str,
    deps: ResolveDeps,
) -> str:
    """The cache content key ``"v1-<distro>-<32hex>"`` (D4).

    A **pure function of ``(distro, arch, recipe, upstream)``** — the D10
    supersession invariant (exactly one current key per ``(distro, arch)``
    group). The hash covers:

    - ``arch`` — folded in explicitly. D4's literal formula hashes
      ``base_fp + "\\0" + canonical_cloud_init``; but native's ``base_fp`` (the
      provider slug) is **arch-blind**, so that formula alone would collide
      across architectures. To honor D10's "pure function of … arch …", ``arch``
      is hashed alongside ``base_fp`` (see the orchestrator note in the M2a
      report — the pinned arch-fold resolving D4-vs-D10).
    - ``base_fp`` — the upstream fingerprint (:func:`base_fingerprint`).
    - the **canonical** rendered cloud-init — ``render_cloudinit`` with the real
      ``operator`` and the pinned :data:`_CACHE_KEY_CANONICAL_PUBKEY` sentinel,
      so the per-run pubkey is normalized out while packages / extra_setup /
      docker_repo / system_update / finalize / template are all captured.

    Hash = **SHA-256**, lowercase hex, first 32 chars. The ``\\0`` separators
    prevent boundary ambiguity (so ``"a" + "bc"`` ≠ ``"ab" + "c"``). Inputs are
    UTF-8 bytes.
    """
    base_fp = base_fingerprint(profile, arch, deps)
    return content_key_from_base_fp(base_fp, profile, arch, operator)


def content_key_from_base_fp(
    base_fp: str,
    profile: DistroProfile,
    arch: str,
    operator: str,
) -> str:
    """The cache content key given an **already-resolved** base fingerprint.

    The pure key derivation half of :func:`content_key`, split out so a caller that
    has *already* paid the (network, gpg) :func:`base_fingerprint` resolve can
    derive the key without resolving twice. ``content_key`` is exactly
    ``content_key_from_base_fp(base_fingerprint(profile, arch, deps), …)`` — one
    implementation, so the two can never hash different bytes.

    Hashes the SAME payload as :func:`content_key`: ``arch \\0 base_fp \\0
    canonical_cloud_init`` (the canonical render uses the real ``operator`` and the
    pinned :data:`_CACHE_KEY_CANONICAL_PUBKEY`), SHA-256, lowercase hex, first 32.
    """
    canonical_cloud_init = render_cloudinit(profile, operator, _CACHE_KEY_CANONICAL_PUBKEY)
    payload = f"{arch}\0{base_fp}\0{canonical_cloud_init}".encode()
    digest = hashlib.sha256(payload).hexdigest()[:_KEY_HASH_HEX_WIDTH]
    return f"v1-{profile.key}-{digest}"


def _truncate_label(value: str) -> str:
    """Cap a label value at the provider's ≤63-char limit (D5)."""
    return value[:_LABEL_VALUE_MAX]


def cache_labels(
    profile: DistroProfile,
    arch: str,
    key: str,
    source_fp: str,
    run_token: str,
) -> dict[str, str]:
    """The full rich cache label set, from ONE function (D5).

    Emitting every cache label from a single place is the no-drift guarantee:
    the query index (``reap-images`` / ``run`` lookup) and the labels
    ``build-image`` applies can never diverge.

    ``source_fp`` is the base-image fingerprint (:func:`base_fingerprint`) — a
    64-hex sha is truncated to keep each value ≤63 chars.

    ``run_token`` is the build's **provenance** token (``vmlease-built``), NOT
    the ephemeral ``vmlease=<run-id>`` reap key: a persistent cache image must
    never carry the per-run reap label (the data-loss guard — a per-run reap
    would otherwise delete the cache). This function therefore emits **no**
    ``vmlease=`` label.
    """
    return {
        LABEL_PURPOSE: PURPOSE_IMAGE_CACHE,
        LABEL_CACHE_KEY: _truncate_label(key),
        LABEL_SCHEMA: SCHEMA_V1,
        LABEL_DISTRO: _truncate_label(profile.key),
        LABEL_ARCH: _truncate_label(arch),
        LABEL_SOURCE_FP: _truncate_label(source_fp),
        LABEL_BUILT: _truncate_label(run_token),
    }


def superseded(images: Iterable[Image], current_key: str) -> list[Image]:
    """One group's superseded images: those whose cache-key ≠ ``current_key`` (D10).

    Accept-(a): when no image in ``images`` carries ``current_key`` (the current
    image is absent — rolled, not yet rebuilt), *every* image is superseded.
    ``--older-than`` is the separate age-protection feature; this is pure key
    comparison. An image with no ``vmlease-cache-key`` label is treated as not
    matching (it is not the current key), so it is superseded.
    """
    return [img for img in images if img.labels.get(LABEL_CACHE_KEY) != current_key]


def _group_of(image: Image) -> tuple[str, str]:
    """The ``(distro, arch)`` group an image belongs to (its label tuple)."""
    return (image.labels.get(LABEL_DISTRO, ""), image.labels.get(LABEL_ARCH, ""))


def resolve_current_keys(
    images: Iterable[Image],
    profile_for: Callable[[str], DistroProfile],
    operator: str,
    deps: ResolveDeps,
    warn: Callable[[str], None],
) -> dict[tuple[str, str], str]:
    """Resolve each present ``(distro, arch)`` group's current key (D10).

    For every distinct ``(distro, arch)`` group present in ``images``, resolve
    that group's current content key (reusing :func:`base_fingerprint` +
    :func:`content_key` through the injected ``deps`` seam) and return the
    ``{(distro, arch): current_key}`` mapping.

    **Fail-safe** (D10): a group whose current key cannot be resolved — e.g. a
    rescue-write mirror is down and ``resolve_and_verify`` raises — is **skipped
    and warned**, never treated as fully superseded. The group is simply absent
    from the returned mapping (the caller keeps every image in an unmapped
    group), so an unverifiable group is never deleted. Resolvable groups are
    still resolved (partial success). ``profile_for`` maps a distro key to its
    profile (e.g. ``vmlease.distro.get_profile``); ``warn`` is the injected
    sink for the skip notice (no I/O in this module).
    """
    groups: dict[tuple[str, str], str] = {}
    seen: set[tuple[str, str]] = set()
    for image in images:
        group = _group_of(image)
        if group in seen:
            continue
        seen.add(group)
        distro_key, arch = group
        try:
            profile = profile_for(distro_key)
            groups[group] = content_key(profile, arch, operator, deps)
        except Exception as exc:  # fail-safe: any resolve failure keeps the group (never delete)
            warn(
                f"cannot resolve current cache key for group "
                f"(distro={distro_key!r}, arch={arch!r}): {exc}; keeping its images"
            )
    return groups
