import os
import io
import asyncio
import logging
import threading
import re
from datetime import datetime, timezone

import discord
from discord.ext import commands
from flask import Flask
from PIL import Image, ImageOps, ImageFilter

from ai_assistant import setup_ai

# ============================================================
# CONFIGURACIÓN
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")

PANEL_CHANNEL_ID = 1560513189438029935
USED_VEHICLES_CHANNEL_ID = 1560447325912961214
IMPORTED_VEHICLES_CHANNEL_ID = 1560447082060447835
WELCOME_CHANNEL_ID = 1558770818433949769

# Canal donde el bot publica la invitación permanente del servidor.
INVITE_CHANNEL_ID = 1560801851467759708

# Canal donde se publica automáticamente la guía de uso V1.
GUIDE_CHANNEL_ID = 1560795723057070170
GUIDE_MARKER = "COUTT_SCHUTZ_GUIDE_V1_2026"
PUBLIC_ROLE_ID = 1558774898103685131

# Sistema de verificación.
VERIFICATION_CHANNEL_ID = 1560779383055327262
VERIFIED_ROLE_ID = 1558774898103685131
UNVERIFIED_ROLE_ID = 1560779721183072266
VERIFICATION_BUTTON_ID = "coutt_schutz:verification_start"

PANEL_MARKER = "COUTT_SCHUTZ_VEHICLE_PANEL"

# Sistema de tickets.
TICKET_PANEL_CHANNEL_ID = 1560447380636176525
# Categoría donde se crean los tickets (opcional). Si es None, se
# crean en la misma categoría del canal del panel de tickets.
TICKET_CATEGORY_ID = None
# Rol de los trabajadores de la concesionaria: se menciona al abrir un
# ticket, puede ver el canal y puede cerrarlo.
TICKET_STAFF_ROLE_ID = 1558775758904893501
TICKET_OPEN_ID = "coutt_schutz:ticket_open"
TICKET_CLOSE_ID = "coutt_schutz:ticket_close"
TICKET_TOPIC_PREFIX = "ticket:"  # el tema del canal guarda el ID del dueño

IMAGE_WIDTH = 1600
IMAGE_HEIGHT = 900
MAX_IMAGE_BYTES = 10 * 1024 * 1024
REQUEST_TIMEOUT = 600

# Color de la barra lateral de cada publicación.
COLOR_USED = discord.Colour.from_rgb(30, 144, 255)      # azul
COLOR_IMPORTED = discord.Colour.from_rgb(46, 204, 113)  # verde
PANEL_COLOR = discord.Colour.from_rgb(212, 175, 55)     # dorado (panel)
WELCOME_COLOR = discord.Colour.from_rgb(212, 175, 55)   # dorado (bienvenida)
CLOSE_COLOR = discord.Colour.from_rgb(237, 66, 69)      # rojo (cierre)
TICKET_COLOR = discord.Colour.from_rgb(88, 101, 242)    # azul (tickets)
VERIFICATION_COLOR = discord.Colour.from_rgb(46, 204, 113)

# Categorías predefinidas que se pueden elegir en el formulario
# (máximo 25). Formato: (nombre, emoji).
VEHICLE_CATEGORIES = [
    ("Deportivos", "🏎️"),
    ("Sedanes", "🚗"),
    ("Camionetas", "🛻"),
    ("Motocicletas", "🏍️"),
    ("Aéreos", "✈️"),
    ("Barcos", "🚤"),
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
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())
        self.add_view(VerificationPanelView())

        # Asistente de IA (mención o comando !ia).
        await setup_ai(
            self,
            USED_VEHICLES_CHANNEL_ID,
            IMPORTED_VEHICLES_CHANNEL_ID,
            ticket_channel_id=TICKET_PANEL_CHANNEL_ID,
            categories=[name for name, _ in VEHICLE_CATEGORIES]
        )


