import requests
import time
import json
import subprocess
import threading
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = "8768567297:AAFi2g7iKdDJKW349hO8PirzRZkMT7fb4Hw"
CHECK_INTERVAL = 30

LOG_FILE = "log.json"
USERS_FILE = "users.json"

# -----------------------
# UTIL
# -----------------------

def load_json(file, default):
    try:
        with open(file, "r") as f:
            return json.load(f)
    except:
        return default

def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=2)

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# -----------------------
# BATTERIA
# -----------------------

def get_battery():
    try:
        result = subprocess.check_output(["termux-battery-status"])
        data = json.loads(result)
        return data.get("percentage"), data.get("plugged")
    except:
        return None, None

# -----------------------
# INTERNET CHECK (FIXED)
# -----------------------

def check_internet():
    try:
        requests.get("https://1.1.1.1", timeout=3)
        return True
    except:
        return False

# -----------------------
# USERS
# -----------------------

def add_user(user_id):
    users = load_json(USERS_FILE, [])
    if user_id not in users:
        users.append(user_id)
        save_json(USERS_FILE, users)

def get_users():
    return load_json(USERS_FILE, [])

# -----------------------
# LOG
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

    if check_internet():
        await update.message.reply_text("✅ Internet attivo")
    else:
        await update.message.reply_text("❌ Internet NON disponibile")

# -----------------------
# MONITOR (THREAD STABILE)
# -----------------------

def monitor(app):
    last_online = True
    down_start = None
    battery_start = None

    while True:
        try:
            online = check_internet()
            print("CHECK:", online)

            if online:
                if not last_online:
                    down_end = datetime.now()
                    duration = int((down_end - down_start).total_seconds())

                    batt_now, plugged_now = get_battery()

                    add_log({
                        "start": down_start.strftime("%Y-%m-%d %H:%M:%S"),
                        "end": down_end.strftime("%Y-%m-%d %H:%M:%S"),
                        "duration": duration,
                        "battery_start": battery_start,
                        "battery_end": batt_now,
                        "plugged_start": battery_start[1] if battery_start else None,
                        "plugged_end": plugged_now
                    })

                    msg = (
                        f"🟢 Internet tornato\n"
                        f"⏱ Durata: {duration} sec\n"
                    )

                    if battery_start and batt_now:
                        msg += f"🔋 Batteria: {battery_start[0]}% → {batt_now}%\n"

                        if battery_start[1] and not plugged_now:
                            msg += "⚡ Possibile blackout\n"
                        else:
                            msg += "🌐 Probabile problema rete\n"

                    for uid in get_users():
                        try:
                            app.bot.send_message(chat_id=uid, text=msg)
                        except:
                            pass

                    last_online = True

            else:
                if last_online:
                    down_start = datetime.now()
                    battery_start = get_battery()
                    last_online = False

        except Exception as e:
            print("MONITOR ERROR:", e)

        time.sleep(CHECK_INTERVAL)

# -----------------------
# MAIN
# -----------------------

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("modem", modem))

    t = threading.Thread(target=monitor, args=(app,), daemon=True)
    t.start()

    print("Bot avviato...")
    app.run_polling()

if __name__ == "__main__":
    main()
