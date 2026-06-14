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

from vmlease import capabilities, distro
from vmlease.model import HostRun, PrepStepResult, Probe, ProbeTag

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vmlease.model import Battery, Host, HostSpec, PrepStep, ProbeResult
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


# The fixed host-detail snapshot command — the self-describing results header an
# operator reads first (os-release, kernel, init, cgroup, id, tool inventory).
# Pure data, captured once as a module constant.
_HOST_DETAIL_COMMAND = (
    "{ echo '## os-release'; cat /etc/os-release; echo '## uname'; uname -a; "
    "echo '## systemd'; systemctl --version | head -1; "
    "echo '## cgroup'; stat -fc %T /sys/fs/cgroup; "
    "echo '## id'; id; "
    "echo '## tools'; command -v docker dockerd-rootless-setuptool.sh "
    "rootlesskit slirp4netns fuse-overlayfs newuidmap runsc 2>/dev/null; } 2>&1 || true"
)

# How many CONSECUTIVE timed-out probes mark a host as wedged. A single isolated
# timeout (e.g. one slow ``machinectl`` probe) is recorded and the battery
# continues; a RUN of timeouts is the signal that the host — not one command — is
# the problem, so the battery stops to cap wasted wall-time at ~K*timeout instead
# of N*timeout. K=2 (not 1) so one slow probe never ends an otherwise-fine battery.
MAX_CONSECUTIVE_TIMEOUTS = 2

# The per-step prep wall-clock bound (seconds) when a ``[[prep.setup]]`` step
# carries no explicit ``timeout``. 1800s (30 min) is the design default (D13.1) —
# generous enough for the slowest legitimate prep (e.g. debian-13's ~1800s tlog
# source build) while still bounding a wedged step.
DEFAULT_PREP_TIMEOUT = 1800.0


def _effective_packages(
    packages: Mapping[str, tuple[str, ...]], manager: str, distro_key: str
) -> tuple[str, ...]:
    """The effective per-host package set: union(manager-list, distro-list), deduped.

    ``[prep.packages]`` keys are package-managers OR distros (disjoint, validated
    at load). The effective set for one host is the union of its manager's list and
    its distro's list, deduplicated in first-seen order with the manager entries
    first (D-E). Selectors absent from the mapping contribute nothing.
    """
    ordered = (*packages.get(manager, ()), *packages.get(distro_key, ()))
    seen: dict[str, None] = {}
    for pkg in ordered:
        seen.setdefault(pkg, None)
    return tuple(seen)


