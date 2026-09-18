
╔══════════════════════════════════════════════════════════════════╗
║          BOT PHÂN TÍCH XÚC XẮC MD5 - BoKietvidai v3.0          ║
║  Flask (Main Thread) + Telegram Bot (Daemon Thread) cho Render   ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ═══════════════════════════════════════════════════════════════════
# 0. CẤU HÌNH CHÍNH
# ═══════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN: str = "8931512528:AAE9CC1Kw_xRFO6QYJkQI6Su60dA7I0cDlQ"
ADMIN_ID: int = 8284419367

# ═══════════════════════════════════════════════════════════════════
# 1. IMPORT
# ═══════════════════════════════════════════════════════════════════
import asyncio
import hashlib
import logging
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import nest_asyncio
from flask import Flask

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ═══════════════════════════════════════════════════════════════════
# 2. LOGGING
# ═══════════════════════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bokiet.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("BoKiet")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ═══════════════════════════════════════════════════════════════════
# 3. FLASK APP — chạy trên Main Thread, Render Health Check
# ═══════════════════════════════════════════════════════════════════
app = Flask(__name__)


@app.route("/")
def index():
    return "Bot is running!", 200


@app.route("/health")
def health():
    return "Bot is running!", 200


# ═══════════════════════════════════════════════════════════════════
# 4. DATABASE — SQLite với Lock chống race condition
# ═══════════════════════════════════════════════════════════════════
DB_PATH = "bokiet.db"
_db_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """Trả về connection SQLite an toàn đa luồng."""
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def db_init() -> None:
    """Tạo tất cả bảng nếu chưa có."""
    with _db_lock, _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS keys (
                key_code        TEXT PRIMARY KEY,
                duration_label  TEXT NOT NULL,
                expires_at      REAL,
                activated_by    INTEGER,
                activated_at    REAL,
                created_at      REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id         INTEGER PRIMARY KEY,
                active_key      TEXT,
                key_expires_at  REAL
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                md5_input       TEXT NOT NULL,
                prediction      TEXT NOT NULL,
                is_correct      INTEGER NOT NULL,
                timestamp_seed  TEXT NOT NULL,
                created_at      REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bias_weights (
                bucket          TEXT PRIMARY KEY,
                weight          REAL NOT NULL DEFAULT 0.0,
                total_feedback  INTEGER NOT NULL DEFAULT 0
            );
        """)
        conn.commit()
    logger.info("✅ Database sẵn sàng: %s", DB_PATH)


# ──────────────────── KEY helpers ────────────────────

def _calc_expires_from_label(label: str) -> Optional[float]:
    """Tính Unix timestamp hết hạn từ nhãn, bắt đầu từ NOW (lúc kích hoạt)."""
    mapping = {
        "1 ngày":    timedelta(days=1),
        "3 ngày":    timedelta(days=3),
        "7 ngày":    timedelta(days=7),
        "1 tháng":   timedelta(days=30),
        "1 năm":     timedelta(days=365),
        "Vĩnh viễn": None,
    }
    delta = mapping.get(label)
    if delta is None:
        return None
    return (datetime.now(timezone.utc) + delta).timestamp()


def db_create_key(label: str) -> str:
    """Sinh key mới, lưu DB, trả về chuỗi key."""
    key_code = f"BoKietvidai-{secrets.token_hex(8).upper()}"
    with _db_lock, _get_conn() as conn:
        conn.execute(
            "INSERT INTO keys (key_code, duration_label, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (key_code, label, None, time.time()),
        )
        conn.commit()
    return key_code


def db_activate_key(key_code: str, user_id: int) -> dict:
    """Kích hoạt key cho user. Trả về {"ok": bool, "msg": str, "expires_at": float|None}."""
    with _db_lock, _get_conn() as conn:
        row = conn.execute(
            "SELECT duration_label, expires_at, activated_by FROM keys WHERE key_code=?",
            (key_code,),
        ).fetchone()

        if row is None:
            return {"ok": False, "msg": "❌ Key không tồn tại!"}

        label, expires_at, activated_by = row

        # Key đã dùng bởi người khác
        if activated_by is not None and activated_by != user_id:
            return {"ok": False, "msg": "❌ Key này đã được người khác sử dụng!"}

        # Key đã hết hạn (lần kích hoạt đầu chưa xảy ra nên expires_at = None ở giai đoạn này)
        if expires_at is not None and time.time() > expires_at:
            return {"ok": False, "msg": "⏰ Key đã hết hạn!"}

        now = time.time()
        # Lần kích hoạt đầu: tính expires từ bây giờ
        if activated_by is None:
            expires_at = _calc_expires_from_label(label)
            conn.execute(
                "UPDATE keys SET activated_by=?, activated_at=?, expires_at=? WHERE key_code=?",
                (user_id, now, expires_at, key_code),
            )

        conn.execute(
            "INSERT OR REPLACE INTO users (user_id, active_key, key_expires_at) VALUES (?,?,?)",
            (user_id, key_code, expires_at),
        )
        conn.commit()

    return {"ok": True, "msg": "✅ Kích hoạt thành công!", "expires_at": expires_at}


def db_check_access(user_id: int) -> dict:
    """Kiểm tra quyền dùng MD5. Admin luôn có quyền."""
    if user_id == ADMIN_ID:
        return {"ok": True, "msg": "admin"}
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT active_key, key_expires_at FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not row or not row[0]:
        return {"ok": False, "msg": "no_key"}
    if row[1] is not None and time.time() > row[1]:
        return {"ok": False, "msg": "expired"}
    return {"ok": True, "msg": "active", "expires_at": row[1]}


def db_get_user_key_info(user_id: int) -> Optional[dict]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT active_key, key_expires_at FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return {"key": row[0], "expires_at": row[1]} if row else None


# ──────────────────── FEEDBACK / BIAS helpers ────────────────────

def db_save_feedback(user_id: int, md5_input: str, prediction: str,
                     is_correct: bool, ts_seed: str) -> None:
    bucket = _bias_bucket(md5_input)
    with _db_lock, _get_conn() as conn:
        conn.execute(
            "INSERT INTO feedback (user_id,md5_input,prediction,is_correct,timestamp_seed,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (user_id, md5_input, prediction, int(is_correct), ts_seed, time.time()),
        )
        # Cập nhật bias weight cho bucket
        sign = 1.0 if is_correct else -1.0
        conn.execute("""
            INSERT INTO bias_weights (bucket, weight, total_feedback) VALUES (?,?,1)
            ON CONFLICT(bucket) DO UPDATE SET
                weight = weight + ?,
                total_feedback = total_feedback + 1
        """, (bucket, sign, sign))
        conn.commit()


def db_get_bias(md5_input: str) -> float:
    """Trả về bias weight của bucket MD5 này (-∞..+∞, dương = ủng hộ dự đoán gốc)."""
    bucket = _bias_bucket(md5_input)
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT weight, total_feedback FROM bias_weights WHERE bucket=?",
            (bucket,),
        ).fetchone()
    if not row or row[1] == 0:
        return 0.0
    # Chuẩn hóa: trả về weight/total để scale [-1, 1]
    return row[0] / row[1]


def _bias_bucket(md5_input: str) -> str:
    """Gom MD5 vào bucket theo 2 ký tự đầu (256 bucket) để học pattern."""
    return md5_input[:2].lower()


# ═══════════════════════════════════════════════════════════════════
# 5. THUẬT TOÁN LÕI — MD5 + Timestamp Seed + Adaptive Bias
# ═══════════════════════════════════════════════════════════════════

def _shannon_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    freq = Counter(data)
    total = len(data)
    return -sum((c / total) * math.log2(c / total) for c in freq.values())


def _bitwise_score(seed_bytes: bytes) -> float:
    """
    Bitwise feature: tỷ lệ bit 1 trong seed.
    > 0.5 → thiên về TÁO, < 0.5 → thiên về XOÀI.
    """
    total_bits = len(seed_bytes) * 8
    set_bits = sum(bin(b).count("1") for b in seed_bytes)
    return set_bits / total_bits


def analyze_md5(md5_hex: str) -> dict:
    """
    Thuật toán phân tích 5 lớp:
    1. Seed = MD5 + Timestamp (giây) → ổn định trong cùng giây
    2. Hash Seed bằng SHA-256 → 32 bytes
    3. Mô phỏng 10 lượt tung 3 xúc xắc (dùng mod 6)
    4. Bitwise score + Shannon Entropy làm tín hiệu phụ
    5. Adaptive Bias từ lịch sử phản hồi điều chỉnh confidence
    """
    # Timestamp theo giây — cố định Seed trong khung 1 giây
    ts_seed = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Bước 1–2: Seed tổng hợp ──
    seed_raw = f"{md5_hex.lower()}|{ts_seed}"
    seed_hash = hashlib.sha256(seed_raw.encode()).digest()   # 32 bytes

    # ── Bước 3: Mô phỏng xúc xắc ──
    scores = []
    for i in range(0, 30, 3):
        d1 = (seed_hash[i]     % 6) + 1
        d2 = (seed_hash[i + 1] % 6) + 1
        d3 = (seed_hash[i + 2] % 6) + 1
        scores.append(d1 + d2 + d3)   # 10 lượt tung

    xoai = sum(1 for s in scores if s <= 10)
    tao  = sum(1 for s in scores if s >= 11)

    # ── Bước 4: Bitwise + Entropy ──
    bit_score = _bitwise_score(seed_hash)      # 0..1
    entropy   = _shannon_entropy(seed_hash)    # 0..8 bits

    # bit_score > 0.5 ủng hộ TÁO, < 0.5 ủng hộ XOÀI
    bit_signal = (bit_score - 0.5) * 2        # -1..+1

    # ── Bước 5: Raw confidence từ dice ──
    total = xoai + tao or 1
    raw_xoai_pct = xoai / total              # 0..1

    # Kết hợp dice + bitwise (trọng số 80/20)
    combined = 0.80 * raw_xoai_pct + 0.20 * (1 - (bit_signal + 1) / 2)

    # ── Bước 5b: Adaptive Bias ──
    bias = db_get_bias(md5_hex)              # -1..+1
    # bias dương → tăng xác suất dự đoán gốc; âm → giảm
    bias_factor = 0.05 * bias               # tối đa ±5% ảnh hưởng
    combined = max(0.0, min(1.0, combined + bias_factor))

    if combined > 0.5:
        prediction = "🍊 XOÀI"
        confidence = round(combined * 100, 2)
    elif combined < 0.5:
        prediction = "🍎 TÁO"
        confidence = round((1 - combined) * 100, 2)
    else:
        # Tie-breaker: byte cuối cùng
        prediction = "🍊 XOÀI" if seed_hash[31] % 2 == 0 else "🍎 TÁO"
        confidence = 50.0

    return {
        "prediction":  prediction,
        "confidence":  confidence,
        "entropy":     round(entropy, 4),
        "bit_score":   round(bit_score * 100, 2),
        "bias":        round(bias * 100, 2),
        "xoai_count":  xoai,
        "tao_count":   tao,
        "ts_seed":     ts_seed,
        "seed_preview": seed_hash.hex()[:12] + "...",
    }


# ═══════════════════════════════════════════════════════════════════
# 6. CONVERSATION STATES & IN-MEMORY FEEDBACK STORE
# ═══════════════════════════════════════════════════════════════════
S_MD5  = 1
S_KEY  = 2
S_TIER = 3

# Lưu context phản hồi: key = f"{user_id}_{message_id}"
# value = {"md5": str, "prediction": str, "ts_seed": str}
_pending_feedback: dict = {}
_pf_lock = threading.Lock()


def _store_pending(user_id: int, msg_id: int, md5: str, pred: str, ts: str):
    key = f"{user_id}_{msg_id}"
    with _pf_lock:
        _pending_feedback[key] = {"md5": md5, "prediction": pred, "ts_seed": ts}


def _pop_pending(user_id: int, msg_id: int) -> Optional[dict]:
    key = f"{user_id}_{msg_id}"
    with _pf_lock:
        return _pending_feedback.pop(key, None)


# ═══════════════════════════════════════════════════════════════════
# 7. KEYBOARDS & FORMAT HELPERS
# ═══════════════════════════════════════════════════════════════════

def main_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔍 [1] Phân tích MD5", callback_data="m_analyze")],
        [InlineKeyboardButton("🔑 [2] Nhập Key",       callback_data="m_enterkey")],
    ]
    if user_id == ADMIN_ID:
        rows.append([InlineKeyboardButton("⚙️ [3] Tạo Key  (Admin)", callback_data="m_createkey")])
    return InlineKeyboardMarkup(rows)


def tier_kb() -> InlineKeyboardMarkup:
    tiers = [
        ("1️⃣  1 ngày",    "t_1d"),
        ("2️⃣  3 ngày",    "t_3d"),
        ("3️⃣  7 ngày",    "t_7d"),
        ("4️⃣  1 tháng",   "t_1m"),
        ("5️⃣  1 năm",     "t_1y"),
        ("6️⃣  Vĩnh viễn", "t_inf"),
    ]
    rows = [[InlineKeyboardButton(lbl, callback_data=cb)] for lbl, cb in tiers]
    rows.append([InlineKeyboardButton("🔙 Quay lại", callback_data="m_back")])
    return InlineKeyboardMarkup(rows)


def feedback_kb(user_id: int, msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ ĐÚNG", callback_data=f"fb_ok_{user_id}_{msg_id}"),
        InlineKeyboardButton("❌ SAI",  callback_data=f"fb_no_{user_id}_{msg_id}"),
    ]])


def fmt_expire(ts: Optional[float]) -> str:
    if ts is None:
        return "♾️ Vĩnh viễn"
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M:%S")


def ascii_bar(pct: float, width: int = 18) -> str:
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


WELCOME = (
    "👋 *Chào mừng đến với BoKiet Dice Bot\\!*\n\n"
    "🎲 Phân tích & dự đoán xúc xắc qua chuỗi MD5\n\n"
    "🍊 *XOÀI* → Tổng 3 xúc xắc: 3–10 điểm\n"
    "🍎 *TÁO*  → Tổng 3 xúc xắc: 11–18 điểm\n\n"
    "👇 Chọn chức năng:"
)

TIER_LABELS = {
    "t_1d": "1 ngày", "t_3d": "3 ngày", "t_7d": "7 ngày",
    "t_1m": "1 tháng", "t_1y": "1 năm",  "t_inf": "Vĩnh viễn",
}

MD5_RE  = re.compile(r"^[0-9a-fA-F]{32}$")
KEY_RE  = re.compile(r"^BoKietvidai-[0-9A-Fa-f]{16}$")


# ═══════════════════════════════════════════════════════════════════
# 8. TELEGRAM HANDLERS
# ═══════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_chat.type != "private":
        return ConversationHandler.END
    await update.message.reply_text(
        WELCOME, parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_menu_kb(update.effective_user.id),
    )
    return ConversationHandler.END


# ──────── Menu callbacks ────────

async def cb_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    access = db_check_access(uid)
    if not access["ok"]:
        note = ("🔒 Chưa kích hoạt Key\\!\nChọn *\\[2\\] Nhập Key* trước\\."
                if access["msg"] == "no_key"
                else "⏰ Key đã hết hạn\\! Vui lòng nhập Key mới\\.")
        await q.edit_message_text(note, parse_mode=ParseMode.MARKDOWN_V2,
                                  reply_markup=main_menu_kb(uid))
        return ConversationHandler.END

    expire_note = ""
    if uid != ADMIN_ID:
        info = db_get_user_key_info(uid)
        if info:
            expire_note = f"\n⏳ Key hết hạn: `{fmt_expire(info['expires_at'])}`\n"

    await q.edit_message_text(
        f"🔍 *Phân tích MD5*{escape_md(expire_note)}\n\n"
        "📋 Gửi chuỗi MD5 \\(32 ký tự hex\\):\n"
        "_Ví dụ: `d41d8cd98f00b204e9800998ecf8427e`_",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return S_MD5


async def cb_enterkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🔑 *Nhập Key kích hoạt*\n\n"
        "Dán chuỗi Key vào đây:\n"
        "_Định dạng: `BoKietvidai-XXXXXXXXXXXXXXXX`_",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return S_KEY


async def cb_createkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        await q.edit_message_text("🚫 Không có quyền truy cập\\!",
                                  parse_mode=ParseMode.MARKDOWN_V2,
                                  reply_markup=main_menu_kb(q.from_user.id))
        return ConversationHandler.END
    await q.edit_message_text(
        "⚙️ *Tạo Key mới*\n\nChọn thời hạn:",
        parse_mode=ParseMode.MARKDOWN_V2, reply_markup=tier_kb(),
    )
    return S_TIER


async def cb_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        WELCOME, parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_menu_kb(q.from_user.id),
    )
    return ConversationHandler.END


async def cb_tier(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        await q.edit_message_text("🚫 Không có quyền\\!", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    label    = TIER_LABELS[q.data]
    key_code = db_create_key(label)
    expire_info = (
        f"`{label}` \\(tính từ lúc User kích hoạt\\)"
        if label != "Vĩnh viễn" else "♾️ Vĩnh viễn"
    )

    await q.edit_message_text(
        f"✅ *Key đã tạo thành công\\!*\n\n"
        f"🔑 Key:\n`{escape_md(key_code)}`\n\n"
        f"⏳ Thời hạn: {expire_info}\n\n"
        f"📋 _Copy key trên gửi cho người dùng\\._",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Tạo Key khác", callback_data="m_createkey")],
            [InlineKeyboardButton("🏠 Menu chính",   callback_data="m_back")],
        ]),
    )
    return ConversationHandler.END


# ──────── Nhận MD5 ────────

async def handle_md5(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid  = update.effective_user.id
    text = (update.message.text or "").strip()

    if not MD5_RE.match(text):
        await update.message.reply_text(
            "⚠️ *MD5 không hợp lệ\\!*\n\n"
            "Phải gồm đúng *32 ký tự Hex* \\(0\\-9, a\\-f\\)\\.\nGửi lại:",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return S_MD5  # Chờ nhập lại

    # Re-check quyền (key có thể hết hạn giữa chừng)
    access = db_check_access(uid)
    if not access["ok"]:
        await update.message.reply_text(
            "⏰ Key vừa hết hạn\\! Vui lòng nhập Key mới\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_menu_kb(uid),
        )
        return ConversationHandler.END

    proc_msg = await update.message.reply_text("⏳ Đang phân tích\\.\\.\\.",
                                               parse_mode=ParseMode.MARKDOWN_V2)

    r = analyze_md5(text)

    bar = ascii_bar(r["confidence"])
    pred_escaped = escape_md(r["prediction"])
    md5_escaped  = escape_md(text.lower())
    ts_escaped   = escape_md(r["ts_seed"])
    seed_escaped = escape_md(r["seed_preview"])
    bias_sign    = "\\+" if r["bias"] >= 0 else ""

    result_text = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 *KẾT QUẢ PHÂN TÍCH XÚC XẮC*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📥 MD5: `{md5_escaped}`\n"
        f"⏱ Seed TS: `{ts_escaped}`\n"
        f"🔐 Seed Hash: `{seed_escaped}`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*DỰ ĐOÁN:  {pred_escaped}*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Độ tin cậy:\n"
        f"`\\[{escape_md(bar)}\\]`\n"
        f"     *{r['confidence']}%*\n\n"
        f"📈 Shannon Entropy: `{r['entropy']} bits`\n"
        f"⚡ Bitwise Score: `{r['bit_score']}%`\n"
        f"🧠 Adaptive Bias: `{bias_sign}{r['bias']}%`\n\n"
        f"🍊 Mẫu XOÀI \\(3–10\\): `{r['xoai_count']}/10`\n"
        f"🍎 Mẫu TÁO  \\(11–18\\): `{r['tao_count']}/10`\n\n"
        "👇 *Kết quả có đúng không?*"
    )

    await proc_msg.delete()
    sent = await update.message.reply_text(
        result_text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=feedback_kb(uid, update.message.message_id),
    )

    # Lưu pending feedback (dùng message_id của tin người dùng gửi làm key duy nhất)
    _store_pending(uid, update.message.message_id, text.lower(), r["prediction"], r["ts_seed"])

    return ConversationHandler.END


# ──────── Nhận Key ────────

async def handle_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid  = update.effective_user.id
    text = (update.message.text or "").strip()

    if not KEY_RE.match(text):
        await update.message.reply_text(
            "⚠️ *Định dạng Key không đúng\\!*\n\n"
            "Key phải có dạng: `BoKietvidai\\-XXXXXXXXXXXXXXXX`\nGửi lại:",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return S_KEY

    result = db_activate_key(text, uid)
    if result["ok"]:
        exp = fmt_expire(result.get("expires_at"))
        await update.message.reply_text(
            f"✅ *Kích hoạt thành công\\!*\n\n"
            f"🔑 Key: `{escape_md(text)}`\n"
            f"⏳ Hết hạn: `{escape_md(exp)}`\n\n"
            f"🎉 Bạn có thể dùng *Phân tích MD5* ngay\\!",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_menu_kb(uid),
        )
        await _notify_admin(ctx, update.effective_user, text, exp)
    else:
        await update.message.reply_text(
            f"{escape_md(result['msg'])}\n\nLiên hệ Admin để được hỗ trợ\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_menu_kb(uid),
        )
    return ConversationHandler.END


# ──────── Feedback callbacks ────────

async def cb_feedback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q   = update.callback_query
    uid = q.from_user.id
    await q.answer()

    # Pattern: fb_ok_{user_id}_{msg_id}  hoặc  fb_no_{user_id}_{msg_id}
    parts = q.data.split("_")
    # parts: ['fb', 'ok'/'no', user_id, msg_id]
    if len(parts) != 4:
        return

    verdict   = parts[1]           # 'ok' hoặc 'no'
    owner_uid = int(parts[2])
    msg_id    = int(parts[3])

    # Chỉ chủ nhân được phản hồi
    if uid != owner_uid:
        await q.answer("🚫 Đây không phải phiên phân tích của bạn!", show_alert=True)
        return

    ctx_data = _pop_pending(uid, msg_id)
    if ctx_data is None:
        await q.edit_message_reply_markup(reply_markup=None)
        return

    is_correct = (verdict == "ok")
    db_save_feedback(
        user_id=uid,
        md5_input=ctx_data["md5"],
        prediction=ctx_data["prediction"],
        is_correct=is_correct,
        ts_seed=ctx_data["ts_seed"],
    )

    icon = "✅" if is_correct else "❌"
    fb_text = "ĐÚNG — Hệ thống đã ghi nhận, cảm ơn\\!" if is_correct else "SAI — Hệ thống đã học từ phản hồi này\\!"

    # Chỉnh sửa message cũ: xóa nút, thêm dòng phản hồi
    old_text = q.message.text or ""
    new_text = old_text.rsplit("👇", 1)[0].strip()
    await q.edit_message_text(
        new_text + f"\n\n{icon} *Phản hồi:* {fb_text}",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Phân tích tiếp", callback_data="m_analyze"),
            InlineKeyboardButton("🏠 Menu",            callback_data="m_back"),
        ]]),
    )


# ──────── Admin notify ────────

async def _notify_admin(ctx: ContextTypes.DEFAULT_TYPE, user, key: str, exp: str) -> None:
    try:
        uname = f"@{user.username}" if user.username else "_\\(không có username\\)_"
        await ctx.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "🔔 *Thông báo kích hoạt Key*\n\n"
                f"👤 User: {escape_md(user.full_name)} \\({uname}\\)\n"
                f"🆔 ID: `{user.id}`\n"
                f"🔑 Key: `{escape_md(key)}`\n"
                f"⏳ Hết hạn: `{escape_md(exp)}`\n"
                f"🕐 Lúc: `{escape_md(datetime.now().strftime('%d/%m/%Y %H:%M:%S'))}`"
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as e:
        logger.warning("Không gửi được thông báo Admin: %s", e)


# ──────── Admin command ────────

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Không có quyền\\!", parse_mode=ParseMode.MARKDOWN_V2)
        return
    with _get_conn() as conn:
        total_keys   = conn.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
        activated    = conn.execute("SELECT COUNT(*) FROM keys WHERE activated_by IS NOT NULL").fetchone()[0]
        total_users  = conn.execute("SELECT COUNT(*) FROM users WHERE active_key IS NOT NULL").fetchone()[0]
        now          = time.time()
        active_users = conn.execute(
            "SELECT COUNT(*) FROM users WHERE active_key IS NOT NULL"
            " AND (key_expires_at IS NULL OR key_expires_at > ?)", (now,)
        ).fetchone()[0]
        total_fb  = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        correct   = conn.execute("SELECT COUNT(*) FROM feedback WHERE is_correct=1").fetchone()[0]

    acc = round(correct / total_fb * 100, 1) if total_fb else 0.0
    await update.message.reply_text(
        "📊 *Thống kê hệ thống*\n\n"
        f"🔑 Tổng Key đã tạo:     `{total_keys}`\n"
        f"✅ Key đã kích hoạt:    `{activated}`\n"
        f"👥 Tổng User có Key:    `{total_users}`\n"
        f"🟢 User đang hoạt động: `{active_users}`\n\n"
        f"📈 Tổng phản hồi:       `{total_fb}`\n"
        f"🎯 Tỉ lệ dự đoán đúng: `{acc}%`\n\n"
        f"🕐 Cập nhật: `{escape_md(datetime.now().strftime('%d/%m/%Y %H:%M:%S'))}`",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_menu_kb(ADMIN_ID),
    )


# ──────── Fallback ────────

async def handle_unexpected(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.type == "private":
        await update.message.reply_text(
            "Dùng lệnh /start để mở menu\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_menu_kb(update.effective_user.id),
        )


# ──────── MarkdownV2 escape helper ────────

_MD2_SPECIAL = r"\_*[]()~`>#+-=|{}.!"

def escape_md(text: str) -> str:
    """Escape ký tự đặc biệt MarkdownV2 của Telegram."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


# ═══════════════════════════════════════════════════════════════════
# 9. BOT SETUP & DAEMON THREAD
# ═══════════════════════════════════════════════════════════════════

def _build_app() -> Application:
    """Xây dựng Application Telegram."""
    tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start",  cmd_start),
            CallbackQueryHandler(cb_analyze,   pattern="^m_analyze$"),
            CallbackQueryHandler(cb_enterkey,  pattern="^m_enterkey$"),
            CallbackQueryHandler(cb_createkey, pattern="^m_createkey$"),
            CallbackQueryHandler(cb_back,      pattern="^m_back$"),
            CallbackQueryHandler(cb_tier,      pattern="^t_"),
        ],
        states={
            S_MD5: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_md5),
                CallbackQueryHandler(cb_analyze,   pattern="^m_analyze$"),
                CallbackQueryHandler(cb_enterkey,  pattern="^m_enterkey$"),
                CallbackQueryHandler(cb_createkey, pattern="^m_createkey$"),
                CallbackQueryHandler(cb_back,      pattern="^m_back$"),
            ],
            S_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_key),
                CallbackQueryHandler(cb_back, pattern="^m_back$"),
            ],
            S_TIER: [
                CallbackQueryHandler(cb_tier, pattern="^t_"),
                CallbackQueryHandler(cb_back, pattern="^m_back$"),
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CommandHandler("admin", cmd_admin),
        ],
        per_message=False,
    )

    tg_app.add_handler(conv)
    tg_app.add_handler(CallbackQueryHandler(cb_feedback, pattern="^fb_"))
    tg_app.add_handler(CommandHandler("admin", cmd_admin))
    tg_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_unexpected)
    )

    return tg_app


