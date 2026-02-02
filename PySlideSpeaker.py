#!/usr/bin/env python3
"""
PySlideSpeaker - Automated Slide Video Generator

CLI tool to automatically generate slide videos (MP4) from PDF materials and YAML scripts.
Features incremental builds to minimize wait times during edits.

Usage:
    python PySlideSpeaker.py --pdf source.pdf --script script.yaml --output output.mp4

Requirements:
    PyMuPDF>=1.23.0      # PDF processing (import as fitz)
    edge-tts>=6.1.0      # Microsoft Edge TTS (async)
    moviepy>=1.0.3       # Video editing
    PyYAML>=6.0          # YAML parsing
"""

from __future__ import annotations

__version__ = "1.1.0"
__author__ = "mfujita47 (Mitsugu Fujita)"

import argparse
import asyncio
import hashlib
import sys
import tempfile
import warnings  # Added explicitly
from concurrent.futures import ThreadPoolExecutor

# Suppress harmless MoviePy warnings about frame reading precision
warnings.filterwarnings("ignore", message=".*bytes wanted but 0 bytes read.*", category=UserWarning)
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Protocol, TypeAlias

import subprocess
import imageio_ffmpeg

# Third-party imports
import edge_tts
import fitz  # PyMuPDF
import yaml
from moviepy import (
    AudioClip,
    AudioFileClip,
    CompositeAudioClip,
    ImageClip,
)


# =============================================================================
# Default Settings & Constants
# =============================================================================

DEFAULT_SETTINGS = {
    "voice": "ja-JP-NanamiNeural",
    "inline_pause": 1.0,
    "slide_pause": 0.0,
    "video_fps": 24,
    "image_dpi": 200,
    "video_codec": "libx264",
    "audio_codec": "aac",
    "rate": "+0%",
}

# =============================================================================
# Type Aliases
# =============================================================================
HashValue: TypeAlias = str
PageNumber: TypeAlias = int

# =============================================================================
# Data Models (Immutable)
# =============================================================================


@dataclass(frozen=True)
class GlobalSettings:
    """グローバル設定"""

    voice: str = DEFAULT_SETTINGS["voice"]
    inline_pause: float = DEFAULT_SETTINGS["inline_pause"]
    slide_pause: float = DEFAULT_SETTINGS["slide_pause"]
    video_fps: int = DEFAULT_SETTINGS["video_fps"]
    image_dpi: int = DEFAULT_SETTINGS["image_dpi"]
    video_codec: str = DEFAULT_SETTINGS["video_codec"]
    audio_codec: str = DEFAULT_SETTINGS["audio_codec"]
    rate: str = DEFAULT_SETTINGS["rate"]


@dataclass(frozen=True)
class SlideEntry:
    """スライド1エントリ分のデータ"""

    index: int  # 0-based index in slides list
    page: PageNumber  # 1-based PDF page number
    text: str
    voice: str | None = None  # Override global voice
    rate: str | None = None  # Override global rate
    note: str | None = None  # Optional note (ignored in processing)


@dataclass
class BuildResult:
    """ビルド結果"""

    success: bool
    output_path: Path | None = None
    cached_count: int = 0
    generated_count: int = 0
    failed_slides: list[int] = field(default_factory=list)
    error_message: str | None = None


# =============================================================================
# Protocols (Interfaces for Extensibility)
# =============================================================================


class TTSEngine(Protocol):
    """音声合成エンジンのインターフェース"""

    async def synthesize(self, text: str, voice: str, rate: str, output_path: Path) -> None:
        """テキストを音声ファイルに変換"""
        ...


class PDFExtractor(Protocol):
    """PDF画像抽出のインターフェース"""

    def extract_page_image(
        self, page: PageNumber, output_path: Path, dpi: int
    ) -> None:
        """PDFページを画像として抽出"""
        ...


# =============================================================================
# Implementations
# =============================================================================


