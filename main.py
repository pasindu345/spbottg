import logging
import time
import requests
import io
import re
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# === CONFIG ===
BOT_TOKEN    = "8013333313:AAEgnseSjuuJRsZL_8ThUafefgFPkg1dqz4"
SEARCH_URL   = "https://sl-bjs-spotify.vercel.app/spotify/search"
# Using the specified download API
DOWNLOAD_API = "https://bj-tricks.serv00.net/Spotify-downloader-api/?url="
PAGE_SIZE    = 10
TIMEOUT_SEC  = 120  # 2 minutes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# In‑memory session store: chat_id → { tracks, query, timestamp }
sessions = {}

# === /start ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("🔍 Search a Song", switch_inline_query_current_chat="")]]
    await update.message.reply_text(
        "👋 Welcome! Send me a song name and I'll list matches. "
        "Use the button below or type in chat.",
        reply_markup=InlineKeyboardMarkup(kb)
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
        nav.append(InlineKeyboardButton("⬅ Back", callback_data=f"page|{chat_id}|{page-1}"))
    if end < len(tracks):
        nav.append(InlineKeyboardButton("➡ Next", callback_data=f"page|{chat_id}|{page+1}"))
    if nav:
        kb.append(nav)

    return InlineKeyboardMarkup(kb)

# === HANDLE TEXT MESSAGES ===
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    
    # Check if it's a Spotify URL
    if "spotify.com" in text:
        # Process the Spotify URL directly
        await process_spotify_url(update, context, text)
        return
    
    # Regular search
    tracks = search_tracks(text)
    if not tracks:
        return await update.message.reply_text("❌ No tracks found. Try another title!")

    # Save session
    sessions[chat_id] = {
        "tracks": tracks,
        "query": text,
        "ts": time.time()
    }

    kb = build_kb(chat_id, page=0)
    await update.message.reply_text(
        f"🎧 Results for: {text}",
        parse_mode="Markdown",
        reply_markup=kb
    )

# === PROCESS SPOTIFY URL ===
async def process_spotify_url(update, context, spotify_url):
    """Process a Spotify URL directly"""
    chat_id = str(update.effective_chat.id)
    
    # Show downloading message
    message = await update.message.reply_text("⏳ Downloading song from Spotify URL...", parse_mode="Markdown")
    
    try:
        # Fetch download link from API
        logger.info(f"Fetching download for: {spotify_url}")
        r = requests.get(DOWNLOAD_API + spotify_url, timeout=15)
        r.raise_for_status()
        payload = r.json()
        logger.info(f"API response: {payload}")
        
        if not payload.get("status"):
            await message.edit_text("❌ Download failed. The API returned an error.")
            return
        
        download_link = payload.get("data", {}).get("downloadLink")
        if not download_link:
            await message.edit_text(
                "❌ Download link not available. The song might be unavailable or the API is having issues.",
                parse_mode="Markdown"
            )
            return
        
        # Send audio file
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_audio")
        
        # Try to get track info from API response
        track_name = payload.get("data", {}).get("trackName", "Unknown Track")
        artist = payload.get("data", {}).get("artist", "Unknown Artist")
        
        # Send the audio file
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=download_link,
            caption=f"🎶 {track_name} — {artist}"
        )
        
        # Update the message to show success
        await message.edit_text(f"✅ Downloaded: {track_name} by {artist}")
        
    except Exception as e:
        logger.error(f"Error processing Spotify URL: {e}")
        # Provide direct link as fallback
        kb = [[InlineKeyboardButton("🔗 Try Direct Download", url=DOWNLOAD_API + spotify_url)]]
        await message.edit_text(
            f"❌ Error downloading. Please try again or use the direct link below:",
            reply_markup=InlineKeyboardMarkup(kb)
        )

# === EXTRACT TRACK ID ===
def extract_track_id(spotify_url):
    """Extract track ID from Spotify URL"""
    # Regular expression to match Spotify track IDs
    track_pattern = r'spotify\.com/track/([a-zA-Z0-9]+)'
    match = re.search(track_pattern, spotify_url)
    
    if match:
        return match.group(1)
    
    # Alternative format
    if "/track/" in spotify_url:
        parts = spotify_url.split("/track/")
        if len(parts) > 1:
            track_id = parts[1]
            # Remove any query parameters
            if "?" in track_id:
                track_id = track_id.split("?")[0]
            return track_id
    
    return None

# === CALLBACK HANDLER ===
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("|")
    cmd, chat_id = parts[0], parts[1]

    session = sessions.get(chat_id)
    if not session:
        return await q.edit_message_text("⏰ Session expired. Please search again.")

    # Timeout check
    if time.time() - session["ts"] > TIMEOUT_SEC:
        sessions.pop(chat_id, None)
        return await q.edit_message_text("⏰ Session timed out. Please search again.")

    # Refresh timestamp
    session["ts"] = time.time()

    # Pagination
    if cmd == "page":
        page = int(parts[2])
        kb = build_kb(chat_id, page)
        return await q.edit_message_text(
            f"🎧 Results for: {session['query']}",
            parse_mode="Markdown",
            reply_markup=kb
        )

    # Play / download a track
    if cmd == "play":
        idx = int(parts[2])
        tracks = session["tracks"]
        if idx < 0 or idx >= len(tracks):
            return await q.edit_message_text("❌ Invalid selection.")

        track = tracks[idx]
        spotify_url = track.get("spotifyUrl")
        if not spotify_url:
            return await q.edit_message_text("❌ No Spotify URL available.")

        # Show downloading message
        await q.edit_message_text(f"⏳ Downloading: {track['trackName']} by {track['artist']}...")
        
        try:
            # Fetch download link from API
            logger.info(f"Fetching download for: {spotify_url}")
            r = requests.get(DOWNLOAD_API + spotify_url, timeout=15)
            r.raise_for_status()
            payload = r.json()
            logger.info(f"API response: {payload}")
            
            if not payload.get("status"):
                await q.edit_message_text("❌ Download failed. The API returned an error.")
                return
            
            download_link = payload.get("data", {}).get("downloadLink")
            if not download_link:
                await q.edit_message_text(
                    "❌ Download link not available. The song might be unavailable or the API is having issues."
                )
                return
            
            # Send audio file
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_audio")
            
            # Send the audio file
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=download_link,
                caption=f"🎶 {track['trackName']} — {track['artist']}"
            )
            
            # Update the message to show success
            await q.edit_message_text(f"✅ Downloaded: {track['trackName']} by {track['artist']}")
            
        except Exception as e:
            logger.error(f"Error downloading: {e}")
            # Provide direct link as fallback
            kb = [[InlineKeyboardButton("🔗 Try Direct Download", url=DOWNLOAD_API + spotify_url)]]
            await q.edit_message_text(
                f"❌ Error downloading. Please try again or use the direct link below:",
                reply_markup=InlineKeyboardMarkup(kb)
            )

# === MAIN ===
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.run_polling()

if _name_ == "_main_":
    main()
