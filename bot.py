import os
import io
import asyncio
import logging
import threading
import re
import json
from pathlib import Path
from datetime import datetime, timezone
from discord import app_commands

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

# Lista pública de trabajadores de la concesionaria.
WORKERS_CHANNEL_ID = 1560902787837988954
WORKERS_BASE_ROLE_ID = 1558775758904893501
WORKERS_LIST_MARKER = "COUTT_SCHUTZ_WORKERS_LIST_V1"

# Panel persistente de estadísticas del catálogo.
STATS_CHANNEL_ID = 1561277200576286730
STATS_PANEL_TITLE = "ESTADÍSTICAS | COUTT & SCHUTZ"

# Lista de vehiculos del concesionario predefinido por el sistema del
# servidor de rol. NO se venden aqui: la IA los usa solo como referencia.
SYSTEM_DEALER_CHANNEL_ID = 1560933334253703188
WORKER_RANKS = [
    (1558775707637645393, "Dueño"),
    (1558775031113326613, "Gerente"),
    (1558775293269901332, "Encargado regional"),
    (1558775198642212895, "Jefe de vendedores"),
    (1558774946128465980, "Vendedor"),
]

# Sistema de verificación.
VERIFICATION_CHANNEL_ID = 1560779383055327262
VERIFIED_ROLE_ID = 1558774898103685131
UNVERIFIED_ROLE_ID = 1560779721183072266
VERIFICATION_BUTTON_ID = "coutt_schutz:verification_start"

PANEL_MARKER = "COUTT_SCHUTZ_VEHICLE_PANEL"

# Sistema de tickets.
TICKET_PANEL_CHANNEL_ID = 1560447380636176525
# Categoría de destino para los tickets creados desde el panel de contacto.
TICKET_CATEGORY_ID = 1561573807724953620
# Registro de aperturas y cierres de tickets.
TICKET_LOG_CHANNEL_ID = 1561573866617184336
# Foro donde se conserva el historial de los tickets cerrados.
TICKET_ARCHIVE_FORUM_ID = 1561573909772501042
# Rol de los trabajadores de la concesionaria: se menciona al abrir un
# ticket, puede ver el canal y puede cerrarlo.
TICKET_STAFF_ROLE_ID = 1558775758904893501
TICKET_OPEN_ID = "coutt_schutz:ticket_open"
TICKET_CLOSE_ID = "coutt_schutz:ticket_close"
TICKET_TOPIC_PREFIX = "ticket:"  # el tema del canal guarda el ID del dueño

# Sistema de pedidos por encargo.
# DATABASE_URL debe apuntar a PostgreSQL (por ejemplo, una base gratuita externa).
# ORDER_LOG_CHANNEL_ID es un canal privado de Discord para el historial auxiliar.
# El JSON local solo se usa como modo de prueba si no se configura DATABASE_URL.
PEDIDOS_DATA_FILE = os.getenv("PEDIDOS_DATA_FILE", "pedidos_encargo.json")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ORDER_LOG_CHANNEL_ID = int(os.getenv("ORDER_LOG_CHANNEL_ID", "0") or "0")
# Canal que contiene el único mensaje visible con la cola actual de pedidos.
# ID fijo del canal donde se muestra el panel de pedidos en tiempo real.
ORDER_QUEUE_CHANNEL_ID = 1561281767414042624
ORDER_REGISTER_BUTTON_ID = "coutt_schutz:order_register"
ORDER_QUEUE_TITLE = "PEDIDOS POR ENCARGO | COUTT & SCHUTZ"

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
        # Registrar primero todos los comandos, incluidos los del asistente,
        # y después sincronizarlos como comandos globales.
        await setup_ai(
            self,
            USED_VEHICLES_CHANNEL_ID,
            IMPORTED_VEHICLES_CHANNEL_ID,
            ticket_channel_id=TICKET_PANEL_CHANNEL_ID,
            categories=[name for name, _ in VEHICLE_CATEGORIES],
            staff_channel_id=WORKERS_CHANNEL_ID,
            system_channel_id=SYSTEM_DEALER_CHANNEL_ID
        )
        # IMPORTANTE: eliminar los comandos registrados en el ámbito del servidor
        # antes de sincronizar los globales. Discord puede mostrar simultáneamente
        # un comando global y otro antiguo de servidor con el mismo nombre.
        # ID del servidor Coutt & Schutz (el primer ID de la URL de un canal).
        target_guild = discord.Object(id=1558770815678283868)
        try:
            self.tree.clear_commands(guild=target_guild)
            guild_synced = await self.tree.sync(guild=target_guild)
            logger.info(
                "Sincronización de comandos específicos del servidor completada: %s comandos restantes.",
                len(guild_synced)
            )
        except discord.HTTPException:
            logger.exception(
                "No se pudieron limpiar los comandos antiguos del servidor %s.",
                target_guild.id
            )
            raise

        # Sincronizar el conjunto actual de comandos globales. Esta operación
        # reemplaza en Discord la lista global de ESTA aplicación.
        synced = await self.tree.sync()
        local_names = sorted(command.name for command in self.tree.get_commands())
        logger.info(
            "SYNC COMPLETADO | bot=%s | application_id=%s | globales=%s | nombres=%s",
            getattr(self.user, "id", "aún no disponible"),
            self.application_id,
            len(synced),
            local_names
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
# SISTEMA DE PEDIDOS POR ENCARGO
# ============================================================

orders_lock = asyncio.Lock()
orders_data = {"next_id": 1, "orders": []}
ORDERS_STORAGE_READY = False

def _postgres_connect():
    """Abre una conexión PostgreSQL usando psycopg 3."""
    import psycopg
    return psycopg.connect(DATABASE_URL, connect_timeout=10)

def load_orders():
    """Carga la cola y marca el almacenamiento como listo solo si la carga es segura."""
    global orders_data, ORDERS_STORAGE_READY
    ORDERS_STORAGE_READY = False
    if DATABASE_URL:
        try:
            with _postgres_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "CREATE TABLE IF NOT EXISTS coutt_schutz_order_state ("
                        "id INTEGER PRIMARY KEY CHECK (id = 1), state JSONB NOT NULL)"
                    )
                    cur.execute("SELECT state FROM coutt_schutz_order_state WHERE id = 1")
                    row = cur.fetchone()
                    if row and row[0]:
                        state = row[0]
                        if isinstance(state, str):
                            state = json.loads(state)
                        if isinstance(state, dict) and isinstance(state.get("orders"), list):
                            orders_data = {
                                "next_id": int(state.get("next_id", 1)),
                                "orders": state["orders"]
                            }
                    else:
                        cur.execute(
                            "INSERT INTO coutt_schutz_order_state (id, state) VALUES (1, %s::jsonb) "
                            "ON CONFLICT (id) DO NOTHING",
                            (json.dumps(orders_data, ensure_ascii=False),)
                        )
            ORDERS_STORAGE_READY = True
            logger.info("Cola de pedidos cargada desde PostgreSQL (%s pedidos).", len(orders_data["orders"]))
            return
        except Exception:
            # No habilitar altas/entregas con una cola vacía si la DB falló:
            # eso podría sobrescribir datos reales al guardar el siguiente cambio.
            logger.exception("No se pudo cargar la cola desde PostgreSQL. El sistema de pedidos queda bloqueado para evitar pérdida de datos; revisa DATABASE_URL, conectividad y psycopg.")
            ORDERS_STORAGE_READY = False
            return

    path = Path(PEDIDOS_DATA_FILE)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("orders"), list):
                orders_data = {"next_id": int(data.get("next_id", 1)), "orders": data["orders"]}
        ORDERS_STORAGE_READY = True
        logger.warning("DATABASE_URL no está configurada: la cola usa JSON local y puede perderse al reiniciar Render.")
    except (OSError, ValueError, TypeError):
        ORDERS_STORAGE_READY = False
        logger.exception("No se pudo leer el archivo local de pedidos. El sistema de pedidos queda bloqueado para evitar sobrescribir datos.")

