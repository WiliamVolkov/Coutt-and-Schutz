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

# Color de la barra lateral de cada publicación.
COLOR_USED = discord.Colour.from_rgb(30, 144, 255)      # azul
COLOR_IMPORTED = discord.Colour.from_rgb(46, 204, 113)  # verde
PANEL_COLOR = discord.Colour.from_rgb(255, 140, 0)      # naranja (panel)

# Categorías predefinidas que se pueden elegir en el formulario
# (máximo 25). Formato: (nombre, emoji).
VEHICLE_CATEGORIES = [
    ("Deportivos", "🏎️"),
    ("Sedanes", "🚗"),
    ("Camionetas", "🛻"),
    ("Motocicletas", "🏍️"),
]

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
# PUBLICACIÓN DE VEHÍCULOS (CONTENEDOR - COMPONENTS V2)
# ============================================================

def build_vehicle_view(name, price, description, category,
                       type_label, vehicle_type, filename):
    """
    Crea la publicación con estilo de contenedor:
    barra de color lateral, separadores, cita y galería de imagen.
    Requiere discord.py >= 2.6.
    """

    accent = COLOR_USED if vehicle_type == "used" else COLOR_IMPORTED

    container = discord.ui.Container(accent_colour=accent)

    # Título grande.
    container.add_item(
        discord.ui.TextDisplay(f"## {name.upper()}")
    )
    container.add_item(discord.ui.Separator())

    # Descripción.
    container.add_item(discord.ui.TextDisplay(description))
    container.add_item(discord.ui.Separator())

    # Bloque de cita con precio y categoría.
    container.add_item(
        discord.ui.TextDisplay(
            f"> 💰 **Precio:** {price}\n"
            f"> 🏷️ **Categoría:** {category}\n"
            f"> 📦 **Tipo:** {type_label}"
        )
    )
    container.add_item(discord.ui.Separator())

    # Imagen grande.
    gallery = discord.ui.MediaGallery()
    gallery.add_item(media=f"attachment://{filename}")
    container.add_item(gallery)

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


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
            placeholder="Ej.: BMW M4 Competition",
            max_length=100,
            required=True
        )

        self.vehicle_category = discord.ui.Select(
            placeholder="Elige una categoría",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=name, value=name, emoji=emoji)
                for name, emoji in VEHICLE_CATEGORIES
            ]
        )

        self.vehicle_price = discord.ui.TextInput(
            placeholder="Ej.: 85.000",
            max_length=50,
            required=True
        )

        self.vehicle_description = discord.ui.TextInput(
            placeholder="Estado, año, motor, extras...",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=False
        )

        self.add_item(discord.ui.Label(
            text="Nombre del vehículo",
            component=self.vehicle_name
        ))
        self.add_item(discord.ui.Label(
            text="Categoría",
            component=self.vehicle_category
        ))
        self.add_item(discord.ui.Label(
            text="Precio del vehículo",
            component=self.vehicle_price
        ))
        self.add_item(discord.ui.Label(
            text="Descripción (opcional)",
            component=self.vehicle_description
        ))

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
            "category": self.vehicle_category.values[0],
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
            f"**Categoría:** {self.vehicle_category.values[0]}\n"
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
# PANEL (CONTENEDOR - COMPONENTS V2)
# ============================================================

PANEL_BUTTON_IDS = {"coutt_schutz:used", "coutt_schutz:imported"}


class PublishButton(discord.ui.Button):
    """Botón que abre el formulario de publicación."""

    def __init__(self, vehicle_type, label, emoji, style, custom_id):
        super().__init__(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=custom_id
        )
        self.vehicle_type = vehicle_type

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "Solo los administradores pueden publicar vehículos.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            VehicleModal(self.vehicle_type)
        )


