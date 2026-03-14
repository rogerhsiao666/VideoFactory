#!/usr/bin/env python3
"""
一次性預先合成 intro / break / outro × 3 版本 → assets/prebuilt/
執行方式：python build_prebuilt.py
"""
import math
import os
from moviepy import (
    ColorClip, ImageClip, VideoFileClip, AudioFileClip,
    CompositeVideoClip, concatenate_videoclips,
)

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR   = os.path.join(BASE_DIR, "assets")
MOCKUP_DIR   = os.path.join(ASSETS_DIR, "mockup")
MOTION_DIR   = os.path.join(ASSETS_DIR, "motion")
AUDIO_DIR    = os.path.join(ASSETS_DIR, "Audio")
PREBUILT_DIR = os.path.join(ASSETS_DIR, "prebuilt")
os.makedirs(PREBUILT_DIR, exist_ok=True)

W, H = 1920, 1080


def build_one(segment: str, variant: int):
    """合成 {segment}_{variant}.mp4，存入 assets/prebuilt/"""
    audio_path  = os.path.join(AUDIO_DIR,  f"{segment}_{variant}.mp3")
    mockup_path = os.path.join(MOCKUP_DIR, f"{segment}.png")
    motion_path = os.path.join(MOTION_DIR, "1.mp4")
    out_path    = os.path.join(PREBUILT_DIR, f"{segment}_{variant}.mp4")

    if not os.path.exists(audio_path):
        print(f"⚠️  找不到 {audio_path}，略過")
        return

    # 音訊時長
    aud = AudioFileClip(audio_path)
    dur = aud.duration
    aud.close()

    # 底色
    bg_clip = ColorClip(size=(W, H), color=(162, 220, 231), duration=dur)

    # 動態中層（30% opacity，手動迴圈至 dur）
    motion_raw = VideoFileClip(motion_path, audio=False)
    n_loops = math.ceil(dur / motion_raw.duration)
    motion_clip = (
        concatenate_videoclips([motion_raw] * n_loops)
        .subclipped(0, dur)
        .resized((W, H))
        .with_opacity(0.30)
    )

    # 上層 PNG（直接傳路徑，MoviePy v2 正確保留 RGBA Alpha）
    mockup_clip = (
        ImageClip(mockup_path)
        .resized((W, H))
        .with_duration(dur)
    )

    # 合成 + 音訊
    composite = CompositeVideoClip([bg_clip, motion_clip, mockup_clip])
    composite = composite.with_audio(AudioFileClip(audio_path))

    composite.write_videofile(
        out_path,
        fps=30, codec="libx264", audio_codec="aac",
        logger="bar", threads=4,
    )
    composite.close()
    print(f"✅ 完成：{out_path}")


if __name__ == "__main__":
    for seg in ("intro", "break", "outro"):
        for v in (1, 2, 3):
            print(f"\n▶ 合成 {seg}_{v}...")
            build_one(seg, v)
    print("\n🎉 所有 prebuilt 片段已完成！")
