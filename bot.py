
import os
from threading import Thread
from urllib.parse import urlparse

import discord
from discord.ext import commands
from flask import Flask
from dotenv import load_dotenv

# ==========================================
# CONFIGURACIÓN
# ==========================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# Canal donde aparecerá el panel de administración
CANAL_PANEL_ID = 1560513189438029935

# Canales de publicación
CANAL_USADOS_ID = 1560447325912961214
CANAL_IMPORTADOS_ID = 1560447082060447835

# Identificador para reconocer el panel existente
MARCA_PANEL = "COUTT_SCHUTZ_PANEL_V1"

# ==========================================
# SERVIDOR WEB PARA RENDER
# ==========================================

app = Flask(__name__)


@app.route("/")
def home():
    return "Coutt-and-Schutz: servidor web activo."


def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    Thread(target=run_web, daemon=True).start()


# ==========================================
# CONFIGURACIÓN DEL BOT
# ==========================================

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

panel_inicializado = False
vista_registrada = False


def es_administrador(interaction: discord.Interaction) -> bool:
    return (
        interaction.guild is not None
        and interaction.user.guild_permissions.administrator
    )


async def obtener_canal(canal_id: int):
    canal = bot.get_channel(canal_id)

    if canal is None:
        canal = await bot.fetch_channel(canal_id)

    return canal


# ==========================================
# FORMULARIO PARA PUBLICAR VEHÍCULOS
# ==========================================

class FormularioVehiculo(discord.ui.Modal):

    def __init__(self, tipo: str, canal_destino_id: int):
        super().__init__(title=f"Publicar vehículo {tipo}")

        self.tipo = tipo
        self.canal_destino_id = canal_destino_id

        self.nombre = discord.ui.TextInput(
            label="Nombre del vehículo",
            placeholder="Ej.: Pegassi Zentorno",
            required=True,
            max_length=100
        )

        self.precio = discord.ui.TextInput(
            label="Precio de venta",
            placeholder="Ej.: $ 1.000.000",
            required=True,
            max_length=50
        )

        self.descripcion = discord.ui.TextInput(
            label="Descripción (opcional)",
            placeholder="Describe el vehículo...",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=1000
        )

        self.imagen = discord.ui.TextInput(
            label="URL de imagen (opcional)",
            placeholder="https://ejemplo.com/vehiculo.png",
            required=False,
            max_length=400
        )

        self.add_item(self.nombre)
        self.add_item(self.precio)
        self.add_item(self.descripcion)
        self.add_item(self.imagen)

    async def on_submit(self, interaction: discord.Interaction):

        # Verificar permisos al enviar
        if not es_administrador(interaction):
            await interaction.response.send_message(
                "No tienes permiso para publicar vehículos.",
                ephemeral=True
            )
            return

        nombre = self.nombre.value.strip()
        precio = self.precio.value.strip()
        descripcion = self.descripcion.value.strip()
        imagen_url = self.imagen.value.strip()

        if not nombre or not precio:
            await interaction.response.send_message(
                "El nombre y el precio son obligatorios.",
                ephemeral=True
            )
            return

        # Validar la dirección de la imagen
        if imagen_url:
            parsed_url = urlparse(imagen_url)

            if (
                parsed_url.scheme not in ("http", "https")
                or not parsed_url.netloc
            ):
                await interaction.response.send_message(
                    "La URL de imagen no es válida. "
                    "Debe comenzar por http:// o https://.",
                    ephemeral=True
                )
                return

        await interaction.response.defer(ephemeral=True)

        try:
            canal = await obtener_canal(self.canal_destino_id)

            # Embed sin color personalizado: sin barra lateral de color
            embed = discord.Embed(
                title=nombre.upper(),
                description=descripcion if descripcion else None
            )

            # Precio separado de la descripción
            embed.add_field(
                name="💰 PRECIO DE VENTA",
                value=f"**{precio}**",
                inline=False
            )

            # Imagen horizontal del vehículo, si se proporciona
            if imagen_url:
                embed.set_image(url=imagen_url)

            embed.set_footer(
                text=f"Coutt-and-Schutz | Vehículos {self.tipo}"
            )

            # El vehículo solo se publica al enviar el formulario
            await canal.send(embed=embed)

            await interaction.followup.send(
                f"✅ Vehículo publicado correctamente en "
                f"el canal de vehículos {self.tipo}.",
                ephemeral=True
            )

            print(
                f"Vehículo publicado: {nombre} | "
                f"Precio: {precio} | Tipo: {self.tipo}"
            )

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ No tengo permisos para publicar en ese canal. "
                "Revisa los permisos del bot.",
                ephemeral=True
            )

        except discord.HTTPException as error:
            print(f"Error de Discord al publicar: {error}")

            await interaction.followup.send(
                "❌ Discord no permitió publicar el vehículo. "
                "Revisa los registros del bot.",
                ephemeral=True
            )

        except Exception as error:
            print(f"Error inesperado al publicar: {error}")

            await interaction.followup.send(
                "❌ Ocurrió un error inesperado al publicar.",
                ephemeral=True
            )


