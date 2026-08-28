import os
import re
import uuid
import time
from pathlib import Path

import aiohttp

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.environ["BOT_TOKEN"]

API_BASE = "http://127.0.0.1:8081/bot"
FILE_API_BASE = "http://127.0.0.1:8081/file/bot"

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

PROGRESS_INTERVAL = 2


def clean_filename(name):
    name = re.sub(
        r'[<>:"/\\|?*\x00-\x1F]',
        "_",
        name,
    )

    return name.strip().strip(".")


def progress_bar(percent, length=20):
    filled = int(length * percent / 100)
    return "█" * filled + "░" * (length - filled)


def format_size(size):
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"

    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"

    return f"{size / 1024 / 1024 / 1024:.2f} GB"


def format_speed(speed):
    if speed < 1024 * 1024:
        return f"{speed / 1024:.1f} KB/s"

    return f"{speed / 1024 / 1024:.1f} MB/s"


def format_eta(seconds):
    if seconds <= 0:
        return "0s"

    seconds = int(seconds)

    if seconds < 60:
        return f"{seconds}s"

    minutes, seconds = divmod(seconds, 60)

    if minutes < 60:
        return f"{minutes}m {seconds}s"

    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


async def update_progress(
    message,
    action,
    current,
    total,
    start_time,
    force=False,
):
    if total <= 0:
        return

    now = time.monotonic()

    last_update = getattr(
        message,
        "_progress_time",
        0,
    )

    if not force and now - last_update < PROGRESS_INTERVAL:
        return

    message._progress_time = now

    percent = current * 100 / total
    elapsed = max(now - start_time, 0.001)

    speed = current / elapsed
    remaining = max(total - current, 0)

    eta = remaining / speed if speed > 0 else 0

    text = (
        f"{action}\n"
        f"{progress_bar(percent)} {percent:.1f}%\n"
        f"{format_size(current)} / {format_size(total)}\n"
        f"⚡ {format_speed(speed)}\n"
        f"⏱️ ETA {format_eta(eta)}"
    )

    try:
        await message.edit_text(text)
    except Exception:
        pass


async def download_file(
    file_path,
    destination,
    progress_message,
    total_size,
):
    url = f"{FILE_API_BASE}/{file_path}"

    start_time = time.monotonic()
    current = 0

    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(
                total=None
            ),
        ) as response:

            response.raise_for_status()

            with open(destination, "wb") as file:
                async for chunk in response.content.iter_chunked(
                    1024 * 1024
                ):
                    file.write(chunk)
                    current += len(chunk)

                    await update_progress(
                        progress_message,
                        "⬇️ Downloading",
                        current,
                        total_size,
                        start_time,
                    )

    await update_progress(
        progress_message,
        "⬇️ Downloading",
        current,
        total_size,
        start_time,
        force=True,
    )


async def upload_file(
    chat_id,
    path,
    filename,
    caption,
    progress_message,
):
    url = f"{API_BASE}/{TOKEN}/sendDocument"

    total_size = path.stat().st_size
    start_time = time.monotonic()

    class ProgressFile:
        def __init__(self, file_path):
            self.file = open(file_path, "rb")
            self.size = total_size
            self.sent = 0

        async def read(self, size=-1):
            data = self.file.read(size)

            if data:
                self.sent += len(data)

                await update_progress(
                    progress_message,
                    "📤 Uploading",
                    self.sent,
                    self.size,
                    start_time,
                )

            return data

        def close(self):
            self.file.close()

    progress_file = ProgressFile(path)

    try:
        form = aiohttp.FormData()

        form.add_field(
            "chat_id",
            str(chat_id),
        )

        form.add_field(
            "caption",
            caption,
        )

        form.add_field(
            "parse_mode",
            "Markdown",
        )

        form.add_field(
            "document",
            progress_file.file,
            filename=filename,
            content_type="application/octet-stream",
        )

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                data=form,
                timeout=aiohttp.ClientTimeout(
                    total=None
                ),
            ) as response:

                result = await response.json()

                if not result.get("ok"):
                    raise RuntimeError(
                        result.get(
                            "description",
                            "Upload failed",
                        )
                    )

        await update_progress(
            progress_message,
            "📤 Uploading",
            total_size,
            total_size,
            start_time,
            force=True,
        )

    finally:
        progress_file.close()


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    context.user_data.clear()

    await update.message.reply_text(
        "🤖 Maxico Rename Bot\n\n"
        "Send or forward one or more documents.\n"
        "I will process them one by one.\n\n"
        "/cancel - Cancel queue\n"
        "/stop - Stop bot"
    )


