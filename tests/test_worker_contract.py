"""Teste de CONTRATO worker <-> bot com subprocess REAL (sem mock).

Diferente do test_worker.py (que mocka subprocess.run), aqui o worker chama um
processo Python de verdade (tests/fake_bot.py) e validamos o contrato inteiro:
  - env vars (EMPRESA_CODIGO/ARQUIVO_IMPORTACAO) chegam no bot
  - exit code do bot vira run_bot()["ok"]  (a correcao que tornou o result confiavel)
  - a linha-sentinela RPA_RESULT_JSON e parseada -> imported/errors/failed_step/message
  - process_job agrega status + imported_total/errors_total

Nao toca Azure nem GUI -> roda em qualquer maquina/CI.
"""

import os
import sys
import json

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402

FAKE_BOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_bot.py")


@pytest.fixture
def real_bot(monkeypatch):
    """Aponta o worker pro fake_bot.py rodando no MESMO interpretador."""
    monkeypatch.setattr(worker, "PY_EXE", sys.executable)
    monkeypatch.setattr(worker, "BOT_PATH", FAKE_BOT)


# ---------------------------------------------------------------------------
# parse_bot_result (unitario puro)
# ---------------------------------------------------------------------------

class TestParseBotResult:
    def test_extracts_fields(self):
        line = "RPA_RESULT_JSON:" + json.dumps(
            {"imported": 142, "errors": 0, "failed_step": None, "message": "ok"})
        out = worker.parse_bot_result(f"ruido\n{line}\nmais ruido")
        assert out == {"imported": 142, "errors": 0,
                       "failed_step": None, "message": "ok"}

    def test_absent_sentinel_returns_empty(self):
        assert worker.parse_bot_result("nenhuma sentinela aqui") == {}

    def test_malformed_json_returns_empty(self):
        assert worker.parse_bot_result("RPA_RESULT_JSON:{nao-e-json}") == {}

    def test_uses_last_sentinel_when_repeated(self):
        a = "RPA_RESULT_JSON:" + json.dumps({"imported": 1})
        b = "RPA_RESULT_JSON:" + json.dumps({"imported": 99})
        assert worker.parse_bot_result(f"{a}\n{b}")["imported"] == 99


# ---------------------------------------------------------------------------
# run_bot com subprocess real
# ---------------------------------------------------------------------------

class TestRunBotReal:
    def test_success_exit0_is_ok_and_parsed(self, real_bot, monkeypatch):
        monkeypatch.setenv("FAKE_BOT_EXIT", "0")
        monkeypatch.setenv("FAKE_BOT_IMPORTED", "142")
        monkeypatch.setenv("FAKE_BOT_ERRORS", "0")
        out = worker.run_bot("5805", r"C:\inbox\notas.txt")
        assert out["ok"] is True
        assert out["returncode"] == 0
        assert out["imported"] == 142
        assert out["errors"] == 0
        assert out["failed_step"] is None

    def test_failure_exit1_is_not_ok(self, real_bot, monkeypatch):
        # ESTE e o gap que a correcao fechou: antes o bot saia 0 mesmo falhando.
        monkeypatch.setenv("FAKE_BOT_EXIT", "1")
        monkeypatch.setenv("FAKE_BOT_FAILED_STEP", "selecionar_empresa")
        out = worker.run_bot("5805", r"C:\inbox\notas.txt")
        assert out["ok"] is False
        assert out["returncode"] == 1
        assert out["failed_step"] == "selecionar_empresa"

    def test_env_vars_reach_the_bot(self, real_bot, monkeypatch):
        monkeypatch.delenv("FAKE_BOT_EXIT", raising=False)
        out = worker.run_bot("9999", r"C:\inbox\x.txt")
        assert "EMPRESA_CODIGO=9999" in out["stdout_tail"]
        assert r"ARQUIVO_IMPORTACAO=C:\inbox\x.txt" in out["stdout_tail"]


# ---------------------------------------------------------------------------
# process_job ponta-a-ponta (download mockado, bot real)
# ---------------------------------------------------------------------------

def _blob_svc(content=b"conteudo"):
    from unittest.mock import MagicMock
    svc = MagicMock()
    svc.get_blob_client.return_value.download_blob.return_value.readall.return_value = content
    return svc


class TestProcessJobReal:
    def test_done_with_aggregated_totals(self, real_bot, monkeypatch, tmp_path):
        monkeypatch.setattr(worker, "INBOX_LOCAL", str(tmp_path))
        monkeypatch.setenv("FAKE_BOT_EXIT", "0")
        monkeypatch.setenv("FAKE_BOT_IMPORTED", "10")
        monkeypatch.setenv("FAKE_BOT_ERRORS", "0")
        job = {"job_id": "jc1",
               "empresas": [
                   {"codigo": "5805", "arquivo_blob": "rpa-files/jc1/a.txt"},
                   {"codigo": "5806", "arquivo_blob": "rpa-files/jc1/b.txt"},
               ]}
        r = worker.process_job(_blob_svc(), job)
        assert r["status"] == "DONE"
        assert r["imported_total"] == 20   # 10 + 10
        assert r["errors_total"] == 0
        assert all(e["ok"] for e in r["empresas"])

    def test_failed_when_bot_exits_nonzero(self, real_bot, monkeypatch, tmp_path):
        monkeypatch.setattr(worker, "INBOX_LOCAL", str(tmp_path))
        monkeypatch.setenv("FAKE_BOT_EXIT", "1")
        monkeypatch.setenv("FAKE_BOT_FAILED_STEP", "login_web")
        job = {"job_id": "jc2",
               "empresas": [{"codigo": "5805", "arquivo_blob": "rpa-files/jc2/a.txt"}]}
        r = worker.process_job(_blob_svc(), job)
        assert r["status"] == "FAILED"
        assert r["empresas"][0]["failed_step"] == "login_web"
