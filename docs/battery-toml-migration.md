# Migrating a battery from JSON to the TOML bundle format

vmlease's battery input format changed **breaking, with no compatibility shim**: the JSON file whose
probes carried a single-line, JSON-escaped `command` string is replaced by a **TOML bundle** — a
`battery.toml` manifest plus co-located `.sh` script files. `parse_battery`/`load_battery` no longer
accept JSON. This guide migrates an existing JSON battery in four steps, lists every behavioral
difference to check, and ends with a troubleshooting table.

**Why it changed:** probe commands had become multi-KB single-line bash blobs — unreviewable,
un-editable without substring surgery, and impossible to shellcheck (findings pointed at "line 2,
column 1341" of a blob). As real shell files, probes are diffable and lintable: `vmlease lint`
shellchecks every probe and reports real `file:line:col` locations, and is severity-gated for use as
a CI/pre-commit gate.

**What did NOT change:** the **results** document is still JSON (and `vmlease summarize` still reads
it); `tag` still means what a probe touches; per-probe `timeout` semantics, the consecutive-timeout
breaker, uploads, teardown/reap — all unchanged. `ok` = exit code **unless a probe declares
`success_when`** (a new optional key — see below). The resolved command runs **verbatim**: `tag` does
not inject, strip, or enforce `sudo`; the `mutating:host-root` tag authorizes and records escalation,
and `vmlease lint` emits an advisory warning when a non-host-root probe invokes `sudo` (a mislabel,
not a block). The `plan`/`run`/`status`/`reap` CLI surface is unchanged except that `--battery` now
takes the manifest path.

**New optional key — `success_when`:** a probe may declare a single literal token (e.g.
`success_when = "CORE_RUNNING_OK"`). When present, it **replaces** the exit-code reading: `ok` is true
iff that token appears as a *complete line* of the probe's stdout (substring matches do not count, and
the exit code is ignored). Such a probe needs no `rc=0; …; exit $rc` plumbing and is exempt from the
vacuous-ok lint warning. A timed-out probe is never `ok` regardless of `success_when`. Omit the key to
keep the exit-code reading; existing batteries that never declare it behave exactly as before.

---

## The format at a glance

Old (`mybattery.json`):

```json
{
  "name": "mybattery",
  "probes": [
    {"id": "KVER", "title": "running kernel", "command": "uname -r", "tag": "read-only"},
    {"id": "SETUP", "title": "install + verify",
     "command": "set -x; rc=0; sudo apt-get install -y foo 2>&1 | tail -3; [ \"$?\" -eq 0 ] || rc=1; exit $rc",
     "tag": "mutating:host-root", "timeout": 120}
  ]
}
```

New — a directory ("bundle") holding `battery.toml` + any scripts:

```
mybattery/
  battery.toml
  setup.sh
```

```toml
name = "mybattery"

[[probe]]
id = "KVER"
title = "running kernel"
tag = "read-only"
run = '''uname -r'''            # short probes stay inline: TOML literal block, zero escaping

[[probe]]
id = "SETUP"
title = "install + verify"
tag = "mutating:host-root"
timeout = 120
script = "setup.sh"             # long probes become real, shellcheckable files
```

`setup.sh` is the old `command` string, un-escaped, as a normal multi-line script:

```bash
#!/usr/bin/env bash
set -x
rc=0
sudo apt-get install -y foo 2>&1 | tail -3
[ "$?" -eq 0 ] || rc=1
exit $rc
```

## Field mapping

| Old JSON | New TOML | Notes |
|---|---|---|
| `name` | `name` | unchanged |
| `probes: [...]` | `[[probe]]` tables | one table per probe, **in execution order** (see step 2) |
| `id`, `title`, `tag`, `classifies`, `timeout` | same keys | unchanged meaning and validation |
| `command` | **exactly one of** `run = '''…'''` or `script = "file.sh"` | `run` for one-liners; `script` for anything non-trivial |
| *(none)* | `success_when = "TOKEN"` | **new, optional** probe key — `ok` becomes "TOKEN is a complete stdout line" instead of exit-code 0 (see above); omit to keep exit-code semantics |

The schema is **strict**: any key not listed above — at the root or on a probe — is rejected by name
(a `timout` typo now fails loud instead of silently using the default). An empty `run` block or empty
script file is rejected. A probe with neither or both of `run`/`script` is rejected.

`script` paths resolve relative to the manifest and are **contained to the bundle**: absolute paths,
`..` escapes, and symlinks whose real target lies outside the bundle directory are all rejected. Keep
the manifest and its scripts together; the directory is a portable unit.

## Migration steps

### 1. Extract each `command` (one-time helper)

Run this against your old JSON battery to scaffold the bundle — it writes `battery.toml` plus one
`.sh` per non-trivial probe, preserving the field values:

