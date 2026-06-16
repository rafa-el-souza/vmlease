# vm-provisioning Specification

## Purpose
The provision → (transform) → run-workload → teardown spine (each host torn down by default, or left live when `--keep` selects it): build a matrix into labelled host specs, render a `plan` dry-run that makes zero provider calls, and execute each host in isolation (one host's failure never discards another's results) with optional parallelism, over a `Provider` abstraction.
## Requirements
### Requirement: Matrix builds into labelled host specs deterministically

The system SHALL turn a run request (a matrix of distro keys × one server type, plus a caller-supplied
run token) into one labelled host spec per distro, deriving the run-id purely from the run token so the
same token yields the same specs and labels. Each host spec SHALL also carry the run's **required
capabilities** (the battery's `requires`, lifted onto the spec by the caller that builds the matrix), so
that capability recipes can be rendered into the host's cloud-init at create time — before the workload
exists. `requires` is a provisioning attribute of the host spec (alongside `distro_key`), not workload
data; the runner reads it from the spec, never from the opaque workload.

#### Scenario: One spec per distro, carrying the run label

- **WHEN** a matrix of N distro keys is built with run token `T`
- **THEN** N host specs are produced, each named for the run-id derived from `T` and its distro key, and
  each carries the `vmlease=<run-id>` label

#### Scenario: Build is pure and deterministic

- **WHEN** the same matrix (same token) is built twice
- **THEN** the resulting host specs (names, images, labels) are identical, with no provider calls made

#### Scenario: Host specs carry the run's required capabilities

- **WHEN** a matrix is built from a battery declaring `requires = ["docker"]`
- **THEN** each host spec carries that required-capability set, so the runner renders the docker recipe into cloud-init at create time

### Requirement: The plan dry-run makes zero provider calls

The system SHALL render a `plan` that shows exactly what a real run would provision — one plan item per
host, including image, server type, distro key, the host's **required capabilities**, and a summary of the
**injected workload** — while making **no** provider calls and running the cost guard so a guard refusal
surfaces before any spend. Because required capabilities change the provisioned image (and therefore the
cache key), the plan SHALL surface them so the dry-run reflects what the run will actually build.

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

#### Scenario: Plan surfaces required capabilities

- **WHEN** `plan` is invoked for a battery requiring docker
- **THEN** each plan item shows that docker is required, so the operator sees the docker variant will be provisioned

### Requirement: Per-host isolation never loses another host's results

The system SHALL create, transform, run the workload on, and tear down each host inside its own
try/finally before the next host starts, so that a failure to provision, transform, or reach one host is
recorded as an error result for that host (not a propagating exception) and every other host still
produces a result. Teardown SHALL run in a `finally` that fires even when a `BaseException` (for example
a `KeyboardInterrupt` from operator Ctrl-C, or a `SystemExit`) propagates through the host's workload — a
caught `Exception` is not the only path that must still tear the host down — UNLESS the host is marked to
be kept (see "Keeping selected hosts live for debugging"), in which case the `finally` deliberately leaves
the host standing and records its reattach coordinates instead of destroying it. Each host's result SHALL
be surfaced to the caller **as that host completes** (not only in the aggregate returned at the end), so a
caller can persist results incrementally; the aggregate return is preserved in matrix order.

#### Scenario: One host's provisioning failure does not abort the run

- **WHEN** one host in a multi-host matrix fails to provision or become reachable
- **THEN** that host yields a result whose detail records the error, the remaining hosts run normally, and
  the run returns exactly one result per requested host

#### Scenario: Teardown always runs and never loses results

- **WHEN** a host's workload has completed (or errored) and the host is not marked to be kept
- **THEN** the host is destroyed in a finally block; if the destroy itself fails, the failure is appended
  to the result as a reap-it warning rather than discarding the collected results

#### Scenario: Teardown runs even when the workload raises a BaseException

- **WHEN** a host's workload raises a `BaseException` (such as `KeyboardInterrupt` or `SystemExit`) after
  the host has been created and the host is not marked to be kept
- **THEN** the host is still destroyed by the `finally` before the exception propagates, so an aborted run
  leaves no billable host behind that path

#### Scenario: A kept host is left live even when the workload raises a BaseException

- **WHEN** a host marked to be kept has its workload raise a `BaseException` (for example operator Ctrl-C)
  after the host has been created
- **THEN** the `finally` leaves the host live rather than destroying it, and records the kept host's
  reattach coordinates, so the operator can SSH into it after the abort

#### Scenario: Each host's result is surfaced as it completes

- **WHEN** a host finishes (its workload completed or errored and it has been torn down or kept)
- **THEN** its result is handed to the caller's completion sink at that point, before later hosts finish,
  while the final aggregate is still returned in matrix order

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
JSON output must be parsed from their plain-text output. A `destroy` SHALL bound its own provider
subprocess with a wall-clock timeout so a wedged provider CLI cannot itself stall teardown indefinitely;
on expiry the subprocess is killed and the destroy is treated as a failed (reap-able) teardown rather than
hanging the run.

#### Scenario: SSH survives a recycled IP

- **WHEN** the provider assigns a previously-used IP to a new host
- **THEN** the SSH connection does not fail on a stale host-key mismatch (no persistent known-hosts entry
  is used)

#### Scenario: Plain-text provider output is parsed

- **WHEN** a provider create / enable-rescue command returns plain text (no JSON mode)
- **THEN** the system parses the host details from that text rather than requiring JSON

#### Scenario: A wedged delete subprocess does not hang teardown

- **WHEN** the provider's delete subprocess does not return within its timeout
- **THEN** the subprocess is killed and the destroy surfaces as a failed teardown (reap-able), rather than
  blocking the host's `finally` forever

### Requirement: The provider supports snapshot image operations
The provider seam SHALL support creating an image (snapshot) from a host, listing images by label,
deleting an image, and powering a host off — each operating on a provider-agnostic `Image` (id, labels,
creation timestamp, disk size, architecture). Image deletion SHALL be idempotent (a not-found delete is
success) and power-off SHALL be idempotent (an already-off host is success). A provider's resource-limit
error SHALL be translated, inside the provider implementation, into a typed `ProviderQuotaError` (a
`ProviderError` subclass); no provider-specific error string or number SHALL escape the provider seam.

#### Scenario: Snapshot operations work on agnostic Image objects
- **WHEN** the runner creates, lists, or deletes a cache image
- **THEN** it does so through the Provider protocol, receiving `Image` objects rather than provider CLI output

#### Scenario: A provider snapshot-limit error is typed at the seam
- **WHEN** the provider reports its snapshot-count limit during image creation
- **THEN** the provider implementation raises a typed `ProviderQuotaError`, not a raw provider string

### Requirement: Provisioning restores from a matching cached image
When provisioning a host, the runner SHALL look up whether a cached image matches the host's content key,
architecture, and disk bound (the snapshot's disk size ≤ the target server's disk), and on a match create
the server from that image (restore, skipping
rescue-write and package install); on no match it SHALL provision via the normal cold path. This lookup
SHALL be performed at provision time, never during `plan`. A run SHALL **consume** cache images but SHALL
**NOT create** them — cache images are produced only by `build-image`.

#### Scenario: A matching image is restored
- **WHEN** provisioning a host whose content key matches an existing cached image of the right architecture and disk bound
- **THEN** the server is created from that image rather than from the default image

#### Scenario: plan still makes zero provider calls
- **WHEN** `plan` is run while caching is available
- **THEN** it makes zero provider calls and shows the cold (pre-cache) provisioning path

#### Scenario: A run never builds a cache image
- **WHEN** a run encounters a cache miss
- **THEN** it provisions via the cold path and does not create a cache image

### Requirement: Keeping selected hosts live for debugging

The run command SHALL accept a keep selection that leaves provisioned hosts RUNNING (billable) instead of
tearing them down, so an operator can SSH in and iterate against a live host. The selection SHALL be
per-host: a bare/empty selection keeps every host; a named-distro selection keeps only hosts of those
distros and tears down the rest. A named distro that is not part of the run's distro set SHALL be rejected
before any host is provisioned, so a typo costs nothing. Each kept host SHALL carry a keep marker label
that distinguishes it from torn-down hosts, and SHALL record a structured reattach record — host name, id,
IPv4, distro, operator, and the private-key path — so the live host is discoverable from the results
without parsing prose. When any host is kept, the run's throwaway keypair SHALL survive the run so the
recorded SSH key path points at a real file.

#### Scenario: Bare keep leaves all hosts live

- **WHEN** a run requests keep with no distro selection
- **THEN** no host is torn down, each carries the keep marker label, and each records its structured
  reattach coordinates

#### Scenario: Per-distro keep leaves only the named hosts live

- **WHEN** a run over multiple distros requests keep of a distro subset
- **THEN** only the named distros' hosts are left live (each with the keep marker label and a reattach
  record) and every other host is torn down normally

#### Scenario: An unknown keep distro is rejected before spend

- **WHEN** the keep selection names a distro not in the run's distro set
- **THEN** the command reports the error and provisions nothing

