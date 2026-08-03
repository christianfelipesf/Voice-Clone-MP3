#!/usr/bin/env python3
"""
Dublador do YouTube
===================
Baixa um video do YouTube na resolucao escolhida e dubla para portugues
(ou outro idioma) usando o dublar.py (clonagem de voz offline).

Fluxo automatico de legendas:
    1. Se o video tiver legenda em portugues (manual)  -> usa direto
    2. Se tiver legenda automatica em portugues        -> usa direto
    3. Se tiver legenda em OUTRO idioma                -> traduz para pt
    4. Se nao tiver nenhuma legenda                    -> transcreve com
       Whisper e traduz (modo automatico do dublar.py)

Uso:
    python dublar_yt.py --url "https://youtu.be/XXXX" --resolution 720
    python dublar_yt.py --url "..." --resolution 1080 --language pt --device cuda
    python dublar_yt.py --url "..." --list-subs          # so lista as legendas

Requisitos:
    pip install -r requirements.txt
    pip install yt-dlp
    ffmpeg no PATH
"""

import os
import re
import sys
import time
import glob
import argparse
import shutil
import subprocess
import tempfile

import yt_dlp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DUBLAR = os.path.join(BASE_DIR, "dublar.py")
RESOLUTIONS = ["best", "144", "240", "360", "480", "720", "1080"]


def force_utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def sanitize(name):
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .") or "video"


def get_info(url):
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    with _ydl(opts) as ydl:
        return ydl.extract_info(url, download=False)


def choose_subtitle(info):
    """Escolhe a melhor legenda: manual pt > auto pt > manual outro > auto outro."""
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    def pick(source):
        pt = sorted(k for k in source if k.lower().startswith("pt"))
        if pt:
            return pt[0]
        others = sorted(k for k in source if k.lower() != "origin")
        return others[0] if others else None

    for source, is_auto in ((subs, False), (auto, True)):
        lang = pick(source)
        if lang:
            return lang, is_auto, source
    return None, None, None


def print_subs(info):
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    if not subs and not auto:
        print("  Nenhuma legenda disponivel.")
        return
    if subs:
        print("  Legendas (manuais):")
        for k in sorted(subs):
            tag = " <- PT" if k.lower().startswith("pt") else ""
            print(f"    {k}{tag}")
    if auto:
        print("  Legendas automaticas:")
        for k in sorted(auto):
            tag = " <- PT" if k.lower().startswith("pt") else ""
            print(f"    {k}{tag}")


def _cookie_opts(cookies, cookies_browser):
    opts = {}
    if cookies:
        opts["cookiefile"] = cookies
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser, None, None, None)
    return opts


def _ydl(opts):
    """Cria um YoutubeDL. Se os cookies nao puderem ser lidos (ex: Edge/Chrome
    com criptografia DPAPI que o yt-dlp nao decifra), cai sem cookies."""
    has_cookies = "cookiefile" in opts or "cookiesfrombrowser" in opts
    if not has_cookies:
        return yt_dlp.YoutubeDL(opts)
    try:
        ydl = yt_dlp.YoutubeDL(opts)
        _ = ydl.cookiejar
        return ydl
    except Exception as ex:
        print(f"  Aviso: nao foi possivel usar cookies ({ex}). "
              f"Continuando sem cookies.")
        opts = {k: v for k, v in opts.items()
                if k not in ("cookiefile", "cookiesfrombrowser")}
        return yt_dlp.YoutubeDL(opts)


