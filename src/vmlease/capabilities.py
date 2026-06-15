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


# The docker recipe SETUP fragments — a **byte-identical move** (D13.3) of the
# apt/dnf docker install blocks (formerly the ``if ! command -v
# dockerd-rootless-setuptool.sh`` guards in ``install.{apt,dnf}.tmpl``) and Arch's
# static-bundle ``extra_setup`` block (formerly in ``distro.arch.extra_setup``).
# Each fragment is rendered verbatim into the cloud-init ``@@capability_setup@@``
# slot (the renderer fills any recipe-local ``@@…@@`` slot — apt's
# ``@@docker_repo_slug@@`` — from the host's profile so the debian↔ubuntu repo
# path is preserved). The bytes here, once substituted, are identical to the
# pre-change rendered docker bytes — proven by a byte-identity test (3.5b).
_DOCKER_SETUP_APT = (
    "# docker-ce via the distro-correct repo path: @@docker_repo_slug@@ (debian != ubuntu).\n"
    "  if ! command -v dockerd-rootless-setuptool.sh >/dev/null 2>&1; then\n"
    "    install -m 0755 -d /etc/apt/keyrings\n"
    '    curl -fsSL "https://download.docker.com/linux/@@docker_repo_slug@@/gpg" \\\n'
    "      | gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg\n"
    "    chmod a+r /etc/apt/keyrings/docker.gpg\n"
    '    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] '
    'https://download.docker.com/linux/@@docker_repo_slug@@ '
    '$(. /etc/os-release && echo "$VERSION_CODENAME") stable" \\\n'
    "      > /etc/apt/sources.list.d/docker.list\n"
    "    apt-get update\n"
    "    apt-get install -y docker-ce docker-ce-cli containerd.io docker-ce-rootless-extras "
    "docker-compose-plugin docker-buildx-plugin\n"
    "  fi"
)

_DOCKER_SETUP_DNF = (
    "if ! command -v dockerd-rootless-setuptool.sh >/dev/null 2>&1; then\n"
    "    dnf -y config-manager addrepo --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo \\\n"
    "      || dnf -y config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo \\\n"
    "      || curl -fsSL https://download.docker.com/linux/fedora/docker-ce.repo > /etc/yum.repos.d/docker-ce.repo\n"
    "    dnf -y install docker-ce docker-ce-cli containerd.io docker-ce-rootless-extras "
    "docker-compose-plugin docker-buildx-plugin\n"
    "  fi"
)

# Arch's `docker` pacman package does NOT ship dockerd-rootless-setuptool.sh. Lay
# the upstream STATIC docker + rootless-extras bundle onto /usr/local/bin (first on
# PATH) — the exact set the upstream installer produces. Guarded so a pre-staged
# bundle is not re-downloaded. (Found on a real host: 'dockerd-rootless-setuptool.sh:
# command not found', 2026-06-01.)
_DOCKER_SETUP_PACMAN = (
    "if ! test -x /usr/local/bin/dockerd-rootless-setuptool.sh; then "
    "ver=29.5.1; m=$(uname -m); d=$(mktemp -d); "
    "curl -fsSL https://download.docker.com/linux/static/stable/${m}/docker-${ver}.tgz -o $d/docker.tgz; "
    "curl -fsSL https://download.docker.com/linux/static/stable/${m}/docker-rootless-extras-${ver}.tgz -o $d/extras.tgz; "
    "tar -C $d -xzf $d/docker.tgz; tar -C $d -xzf $d/extras.tgz; "
    "install -m0755 $d/docker/* /usr/local/bin/; "
    "install -m0755 $d/docker-rootless-extras/* /usr/local/bin/; "
    "rm -rf $d; fi"
)

# The docker packages installed on pacman via the host's main install line (apt/dnf
# install docker-ce *inside* their guarded setup blocks above, so their recipes
# carry no top-line packages — preserving the pre-change rendered bytes + the
# idempotency guard; only pacman folds its docker packages into ``@@packages@@``).
_DOCKER_PACKAGES_PACMAN = (
    "docker", "docker-buildx", "docker-compose", "rootlesskit",
    "slirp4netns", "fuse-overlayfs",
)

# The capability registry: ``capability → package_manager → recipe``. Both levels
# are frozen (``MappingProxyType``) — a static registry populated once at import;
# a runtime mutation would be a bug, so the type system + runtime both forbid it
# (immutability rule; global-state hygiene), mirroring ``distro.PROFILES``.
#
# Docker is the v1 vocabulary (the only capability). The recipe content is the
# byte-identical move of the apt/dnf docker install blocks + Arch's static bundle +
# Arch's docker package set out of the install templates / profile data (D13.3).
_CAPABILITIES: dict[str, dict[str, CapabilityRecipe]] = {
    "docker": {
        "apt": CapabilityRecipe(setup=(_DOCKER_SETUP_APT,)),
        "dnf": CapabilityRecipe(setup=(_DOCKER_SETUP_DNF,)),
        "pacman": CapabilityRecipe(
            packages=_DOCKER_PACKAGES_PACMAN, setup=(_DOCKER_SETUP_PACMAN,)
        ),
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
