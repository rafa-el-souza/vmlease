"""Shared vocabulary for vmlease — frozen dataclasses + enums.

Pure data, no I/O. Every other module speaks in these types, so the layers
(provider / ssh / runner) compose against a stable, mockable contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple, Protocol, runtime_checkable


class Outcome(NamedTuple):
    """The captured result of one probe command — the input an assertion reads.

    A flat ``(exit_code, stdout, stderr)`` triple the runner passes to each
    assertion's :meth:`Assertion.evaluate` / :meth:`Assertion.describe`. Pure
    data, no engine: this keeps :mod:`vmlease.model` free of the regex backend
    (``re2`` lives only in :mod:`vmlease.assertions`).
    """

    exit_code: int
    stdout: str
    stderr: str


@runtime_checkable
class Assertion(Protocol):
    """A predicate over a probe's :class:`Outcome` — structural typing only.

    The concrete, value-bound kinds live in :mod:`vmlease.assertions` (which may
    import a regex engine); they satisfy this Protocol structurally. ``model.py``
    defines only the shape so ``Probe`` can reference it without importing the
    engine — import direction is one-way (``assertions`` → ``model``).
    """

    def evaluate(self, outcome: Outcome) -> bool:
        """Whether this assertion holds for ``outcome``."""
        ...

    def describe(self, outcome: Outcome) -> str:
        """A single-line failure description (only called when it failed)."""
        ...


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
        assertions: The declarative ``[probe.assert]`` predicates over the
            probe's :class:`Outcome`, compiled from the manifest by the loader.
            Each satisfies the :class:`Assertion` Protocol (the concrete kinds —
            and the regex engine — live in :mod:`vmlease.assertions`, never
            here). ``()`` (the default, back-compatible) means no declarative
            assertions. This field is typed by the Protocol so ``model.py``
            stays engine-free.
    """

    id: str
    title: str
    command: str
    tag: ProbeTag
    classifies: str = ""
    timeout: float | None = None
    source: str = ""
    success_when: str = ""
    assertions: tuple[Assertion, ...] = ()


@dataclass(frozen=True)
class Battery:
    """A named collection of probes loaded from a data file.

    Probes execute in **authoring order** — the order they appear in ``probes``
    is the order they run and are recorded. ``tag`` records what each probe
    touches (and authorizes/records sudo escalation — the command runs verbatim,
    the lint warns on a non-host-root sudo); it does not reorder execution.
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
class Image:
    """A provider snapshot image as the provider reports it back.

    The provider-agnostic cache artifact (D1): everything above the provider
    (``imagecache`` / ``safety`` / ``cli`` / ``runner``) speaks only this type,
    never ``hcloud`` JSON. Frozen pure data, no I/O, no clock.

    Attributes:
        id: Provider-unique image id (the restore selector — ``server create
            --image <id>``).
        labels: Key/value labels on the image (the query index over the hashed
            cache key — ``vmlease-purpose=image-cache``, ``vmlease-cache-key``,
            etc.). The persistent image deliberately does NOT carry the
            ephemeral ``vmlease=<run-id>`` reap label.
        created: The provider's creation timestamp as an **ISO-8601 UTC string**
            verbatim (e.g. ``"2024-04-25T13:26:27+00:00"``). Stored as text, not
            a :class:`datetime`: age comparisons (``reap-images --older-than``)
            parse it on demand, so the library reads no clock and stays
            deterministic.
        disk_size: The snapshot's disk size in GB. The restore disk-bound (D9):
            a snapshot restores only onto a server whose disk ≥ this value (a
            mismatch is a graceful cache miss, never an error).
        arch: The CPU architecture the image targets (e.g. ``"x86"`` /
            ``"arm"``). The only hard restore match — already captured in the
            content key via the base image id.
    """

    id: str
    created: str
    disk_size: float
    arch: str
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeResult:
    """The captured outcome of one probe on one host.

    ``ok`` is a **runner-computed STORED verdict** (D9/M1): the SSH layer
    evaluates the probe's declarative assertions (or, transitionally, its
    ``success_when`` token / exit code) in :meth:`OpenSshRunner.run_probe`
    BEFORE constructing this frozen result, then stores the boolean here. It is
    REQUIRED (no default — a defaulted verdict would silently mis-pass). The
    model stays engine-free: it imports neither the regex backend (``re2``) nor
    :mod:`vmlease.assertions`; the verdict arrives already computed.
    ``assertion_failures`` carries the :meth:`Assertion.describe` of each FAILED
    declarative assertion (``()`` when none failed or none were declared) — the
    parsed assertion list itself never travels into the result.

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
    ok: bool
    timed_out: bool = False
    success_when: str = ""
    assertion_failures: tuple[str, ...] = ()


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