class EdgeTTSEngine:
    """edge-tts を使用したTTS実装"""

    async def synthesize(self, text: str, voice: str, rate: str, output_path: Path) -> None:
        """テキストを音声ファイルに変換"""
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await communicate.save(str(output_path))


class PyMuPDFExtractor:
    """PyMuPDF を使用したPDF画像抽出実装"""

    def __init__(self, pdf_path: Path):
        # Load PDF into memory to avoid repeated file I/O
        self.pdf_bytes = pdf_path.read_bytes()

    def extract_page_image(
        self, page: PageNumber, output_path: Path, dpi: int = 200
    ) -> None:
        """PDFページを画像として抽出（1-based page number）"""
        # Open from memory
        doc = fitz.open("pdf", self.pdf_bytes)
        try:
            # page は 1-based, fitz は 0-based
            if page - 1 >= len(doc):
                raise ValueError(f"Page {page} out of range (max {len(doc)})")

            pdf_page = doc[page - 1]
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = pdf_page.get_pixmap(matrix=mat)
            pix.save(str(output_path))
        finally:
            doc.close()


# =============================================================================
# Core Logic
# =============================================================================


class CacheManager:
    """キャッシュ管理"""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get_clip_path(self, hash_value: HashValue) -> Path:
        """クリップファイルパスを取得（ハッシュベース）"""
        return self.cache_dir / f"clip_{hash_value}.mp4"

    def cleanup_unused_clips(self, used_paths: list[Path]) -> None:
        """使用されなかったキャッシュファイルを削除"""
        used_filenames = {p.name for p in used_paths}
        for clip_file in self.cache_dir.glob("clip_*.mp4"):
            if clip_file.name not in used_filenames:
                print(f"Removing unused cache: {clip_file.name}")
                clip_file.unlink(missing_ok=True)


def compute_slide_hash(
    slide: SlideEntry, global_settings: GlobalSettings, pdf_mtime: float, pdf_size: int
) -> HashValue:
    """スライドエントリのハッシュを計算"""
    voice = slide.voice or global_settings.voice
    rate = slide.rate or global_settings.rate
    # ハッシュに影響する要素: ページ番号, テキスト, 音声, 速度, ポーズ長, 遷移ポーズ長, PDFの更新日時, PDFのサイズ, DPI, FPS, Codecs
    content = (
        f"{slide.page}|{slide.text}|{voice}|{rate}|"
        f"{global_settings.inline_pause}|{global_settings.slide_pause}|"
        f"{pdf_mtime}|{pdf_size}|{global_settings.image_dpi}|{global_settings.video_fps}|"
        f"{global_settings.video_codec}|{global_settings.audio_codec}"
    )
    return hashlib.md5(content.encode("utf-8")).hexdigest()


