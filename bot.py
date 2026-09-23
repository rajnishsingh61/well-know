import os
import io
import json
import asyncio
import uuid
import logging
import threading
import aiohttp

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================
# CONFIG
# =========================

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
COOLDOWN = int(os.getenv("COOLDOWN_SECONDS", "14"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))

# Render automatically provides PORT. Local fallback = 10000.
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

jobs = {}
locks = {}


# =========================
# RENDER HEALTH SERVER
# =========================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            body = b"OK"

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8",
            )
            self.send_header(
                "Content-Length",
                str(len(body)),
            )
            self.end_headers()

            self.wfile.write(body)

        else:
            body = b"Not Found"

            self.send_response(404)
            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8",
            )
            self.send_header(
                "Content-Length",
                str(len(body)),
            )
            self.end_headers()

            self.wfile.write(body)

    def log_message(self, format, *args):
        return


def start_health_server():
    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler,
    )

    logging.info(
        "Health server listening on 0.0.0.0:%s",
        PORT,
    )

    server.serve_forever()


# =========================
# JOB MANAGEMENT
# =========================

def get_job(uid):
    return jobs.setdefault(
        uid,
        {
            "state": "idle",
            "name_prefix": None,
            "password_prefix": None,
            "target": 0,
            "records": [],
            "success": 0,
            "failed": 0,
            "duplicates": 0,
            "request_no": 0,
            "cooldown": 0,
            "stop": False,
        },
    )


def reset_job(j):
    j.update(
        {
            "state": "idle",
            "name_prefix": None,
            "password_prefix": None,
            "target": 0,
            "records": [],
            "success": 0,
            "failed": 0,
            "duplicates": 0,
            "request_no": 0,
            "cooldown": 0,
            "stop": False,
        }
    )


def menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ New Job",
                    callback_data="new",
                )
            ],
            [
                InlineKeyboardButton(
                    "📊 Status",
                    callback_data="status",
                ),
                InlineKeyboardButton(
                    "🛑 Stop",
                    callback_data="stop",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📄 JSON",
                    callback_data="json",
                )
            ],
        ]
    )


def status_text(j):
    return (
        f"🟢 State: {j['state']}\n"
        f"📦 Progress: {len(j['records'])}/{j['target']}\n"
        f"✅ Success requests: {j['success']}\n"
        f"❌ Failed requests: {j['failed']}\n"
        f"♻️ Duplicates: {j['duplicates']}\n"
        f"🔄 Request: #{j['request_no']}\n"
        f"⏳ Cooldown: {j['cooldown']}s"
    )


# =========================
# REAL API INTEGRATION
# =========================

async def fetch_real_api(
    name_prefix,
    password_prefix,
    count,
):
    """
    Fetches dynamic data asynchronously from Vercel API.
    """
    url = f"https://guestidgen.vercel.app/api/generate/{count}"
    params = {
        "name": name_prefix,
        "password": password_prefix,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()

                    if isinstance(data, list):
                        records = data
                    elif isinstance(data, dict):
                        records = data.get("data", data.get("results", []))
                    else:
                        records = []

                    return {
                        "success": True,
                        "data": records,
                    }
                else:
                    logging.error("API Error Status: %s", resp.status)
                    return {"success": False, "data": []}

    except Exception as error:
        logging.exception("API Request failed: %s", error)
        return {"success": False, "data": []}


# =========================
# START
# =========================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    uid = update.effective_user.id
    j = get_job(uid)

    if j["state"] == "running":
        await update.message.reply_text(
            "⚠️ Ek job already running hai.\n\n"
            + status_text(j),
            reply_markup=menu(),
        )
        return

    reset_job(j)

    j["state"] = "name"

    await update.message.reply_text(
        "👤 Name prefix bhejo:"
    )


# =========================
# TEXT INPUT
# =========================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    uid = update.effective_user.id
    j = get_job(uid)

    value = update.message.text.strip()

    if j["state"] == "name":

        if not value:
            await update.message.reply_text(
                "Name prefix empty nahi ho sakta."
            )
            return

        j["name_prefix"] = value
        j["state"] = "password"

        await update.message.reply_text(
            "🔐 Password prefix bhejo:"
        )

    elif j["state"] == "password":

        if not value:
            await update.message.reply_text(
                "Password prefix empty nahi ho sakta."
            )
            return

        j["password_prefix"] = value
        j["state"] = "count"

        await update.message.reply_text(
            "🔢 Target count bhejo:"
        )

    elif j["state"] == "count":

        try:
            count = int(value)

        except ValueError:
            await update.message.reply_text(
                "⚠️ Count number me bhejo."
            )
            return

        if not 1 <= count <= 100000:
            await update.message.reply_text(
                "⚠️ Count 1 se 100000 ke beech rakho."
            )
            return

        j["target"] = count
        j["state"] = "confirm"

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "▶️ START",
                        callback_data="startjob",
                    ),
                    InlineKeyboardButton(
                        "❌ CANCEL",
                        callback_data="cancel",
                    ),
                ]
            ]
        )

        await update.message.reply_text(
            "📋 Job Details\n\n"
            f"Name prefix: {j['name_prefix']}\n"
            f"Password prefix: {j['password_prefix']}\n"
            f"Target: {j['target']}\n\n"
            "Start karna hai?",
            reply_markup=keyboard,
        )


