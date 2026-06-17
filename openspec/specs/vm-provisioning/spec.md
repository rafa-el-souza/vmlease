# vm-provisioning Specification

## Purpose
The provision → (transform) → run-workload → teardown spine (each host torn down by default, or left live when `--keep` selects it): build a matrix into labelled host specs, render a `plan` dry-run that makes zero provider calls, and execute each host in isolation (one host's failure never discards another's results) with optional parallelism, over a `Provider` abstraction.
## Requirements
### Requirement: Matrix builds into labelled host specs deterministically

The system SHALL turn a run request — an explicit **list of host specs** (the "shopping list"), each carrying
a per-host `name` (its instance identity), an `os = (family, version)` reference into the profile registry,
and the run's **required capabilities** — plus a caller-supplied run token into one labelled host spec per
list entry, deriving the run-id purely from the run token so the same token yields the same specs and labels.
Host **identity is the per-host bare `name`**, NOT the distro, so a run MAY contain N hosts of the same
`(family, version)` provided their names differ. The bare `name` is the **identity** — what `--keep` tokens,
results per-host keys, and the matrix pivot key on; the **provider server name** SHALL be the derived
composite `vmlease-<run-id>-<name>` (carrying the run-id so concurrent/overlapping runs do not collide on the
provider's unique-server-name constraint), set when the labelled spec is built — NOT the bare identity.
Provider resources SHALL be addressed by **label**, never by name. The host spec's image is resolved from `os`
via the registry **during expansion** (so the resolved list is provider-call-free and `plan` can show
images), while `os` is retained for the provision-time prep/rescue lookup (prep is NOT inlined per host); the
cold create path SHALL use the spec's resolved `image`, not a re-resolved profile image, so plan and execute
cannot diverge. `build_host_specs` SHALL **assert bare-`name` uniqueness fail-closed**, raising a clear error
before any provider call rather than letting a duplicate surface only as a provider `create --name` failure.

The CLI `--hosts` axis surface SHALL be a pure **expander** that produces the host list and SHALL itself make
no provider calls. The expander SHALL be split into a **parse** step (string → unresolved entries) and a
**resolve** step (entries → host specs), so that all version-defaulting, naming, and validation logic operates
on the entry model rather than on raw strings (a future file-based host list is then an additive second parse
front-end). It accepts comma-separated entries, each of the grammar `[name=]family[@version]`, fully qualified
per entry (the comma is the entry separator, so there is **no** cross-entry grouping shorthand such as
`ubuntu@22.04,24.04`): an optional `name=` prefix sets the host's explicit name, a bare family resolves to its
default version, and `@version` selects a version. **Multiplicity is expressed by repetition** — repeating an
entry (or naming each occurrence) produces N hosts; there is **no** `*count` multiplier. `--distros` SHALL
remain a **pure alias** for `--hosts` (same destination and full grammar), soft-deprecated: invoking it SHALL
emit a one-time deprecation notice, and when both `--hosts` and `--distros` are supplied the last one SHALL
win.

A user-supplied `name` SHALL be the host's identity verbatim, validated **fail-closed** against the
provider-agnostic host-name charset `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$` (≤63 chars — the universal
hostname-safe subset every cloud provider accepts), and SHALL contain none of the entry delimiters
`, @ = :`. Auto-generated names SHALL be assigned in a **whole-run, two-phase** pass over the resolved
`(family, version)` multiset: phase 1 resolves every entry to `(family, version)` (bare → default version);
phase 2 names them so that a family appearing **once** keeps the bare family name (e.g. `--distros ubuntu` →
host `ubuntu`, today's behavior), a family appearing with **multiple distinct versions** gets a version
suffix per host (digits only, `22.04` → `2204`), and the **same `(family, version)` repeated** gets an index
suffix (`-1`, `-2`, …); **rolling families take no version suffix** (`arch` → `arch`, repeated →
`arch-1/2/3`). Because naming is over the *resolved* multiset, a bare entry beside an explicit-version sibling
of the same family is disambiguated by version, not left bare. The expander SHALL guarantee unique names.
Each host spec SHALL carry the run's required capabilities (the battery's `requires`, lifted onto the spec by
the caller that builds the list) so that capability recipes can be rendered into the host's cloud-init at
create time — before the workload exists; `requires` is a provisioning attribute of the host spec (alongside
`os`), not workload data; the runner reads it from the spec, never from the opaque workload.

#### Scenario: One spec per host-list entry, carrying the run label

- **WHEN** a host list of N entries is expanded with run token `T`
- **THEN** N host specs are produced, each keyed by its bare per-host `name`, each carrying the
  `vmlease=<run-id>` label for the run-id derived from `T`

#### Scenario: The provider server name carries the run-id while the identity stays bare

- **WHEN** a host with bare identity `ubuntu-2204` is built for the run-id derived from token `T`
- **THEN** its provider server name is `vmlease-<run-id>-ubuntu-2204` (so it cannot collide with another run),
  while its identity `name` — used by `--keep`, the results record, and the matrix column — remains the bare
  `ubuntu-2204`

#### Scenario: A bare family preserves today's host name

- **WHEN** `--distros ubuntu` is expanded
- **THEN** one host spec is produced named `ubuntu` with `os = (ubuntu, <default version>)` — the host name
  is unchanged from today's behavior

#### Scenario: N hosts of the same (family, version) coexist with distinct names

- **WHEN** `--hosts arch,arch,arch` is expanded
- **THEN** three host specs are produced with distinct names `arch-1`, `arch-2`, `arch-3`, each with
  `os = (arch, rolling)`

#### Scenario: An explicit name is used verbatim as the host identity

- **WHEN** `--hosts api=ubuntu@24.04,worker=ubuntu@24.04` is expanded
- **THEN** two host specs are produced named `api` and `worker`, both with `os = (ubuntu, 24.04)`, with no
  index suffix applied

#### Scenario: An invalid user-supplied name fails closed

- **WHEN** `--hosts My_Host=ubuntu` is expanded (the name violates the host-name charset)
- **THEN** the expander reports a name-validation error and produces no host specs, before any provider call

#### Scenario: A bare entry beside an explicit-version sibling is disambiguated by version

- **WHEN** `--hosts ubuntu,ubuntu@22.04` is expanded and the ubuntu default version is `24.04`
- **THEN** two host specs are produced named `ubuntu-2404` and `ubuntu-2204` — the bare entry resolves to the
  default version and is suffixed, rather than keeping the bare name `ubuntu`

#### Scenario: The default host list stays the bare families

- **WHEN** a run is invoked with no `--hosts`/`--distros` selection
- **THEN** the default host list is the four bare families (one host each, named for the family) and does NOT
  expand to every version in the registry

#### Scenario: The deprecated alias warns and last-wins

- **WHEN** `--distros ubuntu` is supplied (alone, or together with a later `--hosts`)
- **THEN** a one-time deprecation notice is emitted, and when both flags are present the last-supplied value
  is the one expanded

#### Scenario: plan renders the resolved os per host

- **WHEN** `plan` is run over `--hosts ubuntu@22.04,arch`
- **THEN** the dry-run output shows each host's resolved `os` — `ubuntu@22.04` rendered as `ubuntu@22.04` and
  the rolling `arch` rendered as bare `arch` (not `arch@rolling`) — alongside its identity `name`, with no
  provider calls made

#### Scenario: Build is pure and deterministic

- **WHEN** the same host list (same token) is built twice
- **THEN** the resulting host specs (names, images, labels) are identical, with no provider calls made

#### Scenario: A duplicate host name fails closed before spend

- **WHEN** `build_host_specs` is given a host list containing two entries with the same `name`
- **THEN** it raises a clear name-collision error before any provider call, rather than letting the collision
  surface as a provider create failure

#### Scenario: Host specs carry the run's required capabilities

- **WHEN** a host list is built from a battery declaring `requires = ["docker"]`
- **THEN** each host spec carries that required-capability set, so the runner renders the docker recipe into cloud-init at create time

### Requirement: The plan dry-run makes zero provider calls

The system SHALL render a `plan` that shows exactly what a real run would provision — one plan item per
host, including image, server type, the resolved `os` (`family@version`, rolling bare), the host's **required capabilities**, and a summary of the
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
tearing them down, so an operator can SSH in and iterate against a live host. The selection SHALL key on the
host **`name`**, with the name-vs-family ambiguity removed structurally rather than by a silent heuristic:

- A bare/empty selection SHALL keep every host.
- A bare `--keep <token>` SHALL match an exact host **`name`** only — never a fuzzy family fallback.
- Keeping a whole family SHALL be the explicit selector `family:<family>` (parsed by splitting on the first
  `:`; only the `family:` prefix is recognized — there is no `host:` form), which keeps every host of that
  family in the run.
- A selector that resolves to **zero** in-run hosts SHALL **fail closed** before any host is provisioned —
  both a bare token matching no host name AND a `family:<family>` form matching no in-run host — with an error
  listing the eligible host names **and** the `family:` hint, never a silent no-op or family fallback, so a
  typo (or a family absent from this run) costs nothing.
- The resolved keep-set (host names + count) SHALL be **echoed before provisioning even under the assume-yes
  flag**, so the kept set is always visible before spend (it rides the existing billing confirm
  interactively, and prints regardless under `--yes`).
- A host `name` equal to a family name SHALL be rejected (no `name`/`family:` namespace collision or
  shadowing).

Each kept host SHALL carry a keep marker label that distinguishes it from torn-down hosts, and SHALL record a
structured reattach record — host `name`, id, IPv4, **family**, **version**, operator, and the private-key
path — so the live host is discoverable from the results without parsing prose. The `--keep` metavar SHALL be
`HOST` (accepting `<name>` or `family:<family>`). When any host is kept, the run's throwaway keypair SHALL
survive the run so the recorded SSH key path points at a real file.

#### Scenario: Bare keep leaves all hosts live

- **WHEN** a run requests keep with no selection
- **THEN** no host is torn down, each carries the keep marker label, and each records its structured reattach
  coordinates (including its `name`, family, and version)

#### Scenario: A bare token keeps only the named host

- **WHEN** a run over multiple hosts requests `--keep ubuntu-2204`
- **THEN** only the host named `ubuntu-2204` is left live (with the keep marker label and a reattach record)
  and every other host is torn down normally

#### Scenario: A family selector keeps the whole family

- **WHEN** a run containing two ubuntu hosts (`ubuntu-2204`, `ubuntu-2404`) requests `--keep family:ubuntu`
- **THEN** both ubuntu-family hosts are left live and every non-ubuntu host is torn down normally

#### Scenario: An unresolved keep token fails closed before spend

- **WHEN** the keep selection names a token that matches no host name and is not a `family:` form
- **THEN** the command reports the error listing the eligible host names and the `family:` hint, and
  provisions nothing

#### Scenario: A family selector matching zero in-run hosts fails closed

- **WHEN** a run containing no fedora hosts requests `--keep family:fedora`
- **THEN** the command fails closed before provisioning, reporting that `family:fedora` matched no host in the
  run, rather than silently keeping nothing

#### Scenario: An empty family selector is a distinct error

- **WHEN** a run requests `--keep family:` with no family name after the colon
- **THEN** the command fails closed reporting that a `family:` selector requires a family name (distinct from a
  zero-match message), so the user sees the typo

#### Scenario: The resolved keep-set is echoed even under --yes

- **WHEN** a run is invoked with `--keep` and the assume-yes flag
- **THEN** the resolved keep-set (host names + count) is printed before provisioning, even though the billing
  prompt is skipped

#### Scenario: A host name equal to a family name is rejected

- **WHEN** a host list would produce a host whose `name` equals a family name in the registry
- **THEN** the run is rejected before provisioning, so a `name`/`family:` namespace collision cannot occur

### Requirement: A run refuses to start when its run-token already has live hosts

The `run` and `build-image` commands SHALL perform a **pre-flight liveness check** before any provisioning and
SHALL **fail closed** when the run-token already has live provider hosts. The rationale: the run-id is derived
deterministically from the run-token (the same token always yields the same run-id), so a run-token identifies
**exactly one live run** — two live runs sharing a token would collide on their derived provider server names
AND on the shared `vmlease=<run-id>` reap label, and since teardown reaps by that label, the second run's
cleanup could destroy the first run's hosts. Accordingly: if any provider host already carries the run-id
label, the command SHALL provision nothing and SHALL report the live host names plus a hint to reap them
(`vmlease reap --run-token <token>`) or use a different run-token. The check SHALL run **after** the cheap
local validations (the cost-guard host cap, `--keep` validation, the within-run name-uniqueness assertion) and
**before** the billing confirmation, so a doomed run is refused before the operator is asked to spend. The
`plan` dry-run SHALL NOT perform this check (it makes no provider calls). If the liveness check's own provider
read fails, the command SHALL fail rather than proceed (liveness unverified), never falling open.

#### Scenario: A run-token with live hosts is refused before spend

- **WHEN** `run` is invoked with a run-token for which a provider host already carries the run-id label
- **THEN** the command fails closed without provisioning, listing the live host name(s) and the reap /
  different-token hint, before any billing confirmation

#### Scenario: A clean run-token proceeds

- **WHEN** `run` is invoked with a run-token that has no live hosts
- **THEN** the pre-flight passes and the run proceeds to the billing confirmation and provisioning

#### Scenario: build-image honors the same pre-flight

- **WHEN** `build-image` is invoked with a run-token whose builder from a prior invocation is still live
- **THEN** the command fails closed without provisioning a new builder, reporting the live host and the reap hint

#### Scenario: The plan dry-run skips the liveness check

- **WHEN** `plan` is invoked with a run-token that has live hosts
- **THEN** the dry-run still renders the plan and makes no provider calls (the liveness pre-flight is run/build-image only)

#### Scenario: A failed liveness read fails the run

- **WHEN** the pre-flight liveness read itself errors (e.g. the provider is unreachable)
- **THEN** the command fails rather than proceeding — liveness could not be verified, so it does not fall open
  and provision anyway

