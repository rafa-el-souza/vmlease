"""Arch rescue-write transform — turn a just-created host INTO an Arch host.

Hetzner has no Arch image, so an Arch host is provisioned in two moves:

1. create a cheap base host (e.g. debian-13) WITH vmlease's normal cloud-init
   ``--user-data`` attached (Hetzner stores it on the server's metadata);
2. **rescue-write** the verified Arch *cloudimg* qcow2 onto that host's disk and
   reboot — the cloudimg ships cloud-init, which reads Hetzner's ``hetzner``
   datasource on first boot and applies the SAME ``--user-data`` prep + injected
   key as every other distro (verified on a real host 2026-06-01).

So there is **no snapshot** and no run-to-run state: the pinned-signature verify
(:mod:`vmlease.archimage`) runs FRESH on every Arch build — verify-every-run,
strictly stronger than verify-once-into-a-reused-snapshot, and nothing billable
lingers. The build cost is one ~530 MiB fetch + a `qemu-img convert` + two
reboots (~2-3 min), which is acceptable for the security gain.

This module owns the orchestration: the pure argv builders + the rescue-write
script renderer are unit-tested; the live provider/ssh/reboot calls are the
billable path, behind injected seams. Every host-side step is a fail-loud probe
(detect the disk, re-verify the sha) rather than a hardcoded assumption — the
recipe encoded here is the real-host-proven one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from vmlease.archimage import ArchVersion, ResolvedImage
    from vmlease.distro import DistroProfile
    from vmlease.model import Host

# The on-rescue-system write script. Runs as root in the Hetzner rescue system,
# which a real-host run confirmed ships qemu-img + curl + sha256sum. It probes the disk
# device (never hardcodes sda vs nvme), re-verifies the sha after transfer, and
# converts qcow2 -> raw onto the disk. ``@@...@@`` slots are filled by
# :func:`render_rescue_script` (logic-free; shell ``$`` passes through).
_RESCUE_WRITE_SCRIPT = """#!/usr/bin/env bash
set -Eeuo pipefail