class AudioProcessor:
    """音声処理"""

    def __init__(self, tts_engine: TTSEngine, temp_dir: Path):
        self.tts_engine = tts_engine
        self.temp_dir = temp_dir
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=4)

    async def create_audio_with_pauses(
        self,
        text: str,
        voice: str,
        rate: str,
        inline_pause: float,
        slide_pause: float,
        output_path: Path,
    ) -> None:
        """
        [pause]タグを処理し、無音を挿入した音声ファイルを生成
        slide_pause: 音声末尾に追加する無音時間（秒）- 次のスライドへの遷移待ち時間
        """
        segments = text.split("[pause]")
        audio_clips: list[AudioClip] = []
        temp_files: list[Path] = []

        try:
            for i, segment in enumerate(segments):
                segment = segment.strip()
                if not segment:
                    continue

                # 一時ファイルに音声を生成 (edge-tts is async native, so we await it)
                temp_audio = self.temp_dir / f"segment_{output_path.stem}_{i}.mp3"
                temp_files.append(temp_audio)
                await self.tts_engine.synthesize(segment, voice, rate, temp_audio)

                # AudioClip として読み込み (Blocking I/O - run in executor)
                # Note: creating AudioFileClip is fast, but better safe than sorry if we process many
                audio_clip = await asyncio.get_running_loop().run_in_executor(
                    self._executor, AudioFileClip, str(temp_audio)
                )
                audio_clips.append(audio_clip)

                # 最後のセグメント以外はポーズを追加
                if i < len(segments) - 1 and inline_pause > 0:
                    silence = AudioClip(lambda t: 0, duration=inline_pause, fps=44100)
                    audio_clips.append(silence)

            # 末尾の遷移用ポーズを追加
            if slide_pause > 0:
                silence = AudioClip(lambda t: 0, duration=slide_pause, fps=44100)
                audio_clips.append(silence)

            if not audio_clips:
                raise ValueError("No audio segments generated")

            # CompositeAudioClip で順次配置
            current_start = 0.0
            clips_with_start = []
            for clip in audio_clips:
                clips_with_start.append(clip.with_start(current_start))
                current_start += clip.duration

            final_audio = CompositeAudioClip(clips_with_start)
            final_audio = final_audio.with_duration(current_start)

            # MP3として保存 (Blocking I/O - run in executor)
            await asyncio.get_running_loop().run_in_executor(
                self._executor,
                partial(
                    final_audio.write_audiofile,
                    str(output_path),
                    fps=44100,
                    logger=None,
                ),
            )

        finally:
            # クリップを閉じる
            for clip in audio_clips:
                try:
                    # moviepy objects usually don't need async close, but accessing them might be safe to keep in sync context
                    # or just close them directly since it's mostly releasing file handles
                    clip.close()
                except Exception:
                    pass
            # 一時ファイル削除
            for temp_file in temp_files:
                temp_file.unlink(missing_ok=True)

    def shutdown(self):
        self._executor.shutdown(wait=True)


class VideoGenerator:
    """動画生成"""

    def __init__(self, pdf_extractor: PDFExtractor, temp_dir: Path, dpi: int):
        self.pdf_extractor = pdf_extractor
        self.temp_dir = temp_dir
        self.dpi = dpi
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=2) # MoviePy + PDF can be heavy

    async def create_slide_clip(
        self,
        page: PageNumber,
        audio_path: Path,
        output_path: Path,
        fps: int,
        video_codec: str,
        audio_codec: str
    ) -> None:
        """スライド画像と音声から動画クリップを生成"""
        # 画像抽出
        image_path = self.temp_dir / f"slide_{page:04d}.png"

        # Run PDF extraction in executor
        await asyncio.get_running_loop().run_in_executor(
            self._executor,
            partial(
                self.pdf_extractor.extract_page_image,
                page, image_path, self.dpi
            )
        )

        try:
            # Run Video generation in executor
            await asyncio.get_running_loop().run_in_executor(
                self._executor,
                partial(
                    self._generate_video_task,
                    image_path, audio_path, output_path, fps, video_codec, audio_codec
                )
            )

        finally:
            # 一時画像削除
            image_path.unlink(missing_ok=True)

    def _generate_video_task(
        self,
        image_path: Path,
        audio_path: Path,
        output_path: Path,
        fps: int,
        video_codec: str,
        audio_codec: str
    ):
        """Executor内で実行される同期的な動画生成処理"""
        audio_clip = None
        image_clip = None
        try:
            # 音声読み込み
            audio_clip = AudioFileClip(str(audio_path))
            duration = audio_clip.duration

            # 画像クリップ作成
            image_clip = ImageClip(str(image_path)).with_duration(duration)
            image_clip = image_clip.with_audio(audio_clip)

            # 動画書き出し
            image_clip.write_videofile(
                str(output_path),
                fps=fps,
                codec=video_codec,
                audio_codec=audio_codec,
                logger=None,  # Suppress MoviePy log output
                temp_audiofile=str(output_path.with_suffix('.temp.m4a')), # Avoid WinError 32
                remove_temp=True,
            )
        finally:
             # リソース解放
            try:
                if audio_clip: audio_clip.close()
            except Exception:
                pass
            try:
                if image_clip: image_clip.close()
            except Exception:
                pass

    def shutdown(self):
        self._executor.shutdown(wait=True)


