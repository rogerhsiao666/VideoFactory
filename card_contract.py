"""Shared content contract for newly generated flashcards."""

import re


MIN_CHUNK_WORDS = 2
MAX_CHUNK_WORDS = 6
MAX_WORD_EN_WORDS = 8
MAX_SENTENCE_EN_WORDS = 14
MAX_TIPS_CHARS = 36
CONTENT_CONTRACT_VERSION = 1
WORD_PATTERN = r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*"

CONTENT_RULES = """欄位契約（生成及審稿都必須遵守）：
- word_en 是 2–6 個英文單字的實用詞塊（Lexical Chunk）、動詞片語或關鍵慣用語，絕對不可是完整句子、問句或獨立命令句，也不可是單一單字。MAX_WORD_EN_WORDS = 8 是額外硬性上限，不是允許產出 7–8 詞。
- 不要寫 word_en="I need to escalate this to a supervisor."；請寫 "escalate this to a supervisor"，不加句末標點。
- sentence_en 是自然口語的完整對話句，最多 14 詞；必須合理應用 word_en，忽略大小寫與標點差異，允許必要的時態、單複數等輕微詞形變化（如 take charge → took charge）及合文法的可分離片語。不可只出現相似單字卻漏用或改變詞塊的核心意思。詞塊應用由 Tier 3 AI Review 判斷，不要求逐字連續相同。例句必須增加實際情境，不可與詞塊相同。
- 例句依現場情緒表達生氣、堅定或委婉，不要教科書、新聞報導或學習旁白。
- word_cn 只翻譯片語本身，不加主詞、請求語氣或例句條件；sentence_cn 翻譯完整句子的情境意譯。兩者必須不同，不能靠標點或空白製造假差異。
- tips 提供語感提示、情緒微調或文化背景，最多 36 個中文字元，不換行、不重複翻譯，不用「當…時，請…」或「當…時使用」等機器人模板。
- tips 範例：「語氣強硬，適合對方屢勸不聽時的最後通牒」。
"""


def english_tokens(text: str) -> list[str]:
    return re.findall(WORD_PATTERN, text.replace("’", "'").casefold())


def chunk_issues(text: str) -> list[str]:
    words = english_tokens(text)
    issues = []
    if len(words) > MAX_WORD_EN_WORDS:
        issues.append(f"word_en has {len(words)}>{MAX_WORD_EN_WORDS} words")
    if not MIN_CHUNK_WORDS <= len(words) <= MAX_CHUNK_WORDS:
        issues.append("word_en must be a 2-6 word lexical chunk")
    # These are provable sentence wrappers, not a general English grammar parser.
    if (re.search(r"[.!?;。！？；]", text)
            or re.match(r"^(?:i|you|we|he|she|it|they)\b", text.strip(), re.I)
            or re.match(r"^(?:this|that|there)(?:['’]s|\s+(?:is|are|was|were|has|have|will|can))\b", text.strip(), re.I)
            or re.match(r"^(?:can|could|would|will|do|does|did|is|are|am|have|has|should|may)\s+(?:i|you|we|he|she|it|they)\b", text.strip(), re.I)
            or re.match(r"^(?:what|when|where|why|how|who)\b.*\b(?:is|are|do|does|did|can|could|will|would)\b", text.strip(), re.I)
            or re.match(r"^please\b", text.strip(), re.I)):
        issues.append("word_en must not be a complete sentence or question")
    return issues


def english_field_issues(word_en: str, sentence_en: str) -> list[str]:
    issues = chunk_issues(word_en)
    chunk = english_tokens(word_en)
    sentence = english_tokens(sentence_en)
    # Inflection and separable phrases need linguistic judgment, not substring matching.
    # Tier 3 reviews actual chunk application; local checks only enforce field shape.
    if len(sentence) <= len(chunk):
        issues.append("sentence_en must be a complete contextual sentence, not word_en alone")
    return issues


def content_issues(item: dict) -> list[str]:
    issues = []
    if isinstance(item.get("word_en"), str) and isinstance(item.get("sentence_en"), str):
        issues.extend(english_field_issues(item["word_en"], item["sentence_en"]))
        sentence_count = len(english_tokens(item["sentence_en"]))
        if sentence_count > MAX_SENTENCE_EN_WORDS:
            issues.append(f"sentence_en has {sentence_count}>{MAX_SENTENCE_EN_WORDS} words")
    if any(isinstance(value, str) and ("\n" in value or "\r" in value) for value in item.values()):
        issues.append("field contains a line break")
    if isinstance(item.get("word_cn"), str) and isinstance(item.get("sentence_cn"), str):
        translations = [re.sub(r"[\W_]", "", item[field]) for field in ("word_cn", "sentence_cn")]
        if translations[0] == translations[1]:
            issues.append("sentence_cn must differ from word_cn: translate the whole contextual sentence")
    if isinstance(item.get("tips"), str):
        tip = item["tips"].strip()
        if len(tip) > MAX_TIPS_CHARS:
            issues.append(f"tips has {len(tip)}>{MAX_TIPS_CHARS} chars; 請精簡")
        plain_tip = re.sub(r"^(?:(?:委婉|中立|強硬)\s*[:：]\s*)+", "", tip)
        if re.match(r"^當.+時", plain_tip):
            issues.append("tips must give nuance, emotion or cultural context, not a 當…時 template")
        if (not re.search(r"[\u4e00-\u9fff]", tip)
                or re.fullmatch(r"(?:保持禮貌|要堅定|先要求報價|報價時使用)[。！!]?", plain_tip)):
            issues.append("tips must provide concrete nuance, emotion or cultural context, not generic advice")
    return issues