# =========================
# CALLBACKS
# =========================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    uid = query.from_user.id
    j = get_job(uid)

    if query.data == "new":

        if j["state"] == "running":
            await query.message.reply_text(
                "⚠️ Pehle running job stop karo."
            )
            return

        reset_job(j)

        j["state"] = "name"

        await query.message.reply_text(
            "👤 Name prefix bhejo:"
        )

    elif query.data == "cancel":

        reset_job(j)

        await query.message.reply_text(
            "❌ Job cancelled.",
            reply_markup=menu(),
        )

    elif query.data == "startjob":

        if j["state"] == "running":
            await query.message.reply_text(
                "⚠️ Job already running hai."
            )
            return

        if not j["name_prefix"]:
            await query.message.reply_text(
                "⚠️ Name prefix missing hai."
            )
            return

        if not j["password_prefix"]:
            await query.message.reply_text(
                "⚠️ Password prefix missing hai."
            )
            return

        if j["target"] <= 0:
            await query.message.reply_text(
                "⚠️ Target count invalid hai."
            )
            return

        j["state"] = "running"
        j["stop"] = False

        locks.setdefault(
            uid,
            asyncio.Lock(),
        )

        await query.message.reply_text(
            "🚀 Job started.\n\n"
            + status_text(j),
            reply_markup=menu(),
        )

        asyncio.create_task(
            worker(
                uid,
                context.application,
            )
        )

    elif query.data == "status":

        await query.message.reply_text(
            status_text(j),
            reply_markup=menu(),
        )

    elif query.data == "stop":

        if j["state"] == "running":

            j["stop"] = True

            await query.message.reply_text(
                "🛑 Stop requested.\n"
                "Current request finish hone ke baad JSON send hoga."
            )

        else:

            await query.message.reply_text(
                "ℹ️ Koi running job nahi hai.",
                reply_markup=menu(),
            )

    elif query.data == "json":

        if not j["records"]:

            await query.message.reply_text(
                "⚠️ Abhi koi record available nahi hai.",
                reply_markup=menu(),
            )
            return

        await send_json(
            query.message,
            j,
        )


# =========================
# WORKER
# =========================

