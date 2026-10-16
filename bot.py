
import os
import io
import logging
import threading
from datetime import datetime, timezone

import discord
from discord.ext import commands
from flask import Flask
from PIL import Image, ImageOps, ImageFilter

# ============================================================
# CONFIGURACIÓN
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")

PANEL_CHANNEL_ID = 1560513189438029935
USED_VEHICLES_CHANNEL_ID = 1560447325912961214
IMPORTED_VEHICLES_CHANNEL_ID = 1560447082060447835
WELCOME_CHANNEL_ID = 1558770818433949769

PANEL_MARKER = "COUTT_SCHUTZ_VEHICLE_PANEL"

IMAGE_WIDTH = 1600
IMAGE_HEIGHT = 900
MAX_IMAGE_BYTES = 10 * 1024 * 1024
REQUEST_TIMEOUT = 600

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("coutt-and-schutz")

# Solicitudes pendientes: ID del administrador -> datos del vehículo.
pending_uploads = {}


# ============================================================
# SERVIDOR WEB PARA RENDER
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "Coutt-and-Schutz está funcionando.", 200


@app.route("/health")
def health():
    return {"status": "ok"}, 200


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False
    )


# ============================================================
# BOT E INTENTS
# ============================================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class CouttSchutzBot(commands.Bot):
    async def setup_hook(self):
        # Mantener los botones operativos después de reiniciar.
        self.add_view(VehiclePanelView())


bot = CouttSchutzBot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# PERMISOS
# ============================================================

def is_admin(member):
    return (
        isinstance(member, discord.Member)
        and member.guild_permissions.administrator
    )


# ============================================================
# PROCESAMIENTO DE IMÁGENES
# ============================================================

def prepare_image(image_bytes):
    """
    Adapta la imagen a 1600x900 (16:9) sin recortar
    la fotografía original.

    Utiliza un fondo ampliado y desenfocado para rellenar
    los espacios sobrantes cuando la proporción original
    no coincide con 16:9.
    """

    with Image.open(io.BytesIO(image_bytes)) as original:
        # Corregir la orientación de las fotografías.
        image = ImageOps.exif_transpose(original)
        image.load()

        # Convertir a RGB para asegurar compatibilidad.
        image = image.convert("RGB")

        target_size = (IMAGE_WIDTH, IMAGE_HEIGHT)

        # Crear el fondo a pantalla completa.
        background = ImageOps.fit(
            image,
            target_size,
            method=Image.Resampling.LANCZOS
        )

        # Desenfocar el fondo para rellenar los espacios.
        background = background.filter(
            ImageFilter.GaussianBlur(radius=25)
        )

        # Reducir la fotografía completa para que quepa
        # dentro del formato sin cortar ningún borde.
        foreground = ImageOps.contain(
            image,
            target_size,
            method=Image.Resampling.LANCZOS
        )

        # Centrar la fotografía original sobre el fondo.
        x = (IMAGE_WIDTH - foreground.width) // 2
        y = (IMAGE_HEIGHT - foreground.height) // 2

        background.paste(foreground, (x, y))

        # Guardar el resultado como PNG.
        output = io.BytesIO()

        background.save(
            output,
            format="PNG",
            optimize=True
        )

        output.seek(0)
        return output.getvalue()


# ============================================================
# FORMULARIO DE VEHÍCULOS
# ============================================================

class VehicleModal(discord.ui.Modal):
    def __init__(self, vehicle_type):
        self.vehicle_type = vehicle_type

        if vehicle_type == "used":
            title = "Publicar vehículo usado"
        else:
            title = "Publicar vehículo importado"

        super().__init__(title=title)

        self.vehicle_name = discord.ui.TextInput(
            label="Nombre del vehículo",
            placeholder="Ej.: BMW M4 Competition",
            max_length=100,
            required=True
        )

        self.vehicle_price = discord.ui.TextInput(
            label="Precio del vehículo",
            placeholder="Ej.: 85.000",
            max_length=50,
            required=True
        )

        self.vehicle_description = discord.ui.TextInput(
            label="Descripción (opcional)",
            placeholder="Estado, año, motor, extras...",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=False
        )

        self.add_item(self.vehicle_name)
        self.add_item(self.vehicle_price)
        self.add_item(self.vehicle_description)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "No tienes permisos para publicar vehículos.",
                ephemeral=True
            )
            return

        panel_channel = bot.get_channel(PANEL_CHANNEL_ID)

        if panel_channel is None:
            try:
                panel_channel = await bot.fetch_channel(
                    PANEL_CHANNEL_ID
                )
            except discord.HTTPException:
                await interaction.response.send_message(
                    "No pude acceder al canal del panel. "
                    "Comprueba el ID y los permisos del bot.",
                    ephemeral=True
                )
                return

        # Guardar los datos del vehículo.
        pending_uploads[interaction.user.id] = {
            "vehicle_type": self.vehicle_type,
            "name": str(self.vehicle_name.value).strip(),
            "price": str(self.vehicle_price.value).strip(),
            "description": (
                str(self.vehicle_description.value).strip()
                or "Sin descripción adicional."
            ),
            "guild_id": interaction.guild_id,
            "created_at": datetime.now(timezone.utc).timestamp()
        }

        await interaction.response.send_message(
            f"**Vehículo:** {self.vehicle_name.value}\n"
            f"**Precio:** {self.vehicle_price.value}\n\n"
            f"Ahora entra en <#{PANEL_CHANNEL_ID}> y envía "
            "la fotografía como archivo adjunto en un mensaje "
            "normal.\n\n"
            "Puedes seleccionar la imagen directamente desde tu PC. "
            "La solicitud caduca en 10 minutos. Una vez publicada "
            "la imagen, el bot intentará eliminar el mensaje temporal.",
            ephemeral=True
        )


