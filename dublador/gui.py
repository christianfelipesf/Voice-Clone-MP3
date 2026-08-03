#!/usr/bin/env python3
"""
Dublador v2 - Interface grafica (customtkinter) - modulo do pacote dublador
==========================================================================
Menu para configurar e rodar o dublar.py sem linha de comando.
Motor de voz: Chatterbox Multilingual V3 (MIT, pt-br).

Modo automatico: basta colar o link do YouTube OU escolher um arquivo
local. O resto (legendas, traducao, transcricao com Whisper) e decidido
sozinho a partir da entrada.

O servidor web (dublador.web) sobe junto com a GUI: ha um botao
"Abrir painel web" para abrir o navegador e o servidor e encerrado
automaticamente ao fechar a janela.

Uso:
    python dublar_gui.py
"""

import os
import re
import queue
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog

import customtkinter as ctk
import psutil

from dublador import preview
from dublador.config import (BASE_DIR, SCRIPT, YT_SCRIPT, PYTHON, CONFIG_PATH,
                             DEVICES, LANGS, WHISPER_MODELS, RESOLUTIONS,
                             BROWSERS, ENGINE_LABELS, PHASES, DEFAULTS,
                             engine_to_value, engine_to_label,
                             load_config, save_config, reset_config)
from dublador.web import WebServer, FLASK_AVAILABLE

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

PURPLE_GRAD = ("#A78BFA", "#6D3FD8")
RED_GRAD = ("#F87171", "#B91C1C")
ACCENT_FILE = "#2f9e44"
ACCENT_YT = "#EF4444"

ENGINES = {label: value for value, label in ENGINE_LABELS.items()}
ENGINE_LABELS_LIST = list(ENGINES)

ctk.set_appearance_mode("light")
_theme_path = os.path.join(BASE_DIR, "dublador_theme.json")
if os.path.exists(_theme_path):
    ctk.set_default_color_theme(_theme_path)


class DublarGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Dublador v2")
        self.geometry("900x720")
        self.minsize(760, 600)
        self.grid_columnconfigure(0, weight=1)

        self.proc = None
        self.output_q = queue.Queue()
        self.samples = {}
        self.sample_rows = {}
        self.paused = False
        self.cancelled = False
        self.theme_dark = False
        self.adv_open = False
        cfg = load_config()

        self.var_mode = tk.StringVar(value=cfg.get("mode", "Arquivo"))
        self.var_file = tk.StringVar(
            value=os.path.join(BASE_DIR, "audio_para_dublar", "audio.mp3"))
        self.var_url = tk.StringVar(value="")
        self.var_out = tk.StringVar(value="")
        self.var_device = tk.StringVar(value=cfg.get("device", "auto"))
        self.var_lang = tk.StringVar(value=cfg.get("lang", "pt"))
        self.var_res = tk.StringVar(value=cfg.get("res", "720"))
        self.var_whisper = tk.StringVar(value=cfg.get("whisper", "small"))
        self.var_srt = tk.StringVar(value="")
        self.var_cookies = tk.StringVar(value=cfg.get("cookies", ""))
        self.var_engine = tk.StringVar(
            value=engine_to_label(cfg.get("engine", "chatterbox")))
        self.var_temp = tk.StringVar(value=cfg.get("temp", ""))
        self.var_volume = tk.StringVar(value=cfg.get("volume", "1.0"))
        self.var_seed = tk.StringVar(value=cfg.get("seed", ""))
        self.var_maxtempo = tk.StringVar(value=cfg.get("maxtempo", ""))
        self.var_samples = tk.BooleanVar(value=False)
        self.var_preview = tk.BooleanVar(value=False)
        self.var_keep = tk.BooleanVar(value=False)
        self.var_dry = tk.BooleanVar(value=False)
        self.grad_colors = PURPLE_GRAD
        self.preview = None

        self._build_ui()
        self._on_mode_change()
        self.theme_dark = bool(cfg.get("theme_dark", False))
        if self.theme_dark:
            ctk.set_appearance_mode("dark")
            self.btn_theme.configure(text="Tema claro")
        self._update_hint()

        self.web = None
        self.web_url = None
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_web_server()

    # ------------------------------------------------------------------
    def _start_web_server(self):
        if not FLASK_AVAILABLE:
            self.log("[WEB] Flask nao instalado; painel web indisponivel. "
                     "Instale com: pip install flask\n")
            self.btn_web.configure(state="disabled",
                                   text="Painel web (sem flask)")
            return
        try:
            self.web = WebServer(host="127.0.0.1", port=5000)
            self.web_url = self.web.start()
            self.log(f"[WEB] Painel web ativo em {self.web_url}\n")
            self.btn_web.configure(state="normal", text="Abrir painel web")
        except Exception as ex:
            self.web = None
            self.web_url = None
            self.log(f"[WEB] Nao foi possivel iniciar o painel web: {ex}\n")
            self.btn_web.configure(state="disabled",
                                   text="Painel web (erro)")

    def open_web(self):
        if not self.web_url:
            self.log("[WEB] Painel web indisponivel.\n")
            return
        webbrowser.open(self.web_url)
        self.log(f"[WEB] Abrindo {self.web_url}\n")

    def _on_close(self):
        if self.web is not None:
            try:
                self.web.stop()
            except Exception:
                pass
        self.destroy()

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
        self.header = tk.Canvas(self, height=88, highlightthickness=0, bd=0)
        self.header.grid(row=0, column=0, sticky="ew")
        self.btn_web = ctk.CTkButton(
            self.header, text="Abrir painel web", width=150, height=30,
            command=self.open_web, font=ctk.CTkFont(size=12),
            fg_color="#1f8a4c", hover_color="#17693a")
        self.header.create_window(0, 0, window=self.btn_web, anchor="ne",
                                  tags=("web_btn",))
        self.btn_theme = ctk.CTkButton(
            self.header, text="Tema escuro", width=110, height=30,
            command=self.toggle_theme, font=ctk.CTkFont(size=12),
            fg_color="#5B3AA8", hover_color="#4A2E8A")
        self.header.create_window(0, 0, window=self.btn_theme, anchor="ne",
                                  tags=("theme_btn",))
        self.header.bind("<Configure>", lambda e: self._draw_header())
        self.header.create_text(22, 26, text="Dublador v2", anchor="w",
                                fill="#FFFFFF", font=("Segoe UI", 24, "bold"))
        self.header.create_text(24, 62, text="Dublagem com clonagem de voz offline",
                                anchor="w", fill="#EBDDFF", font=("Segoe UI", 12))
        return self.header

    def _draw_header(self):
        if not hasattr(self, "header"):
            return
        c1, c2 = self.grad_colors
        self._draw_gradient(self.header, 88, c1, c2)
        w = self.header.winfo_width()
        self.header.coords("theme_btn", w - 8, 44)
        self.header.coords("web_btn", w - 8 - 122, 44)

    def toggle_theme(self):
        self.theme_dark = not self.theme_dark
        ctk.set_appearance_mode("dark" if self.theme_dark else "light")
        self.btn_theme.configure(text="Tema claro" if self.theme_dark else "Tema escuro")
        self._save_config()

    # ------------------------------------------------------------------
    def _build_ui(self):
        self._build_header()

        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=14, pady=8)
        body.grid_columnconfigure(1, weight=1)
        self.body = body

        row = self._build_input_section(body)
        row = self._build_options_section(body, row)
        row = self._build_advanced_section(body, row)
        row = self._build_controls_section(body, row)

        self.txt = ctk.CTkTextbox(self, wrap="word", font=ctk.CTkFont(size=12),
                                  height=160)
        self.txt.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 10))
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)

    def _build_input_section(self, body):
        row = 0
        seg = ctk.CTkSegmentedButton(body, values=["Arquivo", "YouTube"],
                                     variable=self.var_mode,
                                     command=lambda v: self._on_mode_change())
        seg.grid(row=row, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 2))
        row += 1

        self.input_frame = ctk.CTkFrame(body, fg_color="transparent")
        self.input_frame.grid(row=row, column=0, columnspan=3, sticky="ew", padx=4, pady=2)
        self.input_frame.grid_columnconfigure(1, weight=1)

        self.file_row_ui, self.file_entry = self._file_row(
            self.input_frame, 0, "Arquivo (audio/video)", self.var_file,
            lambda: self._pick_input(), on_change=self._update_hint)
        self.url_row_ui, self.url_entry = self._file_row(
            self.input_frame, 0, "Link do YouTube", self.var_url,
            on_change=self._update_hint)
        row += 1

        self.lbl_hint = ctk.CTkLabel(body, text="", text_color="#1f8a4c",
                                     font=ctk.CTkFont(size=12), anchor="w")
        self.lbl_hint.grid(row=row, column=0, columnspan=3, sticky="ew",
                           padx=(8, 4), pady=(0, 2))
        row += 1
        self._file_row(body, row, "Saida (opcional)", self.var_out,
                       lambda: self._pick_out())
        row += 1
        return row + 1

    def _build_options_section(self, body, row):
        ctk.CTkLabel(body, text="Opcoes", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8, pady=(6, 2))
        row += 1

        opts = [
            ("Dispositivo", self.var_device, DEVICES),
            ("Idioma de saida", self.var_lang, LANGS),
            ("Motor de dublagem", self.var_engine, ENGINE_LABELS_LIST),
        ]
        for i, (label, var, values) in enumerate(opts):
            c = i % 2
            col = c * 2
            body.grid_columnconfigure(col, weight=1)
            cell = ctk.CTkFrame(body, fg_color="transparent")
            cell.grid(row=row, column=col, columnspan=2, sticky="ew", padx=4, pady=2)
            ctk.CTkLabel(cell, text=label, width=120).pack(side="left", padx=(4, 8))
            ctk.CTkComboBox(cell, variable=var, values=values, width=160,
                            state="readonly").pack(side="left")
            if i % 2 == 1:
                row += 1
        if len(opts) % 2 == 1:
            row += 1

        self.res_cell = ctk.CTkFrame(body, fg_color="transparent")
        self._res_row = row
        self.res_cell.grid(row=row, column=0, columnspan=2, sticky="ew", padx=4, pady=2)
        ctk.CTkLabel(self.res_cell, text="Resolucao (YouTube)", width=120).pack(
            side="left", padx=(4, 8))
        ctk.CTkComboBox(self.res_cell, variable=self.var_res, values=RESOLUTIONS,
                        width=140, state="readonly").pack(side="left")
        row += 1

        presets = ctk.CTkFrame(body, fg_color="transparent")
        presets.grid(row=row, column=0, columnspan=3, sticky="w", padx=4, pady=(4, 0))
        ctk.CTkButton(presets, text="PC fraco", width=100, height=28,
                      font=ctk.CTkFont(size=12), fg_color="#8a8a8a",
                      hover_color="#6f6f6f",
                      command=self.apply_weak).pack(side="left", padx=(0, 6))
        ctk.CTkButton(presets, text="PC forte", width=100, height=28,
                      font=ctk.CTkFont(size=12), fg_color="#1f8a4c",
                      hover_color="#17693a",
                      command=self.apply_strong).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(presets, text="Presets rapidos (tambem no painel web)",
                     text_color="gray60", font=ctk.CTkFont(size=11)).pack(
            side="left", padx=(10, 0))
        row += 1
        return row

    def _build_advanced_section(self, body, row):
        self.btn_adv = ctk.CTkButton(body, text="Avancado  ▾", width=140, height=28,
                                     font=ctk.CTkFont(size=12),
                                     fg_color="#8a8a8a", hover_color="#6f6f6f",
                                     command=self.toggle_adv)
        self.btn_adv.grid(row=row, column=0, sticky="w", padx=4, pady=(2, 0))
        row += 1
        self.adv = ctk.CTkFrame(body, fg_color="transparent")
        self.adv.grid(row=row, column=0, columnspan=3, sticky="ew", padx=4, pady=(2, 0))
        self.adv.grid_columnconfigure(1, weight=1)

        self._file_row(self.adv, 0, "Legenda .srt (opcional)", self.var_srt,
                       lambda: self._pick_srt())
        ctk.CTkLabel(
            self.adv, text="(vazio = procura um .srt com o mesmo nome do arquivo; "
                           "se nao achar, transcreve com Whisper)",
            text_color="gray60", font=ctk.CTkFont(size=11)).grid(
            row=1, column=0, columnspan=3, sticky="w", padx=(8, 4))
        r = 2

        cell = ctk.CTkFrame(self.adv, fg_color="transparent")
        cell.grid(row=r, column=0, columnspan=3, sticky="ew", padx=4, pady=2)
        ctk.CTkLabel(cell, text="Modelo Whisper", width=120).pack(side="left", padx=(4, 8))
        ctk.CTkComboBox(cell, variable=self.var_whisper, values=WHISPER_MODELS,
                        width=140, state="readonly").pack(side="left")
        ctk.CTkLabel(cell, text="Cookies do navegador", width=140).pack(
            side="left", padx=(16, 8))
        ctk.CTkComboBox(cell, variable=self.var_cookies, values=BROWSERS,
                        width=110, state="readonly").pack(side="left")
        r += 1
        ctk.CTkLabel(
            self.adv, text="(cookies de um navegador logado no YouTube evitam o "
                           "bloqueio 429 nas legendas)",
            text_color="gray60", font=ctk.CTkFont(size=11)).grid(
            row=r, column=0, columnspan=3, sticky="w", padx=(8, 4))
        r += 1

        adv_nums = [
            ("Temperatura", self.var_temp),
            ("Volume", self.var_volume),
            ("Semente", self.var_seed),
            ("Max. esticamento", self.var_maxtempo),
        ]
        for i, (label, var) in enumerate(adv_nums):
            cell = ctk.CTkFrame(self.adv, fg_color="transparent")
            cell.grid(row=r, column=(i % 2) * 2, columnspan=2, sticky="ew",
                      padx=4, pady=2)
            ctk.CTkLabel(cell, text=label, width=90).pack(side="left", padx=(4, 8))
            ctk.CTkEntry(cell, textvariable=var, width=80).pack(side="left")
            if i % 2 == 1:
                r += 1
        if len(adv_nums) % 2 == 1:
            r += 1

        adv_checks = ctk.CTkFrame(self.adv, fg_color="transparent")
        adv_checks.grid(row=r, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 0))
        for i, (txt, var) in enumerate([("Ouvir amostras", self.var_samples),
                                        ("Preview em tempo real (video)", self.var_preview),
                                        ("Manter temporarios", self.var_keep),
                                        ("So listar legendas (dry-run)", self.var_dry)]):
            ctk.CTkCheckBox(adv_checks, text=txt, variable=var).grid(
                row=i, column=0, sticky="w", padx=6, pady=2)
        r += 1

        reset_cell = ctk.CTkFrame(self.adv, fg_color="transparent")
        reset_cell.grid(row=r, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 0))
        ctk.CTkButton(reset_cell, text="Resetar opcoes (padroes)", width=180, height=28,
                      font=ctk.CTkFont(size=12), fg_color="#b3402e",
                      hover_color="#8f2f20",
                      command=self.reset_options).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(reset_cell, text="Volta tudo ao padrao (motor, dispositivo, "
                                       "whisper, resolucao, preview, etc)",
                     text_color="gray60", font=ctk.CTkFont(size=11)).pack(side="left")
        self.adv.grid_remove()
        return row + 1

    def _build_controls_section(self, body, row):
        ctk.CTkLabel(body, text="A voz dublada e esticada para caber exatamente "
                                 "na duracao da fala original.",
                     font=ctk.CTkFont(size=12), text_color="gray70").grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8, pady=(4, 2))
        row += 1

        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.grid(row=row, column=0, columnspan=3, sticky="ew", padx=4, pady=(6, 2))
        btn_row.grid_columnconfigure(0, weight=1)
        self.btn_start = ctk.CTkButton(btn_row, text="Iniciar Dublagem",
                                       font=ctk.CTkFont(size=15, weight="bold"),
                                       height=40, command=self.start,
                                       fg_color="#1f8a4c", hover_color="#17693a")
        self.btn_start_fg = self.btn_start.cget("fg_color")
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
        self.lbl_status = ctk.CTkLabel(prog_row, text="", width=170,
                                       font=ctk.CTkFont(size=11), anchor="e")
        self.lbl_status.grid(row=0, column=1, padx=(8, 0))
        self.lbl_prog = ctk.CTkLabel(prog_row, text="0%", width=40,
                                     font=ctk.CTkFont(size=12))
        self.lbl_prog.grid(row=0, column=2, padx=(8, 0))
        self.btn_pause = ctk.CTkButton(prog_row, text="Pausar", width=80, height=28,
                                       command=self.toggle_pause, state="disabled",
                                       fg_color="#6b5ce0", hover_color="#5749c2",
                                       font=ctk.CTkFont(size=12))
        self.btn_pause.grid(row=0, column=3, padx=(10, 4))
        self.btn_stop = ctk.CTkButton(prog_row, text="Parar", width=80, height=28,
                                      command=self.stop, state="disabled",
                                      fg_color="#b3402e", hover_color="#8f2f20",
                                      font=ctk.CTkFont(size=12))
        self.btn_stop.grid(row=0, column=4, padx=(0, 0))
        row += 1

        samp_row = ctk.CTkFrame(body, fg_color="transparent")
        samp_row.grid(row=row, column=0, columnspan=3, sticky="ew", padx=4, pady=(6, 0))
        ctk.CTkLabel(samp_row, text="Amostras geradas:",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w")
        self.samp_frame = ctk.CTkScrollableFrame(samp_row, height=100)
        self.samp_frame.pack(fill="x", pady=(2, 0))
        ctk.CTkLabel(self.samp_frame, text="(marque 'Ouvir amostras' nas opcoes avancadas)",
                     text_color="gray60", font=ctk.CTkFont(size=12)).pack(anchor="w", padx=4, pady=4)
        return row + 1

    def _file_row(self, parent, row, label, var, browse_cmd=None, on_change=None):
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(row=row, column=0, columnspan=3, sticky="ew", padx=4, pady=2)
        cell.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(cell, text=label, width=120).grid(row=0, column=0, padx=(4, 8), sticky="w")
        entry = ctk.CTkEntry(cell, textvariable=var, border_color="#a0a0a0")
        entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        if on_change is not None:
            entry.bind("<KeyRelease>", lambda e: on_change())
        if browse_cmd is not None:
            ctk.CTkButton(cell, text="...", width=40, command=browse_cmd).grid(row=0, column=2)
        return cell, entry

    def _on_mode_change(self):
        self._set_mode_ui()
        self._apply_mode_theme()
        self._update_hint()

    def _set_mode_ui(self):
        if self.var_mode.get() == "YouTube":
            self.file_row_ui.grid_remove()
            self.url_row_ui.grid()
        else:
            self.url_row_ui.grid_remove()
            self.file_row_ui.grid()

    def _apply_mode_theme(self):
        yt = self._is_youtube()
        self.grad_colors = RED_GRAD if yt else PURPLE_GRAD
        self._draw_header()
        self.btn_theme.configure(
            fg_color="#B3402E" if yt else "#5B3AA8",
            hover_color="#8F2F20" if yt else "#4A2E8A")
        self.url_entry.configure(border_color=ACCENT_YT if yt else "#a0a0a0")
        self.file_entry.configure(border_color=ACCENT_FILE if not yt else "#a0a0a0")
        if yt:
            self.res_cell.grid(row=self._res_row, column=0, columnspan=2,
                               sticky="ew", padx=4, pady=2)
        else:
            self.res_cell.grid_remove()

    def _pick_input(self):
        path = filedialog.askopenfilename(
            title="Selecione o audio/video original",
            filetypes=[("Audio/Video", "*.mp3 *.wav *.m4a *.flac *.ogg *.aac *.mp4 *.mkv *.mov *.avi *.webm"),
                       ("Audio", "*.mp3 *.wav *.m4a *.flac *.ogg *.aac"),
                       ("Video", "*.mp4 *.mkv *.mov *.avi *.webm"),
                       ("Todos", "*.*")])
        if path:
            self.var_file.set(path)
            if not self.var_out.get().strip():
                ext = ".mp4" if path.lower().endswith((".mp4", ".mkv", ".mov", ".avi", ".webm")) else ".mp3"
                base = os.path.splitext(os.path.basename(path))[0]
                self.var_out.set(os.path.join(os.path.dirname(path), base + "_dublado" + ext))
            self._update_hint()

    def _pick_out(self):
        path = filedialog.asksaveasfilename(
            title="Saida dublada",
            defaultextension=".mp3",
            filetypes=[("MP3", "*.mp3"), ("WAV", "*.wav"), ("MP4", "*.mp4")])
        if path:
            self.var_out.set(path)

    def _pick_srt(self):
        path = filedialog.askopenfilename(
            title="Selecione a legenda (.srt)",
            filetypes=[("Legendas", "*.srt"), ("Todos", "*.*")])
        if path:
            self.var_srt.set(path)

    def toggle_adv(self):
        self.adv_open = not self.adv_open
        self.btn_adv.configure(text="Avancado  ▴" if self.adv_open else "Avancado  ▾")
        if self.adv_open:
            self.adv.grid()
        else:
            self.adv.grid_remove()

    def apply_weak(self):
        self.var_device.set("cpu")
        self.var_engine.set(self._engine_label("edge"))
        self.var_whisper.set("tiny")
        self.log("[PRESET] PC fraco: CPU + Edge TTS (leve) + Whisper tiny.\n")

    def apply_strong(self):
        self.var_device.set("auto")
        self.var_engine.set(self._engine_label("chatterbox"))
        self.var_whisper.set("small")
        self.log("[PRESET] PC forte: Chatterbox (clonagem de voz) + Whisper small.\n")

    def reset_options(self):
        mapping = [(self.var_device, "device"), (self.var_lang, "lang"),
                   (self.var_res, "res"), (self.var_whisper, "whisper"),
                   (self.var_volume, "volume"), (self.var_temp, "temp"),
                   (self.var_seed, "seed"), (self.var_maxtempo, "maxtempo"),
                   (self.var_cookies, "cookies")]
        for var, key in mapping:
            var.set(DEFAULTS.get(key, ""))
        self.var_engine.set(self._engine_label(DEFAULTS["engine"]))
        self.var_preview.set(False)
        reset_config()
        self.log("[RESET] Opcoes resetadas para os padroes e config salvo limpo.\n")

    def _engine_label(self, value):
        return engine_to_label(value)

    def _save_config(self):
        data = {
            "device": self.var_device.get(),
            "lang": self.var_lang.get(),
            "res": self.var_res.get(),
            "whisper": self.var_whisper.get(),
            "engine": self._engine_value(),
            "volume": self.var_volume.get(),
            "temp": self.var_temp.get(),
            "seed": self.var_seed.get(),
            "maxtempo": self.var_maxtempo.get(),
            "cookies": self.var_cookies.get(),
            "theme_dark": self.theme_dark,
            "mode": self.var_mode.get(),
            "preview": self.var_preview.get(),
        }
        save_config(data)

    # ------------------------------------------------------------------
    def _is_youtube(self):
        return self.var_mode.get() == "YouTube"

    def _update_hint(self):
        if self._is_youtube():
            self.lbl_hint.configure(
                text="YouTube: procura legenda em pt, traduz ou transcreve automaticamente.")
            return
        inp = self.var_file.get().strip()
        if not inp:
            self.lbl_hint.configure(text="Escolha um arquivo de audio/video local.")
            return
        srt = self._detect_srt(inp)
        if srt:
            self.lbl_hint.configure(text=f"Arquivo local: usando a legenda {os.path.basename(srt)}.")
        else:
            self.lbl_hint.configure(
                text="Arquivo local: sem legenda - detecta o idioma, transcreve e traduz para pt.")

    def _detect_srt(self, audio_path):
        if not audio_path or not os.path.exists(audio_path):
            return None
        base = os.path.splitext(audio_path)[0]
        cand = base + ".srt"
        if os.path.exists(cand):
            return cand
        return None

    def _build_cmd(self):
        if self._is_youtube():
            return self._build_cmd_yt(self.var_url.get().strip())
        return self._build_cmd_file(self.var_file.get().strip())

    def _add_common_yt(self, cmd):
        if self.var_out.get():
            cmd += ["--out", self.var_out.get()]
        if self.var_device.get() != "auto":
            cmd += ["--device", self.var_device.get()]
        cmd += ["--whisper-model", self.var_whisper.get()]
        cmd += ["--engine", self._engine_value()]
        if self.var_cookies.get().strip():
            cmd += ["--cookies-from-browser", self.var_cookies.get().strip()]
        self._add_numeric(cmd, "--temperature", self.var_temp.get())
        self._add_numeric(cmd, "--volume", self.var_volume.get())
        self._add_numeric(cmd, "--seed", self.var_seed.get())
        self._add_numeric(cmd, "--max-tempo", self.var_maxtempo.get())
        self._add_flags(cmd)

    def _build_cmd_yt(self, url):
        cmd = [PYTHON, YT_SCRIPT, "--url", url,
               "--resolution", self.var_res.get(),
               "--language", self.var_lang.get()]
        self._add_common_yt(cmd)
        return cmd

    def _build_cmd_file(self, audio):
        cmd = [PYTHON, SCRIPT, "--audio", audio, "--language", self.var_lang.get(),
               "--engine", self._engine_value()]
        srt = self.var_srt.get().strip() or self._detect_srt(audio)
        if srt and os.path.exists(srt):
            cmd += ["--srt", srt]
        else:
            cmd += ["--whisper-model", self.var_whisper.get()]
        if self.var_out.get():
            cmd += ["--out", self.var_out.get()]
        if self.var_device.get() != "auto":
            cmd += ["--device", self.var_device.get()]
        self._add_numeric(cmd, "--temperature", self.var_temp.get())
        self._add_numeric(cmd, "--volume", self.var_volume.get())
        self._add_numeric(cmd, "--seed", self.var_seed.get())
        self._add_numeric(cmd, "--max-tempo", self.var_maxtempo.get())
        self._add_flags(cmd)
        return cmd

    def _add_numeric(self, cmd, flag, value):
        if not value.strip():
            return
        try:
            if flag == "--seed":
                cmd += [flag, str(int(value))]
            else:
                cmd += [flag, str(float(value))]
        except ValueError:
            self.log(f"[AVISO] Valor invalido para {flag} ('{value}') ignorado.\n")

    def _add_flags(self, cmd):
        if self.var_samples.get() or self.var_preview.get():
            cmd.append("--emit-paths")
        if self.var_keep.get():
            cmd.append("--keep-parts")
        if self.var_dry.get():
            cmd.append("--dry-run")

    def _engine_value(self):
        return engine_to_value(self.var_engine.get())

    # ------------------------------------------------------------------
    def _run(self, cmd):
        if self.proc and self.proc.poll() is None:
            self.log("Ja existe uma dublagem em andamento.\n")
            return
        if self._is_youtube():
            url = self.var_url.get().strip()
            if not url:
                self.log("[ERRO] Cole o link do YouTube.\n")
                return
            if not url.startswith(("http://", "https://")):
                self.log(f"[ERRO] Link invalido: {url}\n")
                return
        else:
            inp = self.var_file.get().strip()
            if not inp:
                self.log("[ERRO] Escolha um arquivo de audio/video.\n")
                return
            if not os.path.exists(inp):
                self.log(f"[ERRO] Arquivo nao encontrado: {inp}\n")
                return
        if shutil.which("ffmpeg") is None:
            self.log("[ERRO] ffmpeg nao encontrado no PATH. "
                     "Instale o ffmpeg e adicione-o ao PATH.\n")
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
        self.preview = None
        for child in self.samp_frame.winfo_children():
            child.destroy()
        if HAS_WINSOUND:
            winsound.PlaySound(None, winsound.SND_PURGE)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        self.proc = subprocess.Popen(
            [PYTHON, "-u"] + cmd[1:],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=env)

        if self.var_preview.get() and not self.var_dry.get() \
                and not self._is_youtube():
            inp = self.var_file.get().strip()
            if preview.has_video_stream(inp):
                self._start_preview(inp)
            else:
                self.log("[AVISO] Preview em tempo real e so para videos "
                         "(com imagem); sem preview neste arquivo.\n")

        self.lbl_status.configure(text="Iniciando...")
        self._save_config()
        threading.Thread(target=self._reader, daemon=True).start()
        self.after(100, self._poll)

    def _start_preview(self, video_path):
        if self.preview is not None:
            return
        self.preview = preview.LivePreview(
            video_path, log=self.log, work_dir=tempfile.gettempdir())
        self.preview.prepare_async()
        self.preview.launch()

    def _close_preview(self):
        if self.preview is not None:
            self.preview.close()
            self.preview = None

    def start(self):
        self._run(self._build_cmd())

    # ------------------------------------------------------------------
    def toggle_pause(self):
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            p = psutil.Process(self.proc.pid)
            desc = p.children(recursive=True)
        except Exception:
            p = None
            desc = []
        try:
            if not self.paused:
                if p is not None:
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
                if p is not None:
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
                self._close_preview()
                if self.cancelled:
                    self.progress.set(0)
                    self.lbl_prog.configure(text="0%")
                    self.lbl_status.configure(text="Cancelado")
                    self.log("Cancelado.\n")
                    self.after(200, self.after, 0, lambda: self._flash("red"))
                    return
                self.progress.set(1)
                self.lbl_prog.configure(text="100%")
                self.lbl_status.configure(text="Concluido")
                self.log("FIM.\n")
                self.after(200, self.after, 0, lambda: self._flash("green"))
                return
            self.log(item)
            self._update_status(item)
            m = re.search(r"\[PROGRESS\]\s+(\d+)/(\d+)", item)
            if m:
                cur, total = int(m.group(1)), int(m.group(2))
                pct = cur / max(total, 1)
                self.progress.set(pct)
                self.lbl_prog.configure(text=f"{int(pct * 100)}%")
            mv = re.match(r"^\[VIDEO\] (.+)$", item)
            if mv:
                vid = mv.group(1).strip()
                self.log(f"Video baixado: {vid}\n")
                if self.var_preview.get() and not self.var_dry.get():
                    self._start_preview(vid)
            m2 = re.match(r"^\[SEG\] (\d+)\t(.+?)\t([\d.]+)\t([\d.]+)\t(.*)$", item)
            if m2:
                idx = int(m2.group(1))
                path = m2.group(2)
                start = float(m2.group(3))
                end = float(m2.group(4))
                text = m2.group(5)
                if self.preview is not None:
                    self.preview.add_segment(start, end, path)
                self.add_sample(idx, path, text)

        if self.proc and self.proc.poll() is None:
            self.after(100, self._poll)

    def _update_status(self, item):
        for key, label in PHASES:
            if key in item:
                self.lbl_status.configure(text=label)
                return
        if "[OK]" in item:
            self.lbl_status.configure(text="Concluido")
            return
        m = re.search(r"\[PROGRESS\]\s+(\d+)/(\d+)", item)
        if m:
            self.lbl_status.configure(text=f"Dublagem {m.group(1)}/{m.group(2)}")
            return
        if "[SRT]" in item or "Legenda traduzida" in item:
            self.lbl_status.configure(text="Preparando legendas...")

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
        self.after(600, lambda: self.btn_start.configure(fg_color=self.btn_start_fg))

    def log(self, text):
        self.txt.insert("end", text)
        self.txt.see("end")

    def open_outdir(self):
        if self._is_youtube():
            try:
                os.startfile(os.getcwd())
            except Exception:
                pass
            return
        inp = self.var_file.get().strip()
        if not inp:
            return
        out = self.var_out.get() or os.path.join(
            os.path.dirname(os.path.abspath(inp)),
            os.path.splitext(os.path.basename(inp))[0] + "_dublado.mp3")
        d = os.path.dirname(os.path.abspath(out))
        try:
            os.startfile(d)
        except Exception:
            pass


def main():
    app = DublarGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
