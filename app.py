"""
Prezentatsiya buyurtma boti + admin panel.

Sozlamalar .env fayl yoki muhit o'zgaruvchilari orqali beriladi:
    BOT_TOKEN      - 8573836600:AAG-L12VvQejditCYFt4ss0M2sckyepAn7A
    ADMIN_ID       - 7374195060
    KARTA_RAQAMI   - 5614681290716020
    DB_PATH        - baza fayli yo'li (ixtiyoriy, standart: bot.db)
"""
import asyncio
import hashlib
import html
import logging
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.error import NetworkError, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ====================== SOZLAMALAR ======================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
DEFAULT_CARD = os.environ.get("KARTA_RAQAMI", "")
DB_PATH = os.environ.get("DB_PATH", "bot.db")
MAX_SLAYD = 50
DEFAULT_PRICES = {10: 3000, 20: 5000, 50: 15000}
# ========================================================

if not BOT_TOKEN or not ADMIN_ID:
    raise SystemExit("BOT_TOKEN va ADMIN_ID o'rnatilmagan! .env faylni tekshiring.")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # tokenni logga chiqarmaslik uchun

HTML = ParseMode.HTML
FAN, MAVZU, SLAYD, TIL, MUDDAT, TASDIQ, ADMINGA_YOZISH = range(7)
A_INPUT = 100

BTN_ORDER = "📋 Prezentatsiya buyurtma berish"
BTN_ADMIN = "👨‍💻 Adminga murojaat"
BTN_BACK = "🔙 Ortga"

MENU = ReplyKeyboardMarkup([[BTN_ORDER], [BTN_ADMIN]], resize_keyboard=True)
SLAYD_TUGMALAR = [["10", "20", "50"], ["Boshqa son"], [BTN_BACK]]
TIL_TUGMALAR = [["O'zbek", "Rus", "Ingliz"], [BTN_BACK]]
TASDIQ_TUGMALAR = [["Ha, yuborish", "Bekor qilish"]]

STATUS = {
    "new": "🆕 Yangi",
    "accepted": "🔧 Bajarilmoqda",
    "payment_wait": "💰 To'lov kutilmoqda",
    "payment_check": "🧾 Chek tekshirilmoqda",
    "paid": "✅ To'langan",
    "done": "📦 Fayl yuborilgan",
    "rejected": "❌ Rad etilgan",
}

# ====================== BAZA ======================
db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, name TEXT, username TEXT, banned INTEGER DEFAULT 0, joined TEXT);
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, fan TEXT, mavzu TEXT,
    slayd INTEGER, til TEXT, muddat TEXT, narx INTEGER, status TEXT DEFAULT 'new', created TEXT);
CREATE TABLE IF NOT EXISTS receipts(hash TEXT PRIMARY KEY, order_id INTEGER);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""
)


def run(sql, args=()):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def one(sql, args=()):
    return db.execute(sql, args).fetchone()[0]


def now():
    return datetime.now(timezone(timedelta(hours=5))).strftime("%Y-%m-%d %H:%M")  # Toshkent vaqti


def esc(s):
    return html.escape(str(s or ""))


def som(summa):
    return f"{int(summa):,}".replace(",", " ") + " so'm"


