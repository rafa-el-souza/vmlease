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
from typing import TYPE_CHECKING, TypeVar

from vmlease.cloudinit import render_cloudinit, render_minimal_cloudinit
from vmlease.distro import get_profile
from vmlease.imagecache import (
    LABEL_CACHE_KEY,
    LABEL_PURPOSE,
    PURPOSE_IMAGE_CACHE,
    content_key,
)
from vmlease.model import HostRun, HostSpec, PlanItem
from vmlease.safety import CostGuard, make_run_id, run_label, validate_remote_dest, validate_upload_source

if TYPE_CHECKING:
    from vmlease.distro import DistroProfile
    from vmlease.keypair import Keypair
    from vmlease.model import Host, Image, UploadSpec
    from vmlease.providers import Provider
    from vmlease.rescue_image import ResolveDeps
    from vmlease.ssh import SshRunner
    from vmlease.workload import Workload

# The result type the ``on_ready`` seam (and therefore the scaffold) produces:
# ``run`` returns a ``HostRun``; ``build-image`` (group 6) will return an
# ``Image``. The scaffold is generic over it.
R = TypeVar("R")

# The single marker a failed teardown leaves in a host's detail, shared between
# the producer here (:func:`_best_effort_destroy`) and the CLI consumer that
# greps for it to exit non-zero (D3). Pinned verbatim — imported elsewhere.
TEARDOWN_WARNING_PREFIX = "WARNING: teardown of"


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

# The two seams the per-host scaffold (:func:`_with_ready_host`) is parameterized
# by (D6):
#
# - ``PlanCreate`` decides HOW the host is created: it returns the
#   ``(image, cloud_init, needs_rescue)`` triple. ``run``'s cold path returns the
#   profile's default image + the full cloud-init + the profile's rescue flag; the
#   cache-aware variant (:func:`cache_aware_plan_create`) returns a snapshot id +
#   minimal cloud-init + ``False`` on a hit. ``build-image`` (group 6) is always
#   cold. ``needs_rescue`` overrides the profile's own flag so a restore can skip
#   the rescue-write a miss would perform.
# - ``OnReady`` is the post-readiness tail, generic over its result ``R``: ``run``
#   uploads + runs the workload → ``HostRun``; ``build-image`` will sysprep +
#   poweroff + snapshot → ``Image``. It takes ``provider`` because the snapshot
#   tail needs it — it is deliberately NOT a ``Workload`` (which stays ssh-only).
PlanCreate = Callable[["DistroProfile"], "tuple[str, str, bool]"]
OnReady = Callable[["Host", "SshRunner", "Provider"], R]


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
    on_host_complete: Callable[[HostRun], None] | None = None,
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

    ``on_host_complete`` (injected, optional) is invoked with each host's
    ``HostRun`` as it finishes, from the **main thread** — serially in the loop,
    or via ``as_completed`` in parallel mode (NOT from a worker thread, so a sink
    that does I/O sees no concurrent calls). It lets the caller persist results
    incrementally (so an abort still leaves the finished hosts on disk) without
    changing the matrix-ordered aggregate return.
    """
    validate_uploads(matrix)
    specs = build_host_specs(matrix)
    guard = cost_guard or CostGuard()
    guard.check([s.server_type for s in specs])

    def _one(spec: HostSpec) -> HostRun:
        return _run_one_host(
            spec, matrix.workload, provider, ssh_factory, keypair, operator, rescue_writer, matrix.uploads
        )

    def _notify(host_run: HostRun) -> None:
        if on_host_complete is not None:
            on_host_complete(host_run)

    try:
        if max_parallel <= 1 or len(specs) <= 1:
            runs: list[HostRun] = []
            for spec in specs:
                host_run = _one(spec)
                _notify(host_run)
                runs.append(host_run)
            return runs
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=min(max_parallel, len(specs))) as pool:
            # Submit keeps a future->index map so the aggregate stays in matrix
            # order; as_completed lets the main thread call the sink as each host
            # finishes (the workers never touch on_host_complete).
            futures = {pool.submit(_one, spec): idx for idx, spec in enumerate(specs)}
            ordered: list[HostRun | None] = [None] * len(specs)
            try:
                for future in as_completed(futures):
                    idx = futures[future]
                    host_run = future.result()
                    ordered[idx] = host_run
                    _notify(host_run)
            except BaseException:
                # Abort-time best-effort drain that RE-RAISES (not a swallow): a
                # propagating BaseException (e.g. a worker's KeyboardInterrupt) would
                # otherwise drop hosts that already finished cleanly but whose
                # as_completed turn hadn't arrived. Fire on_host_complete for each
                # done, non-cancelled, not-yet-recorded future (in matrix order) so
                # their results persist like the serial path, then re-raise.
                for future, idx in sorted(futures.items(), key=lambda kv: kv[1]):
                    if ordered[idx] is not None or future.cancelled() or not future.done():
                        continue
                    try:
                        host_run = future.result()
                    except BaseException:  # this worker itself raised — skip it
                        continue
                    ordered[idx] = host_run
                    _notify(host_run)
                raise
            return [hr for hr in ordered if hr is not None]
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
    outcome is normal data. The host is destroyed in a real ``finally`` regardless
    of how the body left — a normal return, a caught ``Exception``, OR a
    ``BaseException`` (Ctrl-C / ``SystemExit``): teardown fires first, THEN the
    ``BaseException`` keeps propagating so an aborted run still exits. Teardown is
    best-effort and NEVER loses results: a failing ``destroy`` (e.g. a transient
    API timeout that the host-side delete actually completes) is appended to the
    result's detail as a warning, not raised — the collected data is the valuable
    artifact, and a stubborn server is a reap-able orphan, not a reason to discard
    everything. (``provider.destroy`` already retries transient timeouts; this
    guard handles the residual case.)
    """
    from vmlease.model import HostRun

    profile = get_profile(spec.distro_key)

    def plan_create(_profile: DistroProfile) -> tuple[str, str, bool]:
        # ``run``'s COLD path: the profile's default image + the full cloud-init +
        # the profile's own rescue flag. A rescue-write distro's base host gets the
        # SAME cloud-init; the written cloudimg re-applies it from the hetzner
        # datasource. cloud-init is rendered (+ validated) before create, so a
        # template defect fails before spend.
        cloud_init = render_cloudinit(_profile, operator, keypair.public_key)
        return _profile.default_image, cloud_init, _profile.needs_rescue_write

    def on_ready(host: Host, ssh: SshRunner, _provider: Provider) -> HostRun:
        # The workload tail: stage any uploads (transport-generic, before the
        # workload), then run the injected workload against the ready host. An
        # upload that fails raises SshError — surfaced by the scaffold and mapped
        # to a transport host-failure below (distinct from the workload's own
        # captured outcome).
        for upload in uploads:
            ssh.upload(host, upload.local, upload.remote)
        return workload.run(spec, host, ssh)

    # Seed an error result so a teardown note has a HostRun to attach to even if a
    # BaseException unwound the body before ``run`` was assigned a real outcome
    # (the BaseException re-propagates afterwards), and so a caught ``Exception``
    # below has a fallback shape.
    run = HostRun(host_spec=spec, detail="ERROR: run interrupted before completion", results=())
    note_sink: list[str] = []
    try:
        run = _with_ready_host(
            spec,
            profile,
            provider,
            ssh_factory,
            keypair,
            operator,
            rescue_writer,
            plan_create=plan_create,
            on_ready=on_ready,
            note_sink=note_sink,
        )
    except Exception as exc:  # provider / rescue / transport failure → record, don't abort
        run = HostRun(host_spec=spec, detail=f"ERROR: {type(exc).__name__}: {exc}", results=())
    # Fold the scaffold's teardown note (surfaced via ``note_sink``, never swallowed
    # by the scaffold) into the result. The note is captured in the scaffold's real
    # ``finally`` — so it is present whether the body returned, raised an
    # ``Exception`` mapped above, or raised a ``BaseException`` (which re-propagated
    # past the scaffold; in that case this fold does not run, but neither does the
    # caller need a HostRun — the abort unwinds).
    if note_sink:
        run = HostRun(host_spec=run.host_spec, detail=f"{run.detail}\n{note_sink[0]}", results=run.results)
    return run


