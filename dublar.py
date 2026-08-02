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
    python dublar.py --audio filme.mp4 --srt filme.srt        # video mantem a imagem

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
FADE_MS = 15            # crossfade nas bordas de cada legenda
CHATTERBOX_ENGINE = None
CHATTERBOX_T3_MODEL = "v3"   # Chatterbox Multilingual V3


def force_utf8_stdout():
    """Forca UTF-8 no stdout/stderr. Sem isso, quando o processo e pipeado
    (menu grafico) o Python 3.10/3.11 no Windows usa cp1252 e os acentos
    chegam corrompidos na interface."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


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
    out.sort(key=lambda e: (e["start"], e["end"]))
    return out


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


def build_silence_multiplier(entries, n_samples, sr, fade_ms=FADE_MS):
    """Multiplicador para silenciar no audio ORIGINAL apenas as regioes
    que serao dubladas. Intervalos sobrepostos/colados sao fundidos e as
    bordas recebem crossfade. 1.0 = mantem o original, 0.0 = silencio."""
    if not entries:
        return np.ones(n_samples, dtype=np.float32)
    iv = sorted((max(e["start"], 0.0), e["end"]) for e in entries)
    merged = []
    for s, e in iv:
        if merged and s <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
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
    return m


def has_video_stream(path):
    """Detecta se o arquivo de entrada tem stream de video (aceita mp4,
    mkv, mov, avi, webm...) para dublar video mantendo a imagem."""
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


def cleanup_workdir(work_dir, keep_parts, emit_paths):
    """Remove os arquivos intermediarios da pasta de trabalho ao finalizar
    com sucesso (mantem as amostras quando --emit-paths ou --keep-parts)."""
    if not os.path.isdir(work_dir):
        return
    for name in ("orig.wav", "final.wav"):
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
        ),
    )
    ap.add_argument("--audio", required=True,
                    help="Audio ou video no idioma original (mp3/wav/mp4/mkv/mov/avi/webm...)")
    ap.add_argument("--srt", required=True, help="Legenda da traducao (.srt)")
    ap.add_argument("--out", default=None,
                    help="Saida. Padrao: <audio>_dublado.mp3 (ou .mp4 quando o "
                         "arquivo de entrada e video)")
    ap.add_argument("--device", default=None, help="cuda ou cpu (padrao: cuda se disponivel)")
    ap.add_argument("--language", default="pt", help="Idioma da fala (padrao: pt)")
    ap.add_argument("--temperature", type=float, default=None,
                    help="Aleatoriedade da fala (menor = voz mais consistente e "
                         "menos sussurro; maior = mais expressivo). Padrao: 0.8")
    ap.add_argument("--volume", type=float, default=1.0, help="Ganho da voz dublada (padrao: 1.0)")
    ap.add_argument("--seed", type=int, default=None,
                    help="Semente base para reprodutibilidade. Padrao: 1000 "
                         "(cada legenda usa seed + indice)")
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

    dub_entries = [e for e in entries if re.search(r"\w", e["text"])]
    if not dub_entries:
        sys.exit("[ERRO] Nenhuma legenda com texto para dublar no SRT.")
    silence = build_silence_multiplier(dub_entries, len(track), TARGET_SR)
    track *= silence[:, None]

    print("\n[2/4] Carregando motor (chatterbox)...")
    engine = get_chatterbox(args.device)

    print(f"\n[3/4] Dublagem de {len(entries)} legendas...")
    done = 0
    skipped = 0
    for pos, e in enumerate(entries):
        i_disp = pos + 1
        n_tot = len(entries)
        start, end, text = e["start"], e["end"], e["text"]
        if not re.search(r"\w", text):
            skipped += 1
            print(f"  [DUB {i_disp:3d}/{n_tot}] (ignorada: sem texto)")
            continue
        seg_id = f"seg_{e['index']:03d}"
        ref_wav = os.path.join(parts_dir, seg_id + ".ref.wav")
        synth_wav = os.path.join(parts_dir, seg_id + ".wav")
        conv_wav = os.path.join(parts_dir, seg_id + ".conv.wav")
        fitted_wav = os.path.join(parts_dir, seg_id + ".fitted.wav")
        print(f"  [DUB {i_disp:3d}/{n_tot}] {start:8.3f}-{end:8.3f}  {text[:70]}")
        try:
            extract_reference(args.audio, start, end, ref_wav)
            seed = args.seed + e["index"] if args.seed is not None else 1000 + e["index"]
            synthesize_text(engine, text, ref_wav, synth_wav,
                            args.language, temperature=args.temperature, seed=seed)
            convert_to_track_format(synth_wav, conv_wav, channels)
            final_seg = fit_to_duration(conv_wav, fitted_wav, end - start, channels)
            data, _ = sf.read(final_seg, dtype="float32", always_2d=True)
            limit = int(round((end - start) * sr))
            if len(data) > limit:
                data = data[:limit]
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
            print(f"  [DUB {i_disp:3d}/{n_tot}] [ERRO] {ex}")

    print(f"\n[4/4] Finalizando ({done}/{len(entries)} legendas dubladas)...")
    if skipped:
        print(f"  {skipped} legendas ignoradas (sem texto).")
    final_wav = os.path.join(work_dir, "final.wav")
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
