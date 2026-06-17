"""Rescue-write transform — turn a just-created host INTO a no-native-image distro.

Hetzner has no image for some distros (e.g. Arch), so such a host is provisioned
in two moves:

1. create a cheap base host (e.g. debian-13) WITH vmlease's normal cloud-init
   ``--user-data`` attached (Hetzner stores it on the server's metadata);
2. **rescue-write** a verified *cloudimg* qcow2 onto that host's disk and reboot
   — the cloudimg ships cloud-init, which reads Hetzner's ``hetzner`` datasource
   on first boot and applies the SAME ``--user-data`` prep + injected key as every
   other distro (verified on a real host 2026-06-01).

*Which* image, and *how it is trusted*, is the per-distro
:class:`~vmlease.rescue_image.RescueImageSpec` (Arch resolve-latest + pinned-GPG;
a pinned golden image, sha-only). This module owns the image-generic SPINE: the
spec's ``resolve_and_verify`` is the trust gate (run FIRST, before any mutation);
the verified source is then delivered to the rescue system — a
:class:`~vmlease.rescue_image.RemoteUrl` is curled on the rescue side, a
:class:`~vmlease.rescue_image.LocalFile` is scp-pushed to it — and the rescue side
re-verifies the sha before ``qemu-img convert``.

So there is **no snapshot** and no run-to-run state: the verify runs FRESH on
every build — verify-every-run, strictly stronger than
verify-once-into-a-reused-snapshot, and nothing billable lingers. The build cost
is one ~530 MiB fetch + a `qemu-img convert` + two reboots (~2-3 min), which is
acceptable for the security gain.

The pure argv builders + the rescue-write script renderer are unit-tested; the
live provider/ssh/reboot calls are the billable path, behind injected seams.
Every host-side step is a fail-loud probe (detect the disk, re-verify the sha)
rather than a hardcoded assumption — the recipe encoded here is the
real-host-proven one.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vmlease.rescue_image import LocalFile, RemoteUrl, ResolveDeps

if TYPE_CHECKING:
    from collections.abc import Callable

    from vmlease.distro import DistroProfile
    from vmlease.model import Host
    from vmlease.rescue_image import ResolvedRescueImage

# The fixed rescue-side path the image lands at — both delivery modes converge
# here: a ``RemoteUrl`` is curled to it; a ``LocalFile`` is scp-pushed to it by the
# orchestrator before the script runs. The sha re-check + convert read this path.
RESCUE_IMAGE_PATH = "/tmp/vmlease-rescue.qcow2"

# The on-rescue-system write script. Runs as root in the Hetzner rescue system,
# which a real-host run confirmed ships qemu-img + curl + sha256sum. It probes the disk
# device (never hardcodes sda vs nvme), re-verifies the sha after transfer, and
# converts qcow2 -> raw onto the disk. ``@@...@@`` slots are filled by
# :func:`render_rescue_script` (logic-free; shell ``$`` passes through). The
# ``@@fetch_cmd@@`` slot is the source-specific acquisition step: a ``curl`` for a
# remote URL, a presence check for an already-pushed local file.
_RESCUE_WRITE_SCRIPT = """#!/usr/bin/env bash
set -Eeuo pipefail

vmlease_rescue_write() {
  local image='@@image_path@@' expected_sha='@@expected_sha256@@'

  # probe the real root block device (NOT assumed sda vs nvme0n1).
  local disk
  disk="$(lsblk -dpno NAME,TYPE | awk '$2=="disk"{print $1; exit}')"
  [ -n "$disk" ] || { echo 'RESCUE_FAIL: no disk device found' >&2; exit 11; }
  echo "RESCUE_DISK=$disk"

  command -v qemu-img >/dev/null 2>&1 || { echo 'RESCUE_FAIL: qemu-img absent' >&2; exit 12; }

  # acquire the image at the fixed path (curl a remote URL, or assert a pushed
  # local file is present), then RE-verify the sha (defence in depth: the
  # operator side already verified sha[+signature]; the rescue side re-checks
  # integrity post-transfer, source-independent).
  @@fetch_cmd@@
  echo "${expected_sha}  ${image}" | sha256sum -c - \
    || { echo 'RESCUE_FAIL: sha256 mismatch on rescue side' >&2; exit 14; }

  qemu-img convert -O raw "$image" "$disk" \
    || { echo 'RESCUE_FAIL: qemu-img convert failed' >&2; exit 15; }
  sync
  echo 'RESCUE_WRITE_OK'
}

