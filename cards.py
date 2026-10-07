#!/usr/bin/env python3
"""
cards.py — 詞彙卡片語料前置生成工具
輸入主題與主題描述 → OpenAI 生成 → 輸出 output/{topic}.xlsx（12 欄）
按句數分配真實情境（50 句分 10 情境），基礎／進階 60/40，同意思最多兩句。
--legacy 保留舊版 8 欄痛點流程；已完成教材只在內容核對一致時跳過。

第二集可指定一個或多個參考牌組，讓痛點規劃、生成與審稿都避開舊內容：
python3 cards.py --topic "主題_02" --description "本集痛點" --avoid "主題_01"

可先輸出結構化策劃檔供人工調整，再以同一份策劃生成：
python3 cards.py --topic "主題" --plan-only
python3 cards.py --topic "主題" --plan-file "output/主題.plan.json"
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import math
import os
import re
import sys
import time
from collections import Counter
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime
from difflib import SequenceMatcher

import openpyxl
import httpx
from openpyxl.styles import Font, PatternFill, Alignment
from openai import AsyncOpenAI, APIConnectionError, APIError, APITimeoutError, AuthenticationError
from curated_blueprints import get_curated_blueprint
from card_contract import (CONTENT_RULES, MAX_WORD_EN_WORDS, MAX_SENTENCE_EN_WORDS,
                           MAX_TIPS_CHARS, chunk_issues, content_issues)
from topic_rules import (configured_communication_intent, default_content_issues, default_rule_text,
                         get_topic_rules, matches_rule, topic_messages,
                         topic_rule_text, with_topic_rules)
from dotenv import load_dotenv
from prompt_toolkit import prompt as terminal_prompt
from prompt_toolkit.key_binding import KeyBindings

load_dotenv()

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
CARDS_DIR       = os.path.join(BASE_DIR, "cards")
OUTPUT_DIR      = os.path.join(BASE_DIR, "output")
USED_WORDS_FILE = os.path.join(BASE_DIR, "used_words.json")
GENERATE_CHUNK  = 10
REFILL_CANDIDATE_MULTIPLIER = 3
MAX_REFILL_CANDIDATE_MULTIPLIER = 6
DEFAULT_CARD_COUNT = 50
MAX_SENTENCE_CN_CHARS = 52
MAX_REVIEW_REPLACEMENTS = 8
REFERENCE_WORD_SIMILARITY = 0.88
REFERENCE_SENTENCE_SIMILARITY = 0.90
MAX_REFERENCE_CARDS_IN_PROMPT = 200
SEMANTIC_DUPLICATE_POLICY = """嚴格語意去重（整副牌共同遵守）：
- 先按「這句要求對方做什麼／確認什麼，以及得到什麼實際結果」分組，不按字面相似度分組。
- 同一溝通目的最多兩種說法：一種最簡單直白、一種稍微進階且自然的說法；不是每組都必須湊兩句。
- 不同 purpose_id、分類、場所、商品、文件名稱、禮貌程度或補充理由，不代表不同意思。
- 「要求書面報價／書面細節／詳細 quote／estimate／cost breakdown」若都是為了取得價格與明細，應合併為同一組；
  「停止未授權服務」、「拒絕不需要的推銷」、「要求簡單說明術語」、「要求時間考慮」也各自跨情境合併計數。
- 只有真正不同的行動或決策結果才可拆組，例如詢問總價、詢問額外費用是否存在、要求退還已扣款項。
  店員問句與顧客回答、肯定與否定、結果不同的客製要求仍是不同目的。
- 必須同時檢查 word_en 和 sentence_en；短句看似不同，但情境例句完成同一目的，仍納入同組。
- 不得用同義改寫、換商品或新增理由填補句數；多出的名額要留給尚未涵蓋的實際溝通目的。
"""
PLAN_VERSION = 7
PLAN_MIN_EXTRA_CANDIDATES = 20
PLAN_MAX_CATEGORY_SHARE = 0.35
PLAN_MIN_CATEGORIES = 5
PLAN_FALLBACK_MIN_CATEGORIES = 3
PLAN_MIN_COUNTERPART_SHARE = 0.20
PLAN_GENERATE_BATCH = 8

os.makedirs(OUTPUT_DIR, exist_ok=True)

OPENAI_KEYS = [k for k in [os.getenv("OPENAI_API_KEY"), os.getenv("OPENAI_API_KEY_2")] if k]
# Drafting and independent review use separate requests so retries stay practical.
CARD_MODEL = os.getenv("OPENAI_CARD_MODEL", "gpt-4o-mini")
PLAN_MODEL = os.getenv("OPENAI_PLAN_MODEL", "gpt-4o-mini")
REVIEW_MODEL = os.getenv("OPENAI_REVIEW_MODEL", "gpt-4o-mini")
PLAN_REVIEW_MODEL = os.getenv("OPENAI_PLAN_REVIEW_MODEL", REVIEW_MODEL)
DUPLICATE_REVIEW_MODEL = os.getenv("OPENAI_DUPLICATE_REVIEW_MODEL", REVIEW_MODEL)
REVIEW_MODE = os.getenv("CARD_REVIEW_MODE", "hybrid").strip().lower()
if REVIEW_MODE not in {"local", "hybrid", "ai", "off"}:
    raise ValueError("CARD_REVIEW_MODE 必須是 local、hybrid、ai 或 off")
ENABLE_PAIN_POINT_PLAN = os.getenv("OPENAI_PAIN_POINT_PLAN", "1").lower() not in {"0", "false", "no"}


def _positive_seconds(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 必須是大於零的秒數")
    return value


API_REQUEST_TIMEOUT = _positive_seconds("CARD_API_REQUEST_TIMEOUT", 90)
API_CALL_TIMEOUT = _positive_seconds("CARD_API_CALL_TIMEOUT", 180)
GENERATION_TIMEOUT = _positive_seconds("CARD_GENERATION_TIMEOUT", 900)
PROGRESS_INTERVAL = _positive_seconds("CARD_PROGRESS_INTERVAL", 15)
API_MAX_ATTEMPTS = 2
API_RETRY_DELAY = 2
_generation_deadline: ContextVar[float | None] = ContextVar("generation_deadline", default=None)


def _positive_integer(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} 必須是大於零的整數")
    return value


API_MAX_REQUESTS = _positive_integer("CARD_API_MAX_REQUESTS", 200)
API_TOKEN_BUDGET = _positive_integer("CARD_API_TOKEN_BUDGET", 450000)
_api_budget: ContextVar["APIBudget | None"] = ContextVar("api_budget", default=None)

HEADERS = ["id", "word_en", "word_ipa", "word_cn", "tips",
           "sentence_en", "sentence_ipa", "sentence_cn"]


class PlanVersionError(ValueError):
    """Raised when a saved plan predates the current quality contract."""


class GenerationTimeoutError(RuntimeError):
    """Stop all retries and fallbacks when the run exhausts its time budget."""


class CheckpointGenerationError(RuntimeError):
    """An expected generation failure with progress saved for resumption."""


class APIBudgetError(CheckpointGenerationError):
    """Stop before sending a request that would exceed the run's API budget."""


class APIBudget:
    def __init__(self, max_requests: int, token_limit: int):
        self.max_requests = max_requests
        self.token_limit = token_limit
        self.requests = 0
        self.tokens = 0

    def reserve(self, messages: list, kwargs: dict) -> int:
        # UTF-8 bytes conservatively bound text tokens, including JSON schemas.
        input_bound = len(json.dumps([messages, kwargs], ensure_ascii=False).encode("utf-8")) + 64 * (len(messages) + 1)
        output_bound = kwargs.get("max_completion_tokens", kwargs.get("max_tokens", 4096))
        reservation = input_bound + output_bound
        if self.requests >= self.max_requests or self.tokens + reservation > self.token_limit:
            raise APIBudgetError(
                f"本輪 API 預算已達上限：已請求 {self.requests}/{self.max_requests} 次，"
                f"已計入 {self.tokens}/{self.token_limit} tokens；下一次最多需 {reservation} tokens。"
                "已停止新請求並保留進度；相同輸入重跑可接續，不需 --force。"
            )
        self.requests += 1
        self.tokens += reservation
        return reservation

    def settle(self, reservation: int, response) -> None:
        usage = getattr(response, "usage", None)
        actual = getattr(usage, "total_tokens", None)
        if type(actual) is int and actual >= 0:
            self.tokens += actual - reservation


@contextmanager
def api_budget_scope():
    token = _api_budget.set(_api_budget.get() or APIBudget(API_MAX_REQUESTS, API_TOKEN_BUDGET))
    try:
        yield
    finally:
        _api_budget.reset(token)


