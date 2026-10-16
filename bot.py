
import os
import io
from threading import Thread
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import commands
from flask import Flask
from dotenv import load_dotenv
from PIL import Image, ImageOps, UnidentifiedImageError

# ==========================================
# CONFIGURACIÓN
# ==========================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

CANAL_PANEL_ID = 1560513189438029935
CANAL_USADOS_ID = 1560447325912961214
CANAL_IMPORTADOS_ID = 1560447082060447835

MARCA_PANEL = "COUTT_SCHUTZ_PANEL_V1"

# Tamaño de imagen normalizado: 16:9
ANCHO_IMAGEN = 1600
ALTO_IMAGEN = 900
MAX_IMAGEN_BYTES = 8 * 1024 * 1024

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

bot = commands.Bot(command_prefix="!", intents=intents)

panel_inicializado = False


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
# PROCESAMIENTO DE IMÁGENES
# ==========================================

async def descargar_y_ajustar_imagen(url: str):
    """
    Descarga una imagen y la adapta a un lienzo 16:9.
    Conserva la imagen completa sin estirarla ni recortarla.
    """

    parsed = urlparse(url)

    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("La URL de imagen no es válida.")

    timeout = aiohttp.ClientTimeout(total=20)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
        )
    }

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers
    ) as session:
        async with session.get(
            url,
            allow_redirects=True
        ) as response:

            # Comprobar el código HTTP del servidor.
            if response.status != 200:
                raise ValueError(
                    f"El servidor de imágenes respondió HTTP "
                    f"{response.status}. Comprueba el enlace "
                    f"o prueba con otra imagen."
                )

            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()

            if not content_type.startswith("image/"):
                raise ValueError(
                    "El enlace no apunta directamente a una imagen. "
                    "Usa la URL directa del archivo, por ejemplo "
                    "una que termine en .jpg, .jpeg, .png o .webp."
                )

            longitud = response.content_length

            if longitud and longitud > MAX_IMAGEN_BYTES:
                raise ValueError(
                    "La imagen supera el límite de 8 MB."
                )

            contenido = await response.content.read(
                MAX_IMAGEN_BYTES + 1
            )

            if len(contenido) > MAX_IMAGEN_BYTES:
                raise ValueError(
                    "La imagen supera el límite de 8 MB."
                )

    try:
        with Image.open(io.BytesIO(contenido)) as original:
            original.seek(0)
            original.load()

            imagen = ImageOps.exif_transpose(original)

            imagen.thumbnail(
                (ANCHO_IMAGEN, ALTO_IMAGEN),
                Image.Resampling.LANCZOS
            )

            # Crear un fondo oscuro en formato 16:9.
            lienzo = Image.new(
                "RGB",
                (ANCHO_IMAGEN, ALTO_IMAGEN),
                (24, 24, 27)
            )

            # Centrar sin deformar ni cortar la imagen.
            if imagen.mode in ("RGBA", "LA"):
                imagen = imagen.convert("RGBA")

                x = (ANCHO_IMAGEN - imagen.width) // 2
                y = (ALTO_IMAGEN - imagen.height) // 2

                lienzo.paste(imagen, (x, y), imagen)
            else:
                imagen = imagen.convert("RGB")

                x = (ANCHO_IMAGEN - imagen.width) // 2
                y = (ALTO_IMAGEN - imagen.height) // 2

                lienzo.paste(imagen, (x, y))

            salida = io.BytesIO()

            lienzo.save(
                salida,
                format="JPEG",
                quality=92,
                optimize=True
            )

            salida.seek(0)
            return salida

    except (UnidentifiedImageError, OSError) as error:
        raise ValueError(
            "El archivo descargado no es una imagen compatible."
        ) from error


# ==========================================
# FORMULARIO DE VEHÍCULOS
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

        await interaction.response.defer(ephemeral=True)

        imagen_procesada = None

        if imagen_url:
            try:
                imagen_procesada = (
                    await descargar_y_ajustar_imagen(imagen_url)
                )
            except (
                ValueError,
                aiohttp.ClientError,
                TimeoutError
            ) as error:
                await interaction.followup.send(
                    f"No se pudo procesar la imagen: {error}",
                    ephemeral=True
                )
                return

        try:
            canal = await obtener_canal(
                self.canal_destino_id
            )

            embed = discord.Embed(
                title=nombre.upper(),
                description=descripcion if descripcion else None
            )

            embed.add_field(
                name="💰 PRECIO DE VENTA",
                value=f"## {precio}",
                inline=False
            )

            embed.set_footer(
                text=f"Coutt-and-Schutz | Vehículos {self.tipo}"
            )

            if imagen_procesada:
                archivo = discord.File(
                    imagen_procesada,
                    filename="vehiculo.jpg"
                )

                embed.set_image(
                    url="attachment://vehiculo.jpg"
                )

                await canal.send(
                    embed=embed,
                    file=archivo
                )
            else:
                await canal.send(embed=embed)

            await interaction.followup.send(
                f"✅ Vehículo publicado correctamente en "
                f"el canal de vehículos {self.tipo}.",
                ephemeral=True
            )

            print(
                f"Publicado: {nombre} | Precio: {precio} | "
                f"Tipo: {self.tipo}"
            )

        except discord.Forbidden:
            await interaction.followup.send(
                "No tengo permisos para publicar en ese canal. "
                "Comprueba los permisos del bot.",
                ephemeral=True
            )

        except discord.HTTPException as error:
            print(f"Error de Discord al publicar: {error}")

            await interaction.followup.send(
                "Discord no permitió publicar el vehículo. "
                "Revisa los registros de Render.",
                ephemeral=True
            )

        except Exception as error:
            print(f"Error inesperado al publicar: {error}")

            await interaction.followup.send(
                "Ocurrió un error inesperado al publicar.",
                ephemeral=True
            )

        finally:
            if imagen_procesada:
                imagen_procesada.close()


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
            "Selecciona dónde quieres publicar el vehículo.\n\n"
            "🚗 **Vehículos usados**\n"
            "Publica en el catálogo de usados.\n\n"
            "🚘 **Vehículos importados**\n"
            "Publica en el catálogo de importados.\n\n"
            "El nombre y el precio son obligatorios. "
            "La descripción y la imagen son opcionales.\n\n"
            "🔒 Solo los administradores pueden utilizar el panel."
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

        async for mensaje in canal.history(limit=200):
            if bot.user is None or mensaje.author.id != bot.user.id:
                continue

            for embed in mensaje.embeds:
                if (
                    embed.footer
                    and embed.footer.text == MARCA_PANEL
                ):
                    panel_existente = mensaje
                    break

            if panel_existente:
                break

        vista = PanelPublicaciones()

        if panel_existente:
            await panel_existente.edit(
                embed=crear_embed_panel(),
                view=vista
            )

            print(
                f"Panel existente actualizado: "
                f"{panel_existente.id}"
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
async def setup_hook():
    bot.add_view(PanelPublicaciones())


@bot.event
async def on_ready():
    print(f"Bot conectado como {bot.user}")
    await asegurar_panel()


# ==========================================
# ARRANQUE
# ==========================================

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "Falta DISCORD_TOKEN. Configura esta variable "
            "en las Environment Variables de Render."
        )

    keep_alive()
    bot.run(TOKEN)
