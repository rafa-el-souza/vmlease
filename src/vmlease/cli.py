#!/usr/bin/env python3
"""vmlease CLI — ``plan`` · ``run`` · ``status`` · ``lint`` · ``reap``.

- ``plan``   dry-run: what WOULD be provisioned (zero provider calls).
- ``run``    provision -> probe -> ALWAYS tear down; write a timestamped results
             file. Gated by a confirm-before-create prompt (``--yes`` to skip).
- ``status`` list the live hosts carrying a run's label.
- ``lint``   shellcheck every probe in a battery bundle; severity-gated exit code.
- ``reap``   destroy every host carrying a run's label (the orphan backstop).
- ``reap-images`` reap cached snapshot images by ``--distro`` / ``--older-than`` /
             ``--superseded`` (best-effort + idempotent; ``--dry-run`` previews).

A battery is a **TOML bundle** (a ``battery.toml`` manifest plus optional
co-located ``.sh`` scripts); the ``--battery`` flag points at the manifest.

Invoked as the ``vmlease`` console script. The Hetzner provider relies on the
operator's already-active ``hcloud`` context; the token is never read here.

    vmlease plan --battery <battery.toml> --run-token <slug>
    vmlease run  --battery <battery.toml> --run-token <slug> \
        --operator probe --results-dir <dir> [--yes]
    vmlease lint --battery <battery.toml> [--severity warning] [--require-shellcheck]
    vmlease reap --run-token <slug>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vmlease.rescue_image import ResolveDeps

from vmlease.archbuild import (
    ArchBuildError,
    build_live_rescue_writer,
    build_live_resolve_deps,
    ensure_arch_keyring,
    live_subprocess_run,
)
from vmlease.battery import (
    BatteryError,
    lint_battery,
    load_battery,
    structural_violations,
)
from vmlease.capabilities import canonical_requires
from vmlease.distro import DEFAULT_DISTRO_KEYS, UnknownDistroError, get_profile
from vmlease.imagecache import (
    LABEL_ARCH,
    LABEL_CACHE_KEY,
    LABEL_DISTRO,
    base_fingerprint,
    cache_labels,
    content_key_from_base_fp,
    resolve_current_keys,
    superseded,
)
from vmlease.keypair import Keypair, KeypairError, generate_keypair
from vmlease.model import Battery, HostRun, HostSpec, Image, UploadSpec
from vmlease.providers import HetznerProvider, Provider, ProviderError, ProviderQuotaError
from vmlease.results import IncrementalResultsWriter
from vmlease.runner import (
    TEARDOWN_WARNING_PREFIX,
    Matrix,
    RescueWriter,
    build_one_image,
    execute,
    make_snapshot_on_ready,
    plan,
)
from vmlease.safety import (
    DEFAULT_MAX_HOSTS,
    DEFAULT_MAX_IMAGES,
    CostGuard,
    CostGuardError,
    ImageQuotaError,
    ImageQuotaGuard,
    UploadError,
    label_selector_purpose,
    make_run_id,
    reap,
    run_label,
    server_type_arch,
)
from vmlease.shellcheck import (
    ShellcheckFinding,
    ShellcheckRunner,
    findings_at_or_above,
    shellcheck_battery,
)
from vmlease.ssh import OpenSshRunner
from vmlease.summary import overall_exit_code, summarize_results, summary_filename, write_summary
from vmlease.workload import ProbeWorkload, Workload


def _matrix_has_rescue_write(matrix: Matrix) -> bool:
    """``True`` iff any distro in the matrix needs a rescue-write transform."""
    return any(get_profile(k).needs_rescue_write for k in matrix.distro_keys)


def _build_rescue_writer(keypair: Keypair, ssh_key_name: str, rescue_key_path: str) -> RescueWriter:
    """Construct the live rescue-writer: set up the pinned-key gpg keyring first.

    Two distinct keys are in play, and conflating them was the first-run bug:
    - the THROWAWAY keypair gives the operator (``probe``) access into the booted
      host via cloud-init ``authorized_keys`` — used by the probe SSH, NOT here;
    - the REGISTERED ssh key (``ssh_key_name``, injected by ``enable-rescue``) is
      what the RESCUE SYSTEM accepts for root login. So the rescue SSH MUST use
      that registered key's local private half (``rescue_key_path``), not the
      throwaway. The keyring (pinned arch-boxes key only) is built alongside the
      throwaway keypair so the trust gate verifies against the pinned key.
    """
    from vmlease.archbuild import live_subprocess_run

    keyring_path = str(keypair.directory / "arch-boxes.gpg")
    ensure_arch_keyring(keyring_path, live_subprocess_run)
    return build_live_rescue_writer(rescue_key_path, ssh_key_name, keyring_path)


def _build_run_resolve_deps(matrix: Matrix, keypair: Keypair) -> ResolveDeps:
    """Build the run's cache-lookup ``ResolveDeps`` (the keyring only when needed).

    Mirrors ``build-image``'s deps construction but keyed on the matrix: the pinned
    ``arch-boxes`` keyring is set up ONLY when the matrix contains a rescue-write
    distro (a native-only matrix never touches gpg). The keyring lives under the
    per-run keypair's directory (it exists by now — the cache lookup is
    provision-time, after the keypair is generated). The deps feed the advisory
    cache lookup; a resolve failure inside it degrades to the cold path (G9).
    """
    keyring_path = str(keypair.directory / "arch-boxes.gpg")
    if _matrix_has_rescue_write(matrix):
        ensure_arch_keyring(keyring_path, live_subprocess_run)
    return build_live_resolve_deps(keyring_path)


def _parse_upload(value: str) -> UploadSpec:
    """Parse one ``--upload LOCAL[:REMOTE]`` value into an :class:`UploadSpec`.

    Splits on the **first** ``:`` for ``LOCAL:REMOTE``; with no ``:`` the remote
    defaults to ``~/<basename(local)>``. Validation (refuse a symlink / bad dest
    / etc.) happens later in the safety layer, before any spend.
    """
    local_str, sep, remote = value.partition(":")
    local = Path(local_str)
    if not sep:
        remote = f"~/{local.name}"
    return UploadSpec(local=local, remote=remote)


def _matrix_from_args(
    args: argparse.Namespace,
    workload: Workload,
    requires: tuple[str, ...] = (),
) -> Matrix:
    distro_keys = tuple(k.strip() for k in args.distros.split(",") if k.strip())
    uploads = tuple(_parse_upload(v) for v in (args.upload or ()))
    return Matrix(
        workload=workload,
        distro_keys=distro_keys,
        server_type=args.server_type,
        run_token=args.run_token,
        firewall=args.firewall,
        uploads=uploads,
        requires=requires,
    )


def _warn_battery(battery: Battery) -> None:
    """Print non-fatal authoring warnings (vacuous ok) to stderr."""
    for w in lint_battery(battery):
        print(f"warning: {w}", file=sys.stderr)


def _cmd_plan(args: argparse.Namespace) -> int:
    try:
        battery = load_battery(Path(args.battery))
        matrix = _matrix_from_args(
            args, ProbeWorkload(battery), canonical_requires(battery.requires)
        )
        items = plan(matrix, cost_guard=CostGuard(max_hosts=args.max_hosts))
    except (BatteryError, UnknownDistroError, CostGuardError, UploadError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _warn_battery(battery)
    print(f"battery: {battery.name}  ({len(battery.probes)} probes)")
    print(f"plan: {len(items)} host(s) — NOTHING PROVISIONED (dry-run)")
    for it in items:
        requires_note = f"  requires={list(it.requires)}" if it.requires else ""
        print(f"  - {it.host_name}  [{it.distro_key}]  {it.image}  {it.server_type}  {it.workload_summary}{requires_note}")
    return 0


def _confirm(prompt: str, *, assume_yes: bool, reader: Callable[[str], str]) -> bool:
    """Confirm-before-create gate. ``reader`` is injected so tests drive it."""
    if assume_yes:
        return True
    answer = reader(prompt).strip().lower()
    return answer in ("y", "yes")


def _cmd_run(args: argparse.Namespace, *, reader: Callable[[str], str] = input) -> int:
    try:
        battery = load_battery(Path(args.battery))
        matrix = _matrix_from_args(
            args, ProbeWorkload(battery), canonical_requires(battery.requires)
        )
        items = plan(matrix, cost_guard=CostGuard(max_hosts=args.max_hosts))
    except (BatteryError, UnknownDistroError, CostGuardError, UploadError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    _warn_battery(battery)
    print(f"battery: {battery.name}  ({len(battery.probes)} probes)")
    print(f"about to PROVISION {len(items)} real host(s) (billable):")
    for it in items:
        requires_note = f"  requires={list(it.requires)}" if it.requires else ""
        print(f"  - {it.host_name}  [{it.distro_key}]  {it.image}  {it.server_type}{requires_note}")
    if not _confirm("Proceed with provisioning? [y/N]: ", assume_yes=args.yes, reader=reader):
        print("aborted — nothing provisioned.")
        return 0

    run_id = make_run_id(args.run_token)
    try:
        keypair = generate_keypair(run_id)
    except KeypairError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    provider = HetznerProvider()

    def _ssh_factory(operator: str, kp: object) -> OpenSshRunner:
        return OpenSshRunner(operator, keypair.private_key_path, probe_timeout_default=args.probe_timeout)

    # A rescue-writer is only built when the matrix contains a needs_rescue_write
    # distro (e.g. arch); it transforms the base host into the target distro
    # before probing. It needs the REGISTERED ssh key (name + local private half)
    # for root access into the rescue system — distinct from the throwaway probe
    # key. Both must be present, or the rescue SSH cannot authenticate.
    rescue_writer = None
    if _matrix_has_rescue_write(matrix):
        if not args.ssh_key or not args.ssh_key_path:
            print(
                "error: a rescue-write distro (e.g. arch) requires --ssh-key "
                "(registered name) AND --ssh-key-path (its local private half) — "
                "required because a cache miss may still rescue-write (hit/miss is "
                "only known after provisioning)",
                file=sys.stderr,
            )
            keypair.cleanup()
            return 2
        rescue_writer = _build_rescue_writer(keypair, args.ssh_key, args.ssh_key_path)

    # The cache is ADVISORY: build the run's resolve deps (sets up the pinned arch
    # keyring only when the matrix has a rescue-write distro — a native-only matrix
    # never touches gpg) and let execute() try a cached snapshot per host, falling
    # back to the cold path on any miss/failure. run NEVER builds (D3).
    resolve_deps = _build_run_resolve_deps(matrix, keypair)

    writer = IncrementalResultsWriter(Path(args.results_dir), run_id, args.timestamp)

    def _persist(host_run: HostRun) -> None:
        # Adapt the writer's path-returning add() to the None-returning sink hook.
        writer.add(host_run)

    try:
        host_runs = execute(
            matrix, provider, _ssh_factory, keypair, args.operator,
            cost_guard=CostGuard(max_hosts=args.max_hosts), rescue_writer=rescue_writer,
            max_parallel=args.parallel, on_host_complete=_persist,
            resolve_deps=resolve_deps, reap_bad_cache_image=args.reap_bad_cache_image,
        )
    except (KeyboardInterrupt, SystemExit):
        # Aborted mid-run: the per-host hosts that finished are already on disk
        # (writer.add ran for each). Reap the run label so no billable host is
        # left orphaned, note where the partial results are, then RE-RAISE so the
        # process still exits on the interrupt. The backstop reap is itself a
        # provider call that MAY fail; if it does, surface an actionable manual
        # reap hint but never let the ProviderError mask the original interrupt.
        try:
            reaped = reap(provider, run_id)
            print(f"aborted — reaped {len(reaped)} host(s) labelled vmlease={run_id}", file=sys.stderr)
            for h in reaped:
                print(f"  - reaped {h.name} ({h.id})", file=sys.stderr)
        except ProviderError as exc:
            print(
                f"aborted — backstop reap ALSO failed ({exc}); host(s) labelled "
                f"vmlease={run_id} may still be LIVE — run "
                f"`vmlease reap --run-token {args.run_token}` to clean up",
                file=sys.stderr,
            )
        print(f"partial results: {writer.path}", file=sys.stderr)
        raise
    except (ProviderError, CostGuardError, ArchBuildError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    teardown_failures = [hr for hr in host_runs if TEARDOWN_WARNING_PREFIX in hr.detail]
    if teardown_failures:
        # A teardown failed: a billable host may still be live. Reap the run label
        # as a backstop and surface the failure prominently — non-zero exit so a
        # caller (CI) cannot mistake a leaked host for a clean run. The backstop
        # reap is itself a provider call that MAY fail; if it does, surface an
        # actionable manual reap hint — but still exit non-zero (never a traceback).
        try:
            reaped = reap(provider, run_id)
            print(
                f"ERROR: teardown failed for {len(teardown_failures)} host(s); "
                f"reaped {len(reaped)} host(s) labelled vmlease={run_id}",
                file=sys.stderr,
            )
            for h in reaped:
                print(f"  - reaped {h.name} ({h.id})", file=sys.stderr)
        except ProviderError as exc:
            print(
                f"ERROR: teardown failed for {len(teardown_failures)} host(s); "
                f"backstop reap ALSO failed ({exc}); host(s) labelled "
                f"vmlease={run_id} may still be LIVE — run "
                f"`vmlease reap --run-token {args.run_token}` to clean up",
                file=sys.stderr,
            )
        for hr in teardown_failures:
            print(f"  - {hr.host_spec.name}: {hr.detail}", file=sys.stderr)
        print(f"results: {writer.path}", file=sys.stderr)
        return 1

    print(f"results: {writer.path}")
    return 0


def _build_image_resolve_deps(profile: object, keyring_dir: _NoKeypairDir) -> ResolveDeps:
    """Build the resolve-side ``ResolveDeps`` for a build, setting up the keyring.

    A rescue-write distro's base fingerprint resolves through the gpg trust gate,
    so its pinned ``arch-boxes`` keyring is set up under ``keyring_dir`` first; a
    native distro never touches the resolve seams (its base_fp is the arch-blind
    slug), so no gpg keyring is created — an unused keyring path is passed through.
    This runs BEFORE the per-run keypair exists (D11 — the rescue-key gate gates
    provisioning), so it uses a throwaway dir, not the keypair's.
    """
    from vmlease.distro import DistroProfile

    assert isinstance(profile, DistroProfile)
    keyring_path = str(keyring_dir.directory / "arch-boxes.gpg")
    if profile.needs_rescue_write:
        ensure_arch_keyring(keyring_path, live_subprocess_run)
    return build_live_resolve_deps(keyring_path)


def _backstop_reap_builder(provider: Provider, run_id: str, run_token: str, prefix: str) -> None:
    """Reap the builder by its run-label after an abort/teardown failure (keeps the image).

    ``reap`` is server-only (it selects ``vmlease=<run-id>``), so a created cache
    image — which deliberately carries NO per-run reap label — survives. A failing
    backstop reap is itself surfaced with a manual-reap hint, never a traceback.
    """
    try:
        reaped = reap(provider, run_id)
        print(f"{prefix} reaped {len(reaped)} builder host(s) labelled vmlease={run_id}", file=sys.stderr)
        for h in reaped:
            print(f"  - reaped {h.name} ({h.id})", file=sys.stderr)
    except ProviderError as exc:
        print(
            f"{prefix} backstop reap ALSO failed ({exc}); builder labelled "
            f"vmlease={run_id} may still be LIVE — run "
            f"`vmlease reap --run-token {run_token}` to clean up",
            file=sys.stderr,
        )


def _cmd_build_image(args: argparse.Namespace, *, reader: Callable[[str], str] = input) -> int:
    """Build a content-addressed snapshot cache image (D8/D11 + the G5-G10 paths).

    Resolves the recipe's content key ONCE (one base-fingerprint resolve), no-ops
    if it is already cached, enforces the image quota with D8(B) at-cap prune
    ordering, provisions a single billable builder behind a confirm gate, snapshots
    it, and routes the ProviderQuotaError / teardown-failure / Ctrl-C paths to a
    reap-the-builder-keep-the-image backstop.
    """
    import time

    # D11 + cost guard: resolve the profile, gate the (billable) builder's server
    # type, and validate the rescue key BEFORE any keypair/provisioning (do NOT copy
    # run's post-confirm ordering — a rescue-write build with no key creates nothing).
    try:
        profile = get_profile(args.distro)
        CostGuard().check([args.server_type])
    except (UnknownDistroError, CostGuardError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if profile.needs_rescue_write and not (args.ssh_key and args.ssh_key_path):
        print(
            "error: --ssh-key (registered name) AND --ssh-key-path (its local private half) "
            "are required because a build for a rescue-write distro (e.g. arch) rescue-writes "
            "the base image before snapshotting",
            file=sys.stderr,
        )
        return 2

    run_id = make_run_id(args.run_token)
    arch = server_type_arch(args.server_type)
    provider = HetznerProvider()

    # Resolve the base fingerprint ONCE (the expensive, network/gpg step for a
    # rescue-write distro) and derive BOTH the content key and the source-fp label
    # from it. A resolve failure (mirror down) raises ArchBuildError → fail fast,
    # no builder.
    keyring_dir = _NoKeypairDir()
    try:
        deps = _build_image_resolve_deps(profile, keyring_dir)
        base_fp = base_fingerprint(profile, arch, deps)
        key = content_key_from_base_fp(base_fp, profile, arch, args.operator)
    except ArchBuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        # The throwaway keyring is consumed by the resolve above; drop it so a
        # build never leaks a vmlease-build-keyring-* dir.
        keyring_dir.cleanup()

    try:
        images = provider.list_images(label_selector_purpose())
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Idempotent no-op (D8): K_new already cached and not --rebuild → no builder, no
    # spend.
    already = [img for img in images if img.labels.get(LABEL_CACHE_KEY) == key]
    if already and not args.rebuild:
        print(f"already cached (key {key}); nothing to do")
        return 0

    # Quota + D8(B) prune ordering. S = same-(distro,arch) cache images whose key
    # differs from K_new (this group's superseded predecessors).
    same_group = [
        img
        for img in images
        if img.labels.get(LABEL_DISTRO) == profile.key and img.arch == arch
    ]
    s_prune = superseded(same_group, key)
    guard = ImageQuotaGuard(max_images=args.max_images)
    at_cap = False
    try:
        guard.check(len(images))
    except ImageQuotaError as exc:
        at_cap = True
        if not s_prune:
            # at cap & S empty → refuse before provisioning (no builder).
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if at_cap:
        # at cap & S non-empty → prune-then-build (free slots first). A pre-build
        # prune failure aborts before provisioning (no builder).
        try:
            _prune_images(provider, s_prune)
        except ProviderError as exc:
            print(f"error: pre-build prune failed, aborting before provisioning: {exc}", file=sys.stderr)
            return 1

    # Confirm-before-create (the builder is billable).
    print(f"about to PROVISION 1 builder host (billable) for {args.distro} (key {key})")
    print(f"  - {args.server_type}  {profile.default_image}  arch={arch}")
    if not _confirm("Proceed with the build? [y/N]: ", assume_yes=args.yes, reader=reader):
        print("aborted — nothing provisioned.")
        return 0

    try:
        keypair = generate_keypair(run_id)
    except KeypairError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        rescue_writer = None
        if profile.needs_rescue_write:
            rescue_writer = _build_rescue_writer(keypair, args.ssh_key, args.ssh_key_path)

        def _ssh_factory(operator: str, kp: object) -> OpenSshRunner:
            return OpenSshRunner(operator, keypair.private_key_path)

        spec = HostSpec(
            name=f"vmlease-{run_id}-build-{args.distro}",
            image=profile.default_image,
            server_type=args.server_type,
            distro_key=args.distro,
            labels=run_label(run_id),
            firewall=args.firewall,
        )
        on_ready = make_snapshot_on_ready(
            description=f"vmlease cache {args.distro} {key}",
            labels=cache_labels(profile, arch, key, base_fp, args.run_token),
            sleep=time.sleep,
        )
        note_sink: list[str] = []
        try:
            image = build_one_image(
                spec, profile, provider, _ssh_factory, keypair, args.operator,
                rescue_writer, on_ready=on_ready, note_sink=note_sink,
            )
        except ProviderQuotaError as exc:
            # The account-wide ceiling from create_image — the builder is already
            # torn down by the scaffold's finally; surface the reap hint.
            print(
                f"error: the provider snapshot limit was hit ({exc}); reclaim space with "
                f"`vmlease reap-images` (or raise the account limit), then retry",
                file=sys.stderr,
            )
            return 1
        except (KeyboardInterrupt, SystemExit):
            # Aborted mid-build: backstop-reap the builder by label (KEEPS any image
            # — reap is server-only), note the abort, then re-raise so the interrupt
            # still exits.
            _backstop_reap_builder(provider, run_id, args.run_token, "aborted —")
            raise
        except (ProviderError, ArchBuildError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        # G8: a builder-teardown failure is surfaced via note_sink (not raised);
        # backstop-reap the builder, KEEP the created image, exit non-zero + hint.
        if note_sink:
            _backstop_reap_builder(provider, run_id, args.run_token, "ERROR: builder teardown failed —")
            print(f"  - {note_sink[0]}", file=sys.stderr)
            print(f"image kept: {image.id} (key {key})", file=sys.stderr)
            return 1

        # --rebuild: drop ONLY the older-created same-key image (never all other
        # same-key — that would let concurrent rebuilds delete each other's result).
        if args.rebuild and already:
            try:
                _prune_images(provider, _older_same_key(already, image))
            except ProviderError as exc:
                print(f"warning: --rebuild drop of the older same-key image failed: {exc}", file=sys.stderr)

        # not-at-cap post-build prune of S (build-then-prune). A post-build prune
        # failure WARNS and MUST NOT fail the build (G7) — the new image is the
        # artifact; leftovers are reap-able.
        if not at_cap:
            try:
                _prune_images(provider, s_prune)
            except ProviderError as exc:
                print(f"warning: post-build prune of superseded image(s) failed: {exc}", file=sys.stderr)

        print(f"built image: {image.id}")
        print(f"  key: {key}")
        print(f"  labels: {image.labels}")
        return 0
    finally:
        keypair.cleanup()


class _NoKeypairDir:
    """A minimal keypair-shaped stand-in carrying only a temp ``directory``.

    The base-fingerprint resolve needs a keyring path under a writable dir but
    runs BEFORE the throwaway keypair is generated (D11 — no provisioning before
    the rescue-key gate). A native build never touches the keyring; a rescue-write
    build's real keyring is set up here under a throwaway temp dir, separate from
    the per-run keypair (which is generated only after the confirm gate).
    """

    def __init__(self) -> None:
        import tempfile

        self.directory = Path(tempfile.mkdtemp(prefix="vmlease-build-keyring-"))

    def cleanup(self) -> None:
        """Remove the throwaway keyring dir (best-effort — mirrors ``Keypair.cleanup``)."""
        import shutil

        shutil.rmtree(self.directory, ignore_errors=True)


def _prune_images(provider: Provider, images: list[Image]) -> None:
    """Best-effort, idempotent delete of each image in ``images`` (``delete_image``)."""
    for img in images:
        provider.delete_image(img.id)


def _older_same_key(same_key: list[Image], newest: Image) -> list[Image]:
    """The same-key images created strictly before ``newest`` (ISO-8601 string compare).

    ISO-8601 UTC strings sort lexicographically by instant, so a string compare is
    a valid age compare. Returns every same-key image whose ``created`` precedes the
    just-built ``newest``'s — never the newest itself, never a same-instant tie.
    """
    return [img for img in same_key if img.id != newest.id and img.created < newest.created]


def _cmd_summarize(args: argparse.Namespace) -> int:
    """Read a raw results file, write a versioned summary companion, gate on exit.

    Default output is ``<stem>.summary.json`` beside the raw file (``--out`` overrides).
    Missing/malformed raw input (or a bad ``--battery``) is an error to stderr with
    exit 2 and no summary written; otherwise the exit code is the overall verdict.
    """
    raw_path = Path(args.raw)
    try:
        raw_text = raw_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read results file {raw_path}: {exc}", file=sys.stderr)
        return 2
    try:
        raw_doc = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        print(f"error: results file {raw_path} is not valid JSON: {exc}", file=sys.stderr)
        return 2

    battery = None
    if args.battery:
        try:
            battery = load_battery(Path(args.battery))
        except BatteryError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    try:
        summary = summarize_results(raw_doc, battery=battery, source_raw=str(raw_path))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    out_path = Path(args.out) if args.out else raw_path.parent / summary_filename(raw_path)
    write_summary(summary, out_path)
    print(f"summary: {out_path}")
    return overall_exit_code(summary)


def _cmd_reap(args: argparse.Namespace) -> int:
    run_id = make_run_id(args.run_token)
    try:
        reaped = reap(HetznerProvider(), run_id)
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"reaped {len(reaped)} host(s) labelled vmlease={run_id}")
    for h in reaped:
        print(f"  - {h.name} ({h.id})")
    return 0


def _reap_images_superseded_set(
    images: list[Image], operator: str, warn: Callable[[str], None]
) -> set[str]:
    """The ids of cache images that are superseded across all present groups (D10).

    Mirrors ``build-image``'s deps construction: sets up the pinned arch keyring
    ONLY if a listed image's distro group ``needs_rescue_write`` (so a native-only
    cache never touches gpg), builds the live ``ResolveDeps`` once, then resolves
    each present ``(distro, arch)`` group's current key via
    :func:`resolve_current_keys` (the mirror-down **fail-safe** — an unresolvable
    group is omitted from the mapping and so KEPT, never reaped). Within each
    resolved group, an image is superseded iff its ``vmlease-cache-key`` differs
    from that group's current key (the :func:`superseded` primitive) — which also
    yields **accept-(a)**: a group whose current key resolves but has no matching
    cached image has *every* image superseded.
    """
    needs_keyring = any(
        get_profile(img.labels.get(LABEL_DISTRO, "")).needs_rescue_write
        for img in images
        if img.labels.get(LABEL_DISTRO, "") in DEFAULT_DISTRO_KEYS
    )
    keyring_dir = _NoKeypairDir()
    try:
        keyring_path = str(keyring_dir.directory / "arch-boxes.gpg")
        if needs_keyring:
            ensure_arch_keyring(keyring_path, live_subprocess_run)
        deps = build_live_resolve_deps(keyring_path)

        current_keys = resolve_current_keys(images, get_profile, operator, deps, warn)
        superseded_ids: set[str] = set()
        for group, current_key in current_keys.items():
            group_imgs = [
                img
                for img in images
                if (img.labels.get(LABEL_DISTRO, ""), img.labels.get(LABEL_ARCH, "")) == group
            ]
            for img in superseded(group_imgs, current_key):
                superseded_ids.add(img.id)
        return superseded_ids
    finally:
        # Drop the throwaway keyring so reap --superseded never leaks a
        # vmlease-build-keyring-* dir.
        keyring_dir.cleanup()


def _cmd_reap_images(args: argparse.Namespace) -> int:
    """Reap cached snapshot images as a persistent class (D10 — the ``reap-images`` verb).

    Filters (AND of every *given* predicate, within the ``--distro`` scope):
    ``--distro`` scopes to one distro's cache; ``--older-than`` selects images whose
    ``Image.created`` parses to before the supplied ISO-8601 cutoff (validated
    fail-closed BEFORE any provider call — the GIVEN string is parsed, never the
    current clock); ``--superseded`` selects off-current-key images, resolving each
    present group's current key with the mirror-down fail-safe (an unresolvable group
    is KEPT + warned) and accept-(a) (a resolved group with no matching image → all
    superseded). A bare call with no predicate is REFUSED (never an implicit
    whole-cache wipe). Deletes are best-effort + idempotent with a partial-success
    report; ``--dry-run`` issues zero deletes. Exit 0 on full success / dry-run; 1 if
    any real delete failed (or a ``list_images`` failure); 2 on a usage/validation
    refusal.
    """
    has_distro = bool(args.distro)
    has_older = bool(args.older_than)
    if not (has_distro or has_older or args.superseded):
        print(
            "error: specify at least one of --distro / --older-than / --superseded",
            file=sys.stderr,
        )
        return 2

    # Validate --older-than fail-closed BEFORE any provider call. Parse the GIVEN
    # string only (never read the current clock).
    cutoff = None
    if has_older:
        from datetime import datetime

        try:
            cutoff = datetime.fromisoformat(args.older_than)
        except ValueError as exc:
            print(f"error: --older-than {args.older_than!r} is not a valid ISO-8601 cutoff: {exc}", file=sys.stderr)
            return 2

    provider = HetznerProvider()
    try:
        images = provider.list_images(label_selector_purpose())
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # --distro scopes the candidate set; every other predicate is AND-ed within it.
    scoped = [img for img in images if img.labels.get(LABEL_DISTRO, "") == args.distro] if has_distro else images

    def _warn(msg: str) -> None:
        print(f"warning: {msg}", file=sys.stderr)

    superseded_ids: set[str] = set()
    if args.superseded:
        superseded_ids = _reap_images_superseded_set(scoped, args.operator, _warn)

    selected: list[Image] = []
    for img in scoped:
        if has_older and not _created_before(img, cutoff):
            continue
        if args.superseded and img.id not in superseded_ids:
            continue
        selected.append(img)

    if args.dry_run:
        print(f"DRY-RUN: would delete {len(selected)} cache image(s) — NOTHING deleted")
        for img in selected:
            print(f"  - {img.id}  {img.labels.get(LABEL_CACHE_KEY, '?')}  [{img.labels.get(LABEL_DISTRO, '?')}]")
        return 0

    deleted: list[str] = []
    failures: list[tuple[str, str]] = []
    for img in selected:
        try:
            provider.delete_image(img.id)
            deleted.append(img.id)
        except ProviderError as exc:
            failures.append((img.id, str(exc)))

    print(f"reaped {len(deleted)} cache image(s); {len(failures)} failed")
    for img_id in deleted:
        print(f"  - deleted {img_id}")
    for img_id, reason in failures:
        print(f"  - FAILED {img_id}: {reason}", file=sys.stderr)
    return 1 if failures else 0


def _created_before(image: Image, cutoff: object) -> bool:
    """``True`` iff ``image.created`` parses to an instant before ``cutoff``.

    A cache image's ``created`` is a provider ISO-8601 string. A blank/unparseable
    ``created`` fails the predicate (kept — never reaped on an age check we cannot
    verify). ``cutoff`` is the already-parsed ``--older-than`` datetime.
    """
    from datetime import datetime

    if not image.created:
        return False
    try:
        created = datetime.fromisoformat(image.created)
    except ValueError:
        return False
    assert isinstance(cutoff, datetime)
    return created < cutoff


def _cmd_status(args: argparse.Namespace) -> int:
    run_id = make_run_id(args.run_token)
    try:
        hosts = HetznerProvider().list_labeled(run_id)
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{len(hosts)} live host(s) labelled vmlease={run_id}")
    for h in hosts:
        print(f"  - {h.name} ({h.id})  {h.ipv4}")
    return 0


def _cmd_lint(args: argparse.Namespace, *, runner: ShellcheckRunner | None = None) -> int:
    """Shellcheck every probe in a battery bundle; severity-gated exit code (D5/D6).

    Loads + resolves the bundle (a malformed bundle is ``error:`` + exit 2), prints
    the advisory vacuous-ok warnings, evaluates the **structural no-verdict-source**
    rule (fatal here, advisory at load), then runs the shellcheck driver over every
    probe. ``runner`` is the injectable shellcheck seam (tests pass a fake; the
    default lets the driver spawn the real binary). Exit contract:

    - a **structural violation** → ``1`` on **every** return path (clean shellcheck,
      shellcheck-unavailable, or ``--require-shellcheck``) — it is fatal regardless
      of shellcheck availability.
    - clean — no structural violation and no finding at or above ``--severity`` → ``0``
    - any finding at or above ``--severity`` → ``1``
    - shellcheck unavailable → notice + ``0`` (structural + advisory still ran),
      unless a structural violation or ``--require-shellcheck`` forces ``1``.
    """
    try:
        battery = load_battery(Path(args.battery))
    except BatteryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"battery: {battery.name}  ({len(battery.probes)} probes)")
    _warn_battery(battery)

    structural = structural_violations(battery)
    for v in structural:
        print(f"error: {v}", file=sys.stderr)
    has_structural = bool(structural)

    result = shellcheck_battery(battery, runner=runner)
    if not isinstance(result, tuple):
        # The driver returned the unavailable sentinel (binary absent / wedged).
        if args.require_shellcheck:
            print(
                "error: shellcheck is not installed and --require-shellcheck was given",
                file=sys.stderr,
            )
            return 1
        if has_structural:
            return 1
        print("notice: shellcheck unavailable — skipped; advisory checks ran", file=sys.stderr)
        return 0

    _print_findings(result)
    _print_lint_summary(result, args.severity)
    if has_structural or findings_at_or_above(result, args.severity):
        return 1
    return 0


def _print_findings(findings: tuple[ShellcheckFinding, ...]) -> None:
    """Print findings grouped by probe (source label, line:col, severity, SC code, message)."""
    by_probe: dict[str, list[ShellcheckFinding]] = {}
    for f in findings:
        by_probe.setdefault(f.probe_id, []).append(f)
    for probe_id, group in by_probe.items():
        print(f"probe {probe_id} ({group[0].location}):")
        for f in group:
            code = f"{f.code} " if f.code else ""
            print(f"  {f.line}:{f.column}: {f.severity}: {code}{f.message}")


def _print_lint_summary(findings: tuple[ShellcheckFinding, ...], severity: str) -> None:
    """Print a one-line summary: counts by severity + the active threshold."""
    counts = {sev: sum(1 for f in findings if f.severity == sev) for sev in ("error", "warning", "note", "style")}
    parts = ", ".join(f"{counts[sev]} {sev}" for sev in ("error", "warning", "note", "style"))
    print(f"summary: {parts} (threshold: {severity})")


def _add_matrix_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--battery", required=True, help="path to the battery.toml manifest (the TOML bundle)")
    sp.add_argument("--distros", default=",".join(DEFAULT_DISTRO_KEYS), help="comma-separated distro keys")
    sp.add_argument("--server-type", default="cpx22", help="instance size (default: cpx22)")
    sp.add_argument("--max-hosts", type=int, default=DEFAULT_MAX_HOSTS, help="cost-guard host cap")
    sp.add_argument("--run-token", required=True, help="determinism seam for the run-id")
    sp.add_argument("--firewall", default="", help="provider firewall name to attach to every host (default: none)")
    sp.add_argument(
        "--upload", action="append", metavar="LOCAL[:REMOTE]",
        help="upload a local regular file to every host after readiness, before the battery "
        "(default remote: ~/<basename>); repeatable; validated fail-closed before any spend",
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser (separated so tests can drive it)."""
    p = argparse.ArgumentParser(prog="vmlease", description="provision throwaway hosts, run a probe battery, tear down")
    sub = p.add_subparsers(dest="command", required=True)

    plan_p = sub.add_parser("plan", help="dry-run: show what would be provisioned (zero provider calls)")
    _add_matrix_args(plan_p)
    plan_p.set_defaults(func=_cmd_plan)

    run_p = sub.add_parser("run", help="provision -> probe -> tear down (billable; confirm-gated)")
    _add_matrix_args(run_p)
    run_p.add_argument("--operator", default="probe", help="non-root operator account on each host")
    run_p.add_argument("--results-dir", required=True, help="dir to write the timestamped results JSON")
    run_p.add_argument("--timestamp", required=True, help="results timestamp (caller-supplied; determinism seam)")
    run_p.add_argument(
        "--ssh-key", default="",
        help="hcloud-registered ssh-key NAME injected into rescue (required for rescue-write distros, e.g. arch)",
    )
    run_p.add_argument(
        "--ssh-key-path", default="",
        help="local PRIVATE key matching --ssh-key, used for root ssh into the rescue system (rescue-write distros)",
    )
    run_p.add_argument(
        "--parallel", type=int, default=1,
        help="run up to N hosts concurrently (default 1 = serial); same cost, ~Nx faster wall-clock",
    )
    run_p.add_argument(
        "--probe-timeout", type=float, default=600.0,
        help="default per-probe ssh timeout in seconds (a probe's own timeout overrides; default 600)",
    )
    run_p.add_argument("--yes", action="store_true", help="skip the confirm-before-create prompt")
    run_p.add_argument(
        "--reap-bad-cache-image", action="store_true",
        help="if a restored host fails readiness, reap the source cache image (default: hint only — "
        "the image is named in the failure but kept, so a real fault is not masked)",
    )
    run_p.set_defaults(func=_cmd_run)

    build_p = sub.add_parser(
        "build-image", help="build a content-addressed snapshot cache image (billable; confirm-gated)"
    )
    build_p.add_argument("--distro", required=True, help="distro key to build a cache image for (e.g. ubuntu, arch)")
    build_p.add_argument("--server-type", default="cpx22", help="builder instance size (default: cpx22)")
    build_p.add_argument("--operator", default="probe", help="non-root operator account baked into the image")
    build_p.add_argument("--run-token", required=True, help="determinism seam for the builder run-id / reap label")
    build_p.add_argument(
        "--rebuild", action="store_true",
        help="replace the existing same-key image (drops the older-created same-key image)",
    )
    build_p.add_argument(
        "--max-images", type=int, default=DEFAULT_MAX_IMAGES, help="image quota guard cap (self-runaway tidiness limit)"
    )
    build_p.add_argument(
        "--ssh-key", default="",
        help="hcloud-registered ssh-key NAME injected into rescue (required for rescue-write distros, e.g. arch)",
    )
    build_p.add_argument(
        "--ssh-key-path", default="",
        help="local PRIVATE key matching --ssh-key, used for root ssh into the rescue system (rescue-write distros)",
    )
    build_p.add_argument("--firewall", default="", help="provider firewall name to attach to the builder (default: none)")
    build_p.add_argument("--yes", action="store_true", help="skip the confirm-before-create prompt")
    build_p.set_defaults(func=_cmd_build_image)

    status_p = sub.add_parser("status", help="list live hosts for a run-token")
    status_p.add_argument("--run-token", required=True, help="the run-token whose hosts to list")
    status_p.set_defaults(func=_cmd_status)

    lint_p = sub.add_parser("lint", help="shellcheck every probe in a battery bundle (severity-gated exit)")
    lint_p.add_argument("--battery", required=True, help="path to the battery.toml manifest (the TOML bundle)")
    lint_p.add_argument(
        "--severity", choices=("error", "warning", "note"), default="error",
        help="fail (exit 1) on any shellcheck finding at or above this severity (default: error)",
    )
    lint_p.add_argument(
        "--require-shellcheck", action="store_true",
        help="a missing shellcheck binary fails the gate (exit 1) instead of skipping",
    )
    lint_p.set_defaults(func=_cmd_lint)

    summarize_p = sub.add_parser(
        "summarize", help="read a raw results file -> write a versioned .summary.json (exit = verdict)"
    )
    summarize_p.add_argument("raw", help="path to a raw vmlease results JSON file")
    summarize_p.add_argument(
        "--battery", default="",
        help="optional battery.toml: authoritative probe-id->command labels + declared-but-not-run detection",
    )
    summarize_p.add_argument(
        "--out", default="",
        help="explicit summary output path (default: <stem>.summary.json beside the raw file)",
    )
    summarize_p.set_defaults(func=_cmd_summarize)

    reap_p = sub.add_parser("reap", help="destroy all hosts for a run-token (orphan backstop)")
    reap_p.add_argument("--run-token", required=True, help="the run-token whose hosts to destroy")
    reap_p.set_defaults(func=_cmd_reap)

    reap_images_p = sub.add_parser(
        "reap-images", help="reap cached snapshot images by --distro / --older-than / --superseded (best-effort)"
    )
    reap_images_p.add_argument(
        "--distro", default="", help="scope to cached images of this distro key (an explicit per-distro cache clear)"
    )
    reap_images_p.add_argument(
        "--older-than", default="",
        help="ISO-8601 cutoff: reap images whose creation parses to before it (validated fail-closed; no clock read)",
    )
    reap_images_p.add_argument(
        "--superseded", action="store_true",
        help="reap off-current-key images (resolves each group's current key; unresolvable groups are kept + warned)",
    )
    reap_images_p.add_argument(
        "--operator", default="probe",
        help="operator the cache key is derived from; MUST match the build-time --operator (default: probe)",
    )
    reap_images_p.add_argument(
        "--dry-run", action="store_true", help="report what WOULD be deleted and delete nothing (the preview/safety gate)"
    )
    reap_images_p.set_defaults(func=_cmd_reap_images)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    func: Callable[[argparse.Namespace], int] = args.func
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
