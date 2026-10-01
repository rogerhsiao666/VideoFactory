"""Merge an existing deck into beginner/advanced pairs with actionable learning aids."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

import cards


VERSION = 4
SCENARIOS = ("聽懂說明與了解故障", "核對報價與維修必要性", "核實證據與控制施工", "拒絕加購與保留決定權")
LEARNING_HEADERS = ["id", "Scenario", "Level", "Tone", "word_en", "word_ipa",
                    "word_cn", "Core_Vocab", "tips", "sentence_en", "sentence_ipa", "sentence_cn"]
LEVELS = {"basic": "⭐", "advanced": "⭐⭐"}
TONES = ("委婉", "中立", "強硬")


def request_json(prompt: str, stage: str) -> dict:
    kwargs = {"messages": [{"role": "user", "content": prompt + "\nReturn only valid JSON."}],
              "model": cards.CARD_MODEL, "response_format": {"type": "json_object"}}
    if not cards.CARD_MODEL.startswith("gpt-5"):
        kwargs["temperature"] = 0.1
        kwargs["max_tokens"] = 6000
    else:
        kwargs["max_completion_tokens"] = 6000
    response = cards._call_openai(stage=stage, **kwargs)
    result = json.loads(response.choices[0].message.content)
    if not isinstance(result, dict):
        raise ValueError("編輯回應必須是 JSON 物件")
    return result


def source_intent(item: dict) -> str:
    """Conservative anchors for attested sales-deck synonyms, not different decisions."""
    text = item["word_en"].lower().replace("’", "'")
    if re.search(r"\b(?:repair )?process\b", text):
        return "了解維修步驟"
    for pattern, purpose in (
        (r"\bpart do\b", "詢問零件功能"),
        (r"\bprocedure necessary\b", "詢問維修必要性"),
        (r"\bspecific issue\b", "詢問具體故障"),
        (r"\btotal cost\b", "確認總費用"),
        (r"\bread the contract\b", "先閱讀合約再決定簽署"),
        (r"\baccessory.*remove\b", "移除訂單中的加購項目"),
        (r"\bindependent inspection report\b", "取得第三方檢查報告"),
    ):
        if re.search(pattern, text):
            return purpose
    if re.search(r"\b(?:need|send|provide|get|have)\b", text) and re.search(
        r"\b(?:quote|estimate|cost breakdown|detailed breakdown|itemized invoice|written details|"
        r"written document|details in writing)\b", text
    ):
        return "取得書面報價與明細"
    if re.search(r"\b(?:simplify|jargon|term|simply|simple terms)\b", text):
        return "聽懂術語與說明"
    if re.search(r"\b(?:charge|charges|expensive)\b", text):
        return "釐清收費項目的理由與內容"
    if re.search(r"\bproof\b|\bwritten report\b", text):
        return "要求故障證據再決定維修"
    if re.search(r"\bextra (?:repairs|treatments)\b", text):
        return "限定原先同意的服務範圍"
    if re.search(r"\b(?:time to (?:think|decide)|time.*consider)\b", text):
        return "保留考慮時間"
    if re.search(r"\b(?:stop.*(?:sales|selling|pushing)|sales talk|stop pushing|"
                 r"don't need.*(?:product|service)|not interested|don't want it|already have something)\b", text):
        return "拒絕推銷並停止糾纏"
    if re.search(r"\bstop\b", text) and re.search(r"\b(?:work|tasks|authorize|agree)\b", text):
        return "停止未授權施工"
    return ""


def validate_groups(groups, source: list[dict]) -> None:
    if not isinstance(groups, list) or not groups:
        raise ValueError("缺少核心概念分組")
    seen, cores, anchors = set(), set(), {}
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("概念分組必須是物件")
        core = group.get("core", "")
        if not isinstance(core, str) or not core.strip() or core in cores:
            raise ValueError("核心概念不得空白或重複")
        cores.add(core)
        if group.get("scenario") not in SCENARIOS:
            raise ValueError(f"{core}: 無效情境標籤 {group.get('scenario')!r}，必須逐字使用指定情境")
        ids = group.get("source_ids")
        if not isinstance(ids, list) or not ids:
            raise ValueError(f"{core}: source_ids 必須非空")
        group_anchors = set()
        for value in ids:
            if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
                raise ValueError("來源編號必須是整數")
            number = int(value)
            if number not in range(1, len(source) + 1) or number in seen:
                raise ValueError("來源編號超出範圍或重複歸組")
            seen.add(number)
            anchor = source_intent(source[number - 1])
            if anchor:
                group_anchors.add(anchor)
            if anchor and anchors.setdefault(anchor, core) != core:
                raise ValueError(f"{anchor} 被拆成多個概念，必須合併")
        if len(group_anchors) > 1:
            raise ValueError(f"不同現場結果被誤合併：{sorted(group_anchors)}")
    if seen != set(range(1, len(source) + 1)):
        raise ValueError(f"漏列原始句子 {sorted(set(range(1, len(source) + 1)) - seen)}")


def group_source(topic: str, source: list[dict]) -> list[dict]:
    compact = [{"id": i + 1, "word_en": x["word_en"], "sentence_en": x["sentence_en"],
                "merge_anchor": source_intent(x)} for i, x in enumerate(source)]
    prompt = f"""你是 ESL 初學者教材總編。主題：{topic}。
