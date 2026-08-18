import os
import tempfile
import unittest

import cards


def _item(word_en: str, sentence_en: str, purpose_id: int = 1) -> dict:
    return {
        "_purpose_id": purpose_id,
        "word_en": word_en,
        "word_ipa": "/tɛst/",
        "word_cn": "測試",
        "tips": "現場直接使用。",
        "sentence_en": sentence_en,
        "sentence_ipa": "/tɛst/",
        "sentence_cn": "測試句。",
    }


class ReferenceDeckTests(unittest.TestCase):
    def test_exact_reference_phrase_is_rejected(self):
        candidate = _item(
            "Leave enough to tie it back.",
            "Please leave it long enough for a ponytail.",
        )
        reference = dict(candidate, _source_deck="美髮沙龍_02")

        reason = cards._reference_duplicate_reason(candidate, [reference])

        self.assertIn("美髮沙龍_02", reason)

    def test_near_reference_sentence_is_rejected(self):
        candidate = _item(
            "Keep enough length.",
            "Please leave it long enough to tie it back.",
        )
        reference = _item(
            "Leave enough length.",
            "Please leave it long enough to tie back.",
        )

        self.assertIsNotNone(
            cards._reference_duplicate_reason(candidate, [reference])
        )

    def test_similar_short_words_are_not_false_duplicates(self):
        candidate = _item("Shade", "Could we make the color slightly darker?")
        reference = _item("Fade", "Please start the fade close to my ears.")

        self.assertIsNone(
            cards._reference_duplicate_reason(candidate, [reference])
        )

    def test_deck_path_accepts_absolute_xlsx_path(self):
        handle = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        handle.close()
        self.addCleanup(os.unlink, handle.name)

        self.assertEqual(
            cards._resolve_deck_path(handle.name),
            os.path.abspath(handle.name),
        )


class ContentGateTests(unittest.TestCase):
    def test_stylist_pain_point_requires_both_fields_to_be_questions(self):
        item = _item(
            "How much would you like off?",
            "Please take off five centimeters.",
        )

        rejected = cards._local_review_deck(
            "剪髮溝通",
            [item],
            ["聽懂髮型師詢問想剪掉多少長度"],
        )

        self.assertIn(0, rejected)
        self.assertIn("直接問句", rejected[0])

    def test_meta_learning_narration_is_invalid(self):
        item = _item(
            "My hair gets puffy when thinned.",
            "I need to mention my hair gets puffy when thinned.",
        )

        self.assertIn(
            "meta-learning narration must be replaced with the actual spoken line",
            cards._validation_issues(item),
        )

    def test_generation_topic_keeps_filename_label_separate_from_focus(self):
        result = cards._generation_topic("美髮沙龍_03", "只教剪壞補救")

        self.assertEqual(
            result,
            "美髮沙龍_03\n本集內容焦點與邊界：只教剪壞補救",
        )


if __name__ == "__main__":
    unittest.main()
