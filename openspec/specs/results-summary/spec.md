# results-summary Specification

## Purpose
TBD - created by archiving change results-summarize. Update Purpose after archive.
## Requirements
### Requirement: Summarize subcommand reads a raw results file and writes a versioned companion

The CLI SHALL provide a `summarize` subcommand that reads a raw vmlease results JSON file and writes a
summary JSON file, without mutating the raw file. The summary SHALL carry a `schema_version` field
identifying the contract version. Adding the `prep_phase` section (below) bumps the contract: the summary's
top-level `schema_version` SHALL be `"3"`.

#### Scenario: Companion written beside the raw file by default
- **WHEN** `vmlease summarize vmlease-run-ts.json` is invoked with no `--out`
- **THEN** a `vmlease-run-ts.summary.json` file is written in the same directory
- **AND** the original raw file is byte-for-byte unchanged
- **AND** the summary's top-level `schema_version` is `"3"`

#### Scenario: Explicit output path honored
- **WHEN** `vmlease summarize <raw> --out /tmp/s.json` is invoked
- **THEN** the summary is written to `/tmp/s.json`

#### Scenario: Malformed raw input fails loudly
- **WHEN** the raw file is missing or is not valid vmlease results JSON
- **THEN** the command prints an error to stderr and exits non-zero without writing a summary

### Requirement: Per-probe verdict is computed deterministically from the token convention

For each probe, the summarizer SHALL compute a single `verdict` from the probe's `exit_code`, `timed_out`
flag, recorded `ok`, a serialized **`has_assertions`** flag (whether the probe declared one or more
assertions), and the assertion tokens harvested from its stdout. The summarizer reads these from the raw
results file — it has neither the probe's assertion objects nor a regex engine, so it relies on the
producer's recorded `ok` and `has_assertions` and SHALL NOT re-evaluate any assertion. The raw per-probe
record therefore carries `has_assertions` (and the failed-assertion descriptions) **in place of** the
former `success_when` field. Token harvesting SHALL be generic: substrings matching
`[A-Z][A-Z0-9_]*_(OK|FAIL|info|review)` are bucketed by suffix into `ok_tokens`, `fail_tokens`,
`info_tokens`, `review_tokens`. The verdict precedence SHALL be: `timed_out` true → `TIMEOUT`; else,
**when `has_assertions` is set, the assertions are authoritative — `verdict` is `PASS` when the probe is
`ok` (every assertion held) and `FAIL` otherwise, overriding the exit-code and fail-token rules** (the
summarizer honors the recorded `ok` — the single source of the pass/fail predicate); else (`has_assertions`
unset) any `fail_tokens` OR `exit_code != 0` → `FAIL`; else `exit_code == 0` with at least one `ok_token` →
`PASS`; else (`exit_code == 0`, no assertion tokens) → `PASS_NO_ASSERTIONS`. A probe without assertions
SHALL receive exactly the verdict it received before the assertion branch existed — the printed-token
convention is unchanged and coexists with assertions. A raw file predating this schema (no `has_assertions`
field) SHALL be read as `has_assertions` unset for every probe — the summarizer targets the current results
format and does not special-case retired fields.

#### Scenario: Failing token forces FAIL even on a zero exit

- **WHEN** a probe has `exit_code` 0, no declared assertions, but its stdout contains `START_CORE_NOT_RUNNING_FAIL`
- **THEN** that probe's `verdict` is `FAIL`
- **AND** `fail_tokens` contains `START_CORE_NOT_RUNNING_FAIL`

#### Scenario: Passing probe

- **WHEN** a probe has `exit_code` 0, no declared assertions, stdout contains `SETUP_EXIT0_OK`, and no `*_FAIL` token
- **THEN** its `verdict` is `PASS`

#### Scenario: Timeout dominates

- **WHEN** a probe has `timed_out` true
- **THEN** its `verdict` is `TIMEOUT` regardless of tokens, exit code, or declared assertions

#### Scenario: Zero exit with no assertion tokens

- **WHEN** a probe has `exit_code` 0, no declared assertions, and stdout contains no `*_OK`/`*_FAIL` token
- **THEN** its `verdict` is `PASS_NO_ASSERTIONS`

#### Scenario: A probe whose assertions hold is PASS despite a non-zero exit

- **WHEN** a probe declares one or more assertions, is recorded `ok` true (every assertion held), and has a non-zero `exit_code`
- **THEN** its `verdict` is `PASS` — the declared assertions override the exit-code rule

#### Scenario: A probe with a failed assertion is FAIL

- **WHEN** a probe declares one or more assertions and is recorded `ok` false (at least one assertion failed)
- **THEN** its `verdict` is `FAIL` — the assertion branch is authoritative, overriding both any `ok_token`
  and any `fail_token` present in its stdout (the token convention is not consulted when assertions decide)

#### Scenario: An assertion-PASS overrides a stray fail token

- **WHEN** a probe declares assertions, is recorded `ok` true, and its stdout nonetheless contains a
  `*_FAIL` token (a leftover diagnostic line)
- **THEN** its `verdict` is `PASS` — when `has_assertions` is set, the recorded `ok` decides and the
  `fail_tokens` rule does not apply

#### Scenario: A pre-schema raw file is read as having no assertions

- **WHEN** a raw results file predates this schema and carries no `has_assertions` field on its probes
- **THEN** every probe is treated as `has_assertions` unset and resolves via the token/exit precedence,
  without error