```bash
python3 - mybattery.json mybattery/ <<'EOF'
import json, sys
from pathlib import Path

src, out = Path(sys.argv[1]), Path(sys.argv[2])
doc = json.loads(src.read_text())
out.mkdir(parents=True, exist_ok=True)
lines = [f'name = "{doc["name"]}"']
for p in doc["probes"]:
    lines += ["", "[[probe]]", f'id = "{p["id"]}"', f'title = "{p["title"]}"', f'tag = "{p["tag"]}"']
    if p.get("classifies"):
        lines.append(f'classifies = "{p["classifies"]}"')
    if "timeout" in p:
        lines.append(f'timeout = {p["timeout"]}')
    cmd = p["command"]
    if len(cmd) <= 80 and "'''" not in cmd and "\n" not in cmd:
        lines.append(f"run = '''{cmd}'''")
    else:
        sh = out / f'{p["id"].lower()}.sh'
        sh.write_text("#!/usr/bin/env bash\n" + cmd + "\n")
        lines.append(f'script = "{sh.name}"')
(out / "battery.toml").write_text("\n".join(lines) + "\n")
print("wrote", out / "battery.toml")
EOF
```

(Verified against a real pre-migration battery: the scaffold loads and lints cleanly at the default
`error` threshold on the first try.)

The scaffold is correct but ugly: extracted scripts are still one-line blobs. The payoff step is
re-formatting each `.sh` into readable multi-line shell — semicolons to newlines, real indentation,
comments. Behavior is unchanged (it is the same shell text); review the diff of meaning as you go.

### 2. Check the probe ORDER — the one behavioral change

**Probes now execute in authoring order. `tag` no longer reorders.** The old runner sorted by
tag-rank (read-only → operator-space → host-root, stable within each rank); the manifest is now the
execution order, period.

- If your JSON probes were **already written in dependency order** (typical for pipeline-style
  batteries like `PREP → SETUP → START → …` where everything shares one tag), nothing changes —
  the order you read is the order that runs.
- If you **relied on the auto-sort** (e.g. read-only checks written last but expected to run first),
  you MUST reorder the `[[probe]]` tables to the old *execution* order: all `read-only` probes
  first, then `mutating:operator-space`, then `mutating:host-root`, preserving your written order
  within each group. That reproduces the old behavior exactly.
- Silver lining: ordering needs no tag tricks anymore. A read-only verifier that must run *after* a
  host-root setup is simply authored after it — and can now be honestly tagged `read-only`.

### 3. Lint (zero spend)

```bash
vmlease lint --battery mybattery/battery.toml                      # gate at error (default)
vmlease lint --battery mybattery/battery.toml --severity warning   # stricter, opt-in
```

Expect findings on a freshly-migrated battery — real-world corpora typically show `note`/`warning`
level issues (the `A && B || C` token footgun, `SC2155`) and zero `error`s, so the default gate stays
green while showing you what to clean up, now with real `file:line:col` locations. In a CI/pre-commit
context add `--require-shellcheck` so a missing shellcheck binary fails the gate instead of skipping.

Probes are authored as **bash** (that is the dialect lint checks). This was already true in practice
(`PIPESTATUS` etc. only work because the shipped images give the operator bash); it is now the
documented contract.

### 4. Plan, then run

```bash
vmlease plan --battery mybattery/battery.toml --distros ubuntu --run-token migrate-check
```

`plan` loads + resolves the full bundle (catching any containment/empty/strict-schema error) with
zero provider calls. If `plan` is happy, `run` behaves exactly as before.

## Troubleshooting

| Error (`BatteryError`) | Cause | Fix |
|---|---|---|
| `unrecognized key 'X'` | a typo'd or removed field (e.g. `timout`, `command`) | rename to a valid key; `command` no longer exists — use `run` or `script` |
| `probe #N must declare exactly one of 'run'/'script'` (neither/both) | missing or doubled command form | pick one per probe |
| `script ... escapes the bundle` / absolute path / symlink | the script lives outside the manifest's directory (possibly via symlink) | move the real file into the bundle dir |
| `cannot read script ...` | path typo, or the file wasn't copied with the manifest | the bundle travels as one directory |
| empty command | empty/whitespace-only `run` block or script file | a probe must do something; delete it or fill it in |
| TOML parse error | usually quoting — remember `'''…'''` literal blocks need **no** escaping | if your shell itself contains `'''`, use a `script` file |
| probe runs "too early/late" | you relied on the old tag-rank auto-sort | reorder the `[[probe]]` tables (step 2) |

## Known consumers

- `examples/compose-plugin-check/` in this repo is the migrated reference bundle (it is also lint-gated
  in `make check`).
- sandbox-ai `tests/vmlease/` — **migrated** (its `modernize-vmlease-batteries` change). The three
  per-command lifecycle batteries are now TOML bundles (`baseline-op-rootless/`,
  `baseline-separate-user-sudo/`, `baseline-separate-user-polkit/`, each a `battery.toml`). They were
  already in dependency order (one dominant rank), so step 2 was a no-op. Note the authoring choice: the
  probes are **inline `run = '''…'''` blocks**, NOT per-probe `.sh` files — inline blocks are
  runtime- and shellcheck-equivalent to `script` files (both resolve to the probe command, both lint via
  stdin), and keep each battery reviewable in one manifest. Reach for a `script` file only when a probe is
  genuinely large; the ">80 chars → its own file" scaffold heuristic over-produces files for a
  many-similar-probe lifecycle battery.
