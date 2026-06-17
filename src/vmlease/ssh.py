"""SSH seam — the ``SshRunner`` Protocol + an OpenSSH impl.

Five transports over one host as the operator: ``run_probe`` (run a command,
capture at completion), ``upload`` (scp a single file), ``wait_until_ready`` (poll
the readiness sentinel), ``run_streaming`` (run a long command, deliver output
incrementally, bounded by a hard timeout that kills local + remote), and
``upload_dir`` (rsync a directory tree, dropping out-of-tree symlinks). It is a
typed Protocol so the orchestration layer composes against an interface a
``FakeSshRunner`` satisfies in unit tests — no test ever opens a socket. The
OpenSSH impl builds each argv (pure, unit-tested) and shells out via an injected
subprocess runner (capture-style for probes/upload, a streaming seam for
``run_streaming``).
"""

from __future__ import annotations

import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vmlease import assertions
from vmlease.model import Outcome, ProbeResult
from vmlease.safety import validate_remote_dest, validate_upload_dir_source

if TYPE_CHECKING:
    from pathlib import Path

    from vmlease.model import Host, Probe

SshSubprocessRunner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]
Sleeper = Callable[[float], None]

# The run-wide default probe timeout (seconds) when a probe carries no per-probe
# ``timeout``. Generous enough for the slowest legitimate probe; the per-probe
# override (carried in the battery) is the escape hatch for known-slow commands.
DEFAULT_PROBE_TIMEOUT = 600.0
# A streaming runner: run ``argv``, deliver output to the callback as it arrives,
# kill the process and raise :class:`SshError` if it outlives ``timeout`` seconds,
# and return the exit code on normal completion. Injected so tests never open a
# socket (the pure argv builder is unit-tested; the kill mechanic is smoke-tested).
StreamSubprocessRunner = Callable[[list[str], Callable[[str], None], float], int]

# The recycled-IP hardening, ONE source for every transport's argv (rule: a value
# in two places drifts — so all builders splice this, and ``build_rsync_argv``
# joins it into its ``-e ssh …`` string). ``UserKnownHostsFile=/dev/null`` +
# ``StrictHostKeyChecking=accept-new`` survive a reused IP carrying a new host key
# (the run-2 bug); ``BatchMode`` fails fast instead of prompting; ``ConnectTimeout``
# bounds each attempt. ``LogLevel=ERROR`` quiets the per-connect "Permanently added
# '<ip>' … to the list of known hosts" banner that ``UserKnownHostsFile=/dev/null``
# + ``accept-new`` make ssh print on EVERY connection — load-bearing now that
# ``[probe.assert]`` ships stderr assertions (``stderr_empty`` / ``stderr_lacks`` /
# ``stderr_matches_not``): the banner would otherwise pollute captured probe stderr
# and make a clean command fail ``stderr_empty``. ``ERROR`` (not ``QUIET``) keeps
# real ``ERROR``/``FATAL`` connection failures visible.
_BASE_SSH_OPTS: tuple[str, ...] = (
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "LogLevel=ERROR",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
)

# How long the stream reader thread may keep draining the pipe after the process
# has exited before :func:`_default_stream_runner` returns — bounded so a wedged
# pipe can never hang the return (a tail of post-exit output may be dropped, which
# is benign for streamed CI logs).
_PUMP_DRAIN_TIMEOUT = 5.0


class SshError(RuntimeError):
    """An SSH-level failure distinct from a command's own non-zero exit.

    A probe (or streamed gate) that runs and exits non-zero is normal data the
    caller interprets. ``SshError`` is the transport failing: the host never
    became reachable within the readiness budget, an upload/directory-push exited
    non-zero, or a streamed command outlived its timeout and was killed.
    """


