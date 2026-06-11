"""Shared vocabulary for vmlease — frozen dataclasses + enums.

Pure data, no I/O. Every other module speaks in these types, so the layers
(provider / ssh / runner) compose against a stable, mockable contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class ProbeTag(StrEnum):
    """How a probe touches the host — recorded metadata, not a guardrail.

    Hosts are disposable, so probes mutate freely where mutation buys signal;
    the tag records what state a probe left (so results are interpretable) and
    names the sudo-escalation authoring contract: escalation belongs to a
    host-root-tagged probe, so the host-root tag authorizes and records that a
    probe escalates. The tag does NOT enforce this — the resolved command runs
    verbatim and the system injects, strips, or refuses nothing based on tag; an
    advisory lint warns when a non-host-root probe invokes sudo. The tag also
    does NOT order execution — probes run in authoring order.
    """

    READ_ONLY = "read-only"
    MUTATING_OPERATOR_SPACE = "mutating:operator-space"
    MUTATING_HOST_ROOT = "mutating:host-root"


@dataclass(frozen=True)
class Probe:
    """One declarative probe: a named shell command + its tag and intent.

    Attributes:
        id: Stable short identifier (e.g. ``"P1"``).
        title: Human-readable one-line description.
        command: The **resolved** executable shell run over SSH as the operator —
            the verbatim inline block, or the contents of the probe's script file.
            The system runs this command **verbatim**: it does not inject, strip,
            or refuse ``sudo`` based on the tag. Escalation is an authoring
            contract — the author writes ``sudo`` in the command, and the
            ``MUTATING_HOST_ROOT`` tag authorizes and records that escalation;
            an advisory lint warns on a non-host-root probe that invokes sudo.
        source: Provenance of ``command`` — the script path it was read from, or
            ``"<inline>"`` for an inline command. Used only for lint output and
            error messages. Defaults to ``""`` so direct ``Probe(...)``
            construction stays back-compatible.
        tag: What the probe touches (:class:`ProbeTag`). Records what state the
            probe leaves and names the sudo-escalation authoring contract
            (host-root authorizes and records escalation; the command still runs
            verbatim and an advisory lint, not the tag, flags a mismatch); it
            does NOT order execution — probes run in authoring order.
        success_when: Optional literal success token. When non-empty, the
            probe's :attr:`ProbeResult.ok` is decided by this token appearing as
            a **complete line** of stdout (leading/trailing whitespace stripped),
            replacing the exit-code reading — the author emits it as its own line
            (``echo TOKEN``). ``""`` (the default, back-compatible) keeps ``ok``
            exit-code-based.
        classifies: The design action this probe classifies (free text, for the
            results report — e.g. "L2 subuid append").
        timeout: Optional per-probe wall-clock bound (seconds) for the bounded
            probe transport. ``None`` (the default, back-compatible) means "use
            the runner's run-wide default" — the SSH layer resolves the effective
            value and enforces it, recording a timed-out :class:`ProbeResult`
            rather than hanging.
    """

    id: str
    title: str
    command: str
    tag: ProbeTag
    classifies: str = ""
    timeout: float | None = None
    source: str = ""
    success_when: str = ""


@dataclass(frozen=True)
class Battery:
    """A named collection of probes loaded from a data file.

    Probes execute in **authoring order** — the order they appear in ``probes``
    is the order they run and are recorded. ``tag`` records what each probe
    touches and governs sudo escalation; it does not reorder execution.
    """

    name: str
    probes: tuple[Probe, ...]


@dataclass(frozen=True)
class HostSpec:
    """A request for one VM: which image, which size, labelled for teardown.

    Attributes:
        name: Provider-unique server name (carries the run-id for reaping).
        image: Provider image slug (e.g. ``"ubuntu-24.04"``).
        server_type: Provider size slug (e.g. ``"cpx22"``).
        labels: Key/value labels applied to the resource (always includes the
            ``vmlease=<run-id>`` label the safety layer adds).
        distro_key: The :mod:`vmlease.distro` profile key (e.g. ``"ubuntu"``).
        firewall: Optional provider firewall name to attach at create time
            (``""`` = none). Restricting inbound to the operator's IP is good
            hygiene for a host that boots an unconfigured cloud image.
    """

    name: str
    image: str
    server_type: str
    distro_key: str
    labels: dict[str, str] = field(default_factory=dict)
    firewall: str = ""


@dataclass(frozen=True)
class UploadSpec:
    """One file to scp onto every host after readiness, before the battery.

    Immutable data, no I/O. The runner uploads ``local`` to ``remote`` on each
    host once it is ready and before the first probe. The safety layer validates
    both fields (a problematic source/dest is refused before any spend); this
    type just carries the request.

    Attributes:
        local: The local regular file to upload (validated: no symlink, regular,
            readable — see :func:`vmlease.safety.validate_upload_source`).
        remote: The destination path on the host (validated: no ``..``, no
            shell-unsafe chars, no leading ``-`` — see
            :func:`vmlease.safety.validate_remote_dest`). Defaults, when the CLI
            derives it, to ``~/<basename(local)>``.
    """

    local: Path
    remote: str


@dataclass(frozen=True)
class Host:
    """A provisioned VM as the provider reports it back."""

    id: str
    name: str
    ipv4: str
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeResult:
    """The captured outcome of one probe on one host.

    ``timed_out`` (default ``False``, back-compatible) marks a result the bounded
    probe transport produced because the command outlived its effective timeout:
    the SSH layer killed the local process and recorded this result (sentinel exit
    ``124``, best-effort partial output) instead of raising — a timeout is *data
    about the probe*, treated like a non-zero exit, and the consecutive-timeout
    breaker counts on this flag (not on the ``124`` exit a real command could
    coincidentally return).
    """

    probe_id: str
    tag: ProbeTag
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    success_when: str = ""

    @property
    def ok(self) -> bool:
        """Whether the probe passed, by exactly one of two readings.

        A timed-out result is never ok — a killed probe's partial output is not
        a verdict. Otherwise, when ``success_when`` is declared (non-empty), the
        probe is ok iff that token appears as a **complete line** of stdout
        (each line stripped of leading/trailing whitespace) — the exit code does
        not participate. When ``success_when`` is ``""`` (the default,
        back-compatible reading), the probe is ok iff it exited zero.
        Interpretation of a not-ok result is per-probe; it may be an *expected*
        fail — see the battery doc.
        """
        if self.timed_out:
            return False
        if self.success_when:
            return any(line.strip() == self.success_when for line in self.stdout.splitlines())
        return self.exit_code == 0


@dataclass(frozen=True)
class HostRun:
    """All results for one host plus its self-describing detail snapshot."""

    host_spec: HostSpec
    detail: str
    results: tuple[ProbeResult, ...]


@dataclass(frozen=True)
class PlanItem:
    """One line of the ``plan`` dry-run: what WOULD be provisioned + run.

    A plan makes zero provider calls; it renders the matrix the runner would
    execute so an operator can review (and cost-check) before any real spend.
    ``workload_summary`` is the injected workload's one-line self-description
    (e.g. ``probes=3`` for the probe battery) — the plan does not assume probes.
    """

    host_name: str
    image: str
    server_type: str
    distro_key: str
    workload_summary: str