def get_setting(key, default=""):
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    run(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def prices():
    return {k: int(get_setting(f"price_{k}", v)) for k, v in DEFAULT_PRICES.items()}


def narx_hisobla(slayd_soni):
    p = prices()
    for limit in sorted(p):
        if slayd_soni <= limit:
            return p[limit]
    return p[max(p)]


def card():
    return get_setting("card", DEFAULT_CARD) or "Karta kiritilmagan"


def get_order(oid):
    return db.execute(
        "SELECT o.*, u.name AS uname, u.username AS uusername "
        "FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE o.id=?",
        (oid,),
    ).fetchone()


def set_status(oid, status):
    run("UPDATE orders SET status=? WHERE id=?", (status, oid))


def register(update):
    u = update.effective_user
    run(
        "INSERT INTO users(id, name, username, joined) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username",
        (u.id, u.full_name, u.username or "", now()),
    )
    return not one("SELECT banned FROM users WHERE id=?", (u.id,))


async def allowed(update):
    if register(update):
        return True
    await update.effective_message.reply_text("⛔ Siz bloklangansiz.")
    return False


async def safe_send(context, chat_id, text, **kw):
    try:
        await context.bot.send_message(chat_id, text, **kw)
        return True
    except TelegramError as e:
        logging.warning("Xabar yetmadi (%s): %s", chat_id, e)
        return False


def order_card(o):
    uname = f"@{o['uusername']}" if o["uusername"] else "username yo'q"
    return (
        f"<b>📥 Buyurtma #{o['id']}</b> — {STATUS.get(o['status'], o['status'])}\n\n"
        f"<b>Mijoz:</b> <a href=\"tg://user?id={o['user_id']}\">{esc(o['uname'])}</a> ({esc(uname)})\n"
        f"<b>ID:</b> <code>{o['user_id']}</code>\n\n"
        f"<b>Fan:</b> {esc(o['fan'])}\n"
        f"<b>Mavzu:</b> {esc(o['mavzu'])}\n"
        f"<b>Slaydlar:</b> {o['slayd']} ta\n"
        f"<b>Til:</b> {esc(o['til'])}\n"
        f"<b>Muddat:</b> {esc(o['muddat'])}\n"
        f"<b>Narx:</b> {som(o['narx'])}\n"
        f"<b>Vaqt:</b> {o['created']}"
    )


def order_buttons(o):
    i, s = o["id"], o["status"]
    rows = []
    if s == "new":
        rows.append(
            [Btn("✅ Qabul qilish", callback_data=f"o:accept:{i}"),
             Btn("❌ Rad etish", callback_data=f"o:reject:{i}")]
        )
    elif s == "accepted":
        rows.append([Btn("✨ Tayyor (to'lovni so'rash)", callback_data=f"o:ready:{i}")])
    elif s == "payment_check":
        rows.append(
            [Btn("✅ To'lovni tasdiqlash", callback_data=f"o:approve:{i}"),
             Btn("🚫 Chek noto'g'ri", callback_data=f"o:badpay:{i}")]
        )
    elif s in ("paid", "done"):
        rows.append([Btn("📂 Fayl yuborish", callback_data=f"o:file:{i}")])
    rows.append([Btn("💬 Mijozga yozish", callback_data=f"a:reply:{o['user_id']}")])
    return rows


async def refresh(query, o):
    text, kb = order_card(o), Markup(order_buttons(o))
    try:
        if query.message.photo or query.message.document:
            await query.edit_message_caption(caption=text, parse_mode=HTML, reply_markup=kb)
        else:
            await query.edit_message_text(text, parse_mode=HTML, reply_markup=kb)
    except TelegramError as e:
        logging.info("Xabar yangilanmadi: %s", e)


async def not_admin(update):
    cq = update.callback_query
    if cq.from_user.id == ADMIN_ID:
        return False
    await cq.answer("Ruxsat yo'q", show_alert=True)
    return True


# ====================== MIJOZ QISMI ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if not await allowed(update):
        return ConversationHandler.END
    text = (
        "Assalomu alaykum! Prezentatsiya buyurtma berish botiga xush kelibsiz.\n\n"
        "Quyidagi tugmalar yordamida buyurtma berishingiz yoki adminga murojaat qilishingiz mumkin:"
    )
    if update.effective_user.id == ADMIN_ID:
        text += "\n\n🔧 Admin panel: /admin"
    await update.message.reply_text(text, reply_markup=MENU)
    return ConversationHandler.END


async def buyurtma_boshlash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await allowed(update):
        return ConversationHandler.END
    context.user_data.clear()
    p = prices()
    await update.message.reply_text(
        "Prezentatsiya narxlari:\n"
        f"• 10 tagacha slayd: {som(p[10])}\n"
        f"• 20 tagacha slayd: {som(p[20])}\n"
        f"• 50 tagacha slayd: {som(p[50])}\n\n"
        "Bekor qilish uchun istalgan payt /cancel yozing.\n\n"
        "1/5. Qaysi fandan? (masalan: Anatomiya)",
        reply_markup=ReplyKeyboardRemove(),
    )
    return FAN


async def fan(update, context):
    text = update.message.text
    if text == BTN_BACK:
        return await start(update, context)
    context.user_data["fan"] = text[:100]
    await update.message.reply_text("2/5. Mavzu nomini yozing:")
    return MAVZU


async def mavzu(update, context):
    text = update.message.text
    if text == BTN_BACK:
        await update.message.reply_text("1/5. Qaysi fandan?")
        return FAN
    context.user_data["mavzu"] = text[:200]
    await update.message.reply_text(
        "3/5. Nechta slayd kerak?",
        reply_markup=ReplyKeyboardMarkup(SLAYD_TUGMALAR, one_time_keyboard=True, resize_keyboard=True),
    )
    return SLAYD


async def slayd(update, context):
    text = update.message.text
    if text == BTN_BACK:
        await update.message.reply_text("2/5. Mavzu nomini yozing:", reply_markup=ReplyKeyboardRemove())
        return MAVZU
    if text == "Boshqa son":
        await update.message.reply_text(
            f"Slaydlar sonini raqam bilan yozing (1 dan {MAX_SLAYD} gacha):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return SLAYD
    if not text.isdigit() or not (1 <= int(text) <= MAX_SLAYD):
        await update.message.reply_text(f"Iltimos, 1 dan {MAX_SLAYD} gacha bo'lgan raqamni kiriting.")
        return SLAYD
    context.user_data["slayd"] = int(text)
    await update.message.reply_text(
        "4/5. Qaysi tilda bo'lsin?",
        reply_markup=ReplyKeyboardMarkup(TIL_TUGMALAR, one_time_keyboard=True, resize_keyboard=True),
    )
    return TIL


async def til(update, context):
    text = update.message.text
    if text == BTN_BACK:
        await update.message.reply_text(
            "3/5. Nechta slayd kerak?",
            reply_markup=ReplyKeyboardMarkup(SLAYD_TUGMALAR, one_time_keyboard=True, resize_keyboard=True),
        )
        return SLAYD
    context.user_data["til"] = text[:30]
    await update.message.reply_text(
        "5/5. Qachongacha kerak? (masalan: 15-oktabr yoki 3 kun ichida)",
        reply_markup=ReplyKeyboardRemove(),
    )
    return MUDDAT


async def muddat(update, context):
    text = update.message.text
    if text == BTN_BACK:
        await update.message.reply_text(
            "4/5. Qaysi tilda bo'lsin?",
            reply_markup=ReplyKeyboardMarkup(TIL_TUGMALAR, one_time_keyboard=True, resize_keyboard=True),
        )
        return TIL
    context.user_data["muddat"] = text[:100]
    d = context.user_data
    await update.message.reply_text(
        "Buyurtmangizni tekshiring:\n\n"
        f"• Fan: {d['fan']}\n"
        f"• Mavzu: {d['mavzu']}\n"
        f"• Slaydlar: {d['slayd']} ta\n"
        f"• Til: {d['til']}\n"
        f"• Muddat: {d['muddat']}\n"
        f"• Narx: {som(narx_hisobla(d['slayd']))}\n\n"
        "Ma'lumotlar to'g'rimi? Yuboraylikmi?",
        reply_markup=ReplyKeyboardMarkup(TASDIQ_TUGMALAR, one_time_keyboard=True, resize_keyboard=True),
    )
    return TASDIQ


async def tasdiq(update, context):
    d = context.user_data
    if update.message.text != "Ha, yuborish" or not all(k in d for k in ("fan", "mavzu", "slayd", "til", "muddat")):
        return await cancel(update, context)

    oid = run(
        "INSERT INTO orders(user_id, fan, mavzu, slayd, til, muddat, narx, created) VALUES(?,?,?,?,?,?,?,?)",
        (update.effective_user.id, d["fan"], d["mavzu"], d["slayd"], d["til"], d["muddat"],
         narx_hisobla(d["slayd"]), now()),
    ).lastrowid
    o = get_order(oid)
    await context.bot.send_message(
        ADMIN_ID, "<b>YANGI BUYURTMA!</b>\n\n" + order_card(o),
        parse_mode=HTML, reply_markup=Markup(order_buttons(o)),
    )
    await update.message.reply_text(
        f"✅ Buyurtmangiz (#{oid}) adminga yuborildi! Tez orada ko'rib chiqib, siz bilan bog'lanishadi.",
        reply_markup=MENU,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def adminga_murojaat_boshlash(update, context):
    if not await allowed(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "Admin uchun savol yoki murojaatingizni yozib yuboring:",
        reply_markup=ReplyKeyboardMarkup([[BTN_BACK]], resize_keyboard=True),
    )
    return ADMINGA_YOZISH


async def adminga_xabar_jonatish(update, context):
    text = update.message.text
    if text == BTN_BACK:
        return await start(update, context)
    user = update.effective_user
    uname = f"@{user.username}" if user.username else "username yo'q"
    await context.bot.send_message(
        ADMIN_ID,
        "<b>✉️ YANGI MUROJAAT</b>\n\n"
        f"<b>Kimdan:</b> <a href=\"tg://user?id={user.id}\">{esc(user.full_name)}</a> ({esc(uname)})\n"
        f"<b>ID:</b> <code>{user.id}</code>\n\n"
        f"<b>Xabar:</b> {esc(text[:3000])}",
        parse_mode=HTML,
        reply_markup=Markup([[Btn("💬 Javob yozish", callback_data=f"a:reply:{user.id}")]]),
    )
    await update.message.reply_text(
        "Murojaatingiz adminga yuborildi! Tez orada javob qaytaramiz.", reply_markup=MENU
    )
    return ConversationHandler.END


async def handle_receipt(update, context):
    """Mijoz to'lov chekini yuboradi."""
    if not await allowed(update):
        return
    msg, uid = update.message, update.effective_user.id
    row = db.execute(
        "SELECT id FROM orders WHERE user_id=? AND status='payment_wait' ORDER BY id DESC LIMIT 1", (uid,)
    ).fetchone()
    if not row:
        await msg.reply_text(
            "Hozircha to'lov kutilayotgan buyurtmangiz yo'q. "
            "Chekni admin buyurtma tayyor ekanini xabar qilgandan keyin yuboring."
        )
        return
    if msg.photo:
        media = msg.photo[-1]
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        media = msg.document
    else:
        await msg.reply_text("Iltimos, chekni rasm ko'rinishida yuboring.")
        return
    try:
        tg_file = await media.get_file()
        digest = hashlib.sha256(bytes(await tg_file.download_as_bytearray())).hexdigest()
    except TelegramError:
        await msg.reply_text("Rasmni o'qib bo'lmadi, qaytadan yuboring.")
        return
    if db.execute("SELECT 1 FROM receipts WHERE hash IN (?, ?)", (digest, media.file_unique_id)).fetchone():
        await msg.reply_text("❌ Bu chek oldin yuborilgan! Iltimos, yangi chekni yuboring.")
        return
    oid = row["id"]
    for key in (digest, media.file_unique_id):
        run("INSERT OR IGNORE INTO receipts(hash, order_id) VALUES(?, ?)", (key, oid))
    set_status(oid, "payment_check")
    o = get_order(oid)
    await msg.copy(
        chat_id=ADMIN_ID,
        caption=order_card(o) + "\n\n🧾 <b>To'lov cheki</b>",
        parse_mode=HTML,
        reply_markup=Markup(order_buttons(o)),
    )
    await msg.reply_text("Chekingiz adminga yuborildi! 🕒 Tekshirilmoqda, iltimos biroz kuting.")


async def not_text_handler(update, context):
    if update.message.photo or update.message.document:
        await handle_receipt(update, context)
        return
    await update.message.reply_text("Iltimos, matn yuboring.")


async def cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text("Bekor qilindi. Qayta boshlash uchun /start yozing.", reply_markup=MENU)
    return ConversationHandler.END


async def free_text(update, context):
    if await allowed(update):
        await update.message.reply_text("Quyidagi tugmalardan birini tanlang 👇", reply_markup=MENU)


async def myid(update, context):
    await update.message.reply_text(
        f"Sizning ID raqamingiz: <code>{update.effective_user.id}</code>", parse_mode=HTML
    )


# ====================== ADMIN PANEL ======================
def menu_kb():
    return Markup(
        [
            [Btn("📊 Statistika", callback_data="a:stats"), Btn("🆕 Yangi buyurtmalar", callback_data="a:new")],
            [Btn("💰 To'lov bosqichida", callback_data="a:pay"), Btn("📋 Oxirgi buyurtmalar", callback_data="a:all")],
            [Btn("🔍 Buyurtmani topish", callback_data="a:find"), Btn("📢 Xabar tarqatish", callback_data="a:bc")],
            [Btn("💲 Narxlar", callback_data="a:prices"), Btn("💳 Karta raqami", callback_data="a:card")],
        ]
    )


async def admin_menu(update, context):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    context.user_data.pop("adm", None)
    if update.callback_query:
        await update.callback_query.answer()
    await update.effective_message.reply_text(
        "🔧 <b>Admin panel</b>\n\nBloklash: /ban ID  •  Blokdan chiqarish: /unban ID",
        parse_mode=HTML, reply_markup=menu_kb(),
    )
    return ConversationHandler.END


def stats_text():
    today = now()[:10]
    income = one("SELECT COALESCE(SUM(narx), 0) FROM orders WHERE status IN ('paid', 'done')")
    lines = [
        "📊 <b>Statistika</b>\n",
        f"👥 Foydalanuvchilar: {one('SELECT COUNT(*) FROM users')} "
        f"(bloklangan: {one('SELECT COUNT(*) FROM users WHERE banned=1')})",
        f"📥 Jami buyurtmalar: {one('SELECT COUNT(*) FROM orders')}",
        f"📅 Bugungi buyurtmalar: {one('SELECT COUNT(*) FROM orders WHERE created LIKE ?', (today + '%',))}",
        f"💵 Daromad (to'langan): {som(income)}",
        "",
        "<b>Holatlar bo'yicha:</b>",
    ]
    for r in db.execute("SELECT status, COUNT(*) AS c FROM orders GROUP BY status"):
        lines.append(f"{STATUS.get(r['status'], r['status'])}: {r['c']}")
    return "\n".join(lines)


async def panel_cb(update, context):
    if await not_admin(update):
        return
    query = update.callback_query
    await query.answer()
    kind = query.data.split(":")[1]
    if kind == "stats":
        await query.message.reply_text(stats_text(), parse_mode=HTML)
    elif kind == "prices":
        kb = [[Btn(f"{k} tagacha: {som(v)} ✏️", callback_data=f"a:price:{k}")] for k, v in prices().items()]
        await query.message.reply_text(
            "💲 <b>Narxlar</b>\nO'zgartirish uchun tanlang:", parse_mode=HTML, reply_markup=Markup(kb)
        )


async def order_list(update, context):
    if await not_admin(update):
        return
    query = update.callback_query
    await query.answer()
    kind = query.data.split(":")[1]
    where, title = {
        "new": ("status='new'", "🆕 Yangi buyurtmalar"),
        "pay": ("status IN ('payment_wait', 'payment_check')", "💰 To'lov bosqichidagilar"),
        "all": ("1=1", "📋 Oxirgi buyurtmalar"),
    }[kind]
    rows = db.execute(f"SELECT id, fan, slayd, status FROM orders WHERE {where} ORDER BY id DESC LIMIT 15").fetchall()
    if not rows:
        await query.message.reply_text(f"{title}: hozircha bo'sh.")
        return
    kb = [
        [Btn(f"{STATUS[r['status']].split()[0]} #{r['id']} · {r['fan'][:20]} · {r['slayd']} ta",
             callback_data=f"o:view:{r['id']}")]
        for r in rows
    ]
    await query.message.reply_text(title, reply_markup=Markup(kb))


async def order_action(update, context):
    """Qabul / rad / tayyor / chek noto'g'ri / ko'rish tugmalari."""
    if await not_admin(update):
        return
    query = update.callback_query
    _, action, raw = query.data.split(":")
    oid = int(raw)
    o = get_order(oid)
    if not o:
        await query.answer("Buyurtma topilmadi", show_alert=True)
        return
    if action == "view":
        await query.answer()
        await query.message.reply_text(order_card(o), parse_mode=HTML, reply_markup=Markup(order_buttons(o)))
        return

    flow = {
        "accept": ("new", "accepted"),
        "reject": ("new", "rejected"),
        "ready": ("accepted", "payment_wait"),
        "badpay": ("payment_check", "payment_wait"),
    }
    need, new = flow[action]
    if o["status"] != need:
        await query.answer("Holat allaqachon o'zgargan", show_alert=True)
        await refresh(query, o)
        return
    set_status(oid, new)
    await query.answer()
    texts = {
        "accept": f"✅ Buyurtmangiz (#{oid}) qabul qilindi va ustida ish boshlandi! 👨‍💻 Tez orada tayyor bo'ladi.",
        "reject": f"😔 Afsuski, buyurtmangiz (#{oid}) rad etildi.",
        "ready": (
            f"🎉 <b>Prezentatsiyangiz (#{oid}) tayyor!</b>\n\n"
            f"💰 <b>Summa:</b> {som(o['narx'])}\n"
            f"💳 <b>Karta:</b> <code>{esc(card())}</code>\n\n"
            "To'lovni amalga oshirib, <b>chek rasmini</b> shu yerga yuboring!"
        ),
        "badpay": f"⚠️ Buyurtma #{oid} uchun yuborgan chekingiz tasdiqlanmadi. Iltimos, to'g'ri chekni qayta yuboring.",
    }
    if not await safe_send(context, o["user_id"], texts[action], parse_mode=HTML):
        await query.message.reply_text("⚠️ Mijozga xabar yetmadi (u botni bloklagan bo'lishi mumkin).")
    await refresh(query, get_order(oid))


async def adm_input_entry(update, context):
    """Admindan matn/fayl kutadigan amallar boshlanishi."""
    if await not_admin(update):
        return ConversationHandler.END
    query = update.callback_query
    parts = query.data.split(":")

    if parts[0] == "o":  # o:approve:ID  yoki  o:file:ID
        oid = int(parts[2])
        o = get_order(oid)
        if not o:
            await query.answer("Buyurtma topilmadi", show_alert=True)
            return ConversationHandler.END
        if parts[1] == "approve":
            if o["status"] != "payment_check":
                await query.answer("Holat allaqachon o'zgargan", show_alert=True)
                await refresh(query, o)
                return ConversationHandler.END
            set_status(oid, "paid")
            await safe_send(context, o["user_id"], f"✅ #{oid} uchun to'lovingiz tasdiqlandi! Fayl tez orada yuboriladi 📂")
            await refresh(query, get_order(oid))
        elif o["status"] not in ("paid", "done"):
            await query.answer("Avval to'lov tasdiqlanishi kerak", show_alert=True)
            return ConversationHandler.END
        context.user_data["adm"] = ("file", oid)
        prompt = f"📂 #{oid} buyurtma uchun tayyor faylni (.pptx / .pdf) yuboring.\nBekor qilish: /cancel"
    else:  # a:...
        kind = parts[1]
        if kind == "price":
            n = int(parts[2])
            context.user_data["adm"] = ("price", n)
            prompt = (f"💲 {n} tagacha slayd uchun yangi narxni yozing (faqat raqam, so'mda).\n"
                      f"Hozirgi: {som(prices().get(n, 0))}\nBekor qilish: /cancel")
        elif kind == "card":
            context.user_data["adm"] = ("card",)
            prompt = f"💳 Yangi karta raqamini yozing.\nHozirgi: {card()}\nBekor qilish: /cancel"
        elif kind == "bc":
            context.user_data["adm"] = ("bc",)
            prompt = "📢 Barcha foydalanuvchilarga yuboriladigan xabarni yozing (matn yoki rasm).\nBekor qilish: /cancel"
        elif kind == "find":
            context.user_data["adm"] = ("find",)
            prompt = "🔍 Buyurtma raqamini yozing (masalan: 12).\nBekor qilish: /cancel"
        else:  # reply
            context.user_data["adm"] = ("reply", int(parts[2]))
            prompt = "💬 Mijozga yuboriladigan javob matnini yozing.\nBekor qilish: /cancel"
    await query.answer()
    await query.message.reply_text(prompt)
    return A_INPUT


async def broadcast(context, msg):
    ids = [r["id"] for r in db.execute("SELECT id FROM users WHERE banned=0 AND id != ?", (ADMIN_ID,))]
    ok = fail = 0
    for uid in ids:
        try:
            await msg.copy(chat_id=uid)
            ok += 1
        except TelegramError:
            fail += 1
        await asyncio.sleep(0.05)
    await safe_send(context, ADMIN_ID, f"📢 Tarqatish tugadi.\n✅ Yetdi: {ok}\n⚠️ Yetmadi: {fail}")


async def adm_input(update, context):
    msg = update.message
    action = context.user_data.get("adm")
    if not action:
        await msg.reply_text("Amal topilmadi. /admin yozing.")
        return ConversationHandler.END
    kind, text = action[0], (msg.text or "").strip()

    if kind == "file":
        if msg.text:
            await msg.reply_text("Iltimos, fayl yuboring (matn emas). Bekor qilish: /cancel")
            return A_INPUT
        o = get_order(action[1])
        try:
            await msg.copy(
                chat_id=o["user_id"],
                caption=f"🎉 Mana sizning tayyor prezentatsiyangiz (#{o['id']})! Xaridingiz uchun rahmat 😊",
            )
        except TelegramError as e:
            await msg.reply_text(f"❌ Yuborib bo'lmadi: {e}\nBoshqa fayl yuboring yoki /cancel.")
            return A_INPUT
        set_status(o["id"], "done")
        await msg.reply_text(f"✅ Fayl mijozga yuborildi (buyurtma #{o['id']}).")

    elif kind == "price":
        digits = text.replace(" ", "")
        if not digits.isdigit() or int(digits) <= 0:
            await msg.reply_text("Faqat musbat raqam yozing. Bekor qilish: /cancel")
            return A_INPUT
        set_setting(f"price_{action[1]}", digits)
        await msg.reply_text(f"✅ {action[1]} tagacha slayd narxi: {som(int(digits))}")

    elif kind == "card":
        if not text:
            await msg.reply_text("Karta raqamini matn bilan yozing. Bekor qilish: /cancel")
            return A_INPUT
        set_setting("card", text[:50])
        await msg.reply_text(f"✅ Karta yangilandi: {text[:50]}")

    elif kind == "bc":
        context.application.create_task(broadcast(context, msg))
        await msg.reply_text("📢 Xabar tarqatilmoqda, tugagach xabar beraman...")

    elif kind == "find":
        raw = text.lstrip("#")
        o = get_order(int(raw)) if raw.isdigit() else None
        if not o:
            await msg.reply_text("Bunday buyurtma topilmadi.")
        else:
            await msg.reply_text(order_card(o), parse_mode=HTML, reply_markup=Markup(order_buttons(o)))

    elif kind == "reply":
        if not text:
            await msg.reply_text("Javobni matn bilan yozing. Bekor qilish: /cancel")
            return A_INPUT
        ok = await safe_send(
            context, action[1], f"👨‍💻 <b>Admin javobi:</b>\n\n{esc(text[:3500])}", parse_mode=HTML
        )
        await msg.reply_text("✅ Javob yuborildi." if ok else "⚠️ Yuborilmadi (mijoz botni bloklagan bo'lishi mumkin).")

    context.user_data.pop("adm", None)
    return ConversationHandler.END


async def _set_ban(update, context, value):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Foydalanish: /ban 123456789  yoki  /unban 123456789")
        return
    uid = int(context.args[0])
    if uid == ADMIN_ID:
        await update.message.reply_text("O'zingizni bloklab bo'lmaydi 🙂")
        return
    n = run("UPDATE users SET banned=? WHERE id=?", (value, uid)).rowcount
    await update.message.reply_text(
        ("⛔ Bloklandi" if value else "✅ Blokdan chiqarildi") if n else "Bunday foydalanuvchi bazada yo'q."
    )


async def ban_cmd(update, context):
    await _set_ban(update, context, 1)


async def unban_cmd(update, context):
    await _set_ban(update, context, 0)


_last_alert = {"t": 0.0}


async def on_error(update, context):
    logging.error("Xatolik:", exc_info=context.error)
    if isinstance(context.error, NetworkError):  # tarmoq uzilishlari o'zi tiklanadi
        return
    if time.time() - _last_alert["t"] > 600:  # adminga 10 daqiqada 1 martadan ko'p yozmaymiz
        _last_alert["t"] = time.time()
        await safe_send(context, ADMIN_ID, f"⚠️ Botda xatolik: {esc(repr(context.error)[:300])}", parse_mode=HTML)


# ====================== QOTIB QOLISHDAN HIMOYA ======================
RESTART = {"flag": False}


async def watchdog(app):
    """Har 60 soniyada Telegram bilan aloqani tekshiradi; 3 marta ketma-ket uzilsa botni qayta ishga tushiradi."""
    fails = 0
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.wait_for(app.bot.get_me(), timeout=20)
            fails = 0
        except Exception as e:
            fails += 1
            logging.warning("Watchdog: Telegram bilan aloqa yo'q (%s/3): %r", fails, e)
            if fails >= 3:
                RESTART["flag"] = True
                stop = getattr(app, "stop_running", None)
                if stop:
                    stop()
                else:
                    os._exit(1)
                return


async def post_init(app):
    app.create_task(watchdog(app))


# ====================== HEALTH SERVER (Render va shunga o'xshash saytlar uchun) ======================
def start_health_server():
    port = os.environ.get("PORT")
    if not port:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        do_HEAD = do_GET

        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", int(port)), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.info("Health server %s portda ishlamoqda", port)


def start_keep_alive():
    """Render kabi saytlarda bot uxlab qolmasligi uchun o'z-o'ziga har 10 daqiqada so'rov yuboradi."""
    url = os.environ.get("KEEP_ALIVE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return

    def loop():
        while True:
            time.sleep(600)
            try:
                urllib.request.urlopen(url, timeout=15).read()
            except Exception as e:
                logging.warning("Keep-alive xatosi: %s", e)

    threading.Thread(target=loop, daemon=True).start()


# ====================== ISHGA TUSHIRISH ======================
def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)  # bitta sekin so'rov boshqalarni to'xtatib qo'ymaydi
        .connect_timeout(15).read_timeout(20).write_timeout(20).pool_timeout(15)
        .get_updates_connect_timeout(15).get_updates_read_timeout(35)
        .get_updates_write_timeout(20).get_updates_pool_timeout(15)
        .post_init(post_init)
        .build()
    )

    text_only = filters.TEXT & ~filters.COMMAND
    not_text = ~filters.TEXT & ~filters.COMMAND

    admin_conv = ConversationHandler(
        entry_points=[
            CommandHandler("admin", admin_menu, filters=filters.User(ADMIN_ID)),
            CallbackQueryHandler(
                adm_input_entry,
                pattern=r"^(o:(approve|file):\d+|a:(price:\d+|card|bc|find|reply:\d+))$",
            ),
        ],
        states={A_INPUT: [MessageHandler(filters.ALL & ~filters.COMMAND, adm_input)]},
        fallbacks=[
            CommandHandler("admin", admin_menu),
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CallbackQueryHandler(admin_menu, pattern=r"^a:menu$"),
        ],
        allow_reentry=True,
    )

    customer_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Text([BTN_ORDER]), buyurtma_boshlash),
            MessageHandler(filters.Text([BTN_ADMIN]), adminga_murojaat_boshlash),
        ],
        states={
            ADMINGA_YOZISH: [MessageHandler(text_only, adminga_xabar_jonatish), MessageHandler(not_text, not_text_handler)],
            FAN: [MessageHandler(text_only, fan), MessageHandler(not_text, not_text_handler)],
            MAVZU: [MessageHandler(text_only, mavzu), MessageHandler(not_text, not_text_handler)],
            SLAYD: [MessageHandler(text_only, slayd), MessageHandler(not_text, not_text_handler)],
            TIL: [MessageHandler(text_only, til), MessageHandler(not_text, not_text_handler)],
            MUDDAT: [MessageHandler(text_only, muddat), MessageHandler(not_text, not_text_handler)],
            TASDIQ: [MessageHandler(text_only, tasdiq), MessageHandler(not_text, not_text_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
        allow_reentry=True,
    )

    app.add_handler(admin_conv)
    app.add_handler(customer_conv)

    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("ban", ban_cmd, filters=filters.User(ADMIN_ID)))
    app.add_handler(CommandHandler("unban", unban_cmd, filters=filters.User(ADMIN_ID)))
    app.add_handler(CallbackQueryHandler(admin_menu, pattern=r"^a:menu$"))
    app.add_handler(CallbackQueryHandler(panel_cb, pattern=r"^a:(stats|prices)$"))
    app.add_handler(CallbackQueryHandler(order_list, pattern=r"^a:(new|pay|all)$"))
    app.add_handler(CallbackQueryHandler(order_action, pattern=r"^o:(accept|reject|ready|badpay|view):\d+$"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_receipt))
    app.add_handler(MessageHandler(text_only, free_text))
    app.add_error_handler(on_error)

    print("Bot muvaffaqiyatli ishga tushdi...")
    app.run_polling(
        bootstrap_retries=-1,  # internet yo'q bo'lsa ham to'xtamasdan qayta urinadi
        close_loop=False,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    start_health_server()
    start_keep_alive()
    while True:  # biror narsa qulasa yoki qotsa, bot o'zi qayta ishga tushadi
        RESTART["flag"] = False
        asyncio.set_event_loop(asyncio.new_event_loop())
        try:
            main()
        except Exception:
            logging.exception("Bot qulab tushdi, 10 soniyadan keyin qayta ishga tushadi")
            time.sleep(10)
            continue
        if RESTART["flag"]:
            logging.warning("Watchdog botni qayta ishga tushirmoqda...")
            time.sleep(3)
            continue
        break  # oddiy to'xtatish (Ctrl+C / server o'chirish)