bot = CouttSchutzBot(
    command_prefix="!",
    intents=intents,
    help_command=None  # sin comandos por prefijo: todo va por menciones y /
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
# IDENTIDAD VISUAL
# ============================================================

# Emoji de cada categoría, tomado de VEHICLE_CATEGORIES.
CATEGORY_EMOJIS = {name: emoji for name, emoji in VEHICLE_CATEGORIES}


def category_emoji(name):
    return CATEGORY_EMOJIS.get(name, "🚗")


def logo_url():
    """Logo de la concesionaria (el avatar del bot), si está disponible."""
    if bot.user is None:
        return None
    return bot.user.display_avatar.url


def header_block(text):
    """Cabecera con el logo a la derecha (o solo texto si no hay logo)."""
    logo = logo_url()

    if logo:
        return discord.ui.Section(
            discord.ui.TextDisplay(text),
            accessory=discord.ui.Thumbnail(media=logo)
        )

    return discord.ui.TextDisplay(text)


def notice_view(title, body, colour):
    """Mensaje breve con estilo de contenedor (avisos y confirmaciones)."""
    container = discord.ui.Container(accent_colour=colour)
    container.add_item(discord.ui.TextDisplay(f"### {title}\n{body}"))

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


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
# VERIFICACIÓN DE MIEMBROS
# ============================================================

# Formato obligatorio: Nombre_Apellido. Solo letras, incluyendo
# vocales acentuadas, ü y ñ; exactamente un guion bajo.
RP_NAME_PATTERN = re.compile(
    r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+_[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+\Z"
)


class VerificationModal(discord.ui.Modal, title="Verificación del servidor"):
    rp_name = discord.ui.TextInput(
        label="Nombre y apellido de tu personaje",
        placeholder="Ejemplo: Juan_Perez",
        min_length=3,
        max_length=32,  # límite de Discord para los apodos
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        member = interaction.user
        if not isinstance(member, discord.Member) or interaction.guild is None:
            await interaction.response.send_message(
                "No pude identificar tu miembro del servidor. Inténtalo de nuevo.",
                ephemeral=True
            )
            return

        submitted_name = str(self.rp_name.value).strip()

        if not RP_NAME_PATTERN.fullmatch(submitted_name):
            await interaction.response.send_message(
                "❌ **Formato incorrecto.** Escribe el nombre de tu personaje "
                "como `Nombre_Apellido`, con un solo guion bajo y únicamente "
                "letras. No uses espacios ni números.\n\n"
                "Ejemplo válido: `Juan_Perez`.",
                ephemeral=True
            )
            return

        verified_role = interaction.guild.get_role(VERIFIED_ROLE_ID)
        unverified_role = interaction.guild.get_role(UNVERIFIED_ROLE_ID)

        if verified_role is None:
            await interaction.response.send_message(
                "No se encontró el rol de verificado. Avisa a un administrador.",
                ephemeral=True
            )
            logger.error("No se encontró el rol verificado ID %s", VERIFIED_ROLE_ID)
            return

        if verified_role in member.roles:
            await interaction.response.send_message(
                "Ya estás verificado. No necesitas completar el formulario otra vez.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            await member.add_roles(
                verified_role,
                reason=f"Verificación completada con nombre RP: {submitted_name}"
            )

            if unverified_role is not None and unverified_role in member.roles:
                await member.remove_roles(
                    unverified_role,
                    reason="Miembro completó la verificación"
                )

        except discord.Forbidden:
            logger.exception(
                "No se pudieron cambiar los roles de verificación. "
                "Comprueba la jerarquía de roles y el permiso Gestionar roles."
            )
            await interaction.followup.send(
                "No pude asignarte los roles. Avisa a un administrador para que "
                "revise los permisos y la posición del rol del bot.",
                ephemeral=True
            )
            return
        except discord.HTTPException:
            logger.exception("Error de Discord al asignar roles de verificación.")
            await interaction.followup.send(
                "Ocurrió un error al verificarte. Inténtalo de nuevo en unos momentos.",
                ephemeral=True
            )
            return

        # Cambiar el apodo a "Nombre Apellido" (sin guion bajo).
        nickname = submitted_name.replace("_", " ")
        nickname_changed = False

        try:
            await member.edit(
                nick=nickname,
                reason="Verificación completada: apodo con el nombre RP"
            )
            nickname_changed = True
        except discord.Forbidden:
            logger.warning(
                "No se pudo cambiar el apodo de %s. Comprueba el permiso "
                "Gestionar apodos y que el rol del bot esté por encima del "
                "del miembro (el dueño del servidor no se puede modificar).",
                member
            )
        except discord.HTTPException:
            logger.exception("Error de Discord al cambiar el apodo.")

        if nickname_changed:
            body = (
                f"Se te asignó el rol {verified_role.mention}.\n"
                f"Tu apodo ahora es **{nickname}**."
            )
        else:
            body = (
                f"Se te asignó el rol {verified_role.mention}.\n"
                "⚠️ No pude cambiar tu apodo automáticamente. "
                "Avisa a un administrador."
            )

        await interaction.followup.send(
            view=notice_view(
                "✅ ¡Verificación completada!",
                body,
                VERIFICATION_COLOR
            ),
            ephemeral=True
        )

        logger.info(
            "Miembro verificado: %s (%s), nombre RP: %s",
            member,
            member.id,
            submitted_name
        )


class StartVerificationButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Verificarme",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=VERIFICATION_BUTTON_ID
        )

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user

        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "No pude identificar tu miembro del servidor. Inténtalo de nuevo.",
                ephemeral=True
            )
            return

        verified_role = interaction.guild.get_role(VERIFIED_ROLE_ID) if interaction.guild else None

        if verified_role is not None and verified_role in member.roles:
            await interaction.response.send_message(
                "Ya estás verificado. No necesitas completar el formulario otra vez.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(VerificationModal())


class VerificationPanelView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)

        container = discord.ui.Container(accent_colour=VERIFICATION_COLOR)

        container.add_item(header_block(
            "# ✅ VERIFICACIÓN\n"
            "### Bienvenido a Coutt & Schutz\n"
            "-# Acceso al servidor"
        ))
        container.add_item(discord.ui.Separator(
            spacing=discord.SeparatorSpacing.large
        ))

        container.add_item(discord.ui.TextDisplay(
            "Para acceder al resto del servidor necesitas verificar "
            "el nombre de tu personaje de rol."
        ))
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(
            "> 📝 **Formato:** `Nombre_Apellido`\n"
            "> ✔️ **Ejemplo:** `Juan_Perez`\n\n"
            "Usa únicamente letras y un solo guion bajo entre el nombre "
            "y el apellido. No se permiten espacios ni números."
        ))
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.Section(
            discord.ui.TextDisplay(
                "### 🔑 Verificarme\n"
                "Pulsa el botón y completa el formulario. Tu apodo se "
                "actualizará automáticamente a tu nombre."
            ),
            accessory=StartVerificationButton()
        ))
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(
            "-# 🏁 Coutt & Schutz  ·  Acceso al servidor"
        ))

        self.add_item(container)


