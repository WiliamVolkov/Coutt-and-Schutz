import os
from threading import Thread

import discord
from discord.ext import commands
from flask import Flask
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# Token configurado en Render
TOKEN = os.getenv("DISCORD_TOKEN")

# Servidor web para Render
app = Flask(__name__)


@app.route("/")
def home():
    return "Coutt-and-Schutz: servidor web activo."


def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    Thread(target=run_web, daemon=True).start()


# Configuración del bot
intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# Canal de bienvenida
CANAL_BIENVENIDA_ID = 1558770818433949769


@bot.event
async def on_ready():
    print(f"Bot conectado como {bot.user}")


@bot.event
async def on_member_join(member):
    canal = bot.get_channel(CANAL_BIENVENIDA_ID)

    if canal is None:
        try:
            canal = await bot.fetch_channel(CANAL_BIENVENIDA_ID)
        except discord.HTTPException as error:
            print(f"No se pudo encontrar el canal de bienvenida: {error}")
            return

    mensaje = (
        f"🚗 ¡Bienvenido/a a Coutt-and-Schutz, {member.mention}! 🎉\n\n"
        f"Nos alegra tenerte por aquí. Te invitamos a visitar "
        f"nuestro concesionario y descubrir nuestros vehículos.\n\n"
        f"¡Esperamos que encuentres el vehículo de tus sueños!"
    )

    try:
        await canal.send(mensaje)
        print(f"Bienvenida enviada para {member} ({member.id})")
    except discord.HTTPException as error:
        print(f"No se pudo enviar la bienvenida: {error}")


# Inicio del programa
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "Falta la variable de entorno DISCORD_TOKEN. "
            "Configúrala en Render."
        )

    keep_alive()
    bot.run(TOKEN)
