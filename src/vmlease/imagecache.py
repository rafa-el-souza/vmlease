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

from vmlease.capabilities import canonical_requires
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
# The distro VERSION (e.g. ``"22.04"``, or ``"rolling"`` for a rolling family) —
# a supersession-group discriminant so a sibling version is never pruned (D-9).
# ``LABEL_DISTRO`` stays family-only.
LABEL_VERSION = "vmlease-version"
LABEL_ARCH = "vmlease-arch"
LABEL_SOURCE_FP = "vmlease-source-fp"
LABEL_BUILT = "vmlease-built"
# The supersession-group discriminant for the required-capability set (D-D
# correction / D13.8). A short stable HASH of the sorted requires set — NOT the
# raw joined list, which would collapse distinct sets on the 63-char-bounded
# label once a 2nd capability exists, re-opening the reap data-loss bug. With
# this on the group identity, `reap-images` groups by (distro, arch, requires)
# so a docker variant never supersedes the docker-less one.
LABEL_REQUIRES_HASH = "vmlease-requires-hash"
# The raw canonical (sorted+deduped, NUL-joined) required-capability set. The hash
# above is the collision-safe GROUP IDENTITY; this echo is the recompute input —
# `resolve_current_keys` reads it to re-render the group's current cloud-init
# (the hash is one-way, so the set itself must be recoverable to recompute the
# key). For v1's single capability the joined value is far inside the 63-char
# label bound; if a future set ever overflowed, only the recompute echo (never
# the group identity, which is the hash) would be affected.
LABEL_REQUIRES = "vmlease-requires"

# Fixed label values.
PURPOSE_IMAGE_CACHE = "image-cache"
SCHEMA_V1 = "v1"

# Provider label values are capped at 63 chars; a 64-hex sha source-fp would
# overflow, so values are truncated to this width (matching the key's hash
# width — a sha folds to its first 32 hex either way).
_LABEL_VALUE_MAX = 63

# Width of the required-capabilities group hash (D13.8): the first 16 lowercase-hex
# chars of the SHA-256 over the canonicalized requires set.
_REQUIRES_HASH_HEX_WIDTH = 16


def requires_hash(requires: tuple[str, ...]) -> str:
    """The short stable group hash of a required-capability set (D13.8).

    ``sha256("\\0".join(sorted(set(requires)))).hexdigest()[:16]`` — the
    supersession-group discriminant carried on the cache label
    (:data:`LABEL_REQUIRES_HASH`). Sorted+deduped so order can't perturb the
    hash; the ``\\0`` join keeps adjacent-capability boundaries unambiguous; the
    16-hex truncation stays well within the 63-char label bound and is
    collision-safe for the small capability vocabulary. Distinct from
    :func:`vmlease.capabilities.canonical_requires` (which normalizes the tuple
    threaded into the render); this folds that set to the group label.
    """
    payload = "\0".join(sorted(set(requires))).encode()
    return hashlib.sha256(payload).hexdigest()[:_REQUIRES_HASH_HEX_WIDTH]


def base_fingerprint(profile: DistroProfile, arch: str, deps: ResolveDeps) -> str:
    """Resolve the base-image fingerprint for ``(profile, arch)`` (D4).

    The single per-run-variable hashed input. Two sources, one uniform call:

    - **native** (``not profile.needs_rescue_write``) → the arch-blind provider
      image slug ``profile.image``; the probe-confirmed Hetzner system
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
        return profile.image
    # mypy: needs_rescue_write is True ⇒ rescue_image is not None.
    assert profile.rescue_image is not None
    return profile.rescue_image.resolve_and_verify(deps).expected_sha256


def content_key(
    profile: DistroProfile,
    arch: str,
    operator: str,
    requires: tuple[str, ...],
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
      ``operator``, the pinned :data:`_CACHE_KEY_CANONICAL_PUBKEY` sentinel, and
      the host's ``requires`` (so docker vs docker-less variants render distinct
      cloud-init and hence distinct keys — D4/D-D), with the per-run pubkey
      normalized out while packages / extra_setup / capability recipes /
      system_update / finalize / template are all captured.

    ``requires`` is threaded into the render (NOT hashed as a separate term):
    D-D keeps the key a single function of the *real* cloud-init, and the render
    already canonicalizes ``requires`` order, so ``["a","b"]`` and ``["b","a"]``
    fold to the same key.

    Hash = **SHA-256**, lowercase hex, first 32 chars. The ``\\0`` separators
    prevent boundary ambiguity (so ``"a" + "bc"`` ≠ ``"ab" + "c"``). Inputs are
    UTF-8 bytes.
    """
    base_fp = base_fingerprint(profile, arch, deps)
    return content_key_from_base_fp(base_fp, profile, arch, operator, requires)


