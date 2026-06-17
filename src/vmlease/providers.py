"""Provider seam — the ``Provider`` Protocol + a Hetzner ``hcloud`` impl.

The provider is the only layer that talks to a cloud. It is a typed Protocol so
the runner composes against an interface a ``FakeProvider`` can satisfy in unit
tests — **no test ever touches the network**.

The Hetzner impl wraps the ``hcloud`` CLI via :mod:`subprocess`. It **never
reads or logs the API token**: it relies on the operator's already-active
``hcloud`` context (``hcloud context create …`` done out-of-band), so the token
lives only in ``~/.config/hcloud/cli.toml`` and is consumed implicitly. This
module builds argv lists (pure, unit-tested) and shells out; the runner's
``plan`` path makes **zero** provider calls.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vmlease.model import Host, Image

if TYPE_CHECKING:
    from vmlease.model import HostSpec

# A subprocess runner: (argv, timeout) -> completed process (``timeout=None`` =
# unbounded). The injection seam that lets tests drive the Hetzner impl with a
# fake subprocess (a parameter, not module state). The timeout arg lets ``destroy``
# bound its delete so a wedged ``hcloud`` CLI cannot stall teardown forever.
SubprocessRunner = Callable[[list[str], "float | None"], "subprocess.CompletedProcess[str]"]

# Default wall-clock bound (seconds) on a bounded hcloud subprocess (delete,
# power-off, status/describe): generous enough for a real round-trip, short enough
# that a wedged CLI becomes a reap-able failure rather than an indefinite hang.
DEFAULT_OP_TIMEOUT = 120.0


class ProviderError(RuntimeError):
    """A provider operation failed (non-zero CLI exit, unparseable output)."""


class ProviderQuotaError(ProviderError):
    """The provider refused an image create because its snapshot limit is reached.

    The agnostic signal (D1): ``create_image`` translates the provider's
    resource-limit error into this typed subclass so layers above the seam catch
    it generically — the Hetzner ``resource_limit_exceeded`` stderr match stays
    impl-internal and no provider string escapes.
    """


@runtime_checkable
class Provider(Protocol):
    """The cloud seam the runner depends on. Mock this in tests."""

    def create_with_cloudinit(self, spec: HostSpec, cloud_init: str) -> Host:
        """Provision one VM from ``spec`` with ``cloud_init`` user-data; return it once it has an IP."""
        ...

    def destroy(self, host: Host) -> None:
        """Delete one VM. MUST be idempotent (a re-delete is not an error)."""
        ...

    def list_labeled(self, run_id: str) -> list[Host]:
        """Return every live VM carrying this run's ``vmlease=<run-id>`` label."""
        ...

    def create_image(self, server_id: str, description: str, labels: dict[str, str]) -> Image:
        """Snapshot ``server_id`` into an :class:`Image`; ``labels`` applied atomically.

        Labels MUST be applied in the create call so a CLI-timeout orphan is
        still labelled and reap-findable. Raises :class:`ProviderQuotaError` when
        the provider's snapshot limit is reached, :class:`ProviderError` on any
        other failure.
        """
        ...

    def list_images(self, selector: str) -> list[Image]:
        """Return every snapshot :class:`Image` matching ``selector`` (a label query)."""
        ...

    def delete_image(self, image_id: str) -> None:
        """Delete one image. MUST be idempotent (a not-found delete is success)."""
        ...

    def power_off(self, server_id: str) -> None:
        """Power a server off. MUST be idempotent (an already-off host is success)."""
        ...

    def server_status(self, server_id: str) -> str:
        """Return a server's power status (e.g. ``"running"`` / ``"off"``).

        The wait-for-off seam (D6/G2): ``build-image`` polls this after
        ``power_off`` until the host reaches ``"off"`` before snapshotting, so a
        running host is never captured. Raises :class:`ProviderError` on failure.
        """
        ...

    def server_type_disk(self, server_type: str) -> float:
        """Return a server type's primary disk size in GB.

        The restore disk-bound source (D9/G9): the ``run`` cache path needs the
        target server's disk to reject an oversized snapshot. It is a provider
        query (no hardcoded disk map), so a new server type works without a code
        change. Raises :class:`ProviderError` on failure.
        """
        ...


