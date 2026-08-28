import os
import re
import uuid
import asyncio
from pathlib import Path
from dataclasses import dataclass, field

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import TimedOut, NetworkError


TOKEN = os.environ["BOT_TOKEN"].strip()

API_BASE = "http://127.0.0.1:8081/bot"
FILE_API_BASE = "http://127.0.0.1:8081/file/bot"

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

READ_TIMEOUT = 3600
WRITE_TIMEOUT = 3600
CONNECT_TIMEOUT = 120
POOL_TIMEOUT = 120

MAX_RETRIES = 3


@dataclass
class FileItem:
    file_id: str
    original_name: str
    size: int


@dataclass
class UserQueue:
    files: list[FileItem] = field(default_factory=list)
    worker: asyncio.Task | None = None
    waiting_filename: bool = False
    cancelled: bool = False
    position: int = 1
    total: int = 0


queues: dict[int, UserQueue] = {}


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


def get_queue(user_id: int) -> UserQueue:
    if user_id not in queues:
        queues[user_id] = UserQueue()

    return queues[user_id]


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id

    await update.message.reply_text(
        "🤖 Telegram File Rename Bot\n\n"
        "Send or forward documents.\n"
        "Each user has an independent queue.\n\n"
        "/cancel - Cancel your current file and queue\n"
        "/stop - Stop bot"
    )


async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    queue = queues.get(user_id)

    if not queue:
        await update.message.reply_text(
            "❌ Your queue is empty."
        )
        return

    queue.cancelled = True
    queue.files.clear()
    queue.waiting_filename = False

    worker = queue.worker

    if worker and not worker.done():
        worker.cancel()

        await update.message.reply_text(
            "🛑 Cancelling your current transfer and queue..."
        )
    else:
        queues.pop(user_id, None)

        await update.message.reply_text(
            "❌ Your queue has been cancelled."
        )


async def stop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🛑 Stopping bot..."
    )

    context.application.stop_running()


async def receive_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    document = update.message.document

    queue = get_queue(user_id)

    item = FileItem(
        file_id=document.file_id,
        original_name=document.file_name or "file",
        size=document.file_size or 0,
    )

    queue.files.append(item)

    queue.total += 1

    position = (
        queue.position
        + len(queue.files)
        - 1
    )

    await update.message.reply_text(
        "📥 Added to queue\n\n"
        f"📎 {item.original_name}\n"
        f"📏 {format_size(item.size)}\n"
        f"📦 Queue position: {position}"
    )

    if queue.worker is None or queue.worker.done():
        queue.worker = asyncio.create_task(
            queue_worker(
                update,
                context,
                user_id,
            )
        )


async def queue_worker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
):
    queue = queues.get(user_id)

    if not queue:
        return

    try:
        while queue.files and not queue.cancelled:
            item = queue.files[0]

            queue.waiting_filename = True

            current_number = queue.position
            total = queue.total

            await update.message.reply_text(
                f"📦 File {current_number} / {total}\n\n"
                f"📎 {item.original_name}\n"
                f"📏 {format_size(item.size)}\n\n"
                "✏️ Enter new filename:"
            )

            # Wait until the filename handler supplies
            # a filename for this exact queue item.
            while queue.waiting_filename and not queue.cancelled:
                await asyncio.sleep(0.2)

            if queue.cancelled:
                break

            filename = getattr(
                queue,
                "next_filename",
                None,
            )

            if not filename:
                continue

            queue.next_filename = None

            temp_path = (
                DOWNLOAD_DIR
                / f"{uuid.uuid4().hex}_{filename}"
            )

            success = await process_file(
                update,
                context,
                item,
                filename,
                temp_path,
                queue,
            )

            if queue.cancelled:
                break

            if success:
                if queue.files:
                    queue.files.pop(0)

                queue.position += 1

                if queue.files:
                    continue

                await update.message.reply_text(
                    "🎉 All files completed."
                )

                queues.pop(user_id, None)
                return

            # Keep the file in the queue after failure.
            queue.waiting_filename = True

            await update.message.reply_text(
                "⚠️ File was not completed.\n"
                "You can enter the filename again to retry."
            )

    except asyncio.CancelledError:
        pass

    except Exception as error:
        print(
            f"Queue worker error for user {user_id}: "
            f"{error!r}"
        )

        try:
            await update.message.reply_text(
                "❌ Queue worker stopped unexpectedly.\n\n"
                f"{error}"
            )
        except Exception:
            pass

    finally:
        queue = queues.get(user_id)

        if queue and queue.cancelled:
            queues.pop(user_id, None)