def content_key_from_base_fp(
    base_fp: str,
    profile: DistroProfile,
    arch: str,
    operator: str,
    requires: tuple[str, ...],
) -> str:
    """The cache content key given an **already-resolved** base fingerprint.

    The pure key derivation half of :func:`content_key`, split out so a caller that
    has *already* paid the (network, gpg) :func:`base_fingerprint` resolve can
    derive the key without resolving twice. ``content_key`` is exactly
    ``content_key_from_base_fp(base_fingerprint(profile, arch, deps), …)`` — one
    implementation, so the two can never hash different bytes.

    Hashes the SAME payload as :func:`content_key`: ``arch \\0 base_fp \\0
    canonical_cloud_init`` (the canonical render uses the real ``operator``, the
    pinned :data:`_CACHE_KEY_CANONICAL_PUBKEY`, and the host's ``requires`` — so
    the docker vs docker-less variant renders distinct bytes), SHA-256, lowercase
    hex, first 32.
    """
    canonical_cloud_init = render_cloudinit(
        profile, operator, _CACHE_KEY_CANONICAL_PUBKEY, requires
    )
    payload = f"{arch}\0{base_fp}\0{canonical_cloud_init}".encode()
    digest = hashlib.sha256(payload).hexdigest()[:_KEY_HASH_HEX_WIDTH]
    return f"v1-{profile.family}-{digest}"


def _truncate_label(value: str) -> str:
    """Cap a label value at the provider's ≤63-char limit (D5)."""
    return value[:_LABEL_VALUE_MAX]


