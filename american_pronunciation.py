"""Conservative General American repairs using the CMU pronunciation dictionary."""

from __future__ import annotations

from difflib import SequenceMatcher
from functools import lru_cache
import re

import eng_to_ipa


VOWELS = re.compile(r"aɪ|aʊ|eɪ|oʊ|ɔɪ|[æɑɔəɛɪʊiuʌ]")
HOMOGRAPHS = frozenset(("read", "live", "lead", "record", "present", "object", "refuse",
    "content", "permit", "contract", "produce", "project", "invalid", "close", "tear",
    "wind", "minute", "resume", "refund"))


@lru_cache(maxsize=4096)
def pronunciations(word: str) -> tuple[str, ...]:
    result = []
    for phones in eng_to_ipa.get_cmu([word])[0]:
        if phones.startswith("__IGNORE__"):
            continue
        converted = eng_to_ipa.cmu_to_ipa([[phones]])[0][0]
        vowels = [phone for phone in phones.split() if phone[-1:].isdigit()]
        matches = list(VOWELS.finditer(converted))
        if len(vowels) != len(matches):
            continue
        # The library maps every AH to schwa; CMU stress distinguishes STRUT.
        for phone, match in reversed(list(zip(vowels, matches))):
            if phone in ("ah1", "ah2") and match.group() == "ə":
                converted = converted[:match.start()] + "ʌ" + converted[match.end():]
        result.append(converted.replace("ʧ", "tʃ").replace("ʤ", "dʒ"))
    return tuple(dict.fromkeys(result))


def _phonemes(value: str) -> str:
    return re.sub(r"[ˈˌː]", "", value).replace("ɹ", "r").replace("ɚ", "ər").replace("ɝ", "ər")


def dictionary_ipa(english: str, previous: str = "") -> str | None:
    words = re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*", english)
    old = previous.strip().strip("/").split()
    aligned = len(old) == len(words)
    tokens = []
    for index, word in enumerate(words):
        if word.isdigit() or (len(word) > 1 and word.isupper()):
            return None
        key = word.lower().replace("’", "'")
        choices = pronunciations(key)
        if not choices:
            return None
        original = old[index].rstrip(",.;!?") if aligned else ""
        if key in HOMOGRAPHS and len(choices) > 1:
            choices = tuple(choice for choice in choices if _phonemes(choice) == _phonemes(original))
            if not choices:
                return None
        chosen = max(choices, key=lambda choice: SequenceMatcher(None, choice, original).ratio())
        tokens.append(chosen)
    return "/" + " ".join(tokens) + "/" if tokens else None
