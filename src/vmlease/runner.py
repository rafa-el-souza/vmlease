"""Runner — compose a matrix into host specs, plan it, and execute it.

Turns a (battery, distro-keys, server-type) matrix into labelled
:class:`~vmlease.model.HostSpec` objects, gates them through the cost guard, and
either renders a ``plan`` that makes **zero** provider calls or runs the
provision -> probe -> teardown loop. Plan and execute build their specs from the
same generator, so the plan is byte-faithful to what a real run would do.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, TypeVar

from vmlease.capabilities import canonical_requires
from vmlease.cloudinit import SYSPREP_COMMAND, render_cloudinit, render_minimal_cloudinit
from vmlease.distro import get_profile
from vmlease.imagecache import (
    LABEL_CACHE_KEY,
    content_key,
)
from vmlease.model import HostRun, HostSpec, PlanItem, Probe, ProbeTag
from vmlease.safety import (
    CostGuard,
    label_selector_purpose,
    make_run_id,
    run_label,
    validate_remote_dest,
    validate_upload_source,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vmlease.distro import DistroProfile
    from vmlease.keypair import Keypair
    from vmlease.model import Host, Image, UploadSpec
    from vmlease.providers import Provider
    from vmlease.rescue_image import ResolveDeps
    from vmlease.ssh import SshRunner
    from vmlease.workload import Workload

# The result type the ``on_ready`` seam (and therefore the scaffold) produces:
# ``run`` returns a ``HostRun``; ``build-image`` returns an ``Image``. The scaffold
# is generic over it.
R = TypeVar("R")

# The single marker a failed teardown leaves in a host's detail, shared between
# the producer here (:func:`_best_effort_destroy`) and the CLI consumer that
# greps for it to exit non-zero (D3). Pinned verbatim — imported elsewhere.
TEARDOWN_WARNING_PREFIX = "WARNING: teardown of"

# The marker a KEPT (not-torn-down, ``--keep``) host leaves in its detail, shared
# between the producer (:func:`_kept_note`) and the CLI consumer that greps for it
# to print the consolidated live-host / reap block. Distinct from
# ``TEARDOWN_WARNING_PREFIX`` so the teardown-failure reap path never fires on a
# deliberately kept host.
KEPT_HOST_PREFIX = "KEPT:"


def _kept_note(host: Host, operator: str, key_path: Path) -> str:
    """The single note a kept host leaves: how to SSH into the live machine."""
    return (
        f"{KEPT_HOST_PREFIX} {host.name} ({host.id}) is LIVE at {host.ipv4} "
        f"— ssh -i {key_path} {operator}@{host.ipv4}"
    )


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
        requires: The vmlease-provided capabilities every host in the run needs
            (a provisioning attribute, default-off — ``()`` means no capability).
            The CLI lifts this from the battery and canonicalizes it; the runner
            propagates it onto every :class:`~vmlease.model.HostSpec`.
    """

    workload: Workload
    distro_keys: tuple[str, ...]
    server_type: str
    run_token: str
    firewall: str = ""
    uploads: tuple[UploadSpec, ...] = ()
    requires: tuple[str, ...] = ()


