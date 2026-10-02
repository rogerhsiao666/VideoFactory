"""Topic-independent survival flashcards with a code-owned curriculum contract."""

from __future__ import annotations

import hashlib
import copy
import json
import os
import re
import shutil
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import cards
from nltk.stem.snowball import EnglishStemmer
from opencc import OpenCC
from learning_editor import LEARNING_HEADERS, LEVELS, TONES


VERSION = 1
BATCH_SIZE = 8
MAIN_WORD_LIMIT = 12
SEMANTIC_REVIEW_VERSION = 8
MAX_ATTEMPTS = 3
AUTHOR_MODEL = os.getenv("OPENAI_CURRICULUM_AUTHOR_MODEL", "gpt-5-nano")
PLAN_MODEL = os.getenv("OPENAI_CURRICULUM_PLAN_MODEL", AUTHOR_MODEL)
REPAIR_MODEL = os.getenv("OPENAI_CURRICULUM_REPAIR_MODEL", AUTHOR_MODEL)
REVIEW_MODEL = os.getenv("OPENAI_CURRICULUM_REVIEW_MODEL", AUTHOR_MODEL)
SEMANTIC_MODEL = os.getenv("OPENAI_CURRICULUM_SEMANTIC_MODEL", "gpt-5-nano")
SEMANTIC_REASONING = os.getenv("OPENAI_CURRICULUM_SEMANTIC_REASONING", "low")
ADVANCED_MODEL = os.getenv("OPENAI_CURRICULUM_ADVANCED_MODEL", AUTHOR_MODEL)
VOCAB_STEMMER = EnglishStemmer()
TRADITIONAL_CHINESE = OpenCC("s2tw")
SEPARABLE_CORE_PHRASES = frozenset((
    "take out", "take off", "put on", "put in writing", "leave out", "turn on", "turn off",
    "pick up", "drop off", "fill out", "tone down", "write down", "break down", "bring up",
    "put aside", "set aside", "check in", "walk through", "talk through", "run through",
    "spell out", "point out", "sort out", "figure out", "hand over", "hand in", "give back",
    "give up", "fill in", "look up", "call off", "call back", "check out",
))
DIFFICULTY_POLICY = """難度統一標準：
basic：國中常用字、直白的簡單句，如 How long is the wait? / Can I pay by card?
advanced：仍短且實用，但含真正較進階的詞彙、慣用搭配、片語動詞或句法。
例如 happen to have（碰巧有）、leave out（省略）、on the side（分開放）、
split the bill（分開結帳）、walk me through（逐步解釋）、be required to（被要求）。
其他自然進階搭配如 take in the sights（遊覽景點）、be booked into（訂好住宿）、
subject to（須遵守／受…規範）；僅在符合原任務时使用，不要硬套。
常見的慣用片語也可算進階，不必使用冷門字；不是只換 Can/Could、加 please 或理由。
What ingredients are in this dish? 的 ingredients 是超出基本詞彙的具體用字，
但 Can I sit there?、Can I pay with a credit card? 不因指派 advanced 就算進階。
常見基本旅遊句型 check in、take out、fill out 不因是片語就自動算進階。
reservation、flight、luggage、boarding pass 等必要的常見旅遊詞不單獨構成進階依據。
簡單 confirm 問句、report that 後接日常簡單句，也不因字數或補充背景就自動算進階。
Can I check in online? 是 basic；Am I eligible for online check-in? 因 eligible 是 advanced。
point me to/toward（指引方向）、reconfirm（再次確認）是自然的進階搭配或詞彙。
進階依據要指出英文中的實際用字或結構，不可只填 basic/advanced 或『更自然』。
"""
PURPOSE_POLICY = """purpose 必須是『動作＋具體所需資訊或結果』，不能只是情境名。
例如詢問有無空位、詢問等待多久、要求靠窗座位、訂位是四個不同目的。
選定餐點、詢問推薦餐點、詢問成分、詢問有無素食選項是四個不同目的，
絕不能全部標成『選餐』；去掉洋蔥與調整辣度也是不同客製結果。
相反，要求書面報價、要求書面價格明細是同一目的，即使用字不同。
每張卡只處理一個現場溝通目的；不可合併兩個既有目的來假裝是新目的。
"""


class BatchGenerationError(RuntimeError):
    def __init__(self, items: list[dict], feedback: str):
        super().__init__("教材生成三次未通過本地校驗，已保留通過的卡片與修正回饋")
        self.items = items
        self.feedback = feedback


