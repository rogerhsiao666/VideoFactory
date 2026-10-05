"""Video fields and private intermediate paths shared by generation and rendering."""

import hashlib
import shutil
from pathlib import Path


VIDEO_HEADERS = ["id", "word_en", "word_ipa", "word_cn", "tips", "sentence_en", "sentence_cn"]


def cached_artifact(source: Path, base_dir: str, category: str) -> Path:
    identity = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
    return Path(base_dir) / "temp" / "cache" / category / identity / source.name


def preserve_legacy_cache(source: Path, target: Path) -> None:
    if source.is_file() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
