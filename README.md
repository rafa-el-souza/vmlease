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
vmlease build-image  --distro <key> --run-token <slug> [--server-type cpx22] [--rebuild]  # opt-in: cache a prepped snapshot
vmlease reap-images  [--distro <key>] [--older-than <ISO>] [--superseded] [--dry-run]  # prune cached snapshot images
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

## Caching (snapshot image cache)

The expensive part of a `run` is OS prep: Arch pays the multi-minute **rescue-write** every time, and
every distro re-installs its base packages via cloud-init. The snapshot image cache does that prep **once**
and bakes it into a Hetzner snapshot, so later runs start from the prepped disk in ~30s. It is a **speed
*and* reliability** win — the fragile rescue path then runs only when you build the cache, not on every run.

The cache is **opt-in**: nothing is cached until you run `build-image`. A `run` automatically restores from a
matching cached image if one exists, and otherwise behaves exactly as it always has.

### `build-image` — the explicit build verb

`build-image` provisions a throwaway builder, runs the full normal OS prep (Arch rescue-write + cloud-init
package install), **sysprep**s it (wipes `/etc/machine-id` so each restore is unique), powers off, takes a
content-addressed labelled snapshot, and deletes the builder. Building is **always an explicit verb** — a
`run` consumes the cache but never builds it.

```
vmlease build-image --distro <key> --run-token <slug> \
        [--server-type cpx22] [--operator probe] [--rebuild] [--max-images 10] \
        [--ssh-key <name> --ssh-key-path <path>] [--firewall <name>] [--yes]
```

- `--distro` — the distro key to build (e.g. `ubuntu`, `arch`); an unknown key exits 2.
- `--server-type` — builder instance size; **defaults to `cpx22`** (cost-guard allowlisted).
- `--rebuild` — replace the existing same-key image (drops the older-created same-key image).
- `--max-images` — vmlease's own self-cap (default `10`); see *No auto-TTL* below.
- `--ssh-key` / `--ssh-key-path` — the hcloud-registered key name + matching local private key, **required
  for rescue-write distros** (e.g. `arch`) and validated *before* any host is provisioned.
- It is idempotent: if a matching image is already cached, `build-image` is a no-op unless `--rebuild`.

### Restore on `run`

A `run` automatically restores from a cached image when one **matches** — same content key, same
architecture, and the snapshot fits the chosen server's disk. On a hit, the host is created directly from
the snapshot with a **minimal key-only cloud-init**, skipping both the rescue-write *and* the package
install. A `run` **consumes but never builds** — it makes zero `create_image` calls.

The cache is **advisory, never load-bearing**: a miss — no matching image, an oversized image, a vanished
snapshot, or any cache/lookup failure — is a **graceful fall-back to the cold path**. It just doesn't save
time; it never breaks a run. (If a *restored* host then fails readiness, that is recorded as a host failure
naming the source image; the opt-in `--reap-bad-cache-image` flag reaps that image — default is hint-only,
keeping the image so a real fault is not masked.)

### `reap-images` — pruning the persistent class

Cached images are a **persistent class** — they outlive runs and carry a content-key label, not the
ephemeral `vmlease=<run-id>` run label. The per-run `vmlease reap` (a server-only selector) never touches
cache images. To prune the cache, use `reap-images`:

```
vmlease reap-images [--distro <key>] [--older-than <ISO-8601>] [--superseded] [--dry-run]
```

- `--distro` — scope to one distro's cached images (an explicit per-distro cache clear).
- `--older-than <ISO-8601>` — reap images created before the cutoff (validated fail-closed; no clock read).
- `--superseded` — reap off-current-key images; each group's current key is resolved fail-safe, and any
  unresolvable group is **kept and warned**, never blindly deleted.
- `--dry-run` — report what *would* be deleted and delete nothing (the preview/safety gate).

### The content key

Cache validity is keyed on **distro + architecture + the rendered prep recipe + the resolved upstream
image** — `v1-<distro>-<hash>`, where the hash folds the base-image fingerprint and the canonical
cloud-init together. So a recipe change or an upstream image change yields a *new* key, and the next
`build-image` produces a fresh image while a `run` simply misses the stale one and falls back to cold —
freshness is automatic, with no stale-cache footgun.

### Build small → restore anywhere

A snapshot is **not location-bound**: it restores onto any server whose disk is **≥** the snapshot's. So
build on a small, cheap type (`build-image --server-type` defaults to `cpx22`) and restore onto whatever a
`run` provisions. An oversized image (snapshot disk > target disk) is simply a graceful miss → cold path.

### No auto-TTL

There is **no automatic expiry**. Freshness is operator/CI policy via `reap-images --older-than` /
`--superseded`. The real ceiling is the **account-wide provider snapshot cap** (Hetzner's is ~30) — surfaced
as a typed `ProviderQuotaError`; vmlease's own `--max-images` self-cap (default `10`) is just a
self-runaway tidiness limit checked in `build-image` before provisioning.

### Migration / rollback

The cache is **fully backward compatible and opt-in**. Adopting it requires no code change and no battery
change — a `run` with no matching cached image behaves exactly as before. To **roll back**: stop running
`build-image`, and/or `reap-images` the existing snapshots — `run` reverts to the cold path automatically,
with no code change.

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