def _run_bot_in_thread() -> None:
    """
    Chạy Bot Telegram trong event loop riêng biệt.
    nest_asyncio cho phép asyncio chạy bên trong thread có loop sẵn.
    """
    nest_asyncio.apply()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    tg_app = _build_app()

    async def _main():
        await tg_app.bot.set_my_commands([
            BotCommand("start", "Mở menu chính"),
            BotCommand("admin", "Thống kê (Admin only)"),
        ])
        logger.info("🤖 Telegram Bot đã kết nối! ADMIN_ID=%d", ADMIN_ID)
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        # Chạy mãi cho đến khi thread bị dừng
        while True:
            await asyncio.sleep(3600)

    try:
        loop.run_until_complete(_main())
    except Exception as e:
        logger.error("Bot thread lỗi: %s", e, exc_info=True)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════════════
# 10. ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════

def _validate() -> bool:
    ok = True
    if "YOUR_BOT_TOKEN" in TELEGRAM_BOT_TOKEN:
        logger.error("❌ Chưa điền TELEGRAM_BOT_TOKEN!")
        ok = False
    if ADMIN_ID == 123456789:
        logger.error("❌ Chưa điền ADMIN_ID hợp lệ!")
        ok = False
    return ok


# Khởi tạo DB ngay khi module được import (Gunicorn cần điều này)
db_init()

# Khởi động Bot thread ngay khi module load (Gunicorn worker init)
_bot_thread = threading.Thread(target=_run_bot_in_thread, daemon=True, name="TelegramBot")
_bot_thread.start()
logger.info("🚀 Bot thread đã được khởi động (daemon=True)")


if __name__ == "__main__":
    # Chạy local: python main.py
    if not _validate():
        sys.exit(1)
    port = int(os.environ.get("PORT", 5000))
    logger.info("🌐 Flask đang lắng nghe cổng %d ...", port)
    # Không dùng debug=True vì sẽ tạo thêm 1 process con → bot thread chạy 2 lần
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
