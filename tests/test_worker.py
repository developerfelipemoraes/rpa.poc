"""Testes unitarios do worker.py (Fase 1, processamento atomico).

Cobertura:
  decode_message_content  - base64, JSON puro, bytes, whitespace, invalido
  now_iso                 - formato ISO-8601 com tz
  result_exists           - True, False, ResourceNotFoundError
  write_result            - JSON utf-8 + overwrite
  download_file           - parse container/name + cria diretorio
  run_bot                 - ok, fail, timeout, env vars, truncamento de stdout
  process_job             - DONE, PARTIAL, FAILED, download_fail, empresas vazias, scheduled_at
  acquire_singleton_lock  - aquisicao livre, 2a chamada nula, criacao de dir

Nao cobre main() (loop infinito) - validacao end-to-end fica pra teste manual via az cli.
"""

import os
import sys
import json
import base64
import subprocess
from unittest.mock import MagicMock
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402


# ---------------------------------------------------------------------------
# decode_message_content
# ---------------------------------------------------------------------------

class TestDecodeMessage:
    def test_base64_encoded_json(self):
        payload = {"job_id": "abc", "empresas": [{"codigo": "5805"}]}
        raw = base64.b64encode(json.dumps(payload).encode()).decode()
        assert worker.decode_message_content(raw) == payload

    def test_plain_json_string(self):
        payload = {"job_id": "abc"}
        raw = json.dumps(payload)
        assert worker.decode_message_content(raw) == payload

    def test_bytes_input(self):
        payload = {"job_id": "abc"}
        raw = json.dumps(payload).encode("utf-8")
        assert worker.decode_message_content(raw) == payload

    def test_whitespace_is_stripped(self):
        payload = {"job_id": "abc"}
        raw = "  \n" + json.dumps(payload) + "  "
        assert worker.decode_message_content(raw) == payload

    def test_invalid_content_raises(self):
        with pytest.raises(json.JSONDecodeError):
            worker.decode_message_content("not-base64-not-json!!")


# ---------------------------------------------------------------------------
# now_iso
# ---------------------------------------------------------------------------

def test_now_iso_parses_back_with_tz():
    s = worker.now_iso()
    dt = datetime.fromisoformat(s)
    assert dt.tzinfo is not None, "now_iso() precisa carregar timezone (UTC)"


# ---------------------------------------------------------------------------
# result_exists
# ---------------------------------------------------------------------------

class TestResultExists:
    def test_returns_true(self):
        blob_svc = MagicMock()
        blob_svc.get_blob_client.return_value.exists.return_value = True
        assert worker.result_exists(blob_svc, "abc") is True
        blob_svc.get_blob_client.assert_called_with("rpa-results", "abc.json")

    def test_returns_false(self):
        blob_svc = MagicMock()
        blob_svc.get_blob_client.return_value.exists.return_value = False
        assert worker.result_exists(blob_svc, "abc") is False

    def test_resource_not_found_treated_as_false(self):
        from azure.core.exceptions import ResourceNotFoundError
        blob_svc = MagicMock()
        blob_svc.get_blob_client.return_value.exists.side_effect = \
            ResourceNotFoundError("missing")
        assert worker.result_exists(blob_svc, "abc") is False


# ---------------------------------------------------------------------------
# write_result
# ---------------------------------------------------------------------------

def test_write_result_uploads_utf8_json_with_overwrite():
    blob_svc = MagicMock()
    payload = {"job_id": "abc", "status": "DONE", "msg": "acentuação"}
    worker.write_result(blob_svc, "abc", payload)

    blob_svc.get_blob_client.assert_called_with("rpa-results", "abc.json")
    bc = blob_svc.get_blob_client.return_value
    args, kwargs = bc.upload_blob.call_args
    data = args[0]
    assert isinstance(data, bytes)
    assert json.loads(data.decode("utf-8")) == payload
    assert "acentua\\u" not in data.decode("utf-8"), \
        "ensure_ascii=False deveria preservar caracteres unicode"
    assert kwargs.get("overwrite") is True


