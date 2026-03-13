import os
import io
import anthropic
import requests
import asyncio
import math
import re
import time
import threading
import subprocess
import random
import litellm
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from pypdf import PdfReader
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
from telegram.ext import Application, MessageHandler, filters, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup

load_dotenv()
TOKEN = os.environ.get("TELEGRAM_TOKEN")
client = anthropic.Anthropic()

ALLOWED_USERS = [
    int(uid.strip())
    for uid in os.environ.get("ALLOWED_USERS", "").split(",")
    if uid.strip()
]

MODEL = "claude-3-haiku-20240307"
MAX_INPUT_TOKENS = 200_000
MAX_OUTPUT_TOKENS = 4_096

pending: dict[int, dict] = {}

LITELLM_PRICE_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"

MAX_SUMMARY_INPUT = math.floor(MAX_OUTPUT_TOKENS / 0.15)


def get_model_pricing() -> tuple[float, float]:
    for get_map in [
        lambda: litellm.get_model_cost_map(url=LITELLM_PRICE_URL),
        lambda: litellm.model_cost,
    ]:
        try:
            data = get_map().get(MODEL, {})
            input_per_t = data.get("input_cost_per_token")
            output_per_t = data.get("output_cost_per_token")
            if input_per_t and output_per_t:
                return input_per_t * 1_000_000, output_per_t * 1_000_000
        except Exception:
            continue
    raise RuntimeError("не получилось вывести цену запроса")


_rub_cache: dict = {}
RUB_CACHE_TTL = 60 * 30


def calculate_optimal_output_tokens(input_tokens: int) -> int:
    ratio = random.randint(10, 20) / 100
    return min(math.ceil(input_tokens * ratio), MAX_OUTPUT_TOKENS)


def calculate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    input_per_m, output_per_m = get_model_pricing()
    return (input_tokens / 1_000_000) * input_per_m + (output_tokens / 1_000_000) * output_per_m


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def _find_split_position(text: str, target_pos: int, search_radius: int, source: str = "text", text_type: str = "scientific") -> int:

    lo = max(0, target_pos - search_radius)
    window = text[lo:target_pos]

    sent_ends = [lo + m.end()
                 for m in re.finditer(r'(?<![0-9])[.!?]\s+', window)]

    if not sent_ends:
        hi_fb = min(len(text), target_pos + 300)
        first_punct = next(
            (m.start() + 1 for m in re.finditer(r'[.!?]', text[target_pos:hi_fb])), None)
        return target_pos + first_punct if first_punct is not None else target_pos

    last_end = sent_ends[-1]
    after_last = text[last_end:target_pos].strip()
    if after_last:
        sent_ends = sent_ends[:-1]
        if not sent_ends:
            return last_end

    for i in range(len(sent_ends) - 1, 0, -1):
        right_sents = []
        for j in range(i, min(i + 5, len(sent_ends))):
            start = sent_ends[j - 1]
            end = sent_ends[j]
            s = text[start:end].strip()
            if s:
                right_sents.append(s)

        left_sents = []
        for j in range(i - 1, max(i - 6, -1), -1):
            start = sent_ends[j - 1] if j > 0 else lo
            end = sent_ends[j]
            s = text[start:end].strip()
            if s:
                left_sents.append(s)

        if not left_sents or not right_sents:
            continue
        block_before = " ".join(reversed(left_sents))
        block_after = " ".join(right_sents)
        if not _is_split_compatible(text, sent_ends[i], len(block_before) + len(block_after)):
            return sent_ends[i]

    return sent_ends[-1]


def get_rub_rate() -> float:
    now = time.time()
    if _rub_cache and (now - _rub_cache.get("fetched_at", 0)) < RUB_CACHE_TTL:
        return _rub_cache["rate"]
    try:
        response = requests.get(
            'https://api.exchangerate-api.com/v4/latest/USD', timeout=5)
        response.raise_for_status()
        rate = float(response.json()["rates"]["RUB"])
        _rub_cache["rate"] = rate
        _rub_cache["fetched_at"] = now
        return rate
    except Exception:
        fallback = 90.0
        _rub_cache["rate"] = fallback
        return fallback


