# probe-workload Specification

## Purpose
The probe battery workload: declarative `Probe`/`Battery` data loaded from JSON, tag-ordered execution (read-only → operator-space → host-root) over SSH, a self-describing host-detail snapshot, and timestamped JSON results (timestamp injected, not clock-read).
## Requirements
### Requirement: A battery is declarative data loaded from JSON

The system SHALL load a named probe battery from a JSON file, where each probe is a declarative record
(stable id, title, shell command, tag, and optional classification), and SHALL raise a clear error for a
malformed battery file.

#### Scenario: A well-formed battery loads

- **WHEN** a valid battery JSON file is loaded
- **THEN** a named battery of probes is returned

#### Scenario: A malformed battery is rejected

- **WHEN** a malformed battery file is loaded
- **THEN** the system raises a battery-load error

### Requirement: Probes run in tag order

The system SHALL execute a battery's probes in tag order — read-only, then operator-space-mutating, then
host-root-mutating — preserving authoring order within each tag group so declared dependencies inside a
group are respected.

#### Scenario: Read-only probes run before mutating ones

- **WHEN** a battery mixing tags is ordered for execution
- **THEN** read-only probes run first, then operator-space, then host-root, stable within each group

### Requirement: Each probe is run over SSH as the operator and its outcome captured

The system SHALL run each probe's command over SSH as the non-root operator and capture its exit code,
stdout, and stderr, marking a probe ok exactly when it exits zero (interpretation of a non-zero exit is
per-probe — an expected fail is still recorded data, not a run error). A probe SHALL escalate via sudo
only when its tag is host-root.

#### Scenario: Probe outcome is captured

- **WHEN** a probe runs on a ready host
- **THEN** its exit code, stdout, and stderr are recorded, with ok set iff the exit code is zero

#### Scenario: A non-zero probe does not abort the battery

- **WHEN** a probe exits non-zero
- **THEN** its result is recorded and the remaining probes still run

### Requirement: A host-detail snapshot heads each host's results

The system SHALL capture a self-describing host-detail snapshot (os-release, kernel, init, cgroup, id,
tool inventory) before the battery, as the results header.

#### Scenario: Results carry a host-detail header

- **WHEN** a host is probed
- **THEN** its result includes the host-detail snapshot alongside the per-probe results

### Requirement: Results are written as timestamped JSON with an injected timestamp

The system SHALL write each run's results to a timestamped JSON file whose timestamp is supplied by the
caller (not read from the clock), keeping the output deterministic and testable.

#### Scenario: The results timestamp is caller-supplied

- **WHEN** results are written
- **THEN** the filename and content use the injected timestamp, with no wall-clock read in the library

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

