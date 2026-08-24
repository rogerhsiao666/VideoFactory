#!/usr/bin/env python3
"""
cards.py — 詞彙卡片語料前置生成工具
輸入主題 → OpenAI 生成 → 輸出 cards/{topic}.xlsx
已做過的主題自動跳過，同一副牌內自動去重。

第二集可指定一個或多個參考牌組，讓痛點規劃、生成與審稿都避開舊內容：
python3 cards.py --topic "主題_02" --focus "本集痛點" --avoid "主題_01"

可先輸出結構化策劃檔供人工調整，再以同一份策劃生成：
python3 cards.py --topic "主題" --plan-only
python3 cards.py --topic "主題" --plan-file "cards/主題.plan.json"
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
CARDS_DIR       = os.path.join(BASE_DIR, "cards")
OUTPUT_DIR      = os.path.join(BASE_DIR, "output")
USED_WORDS_FILE = os.path.join(BASE_DIR, "used_words.json")
GENERATE_CHUNK  = 10
REFILL_CANDIDATE_MULTIPLIER = 3
DEFAULT_CARD_COUNT = 50
MAX_WORD_EN_WORDS = 8
MAX_SENTENCE_EN_WORDS = 14
MAX_TIPS_CHARS = 36
MAX_SENTENCE_CN_CHARS = 52
MAX_REVIEW_REPLACEMENTS = 8
REFERENCE_WORD_SIMILARITY = 0.88
REFERENCE_SENTENCE_SIMILARITY = 0.90
MAX_REFERENCE_CARDS_IN_PROMPT = 200
PLAN_VERSION = 2
PLAN_CANDIDATE_RATIO = 1.5
PLAN_MIN_EXTRA_CANDIDATES = 20
PLAN_MAX_CATEGORY_SHARE = 0.35
PLAN_MIN_CATEGORIES = 5
PLAN_FALLBACK_MIN_CATEGORIES = 3

os.makedirs(CARDS_DIR,  exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

OPENAI_KEYS = [k for k in [os.getenv("OPENAI_API_KEY"), os.getenv("OPENAI_API_KEY_2")] if k]
# Drafting and independent review use separate requests so retries stay practical.
CARD_MODEL = os.getenv("OPENAI_CARD_MODEL", "gpt-4o-mini")
PLAN_MODEL = os.getenv("OPENAI_PLAN_MODEL", "gpt-4o-mini")
REVIEW_MODEL = os.getenv("OPENAI_REVIEW_MODEL", "gpt-4o-mini")
DUPLICATE_REVIEW_MODEL = os.getenv("OPENAI_DUPLICATE_REVIEW_MODEL", REVIEW_MODEL)
REVIEW_MODE = os.getenv("CARD_REVIEW_MODE", "hybrid").strip().lower()
if REVIEW_MODE not in {"local", "hybrid", "ai", "off"}:
    raise ValueError("CARD_REVIEW_MODE 必須是 local、hybrid、ai 或 off")
ENABLE_PAIN_POINT_PLAN = os.getenv("OPENAI_PAIN_POINT_PLAN", "1").lower() not in {"0", "false", "no"}

HEADERS = ["id", "word_en", "word_ipa", "word_cn", "tips",
           "sentence_en", "sentence_ipa", "sentence_cn"]

FIELD_SPEC = """Return a JSON object with a single key "items" whose value is an array of objects.
Each object MUST have exactly these keys:
- "purpose_id" : the integer number of the assigned pain point in the approved blueprint
- "word_en"     : a high-utility English phrase or response the learner can actually say in the target situation; maximum 8 English words; avoid generic category labels
- "word_ipa"    : IPA pronunciation of the word/phrase, enclosed in forward slashes (e.g., "/tʃɑp ˈvɛdʒtəblz/")
- "word_cn"     : Traditional Chinese translation (繁體中文)
- "tips"        : 極短一句實戰提示，不要 emoji。指出何時說、如何選、店員可能怎麼問，或容易犯的錯；禁止字典式解釋和重複 word_cn。
- "sentence_en" : a natural line of maximum 14 English words that a customer/user or staff member would genuinely say in the exact target situation; not a textbook explanation
- "sentence_ipa": full IPA pronunciation of the example sentence, enclosed in forward slashes (e.g., "/kæn juː hɛlp miː.../")
- "sentence_cn" : 台灣繁體中文口語意譯（非逐字翻譯）"""


def _call_openai(messages: list, **kwargs):
    from openai import RateLimitError, AuthenticationError, APIError
    if not OPENAI_KEYS:
        raise RuntimeError("未設定任何 OPENAI_API_KEY，請在 .env 補上金鑰")
    last_err = None
    for idx, key in enumerate(OPENAI_KEYS):
        try:
            client = OpenAI(api_key=key, timeout=90.0)
            return client.chat.completions.create(messages=messages, **kwargs)
        except (RateLimitError, AuthenticationError) as e:
            print(f"   ⚠️  OpenAI 金鑰 #{idx + 1} 無法使用（{type(e).__name__}），切換備用金鑰...")
            last_err = e
        except APIError as e:
            last_err = e
    raise last_err


def _normalize_key(word: str) -> str:
    if not word:
        return ""
    # Lowercase, strip punctuation, remove articles (a, an, the) to prevent duplicates like "Season meat" vs "Season the meat"
    w = word.lower().strip()
    w = re.sub(r'[.,!?\-_\'"]', '', w)
    w = re.sub(r'\b(the|a|an)\b', '', w)
    return ' '.join(w.split())


def _english_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*", text or ""))


def _validation_issues(item: dict) -> list[str]:
    issues: list[str] = []
    required_keys = ["word_en", "word_ipa", "word_cn", "tips", "sentence_en", "sentence_ipa", "sentence_cn"]
    for key in required_keys:
        val = item.get(key)
        if not val or not isinstance(val, str) or not val.strip():
            issues.append(f"missing {key}")

    if issues:
        return issues

    if any("\n" in item[key] or "\r" in item[key] for key in required_keys):
        issues.append("field contains a line break")

    word_count = _english_word_count(item["word_en"])
    sentence_count = _english_word_count(item["sentence_en"])
    if word_count > MAX_WORD_EN_WORDS:
        issues.append(f"word_en has {word_count}>{MAX_WORD_EN_WORDS} words")
    if sentence_count > MAX_SENTENCE_EN_WORDS:
        issues.append(f"sentence_en has {sentence_count}>{MAX_SENTENCE_EN_WORDS} words")
    if len(item["tips"].strip()) > MAX_TIPS_CHARS:
        issues.append(f"tips has {len(item['tips'].strip())}>{MAX_TIPS_CHARS} chars")
    if len(item["sentence_cn"].strip()) > MAX_SENTENCE_CN_CHARS:
        issues.append(f"sentence_cn has {len(item['sentence_cn'].strip())}>{MAX_SENTENCE_CN_CHARS} chars")
    allergy_text = f'{item["word_en"]} {item["sentence_en"]}'.lower()
    if re.search(r"\b(?:allergen|allergy|peanut|sesame|gluten)[ -]free\b", allergy_text):
        issues.append("unsafe allergy-free guarantee; ask about ingredients and cross-contact instead")
    if re.match(r"^(?:thank you|thanks)\b", item["word_en"].strip(), re.IGNORECASE):
        issues.append("generic gratitude is filler, not a topic-specific pain point")
    meta_text = f'{item["word_en"]} {item["sentence_en"]}'.lower()
    if re.search(
        r"\b(?:i understand you asked|are you asking if|could you ask me if|"
        r"i need to mention|i should mention|i need to ask|i should ask)\b",
        meta_text,
    ):
        issues.append("meta-learning narration must be replaced with the actual spoken line")
    
    # Check if word_ipa or sentence_ipa is just the English text (failed to generate IPA)
    word_en_clean = _normalize_key(item["word_en"])
    word_ipa_clean = _normalize_key(item["word_ipa"])
    if word_en_clean == word_ipa_clean:
        issues.append("word_ipa contains English spelling")
        
    sentence_en_clean = _normalize_key(item["sentence_en"])
    sentence_ipa_clean = _normalize_key(item["sentence_ipa"])
    if sentence_en_clean == sentence_ipa_clean:
        issues.append("sentence_ipa contains English spelling")
        
    if len(item["word_ipa"].strip()) < 2 or len(item["sentence_ipa"].strip()) < 2:
        issues.append("IPA is empty")
    if not item["word_ipa"].strip().startswith("/") or not item["word_ipa"].strip().endswith("/"):
        issues.append("word_ipa is not wrapped in slashes")
    if not item["sentence_ipa"].strip().startswith("/") or not item["sentence_ipa"].strip().endswith("/"):
        issues.append("sentence_ipa is not wrapped in slashes")

    return issues


def _is_valid_item(item: dict) -> bool:
    return not _validation_issues(item)


def _extract_generated_items(content: str) -> list[dict]:
    """Normalize the occasional singular response used for one-card requests."""
    payload = json.loads(content)
    if not isinstance(payload, dict):
        return []
    raw = payload.get("items")
    if raw is None:
        raw = payload.get("item", [])
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _generation_request_size(target_size: int, is_refill: bool) -> int:
    """Overproduce after a short batch so each missing card gets alternatives."""
    multiplier = REFILL_CANDIDATE_MULTIPLIER if is_refill else 1
    return target_size * multiplier


def _similarity_text(text: str) -> str:
    # Keep Unicode letters/numbers so Chinese pain-point plans do not collapse
    # into the same empty key during deduplication.
    return " ".join(re.findall(r"[^\W_]+", (text or "").casefold(), flags=re.UNICODE))


def _score_value(value, default: int = 3) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, 1), 5)


def _normalize_pain_point(value, index: int = 0) -> dict | None:
    """Normalize legacy text and structured planner output to one schema."""
    if isinstance(value, str):
        value = {"task": value}
    if not isinstance(value, dict):
        return None

    task = ""
    for key in ("task", "purpose", "pain_point", "description", "痛點"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            task = candidate.strip()
            break
    if not task:
        return None

    def clean(key: str, fallback: str = "") -> str:
        raw = value.get(key, fallback)
        return str(raw).strip() if raw is not None else fallback

    priority = _score_value(value.get("priority"))
    frequency = _score_value(value.get("frequency"))
    friction = _score_value(value.get("friction"))
    try:
        sequence = int(value.get("sequence", index + 1))
    except (TypeError, ValueError):
        sequence = index + 1

    point = {
        "category": clean("category", "未分類") or "未分類",
        "scenario": clean("scenario"),
        "speaker": clean("speaker", "使用者") or "使用者",
        "intent": clean("intent"),
        "task": task,
        "failure_mode": clean("failure_mode"),
        "priority": priority,
        "frequency": frequency,
        "friction": friction,
        "sequence": max(sequence, 1),
        "score": priority * 7 + frequency * 7 + friction * 6,
        "_source_index": index,
    }
    required_terms = value.get("required_terms", [])
    if isinstance(required_terms, str):
        required_terms = [required_terms]
    point["required_terms"] = (
        [str(term).strip() for term in required_terms if str(term).strip()][:6]
        if isinstance(required_terms, list)
        else []
    )
    return point


def _pain_point_task(point) -> str:
    normalized = _normalize_pain_point(point)
    return normalized["task"] if normalized else ""


def _pain_point_text(point) -> str:
    normalized = _normalize_pain_point(point)
    if not normalized:
        return ""
    parts = [
        f"分類={normalized['category']}",
        f"場景={normalized['scenario']}" if normalized["scenario"] else "",
        f"角色={normalized['speaker']}",
        f"意圖={normalized['intent']}" if normalized["intent"] else "",
        f"任務={normalized['task']}",
        f"失敗情況={normalized['failure_mode']}" if normalized["failure_mode"] else "",
    ]
    return "；".join(part for part in parts if part)


def _pain_point_semantic_key(point) -> tuple[str, ...]:
    normalized = _normalize_pain_point(point)
    if not normalized:
        return ()
    return tuple(
        _similarity_text(normalized[field])
        for field in ("category", "scenario", "speaker", "intent", "failure_mode")
    )


def _pain_points_semantically_duplicate(left, right) -> bool:
    left_point = _normalize_pain_point(left)
    right_point = _normalize_pain_point(right)
    if not left_point or not right_point:
        return False

    left_task = _similarity_text(left_point["task"])
    right_task = _similarity_text(right_point["task"])
    negative_markers = (
        "不", "不要", "拒絕", "取消", "無法", "不能", "沒有",
        " no ", " not ", " don't ", " without ", " refuse ", " cancel ",
    )
    left_padded = f" {left_task} "
    right_padded = f" {right_task} "
    left_negative = any(marker in left_padded for marker in negative_markers)
    right_negative = any(marker in right_padded for marker in negative_markers)
    if left_negative != right_negative:
        return False
    task_ratio = SequenceMatcher(None, left_task, right_task).ratio()
    left_key = _pain_point_semantic_key(left_point)
    right_key = _pain_point_semantic_key(right_point)
    comparable = [(a, b) for a, b in zip(left_key[1:], right_key[1:]) if a and b]
    matching_dimensions = sum(a == b for a, b in comparable)
    same_speaker = bool(left_key[2] and left_key[2] == right_key[2])
    same_intent = bool(left_key[3] and left_key[3] == right_key[3])
    same_core = (
        same_speaker
        and same_intent
        and bool(comparable)
        and matching_dimensions >= min(3, len(comparable))
    )
    scenario_speaker_intent = list(zip(left_key[1:4], right_key[1:4]))
    exact_core = all(a and b and a == b for a, b in scenario_speaker_intent)
    return (
        exact_core
        or (same_speaker and task_ratio >= 0.93)
        or (same_core and task_ratio >= 0.78)
    )


def _flatten_pain_point_candidates(value):
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, list):
        for child in value:
            yield from _flatten_pain_point_candidates(child)
        return
    if not isinstance(value, dict):
        return

    if any(key in value for key in ("task", "purpose", "pain_point", "description", "痛點")):
        yield value
        return
    for key in ("candidates", "pain_points", "items", "points", "tasks", "entries"):
        if key in value:
            yield from _flatten_pain_point_candidates(value[key])


def _select_pain_points(
    raw_points,
    count: int,
    require_categories: bool = False,
    minimum_categories: int | None = None,
) -> list[dict]:
    """Deduplicate, rank, cap category dominance, then restore journey order."""
    candidates: list[dict] = []
    generic_intents = {
        "詢問", "回答", "要求", "拒絕", "確認", "補救", "選擇", "聽懂問句",
    }
    for index, raw in enumerate(_flatten_pain_point_candidates(raw_points)):
        point = _normalize_pain_point(raw, index)
        if not point:
            continue
        if require_categories and (
            point["category"] == "未分類"
            or not point["scenario"]
            or not point["intent"]
            or _similarity_text(point["intent"]) in generic_intents
        ):
            continue
        if any(_pain_points_semantically_duplicate(point, old) for old in candidates):
            continue
        candidates.append(point)

    if len(candidates) < count:
        raise RuntimeError(f"痛點候選去重後僅有 {len(candidates)}/{count} 項")

    categories = {
        _similarity_text(point["category"])
        for point in candidates
        if point["category"] != "未分類"
    }
    category_target = (
        PLAN_MIN_CATEGORIES if minimum_categories is None else minimum_categories
    )
    required_categories = min(category_target, count)
    if require_categories and len(categories) < required_categories:
        raise RuntimeError(
            f"痛點候選只有 {len(categories)} 個有效分類，至少需要 {required_categories} 個"
        )

    ranked = sorted(
        candidates,
        key=lambda point: (-point["score"], point["sequence"], point["_source_index"]),
    )
    category_groups: dict[str, list[dict]] = {}
    for point in ranked:
        category_groups.setdefault(point["category"], []).append(point)

    selected: list[dict] = []
    selected_ids: set[int] = set()
    for group in sorted(category_groups.values(), key=lambda values: -values[0]["score"]):
        if len(selected) >= count:
            break
        point = group[0]
        selected.append(point)
        selected_ids.add(id(point))

    category_limit = max(1, math.ceil(count * PLAN_MAX_CATEGORY_SHARE))
    category_capacity = sum(
        min(len(group), category_limit) for group in category_groups.values()
    )
    if require_categories and len(category_groups) >= 3 and category_capacity < count:
        raise RuntimeError(
            "分類分布過度集中，無法在每類不超過 "
            f"{category_limit}/{count} 項的條件下完成策劃"
        )
    selected_counts = Counter(point["category"] for point in selected)
    for point in ranked:
        if len(selected) >= count:
            break
        if id(point) in selected_ids:
            continue
        if len(category_groups) >= 3 and selected_counts[point["category"]] >= category_limit:
            continue
        selected.append(point)
        selected_ids.add(id(point))
        selected_counts[point["category"]] += 1

    for point in ranked:
        if len(selected) >= count:
            break
        if id(point) not in selected_ids:
            selected.append(point)
            selected_ids.add(id(point))

    selected.sort(key=lambda point: (point["sequence"], point["_source_index"]))
    cleaned: list[dict] = []
    for index, point in enumerate(selected[:count], start=1):
        result = {key: value for key, value in point.items() if not key.startswith("_")}
        result["id"] = index
        cleaned.append(result)
    return cleaned


def _plan_summary(pain_points: list[dict]) -> dict:
    normalized = [_normalize_pain_point(point) for point in pain_points]
    counts = Counter(point["category"] for point in normalized if point)
    return {
        "categories": dict(sorted(counts.items())),
        "average_score": round(
            sum(point["score"] for point in normalized if point) / max(len(normalized), 1),
            1,
        ),
    }


def _save_pain_point_plan(topic: str, pain_points: list[dict], path: str) -> None:
    payload = {
        "version": PLAN_VERSION,
        "topic": topic,
        "count": len(pain_points),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": _plan_summary(pain_points),
        "pain_points": pain_points,
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_pain_point_plan(path: str, expected_count: int | None = None) -> list[dict]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_points = payload.get("pain_points", payload) if isinstance(payload, dict) else payload
    raw_count = len(list(_flatten_pain_point_candidates(raw_points)))
    count = expected_count if expected_count is not None else raw_count
    points = _select_pain_points(raw_points, count)
    if expected_count is not None and len(points) != expected_count:
        raise ValueError(f"策劃檔必須剛好有 {expected_count} 項痛點")
    return points


def _is_near_duplicate(candidate: dict, existing_items: list[dict]) -> bool:
    candidate_word = _similarity_text(candidate.get("word_en", ""))
    candidate_sentence = _similarity_text(candidate.get("sentence_en", ""))
    for existing in existing_items:
        word_ratio = SequenceMatcher(
            None, candidate_word, _similarity_text(existing.get("word_en", ""))
        ).ratio()
        sentence_ratio = SequenceMatcher(
            None, candidate_sentence, _similarity_text(existing.get("sentence_en", ""))
        ).ratio()
        # Keep materially different variants such as double meat vs double
        # cheese; the semantic reviewer handles true purpose duplication.
        if word_ratio >= 0.97 or sentence_ratio >= 0.98:
            return True
    return False


def _reference_duplicate_reason(
    candidate: dict,
    reference_items: list[dict] | None,
    candidate_point=None,
) -> str | None:
    """Return a reason when a card is too close to a previous deck."""
    if not reference_items:
        return None

    candidate_word = _similarity_text(candidate.get("word_en", ""))
    candidate_sentence = _similarity_text(candidate.get("sentence_en", ""))
    candidate_key = _normalize_key(candidate.get("word_en", ""))
    candidate_point = candidate_point or candidate.get("_pain_point")
    for reference in reference_items:
        reference_word = _similarity_text(reference.get("word_en", ""))
        reference_sentence = _similarity_text(reference.get("sentence_en", ""))
        reference_key = _normalize_key(reference.get("word_en", ""))
        source = reference.get("_source_deck", "上一集")

        reference_point = reference.get("_pain_point")
        if (
            candidate_point
            and reference_point
            and _pain_points_semantically_duplicate(candidate_point, reference_point)
        ):
            return (
                f"與 {source} 的場景、角色與意圖重複: "
                f"{_pain_point_task(reference_point)}"
            )

        if candidate_key and candidate_key == reference_key:
            return f"與 {source} 的短句重複: {reference.get('word_en', '')}"

        word_ratio = SequenceMatcher(None, candidate_word, reference_word).ratio()
        sentence_ratio = SequenceMatcher(
            None, candidate_sentence, reference_sentence
        ).ratio()
        similar_word = (
            min(len(candidate_word), len(reference_word)) >= 8
            and word_ratio >= REFERENCE_WORD_SIMILARITY
        )
        similar_sentence = (
            min(len(candidate_sentence), len(reference_sentence)) >= 12
            and sentence_ratio >= REFERENCE_SENTENCE_SIMILARITY
        )
        if similar_word or similar_sentence:
            return (
                f"與 {source} 的卡片過度相似: "
                f"{reference.get('word_en', '')}"
            )
    return None


def _reference_prompt_note(reference_items: list[dict] | None) -> str:
    """Build a bounded prior-deck summary for planning and generation prompts."""
    if not reference_items:
        return ""

    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for item in reference_items:
        word = str(item.get("word_en", "")).strip()
        sentence = str(item.get("sentence_en", "")).strip()
        key = (_normalize_key(word), _similarity_text(sentence))
        if not word or key in seen:
            continue
        seen.add(key)
        source = item.get("_source_deck", "上一集")
        pain_point = _pain_point_text(item.get("_pain_point"))
        purpose_note = f" | 策劃={pain_point}" if pain_point else ""
        lines.append(f"- [{source}] {word} | {sentence}{purpose_note}")
        if len(lines) >= MAX_REFERENCE_CARDS_IN_PROMPT:
            break

    if not lines:
        return ""
    return (
        "\n以下是上一集或指定參考牌組已教過的內容。"
        "不得重出相同短句、同義改寫，或說話角色、意圖、答案都相同的卡片；"
        "只有確實解決不同現場任務時才能沿用相關領域詞彙：\n"
        + "\n".join(lines)
        + "\n"
    )


def _local_review_deck(
    topic: str,
    items: list[dict],
    pain_points: list | None = None,
    reference_items: list[dict] | None = None,
) -> dict[int, str]:
    """Fast deterministic checks for rules that do not need model judgment."""
    rejected: dict[int, str] = {}
    seen_items: list[dict] = []
    seen_purpose_ids: set[int] = set()
    topic_text = _similarity_text(topic)
    english_stopwords = {
        "a", "an", "and", "are", "be", "can", "could", "do", "does",
        "for", "get", "have", "how", "i", "if", "in", "is", "it",
        "me", "my", "of", "on", "or", "please", "the", "this", "to",
        "want", "what", "which", "with", "would", "you", "your",
    }
    staff_question_markers = (
        "聽懂店員問", "聽懂店員詢問", "聽懂對方問", "店員會問", "對方會問",
        "聽懂髮型師問", "聽懂髮型師詢問", "髮型師會問",
        "聽懂服務人員問", "聽懂服務人員詢問", "服務人員會問",
    )
    low_value_patterns = {
        r"\bfresh ingredients?\b": "詢問食材是否新鮮屬低資訊填充",
        r"\bseasonal (?:drink|flavor)\b": "季節限定內容偏離核心任務",
        r"\bcustomer loyalty\b": "customer loyalty 是泛用填充概念",
        r"\bdaily routine\b": "daily routine 是泛用填充概念",
        r"\bhave a nice day\b": "泛用寒暄不是主題痛點",
    }
    grammar_patterns = {
        r"\bis\b[^?.!]*\bavailable and (?:an? )?(?:extra )?charge\b": "缺少必要動詞",
        r"\bfor to go\b": "應使用 to go 或 for takeout",
        r"\bpay with card\b": "付款方式應使用 pay by card 或 pay with a card",
    }

    for idx, item in enumerate(items):
        issues = _validation_issues(item)
        if issues:
            rejected[idx] = ", ".join(issues)
            continue

        if _is_near_duplicate(item, seen_items):
            rejected[idx] = "與前面卡片近乎逐字重複"
            continue

        candidate_point = item.get("_pain_point")
        if pain_points:
            try:
                candidate_purpose_id = int(item.get("_purpose_id"))
            except (TypeError, ValueError):
                candidate_purpose_id = 0
            if 1 <= candidate_purpose_id <= len(pain_points):
                candidate_point = pain_points[candidate_purpose_id - 1]
        reference_reason = _reference_duplicate_reason(
            item, reference_items, candidate_point
        )
        if reference_reason:
            rejected[idx] = reference_reason
            continue
        seen_items.append(item)

        combined = f'{item.get("word_en", "")} {item.get("sentence_en", "")}'.lower()
        assigned_point = ""
        if pain_points:
            try:
                purpose_id = int(item.get("_purpose_id"))
            except (TypeError, ValueError):
                rejected[idx] = "缺少有效 purpose_id"
                continue
            if not 1 <= purpose_id <= len(pain_points):
                rejected[idx] = f"purpose_id={purpose_id} 超出痛點藍圖"
                continue
            if purpose_id in seen_purpose_ids:
                rejected[idx] = f"purpose_id={purpose_id} 重複"
                continue
            seen_purpose_ids.add(purpose_id)
            assigned_point = pain_points[purpose_id - 1]
            assigned_point_text = _pain_point_text(assigned_point)
            assigned_point_task = _pain_point_task(assigned_point)

            if any(
                marker in f"{assigned_point_task} {assigned_point_text}"
                for marker in staff_question_markers
            ):
                if (
                    "?" not in item.get("word_en", "")
                    or "?" not in item.get("sentence_en", "")
                ):
                    rejected[idx] = "指定為對方問句，但 word_en 與 sentence_en 未同時使用直接問句"
                    continue

            normalized_point = _normalize_pain_point(assigned_point)
            explicit_terms = normalized_point.get("required_terms", []) if normalized_point else []
            required_tokens = [term.casefold() for term in explicit_terms]
            if not required_tokens:
                required_tokens = [
                    token.casefold()
                    for token in re.findall(r"[A-Za-z][A-Za-z-]+", assigned_point_task)
                    if token.casefold() not in english_stopwords
                ]
            if required_tokens:
                matched = sum(token in combined for token in required_tokens)
                if matched < math.ceil(len(required_tokens) / 2):
                    rejected[idx] = (
                        "未涵蓋痛點指定英文關鍵詞: " + ", ".join(required_tokens)
                    )
                    continue

        context = f"{topic_text} {_similarity_text(_pain_point_text(assigned_point))}"
        for pattern, reason in low_value_patterns.items():
            match = re.search(pattern, combined)
            if match and _similarity_text(match.group(0)) not in context:
                rejected[idx] = reason
                break
        if idx in rejected:
            continue

        for pattern, reason in grammar_patterns.items():
            if re.search(pattern, combined):
                rejected[idx] = reason
                break

    return rejected


def _plan_pain_points(
    topic: str,
    count: int,
    reference_items: list[dict] | None = None,
) -> list[dict]:
    """Create and rank an oversized content blueprint before writing cards."""
    if not ENABLE_PAIN_POINT_PLAN:
        return []

    candidate_count = max(
        count + PLAN_MIN_EXTRA_CANDIDATES,
        math.ceil(count * PLAN_CANDIDATE_RATIO),
    )
    reference_note = _reference_prompt_note(reference_items)
    prompt = f"""你是台灣成人情境英語課程的內容企劃。主題是「{topic}」。