class PySlideSpeakerBuilder:
    """メインビルダー"""

    def __init__(
        self,
        pdf_path: Path,
        script_path: Path,
        output_path: Path,
        cache_dir: Path,
    ):
        self.pdf_path = pdf_path
        self.script_path = script_path
        self.output_path = output_path
        self.cache_dir = cache_dir

        # Components
        self.tts_engine = EdgeTTSEngine()
        self.pdf_extractor = PyMuPDFExtractor(pdf_path) # Pass path to constructor
        self.cache_manager = CacheManager(self.cache_dir)

    def _load_script_and_settings(self) -> tuple[GlobalSettings, list[SlideEntry]]:
        """YAMLスクリプトを読み込み、設定をマージ"""
        data = yaml.safe_load(self.script_path.read_text(encoding="utf-8"))

        # 1. Defaults
        settings_dict = DEFAULT_SETTINGS.copy()

        if "global_settings" in data:
            # Map old keys to new if present (backward compatibility)
            gs = data["global_settings"]
            if "pause_duration" in gs:
                gs["inline_pause"] = gs.pop("pause_duration")
            if "transition_pause" in gs:
                gs["slide_pause"] = gs.pop("transition_pause")
            settings_dict.update(gs)

        # Build GlobalSettings object
        global_settings = GlobalSettings(
            voice=str(settings_dict["voice"]),
            inline_pause=float(settings_dict["inline_pause"]),
            slide_pause=float(settings_dict["slide_pause"]),
            video_fps=int(settings_dict["video_fps"]),
            image_dpi=int(settings_dict["image_dpi"]),
            video_codec=str(settings_dict["video_codec"]),
            audio_codec=str(settings_dict["audio_codec"]),
            rate=str(settings_dict["rate"]),
        )

        # Slides
        slides: list[SlideEntry] = []
        for i, slide_data in enumerate(data.get("slides", [])):
            slides.append(
                SlideEntry(
                    index=i,
                    page=slide_data["page"],
                    text=slide_data["text"],
                    voice=slide_data.get("voice"),
                    rate=slide_data.get("rate"),
                    note=slide_data.get("note"),
                )
            )

        return global_settings, slides

    async def _process_slide(
        self,
        slide: SlideEntry,
        global_settings: GlobalSettings,
        audio_processor: AudioProcessor,
        video_generator: VideoGenerator,
        clip_path: Path,
    ) -> None:
        """1スライドを処理"""
        voice = slide.voice or global_settings.voice

        # 音声生成
        audio_path = self.cache_dir / f"audio_{clip_path.stem}.mp3" # Use clip hash for audio temp name too
        await audio_processor.create_audio_with_pauses(
            slide.text,
            voice,
            slide.rate or global_settings.rate,
            global_settings.inline_pause,
            global_settings.slide_pause,
            audio_path
        )

        try:
            # 動画生成
            await video_generator.create_slide_clip(
                slide.page,
                audio_path,
                clip_path,
                global_settings.video_fps,
                global_settings.video_codec,
                global_settings.audio_codec
            )
        finally:
            # 音声ファイル削除
            audio_path.unlink(missing_ok=True)

    async def build(self) -> BuildResult:
        """ビルド実行"""
        audio_processor = None
        video_generator = None

        try:
            # 入力ファイル存在チェック
            if not self.pdf_path.exists():
                return BuildResult(success=False, error_message=f"PDF file not found: {self.pdf_path}")
            if not self.script_path.exists():
                return BuildResult(success=False, error_message=f"Script file not found: {self.script_path}")

            # 出力ディレクトリ作成
            self.output_path.parent.mkdir(parents=True, exist_ok=True)

            # 設定とスライド読み込み
            global_settings, slides = self._load_script_and_settings()

            if not slides:
                return BuildResult(success=False, error_message="No slides defined in script")

            # PDF情報取得 (ハッシュに使用)
            pdf_stat = self.pdf_path.stat()
            pdf_mtime = pdf_stat.st_mtime
            pdf_size = pdf_stat.st_size

            # 一時ディレクトリ & Executors
            temp_dir_obj = tempfile.TemporaryDirectory()
            temp_path = Path(temp_dir_obj.name)

            audio_processor = AudioProcessor(self.tts_engine, temp_path / "audio")
            video_generator = VideoGenerator(self.pdf_extractor, temp_path / "video", global_settings.image_dpi)

            cached_count = 0
            generated_count = 0
            failed_slides: list[int] = []
            final_clip_paths: list[Path] = []

            # スライドごとに処理
            total_slides = len(slides)
            print(f"Processing {total_slides} slides...")

            for i, slide in enumerate(slides):
                slide_hash = compute_slide_hash(slide, global_settings, pdf_mtime, pdf_size)
                clip_path = self.cache_manager.get_clip_path(slide_hash)

                # キャッシュチェック (ファイルが存在すればOK)
                if clip_path.exists():
                    print(f"[{i+1}/{total_slides}] Cached: Slide {slide.page}")
                    cached_count += 1
                    final_clip_paths.append(clip_path)
                    continue

                # 新規生成
                print(f"[{i+1}/{total_slides}] Generating: Slide {slide.page}")
                try:
                    await self._process_slide(
                        slide,
                        global_settings,
                        audio_processor,
                        video_generator,
                        clip_path,
                    )
                    generated_count += 1
                    final_clip_paths.append(clip_path)

                except Exception as e:
                    print(f"[{i+1}/{total_slides}] FAILED: Slide {slide.page} - {e}")
                    failed_slides.append(slide.index)
                    # Don't add to final clips if failed
                    continue

            # キャッシュクリーンアップ
            self.cache_manager.cleanup_unused_clips(final_clip_paths)

            if not final_clip_paths:
                return BuildResult(
                    success=False,
                    error_message="No clips were generated successfully",
                    failed_slides=failed_slides,
                )

            # 最終結合
            print(f"\nConcatenating clips -> {self.output_path.name}...")

            # 結合は重い処理なのでExecutorでやりたいが、moviepyのconcatenateは複雑なので
            # シンプルに同期的に呼ぶか、別途ラップする。
            # ここではメインループをブロックしないようにExecutor推奨だが、
            # VideoFileClipの扱いに注意。

            await asyncio.get_running_loop().run_in_executor(
                video_generator._executor, # Reuse video generator's executor
                partial(
                    self._concatenate_clips,
                    final_clip_paths,
                    self.output_path
                )
            )

            return BuildResult(
                success=True,
                output_path=self.output_path,
                cached_count=cached_count,
                generated_count=generated_count,
                failed_slides=failed_slides,
            )

        except Exception as e:
            return BuildResult(success=False, error_message=str(e))
        finally:
            if audio_processor:
                audio_processor.shutdown()
            if video_generator:
                video_generator.shutdown()
            if 'temp_dir_obj' in locals():
                temp_dir_obj.cleanup()

    def _concatenate_clips(self, clip_paths: list[Path], output_path: Path):
        """動画結合処理 (FFmpeg concat demuxer)"""
        if not clip_paths:
            return

        # Create concat list file (use a safer name or temp dir)
        concat_file = output_path.with_name(f"_concat_{output_path.stem}.txt")
        try:
            with open(concat_file, 'w', encoding='utf-8') as f:
                for cp in clip_paths:
                    # FFmpeg requires forward slashes and escaped single quotes
                    safe_path = str(cp.resolve()).replace('\\', '/').replace("'", "'\\''")
                    f.write(f"file '{safe_path}'\n")

            print("Running FFmpeg concat...")
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

            # fast start / steam copy
            cmd = [
                ffmpeg_exe,
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_file),
                "-c", "copy",
                str(output_path)
            ]

            # Run
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if result.returncode != 0:
                raise RuntimeError(f"FFmpeg failed:\n{result.stderr.decode('utf-8')}")

        finally:
            concat_file.unlink(missing_ok=True)


