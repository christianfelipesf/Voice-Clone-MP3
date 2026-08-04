# Dublador

Dublagem automatica de audio/video. A voz dublada e esticada (rubberband)
para ter a mesma duracao da fala original de cada legenda. Funciona com
**audio (mp3/wav/...) e video (mp4/mkv/mov/avi/webm)** — no video a
imagem e mantida e so o audio e substituido.

**Motores de voz:** voce pode escolher entre
- `edge` (**padrao**) — **Edge TTS** (Microsoft, leve e rapido, online),
  sem clonagem, usa uma voz neural padrao do idioma;
- `chatterbox` — **Chatterbox Multilingual V3** (Resemble AI, MIT, 100%
  offline) com clonagem de voz a partir do audio original.

A configuracao padrao prioriza velocidade: `edge` + Whisper
`distil-large-v3` (~6x mais rapido que `small` com perda minima de
qualidade, `int8` + `beam_size=1`). Para clonagem de voz, troque o motor
para `chatterbox` na GUI/web.

**Sem .srt?** Tambem funciona no **modo automatico**: o script detecta o
idioma da midia, transcreve cada fala com timestamps (faster-whisper) e
traduz para o idioma de saida (padrao `pt`) via Google Translate — depois
dubla como se fosse um .srt gerado. A transcricao e offline; a traducao
precisa de internet.

**Do YouTube?** Basta colar o link (na GUI ou via `dublar_yt.py`). O script
baixa o video na resolucao escolhida e procura legendas nesta ordem:
1. legenda em portugues (manual) → usa direto;
2. legenda automatica em portugues → usa direto;
3. legenda em outro idioma → traduz para o idioma de saida;
4. nenhuma legenda → transcreve com Whisper e traduz.

**Preview em tempo real (GUI):** marque *"Preview em tempo real (video)"* nas
opcoes avancadas e um player (ffplay) abre mostrando o video sincronizado
com a geracao: as partes ainda nao dubladas tocam o audio original e, a
cada trecho dublado pronto, ele entra na posicao correta da linha do tempo
— o video espera enquanto um trecho esta sendo gerado. O YouTube limita o
download de legendas por IP (HTTP 429); na GUI escolha o navegador em
*"Cookies do navegador"* para evitar o bloqueio.

**Painel web junto com a GUI:** ao abrir o menu grafico (`dub.bat` ou
`python dublar_gui.py`), o servidor web sobe automaticamente em segundo
plano. Basta clicar em **"Abrir painel web"** (no topo da janela) para abrir
o navegador em `http://127.0.0.1:5000`. O servidor e encerrado sozinho ao
fechar a GUI. Tambem roda de forma independente:

```
python dublar_web.py                 # http://127.0.0.1:5000
python dublar_web.py --port 8080     # porta personalizada
```

O painel web tem visual leve (Pico.css) e roda o mesmo motor: upload de
arquivo pelo browser, modo YouTube, preview do video em tempo real (stream
MPEG-TS + mpegts.js, sem precisar de ffplay). O preview usa o ffmpeg
instalado no sistema: em builds modernos o video toca em tempo real
enquanto dubla; em builds antigos (ex.: de 2013) o stream e entregue
quando a dublagem termina, e mesmo assim o video dublado completo fica
disponivel.

## Requisitos

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/download.html) instalado e no PATH
- Dependencias: `pip install -r requirements.txt`
  (primeira execucao baixa ~2 GB de pesos da HuggingFace do Chatterbox;
  com motor padrao `edge` nao baixa nada)

## Uso

Menu grafico (recomendado):

```
dub.bat
```

ou

```
python dublar_gui.py
```

Linha de comando:

```
python dublar.py --audio "audio_para_dublar\audio.mp3" --srt "audio_para_dublar\audio.srt"
python dublar.py --audio a.mp3 --srt a.srt --out a_dublado.mp3 --device cuda --volume 1.2
python dublar.py --audio filme.mp4 --srt filme.srt          # gera filme_dublado.mp4
python dublar.py --audio a.mp3                              # modo automatico (sem .srt)
python dublar.py --audio a.mp3 --gen-srt                    # modo automatico + salva o .srt
python dublar.py --audio a.mp3 --engine chatterbox         # clonagem de voz (offline, mais pesado)
python dublar.py --audio a.mp3 --whisper-model large-v3     # modo automatico mais preciso
python dublar_yt.py --url "https://youtu.be/XXXX"           # baixa do YouTube e dubla
python dublar_yt.py --url "..." --resolution 1080           # escolhe a resolucao
python dublar_yt.py --url "..." --list-subs                 # lista as legendas do video
```

## Opcoes principais

| Opcao           | Descricao                                                        |
|-----------------|------------------------------------------------------------------|
| `--audio`       | Audio/video no idioma original (obrigatorio; suporta mp4, mkv...)|
| `--srt`         | Legenda da traducao `.srt` (opcional). Omita para o modo automatico (detecta idioma + transcreve + traduz)|
| `--whisper-model`| Modelo do Whisper no modo automatico: `tiny/base/small/medium/large-v3/distil-large-v3/distil-medium.en` (padrao: `distil-large-v3`)|
| `--engine`      | Motor de voz: `edge` (Edge TTS, leve, padrao) ou `chatterbox` (clonagem, offline)|
| `--gen-srt`     | No modo automatico, salva o `.srt` gerado ao lado do audio      |
| `--out`         | Saida `.mp3`/`.wav` ou `.mp4`/`.mkv` (padrao: `<audio>_dublado.mp3` ou `.mp4` se video)|
| `--device`      | `cuda` ou `cpu` (padrao: cuda se disponivel)                     |
| `--language`    | Idioma da fala e da traducao (padrao: `pt`; pt, en, es, fr, de, it, zh, ja, ko)|
| `--temperature` | Aleatoriedade (menor = mais consistente; padrao: 0.8)            |
| `--volume`      | Ganho da voz dublada (padrao: 1.0)                               |
| `--seed`        | Semente base para reprodutibilidade (cada legenda usa seed+idx)  |
| `--dry-run`     | So lista as legendas e sai                                       |
| `--keep-parts`  | Mantem os arquivos intermediarios da pasta de trabalho           |
| `--emit-paths`  | Lista as amostras geradas e as mantem (usado pela GUI)           |

### dublar_yt.py (YouTube)

| Opcao           | Descricao                                                        |
|-----------------|------------------------------------------------------------------|
| `--url`         | Link do video do YouTube (obrigatorio)                           |
| `--resolution`  | `best/144/240/360/480/720/1080` (padrao: `720`)                  |
| `--language`    | Idioma de saida da dublagem e da traducao (padrao: `pt`)         |
| `--whisper-model`| Modelo do Whisper usado quando nao ha legenda (padrao: `distil-large-v3`) |
| `--engine`      | Motor de voz: `edge` (Edge TTS, padrao) ou `chatterbox` (clonagem) |
| `--list-subs`   | So lista as legendas disponiveis e sai                           |
| `--out`         | Saida `.mp4` (padrao: `<titulo>_dublado.mp4` na pasta atual)     |
| `--cookies`     | Arquivo de cookies (formato Netscape) para evitar o HTTP 429     |
| `--cookies-from-browser` | Usar cookies do navegador (edge, chrome, firefox...)  |

## Estrutura

O codigo e organizado no **pacote `dublador/`** (modular). Os scripts na
raiz sao apenas "wrappers" finos que importam o pacote — os comandos
antigos continuam iguais.

```
dublador/
    __init__.py     - metadados do pacote
    config.py       - constantes, caminhos e utilitarios compartilhados
    core.py         - motor de dublagem (dublar.py)
    youtube.py      - baixa do YouTube e dubla (dublar_yt.py)
    preview.py      - player em tempo real (GUI e web)
    web.py          - painel web Flask (create_app + WebServer embutivel)
    gui.py          - menu grafico customtkinter (sobe o web server junto)
static/             - frontend do painel web (Pico.css/mpegts.js)
audio_para_dublar/  - pasta de exemplo
```
