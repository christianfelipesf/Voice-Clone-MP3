#!/usr/bin/env python3
"""
Dublador - Motor de dublagem com clonagem de voz offline (modulo core)
=====================================================================
Dubla um arquivo de audio usando o texto de um .srt (traducao) e
clonando a voz do audio ORIGINAL, 100% offline.

Sem .srt: o script detecta o idioma da midia, transcreve com Whisper
(faster-whisper, com timestamps) e traduz cada fala para o idioma de
saida (padrao pt) via Google Translate. Depois dubla como se fosse um
.srt gerado.

Para cada legenda, o texto e falado no idioma escolhido clonando
a voz do proprio trecho original (referencia por fala). A fala original
da legenda e silenciada e a voz dublada entra no lugar, esticada
(rubberband) para ter a MESMA duracao da fala original.

Uso (CLI):
    python dublar.py --audio "audio_para_dublar\\audio.mp3" --srt "audio_para_dublar\\audio.srt"
    python dublar.py --audio a.mp3 --srt a.srt --out a_dublado.mp3 --device cuda --volume 1.2
    python dublar.py --audio filme.mp4 --srt filme.srt        # video mantem a imagem
    python dublar.py --audio a.mp3                            # modo automatico
    python dublar.py --audio a.mp3 --engine edge              # motor leve (sem clonagem)
"""

import os
import re
import sys
import time
import argparse
import shutil
import subprocess

import numpy as np
import soundfile as sf

from dublador.config import force_utf8_stdout, has_video_stream

SRC_SR = 24000          # amostragem da saida do Chatterbox
TARGET_SR = 44100       # amostragem final
FADE_MS = 15            # crossfade nas bordas de cada legenda
CHATTERBOX_ENGINE = None
CHATTERBOX_T3_MODEL = "v3"   # Chatterbox Multilingual V3

# Vozes do Edge TTS (motor leve) por idioma de saida.
# Cada idioma tem uma voz feminina e uma masculina; a dublagem alterna
# entre elas a cada legenda para soar mais natural.
EDGE_VOICES = {
    "pt": ("pt-BR-FranciscaNeural", "pt-BR-AntonioNeural"),
    "en": ("en-US-JennyNeural", "en-US-GuyNeural"),
    "es": ("es-ES-ElviraNeural", "es-ES-AlvaroNeural"),
    "fr": ("fr-FR-DeniseNeural", "fr-FR-HenriNeural"),
    "de": ("de-DE-KatjaNeural", "de-DE-ConradNeural"),
    "it": ("it-IT-ElsaNeural", "it-IT-DiegoNeural"),
    "zh": ("zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural"),
    "ja": ("ja-JP-NanamiNeural", "ja-JP-KeitaNeural"),
    "ko": ("ko-KR-SunHiNeural", "ko-KR-InJoonNeural"),
}


def patch_torchaudio_soundfile():
    """torchaudio 2.9+ depende do torchcodec (requer DLLs do FFmpeg
    full-shared). No Windows sem essas DLLs, troca o torchaudio.load
    por um loader via soundfile."""
    try:
        import torch
        import torchaudio

        def _load_with_soundfile(path, **kwargs):
            data, sr = sf.read(str(path), dtype="float32", always_2d=True)
            return torch.from_numpy(data.T).contiguous(), sr

        torchaudio.load = _load_with_soundfile
    except ImportError:
        pass


def get_chatterbox(device="cpu"):
    """Carrega o Chatterbox Multilingual V3 uma unica vez e retorna o motor.
    Clona a voz so com o audio de referencia (nao exige transcricao) e
    gera em 24000 Hz mono. Os pesos sao baixados da HuggingFace no 1o uso."""
    global CHATTERBOX_ENGINE
    if CHATTERBOX_ENGINE is None:
        print(f"  Carregando Chatterbox Multilingual V3 em {device} "
              f"(primeira vez baixa ~2GB da HuggingFace)...")
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        CHATTERBOX_ENGINE = ChatterboxMultilingualTTS.from_pretrained(
            device=device, t3_model=CHATTERBOX_T3_MODEL)
        print("  Chatterbox Multilingual V3 pronto.")
    return CHATTERBOX_ENGINE


def run_ffmpeg(args):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou: {r.stderr[-600:]}")
    return r


