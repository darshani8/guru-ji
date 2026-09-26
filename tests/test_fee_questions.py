import unittest

from app.agents.planner import DeterministicPlanner, Vocabulary, extract_entities
from app.domain.principals import PrincipalType
from app.normalization.spoken import normalize_spoken
from app.platform_tools.fees import inr
from platform_fixtures import PlatformFixture, principal


class SpokenFixTests(unittest.TestCase):
    def test_misheard_program_codes_and_fees_are_repaired(self):
        cases = {
            "Face collection": "fees collection",
            "Become degree students": "BCOM degree students",
            "b com students": "BCOM students",
            "B C A fees": "BCA fees",
            "financial era of 2024 2025 phases being connected": "financial year of 2024 2025 fees being collected",
        }
        for spoken, expected in cases.items():
            with self.subTest(spoken=spoken):
                self.assertEqual(normalize_spoken(spoken), expected)

    def test_ordinary_sentences_are_left_alone(self):
        for text in ("I love my face", "my face is connected", "face the exam", "phase 2 of the project", "become a doctor"):
            with self.subTest(text=text):
                self.assertEqual(normalize_spoken(text), text)


class AcademicYearTests(unittest.TestCase):
    def test_year_forms_become_a_label(self):
        vocab = Vocabulary(("BCA",), ())
        cases = {"fees in 2024-25": "2024-25", "2024 to 2025 fees": "2024-25", "24 to 25 fees": "2024-25", "fees 2024 2025": "2024-25", "fees for 2024": "2024-25"}
        for text, label in cases.items():
            with self.subTest(text=text):
                self.assertEqual(extract_entities(text, vocab).academic_year, label)

    def test_attendance_ranges_are_not_years(self):
        self.assertIsNone(extract_entities("students between 65 to 75 attendance", Vocabulary((), ())).academic_year)


class FeePlanTests(unittest.TestCase):
    def setUp(self):
        self.tools = PlatformFixture().registry.for_principal(principal(PrincipalType.PRINCIPAL))
        self.vocab = Vocabulary(("BBA", "BCA", "BCOM", "MBA"), ())

    def plan(self, text):
        plan = DeterministicPlanner().plan(text, self.tools, self.vocab)
        return plan.intent, {key: value for key, value in plan.steps[0].arguments.items() if value is not None} if plan.steps else {}

    def test_collection_questions_carry_program_and_year(self):
        self.assertEqual(self.plan("BCA fees collected in 2025-26"), ("fee_summary", {"program": "BCA", "academic_year": "2025-26"}))
        self.assertEqual(self.plan("24 to 25 Financial year is collected"), ("fee_summary", {"academic_year": "2024-25"}))
        self.assertEqual(self.plan("This collection of the BCA"), ("fee_summary", {"program": "BCA"}))
        self.assertEqual(self.plan("Face collection"), ("fee_summary", {}))

    def test_dues_questions_stay_pending(self):
        intent, args = self.plan("List BCA students with fee dues")
        self.assertEqual((intent, args["program"]), ("pending_fees", "BCA"))


class RupeeFormatTests(unittest.TestCase):
    def test_indian_grouping_and_scale(self):
        self.assertEqual(inr(16915300), "₹1,69,15,300 (1.69 crore)")
        self.assertEqual(inr(4564200), "₹45,64,200 (45.64 lakh)")
        self.assertEqual(inr(10000), "₹10,000")
        self.assertEqual(inr(0), "₹0")


if __name__ == "__main__":
    unittest.main()
