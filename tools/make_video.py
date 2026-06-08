"""Monta um MP4 LEGENDADO a partir dos frames capturados pelo bot (RPA_RECORD_DIR).

Cada frame NN_passo.png vira ~N segundos de video, com uma faixa de legenda
explicando o passo (queimada na imagem via PIL). Alem disso gera um .srt e, se
houver ffmpeg, EMBUTE o .srt como faixa de legenda (soft subs) dentro do MP4 —
um unico arquivo com a legenda unida. Sem ffmpeg, mantem a legenda queimada + .srt.

Uso:
  python tools/make_video.py --frames-dir C:\rpa\rec --out C:\rpa\video-execucao\rpa-execucao.mp4
"""

import os
import glob
import shutil
import argparse
import subprocess

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 720
BANNER_H = 96
FPS = 30

# Legendas amigaveis por passo (casa com os nomes NN_<passo>.png do bot).
# Gravacao comeca DESDE O LOGIN no Dominio.
LEGENDS = {
    "login_dominio": "Login no Dominio Web (dominioweb.com.br)",
    "login_email": "Login: informando o e-mail",
    "login_senha": "Login: informando a senha",
    "plugin_abrindo": "Plugin TRComputerPlugin abrindo o app desktop",
    "login_web": "Login concluido - app Dominio aberto",
    "lista_programas": "Lista de Programas: abrindo Contabilidade",
    "login_modulo": "Login do modulo Contabilidade",
    "selecionar_empresa": "Selecao da empresa (F8 + codigo)",
    "navegar_importacao": "Navegando para a importacao",
    "importar_arquivo": "Importando o arquivo",
    "inicio": "Inicio - area de trabalho da VM",
}


def _ts(seconds):
    """Timestamp SRT: HH:MM:SS,mmm"""
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _font(size):
    for p in (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _legend_for(name):
    # name = "01_login_web" (sem extensao)
    parts = name.split("_", 1)
    key = parts[1] if len(parts) > 1 else name
    if key.startswith("FALHA_"):
        return "FALHA: " + LEGENDS.get(key[len("FALHA_"):], key[len("FALHA_"):])
    return LEGENDS.get(key, key.replace("_", " "))


def _compose(img_path, legend, title):
    """Letterbox do screenshot em 1280x720 + faixa de legenda embaixo + titulo."""
    canvas = Image.new("RGB", (W, H), (15, 17, 22))
    try:
        shot = Image.open(img_path).convert("RGB")
        area_h = H - BANNER_H
        scale = min(W / shot.width, area_h / shot.height)
        nw, nh = int(shot.width * scale), int(shot.height * scale)
        shot = shot.resize((nw, nh), Image.LANCZOS)
        canvas.paste(shot, ((W - nw) // 2, (area_h - nh) // 2))
    except Exception as e:
        ImageDraw.Draw(canvas).text((40, 40), f"(frame ilegivel: {e})", font=_font(28), fill=(255, 120, 120))

    d = ImageDraw.Draw(canvas)
    # faixa
    d.rectangle([0, H - BANNER_H, W, H], fill=(20, 110, 200))
    d.text((24, H - BANNER_H + 22), legend, font=_font(34), fill=(255, 255, 255))
    # titulo (canto superior)
    d.rectangle([0, 0, W, 40], fill=(0, 0, 0))
    d.text((24, 6), title, font=_font(24), fill=(180, 220, 255))
    return cv2.cvtColor(np.array(canvas), cv2.COLOR_RGB2BGR)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds-per-frame", type=float, default=3.0)
    ap.add_argument("--title", default="RPA Dominio - execucao via worker (ate F8)")
    args = ap.parse_args()

    frames = sorted(glob.glob(os.path.join(args.frames_dir, "*.png")))
    if not frames:
        raise SystemExit(f"Nenhum frame em {args.frames_dir}")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    # Escreve primeiro num temp; depois (se houver ffmpeg) muxamos a legenda no .out.
    video_tmp = args.out + ".tmp.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(video_tmp, fourcc, FPS, (W, H))
    reps = max(1, int(FPS * args.seconds_per_frame))

    srt_entries = []
    for idx, f in enumerate(frames):
        name = os.path.splitext(os.path.basename(f))[0]
        legend = _legend_for(name)
        frame = _compose(f, legend, args.title)
        for _ in range(reps):
            vw.write(frame)
        start = idx * args.seconds_per_frame
        end = (idx + 1) * args.seconds_per_frame
        srt_entries.append((idx + 1, start, end, legend))
        print(f"  + {os.path.basename(f)} -> {legend}")

    vw.release()

    # Legenda .srt (mesmo nome do mp4) com o passo da execucao em cada intervalo.
    srt_path = os.path.splitext(args.out)[0] + ".srt"
    with open(srt_path, "w", encoding="utf-8") as s:
        for n, start, end, legend in srt_entries:
            s.write(f"{n}\n{_ts(start)} --> {_ts(end)}\n{legend}\n\n")

    # UNE a legenda ao video: embute o .srt como faixa de legenda (soft subs) no MP4.
    # Mantem a faixa queimada na imagem; ffmpeg so adiciona a trilha selecionavel.
    ff = shutil.which("ffmpeg")
    if ff:
        cmd = [ff, "-y", "-i", video_tmp, "-i", srt_path,
               "-c", "copy", "-c:s", "mov_text",
               "-metadata:s:s:0", "language=por", args.out]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and os.path.isfile(args.out):
            os.remove(video_tmp)
            print(f"OK: legenda EMBUTIDA no video via ffmpeg -> {args.out}")
        else:
            os.replace(video_tmp, args.out)
            print(f"WARN: ffmpeg falhou ({(r.stderr or '')[-200:]}); video com legenda queimada + .srt ao lado")
    else:
        os.replace(video_tmp, args.out)
        print("INFO: ffmpeg ausente; legenda queimada na imagem + .srt ao lado")

    print(f"OK: video salvo em {args.out} ({len(frames)} passos, {args.seconds_per_frame}s cada)")
    print(f"OK: legenda salva em {srt_path}")


if __name__ == "__main__":
    main()
