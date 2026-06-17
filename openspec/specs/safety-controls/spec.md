# safety-controls Specification

## Purpose
The guarantees that keep a throwaway-VM harness from leaking spend: the cost guard (host cap + cheap-server-type allowlist), the deterministic `vmlease=<run-id>` label scheme (plus the `vmlease-keep` marker on hosts `--keep` leaves live), confirm-before-create, best-effort teardown (skipped for kept hosts), the `reap`/`status` orphan backstop (whose automatic firings spare kept hosts, while the explicit `reap` destroys every labelled host), and provider-token-blindness.
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

The system SHALL apply the `vmlease=<run-id>` label to every provisioned **run host** — the ephemeral,
billable servers of a run, **including the throwaway builder a `build-image` provisions** — and SHALL
expose that label as the selector used to list and reap a run's hosts. **Persistent cache images are NOT
run hosts**: they are content-addressed and carry their own image-class label set (`vmlease-purpose=image-cache`,
`vmlease-cache-key`, …) instead of the ephemeral per-run label, and so are excluded from per-run reap (see
"Cached images are reaped as a persistent class").

#### Scenario: Run hosts are reap-discoverable by label

- **WHEN** hosts are provisioned for a run (including a `build-image` builder)
- **THEN** each carries the `vmlease=<run-id>` label and is discoverable by the `vmlease=<run-id>` selector

#### Scenario: A cache image does not carry the run label

- **WHEN** `build-image` creates a cache image
- **THEN** the image carries its content-addressed label set, not the `vmlease=<run-id>` label

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
delete's reported success. The **automatic** backstops triggered by the run command SHALL reap only the
hosts of the run label that are NOT marked to be kept: a deliberately kept host carries a keep marker label
and SHALL be spared by the backstop, whereas the **explicit** `reap` command (and the `vmlease reap`
verb) destroys every labelled host including kept ones — the operator's deliberate "destroy all". The
backstop SHALL be triggered automatically in two cases so that a leaked billable host is never silent:
when the run is **aborted** by a `KeyboardInterrupt` or `SystemExit`, the command SHALL reap the non-kept
hosts of the run label, report what it reaped (and note any kept hosts left live), and re-raise; and when
any host reports a **teardown failure**, the command SHALL attempt a reap of the non-kept hosts, print a
prominent summary, and exit non-zero. A run whose hosts all tore down cleanly SHALL still exit zero.

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

#### Scenario: An aborted run reaps non-kept hosts before propagating

- **WHEN** the run command is interrupted by a `KeyboardInterrupt` or `SystemExit` during execution
- **THEN** it reaps the run's hosts that are not marked to be kept as a backstop, leaves every kept host
  live, reports what it reaped, re-raises the interruption, and leaves the incrementally-written results
  file holding every host that had finished before the abort

#### Scenario: A kept host survives the automatic backstop

- **WHEN** a run with a kept host (a bare keep or a kept distro subset) is aborted or has a teardown
  failure on a different host
- **THEN** the automatic backstop spares every host carrying the keep marker and destroys only the
  non-kept hosts, so the operator can still SSH into the kept host afterward

#### Scenario: A teardown failure surfaces as a non-zero exit

- **WHEN** any host's result carries a teardown-failure warning after the run
- **THEN** the command attempts a reap of the non-kept hosts of the run label, prints a prominent summary,
  and exits non-zero — while a run with all hosts torn down cleanly exits zero

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

### Requirement: Directory upload sources are validated fail-closed

The system SHALL validate a directory upload source fail-closed before transferring it, refusing: a
source that does not exist; a source that is a symlink or is reached through a symlinked path component
(the directory is never reached **through** a symlink); a source that is not a directory; or an unreadable
directory. The source SHALL be inspected without following a symlink. Within-tree symlink safety during
transfer is handled by the recursive directory push (which does not follow out-of-tree symlinks); this
requirement governs the **entry-point** directory itself.

#### Scenario: A directory reached through a symlink is refused

- **WHEN** a directory upload source is itself a symlink, or is reached through a symlinked path component
- **THEN** the push is refused with an error naming the symlink, and nothing is transferred

