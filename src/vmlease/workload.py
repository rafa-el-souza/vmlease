"""Workload seam — the injected unit of on-host work the runner runs over SSH.

The runner owns the billable lifecycle (provision -> rescue-write -> readiness
gate -> upload staging -> **teardown**); a ``Workload`` owns only what runs on a
*ready* host. The probe battery is the reference implementation (:class:`ProbeWorkload`);
a second consumer (a CI gate job, living in another repo) is the reason this seam
exists — designed against the real second workload, not speculatively.

``Workload`` is a typed Protocol (mirroring :class:`~vmlease.ssh.SshRunner`) so a
``FakeWorkload`` satisfies it structurally in tests and the runner composes against
the interface, never a concrete impl.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vmlease.model import HostRun, Probe, ProbeTag

if TYPE_CHECKING:
    from vmlease.model import Battery, Host, HostSpec, ProbeResult
    from vmlease.ssh import SshRunner


@runtime_checkable
class Workload(Protocol):
    """One unit of on-host work, run by the runner against a ready host."""

    def run(self, spec: HostSpec, host: Host, ssh: SshRunner, /) -> HostRun:
        """Do the work on the (already-ready) ``host`` over ``ssh``; return its result.

        The runner has provisioned ``host``, gated its readiness, and staged any
        uploads before calling this; it will tear ``host`` down afterward. A
        transport failure SHALL raise (caught by the runner as an error
        ``HostRun``) — distinct from the workload's own captured outcome data.
        (Positional-only — the runner invokes it as ``workload.run(spec, host, ssh)``.)
        """
        ...

    @property
    def plan_summary(self) -> str:
        """A one-line description of the work, for the ``plan`` dry-run."""
        ...


class HostDetailProbe:
    """The fixed host-detail snapshot command (self-describing results header)."""

    command: str = (
        "{ echo '## os-release'; cat /etc/os-release; echo '## uname'; uname -a; "
        "echo '## systemd'; systemctl --version | head -1; "
        "echo '## cgroup'; stat -fc %T /sys/fs/cgroup; "
        "echo '## id'; id; "
        "echo '## tools'; command -v docker dockerd-rootless-setuptool.sh "
        "rootlesskit slirp4netns fuse-overlayfs newuidmap runsc 2>/dev/null; } 2>&1 || true"
    )


class ProbeWorkload:
    """The probe battery as a :class:`Workload`: host-detail snapshot + battery.

    Byte-faithful to the pre-seam ``_probe_one_host`` body: it captures the
    self-describing host-detail snapshot as the results header, then runs the
    battery in tag order, returning one :class:`~vmlease.model.HostRun`. Readiness
    and upload staging are NOT here — the runner owns them (they are
    transport-generic, shared by every workload).
    """

    def __init__(self, battery: Battery) -> None:
        self._battery = battery

    @property
    def plan_summary(self) -> str:
        """The probe-count token the ``plan`` dry-run renders (e.g. ``probes=3``)."""
        return f"probes={len(self._battery.probes)}"

    def run(self, spec: HostSpec, host: Host, ssh: SshRunner) -> HostRun:
        detail_probe = Probe(
            id="_detail", title="host detail", command=HostDetailProbe().command, tag=ProbeTag.READ_ONLY
        )
        detail = ssh.run_probe(host, detail_probe).stdout
        results: list[ProbeResult] = [ssh.run_probe(host, probe) for probe in self._battery.ordered()]
        return HostRun(host_spec=spec, detail=detail, results=tuple(results))
