from __future__ import annotations

import unittest
from datetime import date

from collector.normalize import (
    content_hash,
    infer_remote,
    name_key,
    normalize_employment_type,
    parse_date,
    parse_datetime,
    parse_money,
    safe_domain,
    slugify,
    strip_html,
)


class NormalizeTests(unittest.TestCase):
    def test_safe_domain(self) -> None:
        self.assertEqual(safe_domain("https://www.Example.com/path"), "example.com")
        self.assertEqual(safe_domain("https://sub.acme.co.uk/x"), "acme.co.uk")
        self.assertIsNone(safe_domain("https://jobs.lever.co/acme"))
        self.assertIsNone(safe_domain("not a url"))

    def test_slug_and_name_key(self) -> None:
        self.assertEqual(slugify("Acme, Inc."), "acme-inc")
        self.assertEqual(name_key("Acme, Inc."), "acmeinc")

    def test_dates(self) -> None:
        self.assertEqual(parse_date("2026-01"), date(2026, 1, 1))
        self.assertEqual(parse_date("2026"), date(2026, 1, 1))
        self.assertIsNotNone(parse_datetime("2026-01-02T03:04:05Z"))
        self.assertIsNotNone(parse_datetime(1788199939703))

    def test_money(self) -> None:
        self.assertEqual(parse_money("$171.2M"), (171_200_000.0, "USD"))
        self.assertEqual(parse_money("€10M"), (10_000_000.0, "EUR"))
        self.assertEqual(parse_money("Php80,000"), (80_000.0, "PHP"))
        self.assertEqual(parse_money("$1,000"), (1_000.0, "USD"))

    def test_remote(self) -> None:
        self.assertTrue(infer_remote("Remote - Worldwide"))
        self.assertFalse(infer_remote("San Francisco"))
        self.assertFalse(infer_remote("Berlin", "Hybrid"))
        self.assertTrue(infer_remote("Berlin", "Remote"))

    def test_employment_type(self) -> None:
        self.assertEqual(normalize_employment_type("Full-time"), "full_time")
        self.assertEqual(normalize_employment_type("Contractor"), "contract")
        self.assertIsNone(normalize_employment_type(""))

    def test_html(self) -> None:
        self.assertEqual(strip_html("<p>Hello <b>world</b></p>"), "Hello world")

    def test_hash_is_stable(self) -> None:
        self.assertEqual(content_hash("a", "b"), content_hash("a", "b"))
        self.assertNotEqual(content_hash("a", "b"), content_hash("a", "c"))


if __name__ == "__main__":
    unittest.main()