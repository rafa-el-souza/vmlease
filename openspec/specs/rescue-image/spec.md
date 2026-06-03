# rescue-image Specification

## Purpose
The injected per-distro rescue-image seam: how a rescue-write distro's disk image is **resolved** (resolve-latest from a mirror, a fixed pinned URL, or a local qcow2), **trust-gated** (SHA256 always; plus a detached signature against a pinned key when the spec defines one — the gate runs operator-side, before any host mutation), and **delivered** to the rescue system (a remote URL fetched on the rescue side, or a local file pushed over SSH), with a rescue-side digest re-check before the write. Decouples the image's origin/trust/delivery from the rescue-write spine; the two instances are the resolve-latest+signature path (Arch) and the fixed-source sha-only path (a pinned golden image).
## Requirements
### Requirement: A rescue image is obtained and trust-gated via an injected per-distro spec

The system SHALL obtain a rescue-write distro's image through an injected image spec that resolves the
image to a content digest and a source, and verifies it operator-side before any host mutation — so the
image's **origin** (resolve-latest, a fixed URL, or a local file) and its **trust gate** (digest only,
or digest **plus** a pinned-key signature) are per-distro, not hardcoded. Verification SHALL be a
load-bearing gate: an image that fails it is never written.

#### Scenario: A resolve-latest, signature-verified image

- **WHEN** a distro's image spec resolves the latest image and verifies its SHA256 plus a detached
  signature against a pinned key
- **THEN** it yields a verified image only when both checks pass, with a remote-URL source

#### Scenario: A fixed-source, digest-only image (a pinned golden image)

- **WHEN** a distro's image spec names a fixed URL **or** a local file plus a pinned SHA256, with no
  signature
- **THEN** it yields a verified image when the digest matches, with the corresponding remote-URL or
  local-file source

#### Scenario: A digest mismatch refuses the image

- **WHEN** the resolved image's bytes do not match the pinned/declared SHA256
- **THEN** the spec refuses it and nothing is written

#### Scenario: A pinned primary key accepts a subkey-signed image

- **WHEN** a signature-verified spec pins a primary-key fingerprint and the image is signed by that
  key's signing subkey (gpg's VALIDSIG line carries the subkey fingerprint first and the primary last)
- **THEN** verification matches the pinned fingerprint in either position and accepts the signature
  (matching only the subkey position would wrongly reject every real signature)

#### Scenario: The trust gate precedes any host mutation

- **WHEN** image verification fails
- **THEN** it fails before `enable-rescue` or any destructive provider call, so nothing runs against an
  untrusted image

### Requirement: The verified image is delivered to the rescue system by its source kind, then re-verified there

The system SHALL deliver the verified image to the rescue system according to its source: a **remote-URL**
source SHALL be fetched on the rescue side; a **local-file** source SHALL be pushed to the rescue system
over SSH as root using the registered rescue key. In **both** cases the system SHALL re-verify the
SHA256 on the rescue side after transfer, before writing the image to disk.

#### Scenario: A remote-URL image is fetched on the rescue side

- **WHEN** the resolved source is a remote URL
- **THEN** the rescue system fetches it and re-verifies its SHA256 before writing

#### Scenario: A local-file image is pushed to the rescue system

- **WHEN** the resolved source is a local file
- **THEN** the system pushes it to the rescue system over SSH (root, via the registered rescue key) and
  re-verifies its SHA256 on the rescue side before writing

#### Scenario: A rescue-side digest mismatch aborts the write

- **WHEN** the rescue-side SHA256 re-check fails after transfer
- **THEN** the image is not written to disk and the build fails

