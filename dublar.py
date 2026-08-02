#!/usr/bin/env python3
"""
Dublador - Dublagem com clonagem de voz offline
===============================================
Dubla um arquivo de audio usando o texto de um .srt (traducao) e
clonando a voz do audio ORIGINAL, 100% offline.

Para cada legenda, o texto do .srt e falado no idioma escolhido clonando
a voz do proprio trecho original (referencia por fala). A fala original
da legenda e silenciada e a voz dublada entra no lugar, esticada
(rubberband) para ter a MESMA duracao da fala original.

Motor de voz:
    chatterbox (unico) - Chatterbox Multilingual V3 (Resemble AI), MIT,
        23+ idiomas (inclui pt-br), clonagem zero-shot so com o audio de
        referencia (nao exige transcricao), ~0.5B.

Uso:
    python dublar.py --audio "audio_para_dublar\\audio.mp3" --srt "audio_para_dublar\\audio.srt"
    python dublar.py --audio a.mp3 --srt a.srt --out a_dublado.mp3 --device cuda --volume 1.2

Requisitos:
    pip install chatterbox-tts soundfile numpy customtkinter psutil
    ffmpeg no PATH
"""

import os
import re
import sys
import argparse
import shutil
import subprocess

import numpy as np
import soundfile as sf

SRC_SR = 24000          # amostragem da saida do Chatterbox
TARGET_SR = 44100       # amostragem final
CHATTERBOX_ENGINE = None
CHATTERBOX_T3_MODEL = "v3"   # Chatterbox Multilingual V3


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
    cmd = ["ffmpeg", "-y"] + args
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou: {r.stderr[-600:]}")
    return r


# ============================================================
# PARSER DE LEGENDAS (.srt)
# ============================================================

def parse_srt(path):
    """Le um .srt e devolve lista de {index, start, end, text} em segundos."""
    with open(path, "r", encoding="utf-8-sig") as f:
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
    return out


# ============================================================
# TEXTO -> SINTESE (CHATTERBOX)
# ============================================================

def split_text(text, max_chars=500):
    """Divide texto longo em trechos de ate ~max_chars (o Chatterbox gera
    ate 1000 tokens por chamada), quebrando sempre em fim de frase."""
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
                chunks.append(s[:max_chars].rstrip())
                s = s[max_chars:].strip()
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
            if not re.search(r"[a-zA-Z0-9]", chunk):
                continue
            wav = engine.generate(text=chunk, **gen_kwargs)
            data = np.asarray(wav.squeeze(0).detach().cpu().numpy(),
                              dtype=np.float32)
            parts.append(data)
            if i < len(chunks) - 1:
                parts.append(gap)
        if parts:
            sf.write(out_wav, np.concatenate(parts), SRC_SR)


def synthesize_text(engine, text, ref_wav, out_wav, language="pt",
                    temperature=None, seed=None):
    """Sintetiza o texto clonando a voz de ref_wav com Chatterbox."""
    temp = 0.8 if temperature is None else temperature
    synthesize_chatterbox(engine, text, ref_wav, out_wav, language,
                          temperature=temp, seed=seed)


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
    """Converte a sintese (24000 mono) para a taxa/canais do original."""
    run_ffmpeg(["-i", synth_wav, "-ar", str(TARGET_SR), "-ac", str(channels),
                "-acodec", "pcm_s16le", conv_wav])


def fit_to_duration(conv_wav, fitted_wav, seg_dur, channels):
    """Estica/encolhe a sintese (rubberband) para ter a MESMA duracao da
    fala original da legenda e depois corta/preenche para a duracao exata.
    Trava o fator entre 0.5x e 2.0x para nao distorcer demais."""
    data, sr = sf.read(conv_wav, dtype="float32", always_2d=True)
    cur = len(data) / sr
    target = max(seg_dur, 0.15)
    if cur <= 0.01:
        return conv_wav
    tempo = min(max(cur / target, 0.5), 2.0)
    if abs(tempo - 1.0) < 0.03:
        run_ffmpeg(["-i", conv_wav, "-ar", str(TARGET_SR), "-ac", str(channels),
                    "-acodec", "pcm_s16le", fitted_wav])
    else:
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