class VehiclePanelView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)

        categories = "  ·  ".join(
            f"{emoji} {name}" for name, emoji in VEHICLE_CATEGORIES
        )

        container = discord.ui.Container(accent_colour=PANEL_COLOR)

        # Cabecera.
        container.add_item(discord.ui.TextDisplay(
            "# 🚗 COUTT & SCHUTZ\n"
            "-# Sistema de publicación de vehículos"
        ))
        container.add_item(discord.ui.Separator(
            spacing=discord.SeparatorSpacing.large
        ))

        # Instrucciones.
        container.add_item(discord.ui.TextDisplay(
            "Selecciona el tipo de vehículo que quieres publicar. "
            "Se abrirá un formulario donde podrás elegir la "
            "**categoría**, el **precio** y añadir una descripción."
        ))
        container.add_item(discord.ui.Separator())

        # Vehículos usados (texto + botón al lado).
        container.add_item(discord.ui.Section(
            discord.ui.TextDisplay(
                "### 🚘 Vehículos usados\n"
                "Publica vehículos de segunda mano."
            ),
            accessory=PublishButton(
                vehicle_type="used",
                label="Publicar",
                emoji="🚘",
                style=discord.ButtonStyle.primary,
                custom_id="coutt_schutz:used"
            )
        ))

        # Vehículos importados (texto + botón al lado).
        container.add_item(discord.ui.Section(
            discord.ui.TextDisplay(
                "### 🌎 Vehículos importados\n"
                "Publica vehículos importados."
            ),
            accessory=PublishButton(
                vehicle_type="imported",
                label="Publicar",
                emoji="🌎",
                style=discord.ButtonStyle.success,
                custom_id="coutt_schutz:imported"
            )
        ))
        container.add_item(discord.ui.Separator())

        # Categorías y pasos.
        container.add_item(discord.ui.TextDisplay(
            f"> 🏷️ **Categorías disponibles**\n"
            f"> {categories}"
        ))
        container.add_item(discord.ui.TextDisplay(
            "**¿Cómo funciona?**\n"
            "**1.** Pulsa el botón del tipo de vehículo.\n"
            "**2.** Rellena el formulario.\n"
            "**3.** Adjunta la fotografía en este canal."
        ))
        container.add_item(discord.ui.Separator())

        # Pie.
        container.add_item(discord.ui.TextDisplay(
            "-# 🔒 Panel exclusivo para administradores  •  "
            "Coutt-and-Schutz"
        ))

        self.add_item(container)


# ============================================================
# GESTIÓN DEL PANEL
# ============================================================

def contains_panel_button(components):
    """Busca de forma recursiva los botones del panel en un mensaje."""
    for component in components:
        if getattr(component, "custom_id", None) in PANEL_BUTTON_IDS:
            return True

        children = getattr(component, "children", None)
        if children and contains_panel_button(children):
            return True

        accessory = getattr(component, "accessory", None)
        if accessory is not None and contains_panel_button([accessory]):
            return True

    return False


def is_legacy_panel(message):
    """Detecta el panel antiguo (embed con marca en el pie)."""
    return any(
        embed.footer and embed.footer.text == PANEL_MARKER
        for embed in message.embeds
    )


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
        current_panel = None

        async for message in channel.history(limit=100):
            if message.author.id != bot.user.id:
                continue

            # Panel antiguo (embed): se elimina para sustituirlo.
            if is_legacy_panel(message):
                try:
                    await message.delete()
                    logger.info("Panel antiguo eliminado.")
                except discord.HTTPException:
                    logger.warning(
                        "No se pudo eliminar el panel antiguo. "
                        "Falta el permiso Gestionar mensajes."
                    )
                continue

            # Panel nuevo (contenedor).
            if contains_panel_button(message.components):
                if current_panel is None:
                    current_panel = message
                else:
                    # Eliminar duplicados.
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        pass

        if current_panel is not None:
            await current_panel.edit(view=VehiclePanelView())
            logger.info("Panel existente actualizado.")
            return

        # Crear un panel si no existe.
        await channel.send(view=VehiclePanelView())
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
                        type_label = "Vehículo usado"
                    else:
                        destination_id = IMPORTED_VEHICLES_CHANNEL_ID
                        type_label = "Vehículo importado"

                    # Categoría elegida en el formulario.
                    category = request["category"]

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

                    # Crear la publicación (contenedor).
                    view = build_vehicle_view(
                        name=request["name"],
                        price=request["price"],
                        description=request["description"],
                        category=category,
                        type_label=type_label,
                        vehicle_type=request["vehicle_type"],
                        filename="vehiculo.png"
                    )

                    # Texto superior. Con Components V2 no se puede
                    # mezclar "content" con el contenedor, así que
                    # va en un mensaje aparte.
                    await destination.send(
                        f"**{category}** - Coutt-and-Schutz | "
                        "¡Consultar stock!"
                    )

                    # Publicar el vehículo.
                    await destination.send(
                        view=view,
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
