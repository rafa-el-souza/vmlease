# distro-profiles Specification

## Purpose
Per-distro provisioning: a read-only profile registry (image + package/repo prep), stdlib `@@name@@` cloud-init rendering, and the rescue-write path for distros with no native provider image (write a verified disk image onto a cheap base host's disk). A profile carries an injected `RescueImageSpec` for the rescue-write distros; the image's acquisition + trust gate is owned by the `rescue-image` capability.
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

### Requirement: Rescue-readiness and post-write-readiness are distinct

The system SHALL treat "the rescue system is ready" (the rescue OS answers, confirmed by its identity —
not merely that some SSH answers) as a different readiness criterion from "the rewritten host is ready"
(owned by the workload's operator-readiness wait), and SHALL not conflate them.

#### Scenario: Rescue readiness confirms the rescue OS

- **WHEN** the system waits for rescue readiness
- **THEN** it confirms the responding system is the rescue OS, not any SSH endpoint (the base OS also
  answers SSH and must not false-positive)

### Requirement: A rescue-written host runs a kernel matching its installed modules

The system SHALL ensure that, after a rescue-written host's first-boot system upgrade, the booted host runs a kernel whose `/lib/modules/$(uname -r)/` module tree is populated — even when that upgrade replaced the kernel package and orphaned the running kernel's module tree. Because cloud-init user-data runs once per instance (a reboot does not re-run it), the post-upgrade provisioning remainder SHALL be carried across a reboot by a self-contained mechanism that does NOT rely on cloud-init re-running. The reboot SHALL fire at most once, and only when the first-boot upgrade orphaned the running kernel's module tree.

#### Scenario: Kernel upgraded — reboot and resume

- **WHEN** a rescue-written host's first-boot upgrade replaces the running kernel (the running kernel's `/lib/modules/$(uname -r)/modules.dep` is no longer present)
- **THEN** the system finishes provisioning across exactly one reboot, so the booted host runs the upgraded kernel with a populated `/lib/modules/$(uname -r)/`, and the readiness sentinel is set only after the post-upgrade remainder (operator account, package install, operator key) has completed

#### Scenario: Kernel unchanged — no reboot

- **WHEN** a rescue-written host's first-boot upgrade leaves the running kernel in place (its `/lib/modules/$(uname -r)/modules.dep` is still present)
- **THEN** provisioning completes on the first boot without rebooting, and the readiness sentinel is set as before

