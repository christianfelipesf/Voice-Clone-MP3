#!/usr/bin/env python3
"""
Dublador - Painel web Flask (modulo do pacote dublador)
=======================================================
Painel web que usa o motor do pacote (core/youtube):

- Upload de audio/video pelo navegador (ou caminho local no servidor)
- Execucao em segundo plano com log, progresso e fases em tempo real (SSE)
- Preview do VIDEO em tempo real (stream MPEG-TS + mpegts.js no navegador)
- Amostras de audio por trecho e download do resultado
- Opcoes avancadas (motor edge/chatterbox, modelos Whisper, resolucao etc.)
- Padrao otimizado para velocidade: Edge TTS + Whisper distil-large-v3

Reutilizavel:
    from dublador.web import create_app, WebServer

    app = create_app()                      # app Flask
    srv = WebServer(host="127.0.0.1", port=5000)
    srv.start()                             # roda em thread (GUI usa isso)
    ...
    srv.stop()

CLI:
    python dublar_web.py                 # http://127.0.0.1:5000
    python dublar_web.py --port 8080     # porta customizada
    python dublar_web.py --host 0.0.0.0  # acessivel pela rede local

Requisitos: pip install flask
"""

import os
import re
import sys
import json
import time
import queue
import uuid
import shutil
import argparse
import subprocess
import threading
from collections import deque

try:
    from flask import (Flask, request, jsonify, Response, abort,
                       send_file, send_from_directory)
    FLASK_AVAILABLE = True
except ImportError:
    Flask = None
    FLASK_AVAILABLE = False

try:
    from dublador import preview as _preview
except Exception:
    _preview = None

from dublador.config import (STATIC_DIR, UPLOAD_DIR, JOBS_DIR,
                             SCRIPT, YT_SCRIPT, PYTHON,
                             DEVICES, LANGS, WHISPER_MODELS, RESOLUTIONS,
                             BROWSERS, ENGINE_LABELS, PHASES, DEFAULTS,
                             EDGE_VOICES, VOICE_MODES,
                             engine_to_value, save_config, load_config,
                             reset_config, force_utf8_stdout,
                             ffmpeg_available, sanitize, has_video_stream)

VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v")
MAX_TOTAL_JOBS = 20

for d in (UPLOAD_DIR, JOBS_DIR):
    os.makedirs(d, exist_ok=True)

_JOBS = {}
_JOBS_LOCK = threading.Lock()


# ============================================================
# CONFIG
# ============================================================

def default_opts():
    cfg = load_config()
    def val(key, default):
        return cfg.get(key, default)
    return {
        "device": val("device", DEFAULTS["device"]),
        "lang": val("lang", DEFAULTS["lang"]),
        "res": val("res", DEFAULTS["res"]),
        "whisper": val("whisper", DEFAULTS["whisper"]),
        "engine": engine_to_value(val("engine", DEFAULTS["engine"])),
        "volume": val("volume", DEFAULTS["volume"]),
        "temp": val("temp", DEFAULTS["temp"]),
        "seed": val("seed", DEFAULTS["seed"]),
        "maxtempo": val("maxtempo", DEFAULTS["maxtempo"]),
        "cookies": val("cookies", DEFAULTS["cookies"]),
        "preview": val("preview", DEFAULTS.get("preview", True)),
        "samples": True,
        "dry": False,
        "parallel": val("parallel", DEFAULTS.get("parallel", "1")),
        "whisper_beam": val("whisper_beam", DEFAULTS.get("whisper_beam", "")),
        "voice": val("voice", DEFAULTS.get("voice", "auto")),
    }


# ============================================================
# JOB
# ============================================================

class Job:
    def __init__(self, jid, cmd, out_path, preview_wanted, mode, title):
        self.id = jid
        self.cmd = cmd
        self.out_path = out_path
        self.preview_wanted = preview_wanted
        self.mode = mode
        self.title = title
        self.dir = os.path.join(JOBS_DIR, jid)
        self.status = "queued"
        self.proc = None
        self.lines = deque(maxlen=500)
        self.q = queue.Queue()
        self.progress = [0, 0]
        self.segments = []
        self.plan = []
        self.preview = None
        self.preview_resume_from = 0.0
        self.error = None
        self.cancelled = False
        self.created = time.time()
        self.finished = None

    def info(self):
        return {
            "id": self.id,
            "status": self.status,
            "mode": self.mode,
            "title": self.title,
            "progress": self.progress,
            "segments": len(self.segments),
            "out_path": self.out_path,
            "created": self.created,
            "finished": self.finished,
            "error": self.error,
            "preview_wanted": self.preview_wanted,
            "preview_resume_from": self.preview_resume_from,
            "cmd": " ".join(self.cmd),
        }


