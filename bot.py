import os
import sys
import json
import subprocess
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
        ("Atenção", "aviso modal (ex: 'configurar empresa para Dashboards')"),
        ("Aviso", "aviso modal generico"),
        ("Informação", "info modal generico"),
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
        # MINIMIZA o console do worker: lancado pela ScheduledTask, ele fica no
        # CENTRO da tela e COBRE o launcher/Contabilidade, fazendo o image-match
        # falhar (icone escondido atras do console). Sem isso o worker quebra em
        # 'lista_programas' mesmo o fluxo estando certo.
        self._minimizar_consoles()
        # Idempotencia: garante estado LIMPO antes de comecar (fecha Dominio/
        # Contabilidade que tenha sobrado de uma execucao anterior). Sem isso, o
        # passo 'lista_programas' falha quando a Contabilidade ja esta aberta.
        self._limpar_estado_inicial()

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

    def _minimizar_consoles(self):
        """Minimiza janelas de console (ConsoleWindowClass). A do worker fica no
        centro e COBRE o launcher/Contabilidade -> image-match nao acha o icone."""
        try:
            import win32gui
            import win32con
        except ImportError:
            return

        def cb(h, _):
            if (win32gui.IsWindowVisible(h)
                    and win32gui.GetClassName(h) == "ConsoleWindowClass"):
                try:
                    win32gui.ShowWindow(h, win32con.SW_MINIMIZE)
                    print(f"  [console] minimizado: '{win32gui.GetWindowText(h)}'")
                except Exception:
                    pass
        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            pass

    def _limpar_estado_inicial(self):
        """Idempotencia de estado: fecha qualquer janela do Dominio/Contabilidade
        remanescente de uma execucao anterior e dispensa dialogos residuais, para
        o run comecar do zero. Best-effort: NUNCA levanta excecao.

        Por que: o Dominio Contabilidade e um app desktop persistente. Se um run
        anterior deixou a Contabilidade aberta, o passo 'lista_programas' nao acha
        o icone para ABRIR (ela ja esta aberta) e falha. Aqui fechamos primeiro
        via WM_CLOSE (gracioso) e, se resistir, matamos o processo dono da janela."""
        print("\n[0/6] Limpando estado anterior (fecha Dominio/Contabilidade aberto)...")
        try:
            import win32gui
            import win32con
            import win32process
        except ImportError:
            print("  win32 indisponivel; pulando limpeza.")
            return

        # Marcadores de titulo de janelas do Dominio (case-insensitive).
        markers = ("domínio", "dominio", "contabil", "thomson reuters",
                   "trcomputer", "tr internet", "trinternet", "dashboard",
                   "conectando", "lista de programas")

        def _alvos():
            achados = []

            def cb(hwnd, _):
                try:
                    if not win32gui.IsWindowVisible(hwnd):
                        return
                    t = (win32gui.GetWindowText(hwnd) or "").strip()
                    if t and any(m in t.lower() for m in markers):
                        achados.append((hwnd, t))
                except Exception:
                    pass
            win32gui.EnumWindows(cb, None)
            return achados

        alvos = _alvos()
        if not alvos:
            print("  Nada do Dominio aberto. Estado ja limpo.")
            self._dismiss_any_known_dialog(waiting=500)
            return

        # 1) fecho gracioso via WM_CLOSE
        for hwnd, t in alvos:
            print(f"  Fechando janela: '{t}'")
            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        self.wait(3000)
        self._dismiss_any_known_dialog(waiting=800)  # eventuais "deseja sair?"/erros
        self.wait(1500)

        # 2) sobreviventes -> mata o processo dono da janela (taskkill /F)
        for hwnd, t in _alvos():
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid:
                    print(f"  Janela resistiu; matando PID {pid} ('{t}')")
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True)
            except Exception as e:
                print(f"  WARN kill falhou: {e}")
        self.wait(2000)
        print("  Limpeza concluida.")

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
        # Garante que o console nao esteja cobrindo os icones do launcher.
        self._minimizar_consoles()
        self.wait(400)
        if not self._find_or_debug("btn_contabilidade", "resources/btn_contabilidade.png",
                                   matching=0.75, waiting=30000, label="icone Contabilidade"):
            return False
        # No app streamed o double_click as vezes so SELECIONA o icone (fica azul)
        # sem abrir (foi o que travou a execucao a6b574ea). Clicar (seleciona) +
        # Enter (abre o icone selecionado) e mais confiavel que o duplo-clique.
        self.click()
        self.wait(600)
        self.enter()
        self.wait(1500)
        print("  OK: Contabilidade aberta (click + Enter).")
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

    def _press(self, key):
        """Pressiona uma tecla unica (ex: mnemonic de item de menu ja aberto)."""
        import pyautogui
        pyautogui.press(key)

    def _dismiss_dominio_modais(self):
        """Fecha os modais do Dominio (janelas top-level 'DisplayClientWindowClass'
        com titulo EXATO 'Atenção'/'Erro'/'Aviso'/...). Foca CADA modal e pressiona
        Enter (= botao OK). NUNCA usa WM_CLOSE: no Dominio isso derruba o app todo.
        Retorna quantos modais tratou."""
        try:
            import win32gui
        except ImportError:
            return 0
        import pyautogui
        import time
        MODAIS = ("atenção", "atencao", "aviso", "erro", "informação",
                  "informacao", "confirmação", "confirmacao", "mensagem")
        achados = []

        def cb(h, _):
            if (win32gui.IsWindowVisible(h)
                    and win32gui.GetClassName(h) == "DisplayClientWindowClass"):
                t = win32gui.GetWindowText(h) or ""
                if t.strip().lower() in MODAIS:   # titulo EXATO -> nao pega o app principal
                    achados.append((h, t))
        win32gui.EnumWindows(cb, None)

        for h, t in achados:
            print(f"  Modal '{t}' -> Enter (OK)")
            try:
                win32gui.SetForegroundWindow(h)
                time.sleep(0.3)
                pyautogui.press("enter")
                time.sleep(0.5)
            except Exception as e:
                # se nao focar, tenta clicar no centro-baixo do modal (onde fica o OK)
                try:
                    l, top, r, b = win32gui.GetWindowRect(h)
                    pyautogui.click((l + r) // 2, b - 25)
                    print(f"  Modal '{t}' -> clique no OK (foco falhou: {e})")
                except Exception:
                    print(f"  WARN modal '{t}': {e}")
        return len(achados)

    def _focar_dominio(self):
        """Traz a janela do Dominio (classe 'DisplayClientWindowClass', app streamed
        pelo plugin) para frente, para que teclado/atalhos cheguem NELA e nao no
        console. Escolhe a MAIOR janela dessa classe (= janela principal do app).
        Retorna True se conseguiu focar."""
        try:
            import win32gui
            import win32con
        except ImportError:
            return False
        cands = []
        # NUNCA focar o launcher 'Lista de Programas' nem modais: o alvo e a janela
        # do MODULO (Contabilidade), que vem com titulo VAZIO. Quando as duas estao
        # abertas, focar a maior pegava o launcher (bug) -> F8/menu iam pro lugar errado.
        EXCLUIR = ("lista de programas", "atenção", "atencao", "aviso", "erro",
                   "informação", "informacao", "confirmação", "confirmacao", "mensagem")

        def cb(h, _):
            if not win32gui.IsWindowVisible(h):
                return
            if win32gui.GetClassName(h) == "DisplayClientWindowClass":
                t = (win32gui.GetWindowText(h) or "").strip()
                if t.lower() in EXCLUIR:
                    return  # pula launcher e modais
                l, top, r, b = win32gui.GetWindowRect(h)
                cands.append(((r - l) * (b - top), h, t))
        win32gui.EnumWindows(cb, None)
        if not cands:
            print("  [foco] janela do MODULO (Contabilidade) nao encontrada (so launcher/modal?)")
            return False
        cands.sort(reverse=True)  # maior modulo = Contabilidade
        _, hwnd, txt = cands[0]
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
            print(f"  [foco] Dominio em foco: '{txt or '(sem titulo)'}'")
            return True
        except Exception as e:
            # SetForegroundWindow pode falhar por restricao do Windows; clicar tambem foca.
            try:
                import pyautogui
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                pyautogui.click((l + r) // 2, t + 8)  # clica na barra de titulo
                print(f"  [foco] foco via clique (SetForeground falhou: {e})")
                return True
            except Exception:
                print(f"  [foco] WARN nao consegui focar o Dominio: {e}")
                return False

    def _fechar_dashboard_e_modais(self):
        """Pre-F8: dispensa modais (ex: 'Atenção') e fecha o Dashboard mirando os
        HWNDs REAIS (nao por titulo fixo), porque o Dominio e Delphi e as classes/
        titulos nao sao os padrao do Windows. Loga TODAS as janelas visiveis
        (titulo|classe) em C:\\rpa\\windows_dump.txt para diagnostico. Best-effort."""
        try:
            import win32gui
            import win32con
        except ImportError:
            print("  win32 indisponivel; pulando limpeza pre-F8.")
            return
        import pyautogui

        dumpfile = r"C:\rpa\windows_dump.txt"
        titulos_modal = ("atenção", "atencao", "aviso", "erro", "informação",
                         "informacao", "confirmação", "confirmacao", "mensagem")

        def _tops():
            out = []

            def cb(h, _):
                if win32gui.IsWindowVisible(h):
                    out.append((h, win32gui.GetClassName(h) or "",
                                win32gui.GetWindowText(h) or ""))
            win32gui.EnumWindows(cb, None)
            return out

        def _children(parent):
            out = []

            def cb(h, _):
                out.append((h, win32gui.GetClassName(h) or "",
                            win32gui.GetWindowText(h) or ""))
            try:
                win32gui.EnumChildWindows(parent, cb, None)
            except Exception:
                pass
            return out

        for i in range(3):
            tops = _tops()
            # --- dump diagnostico (cross-session: leio depois via run-command) ---
            try:
                with open(dumpfile, "a", encoding="utf-8") as f:
                    f.write(f"\n--- passada {i} @ {datetime.now():%H:%M:%S} ---\n")
                    for h, c, t in tops:
                        f.write(f"TOP cls='{c}' txt='{t}'\n")
                        for _hc, cc, tc in _children(h):
                            if tc:
                                f.write(f"   CHILD cls='{cc}' txt='{tc}'\n")
            except Exception:
                pass

            # 1) Dispensa o(s) modal(is) do Dominio (Atenção/Erro/...) focando CADA
            #    UM pelo titulo exato e dando Enter (OK). Antes eu focava o app
            #    principal (maior janela) e o Enter ia pra ele, nao pro modal.
            n_modais = self._dismiss_dominio_modais()
            # 2) Fecha o Dashboard (MDI child): foca o app principal + Ctrl+F4.
            self._focar_dominio()
            self.wait(400)
            pyautogui.hotkey("ctrl", "f4")
            self.wait(900)
            # Para quando nao houver mais modal (apos a 1a passada).
            if n_modais == 0 and i >= 1:
                break

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
        # O modulo Contabilidade demora bastante para carregar apos o login.
        # Esperar 40s (configuravel) ANTES de fechar mensagens do Dashboard / F8.
        load_wait = int(os.getenv("CONTAB_LOAD_WAIT_MS", "40000"))
        print(f"  Aguardando o modulo Contabilidade terminar de carregar ({load_wait // 1000}s)...")
        self.wait(load_wait)

        # Ao abrir, a Contabilidade mostra o Dashboard + um modal "Atenção"
        # ('É necessário configurar a empresa para emissão dos Dashboards').
        # A ORDEM IMPORTA: o modal precisa sair ANTES, senao o Ctrl+F4 nao fecha
        # o Dashboard (o modal rouba o foco) e o F8 nunca abre.
        print("  Limpando modal 'Atencao' + Dashboard antes do F8...")
        self._fechar_dashboard_e_modais()
        self.wait(800)
        # Garante foco na janela da CONTABILIDADE (nao no launcher) antes do F8.
        self._focar_dominio()
        self.wait(500)

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

        # Botao "Acessar" (mnemonic ALT+A) confirma a empresa filtrada.
        # ENTER fazia duplo-clique e ABRIA a janela da empresa (errado).
        print("  ALT+A -> botao 'Acessar' (confirma a empresa filtrada)...")
        self._alt_key("a")
        self.wait(3000)

        print(f"  OK: empresa {empresa} acessada (F8 + codigo + Acessar).")
        return True

    # ------------------------------------------------------------------
    # [5/6] Menu: Utilitarios > Importacao > Importador > Importar
    # Mnemonics (letra sublinhada) CONFIRMADOS na tela:
    #   - Utilitarios: ALT+U
    #   - Importacao : 'i' (unico 'I' do menu Utilitarios; 'm' la pega 'Onvio Messenger')
    #   - Importador : 'm' (no submenu de Importacao; 'i'/'I' la sao ambiguos)
    #   - Importar   : 'i' (unico 'I' no submenu do Importador)
    # ------------------------------------------------------------------
    def navegar_para_importacao(self) -> bool:
        print("\n[5/6] Menu Utilitarios > Importacao(i) > Importador(m) > Importar(i)...")
        # Apos selecionar a empresa, a Contabilidade abre 'Processando Dashboard'
        # (Carregando as informacoes dos modulos) e pode reabrir o modal 'Atenção'.
        # Esses bloqueiam o menu. Espera o carregamento e limpa ANTES do ALT+U.
        load_wait = int(os.getenv("DASHBOARD_LOAD_WAIT_MS", "15000"))
        print(f"  Aguardando 'Processando Dashboard' carregar ({load_wait // 1000}s)...")
        self.wait(load_wait)
        self._fechar_dashboard_e_modais()   # fecha Dashboard/Atenção
        self._focar_dominio()               # foca a Contabilidade (nao o launcher)
        self.wait(800)

        print("  ALT+U -> Utilitarios")
        self._alt_key("u")
        self.wait(1200)
        self._record_frame("menu_utilitarios")

        print("  i -> Importacao (abre submenu)")
        self._press("i")
        self.wait(1200)
        self._record_frame("menu_importacao")

        print("  m -> Importador (abre submenu)")
        self._press("m")
        self.wait(1200)
        self._record_frame("menu_importador")

        print("  i -> Importar (executa; abre dialogo de arquivo)")
        self._press("i")
        self.wait(1500)
        self._record_frame("menu_importar")

        print("  OK: 'Importar' acionado (dialogo de arquivo deve abrir).")
        return True

    # ------------------------------------------------------------------
    # [6/6] Dialogo de arquivo aberto pelo 'Importar': informa o arquivo da
    # empresa baixada e confirma.
    # NOTA: no fluxo manual o dialogo "sobe uma pasta" antes de achar o arquivo.
    # Aqui digitamos o CAMINHO COMPLETO no campo 'nome do arquivo' (mais robusto
    # que navegar pastas). A ser refinado conforme o dialogo real na VM.
    # ------------------------------------------------------------------
    def importar_arquivo(self) -> bool:
        print("\n[6/6] Importando o arquivo no dialogo...")
        arquivo = os.getenv("ARQUIVO_IMPORTACAO", r"C:\rpa\inbox\notas_teste.txt")
        if not Path(arquivo).exists():
            print(f"  ERRO: arquivo de importacao nao encontrado: {arquivo}")
            return False

        self.wait(1500)
        self._record_frame("dialogo_arquivo")

        # Campo "nome do arquivo": digita o caminho completo e confirma.
        print(f"  Digitando caminho do arquivo: {arquivo}")
        self.kb_type(arquivo)
        self.wait(500)
        self.enter()
        self.wait(2500)
        self._record_frame("apos_importar")

        # TODO (com o usuario): confirmar a tela de confirmacao da importacao
        # e ler imported/errors em _ler_resultado_importacao().
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