async def worker(uid, app):

    j = get_job(uid)

    seen = {
        record["uid"]
        for record in j["records"]
        if record.get("uid")
    }

    async with locks[uid]:

        while (
            len(j["records"]) < j["target"]
            and not j["stop"]
        ):

            j["request_no"] += 1

            batch = min(
                BATCH_SIZE,
                j["target"] - len(j["records"]),
            )

            try:
                response = await fetch_real_api(
                    j["name_prefix"],
                    j["password_prefix"],
                    batch,
                )

                if not response.get("success"):
                    raise RuntimeError(
                        "API returned success=false"
                    )

                j["success"] += 1

                for item in response.get("data", []):
                    # Maps 'garena_uid' to 'uid' field
                    record_uid = (
                        item.get("garena_uid")
                        or item.get("uid") 
                        or item.get("id") 
                        or item.get("username")
                    )
                    record_pwd = (
                        item.get("password") 
                        or item.get("pass")
                    )

                    record = {
                        "uid": record_uid,
                        "password": record_pwd,
                    }

                    if not record["uid"]:
                        j["duplicates"] += 1
                        continue

                    if record["uid"] in seen:
                        j["duplicates"] += 1
                        continue

                    seen.add(record["uid"])

                    j["records"].append(record)

                    if len(j["records"]) >= j["target"]:
                        break

                await app.bot.send_message(
                    chat_id=uid,
                    text=status_text(j),
                )

            except Exception as error:

                j["failed"] += 1

                logging.exception(
                    "Request failed: %s",
                    error,
                )

                await app.bot.send_message(
                    chat_id=uid,
                    text=(
                        f"⚠️ Request #{j['request_no']} failed.\n"
                        "Retry continue rahega."
                    ),
                )

                await asyncio.sleep(3)

                continue

            # Cooldown execution
            for left in range(
                COOLDOWN,
                0,
                -1,
            ):

                if j["stop"]:
                    break

                j["cooldown"] = left

                await asyncio.sleep(1)

            j["cooldown"] = 0

        stopped = j["stop"]

        if stopped:
            title = "🛑 JOB STOPPED"
        else:
            title = "✅ TARGET COMPLETE"

        await app.bot.send_message(
            chat_id=uid,
            text=(
                f"{title}\n\n"
                + status_text(j)
                + "\n\n"
                "📄 account.json send ho raha hai..."
            ),
            reply_markup=menu(),
        )

        sent = await send_json_to_chat(
            uid,
            app,
            j,
        )

        if sent:

            reset_job(j)

            await app.bot.send_message(
                chat_id=uid,
                text=(
                    "✅ account.json delivered.\n"
                    "Job reset ho gaya."
                ),
                reply_markup=menu(),
            )


# =========================
# JSON GENERATION
# =========================

def json_bytes(j):

    payload = {
        "dataset_type": "generated/api",
        "count": len(j["records"]),
        "success_requests": j["success"],
        "failed_requests": j["failed"],
        "duplicates": j["duplicates"],
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "records": j["records"],
    }

    return json.dumps(
        payload,
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8")


async def send_json(msg, j):

    data = json_bytes(j)

    file_buffer = io.BytesIO(data)

    file_buffer.seek(0)

    await msg.reply_document(
        document=InputFile(
            file_buffer,
            filename="account.json",
        ),
        caption=(
            "📄 account.json\n"
            f"Records: {len(j['records'])}"
        ),
    )


async def send_json_to_chat(
    uid,
    app,
    j,
):

    try:

        data = json_bytes(j)

        file_buffer = io.BytesIO(data)

        file_buffer.seek(0)

        await app.bot.send_document(
            chat_id=uid,
            document=InputFile(
                file_buffer,
                filename="account.json",
            ),
            caption=(
                "📄 account.json\n"
                f"Records: {len(j['records'])}"
            ),
        )

        return True

    except Exception as error:

        logging.exception(
            "JSON delivery failed: %s",
            error,
        )

        await app.bot.send_message(
            chat_id=uid,
            text=(
                "❌ account.json send nahi ho paya.\n"
                "Records reset nahi kiye gaye."
            ),
        )

        return False


# =========================
# MAIN
# =========================

def main():

    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN .env me set karo."
        )

    health_thread = threading.Thread(
        target=start_health_server,
        daemon=True,
    )

    health_thread.start()

    application = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    logging.info(
        "Bot started. Render PORT=%s",
        PORT,
    )

    application.run_polling()


if __name__ == "__main__":
    main()
