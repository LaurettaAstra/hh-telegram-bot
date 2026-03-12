import os
import asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from hh_api import search_vacancies

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
USER_ID_RAW = os.getenv("TELEGRAM_USER_ID")

print("BOT_TOKEN loaded:", bool(BOT_TOKEN))
print("TELEGRAM_USER_ID loaded:", USER_ID_RAW)

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в .env")

if not USER_ID_RAW:
    raise ValueError("Не найден TELEGRAM_USER_ID в .env")

USER_ID = int(USER_ID_RAW)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        await update.message.reply_text("Доступ запрещен")
        return

    await update.message.reply_text(
        "Бот работает.\n\n"
        "Текущий фильтр:\n"
        "• только Системный / Бизнес аналитик\n"
        "• удаленка\n"
        "• без слов: 1С, Битрикс, DWH, Lead, Senior\n"
        "• зарплата может быть не указана\n"
        "• выборка: 300 последних вакансий\n\n"
        "Команды:\n"
        "/jobs - показать вакансии"
    )


async def jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        await update.message.reply_text("Доступ запрещен")
        return

    await update.message.reply_text("Ищу подходящие вакансии среди 300 последних...")

    try:
        vacancies = search_vacancies()
    except Exception as error:
        await update.message.reply_text(f"Ошибка при запросе к HH: {error}")
        return

    if not vacancies:
        await update.message.reply_text(
            "Подходящих вакансий не найдено.\n"
            "Сейчас фильтр такой:\n"
            "• только системный или бизнес-аналитик\n"
            "• только удаленка\n"
            "• без 1С / Битрикс / DWH / Lead / Senior\n"
            "• проверяются 300 последних вакансий"
        )
        return

    for vacancy in vacancies[:15]:
        text = (
            f"💼 {vacancy['name']}\n"
            f"🏢 {vacancy['company']}\n"
            f"💰 {vacancy['salary']}\n"
            f"📍 {vacancy['area']}\n"
            f"🖥 {vacancy['schedule']}\n"
            f"{vacancy['url']}"
        )
        await update.message.reply_text(text)


def main():
    print("Запускаем бота...")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("jobs", jobs))

    print("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()