def get_job(jid):
    with _JOBS_LOCK:
        return _JOBS.get(jid)


def _emit(job, type_, **kw):
    data = {"type": type_}
    data.update(kw)
    job.q.put(data)


def _parse(job, line):
    m = re.search(r"\[PROGRESS\]\s+(\d+)/(\d+)", line)
    if m:
        done, total = int(m.group(1)), int(m.group(2))
        job.progress = [done, total]
        _emit(job, "progress", done=done, total=total,
              pct=(done / max(total, 1)))
        return
    m = re.match(r"^\[PLAN-SEG\]\s+(\d+)\t([\d.eE+-]+)\t([\d.eE+-]+)\s*$", line)
    if m:
        job.plan.append((int(m.group(1)), float(m.group(2)),
                         float(m.group(3))))
        return
    m = re.match(r"^\[SEG\] (\d+)\t(.+?)\t([\d.]+)\t([\d.]+)\t(.*)$", line)
    if m:
        seg = {"idx": int(m.group(1)), "path": m.group(2),
               "start": float(m.group(3)), "end": float(m.group(4)),
               "text": m.group(5)}
        job.segments.append(seg)
        job.preview_resume_from = max(job.preview_resume_from, seg["end"])
        _emit(job, "seg", **seg)
        if job.preview is not None:
            if not job.preview.has_plan() and job.plan:
                job.preview.set_plan(job.plan)
            job.preview.add_segment(seg["start"], seg["end"], seg["path"])
        return
    m = re.match(r"^\[VIDEO\] (.+)$", line)
    if m:
        vpath = m.group(1).strip()
        _emit(job, "video", path=vpath)
        if job.preview_wanted and job.preview is None:
            _ensure_preview(job, vpath)
        return
    for key, label in PHASES:
        if key in line:
            _emit(job, "phase", label=label)
            return
    if "[OK]" in line:
        _emit(job, "phase", label="Concluido")


def _reader(job):
    try:
        for line in job.proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            job.lines.append(line)
            _emit(job, "log", line=line)
            _parse(job, line)
    except Exception as ex:
        _emit(job, "log", line=f"[ERRO] {ex}")
    code = job.proc.wait()
    job.finished = time.time()
    if job.cancelled:
        job.status = "cancelled"
        job.error = "Cancelado pelo usuario."
    elif code == 0:
        job.status = "done"
    else:
        job.status = "error"
        job.error = job.lines[-1] if job.lines else f"processo saiu com codigo {code}"
    if job.preview is not None:
        try:
            job.preview.close()
        except Exception:
            pass
    _emit(job, "status", status=job.status, error=job.error or None)
    if job.status == "done":
        _emit(job, "output", path=job.out_path)
    job.q.put(None)


def _spawn(job):
    job.status = "running"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    _emit(job, "log", line="> " + " ".join(job.cmd) + "\n")
    job.proc = subprocess.Popen(
        [PYTHON, "-u"] + job.cmd[1:],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        env=env)
    threading.Thread(target=_reader, args=(job,), daemon=True).start()


# ============================================================
# PREVIEW (stream para o navegador)
# ============================================================

def _ensure_preview(job, video_path):
    if job.preview is not None:
        return
    if _preview is None:
        _emit(job, "log", line="[PREVIEW] numpy/soundfile ausentes; "
                               "sem stream em tempo real.\n")
        return
    try:
        pv = _preview.WebLivePreview(
            video_path, log=lambda msg: _emit(job, "log", line=msg),
            work_dir=job.dir,
            on_restart=lambda: _emit(
                job, "preview_restart",
                url=f"/api/jobs/{job.id}/preview"))
        pv.prepare_async()
        pv.launch()
        job.preview = pv
        _emit(job, "preview_start",
              url=f"/api/jobs/{job.id}/preview")
    except Exception as ex:
        _emit(job, "log", line=f"[PREVIEW] nao foi possivel iniciar: {ex}\n")


