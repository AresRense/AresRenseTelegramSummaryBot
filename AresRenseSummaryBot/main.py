import os
import anthropic
from dotenv import load_dotenv
from telegram.ext import Application, MessageHandler, filters

load_dotenv()
TOKEN = os.environ.get("TELEGRAM_TOKEN")
AI = anthropic.Anthropic()


async def summarize(content, lang="Русский"):
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[
            {"role": "user", "content": f"Summarize the following conscisely in {lang}:\n\n{content}"}
        ]
    )
    return message.content[0].text


async def handle_message(update, context):
    user_id = update.message.from_user.id
    text = update.message.text

    if not is_allowed(user_id):
        await update.message.reply_text("Access denied")
        return

    if is_youtube_url(text):
        content = get_youtube_transcript(text)
    elif is_url(text):
        content = get_webpage_text(text)
    else:
        content = text

app = Application.builder().token(TOKEN).build()

app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
app.add_handler(MessageHandler(filters.VOICE, handle_voice))

app.run_polling()
