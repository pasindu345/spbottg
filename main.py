import logging
import time
import requests
import io
import re
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    ChatMemberUpdated,
    ChatMember,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ChatMemberHandler,
)
from typing import Tuple, Optional, cast


# === CONFIG ===
# Replace with your actual Bot Token
BOT_TOKEN = "8013333313:AAEgnseSjuuJRsZL_8ThUafefgFPkg1dqz4"
# Replace with your actual search API if different
SEARCH_URL = "https://sl-bjs-spotify.vercel.app/spotify/search"
# Replace with your actual download API
DOWNLOAD_API = "https://bj-tricks.serv00.net/Spotify-downloader-api/?url="
PAGE_SIZE = 10
TIMEOUT_SEC = 120  # 2 minutes
UPDATES_CHANNEL_URL = "https://t.me/your_updates_channel"  # Replace with your Updates channel/group link
HELP_TEXT = """
How to Use Me:

1.  Search: Send me a song name. I'll show you matching tracks.
2.  Select: Tap the song you want from the list.
3.  Download: I'll send you the MP3 file.
4.  Spotify Link: Paste a Spotify track link directly.
5.  Groups: Add me to your group! I can respond to song requests there too.

Commands:
/start - Start the bot
/help - Show this help message
/stats - Show bot usage statistics (Admin only)

Enjoy the music! 🎶
"""
# Replace with the actual image URL you want to use for /start
START_IMAGE_URL = "https://ibb.co/gMqkM9k"


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# In-memory stores
# sessions: chat_id -> { tracks, query, timestamp, initiator_user_id }
sessions = {}
# stats: simple in-memory counters
stats = {"users": set(), "groups": set(), "downloads": 0, "searches": 0}
# You might want to replace ADMIN_USER_IDS with the actual Telegram User IDs of admins
ADMIN_USER_IDS = {5491775006} # Example Admin User ID


# === Helper to extract chat member changes ===

def extract_status_change(
    chat_member_update: ChatMemberUpdated,
) -> Optional[Tuple[bool, bool]]:
    """Takes a ChatMemberUpdated instance and extracts whether the 'old_chat_member' was a member
    of the chat and whether the 'new_chat_member' is a member of the chat. Returns None, if
    the status didn't change.
    """
    status_change = chat_member_update.difference().get("status")
    old_is_member, new_is_member = chat_member_update.difference().get("is_member", (None, None))

    if status_change is None:
        return None

    old_status, new_status = status_change
    was_member = old_status in [
        ChatMember.MEMBER,
        ChatMember.OWNER,
        ChatMember.ADMINISTRATOR,
    ] or (old_status == ChatMember.RESTRICTED and old_is_member is True)
    is_member = new_status in [
        ChatMember.MEMBER,
        ChatMember.OWNER,
        ChatMember.ADMINISTRATOR,
    ] or (new_status == ChatMember.RESTRICTED and new_is_member is True)

    return was_member, is_member