# ---------------------------------------------------------------------------
# download_file
# ---------------------------------------------------------------------------

def test_download_file_parses_and_writes(tmp_path):
    blob_svc = MagicMock()
    bc = blob_svc.get_blob_client.return_value
    bc.download_blob.return_value.readall.return_value = b"conteudo_bytes"

    local = tmp_path / "sub" / "deeper" / "notas.txt"
    worker.download_file(blob_svc, "rpa-files/test-001/notas.txt", str(local))

    blob_svc.get_blob_client.assert_called_with("rpa-files", "test-001/notas.txt")
    assert local.read_bytes() == b"conteudo_bytes"
    assert local.parent.is_dir(), "diretorio pai foi criado"


def test_download_file_handles_blob_with_subpath(tmp_path):
    blob_svc = MagicMock()
    bc = blob_svc.get_blob_client.return_value
    bc.download_blob.return_value.readall.return_value = b"x"

    local = tmp_path / "out.bin"
    worker.download_file(blob_svc, "rpa-files/a/b/c/d.txt", str(local))
    blob_svc.get_blob_client.assert_called_with("rpa-files", "a/b/c/d.txt")


# ---------------------------------------------------------------------------
# run_bot
# ---------------------------------------------------------------------------

class TestRunBot:
    def test_success(self, monkeypatch):
        fake = MagicMock(returncode=0, stdout="ok", stderr="")
        monkeypatch.setattr(worker.subprocess, "run", lambda *a, **kw: fake)
        out = worker.run_bot("5805", r"C:\inbox\f.txt")
        assert out["ok"] is True
        assert out["returncode"] == 0
        assert out["stdout_tail"] == "ok"
        assert out["stderr_tail"] == ""

    def test_nonzero_exit_marks_failure(self, monkeypatch):
        fake = MagicMock(returncode=2, stdout="", stderr="boom")
        monkeypatch.setattr(worker.subprocess, "run", lambda *a, **kw: fake)
        out = worker.run_bot("5805", r"C:\inbox\f.txt")
        assert out["ok"] is False
        assert out["returncode"] == 2
        assert "boom" in out["stderr_tail"]

    def test_timeout_handled(self, monkeypatch):
        def raises(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="bot.py", timeout=1,
                                             output="parcial", stderr="oops")
        monkeypatch.setattr(worker.subprocess, "run", raises)
        out = worker.run_bot("5805", r"C:\inbox\f.txt")
        assert out["ok"] is False
        assert out["error"] == "timeout"
        assert out["returncode"] == -1
        assert out["stdout_tail"] == "parcial"
        assert out["stderr_tail"] == "oops"

    def test_env_vars_injected(self, monkeypatch):
        captured = {}

        def capture(*a, **kw):
            captured.update(kw)
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(worker.subprocess, "run", capture)
        worker.run_bot("5805", r"C:\inbox\f.txt")
        env = captured["env"]
        assert env["EMPRESA_CODIGO"] == "5805"
        assert env["ARQUIVO_IMPORTACAO"] == r"C:\inbox\f.txt"

    def test_stdout_truncated_to_tail_2000(self, monkeypatch):
        huge = "X" * 5000
        fake = MagicMock(returncode=0, stdout=huge, stderr="")
        monkeypatch.setattr(worker.subprocess, "run", lambda *a, **kw: fake)
        out = worker.run_bot("5805", r"C:\f.txt")
        assert len(out["stdout_tail"]) == 2000
        assert out["stdout_tail"] == "X" * 2000

    def test_stderr_truncated_to_tail_1000(self, monkeypatch):
        big = "E" * 3000
        fake = MagicMock(returncode=1, stdout="", stderr=big)
        monkeypatch.setattr(worker.subprocess, "run", lambda *a, **kw: fake)
        out = worker.run_bot("5805", r"C:\f.txt")
        assert len(out["stderr_tail"]) == 1000

    def test_calls_correct_binary_and_script(self, monkeypatch):
        captured = {}

        def capture(cmd, *a, **kw):
            captured["cmd"] = cmd
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(worker.subprocess, "run", capture)
        worker.run_bot("5805", r"C:\f.txt")
        assert captured["cmd"] == [worker.PY_EXE, worker.BOT_PATH]


