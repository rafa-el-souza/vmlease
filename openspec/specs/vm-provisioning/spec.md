# vm-provisioning Specification

## Purpose
The provision → (transform) → run-workload → always-teardown spine: build a matrix into labelled host specs, render a `plan` dry-run that makes zero provider calls, and execute each host in isolation (one host's failure never discards another's results) with optional parallelism, over a `Provider` abstraction.
## Requirements
### Requirement: Matrix builds into labelled host specs deterministically

The system SHALL turn a run request (a matrix of distro keys × one server type, plus a caller-supplied
run token) into one labelled host spec per distro, deriving the run-id purely from the run token so the
same token yields the same specs and labels.

#### Scenario: One spec per distro, carrying the run label

- **WHEN** a matrix of N distro keys is built with run token `T`
- **THEN** N host specs are produced, each named for the run-id derived from `T` and its distro key, and
  each carries the `vmlease=<run-id>` label

#### Scenario: Build is pure and deterministic

- **WHEN** the same matrix (same token) is built twice
- **THEN** the resulting host specs (names, images, labels) are identical, with no provider calls made

### Requirement: The plan dry-run makes zero provider calls

The system SHALL render a `plan` that shows exactly what a real run would provision — one plan item per
host, including image, server type, distro key, and a summary of the **injected workload** — while
making **no** provider calls and running the cost guard so a guard refusal surfaces before any spend.

#### Scenario: Plan provisions nothing

- **WHEN** `plan` is invoked on a matrix
- **THEN** one plan item per host is returned and no VM is created

#### Scenario: Plan surfaces a cost-guard refusal before spend

- **WHEN** `plan` is invoked on a matrix that violates the cost guard
- **THEN** the guard refusal is raised during planning, before any provider call

#### Scenario: Plan describes the injected workload, not a hardcoded probe count

- **WHEN** `plan` is invoked with an injected workload
- **THEN** each plan item includes that workload's summary (for the probe workload, its probe count)
  rather than assuming a probe battery

### Requirement: Per-host isolation never loses another host's results

The system SHALL create, transform, run the workload on, and tear down each host inside its own
try/finally before the next host starts, so that a failure to provision, transform, or reach one host is
recorded as an error result for that host (not a propagating exception) and every other host still
produces a result.

#### Scenario: One host's provisioning failure does not abort the run

- **WHEN** one host in a multi-host matrix fails to provision or become reachable
- **THEN** that host yields a result whose detail records the error, the remaining hosts run normally, and
  the run returns exactly one result per requested host

#### Scenario: Teardown always runs and never loses results

- **WHEN** a host's workload has completed (or errored)
- **THEN** the host is destroyed in a finally block; if the destroy itself fails, the failure is appended
  to the result as a reap-it warning rather than discarding the collected results

### Requirement: Optional parallelism preserves matrix order

The system SHALL support running up to N hosts concurrently at the same cost as serial, returning results
in matrix order regardless of completion order, with each host fully self-contained (its own create / run
/ teardown) so concurrency is safe and the teardown-always guarantee holds per host.

#### Scenario: Concurrent run returns ordered results

- **WHEN** a matrix is executed with a parallelism greater than one
- **THEN** results are returned in the original matrix order

### Requirement: Provider operations tolerate provider quirks

The system SHALL drive VMs through a `Provider` abstraction (create-with-cloud-init, destroy,
list-by-label) and SHALL remain correct against real provider behavior: connections must survive a
provider recycling a just-freed IP address onto the next host, and provider commands that do not support
JSON output must be parsed from their plain-text output.

#### Scenario: SSH survives a recycled IP

- **WHEN** the provider assigns a previously-used IP to a new host
- **THEN** the SSH connection does not fail on a stale host-key mismatch (no persistent known-hosts entry
  is used)

#### Scenario: Plain-text provider output is parsed

- **WHEN** a provider create / enable-rescue command returns plain text (no JSON mode)
- **THEN** the system parses the host details from that text rather than requiring JSON

