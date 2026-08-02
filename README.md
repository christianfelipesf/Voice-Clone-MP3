# Dublador

Dublagem offline com clonagem de voz usando o **Chatterbox Multilingual V3**
(Resemble AI, licenca MIT, 100% offline). A voz dublada e esticada
(rubberband) para ter a mesma duracao da fala original de cada legenda.
Funciona com **audio (mp3/wav/...) e video (mp4/mkv/mov/avi/webm)** —
no video a imagem e mantida e so o audio e substituido.

## Requisitos

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/download.html) instalado e no PATH
- Dependencias: `pip install -r requirements.txt`
  (primeira execucao baixa ~2 GB de pesos da HuggingFace)

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
```

## Opcoes principais

| Opcao           | Descricao                                                        |
|-----------------|------------------------------------------------------------------|
| `--audio`       | Audio/video no idioma original (obrigatorio; suporta mp4, mkv...)|
| `--srt`         | Legenda da traducao `.srt` (obrigatorio)                         |
| `--out`         | Saida `.mp3`/`.wav` ou `.mp4`/`.mkv` (padrao: `<audio>_dublado.mp3` ou `.mp4` se video)|
| `--device`      | `cuda` ou `cpu` (padrao: cuda se disponivel)                     |
| `--language`    | Idioma da fala (padrao: `pt`; pt, en, es, fr, de, it, zh, ja, ko)|
| `--temperature` | Aleatoriedade (menor = mais consistente; padrao: 0.8)            |
| `--volume`      | Ganho da voz dublada (padrao: 1.0)                               |
| `--seed`        | Semente base para reprodutibilidade (cada legenda usa seed+idx)  |
| `--dry-run`     | So lista as legendas e sai                                       |
| `--keep-parts`  | Mantem os arquivos intermediarios da pasta de trabalho           |
| `--emit-paths`  | Lista as amostras geradas e as mantem (usado pela GUI)           |

## Estrutura

- `dublar.py` - motor de dublagem (CLI)
- `dublar_gui.py` - menu grafico (customtkinter)
- `audio_para_dublar/` - pasta de exemplo
