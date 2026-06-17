# vmlease

Provision throwaway cloud VMs, run a workload over SSH, and tear them down by default (or keep one live to debug) — a small,
project-agnostic VM-provisioning + probe harness. Built for empirical checks that need a fresh, real host
(the "ratified ≠ validated" gate): spin a disposable VM per host, run a declarative battery of probes,
capture structured results, and guarantee teardown by default.

> Graduated from `probehost` (developed inside the `internal-tooling` skill tree, HEAD `efb8f38`) into this
> standalone repo. The behavior is a faithful port; the package/label were renamed `probehost` → `vmlease`.

## What it does

- **Provision → run → guaranteed teardown (by default).** Each host is created, probed, and destroyed in its own
  `try/finally`, so one host's failure never discards another's results, and nothing is left billing — unless you
  deliberately opt a host out with `--keep`, which stays billable and reap-tracked.
- **Safety first.** A cost guard (host cap + cheap-server-type allowlist), `vmlease=<run-id>` labels,
  confirm-before-create, and a `reap` orphan-backstop. The harness is **provider-token-blind** — it relies
  on your already-active `hcloud` context and never reads or logs the token.
- **Per-distro prep** via cloud-init (ubuntu / debian / fedora natively; **arch** by GPG-verified
  rescue-write, since Hetzner ships no native Arch image).
- **Deterministic & testable.** No wall-clock reads or RNG in the library — run-ids and timestamps are
  injected; all external I/O sits behind injected seams.

## Quickstart

The repo ships a real battery — `examples/compose-plugin-check/` — that verifies a freshly-provisioned host
has a working `docker compose` v2 plugin. The full loop is **lint → plan → run → summarize** (prerequisite:
an active `hcloud` context, configured out of band — `run` is billable, the rest are free):

```bash
# 1. lint: shellcheck the battery's probe (+ any prep) scripts and assert it has a verdict source. Free.
vmlease lint --battery examples/compose-plugin-check/battery.toml

# 2. plan: dry-run the matrix — what WOULD provision. Zero provider calls, free.
vmlease plan --battery examples/compose-plugin-check/battery.toml --run-token compose-check

# 3. run: provision one host per entry, run the battery, ALWAYS tear down. Billable; confirm-gated.
vmlease run  --battery examples/compose-plugin-check/battery.toml --run-token compose-check \
        --results-dir ./results --timestamp 2026-06-15T1200 --hosts ubuntu,debian --yes

# 4. summarize: the ONE canonical reader → a .summary.json companion; its exit code is the pass/fail gate.
vmlease summarize ./results/vmlease-compose-check-2026-06-15T1200.json \
        --battery examples/compose-plugin-check/battery.toml; echo $?   # 0 = green
```

`--run-token` and `--timestamp` are determinism seams (the library never reads the clock): the same token →
the same run-id and the same results filename. Gate CI on **`summarize`'s** exit code, never on `run`'s
(see *CI integration & exit codes* below).

### Hosts: the family/version model

`--hosts` is the primary host-selection flag: a comma-separated list, each entry
**`[name=]family[@version]`**. A bare family resolves to that family's **default version**; `@version` pins
one. Repeat an entry to ask for two hosts of the same `(family, version)`.

```bash
--hosts ubuntu                       # one ubuntu host at the family default version
--hosts ubuntu@22.04                 # pin a version
--hosts api=ubuntu@24.04             # name the host `api` (the per-host identity)
--hosts ubuntu@22.04,ubuntu@24.04    # a version matrix of the same family
--hosts arch,arch                    # two arch hosts (repetition = multiplicity)
```

The registry is keyed by **`(family, version)`**: the validated entries are `ubuntu@22.04`, `ubuntu@24.04`,
`debian@12`, `debian@13`, `fedora@43`, `fedora@44`, and `arch` (a rolling family — it takes **no** `@version`;
`arch@…` errors). A bare `--hosts` (the default) is the four bare families.

