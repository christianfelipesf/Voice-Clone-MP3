#!/usr/bin/env python3
"""
Dublador - pacote modular de dublagem com clonagem de voz
=========================================================
Modulos:
    config   - constantes, caminhos e utilitarios compartilhados
    core     - motor de dublagem (dublar.py)
    youtube  - baixa do YouTube e dubla (dublar_yt.py)
    preview  - player em tempo real sincronizado (GUI e web)
    web      - painel web Flask (create_app + WebServer embutivel)
    gui      - interface grafica customtkinter (com painel web embutido)

Os scripts na raiz (dublar.py, dublar_yt.py, dublar_gui.py,
dublar_web.py, preview.py) sao apenas "wrappers" finos que importam
deste pacote, mantendo os comandos antigos funcionando.
"""

__version__ = "2.1.0"