@runtime_checkable
class SshRunner(Protocol):
    """Run one probe command on one host. Mock this in tests."""

    def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
        """Execute ``probe.command`` on ``host`` and capture its outcome."""
        ...

    def upload(self, host: Host, local: Path, remote: str) -> None:
        """Copy local file ``local`` to ``remote`` on ``host`` over SSH."""
        ...

    def wait_until_ready(self, host: Host) -> None:
        """Block until ``host`` is reachable/ready, or raise :class:`SshError`.

        The runner calls this before invoking any workload, so a workload only
        ever runs against a ready host. Impls that need no readiness poll (a
        test fake) satisfy it as a no-op.
        """
        ...

    def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
        """Stream ``command`` on ``host``: deliver output to ``on_output`` as it
        arrives and return the command's exit code (a non-zero exit is data, not
        an error). Bounded by ``timeout`` seconds — on expiry the local and remote
        processes are killed and :class:`SshError` is raised."""
        ...

    def upload_dir(self, host: Host, local: Path, remote: str) -> None:
        """Push directory ``local`` to ``remote`` on ``host`` recursively, without
        following symlinks that point outside the tree."""
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
    ``LogLevel=ERROR`` suppresses ssh's per-connect "Permanently added" banner (an
    INFO message ``accept-new`` emits on every connection) so it does not pollute
    captured probe stderr, while keeping real ``ERROR``/``FATAL`` failures visible.
    Pure — the impl runs it.
    """
    return [
        "ssh",
        "-i", str(private_key_path),
        *_BASE_SSH_OPTS,
        f"{operator}@{host.ipv4}",
        command,
    ]


def build_scp_argv(host: Host, operator: str, private_key_path: Path, local: Path, remote: str) -> list[str]:
    """Build the ``scp`` argv to copy ``local`` to ``remote`` on ``host``.

    Mirrors :func:`build_ssh_argv` exactly — same identity key and the same
    recycled-IP hardening (``UserKnownHostsFile=/dev/null`` +
    ``StrictHostKeyChecking=accept-new`` so a reused IP with a new host key is
    not a refused connection; ``BatchMode`` + a fixed ``ConnectTimeout`` so the
    transfer fails fast instead of prompting). The ``--`` terminates option
    parsing so neither path can be read as a flag — belt-and-suspenders with the
    safety layer's leading-dash refusal on the remote destination. Pure — the
    impl runs it.
    """
    return [
        "scp",
        "-i", str(private_key_path),
        *_BASE_SSH_OPTS,
        "--",
        str(local),
        f"{operator}@{host.ipv4}:{remote}",
    ]


def build_ssh_stream_argv(host: Host, operator: str, private_key_path: Path, command: str) -> list[str]:
    """Build the ``ssh`` argv to STREAM a long-running ``command`` on ``host``.

    Same recycled-IP hardening as :func:`build_ssh_argv`, plus two additions for a
    killable, long-running stream:

    - ``-tt`` forces a PTY so that when the local client is killed (on timeout),
      the closing PTY sends ``SIGHUP`` to the remote process group — the runaway
      command dies on the host instead of being orphaned on a billed VM.
    - ``ServerAliveInterval`` / ``ServerAliveCountMax`` tear the session down if
      the connection goes silently dead.

    ``-tt`` merges stdout+stderr onto the one PTY stream (with CR translation),
    which is acceptable for streamed CI logs. Pure — the impl runs it.
    """
    return [
        "ssh",
        "-tt",
        "-i", str(private_key_path),
        *_BASE_SSH_OPTS,
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        f"{operator}@{host.ipv4}",
        command,
    ]


def build_rsync_argv(host: Host, operator: str, private_key_path: Path, local: Path, remote: str) -> list[str]:
    """Build the ``rsync`` argv to push directory ``local`` to ``remote`` on ``host``.

    ``-a`` preserves the tree; **``--safe-links``** ships symlinks that point inside
    the tree but DROPS symlinks pointing outside it — so a source tree can never
    cause an out-of-tree file (e.g. ``evil -> /etc/passwd``) to be shipped to the
    worker. ``-e ssh …`` carries the SAME recycled-IP hardening as the other
    transports (joined from the one ``_BASE_SSH_OPTS`` source, with the key path
    ``shlex.quote``d so a space in it can't word-split the ``-e`` value). ``--``
    guards option injection. The trailing ``/`` on the source copies its CONTENTS
    into ``remote``. Pure — the impl runs it.
    """
    ssh_cmd = f"ssh -i {shlex.quote(str(private_key_path))} {' '.join(_BASE_SSH_OPTS)}"
    return [
        "rsync",
        "-a",
        "--safe-links",
        "-e", ssh_cmd,
        "--",
        f"{str(local).rstrip('/')}/",
        f"{operator}@{host.ipv4}:{remote}",
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
        stream_runner: StreamSubprocessRunner | None = None,
        probe_timeout_default: float = DEFAULT_PROBE_TIMEOUT,
    ) -> None:
        self._operator = operator
        self._key = private_key_path
        self._run: SshSubprocessRunner = runner or _default_runner
        self._sleep: Sleeper = sleeper or time.sleep
        self._stream: StreamSubprocessRunner = stream_runner or _default_stream_runner
        self._probe_timeout_default = probe_timeout_default

    def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
        """Run ``probe.command`` on ``host``, bounded by the effective timeout.

        The effective timeout is the probe's own ``timeout`` when set, else the
        runner's run-wide default. On :class:`subprocess.TimeoutExpired` the
        bounded transport seam kills the local process; ``run_probe`` RECORDS a
        timed-out :class:`ProbeResult` (sentinel exit ``124``, best-effort partial
        output off the exception, ``timed_out=True``) rather than raising — a
        timeout is data about the probe (treated like a non-zero exit, battery
        continues), not a dead transport. The remote command is orphaned but dies
        with the host that is destroyed seconds later (so no ``-tt`` is needed on
        ``build_ssh_argv``).
        """
        timeout = probe.timeout if probe.timeout is not None else self._probe_timeout_default
        argv = build_ssh_argv(host, self._operator, self._key, probe.command)
        try:
            proc = self._run(argv, timeout)
        except subprocess.TimeoutExpired as exc:
            return _timed_out_result(probe, timeout, exc)
        outcome = Outcome(proc.returncode, proc.stdout, proc.stderr)
        # Compute the verdict BEFORE constructing the frozen result (D10(A)):
        # declared assertions decide ``ok``; otherwise it is the command's exit code.
        if probe.assertions:
            failures = assertions.evaluate(probe.assertions, outcome)
            ok = not failures
        else:
            ok = proc.returncode == 0
            failures = ()
        return ProbeResult(
            probe_id=probe.id,
            tag=probe.tag,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            ok=ok,
            assertion_failures=failures,
            has_assertions=len(probe.assertions) > 0,
        )

    def upload(self, host: Host, local: Path, remote: str) -> None:
        """scp ``local`` to ``remote`` on ``host``; raise on a non-zero scp exit.

        Runs the ``scp`` argv through the same injected ``runner`` seam
        ``run_probe`` uses (so no test opens a socket). A non-zero return is a
        transport failure — raised as :class:`SshError`, the same contract as
        :meth:`wait_until_ready` — distinct from a probe's own non-zero exit. The
        probe-default timeout also bounds the transfer, so a hung scp cannot hang
        the run; a timeout is converted to :class:`SshError` (same contract as the
        non-zero exit), not surfaced as a raw :class:`subprocess.TimeoutExpired`.
        """
        argv = build_scp_argv(host, self._operator, self._key, local, remote)
        try:
            proc = self._run(argv, self._probe_timeout_default)
        except subprocess.TimeoutExpired as exc:
            raise SshError(
                f"upload of {local} to {self._operator}@{host.ipv4}:{remote} timed out "
                f"after {self._probe_timeout_default}s"
            ) from exc
        if proc.returncode != 0:
            raise SshError(
                f"upload of {local} to {self._operator}@{host.ipv4}:{remote} failed "
                f"(scp exit {proc.returncode}): {proc.stderr.strip()}"
            )

    def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
        """Stream ``command`` on ``host``; deliver output to ``on_output`` as it arrives.

        Returns the command's exit code — a non-zero gate result is data, not an
        error. Bounded by ``timeout`` seconds: on expiry the local ssh client is
        killed (its ``-tt`` PTY HUPs the remote, so no runaway process is left on
        the host) and :class:`SshError` is raised, distinct from the command's own
        non-zero exit. Output is the merged stdout+stderr PTY stream. Runs through
        the injected stream seam, so no test opens a socket.
        """
        argv = build_ssh_stream_argv(host, self._operator, self._key, command)
        return self._stream(argv, on_output, timeout)

    def upload_dir(self, host: Host, local: Path, remote: str) -> None:
        """rsync directory ``local`` to ``remote`` on ``host``; raise on a non-zero exit.

        Validates the source (fail-closed: no symlinked path component, must be a
        readable directory) and the remote destination before transferring, then
        pushes the tree with ``rsync --safe-links`` (out-of-tree symlinks are NOT
        followed). Runs through the same injected ``runner`` seam ``upload`` uses.
        A non-zero rsync exit is a transport failure raised as :class:`SshError`.
        The probe-default timeout also bounds the transfer, so a hung rsync cannot
        hang the run; a timeout is converted to :class:`SshError` (same contract as
        the non-zero exit), not surfaced as a raw :class:`subprocess.TimeoutExpired`.
        """
        validate_upload_dir_source(local)
        validate_remote_dest(remote)
        argv = build_rsync_argv(host, self._operator, self._key, local, remote)
        try:
            proc = self._run(argv, self._probe_timeout_default)
        except subprocess.TimeoutExpired as exc:
            raise SshError(
                f"directory upload of {local} to {self._operator}@{host.ipv4}:{remote} timed out "
                f"after {self._probe_timeout_default}s"
            ) from exc
        if proc.returncode != 0:
            raise SshError(
                f"directory upload of {local} to {self._operator}@{host.ipv4}:{remote} failed "
                f"(rsync exit {proc.returncode}): {proc.stderr.strip()}"
            )

    def wait_until_ready(self, host: Host, *, attempts: int = 120) -> None:
        """Poll until the cloud-init readiness sentinel exists AND the running
        kernel has a usable module tree, or raise :class:`SshError`.

        Each attempt runs a cheap ``test -f /var/lib/vmlease-ready`` over SSH;
        once the sentinel lands, the SAME loop runs the module-tree probe. The
        module check lives INSIDE the readiness loop on purpose: the sentinel is
        touched by the post-reboot oneshot, so the very next probe hits a host
        fresh from a reboot whose sshd may still be flapping — a transient SSH
        transport failure (``ssh`` exits 255, ``run_probe`` returns exit 255
        WITHOUT raising) must retry within the attempt budget, NOT be misread as
        a permanent rescue/modules skew. Only a GENUINE skew (the remote ``test``
        actually ran and failed) raises immediately.

        ``attempts`` defaults wide (120 x <=2s ~= 4 min) because reboot-resume
        provisioning (arch rescue/modules fix) only touches the sentinel in the
        post-reboot remainder — the readiness window must comfortably span a
        full Hetzner reboot, not just first-boot cloud-init. Same backoff shape;
        only the budget widened.

        The module-tree check is the generic backstop for the rescue/modules
        skew: if a kernel upgrade orphaned the running kernel's modules and the
        reboot/resume failed to land a consistent kernel, the host fails loud
        HERE (with the running kernel version named), before any workload.
        """
        from vmlease.model import Probe as _Probe
        from vmlease.model import ProbeTag as _Tag

        check = _Probe(id="_ready", title="readiness", command="test -f /var/lib/vmlease-ready", tag=_Tag.READ_ONLY)
        modules = _Probe(
            id="_module_tree",
            title="module-tree assertion",
            command='uname -r; test -f "/lib/modules/$(uname -r)/modules.dep"',
            tag=_Tag.READ_ONLY,
        )
        for attempt in range(attempts):
            if self.run_probe(host, check).exit_code == 0:
                result = self.run_probe(host, modules)
                if result.exit_code == 0:
                    return  # sentinel + usable module tree → ready
                if result.stdout.strip():
                    # ``uname -r`` printed a version → the remote command ran and
                    # the trailing ``test`` is what failed: a genuine skew that
                    # will NOT self-heal. Fail loud with the running kernel.
                    kernel = result.stdout.splitlines()[0].strip()
                    raise SshError(f"host kernel {kernel} has no /lib/modules tree (rescue/modules skew)")
                # empty stdout → the probe never ran on the remote (transport
                # blip / host mid-reboot). Treat as not-ready-yet and retry.
            self._sleep(min(2.0, 1.0 + attempt * 0.1))
        raise SshError(f"host {host.name} ({host.ipv4}) not ready after {attempts} attempts")


def _default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` to completion, bounded by ``timeout`` seconds.

    ``subprocess.run(timeout=…)`` drains both pipes concurrently (no full-pipe
    deadlock) and, on expiry, kills the local process and raises
    :class:`subprocess.TimeoutExpired` carrying whatever partial output was read.
    The caller (:meth:`OpenSshRunner.run_probe`) turns that into a recorded
    timed-out result rather than letting it propagate.

    ``errors="replace"`` decodes stdout/stderr leniently: a probe (or the
    host-detail snapshot) can emit a non-UTF-8 byte (e.g. a Windows-1252 quote
    in ``os-release``), and strict decoding would raise ``UnicodeDecodeError``
    out of the whole host run — zeroing every probe. Lenient decoding matches
    :func:`_decode_partial`'s policy on the timeout path, so the normal and
    timeout paths agree.
    """
    return subprocess.run(
        argv, capture_output=True, text=True, errors="replace", check=False, timeout=timeout
    )


def _timed_out_result(probe: Probe, timeout: float, exc: subprocess.TimeoutExpired) -> ProbeResult:
    """Build the recorded :class:`ProbeResult` for a probe that outlived ``timeout``.

    Partial output is read best-effort off the exception (``str`` when the run was
    text-mode, ``bytes`` decoded leniently, ``None``/absent → empty on a platform
    that does not populate it); a sentinel exit ``124`` and ``timed_out=True`` mark
    it. A "timed out after Ts" note is appended to stderr so the timeout is visible
    in the results without having to read the flag.
    """
    note = f"probe timed out after {timeout}s"
    stdout = _decode_partial(exc.stdout)
    stderr = _decode_partial(exc.stderr)
    stderr = f"{stderr}\n{note}" if stderr else note
    # A killed probe is never ok and its partial ``(124, partial-output)`` must
    # NEVER reach the assertion evaluator (D10(E)) — the verdict short-circuits
    # to ``False`` here without consulting ``probe.assertions``.
    return ProbeResult(
        probe_id=probe.id,
        tag=probe.tag,
        exit_code=124,
        stdout=stdout,
        stderr=stderr,
        ok=False,
        timed_out=True,
        assertion_failures=(),
        has_assertions=len(probe.assertions) > 0,
    )


def _decode_partial(raw: str | bytes | None) -> str:
    """Best-effort partial output off a :class:`subprocess.TimeoutExpired`."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return raw


def _default_stream_runner(argv: list[str], on_output: Callable[[str], None], timeout: float) -> int:
    """Run ``argv``, stream merged output to ``on_output`` line by line, enforce ``timeout``.

    A reader thread pumps the merged stdout/stderr stream to ``on_output`` as lines
    arrive; the main thread bounds the run with ``proc.wait(timeout=…)``. On expiry
    the local process is killed — its ``-tt`` PTY HUPs the remote — and an
    :class:`SshError` is raised. The exit code is returned on normal completion.
    Not unit-tested (the kill mechanic needs a real PTY); the streaming/exit/timeout
    contract is tested via the injected seam, the kill via the real-host smoke.
    """
    # Context-managed so the pipes are always closed (no ResourceWarning under the
    # gate's ``-W error``), even on the timeout-kill path.
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1) as proc:

        def _pump() -> None:
            assert proc.stdout is not None  # stdout=PIPE above guarantees this
            for line in proc.stdout:
                on_output(line)

        pump = threading.Thread(target=_pump, daemon=True)
        pump.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait()
            raise SshError(f"streamed command timed out after {timeout}s and was killed") from exc
        pump.join(timeout=_PUMP_DRAIN_TIMEOUT)
        return proc.returncode
