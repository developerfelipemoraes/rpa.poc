"""
Produtor de jobs (simula o Portal) — RODA NA VM, autentica por Managed Identity.

Faz EXATAMENTE o que o cron do Portal fará em produção (ver memoria
rpa-portal-integration), na ordem que importa:
  1. upload do(s) arquivo(s) a importar  -> container rpa-files/<job_id>/...
  2. enfileira 1 mensagem em rpa-jobs com empresa(s) + ponteiro do arquivo (TTL)

A MI da VM (id-rpadom-dev) precisa de:
  - Storage Blob Data Contributor          (upload em rpa-files)        [ja existia]
  - Storage Queue Data Message Sender      (enqueue em rpa-jobs)        [add no terraform]

A mensagem segue o contrato consumido pelo worker.py / process_job:
  {
    "job_id": "<uuid>",
    "scheduled_at": "<iso8601>",
    "empresas": [ { "codigo": "5805", "arquivo_blob": "rpa-files/<job_id>/notas_5805.txt" } ],
    "callback_blob": "rpa-results/<job_id>.json"
  }

Uso (na VM, com o venv do app):
  C:\rpa\app\.venv\Scripts\python.exe C:\rpa\app\enqueue_job.py --empresa 5805 C:\rpa\inbox\notas.txt
  ...\python.exe enqueue_job.py --empresa 5805 a.txt --empresa 5806 b.txt   (multi-empresa)

O conteudo e enviado em base64 (casa com clientes que base64-encodam, ex: az cli e
o SDK do Portal). O worker decoda base64 e cai pra JSON puro, entao ambos funcionam.
"""

import os
import sys
import json
import uuid
import base64
import argparse
from datetime import datetime, timezone

from azure.identity import ManagedIdentityCredential
from azure.storage.queue import QueueClient
from azure.storage.blob import BlobServiceClient

STORAGE_ACCOUNT   = os.getenv("STORAGE_ACCOUNT", "strpadomdevye0m9")
QUEUE_NAME        = os.getenv("QUEUE_NAME", "rpa-jobs")
FILES_CONTAINER   = "rpa-files"
RESULTS_CONTAINER = "rpa-results"
MI_CLIENT_ID      = os.getenv("MI_CLIENT_ID", "d26b38fc-d92c-4ba5-917c-e90d4e7a8814")
MSG_TTL           = int(os.getenv("MSG_TTL", "3600"))  # 1h, igual ao Portal


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def build_clients():
    cred = ManagedIdentityCredential(client_id=MI_CLIENT_ID)
    blob = BlobServiceClient(
        account_url=f"https://{STORAGE_ACCOUNT}.blob.core.windows.net",
        credential=cred,
    )
    queue = QueueClient(
        account_url=f"https://{STORAGE_ACCOUNT}.queue.core.windows.net",
        queue_name=QUEUE_NAME,
        credential=cred,
    )
    return blob, queue


def upload_file(blob_svc, job_id, codigo, local_path):
    """Sobe o arquivo local pra rpa-files/<job_id>/notas_<codigo><ext> e retorna
    o caminho do blob COM prefixo do container (formato que process_job espera)."""
    ext = os.path.splitext(local_path)[1] or ".txt"
    blob_name = f"{job_id}/notas_{codigo}{ext}"
    with open(local_path, "rb") as f:
        blob_svc.get_blob_client(FILES_CONTAINER, blob_name).upload_blob(f, overwrite=True)
    full = f"{FILES_CONTAINER}/{blob_name}"
    print(f"  upload OK: {full}")
    return full


def main():
    ap = argparse.ArgumentParser(description="Enfileira 1 job RPA (simula o Portal).")
    ap.add_argument("--empresa", nargs=2, metavar=("CODIGO", "ARQUIVO"),
                    action="append", required=True,
                    help="codigo da empresa + caminho local do arquivo a importar (repetivel)")
    ap.add_argument("--job-id", default=None, help="forca um job_id (default: uuid v4)")
    args = ap.parse_args()

    job_id = args.job_id or str(uuid.uuid4())
    blob_svc, queue = build_clients()

    empresas = []
    for codigo, arquivo in args.empresa:
        if not os.path.isfile(arquivo):
            print(f"ERRO: arquivo nao encontrado: {arquivo}", file=sys.stderr)
            sys.exit(2)
        arquivo_blob = upload_file(blob_svc, job_id, codigo, arquivo)
        empresas.append({"codigo": str(codigo), "arquivo_blob": arquivo_blob})

    msg = {
        "job_id": job_id,
        "scheduled_at": now_iso(),
        "empresas": empresas,
        "callback_blob": f"{RESULTS_CONTAINER}/{job_id}.json",
    }
    payload = json.dumps(msg, ensure_ascii=False)
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    queue.send_message(encoded, time_to_live=MSG_TTL)

    print(f"enqueue OK: job_id={job_id} empresas={len(empresas)} ttl={MSG_TTL}s "
          f"account={STORAGE_ACCOUNT} queue={QUEUE_NAME}")
    print(f"resultado aparecera em: {RESULTS_CONTAINER}/{job_id}.json")


if __name__ == "__main__":
    main()