def run_ffmpeg_stream(args, stdin_bytes=None, stdout=False):
    """Executa ffmpeg com argumentos. Se `stdout` for True, le do stdout
    (util para encoders). `stdin_bytes` envia para o stdin e fecha."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args
    if stdin_bytes is not None:
        r = subprocess.run(cmd, input=stdin_bytes, capture_output=True, text=True)
    elif stdout:
        r = subprocess.run(cmd, capture_output=True, text=True)
    else:
        r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou: {r.stderr[-600:]}")
    return r


# ============================================================
# PARSER DE LEGENDAS (.srt)
# ============================================================

def parse_srt(path):
    """Le um .srt e devolve lista de {index, start, end, text} em segundos."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ts_re = re.compile(
        r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
    )

    def to_sec(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

    entries = []
    i, n = 0, len(lines)
    while i < n:
        m = ts_re.search(lines[i].strip())
        if m:
            start = to_sec(*(m.group(k) for k in range(1, 5)))
            end = to_sec(*(m.group(k) for k in range(5, 9)))
            i += 1
            parts = []
            while i < n:
                cur = lines[i].strip()
                if not cur or ts_re.search(cur) or re.fullmatch(r"\d+", cur):
                    break
                parts.append(cur)
                i += 1
            entries.append({"index": len(entries) + 1, "start": start,
                            "end": end, "text": " ".join(parts).strip()})
        else:
            i += 1

    out = []
    for e in entries:
        if e["end"] - e["start"] <= 0.01:
            continue
        low = e["text"].lower()
        if ("uniscribe" in low or "upgrade to remove" in low or
                "transcribed by" in low or "webvtt" in low):
            continue
        out.append(e)
    out.sort(key=lambda e: (e["start"], e["end"]))
    return out


# ============================================================
# MODO AUTOMATICO (sem .srt): transcreve + traduz
# ============================================================

def translate_text(text, target="pt", retries=3):
    """Traduz um trecho para `target` usando Google Translate (online).
    Valida o resultado (Google as vezes devolve pagina de erro 500) e faz
    retries com espera. Em ultimo caso devolve o texto original."""
    text = (text or "").strip()
    if not text:
        return text

    bad = re.compile(r"(error\s*500|that'?s\s*an\s*error|please\s*try\s*again"
                     r"\s*later|server\s*error|<!doctype\s*html|<html)", re.I)

    def ok(t):
        t = (t or "").strip()
        return bool(re.search(r"\w", t)) and not bad.search(t)

    for attempt in range(retries):
        try:
            from deep_translator import GoogleTranslator
            out = GoogleTranslator(source="auto",
                                   target=target).translate(text=text)
            if ok(out):
                return out.strip()
        except Exception:
            pass

        try:
            import json
            import urllib.parse
            import urllib.request
            url = ("https://translate.googleapis.com/translate_a/single"
                   f"?client=gtx&sl=auto&tl={urllib.parse.quote(target)}"
                   f"&dt=t&q={urllib.parse.quote(text)}")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.loads(r.read().decode("utf-8"))
            out = "".join(part[0] for part in data[0] if part and part[0])
            if ok(out):
                return out.strip()
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
    return text


def transcribe_entries(audio_path, model_size="small", device="cpu",
                       target_lang="pt", beam_size=None):
    """Transcreve a midia com faster-whisper (detecta o idioma sozinho),
    traduz cada fala para `target_lang` e devolve a lista de legendas no
    mesmo formato do parse_srt.

    `beam_size` adaptativo: 1 em CPU + tiny/base (3-5x mais rapido),
    3 em CPU + small+, 5 em CUDA. Aceita override explicito."""
    print(f"  Carregando Whisper ({model_size}, {device})...")
    from faster_whisper import WhisperModel
    compute = "float16" if device == "cuda" else "int8"
    if beam_size is None:
        if device == "cuda":
            beam_size = 5
        elif model_size in ("tiny", "base"):
            beam_size = 1
        else:
            beam_size = 3
    print(f"  Whisper config: beam_size={beam_size}, compute={compute}, "
          f"vad_filter=True")
    model = WhisperModel(model_size, device=device, compute_type=compute)
    print("  Transcrevendo (detecta idioma automaticamente)...")
    t0 = time.time()
    segments, info = model.transcribe(
        audio_path, language=None, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        beam_size=beam_size, best_of=beam_size,
        condition_on_previous_text=False)
    detected = info.language
    print(f"  Idioma detectado: {detected} "
          f"(probabilidade {float(info.language_probability):.2f})")
    entries = []
    last_log = 0.0
    for i, seg in enumerate(segments, 1):
        text = (seg.text or "").strip()
        if not text:
            continue
        translated = translate_text(text, target=target_lang)
        entries.append({"index": i, "start": float(seg.start),
                        "end": float(seg.end), "text": translated})
        now = time.time()
        if now - last_log > 1.5 or i % 10 == 0:
            elapsed = now - t0
            print(f"  [WHISPER] {i} segmentos transcritos "
                  f"em {elapsed:.1f}s (ultimo: {seg.start:.1f}-{seg.end:.1f}s)")
            last_log = now
        if i % 5 == 0:
            time.sleep(0.2)
    print(f"  Whisper concluido: {len(entries)} falas em "
          f"{time.time() - t0:.1f}s")
    return entries, detected


def write_srt(entries, path):
    """Salva as legendas geradas no modo automatico como .srt."""
    def fmt(t):
        ms = int(round((t % 1) * 1000))
        s = int(t)
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d},{ms:03d}"

    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(f"{e['index']}\n")
            f.write(f"{fmt(e['start'])} --> {fmt(e['end'])}\n")
            f.write(e["text"].strip() + "\n\n")
    return path


# ============================================================
# TEXTO -> SINTESE (CHATTERBOX)
# ============================================================