# ============================================================
# COMANDOS
# ============================================================

def _add_numeric(cmd, flag, value):
    if not value or not str(value).strip():
        return
    try:
        if flag in ("--seed", "--whisper-beam"):
            cmd += [flag, str(int(value))]
        else:
            cmd += [flag, str(float(value))]
    except ValueError:
        pass


def build_file_cmd(job, audio_path, srt_path, out_path, opts):
    cmd = [PYTHON, SCRIPT, "--audio", audio_path,
           "--language", opts["lang"], "--engine", opts["engine"]]
    if srt_path and os.path.exists(srt_path):
        cmd += ["--srt", srt_path]
    else:
        cmd += ["--whisper-model", opts["whisper"]]
        _add_numeric(cmd, "--whisper-beam", opts.get("whisper_beam"))
    cmd += ["--out", out_path]
    cmd += ["--voice", opts.get("voice") or "auto"]
    if opts["device"] not in ("", "auto"):
        cmd += ["--device", opts["device"]]
    parallel = int(opts.get("parallel") or 1)
    if parallel > 1:
        cmd += ["--parallel", str(parallel)]
    _add_numeric(cmd, "--temperature", opts["temp"])
    _add_numeric(cmd, "--volume", opts["volume"])
    _add_numeric(cmd, "--seed", opts["seed"])
    _add_numeric(cmd, "--max-tempo", opts["maxtempo"])
    cmd += ["--emit-paths"]
    if opts.get("dry"):
        cmd.append("--dry-run")
    return cmd


def build_yt_cmd(job, url, out_path, opts):
    cmd = [PYTHON, YT_SCRIPT, "--url", url,
           "--resolution", opts["res"], "--language", opts["lang"],
           "--out", out_path]
    if opts["device"] not in ("", "auto"):
        cmd += ["--device", opts["device"]]
    cmd += ["--whisper-model", opts["whisper"]]
    _add_numeric(cmd, "--whisper-beam", opts.get("whisper_beam"))
    cmd += ["--engine", opts["engine"]]
    cmd += ["--voice", opts.get("voice") or "auto"]
    if opts.get("cookies"):
        cmd += ["--cookies-from-browser", opts["cookies"]]
    parallel = int(opts.get("parallel") or 1)
    if parallel > 1:
        cmd += ["--parallel", str(parallel)]
    _add_numeric(cmd, "--temperature", opts["temp"])
    _add_numeric(cmd, "--volume", opts["volume"])
    _add_numeric(cmd, "--seed", opts["seed"])
    _add_numeric(cmd, "--max-tempo", opts["maxtempo"])
    cmd += ["--emit-paths"]
    if opts.get("dry"):
        cmd.append("--dry-run")
    return cmd


# ============================================================
# ROTAS
# ============================================================