def _update_notes(current_notes: str, z: str) -> str:
    prompt = (
        f"You are maintaining a reference scratchpad for summarizing a long text section by section.\n\n"
        f"CURRENT SCRATCHPAD:\n{current_notes}\n\n" if current_notes else
        f"You are maintaining a reference scratchpad for summarizing a long text section by section.\n\n"
        f"CURRENT SCRATCHPAD: (empty)\n\n"
    ) + (
        f"NEW SECTION SUMMARY:\n{z}\n\n"
        f"Update the scratchpad by adding any new key facts, character names, scientific terms, "
        f"locations, and concepts from the new section. Remove nothing already there. "
        f"Keep entries concise. Reply only with the updated scratchpad as a bullet list"
    )
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception:
        return current_notes


def _summarize_all_sync(plan: list[list[str]], lang: str = "Русский") -> str:
    zs = []
    notes = ""

    for sub_chunks in plan:
        for sc in sub_chunks:
            out = calculate_optimal_output_tokens(estimate_tokens(sc))
            z = _call_claude_sync(sc, out, lang, notes)
            zs.append(z)
            notes = _update_notes(notes, z)

    return "\n\n".join(zs)


async def summarize_content(content: str, lang: str = "Русский", source: str = "text", text_type: str = "scientific") -> str:
    plan = plan_chunks(content, source, text_type)
    return await asyncio.to_thread(_summarize_all_sync, plan, lang)


def _call_claude_sync(chunk: str, max_tokens: int, lang: str = "Русский", notes: str = "") -> str:
    message = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[
            {"role": "user", "content": f"{notes}Summarize the following concisely in {lang}:\n\n{chunk}"}]
    )
    return message.content[0].text


def _is_split_compatible(text: str, pos: int, para_sample: int) -> bool:
    before = text[max(0, pos - para_sample):pos].strip().split('.')[-1].strip()
    after = text[pos:min(len(text), pos + para_sample)
                 ].strip().split('.')[0].strip()
    prompt = (
        "You are evaluating a text split point. "
        "Read the last sentence of part A and the first sentence of part B.\n\n"
        f"LAST SENTENCE OF PART A:\n{before}\n\n"
        f"FIRST SENTENCE OF PART B:\n{after}\n\n"
        "Do these two sentence continue the same though, or do they represent distinct meaning sections?\n"
        "Reply with exactly one word: CONTINUOUS or DISTINCT"
    )
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=5,
            messages=[{"role": "user", "content": prompt}]
        )
        answer = response.content[0].text.strip().upper()
        return answer == "CONTINUOUS"
    except Exception:
        return False


def split_by_meaning(text: str, num_chunks: int, source: str = "text", text_type: str = "scientific") -> list[str]:
    if num_chunks <= 1:
        return [text]

    total_len = len(text)
    chunk_size = total_len / num_chunks

    if source == "youtube" and text_type == "entertainment":
        step = total_len // num_chunks
        boundaries = [i * step for i in range(num_chunks)] + [total_len]
        return [
            text[boundaries[i]:boundaries[i + 1]].strip()
            for i in range(len(boundaries) - 1)
            if text[boundaries[i]:boundaries[i + 1]].strip()
        ]

    if source == "youtube" and text_type == "scientific":
        search_radius = max(500, int(chunk_size * 0.12))
    else:
        search_radius = max(200, min(600, int(chunk_size * 0.05)))

    split_positions: list[int] = []
    for i in range(1, num_chunks):
        target = int(chunk_size * i)
        pos = _find_split_position(
            text, target, search_radius, source, text_type)
        prev = split_positions[-1] if split_positions else 0
        if prev < pos < total_len:
            split_positions.append(pos)

    boundaries = [0] + split_positions + [total_len]
    return [
        text[boundaries[i]:boundaries[i + 1]].strip()
        for i in range(len(boundaries) - 1)
        if text[boundaries[i]:boundaries[i + 1]].strip()
    ]


def _split_x_from_x(content: str, source: str, text_type: str) -> list[str]:
    input_tokens = estimate_tokens(content)
    if input_tokens <= MAX_INPUT_TOKENS:
        return [content]
    total_len = len(content)
    num_x = math.ceil(input_tokens / MAX_INPUT_TOKENS)
    search_radius = max(200, int(total_len / num_x * 0.10))
    targets = [int(total_len / num_x * i) for i in range(1, num_x)]
    splits = [0]
    for t in targets:
        pos = _find_split_position(
            content, t, search_radius, source, text_type)
        if splits[-1] < pos < total_len:
            splits.append(pos)
    splits.append(total_len)
    raw = [content[splits[i]:splits[i+1]].strip() for i in range(
        len(splits)-1) if content[splits[i]:splits[i+1]].strip()]

    result = []
    carry = ""
    for i, chunk in enumerate(raw):
        text_chunk = (carry + " " + chunk).strip() if carry else chunk
        carry = ""
        if i < len(raw) - 1:
            last_punct = max(text_chunk.rfind(
                '.'), text_chunk.rfind('!'), text_chunk.rfind('?'))
            if last_punct != -1 and last_punct < len(text_chunk) - 1:
                carry = text_chunk[last_punct + 1:].strip()
                text_chunk = text_chunk[:last_punct + 1].strip()
        result.append(text_chunk)
    return result


