import unittest

from american_pronunciation import dictionary_ipa, pronunciations


class AmericanPronunciationTests(unittest.TestCase):
    def test_stressed_strut_vowels_are_not_schwa(self):
        self.assertIn("nɑt", pronunciations("not"))
        self.assertIn("ˈkʌmfərtəbəl", pronunciations("comfortable"))
        self.assertIn("wʌts", pronunciations("what's"))

    def test_curly_apostrophes_and_dash_punctuation_do_not_fuse_words(self):
        ipa = dictionary_ipa("Let’s talk about something else—what’s new with your family?")
        self.assertEqual(len(ipa.strip("/").split()), 10)
        self.assertIn("ɛls wʌts", ipa)
        self.assertNotIn("ɒ", ipa)
        self.assertNotIn("duɪŋ", ipa)

    def test_unknown_words_initialisms_and_numbers_are_not_guessed(self):
        for line in ("My unknowncodexword is private.", "Show your ID.", "I need room 204."):
            with self.subTest(line=line):
                self.assertIsNone(dictionary_ipa(line))

    def test_homographs_need_an_attested_contextual_pronunciation(self):
        self.assertIsNone(dictionary_ipa("I read that."))
        self.assertIn("rɛd", dictionary_ipa("I read that.", "/aɪ rɛd ðæt/"))
        self.assertIn("rid", dictionary_ipa("I read that.", "/aɪ rid ðæt/"))


if __name__ == "__main__":
    unittest.main()
