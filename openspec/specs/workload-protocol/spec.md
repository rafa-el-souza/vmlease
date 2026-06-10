# workload-protocol Specification

## Purpose
The injected-workload seam: the runner runs a caller-supplied `Workload` (a unit of on-host work over a ready host + an SSH connection, returning a `HostRun`) over the provision → run → guaranteed-teardown spine. The runner owns the lifecycle (readiness gate, upload staging, teardown); the workload owns only what runs on a ready host. The probe battery (`ProbeWorkload`) is the reference implementation — preserving the pre-seam probe path's per-probe capture contract — and the seam admits other workloads (e.g. a CI gate job) with no change to provisioning or safety.
## Requirements
### Requirement: The runner executes a caller-injected workload

The system SHALL run a caller-injected workload over each provisioned host rather than a hardcoded probe
battery, so the provision → run → guaranteed-teardown spine can run any unit of on-host work. A workload
SHALL receive the host's provisioning spec, the live host, and an SSH connection, and SHALL return that
host's run result.

#### Scenario: An injected workload runs once per host

- **WHEN** a run is executed with an injected workload across a multi-host matrix
- **THEN** the workload is invoked exactly once per host with that host's spec, the live host, and an
  SSH connection, and each returned result is collected in matrix order

#### Scenario: A different workload requires no change to the spine

- **WHEN** a different workload implementation is injected for the same matrix
- **THEN** it runs over the same provision → run → teardown spine with no change to provisioning, the
  cost guard, upload staging, or teardown

### Requirement: The runner owns readiness; the workload owns on-host work

The system SHALL gate host readiness before invoking a workload and SHALL pass the workload a ready host
plus an SSH connection. A workload SHALL be responsible only for the work performed on a ready host — it
does not provision, gate readiness, stage uploads, or tear the host down.

#### Scenario: A workload runs only after the readiness gate

- **WHEN** a host is provisioned for a workload
- **THEN** the runner waits for the host's readiness gate, then stages any uploads, then invokes the
  workload — the workload is never called against a not-yet-ready host

#### Scenario: A workload error is isolated to its host and still torn down

- **WHEN** a workload raises or errors on one host
- **THEN** that host's result records the error, the host is still torn down, and every other host's
  workload still runs

### Requirement: The probe battery is the reference workload implementation

The system SHALL provide the probe battery as a workload implementation (`ProbeWorkload`) whose on-host
behavior — a host-detail snapshot followed by the battery, run in authoring order — preserves the probe
path's contract: results are captured per probe exactly as before the seam existed. The runner SHALL NOT
name `ProbeWorkload` itself; the caller (the CLI) constructs it from a loaded battery and injects it.

#### Scenario: The probe path runs as an injected workload

- **WHEN** the CLI runs a probe battery
- **THEN** it constructs `ProbeWorkload` from the loaded battery, injects it into the runner, and the
  produced host-detail snapshot and per-probe results are identical to those produced before the seam
  existed

### Requirement: Readiness asserts a usable kernel module tree

The system's readiness gate SHALL treat a host as ready only if its running kernel has a usable module tree — a present `/lib/modules/$(uname -r)/modules.dep` — in addition to the existing readiness sentinel. When the running kernel has no usable module tree, the readiness gate SHALL fail fast, before any workload runs, with an error that names the running kernel version and identifies the rescue/modules skew. This assertion is generic across distros and is evaluated on the host's final running kernel (after any provisioning reboot).

#### Scenario: Missing module tree fails readiness fast

- **WHEN** the readiness gate runs against a host whose running kernel has no `/lib/modules/$(uname -r)/modules.dep`
- **THEN** readiness fails with an error naming the running kernel version and the rescue/modules skew, and no workload is invoked on that host

#### Scenario: Healthy host passes readiness

- **WHEN** the readiness gate runs against a host whose readiness sentinel is present and whose running kernel has a populated `/lib/modules/$(uname -r)/` module tree
- **THEN** the host is treated as ready and the workload proceeds