# ============================================================
# BOTONES DEL PANEL
# ============================================================

class VehiclePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Vehículos usados",
        style=discord.ButtonStyle.primary,
        emoji="🚘",
        custom_id="coutt_schutz:used"
    )
    async def used_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "Solo los administradores pueden publicar vehículos.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            VehicleModal("used")
        )

    @discord.ui.button(
        label="Vehículos importados",
        style=discord.ButtonStyle.success,
        emoji="🌎",
        custom_id="coutt_schutz:imported"
    )
    async def imported_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "Solo los administradores pueden publicar vehículos.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            VehicleModal("imported")
        )


# ============================================================
# PANEL AUTOMÁTICO
# ============================================================

def create_panel_embed():
    embed = discord.Embed(
        title="Coutt-and-Schutz | Venta de vehículos",
        description=(
            "Bienvenido al sistema de publicaciones de "
            "**Coutt-and-Schutz**.\n\n"
            "Selecciona la categoría correspondiente para "
            "iniciar una publicación.\n\n"
            "🚘 **Vehículos usados**\n"
            "Publica vehículos de segunda mano.\n\n"
            "🌎 **Vehículos importados**\n"
            "Publica vehículos importados.\n\n"
            "Después de completar el formulario, adjunta la imagen "
            "directamente desde tu ordenador en el canal del panel."
        ),
        color=discord.Color.dark_blue()
    )

    embed.set_footer(text=PANEL_MARKER)
    return embed