async def ensure_verification_panel():
    channel = bot.get_channel(VERIFICATION_CHANNEL_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(VERIFICATION_CHANNEL_ID)
        except discord.HTTPException:
            logger.exception("No se pudo acceder al canal de verificación.")
            return

    try:
        current_panel = None

        async for message in channel.history(limit=100):
            if message.author.id != bot.user.id:
                continue

            if contains_panel_button(message.components, {VERIFICATION_BUTTON_ID}):
                if current_panel is None:
                    current_panel = message
                else:
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        pass

        if current_panel is not None:
            await current_panel.edit(view=VerificationPanelView())
            logger.info("Panel de verificación actualizado.")
            return

        await channel.send(view=VerificationPanelView())
        logger.info("Panel de verificación publicado.")

    except discord.Forbidden:
        logger.error(
            "Faltan permisos para leer el historial o enviar mensajes "
            "en el canal de verificación."
        )
    except discord.HTTPException:
        logger.exception("Error al configurar el panel de verificación.")


# ============================================================
# PUBLICACIÓN DE VEHÍCULOS (CONTENEDOR - COMPONENTS V2)
# ============================================================

def build_vehicle_view(name, price, description, category,
                       vehicle_type, filename, ticket_url=None):
    """
    Crea la publicación con estilo de contenedor:
    barra de color, título, descripción, precio destacado, imagen
    grande y un botón para consultar por el vehículo.
    Requiere discord.py >= 2.6.
    """

    accent = COLOR_USED if vehicle_type == "used" else COLOR_IMPORTED
    published = int(datetime.now(timezone.utc).timestamp())

    container = discord.ui.Container(accent_colour=accent)

    # Cabecera y título.
    container.add_item(discord.ui.TextDisplay(
        "-# 🏁 COUTT & SCHUTZ  ·  EN VENTA"
    ))
    container.add_item(discord.ui.TextDisplay(f"## {name.upper()}"))
    container.add_item(discord.ui.Separator())

    # Descripción.
    container.add_item(discord.ui.TextDisplay(description))
    container.add_item(discord.ui.Separator())

    # Precio destacado y categoría.
    container.add_item(discord.ui.TextDisplay(
        f"### 💰 **Precio:** {price}\n"
        f"{category_emoji(category)} **Categoría:** {category}"
    ))

    # Imagen grande.
    gallery = discord.ui.MediaGallery()
    gallery.add_item(media=f"attachment://{filename}")
    container.add_item(gallery)

    # Pie con la fecha de publicación.
    container.add_item(discord.ui.TextDisplay(
        f"-# 🏁 Coutt & Schutz  ·  Publicado el <t:{published}:D>"
    ))

    # Botón para consultar por el vehículo (lleva al canal de tickets).
    if ticket_url:
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(
            discord.ui.Button(
                label="Consultar este vehículo",
                emoji="📩",
                url=ticket_url
            )
        ))

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

        category = self.vehicle_category.values[0]

        await interaction.response.send_message(
            view=notice_view(
                "📸 Último paso: adjunta la fotografía",
                f"**Vehículo:** {self.vehicle_name.value}\n"
                f"**Categoría:** {category_emoji(category)} {category}\n"
                f"**Precio:** {self.vehicle_price.value}\n\n"
                f"Entra en <#{PANEL_CHANNEL_ID}> y envía la fotografía "
                "como archivo adjunto en un mensaje normal.\n"
                "-# ⏱️ La solicitud caduca en 10 minutos. El bot borrará "
                "tu mensaje al publicar.",
                PANEL_COLOR
            ),
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

        # Cabecera con el logo.
        container.add_item(header_block(
            "# 🏁 COUTT & SCHUTZ\n"
            "### Panel de publicación de vehículos\n"
            "-# Concesionaria  ·  Solo administradores"
        ))
        container.add_item(discord.ui.Separator(
            spacing=discord.SeparatorSpacing.large
        ))

        # Instrucciones.
        container.add_item(discord.ui.TextDisplay(
            "Elige el tipo de vehículo que vas a publicar. Se abrirá un "
            "formulario para indicar el **nombre**, la **categoría**, el "
            "**precio** y una **descripción**."
        ))
        container.add_item(discord.ui.Separator())

        # Vehículos usados (texto + botón al lado).
        container.add_item(discord.ui.Section(
            discord.ui.TextDisplay(
                "### 🚘 Vehículos usados\n"
                "Unidades de segunda mano, listas para entrega."
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
                "Modelos exclusivos traídos del exterior."
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
            "> 🏷️ **Categorías disponibles**\n"
            f"> {categories}"
        ))
        container.add_item(discord.ui.TextDisplay(
            "**Cómo publicar**\n"
            "1️⃣  Pulsa el botón del tipo de vehículo.\n"
            "2️⃣  Completa el formulario.\n"
            "3️⃣  Adjunta la fotografía en este canal.\n"
            "-# La foto se adapta sola al formato 16:9."
        ))
        container.add_item(discord.ui.Separator())

        # Pie.
        container.add_item(discord.ui.TextDisplay(
            "-# 🔒 Panel exclusivo para administradores  ·  "
            "🏁 Coutt & Schutz"
        ))

        self.add_item(container)


# ============================================================
# GESTIÓN DEL PANEL
# ============================================================

def contains_panel_button(components, ids=PANEL_BUTTON_IDS):
    """Busca de forma recursiva los botones del panel en un mensaje."""
    for component in components:
        if getattr(component, "custom_id", None) in ids:
            return True

        children = getattr(component, "children", None)
        if children and contains_panel_button(children, ids):
            return True

        accessory = getattr(component, "accessory", None)
        if accessory is not None and contains_panel_button([accessory], ids):
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
# SISTEMA DE TICKETS
# ============================================================

class ConfirmCloseButton(discord.ui.Button):
    """Botón de confirmación para cerrar el ticket."""

    def __init__(self, channel):
        super().__init__(
            label="Sí, cerrar ticket",
            emoji="🗑️",
            style=discord.ButtonStyle.danger
        )
        self.channel = channel

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=notice_view(
                "🔒 Cerrando ticket",
                "El canal se eliminará en unos segundos.",
                CLOSE_COLOR
            )
        )

        try:
            await self.channel.send(
                view=notice_view(
                    "🔒 Ticket cerrado",
                    f"Cerrado por {interaction.user.mention}.\n"
                    "-# El canal se eliminará en 5 segundos.",
                    CLOSE_COLOR
                ),
                allowed_mentions=discord.AllowedMentions.none()
            )
            await asyncio.sleep(5)
            await self.channel.delete(
                reason=f"Ticket cerrado por {interaction.user}"
            )
            logger.info(
                "Ticket cerrado: #%s por %s",
                self.channel.name,
                interaction.user
            )
        except discord.Forbidden:
            logger.error(
                "No se pudo eliminar el ticket. "
                "Falta el permiso Gestionar canales."
            )
        except discord.HTTPException:
            logger.exception("Error al cerrar el ticket.")


class CloseTicketConfirmView(discord.ui.LayoutView):
    """Confirmación efímera antes de cerrar el ticket."""

    def __init__(self, channel):
        super().__init__(timeout=30)

        container = discord.ui.Container(accent_colour=CLOSE_COLOR)
        container.add_item(discord.ui.TextDisplay(
            "### 🔒 ¿Cerrar este ticket?\n"
            "El canal se eliminará y no se podrá recuperar."
        ))
        container.add_item(discord.ui.ActionRow(
            ConfirmCloseButton(channel)
        ))

        self.add_item(container)


class CloseTicketButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Cerrar ticket",
            emoji="🔒",
            style=discord.ButtonStyle.danger,
            custom_id=TICKET_CLOSE_ID
        )

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.channel
        topic = getattr(channel, "topic", None) or ""

        # Solo el dueño del ticket o un administrador pueden cerrarlo.
        owner_id = None
        if topic.startswith(TICKET_TOPIC_PREFIX):
            try:
                owner_id = int(topic[len(TICKET_TOPIC_PREFIX):])
            except ValueError:
                owner_id = None

        is_staff = any(
            role.id == TICKET_STAFF_ROLE_ID
            for role in getattr(interaction.user, "roles", [])
        )

        if not (is_admin(interaction.user)
                or is_staff
                or interaction.user.id == owner_id):
            await interaction.response.send_message(
                "Solo el creador del ticket o el personal de la "
                "concesionaria puede cerrarlo.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            view=CloseTicketConfirmView(channel),
            ephemeral=True
        )


