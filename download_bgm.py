#!/usr/bin/env python3
"""
download_bgm.py
從 Archive.org（不需 API key）和 Freesound.org（免費 API key）
下載 CC0 授權的 Lo-Fi / Chillhop 音樂到 assets/bgm/
"""

import os
import json
import time
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
BGM_DIR     = os.path.join(BASE_DIR, "assets", "bgm")
os.makedirs(BGM_DIR, exist_ok=True)

FREESOUND_KEY = os.getenv("FREESOUND_API_KEY", "")
TARGET_COUNT  = 10  # 下載目標數量


# ── 工具函數 ──────────────────────────────────────────────

def safe_filename(text: str, prefix: str = "", maxlen: int = 45) -> str:
    clean = "".join(c if c.isalnum() or c in " -_" else "_" for c in text).strip()
    return f"{prefix}{clean[:maxlen]}.mp3"


def download_file(url: str, dest: str, label: str = "") -> bool:
    """串流下載，顯示檔案大小"""
    try:
        r = requests.get(url, timeout=90, stream=True)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=16384):
                f.write(chunk)
        size = os.path.getsize(dest) / (1024 * 1024)
        if size < 0.05:
            os.remove(dest)
            print(f"   ⚠️  {label} — 檔案過小，跳過")
            return False
        print(f"   ✅ {label} ({size:.1f} MB)")
        return True
    except Exception as e:
        print(f"   ❌ {label} 失敗: {e}")
        if os.path.exists(dest):
            os.remove(dest)
        return False


# ── Archive.org ───────────────────────────────────────────

ARCHIVE_QUERIES = [
    'title:"rain" AND mediatype:audio',
    'title:"ocean waves" AND mediatype:audio',
    'title:"waterfall" AND mediatype:audio',
    'title:"stream" AND mediatype:audio',
    'title:"river" AND mediatype:audio',
    'title:"thunder" AND mediatype:audio',
    'title:"campfire" AND mediatype:audio',
    'title:"wind" AND mediatype:audio',
    'title:"brook" AND mediatype:audio',
    'title:"creek" AND mediatype:audio',
    'title:"waves" AND mediatype:audio',
    'title:"rainfall" AND mediatype:audio',
    'title:"fireplace" AND mediatype:audio',
    'title:"storm" AND mediatype:audio',
    'title:"crickets" AND mediatype:audio',
]

CC0_FILTER = 'licenseurl:"https://creativecommons.org/publicdomain/zero/1.0/"'


def archive_search() -> list[tuple[str, str]]:
    """回傳 [(identifier, title), ...] CC0 音訊清單"""
    results = []
    seen: set[str] = set()

    for q in ARCHIVE_QUERIES:
        if len(results) >= TARGET_COUNT * 3:
            break
        url = (
            "https://archive.org/advancedsearch.php"
            f"?q={requests.utils.quote(q)}"
            f"&fq[]={requests.utils.quote(CC0_FILTER)}"
            "&fl[]=identifier,title"
            "&output=json&rows=12&sort[]=downloads+desc"
        )
        try:
            data = requests.get(url, timeout=15).json()
            docs = data.get("response", {}).get("docs", [])
            for doc in docs:
                iid = doc.get("identifier", "")
                if iid and iid not in seen:
                    seen.add(iid)
                    results.append((iid, doc.get("title", iid)))
        except Exception as e:
            print(f"   ⚠️  Archive 搜尋失敗: {e}")
        time.sleep(0.4)

    return results


def archive_get_mp3(identifier: str) -> str | None:
    """從 Archive.org item 取第一個 MP3 檔名"""
    try:
        data = requests.get(
            f"https://archive.org/metadata/{identifier}/files",
            timeout=15,
        ).json()
        for f in data.get("result", []):
            fmt  = f.get("format", "").lower()
            name = f.get("name", "")
            if fmt in ("mp3", "vbr mp3") and name.lower().endswith(".mp3"):
                return name
    except Exception:
        pass
    return None


def download_from_archive(need: int) -> list[str]:
    print("🔍 搜尋 Archive.org CC0 自然音效...")
    identifiers = archive_search()
    downloaded: list[str] = []

    for iid, title in identifiers:
        if len(downloaded) >= need:
            break

        mp3_name = archive_get_mp3(iid)
        if not mp3_name:
            continue

        url  = f"https://archive.org/download/{iid}/{requests.utils.quote(mp3_name)}"
        dest = os.path.join(BGM_DIR, safe_filename(title, prefix="arc_"))

        if os.path.exists(dest):
            print(f"   ⏩ 已存在，跳過: {os.path.basename(dest)}")
            continue

        if download_file(url, dest, label=title[:50]):
            downloaded.append(dest)

        time.sleep(1.2)

    return downloaded