# === /start ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    stats["users"].add(user.id) # Track user

    logger.info(f"User {user.first_name} ({user.id}) started the bot.")

    welcome_message = (
        f"🎧 Welcome, {user.first_name}!\n\n"
        "I can help you download songs from Spotify.\n"
        "Just send me the name of a song or paste a Spotify link, and I’ll send you the MP3 file instantly!\n\n"
        "➕ You can also use me in groups. Make sure to add me as an admin to ensure I work properly!"
    )

    add_me_url = f"https://t.me/{context.bot.username}?startgroup=true&admin=post_messages+delete_messages"

    keyboard = [
        [InlineKeyboardButton("📢 Updates", url=UPDATES_CHANNEL_URL)],
        [InlineKeyboardButton("ℹ Help", callback_data="help")],
        [InlineKeyboardButton("➕ Add Me To Your Group", url=add_me_url)],
        [InlineKeyboardButton("🔍 Search a Song", switch_inline_query_current_chat="")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        # Try sending photo with caption and keyboard
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=START_IMAGE_URL,
            caption=welcome_message,
            reply_markup=reply_markup,
            parse_mode="HTML" # Use HTML if your caption needs formatting like bold/italics
        )
    except Exception as e:
        logger.error(f"Failed to send photo for /start: {e}. Sending text fallback.")
        # Fallback to text message if photo fails
        await update.message.reply_text(
            welcome_message,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )

# === /help ===
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a help message when the command /help is issued."""
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

# === /stats ===
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows bot stats (admin only)."""
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("⛔ Sorry, this command is only for bot admins.")
        return

    stats_message = (
        f"📊 Bot Statistics\n\n"
        f"👤 Unique Users: {len(stats['users'])}\n"
        f"👥 Groups: {len(stats['groups'])}\n"
        f"🎵 Searches: {stats['searches']}\n"
        f"📥 Downloads: {stats['downloads']}"
    )
    await update.message.reply_text(stats_message, parse_mode="Markdown")


# === SEARCH ===
def search_tracks(q: str):
    """Searches for tracks using the defined SEARCH_URL."""
    try:
        r = requests.get(SEARCH_URL, params={"q": q}, timeout=10)
        r.raise_for_status()  # Raises HTTPError for bad responses (4XX or 5XX)
        # Check if response is valid JSON
        if r.headers.get('content-type') != 'application/json':
            logger.error(f"Search error: Non-JSON response received from {SEARCH_URL}")
            return []
        return r.json().get("tracks", [])
    except requests.exceptions.RequestException as e:
        logger.error(f"Search request error: {e}")
        return []
    except ValueError as e: # Includes JSONDecodeError
        logger.error(f"Search JSON decoding error: {e}")
        return []
    except Exception as e:
        logger.error(f"Unexpected search error: {e}")
        return []

# === BUILD KEYBOARD ===
def build_kb(chat_id: str, page: int):
    """Builds the inline keyboard for search results pagination."""
    data = sessions.get(chat_id)
    if not data or "tracks" not in data:
        return InlineKeyboardMarkup([]) # Return empty keyboard if no session or tracks

    tracks = data["tracks"]
    start_idx = page * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE
    keyboard = []

    # Create buttons for tracks on the current page
    for idx, track in enumerate(tracks[start_idx:end_idx], start=start_idx):
        # Ensure track name and artist are strings and truncate if too long
        track_name = str(track.get('trackName', 'Unknown Track'))[:50]
        artist = str(track.get('artist', 'Unknown Artist'))[:50]
        label = f"{track_name} — {artist}"
        # Callback data format: "command|chat_id|page|track_index"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"play|{chat_id}|{page}|{idx}")])

    # Create navigation buttons
    nav_row = []
    if page > 0:
        # Callback data format: "command|chat_id|target_page"
        nav_row.append(InlineKeyboardButton("⬅ Back", callback_data=f"page|{chat_id}|{page-1}"))
    if end_idx < len(tracks):
        nav_row.append(InlineKeyboardButton("➡ Next", callback_data=f"page|{chat_id}|{page+1}"))

    if nav_row:
        keyboard.append(nav_row)

    return InlineKeyboardMarkup(keyboard)

# === HANDLE TEXT MESSAGES ===
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming text messages for searching or direct URL processing."""
    message = update.message
    text = message.text.strip()
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id

    logger.info(f"Received text message from {user_id} in chat {chat_id}: {text}")

    # --- Check if it's a Spotify URL ---
    # Adjust regex if the URL pattern is different
    spotify_url_pattern = r"https?://(open\.spotify\.com|googleusercontent\.com/spotify\.com)/[a-zA-Z0-9/]+/([a-zA-Z0-9]{22})"
    match = re.search(spotify_url_pattern, text)

    if match:
        spotify_url = match.group(0) # Get the full matched URL
        logger.info(f"Detected Spotify URL: {spotify_url}")
        # Reply directly to the user's message in groups
        reply_to_message_id = message.message_id if update.effective_chat.type != 'private' else None
        await process_spotify_url(update, context, spotify_url, reply_to_message_id)
        return

    # --- Regular Search ---
    stats["searches"] += 1 # Increment search counter
    tracks = search_tracks(text)

    # Reply directly to the user's message in groups
    reply_to_message_id = message.message_id if update.effective_chat.type != 'private' else None

    if not tracks:
        logger.warning(f"No tracks found for query: {text}")
        await message.reply_text(
            "❌ No tracks found. Please try another title or check the spelling.",
            reply_to_message_id=reply_to_message_id
        )
        return

    logger.info(f"Found {len(tracks)} tracks for query: {text}")

    # Save session data, including the user who initiated the search
    sessions[chat_id] = {
        "tracks": tracks,
        "query": text,
        "ts": time.time(),
        "initiator_user_id": user_id # Store the initiator's ID
    }

    keyboard = build_kb(chat_id, page=0)
    await message.reply_text(
        f"🎧 Results for: {text}",
        parse_mode="Markdown",
        reply_markup=keyboard,
        reply_to_message_id=reply_to_message_id
    )

# === PROCESS SPOTIFY URL ===
async def process_spotify_url(update: Update, context: ContextTypes.DEFAULT_TYPE, spotify_url: str, reply_to_message_id: Optional[int] = None):
    """Downloads and sends a song directly from a Spotify URL."""
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id

    # Send initial "downloading" message, replying if in a group
    try:
        message = await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ Downloading song from Spotify URL...",
            reply_to_message_id=reply_to_message_id
        )
    except Exception as e:
        logger.error(f"Failed to send initial download message for URL: {e}")
        # Attempt to inform the user in the chat without replying
        try:
             await context.bot.send_message(chat_id=chat_id, text="Error initiating download process.")
        except:
            pass # Ignore if even this fails
        return

    try:
        stats["downloads"] += 1 # Increment download counter
        logger.info(f"Fetching download link via API for: {spotify_url}")
        api_request_url = DOWNLOAD_API + spotify_url
        r = requests.get(api_request_url, timeout=30) # Increased timeout for API
        r.raise_for_status()

        # Handle potential non-JSON responses from the download API
        if 'application/json' not in r.headers.get('content-type', ''):
             logger.error(f"Download API error: Non-JSON response from {api_request_url}. Response: {r.text[:200]}")
             await message.edit_text("❌ Download failed. The download service returned an invalid response.")
             return

        payload = r.json()
        logger.info(f"Download API response: {payload}")

        if not payload or not payload.get("status"):
            error_detail = payload.get("message", "API returned an unspecified error.")
            logger.error(f"Download API returned error status: {error_detail}")
            await message.edit_text(f"❌ Download failed: {error_detail}")
            return

        download_link = payload.get("data", {}).get("downloadLink")
        track_name = payload.get("data", {}).get("trackName", "Unknown Track")
        artist = payload.get("data", {}).get("artist", "Unknown Artist")

        if not download_link:
            logger.warning(f"No download link found in API response for {spotify_url}")
            await message.edit_text(
                "❌ Download link not available. The song might be unavailable or the download service is having issues.",
                parse_mode="Markdown"
            )
            return

        logger.info(f"Sending audio for {track_name} - {artist}")
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_audio")

        # Send the audio file
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=download_link,
            caption=f"🎶 {track_name} — {artist}",
            reply_to_message_id=reply_to_message_id # Keep replying to original request if in group
        )

        # Update the initial message to show success
        await message.edit_text(f"✅ Downloaded: {track_name} by {artist}", parse_mode="Markdown")
        logger.info(f"Successfully sent audio for {spotify_url}")

    except requests.exceptions.Timeout:
        logger.error(f"Timeout error fetching download link for {spotify_url}")
        await message.edit_text("❌ Download failed: The request to the download service timed out.")
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error processing Spotify URL {spotify_url}: {e}")
        kb = [[InlineKeyboardButton("🔗 Try Direct Download Link", url=api_request_url)]]
        await message.edit_text(
             f"❌ Network error during download. Please try again or use the direct link:",
             reply_markup=InlineKeyboardMarkup(kb)
        )
    except Exception as e:
        logger.exception(f"Unexpected error processing Spotify URL {spotify_url}: {e}")
        kb = [[InlineKeyboardButton("🔗 Try Direct Download Link", url=api_request_url)]] # Provide direct link if possible
        await message.edit_text(
             f"❌ An unexpected error occurred during download. Please try again or use the direct link:",
             reply_markup=InlineKeyboardMarkup(kb)
        )

# === EXTRACT TRACK ID (Example - Adapt based on actual URLs if needed) ===
def extract_track_id(spotify_url):
    """Extract track ID from various Spotify URL formats including the ones provided."""
    # Pattern for standard open.spotify.com URLs
    standard_pattern = r"https?://open\.spotify\.com/(?:intl-\w+/)?track/([a-zA-Z0-9]{22})"
    match = re.search(standard_pattern, spotify_url)
    if match:
        return match.group(1)

    # Patterns based on the user's provided googleusercontent examples
    google_pattern_0 = r"spotify.com/track/([a-zA-Z0-9]{22})"
    match = re.search(google_pattern_0, spotify_url)
    if match:
        return match.group(1)

    google_pattern_1 = r"spotify.com/track/([a-zA-Z0-9]+)/track/([a-zA-Z0-9]{22})" # Adjusted pattern example
    match = re.search(google_pattern_1, spotify_url)
    if match:
        return match.group(1)

    # Fallback for simpler /track/ structure if others fail
    if "/track/" in spotify_url:
        parts = spotify_url.split("/track/")
        if len(parts) > 1:
            track_id_part = parts[1]
            # Remove query parameters like ?si=...
            track_id = track_id_part.split("?")[0]
            # Check if it looks like a valid ID (22 alphanumeric chars)
            if re.fullmatch(r"[a-zA-Z0-9]{22}", track_id):
                return track_id

    logger.warning(f"Could not extract track ID from URL: {spotify_url}")
    return None

# === CALLBACK HANDLER ===
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button presses from inline keyboards."""
    query = update.callback_query
    await query.answer() # Acknowledge callback

    user_id = query.from_user.id
    data = query.data

    logger.info(f"Callback received from user {user_id}: {data}")

    # Handle simple callbacks like 'help'
    if data == "help":
        await query.edit_message_text(HELP_TEXT, parse_mode="Markdown")
        return

    # Parse callback data for session-based actions
    try:
        parts = data.split("|")
        cmd = parts[0]
        chat_id = parts[1]
        # Further parts depend on the command
    except (IndexError, ValueError) as e:
        logger.error(f"Error parsing callback data '{data}': {e}")
        await query.edit_message_text("❌ Error processing request. Invalid data.")
        return

    session = sessions.get(chat_id)

    # --- Session and Permission Checks ---
    if not session:
        logger.warning(f"Session expired or not found for chat {chat_id} on callback.")
        await query.edit_message_text("⏰ Session expired. Please start a new search.")
        return

    # Check if the user pressing the button is the one who started the search (for groups)
    # Allow in private chats OR if the user matches the initiator
    if update.effective_chat.type != 'private' and user_id != session.get("initiator_user_id"):
        logger.info(f"User {user_id} tried to interact with search results initiated by {session.get('initiator_user_id')} in chat {chat_id}.")
        await context.bot.answer_callback_query(
            callback_query_id=query.id,
            text="⚠ Only the user who started the search can use these buttons.",
            show_alert=True # Show a popup alert
        )
        return

    # Timeout check
    if time.time() - session.get("ts", 0) > TIMEOUT_SEC:
        sessions.pop(chat_id, None)
        logger.info(f"Session timed out for chat {chat_id}.")
        await query.edit_message_text("⏰ Session timed out. Please start a new search.")
        return

    # Refresh timestamp
    session["ts"] = time.time()

    # --- Handle Commands ---
    if cmd == "page":
        try:
            page = int(parts[2])
            logger.info(f"Handling page navigation for chat {chat_id}, page {page}")
            keyboard = build_kb(chat_id, page)
            await query.edit_message_text(
                f"🎧 Results for: {session.get('query', 'your search')}",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        except (IndexError, ValueError) as e:
             logger.error(f"Error parsing page number from callback '{data}': {e}")
             await query.edit_message_text("❌ Error changing page.")
        except Exception as e:
            logger.exception(f"Unexpected error during pagination for chat {chat_id}: {e}")
            await query.edit_message_text("❌ An error occurred while loading the next page.")

    elif cmd == "play":
        try:
            page = int(parts[2]) # Page number is needed to calculate correct index
            track_index = int(parts[3])
            logger.info(f"Handling play request for chat {chat_id}, track index {track_index}")

            tracks = session.get("tracks", [])
            if not tracks or track_index < 0 or track_index >= len(tracks):
                logger.warning(f"Invalid track index {track_index} requested for chat {chat_id}")
                await query.edit_message_text("❌ Invalid selection or tracks not found.")
                return

            track = tracks[track_index]
            track_name = track.get('trackName', 'Unknown Track')
            artist = track.get('artist', 'Unknown Artist')
            spotify_url = track.get("spotifyUrl") # Ensure this key exists in your search results

            if not spotify_url:
                logger.warning(f"No Spotify URL found for track index {track_index} in chat {chat_id}")
                await query.edit_message_text("❌ No Spotify URL available for this track.")
                return

            # Edit the original message to show "Downloading..."
            await query.edit_message_text(f"⏳ Downloading: {track_name} by {artist}...", parse_mode="Markdown")

            # Process the URL (similar to on_text but uses query.message for potential edits)
            # Need to pass the message object from the callback query to edit it later
            callback_message = query.message
            await process_spotify_url_from_callback(context, chat_id, user_id, spotify_url, track_name, artist, callback_message)

        except (IndexError, ValueError) as e:
             logger.error(f"Error parsing track index from callback '{data}': {e}")
             await query.edit_message_text("❌ Error selecting track.")
        except Exception as e:
            logger.exception(f"Unexpected error during play request for chat {chat_id}: {e}")
            await query.edit_message_text("❌ An error occurred while trying to download the track.")

    else:
        logger.warning(f"Unhandled callback command '{cmd}' received.")
        # Optionally inform the user
        # await query.edit_message_text("Unknown command.")


async def process_spotify_url_from_callback(context: ContextTypes.DEFAULT_TYPE, chat_id: str, user_id: int, spotify_url: str, track_name: str, artist: str, callback_message):
    """Downloads and sends a song initiated from a callback button press."""
    api_request_url = "" # Define outside try block for use in except
    try:
        stats["downloads"] += 1 # Increment download counter
        logger.info(f"Fetching download link via API for callback: {spotify_url}")
        api_request_url = DOWNLOAD_API + spotify_url
        r = requests.get(api_request_url, timeout=30)
        r.raise_for_status()

        if 'application/json' not in r.headers.get('content-type', ''):
             logger.error(f"Callback Download API error: Non-JSON response from {api_request_url}. Response: {r.text[:200]}")
             await callback_message.edit_text("❌ Download failed. The download service returned an invalid response.")
             return

        payload = r.json()
        logger.info(f"Callback Download API response: {payload}")

        if not payload or not payload.get("status"):
            error_detail = payload.get("message", "API returned an unspecified error.")
            logger.error(f"Callback Download API returned error status: {error_detail}")
            await callback_message.edit_text(f"❌ Download failed: {error_detail}")
            return

        download_link = payload.get("data", {}).get("downloadLink")
        # Use track_name and artist passed from the callback handler if API doesn't return them
        api_track_name = payload.get("data", {}).get("trackName", track_name)
        api_artist = payload.get("data", {}).get("artist", artist)


        if not download_link:
            logger.warning(f"No download link found in API response for callback {spotify_url}")
            await callback_message.edit_text(
                "❌ Download link not available. The song might be unavailable or the download service is having issues."
            )
            return

        logger.info(f"Sending audio for callback {api_track_name} - {api_artist}")
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_audio")

        # Send the audio file - DO NOT reply to the callback message itself with audio
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=download_link,
            caption=f"🎶 {api_track_name} — {api_artist}"
        )

        # Update the original message (from the callback) to show success
        await callback_message.edit_text(f"✅ Downloaded: {api_track_name} by {api_artist}", parse_mode="Markdown")
        logger.info(f"Successfully sent audio for callback {spotify_url}")

    except requests.exceptions.Timeout:
        logger.error(f"Timeout error fetching download link for callback {spotify_url}")
        await callback_message.edit_text("❌ Download failed: The request to the download service timed out.")
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error processing callback Spotify URL {spotify_url}: {e}")
        kb = [[InlineKeyboardButton("🔗 Try Direct Download Link", url=api_request_url)]]
        await callback_message.edit_text(
             f"❌ Network error during download. Please try again or use the direct link:",
             reply_markup=InlineKeyboardMarkup(kb)
        )
    except Exception as e:
        logger.exception(f"Unexpected error processing callback Spotify URL {spotify_url}: {e}")
        kb = [[InlineKeyboardButton("🔗 Try Direct Download Link", url=api_request_url)]]
        await callback_message.edit_text(
             f"❌ An unexpected error occurred during download. Please try again or use the direct link:",
             reply_markup=InlineKeyboardMarkup(kb)
         )


# === GROUP JOIN HANDLER ===
async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles members joining or leaving chats."""
    result = extract_status_change(update.chat_member)
    if result is None:
        return  # Ignore status changes that aren't membership changes

    was_member, is_member = result
    cause_name = update.chat_member.from_user.mention_html()
    member_name = update.chat_member.new_chat_member.user.mention_html()
    chat = update.effective_chat
    bot_id = context.bot.id

    # Check if the update concerns the bot itself
    if update.chat_member.new_chat_member.user.id == bot_id:
        if not was_member and is_member:
            logger.info(f"Bot was added to chat {chat.title} ({chat.id}) by {update.chat_member.from_user.id}")
            stats["groups"].add(chat.id) # Track group
            await context.bot.send_message(
                chat.id,
                f"👋 Thanks for adding me to {chat.title}, {cause_name}!\n\n"
                f"I can download Spotify songs here. Just send a song name or link.\n"
                f"Use /help for more info.",
                parse_mode="Markdown"
            )
        elif was_member and not is_member:
            logger.info(f"Bot was removed from chat {chat.title} ({chat.id}) by {update.chat_member.from_user.id}")
            stats["groups"].discard(chat.id) # Untrack group
            # You might not be able to send a message here if the bot was kicked/left
    # else:
        # Handle other users joining/leaving if needed
        # if not was_member and is_member:
        #     logger.info(f"{member_name} was added by {cause_name}")
        # elif was_member and not is_member:
        #     logger.info(f"{member_name} left or was removed by {cause_name}")


# === MAIN ===
def main():
    """Starts the bot."""
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Command Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))

    # Message Handler for text messages (searches and URLs)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Callback Query Handler for inline buttons
    application.add_handler(CallbackQueryHandler(on_callback))

    # Chat Member Handler for group joins/leaves
    application.add_handler(ChatMemberHandler(on_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))


    logger.info("Bot starting to poll...")
    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Bot stopped.")


if _name_ == "_main_":
    main()