Each host has a **bare `name`** — its identity in results, the matrix pivot, and `--keep` — distinct from the
provider server name (`vmlease-<run-id>-<name>`). Unnamed entries are **auto-named**:

- a family that appears **once** keeps its bare name (`ubuntu`);
- a family with **multiple versions** gets a version suffix (`ubuntu-2204`, `ubuntu-2404`);
- the **same `(family, version)` repeated** gets an index (`arch-1`, `arch-2`);
- **rolling** families (arch) take no version suffix.

> **`--distros` is a deprecated alias** for `--hosts` (identical grammar; using it prints a one-time
> deprecation notice). `--distros ubuntu,debian` still runs verbatim — no migration required.

## CLI

```
vmlease plan   --battery <battery.toml> --run-token <slug>      # dry-run: what WOULD provision (zero provider calls)
vmlease run    --battery <battery.toml> --run-token <slug> --results-dir <dir> --timestamp <ts> \  # provision -> probe -> tear down (or --keep to leave live)
               [--operator probe] [--hosts [name=]family[@version],…] [--parallel N] \
               [--upload LOCAL[:REMOTE]] [--ssh-key <name> --ssh-key-path <path>] [--max-hosts 8] [--firewall <name>] [--keep [HOST ...]] [--yes]
vmlease status --run-token <slug>                               # list the live hosts for a run
vmlease lint   --battery <battery.toml> [--severity warning] [--require-shellcheck]  # shellcheck every probe (gate)
vmlease reap   --run-token <slug>                               # destroy every host carrying a run's label
vmlease build-image  --distro family[@version] --run-token <slug> [--server-type cpx22] [--rebuild]  # opt-in: cache a prepped snapshot
vmlease reap-images  [--distro <family>] [--older-than <ISO>] [--superseded] [--dry-run]  # prune cached snapshot images
vmlease summarize <raw-results.json> [--battery <battery.toml>] [--out <s.json>]  # ONE canonical reader -> .summary.json
```

The Hetzner provider relies on the operator's active `hcloud` context (configured out of band).

> **`--parallel`** (on `run`, default `1` = serial) runs up to N hosts concurrently. It is **same cost,
> ~Nx faster wall-clock**: each host is an independent create/probe/teardown with its own `try/finally`, and
> the only shared state (the throwaway key + the gpg keyring) is read-only after setup, so the
> teardown-always guarantee holds per thread. Results are returned in matrix order regardless of completion
> order. `--parallel` and the cost guard's `--max-hosts` cap are independent: `--max-hosts` bounds how many
> hosts the matrix may request at all; `--parallel` only bounds how many of them run at once (it is clamped
> to the host count).

### Debugging a live host (`--keep`)

Every host is torn down by default. Pass `--keep` to leave hosts **running** so you can SSH in and iterate
against a real, prepped box instead of paying a full provision → prep → teardown cycle per change:

```
vmlease run --battery b.toml --run-token dbg --results-dir ./out --timestamp T \
            --hosts ubuntu,debian --keep ubuntu          # keep ubuntu live, tear debian down
# ... the run prints, per kept host:
#   - vmlease-dbg-ubuntu (id) is LIVE at <ip> — ssh -i <keydir>/id_ed25519 probe@<ip>
vmlease reap --run-token dbg                              # destroy ALL of the run's hosts when done
```

- **`--keep` selects by host `name` or family** (metavar `HOST`). Bare `--keep` keeps **every** host;
  `--keep <name>` keeps the host with that exact name; `--keep family:<family>` keeps every host of that
  family. The selector keys off the **auto-named identity** (see *Hosts: the family/version model*): with
  `--hosts ubuntu` the host is named `ubuntu`, so `--keep ubuntu` keeps it; with
  `--hosts ubuntu@22.04,ubuntu@24.04` the hosts are `ubuntu-2204` / `ubuntu-2404`, so you keep one with
  `--keep ubuntu-2204` (a bare `--keep ubuntu` would match no host and error), or keep both with
  `--keep family:ubuntu`.