### Requirement: Summary carries per-host probe records, a matrix pivot, and totals

The summary SHALL contain, for every host in the raw file, its `distro`, `image`, `detail`, and a list of
probe records each with `id`, `command`, `tag`, `exit_code`, `ok`, `timed_out`, `verdict`, the four token
buckets, the **failed-assertion descriptions** (`assertion_failures`, empty when the probe declared no
assertions or all held), and bounded `stdout_tail`/`stderr_tail`. For every host that ran a prep phase the
summary SHALL also carry its `prep_phase` records (see the prep-phase requirement). The summary SHALL
include a `matrix` mapping each canonical command to a per-distro verdict, and a `totals` count keyed by
verdict — the `totals` key-set SHALL include the prep-phase states (the hard-abort and soft-fail states)
alongside the probe verdicts, so a reader sees prep outcomes in the same tally.

#### Scenario: A failed assertion is named in the probe record

- **WHEN** a probe is recorded `ok` false because its `stdout_has "READY"` assertion did not hold
- **THEN** that probe's summary record lists the failed assertion (e.g. `stdout_has "READY"`) in
  `assertion_failures`

#### Scenario: Matrix pivots command against distro

- **WHEN** a run covered `ubuntu` and `fedora` and the `start` probe passed on fedora but failed on ubuntu
- **THEN** `matrix["sandbox start"]` is `{"ubuntu": "FAIL", "fedora": "PASS"}`

#### Scenario: Multiple probes for one command collapse to the worst verdict

- **WHEN** two probes (`status-stopped`, `status-running`) both map to `sandbox status` and one is `FAIL`
- **THEN** that command's matrix cell for the host is `FAIL`

#### Scenario: totals includes prep-phase states

- **WHEN** a run had a soft prep failure on one host and a hard prep abort on another
- **THEN** `totals` carries distinct counts for the soft-fail and hard-abort states alongside the probe-verdict counts

### Requirement: Exit code reflects the overall verdict

The `summarize` command SHALL exit zero only when no probe verdict is `FAIL` or `TIMEOUT` **and no host
aborted due to a hard prep-phase failure**, and non-zero otherwise, so a caller can gate on the exit code
without parsing the summary. A **hard** prep-phase failure leaves a host with no probe records; that host
SHALL still force a non-zero exit (it is not a silently-green zero-probe host). A **soft** prep-phase
failure (a `required = false` step) SHALL NOT by itself force a non-zero exit — it is surfaced as a distinct
verdict the caller MAY choose to gate on.

#### Scenario: Non-zero exit when any probe failed
- **WHEN** at least one probe in the raw file resolves to `FAIL` or `TIMEOUT`
- **THEN** `summarize` writes the summary AND exits non-zero

#### Scenario: Non-zero exit when a host aborted in a hard prep failure
- **WHEN** a host ran no probes because a hard prep-phase step (or a package install) failed
- **THEN** `summarize` writes the summary AND exits non-zero

#### Scenario: A soft prep failure alone does not force a non-zero exit
- **WHEN** every probe passed and the only prep issue was a `required = false` step that failed
- **THEN** `summarize` exits zero, and the soft prep failure is surfaced in the summary

#### Scenario: Zero exit when all probes passed
- **WHEN** every probe resolves to `PASS` or `PASS_NO_ASSERTIONS` and no host aborted in prep
- **THEN** `summarize` exits zero

### Requirement: Optional battery supplies command mapping and missing-probe detection

When `--battery <file>` is supplied, the summarizer SHALL use it to map each probe `id` to its canonical
command label, and SHALL record any probe declared in the battery but absent from the results (e.g. dropped
by the consecutive-timeout breaker). Without `--battery`, the summarizer SHALL fall back to a built-in
probe-id→command mapping and omit missing-probe detection.

#### Scenario: Declared-but-not-run probe is surfaced
- **WHEN** the battery declares probe `destroy` but the results contain no `destroy` probe for a host
- **THEN** the summary marks `destroy` as not-run for that host

#### Scenario: No battery falls back to built-in mapping
- **WHEN** `summarize` is run without `--battery`
- **THEN** known probe ids still resolve to their canonical command labels

### Requirement: Prep-phase results are surfaced in the summary

The summary SHALL include a `prep_phase` section mapping each executed prep step `id` to its outcome (exit
status, `required` flag, bounded stderr), and SHALL represent prep outcomes as distinct states in `totals`:
a **hard** prep abort SHALL be counted as **`PREP_HARD_FAIL`** and a **soft** prep failure as
**`PREP_SOFT_FAIL`** — each a distinct non-`PASS` state, never collapsed into a probe verdict and never
silently dropped. The soft-failure state SHALL be surfaced (so a caller may gate on it) without itself
forcing a non-zero exit; the hard-abort state SHALL force a non-zero exit (per the exit-code requirement).
The `prep_phase` section SHALL **always be present** in the summary — an empty object when no host declared
`[prep]` — matching the always-present-keys convention (e.g. `totals`).

#### Scenario: A soft prep failure is surfaced in totals
- **WHEN** a host's prep had a `required = false` step that failed
- **THEN** the summary's `prep_phase` records that step and `totals` carries a distinct soft-prep-failure count

#### Scenario: A hard prep abort is surfaced and counted
- **WHEN** a host aborted because a hard prep step failed
- **THEN** the summary's `prep_phase` records the failing step and `totals` reflects the aborted host as a non-pass state

