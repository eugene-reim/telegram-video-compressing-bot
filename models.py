from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompressionSettings:
    max_duration_seconds: int
    max_height: int
    crf: int
    preset: str
    audio_bitrate: str
    fps: int = 0


@dataclass(frozen=True)
class VideoInfo:
    file_id: str
    duration: int
    file_name: str
    file_size: int
