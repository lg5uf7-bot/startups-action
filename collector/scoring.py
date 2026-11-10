from __future__ import annotations

from .models import AtsBoard, CompanyRecord


def score_company(company: CompanyRecord, board: AtsBoard | None) -> int:
    """A 0-100 usefulness score, not a company-quality judgement.

    A company with only a verified ATS board still scores 30, because its jobs
    are useful even when its marketing metadata is thin.
    """
    score = 5  # a usable name is already present
    if company.website_url:
        score += 15
    if company.short_description:
        score += 15
    if company.founded_year:
        score += 5
    if company.headquarters:
        score += 5
    if company.logo_url:
        score += 5
    if company.industries:
        score += 5
    if company.last_funding_round or company.total_funding_usd:
        score += 15
    if company.total_funding_usd:
        score += 10
    if board:
        score += 25
    return min(score, 100)


def is_publishable(score: int, minimum: int) -> bool:
    return score >= minimum