把原始 {len(source)} 句按實際核心概念合併，不補新任務、不維持原句數。
{cards.SEMANTIC_DUPLICATE_POLICY}
相同非空 merge_anchor 必須在同一組；空白標籤由你按目的判斷，不表示獨立概念。
不同非空 merge_anchor 不可合併；各自表示不同的現場結果。
來源有時混合兩種要求，以實際現場行動為主，保留其重要界線。
情境用scenario_id選擇：{json.dumps(dict(enumerate(SCENARIOS, start=1)), ensure_ascii=False)}。
逐句提供 id、core、scenario_id。scenario_id只能是1到4的整數。
core 是可反覆使用的實際目的，不用商品或場所命名。
assignments必須有完整{len(source)}項，包含編號1到{len(source)}各一次，按編號順序。
不是只列有重複的句子，也不可省略單獨目的。相同目的的core逐字相同。
不把價格資料按 quote/estimate/invoice 拆組。
費用項目的內容、為何多收、為何昂貴，都在釐清收費理由，不按extra/service拆組。
要求故障證據與拒絕無證據維修是同一決策目的；獨立檢查另有第三方核實結果。
讀合約不是聽懂術語；限制原先服務範圍不是停止已開始的未授權施工。
若有真正不同的行動，例如確認總價與要求報價明細，仍分開。
只輸出 {{"assignments":[{{"id":1,"core":"聽懂術語與說明","scenario_id":1}}]}}。
完整原句：{json.dumps(compact, ensure_ascii=False)}
"""
    feedback = ""
    for attempt in range(3):
        payload = request_json(prompt + feedback, f"原句合併 {attempt + 1}/3")
        try:
            assignments = payload.get("assignments")
            if not isinstance(assignments, list) or len(assignments) != len(source):
                raise ValueError(f"assignments必須逐句列齊{len(source)}項")
            by_core = {}
            for assignment in assignments:
                if not isinstance(assignment, dict) or type(assignment.get("scenario_id")) is not int:
                    raise ValueError("逐句標記缺少有效情境編號")
                scenario_id = assignment["scenario_id"]
                if not 1 <= scenario_id <= len(SCENARIOS):
                    raise ValueError("情境編號必須1至4")
                core = assignment.get("core", "")
                if not isinstance(core, str) or not core.strip():
                    raise ValueError("每句必須提供非空核心概念")
                merged = by_core.setdefault(core, {"core": core, "scenario": SCENARIOS[scenario_id - 1], "source_ids": []})
                if merged["scenario"] != SCENARIOS[scenario_id - 1]:
                    raise ValueError("相同核心概念必須使用同一情境")
                merged["source_ids"].append(assignment.get("id"))
            groups = list(by_core.values())
            validate_groups(groups, source)
            audit = request_json(
                "你是獨立編輯。核對原句與分組，不可新增任務。只退回不同標籤下意思仍相同、"
                "或重要不同目的被誤合併的分組。換商品、場所、同義文件名稱或拒絕理由不算不同目的。"
                "保留空間針對具體實際結果區分，不按泛用主題把所有問句合併。"
                "必須輸出 JSON {\"issues\": [\"具體問題及應合併的core\"]}，合格時issues為空陣列。"
                + json.dumps({"source": compact, "groups": groups}, ensure_ascii=False),
                "原句合併獨立審查",
            )
            issues = audit.get("issues")
            if not isinstance(issues, list):
                raise ValueError("獨立審查缺少 issues 陣列")
            if issues:
                raise ValueError("；".join(map(str, issues)))
            return groups
        except ValueError as exc:
            feedback = f"\n前次分組未通過：{exc}。請重新輸出完整全部分組。前次：" + json.dumps(payload, ensure_ascii=False)
            cards._progress(f"原句分組退回：{exc}")
    raise RuntimeError("原句合併連續三次未通過，拒絕輸出")


def validate_pair(group: dict, pair: list[dict]) -> None:
    if not isinstance(pair, list) or len(pair) != 2:
        raise ValueError("每個核心概念必須剛好簡單、進階各一句")
    if {x.get("tier") for x in pair if isinstance(x, dict)} != {"basic", "advanced"}:
        raise ValueError("每組必須包含 basic 與 advanced")
    for item in pair:
        if item.get("core") != group["core"] or item.get("Scenario") != group["scenario"]:
            raise ValueError("不能用另一目的或情境佔位")
        if item.get("Level") != LEVELS[item["tier"]] or item.get("Tone") not in TONES:
            raise ValueError(f"難度或語氣標籤無效：Level={item.get('Level')!r}, Tone={item.get('Tone')!r}")
        issues = cards._validation_issues(item)
        issues = [issue for issue in issues if not issue.startswith("tips has ")]
        if issues:
            raise ValueError("；".join(issues))
        tip = item["tips"]
        if len(tip) > 90 or not tip.startswith(item["Tone"] + "："):
            raise ValueError("Tips 須以語氣加冒號開頭且不超過90字")
        action = tip.split("時", 1)[-1] if "時" in tip else tip
        if not re.search(r"(?:當|對方|看到|聽到|發現|遇到|準備)", tip) or not re.search(
            r"(?:先|請|要求|反問|指|拿|停|不要|別|確認|核對|拒絕|看|問|說|讀)", action
        ):
            raise ValueError("Tips 必須包含現場觸發情況與具體動作，不可只寫適用時機")
        vocab = item.get("vocab")
        if not isinstance(vocab, list) or not 1 <= len(vocab) <= 2:
            raise ValueError("每句須有1-2個核心單字或短語")
        spoken = " " + cards._similarity_text(item["word_en"] + " " + item["sentence_en"]) + " "
        for entry in vocab:
            if not isinstance(entry, dict) or not isinstance(entry.get("en"), str) or not isinstance(entry.get("cn"), str):
                raise ValueError("核心單字必須有英文與中文")
            term = cards._similarity_text(entry["en"])
            if not term or len(term.split()) > 4 or " " + term + " " not in spoken or not re.search(r"[\u4e00-\u9fff]", entry["cn"]):
                raise ValueError("核心單字須實際出現在詞句中，且有繁體中文意思")
        expected_vocab = "；".join(entry["en"] + " " + entry["cn"] for entry in vocab)
        if item.get("Core_Vocab") != expected_vocab:
            raise ValueError("Core_Vocab 與核心單字資料不一致")
        if item["tier"] == "advanced" and not str(item.get("progression", "")).strip():
            raise ValueError("進階句須說明實際詞彙或句法差異")
        point = {"target_phrase": item["word_en"], "target_sentence": item["sentence_en"],
                 "task": item["sentence_en"], "category": group["scenario"]}
        # The learning edition intentionally permits longer, vivid action tips.
        compact_item = dict(item, tips="現場提示")
        issue = cards._locked_item_issue(compact_item, point)
        if issue:
            raise ValueError(issue)
    first, second = sorted(pair, key=lambda x: x["tier"] != "basic")
    if cards._spoken_line_key(first["word_en"]) == cards._spoken_line_key(second["word_en"]):
        raise ValueError("簡單與進階句不可相同")


def generate_pair(topic: str, group: dict, source: list[dict], feedback: str = "") -> list[dict]:
    assigned = [{key: source[int(value) - 1][key] for key in ("id", "word_en", "word_cn", "sentence_en", "sentence_cn")}
                for value in group["source_ids"]]
    prompt = f"""你是專業 ESL 教材編輯。主題：{topic}。