class TicketControlView(discord.ui.LayoutView):
    """Mensaje de bienvenida dentro del canal del ticket."""

    def __init__(self, owner=None, staff_role_id=None):
        super().__init__(timeout=None)

        if owner is not None:
            greeting = f"¡Hola, {owner.mention}! "
        else:
            greeting = ""

        container = discord.ui.Container(accent_colour=TICKET_COLOR)

        container.add_item(header_block(
            "# 🎫 TICKET ABIERTO\n"
            f"### {greeting}\n"
            "Bienvenido a **Coutt & Schutz**.\n"
            "-# Atención al cliente"
        ))
        container.add_item(discord.ui.Separator(
            spacing=discord.SeparatorSpacing.large
        ))

        container.add_item(discord.ui.TextDisplay(
            "Para atenderte más rápido, cuéntanos:\n"
            "🚗  El vehículo que te interesa\n"
            "💰  Tu presupuesto\n"
            "📝  Cualquier duda o detalle importante\n\n"
            "-# 🔒 Solo tú y el equipo de la concesionaria podéis ver "
            "este canal."
        ))

        if staff_role_id:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(
                f"🔔 <@&{staff_role_id}>, hay un nuevo ticket pendiente "
                "de atención."
            ))

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(CloseTicketButton()))

        self.add_item(container)


