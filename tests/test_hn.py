from __future__ import annotations

import unittest

from collector.sources.hn import parse_comment


class HnParserTests(unittest.TestCase):
    def test_ats_link(self) -> None:
        candidate = parse_comment(
            {
                "id": 123,
                "text": (
                    "<p>Acme | Engineer | Remote</p>"
                    '<p><a href="https://boards.greenhouse.io/acme">Apply</a></p>'
                ),
            },
            "999",
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.name, "Acme")
        self.assertEqual(candidate.ats_type, "greenhouse")
        self.assertEqual(candidate.ats_slug, "acme")
        self.assertEqual(candidate.company_key, "hn:123")

    def test_company_website(self) -> None:
        candidate = parse_comment(
            {
                "id": 124,
                "text": (
                    "<p>Beta Corp | Designer</p>"
                    '<p><a href="https://beta.com">Website</a></p>'
                ),
            },
            "999",
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.domain, "beta.com")
        self.assertEqual(candidate.company_key, "domain:beta.com")

    def test_no_links_is_skipped(self) -> None:
        self.assertIsNone(parse_comment({"id": 1, "text": "<p>No links here</p>"}, "999"))


if __name__ == "__main__":
    unittest.main()