def download_subtitle(url, lang, auto, work_dir, cookies=None, cookies_browser=None):
    """Baixa a legenda do idioma escolhido. O YouTube limita o endpoint de
    legendas (HTTP 429) por IP: o cliente web tenta com backoff crescente e,
    se persistir, usa clientes alternativos (tv/ios/mweb) que usam endpoints
    diferentes. Com cookies do navegador logado o 429 praticamente nao ocorre."""
    outtmpl = os.path.join(work_dir, "sub.%(ext)s")
    base_opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [lang],
        "subtitlesformat": "srt/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    base_opts.update(_cookie_opts(cookies, cookies_browser))
    clients = [None, "tv", "ios", "mweb"]
    last = None
    for client in clients:
        opts = dict(base_opts)
        if client:
            opts["extractor_args"] = {"youtube": {"player_client": [client]}}
        attempts = 3 if client is None else 1
        ok = False
        for attempt in range(attempts):
            try:
                with _ydl(opts) as ydl:
                    ydl.download([url])
                ok = True
                break
            except Exception as ex:
                last = ex
                if attempt < attempts - 1:
                    wait = 20 * (attempt + 1)
                    print(f"  Aviso: falha ao baixar a legenda ({ex}); "
                          f"tentando de novo em {wait}s...")
                    time.sleep(wait)
        if not ok:
            continue
        found = glob.glob(os.path.join(work_dir, "sub.*.srt"))
        if found:
            return found[0]
        vtt = glob.glob(os.path.join(work_dir, "sub.*.vtt"))
        if vtt:
            srt = os.path.join(work_dir, "sub.srt")
            subprocess.run(["ffmpeg", "-y", "-i", vtt[0], srt],
                           capture_output=True, text=True)
            if os.path.exists(srt):
                return srt
    print(f"  Aviso: nao foi possivel baixar a legenda ({last}). "
          f"Vou gerar a transcricao com Whisper.")
    return None


