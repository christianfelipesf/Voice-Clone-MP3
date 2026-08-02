#!/usr/bin/env python3
"""
Dublador v2 - Interface grafica (customtkinter)
================================================
Menu para configurar e rodar o dublar.py sem linha de comando.
Motores de voz: chatterbox (recomendado, MIT, pt-br) ou xtts.

Uso:
    python dublar_gui.py
"""

import os
import re
import sys
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(BASE_DIR, "dublar.py")
PYTHON = sys.executable

ctk.set_appearance_mode("light")
ctk.set_default_color_theme(os.path.join(BASE_DIR, "dublador_theme.json"))

DEVICES = ["auto", "cuda", "cpu"]
ENGINES = ["auto", "chatterbox", "xtts"]
LANGS = ["pt", "en", "es", "fr", "de", "it", "zh", "ja", "ko"]


class DublarGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Dublador v2")
        self.geometry("900x760")
        self.minsize(760, 600)
        self.grid_columnconfigure(0, weight=1)

        self.proc = None
        self.output_q = queue.Queue()
        self.samples = {}
        self.sample_rows = {}
        self.paused = False
        self.cancelled = False
        self.var_audio = tk.StringVar(value=os.path.join(BASE_DIR, "audio_para_dublar", "audio.mp3"))
        self.var_srt = tk.StringVar(value=os.path.join(BASE_DIR, "audio_para_dublar", "audio.srt"))
        self.var_out = tk.StringVar(value="")
        self.var_device = tk.StringVar(value="auto")
        self.var_engine = tk.StringVar(value="auto")
        self.var_lang = tk.StringVar(value="pt")
        self.var_temp = tk.StringVar(value="")
        self.var_volume = tk.StringVar(value="1.0")
        self.var_dry = tk.BooleanVar(value=False)
        self.var_samples = tk.BooleanVar(value=False)

        self._build_ui()

    # ------------------------------------------------------------------
    def _draw_gradient(self, canvas, height, c1, c2):
        width = canvas.winfo_width()
        if width < 2:
            return
        r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
        r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
        canvas.delete("grad")
        for y in range(height):
            t = y / max(height - 1, 1)
            col = "#%02x%02x%02x" % (int(r1 + (r2 - r1) * t),
                                     int(g1 + (g2 - g1) * t),
                                     int(b1 + (b2 - b1) * t))
            canvas.create_line(0, y, width, y, fill=col, tags="grad")

    def _build_header(self):
        header = tk.Canvas(self, height=88, highlightthickness=0, bd=0)
        header.grid(row=0, column=0, sticky="ew")
        self._draw_gradient(header, 88, "#A78BFA", "#6D3FD8")
        header.bind("<Configure>", lambda e: self._draw_gradient(header, 88, "#A78BFA", "#6D3FD8"))
        header.create_text(22, 26, text="Dublador v2", anchor="w",
                           fill="#FFFFFF", font=("Segoe UI", 24, "bold"))
        header.create_text(24, 62, text="Dublagem com clonagem de voz offline",
                           anchor="w", fill="#EBDDFF", font=("Segoe UI", 12))
        return header

    def _build_ui(self):
        self._build_header()

        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=14, pady=8)
        body.grid_columnconfigure(1, weight=1)
        self.body = body

        row = 0
        self._file_row(body, row, "Audio original", self.var_audio,
                       lambda: self._pick_file("audio"))
        row += 1
        self._file_row(body, row, "Legenda (.srt)", self.var_srt,
                       lambda: self._pick_file("srt"))
        row += 1
        self._file_row(body, row, "Saida (opcional)", self.var_out,
                       lambda: self._pick_file("out"))

        row += 1
        ctk.CTkLabel(body, text="Opcoes de dublagem", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8, pady=(10, 2))
        row += 1

        opts = [
            ("Motor de voz", self.var_engine, ENGINES),
            ("Dispositivo", self.var_device, DEVICES),
            ("Idioma da fala", self.var_lang, LANGS),
        ]
        for i, (label, var, values) in enumerate(opts):
            c = i % 2
            col = c * 2
            if i % 2 == 0:
                body.grid_columnconfigure(col, weight=1)
            cell = ctk.CTkFrame(body, fg_color="transparent")
            cell.grid(row=row, column=col, columnspan=2, sticky="ew", padx=4, pady=2)
            ctk.CTkLabel(cell, text=label, width=110).pack(side="left", padx=(4, 8))
            ctk.CTkComboBox(cell, variable=var, values=values, width=160,
                            state="readonly").pack(side="left")
            if i % 2 == 1:
                row += 1

        nums = [
            ("Temperatura", self.var_temp),
            ("Volume", self.var_volume),
        ]
        for i, (label, var) in enumerate(nums):
            c = i % 2
            col = c * 2
            cell = ctk.CTkFrame(body, fg_color="transparent")
            cell.grid(row=row, column=col, columnspan=2, sticky="ew", padx=4, pady=2)
            ctk.CTkLabel(cell, text=label, width=110).pack(side="left", padx=(4, 8))
            ctk.CTkEntry(cell, textvariable=var, width=160).pack(side="left")
            if i % 2 == 1:
                row += 1
        row += 1

        checks = ctk.CTkFrame(body, fg_color="transparent")
        checks.grid(row=row, column=0, columnspan=3, sticky="w", padx=8, pady=(4, 0))
        checkboxes = [
            ("Ouvir amostras", self.var_samples),
            ("So listar legendas (dry-run)", self.var_dry),
        ]
        for i, (txt, var) in enumerate(checkboxes):
            r, c = divmod(i, 2)
            ctk.CTkCheckBox(checks, text=txt, variable=var).grid(
                row=r, column=c, sticky="w", padx=10, pady=3)
        row += 2

        ctk.CTkLabel(body, text="Dica: 'auto' usa Chatterbox (recomendado, MIT) se "
                                 "instalado, senao XTTS. Temperatura vazia = padrao do "
                                 "motor (xtts 0.3, chatterbox 0.8); menor = voz mais "
                                 "consistente. A voz dublada e esticada para caber "
                                 "exatamente na duracao da fala original.",
                     font=ctk.CTkFont(size=12), text_color="gray70").grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 4))
        row += 1

        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.grid(row=row, column=0, columnspan=3, sticky="ew", padx=4, pady=(6, 2))
        btn_row.grid_columnconfigure(0, weight=1)
        self.btn_start = ctk.CTkButton(btn_row, text="Iniciar Dublagem",
                                       font=ctk.CTkFont(size=15, weight="bold"),
                                       height=40, command=self.start,
                                       fg_color="#1f8a4c", hover_color="#17693a")
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_open = ctk.CTkButton(btn_row, text="Abrir saida", width=110,
                                      command=self.open_outdir, state="disabled",
                                      fg_color="#7a7a7a", hover_color="#5f5f5f")
        self.btn_open.grid(row=0, column=1, padx=(8, 0))
        row += 1

        prog_row = ctk.CTkFrame(body, fg_color="transparent")
        prog_row.grid(row=row, column=0, columnspan=3, sticky="ew", padx=4, pady=(4, 0))
        prog_row.grid_columnconfigure(0, weight=1)
        self.progress = ctk.CTkProgressBar(prog_row, height=14)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.progress.set(0)
        self.lbl_prog = ctk.CTkLabel(prog_row, text="0%", width=46,
                                     font=ctk.CTkFont(size=12))
        self.lbl_prog.grid(row=0, column=1, padx=(8, 0))
        self.btn_pause = ctk.CTkButton(prog_row, text="Pausar", width=80, height=28,
                                       command=self.toggle_pause, state="disabled",
                                       fg_color="#6b5ce0", hover_color="#5749c2",
                                       font=ctk.CTkFont(size=12))
        self.btn_pause.grid(row=0, column=2, padx=(10, 4))
        self.btn_stop = ctk.CTkButton(prog_row, text="Parar", width=80, height=28,
                                      command=self.stop, state="disabled",
                                      fg_color="#b3402e", hover_color="#8f2f20",
                                      font=ctk.CTkFont(size=12))
        self.btn_stop.grid(row=0, column=3, padx=(0, 0))
        row += 1

        samp_row = ctk.CTkFrame(body, fg_color="transparent")
        samp_row.grid(row=row, column=0, columnspan=3, sticky="ew", padx=4, pady=(6, 0))
        ctk.CTkLabel(samp_row, text="Amostras geradas:",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w")
        self.samp_frame = ctk.CTkScrollableFrame(samp_row, height=100)
        self.samp_frame.pack(fill="x", pady=(2, 0))
        ctk.CTkLabel(self.samp_frame, text="(marque 'Ouvir amostras' e inicie a dublagem)",
                     text_color="gray60", font=ctk.CTkFont(size=12)).pack(anchor="w", padx=4, pady=4)

        self.txt = ctk.CTkTextbox(self, wrap="word", font=ctk.CTkFont(size=12),
                                  height=160)
        self.txt.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 10))
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)

    def _file_row(self, parent, row, label, var, browse_cmd):
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(row=row, column=0, columnspan=3, sticky="ew", padx=4, pady=2)
        cell.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(cell, text=label, width=120).grid(row=0, column=0, padx=(4, 8), sticky="w")
        ctk.CTkEntry(cell, textvariable=var).grid(row=0, column=1, sticky="ew", padx=(0, 8))
        ctk.CTkButton(cell, text="...", width=40, command=browse_cmd).grid(row=0, column=2)

    # ------------------------------------------------------------------
    def _pick_file(self, kind):
        if kind == "audio":
            path = filedialog.askopenfilename(
                title="Selecione o audio original",
                filetypes=[("Audio", "*.mp3 *.wav *.m4a *.flac *.ogg *.aac"), ("Todos", "*.*")])
            if path:
                self.var_audio.set(path)
        elif kind == "srt":
            path = filedialog.askopenfilename(
                title="Selecione a legenda (.srt)",
                filetypes=[("Legendas", "*.srt"), ("Todos", "*.*")])
            if path:
                self.var_srt.set(path)
        else:
            path = filedialog.asksaveasfilename(
                title="Saida dublada",
                defaultextension=".mp3",
                filetypes=[("MP3", "*.mp3"), ("WAV", "*.wav")])
            if path:
                self.var_out.set(path)

    def _build_cmd(self):
        cmd = [PYTHON, SCRIPT, "--audio", self.var_audio.get(),
               "--srt", self.var_srt.get()]
        if self.var_out.get():
            cmd += ["--out", self.var_out.get()]
        if self.var_engine.get() != "auto":
            cmd += ["--engine", self.var_engine.get()]
        if self.var_device.get() != "auto":
            cmd += ["--device", self.var_device.get()]
        cmd += ["--language", self.var_lang.get()]
        try:
            cmd += ["--temperature", str(float(self.var_temp.get()))]
        except ValueError:
            pass
        try:
            cmd += ["--volume", str(float(self.var_volume.get()))]
        except ValueError:
            pass
        if self.var_dry.get():
            cmd.append("--dry-run")
        if self.var_samples.get():
            cmd.append("--emit-paths")
        return cmd

    # ------------------------------------------------------------------
    def _run(self, cmd):
        if self.proc and self.proc.poll() is None:
            self.log("Ja existe uma dublagem em andamento.\n")
            return
        if not os.path.exists(self.var_audio.get()):
            self.log(f"[ERRO] Audio nao encontrado: {self.var_audio.get()}\n")
            return
        if not os.path.exists(self.var_srt.get()):
            self.log(f"[ERRO] SRT nao encontrado: {self.var_srt.get()}\n")
            return

        self.txt.delete("0.0", "end")
        self.txt.insert("0.0", " ".join(cmd) + "\n\n")
        self.btn_start.configure(state="disabled", text="Dublando...")
        self.btn_pause.configure(state="normal", text="Pausar")
        self.btn_stop.configure(state="normal")
        self.paused = False
        self.cancelled = False
        self.progress.set(0)
        self.lbl_prog.configure(text="0%")
        self.samples.clear()
        self.sample_rows.clear()
        for child in self.samp_frame.winfo_children():
            child.destroy()
        if HAS_WINSOUND:
            winsound.PlaySound(None, winsound.SND_PURGE)
        self.proc = subprocess.Popen(
            [PYTHON, "-u"] + cmd[1:],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)

        threading.Thread(target=self._reader, daemon=True).start()
        self.after(100, self._poll)

    def start(self):
        self._run(self._build_cmd())

    # ------------------------------------------------------------------
    def toggle_pause(self):
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            import psutil
            p = psutil.Process(self.proc.pid)
            desc = p.children(recursive=True)
        except Exception:
            desc = []
        try:
            if not self.paused:
                try:
                    p.suspend()
                except Exception:
                    pass
                for c in desc:
                    c.suspend()
                self.paused = True
                self.btn_pause.configure(text="Continuar")
                self.log("[PAUSA] Dublagem pausada.\n")
            else:
                for c in reversed(desc):
                    c.resume()
                try:
                    p.resume()
                except Exception:
                    pass
                self.paused = False
                self.btn_pause.configure(text="Pausar")
                self.log("[PAUSA] Dublagem retomada.\n")
        except Exception as ex:
            self.log(f"[ERRO] Nao foi possivel pausar: {ex}\n")

    def stop(self):
        if not self.proc or self.proc.poll() is not None:
            return
        self.cancelled = True
        self.log("[PARADA] Cancelando dublagem...\n")
        try:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
        except Exception:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def _reader(self):
        for line in self.proc.stdout:
            self.output_q.put(line)
        self.output_q.put(None)

    def _poll(self):
        while True:
            try:
                item = self.output_q.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self.proc.wait()
                self.btn_start.configure(state="normal", text="Iniciar Dublagem")
                self.btn_open.configure(state="normal")
                self.btn_pause.configure(state="disabled", text="Pausar")
                self.btn_stop.configure(state="disabled")
                self.paused = False
                if self.cancelled:
                    self.progress.set(0)
                    self.lbl_prog.configure(text="0%")
                    self.log("Cancelado.\n")
                    self.after(200, self.after, 0, lambda: self._flash("red"))
                    return
                self.progress.set(1)
                self.lbl_prog.configure(text="100%")
                self.log("FIM.\n")
                self.after(200, self.after, 0, lambda: self._flash("green"))
                return
            self.log(item)
            m = re.search(r"\[\s*(\d+)/(\d+)\]\s+\d", item)
            if m:
                cur, total = int(m.group(1)), int(m.group(2))
                pct = cur / max(total, 1)
                self.progress.set(pct)
                self.lbl_prog.configure(text=f"{int(pct * 100)}%")
            m2 = re.match(r"^\[SEG\] (\d+)\t(.+?)\t(.*)$", item)
            if m2:
                self.add_sample(int(m2.group(1)), m2.group(2), m2.group(3))

        if self.proc and self.proc.poll() is None:
            self.after(100, self._poll)

    def add_sample(self, idx, path, text):
        if idx in self.sample_rows:
            return
        self.samples[idx] = path
        if not HAS_WINSOUND:
            return
        if hasattr(self, "samp_frame"):
            row = ctk.CTkFrame(self.samp_frame, fg_color="transparent")
            row.pack(fill="x", padx=2, pady=1)
            self.samp_frame._parent_canvas.yview_moveto(1.0)
            btn = ctk.CTkButton(row, text="\u25B6", width=34, command=lambda p=path: self.play_sample(p))
            btn.pack(side="left")
            lbl = ctk.CTkLabel(row, text=f"#{idx}  {text}", font=ctk.CTkFont(size=12), anchor="w")
            lbl.pack(side="left", padx=6, fill="x", expand=True)
            self.sample_rows[idx] = (row, btn, lbl)
            self.after(50, lambda r=row: r.lift())

    def play_sample(self, path):
        if not HAS_WINSOUND or not os.path.exists(path):
            self.log(f"[ERRO] Amostra nao encontrada: {path}\n")
            return
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        self.log(f"[PLAY] #{path.split(os.sep)[-1]}\n")

    def _flash(self, color):
        self.btn_start.configure(fg_color=color)
        self.after(600, lambda: self.btn_start.configure(fg_color=ctk.ThemeManager.theme["CTkButton"]["fg_color"]))

    def log(self, text):
        self.txt.insert("end", text)
        self.txt.see("end")

    def open_outdir(self):
        out = self.var_out.get() or os.path.join(
            os.path.dirname(os.path.abspath(self.var_audio.get())),
            os.path.splitext(os.path.basename(self.var_audio.get()))[0] + "_dublado.mp3")
        d = os.path.dirname(os.path.abspath(out))
        try:
            os.startfile(d)
        except Exception:
            pass


if __name__ == "__main__":
    app = DublarGUI()
    app.mainloop()