# --------------------------------------------------------------------------- #
# Hetzner argv builders (pure — unit-tested without any subprocess)
# --------------------------------------------------------------------------- #
def _labels_args(labels: dict[str, str]) -> list[str]:
    """Render ``--label k=v`` pairs in a deterministic (sorted) order."""
    out: list[str] = []
    for key in sorted(labels):
        out += ["--label", f"{key}={labels[key]}"]
    return out


def build_create_argv(spec: HostSpec, user_data_path: str) -> list[str]:
    """argv for ``hcloud server create`` from a :class:`HostSpec`.

    Pure: builds the command without running it (the impl runs it). The cloud-init
    user-data is passed via ``--user-data-from-file <path>`` (the impl writes the
    rendered script to a temp file first). No SSH key is registered with the
    provider — the throwaway public key is injected through cloud-init's
    ``authorized_keys`` instead, so nothing lingers in the provider account.
    """
    # NB: `hcloud server create` has NO `--output json` (verified on a real host —
    # it errors `unknown flag: --output`). It prints plain text carrying the id +
    # IPv4, parsed by ``parse_create_text``.
    firewall_args = ["--firewall", spec.firewall] if spec.firewall else []
    return [
        "hcloud",
        "server",
        "create",
        "--name",
        spec.server_name,
        "--image",
        spec.image,
        "--type",
        spec.server_type,
        "--user-data-from-file",
        user_data_path,
        *firewall_args,
        *_labels_args(spec.labels),
    ]


def build_delete_argv(host: Host) -> list[str]:
    """argv for ``hcloud server delete`` by server id."""
    return ["hcloud", "server", "delete", host.id]


def build_list_argv(run_id: str) -> list[str]:
    """argv for ``hcloud server list`` filtered to this run's label."""
    from vmlease.safety import label_selector

    return [
        "hcloud",
        "server",
        "list",
        "--selector",
        label_selector(run_id),
        "--output",
        "json",
    ]


def parse_create_text(stdout: str, name: str, labels: dict[str, str]) -> Host:
    """Parse the plain-text ``hcloud server create`` output into a :class:`Host`.

    ``create`` prints (observed on a real host)::

        Server 12345678 created
        IPv4: 123.0.0.1
        ...

    The id + IPv4 are scraped from that text; ``name`` + ``labels`` are the ones
    the caller passed to create (not echoed in the output). Raises
    :class:`ProviderError` if the id or IPv4 line is absent.
    """
    id_m = re.search(r"Server\s+(\d+)\s+created", stdout)
    ip_m = re.search(r"IPv4:\s*(\S+)", stdout)
    if not id_m or not ip_m:
        raise ProviderError(f"could not parse id/IPv4 from create output: {stdout[:200]!r}")
    return Host(id=id_m.group(1), name=name, ipv4=ip_m.group(1), labels=dict(labels))


def parse_list_output(stdout: str) -> list[Host]:
    """Parse ``hcloud server list --output json`` into :class:`Host` objects."""
    try:
        doc = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"unparseable list output: {exc}") from exc
    if not isinstance(doc, list):
        raise ProviderError(f"list output is not a JSON array: {stdout[:200]!r}")
    return [_host_from_server(s) for s in doc if isinstance(s, dict)]


def _host_from_server(server: dict[str, object]) -> Host:
    """Extract the ``(id, name, ipv4, labels)`` shape from an hcloud server dict."""
    server_id = server.get("id")
    name = server.get("name")
    if server_id is None or name is None:
        raise ProviderError(f"server object missing id/name: {server!r}")
    public_net = server.get("public_net")
    ipv4 = ""
    if isinstance(public_net, dict):
        ipv4_obj = public_net.get("ipv4")
        if isinstance(ipv4_obj, dict):
            ipv4 = str(ipv4_obj.get("ip") or "")
    raw_labels = server.get("labels")
    labels = {str(k): str(v) for k, v in raw_labels.items()} if isinstance(raw_labels, dict) else {}
    return Host(id=str(server_id), name=str(name), ipv4=ipv4, labels=labels)


