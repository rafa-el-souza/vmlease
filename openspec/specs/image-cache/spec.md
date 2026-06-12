# image-cache Specification

## Purpose
TBD - created by archiving change snapshot-image-cache. Update Purpose after archive.
## Requirements
### Requirement: build-image produces a content-addressed snapshot of a prepped host
The `vmlease build-image <distro>` command SHALL provision a builder host, run the full OS-level prep
(rescue-write for rescue-write distros plus cloud-init package install), wait for readiness, sysprep the
host, power it off, create a provider snapshot, and destroy the builder. The snapshot's labels SHALL be
applied in the same provider call that creates it (atomic), so no cache image ever exists unlabelled.
build-image SHALL carry a run token that labels its builder as a run host, so on abort or a
builder-teardown failure the builder is reaped by that label, any already-created image is kept, and a
teardown failure exits non-zero.

#### Scenario: Building a cache image for a native distro
- **WHEN** `build-image ubuntu` runs and no image with the current content key exists
- **THEN** a builder is provisioned, prepped, sysprepped, powered off, snapshotted with its labels, and destroyed
- **AND** the resulting image carries `vmlease-purpose=image-cache` and `vmlease-cache-key=<key>`

#### Scenario: build-image is idempotent
- **WHEN** `build-image ubuntu` runs, an image with the current content key already exists, and `--rebuild` is not given
- **THEN** it is a no-op: no builder is provisioned and nothing is spent

#### Scenario: --rebuild replaces the existing same-key image
- **WHEN** `build-image ubuntu --rebuild` runs and an image with the current key exists
- **THEN** a fresh image is built and the older-created same-key image is deleted, keeping the newest

#### Scenario: An aborted build reaps the builder and keeps any created image
- **WHEN** build-image is interrupted, or its builder teardown fails
- **THEN** the builder is reaped by its run label, any already-created image is kept, and a teardown failure exits non-zero

### Requirement: Sysprep precedes the snapshot and its failure aborts the build
build-image SHALL clear `/etc/machine-id` (and the dbus machine-id) on the prepped host before powering
off, so each restored host regenerates a unique id. If sysprep fails, build-image SHALL abort before
creating the snapshot and tear down the builder; it MUST NOT snapshot a non-sysprepped host.

#### Scenario: Sysprep failure aborts with no image
- **WHEN** the sysprep step fails over SSH
- **THEN** no snapshot is created, the builder is torn down, and the command errors

### Requirement: Poweroff precedes the snapshot and its failure aborts the build
build-image SHALL power the host off, within the same bounded wait the system uses for host teardown,
before snapshotting for a filesystem-consistent image. If poweroff fails or the host does not reach the
off state within that bound, build-image SHALL abort and tear down the builder.

#### Scenario: Poweroff timeout aborts
- **WHEN** the host does not reach the off state within the bounded wait
- **THEN** no snapshot is created and the build aborts

### Requirement: The content key is derived from the base-image fingerprint and the rendered cloud-init
The cache content key SHALL be `v1-<distro>-<hash>`, where the hash covers the base-image fingerprint
(rescue-write distro: the resolved image digest; native: the immutable provider image id; golden: the
pinned digest) and the canonically-rendered cloud-init (rendered with a placeholder operator public key so
the per-run key is excluded). The same key SHALL be produced by both build-image (to label) and run (to
look up) from one shared function.

#### Scenario: Identical recipe yields an identical key
- **WHEN** two build-image runs use the same distro, architecture, recipe, and resolved upstream
- **THEN** they compute the same content key

#### Scenario: A recipe change yields a new key
- **WHEN** the distro profile's package set changes
- **THEN** the content key changes and the next run rebuilds rather than reusing the stale image

### Requirement: build-image prunes its own superseded predecessors
After building the current image for a (distro, architecture) group, build-image SHALL delete every cached
image of that same group whose key differs from the new key, keeping at most one current image per group.
When not at the image cap it SHALL build first then prune; when at the cap it SHALL prune the superseded
set first to free slots then build, refusing only if no same-group superseded image exists. build-image
SHALL NOT prune images of other distros.

#### Scenario: Rebuilding a rolling distro reclaims the old slot
- **WHEN** `build-image arch` builds a new key and an older Arch image with a different key exists
- **THEN** the older Arch image is deleted and the new one is kept

#### Scenario: At the cap with nothing to prune refuses before provisioning
- **WHEN** at the image cap and no same-group superseded image exists
- **THEN** build-image refuses with ImageQuotaError before provisioning a builder

### Requirement: Restore re-injects the operator key via minimal cloud-init
On a cache hit, run SHALL create the server directly from the snapshot image with a minimal cloud-init that
re-authorizes the operator's key and nothing else, skipping rescue-write and package install. Restore SHALL
NOT require a provider-registered SSH key.

#### Scenario: Cache hit restores without rescue or prep
- **WHEN** run finds a cached image matching the host's content key, architecture, and disk bound (the snapshot's disk size ≤ the target server's disk)
- **THEN** it creates the server from that image with a minimal key-only cloud-init and skips rescue-write and package install

### Requirement: The cache is advisory and degrades on any restore failure
A cache lookup or a create-from-image that fails before a server is created SHALL degrade to a cache miss
(the cold path), not fail the host. A restored server that is created but fails readiness SHALL be recorded
as a normal host failure (not retried cold), with a message naming the source image. **By default only
that hint is emitted** (the image is not auto-reaped, so a genuine fault is not masked); an opt-in flag MAY
auto-reap the source image on such a readiness failure.

#### Scenario: Image deleted mid-restore falls back to cold
- **WHEN** the matching image is deleted between the lookup and the create-from-image
- **THEN** that host falls back to the cold path and the run does not fail

#### Scenario: Lookup failure falls back to cold
- **WHEN** listing images fails during a run
- **THEN** the affected hosts proceed via the cold path with a warning

#### Scenario: A restored host failing readiness is a host failure, not a cold retry
- **WHEN** a server created from a cached image fails the readiness gate
- **THEN** it is recorded as a host failure naming the source image, and is not re-provisioned cold

### Requirement: build-image server-type is configurable; restore is architecture-matched and disk-bounded
build-image SHALL accept a configurable server type (defaulting to the run default) whose disk size becomes
the snapshot's, and that server type SHALL pass the cost-guard allowlist. A cached image SHALL be used to
restore only onto a server of matching architecture whose disk size is at least the snapshot's; otherwise it
is a cache miss.

#### Scenario: An image too large for the target is a miss
- **WHEN** the only cached image's disk size exceeds the run's chosen server disk
- **THEN** it is treated as a cache miss and the host takes the cold path

### Requirement: build-image validates the rescue key before provisioning
For a rescue-write distro, build-image SHALL require the registered rescue SSH key (its name and local
private half) and validate its presence BEFORE provisioning the builder or generating a keypair.

#### Scenario: Missing rescue key refuses early
- **WHEN** `build-image arch` runs without the rescue key
- **THEN** it refuses before provisioning any host

