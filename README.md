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
vmlease plan   --battery <f.json> --run-token <slug>            # dry-run: what WOULD provision (zero provider calls)
vmlease run    --battery <f.json> --run-token <slug> \          # provision -> probe -> ALWAYS tear down (billable)
               --operator probe --results-dir <dir> [--yes]
vmlease status --run-token <slug>                               # list the live hosts for a run
vmlease reap   --run-token <slug>                               # destroy every host carrying a run's label
```

The Hetzner provider relies on the operator's active `hcloud` context (configured out of band).

### Authoring a battery (two footguns `vmlease` warns about)

A battery is a JSON list of probes. Two contract details bite "set up, then verify" batteries — `plan`
and `run` emit non-fatal `warning:`s for both (see `lint_battery`):

- **Probes execute in tag-rank order, NOT authoring order** — read-only → operator-space → host-root
  (stable within a rank). A verifier that must run *after* a `mutating:host-root` setup probe has to be
  tagged so it sorts after it; otherwise it inspects the host *before* the setup and passes/fails for the
  wrong reason.
- **A probe's `ok` is its command's exit code** — so gate assertions with `exit $rc`. A command ending in
  `… && echo OK || echo FAIL` always exits 0, so `ok` is `true` no matter which token it printed.

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
