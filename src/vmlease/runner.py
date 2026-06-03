"""Runner — compose a matrix into host specs, plan it, and execute it.

Turns a (battery, distro-keys, server-type) matrix into labelled
:class:`~vmlease.model.HostSpec` objects, gates them through the cost guard, and
either renders a ``plan`` that makes **zero** provider calls or runs the
provision -> probe -> teardown loop. Plan and execute build their specs from the
same generator, so the plan is byte-faithful to what a real run would do.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vmlease.cloudinit import render_cloudinit
from vmlease.distro import get_profile
from vmlease.model import HostRun, HostSpec, PlanItem
from vmlease.safety import CostGuard, make_run_id, run_label, validate_remote_dest, validate_upload_source

if TYPE_CHECKING:
    from vmlease.distro import DistroProfile
    from vmlease.keypair import Keypair
    from vmlease.model import Host, UploadSpec
    from vmlease.providers import Provider
    from vmlease.ssh import SshRunner
    from vmlease.workload import Workload


@dataclass(frozen=True)
class Matrix:
    """A run request: one workload across N distros on one server type.

    Attributes:
        workload: The injected :class:`~vmlease.workload.Workload` to run on every
            host (e.g. ``ProbeWorkload`` for the probe battery). The runner never
            names a concrete impl — the caller constructs and injects it.
        distro_keys: Which :mod:`vmlease.distro` profiles to provision.
        server_type: The (cheap, allowlisted) instance size for every host.
        run_token: The determinism seam for the run-id (a slug/timestamp the
            caller supplies — NOT read from the clock here).
        firewall: Optional provider firewall name attached to every host
            (``""`` = none).
        uploads: Files scp'd onto every host after readiness, before the workload
            (``()`` = none). Validated host-independently before any spend.
    """

    workload: Workload
    distro_keys: tuple[str, ...]
    server_type: str
    run_token: str
    firewall: str = ""
    uploads: tuple[UploadSpec, ...] = ()


def build_host_specs(matrix: Matrix) -> list[HostSpec]:
    """Turn a :class:`Matrix` into one labelled :class:`HostSpec` per distro.

    Pure + deterministic (the run-id derives from ``matrix.run_token``). Every
    spec carries the ``vmlease=<run-id>`` label so the safety layer can reap
    the whole run. Raises :class:`~vmlease.distro.UnknownDistroError` for an
    unknown distro key.
    """
    run_id = make_run_id(matrix.run_token)
    labels = run_label(run_id)
    specs: list[HostSpec] = []
    for key in matrix.distro_keys:
        profile = get_profile(key)
        specs.append(
            HostSpec(
                name=f"vmlease-{run_id}-{key}",
                image=profile.default_image,
                server_type=matrix.server_type,
                distro_key=key,
                labels=dict(labels),
                firewall=matrix.firewall,
            )
        )
    return specs


def validate_uploads(matrix: Matrix) -> None:
    """Validate every upload's source + remote dest, fail-closed, before spend.

    Host-independent (the local file and remote path are the same for every
    host), so the run's uploads are validated **once**. Raises
    :class:`~vmlease.safety.UploadError` on the first problematic spec — called
    at the top of both ``plan`` (zero provider calls) and ``execute`` (before the
    provision loop), so a bad ``--upload`` aborts before any host is created.
    """
    for spec in matrix.uploads:
        validate_upload_source(spec.local)
        validate_remote_dest(spec.remote)


def plan(matrix: Matrix, *, cost_guard: CostGuard | None = None) -> list[PlanItem]:
    """Render the dry-run plan. Makes **zero** provider calls.

    Builds the host specs (the same ones a real run would provision), validates
    the uploads + runs the specs through the cost guard (so ``plan`` surfaces a
    refusal *before* any spend), and returns one :class:`PlanItem` per host. The
    CLI prints these + a confirm-before-create prompt; nothing is provisioned
    here.
    """
    validate_uploads(matrix)
    specs = build_host_specs(matrix)
    guard = cost_guard or CostGuard()
    guard.check([s.server_type for s in specs])
    workload_summary = matrix.workload.plan_summary
    return [
        PlanItem(
            host_name=s.name,
            image=s.image,
            server_type=s.server_type,
            distro_key=s.distro_key,
            workload_summary=workload_summary,
        )
        for s in specs
    ]


# A rescue-writer transforms a just-created BASE host into the target distro by
# rescue-writing a verified image onto its disk + rebooting (the Arch path —
# :mod:`vmlease.archbuild`). It runs AFTER create and BEFORE probing, only for
# a profile whose ``needs_rescue_write`` is true. Injected so tests pass a fake
# and the live (billable) orchestration stays behind the seam.
RescueWriter = Callable[["Host", "DistroProfile"], None]


def execute(
    matrix: Matrix,
    provider: Provider,
    ssh_factory: Callable[[str, Keypair], SshRunner],
    keypair: Keypair,
    operator: str,
    *,
    cost_guard: CostGuard | None = None,
    rescue_writer: RescueWriter | None = None,
    max_parallel: int = 1,
) -> list[HostRun]:
    """Per host: provision -> (rescue-write) -> run workload -> **tear down immediately**.

    Each host is **isolated**: it is created, transformed, run, and destroyed in
    its own ``try/finally`` BEFORE the next host starts — so a host dies seconds
    after its workload (lower cost / shorter exposure) and, crucially, **one host's
    failure never discards another's results**. A host that fails to provision /
    rescue-write / become reachable is recorded as a ``HostRun`` with an error
    detail and zero results (NOT a raise), so :func:`execute` always returns
    one ``HostRun`` per requested host and the caller always writes a results file.
    The injected ``matrix.workload`` owns what runs on each ready host. The keypair
    is cleaned once at the end.

    ``ssh_factory`` builds an :class:`~vmlease.ssh.SshRunner` for the operator
    + keypair (injected so tests pass a fake). ``rescue_writer`` (injected) is
    REQUIRED when the matrix contains a ``needs_rescue_write`` distro (e.g. arch).
    ``cost_guard`` re-checks the matrix before any provider call.

    ``max_parallel`` runs up to N hosts concurrently (default 1 = serial). Each
    host is an independent, self-contained :func:`_run_one_host` (own create /
    probe / teardown), and the only shared state — the throwaway public key and
    the gpg keyring — is read-only after setup, so concurrency is safe and the
    teardown-always guarantee holds per thread. The work is I/O-bound (subprocess
    / ssh waits release the GIL), so a thread pool is the right tool. Results are
    returned in **matrix order** regardless of completion order. Running hosts
    concurrently also sidesteps Hetzner's recycled-IP-into-the-next-host behavior
    (hosts overlap, so an IP is not freed mid-run).
    """
    validate_uploads(matrix)
    specs = build_host_specs(matrix)
    guard = cost_guard or CostGuard()
    guard.check([s.server_type for s in specs])

    def _one(spec: HostSpec) -> HostRun:
        return _run_one_host(
            spec, matrix.workload, provider, ssh_factory, keypair, operator, rescue_writer, matrix.uploads
        )

    try:
        if max_parallel <= 1 or len(specs) <= 1:
            return [_one(spec) for spec in specs]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(max_parallel, len(specs))) as pool:
            # map preserves input order, so results align with the matrix.
            return list(pool.map(_one, specs))
    finally:
        keypair.cleanup()


def _run_one_host(
    spec: HostSpec,
    workload: Workload,
    provider: Provider,
    ssh_factory: Callable[[str, Keypair], SshRunner],
    keypair: Keypair,
    operator: str,
    rescue_writer: RescueWriter | None,
    uploads: tuple[UploadSpec, ...] = (),
) -> HostRun:
    """Create, (rescue-write,) run the workload, and ALWAYS destroy a single host.

    The runner owns the lifecycle around the workload: it waits for the host's
    readiness gate and stages any uploads (transport-generic, before the workload),
    then invokes ``workload.run`` against the ready host. Provider/rescue/transport
    failures are caught and returned as an error ``HostRun`` (so a later host can
    still run, and the caller writes results); only the workload's own captured
    outcome is normal data. The host is destroyed in the ``finally`` regardless.
    Teardown is best-effort and NEVER loses results: a failing ``destroy`` (e.g.
    a transient API timeout that the host-side delete actually completes) is
    appended to the result's detail as a warning, not raised — the collected data
    is the valuable artifact, and a stubborn server is a reap-able orphan, not a
    reason to discard everything. (``provider.destroy`` already retries transient
    timeouts; this guard handles the residual case.)
    """
    from vmlease.model import HostRun

    profile = get_profile(spec.distro_key)
    host: Host | None = None
    try:
        # cloud-init is rendered (+ validated) before create — a template defect
        # fails before spend. A rescue-write distro's base host gets the SAME
        # cloud-init; the written cloudimg re-applies it from the hetzner datasource.
        cloud_init = render_cloudinit(profile, operator, keypair.public_key)
        host = provider.create_with_cloudinit(spec, cloud_init)
        if profile.needs_rescue_write:
            if rescue_writer is None:
                raise RuntimeError(
                    f"distro {profile.key!r} needs a rescue-write transform but no "
                    f"rescue_writer was provided to execute()"
                )
            rescue_writer(host, profile)
        ssh = ssh_factory(operator, keypair)
        # Readiness + upload staging are transport-generic and the runner's job —
        # so the workload only ever runs against a ready host with its files in
        # place. An upload that fails raises SshError, caught below as a transport
        # host-failure (distinct from the workload's own captured outcome).
        ssh.wait_until_ready(host)
        for upload in uploads:
            ssh.upload(host, upload.local, upload.remote)
        run = workload.run(spec, host, ssh)
    except Exception as exc:  # provider / rescue / transport failure → record, don't abort
        run = HostRun(host_spec=spec, detail=f"ERROR: {type(exc).__name__}: {exc}", results=())

    if host is not None:
        teardown_note = _best_effort_destroy(provider, host)
        if teardown_note:
            run = HostRun(host_spec=run.host_spec, detail=f"{run.detail}\n{teardown_note}", results=run.results)
    return run


def _best_effort_destroy(provider: Provider, host: Host) -> str:
    """Destroy ``host``; return a warning note if it failed (never raises).

    A teardown failure must not lose the collected results, so it is reported as a
    note (the orphan is reap-able) rather than propagated.
    """
    try:
        provider.destroy(host)
        return ""
    except Exception as exc:
        return f"WARNING: teardown of {host.name} ({host.id}) failed — reap it: {exc}"
