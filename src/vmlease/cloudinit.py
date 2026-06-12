"""Render the cloud-init script for a host from its :class:`DistroProfile`.

Two-stage, logic-free assembly (all branching is Python / profile data, never
in-template — see :mod:`vmlease.templating`):

1. render the per-package-manager install fragment (``install.<mgr>.tmpl``)
   from the profile's packages + docker-repo slug + extra setup;
2. render the base ``cloudinit.sh.tmpl``, injecting the install fragment plus
   the operator name and the throwaway public key.

Templates ship as package data under ``vmlease/templates/`` and are read via
:mod:`importlib.resources`. The render seam fails loud on a slot/key mismatch,
so a drifted template or profile is caught here, not on a half-built host.
"""

from __future__ import annotations

from importlib import resources
from typing import TYPE_CHECKING

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
# journald and dbus (which key state off it) across restored hosts (F-009). We
# truncate it (NOT delete-and-leave-absent): an empty /etc/machine-id signals
# systemd to regenerate a fresh, unique id on the next boot. The dbus copy is
# cleared too so it is re-derived from the new machine-id. ``;`` (not ``&&``)
# separates the two so a missing dbus file can never fail sysprep (``rm -f`` is
# already no-fail; the ``;`` keeps the truncate result from gating it).
SYSPREP_COMMAND: str = "sudo truncate -s 0 /etc/machine-id ; sudo rm -f /var/lib/dbus/machine-id"


class CloudInitError(ValueError):
    """A profile cannot be rendered (no install template for its manager, …)."""


def _read_template(name: str) -> str:
    """Read a packaged template file as text (raises if absent)."""
    return resources.files(_TEMPLATES_PKG).joinpath(name).read_text(encoding="utf-8")


def _render_subset(template_text: str, candidates: dict[str, str]) -> str:
    """Render filling ONLY the slots the template references.

    The install templates differ in which slots they use (apt uses
    ``docker_repo_slug``; dnf/pacman do not). The render seam is strict in both
    directions, so we pass exactly the referenced subset of ``candidates`` —
    making "this template uses these slots" explicit rather than forcing every
    template to reference every key.
    """
    referenced = find_slots(template_text)
    return render(template_text, {k: candidates[k] for k in referenced if k in candidates})


def render_install_block(profile: DistroProfile) -> str:
    """Render the per-package-manager install fragment for ``profile``."""
    template_name = f"install.{profile.package_manager}.tmpl"
    try:
        template_text = _read_template(template_name)
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise CloudInitError(
            f"no install template {template_name!r} for package manager "
            f"{profile.package_manager!r} (distro {profile.key!r})"
        ) from exc
    candidates = {
        "packages": " ".join(profile.packages),
        "docker_repo_slug": profile.docker_repo_slug,
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


def render_cloudinit(profile: DistroProfile, operator: str, operator_pubkey: str) -> str:
    """Render the full cloud-init script for ``profile``.

    ``operator`` is the non-root account the battery runs as; ``operator_pubkey``
    is the throwaway probe public key authorized for it.
    """
    install_block = render_install_block(profile)
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
