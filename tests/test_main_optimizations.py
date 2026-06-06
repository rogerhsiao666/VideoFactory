import asyncio
from contextlib import ExitStack
import importlib
import os
import sys
import tempfile
import types
import unittest
from unittest import mock


def _install_dependency_stubs():
    edge_tts = types.ModuleType("edge_tts")

    class _Communicate:
        def __init__(self, *args, **kwargs):
            pass

        async def save(self, path):
            return path

    edge_tts.Communicate = _Communicate
    sys.modules["edge_tts"] = edge_tts

    openai = types.ModuleType("openai")

    class _OpenAI:
        def __init__(self, *args, **kwargs):
            pass

    openai.OpenAI = _OpenAI
    sys.modules["openai"] = openai

    yt = types.ModuleType("youtube_transcript_api")
    yt.__file__ = "/tmp/youtube_transcript_api.py"
    yt.__version__ = "0.0"

    class _YouTubeTranscriptApi:
        pass

    yt.YouTubeTranscriptApi = _YouTubeTranscriptApi
    sys.modules["youtube_transcript_api"] = yt

    moviepy = types.ModuleType("moviepy")

    class _BaseClip:
        def __init__(self, *args, **kwargs):
            self.duration = kwargs.get("duration", 1.0)
            self.audio = None

        def with_duration(self, duration):
            self.duration = duration
            return self

        def with_audio(self, audio):
            self.audio = audio
            self.duration = getattr(audio, "duration", self.duration)
            return self

        def close(self):
            return None

    class _AudioFileClip(_BaseClip):
        def __init__(self, path):
            super().__init__()
            self.path = path
            self.duration = 1.25
            self.nchannels = 2
            self.fps = 44100

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _ImageClip(_BaseClip):
        pass

    class _VideoFileClip(_BaseClip):
        pass

    def _concatenate_videoclips(clips, method=None):
        clip = _BaseClip()
        clip.clips = clips
        clip.method = method
        return clip

    moviepy.ImageClip = _ImageClip
    moviepy.AudioFileClip = _AudioFileClip
    moviepy.VideoFileClip = _VideoFileClip
    moviepy.concatenate_videoclips = _concatenate_videoclips
    moviepy.CompositeAudioClip = _BaseClip
    sys.modules["moviepy"] = moviepy

    moviepy_audio = types.ModuleType("moviepy.audio")
    sys.modules["moviepy.audio"] = moviepy_audio
    moviepy_audio_clip = types.ModuleType("moviepy.audio.AudioClip")

    class _AudioClip(_BaseClip):
        def __init__(self, make_frame=None, duration=0.0, fps=44100):
            super().__init__(duration=duration)
            self.make_frame = make_frame
            self.fps = fps
            self.nchannels = 2

    def _concatenate_audioclips(clips):
        total = _AudioClip(duration=sum(getattr(c, "duration", 0.0) for c in clips))
        total.clips = clips
        return total

    moviepy_audio_clip.AudioClip = _AudioClip
    moviepy_audio_clip.concatenate_audioclips = _concatenate_audioclips
    sys.modules["moviepy.audio.AudioClip"] = moviepy_audio_clip


def _load_main_module():
    _install_dependency_stubs()
    sys.modules.pop("main", None)
    return importlib.import_module("main")


