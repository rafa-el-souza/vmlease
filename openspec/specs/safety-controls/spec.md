# safety-controls Specification

## Purpose
The guarantees that keep a throwaway-VM harness from leaking spend: the cost guard (host cap + cheap-server-type allowlist), the deterministic `vmlease=<run-id>` label scheme, confirm-before-create, guaranteed best-effort teardown, the `reap`/`status` orphan backstop, and provider-token-blindness.
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

### Requirement: Upload sources and destinations are validated fail-closed before any provisioning

The system SHALL validate every upload source and remote destination **before any provider call** and
SHALL refuse the run fail-closed when an input is problematic, so a bad upload aborts before spend rather
than on a half-built billable host. The `plan` dry-run (which makes zero provider calls) SHALL perform
the same validation. Validation is host-independent and SHALL be performed once per run.

An upload **source** SHALL be refused when it: does not exist; is a symlink (the final component **or**
any symlink in its resolved path chain); is not a regular file (a directory, FIFO, socket, or device);
or is not readable. The source SHALL be inspected without following a symlink, so a symlink's target is
never read or shipped.

An upload **remote destination** SHALL be refused when it: is empty; contains a `..` path segment;
begins with `-` (option injection); or contains any character outside a conservative allowlist (no
spaces and no shell-unsafe metacharacters).

#### Scenario: A symlink source is refused

- **WHEN** an upload source is a symlink, or resolves through a symlinked path component
- **THEN** the run is refused with an error naming the symlink, and nothing is provisioned (the symlink's
  target is never read or shipped)

#### Scenario: A non-regular-file source is refused

- **WHEN** an upload source is a directory (or other non-regular file such as a FIFO, socket, or device)
- **THEN** the run is refused with an error stating it is not a regular file, and nothing is provisioned

#### Scenario: A missing or unreadable source is refused

- **WHEN** an upload source does not exist or is not readable
- **THEN** the run is refused with an error describing the problem, and nothing is provisioned

#### Scenario: A traversing or unsafe remote destination is refused

- **WHEN** an upload remote destination contains a `..` segment, begins with `-`, is empty, or contains a
  shell-unsafe character
- **THEN** the run is refused with an error describing the problem, and nothing is provisioned

#### Scenario: plan rejects a bad upload before any provider call

- **WHEN** `plan` is invoked with a problematic upload source or destination
- **THEN** `plan` raises the upload-validation refusal and makes zero provider calls

