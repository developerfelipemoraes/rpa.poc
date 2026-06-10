"""
Worker da Fase 1 (atomico, 1 job por vez).

Loop infinito:
  1. poll na queue rpa-jobs (Storage Queue)
  2. ja processado? (rpa-results/<job_id>.json existe) -> descarta
  3. dequeue_count > 3 -> grava FAILED e descarta (dead-letter manual)
  4. baixa cada arquivo de rpa-files
  5. invoca bot.py 1x por empresa (EMPRESA_CODIGO + ARQUIVO_IMPORTACAO no env)
  6. grava rpa-results/<job_id>.json
  7. delete da msg

Auth: User-Assigned Managed Identity da VM (id-rpadom-dev).
"""

import os
import sys
import json
import time
import base64
import msvcrt
import traceback
import subprocess
from datetime import datetime, timezone

from dotenv import load_dotenv

from azure.identity import ManagedIdentityCredential
from azure.storage.queue import QueueClient
from azure.storage.blob import BlobServiceClient, ContentSettings
from azure.core.exceptions import ResourceNotFoundError

# Carrega .env ANTES de ler as constantes abaixo, para que flags como RPA_RECORD
# possam ser configuradas no .env (do contrario o worker so leria do env do processo).
load_dotenv()

STORAGE_ACCOUNT = os.getenv("STORAGE_ACCOUNT", "strpadomdevye0m9")
QUEUE_NAME      = os.getenv("QUEUE_NAME", "rpa-jobs")
FILES_CONTAINER   = "rpa-files"
RESULTS_CONTAINER = "rpa-results"

MI_CLIENT_ID = os.getenv("MI_CLIENT_ID", "d26b38fc-d92c-4ba5-917c-e90d4e7a8814")

BOT_PATH    = os.getenv("BOT_PATH",    r"C:\rpa\app\bot.py")
PY_EXE      = os.getenv("PY_EXE",      r"C:\rpa\app\.venv\Scripts\python.exe")
INBOX_LOCAL = os.getenv("INBOX_LOCAL", r"C:\rpa\inbox")

POLL_EMPTY_SLEEP   = int(os.getenv("POLL_EMPTY_SLEEP", "10"))
VISIBILITY_TIMEOUT = int(os.getenv("VISIBILITY_TIMEOUT", "1800"))
BOT_TIMEOUT        = int(os.getenv("BOT_TIMEOUT", "1500"))
MAX_DEQUEUE        = int(os.getenv("MAX_DEQUEUE", "3"))

LOCK_PATH = os.getenv("WORKER_LOCK_PATH", r"C:\rpa\app\worker.lock")

# Gravacao de video da execucao (por empresa). Quando RPA_RECORD liga, o worker:
#   - manda o bot gravar frames (desde o login) numa pasta por execucao/empresa;
#   - compoe um MP4 legendado (+ .srt) via tools/make_video.py;
#   - sobe SO no blob como video/<codigo>_<job_id>.mp4 (sem download local).
RECORD          = os.getenv("RPA_RECORD", "").strip().lower() in ("1", "true", "yes", "sim")
REC_BASE        = os.getenv("RPA_RECORD_BASE", r"C:\rpa\rec")
VIDEO_CONTAINER = os.getenv("VIDEO_CONTAINER", RESULTS_CONTAINER)
VIDEO_PREFIX    = os.getenv("VIDEO_PREFIX", "video")
MAKE_VIDEO_PY   = os.path.join(os.path.dirname(BOT_PATH), "tools", "make_video.py")


def acquire_singleton_lock():
    """Garante 1 worker.py por VM. Libera no exit/crash via OS file handle.

    Win32 gotcha: msvcrt.locking() trava bytes a partir da POSICAO ATUAL.
    Modo "a+" posiciona em EOF e cada processo lockaria um byte diferente
    (lock nao bate). Por isso abrimos em "r+b" (posicao 0) e fazemos
    seek(0) explicito antes do locking.
    """
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    if not os.path.exists(LOCK_PATH):
        open(LOCK_PATH, "a").close()
    fh = open(LOCK_PATH, "r+b")
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return None
    try:
        fh.seek(1)
        fh.truncate()
        fh.write(f"{os.getpid()} {now_iso()}\n".encode("utf-8"))
        fh.flush()
    except OSError:
        pass
    return fh


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def build_clients():
    cred = ManagedIdentityCredential(client_id=MI_CLIENT_ID)
    queue = QueueClient(
        account_url=f"https://{STORAGE_ACCOUNT}.queue.core.windows.net",
        queue_name=QUEUE_NAME,
        credential=cred,
    )
    blob = BlobServiceClient(
        account_url=f"https://{STORAGE_ACCOUNT}.blob.core.windows.net",
        credential=cred,
    )
    return queue, blob


def result_exists(blob_svc, job_id):
    bc = blob_svc.get_blob_client(RESULTS_CONTAINER, f"{job_id}.json")
    try:
        return bc.exists()
    except ResourceNotFoundError:
        return False