class MainOptimizationsTests(unittest.TestCase):
    def test_video_mode_confirms_intro_before_pexels_download(self):
        main = _load_main_module()

        calls = []

        async def _fake_generate_intro(text, voice="zh-TW-HsiaoChenNeural"):
            calls.append(("intro", text))
            return None

        async def _fake_process_group(*args, **kwargs):
            calls.append(("process_group", kwargs.get("phase")))
            return ([], args[3])

        with tempfile.TemporaryDirectory() as td:
            cards_dir = os.path.join(td, "cards")
            temp_dir = os.path.join(td, "temp")
            output_dir = os.path.join(td, "output")
            os.makedirs(cards_dir, exist_ok=True)
            os.makedirs(temp_dir, exist_ok=True)
            os.makedirs(output_dir, exist_ok=True)
            with open(os.path.join(cards_dir, "Airport.json"), "w", encoding="utf-8") as fh:
                fh.write("[]")
            for filename in ("intro.mp4", "break.mp4", "outro.mp4", "merged_no_bgm.mp4"):
                with open(os.path.join(td if filename != "merged_no_bgm.mp4" else temp_dir, filename), "wb") as fh:
                    fh.write(b"video")

            inputs = iter(["Airport", "", "2", "", "custom intro", ""])

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(main, "BASE_DIR", td))
                stack.enter_context(mock.patch.object(main, "CARDS_DIR", cards_dir))
                stack.enter_context(mock.patch.object(main, "DATA_FILE", os.path.join(td, "data.json")))
                stack.enter_context(mock.patch.object(main, "TEMP_DIR", temp_dir))
                stack.enter_context(mock.patch.object(main, "OUTPUT_DIR", output_dir))
                stack.enter_context(mock.patch.object(main, "PEXELS_KEY", "pexels"))
                stack.enter_context(mock.patch.object(main, "INTRO_VIDEO", os.path.join(td, "intro.mp4")))
                stack.enter_context(mock.patch.object(main, "BREAK_VIDEO", os.path.join(td, "break.mp4")))
                stack.enter_context(mock.patch.object(main, "OUTRO_VIDEO", os.path.join(td, "outro.mp4")))
                stack.enter_context(mock.patch.object(main, "load_local_cards", return_value=[{"id": 1, "word_en": "airport"}]))
                stack.enter_context(mock.patch.object(main, "import_review_excel", return_value=[{"id": 1, "word_en": "airport"}]))
                stack.enter_context(mock.patch.object(main, "export_review_excel", return_value=os.path.join(td, "review.xlsx")))
                stack.enter_context(mock.patch.object(main, "generate_custom_intro", side_effect=_fake_generate_intro))
                stack.enter_context(mock.patch.object(main, "download_pexels_images", side_effect=lambda topic, count=10: calls.append(("pexels", topic, count)) or []))
                stack.enter_context(mock.patch.object(main, "process_group", side_effect=_fake_process_group))
                stack.enter_context(mock.patch.object(main, "write_srt"))
                stack.enter_context(mock.patch.object(main, "write_description"))
                stack.enter_context(mock.patch.object(main, "_video_duration", return_value=1.0))
                stack.enter_context(mock.patch.object(main, "_flush_stdin"))
                stack.enter_context(mock.patch.object(main, "check_assets", return_value=True))
                stack.enter_context(mock.patch.object(main.os, "system", return_value=0))
                stack.enter_context(mock.patch("builtins.input", side_effect=lambda prompt="": next(inputs)))

                asyncio.run(main.main())

        self.assertIn(("intro", "custom intro"), calls)
        self.assertIn(("pexels", "Airport", 1), calls)
        self.assertLess(calls.index(("intro", "custom intro")), calls.index(("pexels", "Airport", 1)))

    def test_load_audio_converts_each_mp3_only_once(self):
        main = _load_main_module()

        with tempfile.TemporaryDirectory() as td:
            mp3_path = os.path.join(td, "sample.mp3")
            wav_path = os.path.join(td, "sample.wav")
            with open(mp3_path, "wb") as fh:
                fh.write(b"mp3")

            def _fake_run(*args, **kwargs):
                with open(wav_path, "wb") as fh:
                    fh.write(b"wav")
                return mock.Mock(returncode=0)

            with mock.patch.object(main.subprocess, "run", side_effect=_fake_run) as run_mock:
                clip1 = main._load_audio(mp3_path)
                clip2 = main._load_audio(mp3_path)

            self.assertEqual(run_mock.call_count, 1)
            self.assertEqual(getattr(clip1, "path", None), wav_path)
            self.assertEqual(getattr(clip2, "path", None), wav_path)


if __name__ == "__main__":
    unittest.main()