def _progress(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def _check_generation_deadline() -> None:
    deadline = _generation_deadline.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise GenerationTimeoutError(
            f"生成流程已達總等待上限 {GENERATION_TIMEOUT:g} 秒，已停止請求與重試。"
            "已寫出的檔案會保留；可調整 CARD_GENERATION_TIMEOUT 後重跑。"
        )


class PainPointPlan(list):
    """List-compatible pain-point plan carrying its topic contract."""

    def __init__(self, values=(), *, contract: dict | None = None):
        super().__init__(values)
        self.contract = dict(contract or {})

FIELD_SPEC = """Return a JSON object with a single key "items" whose value is an array of objects.
Each object MUST have exactly these keys:
- "purpose_id" : the integer number of the assigned pain point in the approved blueprint
- "word_en"     : a practical 2-6 word lexical chunk, verb phrase or idiom; NEVER a complete sentence; hard maximum 8 words
- "word_ipa"    : IPA pronunciation of the word/phrase, enclosed in forward slashes (e.g., "/tʃɑp ˈvɛdʒtəblz/")
- "word_cn"     : 台灣繁體中文，只翻譯詞塊本身，不是整句話
- "tips"        : 36 字元內的語感、情緒微調或文化背景；不用「當…時，請…」模板，不重複翻譯
- "sentence_en" : a natural complete spoken sentence of maximum 14 words applying word_en naturally, allowing case/punctuation differences and mild inflections, with contextual emotion
- "sentence_ipa": full IPA pronunciation of the example sentence, enclosed in forward slashes (e.g., "/kæn juː hɛlp miː.../")
- "sentence_cn" : 台灣繁體中文整句情境意譯，必須不同於 word_cn""" + "\n" + CONTENT_RULES


async def _request_with_progress(client, messages: list, kwargs: dict, stage: str, timeout: float):
    started = time.monotonic()

    async def heartbeat():
        while True:
            await asyncio.sleep(PROGRESS_INTERVAL)
            _progress(f"{stage}：等待 API 回應，已 {time.monotonic() - started:.0f} 秒")

    reporter = asyncio.create_task(heartbeat())
    try:
        # A wall-clock limit also stops responses that trickle bytes forever.
        return await asyncio.wait_for(
            client.chat.completions.create(messages=messages, **kwargs), timeout=timeout
        )
    finally:
        reporter.cancel()
        await asyncio.gather(reporter, return_exceptions=True)


async def _call_openai_async(messages: list, stage: str, kwargs: dict, budget_seconds: float | None = None):
    _check_generation_deadline()
    started = time.monotonic()
    call_deadline = started + (budget_seconds if budget_seconds is not None else API_CALL_TIMEOUT)
    run_deadline = _generation_deadline.get()
    if run_deadline is not None:
        call_deadline = min(call_deadline, run_deadline)
    last_err = None
    for attempt in range(API_MAX_ATTEMPTS):
        _check_generation_deadline()
        remaining = call_deadline - time.monotonic()
        if remaining <= 0:
            last_err = APITimeoutError(
                request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
            )
            break
        key_index = min(attempt, len(OPENAI_KEYS) - 1)
        timeout = min(budget_seconds if budget_seconds is not None else API_REQUEST_TIMEOUT, remaining)
        budget = _api_budget.get()
        if budget is not None:
            if "max_tokens" not in kwargs and "max_completion_tokens" not in kwargs:
                kwargs = dict(kwargs)
                kwargs["max_completion_tokens" if str(kwargs.get("model", "")).startswith("gpt-5")
                       else "max_tokens"] = 4096
            reservation = budget.reserve(messages, kwargs)
        _progress(
            f"{stage}：開始請求 {attempt + 1}/{API_MAX_ATTEMPTS}，"
            f"模型 {kwargs.get('model', CARD_MODEL)}，金鑰 #{key_index + 1}，"
            f"本次上限 {timeout:.0f} 秒"
            + (f"；本輪 API {budget.requests}/{budget.max_requests} 次" if budget is not None else "")
        )
        try:
            async with AsyncOpenAI(
                api_key=OPENAI_KEYS[key_index], max_retries=0,
                timeout=httpx.Timeout(timeout, connect=min(10, timeout)),
            ) as client:
                response = await _request_with_progress(client, messages, kwargs, stage, timeout)
            if budget is not None:
                budget.settle(reservation, response)
            _check_generation_deadline()
            _progress(f"{stage}：API 回應完成，累計 {time.monotonic() - started:.1f} 秒")
            return response
        except asyncio.TimeoutError:
            last_err = APITimeoutError(
                request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
            )
        except APIError as exc:
            last_err = exc
        _check_generation_deadline()
        status = getattr(last_err, "status_code", None)
        _progress(
            f"{stage}：請求失敗（{type(last_err).__name__}"
            f"{f', HTTP {status}' if status else ''}）"
        )
        if isinstance(last_err, AuthenticationError):
            retryable = key_index + 1 < len(OPENAI_KEYS)
        else:
            retryable = (
                isinstance(last_err, APIConnectionError)
                or status in {408, 409, 429}
                or (status is not None and status >= 500)
            )
        if not retryable or attempt + 1 >= API_MAX_ATTEMPTS:
            break
        delay = min(API_RETRY_DELAY, max(0, call_deadline - time.monotonic()))
        if delay <= 0:
            break
        _progress(f"{stage}：{delay:g} 秒後重試，下一次金鑰 #{min(attempt + 1, len(OPENAI_KEYS) - 1) + 1}")
        await asyncio.sleep(delay)
    _check_generation_deadline()
    _progress(f"{stage}：API 呼叫失敗，累計 {time.monotonic() - started:.1f} 秒，停止本次呼叫")
    raise last_err


def _call_openai(messages: list, *, stage: str = "OpenAI", budget_seconds: float | None = None, **kwargs):
    if not OPENAI_KEYS:
        raise RuntimeError("未設定任何 OPENAI_API_KEY，請在 .env 補上金鑰")
    return asyncio.run(_call_openai_async(messages, stage, kwargs, budget_seconds))


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


def _validation_issues(item: dict, *, max_word_en_words: int | None = None,
                       sentence_ipa_required: bool = True) -> list[str]:
    issues: list[str] = []
    required_keys = ["word_en", "word_ipa", "word_cn", "tips", "sentence_en", "sentence_ipa", "sentence_cn"]
    if not sentence_ipa_required:
        required_keys.remove("sentence_ipa")
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
    word_limit = min(MAX_WORD_EN_WORDS, max_word_en_words) if max_word_en_words is not None else MAX_WORD_EN_WORDS
    if word_count > word_limit:
        issues.append(f"word_en has {word_count}>{word_limit} words")
    if sentence_count > MAX_SENTENCE_EN_WORDS:
        issues.append(f"sentence_en has {sentence_count}>{MAX_SENTENCE_EN_WORDS} words")
    if len(item["tips"].strip()) > MAX_TIPS_CHARS:
        issues.append(f"tips has {len(item['tips'].strip())}>{MAX_TIPS_CHARS} chars")
    if len(item["sentence_cn"].strip()) > MAX_SENTENCE_CN_CHARS:
        issues.append(f"sentence_cn has {len(item['sentence_cn'].strip())}>{MAX_SENTENCE_CN_CHARS} chars")
    issues.extend(issue for issue in content_issues(item) if issue not in issues)
    issues.extend(default_content_issues(f'{item["word_en"]} {item["sentence_en"]}'))
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
        
    if sentence_ipa_required and _normalize_key(item["sentence_en"]) == _normalize_key(item["sentence_ipa"]):
        issues.append("sentence_ipa contains English spelling")
    for field in ("word_ipa", "sentence_ipa") if sentence_ipa_required else ("word_ipa",):
        if len(item[field].strip()) < 2:
            issues.append("IPA is empty")
        if not item[field].strip().startswith("/") or not item[field].strip().endswith("/"):
            issues.append(f"{field} is not wrapped in slashes")

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
    """Overproduce refills, with a deeper pool for small stubborn gaps."""
    if not is_refill:
        return target_size
    multiplier = min(
        MAX_REFILL_CANDIDATE_MULTIPLIER,
        max(REFILL_CANDIDATE_MULTIPLIER, 24 // max(target_size, 1)),
    )
    return target_size * multiplier


def _required_focus_phrases(topic: str) -> list[str]:
    """Extract English lines from common 必教金句 description formats."""
    match = re.search(
        r"必教金句[：:]\s*(.*?)(?=。?內容邊界[：:]|$)",
        topic,
        flags=re.DOTALL,
    )
    if not match:
        phrases: list[str] = []
        for marker in re.finditer(r"必教(?!金句)", topic):
            section = topic[marker.end():]
            section = re.split(r"\n|\s+\d+[.)、]\s*", section, maxsplit=1)[0]
            quoted = re.findall(
                r'"([^"]+)"|“([^”]+)”|「([^」]+)」|『([^』]+)』',
                section,
            )
            for groups in quoted:
                candidate = next((value.strip() for value in groups if value), "")
                english_chars = len(re.findall(r"[A-Za-z]", candidate))
                visible_chars = len(re.sub(r"\s", "", candidate))
                if (
                    _english_word_count(candidate) >= 2
                    and english_chars >= max(1, math.ceil(visible_chars * 0.6))
                    and candidate not in phrases
                ):
                    phrases.append(candidate)
        return phrases
    section = match.group(1)
    parenthesized = []
    for raw in re.findall(r"[（(]([^()（）]*)[)）]", section):
        candidate = raw.strip()
        english_chars = len(re.findall(r"[A-Za-z]", candidate))
        visible_chars = len(re.sub(r"\s", "", candidate))
        if (
            _english_word_count(candidate) >= 2
            and english_chars >= max(1, math.ceil(visible_chars * 0.6))
        ):
            parenthesized.append(candidate)
    if parenthesized:
        return parenthesized
    section = re.sub(r"\s*[（(][^()（）]*[)）]", "", section)
    phrases: list[str] = []
    for raw in re.split(r"[；;\n]+", section):
        phrase = raw.strip().strip("「」『』\"。 ")
        english_chars = len(re.findall(r"[A-Za-z]", phrase))
        visible_chars = len(re.sub(r"\s", "", phrase))
        if (
            _english_word_count(phrase) >= 2
            and english_chars >= max(1, math.ceil(visible_chars * 0.6))
        ):
            phrases.append(phrase)
    return phrases


def _short_locked_phrase(sentence: str) -> str:
    if _english_word_count(sentence) <= MAX_WORD_EN_WORDS:
        return sentence
    clauses = re.split(r"\bbut\b", sentence, flags=re.IGNORECASE)
    for clause in reversed(clauses):
        candidate = clause.strip(" ,;:-")
        if candidate and _english_word_count(candidate) <= MAX_WORD_EN_WORDS:
            return candidate
    words = list(
        re.finditer(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*", sentence or "")
    )
    if len(words) > MAX_WORD_EN_WORDS:
        candidate = sentence[words[-MAX_WORD_EN_WORDS].start():].strip(" ,;:-")
        return candidate[0].upper() + candidate[1:] if candidate else candidate
    return sentence


def _exact_generation_point(point) -> dict | None:
    """Return a point with code-owned English when its wording is explicit."""
    normalized = _normalize_pain_point(point)
    if not normalized:
        return None
    if normalized.get("target_sentence"):
        if chunk_issues(normalized.get("target_phrase", "")):
            normalized["target_phrase"] = ""
        return normalized
    if normalized.get("role_type") != "counterpart_line":
        return None
    quote = _quoted_english_line(normalized.get("task", ""))
    if not quote:
        return None
    normalized["target_phrase"] = ""
    normalized["target_sentence"] = quote
    return normalized


def _apply_focus_phrase_locks(topic: str, pain_points: list[dict]) -> int:
    """Lock explicit must-teach lines onto their matching planned purposes."""
    locked = 0
    used_indexes: set[int] = set()
    for phrase in _required_focus_phrases(topic):
        phrase_key = _similarity_text(phrase)
        matched_index: int | None = None
        for index, point in enumerate(pain_points):
            task_key = _similarity_text(_pain_point_task(point))
            if phrase_key and phrase_key in task_key:
                matched_index = index
                break
        if matched_index is None:
            matched_index = next(
                (
                    index
                    for index, point in enumerate(pain_points)
                    if index not in used_indexes
                    and _normalize_pain_point(point).get("role_type") == "learner_line"
                ),
                None,
            )
        if matched_index is None:
            continue
        point = pain_points[matched_index]
        point["intent"] = f"逐字使用必教金句：{phrase}"
        point["job_key"] = f"必教金句：{phrase}"
        point["task"] = f"逐字說出必教金句：“{phrase}”"
        point["target_phrase"] = _short_locked_phrase(phrase)
        point["target_sentence"] = phrase
        used_indexes.add(matched_index)
        locked += 1
    return locked


def _topic_requests_learner_only(topic: str) -> bool:
    focus = topic.split("本集內容焦點與邊界：", 1)[-1].casefold()
    learner_marker = any(marker in focus for marker in ("學習者", "自己開口", "learner_line"))
    exclusion_marker = any(
        marker in focus
        for marker in ("不要收錄對方", "不教對方原話", "全部是", "全部由")
    )
    return learner_marker and exclusion_marker


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


def _normalize_topic_contract(value) -> dict:
    if not isinstance(value, dict):
        return {}

    def clean_text(key: str) -> str:
        raw = value.get(key, "")
        return str(raw).strip() if raw is not None else ""

    def clean_list(key: str) -> list[str]:
        raw = value.get(key, [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [str(item).strip() for item in raw if str(item).strip()][:12]

    return {
        "audience": clean_text("audience"),
        "core_pain": clean_text("core_pain"),
        "promised_transformation": clean_text("promised_transformation"),
        "in_scope": clean_list("in_scope"),
        "out_of_scope": clean_list("out_of_scope"),
        "required_moments": clean_list("required_moments"),
        "pain_categories": clean_list("pain_categories"),
        "learner_only": bool(value.get("learner_only", False)),
    }


def _topic_contract_issues(contract: dict) -> list[str]:
    normalized = _normalize_topic_contract(contract)
    issues = []
    for key in ("audience", "core_pain", "promised_transformation"):
        if not normalized[key]:
            issues.append(f"topic_contract 缺少 {key}")
    for key in ("in_scope", "out_of_scope", "required_moments"):
        if len(normalized[key]) < 3:
            issues.append(f"topic_contract.{key} 至少需要 3 項")
    generic_categories = {"開始", "詢問", "回答", "選擇", "確認", "補救", "結束", "其他"}
    categories = normalized["pain_categories"]
    if not 5 <= len(categories) <= 8:
        issues.append("topic_contract.pain_categories 必須有 5 至 8 個痛點機制")
    if any(category in generic_categories for category in categories):
        issues.append("topic_contract.pain_categories 不可使用泛用流程名稱")
    return issues


def _topic_specific_contract_issues(topic: str, contract: dict) -> list[str]:
    """Keep topic categories aligned with the promised user action."""
    categories = _normalize_topic_contract(contract).get("pain_categories", [])
    return [rule["message"] for rule in get_topic_rules(topic).get("contract_categories", [])
            if not any(matches_rule(category, rule) for category in categories)]


def _topic_contract_text(pain_points) -> str:
    contract = _normalize_topic_contract(getattr(pain_points, "contract", {}))
    if not contract["core_pain"]:
        return ""
    return (
        f"目標受眾={contract['audience']}；核心痛點={contract['core_pain']}；"
        f"承諾轉變={contract['promised_transformation']}；"
        f"範圍內={'、'.join(contract['in_scope'])}；"
        f"禁止範圍={'、'.join(contract['out_of_scope'])}；"
        f"必教時刻={'、'.join(contract['required_moments'])}；"
        f"痛點機制={'、'.join(contract['pain_categories'])}"
    )


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
        "job_key": clean("job_key"),
        "task": task,
        "target_phrase": clean("target_phrase"),
        "target_sentence": clean("target_sentence"),
        "pain_trigger": clean("pain_trigger"),
        "user_stakes": clean("user_stakes"),
        "desired_outcome": clean("desired_outcome"),
        "role_type": clean("role_type"),
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
        [
            str(term).strip()
            for term in required_terms
            if str(term).strip() and re.search(r"[A-Za-z]", str(term))
        ][:6]
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
        f"不重複任務={normalized['job_key']}" if normalized["job_key"] else "",
        f"任務={normalized['task']}",
        f"鎖定短句={normalized['target_phrase']}" if normalized["target_phrase"] else "",
        f"鎖定實戰句={normalized['target_sentence']}" if normalized["target_sentence"] else "",
        f"觸發={normalized['pain_trigger']}" if normalized["pain_trigger"] else "",
        f"使用者顧慮={normalized['user_stakes']}" if normalized["user_stakes"] else "",
        f"理想結果={normalized['desired_outcome']}" if normalized["desired_outcome"] else "",
        f"內容角色={normalized['role_type']}" if normalized["role_type"] else "",
        f"失敗情況={normalized['failure_mode']}" if normalized["failure_mode"] else "",
        (
            f"硬性英文關鍵詞={', '.join(normalized['required_terms'])}"
            if normalized["required_terms"]
            else ""
        ),
    ]
    return "；".join(part for part in parts if part)


def _quoted_english_line(text: str) -> str:
    """Extract the authoritative counterpart quote from a planned task."""
    matches = re.findall(r'[“"]([^”"]*[A-Za-z][^”"]*)[”"]', text or "")
    return matches[-1].strip() if matches else ""


def _spoken_line_key(text: str) -> str:
    """Normalize a spoken English line while keeping token boundaries."""
    return " ".join(
        re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*", text or "")
    ).casefold().replace("’", "'")


def _pain_point_alignment_issue(item: dict, point) -> str | None:
    """Return a deterministic role-alignment issue when one is provable."""
    normalized = _normalize_pain_point(point)
    if not normalized or normalized.get("role_type") != "counterpart_line":
        return None

    target_sentence = (
        normalized.get("target_sentence")
        or _quoted_english_line(normalized.get("task", ""))
    )
    if not target_sentence:
        return None

    sentence_key = _spoken_line_key(item.get("sentence_en", ""))
    target_key = _spoken_line_key(target_sentence)
    if sentence_key != target_key:
        return "counterpart_line 的 sentence_en 必須逐字使用對方原話"

    word_key = _spoken_line_key(item.get("word_en", ""))
    if not word_key or f" {word_key} " not in f" {sentence_key} ":
        return "counterpart_line 的 word_en 必須取自同一段對方原話"
    return None


def _apply_locked_blueprint_lines(item: dict, pain_points: list | None) -> dict:
    """Make editorially approved English authoritative over model wording."""
    if not pain_points:
        return item
    try:
        purpose_id = int(item.get("purpose_id"))
    except (TypeError, ValueError):
        return item
    if not 1 <= purpose_id <= len(pain_points):
        return item
    point = _exact_generation_point(pain_points[purpose_id - 1])
    if not point:
        return item
    locked = dict(item)
    mismatched_fields = [
        field
        for field, target in (
            ("word_en", point["target_phrase"]),
            ("sentence_en", point["target_sentence"]),
        )
        if target and _spoken_line_key(item.get(field, "")) != _spoken_line_key(target)
    ]
    if mismatched_fields:
        locked["_locked_source_mismatch"] = ", ".join(mismatched_fields)
    if point["target_phrase"]:
        locked["word_en"] = point["target_phrase"]
    locked["sentence_en"] = point["target_sentence"]
    return locked


def _correct_known_locked_pronunciations(item: dict, point) -> dict:
    """Apply reviewed lexical IPA only when exact source and token alignment agree."""
    point = _normalize_pain_point(point)
    corrected = dict(item)
    lexicon = {"quote": "koʊt", "charge": "tʃɑrdʒ", "charged": "tʃɑrdʒd",
               "cancellation": "ˌkænsəˈleɪʃən", "unauthorized": "ˌʌnˈɔθəraɪzd",
               "authorized": "ˈɔθəraɪzd", "upgraded": "ʌpˈɡreɪdɪd", "connecting": "kəˈnɛktɪŋ"}
    for english_field, ipa_field, target_field in (
        ("word_en", "word_ipa", "target_phrase"),
        ("sentence_en", "sentence_ipa", "target_sentence"),
    ):
        english = item.get(english_field, "")
        ipa = item.get(ipa_field, "")
        if not point or _spoken_line_key(english) != _spoken_line_key(point[target_field]):
            continue
        if not isinstance(ipa, str) or not ipa.startswith("/") or not ipa.endswith("/"):
            continue
        words = re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*", english.lower())
        tokens = ipa.strip("/").split()
        if len(words) != len(tokens):
            continue
        for index, word in enumerate(words):
            replacement = lexicon.get(word)
            if word in {"laptop", "electronics", "electronic", "on", "product", "products", "product's", "quantity"} and "ɒ" in tokens[index]:
                replacement = tokens[index].rstrip(",.;!?").replace("ɒ", "ɑ")
            if word == "what" and "ɒ" in tokens[index]:
                replacement = "wʌt"
            if word == "refund" and index == 0:
                replacement = "rɪˈfʌnd"
            if replacement:
                punctuation = re.search(r"[,.;!?]+$", tokens[index])
                tokens[index] = replacement + (punctuation.group() if punctuation else "")
        corrected[ipa_field] = "/" + " ".join(tokens) + "/"
    return corrected


def _ipa_token_bounds(english: str) -> tuple[int, int]:
    initialisms = {"ID", "ATM", "VIP", "TSA", "USA", "UK", "US", "EU", "ETA", "USB", "GPS", "SIM", "TV", "PC", "PDF", "CEO", "HR", "IT", "SMS"}
    components = re.findall(r"[A-Za-z0-9]+", english)
    maximum = 0
    for index, word in enumerate(components):
        clock = re.fullmatch(r"(\d+)(am|pm)", word, flags=re.I)
        if clock:
            maximum += len(clock.group(1)) + 3
        elif word.lower() in {"am", "pm"} and index and components[index - 1].isdigit():
            maximum += 2
        else:
            maximum += (len(word) if word in initialisms else len(word) + 1 if word.isdigit()
                        else 2 if word == "IDs" else 1)
    return _english_word_count(english), maximum


def _locked_item_issue(item: dict, point, *, max_word_en_words: int | None = None,
                       sentence_ipa_required: bool = True) -> str | None:
    """Validate exact locked English before trusting dependent IPA fields."""
    normalized = _exact_generation_point(point)
    if not normalized:
        return "痛點缺少鎖定英文"
    for field, target in (
        ("word_en", normalized["target_phrase"]),
        ("sentence_en", normalized["target_sentence"]),
    ):
        if target and _spoken_line_key(item.get(field, "")) != _spoken_line_key(target):
            return f"{field} 未逐字使用鎖定英文"
    issues = _validation_issues(item, max_word_en_words=max_word_en_words,
                                sentence_ipa_required=sentence_ipa_required)
    if issues:
        return ", ".join(issues)
    return _pronunciation_and_translation_issue(item, sentence_ipa_required=sentence_ipa_required)


def _pronunciation_and_translation_issue(item: dict, *, sentence_ipa_required: bool = True) -> str | None:
    """Check lexical IPA and known mistranslations after the content gate."""
    for english_field, ipa_field in (
        ("word_en", "word_ipa"),
        ("sentence_en", "sentence_ipa"),
    ):
        if ipa_field == "sentence_ipa" and not sentence_ipa_required:
            continue
        english_count, component_count = _ipa_token_bounds(item.get(english_field, ""))
        ipa_count = len(item.get(ipa_field, "").strip().strip("/").split())
        if not english_count <= ipa_count <= component_count:
            return (
                f"{ipa_field} 詞數 {ipa_count} 與 {english_field} "
                f"詞數範圍 {english_count}-{component_count} 不一致"
            )
        # Catch attested lexical errors without prescribing one accent or notation.
        english = item[english_field].lower()
        ipa = item[ipa_field]
        if re.search(r"\bquote\b", english) and re.search(r"kw(?:oʊ|əʊ)", ipa):
            return f"{ipa_field}: quote 沒有 /w/ 音，應為 /koʊt/ 或 /kəʊt/"
        if re.search(r"\bcharg(?:e|ed)\b", english):
            pronunciations = re.findall(r"tʃ[ɑa][ːrɹ]*[^\s/,.;!?]*", ipa)
            if any("ʒ" not in value for value in pronunciations):
                return f"{ipa_field}: charge 必須包含 /dʒ/，charged 必須包含 /dʒd/"
            if "charged" in english and any(not value.endswith("ʒd") for value in pronunciations):
                return f"{ipa_field}: charged 的字尾應為 /dʒd/"
        if english.startswith("refund ") and re.search(r"ˈri[ː]?fʌnd", ipa):
            return f"{ipa_field}: refund 作動詞時重音在後，應為 /rɪˈfʌnd/"
        if "cancellation" in english and "sləˈeɪ" in ipa:
            return f"{ipa_field}: cancellation 應為 /ˌkænsəˈleɪʃən/"
        if "unauthorized" in english and "ʌnəˈθ" in ipa:
            return f"{ipa_field}: unauthorized 應為 /ˌʌnˈɔθəraɪzd/"
        if re.search(r"\bsouvenirs?\b", english) and re.search(r"su[ː]?ˈv[ɪi]n", ipa):
            return f"{ipa_field}: souvenir 重音在最後音節，應為 /ˌsuːvəˈnɪr/，複數 /ˌsuːvəˈnɪrz/"
        if "upgraded" in english and re.search(r"ʌ[ˈˌ]?[ɡg]reɪd", ipa):
            return f"{ipa_field}: upgraded 不能漏掉 /p/，應為 /ʌpˈɡreɪdɪd/"
        if "connecting" in english and re.search(r"kə[n][ˈˌ]nɛkt", ipa):
            return f"{ipa_field}: connecting 不重複 /n/，應為 /kəˈnɛktɪŋ/"
    if "store credit" in item["sentence_en"].lower() and "商店信用" in item["sentence_cn"]:
        return "sentence_cn: store credit 是店內購物金，不是商店信用"
    if "second opinion" in item["word_en"].lower() and "第二意見" in item["word_cn"]:
        return "word_cn: second opinion 請自然譯為另一位專家的意見"
    if "replaced parts" in item["sentence_en"].lower() and "舊" not in item["sentence_cn"]:
        return "sentence_cn: replaced parts 指拆下來的舊零件，須避免與新零件混淆"
    return None


def _generate_locked_blueprint_items(
    topic: str,
    targets: list[tuple[int, dict]],
) -> list[dict]:
    """Generate pronunciation and translations around code-owned English."""
    completed: dict[int, dict] = {}
    feedback: dict[int, str] = {}
    for attempt in range(1, 4):
        missing = [entry for entry in targets if entry[0] not in completed]
        if not missing:
            break
        assignments = [
            {
                "purpose_id": purpose_id,
                "target_phrase": _exact_generation_point(point)["target_phrase"],
                "target_sentence": _exact_generation_point(point)["target_sentence"],
                "task": _pain_point_task(point),
                "previous_error": feedback.get(purpose_id, ""),
            }
            for purpose_id, point in missing
        ]
        prompt = f"""你是情境英語卡片編輯。主題是「{topic}」。
sentence_en 必須逐字等於 target_sentence，不可改寫、增減或補完。
若 target_phrase 非空，word_en 必須逐字等於 target_phrase；若為空，從 target_sentence 提煉2至6詞的實用詞塊，絕不可照抄完整句。
word_ipa 只能對應 word_en，sentence_ipa 只能對應 sentence_en，不可加入英文中沒有的字。
使用一致的美式 IPA，檢查子音字尾與動詞重音。中文必須自然、忠於各自英文：
word_cn 不可補入只有 sentence_en 才有的條件。replaced parts 指拆下來的舊零件；
second opinion 是另一位專家的意見；store credit 是店內購物金。
tips 提供語感、情緒微調或文化背景，不用「當…時，請…」模板。
若 previous_error 非空，必須修正該錯誤，不可原樣重交。
輸出每個 purpose_id 各一張，並遵守以下欄位規格：
{FIELD_SPEC}

鎖定內容：
{json.dumps(assignments, ensure_ascii=False)}
只輸出 JSON，不要解釋。
"""
        kwargs = {
            "messages": topic_messages(topic, prompt),
            "model": CARD_MODEL,
            "response_format": {"type": "json_object"},
        }
        if CARD_MODEL.startswith("gpt-5"):
            kwargs["max_completion_tokens"] = 2500
        else:
            kwargs["max_tokens"] = 2500
            kwargs["temperature"] = 0.1
        response = _call_openai(stage=f"鎖定卡翻譯與 IPA，第 {attempt} 輪", **kwargs)
        raw_items = _extract_generated_items(response.choices[0].message.content)
        missing_by_id = {purpose_id: point for purpose_id, point in missing}
        for item in raw_items:
            try:
                purpose_id = int(item.get("purpose_id"))
            except (TypeError, ValueError):
                continue
            point = missing_by_id.get(purpose_id)
            if point is None or purpose_id in completed:
                continue
            item = _correct_known_locked_pronunciations(item, point)
            issue = _locked_item_issue(item, point)
            if issue:
                feedback[purpose_id] = issue
                print(
                    f"      ⚠️ 鎖定卡 purpose_id={purpose_id} 第 {attempt} 次未通過: {issue}"
                )
                continue
            completed[purpose_id] = dict(item)
            completed[purpose_id]["purpose_id"] = purpose_id
    missing_ids = [purpose_id for purpose_id, _ in targets if purpose_id not in completed]
    if missing_ids:
        raise RuntimeError(
            "鎖定金句連續 3 次未能產生一致的 IPA 與翻譯，purpose_id="
            + ",".join(map(str, missing_ids))
        )
    return [completed[purpose_id] for purpose_id, _ in targets]


def _has_complete_locked_blueprint(pain_points: list | None) -> bool:
    """Return whether every planned card has code-owned English wording."""
    if not pain_points:
        return False
    normalized = [_normalize_pain_point(point) for point in pain_points]
    return all(
        point
        and point.get("job_key")
        and point.get("target_sentence")
        for point in normalized
    )


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
    if (
        left_task == right_task
        and left_point.get("role_type") == right_point.get("role_type")
    ):
        return True
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
    require_pain_evidence: bool = False,
    contract: dict | None = None,
) -> list[dict]:
    """Deduplicate, rank, cap category dominance, then restore journey order."""
    candidates: list[dict] = []
    generic_intents = {
        "詢問", "回答", "要求", "拒絕", "確認", "補救", "選擇", "聽懂問句",
    }
    generic_categories = {
        "開始", "詢問", "回答", "選擇", "確認", "補救", "結束", "其他", "未分類",
    }
    non_spoken_markers = (
        "深呼吸", "放鬆", "心理建設", "準備講稿", "寫下講稿", "先寫", "事先練習",
        "反覆練習", "找安靜", "選安靜", "整理心情", "設定目標", "自我鼓勵",
    )
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
        if require_pain_evidence and (
            point["category"] in generic_categories
            or not point["pain_trigger"]
            or not point["user_stakes"]
            or not point["desired_outcome"]
            or point["role_type"] not in {"learner_line", "counterpart_line"}
            or any(marker in point["task"] for marker in non_spoken_markers)
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
            selected_counts[point["category"]] += 1

    if require_pain_evidence and count >= 4:
        minimum_counterpart = max(1, math.ceil(count * PLAN_MIN_COUNTERPART_SHARE))

        def role_count(role_type: str) -> int:
            return sum(point["role_type"] == role_type for point in selected)

        def swap_role(target_role: str, required: int) -> None:
            while role_count(target_role) < required:
                replacement = None
                incoming = None
                for candidate in ranked:
                    if id(candidate) in selected_ids or candidate["role_type"] != target_role:
                        continue
                    same_category = next(
                        (
                            point for point in reversed(selected)
                            if point["role_type"] != target_role
                            and point["category"] == candidate["category"]
                        ),
                        None,
                    )
                    flexible = next(
                        (
                            point for point in reversed(selected)
                            if point["role_type"] != target_role
                            and selected_counts[point["category"]] > 1
                            and selected_counts[candidate["category"]] < category_limit
                        ),
                        None,
                    )
                    replacement = same_category or flexible
                    if replacement is not None:
                        incoming = candidate
                        break
                if replacement is None or incoming is None:
                    break
                position = selected.index(replacement)
                selected[position] = incoming
                selected_ids.remove(id(replacement))
                selected_ids.add(id(incoming))
                selected_counts[replacement["category"]] -= 1
                selected_counts[incoming["category"]] += 1

        swap_role("counterpart_line", minimum_counterpart)
        swap_role("learner_line", math.ceil(count * 0.5))

    selected.sort(key=lambda point: (point["sequence"], point["_source_index"]))
    cleaned: list[dict] = []
    for index, point in enumerate(selected[:count], start=1):
        result = {key: value for key, value in point.items() if not key.startswith("_")}
        result["id"] = index
        cleaned.append(result)
    inherited_contract = contract
    if inherited_contract is None:
        inherited_contract = getattr(raw_points, "contract", {})
    return PainPointPlan(cleaned, contract=_normalize_topic_contract(inherited_contract))


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


def _plan_quality_issues(pain_points: list[dict], count: int) -> list[str]:
    """Deterministic deck-level gates before semantic plan review."""
    issues: list[str] = []
    if len(pain_points) != count:
        issues.append(f"痛點數量為 {len(pain_points)}/{count}")
        return issues

    normalized_points = [
        point for point in (_normalize_pain_point(value) for value in pain_points)
        if point
    ]
    locked_points = [point for point in normalized_points if point["target_phrase"]]
    if locked_points:
        if any(not point["target_sentence"] or not point["job_key"] for point in locked_points):
            issues.append("鎖定牌組缺少 target_sentence 或 job_key")
        for field, label in (
            ("job_key", "現場任務"),
            ("target_phrase", "鎖定短句"),
            ("target_sentence", "鎖定實戰句"),
        ):
            values = [_similarity_text(point[field]) for point in locked_points]
            duplicates = [value for value, amount in Counter(values).items() if amount > 1]
            if duplicates:
                issues.append(f"{label}出現重複：" + "、".join(duplicates[:5]))
        for point in locked_points:
            if _english_word_count(point["target_phrase"]) > MAX_WORD_EN_WORDS:
                issues.append(f"鎖定短句超過 {MAX_WORD_EN_WORDS} 字：{point['target_phrase']}")
            if _english_word_count(point["target_sentence"]) > MAX_SENTENCE_EN_WORDS:
                issues.append(f"鎖定實戰句超過 {MAX_SENTENCE_EN_WORDS} 字：{point['target_sentence']}")

    category_count = len({point.get("category", "") for point in pain_points})
    if category_count > 8:
        issues.append(f"痛點分類多達 {category_count} 類，疑似以換場景製造假多樣性")
    contract = _normalize_topic_contract(getattr(pain_points, "contract", {}))
    allowed_categories = set(contract.get("pain_categories", []))
    actual_categories = {point.get("category", "") for point in pain_points}
    if allowed_categories:
        unexpected = actual_categories - allowed_categories
        missing = allowed_categories - actual_categories
        if unexpected:
            issues.append("出現契約外分類：" + "、".join(sorted(unexpected)))
        if missing and count >= len(allowed_categories):
            issues.append("契約分類未被涵蓋：" + "、".join(sorted(missing)))

    counterpart_count = sum(
        _normalize_pain_point(point).get("role_type") == "counterpart_line"
        for point in pain_points
        if _normalize_pain_point(point)
    )
    if contract.get("learner_only") and counterpart_count:
        issues.append(f"learner_only 牌組不可包含對方原話，目前有 {counterpart_count} 項")
    if count >= 4 and not contract.get("learner_only"):
        minimum_counterpart = max(1, math.ceil(count * PLAN_MIN_COUNTERPART_SHARE))
        if counterpart_count < minimum_counterpart:
            issues.append(
                f"對方原話僅 {counterpart_count}/{count} 項，至少需要 {minimum_counterpart} 項"
            )

    if count >= 4:
        learner_count = count - counterpart_count
        if learner_count < math.ceil(count * 0.5):
            issues.append("使用者可直接開口的內容不足一半")

    return issues


def _topic_specific_plan_coverage_issues(
    topic: str,
    pain_points: list[dict],
) -> list[str]:
    """Require every named high-friction moment, not just a related theme."""
    searchable = []
    learner_actions = []
    for point in pain_points:
        normalized = _normalize_pain_point(point)
        if not normalized:
            continue
        searchable.append(" ".join(
            normalized[key]
            for key in (
                "category", "scenario", "intent", "task", "pain_trigger",
                "desired_outcome",
            )
        ).casefold())
        if normalized["role_type"] == "learner_line":
            learner_actions.append(" ".join(
                normalized[key]
                for key in ("intent", "task", "desired_outcome")
            ).casefold())
    deck_text = " ".join(searchable)
    learner_action_text = " ".join(learner_actions)
    texts = {"deck": deck_text, "learner": learner_action_text}
    issues = [rule["message"] for rule in get_topic_rules(topic).get("coverage", [])
              if not matches_rule(texts[rule.get("source", "deck")], rule)]
    for index, point in enumerate(pain_points, start=1):
        explicit_violation = _explicit_focus_exclusion_violation(topic, point)
        if explicit_violation:
            issues.append(f"#{index} {explicit_violation}")
        violation = _topic_specific_plan_violation(topic, point)
        if violation:
            issues.append(f"#{index} {violation}")
    return issues


def _explicit_focus_exclusion_violation(topic: str, point: dict) -> str | None:
    """Enforce concrete exclusions from the original user-authored focus."""
    if "本集內容焦點與邊界：" not in topic:
        return None
    focus = topic.split("本集內容焦點與邊界：", 1)[-1].casefold()
    exclusion_sections = re.findall(
        r"(?:不要收錄|不要包含|禁止包含|禁止收錄)[：:]?([^。\n]+)",
        focus,
    )
    if not exclusion_sections:
        return None
    exclusions = " ".join(exclusion_sections)
    normalized = _normalize_pain_point(point)
    if not normalized:
        return None
    action_text = " ".join(
        normalized[key]
        for key in ("intent", "task", "desired_outcome")
    ).casefold()

    if "放慢" in exclusions and any(
        marker in action_text
        for marker in ("放慢", "說慢", "slow down", "slower", "speak slowly")
    ):
        return "違反 focus 明確排除：不要請別人放慢速度"
    if "重複" in exclusions and any(
        marker in action_text
        for marker in ("請再說", "再說一次", "請重複", "repeat", "say that again")
    ):
        return "違反 focus 明確排除：不要請別人重複"
    if "請別人發言" in exclusions and any(
        marker in action_text
        for marker in (
            "請別人發言", "請對方發言", "請對方分享", "邀請對方",
            "share your thoughts", "what do you think", "anything to add",
        )
    ):
        return "違反 focus 明確排除：不要請別人發言"
    if "一般會議流程" in exclusions and any(
        marker in action_text
        for marker in ("會議流程", "議程安排", "會議紀錄", "主持會議", "meeting agenda", "minutes")
    ):
        return "違反 focus 明確排除：不要一般會議流程"
    return None


def _review_rejection_conflicts_with_contract(
    topic: str,
    point: dict,
    reason: str,
) -> bool:
    """Ignore reviewer claims that contradict an explicit topic requirement."""
    point_text = " ".join(
        str(_normalize_pain_point(point).get(key, ""))
        for key in (
            "category", "scenario", "intent", "task", "pain_trigger",
            "desired_outcome",
        )
    ).casefold()
    protection = get_topic_rules(topic).get("review_protection", {})
    return (any(claim.casefold() in reason.casefold() for claim in protection.get("reason_markers", []))
            and any(marker.casefold() in point_text for marker in protection.get("protected_markers", [])))


def _ai_review_pain_point_plan(
    topic: str,
    contract: dict,
    pain_points: list[dict],
) -> list[str]:
    """Review the blueprint against the title promise before cards are written."""
    topic_specific_note = topic_rule_text(topic, "plan_review")
    role_mix_rule = (
        "5. learner_only=true：每項都必須是 learner_line，禁止加入只是讓學習者聽懂的對方原話。"
        if _normalize_topic_contract(contract).get("learner_only")
        else "5. role_type=counterpart_line 時，task 必須是學習者需要立即聽懂的對方原話；整副牌要同時訓練聽懂與開口。"
    )
    prompt = f"""你是獨立的課程總編。請審核「{topic}」的內容契約與痛點藍圖。

題名契約：
{json.dumps(_normalize_topic_contract(contract), ensure_ascii=False)}

審核時不要相信候選自己填的 priority、frequency、friction 分數。逐項判斷：
1. 內容是否直接解決題名承諾的核心痛點，而不只是發生在相關媒介、場所或使用禮貌句型。
2. pain_trigger 是否是可觀察的當下觸發；user_stakes 是否是使用者真正在怕的後果；desired_outcome 是否是這張卡能促成的具體結果。
3. task 是否能直接指導一句現場原話，且和其他項目有不同的觸發、理解需求、回答、補救或升級結果。
4. category 必須描述痛點機制，不可只用開始、詢問、選擇、確認、補救等流程標籤，也不可用換餐廳、醫院、飯店等場所製造假多樣性。
{role_mix_rule}
6. out_of_scope 中的內容一律退回。
7. 例行詢價、問營業時間、問 Wi-Fi、問折扣、問課程時間等，若沒有問題、焦慮、誤解、風險或補救需求，不能算痛點。
8. failure_mode 不得只是「無法獲得資訊」等同義反述；必須呈現具體代價。
9. 主題若含「本集內容焦點與邊界」，該段描述是最高優先的內容 brief：明確點名的項目是硬需求，不得判為偏題，也不得擴張到描述未涵蓋的受眾、場合或相鄰任務。
{topic_specific_note}
{SEMANTIC_DUPLICATE_POLICY}
藍圖尚未寫出難度不同的英文，因此優先每個實際溝通目的只規劃一項；同一目的超過兩項一律退回。

每個 issues 與 reject.reason 都必須指出具體違反哪一條、哪個 out_of_scope 或哪個內容缺口；禁止只寫「偏離核心痛點」「偏題」「整副牌問題」等無法採取行動的籠統理由。使用者明確列入 focus 的項目若要退回，必須說明它為何沒有完成該焦點，而不能只宣告偏離。

只輸出 JSON：
{{"pass": true, "issues": [], "reject": []}}
或
{{"pass": false, "issues": ["整副牌問題"], "reject": [{{"id": 3, "reason": "偏離核心痛點"}}]}}

待審痛點：
{json.dumps(pain_points, ensure_ascii=False)}
"""
    kwargs = {
        "messages": topic_messages(topic, prompt),
        "model": PLAN_REVIEW_MODEL,
        "response_format": {"type": "json_object"},
    }
    if PLAN_REVIEW_MODEL.startswith("gpt-5"):
        kwargs["max_completion_tokens"] = 8000
    else:
        kwargs["max_tokens"] = 8000
        kwargs["temperature"] = 0.1
    response = _call_openai(stage="痛點藍圖獨立審稿", **kwargs)
    payload = json.loads(response.choices[0].message.content)
    generic_reasons = {"整副牌問題", "偏離核心痛點", "內容偏離核心痛點", "偏題"}
    issues = [
        str(issue).strip()
        for issue in payload.get("issues", [])
        if str(issue).strip() and str(issue).strip() not in generic_reasons
    ]
    for entry in payload.get("reject", []):
        if not isinstance(entry, dict):
            continue
        item_id = entry.get("id", "?")
        reason = str(entry.get("reason", "偏離核心痛點")).strip()
        if reason in generic_reasons:
            continue
        try:
            rejected_point = pain_points[int(item_id) - 1]
            rejected_task = _pain_point_task(rejected_point)
        except (TypeError, ValueError, IndexError):
            rejected_point = None
            rejected_task = ""
        if rejected_point and _review_rejection_conflicts_with_contract(
            topic, rejected_point, reason
        ):
            continue
        task_note = f"「{rejected_task}」" if rejected_task else ""
        issues.append(f"#{item_id} {task_note}{reason}")
    return issues


def _topic_specific_plan_violation(topic: str, point: dict) -> str | None:
    """Reject known false-adjacency patterns before semantic review."""
    text = " ".join(
        str(point.get(key, ""))
        for key in (
            "category", "scenario", "speaker", "intent", "task", "pain_trigger"
        )
    ).casefold()
    for rule in get_topic_rules(topic).get("plan_rejections", []):
        if rule.get("role_type") and point.get("role_type") != rule["role_type"]:
            continue
        category = str(point.get("category", "")).casefold()
        if rule.get("category_any") and not any(marker.casefold() in category for marker in rule["category_any"]):
            continue
        if matches_rule(text, rule):
            return rule["message"]
    return None


def _save_pain_point_plan(topic: str, pain_points: list[dict], path: str) -> None:
    _apply_focus_phrase_locks(topic, pain_points)
    contract = _normalize_topic_contract(getattr(pain_points, "contract", {}))
    contract_issues = _topic_contract_issues(contract)
    contract_issues.extend(_topic_specific_contract_issues(topic, contract))
    if contract_issues:
        raise ValueError("拒絕保存不完整的題名契約：" + "；".join(contract_issues))
    quality_issues = _plan_quality_issues(pain_points, len(pain_points))
    quality_issues.extend(_topic_specific_plan_coverage_issues(topic, pain_points))
    if quality_issues:
        raise ValueError("拒絕保存未通過品質門檻的痛點藍圖：" + "；".join(quality_issues))
    payload = {
        "version": PLAN_VERSION,
        "topic": topic,
        "count": len(pain_points),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "topic_contract": contract,
        "summary": _plan_summary(pain_points),
        "pain_points": pain_points,
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_pain_point_plan(
    path: str,
    expected_count: int | None = None,
    expected_topic: str | None = None,
) -> list[dict]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or payload.get("version") != PLAN_VERSION:
        found_version = payload.get("version", "legacy") if isinstance(payload, dict) else "legacy"
        raise PlanVersionError(
            f"策劃格式 v{found_version} 已過期，目前需要 v{PLAN_VERSION}；請重新規劃"
        )
    saved_topic = str(payload.get("topic", "")).strip()
    if expected_topic is not None and saved_topic != expected_topic.strip():
        raise ValueError(
            "策劃檔的主題描述與本次輸入不一致；"
            f"檔案為「{saved_topic}」，本次為「{expected_topic.strip()}」"
        )
    contract = _normalize_topic_contract(payload.get("topic_contract"))
    contract_issues = _topic_contract_issues(contract)
    contract_issues.extend(
        _topic_specific_contract_issues(str(payload.get("topic", "")), contract)
    )
    if contract_issues:
        raise ValueError("；".join(contract_issues))
    raw_points = payload.get("pain_points", payload) if isinstance(payload, dict) else payload
    raw_count = len(list(_flatten_pain_point_candidates(raw_points)))
    count = expected_count if expected_count is not None else raw_count
    points = _select_pain_points(
        raw_points,
        count,
        require_categories=True,
        require_pain_evidence=True,
        contract=contract,
    )
    _apply_focus_phrase_locks(saved_topic, points)
    if expected_count is not None and len(points) != expected_count:
        raise ValueError(f"策劃檔必須剛好有 {expected_count} 項痛點")
    quality_issues = _plan_quality_issues(points, count)
    quality_issues.extend(
        _topic_specific_plan_coverage_issues(str(payload.get("topic", "")), points)
    )
    if quality_issues:
        raise ValueError("；".join(quality_issues))
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
        # Purpose duplication and single-slot sentence templates have separate gates.
        if word_ratio >= 0.97 or sentence_ratio >= 0.98:
            return True
    return False


def _sentence_pattern_issue(candidate: dict, existing_items: list[dict]) -> str | None:
    """Catch single-slot sentence templates without conflating opposite answers."""
    protected_tokens = {
        "i", "me", "my", "mine", "we", "us", "our", "you", "your", "yours",
        "he", "him", "his", "she", "her", "they", "them", "their", "it", "its",
        "no", "not", "never", "without", "only", "all", "some", "any",
        "can", "could", "will", "would", "may", "might", "must", "should",
        "do", "does", "did", "is", "are", "was", "were", "have", "has", "had",
        "more", "less", "extra", "double", "lightly", "fully", "before", "after",
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
        "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
        "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
        "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
        "thousand", "first", "second", "third", "half", "single", "small", "large",
    }

    def tokens(text: str) -> list[str]:
        return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)*", text.casefold().replace("’", "'"))

    sentence = candidate.get("sentence_en", "")
    candidate_tokens = tokens(sentence)
    if len(candidate_tokens) < 5:
        return None
    candidate_role = (_normalize_pain_point(candidate.get("_pain_point")) or {}).get("role_type")
    for existing in existing_items:
        other_sentence = existing.get("sentence_en", "")
        other_tokens = tokens(other_sentence)
        if len(candidate_tokens) != len(other_tokens):
            continue
        if ("?" in sentence) != ("?" in other_sentence):
            continue
        other_role = (_normalize_pain_point(existing.get("_pain_point")) or {}).get("role_type")
        if candidate_role and other_role and candidate_role != other_role:
            continue
        differences = [(a, b) for a, b in zip(candidate_tokens, other_tokens) if a != b]
        if len(differences) != 1:
            continue
        left, right = differences[0]
        # Changes in negation, role, quantity, or modality need semantic review.
        if any(
            token in protected_tokens or "'" in token or any(char.isdigit() for char in token)
            for token in (left, right)
        ):
            continue
        return (
            f"句型過度相似：與「{other_sentence}」只有單一詞替換；"
            "保留原本場景、角色與要求，改用不同句型或自然同義說法，不能只換名詞"
        )
    return None


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


def _reference_prompt_note(reference_items: list[dict] | None, *, for_review: bool = False) -> str:
    """Bound generation context, but include every distinct reference in Tier 3."""
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
        if not for_review and len(lines) >= MAX_REFERENCE_CARDS_IN_PROMPT:
            break

    if not lines:
        return ""
    review_rule = (
        "跨牌組意圖審查：逐張比對新卡與以下全部參考卡的說話角色、具體行動／資訊及實際結果。"
        "即使字面完全不同，結果相同且可互換的同義改寫也必須退回；"
        "不能因不同主題、分類、禮貌程度或難度而放行。"
        "角色相反、肯定與否定、不同問題或不同結果不算重複。"
        "參考牌組不可退回，只退回新卡；兩種說法的額度只適用當前牌組。"
        "退件理由以「語意重複：」開頭，指出來源牌組、參考原句及相同的具體意圖。\n"
        if for_review else ""
    )
    return (
        "\n以下是上一集或指定參考牌組已教過的內容。"
        "不得重出相同短句、同義改寫，或說話角色、意圖、答案都相同的卡片；"
        "只有確實解決不同現場任務時才能沿用相關領域詞彙：\n"
        + review_rule
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
    counterpart_rule = get_topic_rules(topic).get("counterpart_validation", {})
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

        pattern_issue = _sentence_pattern_issue(item, seen_items)
        if pattern_issue:
            rejected[idx] = pattern_issue
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
                if "?" not in item.get("sentence_en", ""):
                    rejected[idx] = "指定為對方問句，但 sentence_en 未使用直接問句"
                    continue

            normalized_point = _normalize_pain_point(assigned_point)
            exact_point = _exact_generation_point(assigned_point)
            if exact_point:
                normalized_point.update(target_phrase=exact_point["target_phrase"],
                                        target_sentence=exact_point["target_sentence"])
            if normalized_point and normalized_point.get("target_phrase"):
                if item.get("word_en", "").strip() != normalized_point["target_phrase"]:
                    rejected[idx] = (
                        "word_en 必須逐字使用鎖定實戰短句："
                        + normalized_point["target_phrase"]
                    )
                    continue
            if normalized_point and normalized_point.get("target_sentence"):
                if item.get("sentence_en", "").strip() != normalized_point["target_sentence"]:
                    rejected[idx] = (
                        "sentence_en 必須逐字使用鎖定情境原話："
                        + normalized_point["target_sentence"]
                    )
                    continue
            if (
                normalized_point
                and normalized_point.get("role_type") == "counterpart_line"
                and not normalized_point.get("target_phrase")
                and counterpart_rule
            ):
                is_staff_response = not counterpart_rule.get("required_any") or any(
                    marker.casefold() in combined for marker in counterpart_rule.get("required_any", [])
                )
                uses_customer_voice = any(
                    marker.casefold() in f" {combined} " for marker in counterpart_rule.get("forbidden_any", [])
                )
                impact_addresses_customer = (
                    normalized_point.get("category") not in counterpart_rule.get("address_customer_categories", [])
                    or " your " in f" {combined} "
                )
                if (
                    not is_staff_response
                    or uses_customer_voice
                    or not impact_addresses_customer
                ):
                    rejected[idx] = counterpart_rule["message"]
                    continue
            alignment_issue = _pain_point_alignment_issue(item, assigned_point)
            if alignment_issue:
                rejected[idx] = alignment_issue
                continue
            explicit_terms = normalized_point.get("required_terms", []) if normalized_point else []
            required_tokens = [term.casefold() for term in explicit_terms]
            required_match_count = math.ceil(len(required_tokens) / 2)
            if not required_tokens:
                required_tokens = [
                    token.casefold()
                    for token in re.findall(r"[A-Za-z][A-Za-z-]+", assigned_point_task)
                    if token.casefold() not in english_stopwords
                ]
                required_match_count = min(
                    2, math.ceil(len(required_tokens) / 2)
                )
            if required_tokens:
                matched = sum(token in combined for token in required_tokens)
                if matched < required_match_count:
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


def _request_topic_contract(topic: str, reference_note: str, retry_note: str = "") -> dict:
    topic_specific_note = topic_rule_text(topic, "contract")
    prompt = f"""你是台灣成人情境英語課程的內容總編。主題是「{topic}」。
先不要寫詞卡或痛點清單，只定義這個題名對學習者的內容承諾。
{reference_note}

重要邊界：
- 若主題包含「本集內容焦點與邊界」，該段描述是最高優先的內容 brief。audience、場景、core_pain、in_scope 與 required_moments 不得擴張到描述之外；只有完成描述中任務不可缺少的相鄰步驟才能合理推導。
- 若描述明確要求全部都是學習者自己開口、並排除對方原話，learner_only 必須為 true；否則為 false。
- pain_categories 每一類都必須能產生電話或現場直接說出、聽到的英文原話；禁止呼吸、放鬆、寫講稿、心理建設、學習技巧等非語言建議。
- 若主題包含「本集內容焦點與邊界」，其中逐項點名的技能都是硬需求；必須全部寫入 in_scope 或 required_moments，不得濃縮到遺漏任何一項。
{topic_specific_note}

只輸出 JSON：
{{"topic_contract": {{
  "audience": "最需要這副牌的具體使用者，不可只寫英文學習者",
  "core_pain": "題名真正承諾解決的焦慮、摩擦或失敗",
  "promised_transformation": "學完後從什麼困境變成什麼狀態",
  "in_scope": ["至少三項直接服務核心痛點的範圍"],
  "out_of_scope": ["至少三項看似相關但偏題的內容"],
  "required_moments": ["至少三個不教就無法兌現承諾的高摩擦時刻"],
  "pain_categories": ["剛好七個以痛點機制命名的分類；禁止開始、詢問、選擇、確認、補救、結束或場所名稱"],
  "learner_only": false
}}}}
{retry_note}
"""
    kwargs = {
        "messages": topic_messages(topic, prompt),
        "model": PLAN_MODEL,
        "response_format": {"type": "json_object"},
    }
    if PLAN_MODEL.startswith("gpt-5"):
        kwargs["max_completion_tokens"] = 2500
    else:
        kwargs["max_tokens"] = 2500
        kwargs["temperature"] = 0.1
    response = _call_openai(stage="主題範圍策劃", **kwargs)
    payload = json.loads(response.choices[0].message.content)
    contract = _normalize_topic_contract(payload.get("topic_contract"))
    if _topic_requests_learner_only(topic):
        contract["learner_only"] = True
    issues = _topic_contract_issues(contract)
    issues.extend(_topic_specific_contract_issues(topic, contract))
    if issues:
        raise RuntimeError("；".join(issues))
    return contract


def _request_pain_point_candidates(
    topic: str,
    contract: dict,
    candidate_count: int,
    reference_note: str,
    retry_note: str = "",
) -> list[dict]:
    """Generate a large blueprint through bounded requests to avoid JSON timeouts."""
    candidates: list[dict] = []
    normalized_contract = _normalize_topic_contract(contract)
    pain_categories = normalized_contract["pain_categories"]
    learner_only = normalized_contract["learner_only"]
    topic_rules = get_topic_rules(topic)
    role_policy = topic_rules.get("candidate_roles", {})
    rounds = 0
    successful_batches = 0
    max_rounds = math.ceil(candidate_count / PLAN_GENERATE_BATCH) + len(pain_categories) + 4
    consecutive_failures = 0
    zero_add_streak = 0
    batch_retry_note = ""
    while len(candidates) < candidate_count and rounds < max_rounds:
        _check_generation_deadline()
        per_category_target = max(1, math.ceil(candidate_count / len(pain_categories)))
        batch_count = min(
            PLAN_GENERATE_BATCH,
            per_category_target,
            candidate_count - len(candidates),
        )
        category_focus = pain_categories[successful_batches % len(pain_categories)]
        force_learner_category = any(
            marker.casefold() in category_focus.casefold()
            for marker in role_policy.get("learner_category_markers", [])
        )
        if learner_only:
            target_role = "learner_line"
        elif role_policy:
            counterpart_candidates = sum(
                point["role_type"] == "counterpart_line" for point in candidates
            )
            target_role = (
                "learner_line"
                if force_learner_category
                else "counterpart_line"
                if counterpart_candidates < math.ceil(candidate_count * role_policy.get("counterpart_share", 0.25))
                else "learner_line"
            )
        else:
            target_role = (
                "counterpart_line" if successful_batches % 3 == 1 else "learner_line"
            )
        counterpart_instruction = topic_rules.get("instructions", {}).get("counterpart_candidates", "")
        if target_role == "counterpart_line" and counterpart_instruction:
            role_instruction = counterpart_instruction
        elif target_role == "counterpart_line":
            role_instruction = (
                "本批全部是 counterpart_line：speaker 必須是對話中的另一個人；task 必須逐字使用格式"
                "「聽懂對方原話：“[一個完整英文句子]”」，引號內必須是對方真的會直接說出的英文，"
                "禁止放入學習者自己的要求。"
            )
        else:
            role_instruction = (
                "本批全部是 learner_line：speaker 必須是學習者，task 必須描述學習者會直接說出口的原話。"
            )
        existing_note = ""
        if candidates:
            existing_note = (
                "\n整副牌已產生的任務如下，跨分類也不得做同義改寫：\n- " +
                "\n- ".join(
                    f"[{point['category']}] {point['task']}" for point in candidates
                ) + "\n"
            )
        category_action_instruction = next((rule["instruction"]
            for rule in topic_rules.get("category_instructions", [])
            if matches_rule(category_focus, rule)), "")
        prompt = f"""你是台灣成人情境英語課程的內容企劃。主題是「{topic}」。
依照以下已核定題名契約，產生下一批剛好 {batch_count} 個溝通痛點，不要寫英文詞卡：
{json.dumps(_normalize_topic_contract(contract), ensure_ascii=False)}
{reference_note}
{existing_note}
{SEMANTIC_DUPLICATE_POLICY}
規劃候選時優先每個實際溝通目的只產生一項；不可換分類或場景反覆要求同一種書面資料。

規則：
1. 每項都必須直接服務 core_pain；out_of_scope 一律禁止。若主題附有「本集內容焦點與邊界」，不得加入描述未涵蓋的受眾、場合或相鄰任務。
2. 本批只負責痛點機制「{category_focus}」。每項 category 必須逐字填「{category_focus}」，不得新增分類或用換場所製造多樣性。
   {category_action_instruction}
3. 每項是不同的觸發、理解需求、回答、補救或升級結果，不得只替換商品、場所或名詞。
   task 必須明確指導一段學習者會直接說出、或對方會直接說出的英文原話；禁止準備講稿、深呼吸、放鬆、找安靜場所、練習等非語言行動。
4. pain_trigger 是可觀察的當下事件；user_stakes 是使用者真正害怕的具體後果；desired_outcome 是這一句促成的可觀察結果。
5. {role_instruction} 每一項 role_type 都必須逐字填「{target_role}」。禁止把學習者自己的要求標成 counterpart_line。
6. failure_mode 不得只寫「無法取得資訊」。priority、frequency、friction 必須誠實評分，不可全部給 5。
7. sequence 從 {len(candidates) + 1} 開始，依真實溝通流程遞增。

每項欄位：category、scenario、speaker、intent、task、pain_trigger、user_stakes、desired_outcome、role_type、failure_mode、priority、frequency、friction、sequence、required_terms。
只輸出 JSON：{{"candidates": [{{"category": "...", "scenario": "...", "speaker": "...", "intent": "...", "task": "...", "pain_trigger": "...", "user_stakes": "...", "desired_outcome": "...", "role_type": "learner_line", "failure_mode": "...", "priority": 5, "frequency": 5, "friction": 4, "sequence": 1, "required_terms": []}}]}}。
{retry_note}
{batch_retry_note}
"""
        kwargs = {
            "messages": topic_messages(topic, prompt),
            "model": PLAN_MODEL,
            "response_format": {"type": "json_object"},
        }
        if PLAN_MODEL.startswith("gpt-5"):
            kwargs["max_completion_tokens"] = 6000
        else:
            kwargs["max_tokens"] = 6000
            kwargs["temperature"] = 0.1
        try:
            response = _call_openai(
                stage=f"痛點候選 #{rounds + 1}（{category_focus}，已 {len(candidates)}/{candidate_count}）",
                **kwargs,
            )
            payload = json.loads(response.choices[0].message.content)
        except GenerationTimeoutError:
            raise
        except Exception as exc:
            consecutive_failures += 1
            rounds += 1
            print(
                f"   ⚠️  痛點候選批次失敗（連續 {consecutive_failures}/3）：{exc}"
            )
            if consecutive_failures >= 3:
                raise RuntimeError("痛點候選批次連續失敗 3 次") from exc
            batch_retry_note = f"\n前一批技術失敗：{exc}。請重新輸出完整有效 JSON。\n"
            continue
        raw_batch = list(_flatten_pain_point_candidates(payload.get("candidates", [])))
        batch_candidates: list[dict] = []
        role_violation_count = 0
        for raw in raw_batch:
            point = _normalize_pain_point(raw, len(candidates))
            if (
                not point
                or point["category"] != category_focus
                or _topic_specific_plan_violation(topic, point)
                or any(
                _pain_points_semantically_duplicate(point, old)
                for old in candidates + batch_candidates
                )
            ):
                continue
            task = point["task"]
            counterpart_format_ok = (
                task.startswith("聽懂對方原話")
                and bool(re.search(r"[A-Za-z][A-Za-z' -]{4,}", task))
            )
            if (
                point["role_type"] != target_role
                or (
                    target_role == "counterpart_line"
                    and not counterpart_format_ok
                )
                or (
                    target_role == "learner_line"
                    and task.startswith("聽懂對方")
                )
            ):
                role_violation_count += 1
                continue
            batch_candidates.append(
                {key: value for key, value in point.items() if not key.startswith("_")}
            )
            if len(batch_candidates) >= batch_count:
                break
        if role_violation_count:
            consecutive_failures += 1
            rounds += 1
            print(
                f"   ⚠️  痛點候選批次有 {role_violation_count} 項角色或原話格式錯誤，"
                f"重做「{category_focus}」的 {target_role}"
            )
            if consecutive_failures >= 3:
                raise RuntimeError("痛點候選批次連續 3 次角色不符")
            batch_retry_note = (
                f"\n前一批有 {role_violation_count} 項角色或原話格式錯誤。"
                f"這次所有項目都必須是 {target_role}；counterpart_line 的 task "
                "必須包含對方完整英文原話，請依規則重寫。\n"
            )
            continue
        consecutive_failures = 0
        batch_retry_note = ""
        added = len(batch_candidates)
        candidates.extend(batch_candidates)
        if added:
            successful_batches += 1
        rounds += 1
        print(
            f"   🧩 痛點候選批次 #{rounds}：新增 {added}，"
            f"累計 {len(candidates)}/{candidate_count}"
        )
        zero_add_streak = zero_add_streak + 1 if added == 0 else 0
        if zero_add_streak >= len(pain_categories):
            break
    return candidates


def _plan_pain_points(
    topic: str,
    count: int,
    reference_items: list[dict] | None = None,
) -> list[dict]:
    """Create, validate, and independently review a pain-centered blueprint."""
    _check_generation_deadline()
    if not ENABLE_PAIN_POINT_PLAN:
        return []

    curated = get_curated_blueprint(topic, count)
    if curated:
        contract, curated_points = curated
        points = PainPointPlan(curated_points, contract=contract)
        issues = _plan_quality_issues(points, count)
        issues.extend(_topic_specific_plan_coverage_issues(topic, points))
        if issues:
            raise RuntimeError("人工策劃未通過品質規則：" + "；".join(issues))
        print(f"   📌 已載入 {count} 個人工鎖定的實戰任務")
        return points

    extra_candidates = min(PLAN_MIN_EXTRA_CANDIDATES, max(5, count // 10))
    candidate_count = count + extra_candidates
    reference_note = _reference_prompt_note(reference_items)
    points: list[dict] | None = None
    fallback_points: list[dict] | None = None
    fallback_contract: dict = {}
    fallback_category_count = 0
    fallback_raw_count = 0
    raw_count = 0
    last_error: Exception | None = None
    retry_note = ""
    for attempt in range(3):
        _check_generation_deadline()
        _progress(f"痛點策劃：第 {attempt + 1}/3 輪，目標 {count} 張、{candidate_count} 個候選")
        raw_points = None
        contract: dict = {}
        strict_selected = False
        try:
            contract = _request_topic_contract(topic, reference_note, retry_note)
            raw_points = _request_pain_point_candidates(
                topic,
                contract,
                candidate_count,
                reference_note,
                retry_note,
            )
            raw_count = len(list(_flatten_pain_point_candidates(raw_points)))
            if raw_count < count:
                raise RuntimeError(
                    f"僅產生 {raw_count}/{count} 個必要候選"
                )
            if raw_count < candidate_count:
                print(
                    f"   ℹ️  模型回傳 {raw_count}/{candidate_count} 個候選；"
                    f"已達必要的 {count} 個，繼續執行品質審核"
                )
            selected_points = _select_pain_points(
                raw_points,
                count,
                require_categories=True,
                require_pain_evidence=True,
                contract=contract,
            )
            strict_selected = True
            quality_issues = _plan_quality_issues(selected_points, count)
            quality_issues.extend(
                _topic_specific_plan_coverage_issues(topic, selected_points)
            )
            if quality_issues:
                raise RuntimeError("；".join(quality_issues))
            semantic_issues = _ai_review_pain_point_plan(
                topic, contract, selected_points
            )
            if semantic_issues:
                raise RuntimeError("；".join(semantic_issues[:12]))
            points = selected_points
            break
        except GenerationTimeoutError:
            raise
        except Exception as exc:
            last_error = exc
            if raw_points is not None and contract and not strict_selected:
                try:
                    relaxed_points = _select_pain_points(
                        raw_points,
                        count,
                        require_categories=True,
                        minimum_categories=PLAN_FALLBACK_MIN_CATEGORIES,
                        require_pain_evidence=True,
                        contract=contract,
                    )
                    fallback_issues = _plan_quality_issues(relaxed_points, count)
                    fallback_issues.extend(
                        _topic_specific_plan_coverage_issues(topic, relaxed_points)
                    )
                    if fallback_issues:
                        raise RuntimeError("；".join(fallback_issues))
                    semantic_issues = _ai_review_pain_point_plan(
                        topic, contract, relaxed_points
                    )
                    if semantic_issues:
                        raise RuntimeError("；".join(semantic_issues[:12]))
                    relaxed_category_count = len(
                        _plan_summary(relaxed_points)["categories"]
                    )
                    if relaxed_category_count > fallback_category_count:
                        fallback_points = relaxed_points
                        fallback_contract = contract
                        fallback_category_count = relaxed_category_count
                        fallback_raw_count = raw_count
                except GenerationTimeoutError:
                    raise
                except Exception:
                    pass
            if attempt < 2:
                print(f"   ⚠️  痛點策劃未通過（第 {attempt + 1} 次）：{exc}，重新規劃...")
                retry_note = (
                    f"\n前次輸出未通過程式篩選：{exc}。"
                    "這次先修正 topic_contract，再確保每項都有痛點觸發、具體代價、"
                    "可觀察結果與正確 role_type；刪除只是相關或只是禮貌的例行內容。\n"
                )

    if points is None:
        if fallback_points is not None:
            points = PainPointPlan(fallback_points, contract=fallback_contract)
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
    contract_note = _topic_contract_text(pain_points)
    if contract_note:
        blueprint += (
            "\n以下題名契約是內容邊界。任何卡片都必須直接服務核心痛點，"
            "且不得落入禁止範圍：\n" + contract_note + "\n"
        )
    if pain_points:
        blueprint += (
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
{topic_rule_text(topic)}
{blueprint}

整副牌預計 {count} 張。先在心中完成以下判斷，再生成內容，不要輸出分析過程：
- 若主題附有「本集內容焦點與邊界」，它是最高優先的內容 brief；不得自行加入描述之外的受眾、場合或相鄰任務。
- 鎖定這個主題唯一、最具體的場景與使用者身分；品牌、場所或專有名詞不得套用其他同名含義。
- 找出使用者為了完成這件事，最常遇到、最容易卡住、最怕聽不懂或說錯的具體時刻。
- 按真實流程排序思考：開始、關鍵選擇、店員/對方追問、客製需求、確認、付款或收尾、出錯補救。

內容配額（整副牌及每一批輸出都要盡量維持）：
1. 約 30% 是對方最常直接問使用者、使用者必須立刻聽懂的原話。
2. 約 30% 是使用者完成當下這一步所需的簡短回答或要求。
3. 約 20% 是高摩擦選擇：不知道選項差異、不知道怎麼回答、容易點錯或漏講的地方。
4. 約 20% 是臨場應對：聽不懂、要求重複、更改、缺貨、價格、限制、確認與補救。

每一項都必須符合：
1. word_en 必須是2至6詞的實用詞塊、動詞片語或慣用語，絕對不可是完整句、問句或回答。
2. sentence_en 必須是該場景中真人會直接說出口的話。禁止第三人稱介紹、品牌宣傳、背景知識或教科書說明。
3. sentence_en 必須合理應用 word_en，允許大小寫、標點及輕微詞形變化，寫成有真實情境和情緒的完整口語句，不能與詞塊相同。
4. 必須具體到真實選項、決策或操作；看到這張卡，使用者要立刻知道它解決哪個痛點。
5. 優先選擇高頻、搜尋意圖高、最值得收藏的實戰內容，不追求冷門詞彙或表面多樣性。
6. 若商品、規定或選項可能因地區而異，改寫成現場確認的自然問句，不可捏造一定存在的品項。
7. tips 提供語感、情緒微調或文化背景，不用「當…時，請…」模板。
8. word_cn 只翻譯詞塊本身；sentence_cn 翻譯整句情境，兩者必须不同；中文使用台灣繁體中文口語。
9. 必須為詞句生成精確完整的 IPA，並以斜線「/」包裹；絕對不能直接填英文拼寫。
10. 每張卡必須提供不同的實用學習價值。按實際溝通目的做語意去重，不可只比較字面或 purpose_id；
    店員問句與顧客回答、肯定與否定、一般選擇與具體客製需求不是重複。
    句型多樣性另行檢查：不同任務也不要只替換同一句的一個名詞或品項。
    保留各卡的原意、角色、必要關鍵詞與要求，改用不同自然句型或同義說法；
    例如不要同時寫 "Could I change the bread?" 與 "Could I change the sauce?"，
    後者可寫 "I'd prefer a different sauce."。不要為了多樣性改成冷門或不自然的英文。
    明確鎖定的英文仍須逐字遵守，不可擅自改寫。
11. word_en 最多 {MAX_WORD_EN_WORDS} 個英文單字；sentence_en 最多 {MAX_SENTENCE_EN_WORDS} 個英文單字。這是硬性上限，不得超過。
12. 每張卡只處理一個溝通目的，最多帶兩個選擇或條件。完整流程必須拆成多張連續短卡，禁止塞成一個長句。
13. tips 最多 {MAX_TIPS_CHARS} 個中文字元；sentence_cn 最多 {MAX_SENTENCE_CN_CHARS} 個字元，避免卡片爆版。
14. 若 assigned pain point 是「聽懂店員／對方問句」，sentence_en 寫店員原話，word_en 提煉其中連續詞塊；
    禁止寫 "I understand you asked..."、"Are you asking if..." 等學習旁白。

禁止內容：
- 主題的其他同名含義或相鄰但不屬於核心任務的場景。
- generic terms such as service, quality, safety, customer loyalty, daily routine, or other filler concepts unless the learner truly needs to say them on the spot.
- 只是在教一個簡單名詞怎麼念，卻沒有幫助使用者完成任務的內容。
- 看似相關但現場幾乎不會說的句子，以及同一句型只替換一個名詞的灌水項目。
- 寒暄、泛用道謝、詢問食材是否新鮮等低資訊句，除非它確實是該主題的主要痛點。
- 一口氣列出尺寸、品項、配料、醬料、付款等多個步驟的超長總結句。
{SEMANTIC_DUPLICATE_POLICY}
{CONTENT_RULES}

品質範例（只示範具體程度，不代表一定要生成餐飲內容）：
- 差：word_en = "Honey mustard"；sentence_en = "I want honey mustard, please." 這只是在背品名。
- 好：word_en = "just a little"；sentence_en = "Could you add just a little honey mustard?" 它解決醬料用量的需求。
- 差：word_en = "Can I change it?"；沒有說要改什麼。
- 好：word_en = "switch to wheat bread"；sentence_en = "Could I switch to wheat bread before you toast it?" 它包含修改內容和時機。

輸出前逐項自我檢查：如果學習者不能在「{topic}」現場直接說、直接回答或立刻聽懂這一項，就刪掉並換成更實用的內容。
"""


def _sales_communication_intent(item: dict) -> str:
    """Merge known sales-pressure paraphrases without depending on plan labels."""
    text = f"{item.get('word_en', '')} {item.get('sentence_en', '')}".casefold().replace("’", "'")
    return configured_communication_intent(text)


def _semantic_group_rejections(payload: dict, count: int, items: list[dict] | None = None) -> dict[int, str]:
    """Require a complete partition; enforce the cap outside model judgment."""
    if "assignments" in payload:
        assignments = payload["assignments"]
        if not isinstance(assignments, list) or len(assignments) != count:
            raise ValueError(f"語意 assignments 必須逐張列出剛好 {count} 項")
        by_purpose = {}
        for assignment in assignments:
            if not isinstance(assignment, dict) or assignment.get("level") not in ("basic", "advanced"):
                raise ValueError("語意 assignment 必須是物件且 level 為 basic 或 advanced")
            purpose = str(assignment.get("purpose", "")).strip()
            if items:
                try:
                    source_item = items[int(assignment.get("id")) - 1]
                except (TypeError, ValueError, IndexError) as exc:
                    raise ValueError("語意 assignment 編號無效") from exc
                purpose = _sales_communication_intent(source_item) or purpose
            key = _similarity_text(purpose)
            group = by_purpose.setdefault(key, {"purpose": purpose, "ids": [], "basic_id": None, "advanced_id": None})
            group["ids"].append(assignment.get("id"))
            level_key = assignment["level"] + "_id"
            if group[level_key] is None:
                group[level_key] = assignment.get("id")
                if assignment["level"] == "advanced":
                    group["pair_reason"] = str(assignment.get("progression", "")).strip()
        payload["groups"] = list(by_purpose.values())
    groups = payload.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("語意審稿必須回傳完整 groups 分組")
    seen: set[int] = set()
    purposes: set[str] = set()
    rejected: dict[int, str] = {}

    def card_id(value):
        if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
            raise ValueError("語意分組 id 必須是有效編號")
        result = int(value)
        if not 1 <= result <= count:
            raise ValueError("語意分組 id 超出牌組範圍")
        return result

    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("ids"), list) or not group["ids"]:
            raise ValueError("語意分組必須包含非空 ids 陣列")
        purpose = str(group.get("purpose", "")).strip()
        purpose_key = _similarity_text(purpose)
        if not purpose_key or purpose_key in purposes:
            raise ValueError("語意分組 purpose 不得空白或拆成同名多組")
        purposes.add(purpose_key)
        ids = [card_id(value) for value in group["ids"]]
        if len(set(ids)) != len(ids) or seen.intersection(ids):
            raise ValueError(f"每張卡必須剛好出現在一個語意分組，重複歸組編號 {ids}（先前已列 {sorted(seen)}）")
        seen.update(ids)
        keep = [card_id(group[key]) for key in ("basic_id", "advanced_id") if group.get(key) is not None]
        if not keep or len(set(keep)) != len(keep) or not set(keep).issubset(ids):
            raise ValueError("語意分組保留編號必須互異且屬於該組")
        if len(keep) == 2 and not str(group.get("pair_reason", "")).strip():
            raise ValueError("保留兩句必須說明簡單與進階的實際難度差異")
        for value in ids:
            if value not in keep:
                rejected[value - 1] = (
                    f"語意重複：{purpose}；同組 {','.join(f'{i:02d}' for i in ids)}，"
                    f"只保留 {','.join(f'{i:02d}' for i in keep)} 的一簡單一進階上限"
                )
    if seen != set(range(1, count + 1)):
        missing = sorted(set(range(1, count + 1)) - seen)
        raise ValueError(f"語意分組未涵蓋全牌組，漏列編號 {missing}；拒絕把遺漏卡片當成合格")
    return rejected


def _replace_duplicate_pain_points(
    topic: str, pain_points: list, items: list[dict], rejected: dict[int, str],
    reference_items: list[dict] | None = None,
) -> None:
    """Replace redundant jobs, not their wording; keep accepted jobs unchanged."""
    targets = [
        (items[idx]["_purpose_id"], reason)
        for idx, reason in rejected.items() if reason.startswith("語意重複：")
    ]
    if any(_exact_generation_point(pain_points[purpose_id - 1]) for purpose_id, _ in targets):
        raise RuntimeError("鎖定英文的溝通目的重複，需先調整人工策劃，拒絕擅自改寫")
    for start in range(0, len(targets), PLAN_GENERATE_BATCH):
        batch = targets[start:start + PLAN_GENERATE_BATCH]
        assigned = [{
            "id": purpose_id,
            "category": pain_points[purpose_id - 1]["category"],
            "role_type": pain_points[purpose_id - 1]["role_type"],
            "speaker": pain_points[purpose_id - 1]["speaker"],
            "rejected_task": pain_points[purpose_id - 1]["task"],
            "reason": reason,
        } for purpose_id, reason in batch]
        context = [{"id": idx + 1, "task": point["task"]} for idx, point in enumerate(pain_points)]
        prompt = f"""你是情境英語教材總編。主題：{topic}
{_topic_contract_text(pain_points)}
{SEMANTIC_DUPLICATE_POLICY}
以下任務已被語意分組審稿退回。不要改寫原句：將它們換成尚未涵蓋的實際溝通目的。
保留每個指定 id 與 category；新 task、intent、desired_outcome 必須產生不同的行動或決策結果。
不要再添加同義的報價、拒絕、術語解釋；可考慮不同的授權範圍、驗證方法、計費條件、
證據保全、個資與安全界線、具體補救與後續追蹤，但必須在題名契約範圍內。
全部是原指定的說話角色，每個 task 指導一句現場直接說出口的話，不是法律或背景教學。
既有全部任務（包含本輪其他新任務），不得重出：{json.dumps(context, ensure_ascii=False)}
{_reference_prompt_note(reference_items)}
需替換項目：{json.dumps(assigned, ensure_ascii=False)}
每項包含 id、category、scenario、speaker、intent、task、pain_trigger、user_stakes、desired_outcome、
role_type、failure_mode、priority、frequency、friction、sequence、required_terms。
只輸出 JSON，頂層 replacements 是包含完整欄位物件的陣列，不能省略欄位或使用省略號。
"""
        kwargs = {"messages": topic_messages(topic, prompt), "model": PLAN_MODEL,
                  "response_format": {"type": "json_object"}}
        kwargs["max_completion_tokens" if PLAN_MODEL.startswith("gpt-5") else "max_tokens"] = 6000
        if not PLAN_MODEL.startswith("gpt-5"):
            kwargs["temperature"] = 0.3
        expected = {purpose_id for purpose_id, _ in batch}
        for repair_attempt in range(3):
            response = _call_openai(stage=f"替換重複溝通目的 {start + 1}-{start + len(batch)}/{len(targets)}，嘗試 {repair_attempt + 1}/3", **kwargs)
            raw_content = response.choices[0].message.content
            try:
                replacements = json.loads(raw_content).get("replacements", [])
                pending = {}
                for raw in replacements:
                    purpose_id = int(raw.get("id", 0))
                    if purpose_id not in expected or purpose_id in pending:
                        raise ValueError("替換任務編號不符或重複")
                    point = _normalize_pain_point(raw, purpose_id - 1)
                    old = pain_points[purpose_id - 1]
                    if not point:
                        raise ValueError("替換任務缺少有效 task")
                    point.update(category=old["category"], role_type=old["role_type"], speaker=old["speaker"])
                    if any(not point[key] for key in ("scenario", "intent", "pain_trigger", "user_stakes", "desired_outcome", "failure_mode")):
                        raise ValueError("替換任務缺少具體痛點或結果")
                    others = [other for idx, other in enumerate(pain_points, 1) if idx not in expected]
                    conflict = next((other for other in others + list(pending.values()) if _pain_points_semantically_duplicate(point, other)), None)
                    if conflict:
                        raise ValueError(f"替換任務 {purpose_id}「{point['task']}」仍與「{conflict['task']}」重複；intent 和實際結果必須不同")
                    point.update(id=purpose_id, sequence=old["sequence"])
                    pending[purpose_id] = {key: value for key, value in point.items() if not key.startswith("_")}
                if set(pending) != expected:
                    raise ValueError("替換任務數量不足，拒絕留下缺漏藍圖")
                break
            except (ValueError, TypeError) as exc:
                if repair_attempt == 2:
                    raise
                _progress(f"替換任務未通過，要求重寫：{exc}")
                kwargs["messages"] = topic_messages(topic, prompt + f"\n前次未通過：{exc}。請重新輸出本批全部新任務，不可只補一項。\n前次內容：{raw_content}")
        for purpose_id, point in pending.items():
            pain_points[purpose_id - 1] = point


def _ai_review_deck(
    topic: str,
    items: list[dict],
    pain_points: list | None = None,
    reference_items: list[dict] | None = None,
) -> dict[int, str]:
    """Use a separate model as a conservative editorial quality gate."""
    if not items:
        return {}

    compact_items = []
    for idx, item in enumerate(items):
        purpose_id = item.get("_purpose_id", idx + 1)
        assigned_point = None
        if (
            pain_points
            and isinstance(purpose_id, int)
            and 1 <= purpose_id <= len(pain_points)
        ):
            assigned_point = _normalize_pain_point(pain_points[purpose_id - 1])
        compact_items.append({
            "id": f"{idx + 1:02d}",
            "purpose_id": purpose_id,
            "expected_role": assigned_point.get("role_type", "") if assigned_point else "",
            "expected_speaker": assigned_point.get("speaker", "") if assigned_point else "",
            "assigned_pain_point": _pain_point_text(assigned_point) if assigned_point else "",
            "word_en": item.get("word_en", ""),
            "word_cn": item.get("word_cn", ""),
            "tips": item.get("tips", ""),
            "sentence_en": item.get("sentence_en", ""),
            "sentence_cn": item.get("sentence_cn", ""),
        })
    blueprint_rule = ""
    if pain_points:
        blueprint_rule = """
每張卡已附上其唯一的 assigned_pain_point、expected_role 與 expected_speaker。
只依該卡附帶的指派判斷；若卡片偏離任務或說話角色，退回該卡。
"""
    contract_rule = _topic_contract_text(pain_points)
    if contract_rule:
        contract_rule = (
            "\n題名契約如下。即使卡片符合 assigned_pain_point，只要沒有直接服務"
            "核心痛點或落入禁止範圍，仍必須退回：\n" + contract_rule + "\n"
        )
    reference_rule = _reference_prompt_note(reference_items, for_review=True)
    duplicate_review_rule = (
        "3. 按實際溝通目的分組，同一意思最多一種簡單說法與一種進階說法。"
        "purpose_id 不同或已通過藍圖審稿，不代表語意不同；仍須退回超額或沒有難度差異的同義句。"
    )

    prompt = f"""你是獨立的情境英語牌組審稿人。主題是「{topic}」。
請只退回明顯不合格的卡片，不要因為初學或措辭可微調就退回。
{blueprint_rule}
{contract_rule}
{reference_rule}
{SEMANTIC_DUPLICATE_POLICY}
{CONTENT_RULES}

明顯不合格的定義：
1. 套用主題的其他同名含義、偏離核心任務、擴張到主題描述未涵蓋的受眾或場合，或是泛用填充內容。
2. 現場幾乎不會說、無法幫助使用者完成核心任務，或只是孤立品名教學。
{duplicate_review_rule}
4. 一張卡塞入超過兩個選擇／條件，應拆成多張短卡。
5. 明顯捏造品項、規定或事實；不確定的供應內容應使用詢問句。
6. 為了湊數加入低資訊邊角需求，例如泛用道謝、餐巾、餐具、切麵包邊、兒童份量、聯絡取餐等；除非主題明確指定。
7. {default_rule_text("card_review")}
8. 具體商品、配料或服務未出現在主題提供的事實中，卻直接假設一定供應。若它是核心痛點，必須改成現場確認問句。
9. 卡片沒有完成 assigned_pain_point 指定的任務，或用別的痛點內容佔位。
10. 英文不合文法、不自然、指涉不清，或為了壓短而省略必要動詞，例如 "Is avocado available and extra charge?"。
11. assigned_pain_point 指定店員問句時，卡片卻改寫成顧客旁白或 "I understand you asked..." 等教學敘述。
12. expected_role=learner_line 時，word_en 與 sentence_en 必須是學習者說的；
    expected_role=counterpart_line 時，兩者必須是對方說的。問句本身不代表 counterpart_line，
    例如學習者為了插話而問 "Can I jump in?" 仍是 learner_line。
13. 句型過度相似：兩張卡只替換一個名詞、品項，或整體仍是同一個填空句型。
    即使 purpose_id 不同，仍應退回較後的一張，kind 填 sentence_pattern。
    reason 必須指出相似的卡片及建議改寫方向：保留原意、角色與要求，改用自然同義說法或不同句型。
    例如 "Could I change the bread?" / "Could I change the sauce?"，可把後者改為 "I'd prefer a different sauce."。
    肯定與否定、不同說話角色、數量或程度不同的要求，不可只因部分用字相同就退回。
    同義改寫只適用於不同必要任務，或同一目的的一簡單一進階配對；不能靠改寫保留第三句。

字數及 IPA 的基本格式由程式檢查。你必須檢查詞塊完整性：sentence_en 是否合理應用 word_en，忽略大小寫與標點，允許時態、單複數等輕微詞形變化及合文法的可分離片語。不因 take 變 took 而退件；漏用詞塊或改變其核心意思則必須退件。仍須確認 word_en 不是完整句、sentence_en 是自然完整口語句，中文各自忠實翻譯，tips 確實提供語感、情緒微調或文化背景。

請以高訊號為原則：寧可退回低價值卡讓系統補寫，也不要為了湊滿數量放行邊角內容。

不要檢查 IPA。只輸出 JSON：
{{"reject": [{{"id": "01", "kind": "content 或 sentence_pattern 或 semantic_duplicate", "reason": "具體原因及改寫方向"}}]}}
若全部合格，輸出 {{"reject": []}}。

待審卡片：
{json.dumps(compact_items, ensure_ascii=False)}
"""
    try:
        request_kwargs = {
            "messages": topic_messages(topic, prompt),
            "model": REVIEW_MODEL,
            "response_format": {"type": "json_object"},
        }
        if not REVIEW_MODEL.startswith("gpt-5"):
            request_kwargs["temperature"] = 0.1
        response = _call_openai(stage=f"牌組內容審稿（{len(items)} 張）", **request_kwargs)
        raw_rejects = json.loads(response.choices[0].message.content).get("reject", [])

        duplicate_prompt = f"""你只負責檢查英語牌組內及其與參考牌組之間的語意重複。主題是「{topic}」。
{SEMANTIC_DUPLICATE_POLICY}
逐張閱讀全部卡片，先建立跨分類、跨 purpose_id 的語意群組，再決定退回項目。
同組最多保留一張最簡單直白與一張稍微進階的自然說法；沒有明顯難度差異則只保留一張。
每組第三張起一律退回；reason 要指出同組編號和實際溝通目的，kind 填 semantic_duplicate。
例如「分開包裝」與「兩份分開包」、「外帶切半」與「切半方便分享」各自算同一目的。
以下都不算重複，必須保留：
- 店員問 "Cash or card?" 與顧客答 "I'll pay by card."
- 店員問蔬菜選擇與顧客回答不要洋蔥。
- "Toast it"、"Don't toast it"、"Lightly toasted"，因答案或客製結果不同。
- 詢問有哪些選項、確認特定品項是否供應、實際選定其中一項。
- 一般選擇與少量、不要、另外裝等結果不同的客製要求。
不同步驟、不同回答方向或解決不同錯誤也不算重複。
參考牌組已經發布，不能退回參考牌組；若新卡與參考牌組重複，只退回新卡。
跨集不得重出已教的同義句；兩句上限只適用於當前牌組，不能作為跨集重出的理由。
{reference_rule}
必須按 id 順序逐張標記，不可只列重複句或只說沒有重複。
assignments 必須剛好 {len(items)} 項，編號 {', '.join(f'{i:02d}' for i in range(1, len(items) + 1))} 各出現一次。
每項 purpose 用不含場所、商品名稱的核心目的命名。同一目的逐字使用相同標籤，
不能把「要詳細報價」與「要書面估價」拆成不同標籤，也不可為了保留第三句拆成新標籤。
每項 level 為 basic 或 advanced；advanced 的 progression 說明真實的句法、片語或詞彙難度，
不是只加 please、thanks、禮貌程度、商品名稱或理由。basic 的 progression 填空字串。
只輸出 JSON：{{"assignments": [{{"id": "01", "purpose": "取得書面報價與明細", "level": "basic", "progression": ""}}, {{"id": "02", "purpose": "取得書面報價與明細", "level": "advanced", "progression": "使用片語 put in writing"}}], "reject": []}}。
reject 只用來退回與參考牌組重複的卡片；當前牌組上限由程式合併 purpose 標籤後計算。
卡片：{json.dumps([{key: item[key] for key in ('id', 'word_en', 'sentence_en', 'sentence_cn')} for item in compact_items], ensure_ascii=False)}
"""
        duplicate_kwargs = {
            "messages": topic_messages(topic, duplicate_prompt),
            "model": DUPLICATE_REVIEW_MODEL,
            "response_format": {"type": "json_object"},
        }
        if not DUPLICATE_REVIEW_MODEL.startswith("gpt-5"):
            duplicate_kwargs["temperature"] = 0.1
        for audit_attempt in range(3):
            duplicate_response = _call_openai(stage=f"牌組語意去重審稿 {audit_attempt + 1}/3", **duplicate_kwargs)
            raw_audit = duplicate_response.choices[0].message.content
            try:
                duplicate_payload = json.loads(raw_audit)
                if not isinstance(duplicate_payload, dict) or not isinstance(duplicate_payload.get("reject"), list):
                    raise ValueError("語意去重審稿必須回傳 reject 陣列")
                sales_items = items if get_topic_rules(topic).get("intent_rules") else None
                group_rejected = _semantic_group_rejections(duplicate_payload, len(items), sales_items)
                break
            except (ValueError, TypeError) as exc:
                if audit_attempt == 2:
                    raise
                _progress(f"語意審稿格式未通過，要求重審：{exc}")
                duplicate_kwargs["messages"] = topic_messages(topic, (
                    duplicate_prompt + f"\n前次回傳未通過完整性校驗：{exc}。"
                    "請重新輸出完整全部分組，不可只補缺漏組，不能把缺漏卡片隨意獨立成組。"
                    f"\n前次分組供修正：{raw_audit}"
                ))
        _progress(
            f"語意分組檢查：{len(items)} 張／{len(duplicate_payload['groups'])} 個目的；"
            f"退回 {len(group_rejected)} 張超額或無難度差異的同義句"
        )
        for idx, reason in group_rejected.items():
            raw_rejects.append({"id": idx + 1, "kind": "semantic_duplicate", "reason": reason.removeprefix("語意重複：")})
        for group in duplicate_payload["groups"]:
            for value in group["ids"]:
                items[int(value) - 1]["_semantic_group"] = group["purpose"]
        for assignment in duplicate_payload.get("assignments", []):
            items[int(assignment["id"]) - 1]["_semantic_level"] = assignment["level"]
        for entry in duplicate_payload["reject"]:
            if not isinstance(entry, dict):
                raise ValueError("語意去重退回項目必須是物件")
            try:
                rejected_id = int(str(entry.get("id", "")).strip())
            except (TypeError, ValueError) as exc:
                raise ValueError("語意去重退回項目必須有有效 id") from exc
            if not 1 <= rejected_id <= len(items):
                raise ValueError("語意去重退回 id 超出牌組範圍")
            raw_rejects.append({**entry, "kind": "semantic_duplicate"})
    except GenerationTimeoutError:
        raise
    except Exception as e:
        raise RuntimeError(f"自動審稿失敗，拒絕輸出未審核牌組: {e}") from e

    rejected: dict[int, str] = {}
    for entry in raw_rejects:
        try:
            idx = int(str(entry.get("id", "")).strip()) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(items):
            reason = str(entry.get("reason", "未通過內容審查")).strip()
            if entry.get("kind") == "semantic_duplicate":
                reason = "語意重複：" + reason.removeprefix("語意重複：")
            rejected[idx] = reason
    return rejected


def _review_deck(
    topic: str,
    items: list[dict],
    pain_points: list | None = None,
    reference_items: list[dict] | None = None,
) -> dict[int, str]:
    """Run the configured local, AI, or hybrid deck review."""
    _check_generation_deadline()
    if REVIEW_MODE == "off" or not items:
        return {}
    if REVIEW_MODE == "ai":
        if _has_complete_locked_blueprint(pain_points):
            local_rejected = _local_review_deck(topic, items, pain_points, reference_items)
            if local_rejected:
                return local_rejected
        pattern_rejected = {}
        for idx, item in enumerate(items):
            issue = _sentence_pattern_issue(item, items[:idx])
            if issue:
                pattern_rejected[idx] = issue
        if pattern_rejected:
            return pattern_rejected
        return _ai_review_deck(topic, items, pain_points, reference_items)

    local_rejected = _local_review_deck(
        topic, items, pain_points, reference_items
    )
    if REVIEW_MODE == "local" or (local_rejected and _has_complete_locked_blueprint(pain_points)):
        return local_rejected
    return {**local_rejected, **_ai_review_deck(topic, items, pain_points, reference_items)}


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
    files = [
        path
        for directory in (OUTPUT_DIR, CARDS_DIR)
        for path in glob.glob(os.path.join(directory, "*.xlsx"))
        if not os.path.basename(path).startswith("review_")
    ]
    topics = {
        os.path.splitext(os.path.basename(path))[0]
        for path in files
    }
    return sorted(topics)


def _topic_to_slug(topic: str) -> str:
    return topic.strip().replace(" ", "_").replace("-", "_")


def generate(
    topic: str,
    count: int = DEFAULT_CARD_COUNT,
    seed_items: list | None = None,
    pain_points: list | None = None,
    reference_items: list[dict] | None = None,
    checkpoint_path: str | None = None,
    resume: bool = False,
) -> list[dict]:
    _check_generation_deadline()
    reference_items = list(reference_items or [])
    if resume and checkpoint_path and os.path.isfile(checkpoint_path):
        with open(checkpoint_path, encoding="utf-8") as handle:
            checkpoint = json.load(handle)
        if (checkpoint.get("topic"), checkpoint.get("count"), checkpoint.get("version")) != (topic, count, PLAN_VERSION):
            raise ValueError("生成檢查點的主題、數量或版本不符，拒絕沿用")
        pain_points = PainPointPlan(checkpoint["pain_points"], contract=checkpoint["contract"])
        seed_items = checkpoint["items"]
        _progress(f"已恢復生成檢查點：{len(seed_items)}/{count} 張，接續審稿與補寫")
    if pain_points is None:
        pain_points = _plan_pain_points(topic, count, reference_items)
    elif pain_points:
        pain_points = _select_pain_points(pain_points, count)
    if pain_points and len(pain_points) != count:
        raise ValueError(f"指定的痛點藍圖必須剛好有 {count} 項，目前為 {len(pain_points)} 項")
    # Each generation call receives only its missing blueprint slice below.
    # The complete blueprint is reserved for the final reviewer.
    prompt = _build_prompt(topic, count)
    contract_note = _topic_contract_text(pain_points)
    if contract_note:
        prompt += (
            "\n題名契約是硬性內容邊界。每張卡都必須直接服務核心痛點，"
            "不得落入禁止範圍：\n" + contract_note + "\n"
        )
    prompt += _reference_prompt_note(reference_items)
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
        seed_purpose_id = int(item.get("_purpose_id", seed_idx + 1))
        if pain_points and not 1 <= seed_purpose_id <= len(pain_points):
            raise ValueError("生成檢查點的 purpose_id 超出藍圖範圍")
        seed_point = pain_points[seed_purpose_id - 1] if pain_points else None
        if seed_point and _exact_generation_point(seed_point):
            item = _correct_known_locked_pronunciations(item, seed_point)
            issue = _locked_item_issue(item, seed_point)
            if issue:
                print(f"   ⚠️ 移除未通過鎖定卡校驗的既有卡片: {issue}")
                continue
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
                seeded_item["_purpose_id"] = seed_purpose_id
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

    def save_checkpoint():
        if not checkpoint_path:
            return
        payload = {"version": PLAN_VERSION, "topic": topic, "count": count,
                   "pain_points": pain_points, "contract": getattr(pain_points, "contract", {}),
                   "items": all_items}
        with open(checkpoint_path + ".tmp", "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(checkpoint_path + ".tmp", checkpoint_path)

    completed_ids = {item.get("_purpose_id") for item in all_items}
    locked_targets = []
    for idx, point in enumerate(pain_points or []):
        purpose_id = idx + 1
        exact_point = _exact_generation_point(point)
        if exact_point and purpose_id not in completed_ids:
            locked_targets.append((purpose_id, exact_point))
    if locked_targets:
        print(f"   🔒 先生成 {len(locked_targets)} 張固定英文卡...")
        for start in range(0, len(locked_targets), 5):
            batch = locked_targets[start:start + 5]
            for item in _generate_locked_blueprint_items(topic, batch):
                purpose_id = int(item["purpose_id"])
                item["_purpose_id"] = purpose_id
                item["_pain_point"] = pain_points[purpose_id - 1]
                key = _normalize_key(item.get("word_en", ""))
                if not key or key in seen_normalized or _is_near_duplicate(item, all_items):
                    raise RuntimeError(
                        f"固定英文與牌組內既有內容重複: {item.get('word_en', '')}"
                    )
                reference_reason = _reference_duplicate_reason(
                    item, reference_items, item["_pain_point"]
                )
                if reference_reason:
                    raise RuntimeError(
                        f"固定英文與參考牌組重複: {item.get('word_en', '')}: "
                        + reference_reason
                    )
                all_items.append(item)
                seen_normalized.add(key)
            print(
                f"      ✅ 固定英文批次 {start // 5 + 1} 完成"
                f"（累計 {len(all_items)}/{count}）"
            )
            save_checkpoint()

    while rounds < max_rounds:
        _check_generation_deadline()
        if len(all_items) >= count:
            all_items = all_items[:count]
            if pain_points:
                all_items.sort(key=lambda item: item.get("_purpose_id", count + 1))
            save_checkpoint()
            print(
                f"   🔎 {REVIEW_MODE} 審稿 #{review_replacements + 1}"
                f"（{len(all_items)} 張）..."
            )
            rejected = _review_deck(
                topic, all_items, pain_points, reference_items
            )
            if not rejected:
                print("      ✅ 自動審稿通過")
                save_checkpoint()
                review_passed = True
                break
            for idx, reason in rejected.items():
                if "句型" in reason and pain_points:
                    purpose_id = all_items[idx].get("_purpose_id")
                    if (
                        isinstance(purpose_id, int) and 1 <= purpose_id <= len(pain_points)
                        and _exact_generation_point(pain_points[purpose_id - 1])
                    ):
                        raise RuntimeError(
                            f"第 {idx + 1:02d} 張鎖定英文未通過句型多樣性，"
                            f"需先調整策劃中的鎖定原話，拒絕擅自改寫或輸出: {reason}"
                        )
            if review_replacements >= MAX_REVIEW_REPLACEMENTS:
                details = "; ".join(
                    f"{idx + 1:02d}: {reason}" for idx, reason in sorted(rejected.items())
                )
                raise RuntimeError(f"牌組連續未通過自動審稿，拒絕輸出: {details}")

            if pain_points and any(reason.startswith("語意重複：") for reason in rejected.values()):
                _replace_duplicate_pain_points(topic, pain_points, all_items, rejected, reference_items)
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
            save_checkpoint()
            continue

        need = count - len(all_items)
        chunk_size = min(GENERATE_CHUNK, need)
        request_size = _generation_request_size(
            chunk_size, overgenerate_next_round
        )
        candidate_multiplier = max(1, request_size // max(chunk_size, 1))
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
            exclusion_note += (
                "\nSENTENCE DIVERSITY: Do not reuse an existing sentence template with only "
                "a noun or product swapped. Preserve the assigned task, speaker, meaning, "
                "and required terms, but use a different natural sentence structure or synonymous "
                "expression. Do not invent a different task just to vary the wording.\n"
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
                    + f"\nReturn {candidate_multiplier} materially different candidates "
                    "for EACH entry. Copy the entry number exactly into purpose_id for every "
                    "candidate. The system will validate them and keep one per entry. "
                    "If an entry lists 硬性英文關鍵詞, word_en or sentence_en must contain "
                    "at least half of those exact phrases (rounded up), verbatim.\n"
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
                f"Provide exactly {candidate_multiplier} candidates for EACH of the "
                f"{chunk_size} missing purposes. Make every alternative a genuinely different "
                "natural line; the system will keep the first one per purpose that passes validation.\n"
            )
        else:
            generation_note = (
                f"\nGenerate exactly {request_size} NEW items with distinct communicative purposes.\n"
            )

        role_note = ""
        learner_only = _normalize_topic_contract(
            getattr(pain_points, "contract", {})
        ).get("learner_only", False)
        if pain_points and learner_only:
            role_note = (
                "\nLEARNER-ONLY IS A HARD CONSTRAINT FOR BOTH word_en AND sentence_en: "
                "every field must be something the learner directly says in the assigned situation. "
                "Never write the counterpart's line. Learner questions are allowed when the assigned "
                "task asks the learner to request, clarify, interrupt, or respond.\n"
            )
        elif pain_points and get_topic_rules(topic).get("instructions", {}).get("card_roles"):
            role_note = "\n" + get_topic_rules(topic)["instructions"]["card_roles"] + "\n"
        elif pain_points:
            role_note = (
                "\nROLE IS A HARD CONSTRAINT FOR BOTH word_en AND sentence_en:\n"
                "- For counterpart_line, copy the complete English quote inside the assigned task "
                "exactly into sentence_en. word_en MUST be copied as an exact contiguous span of "
                "that sentence, so both fields have the same speaker. Never write the learner's "
                "answer or reaction.\n"
                "- For learner_line, write what the learner says to complete the assigned task.\n"
                "Never switch speaker perspective between word_en and sentence_en.\n"
            )

        locked_note = ""
        if remaining and any(
            _normalize_pain_point(point).get("target_phrase")
            for _, point in remaining[:chunk_size]
        ):
            locked_note = (
                "\nLOCKED ENGLISH IS NON-NEGOTIABLE: for every blueprint entry that includes "
                "a locked phrase and sentence, copy "
                "target_phrase exactly into word_en and target_sentence exactly into sentence_en. "
                "Do not paraphrase, shorten, expand, or switch pronouns. Only generate IPA, "
                "Traditional Chinese translations, and a concrete usage tip around those exact lines.\n"
            )

        full_prompt = (
            prompt
            + exclusion_note
            + purpose_note
            + generation_note
            + role_note
            + locked_note
            + FIELD_SPEC
        )
        try:
            request_kwargs = {
                "messages": topic_messages(topic, full_prompt),
                "model": CARD_MODEL,
                "response_format": {"type": "json_object"},
            }
            if not CARD_MODEL.startswith("gpt-5"):
                # GPT-5 models currently only support their default temperature.
                request_kwargs["temperature"] = 0.55 if len(all_items) > 0 else 0.4
            resp = _call_openai(stage=f"卡片生成（已 {len(all_items)}/{count}）", **request_kwargs)
            raw = _extract_generated_items(resp.choices[0].message.content)
            if not raw:
                print("      ⚠️ 模型未回傳可解析的 items 候選陣列")
        except GenerationTimeoutError:
            raise
        except Exception as e:
            consecutive_fails += 1
            if consecutive_fails >= 3:
                print("   ⚠️  連續失敗 3 次，中斷生成。")
                break
            _check_generation_deadline()
            deadline = _generation_deadline.get()
            delay = min(2, max(0, deadline - time.monotonic())) if deadline is not None else 2
            _progress(f"卡片生成失敗（第 {consecutive_fails}/3 次）：{e}；{delay:g} 秒後重試")
            time.sleep(delay)
            _check_generation_deadline()
            continue

        consecutive_fails = 0
        added = 0
        for item in raw:
            if len(all_items) >= count:
                break
            item = _apply_locked_blueprint_lines(item, pain_points)
            locked_source_mismatch = item.pop("_locked_source_mismatch", "")
            if locked_source_mismatch:
                print(
                    "      ⚠️ 跳過未逐字遵守鎖定英文的項目 "
                    f"{item.get('word_en', 'Unknown')}: {locked_source_mismatch}"
                )
                continue
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

                alignment_issue = _pain_point_alignment_issue(
                    item, pain_points[purpose_id - 1]
                )
                if alignment_issue:
                    print(
                        f"      ⚠️ 跳過角色不符的 purpose_id={purpose_id} 項目 "
                        f"{item.get('word_en', 'Unknown')}: {alignment_issue}"
                    )
                    continue

                candidate_rejected = _local_review_deck(
                    topic, [item], pain_points, reference_items
                )
                if candidate_rejected:
                    print(
                        f"      ⚠️ 跳過未通過本地審稿的 purpose_id={purpose_id} 項目 "
                        f"{item.get('word_en', 'Unknown')}: {candidate_rejected[0]}"
                    )
                    continue

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
        save_checkpoint()
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

    _check_generation_deadline()
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
    """Resolve an --avoid value as a path or a deck name in output/ or cards/."""
    raw = os.path.expanduser(value.strip())
    candidates = [raw]
    if not os.path.isabs(raw):
        candidates.extend([
            os.path.join(BASE_DIR, raw),
            os.path.join(OUTPUT_DIR, raw),
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
        f"找不到參考牌組「{value}」。請提供 output/ 或 cards/ 內的牌組名稱，或 XLSX 路徑"
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
            except PlanVersionError as exc:
                print(f"⚠️  忽略參考牌組的舊版策劃檔：{plan_path}: {exc}")
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
        description="生成初學者生存對話教材（預設 12 欄，情境均分、難度 60/40）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""範例：
  python3 cards.py --topic "美髮沙龍_03_剪壞補救" \\
    --description "只教剪髮中要求暫停、確認與修正的現場溝通" \\
    --avoid "美髮沙龍" --avoid "美髮沙龍_02_剪髮溝通" \\
    --review hybrid

  # 先只產生策劃檔，人工檢查後再生成卡片
  python3 cards.py --topic "租車英文" --description "取車、驗車、事故與還車" --plan-only
  python3 cards.py --topic "租車英文" --plan-file "output/租車英文.plan.json"

不帶參數執行時，仍會進入原本的互動模式。
""",
    )
    parser.add_argument(
        "--topic",
        help="牌組名稱，也是預設輸出檔名（不含 .xlsx）；主題規則自動讀取 topic_rules.json",
    )
    parser.add_argument(
        "--description",
        "--focus",
        dest="focus",
        default="",
        help="主題描述：目標對象、具體情境、核心痛點、必教內容與禁止範圍",
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
        help=f"卡片數量（預設 {DEFAULT_CARD_COUNT}，新版至少 3；60/40 取最接近整數）",
    )
    parser.add_argument(
        "--output",
        help="自訂 XLSX 輸出路徑；預設為 output/{topic}.xlsx",
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
    parser.add_argument(
        "--resume", action="store_true",
        help="接續策劃或教材檢查點；新版相同輸入重跑也會自動接續（--legacy 使用 .draft.json）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="明確重建教材；新版先備份既有 Excel，不會直接遺失舊教材",
    )
    parser.add_argument("--legacy", action="store_true",
                        help="使用舊版 8 欄痛點生成流程；預設使用通用 12 欄初學者教材")
    return parser


def _generation_topic(topic: str, focus: str = "") -> str:
    clean_focus = focus.strip()
    if not clean_focus:
        return topic
    return f"{topic}\n本集內容焦點與邊界：{clean_focus}"


def _prompt_topic_description() -> str:
    print(
        "\n📝 請補充主題描述（建議填寫目標對象、具體情境、核心痛點、"
        "必教內容與不要包含的內容）"
    )
    print("   貼上整段描述後，按 Enter 送出；直接 Enter 則略過。")
    print("   要手動換行可按 Esc 再按 Enter。")
    _progress("等待輸入：主題描述；尚未開始 AI 生成，描述送出並確認卡片數量後才會開始")
    bindings = KeyBindings()

    @bindings.add("enter")
    def submit(event):
        event.current_buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    def newline(event):
        event.current_buffer.insert_text("\n")

    # Bracketed paste inserts the whole clipboard without treating its newlines as Enter.
    try:
        description = terminal_prompt(
            "描述 > ", multiline=True, key_bindings=bindings,
        ).strip()
    except EOFError:
        description = ""
    _progress(f"描述輸入已完成（{len(description)} 字），接下來確認卡片數量")
    return description


def _parse_srt_starts(srt_path: str) -> list[float]:
    """讀取字幕的實際起始時間，供 YouTube 進度時間使用。"""
    if not os.path.exists(srt_path):
        return []
    starts: list[float] = []
    with open(srt_path, "r", encoding="utf-8") as f:
        for line in f:
            match = re.match(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->", line)
            if match:
                hours, minutes, seconds, milliseconds = map(int, match.groups())
                starts.append(hours * 3600 + minutes * 60 + seconds + milliseconds / 1000)
    return starts


def _youtube_scope_note(content_context: str) -> str:
    context = content_context.strip()
    if not context:
        return ""
    return (
        f"\n本集實際內容焦點如下：\n{context}\n"
        "標題、描述與 SEO 必須精確反映這個焦點，不得擴寫成泛用或相鄰主題。\n"
    )


def _youtube_content_context(
    topic_description: str,
    pain_points: list[dict] | None,
) -> str:
    if topic_description.strip():
        return topic_description.strip()
    return _topic_contract_text(pain_points)


def _generate_yt_title(topic: str, content_context: str = "") -> str:
    fallback_title = f"【日常英文】{topic} 英文懶人包｜14 天上手"
    prompt = (
        f"你是台灣 YouTube 英語教學頻道的標題撰稿人。"
        f"請為主題「{topic}」寫一句 YouTube 影片標題（繁體中文，25-40 字）。"
        f"{_youtube_scope_note(content_context)}"
        f"風格：吸睛、有痛點、含具體場景。"
        f"標題禁止出現 Rayo、智慧閃卡、Rayo 智慧閃卡、用 Rayo 智慧閃卡。"
        f"只輸出標題本身，不要引號、不要 hashtag、不要多行。"
    )
    try:
        resp = _call_openai(
            stage="YouTube 標題",
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4o-mini",
            temperature=0.9,
            max_tokens=80,
        )
        title = resp.choices[0].message.content.strip().strip('「」""\'\'')
        if title:
            title = re.sub(r"\s*[\|｜-]?\s*用?\s*Rayo\s*智慧閃卡.*$", "", title).strip()
            title = re.sub(r"\s*[\|｜-]?\s*Rayo\s*智慧閃卡.*$", "", title).strip()
        return title.splitlines()[0] if title else fallback_title
    except GenerationTimeoutError:
        raise
    except Exception as e:
        print(f"⚠️  OpenAI 生成 YouTube 標題失敗 ({e})，使用預設模板")
        return fallback_title


def _generate_yt_topic_paragraph(topic: str, content_context: str = "") -> str:
    prompt = (
        f"你是一位台灣 YouTube 英語教學頻道的文案寫手。"
        f"請為主題「{topic}」寫一段 YouTube 影片描述（約 100-150 字繁體中文）。"
        f"{_youtube_scope_note(content_context)}"
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
            stage="YouTube 描述",
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4o-mini",
            temperature=0.9,
            max_tokens=400,
        )
        return resp.choices[0].message.content.strip()
    except GenerationTimeoutError:
        raise
    except Exception as e:
        print(f"⚠️  OpenAI 生成 YouTube 描述失敗 ({e})，使用預設模板")
        return (
            f"想學「{topic}」實用英文卻不知從何開始？"
            f"這集為你準備了最實用的「{topic}」英文懶人包，"
            f"專為沒時間準備的零基礎新手設計。"
            f"搭配 Rayo 智慧閃卡與影子跟讀（Shadowing），"
            f"每天只需幾分鐘，把最實用的金句印在腦海裡！"
        )


def _generate_yt_hashtags(topic: str, content_context: str = "") -> list[str]:
    prompt = (
        f"為 YouTube 英語教學影片主題「{topic}」生成 8-12 個 SEO 標籤，"
        f"{_youtube_scope_note(content_context)}"
        f"涵蓋：繁體中文（如「{topic}英文、{topic}單字」等 2-4 個同義詞）、"
        f"簡體中文（2-3 個）、英文小寫（3-5 個，如「kitchen english、cooking vocabulary」風格）。"
        f'只輸出 JSON：{{"tags": ["tag1", "tag2", ...]}}，不要多餘文字。'
    )
    try:
        resp = _call_openai(
            stage="YouTube 標籤",
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
    except GenerationTimeoutError:
        raise
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


def write_youtube_description(
    topic: str,
    card_count: int,
    output_path: str,
    content_context: str = "",
):
    """產出含四個進度時間的描述；尚無字幕時先以 00:00 佔位。"""
    slug = _topic_to_slug(topic)
    srt_path = os.path.join(OUTPUT_DIR, f"final_{slug.lower()}.srt")
    srt_starts = _parse_srt_starts(srt_path)

    def progress_time(index: int) -> str:
        if not srt_starts:
            return "00:00"
        seconds = srt_starts[min(index, len(srt_starts) - 1)]
        return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"

    midpoint = card_count // 2
    ts_25 = progress_time(midpoint)
    ts_50 = progress_time(card_count)
    ts_75 = progress_time(card_count + midpoint)

    title = _generate_yt_title(topic, content_context)
    paragraph = _generate_yt_topic_paragraph(topic, content_context)
    topic_tags = _generate_yt_hashtags(topic, content_context)

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
        "00:00 開始學習！",
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


def write_xlsx(items: list[dict], path: str, *, learning: bool = False, curriculum_plan: dict | None = None):
    if not items:
        raise ValueError("拒絕輸出空牌組")

    headers = HEADERS
    if curriculum_plan is not None:
        from curriculum import MAIN_WORD_LIMIT, VIDEO_HEADERS, validate_deck
        validate_deck(items, curriculum_plan, reviewed=True)
        learning = True
        headers = VIDEO_HEADERS
    elif learning:
        from learning_editor import LEARNING_HEADERS, validate_pair
        headers = LEARNING_HEADERS
        groups = {}
        for item in items:
            if not item.get("core"):
                raise ValueError("學習版缺少核心概念")
            groups.setdefault(item["core"], []).append(item)
        for core, pair in groups.items():
            validate_pair({"core": core, "scenario": pair[0].get("Scenario")}, pair)

    deck_issues: list[str] = []
    accepted: list[dict] = []
    for idx, item in enumerate(items):
        expected_id = f"{idx + 1:02d}"
        if str(item.get("id", "")).strip() != expected_id:
            deck_issues.append(f"row {idx + 1}: id 應為 {expected_id}")
        for issue in _validation_issues(item, max_word_en_words=MAIN_WORD_LIMIT if curriculum_plan is not None else None,
                                       sentence_ipa_required=curriculum_plan is None):
            if learning and issue.startswith("tips has "):
                continue
            deck_issues.append(f"{expected_id}: {issue}")
        if not learning and _is_near_duplicate(item, accepted):
            deck_issues.append(f"{expected_id}: 與前面卡片近似重複")
        pattern_issue = _sentence_pattern_issue(item, accepted) if not learning else None
        if pattern_issue:
            deck_issues.append(f"{expected_id}: {pattern_issue}")
        accepted.append(item)

    if deck_issues:
        raise ValueError("牌組未通過寫檔校驗: " + "; ".join(deck_issues))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cards"

    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for item in items:
        ws.append([item.get(h, "") for h in headers])

    column_widths = {
        "A": 6,
        "B": 34,
        "C": 38,
        "D": 20,
        "E": 28,
        "F": 44,
        "G": 48,
        "H": 34,
    }
    if curriculum_plan is not None:
        column_widths = dict(zip("ABCDEFG", (6, 34, 38, 24, 30, 44, 34)))
    elif learning:
        column_widths = dict(zip("ABCDEFGHIJKL", (6, 24, 9, 9, 34, 38, 24, 30, 52, 44, 48, 34)))
    for column, width in column_widths.items():
        ws.column_dimensions[column].width = width

    last_column = "G" if curriculum_plan is not None else "L" if learning else "H"
    ws.freeze_panes = "B2" if curriculum_plan is not None else "E2" if learning else "A2"
    ws.auto_filter.ref = f"A1:{last_column}{ws.max_row}"
    ws.row_dimensions[1].height = 24
    for row_idx in range(2, ws.max_row + 1):
        max_lines = 1
        for col_idx, column in enumerate(column_widths, start=1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.alignment = Alignment(
                horizontal="center" if column == "A" else "left",
                vertical="top",
                wrap_text=True,
            )
            display_units = sum(
                2 if ord(char) > 127 else 1 for char in str(cell.value or "")
            )
            max_lines = max(
                max_lines,
                math.ceil(display_units / max(column_widths[column] - 2, 1)),
            )
        ws.row_dimensions[row_idx].height = min(
            max(45 if learning else 30, max_lines * 15 + (15 if learning else 0)), 75,
        )

    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:1"
    ws.print_area = f"A1:{last_column}{ws.max_row}"

    wb.save(path)


def main(argv: list[str] | None = None):
    token = _generation_deadline.set(None)
    try:
        with api_budget_scope():
            return _main(argv)
    finally:
        _generation_deadline.reset(token)


def _main(argv: list[str] | None = None):
    global REVIEW_MODE

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    cli_mode = bool(raw_argv)
    parser = _build_cli_parser()
    args = parser.parse_args(raw_argv)
    if cli_mode and not args.topic:
        parser.error("非互動模式必須提供 --topic")
    if args.review:
        REVIEW_MODE = args.review

    if not args.legacy:
        import curriculum
        return curriculum.run(args, parser, cli_mode)

    existing = _existing_topics()
    if existing:
        print(f"✅ 已有主題: {', '.join(existing)}")
    print(f"🔎 牌組審稿模式: {REVIEW_MODE}")

    topic = (args.topic or input("\n📌 請輸入主題名稱: ")).strip()
    if not topic:
        print("⛔ 主題不能為空")
        return

    topic_description = args.focus.strip() if cli_mode else _prompt_topic_description()
    generation_topic = _generation_topic(topic, topic_description)
    if topic_description:
        print("🎯 已套用主題描述，痛點策劃、生成與審稿都會依此限制範圍")

    slug = _topic_to_slug(topic)
    if args.output:
        xlsx_path = os.path.abspath(os.path.expanduser(args.output))
        if not xlsx_path.lower().endswith(".xlsx"):
            xlsx_path += ".xlsx"
    else:
        xlsx_path = os.path.join(OUTPUT_DIR, f"{slug}.xlsx")
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

    xlsx_exists    = os.path.exists(xlsx_path)
    yt_desc_exists = os.path.exists(yt_desc_path)

    if cli_mode:
        count = args.count
    else:
        _progress(f"等待輸入：卡片數量；按 Enter 使用預設 {DEFAULT_CARD_COUNT} 張並開始生成")
        raw_count = input(f"🔢 卡片數量（留空={DEFAULT_CARD_COUNT}）: ").strip()
        count = int(raw_count) if raw_count.isdigit() and int(raw_count) > 0 else DEFAULT_CARD_COUNT

    _generation_deadline.set(time.monotonic() + GENERATION_TIMEOUT)
    _progress(
        f"開始處理「{topic}」：{count} 張；每 {PROGRESS_INTERVAL:g} 秒回報等待狀態，"
        f"流程時間預算 {GENERATION_TIMEOUT:g} 秒（不含輸入時間）"
    )
    _progress("階段 1/3：載入或建立痛點策劃（含策劃審核），完成後才開始生成卡片")

    plan_path = (
        os.path.abspath(os.path.expanduser(args.plan_file))
        if args.plan_file
        else _default_plan_path(xlsx_path)
    )
    pain_points: list[dict] | None = None
    plan_was_created = False
    if os.path.isfile(plan_path):
        try:
            pain_points = _load_pain_point_plan(
                plan_path,
                expected_count=count,
                # A newly entered description must never reuse a stale plan.
                # With no description, keep supporting existing --plan-file flows.
                expected_topic=generation_topic if topic_description else None,
            )
        except PlanVersionError as exc:
            if args.plan_file:
                parser.error(f"策劃檔無法讀取：{plan_path}: {exc}")
            print(f"♻️  {exc}，正在依新版痛點契約重建：{plan_path}")
            pain_points = _plan_pain_points(
                generation_topic, count, reference_items
            )
            _save_pain_point_plan(generation_topic, pain_points, plan_path)
            plan_was_created = True
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            if args.plan_file:
                parser.error(f"策劃檔無法讀取：{plan_path}: {exc}")
            print(f"♻️  策劃未通過目前品質規則：{exc}，正在重建：{plan_path}")
            pain_points = _plan_pain_points(
                generation_topic, count, reference_items
            )
            _save_pain_point_plan(generation_topic, pain_points, plan_path)
            plan_was_created = True
        action = "已重建" if plan_was_created else "已載入"
        print(f"🗺️  {action}痛點策劃：{plan_path}")
    elif args.plan_file:
        parser.error(f"找不到 --plan-file：{plan_path}")
    elif not args.plan_file:
        pain_points = _plan_pain_points(generation_topic, count, reference_items)
        if not pain_points:
            parser.error("痛點策劃已停用，無法建立 plan 檔")
        _save_pain_point_plan(generation_topic, pain_points, plan_path)
        plan_was_created = True
        print(f"🗺️  已保存痛點策劃：{plan_path}")

    youtube_context = _youtube_content_context(topic_description, pain_points)
    _progress(f"階段 1/3 完成：痛點策劃共 {len(pain_points or [])} 項")

    if args.plan_only:
        if pain_points is None:
            pain_points = _plan_pain_points(generation_topic, count, reference_items)
            _save_pain_point_plan(generation_topic, pain_points, plan_path)
        print(f"✅ 策劃完成，共 {len(pain_points)} 個痛點；依 --plan-only 停止")
        _progress(f"流程已完成：策劃檔 {plan_path}（未執行卡片生成）")
        return

    _progress(f"階段 2/3：生成與 {REVIEW_MODE} 審稿，目標 {count} 張；退回的卡片會自動補寫")
    if xlsx_exists:
        existing_items = load_xlsx_items(xlsx_path)
        have = len(existing_items)
        rebuild_all = plan_was_created or args.force
        items = [] if rebuild_all else existing_items[:count]
        if rebuild_all:
            reason = "已指定 --force" if args.force else "新版痛點藍圖已建立"
            print(f"♻️  {reason}；不沿用舊卡片，將整副重新生成")
            have = 0
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
                checkpoint_path=xlsx_path + ".draft.json",
                resume=args.resume,
            )
            _progress(f"正在寫入 XLSX：{len(items)} 張 → {xlsx_path}")
            if pain_points and all(item.get("_pain_point") for item in items):
                pain_points = PainPointPlan([item["_pain_point"] for item in items], contract=pain_points.contract)
                _save_pain_point_plan(generation_topic, pain_points, plan_path)
            write_xlsx(items, xlsx_path)
            used_after = len(_load_used_words())
            print(f"\n✅ 已校驗並輸出 {len(items)} 個詞彙 → {xlsx_path}")
            print(f"📝 used_words.json 已更新（{used_before} → {used_after}）")

        _progress(f"階段 2/3 完成：{len(items)}/{count} 張已通過審稿，XLSX 已就緒")
        _progress("階段 3/3：處理 YouTube 標題、描述與標籤")
        if args.no_youtube:
            print("ℹ️  已依 --no-youtube 跳過 YouTube 描述")
        elif not yt_desc_exists:
            write_youtube_description(
                topic,
                len(items),
                yt_desc_path,
                content_context=youtube_context,
            )
        else:
            print(f"⚠️  YouTube 描述已存在，跳過：{yt_desc_path}")
        _progress(f"流程已完成：{xlsx_path}")
        return

    used_before = len(_load_used_words())
    print(f"\n🆕 開始生成「{topic}」({count} 個詞彙)...")
    items = generate(
        generation_topic,
        count,
        pain_points=pain_points,
        reference_items=reference_items,
        checkpoint_path=xlsx_path + ".draft.json",
        resume=args.resume,
    )

    if not items:
        _progress("流程失敗：未生成任何詞彙，未輸出 XLSX")
        print("❌ 未生成任何詞彙")
        return

    _progress(f"正在寫入 XLSX：{len(items)} 張 → {xlsx_path}")
    if pain_points and all(item.get("_pain_point") for item in items):
        pain_points = PainPointPlan([item["_pain_point"] for item in items], contract=pain_points.contract)
        _save_pain_point_plan(generation_topic, pain_points, plan_path)
    write_xlsx(items, xlsx_path)
    used_after = len(_load_used_words())
    print(f"\n✅ 已生成 {len(items)} 個詞彙 → {xlsx_path}")
    print(f"📝 used_words.json 已更新（{used_before} → {used_after}）")

    _progress(f"階段 2/3 完成：{len(items)}/{count} 張已通過審稿，XLSX 已就緒")
    _progress("階段 3/3：處理 YouTube 標題、描述與標籤")
    if args.no_youtube:
        print("ℹ️  已依 --no-youtube 跳過 YouTube 描述")
    else:
        write_youtube_description(
            topic,
            len(items),
            yt_desc_path,
            content_context=youtube_context,
        )
    _progress(f"流程已完成：{xlsx_path}")


if __name__ == "__main__":
    # Shared helpers must use this same module, deadline and review configuration.
    sys.modules["cards"] = sys.modules[__name__]
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⛔ 已取消，未繼續生成。")
        raise SystemExit(130)
    except GenerationTimeoutError as exc:
        _progress(f"逾時停止：{exc}")
        raise SystemExit(1)
    except CheckpointGenerationError as exc:
        _progress(f"生成停止：{exc}；請以相同輸入接續，勿加 --force。")
        raise SystemExit(1)
