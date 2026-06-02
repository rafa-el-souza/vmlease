"""Shared vocabulary for vmlease — frozen dataclasses + enums.

Pure data, no I/O. Every other module speaks in these types, so the layers
(provider / ssh / runner) compose against a stable, mockable contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ProbeTag(StrEnum):
    """How a probe touches the host — recorded metadata, not a guardrail.

    Hosts are disposable, so probes mutate freely where mutation buys signal;
    the tag records what state a probe left (so results are interpretable and
    the runner can order read-only probes before mutating ones).
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
        command: The shell command run over SSH as the operator. A probe never
            invokes ``sudo`` unless its tag is ``MUTATING_HOST_ROOT`` (the one
            sanctioned escalation probe).
        tag: What the probe touches (:class:`ProbeTag`).
        classifies: The design action this probe classifies (free text, for the
            results report — e.g. "L2 subuid append").
    """

    id: str
    title: str
    command: str
    tag: ProbeTag
    classifies: str = ""


@dataclass(frozen=True)
class Battery:
    """An ordered, named collection of probes loaded from a data file."""

    name: str
    probes: tuple[Probe, ...]

    def ordered(self) -> tuple[Probe, ...]:
        """Probes in execution order: read-only, then operator-space, then host-root.

        Stable within each tag group (preserves the authoring order), so the
        declared dependency order inside the host-root batch is respected.
        """
        rank = {
            ProbeTag.READ_ONLY: 0,
            ProbeTag.MUTATING_OPERATOR_SPACE: 1,
            ProbeTag.MUTATING_HOST_ROOT: 2,
        }
        return tuple(sorted(self.probes, key=lambda p: rank[p.tag]))


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
class Host:
    """A provisioned VM as the provider reports it back."""

    id: str
    name: str
    ipv4: str
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeResult:
    """The captured outcome of one probe on one host."""

    probe_id: str
    tag: ProbeTag
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """``True`` iff the probe exited zero. (Interpretation is per-probe;
        a ``False`` ``ok`` may be an *expected* fail — see the battery doc.)"""
        return self.exit_code == 0


@dataclass(frozen=True)
class HostRun:
    """All results for one host plus its self-describing detail snapshot."""

    host_spec: HostSpec
    detail: str
    results: tuple[ProbeResult, ...]


@dataclass(frozen=True)
class PlanItem:
    """One line of the ``plan`` dry-run: what WOULD be provisioned + probed.

    A plan makes zero provider calls; it renders the matrix the runner would
    execute so an operator can review (and cost-check) before any real spend.
    """

    host_name: str
    image: str
    server_type: str
    distro_key: str
    probe_count: int