#### Scenario: A non-directory or unreadable source is refused

- **WHEN** a directory upload source does not exist, is not a directory, or is not readable
- **THEN** the push is refused with an error describing the problem, and nothing is transferred

### Requirement: The image quota guard caps cached image count
A count-only image quota guard SHALL cap the number of vmlease cache images (default 10, overridable),
checked during build-image before a builder is provisioned. It is a self-limit on vmlease's own images,
distinct from the provider's account-wide ceiling (which is enforced separately by the typed provider
quota error). Reaching the guard with no same-group superseded image to reclaim SHALL raise
`ImageQuotaError`.

#### Scenario: At the guard cap with nothing to reclaim refuses
- **WHEN** build-image is at the image cap and no same-group superseded image exists
- **THEN** it raises `ImageQuotaError` before provisioning a builder

### Requirement: Cached images are reaped as a persistent class
Cache images SHALL be reaped by a dedicated `reap-images` command — by family (`--distro <family>`), by an
explicit older-than cutoff timestamp, or by supersession (a content key no longer current for its group) —
with a `--dry-run` mode that reports without deleting. `reap-images --superseded` SHALL group per
**`(family, version)`** (joined with architecture and the required-capability set), so a sibling version's
cached image is **never** deleted by another version's supersession (the cross-version data-loss class this
grouping exists to prevent). `reap-images --distro <family>` SHALL stay **family-scoped** — it reaps every
version of that family. Reaping SHALL be idempotent and best-effort with a partial-success report.
Supersession resolution SHALL be fail-safe: a group whose current key cannot be resolved is kept and warned,
never deleted. Cache images are persistent and SHALL NOT be reaped at run end.

#### Scenario: Superseded reap groups per (family, version)
- **WHEN** `reap-images --superseded` runs with cached images for `ubuntu@22.04` and `ubuntu@24.04`, each current for its own `(family, version)` group
- **THEN** neither is deleted as superseded — supersession is resolved per `(family, version)`, so one version's image is never treated as a superseded predecessor of another's

#### Scenario: A family-scoped reap removes every version of the family
- **WHEN** `reap-images --distro ubuntu` runs
- **THEN** every cached image of the ubuntu family is reaped regardless of version

#### Scenario: Superseded reap is fail-safe on an unresolvable group
- **WHEN** `reap-images --superseded` cannot resolve the current key for a group
- **THEN** images of that group are kept and a warning is reported, while resolvable groups proceed

#### Scenario: Older-than uses an explicit caller-supplied cutoff
- **WHEN** `reap-images --older-than` is given an ISO-8601 cutoff timestamp
- **THEN** images whose creation timestamp parses to before that cutoff are deleted, with no reliance on an internal clock

#### Scenario: Supersession removes a whole group whose current image is absent
- **WHEN** `reap-images --superseded` runs for a `(family, version)` group whose current key resolves but no cached image matches it (upstream rolled and nothing was rebuilt)
- **THEN** every cached image for that group is treated as superseded and deleted (use `--older-than` for age-protection instead)

### Requirement: Cached images carry a persistent content-addressed label set
Each cache image SHALL carry a persistent label set (purpose, content key, schema version, distro (= the
**family**), **version**, architecture, source fingerprint, build provenance) emitted by a single function,
and SHALL NOT carry the ephemeral per-run reap label. The `version` label SHALL accompany the existing
`distro` (= family) label so a `(family, version)` group is identifiable from the labels alone; for a rolling
family the `version` label SHALL be the sentinel `rolling`. Image age for reaping SHALL be read from the
image's own creation timestamp, not from a label.

#### Scenario: A cache image survives a per-run reap
- **WHEN** a run's per-run reap executes
- **THEN** cache images, which lack the per-run label, are not deleted

#### Scenario: A cache image carries family and version labels
- **WHEN** `build-image --distro ubuntu@22.04` creates a cache image
- **THEN** the image carries a `distro` label of `ubuntu` (family) and a `version` label of `22.04`, distinguishing it from an `ubuntu@24.04` image of the same family

