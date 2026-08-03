#!/usr/bin/env python3
"""
Dublador - Preview em tempo real (modulo do pacote dublador)
============================================================
Mostra o video em um player (ffplay) enquanto a dublagem e gerada,
sincronizado com o progresso. Tambem oferece o WebLivePreview, que monta
o mesmo fluxo MPEG-TS para o navegador (painel web).

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
import re
import sys
import time
import queue
import threading
import subprocess
from collections import deque

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
        bsf = []
        if codec in _VIDEO_CODECS_COPY:
            venc = ["-c:v", "copy"]
            if codec in ("h264", "avc1", "avc3"):
                bsf = ["-bsf:v", "h264_mp4toannexb"]
            elif codec in ("hevc", "h265", "hev1", "hvc1"):
                bsf = ["-bsf:v", "hevc_mp4toannexb"]
        else:
            venc = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency"]

        try:
            self.proc_mux = subprocess.Popen(
                ["ffmpeg", "-y", "-re", "-i", self.video_path,
                 "-f", "s16le", "-ar", str(SR), "-ac", str(CH), "-i", "pipe:0",
                 "-map", "0:v:0", "-map", "1:a:0",
                 *venc, *bsf, "-c:a", "aac", "-strict", "-2", "-b:a", "192k",
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


# ============================================================
# STREAM PARA O NAVEGADOR (painel web)
# ============================================================

_EMPTY = object()


class _Bounded:
    """Buffer circular de bytes para o stream HTTP (descartar o mais antigo
    quando cheio, para o streaming ficar 'ao vivo' e nao explodir a RAM)."""

    def __init__(self, max_items=240):
        self.items = deque(maxlen=max_items)
        self.lock = threading.Condition()
        self.done = False

    def push(self, item):
        with self.lock:
            if self.done:
                return
            self.items.append(item)
            self.lock.notify_all()

    def get(self, timeout=1.0):
        with self.lock:
            if self.items:
                return self.items.popleft()
            if self.done:
                return None
            self.lock.wait(timeout)
            if self.items:
                return self.items.popleft()
            if self.done:
                return None
            return _EMPTY

    def close(self):
        with self.lock:
            self.done = True
            self.lock.notify_all()


class WebLivePreview:
    """Preview em tempo real para o navegador: monta o video + audio dublado
    em um stream MPEG-TS (igual ao LivePreview) mas, em vez de abrir ffplay,
    empurra os bytes para um buffer que o painel web serve por HTTP
    (video/mp2t) e o navegador toca com mpegts.js (MSE)."""

    SR = 44100
    CH = 2
    _VIDEO_CODECS_COPY = ("h264", "hevc", "h265", "avc1", "vp9")

    def __init__(self, video_path, log=None, work_dir=None):
        self.video_path = video_path
        self.log = log or (lambda msg: None)
        self.work_dir = work_dir
        self.lock = threading.Lock()
        self.q = queue.Queue()
        self.closed = False
        self.failed = False
        self.started = False

        self.base = None
        self.base_frames = 0
        self.duration_frames = 0
        self.sent = 0
        self.prepare_thread = None

        self.proc_mux = None
        self.pump = None
        self.feeder = None
        self.buf = _Bounded()
        self._orig_pcm = None
        self._stream_path = None

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
                self.duration_frames = int(float(dur_s) * self.SR)
            except Exception:
                self.duration_frames = 0

            tmp = os.path.join(self.work_dir or ".",
                               f"preview_orig_{os.getpid()}.pcm")
            self._orig_pcm = tmp
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", self.video_path, "-vn",
                 "-ac", str(self.CH), "-ar", str(self.SR), "-f", "s16le", tmp],
                capture_output=True, text=True)
            if r.returncode == 0 and os.path.exists(tmp):
                nbytes = os.path.getsize(tmp)
                self.base_frames = nbytes // (self.CH * 2)
                if self.base_frames > 0:
                    self.base = np.memmap(tmp, dtype="<i2", mode="r",
                                          shape=(self.base_frames, self.CH))
            if not self.duration_frames:
                self.duration_frames = self.base_frames
        except Exception as ex:
            self.failed = True
            self.buf.close()
            self.log(f"[PREVIEW] falha na preparacao: {ex}\n")

    # ------------------------------------------------------------------
    # RECEBER TRECHO DUBLADO
    # ------------------------------------------------------------------
    def add_segment(self, start, end, wav_path):
        if self.closed:
            return
        self.q.put((float(start), float(end), wav_path))

    # ------------------------------------------------------------------
    # MUXER (sem ffplay; bytes vao para o buffer HTTP)
    # ------------------------------------------------------------------
    def _start_muxer(self):
        if self.started or self.closed:
            return False
        if not self.base_frames and self.duration_frames == 0:
            self.log("[PREVIEW] nao foi possivel medir o video.\n")
            return False

        codec = ffprobe(self.video_path, "-select_streams", "v:0",
                        "-show_entries", "stream=codec_name", "-of", "csv=p=0")
        venc = []
        bsf = []
        if codec in self._VIDEO_CODECS_COPY:
            venc = ["-c:v", "copy"]
            if codec in ("h264", "avc1", "avc3"):
                bsf = ["-bsf:v", "h264_mp4toannexb"]
            elif codec in ("hevc", "h265", "hev1", "hvc1"):
                bsf = ["-bsf:v", "hevc_mp4toannexb"]
        else:
            venc = ["-c:v", "libx264", "-preset", "veryfast",
                    "-tune", "zerolatency"]

        try:
            self._stream_path = os.path.join(self.work_dir or ".",
                                             f"preview_stream_{os.getpid()}.ts")
            if os.path.exists(self._stream_path):
                try:
                    os.remove(self._stream_path)
                except OSError:
                    pass
            self.proc_mux = subprocess.Popen(
                ["ffmpeg", "-y", "-re", "-i", self.video_path,
                 "-f", "s16le", "-ar", str(self.SR), "-ac", str(self.CH),
                 "-i", "pipe:0",
                 "-map", "0:v:0", "-map", "1:a:0",
                 *venc, *bsf, "-c:a", "aac", "-strict", "-2", "-b:a", "192k",
                 "-g", "30", "-sc_threshold", "0",
                 "-f", "mpegts", "-muxdelay", "0.2",
                 "-flush_packets", "1", self._stream_path],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, bufsize=0)

            def _err():
                log_path = os.path.join(self.work_dir or ".",
                                        "preview_mux.log")
                try:
                    with open(log_path, "w", encoding="utf-8",
                              errors="replace") as fh:
                        for line in self.proc_mux.stderr:
                            s = line.decode("utf-8", "replace").rstrip()
                            fh.write(s + "\n")
                            fh.flush()
                            if s and not s.startswith("ffmpeg version") \
                                    and not s.startswith("  built on") \
                                    and not s.startswith("  configuration"):
                                self.log(f"[PREVIEW] muxer: {s}\n")
                except Exception:
                    pass

            threading.Thread(target=_err, daemon=True).start()

            def _pump():
                offset = 0
                try:
                    while True:
                        try:
                            with open(self._stream_path, "rb") as f:
                                f.seek(offset)
                                data = f.read(65536)
                                if data:
                                    offset += len(data)
                                    self.buf.push(data)
                        except OSError:
                            pass
                        if self.proc_mux.poll() is not None:
                            try:
                                with open(self._stream_path, "rb") as f:
                                    f.seek(offset)
                                    data = f.read()
                                    if data:
                                        offset += len(data)
                                        self.buf.push(data)
                            except OSError:
                                pass
                            break
                        time.sleep(0.05)
                except Exception:
                    pass
                finally:
                    self.buf.close()

            self.pump = threading.Thread(target=_pump, daemon=True)
            self.pump.start()
            self.started = True
            self.log("[PREVIEW] stream aberto (sincronizado com a geracao).\n")
            return True
        except Exception as ex:
            self.failed = True
            self.buf.close()
            self.log(f"[PREVIEW] nao foi possivel abrir o stream: {ex}\n")
            return False

    def _run_feeder(self):
        if self.prepare_thread is not None:
            self.prepare_thread.join()
        if self.failed:
            self._drain()
            return
        first = self.q.get()
        if first is None or self.closed:
            return
        if not self._start_muxer():
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
                self.buf.close()
                return

    # ------------------------------------------------------------------
    # ALIMENTACAO DO PIPE DE AUDIO (igual ao LivePreview)
    # ------------------------------------------------------------------
    def _feed(self, start, end, wav_path):
        a = int(round(start * self.SR))
        b = int(round(end * self.SR))
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
            self._write_int16(np.zeros((b - a, self.CH), dtype=np.int16), 0, b - a)

    def _write_dubbed(self, a, b, wav_path):
        data, sr = sf.read(wav_path, dtype="float32", always_2d=True)
        if sr != self.SR:
            self.log(f"[PREVIEW] aviso: taxa {sr} != {self.SR} "
                     f"ignorando alinhamento.\n")
        if data.shape[1] == 1:
            data = np.repeat(data, self.CH, axis=1)
        elif data.shape[1] > self.CH:
            data = data[:, :self.CH]
        n = min(len(data), b - a)
        if n <= 0:
            return
        pcm = (np.clip(data[:n], -1.0, 1.0) * 32767).astype(np.int16)
        self._write_int16(pcm, 0, n)

    def _write_int16(self, arr, a, b):
        if self.closed or self.proc_mux is None or self.proc_mux.stdin is None:
            return
        step = self.SR
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
        if not self.closed:
            self.closed = True
            if self.sent < self.duration_frames and not self.failed:
                self._write_base(self.sent, self.duration_frames)
        self._close_procs()
        self.buf.close()

    def _close_procs(self):
        if self.proc_mux is not None:
            try:
                if self.proc_mux.stdin:
                    self.proc_mux.stdin.close()
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
        if self._orig_pcm and os.path.exists(self._orig_pcm):
            try:
                os.remove(self._orig_pcm)
            except OSError:
                pass
        if self._stream_path and os.path.exists(self._stream_path):
            try:
                os.remove(self._stream_path)
            except OSError:
                pass

    def close(self):
        self.closed = True
        self.q.put(None)
        if self.feeder is not None:
            self.feeder.join(timeout=8)
        self._finish()

    def launch(self):
        if self.feeder is None and not self.closed:
            self.feeder = threading.Thread(target=self._run_feeder, daemon=True)
            self.feeder.start()

    # ------------------------------------------------------------------
    # GENERATOR PARA O HTTP (Flask Response)
    # ------------------------------------------------------------------
    def iter_output(self):
        while not (self.started or self.failed or self.closed):
            if self.failed or self.closed:
                break
            time.sleep(0.2)
        if not self.started:
            return
        while True:
            item = self.buf.get(timeout=1.0)
            if item is _EMPTY:
                continue
            if item is None:
                break
            yield item
