import logging
import time
import requests
import re
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    InlineQueryResultAudio,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    filters,
    ContextTypes,
)

# === CONFIG ===
BOT_TOKEN = "8013333313:AAEgnseSjuuJRsZL_8ThUafefgFPkg1dqz4"
SEARCH_URL = "https://sl-bjs-spotify.vercel.app/spotify/search"
DOWNLOAD_API = "https://bj-tricks.serv00.net/Spotify-downloader-api/?url="
PAGE_SIZE = 10
TIMEOUT_SEC = 120  # 2 minutes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory session store: chat_id → { tracks, query, timestamp }
sessions = {}

# Default response message
DEFAULT_RESPONSE_MESSAGE = "Type the song name to download"
# Store for custom response messages
custom_response_messages = {}

# === WELCOME MESSAGE ===
WELCOME_MESSAGE = """
🎵 *Welcome to Spotify Downloader Bot!* 🎵

I can help you download songs from Spotify. Here's how to use me:

• Send me a song name to search for tracks
• Send me a Spotify track link to download directly
• Use inline mode by typing @spotify_song_xbot in any chat

Enjoy your music! 🎧
"""

# === /start ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("🔍 Search a Song", switch_inline_query_current_chat="")]]
    
    # Check if this is a deep link with a track ID
    if context.args and context.args[0].startswith('download_'):
        track_id = context.args[0].replace('download_', '')
        if track_id:
            spotify_url = f"https://open.spotify.com/track/{track_id}"
            await process_spotify_url(update, context, spotify_url)
            return
    
    # Get custom response message if available
    chat_id = str(update.effective_chat.id)
    response_message = custom_response_messages.get(chat_id, DEFAULT_RESPONSE_MESSAGE)
    
    await update.message.reply_text(
        WELCOME_MESSAGE + f"\n\n{response_message}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# === /set command - Set custom response message ===
async def set_response_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Please provide a message to set as the response.\n"
            "Example: `/set Type your favorite song name`",
            parse_mode="Markdown"
        )
        return
    
    message = ' '.join(context.args)
    chat_id = str(update.effective_chat.id)
    custom_response_messages[chat_id] = message
    
    await update.message.reply_text(
        f"✅ Response message set to:\n\n\"{message}\"",
        parse_mode="Markdown"
    )

# === SEARCH ===
def search_tracks(q: str):
    try:
        r = requests.get(SEARCH_URL, params={"q": q}, timeout=10)
        r.raise_for_status()
        return r.json().get("tracks", [])
    except Exception as e:
        logger.error("Search error: %s", e)
        return []

# === BUILD KEYBOARD ===
def build_kb(chat_id: str, page: int):
    data = sessions.get(chat_id)
    tracks = data["tracks"]
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE

    kb = []
    for idx, track in enumerate(tracks[start:end], start):
        label = f"{track['trackName']} — {track['artist']}"
        kb.append([InlineKeyboardButton(label, callback_data=f"play|{chat_id}|{idx}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Back", callback_data=f"page|{chat_id}|{page-1}"))
    if end < len(tracks):
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"page|{chat_id}|{page+1}"))
    if nav:
        kb.append(nav)

    return InlineKeyboardMarkup(kb)

# === HANDLE INLINE QUERY ===
async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    if not query:
        return
    
    tracks = search_tracks(query)
    if not tracks:
        await update.inline_query.answer([], cache_time=1)
        return

    results = []
    for idx, track in enumerate(tracks[:10]):  # Limit to 10 results
        audio_url = track.get("audio")
        if not audio_url:
            continue

        results.append(
            InlineQueryResultAudio(
                id=f"track-{idx}",
                audio_url=audio_url,
                title=track.get("trackName", "Unknown Title"),
                performer=track.get("artist", "Unknown Artist"),
            )
        )
    
    await update.inline_query.answer(results, cache_time=1)

# === HANDLE TEXT MESSAGES ===
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    
    if "spotify.com" in text:
        await process_spotify_url(update, context, text)
        return
    
    tracks = search_tracks(text)
    if not tracks:
        response_message = custom_response_messages.get(chat_id, DEFAULT_RESPONSE_MESSAGE)
        return await update.message.reply_text(f"❌ No tracks found. {response_message}")

    sessions[chat_id] = {
        "tracks": tracks,
        "query": text,
        "ts": time.time()
    }

    kb = build_kb(chat_id, page=0)
    await update.message.reply_text(
        f"🎧 Results for: *{text}*",
        parse_mode="Markdown",
        reply_markup=kb
    )

# === PROCESS SPOTIFY URL ===
async def process_spotify_url(update, context, spotify_url):
    chat_id = str(update.effective_chat.id)
    message = await update.message.reply_text("⏳ Downloading song from Spotify URL...", parse_mode="Markdown")
    
    try:
        r = requests.get(DOWNLOAD_API + spotify_url, timeout=15)
        r.raise_for_status()
        payload = r.json()
        
        if not payload.get("status"):
            await message.edit_text("❌ Download failed. The API returned an error.")
            return
        
        download_link = payload.get("data", {}).get("downloadLink")
        if not download_link:
            await message.edit_text("❌ Download link not available.")
            return
        
        track_name = payload.get("data", {}).get("trackName", "Unknown Track")
        artist = payload.get("data", {}).get("artist", "Unknown Artist")
        
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=download_link,
            caption=f"🎶 {track_name} — {artist}"
        )
        await message.edit_text(f"✅ Downloaded: {track_name} by {artist}")
        
    except Exception as e:
        logger.error(f"Error processing Spotify URL: {e}")
        kb = [[InlineKeyboardButton("🔗 Try Direct Download", url=DOWNLOAD_API + spotify_url)]]
        await message.edit_text(
            "❌ Error downloading. Please try again or use the direct link below:",
            reply_markup=InlineKeyboardMarkup(kb)
        )

# === CALLBACK HANDLER ===
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("|")
    cmd, chat_id = parts[0], parts[1]

    session = sessions.get(chat_id)
    if not session:
        return await q.edit_message_text("⏰ Session expired. Please search again.")

    if time.time() - session["ts"] > TIMEOUT_SEC:
        sessions.pop(chat_id, None)
        return await q.edit_message_text("⏰ Session timed out. Please search again.")

    session["ts"] = time.time()

    if cmd == "page":
        page = int(parts[2])
        kb = build_kb(chat_id, page)
        return await q.edit_message_text(
            f"🎧 Results for: *{session['query']}*",
            parse_mode="Markdown",
            reply_markup=kb
        )

    if cmd == "play":
        idx = int(parts[2])
        tracks = session["tracks"]
        if idx < 0 or idx >= len(tracks):
            return await q.edit_message_text("❌ Invalid selection.")

        track = tracks[idx]
        spotify_url = track.get("spotifyUrl")
        if not spotify_url:
            return await q.edit_message_text("❌ No Spotify URL available.")

        await q.edit_message_text(f"⏳ Downloading: {track['trackName']} by {track['artist']}...")
        await process_spotify_url(update, context, spotify_url)

# === MAIN ===
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set", set_response_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(InlineQueryHandler(on_inline_query))
    app.run_polling()

if __name__ == "__main__":
    main()