先不要寫英文詞卡。請建立 {candidate_count} 個候選「溝通痛點」，系統會評分選出 {count} 個。
{reference_note}

每個痛點必須：
1. 描述使用者在現場某一刻需要聽懂、回答、詢問、選擇或補救的單一任務。
2. 具體到能指導下一位編輯寫出一句可直接開口的英文，不能只是「學習麵包單字」等寬泛分類。
3. 與其他痛點的說話角色、意圖或答案至少一項不同。純同義改寫仍算重複，
   但店員問句與顧客回答、肯定與否定、一般要求與有實際差異的客製要求可以分開教。
4. 優先處理高頻、高摩擦、容易說錯或聽不懂的情境，不要用寒暄、餐具、包裝小事等內容湊數。
5. 品牌品項或規定若可能因地區而異，痛點必須設計為「現場確認」，不可預設一定有。
6. 使用者在主題中明確點名的例子、疑問或需求必須優先納入。
7. 放入 5 至 8 個貼合主題的 category，涵蓋完整流程、主要決策及出錯補救；不可用「其他」湊分類。

必須輸出剛好 {candidate_count} 個候選。可把完整流程拆成不同決策、對方問句與使用者回答，
但不得用同一句型替換品項來湊數。若主題文字已列出必教項目，必須逐一保留。
同一步驟中「對方會怎麼問」和「使用者怎麼回答」可以分成兩個痛點，因為學習任務不同。
禁止用詢問是否新鮮、季節限定飲料、泛稱健康／快速／經典選擇、寒暄或道謝補足數量。

