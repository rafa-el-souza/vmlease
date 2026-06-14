"""Render the cloud-init script for a host from its :class:`DistroProfile`.

Two-stage, logic-free assembly (all branching is Python / profile data, never
in-template — see :mod:`vmlease.templating`):

1. render the per-package-manager install fragment (``install.<mgr>.tmpl``)
   from the profile's packages + extra setup, plus the recipe of each capability
   the host requires (injected per package manager — see :mod:`vmlease.capabilities`);
2. render the base ``cloudinit.sh.tmpl``, injecting the install fragment plus
   the operator name and the throwaway public key.

Templates ship as package data under ``vmlease/templates/`` and are read via
:mod:`importlib.resources`. The render seam fails loud on a slot/key mismatch,
so a drifted template or profile is caught here, not on a half-built host.
"""

from __future__ import annotations

from importlib import resources
from typing import TYPE_CHECKING

from vmlease.capabilities import canonical_requires, recipe_for
from vmlease.distro import system_update_command
from vmlease.templating import find_slots, render

if TYPE_CHECKING:
    from vmlease.distro import DistroProfile

_TEMPLATES_PKG = "vmlease.templates"
_BASE_TEMPLATE = "cloudinit.sh.tmpl"
_MINIMAL_TEMPLATE = "cloudinit-minimal.sh.tmpl"

# Sysprep run over SSH as the (NOPASSWD-sudo) operator on the build host BEFORE
# poweroff/snapshot. A snapshot freezes /etc/machine-id into the image, so every
# host restored from it would otherwise share one machine-id — breaking systemd,
# journald and dbus (which key state off it) across restored hosts (F-009). TWO
# real-host findings (E-012 10.1, 2026-06-12) shape the exact command — both proven
# by build→restore-x3 runs:
#   1. ``sync`` is LOAD-BEARING. The reset write is not durable on the snapshot
#      unless explicitly synced: the fast poweroff→create-image otherwise captures
#      the stale on-disk block and the snapshot keeps the BUILDER's id, which every
#      restore inherits. (Dropping ``sync`` → 3 distinct hosts all read one shared
#      id; with it → 3 distinct ids.) A clean ACPI poweroff does NOT reliably flush
#      it in time.
#   2. The reset VALUE is systemd's golden-image sentinel ``uninitialized\n`` (a
#      PRESENT file that systemd regenerates a fresh, unique id from on first boot),
#      not ``truncate -s0`` (empty) or ``rm`` (absent).
# The dbus copy/symlink is cleared so it re-derives from the regenerated id.
SYSPREP_COMMAND: str = (
    "printf 'uninitialized\\n' | sudo tee /etc/machine-id >/dev/null"
    " ; sudo rm -f /var/lib/dbus/machine-id ; sync"
)


class CloudInitError(ValueError):
    """A profile cannot be rendered (no install template for its manager, …)."""


def _read_template(name: str) -> str:
    """Read a packaged template file as text (raises if absent)."""
    return resources.files(_TEMPLATES_PKG).joinpath(name).read_text(encoding="utf-8")


def _render_subset(template_text: str, candidates: dict[str, str]) -> str:
    """Render filling ONLY the slots the template references.

    The install templates differ in which slots they use, and a capability
    recipe's setup fragment may carry its own recipe-local slot (the apt docker
    recipe carries ``docker_repo_slug``; the others do not). The render seam is
    strict in both directions, so we pass exactly the referenced subset of
    ``candidates`` —
    making "this template uses these slots" explicit rather than forcing every
    template to reference every key.
    """
    referenced = find_slots(template_text)
    return render(template_text, {k: candidates[k] for k in referenced if k in candidates})


# Recipe-local slot values, derived from the host's profile. A capability
# recipe's ``setup`` fragment is verbatim shell that MAY carry its own
# ``@@name@@`` slots (the apt docker recipe carries ``@@docker_repo_slug@@`` so
# the debian↔ubuntu repo path is preserved per host). These are filled when the
# recipe is injected, BEFORE the install template's single substitution pass (the
# renderer does not re-substitute slots inside substituted values).
def _recipe_slot_values(profile: DistroProfile) -> dict[str, str]:
    """The values for recipe-local ``@@name@@`` slots on ``profile``.

    ``docker_repo_slug`` is the ``download.docker.com/linux/<slug>`` path
    component, which diverges debian↔ubuntu under the SAME apt mechanics; it
    equals the distro key (``"debian"`` / ``"ubuntu"``) and is derived here rather
    than stored on the profile (the profile no longer carries a docker-repo slug).
    """
    return {"docker_repo_slug": profile.key}


