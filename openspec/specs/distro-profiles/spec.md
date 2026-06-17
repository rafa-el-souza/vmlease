# distro-profiles Specification

## Purpose
Per-distro provisioning: a read-only profile registry (image + package/repo prep), stdlib `@@name@@` cloud-init rendering, and the rescue-write path for distros with no native provider image (write a verified disk image onto a cheap base host's disk). A profile carries an injected `RescueImageSpec` for the rescue-write distros; the image's acquisition + trust gate is owned by the `rescue-image` capability.
## Requirements
### Requirement: A read-only distro profile registry drives per-distro provisioning

The system SHALL resolve each distro reference — an `os = (family, version)` tuple — to a profile carrying
that version's provider image slug and its per-distro preparation (packages / repository / extra setup) and
rescue behavior, exposed through a read-only registry **keyed by `(family, version)`**, and SHALL raise a
clear error for an unknown reference (an unknown family, or an unknown version of a known family). A family
SHALL have at least one version entry, and **each version entry SHALL carry its own image slug, substrate
prep, and rescue behavior** (any version-conditional prep is declarative in that entry). The registry SHALL
expose a per-family **default-version pointer** — an **explicit designation** that SHALL NOT be inferred from
declaration order — so that a bare family reference (no version) resolves to that family's current default
version, preserving the back-compat case where a caller names only the family. **Rolling families** (e.g.
arch) SHALL use the sentinel version `rolling` and SHALL take no explicit `@version`; an `arch@<anything>`
reference SHALL be an error. A profile's image slug SHALL be resolved per `(family, version)` entry (its own
`image` field) and SHALL NOT weld the version into a single image slug shared across versions.
A profile's preparation SHALL describe the **generic, battery-agnostic distro box** only; optional
vmlease-provided capabilities (e.g. docker) SHALL NOT be part of profile preparation — they are layered
per-distro as capability recipes selected by a battery's `requires` (see the `host-capabilities` capability).
Substrate that every host of a `(family, version)` needs regardless of capability (e.g. kernel-module loads)
MAY remain in profile extra-setup.

#### Scenario: A known (family, version) resolves to its profile

- **WHEN** a known `(family, version)` reference is looked up
- **THEN** its profile (that version's image slug + prep + rescue behavior) is returned

#### Scenario: A bare family resolves via the default-version pointer

- **WHEN** a known family is looked up with no version
- **THEN** it resolves to that family's current default version's profile

#### Scenario: The default version is explicit, not declaration-order dependent

- **WHEN** a family's version entries are reordered in the registry without changing the explicit
  default designation
- **THEN** a bare family reference resolves to the same default version as before (the default is not
  the first-declared entry)

#### Scenario: A rolling family takes no version

- **WHEN** a rolling family (e.g. arch) is referenced with an explicit `@version`
- **THEN** the system raises an error; a bare reference resolves to the sentinel `rolling` version

#### Scenario: An unknown distro is rejected

- **WHEN** an unknown reference is looked up (an unknown family, or an unknown version of a known family)
- **THEN** the system raises an unknown-distro error naming the family (and version when applicable)

#### Scenario: Docker is not part of profile preparation

- **WHEN** any distro profile's preparation is inspected
- **THEN** it contains no docker packages, docker repository setup, or docker bundle (docker is a capability recipe, not profile prep), while always-on substrate such as kernel-module loads remains

### Requirement: Cloud-init is rendered with stdlib templating before create

The system SHALL render each host's cloud-init from its distro profile using stdlib `@@name@@`
placeholder substitution (no third-party templating), injecting the operator account and the throwaway
public key, and SHALL render (and validate) it before any create call so a template defect fails before
spend. The render SHALL also inject, for the host's package manager, the recipe of each capability named in
the battery's `requires`; a host whose battery requires no capability SHALL render no optional-capability
content (e.g. a docker-less cloud-init). The required-capability set SHALL be a **required input** to the
render, so the identical render feeds both provisioning and the cache-key computation (the key therefore
varies with `requires` without a separate term).

#### Scenario: Cloud-init carries operator access

- **WHEN** cloud-init is rendered for a host
- **THEN** it authorizes the throwaway public key for the operator account, and the same cloud-init is
  re-applied by a rescue-written image from the provider datasource

#### Scenario: A required capability is rendered; a default host is capability-less

- **WHEN** cloud-init is rendered for a host whose battery requires docker, and separately for a host whose battery requires nothing
- **THEN** the first render contains the docker recipe for the host's manager and the second contains no docker content

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