每個候選輸出以下欄位：
- category：主題專屬分類，例如開始、選擇、確認、補救等更具體的名稱。
- scenario：發生地點或流程節點，必須具體。
- speaker：真正說話的人，例如使用者、店員、路人、醫師。
- intent：單一但具體的溝通意圖，必須寫明答案、要求或結果，例如「回答使用信用卡付款」；禁止只填「詢問」「回答」「要求」。
- task：繁體中文的一句精確任務，足以指導編輯寫出現場原話。
- failure_mode：不會這句時最可能發生的具體問題；沒有則填空字串。
- priority、frequency、friction：各給 1 至 5 整數，5 代表最高價值、最高頻或最高摩擦。
- sequence：真實使用流程中的排序數字，越早發生數字越小。
- required_terms：只有英文必須包含特定關鍵詞時才填陣列，否則空陣列。

只輸出 JSON：{{"candidates": [{{"category": "...", "scenario": "...", "speaker": "...", "intent": "...", "task": "...", "failure_mode": "...", "priority": 5, "frequency": 5, "friction": 4, "sequence": 1, "required_terms": []}}]}}。
"""
    points: list[dict] | None = None
    fallback_points: list[dict] | None = None
    fallback_category_count = 0
    fallback_raw_count = 0
    raw_count = 0
    last_error: Exception | None = None
    retry_note = ""
    for attempt in range(3):
        raw_points = None
        kwargs = {
            "messages": [{"role": "user", "content": prompt + retry_note}],
            "model": PLAN_MODEL,
            "response_format": {"type": "json_object"},
        }
        if PLAN_MODEL.startswith("gpt-5"):
            kwargs["max_completion_tokens"] = 16000
        else:
            kwargs["max_tokens"] = 16000
            kwargs["temperature"] = 0.1
        try:
            response = _call_openai(**kwargs)
            payload = json.loads(response.choices[0].message.content)
            raw_points = payload.get("candidates", payload.get("pain_points", []))
            raw_count = len(list(_flatten_pain_point_candidates(raw_points)))
            if raw_count < candidate_count:
                raise RuntimeError(
                    f"僅產生 {raw_count}/{candidate_count} 個候選"
                )
            points = _select_pain_points(
                raw_points, count, require_categories=True
            )
            break
        except Exception as exc:
            last_error = exc
            if raw_points is not None:
                try:
                    relaxed_points = _select_pain_points(
                        raw_points,
                        count,
                        require_categories=True,
                        minimum_categories=PLAN_FALLBACK_MIN_CATEGORIES,
                    )
                    relaxed_category_count = len(
                        _plan_summary(relaxed_points)["categories"]
                    )
                    if relaxed_category_count > fallback_category_count:
                        fallback_points = relaxed_points
                        fallback_category_count = relaxed_category_count
                        fallback_raw_count = raw_count
                except Exception:
                    pass
            if attempt < 2:
                print(f"   ⚠️  痛點策劃未通過（第 {attempt + 1} 次）：{exc}，重新規劃...")
                retry_note = (
                    f"\n前次輸出未通過程式篩選：{exc}。"
                    "這次務必輸出完整數量、至少五個實質不同的有效分類，"
                    "並避免相同場景、角色與意圖的同義改寫。\n"
                )

    if points is None:
        if fallback_points is not None:
            points = fallback_points
            raw_count = fallback_raw_count
            print(
                f"   ⚠️  五類品質門檻連續 3 次未通過；已採用最佳備選"
                f"（{fallback_category_count} 類），且仍符合每類最多 "
                f"{math.ceil(count * PLAN_MAX_CATEGORY_SHARE)}/{count} 項的分布限制。"
            )
        else:
            print(f"   ❌ 痛點策劃未通過（第 3 次）：{last_error}")
    if points is None:
        raise RuntimeError(
            f"痛點規劃連續 3 次失敗，拒絕生成未經策劃的牌組: {last_error}"
        ) from last_error

    summary = _plan_summary(points)
    category_text = "、".join(
        f"{category} {amount} 項"
        for category, amount in summary["categories"].items()
    )
    print(
        f"   🧭 已從 {raw_count} 個候選選出 {len(points)} 個痛點"
        f"（平均 {summary['average_score']} 分；{category_text}）"
    )
    return points


def _build_prompt(topic: str, count: int, pain_points: list | None = None) -> str:
    blueprint = ""
    if pain_points:
        blueprint = (
            "\n以下是已核定的內容藍圖。每個痛點只能對應一張卡，必須全部涵蓋，"
            "並依此順序輸出；不得自行增加藍圖外內容：\n"
            + "\n".join(
                f"{idx + 1}. {_pain_point_text(point)}"
                for idx, point in enumerate(pain_points)
            )
            + "\n"
        )
    return f"""你是為台灣成人設計情境英語內容的資深編輯。這副牌的精確主題是：
