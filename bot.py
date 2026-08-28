import os
import re
import uuid
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


TOKEN = os.environ["BOT_TOKEN"].strip()

API_BASE = "http://127.0.0.1:8081/bot"
FILE_API_BASE = "http://127.0.0.1:8081/file/bot"

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)


def clean_filename(name: str) -> str:
    name = re.sub(
        r'[<>:"/\\|?*\x00-\x1F]',
        "_",
        name,
    )

    return name.strip().strip(".")


def format_size(size: int | None) -> str:
    if not size:
        return "Unknown size"

    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"

    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"

    return f"{size / 1024 / 1024 / 1024:.2f} GB"


def progress_bar(percent: int, length: int = 20) -> str:
    percent = max(0, min(100, percent))
    filled = int(length * percent / 100)

    return (
        "█" * filled
        + "░" * (length - filled)
    )


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    context.user_data.clear()

    await update.message.reply_text(
        "🤖 Telegram File Rename Bot\n\n"
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
        f"📥 Added to queue\n\n"
        f"📎 `{document.file_name or 'file'}`\n"
        f"📏 {format_size(document.file_size)}\n"
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
        context.user_data.clear()

        await update.message.reply_text(
            "✅ All files completed."
        )

        return

    context.user_data["processing"] = True

    item = queue[0]

    context.user_data["current_file"] = item

    await update.message.reply_text(
        f"📦 File 1 / {len(queue)}\n\n"
        f"📎 `{item['original_name']}`\n"
        f"📏 {format_size(item['size'])}\n\n"
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

    # Unique temporary filename.
    temp_name = (
        f"{uuid.uuid4().hex}_{new_name}"
    )

    temp_path = DOWNLOAD_DIR / temp_name

    try:
        # ---------------------------------------------
        # DOWNLOAD
        # ---------------------------------------------

        progress = await update.message.reply_text(
            "⬇️ Downloading...\n\n"
            f"{progress_bar(0)} 0%"
        )

        telegram_file = await context.bot.get_file(
            item["file_id"]
        )

        local_source = (
            await telegram_file.download_to_drive(
                custom_path=temp_path,
                read_timeout=600,
                write_timeout=600,
                connect_timeout=60,
                pool_timeout=60,
            )
        )

        await progress.edit_text(
            "⬇️ Downloading...\n\n"
            f"{progress_bar(100)} 100%\n\n"
            "✅ Download complete"
        )

        # ---------------------------------------------
        # PREPARE
        # ---------------------------------------------

        await progress.edit_text(
            "🔄 Preparing renamed file...\n\n"
            f"📎 `{new_name}`",
            parse_mode="Markdown",
        )

        # ---------------------------------------------
        # UPLOAD
        # ---------------------------------------------

        await progress.edit_text(
            "📤 Uploading...\n\n"
            "⏳ Sending to Telegram..."
        )

        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=local_source,
            filename=new_name,
            caption=f"✅ `{new_name}`",
            parse_mode="Markdown",
            read_timeout=600,
            write_timeout=600,
            connect_timeout=60,
            pool_timeout=60,
        )

        # ---------------------------------------------
        # COMPLETE
        # ---------------------------------------------

        await progress.edit_text(
            "✅ Complete\n\n"
            f"📎 `{new_name}`",
            parse_mode="Markdown",
        )

        # Delete temporary file.
        try:
            Path(local_source).unlink(
                missing_ok=True
            )
        except Exception:
            pass

        temp_path.unlink(
            missing_ok=True
        )

        # Remove completed file.
        queue.pop(0)

        context.user_data.pop(
            "current_file",
            None,
        )

        # ---------------------------------------------
        # NEXT FILE
        # ---------------------------------------------

        if queue:
            await process_next(
                update,
                context,
            )
        else:
            context.user_data.clear()

            await update.message.reply_text(
                "🎉 All files completed."
            )

    except Exception as error:
        temp_path.unlink(
            missing_ok=True
        )

        await update.message.reply_text(
            "❌ Error\n\n"
            f"`{error}`",
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
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "cancel",
            cancel,
        )
    )

    app.add_handler(
        CommandHandler(
            "stop",
            stop,
        )
    )

    # Documents only.
    app.add_handler(
        MessageHandler(
            filters.Document.ALL,
            receive_document,
        )
    )

    # Filename input.
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            rename_file,
        )
    )

    print("----------------------------------------")
    print("🤖 Telegram File Rename Bot")
    print("----------------------------------------")
    print("📁 Documents only")
    print("📦 Multi-file queue")
    print("👥 Per-user queue")
    print("🔀 Unique temporary filenames")
    print("📊 Transfer status")
    print("📦 Local Bot API")
    print("🟢 Running")
    print("----------------------------------------")

    app.run_polling()


if __name__ == "__main__":
    main()