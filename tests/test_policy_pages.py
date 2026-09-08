from pathlib import Path
import unittest


class PolicyPagesTests(unittest.TestCase):
    def test_public_policy_pages_include_required_content(self) -> None:
        root = Path(__file__).parents[1]
        expected = {
            "terms.html": "Terms of Service",
            "privacy.html": "Privacy Notice",
            "refunds.html": "Refund Policy",
        }
        for filename, heading in expected.items():
            page = (root / "service" / "web" / filename).read_text(encoding="utf-8")
            self.assertIn(heading, page)
            self.assertIn("maorblumberg@gmail.com", page)

    def test_public_routes_and_homepage_footer_are_present(self) -> None:
        root = Path(__file__).parents[1]
        application = (root / "service" / "app.py").read_text(encoding="utf-8")
        for route in ('@application.get("/terms"', '@application.get("/privacy"', '@application.get("/refunds"'):
            self.assertIn(route, application)
        self.assertIn('href="/refunds">Refund Policy', application)

