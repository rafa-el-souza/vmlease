# probe-workload Specification

## Purpose
The probe battery workload: declarative `Probe`/`Battery` data loaded from a TOML manifest whose probes carry real shell (a co-located script file or an inline block, shellcheckable via the severity-gated `vmlease lint`), executed in authoring order over SSH, with a self-describing host-detail snapshot and timestamped JSON results (timestamp injected, not clock-read).
## Requirements
### Requirement: Each probe is run over SSH as the operator and its outcome captured

The system SHALL run each probe's command over SSH as the non-root operator and capture its exit code,
stdout, and stderr. A probe's `ok` SHALL be determined by its **declared assertions**: when the probe
declares one or more assertions (a `[probe.assert]` table), `ok` is true exactly when **every** declared
assertion holds against the captured `(exit_code, stdout, stderr)`, and the system SHALL record **which**
assertion(s) failed; absent any declared assertion, `ok` is true exactly when the probe exits zero
(interpretation of a non-zero exit is per-probe — an expected fail is still recorded data, not a run
error). The system SHALL evaluate assertions **once, at run time**, and record the resulting `ok` together
with the failed-assertion descriptions on the probe result; `ok` is that recorded outcome and SHALL NOT be
re-derived by downstream consumers (the summarizer and the results file trust the recorded `ok`). A
timed-out probe SHALL be recorded **not ok regardless of its declared assertions** — the timed-out
determination takes precedence over assertion evaluation, so a killed probe's partial `(124, partial
output)` can never satisfy an assertion (e.g. an `exit_not = 0`) into a pass; a killed probe's partial
output is not a verdict. The system SHALL execute the resolved command **verbatim**: it SHALL NOT inject, strip, or refuse
`sudo` based on the probe's tag. Escalation is an authoring contract — the author writes `sudo` in the
command, and the host-root tag authorizes and records that escalation — backed by an advisory lint warning
(see the lint requirement), not by an enforcement gate. Each probe SHALL be bounded by a timeout: a probe
MAY declare an optional per-probe `timeout`, and absent one it SHALL inherit a run-wide default supplied by
the caller, so no single probe can block the battery — and thus the host's teardown — without bound. The
battery manifest SHALL remain back-compatible: a probe without a `timeout` is valid and uses the run-wide
default. A probe that exceeds its timeout SHALL be recorded as a timed-out result (marked distinctly from a
non-zero exit) and SHALL NOT abort the battery — the remaining probes still run, exactly as for a non-zero
exit. To bound the cost of a wedged host, after a configurable number of **consecutive** timed-out probes
(default 2) the system SHALL stop running the rest of that host's battery and record why, rather than
spending a full timeout on every remaining probe.

Assertions SHALL evaluate against the captured streams with these semantics: `stdout_has`/`stdout_lacks`
(and the stderr pair) match a **literal substring** of the stream; `stdout_matches`/`stdout_matches_not`
(and the stderr pair) evaluate an **unanchored regex search** (the engine's `search`, not an anchored
`match`/`fullmatch`) — `^`/`$` anchor to the whole captured text, not per line, unless the pattern opts into
multiline matching; `stdout_empty`/`stderr_empty` treat a stream as empty when it is empty after stripping
surrounding whitespace, with a `true` value asserting empty and a `false` value asserting non-empty;
`exit`/`exit_not` compare the captured exit code. **Against an empty stream the predicates evaluate by
their plain definition, with no special case:** a `_has`/`_matches` over empty output does **not** hold
(nothing is present to match), and a `_lacks`/`_matches_not` over empty output **does** hold (the forbidden
content is vacuously absent). An author who needs "produced the expected output AND lacked the bad string"
SHALL pair the absence assertion with a presence assertion or a `*_empty = false` — the absence predicate
alone passing on no-output is intended, not a failure to detect. An assertion value MAY be a **list**, which
conjoins: a list for `_has`/`_matches` holds when **every** element holds, a list for `_lacks`/`_matches_not`
holds when **none** of the elements holds. When a probe declares mutually unsatisfiable assertions (e.g.
`exit` and `exit_not` of the same value), no special-casing applies — the probe is simply never `ok`.

#### Scenario: A probe with no assertions is ok iff it exits zero

- **WHEN** a probe declaring no `[probe.assert]` table runs on a ready host
- **THEN** its exit code, stdout, and stderr are recorded, with ok set iff the exit code is zero

#### Scenario: Declared assertions decide ok instead of the exit code

- **WHEN** a probe declares `[probe.assert]` with `stdout_has = "CORE_RUNNING_OK"`, its stdout contains
  that substring, and it exits non-zero
- **THEN** its result is ok — every declared assertion holds, and the exit code does not separately gate ok

#### Scenario: A failing assertion makes the probe not ok and is named

- **WHEN** a probe declares `stdout_has = "READY"` but its stdout never contains `READY`
- **THEN** its result is not ok, and the failing assertion (`stdout_has "READY"`) is recorded

#### Scenario: An exit-negation assertion passes on an expected failure

- **WHEN** a refusal probe declares `exit_not = 0` and its command exits non-zero
- **THEN** its result is ok — the declared assertion holds without the author hand-inverting the exit code

#### Scenario: A regex assertion matches anywhere by default

- **WHEN** a probe declares `stdout_matches = "READY|LISTENING"` and its stdout contains `server LISTENING now`
  embedded in a longer line
- **THEN** its result is ok — the pattern is searched unanchored across the captured text

#### Scenario: Per-line anchoring requires opting into multiline

- **WHEN** a probe declares `stdout_matches = '(?m)^DONE$'` and its multi-line stdout has `DONE` as its own
  line
- **THEN** its result is ok — `(?m)` makes `^`/`$` match line boundaries, where an un-flagged `^DONE$`
  would require `DONE` to be the entire output

#### Scenario: A list assertion conjoins

- **WHEN** a probe declares `stdout_has = ["ALPHA", "BETA"]` and its stdout contains `ALPHA` but not `BETA`
- **THEN** its result is not ok — every element of a `_has` list must hold, and the missing `BETA` is the
  recorded failure

#### Scenario: An emptiness assertion gates on a stripped stream

- **WHEN** a probe declares `stderr_empty = true` and its stderr contains only whitespace (e.g. a trailing
  newline)
- **THEN** its result is ok — a stream that is empty after stripping surrounding whitespace counts as empty

#### Scenario: A timed-out refusal probe is not ok despite a satisfiable exit assertion

- **WHEN** a probe declaring `exit_not = 0` exceeds its timeout (recorded with the timeout sentinel exit and
  `timed_out` true)
- **THEN** the result is recorded as timed out and is **not ok** — the timed-out determination precedes
  assertion evaluation, so the non-zero sentinel does not satisfy `exit_not = 0` into a pass

#### Scenario: An absence assertion holds vacuously over empty output

- **WHEN** a probe declares `stdout_lacks = "ERROR"` (or `stderr_matches_not = "fail"`) and the
  corresponding stream is empty
- **THEN** the assertion holds — the forbidden content is vacuously absent — and a presence assertion or
  `*_empty = false` is the author's tool when "produced output" must also be required

#### Scenario: A non-empty assertion holds on a stream with content

- **WHEN** a probe declares `stdout_empty = false` and its stdout contains non-whitespace content
- **THEN** the assertion holds; conversely an empty-after-stripping stdout would make it fail

#### Scenario: The command runs verbatim regardless of tag

- **WHEN** a probe not tagged host-root has `sudo` in its command
- **THEN** the command is executed unchanged (the mismatch is the lint's concern, not the runner's)

#### Scenario: A non-zero probe does not abort the battery

- **WHEN** a probe exits non-zero
- **THEN** its result is recorded and the remaining probes still run

#### Scenario: A probe without a declared timeout uses the run-wide default

- **WHEN** a battery probe declares no `timeout`
- **THEN** it is run bounded by the run-wide default timeout, and the battery loads without error

#### Scenario: A probe's declared timeout overrides the run-wide default

- **WHEN** a battery probe declares its own `timeout`
- **THEN** that probe is bounded by its declared value rather than the run-wide default

#### Scenario: A single timed-out probe is recorded and the battery continues

- **WHEN** one probe times out but the following probes do not
- **THEN** the timeout is recorded as a timed-out result and every subsequent probe still runs, so the
  host's collected results are preserved rather than discarded

#### Scenario: Consecutive timeouts stop the battery for a wedged host

- **WHEN** the configured number of consecutive probes (default 2) all time out
- **THEN** the system stops running the rest of that host's battery, records that it stopped and why, and
  the host still tears down — bounding wasted time at roughly K×timeout rather than N×timeout

### Requirement: A host-detail snapshot heads each host's results

The system SHALL capture a self-describing host-detail snapshot (os-release, kernel, init, cgroup, id,
tool inventory) before the battery, as the results header.

#### Scenario: Results carry a host-detail header

- **WHEN** a host is probed
- **THEN** its result includes the host-detail snapshot alongside the per-probe results

### Requirement: Results are written as timestamped JSON with an injected timestamp

The system SHALL write each run's results to a timestamped JSON file whose timestamp is supplied by the
caller (not read from the clock), keeping the output deterministic and testable. Results SHALL be
persisted **incrementally, as each host completes**, so that a run aborted partway through (for example by
an operator `KeyboardInterrupt`) still leaves a results file containing every host that finished — only an
in-flight host is absent. Each **per-probe record** in the results file SHALL carry the probe's `id`,
`tag`, `exit_code`, `timed_out` flag, recorded `ok`, captured `stdout`/`stderr`, a boolean
**`has_assertions`** (true when the probe declared one or more assertions), and **`assertion_failures`**
(the descriptions of the assertions that did not hold — empty when the probe declared none or all held).
The retired `success_when` field SHALL NOT appear in the per-probe record. This is the producer side of the
contract the `summarize` consumer relies on (it reads `has_assertions` + the recorded `ok` and does not
re-evaluate assertions). The document is rewritten with the completed-so-far set each time a host finishes.

#### Scenario: The results timestamp is caller-supplied

- **WHEN** results are written
- **THEN** the filename and content use the injected timestamp, with no wall-clock read in the library

#### Scenario: A per-probe record carries the assertions signal, not success_when

- **WHEN** a probe that declared assertions is written to the results file
- **THEN** its record carries `has_assertions` true and lists any `assertion_failures`, and carries no
  `success_when` field

#### Scenario: A completed host's result survives an abort

- **WHEN** at least one host has completed and the run is then aborted before the remaining hosts finish
- **THEN** the results file already on disk contains the completed host(s), and only the in-flight host is
  missing

### Requirement: Caller-specified files are uploaded to each host before the battery

The system SHALL upload each caller-specified local file to every host over SSH, after the host's
readiness gate and before the probe battery (including the host-detail snapshot), so the file is present
for the first probe. An upload SHALL be transferred to the caller-specified remote destination (default
`~/<basename>`). If an upload does not complete, the system SHALL fail that host with a transport error
(an `SshError`-class host failure recorded as an error result with no probe results) — not a probe
non-zero — and SHALL still tear the host down.

#### Scenario: An upload lands before the first probe

- **WHEN** a host becomes ready and the run has one or more upload specs
- **THEN** every upload is transferred over SSH after readiness and before the host-detail snapshot and
  battery, so the uploaded file is present for the first probe

#### Scenario: An upload transport failure fails the host, not a probe

- **WHEN** an upload's transfer exits non-zero (a transport failure)
- **THEN** the host is recorded as an `SshError`-class host failure with no probe results, the remaining
  battery does not run on that host, and the host is still torn down — it is not recorded as a probe
  non-zero result

#### Scenario: No upload spec leaves the lifecycle unchanged

- **WHEN** a run has no upload specs
- **THEN** no upload step runs and the host lifecycle is identical to a run without the upload feature

### Requirement: A loaded battery is linted for footguns, surfaced non-fatally

The system SHALL lint a loaded battery and surface **non-fatal** warnings at load; the lint itself SHALL
NOT raise, reorder probes, or alter any probe's `ok` semantics — at load it only warns. Two footguns are
checked:

- **no-verdict-source (structural)** — a probe that declares **no** assertions and contains an un-gated
  token-printing `echo` (a `&& echo` / `|| echo` **anywhere** in the command, or a command whose final
  statement is an `echo`) with **no** statement-level `exit` to make the status reflect it: its `ok`
  reflects only the exit code, so the printed result is vacuous. Detection reuses the existing
  un-gated-echo heuristic exactly (`_looks_vacuously_ok`: a `&& echo`/`|| echo` substring anywhere, or a
  trailing-`echo` final statement, when no statement-level `exit` is present; the shell is not parsed and
  it does **not** scan for `*_OK`/`*_FAIL` token strings), only swapping the prior `success_when`-exempt
  path for an **assertions-declared**-exempt path: a probe that declares one or more assertions SHALL NOT
  be flagged (its `ok` is evaluated from the assertions), nor SHALL a probe with a statement-level `exit`
  or with no such echo (an honest exit-code or diagnostic probe). The `vmlease lint` command escalates
  this same rule to a hard failure (see the lint-command requirement).
- **non-host-root sudo** — a probe whose tag is not host-root but whose command invokes `sudo`: the
  escalation authoring contract reserves `sudo` for host-root-tagged probes, so the mismatch means the
  tag is lying about what the probe does. The command still runs verbatim; the warning surfaces the
  mislabel.

(The former "order-surprise" footgun no longer exists: execution order is now authoring order — see
"Probes run in authoring order" — so there is no hidden reordering to warn about.)

#### Scenario: A probe with no verdict source warns

- **WHEN** a probe declares no assertions, prints success/failure tokens, and is not gated with an
  explicit exit
- **THEN** a non-fatal warning naming the probe is surfaced at load (its `ok` reflects only the exit
  code), and the run still proceeds

#### Scenario: A probe declaring assertions is exempt from the no-verdict-source warning

- **WHEN** a probe declares one or more assertions and its command prints tokens without an explicit exit
- **THEN** no no-verdict-source warning is surfaced for it — its `ok` is evaluated from the assertions

#### Scenario: A non-host-root probe invoking sudo warns

- **WHEN** a probe tagged read-only or operator-space has `sudo` in its command
- **THEN** a non-fatal warning naming the probe is surfaced, and the run still proceeds with the
  command unchanged

#### Scenario: A host-root probe invoking sudo does not warn

- **WHEN** a probe tagged host-root has `sudo` in its command
- **THEN** no sudo warning is surfaced — escalation is what the tag authorizes

#### Scenario: A clean battery warns nothing

- **WHEN** every probe either declares assertions or is honestly exit-gated, and no non-host-root probe
  invokes sudo
- **THEN** no lint warning is surfaced

### Requirement: A battery is declarative data loaded from a TOML manifest

The system SHALL load a named probe battery from a **TOML manifest**, where each probe is a declarative
record carrying a stable id, title, tag, optional classification, optional timeout, and an optional
`[probe.assert]` table of assertions, plus its command expressed as **exactly one of** an inline `run`
block (a literal shell string) or a `script` reference to a co-located shell file. An **assertion** is a
named predicate over the captured `(exit_code, stdout, stderr)`; the manifest SHALL accept the assertion
keys `exit`, `exit_not`, `stdout_has`, `stdout_lacks`, `stderr_has`, `stderr_lacks`, `stdout_matches`,
`stdout_matches_not`, `stderr_matches`, `stderr_matches_not`, `stdout_empty`, and `stderr_empty`. The
manifest SHALL also accept, at the **root**, an optional `requires` list and an optional `[prep]` section;
these are recognized root keys and SHALL NOT be rejected as unknown. Their detailed schema and semantics
are defined by the `host-capabilities` capability (`requires`) and the `battery-prep` capability (`[prep]`);
this requirement only establishes that the loader accepts them at the root alongside `name` and the
`[[probe]]` array. The system SHALL raise a clear error for a malformed battery — invalid TOML, a missing
required field, an unknown tag, an **unrecognized key** at the root (any root key other than `name`,
`probe`, `requires`, `prep`), on a probe, or **in the `[probe.assert]` table** (a typo
like `timout` or `stdout_have` SHALL fail loud, naming the key, rather than silently falling back to a
default), a probe declaring **neither** `run` nor `script`, a probe declaring **both**, an assertion whose
value has the wrong shape (e.g. a non-integer `exit`, a non-string `stdout_has`, a non-boolean
`stderr_empty`), an assertion whose value is an **empty list** (a no-op assertion), or a regex assertion
(`*_matches`/`*_matches_not`) whose pattern is malformed, uses an unsupported construct, or whose compiled
program exceeds the regex engine's memory budget. These regex defects SHALL be detected when the pattern is
**compiled at load**, before any run — the linear-time regex engine guarantees that a pattern compiling
within budget then matches correctly at run time, so there is no runtime memory-overflow or backtracking
failure mode that could silently turn a match into a non-match. An `exit`/`exit_not` value MAY be any
integer — no operating-system exit-range validation is applied (a code that can never occur simply never
matches). The manifest is parsed with the standard library, except the regex engine for `*_matches`
assertions; the results document this feeds remains JSON.

#### Scenario: A well-formed battery loads

- **WHEN** a valid battery TOML manifest is loaded
- **THEN** a named battery of probes is returned, each probe carrying its resolved command and its parsed
  assertions

#### Scenario: A probe declaring assertions loads and carries them

- **WHEN** a probe declares `[probe.assert]` with `exit = 0` and `stdout_has = "SETUP_OK"`
- **THEN** the battery loads and that probe carries both assertions for the runner's ok evaluation

#### Scenario: requires and [prep] are accepted at the root

- **WHEN** a manifest carries a root-level `requires` list and/or a `[prep]` section alongside `name` and `[[probe]]`
- **THEN** the battery loads (the keys are recognized, validated per the host-capabilities and battery-prep capabilities), not rejected as unknown root keys

#### Scenario: A malformed battery is rejected

- **WHEN** a battery manifest is invalid TOML, missing a required field, or names an unknown tag
- **THEN** the system raises a battery-load error

#### Scenario: An unrecognized assertion key is rejected

- **WHEN** a `[probe.assert]` table carries a key the schema does not define (e.g. `stdout_have`)
- **THEN** the system raises a battery-load error naming the unrecognized assertion key and the probe

#### Scenario: A wrong-shaped assertion value is rejected

- **WHEN** an assertion's value has the wrong type (e.g. `exit = "zero"` or `stderr_empty = "yes"`)
- **THEN** the system raises a battery-load error naming the probe and the assertion

#### Scenario: An empty-list assertion value is rejected

- **WHEN** an assertion declares an empty list (e.g. `stdout_has = []`), which would assert nothing
- **THEN** the system raises a battery-load error naming the probe and the assertion

#### Scenario: A malformed regex assertion is rejected at load

- **WHEN** a probe declares a `*_matches` assertion whose pattern does not compile (malformed, or an
  unsupported construct such as a backreference)
- **THEN** the system raises a battery-load error naming the probe and the assertion, before any run

#### Scenario: A probe must declare exactly one command form

- **WHEN** a probe declares neither `run` nor `script`, or declares both
- **THEN** the system raises a battery-load error naming the offending probe

#### Scenario: An unrecognized key is rejected

- **WHEN** a manifest carries a key the schema does not define (e.g. a `timout` typo for `timeout`, or an unknown root key that is not `requires`/`prep`)
- **THEN** the system raises a battery-load error naming the unrecognized key

### Requirement: Probes run in authoring order

The system SHALL execute a battery's probes in the order they appear in the manifest — the manifest **is**
the execution order, regardless of tag. A probe's `tag` records what the probe touches (and is the
authoring contract that authorizes sudo escalation — see the probe-outcome requirement) but SHALL NOT
reorder execution. Results SHALL be recorded in that same authoring order. This lets a probe's
prerequisites be expressed directly — author the prerequisite probe earlier — and makes the results
sequence match the manifest, so a result is interpreted in the order it was written.

#### Scenario: Probes run top-to-bottom as written

- **WHEN** a battery's probes are executed
- **THEN** they run in manifest order regardless of tag, and the results are recorded in that same order

#### Scenario: A later probe may depend on an earlier one regardless of tag

- **WHEN** a read-only verification probe is authored after a host-root setup probe it checks
- **THEN** the setup probe runs first and the verification runs after it, exactly as authored

### Requirement: A probe's command resolves from a co-located script or an inline block, contained to the bundle

The system SHALL resolve each probe's command before execution: a `run` probe's command is its inline
literal block verbatim; a `script` probe's command is the contents of the referenced file, read from a
path resolved **relative to the manifest's directory**. A `script` path SHALL be **contained to the
bundle**: the system SHALL reject an absolute path, a relative path that escapes the manifest directory
(via `..`), and a path whose **real (symlink-resolved) location** falls outside the manifest directory —
so a manifest cannot reach a file beyond its own bundle by any means, including a symlink (mirroring the
`upload_dir` transport's existing `--safe-links` posture). A referenced script that is missing or
unreadable SHALL be a clear battery-load error naming the probe and the path. The resolved command text
SHALL be **non-empty** — an empty `run` block or an empty (or whitespace-only) script file is a
battery-load error naming the probe, since an empty command is a vacuous always-pass probe. The **resolved
command text is executed unchanged** by the downstream transport — the command's origin (file or inline)
does not alter how it runs; only its provenance is retained for linting and error messages.

#### Scenario: A script reference resolves and runs

- **WHEN** a probe declares `script = "prep.sh"` and `prep.sh` sits beside the manifest
- **THEN** the probe's command is the file's contents, executed exactly as an inline command would be

#### Scenario: An inline run block runs verbatim

- **WHEN** a probe declares an inline `run` block
- **THEN** the probe's command is that block's text, executed unchanged

#### Scenario: A script path escaping the bundle is rejected

- **WHEN** a probe's `script` path is absolute, escapes the manifest directory with `..`, or is a symlink
  whose real target lies outside the manifest directory
- **THEN** the system raises a battery-load error and loads no battery

#### Scenario: A missing script file is a clear error

- **WHEN** a probe's `script` references a file that does not exist or cannot be read
- **THEN** the system raises a battery-load error naming the probe and the path

#### Scenario: An empty command is rejected

- **WHEN** a probe's `run` block is empty, or its referenced script file is empty
- **THEN** the system raises a battery-load error naming the probe

### Requirement: A battery is shellchecked via a severity-gated lint command

The system SHALL provide a `lint` command that loads a battery bundle and runs `shellcheck` over **every**
probe's resolved shell text **and over every `[[prep.setup]]` step's resolved shell text** (both inline `run`
blocks and `script` references), in bash mode, labelling each finding with its provenance (the script
file path — whose reported `line:col` aligns with the file, since the resolved text is the file's
content — or the probe/prep-step id for an inline block). In addition to the shellcheck pass, the `lint` command
SHALL enforce the **structural verdict-source rule as a hard failure**: a probe that declares no assertions
and contains an un-gated token-printing `echo` (a `&& echo`/`|| echo` anywhere, or a trailing-`echo` final
statement, with no statement-level `exit` — the same detection the load-time advisory uses) SHALL cause the
command to **exit non-zero**, naming the probe — so a battery whose `ok` would be vacuous cannot pass the
gate (at load the same footgun is only an advisory warning; the `lint` command is where it is fatal). The structural verdict-source rule applies to probes only (prep steps carry no verdict). The command SHALL report the
findings and SHALL **exit non-zero when any shellcheck finding is at or above a configurable severity
threshold (default `error`)**, so it is usable as a CI gate; the threshold SHALL be selectable (`error`,
`warning`, or `note`) so a stricter gate can be opted into. Probe and prep commands are authored as **bash** — the
dialect lint checks; guaranteeing that dialect at the execution transport is a separate follow-up change.
When `shellcheck` is not installed, the command SHALL surface a notice and skip the shellcheck pass (still
running the structural and advisory checks) rather than crashing — unless the caller passes a strict flag
requiring shellcheck's presence, in which case its absence SHALL be a non-zero exit, so a gate cannot read
green merely because the linter is missing.

#### Scenario: A clean battery passes the gate

- **WHEN** `lint` runs over a battery whose probes and prep steps have no shellcheck findings at or above the threshold
  and no structural verdict-source violation
- **THEN** the findings (if any, below threshold) are reported and the command exits zero

#### Scenario: A prep script with a threshold finding fails the gate

- **WHEN** `lint` runs over a battery whose `[[prep.setup]]` script has a shellcheck finding at or above the active threshold
- **THEN** the finding is reported, labelled with the prep step's provenance, and the command exits non-zero

#### Scenario: A battery with a threshold finding fails the gate

- **WHEN** `lint` runs over a battery with a shellcheck finding at or above the active severity threshold
- **THEN** the finding is reported and the command exits non-zero

#### Scenario: A structural verdict-source violation fails the gate

- **WHEN** `lint` runs over a battery containing a probe that declares no assertions and contains an
  un-gated token-printing `echo` (a `&& echo`/`|| echo` anywhere, or a trailing `echo`) with no
  statement-level `exit`
- **THEN** the violation is reported naming the probe and the command exits non-zero, even if shellcheck
  finds nothing at or above the threshold

#### Scenario: The severity threshold is selectable

- **WHEN** `lint` is run with a stricter threshold than the default `error`
- **THEN** findings at or above that threshold (e.g. warnings) cause a non-zero exit

#### Scenario: A missing shellcheck is skipped, not fatal

- **WHEN** `shellcheck` is not installed
- **THEN** `lint` surfaces a notice, skips the shellcheck pass, still runs the structural and advisory
  checks, and does not crash

#### Scenario: A gate can refuse to pass without shellcheck

- **WHEN** `lint` is run with the require-shellcheck flag and `shellcheck` is not installed
- **THEN** the command exits non-zero instead of skipping

