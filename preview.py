#!/usr/bin/env python3
"""
Dublador - Preview em tempo real (player sincronizado)
=======================================================
Mostra o video em um player (ffplay) enquanto a dublagem e gerada,
sincronizado com o progresso: o player toca o audio ORIGINAL nas partes
ainda nao dubladas e, a medida que cada trecho dublado fica pronto, ele
e colocado na posicao correta da linha do tempo. O video "espera" quando
um trecho ainda esta sendo gerado - voce assiste a dublagem sendo montada
em tempo real.

Como funciona:
  - O audio dublado chega em tempo real como uma sequencia de trechos
    (a posicao na linha do tempo e conhecida). Cada trecho pronto e
    "empurrado" para um pipe de audio (PCM s16le 44100 Hz stereo).
  - O video original e lido em tempo real (-re) direto do arquivo.
  - Um ffmpeg junta os dois fluxos em mpegts e um ffplay exibe.
  - O player nunca avanca alem do ultimo trecho finalizado: quando a
    geracao para, o pipe de audio fica em silencio (sem dados) e o
    fluxo fica aguardando.

Uso (pelo menu grafico):
    pv = preview.LivePreview(video_path, log=callback)
    pv.prepare_async()
    ...
    pv.add_segment(start, end, wav_path)   # a cada trecho dublado pronto
    ...
    pv.close()

Requisitos: ffmpeg/ffplay/ffprobe no PATH, numpy, soundfile.
"""

import os
import sys
import queue
import threading
import subprocess

import numpy as np
import soundfile as sf

SR = 44100          # taxa do preview (igual ao TARGET_SR do dublar.py)
CH = 2              # canais do preview (sempre stereo)
FRAME = SR * 2 * 2  # bytes por segundo (2 canais * 2 bytes)
_VIDEO_CODECS_COPY = ("h264", "hevc", "h265", "avc1", "vp9")


