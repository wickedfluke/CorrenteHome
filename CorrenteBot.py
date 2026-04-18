import requests
import time
import json
import subprocess
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = "8768567297:AAFi2g7iKdDJKW349hO8PirzRZkMT7fb4Hw"
CHECK_INTERVAL = 30  # secondi
LOG_FILE = "log.json"
USERS_FILE = "users.json"

# -----------------------
# Utility
# -----------------------

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def load_json(file, default):
    try:
        with open(file, "r") as f:
            return json.load(f)
    except:
        return default

def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=2)

# -----------------------
# Batteria
# -----------------------

def get_battery():
    try:
        result = subprocess.check_output(["termux-battery-status"])
        data = json.loads(result)
        return data["percentage"], data["plugged"]
    except:
        return None, None

# -----------------------
# Internet check
# -----------------------

def check_internet():
    try:
        requests.get("https://8.8.8.8", timeout=3)
        return True
    except:
        return False

# -----------------------
# Telegram utenti
# -----------------------

def add_user(user_id):
    users = load_json(USERS_FILE, [])
    if user_id not in users:
        users.append(user_id)
        save_json(USERS_FILE, users)

async def send_all(app, text):
    users = load_json(USERS_FILE, [])
    for uid in users:
        try:
            await app.bot.send_message(chat_id=uid, text=text)
        except:
            pass

# -----------------------
# Log downtime
# -----------------------

def add_log(entry):
    logs = load_json(LOG_FILE, [])
    logs.append(entry)
    save_json(LOG_FILE, logs)

# -----------------------
# BOT COMMANDS
# -----------------------

async def modem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_user.id)

    online = check_internet()

    if online:
        await update.message.reply_text("✅ Internet attivo")
    else:
        await update.message.reply_text("❌ Internet NON disponibile")

async def graph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_user.id)

    file = generate_graph()
    if file:
        await context.bot.send_photo(chat_id=update.effective_user.id, photo=open(file, "rb"))
    else:
        await update.message.reply_text("Nessun dato disponibile")

# -----------------------
# MONITOR LOOP
# -----------------------

import threading

def start_monitor(app):
    import asyncio

    async def monitor():
        last_online = True
        down_start = None
        battery_start = None

        while True:
            online = check_internet()
            print("CHECK:", online)

            if online:
                if not last_online:
                    down_end = datetime.now()
                    duration = int((down_end - down_start).total_seconds())

                    add_log({
                        "start": down_start.strftime("%Y-%m-%d %H:%M:%S"),
                        "end": down_end.strftime("%Y-%m-%d %H:%M:%S"),
                        "duration": duration
                    })

                    users = load_json(USERS_FILE, [])
                    for uid in users:
                        await app.bot.send_message(
                            chat_id=uid,
                            text=f"🟢 Internet tornato\nDurata down: {duration}s"
                        )

                    last_online = True

            else:
                if last_online:
                    down_start = datetime.now()
                    battery_start = get_battery()
                    last_online = False

            await asyncio.sleep(30)

    asyncio.run(monitor())

# -----------------------
# MAIN
# -----------------------

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("modem", modem))

    threading.Thread(target=start_monitor, args=(app,), daemon=True).start()

    print("Bot avviato...")
    app.run_polling()

if __name__ == "__main__":
    main()