# =============================================================================
# CLI
# =============================================================================


def parse_args() -> argparse.Namespace:
    """コマンドライン引数をパース"""
    parser = argparse.ArgumentParser(
        prog="PySlideSpeaker",
        description="Automated Slide Video Generator from PDF and YAML script",
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        default=None,
        help="Input PDF file path (default: auto-detect single .pdf in current dir)",
    )
    parser.add_argument(
        "--script",
        type=Path,
        default=None,
        help="YAML script file path (default: auto-detect single .yaml in current dir)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output file path (default: {pdf_name}.mp4)",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="Cache directory (default: current dir)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Clean cache and rebuild completely",
    )
    return parser.parse_args()


def _find_single_file(pattern: str, description: str) -> Path:
    """
    カレントディレクトリで指定パターンのファイルを探す。
    1つだけ見つかればそれを返す。0個または複数の場合はエラー。
    """
    cwd = Path.cwd()
    files = list(cwd.glob(pattern))

    if len(files) == 0:
        print(
            f"Error: No {description} file found in current directory.", file=sys.stderr
        )
        print(
            f"  Specify with --{description.split()[0].lower()} option.",
            file=sys.stderr,
        )
        sys.exit(1)
    elif len(files) > 1:
        print(f"Error: Multiple {description} files found:", file=sys.stderr)
        for f in files:
            print(f"  - {f.name}", file=sys.stderr)
        print(
            f"  Specify one with --{description.split()[0].lower()} option.",
            file=sys.stderr,
        )
        sys.exit(1)

    return files[0]


