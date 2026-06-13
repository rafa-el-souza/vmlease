#!/usr/bin/env python3
"""Unit tests for vmlease.assertions — the eight non-regex assertion kinds.

stdlib unittest only. Run with:
    uv run python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import unittest

from vmlease.assertions import _ASSERTIONS
from vmlease.model import Assertion, Outcome


def _build(key: str, value: object) -> Assertion:
    """Validate + build the assertion for ``key`` bound to ``value``."""
    kind = _ASSERTIONS[key]
    kind.validate(value)
    return kind.build(value)


class TestRegistryShape(unittest.TestCase):
    def test_exactly_the_eight_keys(self) -> None:
        self.assertEqual(
            set(_ASSERTIONS),
            {
                "exit",
                "exit_not",
                "stdout_has",
                "stdout_lacks",
                "stderr_has",
                "stderr_lacks",
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
        self.assertTrue(a.evaluate(Outcome(0, "", "")))

    def test_exit_fail_and_describe(self) -> None:
        a = _build("exit", 0)
        outcome = Outcome(3, "", "")
        self.assertFalse(a.evaluate(outcome))
        self.assertEqual(a.describe(outcome), "exit 0: exit was 3")

    def test_exit_not_pass(self) -> None:
        a = _build("exit_not", 0)
        self.assertTrue(a.evaluate(Outcome(1, "", "")))

    def test_exit_not_fail_and_describe(self) -> None:
        a = _build("exit_not", 0)
        outcome = Outcome(0, "", "")
        self.assertFalse(a.evaluate(outcome))
        self.assertEqual(a.describe(outcome), "exit_not 0: exit was 0")

    def test_any_int_accepted_no_range_check(self) -> None:
        # D8#9 — 300 is out of 0..255 but must NOT be range-rejected.
        a = _build("exit", 300)
        self.assertTrue(a.evaluate(Outcome(300, "", "")))
        self.assertFalse(a.evaluate(Outcome(44, "", "")))

    def test_bool_rejected_as_shape_error(self) -> None:
        with self.assertRaises(ValueError):
            _build("exit", True)

    def test_non_int_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _build("exit", "0")


class TestSubstringHas(unittest.TestCase):
    def test_pass(self) -> None:
        a = _build("stdout_has", "READY")
        self.assertTrue(a.evaluate(Outcome(0, "all READY now", "")))

    def test_fail_and_describe(self) -> None:
        a = _build("stdout_has", "READY")
        outcome = Outcome(0, "nope", "")
        self.assertFalse(a.evaluate(outcome))
        self.assertEqual(a.describe(outcome), 'stdout_has "READY": substring not found')

    def test_literal_substring_not_complete_line(self) -> None:
        # D5 — substring, not complete-line: a mid-line match holds.
        a = _build("stdout_has", "EADY")
        self.assertTrue(a.evaluate(Outcome(0, "READY", "")))

    def test_stderr_stream(self) -> None:
        a = _build("stderr_has", "boom")
        self.assertTrue(a.evaluate(Outcome(0, "", "kaboom")))
        self.assertFalse(a.evaluate(Outcome(0, "boom", "")))

    def test_list_conjoins_all_present(self) -> None:
        a = _build("stdout_has", ["A", "B"])
        self.assertTrue(a.evaluate(Outcome(0, "A then B", "")))

    def test_list_fails_when_one_missing_describe_names_it(self) -> None:
        a = _build("stdout_has", ["A", "B"])
        outcome = Outcome(0, "only A", "")
        self.assertFalse(a.evaluate(outcome))
        self.assertEqual(a.describe(outcome), 'stdout_has "B": substring not found')

    def test_empty_stream_has_is_false(self) -> None:
        a = _build("stdout_has", "x")
        self.assertFalse(a.evaluate(Outcome(0, "", "")))


class TestSubstringLacks(unittest.TestCase):
    def test_pass(self) -> None:
        a = _build("stdout_lacks", "ERROR")
        self.assertTrue(a.evaluate(Outcome(0, "all good", "")))

    def test_fail_and_describe(self) -> None:
        a = _build("stdout_lacks", "ERROR")
        outcome = Outcome(0, "got ERROR here", "")
        self.assertFalse(a.evaluate(outcome))
        self.assertEqual(a.describe(outcome), 'stdout_lacks "ERROR": substring present')

    def test_stderr_stream(self) -> None:
        a = _build("stderr_lacks", "fail")
        self.assertTrue(a.evaluate(Outcome(0, "fail", "")))
        self.assertFalse(a.evaluate(Outcome(0, "", "fail")))

    def test_list_none_present(self) -> None:
        a = _build("stdout_lacks", ["X", "Y"])
        self.assertTrue(a.evaluate(Outcome(0, "Z", "")))

    def test_list_fails_when_one_present_describe_names_it(self) -> None:
        a = _build("stdout_lacks", ["X", "Y"])
        outcome = Outcome(0, "has Y", "")
        self.assertFalse(a.evaluate(outcome))
        self.assertEqual(a.describe(outcome), 'stdout_lacks "Y": substring present')

    def test_empty_stream_lacks_vacuously_true(self) -> None:
        # D8 boundary — absence over an empty stream is vacuously satisfied.
        a = _build("stdout_lacks", "x")
        self.assertTrue(a.evaluate(Outcome(0, "", "")))


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
        self.assertTrue(a.evaluate(Outcome(0, "   \n", "")))

    def test_stdout_empty_true_fail_and_describe(self) -> None:
        a = _build("stdout_empty", True)
        outcome = Outcome(0, "data", "")
        self.assertFalse(a.evaluate(outcome))
        self.assertEqual(a.describe(outcome), "stdout_empty: stdout was not empty")

    def test_stdout_empty_false_asserts_non_empty(self) -> None:
        # D8#4 — false asserts NON-empty: passes when there is content.
        a = _build("stdout_empty", False)
        self.assertTrue(a.evaluate(Outcome(0, "data", "")))

    def test_stdout_empty_false_fails_on_blank_and_describe(self) -> None:
        a = _build("stdout_empty", False)
        outcome = Outcome(0, "  ", "")
        self.assertFalse(a.evaluate(outcome))
        self.assertEqual(a.describe(outcome), "stdout_empty: stdout was empty")

    def test_stderr_empty_true_pass(self) -> None:
        a = _build("stderr_empty", True)
        self.assertTrue(a.evaluate(Outcome(0, "noise", "")))

    def test_stderr_empty_true_fail_and_describe(self) -> None:
        a = _build("stderr_empty", True)
        outcome = Outcome(0, "", "warn")
        self.assertFalse(a.evaluate(outcome))
        self.assertEqual(a.describe(outcome), "stderr_empty: stderr was not empty")

    def test_empty_bool_shape_check(self) -> None:
        with self.assertRaises(ValueError):
            _build("stdout_empty", "yes")


if __name__ == "__main__":
    unittest.main()
