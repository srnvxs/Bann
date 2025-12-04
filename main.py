"""
Telegram PDF Banner Replacer Bot (single-file)

Features:
- /setbanner  -> bot asks you to upload an image (PNG/JPG). Saves banner per-chat.
- /removebanner -> removes saved banner for the chat
- /status -> shows whether banner is set
- When a PDF file is sent, bot replaces the ENTIRE first page with a new page consisting only of the banner image and returns the edited PDF.
- Also /process <reply-to-pdf> to force processing

Requirements (also include in requirements.txt):
- pyrogram
- tgcrypto
- pikepdf
- reportlab
- pillow

Environment variables required:
- BOT_TOKEN (Bot token from BotFather)
- API_ID, API_HASH (from my.telegram.org)

Run locally: python telegram_pdf_banner_bot.py

Notes about Render deployment:
- Use the same env vars in Render dashboard.
- Persist storage: Render's ephemeral filesystem may be reset; for permanent banner storage consider S3 or attach a persistent disk. This script stores banners to ./banners/{chat_id}/banner.png locally.

"""

import os
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message
from PIL import Image
import pikepdf
from reportlab.pdfgen import canvas
from reportlab.lib.units import pt
from io import BytesIO

# Configure paths
DATA_DIR = os.environ.get("DATA_DIR", ".")
BANNER_DIR = os.path.join(DATA_DIR, "banners")
TMP_DIR = os.path.join(DATA_DIR, "tmp")
os.makedirs(BANNER_DIR, exist_ok=True)
os.makedirs(TMP_DIR, exist_ok=True)

# Simple in-memory state: awaiting banner upload after /setbanner
awaiting_banner = {}

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", 0)) if os.environ.get("API_ID") else None
API_HASH = os.environ.get("API_HASH")

if not BOT_TOKEN or not API_ID or not API_HASH:
    print("Missing BOT_TOKEN or API_ID/API_HASH environment variables. Exiting.")
    raise SystemExit(1)

app = Client("pdf_banner_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)


def banner_path_for_chat(chat_id: int) -> str:
    d = os.path.join(BANNER_DIR, str(chat_id))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "banner.png")


@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    await message.reply_text("Hello! Send /setbanner to upload a banner, then send a PDF to replace its first page with that banner.")


@app.on_message(filters.command("setbanner"))
async def setbanner_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    awaiting_banner[chat_id] = True
    await message.reply_text("Okay — send the banner image now as a photo or image file. I will save it for this chat.")


@app.on_message(filters.command("removebanner"))
async def removebanner_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    path = banner_path_for_chat(chat_id)
    if os.path.exists(path):
        os.remove(path)
        await message.reply_text("Banner removed for this chat.")
    else:
        await message.reply_text("No banner was set for this chat.")


@app.on_message(filters.command("status"))
async def status_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    path = banner_path_for_chat(chat_id)
    if os.path.exists(path):
        await message.reply_text("Banner is set for this chat.")
    else:
        await message.reply_text("No banner set. Use /setbanner to upload one.")


@app.on_message(filters.photo | filters.document)
async def receive_image(client: Client, message: Message):
    chat_id = message.chat.id
    if not awaiting_banner.get(chat_id):
        return  # ignore images unless we asked for them

    # Accept photo OR document that is an image
    file = None
    try:
        if message.photo:
            file = await client.download_media(message.photo.file_id, file_name=os.path.join(TMP_DIR, f"{message.message_id}_banner"))
        elif message.document and message.document.mime_type.startswith("image"):
            file = await client.download_media(message.document.file_id, file_name=os.path.join(TMP_DIR, f"{message.message_id}_banner"))
        else:
            await message.reply_text("Please send a PNG or JPG image.")
            return

        # Normalize and save as PNG
        img = Image.open(file)
        dest = banner_path_for_chat(chat_id)
        img.convert("RGBA").save(dest, format="PNG")
        awaiting_banner.pop(chat_id, None)
        await message.reply_text("Banner saved for this chat.")
    except Exception as e:
        awaiting_banner.pop(chat_id, None)
        await message.reply_text(f"Failed to save banner: {e}")