def cache_labels(
    profile: DistroProfile,
    arch: str,
    key: str,
    source_fp: str,
    run_token: str,
    requires: tuple[str, ...],
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

    ``requires`` is folded to its short group hash (:func:`requires_hash`) and
    emitted as :data:`LABEL_REQUIRES_HASH` so the supersession group identity is
    ``(family, version, arch, requires-hash)`` — a docker variant never supersedes
    the docker-less one, and a sibling version is never pruned (D-9 / D-D).
    """
    return {
        LABEL_PURPOSE: PURPOSE_IMAGE_CACHE,
        LABEL_CACHE_KEY: _truncate_label(key),
        LABEL_SCHEMA: SCHEMA_V1,
        LABEL_DISTRO: _truncate_label(profile.family),
        LABEL_VERSION: _truncate_label(profile.version),
        LABEL_ARCH: _truncate_label(arch),
        LABEL_SOURCE_FP: _truncate_label(source_fp),
        LABEL_BUILT: _truncate_label(run_token),
        LABEL_REQUIRES_HASH: _truncate_label(requires_hash(requires)),
        LABEL_REQUIRES: _truncate_label("\0".join(canonical_requires(requires))),
    }


def _requires_of(image: Image) -> tuple[str, ...]:
    """The canonical required-capability set recorded on an image (recompute echo).

    Reads :data:`LABEL_REQUIRES` (NUL-joined canonical set), returning ``()`` for
    a docker-less / pre-``requires`` image with no label. Used by
    :func:`resolve_current_keys` to re-render a group's current cloud-init (the
    group's :data:`LABEL_REQUIRES_HASH` identity is one-way, so the set itself is
    recovered from this echo).
    """
    raw = image.labels.get(LABEL_REQUIRES, "")
    return tuple(part for part in raw.split("\0") if part)


def superseded(images: Iterable[Image], current_key: str) -> list[Image]:
    """One group's superseded images: those whose cache-key ≠ ``current_key`` (D10).

    Accept-(a): when no image in ``images`` carries ``current_key`` (the current
    image is absent — rolled, not yet rebuilt), *every* image is superseded.
    ``--older-than`` is the separate age-protection feature; this is pure key
    comparison. An image with no ``vmlease-cache-key`` label is treated as not
    matching (it is not the current key), so it is superseded.
    """
    return [img for img in images if img.labels.get(LABEL_CACHE_KEY) != current_key]


def group_of(image: Image) -> tuple[str, str, str, str]:
    """The ``(family, version, arch, requires-hash)`` group an image belongs to (D-9 / D-D).

    Both the distro **version** (:data:`LABEL_VERSION`) and the required-capability
    set (the collision-safe :data:`LABEL_REQUIRES_HASH`) are part of the
    supersession-group identity, so a sibling version — and a docker vs docker-less
    image of the same family+version+arch — are distinct groups and never supersede
    one another. ``LABEL_DISTRO`` stays family-only.
    """
    return (
        image.labels.get(LABEL_DISTRO, ""),
        image.labels.get(LABEL_VERSION, ""),
        image.labels.get(LABEL_ARCH, ""),
        image.labels.get(LABEL_REQUIRES_HASH, ""),
    )


def resolve_current_keys(
    images: Iterable[Image],
    profile_for: Callable[[str, str], DistroProfile],
    operator: str,
    deps: ResolveDeps,
    warn: Callable[[str], None],
) -> dict[tuple[str, str, str, str], str]:
    """Resolve each present ``(family, version, arch, requires-hash)`` group's key (D-9/D10).

    For every distinct ``(family, version, arch, requires-hash)`` group present in
    ``images``, resolve that group's current content key (reusing
    :func:`base_fingerprint` + :func:`content_key` through the injected ``deps``
    seam, with the group's recorded ``requires`` recovered from
    :func:`_requires_of`) and return the ``{(family, version, arch, requires-hash):
    current_key}`` mapping. Because both ``version`` and ``requires`` are part of
    the group identity, a sibling version — and a docker vs docker-less group of one
    family+version+arch — each resolve their own current key; neither supersedes the
    other (the reap data-loss guard).

    **Fail-safe** (D10): a group whose current key cannot be resolved — e.g. a
    rescue-write mirror is down and ``resolve_and_verify`` raises, OR an OLD image
    carries no :data:`LABEL_VERSION` so ``version=""`` and ``profile_for(family, "")``
    raises — is **skipped and warned**, never treated as fully superseded. The group
    is simply absent from the returned mapping (the caller keeps every image in an
    unmapped group), so an unverifiable group is never deleted. Resolvable groups are
    still resolved (partial success). ``profile_for`` maps a ``(family, version)`` to
    its profile (e.g. ``vmlease.distro.get_profile``); ``warn`` is the injected
    sink for the skip notice (no I/O in this module).
    """
    groups: dict[tuple[str, str, str, str], str] = {}
    seen: set[tuple[str, str, str, str]] = set()
    for image in images:
        group = group_of(image)
        if group in seen:
            continue
        seen.add(group)
        family, version, arch, _requires_hash = group
        requires = _requires_of(image)
        try:
            profile = profile_for(family, version)
            groups[group] = content_key(profile, arch, operator, requires, deps)
        except Exception as exc:  # fail-safe: any resolve failure keeps the group (never delete)
            warn(
                f"cannot resolve current cache key for group "
                f"(family={family!r}, version={version!r}, arch={arch!r}, "
                f"requires={list(requires)!r}): {exc}; keeping its images"
            )
    return groups