將這一核心概念只保留 basic 與 advanced 各一張，不能增加不同任務。
Basic 最簡單直白，國中常見單字；advanced 用真實自然的進階詞彙或句法，
不能只是加 please、理由、商品名稱或變得強硬。進階也要短、現場能說。
英文短句 word_en 不超過8字，例句 sentence_en 不超過14字；兩者完成同一核心概念。
word_en必須是顧客當場能說的完整要求或問句，不只是名詞或教學指令。
例句只補現場條件，不能改說話角色。兩句都要實際完成指定目的。
書面報價必須說written或in writing；獨立檢查必須說another expert或independent。
移除訂單必須明確要求take off/remove/exclude，不用not interested代替。
原始中文與例句也可能有錯，請按實際目的重新編寫，不機械照抄。
兩句語氣須一致；不要短句中立、例句卻改成Could you的委婉請求。
中文自然、台灣繁體中文口語。word_cn 只翻譯 word_en，不补入例句條件。
美式 IPA 逐字對應英文，斜線包裹。原句是待修訂素材，不沿用錯誤音標。
Tips 為具體行動指令，最多85字。不加語氣前綴，程式會依Tone填入。
不能只說「當你想...時使用」或「要堅定」。必須有看得見的現場觸發及動作，例如：
「對方指著零件說壞了時，先不要答應更換，指著零件問它的具體功能。」
勿因需要報價就要求簽字、先付費；勿捏造法律或暗示單字永遠足以表達否定或授權。
每張 vocab 提煉1-2個英文中實際出現的單字或短語及其中文意思，保留必要片語如 not interested。
不要一整句當核心單字。Core_Vocab由程式填入。
每張輸出欄位：core, tier, Scenario, Tone, word_en, word_ipa, word_cn,
tips, sentence_en, sentence_ipa, sentence_cn, vocab, progression。
tier只能basic或advanced；Level由程式依tier填入，不必生成。
Scenario逐字使用指派的scenario；Tone按措辭選委婉/中立/強硬，不把中立問句標成委婉。
vocab如 [{{"en":"estimate","cn":"估價單"}}]；advanced的progression說明真實進階之處。
指派概念：{json.dumps(group, ensure_ascii=False)}
原句：{json.dumps(assigned, ensure_ascii=False)}
前次校驗回饋：{feedback}
只輸出 {{"items": [...]}}。
"""
    error = ""
    for attempt in range(3):
        payload = request_json(prompt + "\n本次必須修正：" + error, f"教材配對：{group['core']} {attempt + 1}/3")
        try:
            pair = payload.get("items")
            if isinstance(pair, list):
                for item in pair:
                    if isinstance(item, dict) and item.get("tier") in LEVELS:
                        item["Level"] = LEVELS[item["tier"]]
                    if isinstance(item, dict) and item.get("Tone") in TONES and isinstance(item.get("tips"), str):
                        body = re.sub(r"^(?:委婉|中立|強硬)\s*[:：]\s*", "", item["tips"].strip())
                        item["tips"] = item["Tone"] + "：" + body
                    if isinstance(item, dict) and isinstance(item.get("vocab"), list):
                        item["Core_Vocab"] = "；".join(str(x.get("en", "")) + " " + str(x.get("cn", ""))
                                                       for x in item["vocab"] if isinstance(x, dict))
                        point = {"target_phrase": item.get("word_en", ""), "target_sentence": item.get("sentence_en", ""),
                                 "task": item.get("sentence_en", ""), "category": group["scenario"]}
                        corrected = cards._correct_known_locked_pronunciations(item, point)
                        item.update(corrected)
            validate_pair(group, pair)
            return sorted(pair, key=lambda x: x["tier"] != "basic")
        except (ValueError, TypeError, KeyError) as exc:
            error = str(exc) + "\n待修正內容：" + json.dumps(payload, ensure_ascii=False)
            cards._progress(f"教材配對退回：{group['core']}：{error}")
    raise RuntimeError(f"{group['core']}：連續三次未通過教材校驗：{error}")


def validate_deck(items: list[dict], groups: list[dict], source: list[dict]) -> None:
    validate_groups(groups, source)
    if len(items) != 2 * len(groups):
        raise ValueError("教材數量與概念配對不一致")
    for index, item in enumerate(items, start=1):
        if item.get("id") != f"{index:02d}":
            raise ValueError("教材編號必須連續")
    for group in groups:
        try:
            validate_pair(group, [item for item in items if item.get("core") == group["core"]])
        except ValueError as exc:
            raise ValueError(f"{group['core']}：{exc}") from exc


def review_edition(topic: str, items: list[dict]) -> dict[str, str]:
    compact = [{key: item[key] for key in ("id", "core", "tier", "word_en", "word_cn", "sentence_en",
                                         "sentence_cn", "Scenario", "Level", "Tone", "tips", "vocab", "progression")}
               for item in items]
    prompt = f"""你是獨立 ESL 初學者教材審稿人。主題：{topic}。
{cards.SEMANTIC_DUPLICATE_POLICY}
核對整副每個core恰有一簡單一进階且實際意思相同；跨core同義概念必須合併。
每個core一簡單一進階兩句目的相同是必要條件，不是重複問題。
禁止因同一core的兩句都是要報價就要求合併；要檢查是否有真正難度差異。
不同結果不是同義句，例如詢問总價與要求價目明細、移除訂單項目與停止正在施工。
進階須有真實詞彙或句法差異，不只是please、商品、理由或語氣。
核對所有句子的英文、中文、語氣標籤、核心單字翻譯及Tips是否合適。
Tips必須有具体現場觸發及可執行動作，且不能暗示簽字授權、違法恐嚇或先付款。
「當不清楚時，詢問具體功能」仍太籠統。應包含對方實際說或做的事，以及可做的動作與對象。
例句不可轉成服務人員說「我需要證明才能幫忙」；必須是顧客取得证據再決定是否接受維修。
獨立報告與移除訂單不可用not interested佔位。核心單字的否定片語需保留not。
核心單字可帮助開口，但不保證單字足以表達否定、取消或授權。
語氣按真實措辭判斷：普通確認問句多為中立；Could you通常委婉；命令停止通常強硬。
只退回明顯問題，勿為細微措辭偏好退回。每個issues項目指定需重寫的core與具體reason。
若跨core仍重複，reason以「合併：」開頭，不靠改寫保留超額句。
跨core合併必須另填merge_with，指定另一個不同的core；同core無難度差異只要求重寫。
只輸出JSON {{"issues":[{{"core":"...","reason":"...","merge_with":"只有跨core合併時才填另一core，否則空字串"}}]}}，合格時issues是空陣列。
教材：{json.dumps(compact, ensure_ascii=False)}
"""
    payload = request_json(prompt, "教材全欄位獨立審查")
    raw = payload.get("issues")
    if not isinstance(raw, list):
        raise ValueError("教材審查缺少 issues 陣列")
    rejected = {}
    for issue in raw:
        if not isinstance(issue, dict) or issue.get("core") not in {x["core"] for x in items} or not issue.get("reason"):
            raise ValueError("教材審查退回項目格式不完整")
        if issue.get("merge_with"):
            if issue["merge_with"] == issue["core"] or issue["merge_with"] not in {x["core"] for x in items}:
                raise ValueError("跨概念合併必須指定另一個有效core")
            raise RuntimeError("概念分組仍重複，需重新合併：" + str(issue["reason"]))
        rejected[issue["core"]] = str(issue["reason"])
    return rejected


def edit_deck(topic: str, source: list[dict], checkpoint_path: Path, resume=False) -> dict:
    fingerprint = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    state = {"version": VERSION, "topic": topic, "source_sha256": fingerprint,
             "source": source, "groups": [], "items": [], "review_passed": False}
    if resume and checkpoint_path.exists():
        state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if (state.get("version"), state.get("topic"), state.get("source_sha256")) != (VERSION, topic, fingerprint):
            raise ValueError("教材檢查點與原始資料或版本不符")

    def save():
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = checkpoint_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(checkpoint_path)

    if not state["groups"]:
        state["groups"] = group_source(topic, source)
        save()
    groups = state["groups"]
    validate_groups(groups, source)
    cards._progress(f"合併完成：原始 {len(source)} 句 → {len(groups)} 個概念，目標 {2 * len(groups)} 句")
    for group in groups:
        existing = [item for item in state["items"] if item.get("core") == group["core"]]
        try:
            validate_pair(group, existing)
        except ValueError:
            state["items"] = [item for item in state["items"] if item.get("core") != group["core"]]
            state["items"].extend(generate_pair(topic, group, source))
            state["review_passed"] = False
            save()
        cards._progress(f"教材配對已完成：{group['core']}（{len(state['items'])}/{2 * len(groups)} 句）")
    for attempt in range(3):
        order = {group["core"]: index for index, group in enumerate(groups)}
        state["items"].sort(key=lambda x: (order[x["core"]], x["tier"] != "basic"))
        for index, item in enumerate(state["items"], start=1):
            item["id"] = f"{index:02d}"
        validate_deck(state["items"], groups, source)
        save()
        rejected = review_edition(topic, state["items"])
        if not rejected:
            # Independent complete semantic partition, not trusting our core labels.
            compact_tips = [dict(item, tips=item["Tone"] + "，確認現場要求。") for item in state["items"]]
            semantic_rejects = cards._ai_review_deck(topic, compact_tips)
            if semantic_rejects:
                raise RuntimeError("教材未通過整副語意審查：" + json.dumps(semantic_rejects, ensure_ascii=False))
            for item, reviewed in zip(state["items"], compact_tips):
                item["_semantic_group"] = reviewed["_semantic_group"]
                item["_semantic_level"] = reviewed["_semantic_level"]
            state["review_passed"] = True
            save()
            return state
        if attempt == 2:
            raise RuntimeError("教材全欄位審查三次未通過：" + json.dumps(rejected, ensure_ascii=False))
        for group in groups:
            if group["core"] in rejected:
                cards._progress(f"教材独立審查退回：{group['core']}：{rejected[group['core']]}")
                state["items"] = [x for x in state["items"] if x["core"] != group["core"]]
                state["items"].extend(generate_pair(topic, group, source, rejected[group["core"]]))
                save()
    raise RuntimeError("教材編輯未完成")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="原始 XLSX（不覆蓋）")
    parser.add_argument("--output", required=True, help="修訂 XLSX")
    parser.add_argument("--topic", default="推銷與隱形敲詐")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    source_path, output_path = Path(args.source).resolve(), Path(args.output).resolve()
    if source_path == output_path or output_path.exists():
        parser.error("必須使用尚未存在的独立輸出路徑，拒絕覆蓋原始或已完成的 Excel")
    source = cards.load_xlsx_items(str(source_path))
    if not source:
        parser.error("原始教材沒有卡片")
    cards._generation_deadline.set(time.monotonic() + cards.GENERATION_TIMEOUT)
    state = edit_deck(args.topic, source, Path(str(output_path) + ".edit.json"), args.resume)
    cards.write_xlsx(state["items"], str(output_path), learning=True)
    cards._progress(f"教材已通過概念、語言與欄位審查，輸出 {len(state['items'])} 句 → {output_path}")


if __name__ == "__main__":
    main()