async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    context.user_data.clear()

    await update.message.reply_text(
        "❌ Queue cancelled."
    )


async def stop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🛑 Stopping..."
    )

    context.application.stop_running()


async def receive_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    document = update.message.document

    queue = context.user_data.setdefault(
        "queue",
        [],
    )

    queue.append(
        {
            "file_id": document.file_id,
            "original_name": (
                document.file_name or "file"
            ),
            "size": document.file_size or 0,
        }
    )

    await update.message.reply_text(
        f"📥 Added: "
        f"`{document.file_name or 'file'}`\n"
        f"📦 Queue: {len(queue)} file(s)",
        parse_mode="Markdown",
    )

    if not context.user_data.get("processing"):
        await process_next(
            update,
            context,
        )


async def process_next(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    queue = context.user_data.get(
        "queue",
        [],
    )

    if not queue:
        context.user_data.pop(
            "processing",
            None,
        )

        await update.message.reply_text(
            "✅ All files completed."
        )

        return

    context.user_data["processing"] = True

    item = queue[0]

    context.user_data["current_file"] = item

    await update.message.reply_text(
        f"📁 `{item['original_name']}`\n\n"
        "✏️ Enter new filename:",
        parse_mode="Markdown",
    )


async def rename_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    queue = context.user_data.get(
        "queue",
        [],
    )

    if not queue:
        await update.message.reply_text(
            "📁 Send or forward a document first."
        )
        return

    item = queue[0]

    new_name = clean_filename(
        update.message.text.strip()
    )

    if not new_name:
        await update.message.reply_text(
            "❌ Invalid filename."
        )
        return

    original_name = item["original_name"]

    extension = Path(original_name).suffix

    if extension and "." not in new_name:
        new_name += extension

    temp_name = (
        f"{uuid.uuid4().hex}_{new_name}"
    )

    path = DOWNLOAD_DIR / temp_name

    progress_message = None

    try:
        progress_message = await update.message.reply_text(
            "⬇️ Preparing download..."
        )

        telegram_file = await context.bot.get_file(
            item["file_id"]
        )

        await download_file(
            telegram_file.file_path,
            path,
            progress_message,
            item["size"],
        )

        await progress_message.edit_text(
            "📤 Preparing upload..."
        )

        await upload_file(
            update.effective_chat.id,
            path,
            new_name,
            f"✅ `{new_name}`",
            progress_message,
        )

        await progress_message.edit_text(
            f"✅ Complete\n\n"
            f"📎 `{new_name}`",
            parse_mode="Markdown",
        )

        path.unlink(missing_ok=True)

        queue.pop(0)

        context.user_data.pop(
            "current_file",
            None,
        )

        if queue:
            await process_next(
                update,
                context,
            )
        else:
            context.user_data.clear()

            await update.message.reply_text(
                "✅ All files completed."
            )

    except Exception as error:
        path.unlink(missing_ok=True)

        await update.message.reply_text(
            f"❌ Error:\n`{error}`",
            parse_mode="Markdown",
        )

        context.user_data.pop(
            "processing",
            None,
        )


def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .base_url(API_BASE)
        .base_file_url(FILE_API_BASE)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("cancel", cancel)
    )

    app.add_handler(
        CommandHandler("stop", stop)
    )

    app.add_handler(
        MessageHandler(
            filters.Document.ALL,
            receive_document,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            rename_file,
        )
    )

    print("----------------------------------------")
    print("🤖 Maxico Rename Bot")
    print("----------------------------------------")
    print("📁 Documents only")
    print("📦 Multi-file queue")
    print("👥 Per-user queue")
    print("📊 Download/upload progress")
    print("📦 Local Bot API")
    print("🟢 Running")
    print("----------------------------------------")

    app.run_polling()


if __name__ == "__main__":
    main()