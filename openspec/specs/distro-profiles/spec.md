# distro-profiles Specification

## Purpose
TBD - created by archiving change graduate-probehost-to-vmlease. Update Purpose after archive.
## Requirements
### Requirement: A read-only distro profile registry drives per-distro provisioning

The system SHALL resolve each distro key to a profile carrying its default provider image and its
per-distro preparation (packages / repository / extra setup), exposed through a read-only registry, and
SHALL raise a clear error for an unknown distro key.

#### Scenario: A known distro resolves to its profile

- **WHEN** a known distro key is looked up
- **THEN** its profile (default image + prep) is returned

#### Scenario: An unknown distro is rejected

- **WHEN** an unknown distro key is looked up
- **THEN** the system raises an unknown-distro error

### Requirement: Cloud-init is rendered with stdlib templating before create

The system SHALL render each host's cloud-init from its distro profile using stdlib `@@name@@`
placeholder substitution (no third-party templating), injecting the operator account and the throwaway
public key, and SHALL render (and validate) it before any create call so a template defect fails before
spend.

#### Scenario: Cloud-init carries operator access

- **WHEN** cloud-init is rendered for a host
- **THEN** it authorizes the throwaway public key for the operator account, and the same cloud-init is
  re-applied by a rescue-written image from the provider datasource

### Requirement: Rescue-write provisions distros with no native provider image

For a distro flagged as needing a rescue-write, the system SHALL transform a freshly-created base host
into the target distro — by rescue-writing a verified image onto its disk and rebooting — after create
and before the workload, and SHALL require the registered ssh key (its name plus its local private half)
for root access into the rescue system, distinct from the throwaway operator key.

#### Scenario: Rescue-write requires the registered key

- **WHEN** a matrix contains a rescue-write distro but no registered ssh key / private-half path is
  provided
- **THEN** the run refuses with an error naming both required inputs, and nothing is provisioned

#### Scenario: Two keys, two roles

- **WHEN** a rescue-write host is transformed
- **THEN** root access into the rescue system uses the registered key's private half, while operator
  access into the booted host uses the throwaway key

### Requirement: The rescue image is trust-gated before use

The system SHALL, for the rescue-write path, resolve the latest image, verify its SHA256, and verify its
signature against a pinned GPG key before writing it, treating that verification as a load-bearing trust
gate.

#### Scenario: An unverified image is not written

- **WHEN** the resolved image fails SHA256 or pinned-GPG-signature verification
- **THEN** the system refuses to rescue-write it

#### Scenario: A pinned primary key accepts a subkey-signed image

- **WHEN** the image is signed by the pinned key's signing subkey and the operator has pinned the
  primary-key fingerprint (gpg's VALIDSIG line carries the subkey fingerprint first and the primary last)
- **THEN** verification matches the pinned fingerprint in either position and accepts the signature
  (matching only the subkey position would wrongly reject every real signature)

#### Scenario: The trust gate runs before any host mutation

- **WHEN** image verification fails
- **THEN** it fails before `enable-rescue` or any destructive provider call, so nothing runs against an
  untrusted image

### Requirement: Rescue-readiness and post-write-readiness are distinct

The system SHALL treat "the rescue system is ready" (the rescue OS answers, confirmed by its identity —
not merely that some SSH answers) as a different readiness criterion from "the rewritten host is ready"
(owned by the workload's operator-readiness wait), and SHALL not conflate them.

#### Scenario: Rescue readiness confirms the rescue OS

- **WHEN** the system waits for rescue readiness
- **THEN** it confirms the responding system is the rescue OS, not any SSH endpoint (the base OS also
  answers SSH and must not false-positive)

