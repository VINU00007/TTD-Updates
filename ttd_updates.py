import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime, time as dt_time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.request import HTTPXRequest


print("🛕 TTD BOT STARTING...")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

URL = "https://ttdevasthanams.ap.gov.in/home/dashboard"


# -------------------------
# SCRAPE TTD WEBSITE
# -------------------------

def fetch_updates():

    response = requests.get(URL, timeout=20)

    soup = BeautifulSoup(response.text, "html.parser")

    updates = []

    items = soup.find_all("li")

    for item in items:

        text = item.get_text(strip=True)

        if "New" in text:

            clean = text.replace("New", "").strip()

            if len(clean) > 5:
                updates.append(clean)

    return updates


# -------------------------
# FORMAT MESSAGE
# -------------------------

def format_message(updates):

    now = datetime.now().strftime("%d %b %Y | %I:%M %p")

    if updates:

        message = f"🛕 TTD NEW UPDATES\n\n📅 {now}\n\n"

        for i, u in enumerate(updates, 1):
            message += f"{i}. {u}\n"

    else:

        message = f"""🛕 TTD UPDATE

📅 {now}

No new notifications today.
"""

    message += "\n🔗 https://ttdevasthanams.ap.gov.in/home/dashboard"

    return message


# -------------------------
# /start COMMAND
# -------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [[InlineKeyboardButton("🔍 Check TTD Updates", callback_data="check")]]

    await update.message.reply_text(
        "🛕 TTD Update Bot\n\nClick the button below to check latest updates.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# -------------------------
# BUTTON HANDLER
# -------------------------

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    updates = fetch_updates()

    message = format_message(updates)

    await query.edit_message_text(message)


# -------------------------
# DAILY SUMMARY
# -------------------------

async def daily_summary(context: ContextTypes.DEFAULT_TYPE):

    updates = fetch_updates()

    message = format_message(updates)

    await context.bot.send_message(chat_id=CHAT_ID, text=message)


# -------------------------
# MAIN
# -------------------------

def main():

    request = HTTPXRequest(connect_timeout=30, read_timeout=30)

    app = ApplicationBuilder().token(BOT_TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))

    job_queue = app.job_queue

    job_queue.run_daily(
        daily_summary,
        time=dt_time(hour=8, minute=0)
    )

    print("🚀 TTD BOT RUNNING...")

    app.run_polling()


if __name__ == "__main__":
    main()