def split_text(text, max_chars=500):
    """Divide texto longo em trechos de ate ~max_chars (o Chatterbox gera
    ate 1000 tokens por chamada), quebrando sempre em fim de frase e, no
    caso de frases muito longas, no ultimo espaco dentro do limite para nao
    cortar palavras no meio."""
    sentences = re.split(r"(?<=[.!?…])\s+", text.strip())
    chunks, cur = [], ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if cur and len(cur) + 1 + len(s) <= max_chars:
            cur = cur + " " + s
        else:
            if cur:
                chunks.append(cur)
            while len(s) > max_chars:
                cut = s.rfind(" ", 0, max_chars)
                if cut < max_chars // 4:
                    cut = max_chars
                chunks.append(s[:cut].strip())
                s = s[cut:].strip()
            cur = s
    if cur:
        chunks.append(cur)
    return chunks


def _torch_seed(seed):
    if seed is not None:
        try:
            import torch
            torch.manual_seed(seed)
        except Exception:
            pass


def synthesize_chatterbox(engine, text, ref_wav, out_wav, language="pt",
                          temperature=0.8, seed=None):
    """Sintetiza o texto clonando a voz de ref_wav com Chatterbox Multilingual.
    Clona so com o audio de referencia (nao exige transcricao). O motor ja
    normaliza pontuacao internamente e embute marca d'agua (PerTh).

    seed: fixa a semente do torch antes de gerar, para a voz nao trocar de
    tom de uma fala para a outra."""
    _torch_seed(seed)
    gen_kwargs = {"language_id": language, "audio_prompt_path": ref_wav}
    if temperature is not None:
        gen_kwargs["temperature"] = temperature
    chunks = split_text(text)
    if len(chunks) == 1:
        wav = engine.generate(text=text, **gen_kwargs)
        data = np.asarray(wav.squeeze(0).detach().cpu().numpy(),
                          dtype=np.float32)
        sf.write(out_wav, data, SRC_SR)
    else:
        gap = np.zeros(int(0.18 * SRC_SR), dtype=np.float32)
        parts = []
        for i, chunk in enumerate(chunks):
            if not re.search(r"\w", chunk):
                continue
            wav = engine.generate(text=chunk, **gen_kwargs)
            data = np.asarray(wav.squeeze(0).detach().cpu().numpy(),
                              dtype=np.float32)
            parts.append(data)
            if i < len(chunks) - 1:
                parts.append(gap)
        if not parts:
            raise RuntimeError("texto so com pontuacao (sem palavra para falar)")
        sf.write(out_wav, np.concatenate(parts), SRC_SR)


def synthesize_text(engine, text, ref_wav, out_wav, language="pt",
                    temperature=None, seed=None, engine_name="chatterbox",
                    voice_idx=0):
    """Sintetiza o texto. engine_name='chatterbox' clona a voz de ref_wav
    com o Chatterbox; engine_name='edge' usa o Edge TTS (voz neural do
    idioma, leve, sem clonagem, requer internet). `voice_idx` alterna entre
    a voz feminina (par) e masculina (impar) no Edge."""
    if engine_name == "edge":
        synthesize_edge(text, out_wav, language, voice_idx)
        return
    temp = 0.8 if temperature is None else temperature
    synthesize_chatterbox(engine, text, ref_wav, out_wav, language,
                          temperature=temp, seed=seed)


def _edge_save(text, voice, path):
    """Gera o audio do texto com o Edge TTS e salva em `path` (mp3)."""
    import asyncio
    import edge_tts

    async def _go():
        c = edge_tts.Communicate(text, voice)
        await c.save(path)

    asyncio.run(_go())


def synthesize_edge(text, out_wav, language="pt", voice_idx=0):
    """Sintetiza o texto com Edge TTS. Cada idioma tem uma voz feminina e
    uma masculina; `voice_idx` par usa a feminina e impar a masculina.
    Textos longos sao divididos (como no Chatterbox) e unidos por ffmpeg,
    para nao estourar o limite por requisicao do servico."""
    import edge_tts
    voices = EDGE_VOICES.get(language.lower(), EDGE_VOICES["pt"])
    voice = voices[voice_idx % 2]
    chunks = split_text(text)
    if len(chunks) == 1:
        _edge_save(chunks[0], voice, out_wav)
        return
    parts_dir = os.path.dirname(os.path.abspath(out_wav))
    tmp_parts = []
    for i, chunk in enumerate(chunks):
        if not re.search(r"\w", chunk):
            continue
        p = os.path.join(parts_dir, f"_edge_{i}.mp3")
        _edge_save(chunk, voice, p)
        tmp_parts.append(p)
    if not tmp_parts:
        raise RuntimeError("texto so com pontuacao (sem palavra para falar)")
    lst = os.path.join(parts_dir, "_edge_list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in tmp_parts:
            f.write(f"file '{p}'\n")
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", lst,
                "-c:a", "libmp3lame", "-q:a", "4", out_wav])
    for p in tmp_parts:
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        os.remove(lst)
    except OSError:
        pass


# ============================================================
# CONVERSAO / MIXAGEM
# ============================================================