def load_track(audio_path, work_dir):
    """Converte o audio original para WAV pcm_s16le (taxa padrao,
    mantendo canais) e devolve (numpy float32, sr)."""
    orig_wav = os.path.join(work_dir, "orig.wav")
    run_ffmpeg(["-i", audio_path, "-ar", str(TARGET_SR),
                "-acodec", "pcm_s16le", orig_wav])
    data, sr = sf.read(orig_wav, dtype="float32", always_2d=True)
    return data, sr


def place_segment(track, start, synth_data, volume):
    """Soma a sintese na linha do tempo a partir do inicio da legenda."""
    start_idx = int(round(start * TARGET_SR))
    n = len(synth_data)
    if start_idx >= len(track):
        return
    end_idx = min(start_idx + n, len(track))
    track[start_idx:end_idx] += synth_data[:end_idx - start_idx] * volume


# ============================================================
# MAIN
# ============================================================

def main():
    ap = argparse.ArgumentParser(
        description="Dublador - dubla audio com clonagem de voz offline "
                    "(motor: Chatterbox Multilingual V3)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemplo:\n"
            "  python dublar.py --audio audio_para_dublar\\audio.mp3 "
            "--srt audio_para_dublar\\audio.srt\n"
        ),
    )
    ap.add_argument("--audio", required=True, help="Audio no idioma original (mp3/wav...)")
    ap.add_argument("--srt", required=True, help="Legenda da traducao (.srt)")
    ap.add_argument("--out", default=None, help="Saida (.mp3 ou .wav). Padrao: <audio>_dublado.mp3")
    ap.add_argument("--device", default=None, help="cuda ou cpu (padrao: cuda se disponivel)")
    ap.add_argument("--language", default="pt", help="Idioma da fala (padrao: pt)")
    ap.add_argument("--temperature", type=float, default=None,
                    help="Aleatoriedade da fala (menor = voz mais consistente e "
                         "menos sussurro; maior = mais expressivo). Padrao: 0.8")
    ap.add_argument("--volume", type=float, default=1.0, help="Ganho da voz dublada (padrao: 1.0)")
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
        sys.exit(f"[ERRO] Audio nao encontrado: {args.audio}")
    if not os.path.exists(args.srt):
        sys.exit(f"[ERRO] SRT nao encontrado: {args.srt}")

    entries = parse_srt(args.srt)
    if not entries:
        sys.exit("[ERRO] Nenhuma legenda valida encontrada no SRT.")

    if args.dry_run:
        print(f"[PLAN] {len(entries)} legendas em {args.srt}")
        for e in entries:
            print(f"  [{e['index']:3d}] {e['start']:8.3f} -> {e['end']:8.3f} "
                  f"({e['end'] - e['start']:6.2f}s)  {e['text'][:70]}")
        return

    if args.device is None:
        try:
            import torch
            args.device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            args.device = "cpu"

    os.environ["COQUI_TOS_AGREED"] = "1"
    os.environ["TQDM_DISABLE"] = "1"
    patch_torchaudio_soundfile()

    try:
        from chatterbox.mtl_tts import SUPPORTED_LANGUAGES
        if args.language.lower() not in SUPPORTED_LANGUAGES:
            sys.exit(f"[ERRO] Idioma '{args.language}' nao suportado pelo "
                     f"Chatterbox. Disponiveis: {', '.join(sorted(SUPPORTED_LANGUAGES))}")
    except ImportError:
        pass

    base = os.path.splitext(os.path.basename(args.audio))[0]
    out_path = args.out or os.path.join(os.path.dirname(os.path.abspath(args.audio)),
                                        base + "_dublado.mp3")
    out_dir = os.path.dirname(os.path.abspath(out_path))
    work_dir = args.workdir or os.path.join(out_dir, f".dub_{base}")
    parts_dir = os.path.join(work_dir, "parts")
    os.makedirs(parts_dir, exist_ok=True)

    print("=" * 60)
    print("  Dublador v2 (dublagem simples)")
    print(f"  Audio: {args.audio}")
    print(f"  SRT:   {args.srt} ({len(entries)} legendas)")
    print(f"  Saida: {out_path}")
    print(f"  Motor: Chatterbox Multilingual V3   Idio: {args.language}   "
          f"Device: {args.device}")
    print("  Voz dublada esticada para a mesma duracao da fala original")
    print("=" * 60)

    print("\n[1/4] Preparando audio...")
    track, sr = load_track(args.audio, work_dir)
    channels = track.shape[1]
    print(f"  duracao: {len(track) / sr:.1f}s | canais: {channels} | {TARGET_SR} Hz")

    print("\n[2/4] Carregando motor (chatterbox)...")
    engine = get_chatterbox(args.device)

    print(f"\n[3/4] Dublagem de {len(entries)} legendas...")
    done = 0
    skipped = 0
    for e in entries:
        start, end, text = e["start"], e["end"], e["text"]
        if not re.search(r"[a-zA-Z0-9]", text):
            skipped += 1
            continue
        seg_id = f"seg_{e['index']:03d}"
        ref_wav = os.path.join(parts_dir, seg_id + ".ref.wav")
        synth_wav = os.path.join(parts_dir, seg_id + ".wav")
        conv_wav = os.path.join(parts_dir, seg_id + ".conv.wav")
        fitted_wav = os.path.join(parts_dir, seg_id + ".fitted.wav")
        print(f"  [{e['index']:3d}/{len(entries)}] {start:8.3f}-{end:8.3f}  {text[:70]}")
        try:
            extract_reference(args.audio, start, end, ref_wav)
            synthesize_text(engine, text, ref_wav, synth_wav,
                            args.language, temperature=args.temperature,
                            seed=1000 + e["index"])
            convert_to_track_format(synth_wav, conv_wav, channels)
            final_seg = fit_to_duration(conv_wav, fitted_wav, end - start, channels)
            data, _ = sf.read(final_seg, dtype="float32", always_2d=True)
            limit = int(round((end - start) * sr))
            if len(data) > limit:
                data = data[:limit]
            si, ei = int(round(start * sr)), min(int(round(start * sr)) + len(data),
                                                 len(track))
            if si < len(track):
                track[si:ei] = 0.0
            place_segment(track, start, data, args.volume)
            done += 1
            if args.emit_paths:
                print(f"[SEG] {e['index']}\t{os.path.abspath(final_seg)}\t{text[:70]}")
            if not args.keep_parts:
                for p in (synth_wav, conv_wav, fitted_wav, ref_wav):
                    if args.emit_paths and p == final_seg:
                        continue
                    if os.path.exists(p):
                        os.remove(p)
        except Exception as ex:
            print(f"    [ERRO] {ex}")

    print(f"\n[4/4] Finalizando ({done}/{len(entries)} legendas dubladas)...")
    if skipped:
        print(f"  {skipped} legendas ignoradas (sem texto).")
    final_wav = os.path.join(work_dir, "final.wav")
    np.clip(track, -1.0, 1.0, out=track)
    sf.write(final_wav, track, TARGET_SR)
    if out_path.lower().endswith(".wav"):
        try:
            os.replace(final_wav, out_path)
        except OSError:
            shutil.move(final_wav, out_path)
    else:
        run_ffmpeg(["-i", final_wav, "-q:a", "2", out_path])
    print(f"[OK] Audio dublado em: {out_path}")


if __name__ == "__main__":
    main()