def _wait_preview(job, timeout=90.0):
    """Espera (com limite) o preview ficar pronto. Em jobs do YouTube o
    preview so pode ser montado depois que o video for baixado; quem pede
    o stream antes disso nao deve levar 404 imediato, e sim aguardar."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.preview is not None:
            return job.preview
        if job.status in ("done", "error", "cancelled"):
            return None
        time.sleep(0.1)
    return None


def create_app():
    """Cria o app Flask. Requer flask instalado."""
    if not FLASK_AVAILABLE:
        raise RuntimeError(
            "Flask nao instalado. Rode: pip install flask")

    app = Flask(__name__, static_folder=STATIC_DIR,
                static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = None

    @app.route("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.route("/api/info")
    def api_info():
        return jsonify({
            "ffmpeg": ffmpeg_available(),
            "preview": _preview is not None,
            "devices": DEVICES,
            "langs": LANGS,
            "whisper_models": WHISPER_MODELS,
            "resolutions": RESOLUTIONS,
            "browsers": BROWSERS,
            "engines": ENGINE_LABELS,
            "voices": EDGE_VOICES,
            "voice_modes": VOICE_MODES,
            "defaults": default_opts(),
        })

    @app.route("/api/jobs", methods=["GET"])
    def list_jobs():
        with _JOBS_LOCK:
            jobs = sorted(_JOBS.values(), key=lambda j: j.created,
                          reverse=True)
        return jsonify([j.info() for j in jobs])

    @app.route("/api/jobs", methods=["POST"])
    def create_job():
        if not ffmpeg_available():
            return jsonify(error="ffmpeg nao encontrado no PATH."), 400
        form = request.form
        opts = default_opts()
        for k in opts:
            if k in form:
                v = form.get(k)
                if k in ("preview", "samples", "dry"):
                    opts[k] = v in ("1", "true", "on")
                else:
                    opts[k] = (v or "").strip()

        mode = form.get("mode", "file")
        jid = uuid.uuid4().hex[:12]
        jobdir = os.path.join(JOBS_DIR, jid)
        os.makedirs(jobdir, exist_ok=True)

        try:
            if mode == "youtube":
                url = form.get("url", "").strip()
                if not url.startswith(("http://", "https://")):
                    return jsonify(error="Cole um link do YouTube valido."), 400
                title = "youtube"
                out_path = os.path.join(jobdir, "saida_dublada.mp4")
                cmd = build_yt_cmd(jid, url, out_path, opts)
                preview_wanted = opts["preview"]
            else:
                uploaded = request.files.get("file")
                srt_file = request.files.get("srt")
                local_path = form.get("path", "").strip()
                audio_path = None
                if uploaded is not None and uploaded.filename:
                    name = sanitize(os.path.basename(uploaded.filename))
                    audio_path = os.path.join(jobdir, name)
                    uploaded.save(audio_path)
                elif local_path and os.path.exists(local_path):
                    audio_path = local_path
                if not audio_path:
                    return jsonify(error="Selecione um arquivo ou informe um "
                                         "caminho valido."), 400
                srt_path = None
                if srt_file is not None and srt_file.filename:
                    srt_path = os.path.join(jobdir, "legenda.srt")
                    srt_file.save(srt_path)
                is_video = has_video_stream(audio_path)
                ext = ".mp4" if is_video else ".mp3"
                base = os.path.splitext(os.path.basename(audio_path))[0]
                out_path = os.path.join(jobdir, base + "_dublado" + ext)
                title = os.path.basename(audio_path)
                cmd = build_file_cmd(jid, audio_path, srt_path, out_path, opts)
                preview_wanted = opts["preview"] and is_video

            with _JOBS_LOCK:
                if len(_JOBS) >= MAX_TOTAL_JOBS:
                    oldest = sorted(_JOBS.values(),
                                    key=lambda j: j.created)[0]
                    _JOBS.pop(oldest.id, None)
                job = Job(jid, cmd, out_path, preview_wanted, mode, title)
                _JOBS[jid] = job
        except Exception as ex:
            return jsonify(error=f"Falha ao preparar o job: {ex}"), 500

        _spawn(job)
        if preview_wanted and mode == "file" and _preview is not None:
            _ensure_preview(job, audio_path)

        return jsonify(id=jid, preview=preview_wanted)

    @app.route("/api/jobs/<jid>")
    def job_info(jid):
        job = get_job(jid)
        if job is None:
            abort(404)
        return jsonify(job.info())

    @app.route("/api/jobs/<jid>/stream")
    def job_stream(jid):
        job = get_job(jid)
        if job is None:
            abort(404)

        def gen():
            yield ": connected\n\n"
            while True:
                try:
                    data = job.q.get(timeout=15)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                if data is None:
                    break
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    @app.route("/api/jobs/<jid>/preview")
    def job_preview(jid):
        job = get_job(jid)
        if job is None:
            abort(404)
        pv = job.preview
        if pv is None:
            pv = _wait_preview(job)
            if pv is None:
                abort(404)
        return Response(pv.iter_output(), mimetype="video/mp2t",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    @app.route("/api/jobs/<jid>/samples/<int:idx>")
    def job_sample(jid, idx):
        job = get_job(jid)
        if job is None:
            abort(404)
        for seg in job.segments:
            if seg["idx"] == idx and os.path.exists(seg["path"]):
                return send_file(seg["path"], as_attachment=False,
                                 mimetype="audio/wav")
        abort(404)

    @app.route("/api/jobs/<jid>/output")
    def job_output(jid):
        job = get_job(jid)
        if job is None or job.status != "done":
            abort(404)
        if not os.path.exists(job.out_path):
            abort(404)
        return send_file(job.out_path, as_attachment=True,
                         download_name=os.path.basename(job.out_path))

    @app.route("/api/jobs/<jid>/stop", methods=["POST"])
    def job_stop(jid):
        job = get_job(jid)
        if job is None or job.proc is None or job.proc.poll() is not None:
            return jsonify(ok=False, error="Nenhuma dublagem em andamento."), 400
        job.cancelled = True
        try:
            subprocess.run(["taskkill", "/PID", str(job.proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
        except Exception:
            try:
                job.proc.terminate()
            except Exception:
                pass
        return jsonify(ok=True)

    @app.route("/api/jobs/<jid>/pause", methods=["POST"])
    def job_pause(jid):
        job = get_job(jid)
        if job is None or job.proc is None or job.proc.poll() is not None:
            return jsonify(ok=False), 400
        action = request.json.get("action") if request.is_json else None
        try:
            import psutil
            p = psutil.Process(job.proc.pid)
            desc = p.children(recursive=True)
        except Exception:
            p, desc = None, []
        try:
            if action == "pause":
                if p is not None:
                    p.suspend()
                for c in desc:
                    c.suspend()
            else:
                for c in reversed(desc):
                    c.resume()
                if p is not None:
                    p.resume()
        except Exception as ex:
            return jsonify(ok=False, error=str(ex)), 500
        return jsonify(ok=True)

    @app.route("/api/jobs/<jid>", methods=["DELETE"])
    def job_delete(jid):
        job = get_job(jid)
        if job is None:
            abort(404)
        if job.proc is not None and job.proc.poll() is None:
            return jsonify(ok=False, error="Pare a dublagem antes de remover."), 400
        with _JOBS_LOCK:
            _JOBS.pop(jid, None)
        shutil.rmtree(job.dir, ignore_errors=True)
        return jsonify(ok=True)

    @app.route("/api/prefs", methods=["POST"])
    def save_prefs():
        data = request.get_json(force=True) or {}
        cfg = load_config()
        cfg.update(data)
        save_config(cfg)
        return jsonify(ok=True)

    @app.route("/api/prefs/reset", methods=["POST"])
    def reset_prefs():
        reset_config()
        return jsonify(ok=True)

    return app


# ============================================================
# SERVIDOR EMBUTIVEL (usado pela GUI)
# ============================================================

class WebServer:
    """Roda o painel web em segundo plano (thread). A GUI chama
    start() quando abre e stop() ao fechar."""

    def __init__(self, host="127.0.0.1", port=5000):
        self.host = host
        self.port = port
        self.url = f"http://{host}:{port}"
        self._thread = None
        self._server = None
        self._app = None

    def start(self):
        if not FLASK_AVAILABLE:
            raise RuntimeError(
                "Flask nao instalado. Rode: pip install flask")
        from werkzeug.serving import make_server
        self._app = create_app()
        self._server = make_server(self.host, self.port, self._app,
                                   threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self.url

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        self._thread = None

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass


# ============================================================
# MAIN (CLI)
# ============================================================

def main():
    force_utf8_stdout()
    ap = argparse.ArgumentParser(description="Painel web do Dublador (Flask)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="IP para escutar (padrao: 127.0.0.1; use 0.0.0.0 para a rede)")
    ap.add_argument("--port", type=int, default=5000, help="Porta (padrao: 5000)")
    args = ap.parse_args()

    if not FLASK_AVAILABLE:
        print("[ERRO] Flask nao instalado. Rode: pip install flask")
        sys.exit(1)

    print("=" * 60)
    print("  Dublador Web")
    print(f"  Acesse: http://{args.host}:{args.port}")
    print("  ffmpeg:", "OK" if ffmpeg_available() else "NAO ENCONTRADO (instale!)")
    print("  preview em tempo real:", "OK" if _preview is not None else "indisponivel (faltam numpy/soundfile)")
    print("=" * 60)
    app = create_app()
    app.run(host=args.host, port=args.port, threaded=True,
            use_reloader=False, debug=False)


if __name__ == "__main__":
    main()