# ==========================================
# PANEL DE PUBLICACIONES
# ==========================================

class PanelPublicaciones(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Publicar vehículo usado",
        style=discord.ButtonStyle.secondary,
        emoji="🚗",
        custom_id="coutt:publicar_usado"
    )
    async def publicar_usado(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if not es_administrador(interaction):
            await interaction.response.send_message(
                "🔒 Solo los administradores pueden usar este panel.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            FormularioVehiculo(
                tipo="usados",
                canal_destino_id=CANAL_USADOS_ID
            )
        )

    @discord.ui.button(
        label="Publicar vehículo importado",
        style=discord.ButtonStyle.secondary,
        emoji="🚘",
        custom_id="coutt:publicar_importado"
    )
    async def publicar_importado(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if not es_administrador(interaction):
            await interaction.response.send_message(
                "🔒 Solo los administradores pueden usar este panel.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            FormularioVehiculo(
                tipo="importados",
                canal_destino_id=CANAL_IMPORTADOS_ID
            )
        )


def crear_embed_panel():
    embed = discord.Embed(
        title="COUTT-AND-SCHUTZ",
        description=(
            "**Panel de publicación de vehículos**\n\n"
            "Selecciona el tipo de vehículo que deseas publicar. "
            "Se abrirá un formulario para completar el nombre, "
            "el precio, la descripción y la imagen.\n\n"
            "🚗 **Vehículos usados**\n"
            "Publica en el catálogo de usados.\n\n"
            "🚘 **Vehículos importados**\n"
            "Publica en el catálogo de importados.\n\n"
            "🔒 Solo los administradores pueden utilizar este panel."
        )
    )

    embed.set_footer(text=MARCA_PANEL)

    return embed


# ==========================================
# CREAR O RECUPERAR EL PANEL
# ==========================================

async def asegurar_panel():
    global panel_inicializado

    if panel_inicializado:
        return

    try:
        canal = await obtener_canal(CANAL_PANEL_ID)

        panel_existente = None

        # Buscar el panel entre los últimos 200 mensajes
        async for mensaje in canal.history(limit=200):
            if mensaje.author.id != bot.user.id:
                continue

            for embed in mensaje.embeds:
                if (
                    embed.footer
                    and embed.footer.text == MARCA_PANEL
                ):
                    panel_existente = mensaje
                    break

            if panel_existente is not None:
                break

        vista = PanelPublicaciones()

        if panel_existente is not None:
            await panel_existente.edit(
                embed=crear_embed_panel(),
                view=vista
            )

            print(
                f"Panel existente actualizado: {panel_existente.id}"
            )

        else:
            mensaje = await canal.send(
                embed=crear_embed_panel(),
                view=vista
            )

            print(f"Panel creado: {mensaje.id}")

        panel_inicializado = True

    except discord.Forbidden:
        print(
            "ERROR: faltan permisos en el canal del panel. "
            "Necesito Ver canal, Leer historial y Enviar mensajes."
        )

    except discord.HTTPException as error:
        print(f"No se pudo inicializar el panel: {error}")


# ==========================================
# EVENTOS
# ==========================================

@bot.event
async def on_ready():
    global vista_registrada

    print(f"Bot conectado como {bot.user}")

    # Registrar los botones persistentes una sola vez
    if not vista_registrada:
        bot.add_view(PanelPublicaciones())
        vista_registrada = True

    await asegurar_panel()


# ==========================================
# INICIO
# ==========================================

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "Falta DISCORD_TOKEN. Configura esta variable "
            "en las Environment Variables de Render."
        )

    keep_alive()
    bot.run(TOKEN)
