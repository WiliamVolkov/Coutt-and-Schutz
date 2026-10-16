import os
from flask import Flask
from threading import Thread
import discord
from discord.ext import commands
from dotenv import load_dotenv

# Cargar variables de entorno localmente o desde Render
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Servidor web pequeño para Render
app = Flask('')

@app.route('/')
def home():
    return "Bot en línea 24/7"

def run_web():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_web)
    t.start()

# Configuración del Bot
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

CANAL_BIENVENIDA_ID = 1558770818433949769
CANAL_IMPORTADOS_ID = 1560447082060447835
CANAL_USADOS_ID = 1560447325912961214
CANAL_CONTACTO_ID = 1560447380636176525

@bot.event
async def on_ready():
    print(f"¡Bot del concesionario encendido como {bot.user}!")

@bot.event
async def on_member_join(member):
    canal = bot.get_channel(CANAL_BIENVENIDA_ID)
    if canal:
        mensaje = (
            f"🚗 ¡Bienvenido/a a nuestro concesionario, {member.mention}! 🎉\n\n"
            f"Nos alegra un montón tenerte por aquí. Te recomendamos visitar:\n"
            f"• 🏎️ <#{CANAL_IMPORTADOS_ID}>\n"
            f"• 🚙 <#{CANAL_USADOS_ID}>\n"
            f"• 📞 <#{CANAL_CONTACTO_ID}>\n\n"
            f"¡Esperamos que encuentres el vehículo de tus sueños!"
        )
        await canal.send(mensaje)

@bot.command()
async def testbienvenida(ctx):
    await on_member_join(ctx.author)

# Iniciar servidor web y bot
keep_alive()
bot.run(MTU2MDQ0MzAyMzM1ODE3MzI2NA.GXEcDm.S2WHiMCqBYKoLSHKXwqLAT9lqSX2XwOCbuJzuk)