def ffprobe(path, *args):
    cmd = ["ffprobe", "-v", "error"] + list(args) + [path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout.strip()


def has_video_stream(path):
    out = ffprobe(path, "-select_streams", "v:0",
                  "-show_entries", "stream=codec_type", "-of", "csv=p=0")
    return "video" in out.lower()


class LivePreview:
    """Player de video sincronizado com a geracao da dublagem."""

    def __init__(self, video_path, log=None, work_dir=None):
        self.video_path = video_path
        self.log = log or (lambda msg: None)
        self.work_dir = work_dir
        self.lock = threading.Lock()
        self.q = queue.Queue()
        self.closed = False
        self.failed = False
        self.started = False

        self.base = None            # memmap int16 (N, 2) do audio ORIGINAL
        self.base_frames = 0        # numero de frames do audio original
        self.duration_frames = 0    # duracao do video em frames
        self.sent = 0               # frames ja escritos no pipe
        self.prepare_thread = None

        self.proc_mux = None
        self.proc_play = None
        self.pump = None
        self.feeder = None
        self._orig_pcm = None

    # ------------------------------------------------------------------
    # PREPARACAO (decodifica o audio original e mede a duracao)
    # ------------------------------------------------------------------
    def prepare_async(self):
        self.prepare_thread = threading.Thread(target=self._prepare, daemon=True)
        self.prepare_thread.start()

    def _prepare(self):
        try:
            dur_s = ffprobe(self.video_path, "-show_entries",
                            "format=duration", "-of", "csv=p=0")
            try:
                self.duration_frames = int(float(dur_s) * SR)
            except Exception:
                self.duration_frames = 0

            tmp = os.path.join(self.work_dir or ".",
                               f"preview_orig_{os.getpid()}.pcm")
            self._orig_pcm = tmp
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", self.video_path, "-vn",
                 "-ac", str(CH), "-ar", str(SR), "-f", "s16le", tmp],
                capture_output=True, text=True)
            if r.returncode == 0 and os.path.exists(tmp):
                nbytes = os.path.getsize(tmp)
                self.base_frames = nbytes // (CH * 2)
                if self.base_frames > 0:
                    self.base = np.memmap(tmp, dtype="<i2", mode="r",
                                          shape=(self.base_frames, CH))
            if not self.duration_frames:
                self.duration_frames = self.base_frames
        except Exception as ex:
            self.failed = True
            self.log(f"[PREVIEW] falha na preparacao: {ex}\n")

    # ------------------------------------------------------------------
    # RECEBER TRECHO DUBLADO (chamado a cada [SEG])
    # ------------------------------------------------------------------
    def add_segment(self, start, end, wav_path):
        if self.closed:
            return
        self.q.put((float(start), float(end), wav_path))

    # ------------------------------------------------------------------
    # INICIO (junta o ffmpeg muxer + ffplay)
    # ------------------------------------------------------------------
    def _start_player(self):
        if self.started or self.closed:
            return False
        if not self.base_frames and self.duration_frames == 0:
            self.log("[PREVIEW] nao foi possivel medir o video.\n")
            return False

        codec = ffprobe(self.video_path, "-select_streams", "v:0",
                        "-show_entries", "stream=codec_name", "-of", "csv=p=0")
        venc = []
        if codec in _VIDEO_CODECS_COPY:
            venc = ["-c:v", "copy"]
        else:
            venc = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency"]

        try:
            self.proc_mux = subprocess.Popen(
                ["ffmpeg", "-y", "-re", "-i", self.video_path,
                 "-f", "s16le", "-ar", str(SR), "-ac", str(CH), "-i", "pipe:0",
                 "-map", "0:v:0", "-map", "1:a:0",
                 *venc, "-c:a", "aac", "-b:a", "192k",
                 "-f", "mpegts", "-muxdelay", "0.2", "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0)
            self.proc_play = subprocess.Popen(
                ["ffplay", "-f", "mpegts", "-i", "pipe:0",
                 "-window_title", "Dublagem em tempo real",
                 "-autoexit", "-loglevel", "error"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, bufsize=0)

            def _pump():
                try:
                    while not self.closed:
                        data = self.proc_mux.stdout.read(65536)
                        if not data:
                            break
                        if self.proc_play and self.proc_play.stdin:
                            self.proc_play.stdin.write(data)
                            self.proc_play.stdin.flush()
                except Exception:
                    pass

            self.pump = threading.Thread(target=_pump, daemon=True)
            self.pump.start()
            self.started = True
            self.log("[PREVIEW] player aberto (sincronizado com a geracao).\n")
            return True
        except Exception as ex:
            self.failed = True
            self.log(f"[PREVIEW] nao foi possivel abrir o player: {ex}\n")
            return False

    def _run_feeder(self):
        # espera a preparacao terminar (decodificacao do audio original)
        if self.prepare_thread is not None:
            self.prepare_thread.join()
        if self.failed:
            self._drain()
            return
        # so abre o player quando o PRIMEIRO trecho chegar, para o audio
        # comecar em t=0 junto com o video (evita dessincronia)
        first = self.q.get()
        if first is None or self.closed:
            return
        if not self._start_player():
            self._drain()
            return
        try:
            self._feed(*first)
        except Exception as ex:
            self.log(f"[PREVIEW] erro ao alimentar audio: {ex}\n")
        while not self.closed:
            try:
                item = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._feed(*item)
            except Exception as ex:
                self.log(f"[PREVIEW] erro ao alimentar audio: {ex}\n")
                break
        self._finish()

    def _drain(self):
        while not self.closed:
            try:
                self.q.get(timeout=0.2)
            except queue.Empty:
                return

    # ------------------------------------------------------------------
    # ALIMENTACAO DO PIPE DE AUDIO
    # ------------------------------------------------------------------
    def _feed(self, start, end, wav_path):
        a = int(round(start * SR))
        b = int(round(end * SR))
        if a < self.sent:
            self.log(f"[PREVIEW] aviso: trecho #{wav_path.split(os.sep)[-1]} "
                     f"sobrepoe o anterior (ainda nao suportado).\n")
        self._write_base(self.sent, a)
        self._write_dubbed(a, b, wav_path)
        self.sent = max(self.sent, b)

    def _write_base(self, a, b):
        a = max(a, 0)
        b = min(b, max(self.base_frames, a))
        if b <= a:
            return
        if self.base is not None:
            self._write_int16(self.base, a, b)
        else:
            self._write_int16(np.zeros((b - a, CH), dtype=np.int16), 0, b - a)

    def _write_dubbed(self, a, b, wav_path):
        data, sr = sf.read(wav_path, dtype="float32", always_2d=True)
        if sr != SR:
            self.log(f"[PREVIEW] aviso: taxa {sr} != {SR} ignorando alinhamento.\n")
        if data.shape[1] == 1:
            data = np.repeat(data, CH, axis=1)
        elif data.shape[1] > CH:
            data = data[:, :CH]
        n = min(len(data), b - a)
        if n <= 0:
            return
        pcm = (np.clip(data[:n], -1.0, 1.0) * 32767).astype(np.int16)
        self._write_int16(pcm, 0, n)

    def _write_int16(self, arr, a, b):
        if self.closed or self.proc_mux is None or self.proc_mux.stdin is None:
            return
        step = SR  # ~1 segundo por escrita
        while a < b and not self.closed:
            e = min(b, a + step)
            try:
                self.proc_mux.stdin.write(arr[a:e].tobytes())
                self.proc_mux.stdin.flush()
            except Exception:
                self.closed = True
                return
            a = e

    # ------------------------------------------------------------------
    # FIM
    # ------------------------------------------------------------------
    def _finish(self):
        if self.closed:
            return
        self.closed = True
        # completa com o restante do audio original ate o fim do video
        if self.sent < self.duration_frames and not self.failed:
            self._write_base(self.sent, self.duration_frames)
        self._close_procs()

    def _close_procs(self):
        for proc, is_mux in ((self.proc_mux, True), (self.proc_play, False)):
            if proc is None:
                continue
            try:
                if is_mux and proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
        if self.proc_mux is not None:
            try:
                self.proc_mux.wait(timeout=5)
            except Exception:
                try:
                    self.proc_mux.kill()
                except Exception:
                    pass
        if self.proc_play is not None:
            try:
                self.proc_play.wait(timeout=3)
            except Exception:
                try:
                    self.proc_play.kill()
                except Exception:
                    pass
        if self._orig_pcm and os.path.exists(self._orig_pcm):
            try:
                os.remove(self._orig_pcm)
            except OSError:
                pass

    def close(self):
        self.closed = True
        self.q.put(None)
        if self.feeder is not None:
            self.feeder.join(timeout=8)
        self._close_procs()

    def launch(self):
        """Inicia o preview (thread de alimentacao). Deve ser chamado depois
        de prepare_async; a cada trecho pronto, chame add_segment."""
        if self.feeder is None and not self.closed:
            self.feeder = threading.Thread(target=self._run_feeder, daemon=True)
            self.feeder.start()