def request_json(prompt: str, stage: str, model: str, *, job_ids: list[str] | None = None,
                 anchors: dict[str, dict] | None = None) -> dict:
    kwargs = {"messages": [{"role": "system", "content":
              "You are a meticulous ESL curriculum editor. Follow the explicit contract and actual item data. "
              "Never copy placeholder labels, invent requirements, or mistake examples for the user's task."},
              {"role": "user", "content": prompt + "\nReturn valid JSON only."}],
              "model": model, "response_format": {"type": "json_object"}}
    kwargs["max_completion_tokens" if model.startswith("gpt-5") else "max_tokens"] = 10000
    if model.startswith("gpt-5") and stage in ("教材完整語意分組", "教材跨標籤同義複核"):
        kwargs["max_completion_tokens"] = 20000
    if not model.startswith("gpt-5"):
        kwargs["temperature"] = 0.2
    else:
        kwargs["reasoning_effort"] = (SEMANTIC_REASONING if stage == "教材完整語意分組"
                                      else "medium" if stage == "進階英文骨架" else "low")
    if stage == "主題情境策劃":
        kwargs["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "curriculum_scenarios", "strict": True, "schema": {
                "type": "object", "properties": {
                    "scenarios": {"type": "array", "items": {"type": "string"}}},
                "required": ["scenarios"], "additionalProperties": False}}}
    elif stage in ("逐句教材策劃", "替換重複教材目的"):
        fields = {name: {"type": "string"} for name in ("id", "core", "task", "role", "speaker")}
        fields["role"] = {"type": "string", "enum": ["learner", "counterpart"]}
        if job_ids:
            fields["id"] = {"type": "string", "enum": job_ids}
        kwargs["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "curriculum_jobs", "strict": True, "schema": {
                "type": "object", "properties": {"jobs": {"type": "array", "items": {
                    "type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}}},
                "required": ["jobs"], "additionalProperties": False}}}
    elif stage == "進階英文骨架":
        fields = {name: {"type": "string"} for name in ("id", "word_en", "sentence_en", "progression")}
        fields["id"]["enum"] = job_ids
        fields["word_en"]["description"] = f"A COMPLETE natural spoken English line, 2-{MAIN_WORD_LIMIT} words, genuinely intermediate grammar or vocabulary, fulfilling this assigned task. NEVER a lone word or fragment."
        fields["sentence_en"]["description"] = "A natural contextual example, maximum 14 words, SAME task and intermediate difficulty as word_en. Keep its real advanced construction or idiom, not a simplified can/could/need to version."
        fields["progression"]["description"] = "Cite the exact nontrivial English feature in word_en. Politeness, can/could/please, product names and added reasons alone do not count."
        kwargs["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "curriculum_advanced_lines", "strict": True, "schema": {
                "type": "object", "properties": {"lines": {"type": "array", "minItems": len(job_ids),
                    "maxItems": len(job_ids), "items": {"type": "object", "properties": fields,
                        "required": list(fields), "additionalProperties": False}}},
                "required": ["lines"], "additionalProperties": False}}}
    elif stage.startswith("教材生成 "):
        fields = {name: {"type": "string"} for name in (
            "id", "word_en", "word_ipa", "word_cn", "tips", "sentence_en", "sentence_ipa", "sentence_cn", "progression")}
        fields["Tone"] = {"type": "string", "enum": list(TONES)}
        descriptions = {
            "word_en": f"The MAIN complete spoken English sentence (maximum {MAIN_WORD_LIMIT} words), NEVER a vocabulary word or fragment. It must fulfill the assigned task at the assigned difficulty.",
            "word_ipa": "Full American IPA for EVERY word in word_en, enclosed in slashes.",
            "word_cn": "Natural Traditional Chinese translation of the complete word_en sentence.",
            "sentence_en": "A complete spoken paraphrase by the SAME speaker with the SAME intent, role, tone and difficulty (maximum 14 words). NEVER the listener's reply to word_en.",
            "sentence_ipa": "Full American IPA for EVERY word in sentence_en, enclosed in slashes.",
            "sentence_cn": "Natural Traditional Chinese translation of sentence_en.",
            "tips": "Traditional Chinese concrete usage timing, beginning with 當 and ending the timing clause with 時; then a practical action. Maximum 85 characters. No tone prefix or grammar explanation.",
        }
        for name, description in descriptions.items():
            fields[name]["description"] = description
        fields["progression"]["description"] = (
            "For advanced slots explain the actual English idiom, vocabulary or grammar used, "
            "e.g. '使用片語 leave out 表示省略配料'. Never return a tier label. For basic slots return an empty string.")
        fields["vocab"] = {"type": "array", "minItems": 1, "maxItems": 2, "items": {
            "type": "object", "properties": {"en": {"type": "string"}, "cn": {"type": "string"}},
            "required": ["en", "cn"], "additionalProperties": False}}
        vocab_schema = fields.pop("vocab")
        fields.pop("id")
        fields["vocab"] = {"$ref": "#/$defs/vocab"}
        keyed_items = {}
        for identifier in job_ids:
            item_fields = dict(fields)
            if anchors and identifier in anchors:
                phrase = anchors[identifier]["word_en"]
                item_fields["word_en"] = {"type": "string", "enum": [phrase]}
                if anchors[identifier].get("sentence_en"):
                    item_fields["sentence_en"] = {"type": "string", "enum": [anchors[identifier]["sentence_en"]]}
            keyed_items[identifier] = {"type": "object", "properties": item_fields,
                                       "required": list(item_fields), "additionalProperties": False}
        kwargs["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "curriculum_cards", "strict": True, "schema": {
                "type": "object", "$defs": {"vocab": vocab_schema},
                "properties": {"items": {"type": "object", "properties": keyed_items,
                    "required": list(keyed_items), "additionalProperties": False}},
                "required": ["items"], "additionalProperties": False}}}
    elif stage == "教材同義逐對複核":
        check = {"type": "object", "properties": {
            "equivalent": {"type": "boolean"}, "reason": {"type": "string"}},
            "required": ["equivalent", "reason"], "additionalProperties": False}
        kwargs["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "curriculum_pair_checks", "strict": True, "schema": {
                "type": "object", "properties": {"checks": {"type": "object", "properties": {
                    key: check for key in job_ids}, "required": job_ids, "additionalProperties": False}},
                "required": ["checks"], "additionalProperties": False}}}
    elif stage == "教材跨標籤同義複核":
        fields = {"ids": {"type": "array", "minItems": 2, "items": {"type": "string", "enum": job_ids}},
                  "purpose": {"type": "string"}}
        kwargs["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "curriculum_equivalent_groups", "strict": True, "schema": {
                "type": "object", "properties": {"groups": {"type": "array", "items": {
                    "type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}}},
                "required": ["groups"], "additionalProperties": False}}}
    elif stage in ("教材退回複核", "教材難度複核"):
        fields = {"id": {"type": "string", "enum": job_ids}}
        if stage == "教材退回複核":
            fields.update(valid={"type": "boolean"}, reason={"type": "string"})
        else:
            fields.update(level={"type": "string", "enum": ["basic", "advanced"]}, progression={"type": "string"})
        kwargs["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "curriculum_confirmation", "strict": True, "schema": {
                "type": "object", "properties": {"checks": {"type": "array", "minItems": len(job_ids),
                    "maxItems": len(job_ids), "items": {"type": "object", "properties": fields,
                        "required": list(fields), "additionalProperties": False}}},
                "required": ["checks"], "additionalProperties": False}}}
    elif stage in ("教材策劃獨立審查", "教材全欄位獨立審查", "教材完整語意分組"):
        identifier = {"type": "string", "enum": job_ids} if job_ids else {"type": "string"}
        rejection = {"type": "array", "items": {"type": "object", "properties": {
            "id": identifier, "reason": {"type": "string"}},
            "required": ["id", "reason"], "additionalProperties": False}}
        properties = {}
        if stage != "教材全欄位獨立審查":
            fields = {"id": identifier, "purpose": {"type": "string"}}
            fields["purpose"]["description"] = (
                "The specific action or information requested or provided by THIS card, not its scenario/topic. "
                "Use the same label only for equal meanings. Never return a placeholder such as 核心目的.")
            if stage == "教材策劃獨立審查":
                fields.update(in_scope={"type": "boolean"}, reason={"type": "string"})
            else:
                fields.update(level={"type": "string", "enum": ["basic", "advanced"]},
                              progression={"type": "string"})
                fields["level"]["description"] = "Actual difficulty of this entire CARD, NOT a comparison between its main line and example. Both fields must support an advanced rating. Do not trust author-assigned tiers."
                fields["progression"]["description"] = "For advanced, briefly cite the actual nontrivial idiom, grammar or vocabulary from word_en. For basic, briefly state why it is basic. Do not draft alternative sentences. Politeness, products and details do not count."
            properties["assignments"] = {"type": "array", "items": {
                "type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}}
            if job_ids:
                properties["assignments"].update(minItems=len(job_ids), maxItems=len(job_ids))
        if stage != "教材策劃獨立審查":
            properties["reject"] = rejection
        kwargs["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "curriculum_review", "strict": True, "schema": {
                "type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}}}
    if job_ids and stage in ("逐句教材策劃", "替換重複教材目的"):
        array = kwargs["response_format"]["json_schema"]["schema"]["properties"]["jobs"]
        array.update(minItems=len(job_ids), maxItems=len(job_ids))
    budget = min(300, max(cards.API_CALL_TIMEOUT, len(job_ids or []) * 5)) if stage in (
        "教材完整語意分組", "教材跨標籤同義複核") else None
    response = cards._call_openai(stage=stage, budget_seconds=budget, **kwargs)
    choice = response.choices[0]
    message = choice.message
    if getattr(message, "refusal", None):
        raise ValueError(f"{stage}：模型拒絕輸出教材")
    if getattr(choice, "finish_reason", "stop") != "stop":
        raise ValueError(f"{stage}：回應未完整結束（{choice.finish_reason}），不接受部分教材")
    if not isinstance(message.content, str) or not message.content.strip():
        raise ValueError(f"{stage}：API 回傳空內容，未產生教材")
    payload = json.loads(message.content)
    if not isinstance(payload, dict):
        raise ValueError("Curriculum response must be a JSON object")
    if stage.startswith("教材生成 ") and isinstance(payload.get("items"), dict):
        payload["items"] = [dict(item, id=identifier) for identifier, item in payload["items"].items()]
    return payload


def basic_count(count: int) -> int:
    if type(count) is not int or count < 3:
        raise ValueError("至少需要 3 句才能涵蓋 3 個真實情境")
    return (count * 3 + 2) // 5


def slots_for(count: int, scenarios: list[str]) -> list[dict]:
    basic = basic_count(count)
    if not isinstance(scenarios, list) or len(scenarios) not in (3, 4) or len(scenarios) > count:
        raise ValueError(f"必須有 3 至 4 個真實子情境；收到 {scenarios!r}")
    if any(not isinstance(name, str) or not name.strip() for name in scenarios):
        raise ValueError("情境名稱不能空白")
    if len({name.strip() for name in scenarios}) != len(scenarios):
        raise ValueError("情境名稱不能重複")
    sizes = [count // len(scenarios) + (index < count % len(scenarios))
             for index in range(len(scenarios))]
    quotas = [size * basic // count for size in sizes]
    order = sorted(range(len(sizes)), key=lambda i: (-(sizes[i] * basic % count), i))
    for index in order[:basic - sum(quotas)]:
        quotas[index] += 1
    slots = []
    for name, size, quota in zip(scenarios, sizes, quotas):
        for index in range(size):
            slots.append({"id": f"{len(slots) + 1:02d}", "Scenario": name,
                          "tier": "basic" if index < quota else "advanced"})
    return slots


def validate_plan(plan: dict, topic: str, count: int, *, semantic: bool = True) -> None:
    if not isinstance(plan, dict) or (plan.get("version"), plan.get("topic"), plan.get("count")) != (VERSION, topic, count):
        raise ValueError("策劃版本、主題描述或句數不符，不能沿用")
    slots = slots_for(count, plan.get("scenarios"))
    jobs = plan.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != count:
        raise ValueError("策劃必須逐句列齊全部任務")
    cores = defaultdict(list)
    for job, slot in zip(jobs, slots):
        if not isinstance(job, dict) or any(job.get(key) != value for key, value in slot.items()):
            raise ValueError("策劃編號、情境或難度配额被改動")
        for key in ("core", "task", "speaker"):
            if not isinstance(job.get(key), str) or not job[key].strip():
                raise ValueError(f"策劃缺少 {key}")
        if job.get("role") not in ("learner", "counterpart"):
            raise ValueError("策劃說話角色無效")
        if (cards._topic_requests_learner_only(topic) or re.search(
            r"(?:不收錄|不要收錄|不教|禁止).{0,12}(?:店員|對方).{0,8}原話", topic
        )) and job["role"] != "learner":
            raise ValueError("brief 要求只有學習者開口，不能加入對方原話")
        cores[cards._similarity_text(job["core"])].append(job)
    for core, group in cores.items() if semantic else ():
        if len(group) > 2 or (len(group) == 2 and {job["tier"] for job in group} != {"basic", "advanced"}):
            raise ValueError(f"{core}: 編號 {','.join(job['id'] for job in group)} 同意思最多一基礎、一進階；其餘改成新資訊或行動")


def parse_rejections(payload: dict, ids: set[str]) -> dict[str, str]:
    raw = payload.get("reject")
    if not isinstance(raw, list):
        raise ValueError("審稿缺少 reject 陣列，不能視為通過")
    result = {}
    for entry in raw:
        if not isinstance(entry, dict) or entry.get("id") not in ids or entry["id"] in result:
            raise ValueError("審稿編號缺漏、重複或超出範圍")
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("審稿退回項目缺少具體理由")
        result[entry["id"]] = reason
    return result


def plan_curriculum(topic: str, count: int, references: list[dict]) -> dict:
    basic_count(count)
    scenario_count = 4 if count >= 8 else 3
    reference_note = cards._reference_prompt_note(references)
    prompt = f"""你是 ESL 初學者課程總編。精確主題與使用者 brief：{topic}
拆成剛好 {scenario_count} 個真实、彼此不同的現場子情境，按實際流程排序。
不是開始/詢問/回答等抽象標籤，不借換場所或商品製造假多樣性。
每個情境必須足夠廣，可涵蓋 {((count + scenario_count - 1) // scenario_count)} 個不同現場任務。
例如『旅行目的與行程』可涵蓋目的、停留時間、目的地；只用『回答旅行目的』會太窄。
遵守 brief 的受眾、必教內容和禁止內容。總句數 {count}，情境數不能大於句數。
只輸出 {{"scenarios":["子情境名稱", "..."]}}，scenarios 是剛好 {scenario_count} 個名稱的字串陣列。
"""
    feedback = ""
    for attempt in range(MAX_ATTEMPTS):
        raw_jobs = None
        try:
            model = REPAIR_MODEL if attempt else PLAN_MODEL
            scenarios = request_json(prompt + feedback, "主題情境策劃", model).get("scenarios")
            slots = slots_for(count, scenarios)
            jobs_prompt = f"""You are an ESL curriculum planner. Exact topic and user brief: {topic}
Plan one DISTINCT concrete communication outcome per slot. Most outcomes should appear only once.
An optional same-meaning pair MUST consist of exactly one basic and one advanced slot.
Two basic slots must NEVER ask or answer the same underlying question.
Giving 'tourism' and 'business' as travel purposes both answer the SAME question; they are NOT distinct tasks.
Stating travel purpose, length of stay, destination, who you travel with, and first visit are distinct questions.
For a restaurant, choosing an order, asking recommendations, ingredients, and portion size are distinct questions.
Every slot's task must be achievable at its specified difficulty without contrived wording.
Cover natural requests, factual replies, practical decisions and problem resolution where relevant.
Do not fill the deck with vague permission questions such as whether the learner must answer questions.
Learner-only means the learner's spoken lines, including truthful replies to staff; it does not mean questions only.
{cards.SEMANTIC_DUPLICATE_POLICY}
{PURPOSE_POLICY}
{DIFFICULTY_POLICY}
按指定槽位規劃句子的現場 task、說話者 speaker、role(learner/counterpart)、core(實際目的)。
不要生成英文卡片。task 寫具體溝通結果，不是泛用教學指令。
同目的最多兩句，只有一 basic、一 advanced 才可配對；不是每組都要湊兩句。
同義配對使用同一 core；可以跨情境，但整副牌仍最多一 basic、一 advanced。
不同目的不可用同一 core；配對不能藉由換角色來改變目的。
基礎國中常用字、短且直接；進階真實片語或句法差異，不是加 please 或換商品。
保留 brief 的說話角色、必教英文與禁止內容。不要靠新理由、商品或文件同義詞湊數。
{reference_note}
槽位的情境與難度由程式指定，不能修改：{json.dumps(slots, ensure_ascii=False)}
同情境的兩張 basic 必須是不同目的，例如問有無空位與問需等待多久。
只輸出 {{"jobs":[{{"id":"01","core":"實際目的","task":"具體現場行動",
"role":"learner","speaker":"顧客"}}]}}，全部 {count} 項，不輸出 Scenario/tier，程式會依編號填入。
前次回饋：{feedback}
"""
            payload = request_json(jobs_prompt, "逐句教材策劃", model, job_ids=[slot["id"] for slot in slots])
            raw_jobs = payload.get("jobs")
            if not isinstance(raw_jobs, list) or len(raw_jobs) != count:
                raise ValueError("策劃必須完整列齊全部任務")
            by_id = {job.get("id"): job for job in raw_jobs if isinstance(job, dict)}
            if len(by_id) != count or set(by_id) != {slot["id"] for slot in slots}:
                raise ValueError("策劃編號重複、缺漏或超額")
            plan = {"version": VERSION, "topic": topic, "count": count,
                    "scenarios": scenarios, "jobs": [dict(by_id[slot["id"]], **slot) for slot in slots]}
            validate_plan(plan, topic, count, semantic=False)
            audit = request_json(
                f"獨立審核此 ESL 策劃是否符合精確主題：{topic}\n"
                + cards.SEMANTIC_DUPLICATE_POLICY + PURPOSE_POLICY + reference_note
                + "\n核對情境是否真實、tasks 是否偏題、跨 core 是否是假同義拆組。"
                "使用者 brief 比簡短題名優先；brief 明列的流程及其自然現場問題都在範圍內。"
                "此階段未生成英文原話，task 是中文教學策劃描述，不是拿給顧客照念的句子；"
                "不可因 task 用『詢問』『核對』『要求』就說顧客無法直接說。"
                "例如餐廳等位問有無空位、要等多久、索取菜單、問推薦或付款，都是可用英文直接開口的任務。"
                "不要退回真實高頻詢問或因可換措辭就退回；只報告明顯錯誤。"
                "此階段只按真正目的分組，不判斷兩句是否應保留；保留數量由程式處理。"
                "同一實際目的使用逐字相同 purpose，不用商品或場景換標籤。"
                "問有沒有空位、問等位多久、要求特定位置，是不同的實際結果。"
                "逐句列齊全部 assignments：id、purpose、in_scope（是否明顯符合主題）、reason。"
                "in_scope=false 僅限明顯違反 brief，不可因同義或難度理由設成 false。"
                "只輸出 {\"assignments\":[{\"id\":\"01\",\"purpose\":\"實際目的\","
                "\"in_scope\":true,\"reason\":\"\"}]}\n"
                + json.dumps(plan, ensure_ascii=False), "教材策劃獨立審查", REVIEW_MODEL,
                job_ids=[slot["id"] for slot in slots])
            assignments = audit.get("assignments")
            if not isinstance(assignments, list) or len(assignments) != count:
                raise ValueError("策劃独立分組未完整列齊")
            by_id = {entry.get("id"): entry for entry in assignments if isinstance(entry, dict)}
            if len(by_id) != count or set(by_id) != {slot["id"] for slot in slots}:
                raise ValueError("策劃独立分組有重複、缺漏或不明編號")
            for job in plan["jobs"]:
                assignment = by_id[job["id"]]
                if assignment.get("in_scope") is not True:
                    raise ValueError(str(assignment.get("reason", "任務未確認在主題範圍內")))
                purpose = assignment.get("purpose")
                if not isinstance(purpose, str) or not purpose.strip():
                    raise ValueError("策劃独立分組缺少實際目的")
                job["core"] = purpose.strip()
            prospective = [{"id": job["id"], "word_en": job["task"], "sentence_en": job["task"]}
                           for job in plan["jobs"]]
            grouped = {"assignments": [{"id": job["id"], "purpose": job["core"]} for job in plan["jobs"]]}
            verify_semantic_pairs(plan, prospective, grouped, {"groups": []})
            verified_cores = {entry["id"]: entry["purpose"] for entry in grouped["assignments"]}
            for job in plan["jobs"]:
                job["core"] = verified_cores[job["id"]]
            validate_plan(plan, topic, count)
            return plan
        except (ValueError, TypeError, KeyError) as exc:
            feedback = (f"\n前次不合格：{exc}，請重新輸出完整情境與策劃。"
                        + "\n以下是失敗策劃，必須修正而非照抄：" + json.dumps(raw_jobs, ensure_ascii=False))
            cards._progress(f"教材策劃退回 {attempt + 1}/{MAX_ATTEMPTS}：{exc}")
    raise RuntimeError("教材策劃三次未通過，拒絕輸出")


def vocab_occurs(term: str, spoken: str) -> bool:
    if f" {term} " in spoken:
        return True
    stemmed_term = " ".join(VOCAB_STEMMER.stem(word) for word in term.split())
    stemmed_spoken = " " + " ".join(VOCAB_STEMMER.stem(word) for word in spoken.split()) + " "
    if f" {stemmed_term} " in stemmed_spoken:
        return True
    if term not in SEPARABLE_CORE_PHRASES:
        return False
    verb, _, particle = stemmed_term.partition(" ")
    return bool(re.search(r"\b" + re.escape(verb) + r"(?:\s+\w+){1,4}\s+" + re.escape(particle) + r"\b", stemmed_spoken))


def validate_progression(progression: str) -> None:
    if not isinstance(progression, str) or progression.strip().casefold() in ("", "basic", "advanced"):
        raise ValueError("進階句必須說明實際詞彙或句法，不能只填難度標籤")
    reason = progression.casefold()
    modal_only = re.search(r"\b(?:can|could|please|may)\b", reason) and re.search(r"polite|禮貌|委婉", reason)
    actual_feature = (re.search(r"passive|被動|idiom|慣用|phrasal|片語|clause|子句|vocab|詞彙|mind|happen|embedded|嵌入|noun phrase|名詞組合", reason)
                      or explicit_advanced_features(reason))
    style_only = re.search(r"polite|formal|禮貌|委婉|正式", reason) and not actual_feature
    if (modal_only or style_only) and not actual_feature:
        raise ValueError("只把 can 換成 could/may 或加 please 不算進階；請用真正的詞彙、片語或句法")


def explicit_advanced_features(line: str) -> list[str]:
    text = cards._similarity_text(line)
    features = []
    constructions = {
        "mind + -ing": r"\bmind\s+(?:\w+\s+){0,3}\w+ing\b",
        "passive voice": r"\b(?:am|is|are|was|were|be|been)\s+(?:\w+\s+){0,2}(?:required|assigned|issued|seated|equipped|permitted|expected|delivered|provided|reserved|arranged|sent|shown|upgraded|carried|exchanged|returned|repaired)\b",
        "would appreciate it if": r"\b(?:would|d)\s+(?:greatly\s+)?appreciate\s+it\s+if\b",
        "equipped with": r"\bequipped\s+with\b",
        "happen to": r"\bhappen\s+to\b",
        "subject to": r"\bsubject\s+to\b",
        "walk someone through": r"\bwalk\s+(?:me|us|you)\s+through\b",
        "guide someone through": r"\bguide\s+(?:me|us|you)\s+through\b",
        "point someone to": r"\bpoint\s+(?:me|us|you)\s+(?:to|toward|towards)\b",
        "tone down": r"\btone\s+(?:\w+\s+){0,3}down\b",
        "spell out": r"\bspell\s+(?:\w+\s+){0,3}out\b",
        "on the side": r"\bon\s+the\s+side\b",
        "split the bill": r"\bsplit\s+the\s+bill\b",
        "set aside": r"\bset\s+(?:\w+\s+){0,3}aside\b",
        "on hand": r"\bon\s+hand\b",
    }
    for feature, pattern in constructions.items():
        if re.search(pattern, text):
            features.append(feature)
    vocabulary = {VOCAB_STEMMER.stem(word) for word in ("ingredient", "eligible", "eligibility", "qualify", "reconfirm", "clarify", "undergo", "restriction", "verify", "accurate", "commence", "obligated", "outline", "surcharge", "applicable")}
    features.extend("vocabulary: " + word for word in text.split() if VOCAB_STEMMER.stem(word) in vocabulary)
    return features


def normalize_chinese(item: dict) -> dict:
    normalized = copy.deepcopy(item)
    for field in ("word_cn", "sentence_cn", "tips", "Core_Vocab"):
        if isinstance(normalized.get(field), str):
            normalized[field] = TRADITIONAL_CHINESE.convert(normalized[field])
    for entry in normalized.get("vocab", []) if isinstance(normalized.get("vocab"), list) else ():
        if isinstance(entry, dict) and isinstance(entry.get("cn"), str):
            entry["cn"] = TRADITIONAL_CHINESE.convert(entry["cn"])
    return normalized


def validate_item(item: dict, job: dict) -> None:
    if not isinstance(item, dict) or any(item.get(key) != job[key] for key in ("id", "Scenario", "tier", "core")):
        raise ValueError("卡片不能改動指派編號、情境、難度或核心目的")
    if item.get("Level") != LEVELS[job["tier"]] or item.get("Tone") not in TONES:
        raise ValueError("難度或語氣標籤無效")
    issues = [issue for issue in cards._validation_issues(item, max_word_en_words=MAIN_WORD_LIMIT)
              if not issue.startswith("tips has ")]
    if issues:
        raise ValueError("；".join(issues))
    if normalize_chinese(item) != item:
        raise ValueError("中文輸出必須統一使用繁體中文")
    for field in ("word_en", "sentence_en"):
        if re.search(r"\brecheck[ -]?in\b", item[field], re.I):
            raise ValueError(f"{field} 再次辦理登機手續請用 check in again，不用 recheck in")
        if re.search(r"\bi (?:need|want) to (?:say|show you)\b", item[field], re.I):
            raise ValueError(f"{field} 必須直接說出現場資訊，不要描述自己需要說話或展示")
    main = cards._similarity_text(item["word_en"])
    example = cards._similarity_text(item["sentence_en"])
    listener_request = re.search(r"^(?:could|can|would|will) you\b|\b(?:walk|guide) me through\b", main)
    listener_reply = re.search(r"^(?:yes|sure|of course|absolutely) i (?:can|will|ll|would)\b|\bi can (?:walk|guide) you through\b", example)
    if listener_request and listener_reply:
        raise ValueError("sentence_en 不能是店員對 word_en 的回答；必須保持原說話者提出相同請求")
    if any("ɒ" in item[field] for field in ("word_ipa", "sentence_ipa")):
        raise ValueError("請用一般美式 IPA；ɒ 是此處的英式記法，依實詞改用 ɑ 或 ɔ")
    tip = item["tips"]
    if not tip.startswith(item["Tone"] + "：") or len(tip) > 90:
        raise ValueError("Tips 必須以語氣加冒號開頭，且不超過 90 字")
    if not re.search(r"(?:當|對方|看到|聽到|發現|遇到|準備|詢問|回答|要求|需要|想要|確認|時)", tip):
        raise ValueError(f"{job['id']} Tips {tip!r} 缺少具體現場使用時機；請用『當…時，…』描述")
    vocab = item.get("vocab")
    if not isinstance(vocab, list) or not 1 <= len(vocab) <= 2:
        raise ValueError("必須提供 1 至 2 個核心單字或片語")
    spoken = [" " + cards._similarity_text(item[field]) + " " for field in ("word_en", "sentence_en")]
    for entry in vocab:
        if not isinstance(entry, dict) or not isinstance(entry.get("en"), str) or not isinstance(entry.get("cn"), str):
            raise ValueError("核心單字缺少英文或繁體中文")
        term = cards._similarity_text(entry["en"])
        if not term or len(term.split()) > 4 or not any(vocab_occurs(term, line) for line in spoken) or not re.search(r"[\u4e00-\u9fff]", entry["cn"]):
            raise ValueError(f"{job['id']} 核心詞 {entry['en']!r} 必須出現在英文中（允許字形變化與可分離片語），附中文且至多4詞")
    if item.get("Core_Vocab") != "；".join(entry["en"] + " " + entry["cn"] for entry in vocab):
        raise ValueError("Core_Vocab 與核心單字不一致")
    if job["tier"] == "advanced":
        validate_progression(item.get("progression"))
    else:
        for field in ("word_en", "sentence_en"):
            features = explicit_advanced_features(item[field])
            if features:
                raise ValueError(f"{field} 必須保持基礎難度，請用 Can I / Do I need / 簡單直述句，不用 " + ", ".join(features))
    point = {"target_phrase": item["word_en"], "target_sentence": item["sentence_en"],
             "task": item["sentence_en"], "category": job["Scenario"]}
    ipa_issue = cards._locked_item_issue(dict(item, tips="現場提示"), point, max_word_en_words=MAIN_WORD_LIMIT)
    if ipa_issue:
        raise ValueError(ipa_issue)


def validate_deck(items: list[dict], plan: dict, *, reviewed: bool = False) -> None:
    validate_plan(plan, plan["topic"], plan["count"])
    if len(items) != plan["count"]:
        raise ValueError("輸出句數與請求不一致")
    lines = set()
    semantic = defaultdict(list)
    for item, job in zip(items, plan["jobs"]):
        validate_item(item, job)
        key = cards._spoken_line_key(item["word_en"])
        if key in lines:
            raise ValueError("同一句英文不能重複")
        lines.add(key)
        if reviewed:
            if not item.get("_semantic_group") or item.get("_semantic_level") != job["tier"]:
                raise ValueError("独立語意審稿的難度或群組尚未通過")
            if item.get("_semantic_review_version") != SEMANTIC_REVIEW_VERSION:
                raise ValueError("卡片尚未通過現行逐對同義複核")
            semantic[item["_semantic_group"]].append(item)
    for group in semantic.values():
        if len(group) > 2 or (len(group) == 2 and {item["tier"] for item in group} != {"basic", "advanced"}):
            raise ValueError("跨標籤同義句超額或缺乏難度差異")


def generate_batch(plan: dict, jobs: list[dict], accepted: list[dict], feedback: str = "") -> list[dict]:
    advanced = [job for job in jobs if job["tier"] == "advanced"]
    anchors = {}
    draft_failures = {}
    if advanced:
        try:
            previous = json.loads(feedback)
        except (ValueError, TypeError):
            previous = []
        if isinstance(previous, list):
            draft_feedback = [{"id": row["id"], "reason": re.split(
                r"advanced equivalent|advanced alternative|suggestion|進階建議", str(row.get("reason", "")), flags=re.I)[0][:400]}
                for row in previous if isinstance(row, dict) and row.get("id") in {job["id"] for job in advanced}]
        elif isinstance(previous, dict) and isinstance(previous.get("issues"), dict):
            draft_feedback = [{"id": identifier, "reason": str(reason)[:400]}
                              for identifier, reason in previous["issues"].items()
                              if identifier in {job["id"] for job in advanced}]
        else:
            draft_feedback = []
        draft_base = ("Draft BOTH word_en and sentence_en for each assigned task at genuinely intermediate difficulty. "
            "Both must use an actual idiom, phrasal verb, richer vocabulary or advanced grammar, "
            f"not just can/could/please or added context. Maximum {MAIN_WORD_LIMIT} English words per line. "
            "sentence_en may use up to 14 words for natural context. Prefer common spoken idioms or "
            "clear intermediate grammar such as mind + -ing or passive voice, not obscure formal words. "
            "For requests, prefer mind + -ing or an actual passive construction when appropriate. "
            "Do not use plain need to, be able to, or would it be possible as the only advancement. "
            "Keep the actual advanced feature in BOTH English fields. Both are locked for later IPA drafting. "
            "Preserve the exact task intent and speaker; avoid invented facts or rules. "
            "sentence_en is another line spoken by the SAME learner, NOT the staff member's reply. "
            "If word_en asks 'Could you walk me through...?', the example must also ask for help, "
            "never answer 'Yes, I can walk you through...'. "
            "Do not omit necessary articles or determiners to meet the word limit. "
            "Use a different grammatical structure if needed for natural, correct short English. "
            "Examples: 'Can I check in online?' is too basic; 'Am I eligible for online check-in?' is advanced. "
            "For an onward boarding pass, 'Could my onward boarding pass be issued?' is natural advanced English; "
            "'boarding pass for connection' is ungrammatical without a determiner. "
            "Ignore any English wording in task if it is too long or too simple; convey its intended outcome. "
            "Do not simplify the example's eligible/required/mind construction into could you confirm or need to. "
            "In progression cite the EXACT feature present in the line.\n" + DIFFICULTY_POLICY
            + "\nTopic: " + plan["topic"]
            + "\nPrevious objective issues (do NOT copy prior failed sentences): " + json.dumps(draft_feedback, ensure_ascii=False))
        pending_drafts = list(advanced)
        for attempt in range(MAX_ATTEMPTS):
            draft_prompt = (draft_base + "\nTasks: " + json.dumps(pending_drafts, ensure_ascii=False)
                + "\nPrevious draft issues: " + json.dumps(draft_failures, ensure_ascii=False))
            if attempt:
                draft_prompt += ("\nThe retry MUST change the actual English in BOTH fields. For a request/information task, "
                    "use a natural mind + gerund, walk me through, clarify, or an actual be required/issued construction. "
                    "Do NOT resubmit a plain Should I, What steps should I take, or Could you tell me line. "
                    "For guidance, 'Could you walk me through reporting a missing item?' has a real idiom. "
                    "For a rule, 'Am I required to pack these items separately?' has actual passive obligation. "
                    "For documentation, 'Which documents am I required to have on hand?' is genuine intermediate English. "
                    "At flight connections say check in again, NOT recheck in again. Keep the exact assigned outcome.")
            try:
                draft = request_json(draft_prompt, "進階英文骨架", ADVANCED_MODEL if not attempt else REPAIR_MODEL,
                                     job_ids=[job["id"] for job in pending_drafts])
                lines = draft.get("lines")
                candidates = {line.get("id"): line for line in lines if isinstance(line, dict)} if isinstance(lines, list) else {}
                if len(candidates) != len(pending_drafts) or len(lines or []) != len(pending_drafts) or set(candidates) != {job["id"] for job in pending_drafts}:
                    raise ValueError("進階骨架缺漏、重複或超出指定編號")
                draft_failures = {}
                draft_validated = {}
                for identifier, line in candidates.items():
                    try:
                        if not isinstance(line.get("word_en"), str) or not 2 <= cards._english_word_count(line["word_en"]) <= MAIN_WORD_LIMIT:
                            raise ValueError(f"進階骨架須是2至{MAIN_WORD_LIMIT}詞的完整現場句：{line.get('word_en')!r}")
                        if not isinstance(line.get("sentence_en"), str) or not 2 <= cards._english_word_count(line["sentence_en"]) <= cards.MAX_SENTENCE_EN_WORDS:
                            raise ValueError("進階例句須是2至14詞的完整現場句")
                        main_features = explicit_advanced_features(line["word_en"])
                        example_features = explicit_advanced_features(line["sentence_en"])
                        if main_features and example_features:
                            line["progression"] = ("word_en: " + ", ".join(main_features)
                                                   + "; sentence_en: " + ", ".join(example_features))
                        validate_progression(line.get("progression"))
                    except (ValueError, TypeError) as exc:
                        draft_failures[identifier] = str(exc)
                    else:
                        draft_validated[identifier] = line
                if draft_validated:
                    field_issues = review_field_difficulty([dict(line, tier="advanced") for line in draft_validated.values()])
                    for identifier, line in draft_validated.items():
                        if identifier in field_issues:
                            draft_failures[identifier] = field_issues[identifier]
                        else:
                            anchors[identifier] = line
                pending_drafts = [job for job in pending_drafts if job["id"] not in anchors]
                if not pending_drafts:
                    break
                cards._progress(f"進階骨架保留 {len(anchors)}/{len(advanced)} 句，重試：" + json.dumps(draft_failures, ensure_ascii=False))
            except (ValueError, TypeError, KeyError) as exc:
                draft_failures = {job["id"]: str(exc) for job in pending_drafts}
                cards._progress(f"進階骨架退回 {attempt + 1}/{MAX_ATTEMPTS}：{exc}")
    ready_jobs = [job for job in jobs if job["tier"] == "basic" or job["id"] in anchors]
    draft_feedback = json.dumps({"stage": "advanced_draft", "issues": draft_failures}, ensure_ascii=False)
    if not ready_jobs:
        raise BatchGenerationError([], draft_feedback)
    retained_lines = [{key: item[key] for key in ("id", "word_en", "sentence_en")} for item in accepted]
    prompt = f"""You are an ESL survival-dialogue author. Exact topic and user brief: {plan['topic']}
Write ONLY the assigned batch, exactly one card per target ID.
Each card must accomplish its OWN assigned task and speaker role. Do not replace the task.
Basic slots: use plain middle-school vocabulary and simple direct sentences.
Basic examples MUST keep simple grammar too. Never turn 'Do I need to' into 'Am I required to',
or 'Can I' into 'Could you verify/provide'. Add concrete context using the same simple grammar.
Ask directly for the needed information. Prefer 'When does boarding start?' over 'Can I know the boarding time?'.
Do not write vague permission-to-ask lines such as 'Can I ask about baggage claim?' for a task asking its location.
Advanced slots: use a natural idiom, phrasal verb, richer vocabulary, or genuinely more advanced grammar.
For example a seat request can use 'Would you mind seating us over there?' (mind + -ing),
and a card-payment question can use 'Do you happen to take credit cards?' (happen to).
These are illustrations, not required topic-specific phrases. Never just label a simple line advanced.
If feedback includes a failed line, change the deficient wording instead of repeating it.
Do NOT copy a retained card. The remaining slots are NOT requests to reprint the whole deck.
{cards.SEMANTIC_DUPLICATE_POLICY}
{PURPOSE_POLICY}
{DIFFICULTY_POLICY}
按指定 task 和說話者寫可立即使用的完整英文短句，不是名詞或學習旁白。
必須忠實完成原 task 的意思，不必照抄 task 的英文；超長英文任務必須重新措辭。
例如靠窗座位可用被動式 Could we be seated by the window?（8詞），
調低辣度可用片語 Could you tone down the spice?（7詞）。
審稿回饋不是照抄先前失敗句的要求；必須依具體理由修正英文。
basic 僅用國中程度的日常詞，最短直接說法；advanced 用自然片語或較進階句法。
word_en 最多 {MAIN_WORD_LIMIT} 個單字，sentence_en 最多 14 個單字。例句補現場條件，不改意思或角色。
不得捏造規定或保證食物過敏安全。保留 brief 的必教句、對象與禁止內容。
美式 IPA 完整逐字對應，斜線包裹；中文用台灣繁體中文日常口語。
Tone 按真實措辭選委婉/中立/強硬；短句和例句語氣一致，不把普通問句標成委婉。
tips 寫具體使用時機及現場動作，最多 85 字，不加語氣前綴；程式會填入 Tone。
vocab 提煉實際出現在英文中的 1-2 個核心單字或片語，每項 en/cn；不要整句。
If a core vocabulary term is missing after simplifying the English, replace the vocab entry.
NEVER add a harder word back into a basic sentence merely to match an old vocabulary entry.
For a BASIC ingredients task, use 'What is in this product?' with vocab 'product', NOT 'ingredients'.
advanced 的 progression 寫具體進階用法，不能只是加 please 或補理由；basic 填空字串。
progression 範例："使用片語 on the side 表示醬料分開放"；絕不可填 "advanced"。
保留指派 id。Scenario、tier、core、Level、Core_Vocab 由程式填入，不要輸出。
每項欄位：id, Tone, word_en, word_ipa, word_cn, tips,
sentence_en, sentence_ipa, sentence_cn, vocab, progression。
sentence_en 不是對 word_en 的回答。两個欄位都由同一個人說；請求的例句仍須是請求。
已保留卡片不可重出：{json.dumps(retained_lines, ensure_ascii=False)}
前次退回回饋：{feedback}
進階英文骨架：{json.dumps(list(anchors.values()), ensure_ascii=False)}
進階卡的 word_en 和 sentence_en 必須逐字使用此骨架的兩個英文欄位，不要在填 IPA 時改寫。
progression 使用其具體依據；兩句已在起稿階段自然改寫，且都會獨立審稿。
只輸出 items 物件，以指定 id 為鍵，每項包含上述欄位但不重複輸出 id。
"""
    pending = list(ready_jobs)
    completed = {}
    last_feedback = ""
    for attempt in range(MAX_ATTEMPTS):
        try:
            model = REPAIR_MODEL if attempt or feedback else AUTHOR_MODEL
            batch_prompt = prompt + "\n本次只輸出以下尚未通過的任務：" + json.dumps(pending, ensure_ascii=False)
            if last_feedback:
                batch_prompt += ("\nLATEST correction, overriding earlier failed drafts: " + last_feedback
                    + "\nFix ONLY unresolved errors. Preserve the assigned tier. Change vocabulary entries to "
                    "match simplified English, not the other way around. Never reintroduce an advanced word into BASIC.")
            batch_prompt += "\n本批已通過不可再輸出：" + json.dumps(
                [{"id": item["id"], "word_en": item["word_en"]} for item in completed.values()], ensure_ascii=False)
            payload = request_json(batch_prompt, f"教材生成 {pending[0]['id']}-{pending[-1]['id']}", model,
                                   job_ids=[job["id"] for job in pending], anchors=anchors)
            raw = payload.get("items")
            if not isinstance(raw, list) or len(raw) != len(pending):
                raise ValueError("本批卡片必須完整列齊")
            by_id = {}
            for item in raw:
                if not isinstance(item, dict) or item.get("id") in by_id:
                    raise ValueError("本批卡片編號重複或格式錯誤")
                by_id[item.get("id")] = item
            if set(by_id) != {job["id"] for job in pending}:
                raise ValueError("本批卡片編號缺漏或超額")
            issues = []
            remaining = []
            for job in pending:
                item = by_id[job["id"]]
                item.update({key: job[key] for key in ("id", "Scenario", "tier", "core")})
                item["Level"] = LEVELS[job["tier"]]
                item = normalize_chinese(item)
                if item.get("Tone") in TONES and isinstance(item.get("tips"), str):
                    body = re.sub(r"^(?:委婉|中立|強硬)\s*[:：]\s*", "", item["tips"].strip())
                    item["tips"] = item["Tone"] + "：" + body
                if isinstance(item.get("vocab"), list):
                    item["Core_Vocab"] = "；".join(entry.get("en", "") + " " + entry.get("cn", "")
                                                       for entry in item["vocab"] if isinstance(entry, dict))
                point = {"target_phrase": item.get("word_en", ""), "target_sentence": item.get("sentence_en", ""),
                         "task": item.get("sentence_en", ""), "category": job["Scenario"]}
                item = cards._correct_known_locked_pronunciations(item, point)
                try:
                    if job["id"] in anchors:
                        anchor = anchors[job["id"]]
                        if cards._spoken_line_key(item.get("word_en", "")) != cards._spoken_line_key(anchor["word_en"]):
                            raise ValueError("word_en 必須保留已指定進階骨架：" + anchor["word_en"])
                        if cards._spoken_line_key(item.get("sentence_en", "")) != cards._spoken_line_key(anchor["sentence_en"]):
                            raise ValueError("sentence_en 必須保留已指定進階例句：" + anchor["sentence_en"])
                        item["progression"] = anchor["progression"]
                    validate_item(item, job)
                    if any(cards._spoken_line_key(item["word_en"]) == cards._spoken_line_key(old["word_en"])
                           for old in accepted + list(completed.values())):
                        raise ValueError(f"{job['id']} 的 {item['word_en']!r} 與已保留句子完全重複；必須完成本項自己的 task")
                except (ValueError, TypeError, KeyError) as exc:
                    issues.append(f"{job['id']}: {exc}；失敗英文：{item.get('word_en')!r} / {item.get('sentence_en')!r}")
                    remaining.append(job)
                else:
                    completed[job["id"]] = item
            if not remaining:
                if draft_failures:
                    raise BatchGenerationError(list(completed.values()), draft_feedback)
                return [completed[job["id"]] for job in jobs]
            pending = remaining
            raise ValueError("；".join(issues))
        except (ValueError, TypeError, KeyError) as exc:
            last_feedback = str(exc)
            cards._progress(f"教材本地校驗退回 {attempt + 1}/{MAX_ATTEMPTS}：{exc}")
    raise BatchGenerationError(list(completed.values()), last_feedback + ("\n" + draft_feedback if draft_failures else ""))


def reconcile_semantic_groups(plan: dict, semantic: dict) -> None:
    assignments = {entry["id"]: entry for entry in semantic["assignments"]}
    parents = {identifier: identifier for identifier in assignments}

    def root(identifier):
        while parents[identifier] != identifier:
            parents[identifier] = parents[parents[identifier]]
            identifier = parents[identifier]
        return identifier

    for field, rows in (("core", plan["jobs"]), ("purpose", semantic["assignments"])):
        first = {}
        for row in rows:
            key = cards._similarity_text(row[field])
            identifier = row["id"]
            if key in first:
                parents[root(identifier)] = root(first[key])
            else:
                first[key] = identifier
    labels = {}
    for entry in semantic["assignments"]:
        entry["purpose"] = labels.setdefault(root(entry["id"]), entry["purpose"])


def merge_equivalent_groups(semantic: dict, payload: dict) -> None:
    groups = payload.get("groups")
    if not isinstance(groups, list):
        raise ValueError("跨標籤同義複核缺少 groups 陣列")
    by_id = {entry["id"]: entry for entry in semantic["assignments"]}
    for group in groups:
        identifiers = group.get("ids") if isinstance(group, dict) else None
        if (not isinstance(identifiers, list) or len(identifiers) < 2
                or any(not isinstance(identifier, str) or identifier not in by_id for identifier in identifiers)
                or len(set(identifiers)) != len(identifiers)
                or not isinstance(group.get("purpose"), str) or not group["purpose"].strip()):
            raise ValueError("跨標籤同義組缺少明確目的，或編號缺漏、重複、超額")
        labels = {cards._similarity_text(by_id[identifier]["purpose"]) for identifier in identifiers}
        label = by_id[identifiers[0]]["purpose"]
        for entry in semantic["assignments"]:
            if cards._similarity_text(entry["purpose"]) in labels:
                entry["purpose"] = label


def verify_semantic_pairs(plan: dict, items: list[dict], semantic: dict, equivalents: dict) -> None:
    candidates = copy.deepcopy(semantic)
    merge_equivalent_groups(candidates, equivalents)
    reconcile_semantic_groups(plan, candidates)
    groups = defaultdict(list)
    for entry in candidates["assignments"]:
        groups[cards._similarity_text(entry["purpose"])].append(entry["id"])
    pair_set = {tuple(sorted((left, right))) for identifiers in groups.values()
                for index, left in enumerate(identifiers) for right in identifiers[index + 1:]}
    stopwords = {"i", "you", "we", "my", "your", "our", "me", "us", "this", "that", "the", "a", "an",
                 "can", "could", "may", "will", "would", "should", "must", "do", "does", "did", "am", "is",
                 "are", "was", "were", "be", "to", "need", "have", "it", "there", "possible", "please",
                 "what", "which", "where", "when", "how", "many", "much", "about", "for", "of", "in", "on"}
    actions = defaultdict(set)
    for item in items:
        for field in ("word_en", "sentence_en"):
            content = [token for token in cards._similarity_text(item[field]).split() if token not in stopwords]
            if content and content[0].isascii() and content[0].isalpha():
                actions[VOCAB_STEMMER.stem(content[0])].add(item["id"])
    for identifiers in actions.values():
        ordered = sorted(identifiers)
        pair_set.update((left, right) for index, left in enumerate(ordered) for right in ordered[index + 1:])
    pairs = sorted(pair_set)
    by_id = {item["id"]: item for item in items}
    parents = {identifier: identifier for identifier in by_id}

    def root(identifier):
        while parents[identifier] != identifier:
            parents[identifier] = parents[parents[identifier]]
            identifier = parents[identifier]
        return identifier

    for start in range(0, len(pairs), 32):
        batch = pairs[start:start + 32]
        keys = [left + ":" + right for left, right in batch]
        targets = [{"key": key, "a": {field: by_id[left][field] for field in ("word_en", "sentence_en")},
                    "b": {field: by_id[right][field] for field in ("word_en", "sentence_en")}}
                   for key, (left, right) in zip(keys, batch)]
        checks = request_json(
            "For EACH pair independently determine whether both cards accomplish the SAME concrete "
            "communication outcome. These are CANDIDATES, not known synonyms. "
            "Equivalent=true only when they request/provide the same action and information/result. "
            "A shared topic, venue, verb or category is NOT sufficient. "
            "Checking a reservation NAME versus DATES is different information: false. "
            "Requesting a double bed versus a quiet room changes different room attributes: false. "
            "Room facilities, price, breakfast, air-conditioning repair and towels are all distinct: false. "
            "Arranging checked baggage versus asking its current status is false: action versus progress information. "
            "Explaining security procedures versus asking whether extra checks apply is false. "
            "Requesting written quotes versus written pricing details is true. "
            "Permission to bring an item versus medicine/liquids is true when only the object changes; "
            "asking permission versus asking a quantity limit is false. "
            "Removing an item versus a laptop for screening is true; removing shoes is a different action. "
            "Gate number versus walking directions is false. Ignoring wording/difficulty/tone, give a brief "
            "reason naming the actual result of EACH card. Never refer to another pair. "
            "Return checks keyed by the exact pair key, each with equivalent boolean and reason.\n"
            + json.dumps(targets, ensure_ascii=False), "教材同義逐對複核", REVIEW_MODEL, job_ids=keys).get("checks")
        if not isinstance(checks, dict) or set(checks) != set(keys):
            raise ValueError("逐對同義複核漏列或多列候選配對")
        for key, (left, right) in zip(keys, batch):
            check = checks[key]
            if (not isinstance(check, dict) or type(check.get("equivalent")) is not bool
                    or not isinstance(check.get("reason"), str) or not check["reason"].strip()):
                raise ValueError("逐對同義複核缺少明確判定或具體理由")
            if check["equivalent"]:
                parents[root(right)] = root(left)
    names = {entry["id"]: entry["purpose"] for entry in semantic["assignments"]}
    for entry in semantic["assignments"]:
        representative = root(entry["id"])
        entry["purpose"] = representative + ": " + names[representative]


def confirm_difficulty(mismatches: list[dict], *, model: str | None = None) -> list[dict]:
    prompt = ("Independently classify the ACTUAL difficulty of EACH SINGLE English line_en. "
        "These lines have no main/example role and are NOT paired. IDs and order are arbitrary. "
        "No author-assigned levels are supplied. Classify only the words and grammar actually present. "
        "Apply the explicit policy, not sentence length or politeness. "
        "Examples: 'Am I required to show my ID?' uses passive obligation and is advanced. "
        "'Could you point me to baggage claim?' uses the idiomatic point me to and is advanced. "
        "'Can I check in online?' is basic. 'Would it be possible to check in online?' "
        "is not advanced just for politeness. 'reconfirm' is richer vocabulary than 'check'. "
        "'I would like to know my gate number', 'Could you tell me the time?', and 'Is it okay for me to wait?' "
        "are BASIC. Could/should/will/have to and simple indirect questions do not alone make a line advanced. "
        "'Will I have to pay for my checked luggage?' is BASIC: future obligation and checked luggage are common travel English. "
        "For advanced quote the actual nontrivial phrase, vocabulary or grammar in this line. "
        "For basic briefly explain why. NEVER draft alternative English; do not try to make every line advanced. "
        "NEVER output N/A or a tier label "
        "as progression; each check needs concrete English evidence.\n"
        + DIFFICULTY_POLICY + "\n" + json.dumps(mismatches, ensure_ascii=False))
    ids = {item["id"] for item in mismatches}
    completed = {}
    errors = {}
    for attempt in range(MAX_ATTEMPTS):
        try:
            pending_ids = ids - set(completed)
            response = request_json(prompt + "\nReview ONLY these IDs: " + json.dumps(sorted(pending_ids)),
                                    "教材難度複核", (model or SEMANTIC_MODEL) if not attempt else REVIEW_MODEL,
                                    job_ids=sorted(pending_ids))
            checks = response.get("checks")
            by_id = {entry.get("id"): entry for entry in checks if isinstance(entry, dict)} if isinstance(checks, list) else {}
            if len(by_id) != len(pending_ids) or len(checks or []) != len(pending_ids) or set(by_id) != pending_ids:
                raise ValueError("難度複核編號缺漏、重複或超額")
            for entry in checks:
                try:
                    reason = entry.get("progression")
                    if (entry.get("level") not in ("basic", "advanced") or not isinstance(reason, str)
                            or reason.strip().casefold() in ("", "n/a", "na", "none", "basic", "advanced", "not applicable")):
                        raise ValueError("難度複核缺少實際依據，不能用 N/A 或難度標籤")
                    if entry["level"] == "advanced":
                        validate_progression(reason)
                except (ValueError, TypeError) as exc:
                    errors[entry["id"]] = str(exc) + ": " + str(entry.get("progression", ""))
                else:
                    completed[entry["id"]] = entry
            if set(completed) == ids:
                return [completed[item["id"]] for item in mismatches]
            raise ValueError(json.dumps({key: value for key, value in errors.items() if key not in completed}, ensure_ascii=False))
        except (ValueError, TypeError, KeyError) as exc:
            cards._progress(f"難度複核格式退回 {attempt + 1}/{MAX_ATTEMPTS}：{exc}")
            prompt += f"\nCorrect this response error: {exc}"
    return [completed.get(item["id"], {"id": item["id"], "level": "basic", "_valid": False,
            "progression": errors.get(item["id"], "難度複核三次未提供完整具體依據")}) for item in mismatches]


def review_field_difficulty(items: list[dict]) -> dict[str, str]:
    # Each English field is reviewed alone so one hard sentence cannot mask a basic one.
    targets = []
    owners = {}
    line_ids = {}
    rejected = {}
    for item in items:
        for field in ("word_en", "sentence_en"):
            line = item[field]
            features = explicit_advanced_features(line)
            if features:
                if item["tier"] == "basic":
                    rejected[item["id"]] = f"{field} 超出基礎難度：" + ", ".join(features)
            else:
                key = cards._spoken_line_key(line)
                if key not in line_ids:
                    identifier = f"F{len(line_ids) + 1:03d}"
                    line_ids[key] = identifier
                    owners[identifier] = []
                    targets.append({"id": identifier, "line_en": line})
                owners[line_ids[key]].append((item, field))
    for start in range(0, len(targets), 32):
        for check in confirm_difficulty(targets[start:start + 32]):
            for item, field in owners[check["id"]]:
                if check.get("_valid") is False:
                    reason = f"{field} 未能核定難度，請改用明確符合 {item['tier']} 的措辭：{check['progression']}"
                    rejected[item["id"]] = rejected.get(item["id"], "") + reason + "；"
                elif check["level"] != item["tier"]:
                    reason = f"{field} 實際為 {check['level']}，須改成 {item['tier']}；依據：{check['progression']}"
                    rejected[item["id"]] = rejected.get(item["id"], "") + reason + "；"
    return rejected


def review_task_fidelity(plan: dict, items: list[dict]) -> dict[str, str]:
    jobs = {job["id"]: job for job in plan["jobs"]}
    rejected = {}
    for start in range(0, len(items), 16):
        batch = items[start:start + 16]
        targets = [{"id": item["id"], "role": jobs[item["id"]]["role"],
                    "speaker": jobs[item["id"]]["speaker"], "task": jobs[item["id"]]["task"],
                    **{key: item[key] for key in ("word_en", "sentence_en", "word_cn", "sentence_cn")}}
                   for item in batch]
        response = request_json(
            "Independently compare the literal communication outcome of EACH card's two English lines "
            "and their Chinese translations against its task and speaker. Check ONLY meaning and role. "
            "Both English lines must accomplish the SAME information/action, with accurate translations. "
            "Do not infer a missing information request from shared vocabulary. "
            "'Would you mind my electronics going in a separate bag?' asks permission, NOT whether separate "
            "packing is required. Its Chinese must not say 我需要分袋嗎. "
            "Asking quantity limits versus required documents is different information: reject. "
            "Asking duty-free allowances versus what to declare is different information: reject. "
            "'Please declare these items' orders the listener to declare; it does NOT ask staff how the "
            "traveler should declare them. Reject that role reversal. "
            "'Could you walk me through filing a complaint?' and 'Yes, I can walk you through filing "
            "a complaint' are DIFFERENT SPEAKERS: requester versus helper. Always reject this reply-as-example. "
            "'Walk me through' asks for sequential steps, NOT a current status or estimated waiting time. "
            "For a waiting-time task, clarify the remaining wait instead. "
            "Do not invent airport procedure such as baggage claim necessarily being after customs. "
            "Natural yes/no questions and direct statements are valid. 'Am I eligible for online check-in?' "
            "asks whether the speaker can check in online; it does NOT ask for a list of conditions. "
            "A polite request for a seat and a simple 'Can I get that seat?' have the same practical outcome. "
            "The example may add context, not switch the information, actor, quantity, or required action. "
            "For EACH ID return valid=true only when all four fields match the same task and role. "
            "For false name the exact discrepancy and correction; do NOT judge difficulty, tone or IPA.\n"
            + json.dumps({"brief": plan["topic"], "cards": targets}, ensure_ascii=False),
            "教材退回複核", REVIEW_MODEL, job_ids=[item["id"] for item in batch])
        checks = response.get("checks")
        by_id = {entry.get("id"): entry for entry in checks if isinstance(entry, dict)} if isinstance(checks, list) else {}
        if len(checks or []) != len(batch) or set(by_id) != {item["id"] for item in batch}:
            raise ValueError("溝通目的複核缺漏、重複或超額")
        for identifier, check in by_id.items():
            if check.get("valid") is True:
                continue
            if check.get("valid") is not False or not isinstance(check.get("reason"), str) or not check["reason"].strip():
                raise ValueError("溝通目的複核缺少明確判定或理由")
            rejected[identifier] = check["reason"]
    return rejected


def review_deck(plan: dict, items: list[dict], references: list[dict]) -> dict[str, str]:
    audit = request_json(
        f"You are an independent ESL copy editor. Exact topic and brief: {plan['topic']}\n"
        "Check grammar, natural spoken English, Traditional Chinese translation, complete American IPA, "
        "tone, actionable tips, vocabulary, and faithful completion of the assigned task and role. "
        "Reject ONLY objective errors, not stylistic preferences. Both English fields must fulfill the task. "
        "Both English fields must convey the SAME result; context may be added but the result cannot change. "
        "A double room (usually one double bed) and a twin room (two single beds) are not interchangeable. "
        "A correct short line does not need to name a specific product; the example may add that detail. "
        "The main line must itself ask/provide the assigned information, not merely ask permission to ask. "
        "Natural yes/no questions, requests, statements, and 'I'm wondering whether...' are valid "
        "when they actually convey the task; a What/When/Where question is NOT mandatory. "
        "Reject 'I need to say there is no hot water' as metalinguistic narration; use a direct report. "
        "For checking in again use 'check in again', never 'recheck in again'. "
        "For a location task 'Can I ask about baggage claim?' does not ask where it is. "
        "Prefer natural American questions such as 'When does boarding start?' over 'Can I know the boarding time?'. "
        "In an airline check-in task use check-in/status, not registration (a different process). "
        "The planning task/core may be Chinese; these are editor metadata, not English card fields. "
        "Never reject a card just because its planning metadata is Chinese. "
        "Do NOT judge difficulty or semantic duplication: a separate independent review handles these. "
        "Do not suggest adding products or please as a difficulty improvement. "
        "Do not invent allergy safety guarantees. Only the following explicitly required English phrases are locked "
        f"verbatim; if this list is empty, there are NO locked phrases: {cards._required_focus_phrases(plan['topic'])}. "
        'Return {"reject":[{"id":"01","reason":"具體客觀錯誤及修正方向"}]}; empty reject means no objective errors.\n'
        + json.dumps({"jobs": [{key: job[key] for key in ("id", "Scenario", "task", "role", "speaker")} for job in plan["jobs"]],
            "items": [{key: item[key] for key in LEARNING_HEADERS} for item in items]}, ensure_ascii=False),
        "教材全欄位獨立審查", REVIEW_MODEL, job_ids=[item["id"] for item in items])
    rejected = parse_rejections(audit, {item["id"] for item in items})
    if rejected:
        targets = [{"job": job, "card": {key: item[key] for key in LEARNING_HEADERS}}
                   for job, item in zip(plan["jobs"], items) if item["id"] in rejected]
        confirmation = request_json(
            f"Independently copy-edit these ESL cards. Exact brief: {plan['topic']}\n"
            "Check ONLY objective grammar, translation, complete American IPA, tone, actionable tips, "
            "and faithful task/role/scenario alignment. Do not judge difficulty or duplicates. "
            "The main line and example must convey the SAME result. A double room does not mean two beds; "
            "a twin room has two single beds. Do not approve examples that change the bed type or quantity. "
            "The main line must itself accomplish the assigned question, not just 'Can I ask about' its topic. "
            "Avoid the unnatural American 'Can I know ...?' form. Natural yes/no questions such as "
            "'Am I eligible for online check-in?' and 'Am I required to show my ID?' are correct. "
            "Requests, statements and 'I'm wondering whether...' also accomplish information tasks. "
            "Do NOT require What/When/Where or call an actual question vague permission to ask. "
            "Reject metalinguistic narration like 'I need to say there is no hot water'; report it directly. "
            "For checking in again use 'check in again', never 'recheck in again'. "
            "Airline check-in is not called registration; flag that inaccurate term in a check-in task. "
            "A different preferred phrasing is NOT an error. An omitted relative pronoun in an object "
            "relative clause is valid. A short correct main line need not include all the example's details. "
            "The job's planning task/core may be Chinese; this is NOT a card error. "
            "The optional 'that' in a finite complement such as reconfirm my details are correct may be omitted. "
            "For EACH id, valid=true if no objective errors. If valid=false, give the exact incorrect text "
            "and a concrete correction in reason. Do not refer to any other card's ID or wording. "
            "No English lines are locked except these explicit required phrases: "
            + json.dumps(cards._required_focus_phrases(plan["topic"]), ensure_ascii=False)
            + "\n" + json.dumps(targets, ensure_ascii=False), "教材退回複核", SEMANTIC_MODEL, job_ids=sorted(rejected))
        checks = confirmation.get("checks")
        if not isinstance(checks, list) or len(checks) != len(rejected):
            raise ValueError("語言複核未完整列齊退回編號")
        by_id = {entry.get("id"): entry for entry in checks if isinstance(entry, dict)}
        if len(by_id) != len(checks) or set(by_id) != set(rejected):
            raise ValueError("語言複核有缺漏、重複或不明編號")
        for identifier, check in by_id.items():
            if check.get("valid") is True:
                del rejected[identifier]
            elif check.get("valid") is False and isinstance(check.get("reason"), str) and check["reason"].strip():
                rejected[identifier] = check["reason"]
            else:
                raise ValueError("語言複核缺少明確判定或客觀錯誤理由")
    rejected.update(review_task_fidelity(plan, items))
    compact = [{key: item[key] for key in ("id", "word_en", "sentence_en", "sentence_cn")} for item in items]
    semantic = request_json(
        f"Independently classify each ESL CARD's communication purpose and difficulty. Topic: {plan['topic']}\n"
        "One card contains a main spoken line and an example of that SAME intent; these are NOT a basic/advanced pair. "
        "Return exactly one assignment per ID, with a precise underlying action/information outcome as purpose. "
        "Use an IDENTICAL purpose label for equal outcomes, regardless of wording, products, reasons or place. "
        "For example, declaring souvenirs and declaring food both declare carried items; requesting a quote, "
        "estimate or written cost details all obtain written pricing. Do not hide duplicates by changing labels. "
        "Different questions/outcomes such as table availability, wait time and seat location stay distinct. "
        "Classify the entire card basic or advanced from the ACTUAL ENGLISH, ignoring assigned levels. "
        "The main line AND example must both meet the rating. A simple main line cannot be rescued by a harder example. "
        "For advanced, progression must cite a real nontrivial phrase/word/grammar in the MAIN line. "
        "Could instead of can, please/sorry, added products/reasons/location, and repeated common words are NOT advancement. "
        "Simple comparatives such as less spicy and by the window are basic; tone down the spice and be seated "
        "(passive voice) are genuinely advanced. State the actual level, even if the author might prefer another. "
        + DIFFICULTY_POLICY + PURPOSE_POLICY + cards._reference_prompt_note(references)
        + "\nReturn complete assignments plus reject. Only use reject for duplicates of a provided reference deck; "
        "Keep progression concise: for advanced cite its actual feature; for basic briefly state why it is basic. "
        "Do NOT draft alternative lines here; a targeted follow-up handles difficulty mismatches. "
        "when there are no references, reject is empty. The program enforces group sizes and difficulty quotas.\n"
        + json.dumps(compact, ensure_ascii=False), "教材完整語意分組", SEMANTIC_MODEL,
        job_ids=[item["id"] for item in items])
    if not isinstance(semantic.get("reject"), list):
        raise ValueError("語意審稿缺少 reject 陣列")
    sales = items if any(marker in plan["topic"].casefold() for marker in ("推銷", "敲詐", "upsell")) else None
    # Validate the complete partition before considering any classification corrections.
    partition = copy.deepcopy(semantic)
    for entry in partition.get("assignments", []):
        if not isinstance(entry, dict) or entry.get("level") not in ("basic", "advanced"):
            raise ValueError("語意審稿缺少有效難度欄位")
        entry["level"] = "basic"
    cards._semantic_group_rejections(partition, len(items), sales)
    assigned = {entry["id"]: entry for entry in semantic["assignments"]}
    field_rejections = review_field_difficulty(items)
    for item in items:
        if item["id"] not in field_rejections:
            # Both fields independently met this tier; the coarse whole-card guess is not authoritative.
            assigned[item["id"]]["level"] = item["tier"]
            assigned[item["id"]]["progression"] = item["progression"]
    for identifier, reason in field_rejections.items():
        rejected[identifier] = rejected.get(identifier, "") + reason
    equivalents = request_json(
        "Independently find every group of cards with the SAME underlying communication outcome. "
        "Compare the actual English, NOT scenario labels or purpose labels from another editor. "
        "Different words such as ask/check/confirm/get details do NOT automatically mean different outcomes. "
        "Changing the object, product, reason, or location alone does not create a new outcome. "
        "For example 'Can I check the gate?' and 'Can I get gate information?' both obtain gate information. "
        "'Take this out' and 'take out my laptop for screening' both ask about removing an item for screening. "
        "Permission to bring this, medicine or liquids is the same carry-permission question unless the actual "
        "line asks a substantively different policy detail such as a quantity limit. "
        "Keep genuinely different outcomes separate: permission vs quantity limits, fees vs payment, "
        "a gate number vs walking directions to that gate, wait time vs seat availability. "
        "List ALL equivalent groups with at least two IDs, INCLUDING valid basic/advanced pairs. "
        "Do not decide which cards to keep or judge difficulty; code applies those rules. "
        "Return {\"groups\":[{\"ids\":[\"01\",\"02\"],\"purpose\":\"shared concrete outcome\"}]}; "
        "empty groups ONLY if every card has a genuinely distinct outcome.\n"
        + PURPOSE_POLICY + "\n" + json.dumps(compact, ensure_ascii=False),
        "教材跨標籤同義複核", SEMANTIC_MODEL, job_ids=[item["id"] for item in items])
    verify_semantic_pairs(plan, items, semantic, equivalents)
    grouped_rejections = cards._semantic_group_rejections(semantic, len(items), sales)
    for index, reason in grouped_rejections.items():
        rejected[items[index]["id"]] = reason
    rejected.update(parse_rejections(semantic, {item["id"] for item in items}))
    assignments = {entry["id"]: entry for entry in semantic["assignments"]}
    for original in items:
        checked = assignments[original["id"]]
        original["_semantic_group"] = checked["purpose"]
        original["_semantic_level"] = checked["level"]
        original["_semantic_review_version"] = SEMANTIC_REVIEW_VERSION
        if original["_semantic_level"] != original["tier"]:
            difficulty = ("獨立審稿的實際難度與指派難度不同；重寫成真正的 "
                + original["tier"] + "。保留原 task，使用統一難度標準中的真實用字或結構。審稿依據："
                + str(checked.get("progression", "")))
            previous = rejected.get(original["id"], "")
            rejected[original["id"]] = previous + "；" + difficulty if previous else difficulty
    return rejected


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def fingerprint_for(plan: dict, references: list[dict]) -> str:
    return hashlib.sha256(json.dumps({"plan": plan, "references": references}, sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()


def backup_generation(paths: list[Path]) -> Path | None:
    existing = [path for path in paths if path.is_file()]
    if not existing:
        return None
    directory = Path(cards.BASE_DIR) / ".cleanup-backups" / ("generation-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    directory.mkdir(parents=True)
    manifest = []
    for index, path in enumerate(existing):
        target = directory / f"{index:02d}-{path.name}"
        shutil.copy2(path, target)
        manifest.append({"source": str(path.resolve()), "backup": target.name,
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    save_json(directory / "manifest.json", {"files": manifest})
    cards._progress(f"重建前保留舊教材與檢查點：{directory}")
    return directory


def repair_duplicate_jobs(plan: dict, duplicate_ids: set[str], rejected: dict, references: list[dict]) -> list[dict]:
    retained = [job for job in plan["jobs"] if job["id"] not in duplicate_ids]
    withdrawn = [job for job in plan["jobs"] if job["id"] in duplicate_ids]
    prompt = (f"修正 ESL 教材策劃。精確主題：{plan['topic']}\n" + cards.SEMANTIC_DUPLICATE_POLICY
        + PURPOSE_POLICY + "\n以下退回任務必須改成尚未涵蓋的真正新目的，不只是改寫同義句。"
        "core 必須是動作＋具體資訊或結果，不能用『安檢物品』『房間需求』等大分類。"
        "不要用更換物品或理由湊新目的；涵蓋尚未收錄的現場問題、事實回答或問題處理。"
        "DO NOT preserve the old task or core of a replacement ID. Invent a different uncovered outcome. "
        "Each replacement must have ONE communication outcome, not a compound of old outcomes. "
        "Prefer an uncovered factual reply or problem report instead of another vague permission question. "
        "只輸出被退回 id 的新任務。保留每項 id 和原角色 role/speaker，不能改情境或難度。"
        "Scenario/tier 由程式填入，不要輸出。輸出 jobs 每項包含 id/core/task/role/speaker。\n"
        + cards._reference_prompt_note(references)
        + json.dumps({
            "retained_tasks": [{key: job[key] for key in ("id", "Scenario", "task")} for job in retained],
            "withdrawn_outcomes_DO_NOT_REUSE": [job["task"] for job in withdrawn],
            "empty_slots_to_fill": [{key: job[key] for key in ("id", "Scenario", "tier", "role", "speaker")}
                                    for job in withdrawn],
            "reasons": rejected}, ensure_ascii=False))
    for attempt in range(MAX_ATTEMPTS):
        try:
            repair = request_json(prompt, "替換重複教材目的", REPAIR_MODEL,
                                  job_ids=sorted(duplicate_ids))
            replacements = repair.get("jobs")
            if not isinstance(replacements, list) or len(replacements) != len(duplicate_ids):
                raise ValueError("替換任務未完整列齊")
            by_id = {job.get("id"): job for job in replacements if isinstance(job, dict)}
            if len(by_id) != len(replacements) or set(by_id) != duplicate_ids:
                raise ValueError("替換任務編號重複、缺漏或超額")
            updated = []
            for job in plan["jobs"]:
                incoming = dict(by_id[job["id"]], Scenario=job["Scenario"], tier=job["tier"]) if job["id"] in by_id else job
                if any(incoming.get(key) != job[key] for key in ("id", "Scenario", "tier", "role", "speaker")):
                    raise ValueError("替換不能改動情境、難度或角色")
                updated.append(incoming)
            validate_plan(dict(plan, jobs=updated), plan["topic"], plan["count"])
            validate_replacement_outcomes(plan, updated, duplicate_ids)
            return updated
        except (ValueError, TypeError, KeyError) as exc:
            cards._progress(f"替換目的退回 {attempt + 1}/{MAX_ATTEMPTS}：{exc}")
            prompt += f"\n上次提案未通過：{exc}；請用新的具體目的修正，不要重交錯誤提案。"
            if "repair" in locals():
                prompt += "\n已退回且禁止重交的提案：" + json.dumps(repair, ensure_ascii=False)
    raise ValueError("替換目的三次未通過，保留原策劃與檢查點")


def validate_replacement_outcomes(plan: dict, updated: list[dict], replacement_ids: set[str]) -> None:
    proposals = [job for job in updated if job["id"] in replacement_ids]
    response = request_json(
        "Independently review proposed replacement ESL tasks, NOT finished English cards. "
        "Each replacement MUST introduce a genuinely NEW communication outcome within its scenario and brief. "
        "A combination of two old outcomes is NOT new. Reject compound tasks that bundle unrelated requests. "
        "Compare each proposal with ALL old tasks (including its own withdrawn task) and the other proposals. "
        "Changing words, objects, reasons or core labels alone is NOT a new outcome. "
        "A shared scenario or category is NOT equivalence. Checking a reservation NAME, checking DATES, "
        "asking CHECK-OUT TIME, and asking about an EARLY CHECK-IN are all distinct outcomes. "
        "A baggage FEE, WEIGHT LIMIT, and obtaining a BAGGAGE TAG are different information. "
        "Asking a gate number versus asking which gate is assigned is the SAME. "
        "Replacing a question about gate location with a question about gate location is invalid. "
        "Requesting written quotes versus written pricing details is the SAME. "
        "valid=true only for a distinct in-scope outcome. For false, cite the overlapping old task and "
        "suggest a different uncovered result. Return checks with id, valid, reason.\n"
        + json.dumps({"brief": plan["topic"],
            "old_tasks": [{key: job[key] for key in ("id", "Scenario", "task")} for job in plan["jobs"]],
            "proposals": [{key: job[key] for key in ("id", "Scenario", "task")} for job in proposals]}, ensure_ascii=False),
        "教材退回複核", SEMANTIC_MODEL, job_ids=sorted(replacement_ids))
    checks = response.get("checks")
    by_id = {entry.get("id"): entry for entry in checks if isinstance(entry, dict)} if isinstance(checks, list) else {}
    if len(checks or []) != len(replacement_ids) or set(by_id) != replacement_ids:
        raise ValueError("替換目的獨立複核缺漏、重複或超額")
    errors = [f"{identifier} 替換仍非新目的：{check.get('reason', '缺少確認')}"
              for identifier, check in by_id.items() if check.get("valid") is not True]
    if errors:
        raise ValueError("；".join(errors))


def generate_deck(plan: dict, checkpoint: Path, references: list[dict], resume: bool = False) -> dict:
    validate_plan(plan, plan["topic"], plan["count"])
    fingerprint = fingerprint_for(plan, references)
    state = {"version": VERSION, "fingerprint": fingerprint, "plan": plan, "items": [], "review_passed": False}
    if resume:
        if not checkpoint.is_file():
            raise ValueError("找不到可接續的教材檢查點")
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        if (state.get("version"), state.get("fingerprint")) != (VERSION, fingerprint):
            raise ValueError("檢查點的主題、配額、策劃或參考牌組已變更")
        state["plan"] = plan
        jobs = {job["id"]: job for job in plan["jobs"]}
        seen = set()
        retained = []
        for item in state.get("items", []):
            if item.get("id") not in jobs or item["id"] in seen:
                raise ValueError("檢查點有重複或不明編號")
            seen.add(item["id"])
            item = normalize_chinese(item)
            try:
                validate_item(item, jobs[item["id"]])
            except ValueError as exc:
                cards._progress(f"檢查點 {item['id']} 未通過現行校驗，將重寫：{exc}")
            else:
                retained.append(item)
        state["items"] = retained
        state["review_passed"] = False
        cards._progress(f"接續教材檢查點：{len(state['items'])}/{plan['count']} 句")
    save_json(checkpoint, state)
    feedback = state.get("feedback", "")
    if state.get("pending_review_rejections") and state.get("pending_review_rejections_version") != SEMANTIC_REVIEW_VERSION:
        state.pop("pending_review_rejections", None)
        state.pop("pending_review_rejections_version", None)
        feedback = ""
    if resume and state.get("pending_review_rejections"):
        rejected = state["pending_review_rejections"]
        duplicate_ids = {key for key, reason in rejected.items() if reason.startswith("語意重複：")}
        if duplicate_ids:
            plan["jobs"] = repair_duplicate_jobs(plan, duplicate_ids, rejected, references)
            state["fingerprint"] = fingerprint_for(plan, references)
        state["items"] = [item for item in state["items"] if item["id"] not in rejected]
        state.pop("pending_review_rejections", None)
        state.pop("pending_review_rejections_version", None)
        save_json(checkpoint, state)
    for attempt in range(MAX_ATTEMPTS):
        present = {item["id"] for item in state["items"]}
        missing = [job for job in plan["jobs"] if job["id"] not in present]
        for start in range(0, len(missing), BATCH_SIZE):
            try:
                generated = generate_batch(plan, missing[start:start + BATCH_SIZE], state["items"], feedback)
            except BatchGenerationError as exc:
                state["items"].extend(exc.items)
                state["items"].sort(key=lambda item: int(item["id"]))
                state["feedback"] = exc.feedback
                save_json(checkpoint, state)
                raise
            state["items"].extend(generated)
            state["items"].sort(key=lambda item: int(item["id"]))
            save_json(checkpoint, state)
            cards._progress(f"教材完成：{len(state['items'])}/{plan['count']} 句，已保存檢查點")
        validate_deck(state["items"], plan)
        try:
            rejected = review_deck(plan, state["items"], references)
        except (ValueError, KeyError, TypeError) as exc:
            cards._progress(f"獨立審稿格式退回 {attempt + 1}/{MAX_ATTEMPTS}：{exc}")
            if attempt + 1 == MAX_ATTEMPTS:
                raise RuntimeError("獨立審稿未完整通過，拒絕輸出") from exc
            continue
        if not rejected:
            validate_deck(state["items"], plan, reviewed=True)
            state["review_passed"] = True
            state["review_version"] = SEMANTIC_REVIEW_VERSION
            save_json(checkpoint, state)
            return state
        feedback = json.dumps([{"id": item["id"], "failed_word_en": item["word_en"],
            "failed_sentence_en": item["sentence_en"], "reason": rejected[item["id"]]}
            for item in state["items"] if item["id"] in rejected], ensure_ascii=False)
        state["feedback"] = feedback
        state["pending_review_rejections"] = rejected
        state["pending_review_rejections_version"] = SEMANTIC_REVIEW_VERSION
        save_json(checkpoint, state)
        cards._progress("教材退回原因：" + feedback)
        if attempt + 1 == MAX_ATTEMPTS:
            raise RuntimeError("教材審稿三次未通過，拒絕輸出：" + json.dumps(rejected, ensure_ascii=False))
        duplicate_ids = {key for key, reason in rejected.items() if reason.startswith("語意重複：")}
        if duplicate_ids:
            plan["jobs"] = repair_duplicate_jobs(plan, duplicate_ids, rejected, references)
            state["fingerprint"] = fingerprint_for(plan, references)
        state["items"] = [item for item in state["items"] if item["id"] not in rejected]
        state.pop("pending_review_rejections", None)
        state.pop("pending_review_rejections_version", None)
        save_json(checkpoint, state)
        cards._progress(f"教材審稿退回 {len(rejected)} 句，自動補寫")
    raise RuntimeError("教材未完成")


def run(args, parser, cli_mode: bool) -> None:
    if cards.REVIEW_MODE not in ("hybrid", "ai"):
        parser.error("新版教材必須啟用 hybrid 或 ai 審稿；local/off 僅適用 --legacy")
    topic = (args.topic or input("\n主題名稱: ")).strip()
    if not topic or any(char in topic for char in ("/", "\\")) or topic in (".", ".."):
        parser.error("主題名稱不能空白或含路徑分隔符號")
    focus = args.focus.strip() if cli_mode else cards._prompt_topic_description()
    count = args.count
    if not cli_mode:
        raw = input(f"句數（留空={cards.DEFAULT_CARD_COUNT}）: ").strip()
        if raw:
            try:
                count = int(raw)
            except ValueError:
                parser.error("句數必須是整數")
    try:
        basic = basic_count(count)
    except ValueError as exc:
        parser.error(str(exc))
    brief = cards._generation_topic(topic, focus)
    output = Path(args.output).expanduser().resolve() if args.output else Path(cards.OUTPUT_DIR) / (cards._topic_to_slug(topic) + ".xlsx")
    if output.suffix.lower() != ".xlsx":
        output = output.with_name(output.name + ".xlsx")
    plan_path = Path(args.plan_file).expanduser().resolve() if args.plan_file else output.with_suffix(".plan.json")
    checkpoint = Path(str(output) + ".curriculum.json")
    if len({output.resolve(), plan_path.resolve(), checkpoint.resolve()}) != 3:
        parser.error("Excel、策劃與檢查點必須使用不同路徑")
    references, paths = cards._load_reference_decks(args.avoid)
    if str(output.resolve()) in paths:
        parser.error("輸出不能同時列入 --avoid")
    if output.exists() and not args.force and not args.plan_only:
        if checkpoint.exists():
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            if (saved.get("review_passed") is True and saved.get("version") == VERSION
                    and saved.get("review_version") == SEMANTIC_REVIEW_VERSION
                    and saved.get("fingerprint") == fingerprint_for(saved.get("plan"), references)):
                validate_plan(saved["plan"], brief, count)
                validate_deck(saved["items"], saved["plan"], reviewed=True)
                expected = [{key: item[key] for key in LEARNING_HEADERS} for item in saved["items"]]
                if cards.load_xlsx_items(str(output)) == expected:
                    cards._progress(f"已完成且內容一致，保留既有教材：{output}")
                    caption = output.with_name("youtube_" + output.stem + ".txt")
                    if not args.no_youtube and not caption.exists():
                        cards._generation_deadline.set(time.monotonic() + cards.GENERATION_TIMEOUT)
                        context = focus + "\n教材情境：" + "、".join(saved["plan"]["scenarios"])
                        cards.write_youtube_description(topic, count, str(caption), content_context=context)
                    return
        parser.error("Excel 已存在；拒絕自動覆蓋。要重建請明確加 --force")
    if args.force:
        backup_generation([output, plan_path, checkpoint])
    cards._generation_deadline.set(time.monotonic() + cards.GENERATION_TIMEOUT)
    cards._progress(f"階段 1/3：教材策劃；{count} 句，基礎 {basic}／進階 {count - basic}")
    if args.plan_file or args.resume:
        if not plan_path.exists():
            parser.error("找不到新版教材策劃檔")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        validate_plan(plan, brief, count)
    else:
        plan = plan_curriculum(brief, count, references)
        save_json(plan_path, plan)
    cards._progress(f"階段 1/3 完成：{len(plan['scenarios'])} 個情境，句數平均分配")
    if args.plan_only:
        cards._progress(f"流程已完成：{plan_path}（未生成卡片）")
        return
    cards._progress("階段 2/3：生成、語言與完整語意審稿")
    try:
        state = generate_deck(plan, checkpoint, references, args.resume)
    finally:
        # Task repairs must also be persisted so --resume has the same contract.
        save_json(plan_path, plan)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".curriculum-export-", dir=output.parent) as directory:
        temporary = Path(directory) / output.name
        cards.write_xlsx(state["items"], str(temporary), curriculum_plan=plan)
        expected = [{key: item[key] for key in LEARNING_HEADERS} for item in state["items"]]
        if cards.load_xlsx_items(str(temporary)) != expected:
            raise RuntimeError("Excel 回讀與審核內容不符，拒絕發布")
        temporary.replace(output)
    cards._progress(f"階段 2/3 完成：12 欄 Excel 已回讀核對 → {output}")
    cards._progress("階段 3/3：YouTube 描述")
    if not args.no_youtube:
        context = focus + "\n教材情境：" + "、".join(plan["scenarios"])
        cards.write_youtube_description(topic, count, str(output.with_name("youtube_" + output.stem + ".txt")), content_context=context)
    cards._progress(f"流程已完成：{output}")
