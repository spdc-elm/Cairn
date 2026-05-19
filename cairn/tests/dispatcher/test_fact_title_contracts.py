from __future__ import annotations

import unittest

from cairn.dispatcher.contracts import (
    validate_bootstrap_conclude_payload,
    validate_bootstrap_execute_payload,
    validate_explore_payload,
)


class FactTitleContractTests(unittest.TestCase):
    def test_explore_accepts_title_and_legacy_description_only(self) -> None:
        kind, fact = validate_explore_payload(
            {"accepted": True, "data": {"title": "Open Port", "description": "port 80 is open"}}
        )
        self.assertEqual(kind, "fact")
        self.assertEqual(fact["title"], "Open Port")
        self.assertEqual(fact["description"], "port 80 is open")

        kind, legacy_fact = validate_explore_payload({"description": "legacy fact"})
        self.assertEqual(kind, "fact")
        self.assertIsNone(legacy_fact["title"])
        self.assertEqual(legacy_fact["description"], "legacy fact")

    def test_bootstrap_payloads_parse_fact_title(self) -> None:
        kind, data = validate_bootstrap_execute_payload(
            {
                "accepted": True,
                "data": {
                    "fact": {"title": "Solved", "description": "goal evidence found"},
                    "complete": {"description": "evidence satisfies goal"},
                },
            }
        )
        self.assertEqual(kind, "complete")
        self.assertEqual(data["fact_title"], "Solved")
        self.assertEqual(data["fact_description"], "goal evidence found")

        kind, fact = validate_bootstrap_conclude_payload(
            {"accepted": True, "data": {"fact": {"title": "Partial", "description": "partial evidence"}}}
        )
        self.assertEqual(kind, "fact")
        self.assertEqual(fact["title"], "Partial")
        self.assertEqual(fact["description"], "partial evidence")


if __name__ == "__main__":
    unittest.main()
