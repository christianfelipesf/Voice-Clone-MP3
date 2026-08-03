#!/usr/bin/env python3
"""
Dublador - Configuracoes compartilhadas
=======================================
Constantes, caminhos e utilitarios usados por todos os modulos
(core, youtube, preview, gui, web). Evita duplicacao.
"""

import os
import re
import sys
import shutil
import subprocess
import json

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(PACKAGE_DIR)

SCRIPT = os.path.join(BASE_DIR, "dublar.py")
YT_SCRIPT = os.path.join(BASE_DIR, "dublar_yt.py")
PYTHON = sys.executable
CONFIG_PATH = os.path.join(BASE_DIR, "dublador_config.json")

STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOAD_DIR = os.path.join(BASE_DIR, "web_uploads")
JOBS_DIR = os.path.join(BASE_DIR, "web_jobs")

DEVICES = ["auto", "cuda", "cpu"]
LANGS = ["pt", "en", "es", "fr", "de", "it", "zh", "ja", "ko"]
WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3"]
RESOLUTIONS = ["360", "720", "1080", "480", "240", "144", "best"]
BROWSERS = ["", "edge", "chrome", "firefox", "brave", "opera"]
ENGINE_LABELS = {"chatterbox": "Chatterbox (clonagem)",
                 "edge": "Edge TTS (leve)"}

PHASES = [
    ("Baixando video", "Baixando video..."),
    ("Transcrevendo e traduzindo", "Transcrevendo e traduzindo..."),
    ("Transcrevendo", "Transcrevendo..."),
    ("Preparando audio", "Preparando audio..."),
    ("Carregando motor", "Carregando motor de voz..."),
    ("Dublagem de", "Dublagem..."),
    ("Finalizando", "Finalizando..."),
]

# Valores padrao usados ao resetar as opcoes.
DEFAULTS = {
    "device": "auto",
    "lang": "pt",
    "res": "720",
    "whisper": "small",
    "engine": "chatterbox",
    "volume": "1.0",
    "temp": "",
    "seed": "",
    "maxtempo": "",
    "cookies": "",
    "theme_dark": False,
    "mode": "Arquivo",
    "preview": False,
}

_LABEL_TO_VALUE = {label: value for value, label in ENGINE_LABELS.items()}
_VALUE_TO_LABEL = dict(ENGINE_LABELS)


def engine_to_value(engine):
    """Converte label OU valor do motor no valor canonico ('edge'/'chatterbox').
    Aceita os dois formatos para ser tolerante com configs antigos."""
    if engine in _LABEL_TO_VALUE:
        return _LABEL_TO_VALUE[engine]
    if engine in _VALUE_TO_LABEL:
        return engine
    return DEFAULTS["engine"]


def engine_to_label(engine):
    return _VALUE_TO_LABEL.get(engine_to_value(engine), DEFAULTS["engine"])


def force_utf8_stdout():
    """Forca UTF-8 no stdout/stderr. Sem isso, quando o processo e pipeado
    (menu grafico/web) o Python no Windows usa cp1252 e os acentos chegam
    corrompidos."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(data):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception:
        return False


def reset_config():
    """Apaga o arquivo de config salvo, voltando aos padroes."""
    try:
        os.remove(CONFIG_PATH)
        return True
    except OSError:
        return False


def sanitize(name):
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .") or "arquivo"


def has_video_stream(path):
    """Detecta se o arquivo tem stream de video (mp4, mkv, mov, avi,
    webm...). Usa ffprobe e, como fallback, o proprio ffmpeg."""
    try:
        fp = shutil.which("ffprobe")
        if fp:
            r = subprocess.run(
                [fp, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=60)
            if "video" in r.stdout.lower():
                return True
    except Exception:
        pass
    try:
        r = subprocess.run(["ffmpeg", "-i", path],
                           capture_output=True, text=True, timeout=60)
        return "Video:" in r.stderr
    except Exception:
        return False


def ffmpeg_available():
    return shutil.which("ffmpeg") is not None
