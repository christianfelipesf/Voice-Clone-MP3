#!/usr/bin/env python3
"""
Dublador - Preview em tempo real (wrapper fino).
Todo o codigo vive em dublador/preview.py.

Uso:
    from dublador.preview import LivePreview, WebLivePreview
"""
from dublador.preview import (LivePreview, WebLivePreview, ffprobe,
                              has_video_stream)

__all__ = ["LivePreview", "WebLivePreview", "ffprobe", "has_video_stream"]