def ticket_channel_name(member):
    """
    Nombre del canal del ticket a partir del apodo del miembro
    (o su nombre visible si no tiene apodo). Discord no permite
    espacios ni mayúsculas en canales de texto, así que
    "Tyrell Simmons" queda como "tyrell-simmons".
    """

    def clean(text):
        text = re.sub(r"\s+", "-", text.strip().lower())
        text = re.sub(r"[^\w\-]", "", text)  # quitar emojis y símbolos
        return text.strip("-_")[:90]

    return clean(member.display_name) or clean(member.name) or "ticket"


class OpenTicketButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Abrir ticket",
            emoji="📩",
            style=discord.ButtonStyle.primary,
            custom_id=TICKET_OPEN_ID
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user

        if guild is None or not isinstance(member, discord.Member):
            return

        # Evitar tickets duplicados del mismo usuario.
        topic = f"{TICKET_TOPIC_PREFIX}{member.id}"
        existing = discord.utils.find(
            lambda c: c.topic == topic,
            guild.text_channels
        )

        if existing is not None:
            await interaction.response.send_message(
                f"Ya tienes un ticket abierto: {existing.mention}",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Categoría donde se creará el ticket.
        category = None
        if TICKET_CATEGORY_ID:
            category = guild.get_channel(TICKET_CATEGORY_ID)
        if category is None and interaction.channel is not None:
            category = getattr(interaction.channel, "category", None)

        # Solo el usuario, el bot y los administradores pueden ver
        # el canal (los administradores ven todo por su permiso).
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False
            ),
            member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                embed_links=True,
                manage_channels=True
            ),
        }

        # Los trabajadores de la concesionaria también ven y escriben.
        staff_role = guild.get_role(TICKET_STAFF_ROLE_ID)

        if staff_role is None:
            logger.warning(
                "No se encontró el rol de trabajadores ID %s",
                TICKET_STAFF_ROLE_ID
            )
        else:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True
            )

        try:
            channel = await guild.create_text_channel(
                name=ticket_channel_name(member),
                category=category,
                overwrites=overwrites,
                topic=topic,
                reason=f"Ticket abierto por {member}"
            )

            await channel.send(
                view=TicketControlView(
                    member,
                    staff_role.id if staff_role else None
                ),
                allowed_mentions=discord.AllowedMentions(
                    users=[member],
                    roles=[staff_role] if staff_role else False
                )
            )

        except discord.Forbidden:
            logger.error(
                "No se pudo crear el ticket. "
                "Falta el permiso Gestionar canales."
            )
            await interaction.followup.send(
                "No pude crear el ticket. Avisa a un administrador.",
                ephemeral=True
            )
            return
        except discord.HTTPException:
            logger.exception("Error al crear el ticket.")
            await interaction.followup.send(
                "Ocurrió un error al crear el ticket. "
                "Inténtalo de nuevo más tarde.",
                ephemeral=True
            )
            return

        await interaction.followup.send(
            view=notice_view(
                "✅ Ticket creado",
                f"Tu ticket está listo: {channel.mention}\n"
                "-# Un miembro del equipo te atenderá en breve.",
                TICKET_COLOR
            ),
            ephemeral=True
        )

        logger.info("Ticket creado: #%s (%s)", channel.name, member)


class TicketPanelView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)

        container = discord.ui.Container(accent_colour=TICKET_COLOR)

        container.add_item(header_block(
            "# 🎫 ATENCIÓN AL CLIENTE\n"
            "### Coutt & Schutz\n"
            "-# Concesionaria de vehículos  ·  Ventas y soporte"
        ))
        container.add_item(discord.ui.Separator(
            spacing=discord.SeparatorSpacing.large
        ))

        container.add_item(discord.ui.TextDisplay(
            "¿Quieres comprar un vehículo, consultar un precio o tienes "
            "alguna duda? Abre un ticket y nuestro equipo te atenderá "
            "personalmente."
        ))
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.Section(
            discord.ui.TextDisplay(
                "### 📩 Abrir un ticket\n"
                "Se creará un canal privado solo para ti y el equipo "
                "de la concesionaria."
            ),
            accessory=OpenTicketButton()
        ))
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(
            "**¿En qué podemos ayudarte?**\n"
            "🔑  Compra de vehículos\n"
            "💬  Consultas sobre el catálogo\n"
            "🛠️  Cualquier otra duda\n"
            "-# Solo puedes tener un ticket abierto a la vez."
        ))

        self.add_item(container)


