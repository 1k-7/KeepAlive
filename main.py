import logging
import re
import asyncio
import os
from threading import Thread
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from pyrogram import Client
from pyrogram.errors import YouBlockedUser, PeerIdInvalid, UserIsBot
import motor.motor_asyncio

# --- Configuration & Setup ---

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Web Server for Render Health Checks ---
web_app = Flask(__name__)

@web_app.route('/')
def health_check():
    """Render health check endpoint."""
    return "OK", 200

def run_web_server():
    """Runs the Flask web server."""
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

# --- State definitions for ConversationHandler ---
(
    SELECTING_ACTION,
    AWAIT_BOT_USERNAME,
    AWAIT_SESSION_STRING,
    AWAIT_INTERVAL,
    MANAGE_BOTS,
) = range(5)


# --- Database Configuration ---
MONGO_URI = os.environ.get("DATABASE_URI")
if not MONGO_URI:
    logger.critical("DATABASE_URI environment variable is not set!")
    MONGO_URI = "mongodb://localhost:27017" # Dummy value for graceful failure

try:
    client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
    db = client["KeepAliveBotDB"]
    settings_collection = db["settings"]
    logger.info("Successfully connected to MongoDB.")
except Exception as e:
    logger.critical(f"Failed to connect to MongoDB: {e}")
    client = None

# --- Data Persistence (MongoDB) ---

def get_default_data():
    """Returns a dictionary with the default settings."""
    return {
        "userbot_session": None,
        "target_bots": [],
        "ping_interval_seconds": 420,  # Default 7 minutes
        "is_running": False,
    }

async def load_data(chat_id: int):
    """Loads bot data for a specific user from MongoDB."""
    if not client: return get_default_data()
    document = await settings_collection.find_one({"_id": chat_id})
    if document:
        defaults = get_default_data(); defaults.update(document); return defaults
    return get_default_data()

async def save_data(chat_id: int, data: dict):
    """Saves bot data for a specific user to MongoDB."""
    if not client:
        logger.error("Cannot save data, no database connection.")
        return
    data.pop('_id', None)
    await settings_collection.update_one({"_id": chat_id}, {"$set": data}, upsert=True)

# --- Pinger (Userbot) Logic ---