「{topic}」
{blueprint}

整副牌預計 {count} 張。先在心中完成以下判斷，再生成內容，不要輸出分析過程：
- 鎖定這個主題唯一、最具體的場景與使用者身分；品牌、場所或專有名詞不得套用其他同名含義。
- 找出使用者為了完成這件事，最常遇到、最容易卡住、最怕聽不懂或說錯的具體時刻。
- 按真實流程排序思考：開始、關鍵選擇、店員/對方追問、客製需求、確認、付款或收尾、出錯補救。

內容配額（整副牌及每一批輸出都要盡量維持）：
1. 約 30% 是對方最常直接問使用者、使用者必須立刻聽懂的原話。
2. 約 30% 是使用者完成當下這一步所需的簡短回答或要求。
3. 約 20% 是高摩擦選擇：不知道選項差異、不知道怎麼回答、容易點錯或漏講的地方。
4. 約 20% 是臨場應對：聽不懂、要求重複、更改、缺貨、價格、限制、確認與補救。

每一項都必須符合：
1. word_en 優先使用可直接開口的短句、問句、回答或高頻搭配，不要用寬泛名詞湊數。
2. sentence_en 必須是該場景中真人會直接說出口的話。禁止第三人稱介紹、品牌宣傳、背景知識或教科書說明。
3. sentence_en 通常要展示比 word_en 更完整的使用方式，不能只是原句後面加 please。若 word_en 本身已是完整、自然且可直接使用的問句或回答，sentence_en 可以相同。
4. 必須具體到真實選項、決策或操作；看到這張卡，使用者要立刻知道它解決哪個痛點。
5. 優先選擇高頻、搜尋意圖高、最值得收藏的實戰內容，不追求冷門詞彙或表面多樣性。
6. 若商品、規定或選項可能因地區而異，改寫成現場確認的自然問句，不可捏造一定存在的品項。
7. tips 只寫一句極短實戰提醒：何時用、怎麼選、對方可能怎麼問，或台灣學習者常犯的錯。
8. 所有中文使用台灣繁體中文日常口語；sentence_cn 要自然意譯，不要逐字硬翻。
9. 必須為詞句生成精確完整的 IPA，並以斜線「/」包裹；絕對不能直接填英文拼寫。
10. 每張卡必須提供不同的實用學習價值。只換同義詞、句型與答案都相同才算重複；
    店員問句與顧客回答、肯定與否定、一般選擇與具體客製需求不是重複。
11. word_en 最多 {MAX_WORD_EN_WORDS} 個英文單字；sentence_en 最多 {MAX_SENTENCE_EN_WORDS} 個英文單字。這是硬性上限，不得超過。
12. 每張卡只處理一個溝通目的，最多帶兩個選擇或條件。完整流程必須拆成多張連續短卡，禁止塞成一個長句。
13. tips 最多 {MAX_TIPS_CHARS} 個中文字元；sentence_cn 最多 {MAX_SENTENCE_CN_CHARS} 個字元，避免卡片爆版。
14. 若 assigned pain point 是「聽懂店員／對方問句」，word_en 與 sentence_en 必須直接寫店員真的會說的原話；
    禁止寫 "I understand you asked..."、"Are you asking if..." 等學習旁白。

禁止內容：
- 主題的其他同名含義或相鄰但不屬於核心任務的場景。
- generic terms such as service, quality, safety, customer loyalty, daily routine, or other filler concepts unless the learner truly needs to say them on the spot.
- 只是在教一個簡單名詞怎麼念，卻沒有幫助使用者完成任務的內容。
- 看似相關但現場幾乎不會說的句子，以及同一句型只替換一個名詞的灌水項目。
- 寒暄、泛用道謝、詢問食材是否新鮮等低資訊句，除非它確實是該主題的主要痛點。
- 一口氣列出尺寸、品項、配料、醬料、付款等多個步驟的超長總結句。

品質範例（只示範具體程度，不代表一定要生成餐飲內容）：
- 差：word_en = "Honey mustard"；sentence_en = "I want honey mustard, please." 這只是在背品名。
- 好：word_en = "Just a little honey mustard"；sentence_en = "Just a little honey mustard, please, and no mayo." 它解決醬料用量和排除另一種醬的需求。
- 差：word_en = "Can I change it?"；沒有說要改什麼。
- 好：word_en = "Could I switch to...?"；sentence_en = "Could I switch to wheat bread before you toast it?" 它包含修改內容和時機。