vmlease_rescue_write() {
  local url='@@qcow2_url@@' expected_sha='@@expected_sha256@@'

  # probe the real root block device (NOT assumed sda vs nvme0n1).
  local disk
  disk="$(lsblk -dpno NAME,TYPE | awk '$2=="disk"{print $1; exit}')"
  [ -n "$disk" ] || { echo 'RESCUE_FAIL: no disk device found' >&2; exit 11; }
  echo "RESCUE_DISK=$disk"

  command -v qemu-img >/dev/null 2>&1 || { echo 'RESCUE_FAIL: qemu-img absent' >&2; exit 12; }

  # fetch + RE-verify the sha (defence in depth: the operator side already
  # verified sha+signature; the rescue side re-checks integrity post-transfer).
  curl -fsSL "$url" -o /tmp/arch.qcow2 || { echo 'RESCUE_FAIL: download failed' >&2; exit 13; }
  echo "${expected_sha}  /tmp/arch.qcow2" | sha256sum -c - \
    || { echo 'RESCUE_FAIL: sha256 mismatch on rescue side' >&2; exit 14; }

  qemu-img convert -O raw /tmp/arch.qcow2 "$disk" \
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


def render_rescue_script(qcow2_url: str, expected_sha256: str) -> str:
    """Render the on-rescue-system write script (logic-free slot fill)."""
    from vmlease.templating import render

    return render(_RESCUE_WRITE_SCRIPT, {"qcow2_url": qcow2_url, "expected_sha256": expected_sha256})


def rescue_image_url(version: ArchVersion, profile: DistroProfile) -> str:
    """The mirror URL of the profile's rescue image for ``version``."""
    from vmlease.archimage import version_dir_url

    return f"{version_dir_url(version)}{profile.rescue_image}"


# --------------------------------------------------------------------------- #
# The live rescue-write orchestration (billable; behind injected seams)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RescueWriteDeps:
    """Injected dependencies for :func:`rescue_write_host` (the live path).

    Each is a seam so the orchestration is unit-testable without hcloud/ssh/network:

    - ``verify``: resolve the latest image AND verify it (SHA256 + the pinned
      ``arch-boxes`` GPG signature), returning the validated
      :class:`~vmlease.archimage.ResolvedImage`. This is the **trust gate** —
      it runs FIRST, before any host mutation, so an unauthentic image aborts the
      build before a single billable/destructive step. Raising here is the
      fail-closed path. The live wiring delegates to
      :func:`vmlease.archimage.resolve_and_verify` with the pinned key.
    - ``cli``: run an ``hcloud`` argv -> ``(rc, stdout, stderr)`` (provider CLI).
    - ``ssh_root``: run a shell script on the rescue/host as root over ssh,
      returning ``(exit_code, combined_output)``.
    - ``wait_rescue_ready``: bounded poll until the host is in the rescue system
      (hostname=='rescue'). The post-write target-OS readiness is NOT here — it
      is owned by the probe phase's operator-readiness wait (different principal /
      key), so the rescue-write only ISSUES the final reset.
    """

    verify: Callable[[DistroProfile], ResolvedImage]
    cli: Callable[[list[str]], tuple[int, str, str]]
    ssh_root: Callable[[str, str], tuple[int, str]]
    wait_rescue_ready: Callable[[str], None]


def rescue_write_host(host: Host, profile: DistroProfile, deps: RescueWriteDeps, ssh_key_name: str) -> None:
    """Transform ``host`` into the rescue-write distro IN PLACE (the real-host recipe).

    Order is load-bearing for trust: **verify FIRST** (SHA256 + pinned-key GPG
    signature, via ``deps.verify``) — so an unauthentic or tampered image aborts
    BEFORE any rescue/reset mutation — then enable rescue, reset into it, write the
    image onto the disk (the rescue side re-checks the SHA post-transfer), disable
    rescue, reset into the written disk. Raises :class:`ArchBuildError` (or the
    verify's :class:`~vmlease.archimage.ArchImageError`) on any failure; the
    caller (runner) tears the host down regardless.
    """
    # TRUST GATE — must precede every mutation. A bad signature / sha raises here,
    # before enable-rescue, so nothing destructive runs against an untrusted image.
    resolved = deps.verify(profile)
    url = rescue_image_url(resolved.version, profile)

    _check(deps.cli(build_enable_rescue_argv(host.id, ssh_key_name)), "enable-rescue")
    _check(deps.cli(build_reset_argv(host.id)), "reset-into-rescue")
    deps.wait_rescue_ready(host.ipv4)

    script = render_rescue_script(url, resolved.expected_sha256)
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


def build_verify(
    *,
    text_fetcher: Callable[[str], str],
    fetcher: Callable[[str], bytes],
    gpg_runner: Callable[[list[str]], tuple[int, str, str]],
    keyring_path: str,
    write_temp: Callable[[bytes], str],
    fingerprint: str = "",
) -> Callable[[DistroProfile], ResolvedImage]:
    """Build the trust-gate ``verify`` seam: resolve-latest + SHA256 + pinned-key sig.

    Delegates to :func:`vmlease.archimage.resolve_and_verify`, which returns a
    :class:`~vmlease.archimage.ResolvedImage` only when BOTH the SHA256 and the
    detached GPG signature against the pinned ``arch-boxes`` fingerprint pass —
    raising :class:`~vmlease.archimage.ArchImageError` otherwise (fail-closed).
    The ``profile`` arg is accepted for signature uniformity; the image identity
    is the Arch mirror's latest, independent of the profile.
    """
    import subprocess

    from vmlease.archimage import DEFAULT_ARCH_KEY_FINGERPRINT, resolve_and_verify

    fp = fingerprint or DEFAULT_ARCH_KEY_FINGERPRINT

    def _gpg(argv: list[str]) -> subprocess.CompletedProcess[str]:
        rc, out, err = gpg_runner(argv)
        return subprocess.CompletedProcess(argv, rc, out, err)

    def _verify(profile: DistroProfile) -> ResolvedImage:
        return resolve_and_verify(
            text_fetcher=text_fetcher,
            fetcher=fetcher,
            gpg_runner=_gpg,
            keyring_path=keyring_path,
            expected_fingerprint=fp,
            write_temp=write_temp,
        )

    return _verify


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
    but are injectable, so the factory's WIRING — the **trust gate** (SHA256 +
    pinned-key GPG signature via :func:`build_verify`), the ssh argv shape, the
    readiness-retry loop — is unit-testable without real I/O. ``keyring_path`` is
    the gpg keyring holding the pinned ``arch-boxes`` key (set up by the caller).
    The orchestration LOGIC lives in the already-tested :func:`rescue_write_host`.
    """
    import time

    _sleep = sleep if sleep is not None else time.sleep

    def _cli(argv: list[str]) -> tuple[int, str, str]:
        return run(argv, None)

    def _gpg_runner(argv: list[str]) -> tuple[int, str, str]:
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

    verify = build_verify(
        text_fetcher=fetch_text, fetcher=fetch_bytes, gpg_runner=_gpg_runner,
        keyring_path=keyring_path, write_temp=write_temp,
    )

    deps = RescueWriteDeps(
        verify=verify, cli=_cli, ssh_root=_ssh_root,
        wait_rescue_ready=_wait_rescue,
    )

    def _writer(host: Host, profile: DistroProfile) -> None:
        rescue_write_host(host, profile, deps, ssh_key_name)

    return _writer
