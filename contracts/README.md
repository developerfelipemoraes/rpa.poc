# Contratos da integração Portal ↔ Worker

Mensagens trocadas via Azure Storage (conta `strpadomdevye0m9`). Ver arquitetura
completa na memória `rpa-portal-integration`.

## 1. Entrada — Queue `rpa-jobs`

O Portal (ou o produtor `enqueue_job.py`, que o simula) enfileira **uma mensagem por job**.
Exemplo: [`rpa-jobs.message.example.json`](rpa-jobs.message.example.json).

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `job_id` | string (uuid v4) | sim | chave de idempotência; nomeia o blob de resultado |
| `scheduled_at` | string (ISO-8601) | não | metadado/log — o worker executa **imediatamente** ao receber |
| `empresas` | array | sim | 1..N empresas processadas sequencialmente |
| `empresas[].codigo` | string | sim | código da empresa no Domínio (digitado via F8) → vai pro bot em `EMPRESA_CODIGO` |
| `empresas[].arquivo_blob` | string | sim | caminho do blob **com prefixo do container** (`rpa-files/<job_id>/...`); baixado e passado ao bot em `ARQUIVO_IMPORTACAO` |
| `callback_blob` | string | não | onde o resultado será gravado (informativo; o worker sempre usa `rpa-results/<job_id>.json`) |

**Pré-condição:** o(s) arquivo(s) referenciados em `arquivo_blob` já devem estar no
container `rpa-files` **antes** de enfileirar (o `enqueue_job.py` faz upload + enqueue
nessa ordem).

**Encoding:** a mensagem pode ir em **base64** (padrão do `az cli` / SDK do Portal) ou
**JSON puro** — o worker tenta base64 primeiro e cai pra JSON. O `enqueue_job.py` envia base64.

**Configs no envio (produtor):** `time_to_live = 3600s` (1h). Se o worker estiver morto,
a msg expira e o cron de resultado do Portal marca `STALE` em vez de ressuscitar job obsoleto.

## 2. Saída — Blob `rpa-results/<job_id>.json`

O worker grava o resultado e só então deleta a mensagem da queue.
Exemplo: [`rpa-results.example.json`](rpa-results.example.json).

| Campo | Descrição |
|---|---|
| `status` | `DONE` (todas ok), `PARTIAL` (algumas ok), `FAILED` (nenhuma) |
| `imported_total` / `errors_total` | soma dos contadores por empresa (apenas os que o bot conseguiu ler) |
| `empresas[].ok` | `returncode == 0` do `bot.py` (agora confiável — bot sai != 0 em falha) |
| `empresas[].imported` / `errors` | contadores lidos da tela de confirmação do Domínio; `null` enquanto o leitor dessa tela não estiver implementado |
| `empresas[].failed_step` | etapa onde o bot parou (`login_web`, `selecionar_empresa`, …) ou `null` |
| `empresas[].message` | resumo legível |
| `empresas[].stdout_tail` / `stderr_tail` | últimas linhas dos logs do bot |

> `imported`/`errors` são `null` até capturarmos o template da tela de resultado da
> importação na VM (1920×1080). O `bot.py` já grava `import_result_<ts>.png` no fim do
> fluxo — envie esse print para implementar o parser e os números passam a vir preenchidos.

## 3. Como produzir uma mensagem (na VM)

```powershell
C:\rpa\app\.venv\Scripts\python.exe C:\rpa\app\enqueue_job.py --empresa 5805 C:\rpa\inbox\notas.txt
```
