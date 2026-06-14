"""vmlease-provided capability recipes — opt-in host capabilities as data.

A *capability* (e.g. ``docker``) is a vmlease-provided host feature a battery
opts into via ``requires = ["docker"]`` (default-off — no ``requires`` means no
capability). Each capability is realized per **package-manager** by an inert
:class:`CapabilityRecipe` — a package list plus an optional non-package setup
fragment — held in a two-level read-only registry keyed ``capability →
manager → recipe``.

This mirrors :mod:`vmlease.distro`'s ``PROFILES`` / ``get_profile`` /
``UnknownDistroError`` idiom (frozen data + a typed accessor), NOT the
``RescueImageSpec`` Protocol: a recipe is inert data with no call-time I/O and no
trust gate, and the per-manager differences are data, not algorithm — so a
Protocol would be empty ceremony. Keyed on *manager* (not distro) because
manager is the install *mechanics* axis (the same lesson ``distro.py`` records:
apt serves both debian and ubuntu).

``requires`` gates recipe inclusion uniformly across all distros; the install
templates no longer hardcode any capability. This module owns the **one**
``requires`` canonicalizer (:func:`canonical_requires`) reused by every consumer
(matrix lift, render, build-image, cache key), and the per-manager **install**
command map (the install counterpart to ``distro._SYSTEM_UPDATE_BY_MANAGER``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class CapabilityRecipe:
    """How one capability is realized on one package manager — inert data.

    Attributes:
        packages: The packages this capability installs on this manager
            (folded into the manager's install line in the rendered cloud-init).
            ``()`` for a capability that is pure non-package setup.
        setup: Non-package shell steps for this capability (a repo/keyring
            configuration, a static-bundle lay-down, an idempotency guard) — the
            steps rendered into the cloud-init ``@@capability_setup@@`` slot, in
            ``canonical_requires`` order across capabilities. ``()`` for a
            capability that is pure packages.
    """

    packages: tuple[str, ...] = ()
    setup: tuple[str, ...] = ()


# The capability registry: ``capability → package_manager → recipe``. Both levels
# are frozen (``MappingProxyType``) — a static registry populated once at import;
# a runtime mutation would be a bug, so the type system + runtime both forbid it
# (immutability rule; global-state hygiene), mirroring ``distro.PROFILES``.
#
# Docker is the v1 vocabulary (the only capability). The recipe CONTENT is the
# byte-identical move of the apt/dnf docker install blocks + Arch's static bundle
# out of the install templates / profile data — that move lands in a later task;
# the KEYS (docker for apt/dnf/pacman) are registered here so every consumer can
# import the registry and gate on ``requires`` now.
_CAPABILITIES: dict[str, dict[str, CapabilityRecipe]] = {
    "docker": {
        "apt": CapabilityRecipe(),
        "dnf": CapabilityRecipe(),
        "pacman": CapabilityRecipe(),
    },
}

# The public, read-only view (both levels): any assignment / ``del`` raises
# ``TypeError`` at either nesting level.
CAPABILITIES: Mapping[str, Mapping[str, CapabilityRecipe]] = MappingProxyType(
    {cap: MappingProxyType(by_mgr) for cap, by_mgr in _CAPABILITIES.items()}
)


# Per-manager **install** command — the install counterpart to
# ``distro._SYSTEM_UPDATE_BY_MANAGER``. A prep package pass resolves the host's
# manager here and appends the effective package list. ``-y`` / ``--noconfirm``
# keep the pass non-interactive (it runs unattended over SSH).
_INSTALL_BY_MANAGER: Mapping[str, str] = MappingProxyType({
    "apt": "apt-get install -y",
    "dnf": "dnf install -y",
    "pacman": "pacman -S --noconfirm",
})


class UnknownCapabilityError(KeyError):
    """A ``requires`` entry named a capability with no :class:`CapabilityRecipe`."""


class UnknownPackageManagerError(KeyError):
    """A package manager has no known install command."""


def known_capabilities() -> frozenset[str]:
    """The known capability vocabulary (the registry's top-level key set)."""
    return frozenset(CAPABILITIES)


def canonical_requires(requires: tuple[str, ...]) -> tuple[str, ...]:
    """The ONE ``requires`` canonicalizer: sorted + deduplicated.

    Every consumer (the matrix lift, the render, ``build-image --requires``, and
    the cache key) funnels its ``requires`` through this single function so order
    can never perturb a cache key (``["a", "b"]`` and ``["b", "a"]`` collapse to
    one tuple). Pure data — no validation against the registry (the loader does
    that); this only normalizes order + duplicates.
    """
    return tuple(sorted(set(requires)))


def recipe_for(
    capability: str,
    profile_manager: str,
    *,
    registry: Mapping[str, Mapping[str, CapabilityRecipe]] = CAPABILITIES,
) -> CapabilityRecipe:
    """The recipe for ``capability`` on ``profile_manager``, or raise a clear error.

    Raises :class:`UnknownCapabilityError` when the capability is unknown, and
    when the capability exists but has no recipe for this manager — the latter is
    a "capability unsupported on this host's manager" error, named (capability +
    manager) **before spend**. ``registry`` is injectable so a synthetic
    capability can exercise the no-recipe-for-this-manager branch in tests.
    """
    try:
        by_manager = registry[capability]
    except KeyError as exc:
        raise UnknownCapabilityError(
            f"unknown capability {capability!r}; known: {sorted(registry)}"
        ) from exc
    try:
        return by_manager[profile_manager]
    except KeyError as exc:
        raise UnknownCapabilityError(
            f"capability {capability!r} has no recipe for package manager "
            f"{profile_manager!r}; available: {sorted(by_manager)}"
        ) from exc


def install_command(package_manager: str) -> str:
    """The non-interactive install command for ``package_manager``, or raise.

    Raises :class:`UnknownPackageManagerError` when the manager has no known
    install command. The install counterpart to
    :func:`vmlease.distro.system_update_command`.
    """
    try:
        return _INSTALL_BY_MANAGER[package_manager]
    except KeyError as exc:
        raise UnknownPackageManagerError(
            f"no install command for package manager {package_manager!r}"
        ) from exc
