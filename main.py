# Veltrix Store Bot - Full Version
# Auto pay + restock announce + all features

import os
import sqlite3
import logging
import asyncio
import random
import string
import hmac
import hashlib
import time
from datetime import datetime, timezone

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

ADMIN_ID = 6800369787
FORCE_JOIN_CHANNEL = "@veltrixxstore"
SUPPORT_USERNAME = "@sagaredtzz"
BEP20_ADDRESS = "0x99f30ec925ff5b997c582e8c9ac92ffe9093d9c8"
BINANCE_PAY_ID = "511225998"
STORE_NAME = "Veltrix Store"
DB_NAME = "/data/veltrix_store.db"
ANNOUNCE_CHANNEL = "@veltrixxstore"
BOT_USERNAME = "VeltrixStore_bot"

BOT_TOKEN = os.getenv("BOT_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
if not BOT_TOKEN:
    raise Exception("BOT_TOKEN environment variable is missing!")


def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
        balance REAL DEFAULT 0.0, total_spent REAL DEFAULT 0.0,
        total_orders INTEGER DEFAULT 0, joined_at TEXT,
        is_banned INTEGER DEFAULT 0, last_active TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS products (
        product_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        duration TEXT, price REAL NOT NULL, emoji TEXT, is_active INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS stock (
        stock_id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER,
        content TEXT NOT NULL, status TEXT DEFAULT 'AVAILABLE',
        reserved_by INTEGER, reserved_at TEXT, delivered_at TEXT, order_id TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY, user_id INTEGER, product_id INTEGER,
        quantity INTEGER, total_amount REAL, payment_method TEXT,
        status TEXT DEFAULT 'PENDING', txid TEXT, binance_ref TEXT,
        created_at TEXT, approved_at TEXT, delivered_at TEXT, admin_note TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS wallet_deposits (
        deposit_id TEXT PRIMARY KEY, user_id INTEGER, amount REAL,
        payment_method TEXT, status TEXT DEFAULT 'PENDING',
        txid TEXT, binance_ref TEXT, created_at TEXT, approved_at TEXT, admin_note TEXT)''')
    c.execute("SELECT COUNT(*) FROM products")
    if c.fetchone()[0] == 0:
        products = [
            ("Netflix 4K UHD", "1 Month", 1.00, "📺"),
            ("Spotify Premium", "2 Months", 0.60, "🎵"),
            ("YouTube Premium Invite", "1 Month", 0.30, "▶️"),
            ("Gemini Pro", "18 Months", 0.50, "✨"),
        ]
        c.executemany("INSERT INTO products (name, duration, price, emoji) VALUES (?, ?, ?, ?)", products)
    conn.commit()
    conn.close()
    print("Database initialized")


def generate_order_id():
    return f"VTX-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{''.join(random.choices(string.ascii_uppercase + string.digits, k=6))}"


def generate_deposit_id():
    return f"DEP-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{''.join(random.choices(string.ascii_uppercase + string.digits, k=6))}"


def is_admin(user_id):
    return user_id == ADMIN_ID


def format_money(amount):
    return f"${amount:.2f}"


def get_current_time():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def is_txid_already_used(txid: str) -> bool:
    if not txid:
        return False
    txid = txid.strip().lower()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT order_id FROM orders WHERE LOWER(COALESCE(txid,'')) = ? OR LOWER(COALESCE(binance_ref,'')) = ?", (txid, txid))
    if c.fetchone():
        conn.close()
        return True
    c.execute("SELECT deposit_id FROM wallet_deposits WHERE LOWER(COALESCE(txid,'')) = ? OR LOWER(COALESCE(binance_ref,'')) = ?", (txid, txid))
    if c.fetchone():
        conn.close()
        return True
    conn.close()
    return False


async def check_binance_deposit(txid_or_ref: str, expected_amount: float) -> dict:
    try:
        if not BINANCE_API_KEY or not BINANCE_API_SECRET:
            return {"found": False, "message": "API keys not configured"}
        timestamp = int(time.time() * 1000)
        query = f"timestamp={timestamp}&limit=100"
        signature = hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = f"https://api.binance.com/sapi/v1/capital/deposit/hisrec?{query}&signature={signature}"
        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    return {"found": False, "message": f"Binance API error: {resp.status}"}
                deposits = await resp.json()
        if not isinstance(deposits, list):
            return {"found": False, "message": "Invalid response"}
        search = txid_or_ref.strip().lower()
        for dep in deposits:
            tx_id = str(dep.get("txId", "")).lower()
            amount = float(dep.get("amount", 0) or 0)
            if dep.get("status") == 1 and search and (search in tx_id or search == tx_id):
                if abs(amount - expected_amount) <= 0.05 or amount >= expected_amount:
                    return {"found": True, "message": f"Found {amount} USDT", "amount": amount}
        return {"found": False, "message": "Not found in recent deposits"}
    except Exception as e:
        return {"found": False, "message": str(e)}


async def send_youtube_instruction(context, order_id, user_id):
    text = (
        f"✅ <b>YouTube Premium – Payment Successful!</b>\n\n"
        f"🆔 Order ID: <code>{order_id}</code>\n\n"
        f"Please send your <b>Gmail ID only</b> to:\n👉 <b>@veltrixstoreyoutube_bot</b>"
    )
    await context.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)


async def send_purchase_announcement(context, product_name, quantity, emoji):
    try:
        await context.bot.send_message(
            chat_id=ANNOUNCE_CHANNEL,
            text=f"🛒 Someone just bought <b>{quantity}×</b> {emoji} <b>{product_name}</b>!!",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        print(f"Announce error: {e}")


async def send_new_product_announcement(context, name, duration, price, emoji, product_id):
    try:
        text = (
            f"🆕 <b>New Product Available!</b>\n\n"
            f"{emoji} <b>{name}</b>\n"
            f"⏱ Duration: <b>{duration}</b>\n"
            f"💰 Price: <b>{format_money(price)}</b>\n\n"
            f"Tap Buy Now 👇"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🛒 Buy Now", url=f"https://t.me/{BOT_USERNAME}?start=buy_{product_id}")
        ]])
        await context.bot.send_message(chat_id=ANNOUNCE_CHANNEL, text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"New product announce error: {e}")


async def send_restock_announcement(context, product, added, available):
    try:
        text = (
            f"📦 <b>Restock Alert!</b>\n\n"
            f"{product['emoji']} <b>{product['name']}</b>\n"
            f"⏱ {product.get('duration') or ''}\n"
            f"➕ Added: <b>{added}</b>\n"
            f"📊 Available now: <b>{available}</b>\n"
            f"💰 Price: <b>{format_money(product['price'])}</b>\n\n"
            f"Tap Buy Now 👇"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🛒 Buy Now", url=f"https://t.me/{BOT_USERNAME}?start=buy_{product['product_id']}")
        ]])
        await context.bot.send_message(chat_id=ANNOUNCE_CHANNEL, text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"Restock announce error: {e}")


async def check_membership(user_id, context):
    try:
        member = await context.bot.get_chat_member(chat_id=FORCE_JOIN_CHANNEL, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.error(f"Membership error: {e}")
        return False


async def force_join_message(update, context):
    keyboard = [
        [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{FORCE_JOIN_CHANNEL.replace('@', '')}")],
        [InlineKeyboardButton("✅ Check Membership", callback_data="check_membership")]
    ]
    text = (
        f"🔒 <b>Channel Membership Required</b>\n\n"
        f"Welcome to <b>{STORE_NAME}</b>!\n\n"
        f"Join: <b>{FORCE_JOIN_CHANNEL}</b>\nThen press ✅ Check Membership"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def check_membership_callback(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if await check_membership(user_id, context):
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if not c.fetchone():
            c.execute("INSERT INTO users (user_id, username, full_name, joined_at, last_active) VALUES (?, ?, ?, ?, ?)",
                      (user_id, query.from_user.username, query.from_user.full_name, get_current_time(), get_current_time()))
        else:
            c.execute("UPDATE users SET last_active = ? WHERE user_id = ?", (get_current_time(), user_id))
        conn.commit()
        conn.close()
        await show_main_menu(update, context)
    else:
        await query.answer("❌ You haven't joined yet!", show_alert=True)
        await force_join_message(update, context)


async def open_product_from_deep_link(update, context, product_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE product_id = ? AND is_active = 1", (product_id,))
    product = c.fetchone()
    c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (product_id,))
    stock_count = c.fetchone()[0]
    conn.close()
    if not product or stock_count == 0:
        await update.message.reply_text("❌ Product not available or out of stock.")
        await show_main_menu(update, context)
        return
    context.user_data['selected_product_id'] = product_id
    context.user_data['selected_product'] = dict(product)
    text = (
        f"{product['emoji']} <b>{product['name']}</b>\n"
        f"⏱ Duration: <b>{product['duration']}</b>\n"
        f"💵 Price: <b>{format_money(product['price'])}</b> each\n"
        f"📦 Available: <b>{stock_count}</b>\n\nSelect quantity:"
    )
    max_qty = min(5, stock_count)
    keyboard, row = [], []
    for i in range(1, max_qty + 1):
        row.append(InlineKeyboardButton(str(i), callback_data=f"qty_{i}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Shop", callback_data="shop")])
    await update.message.reply_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def start_command(update, context):
    user_id = update.effective_user.id
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row and row["is_banned"]:
        conn.close()
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return
    conn.close()

    if not await check_membership(user_id, context):
        await force_join_message(update, context)
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if not c.fetchone():
        c.execute("INSERT INTO users (user_id, username, full_name, joined_at, last_active) VALUES (?, ?, ?, ?, ?)",
                  (user_id, update.effective_user.username, update.effective_user.full_name, get_current_time(), get_current_time()))
    else:
        c.execute("UPDATE users SET last_active = ?, username = ?, full_name = ? WHERE user_id = ?",
                  (get_current_time(), update.effective_user.username, update.effective_user.full_name, user_id))
    conn.commit()
    conn.close()

    if context.args and context.args[0].startswith("buy_"):
        try:
            await open_product_from_deep_link(update, context, int(context.args[0].replace("buy_", "")))
            return
        except Exception as e:
            print(f"Deep link error: {e}")

    await show_main_menu(update, context)


async def show_main_menu(update, context):
    user = update.effective_user
    text = f"🏠 <b>Welcome to {STORE_NAME}</b>\n\nHello <b>{user.first_name}</b>!\nChoose an option:"
    keyboard = [
        [InlineKeyboardButton("🛍️ Shop Products", callback_data="shop")],
        [InlineKeyboardButton("👤 Profile", callback_data="profile"), InlineKeyboardButton("💰 Wallet", callback_data="wallet")],
        [InlineKeyboardButton("📦 My Orders", callback_data="my_orders"), InlineKeyboardButton("🎧 Support", callback_data="support")],
        [InlineKeyboardButton("📜 Terms & Info", callback_data="terms")]
    ]
    if update.callback_query:
        await update.callback_query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        await update.callback_query.answer()
    else:
        await update.message.reply_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def main_menu_callback(update, context):
    await update.callback_query.answer()
    await show_main_menu(update, context)


async def profile_handler(update, context):
    query = update.callback_query
    await query.answer()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (query.from_user.id,))
    user = c.fetchone()
    conn.close()
    if not user:
        await query.edit_message_text("❌ User not found. Please /start again.")
        return
    text = (
        f"👤 <b>Your Profile</b>\n\n"
        f"🆔 User ID: <code>{user['user_id']}</code>\n"
        f"👤 Name: <b>{user['full_name'] or 'N/A'}</b>\n"
        f"🔗 Username: @{user['username'] or 'None'}\n\n"
        f"💰 Balance: <b>{format_money(user['balance'])}</b>\n"
        f"🛒 Total Spent: <b>{format_money(user['total_spent'])}</b>\n"
        f"📦 Total Orders: <b>{user['total_orders']}</b>\n"
        f"📅 Joined: <b>{user['joined_at']}</b>"
    )
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]]), parse_mode=ParseMode.HTML)


async def support_handler(update, context):
    query = update.callback_query
    await query.answer()
    text = f"🎧 <b>Support</b>\n\nContact: {SUPPORT_USERNAME}\nInclude your Order ID if needed."
    keyboard = [
        [InlineKeyboardButton("📩 Contact Support", url=f"https://t.me/{SUPPORT_USERNAME.replace('@', '')}")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def terms_handler(update, context):
    query = update.callback_query
    await query.answer()
    text = (
        f"📜 <b>Terms & Information</b>\n\n"
        f"• Digital products after payment approval\n"
        f"• BEP20 may auto-verify\n"
        f"• Binance Pay may need manual approval\n"
        f"• No refunds after delivery\n"
        f"• Support: {SUPPORT_USERNAME}\n\n<b>{STORE_NAME}</b>"
    )
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]]), parse_mode=ParseMode.HTML)


async def shop_handler(update, context):
    query = update.callback_query
    await query.answer()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE is_active = 1 ORDER BY product_id")
    products = c.fetchall()
    conn.close()
    text = "🛍️ <b>Shop Products</b>\n\nSelect a product:"
    keyboard = []
    for p in products:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (p['product_id'],))
        stock_count = c.fetchone()[0]
        conn.close()
        btn = f"{p['emoji']} {p['name']} ({p['duration']}) — {format_money(p['price'])}"
        if stock_count == 0:
            btn += " [Out of Stock]"
        keyboard.append([InlineKeyboardButton(btn, callback_data=f"product_{p['product_id']}")])
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")])
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def product_selected(update, context):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("_")[1])
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE product_id = ?", (product_id,))
    product = c.fetchone()
    c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (product_id,))
    stock_count = c.fetchone()[0]
    conn.close()
    if not product or stock_count == 0:
        await query.edit_message_text("❌ Product not available or out of stock.")
        return
    context.user_data['selected_product_id'] = product_id
    context.user_data['selected_product'] = dict(product)
    text = (
        f"{product['emoji']} <b>{product['name']}</b>\n"
        f"⏱ Duration: <b>{product['duration']}</b>\n"
        f"💵 Price: <b>{format_money(product['price'])}</b> each\n"
        f"📦 Available: <b>{stock_count}</b>\n\nSelect quantity:"
    )
    max_qty = min(5, stock_count)
    keyboard, row = [], []
    for i in range(1, max_qty + 1):
        row.append(InlineKeyboardButton(str(i), callback_data=f"qty_{i}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="shop")])
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def quantity_selected(update, context):
    query = update.callback_query
    await query.answer()
    qty = int(query.data.split("_")[1])
    product = context.user_data.get('selected_product')
    if not product:
        await query.edit_message_text("❌ Session expired. Please start again.")
        return
    total = product['price'] * qty
    context.user_data['quantity'] = qty
    context.user_data['total_amount'] = total
    text = (
        f"🛒 <b>Order Summary</b>\n\n"
        f"{product['emoji']} <b>{product['name']}</b>\n"
        f"📦 Quantity: <b>{qty}</b>\n"
        f"💰 <b>Total: {format_money(total)}</b>\n\nSelect payment method:"
    )
    keyboard = [
        [InlineKeyboardButton("💰 Pay from Wallet", callback_data="pay_wallet")],
        [InlineKeyboardButton("🪙 Pay with BEP20", callback_data="pay_bep20")],
        [InlineKeyboardButton("🔵 Pay with Binance Pay", callback_data="pay_binance")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"product_{product['product_id']}")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
async def pay_from_wallet(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    product = context.user_data.get('selected_product')
    qty = context.user_data.get('quantity')
    total = context.user_data.get('total_amount')
    if not product or not qty or total is None:
        await query.edit_message_text("❌ Session expired. Please start again from Shop.")
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    balance = row['balance'] if row else 0.0
    if balance < total:
        conn.close()
        text = f"❌ <b>Insufficient Balance</b>\n\nYour balance: <b>{format_money(balance)}</b>\nRequired: <b>{format_money(total)}</b>"
        keyboard = [[InlineKeyboardButton("💰 Go to Wallet", callback_data="wallet")], [InlineKeyboardButton("🔙 Back", callback_data="shop")]]
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return

    c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (product['product_id'],))
    if c.fetchone()[0] < qty:
        conn.close()
        await query.edit_message_text("❌ Not enough stock left.")
        return

    order_id = generate_order_id()
    c.execute("UPDATE users SET balance = balance - ?, total_spent = total_spent + ?, total_orders = total_orders + 1 WHERE user_id = ?", (total, total, user_id))
    c.execute("INSERT INTO orders (order_id, user_id, product_id, quantity, total_amount, payment_method, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              (order_id, user_id, product['product_id'], qty, total, "wallet", "DELIVERED", get_current_time()))
    c.execute("SELECT stock_id, content FROM stock WHERE product_id = ? AND status = 'AVAILABLE' LIMIT ?", (product['product_id'], qty))
    stocks = c.fetchall()
    delivered_items = []
    for stock in stocks:
        c.execute("UPDATE stock SET status = 'DELIVERED', reserved_by = ?, delivered_at = ?, order_id = ? WHERE stock_id = ?",
                  (user_id, get_current_time(), order_id, stock['stock_id']))
        delivered_items.append(stock['content'])
    conn.commit()
    conn.close()

    if "YouTube" in product['name']:
        await send_youtube_instruction(context, order_id, user_id)
        await query.edit_message_text("✅ <b>Payment Successful!</b>\n\nPlease check the message below.", parse_mode=ParseMode.HTML)
    else:
        delivery_text = (
            f"✅ <b>Payment Successful!</b>\n\n"
            f"🆔 Order ID: <code>{order_id}</code>\n"
            f"{product['emoji']} <b>{product['name']}</b>\n"
            f"📦 Quantity: {qty}\n"
            f"💰 Paid: {format_money(total)} (from Wallet)\n\n"
            f"<b>Your Product(s):</b>\n\n"
        )
        for i, item in enumerate(delivered_items, 1):
            delivery_text += f"<b>#{i}</b>\n<code>{item}</code>\n\n"
        delivery_text += "⚠️ Keep this information safe!"
        keyboard = [[InlineKeyboardButton("📦 My Orders", callback_data="my_orders")], [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]
        await query.edit_message_text(text=delivery_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

    await send_purchase_announcement(context, product['name'], qty, product['emoji'])
    context.user_data.clear()


async def pay_bep20(update, context):
    query = update.callback_query
    await query.answer()
    product = context.user_data.get('selected_product')
    qty = context.user_data.get('quantity')
    total = context.user_data.get('total_amount')
    if not product or not qty or total is None:
        await query.edit_message_text("❌ Session expired.")
        return
    order_id = generate_order_id()
    context.user_data['pending_order_id'] = order_id
    context.user_data['payment_method'] = "bep20"
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO orders (order_id, user_id, product_id, quantity, total_amount, payment_method, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              (order_id, query.from_user.id, product['product_id'], qty, total, "bep20", "PENDING", get_current_time()))
    conn.commit()
    conn.close()
    text = (
        f"🪙 <b>BEP20 Payment</b>\n\n"
        f"🆔 Order ID: <code>{order_id}</code>\n"
        f"{product['emoji']} {product['name']} × {qty}\n"
        f"💰 Amount: <b>{format_money(total)} USDT</b>\n\n"
        f"<b>Send exactly to:</b>\n<code>{BEP20_ADDRESS}</code>\n\n"
        f"Network: <b>BEP20 (BSC)</b>\n\n"
        f"After payment, click below and submit TXID.\nAuto-verify enabled for valid deposits."
    )
    keyboard = [
        [InlineKeyboardButton("✅ I Have Paid – Submit TXID", callback_data="submit_txid")],
        [InlineKeyboardButton("❌ Cancel Order", callback_data=f"cancel_order_{order_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="shop")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def pay_binance(update, context):
    query = update.callback_query
    await query.answer()
    product = context.user_data.get('selected_product')
    qty = context.user_data.get('quantity')
    total = context.user_data.get('total_amount')
    if not product or not qty or total is None:
        await query.edit_message_text("❌ Session expired.")
        return
    order_id = generate_order_id()
    context.user_data['pending_order_id'] = order_id
    context.user_data['payment_method'] = "binance"
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO orders (order_id, user_id, product_id, quantity, total_amount, payment_method, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              (order_id, query.from_user.id, product['product_id'], qty, total, "binance", "PENDING", get_current_time()))
    conn.commit()
    conn.close()
    text = (
        f"🔵 <b>Binance Pay</b>\n\n"
        f"🆔 Order ID: <code>{order_id}</code>\n"
        f"{product['emoji']} {product['name']} × {qty}\n"
        f"💰 Amount: <b>{format_money(total)}</b>\n\n"
        f"<b>Pay to Binance ID:</b>\n<code>{BINANCE_PAY_ID}</code>\n\n"
        f"After payment, submit your Payment Reference.\nIf auto-check fails, admin will approve."
    )
    keyboard = [
        [InlineKeyboardButton("✅ I Have Paid – Submit Reference", callback_data="submit_binance_ref")],
        [InlineKeyboardButton("❌ Cancel Order", callback_data=f"cancel_order_{order_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="shop")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def ask_txid(update, context):
    query = update.callback_query
    await query.answer()
    order_id = context.user_data.get('pending_order_id')
    if not order_id:
        await query.edit_message_text("❌ Session expired. Please start order again from Shop.")
        return
    context.user_data['waiting_for'] = "txid"
    text = f"📝 <b>Submit TXID</b>\n\n🆔 Order ID: <code>{order_id}</code>\n\nSend your BEP20 Transaction ID now."
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_order_{order_id}")]]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def ask_binance_ref(update, context):
    query = update.callback_query
    await query.answer()
    order_id = context.user_data.get('pending_order_id')
    if not order_id:
        await query.edit_message_text("❌ Session expired. Please start order again from Shop.")
        return
    context.user_data['waiting_for'] = "binance_ref"
    text = f"📝 <b>Submit Binance Reference</b>\n\n🆔 Order ID: <code>{order_id}</code>\n\nSend your Binance Payment Reference now."
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_order_{order_id}")]]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def handle_payment_proof(update, context):
    if not update.message or not update.message.text:
        return
    waiting_for = context.user_data.get('waiting_for')
    order_id = context.user_data.get('pending_order_id')
    if waiting_for not in ["txid", "binance_ref"] or not order_id:
        return

    user_id = update.effective_user.id
    proof = update.message.text.strip()
    if len(proof) < 8:
        await update.message.reply_text("❌ Invalid proof. Send a valid TXID or reference.")
        return
    if is_txid_already_used(proof):
        await update.message.reply_text("❌ <b>This TXID / Reference was already used.</b>", parse_mode=ParseMode.HTML)
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE order_id = ? AND user_id = ? AND status = 'PENDING'", (order_id, user_id))
    order = c.fetchone()
    if not order:
        conn.close()
        await update.message.reply_text("❌ Order not found or already processed.")
        context.user_data.clear()
        return

    if waiting_for == "txid":
        c.execute("UPDATE orders SET txid = ? WHERE order_id = ?", (proof, order_id))
        proof_type = "TXID"
    else:
        c.execute("UPDATE orders SET binance_ref = ? WHERE order_id = ?", (proof, order_id))
        proof_type = "Binance Reference"
    conn.commit()

    expected_amount = float(order['total_amount'])
    verification = await check_binance_deposit(proof, expected_amount)

    if verification.get("found"):
        product_id = order['product_id']
        qty = order['quantity']
        c.execute("SELECT stock_id, content FROM stock WHERE product_id = ? AND status = 'AVAILABLE' LIMIT ?", (product_id, qty))
        stocks = c.fetchall()
        if len(stocks) < qty:
            conn.close()
            await update.message.reply_text("✅ Payment detected, but stock is low. Admin will deliver shortly.")
            try:
                await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Auto-paid but low stock\n🆔 <code>{order_id}</code>\n📄 <code>{proof}</code>", parse_mode=ParseMode.HTML)
            except Exception:
                pass
            context.user_data.clear()
            return

        c.execute("UPDATE orders SET status = 'DELIVERED', approved_at = ?, delivered_at = ? WHERE order_id = ?", (get_current_time(), get_current_time(), order_id))
        c.execute("UPDATE users SET total_spent = total_spent + ?, total_orders = total_orders + 1 WHERE user_id = ?", (expected_amount, user_id))
        delivered_items = []
        for stock in stocks:
            c.execute("UPDATE stock SET status = 'DELIVERED', reserved_by = ?, delivered_at = ?, order_id = ? WHERE stock_id = ?",
                      (user_id, get_current_time(), order_id, stock['stock_id']))
            delivered_items.append(stock['content'])
        c.execute("SELECT name, emoji FROM products WHERE product_id = ?", (product_id,))
        product = c.fetchone()
        conn.commit()
        conn.close()

        if "YouTube" in product['name']:
            await send_youtube_instruction(context, order_id, user_id)
            await update.message.reply_text(f"✅ <b>Payment Automatically Verified!</b>\n\n🆔 <code>{order_id}</code>\nCheck next message.", parse_mode=ParseMode.HTML)
        else:
            delivery_text = (
                f"✅ <b>Payment Automatically Verified!</b>\n\n"
                f"🆔 Order ID: <code>{order_id}</code>\n"
                f"{product['emoji']} <b>{product['name']}</b>\n"
                f"📦 Quantity: {qty}\n\n<b>Your Product(s):</b>\n\n"
            )
            for i, item in enumerate(delivered_items, 1):
                delivery_text += f"<b>#{i}</b>\n<code>{item}</code>\n\n"
            delivery_text += "⚠️ Keep this information safe!"
            await update.message.reply_text(delivery_text, parse_mode=ParseMode.HTML)

        await send_purchase_announcement(context, product['name'], qty, product['emoji'])
        context.user_data.clear()
        return

    conn.close()
    await update.message.reply_text(
        f"⏳ <b>{proof_type} Submitted</b>\n\n"
        f"🆔 Order ID: <code>{order_id}</code>\n"
        f"📄 {proof_type}: <code>{proof}</code>\n\n"
        f"Auto-check: not confirmed yet.\nStatus: <b>Pending Admin Approval</b>",
        parse_mode=ParseMode.HTML
    )
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🔔 <b>New Payment Pending</b>\n\n🆔 <code>{order_id}</code>\n👤 <code>{user_id}</code>\n💰 {format_money(order['total_amount'])}\n📄 {proof_type}: <code>{proof}</code>\n🔍 {verification.get('message', 'n/a')}",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass
    context.user_data.clear()


async def cancel_order(update, context):
    query = update.callback_query
    await query.answer()
    order_id = query.data.split("_")[-1]
    user_id = query.from_user.id
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE order_id = ? AND user_id = ? AND status = 'PENDING'", (order_id, user_id))
    if not c.fetchone():
        conn.close()
        await query.edit_message_text("❌ Order not found or cannot be cancelled.")
        return
    c.execute("UPDATE orders SET status = 'CANCELLED' WHERE order_id = ?", (order_id,))
    conn.commit()
    conn.close()
    context.user_data.clear()
    await query.edit_message_text(f"❌ <b>Order Cancelled</b>\n\n🆔 <code>{order_id}</code>", parse_mode=ParseMode.HTML)


async def wallet_handler(update, context):
    query = update.callback_query
    await query.answer()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id = ?", (query.from_user.id,))
    row = c.fetchone()
    balance = row['balance'] if row else 0.0
    conn.close()
    text = f"💰 <b>Your Wallet</b>\n\nCurrent Balance: <b>{format_money(balance)}</b>"
    keyboard = [[InlineKeyboardButton("➕ Deposit", callback_data="deposit")], [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def deposit_start(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data['waiting_for'] = "deposit_amount"
    text = "➕ <b>Deposit Funds</b>\n\nEnter amount (example: 5 or 10.5)"
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wallet")]]), parse_mode=ParseMode.HTML)


async def handle_deposit_amount(update, context):
    if not update.message or not update.message.text:
        return
    if context.user_data.get('waiting_for') != "deposit_amount":
        return
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("❌ Invalid amount.")
        return
    context.user_data['deposit_amount'] = amount
    context.user_data['waiting_for'] = None
    text = f"💰 Deposit Amount: <b>{format_money(amount)}</b>\n\nSelect method:"
    keyboard = [
        [InlineKeyboardButton("🪙 BEP20", callback_data="deposit_bep20")],
        [InlineKeyboardButton("🔵 Binance Pay", callback_data="deposit_binance")],
        [InlineKeyboardButton("❌ Cancel", callback_data="wallet")]
    ]
    await update.message.reply_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def deposit_bep20(update, context):
    query = update.callback_query
    await query.answer()
    amount = context.user_data.get('deposit_amount')
    if not amount:
        await query.edit_message_text("❌ Session expired.")
        return
    deposit_id = generate_deposit_id()
    context.user_data['pending_deposit_id'] = deposit_id
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO wallet_deposits (deposit_id, user_id, amount, payment_method, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
              (deposit_id, query.from_user.id, amount, "bep20", "PENDING", get_current_time()))
    conn.commit()
    conn.close()
    text = (
        f"🪙 <b>BEP20 Deposit</b>\n\n"
        f"🆔 Deposit ID: <code>{deposit_id}</code>\n"
        f"💰 Amount: <b>{format_money(amount)} USDT</b>\n\n"
        f"<code>{BEP20_ADDRESS}</code>\n\nAfter payment, submit TXID."
    )
    keyboard = [[InlineKeyboardButton("✅ Submit TXID", callback_data="deposit_submit_txid")], [InlineKeyboardButton("❌ Cancel", callback_data="wallet")]]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def deposit_binance(update, context):
    query = update.callback_query
    await query.answer()
    amount = context.user_data.get('deposit_amount')
    if not amount:
        await query.edit_message_text("❌ Session expired.")
        return
    deposit_id = generate_deposit_id()
    context.user_data['pending_deposit_id'] = deposit_id
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO wallet_deposits (deposit_id, user_id, amount, payment_method, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
              (deposit_id, query.from_user.id, amount, "binance", "PENDING", get_current_time()))
    conn.commit()
    conn.close()
    text = (
        f"🔵 <b>Binance Deposit</b>\n\n"
        f"🆔 Deposit ID: <code>{deposit_id}</code>\n"
        f"💰 Amount: <b>{format_money(amount)}</b>\n\n"
        f"Pay to: <code>{BINANCE_PAY_ID}</code>\n\nAfter payment, submit Reference."
    )
    keyboard = [[InlineKeyboardButton("✅ Submit Reference", callback_data="deposit_submit_binance")], [InlineKeyboardButton("❌ Cancel", callback_data="wallet")]]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def deposit_ask_txid(update, context):
    query = update.callback_query
    await query.answer()
    deposit_id = context.user_data.get('pending_deposit_id')
    if not deposit_id:
        await query.edit_message_text("❌ Session expired.")
        return
    context.user_data['waiting_for'] = "deposit_txid"
    await query.edit_message_text(f"📝 Submit TXID\n\n🆔 Deposit ID: <code>{deposit_id}</code>\n\nSend your TXID now.", parse_mode=ParseMode.HTML)


async def deposit_ask_binance(update, context):
    query = update.callback_query
    await query.answer()
    deposit_id = context.user_data.get('pending_deposit_id')
    if not deposit_id:
        await query.edit_message_text("❌ Session expired.")
        return
    context.user_data['waiting_for'] = "deposit_binance_ref"
    await query.edit_message_text(f"📝 Submit Binance Reference\n\n🆔 Deposit ID: <code>{deposit_id}</code>\n\nSend your reference now.", parse_mode=ParseMode.HTML)


async def handle_deposit_proof(update, context):
    if not update.message or not update.message.text:
        return
    waiting_for = context.user_data.get('waiting_for')
    deposit_id = context.user_data.get('pending_deposit_id')
    if waiting_for not in ["deposit_txid", "deposit_binance_ref"] or not deposit_id:
        return

    proof = update.message.text.strip()
    user_id = update.effective_user.id
    if len(proof) < 8:
        await update.message.reply_text("❌ Invalid proof.")
        return
    if is_txid_already_used(proof):
        await update.message.reply_text("❌ <b>This TXID / Reference was already used.</b>", parse_mode=ParseMode.HTML)
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM wallet_deposits WHERE deposit_id = ? AND user_id = ? AND status = 'PENDING'", (deposit_id, user_id))
    deposit = c.fetchone()
    if not deposit:
        conn.close()
        await update.message.reply_text("❌ Deposit not found.")
        context.user_data.clear()
        return

    if waiting_for == "deposit_txid":
        c.execute("UPDATE wallet_deposits SET txid = ? WHERE deposit_id = ?", (proof, deposit_id))
        proof_type = "TXID"
    else:
        c.execute("UPDATE wallet_deposits SET binance_ref = ? WHERE deposit_id = ?", (proof, deposit_id))
        proof_type = "Binance Reference"
    conn.commit()

    expected_amount = float(deposit['amount'])
    verification = await check_binance_deposit(proof, expected_amount)

    if verification.get("found"):
        c.execute("UPDATE users SET balance = COALESCE(balance, 0) + ? WHERE user_id = ?", (expected_amount, user_id))
        c.execute("UPDATE wallet_deposits SET status = 'APPROVED', approved_at = ? WHERE deposit_id = ?", (get_current_time(), deposit_id))
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        new_balance = c.fetchone()['balance']
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ <b>Deposit Automatically Verified!</b>\n\n"
            f"🆔 Deposit ID: <code>{deposit_id}</code>\n"
            f"💰 Amount: <b>{format_money(expected_amount)}</b>\n"
            f"📄 Proof: <code>{proof}</code>\n\n"
            f"New Wallet Balance: <b>{format_money(new_balance)}</b>",
            parse_mode=ParseMode.HTML
        )
        context.user_data.clear()
        return

    conn.close()
    await update.message.reply_text(
        f"⏳ <b>{proof_type} submitted!</b>\n\n"
        f"🆔 Deposit ID: <code>{deposit_id}</code>\n"
        f"Auto-check: not confirmed yet.\nWaiting for admin approval.",
        parse_mode=ParseMode.HTML
    )
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"💵 New Wallet Deposit\n🆔 <code>{deposit_id}</code>\n💰 {format_money(deposit['amount'])}\n📄 {proof_type}: <code>{proof}</code>\n🔍 {verification.get('message', 'n/a')}",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass
    context.user_data.clear()


async def my_orders_handler(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        SELECT o.*, p.name as product_name, p.emoji
        FROM orders o JOIN products p ON o.product_id = p.product_id
        WHERE o.user_id = ? ORDER BY o.created_at DESC LIMIT 15
    ''', (user_id,))
    orders = c.fetchall()
    if not orders:
        conn.close()
        await query.edit_message_text("📦 <b>My Orders</b>\n\nYou have no orders yet.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]),
                                      parse_mode=ParseMode.HTML)
        return

    text = "📦 <b>My Orders</b>\n\n"
    for order in orders:
        status_emoji = {"PENDING": "⏳", "APPROVED": "✅", "DELIVERED": "✅", "REJECTED": "❌", "CANCELLED": "🚫"}.get(order['status'], "❓")
        text += (
            f"{status_emoji} <b>{order['order_id']}</b>\n"
            f"{order['emoji']} {order['product_name']} × {order['quantity']}\n"
            f"💰 {format_money(order['total_amount'])} | {order['payment_method'].upper()}\n"
            f"📅 {order['created_at']}\nStatus: <b>{order['status']}</b>\n"
        )
        if order['status'] in ["APPROVED", "DELIVERED"]:
            c.execute("SELECT content FROM stock WHERE order_id = ? AND status = 'DELIVERED'", (order['order_id'],))
            items = c.fetchall()
            if items:
                text += "🔑 <b>Credentials:</b>\n"
                for item in items:
                    text += f"<code>{item['content']}</code>\n"
            elif "YouTube" in order['product_name']:
                text += "▶️ Gmail via @veltrixstoreyoutube_bot\n"
        text += "\n" + "─" * 20 + "\n\n"
    conn.close()
    if len(text) > 4000:
        text = text[:3900] + "\n\n... (recent orders)"
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]]), parse_mode=ParseMode.HTML)
# ====================== ADMIN ======================
async def admin_command(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Access denied.")
        return
    await show_admin_panel(update, context)


async def show_admin_panel(update, context):
    text = f"🛡️ <b>{STORE_NAME} – Admin Panel</b>\n\nChoose an option:"
    keyboard = [
        [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Manage Users", callback_data="admin_users")],
        [InlineKeyboardButton("📦 Stock Management", callback_data="admin_stock_menu")],
        [InlineKeyboardButton("➕ Add New Product", callback_data="admin_add_product")],
        [InlineKeyboardButton("🗑️ Remove Product", callback_data="admin_remove_product")],
        [InlineKeyboardButton("💳 Direct Payments", callback_data="admin_direct_payments")],
        [InlineKeyboardButton("💵 Wallet Deposits", callback_data="admin_wallet_deposits")],
        [InlineKeyboardButton("📋 Order History", callback_data="admin_order_history")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔙 Close", callback_data="admin_close")]
    ]
    if update.callback_query:
        await update.callback_query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        await update.callback_query.answer()
    else:
        await update.message.reply_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_panel_callback(update, context):
    await update.callback_query.answer()
    if not is_admin(update.callback_query.from_user.id):
        return
    await show_admin_panel(update, context)


async def admin_close(update, context):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("🛡️ Admin panel closed.")


async def admin_users(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, full_name, username, balance, total_spent, total_orders FROM users ORDER BY total_spent DESC LIMIT 30")
    users = c.fetchall()
    conn.close()
    if not users:
        await query.edit_message_text("👥 <b>Manage Users</b>\n\nNo users found.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
                                      parse_mode=ParseMode.HTML)
        return
    text = "👥 <b>Manage Users</b> (Top 30)\n\nSelect a user:"
    keyboard = [[InlineKeyboardButton("🔍 Search User", callback_data="admin_search_user")]]
    for u in users:
        name = u['full_name'] or "No Name"
        uname = f"@{u['username']}" if u['username'] else "No Username"
        keyboard.append([InlineKeyboardButton(f"{name} | {uname} | 💰{format_money(u['balance'])}"[:60], callback_data=f"view_user_{u['user_id']}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def view_user_profile(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    try:
        user_id = int(query.data.replace("view_user_", ""))
    except Exception:
        await query.answer("Invalid user", show_alert=True)
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = c.fetchone()
    conn.close()
    if not user:
        await query.answer("User not found", show_alert=True)
        return
    banned = "Yes 🚫" if user['is_banned'] else "No"
    text = (
        f"👤 <b>User Profile</b>\n\n"
        f"🆔 <code>{user['user_id']}</code>\n"
        f"👤 {user['full_name'] or 'N/A'}\n"
        f"🔗 @{user['username'] or 'N/A'}\n"
        f"💰 Balance: <b>{format_money(user['balance'])}</b>\n"
        f"💸 Spent: <b>{format_money(user['total_spent'])}</b>\n"
        f"📦 Orders: <b>{user['total_orders']}</b>\n"
        f"🚫 Banned: <b>{banned}</b>"
    )
    keyboard = [
        [InlineKeyboardButton("➕ Add Balance", callback_data=f"add_balance_{user_id}"),
         InlineKeyboardButton("➖ Remove Balance", callback_data=f"remove_balance_{user_id}")],
        [InlineKeyboardButton("📦 Order History", callback_data=f"user_orders_{user_id}"),
         InlineKeyboardButton("🚫 Ban", callback_data=f"ban_user_{user_id}")],
        [InlineKeyboardButton("✅ Unban", callback_data=f"unban_user_{user_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_users")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def user_order_history(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    user_id = int(query.data.replace("user_orders_", ""))
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT o.order_id, o.quantity, o.total_amount, o.status, o.created_at, p.name, p.emoji
                 FROM orders o JOIN products p ON o.product_id = p.product_id
                 WHERE o.user_id = ? ORDER BY o.created_at DESC LIMIT 15''', (user_id,))
    orders = c.fetchall()
    conn.close()
    text = f"📦 <b>Order History</b>\nUser: <code>{user_id}</code>\n\n"
    if not orders:
        text += "No orders."
    else:
        for o in orders:
            text += f"• <code>{o['order_id']}</code>\n  {o['emoji']} {o['name']} × {o['quantity']}\n  💰 {format_money(o['total_amount'])} | {o['status']}\n  📅 {o['created_at']}\n\n"
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"view_user_{user_id}")]]), parse_mode=ParseMode.HTML)


async def ban_user(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    user_id = int(query.data.replace("ban_user_", ""))
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    await query.answer("🚫 Banned!", show_alert=True)
    query.data = f"view_user_{user_id}"
    await view_user_profile(update, context)


async def unban_user(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    user_id = int(query.data.replace("unban_user_", ""))
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    await query.answer("✅ Unbanned!", show_alert=True)
    query.data = f"view_user_{user_id}"
    await view_user_profile(update, context)


async def admin_search_user(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data['waiting_for'] = "admin_search_user"
    await query.edit_message_text("🔍 <b>Search User</b>\n\nSend User ID or Username (without @):", parse_mode=ParseMode.HTML)


async def handle_admin_search(update, context):
    if context.user_data.get('waiting_for') != "admin_search_user":
        return
    if not is_admin(update.effective_user.id):
        return
    text = update.message.text.strip().lstrip("@")
    conn = get_db_connection()
    c = conn.cursor()
    if text.isdigit():
        c.execute("SELECT * FROM users WHERE user_id = ?", (int(text),))
    else:
        c.execute("SELECT * FROM users WHERE username = ?", (text,))
    user = c.fetchone()
    conn.close()
    context.user_data.clear()
    if not user:
        await update.message.reply_text("❌ User not found.")
        return
    banned = "Yes 🚫" if user['is_banned'] else "No"
    uid = user['user_id']
    profile = (
        f"👤 <b>User Profile</b>\n\n🆔 <code>{uid}</code>\n👤 {user['full_name'] or 'N/A'}\n"
        f"🔗 @{user['username'] or 'N/A'}\n💰 <b>{format_money(user['balance'])}</b>\n"
        f"💸 <b>{format_money(user['total_spent'])}</b>\n📦 <b>{user['total_orders']}</b>\n🚫 {banned}"
    )
    keyboard = [
        [InlineKeyboardButton("➕ Add Balance", callback_data=f"add_balance_{uid}"),
         InlineKeyboardButton("➖ Remove Balance", callback_data=f"remove_balance_{uid}")],
        [InlineKeyboardButton("📦 Orders", callback_data=f"user_orders_{uid}"),
         InlineKeyboardButton("🚫 Ban", callback_data=f"ban_user_{uid}")],
        [InlineKeyboardButton("✅ Unban", callback_data=f"unban_user_{uid}")],
        [InlineKeyboardButton("🔙 Users", callback_data="admin_users")]
    ]
    await update.message.reply_text(profile, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def add_balance_start(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    user_id = int(query.data.replace("add_balance_", ""))
    context.user_data['admin_action'] = "add_balance"
    context.user_data['target_user_id'] = user_id
    context.user_data['waiting_for'] = "admin_balance_amount"
    await query.edit_message_text(f"➕ <b>Add Balance</b>\n\nUser: <code>{user_id}</code>\n\nSend amount:", parse_mode=ParseMode.HTML)


async def remove_balance_start(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    user_id = int(query.data.replace("remove_balance_", ""))
    context.user_data['admin_action'] = "remove_balance"
    context.user_data['target_user_id'] = user_id
    context.user_data['waiting_for'] = "admin_balance_amount"
    await query.edit_message_text(f"➖ <b>Remove Balance</b>\n\nUser: <code>{user_id}</code>\n\nSend amount:", parse_mode=ParseMode.HTML)


async def handle_admin_balance_amount(update, context):
    if context.user_data.get('waiting_for') != "admin_balance_amount":
        return
    if not is_admin(update.effective_user.id):
        return
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("❌ Send a valid number.")
        return
    user_id = context.user_data.get('target_user_id')
    action = context.user_data.get('admin_action')
    conn = get_db_connection()
    c = conn.cursor()
    if action == "add_balance":
        c.execute("UPDATE users SET balance = COALESCE(balance, 0) + ? WHERE user_id = ?", (amount, user_id))
        msg = f"added <b>{format_money(amount)}</b> to"
    else:
        c.execute("UPDATE users SET balance = MAX(COALESCE(balance, 0) - ?, 0) WHERE user_id = ?", (amount, user_id))
        msg = f"removed <b>{format_money(amount)}</b> from"
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    new_balance = c.fetchone()['balance']
    conn.commit()
    conn.close()
    context.user_data.clear()
    await update.message.reply_text(f"✅ Successfully {msg} user <code>{user_id}</code>\n\nNew Balance: <b>{format_money(new_balance)}</b>", parse_mode=ParseMode.HTML)


async def admin_stats(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users"); total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM orders"); total_orders = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM orders WHERE status = 'PENDING'"); pending = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(total_amount), 0) FROM orders WHERE status IN ('APPROVED', 'DELIVERED')"); revenue = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM stock WHERE status = 'AVAILABLE'"); stock = c.fetchone()[0]
    conn.close()
    text = (
        f"📊 <b>Statistics</b>\n\n"
        f"👥 Users: <b>{total_users}</b>\n📦 Orders: <b>{total_orders}</b>\n"
        f"⏳ Pending: <b>{pending}</b>\n💰 Revenue: <b>{format_money(revenue)}</b>\n"
        f"📦 Stock: <b>{stock}</b>"
    )
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]), parse_mode=ParseMode.HTML)


async def admin_direct_payments(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT o.*, p.name as product_name, p.emoji FROM orders o
                 JOIN products p ON o.product_id = p.product_id
                 WHERE o.status = 'PENDING' AND o.payment_method IN ('bep20', 'binance')
                 ORDER BY o.created_at ASC LIMIT 10''')
    pending = c.fetchall()
    conn.close()
    if not pending:
        await query.edit_message_text("✅ No pending direct payments.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
                                      parse_mode=ParseMode.HTML)
        return
    text = f"💳 <b>Pending Payments</b> ({len(pending)})\n\n"
    keyboard = []
    for order in pending:
        proof = order['txid'] or order['binance_ref'] or "Not submitted"
        text += f"🆔 <code>{order['order_id']}</code>\n{order['emoji']} {order['product_name']} × {order['quantity']}\n💰 {format_money(order['total_amount'])}\n📄 <code>{proof}</code>\n\n"
        keyboard.append([
            InlineKeyboardButton(f"✅ {order['order_id'][-6:]}", callback_data=f"approve_order_{order['order_id']}"),
            InlineKeyboardButton(f"❌ {order['order_id'][-6:]}", callback_data=f"reject_order_{order['order_id']}")
        ])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def approve_order(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    order_id = query.data.replace("approve_order_", "")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE order_id = ? AND status = 'PENDING'", (order_id,))
    order = c.fetchone()
    if not order:
        conn.close()
        await query.answer("Already processed.", show_alert=True)
        return
    product_id, qty, user_id = order['product_id'], order['quantity'], order['user_id']
    c.execute("SELECT stock_id, content FROM stock WHERE product_id = ? AND status = 'AVAILABLE' LIMIT ?", (product_id, qty))
    stocks = c.fetchall()
    if len(stocks) < qty:
        conn.close()
        await query.answer("Not enough stock!", show_alert=True)
        return
    c.execute("UPDATE orders SET status = 'DELIVERED', approved_at = ?, delivered_at = ? WHERE order_id = ?", (get_current_time(), get_current_time(), order_id))
    c.execute("UPDATE users SET total_spent = total_spent + ?, total_orders = total_orders + 1 WHERE user_id = ?", (order['total_amount'], user_id))
    delivered_items = []
    for stock in stocks:
        c.execute("UPDATE stock SET status = 'DELIVERED', reserved_by = ?, delivered_at = ?, order_id = ? WHERE stock_id = ?",
                  (user_id, get_current_time(), order_id, stock['stock_id']))
        delivered_items.append(stock['content'])
    c.execute("SELECT name, emoji FROM products WHERE product_id = ?", (product_id,))
    product = c.fetchone()
    conn.commit()
    conn.close()
    if "YouTube" in product['name']:
        await send_youtube_instruction(context, order_id, user_id)
    else:
        delivery_text = f"✅ <b>Payment Approved!</b>\n\n🆔 <code>{order_id}</code>\n{product['emoji']} <b>{product['name']}</b>\n📦 {qty}\n\n<b>Your Product(s):</b>\n\n"
        for i, item in enumerate(delivered_items, 1):
            delivery_text += f"<b>#{i}</b>\n<code>{item}</code>\n\n"
        try:
            await context.bot.send_message(chat_id=user_id, text=delivery_text, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    await send_purchase_announcement(context, product['name'], qty, product['emoji'])
    await query.answer("✅ Approved!", show_alert=True)
    await admin_direct_payments(update, context)


async def reject_order(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    order_id = query.data.replace("reject_order_", "")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM orders WHERE order_id = ? AND status = 'PENDING'", (order_id,))
    order = c.fetchone()
    if not order:
        conn.close()
        await query.answer("Already processed.", show_alert=True)
        return
    c.execute("UPDATE orders SET status = 'REJECTED' WHERE order_id = ?", (order_id,))
    conn.commit()
    conn.close()
    try:
        await context.bot.send_message(chat_id=order['user_id'], text=f"❌ Payment Rejected\n🆔 <code>{order_id}</code>\nSupport: {SUPPORT_USERNAME}", parse_mode=ParseMode.HTML)
    except Exception:
        pass
    await query.answer("❌ Rejected", show_alert=True)
    await admin_direct_payments(update, context)


async def admin_wallet_deposits(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM wallet_deposits WHERE status = 'PENDING' ORDER BY created_at ASC LIMIT 10")
    pending = c.fetchall()
    conn.close()
    if not pending:
        await query.edit_message_text("✅ No pending deposits.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
                                      parse_mode=ParseMode.HTML)
        return
    text = "💵 <b>Pending Deposits</b>\n\n"
    keyboard = []
    for dep in pending:
        proof = dep['txid'] or dep['binance_ref'] or "Not submitted"
        text += f"🆔 <code>{dep['deposit_id']}</code>\n💰 {format_money(dep['amount'])}\n📄 <code>{proof}</code>\n\n"
        keyboard.append([
            InlineKeyboardButton(f"✅ {dep['deposit_id'][-6:]}", callback_data=f"approve_deposit_{dep['deposit_id']}"),
            InlineKeyboardButton(f"❌ {dep['deposit_id'][-6:]}", callback_data=f"reject_deposit_{dep['deposit_id']}")
        ])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def approve_deposit(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    deposit_id = query.data.replace("approve_deposit_", "")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM wallet_deposits WHERE deposit_id = ? AND status = 'PENDING'", (deposit_id,))
    deposit = c.fetchone()
    if not deposit:
        conn.close()
        await query.answer("Already processed.", show_alert=True)
        return
    user_id, amount = deposit["user_id"], float(deposit["amount"])
    c.execute("UPDATE users SET balance = COALESCE(balance, 0) + ? WHERE user_id = ?", (amount, user_id))
    c.execute("UPDATE wallet_deposits SET status = 'APPROVED', approved_at = ? WHERE deposit_id = ?", (get_current_time(), deposit_id))
    conn.commit()
    conn.close()
    try:
        await context.bot.send_message(chat_id=user_id, text=f"✅ <b>Deposit Approved!</b>\n\n🆔 <code>{deposit_id}</code>\n💰 <b>${amount:.2f}</b>", parse_mode=ParseMode.HTML)
    except Exception:
        pass
    await query.answer("✅ Credited!", show_alert=True)
    await admin_wallet_deposits(update, context)


async def reject_deposit(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    deposit_id = query.data.replace("reject_deposit_", "")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM wallet_deposits WHERE deposit_id = ? AND status = 'PENDING'", (deposit_id,))
    deposit = c.fetchone()
    if not deposit:
        conn.close()
        await query.answer("Already processed.", show_alert=True)
        return
    c.execute("UPDATE wallet_deposits SET status = 'REJECTED' WHERE deposit_id = ?", (deposit_id,))
    conn.commit()
    conn.close()
    try:
        await context.bot.send_message(chat_id=deposit['user_id'], text=f"❌ Deposit Rejected\nSupport: {SUPPORT_USERNAME}", parse_mode=ParseMode.HTML)
    except Exception:
        pass
    await query.answer("❌ Rejected", show_alert=True)
    await admin_wallet_deposits(update, context)


async def admin_broadcast(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data['waiting_for'] = "broadcast_message"
    await query.edit_message_text("📢 Send the broadcast message now:")


async def handle_broadcast_message(update, context):
    if context.user_data.get('waiting_for') != "broadcast_message":
        return
    if not is_admin(update.effective_user.id):
        return
    message = update.message.text
    context.user_data.clear()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = c.fetchall()
    conn.close()
    success = 0
    for user in users:
        try:
            await context.bot.send_message(chat_id=user['user_id'], text=message, parse_mode=ParseMode.HTML)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await update.message.reply_text(f"✅ Broadcast sent to {success} users.")


async def message_router(update, context):
    waiting = context.user_data.get('waiting_for')
    if waiting in ["txid", "binance_ref"]:
        await handle_payment_proof(update, context)
    elif waiting == "deposit_amount":
        await handle_deposit_amount(update, context)
    elif waiting in ["deposit_txid", "deposit_binance_ref"]:
        await handle_deposit_proof(update, context)
    elif waiting == "admin_balance_amount":
        await handle_admin_balance_amount(update, context)
    elif waiting == "admin_search_user":
        await handle_admin_search(update, context)
    elif waiting == "broadcast_message":
        await handle_broadcast_message(update, context)
    elif waiting in ["new_product_name", "new_product_duration", "new_product_price", "new_product_emoji"]:
        await handle_new_product(update, context)


async def admin_stock_menu(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    keyboard = [
        [InlineKeyboardButton("📥 Add Stock", callback_data="admin_add_stock")],
        [InlineKeyboardButton("🗑️ Clear Stock", callback_data="admin_clear_stock")],
        [InlineKeyboardButton("📊 View Stock Levels", callback_data="admin_view_stock")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
    ]
    await query.edit_message_text("📦 <b>Stock Management</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_view_stock(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM products ORDER BY product_id")
    products = c.fetchall()
    text = "📊 <b>Current Stock Levels</b>\n\n"
    for p in products:
        c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (p['product_id'],))
        available = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'DELIVERED'", (p['product_id'],))
        delivered = c.fetchone()[0]
        text += f"{p['emoji']} <b>{p['name']}</b>\n   ✅ {available} | 📤 {delivered}\n\n"
    conn.close()
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_stock_menu")]]), parse_mode=ParseMode.HTML)


async def admin_add_stock_start(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM products ORDER BY product_id")
    products = c.fetchall()
    conn.close()
    keyboard = [[InlineKeyboardButton(f"{p['emoji']} {p['name']}", callback_data=f"addstock_{p['product_id']}")] for p in products]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_stock_menu")])
    await query.edit_message_text("📥 <b>Add Stock</b>\n\nSelect product:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_add_stock_product(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    product_id = int(query.data.split("_")[1])
    context.user_data['add_stock_product_id'] = product_id
    context.user_data['waiting_for'] = "stock_file"
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name, emoji FROM products WHERE product_id = ?", (product_id,))
    product = c.fetchone()
    conn.close()
    text = f"📥 <b>Add Stock</b>\n\nProduct: {product['emoji']} <b>{product['name']}</b>\n\nUpload a <b>.txt</b> file (one item per line)."
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_stock_menu")]]), parse_mode=ParseMode.HTML)


async def handle_stock_file(update, context):
    if not update.message or not update.message.document:
        return
    if context.user_data.get('waiting_for') != "stock_file":
        return
    if not is_admin(update.effective_user.id):
        return
    document = update.message.document
    if not document.file_name.lower().endswith(".txt"):
        await update.message.reply_text("❌ Upload a .txt file only.")
        return
    product_id = context.user_data.get('add_stock_product_id')
    if not product_id:
        await update.message.reply_text("❌ Session expired.")
        return
    file = await context.bot.get_file(document.file_id)
    content = (await file.download_as_bytearray()).decode("utf-8", errors="ignore")
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        await update.message.reply_text("❌ File is empty.")
        return
    conn = get_db_connection()
    c = conn.cursor()
    added = 0
    for line in lines:
        c.execute("INSERT INTO stock (product_id, content, status) VALUES (?, ?, 'AVAILABLE')", (product_id, line))
        added += 1
    c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (product_id,))
    total_available = c.fetchone()[0]
    c.execute("SELECT * FROM products WHERE product_id = ?", (product_id,))
    product = c.fetchone()
    conn.commit()
    conn.close()
    context.user_data.clear()
    await update.message.reply_text(
        f"✅ <b>Stock Added!</b>\n\n{product['emoji']} <b>{product['name']}</b>\n➕ Added: <b>{added}</b>\n📦 Available: <b>{total_available}</b>",
        parse_mode=ParseMode.HTML
    )
    await send_restock_announcement(context, dict(product), added, total_available)


async def admin_clear_stock_start(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM products ORDER BY product_id")
    products = c.fetchall()
    conn.close()
    keyboard = [[InlineKeyboardButton(f"{p['emoji']} {p['name']}", callback_data=f"clearstock_{p['product_id']}")] for p in products]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_stock_menu")])
    await query.edit_message_text("🗑️ <b>Clear Stock</b>\n\nSelect product:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_clear_stock_confirm(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    product_id = int(query.data.split("_")[1])
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name, emoji FROM products WHERE product_id = ?", (product_id,))
    product = c.fetchone()
    c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (product_id,))
    count = c.fetchone()[0]
    conn.close()
    text = f"⚠️ <b>Confirm Clear</b>\n\n{product['emoji']} <b>{product['name']}</b>\nWill delete: <b>{count}</b> available items"
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Clear", callback_data=f"confirm_clear_{product_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_stock_menu")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_clear_stock_execute(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    product_id = int(query.data.split("_")[-1])
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (product_id,))
    deleted = c.rowcount
    c.execute("SELECT name, emoji FROM products WHERE product_id = ?", (product_id,))
    product = c.fetchone()
    conn.commit()
    conn.close()
    await query.edit_message_text(f"🗑️ Cleared <b>{deleted}</b> from {product['emoji']} <b>{product['name']}</b>", parse_mode=ParseMode.HTML)


async def admin_add_product_start(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data['waiting_for'] = "new_product_name"
    await query.edit_message_text("➕ <b>Add New Product</b>\n\nSend <b>Product Name</b>\nExample: <code>ChatGPT Plus</code>",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]),
                                  parse_mode=ParseMode.HTML)


async def handle_new_product(update, context):
    if not update.message or not update.message.text or not is_admin(update.effective_user.id):
        return
    waiting = context.user_data.get('waiting_for')
    text = update.message.text.strip()
    if waiting == "new_product_name":
        context.user_data['new_product_name'] = text
        context.user_data['waiting_for'] = "new_product_duration"
        await update.message.reply_text("⏱ Send <b>Duration</b>\nExample: <code>1 Month</code>", parse_mode=ParseMode.HTML)
    elif waiting == "new_product_duration":
        context.user_data['new_product_duration'] = text
        context.user_data['waiting_for'] = "new_product_price"
        await update.message.reply_text("💰 Send <b>Price</b>\nExample: <code>2.50</code>", parse_mode=ParseMode.HTML)
    elif waiting == "new_product_price":
        try:
            price = float(text)
            if price <= 0:
                raise ValueError
        except Exception:
            await update.message.reply_text("❌ Invalid price.")
            return
        context.user_data['new_product_price'] = price
        context.user_data['waiting_for'] = "new_product_emoji"
        await update.message.reply_text("✨ Send an <b>Emoji</b>\nExample: <code>🤖</code>", parse_mode=ParseMode.HTML)
    elif waiting == "new_product_emoji":
        name = context.user_data.get('new_product_name')
        duration = context.user_data.get('new_product_duration')
        price = context.user_data.get('new_product_price')
        emoji = text
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO products (name, duration, price, emoji, is_active) VALUES (?, ?, ?, ?, 1)", (name, duration, price, emoji))
        product_id = c.lastrowid
        conn.commit()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ <b>Product Added!</b>\n\n{emoji} <b>{name}</b>\n⏱ {duration}\n💰 {format_money(price)}\n\nAnnounced in channel.",
            parse_mode=ParseMode.HTML
        )
        await send_new_product_announcement(context, name, duration, price, emoji, product_id)


async def admin_remove_product_start(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM products ORDER BY product_id")
    products = c.fetchall()
    conn.close()
    if not products:
        await query.edit_message_text("❌ No products.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]), parse_mode=ParseMode.HTML)
        return
    keyboard = [[InlineKeyboardButton(f"{p['emoji']} {p['name']} ({p['duration']})", callback_data=f"remove_product_{p['product_id']}")] for p in products]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await query.edit_message_text("🗑️ <b>Remove Product</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_remove_product_confirm(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    product_id = int(query.data.split("_")[-1])
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name, emoji FROM products WHERE product_id = ?", (product_id,))
    product = c.fetchone()
    if not product:
        conn.close()
        await query.edit_message_text("❌ Not found.")
        return
    c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ?", (product_id,))
    stock_count = c.fetchone()[0]
    conn.close()
    text = f"⚠️ Delete {product['emoji']} <b>{product['name']}</b>?\nStock items: <b>{stock_count}</b>"
    keyboard = [
        [InlineKeyboardButton("✅ Delete Permanently", callback_data=f"confirm_remove_{product_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_remove_product")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_remove_product_execute(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    product_id = int(query.data.split("_")[-1])
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name, emoji FROM products WHERE product_id = ?", (product_id,))
    product = c.fetchone()
    if not product:
        conn.close()
        await query.edit_message_text("❌ Not found.")
        return
    c.execute("DELETE FROM stock WHERE product_id = ?", (product_id,))
    c.execute("DELETE FROM products WHERE product_id = ?", (product_id,))
    conn.commit()
    conn.close()
    await query.edit_message_text(f"🗑️ Deleted {product['emoji']} <b>{product['name']}</b>", parse_mode=ParseMode.HTML)


async def admin_order_history(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT o.*, p.name as product_name, p.emoji, u.full_name, u.username
                 FROM orders o JOIN products p ON o.product_id = p.product_id
                 JOIN users u ON o.user_id = u.user_id
                 ORDER BY o.created_at DESC LIMIT 20''')
    orders = c.fetchall()
    conn.close()
    if not orders:
        await query.edit_message_text("📋 No orders.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]), parse_mode=ParseMode.HTML)
        return
    text = "📋 <b>Order History</b> (Last 20)\n\n"
    keyboard = []
    for order in orders:
        se = {"PENDING": "⏳", "APPROVED": "✅", "DELIVERED": "✅", "REJECTED": "❌", "CANCELLED": "🚫"}.get(order['status'], "❓")
        text += f"{se} <code>{order['order_id']}</code>\n{order['emoji']} {order['product_name']} × {order['quantity']}\n💰 {format_money(order['total_amount'])} | {order['status']}\n👤 {order['full_name']}\n\n"
        if order['status'] == "REJECTED":
            keyboard.append([InlineKeyboardButton(f"🔄 Re-Approve {order['order_id'][-6:]}", callback_data=f"reapprove_order_{order['order_id']}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    if len(text) > 3800:
        text = text[:3700] + "\n\n..."
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def reapprove_order(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    order_id = query.data.replace("reapprove_order_", "")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE order_id = ? AND status = 'REJECTED'", (order_id,))
    order = c.fetchone()
    if not order:
        conn.close()
        await query.answer("Not found / not rejected.", show_alert=True)
        return
    product_id, qty, user_id = order['product_id'], order['quantity'], order['user_id']
    c.execute("SELECT stock_id, content FROM stock WHERE product_id = ? AND status = 'AVAILABLE' LIMIT ?", (product_id, qty))
    stocks = c.fetchall()
    if len(stocks) < qty:
        conn.close()
        await query.answer(f"Only {len(stocks)} stock left!", show_alert=True)
        return
    c.execute("UPDATE orders SET status = 'DELIVERED', approved_at = ?, delivered_at = ? WHERE order_id = ?", (get_current_time(), get_current_time(), order_id))
    c.execute("UPDATE users SET total_spent = total_spent + ?, total_orders = total_orders + 1 WHERE user_id = ?", (order['total_amount'], user_id))
    delivered_items = []
    for stock in stocks:
        c.execute("UPDATE stock SET status = 'DELIVERED', reserved_by = ?, delivered_at = ?, order_id = ? WHERE stock_id = ?",
                  (user_id, get_current_time(), order_id, stock['stock_id']))
        delivered_items.append(stock['content'])
    c.execute("SELECT name, emoji FROM products WHERE product_id = ?", (product_id,))
    product = c.fetchone()
    conn.commit()
    conn.close()
    if "YouTube" in product['name']:
        await send_youtube_instruction(context, order_id, user_id)
    else:
        delivery_text = f"✅ <b>Re-Approved & Delivered!</b>\n\n🆔 <code>{order_id}</code>\n{product['emoji']} <b>{product['name']}</b>\n📦 {qty}\n\n"
        for i, item in enumerate(delivered_items, 1):
            delivery_text += f"<b>#{i}</b>\n<code>{item}</code>\n\n"
        try:
            await context.bot.send_message(chat_id=user_id, text=delivery_text, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    await send_purchase_announcement(context, product['name'], qty, product['emoji'])
    await query.answer("✅ Re-approved!", show_alert=True)
    await admin_order_history(update, context)


def main():
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(check_membership_callback, pattern="^check_membership$"))
    application.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"))
    application.add_handler(CallbackQueryHandler(shop_handler, pattern="^shop$"))
    application.add_handler(CallbackQueryHandler(admin_users, pattern="^admin_users$"))
    application.add_handler(CallbackQueryHandler(view_user_profile, pattern="^view_user_"))
    application.add_handler(CallbackQueryHandler(add_balance_start, pattern="^add_balance_"))
    application.add_handler(CallbackQueryHandler(remove_balance_start, pattern="^remove_balance_"))
    application.add_handler(CallbackQueryHandler(user_order_history, pattern="^user_orders_"))
    application.add_handler(CallbackQueryHandler(ban_user, pattern="^ban_user_"))
    application.add_handler(CallbackQueryHandler(unban_user, pattern="^unban_user_"))
    application.add_handler(CallbackQueryHandler(admin_search_user, pattern="^admin_search_user$"))
    application.add_handler(CallbackQueryHandler(profile_handler, pattern="^profile$"))
    application.add_handler(CallbackQueryHandler(wallet_handler, pattern="^wallet$"))
    application.add_handler(CallbackQueryHandler(my_orders_handler, pattern="^my_orders$"))
    application.add_handler(CallbackQueryHandler(support_handler, pattern="^support$"))
    application.add_handler(CallbackQueryHandler(terms_handler, pattern="^terms$"))
    application.add_handler(CallbackQueryHandler(product_selected, pattern="^product_"))
    application.add_handler(CallbackQueryHandler(quantity_selected, pattern="^qty_"))
    application.add_handler(CallbackQueryHandler(pay_from_wallet, pattern="^pay_wallet$"))
    application.add_handler(CallbackQueryHandler(pay_bep20, pattern="^pay_bep20$"))
    application.add_handler(CallbackQueryHandler(pay_binance, pattern="^pay_binance$"))
    application.add_handler(CallbackQueryHandler(ask_txid, pattern="^submit_txid$"))
    application.add_handler(CallbackQueryHandler(ask_binance_ref, pattern="^submit_binance_ref$"))
    application.add_handler(CallbackQueryHandler(cancel_order, pattern="^cancel_order_"))
    application.add_handler(CallbackQueryHandler(deposit_start, pattern="^deposit$"))
    application.add_handler(CallbackQueryHandler(deposit_bep20, pattern="^deposit_bep20$"))
    application.add_handler(CallbackQueryHandler(deposit_binance, pattern="^deposit_binance$"))
    application.add_handler(CallbackQueryHandler(deposit_ask_txid, pattern="^deposit_submit_txid$"))
    application.add_handler(CallbackQueryHandler(deposit_ask_binance, pattern="^deposit_submit_binance$"))
    application.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^admin_panel$"))
    application.add_handler(CallbackQueryHandler(admin_stock_menu, pattern="^admin_stock_menu$"))
    application.add_handler(CallbackQueryHandler(admin_view_stock, pattern="^admin_view_stock$"))
    application.add_handler(CallbackQueryHandler(admin_add_stock_start, pattern="^admin_add_stock$"))
    application.add_handler(CallbackQueryHandler(admin_add_stock_product, pattern="^addstock_"))
    application.add_handler(CallbackQueryHandler(admin_clear_stock_start, pattern="^admin_clear_stock$"))
    application.add_handler(CallbackQueryHandler(admin_add_product_start, pattern="^admin_add_product$"))
    application.add_handler(CallbackQueryHandler(admin_remove_product_start, pattern="^admin_remove_product$"))
    application.add_handler(CallbackQueryHandler(admin_remove_product_confirm, pattern="^remove_product_"))
    application.add_handler(CallbackQueryHandler(admin_remove_product_execute, pattern="^confirm_remove_"))
    application.add_handler(CallbackQueryHandler(admin_clear_stock_confirm, pattern="^clearstock_"))
    application.add_handler(CallbackQueryHandler(admin_clear_stock_execute, pattern="^confirm_clear_"))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_stock_file))
    application.add_handler(CallbackQueryHandler(admin_stats, pattern="^admin_stats$"))
    application.add_handler(CallbackQueryHandler(admin_close, pattern="^admin_close$"))
    application.add_handler(CallbackQueryHandler(admin_direct_payments, pattern="^admin_direct_payments$"))
    application.add_handler(CallbackQueryHandler(approve_order, pattern="^approve_order_"))
    application.add_handler(CallbackQueryHandler(reject_order, pattern="^reject_order_"))
    application.add_handler(CallbackQueryHandler(admin_order_history, pattern="^admin_order_history$"))
    application.add_handler(CallbackQueryHandler(reapprove_order, pattern="^reapprove_order_"))
    application.add_handler(CallbackQueryHandler(admin_wallet_deposits, pattern="^admin_wallet_deposits$"))
    application.add_handler(CallbackQueryHandler(approve_deposit, pattern="^approve_deposit_"))
    application.add_handler(CallbackQueryHandler(reject_deposit, pattern="^reject_deposit_"))
    application.add_handler(CallbackQueryHandler(admin_broadcast, pattern="^admin_broadcast$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))

    print("🚀 Veltrix Store Bot is starting...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()    