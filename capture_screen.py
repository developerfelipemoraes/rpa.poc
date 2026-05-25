from botcity.core import DesktopBot
from datetime import datetime

bot = DesktopBot()
filename = f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
bot.screenshot(filename)

print(f"Screenshot salvo: {filename}")
print()
print("Proximos passos:")
print("  1. Abra o PNG no Paint ou em qualquer editor de imagem")
print("  2. Recorte SOMENTE o icone 'Contabilidade' (inclua o texto abaixo do icone)")
print("  3. Salve o recorte como: resources/btn_contabilidade.png")
print("  4. Rode: python bot.py")
