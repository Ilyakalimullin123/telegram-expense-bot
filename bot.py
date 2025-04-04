import os
import logging
from dotenv import load_dotenv
from openai import OpenAI
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
from pathlib import Path
import asyncio
import nest_asyncio
import re
import matplotlib.pyplot as plt
import pandas as pd

from telegram import Update, ReplyKeyboardMarkup, BotCommand, InputFile
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from telegram.constants import ParseMode

# Загрузка переменных окружения из tokens.env
dotenv_path = Path('.') / 'tokens.env'
load_dotenv(dotenv_path)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")

print("🔁 Загруженные переменные:")
print("TELEGRAM_TOKEN =", TELEGRAM_TOKEN)
print("OPENAI_API_KEY =", OPENAI_API_KEY)
print("GOOGLE_SHEET_NAME =", GOOGLE_SHEET_NAME)

# Настройка OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# Настройка Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client_gs = gspread.authorize(credentials)
print("🔍 Ищу таблицу:", GOOGLE_SHEET_NAME)

sheets = client_gs.openall()
print("📄 Таблицы, к которым у меня есть доступ:")
for s in sheets:
    print("-", s.title)

sheet = client_gs.open(GOOGLE_SHEET_NAME).sheet1

# Логирование
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

CATEGORY_KEYWORDS = {
    "еда": ["кофе", "обед", "ужин", "завтрак", "перекус", "бургер", "пицца"],
    "топливо": ["заправка", "бензин", "газ", "дизель"],
    "транспорт": ["такси", "автобус", "метро", "маршрутка", "транспорт"],
    "развлечения": ["кино", "театр", "игра", "подписка", "музыка"],
    "Сигареты": ["стики", "сиги", "сигареты", "сижки", "курево"]
}

def detect_category(text: str):
    text_lower = text.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        for word in keywords:
            if word in text_lower:
                return category
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Классифицируй текст по категории расхода: еда, топливо, транспорт, развлечения, другое."},
                {"role": "user", "content": text}
            ]
        )
        result = response.choices[0].message.content.strip().lower()
        return result if result in CATEGORY_KEYWORDS else "другое"
    except:
        return "другое"

def extract_amount(text: str):
    # Сначала попробуем найти явное число
    match = re.search(r"\d+[.,]?\d*", text)
    if match:
        return match.group().replace(',', '.')

    # Если число прописью, запрашиваем у GPT
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Извлеки числовое значение из этого текста. Просто напиши число в цифрах, без текста и символов."},
                {"role": "user", "content": text}
            ]
        )
        value = response.choices[0].message.content.strip()
        if re.match(r"^\d+[.,]?\d*$", value):
            return value.replace(',', '.')
    except Exception as e:
        logging.error("Ошибка при извлечении суммы: %s", e)
    return ""

def add_to_sheet(text: str):
    date = datetime.now().strftime("%Y-%m-%d %H:%M")
    amount = extract_amount(text)
    category = detect_category(text)
    comment = text.capitalize()
    sheet.append_row([date, category, amount, comment])



def has_entries_today():
    today = datetime.now().strftime("%Y-%m-%d")
    values = sheet.get_all_values()[1:]
    return any(row and row[0].startswith(today) for row in values)

async def recognize_voice(file_path):
    with open(file_path, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file
        )
    return transcript.text

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    logging.info(f"📩 Получено: {text}")
    if text.lower() == "итого за сегодня":
        return await total_today(update, context)
    if text.lower() == "график":
        return await send_chart(update)
    if text.lower() == "экспорт":
        return await export_excel(update)
    add_to_sheet(text)
    await update.message.reply_text("📜 Сообщение записано!")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.voice.get_file()
    file_path = "voice.ogg"
    await file.download_to_drive(file_path)
    try:
        text = await recognize_voice(file_path)
        logging.info(f"🎤 Распознано: {text}")
        add_to_sheet(text)
        await update.message.reply_text(f"📄 Записано: {text}")
    except Exception as e:
        await update.message.reply_text("❌ Ошибка при распознавании")
        logging.error(e)
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("🚀 Бот получил команду /start")
    keyboard = [["Итого за сегодня", "График"], ["Экспорт"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "Привет! Я готов записывать твои расходы. Просто отправь сообщение с суммой и категорией.",
        reply_markup=reply_markup
    )

async def debug_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.debug("📦 Пришло что-то от Telegram!")
    logging.debug(update)

async def total_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    values = sheet.get_all_values()[1:]
    daily_sum = 0.0
    for row in values:
        if row and row[0].startswith(today):
            try:
                daily_sum += float(row[2])
            except:
                continue
    await update.message.reply_text(f"📊 Расходы за сегодня: {daily_sum:.2f} тг")

async def send_chart(update: Update):
    values = sheet.get_all_values()[1:]
    daily_totals = {}
    for row in values:
        if row and len(row) >= 3:
            date = row[0][:10]
            try:
                amount = float(row[2])
            except:
                continue
            daily_totals[date] = daily_totals.get(date, 0) + amount

    dates = sorted(daily_totals.keys())
    sums = [daily_totals[date] for date in dates]

    plt.figure(figsize=(10, 5))
    plt.plot(dates, sums, marker='o')
    plt.xticks(rotation=45)
    plt.title("Расходы по дням")
    plt.xlabel("Дата")
    plt.ylabel("Сумма, тг")
    plt.tight_layout()
    chart_file = "chart.png"
    plt.savefig(chart_file)
    plt.close()

    with open(chart_file, 'rb') as photo:
        await update.message.reply_photo(photo)

async def export_excel(update: Update):
    values = sheet.get_all_values()
    df = pd.DataFrame(values)
    filename = f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    df.to_excel(filename, index=False, header=False)
    with open(filename, 'rb') as f:
        await update.message.reply_document(document=InputFile(f, filename))

async def send_daily_report(app):
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    values = sheet.get_all_values()[1:]
    daily_sum = 0.0
    for row in values:
        if row and row[0].startswith(today):
            try:
                daily_sum += float(row[2])
            except:
                continue

    message = f"📊 Расходы за {today}: {daily_sum:.2f} тг"

    if OWNER_CHAT_ID:
        if has_entries_today():
            await app.bot.send_message(chat_id=OWNER_CHAT_ID, text=message)
        else:
            await app.bot.send_message(chat_id=OWNER_CHAT_ID, text="🔔 Сегодня ещё не было записей о расходах!")

    sheet.append_row([f"**{today} итого**", "", f"{daily_sum:.2f}", ""], value_input_option="USER_ENTERED")

async def schedule_loop(app):
    while True:
        now = datetime.now()
        next_run = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        wait_seconds = (next_run - now).total_seconds()
        await asyncio.sleep(wait_seconds)
        await send_daily_report(app)

async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    await app.bot.set_my_commands([
        BotCommand("start", "Приветствие и инструкция"),
        BotCommand("total", "Показать сумму расходов за сегодня"),
        BotCommand("chart", "График расходов по дням"),
        BotCommand("export", "Экспорт в Excel")
    ])

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("total", total_today))
    app.add_handler(CommandHandler("chart", send_chart))
    app.add_handler(CommandHandler("export", export_excel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.ALL, debug_all_messages))

    logging.info("✅ Бот запущен")
    asyncio.create_task(schedule_loop(app))
    await app.run_polling()

if __name__ == "__main__":
    nest_asyncio.apply()
    asyncio.run(main())