- **Fail-closed.** A token matching no host — including a `family:` selector that matches zero in-run hosts —
  is rejected before anything provisions, listing the eligible host names. The resolved keep-set (names +
  count) is **echoed before provisioning even under `--yes`**, so the kept hosts are always visible before
  spend.
- A kept host stays **billable with no TTL**. Its reattach coordinates (IP, operator, key path) are printed
  and recorded in the results file; the throwaway SSH key survives on disk so the printed `ssh` line works.

> **The safety backstops still apply.** `--keep` only relaxes the *default* teardown — it is not a leak: a
> kept host carries the `vmlease=<run-id>` label so `reap`/`status` always find it, and an aborted (Ctrl-C)
> or teardown-failing run still reaps the **non-kept** hosts. The explicit `vmlease reap` destroys
> everything, kept hosts included.

> **Live-run pre-flight.** A run-token deterministically identifies one live run, so `run` and `build-image`
> **refuse to start** (fail closed, before the billing confirm) if the run-token already has live provider
> hosts — it lists them and hints at `vmlease reap --run-token <token>` (or use a different token). This
> prevents a second run from colliding with — and reaping — the first run's hosts. `plan` is exempt (it makes
> no provider calls).

## Authoring a battery

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
- **Or declare the verdict structurally with `[probe.assert]`** (next subsection) — the recommended way to
  avoid the exit-code footgun entirely.

### Declarative verdicts: `[probe.assert]`

Instead of the `*_OK`/`*_FAIL` token convention, a probe may carry a `[probe.assert]` table of declarative
predicates. When a probe declares **any** assertion: its `ok` becomes the **AND of all the assertions**, the
raw exit code is **ignored** (no `exit $rc` gating needed), and the probe is **exempt** from the un-gated-token
`lint`/`run` warning. The full key set:

| Key | Value | Holds when |
|-----|-------|------------|
| `exit` | int | the command's exit code **equals** the value |
| `exit_not` | int | the command's exit code is **not** the value |
| `stdout_has` / `stderr_has` | string or `[string]` | the stream **contains** the substring (every one, if a list) |
| `stdout_lacks` / `stderr_lacks` | string or `[string]` | the stream contains **none** of the substrings |
| `stdout_matches` / `stderr_matches` | string or `[string]` | an **RE2** pattern matches the stream (every one, if a list) |
| `stdout_matches_not` / `stderr_matches_not` | string or `[string]` | **no** RE2 pattern matches the stream |
| `stdout_empty` / `stderr_empty` | bool | the (whitespace-stripped) stream is empty (`true`) / non-empty (`false`) |

```toml
[[probe]]
id = "compose-v2"
title = "docker compose v2 plugin is present"
tag = "read-only"
run = '''docker compose version'''

[probe.assert]
exit = 0
stdout_matches = '''Docker Compose version v2\.'''   # RE2, unanchored
stderr_lacks = "unknown shorthand flag"
```

- **List values conjoin (AND).** A list value means *every* element must hold; an **empty list is rejected**
  at battery load (a no-op assertion is a footgun, not a pass).
