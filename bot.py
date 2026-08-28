import os
import re
import uuid
import asyncio
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

READ_TIMEOUT = 3600
WRITE_TIMEOUT = 3600
CONNECT_TIMEOUT = 120
POOL_TIMEOUT = 120


def clean_filename(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)
    return name.strip().strip(".")


def escape_md(text):
    return re.sub(
        r"([_*\[\]()~`>#+\-=|{}.!])",
        r"\\\1",
        str(text),
    )


def format_size(size):
    if not size:
        return "Unknown size"

    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"

    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"

    return f"{size / 1024 / 1024 / 1024:.2f} GB"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    await update.message.reply_text(
        "🤖 Telegram File Rename Bot\n\n"
        "Send or forward documents.\n"
        "Files are processed one by one.\n\n"
        "/cancel - Force cancel current file + queue\n"
        "/stop - Stop bot"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = context.user_data.get("processing_task")

    context.user_data["cancelled"] = True
    context.user_data["queue"] = []
    context.user_data["waiting_name"] = False

    if task and not task.done():
        task.cancel()
        await update.message.reply_text(
            "🛑 Cancelling current transfer..."
        )
    else:
        context.user_data.clear()
        await update.message.reply_text(
            "❌ Queue cancelled."
        )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛑 Stopping bot..."
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

    queue.append({
        "file_id": document.file_id,
        "original_name": document.file_name or "file",
        "size": document.file_size or 0,
    })

    if "total" not in context.user_data:
        context.user_data["total"] = len(queue)

    await update.message.reply_text(
        f"📥 Added to queue\n\n"
        f"📎 `{escape_md(document.file_name or 'file')}`\n"
        f"📏 {format_size(document.file_size)}\n"
        f"📦 Queue: {len(queue)} file(s)",
        parse_mode="MarkdownV2",
    )

    if not context.user_data.get("processing_task"):
        await ask_filename(update, context)


async def ask_filename(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    queue = context.user_data.get("queue", [])

    if not queue:
        context.user_data.clear()

        await update.message.reply_text(
            "🎉 All files completed."
        )
        return

    context.user_data["waiting_name"] = True

    position = context.user_data.get("position", 1)
    total = context.user_data.get("total", len(queue))

    item = queue[0]

    await update.message.reply_text(
        f"📦 File {position} / {total}\n\n"
        f"📎 `{escape_md(item['original_name'])}`\n"
        f"📏 {format_size(item['size'])}\n\n"
        "✏️ Enter new filename:",
        parse_mode="MarkdownV2",
    )


async def process_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    item,
    new_name,
    temp_path,
):
    status = None

    try:
        status = await update.message.reply_text(
            "⬇️ Downloading file...\n\n"
            f"📎 `{escape_md(item['original_name'])}`\n"
            f"📏 {format_size(item['size'])}",
            parse_mode="MarkdownV2",
        )

        telegram_file = await context.bot.get_file(
            item["file_id"]
        )

        local_source = await telegram_file.download_to_drive(
            custom_path=temp_path,
            read_timeout=READ_TIMEOUT,
            write_timeout=WRITE_TIMEOUT,
            connect_timeout=CONNECT_TIMEOUT,
            pool_timeout=POOL_TIMEOUT,
        )

        if context.user_data.get("cancelled"):
            return False

        await status.edit_text(
            "📤 Uploading renamed file...\n\n"
            f"📎 `{escape_md(new_name)}`\n"
            f"📏 {format_size(item['size'])}",
            parse_mode="MarkdownV2",
        )

        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=local_source,
            filename=new_name,
            caption=f"✅ `{escape_md(new_name)}`",
            parse_mode="MarkdownV2",
            read_timeout=READ_TIMEOUT,
            write_timeout=WRITE_TIMEOUT,
            connect_timeout=CONNECT_TIMEOUT,
            pool_timeout=POOL_TIMEOUT,
        )

        await status.edit_text(
            "✅ Complete\n\n"
            f"📎 `{escape_md(new_name)}`",
            parse_mode="MarkdownV2",
        )

        return True

    except asyncio.CancelledError:
        if status:
            try:
                await status.edit_text(
                    "🛑 Transfer cancelled."
                )
            except Exception:
                pass

        raise

    except Exception as error:
        if context.user_data.get("cancelled"):
            return False

        await update.message.reply_text(
            "❌ Error processing file.\n\n"
            f"`{escape_md(error)}`",
            parse_mode="MarkdownV2",
        )

        return False

    finally:
        try:
            temp_path.unlink(
                missing_ok=True
            )
        except Exception:
            pass


async def rename_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    queue = context.user_data.get("queue", [])

    if not queue or not context.user_data.get(
        "waiting_name"
    ):
        await update.message.reply_text(
            "📁 Send or forward a document first."
        )
        return

    new_name = clean_filename(
        update.message.text.strip()
    )

    if not new_name:
        await update.message.reply_text(
            "❌ Invalid filename."
        )
        return

    item = queue[0]

    temp_path = (
        DOWNLOAD_DIR
        / f"{uuid.uuid4().hex}_{new_name}"
    )

    context.user_data["waiting_name"] = False
    context.user_data["cancelled"] = False

    task = asyncio.create_task(
        process_file(
            update,
            context,
            item,
            new_name,
            temp_path,
        )
    )

    context.user_data["processing_task"] = task

    try:
        success = await task

    except asyncio.CancelledError:
        success = False

    finally:
        context.user_data.pop(
            "processing_task",
            None,
        )

    if context.user_data.get("cancelled"):
        context.user_data.clear()

        await update.message.reply_text(
            "❌ Current file and remaining queue cancelled."
        )

        return

    if not success:
        context.user_data["waiting_name"] = True
        return

    queue = context.user_data.get("queue", [])

    if queue:
        queue.pop(0)

    context.user_data["position"] = (
        context.user_data.get("position", 1) + 1
    )

    if queue:
        await ask_filename(update, context)

    else:
        context.user_data.clear()

        await update.message.reply_text(
            "🎉 All files completed."
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
    print("🤖 Telegram File Rename Bot")
    print("----------------------------------------")
    print("📁 Documents only")
    print("📦 Multi-file queue")
    print("👥 Per-user queue")
    print("🔀 Unique temporary filenames")
    print("📦 Local Bot API")
    print("⏱️ 1-hour transfer timeout")
    print("📝 Exact user filename")
    print("🛑 Force /cancel")
    print("🟢 Running")
    print("----------------------------------------")

    app.run_polling()


if __name__ == "__main__":
    main()