# ── Freesound.org ─────────────────────────────────────────

FREESOUND_QUERIES = [
    "rain ambience",
    "ocean waves",
    "waterfall",
    "river flowing",
    "campfire crackling",
    "thunderstorm",
    "wind ambience",
    "fireplace",
    "crickets night",
    "rain on window",
]


def download_from_freesound(need: int) -> list[str]:
    if not FREESOUND_KEY:
        print("ℹ️  未設定 FREESOUND_API_KEY，跳過 Freesound")
        return []

    print("🔍 搜尋 Freesound.org CC0 音效...")
    downloaded: list[str] = []
    seen: set[int] = set()

    for q in FREESOUND_QUERIES:
        if len(downloaded) >= need:
            break
        url = (
            "https://freesound.org/apiv2/search/text/"
            f"?query={requests.utils.quote(q)}"
            '&filter=license:"Creative Commons 0" duration:[20 TO 600]'
            "&fields=id,name,previews"
            f"&page_size=6&token={FREESOUND_KEY}"
        )
        try:
            results = requests.get(url, timeout=15).json().get("results", [])
            for sound in results:
                sid = sound.get("id")
                if sid in seen:
                    continue
                seen.add(sid)
                preview = sound.get("previews", {}).get("preview-hq-mp3", "")
                if not preview:
                    continue
                name = sound.get("name", str(sid))
                dest = os.path.join(BGM_DIR, safe_filename(name, prefix="fs_"))
                if os.path.exists(dest):
                    print(f"   ⏩ 已存在，跳過: {os.path.basename(dest)}")
                    continue
                if download_file(preview, dest, label=name[:50]):
                    downloaded.append(dest)
                time.sleep(0.5)
        except Exception as e:
            print(f"   ⚠️  Freesound 查詢失敗: {e}")
        time.sleep(0.3)

    return downloaded


# ── 正規化音量 ────────────────────────────────────────────

def normalize_bgm(path: str) -> bool:
    """用 ffmpeg loudnorm 將音量統一到 -18 LUFS，原地覆蓋。"""
    tmp = path + ".tmp.mp3"
    ret = os.system(
        f'ffmpeg -y -i "{path}" '
        f'-af loudnorm=I=-18:TP=-1.5:LRA=11 '
        f'-c:a libmp3lame -q:a 4 "{tmp}" -loglevel error'
    )
    if ret == 0 and os.path.exists(tmp):
        os.replace(tmp, path)
        return True
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


# ── 主程式 ────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  🎵 VideoFactory BGM 下載器")
    print("  來源: Archive.org (CC0) + Freesound.org (CC0)")
    print("=" * 55)
    print(f"目標資料夾: {BGM_DIR}\n")


    existing = [f for f in os.listdir(BGM_DIR) if f.endswith(".mp3")]
    need = max(0, TARGET_COUNT - len(existing))
    print(f"📁 已有 {len(existing)} 個，還需下載 {need} 個\n")

    downloaded: list[str] = []

    if need > 0:
        # 1. Archive.org（不需 API key）
        downloaded += download_from_archive(need)

        # 2. Freesound（若有 key 則補充）
        still_need = need - len(downloaded)
        if still_need > 0:
            downloaded += download_from_freesound(still_need)

    # ── 正規化所有 BGM 音量 ───────────────────────────────
    all_files = [f for f in os.listdir(BGM_DIR) if f.endswith(".mp3")]
    if all_files:
        print(f"\n🔊 正規化音量（loudnorm -18 LUFS）...")
        for fname in sorted(all_files):
            fpath = os.path.join(BGM_DIR, fname)
            ok = normalize_bgm(fpath)
            print(f"   {'✅' if ok else '⚠️ '} {fname}")

    # ── 結果報告 ──────────────────────────────────────────
    all_files = [f for f in os.listdir(BGM_DIR) if f.endswith(".mp3")]
    print(f"\n{'=' * 55}")
    print(f"✅ 完成！assets/bgm/ 現有 {len(all_files)} 個背景音樂：")
    for fname in sorted(all_files):
        size = os.path.getsize(os.path.join(BGM_DIR, fname)) / (1024 * 1024)
        print(f"   🎵 {fname} ({size:.1f} MB)")

    if not all_files:
        print("\n❌ 沒有成功下載任何檔案。")
        print("   建議手動放一個 MP3 到 assets/bgm/ 資料夾")
        print("   或在 .env 加入 FREESOUND_API_KEY 後重試")
    else:
        print(f"\n🎬 執行 main.py 時會從以上 {len(all_files)} 首隨機挑選 BGM")

    print("=" * 55)


if __name__ == "__main__":
    main()