def _capability_injection(
    profile: DistroProfile, requires: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Resolve ``requires`` into (extra packages, rendered setup fragments).

    Each capability named in ``requires`` is resolved to its recipe for the
    host's package manager (raising a clear, before-spend error if the capability
    is unknown or unsupported on this manager), in **canonical** (sorted +
    deduplicated) order so ``requires`` ordering can never perturb the rendered
    bytes (and therefore the cache key). The setup fragments are pre-rendered
    against the recipe-local slot values so no ``@@name@@`` survives into the
    install template's substitution pass.
    """
    slot_values = _recipe_slot_values(profile)
    packages: list[str] = []
    setup: list[str] = []
    for capability in canonical_requires(requires):
        recipe = recipe_for(capability, profile.package_manager)
        packages.extend(recipe.packages)
        for fragment in recipe.setup:
            setup.append(_render_subset(fragment, slot_values))
    return tuple(packages), tuple(setup)


def render_install_block(profile: DistroProfile, requires: tuple[str, ...]) -> str:
    """Render the per-package-manager install fragment for ``profile``.

    ``requires`` is the host's **required** capability set (a required parameter —
    no caller may silently omit it and render the wrong variant). Each required
    capability's recipe for this manager is injected: its packages fold into the
    install line and its setup fragment renders into the ``@@capability_setup@@``
    slot, in canonical order. A host that requires nothing renders no
    optional-capability content (e.g. a docker-free install block).
    """
    template_name = f"install.{profile.package_manager}.tmpl"
    try:
        template_text = _read_template(template_name)
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise CloudInitError(
            f"no install template {template_name!r} for package manager "
            f"{profile.package_manager!r} (distro {profile.key!r})"
        ) from exc
    cap_packages, cap_setup = _capability_injection(profile, requires)
    candidates = {
        "packages": " ".join((*profile.packages, *cap_packages)),
        "capability_setup": "\n  ".join(cap_setup),
        "extra_setup": "\n  ".join(profile.extra_setup),
        "distro_key": profile.key,
    }
    missing = find_slots(template_text) - set(candidates)
    if missing:
        raise CloudInitError(
            f"install template {template_name!r} references slot(s) {sorted(missing)} "
            f"with no profile data"
        )
    return _render_subset(template_text, candidates)


def render_finalize_block(profile: DistroProfile) -> str:
    """Render the readiness-sentinel finalize fragment for ``profile``.

    Mirrors :func:`render_install_block`: the fragment file is chosen by the
    profile (``finalize.<slug>.tmpl``, slug from
    :attr:`~vmlease.distro.DistroProfile.finalize_fragment`), and only the slots
    it references are filled — native-image distros render the default
    (set-sentinel-in-place) fragment; rescue-write distros render the
    reboot-resume fragment. The selection is profile data, never a renderer
    ``if key == ...``.
    """
    template_name = f"finalize.{profile.finalize_fragment}.tmpl"
    try:
        template_text = _read_template(template_name)
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise CloudInitError(
            f"no finalize template {template_name!r} for finalize fragment "
            f"{profile.finalize_fragment!r} (distro {profile.key!r})"
        ) from exc
    candidates = {"distro_key": profile.key}
    missing = find_slots(template_text) - set(candidates)
    if missing:
        raise CloudInitError(
            f"finalize template {template_name!r} references slot(s) {sorted(missing)} "
            f"with no profile data"
        )
    return _render_subset(template_text, candidates)


def render_cloudinit(
    profile: DistroProfile,
    operator: str,
    operator_pubkey: str,
    requires: tuple[str, ...],
) -> str:
    """Render the full cloud-init script for ``profile``.

    ``operator`` is the non-root account the battery runs as; ``operator_pubkey``
    is the throwaway probe public key authorized for it. ``requires`` is the
    host's **required** capability set (a required parameter — so the identical
    render feeds both provisioning and the cache key, and the key varies with
    ``requires`` without a separate term). A host that requires nothing renders a
    capability-less (e.g. docker-free) cloud-init.
    """
    install_block = render_install_block(profile, requires)
    finalize = render_finalize_block(profile)
    base = _read_template(_BASE_TEMPLATE)
    candidates = {
        "operator": operator,
        "operator_pubkey": operator_pubkey.strip(),
        "install_block": install_block,
        "finalize": finalize,
        "system_update": system_update_command(profile),
        "distro_key": profile.key,
        "package_manager": profile.package_manager,
    }
    return _render_subset(base, candidates)


def render_minimal_cloudinit(operator: str, pubkey: str) -> str:
    """Render the restore-path (cache-hit) cloud-init for ``operator``.

    The operator account, sudoers, docker prerequisites and all prep are baked
    into the snapshot (F-009); cloud-init re-runs per-instance on a snapshot
    boot, so this re-authorizes ONLY the fresh per-run ``pubkey`` for the baked
    operator and re-asserts the ``/var/lib/vmlease-ready`` sentinel — no system
    update, package install, account creation or rescue-write. ``pubkey`` is the
    throwaway probe public key authorized for the restored host's run.
    """
    template_text = _read_template(_MINIMAL_TEMPLATE)
    candidates = {"operator": operator, "operator_pubkey": pubkey.strip()}
    return _render_subset(template_text, candidates)
