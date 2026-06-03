"""Per-distro provisioning profiles — cloud-init + package knowledge.

The durable home for the per-distro host-prep knowledge validated on real hosts:
the package sets, docker install, Arch static bundle + ``nf_tables``, Debian
``uidmap``/``gnupg``. The runner uses a profile to (a) pick the provider image
slug and (b) render the cloud-init that installs deps + creates the non-root
operator before the battery runs.

Project-agnostic: a profile describes *how to prepare a distro*, independent of
any particular battery. A consumer selects profiles by key in its matrix.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from vmlease.archimage import DEFAULT_ARCH_KEY_FINGERPRINT
from vmlease.rescue_image import ArchRescueImageSpec, RescueImageSpec


@dataclass(frozen=True)
class DistroProfile:
    """How to provision + prepare one distro family.

    Two axes are kept distinct (the lesson from apt serving both debian and
    ubuntu): the **package manager** is the install *mechanics* (one template
    per manager — ``install.<mgr>.tmpl``), and the **distro** is the *data* (this
    profile). Every per-distro difference beyond the shared mechanics is an
    explicit field here, NOT a forked template — so a new distro under an
    existing manager is a new profile, not a new template.

    Attributes:
        key: Stable selector (e.g. ``"ubuntu"``, ``"debian"``, ``"fedora"``, ``"arch"``).
        default_image: Provider image slug for this distro.
        package_manager: ``"apt" | "dnf" | "pacman"`` — selects the install template.
        packages: Base packages to install (the rootless-docker prerequisite set).
        docker_repo_slug: The ``download.docker.com/linux/<slug>`` path component —
            ``"debian"`` vs ``"ubuntu"`` diverge here under the SAME apt mechanics;
            ``""`` when the manager template does not use a docker repo.
        extra_setup: Profile-supplied shell steps appended to the install block
            for genuinely per-distro work that is not a package install (e.g.
            Arch's ``modprobe nf_tables`` + static docker bundle). Empty for most.
        system_update_override: When set, the exact system-refresh command for
            this distro, overriding the package-manager default
            (:func:`system_update_command`). Empty for most (the manager default
            covers every distro under that manager).
        rescue_image: When set, this distro has no native Hetzner image and is
            built by a rescue-write transform (:mod:`vmlease.archbuild`):
            ``default_image`` is the cheap BASE host to provision, and this is the
            injected :class:`~vmlease.rescue_image.RescueImageSpec` that resolves +
            trust-gates the cloud image to write onto its disk (Arch:
            :class:`~vmlease.rescue_image.ArchRescueImageSpec`; a pinned golden
            image: :class:`~vmlease.rescue_image.GoldenRescueImageSpec`). ``None``
            for native-image distros (the common case).
        notes: Free-text provenance / per-distro gotchas (for the results report).
    """

    key: str
    default_image: str
    package_manager: str
    packages: tuple[str, ...]
    docker_repo_slug: str = ""
    extra_setup: tuple[str, ...] = ()
    system_update_override: str = ""
    rescue_image: RescueImageSpec | None = None
    notes: str = ""

    @property
    def needs_rescue_write(self) -> bool:
        """``True`` iff this distro is provisioned via a rescue-write transform."""
        return self.rescue_image is not None


# Package sets validated on real hosts — the rootless-docker prerequisites a probe
# battery assumes are present before it probes what the *operator* can do. Exposed
# as a read-only ``Mapping`` (``MappingProxyType`` below) — it is a static registry
# populated once at import; a runtime mutation would be a bug, so the type system +
# runtime both forbid it (immutability rule; global-state hygiene).
_PROFILES: dict[str, DistroProfile] = {
    "ubuntu": DistroProfile(
        key="ubuntu",
        default_image="ubuntu-24.04",
        package_manager="apt",
        packages=(
            "systemd-container", "acl", "rsync", "fuse-overlayfs", "e2fsprogs",
            "procps", "sudo", "curl", "ca-certificates", "gnupg", "uidmap",
        ),
        docker_repo_slug="ubuntu",
        notes="docker-ce via the ubuntu repo path (NOT debian); uidmap is "
        "a separate pkg the rootless setuptool needs.",
    ),
    "debian": DistroProfile(
        key="debian",
        default_image="debian-13",
        package_manager="apt",
        packages=(
            "systemd-container", "acl", "rsync", "fuse-overlayfs", "e2fsprogs",
            "procps", "sudo", "curl", "ca-certificates", "gnupg", "uidmap",
        ),
        docker_repo_slug="debian",
        notes="same apt mechanics as ubuntu but the debian docker repo path; "
        "uidmap is a separate pkg.",
    ),
    "fedora": DistroProfile(
        key="fedora",
        default_image="fedora-43",
        package_manager="dnf",
        packages=(
            "systemd-container", "acl", "rsync", "slirp4netns",
            "fuse-overlayfs", "e2fsprogs", "procps-ng", "shadow-utils", "curl",
            "dnf-plugins-core",
        ),
        extra_setup=(
            # the rootless setuptool's iptables preflight needs the legacy
            # `ip_tables` module loaded; the minimal Fedora cloud image does not
            # auto-load it (found on a real host: 'Missing system requirements …
            # modprobe ip_tables', 2026-06-01).
            "modprobe ip_tables || true",
            "echo ip_tables > /etc/modules-load.d/zz-vmlease-ip_tables.conf",
        ),
        notes="docker-ce-rootless-extras via the fedora docker-ce repo; newuidmap "
        "via shadow-utils; needs modprobe ip_tables for the rootless iptables preflight.",
    ),
    "arch": DistroProfile(
        key="arch",
        # No native Hetzner Arch image: provision a cheap debian-13 base, then
        # rescue-write the verified Arch cloudimg onto its disk (archbuild). The
        # cloudimg ships cloud-init -> reads the hetzner datasource -> applies the
        # SAME --user-data prep as every other distro (verified on a real host 2026-06-01).
        default_image="debian-13",
        rescue_image=ArchRescueImageSpec(fingerprint=DEFAULT_ARCH_KEY_FINGERPRINT),
        package_manager="pacman",
        packages=(
            "systemd", "rsync", "acl", "e2fsprogs", "procps-ng", "shadow",
            "docker", "docker-buildx", "docker-compose", "rootlesskit",
            "slirp4netns", "fuse-overlayfs", "git", "base-devel", "curl",
        ),
        extra_setup=(
            # (1) rootless setuptool's iptables preflight needs nf_tables/ip_tables;
            # a fresh minimal Arch VM does not auto-load them (found on a real host).
            "modprobe nf_tables ip_tables || true",
            "echo nf_tables > /etc/modules-load.d/zz-vmlease-nf_tables.conf",
            "echo ip_tables >> /etc/modules-load.d/zz-vmlease-nf_tables.conf",
            # (2) Arch's `docker` pacman package does NOT ship
            # dockerd-rootless-setuptool.sh. Lay the upstream STATIC docker +
            # rootless-extras bundle onto /usr/local/bin (first on PATH) — the exact
            # set the upstream installer produces. Guarded so a pre-staged bundle is
            # not re-downloaded. (Found on a real host: 'dockerd-rootless-setuptool.sh:
            # command not found', 2026-06-01.)
            "if ! test -x /usr/local/bin/dockerd-rootless-setuptool.sh; then "
            "ver=29.5.1; m=$(uname -m); d=$(mktemp -d); "
            "curl -fsSL https://download.docker.com/linux/static/stable/${m}/docker-${ver}.tgz -o $d/docker.tgz; "
            "curl -fsSL https://download.docker.com/linux/static/stable/${m}/docker-rootless-extras-${ver}.tgz -o $d/extras.tgz; "
            "tar -C $d -xzf $d/docker.tgz; tar -C $d -xzf $d/extras.tgz; "
            "install -m0755 $d/docker/* /usr/local/bin/; "
            "install -m0755 $d/docker-rootless-extras/* /usr/local/bin/; "
            "rm -rf $d; fi",
        ),
        notes="needs modprobe nf_tables+ip_tables (rootless iptables preflight); the "
        "`docker` pacman pkg has NO rootless setuptool, so the static docker + "
        "rootless-extras bundle is laid on /usr/local/bin. "
        "Arch image is built by rescue-write (no native Hetzner image).",
    ),
}

# The public, read-only view: any `PROFILES[k] = ...` / `del` raises TypeError.
PROFILES: Mapping[str, DistroProfile] = MappingProxyType(_PROFILES)

# The default distro matrix.
DEFAULT_DISTRO_KEYS: tuple[str, ...] = ("ubuntu", "debian", "fedora", "arch")

# System-refresh command per package manager (single source — derived from the
# manager, so a fresh-baseline upgrade covers every distro under that manager,
# not just ubuntu/debian). A current kernel/systemd matters for many probes; the
# apt form is validated on real hosts. A profile may override via
# ``system_update_override`` for a distro that needs something special.
_SYSTEM_UPDATE_BY_MANAGER: Mapping[str, str] = MappingProxyType({
    "apt": "apt-get update -qq && apt-get upgrade -y -qq",
    "dnf": "dnf -y upgrade",
    "pacman": "pacman -Syu --noconfirm",
})


def system_update_command(profile: DistroProfile) -> str:
    """The system-refresh command for ``profile`` (override, else manager default).

    Raises :class:`UnknownPackageManagerError` if the manager has no known
    refresh command and the profile supplies no override.
    """
    if profile.system_update_override:
        return profile.system_update_override
    try:
        return _SYSTEM_UPDATE_BY_MANAGER[profile.package_manager]
    except KeyError as exc:
        raise UnknownPackageManagerError(
            f"no system-update command for package manager "
            f"{profile.package_manager!r} (distro {profile.key!r}); set "
            f"system_update_override on the profile"
        ) from exc


class UnknownDistroError(KeyError):
    """A matrix referenced a distro key with no :class:`DistroProfile`."""


class UnknownPackageManagerError(KeyError):
    """A profile's package manager has no known system-update command + no override."""


def get_profile(key: str) -> DistroProfile:
    """Return the profile for ``key`` or raise :class:`UnknownDistroError`."""
    try:
        return PROFILES[key]
    except KeyError as exc:
        raise UnknownDistroError(
            f"no distro profile for {key!r}; known: {sorted(PROFILES)}"
        ) from exc
