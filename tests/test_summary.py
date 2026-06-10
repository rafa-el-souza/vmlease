#!/usr/bin/env python3
"""Unit tests for vmlease.summary + the `summarize` CLI subcommand.

stdlib unittest only; no network, no real VMs. Pure dict fixtures + temp files.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tests.battery_helpers import battery_toml
from vmlease import cli, model, summary
from vmlease.battery import load_battery


def _write_battery(d: str, manifest: str) -> Path:
    p = Path(d) / "battery.toml"
    p.write_text(manifest, encoding="utf-8")
    return p


def _load_battery(manifest: str) -> model.Battery:
    with tempfile.TemporaryDirectory() as d:
        return load_battery(_write_battery(d, manifest))


_BATTERY_MANIFEST = battery_toml(
    "summary-demo",
    (
        {"id": "start", "title": "start the sandbox", "run": "true",
         "tag": "mutating:host-root", "classifies": "sandbox start"},
        {"id": "status-stopped", "title": "status while stopped", "run": "true",
         "tag": "read-only", "classifies": "sandbox status"},
        {"id": "destroy", "title": "destroy", "run": "true",
         "tag": "mutating:host-root", "classifies": "sandbox destroy"},
    ),
)


def _probe(pid: str, exit_code: int = 0, stdout: str = "", stderr: str = "",
           timed_out: bool = False, tag: str = "read-only") -> dict[str, object]:
    return {
        "id": pid, "tag": tag, "exit_code": exit_code, "ok": exit_code == 0,
        "timed_out": timed_out, "stdout": stdout, "stderr": stderr,
    }


def _raw_doc(hosts: list[dict[str, object]]) -> dict[str, object]:
    return {"run_id": "r1", "timestamp": "20260601T000000Z", "hosts": hosts}


# --------------------------------------------------------------------------- #
# harvest_tokens + verdict
# --------------------------------------------------------------------------- #
class TestHarvestTokens(unittest.TestCase):
    def test_buckets_by_suffix(self) -> None:
        tokens = summary.harvest_tokens("SETUP_EXIT0_OK then START_CORE_FAIL plus NOTE_info and X_review")
        self.assertEqual(tokens["ok_tokens"], ["SETUP_EXIT0_OK"])
        self.assertEqual(tokens["fail_tokens"], ["START_CORE_FAIL"])
        self.assertEqual(tokens["info_tokens"], ["NOTE_info"])
        self.assertEqual(tokens["review_tokens"], ["X_review"])

    def test_order_preserving_and_deduped(self) -> None:
        tokens = summary.harvest_tokens("A_OK B_OK A_OK")
        self.assertEqual(tokens["ok_tokens"], ["A_OK", "B_OK"])

    def test_no_tokens_yields_empty_buckets(self) -> None:
        tokens = summary.harvest_tokens("plain prose, nothing shouty")
        self.assertEqual(tokens, {"ok_tokens": [], "fail_tokens": [], "info_tokens": [], "review_tokens": []})


class TestVerdict(unittest.TestCase):
    def test_timeout_dominates(self) -> None:
        # timed_out wins regardless of tokens/exit (spec: Timeout dominates)
        self.assertEqual(summary.verdict(0, True, ["X_FAIL"], ["Y_OK"]), summary.TIMEOUT)

    def test_fail_token_forces_fail_on_zero_exit(self) -> None:
        self.assertEqual(summary.verdict(0, False, ["START_CORE_NOT_RUNNING_FAIL"], []), summary.FAIL)

    def test_nonzero_exit_is_fail(self) -> None:
        self.assertEqual(summary.verdict(1, False, [], ["Y_OK"]), summary.FAIL)

    def test_pass_with_ok_token(self) -> None:
        self.assertEqual(summary.verdict(0, False, [], ["SETUP_EXIT0_OK"]), summary.PASS)

    def test_pass_no_assertions(self) -> None:
        self.assertEqual(summary.verdict(0, False, [], []), summary.PASS_NO_ASSERTIONS)


# --------------------------------------------------------------------------- #
# summarize_results
# --------------------------------------------------------------------------- #
class TestSummarizeResults(unittest.TestCase):
    def test_schema_and_top_level_fields(self) -> None:
        s = summary.summarize_results(_raw_doc([]), source_raw="r.json")
        self.assertEqual(s["schema_version"], "1")
        self.assertEqual(s["run_id"], "r1")
        self.assertEqual(s["timestamp"], "20260601T000000Z")
        self.assertEqual(s["source_raw"], "r.json")
        self.assertIsNone(s["battery"])

    def test_rejects_malformed_doc(self) -> None:
        with self.assertRaises(ValueError):
            summary.summarize_results({"no": "hosts"})

    def test_probe_record_shape_and_builtin_command_map(self) -> None:
        doc = _raw_doc([{"distro": "ubuntu", "image": "u", "detail": "d",
                         "probes": [_probe("start", stdout="SETUP_EXIT0_OK")]}])
        s = summary.summarize_results(doc)
        probe = s["hosts"][0]["probes"][0]
        self.assertEqual(probe["command"], "sandbox start")  # built-in map fallback
        self.assertEqual(probe["verdict"], summary.PASS)
        self.assertEqual(probe["ok_tokens"], ["SETUP_EXIT0_OK"])
        self.assertIn("stdout_tail", probe)
        self.assertIn("stderr_tail", probe)
        self.assertNotIn("not_run", s["hosts"][0])  # no battery → omitted

    def test_matrix_pivots_command_against_distro(self) -> None:
        doc = _raw_doc([
            {"distro": "ubuntu", "image": "u", "detail": "", "probes": [_probe("start", exit_code=1)]},
            {"distro": "fedora", "image": "f", "detail": "", "probes": [_probe("start", stdout="X_OK")]},
        ])
        s = summary.summarize_results(doc)
        self.assertEqual(s["matrix"]["sandbox start"], {"ubuntu": "FAIL", "fedora": "PASS"})

    def test_worst_of_collapse_for_one_command_two_probes(self) -> None:
        doc = _raw_doc([{"distro": "ubuntu", "image": "u", "detail": "", "probes": [
            _probe("status-stopped", stdout="A_OK"),
            _probe("status-running", stdout="B_FAIL"),
        ]}])
        s = summary.summarize_results(doc)
        self.assertEqual(s["matrix"]["sandbox status"]["ubuntu"], "FAIL")

    def test_totals_count_by_verdict(self) -> None:
        doc = _raw_doc([{"distro": "ubuntu", "image": "u", "detail": "", "probes": [
            _probe("start", stdout="A_OK"),                 # PASS
            _probe("stop", exit_code=1),                    # FAIL
            _probe("doctor"),                               # PASS_NO_ASSERTIONS
            _probe("attach", timed_out=True),               # TIMEOUT
        ]}])
        s = summary.summarize_results(doc)
        self.assertEqual(s["totals"], {"PASS": 1, "FAIL": 1, "TIMEOUT": 1, "PASS_NO_ASSERTIONS": 1})

    def test_tail_bounding(self) -> None:
        big = "x" * (summary.TAIL_LEN + 500)
        doc = _raw_doc([{"distro": "ubuntu", "image": "u", "detail": "", "probes": [
            _probe("start", stdout=big, stderr=big),
        ]}])
        s = summary.summarize_results(doc)
        probe = s["hosts"][0]["probes"][0]
        self.assertEqual(len(probe["stdout_tail"]), summary.TAIL_LEN)
        self.assertEqual(len(probe["stderr_tail"]), summary.TAIL_LEN)

    def test_battery_overlay_and_declared_but_not_run(self) -> None:
        battery = _load_battery(_BATTERY_MANIFEST)
        doc = _raw_doc([{"distro": "ubuntu", "image": "u", "detail": "", "probes": [
            _probe("start", stdout="A_OK"),
            _probe("status-stopped", stdout="B_OK"),
            # 'destroy' declared in the battery but absent here.
        ]}])
        s = summary.summarize_results(doc, battery=battery)
        self.assertEqual(s["battery"], "summary-demo")
        self.assertEqual(s["hosts"][0]["probes"][0]["command"], "sandbox start")  # builtin label
        self.assertEqual(s["hosts"][0]["not_run"], ["destroy"])

    def test_builtin_label_wins_and_matrix_keys_stay_clean_with_battery(self) -> None:
        # A battery whose `classifies` for a KNOWN id is a long sentence (not a
        # label) must NOT pollute the command/matrix key — the builtin wins.
        battery = _load_battery(battery_toml("noisy", (
            {"id": "start", "title": "t", "run": "true", "tag": "mutating:host-root",
             "classifies": "a very long human sentence describing what start does in detail"},
            {"id": "novel-probe", "title": "novel title", "run": "true",
             "tag": "read-only", "classifies": "novel classifies label"},
        )))
        doc = _raw_doc([{"distro": "ubuntu", "image": "u", "detail": "", "probes": [
            _probe("start", stdout="A_OK"),
            _probe("novel-probe", stdout="B_OK"),
        ]}])
        s = summary.summarize_results(doc, battery=battery)
        # Known id → clean builtin label, NOT the classifies sentence.
        self.assertEqual(s["hosts"][0]["probes"][0]["command"], "sandbox start")
        # Unknown id → battery fills it (prefers classifies).
        self.assertEqual(s["hosts"][0]["probes"][1]["command"], "novel classifies label")
        # Matrix keys are the clean labels, not sentences.
        self.assertIn("sandbox start", s["matrix"])
        self.assertNotIn(
            "a very long human sentence describing what start does in detail", s["matrix"]
        )

    def test_unknown_id_degrades_to_raw_id(self) -> None:
        doc = _raw_doc([{"distro": "ubuntu", "image": "u", "detail": "", "probes": [_probe("mystery")]}])
        s = summary.summarize_results(doc)
        self.assertEqual(s["hosts"][0]["probes"][0]["command"], "mystery")

    def test_token_trailing_boundary_rejects_longer_word(self) -> None:
        # `_FAILED` (a longer word) must NOT be harvested as a `_FAIL` token.
        doc = _raw_doc([{"distro": "ubuntu", "image": "u", "detail": "", "probes": [
            _probe("start", exit_code=0, stdout="STEP_FAILED maybe; STEP_OK"),
        ]}])
        s = summary.summarize_results(doc)
        probe = s["hosts"][0]["probes"][0]
        self.assertEqual(probe["fail_tokens"], [])           # STEP_FAILED is not a _FAIL token
        self.assertEqual(probe["ok_tokens"], ["STEP_OK"])
        self.assertEqual(probe["verdict"], "PASS")


class TestOverallExitCode(unittest.TestCase):
    def test_zero_when_all_pass(self) -> None:
        s = {"totals": {"PASS": 2, "FAIL": 0, "TIMEOUT": 0, "PASS_NO_ASSERTIONS": 1}}
        self.assertEqual(summary.overall_exit_code(s), 0)

    def test_nonzero_on_fail(self) -> None:
        self.assertEqual(summary.overall_exit_code({"totals": {"FAIL": 1}}), 1)

    def test_nonzero_on_timeout(self) -> None:
        self.assertEqual(summary.overall_exit_code({"totals": {"TIMEOUT": 1}}), 1)


class TestSummaryFilename(unittest.TestCase):
    def test_derives_summary_json(self) -> None:
        self.assertEqual(
            summary.summary_filename(Path("/x/vmlease-r1-20260601T000000Z.json")),
            "vmlease-r1-20260601T000000Z.summary.json",
        )


# --------------------------------------------------------------------------- #
# CLI summarize — end-to-end on temp files
# --------------------------------------------------------------------------- #
class TestCliSummarize(unittest.TestCase):
    def _write_raw(self, d: str, hosts: list[dict[str, object]]) -> Path:
        p = Path(d) / "vmlease-r1-20260601T000000Z.json"
        p.write_text(json.dumps(_raw_doc(hosts), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return p

    def test_companion_written_beside_raw_and_raw_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            raw = self._write_raw(d, [{"distro": "ubuntu", "image": "u", "detail": "",
                                       "probes": [_probe("start", stdout="A_OK")]}])
            before = raw.read_bytes()
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["summarize", str(raw)])
            self.assertEqual(rc, 0)
            companion = raw.parent / "vmlease-r1-20260601T000000Z.summary.json"
            self.assertTrue(companion.exists())
            self.assertIn(str(companion), buf.getvalue())
            self.assertEqual(raw.read_bytes(), before)  # raw byte-for-byte unchanged
            doc = json.loads(companion.read_text(encoding="utf-8"))
            self.assertEqual(doc["schema_version"], "1")

    def test_explicit_out_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            raw = self._write_raw(d, [{"distro": "ubuntu", "image": "u", "detail": "",
                                       "probes": [_probe("start", stdout="A_OK")]}])
            out = Path(d) / "s.json"
            rc = cli.main(["summarize", str(raw), "--out", str(out)])
            self.assertEqual(rc, 0)
            self.assertTrue(out.exists())

    def test_exit_nonzero_when_a_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            raw = self._write_raw(d, [{"distro": "ubuntu", "image": "u", "detail": "",
                                       "probes": [_probe("start", exit_code=1)]}])
            with redirect_stdout(io.StringIO()):
                rc = cli.main(["summarize", str(raw)])
            self.assertEqual(rc, 1)
            self.assertTrue((raw.parent / "vmlease-r1-20260601T000000Z.summary.json").exists())  # still written

    def test_battery_flag_surfaces_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            raw = self._write_raw(d, [{"distro": "ubuntu", "image": "u", "detail": "",
                                       "probes": [_probe("start", stdout="A_OK"),
                                                  _probe("status-stopped", stdout="B_OK")]}])
            bpath = _write_battery(d, _BATTERY_MANIFEST)
            out = Path(d) / "s.json"
            rc = cli.main(["summarize", str(raw), "--battery", str(bpath), "--out", str(out)])
            self.assertEqual(rc, 0)
            doc = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(doc["hosts"][0]["not_run"], ["destroy"])

    def test_missing_file_exits_2_without_summary(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "nope.json"
            err = io.StringIO()
            with redirect_stderr(err):
                rc = cli.main(["summarize", str(missing)])
            self.assertEqual(rc, 2)
            self.assertIn("error:", err.getvalue())
            self.assertFalse((Path(d) / "nope.summary.json").exists())

    def test_malformed_json_exits_2_without_summary(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            raw = Path(d) / "bad.json"
            raw.write_text("{not json", encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err):
                rc = cli.main(["summarize", str(raw)])
            self.assertEqual(rc, 2)
            self.assertIn("not valid JSON", err.getvalue())
            self.assertFalse((Path(d) / "bad.summary.json").exists())

    def test_bad_battery_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            raw = self._write_raw(d, [])
            bpath = Path(d) / "battery.toml"
            bpath.write_text("name = ", encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err):
                rc = cli.main(["summarize", str(raw), "--battery", str(bpath)])
            self.assertEqual(rc, 2)
            self.assertIn("error:", err.getvalue())

    def test_malformed_doc_shape_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            raw = Path(d) / "shape.json"
            raw.write_text(json.dumps({"run_id": "r", "no_hosts": True}), encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err):
                rc = cli.main(["summarize", str(raw)])
            self.assertEqual(rc, 2)
            self.assertIn("error:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
