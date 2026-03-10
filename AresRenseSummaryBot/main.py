import os
import io
import anthropic
import requests
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from pypdf import PdfReader
from youtube_transcript_api import YouTubeTranscriptApi
from telegram.ext import Application, MessageHandler, filters

load_dotenv()
TOKEN = os.environ.get("TELEGRAM_TOKEN")
client = anthropic.Anthropic()


def is_allowed(user_id):
    return True


def is_youtube_url(text):
    return "youtube.com/watch" in text or "youtu.be" in text


def is_url(text):
    return text.startswith("http://") or text.startswith("https://")


def get_youtube_transcript(url):
    if "youtu.be/" in url:
        video_id = url.split("youtu.be/")[1].split("?")[0]
    else:
        video_id = url.split("v=")[1].split("&")[0]

    transcript = YouTubeTranscriptApi.get_transcript(video_id)
    return " ".join([t["text"] for t in transcript])


def get_webpage_text(url):
    response = requests.get(url, timeout=10)
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)[:10000]


async def summarize(content, lang="Русский"):
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
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

    await update.message.reply_text("Конспектируем...")
    summary = await summarize(content)
    await update.message.reply_text(summary)


async def handle_pdf(update, context):
    await update.message.reply_text("PDF получен!")
    file = await context.bot.get_file(update.message.document.file_id)
    file_bytes = await file.download_as_bytearray()

    reader = PdfReader(io.BytesIO(file_bytes))
    text = " ".join([page.extract_text() for page in reader.pages])

    summary = await summarize(text)
    await update.message.reply_text(summary)

app = Application.builder().token(TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT, handle_message))
app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
app.run_polling()
