# results-summary Specification

## Purpose
TBD - created by archiving change results-summarize. Update Purpose after archive.
## Requirements
### Requirement: Summarize subcommand reads a raw results file and writes a versioned companion

The CLI SHALL provide a `summarize` subcommand that reads a raw vmlease results JSON file and writes a
summary JSON file, without mutating the raw file. The summary SHALL carry a `schema_version` field
identifying the contract version.

#### Scenario: Companion written beside the raw file by default
- **WHEN** `vmlease summarize vmlease-run-ts.json` is invoked with no `--out`
- **THEN** a `vmlease-run-ts.summary.json` file is written in the same directory
- **AND** the original raw file is byte-for-byte unchanged
- **AND** the summary's top-level `schema_version` is `"1"`

#### Scenario: Explicit output path honored
- **WHEN** `vmlease summarize <raw> --out /tmp/s.json` is invoked
- **THEN** the summary is written to `/tmp/s.json`

#### Scenario: Malformed raw input fails loudly
- **WHEN** the raw file is missing or is not valid vmlease results JSON
- **THEN** the command prints an error to stderr and exits non-zero without writing a summary

### Requirement: Per-probe verdict is computed deterministically from the token convention

For each probe, the summarizer SHALL compute a single `verdict` from the probe's `exit_code`, `timed_out`
flag, and the assertion tokens harvested from its stdout. Token harvesting SHALL be generic: substrings
matching `[A-Z][A-Z0-9_]*_(OK|FAIL|info|review)` are bucketed by suffix into `ok_tokens`, `fail_tokens`,
`info_tokens`, `review_tokens`. The verdict precedence SHALL be: `timed_out` true → `TIMEOUT`; else any
`fail_tokens` OR `exit_code != 0` → `FAIL`; else `exit_code == 0` with at least one `ok_token` → `PASS`;
else (`exit_code == 0`, no assertion tokens) → `PASS_NO_ASSERTIONS`.

#### Scenario: Failing token forces FAIL even on a zero exit
- **WHEN** a probe has `exit_code` 0 but its stdout contains `START_CORE_NOT_RUNNING_FAIL`
- **THEN** that probe's `verdict` is `FAIL`
- **AND** `fail_tokens` contains `START_CORE_NOT_RUNNING_FAIL`

#### Scenario: Passing probe
- **WHEN** a probe has `exit_code` 0, stdout contains `SETUP_EXIT0_OK`, and no `*_FAIL` token
- **THEN** its `verdict` is `PASS`

#### Scenario: Timeout dominates
- **WHEN** a probe has `timed_out` true
- **THEN** its `verdict` is `TIMEOUT` regardless of tokens or exit code

#### Scenario: Zero exit with no assertion tokens
- **WHEN** a probe has `exit_code` 0 and stdout contains no `*_OK`/`*_FAIL` token
- **THEN** its `verdict` is `PASS_NO_ASSERTIONS`

### Requirement: Summary carries per-host probe records, a matrix pivot, and totals

The summary SHALL contain, for every host in the raw file, its `distro`, `image`, `detail`, and a list of
probe records each with `id`, `command`, `tag`, `exit_code`, `ok`, `timed_out`, `verdict`, the four token
buckets, and bounded `stdout_tail`/`stderr_tail`. The summary SHALL include a `matrix` mapping each
canonical command to a per-distro verdict, and a `totals` count keyed by verdict.

#### Scenario: Matrix pivots command against distro
- **WHEN** a run covered `ubuntu` and `fedora` and the `start` probe passed on fedora but failed on ubuntu
- **THEN** `matrix["sandbox start"]` is `{"ubuntu": "FAIL", "fedora": "PASS"}`

#### Scenario: Multiple probes for one command collapse to the worst verdict
- **WHEN** two probes (`status-stopped`, `status-running`) both map to `sandbox status` and one is `FAIL`
- **THEN** that command's matrix cell for the host is `FAIL`

### Requirement: Exit code reflects the overall verdict

The `summarize` command SHALL exit zero only when no probe verdict is `FAIL` or `TIMEOUT`, and non-zero
otherwise, so a caller can gate on the exit code without parsing the summary.

#### Scenario: Non-zero exit when any probe failed
- **WHEN** at least one probe in the raw file resolves to `FAIL` or `TIMEOUT`
- **THEN** `summarize` writes the summary AND exits non-zero

#### Scenario: Zero exit when all probes passed
- **WHEN** every probe resolves to `PASS` or `PASS_NO_ASSERTIONS`
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