async def ping_bots_task(context: ContextTypes.DEFAULT_TYPE):
    """The core background task that uses the userbot to ping other bots."""
    chat_id = context.job.chat_id
    data = await load_data(chat_id)

    if not data["is_running"]:
        logger.info(f"Pinger is stopped for chat_id {chat_id}. Exiting task.")
        return

    if not data["userbot_session"]:
        logger.warning(f"Userbot session string not set for chat_id {chat_id}. Stopping pinger.")
        await context.bot.send_message(chat_id, "⚠️ Userbot session not found! Stopping the pinger.")
        data["is_running"] = False; await save_data(chat_id, data)
        return
    
    my_bot_info = await context.bot.get_me()
    my_bot_username = f"@{my_bot_info.username}"
    all_bots_to_ping = list(set(data.get("target_bots", []) + [my_bot_username]))

    if not all_bots_to_ping:
        logger.warning(f"No target bots to ping for chat_id {chat_id}. Stopping pinger.")
        await context.bot.send_message(chat_id, "⚠️ No bots in the list to ping! Stopping the pinger.")
        data["is_running"] = False; await save_data(chat_id, data)
        return
        
    logger.info(f"Pinger starting for {', '.join(all_bots_to_ping)} (chat_id: {chat_id})")
    
    try:
        async with Client("userbot", session_string=data["userbot_session"], in_memory=True) as app:
            ping_results = []
            for bot_username in all_bots_to_ping:
                try:
                    await app.send_message(bot_username, "/start")
                    ping_results.append(f"✅ `{bot_username}`: OK")
                except Exception as e:
                    logger.error(f"Error pinging {bot_username}: {e}")
                    ping_results.append(f"❌ `{bot_username}`: Error ({type(e).__name__})")
            
            status_message = "Ping cycle complete:\n" + "\n".join(ping_results)
            await context.bot.send_message(chat_id, status_message, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Critical userbot error for chat_id {chat_id}: {e}")
        await context.bot.send_message(chat_id, f"🚨 Critical Error with Userbot: `{e}`. Stopping pinger.", parse_mode="Markdown")
        data["is_running"] = False; await save_data(chat_id, data)

# --- Bot UI and Handlers ---

async def check_db_connection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not client:
        reply_target = update.message or update.callback_query.message
        await reply_target.reply_text(
            "🚨 **Database Error**\nI can't connect. Check `DATABASE_URI` and restart.",
            parse_mode="Markdown"
        )
        return False
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await check_db_connection(update, context): return ConversationHandler.END
    user = update.effective_user
    await update.message.reply_html(
        rf"👋 Hello, {user.mention_html()}!"
        "\n\nI am your Bot Keep-Alive assistant. I use a user account to keep your bots online."
        "\n\n<b>My settings are persistent!</b> I'll remember everything even after a restart."
    )
    await show_main_menu(update, context)
    return SELECTING_ACTION

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    data = await load_data(chat_id)
    
    session_status = "✅ Set" if data.get("userbot_session") else "❌ Not Set"
    bots_status = f" ({len(data.get('target_bots', []))}) added"
    interval_minutes = data.get('ping_interval_seconds', 420) / 60
    pinger_status = "🟢 Running" if data.get("is_running", False) else "🔴 Stopped"
    start_stop_text = "Stop Pinger" if data.get("is_running", False) else "Start Pinger"

    keyboard = [
        [InlineKeyboardButton(f"⚙️ Set Userbot Session [{session_status}]", callback_data="set_session")],
        [InlineKeyboardButton(f"🤖 Manage Other Bots{bots_status}", callback_data="manage_bots")],
        [InlineKeyboardButton(f"⏱️ Set Interval ({interval_minutes:.0f} min)", callback_data="set_interval")],
        [InlineKeyboardButton(start_stop_text, callback_data="toggle_pinger")],
        [InlineKeyboardButton(f"📊 Status: {pinger_status}", callback_data="status_check")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    message_text = "👇 **Main Menu** 👇"
    
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode="Markdown")

async def save_session_string(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    data = await load_data(chat_id)
    data["userbot_session"] = update.message.text
    await save_data(chat_id, data)
    await update.message.reply_text("✅ Userbot session string saved permanently.")
    await show_main_menu(update, context)
    return SELECTING_ACTION

async def save_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    try:
        interval_minutes = int(update.message.text)
        if interval_minutes <= 0: raise ValueError
        
        data = await load_data(chat_id)
        data["ping_interval_seconds"] = interval_minutes * 60
        await save_data(chat_id, data)
        await update.message.reply_text(f"✅ Interval set to {interval_minutes} minutes.")
        
        if data.get("is_running", False):
            for job in context.job_queue.get_jobs_by_name(f"pinger_job_{chat_id}"): job.schedule_removal()
            context.job_queue.run_repeating(
                ping_bots_task, interval=data["ping_interval_seconds"], first=1,
                name=f"pinger_job_{chat_id}", chat_id=chat_id
            )
            await update.message.reply_text("Pinger schedule updated.")
    except (ValueError, TypeError):
        await update.message.reply_text("Invalid input. Please send a positive number.")
    await show_main_menu(update, context); return SELECTING_ACTION

# --- NEW: Bulk-add logic ---
async def save_bot_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Saves one or more bot usernames from a single message."""
    chat_id = update.effective_chat.id
    raw_text = update.message.text
    
    # Regex to find all valid @usernames
    potential_bots = re.findall(r"@[a-zA-Z0-9_]{5,32}", raw_text)

    if not potential_bots:
        await update.message.reply_text("I couldn't find any valid bot usernames (like @my_bot) in your message. Please try again.")
        return AWAIT_BOT_USERNAME

    data = await load_data(chat_id)
    my_bot_info = await context.bot.get_me()
    my_bot_username = f"@{my_bot_info.username}"

    added_bots = []
    skipped_bots = []
    
    # Use a set for efficient checking of existing bots
    existing_bots = set(data.get("target_bots", []))

    for bot in potential_bots:
        if bot == my_bot_username:
            skipped_bots.append(f"`{bot}` (it's me!)")
        elif bot in existing_bots:
            skipped_bots.append(f"`{bot}` (already added)")
        else:
            existing_bots.add(bot)
            added_bots.append(f"`{bot}`")
    
    # Update data in DB only if there are new additions
    if added_bots:
        data["target_bots"] = sorted(list(existing_bots)) # Save as a sorted list
        await save_data(chat_id, data)

    # Build and send a summary message
    summary_parts = []
    if added_bots:
        summary_parts.append("✅ **Added:**\n" + "\n".join(added_bots))
    if skipped_bots:
        summary_parts.append("☑️ **Skipped:**\n" + "\n".join(skipped_bots))
        
    await update.message.reply_text("\n\n".join(summary_parts), parse_mode="Markdown")
    
    await manage_bots_menu(update, context)
    return MANAGE_BOTS
# --- End of bulk-add logic ---

async def remove_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    chat_id = update.effective_chat.id
    bot_username = query.data.replace("remove_", "")
    data = await load_data(chat_id)
    if "target_bots" in data and bot_username in data["target_bots"]:
        data["target_bots"].remove(bot_username)
        await save_data(chat_id, data)
    await manage_bots_menu(update, context); return MANAGE_BOTS

async def toggle_pinger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    chat_id = update.effective_chat.id
    if not await check_db_connection(update, context): return SELECTING_ACTION

    data = await load_data(chat_id)
    job_name = f"pinger_job_{chat_id}"

    if data.get("is_running", False):
        data["is_running"] = False
        await save_data(chat_id, data)
        for job in context.job_queue.get_jobs_by_name(job_name): job.schedule_removal()
        await query.message.reply_text("🔴 Pinger has been stopped.")
    else:
        if not data.get("userbot_session"):
            await query.message.reply_text("⚠️ Cannot start: Userbot session is not set.")
        else:
            data["is_running"] = True
            await save_data(chat_id, data)
            context.job_queue.run_repeating(
                ping_bots_task, interval=data["ping_interval_seconds"], first=1, name=job_name, chat_id=chat_id
            )
            await query.message.reply_text(f"🟢 Pinger started! Will ping every {data['ping_interval_seconds']/60:.0f} minutes.")

    await show_main_menu(update, context); return SELECTING_ACTION
    
async def post_init(application: Application):
    """Restore running jobs on bot restart from database."""
    if not client:
        logger.warning("post_init: No DB connection, skipping job restoration.")
        return
    logger.info("Checking for jobs to restore from database...")
    async for doc in settings_collection.find({"is_running": True}):
        chat_id = doc["_id"]
        interval = doc.get("ping_interval_seconds", 420)
        job_name = f"pinger_job_{chat_id}"
        application.job_queue.run_repeating(
            ping_bots_task, interval=interval, first=10, name=job_name, chat_id=chat_id
        )
        logger.info(f"Restored pinger job for chat_id {chat_id} with interval {interval}s.")

# --- Prompting and Menu Handlers ---
async def prompt_session_string(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    await query.message.reply_text("Please send your Pyrogram session string.\n\nSend /cancel to return.")
    return AWAIT_SESSION_STRING

async def prompt_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    await query.message.reply_text("Please enter the ping interval in minutes (e.g., `7`).\n\nSend /cancel to return.")
    return AWAIT_INTERVAL
    
async def manage_bots_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    if update.callback_query: await update.callback_query.answer()
    data = await load_data(chat_id)
    keyboard = [[InlineKeyboardButton("➕ Add New Bot(s)", callback_data="add_bot_prompt")]]
    
    for bot_username in data.get("target_bots", []):
        keyboard.append([InlineKeyboardButton(f"➖ Remove {bot_username}", callback_data=f"remove_{bot_username}")])

    keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "🤖 **Manage Other Bots**\n\nI keep myself awake automatically. Add your *other* bots here."
    
    reply_target = update.callback_query.message if update.callback_query else update.message
    if update.callback_query:
        await reply_target.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await reply_target.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    return MANAGE_BOTS

# --- NEW: Updated prompt text ---
async def prompt_add_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    await query.message.reply_text(
        "Please send one or more bot usernames to add.\n\n"
        "You can separate them with spaces, commas, or put each on a new line.\n\n"
        "Example:\n`@my_bot1, @my_bot2 @my_bot3`\n\n"
        "Send /cancel to go back.",
        parse_mode="Markdown"
        )
    return AWAIT_BOT_USERNAME
    
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Operation cancelled.")
    await show_main_menu(update, context); return SELECTING_ACTION

def main() -> None:
    web_thread = Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()
    logger.info("Web server started for health checks.")

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.critical("TELEGRAM_BOT_TOKEN environment variable not set! Exiting."); return
        
    application = Application.builder().token(token).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECTING_ACTION: [
                CallbackQueryHandler(prompt_session_string, pattern="^set_session$"),
                CallbackQueryHandler(manage_bots_menu, pattern="^manage_bots$"),
                CallbackQueryHandler(prompt_interval, pattern="^set_interval$"),
                CallbackQueryHandler(toggle_pinger, pattern="^toggle_pinger$"),
                CallbackQueryHandler(show_main_menu, pattern="^status_check$"),
            ],
            AWAIT_SESSION_STRING: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_session_string)],
            AWAIT_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_interval)],
            AWAIT_BOT_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_bot_username)],
            MANAGE_BOTS: [
                CallbackQueryHandler(prompt_add_bot, pattern="^add_bot_prompt$"),
                CallbackQueryHandler(remove_bot, pattern="^remove_.*$"),
                CallbackQueryHandler(show_main_menu, pattern="^main_menu$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )

    application.add_handler(conv_handler)
    logger.info("Bot is starting polling...")
    application.run_polling()

if __name__ == "__main__":
    main()

