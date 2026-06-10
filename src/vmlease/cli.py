#!/usr/bin/env python3
"""vmlease CLI — ``plan`` · ``run`` · ``status`` · ``lint`` · ``reap``.

- ``plan``   dry-run: what WOULD be provisioned (zero provider calls).
- ``run``    provision -> probe -> ALWAYS tear down; write a timestamped results
             file. Gated by a confirm-before-create prompt (``--yes`` to skip).
- ``status`` list the live hosts carrying a run's label.
- ``lint``   shellcheck every probe in a battery bundle; severity-gated exit code.
- ``reap``   destroy every host carrying a run's label (the orphan backstop).

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

from vmlease.archbuild import (
    ArchBuildError,
    build_live_rescue_writer,
    ensure_arch_keyring,
)
from vmlease.battery import (
    BatteryError,
    ShellcheckFinding,
    ShellcheckRunner,
    findings_at_or_above,
    lint_battery,
    load_battery,
    shellcheck_battery,
)
from vmlease.distro import DEFAULT_DISTRO_KEYS, UnknownDistroError, get_profile
from vmlease.keypair import Keypair, KeypairError, generate_keypair
from vmlease.model import Battery, HostRun, UploadSpec
from vmlease.providers import HetznerProvider, ProviderError
from vmlease.results import IncrementalResultsWriter
from vmlease.runner import TEARDOWN_WARNING_PREFIX, Matrix, RescueWriter, execute, plan
from vmlease.safety import DEFAULT_MAX_HOSTS, CostGuard, CostGuardError, UploadError, make_run_id, reap
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


def _matrix_from_args(args: argparse.Namespace, workload: Workload) -> Matrix:
    distro_keys = tuple(k.strip() for k in args.distros.split(",") if k.strip())
    uploads = tuple(_parse_upload(v) for v in (args.upload or ()))
    return Matrix(
        workload=workload,
        distro_keys=distro_keys,
        server_type=args.server_type,
        run_token=args.run_token,
        firewall=args.firewall,
        uploads=uploads,
    )


def _warn_battery(battery: Battery) -> None:
    """Print non-fatal authoring warnings (vacuous ok) to stderr."""
    for w in lint_battery(battery):
        print(f"warning: {w}", file=sys.stderr)


def _cmd_plan(args: argparse.Namespace) -> int:
    try:
        battery = load_battery(Path(args.battery))
        matrix = _matrix_from_args(args, ProbeWorkload(battery))
        items = plan(matrix, cost_guard=CostGuard(max_hosts=args.max_hosts))
    except (BatteryError, UnknownDistroError, CostGuardError, UploadError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _warn_battery(battery)
    print(f"battery: {battery.name}  ({len(battery.probes)} probes)")
    print(f"plan: {len(items)} host(s) — NOTHING PROVISIONED (dry-run)")
    for it in items:
        print(f"  - {it.host_name}  [{it.distro_key}]  {it.image}  {it.server_type}  {it.workload_summary}")
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
        matrix = _matrix_from_args(args, ProbeWorkload(battery))
        items = plan(matrix, cost_guard=CostGuard(max_hosts=args.max_hosts))
    except (BatteryError, UnknownDistroError, CostGuardError, UploadError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    _warn_battery(battery)
    print(f"battery: {battery.name}  ({len(battery.probes)} probes)")
    print(f"about to PROVISION {len(items)} real host(s) (billable):")
    for it in items:
        print(f"  - {it.host_name}  [{it.distro_key}]  {it.image}  {it.server_type}")
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
                "(registered name) AND --ssh-key-path (its local private half)",
                file=sys.stderr,
            )
            keypair.cleanup()
            return 2
        rescue_writer = _build_rescue_writer(keypair, args.ssh_key, args.ssh_key_path)

    writer = IncrementalResultsWriter(Path(args.results_dir), run_id, args.timestamp)

    def _persist(host_run: HostRun) -> None:
        # Adapt the writer's path-returning add() to the None-returning sink hook.
        writer.add(host_run)

    try:
        host_runs = execute(
            matrix, provider, _ssh_factory, keypair, args.operator,
            cost_guard=CostGuard(max_hosts=args.max_hosts), rescue_writer=rescue_writer,
            max_parallel=args.parallel, on_host_complete=_persist,
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
    the advisory vacuous-ok warnings, then runs the shellcheck driver over every
    probe. ``runner`` is the injectable shellcheck seam (tests pass a fake; the
    default lets the driver spawn the real binary). Exit contract:

    - clean — no finding at or above ``--severity`` → ``0``
    - any finding at or above ``--severity`` → ``1``
    - shellcheck unavailable → notice + ``0`` (advisory still ran), unless
      ``--require-shellcheck`` makes the absence itself a ``1`` failure.
    """
    try:
        battery = load_battery(Path(args.battery))
    except BatteryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"battery: {battery.name}  ({len(battery.probes)} probes)")
    _warn_battery(battery)

    result = shellcheck_battery(battery, runner=runner)
    if not isinstance(result, tuple):
        # The driver returned the unavailable sentinel (binary absent / wedged).
        if args.require_shellcheck:
            print(
                "error: shellcheck is not installed and --require-shellcheck was given",
                file=sys.stderr,
            )
            return 1
        print("notice: shellcheck unavailable — skipped; advisory checks ran", file=sys.stderr)
        return 0

    _print_findings(result)
    _print_lint_summary(result, args.severity)
    return 1 if findings_at_or_above(result, args.severity) else 0


def _print_findings(findings: tuple[ShellcheckFinding, ...]) -> None:
    """Print findings grouped by probe (source label, line:col, severity, SC code, message)."""
    by_probe: dict[str, list[ShellcheckFinding]] = {}
    for f in findings:
        by_probe.setdefault(f.probe_id, []).append(f)
    for probe_id, group in by_probe.items():
        print(f"probe {probe_id} ({group[0].location}):")
        for f in group:
            code = f" {f.code}" if f.code else ""
            print(f"  {f.line}:{f.column}: {f.severity}:{code} {f.message}")


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
    run_p.set_defaults(func=_cmd_run)

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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    func: Callable[[argparse.Namespace], int] = args.func
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
