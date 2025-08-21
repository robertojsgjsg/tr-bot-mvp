# SPDX-License-Identifier: MIT
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, date
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo
import httpx

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, Application, CommandHandler, ContextTypes

from .memory.rest import RestMemory, make_fingerprint
from .providers.base import BaseProvider
from .providers.dummy import DummyProvider

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", "10000"))
TZ = os.getenv("TZ", "Europe/Madrid")
DAY_OFFSET_DEFAULT = int(os.getenv("DAY_OFFSET_DEFAULT", "1"))
TOP_K = int(os.getenv("TOP_K", "4"))
MEMORY_TTL_DAYS = int(os.getenv("MEMORY_TTL_DAYS", "14"))
MEMORY_NAMESPACE = os.getenv("MEMORY_NAMESPACE", "pickmem")
REDIS_REST_URL = os.getenv("REDIS_REST_URL", "")
REDIS_REST_TOKEN = os.getenv("REDIS_REST_TOKEN", "")
PROVIDER_NAME = os.getenv("PROVIDER", "dummy")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logger = logging.getLogger("bot")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

def get_provider(name: str) -> BaseProvider:
    name = (name or "dummy").strip().lower()
    if name == "dummy":
        return DummyProvider()
    # Más adelante: TradeRepublicProvider()
    return DummyProvider()

def parse_date_arg(arg: str) -> date:
    from datetime import datetime as dt
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return dt.strptime(arg.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError("Formato de fecha no válido. Usa dd/mm/aaaa (p.ej. 21/08/2025).")

def fmt_item(item: Dict[str, Any]) -> str:
    dt_txt = item.get("date", "")
    league = item.get("league") or item.get("category") or ""
    name = item.get("name", "")
    market = item.get("market", "")
    selection = item.get("selection", "")
    price = item.get("price", "")
    src = item.get("source", "")
    value = item.get("value")
    value_txt = f"\nValor: {value:+.2%}" if isinstance(value, (int, float)) else ""
    league_txt = f"[{league}] " if league else ""
    return (f"{league_txt}{dt_txt}\n"
            f"• {name}\n"
            f"• {market}: *{selection}* @ *{price}*{value_txt}\n"
            f"Fuente: {src}")

def build_fingerprint_payload(item: Dict[str, Any]) -> str:
    keys = ["date", "league", "name", "market", "selection"]
    return "|".join(str(item.get(k, "")) for k in keys)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = ("¡Hola! 👋\n\n"
            "Este bot está desplegado con *webhook* en Render y soporta proveedor pluggable.\n"
            "Comandos:\n"
            "/today — elementos de hoy\n"
            "/tomorrow — elementos de mañana\n"
            "/picks — por defecto usa DAY_OFFSET_DEFAULT\n"
            "/day dd/mm/aaaa — fecha concreta\n"
            "/week — próximos 7 días\n"
            "/meminfo — info sobre memoria de deduplicación\n"
            "/forgetall — aviso sobre limitaciones REST\n")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def _send_items(update: Update, context: ContextTypes.DEFAULT_TYPE, day_from: date, day_to: date, label: str) -> None:
    tz = ZoneInfo(TZ)
    chat_id = update.effective_chat.id if update.effective_chat else None
    provider = get_provider(PROVIDER_NAME)
    async with httpx.AsyncClient(timeout=httpx.Timeout(6.0)) as client:
        mem = RestMemory(REDIS_REST_URL, REDIS_REST_TOKEN, client=client) if (REDIS_REST_URL and REDIS_REST_TOKEN) else None
        try:
            items = await provider.get_items(day_from, day_to, top_k=TOP_K)
        except Exception as e:
            await update.message.reply_text(f"⚠️ Error al obtener datos del proveedor: {e}")
            return
        if not items:
            await update.message.reply_text(f"Sin resultados para {label}.")
            return
        ttl_seconds = max(60, MEMORY_TTL_DAYS * 24 * 3600)
        sent = 0
        for item in items:
            payload = build_fingerprint_payload(item)
            fp = make_fingerprint(MEMORY_NAMESPACE, str(chat_id), payload)
            should_skip = False
            if mem:
                try:
                    exists = await mem.exists(fp)
                    if exists:
                        should_skip = True
                    else:
                        await mem.setex(fp, ttl_seconds, "1")
                except Exception:
                    pass
            if should_skip:
                continue
            await update.message.reply_text(fmt_item(item), parse_mode=ParseMode.MARKDOWN)
            sent += 1
        if sent == 0:
            await update.message.reply_text("No hay novedades (todo estaba ya enviado previamente).")

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tz = ZoneInfo(TZ)
    today = datetime.now(tz).date()
    await _send_items(update, context, today, today, "hoy")

async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tz = ZoneInfo(TZ)
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
    await _send_items(update, context, tomorrow, tomorrow, "mañana")

async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tz = ZoneInfo(TZ)
    d = (datetime.now(tz) + timedelta(days=DAY_OFFSET_DEFAULT)).date()
    await _send_items(update, context, d, d, f"día +{DAY_OFFSET_DEFAULT}")

async def cmd_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Uso: /day dd/mm/aaaa")
        return
    try:
        d = parse_date_arg(context.args[0])
    except ValueError as e:
        await update.message.reply_text(str(e))
        return
    await _send_items(update, context, d, d, d.strftime("%d/%m/%Y"))

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tz = ZoneInfo(TZ)
    start = datetime.now(tz).date()
    end = start + timedelta(days=6)
    await _send_items(update, context, start, end, "próximos 7 días")

async def cmd_meminfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = ("ℹ️ *Memoria (Upstash REST)*\n"
            "- Se usa deduplicación por huella (SHA-256) con TTL.\n"
            "- Por limitaciones de REST, no se listan ni borran claves por prefijo.\n"
            "- Tip: cambia `MEMORY_NAMESPACE` para un reset lógico inmediato.")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_forgetall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = ("⚠️ *Limpiado masivo no disponible con REST.*\n"
            "Cambia `MEMORY_NAMESPACE` en Environment y redeploy para un reset lógico.")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

def build_app() -> Application:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("picks", cmd_picks))
    app.add_handler(CommandHandler("day", cmd_day))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("meminfo", cmd_meminfo))
    app.add_handler(CommandHandler("forgetall", cmd_forgetall))
    return app

def main() -> None:
    app = build_app()
    if WEBHOOK_URL:
        url = WEBHOOK_URL.rstrip("/")
        path = TELEGRAM_BOT_TOKEN
        full = f"{url}/{path}"
        logger.info("Starting webhook at %s", full)
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=path, webhook_url=full)
    else:
        logger.info("WEBHOOK_URL not set; polling mode")
        app.run_polling()

if __name__ == "__main__":
    main()
