import os
import asyncio
from datetime import time
from playwright.async_api import async_playwright
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)

print("🛕 TTD BOT STARTING...")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

URL = "https://ttdevasthanams.ap.gov.in/home/dashboard"


# ----------------------------------------
# SCRAPE TTD WEBSITE
# ----------------------------------------

async def fetch_updates():

    async with async_playwright() as p:

        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        await page.goto(URL, timeout=60000)

        # wait for content to load
        await page.wait_for_timeout(5000)

        elements = await page.locator("li").all()

        updates = []

        for el in elements:

            text = await el.inner_text()

            if "New" in text:

                clean = text.replace("New", "").strip()

                if len(clean) > 5:
                    updates.append(clean)

        await browser.close()

        return updates


# ----------------------------------------
# FORMAT TELEGRAM MESSAGE
# ----------------------------------------

def format_message(updates):

    from datetime import datetime

    now = datetime.now().strftime("%d %b %Y | %I:%M %p")

    if updates:

        message = f"🛕 TTD NEW UPDATES\n\n📅 {now}\n\n"

        for i, update in enumerate(updates, 1):
            message += f"{i}. {update}\n"

    else:

        message = f"""🛕 TTD DAILY UPDATE

📅 {now}

No new notifications today.
"""

    message += "\n🔗 https://ttdevasthanams.ap.gov.in/home/dashboard"

    return message


# ----------------------------------------
# /start COMMAND
# ----------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [
        [InlineKeyboardButton("🔍 Check TTD Updates", callback_data="check")]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🛕 *TTD Update Bot*\n\nClick the button below to check latest updates.",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )


# ----------------------------------------
# BUTTON CLICK
# ----------------------------------------

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    updates = await fetch_updates()

    message = format_message(updates)

    await query.edit_message_text(message)


# ----------------------------------------
# DAILY 8AM JOB
# ----------------------------------------

async def daily_summary(context: ContextTypes.DEFAULT_TYPE):

    updates = await fetch_updates()

    message = format_message(updates)

    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=message
    )


# ----------------------------------------
# MAIN
# ----------------------------------------

def main():

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))

    job_queue = app.job_queue

    job_queue.run_daily(
        daily_summary,
        time=time(hour=8, minute=0)
    )

    print("🚀 TTD BOT RUNNING...")

    app.run_polling()


if __name__ == "__main__":
    main()