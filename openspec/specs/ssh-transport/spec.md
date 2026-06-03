# ssh-transport Specification

## Purpose
The CI-oriented SSH transports beyond the capture-at-end probe exec and single-file upload: **streaming** command execution (output delivered to a callback as it arrives, bounded by a hard wall-clock timeout that kills the local SSH client and the remote process so a hung gate leaves no orphan on a billed host), and **recursive directory push** (ships in-tree symlinks but drops symlinks pointing outside the tree, so a source tree cannot exfiltrate an out-of-tree file). Both reuse the recycled-IP SSH hardening. Consumed by an external CI workload; vmlease stays GitHub-blind.
## Requirements
### Requirement: Streaming command execution emits output incrementally and returns the exit code

The system SHALL provide a streaming command execution over SSH that invokes a caller-supplied callback
with output **as it arrives** (not captured only at completion) and SHALL return the command's exit code.
The command's own non-zero exit SHALL be returned as data — it is a result, not a transport error.

#### Scenario: Output is delivered incrementally

- **WHEN** a streamed command produces output over time
- **THEN** the caller's callback is invoked with output chunks as they arrive, before the command
  completes

#### Scenario: A non-zero command exit is returned, not raised

- **WHEN** a streamed command exits non-zero
- **THEN** its exit code is returned to the caller as data (a failing gate is a result, not a transport
  error)

### Requirement: A streamed command is bounded by a hard timeout that kills local and remote

The system SHALL bound a streamed command with a caller-supplied wall-clock timeout. On expiry it SHALL
terminate the local SSH client **and** ensure the remote process is killed, so no runaway process is left
on the host, and SHALL surface the timeout as a transport error (`SshError`-class) distinct from the
command's own non-zero exit.

#### Scenario: A command exceeding its timeout is killed and raises a transport error

- **WHEN** a streamed command runs longer than its timeout
- **THEN** the local SSH client is terminated, the remote process is killed (no orphan left on the host),
  and a transport error is raised — not a command-exit result

### Requirement: A directory is pushed recursively without shipping out-of-tree symlink targets

The system SHALL push a local directory tree to a remote destination over SSH, preserving symlinks that
point **inside** the tree but **not** following symlinks that point **outside** the tree — so a source
tree can never cause a file outside the tree to be shipped. The transfer SHALL use the same recycled-IP
SSH hardening (discarded known-hosts, accept-new host keys, batch mode, bounded connect timeout) as the
other SSH transports.

#### Scenario: A directory tree is transferred

- **WHEN** a directory is pushed to a remote destination
- **THEN** its files and its in-tree symlinks are transferred to the destination on the host

#### Scenario: An out-of-tree symlink target is not shipped

- **WHEN** the pushed tree contains a symlink whose target is outside the tree
- **THEN** that target is not shipped — the out-of-tree file does not leave the source host