輸出前逐項自我檢查：如果學習者不能在「{topic}」現場直接說、直接回答或立刻聽懂這一項，就刪掉並換成更實用的內容。
"""


def _ai_review_deck(
    topic: str,
    items: list[dict],
    pain_points: list | None = None,
    reference_items: list[dict] | None = None,
) -> dict[int, str]:
    """Use a separate model as a conservative editorial quality gate."""
    if not items:
        return {}

    compact_items = [
        {
            "id": f"{idx + 1:02d}",
            "purpose_id": item.get("_purpose_id", idx + 1),
            "assigned_pain_point": (
                _pain_point_text(pain_points[item.get("_purpose_id", idx + 1) - 1])
                if pain_points
                and isinstance(item.get("_purpose_id", idx + 1), int)
                and 1 <= item.get("_purpose_id", idx + 1) <= len(pain_points)
                else ""
            ),
            "word_en": item.get("word_en", ""),
            "tips": item.get("tips", ""),
            "sentence_en": item.get("sentence_en", ""),
            "sentence_cn": item.get("sentence_cn", ""),
        }
        for idx, item in enumerate(items)
    ]
    blueprint = "\n".join(
        f"{idx + 1}. {_pain_point_text(point)}"
        for idx, point in enumerate(pain_points or [])
    )
    blueprint_rule = ""
    if blueprint:
        blueprint_rule = f"""
這副牌必須逐項覆蓋以下核定痛點，每項剛好一張：
{blueprint}
若有卡片偏離藍圖、重複佔用同一痛點，或造成另一痛點缺漏，退回偏離或較低價值的卡片。
"""
    reference_rule = _reference_prompt_note(reference_items)

    prompt = f"""你是獨立的情境英語牌組審稿人。主題是「{topic}」。
請只退回明顯不合格的卡片，不要因為初學、句型相似或措辭可微調就退回。
{blueprint_rule}
{reference_rule}

明顯不合格的定義：
1. 套用主題的其他同名含義、偏離核心任務，或是泛用填充內容。
2. 現場幾乎不會說、無法幫助使用者完成核心任務，或只是孤立品名教學。
3. 與另一張卡的說話角色、意圖和答案都實質相同，只是改寫措辭。
4. 一張卡塞入超過兩個選擇／條件，應拆成多張短卡。
5. 明顯捏造品項、規定或事實；不確定的供應內容應使用詢問句。
6. 為了湊數加入低資訊邊角需求，例如泛用道謝、餐巾、餐具、切麵包邊、兒童份量、聯絡取餐等；除非主題明確指定。
7. 食品過敏卡直接要求「無某過敏原」或暗示店家能保證安全，而不是詢問成分及交叉接觸風險。
8. 具體商品、配料或服務未出現在主題提供的事實中，卻直接假設一定供應。若它是核心痛點，必須改成現場確認問句。
9. 卡片沒有完成 assigned_pain_point 指定的任務，或用別的痛點內容佔位。
10. 英文不合文法、不自然、指涉不清，或為了壓短而省略必要動詞，例如 "Is avocado available and extra charge?"。
11. assigned_pain_point 指定店員問句時，卡片卻改寫成顧客旁白或 "I understand you asked..." 等教學敘述。

不要審查字數、word_en 與 sentence_en 的關係或 IPA；這些由程式規則負責。

請以高訊號為原則：寧可退回低價值卡讓系統補寫，也不要為了湊滿數量放行邊角內容。

不要檢查 IPA。只輸出 JSON：
{{"reject": [{{"id": "01", "reason": "極短原因"}}]}}
若全部合格，輸出 {{"reject": []}}。

待審卡片：
{json.dumps(compact_items, ensure_ascii=False)}
"""
    try:
        request_kwargs = {
            "messages": [{"role": "user", "content": prompt}],
            "model": REVIEW_MODEL,
            "response_format": {"type": "json_object"},
        }
        if not REVIEW_MODEL.startswith("gpt-5"):
            request_kwargs["temperature"] = 0.1
        response = _call_openai(**request_kwargs)
        raw_rejects = json.loads(response.choices[0].message.content).get("reject", [])

        duplicate_prompt = f"""你只負責檢查英語牌組內及其與參考牌組之間的語意重複。主題是「{topic}」。