def _with_ready_host(
    spec: HostSpec,
    profile: DistroProfile,
    provider: Provider,
    ssh_factory: Callable[[str, Keypair], SshRunner],
    keypair: Keypair,
    operator: str,
    rescue_writer: RescueWriter | None,
    *,
    plan_create: PlanCreate,
    on_ready: OnReady[R],
    note_sink: list[str],
) -> R:
    """Create → (rescue-write) → wait-ready → ``on_ready``, ALWAYS tearing down.

    The single home of the teardown-always invariant (D6 — the one thing least
    safe to duplicate across the ``run`` and ``build-image`` call sites). It is
    parameterized by two seams: ``plan_create`` decides how the host is created
    (cold or cache-restore) and ``on_ready`` is the post-readiness tail (workload
    run, or snapshot build).

    The host is destroyed in a real ``finally`` regardless of how the body left —
    a normal return, a propagating ``Exception`` (provider/rescue/transport
    failure, surfaced to the caller to map), OR a ``BaseException`` (Ctrl-C /
    ``SystemExit``): teardown fires first, THEN the exception keeps propagating so
    an aborted run still exits. Teardown is best-effort and NEVER raises: a failing
    ``destroy`` is recorded as a note and appended to ``note_sink`` (read by the
    caller) rather than raised — the collected data is the valuable artifact and a
    stubborn server is a reap-able orphan. (``provider.destroy`` already retries
    transient timeouts; this guard handles the residual case.) Surfacing the note
    via ``note_sink`` instead of folding it here keeps the except→error-``HostRun``
    mapping and the note fold in the adapter — ``build-image`` will route the same
    note to a non-zero exit, not a ``HostRun``.
    """
    host: Host | None = None
    try:
        image, cloud_init, needs_rescue = plan_create(profile)
        # ``plan_create`` resolves which image the host is created from (the cold
        # default, or a cache-restore snapshot id) — rebuild the spec with it
        # rather than mutate (immutable data); for the cold path it is the same
        # image the spec already carried.
        host = provider.create_with_cloudinit(_replace_image(spec, image), cloud_init)
        if needs_rescue:
            if rescue_writer is None:
                raise RuntimeError(
                    f"distro {profile.key!r} needs a rescue-write transform but no "
                    f"rescue_writer was provided to execute()"
                )
            rescue_writer(host, profile)
        ssh = ssh_factory(operator, keypair)
        # Readiness gating is transport-generic and the runner's job — so the tail
        # only ever runs against a ready host.
        ssh.wait_until_ready(host)
        return on_ready(host, ssh, provider)
    finally:
        # A real finally: fires on return, on a propagating Exception, AND when a
        # BaseException propagates through it (teardown happens, then it keeps
        # unwinding). The note is surfaced to the caller via ``note_sink`` — never
        # swallowed, never raised.
        if host is not None:
            teardown_note = _best_effort_destroy(provider, host)
            if teardown_note:
                note_sink.append(teardown_note)


