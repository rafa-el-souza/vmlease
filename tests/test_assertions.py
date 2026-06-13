#!/usr/bin/env python3
"""Unit tests for vmlease.assertions — the eight non-regex assertion kinds.

stdlib unittest only. Run with:
    uv run python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import subprocess
import sys
import time
import unittest
import unittest.mock

from vmlease.assertions import _ASSERTIONS
from vmlease.model import Assertion, Outcome


def _build(key: str, value: object) -> Assertion:
    """Validate + build the assertion for ``key`` bound to ``value``."""
    kind = _ASSERTIONS[key]
    kind.validate(value)
    return kind.build(value)


class TestRegistryShape(unittest.TestCase):
    def test_exactly_the_twelve_keys(self) -> None:
        self.assertEqual(
            set(_ASSERTIONS),
            {
                "exit",
                "exit_not",
                "stdout_has",
                "stdout_lacks",
                "stderr_has",
                "stderr_lacks",
                "stdout_matches",
                "stdout_matches_not",
                "stderr_matches",
                "stderr_matches_not",
                "stdout_empty",
                "stderr_empty",
            },
        )

    def test_built_objects_satisfy_protocol(self) -> None:
        # runtime_checkable Protocol — a structural sanity check.
        self.assertIsInstance(_build("exit", 0), Assertion)
        self.assertIsInstance(_build("stdout_has", "x"), Assertion)
        self.assertIsInstance(_build("stdout_empty", True), Assertion)


class TestExit(unittest.TestCase):
    def test_exit_pass(self) -> None:
        a = _build("exit", 0)
        self.assertIsNone(a.check(Outcome(0, "", "")))

    def test_exit_fail_and_describe(self) -> None:
        a = _build("exit", 0)
        outcome = Outcome(3, "", "")
        self.assertEqual(a.check(outcome), "exit 0: exit was 3")

    def test_exit_not_pass(self) -> None:
        a = _build("exit_not", 0)
        self.assertIsNone(a.check(Outcome(1, "", "")))

    def test_exit_not_fail_and_describe(self) -> None:
        a = _build("exit_not", 0)
        outcome = Outcome(0, "", "")
        self.assertEqual(a.check(outcome), "exit_not 0: exit was 0")

    def test_any_int_accepted_no_range_check(self) -> None:
        # D8#9 — 300 is out of 0..255 but must NOT be range-rejected.
        a = _build("exit", 300)
        self.assertIsNone(a.check(Outcome(300, "", "")))
        self.assertIsNotNone(a.check(Outcome(44, "", "")))

    def test_bool_rejected_as_shape_error(self) -> None:
        with self.assertRaises(ValueError):
            _build("exit", True)

    def test_non_int_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _build("exit", "0")


class TestSubstringHas(unittest.TestCase):
    def test_pass(self) -> None:
        a = _build("stdout_has", "READY")
        self.assertIsNone(a.check(Outcome(0, "all READY now", "")))

    def test_fail_and_describe(self) -> None:
        a = _build("stdout_has", "READY")
        outcome = Outcome(0, "nope", "")
        self.assertEqual(a.check(outcome), 'stdout_has "READY": substring not found')

    def test_literal_substring_not_complete_line(self) -> None:
        # D5 — substring, not complete-line: a mid-line match holds.
        a = _build("stdout_has", "EADY")
        self.assertIsNone(a.check(Outcome(0, "READY", "")))

    def test_stderr_stream(self) -> None:
        a = _build("stderr_has", "boom")
        self.assertIsNone(a.check(Outcome(0, "", "kaboom")))
        self.assertIsNotNone(a.check(Outcome(0, "boom", "")))

    def test_list_conjoins_all_present(self) -> None:
        a = _build("stdout_has", ["A", "B"])
        self.assertIsNone(a.check(Outcome(0, "A then B", "")))

    def test_list_fails_when_one_missing_describe_names_it(self) -> None:
        a = _build("stdout_has", ["A", "B"])
        outcome = Outcome(0, "only A", "")
        self.assertEqual(a.check(outcome), 'stdout_has "B": substring not found')

    def test_empty_stream_has_is_false(self) -> None:
        a = _build("stdout_has", "x")
        self.assertIsNotNone(a.check(Outcome(0, "", "")))


class TestSubstringLacks(unittest.TestCase):
    def test_pass(self) -> None:
        a = _build("stdout_lacks", "ERROR")
        self.assertIsNone(a.check(Outcome(0, "all good", "")))

    def test_fail_and_describe(self) -> None:
        a = _build("stdout_lacks", "ERROR")
        outcome = Outcome(0, "got ERROR here", "")
        self.assertEqual(a.check(outcome), 'stdout_lacks "ERROR": substring present')

    def test_stderr_stream(self) -> None:
        a = _build("stderr_lacks", "fail")
        self.assertIsNone(a.check(Outcome(0, "fail", "")))
        self.assertIsNotNone(a.check(Outcome(0, "", "fail")))

    def test_list_none_present(self) -> None:
        a = _build("stdout_lacks", ["X", "Y"])
        self.assertIsNone(a.check(Outcome(0, "Z", "")))

    def test_list_fails_when_one_present_describe_names_it(self) -> None:
        a = _build("stdout_lacks", ["X", "Y"])
        outcome = Outcome(0, "has Y", "")
        self.assertEqual(a.check(outcome), 'stdout_lacks "Y": substring present')

    def test_empty_stream_lacks_vacuously_true(self) -> None:
        # D8 boundary — absence over an empty stream is vacuously satisfied.
        a = _build("stdout_lacks", "x")
        self.assertIsNone(a.check(Outcome(0, "", "")))


class TestEmptyList(unittest.TestCase):
    def test_empty_list_rejected_naming_key(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _build("stdout_has", [])
        self.assertIn("stdout_has", str(ctx.exception))

    def test_non_string_list_element_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _build("stdout_has", ["ok", 3])

    def test_wrong_shape_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _build("stdout_has", 7)


class TestEmpty(unittest.TestCase):
    def test_stdout_empty_true_pass_on_blank(self) -> None:
        a = _build("stdout_empty", True)
        self.assertIsNone(a.check(Outcome(0, "   \n", "")))

    def test_stdout_empty_true_fail_and_describe(self) -> None:
        a = _build("stdout_empty", True)
        outcome = Outcome(0, "data", "")
        self.assertEqual(a.check(outcome), "stdout_empty: stdout was not empty")

    def test_stdout_empty_false_asserts_non_empty(self) -> None:
        # D8#4 — false asserts NON-empty: passes when there is content.
        a = _build("stdout_empty", False)
        self.assertIsNone(a.check(Outcome(0, "data", "")))

    def test_stdout_empty_false_fails_on_blank_and_describe(self) -> None:
        a = _build("stdout_empty", False)
        outcome = Outcome(0, "  ", "")
        self.assertEqual(a.check(outcome), "stdout_empty: stdout was empty")

    def test_stderr_empty_true_pass(self) -> None:
        a = _build("stderr_empty", True)
        self.assertIsNone(a.check(Outcome(0, "noise", "")))

    def test_stderr_empty_true_fail_and_describe(self) -> None:
        a = _build("stderr_empty", True)
        outcome = Outcome(0, "", "warn")
        self.assertEqual(a.check(outcome), "stderr_empty: stderr was not empty")

    def test_empty_bool_shape_check(self) -> None:
        with self.assertRaises(ValueError):
            _build("stdout_empty", "yes")


class TestRegexMatches(unittest.TestCase):
    def test_stdout_happy(self) -> None:
        a = _build("stdout_matches", r"READ[Yy]")
        self.assertIsNone(a.check(Outcome(0, "all READY now", "")))

    def test_stdout_fail_and_describe(self) -> None:
        a = _build("stdout_matches", r"READY")
        outcome = Outcome(0, "nope", "")
        self.assertEqual(
            a.check(outcome), 'stdout_matches "READY": pattern did not match'
        )

    def test_stderr_happy_and_fail(self) -> None:
        a = _build("stderr_matches", r"boo.")
        self.assertIsNone(a.check(Outcome(0, "", "kaboom")))
        self.assertIsNotNone(a.check(Outcome(0, "boom", "")))

    def test_unanchored_search_matches_anywhere(self) -> None:
        # .search is unanchored (NOT .match/.fullmatch): mid-text holds.
        a = _build("stdout_matches", r"DY")
        self.assertIsNone(a.check(Outcome(0, "READY", "")))

    def test_list_conjoins_all_match(self) -> None:
        a = _build("stdout_matches", [r"A.", r"B."])
        self.assertIsNone(a.check(Outcome(0, "Ax then Bz", "")))

    def test_list_fails_when_one_missing_describe_names_it(self) -> None:
        a = _build("stdout_matches", [r"A.", r"B."])
        outcome = Outcome(0, "Ax only", "")
        self.assertEqual(
            a.check(outcome), 'stdout_matches "B.": pattern did not match'
        )

    def test_empty_stream_matches_is_false(self) -> None:
        a = _build("stdout_matches", r"x")
        self.assertIsNotNone(a.check(Outcome(0, "", "")))

    def test_empty_list_rejected_naming_key(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _build("stdout_matches", [])
        self.assertIn("stdout_matches", str(ctx.exception))


class TestRegexMatchesNot(unittest.TestCase):
    def test_pass_when_absent(self) -> None:
        a = _build("stdout_matches_not", r"ERROR")
        self.assertIsNone(a.check(Outcome(0, "all good", "")))

    def test_fail_and_describe(self) -> None:
        a = _build("stdout_matches_not", r"ERR.R")
        outcome = Outcome(0, "got ERROR here", "")
        self.assertEqual(
            a.check(outcome), 'stdout_matches_not "ERR.R": pattern matched'
        )

    def test_stderr_stream(self) -> None:
        a = _build("stderr_matches_not", r"fail")
        self.assertIsNone(a.check(Outcome(0, "fail", "")))
        self.assertIsNotNone(a.check(Outcome(0, "", "fail")))

    def test_list_none_match(self) -> None:
        a = _build("stdout_matches_not", [r"X.", r"Y."])
        self.assertIsNone(a.check(Outcome(0, "Zz", "")))

    def test_list_fails_when_one_matches_describe_names_it(self) -> None:
        a = _build("stdout_matches_not", [r"X.", r"Y."])
        outcome = Outcome(0, "has Yo", "")
        self.assertEqual(
            a.check(outcome), 'stdout_matches_not "Y.": pattern matched'
        )

    def test_empty_stream_matches_not_vacuously_true(self) -> None:
        a = _build("stdout_matches_not", r"x")
        self.assertIsNone(a.check(Outcome(0, "", "")))


class TestRE2AnchoringSemantics(unittest.TestCase):
    """Anchoring asserted as RE2, NOT Python `re` (D8#1/D10(K))."""

    def test_multiline_flag_anchors_per_line(self) -> None:
        # (?m)^X$ matches a line in the middle of multi-line text.
        a = _build("stdout_matches", r"(?m)^READY$")
        self.assertIsNone(a.check(Outcome(0, "starting\nREADY\ndone\n", "")))

    def test_bare_anchors_are_whole_text_not_per_line(self) -> None:
        # Without (?m), ^X$ is whole-text: a mid-text line does NOT match.
        a = _build("stdout_matches", r"^READY$")
        self.assertIsNotNone(a.check(Outcome(0, "starting\nREADY\ndone\n", "")))

    def test_dollar_does_not_match_before_trailing_newline(self) -> None:
        # RE2 `$` is end-of-text only — unlike Python `re`, it does NOT match
        # before a trailing \n. So `^READY$` fails against "READY\n".
        a = _build("stdout_matches", r"^READY$")
        self.assertIsNotNone(a.check(Outcome(0, "READY\n", "")))
        # Without the trailing newline it matches whole-text.
        self.assertIsNone(a.check(Outcome(0, "READY", "")))


class TestRegexCompileRejection(unittest.TestCase):
    """Bad patterns rejected AT COMPILE (validate/build), raising ValueError (D10(I/J))."""

    def test_malformed_pattern_rejected_at_validate(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _ASSERTIONS["stdout_matches"].validate(r"(unclosed")
        self.assertIn("stdout_matches", str(ctx.exception))

    def test_backreference_rejected(self) -> None:
        # RE2 omits backreferences — surfaces through the compile-failure path.
        with self.assertRaises(ValueError):
            _build("stdout_matches", r"(a)\1")

    def test_lookahead_rejected(self) -> None:
        # RE2 omits lookaround — same compile-failure path.
        with self.assertRaises(ValueError):
            _build("stdout_matches", r"(?=x)")

    def test_over_max_mem_pattern_rejected_at_compile(self) -> None:
        # A syntactically-valid pattern whose automaton exceeds the 8 MiB
        # max_mem budget is rejected at compile (D8#8/D10(H)). Self-checking:
        # the SAME pattern compiles under a generous budget, proving the
        # rejection is budget-driven, not a syntax error.
        import re2

        over_budget = "[Ā-￿]{1000}" * 100
        with self.assertRaises(ValueError):
            _build("stdout_matches", over_budget)
        big = re2.Options()
        big.max_mem = 1024 * 1024 * 1024
        # Proves the pattern is well-formed; only the budget rejected it above.
        self.assertIsNotNone(re2.compile(over_budget, options=big))


class TestRe2CompileLoggingSilenced(unittest.TestCase):
    """RE2's own compile-error logging never reaches operator stderr (finding 1).

    A malformed pattern in ``[probe.assert]`` is surfaced as ``BatteryError`` by
    the loader; RE2's default ``log_errors`` would ALSO dump raw absl/C++ engine
    noise to fd 2 (``WARNING: All log messages before absl::InitializeLog()…``,
    ``E0000 … re2.cc … Error parsing …``) on top of that clean error. The
    ``log_errors=False`` option on ``_compile_re2``'s ``Options`` silences it.

    The noise is written to fd 2 from C++, so ``contextlib.redirect_stderr`` can
    NOT capture it — this runs ``parse_battery`` over a malformed-regex battery in
    a SUBPROCESS and inspects the real fd-2 stream (mirrors the engine-free model
    gate ``test_model_imports_no_regex_engine_or_assertions``).
    """

    def test_parse_battery_malformed_regex_keeps_stderr_clean(self) -> None:
        manifest = (
            "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\n"
            "tag = '''read-only'''\nrun = '''c'''\n"
            "[probe.assert]\nstdout_matches = '''(unclosed'''\n"
        )
        code = (
            "import vmlease.battery as b\n"
            f"manifest = {manifest!r}\n"
            "try:\n"
            "    b.parse_battery(manifest)\n"
            "except b.BatteryError:\n"
            "    print('BATTERY_ERROR_RAISED')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        # (b) the clean BatteryError still fired (engine still raises identically).
        self.assertIn("BATTERY_ERROR_RAISED", proc.stdout)
        # (a) NONE of RE2's raw engine noise leaked to operator stderr.
        for needle in ("re2.cc", "E0000", "absl"):
            self.assertNotIn(needle, proc.stderr)


class TestCheckSinglePass(unittest.TestCase):
    """``check`` computes the match once — ``_describe`` is never called on a pass.

    Proves the recompute is gone: the old ``evaluate`` + ``describe`` shape
    re-scanned to name the offender. The merged ``check`` builds the failure
    string in the same pass, so ``_describe`` fires exactly once on failure and
    never on the pass path.
    """

    def test_substring_pass_does_not_call_describe(self) -> None:
        a = _build("stdout_has", "READY")
        with unittest.mock.patch("vmlease.assertions._describe") as describe:
            self.assertIsNone(a.check(Outcome(0, "all READY now", "")))
            describe.assert_not_called()

    def test_substring_fail_calls_describe_once(self) -> None:
        a = _build("stdout_has", "READY")
        with unittest.mock.patch(
            "vmlease.assertions._describe", return_value="MSG"
        ) as describe:
            self.assertEqual(a.check(Outcome(0, "nope", "")), "MSG")
            describe.assert_called_once()


class TestRe2ReDoSResistance(unittest.TestCase):
    """RE2 evaluates an adversarial pattern x adversarial input in LINEAR time (D6).

    Threat model (D6): batteries AND workloads are untrusted, so BOTH the
    author-written pattern and the workload-controlled stdout/stderr are
    adversarial, and assertion eval is post-capture in-harness (NOT bounded by
    the per-probe command timeout). A backtracking engine (stdlib ``re``) on
    ``(a+)+$`` against a long all-``a`` input with a non-matching tail exhibits
    CATASTROPHIC exponential backtracking — an effectively unbounded hang (ReDoS).
    RE2 is linear-time BY CONSTRUCTION, so it evaluates near-instantly. This also
    validates the no-input-cap decision (D8#2): even a 100k-char adversarial input
    is bounded by the engine, not by truncation (which would silently false-pass an
    absence assertion past the cap).
    """

    def test_catastrophic_pattern_evaluates_in_linear_time(self) -> None:
        # ``(a+)+$`` over ``"a"*N + "!"`` is the classic ReDoS worst case: every
        # grouping of the a-run must be tried to reach ``$``, which the trailing
        # ``!`` defeats. Exponential under backtracking; O(n) under RE2.
        a = _build("stdout_matches", r"(a+)+$")
        outcome = Outcome(0, "a" * 100_000 + "!", "")
        start = time.monotonic()
        result = a.check(outcome)
        elapsed = time.monotonic() - start
        # No match (the ``!`` blocks ``$``) -> a failure description, computed FAST.
        self.assertIsNotNone(result)
        self.assertLess(
            elapsed,
            2.0,
            f"RE2 eval took {elapsed:.3f}s over a ReDoS pattern+input; a backtracking "
            "engine would hang here. RE2's linear-time guarantee (D6) is broken if slow.",
        )

    def test_embedded_code_construct_rejected_at_compile(self) -> None:
        # No-RCE is STRUCTURAL: RE2 has no embedded-code construct, so
        # ``(?{...})`` / ``(??{...})`` fail to compile (rejected at load, never
        # executed) — the same compile-failure path as backref/lookaround.
        for evil in (r"(?{ system('id') })", r"(??{ 1 })"):
            with self.assertRaises(ValueError):
                _build("stdout_matches", evil)


if __name__ == "__main__":
    unittest.main()