class ProbeWorkload:
    """The probe battery as a :class:`Workload`: host-detail snapshot + battery.

    Preserves the pre-seam probe path's per-probe capture contract: it captures
    the self-describing host-detail snapshot as the results header, then runs the
    battery in authoring order, returning one :class:`~vmlease.model.HostRun`. Readiness and upload
    staging are NOT here — the runner owns them (they are transport-generic,
    shared by every workload).
    """

    def __init__(self, battery: Battery) -> None:
        self._battery = battery

    @property
    def plan_summary(self) -> str:
        """The probe-count token the ``plan`` dry-run renders (e.g. ``probes=3``)."""
        return f"probes={len(self._battery.probes)}"

    def run(self, spec: HostSpec, host: Host, ssh: SshRunner, /) -> HostRun:
        """Capture the host-detail snapshot, then run the battery with a breaker.

        Every probe (the host-detail snapshot included) goes through the same
        bounded ``run_probe``; the runner holds the run-wide default and resolves
        each probe's effective timeout, so a hung command is recorded as a
        timed-out result rather than hanging the battery. An isolated timeout is
        recorded and the loop continues; after :data:`MAX_CONSECUTIVE_TIMEOUTS`
        consecutive timed-out results the host is judged wedged — the loop stops,
        a note lands in ``detail``, and every probe captured before the wedge is
        preserved. This caps wasted wall-time at ~K*timeout while losing no data.
        """
        detail_probe = Probe(
            id="_detail", title="host detail", command=_HOST_DETAIL_COMMAND, tag=ProbeTag.READ_ONLY
        )
        detail = ssh.run_probe(host, detail_probe).stdout

        # Prep phase (D-A layer 2): the battery's declared host prep, run once
        # after readiness and BEFORE the probe loop — first the package install
        # pass, then the ordered setup steps. A HARD failure (a package-pass
        # failure or a ``required`` setup step) returns a HostRun carrying the
        # captured ``prep_phase`` and zero probes — it does NOT raise (D-I.1):
        # raising would route through the runner's error path, which discards the
        # return value, so ``summarize`` could never count PREP_HARD_FAIL.
        # Teardown still fires via the runner's existing ``finally``.
        prep_phase: list[PrepStepResult] = []
        if self._run_prep(spec, host, ssh, prep_phase):
            return HostRun(
                host_spec=spec,
                detail=detail,
                results=(),
                prep_phase=tuple(prep_phase),
            )

        results: list[ProbeResult] = []
        consecutive_timeouts = 0
        probes = self._battery.probes
        for probe in probes:
            result = ssh.run_probe(host, probe)
            results.append(result)
            consecutive_timeouts = consecutive_timeouts + 1 if result.timed_out else 0
            if consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                not_run = [p.id for p in probes[len(results) :]]
                detail = (
                    f"{detail}\nbattery stopped: host wedged after {consecutive_timeouts} "
                    f"consecutive probe timeouts; probes {not_run} not run"
                )
                break
        return HostRun(
            host_spec=spec,
            detail=detail,
            results=tuple(results),
            prep_phase=tuple(prep_phase),
        )

    def _run_prep(
        self,
        spec: HostSpec,
        host: Host,
        ssh: SshRunner,
        prep_phase: list[PrepStepResult],
        /,
    ) -> bool:
        """Run the battery's prep phase; append outcomes to ``prep_phase`` in order.

        Runs the ``[prep.packages]`` install pass first (always hard), then the
        ``[[prep.setup]]`` steps in authoring order (skipping ``distros``-excluded
        steps). Returns ``True`` when a HARD failure aborts the host (a package-pass
        failure OR a ``required`` setup step exited non-zero) — the caller then
        returns a zero-probe :class:`HostRun` carrying ``prep_phase``. A soft
        (``required=false``) failure is recorded and the phase continues.
        """
        prep = self._battery.prep
        if prep is None:
            return False
        profile = distro.get_profile(spec.distro_key)
        manager = profile.package_manager

        # 1) The package install pass — apt-get update first on apt (D13.2), then
        # one ``<install> <pkgs>`` pass. Always hard: any non-zero exit aborts.
        packages = _effective_packages(prep.packages, manager, spec.distro_key)
        if packages and not self._run_package_pass(host, ssh, manager, packages, prep_phase):
            return True

        # 2) The ordered setup steps (authoring order); skip distros-excluded ones.
        for step in prep.setup:
            if step.distros and spec.distro_key not in step.distros:
                continue
            result = self._run_setup_step(host, ssh, step)
            prep_phase.append(result)
            if result.exit != 0 and step.required:
                return True
        return False

    def _run_package_pass(
        self,
        host: Host,
        ssh: SshRunner,
        manager: str,
        packages: tuple[str, ...],
        prep_phase: list[PrepStepResult],
        /,
    ) -> bool:
        """Run the single ``[prep.packages]`` install pass; record its outcome.

        On apt, ``apt-get update`` runs FIRST (D13.2) so the install pass sees a
        fresh index. Returns ``True`` on success, ``False`` on a non-zero exit
        (a hard abort). The outcome is recorded in ``prep_phase`` under the
        synthetic step id ``_packages`` (always ``required=True``).
        """
        install = capabilities.install_command(manager)
        command = f"sudo {install} {' '.join(packages)}"
        if manager == "apt":
            command = f"sudo apt-get update && {command}"
        probe = Probe(
            id="_packages", title="prep packages", command=command, tag=ProbeTag.MUTATING_HOST_ROOT
        )
        outcome = ssh.run_probe(host, probe)
        prep_phase.append(
            PrepStepResult(id="_packages", exit=outcome.exit_code, required=True, stderr=outcome.stderr)
        )
        return outcome.exit_code == 0

    def _run_setup_step(self, host: Host, ssh: SshRunner, step: PrepStep, /) -> PrepStepResult:
        """Run one ``[[prep.setup]]`` step and capture it as a :class:`PrepStepResult`.

        The step's effective per-step timeout is its own ``timeout`` when set, else
        :data:`DEFAULT_PREP_TIMEOUT` (D13.1). The command runs verbatim over SSH
        (the author writes ``sudo`` inline where root is needed).
        """
        timeout = step.timeout if step.timeout is not None else DEFAULT_PREP_TIMEOUT
        probe = Probe(
            id=step.id,
            title=step.title,
            command=step.command,
            tag=ProbeTag.MUTATING_HOST_ROOT,
            timeout=timeout,
        )
        outcome = ssh.run_probe(host, probe)
        return PrepStepResult(
            id=step.id, exit=outcome.exit_code, required=step.required, stderr=outcome.stderr
        )
