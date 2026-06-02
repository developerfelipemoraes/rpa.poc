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

from azure.identity import ManagedIdentityCredential
from azure.storage.queue import QueueClient
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError

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


def run_bot(empresa_codigo, arquivo_path):
    env = {**os.environ,
           "EMPRESA_CODIGO": str(empresa_codigo),
           "ARQUIVO_IMPORTACAO": arquivo_path}
    try:
        r = subprocess.run(
            [PY_EXE, BOT_PATH],
            env=env, capture_output=True, text=True,
            timeout=BOT_TIMEOUT,
        )
        return {
            "ok": r.returncode == 0,
            "returncode": r.returncode,
            "stdout_tail": (r.stdout or "")[-2000:],
            "stderr_tail": (r.stderr or "")[-1000:],
        }
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "returncode": -1,
            "error": "timeout",
            "stdout_tail": (e.stdout or "")[-2000:] if e.stdout else "",
            "stderr_tail": (e.stderr or "")[-1000:] if e.stderr else "",
        }


def process_job(blob_svc, job):
    job_id = job["job_id"]
    started = now_iso()
    empresas_result = []

    for emp in job.get("empresas", []):
        codigo    = emp["codigo"]
        blob_path = emp["arquivo_blob"]
        local     = os.path.join(INBOX_LOCAL, f"{job_id}_{codigo}.txt")

        try:
            download_file(blob_svc, blob_path, local)
        except Exception as e:
            empresas_result.append({"codigo": codigo, "ok": False,
                                    "error": f"download_failed: {e}"})
            continue

        outcome = run_bot(codigo, local)
        empresas_result.append({"codigo": codigo, **outcome})

    oks = [e for e in empresas_result if e.get("ok")]
    
    if len(oks) == len(empresas_result) and empresas_result:
        overall = "DONE"
    elif oks:
        overall = "PARTIAL"
    else:
        overall = "FAILED"

    return {
        "job_id": job_id,
        "status": overall,
        "started_at": started,
        "finished_at": now_iso(),
        "scheduled_at": job.get("scheduled_at"),
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