vmlease_rescue_write "$@"
"""

_RESCUE_PW_RE = re.compile(r"root password:\s*(\S+)")


class ArchBuildError(RuntimeError):
    """A rescue-write step failed (diagnostic message preserved)."""


# --------------------------------------------------------------------------- #
# Pure argv builders + parsers + renderer (unit-tested; no live calls)
# --------------------------------------------------------------------------- #
def build_enable_rescue_argv(server_id: str, ssh_key_name: str) -> list[str]:
    """argv for ``hcloud server enable-rescue`` (linux64, with the probe ssh key).

    NB: ``enable-rescue`` has NO ``--output`` flag (verified on a real host) — it
    prints plain text; the password is scraped by :func:`parse_rescue_password`.
    """
    return [
        "hcloud", "server", "enable-rescue",
        "--type", "linux64",
        "--ssh-key", ssh_key_name,
        server_id,
    ]


def build_disable_rescue_argv(server_id: str) -> list[str]:
    """argv for ``hcloud server disable-rescue`` (so the next reset boots the disk)."""
    return ["hcloud", "server", "disable-rescue", server_id]


def build_reset_argv(server_id: str) -> list[str]:
    """argv for a hard ``hcloud server reset`` (enter rescue / reboot the written disk)."""
    return ["hcloud", "server", "reset", server_id]


def parse_rescue_password(enable_rescue_stdout: str) -> str:
    """Scrape the rescue root password from the plain-text ``enable-rescue`` output.

    The line is ``Rescue enabled for server <id> with root password: <PWD>``.
    Raises :class:`ArchBuildError` if absent (the format is one of the things
    confirmed on a real host — fail loud, not silent).
    """
    m = _RESCUE_PW_RE.search(enable_rescue_stdout)
    if not m:
        raise ArchBuildError(
            f"no rescue root password in enable-rescue output (format may have "
            f"changed): {enable_rescue_stdout[:200]!r}"
        )
    return m.group(1)


def render_fetch_cmd(source: RemoteUrl | LocalFile) -> str:
    """The source-specific acquisition step for the on-rescue-system script.

    - :class:`RemoteUrl`: ``curl -fsSL <url> -o <path>`` (the rescue side fetches);
      the URL is :func:`shlex.quote`-escaped so it cannot break out of the script.
    - :class:`LocalFile`: a ``test -f <path>`` presence check (the orchestrator has
      already scp-pushed the file to that fixed path; the script only asserts it).

    Both leave the image at :data:`RESCUE_IMAGE_PATH`, where the sha re-check +
    ``qemu-img convert`` read it.
    """
    if isinstance(source, RemoteUrl):
        return (
            f"curl -fsSL {shlex.quote(source.url)} -o {RESCUE_IMAGE_PATH} "
            "|| { echo 'RESCUE_FAIL: download failed' >&2; exit 13; }"
        )
    return (
        f"test -f {RESCUE_IMAGE_PATH} "
        "|| { echo 'RESCUE_FAIL: pushed image absent' >&2; exit 13; }"
    )


def render_rescue_script(source: RemoteUrl | LocalFile, expected_sha256: str) -> str:
    """Render the on-rescue-system write script (logic-free slot fill).

    The ``source`` drives only the rendered ``@@fetch_cmd@@`` acquisition step; the
    disk probe, sha re-check, and convert are source-independent.
    """
    from vmlease.templating import render

    return render(
        _RESCUE_WRITE_SCRIPT,
        {
            "fetch_cmd": render_fetch_cmd(source),
            "image_path": RESCUE_IMAGE_PATH,
            "expected_sha256": expected_sha256,
        },
    )


# --------------------------------------------------------------------------- #
# The live rescue-write orchestration (billable; behind injected seams)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RescueWriteDeps:
    """Injected dependencies for :func:`rescue_write_host` (the live path).

    Each is a seam so the orchestration is unit-testable without hcloud/ssh/network:

    - ``resolve_deps``: the IO seams (:class:`~vmlease.rescue_image.ResolveDeps`)
      the profile's :class:`~vmlease.rescue_image.RescueImageSpec` uses to resolve
      + verify its image. The spec call is the **trust gate** — it runs FIRST,
      before any host mutation, so an unauthentic image (bad sha, or bad signature
      for a signed spec) aborts the build before a single billable/destructive
      step. Raising there is the fail-closed path.
    - ``cli``: run an ``hcloud`` argv -> ``(rc, stdout, stderr)`` (provider CLI).
    - ``ssh_root``: run a shell script on the rescue/host as root over ssh,
      returning ``(exit_code, combined_output)``.
    - ``wait_rescue_ready``: bounded poll until the host is in the rescue system
      (hostname=='rescue'). The post-write target-OS readiness is NOT here — it
      is owned by the probe phase's operator-readiness wait (different principal /
      key), so the rescue-write only ISSUES the final reset.
    - ``push_to_rescue``: scp a LOCAL image file to the rescue system at the fixed
      rescue-side path (``ip, local_path, remote_path``) — used only when the
      resolved source is a :class:`~vmlease.rescue_image.LocalFile`; a
      :class:`~vmlease.rescue_image.RemoteUrl` is curled by the script instead.
    """

    resolve_deps: ResolveDeps
    cli: Callable[[list[str]], tuple[int, str, str]]
    ssh_root: Callable[[str, str], tuple[int, str]]
    wait_rescue_ready: Callable[[str], None]
    push_to_rescue: Callable[[str, Path, str], None]


def rescue_write_host(host: Host, profile: DistroProfile, deps: RescueWriteDeps, ssh_key_name: str) -> None:
    """Transform ``host`` into the rescue-write distro IN PLACE (the real-host recipe).

    Order is load-bearing for trust: **verify FIRST** (the profile's
    ``RescueImageSpec.resolve_and_verify`` — SHA256, plus a pinned-key signature
    for a signed spec) — so an unauthentic or tampered image aborts BEFORE any
    rescue/reset mutation — then enable rescue, reset into it, deliver the image
    (curl a remote URL on the rescue side, or scp-push a local file to it), write
    it onto the disk (the rescue side re-checks the SHA post-transfer), disable
    rescue, reset into the written disk. Raises :class:`ArchBuildError` (or the
    verify's :class:`~vmlease.archimage.ArchImageError`) on any failure; the
    caller (runner) tears the host down regardless.
    """
    spec = profile.rescue_image
    if spec is None:
        raise ArchBuildError(f"distro {profile.family!r} has no rescue_image spec; cannot rescue-write")
    # TRUST GATE — must precede every mutation. A bad signature / sha raises here,
    # before enable-rescue, so nothing destructive runs against an untrusted image.
    resolved: ResolvedRescueImage = spec.resolve_and_verify(deps.resolve_deps)

    _check(deps.cli(build_enable_rescue_argv(host.id, ssh_key_name)), "enable-rescue")
    _check(deps.cli(build_reset_argv(host.id)), "reset-into-rescue")
    deps.wait_rescue_ready(host.ipv4)

    # LocalFile delivery: push the verified file to the fixed rescue-side path
    # BEFORE the script (which then only asserts its presence). RemoteUrl is curled
    # by the script instead — no push.
    if isinstance(resolved.source, LocalFile):
        deps.push_to_rescue(host.ipv4, resolved.source.path, RESCUE_IMAGE_PATH)

    script = render_rescue_script(resolved.source, resolved.expected_sha256)
    rc, out = deps.ssh_root(host.ipv4, script)
    if rc != 0 or "RESCUE_WRITE_OK" not in out:
        raise ArchBuildError(f"rescue write failed (rc={rc}) for {host.name}: {out[-400:]!r}")

    _check(deps.cli(build_disable_rescue_argv(host.id)), "disable-rescue")
    _check(deps.cli(build_reset_argv(host.id)), "reset-into-disk")
    # NOTE: we do NOT wait for the booted target OS here. The written Arch cloudimg
    # provisions the ssh key for the OPERATOR (via cloud-init), not necessarily
    # root — so an archbuild-side root wait is both wrong-principal and redundant
    # with the runner's own operator-readiness gate (_run_one_host ->
    # ssh.wait_until_ready, with the throwaway operator key). Issuing the reset is
    # enough; the operator-readiness wait owns "the target booted".


def _check(result: tuple[int, str, str], step: str) -> None:
    """Raise :class:`ArchBuildError` if an hcloud CLI step exited non-zero."""
    rc, _out, err = result
    if rc != 0:
        raise ArchBuildError(f"hcloud {step} failed (rc={rc}): {err[:200]!r}")


def build_keyring_import_argv(keyring_path: str, fingerprint: str) -> list[str]:
    """argv to import the pinned arch-boxes key into a dedicated gpg keyring.

    Receives the key from a keyserver into an isolated keyring (NOT the operator's
    default keyring), so signature verification depends only on the pinned key.
    """
    return [
        "gpg", "--no-default-keyring", "--keyring", keyring_path,
        "--keyserver", "hkps://keyserver.ubuntu.com", "--recv-keys", fingerprint,
    ]


def ensure_arch_keyring(
    keyring_path: str,
    run: Callable[[list[str], str | None], tuple[int, str, str]],
    fingerprint: str = "",
) -> None:
    """Import the pinned arch-boxes key into ``keyring_path`` (raises on failure)."""
    from vmlease.archimage import DEFAULT_ARCH_KEY_FINGERPRINT

    fp = fingerprint or DEFAULT_ARCH_KEY_FINGERPRINT
    rc, _out, err = run(build_keyring_import_argv(keyring_path, fp), None)
    if rc != 0:
        raise ArchBuildError(f"failed to import pinned arch-boxes key {fp} into keyring: {err[:200]!r}")


def live_subprocess_run(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
    """Default subprocess seam for the live factory (capture text, never raise)."""
    import subprocess

    p = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
    return p.returncode, p.stdout, p.stderr


def _live_urlopen_text(url: str) -> str:
    """Default text URL-fetch seam for the live factory."""
    return _live_urlopen_bytes(url).decode("utf-8", "replace")


def _live_urlopen_bytes(url: str) -> bytes:
    """Default binary URL-fetch seam for the live factory."""
    import urllib.request

    with urllib.request.urlopen(url) as resp:
        data: bytes = resp.read()
    return data


def _live_write_temp(data: bytes) -> str:
    """Stage bytes to a temp file (for gpg --verify of qcow2 / sig)."""
    import tempfile

    fd, path = tempfile.mkstemp(prefix="vmlease-arch-")
    import os

    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


def build_live_resolve_deps(
    keyring_path: str,
    *,
    run: Callable[[list[str], str | None], tuple[int, str, str]] = live_subprocess_run,
    fetch_text: Callable[[str], str] = _live_urlopen_text,
    fetch_bytes: Callable[[str], bytes] = _live_urlopen_bytes,
    write_temp: Callable[[bytes], str] = _live_write_temp,
) -> ResolveDeps:
    """Assemble the live :class:`~vmlease.rescue_image.ResolveDeps` trust-gate seam.

    The single source of the resolve-side IO bundle the profile's
    ``RescueImageSpec.resolve_and_verify`` verifies against: the URL fetchers, the
    temp-staging seam, and the gpg verify runner (a thin wrapper that maps the
    2-tuple ``run`` seam into the ``CompletedProcess`` the gpg path expects). The
    stdlib seams default to live implementations but are injectable, so the wiring
    stays unit-testable without real network/gpg. Both ``build_live_rescue_writer``
    (the rescue-write path) and ``build-image`` (the cache key derivation) build
    their ``ResolveDeps`` through here, so the trust-gate wiring can't drift.

    ``keyring_path`` is the gpg keyring holding the pinned ``arch-boxes`` key (set
    up by the caller); a native distro never touches these seams, so an unused
    keyring path is fine on that path.
    """
    import subprocess

    def _gpg_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        rc, out, err = run(argv, None)
        return subprocess.CompletedProcess(argv, rc, out, err)

    return ResolveDeps(
        text_fetcher=fetch_text, fetcher=fetch_bytes, gpg_runner=_gpg_runner,
        write_temp=write_temp, keyring_path=keyring_path,
    )


def build_live_rescue_writer(
    private_key_path: str,
    ssh_key_name: str,
    keyring_path: str,
    *,
    run: Callable[[list[str], str | None], tuple[int, str, str]] = live_subprocess_run,
    sleep: Callable[[float], None] | None = None,
    fetch_text: Callable[[str], str] = _live_urlopen_text,
    fetch_bytes: Callable[[str], bytes] = _live_urlopen_bytes,
    write_temp: Callable[[bytes], str] = _live_write_temp,
) -> Callable[[Host, DistroProfile], None]:
    """Build the rescue-writer closure, assembling the live deps for :func:`rescue_write_host`.

    The stdlib seams (``run`` subprocess runner, ``sleep``, ``fetch_text`` /
    ``fetch_bytes`` URL fetchers, ``write_temp``) default to live implementations
    but are injectable, so the factory's WIRING — the **trust gate**
    (:class:`~vmlease.rescue_image.ResolveDeps` the profile's spec verifies with),
    the ssh/scp argv shape, the readiness-retry loop — is unit-testable without
    real I/O. ``keyring_path`` is the gpg keyring holding the pinned ``arch-boxes``
    key (set up by the caller), passed through to the Arch spec's signature check.
    The orchestration LOGIC lives in the already-tested :func:`rescue_write_host`.
    """
    import time

    _sleep = sleep if sleep is not None else time.sleep

    def _cli(argv: list[str]) -> tuple[int, str, str]:
        return run(argv, None)

    def _ssh_root(ipv4: str, script: str) -> tuple[int, str]:
        # fresh host key each OS swap → don't pin known_hosts; feed the script on stdin.
        argv = [
            "ssh", "-i", private_key_path, "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes",
            f"root@{ipv4}", "bash -s",
        ]
        rc, out, err = run(argv, script)
        return rc, out + err

    def _wait_rescue(ipv4: str, attempts: int = 60) -> None:
        # The RESCUE wait: only a `rescue` hostname counts. The base OS (created
        # with the same registered key) also answers ssh — accepting any ssh
        # success would false-positive on the base before the reset enters rescue,
        # then write the image against the wrong OS. Treat ONLY hostname==rescue
        # as ready; root login on the base (which may deny it) is never mistaken
        # for rescue.
        last = ""
        for _ in range(attempts):
            rc, out = _ssh_root(ipv4, "cat /etc/hostname")
            if rc == 0 and "rescue" in out.lower():
                return
            last = f"rc={rc}: {out.strip()[-160:]}"
            _sleep(4)
        raise ArchBuildError(f"host {ipv4} not reachable in RESCUE after {attempts} attempts; last: {last!r}")

    def _push_to_rescue(ipv4: str, local: Path, remote: str) -> None:
        # scp the verified LOCAL image to the rescue system as root, using the
        # SAME registered rescue key the rescue ssh authenticates with (NOT the
        # throwaway operator key). Reuses build_scp_argv's recycled-IP hardening.
        from vmlease.model import Host as _Host
        from vmlease.ssh import build_scp_argv

        argv = build_scp_argv(_Host(id="", name="", ipv4=ipv4), "root", Path(private_key_path), local, remote)
        rc, _out, err = run(argv, None)
        if rc != 0:
            raise ArchBuildError(f"scp of {local} to root@{ipv4}:{remote} failed (rc={rc}): {err[:200]!r}")

    resolve_deps = build_live_resolve_deps(
        keyring_path, run=run, fetch_text=fetch_text, fetch_bytes=fetch_bytes, write_temp=write_temp,
    )

    deps = RescueWriteDeps(
        resolve_deps=resolve_deps, cli=_cli, ssh_root=_ssh_root,
        wait_rescue_ready=_wait_rescue, push_to_rescue=_push_to_rescue,
    )

    def _writer(host: Host, profile: DistroProfile) -> None:
        rescue_write_host(host, profile, deps, ssh_key_name)

    return _writer