def download_video(url, resolution, work_dir, cookies=None, cookies_browser=None):
    outtmpl = os.path.join(work_dir, "video.%(ext)s")
    if resolution == "best":
        fmt = "bv*+ba/b"
    else:
        fmt = (f"bv*[height<={resolution}]+ba[ext=m4a]/b[height<={resolution}]"
               f"/bv*+ba/b")
    opts = {
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    opts.update(_cookie_opts(cookies, cookies_browser))
    print(f"  Baixando video ({resolution}p ou melhor disponivel)...")
    with _ydl(opts) as ydl:
        ydl.download([url])
    vids = [p for p in glob.glob(os.path.join(work_dir, "video.*"))
            if not p.lower().endswith((".vtt", ".srt", ".json", ".info.json"))]
    if not vids:
        raise RuntimeError("Falha ao baixar o video.")
    vids.sort(key=lambda p: os.path.getsize(p), reverse=True)
    return vids[0]


def translate_srt(src, target_lang, out_path):
    import dublar
    entries = dublar.parse_srt(src)
    if not entries:
        return None
    for e in entries:
        e["text"] = dublar.translate_text(e["text"], target=target_lang)
    dublar.write_srt(entries, out_path)
    return out_path


def run_dublar(video_path, srt_path, language, extra):
    cmd = [sys.executable, DUBLAR, "--audio", video_path,
           "--language", language]
    if extra.get("engine"):
        cmd += ["--engine", extra["engine"]]
    if srt_path:
        cmd += ["--srt", srt_path]
    else:
        cmd += ["--whisper-model", extra.get("whisper_model", "small")]
    if extra.get("out"):
        cmd += ["--out", extra["out"]]
    for flag, val in (("--device", extra.get("device")),
                      ("--temperature", extra.get("temperature")),
                      ("--volume", extra.get("volume")),
                      ("--max-tempo", extra.get("max_tempo")),
                      ("--seed", extra.get("seed"))):
        if val is not None:
            cmd += [flag, str(val)]
    for flag in ("--dry-run", "--keep-parts", "--emit-paths"):
        if extra.get(flag.lstrip("-").replace("-", "_")):
            cmd.append(flag)
    print("  " + " ".join(cmd) + "\n")
    return subprocess.run(cmd).returncode


def main():
    force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="Baixa um video do YouTube e dubla para portugues "
                    "com clonagem de voz (usa dublar.py)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemplo:\n"
            "  python dublar_yt.py --url \"https://youtu.be/XXXX\"\n"
            "  python dublar_yt.py --url \"...\" --resolution 1080 --language pt\n"
            "  python dublar_yt.py --url \"...\" --list-subs\n"
        ),
    )
    ap.add_argument("--url", required=True, help="Link do video do YouTube")
    ap.add_argument("--resolution", default="720",
                    help="Resolucao desejada do video (best/144/240/360/480/720/1080; "
                         "padrao: 720)")
    ap.add_argument("--language", default="pt", help="Idioma de saida (padrao: pt)")
    ap.add_argument("--out", default=None,
                    help="Saida .mp4 (padrao: <titulo>_dublado.mp4 na pasta atual)")
    ap.add_argument("--list-subs", action="store_true",
                    help="So lista as legendas disponiveis e sai")
    ap.add_argument("--cookies", default=None,
                    help="Arquivo de cookies (formato Netscape) para evitar o "
                         "bloqueio de legendas do YouTube (HTTP 429)")
    ap.add_argument("--cookies-from-browser", default=None,
                    help="Usar cookies do navegador instalado para evitar o 429 "
                         "(edge, chrome, firefox, brave...)")
    ap.add_argument("--workdir", default=None,
                    help="Pasta para os downloads temporarios (padrao: pasta temporaria)")
    ap.add_argument("--device", default=None, help="cuda ou cpu")
    ap.add_argument("--temperature", type=float, default=None)
    ap.add_argument("--volume", type=float, default=None)
    ap.add_argument("--max-tempo", type=float, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--whisper-model", default="small",
                    help="Modelo do Whisper quando nao houver legenda (padrao: small)")
    ap.add_argument("--engine", default="chatterbox",
                    help="Motor de dublagem: chatterbox (clonagem, padrao) "
                         "ou edge (Edge TTS, leve)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-parts", action="store_true")
    ap.add_argument("--emit-paths", action="store_true")
    args = ap.parse_args()

    if args.resolution not in RESOLUTIONS:
        sys.exit(f"[ERRO] Resolucao invalida: {args.resolution}. "
                 f"Use: {', '.join(RESOLUTIONS)}")

    print("=" * 60)
    print("  Dublador do YouTube")
    print("=" * 60)
    print("  Obtendo informacoes do video...")
    try:
        info = get_info(args.url)
    except Exception as ex:
        sys.exit(f"[ERRO] Nao foi possivel acessar o video: {ex}")
    title = info.get("title", "video")
    dur = info.get("duration")
    print(f"  Titulo: {title}")
    if dur:
        print(f"  Duracao: {dur // 60}m{dur % 60:02d}s")
    print_subs(info)

    if args.list_subs:
        return

    lang, is_auto, _ = choose_subtitle(info)
    if lang:
        print(f"  Legenda escolhida: '{lang}' "
              f"({'automatica' if is_auto else 'manual'})")
    else:
        print("  Nenhuma legenda encontrada - a transcricao sera gerada "
              "com Whisper.")

    work_dir = args.workdir or tempfile.mkdtemp(prefix="dub_yt_")
    os.makedirs(work_dir, exist_ok=True)
    print(f"  Pasta de trabalho: {work_dir}")
    try:
        print(f"\n[1/3] Baixando video ({args.resolution})...")
        video_path = download_video(args.url, args.resolution, work_dir,
                                    args.cookies, args.cookies_from_browser)
        print(f"  Video: {video_path}")
        print(f"[VIDEO] {video_path}")

        srt_path = None
        print(f"\n[2/3] Legendas...")
        if lang:
            time.sleep(1)
            sub_path = download_subtitle(args.url, lang, is_auto, work_dir,
                                         args.cookies, args.cookies_from_browser)
            if sub_path:
                target = lang
                need_translate = not lang.lower().startswith("pt")
                if need_translate and args.language.lower() != lang.lower():
                    out_srt = os.path.join(work_dir, "traduzido.srt")
                    print(f"  Traduzindo legenda '{lang}' para "
                          f"'{args.language}'...")
                    srt_path = translate_srt(sub_path, args.language, out_srt)
                    target = args.language
                else:
                    srt_path = sub_path
                used_direct = (srt_path == sub_path)
                print(f"  Legenda {'usada direto' if used_direct else 'traduzida'}: "
                      f"{srt_path}")
            else:
                print("  Nao foi possivel baixar a legenda - usando Whisper.")

        extra = {
            "out": args.out or os.path.join(os.getcwd(),
                                            sanitize(title) + "_dublado.mp4"),
            "whisper_model": args.whisper_model,
            "engine": args.engine,
            "device": args.device,
            "temperature": args.temperature,
            "volume": args.volume,
            "max_tempo": args.max_tempo,
            "seed": args.seed,
            "dry_run": args.dry_run,
            "keep_parts": args.keep_parts,
            "emit_paths": args.emit_paths,
        }
        print(f"\n[3/3] Dublagem...")
        code = run_dublar(video_path, srt_path, args.language, extra)
        sys.exit(code)
    finally:
        if not args.keep_parts and os.path.isdir(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