def extract_reference(audio_path, start, end, out_path):
    """Recorta o trecho [start, end] do audio original como referencia
    de voz (mono, 24000 Hz). Usa seek por entrada para ser rapido."""
    run_ffmpeg(["-ss", f"{start:.3f}", "-i", audio_path,
                "-t", f"{max(end - start, 0.1):.3f}",
                "-ac", "1", "-ar", str(SRC_SR), "-acodec", "pcm_s16le", out_path])


def convert_to_track_format(synth_wav, conv_wav, channels):
    """Converte a sintese (24000 mono) para a taxa/canais do original.
    Se o WAV ja esta no formato alvo, reaproveita o arquivo (sem ffmpeg)."""
    try:
        info = sf.info(synth_wav)
    except Exception:
        info = None
    if (info is not None and info.samplerate == TARGET_SR
            and info.channels == channels
            and info.subtype == "PCM_16"):
        if os.path.abspath(synth_wav) != os.path.abspath(conv_wav):
            try:
                os.replace(synth_wav, conv_wav)
            except OSError:
                shutil.copy2(synth_wav, conv_wav)
                try:
                    os.remove(synth_wav)
                except OSError:
                    pass
        return conv_wav
    run_ffmpeg(["-i", synth_wav, "-ar", str(TARGET_SR), "-ac", str(channels),
                "-acodec", "pcm_s16le", conv_wav])


def fit_to_duration(conv_wav, fitted_wav, seg_dur, channels, max_tempo=2.0):
    """Estica/encolhe a trilha (rubberband) para ter a MESMA duracao da
    fala original da legenda e depois corta/preenche para a duracao exata.
    Trava o fator entre 0.5x e max_tempo para nao distorcer demais.
    Quando nao precisa esticar, reaproveita o conv_wav (ja em TARGET_SR)
    sem chamar ffmpeg de novo, so ajusta o comprimento."""
    data, sr = sf.read(conv_wav, dtype="float32", always_2d=True)
    cur = len(data) / sr
    target = max(seg_dur, 0.15)
    if cur <= 0.01:
        return conv_wav
    tempo = min(max(cur / target, 0.5), max_tempo)
    if abs(tempo - 1.0) < 0.03:
        n = int(round(target * sr))
        if len(data) > n:
            data = data[:n]
        elif len(data) < n:
            data = np.pad(data, ((0, n - len(data)), (0, 0)))
        sf.write(fitted_wav, data, sr)
        return fitted_wav
    run_ffmpeg(["-i", conv_wav, "-af", f"rubberband=tempo={tempo:.4f}",
                "-ar", str(TARGET_SR), "-ac", str(channels),
                "-acodec", "pcm_s16le", fitted_wav])
    data, sr = sf.read(fitted_wav, dtype="float32", always_2d=True)
    n = int(round(target * sr))
    if len(data) > n:
        data = data[:n]
    elif len(data) < n:
        data = np.pad(data, ((0, n - len(data)), (0, 0)))
    sf.write(fitted_wav, data, sr)
    return fitted_wav


def load_track(audio_path, work_dir, memmap=None):
    """Converte o audio original para WAV pcm_s16le (taxa padrao,
    mantendo canais) e devolve (numpy float32, sr). Se `memmap` for True,
    retorna um array espelhado em disco (np.memmap) no lugar de carregar
    tudo em RAM - util para videos/filmes longos."""
    orig_wav = os.path.join(work_dir, "orig.wav")
    run_ffmpeg(["-i", audio_path, "-ar", str(TARGET_SR),
                "-acodec", "pcm_s16le", orig_wav])
    data, sr = sf.read(orig_wav, dtype="float32", always_2d=True)
    if memmap and data.nbytes > 256 * 1024 * 1024:
        fpath = os.path.join(work_dir, "track.npy")
        mm = np.memmap(fpath, dtype="float32", mode="w+", shape=data.shape)
        mm[:] = data[:]
        mm.flush()
        return mm, sr
    return data, sr


