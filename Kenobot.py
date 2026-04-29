from telegram import Update, WebAppInfo, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = "8695584253:AAF8EYGpcNv_M-xY1mW-kox79O0HDWNuh8E"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Create a button that opens the Mini App
    button = KeyboardButton(
        text="🎲 Play Fast Keno",
        web_app=WebAppInfo(url="https://kenov2bot-v13onrender.com")  # replace with your server's URL
    )
    markup = ReplyKeyboardMarkup([[button]], resize_keyboard=True)
    await update.message.reply_text("Welcome! Tap the button to play.", reply_markup=markup)

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.run_polling()

if __name__ == "__main__":
    main()
