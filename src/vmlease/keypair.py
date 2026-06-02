"""Throwaway SSH keypair per run — generate, read the public half, discard.

A fresh ed25519 keypair is generated into a temp dir for each run; its public
half is dropped into the host's cloud-init (authorized_keys), and the private
half is used by the SSH runner. On teardown the whole temp dir is deleted, so
no probe key ever lingers.

``ssh-keygen`` is shelled out via an **injected** runner (the determinism /
mocking seam), so tests drive keypair generation without touching the real
binary or filesystem-coupled key material.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

KeygenRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class KeypairError(RuntimeError):
    """Key generation failed (ssh-keygen non-zero, public half unreadable)."""


@dataclass(frozen=True)
class Keypair:
    """A throwaway keypair: the temp dir, the private key path, the public text."""

    directory: Path
    private_key_path: Path
    public_key: str

    def cleanup(self) -> None:
        """Delete the key material's temp dir (idempotent)."""
        shutil.rmtree(self.directory, ignore_errors=True)


def _default_keygen_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def build_keygen_argv(private_key_path: Path, comment: str) -> list[str]:
    """argv for a non-interactive ed25519 ``ssh-keygen`` (no passphrase)."""
    return [
        "ssh-keygen",
        "-t", "ed25519",
        "-N", "",
        "-C", comment,
        "-f", str(private_key_path),
    ]


def generate_keypair(
    run_id: str,
    *,
    runner: KeygenRunner | None = None,
    base_dir: Path | None = None,
) -> Keypair:
    """Generate a throwaway ed25519 keypair for ``run_id``.

    ``runner`` (injected) and ``base_dir`` (a temp-dir override) are the test
    seams. Raises :class:`KeypairError` on a non-zero ``ssh-keygen`` or an
    unreadable public half.
    """
    run = runner or _default_keygen_runner
    directory = Path(tempfile.mkdtemp(prefix=f"vmlease-{run_id}-", dir=base_dir))
    private_key_path = directory / "id_ed25519"
    public_key_path = directory / "id_ed25519.pub"
    proc = run(build_keygen_argv(private_key_path, f"vmlease-{run_id}"))
    if proc.returncode != 0:
        shutil.rmtree(directory, ignore_errors=True)
        raise KeypairError(f"ssh-keygen failed ({proc.returncode}): {proc.stderr}")
    try:
        public_key = public_key_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise KeypairError(f"public key not readable at {public_key_path}: {exc}") from exc
    if not public_key:
        shutil.rmtree(directory, ignore_errors=True)
        raise KeypairError(f"public key at {public_key_path} is empty")
    return Keypair(directory=directory, private_key_path=private_key_path, public_key=public_key)
