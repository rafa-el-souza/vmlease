#!/usr/bin/env python3
"""Unit tests for vmlease — all mocked, NO network, NO real VMs.

stdlib unittest only. Run with:
    uv run python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tests.battery_helpers import battery_toml

# vmlease is a real package, so import it directly — this keeps mypy --strict
# name-resolution working for the typed fakes below (importlib.import_module
# would erase the symbol types).
from vmlease import (
    archbuild,
    archimage,
    assertions,
    capabilities,
    cli,
    cloudinit,
    distro,
    imagecache,
    keypair,
    model,
    providers,
    rescue_image,
    results,
    runner,
    safety,
    ssh,
    templating,
    workload,
)
from vmlease import battery as battery_mod
from vmlease import shellcheck as shellcheck_mod
from vmlease.model import Assertion, Host, HostSpec, Image, Probe, ProbeResult, ProbeTag


def make_probe_result(
    probe_id: str,
    tag: ProbeTag,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
    *,
    ok: bool | None = None,
    timed_out: bool = False,
    assertion_failures: tuple[str, ...] = (),
    has_assertions: bool = False,
) -> ProbeResult:
    """Construct a :class:`ProbeResult` with a defaulted ``ok`` for tests.

    ``ok`` is now a REQUIRED stored field (the runner computes it). Test
    constructions that don't care about the verdict default it to
    ``exit_code == 0``; pass ``ok=`` to override (e.g. a token-derived pass on a
    non-zero exit). This bounds the churn from the field becoming stored.
    """
    return ProbeResult(
        probe_id=probe_id,
        tag=tag,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        ok=(exit_code == 0) if ok is None else ok,
        timed_out=timed_out,
        assertion_failures=assertion_failures,
        has_assertions=has_assertions,
    )


# --------------------------------------------------------------------------- #
# Fakes (the mock seams — no network ever)
# --------------------------------------------------------------------------- #
class FakeProvider:
    """In-memory Provider: records create/destroy/list calls (thread-safe)."""

    def __init__(self) -> None:
        self.created: list[HostSpec] = []
        self.cloud_inits: list[str] = []
        self.destroyed: list[Host] = []
        self._live: dict[str, Host] = {}
        self._lock = threading.Lock()
        # snapshot ops (downstream milestones drive the cache through these):
        self.images: dict[str, Image] = {}
        self.created_images: list[tuple[str, str, dict[str, str]]] = []
        self.deleted_images: list[str] = []
        self.powered_off: list[str] = []
        self._image_seq = 0
        # per-server power state (default "running"; power_off flips it to "off").
        self._power: dict[str, str] = {}
        # per-server-type primary disk GB (default 40.0; set entries to override).
        self.server_type_disks: dict[str, float] = {}
        self.default_server_type_disk: float = 40.0

    def create_with_cloudinit(self, spec: HostSpec, cloud_init: str) -> Host:
        # each host gets a DISTINCT ip so a parallel run mirrors reality.
        with self._lock:
            host = Host(id=f"id-{spec.name}", name=spec.name, ipv4=f"10.0.0.{len(self.created) + 1}", labels=dict(spec.labels))
            self.created.append(spec)
            self.cloud_inits.append(cloud_init)
            self._live[host.id] = host
        return host

    def destroy(self, host: Host) -> None:
        with self._lock:
            self.destroyed.append(host)
            self._live.pop(host.id, None)

    def list_labeled(self, run_id: str) -> list[Host]:
        sel = f"{safety.LABEL_KEY}={run_id}"
        return [h for h in self._live.values() if f"{safety.LABEL_KEY}={h.labels.get(safety.LABEL_KEY)}" == sel]

    def create_image(self, server_id: str, description: str, labels: dict[str, str]) -> Image:
        # labels applied atomically (mirrors the real provider): the returned
        # Image carries exactly the labels the caller passed, indexed in-memory.
        with self._lock:
            self._image_seq += 1
            image_id = f"img-{self._image_seq}"
            image = Image(
                id=image_id,
                created="2024-01-01T00:00:00+00:00",
                disk_size=40.0,  # a plausible default builder disk (GB)
                arch="x86",
                labels=dict(labels),
            )
            self.images[image_id] = image
            self.created_images.append((server_id, description, dict(labels)))
        return image

    def list_images(self, selector: str) -> list[Image]:
        # selector is "k=v"; match images whose label k equals v (the query index).
        with self._lock:
            if "=" not in selector:
                return list(self.images.values())
            key, _, value = selector.partition("=")
            return [img for img in self.images.values() if img.labels.get(key) == value]

    def delete_image(self, image_id: str) -> None:
        # idempotent: deleting an absent image is success (concurrent prune/reap race).
        with self._lock:
            self.deleted_images.append(image_id)
            self.images.pop(image_id, None)

    def power_off(self, server_id: str) -> None:
        # idempotent: powering off an already-off (or absent) host is success.
        with self._lock:
            self.powered_off.append(server_id)
            self._power[server_id] = "off"

    def server_status(self, server_id: str) -> str:
        # default "running"; power_off flips it to "off".
        with self._lock:
            return self._power.get(server_id, "running")

    def server_type_disk(self, server_type: str) -> float:
        # a settable per-type disk; defaults to default_server_type_disk.
        with self._lock:
            return self.server_type_disks.get(server_type, self.default_server_type_disk)


def _fake_subprocess(
    returncode: int, stdout: str = "", stderr: str = ""
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return _run


def _fake_provider_runner(
    returncode: int, stdout: str = "", stderr: str = ""
) -> providers.SubprocessRunner:
    """A 2-arg provider subprocess seam (argv, timeout) -> CompletedProcess."""

    def _run(argv: list[str], _timeout: float | None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return _run


class FakeSshRunner:
    """Scripted SshRunner: returns exit 0 by default, or raises/fails per config."""

    def __init__(
        self, *, fail_on: str | None = None, raise_on: str | None = None, raise_upload: bool = False
    ) -> None:
        self.ran: list[str] = []  # ordered call log: "upload:<remote>" then probe ids
        self.uploads: list[tuple[Path, str]] = []
        self._fail_on = fail_on
        self._raise_on = raise_on
        self._raise_upload = raise_upload

    def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
        self.ran.append(probe.id)
        if self._raise_on is not None and probe.id == self._raise_on:
            raise ssh.SshError(f"boom on {probe.id}")
        code = 7 if probe.id == self._fail_on else 0
        return make_probe_result(probe.id, probe.tag, code, stdout=f"out-{probe.id}")

    def upload(self, host: Host, local: Path, remote: str) -> None:
        self.ran.append(f"upload:{remote}")
        self.uploads.append((local, remote))
        if self._raise_upload:
            raise ssh.SshError(f"upload of {local} failed")

    def wait_until_ready(self, host: Host) -> None:
        # no-op: the fake host is always "ready" (no readiness logged, so the
        # call log stays upload-then-detail like the live readiness gate).
        return None

    def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
        self.ran.append(f"stream:{command}")
        return 0

    def upload_dir(self, host: Host, local: Path, remote: str) -> None:
        self.ran.append(f"upload_dir:{remote}")


def _demo_workload() -> workload.ProbeWorkload:
    """The probe battery as a workload — the injected default for runner tests."""
    return workload.ProbeWorkload(_resolve_toml(_DEMO_BATTERY))


def _fake_keypair(tmp: Path) -> keypair.Keypair:
    d = tmp / "kp"
    d.mkdir(parents=True, exist_ok=True)
    priv = d / "id_ed25519"
    priv.write_text("PRIV", encoding="utf-8")
    return keypair.Keypair(directory=d, private_key_path=priv, public_key="ssh-ed25519 AAAA probe")


# --------------------------------------------------------------------------- #
# TOML battery fixtures — ``battery_toml`` lives in ``battery_helpers`` (shared)
# --------------------------------------------------------------------------- #
def _resolve_toml(manifest: str, scripts: dict[str, str] | None = None) -> model.Battery:
    """Resolve a manifest (plus optional ``{path: contents}`` scripts) to a Battery.

    Writes the bundle to a throwaway temp dir and loads it through the real
    loader, so fixtures exercise the TOML + script-resolution path end-to-end.
    """
    d = Path(tempfile.mkdtemp())
    for rel, contents in (scripts or {}).items():
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
    p = d / "battery.toml"
    p.write_text(manifest, encoding="utf-8")
    return battery_mod.load_battery(p)


def _write_battery_bundle(
    d: str, manifest: str, scripts: dict[str, str] | None = None
) -> str:
    """Write a ``battery.toml`` (+ optional script files) into ``d``; return its path."""
    base = Path(d)
    for rel, contents in (scripts or {}).items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
    p = base / "battery.toml"
    p.write_text(manifest, encoding="utf-8")
    return str(p)


_DEMO_BATTERY = battery_toml(
    "demo-battery",
    (
        {"id": "P1", "title": "subid", "run": "grep x /etc/subuid", "tag": "read-only", "classifies": "L2"},
        {"id": "P6", "title": "linger", "run": "loginctl enable-linger", "tag": "mutating:operator-space"},
        {"id": "P12", "title": "batch", "run": "sudo true", "tag": "mutating:host-root"},
    ),
)


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
class TestModel(unittest.TestCase):
    def test_probe_result_ok_is_a_stored_field(self) -> None:
        # ``ok`` is now a runner-computed STORED verdict — the model stores it
        # verbatim and applies no reading of its own (computation moved to
        # ``run_probe``; see TestSsh for the verdict-rule coverage).
        ok = model.ProbeResult("P1", model.ProbeTag.READ_ONLY, 3, "out", "", ok=True)
        bad = model.ProbeResult("P1", model.ProbeTag.READ_ONLY, 0, "out", "", ok=False)
        self.assertTrue(ok.ok)
        self.assertFalse(bad.ok)

    def test_probe_result_assertion_failures_defaults_empty(self) -> None:
        res = make_probe_result("P1", model.ProbeTag.READ_ONLY, 0)
        self.assertEqual(res.assertion_failures, ())

    def test_probe_result_timed_out_defaults_false(self) -> None:
        res = make_probe_result("P1", model.ProbeTag.READ_ONLY, 0, "out")
        self.assertFalse(res.timed_out)

    def test_model_imports_no_regex_engine_or_assertions(self) -> None:
        # M1-gate / D10(B): importing ``vmlease.model`` must NOT transitively pull
        # in the regex backend (``re2``) or ``vmlease.assertions``. A fresh
        # subprocess proves no transitive runtime import (the verdict arrives
        # already computed; the engine lives only in ``assertions``/``ssh``).
        code = (
            "import vmlease.model, sys; "
            "print('re2' in sys.modules); "
            "print('vmlease.assertions' in sys.modules)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        self.assertEqual(proc.stdout.split(), ["False", "False"])

    def test_probe_timeout_defaults_none(self) -> None:
        p = model.Probe(id="P1", title="t", command="c", tag=model.ProbeTag.READ_ONLY)
        self.assertIsNone(p.timeout)

    def test_probe_assertions_defaults_empty(self) -> None:
        p = model.Probe(id="P1", title="t", command="c", tag=model.ProbeTag.READ_ONLY)
        self.assertEqual(p.assertions, ())

    def test_probe_source_defaults_empty(self) -> None:
        p = model.Probe(id="P1", title="t", command="c", tag=model.ProbeTag.READ_ONLY)
        self.assertEqual(p.source, "")

    def test_probe_source_carries_provenance(self) -> None:
        p = model.Probe(id="P1", title="t", command="c", tag=model.ProbeTag.READ_ONLY, source="prep.sh")
        self.assertEqual(p.source, "prep.sh")

    def test_battery_probes_preserve_authoring_order(self) -> None:
        # probes run + record in authoring order regardless of tag; a read-only
        # probe authored AFTER a host-root probe stays after it (no tag-rank sort).
        manifest = battery_toml("x", (
            {"id": "SETUP", "title": "setup", "run": "c", "tag": "mutating:host-root"},
            {"id": "VERIFY", "title": "verify", "run": "c", "tag": "read-only"},
        ))
        b = _resolve_toml(manifest)
        self.assertEqual([p.id for p in b.probes], ["SETUP", "VERIFY"])


# --------------------------------------------------------------------------- #
# safety
# --------------------------------------------------------------------------- #
class TestSafety(unittest.TestCase):
    def test_make_run_id_normalizes(self) -> None:
        self.assertEqual(safety.make_run_id("2026-06-01 OpRun!"), "2026-06-01-oprun")

    def test_make_run_id_deterministic(self) -> None:
        self.assertEqual(safety.make_run_id("abc-run"), safety.make_run_id("abc-run"))

    def test_make_run_id_rejects_too_short(self) -> None:
        with self.assertRaises(ValueError):
            safety.make_run_id("a")

    def test_make_run_id_rejects_empty_after_norm(self) -> None:
        with self.assertRaises(ValueError):
            safety.make_run_id("___")

    def test_run_label_and_selector(self) -> None:
        self.assertEqual(safety.run_label("r1"), {"vmlease": "r1"})
        self.assertEqual(safety.label_selector("r1"), "vmlease=r1")

    def test_cost_guard_passes_within_bounds(self) -> None:
        safety.CostGuard().check(["cpx22", "cpx22"])  # no raise

    def test_cost_guard_caps_host_count(self) -> None:
        g = safety.CostGuard(max_hosts=2)
        with self.assertRaises(safety.CostGuardError):
            g.check(["cpx22", "cpx22", "cpx22"])

    def test_cost_guard_rejects_non_allowlisted_type(self) -> None:
        with self.assertRaises(safety.CostGuardError):
            safety.CostGuard().check(["cpx22", "ccx63"])

    def test_image_quota_guard_passes_under_cap(self) -> None:
        # Default cap is 10; nine images leaves headroom for one more.
        safety.ImageQuotaGuard().check(9)  # no raise

    def test_image_quota_guard_at_cap_raises(self) -> None:
        # At the cap there is no headroom to create one more — refuse.
        with self.assertRaises(safety.ImageQuotaError):
            safety.ImageQuotaGuard().check(safety.DEFAULT_MAX_IMAGES)

    def test_image_quota_guard_over_cap_raises(self) -> None:
        with self.assertRaises(safety.ImageQuotaError):
            safety.ImageQuotaGuard().check(safety.DEFAULT_MAX_IMAGES + 5)

    def test_image_quota_guard_honors_custom_max(self) -> None:
        g = safety.ImageQuotaGuard(max_images=2)
        g.check(1)  # one image, headroom for one more — passes
        with self.assertRaises(safety.ImageQuotaError):
            g.check(2)  # at the custom cap — refuse

    def test_image_quota_error_message_is_operator_actionable(self) -> None:
        with self.assertRaises(safety.ImageQuotaError) as ctx:
            safety.ImageQuotaGuard(max_images=3).check(3)
        msg = str(ctx.exception)
        self.assertIn("reap-images", msg)
        self.assertIn("--max-images", msg)
        self.assertIn("3", msg)


# --------------------------------------------------------------------------- #
# providers — argv builders + parsers (pure) + impl via injected runner
# --------------------------------------------------------------------------- #
class TestProviderArgv(unittest.TestCase):
    def _spec(self) -> model.HostSpec:
        return model.HostSpec(
            name="vmlease-r1-ubuntu", image="ubuntu-24.04", server_type="cpx22",
            distro_key="ubuntu", labels={"vmlease": "r1", "a": "b"},
        )

    def test_create_argv_firewall_present_and_absent(self) -> None:
        # no firewall on the spec -> no --firewall in argv
        self.assertNotIn("--firewall", providers.build_create_argv(self._spec(), "/tmp/c"))
        # firewall set -> --firewall <name> present
        fw_spec = model.HostSpec(
            name="n", image="i", server_type="cpx22", distro_key="ubuntu",
            labels={"vmlease": "r1"}, firewall="my-firewall",
        )
        argv = providers.build_create_argv(fw_spec, "/tmp/c")
        self.assertIn("--firewall", argv)
        self.assertEqual(argv[argv.index("--firewall") + 1], "my-firewall")

    def test_create_argv_sorted_labels(self) -> None:
        argv = providers.build_create_argv(self._spec(), "/tmp/cloud.init")
        self.assertEqual(argv[:3], ["hcloud", "server", "create"])
        # labels rendered in sorted key order: a=b appears before vmlease=r1
        self.assertLess(argv.index("a=b"), argv.index("vmlease=r1"))
        self.assertIn("--user-data-from-file", argv)
        self.assertEqual(argv[argv.index("--user-data-from-file") + 1], "/tmp/cloud.init")

    def test_delete_and_list_argv(self) -> None:
        host = model.Host(id="42", name="n", ipv4="1.2.3.4")
        self.assertEqual(providers.build_delete_argv(host), ["hcloud", "server", "delete", "42"])
        self.assertIn("vmlease=r1", providers.build_list_argv("r1"))

    def test_parse_create_text(self) -> None:
        # the plain-text shape `hcloud server create` actually prints (observed on a real host)
        out = "Waiting for server ... done\nServer 12345678 created\nIPv4: 123.0.0.1\nIPv6: ...\n"
        host = providers.parse_create_text(out, "vmlease-r1-arch", {"vmlease": "r1"})
        self.assertEqual((host.id, host.ipv4, host.name), ("12345678", "123.0.0.1", "vmlease-r1-arch"))
        self.assertEqual(host.labels, {"vmlease": "r1"})

    def test_parse_create_text_missing_id(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_create_text("IPv4: 1.2.3.4\n", "n", {})

    def test_parse_create_text_missing_ipv4(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_create_text("Server 5 created\n", "n", {})

    def test_parse_list_output(self) -> None:
        out = json.dumps([{"id": 1, "name": "a"}, {"id": 2, "name": "b", "public_net": {"ipv4": {"ip": "5.5.5.5"}}}])
        hosts = providers.parse_list_output(out)
        self.assertEqual([h.id for h in hosts], ["1", "2"])
        self.assertEqual(hosts[1].ipv4, "5.5.5.5")

    def test_parse_list_output_not_array(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_list_output(json.dumps({"not": "a list"}))

    def test_parse_list_output_skips_non_dict_elements(self) -> None:
        out = json.dumps([{"id": 1, "name": "a"}, "garbage", 7])
        hosts = providers.parse_list_output(out)
        self.assertEqual([h.id for h in hosts], ["1"])

    def test_host_from_server_no_ipv4(self) -> None:
        # public_net absent/malformed -> ipv4 falls back to "" (via the list path,
        # which still uses --output json + _host_from_server)
        hosts = providers.parse_list_output(json.dumps([{"id": 3, "name": "n"}]))
        self.assertEqual(hosts[0].ipv4, "")

    def test_parse_list_output_bad_json(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_list_output("{not json")

    # --- snapshot image argv builders (pure) --------------------------------
    def test_build_create_image_argv_shape_and_atomic_labels(self) -> None:
        argv = providers.build_create_image_argv("99", "v1-arch-snap", {"vmlease-cache-key": "k", "a": "b"})
        self.assertEqual(argv[:5], ["hcloud", "server", "create-image", "--type", "snapshot"])
        self.assertEqual(argv[argv.index("--description") + 1], "v1-arch-snap")
        # labels in the create call (atomic), sorted, server id last
        self.assertLess(argv.index("a=b"), argv.index("vmlease-cache-key=k"))
        self.assertEqual(argv[-1], "99")
        self.assertIn("--label", argv)

    def test_build_list_images_argv_snapshot_json(self) -> None:
        argv = providers.build_list_images_argv("vmlease-purpose=image-cache")
        self.assertEqual(
            argv,
            ["hcloud", "image", "list", "--selector", "vmlease-purpose=image-cache",
             "--type", "snapshot", "--output", "json"],
        )

    def test_build_describe_delete_poweroff_argv(self) -> None:
        self.assertEqual(providers.build_describe_image_argv("7"), ["hcloud", "image", "describe", "7", "--output", "json"])
        self.assertEqual(providers.build_delete_image_argv("7"), ["hcloud", "image", "delete", "7"])
        self.assertEqual(providers.build_poweroff_argv("7"), ["hcloud", "server", "poweroff", "7"])

    def test_build_describe_server_argv(self) -> None:
        self.assertEqual(
            providers.build_describe_server_argv("7"),
            ["hcloud", "server", "describe", "7", "--output", "json"],
        )

    def test_parse_server_status(self) -> None:
        self.assertEqual(providers.parse_server_status(json.dumps({"status": "off"})), "off")
        self.assertEqual(providers.parse_server_status(json.dumps({"status": "running"})), "running")

    def test_parse_server_status_bad_json_raises(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_server_status("{not json")

    def test_parse_server_status_not_object_raises(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_server_status(json.dumps([1, 2]))

    def test_parse_server_status_missing_status_raises(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_server_status(json.dumps({"id": 9}))

    def test_build_describe_server_type_argv(self) -> None:
        self.assertEqual(
            providers.build_describe_server_type_argv("cpx22"),
            ["hcloud", "server-type", "describe", "cpx22", "--output", "json"],
        )

    def test_parse_server_type_disk(self) -> None:
        self.assertEqual(providers.parse_server_type_disk(json.dumps({"disk": 80})), 80.0)
        self.assertEqual(providers.parse_server_type_disk(json.dumps({"disk": 40.5})), 40.5)

    def test_parse_server_type_disk_bad_json_raises(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_server_type_disk("{not json")

    def test_parse_server_type_disk_not_object_raises(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_server_type_disk(json.dumps([1, 2]))

    def test_parse_server_type_disk_missing_disk_raises(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_server_type_disk(json.dumps({"name": "cpx22"}))

    def test_parse_image_list(self) -> None:
        out = json.dumps([
            {"id": 11, "labels": {"vmlease-cache-key": "k1"}, "created": "2024-04-25T13:26:27+00:00",
             "disk_size": 40, "architecture": "x86"},
            "garbage",
            {"id": 12, "labels": {}, "created": "2024-05-01T00:00:00+00:00", "image_size": 1.5, "architecture": "arm"},
        ])
        images = providers.parse_image_list(out)
        self.assertEqual([i.id for i in images], ["11", "12"])
        self.assertEqual(images[0].created, "2024-04-25T13:26:27+00:00")
        self.assertEqual(images[0].disk_size, 40.0)
        self.assertEqual(images[0].arch, "x86")
        self.assertEqual(images[0].labels, {"vmlease-cache-key": "k1"})
        # disk_size falls back to image_size when disk_size is absent
        self.assertEqual(images[1].disk_size, 1.5)
        self.assertEqual(images[1].arch, "arm")

    def test_parse_image_list_bad_json_raises(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_image_list("{not json")

    def test_parse_image_list_not_array_raises(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_image_list(json.dumps({"id": 1}))

    def test_parse_image_describe(self) -> None:
        out = json.dumps({"id": 5, "labels": {"a": "b"}, "created": "2024-01-02T03:04:05+00:00", "disk_size": 80, "architecture": "x86"})
        img = providers.parse_image_describe(out)
        self.assertEqual((img.id, img.disk_size, img.arch), ("5", 80.0, "x86"))

    def test_parse_image_describe_not_object_raises(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_image_describe(json.dumps([1, 2]))

    def test_parse_image_describe_bad_json_raises(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_image_describe("{not json")

    def test_image_from_dict_missing_id_raises(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_image_list(json.dumps([{"created": "2024-01-01T00:00:00+00:00"}]))

    def test_image_defaults_when_fields_absent(self) -> None:
        img = providers.parse_image_list(json.dumps([{"id": 9}]))[0]
        self.assertEqual((img.created, img.disk_size, img.arch, img.labels), ("", 0.0, "", {}))


class TestHetznerProviderImpl(unittest.TestCase):
    def _spec(self) -> model.HostSpec:
        return model.HostSpec(name="n", image="ubuntu-24.04", server_type="cpx22", distro_key="ubuntu", labels={"vmlease": "r1"})

    def test_create_success(self) -> None:
        out = "Server 5 created\nIPv4: 1.1.1.1\n"
        prov = providers.HetznerProvider(runner=_fake_provider_runner(0, out))
        host = prov.create_with_cloudinit(self._spec(), "#!/bin/bash\necho hi")
        self.assertEqual((host.id, host.ipv4), ("5", "1.1.1.1"))

    def test_create_failure_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "boom"))
        with self.assertRaises(providers.ProviderError):
            prov.create_with_cloudinit(self._spec(), "#!/bin/bash")

    def test_destroy_idempotent_on_not_found(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "server not found"))
        prov.destroy(model.Host(id="9", name="n", ipv4=""), sleep=lambda _s: None)  # no raise

    def test_destroy_non_transient_error_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "forbidden"))
        with self.assertRaises(providers.ProviderError):
            prov.destroy(model.Host(id="9", name="n", ipv4=""), sleep=lambda _s: None)

    def test_destroy_retries_transient_timeout_then_succeeds(self) -> None:
        # first call times out (transient), second succeeds → no raise, 2 calls
        calls = {"n": 0}

        def flaky(argv: list[str], _timeout: float | None) -> subprocess.CompletedProcess[str]:
            calls["n"] += 1
            if calls["n"] == 1:
                return subprocess.CompletedProcess(argv, 1, "", "request timeout, please retry")
            return subprocess.CompletedProcess(argv, 0, "", "")

        prov = providers.HetznerProvider(runner=flaky)
        prov.destroy(model.Host(id="9", name="n", ipv4=""), sleep=lambda _s: None)
        self.assertEqual(calls["n"], 2)

    def test_destroy_persistent_timeout_eventually_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "request timeout, please retry"))
        with self.assertRaises(providers.ProviderError):
            prov.destroy(model.Host(id="9", name="n", ipv4=""), attempts=3, sleep=lambda _s: None)

    def test_destroy_passes_op_timeout_to_seam(self) -> None:
        # the delete subprocess is bounded — the ctor's op_timeout reaches the seam
        seen: list[float | None] = []

        def capture(argv: list[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
            seen.append(timeout)
            return subprocess.CompletedProcess(argv, 0, "", "")

        prov = providers.HetznerProvider(runner=capture, op_timeout=37.5)
        prov.destroy(model.Host(id="9", name="n", ipv4=""), sleep=lambda _s: None)
        self.assertEqual(seen, [37.5])

    def test_destroy_default_op_timeout_is_the_module_default(self) -> None:
        seen: list[float | None] = []

        def capture(argv: list[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
            seen.append(timeout)
            return subprocess.CompletedProcess(argv, 0, "", "")

        providers.HetznerProvider(runner=capture).destroy(
            model.Host(id="9", name="n", ipv4=""), sleep=lambda _s: None
        )
        self.assertEqual(seen, [providers.DEFAULT_OP_TIMEOUT])

    def test_destroy_wedged_subprocess_killed_surfaces_as_provider_error(self) -> None:
        # a delete that never returns (TimeoutExpired) is killed and surfaces as a
        # ProviderError (reap-able teardown failure), NOT a hang, and is NOT retried.
        calls = {"n": 0}

        def wedged(argv: list[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
            calls["n"] += 1
            raise subprocess.TimeoutExpired(argv, timeout or 0.0)

        prov = providers.HetznerProvider(runner=wedged)
        with self.assertRaises(providers.ProviderError):
            prov.destroy(model.Host(id="9", name="n", ipv4=""), attempts=4, sleep=lambda _s: None)
        self.assertEqual(calls["n"], 1)  # timeout is fail-fast, not retried

    def test_list_labeled(self) -> None:
        out = json.dumps([{"id": 1, "name": "a", "labels": {"vmlease": "r1"}}])
        prov = providers.HetznerProvider(runner=_fake_provider_runner(0, out))
        self.assertEqual(len(prov.list_labeled("r1")), 1)

    def test_list_labeled_failure_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "boom"))
        with self.assertRaises(providers.ProviderError):
            prov.list_labeled("r1")

    def test_provider_protocol_satisfied(self) -> None:
        self.assertIsInstance(FakeProvider(), providers.Provider)

    # --- snapshot image ops -------------------------------------------------
    def test_create_image_scrapes_id_then_describes(self) -> None:
        describe = json.dumps({"id": 42, "labels": {"vmlease-cache-key": "k"}, "created": "2024-04-25T13:26:27+00:00", "disk_size": 40, "architecture": "x86"})
        seen: list[list[str]] = []

        def scripted(argv: list[str], _timeout: float | None) -> subprocess.CompletedProcess[str]:
            seen.append(argv)
            if argv[1:3] == ["server", "create-image"]:
                return subprocess.CompletedProcess(argv, 0, "Image 42 created from server 9\n", "")
            return subprocess.CompletedProcess(argv, 0, describe, "")

        prov = providers.HetznerProvider(runner=scripted)
        img = prov.create_image("9", "v1-snap", {"vmlease-cache-key": "k"})
        self.assertEqual((img.id, img.disk_size, img.arch), ("42", 40.0, "x86"))
        # labels were applied in the create-image call (atomic), and a describe followed
        self.assertIn("--label", seen[0])
        self.assertEqual(seen[1][:4], ["hcloud", "image", "describe", "42"])

    def test_create_image_quota_error_matches_code_not_message(self) -> None:
        # the human message varies; the CODE resource_limit_exceeded is what's matched
        stderr = "primary IP limit reached (resource_limit_exceeded, abc-123)"
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", stderr))
        with self.assertRaises(providers.ProviderQuotaError):
            prov.create_image("9", "v1-snap", {})

    def test_create_image_quota_error_is_provider_error_subclass(self) -> None:
        self.assertTrue(issubclass(providers.ProviderQuotaError, providers.ProviderError))

    def test_create_image_other_failure_raises_plain_provider_error(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "forbidden"))
        with self.assertRaises(providers.ProviderError) as ctx:
            prov.create_image("9", "v1-snap", {})
        self.assertNotIsInstance(ctx.exception, providers.ProviderQuotaError)

    def test_create_image_unparseable_id_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(0, "no digits here\n"))
        with self.assertRaises(providers.ProviderError):
            prov.create_image("9", "v1-snap", {})

    def test_create_image_describe_failure_raises(self) -> None:
        def scripted(argv: list[str], _timeout: float | None) -> subprocess.CompletedProcess[str]:
            if argv[1:3] == ["server", "create-image"]:
                return subprocess.CompletedProcess(argv, 0, "Image 42 created\n", "")
            return subprocess.CompletedProcess(argv, 1, "", "describe boom")

        prov = providers.HetznerProvider(runner=scripted)
        with self.assertRaises(providers.ProviderError):
            prov.create_image("9", "v1-snap", {})

    def test_list_images_success(self) -> None:
        out = json.dumps([{"id": 1, "labels": {"vmlease-purpose": "image-cache"}, "created": "2024-01-01T00:00:00+00:00", "disk_size": 40, "architecture": "x86"}])
        prov = providers.HetznerProvider(runner=_fake_provider_runner(0, out))
        images = prov.list_images("vmlease-purpose=image-cache")
        self.assertEqual([i.id for i in images], ["1"])

    def test_list_images_failure_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "boom"))
        with self.assertRaises(providers.ProviderError):
            prov.list_images("vmlease-purpose=image-cache")

    def test_list_images_malformed_json_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(0, "{not json"))
        with self.assertRaises(providers.ProviderError):
            prov.list_images("vmlease-purpose=image-cache")

    def test_delete_image_success(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(0, "Image 7 deleted"))
        prov.delete_image("7")  # no raise

    def test_delete_image_idempotent_on_not_found(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "image not found"))
        prov.delete_image("7")  # not-found = success, no raise

    def test_delete_image_other_failure_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "forbidden"))
        with self.assertRaises(providers.ProviderError):
            prov.delete_image("7")

    def test_power_off_success(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(0, "Server 9 powered off"))
        prov.power_off("9")  # no raise

    def test_power_off_idempotent_when_already_off(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "server is already off"))
        prov.power_off("9")  # already-off = success, no raise

    def test_power_off_other_failure_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "forbidden"))
        with self.assertRaises(providers.ProviderError):
            prov.power_off("9")

    def test_server_status_success(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(0, json.dumps({"status": "off"})))
        self.assertEqual(prov.server_status("9"), "off")

    def test_server_status_failure_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "boom"))
        with self.assertRaises(providers.ProviderError):
            prov.server_status("9")

    def test_server_type_disk_success(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(0, json.dumps({"disk": 80})))
        self.assertEqual(prov.server_type_disk("cpx22"), 80.0)

    def test_server_type_disk_failure_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "boom"))
        with self.assertRaises(providers.ProviderError):
            prov.server_type_disk("cpx22")

    def test_image_ops_bounded_by_op_timeout(self) -> None:
        seen: list[float | None] = []

        def capture(argv: list[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
            seen.append(timeout)
            return subprocess.CompletedProcess(argv, 0, "", "")

        prov = providers.HetznerProvider(runner=capture, op_timeout=37.5)
        prov.delete_image("7")
        prov.power_off("9")
        self.assertEqual(seen, [37.5, 37.5])


# --------------------------------------------------------------------------- #
# battery loader
# --------------------------------------------------------------------------- #
class TestBattery(unittest.TestCase):
    def _one(self, **over: object) -> dict[str, object]:
        base: dict[str, object] = {"id": "P", "title": "t", "run": "c", "tag": "read-only"}
        base.update(over)
        return base

    def test_parse_ok(self) -> None:
        spec = battery_mod.parse_battery(_DEMO_BATTERY)
        self.assertEqual(spec.name, "demo-battery")
        self.assertEqual(len(spec.probes), 3)
        self.assertEqual(spec.probes[0].classifies, "L2")

    def test_resolve_ok(self) -> None:
        # a well-formed manifest with a run probe AND a script probe resolves.
        manifest = battery_toml("mixed", (
            {"id": "R", "title": "inline", "run": "uname -r", "tag": "read-only"},
            {"id": "S", "title": "scripted", "script": "prep.sh", "tag": "read-only"},
        ))
        b = _resolve_toml(manifest, {"prep.sh": "echo hi\n"})
        self.assertEqual(b.name, "mixed")
        self.assertEqual(b.probes[0].command, "uname -r")
        self.assertEqual(b.probes[0].source, "<inline>")
        self.assertEqual(b.probes[1].command, "echo hi\n")
        self.assertEqual(b.probes[1].source, "prep.sh")

    def test_real_example_battery_loads(self) -> None:
        # The shipped in-repo example is a standing regression artifact: it must
        # load through the real loader and stay shaped as documented.
        example = Path(__file__).parent.parent / "examples" / "compose-plugin-check" / "battery.toml"
        b = battery_mod.load_battery(example)
        self.assertEqual(len(b.probes), 1)
        self.assertEqual(b.probes[0].tag, ProbeTag.READ_ONLY)
        self.assertTrue(b.probes[0].command.strip())
        # The example is a docker smoke: post-migration it opts into docker via
        # `requires = ["docker"]` (default-off) rather than relying on always-on
        # docker. A docker-less host would fail its probes loudly.
        self.assertEqual(b.requires, ("docker",))

    def test_parse_bad_toml(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery("name = ")

    def test_parse_missing_name(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(battery_toml("x", (self._one(),)).replace("name = '''x'''\n", ""))

    def test_parse_empty_probes(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery("name = '''x'''\n")

    def test_parse_missing_probe_field(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\nrun = '''c'''\ntag = '''read-only'''\n"
            )

    def test_parse_unknown_tag(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(battery_toml("x", (self._one(tag="weird"),)))

    def test_parse_neither_run_nor_script(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\ntag = '''read-only'''\n"
            )

    def test_parse_both_run_and_script(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\n"
                "tag = '''read-only'''\nrun = '''c'''\nscript = '''s.sh'''\n"
            )

    def test_parse_unrecognized_root_key(self) -> None:
        with self.assertRaises(battery_mod.BatteryError) as ctx:
            battery_mod.parse_battery(
                "name = '''x'''\nbogus = '''v'''\n\n[[probe]]\nid = '''P'''\n"
                "title = '''t'''\ntag = '''read-only'''\nrun = '''c'''\n"
            )
        self.assertIn("bogus", str(ctx.exception))

    def test_parse_probe_not_a_table(self) -> None:
        # a `probe` array element that is not a table is rejected.
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery("name = '''x'''\nprobe = ['''not-a-table''']\n")

    def test_parse_run_not_a_string(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\n"
                "tag = '''read-only'''\nrun = 5\n"
            )

    def test_parse_script_empty_string(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\n"
                "tag = '''read-only'''\nscript = ''''''\n"
            )

    def test_parse_unrecognized_probe_key(self) -> None:
        # a `timout` typo must fail loud, not silently use the default timeout.
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\n"
                "tag = '''read-only'''\nrun = '''c'''\ntimout = 30\n"
            )

    def test_parse_duplicate_id(self) -> None:
        manifest = battery_toml("x", (
            {"id": "P", "title": "t", "run": "c", "tag": "read-only"},
            {"id": "P", "title": "t2", "run": "c2", "tag": "read-only"},
        ))
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(manifest)

    def test_parse_timeout_absent_is_none(self) -> None:
        # back-compat: a battery without per-probe timeout loads, timeout None.
        spec = battery_mod.parse_battery(_DEMO_BATTERY)
        self.assertIsNone(spec.probes[0].timeout)

    def test_parse_timeout_value_carried(self) -> None:
        spec = battery_mod.parse_battery(battery_toml("x", (self._one(timeout=42),)))
        self.assertEqual(spec.probes[0].timeout, 42.0)

    def test_parse_timeout_non_positive_raises(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(battery_toml("x", (self._one(timeout=0),)))

    def test_parse_timeout_non_numeric_raises(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\n"
                "tag = '''read-only'''\nrun = '''c'''\ntimeout = '''soon'''\n"
            )

    def test_parse_timeout_bool_rejected(self) -> None:
        # bool is an int subclass; ``true`` must not be silently read as ``1``.
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\n"
                "tag = '''read-only'''\nrun = '''c'''\ntimeout = true\n"
            )

    def test_parse_success_when_key_now_rejected_as_unknown(self) -> None:
        # success_when was retired (§8): the strict schema rejects it as an
        # unrecognized probe key, naming it.
        with self.assertRaises(battery_mod.BatteryError) as ctx:
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\n"
                "tag = '''read-only'''\nrun = '''c'''\nsuccess_when = '''READY'''\n"
            )
        self.assertIn("success_when", str(ctx.exception))

    # --- [probe.assert] declarative assertions (§5) ----------------------- #
    def _with_assert(self, assert_body: str) -> str:
        """A minimal manifest with an inline ``[probe.assert]`` table body."""
        return (
            "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\n"
            "tag = '''read-only'''\nrun = '''c'''\n"
            f"[probe.assert]\n{assert_body}"
        )

    def test_parse_assert_absent_is_empty_tuple(self) -> None:
        # back-compat: a probe without [probe.assert] carries no assertions.
        spec = battery_mod.parse_battery(_DEMO_BATTERY)
        self.assertEqual(spec.probes[0].assertions, ())

    def test_parse_assert_unknown_key_rejected_naming_it(self) -> None:
        # the registry key set IS the schema — an unknown key fails loud.
        with self.assertRaises(battery_mod.BatteryError) as ctx:
            battery_mod.parse_battery(self._with_assert("bogus_check = 0\n"))
        self.assertIn("bogus_check", str(ctx.exception))

    def test_parse_assert_not_a_table_rejected(self) -> None:
        with self.assertRaises(battery_mod.BatteryError) as ctx:
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\n"
                "tag = '''read-only'''\nrun = '''c'''\nassert = '''nope'''\n"
            )
        self.assertIn("assert", str(ctx.exception))

    def test_parse_assert_wrong_shaped_value_rejected_naming_key(self) -> None:
        # exit expects an int; a string is a shape error (ValueError -> BatteryError).
        with self.assertRaises(battery_mod.BatteryError) as ctx:
            battery_mod.parse_battery(self._with_assert("exit = '''x'''\n"))
        self.assertIn("exit", str(ctx.exception))

    def test_parse_assert_malformed_regex_fails_in_pure_pass(self) -> None:
        # D10(I): a bad pattern is compiled inside parse_battery (the PURE pass)
        # and surfaces as BatteryError at load, not at evaluation.
        with self.assertRaises(battery_mod.BatteryError) as ctx:
            battery_mod.parse_battery(self._with_assert("stdout_matches = '''(unclosed'''\n"))
        self.assertIn("stdout_matches", str(ctx.exception))

    def test_parse_assert_carried_onto_spec_and_evaluates(self) -> None:
        spec = battery_mod.parse_battery(
            self._with_assert("exit = 0\nstdout_has = '''READY'''\n")
        )
        self.assertEqual(len(spec.probes[0].assertions), 2)
        ok = model.Outcome(exit_code=0, stdout="READY now", stderr="")
        bad = model.Outcome(exit_code=1, stdout="nope", stderr="")
        self.assertTrue(all(a.check(ok) is None for a in spec.probes[0].assertions))
        self.assertFalse(all(a.check(bad) is None for a in spec.probes[0].assertions))

    def test_resolve_assert_carried_onto_probe(self) -> None:
        b = _resolve_toml(self._with_assert("exit = 0\n"))
        self.assertEqual(len(b.probes[0].assertions), 1)
        self.assertIsNone(
            b.probes[0].assertions[0].check(model.Outcome(0, "", ""))
        )

    # --- resolution + symlink-safe containment ---------------------------- #
    def test_resolve_script_absolute_path_rejected(self) -> None:
        manifest = battery_toml("x", (self._one(script="/etc/passwd"),))
        with self.assertRaises(battery_mod.BatteryError):
            _resolve_toml(manifest)

    def test_resolve_script_dotdot_escape_rejected(self) -> None:
        manifest = battery_toml("x", (self._one(script="../secret.sh"),))
        with self.assertRaises(battery_mod.BatteryError):
            _resolve_toml(manifest)

    def test_resolve_script_symlink_out_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as outside_d, tempfile.TemporaryDirectory() as bundle_d:
            secret = Path(outside_d) / "secret.sh"
            secret.write_text("echo leak\n", encoding="utf-8")
            link = Path(bundle_d) / "prep.sh"
            link.symlink_to(secret)  # contained-looking name, out-of-tree target
            manifest = battery_toml("x", (self._one(script="prep.sh"),))
            (Path(bundle_d) / "battery.toml").write_text(manifest, encoding="utf-8")
            with self.assertRaises(battery_mod.BatteryError):
                battery_mod.load_battery(Path(bundle_d) / "battery.toml")

    def test_resolve_missing_script_names_probe_and_path(self) -> None:
        manifest = battery_toml("x", (self._one(script="prep.sh"),))
        with self.assertRaises(battery_mod.BatteryError) as ctx:
            _resolve_toml(manifest)
        self.assertIn("'P'", str(ctx.exception))
        self.assertIn("prep.sh", str(ctx.exception))

    def test_resolve_empty_script_file_rejected(self) -> None:
        manifest = battery_toml("x", (self._one(script="prep.sh"),))
        with self.assertRaises(battery_mod.BatteryError):
            _resolve_toml(manifest, {"prep.sh": "   \n\t"})

    def test_resolve_empty_run_block_rejected(self) -> None:
        manifest = battery_toml("x", (self._one(run="   \n"),))
        with self.assertRaises(battery_mod.BatteryError):
            _resolve_toml(manifest)

    def test_resolve_run_only_battery_no_script_files(self) -> None:
        # a run-only battery loads with no script files present beside it.
        b = _resolve_toml(_DEMO_BATTERY)
        self.assertEqual([p.source for p in b.probes], ["<inline>", "<inline>", "<inline>"])

    def test_load_battery_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = _write_battery_bundle(d, _DEMO_BATTERY)
            b = battery_mod.load_battery(Path(p))
            self.assertEqual(b.name, "demo-battery")

    def test_load_battery_missing_file(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.load_battery(Path("/no/such/battery.toml"))


# --------------------------------------------------------------------------- #
# capabilities — the recipe registry + the one canonicalizer
# --------------------------------------------------------------------------- #
class TestCapabilities(unittest.TestCase):
    def test_docker_is_the_v1_vocabulary(self) -> None:
        self.assertEqual(capabilities.known_capabilities(), frozenset({"docker"}))

    def test_docker_registered_for_apt_dnf_pacman(self) -> None:
        for mgr in ("apt", "dnf", "pacman"):
            self.assertIsInstance(
                capabilities.recipe_for("docker", mgr), capabilities.CapabilityRecipe
            )

    def test_canonical_requires_sorts_and_dedups(self) -> None:
        self.assertEqual(
            capabilities.canonical_requires(("b", "a", "b")), ("a", "b")
        )

    def test_canonical_requires_order_invariant(self) -> None:
        self.assertEqual(
            capabilities.canonical_requires(("a", "b")),
            capabilities.canonical_requires(("b", "a")),
        )

    def test_recipe_for_unknown_capability_raises(self) -> None:
        with self.assertRaises(capabilities.UnknownCapabilityError) as ctx:
            capabilities.recipe_for("nope", "apt")
        self.assertIn("nope", str(ctx.exception))

    def test_recipe_for_unsupported_manager_raises_naming_both(self) -> None:
        # injectable registry: a synthetic capability with no recipe for a manager
        # raises a clear error naming the capability AND the manager (before spend).
        from types import MappingProxyType
        registry = MappingProxyType(
            {"widget": MappingProxyType({"apt": capabilities.CapabilityRecipe()})}
        )
        with self.assertRaises(capabilities.UnknownCapabilityError) as ctx:
            capabilities.recipe_for("widget", "dnf", registry=registry)
        msg = str(ctx.exception)
        self.assertIn("widget", msg)
        self.assertIn("dnf", msg)

    def test_install_command_per_manager(self) -> None:
        self.assertEqual(capabilities.install_command("apt"), "apt-get install -y")
        self.assertEqual(capabilities.install_command("dnf"), "dnf install -y")
        self.assertEqual(capabilities.install_command("pacman"), "pacman -S --noconfirm")

    def test_install_command_unknown_manager_raises(self) -> None:
        with self.assertRaises(capabilities.UnknownPackageManagerError):
            capabilities.install_command("brew")


# --------------------------------------------------------------------------- #
# battery — requires + [prep] schema (happy + reject paths)
# --------------------------------------------------------------------------- #
class TestBatteryRequiresAndPrep(unittest.TestCase):
    _PROBE = "\n[[probe]]\nid = '''P'''\ntitle = '''t'''\ntag = '''read-only'''\nrun = '''c'''\n"

    def _manifest(self, body: str) -> str:
        return f"name = '''x'''\n{body}{self._PROBE}"

    # --- requires (host-capabilities) ------------------------------------- #
    def test_requires_absent_defaults_empty(self) -> None:
        b = _resolve_toml(self._manifest(""))
        self.assertEqual(b.requires, ())

    def test_requires_docker_carried(self) -> None:
        b = _resolve_toml(self._manifest("requires = ['''docker''']\n"))
        self.assertEqual(b.requires, ("docker",))

    def test_requires_unknown_capability_rejected(self) -> None:
        with self.assertRaises(battery_mod.BatteryError) as ctx:
            battery_mod.parse_battery(self._manifest("requires = ['''dokcer''']\n"))
        self.assertIn("dokcer", str(ctx.exception))

    def test_requires_non_list_rejected(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(self._manifest("requires = '''docker'''\n"))

    # --- [prep] happy paths ----------------------------------------------- #
    def test_prep_absent_is_none(self) -> None:
        b = _resolve_toml(self._manifest(""))
        self.assertIsNone(b.prep)

    def test_prep_packages_union_selectors_carried(self) -> None:
        b = _resolve_toml(self._manifest(
            "[prep.packages]\napt = ['''a''']\ndebian = ['''b''']\n"
        ))
        assert b.prep is not None
        self.assertEqual(b.prep.packages["apt"], ("a",))
        self.assertEqual(b.prep.packages["debian"], ("b",))

    def test_prep_packages_union_resolution_manager_first_dedup(self) -> None:
        # the loader carries the per-selector lists; the documented union rule is
        # union(manager, distro), deduped, manager-first — exercised here over the
        # carried lists (the runtime resolver in group 5 consumes the same shape).
        b = _resolve_toml(self._manifest(
            "[prep.packages]\napt = ['''a''', '''shared''']\nubuntu = ['''shared''', '''u''']\n"
        ))
        assert b.prep is not None
        manager_list = b.prep.packages["apt"]
        distro_list = b.prep.packages["ubuntu"]
        union: list[str] = list(manager_list)
        for p in distro_list:
            if p not in union:
                union.append(p)
        self.assertEqual(union, ["a", "shared", "u"])

    def test_prep_setup_step_defaults(self) -> None:
        b = _resolve_toml(self._manifest(
            "[[prep.setup]]\nid = '''s1'''\nrun = '''echo hi'''\n"
        ))
        assert b.prep is not None
        step = b.prep.setup[0]
        self.assertEqual(step.id, "s1")
        self.assertEqual(step.command, "echo hi")
        self.assertEqual(step.distros, ())
        self.assertTrue(step.required)
        self.assertEqual(step.timeout, 1800.0)
        self.assertEqual(step.source, "<inline>")

    def test_prep_setup_timeout_override(self) -> None:
        b = _resolve_toml(self._manifest(
            "[[prep.setup]]\nid = '''s1'''\nrun = '''echo hi'''\ntimeout = 42\n"
        ))
        assert b.prep is not None
        self.assertEqual(b.prep.setup[0].timeout, 42.0)

    def test_prep_setup_required_false_carried(self) -> None:
        b = _resolve_toml(self._manifest(
            "[[prep.setup]]\nid = '''s1'''\nrun = '''echo hi'''\nrequired = false\n"
        ))
        assert b.prep is not None
        self.assertFalse(b.prep.setup[0].required)

    def test_prep_setup_script_resolved_and_contained(self) -> None:
        b = _resolve_toml(
            self._manifest("[[prep.setup]]\nid = '''s1'''\nscript = '''prep.sh'''\n"),
            {"prep.sh": "echo prep\n"},
        )
        assert b.prep is not None
        self.assertEqual(b.prep.setup[0].command, "echo prep\n")
        self.assertEqual(b.prep.setup[0].source, "prep.sh")

    def test_prep_setup_distros_allowlist_carried(self) -> None:
        b = _resolve_toml(self._manifest(
            "[[prep.setup]]\nid = '''s1'''\nrun = '''c'''\ndistros = ['''arch''']\n"
        ))
        assert b.prep is not None
        self.assertEqual(b.prep.setup[0].distros, ("arch",))

    # --- [prep] reject paths ---------------------------------------------- #
    def test_prep_unknown_key_rejected(self) -> None:
        with self.assertRaises(battery_mod.BatteryError) as ctx:
            battery_mod.parse_battery(self._manifest("[prep]\nbogus = 1\n"))
        self.assertIn("bogus", str(ctx.exception))

    def test_prep_packages_unknown_selector_rejected(self) -> None:
        for bad in ("apt-get", "ubntu"):
            with self.assertRaises(battery_mod.BatteryError) as ctx:
                battery_mod.parse_battery(
                    self._manifest(f"[prep.packages]\n{bad} = ['''x''']\n")
                )
            self.assertIn(bad, str(ctx.exception))

    def test_prep_setup_distros_typo_rejected(self) -> None:
        with self.assertRaises(battery_mod.BatteryError) as ctx:
            battery_mod.parse_battery(self._manifest(
                "[[prep.setup]]\nid = '''s1'''\nrun = '''c'''\ndistros = ['''arhc''']\n"
            ))
        self.assertIn("arhc", str(ctx.exception))

    def test_prep_setup_duplicate_id_rejected(self) -> None:
        with self.assertRaises(battery_mod.BatteryError) as ctx:
            battery_mod.parse_battery(self._manifest(
                "[[prep.setup]]\nid = '''s1'''\nrun = '''a'''\n"
                "[[prep.setup]]\nid = '''s1'''\nrun = '''b'''\n"
            ))
        self.assertIn("s1", str(ctx.exception))

    def test_prep_setup_neither_run_nor_script_rejected(self) -> None:
        with self.assertRaises(battery_mod.BatteryError) as ctx:
            battery_mod.parse_battery(self._manifest("[[prep.setup]]\nid = '''s1'''\n"))
        self.assertIn("s1", str(ctx.exception))

    def test_prep_setup_both_run_and_script_rejected(self) -> None:
        with self.assertRaises(battery_mod.BatteryError) as ctx:
            battery_mod.parse_battery(self._manifest(
                "[[prep.setup]]\nid = '''s1'''\nrun = '''c'''\nscript = '''s.sh'''\n"
            ))
        self.assertIn("s1", str(ctx.exception))

    def test_prep_setup_empty_command_rejected(self) -> None:
        # an empty resolved command is a vacuous step, caught at resolve (the full
        # loader), mirroring the probe empty-command rule.
        with self.assertRaises(battery_mod.BatteryError) as ctx:
            _resolve_toml(self._manifest(
                "[[prep.setup]]\nid = '''s1'''\nrun = '''   '''\n"
            ))
        self.assertIn("s1", str(ctx.exception))

    def test_prep_setup_unknown_step_key_rejected(self) -> None:
        with self.assertRaises(battery_mod.BatteryError) as ctx:
            battery_mod.parse_battery(self._manifest(
                "[[prep.setup]]\nid = '''s1'''\nrun = '''c'''\ntimout = 1\n"
            ))
        self.assertIn("timout", str(ctx.exception))

    def test_prep_setup_script_containment_escape_rejected(self) -> None:
        # absolute path, .. escape, and out-of-tree symlink are each refused.
        with self.assertRaises(battery_mod.BatteryError):
            _resolve_toml(self._manifest(
                "[[prep.setup]]\nid = '''s1'''\nscript = '''/etc/passwd'''\n"
            ))
        with self.assertRaises(battery_mod.BatteryError):
            _resolve_toml(self._manifest(
                "[[prep.setup]]\nid = '''s1'''\nscript = '''../outside.sh'''\n"
            ))
        d = Path(tempfile.mkdtemp())
        outside = Path(tempfile.mkdtemp()) / "target.sh"
        outside.write_text("echo nope\n", encoding="utf-8")
        (d / "link.sh").symlink_to(outside)
        (d / "battery.toml").write_text(
            self._manifest("[[prep.setup]]\nid = '''s1'''\nscript = '''link.sh'''\n"),
            encoding="utf-8",
        )
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.load_battery(d / "battery.toml")


# --------------------------------------------------------------------------- #
# battery.lint_battery — non-fatal authoring guardrails
# --------------------------------------------------------------------------- #
class TestBatteryLint(unittest.TestCase):
    def _p(self, pid: str, cmd: str, tag: ProbeTag) -> Probe:
        return Probe(id=pid, title=pid, command=cmd, tag=tag)

    def _b(self, *probes: Probe) -> model.Battery:
        return model.Battery(name="t", probes=tuple(probes))

    def test_clean_battery_no_warnings(self) -> None:
        b = self._b(
            self._p("A", "uname -a", ProbeTag.READ_ONLY),
            self._p("B", "do-setup; exit $?", ProbeTag.MUTATING_HOST_ROOT),
        )
        self.assertEqual(battery_mod.lint_battery(b), ())

    def test_no_order_warning_for_mixed_tag_order(self) -> None:
        # a read-only probe authored AFTER a host-root probe no longer warns —
        # execution is authoring order, so there is no reorder to surprise on.
        b = self._b(
            self._p("SETUP", "do-setup; exit $?", ProbeTag.MUTATING_HOST_ROOT),
            self._p("VERIFY", "check; exit $?", ProbeTag.READ_ONLY),
        )
        self.assertEqual(battery_mod.lint_battery(b), ())

    def test_vacuous_ok_conditional_echo_warns(self) -> None:
        b = self._b(self._p("V", "grep x f && echo OK || echo FAIL", ProbeTag.READ_ONLY))
        warns = battery_mod.lint_battery(b)
        self.assertTrue(any("'V'" in w and "exit $rc" in w for w in warns))

    def test_vacuous_ok_trailing_echo_warns(self) -> None:
        b = self._b(self._p("V", "do-thing; echo DONE", ProbeTag.READ_ONLY))
        self.assertTrue(any("'V'" in w for w in battery_mod.lint_battery(b)))

    def test_exit_gated_command_not_flagged(self) -> None:
        b = self._b(self._p("V", "grep x f && echo OK || echo FAIL; exit $?", ProbeTag.READ_ONLY))
        self.assertEqual(battery_mod.lint_battery(b), ())

    def test_exit_word_inside_echo_string_is_not_gating(self) -> None:
        # the c004 false-negative: "exit" appears only inside an echo string, the
        # command is NOT actually exit-gated -> must still warn (statement-level check)
        b = self._b(self._p("A", 'echo "setup exit: $RC"; check && echo OK || echo FAIL', ProbeTag.READ_ONLY))
        self.assertTrue(any("'A'" in w for w in battery_mod.lint_battery(b)))

    def test_plain_command_not_flagged(self) -> None:
        b = self._b(self._p("V", "uname -a", ProbeTag.READ_ONLY))
        self.assertEqual(battery_mod.lint_battery(b), ())

    def _ps(self, pid: str, cmd: str, tag: ProbeTag) -> Probe:
        # A probe that DECLARES an assertion (exempt from the vacuous-ok advisory).
        assertion = assertions._ASSERTIONS["stdout_has"].build("READY")
        return Probe(id=pid, title=pid, command=cmd, tag=tag, assertions=(assertion,))

    def test_assertions_declared_probe_exempt_from_vacuous_ok(self) -> None:
        # un-gated token-printing tail, but ok is decided by declared assertions
        # -> not a footgun.
        b = self._b(
            self._ps("T", "check && echo READY || echo NOPE", ProbeTag.READ_ONLY)
        )
        self.assertEqual(battery_mod.lint_battery(b), ())

    def test_same_probe_without_assertions_still_warns(self) -> None:
        b = self._b(self._p("T", "check && echo READY || echo NOPE", ProbeTag.READ_ONLY))
        warns = battery_mod.lint_battery(b)
        self.assertTrue(any("'T'" in w and "exit $rc" in w for w in warns))

    def test_non_host_root_sudo_warns_naming_probe(self) -> None:
        b = self._b(self._p("S", "sudo systemctl restart foo; exit $?", ProbeTag.READ_ONLY))
        warns = battery_mod.lint_battery(b)
        self.assertTrue(
            any("'S'" in w and "sudo" in w and "mutating:host-root" in w for w in warns)
        )

    def test_operator_space_sudo_warns(self) -> None:
        b = self._b(self._p("O", "sudo -n true; exit $?", ProbeTag.MUTATING_OPERATOR_SPACE))
        self.assertTrue(any("'O'" in w and "sudo" in w for w in battery_mod.lint_battery(b)))

    def test_host_root_sudo_does_not_warn(self) -> None:
        b = self._b(self._p("H", "sudo systemctl restart foo; exit $?", ProbeTag.MUTATING_HOST_ROOT))
        self.assertEqual(battery_mod.lint_battery(b), ())

    def test_sudo_word_boundary_no_false_positive_on_substrings(self) -> None:
        # 'pseudo' / 'sudoers' must NOT trip the sudo heuristic.
        b = self._b(self._p("P", "cat /etc/sudoers; ls /pseudo; exit $?", ProbeTag.READ_ONLY))
        self.assertEqual(battery_mod.lint_battery(b), ())

    def test_clean_battery_with_declared_and_gated_probes_silent(self) -> None:
        b = self._b(
            self._ps("T", "check && echo READY || echo NOPE", ProbeTag.READ_ONLY),
            self._p("G", "do-setup; exit $?", ProbeTag.MUTATING_HOST_ROOT),
        )
        self.assertEqual(battery_mod.lint_battery(b), ())

    def test_probe_can_trigger_both_rules(self) -> None:
        # un-gated token tail (no declared assertions) AND non-host-root sudo -> two warnings.
        b = self._b(self._p("B", "sudo check && echo OK || echo FAIL", ProbeTag.READ_ONLY))
        warns = battery_mod.lint_battery(b)
        self.assertEqual(len(warns), 2)
        self.assertTrue(any("exit $rc" in w for w in warns))
        self.assertTrue(any("sudo" in w for w in warns))

    # --- structural_violations: the fatal no-verdict-source subset (§9) ---- #
    def test_structural_violations_flags_no_verdict_source_probe(self) -> None:
        b = self._b(self._p("V", "grep x f && echo OK || echo FAIL", ProbeTag.READ_ONLY))
        viol = battery_mod.structural_violations(b)
        self.assertTrue(any("'V'" in v and "exit $rc" in v for v in viol))

    def test_structural_violations_exempts_assertion_declaring_probe(self) -> None:
        b = self._b(self._ps("T", "check && echo READY || echo NOPE", ProbeTag.READ_ONLY))
        self.assertEqual(battery_mod.structural_violations(b), ())

    def test_structural_violations_exempts_exit_gated_probe(self) -> None:
        b = self._b(self._p("G", "grep x f && echo OK || echo FAIL; exit $?", ProbeTag.READ_ONLY))
        self.assertEqual(battery_mod.structural_violations(b), ())

    def test_structural_violations_single_sourced_into_advisory(self) -> None:
        # the advisory vacuous-ok finding IS the fatal structural finding (same set).
        b = self._b(self._p("V", "grep x f && echo OK || echo FAIL", ProbeTag.READ_ONLY))
        viol = battery_mod.structural_violations(b)
        warns = battery_mod.lint_battery(b)
        self.assertEqual(len(viol), 1)
        self.assertTrue(all(v in warns for v in viol))

    def test_structural_violations_ignores_sudo_mislabel(self) -> None:
        # sudo-mislabel is advisory-only; it is NOT a structural violation.
        b = self._b(self._p("S", "sudo systemctl restart foo; exit $?", ProbeTag.READ_ONLY))
        self.assertEqual(battery_mod.structural_violations(b), ())
        self.assertTrue(any("sudo" in w for w in battery_mod.lint_battery(b)))


# --------------------------------------------------------------------------- #
# battery.shellcheck_battery — severity-graded findings over every probe (D5)
# --------------------------------------------------------------------------- #
# A realistic ``shellcheck --shell=bash --format=gcc -`` sample: an SC2155 warning
# (return-value masking) and an SC2015 note (the ``A && B || C`` footgun). The
# filename is ``-`` because the script is fed over stdin.
_GCC_SAMPLE = (
    "-:1:7: warning: Declare and assign separately to avoid masking return values. [SC2155]\n"
    "-:3:12: note: Note that A && B || C is not if-then-else. C may run when A is true. [SC2015]\n"
)


class TestShellcheckDriver(unittest.TestCase):
    def _b(self, *probes: Probe) -> model.Battery:
        return model.Battery(name="t", probes=tuple(probes))

    def _runner(
        self, stdout: str, *, returncode: int = 1
    ) -> Callable[[list[str], str | None], subprocess.CompletedProcess[str]]:
        calls: list[tuple[list[str], str | None]] = []

        def _run(argv: list[str], stdin_text: str | None) -> subprocess.CompletedProcess[str]:
            calls.append((argv, stdin_text))
            return subprocess.CompletedProcess(argv, returncode, stdout, "")

        self.calls = calls
        return _run

    def test_parses_severities_codes_locations(self) -> None:
        b = self._b(Probe(id="PREP", title="prep", command="x=$(date)\n\ngrep f && a || b", tag=ProbeTag.READ_ONLY, source="prep.sh"))
        findings = shellcheck_mod.shellcheck_battery(b, runner=self._runner(_GCC_SAMPLE))
        assert isinstance(findings, tuple)
        self.assertEqual(len(findings), 2)
        warn, note = findings
        self.assertEqual((warn.severity, warn.code, warn.line, warn.column), ("warning", "SC2155", 1, 7))
        self.assertEqual((note.severity, note.code, note.line, note.column), ("note", "SC2015", 3, 12))
        # script probe -> location is the script path; line numbers index file content
        self.assertEqual(warn.location, "prep.sh")
        self.assertEqual(warn.probe_id, "PREP")
        self.assertNotIn("[SC2155]", warn.message)

    def test_run_probe_labelled_by_probe_id_and_fed_via_stdin(self) -> None:
        b = self._b(Probe(id="KVER", title="k", command="uname -r && echo ok || echo no", tag=ProbeTag.READ_ONLY, source="<inline>"))
        runner = self._runner("-:1:9: note: msg [SC2015]\n")
        findings = shellcheck_mod.shellcheck_battery(b, runner=runner)
        assert isinstance(findings, tuple)
        # run-block findings are located by the probe's source ("<inline>") + id
        self.assertEqual(findings[0].location, "<inline>")
        self.assertEqual(findings[0].probe_id, "KVER")
        # both kinds fed over stdin: argv ends with "-", stdin carries the command
        argv, stdin_text = self.calls[0]
        self.assertEqual(argv, ["shellcheck", "--shell=bash", "--format=gcc", "-"])
        self.assertEqual(stdin_text, "uname -r && echo ok || echo no")

    def test_both_probe_kinds_fed_via_stdin_with_correct_labels(self) -> None:
        b = self._b(
            Probe(id="S", title="s", command="echo from-script", tag=ProbeTag.READ_ONLY, source="s.sh"),
            Probe(id="R", title="r", command="echo from-run", tag=ProbeTag.READ_ONLY, source="<inline>"),
        )
        findings = shellcheck_mod.shellcheck_battery(b, runner=self._runner("-:1:1: style: m [SC2086]\n"))
        assert isinstance(findings, tuple)
        self.assertEqual([f.location for f in findings], ["s.sh", "<inline>"])
        # every call fed the command text over stdin, no path on the argv
        self.assertEqual([stdin for _, stdin in self.calls], ["echo from-script", "echo from-run"])
        for argv, _ in self.calls:
            self.assertEqual(argv[-1], "-")
            self.assertNotIn("--", argv)

    def test_non_matching_lines_skipped(self) -> None:
        b = self._b(Probe(id="P", title="p", command="x", tag=ProbeTag.READ_ONLY, source="<inline>"))
        noisy = "\nIn - line 1:\n^-- some caret art\n" + _GCC_SAMPLE
        findings = shellcheck_mod.shellcheck_battery(b, runner=self._runner(noisy))
        assert isinstance(findings, tuple)
        self.assertEqual(len(findings), 2)

    def test_finding_without_code_keeps_empty_code(self) -> None:
        b = self._b(Probe(id="P", title="p", command="x", tag=ProbeTag.READ_ONLY, source="<inline>"))
        findings = shellcheck_mod.shellcheck_battery(b, runner=self._runner("-:2:4: error: bare message no code\n"))
        assert isinstance(findings, tuple)
        self.assertEqual(findings[0].code, "")
        self.assertEqual(findings[0].message, "bare message no code")

    def test_file_not_found_yields_unavailable_sentinel(self) -> None:
        b = self._b(Probe(id="P", title="p", command="x", tag=ProbeTag.READ_ONLY, source="<inline>"))

        def _run(argv: list[str], stdin_text: str | None) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("shellcheck")

        self.assertIs(shellcheck_mod.shellcheck_battery(b, runner=_run), shellcheck_mod.SHELLCHECK_UNAVAILABLE)

    def test_timeout_yields_unavailable_sentinel(self) -> None:
        b = self._b(Probe(id="P", title="p", command="x", tag=ProbeTag.READ_ONLY, source="<inline>"))

        def _run(argv: list[str], stdin_text: str | None) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(argv, 60.0)

        self.assertIs(shellcheck_mod.shellcheck_battery(b, runner=_run), shellcheck_mod.SHELLCHECK_UNAVAILABLE)

    def test_findings_at_or_above_thresholds(self) -> None:
        b = self._b(Probe(id="P", title="p", command="x", tag=ProbeTag.READ_ONLY, source="<inline>"))
        sample = (
            "-:1:1: style: s [SC2086]\n"
            "-:2:1: note: n [SC2015]\n"
            "-:3:1: warning: w [SC2155]\n"
            "-:4:1: error: e [SC1009]\n"
        )
        findings = shellcheck_mod.shellcheck_battery(b, runner=self._runner(sample))
        assert isinstance(findings, tuple)
        self.assertEqual(len(shellcheck_mod.findings_at_or_above(findings, "style")), 4)
        self.assertEqual(len(shellcheck_mod.findings_at_or_above(findings, "note")), 3)
        self.assertEqual(len(shellcheck_mod.findings_at_or_above(findings, "warning")), 2)
        sev = [f.severity for f in shellcheck_mod.findings_at_or_above(findings, "error")]
        self.assertEqual(sev, ["error"])

    def test_clean_battery_yields_empty_findings_not_sentinel(self) -> None:
        b = self._b(Probe(id="P", title="p", command="uname -r", tag=ProbeTag.READ_ONLY, source="<inline>"))
        findings = shellcheck_mod.shellcheck_battery(b, runner=self._runner("", returncode=0))
        self.assertEqual(findings, ())


# --------------------------------------------------------------------------- #
# distro
# --------------------------------------------------------------------------- #
class TestDistro(unittest.TestCase):
    def test_known_profiles(self) -> None:
        for key in distro.DEFAULT_DISTRO_KEYS:
            self.assertEqual(distro.get_profile(key).key, key)

    def test_unknown_profile_raises(self) -> None:
        with self.assertRaises(distro.UnknownDistroError):
            distro.get_profile("plan9")

    def test_debian_has_uidmap(self) -> None:
        self.assertIn("uidmap", distro.get_profile("debian").packages)

    def test_system_update_manager_defaults(self) -> None:
        self.assertIn("apt-get upgrade", distro.system_update_command(distro.get_profile("ubuntu")))
        self.assertIn("dnf -y upgrade", distro.system_update_command(distro.get_profile("fedora")))
        self.assertIn("pacman -Syu", distro.system_update_command(distro.get_profile("arch")))

    def test_system_update_override_wins(self) -> None:
        p = distro.DistroProfile(key="x", default_image="i", package_manager="apt", packages=("p",), system_update_override="custom-update")
        self.assertEqual(distro.system_update_command(p), "custom-update")

    def test_system_update_unknown_manager_raises(self) -> None:
        p = distro.DistroProfile(key="x", default_image="i", package_manager="zypper", packages=("p",))
        with self.assertRaises(distro.UnknownPackageManagerError):
            distro.system_update_command(p)

    def test_profiles_registries_are_read_only(self) -> None:
        # global-state hygiene: the two module-level registries are wrapped in
        # MappingProxyType — being that type IS the read-only guarantee (it has no
        # __setitem__/__delitem__, so any mutation raises at runtime). Assert the
        # type rather than attempting a mutation (which mypy --strict correctly
        # rejects on a Mapping — itself proof the static type is read-only too).
        from types import MappingProxyType

        self.assertIsInstance(distro.PROFILES, MappingProxyType)
        self.assertIsInstance(distro._SYSTEM_UPDATE_BY_MANAGER, MappingProxyType)


# --------------------------------------------------------------------------- #
# runner — build_host_specs + plan (zero provider calls)
# --------------------------------------------------------------------------- #
class TestRunner(unittest.TestCase):
    def _matrix(self, distros: tuple[str, ...] = ("ubuntu", "debian")) -> runner.Matrix:
        return runner.Matrix(
            workload=_demo_workload(),
            distro_keys=tuple(distros),
            server_type="cpx22",
            run_token="run-xyz",
        )

    def test_build_host_specs_labels_and_names(self) -> None:
        specs = runner.build_host_specs(self._matrix())
        self.assertEqual([s.distro_key for s in specs], ["ubuntu", "debian"])
        for s in specs:
            self.assertEqual(s.labels, {"vmlease": "run-xyz"})
            self.assertTrue(s.name.startswith("vmlease-run-xyz-"))
            self.assertEqual(s.firewall, "")  # no firewall by default

    def test_build_host_specs_threads_firewall(self) -> None:
        m = runner.Matrix(_demo_workload(), ("ubuntu",), "cpx22", "run-fw", firewall="my-firewall")
        specs = runner.build_host_specs(m)
        self.assertEqual(specs[0].firewall, "my-firewall")

    def test_build_host_specs_deterministic(self) -> None:
        a = runner.build_host_specs(self._matrix())
        b = runner.build_host_specs(self._matrix())
        self.assertEqual([s.name for s in a], [s.name for s in b])

    def test_plan_makes_no_provider_calls(self) -> None:
        # plan only needs the matrix; a provider is never constructed or passed.
        items = runner.plan(self._matrix())
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].workload_summary, "probes=3")

    def test_plan_surfaces_cost_guard(self) -> None:
        m = self._matrix(distros=("ubuntu", "debian", "fedora"))
        with self.assertRaises(safety.CostGuardError):
            runner.plan(m, cost_guard=safety.CostGuard(max_hosts=2))

    def test_plan_unknown_distro(self) -> None:
        m = runner.Matrix(workload=_demo_workload(), distro_keys=("nope",), server_type="cpx22", run_token="t-okay")
        with self.assertRaises(distro.UnknownDistroError):
            runner.plan(m)

    def test_plan_validates_good_upload(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "w.whl"
            local.write_bytes(b"WHEEL")
            m = runner.Matrix(
                _demo_workload(), ("ubuntu",), "cpx22", "run-up",
                uploads=(model.UploadSpec(local=local, remote="~/w.whl"),),
            )
            items = runner.plan(m)
            self.assertEqual(len(items), 1)

    def test_plan_rejects_bad_upload_source(self) -> None:
        # fail-closed: a bad upload aborts plan (zero provider calls) before spend
        m = runner.Matrix(
            _demo_workload(), ("ubuntu",), "cpx22", "run-up",
            uploads=(model.UploadSpec(local=Path("/no/such/file.whl"), remote="~/f.whl"),),
        )
        with self.assertRaises(safety.UploadError):
            runner.plan(m)

    def test_plan_rejects_bad_upload_remote(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "w.whl"
            local.write_bytes(b"WHEEL")
            m = runner.Matrix(
                _demo_workload(), ("ubuntu",), "cpx22", "run-up",
                uploads=(model.UploadSpec(local=local, remote="~/../etc/x"),),
            )
            with self.assertRaises(safety.UploadError):
                runner.plan(m)

    # --- requires on the provisioning spine (group 2) -------------------- #
    def test_matrix_carries_requires_default_empty(self) -> None:
        self.assertEqual(self._matrix().requires, ())

    def test_matrix_carries_requires(self) -> None:
        m = runner.Matrix(
            _demo_workload(), ("ubuntu",), "cpx22", "run-req",
            requires=("docker",),
        )
        self.assertEqual(m.requires, ("docker",))

    def test_build_host_specs_propagates_requires_to_every_host(self) -> None:
        m = runner.Matrix(
            _demo_workload(), ("ubuntu", "debian", "fedora"), "cpx22", "run-req",
            requires=("docker",),
        )
        specs = runner.build_host_specs(m)
        self.assertEqual(len(specs), 3)
        for s in specs:
            self.assertEqual(s.requires, ("docker",))

    def test_build_host_specs_canonicalizes_requires(self) -> None:
        # build_host_specs funnels through capabilities.canonical_requires:
        # order + duplicates collapse to the single canonical tuple.
        m = runner.Matrix(
            _demo_workload(), ("ubuntu",), "cpx22", "run-req",
            requires=("docker", "docker"),
        )
        self.assertEqual(runner.build_host_specs(m)[0].requires, ("docker",))

    def test_build_host_specs_requires_default_empty(self) -> None:
        for s in runner.build_host_specs(self._matrix()):
            self.assertEqual(s.requires, ())

    def test_build_host_specs_deterministic_with_requires(self) -> None:
        m = runner.Matrix(
            _demo_workload(), ("ubuntu", "debian"), "cpx22", "run-req",
            requires=("docker",),
        )
        a = runner.build_host_specs(m)
        b = runner.build_host_specs(m)
        self.assertEqual(
            [(s.name, s.requires) for s in a],
            [(s.name, s.requires) for s in b],
        )

    def test_plan_surfaces_requires_and_no_provider_calls(self) -> None:
        # plan takes no provider (zero provider calls structurally) and surfaces
        # the host's requires on every PlanItem.
        m = runner.Matrix(
            _demo_workload(), ("ubuntu", "debian"), "cpx22", "run-req",
            requires=("docker",),
        )
        items = runner.plan(m)
        self.assertEqual(len(items), 2)
        for it in items:
            self.assertEqual(it.requires, ("docker",))

    def test_plan_requires_default_empty(self) -> None:
        for it in runner.plan(self._matrix()):
            self.assertEqual(it.requires, ())


# --------------------------------------------------------------------------- #
# cli — plan subcommand (zero provider calls)
# --------------------------------------------------------------------------- #
class TestCli(unittest.TestCase):
    def _write_battery(self, d: str) -> str:
        return _write_battery_bundle(d, _DEMO_BATTERY)

    def test_plan_surfaces_lint_warnings_to_stderr(self) -> None:
        # a vacuously-ok probe → plan warns (non-fatal) on stderr, still exits 0
        with tempfile.TemporaryDirectory() as d:
            manifest = battery_toml("lint", (
                {"id": "V", "title": "v", "run": "grep x f && echo OK || echo FAIL", "tag": "read-only"},
            ))
            p = Path(_write_battery_bundle(d, manifest))
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = cli.main(["plan", "--battery", str(p), "--distros", "ubuntu", "--run-token", "lint-run"])
            self.assertEqual(rc, 0)  # non-fatal
            self.assertIn("warning:", err.getvalue())
            self.assertIn("exit $rc", err.getvalue())

    def test_plan_ok(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["plan", "--battery", self._write_battery(d), "--distros", "ubuntu,debian", "--run-token", "cli-run"])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("demo-battery", out)
            self.assertIn("NOTHING PROVISIONED", out)
            self.assertIn("vmlease-cli-run-ubuntu", out)

    def test_plan_bad_battery_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "battery.toml"
            p.write_text("name = ", encoding="utf-8")
            rc = cli.main(["plan", "--battery", str(p), "--run-token", "cli-run"])
            self.assertEqual(rc, 2)

    def test_plan_cost_guard_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rc = cli.main([
                "plan", "--battery", self._write_battery(d),
                "--distros", "ubuntu,debian,fedora,arch", "--max-hosts", "2", "--run-token", "cli-run",
            ])
            self.assertEqual(rc, 2)

    def test_plan_unknown_distro_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rc = cli.main(["plan", "--battery", self._write_battery(d), "--distros", "plan9", "--run-token", "cli-run"])
            self.assertEqual(rc, 2)

    def test_parse_upload_default_remote(self) -> None:
        spec = cli._parse_upload("/src/dist/sandbox_ai-0.1.whl")
        self.assertEqual(spec.local, Path("/src/dist/sandbox_ai-0.1.whl"))
        self.assertEqual(spec.remote, "~/sandbox_ai-0.1.whl")

    def test_parse_upload_explicit_remote_splits_first_colon(self) -> None:
        spec = cli._parse_upload("/src/w.whl:/opt/a:b/w.whl")
        self.assertEqual(spec.local, Path("/src/w.whl"))
        self.assertEqual(spec.remote, "/opt/a:b/w.whl")  # split on the FIRST colon only

    def test_upload_flag_repeatable_into_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ns = cli.build_parser().parse_args([
                "plan", "--battery", self._write_battery(d), "--distros", "ubuntu", "--run-token", "cli-run",
                "--upload", "/a/x.whl", "--upload", "/b/y.bin:~/dest/y.bin",
            ])
            m = cli._matrix_from_args(ns, _demo_workload())
            self.assertEqual(len(m.uploads), 2)
            self.assertEqual(m.uploads[0].remote, "~/x.whl")
            self.assertEqual((m.uploads[1].local, m.uploads[1].remote), (Path("/b/y.bin"), "~/dest/y.bin"))

    def test_matrix_from_args_default_requires_empty(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ns = cli.build_parser().parse_args([
                "plan", "--battery", self._write_battery(d), "--distros", "ubuntu", "--run-token", "cli-run",
            ])
            self.assertEqual(cli._matrix_from_args(ns, _demo_workload()).requires, ())

    def test_matrix_from_args_carries_requires(self) -> None:
        # the requires arg is already canonicalized (D-I.4) and lands on Matrix.
        with tempfile.TemporaryDirectory() as d:
            ns = cli.build_parser().parse_args([
                "plan", "--battery", self._write_battery(d), "--distros", "ubuntu", "--run-token", "cli-run",
            ])
            m = cli._matrix_from_args(ns, _demo_workload(), ("docker",))
            self.assertEqual(m.requires, ("docker",))

    def test_plan_rejects_bad_upload_no_provider_call(self) -> None:
        # the free-plan fail-closed path: a symlink source is refused at plan,
        # exit 2, and (since plan makes zero provider calls) nothing is provisioned.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "real.whl"
            target.write_bytes(b"WHEEL")
            link = Path(d) / "link.whl"
            os.symlink(target, link)
            rc = cli.main([
                "plan", "--battery", self._write_battery(d), "--distros", "ubuntu",
                "--run-token", "cli-run", "--upload", str(link),
            ])
            self.assertEqual(rc, 2)


# --------------------------------------------------------------------------- #
# cli — lint subcommand (severity-gated shellcheck gate; injected runner seam)
# --------------------------------------------------------------------------- #
class TestCliLint(unittest.TestCase):
    def _write(self, d: str, run: str = "uname -r") -> str:
        manifest = battery_toml("lint-cli", (
            {"id": "P", "title": "p", "run": run, "tag": "read-only"},
        ))
        return _write_battery_bundle(d, manifest)

    def _runner(
        self, stdout: str, *, returncode: int = 1
    ) -> Callable[[list[str], str | None], subprocess.CompletedProcess[str]]:
        def _run(argv: list[str], stdin_text: str | None) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, returncode, stdout, "")

        return _run

    def _unavailable_runner(self) -> Callable[[list[str], str | None], subprocess.CompletedProcess[str]]:
        def _run(argv: list[str], stdin_text: str | None) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("shellcheck")

        return _run

    def _run_lint(
        self,
        argv: list[str],
        runner: Callable[[list[str], str | None], subprocess.CompletedProcess[str]],
    ) -> tuple[int, str, str]:
        ns = cli.build_parser().parse_args(argv)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli._cmd_lint(ns, runner=runner)
        return rc, out.getvalue(), err.getvalue()

    def test_clean_battery_exits_0(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rc, out, _ = self._run_lint(
                ["lint", "--battery", self._write(d)], self._runner("", returncode=0)
            )
            self.assertEqual(rc, 0)
            self.assertIn("battery: lint-cli", out)
            self.assertIn("threshold: error", out)

    def test_error_finding_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rc, out, _ = self._run_lint(
                ["lint", "--battery", self._write(d)],
                self._runner("-:1:1: error: syntax boom [SC1009]\n"),
            )
            self.assertEqual(rc, 1)
            self.assertIn("SC1009", out)
            self.assertIn("syntax boom", out)

    def test_severity_warning_flips_warning_only_battery_to_1(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            sample = "-:1:7: warning: masking return values [SC2155]\n"
            # at default `error`: a warning-only battery passes (exit 0)
            rc_default, _, _ = self._run_lint(
                ["lint", "--battery", self._write(d)], self._runner(sample)
            )
            self.assertEqual(rc_default, 0)
            # tightened to `warning`: the same finding now fails the gate
            rc_strict, _, _ = self._run_lint(
                ["lint", "--battery", self._write(d), "--severity", "warning"],
                self._runner(sample),
            )
            self.assertEqual(rc_strict, 1)

    def test_unavailable_skips_with_notice_and_exits_0(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            # an advisory-only (sudo-mislabel) probe so the advisory check still has
            # something to print, but there is NO structural violation to gate on.
            manifest = battery_toml("lint-cli", (
                {"id": "P", "title": "p", "run": "sudo true; exit $?", "tag": "read-only"},
            ))
            p = _write_battery_bundle(d, manifest)
            rc, _, err = self._run_lint(["lint", "--battery", p], self._unavailable_runner())
            self.assertEqual(rc, 0)
            self.assertIn("shellcheck unavailable", err)
            self.assertIn("warning:", err)  # advisory sudo-mislabel still printed

    def test_unavailable_with_require_shellcheck_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rc, _, err = self._run_lint(
                ["lint", "--battery", self._write(d), "--require-shellcheck"],
                self._unavailable_runner(),
            )
            self.assertEqual(rc, 1)
            self.assertIn("error:", err)
            self.assertIn("shellcheck", err)

    def test_malformed_battery_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "battery.toml"
            p.write_text("name = ", encoding="utf-8")
            rc, _, err = self._run_lint(
                ["lint", "--battery", str(p)], self._runner("", returncode=0)
            )
            self.assertEqual(rc, 2)
            self.assertIn("error:", err)

    # --- structural no-verdict-source rule: HARD at `vmlease lint` (§9) ---- #
    _VACUOUS = "grep x f && echo OK || echo FAIL"

    def test_structural_violation_hard_fails_with_clean_shellcheck(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            manifest = battery_toml("lint-cli", (
                {"id": "V", "title": "v", "run": self._VACUOUS, "tag": "read-only"},
            ))
            p = _write_battery_bundle(d, manifest)
            # shellcheck is CLEAN (returncode 0, no findings) — structural still fatal.
            rc, _, err = self._run_lint(["lint", "--battery", p], self._runner("", returncode=0))
            self.assertEqual(rc, 1)
            self.assertIn("error:", err)
            self.assertIn("exit $rc", err)

    def test_structural_violation_hard_fails_with_shellcheck_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            manifest = battery_toml("lint-cli", (
                {"id": "V", "title": "v", "run": self._VACUOUS, "tag": "read-only"},
            ))
            p = _write_battery_bundle(d, manifest)
            rc, _, err = self._run_lint(["lint", "--battery", p], self._unavailable_runner())
            self.assertEqual(rc, 1)  # fatal regardless of shellcheck availability
            self.assertIn("error:", err)
            self.assertIn("exit $rc", err)

    def test_structural_violation_hard_fails_with_require_shellcheck(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            manifest = battery_toml("lint-cli", (
                {"id": "V", "title": "v", "run": self._VACUOUS, "tag": "read-only"},
            ))
            p = _write_battery_bundle(d, manifest)
            rc, _, err = self._run_lint(
                ["lint", "--battery", p, "--require-shellcheck"], self._unavailable_runner()
            )
            self.assertEqual(rc, 1)
            self.assertIn("error:", err)

    def test_assertion_declaring_probe_passes_lint(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            # an un-gated token tail but ok is decided by a declared assertion -> exempt.
            manifest = (
                "name = '''lint-cli'''\n\n[[probe]]\nid = '''A'''\ntitle = '''a'''\n"
                "tag = '''read-only'''\nrun = '''check && echo READY || echo NOPE'''\n"
                "[probe.assert]\nstdout_has = '''READY'''\n"
            )
            p = _write_battery_bundle(d, manifest)
            rc, _, _ = self._run_lint(["lint", "--battery", p], self._runner("", returncode=0))
            self.assertEqual(rc, 0)

    def test_exit_gated_probe_passes_lint(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            manifest = battery_toml("lint-cli", (
                {"id": "G", "title": "g", "run": self._VACUOUS + "; exit $?", "tag": "read-only"},
            ))
            p = _write_battery_bundle(d, manifest)
            rc, _, _ = self._run_lint(["lint", "--battery", p], self._runner("", returncode=0))
            self.assertEqual(rc, 0)

    def test_load_time_advisory_warns_but_does_not_gate_on_plan(self) -> None:
        # a no-verdict-source probe WARNS on plan but does not raise / change flow.
        with tempfile.TemporaryDirectory() as d:
            manifest = battery_toml("lint-cli", (
                {"id": "V", "title": "v", "run": self._VACUOUS, "tag": "read-only"},
            ))
            p = _write_battery_bundle(d, manifest)
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = cli.main(["plan", "--battery", p, "--distros", "ubuntu", "--run-token", "lint-run"])
            self.assertEqual(rc, 0)  # advisory only — does not gate
            self.assertIn("warning:", err.getvalue())
            self.assertIn("exit $rc", err.getvalue())

    def test_load_time_advisory_warns_but_does_not_gate_on_run(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            manifest = battery_toml("lint-cli", (
                {"id": "V", "title": "v", "run": self._VACUOUS, "tag": "read-only"},
            ))
            p = _write_battery_bundle(d, manifest)
            err = io.StringIO()
            # decline the provisioning confirmation -> exits 0, zero provider calls.
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = cli._cmd_run(
                    cli.build_parser().parse_args([
                        "run", "--battery", p, "--distros", "ubuntu",
                        "--results-dir", str(Path(d) / "r"), "--timestamp", "T",
                        "--run-token", "lint-run",
                    ]),
                    reader=lambda _prompt: "n",
                )
            self.assertEqual(rc, 0)  # advisory only — does not gate
            self.assertIn("warning:", err.getvalue())
            self.assertIn("exit $rc", err.getvalue())


# --------------------------------------------------------------------------- #
# templating
# --------------------------------------------------------------------------- #
class TestTemplating(unittest.TestCase):
    def test_render_fills_slots_and_passes_shell_dollar(self) -> None:
        out = templating.render('hi @@name@@ $HOME $(date)', {"name": "probe"})
        self.assertEqual(out, "hi probe $HOME $(date)")

    def test_render_missing_slot_raises(self) -> None:
        with self.assertRaises(templating.TemplateError):
            templating.render("@@a@@ @@b@@", {"a": "x"})

    def test_render_unused_key_raises(self) -> None:
        with self.assertRaises(templating.TemplateError):
            templating.render("@@a@@", {"a": "x", "b": "y"})

    def test_find_slots(self) -> None:
        self.assertEqual(templating.find_slots("@@a@@ @@b@@ $c"), {"a", "b"})


# --------------------------------------------------------------------------- #
# cloudinit — per-distro render (the apt debian/ubuntu divergence)
# --------------------------------------------------------------------------- #
class TestCloudInit(unittest.TestCase):
    def test_apt_uses_distro_specific_docker_repo(self) -> None:
        # The debian↔ubuntu repo-path divergence holds when docker is required —
        # the docker recipe (manager-keyed) renders the per-distro slug derived
        # from the profile key (the profile no longer carries a docker-repo slug).
        deb = cloudinit.render_cloudinit(distro.get_profile("debian"), "probe", "ssh-ed25519 AAAA", ("docker",))
        ubu = cloudinit.render_cloudinit(distro.get_profile("ubuntu"), "probe", "ssh-ed25519 AAAA", ("docker",))
        self.assertIn("download.docker.com/linux/debian", deb)
        self.assertIn("download.docker.com/linux/ubuntu", ubu)
        # debian must NOT carry the ubuntu repo path and vice versa
        self.assertNotIn("download.docker.com/linux/ubuntu", deb)

    # --- requires gates the docker recipe (3.5a) -------------------------- #
    def test_no_requires_renders_docker_free_cloudinit(self) -> None:
        # The acceptance test: a battery that requires nothing gets a docker-FREE
        # rendered cloud-init on every distro — no docker packages, no repo/keyring
        # setup, no static bundle. Always-on substrate (arch's modprobe) stays.
        for key in ("ubuntu", "debian", "fedora", "arch"):
            with self.subTest(distro=key):
                out = cloudinit.render_cloudinit(distro.get_profile(key), "probe", "ssh-ed25519 AAAA", ())
                for forbidden in (
                    "docker-ce",
                    "dockerd-rootless-setuptool.sh",
                    "download.docker.com",
                    "docker-buildx",
                    " docker ",  # the pacman docker package, space-delimited on the install line
                ):
                    self.assertNotIn(forbidden, out, msg=f"{key}: docker leaked: {forbidden!r}")
                # no unfilled slots regardless of requires
                self.assertNotIn("@@", out)
        # arch's always-on substrate (modprobe) survives a docker-free render
        arch_free = cloudinit.render_cloudinit(distro.get_profile("arch"), "probe", "ssh-ed25519 AAAA", ())
        self.assertIn("modprobe nf_tables ip_tables", arch_free)

    def test_requires_docker_renders_recipe_per_manager(self) -> None:
        # requires=["docker"] renders the docker recipe for each host's manager.
        apt_out = cloudinit.render_cloudinit(distro.get_profile("ubuntu"), "probe", "ssh-ed25519 AAAA", ("docker",))
        self.assertIn("dockerd-rootless-setuptool.sh", apt_out)
        self.assertIn("apt-get install -y docker-ce", apt_out)
        dnf_out = cloudinit.render_cloudinit(distro.get_profile("fedora"), "probe", "ssh-ed25519 AAAA", ("docker",))
        self.assertIn("dockerd-rootless-setuptool.sh", dnf_out)
        self.assertIn("dnf -y install docker-ce", dnf_out)
        pac_out = cloudinit.render_cloudinit(distro.get_profile("arch"), "probe", "ssh-ed25519 AAAA", ("docker",))
        # pacman: docker packages fold into the install line; the static bundle into setup
        self.assertIn("docker-buildx", pac_out)
        self.assertIn("/usr/local/bin/dockerd-rootless-setuptool.sh", pac_out)

    # --- byte-identity of the docker move (3.5b, D13.3) ------------------- #
    def test_docker_block_bytes_unchanged_apt(self) -> None:
        # The apt docker block — the byte-identical move of the former
        # install.apt.tmpl `if ! command -v dockerd-rootless-setuptool.sh` guard,
        # incl. the keyring/repo setup + the guarded docker-ce install. Asserted
        # verbatim so a rewrite (vs a move) goes RED.
        out = cloudinit.render_install_block(distro.get_profile("debian"), ("docker",))
        expected = (
            "  # docker-ce via the distro-correct repo path: debian (debian != ubuntu).\n"
            "  if ! command -v dockerd-rootless-setuptool.sh >/dev/null 2>&1; then\n"
            "    install -m 0755 -d /etc/apt/keyrings\n"
            '    curl -fsSL "https://download.docker.com/linux/debian/gpg" \\\n'
            "      | gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg\n"
            "    chmod a+r /etc/apt/keyrings/docker.gpg\n"
            '    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] '
            'https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \\\n'
            "      > /etc/apt/sources.list.d/docker.list\n"
            "    apt-get update\n"
            "    apt-get install -y docker-ce docker-ce-cli containerd.io docker-ce-rootless-extras "
            "docker-compose-plugin docker-buildx-plugin\n"
            "  fi"
        )
        self.assertIn(expected, out)

    def test_docker_block_bytes_unchanged_dnf(self) -> None:
        out = cloudinit.render_install_block(distro.get_profile("fedora"), ("docker",))
        expected = (
            "  if ! command -v dockerd-rootless-setuptool.sh >/dev/null 2>&1; then\n"
            "    dnf -y config-manager addrepo --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo \\\n"
            "      || dnf -y config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo \\\n"
            "      || curl -fsSL https://download.docker.com/linux/fedora/docker-ce.repo > /etc/yum.repos.d/docker-ce.repo\n"
            "    dnf -y install docker-ce docker-ce-cli containerd.io docker-ce-rootless-extras "
            "docker-compose-plugin docker-buildx-plugin\n"
            "  fi"
        )
        self.assertIn(expected, out)

    def test_docker_static_bundle_bytes_unchanged_pacman(self) -> None:
        # The Arch static-bundle setup fragment — the byte-identical move of the
        # former arch.extra_setup `if ! test -x …` guard (incl. all three guards:
        # the static-bundle existence guard here, plus the apt/dnf command-v guards
        # above). The pacman package-line intra-order is implementer's choice
        # (D13 safe-to-leave) — only the docker *bytes* (the bundle) are pinned.
        out = cloudinit.render_install_block(distro.get_profile("arch"), ("docker",))
        expected = (
            "if ! test -x /usr/local/bin/dockerd-rootless-setuptool.sh; then "
            "ver=29.5.1; m=$(uname -m); d=$(mktemp -d); "
            "curl -fsSL https://download.docker.com/linux/static/stable/${m}/docker-${ver}.tgz -o $d/docker.tgz; "
            "curl -fsSL https://download.docker.com/linux/static/stable/${m}/docker-rootless-extras-${ver}.tgz -o $d/extras.tgz; "
            "tar -C $d -xzf $d/docker.tgz; tar -C $d -xzf $d/extras.tgz; "
            "install -m0755 $d/docker/* /usr/local/bin/; "
            "install -m0755 $d/docker-rootless-extras/* /usr/local/bin/; "
            "rm -rf $d; fi"
        )
        self.assertIn(expected, out)
        # all the former arch docker packages are present on the install line
        for pkg in ("docker", "docker-buildx", "docker-compose", "rootlesskit", "slirp4netns", "fuse-overlayfs"):
            self.assertIn(pkg, out)

    def test_recipe_injection_order_is_canonical(self) -> None:
        # requires ordering must not perturb the rendered bytes (and so the cache
        # key) — the recipes inject in canonical sorted-deduplicated order. Two
        # capabilities are needed to truly exercise ordering, so this uses an
        # injected two-capability registry whose recipes render distinguishable
        # markers; the rendered capability_setup must be order-invariant.
        from vmlease import capabilities
        out_ab = cloudinit.render_install_block(distro.get_profile("ubuntu"), ("docker", "docker"))
        out_ba = cloudinit.render_install_block(distro.get_profile("ubuntu"), ("docker",))
        # dedup: ["docker","docker"] == ["docker"]
        self.assertEqual(out_ab, out_ba)
        # canonical order across a synthetic two-capability set via the public
        # canonicalizer (the one source of order for every consumer)
        self.assertEqual(
            capabilities.canonical_requires(("b", "a", "b")),
            capabilities.canonical_requires(("a", "b")),
        )

    def test_operator_and_pubkey_injected(self) -> None:
        out = cloudinit.render_cloudinit(distro.get_profile("ubuntu"), "alice", "ssh-ed25519 KEY alice", ())
        self.assertIn("alice", out)
        self.assertIn("ssh-ed25519 KEY alice", out)
        # all logic inside a main function; no global-scope mutable vars
        self.assertIn("vmlease_cloudinit_main", out)
        # system refresh + sudoers validation folded in
        self.assertIn("apt-get upgrade", out)
        self.assertIn("visudo -c -f", out)

    def test_arch_extra_setup_nf_tables(self) -> None:
        out = cloudinit.render_cloudinit(distro.get_profile("arch"), "probe", "ssh-ed25519 AAAA", ())
        self.assertIn("nf_tables", out)
        self.assertIn("pacman", out)

    def test_fedora_dnf(self) -> None:
        out = cloudinit.render_cloudinit(distro.get_profile("fedora"), "probe", "ssh-ed25519 AAAA", ())
        self.assertIn("dnf -y install", out)

    def test_unknown_manager_raises(self) -> None:
        bad = distro.DistroProfile(key="x", default_image="img", package_manager="zypper", packages=("p",))
        with self.assertRaises(cloudinit.CloudInitError):
            cloudinit.render_install_block(bad, ())

    # --- finalize fragment: native-image distros set the sentinel in place -- #
    def test_native_distro_finalize_is_byte_identical_ending(self) -> None:
        # The default finalize fragment must reproduce the pre-fragment ending
        # EXACTLY — non-rescue provisioning is unchanged in render, not just
        # behavior. Capture the expected final-step text and assert equality.
        out = cloudinit.render_cloudinit(distro.get_profile("ubuntu"), "probe", "ssh-ed25519 AAAA", ())
        expected_ending = (
            "  # --- readiness sentinel the harness polls for over SSH ------------------\n"
            "  touch /var/lib/vmlease-ready\n"
            '  echo "vmlease cloud-init complete for $operator (ubuntu)"\n'
            "}\n\n"
            'vmlease_cloudinit_main "$@"\n'
        )
        self.assertTrue(
            out.endswith(expected_ending),
            msg=f"native finalize ending drifted; got tail:\n{out[-len(expected_ending):]!r}",
        )
        # The native path must NOT carry any reboot-resume machinery.
        self.assertNotIn("systemctl reboot", out)
        self.assertNotIn("vmlease-ready.service", out)

    def test_native_finalize_fragment_selected_default(self) -> None:
        self.assertEqual(distro.get_profile("ubuntu").finalize_fragment, distro.FINALIZE_FRAGMENT_DEFAULT)
        self.assertEqual(distro.get_profile("fedora").finalize_fragment, distro.FINALIZE_FRAGMENT_DEFAULT)

    # --- finalize fragment: rescue-write distros reboot into the new kernel - #
    def test_rescue_write_finalize_fragment_selected_reboot_resume(self) -> None:
        # Selection is profile data keyed on needs_rescue_write — arch is the
        # only rescue-write distro today.
        arch = distro.get_profile("arch")
        self.assertTrue(arch.needs_rescue_write)
        self.assertEqual(arch.finalize_fragment, distro.FINALIZE_FRAGMENT_RESCUE_WRITE)

    def test_arch_finalize_reboots_and_defers_sentinel(self) -> None:
        out = cloudinit.render_cloudinit(distro.get_profile("arch"), "probe", "ssh-ed25519 AAAA", ())
        # (a) kernel-bump detection via the running kernel's modules.dep going missing
        self.assertIn('test -f "/lib/modules/$(uname -r)/modules.dep"', out)
        # (b) once-only reboot guard marker — written AND the reboot branch is
        # actually GATED on it (the marker is checked, not merely created), so a
        # later boot that re-enters this path does not reboot a second time.
        self.assertIn("/var/lib/vmlease-kernel-reboot.done", out)
        self.assertIn('! test -f "$vmlease_reboot_guard"', out)
        # (c) self-disabling systemd oneshot that touches the sentinel next boot
        self.assertIn("/etc/systemd/system/vmlease-ready.service", out)
        self.assertIn("Type=oneshot", out)
        self.assertIn("systemctl disable vmlease-ready.service", out)
        # daemon-reload precedes enable so a not-yet-loaded unit can't make
        # `enable` exit non-zero and abort before the reboot under pipefail
        self.assertIn("systemctl daemon-reload", out)
        self.assertLess(out.index("systemctl daemon-reload"), out.index("systemctl enable vmlease-ready.service"))
        self.assertIn("rm -f /etc/systemd/system/vmlease-ready.service", out)
        # ordered after modules are loaded so the nf_tables/ip_tables drop-in lands
        self.assertIn("After=systemd-modules-load.service", out)
        # (d) the actual reboot
        self.assertIn("systemctl reboot", out)
        # (e) the no-bump / post-reboot branch still touches the sentinel as today
        self.assertIn("touch /var/lib/vmlease-ready", out)

    def test_arch_does_not_touch_sentinel_before_reboot(self) -> None:
        # On the bumped-kernel path the sentinel must NOT be touched before the
        # reboot — the harness must not connect until the upgraded kernel runs.
        # Assert the reboot precedes the (sole) post-reboot touch in the rendered
        # script's bumped-kernel branch.
        out = cloudinit.render_cloudinit(distro.get_profile("arch"), "probe", "ssh-ed25519 AAAA", ())
        reboot_at = out.index("systemctl reboot")
        # The finalizing sentinel is the bare, indented `touch` command in the
        # else-branch — distinct from the oneshot unit's `ExecStart=/usr/bin/touch`
        # (which legitimately precedes the reboot, as the unit must be written
        # before rebooting). The bare command must come AFTER the reboot.
        bare_touch_at = out.index("    touch /var/lib/vmlease-ready")
        self.assertLess(reboot_at, bare_touch_at, "sentinel touched before reboot on the bumped-kernel path")

    def test_finalize_render_seam_strict_for_all_default_distros(self) -> None:
        # Smoke: the strict render seam holds for arch (reboot-resume) and a
        # native distro — no unfilled or extra slots.
        for key in ("ubuntu", "arch"):
            with self.subTest(distro=key):
                out = cloudinit.render_cloudinit(distro.get_profile(key), "probe", "ssh-ed25519 AAAA", ())
                self.assertNotIn("@@", out)

    def test_unknown_finalize_fragment_raises(self) -> None:
        # A profile whose finalize_fragment names a nonexistent template fails
        # loud at the render seam (mirrors the unknown-install-manager guard).
        class _BadFinalizeProfile(distro.DistroProfile):
            @property
            def finalize_fragment(self) -> str:
                return "does-not-exist"

        bad = _BadFinalizeProfile(key="x", default_image="img", package_manager="apt", packages=("p",))
        with self.assertRaises(cloudinit.CloudInitError):
            cloudinit.render_finalize_block(bad)


# --------------------------------------------------------------------------- #
# cloudinit — minimal restore render + machine-id sysprep (D7, F-009)
# --------------------------------------------------------------------------- #
class TestMinimalCloudInit(unittest.TestCase):
    def test_minimal_render_authorizes_key_and_touches_sentinel(self) -> None:
        out = cloudinit.render_minimal_cloudinit("alice", "ssh-ed25519 RESTOREKEY alice")
        # operator name + the fresh per-run pubkey are injected
        self.assertIn("alice", out)
        self.assertIn("ssh-ed25519 RESTOREKEY alice", out)
        # the .ssh dir + authorized_keys install (mirrors the base template)
        self.assertIn('install -d -m 0700 -o "$operator" -g "$operator" "/home/$operator/.ssh"', out)
        self.assertIn('"/home/$operator/.ssh/authorized_keys"', out)
        # the readiness sentinel is re-asserted
        self.assertIn("touch /var/lib/vmlease-ready", out)
        # hardened header + no unfilled slots
        self.assertIn("set -Eeuo pipefail", out)
        self.assertNotIn("@@", out)

    def test_minimal_render_does_nothing_else(self) -> None:
        # The "nothing else" guarantee: the restore path must carry NONE of the
        # cold-path prep — package install, system update, sudoers/account
        # creation or rescue-write machinery (all baked into the snapshot).
        out = cloudinit.render_minimal_cloudinit("probe", "ssh-ed25519 AAAA")
        for forbidden in (
            "apt-get",
            "dnf -y install",
            "pacman",
            "useradd",
            "visudo",
            "/etc/sudoers.d",
            "systemctl reboot",
            "vmlease-ready.service",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, out)

    def test_minimal_render_strips_pubkey_whitespace(self) -> None:
        # Mirrors render_cloudinit: a trailing newline on the key must not leak
        # a blank line into the authorized_keys heredoc.
        out = cloudinit.render_minimal_cloudinit("probe", "ssh-ed25519 KEY\n")
        self.assertIn("ssh-ed25519 KEY\nPUBKEY", out)

    def test_sysprep_resets_machine_id_to_uninitialized_sentinel(self) -> None:
        # E-012 10.1 (real-host, 2026-06-12), two findings, both proven by
        # build→restore-x3 runs: (1) the reset VALUE is systemd's golden-image
        # sentinel ``uninitialized`` (a present file regenerated on first boot), not
        # truncate (empty) or rm (absent); (2) ``sync`` is LOAD-BEARING — without it
        # the reset is not durable on the snapshot and every restore reads the
        # builder's id (dropping sync → 3 hosts shared one id; with it → 3 distinct).
        self.assertIn("uninitialized", cloudinit.SYSPREP_COMMAND)
        self.assertIn("/etc/machine-id", cloudinit.SYSPREP_COMMAND)
        self.assertNotIn("truncate", cloudinit.SYSPREP_COMMAND)
        # the dbus copy/symlink is cleared so it re-derives from the regenerated id
        self.assertIn("/var/lib/dbus/machine-id", cloudinit.SYSPREP_COMMAND)
        # sync is load-bearing: the reset must be flushed before the snapshot
        self.assertIn("sync", cloudinit.SYSPREP_COMMAND)


# --------------------------------------------------------------------------- #
# keypair — generate via injected runner; cleanup
# --------------------------------------------------------------------------- #
class TestKeypair(unittest.TestCase):
    def test_generate_success(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            def fake_keygen(argv: list[str]) -> subprocess.CompletedProcess[str]:
                # ssh-keygen writes id_ed25519.pub next to -f; emulate it
                idx = argv.index("-f")
                Path(argv[idx + 1] + ".pub").write_text("ssh-ed25519 AAAA probe\n", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, "", "")

            kp = keypair.generate_keypair("run1", runner=fake_keygen, base_dir=Path(d))
            self.assertEqual(kp.public_key, "ssh-ed25519 AAAA probe")
            self.assertTrue(kp.directory.exists())
            kp.cleanup()
            self.assertFalse(kp.directory.exists())

    def test_generate_keygen_failure_raises_and_cleans(self) -> None:
        with tempfile.TemporaryDirectory() as d, self.assertRaises(keypair.KeypairError):
            keypair.generate_keypair("run1", runner=_fake_subprocess(1, "", "nope"), base_dir=Path(d))

    def test_generate_empty_pubkey_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            def fake_keygen(argv: list[str]) -> subprocess.CompletedProcess[str]:
                idx = argv.index("-f")
                Path(argv[idx + 1] + ".pub").write_text("", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with self.assertRaises(keypair.KeypairError):
                keypair.generate_keypair("run1", runner=fake_keygen, base_dir=Path(d))

    def test_build_keygen_argv(self) -> None:
        argv = keypair.build_keygen_argv(Path("/tmp/k"), "vmlease-r1")
        self.assertEqual(argv[:4], ["ssh-keygen", "-t", "ed25519", "-N"])


# --------------------------------------------------------------------------- #
# ssh — argv builder + run_probe + readiness poll
# --------------------------------------------------------------------------- #
class TestSsh(unittest.TestCase):
    def _host(self) -> Host:
        return Host(id="1", name="n", ipv4="9.9.9.9")

    def test_build_ssh_argv(self) -> None:
        argv = ssh.build_ssh_argv(self._host(), "probe", Path("/tmp/k"), "id")
        self.assertIn("probe@9.9.9.9", argv)
        self.assertEqual(argv[-1], "id")
        self.assertIn("BatchMode=yes", argv)
        # recycled-IP safety: discard the persistent host-key store so a reused
        # IP with a new host key is never a refused connection (run-2 bug).
        self.assertIn("UserKnownHostsFile=/dev/null", argv)
        self.assertIn("StrictHostKeyChecking=accept-new", argv)
        # quiet the per-connect "Permanently added" banner (accept-new emits it on
        # every connection) so it never pollutes captured probe stderr — load-bearing
        # for the [probe.assert] stderr_empty/stderr_lacks/stderr_matches_not checks.
        self.assertIn("LogLevel=ERROR", argv)

    def test_run_probe_captures_exit(self) -> None:
        r = OpenSshRunnerForTest(_fake_ssh_subprocess(7, "out", "err"))
        probe = Probe(id="P1", title="t", command="c", tag=ProbeTag.READ_ONLY)
        res = r.run_probe(self._host(), probe)
        self.assertEqual((res.exit_code, res.stdout), (7, "out"))
        self.assertFalse(res.timed_out)  # a within-timeout probe is not a timeout

    def _assert(self, key: str, value: object) -> Assertion:
        return assertions._ASSERTIONS[key].build(value)

    def test_run_probe_no_assertion_ok_is_exit_zero(self) -> None:
        # No assertions declared → ok iff exit 0 (the relocated old rule).
        r0 = OpenSshRunnerForTest(_fake_ssh_subprocess(0, "out", ""))
        r3 = OpenSshRunnerForTest(_fake_ssh_subprocess(3, "out", "err"))
        probe = Probe(id="P1", title="t", command="c", tag=ProbeTag.READ_ONLY)
        self.assertTrue(r0.run_probe(self._host(), probe).ok)
        self.assertFalse(r3.run_probe(self._host(), probe).ok)

    def test_run_probe_assertion_decided_ok_and_failures(self) -> None:
        # A declared assertion decides ok; a satisfied one → ok with no failures.
        r = OpenSshRunnerForTest(_fake_ssh_subprocess(0, "READY now", ""))
        probe = Probe(
            id="P1", title="t", command="c", tag=ProbeTag.READ_ONLY,
            assertions=(self._assert("stdout_has", "READY"),),
        )
        res = r.run_probe(self._host(), probe)
        self.assertTrue(res.ok)
        self.assertEqual(res.assertion_failures, ())

    def test_run_probe_assertion_failure_records_description(self) -> None:
        # A failed assertion → not ok and its describe() lands in assertion_failures.
        r = OpenSshRunnerForTest(_fake_ssh_subprocess(0, "nope", ""))
        probe = Probe(
            id="P1", title="t", command="c", tag=ProbeTag.READ_ONLY,
            assertions=(self._assert("stdout_has", "READY"),),
        )
        res = r.run_probe(self._host(), probe)
        self.assertFalse(res.ok)
        self.assertEqual(len(res.assertion_failures), 1)
        self.assertIn("stdout_has", res.assertion_failures[0])

    def test_run_probe_refusal_exit_not_passes_on_nonzero_exit(self) -> None:
        # A refusal assertion ``exit_not = 0`` PASSES on a non-zero exit — the
        # assertion verdict, not the exit code, decides ok.
        r = OpenSshRunnerForTest(_fake_ssh_subprocess(13, "", "denied"))
        probe = Probe(
            id="P1", title="t", command="c", tag=ProbeTag.READ_ONLY,
            assertions=(self._assert("exit_not", 0),),
        )
        res = r.run_probe(self._host(), probe)
        self.assertTrue(res.ok)  # exit was non-zero → exit_not 0 holds
        self.assertEqual(res.assertion_failures, ())

    def test_run_probe_timed_out_refusal_not_ok_despite_satisfiable_assertion(self) -> None:
        # A timed-out probe is not ok even when its exit assertion WOULD be
        # satisfiable — the evaluator is never invoked on the killed result (D10(E)).
        def slow(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(argv, timeout, output="", stderr="")

        r = OpenSshRunnerForTest(slow)
        probe = Probe(
            id="P1", title="t", command="c", tag=ProbeTag.READ_ONLY,
            assertions=(self._assert("exit_not", 0),),  # 124 != 0 → would pass
        )
        res = r.run_probe(self._host(), probe)
        self.assertTrue(res.timed_out)
        self.assertFalse(res.ok)
        self.assertEqual(res.assertion_failures, ())  # evaluator never ran

    def test_run_probe_blocking_seam_records_timed_out_result(self) -> None:
        # T1: a slow/blocking transport that raises TimeoutExpired is RECORDED as a
        # timed-out result (exit 124, timed_out=True, partial output), not raised
        # and not a hang.
        def slow(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(argv, timeout, output="partial-out", stderr="partial-err")

        r = OpenSshRunnerForTest(slow)
        probe = Probe(id="P1", title="t", command="c", tag=ProbeTag.READ_ONLY)
        res = r.run_probe(self._host(), probe)
        self.assertTrue(res.timed_out)
        self.assertEqual(res.exit_code, 124)
        self.assertFalse(res.ok)
        self.assertIn("partial-out", res.stdout)  # best-effort partial preserved
        self.assertIn("partial-err", res.stderr)
        self.assertIn("timed out after", res.stderr)  # the note is appended

    def test_run_probe_timeout_empty_partial_is_fine(self) -> None:
        # a platform that doesn't populate partial output → empty, still recorded.
        def slow(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(argv, timeout)

        r = OpenSshRunnerForTest(slow)
        probe = Probe(id="P1", title="t", command="c", tag=ProbeTag.READ_ONLY)
        res = r.run_probe(self._host(), probe)
        self.assertTrue(res.timed_out)
        self.assertEqual(res.stdout, "")
        self.assertIn("timed out after", res.stderr)

    def test_run_probe_resolves_per_probe_timeout(self) -> None:
        # the probe's own timeout wins over the runner default and reaches the seam.
        seen: list[float] = []

        def capture(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            seen.append(timeout)
            return subprocess.CompletedProcess(argv, 0, "", "")

        r = ssh.OpenSshRunner(
            "probe", Path("/tmp/k"), runner=capture, sleeper=lambda _x: None, probe_timeout_default=600.0
        )
        probe = Probe(id="P1", title="t", command="c", tag=ProbeTag.READ_ONLY, timeout=12.5)
        r.run_probe(self._host(), probe)
        self.assertEqual(seen, [12.5])

    def test_run_probe_uses_runner_default_when_probe_has_none(self) -> None:
        seen: list[float] = []

        def capture(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            seen.append(timeout)
            return subprocess.CompletedProcess(argv, 0, "", "")

        r = ssh.OpenSshRunner(
            "probe", Path("/tmp/k"), runner=capture, sleeper=lambda _x: None, probe_timeout_default=77.0
        )
        probe = Probe(id="P1", title="t", command="c", tag=ProbeTag.READ_ONLY)
        r.run_probe(self._host(), probe)
        self.assertEqual(seen, [77.0])

    def test_build_ssh_argv_has_no_tt(self) -> None:
        # the probe argv must NOT force a PTY (-tt) — that would merge stdout/stderr.
        argv = ssh.build_ssh_argv(self._host(), "probe", Path("/tmp/k"), "id")
        self.assertNotIn("-tt", argv)

    def test_build_scp_argv(self) -> None:
        argv = ssh.build_scp_argv(self._host(), "probe", Path("/tmp/k"), Path("/src/w.whl"), "~/w.whl")
        self.assertEqual(argv[0], "scp")
        # the same recycled-IP hardening as build_ssh_argv
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("UserKnownHostsFile=/dev/null", argv)
        self.assertIn("StrictHostKeyChecking=accept-new", argv)
        self.assertIn("ConnectTimeout=10", argv)
        self.assertIn("-i", argv)
        # the -- option terminator precedes the two positional path args
        self.assertEqual(argv[-3:], ["--", "/src/w.whl", "probe@9.9.9.9:~/w.whl"])

    def test_upload_runs_scp_via_seam(self) -> None:
        seen: list[list[str]] = []

        def runner_fn(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn, sleeper=lambda _x: None)
        r.upload(self._host(), Path("/src/w.whl"), "~/w.whl")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "scp")
        self.assertEqual(seen[0][-1], "probe@9.9.9.9:~/w.whl")

    def test_upload_raises_ssh_error_on_nonzero(self) -> None:
        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=_fake_ssh_subprocess(1, "", "no route"), sleeper=lambda _x: None)
        with self.assertRaises(ssh.SshError):
            r.upload(self._host(), Path("/src/w.whl"), "~/w.whl")

    def test_upload_timeout_raises_ssh_error_not_timeout_expired(self) -> None:
        # a hung transfer (seam raises TimeoutExpired) becomes SshError, not a raw
        # TimeoutExpired — the transport contract the caller catches.
        def runner_fn(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(argv, timeout)

        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn, sleeper=lambda _x: None)
        with self.assertRaises(ssh.SshError):
            r.upload(self._host(), Path("/src/w.whl"), "~/w.whl")

    def test_wait_until_ready_polls_then_succeeds(self) -> None:
        # fail twice, then the sentinel lands, then the module-tree probe passes
        # (the 4th call): 3 sentinel polls + the in-loop module-tree assertion.
        seq = [1, 1, 0, 0]
        calls = {"n": 0}

        def runner_fn(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            rc = seq[min(calls["n"], len(seq) - 1)]
            calls["n"] += 1
            return subprocess.CompletedProcess(argv, rc, "6.9.0-arch1\n", "")

        slept: list[float] = []
        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn, sleeper=slept.append)
        r.wait_until_ready(self._host(), attempts=5)
        self.assertEqual(calls["n"], 4)  # 3 sentinel polls + 1 module-tree assertion

    def test_wait_until_ready_times_out(self) -> None:
        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=_fake_ssh_subprocess(1), sleeper=lambda _x: None)
        with self.assertRaises(ssh.SshError):
            r.wait_until_ready(self._host(), attempts=2)

    def test_wait_until_ready_default_budget_spans_a_reboot(self) -> None:
        # the default attempt budget must comfortably span a Hetzner reboot
        # (≈4 min): reboot-resume only touches the sentinel post-reboot. At
        # ≤2s/attempt the budget must clear ~240s.
        import inspect

        default = inspect.signature(ssh.OpenSshRunner.wait_until_ready).parameters["attempts"].default
        self.assertGreaterEqual(default, 120)
        self.assertGreaterEqual(default * 2.0, 240.0)  # 2s backoff cap x budget >= 4 min

    def test_wait_until_ready_rejects_missing_module_tree(self) -> None:
        # sentinel present (exit 0), but the module-tree assertion exits non-zero
        # with the running kernel on stdout: readiness must fail with the skew.
        seen_commands: list[str] = []

        def runner_fn(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            command = argv[-1]
            seen_commands.append(command)
            if command == "test -f /var/lib/vmlease-ready":
                return subprocess.CompletedProcess(argv, 0, "", "")
            # the module-tree probe: stdout carries the version, exit != 0 = skew
            return subprocess.CompletedProcess(argv, 1, "6.9.0-arch1\n", "")

        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn, sleeper=lambda _x: None)
        with self.assertRaises(ssh.SshError) as ctx:
            r.wait_until_ready(self._host())
        msg = str(ctx.exception)
        self.assertIn("6.9.0-arch1", msg)  # names the running kernel version
        self.assertIn("rescue/modules skew", msg)  # identifies the skew
        # the module-tree assertion actually ran (not just the sentinel)
        self.assertTrue(any("modules.dep" in c for c in seen_commands))

    def test_wait_until_ready_healthy_host_passes(self) -> None:
        # sentinel present AND module-tree probe exits 0 → ready, returns normally.
        def runner_fn(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, "6.9.0-arch1\n", "")

        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn, sleeper=lambda _x: None)
        r.wait_until_ready(self._host())  # no raise == ready

    def test_run_probe_runs_command_verbatim_regardless_of_tag(self) -> None:
        # Scenario #12: the runner executes the command VERBATIM — it does NOT
        # strip/inject/refuse `sudo` based on the tag. A non-host-root probe whose
        # command invokes sudo still reaches the transport unchanged; a tag<->sudo
        # mismatch is lint's concern, not the runner's.
        captured: list[str] = []

        def capture(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            captured.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, "", "")

        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=capture, sleeper=lambda _x: None)
        probe = Probe(id="P", title="t", command="sudo systemctl restart foo", tag=ProbeTag.READ_ONLY)
        r.run_probe(self._host(), probe)
        self.assertEqual(captured[-1], "sudo systemctl restart foo")

    def test_wait_until_ready_retries_module_probe_transport_blip(self) -> None:
        # Right after the post-reboot oneshot touches the sentinel the host is
        # fresh from a reboot: the module probe can hit a transient SSH transport
        # failure (exit 255, EMPTY stdout — the remote command never ran). That
        # must NOT be misread as a skew; the loop retries and succeeds once a
        # later module probe returns exit 0 with a version.
        seq: list[tuple[int, str]] = [
            (0, ""),  # sentinel present
            (255, ""),  # module probe: transport blip, empty stdout → retry
            (0, ""),  # sentinel present
            (0, "6.9.0-arch1\n"),  # module probe: real success → ready
        ]
        calls = {"n": 0}

        def runner_fn(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            rc, out = seq[min(calls["n"], len(seq) - 1)]
            calls["n"] += 1
            return subprocess.CompletedProcess(argv, rc, out, "")

        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn, sleeper=lambda _x: None)
        r.wait_until_ready(self._host(), attempts=5)  # no raise == retried past the blip
        self.assertEqual(calls["n"], 4)  # blip retried, did not raise skew

    def test_ssh_runner_protocol_satisfied(self) -> None:
        self.assertIsInstance(FakeSshRunner(), ssh.SshRunner)

    # --- D9.2 streaming command execution + hard-timeout/kill ---

    def test_build_ssh_stream_argv_forces_pty_and_keepalive(self) -> None:
        argv = ssh.build_ssh_stream_argv(self._host(), "probe", Path("/tmp/k"), "make check")
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-tt", argv)  # PTY → the dropped client SIGHUPs the remote
        self.assertIn("ServerAliveInterval=15", argv)
        self.assertIn("ServerAliveCountMax=3", argv)
        self.assertIn("UserKnownHostsFile=/dev/null", argv)  # recycled-IP hardening preserved
        self.assertEqual(argv[-1], "make check")

    def test_run_streaming_delivers_output_and_returns_exit(self) -> None:
        seen_argv: list[list[str]] = []

        def fake_stream(argv: list[str], on_output: Callable[[str], None], timeout: float) -> int:
            seen_argv.append(argv)
            on_output("building\n")
            on_output("FAILED\n")
            return 2  # a non-zero gate exit is DATA, not a transport error

        chunks: list[str] = []
        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), stream_runner=fake_stream)
        rc = r.run_streaming(self._host(), "make check", chunks.append, timeout=60.0)
        self.assertEqual(rc, 2)
        self.assertEqual(chunks, ["building\n", "FAILED\n"])  # delivered incrementally
        self.assertIn("-tt", seen_argv[0])
        self.assertEqual(seen_argv[0][-1], "make check")

    def test_run_streaming_timeout_raises_ssh_error(self) -> None:
        def fake_stream(argv: list[str], on_output: Callable[[str], None], timeout: float) -> int:
            raise ssh.SshError(f"timed out after {timeout}s and was killed")

        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), stream_runner=fake_stream)
        with self.assertRaises(ssh.SshError):
            r.run_streaming(self._host(), "sleep 999", lambda _s: None, timeout=1.0)

    def test_default_stream_runner_streams_local_output_and_returns_exit(self) -> None:
        # the REAL default impl, exercised with a LOCAL command (no ssh, no socket):
        # proves the pump-thread + line delivery + exit-code path.
        chunks: list[str] = []
        rc = ssh._default_stream_runner(["printf", "alpha\\nbeta\\n"], chunks.append, 10.0)
        self.assertEqual(rc, 0)
        self.assertEqual("".join(chunks), "alpha\nbeta\n")

    def test_default_stream_runner_nonzero_exit_returned(self) -> None:
        rc = ssh._default_stream_runner(["sh", "-c", "exit 5"], lambda _s: None, 10.0)
        self.assertEqual(rc, 5)  # a non-zero exit is returned, not raised

    def test_default_stream_runner_timeout_kills_and_raises(self) -> None:
        # a local command that outlives the timeout is killed and raises SshError
        with self.assertRaises(ssh.SshError):
            ssh._default_stream_runner(["sleep", "10"], lambda _s: None, 0.2)

    # --- D9.3 recursive directory push (safe symlinks) ---

    def test_build_rsync_argv_safe_links_and_hardening(self) -> None:
        argv = ssh.build_rsync_argv(self._host(), "probe", Path("/tmp/k"), Path("/src/tree"), "~/dest")
        self.assertEqual(argv[0], "rsync")
        self.assertIn("-a", argv)
        self.assertIn("--safe-links", argv)  # out-of-tree symlinks dropped — no exfil
        e_idx = argv.index("-e")
        self.assertIn("UserKnownHostsFile=/dev/null", argv[e_idx + 1])  # hardening rides in -e ssh
        self.assertIn("StrictHostKeyChecking=accept-new", argv[e_idx + 1])
        # -- guard, then source-with-trailing-slash (contents-into-dest), then dest
        self.assertEqual(argv[-3:], ["--", "/src/tree/", "probe@9.9.9.9:~/dest"])

    def test_build_rsync_argv_quotes_key_path_with_space(self) -> None:
        # a space in the key path must not word-split the -e ssh value
        argv = ssh.build_rsync_argv(self._host(), "probe", Path("/tmp/my key/id"), Path("/src/tree"), "~/dest")
        e_val = argv[argv.index("-e") + 1]
        self.assertIn("'/tmp/my key/id'", e_val)

    def test_upload_dir_runs_rsync_via_seam(self) -> None:
        seen: list[list[str]] = []

        def runner_fn(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as d:
            r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn)
            r.upload_dir(self._host(), Path(d), "~/dest")
        self.assertEqual(seen[0][0], "rsync")
        self.assertIn("--safe-links", seen[0])

    def test_upload_dir_raises_ssh_error_on_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=_fake_ssh_subprocess(23, "", "rsync failed"))
            with self.assertRaises(ssh.SshError):
                r.upload_dir(self._host(), Path(d), "~/dest")

    def test_upload_dir_timeout_raises_ssh_error_not_timeout_expired(self) -> None:
        # a hung directory push (seam raises TimeoutExpired) becomes SshError.
        def runner_fn(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(argv, timeout)

        with tempfile.TemporaryDirectory() as d:
            r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn)
            with self.assertRaises(ssh.SshError):
                r.upload_dir(self._host(), Path(d), "~/dest")

    def test_upload_dir_validates_source_before_transfer(self) -> None:
        seen: list[list[str]] = []

        def runner_fn(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn)
        with self.assertRaises(safety.UploadError):
            r.upload_dir(self._host(), Path("/no/such/dir"), "~/dest")
        self.assertEqual(seen, [])  # validated fail-closed before any rsync


def _fake_ssh_subprocess(
    returncode: int, stdout: str = "", stderr: str = ""
) -> Callable[[list[str], float], subprocess.CompletedProcess[str]]:
    """A 2-arg (argv, timeout) ssh subprocess seam returning a fixed result."""

    def _run(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return _run


def OpenSshRunnerForTest(
    runner_fn: Callable[[list[str], float], subprocess.CompletedProcess[str]],
) -> ssh.OpenSshRunner:
    return ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn, sleeper=lambda _x: None)


# --------------------------------------------------------------------------- #
# safety.reap
# --------------------------------------------------------------------------- #
class TestReap(unittest.TestCase):
    def test_reap_destroys_labeled(self) -> None:
        prov = FakeProvider()
        prov.create_with_cloudinit(HostSpec(name="n", image="i", server_type="cpx22", distro_key="ubuntu", labels={"vmlease": "r1"}), "ci")
        reaped = safety.reap(prov, "r1")
        self.assertEqual(len(reaped), 1)
        self.assertEqual(len(prov.destroyed), 1)


# --------------------------------------------------------------------------- #
# safety — upload source / remote-dest validators (fail-closed before spend)
# --------------------------------------------------------------------------- #
class TestUploadValidation(unittest.TestCase):
    def test_source_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "w.whl"
            f.write_bytes(b"WHEEL")
            safety.validate_upload_source(f)  # no raise

    def test_source_missing_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_source(Path(d) / "nope.whl")
            self.assertIn("does not exist", str(cm.exception))

    def test_source_symlink_final_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "real.whl"
            target.write_bytes(b"WHEEL")
            link = Path(d) / "link.whl"
            os.symlink(target, link)
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_source(link)
            self.assertIn("symlink", str(cm.exception))

    def test_source_symlink_in_parent_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            realdir = Path(d) / "realdir"
            realdir.mkdir()
            f = realdir / "w.whl"
            f.write_bytes(b"WHEEL")
            linkdir = Path(d) / "linkdir"
            os.symlink(realdir, linkdir)
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_source(linkdir / "w.whl")
            self.assertIn("symlink component", str(cm.exception))

    def test_source_directory_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_source(Path(d))
            self.assertIn("not a regular file", str(cm.exception))

    def test_source_fifo_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            fifo = Path(d) / "pipe"
            os.mkfifo(fifo)
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_source(fifo)
            self.assertIn("not a regular file", str(cm.exception))

    def test_source_unreadable_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "w.whl"
            f.write_bytes(b"WHEEL")
            os.chmod(f, 0o000)
            try:
                with self.assertRaises(safety.UploadError) as cm:
                    safety.validate_upload_source(f)
                self.assertIn("not readable", str(cm.exception))
            finally:
                os.chmod(f, 0o600)  # restore so TemporaryDirectory can clean up

    def test_dir_source_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            safety.validate_upload_dir_source(Path(d))  # no raise — a plain readable dir

    def test_dir_source_missing_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_dir_source(Path(d) / "nope")
            self.assertIn("does not exist", str(cm.exception))

    def test_dir_source_symlink_final_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            realdir = Path(d) / "realdir"
            realdir.mkdir()
            link = Path(d) / "linkdir"
            os.symlink(realdir, link)
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_dir_source(link)
            self.assertIn("symlink", str(cm.exception))

    def test_dir_source_symlink_in_parent_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            realdir = Path(d) / "realdir"
            (realdir / "sub").mkdir(parents=True)
            linkdir = Path(d) / "linkdir"
            os.symlink(realdir, linkdir)
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_dir_source(linkdir / "sub")
            self.assertIn("symlink component", str(cm.exception))

    def test_dir_source_non_directory_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "w.whl"
            f.write_bytes(b"WHEEL")
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_dir_source(f)
            self.assertIn("not a directory", str(cm.exception))

    def test_dir_source_unreadable_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d) / "sub"
            sub.mkdir()
            os.chmod(sub, 0o000)
            try:
                with self.assertRaises(safety.UploadError) as cm:
                    safety.validate_upload_dir_source(sub)
                self.assertIn("not readable", str(cm.exception))
            finally:
                os.chmod(sub, 0o700)  # restore so TemporaryDirectory can clean up

    def test_remote_happy_path(self) -> None:
        safety.validate_remote_dest("~/w.whl")  # no raise
        safety.validate_remote_dest("/opt/app/wheels/w-1.0+local.whl")  # no raise

    def test_remote_empty_refused(self) -> None:
        with self.assertRaises(safety.UploadError) as cm:
            safety.validate_remote_dest("")
        self.assertIn("empty", str(cm.exception))

    def test_remote_traversal_refused(self) -> None:
        with self.assertRaises(safety.UploadError) as cm:
            safety.validate_remote_dest("~/../../etc/cron.d/x")
        self.assertIn("..", str(cm.exception))

    def test_remote_leading_dash_refused(self) -> None:
        with self.assertRaises(safety.UploadError) as cm:
            safety.validate_remote_dest("-oProxyCommand=evil")
        self.assertIn("-", str(cm.exception))

    def test_remote_bad_char_refused(self) -> None:
        with self.assertRaises(safety.UploadError) as cm:
            safety.validate_remote_dest("~/w.whl; rm -rf /")
        self.assertIn("disallowed character", str(cm.exception))


# --------------------------------------------------------------------------- #
# results serialization
# --------------------------------------------------------------------------- #
class TestResults(unittest.TestCase):
    def _run(self) -> model.HostRun:
        spec = HostSpec(name="vmlease-r1-ubuntu", image="ubuntu-24.04", server_type="cpx22", distro_key="ubuntu")
        res = (
            make_probe_result("P1", ProbeTag.READ_ONLY, 0, "ok"),
            make_probe_result("P2", ProbeTag.READ_ONLY, 124, "", "probe timed out after 5.0s", timed_out=True),
        )
        return model.HostRun(host_spec=spec, detail="## os-release\nID=ubuntu", results=res)

    def test_filename_deterministic(self) -> None:
        self.assertEqual(results.results_filename("r1", "20260601T000000Z"), "vmlease-r1-20260601T000000Z.json")

    def test_results_never_reads_the_wall_clock(self) -> None:
        # Scenario #38: the timestamp is purely caller-supplied; the library reads
        # NO wall clock (runs stay reproducible; tests pin the filename). Two guards:
        # (a) the full API still produces output with `time` sabotaged to raise;
        # (b) the module source has no clock CALL (regression guard, any import style).
        import inspect
        import time as _time
        from unittest import mock

        def _boom(*_a: object, **_k: object) -> float:
            raise AssertionError("vmlease.results must not read the wall clock")

        with mock.patch.object(_time, "time", _boom), mock.patch.object(_time, "monotonic", _boom):
            self.assertEqual(results.results_filename("r1", "TS"), "vmlease-r1-TS.json")
            self.assertIn("TS", results.serialize_run("r1", "TS", [self._run()]))
            with tempfile.TemporaryDirectory() as d:
                results.write_results(Path(d) / "out", "r1", "TS", [self._run()])

        src = inspect.getsource(results)
        for clock_call in ("time.time(", "time.monotonic(", "datetime.now(", "datetime.today(", "utcnow("):
            self.assertNotIn(clock_call, src, f"results must not call the clock ({clock_call})")

    def test_serialize_round_trips(self) -> None:
        text = results.serialize_run("r1", "20260601T000000Z", [self._run()])
        doc = json.loads(text)
        self.assertEqual(doc["run_id"], "r1")
        self.assertEqual(doc["hosts"][0]["probes"][0]["id"], "P1")
        self.assertTrue(doc["hosts"][0]["probes"][0]["ok"])
        self.assertFalse(doc["hosts"][0]["probes"][0]["timed_out"])  # normal probe
        self.assertEqual(doc["hosts"][0]["probes"][1]["exit_code"], 124)
        self.assertTrue(doc["hosts"][0]["probes"][1]["timed_out"])  # timed-out probe is marked in the JSON

    def test_serialize_passing_assertion_probe_carries_declared_count_true(self) -> None:
        # (7.5) DECLARED-count: a PASSING assertion probe (assertion_failures=())
        # serializes has_assertions=True and assertion_failures=[] — has_assertions
        # is the declared count, NOT derived from the (empty) failures list.
        spec = HostSpec(name="vmlease-r1-ubuntu", image="ubuntu-24.04", server_type="cpx22", distro_key="ubuntu")
        res = (make_probe_result("P1", ProbeTag.READ_ONLY, 0, "out", ok=True, has_assertions=True),)
        hr = model.HostRun(host_spec=spec, detail="ok", results=res)
        doc = json.loads(results.serialize_run("r1", "20260601T000000Z", [hr]))
        probe = doc["hosts"][0]["probes"][0]
        self.assertTrue(probe["has_assertions"])              # declared, even though it PASSED
        self.assertEqual(probe["assertion_failures"], [])

    def test_serialize_failing_assertion_probe_carries_failures(self) -> None:
        spec = HostSpec(name="vmlease-r1-ubuntu", image="ubuntu-24.04", server_type="cpx22", distro_key="ubuntu")
        res = (
            make_probe_result(
                "P1", ProbeTag.READ_ONLY, 0, "out", ok=False,
                has_assertions=True, assertion_failures=("stdout did not match /ready/",),
            ),
        )
        hr = model.HostRun(host_spec=spec, detail="ok", results=res)
        doc = json.loads(results.serialize_run("r1", "20260601T000000Z", [hr]))
        probe = doc["hosts"][0]["probes"][0]
        self.assertTrue(probe["has_assertions"])
        self.assertEqual(probe["assertion_failures"], ["stdout did not match /ready/"])

    def test_write_results_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = results.write_results(Path(d) / "out", "r1", "20260601T000000Z", [self._run()])
            self.assertTrue(path.exists())
            self.assertEqual(path.name, "vmlease-r1-20260601T000000Z.json")

    def _run_named(self, name: str) -> model.HostRun:
        spec = HostSpec(name=name, image="ubuntu-24.04", server_type="cpx22", distro_key="ubuntu")
        return model.HostRun(host_spec=spec, detail="ok", results=())

    def test_incremental_writer_path_known_before_first_write(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            w = results.IncrementalResultsWriter(Path(d) / "out", "r1", "20260601T000000Z")
            self.assertEqual(w.path.name, "vmlease-r1-20260601T000000Z.json")
            self.assertFalse(w.path.exists())  # deterministic, but nothing written yet

    def test_incremental_writer_accumulates_each_host(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            w = results.IncrementalResultsWriter(Path(d) / "out", "r1", "20260601T000000Z")
            path = w.add(self._run_named("host-a"))
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([h["name"] for h in doc["hosts"]], ["host-a"])  # first host present
            path2 = w.add(self._run_named("host-b"))
            self.assertEqual(path2, w.path)  # same deterministic file
            doc2 = json.loads(path2.read_text(encoding="utf-8"))
            self.assertEqual([h["name"] for h in doc2["hosts"]], ["host-a", "host-b"])  # both now


# --------------------------------------------------------------------------- #
# runner.execute — the teardown-ALWAYS guarantee
# --------------------------------------------------------------------------- #
class TestExecute(unittest.TestCase):
    def _matrix(self, distros: tuple[str, ...] = ("ubuntu", "debian")) -> runner.Matrix:
        return runner.Matrix(_demo_workload(), distros, "cpx22", "run-xyz")

    def _factory(self, ssh_runner: ssh.SshRunner) -> Callable[[str, keypair.Keypair], ssh.SshRunner]:
        return lambda _op, _kp: ssh_runner

    def test_happy_path_provisions_probes_teardown(self) -> None:
        prov = FakeProvider()
        fssh = FakeSshRunner()
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(self._matrix(), prov, self._factory(fssh), _fake_keypair(Path(d)), "probe")
        self.assertEqual(len(runs), 2)
        self.assertEqual(len(prov.created), 2)
        self.assertEqual(len(prov.destroyed), 2)  # ALWAYS torn down
        # battery ran in authoring order on each host (P1 first, P12 authored last)
        self.assertEqual(fssh.ran[-1], "P12")

    def _upload_matrix(self, remote: str = "~/w.whl") -> tuple[runner.Matrix, Path]:
        # a matrix carrying one upload; returns it plus the (real, valid) local file
        tmp = Path(tempfile.mkdtemp())
        local = tmp / "w.whl"
        local.write_bytes(b"WHEEL")
        m = runner.Matrix(
            _demo_workload(), ("ubuntu",), "cpx22", "run-up",
            uploads=(model.UploadSpec(local=local, remote=remote),),
        )
        return m, local

    def test_upload_precedes_first_probe(self) -> None:
        # the lifecycle contract: upload lands after readiness, before detail/battery
        prov = FakeProvider()
        fssh = FakeSshRunner()
        m, local = self._upload_matrix()
        with tempfile.TemporaryDirectory() as d:
            runner.execute(m, prov, self._factory(fssh), _fake_keypair(Path(d)), "probe")
        self.assertEqual(fssh.uploads, [(local, "~/w.whl")])
        # "upload:~/w.whl" appears in the call log BEFORE the first probe (_detail)
        self.assertEqual(fssh.ran[0], "upload:~/w.whl")
        self.assertEqual(fssh.ran[1], "_detail")

    def test_upload_failure_is_host_error_and_tears_down(self) -> None:
        # an upload that raises SshError is a transport host-failure (error HostRun),
        # NOT a probe non-zero — and the host is still torn down.
        prov = FakeProvider()
        failing = FakeSshRunner(raise_upload=True)
        m, _local = self._upload_matrix()
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(m, prov, self._factory(failing), _fake_keypair(Path(d)), "probe")
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0].detail.startswith("ERROR:"))  # transport failure recorded
        self.assertEqual(runs[0].results, ())  # no probes ran
        self.assertEqual(len(prov.destroyed), 1)  # torn down despite the failure

    def test_cloudinit_rendered_per_distro(self) -> None:
        prov = FakeProvider()
        # The distro-specific docker repo path (linux/<distro>) is now part of the
        # docker capability recipe, so render the docker variant to assert it.
        m = runner.Matrix(_demo_workload(), ("debian", "ubuntu"), "cpx22", "run-xyz", requires=("docker",))
        with tempfile.TemporaryDirectory() as d:
            runner.execute(m, prov, self._factory(FakeSshRunner()), _fake_keypair(Path(d)), "probe")
        self.assertIn("linux/debian", prov.cloud_inits[0])
        self.assertIn("linux/ubuntu", prov.cloud_inits[1])

    def test_teardown_fires_when_probe_raises(self) -> None:
        # load-bearing safety: a mid-probe transport raise still tears down AND
        # is recorded as an error HostRun (per-host isolation — no propagation).
        prov = FakeProvider()
        raising = FakeSshRunner(raise_on="P6")
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(self._matrix(("ubuntu",)), prov, self._factory(raising), _fake_keypair(Path(d)), "probe")
        self.assertEqual(len(prov.created), 1)
        self.assertEqual(len(prov.destroyed), 1)  # destroyed despite the raise
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0].detail.startswith("ERROR:"))  # recorded, not lost
        self.assertEqual(runs[0].results, ())

    def test_one_host_failure_does_not_discard_others(self) -> None:
        # the first-run lesson: a later host's failure must NOT lose earlier
        # hosts' results. ubuntu probes fine; debian's transport raises.
        prov = FakeProvider()

        class _SelectiveSsh:
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                if "debian" in host.name:
                    raise ssh.SshError("debian unreachable")
                return make_probe_result(probe.id, probe.tag, 0, "ok")

            def upload(self, host: Host, local: Path, remote: str) -> None:
                return None

            def wait_until_ready(self, host: Host) -> None:
                return None

            def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
                return 0

            def upload_dir(self, host: Host, local: Path, remote: str) -> None:
                return None

        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(self._matrix(("ubuntu", "debian")), prov, lambda _o, _k: _SelectiveSsh(), _fake_keypair(Path(d)), "probe")
        self.assertEqual(len(runs), 2)  # BOTH recorded
        self.assertEqual(len(prov.destroyed), 2)  # both torn down
        ubuntu = next(r for r in runs if r.host_spec.distro_key == "ubuntu")
        debian = next(r for r in runs if r.host_spec.distro_key == "debian")
        self.assertTrue(ubuntu.results)  # ubuntu data preserved
        self.assertTrue(debian.detail.startswith("ERROR:"))  # debian error captured

    def test_teardown_cleans_keypair(self) -> None:
        prov = FakeProvider()
        with tempfile.TemporaryDirectory() as d:
            kp = _fake_keypair(Path(d))
            runner.execute(self._matrix(("ubuntu",)), prov, self._factory(FakeSshRunner()), kp, "probe")
            self.assertFalse(kp.directory.exists())

    def test_probe_nonzero_exit_is_captured_not_raised(self) -> None:
        prov = FakeProvider()
        failing = FakeSshRunner(fail_on="P12")
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(self._matrix(("ubuntu",)), prov, self._factory(failing), _fake_keypair(Path(d)), "probe")
        p12 = next(r for r in runs[0].results if r.probe_id == "P12")
        self.assertFalse(p12.ok)  # captured, not raised

    def test_parallel_preserves_order_and_tears_down_all(self) -> None:
        prov = FakeProvider()
        m = runner.Matrix(_demo_workload(), ("ubuntu", "debian", "fedora"), "cpx22", "run-par")
        # a fresh FakeSshRunner per host (thread-safe-ish; each host independent)
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(m, prov, lambda _o, _k: FakeSshRunner(), _fake_keypair(Path(d)), "probe", max_parallel=3)
        # results in MATRIX order regardless of completion order
        self.assertEqual([r.host_spec.distro_key for r in runs], ["ubuntu", "debian", "fedora"])
        self.assertEqual(len(prov.created), 3)
        self.assertEqual(len(prov.destroyed), 3)  # all torn down

    def test_teardown_failure_does_not_lose_results(self) -> None:
        # a destroy that raises must NOT discard the probe data — it becomes a
        # WARNING note appended to the (preserved) HostRun. The first-run lesson.
        class _DestroyFails(FakeProvider):
            def destroy(self, host: Host) -> None:
                super().destroy(host)  # still record it locally
                raise providers.ProviderError("request timeout")

        prov = _DestroyFails()
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(self._matrix(("ubuntu",)), prov, self._factory(FakeSshRunner()), _fake_keypair(Path(d)), "probe")
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0].results)  # probe data preserved
        self.assertIn(runner.TEARDOWN_WARNING_PREFIX, runs[0].detail)  # failure noted, not raised

    def test_teardown_fires_on_base_exception_then_it_propagates(self) -> None:
        # T2: a workload raising a BaseException (KeyboardInterrupt) AFTER the host
        # is created → the host is STILL destroyed by the finally, and the
        # BaseException keeps propagating (the run aborts).
        prov = FakeProvider()

        class _Interrupting:
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                raise KeyboardInterrupt("operator hit Ctrl-C")

            def upload(self, host: Host, local: Path, remote: str) -> None:
                return None

            def wait_until_ready(self, host: Host) -> None:
                return None

            def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
                return 0

            def upload_dir(self, host: Host, local: Path, remote: str) -> None:
                return None

        with tempfile.TemporaryDirectory() as d, self.assertRaises(KeyboardInterrupt):
            runner.execute(self._matrix(("ubuntu",)), prov, lambda _o, _k: _Interrupting(), _fake_keypair(Path(d)), "probe")
        self.assertEqual(len(prov.created), 1)
        self.assertEqual(len(prov.destroyed), 1)  # torn down despite the BaseException

    def test_create_failure_records_error_and_skips_teardown(self) -> None:
        # F3: a failure BEFORE create_with_cloudinit succeeds → no host exists, so
        # the finally's host-is-None arm attempts NO teardown; the seeded error is
        # recorded as the HostRun (zero results) and the run is NOT aborted.
        class _CreateFails(FakeProvider):
            def create_with_cloudinit(self, spec: HostSpec, cloud_init: str) -> Host:
                raise providers.ProviderError("create rejected")

        prov = _CreateFails()
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(self._matrix(("ubuntu",)), prov, self._factory(FakeSshRunner()), _fake_keypair(Path(d)), "probe")
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0].detail.startswith("ERROR:"))  # create failure recorded
        self.assertEqual(runs[0].results, ())  # no probes ran
        self.assertEqual(len(prov.destroyed), 0)  # host never created → no teardown

    def test_parallel_abort_persists_already_completed_host(self) -> None:
        # F4: in the parallel path, a host (fedora) finishes cleanly but its
        # as_completed turn hasn't arrived when another host (debian) raises
        # KeyboardInterrupt. The abort-time drain must STILL fire on_host_complete
        # for the already-done fedora (so its result persists like the serial path)
        # before the KeyboardInterrupt propagates.
        #
        # Determinism — the construction pins the completion order to A, debian,
        # fedora and parks the main consumer until ALL three are done:
        #   * ubuntu (A) completes first; its on_host_complete PARKS the consumer
        #     until ``fedora_done`` so the loop accumulates the other two while held.
        #   * debian raises KeyboardInterrupt and signals ``debian_done`` — it
        #     COMPLETES (and is queued by as_completed) BEFORE fedora.
        #   * fedora waits for ``debian_done`` then completes — so it is done-but-
        #     not-yet-yielded when, on unpark, the loop reaches debian's turn and
        #     the KeyboardInterrupt unwinds. Only the drain can persist fedora.
        prov = FakeProvider()
        ubuntu_done = threading.Event()
        debian_done = threading.Event()
        fedora_done = threading.Event()
        seen: list[str] = []
        seen_lock = threading.Lock()

        class _OrchestratedAbort:
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                if "debian" in host.name:
                    ubuntu_done.wait(timeout=5.0)  # raise strictly after ubuntu is done
                    debian_done.set()
                    raise KeyboardInterrupt("operator hit Ctrl-C")
                if "fedora" in host.name:
                    debian_done.wait(timeout=5.0)  # complete strictly after debian
                    fedora_done.set()
                    return make_probe_result(probe.id, probe.tag, 0, "ok")
                # ubuntu: completes first and fastest (no waits), parking the sink.
                return make_probe_result(probe.id, probe.tag, 0, "ok")

            def upload(self, host: Host, local: Path, remote: str) -> None:
                return None

            def wait_until_ready(self, host: Host) -> None:
                return None

            def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
                return 0

            def upload_dir(self, host: Host, local: Path, remote: str) -> None:
                return None

        def sink(hr: model.HostRun) -> None:
            # ubuntu is the first future done, so it is yielded first. Mark it done
            # (releasing debian's raise) and PARK the consumer until BOTH debian and
            # fedora are done — so debian's raise and fedora's clean completion both
            # land while the consumer is held, making fedora the done-but-unyielded
            # host that ONLY the abort-time drain can persist.
            if hr.host_spec.distro_key == "ubuntu":
                ubuntu_done.set()
                fedora_done.wait(timeout=5.0)
            with seen_lock:
                seen.append(hr.host_spec.distro_key)

        m = runner.Matrix(_demo_workload(), ("ubuntu", "debian", "fedora"), "cpx22", "run-abort")
        with tempfile.TemporaryDirectory() as d, self.assertRaises(KeyboardInterrupt):
            runner.execute(
                m, prov, lambda _o, _k: _OrchestratedAbort(), _fake_keypair(Path(d)), "probe",
                max_parallel=3, on_host_complete=sink,
            )
        # Exactly ubuntu (normal loop, after unpark) then fedora (abort-time drain);
        # debian raised, so it is NOT persisted. Without the drain `seen` would be just
        # ["ubuntu"] — so this equality distinguishes drain-path persistence, not merely
        # that fedora appears somehow.
        self.assertEqual(seen, ["ubuntu", "fedora"])

    def test_on_host_complete_called_per_host_in_completion_order(self) -> None:
        # the incremental sink fires once per host as it completes (serial path)
        prov = FakeProvider()
        seen: list[str] = []
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(
                self._matrix(("ubuntu", "debian")), prov, self._factory(FakeSshRunner()),
                _fake_keypair(Path(d)), "probe",
                on_host_complete=lambda hr: seen.append(hr.host_spec.distro_key),
            )
        self.assertEqual(seen, ["ubuntu", "debian"])  # one call per host
        self.assertEqual([r.host_spec.distro_key for r in runs], ["ubuntu", "debian"])

    def test_parallel_on_host_complete_called_once_per_host_main_thread(self) -> None:
        # parallel path: sink fires once per host from the MAIN thread; aggregate
        # stays matrix-ordered regardless of completion order.
        prov = FakeProvider()
        seen: list[str] = []
        main_thread = threading.current_thread()
        threads_seen: list[bool] = []

        def sink(hr: model.HostRun) -> None:
            threads_seen.append(threading.current_thread() is main_thread)
            seen.append(hr.host_spec.distro_key)

        m = runner.Matrix(_demo_workload(), ("ubuntu", "debian", "fedora"), "cpx22", "run-sink")
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(
                m, prov, lambda _o, _k: FakeSshRunner(), _fake_keypair(Path(d)), "probe",
                max_parallel=3, on_host_complete=sink,
            )
        self.assertEqual(sorted(seen), ["debian", "fedora", "ubuntu"])  # each host once
        self.assertTrue(all(threads_seen))  # all sink calls on the main thread
        self.assertEqual([r.host_spec.distro_key for r in runs], ["ubuntu", "debian", "fedora"])

    def test_parallel_one_failure_does_not_discard_others(self) -> None:
        prov = FakeProvider()

        class _SelectiveSsh:
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                if "debian" in host.name:
                    raise ssh.SshError("debian unreachable")
                return make_probe_result(probe.id, probe.tag, 0, "ok")

            def upload(self, host: Host, local: Path, remote: str) -> None:
                return None

            def wait_until_ready(self, host: Host) -> None:
                return None

            def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
                return 0

            def upload_dir(self, host: Host, local: Path, remote: str) -> None:
                return None

        m = runner.Matrix(_demo_workload(), ("ubuntu", "debian"), "cpx22", "run-par2")
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(m, prov, lambda _o, _k: _SelectiveSsh(), _fake_keypair(Path(d)), "probe", max_parallel=2)
        self.assertEqual(len(runs), 2)
        self.assertEqual(len(prov.destroyed), 2)
        by_distro = {r.host_spec.distro_key: r for r in runs}
        self.assertTrue(by_distro["ubuntu"].results)  # preserved
        self.assertTrue(by_distro["debian"].detail.startswith("ERROR:"))


# --------------------------------------------------------------------------- #
# workload seam — the injected Workload Protocol (D9.1)
# --------------------------------------------------------------------------- #
class _RecordingWorkload:
    """A Workload that records each host it runs on — for seam/ordering tests."""

    plan_summary = "recording"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, spec: HostSpec, host: Host, ssh: ssh.SshRunner, /) -> model.HostRun:
        self.calls.append(host.name)
        return model.HostRun(host_spec=spec, detail="recorded", results=())


class TestWorkloadSeam(unittest.TestCase):
    def test_probe_workload_satisfies_protocol(self) -> None:
        self.assertIsInstance(_demo_workload(), workload.Workload)

    def test_a_non_probe_workload_satisfies_protocol(self) -> None:
        # the seam admits other workloads — a fake conforms structurally
        self.assertIsInstance(_RecordingWorkload(), workload.Workload)

    def test_injected_workload_runs_once_per_host_in_matrix_order(self) -> None:
        prov = FakeProvider()
        rec = _RecordingWorkload()
        m = runner.Matrix(rec, ("ubuntu", "debian"), "cpx22", "run-inj")
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(m, prov, lambda _o, _k: FakeSshRunner(), _fake_keypair(Path(d)), "probe")
        self.assertEqual([r.host_spec.distro_key for r in runs], ["ubuntu", "debian"])  # matrix order
        self.assertEqual(len(rec.calls), 2)  # exactly one run() per host
        self.assertEqual([n.rsplit("-", 1)[-1] for n in rec.calls], ["ubuntu", "debian"])

    def test_runner_gates_readiness_before_invoking_workload(self) -> None:
        # the runner owns readiness: wait_until_ready fires BEFORE workload.run
        prov = FakeProvider()
        order: list[str] = []

        class _ReadyRecordingSsh:
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                return make_probe_result(probe.id, probe.tag, 0, "")

            def upload(self, host: Host, local: Path, remote: str) -> None:
                return None

            def wait_until_ready(self, host: Host) -> None:
                order.append("ready")

            def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
                return 0

            def upload_dir(self, host: Host, local: Path, remote: str) -> None:
                return None

        class _OrderWorkload:
            plan_summary = "order"

            def run(self, spec: HostSpec, host: Host, ssh: ssh.SshRunner, /) -> model.HostRun:
                order.append("run")
                return model.HostRun(host_spec=spec, detail="", results=())

        m = runner.Matrix(_OrderWorkload(), ("ubuntu",), "cpx22", "run-ready")
        with tempfile.TemporaryDirectory() as d:
            runner.execute(m, prov, lambda _o, _k: _ReadyRecordingSsh(), _fake_keypair(Path(d)), "probe")
        self.assertEqual(order, ["ready", "run"])

    def test_plan_renders_the_workload_summary(self) -> None:
        wl = _demo_workload()
        items = runner.plan(runner.Matrix(wl, ("ubuntu",), "cpx22", "run-ps"))
        self.assertEqual(items[0].workload_summary, "probes=3")
        self.assertEqual(items[0].workload_summary, wl.plan_summary)


class _RaisingWorkload:
    """A workload whose ``run`` raises (a transport-style host failure)."""

    plan_summary = "raise"

    def run(self, spec: HostSpec, host: Host, runner_ssh: ssh.SshRunner, /) -> model.HostRun:
        raise ssh.SshError("workload boom")


class _ZeroProbeWorkload:
    """A workload that returns normally with a zero-probe HostRun (prep hard-abort
    shape: a non-raising return carrying no probe results)."""

    plan_summary = "prep-abort"

    def run(self, spec: HostSpec, host: Host, runner_ssh: ssh.SshRunner, /) -> model.HostRun:
        return model.HostRun(
            host_spec=spec, detail="prep packages install failed (hard abort)", results=()
        )


class _SpyKeypair(keypair.Keypair):
    """A Keypair that records whether ``cleanup`` was called (the survives-under-keep
    guard for the printed ``ssh -i <path>`` pointing at a live file)."""

    cleaned: list[bool]

    @staticmethod
    def make(tmp: Path) -> _SpyKeypair:
        d = tmp / "kp"
        d.mkdir(parents=True, exist_ok=True)
        priv = d / "id_ed25519"
        priv.write_text("PRIV", encoding="utf-8")
        kp = _SpyKeypair(directory=d, private_key_path=priv, public_key="ssh-ed25519 AAAA probe")
        # Keypair is a frozen dataclass; set the spy's `cleaned` field past that guard.
        object.__setattr__(kp, "cleaned", [])
        return kp

    def cleanup(self) -> None:
        self.cleaned.append(True)
        # do NOT actually rmtree — tests read the dir afterwards.


class TestKeepFlag(unittest.TestCase):
    """The ``--keep`` design: kept hosts are NOT destroyed, leave a KEPT note, and
    the keypair survives so the printed ssh path is real."""

    def _matrix(
        self, distros: tuple[str, ...] = ("ubuntu",), wl: workload.Workload | None = None
    ) -> runner.Matrix:
        return runner.Matrix(wl or _demo_workload(), distros, "cpx22", "run-keep")

    def _factory(self, ssh_runner: ssh.SshRunner) -> Callable[[str, keypair.Keypair], ssh.SshRunner]:
        return lambda _op, _kp: ssh_runner

    def test_kept_host_not_destroyed_and_carries_kept_note(self) -> None:
        # 1: keep=True → provider.destroy NEVER called; detail carries the KEPT
        # marker, ipv4, operator, and the key path.
        prov = FakeProvider()
        fssh = FakeSshRunner()
        with tempfile.TemporaryDirectory() as d:
            kp = _fake_keypair(Path(d))
            runs = runner.execute(
                self._matrix(), prov, self._factory(fssh), kp, "probe", keep=True
            )
        self.assertEqual(len(runs), 1)
        self.assertEqual(prov.destroyed, [])  # NEVER torn down under keep
        detail = runs[0].detail
        self.assertIn(runner.KEPT_HOST_PREFIX, detail)
        self.assertIn("10.0.0.1", detail)  # the host ipv4
        self.assertIn("probe@10.0.0.1", detail)  # operator@ip ssh target
        self.assertIn(str(kp.private_key_path), detail)  # the real key path

    def test_default_run_still_destroys(self) -> None:
        # 2: regression — default (keep absent) STILL tears down.
        prov = FakeProvider()
        fssh = FakeSshRunner()
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(self._matrix(), prov, self._factory(fssh), _fake_keypair(Path(d)), "probe")
        self.assertEqual(len(prov.destroyed), 1)
        self.assertNotIn(runner.KEPT_HOST_PREFIX, runs[0].detail)

    def test_multi_host_all_kept_even_when_one_workload_raises(self) -> None:
        # 3: two hosts, one workload raises → both kept (destroy never called for
        # either), two HostRuns in matrix order, the failed one carries its error
        # AND the KEPT note.
        prov = FakeProvider()
        fssh = FakeSshRunner()
        m = self._matrix(("ubuntu", "debian"), wl=_RaisingWorkload())
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(m, prov, self._factory(fssh), _fake_keypair(Path(d)), "probe", keep=True)
        self.assertEqual(prov.destroyed, [])  # neither host torn down
        self.assertEqual(len(runs), 2)
        self.assertEqual([r.host_spec.distro_key for r in runs], ["ubuntu", "debian"])  # matrix order
        for r in runs:
            self.assertIn("ERROR:", r.detail)  # the workload raise mapped to error
            self.assertIn(runner.KEPT_HOST_PREFIX, r.detail)  # AND kept

    def test_keypair_survives_under_keep_but_not_otherwise(self) -> None:
        # 4: cleanup NOT called when keep=True; IS called when keep=False.
        prov = FakeProvider()
        with tempfile.TemporaryDirectory() as d:
            kept = _SpyKeypair.make(Path(d) / "a")
            runner.execute(self._matrix(), prov, self._factory(FakeSshRunner()), kept, "probe", keep=True)
            self.assertEqual(kept.cleaned, [])  # survives → ssh -i <path> is real

            torn = _SpyKeypair.make(Path(d) / "b")
            runner.execute(self._matrix(), FakeProvider(), self._factory(FakeSshRunner()), torn, "probe")
            self.assertEqual(torn.cleaned, [True])  # default → reaped

    def test_prep_hard_abort_host_is_kept(self) -> None:
        # 5: a zero-probe HostRun (prep abort returns normally) under keep → host
        # NOT destroyed, KEPT note present.
        prov = FakeProvider()
        m = self._matrix(wl=_ZeroProbeWorkload())
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(m, prov, self._factory(FakeSshRunner()), _fake_keypair(Path(d)), "probe", keep=True)
        self.assertEqual(prov.destroyed, [])  # kept despite prep abort
        self.assertEqual(runs[0].results, ())  # zero probes
        self.assertIn(runner.KEPT_HOST_PREFIX, runs[0].detail)

    def test_build_image_still_destroys_its_builder(self) -> None:
        # 6: blast-radius guard — build_one_image has no keep; its builder is
        # ALWAYS torn down.
        prov = FakeProvider()
        fssh = FakeSshRunner()
        note_sink: list[str] = []
        on_ready = runner.make_snapshot_on_ready(
            "v1-ubuntu-key", {"vmlease-cache-key": "v1-ubuntu-key"}, sleep=lambda _s: None, poweroff_attempts=5
        )
        spec = HostSpec(
            name="vmlease-build-ubuntu",
            image=distro.get_profile("ubuntu").default_image,
            server_type="cpx22", distro_key="ubuntu", labels={"vmlease": "build-run"},
        )
        with tempfile.TemporaryDirectory() as d:
            runner.build_one_image(
                spec, distro.get_profile("ubuntu"), prov, lambda _o, _k: fssh,
                _fake_keypair(Path(d)), "probe", None, on_ready=on_ready, note_sink=note_sink,
            )
        self.assertEqual([h.name for h in prov.destroyed], ["vmlease-build-ubuntu"])  # ALWAYS reaped
        self.assertEqual(note_sink, [])

    def test_cli_keep_gate_aborts_before_provisioning(self) -> None:
        # 7: --keep without --yes, reader "n" → aborts BEFORE keypair/provision.
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            battery = _write_battery_bundle(d, _DEMO_BATTERY)
            ns = cli.build_parser().parse_args([
                "run", "--battery", battery, "--distros", "ubuntu", "--keep",
                "--results-dir", str(Path(d) / "r"), "--timestamp", "T", "--run-token", "cli-keep",
            ])
            generated: list[str] = []

            def _spy_keygen(rid: str) -> keypair.Keypair:
                generated.append(rid)
                return _fake_keypair(Path(d))

            buf = io.StringIO()
            # reader says "y" to the FIRST (provisioning) confirm, then "n" to the
            # --keep confirm → must abort before any keypair is generated.
            answers = iter(["y", "n"])
            with mock.patch.object(cli, "generate_keypair", _spy_keygen), redirect_stdout(buf):
                rc = cli._cmd_run(ns, reader=lambda _p: next(answers))
            self.assertEqual(rc, 0)
            self.assertIn("aborted", buf.getvalue())
            self.assertEqual(generated, [])  # no keypair → no provisioning
            self.assertFalse((Path(d) / "r").exists())

    def test_cli_keep_proceeds_with_yes(self) -> None:
        # 7 (proceed leg): --keep with --yes → both confirms skipped, run proceeds
        # and the keypair IS generated (provisioning attempted).
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            battery = _write_battery_bundle(d, _DEMO_BATTERY)
            ns = cli.build_parser().parse_args([
                "run", "--battery", battery, "--distros", "ubuntu", "--keep", "--yes",
                "--results-dir", str(Path(d) / "r"), "--timestamp", "T", "--run-token", "cli-keep",
            ])
            generated: list[str] = []

            def _spy_keygen(rid: str) -> keypair.Keypair:
                generated.append(rid)
                return _fake_keypair(Path(d))

            fssh = FakeSshRunner()
            buf = io.StringIO()
            with mock.patch.object(cli, "generate_keypair", _spy_keygen), \
                    mock.patch.object(cli, "HetznerProvider", FakeProvider), \
                    mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: fssh), \
                    redirect_stdout(buf), redirect_stderr(io.StringIO()):
                rc = cli._cmd_run(ns, reader=lambda _p: "n")  # reader ignored under --yes
            self.assertEqual(rc, 0)
            self.assertEqual(len(generated), 1)  # proceeded → keypair generated

    def test_cli_keep_renders_consolidated_live_host_block_on_stderr(self) -> None:
        # 8: --keep --yes drives _cmd_run end-to-end → the consolidated live-host
        # block is rendered to STDERR (header + count, an `ssh -i` KEPT note, the
        # backticked reap hint, and the surviving key-dir note). Exit 0; the kept
        # host is NEVER destroyed.
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            battery = _write_battery_bundle(d, _DEMO_BATTERY)
            ns = cli.build_parser().parse_args([
                "run", "--battery", battery, "--distros", "ubuntu", "--keep", "--yes",
                "--results-dir", str(Path(d) / "r"), "--timestamp", "T", "--run-token", "cli-keep",
            ])
            kept_kp = _fake_keypair(Path(d))
            prov = FakeProvider()
            fssh = FakeSshRunner()
            err = io.StringIO()
            with mock.patch.object(cli, "generate_keypair", lambda _rid: kept_kp), \
                    mock.patch.object(cli, "HetznerProvider", lambda: prov), \
                    mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: fssh), \
                    redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = cli._cmd_run(ns, reader=lambda _p: "n")  # reader ignored under --yes
            self.assertEqual(rc, 0)
            self.assertEqual(prov.destroyed, [])  # kept → never torn down
            block = err.getvalue()
            self.assertIn("WARNING: --keep left 1 billable host(s) LIVE", block)
            self.assertIn("ssh -i ", block)  # a KEPT note's ssh line
            self.assertIn("`vmlease reap --run-token cli-keep`", block)  # backticked reap hint
            self.assertIn(str(kept_kp.directory), block)  # surviving key-dir note


class _ScriptedTimeoutSsh:
    """An SshRunner whose ``run_probe`` returns timed-out results for named probes.

    ``timeout_ids`` is the set of probe ids that come back ``timed_out`` (exit 124);
    everything else (including the ``_detail`` snapshot) returns a normal exit-0
    result. For exercising the consecutive-timeout breaker without a real socket.
    """

    def __init__(self, timeout_ids: set[str]) -> None:
        self._timeout_ids = timeout_ids
        self.ran: list[str] = []

    def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
        self.ran.append(probe.id)
        if probe.id in self._timeout_ids:
            return make_probe_result(probe.id, probe.tag, 124, "", "timed out after 1.0s", timed_out=True)
        return make_probe_result(probe.id, probe.tag, 0, f"out-{probe.id}")

    def upload(self, host: Host, local: Path, remote: str) -> None:
        return None

    def wait_until_ready(self, host: Host) -> None:
        return None

    def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
        return 0

    def upload_dir(self, host: Host, local: Path, remote: str) -> None:
        return None


class TestProbeWorkloadBreaker(unittest.TestCase):
    """T1b: timeout does not abort the battery; K consecutive timeouts stop it."""

    def _battery(self) -> model.Battery:
        # four probes; they run in authoring order Q1..Q4.
        manifest = battery_toml("b", (
            {"id": "Q1", "title": "t", "run": "c", "tag": "read-only"},
            {"id": "Q2", "title": "t", "run": "c", "tag": "read-only"},
            {"id": "Q3", "title": "t", "run": "c", "tag": "read-only"},
            {"id": "Q4", "title": "t", "run": "c", "tag": "read-only"},
        ))
        return _resolve_toml(manifest)

    def _spec(self) -> model.HostSpec:
        return model.HostSpec(name="n", image="i", server_type="cpx22", distro_key="ubuntu")

    def _host(self) -> Host:
        return Host(id="1", name="n", ipv4="9.9.9.9")

    def test_isolated_timeout_continues_battery(self) -> None:
        # one timeout in the middle, the next probe NOT a timeout → battery runs on,
        # every later probe's result is present.
        wl = workload.ProbeWorkload(self._battery())
        ssh_fake = _ScriptedTimeoutSsh({"Q2"})
        run = wl.run(self._spec(), self._host(), ssh_fake)
        self.assertEqual([r.probe_id for r in run.results], ["Q1", "Q2", "Q3", "Q4"])
        self.assertTrue(run.results[1].timed_out)  # the isolated timeout is recorded
        self.assertFalse(run.results[3].timed_out)  # later probes still ran
        self.assertNotIn("battery stopped", run.detail)

    def test_k_consecutive_timeouts_stop_battery(self) -> None:
        # Q2 and Q3 both time out (K=2) → battery stops; Q4 is NOT run; prior
        # results (Q1, Q2, Q3) are preserved and the detail carries the note.
        wl = workload.ProbeWorkload(self._battery())
        ssh_fake = _ScriptedTimeoutSsh({"Q2", "Q3"})
        run = wl.run(self._spec(), self._host(), ssh_fake)
        self.assertEqual([r.probe_id for r in run.results], ["Q1", "Q2", "Q3"])  # Q4 not run
        self.assertNotIn("Q4", ssh_fake.ran)  # the breaker really stopped the loop
        self.assertIn("battery stopped", run.detail)
        self.assertIn("2 consecutive probe timeouts", run.detail)
        self.assertIn("'Q4'", run.detail)  # names the probe(s) not run

    def test_non_consecutive_timeouts_do_not_trip(self) -> None:
        # Q1 and Q3 time out but Q2 resets the counter → never K-in-a-row, full run.
        wl = workload.ProbeWorkload(self._battery())
        ssh_fake = _ScriptedTimeoutSsh({"Q1", "Q3"})
        run = wl.run(self._spec(), self._host(), ssh_fake)
        self.assertEqual(len(run.results), 4)  # all four ran
        self.assertNotIn("battery stopped", run.detail)

    def test_breaker_constant_default_is_two(self) -> None:
        self.assertEqual(workload.MAX_CONSECUTIVE_TIMEOUTS, 2)

    def test_mixed_tag_battery_runs_in_authoring_order(self) -> None:
        # a host-root probe authored before a read-only one executes (and records)
        # first — authoring order, not tag-rank; the read-only verifier runs after.
        manifest = battery_toml("b", (
            {"id": "SETUP", "title": "t", "run": "c", "tag": "mutating:host-root"},
            {"id": "VERIFY", "title": "t", "run": "c", "tag": "read-only"},
        ))
        wl = workload.ProbeWorkload(_resolve_toml(manifest))
        ssh_fake = _ScriptedTimeoutSsh(set())
        run = wl.run(self._spec(), self._host(), ssh_fake)
        self.assertEqual(ssh_fake.ran, ["_detail", "SETUP", "VERIFY"])
        self.assertEqual([r.probe_id for r in run.results], ["SETUP", "VERIFY"])

    def test_script_probe_resolves_and_executes_via_workload(self) -> None:
        # a `script` probe's resolved command (the file's contents) reaches the
        # transport unchanged — exercising the script-ref path end-to-end.
        manifest = battery_toml("b", (
            {"id": "SCRIPTED", "title": "t", "script": "prep.sh", "tag": "read-only"},
        ))
        script_body = "set -euo pipefail\nuname -r\nexit 0\n"
        battery = _resolve_toml(manifest, {"prep.sh": script_body})

        seen: list[str] = []

        class _CommandRecordingSsh:
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                seen.append(probe.command)
                return make_probe_result(probe.id, probe.tag, 0, "")

            def upload(self, host: Host, local: Path, remote: str) -> None:
                return None

            def wait_until_ready(self, host: Host) -> None:
                return None

            def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
                return 0

            def upload_dir(self, host: Host, local: Path, remote: str) -> None:
                return None

        wl = workload.ProbeWorkload(battery)
        wl.run(self._spec(), self._host(), _CommandRecordingSsh())
        self.assertIn(script_body, seen)  # the file's contents reached the transport


# --------------------------------------------------------------------------- #
# ProbeWorkload — the [prep] phase (packages + setup) before the probe loop (E-014)
# --------------------------------------------------------------------------- #
class _ScriptedPrepSsh:
    """An SshRunner recording every command, with per-probe-id scripted exits.

    ``exits`` maps a probe id to the exit code its ``run_probe`` returns
    (anything absent returns 0). ``ran`` records the ordered (id) sequence and
    ``commands`` the ordered command text — so a test can assert prep ran before
    probes, packages before setup, and which steps were skipped.
    """

    def __init__(self, exits: dict[str, int] | None = None) -> None:
        self._exits = exits or {}
        self.ran: list[str] = []
        self.commands: list[str] = []

    def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
        self.ran.append(probe.id)
        self.commands.append(probe.command)
        code = self._exits.get(probe.id, 0)
        return make_probe_result(probe.id, probe.tag, code, "", f"err-{probe.id}" if code else "")

    def upload(self, host: Host, local: Path, remote: str) -> None:
        return None

    def wait_until_ready(self, host: Host) -> None:
        return None

    def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
        return 0

    def upload_dir(self, host: Host, local: Path, remote: str) -> None:
        return None


class TestProbeWorkloadPrepPhase(unittest.TestCase):
    """Prep runs after readiness, before probes; hard fail returns (not raises)."""

    _PROBE = "\n[[probe]]\nid = '''P1'''\ntitle = '''t'''\ntag = '''read-only'''\nrun = '''c'''\n"

    def _battery(self, prep_body: str) -> model.Battery:
        return _resolve_toml(f"name = '''b'''\n{prep_body}{self._PROBE}")

    def _spec(self, distro_key: str = "ubuntu") -> model.HostSpec:
        return model.HostSpec(name="n", image="i", server_type="cpx22", distro_key=distro_key)

    def _host(self) -> Host:
        return Host(id="1", name="n", ipv4="9.9.9.9")

    def test_no_prep_host_runs_probes_with_empty_prep_phase(self) -> None:
        # a battery with no [prep] runs the probe loop unchanged; prep_phase is ().
        wl = workload.ProbeWorkload(self._battery(""))
        ssh_fake = _ScriptedPrepSsh()
        run = wl.run(self._spec(), self._host(), ssh_fake)
        self.assertEqual(run.prep_phase, ())
        self.assertEqual([r.probe_id for r in run.results], ["P1"])

    def test_prep_runs_before_probes_packages_before_setup(self) -> None:
        # packages pass -> setup step -> probe loop, in that order (5.5a).
        prep = (
            "[prep.packages]\napt = ['''pkg-a''']\n"
            "[[prep.setup]]\nid = '''s1'''\nrun = '''echo s1'''\n"
        )
        wl = workload.ProbeWorkload(self._battery(prep))
        ssh_fake = _ScriptedPrepSsh()
        run = wl.run(self._spec(), self._host(), ssh_fake)
        # _detail first, then the package pass, then the setup step, then the probe
        self.assertEqual(ssh_fake.ran, ["_detail", "_packages", "s1", "P1"])
        # apt-get update runs FIRST inside the package command (D13.2)
        pkg_cmd = ssh_fake.commands[ssh_fake.ran.index("_packages")]
        self.assertTrue(pkg_cmd.startswith("sudo apt-get update && "))
        self.assertIn("apt-get install -y pkg-a", pkg_cmd)
        # prep_phase carries both prep outcomes in order; probe captured separately
        self.assertEqual([p.id for p in run.prep_phase], ["_packages", "s1"])
        self.assertEqual([r.probe_id for r in run.results], ["P1"])

    def test_package_pass_union_manager_and_distro_selectors(self) -> None:
        # union(manager-list, distro-list), deduped manager-first, one install pass.
        prep = "[prep.packages]\napt = ['''a''', '''shared''']\nubuntu = ['''shared''', '''u''']\n"
        wl = workload.ProbeWorkload(self._battery(prep))
        ssh_fake = _ScriptedPrepSsh()
        wl.run(self._spec("ubuntu"), self._host(), ssh_fake)
        pkg_cmd = ssh_fake.commands[ssh_fake.ran.index("_packages")]
        self.assertIn("apt-get install -y a shared u", pkg_cmd)

    def test_distros_excluded_step_absent_from_prep_phase(self) -> None:
        # a step whose distros allowlist excludes this host is skipped entirely.
        prep = (
            "[[prep.setup]]\nid = '''arch-only'''\nrun = '''c'''\ndistros = ['''arch''']\n"
            "[[prep.setup]]\nid = '''everywhere'''\nrun = '''c'''\n"
        )
        wl = workload.ProbeWorkload(self._battery(prep))
        ssh_fake = _ScriptedPrepSsh()
        run = wl.run(self._spec("ubuntu"), self._host(), ssh_fake)
        self.assertNotIn("arch-only", ssh_fake.ran)
        self.assertEqual([p.id for p in run.prep_phase], ["everywhere"])

    def test_hard_package_fail_returns_zero_probe_hostrun_with_prep_phase(self) -> None:
        # the package pass is always hard: a non-zero exit aborts before any setup
        # or probe, returning (NOT raising) a HostRun carrying prep_phase + 0 probes.
        prep = (
            "[prep.packages]\napt = ['''bad''']\n"
            "[[prep.setup]]\nid = '''s1'''\nrun = '''c'''\n"
        )
        wl = workload.ProbeWorkload(self._battery(prep))
        ssh_fake = _ScriptedPrepSsh({"_packages": 100})
        run = wl.run(self._spec(), self._host(), ssh_fake)
        self.assertEqual(run.results, ())          # zero probes
        self.assertNotIn("s1", ssh_fake.ran)       # setup never reached
        self.assertNotIn("P1", ssh_fake.ran)       # probe never reached
        self.assertEqual([p.id for p in run.prep_phase], ["_packages"])
        self.assertEqual(run.prep_phase[0].exit, 100)
        self.assertTrue(run.prep_phase[0].required)

    def test_hard_required_setup_fail_returns_zero_probe_hostrun(self) -> None:
        # a required setup step's non-zero exit is a hard abort: prep_phase carries
        # the failing step, probes are skipped, the HostRun is RETURNED not raised.
        prep = (
            "[[prep.setup]]\nid = '''s1'''\nrun = '''c'''\n"
            "[[prep.setup]]\nid = '''s2'''\nrun = '''c'''\n"
        )
        wl = workload.ProbeWorkload(self._battery(prep))
        ssh_fake = _ScriptedPrepSsh({"s1": 7})
        run = wl.run(self._spec(), self._host(), ssh_fake)
        self.assertEqual(run.results, ())
        self.assertNotIn("s2", ssh_fake.ran)   # the loop stopped at the hard fail
        self.assertNotIn("P1", ssh_fake.ran)
        self.assertEqual([p.id for p in run.prep_phase], ["s1"])
        self.assertEqual(run.prep_phase[0].exit, 7)

    def test_soft_setup_fail_recorded_and_probes_continue(self) -> None:
        # a required=false step that fails is recorded but the phase + probes go on.
        prep = (
            "[[prep.setup]]\nid = '''soft'''\nrun = '''c'''\nrequired = false\n"
            "[[prep.setup]]\nid = '''after'''\nrun = '''c'''\n"
        )
        wl = workload.ProbeWorkload(self._battery(prep))
        ssh_fake = _ScriptedPrepSsh({"soft": 3})
        run = wl.run(self._spec(), self._host(), ssh_fake)
        self.assertEqual([p.id for p in run.prep_phase], ["soft", "after"])
        self.assertEqual(run.prep_phase[0].exit, 3)
        self.assertFalse(run.prep_phase[0].required)
        self.assertEqual([r.probe_id for r in run.results], ["P1"])  # probes ran

    def test_setup_step_timeout_default_is_1800(self) -> None:
        # an explicit step timeout overrides; an absent one takes the loader-resolved
        # default (battery.PREP_STEP_DEFAULT_TIMEOUT, 1800s) — verified end-to-end at
        # the probe the workload submits.
        prep = (
            "[[prep.setup]]\nid = '''d'''\nrun = '''c'''\n"
            "[[prep.setup]]\nid = '''o'''\nrun = '''c'''\ntimeout = 42\n"
        )
        seen: dict[str, float | None] = {}

        class _TimeoutRecordingSsh(_ScriptedPrepSsh):
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                seen[probe.id] = probe.timeout
                return super().run_probe(host, probe)

        wl = workload.ProbeWorkload(self._battery(prep))
        wl.run(self._spec(), self._host(), _TimeoutRecordingSsh())
        self.assertEqual(seen["d"], 1800.0)
        self.assertEqual(seen["o"], 42.0)

    def test_package_pass_uses_the_prep_timeout_default(self) -> None:
        # the [prep.packages] install is prep work, so its synthetic _packages probe
        # shares the prep-phase bound (1800s), not the shorter probe default.
        prep = "[prep.packages]\napt = ['''jq''']\n"
        seen: dict[str, float | None] = {}

        class _TimeoutRecordingSsh(_ScriptedPrepSsh):
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                seen[probe.id] = probe.timeout
                return super().run_probe(host, probe)

        wl = workload.ProbeWorkload(self._battery(prep))
        wl.run(self._spec(), self._host(), _TimeoutRecordingSsh())
        self.assertEqual(seen["_packages"], battery_mod.PREP_STEP_DEFAULT_TIMEOUT)
        self.assertEqual(seen["_packages"], 1800.0)


# --------------------------------------------------------------------------- #
# results.serialize_run — prep_phase is always present (D13.5 / D-I.2)
# --------------------------------------------------------------------------- #
class TestSerializeRunPrepPhase(unittest.TestCase):
    def _spec(self) -> model.HostSpec:
        return model.HostSpec(name="n", image="ubuntu-24.04", server_type="cpx22", distro_key="ubuntu")

    def test_prep_phase_emitted_as_list_of_dicts(self) -> None:
        hr = model.HostRun(
            host_spec=self._spec(),
            detail="d",
            results=(),
            prep_phase=(
                model.PrepStepResult(id="uv", exit=0, required=True, stderr=""),
                model.PrepStepResult(id="tlog", exit=5, required=False, stderr="boom"),
            ),
        )
        doc = json.loads(results.serialize_run("r1", "ts", [hr]))
        prep = doc["hosts"][0]["prep_phase"]
        self.assertEqual(prep, [
            {"id": "uv", "exit": 0, "required": True, "stderr": ""},
            {"id": "tlog", "exit": 5, "required": False, "stderr": "boom"},
        ])

    def test_prep_phase_always_present_empty_when_no_prep(self) -> None:
        hr = model.HostRun(host_spec=self._spec(), detail="d", results=())
        doc = json.loads(results.serialize_run("r1", "ts", [hr]))
        self.assertEqual(doc["hosts"][0]["prep_phase"], [])


# --------------------------------------------------------------------------- #
# shellcheck — the lint driver also iterates [[prep.setup]] steps (5.4)
# --------------------------------------------------------------------------- #
class TestShellcheckPrepSteps(unittest.TestCase):
    def _runner(
        self, stdout: str
    ) -> Callable[[list[str], str | None], subprocess.CompletedProcess[str]]:
        calls: list[tuple[list[str], str | None]] = []

        def _run(argv: list[str], stdin_text: str | None) -> subprocess.CompletedProcess[str]:
            calls.append((argv, stdin_text))
            return subprocess.CompletedProcess(argv, 1, stdout, "")

        self.calls = calls
        return _run

    def test_lint_flags_a_prep_setup_finding(self) -> None:
        # a [[prep.setup]] step is fed to shellcheck and a finding is located to it.
        battery = model.Battery(
            name="t",
            probes=(Probe(id="P", title="p", command="echo ok", tag=ProbeTag.READ_ONLY, source="<inline>"),),
            prep=model.Prep(
                setup=(
                    model.PrepStep(id="prep-s", command="x=$(date)", source="prep.sh"),
                ),
            ),
        )
        findings = shellcheck_mod.shellcheck_battery(
            battery, runner=self._runner("-:1:7: warning: masked [SC2155]\n")
        )
        assert isinstance(findings, tuple)
        # both the probe AND the prep step were fed to shellcheck (two calls)
        self.assertEqual([stdin for _, stdin in self.calls], ["echo ok", "x=$(date)"])
        # the prep-step finding is located back to the step (id + source)
        prep_findings = [f for f in findings if f.probe_id == "prep-s"]
        self.assertEqual(len(prep_findings), 1)
        self.assertEqual(prep_findings[0].location, "prep.sh")
        self.assertEqual(prep_findings[0].code, "SC2155")


# --------------------------------------------------------------------------- #
# cli — run (confirm gate) / reap / status, with provider + ssh stubbed
# --------------------------------------------------------------------------- #
class TestCliRun(unittest.TestCase):
    def _write_battery(self, d: str) -> str:
        return _write_battery_bundle(d, _DEMO_BATTERY)

    def test_run_aborts_without_confirm(self) -> None:
        # _cmd_run reads via injected reader; "n" aborts before provisioning
        with tempfile.TemporaryDirectory() as d:
            ns = cli.build_parser().parse_args([
                "run", "--battery", self._write_battery(d), "--distros", "ubuntu",
                "--results-dir", str(Path(d) / "r"), "--timestamp", "T", "--run-token", "cli-run",
            ])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli._cmd_run(ns, reader=lambda _p: "n")
            self.assertEqual(rc, 0)
            self.assertIn("aborted", buf.getvalue())
            self.assertFalse((Path(d) / "r").exists())  # no results written

    def test_run_cost_guard_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rc = cli.main([
                "run", "--battery", self._write_battery(d), "--distros", "ubuntu,debian,fedora,arch",
                "--max-hosts", "2", "--results-dir", str(Path(d) / "r"), "--timestamp", "T", "--run-token", "cli-run",
            ])
            self.assertEqual(rc, 2)

    def test_run_arch_without_ssh_key_path_refused(self) -> None:
        # a rescue-write distro needs BOTH --ssh-key and --ssh-key-path; omitting
        # the path is refused AFTER the confirm (so no host is provisioned).
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cli.main([
                        "run", "--battery", self._write_battery(d), "--distros", "arch",
                        "--ssh-key", "mykey",  # name given, but no --ssh-key-path
                        "--results-dir", str(Path(d) / "r"), "--timestamp", "T",
                        "--run-token", "cli-run", "--yes",
                    ])
            self.assertEqual(rc, 2)

    def test_run_arch_refusal_message_mentions_cache_miss_rescue_write(self) -> None:
        # D11: the improved message explains the key is required because a cache
        # miss may still rescue-write.
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))):
                buf = io.StringIO()
                with redirect_stderr(buf):
                    rc = cli.main([
                        "run", "--battery", self._write_battery(d), "--distros", "arch",
                        "--results-dir", str(Path(d) / "r"), "--timestamp", "T",
                        "--run-token", "cli-run", "--yes",
                    ])
            self.assertEqual(rc, 2)
            self.assertIn("cache miss may still rescue-write", buf.getvalue())

    def test_run_reap_bad_cache_image_threads_to_execute(self) -> None:
        # --reap-bad-cache-image is parsed and threaded into execute().
        from unittest import mock

        captured: dict[str, object] = {}

        def _capture_execute(*a: object, **k: object) -> list[model.HostRun]:
            captured.update(k)
            return []

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(cli, "HetznerProvider", FakeProvider), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: FakeSshRunner()), \
                 mock.patch.object(cli, "execute", _capture_execute):
                cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu",
                    "--results-dir", str(Path(d) / "r"), "--timestamp", "T",
                    "--run-token", "cli-run", "--yes", "--reap-bad-cache-image",
                ])
            self.assertTrue(captured.get("reap_bad_cache_image"))
            self.assertIn("resolve_deps", captured)

    def test_run_cache_hit_restores_via_cli(self) -> None:
        # end-to-end CLI: a matching cache image → the host is created from the
        # snapshot id (the wiring carries resolve_deps through to a real hit).
        from unittest import mock

        prov = FakeProvider()
        prov.images["img-cache"] = _cached_run_image(
            key=imagecache.content_key(distro.get_profile("ubuntu"), "x86", "probe", (), _null_deps())
        )
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(cli, "HetznerProvider", lambda: prov), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: FakeSshRunner()):
                rc = cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu",
                    "--results-dir", str(Path(d) / "r"), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes",
                ])
            self.assertEqual(rc, 0)
            self.assertEqual(prov.created[0].image, "img-cache")
            self.assertEqual(prov.created_images, [])  # run never builds

    def test_run_docker_battery_hits_docker_cache_image(self) -> None:
        # 4.4b: a `build-image --requires docker` image (docker requires-hash +
        # docker-rendered key) is restored by a `requires=["docker"]` battery run
        # — the docker variant cache-hits for a docker battery.
        from unittest import mock

        prov = FakeProvider()
        docker_key = imagecache.content_key(
            distro.get_profile("ubuntu"), "x86", "probe", ("docker",), _null_deps()
        )
        prov.images["img-docker"] = _cached_run_image(
            key=docker_key, requires=("docker",), img_id="img-docker"
        )
        docker_battery = "requires = ['''docker''']\n" + _DEMO_BATTERY
        with tempfile.TemporaryDirectory() as d:
            bat = _write_battery_bundle(d, docker_battery)
            with mock.patch.object(cli, "HetznerProvider", lambda: prov), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: FakeSshRunner()):
                rc = cli.main([
                    "run", "--battery", bat, "--distros", "ubuntu",
                    "--results-dir", str(Path(d) / "r"), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes",
                ])
            self.assertEqual(rc, 0)
            self.assertEqual(prov.created[0].image, "img-docker")  # docker variant restored
            self.assertEqual(prov.created_images, [])  # run never builds
            # the restore is OBSERVABLE in the results (the cache-HIT oracle)
            doc = json.loads(next((Path(d) / "r").glob("*.json")).read_text())
            self.assertEqual(doc["hosts"][0]["restored_image"], "img-docker")

    def test_run_docker_less_battery_misses_docker_cache_image(self) -> None:
        # 4.4b: the OTHER side of the guard — a docker-less (no `requires`) run
        # does NOT hit a docker cache image (its key is the docker-less render),
        # so it cold-paths instead of wrongly restoring the docker variant.
        from unittest import mock

        prov = FakeProvider()
        docker_key = imagecache.content_key(
            distro.get_profile("ubuntu"), "x86", "probe", ("docker",), _null_deps()
        )
        prov.images["img-docker"] = _cached_run_image(
            key=docker_key, requires=("docker",), img_id="img-docker"
        )
        with tempfile.TemporaryDirectory() as d:
            bat = _write_battery_bundle(d, _DEMO_BATTERY)  # NO requires
            with mock.patch.object(cli, "HetznerProvider", lambda: prov), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: FakeSshRunner()):
                rc = cli.main([
                    "run", "--battery", bat, "--distros", "ubuntu",
                    "--results-dir", str(Path(d) / "r"), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes",
                ])
            self.assertEqual(rc, 0)
            # cold path: a brand-new host was created (NOT the docker snapshot).
            self.assertNotEqual(prov.created[0].image, "img-docker")
            # and the miss is OBSERVABLE: restored_image is null (not a false HIT)
            doc = json.loads(next((Path(d) / "r").glob("*.json")).read_text())
            self.assertIsNone(doc["hosts"][0]["restored_image"])

    def test_run_success_writes_results(self) -> None:
        # stub the provider + keypair + ssh so the run path executes end-to-end
        # with no network. --yes skips the confirm prompt.
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r"
            with mock.patch.object(cli, "HetznerProvider", FakeProvider), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: FakeSshRunner()):
                rc = cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu",
                    "--results-dir", str(rdir), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes",
                ])
            self.assertEqual(rc, 0)
            self.assertTrue((rdir / "vmlease-cli-run-20260601T000000Z.json").exists())

    def test_run_probe_timeout_reaches_ssh_runner(self) -> None:
        # --probe-timeout (run-only) must be threaded into OpenSshRunner via the
        # factory as probe_timeout_default; capture the kwarg the factory passes.
        from unittest import mock

        captured: dict[str, object] = {}

        def _capture(*_a: object, **k: object) -> FakeSshRunner:
            captured.update(k)
            return FakeSshRunner()

        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r"
            with mock.patch.object(cli, "HetznerProvider", FakeProvider), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", _capture):
                rc = cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu",
                    "--results-dir", str(rdir), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes", "--probe-timeout", "42.5",
                ])
            self.assertEqual(rc, 0)
            self.assertEqual(captured.get("probe_timeout_default"), 42.5)

    def test_run_writes_results_incrementally_per_host(self) -> None:
        # the per-host sink rewrites the results file as each host lands; after a
        # clean run the deterministic path holds every host and rc is 0.
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r"
            with mock.patch.object(cli, "HetznerProvider", FakeProvider), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: FakeSshRunner()):
                rc = cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu,debian",
                    "--results-dir", str(rdir), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes",
                ])
            self.assertEqual(rc, 0)
            written = json.loads((rdir / "vmlease-cli-run-20260601T000000Z.json").read_text())
            self.assertEqual(len(written["hosts"]), 2)

    def test_run_end_to_end_smoke_ties_the_lifecycle_together(self) -> None:
        # F5: one realistic, fully-mocked `vmlease run` over TWO distros that ties
        # the whole lifecycle together — the unit tests cover each behavior in
        # isolation; this asserts they compose. The live billable smoke (real
        # machinectl/VM) stays deferred; nothing here opens a socket.
        #
        # Asserts in ONE run: clean rc; results file holds BOTH hosts (incremental
        # persistence end-to-end); a timed-out probe is recorded (exit 124) and did
        # NOT abort the battery (every probe present); destroy fired for BOTH hosts
        # (per-host finally teardown); and --probe-timeout reached OpenSshRunner.
        from unittest import mock

        captured: dict[str, object] = {}

        class _TimeoutOneProbeSsh(FakeSshRunner):
            # normal results for every probe EXCEPT P6, which times out (exit 124,
            # timed_out=True) — a SINGLE timeout, below the K=2 breaker, so the
            # battery runs to completion and the timeout is just recorded.
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                self.ran.append(probe.id)
                if probe.id == "P6":
                    return make_probe_result(probe.id, probe.tag, 124, "", "timed out after 42.5s", timed_out=True)
                return make_probe_result(probe.id, probe.tag, 0, f"out-{probe.id}")

        def _capture(*_a: object, **k: object) -> _TimeoutOneProbeSsh:
            captured.update(k)
            return _TimeoutOneProbeSsh()

        prov = FakeProvider()
        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r"
            with mock.patch.object(cli, "HetznerProvider", lambda: prov), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", _capture):
                rc = cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu,fedora",
                    "--results-dir", str(rdir), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes", "--probe-timeout", "42.5",
                ])
            self.assertEqual(rc, 0)  # clean run
            # --probe-timeout reached the ssh runner factory.
            self.assertEqual(captured.get("probe_timeout_default"), 42.5)
            # destroy fired for BOTH hosts (per-host finally teardown).
            self.assertEqual(len(prov.destroyed), 2)
            # results file exists and holds BOTH hosts (incremental persistence).
            path = rdir / "vmlease-cli-run-20260601T000000Z.json"
            self.assertTrue(path.exists())
            written = json.loads(path.read_text())
            self.assertEqual(len(written["hosts"]), 2)
            self.assertEqual(
                sorted(h["distro"] for h in written["hosts"]), ["fedora", "ubuntu"]
            )
            # the timed-out P6 is recorded (exit 124) and the battery did NOT abort:
            # every battery probe is present on each host.
            for host in written["hosts"]:
                ids = [p["id"] for p in host["probes"]]
                self.assertEqual(ids, ["P1", "P6", "P12"])  # full battery, none dropped
                p6 = next(p for p in host["probes"] if p["id"] == "P6")
                self.assertEqual(p6["exit_code"], 124)  # the timeout is recorded
                self.assertFalse(p6["ok"])

    def test_run_teardown_failure_returns_nonzero_and_reaps(self) -> None:
        # a destroy that raises (host stays live) → HostRun.detail carries the
        # teardown warning → _cmd_run reaps the run label and returns non-zero.
        from unittest import mock

        class _DestroyFailsOnce(FakeProvider):
            def __init__(self) -> None:
                super().__init__()
                self.destroy_calls = 0

            def destroy(self, host: Host) -> None:
                # First teardown (in-run) fails, leaving the host live; the reap
                # backstop's destroy then succeeds (a transient provider blip).
                self.destroy_calls += 1
                if self.destroy_calls == 1:
                    raise providers.ProviderError("request timeout")
                super().destroy(host)

        prov = _DestroyFailsOnce()
        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r"
            buf = io.StringIO()
            with mock.patch.object(cli, "HetznerProvider", lambda: prov), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: FakeSshRunner()), \
                 redirect_stderr(buf):
                rc = cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu",
                    "--results-dir", str(rdir), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes",
                ])
            self.assertEqual(rc, 1)
            self.assertIn("teardown failed", buf.getvalue())
            # the backstop reap names the host(s) it cleaned up (like `vmlease reap`)
            self.assertIn("reaped vmlease-cli-run-ubuntu", buf.getvalue())
            # reap was attempted: list_labeled then destroy on the still-live host.
            self.assertTrue(prov.list_labeled(safety.make_run_id("cli-run")) == [])

    def test_run_abort_reaps_and_reraises(self) -> None:
        # an operator interrupt mid-run: the host that finished is on disk, the
        # run label is reaped, and the KeyboardInterrupt keeps propagating.
        from unittest import mock

        class _Interrupting(FakeSshRunner):
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                raise KeyboardInterrupt("operator hit Ctrl-C")

        prov = FakeProvider()
        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r"
            buf = io.StringIO()
            with mock.patch.object(cli, "HetznerProvider", lambda: prov), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: _Interrupting()), \
                 redirect_stderr(buf), \
                 self.assertRaises(KeyboardInterrupt):
                cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu",
                    "--results-dir", str(rdir), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes",
                ])
            self.assertIn("aborted", buf.getvalue())
            # the run label was reaped even though the interrupt propagated.
            self.assertEqual(prov.list_labeled(safety.make_run_id("cli-run")), [])

    def test_run_teardown_failure_and_backstop_reap_also_fails(self) -> None:
        # teardown fails (host stays live) AND the backstop reap ALSO raises
        # ProviderError: _cmd_run must still return non-zero (no traceback) and
        # print the actionable `vmlease reap --run-token` manual-cleanup hint.
        from unittest import mock

        class _DestroyAlwaysFails(FakeProvider):
            def destroy(self, host: Host) -> None:
                # In-run teardown fails (host stays live), and the backstop
                # reap's destroy fails too — the host can't be reaped.
                raise providers.ProviderError("request timeout")

        prov = _DestroyAlwaysFails()
        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r"
            buf = io.StringIO()
            with mock.patch.object(cli, "HetznerProvider", lambda: prov), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: FakeSshRunner()), \
                 redirect_stderr(buf):
                rc = cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu",
                    "--results-dir", str(rdir), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes",
                ])
            self.assertEqual(rc, 1)  # non-zero, NOT a traceback
            self.assertIn("backstop reap ALSO failed", buf.getvalue())
            self.assertIn("vmlease reap --run-token cli-run", buf.getvalue())

    def test_run_abort_and_backstop_reap_also_fails_reraises_interrupt(self) -> None:
        # an operator interrupt mid-run AND the backstop reap ALSO raises
        # ProviderError: the ORIGINAL KeyboardInterrupt (not ProviderError) must
        # propagate, and the actionable manual-reap hint must be printed.
        from unittest import mock

        class _Interrupting(FakeSshRunner):
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                raise KeyboardInterrupt("operator hit Ctrl-C")

        class _ReapFails(FakeProvider):
            def list_labeled(self, run_id: str) -> list[Host]:
                raise providers.ProviderError("request timeout")

        prov = _ReapFails()
        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r"
            buf = io.StringIO()
            with mock.patch.object(cli, "HetznerProvider", lambda: prov), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: _Interrupting()), \
                 redirect_stderr(buf), \
                 self.assertRaises(KeyboardInterrupt):
                cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu",
                    "--results-dir", str(rdir), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes",
                ])
            self.assertIn("backstop reap ALSO failed", buf.getvalue())
            self.assertIn("vmlease reap --run-token cli-run", buf.getvalue())

    def test_reap_lists_destroyed(self) -> None:
        from unittest import mock

        prov = FakeProvider()
        prov.create_with_cloudinit(HostSpec(name="n", image="i", server_type="cpx22", distro_key="ubuntu", labels={"vmlease": "cli-run"}), "ci")
        buf = io.StringIO()
        with mock.patch.object(cli, "HetznerProvider", lambda: prov), redirect_stdout(buf):
            rc = cli.main(["reap", "--run-token", "cli-run"])
        self.assertEqual(rc, 0)
        self.assertIn("reaped 1 host", buf.getvalue())

    def test_status_lists_live(self) -> None:
        from unittest import mock

        prov = FakeProvider()
        prov.create_with_cloudinit(HostSpec(name="n", image="i", server_type="cpx22", distro_key="ubuntu", labels={"vmlease": "cli-run"}), "ci")
        buf = io.StringIO()
        with mock.patch.object(cli, "HetznerProvider", lambda: prov), redirect_stdout(buf):
            rc = cli.main(["status", "--run-token", "cli-run"])
        self.assertEqual(rc, 0)
        self.assertIn("1 live host", buf.getvalue())

    def test_reap_provider_error_returns_1(self) -> None:
        from unittest import mock

        class _Boom:
            def list_labeled(self, run_id: str) -> list[Host]:
                raise providers.ProviderError("boom")

        with mock.patch.object(cli, "HetznerProvider", _Boom):
            rc = cli.main(["reap", "--run-token", "cli-run"])
        self.assertEqual(rc, 1)


# --------------------------------------------------------------------------- #
# archimage — resolve-latest + SHA256 + pinned-key signature verify
# --------------------------------------------------------------------------- #
class TestArchImage(unittest.TestCase):
    _INDEX = """
    <a href="v20260101.100000/">v20260101.100000/</a>
    <a href="v20260515.530093/">v20260515.530093/</a>
    <a href="v20260301.420000/">v20260301.420000/</a>
    """

    def test_parse_versions_dedupes_and_sorts(self) -> None:
        vs = archimage.parse_versions(self._INDEX + " v20260515.530093")
        self.assertEqual([v.tag for v in vs], ["v20260101.100000", "v20260301.420000", "v20260515.530093"])

    def test_latest_version(self) -> None:
        self.assertEqual(archimage.latest_version(self._INDEX).tag, "v20260515.530093")

    def test_latest_version_serial_breaks_tie(self) -> None:
        idx = "v20260515.1 v20260515.30 v20260515.2"
        self.assertEqual(archimage.latest_version(idx).serial, 30)

    def test_no_versions_raises(self) -> None:
        with self.assertRaises(archimage.ArchImageError):
            archimage.latest_version("nothing here")

    def test_urls(self) -> None:
        v = archimage.latest_version(self._INDEX)
        self.assertTrue(archimage.qcow2_url(v).endswith("v20260515.530093/Arch-Linux-x86_64-cloudimg.qcow2"))
        self.assertTrue(archimage.sha256_url(v).endswith(".qcow2.SHA256"))
        self.assertTrue(archimage.sig_url(v).endswith(".qcow2.sig"))

    def test_parse_expected_sha256(self) -> None:
        digest = "a" * 64
        self.assertEqual(archimage.parse_expected_sha256(f"{digest}  Arch-Linux-x86_64-cloudimg.qcow2"), digest)

    def test_parse_expected_sha256_missing(self) -> None:
        with self.assertRaises(archimage.ArchImageError):
            archimage.parse_expected_sha256("no digest here")

    def test_verify_sha256_ok_and_mismatch(self) -> None:
        data = b"qcow2-bytes"
        good = hashlib.sha256(data).hexdigest()
        archimage.verify_sha256(data, good)  # no raise
        with self.assertRaises(archimage.ArchImageError):
            archimage.verify_sha256(data, "b" * 64)

    def test_build_gpg_verify_argv(self) -> None:
        argv = archimage.build_gpg_verify_argv("/t/x.sig", "/t/x.qcow2", "/t/keyring.gpg")
        self.assertIn("--keyring", argv)
        self.assertIn("/t/keyring.gpg", argv)
        self.assertEqual(argv[-3:], ["--verify", "/t/x.sig", "/t/x.qcow2"])

    def test_verify_signature_accepts_pinned_key(self) -> None:
        fp = "ABCD1234" * 5  # 40-hex fingerprint
        status = f"[GNUPG:] VALIDSIG {fp} 2026-05-15 ...\n"
        archimage.verify_signature("s", "q", "k", fp, gpg_runner=_fake_subprocess(0, status))  # no raise

    def test_verify_signature_pinned_primary_matches_subkey_sig(self) -> None:
        # Real gpg shape: VALIDSIG <signing-subkey-fp> <date> <ts> ... <primary-fp>.
        # arch-boxes signs with a [S] subkey under a certify-only [C] primary, so
        # pinning the PRIMARY fingerprint must validate a subkey-made signature.
        primary = archimage.DEFAULT_ARCH_KEY_FINGERPRINT
        subkey = "00FF" * 10  # the subkey that actually signed (different fp)
        status = f"[GNUPG:] VALIDSIG {subkey} 2026-06-01 1700000000 0 4 0 22 10 00 {primary}\n"
        archimage.verify_signature("s", "q", "k", primary, gpg_runner=_fake_subprocess(0, status))  # no raise

    def test_verify_signature_rejects_wrong_key(self) -> None:
        good = "ABCD1234" * 5
        other = "9999FFFF" * 5
        status = f"[GNUPG:] VALIDSIG {other} ...\n"
        with self.assertRaises(archimage.ArchImageError):
            archimage.verify_signature("s", "q", "k", good, gpg_runner=_fake_subprocess(0, status))

    def test_verify_signature_gpg_nonzero(self) -> None:
        with self.assertRaises(archimage.ArchImageError):
            archimage.verify_signature("s", "q", "k", "F" * 40, gpg_runner=_fake_subprocess(1, "", "bad sig"))

    def test_verify_signature_skips_noise_lines_and_bare_validsig(self) -> None:
        fp = "ABCD1234" * 5
        # a non-VALIDSIG line (skipped), a bare VALIDSIG with no fields (skipped),
        # then the real line — exercises both defensive continues.
        status = f"[GNUPG:] NEWSIG\n[GNUPG:] VALIDSIG\n[GNUPG:] VALIDSIG {fp} 2026 0 0\n"
        archimage.verify_signature("s", "q", "k", fp, gpg_runner=_fake_subprocess(0, status))  # no raise

    def test_resolve_and_verify_end_to_end(self) -> None:
        fp = "ABCD1234" * 5
        qcow = b"the-disk-image"
        sha = hashlib.sha256(qcow).hexdigest()

        def text_fetcher(url: str) -> str:
            if url == archimage.MIRROR_BASE:
                return self._INDEX
            if url.endswith(".SHA256"):
                return f"{sha}  Arch-Linux-x86_64-cloudimg.qcow2"
            raise AssertionError(url)

        def fetcher(url: str) -> bytes:
            return qcow if url.endswith(".qcow2") else b"SIGBYTES"

        written: list[bytes] = []

        def write_temp(b: bytes) -> str:
            written.append(b)
            return f"/tmp/probe-{len(written)}"

        result = archimage.resolve_and_verify(
            text_fetcher=text_fetcher, fetcher=fetcher,
            gpg_runner=_fake_subprocess(0, f"[GNUPG:] VALIDSIG {fp} ...\n"),
            keyring_path="/k", expected_fingerprint=fp, write_temp=write_temp,
        )
        self.assertEqual(result.version.tag, "v20260515.530093")
        self.assertEqual(result.expected_sha256, sha)

    def test_resolve_and_verify_sha_mismatch_raises(self) -> None:
        def text_fetcher(url: str) -> str:
            return self._INDEX if url == archimage.MIRROR_BASE else f"{'0' * 64}  x.qcow2"

        with self.assertRaises(archimage.ArchImageError):
            archimage.resolve_and_verify(
                text_fetcher=text_fetcher, fetcher=lambda _u: b"data",
                gpg_runner=_fake_subprocess(0, ""), keyring_path="/k",
                expected_fingerprint="F" * 40, write_temp=lambda _b: "/tmp/x",
            )


# --------------------------------------------------------------------------- #
# runner — the rescue-write seam
# --------------------------------------------------------------------------- #
class TestRunnerRescueWrite(unittest.TestCase):
    def _arch_matrix(self) -> runner.Matrix:
        return runner.Matrix(_demo_workload(), ("arch",), "cpx22", "run-rw")

    def _factory(self, ssh_runner: ssh.SshRunner) -> Callable[[str, keypair.Keypair], ssh.SshRunner]:
        return lambda _op, _kp: ssh_runner

    def test_rescue_writer_invoked_for_arch(self) -> None:
        prov = FakeProvider()
        calls: list[tuple[str, str]] = []

        def writer(host: Host, profile: distro.DistroProfile) -> None:
            calls.append((host.name, profile.key))

        with tempfile.TemporaryDirectory() as d:
            runner.execute(self._arch_matrix(), prov, self._factory(FakeSshRunner()), _fake_keypair(Path(d)), "probe", rescue_writer=writer)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "arch")
        self.assertEqual(len(prov.destroyed), 1)  # still torn down

    def test_native_distro_does_not_invoke_writer(self) -> None:
        prov = FakeProvider()
        calls: list[str] = []
        m = runner.Matrix(_demo_workload(), ("ubuntu",), "cpx22", "run-nat")
        with tempfile.TemporaryDirectory() as d:
            runner.execute(m, prov, self._factory(FakeSshRunner()), _fake_keypair(Path(d)), "probe",
                           rescue_writer=lambda h, p: calls.append(h.name))
        self.assertEqual(calls, [])

    def test_missing_writer_for_arch_recorded_and_torn_down(self) -> None:
        # per-host isolation: a missing rescue-writer is captured as an error
        # HostRun (not a propagating raise) and the base host is still torn down.
        prov = FakeProvider()
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(self._arch_matrix(), prov, self._factory(FakeSshRunner()), _fake_keypair(Path(d)), "probe")
        self.assertEqual(len(prov.destroyed), 1)  # created host still torn down
        self.assertTrue(runs[0].detail.startswith("ERROR:"))


# --------------------------------------------------------------------------- #
# archbuild — ephemeral rescue-write transform (pure builders/parsers)
# --------------------------------------------------------------------------- #
class TestArchBuild(unittest.TestCase):
    def test_qcow2_name_is_cloudimg(self) -> None:
        # the cloudimg variant (has cloud-init), NOT basic
        self.assertEqual(archimage.QCOW2_NAME, "Arch-Linux-x86_64-cloudimg.qcow2")

    def test_enable_rescue_argv_no_output_flag(self) -> None:
        argv = archbuild.build_enable_rescue_argv("12345678", "mykey")
        self.assertIn("enable-rescue", argv)
        self.assertIn("linux64", argv)
        self.assertIn("mykey", argv)
        self.assertNotIn("--output", argv)  # enable-rescue has no --output

    def test_disable_and_reset_argv(self) -> None:
        self.assertEqual(archbuild.build_disable_rescue_argv("5"), ["hcloud", "server", "disable-rescue", "5"])
        self.assertEqual(archbuild.build_reset_argv("5"), ["hcloud", "server", "reset", "5"])

    def test_parse_rescue_password(self) -> None:
        out = "Rescue enabled for server 12345678 with root password: r3scueFAKEpw00\n"
        self.assertEqual(archbuild.parse_rescue_password(out), "r3scueFAKEpw00")

    def test_parse_rescue_password_missing(self) -> None:
        with self.assertRaises(archbuild.ArchBuildError):
            archbuild.parse_rescue_password("Rescue enabled, no password line\n")

    def test_render_rescue_script_remote_url_renders_curl(self) -> None:
        from vmlease.rescue_image import RemoteUrl
        s = archbuild.render_rescue_script(RemoteUrl("https://m/Arch.qcow2"), "a" * 64)
        self.assertIn("https://m/Arch.qcow2", s)
        # a metachar-free URL needs no quoting; shlex.quote leaves it bare
        self.assertIn(f"curl -fsSL https://m/Arch.qcow2 -o {archbuild.RESCUE_IMAGE_PATH}", s)
        self.assertIn("a" * 64, s)
        # the script probes the disk (never hardcodes sda) and re-verifies the sha
        self.assertIn("lsblk", s)
        self.assertIn("sha256sum -c", s)
        self.assertIn("qemu-img convert -O raw", s)
        # shell vars pass through the @@ templating untouched
        self.assertIn("$disk", s)

    def test_render_rescue_script_local_file_renders_presence_check(self) -> None:
        from vmlease.rescue_image import LocalFile
        s = archbuild.render_rescue_script(LocalFile(Path("/local/golden.qcow2")), "b" * 64)
        # a LocalFile is already pushed → the script asserts presence, does NOT curl
        self.assertIn(f"test -f {archbuild.RESCUE_IMAGE_PATH}", s)
        self.assertNotIn("curl -fsSL", s)  # no actual fetch command (the comment mentions curl)
        # both sources still re-verify the sha on the rescue side
        self.assertIn("sha256sum -c", s)
        self.assertIn("b" * 64, s)

    def test_render_fetch_cmd_remote_url_is_shell_quoted(self) -> None:
        from vmlease.rescue_image import RemoteUrl
        # a URL with a single quote + shell metachars must not break out of the
        # root-side script — shlex.quote escapes it safely.
        evil = "https://m/x.qcow2'; rm -rf /; echo '"
        cmd = archbuild.render_fetch_cmd(RemoteUrl(evil))
        self.assertEqual(
            cmd,
            f"curl -fsSL {shlex.quote(evil)} -o {archbuild.RESCUE_IMAGE_PATH} "
            "|| { echo 'RESCUE_FAIL: download failed' >&2; exit 13; }",
        )
        # the quoted argument round-trips back to the exact URL under shell parsing,
        # so the injection payload is inert (a single curl arg, not extra commands).
        _, _, rest = cmd.partition("curl -fsSL ")
        quoted_arg = rest[: rest.index(" -o ")]
        self.assertEqual(shlex.split(quoted_arg), [evil])

    # The Arch profile's spec resolves the latest mirror image; these fakes drive
    # its injected IO seams (ResolveDeps) so it produces the real RemoteUrl + sha
    # — byte-faithful to the pre-extraction resolve-latest + sha + pinned-GPG.
    _ARCH_QCOW = b"the-disk-image"

    def _arch_resolve_deps(self) -> rescue_image.ResolveDeps:
        from vmlease.archimage import DEFAULT_ARCH_KEY_FINGERPRINT
        from vmlease.rescue_image import ResolveDeps
        sha = hashlib.sha256(self._ARCH_QCOW).hexdigest()

        def text_fetcher(url: str) -> str:
            if url == archimage.MIRROR_BASE:
                return "v20260601.539459/"
            return f"{sha}  Arch-Linux-x86_64-cloudimg.qcow2"

        def fetcher(url: str) -> bytes:
            return self._ARCH_QCOW if url.endswith(".qcow2") else b"SIGBYTES"

        return ResolveDeps(
            text_fetcher=text_fetcher, fetcher=fetcher,
            gpg_runner=_fake_subprocess(0, f"[GNUPG:] VALIDSIG {DEFAULT_ARCH_KEY_FINGERPRINT} 2026 0 0\n"),
            write_temp=lambda _b: "/tmp/stage", keyring_path="/k",
        )

    def _deps(self, *, ssh_out: str = "RESCUE_WRITE_OK", ssh_rc: int = 0, cli_rc: int = 0,
              resolve_deps: rescue_image.ResolveDeps | None = None,
              push_calls: list[tuple[str, Path, str]] | None = None,
              ) -> tuple[archbuild.RescueWriteDeps, list[list[str]]]:
        cli_calls: list[list[str]] = []

        def cli(argv: list[str]) -> tuple[int, str, str]:
            cli_calls.append(argv)
            return (cli_rc, "", "" if cli_rc == 0 else "boom")

        def ssh_root(_ip: str, _script: str) -> tuple[int, str]:
            return (ssh_rc, ssh_out)

        def push_to_rescue(ip: str, local: Path, remote: str) -> None:
            if push_calls is not None:
                push_calls.append((ip, local, remote))

        deps = archbuild.RescueWriteDeps(
            resolve_deps=resolve_deps if resolve_deps is not None else self._arch_resolve_deps(),
            cli=cli, ssh_root=ssh_root, wait_rescue_ready=lambda _ip: None,
            push_to_rescue=push_to_rescue,
        )
        return deps, cli_calls

    def _host(self) -> Host:
        return Host(id="42", name="vmlease-run-rw-arch", ipv4="1.2.3.4")

    def test_rescue_write_host_happy_path(self) -> None:
        deps, cli_calls = self._deps()
        archbuild.rescue_write_host(self._host(), distro.get_profile("arch"), deps, "mykey")
        verbs = [a[2] for a in cli_calls]  # hcloud server <verb>
        self.assertEqual(verbs, ["enable-rescue", "reset", "disable-rescue", "reset"])

    def test_rescue_write_host_remote_url_no_push(self) -> None:
        # Arch resolves a RemoteUrl → the rescue side curls it; no scp push.
        push_calls: list[tuple[str, Path, str]] = []
        deps, _ = self._deps(push_calls=push_calls)
        archbuild.rescue_write_host(self._host(), distro.get_profile("arch"), deps, "mykey")
        self.assertEqual(push_calls, [])

    def test_rescue_write_host_write_failure_raises(self) -> None:
        deps, _ = self._deps(ssh_out="RESCUE_FAIL: sha mismatch", ssh_rc=14)
        with self.assertRaises(archbuild.ArchBuildError):
            archbuild.rescue_write_host(self._host(), distro.get_profile("arch"), deps, "mykey")

    def test_rescue_write_host_cli_failure_raises(self) -> None:
        deps, _ = self._deps(cli_rc=1)
        with self.assertRaises(archbuild.ArchBuildError):
            archbuild.rescue_write_host(self._host(), distro.get_profile("arch"), deps, "mykey")

    def test_local_file_pushed_before_script(self) -> None:
        # a golden LocalFile spec → push_to_rescue invoked with the fixed remote path.
        from vmlease.rescue_image import GoldenRescueImageSpec
        qcow = b"golden-bytes"
        sha = hashlib.sha256(qcow).hexdigest()
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "golden.qcow2"
            local.write_bytes(qcow)
            profile = distro.DistroProfile(
                key="golden", default_image="debian-13", package_manager="apt",
                packages=(), rescue_image=GoldenRescueImageSpec(sha256=sha, path=local),
            )
            push_calls: list[tuple[str, Path, str]] = []
            deps, _ = self._deps(push_calls=push_calls)
            archbuild.rescue_write_host(self._host(), profile, deps, "mykey")
        self.assertEqual(len(push_calls), 1)
        self.assertEqual(push_calls[0][0], "1.2.3.4")
        self.assertEqual(push_calls[0][1], local)
        self.assertEqual(push_calls[0][2], archbuild.RESCUE_IMAGE_PATH)

    def test_trust_gate_aborts_before_any_mutation(self) -> None:
        # resolve_and_verify (SHA256 + pinned-key signature) runs FIRST; a bad image
        # raises BEFORE any hcloud cli step → zero mutations against an untrusted image.
        from vmlease.archimage import ArchImageError
        from vmlease.rescue_image import GoldenRescueImageSpec
        profile = distro.DistroProfile(
            key="golden", default_image="debian-13", package_manager="apt",
            packages=(), rescue_image=GoldenRescueImageSpec(sha256="0" * 64, url="https://m/x.qcow2"),
        )
        # the fetched bytes' sha will NOT match the pinned digest → refusal.
        from vmlease.rescue_image import ResolveDeps
        rd = ResolveDeps(
            text_fetcher=lambda _u: "", fetcher=lambda _u: b"not-the-pinned-bytes",
            gpg_runner=_fake_subprocess(0, ""), write_temp=lambda _b: "/tmp/x", keyring_path="/k",
        )
        deps, cli_calls = self._deps(resolve_deps=rd)
        with self.assertRaises(ArchImageError):
            archbuild.rescue_write_host(self._host(), profile, deps, "mykey")
        self.assertEqual(cli_calls, [])  # NOTHING ran — fail-closed before enable-rescue

    def _live_fakes(self, qcow: bytes) -> tuple[Callable[[list[str], str | None], tuple[int, str, str]], list[list[str]], Callable[[str], str], Callable[[str], bytes]]:
        from vmlease.archimage import DEFAULT_ARCH_KEY_FINGERPRINT
        sha = hashlib.sha256(qcow).hexdigest()
        runs: list[list[str]] = []

        def fake_run(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
            runs.append(argv)
            if argv[0] == "gpg":  # the trust-gate verify: emit a VALIDSIG for the pinned key
                return (0, f"[GNUPG:] VALIDSIG {DEFAULT_ARCH_KEY_FINGERPRINT} 2026 0 0\n", "")
            if argv[0] == "ssh":
                # mimic `bash -s` for each step's stdin script: the rescue-write
                # script -> RESCUE_WRITE_OK; the rescue readiness probe
                # (cat /etc/hostname) -> `rescue` (so _wait_rescue returns).
                if stdin and "qemu-img" in stdin:
                    return (0, "RESCUE_WRITE_OK", "")
                if stdin and "hostname" in stdin:
                    return (0, "rescue\n", "")
                return (0, "", "")
            return (0, "", "")  # hcloud cli steps

        def fake_text(url: str) -> str:
            return f"{sha}  img.qcow2" if url.endswith(".SHA256") else "v20260601.539459/"

        def fake_bytes(url: str) -> bytes:
            return qcow if url.endswith(".qcow2") else b"SIGBYTES"

        return fake_run, runs, fake_text, fake_bytes

    def test_build_live_rescue_writer_wires_and_runs(self) -> None:
        # exercise the live FACTORY wiring with injected fakes (no real I/O):
        # the trust-gate verify, the ssh argv shape, the readiness retry, full path.
        fake_run, runs, fake_text, fake_bytes = self._live_fakes(b"the-disk-image")
        writer = archbuild.build_live_rescue_writer(
            "/home/op/.ssh/key", "mykey", "/tmp/keyring.gpg",
            run=fake_run, sleep=lambda _s: None,
            fetch_text=fake_text, fetch_bytes=fake_bytes, write_temp=lambda _b: "/tmp/stage",
        )
        writer(Host(id="42", name="vmlease-arch", ipv4="1.2.3.4"), distro.get_profile("arch"))
        # the trust gate (gpg verify) ran, AND the ssh argv carries the injected key
        self.assertTrue(any(a[0] == "gpg" for a in runs))
        ssh_calls = [a for a in runs if a[0] == "ssh"]
        self.assertIn("/home/op/.ssh/key", ssh_calls[0])
        self.assertIn("StrictHostKeyChecking=accept-new", ssh_calls[0])

    def test_build_live_rescue_writer_local_file_scps(self) -> None:
        # a golden LOCAL profile through the live factory → the push_to_rescue
        # closure scps the file (root@ip via the rescue key) before the script.
        from vmlease.rescue_image import GoldenRescueImageSpec
        qcow = b"golden-local"
        sha = hashlib.sha256(qcow).hexdigest()
        runs: list[list[str]] = []

        def fake_run(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
            runs.append(argv)
            if argv[0] == "ssh":
                if stdin and "hostname" in stdin:
                    return (0, "rescue\n", "")
                return (0, "RESCUE_WRITE_OK", "")
            return (0, "", "")

        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "golden.qcow2"
            local.write_bytes(qcow)
            profile = distro.DistroProfile(
                key="golden", default_image="debian-13", package_manager="apt",
                packages=(), rescue_image=GoldenRescueImageSpec(sha256=sha, path=local),
            )
            writer = archbuild.build_live_rescue_writer(
                "/home/op/.ssh/rescue", "mykey", "/tmp/keyring.gpg",
                run=fake_run, sleep=lambda _s: None,
            )
            writer(Host(id="9", name="g", ipv4="1.2.3.4"), profile)
        scp_calls = [a for a in runs if a[0] == "scp"]
        self.assertEqual(len(scp_calls), 1)
        self.assertIn("/home/op/.ssh/rescue", scp_calls[0])  # the rescue key
        self.assertIn(f"root@1.2.3.4:{archbuild.RESCUE_IMAGE_PATH}", scp_calls[0])
        self.assertNotIn("gpg", [a[0] for a in runs])  # golden → no gpg verify

    def test_build_live_rescue_writer_scp_failure_raises(self) -> None:
        from vmlease.rescue_image import GoldenRescueImageSpec
        qcow = b"golden-local"
        sha = hashlib.sha256(qcow).hexdigest()

        def fake_run(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
            if argv[0] == "scp":
                return (1, "", "permission denied")
            if argv[0] == "ssh" and stdin and "hostname" in stdin:
                return (0, "rescue\n", "")
            return (0, "", "")

        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "golden.qcow2"
            local.write_bytes(qcow)
            profile = distro.DistroProfile(
                key="golden", default_image="debian-13", package_manager="apt",
                packages=(), rescue_image=GoldenRescueImageSpec(sha256=sha, path=local),
            )
            writer = archbuild.build_live_rescue_writer(
                "/k", "mykey", "/tmp/keyring.gpg", run=fake_run, sleep=lambda _s: None,
            )
            with self.assertRaises(archbuild.ArchBuildError):
                writer(Host(id="9", name="g", ipv4="1.2.3.4"), profile)

    def test_rescue_write_host_no_spec_raises(self) -> None:
        # a profile with no rescue_image spec cannot be rescue-written (guard).
        profile = distro.DistroProfile(
            key="nope", default_image="debian-13", package_manager="apt", packages=(),
        )
        deps, _ = self._deps()
        with self.assertRaises(archbuild.ArchBuildError):
            archbuild.rescue_write_host(self._host(), profile, deps, "mykey")

    def test_build_live_rescue_writer_readiness_timeout(self) -> None:
        # verify passes, but ssh never returns 0 → bounded readiness loop raises
        fake_run, _runs, fake_text, fake_bytes = self._live_fakes(b"img")

        def run_ssh_down(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
            if argv[0] == "ssh":
                return (255, "", "no route")
            return fake_run(argv, stdin)

        writer = archbuild.build_live_rescue_writer(
            "/k", "mykey", "/tmp/keyring.gpg", run=run_ssh_down, sleep=lambda _s: None,
            fetch_text=fake_text, fetch_bytes=fake_bytes, write_temp=lambda _b: "/tmp/stage",
        )
        with self.assertRaises(archbuild.ArchBuildError):
            writer(Host(id="1", name="n", ipv4="1.2.3.4"), distro.get_profile("arch"))

    def test_build_keyring_import_argv(self) -> None:
        argv = archbuild.build_keyring_import_argv("/tmp/kr.gpg", "FP")
        self.assertIn("--recv-keys", argv)
        self.assertIn("FP", argv)
        self.assertIn("/tmp/kr.gpg", argv)

    def test_ensure_arch_keyring_failure_raises(self) -> None:
        with self.assertRaises(archbuild.ArchBuildError):
            archbuild.ensure_arch_keyring("/tmp/kr.gpg", lambda _argv, _stdin: (2, "", "no network"))

    def test_ensure_arch_keyring_success(self) -> None:
        calls: list[list[str]] = []

        def ok(argv: list[str], _stdin: str | None) -> tuple[int, str, str]:
            calls.append(argv)
            return (0, "imported", "")

        archbuild.ensure_arch_keyring("/tmp/kr.gpg", ok, "FP")
        self.assertIn("--recv-keys", calls[0])

    def test_live_write_temp_stages_bytes(self) -> None:
        path = archbuild._live_write_temp(b"qcow2-bytes")
        try:
            self.assertEqual(Path(path).read_bytes(), b"qcow2-bytes")
        finally:
            Path(path).unlink()


# --------------------------------------------------------------------------- #
# rescue_image — the RescueImageSpec seam (Arch + golden)
# --------------------------------------------------------------------------- #
class TestRescueImageSpec(unittest.TestCase):
    def _arch_deps(self, qcow: bytes) -> rescue_image.ResolveDeps:
        from vmlease.archimage import DEFAULT_ARCH_KEY_FINGERPRINT
        from vmlease.rescue_image import ResolveDeps
        sha = hashlib.sha256(qcow).hexdigest()

        def text_fetcher(url: str) -> str:
            return "v20260601.539459/" if url == archimage.MIRROR_BASE else f"{sha}  x.qcow2"

        return ResolveDeps(
            text_fetcher=text_fetcher,
            fetcher=lambda u: qcow if u.endswith(".qcow2") else b"SIG",
            gpg_runner=_fake_subprocess(0, f"[GNUPG:] VALIDSIG {DEFAULT_ARCH_KEY_FINGERPRINT} 2026 0 0\n"),
            write_temp=lambda _b: "/tmp/stage", keyring_path="/k",
        )

    def _golden_deps(self, *, fetcher: Callable[[str], bytes] | None = None) -> rescue_image.ResolveDeps:
        from vmlease.rescue_image import ResolveDeps
        # text_fetcher/gpg_runner must NEVER be touched by a golden spec → make them
        # raise so any accidental use fails loudly.
        def boom_text(_u: str) -> str:
            raise AssertionError("golden spec must not call text_fetcher")

        def boom_gpg(_argv: list[str]) -> subprocess.CompletedProcess[str]:
            raise AssertionError("golden spec must not invoke gpg")

        return ResolveDeps(
            text_fetcher=boom_text, fetcher=fetcher if fetcher is not None else (lambda _u: b""),
            gpg_runner=boom_gpg, write_temp=lambda _b: "/tmp/x", keyring_path="/k",
        )

    def test_arch_spec_yields_remote_url_and_sha(self) -> None:
        from vmlease.rescue_image import ArchRescueImageSpec, RemoteUrl
        qcow = b"the-disk-image"
        sha = hashlib.sha256(qcow).hexdigest()
        spec = ArchRescueImageSpec(fingerprint=archimage.DEFAULT_ARCH_KEY_FINGERPRINT)
        resolved = spec.resolve_and_verify(self._arch_deps(qcow))
        self.assertEqual(resolved.expected_sha256, sha)
        self.assertIsInstance(resolved.source, RemoteUrl)
        assert isinstance(resolved.source, RemoteUrl)
        self.assertTrue(resolved.source.url.endswith("v20260601.539459/Arch-Linux-x86_64-cloudimg.qcow2"))

    def test_golden_url_yields_remote_url_no_gpg(self) -> None:
        from vmlease.rescue_image import GoldenRescueImageSpec, RemoteUrl
        qcow = b"golden-url-bytes"
        sha = hashlib.sha256(qcow).hexdigest()
        spec = GoldenRescueImageSpec(sha256=sha, url="https://m/golden.qcow2")
        resolved = spec.resolve_and_verify(self._golden_deps(fetcher=lambda _u: qcow))
        self.assertEqual(resolved.expected_sha256, sha)
        self.assertIsInstance(resolved.source, RemoteUrl)
        assert isinstance(resolved.source, RemoteUrl)
        self.assertEqual(resolved.source.url, "https://m/golden.qcow2")

    def test_golden_local_yields_local_file_no_gpg(self) -> None:
        from vmlease.rescue_image import GoldenRescueImageSpec, LocalFile
        qcow = b"golden-local-bytes"
        sha = hashlib.sha256(qcow).hexdigest()
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "golden.qcow2"
            local.write_bytes(qcow)
            spec = GoldenRescueImageSpec(sha256=sha, path=local)
            resolved = spec.resolve_and_verify(self._golden_deps())  # fetcher unused
            self.assertEqual(resolved.expected_sha256, sha)
            self.assertIsInstance(resolved.source, LocalFile)
            assert isinstance(resolved.source, LocalFile)
            self.assertEqual(resolved.source.path, local)

    def test_golden_url_sha_mismatch_refuses(self) -> None:
        from vmlease.archimage import ArchImageError
        from vmlease.rescue_image import GoldenRescueImageSpec
        spec = GoldenRescueImageSpec(sha256="0" * 64, url="https://m/golden.qcow2")
        with self.assertRaises(ArchImageError):
            spec.resolve_and_verify(self._golden_deps(fetcher=lambda _u: b"wrong-bytes"))

    def test_golden_local_sha_mismatch_refuses(self) -> None:
        from vmlease.archimage import ArchImageError
        from vmlease.rescue_image import GoldenRescueImageSpec
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "golden.qcow2"
            local.write_bytes(b"wrong-bytes")
            spec = GoldenRescueImageSpec(sha256="0" * 64, path=local)
            with self.assertRaises(ArchImageError):
                spec.resolve_and_verify(self._golden_deps())

    def test_golden_requires_exactly_one_source(self) -> None:
        from vmlease.archimage import ArchImageError
        from vmlease.rescue_image import GoldenRescueImageSpec
        # neither url nor path
        with self.assertRaises(ArchImageError):
            GoldenRescueImageSpec(sha256="a" * 64).resolve_and_verify(self._golden_deps())
        # both url AND path
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "x.qcow2"
            local.write_bytes(b"x")
            both = GoldenRescueImageSpec(sha256="a" * 64, url="https://m/x", path=local)
            with self.assertRaises(ArchImageError):
                both.resolve_and_verify(self._golden_deps())

    def test_both_instances_satisfy_protocol(self) -> None:
        from vmlease.rescue_image import ArchRescueImageSpec, GoldenRescueImageSpec, RescueImageSpec
        arch = ArchRescueImageSpec(fingerprint="F")
        golden = GoldenRescueImageSpec(sha256="a" * 64, url="https://m/x")
        self.assertIsInstance(arch, RescueImageSpec)
        self.assertIsInstance(golden, RescueImageSpec)


# --------------------------------------------------------------------------- #
# distro — the rescue-write Arch profile
# --------------------------------------------------------------------------- #
class TestDistroRescue(unittest.TestCase):
    def test_arch_needs_rescue_write(self) -> None:
        from vmlease.rescue_image import ArchRescueImageSpec, RescueImageSpec
        arch = distro.get_profile("arch")
        self.assertTrue(arch.needs_rescue_write)
        self.assertIsInstance(arch.rescue_image, ArchRescueImageSpec)
        self.assertIsInstance(arch.rescue_image, RescueImageSpec)  # Protocol conformance
        assert isinstance(arch.rescue_image, ArchRescueImageSpec)
        self.assertEqual(arch.rescue_image.fingerprint, archimage.DEFAULT_ARCH_KEY_FINGERPRINT)
        self.assertEqual(arch.default_image, "debian-13")  # cheap base to rescue-write

    def test_native_distros_do_not_need_rescue_write(self) -> None:
        for key in ("ubuntu", "debian", "fedora"):
            profile = distro.get_profile(key)
            self.assertFalse(profile.needs_rescue_write)
            self.assertIsNone(profile.rescue_image)


def _null_deps() -> rescue_image.ResolveDeps:
    """A ResolveDeps whose seams all raise — for native paths that never resolve.

    A native distro's base fingerprint is the arch-blind slug; it must NEVER
    touch a resolve seam, so make every seam fail loudly if it does.
    """
    from vmlease.rescue_image import ResolveDeps

    def boom_text(_u: str) -> str:
        raise AssertionError("native path must not call text_fetcher")

    def boom_bytes(_u: str) -> bytes:
        raise AssertionError("native path must not call fetcher")

    def boom_gpg(_argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError("native path must not invoke gpg")

    return ResolveDeps(
        text_fetcher=boom_text, fetcher=boom_bytes, gpg_runner=boom_gpg,
        write_temp=lambda _b: "/tmp/x", keyring_path="/k",
    )


class _FakeResolveSpec:
    """A fake RescueImageSpec returning a fixed sha (or raising) — no network."""

    def __init__(self, *, sha: str = "", raises: BaseException | None = None) -> None:
        self._sha = sha
        self._raises = raises

    def resolve_and_verify(self, deps: rescue_image.ResolveDeps, /) -> rescue_image.ResolvedRescueImage:
        if self._raises is not None:
            raise self._raises
        from vmlease.rescue_image import RemoteUrl, ResolvedRescueImage
        return ResolvedRescueImage(expected_sha256=self._sha, source=RemoteUrl("https://m/x.qcow2"))


def _rescue_profile(spec: object) -> distro.DistroProfile:
    """A minimal rescue-write profile carrying an injected fake spec."""
    from vmlease.rescue_image import RescueImageSpec
    assert isinstance(spec, RescueImageSpec)
    base = distro.get_profile("arch")
    return distro.DistroProfile(
        key=base.key, default_image=base.default_image, package_manager=base.package_manager,
        packages=base.packages, extra_setup=base.extra_setup,
        system_update_override=base.system_update_override, rescue_image=spec, notes=base.notes,
    )


def _cache_image(
    *, distro_key: str, arch: str, key: str, img_id: str = "img-1",
    requires: tuple[str, ...] = (),
) -> Image:
    """A cache Image carrying the supersession-relevant labels (incl. requires-hash)."""
    return Image(
        id=img_id, created="2024-04-25T13:26:27+00:00", disk_size=40.0, arch=arch,
        labels={
            imagecache.LABEL_DISTRO: distro_key,
            imagecache.LABEL_ARCH: arch,
            imagecache.LABEL_CACHE_KEY: key,
            imagecache.LABEL_REQUIRES_HASH: imagecache.requires_hash(requires),
            imagecache.LABEL_REQUIRES: "\0".join(capabilities.canonical_requires(requires)),
        },
    )


class TestImageCacheBaseFingerprint(unittest.TestCase):
    def test_native_returns_arch_blind_slug(self) -> None:
        # ubuntu is native — the fingerprint is its provider slug, no resolve seam touched.
        ubuntu = distro.get_profile("ubuntu")
        fp = imagecache.base_fingerprint(ubuntu, "x86", _null_deps())
        self.assertEqual(fp, "ubuntu-24.04")

    def test_rescue_write_returns_resolved_digest(self) -> None:
        # an Arch-shaped rescue-write profile resolves to the spec's qcow2 sha.
        prof = _rescue_profile(_FakeResolveSpec(sha="deadbeef" * 8))
        fp = imagecache.base_fingerprint(prof, "x86", _null_deps())
        self.assertEqual(fp, "deadbeef" * 8)

    def test_golden_returns_pinned_sha(self) -> None:
        # a golden spec's resolve returns its pinned sha — same uniform call.
        from vmlease.rescue_image import GoldenRescueImageSpec
        sha = hashlib.sha256(b"golden").hexdigest()
        prof = _rescue_profile(GoldenRescueImageSpec(sha256=sha, url="https://m/golden.qcow2"))

        from vmlease.rescue_image import ResolveDeps
        deps = ResolveDeps(
            text_fetcher=lambda _u: "", fetcher=lambda _u: b"golden",
            gpg_runner=_fake_subprocess(0, ""), write_temp=lambda _b: "/tmp/x", keyring_path="/k",
        )
        self.assertEqual(imagecache.base_fingerprint(prof, "x86", deps), sha)


class TestImageCacheContentKey(unittest.TestCase):
    def test_same_recipe_same_key(self) -> None:
        ubuntu = distro.get_profile("ubuntu")
        k1 = imagecache.content_key(ubuntu, "x86", "probe", (), _null_deps())
        k2 = imagecache.content_key(ubuntu, "x86", "probe", (), _null_deps())
        self.assertEqual(k1, k2)
        self.assertTrue(k1.startswith("v1-ubuntu-"))
        self.assertEqual(len(k1), len("v1-ubuntu-") + 32)

    def test_recipe_change_changes_key(self) -> None:
        ubuntu = distro.get_profile("ubuntu")
        mutated = distro.DistroProfile(
            key=ubuntu.key, default_image=ubuntu.default_image, package_manager=ubuntu.package_manager,
            packages=(*ubuntu.packages, "htop"),  # a recipe change
        )
        self.assertNotEqual(
            imagecache.content_key(ubuntu, "x86", "probe", (), _null_deps()),
            imagecache.content_key(mutated, "x86", "probe", (), _null_deps()),
        )

    def test_different_arch_changes_key(self) -> None:
        # native's slug is arch-blind, so the arch-fold is what makes the key vary.
        ubuntu = distro.get_profile("ubuntu")
        self.assertNotEqual(
            imagecache.content_key(ubuntu, "x86", "probe", (), _null_deps()),
            imagecache.content_key(ubuntu, "arm", "probe", (), _null_deps()),
        )

    def test_operator_changes_key(self) -> None:
        # operator is part of the canonical render (baked user), so it is in the key.
        ubuntu = distro.get_profile("ubuntu")
        self.assertNotEqual(
            imagecache.content_key(ubuntu, "x86", "probe", (), _null_deps()),
            imagecache.content_key(ubuntu, "x86", "alice", (), _null_deps()),
        )

    def test_pinned_algorithm_exact_key(self) -> None:
        # A pinned exact-key assertion: this goes RED if the algorithm (SHA-256,
        # [:32], arch-fold, \0 separators, sentinel) or the sentinel value drifts.
        ubuntu = distro.get_profile("ubuntu")
        canonical = cloudinit.render_cloudinit(ubuntu, "probe", imagecache._CACHE_KEY_CANONICAL_PUBKEY, ())
        expected_payload = f"x86\0ubuntu-24.04\0{canonical}".encode()
        expected_digest = hashlib.sha256(expected_payload).hexdigest()[:32]
        expected = f"v1-ubuntu-{expected_digest}"
        self.assertEqual(imagecache.content_key(ubuntu, "x86", "probe", (), _null_deps()), expected)

    def test_sentinel_value_is_pinned(self) -> None:
        self.assertEqual(imagecache._CACHE_KEY_CANONICAL_PUBKEY, "vmlease-cache-key-canonical-pubkey")

    def test_requires_change_changes_key(self) -> None:
        # 4.4b: docker vs docker-less render distinct cloud-init ⇒ distinct keys
        # (the docker/docker-less cache entries are kept separate, D4/D-D).
        ubuntu = distro.get_profile("ubuntu")
        self.assertNotEqual(
            imagecache.content_key(ubuntu, "x86", "probe", (), _null_deps()),
            imagecache.content_key(ubuntu, "x86", "probe", ("docker",), _null_deps()),
        )

    def test_requires_order_invariant_key(self) -> None:
        # 4.4b: the key is invariant to requires ORDER/dups — the render
        # canonicalizes (sorted+deduped), so build-image's flag order and a
        # battery's declared order fold to one key (no silent permanent miss).
        ubuntu = distro.get_profile("ubuntu")
        self.assertEqual(
            imagecache.content_key(ubuntu, "x86", "probe", ("docker", "docker"), _null_deps()),
            imagecache.content_key(ubuntu, "x86", "probe", ("docker",), _null_deps()),
        )

    def test_build_and_run_derive_identical_key(self) -> None:
        # 4.4b: build-image's split derivation (content_key_from_base_fp, the
        # label path) and run's full content_key derive the SAME key for the same
        # requires — the determinism contract that makes a built docker image
        # cache-hit on a docker run.
        ubuntu = distro.get_profile("ubuntu")
        base_fp = imagecache.base_fingerprint(ubuntu, "x86", _null_deps())
        build_key = imagecache.content_key_from_base_fp(base_fp, ubuntu, "x86", "probe", ("docker",))
        run_key = imagecache.content_key(ubuntu, "x86", "probe", ("docker",), _null_deps())
        self.assertEqual(build_key, run_key)


def _cached_run_image(
    *, key: str, arch: str = "x86", disk_size: float = 40.0, img_id: str = "img-cache",
    requires: tuple[str, ...] = (),
) -> Image:
    """A purpose-labelled cache Image for the run-restore lookup (key + arch + disk)."""
    return Image(
        id=img_id, created="2024-04-25T13:26:27+00:00", disk_size=disk_size, arch=arch,
        labels={
            imagecache.LABEL_PURPOSE: imagecache.PURPOSE_IMAGE_CACHE,
            imagecache.LABEL_CACHE_KEY: key,
            imagecache.LABEL_DISTRO: "ubuntu",
            imagecache.LABEL_ARCH: arch,
            imagecache.LABEL_REQUIRES_HASH: imagecache.requires_hash(requires),
            imagecache.LABEL_REQUIRES: "\0".join(capabilities.canonical_requires(requires)),
        },
    )


class TestLookupCacheImage(unittest.TestCase):
    """runner._lookup_cache_image: hit | miss | oversized | wrong-arch | graceful.

    The thin cache-lookup helper returns the matched ``Image`` on a hit, ``None`` on
    a miss (no match / oversized / wrong arch / wrong key) or a graceful lookup
    failure. It renders NO cloud-init — the run path renders the right one on the
    branch that needs it.
    """

    def _ubuntu(self) -> distro.DistroProfile:
        return distro.get_profile("ubuntu")

    def _key(self, arch: str = "x86") -> str:
        return imagecache.content_key(self._ubuntu(), arch, "probe", (), _null_deps())

    def _call(self, prov: providers.Provider, *, arch: str = "x86", target_disk: float = 40.0,
              warn: Callable[[str], None] | None = None) -> Image | None:
        warns: list[str] = []
        return runner._lookup_cache_image(
            self._ubuntu(),
            operator="probe", arch=arch, requires=(), target_disk=target_disk,
            provider=prov, deps=_null_deps(),
            warn=warn if warn is not None else warns.append,
        )

    def test_hit_returns_the_matched_image(self) -> None:
        prov = FakeProvider()
        prov.images["img-cache"] = _cached_run_image(key=self._key())
        match = self._call(prov)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.id, "img-cache")

    def test_miss_no_image_returns_none(self) -> None:
        prov = FakeProvider()  # no images at all
        self.assertIsNone(self._call(prov))

    def test_oversized_image_is_a_miss(self) -> None:
        # disk_size (50) > target_disk (40): the snapshot can't restore here → miss.
        prov = FakeProvider()
        prov.images["img-big"] = _cached_run_image(key=self._key(), disk_size=50.0, img_id="img-big")
        self.assertIsNone(self._call(prov, target_disk=40.0))

    def test_equal_disk_is_a_hit(self) -> None:
        # the bound is <=: a snapshot whose disk equals the target restores.
        prov = FakeProvider()
        prov.images["img-eq"] = _cached_run_image(key=self._key(), disk_size=40.0, img_id="img-eq")
        match = self._call(prov, target_disk=40.0)
        assert match is not None
        self.assertEqual(match.id, "img-eq")

    def test_wrong_arch_image_is_a_miss(self) -> None:
        # the image's arch (arm) != the target arch (x86) → miss even if labelled.
        prov = FakeProvider()
        prov.images["img-arm"] = _cached_run_image(key=self._key("x86"), arch="arm", img_id="img-arm")
        self.assertIsNone(self._call(prov, arch="x86"))

    def test_wrong_key_image_is_a_miss(self) -> None:
        prov = FakeProvider()
        prov.images["img-other"] = _cached_run_image(key="v1-ubuntu-NOTOURKEY", img_id="img-other")
        self.assertIsNone(self._call(prov))

    def test_list_images_failure_is_graceful_miss_and_warns(self) -> None:
        # a list_images that raises → warn + miss (None) (the cache is advisory).
        class _ListBoom(FakeProvider):
            def list_images(self, selector: str) -> list[Image]:
                raise providers.ProviderError("hcloud image list exploded")

        warns: list[str] = []
        self.assertIsNone(self._call(_ListBoom(), warn=warns.append))
        self.assertEqual(len(warns), 1)
        self.assertIn("cache lookup failed", warns[0])


class TestRunCacheConsumption(unittest.TestCase):
    """execute() cache consumption (group 8): hit-restore, G3 cold-fallback, G4 host-failure."""

    def _ubuntu(self) -> distro.DistroProfile:
        return distro.get_profile("ubuntu")

    def _key(self, arch: str = "x86") -> str:
        return imagecache.content_key(self._ubuntu(), arch, "probe", (), _null_deps())

    def _matrix(self) -> runner.Matrix:
        return runner.Matrix(_demo_workload(), ("ubuntu",), "cpx22", "run-cache")

    def _factory(self, ssh_runner: ssh.SshRunner) -> Callable[[str, keypair.Keypair], ssh.SshRunner]:
        return lambda _op, _kp: ssh_runner

    def _exec(
        self,
        prov: providers.Provider,
        ssh_runner: ssh.SshRunner | None = None,
        *,
        resolve_deps: rescue_image.ResolveDeps | None = None,
        reap_bad_cache_image: bool = False,
    ) -> list[model.HostRun]:
        with tempfile.TemporaryDirectory() as d:
            return runner.execute(
                self._matrix(), prov, self._factory(ssh_runner or FakeSshRunner()),
                _fake_keypair(Path(d)), "probe",
                resolve_deps=resolve_deps if resolve_deps is not None else _null_deps(),
                reap_bad_cache_image=reap_bad_cache_image,
            )

    def test_hit_restores_from_snapshot_skips_rescue_workload_runs(self) -> None:
        # a cache hit creates from the snapshot id with the MINIMAL cloud-init and
        # the workload runs; run NEVER builds.
        prov = FakeProvider()
        prov.images["img-cache"] = _cached_run_image(key=self._key())
        fssh = FakeSshRunner()
        runs = self._exec(prov, fssh)
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0].results)  # workload ran
        self.assertEqual(len(prov.created), 1)
        self.assertEqual(prov.created[0].image, "img-cache")  # created from snapshot
        # minimal restore cloud-init (re-authorize the per-run key only)
        self.assertEqual(
            prov.cloud_inits[0],
            cloudinit.render_minimal_cloudinit("probe", "ssh-ed25519 AAAA probe"),
        )
        self.assertEqual(len(prov.destroyed), 1)  # torn down
        self.assertEqual(prov.created_images, [])  # run NEVER builds (D3)

    def test_miss_uses_cold_path(self) -> None:
        # no cached image → cold default image, full cloud-init.
        prov = FakeProvider()
        runs = self._exec(prov)
        self.assertEqual(prov.created[0].image, self._ubuntu().default_image)
        self.assertTrue(runs[0].results)
        self.assertEqual(prov.created_images, [])

    def test_g3_create_from_image_failure_falls_back_to_cold(self) -> None:
        # G3: a hit whose create-from-image fails (image gone) → cold path; the run
        # does NOT fail; the host is provisioned cold.
        class _RestoreCreateBoom(FakeProvider):
            def create_with_cloudinit(self, spec: HostSpec, cloud_init: str) -> Host:
                if spec.image == "img-cache":
                    raise providers.ProviderError("image not found (pruned mid-flight)")
                return super().create_with_cloudinit(spec, cloud_init)

        prov = _RestoreCreateBoom()
        prov.images["img-cache"] = _cached_run_image(key=self._key())
        runs = self._exec(prov)
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0].results)  # ran cold, did NOT fail
        # exactly one host created — the COLD one (the restore create raised first)
        self.assertEqual([s.image for s in prov.created], [self._ubuntu().default_image])
        self.assertEqual(len(prov.destroyed), 1)  # cold host torn down
        self.assertEqual(prov.created_images, [])

    def test_g4_restored_host_readiness_failure_is_host_failure_naming_image(self) -> None:
        # G4: a restored host that fails readiness → host failure naming the source
        # image, NOT cold-retried (only ONE host created — the restore).
        class _ReadinessBoom(FakeSshRunner):
            def wait_until_ready(self, host: Host) -> None:
                raise ssh.SshError("never became reachable")

        prov = FakeProvider()
        prov.images["img-cache"] = _cached_run_image(key=self._key())
        runs = self._exec(prov, _ReadinessBoom())
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].results, ())  # host failure
        self.assertTrue(runs[0].detail.startswith("ERROR:"))
        self.assertIn("img-cache", runs[0].detail)  # names the source image
        # NOT cold-retried: only the restore host was created (no second cold host)
        self.assertEqual([s.image for s in prov.created], ["img-cache"])
        self.assertEqual(len(prov.destroyed), 1)  # restored host torn down

    def test_g4_default_keeps_the_image_hint_only(self) -> None:
        # default (no --reap-bad-cache-image): the image is named but KEPT.
        class _ReadinessBoom(FakeSshRunner):
            def wait_until_ready(self, host: Host) -> None:
                raise ssh.SshError("never became reachable")

        prov = FakeProvider()
        prov.images["img-cache"] = _cached_run_image(key=self._key())
        runs = self._exec(prov, _ReadinessBoom())
        self.assertEqual(prov.deleted_images, [])  # image survives
        self.assertIn("the image was KEPT", runs[0].detail)
        self.assertIn("img-cache", runs[0].detail)

    def test_g4_reap_flag_reaps_the_source_image(self) -> None:
        # --reap-bad-cache-image set: the source image is reaped on readiness failure.
        class _ReadinessBoom(FakeSshRunner):
            def wait_until_ready(self, host: Host) -> None:
                raise ssh.SshError("never became reachable")

        prov = FakeProvider()
        prov.images["img-cache"] = _cached_run_image(key=self._key())
        runs = self._exec(prov, _ReadinessBoom(), reap_bad_cache_image=True)
        self.assertEqual(prov.deleted_images, ["img-cache"])  # reaped
        self.assertIn("reaped the bad cache image img-cache", runs[0].detail)

    def test_g4_reap_failure_is_noted_not_raised(self) -> None:
        # the reap of the bad image itself failing is a NOTE in the detail, never a
        # raise (still a recorded host failure).
        class _ReadinessBoom(FakeSshRunner):
            def wait_until_ready(self, host: Host) -> None:
                raise ssh.SshError("unreachable")

        class _DeleteBoom(FakeProvider):
            def delete_image(self, image_id: str) -> None:
                raise providers.ProviderError("image delete exploded")

        prov = _DeleteBoom()
        prov.images["img-cache"] = _cached_run_image(key=self._key())
        runs = self._exec(prov, _ReadinessBoom(), reap_bad_cache_image=True)
        self.assertEqual(runs[0].results, ())
        self.assertIn("reap of the bad cache image img-cache FAILED", runs[0].detail)

    def test_cutoff_create_fail_cold_vs_readiness_fail_host_failure(self) -> None:
        # the load-bearing distinction: a create-from-image fail → cold success; a
        # readiness fail on the restored host → host failure (no cold retry).
        class _RestoreCreateBoom(FakeProvider):
            def create_with_cloudinit(self, spec: HostSpec, cloud_init: str) -> Host:
                if spec.image == "img-cache":
                    raise providers.ProviderError("image gone")
                return super().create_with_cloudinit(spec, cloud_init)

        class _ReadinessBoom(FakeSshRunner):
            def wait_until_ready(self, host: Host) -> None:
                raise ssh.SshError("unreachable")

        prov_a = _RestoreCreateBoom()
        prov_a.images["img-cache"] = _cached_run_image(key=self._key())
        runs_a = self._exec(prov_a)
        # create-fail → cold success (workload ran, one cold host)
        self.assertTrue(runs_a[0].results)
        self.assertEqual([s.image for s in prov_a.created], [self._ubuntu().default_image])

        prov_b = FakeProvider()
        prov_b.images["img-cache"] = _cached_run_image(key=self._key())
        runs_b = self._exec(prov_b, _ReadinessBoom())
        # readiness-fail → host failure (no cold retry, names the image)
        self.assertEqual(runs_b[0].results, ())
        self.assertEqual([s.image for s in prov_b.created], ["img-cache"])

    def test_server_type_disk_failure_is_graceful_miss_cold(self) -> None:
        # G9: a server_type_disk failure → warn + cache miss → cold path (the host
        # is provisioned cold, never failed).
        class _DiskBoom(FakeProvider):
            def server_type_disk(self, server_type: str) -> float:
                raise providers.ProviderError("server-type describe exploded")

        prov = _DiskBoom()
        prov.images["img-cache"] = _cached_run_image(key=self._key())
        runs = self._exec(prov)
        self.assertTrue(runs[0].results)  # ran cold
        self.assertEqual(prov.created[0].image, self._ubuntu().default_image)
        self.assertEqual(prov.created_images, [])

    def test_resolve_deps_none_is_pure_cold_no_disk_call(self) -> None:
        # resolve_deps=None → pure cold path: no lookup, no server_type_disk call.
        class _NoDiskAllowed(FakeProvider):
            def server_type_disk(self, server_type: str) -> float:
                raise AssertionError("cold path must not call server_type_disk")

            def list_images(self, selector: str) -> list[Image]:
                raise AssertionError("cold path must not call list_images")

        prov = _NoDiskAllowed()
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(
                self._matrix(), prov, self._factory(FakeSshRunner()),
                _fake_keypair(Path(d)), "probe", resolve_deps=None,
            )
        self.assertTrue(runs[0].results)
        self.assertEqual(prov.created[0].image, self._ubuntu().default_image)

    def test_run_makes_zero_create_image_calls_on_hit_and_miss(self) -> None:
        # D3: run NEVER builds — zero create_image calls on both a hit and a miss.
        hit = FakeProvider()
        hit.images["img-cache"] = _cached_run_image(key=self._key())
        self._exec(hit)
        self.assertEqual(hit.created_images, [])

        miss = FakeProvider()
        self._exec(miss)
        self.assertEqual(miss.created_images, [])


class TestPlanZeroProviderCalls(unittest.TestCase):
    """plan() makes ZERO provider calls even with caching available (D3/8.3)."""

    def test_plan_makes_zero_provider_calls(self) -> None:
        class _NoCallProvider(FakeProvider):
            def list_images(self, selector: str) -> list[Image]:
                raise AssertionError("plan must not call list_images")

            def server_type_disk(self, server_type: str) -> float:
                raise AssertionError("plan must not call server_type_disk")

            def create_with_cloudinit(self, spec: HostSpec, cloud_init: str) -> Host:
                raise AssertionError("plan must not create")

        prov = _NoCallProvider()
        prov.images["img-cache"] = _cached_run_image(
            key=imagecache.content_key(distro.get_profile("ubuntu"), "x86", "probe", (), _null_deps())
        )
        # plan takes no provider at all — it is call-free by construction. Asserting
        # the matrix plans without touching the provider object is the guarantee.
        items = runner.plan(runner.Matrix(_demo_workload(), ("ubuntu",), "cpx22", "run-plan"))
        self.assertEqual(len(items), 1)
        self.assertEqual(prov.created, [])
        self.assertEqual(prov.created_images, [])


class TestImageCacheLabels(unittest.TestCase):
    def test_full_label_set(self) -> None:
        ubuntu = distro.get_profile("ubuntu")
        labels = imagecache.cache_labels(
            ubuntu, "x86", key="v1-ubuntu-abc", source_fp="ubuntu-24.04", run_token="run-xyz",
            requires=(),
        )
        self.assertEqual(labels, {
            "vmlease-purpose": "image-cache",
            "vmlease-cache-key": "v1-ubuntu-abc",
            "vmlease-schema": "v1",
            "vmlease-distro": "ubuntu",
            "vmlease-arch": "x86",
            "vmlease-source-fp": "ubuntu-24.04",
            "vmlease-built": "run-xyz",
            "vmlease-requires-hash": imagecache.requires_hash(()),
            "vmlease-requires": "",
        })

    def test_no_per_run_reap_label(self) -> None:
        # the data-loss guard: a persistent cache image must NOT carry vmlease=<run-id>.
        ubuntu = distro.get_profile("ubuntu")
        labels = imagecache.cache_labels(
            ubuntu, "x86", key="k", source_fp="fp", run_token="run-1", requires=(),
        )
        self.assertNotIn("vmlease", labels)

    def test_long_value_truncated_to_63(self) -> None:
        # a 64-hex sha source-fp would overflow the provider's ≤63-char limit.
        ubuntu = distro.get_profile("ubuntu")
        sha = "f" * 64
        labels = imagecache.cache_labels(
            ubuntu, "x86", key="k", source_fp=sha, run_token="run-1", requires=(),
        )
        self.assertEqual(len(labels["vmlease-source-fp"]), 63)
        self.assertEqual(labels["vmlease-source-fp"], "f" * 63)


class TestImageCacheSupersession(unittest.TestCase):
    def test_superseded_subset(self) -> None:
        current = _cache_image(distro_key="ubuntu", arch="x86", key="v1-ubuntu-CUR", img_id="cur")
        old = _cache_image(distro_key="ubuntu", arch="x86", key="v1-ubuntu-OLD", img_id="old")
        result = imagecache.superseded([current, old], "v1-ubuntu-CUR")
        self.assertEqual([img.id for img in result], ["old"])

    def test_accept_a_no_current_image_all_superseded(self) -> None:
        # accept-(a): current image absent ⇒ every image in the group is superseded.
        a = _cache_image(distro_key="ubuntu", arch="x86", key="v1-ubuntu-OLD1", img_id="a")
        b = _cache_image(distro_key="ubuntu", arch="x86", key="v1-ubuntu-OLD2", img_id="b")
        result = imagecache.superseded([a, b], "v1-ubuntu-CUR")
        self.assertEqual({img.id for img in result}, {"a", "b"})

    def test_resolve_current_keys_native(self) -> None:
        img = _cache_image(distro_key="ubuntu", arch="x86", key="v1-ubuntu-anything")
        warnings: list[str] = []
        keys = imagecache.resolve_current_keys(
            [img], distro.get_profile, "probe", _null_deps(), warnings.append,
        )
        expected = imagecache.content_key(distro.get_profile("ubuntu"), "x86", "probe", (), _null_deps())
        self.assertEqual(keys, {("ubuntu", "x86", imagecache.requires_hash(())): expected})
        self.assertEqual(warnings, [])

    def test_resolve_current_keys_fail_safe_keeps_group(self) -> None:
        # a raising resolve (mirror down) ⇒ group skipped + warned, never deleted.
        from vmlease.archbuild import ArchBuildError
        raising_spec = _FakeResolveSpec(raises=ArchBuildError("mirror down"))

        def profile_for(key: str) -> distro.DistroProfile:
            if key == "arch":
                return _rescue_profile(raising_spec)
            return distro.get_profile(key)

        arch_img = _cache_image(distro_key="arch", arch="x86", key="v1-arch-OLD", img_id="arch1")
        ubuntu_img = _cache_image(distro_key="ubuntu", arch="x86", key="v1-ubuntu-X", img_id="ub1")
        warnings: list[str] = []
        keys = imagecache.resolve_current_keys(
            [arch_img, ubuntu_img], profile_for, "probe", _null_deps(), warnings.append,
        )
        # ubuntu resolved; arch skipped (kept) with a warning.
        self.assertIn(("ubuntu", "x86", imagecache.requires_hash(())), keys)
        self.assertNotIn(("arch", "x86", imagecache.requires_hash(())), keys)
        self.assertEqual(len(warnings), 1)
        self.assertIn("arch", warnings[0])

    def test_resolve_current_keys_dedups_group(self) -> None:
        # two images of the same group ⇒ the key is resolved once.
        a = _cache_image(distro_key="ubuntu", arch="x86", key="v1-ubuntu-A", img_id="a")
        b = _cache_image(distro_key="ubuntu", arch="x86", key="v1-ubuntu-B", img_id="b")
        keys = imagecache.resolve_current_keys(
            [a, b], distro.get_profile, "probe", _null_deps(), lambda _m: None,
        )
        self.assertEqual(len(keys), 1)


class TestWaitUntilOff(unittest.TestCase):
    def test_returns_when_off(self) -> None:
        prov = FakeProvider()
        prov.power_off("9")  # flips state to "off"
        slept: list[float] = []
        runner._wait_until_off(prov, "9", attempts=5, sleep=slept.append)
        self.assertEqual(slept, [])  # off on the first poll → no sleep

    def test_polls_then_returns_when_it_turns_off(self) -> None:
        # status "running" for the first two polls, then "off".
        class _Slow(FakeProvider):
            def __init__(self) -> None:
                super().__init__()
                self._polls = 0

            def server_status(self, server_id: str) -> str:
                self._polls += 1
                return "off" if self._polls >= 3 else "running"

        slept: list[float] = []
        runner._wait_until_off(_Slow(), "9", attempts=5, sleep=slept.append)
        self.assertEqual(len(slept), 2)  # slept between the first two running polls

    def test_timeout_raises(self) -> None:
        # never off → exhausts the attempt budget → raises (G2), no real clock.
        prov = FakeProvider()  # default status "running"
        slept: list[float] = []
        with self.assertRaises(runner.PoweroffTimeoutError):
            runner._wait_until_off(prov, "9", attempts=3, sleep=slept.append)
        self.assertEqual(len(slept), 2)  # slept between polls but not after the last


class TestBuildOneImage(unittest.TestCase):
    def _spec(self, distro_key: str = "ubuntu") -> HostSpec:
        return HostSpec(
            name=f"vmlease-build-{distro_key}",
            image=distro.get_profile(distro_key).default_image,
            server_type="cpx22",
            distro_key=distro_key,
            labels={"vmlease": "build-run"},
        )

    def _build(
        self,
        prov: providers.Provider,
        fssh: ssh.SshRunner,
        *,
        description: str = "v1-ubuntu-key",
        labels: dict[str, str] | None = None,
        distro_key: str = "ubuntu",
    ) -> tuple[Image, list[str]]:
        labels = labels if labels is not None else {"vmlease-cache-key": "v1-ubuntu-key"}
        note_sink: list[str] = []
        on_ready = runner.make_snapshot_on_ready(
            description, labels, sleep=lambda _s: None, poweroff_attempts=5
        )
        with tempfile.TemporaryDirectory() as d:
            image = runner.build_one_image(
                self._spec(distro_key),
                distro.get_profile(distro_key),
                prov,
                lambda _o, _k: fssh,
                _fake_keypair(Path(d)),
                "probe",
                None,
                on_ready=on_ready,
                note_sink=note_sink,
            )
        return image, note_sink

    def test_happy_build_returns_labelled_image_and_tears_down(self) -> None:
        prov = FakeProvider()
        fssh = FakeSshRunner()
        image, note_sink = self._build(prov, fssh)
        # returns an Image carrying the supplied labels (atomic create)
        self.assertEqual(image.labels.get("vmlease-cache-key"), "v1-ubuntu-key")
        self.assertEqual(len(prov.created_images), 1)
        # the builder went off, then was torn down
        self.assertEqual(prov.powered_off, ["id-vmlease-build-ubuntu"])
        self.assertEqual(prov.server_status("id-vmlease-build-ubuntu"), "off")
        self.assertEqual([h.name for h in prov.destroyed], ["vmlease-build-ubuntu"])
        self.assertEqual(note_sink, [])  # clean teardown → no note
        # sysprep ran first over SSH
        self.assertEqual(fssh.ran[0], "_sysprep")

    def test_sysprep_failure_aborts_before_snapshot_and_tears_down(self) -> None:
        # G1: a non-zero sysprep exit raises, NO create_image, builder destroyed.
        prov = FakeProvider()
        fssh = FakeSshRunner(fail_on="_sysprep")
        with self.assertRaises(RuntimeError):
            self._build(prov, fssh)
        self.assertEqual(prov.created_images, [])  # never snapshotted
        self.assertEqual(prov.powered_off, [])  # never even powered off
        self.assertEqual([h.name for h in prov.destroyed], ["vmlease-build-ubuntu"])  # torn down

    def test_poweroff_never_off_aborts_before_snapshot_and_tears_down(self) -> None:
        # G2: the host never reaches off → wait-for-off raises, no create_image.
        class _NeverOff(FakeProvider):
            def power_off(self, server_id: str) -> None:
                self.powered_off.append(server_id)  # records, but stays "running"

        prov = _NeverOff()
        fssh = FakeSshRunner()
        with self.assertRaises(runner.PoweroffTimeoutError):
            self._build(prov, fssh)
        self.assertEqual(prov.created_images, [])  # never snapshotted
        self.assertEqual(len(prov.powered_off), 1)  # poweroff was attempted
        self.assertEqual([h.name for h in prov.destroyed], ["vmlease-build-ubuntu"])  # torn down

    def test_teardown_failure_surfaces_note_for_next_milestone(self) -> None:
        # a failing builder teardown is surfaced via note_sink (not swallowed,
        # not raised) so _cmd_build_image can route it to a non-zero exit.
        class _DestroyFails(FakeProvider):
            def destroy(self, host: Host) -> None:
                super().destroy(host)
                raise providers.ProviderError("request timeout")

        prov = _DestroyFails()
        image, note_sink = self._build(prov, FakeSshRunner())
        self.assertEqual(image.labels.get("vmlease-cache-key"), "v1-ubuntu-key")  # image kept
        self.assertEqual(len(note_sink), 1)
        self.assertIn(runner.TEARDOWN_WARNING_PREFIX, note_sink[0])


class TestServerTypeArch(unittest.TestCase):
    def test_x86_for_cpx(self) -> None:
        self.assertEqual(safety.server_type_arch("cpx22"), "x86")

    def test_arm_for_cax(self) -> None:
        self.assertEqual(safety.server_type_arch("cax11"), "arm")

    def test_x86_for_other_families(self) -> None:
        self.assertEqual(safety.server_type_arch("cx23"), "x86")
        self.assertEqual(safety.server_type_arch("ccx13"), "x86")


class TestBuildLiveResolveDeps(unittest.TestCase):
    def test_returns_resolve_deps_with_keyring_and_wired_seams(self) -> None:
        observed: dict[str, object] = {}

        def fake_run(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
            observed["run"] = argv
            return 0, "ok", ""

        def fake_text(url: str) -> str:
            observed["text"] = url
            return "T"

        def fake_bytes(url: str) -> bytes:
            observed["bytes"] = url
            return b"B"

        deps = archbuild.build_live_resolve_deps(
            "/keyring/arch-boxes.gpg", run=fake_run, fetch_text=fake_text, fetch_bytes=fake_bytes,
        )
        self.assertIsInstance(deps, rescue_image.ResolveDeps)
        self.assertEqual(deps.keyring_path, "/keyring/arch-boxes.gpg")
        # the gpg_runner wraps `run` into a CompletedProcess, observing the seam.
        cp = deps.gpg_runner(["gpg", "--verify"])
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(observed["run"], ["gpg", "--verify"])
        # the fetchers are wired straight through.
        self.assertEqual(deps.text_fetcher("u1"), "T")
        self.assertEqual(deps.fetcher("u2"), b"B")
        self.assertEqual(observed["text"], "u1")
        self.assertEqual(observed["bytes"], "u2")


class TestContentKeyFromBaseFp(unittest.TestCase):
    def test_matches_content_key(self) -> None:
        # the split derivation hashes the SAME bytes as content_key.
        ubuntu = distro.get_profile("ubuntu")
        base_fp = imagecache.base_fingerprint(ubuntu, "x86", _null_deps())
        derived = imagecache.content_key_from_base_fp(base_fp, ubuntu, "x86", "probe", ())
        whole = imagecache.content_key(ubuntu, "x86", "probe", (), _null_deps())
        self.assertEqual(derived, whole)


class TestCliBuildImage(unittest.TestCase):
    """``vmlease build-image`` lifecycle (tasks 6.2-6.6), fully mocked (no network)."""

    def _key(self, distro_key: str = "ubuntu", arch: str = "x86", operator: str = "probe") -> str:
        prof = distro.get_profile(distro_key)
        base_fp = imagecache.base_fingerprint(prof, arch, _null_deps())
        return imagecache.content_key_from_base_fp(base_fp, prof, arch, operator, ())

    def _cache_img(
        self, *, key: str, img_id: str, distro_key: str = "ubuntu", arch: str = "x86",
        created: str = "2024-01-01T00:00:00+00:00", requires: tuple[str, ...] = (),
    ) -> Image:
        return Image(
            id=img_id, created=created, disk_size=40.0, arch=arch,
            labels={
                imagecache.LABEL_PURPOSE: imagecache.PURPOSE_IMAGE_CACHE,
                imagecache.LABEL_DISTRO: distro_key,
                imagecache.LABEL_ARCH: arch,
                imagecache.LABEL_CACHE_KEY: key,
                imagecache.LABEL_REQUIRES_HASH: imagecache.requires_hash(requires),
                imagecache.LABEL_REQUIRES: "\0".join(capabilities.canonical_requires(requires)),
            },
        )

    def _run(
        self, prov: FakeProvider, argv: list[str], *, tmp: str,
        reader: Callable[[str], str] = lambda _p: "y",
    ) -> tuple[int, str, str]:
        from unittest import mock

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "HetznerProvider", lambda: prov), \
             mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(tmp))), \
             mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: FakeSshRunner()), \
             redirect_stdout(out), redirect_stderr(err):
            ns = cli.build_parser().parse_args(argv)
            rc = cli._cmd_build_image(ns, reader=reader)
        return rc, out.getvalue(), err.getvalue()

    # --- 6.2 ---------------------------------------------------------------- #
    def test_build_provisions_builder_with_run_label(self) -> None:
        prov = FakeProvider()
        with tempfile.TemporaryDirectory() as d:
            rc, _o, _e = self._run(
                prov, ["build-image", "--distro", "ubuntu", "--run-token", "bird", "--yes"], tmp=d,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(len(prov.created), 1)
        spec = prov.created[0]
        self.assertEqual(spec.labels.get("vmlease"), "bird")  # builder carries the reap label
        self.assertEqual(spec.name, "vmlease-bird-build-ubuntu")
        # the image was created and carries the cache key (NOT vmlease=<run-id>).
        self.assertEqual(len(prov.created_images), 1)
        img_labels = prov.created_images[0][2]
        self.assertEqual(img_labels.get(imagecache.LABEL_CACHE_KEY), self._key())
        self.assertNotIn("vmlease", img_labels)

    def test_unknown_distro_exits_2(self) -> None:
        prov = FakeProvider()
        with tempfile.TemporaryDirectory() as d:
            rc, _o, err = self._run(
                prov, ["build-image", "--distro", "nope", "--run-token", "bird", "--yes"], tmp=d,
            )
        self.assertEqual(rc, 2)
        self.assertEqual(prov.created, [])
        self.assertIn("error:", err)

    def test_non_allowlisted_server_type_exits_2(self) -> None:
        prov = FakeProvider()
        with tempfile.TemporaryDirectory() as d:
            rc, _o, _e = self._run(
                prov,
                ["build-image", "--distro", "ubuntu", "--server-type", "ccx33", "--run-token", "bird", "--yes"],
                tmp=d,
            )
        self.assertEqual(rc, 2)
        self.assertEqual(prov.created, [])

    def test_rescue_write_without_ssh_key_exits_2_no_provision(self) -> None:
        # D11: the rescue-key gate fires BEFORE any keypair/provisioning.
        prov = FakeProvider()
        with tempfile.TemporaryDirectory() as d:
            rc, _o, err = self._run(
                prov, ["build-image", "--distro", "arch", "--run-token", "bird", "--yes"], tmp=d,
            )
        self.assertEqual(rc, 2)
        self.assertEqual(prov.created, [])  # zero hosts
        self.assertEqual(prov.created_images, [])
        self.assertIn("--ssh-key", err)

    # --- 6.3 ---------------------------------------------------------------- #
    def test_already_cached_is_noop_exit_0_zero_hosts(self) -> None:
        prov = FakeProvider()
        prov.images["img-cur"] = self._cache_img(key=self._key(), img_id="img-cur")
        with tempfile.TemporaryDirectory() as d:
            rc, out, _e = self._run(
                prov, ["build-image", "--distro", "ubuntu", "--run-token", "bird", "--yes"], tmp=d,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(prov.created, [])  # no builder
        self.assertEqual(prov.created_images, [])  # no new image
        self.assertIn("already cached", out)

    def test_at_cap_s_empty_refuses_exit_1_no_builder(self) -> None:
        # max-images=1, one cached image of a DIFFERENT group/key (not superseded
        # of THIS group, since this group is absent ⇒ S of this group is empty).
        prov = FakeProvider()
        prov.images["img-other"] = self._cache_img(
            key="v1-fedora-X", img_id="img-other", distro_key="fedora",
        )
        with tempfile.TemporaryDirectory() as d:
            rc, _o, err = self._run(
                prov,
                ["build-image", "--distro", "ubuntu", "--run-token", "bird", "--max-images", "1", "--yes"],
                tmp=d,
            )
        self.assertEqual(rc, 1)
        self.assertEqual(prov.created, [])  # no builder
        self.assertIn("image quota", err)

    # --- 6.4 ---------------------------------------------------------------- #
    def test_not_at_cap_builds_then_prunes_s(self) -> None:
        prov = FakeProvider()
        prov.images["img-old"] = self._cache_img(key=self._key()[:-1] + "Z", img_id="img-old")
        with tempfile.TemporaryDirectory() as d:
            rc, _o, _e = self._run(
                prov, ["build-image", "--distro", "ubuntu", "--run-token", "bird", "--yes"], tmp=d,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(len(prov.created_images), 1)  # built
        self.assertIn("img-old", prov.deleted_images)  # then pruned S

    def test_build_docker_variant_does_not_prune_docker_less(self) -> None:
        # 4.4b build-image prune data-loss guard: building the `--requires docker`
        # variant prunes only the same-(distro, arch, requires-hash) group's stale
        # images — the live docker-less cache image is a DIFFERENT group and must
        # survive the prune.
        prov = FakeProvider()
        prov.images["dl-cur"] = self._cache_img(
            key="v1-ubuntu-DLCUR", img_id="dl-cur", requires=(),
        )
        with tempfile.TemporaryDirectory() as d:
            rc, _o, _e = self._run(
                prov,
                ["build-image", "--distro", "ubuntu", "--requires", "docker", "--run-token", "bird", "--yes"],
                tmp=d,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(len(prov.created_images), 1)  # docker image built
        # the docker image carries the docker requires-hash...
        built_labels = prov.created_images[0][2]
        self.assertEqual(
            built_labels.get(imagecache.LABEL_REQUIRES_HASH), imagecache.requires_hash(("docker",)),
        )
        # ...and the docker-less variant was NOT pruned (distinct group).
        self.assertNotIn("dl-cur", prov.deleted_images)

    def test_at_cap_s_nonempty_prunes_then_builds(self) -> None:
        prov = FakeProvider()
        prov.images["img-old"] = self._cache_img(key=self._key()[:-1] + "Z", img_id="img-old")
        order: list[str] = []
        real_delete = prov.delete_image
        real_create = prov.create_with_cloudinit

        def track_delete(image_id: str) -> None:
            order.append(f"delete:{image_id}")
            real_delete(image_id)

        def track_create(spec: HostSpec, cloud_init: str) -> Host:
            order.append("create")
            return real_create(spec, cloud_init)

        prov.delete_image = track_delete  # type: ignore[method-assign]
        prov.create_with_cloudinit = track_create  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as d:
            rc, _o, _e = self._run(
                prov,
                ["build-image", "--distro", "ubuntu", "--run-token", "bird", "--max-images", "1", "--yes"],
                tmp=d,
            )
        self.assertEqual(rc, 0)
        # prune-then-build: the S delete happened before the builder create.
        self.assertEqual(order, ["delete:img-old", "create"])

    def test_post_build_prune_failure_still_succeeds(self) -> None:
        # G7: a not-at-cap post-build prune failure warns, build still succeeds.
        class _PruneFails(FakeProvider):
            def delete_image(self, image_id: str) -> None:
                raise providers.ProviderError("delete exploded")

        prov = _PruneFails()
        prov.images["img-old"] = self._cache_img(key=self._key()[:-1] + "Z", img_id="img-old")
        with tempfile.TemporaryDirectory() as d:
            rc, _o, err = self._run(
                prov, ["build-image", "--distro", "ubuntu", "--run-token", "bird", "--yes"], tmp=d,
            )
        self.assertEqual(rc, 0)  # build NOT failed
        self.assertEqual(len(prov.created_images), 1)
        self.assertIn("warning", err)

    def test_at_cap_pre_build_prune_failure_aborts_before_provisioning(self) -> None:
        class _PruneFails(FakeProvider):
            def delete_image(self, image_id: str) -> None:
                raise providers.ProviderError("delete exploded")

        prov = _PruneFails()
        prov.images["img-old"] = self._cache_img(key=self._key()[:-1] + "Z", img_id="img-old")
        with tempfile.TemporaryDirectory() as d:
            rc, _o, err = self._run(
                prov,
                ["build-image", "--distro", "ubuntu", "--run-token", "bird", "--max-images", "1", "--yes"],
                tmp=d,
            )
        self.assertEqual(rc, 1)
        self.assertEqual(prov.created, [])  # no builder
        self.assertIn("pre-build prune", err)

    # --- 6.5 ---------------------------------------------------------------- #
    def test_rebuild_drops_only_older_same_key_image(self) -> None:
        # an EXISTING same-key image, older than the freshly-built one (which the
        # FakeProvider stamps "2024-01-01..."), is dropped; a same-key image with a
        # LATER created stamp would survive. Use an older existing image.
        prov = FakeProvider()
        prov.images["img-prev"] = self._cache_img(
            key=self._key(), img_id="img-prev", created="2023-01-01T00:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as d:
            rc, _o, _e = self._run(
                prov, ["build-image", "--distro", "ubuntu", "--run-token", "bird", "--rebuild", "--yes"], tmp=d,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(len(prov.created_images), 1)  # rebuilt
        self.assertIn("img-prev", prov.deleted_images)  # older same-key dropped
        # the just-built image (newest) survives.
        self.assertEqual(len(prov.images), 1)
        self.assertNotIn("img-prev", prov.images)

    def test_rebuild_keeps_newer_same_key_image(self) -> None:
        # a same-key image created AFTER the just-built one must NOT be dropped
        # (never "all other same-key").
        prov = FakeProvider()
        prov.images["img-newer"] = self._cache_img(
            key=self._key(), img_id="img-newer", created="2999-01-01T00:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as d:
            rc, _o, _e = self._run(
                prov, ["build-image", "--distro", "ubuntu", "--run-token", "bird", "--rebuild", "--yes"], tmp=d,
            )
        self.assertEqual(rc, 0)
        self.assertNotIn("img-newer", prov.deleted_images)  # newer survives
        self.assertIn("img-newer", prov.images)

    # --- 6.6 ---------------------------------------------------------------- #
    def test_provider_quota_error_exits_1_with_reap_hint(self) -> None:
        class _QuotaFails(FakeProvider):
            def create_image(self, server_id: str, description: str, labels: dict[str, str]) -> Image:
                raise providers.ProviderQuotaError("snapshot limit (resource_limit_exceeded)")

        prov = _QuotaFails()
        with tempfile.TemporaryDirectory() as d:
            rc, _o, err = self._run(
                prov, ["build-image", "--distro", "ubuntu", "--run-token", "bird", "--yes"], tmp=d,
            )
        self.assertEqual(rc, 1)
        self.assertIn("reap-images", err)
        # the builder was torn down by the scaffold's finally.
        self.assertEqual([h.name for h in prov.destroyed], ["vmlease-bird-build-ubuntu"])

    def test_builder_teardown_failure_exits_nonzero_keeps_image(self) -> None:
        # G8: a builder-teardown failure → reap attempted, image KEPT, non-zero exit.
        class _DestroyFails(FakeProvider):
            def destroy(self, host: Host) -> None:
                super().destroy(host)
                raise providers.ProviderError("request timeout")

        prov = _DestroyFails()
        with tempfile.TemporaryDirectory() as d:
            rc, _o, err = self._run(
                prov, ["build-image", "--distro", "ubuntu", "--run-token", "bird", "--yes"], tmp=d,
            )
        self.assertEqual(rc, 1)
        self.assertEqual(len(prov.created_images), 1)  # image kept (reap is server-only)
        self.assertIn("teardown failed", err)
        self.assertIn("image kept", err)

    def test_ctrl_c_during_build_backstop_reaps_and_reraises(self) -> None:
        # KeyboardInterrupt mid-build → reap the builder by label (image kept),
        # then re-raise so the interrupt still exits.
        from unittest import mock

        class _Interrupting(FakeSshRunner):
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                raise KeyboardInterrupt

        prov = FakeProvider()
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(cli, "HetznerProvider", lambda: prov), \
             mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
             mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: _Interrupting()), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            ns = cli.build_parser().parse_args(
                ["build-image", "--distro", "ubuntu", "--run-token", "bird", "--yes"]
            )
            with self.assertRaises(KeyboardInterrupt):
                cli._cmd_build_image(ns, reader=lambda _p: "y")
        # the builder was reaped by run-label (list_labeled then destroy).
        self.assertEqual([h.name for h in prov.destroyed], ["vmlease-bird-build-ubuntu"])

    def test_generic_provider_error_during_build_exits_1(self) -> None:
        # a sysprep failure raises RuntimeError inside the build → exit 1, no traceback.
        from unittest import mock

        prov = FakeProvider()
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(cli, "HetznerProvider", lambda: prov), \
             mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
             mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: FakeSshRunner(fail_on="_sysprep")), \
             redirect_stdout(out), redirect_stderr(err):
            ns = cli.build_parser().parse_args(
                ["build-image", "--distro", "ubuntu", "--run-token", "bird", "--yes"]
            )
            rc = cli._cmd_build_image(ns, reader=lambda _p: "y")
        self.assertEqual(rc, 1)
        self.assertEqual(prov.created_images, [])  # never snapshotted
        self.assertIn("error:", err.getvalue())

    def test_confirm_no_aborts_before_provisioning(self) -> None:
        prov = FakeProvider()
        with tempfile.TemporaryDirectory() as d:
            rc, out, _e = self._run(
                prov, ["build-image", "--distro", "ubuntu", "--run-token", "bird"], tmp=d, reader=lambda _p: "n",
            )
        self.assertEqual(rc, 0)
        self.assertEqual(prov.created, [])
        self.assertIn("aborted", out)


class TestCliReapImages(unittest.TestCase):
    """``vmlease reap-images`` (tasks 7.1), fully mocked (no network)."""

    def _img(
        self, *, img_id: str, distro_key: str = "ubuntu", arch: str = "x86",
        key: str = "v1-ubuntu-CUR", created: str = "2024-04-25T13:26:27+00:00",
        requires: tuple[str, ...] = (),
    ) -> Image:
        return Image(
            id=img_id, created=created, disk_size=40.0, arch=arch,
            labels={
                imagecache.LABEL_PURPOSE: imagecache.PURPOSE_IMAGE_CACHE,
                imagecache.LABEL_DISTRO: distro_key,
                imagecache.LABEL_ARCH: arch,
                imagecache.LABEL_CACHE_KEY: key,
                imagecache.LABEL_REQUIRES_HASH: imagecache.requires_hash(requires),
                imagecache.LABEL_REQUIRES: "\0".join(capabilities.canonical_requires(requires)),
            },
        )

    def _run(
        self, prov: FakeProvider, argv: list[str], *,
        resolve_current_keys: Callable[..., dict[tuple[str, str, str], str]] | None = None,
    ) -> tuple[int, str, str]:
        from contextlib import ExitStack
        from unittest import mock

        out, err = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "HetznerProvider", lambda: prov))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            if resolve_current_keys is not None:
                stack.enter_context(mock.patch.object(cli, "resolve_current_keys", resolve_current_keys))
            ns = cli.build_parser().parse_args(argv)
            rc = cli._cmd_reap_images(ns)
        return rc, out.getvalue(), err.getvalue()

    # --- bare call refuses --------------------------------------------------- #
    def test_bare_no_filter_refuses_exit_2_no_provider_call(self) -> None:
        prov = FakeProvider()
        rc, _o, err = self._run(prov, ["reap-images"])
        self.assertEqual(rc, 2)
        self.assertEqual(prov.deleted_images, [])
        self.assertIn("at least one of", err)

    # --- --distro scope ------------------------------------------------------ #
    def test_distro_scope_reaps_only_that_distro(self) -> None:
        prov = FakeProvider()
        prov.images["u1"] = self._img(img_id="u1", distro_key="ubuntu")
        prov.images["f1"] = self._img(img_id="f1", distro_key="fedora", key="v1-fedora-X")
        rc, _o, _e = self._run(prov, ["reap-images", "--distro", "ubuntu"])
        self.assertEqual(rc, 0)
        self.assertEqual(prov.deleted_images, ["u1"])  # fedora untouched

    # --- --older-than -------------------------------------------------------- #
    def test_older_than_selects_only_older(self) -> None:
        prov = FakeProvider()
        prov.images["old"] = self._img(img_id="old", created="2023-01-01T00:00:00+00:00")
        prov.images["new"] = self._img(img_id="new", created="2025-12-31T00:00:00+00:00")
        rc, _o, _e = self._run(prov, ["reap-images", "--older-than", "2024-06-01T00:00:00+00:00"])
        self.assertEqual(rc, 0)
        self.assertEqual(prov.deleted_images, ["old"])  # the newer one survives

    def test_older_than_keeps_image_with_blank_or_unparseable_created(self) -> None:
        # a cache image with a blank/garbled `created` fails the age predicate (kept
        # — never reaped on an age check we cannot verify).
        prov = FakeProvider()
        prov.images["blank"] = self._img(img_id="blank", created="")
        prov.images["junk"] = self._img(img_id="junk", created="not-a-timestamp")
        prov.images["old"] = self._img(img_id="old", created="2023-01-01T00:00:00+00:00")
        rc, _o, _e = self._run(prov, ["reap-images", "--older-than", "2024-06-01T00:00:00+00:00"])
        self.assertEqual(rc, 0)
        self.assertEqual(prov.deleted_images, ["old"])  # only the parseable-older one

    def test_malformed_older_than_exits_2_no_provider_call(self) -> None:
        prov = FakeProvider()
        prov.images["u1"] = self._img(img_id="u1")
        # a list_images call would record nothing observable here; assert no delete
        # AND that the validation happens before any provider call by tracking it.
        listed: list[str] = []
        real_list = prov.list_images

        def track_list(selector: str) -> list[Image]:
            listed.append(selector)
            return real_list(selector)

        prov.list_images = track_list  # type: ignore[method-assign]
        rc, _o, err = self._run(prov, ["reap-images", "--older-than", "not-a-date"])
        self.assertEqual(rc, 2)
        self.assertEqual(listed, [])  # validated fail-closed BEFORE any provider call
        self.assertEqual(prov.deleted_images, [])
        self.assertIn("--older-than", err)

    # --- --dry-run ----------------------------------------------------------- #
    def test_dry_run_deletes_nothing(self) -> None:
        prov = FakeProvider()
        prov.images["u1"] = self._img(img_id="u1")
        rc, out, _e = self._run(prov, ["reap-images", "--distro", "ubuntu", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(prov.deleted_images, [])  # ZERO deletes
        self.assertIn("DRY-RUN", out)
        self.assertIn("u1", out)

    # --- --superseded -------------------------------------------------------- #
    def test_superseded_reaps_off_key_keeps_current(self) -> None:
        prov = FakeProvider()
        prov.images["cur"] = self._img(img_id="cur", key="v1-ubuntu-CUR")
        prov.images["old"] = self._img(img_id="old", key="v1-ubuntu-OLD")

        def resolve(images, profile_for, operator, deps, warn):  # type: ignore[no-untyped-def]
            return {("ubuntu", "x86", imagecache.requires_hash(())): "v1-ubuntu-CUR"}

        rc, _o, _e = self._run(prov, ["reap-images", "--superseded"], resolve_current_keys=resolve)
        self.assertEqual(rc, 0)
        self.assertEqual(prov.deleted_images, ["old"])  # current key kept

    def test_superseded_fail_safe_keeps_unresolvable_group_and_warns(self) -> None:
        prov = FakeProvider()
        prov.images["a1"] = self._img(img_id="a1", distro_key="arch", key="v1-arch-OLD")
        prov.images["u1"] = self._img(img_id="u1", distro_key="ubuntu", key="v1-ubuntu-OLD")

        def resolve(images, profile_for, operator, deps, warn):  # type: ignore[no-untyped-def]
            # arch unresolvable (omitted + warned); ubuntu resolves to a new key.
            warn("cannot resolve current cache key for group (distro='arch', arch='x86'): mirror down")
            return {("ubuntu", "x86", imagecache.requires_hash(())): "v1-ubuntu-CUR"}

        rc, _o, err = self._run(prov, ["reap-images", "--superseded"], resolve_current_keys=resolve)
        self.assertEqual(rc, 0)
        self.assertEqual(prov.deleted_images, ["u1"])  # arch group KEPT (fail-safe)
        self.assertIn("arch", err)

    def test_superseded_accept_a_reaps_group_with_no_current_image(self) -> None:
        # accept-(a): the group's current key resolves but no cached image carries
        # it ⇒ every image in the group is superseded and reaped.
        prov = FakeProvider()
        prov.images["o1"] = self._img(img_id="o1", key="v1-ubuntu-OLD1")
        prov.images["o2"] = self._img(img_id="o2", key="v1-ubuntu-OLD2")

        def resolve(images, profile_for, operator, deps, warn):  # type: ignore[no-untyped-def]
            return {("ubuntu", "x86", imagecache.requires_hash(())): "v1-ubuntu-CUR"}  # no image carries CUR

        rc, _o, _e = self._run(prov, ["reap-images", "--superseded"], resolve_current_keys=resolve)
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(prov.deleted_images), ["o1", "o2"])

    def test_superseded_docker_variant_does_not_supersede_docker_less(self) -> None:
        # 4.4b reap data-loss guard: a docker and a docker-less image of the SAME
        # distro+arch are DISTINCT supersession groups (distinct requires-hash),
        # so reaping the off-key image of one group must NOT touch the live image
        # of the other. Each group keeps its own current image.
        prov = FakeProvider()
        prov.images["dl-cur"] = self._img(img_id="dl-cur", key="v1-ubuntu-DLCUR", requires=())
        prov.images["dl-old"] = self._img(img_id="dl-old", key="v1-ubuntu-DLOLD", requires=())
        prov.images["dk-cur"] = self._img(img_id="dk-cur", key="v1-ubuntu-DKCUR", requires=("docker",))
        prov.images["dk-old"] = self._img(img_id="dk-old", key="v1-ubuntu-DKOLD", requires=("docker",))

        def resolve(images, profile_for, operator, deps, warn):  # type: ignore[no-untyped-def]
            return {
                ("ubuntu", "x86", imagecache.requires_hash(())): "v1-ubuntu-DLCUR",
                ("ubuntu", "x86", imagecache.requires_hash(("docker",))): "v1-ubuntu-DKCUR",
            }

        rc, _o, _e = self._run(prov, ["reap-images", "--superseded"], resolve_current_keys=resolve)
        self.assertEqual(rc, 0)
        # only the off-key image of each group is reaped; BOTH current images survive.
        self.assertEqual(sorted(prov.deleted_images), ["dk-old", "dl-old"])

    # --- list failure -------------------------------------------------------- #
    def test_list_images_failure_exits_1(self) -> None:
        class _ListBoom(FakeProvider):
            def list_images(self, selector: str) -> list[Image]:
                raise providers.ProviderError("list exploded")

        prov = _ListBoom()
        rc, _o, err = self._run(prov, ["reap-images", "--distro", "ubuntu"])
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)

    # --- partial-success report ---------------------------------------------- #
    def test_per_image_delete_failure_partial_report_exit_1(self) -> None:
        class _DeleteFailsOne(FakeProvider):
            def delete_image(self, image_id: str) -> None:
                if image_id == "bad":
                    raise providers.ProviderError("delete exploded")
                super().delete_image(image_id)

        prov = _DeleteFailsOne()
        prov.images["good"] = self._img(img_id="good")
        prov.images["bad"] = self._img(img_id="bad")
        rc, out, err = self._run(prov, ["reap-images", "--distro", "ubuntu"])
        self.assertEqual(rc, 1)  # a real delete failed
        self.assertIn("good", prov.deleted_images)  # the other delete still happened
        self.assertIn("FAILED", err)
        self.assertIn("reaped 1", out)


class TestCacheImagePerRunReapIsolation(unittest.TestCase):
    """Task 7.2 — the data-loss guard: cache images vs the per-run reap (no network)."""

    def test_cache_image_carries_content_labels_not_run_label(self) -> None:
        ubuntu = distro.get_profile("ubuntu")
        labels = imagecache.cache_labels(
            ubuntu, "x86", key="v1-ubuntu-K", source_fp="fp", run_token="bird", requires=(),
        )
        # content-addressed labels present...
        self.assertEqual(labels[imagecache.LABEL_PURPOSE], imagecache.PURPOSE_IMAGE_CACHE)
        self.assertEqual(labels[imagecache.LABEL_CACHE_KEY], "v1-ubuntu-K")
        self.assertEqual(labels[imagecache.LABEL_DISTRO], "ubuntu")
        # ...and the ephemeral per-run reap label is ABSENT.
        self.assertNotIn(safety.LABEL_KEY, labels)

    def test_per_run_reap_leaves_cache_image_intact(self) -> None:
        prov = FakeProvider()
        run_id = safety.make_run_id("bird")
        # a cache image carrying ONLY content labels (no vmlease=<run-id>).
        cache_labels = imagecache.cache_labels(
            distro.get_profile("ubuntu"), "x86", key="v1-ubuntu-K", source_fp="fp", run_token="bird",
            requires=(),
        )
        cache_img = Image(
            id="cache-1", created="2024-04-25T13:26:27+00:00", disk_size=40.0, arch="x86", labels=cache_labels,
        )
        prov.images["cache-1"] = cache_img
        # a labelled run host (the ephemeral reap target).
        spec = HostSpec(
            name="vmlease-bird-host", image="ubuntu-24.04", server_type="cpx22",
            distro_key="ubuntu", labels=safety.run_label(run_id),
        )
        host = prov.create_with_cloudinit(spec, "#cloud-config")

        reaped = safety.reap(prov, run_id)

        # the run host is gone; the cache image survives untouched.
        self.assertEqual([h.name for h in reaped], ["vmlease-bird-host"])
        self.assertEqual(prov.destroyed, [host])
        self.assertEqual(prov.deleted_images, [])
        self.assertIn("cache-1", prov.images)


if __name__ == "__main__":
    unittest.main()