只有在兩張卡的「說話角色、當下意圖、實際答案」三者都相同，只是換同義詞或改寫措辭時，才退回其中一張。
例如「分開包裝」與「兩份分開包」、「外帶切半」與「切半方便分享」算重複。
以下都不算重複，必須保留：
- 店員問 "Cash or card?" 與顧客答 "I'll pay by card."
- 店員問蔬菜選擇與顧客回答不要洋蔥。
- "Toast it"、"Don't toast it"、"Lightly toasted"，因答案或客製結果不同。
- 詢問有哪些選項、確認特定品項是否供應、實際選定其中一項。
- 一般選擇與少量、不要、另外裝等結果不同的客製要求。
不同步驟、不同回答方向或解決不同錯誤也不算重複。
每組只保留較具體、較實用或編號較前的一張，退回真正的改寫複本。
參考牌組已經發布，不能退回參考牌組；若新卡與參考牌組重複，只退回新卡。
{reference_rule}
只輸出 JSON：{{"reject": [{{"id": "02", "reason": "與 01 溝通目的重複"}}]}}；沒有重複則輸出空陣列。
卡片：{json.dumps(compact_items, ensure_ascii=False)}
"""
        duplicate_kwargs = {
            "messages": [{"role": "user", "content": duplicate_prompt}],
            "model": DUPLICATE_REVIEW_MODEL,
            "response_format": {"type": "json_object"},
        }
        if not DUPLICATE_REVIEW_MODEL.startswith("gpt-5"):
            duplicate_kwargs["temperature"] = 0.1
        duplicate_response = _call_openai(**duplicate_kwargs)
        raw_rejects.extend(
            json.loads(duplicate_response.choices[0].message.content).get("reject", [])
        )
    except Exception as e:
        raise RuntimeError(f"自動審稿失敗，拒絕輸出未審核牌組: {e}") from e

    rejected: dict[int, str] = {}
    for entry in raw_rejects:
        try:
            idx = int(str(entry.get("id", "")).strip()) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(items):
            rejected[idx] = str(entry.get("reason", "未通過內容審查")).strip()
    return rejected


def _review_deck(
    topic: str,
    items: list[dict],
    pain_points: list | None = None,
    reference_items: list[dict] | None = None,
) -> dict[int, str]:
    """Run the configured local, AI, or hybrid deck review."""
    if REVIEW_MODE == "off" or not items:
        return {}
    if REVIEW_MODE == "ai":
        return _ai_review_deck(topic, items, pain_points, reference_items)

    local_rejected = _local_review_deck(
        topic, items, pain_points, reference_items
    )
    if local_rejected or REVIEW_MODE == "local":
        return local_rejected
    return _ai_review_deck(topic, items, pain_points, reference_items)


def _load_used_words() -> set[str]:
    if os.path.exists(USED_WORDS_FILE):
        try:
            with open(USED_WORDS_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def _save_used_words(words: set[str]):
    tmp = USED_WORDS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(words), f, ensure_ascii=False, indent=2)
    os.replace(tmp, USED_WORDS_FILE)


def _existing_topics() -> list[str]:
    files = glob.glob(os.path.join(CARDS_DIR, "*.xlsx"))
    return [os.path.splitext(os.path.basename(f))[0] for f in sorted(files)]


def _topic_to_slug(topic: str) -> str:
    return topic.strip().replace(" ", "_").replace("-", "_")


def generate(
    topic: str,
    count: int = DEFAULT_CARD_COUNT,
    seed_items: list | None = None,
    pain_points: list | None = None,
    reference_items: list[dict] | None = None,
) -> list[dict]:
    reference_items = list(reference_items or [])
    if pain_points is None:
        pain_points = _plan_pain_points(topic, count, reference_items)
    elif pain_points:
        pain_points = _select_pain_points(pain_points, count)
    if pain_points and len(pain_points) != count:
        raise ValueError(f"指定的痛點藍圖必須剛好有 {count} 項，目前為 {len(pain_points)} 項")
    # Each generation call receives only its missing blueprint slice below.
    # The complete blueprint is reserved for the final reviewer.
    prompt = _build_prompt(topic, count) + _reference_prompt_note(reference_items)
    used_words = _load_used_words()
    # Reusing a genuinely useful phrase across different topics is preferable to
    # filling a deck with obscure alternatives. Only dedupe within this deck.
    seen_normalized: set[str] = set()
    all_items: list = []

    for seed_idx, item in enumerate(seed_items or []):
        issues = _validation_issues(item)
        if issues:
            print(f"   ⚠️  移除未通過新版校驗的既有卡片 {item.get('word_en', 'Unknown')}: {', '.join(issues)}")
            continue
        if _is_near_duplicate(item, all_items):
            print(f"   ⚠️  移除近似重複的既有卡片: {item.get('word_en', 'Unknown')}")
            continue
        seed_point = pain_points[seed_idx] if pain_points and seed_idx < len(pain_points) else None
        reference_reason = _reference_duplicate_reason(
            item, reference_items, seed_point
        )
        if reference_reason:
            print(
                f"   ⚠️  移除與參考牌組重複的既有卡片 "
                f"{item.get('word_en', 'Unknown')}: {reference_reason}"
            )
            continue
        key = _normalize_key(item.get("word_en", ""))
        if item.get("word_en"):
            seeded_item = dict(item)
            if pain_points:
                seeded_item["_purpose_id"] = seed_idx + 1
                if seed_point:
                    seeded_item["_pain_point"] = seed_point
            all_items.append(seeded_item)
        if key:
            seen_normalized.add(key)

    # 過濾／去重後常會不足，持續補齊直到滿額；硬上限防止無限迴圈
    max_rounds = max(math.ceil(count / GENERATE_CHUNK) * 8 + 20, 40)
    rounds = 0
    consecutive_fails = 0
    empty_streak = 0
    review_replacements = 0
    rejected_examples: list[str] = []
    review_passed = False
    overgenerate_next_round = False

    while rounds < max_rounds:
        if len(all_items) >= count:
            all_items = all_items[:count]
            if pain_points:
                all_items.sort(key=lambda item: item.get("_purpose_id", count + 1))
            print(
                f"   🔎 {REVIEW_MODE} 審稿 #{review_replacements + 1}"
                f"（{len(all_items)} 張）..."
            )
            rejected = _review_deck(
                topic, all_items, pain_points, reference_items
            )
            if not rejected:
                print("      ✅ 自動審稿通過")
                review_passed = True
                break
            if review_replacements >= MAX_REVIEW_REPLACEMENTS:
                details = "; ".join(
                    f"{idx + 1:02d}: {reason}" for idx, reason in sorted(rejected.items())
                )
                raise RuntimeError(f"牌組連續未通過自動審稿，拒絕輸出: {details}")

            print(f"      ⚠️  退回 {len(rejected)} 張，開始自動補寫")
            for idx, reason in sorted(rejected.items()):
                print(f"         - {idx + 1:02d} {all_items[idx].get('word_en', '')}: {reason}")
            rejected_examples.extend(
                f'{all_items[idx].get("word_en", "")} | '
                f'{all_items[idx].get("sentence_en", "")} | reason: {reason}'
                for idx, reason in rejected.items()
            )
            all_items = [item for idx, item in enumerate(all_items) if idx not in rejected]
            seen_normalized = {
                _normalize_key(item.get("word_en", "")) for item in all_items if item.get("word_en")
            }
            review_replacements += 1
            overgenerate_next_round = True
            continue

        need = count - len(all_items)
        chunk_size = min(GENERATE_CHUNK, need)
        request_size = _generation_request_size(
            chunk_size, overgenerate_next_round
        )
        candidate_note = (
            f"，請求 {request_size} 個候選" if overgenerate_next_round else ""
        )
        print(
            f"   🔄 OpenAI #{rounds + 1}（目標 {chunk_size} 個{candidate_note}，"
            f"已有 {len(all_items)}/{count}）..."
        )

        # Prevent duplicates in the current deck. Historical decks are not hard
        # exclusions because common real-world phrases naturally cross topics.
        current_items = [
            f'{item.get("word_en", "")} | {item.get("sentence_en", "")}'
            for item in all_items
            if item.get("word_en")
        ]

        exclusion_note = ""
        if current_items:
            exclusion_note = (
                "\nThe following cards are already in this deck. Do not create a paraphrase "
                "with the same speaker, intent, and answer. A related staff question, customer "
                "answer, opposite answer, or materially different customization is allowed when "
                "the missing blueprint explicitly requires it:\n- "
                + "\n- ".join(current_items)
                + "\n"
            )
        if rejected_examples:
            exclusion_note += (
                "\nThese patterns were rejected by the independent editor. "
                "Do not recreate them:\n- "
                + "\n- ".join(rejected_examples[-30:])
                + "\n"
            )

        purpose_note = ""
        remaining: list[tuple[int, dict]] = []
        if pain_points:
            completed_purpose_ids = {
                item.get("_purpose_id") for item in all_items if item.get("_purpose_id")
            }
            remaining = [
                (idx + 1, point)
                for idx, point in enumerate(pain_points)
                if idx + 1 not in completed_purpose_ids
            ]
            if overgenerate_next_round:
                purpose_note = (
                    "\nGenerate alternative cards ONLY for these missing blueprint entries:\n"
                    + "\n".join(
                        f"{idx}. {_pain_point_text(point)}"
                        for idx, point in remaining[:chunk_size]
                    )
                    + f"\nReturn {REFILL_CANDIDATE_MULTIPLIER} materially different candidates "
                    "for EACH entry. Copy the entry number exactly into purpose_id for every "
                    "candidate. The system will validate them and keep one per entry.\n"
                )
            else:
                purpose_note = (
                    "\nGenerate cards ONLY for these missing blueprint entries. "
                    "Copy each number exactly into purpose_id and output one card per entry:\n"
                    + "\n".join(
                        f"{idx}. {_pain_point_text(point)}"
                        for idx, point in remaining[:chunk_size]
                    )
                    + "\n"
                )

        if overgenerate_next_round:
            generation_note = (
                f"\nReturn exactly {request_size} alternative candidate items in the items array. "
                f"Provide exactly {REFILL_CANDIDATE_MULTIPLIER} candidates for EACH of the "
                f"{chunk_size} missing purposes. Make every alternative a genuinely different "
                "natural line; the system will keep the first one per purpose that passes validation.\n"
            )
        else:
            generation_note = (
                f"\nGenerate exactly {request_size} NEW items with distinct communicative purposes.\n"
            )

        full_prompt = (
            prompt
            + exclusion_note
            + purpose_note
            + generation_note
            + FIELD_SPEC
        )
        try:
            request_kwargs = {
                "messages": [{"role": "user", "content": full_prompt}],
                "model": CARD_MODEL,
                "response_format": {"type": "json_object"},
            }
            if not CARD_MODEL.startswith("gpt-5"):
                # GPT-5 models currently only support their default temperature.
                request_kwargs["temperature"] = 0.55 if len(all_items) > 0 else 0.4
            resp = _call_openai(**request_kwargs)
            raw = _extract_generated_items(resp.choices[0].message.content)
            if not raw:
                print("      ⚠️ 模型未回傳可解析的 items 候選陣列")
        except Exception as e:
            print(f"      ⚠️ API 呼召失敗，等待 2 秒後重試: {e}")
            time.sleep(2)
            consecutive_fails += 1
            if consecutive_fails >= 3:
                print("   ⚠️  連續失敗 3 次，中斷生成。")
                break
            continue

        consecutive_fails = 0
        added = 0
        for item in raw:
            if len(all_items) >= count:
                break
            issues = _validation_issues(item)
            if issues:
                print(
                    f"      ⚠️ 跳過未通過校驗的項目 {item.get('word_en', 'Unknown')}: "
                    f"{', '.join(issues)}"
                )
                continue

            if pain_points:
                try:
                    purpose_id = int(item.get("purpose_id"))
                except (TypeError, ValueError):
                    print(f"      ⚠️ 跳過缺少有效 purpose_id 的項目: {item.get('word_en', 'Unknown')}")
                    continue
                completed_purpose_ids = {
                    existing.get("_purpose_id") for existing in all_items
                }
                if not 1 <= purpose_id <= len(pain_points):
                    print(f"      ⚠️ 跳過重複或越界 purpose_id={purpose_id}: {item.get('word_en', 'Unknown')}")
                    continue
                if purpose_id in completed_purpose_ids:
                    # 補寫模式本來就會為每個痛點回傳多個候選，其餘備選無須當作異常輸出。
                    if not overgenerate_next_round:
                        print(f"      ⚠️ 跳過重複 purpose_id={purpose_id}: {item.get('word_en', 'Unknown')}")
                    continue
                item["_purpose_id"] = purpose_id
                item["_pain_point"] = pain_points[purpose_id - 1]

            raw_word = item.get("word_en", "")
            key = _normalize_key(raw_word)
            if key and key not in seen_normalized:
                if _is_near_duplicate(item, all_items):
                    print(f"      ⚠️ 跳過近似重複項目: {raw_word}")
                    continue
                reference_reason = _reference_duplicate_reason(
                    item, reference_items, item.get("_pain_point")
                )
                if reference_reason:
                    print(f"      ⚠️ 跳過跨集重複項目 {raw_word}: {reference_reason}")
                    continue
                w = raw_word.strip()
                item["word_en"] = w[0].upper() + w[1:] if w else w

                # Format IPA columns to always have forward slashes
                for ipa_key in ["word_ipa", "sentence_ipa"]:
                    ipa_val = item[ipa_key].strip()
                    if not ipa_val.startswith("/"):
                        ipa_val = "/" + ipa_val
                    if not ipa_val.endswith("/"):
                        ipa_val = ipa_val + "/"
                    item[ipa_key] = ipa_val

                all_items.append(item)
                seen_normalized.add(key)
                added += 1

        rounds += 1
        overgenerate_next_round = added < chunk_size
        if added == 0:
            empty_streak += 1
            print(f"      ⚠️ 本輪 0 個新詞（連續空輪 {empty_streak}）")
            if empty_streak >= 8:
                print("   ⚠️  連續多輪無法補到新詞，停止補齊。")
                break
        else:
            empty_streak = 0
            print(f"      ✅ 本輪新增 {added} 個（累計 {len(all_items)}/{count}）")

    if len(all_items) < count:
        raise RuntimeError(f"僅生成 {len(all_items)}/{count} 張，未達數量與品質要求，拒絕輸出")

    if not review_passed:
        final_rejected = _review_deck(
            topic, all_items[:count], pain_points, reference_items
        )
        if final_rejected:
            details = "; ".join(
                f"{idx + 1:02d}: {reason}" for idx, reason in sorted(final_rejected.items())
            )
            raise RuntimeError(f"最終牌組未通過自動審稿，拒絕輸出: {details}")
    print(f"   ✅ 已補滿並通過審稿：{count} 張")

    all_items = all_items[:count]
    if pain_points:
        all_items.sort(key=lambda item: item.get("_purpose_id", count + 1))
    for i, item in enumerate(all_items):
        item["id"] = f"{i + 1:02d}"

    new_keys = {item["word_en"].lower().strip().strip(".,!?") for item in all_items if item.get("word_en")}
    _save_used_words(used_words | new_keys)
    return all_items


def load_xlsx_items(path: str) -> list[dict]:
    """讀取既有 xlsx 詞卡為 dict list。"""
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(h or "").strip() for h in rows[0]]
    items = []
    for row in rows[1:]:
        item = {}
        for i, h in enumerate(headers):
            if not h:
                continue
            val = row[i] if i < len(row) else ""
            item[h] = (val if val is not None else "")
            if isinstance(item[h], str):
                item[h] = item[h].strip()
        if item.get("word_en"):
            items.append(item)
    return items


def _resolve_deck_path(value: str) -> str:
    """Resolve an --avoid value as a path or a deck name in cards/."""
    raw = os.path.expanduser(value.strip())
    candidates = [raw]
    if not os.path.isabs(raw):
        candidates.extend([
            os.path.join(BASE_DIR, raw),
            os.path.join(CARDS_DIR, raw),
        ])

    expanded: list[str] = []
    for candidate in candidates:
        expanded.append(candidate)
        if not candidate.lower().endswith(".xlsx"):
            expanded.append(candidate + ".xlsx")

    for candidate in expanded:
        path = os.path.abspath(candidate)
        if os.path.isfile(path):
            return path
    raise FileNotFoundError(
        f"找不到參考牌組「{value}」。請提供 cards/ 內的牌組名稱或 XLSX 路徑"
    )


def _default_plan_path(xlsx_path: str) -> str:
    return os.path.splitext(os.path.abspath(xlsx_path))[0] + ".plan.json"


def _load_reference_decks(values: list[str]) -> tuple[list[dict], list[str]]:
    items: list[dict] = []
    paths: list[str] = []
    seen_paths: set[str] = set()
    for value in values:
        path = _resolve_deck_path(value)
        if path in seen_paths:
            continue
        seen_paths.add(path)
        paths.append(path)
        source = os.path.splitext(os.path.basename(path))[0]
        deck_items = load_xlsx_items(path)
        plan_path = _default_plan_path(path)
        reference_plan: list[dict] = []
        if os.path.isfile(plan_path):
            try:
                reference_plan = _load_pain_point_plan(
                    plan_path, expected_count=len(deck_items)
                )
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"參考牌組策劃檔無法讀取：{plan_path}: {exc}") from exc
        for index, item in enumerate(deck_items):
            reference = dict(item)
            reference["_source_deck"] = source
            if index < len(reference_plan):
                reference["_pain_point"] = reference_plan[index]
            items.append(reference)
    return items, paths


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必須是正整數") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("必須是正整數")
    return number


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成痛點導向情境英語 XLSX 牌組",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""範例：
  python3 cards.py --topic "美髮沙龍_03_剪壞補救" \\
    --focus "只教剪髮中要求暫停、確認與修正的現場溝通" \\
    --avoid "美髮沙龍" --avoid "美髮沙龍_02_剪髮溝通" \\
    --review hybrid

  # 先只產生策劃檔，人工檢查後再生成卡片
  python3 cards.py --topic "租車英文" --focus "取車、驗車、事故與還車" --plan-only
  python3 cards.py --topic "租車英文" --plan-file "cards/租車英文.plan.json"

不帶參數執行時，仍會進入原本的互動模式。
""",
    )
    parser.add_argument(
        "--topic",
        help="牌組名稱，也是預設輸出檔名（不含 .xlsx）",
    )
    parser.add_argument(
        "--focus",
        default="",
        help="本集內容焦點、使用者痛點與禁止範圍",
    )
    parser.add_argument(
        "--avoid",
        action="append",
        default=[],
        metavar="DECK",
        help="要避開的上一集牌組名稱或 XLSX 路徑，可重複使用",
    )
    parser.add_argument(
        "--count",
        type=_positive_int,
        default=DEFAULT_CARD_COUNT,
        help=f"卡片數量（預設 {DEFAULT_CARD_COUNT}）",
    )
    parser.add_argument(
        "--output",
        help="自訂 XLSX 輸出路徑；預設為 cards/{topic}.xlsx",
    )
    parser.add_argument(
        "--review",
        choices=("local", "hybrid", "ai", "off"),
        help=f"審稿模式（預設沿用環境設定，目前為 {REVIEW_MODE}）",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="只建立並保存痛點策劃 JSON，不生成卡片或 YouTube 描述",
    )
    parser.add_argument(
        "--plan-file",
        help="讀取人工審核或修改過的策劃 JSON；未指定時使用輸出檔旁的 .plan.json",
    )
    parser.add_argument(
        "--no-youtube",
        action="store_true",
        help="不要生成 youtube_{topic}.txt",
    )
    return parser


