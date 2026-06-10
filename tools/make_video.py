"""Monta um MP4 de COMPROVACAO a partir dos frames capturados pelo bot.

Cada frame NN_passo.png e mostrado em TELA CHEIA (desktop inteiro, barra de
tarefas, fundo, contexto do servidor) por ~N segundos, com:
  - um CABECALHO DE PROVA no topo (servidor, job, data/hora, "execucao automatica"),
  - uma faixa de LEGENDA embaixo (o passo da execucao).
Gera tambem um .srt e, se houver ffmpeg, EMBUTE a legenda no MP4 (soft subs).

O objetivo e COMPROVAR que a RPA executou no servidor (VM) de forma automatica
(worker), nao um usuario logado manualmente -> por isso mostra a tela inteira.

Uso:
  python tools/make_video.py --frames-dir C:\rpa\rec\<job> --out C:\rpa\v\<job>.mp4 \
      --server vm-rpadom-dev --job <job_id>
"""

import os
import glob
import shutil
import argparse
import subprocess
from datetime import datetime

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FPS = 30

# Legendas amigaveis por passo (casa com os nomes NN_<passo>.png do bot).
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
    "menu_utilitarios": "Menu Utilitarios",
    "menu_importacao": "Menu Importacao",
    "menu_importador": "Menu Importador",
    "menu_importar": "Menu Importar",
    "dialogo_arquivo": "Tela 'Importacao de Arquivo' aberta",
    "tipo_xml": "Tipo do arquivo definido como XML",
    "localizar_arquivo": "Botao '...' - abrindo 'Procurar Pasta'",
    "procurar_pasta": "Procurar Pasta: selecionando a pasta do XML",
    "pasta_selecionada": "Pasta selecionada (OK)",
    "caminho_preenchido": "Caminho preenchido com o arquivo .xml",
    "apos_importar": "Importacao acionada",
    "importar_arquivo": "Importando o arquivo",
    "inicio": "Inicio - area de trabalho da VM",
}


def _ts(seconds):
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
    parts = name.split("_", 1)
    key = parts[1] if len(parts) > 1 else name
    if key.startswith("FALHA_"):
        return "FALHA: " + LEGENDS.get(key[len("FALHA_"):], key[len("FALHA_"):])
    return LEGENDS.get(key, key.replace("_", " "))


def _compose(img_path, legend, header, sw, sh, header_h, banner_h, fs_h, fs_b):
    """Desktop INTEIRO (sw x sh) em tela cheia + cabecalho de prova no topo +
    faixa de legenda embaixo. Nada de recorte: mostra o contexto do servidor."""
    W, H = sw, sh + header_h + banner_h
    canvas = Image.new("RGB", (W, H), (10, 12, 16))
    try:
        shot = Image.open(img_path).convert("RGB")
        if shot.size != (sw, sh):
            shot = shot.resize((sw, sh), Image.LANCZOS)
        canvas.paste(shot, (0, header_h))  # tela cheia, sem letterbox/recorte
    except Exception as e:
        ImageDraw.Draw(canvas).text((40, header_h + 40), f"(frame ilegivel: {e})",
                                    font=_font(28), fill=(255, 120, 120))

    d = ImageDraw.Draw(canvas)
    # CABECALHO DE PROVA (topo) - vermelho/escuro pra destacar que e evidencia
    d.rectangle([0, 0, W, header_h], fill=(140, 20, 20))
    d.text((20, max(6, (header_h - fs_h) // 2)), header, font=_font(fs_h),
           fill=(255, 255, 255))
    # FAIXA DE LEGENDA (rodape) - o passo da execucao
    d.rectangle([0, header_h + sh, W, H], fill=(20, 110, 200))
    d.text((20, header_h + sh + max(6, (banner_h - fs_b) // 2)), legend,
           font=_font(fs_b), fill=(255, 255, 255))
    return cv2.cvtColor(np.array(canvas), cv2.COLOR_RGB2BGR)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds-per-frame", type=float, default=3.0)
    ap.add_argument("--title", default="RPA Dominio - execucao via worker")
    ap.add_argument("--server", default=os.getenv("COMPUTERNAME", "vm-rpadom-dev"))
    ap.add_argument("--job", default="")
    args = ap.parse_args()

    frames = sorted(glob.glob(os.path.join(args.frames_dir, "*.png")))
    if not frames:
        raise SystemExit(f"Nenhum frame em {args.frames_dir}")

    # Tamanho = resolucao NATIVA do desktop (1o frame) -> tela cheia, nitida.
    with Image.open(frames[0]) as s0:
        sw, sh = s0.size
    sw -= sw % 2
    sh -= sh % 2  # dimensoes pares (exigencia de alguns codecs)
    header_h = max(40, sh // 18)
    banner_h = max(48, sh // 14)
    fs_h = max(15, sw // 70)   # fonte do cabecalho
    fs_b = max(20, sw // 42)   # fonte da legenda
    W, H = sw, sh + header_h + banner_h

    quando = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    header = f"SERVIDOR: {args.server}   |   EXECUCAO AUTOMATICA (worker)   |   {quando}"
    if args.job:
        header += f"   |   JOB {args.job}"

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    video_tmp = args.out + ".tmp.mp4"
    vw = cv2.VideoWriter(video_tmp, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    reps = max(1, int(FPS * args.seconds_per_frame))

    srt_entries = []
    for idx, f in enumerate(frames):
        name = os.path.splitext(os.path.basename(f))[0]
        legend = _legend_for(name)
        frame = _compose(f, legend, header, sw, sh, header_h, banner_h, fs_h, fs_b)
        for _ in range(reps):
            vw.write(frame)
        srt_entries.append((idx + 1, idx * args.seconds_per_frame,
                            (idx + 1) * args.seconds_per_frame, legend))
        print(f"  + {os.path.basename(f)} -> {legend}")
    vw.release()

    srt_path = os.path.splitext(args.out)[0] + ".srt"
    with open(srt_path, "w", encoding="utf-8") as s:
        for n, start, end, legend in srt_entries:
            s.write(f"{n}\n{_ts(start)} --> {_ts(end)}\n{legend}\n\n")

    ff = shutil.which("ffmpeg")
    if ff:
        cmd = [ff, "-y", "-i", video_tmp, "-i", srt_path, "-c", "copy",
               "-c:s", "mov_text", "-metadata:s:s:0", "language=por", args.out]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and os.path.isfile(args.out):
            os.remove(video_tmp)
            print(f"OK: legenda EMBUTIDA via ffmpeg -> {args.out}")
        else:
            os.replace(video_tmp, args.out)
            print(f"WARN: ffmpeg falhou ({(r.stderr or '')[-200:]}); legenda queimada + .srt")
    else:
        os.replace(video_tmp, args.out)
        print("INFO: ffmpeg ausente; legenda queimada + .srt ao lado")

    print(f"OK: video {W}x{H} salvo em {args.out} ({len(frames)} passos)")
    print(f"OK: legenda salva em {srt_path}")


if __name__ == "__main__":
    main()
