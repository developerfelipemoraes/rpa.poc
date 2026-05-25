import os
from pathlib import Path
from dotenv import load_dotenv
from botcity.core import DesktopBot
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

PROJECT_DIR = Path(__file__).parent
PROFILE_DIR = str(PROJECT_DIR / "playwright-profile")


class DominioBot(DesktopBot):

    def action(self, execution=None):
        if not self.tela_login_web():
            return
        if not self.tela_lista_programas():
            return
        if not self.tela_login_modulo():
            return

        print("\n=== FLUXO COMPLETO COM SUCESSO ===")

    def tela_login_web(self) -> bool:
        print("\n[1/3] Login web (Playwright)...")
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
                    senha_input.press("Enter")
                    print("  Submetido [senha]")
                except PlaywrightTimeout:
                    print("  Tela de senha pulada (talvez ja logado)")

                # aguarda o protocolo TRComputerPluginWindows disparar
                print("  Aguardando 15s pro plugin abrir...")
                page.wait_for_timeout(15000)

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
        print("\n[2/3] Tela 'Lista de Programas' - abrindo Contabilidade...")
        self.add_image("btn_contabilidade", "resources/btn_contabilidade.png")
        if not self.find("btn_contabilidade", matching=0.75, waiting_time=30000):
            print("  ERRO: icone Contabilidade nao encontrado.")
            from datetime import datetime
            debug_path = f"debug_lista_programas_{datetime.now().strftime('%H%M%S')}.png"
            self.screenshot(debug_path)
            print(f"  DEBUG: screenshot salvo em {debug_path}")
            return False
        self.double_click()
        print("  OK: duplo-clique em Contabilidade.")
        return True

    def tela_login_modulo(self) -> bool:
        print("\n[3/3] Login do modulo Contabilidade (Conectando...)...")
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


if __name__ == "__main__":
    DominioBot.main()