def build_host_specs(matrix: Matrix) -> list[HostSpec]:
    """Turn a :class:`Matrix` into one labelled :class:`HostSpec` per distro.

    Pure + deterministic (the run-id derives from ``matrix.run_token``). Every
    spec carries the ``vmlease=<run-id>`` label so the safety layer can reap
    the whole run. Raises :class:`~vmlease.distro.UnknownDistroError` for an
    unknown distro key.
    """
    run_id = make_run_id(matrix.run_token)
    labels = run_label(run_id)
    requires = canonical_requires(matrix.requires)
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
                requires=requires,
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

    ``plan`` NEVER does a cache lookup — the restore hit/miss decision (which
    ``list_images`` the provider) is **provision-time only**, inside ``execute``.
    So ``plan`` makes **zero** provider calls even when a cache is present; it
    always shows the cold image (a hit only changes which image is *created*, not
    the host set), keeping the dry-run faithful and call-free.
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
            requires=s.requires,
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
#   cache-restore candidate returns a snapshot id + minimal cloud-init + ``False`` on
#   a hit. ``build-image`` is always cold. ``needs_rescue`` overrides the profile's
#   own flag so a restore can skip the rescue-write a miss would perform.
# - ``OnReady`` is the post-readiness tail, generic over its result ``R``: ``run``
#   uploads + runs the workload → ``HostRun``; ``build-image`` syspreps + powers off
#   + snapshots → ``Image``. It takes ``provider`` because the snapshot tail needs
#   it — it is deliberately NOT a ``Workload`` (which stays ssh-only).
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
    resolve_deps: ResolveDeps | None = None,
    reap_bad_cache_image: bool = False,
    keep: bool = False,
) -> list[HostRun]:
    """Per host: provision -> (rescue-write) -> run workload -> tear down (unless ``keep``).

    Each host is **isolated**: it is created, transformed, run, and destroyed in
    its own ``try/finally`` BEFORE the next host starts — so a host dies seconds
    after its workload (lower cost / shorter exposure) and, crucially, **one host's
    failure never discards another's results**. A host that fails to provision /
    rescue-write / become reachable is recorded as a ``HostRun`` with an error
    detail and zero results (NOT a raise), so :func:`execute` always returns
    one ``HostRun`` per requested host and the caller always writes a results file.
    The injected ``matrix.workload`` owns what runs on each ready host. The keypair
    is cleaned once at the end — UNLESS ``keep`` is set, which leaves the host(s)
    standing and skips that cleanup (see ``keep`` below).

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

    ``resolve_deps`` (injected, optional) activates the **cache-aware** restore
    path: when present, each host first tries the content-addressed cached image
    (a hit restores from the snapshot + skips rescue-write), falling back to the
    cold path on a miss / lookup failure / create-from-image failure (G3/G9). The
    cache is **advisory** — it never fails a host. When ``resolve_deps is None``
    the behavior is the **pure cold path, byte-identical to a non-caching run** —
    so every existing ``execute``/abort/parallelism test holds unchanged.
    ``reap_bad_cache_image`` (opt-in, default off) reaps the source image when a
    *restored* host fails readiness (G4); default is a hint only (the image is
    named in the failure detail but kept, so a real fault is not masked).
    ``keep`` (opt-in, default off) leaves every host RUNNING (billable) instead of
    tearing it down — each kept host carries a KEPT note (how to SSH in) and the
    keypair is NOT cleaned, so the printed ``ssh -i <path>`` points at a live file.
    """
    validate_uploads(matrix)
    specs = build_host_specs(matrix)
    guard = cost_guard or CostGuard()
    guard.check([s.server_type for s in specs])

    def _one(spec: HostSpec) -> HostRun:
        return _run_one_host(
            spec, matrix.workload, provider, ssh_factory, keypair, operator, rescue_writer, matrix.uploads,
            resolve_deps=resolve_deps, reap_bad_cache_image=reap_bad_cache_image, keep=keep,
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
        # Under ``--keep`` the private key must SURVIVE the run so the printed
        # ``ssh -i <path>`` points at a real file the operator can use against the
        # still-live host(s). Otherwise the key dir is reaped as usual.
        if not keep:
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
    *,
    resolve_deps: ResolveDeps | None = None,
    reap_bad_cache_image: bool = False,
    keep: bool = False,
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

    **Cache consumption (D3/D6/G3/G4/G9):** when ``resolve_deps`` is provided, the
    host first tries a content-addressed cached snapshot. The candidate list is
    ``[restore, cold]`` on a cache hit (a create-from-image failure degrades to the
    cold candidate — G3) and ``[cold]`` on a miss / lookup failure (advisory, G9).
    ``run`` NEVER builds — it only consumes (D3). When ``resolve_deps is None`` the
    candidate list is exactly ``[cold]`` — byte-identical to the non-caching run,
    so the existing tests hold unchanged. A *restored* host that fails AFTER create
    (rescue/readiness/workload) is a host failure naming the source image, NOT a
    cold re-provision (G4); ``reap_bad_cache_image`` additionally reaps that image
    (default: hint only — name it, keep it, don't mask a real fault).
    """
    from vmlease.model import HostRun

    profile = get_profile(spec.distro_key)

    def cold_plan_create(_profile: DistroProfile) -> tuple[str, str, bool]:
        # ``run``'s COLD path: the profile's default image + the full cloud-init +
        # the profile's own rescue flag. A rescue-write distro's base host gets the
        # SAME cloud-init; the written cloudimg re-applies it from the hetzner
        # datasource. cloud-init is rendered (+ validated) before create, so a
        # template defect fails before spend.
        cloud_init = render_cloudinit(_profile, operator, keypair.public_key, spec.requires)
        return _profile.default_image, cloud_init, _profile.needs_rescue_write

    # Build the candidate list. The cold candidate is ALWAYS the fallback; a cache
    # hit prepends a restore candidate (and records the source image for the G4
    # naming/reap path). resolve_deps None → pure cold (no lookup, no provider
    # server_type_disk call) — byte-identical to a non-caching run.
    plan_creates: list[PlanCreate] = [cold_plan_create]
    restore_image_id: str | None = None
    if resolve_deps is not None:
        restore_image_id = _resolve_cache_hit_image(
            spec, profile, provider, operator, resolve_deps
        )
        if restore_image_id is not None:
            hit_image = restore_image_id

            def restore_plan_create(_profile: DistroProfile) -> tuple[str, str, bool]:
                # The HIT: create from the snapshot, re-authorize the fresh per-run
                # key via the minimal cloud-init, and SKIP rescue-write (the prepped
                # state is baked into the snapshot).
                return hit_image, render_minimal_cloudinit(operator, keypair.public_key), False

            plan_creates = [restore_plan_create, cold_plan_create]

    def on_ready(host: Host, ssh: SshRunner, _provider: Provider) -> HostRun:
        # The workload tail: stage any uploads (transport-generic, before the
        # workload), then run the injected workload against the ready host. An
        # upload that fails raises SshError — surfaced by the scaffold and mapped
        # to a transport host-failure below (distinct from the workload's own
        # captured outcome).
        for upload in uploads:
            ssh.upload(host, upload.local, upload.remote)
        return workload.run(spec, host, ssh)

    # The restore candidate is always index 0 when present; ``chosen`` records which
    # candidate actually created the host so a terminal post-create failure can be
    # attributed to the restore (G4) vs the cold path.
    chosen: list[int] = []
    restored_index = 0 if restore_image_id is not None else -1

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
            plan_creates=plan_creates,
            on_ready=on_ready,
            note_sink=note_sink,
            on_chosen=chosen.append,
            keep=keep,
        )
    except Exception as exc:  # provider / rescue / transport failure → record, don't abort
        if chosen and chosen[0] == restored_index and restore_image_id is not None:
            # G4: a RESTORED host was created but failed AFTER create (rescue /
            # readiness / workload). This is NOT a cold re-provision (that would
            # double-provision) — record a host failure naming the source image,
            # and (opt-in) reap that image so a poisoned snapshot stops causing
            # repeated failures.
            run = HostRun(
                host_spec=spec,
                detail=_restore_failure_detail(
                    exc, restore_image_id, provider, reap=reap_bad_cache_image
                ),
                results=(),
            )
        else:
            run = HostRun(host_spec=spec, detail=f"ERROR: {type(exc).__name__}: {exc}", results=())
    # Fold the scaffold's teardown note (surfaced via ``note_sink``, never swallowed
    # by the scaffold) into the result. The note is captured in the scaffold's real
    # ``finally`` — so it is present whether the body returned, raised an
    # ``Exception`` mapped above, or raised a ``BaseException`` (which re-propagated
    # past the scaffold; in that case this fold does not run, but neither does the
    # caller need a HostRun — the abort unwinds).
    # Stamp the restore decision so the results are observable: a host created from
    # the restore candidate (chosen index 0 == restored_index) carries the snapshot
    # id; a cold/miss provision carries None. Covers the success path AND G4 (a
    # restored host that failed after create — it WAS restored).
    was_restored = bool(chosen) and chosen[0] == restored_index and restore_image_id is not None
    run = replace(run, restored_image=restore_image_id if was_restored else None)
    if note_sink:
        run = replace(run, detail=f"{run.detail}\n{note_sink[0]}")
    return run


def _resolve_cache_hit_image(
    spec: HostSpec,
    profile: DistroProfile,
    provider: Provider,
    operator: str,
    deps: ResolveDeps,
) -> str | None:
    """Return the cached snapshot image id to restore from, or ``None`` (a miss).

    Sources the restore arch + disk bound from the server type (D9): a
    ``server_type_disk`` failure is advisory — it warns and degrades to a miss
    (cold), never failing the host (G9). The hit/miss decision itself is
    :func:`_lookup_cache_image`, which already degrades a lookup failure to a miss
    (``None``). On a hit the matched snapshot's id is returned; the minimal restore
    cloud-init is rendered later, on the restore candidate that needs it (no
    cloud-init is rendered during the lookup).
    """
    from vmlease.safety import server_type_arch

    def _warn(msg: str) -> None:
        # Advisory: the cache is best-effort, so a warning is the loudest a cache
        # problem ever gets — it never fails the host.
        import sys

        print(f"warning: {msg}", file=sys.stderr)

    arch = server_type_arch(spec.server_type)
    try:
        target_disk = provider.server_type_disk(spec.server_type)
    except Exception as exc:  # disk lookup failure → advisory miss (cold), never a host failure
        _warn(
            f"could not read disk size for server type {spec.server_type!r} "
            f"(treating as a cache miss → cold path): {exc}"
        )
        return None
    match = _lookup_cache_image(
        profile,
        operator=operator,
        arch=arch,
        requires=spec.requires,
        target_disk=target_disk,
        provider=provider,
        deps=deps,
        warn=_warn,
    )
    return match.id if match is not None else None


def _lookup_cache_image(
    profile: DistroProfile,
    *,
    operator: str,
    arch: str,
    requires: tuple[str, ...],
    target_disk: float,
    provider: Provider,
    deps: ResolveDeps,
    warn: Callable[[str], None],
) -> Image | None:
    """Find the cached snapshot to restore from for this group, or ``None`` (a miss).

    Pure cache lookup (D6/D9): compute the content key for
    ``(profile, arch, operator, requires, recipe, upstream)`` — ``requires`` is the
    D-H spine read, so a docker run looks up the docker variant and a docker-less
    run the docker-less one — ``list_images`` the cache, and
    pick the first **match** — an image whose ``vmlease-cache-key`` equals the key,
    whose architecture equals ``arch``, AND whose ``disk_size`` is ``<= target_disk``
    (the restore disk-bound, D9: a snapshot restores only onto a server whose disk is
    at least the snapshot's). Renders NO cloud-init — the caller renders the right one
    on the branch that needs it (cold renders cold; the restore branch renders
    minimal), so the run-path cache check never renders a cloud-init it discards.

    **Advisory + graceful** (D9, "the cache degrades, never breaks"): ANY exception
    during the lookup — ``content_key`` raising (e.g. an upstream resolve failure) or
    ``list_images`` failing — is caught, ``warn``-ed, and reported as a miss
    (``None``). A cache problem NEVER fails the host.
    """
    try:
        key = content_key(profile, arch, operator, requires, deps)
        images = provider.list_images(label_selector_purpose())
    except Exception as exc:  # lookup failure → advisory miss, never a host failure
        warn(f"cache lookup failed for {profile.key!r} (arch={arch!r}); using cold path: {exc}")
        return None
    return _first_matching_image(images, key=key, arch=arch, target_disk=target_disk)


def _restore_failure_detail(
    exc: Exception, image_id: str, provider: Provider, *, reap: bool
) -> str:
    """Build the G4 host-failure detail for a restored host that failed post-create.

    Names the source image (so the operator can investigate / reap it manually).
    When ``reap`` is set, additionally deletes that image (best-effort — a failed
    reap is appended as a note, never raised) so a poisoned snapshot stops causing
    repeated readiness failures. Default (``reap=False``) is a hint only — the
    image is named but KEPT, so a real (non-image) fault is not masked by reaping a
    good snapshot.
    """
    base = (
        f"ERROR: {type(exc).__name__}: {exc} "
        f"(restored from cache image {image_id})"
    )
    if not reap:
        return f"{base}; the image was KEPT (pass --reap-bad-cache-image to reap it)"
    try:
        provider.delete_image(image_id)
    except Exception as reap_exc:
        return f"{base}; reap of the bad cache image {image_id} FAILED: {reap_exc}"
    return f"{base}; reaped the bad cache image {image_id}"


def _with_ready_host(
    spec: HostSpec,
    profile: DistroProfile,
    provider: Provider,
    ssh_factory: Callable[[str, Keypair], SshRunner],
    keypair: Keypair,
    operator: str,
    rescue_writer: RescueWriter | None,
    *,
    plan_creates: list[PlanCreate],
    on_ready: OnReady[R],
    note_sink: list[str],
    on_chosen: Callable[[int], None] | None = None,
    keep: bool = False,
) -> R:
    """Create → (rescue-write) → wait-ready → ``on_ready``, ALWAYS tearing down.

    The single home of the teardown-always invariant (D6 — the one thing least
    safe to duplicate across the ``run`` and ``build-image`` call sites). It is
    parameterized by two seams: ``plan_creates`` is an **ordered list of candidate**
    ``plan_create`` functions, each deciding how the host is created (cold or
    cache-restore), and ``on_ready`` is the post-readiness tail (workload run, or
    snapshot build).

    **Candidate fallback (D6, the run cache G3/G4 cutoff):** the candidates are
    tried in order. A candidate's ``create_with_cloudinit`` (the **create** step)
    failure advances to the next candidate — so a cache *hit* whose snapshot was
    pruned mid-flight (no server created) degrades to the cold candidate (G3). But
    once a host **is** created, any later failure (rescue-write / readiness /
    ``on_ready``) is **terminal**: it propagates (and the created host is torn
    down) rather than re-trying the next candidate — a restored host that fails
    readiness is a host failure, NOT a cold re-provision (G4). The last candidate's
    create failure also propagates. ``build-image`` / the cold run path pass a
    single-candidate list (no fallback).

    The host is destroyed in a real ``finally`` regardless of how the body left —
    a normal return, a propagating ``Exception`` (provider/rescue/transport
    failure, surfaced to the caller to map), OR a ``BaseException`` (Ctrl-C /
    ``SystemExit``): teardown fires first, THEN the exception keeps propagating so
    an aborted run still exits. The ONE teardown ``finally`` covers whichever
    candidate created the host (restore or cold). Teardown is best-effort and NEVER
    raises: a failing ``destroy`` is recorded as a note and appended to
    ``note_sink`` (read by the caller) rather than raised — the collected data is
    the valuable artifact and a stubborn server is a reap-able orphan.
    (``provider.destroy`` already retries transient timeouts; this guard handles
    the residual case.) Surfacing the note via ``note_sink`` instead of folding it
    here keeps the except→error-``HostRun`` mapping and the note fold in the adapter
    — ``build-image`` will route the same note to a non-zero exit, not a ``HostRun``.
    """
    host: Host | None = None
    try:
        host, needs_rescue, chosen = _create_first_viable(spec, profile, provider, plan_creates)
        # ``on_chosen`` lets the caller record WHICH candidate created the host
        # (only ever invoked after a successful create), so a terminal post-create
        # failure can be attributed to the restore vs cold candidate (the G4 path).
        if on_chosen is not None:
            on_chosen(chosen)
        # Past this point a host EXISTS — every failure is terminal (no fallback to
        # the next candidate), so a restored host that fails readiness is a host
        # failure, never a silent cold re-provision (G4).
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
            if keep:
                # ``--keep``: leave the live host standing (regardless of outcome —
                # success, probe-fail, or prep hard-abort) and surface exactly one
                # KEPT note via note_sink so the operator can SSH in and later reap.
                note_sink.append(_kept_note(host, operator, keypair.private_key_path))
            else:
                teardown_note = _best_effort_destroy(provider, host)
                if teardown_note:
                    note_sink.append(teardown_note)


def _create_first_viable(
    spec: HostSpec,
    profile: DistroProfile,
    provider: Provider,
    plan_creates: list[PlanCreate],
) -> tuple[Host, bool, int]:
    """Try each candidate's create; return the first ``(host, needs_rescue, idx)`` made.

    A candidate's ``plan_create`` resolves which image the host is created from (a
    cache-restore snapshot id, or the cold default) — the spec is rebuilt with it
    rather than mutated (immutable data). A **create** failure on a non-final
    candidate is the G3 cutoff: it falls through to the next candidate (e.g. a hit
    whose snapshot vanished → cold). The final candidate's create failure
    propagates. Returns the created host, the chosen candidate's ``needs_rescue``
    flag (a hit restore returns ``False``; a cold candidate returns the profile's
    own flag), and the chosen candidate's index (so the caller can attribute a
    later terminal failure to the restore vs cold candidate — the G4 path).
    """
    # The final candidate is created OUTSIDE the fallback try (its failure
    # propagates), so every loop branch either returns or raises — no unreachable
    # tail. plan_creates is always non-empty (the cold candidate is always
    # present).
    for idx, plan_create in enumerate(plan_creates[:-1]):
        image, cloud_init, needs_rescue = plan_create(profile)
        try:
            host = provider.create_with_cloudinit(_replace_image(spec, image), cloud_init)
        except Exception:
            continue  # G3: this candidate's create failed → try the next
        return host, needs_rescue, idx
    final_idx = len(plan_creates) - 1
    image, cloud_init, needs_rescue = plan_creates[final_idx](profile)
    host = provider.create_with_cloudinit(_replace_image(spec, image), cloud_init)
    return host, needs_rescue, final_idx


def _replace_image(spec: HostSpec, image: str) -> HostSpec:
    """Return a copy of ``spec`` with a different provider ``image`` (immutable).

    The scaffold's ``plan_create`` may resolve a different image than the spec
    carries (a cache-restore snapshot id vs the cold default) — the host is created
    from that resolved image, so the spec is rebuilt rather than mutated.

    Uses :func:`dataclasses.replace` so every other field (incl. ``requires``)
    rides along verbatim and a new field can't silently drop out of the copy.
    """
    return replace(spec, image=image)


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


# --------------------------------------------------------------------------- #
# build-image: the snapshot ``on_ready`` tail + the build driver (D6, G1/G2)
# --------------------------------------------------------------------------- #

# The default bound (number of poll attempts) on the wait-for-off loop. Bounded
# like ``destroy`` so a host that never powers off becomes an abort (G2), not a
# hang. The wall-clock is the caller's injected ``sleep`` x attempts; the budget
# is an attempt count so tests stay clock-free.
DEFAULT_POWEROFF_ATTEMPTS = 30


class PoweroffTimeoutError(RuntimeError):
    """The host did not reach the ``"off"`` state within the bounded wait (G2).

    Raised by :func:`_wait_until_off` so the snapshot tail aborts before any
    ``create_image`` — a running host is never snapshotted.
    """


def _wait_until_off(
    provider: Provider,
    server_id: str,
    *,
    attempts: int = DEFAULT_POWEROFF_ATTEMPTS,
    sleep: Callable[[float], None],
    poll_interval: float = 2.0,
) -> None:
    """Poll ``provider.server_status`` until ``"off"``; raise on timeout (G2).

    Bounded like :meth:`HetznerProvider.destroy`: at most ``attempts`` polls, with
    the injected ``sleep`` between them (no real clock — tests pass a no-op sleep
    and a small attempt budget). On exhaustion raises :class:`PoweroffTimeoutError`
    so the caller aborts the build and tears the builder down; never silently
    snapshots a running host. A ``server_status`` that raises (provider failure)
    propagates — also an abort.
    """
    for attempt in range(attempts):
        if provider.server_status(server_id) == "off":
            return
        if attempt < attempts - 1:
            sleep(poll_interval)
    raise PoweroffTimeoutError(
        f"server {server_id} did not reach the off state within {attempts} polls"
    )


def make_snapshot_on_ready(
    description: str,
    labels: dict[str, str],
    *,
    sleep: Callable[[float], None],
    poweroff_attempts: int = DEFAULT_POWEROFF_ATTEMPTS,
) -> OnReady[Image]:
    """Build the snapshot ``on_ready`` tail: sysprep → poweroff → wait-off → snapshot.

    Returns an ``on_ready(host, ssh, provider) -> Image`` for the per-host scaffold
    (D6). The tail, in order:

    1. **Sysprep** (D7/F-009): clear ``/etc/machine-id`` + the dbus id over SSH. A
       **non-zero exit raises** (G1) — a non-sysprepped host carries a shared
       machine-id and MUST NEVER be snapshotted. The abort propagates; the scaffold
       still tears the builder down.
    2. **Power off** then :func:`_wait_until_off` (G2): a poweroff failure or a
       wait-for-off timeout raises **before** any ``create_image`` — a running host
       is never captured.
    3. **Snapshot**: ``provider.create_image(host.id, description, labels)`` with the
       ``labels`` applied **in the create call** (atomic, D1) — the caller supplies
       the cache-key description + labels.

    ``description`` / ``labels`` are captured here; ``sleep`` is injected so the
    wait stays clock-free.
    """

    def on_ready(host: Host, ssh: SshRunner, provider: Provider) -> Image:
        sysprep = Probe(
            id="_sysprep",
            title="sysprep machine-id",
            command=SYSPREP_COMMAND,
            tag=ProbeTag.MUTATING_HOST_ROOT,
        )
        result = ssh.run_probe(host, sysprep)
        if result.exit_code != 0:
            # G1: never snapshot a non-sysprepped host (machine-id would be shared).
            raise RuntimeError(
                f"sysprep failed on {host.name} ({host.id}) "
                f"(exit {result.exit_code}): {result.stderr or result.stdout}"
            )
        # G2: poweroff + bounded wait-for-off before the snapshot; any failure here
        # aborts before create_image.
        provider.power_off(host.id)
        _wait_until_off(provider, host.id, attempts=poweroff_attempts, sleep=sleep)
        return provider.create_image(host.id, description, labels)

    return on_ready


def build_one_image(
    spec: HostSpec,
    profile: DistroProfile,
    provider: Provider,
    ssh_factory: Callable[[str, Keypair], SshRunner],
    keypair: Keypair,
    operator: str,
    rescue_writer: RescueWriter | None,
    *,
    on_ready: OnReady[Image],
    note_sink: list[str],
) -> Image:
    """Provision ONE builder, run the snapshot tail, ALWAYS tear the builder down.

    The ``build-image`` driver (D6): it builds the **cold** ``plan_create`` (the
    full ``render_cloudinit`` prep — ``build-image`` always prepares fully) and
    runs the per-host scaffold with the snapshot ``on_ready`` tail. The scaffold's
    teardown-always ``finally`` reaps the builder regardless of how the body left —
    a successful snapshot OR an abort (sysprep G1 / poweroff-or-timeout G2 /
    provider error). Those aborts **propagate as exceptions** (the builder is still
    torn down); they are deliberately NOT caught-and-swallowed into a success.

    The teardown note is surfaced via ``note_sink`` (the scaffold appends to it in
    its real ``finally``) so ``_cmd_build_image`` can route a teardown failure to a
    non-zero exit + reap hint — exactly as the run adapter
    folds it into a ``HostRun`` detail.
    """

    def plan_create(_profile: DistroProfile) -> tuple[str, str, bool]:
        # build-image is ALWAYS cold: the cold default image + the full cloud-init +
        # the profile's own rescue flag (a rescue-write distro's builder is written
        # then prepped before the snapshot captures it).
        cloud_init = render_cloudinit(_profile, operator, keypair.public_key, spec.requires)
        return _profile.default_image, cloud_init, _profile.needs_rescue_write

    return _with_ready_host(
        spec,
        profile,
        provider,
        ssh_factory,
        keypair,
        operator,
        rescue_writer,
        plan_creates=[plan_create],
        on_ready=on_ready,
        note_sink=note_sink,
    )