async def download_file(
    context,
    file_id: str,
    temp_path: Path,
):
    telegram_file = await context.bot.get_file(
        file_id,
        read_timeout=READ_TIMEOUT,
        write_timeout=WRITE_TIMEOUT,
        connect_timeout=CONNECT_TIMEOUT,
        pool_timeout=POOL_TIMEOUT,
    )

    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):
        try:
            return await telegram_file.download_to_drive(
                custom_path=temp_path,
                read_timeout=READ_TIMEOUT,
                write_timeout=WRITE_TIMEOUT,
                connect_timeout=CONNECT_TIMEOUT,
                pool_timeout=POOL_TIMEOUT,
            )

        except (TimedOut, NetworkError):
            if attempt >= MAX_RETRIES:
                raise

            print(
                f"Download retry "
                f"{attempt}/{MAX_RETRIES - 1}"
            )

            try:
                temp_path.unlink(
                    missing_ok=True
                )
            except Exception:
                pass

            await asyncio.sleep(
                3 * attempt
            )


async def upload_file(
    context,
    chat_id: int,
    temp_path: Path,
    filename: str,
):
    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):
        try:
            return await context.bot.send_document(
                chat_id=chat_id,
                document=temp_path,
                filename=filename,
                caption=f"✅ {filename}",
                read_timeout=READ_TIMEOUT,
                write_timeout=WRITE_TIMEOUT,
                connect_timeout=CONNECT_TIMEOUT,
                pool_timeout=POOL_TIMEOUT,
            )

        except (TimedOut, NetworkError):
            if attempt >= MAX_RETRIES:
                raise

            print(
                f"Upload retry "
                f"{attempt}/{MAX_RETRIES - 1}"
            )

            await asyncio.sleep(
                3 * attempt
            )


async def process_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    item: FileItem,
    filename: str,
    temp_path: Path,
    queue: UserQueue,
):
    status = None

    try:
        status = await update.message.reply_text(
            "⬇️ Downloading file...\n\n"
            f"📎 {item.original_name}\n"
            f"📏 {format_size(item.size)}"
        )

        try:
            local_file = await download_file(
                context,
                item.file_id,
                temp_path,
            )

        except asyncio.CancelledError:
            raise

        except Exception as error:
            await status.edit_text(
                "❌ Download failed.\n\n"
                f"{error}"
            )
            return False

        if queue.cancelled:
            return False

        await status.edit_text(
            "📤 Uploading renamed file...\n\n"
            f"📎 {filename}\n"
            f"📏 {format_size(item.size)}"
        )

        try:
            await upload_file(
                context,
                update.effective_chat.id,
                local_file,
                filename,
            )

        except asyncio.CancelledError:
            raise

        except Exception as error:
            await status.edit_text(
                "❌ Upload failed.\n\n"
                f"{error}"
            )
            return False

        await status.edit_text(
            "✅ Complete\n\n"
            f"📎 {filename}"
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

    finally:
        try:
            temp_path.unlink(
                missing_ok=True
            )
        except Exception:
            pass


async def receive_filename(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    queue = queues.get(user_id)

    if not queue or not queue.files:
        await update.message.reply_text(
            "📁 Send or forward a document first."
        )
        return

    if not queue.waiting_filename:
        await update.message.reply_text(
            "⏳ Your file is currently being processed."
        )
        return

    filename = clean_filename(
        update.message.text.strip()
    )

    if not filename:
        await update.message.reply_text(
            "❌ Invalid filename."
        )
        return

    # Store the filename for the worker.
    queue.next_filename = filename
    queue.waiting_filename = False


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    print(
        f"Unhandled bot error: "
        f"{context.error!r}"
    )


def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .base_url(API_BASE)
        .base_file_url(FILE_API_BASE)
        .read_timeout(READ_TIMEOUT)
        .write_timeout(WRITE_TIMEOUT)
        .connect_timeout(CONNECT_TIMEOUT)
        .pool_timeout(POOL_TIMEOUT)
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

    app.add_handler(
        MessageHandler(
            filters.Document.ALL,
            receive_document,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_filename,
        )
    )

    app.add_error_handler(
        error_handler
    )

    print("----------------------------------------")
    print("🤖 Telegram File Rename Bot")
    print("----------------------------------------")
    print("📁 Documents only")
    print("📦 Per-user queues")
    print("👥 Multiple users concurrently")
    print("🔀 Unique temporary filenames")
    print("📦 Local Bot API")
    print("🔁 Download/upload retries: 3")
    print("⏱️ Transfer timeout: 1 hour")
    print("📝 Exact user filename")
    print("🛑 Force /cancel")
    print("🟢 Running")
    print("----------------------------------------")

    app.run_polling()


if __name__ == "__main__":
    main()