def _generation_topic(topic: str, focus: str = "") -> str:
    clean_focus = focus.strip()
    if not clean_focus:
        return topic
    return f"{topic}\n本集內容焦點與邊界：{clean_focus}"


def _chapter_time(seconds: float) -> str:
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m:02d}:{s:02d}"


def _parse_srt_starts(srt_path: str) -> list[float]:
    """讀取 SRT，回傳每條字幕的起始秒數。"""
    if not os.path.exists(srt_path):
        return []
    starts: list[float] = []
    with open(srt_path, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->", line)
            if m:
                h, mm, ss, ms = (int(x) for x in m.groups())
                starts.append(h * 3600 + mm * 60 + ss + ms / 1000.0)
    return starts


def _generate_yt_title(topic: str) -> str:
    prompt = (
        f"你是台灣 YouTube 英語教學頻道的標題撰稿人。"
        f"請為主題「{topic}」寫一句 YouTube 影片標題（繁體中文，25-40 字）。"
        f"風格：吸睛、有痛點、含具體場景與 Rayo 智慧閃卡關鍵字。"
        f"只輸出標題本身，不要引號、不要 hashtag、不要多行。"
    )
    try:
        resp = _call_openai(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4o-mini",
            temperature=0.9,
            max_tokens=80,
        )
        title = resp.choices[0].message.content.strip().strip('「」""\'\'')
        return title.splitlines()[0] if title else ""
    except Exception as e:
        print(f"⚠️  OpenAI 生成 YouTube 標題失敗 ({e})，使用預設模板")
        return f"【日常英文】{topic} 英文懶人包｜Rayo 智慧閃卡陪你 14 天上手"


def _generate_yt_topic_paragraph(topic: str) -> str:
    prompt = (
        f"你是一位台灣 YouTube 英語教學頻道的文案寫手。"
        f"請為主題「{topic}」寫一段 YouTube 影片描述（約 100-150 字繁體中文）。"
        f"格式要求：**第一句必須是一個以問號結尾的痛點/情境 hook**，"
        f"例如「暑假馬上就要飛了，卻發現英文還沒準備好？」的風格，"
        f"貼合「{topic}」情境。"
        f"接著的 2-3 句延續 hook，介紹本集內容（濃縮的實用情境／金句），"
        f"並明確提到搭配 Rayo 智慧閃卡與影子跟讀（Shadowing）練習。"
        f"風格活潑、有吸引力、貼近台灣觀眾口吻。"
        f"不要加標題、不要加 hashtag、不要加連結、不要換行，全部合成一段。"
    )
    try:
        resp = _call_openai(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4o-mini",
            temperature=0.9,
            max_tokens=400,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"⚠️  OpenAI 生成 YouTube 描述失敗 ({e})，使用預設模板")
        return (
            f"想學「{topic}」實用英文卻不知從何開始？"
            f"這集為你準備了最實用的「{topic}」英文懶人包，"
            f"專為沒時間準備的零基礎新手設計。"
            f"搭配 Rayo 智慧閃卡與影子跟讀（Shadowing），"
            f"每天只需幾分鐘，把最實用的金句印在腦海裡！"
        )


def _generate_yt_hashtags(topic: str) -> list[str]:
    prompt = (
        f"為 YouTube 英語教學影片主題「{topic}」生成 8-12 個 SEO 標籤，"
        f"涵蓋：繁體中文（如「{topic}英文、{topic}單字」等 2-4 個同義詞）、"
        f"簡體中文（2-3 個）、英文小寫（3-5 個，如「kitchen english、cooking vocabulary」風格）。"
        f'只輸出 JSON：{{"tags": ["tag1", "tag2", ...]}}，不要多餘文字。'
    )
    try:
        resp = _call_openai(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4o-mini",
            temperature=0.7,
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        tags = data.get("tags", [])
        cleaned = [t.strip() for t in tags if isinstance(t, str) and t.strip()]
        if cleaned:
            return cleaned
    except Exception as e:
        print(f"⚠️  OpenAI 生成主題 hashtags 失敗 ({e})，使用預設 fallback")
    return [f"{topic}英文", f"{topic} english"]


def _count_xlsx_rows(xlsx_path: str) -> int:
    """讀取現有 xlsx 的資料筆數（扣除 header 列）。"""
    try:
        wb = openpyxl.load_workbook(xlsx_path, read_only=True)
        ws = wb.active
        return max(0, ws.max_row - 1)
    except Exception:
        return 0


def write_youtube_description(topic: str, card_count: int, output_path: str):
    """產出 youtube_{topic}.txt。若對應的 SRT 已存在，四個進度時間戳從中讀取；否則用 00:00。"""
    slug = _topic_to_slug(topic)
    srt_path = os.path.join(OUTPUT_DIR, f"final_{slug.lower()}.srt")
    srt_starts = _parse_srt_starts(srt_path)

    def _srt_time(idx: int) -> str:
        if 0 <= idx < len(srt_starts):
            return _chapter_time(srt_starts[idx])
        return "00:00"

    ts_start = "00:00"
    if srt_starts:
        ts_25 = _srt_time(25)
        ts_50 = _srt_time(card_count)
        ts_75 = _srt_time(card_count + 25)
        print(f"📼 已找到 SRT，四個進度時間戳從字幕讀取")
    else:
        ts_25 = ts_50 = ts_75 = "00:00"
        print(f"ℹ️  未找到 {srt_path}，時間戳先用 00:00 佔位（跑完影片後可重新產生）")

    title = _generate_yt_title(topic)
    paragraph = _generate_yt_topic_paragraph(topic)
    topic_tags = _generate_yt_hashtags(topic)

    fixed_hashtags = ["英文學習", "日常對話", "14天挑戰", "英文口說", "影子跟讀", "英語教學"]
    hashtag_line = " ".join(f"#{t}" for t in fixed_hashtags)

    extended_tags = [
        "Rayo智慧閃卡", "間隔重複", "Shadowing",
        "英文學習", "英语学习", "learn english",
        "零基礎英文", "生活英文", "daily english", "english speaking practice",
    ]
    seen: set[str] = set()
    combined_tags: list[str] = []
    for t in extended_tags + list(topic_tags):
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        combined_tags.append(t)
    comma_line = ", ".join(combined_tags)

    lines = [
        title,
        "",
        paragraph,
        "",
        "👇 搭配 Rayo 智慧閃卡，學習效率翻倍 👇",
        "官網：https://rayo-ai.com/",
        "iOS App：https://rayo.pse.is/8ugjnq",
        "Chrome 插件：https://rayo.pse.is/8ughfh",
        "",
        f"{ts_start} 開始學習！",
        f"{ts_25} 25%繼續加油！",
        f"{ts_50} 50% 再複習一次  GO! GO!",
        f"{ts_75} 75% 最後衝刺！",
        "",
        "✅ 訂閱頻道並開啟小鈴鐺",
        "💬 在下方留言告訴我：你覺得最難開口的一句英文是什麼？",
        "",
        hashtag_line,
        "",
        comma_line,
    ]
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"✅ YouTube 描述: {output_path}")


def write_xlsx(items: list[dict], path: str):
    if not items:
        raise ValueError("拒絕輸出空牌組")

    deck_issues: list[str] = []
    accepted: list[dict] = []
    for idx, item in enumerate(items):
        expected_id = f"{idx + 1:02d}"
        if str(item.get("id", "")).strip() != expected_id:
            deck_issues.append(f"row {idx + 1}: id 應為 {expected_id}")
        for issue in _validation_issues(item):
            deck_issues.append(f"{expected_id}: {issue}")
        if _is_near_duplicate(item, accepted):
            deck_issues.append(f"{expected_id}: 與前面卡片近似重複")
        accepted.append(item)

    if deck_issues:
        raise ValueError("牌組未通過寫檔校驗: " + "; ".join(deck_issues))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cards"

    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    for col_idx, h in enumerate(HEADERS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for item in items:
        ws.append([item.get(h, "") for h in HEADERS])

    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=0)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 60)

    wb.save(path)


