#!/usr/bin/env python3
"""
Soccer Pot Bot - a tiny Telegram bot that tracks who's paid for the game.

Honor-system payment tracker for a casual group chat:
  - An organizer starts a game with a per-head cost  (/newgame 10 Sunday 7s)
  - Players tap "I'm in" to join the roster
  - Players tap "I paid" when they've sent the money
  - /status shows a live scoreboard of who still owes
  - /remind privately DMs everyone who still owes (attributed to the sender)

No database server needed - everything lives in a single SQLite file.
"""

import os
import html
import logging
import sqlite3
from datetime import datetime
from contextlib import closing

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

DB_PATH = os.environ.get("POTBOT_DB", "potbot.db")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Extra organizers by user id, on top of the chat's own admins.
# e.g.  export ORGANIZER_IDS="11111111,22222222"
ORGANIZER_IDS = {
    int(x)
    for x in os.environ.get("ORGANIZER_IDS", "").replace(" ", "").split(",")
    if x
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ----------------------------- database -----------------------------

def init_db():
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS games (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                amount      REAL    NOT NULL,
                label       TEXT,
                status      TEXT    NOT NULL DEFAULT 'open',
                created_at  TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS participants (
                game_id     INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                name        TEXT    NOT NULL,
                username    TEXT,
                paid        INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (game_id, user_id)
            );
            """
        )
        db.commit()


def open_game(chat_id):
    """Return the current open game for a chat, or None."""
    with closing(sqlite3.connect(DB_PATH)) as db:
        r = db.execute(
            "SELECT id, chat_id, amount, label, status FROM games "
            "WHERE chat_id=? AND status='open' ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    return _row_to_game(r)


def game_by_id(game_id):
    with closing(sqlite3.connect(DB_PATH)) as db:
        r = db.execute(
            "SELECT id, chat_id, amount, label, status FROM games WHERE id=?",
            (game_id,),
        ).fetchone()
    return _row_to_game(r)


def _row_to_game(r):
    if not r:
        return None
    return {"id": r[0], "chat_id": r[1], "amount": r[2], "label": r[3], "status": r[4]}


def add_participant(game_id, user):
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute(
            "INSERT OR IGNORE INTO participants (game_id, user_id, name, username, paid) "
            "VALUES (?,?,?,?,0)",
            (game_id, user.id, user.first_name, user.username),
        )
        db.commit()


def set_paid(game_id, user_id, paid):
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute(
            "UPDATE participants SET paid=? WHERE game_id=? AND user_id=?",
            (1 if paid else 0, game_id, user_id),
        )
        db.commit()


def remove_participant(game_id, user_id):
    """Drop a player from the roster (used by 'I'm out' / 'Revoke')."""
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute(
            "DELETE FROM participants WHERE game_id=? AND user_id=?",
            (game_id, user_id),
        )
        db.commit()


def is_participant(game_id, user_id):
    with closing(sqlite3.connect(DB_PATH)) as db:
        r = db.execute(
            "SELECT 1 FROM participants WHERE game_id=? AND user_id=?",
            (game_id, user_id),
        ).fetchone()
    return r is not None


def count_participants(game_id):
    with closing(sqlite3.connect(DB_PATH)) as db:
        (n,) = db.execute(
            "SELECT COUNT(*) FROM participants WHERE game_id=?", (game_id,)
        ).fetchone()
    return n


def get_participants(game_id):
    with closing(sqlite3.connect(DB_PATH)) as db:
        rows = db.execute(
            "SELECT user_id, name, username, paid FROM participants "
            "WHERE game_id=? ORDER BY paid DESC, name COLLATE NOCASE",
            (game_id,),
        ).fetchall()
    return [
        {"user_id": r[0], "name": r[1], "username": r[2], "paid": bool(r[3])}
        for r in rows
    ]


# ----------------------------- permissions -----------------------------

async def is_organizer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Organizer = a chat admin/creator, or an id listed in ORGANIZER_IDS."""
    chat = update.effective_chat
    user = update.effective_user
    if user and user.id in ORGANIZER_IDS:
        return True
    if chat.type == "private":
        return True  # your own DM, you're the organizer
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("creator", "administrator")
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not check admin status: %s", e)
        return False


# ----------------------------- rendering -----------------------------

def mention(p):
    """An HTML mention that pings the user even if they have no @username."""
    return f'<a href="tg://user?id={p["user_id"]}">{html.escape(p["name"])}</a>'


def start_link(context: ContextTypes.DEFAULT_TYPE):
    u = getattr(context.bot, "username", None)
    return f"https://t.me/{u}?start=pay" if u else None


def game_header(amount, label):
    title = f"⚽ <b>{html.escape(label)}</b>" if label else "⚽ <b>Game pot</b>"
    return f"{title}\n${amount:.2f} per player"


def game_keyboard(game_id):
    """A two-stage shared keyboard.

    Telegram attaches one keyboard to a group message (everyone sees the same
    buttons), so we stage the controls by the roster's state instead of per
    user:

      * Nobody's joined yet  →  just a single "I'm in" button.
      * Someone is in         →  reveal the follow-up controls: Revoke (leave),
                                  I paid, Not paid.

    Whichever button a player taps acts on that player.
    """
    if count_participants(game_id) == 0:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("I'm in ⚽", callback_data=f"join:{game_id}"),
                ]
            ]
        )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Revoke ↩️", callback_data=f"leave:{game_id}"),
                InlineKeyboardButton("I paid ✅", callback_data=f"paid:{game_id}"),
                InlineKeyboardButton("Not paid 💸", callback_data=f"unpaid:{game_id}"),
            ],
        ]
    )


def render_status(game):
    amount, label = game["amount"], game["label"]
    parts = get_participants(game["id"])
    header = game_header(amount, label)
    if not parts:
        return header + "\n\nNobody's joined yet. Tap <b>I'm in</b> ⚽"

    paid = [p for p in parts if p["paid"]]
    owe = [p for p in parts if not p["paid"]]

    lines = [header, "", f"<b>Paid ({len(paid)}/{len(parts)})</b>"]
    lines += [f"  ✅ {mention(p)}" for p in paid] or ["  —"]
    if owe:
        lines += ["", f"<b>Still owe — ${amount:.2f} each</b>"]
        lines += [f"  ❌ {mention(p)}" for p in owe]

    collected = len(paid) * amount
    outstanding = len(owe) * amount
    lines += ["", f"💰 Collected ${collected:.2f}  •  Outstanding ${outstanding:.2f}"]
    return "\n".join(lines)


# ----------------------------- handlers -----------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Tap-to-start deep link (t.me/Bot?start=pay) so the bot can DM the user.
    if (
        context.args
        and context.args[0] == "pay"
        and update.effective_chat.type == "private"
    ):
        await update.message.reply_text(
            "✅ You're all set — I can now send you private payment reminders here.\n\n"
            "Head back to your soccer group and tap <b>I paid</b> once you've sent "
            "your share.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        "⚽ <b>Soccer Pot Bot</b>\n\n"
        "I track who's paid for the game so nobody has to nag.\n\n"
        "<b>Commands</b>\n"
        "/newgame &lt;amount&gt; [label] — start collecting "
        "(<i>organizers only</i>)\n"
        "/status — see who's paid and who owes\n"
        "/remind — privately DM everyone who still owes\n"
        "/nudge — ping everyone who owes publicly in the group\n"
        "/markpaid — reply to someone's message to mark them paid\n"
        "/close — wrap up with a final tally (<i>organizers only</i>)\n\n"
        "Players tap the buttons: <b>I'm in</b>, then <b>I paid</b>. 🤝\n\n"
        "ℹ️ <i>Organizers are the group's admins. To receive private reminders, "
        "open this chat with me and press Start once.</i>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_organizer(update, context):
        await update.message.reply_text(
            "🔒 Only organizers (the group's admins) can start a new game.\n"
            "Ask an admin to run it — or add your id to ORGANIZER_IDS."
        )
        return

    chat_id = update.effective_chat.id
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /newgame <amount> [label]\nExample: /newgame 10 Sunday 7-a-side"
        )
        return
    try:
        amount = float(args[0].replace("$", "").replace(",", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "That amount doesn't look right. Try: /newgame 10"
        )
        return

    label = " ".join(args[1:]) if len(args) > 1 else None

    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute(
            "UPDATE games SET status='closed' WHERE chat_id=? AND status='open'",
            (chat_id,),
        )
        cur = db.execute(
            "INSERT INTO games (chat_id, amount, label, status, created_at) "
            "VALUES (?,?,?, 'open', ?)",
            (chat_id, amount, label, datetime.utcnow().isoformat()),
        )
        db.commit()
        game_id = cur.lastrowid

    game = game_by_id(game_id)
    msg = (
        render_status(game)
        + "\n\nTap <b>I'm in</b> to join, then <b>I paid</b> once you've sent the money."
    )
    link = start_link(context)
    if link:
        msg += (
            f'\n\n🔔 <a href="{link}">Tap here & press Start</a> so I can send you '
            "private reminders."
        )
    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=game_keyboard(game_id),
        disable_web_page_preview=True,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    game = open_game(update.effective_chat.id)
    if not game:
        await update.message.reply_text("No active game. Ask an organizer to /newgame.")
        return
    await update.message.reply_text(
        render_status(game),
        parse_mode=ParseMode.HTML,
        reply_markup=game_keyboard(game["id"]),
    )


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    game = open_game(chat.id)
    if not game:
        await update.message.reply_text("No active game. Ask an organizer to /newgame.")
        return

    owe = [p for p in get_participants(game["id"]) if not p["paid"]]
    if not owe:
        await update.message.reply_text("Everyone's paid up! 🎉")
        return

    initiator = update.effective_user
    initiator_m = (
        f'<a href="tg://user?id={initiator.id}">'
        f"{html.escape(initiator.first_name)}</a>"
    )
    group_name = chat.title or "your group"
    game_name = game["label"] or "the game"
    amount = game["amount"]

    reached, missed = [], []
    for p in owe:
        dm = (
            "💸 <b>Payment reminder</b>\n\n"
            f"{initiator_m} is collecting for <b>{html.escape(game_name)}</b> in "
            f"<b>{html.escape(group_name)}</b>.\n"
            f"You still owe <b>${amount:.2f}</b> — send it over when you can. "
            "Thanks! ⚽"
        )
        try:
            await context.bot.send_message(
                chat_id=p["user_id"], text=dm, parse_mode=ParseMode.HTML
            )
            reached.append(p)
        except (Forbidden, BadRequest):
            missed.append(p)

    # Build a private summary for the initiator (keeps who-owes off the group).
    link = start_link(context)
    summary = [f"📨 Reminder for <b>{html.escape(game_name)}</b> ({html.escape(group_name)}):"]
    if reached:
        summary.append("✅ Privately reminded: " + ", ".join(html.escape(p["name"]) for p in reached))
    if missed:
        summary.append("⚠️ Couldn't DM (haven't started the bot): " + ", ".join(html.escape(p["name"]) for p in missed))
        if link:
            summary.append(f"They need to open {link} and press Start, then you can /remind again.")

    summary_sent_privately = False
    try:
        await context.bot.send_message(
            chat_id=initiator.id,
            text="\n".join(summary),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        summary_sent_privately = True
    except (Forbidden, BadRequest):
        pass

    # Public confirmation — deliberately does NOT name who owes.
    if summary_sent_privately:
        note = f"📨 Sent {len(reached)} private reminder(s) — full details are in your DMs."
        if missed:
            note += f" ({len(missed)} couldn't be reached.)"
        await update.message.reply_text(note)
    else:
        lines = [f"📨 Sent {len(reached)} private reminder(s)."]
        if missed:
            lines.append(f"⚠️ {len(missed)} player(s) haven't started the bot, so I couldn't DM them.")
        if link:
            lines.append(f"Want the private summary yourself? Open {link}, press Start, then /remind again.")
        await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)


async def cmd_nudge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Public version of /remind: pings everyone who owes right in the group."""
    game = open_game(update.effective_chat.id)
    if not game:
        await update.message.reply_text("No active game. Ask an organizer to /newgame.")
        return
    owe = [p for p in get_participants(game["id"]) if not p["paid"]]
    if not owe:
        await update.message.reply_text("Everyone's paid up! 🎉")
        return
    mentions = " ".join(mention(p) for p in owe)
    await update.message.reply_text(
        f"💸 Friendly nudge — ${game['amount']:.2f} each still owed:\n{mentions}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_markpaid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    game = open_game(update.effective_chat.id)
    if not game:
        await update.message.reply_text("No active game.")
        return
    reply = update.message.reply_to_message
    if not reply:
        await update.message.reply_text(
            "Reply to someone's message with /markpaid to mark them paid."
        )
        return
    target = reply.from_user
    add_participant(game["id"], target)
    set_paid(game["id"], target.id, True)
    await update.message.reply_text(
        f"Marked {html.escape(target.first_name)} as paid ✅",
        parse_mode=ParseMode.HTML,
    )


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_organizer(update, context):
        await update.message.reply_text(
            "🔒 Only organizers (the group's admins) can close a game."
        )
        return
    game = open_game(update.effective_chat.id)
    if not game:
        await update.message.reply_text("No active game to close.")
        return
    owe = [p for p in get_participants(game["id"]) if not p["paid"]]
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute("UPDATE games SET status='closed' WHERE id=?", (game["id"],))
        db.commit()
    note = "\n\n🔒 Game closed."
    if owe:
        note += f" {len(owe)} player(s) still owe ${game['amount']:.2f}."
    await update.message.reply_text(
        render_status(game) + note, parse_mode=ParseMode.HTML
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    action, _, gid = query.data.partition(":")

    game = game_by_id(int(gid)) if gid.isdigit() else None
    if not game or game["status"] != "open":
        await query.answer(
            "That game is closed. Ask an organizer to /newgame.", show_alert=True
        )
        return

    if action == "join":
        add_participant(game["id"], user)
        await query.answer("You're in! ⚽")
    elif action == "leave":
        if is_participant(game["id"], user.id):
            remove_participant(game["id"], user.id)
            await query.answer("Took you off the roster. ↩️")
        else:
            await query.answer("You weren't on the roster.")
    elif action == "paid":
        add_participant(game["id"], user)
        set_paid(game["id"], user.id, True)
        await query.answer("Marked as paid ✅ Thanks!")
    elif action == "unpaid":
        add_participant(game["id"], user)
        set_paid(game["id"], user.id, False)
        await query.answer("Marked as not paid yet.")
    else:  # status
        await query.answer()

    try:
        await query.edit_message_text(
            render_status(game),
            parse_mode=ParseMode.HTML,
            reply_markup=game_keyboard(game["id"]),
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ------------------------------- main -------------------------------

def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Set the BOT_TOKEN environment variable (get one from @BotFather)."
        )
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("nudge", cmd_nudge))
    app.add_handler(CommandHandler("markpaid", cmd_markpaid))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CallbackQueryHandler(on_button))
    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