async def ensure_ticket_panel():
    channel = bot.get_channel(TICKET_PANEL_CHANNEL_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(TICKET_PANEL_CHANNEL_ID)
        except discord.HTTPException:
            logger.exception(
                "No se pudo acceder al canal del panel de tickets."
            )
            return

    try:
        current_panel = None

        async for message in channel.history(limit=100):
            if message.author.id != bot.user.id:
                continue

            if contains_panel_button(
                message.components,
                {TICKET_OPEN_ID}
            ):
                if current_panel is None:
                    current_panel = message
                else:
                    # Eliminar duplicados.
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        pass

        if current_panel is not None:
            await current_panel.edit(view=TicketPanelView())
            logger.info("Panel de tickets actualizado.")
            return

        await channel.send(view=TicketPanelView())
        logger.info("Panel de tickets publicado.")

    except discord.Forbidden:
        logger.error(
            "Faltan permisos para leer el historial "
            "o enviar mensajes en el panel de tickets."
        )
    except discord.HTTPException:
        logger.exception("Error al configurar el panel de tickets.")


# ============================================================
# INVITACIÓN PERMANENTE
# ============================================================

INVITE_CODE_RE = re.compile(
    r"(?:discord\.gg|discord\.com/invite)/([A-Za-z0-9-]+)"
)

# Código de la invitación permanente vigente.
current_invite_code = None


class InvitePanelView(discord.ui.LayoutView):
    def __init__(self, code):
        super().__init__(timeout=None)

        url = f"https://discord.gg/{code}"

        container = discord.ui.Container(accent_colour=PANEL_COLOR)

        container.add_item(header_block(
            "# 🔗 ÚNETE A COUTT & SCHUTZ\n"
            "### Invitación oficial al servidor\n"
            "-# Enlace permanente"
        ))
        container.add_item(discord.ui.Separator(
            spacing=discord.SeparatorSpacing.large
        ))

        container.add_item(discord.ui.TextDisplay(
            "Comparte este enlace con tus amigos para que se unan a "
            "nuestra comunidad y descubran nuestro catálogo de vehículos "
            "usados e importados."
        ))
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(
            f"> 🔗 **{url}**"
        ))
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(
            "-# 🏁 Coutt & Schutz  ·  Enlace oficial"
        ))

        self.add_item(container)


def find_invite_code(components):
    """Busca enlaces de invitación tanto en botones como en componentes de texto."""
    for component in components or []:
        # Botones de enlace tradicionales.
        url = getattr(component, "url", None)
        if isinstance(url, str):
            match = INVITE_CODE_RE.search(url)
            if match:
                return match.group(1)

        # Los LayoutView actuales muestran la URL dentro de TextDisplay,
        # no necesariamente en la propiedad `url` de un botón.
        for attribute in ("content", "text", "label", "description"):
            value = getattr(component, attribute, None)
            if isinstance(value, str):
                match = INVITE_CODE_RE.search(value)
                if match:
                    return match.group(1)

        children = getattr(component, "children", None)
        if children:
            code = find_invite_code(children)
            if code:
                return code

        nested = getattr(component, "components", None)
        if nested:
            code = find_invite_code(nested)
            if code:
                return code

    return None


