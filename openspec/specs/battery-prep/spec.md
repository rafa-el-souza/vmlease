# battery-prep Specification

## Purpose
TBD - created by archiving change battery-prep-and-requires. Update Purpose after archive.
## Requirements
### Requirement: A battery may declare a prep phase of per-distro packages and setup steps

The battery manifest MAY carry a root-level `[prep]` section declaring host prerequisites the battery brings (distinct from probes — prep is setup, not a test). `[prep.packages]` SHALL be a flat table whose every key is a known package-manager OR a known distro (the two name-sets are disjoint); a key that is neither SHALL be a battery-load error. A host's effective package set SHALL be the union of the list under the host's package-manager key and the list under the host's distro key, deduplicated, manager entries first. `[[prep.setup]]` SHALL be an ordered array of setup steps, each carrying a unique `id`, **exactly one of** an inline `run` block or a `script` reference, an optional `distros` allowlist (default: every distro), an optional `required` boolean (default `true`), an optional `title`, and an optional `timeout` (seconds; when omitted, a prep-specific default of **1800s** applies — longer than the probe-runner default, since prep steps include source builds). A `script` step's file SHALL be resolved relative to the manifest directory, contained to the bundle, and shellchecked exactly as a probe `script` is. The system SHALL raise a clear battery-load error for a malformed `[prep]`: an unrecognized key at `[prep]`/`[prep.packages]`/a setup step, a selector key that is neither a known manager nor a known distro, a `distros` allowlist value that is not a known distro (a typo guard), a setup step declaring neither/both of `run`/`script`, a duplicate setup `id`, or an empty resolved command.

#### Scenario: Packages resolve as the union of manager and distro selectors
- **WHEN** `[prep.packages]` has `apt = ["a"]` and `debian = ["b"]` and the host is debian (an apt distro)
- **THEN** the effective package set is `["a", "b"]` (manager list first, deduplicated)

#### Scenario: A selector key that is neither a manager nor a distro is rejected
- **WHEN** `[prep.packages]` carries a key like `apt-get` or `ubntu`
- **THEN** the system raises a battery-load error naming the unrecognized selector key

#### Scenario: A setup step must declare exactly one command form
- **WHEN** a `[[prep.setup]]` step declares neither `run` nor `script`, or declares both
- **THEN** the system raises a battery-load error naming the offending step `id`

#### Scenario: A script setup step is contained and shellchecked
- **WHEN** a setup step references a `script` whose real path escapes the bundle (absolute, `..`, or out-of-tree symlink)
- **THEN** the system raises a battery-load error naming the step and the path, and a contained script is shellchecked like a probe script

#### Scenario: A duplicate setup id is rejected
- **WHEN** two `[[prep.setup]]` steps share an `id`
- **THEN** the system raises a battery-load error naming the duplicated `id`

#### Scenario: An unknown distro in a step's allowlist is rejected
- **WHEN** a setup step declares `distros = ["arhc"]` (a typo) — a value that is not a known distro
- **THEN** the system raises a battery-load error naming the unknown distro, rather than silently skipping the step on every host

### Requirement: The prep phase runs once per host after readiness and before the probe loop

The system SHALL run a battery's declared prep phase once per host, over SSH as the operator (with `sudo` written inline where root is needed), after the host's readiness gate and before the first probe. Within the phase, `[prep.packages]` SHALL be installed first as a single package-manager install pass, using the host's manager's install command (e.g. `apt-get install -y`, `dnf install -y`, `pacman -S --noconfirm`) resolved from a per-manager install-command mapping (the install counterpart to the existing per-manager system-update mapping); on apt the package index SHALL be refreshed (`apt-get update`) before install, since prep may run on a cached host with a stale index (dnf/pacman refresh implicitly); then the `[[prep.setup]]` steps SHALL run in authoring order; a setup step whose `distros` allowlist excludes the host SHALL be skipped. The prep phase is a battery-declared phase distinct from the probe loop, so it may later move behind a runner-sequenced boundary without any manifest change.

#### Scenario: Prep runs before probes
- **WHEN** a battery declares `[prep]` and probes
- **THEN** the prep phase completes on the ready host before the first probe runs

#### Scenario: Packages install before setup steps
- **WHEN** a battery declares both `[prep.packages]` and `[[prep.setup]]`
- **THEN** the package install pass runs first, then the setup steps in authoring order

#### Scenario: A step not matching the host distro is skipped
- **WHEN** a setup step declares `distros = ["arch"]` and the host is ubuntu
- **THEN** that step is skipped and not recorded as run

### Requirement: A prep step's failure aborts the host unless it is marked not required

`[prep.packages]` failure, and any `[[prep.setup]]` step with `required` true that fails, SHALL abort the host: its probes SHALL NOT run and the host SHALL be torn down through the normal teardown/reap path (the same path as any host failure). A `[[prep.setup]]` step with `required = false` that fails SHALL be recorded and the phase SHALL continue to the remaining setup steps and then the probe loop.

#### Scenario: A hard prep failure aborts the host
- **WHEN** a `[prep.packages]` install or a `required = true` setup step fails
- **THEN** the host runs no probes and is torn down via the normal teardown path

#### Scenario: A soft prep failure records and continues
- **WHEN** a `required = false` setup step exits non-zero
- **THEN** the failure is recorded and the remaining setup steps and the probe loop still run

### Requirement: Prep results are recorded in a structured prep_phase section

The results document SHALL carry a structured `prep_phase` section recording each executed prep step's `id`, exit status, `required` flag, and bounded captured stderr — distinct from the per-probe records, because a prep step is not a probe (it carries no tag and no assertions). A soft failure SHALL be recorded as a distinct non-pass state and SHALL NOT be silently dropped.

#### Scenario: A soft failure is recorded distinctly
- **WHEN** a `required = false` setup step fails
- **THEN** the `prep_phase` section records that step with its non-zero exit and a non-pass state

#### Scenario: prep_phase carries per-step records
- **WHEN** a prep phase runs
- **THEN** each executed step appears in `prep_phase` with its `id`, exit status, and `required` flag