- **`_lacks` / `_matches_not` / `_empty` on an empty stream.** A `_lacks` / `_matches_not` over an empty
  stream is **vacuously true** (nothing present to forbid); a `_has` / `_matches` over an empty stream is
  **false** (the substring/pattern can't be present). `stdout_empty = true` holds iff the stream is
  whitespace-empty.

> **Regex dialect is RE2, not Python `re`.** Patterns compile through [RE2](https://github.com/google/re2):
> matching is **unanchored** (a `search` — `^`/`$` anchor to the whole text unless you use `(?m)`),
> consistent with substring `_has`. RE2 has **no backreferences and no lookaround** (the features that make
> a regex vulnerable to catastrophic backtracking), and each compiled pattern is bounded by an **8 MiB**
> automaton memory budget. A malformed pattern, an unsupported backreference/lookaround, or an over-budget
> automaton is a hard battery-load error naming the probe and the pattern — caught at parse, never at
> evaluation.

### Uploads: `--upload`

`--upload LOCAL[:REMOTE]` (on `plan`/`run`, **repeatable**) stages a local file onto **every** host. With no
`:REMOTE`, the remote defaults to `~/<basename(local)>`. Every upload is validated **fail-closed before any
spend** — a bad `--upload` aborts in `plan` and at the top of `execute`, before a single host is created
(the validation is host-independent, so it runs once for the whole run).

- **`--upload` takes a regular file.** The source must be a plain, readable, non-symlinked regular file; a
  directory (or FIFO / socket / device) is refused fail-closed (`is not a regular file`). The transport
  layer *does* carry a recursive directory push (`upload_dir`, via `rsync --safe-links`, with its own
  dir-source validator that drops out-of-tree symlinks), but the `--upload` flag is wired only to the
  single-file path — directory staging is not yet exposed on the CLI.
- **Symlinks are refused.** The source entry itself, or any symlink component anywhere in its resolved path,
  is rejected — a symlink's target is never read or shipped.
- **The remote destination is guarded.** No `..` path segment (traversal), no leading `-` (scp
  option-injection), and a conservative character allowlist (letters, digits, and `._/~@,=+-` — no
  whitespace, no shell metacharacters).

**Ordering — uploads land after readiness, before *both* prep and probes.** Per host, the runner waits for
the readiness gate, **stages the uploads**, *then* runs the workload — which runs `[prep.packages]`, then the
`[[prep.setup]]` steps, then the probe loop. So an uploaded file is on disk before prep runs, and **prep can
consume an upload** (e.g. a `[[prep.setup]]` step that installs an uploaded artifact). The full host
prerequisite order is therefore:

```
capabilities (requires, baked into cloud-init) → readiness gate → UPLOADS
  → [prep.packages] → [[prep.setup]] (authoring order) → probe loop
```

### Linting a battery: `vmlease lint`

`vmlease lint --battery <battery.toml>` does **two** independent things and gates a single exit code on both:

- **shellcheck** every probe command **and** every `[[prep.setup]]` step (fed through
  `shellcheck --shell=bash`). `--severity` (default `error`; one of `error`/`warning`/`note`) sets the
  threshold — any finding at or above it fails the gate (exit `1`). If the `shellcheck` binary is missing,
  lint prints a notice and **skips** that part (exit `0`) — unless `--require-shellcheck` is given, which
  makes a missing binary fail (exit `1`).
- a **structural no-verdict-source** check, which is **always fatal** (exit `1`) regardless of shellcheck
  availability or `--severity`. It flags a probe that declares **no** `[probe.assert]` and whose command
  prints `*_OK`/`*_FAIL` tokens without an explicit `exit $rc` — such a probe's `ok` is always its exit code
  (`0`), so it has no real source of truth. A probe that declares assertions or exit-gates its command is
  exempt. (The same finding is an advisory `warning:` at plan/run time; `lint` is where it becomes fatal.)

## Host prerequisites: `requires` and `[prep]`

A battery declares its host prerequisites **declaratively**: the vmlease-provided capabilities it opts
into (`requires`) and the packages + setup steps it brings itself (`[prep]`). Both are optional and
additive to the manifest. The provisioning order per host is: capabilities (`requires`, baked into
cloud-init at create time) → readiness gate → uploads → `[prep.packages]` → `[[prep.setup]]` (authoring
order) → the probe loop.

#### `requires` — opt into vmlease-provided capabilities (default-off)

```toml
requires = ["docker"]   # opt into the docker capability; default-off
```

`requires` names vmlease-provided capabilities the host needs. It is **opt-in and default-off**: a
battery that names no capability provisions a host **without** it — in particular, **docker is no longer
always installed**. A battery that needs docker MUST declare `requires = ["docker"]`; otherwise it lands
on a docker-less host and its docker probes fail loudly (loud, not silent). An entry outside the known
vocabulary is a hard battery-load error naming the unknown capability. The v1 vocabulary is exactly
`docker`. A capability is realized per package-manager by a recipe (a package set + an optional setup
fragment) injected into the host's cloud-init only when required — so docker and docker-less hosts are
distinct cache entries automatically (see *Caching* below).

> **Breaking:** docker used to be baked into every host. Existing batteries that rely on docker must add
> `requires = ["docker"]`. The in-repo `examples/compose-plugin-check/` is migrated as the reference.

#### `[prep]` — packages and setup the battery brings itself

`[prep]` is host *setup*, not a test (distinct from probes — it carries no `tag` and no assertions). It
runs once per host over SSH as the operator (`sudo` written inline where root is needed), after readiness
(and after any uploads) and before the first probe.

```toml
[prep.packages]
# A flat, validated table of UNION-OF-APPLICABLE-SELECTORS. Every key is EITHER a
# known package-manager (apt / dnf / pacman) OR a known distro (ubuntu / debian /
# fedora / arch …) — the two name-sets are disjoint and closed. A key that is
# neither is a hard battery-load error (so an `apt-get` or `ubntu` typo fails loud).
apt    = ["jq", "ripgrep"]   # installed on every apt host (ubuntu AND debian)
debian = ["some-debian-only-pkg"]   # ADDED on debian only (its DISTRO key)

[[prep.setup]]
id = "INSTALL_UV"            # unique across setup steps
title = "install uv"        # optional, human label
run = '''curl -LsSf https://astral.sh/uv/install.sh | sudo sh'''  # inline; or `script = "step.sh"`
# distros = ["ubuntu", "debian"]   # optional allowlist (default: every distro); a
                                    # value that is not a known distro is rejected (typo guard)
# required = true                   # default true; see soft/hard below
# timeout = 1800                    # seconds; prep default is 1800 (longer than the
                                    # probe default — prep includes source builds)
```

- **Union-of-applicable-selectors rule.** A host's effective package set is the **union** of the list
  under the host's **package-manager** key and the list under the host's **distro** key, deduplicated,
  manager entries first — one `<mgr> install` pass (`apt-get install -y` / `dnf install -y` /
  `pacman -S --noconfirm`; on apt the index is refreshed with `apt-get update` first, since prep may run
  on a cached host with a stale index). This is union-only (no subtraction): put the common packages
  under the manager key and per-distro extras under the distro key. So on a debian host,
  `apt = ["a"]` + `debian = ["b"]` ⇒ effective `["a", "b"]`.
- **The manager-vs-distro key taxonomy.** The flat mixed key-set is the one non-obvious part: a key is a
  *manager* (the install **mechanics** axis — apt serves both ubuntu and debian) or a *distro* (a single
  OS). They are disjoint closed sets; pick the manager key for "all distros that share this installer"
  and the distro key for "this one OS only."
- **`[[prep.setup]]` steps** run in authoring order, each with a unique `id`, **exactly one of** an inline
  `run` block or a `script` reference (resolved relative to the manifest, contained to the bundle, and
  shellchecked exactly like a probe `script` — `vmlease lint` covers prep scripts too), an optional
  `distros` allowlist (default: every distro; a step whose allowlist excludes the host is skipped), an
  optional `required` flag (default `true`), an optional `title`, and an optional `timeout` (default
  `1800`s). A malformed `[prep]` is a hard battery-load error: an unrecognized key, a selector that is
  neither manager nor distro, an unknown `distros` value, a duplicate `id`, a step with neither/both of
  `run`/`script`, or an empty resolved command.

#### Soft vs hard fail — and the `prep_phase` / `summarize` contract

- **`[prep.packages]` always hard-fails**, and a **`required = true`** setup step that fails is a **hard**
  failure: the host runs **no probes** and is torn down through the normal teardown/reap path.
- A **`required = false`** setup step that fails is a **soft** failure: it is **recorded** and the phase
  continues to the remaining setup steps and then the probe loop.
- Every executed prep step is recorded in a structured **`prep_phase`** section of the results (per step:
  `id`, `exit`, `required`, captured `stderr`) — always present (empty for a no-prep host), distinct from
  the per-probe records. A soft failure is never silently dropped.
- **`summarize` is where prep failure becomes a verdict.** A hard prep abort counts as **`PREP_HARD_FAIL`**
  and forces a **non-zero** `summarize` exit (this also closes the old zero-probe-host → exit-0 hole — a
  hard-aborted host has zero probes but is *not* silently green). A soft prep failure counts as
  **`PREP_SOFT_FAIL`** — a distinct, visible state in `totals` that does **not** by itself force a non-zero
  exit (a CI gate may still choose to fail on it). The summary `schema_version` is `"4"`.

## Results & summary

`vmlease run` writes a raw, per-host × per-probe transcript; `vmlease summarize` reads it and writes a
versioned `.summary.json` companion whose **exit code** is the gate (see *CI integration & exit codes*). The
raw file is the source of truth and is **never mutated**.

### The raw results file (`results.py`)

```jsonc
{
  "run_id": "...", "timestamp": "...",
  "hosts": [
    {
      "name": "ubuntu-2204",           // the per-host instance identity (auto-named or `name=`)
      "family": "ubuntu", "version": "22.04",   // the resolved (family, version)
      "distro": "ubuntu",              // == family; retained for old readers (back-compat)
      "image": "ubuntu-24.04",
      "restored_image": null,          // snapshot id if a cache hit restored this host, else null (cold miss)
      "detail": "...",                 // the self-describing host-detail snapshot (os-release, kernel, …)
      "prep_phase": [                  // one entry per executed prep step; [] for a no-prep host
        { "id": "_packages", "exit": 0, "required": true, "stderr": "..." },
        { "id": "INSTALL_UV", "exit": 0, "required": true, "stderr": "..." }
      ],
      "probes": [
        {
          "id": "compose-v2", "tag": "read-only", "exit_code": 0,
          "has_assertions": true,      // did the probe declare [probe.assert]?
          "assertion_failures": [],    // describe() of each FAILED assertion (empty = all held)
          "ok": true,                  // AND-of-assertions if has_assertions, else just exit_code == 0
          "timed_out": false,
          "stdout": "...", "stderr": "..."   // full streams (the summary keeps only bounded tails)
        }
      ]
    }
  ]
}
```

### The summary file (`summary.py`)

`summarize` adds `schema_version: "4"`, a computed per-probe `verdict`, the four token buckets, bounded
stream tails (last 2000 chars), a `matrix` pivot (command × host **name**, collapsed worst-of), and `totals`
by verdict. The per-host `name`/`family`/`version` attributes ride additively (the `"4"` bump):

```jsonc
{
  "schema_version": "4",
  "run_id": "...", "timestamp": "...", "battery": "vmlease-compose-plugin-check",  // battery is null without --battery
  "hosts": [
    {
      "name": "ubuntu-2204",           // per-host identity; the matrix pivots on this
      "family": "ubuntu", "version": "22.04",
      "distro": "ubuntu",              // == family (back-compat)
      "image": "ubuntu-24.04", "detail": "...",
      "prep_phase": {                  // step-id-keyed (raw is a list); {} for a no-prep host
        "INSTALL_UV": { "exit": 0, "required": true, "verdict": "", "stderr_tail": "..." }
      },
      "probes": [
        {
          "id": "compose-v2", "command": "...", "tag": "read-only",
          "exit_code": 0, "ok": true, "timed_out": false,
          "verdict": "PASS",           // the ONE canonical computed verdict
          "assertion_failures": [],
          "ok_tokens": [], "fail_tokens": [], "info_tokens": [], "review_tokens": [],
          "stdout_tail": "...", "stderr_tail": "..."
        }
      ],
      "not_run": []                    // declared-but-not-run probe ids — only when --battery is supplied
    }
  ],
  "matrix": { "<command>": { "ubuntu-2204": "PASS", "ubuntu-2404": "FAIL" } },  // keyed by host NAME
  "totals": { "PASS": 3, "FAIL": 1, "TIMEOUT": 0, "PASS_NO_ASSERTIONS": 2,
              "PREP_SOFT_FAIL": 0, "PREP_HARD_FAIL": 0 }
}
```

The per-probe `verdict` is deterministic, in precedence order: `timed_out` → **`TIMEOUT`**; else if the
probe declared `[probe.assert]`, the runner-stored `ok` (the AND of those assertions) is authoritative —
**`PASS`** iff it holds, else **`FAIL`** (overriding exit-code and tokens both ways); else any `*_FAIL` token
or non-zero exit → **`FAIL`**; else a zero exit with an `*_OK` token → **`PASS`**; else (zero exit, no
assertion tokens) → **`PASS_NO_ASSERTIONS`**. A hard prep abort is **`PREP_HARD_FAIL`** (forces a non-zero
exit); a soft prep fail is **`PREP_SOFT_FAIL`** (surfaced, but does not by itself force non-zero).

The `matrix` pivots each command against the host **`name`**, not the distro — so N hosts of the same family
no longer collapse into one column: a run over `ubuntu-2204` and `ubuntu-2404` yields two distinct columns. A
pre-schema raw file (no `name`/`version`) still summarizes — `name` falls back to the raw `distro` field.

Pass `--battery <battery.toml>` to use authoritative command labels and surface declared-but-not-run probes
per host (the `not_run` list); without it a built-in probe-id→command map is the fallback. See
`src/vmlease/summary.py` for the full shape.

## CI integration & exit codes

> **CI gates on `summarize`, never on `run`.** `vmlease run`'s exit code reflects only the run *mechanics*
> (provisioning / teardown) — it does **not** parse `*_OK`/`*_FAIL` tokens or assertion verdicts, and a soft
> prep fail leaves it running. The trustworthy pass/fail verdict — including `PREP_HARD_FAIL` → non-zero —
> lives in `summarize`'s exit code. Always gate on `vmlease summarize <raw>; echo $?`, not on `run`.

Per-verb exit codes:

| Verb | `0` | non-zero |
|------|-----|----------|
| `plan` | plan rendered | `2` on a battery-load / cost-guard / upload-validation error |
| `run` | run completed (mechanics OK) | `1` on a provisioning/teardown failure; `2` on a load/guard/key error. **Never reflects probe verdicts.** |
| `lint` | clean (no finding ≥ `--severity`, no structural violation) | `1` on any finding at/above severity **OR** the structural no-verdict-source violation (always fatal) |
| `summarize` | all probes `PASS` / `PASS_NO_ASSERTIONS` (and no `PREP_HARD_FAIL`) | `1` iff any **failing verdict** is present; `2` on unreadable/invalid raw input or a bad `--battery` |
| `reap-images` | reaped (or `--dry-run`) | `1` on a delete/list failure; `2` on a usage refusal (no selector given) |

The **failing verdicts** that make `summarize` exit non-zero are `FAIL`, `TIMEOUT`, and `PREP_HARD_FAIL`.
A `PREP_SOFT_FAIL` is surfaced in `totals` but does **not** by itself force a non-zero exit (your CI may
still choose to fail on it by inspecting the summary).

```bash
vmlease run       --battery b.toml --run-token ci --results-dir ./out --timestamp "$CI_RUN" --yes
vmlease summarize ./out/vmlease-ci-"$CI_RUN".json --battery b.toml   # THIS exit code is your CI gate
```

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
vmlease build-image --distro family[@version] --run-token <slug> \
        [--server-type cpx22] [--operator probe] [--requires docker] [--rebuild] [--max-images 10] \
        [--ssh-key <name> --ssh-key-path <path>] [--firewall <name>] [--yes]
```

- `--distro` — the `family[@version]` to build (same grammar as `--hosts`, but a single host: no `name=`, no
  multiplicity). A bare family (e.g. `ubuntu`, `arch`) mints its **default version**; `ubuntu@22.04` mints
  that version. An unknown family/version exits 2.
- `--server-type` — builder instance size; **defaults to `cpx22`** (cost-guard allowlisted).
- `--requires` — capability to bake into this variant (e.g. `docker`); repeatable; default builds the
  capability-less variant. A `--requires docker` image is a distinct cache key and never supersedes the
  docker-less one.
- `--rebuild` — replace the existing same-key image (drops the older-created same-key image).
- `--max-images` — vmlease's own self-cap (default `10`); see *No auto-TTL* below.
- `--ssh-key` / `--ssh-key-path` — the hcloud-registered key name + matching local private key, **required
  for rescue-write distros** (e.g. `arch`) and validated *before* any host is provisioned.
- It is idempotent: if a matching image is already cached, `build-image` is a no-op unless `--rebuild`.

### Restore on `run`

A `run` automatically restores from a cached image when one **matches** — same content key, same
architecture, and the snapshot fits the chosen server's disk. On a hit, the host is created directly from
the snapshot with a **minimal key-only cloud-init**, skipping both the rescue-write *and* the package
install. A `run` **consumes but never builds** — it makes zero `create_image` calls. Each host's results
record **`restored_image`** — the snapshot id it restored from on a hit, or `null` on a cold miss — so a
cache hit/miss is observable in the results (a permanent silent miss can't hide behind passing probes).

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
vmlease reap-images [--distro <family>] [--older-than <ISO-8601>] [--superseded] [--dry-run]
```

- `--distro <family>` — scope to all cached images of one **family**, **every version** (an explicit
  per-family cache clear).
- `--older-than <ISO-8601>` — reap images created before the cutoff (validated fail-closed; no clock read).
- `--superseded` — reap off-current-key images; supersession groups per **`(family, version)`** (so no
  version's image is ever pruned by another version's), each group's current key resolved fail-safe, and any
  unresolvable group is **kept and warned**, never blindly deleted.
- `--dry-run` — report what *would* be deleted and delete nothing (the preview/safety gate).

A bare `reap-images` with no selector is **refused** (exit `2`) — never an implicit whole-cache wipe.

### The content key

Cache validity is keyed on **family + architecture + the rendered prep recipe + the resolved upstream
image** — `v1-<family>-<hash>`, where the hash folds the base-image fingerprint and the canonical
cloud-init together. The base-image slug differs per version, so each `(family, version)` already gets a
distinct content key; a `vmlease-version` label additionally carries the version so supersession groups
per `(family, version)` (no cross-version prune). So a recipe change or an upstream image change yields a
*new* key, and the next
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

### Arch / rescue-write: the SSH key requirement

A `run` or `build-image` whose matrix touches a **rescue-write distro** (currently only `arch`, which has no
native Hetzner image and is written via the GPG-verified rescue system) requires **two distinct SSH keys**,
both validated **before any spend**:

- `--ssh-key` — the name of an **hcloud-registered** key, injected into the rescue system so its root login
  accepts you;
- `--ssh-key-path` — the **matching local private key**, used for the root SSH into that rescue system.

Both are required **up front**, even for a `run` that is likely to be a cache hit: the hit/miss decision is
only known *after* provisioning, so a miss can still fall through to a rescue-write, and the harness refuses
to provision anything it might not be able to finish. These two keys are **distinct from** the throwaway,
per-run keypair vmlease generates for the operator's (`probe`) probe SSH — that one is created and cleaned up
automatically and is never the rescue key.

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