# --------------------------------------------------------------------------- #
# Snapshot image argv builders (pure — unit-tested without any subprocess)
# --------------------------------------------------------------------------- #
def build_create_image_argv(server_id: str, description: str, labels: dict[str, str]) -> list[str]:
    """argv for ``hcloud server create-image --type snapshot`` from a live server.

    Pure: builds the command without running it. The labels are applied **in the
    create call** (atomic per D1's error rule): a CLI-timeout orphan image is
    still labelled and so ``reap-images``-findable — only an unlabelled image
    would be a true leak. ``create-image`` has NO ``--output json`` (like
    ``server create``); the impl scrapes the new image id from stdout text, then
    describes it for the full :class:`Image`.
    """
    return [
        "hcloud",
        "server",
        "create-image",
        "--type",
        "snapshot",
        "--description",
        description,
        *_labels_args(labels),
        server_id,
    ]


def build_list_images_argv(selector: str) -> list[str]:
    """argv for ``hcloud image list`` filtered to a label selector, snapshots only."""
    return [
        "hcloud",
        "image",
        "list",
        "--selector",
        selector,
        "--type",
        "snapshot",
        "--output",
        "json",
    ]


def build_describe_image_argv(image_id: str) -> list[str]:
    """argv for ``hcloud image describe <id> --output json`` (the create follow-up)."""
    return ["hcloud", "image", "describe", image_id, "--output", "json"]


def build_delete_image_argv(image_id: str) -> list[str]:
    """argv for ``hcloud image delete`` by image id."""
    return ["hcloud", "image", "delete", image_id]


def build_poweroff_argv(server_id: str) -> list[str]:
    """argv for ``hcloud server poweroff`` by server id."""
    return ["hcloud", "server", "poweroff", server_id]


def build_describe_server_argv(server_id: str) -> list[str]:
    """argv for ``hcloud server describe <id> --output json`` (the wait-for-off poll).

    Distinct from :func:`build_describe_image_argv` — this describes a *server*
    (to read its power ``status``), not an image.
    """
    return ["hcloud", "server", "describe", server_id, "--output", "json"]


def build_describe_server_type_argv(server_type: str) -> list[str]:
    """argv for ``hcloud server-type describe <type> --output json`` (the disk-bound source).

    Distinct from :func:`build_describe_server_argv` — this describes a *server
    type* (a catalog entry, to read its primary ``disk`` GB), not a live server.
    """
    return ["hcloud", "server-type", "describe", server_type, "--output", "json"]


def parse_server_type_disk(stdout: str) -> float:
    """Parse the ``disk`` field (GB) from ``hcloud server-type describe -o json``.

    Mirrors :func:`parse_server_status`: malformed JSON, a non-object document, or
    a missing/non-numeric ``disk`` field raises :class:`ProviderError`.
    """
    try:
        doc = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"unparseable server-type describe output: {exc}") from exc
    if not isinstance(doc, dict):
        raise ProviderError(f"server-type describe output is not a JSON object: {stdout[:200]!r}")
    disk = doc.get("disk")
    if not isinstance(disk, (int, float)):
        raise ProviderError(f"server-type describe output missing numeric disk: {stdout[:200]!r}")
    return float(disk)


def parse_image_list(stdout: str) -> list[Image]:
    """Parse ``hcloud image list --output json`` into :class:`Image` objects.

    Mirrors :func:`parse_list_output`: malformed JSON or a non-array document
    raises :class:`ProviderError`; non-dict array elements are skipped.
    """
    try:
        doc = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"unparseable image list output: {exc}") from exc
    if not isinstance(doc, list):
        raise ProviderError(f"image list output is not a JSON array: {stdout[:200]!r}")
    return [_image_from_dict(i) for i in doc if isinstance(i, dict)]


def parse_image_describe(stdout: str) -> Image:
    """Parse ``hcloud image describe --output json`` (a single object) into an :class:`Image`."""
    try:
        doc = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"unparseable image describe output: {exc}") from exc
    if not isinstance(doc, dict):
        raise ProviderError(f"image describe output is not a JSON object: {stdout[:200]!r}")
    return _image_from_dict(doc)


def parse_server_status(stdout: str) -> str:
    """Parse the ``status`` field from ``hcloud server describe -o json``.

    Mirrors :func:`parse_list_output`: malformed JSON, a non-object document, or a
    missing ``status`` field raises :class:`ProviderError`.
    """
    try:
        doc = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"unparseable server describe output: {exc}") from exc
    if not isinstance(doc, dict):
        raise ProviderError(f"server describe output is not a JSON object: {stdout[:200]!r}")
    status = doc.get("status")
    if not isinstance(status, str):
        raise ProviderError(f"server describe output missing status: {stdout[:200]!r}")
    return status


