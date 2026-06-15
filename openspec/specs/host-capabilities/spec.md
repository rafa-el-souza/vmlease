# host-capabilities Specification

## Purpose
TBD - created by archiving change battery-prep-and-requires. Update Purpose after archive.
## Requirements
### Requirement: A battery opts in to vmlease-provided capabilities via `requires`, default-off

The battery manifest MAY carry a root-level `requires` list naming vmlease-provided capabilities the host needs. The set is **opt-in and default-off**: a battery that does not name a capability SHALL provision a host without it. The system SHALL raise a clear battery-load error for a `requires` entry that is not a known capability. The v1 capability vocabulary SHALL be exactly `docker`.

#### Scenario: A battery without requires gets a capability-less host
- **WHEN** a battery declares no `requires` (or `requires = []`)
- **THEN** the provisioned host has no vmlease-provided capability installed (in particular, no docker)

#### Scenario: requires names docker
- **WHEN** a battery declares `requires = ["docker"]`
- **THEN** the provisioned host has docker installed

#### Scenario: An unknown capability is rejected
- **WHEN** a battery declares `requires = ["dokcer"]` or any name outside the known vocabulary
- **THEN** the system raises a battery-load error naming the unknown capability

### Requirement: A capability is realized per-distro by a recipe injected into cloud-init only when required

The system SHALL model each capability as a per-package-manager **recipe** — a package list (added to the base install line) plus an optional setup fragment (shell run during cloud-init, which MAY itself perform a guarded package install) — held in a read-only registry keyed by capability then package-manager. When a host's battery requires a capability, the system SHALL inject that capability's recipe for the host's package manager into the host's rendered cloud-init; when the capability is not required, its recipe SHALL be absent from the render. A required capability that has no recipe for the host's package manager SHALL raise a clear error. Docker SHALL be provided exclusively through this mechanism and SHALL NOT be part of any base distro profile's preparation.

#### Scenario: A required capability's recipe is rendered
- **WHEN** a host's battery requires docker and the host's manager is apt
- **THEN** the rendered cloud-init contains the apt docker recipe's packages and setup fragment

#### Scenario: An unrequired capability is absent from the render
- **WHEN** a host's battery does not require docker
- **THEN** the rendered cloud-init contains no docker packages, repo setup, or bundle

#### Scenario: A capability unsupported on the host's manager errors
- **WHEN** a battery requires a capability that has no recipe for the host's package manager
- **THEN** the system raises a clear error naming the capability and the manager, before spend

#### Scenario: A recipe's setup fragment carries the guarded install
- **WHEN** the apt docker recipe is rendered
- **THEN** its `setup` fragment carries the docker-repo configuration (keyring + sources) **and** the guarded `docker-ce` install (behind the existing `command -v dockerd-rootless-setuptool.sh` idempotency guard), preserving byte-for-byte the block formerly hardcoded in the apt install template; the recipe's `packages` is empty for apt (the install stays inside the guarded setup fragment, not the base install line, so re-runs and the rendered bytes are unchanged), and the distro profile no longer carries a docker-repo slug