async def ensure_panel():
    channel = bot.get_channel(PANEL_CHANNEL_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(PANEL_CHANNEL_ID)
        except discord.HTTPException:
            logger.exception(
                "No se pudo acceder al canal del panel."
            )
            return

    try:
        # Buscar el panel existente para evitar duplicados.
        async for message in channel.history(limit=100):
            if message.author.id != bot.user.id:
                continue

            for embed in message.embeds:
                if embed.footer and embed.footer.text == PANEL_MARKER:
                    await message.edit(view=VehiclePanelView())
                    logger.info("Panel existente restaurado.")
                    return

        # Crear un panel si no existe.
        await channel.send(
            embed=create_panel_embed(),
            view=VehiclePanelView()
        )

        logger.info("Panel de vehículos publicado.")

    except discord.Forbidden:
        logger.error(
            "Faltan permisos para leer el historial "
            "o enviar mensajes en el panel."
        )
    except discord.HTTPException:
        logger.exception("Error al configurar el panel.")


# ============================================================
# BIENVENIDA AUTOMÁTICA
# ============================================================

@bot.event
async def on_member_join(member: discord.Member):
    channel = bot.get_channel(WELCOME_CHANNEL_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(
                WELCOME_CHANNEL_ID
            )
        except discord.HTTPException:
            logger.exception(
                "No se pudo acceder al canal de bienvenida."
            )
            return

    embed = discord.Embed(
        title="¡Bienvenido a Coutt-and-Schutz!",
        description=(
            f"¡Hola, {member.mention}! Te damos la bienvenida "
            "a nuestro servidor.\n\n"
            "Esperamos que disfrutes de tu estancia."
        ),
        color=discord.Color.blue()
    )

    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"Miembro: {member.name}")

    try:
        await channel.send(
            content=member.mention,
            embed=embed
        )
    except discord.HTTPException:
        logger.exception("No se pudo enviar la bienvenida.")


# ============================================================
# RECEPCIÓN Y PUBLICACIÓN DE IMÁGENES
# ============================================================

@bot.event
async def on_message(message: discord.Message):
    # Ignorar bots.
    if message.author.bot:
        return

    # Solo recibir fotografías del canal del panel.
    if message.channel.id == PANEL_CHANNEL_ID:
        request = pending_uploads.get(message.author.id)

        if request is not None:
            now = datetime.now(timezone.utc).timestamp()

            # Comprobar caducidad.
            if now - request["created_at"] > REQUEST_TIMEOUT:
                pending_uploads.pop(message.author.id, None)

                try:
                    await message.reply(
                        "La solicitud ha caducado. "
                        "Vuelve a iniciar la publicación desde el panel.",
                        mention_author=False
                    )
                except discord.HTTPException:
                    pass

                await bot.process_commands(message)
                return

            # Verificar que la solicitud pertenece a este servidor.
            if message.guild is None or message.guild.id != request["guild_id"]:
                await bot.process_commands(message)
                return

            # Buscar una imagen adjunta.
            image_attachment = next(
                (
                    attachment
                    for attachment in message.attachments
                    if (
                        attachment.content_type
                        and attachment.content_type.startswith("image/")
                    )
                    or attachment.filename.lower().endswith(
                        (".png", ".jpg", ".jpeg", ".webp", ".gif")
                    )
                ),
                None
            )

            if image_attachment is not None:
                if image_attachment.size > MAX_IMAGE_BYTES:
                    try:
                        await message.reply(
                            "La imagen supera el límite de 10 MB. "
                            "Sube una imagen más pequeña.",
                            mention_author=False
                        )
                    except discord.HTTPException:
                        pass

                    await bot.process_commands(message)
                    return

                try:
                    # Descargar y adaptar la fotografía.
                    image_bytes = await image_attachment.read()
                    processed_bytes = prepare_image(image_bytes)

                    # Determinar el canal de destino.
                    if request["vehicle_type"] == "used":
                        destination_id = USED_VEHICLES_CHANNEL_ID
                        category = "Vehículos usados"
                    else:
                        destination_id = IMPORTED_VEHICLES_CHANNEL_ID
                        category = "Vehículos importados"

                    destination = bot.get_channel(destination_id)

                    if destination is None:
                        destination = await bot.fetch_channel(
                            destination_id
                        )

                    # Crear el archivo procesado.
                    file = discord.File(
                        io.BytesIO(processed_bytes),
                        filename="vehiculo.png"
                    )

                    # Crear la publicación.
                    embed = discord.Embed(
                        title=request["name"],
                        description=request["description"],
                        color=discord.Color.dark_blue()
                    )

                    embed.add_field(
                        name="💰 Precio",
                        value=f"**{request['price']}**",
                        inline=False
                    )

                    embed.add_field(
                        name="Categoría",
                        value=category,
                        inline=True
                    )

                    embed.set_image(url="attachment://vehiculo.png")

                    embed.set_footer(
                        text="Coutt-and-Schutz | Publicación de vehículos"
                    )

                    # Publicar el vehículo.
                    await destination.send(
                        embed=embed,
                        file=file
                    )

                    # Eliminar la solicitud tras publicar correctamente.
                    pending_uploads.pop(message.author.id, None)

                    # Borrar la imagen temporal del panel.
                    try:
                        await message.delete()
                    except discord.Forbidden:
                        logger.warning(
                            "No se pudo borrar la imagen temporal. "
                            "Falta el permiso Gestionar mensajes."
                        )
                    except discord.HTTPException:
                        logger.exception(
                            "Error al borrar la imagen temporal."
                        )

                    # Confirmación temporal.
                    try:
                        await message.channel.send(
                            f"✅ Vehículo **{request['name']}** "
                            f"publicado en <#{destination_id}>.",
                            delete_after=10
                        )
                    except discord.HTTPException:
                        pass

                    logger.info(
                        "Vehículo publicado: %s (%s)",
                        request["name"],
                        category
                    )

                except (
                    discord.HTTPException,
                    OSError,
                    ValueError
                ):
                    logger.exception(
                        "No se pudo procesar o publicar el vehículo."
                    )

                    try:
                        await message.reply(
                            "No se pudo procesar la imagen o publicar "
                            "el vehículo. Comprueba los permisos del bot "
                            "y vuelve a intentarlo.",
                            mention_author=False
                        )
                    except discord.HTTPException:
                        pass

    # Mantener disponibles los comandos del bot.
    await bot.process_commands(message)


# ============================================================
# INICIO
# ============================================================

@bot.event
async def on_ready():
    logger.info(
        "Bot conectado como %s (ID: %s)",
        bot.user,
        bot.user.id
    )

    await ensure_panel()


def main():
    if not TOKEN:
        raise RuntimeError(
            "Falta la variable de entorno DISCORD_TOKEN en Render."
        )

    web_thread = threading.Thread(
        target=run_web_server,
        daemon=True
    )
    web_thread.start()

    bot.run(TOKEN)


if __name__ == "__main__":
    main()