def _split_y_from_x(x: str, source: str, text_type: str) -> list[str]:
    y_tokens = estimate_tokens(x)
    if y_tokens <= MAX_SUMMARY_INPUT:
        return [x]
    x_len = len(x)
    num_y = math.ceil(y_tokens / MAX_SUMMARY_INPUT)
    search_radius = max(300, int(x_len / num_y * 0.10))
    targets = [int(x_len / num_y * i) for i in range(1, num_y)]
    splits = [0]
    for t in targets:
        pos = _find_split_position(x, t, search_radius, source, text_type)
        if splits[-1] < pos < x_len:
            splits.append(pos)
    splits.append(x_len)
    return [x[splits[i]:splits[i+1]].strip() for i in range(len(splits)-1) if x[splits[i]:splits[i+1]].strip()]


def plan_chunks(content: str, source: str = "text", text_type: str = "scientific") -> list[list[str]]:
    x_list = _split_x_from_x(content, source, text_type)
    return [_split_y_from_x(x, source, text_type) for x in x_list]


def _estimate_para_sample(text: str, source: str = "text", text_type: str = "scientific") -> int:
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    num_paras = len(paragraphs)
    if num_paras >= 2:
        avg_para = len(text) / num_paras
    else:
        sentences = re.split(r'(?<=[.!?])\s+', text)
        avg_sent = len(text) / max(len(sentences), 1)
        avg_para = avg_sent * 5

    if source == "youtube" and text_type == "entertainment":
        lo, hi = 200, 600
    elif source == "youtube" and text_type == "scientific":
        lo, hi = 400, 1200
    elif text_type == "fictional":
        lo, hi = 600, 2000
    elif text_type == "scientific":
        lo, hi = 300, 1200
    else:
        lo, hi = 300, 1000

    return max(lo, min(hi, math.ceil(avg_para)))


def estimate_plan_tokens(plan: list[list[str]]) -> tuple[int, int, int]:
    total_in, total_out, total_calls = 0, 0, 0
    y_out_tokens: list[int] = []

    for sub_chunks in plan:
        if len(sub_chunks) == 1:
            y_in = estimate_tokens(sub_chunks[0])
            y_out = calculate_optimal_output_tokens(y_in)
            total_in += y_in
            total_out += y_out
            total_calls += 1
            y_out_tokens.append(y_out)
        else:
            sub_out_sum = 0
            for sc in sub_chunks:
                sc_out = calculate_optimal_output_tokens(estimate_tokens(sc))
                total_in += estimate_tokens(sc)
                total_out += sc_out
                total_calls += 1
                sub_out_sum += sc_out

            total_in += sub_out_sum
            total_out += MAX_OUTPUT_TOKENS
            total_calls += 1
            y_out_tokens.append(MAX_OUTPUT_TOKENS)

    if len(plan) > 1:
        total_in += sum(y_out_tokens)
        total_out += MAX_OUTPUT_TOKENS
        total_calls += 1

    return total_in, total_out, total_calls


def check_integrity(content: str) -> tuple[bool, str]:
    if not content or not content.strip():
        return False, "пустой текст"
    if len(content.strip()) < 20:
        return False, "слишком короткий текст"
    return True, ""


def build_cost_message(content: str) -> tuple[str, float]:
    input_tokens = estimate_tokens(content)
    if input_tokens > MAX_INPUT_TOKENS:
        num_chunks = math.ceil(input_tokens / MAX_INPUT_TOKENS)
        chunk_size = math.ceil(input_tokens / num_chunks)
        per_chunk_out = calculate_optimal_output_tokens(chunk_size)
        combining_in = per_chunk_out * num_chunks
        total_input = input_tokens + combining_in
        total_output = (MAX_OUTPUT_TOKENS * num_chunks) + MAX_OUTPUT_TOKENS
    else:
        num_chunks = 1
        combining_in = 0
        total_input = input_tokens
        total_output = calculate_optimal_output_tokens(input_tokens)
    try:
        input_per_m, output_per_m = get_model_pricing()
        cost_usd = calculate_cost_usd(total_input, total_output)
        cost_cents = cost_usd * 100
        rub_rate = get_rub_rate()
        cost_rubs = cost_usd * rub_rate
        cost_block = (
            f"цены {MODEL}:\n"
            f"рассмотрение текста для конспектирования: {input_per_m}/млн токенов\n"
            f"генерация конспекта: {output_per_m}/млн токенов\n"
            f"стоимость данного запроса:\n"
            f"{cost_cents:.4f}$\n"
            f"{cost_rubs:.4f}руб (курс {rub_rate:.2f} руб/$)\n\n"
        )
        cost_usd_result = 0.0
    except RuntimeError:
        cost_block = "ошибка рассчёта\n"
        cost_usd_result = 0.0

    message = (
        f"запрос\n"
        f"модель: {MODEL}\n"
        f"{cost_block}"
        f"подтвердить?"
    )
    return message, cost_usd_result