async def create_banner_pdf_from_image(img_path: str, out_pdf_path: str, width_pt: float, height_pt: float):
    """Create a single-page PDF sized width_pt x height_pt (points) with the image stretched to fit."""
    # reportlab works in points. We'll create a canvas and draw the image scaled to the page.
    c = canvas.Canvas(out_pdf_path, pagesize=(width_pt, height_pt))
    # Use PIL to open image and preserve orientation
    img = Image.open(img_path)
    iw, ih = img.size
    # Fit image to page while preserving aspect ratio, center it
    ratio = min(width_pt / iw, height_pt / ih)
    new_w = iw * ratio
    new_h = ih * ratio
    x = (width_pt - new_w) / 2
    y = (height_pt - new_h) / 2
    # Save a temporary resized version in memory
    buf = BytesIO()
    img = img.convert("RGBA")
    img.resize((int(new_w), int(new_h)), Image.LANCZOS).save(buf, format="PNG")
    buf.seek(0)
    c.drawImage(buf, x, y, width=new_w, height=new_h, mask='auto')
    c.showPage()
    c.save()


async def replace_first_page_with_banner(original_pdf_path: str, banner_img_path: str, output_pdf_path: str):
    # Open original to read page size
    with pikepdf.Pdf.open(original_pdf_path) as original:
        if len(original.pages) == 0:
            raise ValueError("PDF has no pages")
        # Get media box of first page
        mbox = original.pages[0].MediaBox
        # MediaBox gives [llx, lly, urx, ury]
        llx, lly, urx, ury = [float(x) for x in mbox]
        width_pt = abs(urx - llx)
        height_pt = abs(ury - lly)

    banner_pdf_tmp = os.path.join(TMP_DIR, f"banner_{os.path.basename(banner_img_path)}.pdf")
    await create_banner_pdf_from_image(banner_img_path, banner_pdf_tmp, width_pt, height_pt)

    # Now merge: create new PDF starting with banner page, then append original pages from index 1 onward
    with pikepdf.Pdf.open(banner_pdf_tmp) as banner_pdf, pikepdf.Pdf.open(original_pdf_path) as original:
        out = pikepdf.Pdf.new()
        # Add banner page(s)
        for p in banner_pdf.pages:
            out.pages.append(p)
        # Append remaining pages from original (skip first)
        for i in range(1, len(original.pages)):
            out.pages.append(original.pages[i])
        out.save(output_pdf_path)


@app.on_message(filters.command("process") & filters.reply)
async def process_cmd(client: Client, message: Message):
    # User replied to a PDF with /process
    if not message.reply_to_message:
        await message.reply_text("Reply to a PDF message with /process to replace its first page.")
        return
    await handle_pdf_message(client, message.reply_to_message)


@app.on_message(filters.document & filters.mime_type("application/pdf"))
async def handle_pdf_message(client: Client, message: Message):
    await handle_pdf_message(client, message)


async def handle_pdf_message(client: Client, message: Message):
    chat_id = message.chat.id
    path = banner_path_for_chat(chat_id)
    if not os.path.exists(path):
        await message.reply_text("No banner set for this chat. Use /setbanner to upload a banner first.")
        return

    # download PDF
    try:
        orig_file = await client.download_media(message.document.file_id, file_name=os.path.join(TMP_DIR, f"{message.message_id}_orig.pdf"))
        out_file = os.path.join(TMP_DIR, f"{message.message_id}_out.pdf")
        await replace_first_page_with_banner(orig_file, path, out_file)
        await client.send_document(chat_id, out_file, caption="Here is your edited PDF (first page replaced with banner).")
    except Exception as e:
        await message.reply_text(f"Failed to process PDF: {e}")


if __name__ == "__main__":
    print("Starting PDF Banner Replacer Bot...")
    app.run()