def _image_from_dict(image: dict[str, object]) -> Image:
    """Extract the ``(id, labels, created, disk_size, arch)`` shape from an hcloud image dict.

    Defensive about field naming/absence: the disk bound prefers ``disk_size``
    (the restore-onto rule, D9) and falls back to ``image_size`` (billable GB);
    ``architecture`` is the arch; both default safely when absent or malformed.
    """
    image_id = image.get("id")
    if image_id is None:
        raise ProviderError(f"image object missing id: {image!r}")
    raw_labels = image.get("labels")
    labels = {str(k): str(v) for k, v in raw_labels.items()} if isinstance(raw_labels, dict) else {}
    created = str(image.get("created") or "")
    size_raw = image.get("disk_size")
    if not isinstance(size_raw, (int, float)):
        size_raw = image.get("image_size")
    disk_size = float(size_raw) if isinstance(size_raw, (int, float)) else 0.0
    arch = str(image.get("architecture") or "")
    return Image(id=str(image_id), created=created, disk_size=disk_size, arch=arch, labels=labels)


# --------------------------------------------------------------------------- #
# The Hetzner implementation (subprocess; exercised via an injected fake runner)
# --------------------------------------------------------------------------- #
class HetznerProvider:
    """``hcloud``-CLI-backed provider. Relies on the active context; no token here.

    ``runner`` is an injected callable (defaulting to a sterile
    :func:`subprocess.run` wrapper) so tests can drive the impl with a fake
    subprocess — the determinism/mocking seam.
    """

    def __init__(
        self,
        runner: SubprocessRunner | None = None,
        *,
        op_timeout: float = DEFAULT_OP_TIMEOUT,
    ) -> None:
        self._run: SubprocessRunner = runner or _default_runner
        self._op_timeout = op_timeout

    def create_with_cloudinit(self, spec: HostSpec, cloud_init: str) -> Host:
        with tempfile.NamedTemporaryFile("w", suffix=".cloudinit", delete=True) as fh:
            fh.write(cloud_init)
            fh.flush()
            proc = self._run(build_create_argv(spec, fh.name), None)
        if proc.returncode != 0:
            raise ProviderError(f"hcloud server create failed ({proc.returncode}): {proc.stderr}")
        return parse_create_text(proc.stdout, spec.name, dict(spec.labels))

    def destroy(self, host: Host, *, attempts: int = 4, sleep: Callable[[float], None] | None = None) -> None:
        """Delete a server, retrying transient API timeouts (hcloud says "please retry").

        Idempotent: a not-found delete (already reaped) is success. A "request
        timeout" / "please retry" is transient — the deletion usually completes
        server-side anyway — so retry with a short backoff rather than fail the
        run. Only a persistent non-timeout error raises :class:`ProviderError`.

        The delete subprocess is wall-clock-bounded (``op_timeout``): a wedged
        ``hcloud`` CLI that never returns is killed and surfaced as a
        :class:`ProviderError` (a failed, reap-able teardown) so it can never stall
        the per-host ``finally`` forever. A *subprocess* timeout is NOT the same as
        the transient API "please retry" — the process is hung, not the API — so it
        is not retried; it fails fast and the orphan is reaped.
        """
        import time

        _sleep = sleep if sleep is not None else time.sleep
        for attempt in range(attempts):
            try:
                proc = self._run(build_delete_argv(host), self._op_timeout)
            except subprocess.TimeoutExpired as exc:
                raise ProviderError(
                    f"hcloud server delete timed out after {self._op_timeout}s "
                    f"(killed): {host.name} ({host.id})"
                ) from exc
            err = (proc.stderr or "").lower()
            if proc.returncode == 0 or "not found" in err:
                return
            transient = "timeout" in err or "please retry" in err or "rate limit" in err
            if not transient or attempt == attempts - 1:
                raise ProviderError(f"hcloud server delete failed ({proc.returncode}): {proc.stderr}")
            _sleep(min(8.0, 2.0 * (attempt + 1)))

    def list_labeled(self, run_id: str) -> list[Host]:
        proc = self._run(build_list_argv(run_id), None)
        if proc.returncode != 0:
            raise ProviderError(f"hcloud server list failed ({proc.returncode}): {proc.stderr}")
        return parse_list_output(proc.stdout)

    def create_image(self, server_id: str, description: str, labels: dict[str, str]) -> Image:
        """Snapshot a server, labels applied atomically; describe the result.

        ``create-image`` has no ``--output json`` (like ``server create``), so the
        new image id is scraped from stdout text and the full :class:`Image` is
        populated by a follow-up ``hcloud image describe <id> -o json``. The
        provider snapshot-limit error is matched on the **code**
        ``resource_limit_exceeded`` in stderr (hcloud-go formats
        ``"<msg> (resource_limit_exceeded)"`` — the human ``<msg>`` varies by
        resource, so we never match it) and raised as :class:`ProviderQuotaError`.
        """
        proc = self._run(build_create_image_argv(server_id, description, labels), None)
        if proc.returncode != 0:
            stderr = proc.stderr or ""
            if "resource_limit_exceeded" in stderr:
                raise ProviderQuotaError(
                    f"hcloud server create-image hit the snapshot limit: {stderr}"
                )
            raise ProviderError(f"hcloud server create-image failed ({proc.returncode}): {stderr}")
        image_id = _scrape_image_id(proc.stdout)
        if image_id is None:
            raise ProviderError(
                f"could not parse image id from create-image output: {proc.stdout[:200]!r}"
            )
        describe = self._run(build_describe_image_argv(image_id), None)
        if describe.returncode != 0:
            raise ProviderError(
                f"hcloud image describe failed ({describe.returncode}): {describe.stderr}"
            )
        return parse_image_describe(describe.stdout)

    def list_images(self, selector: str) -> list[Image]:
        proc = self._run(build_list_images_argv(selector), None)
        if proc.returncode != 0:
            raise ProviderError(f"hcloud image list failed ({proc.returncode}): {proc.stderr}")
        return parse_image_list(proc.stdout)

    def delete_image(self, image_id: str) -> None:
        """Delete an image. Idempotent: a not-found delete (already reaped) is success."""
        proc = self._run(build_delete_image_argv(image_id), self._op_timeout)
        err = (proc.stderr or "").lower()
        if proc.returncode == 0 or "not found" in err:
            return
        raise ProviderError(f"hcloud image delete failed ({proc.returncode}): {proc.stderr}")

    def power_off(self, server_id: str) -> None:
        """Power a server off. Idempotent: an already-off host is success."""
        proc = self._run(build_poweroff_argv(server_id), self._op_timeout)
        err = (proc.stderr or "").lower()
        if proc.returncode == 0 or "already off" in err or "is off" in err or "not found" in err:
            return
        raise ProviderError(f"hcloud server poweroff failed ({proc.returncode}): {proc.stderr}")

    def server_status(self, server_id: str) -> str:
        """Describe a server and return its power ``status``; raise on non-zero exit."""
        proc = self._run(build_describe_server_argv(server_id), self._op_timeout)
        if proc.returncode != 0:
            raise ProviderError(f"hcloud server describe failed ({proc.returncode}): {proc.stderr}")
        return parse_server_status(proc.stdout)

    def server_type_disk(self, server_type: str) -> float:
        """Describe a server type and return its primary disk GB; raise on non-zero exit."""
        proc = self._run(build_describe_server_type_argv(server_type), self._op_timeout)
        if proc.returncode != 0:
            raise ProviderError(f"hcloud server-type describe failed ({proc.returncode}): {proc.stderr}")
        return parse_server_type_disk(proc.stdout)


def _scrape_image_id(stdout: str) -> str | None:
    """Scrape the new image id from ``hcloud server create-image`` plain-text output.

    ``create-image`` prints (observed on a real host)::

        Image 12345678 created from server 9 (...)

    The first standalone run of digits is the image id; ``None`` if absent.
    """
    match = re.search(r"Image\s+(\d+)\s+created", stdout)
    if match:
        return match.group(1)
    fallback = re.search(r"\b(\d+)\b", stdout)
    return fallback.group(1) if fallback else None


def _default_runner(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Sterile subprocess wrapper: capture text, never raise on non-zero.

    ``timeout=None`` (the default for create/list) is unbounded. When a bound is
    given (``destroy``), ``subprocess.run`` kills the child and re-raises
    :class:`subprocess.TimeoutExpired` on expiry — the caller turns that into a
    reap-able teardown failure.
    """
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)
