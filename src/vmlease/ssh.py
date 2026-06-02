"""SSH seam — the ``SshRunner`` Protocol + an OpenSSH impl + a readiness poll.

The runner executes one probe command on one host as the operator and returns
its captured outcome. It is a typed Protocol so the orchestration layer composes
against an interface a ``FakeSshRunner`` satisfies in unit tests — no test ever
opens a socket. The OpenSSH impl builds an ``ssh`` argv (pure, unit-tested) and
shells out via an injected subprocess runner.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vmlease.model import ProbeResult

if TYPE_CHECKING:
    from pathlib import Path

    from vmlease.model import Host, Probe

SshSubprocessRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]
Sleeper = Callable[[float], None]


class SshError(RuntimeError):
    """An SSH-level failure distinct from a probe's own non-zero exit.

    A probe that runs and exits non-zero is a normal :class:`ProbeResult` (the
    battery interprets it). ``SshError`` is the transport failing — the host
    never became reachable within the readiness budget.
    """


@runtime_checkable
class SshRunner(Protocol):
    """Run one probe command on one host. Mock this in tests."""

    def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
        """Execute ``probe.command`` on ``host`` and capture its outcome."""
        ...


def build_ssh_argv(host: Host, operator: str, private_key_path: Path, command: str) -> list[str]:
    """Build the ``ssh`` argv for ``command`` on ``host`` as ``operator``.

    ``UserKnownHostsFile=/dev/null`` + ``StrictHostKeyChecking=accept-new`` is
    load-bearing for disposable hosts on **recycled IPs**: Hetzner hands a
    just-freed IP back to the next host (especially under per-host teardown, where
    only one host is up at a time), so a persistent ``known_hosts`` would see the
    SAME IP with a DIFFERENT host key and REFUSE the connection (the run-2
    failure). Discarding the host-key store sidesteps that entirely. ``BatchMode``
    fails fast instead of prompting; a fixed connect timeout bounds each attempt.
    Pure — the impl runs it.
    """
    return [
        "ssh",
        "-i", str(private_key_path),
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        f"{operator}@{host.ipv4}",
        command,
    ]


class OpenSshRunner:
    """``ssh``-backed runner. ``runner``/``sleeper`` are injected test seams."""

    def __init__(
        self,
        operator: str,
        private_key_path: Path,
        *,
        runner: SshSubprocessRunner | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        self._operator = operator
        self._key = private_key_path
        self._run: SshSubprocessRunner = runner or _default_runner
        self._sleep: Sleeper = sleeper or time.sleep

    def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
        argv = build_ssh_argv(host, self._operator, self._key, probe.command)
        proc = self._run(argv)
        return ProbeResult(
            probe_id=probe.id,
            tag=probe.tag,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    def wait_until_ready(self, host: Host, *, attempts: int = 30) -> None:
        """Poll until the cloud-init readiness sentinel exists, or raise.

        Runs a cheap ``test -f /var/lib/vmlease-ready`` over SSH in a bounded
        loop. Raises :class:`SshError` if the host never becomes ready — the
        transport-failure signal (distinct from a probe's non-zero exit).
        """
        from vmlease.model import Probe as _Probe
        from vmlease.model import ProbeTag as _Tag

        check = _Probe(id="_ready", title="readiness", command="test -f /var/lib/vmlease-ready", tag=_Tag.READ_ONLY)
        for attempt in range(attempts):
            if self.run_probe(host, check).exit_code == 0:
                return
            self._sleep(min(2.0, 1.0 + attempt * 0.1))
        raise SshError(f"host {host.name} ({host.ipv4}) not ready after {attempts} attempts")


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)