def write_result(blob_svc, job_id, payload):
    bc = blob_svc.get_blob_client(RESULTS_CONTAINER, f"{job_id}.json")
    bc.upload_blob(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
                   overwrite=True)


def download_file(blob_svc, blob_path, local_path):
    """blob_path = 'rpa-files/<job_id>/notas_xxx.txt' (com prefixo do container)."""
    container, _, name = blob_path.partition("/")
    bc = blob_svc.get_blob_client(container, name)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    with open(local_path, "wb") as f:
        f.write(bc.download_blob().readall())


RESULT_SENTINEL = "RPA_RESULT_JSON:"


def parse_bot_result(stdout):
    """Extrai a linha-sentinela 'RPA_RESULT_JSON:{...}' emitida pelo bot.py e
    devolve os campos enriquecidos (imported/errors/failed_step/message).

    Procura de baixo pra cima (a sentinela e a ultima linha do bot) e retorna {}
    se ausente/invalida - nesse caso o result fica so com returncode/logs."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith(RESULT_SENTINEL):
            try:
                data = json.loads(line[len(RESULT_SENTINEL):])
            except (ValueError, TypeError):
                return {}
            return {
                "imported": data.get("imported"),
                "errors": data.get("errors"),
                "failed_step": data.get("failed_step"),
                "message": data.get("message"),
            }
    return {}


def run_bot(empresa_codigo, arquivo_path, record_dir=None):
    env = {**os.environ,
           "EMPRESA_CODIGO": str(empresa_codigo),
           "ARQUIVO_IMPORTACAO": arquivo_path}
    if record_dir:
        # Sobrescreve qualquer RPA_RECORD_DIR do .env: o worker e dono do destino.
        env["RPA_RECORD_DIR"] = record_dir
    try:
        r = subprocess.run(
            [PY_EXE, BOT_PATH],
            env=env, capture_output=True, text=True,
            timeout=BOT_TIMEOUT,
        )
        stdout = r.stdout or ""
        out = {
            "ok": r.returncode == 0,
            "returncode": r.returncode,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": (r.stderr or "")[-1000:],
        }
        out.update(parse_bot_result(stdout))
        return out
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout or ""
        out = {
            "ok": False,
            "returncode": -1,
            "error": "timeout",
            "stdout_tail": stdout[-2000:] if stdout else "",
            "stderr_tail": (e.stderr or "")[-1000:] if e.stderr else "",
        }
        out.update(parse_bot_result(stdout))
        return out


def _prep_record_dir(job_id, codigo):
    """Pasta de frames por execucao/empresa, limpa antes de cada run."""
    d = os.path.join(REC_BASE, f"{job_id}_{codigo}")
    if os.path.isdir(d):
        for f in os.listdir(d):
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass
    os.makedirs(d, exist_ok=True)
    return d


def compose_and_upload_video(blob_svc, job_id, codigo, record_dir):
    """Compoe o MP4 legendado (+ .srt) dos frames e sobe SO no blob como
    video/<codigo>_<job_id>.mp4. Retorna o caminho do blob ou None.

    Best-effort: erro de gravacao NUNCA derruba o processamento do job."""
    try:
        frames = [f for f in os.listdir(record_dir) if f.lower().endswith(".png")]
        if not frames:
            print(f"  [rec] sem frames para {codigo}; pulando video.")
            return None
        out_mp4 = os.path.join(record_dir, f"{codigo}_{job_id}.mp4")
        title = f"RPA Dominio - empresa {codigo} - exec {job_id}"
        servidor = os.getenv("COMPUTERNAME", "vm-rpadom-dev")
        r = subprocess.run(
            [PY_EXE, MAKE_VIDEO_PY, "--frames-dir", record_dir,
             "--out", out_mp4, "--title", title,
             "--server", servidor, "--job", str(job_id)],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0 or not os.path.isfile(out_mp4):
            print(f"  [rec] make_video falhou: {(r.stderr or r.stdout)[-500:]}")
            return None

        blob_name = f"{VIDEO_PREFIX}/{codigo}_{job_id}.mp4"
        with open(out_mp4, "rb") as fh:
            blob_svc.get_blob_client(VIDEO_CONTAINER, blob_name).upload_blob(
                fh, overwrite=True,
                content_settings=ContentSettings(content_type="video/mp4"))
        # legenda .srt junto (mesmo nome)
        srt = os.path.splitext(out_mp4)[0] + ".srt"
        if os.path.isfile(srt):
            with open(srt, "rb") as fh:
                blob_svc.get_blob_client(
                    VIDEO_CONTAINER, f"{VIDEO_PREFIX}/{codigo}_{job_id}.srt").upload_blob(
                    fh, overwrite=True,
                    content_settings=ContentSettings(content_type="text/plain; charset=utf-8"))
        print(f"  [rec] video no blob: {VIDEO_CONTAINER}/{blob_name}")
        return f"{VIDEO_CONTAINER}/{blob_name}"
    except Exception as e:
        print(f"  [rec] WARN compose/upload falhou: {e}")
        return None


def process_job(blob_svc, job):
    job_id = job["job_id"]
    started = now_iso()
    empresas_result = []

    for emp in job.get("empresas", []):
        codigo    = emp["codigo"]
        blob_path = emp["arquivo_blob"]
        # Baixa o arquivo do blob para uma PASTA por job/empresa, preservando o
        # nome/extensao reais (.xml). O bot seleciona ESSA pasta no 'Procurar Pasta'
        # e usa o caminho completo do arquivo no campo 'Caminho'.
        fname     = os.path.basename(blob_path) or f"{job_id}_{codigo}.xml"
        local     = os.path.join(INBOX_LOCAL, f"{job_id}_{codigo}", fname)

        try:
            download_file(blob_svc, blob_path, local)
        except Exception as e:
            empresas_result.append({"codigo": codigo, "ok": False,
                                    "error": f"download_failed: {e}"})
            continue

        record_dir = _prep_record_dir(job_id, codigo) if RECORD else None
        outcome = run_bot(codigo, local, record_dir=record_dir)
        if RECORD and record_dir:
            video_blob = compose_and_upload_video(blob_svc, job_id, codigo, record_dir)
            if video_blob:
                outcome["video_blob"] = video_blob
        empresas_result.append({"codigo": codigo, **outcome})

    oks = [e for e in empresas_result if e.get("ok")]

    if len(oks) == len(empresas_result) and empresas_result:
        overall = "DONE"
    elif oks:
        overall = "PARTIAL"
    else:
        overall = "FAILED"

    # Totais agregados (somando so o que o bot conseguiu ler; None = desconhecido).
    imported_total = sum(e["imported"] for e in empresas_result
                         if isinstance(e.get("imported"), int))
    errors_total = sum(e["errors"] for e in empresas_result
                       if isinstance(e.get("errors"), int))

    return {
        "job_id": job_id,
        "status": overall,
        "started_at": started,
        "finished_at": now_iso(),
        "scheduled_at": job.get("scheduled_at"),
        "imported_total": imported_total,
        "errors_total": errors_total,
        "empresas": empresas_result,
    }


def decode_message_content(content):
    """Tenta base64 (padrao do az cli e da maioria dos clientes) e cai pra JSON puro."""
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    txt = content.strip()
    try:
        return json.loads(base64.b64decode(txt))
    except Exception:
        return json.loads(txt)


def main():
    lock_fh = acquire_singleton_lock()
    if lock_fh is None:
        print(f"[worker] outra instancia ja segura {LOCK_PATH}. Saindo.",
              file=sys.stderr, flush=True)
        sys.exit(2)
    print(f"[worker] singleton lock OK ({LOCK_PATH}, pid={os.getpid()})",
          flush=True)

    queue, blob_svc = build_clients()
    print(f"[worker] started; account={STORAGE_ACCOUNT} queue={QUEUE_NAME} "
          f"poll_sleep={POLL_EMPTY_SLEEP}s visibility={VISIBILITY_TIMEOUT}s",
          flush=True)

    while True:
        try:
            batch = queue.receive_messages(max_messages=1,
                                           visibility_timeout=VISIBILITY_TIMEOUT)
            msg = next(iter(batch), None)
        except Exception as e:
            print(f"[worker] receive failed: {e}", flush=True)
            time.sleep(POLL_EMPTY_SLEEP)
            continue

        if msg is None:
            time.sleep(POLL_EMPTY_SLEEP)
            continue

        try:
            job = decode_message_content(msg.content)
            job_id = job["job_id"]
            print(f"[worker] msg id={msg.id} job={job_id} "
                  f"dequeue_count={msg.dequeue_count}", flush=True)

            if result_exists(blob_svc, job_id):
                print(f"[worker]   {job_id} ja tem result; descartando msg",
                      flush=True)
                queue.delete_message(msg)
                continue

            if msg.dequeue_count > MAX_DEQUEUE:
                print(f"[worker]   {job_id} excedeu MAX_DEQUEUE; grava FAILED",
                      flush=True)
                write_result(blob_svc, job_id, {
                    "job_id": job_id, "status": "FAILED",
                    "error": "max_retries_exceeded",
                    "finished_at": now_iso(),
                })
                queue.delete_message(msg)
                continue

            result = process_job(blob_svc, job)
            write_result(blob_svc, job_id, result)
            queue.delete_message(msg)
            print(f"[worker]   {job_id} -> {result['status']}", flush=True)

        except Exception as e:
            print(f"[worker] ERROR processing msg id={getattr(msg,'id',None)}: "
                  f"{e}\n{traceback.format_exc()}", flush=True)
            # NAO deleta a msg -> volta visivel em VISIBILITY_TIMEOUT


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("[worker] interrupted, exiting.", flush=True)
        sys.exit(0)