async def ensure_invite_panel():
    """
    Publica (o actualiza) el mensaje con la invitación permanente.
    Reutiliza la invitación existente mientras siga siendo válida y crea
    una nueva solo si fue eliminada.
    """
    global current_invite_code

    channel = bot.get_channel(INVITE_CHANNEL_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(INVITE_CHANNEL_ID)
        except discord.HTTPException:
            logger.exception("No se pudo acceder al canal de invitación.")
            return

    try:
        existing = None
        code = None

        async for message in channel.history(limit=50):
            if message.author.id != bot.user.id:
                continue

            found = find_invite_code(message.components)
            if not found:
                # Compatibilidad con versiones de discord.py donde los
                # componentes de LayoutView no exponen sus textos igual.
                searchable_text = message.content or ""
                for embed in message.embeds:
                    searchable_text += "\n" + (embed.title or "") + "\n" + (embed.description or "")
                    if embed.footer and embed.footer.text:
                        searchable_text += "\n" + embed.footer.text
                match = INVITE_CODE_RE.search(searchable_text)
                if match:
                    found = match.group(1)
            if not found:
                continue

            if existing is None:
                existing, code = message, found
            else:
                # Eliminar mensajes duplicados.
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass

        # Comprobar que la invitación guardada sigue existiendo.
        if code is not None:
            try:
                await bot.fetch_invite(code)
            except discord.NotFound:
                logger.warning("La invitación %s ya no existe.", code)
                code = None
            except discord.HTTPException:
                # No se pudo comprobar: se conserva la actual.
                pass

        # Crear una invitación permanente nueva si hace falta.
        if code is None:
            invite = await channel.create_invite(
                max_age=0,      # nunca caduca
                max_uses=0,     # sin límite de usos
                temporary=False,
                unique=True,
                reason="Invitación permanente del servidor"
            )
            code = invite.code
            logger.info("Invitación permanente creada: %s", code)

        current_invite_code = code
        view = InvitePanelView(code)

        if existing is not None:
            await existing.edit(view=view)
            logger.info("Panel de invitación actualizado.")
        else:
            await channel.send(view=view)
            logger.info("Panel de invitación publicado.")

    except discord.Forbidden:
        logger.error(
            "No se pudo configurar la invitación. Comprueba que el bot "
            "tenga los permisos Crear invitación, Ver canal, Enviar "
            "mensajes y Leer historial en el canal."
        )
    except discord.HTTPException:
        logger.exception("Error al configurar la invitación permanente.")


@bot.event
async def on_invite_delete(invite: discord.Invite):
    # Si borran la invitación permanente, se crea otra automáticamente.
    if invite.code == current_invite_code:
        logger.warning(
            "La invitación permanente fue eliminada. Creando otra..."
        )
        await ensure_invite_panel()


# ============================================================
# BIENVENIDA AUTOMÁTICA
# ============================================================

@bot.event
async def on_member_join(member: discord.Member):
    # Asignar el rol "No verificado" antes de intentar enviar la bienvenida.
    unverified_role = member.guild.get_role(UNVERIFIED_ROLE_ID)

    if unverified_role is None:
        logger.error(
            "No se encontró el rol No verificado ID %s",
            UNVERIFIED_ROLE_ID
        )
    else:
        try:
            await member.add_roles(
                unverified_role,
                reason="Nuevo miembro: requiere completar la verificación"
            )
        except discord.Forbidden:
            logger.exception(
                "No se pudo asignar No verificado. Comprueba Gestionar roles "
                "y que el rol del bot esté por encima de ese rol."
            )
        except discord.HTTPException:
            logger.exception("Error al asignar el rol No verificado.")

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

    container = discord.ui.Container(accent_colour=WELCOME_COLOR)

    container.add_item(discord.ui.Section(
        discord.ui.TextDisplay(
            "# 👋 ¡BIENVENIDO!\n"
            f"### {member.mention}\n"
            "Te damos la bienvenida a **Coutt & Schutz**, "
            "tu concesionaria de confianza."
        ),
        accessory=discord.ui.Thumbnail(media=member.display_avatar.url)
    ))
    container.add_item(discord.ui.Separator(
        spacing=discord.SeparatorSpacing.large
    ))

    container.add_item(discord.ui.TextDisplay(
        f"🔑  **Verifícate** en <#{VERIFICATION_CHANNEL_ID}> para acceder "
        "al servidor.\n"
        f"🚘  Descubre los **vehículos usados** en <#{USED_VEHICLES_CHANNEL_ID}>\n"
        f"🌎  y los **importados** en <#{IMPORTED_VEHICLES_CHANNEL_ID}>.\n"
        f"🎫  ¿Dudas? Abre un ticket en <#{TICKET_PANEL_CHANNEL_ID}>."
    ))
    container.add_item(discord.ui.Separator())

    container.add_item(discord.ui.TextDisplay(
        f"-# 🏁 Coutt & Schutz  ·  Miembro n.º {member.guild.member_count}"
    ))

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)

    try:
        await channel.send(
            view=view,
            allowed_mentions=discord.AllowedMentions(users=[member])
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
                    else:
                        destination_id = IMPORTED_VEHICLES_CHANNEL_ID

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
                        vehicle_type=request["vehicle_type"],
                        filename="vehiculo.png",
                        ticket_url=(
                            f"https://discord.com/channels/"
                            f"{message.guild.id}/{TICKET_PANEL_CHANNEL_ID}"
                        )
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
                            view=notice_view(
                                "✅ Vehículo publicado",
                                f"**{request['name']}** ya está disponible "
                                f"en <#{destination_id}>.",
                                (
                                    COLOR_USED
                                    if request["vehicle_type"] == "used"
                                    else COLOR_IMPORTED
                                )
                            ),
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
# GUÍA DE USO V1 PARA NUEVOS MIEMBROS
# ============================================================

def build_v1_guide_messages():
    """Crea la guía pública de uso sin incluir instrucciones administrativas."""
    return [
        (
            f"-# {GUIDE_MARKER}\n"
            f"<@&{PUBLIC_ROLE_ID}>\n\n"
            "# 🏁 COUTT & SCHUTZ — GUÍA OFICIAL V1\n"
            "¡Te damos la bienvenida! Aquí encontrarás cómo verificar tu personaje, "
            "consultar vehículos y contactar con nuestro equipo.\n\n"
            "## 🔑 1. Verificación de tu personaje\n"
            f"1. Entra en <#{VERIFICATION_CHANNEL_ID}>.\n"
            "2. Pulsa **Verificarme** y completa el formulario.\n"
            "3. Escribe el nombre de tu personaje con el formato `Nombre_Apellido`, "
            "por ejemplo `Juan_Perez`.\n"
            "4. Usa letras y un solo guion bajo; no escribas espacios ni números.\n\n"
            "Si el nombre tiene el formato correcto, tu acceso quedará verificado y "
            "tu apodo se actualizará con el nombre de tu personaje.\n\n"
            "## 🚘 2. Catálogo de vehículos\n"
            f"**Vehículos usados:** <#{USED_VEHICLES_CHANNEL_ID}>\n"
            f"**Vehículos importados:** <#{IMPORTED_VEHICLES_CHANNEL_ID}>\n\n"
            "Las publicaciones pueden incluir nombre, precio, categoría, descripción y fotografía. "
            "Encontrarás estas categorías: Deportivos, Sedanes, Camionetas, Motocicletas, Aéreos y Barcos.\n\n"
            "Si una publicación incluye el botón **Consultar este vehículo**, púlsalo para "
            "continuar con una consulta al equipo. Confirma con el personal la disponibilidad "
            "y las condiciones finales."
        ),
        (
            "## 🤖 3. Asistente virtual con IA\n"
            "Puedes hacer preguntas sobre los vehículos publicados, sus categorías y precios, "
            "comparar opciones o pedir recomendaciones según tus preferencias y presupuesto.\n\n"
            "**Cómo usarlo:** menciona a Coutt & Schutz en un mensaje y escribe tu pregunta, "
            "o utiliza el comando `/ia` si aparece disponible.\n\n"
            "Ejemplos:\n"
            "• ¿Qué deportivos hay?\n"
            "• Compara los sedanes publicados.\n"
            "• ¿Qué opciones hay dentro de mi presupuesto?\n\n"
            "El asistente se basa en las publicaciones del catálogo. Para consultas particulares, "
            "negociaciones o atención personalizada, puedes abrir un ticket. Verifica con el equipo "
            "los datos importantes antes de concretar una operación.\n\n"
            "## 🎫 4. Atención al cliente mediante tickets\n"
            f"Entra en <#{TICKET_PANEL_CHANNEL_ID}> y pulsa **Abrir ticket**.\n\n"
            "Se creará un canal privado para conversar con el equipo. Explica qué necesitas y, "
            "si preguntas por un vehículo, incluye el nombre o enlace de la publicación, "
            "tu presupuesto y cualquier duda que tengas.\n\n"
            "Solo puedes tener un ticket abierto a la vez. Cuando termines, utiliza **Cerrar ticket** "
            "y confirma la acción. El canal se eliminará al cerrarlo, así que guarda antes cualquier "
            "información que quieras conservar."
        ),
        (
            "## 🔗 5. Invita a otras personas\n"
            f"En <#{INVITE_CHANNEL_ID}> encontrarás el enlace oficial para invitar a otras personas "
            "a Coutt & Schutz. Copia el enlace publicado allí y compártelo.\n\n"
            "## ❓ 6. ¿Necesitas ayuda?\n"
            "• **No puedo verificarme:** revisa que el nombre tenga el formato `Nombre_Apellido`.\n"
            "• **Busco información sobre un vehículo:** revisa el catálogo o consulta al asistente de IA.\n"
            "• **Quiero comprar, negociar o hacer una consulta particular:** abre un ticket.\n"
            "• **Un botón no responde:** vuelve a intentarlo y, si el problema continúa, contacta con el equipo.\n\n"
            "### 📌 Antes de concretar una operación\n"
            "La disponibilidad de los vehículos, los precios vigentes y las condiciones de compra "
            "deben confirmarse con el equipo de Coutt & Schutz.\n\n"
            "-# Coutt & Schutz · Guía de uso V1"
        ),
    ]

async def ensure_v1_guide_post():
    """Publica la guía una vez y evita duplicarla al reiniciar el bot."""
    channel = bot.get_channel(GUIDE_CHANNEL_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(GUIDE_CHANNEL_ID)
        except discord.HTTPException:
            logger.exception("No se pudo acceder al canal de la guía V1.")
            return

    try:
        async for message in channel.history(limit=100):
            if message.author.id == bot.user.id and GUIDE_MARKER in message.content:
                logger.info("La guía V1 ya está publicada; no se duplica.")
                return

        messages = build_v1_guide_messages()
        for index, content in enumerate(messages, start=1):
            if len(content) > 2000:
                logger.error("El mensaje %s de la guía supera el límite de Discord (%s caracteres).", index, len(content))
                return

        for index, content in enumerate(messages):
            if index == 0:
                # Permite mencionar únicamente al rol Público al publicar la guía.
                allowed_mentions = discord.AllowedMentions(roles=[discord.Object(id=PUBLIC_ROLE_ID)])
            else:
                allowed_mentions = discord.AllowedMentions.none()
            await channel.send(content, allowed_mentions=allowed_mentions)

        logger.info("Guía de uso V1 publicada en el canal %s.", GUIDE_CHANNEL_ID)

    except discord.Forbidden:
        logger.error(
            "No se pudo publicar la guía V1. Comprueba que el bot tenga permisos "
            "para ver el canal, enviar mensajes y leer el historial."
        )
    except discord.HTTPException:
        logger.exception("Error al publicar la guía de uso V1.")


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
    await ensure_ticket_panel()
    await ensure_verification_panel()
    await ensure_invite_panel()
    await ensure_v1_guide_post()


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
