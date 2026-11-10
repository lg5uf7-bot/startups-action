from __future__ import annotations

import unittest

from collector.ats.patterns import board_url, extract_ats


class AtsPatternTests(unittest.TestCase):
    def test_greenhouse(self) -> None:
        self.assertEqual(
            extract_ats("https://boards.greenhouse.io/acme"),
            ("greenhouse", "acme"),
        )
        self.assertEqual(
            extract_ats("https://job-boards.greenhouse.io/acme/jobs/123"),
            ("greenhouse", "acme"),
        )

    def test_lever(self) -> None:
        self.assertEqual(
            extract_ats("https://jobs.lever.co/acme/123"),
            ("lever", "acme"),
        )

    def test_ashby(self) -> None:
        self.assertEqual(
            extract_ats("https://jobs.ashbyhq.com/acme/123"),
            ("ashby", "acme"),
        )

    def test_workable_and_smartrecruiters(self) -> None:
        self.assertEqual(
            extract_ats("https://apply.workable.com/acme/"),
            ("workable", "acme"),
        )
        self.assertEqual(
            extract_ats("https://jobs.smartrecruiters.com/Acme/123"),
            ("smartrecruiters", "Acme"),
        )

    def test_no_match(self) -> None:
        self.assertIsNone(extract_ats("https://example.com/careers"))

    def test_board_urls(self) -> None:
        page, api = board_url("greenhouse", "acme")
        self.assertEqual(page, "https://job-boards.greenhouse.io/acme")
        self.assertIn("boards-api.greenhouse.io/v1/boards/acme/jobs", api)
        page, api = board_url("lever", "acme", "eu")
        self.assertEqual(page, "https://jobs.eu.lever.co/acme")
        self.assertIn("api.eu.lever.co/v0/postings/acme", api)


if __name__ == "__main__":
    unittest.main()