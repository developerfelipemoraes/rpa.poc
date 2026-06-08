import os
import sys
import json
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from botcity.core import DesktopBot
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

PROJECT_DIR = Path(__file__).parent
PROFILE_DIR = str(PROJECT_DIR / "playwright-profile")


class DominioBot(DesktopBot):

    # Janelas (por titulo exato) que sao transient e devem ser fechadas via
    # win32 WM_CLOSE sempre que aparecerem. Mais robusto que image matching.
    _KNOWN_DISMISSABLE_WINDOWS = [
        ("Erro", "popup de erro generico (ex: Busca Convencoes)"),
    ]

    # Dialogos por imagem - fallback se win32 nao encontrar/fechar.
    _KNOWN_DISMISSABLE_DIALOGS = [
        ("err_busca_convencoes", "resources/err_busca_convencoes.png", "erro Busca Convencoes"),
    ]

    def action(self, execution=None):
        """Executa o fluxo e RETORNA um dict de result (consumido pelo worker.py
        via a linha-sentinela RPA_RESULT_JSON). NUNCA mais retorna None silencioso:
        cada etapa que falha vira ok=False + failed_step, e o __main__ traduz isso
        em sys.exit(1) para o worker enxergar a falha pelo returncode."""
        steps = [
            ("login_web", self.tela_login_web),
            ("lista_programas", self.tela_lista_programas),
            ("login_modulo", self.tela_login_modulo),
            ("selecionar_empresa", self.tela_selecionar_empresa),
            ("navegar_importacao", self.navegar_para_importacao),
            ("importar_arquivo", self.importar_arquivo),
        ]

        # Enquanto a importacao nao esta pronta (faltam os PNGs de menu/importar),
        # o fluxo pode parar logo APOS a selecao da empresa (F8). Ligado por env
        # RPA_ATE_EMPRESA=1 -> conclui DONE em selecionar_empresa, sem navegar/importar.
        ate_empresa = os.getenv("RPA_ATE_EMPRESA", "").strip().lower() in ("1", "true", "yes", "sim")
        if ate_empresa:
            cut = [n for n, _ in steps].index("selecionar_empresa") + 1
            steps = steps[:cut]

        # Gravacao comeca DESDE O LOGIN no Dominio (frames intra-login sao capturados
        # dentro de tela_login_web). Aqui registramos o fim de cada passo seguinte.
        for name, fn in steps:
            if not fn():
                self._record_frame(f"FALHA_{name}")
                return {
                    "ok": False,
                    "failed_step": name,
                    "imported": None,
                    "errors": None,
                    "message": f"falha na etapa '{name}'",
                }
            self._record_frame(name)

        if ate_empresa:
            print("\n=== ATE F8/EMPRESA OK (importacao desabilitada via RPA_ATE_EMPRESA) ===")
            return {
                "ok": True,
                "failed_step": None,
                "imported": None,
                "errors": None,
                "message": "parou apos selecao de empresa (F8); importacao desabilitada (RPA_ATE_EMPRESA)",
            }

        imported, errors = self._ler_resultado_importacao()
        print("\n=== FLUXO COMPLETO COM SUCESSO ===")
        return {
            "ok": True,
            "failed_step": None,
            "imported": imported,
            "errors": errors,
            "message": "fluxo completo",
        }

    def _record_frame(self, name):
        """Se RPA_RECORD_DIR estiver setado, salva um screenshot do desktop com
        indice GLOBAL incremental (NN_nome.png), preservando a ordem mesmo com
        frames capturados dentro do login. Base do video legendado da execucao."""
        rec = os.getenv("RPA_RECORD_DIR", "").strip()
        if not rec:
            return
        if not hasattr(self, "_frame_i"):
            self._frame_i = 0
        try:
            os.makedirs(rec, exist_ok=True)
            path = os.path.join(rec, f"{self._frame_i:02d}_{name}.png")
            self.screenshot(path)
            self._frame_i += 1
            print(f"  [rec] frame salvo: {path}")
        except Exception as e:
            print(f"  [rec] WARN frame '{name}' falhou: {e}")

    def _ler_resultado_importacao(self):
        """Lê os contadores reais da importação (lançamentos importados / erros) da
        tela de confirmação do Domínio Contabilidade.

        ATENÇÃO: o parsing da tela de resultado depende de capturar o template
        dessa tela na VM (1920x1080). Enquanto o template não existe, gravamos um
        screenshot 'import_result_<ts>.png' para captura e retornamos (None, None)
        — o contrato do result já carrega os campos; os números entram quando o
        leitor da tela for implementado a partir do print capturado."""
        ts = datetime.now().strftime("%H%M%S")
        try:
            self.screenshot(f"import_result_{ts}.png")
            print(f"  Tela de resultado capturada: import_result_{ts}.png "
                  f"(envie para implementar a leitura de imported/errors).")
        except Exception as e:
            print(f"  WARN: nao consegui capturar a tela de resultado: {e}")
        return None, None

    def tela_login_web(self) -> bool:
        print("\n[1/6] Login web (Playwright)...")
        usuario = os.getenv("DOMINIO_USUARIO")
        senha = os.getenv("DOMINIO_SENHA")
        if not usuario or not senha:
            print("  ERRO: defina DOMINIO_USUARIO e DOMINIO_SENHA no .env")
            return False

        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=False,
                args=["--start-maximized"],
                no_viewport=True,
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            try:
                print("  Navegando para dominioweb.com.br...")
                page.goto("https://www.dominioweb.com.br/", wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                # PRIMEIRO frame da gravacao = tela de login do Dominio.
                self._record_frame("login_dominio")

                # tela inicial "Vamos começar"
                try:
                    btn = page.get_by_role("button", name="Entrar")
                    btn.wait_for(state="visible", timeout=5000)
                    btn.click()
                    print("  Cliquei em 'Entrar' (tela inicial)")
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(2000)
                except PlaywrightTimeout:
                    print("  Tela inicial pulada (provavelmente ja passou)")

                # tela de email - filtra pelo visivel (Auth0 tem inputs duplicados)
                try:
                    email_input = page.locator('input#username:visible').first
                    email_input.wait_for(state="visible", timeout=10000)
                    email_input.fill(usuario)
                    print(f"  Email preenchido: {usuario}")
                    self._record_frame("login_email")
                    email_input.press("Enter")
                    print("  Submetido [email]")
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(2000)
                except PlaywrightTimeout:
                    print("  Tela de email pulada (talvez ja logado)")

                # tela de senha
                try:
                    senha_input = page.locator('input#password:visible').first
                    senha_input.wait_for(state="visible", timeout=10000)
                    senha_input.fill(senha)
                    print("  Senha preenchida")
                    self._record_frame("login_senha")
                    senha_input.press("Enter")
                    print("  Submetido [senha]")
                except PlaywrightTimeout:
                    print("  Tela de senha pulada (talvez ja logado)")

                # tela de aviso/manutencao pos-login (opcional - aparece eventualmente,
                # ex.: aviso de janela de manutencao com botao Continuar)
                try:
                    continuar_btn = page.get_by_role("button", name="Continuar")
                    continuar_btn.wait_for(state="visible", timeout=5000)
                    continuar_btn.click()
                    print("  Cliquei em 'Continuar' (tela de aviso pos-login)")
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(2000)
                except PlaywrightTimeout:
                    print("  Sem tela de aviso pos-login")

                # aguarda o protocolo TRComputerPluginWindows disparar
                print("  Aguardando 15s pro plugin abrir...")
                page.wait_for_timeout(15000)
                self._record_frame("plugin_abrindo")

                ctx.close()
                print("  OK: login web concluido.")
                return True
            except Exception as e:
                print(f"  ERRO no login web: {e}")
                try:
                    ctx.close()
                except Exception:
                    pass
                return False

    def tela_lista_programas(self) -> bool:
        print("\n[2/6] Tela 'Lista de Programas' - abrindo Contabilidade...")
        if not self._find_or_debug("btn_contabilidade", "resources/btn_contabilidade.png",
                                   matching=0.75, waiting=30000, label="icone Contabilidade"):
            return False
        self.double_click()
        print("  OK: duplo-clique em Contabilidade.")
        return True

    def tela_login_modulo(self) -> bool:
        print("\n[3/6] Login do modulo Contabilidade (Conectando...)...")
        self.add_image("anchor_nome_usuario", "resources/btn_nome_usuario.png")
        self.add_image("btn_ok_modulo", "resources/btn_modulo_ok.png")

        if not self.find("anchor_nome_usuario", matching=0.70, waiting_time=40000):
            print("  ERRO: dialog 'Conectando' nao encontrado.")
            return False

        usuario = os.getenv("MODULO_USUARIO", "GERENTE")
        senha = os.getenv("MODULO_SENHA", "GERENTE")

        self.click()
        self.wait(300)
        self.control_a()
        self.wait(100)
        self.kb_type(usuario)
        self.wait(200)

        self.tab()
        self.wait(300)
        self.control_a()
        self.wait(100)
        self.kb_type(senha)
        self.wait(300)

        # tenta achar o botao OK por imagem (relaxa threshold pq pode estar com foco)
        if self.find("btn_ok_modulo", matching=0.60, waiting_time=5000):
            self.click()
            print("  OK clicado por imagem.")
        else:
            # fallback: Enter dispara o botao default (OK) em qualquer dialog Win32
            print("  Botao OK nao encontrado por imagem. Usando ENTER (botao default).")
            self.enter()
        self.wait(5000)
        return True

    # ------------------------------------------------------------------
    # Helper: adiciona a imagem, procura, e em caso de falha salva um
    # screenshot de debug (padronza o tratamento "caso algo quebre").
    # ------------------------------------------------------------------
    def _find_or_debug(self, name, path, matching=0.80, waiting=30000, label=""):
        # SEMPRE dismissa dialogos conhecidos primeiro - mesmo nos passos novos
        # onde o PNG-template ainda nao existe, queremos que o debug seja salvo
        # da tela SEM o popup (pra a gente conseguir capturar o elemento real).
        self._dismiss_any_known_dialog()

        # Se o PNG-template ainda nao existe (passo novo sem captura), salva
        # um screenshot da tela atual pra identificar o elemento a capturar.
        if not os.path.isfile(path):
            ts = datetime.now().strftime("%H%M%S")
            shot = f"debug_{name}_MISSING_{ts}.png"
            try:
                self.screenshot(shot)
            except Exception as e:
                print(f"  WARN: nao consegui salvar screenshot: {e}")
            print(f"  ERRO: PNG '{path}' nao existe. Capture a tela atual ({shot}) pra gerar o template.")
            return False

        self.add_image(name, path)

        # Polling: procura em chunks de 5s, intercalando com quick-dismiss (500ms).
        # Captura dialogos transient que aparecam DURANTE a espera, nao so antes.
        poll_ms = 5000
        elapsed = 0
        while elapsed < waiting:
            chunk = min(poll_ms, waiting - elapsed)
            if self.find(name, matching=matching, waiting_time=chunk):
                return True
            elapsed += chunk
            if elapsed < waiting:
                # chunk falhou - pode ter aparecido dialog blocking. Quick check.
                self._dismiss_any_known_dialog(waiting=500)

        ts = datetime.now().strftime("%H%M%S")
        shot = f"debug_{name}_{ts}.png"
        self.screenshot(shot)
        print(f"  ERRO: '{label or name}' nao encontrado. Debug: {shot}")
        return False

    # ------------------------------------------------------------------
    # Helper: detecta dialogo opcional (erro/aviso transient) e dismissa
    # com ENTER (botao default OK em dialog Win32). Se o PNG nao existir
    # ou o dialogo nao aparecer, segue silenciosamente.
    # ------------------------------------------------------------------
    def _dismiss_dialog_if_present(self, name, path, label="", matching=0.75, waiting=2000):
        if not os.path.isfile(path):
            return False
        self.add_image(name, path)
        if self.find(name, matching=matching, waiting_time=waiting):
            print(f"  Dialog detectado ({label or name}) -> ENTER (OK default).")
            self.enter()
            self.wait(800)
            return True
        return False

    def _close_window_by_title(self, title):
        """Fecha janela com titulo EXATO via win32 WM_CLOSE. Mais robusto que
        image matching - nao depende de captura/threshold/DPI. Retorna True
        se a janela foi encontrada visivel e o close foi enviado.

        NOTA: so funciona pra janelas TOP-LEVEL. MDI childs (ex: 'Dashboard'
        dentro do Contabilidade) nao sao encontrados por FindWindow direto."""
        try:
            import win32gui
            import win32con
        except ImportError:
            return False
        try:
            hwnd = win32gui.FindWindow(None, title)
            if hwnd and win32gui.IsWindowVisible(hwnd):
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                self.wait(800)
                return True
        except Exception as e:
            print(f"  WARN: win32 close '{title}' falhou: {e}")
        return False

    def _alt_key(self, letter):
        """Envia ALT+<letter> (mnemonic activator de dialog Win32). Usa pyautogui
        direto porque botcity DesktopBot nao expoe um key_combo generico."""
        import pyautogui
        pyautogui.hotkey("alt", letter)

    def _ctrl_f4(self):
        """Ctrl+F4 = atalho Windows pra fechar MDI child window (ex: form
        'Dashboard' dentro do app Contabilidade)."""
        import pyautogui
        pyautogui.hotkey("ctrl", "f4")

    def _dismiss_any_known_dialog(self, waiting=2000):
        """Tenta fechar dialogos/janelas conhecidos. Win32 (por titulo exato)
        primeiro - mais robusto. Image matching como fallback."""
        closed = False
        # 1. Win32 WM_CLOSE - por titulo. Mais confiavel pra Win32 dialogs.
        for title, label in self._KNOWN_DISMISSABLE_WINDOWS:
            if self._close_window_by_title(title):
                print(f"  Janela '{title}' fechada via win32 ({label}).")
                closed = True
        # 2. Image matching - fallback caso win32 nao encontre o titulo.
        for name, path, label in self._KNOWN_DISMISSABLE_DIALOGS:
            if self._dismiss_dialog_if_present(name, path, label=label, waiting=waiting):
                closed = True
        return closed

    # ------------------------------------------------------------------
    # [4/6] Selecao da empresa
    # TODO: capturar o(s) PNG(s) na VM (1920x1080) e ajustar nomes/threshold.
    # ------------------------------------------------------------------
    def tela_selecionar_empresa(self) -> bool:
        print("\n[4/6] Selecionando a empresa...")
        # O modulo Contabilidade demora a carregar depois do login - da um tempo
        # antes de procurar a tela de empresa.
        print("  Aguardando o modulo Contabilidade terminar de carregar (15s)...")
        self.wait(15000)

        # Fecha popups e o Dashboard que bloqueiam a tela principal do Contabilidade.
        # Dashboard eh MDI child (FindWindow nao acha) -> fallback via Ctrl+F4.
        self._dismiss_any_known_dialog()
        if self._close_window_by_title("Dashboard"):
            print("  Janela 'Dashboard' fechada via WM_CLOSE.")
        else:
            print("  Tentando fechar Dashboard via Ctrl+F4 (MDI child close)...")
            self._ctrl_f4()
        self.wait(2000)
        self._dismiss_any_known_dialog()
        self.wait(1000)

        # F8 = atalho do Dominio Contabilidade pra abrir o dialogo de selecao
        # de empresa. Sequencia: F8 -> ALT+C (radio 'Codigo') -> TAB (caixa de
        # texto) -> digita codigo -> ENTER (seleciona empresa highlighted, eh
        # equivalente ao duplo-clique na empresa filtrada).
        empresa = os.getenv("EMPRESA_CODIGO", "").strip()
        if not empresa:
            print("  ERRO: EMPRESA_CODIGO nao definido no .env (precisa do codigo da empresa).")
            return False

        print(f"  F8 -> abrindo dialogo de empresa (alvo: codigo {empresa})...")
        self.key_f8()
        self.wait(2500)

        # ALT+C = mnemonic do radio 'Codigo' (C sublinhado). Idempotente:
        # se ja estiver selecionado, nada muda.
        print("  ALT+C -> garantindo modo de busca por 'Codigo'...")
        self._alt_key("c")
        self.wait(600)

        # TAB sai do radio e cai na caixa de texto do filtro.
        self.tab()
        self.wait(300)

        # Digita o codigo da empresa - lista filtra em tempo real.
        print(f"  Digitando codigo {empresa}...")
        self.kb_type(empresa)
        self.wait(2500)  # tempo da lista filtrar

        # ENTER = seleciona empresa highlighted (equivalente ao duplo-clique).
        print("  ENTER -> selecionando empresa filtrada (equivale a duplo-clique)...")
        self.enter()
        self.wait(3000)

        print(f"  OK: empresa {empresa} selecionada (via F8 + ALT+C + ENTER).")
        return True

    # ------------------------------------------------------------------
    # [5/6] Navega pelo menu ate a tela de importacao
    # TODO: ajustar o caminho do menu (ex: Movimentos -> Importacao -> ...).
    # ------------------------------------------------------------------
    def navegar_para_importacao(self) -> bool:
        print("\n[5/6] Navegando ate a tela de importacao...")
        passos = [
            ("menu_movimentos", "resources/menu_movimentos.png", "menu Movimentos"),
            ("menu_importacao", "resources/menu_importacao.png", "menu Importacao"),
        ]
        for name, png, label in passos:
            if not self._find_or_debug(name, png, matching=0.80, waiting=20000, label=label):
                return False
            self.click()
            self.wait(800)
        print("  OK: tela de importacao aberta.")
        return True

    # ------------------------------------------------------------------
    # [6/6] Importa o arquivo (botao Importar -> dialogo "Abrir" -> confirma)
    # TODO: confirmar origem/caminho do arquivo e o passo de confirmacao.
    # ------------------------------------------------------------------
    def importar_arquivo(self) -> bool:
        print("\n[6/6] Importando o arquivo...")
        arquivo = os.getenv("ARQUIVO_IMPORTACAO", r"C:\rpa\app\dados\importar.txt")
        if not Path(arquivo).exists():
            print(f"  ERRO: arquivo de importacao nao encontrado: {arquivo}")
            return False
        if not self._find_or_debug("btn_importar", "resources/btn_importar.png",
                                   matching=0.80, waiting=20000, label="botao Importar"):
            return False
        self.click()
        self.wait(1500)
        # dialogo Win32 "Abrir": digita o caminho e confirma (mesmo padrao do login do modulo)
        self.kb_type(arquivo)
        self.wait(300)
        self.enter()
        self.wait(2000)
        # TODO: confirmar a importacao + validar sucesso, ex:
        #   if self._find_or_debug("btn_confirmar_import", "resources/btn_confirmar_import.png", waiting=10000):
        #       self.click()
        print(f"  OK: importacao disparada para {arquivo}.")
        return True


if __name__ == "__main__":
    bot = DominioBot()
    try:
        result = bot.action()
    except Exception as e:
        result = {
            "ok": False,
            "failed_step": "exception",
            "imported": None,
            "errors": None,
            "message": f"{type(e).__name__}: {e}",
        }

    # Linha-sentinela: o worker.py faz parse de RPA_RESULT_JSON no stdout para
    # enriquecer o result do job (imported/errors/failed_step/message).
    print("RPA_RESULT_JSON:" + json.dumps(result, ensure_ascii=False), flush=True)

    # Exit code = contrato com o worker (run_bot considera ok = returncode == 0).
    sys.exit(0 if result.get("ok") else 1)