def _replace_image(spec: HostSpec, image: str) -> HostSpec:
    """Return a copy of ``spec`` with a different provider ``image`` (immutable).

    The scaffold's ``plan_create`` may resolve a different image than the spec
    carries (a cache-restore snapshot id vs the cold default) — the host is created
    from that resolved image, so the spec is rebuilt rather than mutated.
    """
    return HostSpec(
        name=spec.name,
        image=image,
        server_type=spec.server_type,
        distro_key=spec.distro_key,
        labels=dict(spec.labels),
        firewall=spec.firewall,
    )


def _best_effort_destroy(provider: Provider, host: Host) -> str:
    """Destroy ``host``; return a warning note if it failed (never raises).

    A teardown failure must not lose the collected results, so it is reported as a
    note (the orphan is reap-able) rather than propagated.
    """
    try:
        provider.destroy(host)
        return ""
    except Exception as exc:
        return f"{TEARDOWN_WARNING_PREFIX} {host.name} ({host.id}) failed — reap it: {exc}"


def cache_aware_plan_create(
    profile: DistroProfile,
    *,
    operator: str,
    arch: str,
    target_disk: float,
    provider: Provider,
    keypair: Keypair,
    deps: ResolveDeps,
    warn: Callable[[str], None],
) -> tuple[str, str, bool]:
    """The cache-aware ``plan_create`` for ``run``: restore-on-hit, cold-on-miss (D6/D9).

    Computes the content key for ``(profile, arch, operator, recipe, upstream)``,
    looks up the cached snapshot images, and picks a **match**: an image whose
    ``vmlease-cache-key`` equals the key, whose architecture equals ``arch``, AND
    whose ``disk_size`` is ``<= target_disk`` (the restore disk-bound, D9 — a
    snapshot restores only onto a server whose disk is at least the snapshot's).

    - **Hit** → ``(match.id, render_minimal_cloudinit(operator, pubkey), False)``:
      create from the snapshot, re-authorize the fresh per-run key via the minimal
      cloud-init, and SKIP rescue-write (the prepped/written state is baked in).
    - **Miss** (no match) → the cold triple
      ``(profile.default_image, render_cloudinit(...), profile.needs_rescue_write)``.

    **Advisory + graceful** (D9, spec "the cache degrades, never breaks"): ANY
    exception during the lookup — ``content_key`` raising (e.g. an upstream resolve
    failure) or ``list_images`` failing — is caught, ``warn``-ed, and falls through
    to the cold (miss) triple. A cache problem NEVER fails the host.

    This is a standalone function: it is **not yet wired into ``execute``** (group 8
    sources ``arch`` / ``target_disk`` from the server type). It accepts both as
    injected parameters; no server-type→disk/arch mapping is performed here.
    """
    cold = (
        profile.default_image,
        render_cloudinit(profile, operator, keypair.public_key),
        profile.needs_rescue_write,
    )
    try:
        key = content_key(profile, arch, operator, deps)
        selector = f"{LABEL_PURPOSE}={PURPOSE_IMAGE_CACHE}"
        images = provider.list_images(selector)
    except Exception as exc:  # lookup failure → advisory miss, never a host failure
        warn(f"cache lookup failed for {profile.key!r} (arch={arch!r}); using cold path: {exc}")
        return cold
    match = _first_matching_image(images, key=key, arch=arch, target_disk=target_disk)
    if match is None:
        return cold
    return match.id, render_minimal_cloudinit(operator, keypair.public_key), False


def _first_matching_image(
    images: list[Image], *, key: str, arch: str, target_disk: float
) -> Image | None:
    """The first cached image matching key + arch + the disk bound (or ``None``).

    The hard restore match (D9): the content key, the architecture, and the disk
    bound (the snapshot's ``disk_size`` must be ``<= target_disk`` — an oversized
    snapshot cannot restore onto this server, so it is a graceful miss).
    """
    for image in images:
        if (
            image.labels.get(LABEL_CACHE_KEY) == key
            and image.arch == arch
            and image.disk_size <= target_disk
        ):
            return image
    return None