def save_orders():
    """Guarda la cola; no permite escribir hasta confirmar que la carga inicial fue segura."""
    if not ORDERS_STORAGE_READY:
        raise RuntimeError("El almacenamiento de pedidos no está listo; se bloquea la escritura para proteger los datos existentes.")
    if DATABASE_URL:
        with _postgres_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO coutt_schutz_order_state (id, state) VALUES (1, %s::jsonb) "
                    "ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state",
                    (json.dumps(orders_data, ensure_ascii=False),)
                )
        return
    path = Path(PEDIDOS_DATA_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(orders_data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)

async def log_order_event(guild, title, description, order=None):
    """Registra los cambios en el canal privado de auditoría de Discord."""
    if not ORDER_LOG_CHANNEL_ID or guild is None:
        logger.warning("ORDER_LOG_CHANNEL_ID no configurado; no se publicó el registro auxiliar de pedidos.")
        return

    # El canal de la cola debe contener solo el mensaje-lista. Si el canal de
    # auditoría coincide con él, no se publican allí eventos individuales.
    if ORDER_LOG_CHANNEL_ID == ORDER_QUEUE_CHANNEL_ID:
        logger.info(
            "Registro auxiliar omitido porque ORDER_LOG_CHANNEL_ID coincide con "
            "ORDER_QUEUE_CHANNEL_ID; se conserva el canal como lista única."
        )
        return

    channel = guild.get_channel(ORDER_LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await guild.fetch_channel(ORDER_LOG_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.warning("No se pudo acceder al canal de registro de pedidos %s.", ORDER_LOG_CHANNEL_ID)
            return
    embed = discord.Embed(title=title, description=description, colour=COLOR_IMPORTED, timestamp=datetime.now(timezone.utc))
    if order:
        embed.add_field(name="Pedido", value=f"#{order.get('id')} · {order.get('model', 'Modelo desconocido')}", inline=False)
        embed.add_field(name="Cliente", value=f"<@{order.get('customer_id')}>", inline=True)
        if order.get("ticket_url"):
            embed.add_field(name="Ticket", value=f"[Abrir ticket]({order['ticket_url']})", inline=True)
        embed.add_field(name="Estado", value=order.get("status", "desconocido").capitalize(), inline=True)
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("No se pudo escribir en el canal privado de registro de pedidos.")


def is_ticket_staff(member):
    return is_admin(member) or (
        isinstance(member, discord.Member)
        and any(role.id == TICKET_STAFF_ROLE_ID for role in member.roles)
    )


def get_ticket_owner_id(channel):
    topic = getattr(channel, "topic", None) or ""
    if topic.startswith(TICKET_TOPIC_PREFIX):
        try:
            return int(topic[len(TICKET_TOPIC_PREFIX):])
        except ValueError:
            return None
    return None


orders_queue_lock = asyncio.Lock()


def get_saved_orders_queue_message_id():
    """Recupera de PostgreSQL el ID del mensaje único de la cola, si existe."""
    if not DATABASE_URL:
        return None
    try:
        with _postgres_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS coutt_schutz_order_queue_message ("
                    "id INTEGER PRIMARY KEY CHECK (id = 1), message_id BIGINT NOT NULL)"
                )
                cur.execute(
                    "SELECT message_id FROM coutt_schutz_order_queue_message WHERE id = 1"
                )
                row = cur.fetchone()
                return int(row[0]) if row else None
    except Exception:
        logger.exception("No se pudo recuperar el ID del mensaje único de pedidos desde PostgreSQL.")
        return None


def save_orders_queue_message_id(message_id):
    """Guarda el ID del mensaje único para reconocerlo después de reiniciar."""
    if not DATABASE_URL:
        return
    try:
        with _postgres_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS coutt_schutz_order_queue_message ("
                    "id INTEGER PRIMARY KEY CHECK (id = 1), message_id BIGINT NOT NULL)"
                )
                cur.execute(
                    "INSERT INTO coutt_schutz_order_queue_message (id, message_id) VALUES (1, %s) "
                    "ON CONFLICT (id) DO UPDATE SET message_id = EXCLUDED.message_id",
                    (int(message_id),)
                )
    except Exception:
        logger.exception("No se pudo guardar el ID del mensaje único de pedidos en PostgreSQL.")


async def refresh_orders_queue(guild=None):
    """Mantiene un único mensaje permanente: lo edita si existe y lo recrea si fue borrado."""
    async with orders_queue_lock:
        channel = guild.get_channel(ORDER_QUEUE_CHANNEL_ID) if guild else bot.get_channel(ORDER_QUEUE_CHANNEL_ID)
        if channel is None:
            try:
                channel = await bot.fetch_channel(ORDER_QUEUE_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.exception("No se pudo acceder al canal de la cola de pedidos %s.", ORDER_QUEUE_CHANNEL_ID)
                return
        if not isinstance(channel, discord.TextChannel):
            logger.error("ORDER_QUEUE_CHANNEL_ID debe apuntar a un canal de texto.")
            return

        embed = build_orders_embed()
        existing = None
        saved_id = await asyncio.to_thread(get_saved_orders_queue_message_id)

        # Primero se consulta el ID guardado en PostgreSQL: así no se publica otro
        # mensaje cada vez que Render reinicia el proceso.
        if saved_id:
            try:
                candidate = await channel.fetch_message(saved_id)
                if candidate.author.id == bot.user.id and any(
                    e.title == ORDER_QUEUE_TITLE for e in candidate.embeds
                ):
                    existing = candidate
            except discord.NotFound:
                logger.info("El mensaje guardado de la cola ya no existe; se recreará.")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("No se pudo comprobar el mensaje guardado de la cola.")

        # Se revisa el historial incluso cuando PostgreSQL ya conoce el ID:
        # así se eliminan duplicados y eventos individuales antiguos en cada arranque.
        try:
            # Límite defensivo: evita recorrer un historial completo en cada cambio.
            # El ID persistido sigue siendo la fuente principal para reutilizar la lista.
            async for message in channel.history(limit=200):
                if message.author.id != bot.user.id:
                    continue
                titles = [e.title or "" for e in message.embeds]
                is_queue = ORDER_QUEUE_TITLE in titles
                is_old_order_event = any(
                    "pedido por encargo registrado" in title.casefold()
                    or "pedido por encargo entregado" in title.casefold()
                    for title in titles
                )
                if is_queue:
                    if existing is None:
                        existing = message
                    elif message.id != existing.id:
                        try:
                            await message.delete()
                            logger.info("Mensaje duplicado de la cola eliminado (%s).", message.id)
                        except discord.HTTPException:
                            logger.warning("No se pudo eliminar un mensaje duplicado antiguo de la cola.")
                elif is_old_order_event:
                    # El canal configurado como cola debe contener únicamente el
                    # mensaje-lista, sin importar si el ID del canal de auditoría
                    # cambió en Render o quedó configurado incorrectamente.
                    try:
                        await message.delete()
                        logger.info("Publicación individual antigua eliminada del canal de cola (%s).", message.id)
                    except discord.HTTPException:
                        logger.warning("No se pudo eliminar una publicación individual antigua (%s).", message.id)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("No se pudo revisar el historial del canal de pedidos.")
            return

        try:
            if existing is not None:
                # Edita el mensaje actual, sin volver a enviarlo al canal.
                await existing.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                message = existing
                logger.info("Lista única de pedidos actualizada en el mensaje existente (%s).", message.id)
            else:
                # Solo crea un mensaje cuando no existe ninguno válido.
                message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                logger.info("Lista de pedidos creada en el canal %s (mensaje %s).", channel.id, message.id)
            await asyncio.to_thread(save_orders_queue_message_id, message.id)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("No se pudo crear o actualizar la lista única de pedidos.")


def build_orders_embed():
    embed = discord.Embed(
        title=ORDER_QUEUE_TITLE,
        description=(
            "Cola general de vehículos importados por encargo, ordenada "
            "por fecha de registro. Los pedidos se reservan y se gestionan "
            "por orden de llegada."
        ),
        colour=COLOR_IMPORTED,
        timestamp=datetime.now(timezone.utc)
    )
    pending = [o for o in orders_data["orders"] if o.get("status") == "pendiente"]
    if not pending:
        embed.add_field(name="Cola actual", value="No hay pedidos pendientes.", inline=False)
    else:
        lines = []
        for order in pending[:25]:
            customer = f"<@{order['customer_id']}>"
            ticket = f"[Abrir ticket]({order['ticket_url']})" if order.get("ticket_url") else "Sin enlace"
            lines.append(
                f"**#{order['id']}** · {order['model']}\n"
                f"Cliente: {customer} · {ticket}"
            )
        embed.add_field(name=f"Pendientes ({len(pending)})", value="\n\n".join(lines), inline=False)
        if len(pending) > 25:
            embed.set_footer(text=f"Mostrando 25 de {len(pending)} pedidos pendientes.")
    return embed


async def notify_customer(guild, customer_id, message):
    member = guild.get_member(customer_id)
    if member is None:
        try:
            member = await guild.fetch_member(customer_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            member = None
    if member is not None:
        try:
            await member.send(message)
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("No se pudo enviar DM al cliente %s.", customer_id)
    return False


class RegisterOrderModal(discord.ui.Modal, title="Registrar pedido por encargo"):
    model = discord.ui.TextInput(
        label="Modelo del vehículo solicitado",
        placeholder="Ej.: Pfister Comet S2",
        max_length=100,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        channel = interaction.channel
        guild = interaction.guild
        if guild is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Este formulario solo funciona dentro de un ticket del servidor.", ephemeral=True)
            return
        if not is_ticket_staff(interaction.user):
            await interaction.response.send_message("Solo el equipo de Coutt & Schutz puede registrar pedidos.", ephemeral=True)
            return
        customer_id = get_ticket_owner_id(channel)
        if customer_id is None:
            await interaction.response.send_message("No pude identificar al cliente dueño de este ticket.", ephemeral=True)
            return

        if not ORDERS_STORAGE_READY:
            await interaction.response.send_message(
                "El sistema de pedidos no puede confirmar el almacenamiento en este momento. "
                "No se registró el pedido para evitar perder información; avisa a un administrador.",
                ephemeral=True
            )
            return

        model_name = str(self.model.value).strip()
        async with orders_lock:
            # La numeración representa la posición actual en la cola, no el total
            # histórico de pedidos. Se conservan únicamente los pedidos pendientes.
            orders_data["orders"] = [
                o for o in orders_data["orders"] if o.get("status") == "pendiente"
            ]
            orders_data["orders"].sort(key=lambda o: o.get("created_at", ""))
            for position, existing_order in enumerate(orders_data["orders"], start=1):
                existing_order["id"] = position
            orders_data["next_id"] = len(orders_data["orders"]) + 1

            # Evita registrar dos veces el mismo modelo desde el mismo ticket.
            duplicate = next((o for o in orders_data["orders"] if o.get("ticket_id") == channel.id and o.get("model", "").casefold() == model_name.casefold()), None)
            if duplicate:
                await interaction.response.send_message(f"Ese vehículo ya está en la cola para este ticket: pedido **#{duplicate['id']}**.", ephemeral=True)
                return
            order_id = len(orders_data["orders"]) + 1
            order = {
                "id": order_id,
                "customer_id": customer_id,
                "model": model_name,
                "ticket_id": channel.id,
                "ticket_url": channel.jump_url,
                "guild_id": guild.id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "created_by": interaction.user.id,
                "status": "pendiente",
                "delivered_at": None,
                "delivered_by": None
            }
            orders_data["orders"].append(order)
            orders_data["next_id"] = order_id + 1
            try:
                save_orders()
            except Exception:
                logger.exception("No se pudo persistir el nuevo pedido.")
                orders_data["orders"].remove(order)
                orders_data["next_id"] = order_id
                await interaction.response.send_message("No pude guardar el pedido. Revisa el almacenamiento del bot e inténtalo otra vez.", ephemeral=True)
                return

        await interaction.response.send_message(f"Pedido **#{order_id}** registrado en la cola general. Cliente: <@{customer_id}> · Vehículo: **{model_name}**. El enlace de este ticket quedó guardado.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        # No se envía DM al cliente al añadir el pedido a la cola.
        # La confirmación visible para el cliente se publica únicamente en este ticket.
        await channel.send(
            f"🧾 Pedido **#{order_id}** registrado por {interaction.user.mention}: **{model_name}**. "
            f"Enlace guardado en la cola: {channel.jump_url}",
            allowed_mentions=discord.AllowedMentions.none()
        )
        await log_order_event(
            guild, "📦 Pedido por encargo registrado",
            f"Registrado por <@{interaction.user.id}>.", order
        )
        await refresh_orders_queue(guild)


class RegisterOrderButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Registrar pedido por encargo", emoji="📦", style=discord.ButtonStyle.success, custom_id=ORDER_REGISTER_BUTTON_ID)

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.TextChannel) or get_ticket_owner_id(interaction.channel) is None:
            await interaction.response.send_message("Este botón solo se puede usar dentro de un ticket válido.", ephemeral=True)
            return
        if not is_ticket_staff(interaction.user):
            await interaction.response.send_message("Solo el equipo de Coutt & Schutz puede registrar pedidos por encargo.", ephemeral=True)
            return
        await interaction.response.send_modal(RegisterOrderModal())


@bot.tree.command(name="pedido", description="Abre el formulario para registrar un vehículo por encargo en este ticket.")
async def pedido(interaction: discord.Interaction):
    """Alias slash para abrir el formulario de pedido desde un ticket válido."""
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel) or get_ticket_owner_id(channel) is None:
        await interaction.response.send_message(
            "Este comando solo funciona dentro de un ticket válido de Coutt & Schutz.",
            ephemeral=True
        )
        return
    if not is_ticket_staff(interaction.user):
        await interaction.response.send_message(
            "Solo el equipo de Coutt & Schutz puede registrar pedidos por encargo.",
            ephemeral=True
        )
        return
    await interaction.response.send_modal(RegisterOrderModal())


@bot.tree.command(name="pedido_cola", description="Muestra los pedidos por encargo pendientes, en orden de llegada.")
async def pedido_cola(interaction: discord.Interaction):
    if not is_ticket_staff(interaction.user):
        await interaction.response.send_message("Solo el equipo de Coutt & Schutz puede consultar la cola.", ephemeral=True)
        return
    await interaction.response.send_message(embed=build_orders_embed(), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="pedido_entregar", description="Marca un pedido por encargo como entregado y lo informa en el ticket.")
@app_commands.describe(numero="Número de pedido registrado en la cola")
async def pedido_entregar(interaction: discord.Interaction, numero: app_commands.Range[int, 1, 1000000]):
    if not is_ticket_staff(interaction.user):
        await interaction.response.send_message("Solo el equipo de Coutt & Schutz puede marcar pedidos como entregados.", ephemeral=True)
        return
    if not ORDERS_STORAGE_READY:
        await interaction.response.send_message(
            "El almacenamiento de pedidos no está disponible. No se modificó el pedido; avisa a un administrador.",
            ephemeral=True
        )
        return
    async with orders_lock:
        order = next((o for o in orders_data["orders"] if int(o.get("id", 0)) == numero), None)
        if order is None:
            await interaction.response.send_message(f"No existe el pedido **#{numero}**.", ephemeral=True)
            return
        if order.get("status") == "entregado":
            await interaction.response.send_message(f"El pedido **#{numero}** ya estaba marcado como entregado.", ephemeral=True)
            return
        order["status"] = "entregado"
        order["delivered_at"] = datetime.now(timezone.utc).isoformat()
        order["delivered_by"] = interaction.user.id
        # Al entregar, el pedido sale de la cola activa. Los restantes se
        # renumeran 1..N; el evento de entrega queda registrado en el canal de auditoría.
        old_orders = list(orders_data["orders"])
        old_next_id = orders_data["next_id"]
        orders_data["orders"] = [o for o in orders_data["orders"] if o is not order]
        orders_data["orders"].sort(key=lambda o: o.get("created_at", ""))
        for position, remaining_order in enumerate(orders_data["orders"], start=1):
            remaining_order["id"] = position
        orders_data["next_id"] = len(orders_data["orders"]) + 1
        try:
            save_orders()
        except Exception:
            orders_data["orders"] = old_orders
            orders_data["next_id"] = old_next_id
            order["status"] = "pendiente"
            order["delivered_at"] = None
            order["delivered_by"] = None
            await interaction.response.send_message("No pude guardar el cambio. Revisa el almacenamiento e inténtalo otra vez.", ephemeral=True)
            return

    guild = interaction.guild

    # No se envían mensajes privados al cliente ni al registrar ni al entregar.
    # La notificación de entrega se publica únicamente dentro del ticket original.
    ticket_notified = False
    if guild is not None and order.get("ticket_id"):
        ticket_channel = guild.get_channel(int(order["ticket_id"]))
        if ticket_channel is None:
            try:
                ticket_channel = await guild.fetch_channel(int(order["ticket_id"]))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                ticket_channel = None
        if isinstance(ticket_channel, discord.TextChannel):
            try:
                await ticket_channel.send(
                    f"✅ **Pedido #{numero} entregado**\n"
                    f"El vehículo **{order['model']}** fue marcado como entregado por {interaction.user.mention}. "
                    "¡Gracias por confiar en Coutt & Schutz!",
                    allowed_mentions=discord.AllowedMentions.none()
                )
                ticket_notified = True
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("No se pudo publicar la entrega en el ticket del pedido #%s.", numero)

    await log_order_event(
        guild, "✅ Pedido por encargo entregado",
        f"Marcado como entregado por <@{interaction.user.id}>.", order
    )
    await refresh_orders_queue(guild)
    ticket_status = "Se informó en el ticket." if ticket_notified else "El pedido se marcó como entregado, pero no se pudo publicar el aviso en el ticket."
    await interaction.response.send_message(f"Pedido **#{numero}** marcado como entregado. {ticket_status}", ephemeral=True)


load_orders()


async def _get_guild_channel(guild, channel_id):
    """Obtiene un canal por caché o por API, sin dejar caer el evento."""
    if guild is None or not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await guild.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        logger.exception("No se pudo acceder al canal configurado %s.", channel_id)
        return None


async def log_ticket_event(guild, title, description, channel=None, actor=None):
    """Escribe un evento de ticket en el canal de registro configurado."""
    log_channel = await _get_guild_channel(guild, TICKET_LOG_CHANNEL_ID)
    if not isinstance(log_channel, (discord.TextChannel, discord.Thread)):
        logger.error("El canal de registro de tickets %s no existe o no es un canal de texto.", TICKET_LOG_CHANNEL_ID)
        return False
    embed = discord.Embed(
        title=title,
        description=description,
        colour=TICKET_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    if channel is not None:
        embed.add_field(name="Ticket", value=f"`{channel.name}` · `{channel.id}`", inline=False)
        embed.add_field(name="Canal", value=f"[Abrir ticket]({channel.jump_url})", inline=True)
        owner_id = get_ticket_owner_id(channel)
        if owner_id:
            embed.add_field(name="Cliente", value=f"<@{owner_id}> (`{owner_id}`)", inline=True)
    if actor is not None:
        embed.add_field(name="Acción realizada por", value=f"{actor.mention} (`{actor.id}`)", inline=False)
    try:
        await log_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        return True
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("No se pudo publicar en el registro de tickets %s.", TICKET_LOG_CHANNEL_ID)
        return False


async def archive_ticket(channel, closed_by):
    """Guarda el historial en el foro antes de permitir borrar el ticket."""
    guild = channel.guild
    forum = await _get_guild_channel(guild, TICKET_ARCHIVE_FORUM_ID)
    if not isinstance(forum, discord.ForumChannel):
        logger.error("El canal de archivo %s no existe o no es un foro.", TICKET_ARCHIVE_FORUM_ID)
        return False

    try:
        messages = [message async for message in channel.history(limit=1000, oldest_first=True)]
        lines = []
        for message in messages:
            stamp = message.created_at.strftime("%Y-%m-%d %H:%M UTC")
            content = (message.content or "").strip()
            if message.embeds:
                embed_parts = []
                for embed in message.embeds:
                    if embed.title:
                        embed_parts.append(embed.title)
                    if embed.description:
                        embed_parts.append(embed.description)
                    embed_parts.extend(f"{field.name}: {field.value}" for field in embed.fields)
                if embed_parts:
                    content = (content + " | " if content else "") + "[Embed: " + " / ".join(embed_parts) + "]"
            if message.attachments:
                attachment_urls = " ".join(a.url for a in message.attachments)
                content = (content + " | " if content else "") + f"Adjuntos: {attachment_urls}"
            if message.stickers:
                content = (content + " | " if content else "") + "Stickers: " + ", ".join(s.name for s in message.stickers)
            if not content:
                content = "[Mensaje sin texto]"
            # Evita que un historial muy grande genere un único mensaje imposible de enviar.
            lines.append(f"[{stamp}] {message.author} ({message.author.id}): {content}")

        owner_id = get_ticket_owner_id(channel)
        transcript = "\n".join(lines) or "No se encontraron mensajes en el historial."
        header = (
            f"Ticket original: #{channel.name} (`{channel.id}`)\n"
            f"Cliente: <@{owner_id}>\n" if owner_id else f"Ticket original: #{channel.name} (`{channel.id}`)\n"
        ) + f"Cerrado por: {closed_by.mention} (`{closed_by.id}`)\nMensajes archivados: {len(messages)}\n\n"
        chunks = [transcript[i:i + 1700] for i in range(0, len(transcript), 1700)] or [transcript]
        first_content = header + "```text\n" + chunks[0] + "\n```"
        if len(first_content) > 1950:
            first_content = first_content[:1940] + "…"
        result = await forum.create_thread(
            name=f"{channel.name} · {channel.id}"[:100],
            content=first_content,
            reason=f"Archivo automático del ticket {channel.id}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        archive_thread = getattr(result, "thread", None)
        if archive_thread is None and isinstance(result, tuple):
            archive_thread = result[0]
        if archive_thread is None:
            logger.error("Discord no devolvió el hilo creado para archivar el ticket %s.", channel.id)
            return False
        for index, chunk in enumerate(chunks[1:], start=2):
            await archive_thread.send(
                f"**Continuación del historial ({index}/{len(chunks)})**\n```text\n{chunk}```",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await log_ticket_event(
            guild,
            "📦 Ticket archivado",
            f"El historial se guardó en {archive_thread.mention}.",
            channel=channel,
            actor=closed_by,
        )
        return True
    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
        logger.exception("No se pudo archivar el ticket %s; se conservará el canal original.", channel.id)
        return False
    except Exception:
        logger.exception("Error inesperado archivando el ticket %s; se conservará el canal original.", channel.id)
        return False


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
                "Se guardará el historial en el foro antes de eliminar el canal.",
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
            await asyncio.sleep(2)
            archived = await archive_ticket(self.channel, interaction.user)
            if not archived:
                await self.channel.send(
                    "⚠️ No pude archivar el historial en el foro. El ticket se conserva para evitar perder la conversación. "
                    "Revisa los permisos del bot y los IDs de registro/archivo.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await self.channel.delete(
                reason=f"Ticket cerrado por {interaction.user}"
            )
            await log_ticket_event(
                self.channel.guild,
                "🔒 Ticket cerrado",
                "El canal original se eliminó después de archivar el historial.",
                channel=self.channel,
                actor=interaction.user,
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
        container.add_item(discord.ui.ActionRow(RegisterOrderButton(), CloseTicketButton()))

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

    username = clean(member.display_name) or clean(member.name) or "usuario"
    return f"ticket-{username}"[:100]


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

        # Los tickets del panel de contacto van siempre a la categoría configurada.
        category = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
        if category is None and TICKET_CATEGORY_ID:
            try:
                category = await guild.fetch_channel(TICKET_CATEGORY_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.exception("No se pudo acceder a la categoría de tickets %s.", TICKET_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            logger.error("La categoría de tickets %s no existe o no es una categoría.", TICKET_CATEGORY_ID)
            await interaction.followup.send(
                "No se pudo crear el ticket porque la categoría de contacto no está disponible. Avisa a un administrador.",
                ephemeral=True,
            )
            return

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

        await log_ticket_event(
            guild,
            "🎫 Nuevo ticket de contacto",
            "Se creó un nuevo ticket desde el panel de contacto.",
            channel=channel,
            actor=member,
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
            await update_workers_list(member.guild)
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

    await update_workers_list(member.guild)


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
                    await update_stats_panel(message.guild)

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
# PANEL PERSISTENTE DE ESTADÍSTICAS
# ============================================================

def _collect_component_texts(components, output):
    """Recoge textos de publicaciones hechas con Components V2."""
    for component in components or []:
        content = getattr(component, "content", None)
        if isinstance(content, str) and content.strip():
            output.append(content.strip())
        children = getattr(component, "children", None)
        if children:
            _collect_component_texts(children, output)

def _parse_stats_price(text):
    """Extrae un precio numérico del texto de una publicación."""
    import re as _re
    match = _re.search(r"(?:\*\*Precio:\*\*|Precio:)\s*([^\n]+)", text, _re.IGNORECASE)
    if not match:
        return None
    value = match.group(1).strip()
    # Quitar emojis/texto posterior si se encuentra en la misma línea.
    digits = _re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else None

def _stats_vehicle_data(message):
    """Reconoce publicaciones de vehículos nuevas y antiguas del bot."""
    parts = []
    _collect_component_texts(message.components, parts)
    for embed in message.embeds:
        if embed.title:
            parts.append(embed.title)
        if embed.description:
            parts.append(embed.description)
        for field in embed.fields:
            parts.append(f"{field.name}: {field.value}")
    text = "\n".join(parts)
    if "COUTT & SCHUTZ" not in text.upper() and not message.embeds:
        return None
    name = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("## "):
            name = line[3:].strip().strip("*")
            break
    if not name and message.embeds:
        name = next((e.title for e in message.embeds if e.title), None)
    price = _parse_stats_price(text)
    if not name or price is None:
        return None
    return {"name": name, "price": price}

async def _get_catalog_stats(guild):
    """Lee el historial de los dos canales y calcula cifras reales."""
    data = {"used": [], "imported": []}
    for channel_id, kind in ((USED_VEHICLES_CHANNEL_ID, "used"),
                             (IMPORTED_VEHICLES_CHANNEL_ID, "imported")):
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.HTTPException:
                logger.exception("No se pudo acceder al canal de catálogo %s", channel_id)
                continue
        try:
            async for message in channel.history(limit=None):
                if message.author.id != bot.user.id:
                    continue
                vehicle = _stats_vehicle_data(message)
                if vehicle:
                    data[kind].append(vehicle)
        except discord.Forbidden:
            logger.error("Sin permiso para leer el historial del catálogo %s", channel_id)
        except discord.HTTPException:
            logger.exception("Error leyendo el catálogo %s", channel_id)
    return data

def build_stats_embed(data):
    used = data["used"]
    imported = data["imported"]
    all_vehicles = used + imported
    prices = [v["price"] for v in all_vehicles if v.get("price") is not None]
    average = round(sum(prices) / len(prices)) if prices else None
    highest = max(all_vehicles, key=lambda v: v["price"], default=None)

    def money(value):
        return f"${value:,}".replace(",", ".")

    embed = discord.Embed(
        title=STATS_PANEL_TITLE,
        description="Resumen actualizado del catálogo de vehículos.",
        colour=PANEL_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="🚘 Vehículos publicados", value=f"**{len(all_vehicles)}**", inline=True)
    embed.add_field(name="🔹 Usados", value=f"**{len(used)}**", inline=True)
    embed.add_field(name="🌎 Importados", value=f"**{len(imported)}**", inline=True)
    embed.add_field(name="💰 Precio promedio", value=f"**{money(average)}**" if average is not None else "Sin datos de precios", inline=True)
    if highest:
        embed.add_field(name="🏆 Vehículo de mayor precio", value=f"**{highest['name']}**\n{money(highest['price'])}", inline=True)
    else:
        embed.add_field(name="🏆 Vehículo de mayor precio", value="Sin vehículos publicados", inline=True)
    embed.set_footer(text="Coutt & Schutz · Estadísticas automáticas del catálogo")
    return embed

async def update_stats_panel(guild=None):
    """Publica el panel una vez y después edita el mensaje existente."""
    guild = guild or (bot.guilds[0] if bot.guilds else None)
    if guild is None:
        return
    channel = bot.get_channel(STATS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(STATS_CHANNEL_ID)
        except discord.HTTPException:
            logger.exception("No se pudo acceder al canal de estadísticas.")
            return
    try:
        data = await _get_catalog_stats(guild)
        embed = build_stats_embed(data)
        async for message in channel.history(limit=100):
            if message.author.id != bot.user.id:
                continue
            if any(e.title == STATS_PANEL_TITLE for e in message.embeds):
                await message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                logger.info("Panel de estadísticas actualizado en el mensaje existente.")
                return
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        logger.info("Panel de estadísticas publicado en el canal %s.", STATS_CHANNEL_ID)
    except discord.Forbidden:
        logger.error("El bot necesita ver el canal de estadísticas, enviar mensajes y leer el historial.")
    except discord.HTTPException:
        logger.exception("Error actualizando el panel de estadísticas.")


# ============================================================
# LISTA PÚBLICA DE TRABAJADORES
# ============================================================

def build_workers_embed(guild):
    """Construye una lista minimalista: un trabajador por línea, con su rango."""
    embed = discord.Embed(
        title="TRABAJADORES | COUTT & SCHUTZ",
        colour=PANEL_COLOR,
        timestamp=datetime.now(timezone.utc),
    )

    staff_role = guild.get_role(WORKERS_BASE_ROLE_ID)
    if staff_role is None:
        embed.description = "No hay trabajadores para mostrar."
        return embed

    staff_members = [
        member for member in guild.members
        if not member.bot and staff_role in member.roles
    ]

    # El orden de WORKER_RANKS define la jerarquía. Si una persona tiene
    # varios rangos, se muestra una sola vez con el rango más alto.
    rank_by_id = {role_id: label for role_id, label in WORKER_RANKS}
    rank_order = {role_id: index for index, (role_id, _label) in enumerate(WORKER_RANKS)}
    entries = []
    for member in staff_members:
        member_rank_ids = [role.id for role in member.roles if role.id in rank_by_id]
        if member_rank_ids:
            highest_rank_id = min(member_rank_ids, key=lambda role_id: rank_order[role_id])
            rank_label = rank_by_id[highest_rank_id]
            rank_index = rank_order[highest_rank_id]
        else:
            rank_label = "Sin rango asignado"
            rank_index = len(WORKER_RANKS)
        entries.append((rank_index, member.display_name.casefold(), member.display_name, rank_label))

    entries.sort(key=lambda item: (item[0], item[1]))
    if entries:
        embed.description = "\n".join(
            f"• **{name}** — {rank_label}"
            for _rank_index, _sort_name, name, rank_label in entries
        )
    else:
        embed.description = "*No hay trabajadores asignados.*"

    return embed


async def update_workers_list(guild=None):
    """Crea la lista una sola vez y edita ese mismo mensaje en futuras actualizaciones."""
    if guild is None:
        guild = bot.guilds[0] if bot.guilds else None
    if guild is None:
        logger.warning("No hay un servidor disponible para actualizar la lista de trabajadores.")
        return

    channel = bot.get_channel(WORKERS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(WORKERS_CHANNEL_ID)
        except discord.HTTPException:
            logger.exception("No se pudo acceder al canal de trabajadores.")
            return

    try:
        # Completa la caché de miembros para que la lista no quede incompleta al iniciar.
        try:
            async for _member in guild.fetch_members(limit=None):
                pass
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("No se pudieron cargar todos los miembros; se usará la caché disponible.")

        embed = build_workers_embed(guild)
        async for message in channel.history(limit=100):
            if message.author.id != bot.user.id:
                continue
            if any(e.title == "TRABAJADORES | COUTT & SCHUTZ" for e in message.embeds):
                await message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                logger.info("Lista de trabajadores actualizada en el mensaje existente.")
                return

        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        logger.info("Lista de trabajadores publicada en el canal %s.", WORKERS_CHANNEL_ID)
    except discord.Forbidden:
        logger.error("El bot necesita ver el canal de trabajadores, enviar mensajes y leer el historial.")
    except discord.HTTPException:
        logger.exception("Error al actualizar la lista de trabajadores.")


@bot.event
async def on_member_update(before, after):
    relevant_role_ids = {WORKERS_BASE_ROLE_ID, *(role_id for role_id, _ in WORKER_RANKS)}
    before_roles = {role.id for role in before.roles if role.id in relevant_role_ids}
    after_roles = {role.id for role in after.roles if role.id in relevant_role_ids}
    if before_roles != after_roles or before.display_name != after.display_name:
        await update_workers_list(after.guild)


@bot.event
async def on_member_remove(member):
    await update_workers_list(member.guild)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Evita que un comando obsoleto deje la interacción sin respuesta."""
    logger.error(
        "Error al procesar comando de aplicación: %s",
        error,
        exc_info=(type(error), error, error.__traceback__)
    )
    if interaction.response.is_done():
        return
    if isinstance(error, app_commands.CommandNotFound):
        message = (
            "Este comando es una versión antigua y ya no está registrado. "
            "Vuelve a escribir `/` y selecciona el comando actualizado."
        )
    else:
        message = "Ocurrió un error al ejecutar el comando. El equipo técnico ya tiene el registro del fallo."
    try:
        await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        logger.exception("No se pudo responder a la interacción que falló.")


# ============================================================
# INICIO
# ============================================================

@bot.event
async def on_message_delete(message: discord.Message):
    """Si borran la lista de pedidos, la vuelve a crear en el canal configurado."""
    if (
        message.channel.id == ORDER_QUEUE_CHANNEL_ID
        and message.author.id == getattr(bot.user, "id", None)
        and any(embed.title == ORDER_QUEUE_TITLE for embed in message.embeds)
    ):
        logger.warning("Se eliminó el mensaje único de pedidos; se restaurará automáticamente.")
        await refresh_orders_queue()


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
    await refresh_orders_queue()
    await ensure_v1_guide_post()
    await update_workers_list()
    await update_stats_panel()


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
