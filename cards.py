#!/usr/bin/env python3
"""
cards.py — 詞彙卡片語料前置生成工具
輸入主題 → OpenAI 生成 → 輸出 cards/{topic}.xlsx
已做過的主題自動跳過，跨主題詞彙自動去重。
"""

import glob
import json
import math
import os
import random
import re
import time

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
CARDS_DIR       = os.path.join(BASE_DIR, "cards")
USED_WORDS_FILE = os.path.join(BASE_DIR, "used_words.json")
GENERATE_CHUNK  = 20

os.makedirs(CARDS_DIR, exist_ok=True)

OPENAI_KEYS = [k for k in [os.getenv("OPENAI_API_KEY"), os.getenv("OPENAI_API_KEY_2")] if k]

HEADERS = ["id", "word_en", "word_ipa", "word_cn", "tips",
           "sentence_en", "sentence_ipa", "sentence_cn"]

FIELD_SPEC = """Return a JSON object with a single key "items" whose value is an array of objects.
Each object MUST have exactly these keys:
- "word_en"     : the English word or common phrase
- "word_ipa"    : IPA pronunciation of the word/phrase, enclosed in forward slashes (e.g., "/tʃɑp ˈvɛdʒtəblz/")
- "word_cn"     : Traditional Chinese translation (繁體中文)
- "tips"        : 極短一句話，不要 emoji。提供記憶法、字根拆解、常見搭配詞或使用情境提示。禁止重複 word_cn 的中文解釋。
- "sentence_en" : a natural, conversational English example sentence (not textbook style)
- "sentence_ipa": full IPA pronunciation of the example sentence, enclosed in forward slashes (e.g., "/kæn juː hɛlp miː.../")
- "sentence_cn" : 台灣繁體中文口語意譯（非逐字翻譯）"""


def _call_openai(messages: list, **kwargs):
    from openai import RateLimitError, AuthenticationError, APIError
    if not OPENAI_KEYS:
        raise RuntimeError("未設定任何 OPENAI_API_KEY，請在 .env 補上金鑰")
    last_err = None
    for idx, key in enumerate(OPENAI_KEYS):
        try:
            client = OpenAI(api_key=key)
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


def _is_valid_item(item: dict) -> bool:
    required_keys = ["word_en", "word_ipa", "word_cn", "tips", "sentence_en", "sentence_ipa", "sentence_cn"]
    for key in required_keys:
        val = item.get(key)
        if not val or not isinstance(val, str) or not val.strip():
            return False
    
    # Check if word_ipa or sentence_ipa is just the English text (failed to generate IPA)
    word_en_clean = _normalize_key(item["word_en"])
    word_ipa_clean = _normalize_key(item["word_ipa"])
    if word_en_clean == word_ipa_clean:
        return False
        
    sentence_en_clean = _normalize_key(item["sentence_en"])
    sentence_ipa_clean = _normalize_key(item["sentence_ipa"])
    if sentence_en_clean == sentence_ipa_clean:
        return False
        
    if len(item["word_ipa"].strip()) < 2 or len(item["sentence_ipa"].strip()) < 2:
        return False
        
    return True


def _build_prompt(topic: str, count: int) -> str:
    return f"""以「{topic}」實用的對話為主題，盡量涵蓋各種「{topic}」的狀況，給我 {count} 句。

規則：
1. 以實用對話情境為核心，每個詞彙都要是在「{topic}」場景中真正會用到的。
2. tips 極度短，一句話就好，不要 emoji，不要重複中文解釋。用記憶法、字根拆解或常見搭配詞皆可。
3. sentence_en 必須是實際對話中會說的自然句子，不要教科書式的生硬句子。
4. 所有中文（word_cn, tips, sentence_cn）使用台灣繁體中文日常口語，不要逐字翻譯。
5. 涵蓋「{topic}」的各種不同子情境，不要集中在同一個場景。
6. 優先收錄片語、搭配詞、慣用語，而非單一簡單單字。
7. 必須為每個詞彙和句子生成精確且完整的 IPA 國際音標。國際音標必須使用斜線「/」包裹（例如：/ˈæp.əl/）。絕對不能遺漏音標欄位，也絕對不能直接填入英文拼寫。
"""


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


