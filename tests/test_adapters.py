from __future__ import annotations

import unittest

from collector.ats.adapters import (
    normalize_ashby_row,
    normalize_greenhouse_row,
    normalize_lever_row,
    normalize_smartrecruiters_row,
    normalize_workable_row,
)


class AdapterTests(unittest.TestCase):
    def test_greenhouse(self) -> None:
        job = normalize_greenhouse_row(
            {
                "id": 1,
                "title": "Engineer",
                "absolute_url": "https://example.com/jobs/1",
                "location": {"name": "Remote"},
                "content": "&lt;p&gt;Hello&lt;/p&gt;",
                "metadata": [{"name": "Employment Type", "value": "Full-time"}],
                "first_published": "2026-01-01T00:00:00Z",
                "departments": [{"name": "Engineering"}],
            }
        )
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.external_id, "1")
        self.assertEqual(job.description, "<p>Hello</p>")
        self.assertEqual(job.description_text, "Hello")
        self.assertTrue(job.is_remote)
        self.assertEqual(job.employment_type, "full_time")
        self.assertEqual(job.department, "Engineering")

    def test_lever(self) -> None:
        job = normalize_lever_row(
            {
                "id": "abc",
                "text": "Designer",
                "categories": {
                    "location": "Berlin",
                    "team": "Design",
                    "commitment": "Full-time",
                },
                "descriptionPlain": "Hello",
                "createdAt": 1788199939703,
                "hostedUrl": "https://jobs.lever.co/acme/abc",
                "applyUrl": "https://jobs.lever.co/acme/abc/apply",
                "workplaceType": "hybrid",
            }
        )
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.external_id, "abc")
        self.assertEqual(job.location, "Berlin")
        self.assertEqual(job.department, "Design")
        self.assertIsNotNone(job.published_at)
        self.assertFalse(job.is_remote)

    def test_ashby(self) -> None:
        job = normalize_ashby_row(
            {
                "id": "x",
                "title": "PM",
                "location": "San Francisco",
                "department": "Product",
                "descriptionHtml": "<p>Hi</p>",
                "isRemote": True,
                "workplaceType": "Hybrid",
                "publishedAt": "2026-11-05T02:20:02Z",
                "jobUrl": "https://jobs.ashbyhq.com/openai/x",
                "applyUrl": "https://jobs.ashbyhq.com/openai/x/application",
                "employmentType": "FullTime",
            }
        )
        self.assertIsNotNone(job)
        assert job is not None
        self.assertTrue(job.is_remote)
        self.assertEqual(job.employment_type, "full_time")
        self.assertEqual(job.department, "Product")

    def test_workable(self) -> None:
        job = normalize_workable_row(
            {
                "id": "w1",
                "title": "Support",
                "location": {"city": "Paris", "country": "France"},
                "department": "Support",
                "employment_type": "Full-time",
                "url": "https://apply.workable.com/acme/j/w1",
                "application_url": "https://apply.workable.com/acme/j/w1/apply",
                "description": "<p>Help</p>",
                "published_on": "2026-01-01",
            }
        )
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.location, "Paris, France")

    def test_smartrecruiters(self) -> None:
        job = normalize_smartrecruiters_row(
            {
                "id": "s1",
                "name": "Data",
                "location": {"city": "Paris", "country": "France"},
                "typeOfEmployment": {"label": "Full-time"},
                "releasedDate": "2026-01-01",
                "jobAd": {
                    "sections": {
                        "jobDescription": {"text": "<p>Data</p>"}
                    }
                },
            },
            "Acme",
        )
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.external_id, "s1")
        self.assertEqual(job.job_url, "https://jobs.smartrecruiters.com/Acme/s1")

    def test_invalid_rows(self) -> None:
        self.assertIsNone(normalize_greenhouse_row({}))
        self.assertIsNone(normalize_lever_row({}))
        self.assertIsNone(normalize_ashby_row({}))


if __name__ == "__main__":
    unittest.main()