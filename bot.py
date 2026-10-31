import os
import math
import asyncio
import subprocess
import logging
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, filters
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
OWNER_ID = int(os.getenv("OWNER_ID"))

# Configuración de scraping (AnimeFLV como ejemplo)
BASE_URL = "https://www3.animeflv.net"
SEARCH_URL = f"{BASE_URL}/buscador"
ANIME_LIST = ["shingeki-no-kyojin", "jujutsu-kaisen"]  # IDs de ejemplo; ajústalos

TEMP_DIR = Path("temp_videos")
TEMP_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO)

# Estado global simple (en producción, usa SQLite o un JSON)
estado = {"activo": True, "ultima_publicacion": "Nunca"}

# ---------- SCRAPING LIGERO ----------
async def obtener_enlaces_480p(anime_id: str):
    """Devuelve una lista de (nombre_episodio, url_descarga_480p)."""
    enlaces = []
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        # 1. Página del anime para listar episodios
        r = await client.get(f"{BASE_URL}/anime/{anime_id}")
        soup = BeautifulSoup(r.text, "html.parser")
        # Buscar enlaces a episodios (ajusta el selector según la estructura actual)
        episodios = soup.select("ul.ListEpisodios li a")
        for ep in episodios:
            cap = ep.get_text(strip=True)
            ep_url = urljoin(BASE_URL, ep["href"])
            # 2. Página del episodio para extraer enlaces de servidores
            r_ep = await client.get(ep_url)
            soup_ep = BeautifulSoup(r_ep.text, "html.parser")
            # Buscar el enlace de descarga de 480p (selector de ejemplo)
            for link in soup_ep.select("a[href*='480p'], a:contains('480p')"):
                href = link.get("href")
                if href and ("480" in href or "480" in link.get_text().lower()):
                    enlaces.append((cap, href))
                    break  # solo el primero de 480p por episodio
    return enlaces

# ---------- DESCARGA Y DIVISIÓN ----------
async def descargar_video(url: str, destino: Path):
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(destino, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
    return destino

def dividir_video(ruta_entrada: Path, tamano_max_mb: int = 45):
    """Divide el video en partes de menos de 45 MB usando FFmpeg sin recodificar."""
    # Obtener duración
    cmd_dur = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(ruta_entrada)
    ]
    duracion = float(subprocess.check_output(cmd_dur).strip())
    tamano_mb = ruta_entrada.stat().st_size / (1024 * 1024)
    num_partes = max(1, math.ceil(tamano_mb / tamano_max_mb))
    dur_parte = duracion / num_partes

    patron_salida = str(ruta_entrada.with_suffix("")) + "_parte_%03d.mp4"
    cmd_div = [
        "ffmpeg", "-i", str(ruta_entrada), "-c", "copy", "-map", "0",
        "-f", "segment", "-segment_time", str(dur_parte),
        "-reset_timestamps", "1", patron_salida
    ]
    subprocess.run(cmd_div, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    partes = sorted([
        p for p in ruta_entrada.parent.glob(ruta_entrada.stem + "_parte_*.mp4")
    ])
    return partes

# ---------- PUBLICACIÓN ----------
async def publicar_episodio(bot, nombre_anime: str, hashtag: str, capitulo: str, url_descarga: str):
    ruta_original = TEMP_DIR / f"{hashtag}_{capitulo}.mp4"
    try:
        await descargar_video(url_descarga, ruta_original)
        partes = dividir_video(ruta_original)

        for i, parte in enumerate(partes, start=1):
            caption = (
                f"**{nombre_anime}**\n"
                f"Capítulo {capitulo} (Parte {i}/{len(partes)})\n"
                f"#{hashtag}\n"
                f"📢 Comparte: https://t.me/tu_canal"
            )
            with open(parte, "rb") as video:
                await bot.send_video(
                    chat_id=CHANNEL_ID, video=video, caption=caption,
                    parse_mode="Markdown", supports_streaming=True
                )
            parte.unlink(missing_ok=True)  # limpieza inmediata
    except Exception as e:
        logging.error(f"Error publicando {nombre_anime} {capitulo}: {e}")
    finally:
        ruta_original.unlink(missing_ok=True)

# ---------- JOB AUTOMÁTICO ----------
async def job_publicacion(context: ContextTypes.DEFAULT_TYPE):
    if not estado["activo"]:
        return
    for anime_id in ANIME_LIST:
        enlaces = await obtener_enlaces_480p(anime_id)
        # Aquí deberías llevar un registro de episodios ya publicados (SQLite/JSON)
        # Para el ejemplo, publicamos el último episodio encontrado
        if enlaces:
            cap, url = enlaces[-1]
            nombre = anime_id.replace("-", " ").title()
            hashtag = "".join(filter(str.isalnum, nombre))
            await publicar_episodio(context.bot, nombre, hashtag, cap, url)
            estado["ultima_publicacion"] = f"{nombre} - Cap {cap}"

# ---------- PANEL DE CONTROL ----------
def construir_panel():
    texto = (
        f"🤖 **Panel de Control**\n\n"
        f"Estado: {'🟢 Activo' if estado['activo'] else '🔴 Pausado'}\n"
        f"Última publicación: {estado['ultima_publicacion']}\n"
        f"Animes en seguimiento: {len(ANIME_LIST)}"
    )
    teclado = [
        [InlineKeyboardButton(
            "⏸️ Pausar" if estado["activo"] else "▶️ Reanudar",
            callback_data="toggle"
        )],
        [InlineKeyboardButton("🔄 Forzar comprobación", callback_data="forzar")],
        [InlineKeyboardButton("📋 Ver animes", callback_data="lista")],
    ]
    return texto, InlineKeyboardMarkup(teclado)

async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return  # solo el dueño puede usar el panel
    texto, markup = construir_panel()
    await update.message.reply_text(texto, reply_markup=markup, parse_mode="Markdown")

async def boton_pulsado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != OWNER_ID:
        return

    if query.data == "toggle":
        estado["activo"] = not estado["activo"]
        texto, markup = construir_panel()
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="Markdown")
    elif query.data == "forzar":
        await job_publicacion(context)
        texto, markup = construir_panel()
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="Markdown")
    elif query.data == "lista":
        lista = "\n".join(f"• {a.replace('-', ' ').title()}" for a in ANIME_LIST)
        texto = f"**Animes en seguimiento:**\n{lista}"
        await query.edit_message_text(texto, parse_mode="Markdown")

# ---------- MAIN ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("panel", panel, filters=filters.User(OWNER_ID)))
    app.add_handler(CallbackQueryHandler(boton_pulsado))

    # Job cada 6 horas (ajusta el intervalo)
    app.job_queue.run_repeating(job_publicacion, interval=6*3600, first=10)

    app.run_polling()

if __name__ == "__main__":
    main()