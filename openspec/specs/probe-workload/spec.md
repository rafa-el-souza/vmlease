# probe-workload Specification

## Purpose
The probe battery workload: declarative `Probe`/`Battery` data loaded from a TOML manifest whose probes carry real shell (a co-located script file or an inline block, shellcheckable via the severity-gated `vmlease lint`), executed in authoring order over SSH, with a self-describing host-detail snapshot and timestamped JSON results (timestamp injected, not clock-read).
## Requirements
### Requirement: Each probe is run over SSH as the operator and its outcome captured

The system SHALL run each probe's command over SSH as the non-root operator and capture its exit code,
stdout, and stderr, marking a probe ok exactly when it exits zero (interpretation of a non-zero exit is
per-probe — an expected fail is still recorded data, not a run error). A probe SHALL escalate via sudo
only when its tag is host-root. Each probe SHALL be bounded by a timeout: a probe MAY declare an optional
per-probe `timeout`, and absent one it SHALL inherit a run-wide default supplied by the caller, so no
single probe can block the battery — and thus the host's teardown — without bound. The battery JSON
SHALL remain back-compatible: a probe without a `timeout` is valid and uses the run-wide default. A probe
that exceeds its timeout SHALL be recorded as a timed-out result (marked distinctly from a non-zero exit)
and SHALL NOT abort the battery — the remaining probes still run, exactly as for a non-zero exit. To bound
the cost of a wedged host, after a configurable number of **consecutive** timed-out probes (default 2) the
system SHALL stop running the rest of that host's battery and record why, rather than spending a full
timeout on every remaining probe.

#### Scenario: Probe outcome is captured

- **WHEN** a probe runs on a ready host
- **THEN** its exit code, stdout, and stderr are recorded, with ok set iff the exit code is zero

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
in-flight host is absent. The document shape is unchanged; it is simply rewritten with the
completed-so-far set each time a host finishes.

#### Scenario: The results timestamp is caller-supplied

- **WHEN** results are written
- **THEN** the filename and content use the injected timestamp, with no wall-clock read in the library

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

The system SHALL lint a loaded battery for the **vacuous-ok** footgun and surface a **non-fatal** warning:
a probe command that is **not exit-gated** yet prints success/failure tokens — so its `ok`, which reflects
only the exit code, would be vacuous. The lint itself SHALL NOT raise, reorder probes, or alter the
exit-code `ok` semantics — it only warns. (The former "order-surprise" footgun no longer exists: execution
order is now authoring order — see "Probes run in authoring order" — so there is no hidden reordering to
warn about.)

#### Scenario: A vacuously-ok probe warns

- **WHEN** a probe's command prints success/failure tokens but is not gated with an explicit exit
- **THEN** a non-fatal warning naming the probe is surfaced (its `ok` reflects only the exit code), and
  the run still proceeds

#### Scenario: A clean battery warns nothing

- **WHEN** no probe has an un-gated token-printing tail
- **THEN** no lint warning is surfaced

### Requirement: A battery is declarative data loaded from a TOML manifest

The system SHALL load a named probe battery from a **TOML manifest**, where each probe is a declarative
record carrying a stable id, title, tag, optional classification, and optional timeout, plus its command
expressed as **exactly one of** an inline `run` block (a literal shell string) or a `script` reference to a
co-located shell file. The system SHALL raise a clear error for a malformed battery — invalid TOML, a
missing required field, an unknown tag, an **unrecognized key** at the root or on a probe (a typo like
`timout` SHALL fail loud, naming the key, rather than silently falling back to a default), a probe
declaring **neither** `run` nor `script`, or a probe declaring **both**. The manifest is parsed with the
standard library (no third-party dependency); the results document this feeds remains JSON.

#### Scenario: A well-formed battery loads

- **WHEN** a valid battery TOML manifest is loaded
- **THEN** a named battery of probes is returned, each probe carrying its resolved command

#### Scenario: A malformed battery is rejected

- **WHEN** a battery manifest is invalid TOML, missing a required field, or names an unknown tag
- **THEN** the system raises a battery-load error

#### Scenario: A probe must declare exactly one command form

- **WHEN** a probe declares neither `run` nor `script`, or declares both
- **THEN** the system raises a battery-load error naming the offending probe

#### Scenario: An unrecognized key is rejected

- **WHEN** a manifest carries a key the schema does not define (e.g. a `timout` typo for `timeout`)
- **THEN** the system raises a battery-load error naming the unrecognized key

### Requirement: Probes run in authoring order

The system SHALL execute a battery's probes in the order they appear in the manifest — the manifest **is**
the execution order, regardless of tag. A probe's `tag` records what the probe touches (and governs the
sudo-escalation contract) but SHALL NOT reorder execution. Results SHALL be recorded in that same
authoring order. This lets a probe's prerequisites be expressed directly — author the prerequisite probe
earlier — and makes the results sequence match the manifest, so a result is interpreted in the order it
was written.

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
probe's resolved shell text, in bash mode, labelling each finding with the probe's provenance (the script
file path — whose reported `line:col` aligns with the file, since the resolved text is the file's
content — or the probe's id for an inline block) — in addition to the existing non-fatal vacuous-ok
authoring warning. The command SHALL report the
findings and SHALL **exit non-zero when any finding is at or above a configurable severity threshold
(default `error`)**, so it is usable as a CI gate; the threshold SHALL be selectable (`error`, `warning`,
or `note`) so a stricter gate can be opted into. Probe commands are authored as **bash** — the dialect
lint checks; guaranteeing that dialect at the execution transport is a separate follow-up change. When
`shellcheck` is not installed, the command SHALL surface a notice and skip the shellcheck pass (still
running the advisory check) rather than crashing — unless the caller passes a strict flag requiring
shellcheck's presence, in which case its absence SHALL be a non-zero exit, so a gate cannot read green
merely because the linter is missing.

#### Scenario: A clean battery passes the gate

- **WHEN** `lint` runs over a battery whose probes have no shellcheck findings at or above the threshold
- **THEN** the findings (if any, below threshold) are reported and the command exits zero

#### Scenario: A battery with a threshold finding fails the gate

- **WHEN** `lint` runs over a battery with a shellcheck finding at or above the active severity threshold
- **THEN** the finding is reported and the command exits non-zero

#### Scenario: The severity threshold is selectable

- **WHEN** `lint` is run with a stricter threshold than the default `error`
- **THEN** findings at or above that threshold (e.g. warnings) cause a non-zero exit

#### Scenario: A missing shellcheck is skipped, not fatal

- **WHEN** `shellcheck` is not installed
- **THEN** `lint` surfaces a notice, skips the shellcheck pass, still runs the advisory check, and does
  not crash

#### Scenario: A gate can refuse to pass without shellcheck

- **WHEN** `lint` is run with the require-shellcheck flag and `shellcheck` is not installed
- **THEN** the command exits non-zero instead of skipping