def generate(topic: str, count: int = 50) -> list[dict]:
    prompt = _build_prompt(topic, count)
    used_words = _load_used_words()
    seen_normalized = { _normalize_key(w) for w in used_words if w }
    all_items: list = []
    # Increase max_rounds in case some generated items are filtered out/invalid
    max_rounds = math.ceil(count / GENERATE_CHUNK) * 2 + 5
    rounds = 0
    consecutive_fails = 0

    while len(all_items) < count and rounds < max_rounds:
        need = count - len(all_items)
        chunk_size = min(GENERATE_CHUNK, need + 3)
        print(f"   🔄 OpenAI #{rounds + 1}（目標 {chunk_size} 個，已有 {len(all_items)} 個）...")

        exclusion_note = ""
        if used_words:
            sample = random.sample(list(used_words), min(200, len(used_words)))
            exclusion_note = f"\nNEVER generate any of these already-used words/phrases: {', '.join(sample)}\n"

        full_prompt = prompt + exclusion_note + f"\nGenerate exactly {chunk_size} items.\n" + FIELD_SPEC
        try:
            resp = _call_openai(
                messages=[{"role": "user", "content": full_prompt}],
                model="gpt-4o-mini",
                response_format={"type": "json_object"},
                # Lower temperature to 0.4 for higher quality, structure compliance, and less random hallucinations
                temperature=0.4,
            )
            raw = json.loads(resp.choices[0].message.content).get("items", [])
        except Exception as e:
            print(f"      ⚠️ API 呼召失敗，等待 2 秒後重試: {e}")
            time.sleep(2)
            consecutive_fails += 1
            if consecutive_fails >= 3:
                print("   ⚠️  連續失敗 3 次，中斷生成。")
                break
            continue

        consecutive_fails = 0
        for item in raw:
            if not _is_valid_item(item):
                print(f"      ⚠️ 跳過格式或音標無效的項目: {item.get('word_en', 'Unknown')}")
                continue
            
            raw_word = item.get("word_en", "")
            key = _normalize_key(raw_word)
            if key and key not in seen_normalized:
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
        rounds += 1

    all_items = all_items[:count]
    for i, item in enumerate(all_items):
        item["id"] = f"{i + 1:02d}"

    new_keys = {item["word_en"].lower().strip().strip(".,!?") for item in all_items if item.get("word_en")}
    _save_used_words(used_words | new_keys)
    return all_items


def write_xlsx(items: list[dict], path: str):
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


def main():
    existing = _existing_topics()
    if existing:
        print(f"✅ 已有主題: {', '.join(existing)}")

    topic = input("\n📌 請輸入主題名稱: ").strip()
    if not topic:
        print("⛔ 主題不能為空")
        return

    slug = _topic_to_slug(topic)
    xlsx_path = os.path.join(CARDS_DIR, f"{slug}.xlsx")
    if os.path.exists(xlsx_path):
        print(f"⚠️  「{topic}」已存在（{xlsx_path}），跳過")
        return

    raw_count = input("🔢 卡片數量（留空=50）: ").strip()
    count = int(raw_count) if raw_count.isdigit() and int(raw_count) > 0 else 50

    used_before = len(_load_used_words())
    print(f"\n🆕 開始生成「{topic}」({count} 個詞彙)...")
    items = generate(topic, count)

    if not items:
        print("❌ 未生成任何詞彙")
        return

    write_xlsx(items, xlsx_path)
    used_after = len(_load_used_words())
    print(f"\n✅ 已生成 {len(items)} 個詞彙 → {xlsx_path}")
    print(f"📝 used_words.json 已更新（{used_before} → {used_after}）")


if __name__ == "__main__":
    main()
