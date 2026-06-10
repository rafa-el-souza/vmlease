# vmlease

Provision throwaway cloud VMs, run a workload over SSH, and **always** tear them down — a small,
project-agnostic VM-provisioning + probe harness. Built for empirical checks that need a fresh, real host
(the "ratified ≠ validated" gate): spin a disposable VM per distro, run a declarative battery of probes,
capture structured results, and guarantee teardown.

> Graduated from `probehost` (developed inside the `internal-tooling` skill tree, HEAD `efb8f38`) into this
> standalone repo. The behavior is a faithful port; the package/label were renamed `probehost` → `vmlease`.

## What it does

- **Provision → run → guaranteed teardown.** Each host is created, probed, and destroyed in its own
  `try/finally`, so one host's failure never discards another's results, and nothing is left billing.
- **Safety first.** A cost guard (host cap + cheap-server-type allowlist), `vmlease=<run-id>` labels,
  confirm-before-create, and a `reap` orphan-backstop. The harness is **provider-token-blind** — it relies
  on your already-active `hcloud` context and never reads or logs the token.
- **Per-distro prep** via cloud-init (ubuntu / debian / fedora natively; **arch** by GPG-verified
  rescue-write, since Hetzner ships no native Arch image).
- **Deterministic & testable.** No wall-clock reads or RNG in the library — run-ids and timestamps are
  injected; all external I/O sits behind injected seams.

## CLI

```
vmlease plan   --battery <battery.toml> --run-token <slug>      # dry-run: what WOULD provision (zero provider calls)
vmlease run    --battery <battery.toml> --run-token <slug> \    # provision -> probe -> ALWAYS tear down (billable)
               --operator probe --results-dir <dir> [--yes]
vmlease status --run-token <slug>                               # list the live hosts for a run
vmlease lint   --battery <battery.toml> [--severity warning] [--require-shellcheck]  # shellcheck every probe (gate)
vmlease reap   --run-token <slug>                               # destroy every host carrying a run's label
vmlease summarize <raw-results.json> [--battery <battery.toml>] [--out <s.json>]  # ONE canonical reader -> .summary.json
```

The Hetzner provider relies on the operator's active `hcloud` context (configured out of band).

### Authoring a battery

A battery is a **TOML bundle**: a `battery.toml` manifest (a `name` plus a `[[probe]]` array) alongside any
co-located shell scripts. Each probe declares a stable `id`, a `title`, a `tag`, an optional `classifies`
label and `timeout`, and **exactly one of** an inline `run` block (TOML's `'''…'''` literal strings need no
escaping) or a `script` reference to a sibling `.sh` file (resolved relative to the manifest and contained
to the bundle — an absolute path, a `..` escape, or an out-of-tree symlink is rejected). Probes are authored
as **bash**. See `examples/compose-plugin-check/battery.toml`.

- **Probes execute in authoring order** — the manifest *is* the execution order, regardless of `tag`. A
  verifier that must run *after* a `mutating:host-root` setup probe is simply authored after it; `tag`
  records what a probe touches and governs sudo escalation but no longer reorders execution.
- **A probe's `ok` is its command's exit code** — so gate assertions with `exit $rc` (`plan`/`run` emit a
  non-fatal `warning:` for an un-gated token-printing probe; see `lint_battery`). A command ending in
  `… && echo OK || echo FAIL` always exits 0, so `ok` is `true` no matter which token it printed.

Migrating a pre-TOML (JSON) battery? See [`docs/battery-toml-migration.md`](docs/battery-toml-migration.md)
— field mapping, a one-time extraction helper, the authoring-order check, and a troubleshooting table.

### Summarizing results (`vmlease summarize`)

A raw results file records each probe's `ok` as *only its exit code* — but the real "did it pass?" lives
in `*_OK` / `*_FAIL` assertion tokens the probe prints to stdout (the vacuous-ok footgun). `summarize` is
the ONE canonical reader: it writes a versioned `<stem>.summary.json` companion beside the raw file (the
raw file is never mutated) and exits with the overall verdict so a caller can gate without parsing:

```
vmlease summarize results/vmlease-run-ts.json; echo $?   # 0 = all PASS/PASS_NO_ASSERTIONS, non-zero otherwise
```

The summary carries `schema_version: "1"`; per host its `distro`/`image`/`detail` + a probe record each
with a computed `verdict`, the four harvested token buckets, and bounded stdout/stderr tails; a `matrix`
pivot (canonical command × distro, collapsed worst-of); and `totals` by verdict. The per-probe verdict is
deterministic: `timed_out` → `TIMEOUT`; else any `*_FAIL` token or non-zero exit → `FAIL`; else zero exit
with a `*_OK` token → `PASS`; else (zero exit, no assertion tokens) → `PASS_NO_ASSERTIONS`. Pass
`--battery <battery.toml>` to use authoritative command labels and surface declared-but-not-run probes per host;
without it a built-in probe-id→command map is the fallback. See `src/vmlease/summary.py` for the full shape.

## Development

All project commands run through the `Makefile` (which wraps `uv`):

```
make install     # sync the env + dev tools
make lint        # strict lint  (ruff)
make typecheck   # strict types (mypy --strict)
make test        # run the suite (warnings = errors)
make coverage    # suite under coverage + report (fails below the floor)
make format      # apply ruff's SAFE autofixes
make check       # the full gate: lint -> typecheck -> test -> coverage
make hooks       # install the gate as a pre-commit hook
```

> `ruff format` is **banned** (it has a code-mangling bug) — excluded in
> `pyproject.toml` and never invoked. `make format` uses `ruff check --fix`.
