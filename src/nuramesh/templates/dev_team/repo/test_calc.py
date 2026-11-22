"""Tests for calc.py (standard library unittest, so the sandbox image needs nothing extra)."""

import unittest

from calc import add, multiply


class CalcTests(unittest.TestCase):
    def test_add(self) -> None:
        self.assertEqual(add(2, 3), 5)

    def test_add_negative(self) -> None:
        self.assertEqual(add(-2, 3), 1)

    def test_multiply(self) -> None:
        self.assertEqual(multiply(4, 5), 20)


if __name__ == "__main__":
    unittest.main()