def main(argv: list[str] | None = None):
    global REVIEW_MODE

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    cli_mode = bool(raw_argv)
    parser = _build_cli_parser()
    args = parser.parse_args(raw_argv)
    if cli_mode and not args.topic:
        parser.error("非互動模式必須提供 --topic")
    if args.review:
        REVIEW_MODE = args.review

    existing = _existing_topics()
    if existing:
        print(f"✅ 已有主題: {', '.join(existing)}")
    print(f"🔎 牌組審稿模式: {REVIEW_MODE}")

    topic = (args.topic or input("\n📌 請輸入主題名稱: ")).strip()
    if not topic:
        print("⛔ 主題不能為空")
        return

    slug = _topic_to_slug(topic)
    if args.output:
        xlsx_path = os.path.abspath(os.path.expanduser(args.output))
        if not xlsx_path.lower().endswith(".xlsx"):
            xlsx_path += ".xlsx"
    else:
        xlsx_path = os.path.join(CARDS_DIR, f"{slug}.xlsx")
    os.makedirs(os.path.dirname(xlsx_path), exist_ok=True)
    yt_desc_path = os.path.join(
        os.path.dirname(xlsx_path),
        f"youtube_{os.path.splitext(os.path.basename(xlsx_path))[0]}.txt",
    )

    try:
        reference_items, reference_paths = _load_reference_decks(args.avoid)
    except (FileNotFoundError, RuntimeError) as exc:
        parser.error(str(exc))
    if os.path.abspath(xlsx_path) in reference_paths:
        parser.error("輸出牌組不能同時列在 --avoid 中")
    if reference_paths:
        names = ", ".join(
            os.path.splitext(os.path.basename(path))[0]
            for path in reference_paths
        )
        print(f"🚫 跨集排除: {names}（共 {len(reference_items)} 張）")

    generation_topic = _generation_topic(topic, args.focus)

    xlsx_exists    = os.path.exists(xlsx_path)
    yt_desc_exists = os.path.exists(yt_desc_path)

    if cli_mode:
        count = args.count
    else:
        raw_count = input(f"🔢 卡片數量（留空={DEFAULT_CARD_COUNT}）: ").strip()
        count = int(raw_count) if raw_count.isdigit() and int(raw_count) > 0 else DEFAULT_CARD_COUNT

    plan_path = (
        os.path.abspath(os.path.expanduser(args.plan_file))
        if args.plan_file
        else _default_plan_path(xlsx_path)
    )
    pain_points: list[dict] | None = None
    if os.path.isfile(plan_path):
        try:
            pain_points = _load_pain_point_plan(plan_path, expected_count=count)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            parser.error(f"策劃檔無法讀取：{plan_path}: {exc}")
        print(f"🗺️  已載入痛點策劃：{plan_path}")
    elif args.plan_file:
        parser.error(f"找不到 --plan-file：{plan_path}")
    elif args.plan_only or not xlsx_exists:
        pain_points = _plan_pain_points(generation_topic, count, reference_items)
        if not pain_points:
            parser.error("痛點策劃已停用，無法建立 plan 檔")
        _save_pain_point_plan(generation_topic, pain_points, plan_path)
        print(f"🗺️  已保存痛點策劃：{plan_path}")

    if args.plan_only:
        if pain_points is None:
            pain_points = _plan_pain_points(generation_topic, count, reference_items)
            _save_pain_point_plan(generation_topic, pain_points, plan_path)
        print(f"✅ 策劃完成，共 {len(pain_points)} 個痛點；依 --plan-only 停止")
        return

    if xlsx_exists:
        existing_items = load_xlsx_items(xlsx_path)
        have = len(existing_items)
        items = existing_items[:count]
        if pain_points:
            for index, item in enumerate(items):
                item["_purpose_id"] = index + 1
                item["_pain_point"] = pain_points[index]
        existing_rejected = (
            _review_deck(
                generation_topic,
                items,
                pain_points=pain_points,
                reference_items=reference_items,
            )
            if have >= count
            else {0: "數量不足"}
        )
        if have >= count and not existing_rejected:
            print(f"📄 「{topic}」已有 {have} 張且通過 {REVIEW_MODE} 審稿，跳過生成")
        else:
            print(f"📄 「{topic}」已有 {have} 張，開始補寫未通過項目...")
            if pain_points is None:
                pain_points = _plan_pain_points(
                    generation_topic, count, reference_items
                )
                _save_pain_point_plan(generation_topic, pain_points, plan_path)
                print(f"🗺️  已保存痛點策劃：{plan_path}")
            used_before = len(_load_used_words())
            items = generate(
                generation_topic,
                count,
                seed_items=items,
                pain_points=pain_points,
                reference_items=reference_items,
            )
            write_xlsx(items, xlsx_path)
            used_after = len(_load_used_words())
            print(f"\n✅ 已校驗並輸出 {len(items)} 個詞彙 → {xlsx_path}")
            print(f"📝 used_words.json 已更新（{used_before} → {used_after}）")

        if args.no_youtube:
            print("ℹ️  已依 --no-youtube 跳過 YouTube 描述")
        elif not yt_desc_exists:
            write_youtube_description(topic, len(items), yt_desc_path)
        else:
            print(f"⚠️  YouTube 描述已存在，跳過：{yt_desc_path}")
        return

    used_before = len(_load_used_words())
    print(f"\n🆕 開始生成「{topic}」({count} 個詞彙)...")
    items = generate(
        generation_topic,
        count,
        pain_points=pain_points,
        reference_items=reference_items,
    )

    if not items:
        print("❌ 未生成任何詞彙")
        return

    write_xlsx(items, xlsx_path)
    used_after = len(_load_used_words())
    print(f"\n✅ 已生成 {len(items)} 個詞彙 → {xlsx_path}")
    print(f"📝 used_words.json 已更新（{used_before} → {used_after}）")

    if args.no_youtube:
        print("ℹ️  已依 --no-youtube 跳過 YouTube 描述")
    else:
        write_youtube_description(topic, len(items), yt_desc_path)


if __name__ == "__main__":
    main()
