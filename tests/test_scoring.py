from __future__ import annotations

import unittest

from collector.models import AtsBoard, CompanyRecord
from collector.scoring import score_company


class ScoringTests(unittest.TestCase):
    def test_ats_only_company_is_publishable(self) -> None:
        company = CompanyRecord(name="Acme")
        board = AtsBoard(
            provider="greenhouse",
            token="acme",
            board_url="https://job-boards.greenhouse.io/acme",
            api_url="https://boards-api.greenhouse.io/v1/boards/acme/jobs",
        )
        self.assertEqual(score_company(company, None), 5)
        self.assertEqual(score_company(company, board), 30)

    def test_rich_company(self) -> None:
        company = CompanyRecord(
            name="Acme",
            website_url="https://acme.com",
            short_description="A useful company.",
            founded_year=2020,
            headquarters="Paris",
            logo_url="https://acme.com/logo.png",
            industries=["Software"],
            last_funding_round="Seed",
            total_funding_usd=1_000_000,
        )
        self.assertGreaterEqual(score_company(company, None), 70)


if __name__ == "__main__":
    unittest.main()