def main() -> int:
    """メインエントリーポイント"""
    args = parse_args()

    # PDFパス解決（未指定時はカレントディレクトリから自動検出）
    if args.pdf is None:
        pdf_path = _find_single_file("*.pdf", "PDF")
    else:
        pdf_path = args.pdf.resolve()

    # スクリプトパス解決（未指定時はカレントディレクトリから自動検出）
    if args.script is None:
        script_path = _find_single_file("*.yaml", "YAML script")
    else:
        script_path = args.script.resolve()


    output_file = args.output.resolve() if args.output else Path(f"{pdf_path.stem}.mp4").resolve()

    # Cache logic: if not specified, use {pdf_stem} in current directory
    if args.cache:
        cache_dir = args.cache.resolve()
    else:
        cache_dir = Path(pdf_path.stem).resolve()

    print(f"PySlideSpeaker - Automated Slide Video Generator")
    print(f"=" * 50)
    print(f"PDF:    {pdf_path}")
    print(f"Script: {script_path}")
    print(f"Output: {output_file}")
    print(f"Cache:  {cache_dir}")
    print()

    # ビルダー作成
    builder = PySlideSpeakerBuilder(pdf_path, script_path, output_file, cache_dir)
    if args.clean:
        print("Cleaning cache...")
        for f in cache_dir.glob("*"):
            if f.is_file():
                f.unlink()

    # ビルド実行
    result = asyncio.run(builder.build())

    # 結果表示
    print()
    print("=" * 50)
    if result.success:
        print(f"✓ Build successful!")
        print(f"  Output: {result.output_path}")
        print(f"  Cached: {result.cached_count}, Generated: {result.generated_count}")
        if result.failed_slides:
            print(f"  ⚠ Failed slides (skipped): {result.failed_slides}")
        return 0
    else:
        print(f"✗ Build failed: {result.error_message}")
        if result.failed_slides:
            print(f"  Failed slides: {result.failed_slides}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
