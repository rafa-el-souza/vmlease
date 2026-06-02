# safety-controls Specification

## Purpose
TBD - created by archiving change graduate-probehost-to-vmlease. Update Purpose after archive.
## Requirements
### Requirement: The cost guard caps host count and restricts server types

The system SHALL refuse, before any provider call, a matrix that exceeds a maximum host count or that
requests a server type outside a cheap, hourly-billed allowlist, so a runaway matrix cannot spin up a
fleet of expensive boxes.

#### Scenario: Too many hosts is refused

- **WHEN** a matrix requests more hosts than the configured cap
- **THEN** the cost guard raises a refusal naming the count and the cap, and nothing is provisioned

#### Scenario: A non-allowlisted server type is refused

- **WHEN** a matrix requests a server type not in the cheap allowlist
- **THEN** the cost guard raises a refusal naming the offending type, and nothing is provisioned

### Requirement: Run-ids are derived deterministically from a caller token

The system SHALL derive a label-safe run-id from a caller-supplied token (not a wall-clock read or RNG),
normalizing it to `[a-z0-9-]` and validating its shape, so the same token always targets the same run and
its resources can be re-found for status/reap.

#### Scenario: A token normalizes to a stable run-id

- **WHEN** a token is supplied
- **THEN** it is normalized and validated into a run-id, and the same token yields the same run-id every
  time

#### Scenario: An unusable token is rejected

- **WHEN** a token cannot yield a valid run-id (wrong length / charset)
- **THEN** the system raises an error describing the required shape

### Requirement: Every resource carries the run label

The system SHALL apply the `vmlease=<run-id>` label to every provisioned resource, and SHALL expose that
label as the selector used to list and reap a run's hosts.

#### Scenario: Resources are reap-discoverable by label

- **WHEN** hosts are provisioned for a run
- **THEN** each carries the `vmlease=<run-id>` label and is discoverable by the `vmlease=<run-id>`
  selector

### Requirement: Confirm-before-create gates real spend

The system SHALL print what it is about to provision (billable) and require explicit confirmation before
creating any host, with an opt-out flag for non-interactive use.

#### Scenario: Declining the prompt provisions nothing

- **WHEN** the operator is prompted before provisioning and does not confirm
- **THEN** nothing is provisioned and the run aborts cleanly

#### Scenario: The opt-out flag skips the prompt

- **WHEN** the run is invoked with the assume-yes flag
- **THEN** provisioning proceeds without prompting

### Requirement: Reap is the idempotent orphan backstop

The system SHALL provide a `reap` that destroys every live host carrying a run's label and returns what it
destroyed, tolerating already-gone hosts so a re-reap is safe; a `destroy` SHALL retry transient
provider timeouts, and reap correctness SHALL be confirmed by verifying zero residue rather than by a
delete's reported success.

#### Scenario: Reap destroys all of a run's hosts

- **WHEN** `reap` is invoked for a run-id with live labelled hosts
- **THEN** every such host is destroyed and the destroyed set is returned

#### Scenario: Re-reaping is safe

- **WHEN** `reap` is invoked again for the same run-id
- **THEN** it completes without error even though the hosts are already gone

#### Scenario: A transient delete timeout does not leave an undetected orphan

- **WHEN** a `destroy` reports a transient timeout
- **THEN** the system retries and treats post-delete zero-residue as the proof of teardown, not the
  command's reported success

### Requirement: The harness is provider-token-blind

The system SHALL never read or log the provider API token, relying instead on the operator's
out-of-band-configured provider context.

#### Scenario: No token is read

- **WHEN** any provider operation runs
- **THEN** the harness does not read or emit the provider token