# ---------------------------------------------------------------------------
# process_job
# ---------------------------------------------------------------------------

def _blob_svc_with_download(content=b"x"):
    svc = MagicMock()
    svc.get_blob_client.return_value.download_blob.return_value.readall.return_value = content
    return svc


class TestProcessJob:
    def test_single_empresa_all_ok_is_done(self, monkeypatch, tmp_path):
        monkeypatch.setattr(worker, "INBOX_LOCAL", str(tmp_path))
        monkeypatch.setattr(worker, "run_bot",
                            lambda c, p: {"ok": True, "returncode": 0})
        job = {"job_id": "j1",
               "empresas": [{"codigo": "5805",
                              "arquivo_blob": "rpa-files/j1/n.txt"}]}
        r = worker.process_job(_blob_svc_with_download(), job)
        assert r["status"] == "DONE"
        assert r["job_id"] == "j1"
        assert len(r["empresas"]) == 1
        assert r["empresas"][0]["codigo"] == "5805"
        assert "started_at" in r and "finished_at" in r

    def test_all_empresas_fail_is_failed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(worker, "INBOX_LOCAL", str(tmp_path))
        monkeypatch.setattr(worker, "run_bot",
                            lambda c, p: {"ok": False, "returncode": 1})
        job = {"job_id": "j1",
               "empresas": [
                   {"codigo": "5805", "arquivo_blob": "rpa-files/j1/a.txt"},
                   {"codigo": "5806", "arquivo_blob": "rpa-files/j1/b.txt"},
               ]}
        r = worker.process_job(_blob_svc_with_download(), job)
        assert r["status"] == "FAILED"

    def test_mixed_outcome_is_partial(self, monkeypatch, tmp_path):
        monkeypatch.setattr(worker, "INBOX_LOCAL", str(tmp_path))
        calls = iter([
            {"ok": True, "returncode": 0},
            {"ok": False, "returncode": 1},
        ])
        monkeypatch.setattr(worker, "run_bot", lambda c, p: next(calls))
        job = {"job_id": "j1",
               "empresas": [
                   {"codigo": "5805", "arquivo_blob": "rpa-files/j1/a.txt"},
                   {"codigo": "5806", "arquivo_blob": "rpa-files/j1/b.txt"},
               ]}
        r = worker.process_job(_blob_svc_with_download(), job)
        assert r["status"] == "PARTIAL"

    def test_download_failure_skips_bot_call(self, monkeypatch, tmp_path):
        monkeypatch.setattr(worker, "INBOX_LOCAL", str(tmp_path))
        called = []
        monkeypatch.setattr(worker, "run_bot",
                            lambda c, p: called.append((c, p)) or {"ok": True})
        svc = MagicMock()
        svc.get_blob_client.return_value.download_blob.side_effect = \
            RuntimeError("net fail")
        job = {"job_id": "j1",
               "empresas": [{"codigo": "5805",
                              "arquivo_blob": "rpa-files/j1/a.txt"}]}
        r = worker.process_job(svc, job)
        assert called == [], "bot.py NAO deve ser chamado se download falhou"
        assert r["status"] == "FAILED"
        assert "download_failed" in r["empresas"][0]["error"]

    def test_empty_empresas_is_failed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(worker, "INBOX_LOCAL", str(tmp_path))
        r = worker.process_job(_blob_svc_with_download(),
                                {"job_id": "j1", "empresas": []})
        assert r["status"] == "FAILED"

    def test_scheduled_at_is_propagated(self, monkeypatch, tmp_path):
        monkeypatch.setattr(worker, "INBOX_LOCAL", str(tmp_path))
        monkeypatch.setattr(worker, "run_bot",
                            lambda c, p: {"ok": True, "returncode": 0})
        job = {"job_id": "j1",
               "scheduled_at": "2026-06-01T14:00:00-03:00",
               "empresas": [{"codigo": "5805",
                              "arquivo_blob": "rpa-files/j1/a.txt"}]}
        r = worker.process_job(_blob_svc_with_download(), job)
        assert r["scheduled_at"] == "2026-06-01T14:00:00-03:00"

    def test_local_file_name_includes_job_and_codigo(self, monkeypatch, tmp_path):
        monkeypatch.setattr(worker, "INBOX_LOCAL", str(tmp_path))
        captured = {}

        def capture(codigo, path):
            captured["codigo"] = codigo
            captured["path"] = path
            return {"ok": True, "returncode": 0}

        monkeypatch.setattr(worker, "run_bot", capture)
        job = {"job_id": "abcd-1234",
               "empresas": [{"codigo": "5805",
                              "arquivo_blob": "rpa-files/abcd-1234/x.txt"}]}
        worker.process_job(_blob_svc_with_download(), job)
        assert "abcd-1234" in captured["path"]
        assert "5805" in captured["path"]


