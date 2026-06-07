"""Bot FALSO para testar o contrato worker <-> bot SEM o Dominio/GUI.

Honra exatamente o mesmo contrato que o bot.py real:
  - le EMPRESA_CODIGO + ARQUIVO_IMPORTACAO do env (e ecoa pra provar que chegaram)
  - emite a linha-sentinela 'RPA_RESULT_JSON:{...}' no stdout
  - sai com exit code 0 (sucesso) ou != 0 (falha)

Comportamento parametrizavel por env (default = sucesso):
  FAKE_BOT_EXIT        exit code (default "0")
  FAKE_BOT_IMPORTED    inteiro -> result.imported (default None)
  FAKE_BOT_ERRORS      inteiro -> result.errors (default None)
  FAKE_BOT_FAILED_STEP string -> result.failed_step (default None)
  FAKE_BOT_MESSAGE     string -> result.message
"""

import os
import sys
import json


def _maybe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def main():
    exit_code = int(os.getenv("FAKE_BOT_EXIT", "0"))

    # Ecoa o que o worker injetou (o teste valida que essas linhas aparecem).
    print(f"EMPRESA_CODIGO={os.getenv('EMPRESA_CODIGO')}")
    print(f"ARQUIVO_IMPORTACAO={os.getenv('ARQUIVO_IMPORTACAO')}")

    result = {
        "ok": exit_code == 0,
        "failed_step": os.getenv("FAKE_BOT_FAILED_STEP") or None,
        "imported": _maybe_int(os.getenv("FAKE_BOT_IMPORTED")),
        "errors": _maybe_int(os.getenv("FAKE_BOT_ERRORS")),
        "message": os.getenv("FAKE_BOT_MESSAGE", "fake bot run"),
    }
    print("RPA_RESULT_JSON:" + json.dumps(result, ensure_ascii=False), flush=True)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
