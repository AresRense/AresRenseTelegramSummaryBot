import os
import io
import anthropic
import requests
import asyncio
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from pypdf import PdfReader
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
from telegram.ext import Application, MessageHandler, filters, CommandHandler, ContextTypes
from telegram import Update

load_dotenv()
TOKEN = os.environ.get("TELEGRAM_TOKEN")
client = anthropic.Anthropic()

ALLOWED_USERS = [
    int(uid.strip())
    for uid in os.environ.get("ALLOWED_USERS", "").split(",")
    if uid.strip()
]


def is_allowed(user_id):
    return user_id in ALLOWED_USERS


def is_youtube_url(text):
    return any(x in text for x in ["youtube.com/watch", "youtu.be", "youtube.com/shorts/"])


def is_url(text):
    return text.startswith("http://") or text.startswith("https://")


def get_youtube_transcript(url):
    if "youtu.be/" in url:
        video_id = url.split("youtu.be/")[1].split("?")[0]
    elif "shorts" in url:
        video_id = url.split("shorts/")[1].split("?")[0]
    else:
        video_id = url.split("v=")[1].split("&")[0]

    try:
        ytt = YouTubeTranscriptApi()
        transcript = ytt.fetch(video_id, languages=['ru', 'en'])
    except NoTranscriptFound:
        transcript_list = ytt.list(video_id)
        transcript = transcript_list.find_any_transcript().fetch()

    full_text = " ".join([t.text for t in transcript])
    return full_text[:12000]


async def summarize(content, lang="Русский"):
    return await asyncio.to_thread(_summarize_sync, content, lang)


def get_webpage_text(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, timeout=10, headers=headers)
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)[:10000]


def _summarize_sync(content, lang="Русский"):
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[
            {"role": "user", "content": f"Summarize the following conscisely in {lang}:\n\n{content}"}
        ]
    )
    return message.content[0].text


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "привет, я бот который делает конспекты.\n"
        "отправь мне:\n"
        "любой текст\n"
        "любую ссылку(могу YouTube)\n"
        "PDF файл\n"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip()

    if not is_allowed(user_id):
        await update.message.reply_text("Access denied")
        return

    await update.message.reply_text("Конспектируем...")

    try:
        if is_youtube_url(text):
            content = get_youtube_transcript(text)
        elif is_url(text):
            content = get_webpage_text(text)
        else:
            content = text

        summary = await summarize(content)
        await update.message.reply_text(summary)

    except (TranscriptsDisabled, NoTranscriptFound):
        await update.message.reply_text("у этого видео нет субтитров")
    except requests.exceptions.RequestException:
        await update.message.reply_text("не получилось загрузить страницу")
    except Exception as e:
        await update.message.reply_text(f"ошибка: {str(e)}")


async def handle_pdf(update, context):
    await update.message.reply_text("PDF получен!")
    try:
        file = await context.bot.get_file(update.message.document.file_id)
        file_bytes = await file.download_as_bytearray()

        reader = PdfReader(io.BytesIO(file_bytes))
        text = " ".join([page.extract_text() or "" for page in reader.pages])

        if not text.strip():
            await update.message.reply_text("не удалось извлечь текст из пдф")
            return

        summary = await summarize(text[:12000])
        await update.message.reply_text(summary)

    except Exception as e:
        await update.message.reply_text(f"ошибка обработки пдф: {str(e)}")

app = Application.builder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(
    filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
app.run_polling()
