#!/usr/bin/env python3
"""
download_bgm.py
從 Archive.org（不需 API key）和 Freesound.org（免費 API key）
下載 CC0 授權的環境音樂到 assets/bgm/
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
TARGET_COUNT  = 8   # 下載目標數量


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
        if size < 0.05:          # 太小 = 下載失敗或空檔案
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
    'subject:"ambient music" AND mediatype:audio',
    'subject:"nature sounds" AND mediatype:audio',
    'subject:"white noise" AND mediatype:audio',
    'subject:"lo-fi" AND mediatype:audio',
    'subject:"relaxing music" AND mediatype:audio',
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


def download_from_archive() -> list[str]:
    print("🔍 搜尋 Archive.org CC0 環境音樂...")
    identifiers = archive_search()
    downloaded: list[str] = []

    for iid, title in identifiers:
        if len(downloaded) >= TARGET_COUNT:
            break

        mp3_name = archive_get_mp3(iid)
        if not mp3_name:
            continue

        url  = f"https://archive.org/download/{iid}/{requests.utils.quote(mp3_name)}"
        dest = os.path.join(BGM_DIR, safe_filename(title, prefix="arc_"))

        if os.path.exists(dest):
            print(f"   ⏩ 已存在，跳過: {os.path.basename(dest)}")
            downloaded.append(dest)
            continue

        if download_file(url, dest, label=title[:50]):
            downloaded.append(dest)

        time.sleep(1.2)

    return downloaded


# ── Freesound.org ─────────────────────────────────────────

FREESOUND_QUERIES = [
    "ambient drone loop",
    "study background music",
    "white noise loop",
    "rain ambient",
    "cafe ambience",
    "forest nature sounds",
]


def download_from_freesound() -> list[str]:
    if not FREESOUND_KEY:
        print("ℹ️  未設定 FREESOUND_API_KEY，跳過 Freesound")
        return []

    print("🔍 搜尋 Freesound.org CC0 音效...")
    downloaded: list[str] = []
    seen: set[int] = set()

    for q in FREESOUND_QUERIES:
        if len(downloaded) >= 4:
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
                    downloaded.append(dest)
                    continue
                if download_file(preview, dest, label=name[:50]):
                    downloaded.append(dest)
                time.sleep(0.5)
        except Exception as e:
            print(f"   ⚠️  Freesound 查詢失敗: {e}")
        time.sleep(0.3)

    return downloaded


# ── 主程式 ────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  🎵 VideoFactory BGM 下載器")
    print("  來源: Archive.org (CC0) + Freesound.org (CC0)")
    print("=" * 55)
    print(f"目標資料夾: {BGM_DIR}\n")

    existing = [f for f in os.listdir(BGM_DIR) if f.endswith(".mp3")]
    if existing:
        print(f"📁 已有 {len(existing)} 個檔案\n")

    downloaded: list[str] = []

    # 1. Archive.org（不需 API key）
    downloaded += download_from_archive()

    # 2. Freesound（若有 key 則補充）
    if len(downloaded) < TARGET_COUNT:
        downloaded += download_from_freesound()

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
