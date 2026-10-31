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
from curl_cffi.requests import AsyncSession

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, filters
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# Configuración para AnimeFLV
BASE_URL = "https://animeflv.net"
ANIME_LIST = ["black-clover", "jujutsu-kaisen"]

TEMP_DIR = Path("temp_videos")
TEMP_DIR.mkdir(exist_ok=True)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
estado = {"activo": True, "ultima_publicacion": "Nunca"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ---------- COMANDO START ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ No tienes permiso para usar este bot.")
        return
    await update.message.reply_text(
        f"¡Hola! Soy tu bot publicador de anime. 🤖\n\n"
        f"Para abrir el menú de control, usa el comando:\n👉 /panel"
    )

# ---------- SCRAPING CON ANIMEFLV ----------
async def obtener_ultimo_episodio(anime_slug: str):
    try:
        # Intentamos con httpx + User-Agent falso (suele funcionar en AnimeFLV)
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30) as client:
            r = await client.get(f"{BASE_URL}/anime/{anime_slug}")
            if r.status_code != 200:
                logging.error(f"Error {r.status_code} al acceder a {anime_slug}")
                return None, None
                
            soup = BeautifulSoup(r.text, "html.parser")
            # Selector de episodios de AnimeFLV
            episodios = soup.select("ul.ListEpisodios li a")
            if not episodios:
                logging.warning(f"No se encontraron episodios para {anime_slug}")
                return None, None
                
            ultimo_ep_url = urljoin(BASE_URL, episodios[-1]["href"])  # El último suele ser el más reciente
            partes_url = ultimo_ep_url.strip("/").split("/")
            num_capitulo = partes_url[-1] if partes_url[-1].isdigit() else "1"

            # Entrar al episodio para buscar enlaces de descarga
            r_ep = await client.get(ultimo_ep_url)
            soup_ep = BeautifulSoup(r_ep.text, "html.parser")
            
            enlace_descarga = None
            # AnimeFLV suele tener los enlaces en una tabla o en iframes
            for a in soup_ep.find_all("a", href=True):
                href = a["href"]
                if ".mp4" in href or "download" in href or "stream" in href:
                    enlace_descarga = href
                    break
            
            if enlace_descarga:
                return num_capitulo, enlace_descarga
            return None, None

    except Exception as e:
        logging.error(f"Error en scraping de {anime_slug}: {e}")
        return None, None

# ---------- DESCARGA Y DIVISIÓN ----------
async def descargar_video(url: str, destino: Path):
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=120) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(destino, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
    return destino

def dividir_video(ruta_entrada: Path, tamano_max_mb: int = 45):
    try:
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
    except Exception as e:
        logging.error(f"Error al dividir video: {e}")
        return [ruta_entrada]

# ---------- PUBLICACIÓN ----------
async def publicar_episodio(bot, nombre_anime: str, hashtag: str, capitulo: str, url_descarga: str):
    ruta_original = TEMP_DIR / f"{hashtag}_{capitulo}.mp4"
    try:
        logging.info(f"Descargando {nombre_anime} Cap {capitulo}...")
        await descargar_video(url_descarga, ruta_original)
        
        logging.info("Descarga completada. Dividiendo si es necesario...")
        partes = dividir_video(ruta_original)

        for i, parte in enumerate(partes, start=1):
            caption = f"**{nombre_anime}**\nCapítulo {capitulo}"
            if len(partes) > 1:
                caption += f" (Parte {i}/{len(partes)})"
            caption += f"\n\n#{hashtag}\n📢 Comparte: https://t.me/tu_canal"
            
            with open(parte, "rb") as video:
                await bot.send_video(
                    chat_id=CHANNEL_ID, video=video, caption=caption,
                    parse_mode="Markdown", supports_streaming=True
                )
            parte.unlink(missing_ok=True)
            
        estado["ultima_publicacion"] = f"{nombre_anime} - Cap {capitulo}"
        logging.info(f"Publicado exitosamente: {nombre_anime} Cap {capitulo}")
        
    except Exception as e:
        logging.error(f"Error publicando {nombre_anime} {capitulo}: {e}")
    finally:
        ruta_original.unlink(missing_ok=True)

# ---------- JOB AUTOMÁTICO ----------
async def job_publicacion(context: ContextTypes.DEFAULT_TYPE):
    if not estado["activo"]:
        return
    for anime_slug in ANIME_LIST:
        cap, url = await obtener_ultimo_episodio(anime_slug)
        if url:
            nombre = anime_slug.replace("-", " ").title()
            hashtag = "".join(filter(str.isalnum, nombre))
            await publicar_episodio(context.bot, nombre, hashtag, cap, url)

# ---------- PANEL DE CONTROL ----------
def construir_panel():
    texto = (
        f"🤖 **Panel de Control**\n\n"
        f"Estado: {'🟢 Activo' if estado['activo'] else '🔴 Pausado'}\n"
        f"Última publicación: {estado['ultima_publicacion']}\n"
        f"Animes en seguimiento: {len(ANIME_LIST)}"
    )
    teclado = [
        [InlineKeyboardButton("⏸️ Pausar" if estado["activo"] else "▶️ Reanudar", callback_data="toggle")],
        [InlineKeyboardButton("🔄 Forzar comprobación", callback_data="forzar")],
        [InlineKeyboardButton("📋 Ver animes", callback_data="lista")],
    ]
    return texto, InlineKeyboardMarkup(teclado)

async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text(f"⛔ No tienes permiso. Tu ID es {update.effective_user.id}")
        return
    texto, markup = construir_panel()
    await update.message.reply_text(texto, reply_markup=markup, parse_mode="Markdown")

async def boton_pulsado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != OWNER_ID: return

    if query.data == "toggle":
        estado["activo"] = not estado["activo"]
        texto, markup = construir_panel()
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="Markdown")
        
    elif query.data == "forzar":
        await query.edit_message_text("⏳ Buscando y publicando... (esto puede tardar)", parse_mode="Markdown")
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
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("panel", panel))
    app.add_handler(CallbackQueryHandler(boton_pulsado))
    app.job_queue.run_repeating(job_publicacion, interval=6*3600, first=10)
    
    logging.info("Bot iniciado correctamente. Esperando comandos...")
    app.run_polling()

if __name__ == "__main__":
    main()
