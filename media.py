from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from telegram import Message

from models import CompressionSettings, VideoInfo

logger = logging.getLogger("video-compressor-bot")
ProgressCallback = Optional[Callable[[float, Optional[float]], None]]
VOICE_MAX_BYTES = 50 * 1024 * 1024


def get_video_duration(path: Path) -> Optional[float]:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception as exc:
        logger.warning("ffprobe failed: %s", exc)
    return None


def compress_video(input_path: Path, output_path: Path, settings: CompressionSettings, progress_callback: ProgressCallback = None, fps: int = 0) -> bool:
    filters = [f"scale=-2:'min({settings.max_height},ih)'"]
    if fps > 0:
        filters.append(f"fps={fps}")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(input_path),
        "-c:v", "libx264", "-crf", str(settings.crf), "-preset", settings.preset, "-vf", ",".join(filters),
        "-c:a", "aac", "-b:a", settings.audio_bitrate, "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats", str(output_path),
    ]
    return _run_ffmpeg_with_progress(cmd, input_path, output_path, progress_callback, "Compression")


def has_audio_stream(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception as exc:
        logger.warning("ffprobe audio check failed: %s", exc)
        return False


def extract_audio_opus(input_path: Path, output_path: Path, progress_callback: ProgressCallback = None) -> bool:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(input_path),
        "-vn", "-map", "0:a:0", "-c:a", "libopus", "-b:a", "48k", "-ac", "1", "-application", "voip",
        "-progress", "pipe:1", "-nostats", str(output_path),
    ]
    return _run_ffmpeg_with_progress(cmd, input_path, output_path, progress_callback, "Audio extraction")


def extract_audio_mp3(input_path: Path, output_path: Path, audio_bitrate: str, progress_callback: ProgressCallback = None) -> bool:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(input_path),
        "-vn", "-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", audio_bitrate,
        "-progress", "pipe:1", "-nostats", str(output_path),
    ]
    return _run_ffmpeg_with_progress(cmd, input_path, output_path, progress_callback, "Audio extraction")


def _run_ffmpeg_with_progress(cmd: list[str], input_path: Path, output_path: Path, progress_callback: ProgressCallback, operation: str) -> bool:
    logger.info("Running ffmpeg: %s", " ".join(cmd))
    process: Optional[subprocess.Popen[str]] = None
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if process.stdout is None:
            raise RuntimeError("ffmpeg stdout is unavailable")

        # Late SEI and similar decoder warnings can fill the stderr pipe and
        # deadlock ffmpeg if nobody reads it. Drain in the background.
        stderr_chunks: deque[str] = deque(maxlen=80)

        def _drain_stderr() -> None:
            if process.stderr is None:
                return
            try:
                for err_line in process.stderr:
                    stderr_chunks.append(err_line)
            except Exception:
                pass

        drainer = threading.Thread(target=_drain_stderr, name="ffmpeg-stderr", daemon=True)
        drainer.start()

        duration = get_video_duration(input_path) or 0.0
        last_percent = -1.0
        start_time = time.time()
        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("out_time_ms="):
                try:
                    out_ms = int(line.split("=", 1)[1])
                    if duration > 0:
                        percent = min(99.0, (out_ms / 1_000_000) / duration * 100)
                        if progress_callback and abs(percent - last_percent) >= 2.0:
                            last_percent = percent
                            eta = None
                            if percent > 1:
                                elapsed = time.time() - start_time
                                eta = (elapsed / (percent / 100)) - elapsed
                            progress_callback(percent, eta)
                except ValueError:
                    pass
            elif line.startswith("progress=") and line.endswith("end"):
                break
        process.wait(timeout=3600)
        drainer.join(timeout=2)
        if process.returncode != 0:
            stderr = "".join(stderr_chunks)
            logger.error("%s failed (code %s): %s", operation, process.returncode, stderr[-2000:])
            return False
        if progress_callback:
            progress_callback(100.0, 0)
        return output_path.exists() and output_path.stat().st_size > 0
    except Exception:
        logger.exception("%s error", operation)
        if process is not None and process.poll() is None:
            process.kill()
        return False


def human_size(num: int | float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} ТБ"


def extract_video_info(message: Message) -> Optional[VideoInfo]:
    if message.animation:
        return None
    if message.video:
        video = message.video
        return VideoInfo(video.file_id, video.duration or 0, getattr(video, "file_name", None) or "video.mp4", video.file_size or 0)
    if message.document and message.document.mime_type and message.document.mime_type.startswith("video/") and not (message.document.file_name or "").lower().endswith(".gif") and message.document.mime_type != "image/gif":
        document = message.document
        return VideoInfo(document.file_id, 0, document.file_name or "video.mp4", document.file_size or 0)
    return None