# ---------------------------------------------------------------------------
# acquire_singleton_lock
# ---------------------------------------------------------------------------

class TestSingletonLock:
    def test_acquires_when_free(self, tmp_path, monkeypatch):
        lock_path = str(tmp_path / "worker.lock")
        monkeypatch.setattr(worker, "LOCK_PATH", lock_path)
        fh = worker.acquire_singleton_lock()
        try:
            assert fh is not None
            assert os.path.exists(lock_path)
        finally:
            if fh:
                fh.close()

    def test_writes_pid(self, tmp_path, monkeypatch):
        """PID + timestamp gravados a partir do byte 1 (byte 0 e o lock).

        Usamos read binario porque o handle do worker e "r+b".
        """
        lock_path = str(tmp_path / "worker.lock")
        monkeypatch.setattr(worker, "LOCK_PATH", lock_path)
        fh = worker.acquire_singleton_lock()
        try:
            fh.seek(0)
            content = fh.read().decode("utf-8", errors="ignore")
            assert str(os.getpid()) in content, \
                f"PID nao encontrado em {content!r}"
        finally:
            if fh:
                fh.close()

    def test_second_caller_in_other_process_returns_none(self, tmp_path,
                                                          monkeypatch):
        """Cenario real de producao: processo A segura lock, processo B tenta.

        Nao da pra testar same-process (msvcrt.locking de outro handle no
        mesmo processo tem semantica indefinida no Windows). Subprocess
        valida o gap que importa: 2 instancias do worker.py rodando.
        """
        lock_path = str(tmp_path / "worker.lock")
        monkeypatch.setattr(worker, "LOCK_PATH", lock_path)

        first = worker.acquire_singleton_lock()
        assert first is not None
        try:
            worker_dir = os.path.dirname(worker.__file__)
            code = (
                f"import sys; sys.path.insert(0, r'{worker_dir}'); "
                f"import worker; worker.LOCK_PATH = r'{lock_path}'; "
                f"fh = worker.acquire_singleton_lock(); "
                f"sys.exit(0 if fh is None else 1)"
            )
            r = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True, timeout=15)
            assert r.returncode == 0, \
                (f"subprocess deveria sair 0 (lock indisponivel); "
                 f"saiu {r.returncode}.\nstderr={r.stderr}")
        finally:
            first.close()

    def test_after_close_lock_is_reusable(self, tmp_path, monkeypatch):
        lock_path = str(tmp_path / "worker.lock")
        monkeypatch.setattr(worker, "LOCK_PATH", lock_path)
        first = worker.acquire_singleton_lock()
        assert first is not None
        first.close()
        second = worker.acquire_singleton_lock()
        try:
            assert second is not None, "depois do close(), lock deve estar livre"
        finally:
            if second:
                second.close()

    def test_creates_parent_directory(self, tmp_path, monkeypatch):
        lock_path = str(tmp_path / "sub" / "deeper" / "worker.lock")
        monkeypatch.setattr(worker, "LOCK_PATH", lock_path)
        fh = worker.acquire_singleton_lock()
        try:
            assert fh is not None
            assert os.path.exists(lock_path)
        finally:
            if fh:
                fh.close()