def _upgrade_litellm():
    try:
        subprocess.run(
            ["pip", "install", "--upgrade", "litellm",
                "--break-system-packages", "-q"],
            check=True
        )

        litellm.register_model(
            model_cost="https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
        )
        print("[litellm] updated successfully")
    except Exception as e:
        print(f"[litellm] update failed {e}")


def schedule_litellm_updates(interval_days: int = 7):
    def loop():
        while True:
            _upgrade_litellm()
            time.sleep(interval_days * 24 * 60 * 60)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()


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
            {"role": "user", "content": f"Summarize the following concisely in {lang}:\n\n{content}"}
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

        summary = await summarize_content(content)
        await update.message.reply_text(summary)

    except (TranscriptsDisabled, NoTranscriptFound):
        await update.message.reply_text("у этого видео нет субтитров")
    except requests.exceptions.RequestException:
        await update.message.reply_text("не получилось загрузить страницу")
    except Exception as e:
        await update.message.reply_text(f"ошибка: {str(e)}")


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("недопуск")
        return
    await update.message.reply_text("pdf принят")
    try:
        file = await context.bot.get_file(update.message.document.file_id)
        file_bytes = await file.download_as_bytearray()
        reader = PdfReader(io.BytesIO(file_bytes))
        content = " ".join(
            [page.extract_text() or "" for page in reader.pages])
        if not content.strip():
            await update.message.reply_text("текст не был извлечён")
            return
        valid, error = check_integrity(content)
        if not valid:
            await update.message.reply_text(f"ошибка")
            return
        pending[user_id] = {"content": content,
                            "source": "pdf", "text_type": None}
        await ask_text_type(update, user_id, "pdf")
    except Exception as e:
        await update.message.reply_text(f"ошибка обработки пдф {str(e)}")


async def send_confirmation(query, user_id: int) -> None:
    entry = pending.get(user_id)
    if not entry:
        await query.edit_message_text("данные устарели")
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("подтвердить", callback_data="confirm_summary"),
        InlineKeyboardButton("отменить", callback_data="cancel_summary"),
    ]])
    await query.edit_message_text(f"цена: {cost_message} подтвердить конспектирование?", reply_markup=keyboard)


async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if query.data in ("type_scientific", "type_fictional", "type_entertainment"):
        entry = pending.get(user_id)
        if not entry:
            await query.edit_message_text("данные устарели")
            return
        entry["text_type"] = query.data.replace("type_", "")
        await send_confirmation(query, user_id)
        return

    if query.data == "cancel_summary":
        pending.pop(user_id, None)
        await query.edit_message_text("отменено")
        return

    if query.data == "confirm_summary":
        entry = pending.pop(user_id, None)
        if not entry:
            await query.edit_message_text("данные устарели")
            return
        await query.edit_message_text("конспектирую")
        try:
            summary = await summarize_content(
                entry["content"],
                source=entry["source"],
                text_type=entry["text_type"],
            )
            await query.message.reply_text(summary)
        except:
            return


async def ask_text_type(update: Update, user_id: int, source: str) -> None:
    if source == "youtube":
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Научный", callback_data="type_scientific"),
            InlineKeyboardButton(
                "Развлекательный", callback_data="type_entertainment"),
        ]])
        await update.message.reply_text("Какой тип видео?", reply_markup=keyboard)
    else:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("научный", callback_data="type_scientific"),
            InlineKeyboardButton(
                "художественный", callback_data="type_fictional")
        ]])
        await update.message.reply_text("какой тип текста?", reply_markup=keyboard)


schedule_litellm_updates()
app = Application.builder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(
    filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
app.add_handler(CallbackQueryHandler(handle_confirmation))
app.run_polling()