def place_segment(track, start, synth_data, volume, fade_ms=FADE_MS):
    """Soma a sintese na linha do tempo a partir do inicio da legenda,
    com crossfade curto nas bordas para evitar estalos/cortes secos."""
    start_idx = int(round(start * TARGET_SR))
    n = len(synth_data)
    if start_idx >= len(track) or n <= 0:
        return
    data = synth_data[:min(n, len(track) - start_idx)]
    f = max(int(round(fade_ms / 1000.0 * TARGET_SR)), 1)
    if len(data) > 2 * f:
        data = data.copy()
        data[:f] *= np.linspace(0.0, 1.0, f, dtype=np.float32)[:, None]
        data[-f:] *= np.linspace(1.0, 0.0, f, dtype=np.float32)[:, None]
    elif len(data) > 1:
        data = data.copy()
        half = max(len(data) // 2, 1)
        data[:half] *= np.linspace(0.0, 1.0, half, dtype=np.float32)[:, None]
        data[half:] *= np.linspace(1.0, 0.0, len(data) - half, dtype=np.float32)[:, None]
    track[start_idx:start_idx + len(data)] += data * volume


def build_silence_multiplier(entries, n_samples, sr, fade_ms=FADE_MS, memmap_path=None):
    """Multiplicador para silenciar no audio ORIGINAL apenas as regioes
    que serao dubladas. Intervalos sobrepostos/colados sao fundidos e as
    bordas recebem crossfade. 1.0 = mantem o original, 0.0 = silencio.
    Se `memmap_path` for dado, o multiplicador e alocado em disco (util
    para arquivos muito longos, evitando duplicar a RAM)."""
    if not entries:
        return np.ones(n_samples, dtype=np.float32)
    iv = sorted((max(e["start"], 0.0), e["end"]) for e in entries)
    merged = []
    for s, e in iv:
        if merged and s <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    if memmap_path is not None:
        m = np.memmap(memmap_path, dtype="float32", mode="w+", shape=(n_samples,))
        m[:] = 1.0
    else:
        m = np.ones(n_samples, dtype=np.float32)
    f = max(int(round(fade_ms / 1000.0 * sr)), 1)
    for s, e in merged:
        si = min(int(round(s * sr)), n_samples - 1)
        ei = min(int(round(e * sr)), n_samples)
        if ei - si <= 2 * f + 2:
            m[si:ei] = 0.0
            continue
        m[si + f:ei - f] = 0.0
        m[si:si + f] *= np.linspace(1.0, 0.0, f, dtype=np.float32)
        m[ei - f:ei] *= np.linspace(0.0, 1.0, f, dtype=np.float32)
    if memmap_path is not None:
        m.flush()
    return m


def cleanup_workdir(work_dir, keep_parts, emit_paths):
    """Remove os arquivos intermediarios da pasta de trabalho ao finalizar
    com sucesso (mantem as amostras quando --emit-paths ou --keep-parts)."""
    if not os.path.isdir(work_dir):
        return
    for name in ("orig.wav", "final.wav", "track.npy", "silence.npy"):
        p = os.path.join(work_dir, name)
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    parts_dir = os.path.join(work_dir, "parts")
    if (not keep_parts) and (not emit_paths) and os.path.isdir(parts_dir):
        shutil.rmtree(parts_dir, ignore_errors=True)
    try:
        if not os.listdir(work_dir):
            os.rmdir(work_dir)
    except OSError:
        pass


# ============================================================
# MAIN
# ============================================================

def main():
    force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="Dublador - dubla audio (ou video mantendo a imagem) com "
                    "clonagem de voz offline (motor: Chatterbox Multilingual V3)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemplo:\n"
            "  python dublar.py --audio audio_para_dublar\\audio.mp3 "
            "--srt audio_para_dublar\\audio.srt\n"
            "  python dublar.py --audio filme.mp4 --srt filme.srt\n"
            "  python dublar.py --audio a.mp3\n"
            "    (sem --srt: detecta o idioma, transcreve com Whisper e\n"
            "     traduz para o idioma de saida, gerando as legendas)\n"
        ),
    )
    ap.add_argument("--audio", required=True,
                    help="Audio ou video no idioma original (mp3/wav/mp4/mkv/mov/avi/webm...)")
    ap.add_argument("--srt", default=None,
                    help="Legenda da traducao (.srt). Omita para o modo "
                         "automatico: detecta o idioma da midia, transcreve "
                         "com Whisper e traduz para o idioma de saida")
    ap.add_argument("--whisper-model", default="small",
                    help="Modelo do Whisper para o modo automatico "
                         "(tiny/base/small/medium/large-v3; padrao: small)")
    ap.add_argument("--whisper-beam", type=int, default=None,
                    help="beam_size do Whisper (padrao adaptativo: 1 p/ "
                         "tiny/base em CPU, 3 p/ small+ em CPU, 5 em CUDA)")
    ap.add_argument("--engine", default="chatterbox",
                    help="Motor de dublagem: chatterbox (clonagem de voz, "
                         "offline, padrao) ou edge (Edge TTS, leve, online, "
                         "sem clonagem)")
    ap.add_argument("--gen-srt", action="store_true",
                    help="No modo automatico, salva o .srt gerado ao lado do audio")
    ap.add_argument("--out", default=None,
                    help="Saida. Padrao: <audio>_dublado.mp3 (ou .mp4 quando o "
                         "arquivo de entrada e video)")
    ap.add_argument("--device", default=None, help="cuda ou cpu (padrao: cuda se disponivel)")
    ap.add_argument("--language", default="pt", help="Idioma da fala (padrao: pt)")
    ap.add_argument("--temperature", type=float, default=None,
                    help="Aleatoriedade da fala (menor = voz mais consistente e "
                         "menos sussurro; maior = mais expressivo). Padrao: 0.8")
    ap.add_argument("--volume", type=float, default=1.0, help="Ganho da voz dublada (padrao: 1.0)")
    ap.add_argument("--max-tempo", type=float, default=2.0,
                    help="Limite do esticamento (rubberband) da voz dublada. "
                         "Se a voz sintetizada for muito mais longa que a fala "
                         "original, e cortada aqui e o restante vira silencio "
                         "(padrao: 2.0).")
    ap.add_argument("--seed", type=int, default=None,
                    help="Semente base para reprodutibilidade. Padrao: 1000 "
                         "(cada legenda usa seed + indice)")
    ap.add_argument("--parallel", type=int, default=1,
                    help="Numero de sintetizadores em paralelo (Chatterbox). "
                         "1 = sequencial (padrao). 2-4 = mais rapido em PC "
                         "forte com CUDA; aumenta uso de VRAM/RAM.")
    ap.add_argument("--dry-run", action="store_true", help="So lista as legendas e sai")
    ap.add_argument("--workdir", default=None, help="Pasta de trabalho (padrao: .dub_<nome> ao lado da saida)")
    ap.add_argument("--keep-parts", action="store_true", help="Nao apaga os arquivos intermediarios")
    ap.add_argument("--emit-paths", action="store_true",
                    help="Imprime [SEG] <idx> <path> a cada amostra gerada e mantem "
                         "o arquivo (para ouvir pelo menu grafico)")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        sys.exit("[ERRO] ffmpeg nao encontrado no PATH. "
                 "Instale o ffmpeg e adicione-o ao PATH.")

    if not os.path.exists(args.audio):
        sys.exit(f"[ERRO] Audio/Video nao encontrado: {args.audio}")
    if args.srt and not os.path.exists(args.srt):
        sys.exit(f"[ERRO] SRT nao encontrado: {args.srt}")

    auto_mode = not args.srt

    if args.device is None:
        try:
            import torch
            args.device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            args.device = "cpu"

    if auto_mode:
        print("=" * 60)
        print("  Modo automatico (sem .srt):")
        print("    1. detecta o idioma da midia (Whisper)")
        print("    2. transcreve cada fala com timestamps")
        print("    3. traduz para '" + args.language + "' (Google Translate)")
        print("    4. dubla as legendas geradas")
        print("=" * 60)
        print("\n[0/4] Transcrevendo e traduzindo...")
        entries, detected_lang = transcribe_entries(
            args.audio, args.whisper_model, args.device, args.language,
            beam_size=args.whisper_beam)
        if not entries:
            sys.exit("[ERRO] Nenhuma fala detectada no audio.")
        if args.gen_srt:
            base = os.path.splitext(os.path.basename(args.audio))[0]
            srt_path = os.path.join(os.path.dirname(os.path.abspath(args.audio)),
                                    base + "_traducao.srt")
            write_srt(entries, srt_path)
            print(f"  [SRT] Traducao salva em: {srt_path}")
    else:
        entries = parse_srt(args.srt)
        detected_lang = None

    if not entries:
        sys.exit("[ERRO] Nenhuma legenda valida encontrada no SRT.")

    if args.dry_run:
        print(f"[PLAN] {len(entries)} legendas "
              f"{'geradas automaticamente' if auto_mode else f'em {args.srt}'}")
        for e in entries:
            print(f"  [{e['index']:3d}] {e['start']:8.3f} -> {e['end']:8.3f} "
                  f"({e['end'] - e['start']:6.2f}s)  {e['text'][:70]}")
        return

    os.environ["COQUI_TOS_AGREED"] = "1"
    os.environ["TQDM_DISABLE"] = "1"
    patch_torchaudio_soundfile()

    try:
        if args.engine == "edge":
            if args.language.lower() not in EDGE_VOICES:
                sys.exit(f"[ERRO] Idioma '{args.language}' nao suportado pelo "
                         f"Edge TTS. Disponiveis: {', '.join(sorted(EDGE_VOICES))}")
        else:
            from chatterbox.mtl_tts import SUPPORTED_LANGUAGES
            if args.language.lower() not in SUPPORTED_LANGUAGES:
                sys.exit(f"[ERRO] Idioma '{args.language}' nao suportado pelo "
                         f"Chatterbox. Disponiveis: {', '.join(sorted(SUPPORTED_LANGUAGES))}")
    except ImportError:
        pass

    is_video = has_video_stream(args.audio)
    base = os.path.splitext(os.path.basename(args.audio))[0]
    default_ext = ".mp4" if is_video else ".mp3"
    out_path = args.out or os.path.join(os.path.dirname(os.path.abspath(args.audio)),
                                        base + "_dublado" + default_ext)
    out_dir = os.path.dirname(os.path.abspath(out_path))
    work_dir = args.workdir or os.path.join(out_dir, f".dub_{base}")
    parts_dir = os.path.join(work_dir, "parts")
    os.makedirs(parts_dir, exist_ok=True)

    print("=" * 60)
    print("  Dublador v2 (dublagem simples)")
    print(f"  Audio: {args.audio}{'  [VIDEO]' if is_video else ''}")
    if auto_mode:
        print(f"  Auto:  idioma '{detected_lang}' transcrito e traduzido "
              f"para '{args.language}' ({len(entries)} legendas)")
    else:
        print(f"  SRT:   {args.srt} ({len(entries)} legendas)")
    print(f"  Saida: {out_path}")
    print(f"  Motor: {'Edge TTS (leve)' if args.engine == 'edge' else 'Chatterbox Multilingual V3'}   "
          f"Idio: {args.language}   "
          f"Device: {args.device if args.engine != 'edge' else 'n/a'}")
    print("  Voz dublada esticada para a mesma duracao da fala original")
    print("=" * 60)

    print("\n[1/4] Preparando audio...")
    track, sr = load_track(args.audio, work_dir, memmap=True)
    using_mem = os.path.exists(os.path.join(work_dir, "track.npy"))
    channels = track.shape[1]
    print(f"  duracao: {len(track) / sr:.1f}s | canais: {channels} | {TARGET_SR} Hz"
          + (" | em disco (memmap)" if using_mem else ""))

    dub_entries = [e for e in entries if re.search(r"\w", e["text"])]
    if not dub_entries:
        sys.exit("[ERRO] Nenhuma legenda com texto para dublar no SRT.")
    sil_path = os.path.join(work_dir, "silence.npy") if using_mem else None
    silence = build_silence_multiplier(dub_entries, len(track), TARGET_SR,
                                       memmap_path=sil_path)
    track *= silence[:, None]

    is_edge = args.engine == "edge"
    print("\n[2/4] Carregando motor"
          + (" (Edge TTS, leve)..." if is_edge else " (chatterbox)..."))
    engine = None
    if not is_edge:
        engine = get_chatterbox(args.device)

    print(f"\n[3/4] Dublagem de {len(entries)} legendas...")
    done = 0
    skipped = 0
    dub_total = len(dub_entries)

    if is_edge:
        # Fase A: sintetiza TODAS as legendas em paralelo (Edge TTS e
        # limitado pela rede, nao pela CPU) e depois mixa em sequencia.
        jobs = []
        for pos, e in enumerate(entries):
            if not re.search(r"\w", e["text"]):
                skipped += 1
                continue
            seg_id = f"seg_{e['index']:03d}"
            synth_wav = os.path.join(parts_dir, seg_id + ".wav")
            jobs.append((e, synth_wav))
        print(f"  Sintetizando {len(jobs)} falas com Edge TTS "
              f"(vozes feminina/masculina alternadas, em paralelo)...")
        import concurrent.futures as cf
        max_workers = min(8, max(2, os.cpu_count() or 4))
        with cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
            def _edge_job(e, path):
                synthesize_text(None, e["text"], None, path,
                                args.language, engine_name="edge",
                                voice_idx=e["index"])
                return e, path
            futs = [pool.submit(_edge_job, e, path) for e, path in jobs]
            for i, fut in enumerate(cf.as_completed(futs), 1):
                e, path = fut.result()
                print(f"  [SYN {i:3d}/{len(jobs)}] {e['start']:8.3f}-{e['end']:8.3f}"
                      f"  {e['text'][:70]}")
        print(f"  Sintese concluida. Mixando {len(jobs)} falas na trilha...")
        for pos, (e, synth_wav) in enumerate(jobs):
            i_disp = pos + 1
            n_tot = len(jobs)
            start, end = e["start"], e["end"]
            conv_wav = synth_wav[:-4] + ".conv.wav"
            fitted_wav = synth_wav[:-4] + ".fitted.wav"
            try:
                convert_to_track_format(synth_wav, conv_wav, channels)
                final_seg = fit_to_duration(conv_wav, fitted_wav, end - start,
                                            channels, max_tempo=args.max_tempo)
                data, _ = sf.read(final_seg, dtype="float32", always_2d=True)
                limit = int(round((end - start) * sr))
                if len(data) > limit:
                    data = data[:limit]
                place_segment(track, start, data, args.volume)
                done += 1
                print(f"[PROGRESS] {done}/{dub_total}")
                if args.emit_paths:
                    print(f"[SEG] {e['index']}\t{os.path.abspath(final_seg)}"
                          f"\t{start:.3f}\t{end:.3f}\t{e['text'][:70]}")
                if not args.keep_parts:
                    for p in (synth_wav, conv_wav, fitted_wav):
                        if args.emit_paths and p == final_seg:
                            continue
                        if os.path.exists(p):
                            os.remove(p)
            except Exception as ex:
                print(f"  [DUB {i_disp:3d}/{n_tot}] [ERRO] {ex}")
    else:
        import concurrent.futures as cf
        n_parallel = max(1, int(getattr(args, "parallel", 1) or 1))
        is_cuda = (args.device == "cuda")
        if n_parallel > 1 and not is_cuda:
            print(f"  Paralelismo={n_parallel} em CPU; pode competir por nucleos. "
                  f"Use 2-4 apenas se sobrar nucleos livres.")
        print(f"  Modo: {'paralelo (' + str(n_parallel) + ' workers)' if n_parallel > 1 else 'sequencial'}")

        def _dub_one(e, ref_wav, synth_wav, conv_wav, fitted_wav):
            seg_start, seg_end = e["start"], e["end"]
            try:
                extract_reference(args.audio, seg_start, seg_end, ref_wav)
                seed = args.seed + e["index"] if args.seed is not None else 1000 + e["index"]
                synthesize_text(engine, e["text"], ref_wav, synth_wav,
                                args.language, temperature=args.temperature, seed=seed)
                convert_to_track_format(synth_wav, conv_wav, channels)
                final_seg = fit_to_duration(conv_wav, fitted_wav,
                                            seg_end - seg_start, channels,
                                            max_tempo=args.max_tempo)
                data, _ = sf.read(final_seg, dtype="float32", always_2d=True)
                limit = int(round((seg_end - seg_start) * sr))
                if len(data) > limit:
                    data = data[:limit]
                return (e, final_seg, data, None)
            except Exception as ex:
                return (e, None, None, str(ex))

        pending = []
        for pos, e in enumerate(entries):
            i_disp = pos + 1
            n_tot = len(entries)
            text = e["text"]
            if not re.search(r"\w", text):
                skipped += 1
                print(f"  [DUB {i_disp:3d}/{n_tot}] (ignorada: sem texto)")
                continue
            seg_id = f"seg_{e['index']:03d}"
            ref_wav = os.path.join(parts_dir, seg_id + ".ref.wav")
            synth_wav = os.path.join(parts_dir, seg_id + ".wav")
            conv_wav = os.path.join(parts_dir, seg_id + ".conv.wav")
            fitted_wav = os.path.join(parts_dir, seg_id + ".fitted.wav")
            pending.append((e, ref_wav, synth_wav, conv_wav, fitted_wav))

        if n_parallel <= 1 or len(pending) <= 1:
            for tup in pending:
                e, ref_wav, synth_wav, conv_wav, fitted_wav = tup
                i_disp = entries.index(e) + 1 if e in entries else 0
                print(f"  [DUB {i_disp:3d}/{len(entries)}] {e['start']:8.3f}-{e['end']:8.3f}"
                      f"  {e['text'][:70]}")
                e, final_seg, data, err = _dub_one(e, ref_wav, synth_wav, conv_wav, fitted_wav)
                if err is not None:
                    print(f"  [DUB {i_disp:3d}/{len(entries)}] [ERRO] {err}")
                    continue
                start, end = e["start"], e["end"]
                place_segment(track, start, data, args.volume)
                done += 1
                print(f"[PROGRESS] {done}/{dub_total}")
                if args.emit_paths:
                    print(f"[SEG] {e['index']}\t{os.path.abspath(final_seg)}"
                          f"\t{start:.3f}\t{end:.3f}\t{e['text'][:70]}")
                if not args.keep_parts:
                    for p in (synth_wav, conv_wav, fitted_wav, ref_wav):
                        if args.emit_paths and p == final_seg:
                            continue
                        if os.path.exists(p):
                            try:
                                os.remove(p)
                            except OSError:
                                pass
        else:
            ctx = cf.ThreadPoolExecutor(max_workers=n_parallel)
            fut_map = {}
            for tup in pending:
                e, ref_wav, synth_wav, conv_wav, fitted_wav = tup
                fut = ctx.submit(_dub_one, e, ref_wav, synth_wav, conv_wav, fitted_wav)
                fut_map[fut] = (e, ref_wav, synth_wav, conv_wav, fitted_wav)
            for fut in cf.as_completed(fut_map):
                e, ref_wav, synth_wav, conv_wav, fitted_wav = fut_map[fut]
                i_disp = e["index"]
                try:
                    _e, final_seg, data, err = fut.result()
                except Exception as ex:
                    err = str(ex)
                    final_seg = None
                    data = None
                if err is not None or data is None:
                    print(f"  [DUB {i_disp:3d}/{len(entries)}] [ERRO] {err}")
                    continue
                start, end = e["start"], e["end"]
                place_segment(track, start, data, args.volume)
                done += 1
                print(f"[PROGRESS] {done}/{dub_total}")
                if args.emit_paths:
                    print(f"[SEG] {e['index']}\t{os.path.abspath(final_seg)}"
                          f"\t{start:.3f}\t{end:.3f}\t{e['text'][:70]}")
                if not args.keep_parts:
                    for p in (synth_wav, conv_wav, fitted_wav, ref_wav):
                        if args.emit_paths and p == final_seg:
                            continue
                        if os.path.exists(p):
                            try:
                                os.remove(p)
                            except OSError:
                                pass
            ctx.shutdown(wait=True)

    print(f"\n[4/4] Finalizando ({done}/{len(entries)} legendas dubladas)...")
    if skipped:
        print(f"  {skipped} legendas ignoradas (sem texto).")
    final_wav = os.path.join(work_dir, "final.wav")
    if using_mem:
        track.flush()
    peak = float(np.max(np.abs(track))) if len(track) else 0.0
    if peak > 1.0:
        track *= (0.98 / peak)
    np.clip(track, -1.0, 1.0, out=track)
    sf.write(final_wav, track, TARGET_SR)
    is_video_out = is_video and out_path.lower().endswith((".mp4", ".mkv", ".mov", ".avi", ".webm"))
    if is_video_out:
        run_ffmpeg(["-i", args.audio, "-i", final_wav,
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", out_path])
    elif out_path.lower().endswith(".wav"):
        try:
            os.replace(final_wav, out_path)
        except OSError:
            shutil.move(final_wav, out_path)
    else:
        run_ffmpeg(["-i", final_wav, "-q:a", "2", out_path])
    cleanup_workdir(work_dir, args.keep_parts, args.emit_paths)
    print(f"[OK] {'Video' if is_video_out else 'Audio'} dublado em: {out_path}")


if __name__ == "__main__":
    main()
