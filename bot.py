import os
import json
import math
import secrets
import hashlib
import logging
import threading
import asyncio
from datetime import datetime, timedelta
from collections import Counter
from typing import Optional

from flask import Flask, request, Response
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

# ═══════════════════════════════════════════════════════════
#  CẤU HÌNH & HẰNG SỐ
# ═══════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN   = os.environ.get("8931512528:AAE9CC1Kw_xRFO6QYJkQI6Su60dA7I0cDlQ", "")
ADMIN_ID    = 8284419367
PORT        = int(os.environ.get("PORT", 5000))
RENDER_URL  = os.environ.get("RENDER_EXTERNAL_URL", "")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}" if BOT_TOKEN else "/webhook"
WEBHOOK_URL  = f"{RENDER_URL}{WEBHOOK_PATH}"

# Đường dẫn file dữ liệu
DATA_DIR      = os.path.join(os.path.dirname(__file__), "data")
USERS_FILE    = os.path.join(DATA_DIR, "users.json")
KEYS_FILE     = os.path.join(DATA_DIR, "keys.json")
FEEDBACK_FILE = os.path.join(DATA_DIR, "feedback.json")
WEIGHTS_FILE  = os.path.join(DATA_DIR, "weights.json")

KEY_PREFIX = "BoKietvidai-"

# ═══════════════════════════════════════════════════════════
#  TẦNG DỮ LIỆU (JSON PERSISTENCE)
# ═══════════════════════════════════════════════════════════

_lock = threading.Lock()

def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

def _load(path: str, default):
    _ensure_data_dir()
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return default

def _save(path: str, data):
    _ensure_data_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_users()    -> dict: return _load(USERS_FILE, {})
def load_keys()     -> dict: return _load(KEYS_FILE, {})
def load_feedback() -> list: return _load(FEEDBACK_FILE, [])
def load_weights()  -> dict:
    default = {"bias": 0.5, "ema_alpha": 0.15, "total": 0, "correct": 0}
    return _load(WEIGHTS_FILE, default)

def save_users(d):    _save(USERS_FILE, d)
def save_keys(d):     _save(KEYS_FILE, d)
def save_feedback(d): _save(FEEDBACK_FILE, d)
def save_weights(d):  _save(WEIGHTS_FILE, d)

# ═══════════════════════════════════════════════════════════
#  QUẢN LÝ KEY & USER
# ═══════════════════════════════════════════════════════════

DURATION_MAP = {
    "1d":  timedelta(days=1),
    "3d":  timedelta(days=3),
    "7d":  timedelta(days=7),
    "1m":  timedelta(days=30),
    "1y":  timedelta(days=365),
    "inf": None,
}
DURATION_LABEL = {
    "1d": "1 ngày", "3d": "3 ngày", "7d": "7 ngày",
    "1m": "1 tháng", "1y": "1 năm", "inf": "Vĩnh viễn",
}

def generate_key() -> str:
    rand = secrets.token_urlsafe(16)
    return KEY_PREFIX + rand

def create_key(duration_code: str) -> str:
    with _lock:
        keys = load_keys()
        key = generate_key()
        now = datetime.utcnow()
        delta = DURATION_MAP.get(duration_code)
        expiry = (now + delta).isoformat() if delta else "infinity"
        keys[key] = {
            "created_at": now.isoformat(),
            "duration":   duration_code,
            "expiry":     expiry,
            "used_by":    None,
            "active":     True,
        }
        save_keys(keys)
    return key

def activate_key(user_id: int, key_str: str) -> tuple[bool, str]:
    with _lock:
        keys  = load_keys()
        users = load_users()
        uid   = str(user_id)

        if key_str not in keys:
            return False, "❌ Key không tồn tại."

        k = keys[key_str]

        if not k.get("active", True):
            return False, "❌ Key đã bị thu hồi."

        if k["used_by"] is not None and k["used_by"] != uid:
            return False, "❌ Key này đã được sử dụng bởi người khác."

        if k["expiry"] != "infinity":
            exp = datetime.fromisoformat(k["expiry"])
            if datetime.utcnow() > exp:
                return False, "❌ Key đã hết hạn."

        k["used_by"] = uid
        keys[key_str] = k

        users[uid] = {
            "user_id":      user_id,
            "key":          key_str,
            "key_expiry":   k["expiry"],
            "activated_at": datetime.utcnow().isoformat(),
            "state":        None,
        }
        save_keys(keys)
        save_users(users)

        label = DURATION_LABEL.get(k["duration"], k["duration"])
        return True, f"✅ Kích hoạt thành công! Hạn sử dụng: *{label}*"

def check_user_access(user_id: int) -> tuple[bool, str]:
    if user_id == ADMIN_ID:
        return True, "admin"
        
    users = load_users()
    uid   = str(user_id)

    if uid not in users:
        return False, "no_key"

    u = users[uid]
    exp = u.get("key_expiry", "infinity")
    if exp == "infinity":
        return True, "ok"

    keys = load_keys()
    key  = u.get("key", "")
    if key and not keys.get(key, {}).get("active", True):
        return False, "revoked"

    if datetime.utcnow() > datetime.fromisoformat(exp):
        return False, "expired"

    return True, "ok"

def revoke_key(key_str: str) -> tuple[bool, str]:
    with _lock:
        keys = load_keys()
        if key_str not in keys:
            return False, "Key không tồn tại."
        keys[key_str]["active"] = False
        save_keys(keys)
        return True, f"✅ Đã thu hồi key thành công."

def get_all_active_users() -> list:
    users = load_users()
    keys  = load_keys()
    result = []
    for uid, u in users.items():
        exp  = u.get("key_expiry", "infinity")
        key  = u.get("key", "")
        revoked = not keys.get(key, {}).get("active", True)
        if revoked:
            status = "🔴 Bị thu hồi"
        elif exp == "infinity":
            status = "🟢 Vĩnh viễn"
        elif datetime.utcnow() > datetime.fromisoformat(exp):
            status = "🟡 Hết hạn"
        else:
            status = f"🟢 Còn hạn → {exp[:10]}"
        result.append({"uid": uid, "status": status, "key": key[:20] + "..."})
    return result

def get_all_keys() -> list:
    keys = load_keys()
    result = []
    for k, v in keys.items():
        active  = v.get("active", True)
        used_by = v.get("used_by", "Chưa dùng") or "Chưa dùng"
        expiry  = v.get("expiry", "?")[:10] if v.get("expiry") != "infinity" else "Vĩnh viễn"
        result.append({
            "key":    k[:28] + "...",
            "active": "✅" if active else "❌",
            "used":   used_by,
            "expiry": expiry,
        })
    return result

# ═══════════════════════════════════════════════════════════
#  ENGINE PHÂN TÍCH MD5
# ═══════════════════════════════════════════════════════════

def _hex_to_bytes(md5: str) -> list[int]:
    return [int(md5[i:i+2], 16) for i in range(0, 32, 2)]

def _shannon_entropy(data: list[int]) -> float:
    n = len(data)
    counts = Counter(data)
    return -sum((c/n) * math.log2(c/n) for c in counts.values() if c > 0)

def _block_entropy(md5: str) -> list[float]:
    result = []
    for i in range(4):
        block = md5[i*8:(i+1)*8]
        bvals = [int(block[j:j+2], 16) for j in range(0, 8, 2)]
        result.append(_shannon_entropy(bvals))
    return result

def _positional_weighted_sum(bts: list[int]) -> int:
    fibs = [1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 233, 377, 610, 987]
    return sum(b * fibs[i % 16] for i, b in enumerate(bts))

def _simulate_dice(bts: list[int]) -> list[int]:
    rolls = []
    for i in range(10):
        b0 = bts[(i*3)     % 16]
        b1 = bts[(i*3 + 1) % 16]
        b2 = bts[(i*3 + 2) % 16]
        b0 = b0 ^ bts[i % 16]
        total = (b0 % 6 + 1) + (b1 % 6 + 1) + (b2 % 6 + 1)
        rolls.append(total)
    return rolls

def extract_features(md5: str) -> dict:
    md5 = md5.lower().strip()
    bts = _hex_to_bytes(md5)

    f1_entropy = _shannon_entropy(bts)
    even_sum = sum(bts[i] for i in range(0, 16, 2))
    odd_sum  = sum(bts[i] for i in range(1, 16, 2))
    f2_ratio = even_sum / (odd_sum + 1e-9)

    xor_val = 0
    for b in bts:
        xor_val ^= b
    f3_xor = xor_val / 255.0

    blk = _block_entropy(md5)
    f4_block_ratio = blk[-1] / (blk[0] + 1e-9)
    f5_pws = (_positional_weighted_sum(bts) % 11) / 10.0

    dice_rolls = _simulate_dice(bts)
    xoai_count = sum(1 for r in dice_rolls if r <= 10)
    f6_dice_ratio = xoai_count / 10.0

    return {
        "entropy":      f1_entropy,
        "even_odd":     f2_ratio,
        "xor_norm":     f3_xor,
        "block_ratio":  f4_block_ratio,
        "pws_norm":     f5_pws,
        "dice_ratio":   f6_dice_ratio,
        "dice_rolls":   dice_rolls,
        "xoai_count":   xoai_count,
        "tao_count":    10 - xoai_count,
    }

def predict(md5: str) -> dict:
    feats   = extract_features(md5)
    weights = load_weights()
    bias    = weights.get("bias", 0.5)

    s1 = 0.5
    s2 = 1.0 if feats["even_odd"] > 1.02 else (0.0 if feats["even_odd"] < 0.98 else 0.5)
    s3 = feats["xor_norm"]
    s4 = 1.0 if feats["block_ratio"] > 1.1 else (0.0 if feats["block_ratio"] < 0.9 else 0.5)
    s5 = feats["pws_norm"]
    s6 = feats["dice_ratio"]

    W = [0.05, 0.15, 0.20, 0.10, 0.15, 0.35]
    raw_score = (
        W[0]*s1 + W[1]*s2 + W[2]*s3 +
        W[3]*s4 + W[4]*s5 + W[5]*s6
    )

    adjustment = (bias - 0.5) * 0.3
    final_score = max(0.01, min(0.99, raw_score - adjustment))

    prediction = "XOÀI" if final_score >= 0.5 else "TÁO"
    confidence = final_score if prediction == "XOÀI" else (1 - final_score)
    confidence_pct = round(confidence * 100, 1)

    total   = weights.get("total", 0)
    correct = weights.get("correct", 0)
    acc     = round(correct / total * 100, 1) if total > 0 else 0.0

    return {
        "md5":          md5,
        "prediction":   prediction,
        "confidence":   confidence_pct,
        "raw_score":    round(raw_score, 4),
        "final_score":  round(final_score, 4),
        "entropy":      round(feats["entropy"], 4),
        "dice_rolls":   feats["dice_rolls"],
        "xoai_count":   feats["xoai_count"],
        "tao_count":    feats["tao_count"],
        "total_pred":   total,
        "accuracy":     acc,
    }

def update_weights(prediction: str, is_correct: bool):
    with _lock:
        w = load_weights()
        alpha = w.get("ema_alpha", 0.15)

        w["total"]   = w.get("total", 0) + 1
        w["correct"] = w.get("correct", 0) + (1 if is_correct else 0)

        if not is_correct:
            actual_is_xoai = (prediction == "TÁO")
        else:
            actual_is_xoai = (prediction == "XOÀI")

        target = 1.0 if actual_is_xoai else 0.0

        old_bias = w.get("bias", 0.5)
        new_bias = alpha * target + (1 - alpha) * old_bias
        w["bias"] = round(new_bias, 6)

        save_weights(w)

# ═══════════════════════════════════════════════════════════
#  ĐỊNH DẠNG TIN NHẮN & KEYBOARDS
# ═══════════════════════════════════════════════════════════

def format_result(res: dict) -> str:
    pred  = res["prediction"]
    emoji = "🍊" if pred == "XOÀI" else "🍎"
    range_txt = "(3–10 điểm)" if pred == "XOÀI" else "(11–18 điểm)"

    dice_str = " | ".join(
        ("🍊" if r <= 10 else "🍎") + str(r)
        for r in res["dice_rolls"]
    )

    acc_txt = f"{res['accuracy']}% ({res['total_pred']} lượt)" if res["total_pred"] > 0 else "Chưa có dữ liệu"

    return (
        f"📊 *KẾT QUẢ PHÂN TÍCH MD5*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔑 MD5: `{res['md5'][:16]}...{res['md5'][-8:]}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Dự đoán: *{emoji} {pred}* {range_txt}\n"
        f"📈 Độ tin cậy: *{res['confidence']}%*\n"
        f"🔬 Entropy: `{res['entropy']} bits`\n"
        f"⚙️ Method: Bitwise + EMA Adaptive\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎲 *Mô phỏng 10 lượt xúc xắc:*\n"
        f"`{dice_str}`\n"
        f"   🍊 Xoài: {res['xoai_count']}/10  |  🍎 Táo: {res['tao_count']}/10\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📉 Độ chính xác hệ thống: {acc_txt}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_Kết quả có đúng không? Bấm bên dưới nhé!_"
    )

def feedback_keyboard(md5: str, prediction: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ ĐÚNG", callback_data=f"fb|correct|{prediction}|{md5[:16]}"),
        InlineKeyboardButton("❌ SAI", callback_data=f"fb|wrong|{prediction}|{md5[:16]}"),
    ]])

def main_menu(user_id: int) -> InlineKeyboardMarkup:
    is_admin = (user_id == ADMIN_ID)
    rows = [
        [InlineKeyboardButton("📡 [1] Phân tích MD5", callback_data="menu|analyze")],
        [InlineKeyboardButton("🔑 [2] Nhập Key",       callback_data="menu|enter_key")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🛠 [3] Tạo Key (Admin)",   callback_data="menu|create_key")])
        rows.append([InlineKeyboardButton("👥 [4] Quản lý Users",     callback_data="menu|users")])
        rows.append([InlineKeyboardButton("📋 [5] Danh sách Keys",    callback_data="menu|keys")])
        rows.append([InlineKeyboardButton("🚫 [6] Thu hồi Key",       callback_data="menu|revoke")])
    return InlineKeyboardMarkup(rows)

def duration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1 Ngày",   callback_data="dur|1d"),
            InlineKeyboardButton("3 Ngày",   callback_data="dur|3d"),
            InlineKeyboardButton("7 Ngày",   callback_data="dur|7d"),
        ],
        [
            InlineKeyboardButton("1 Tháng",  callback_data="dur|1m"),
            InlineKeyboardButton("1 Năm",    callback_data="dur|1y"),
            InlineKeyboardButton("Vĩnh viễn",callback_data="dur|inf"),
        ],
        [InlineKeyboardButton("❌ Hủy",      callback_data="dur|cancel")],
    ])

# ═══════════════════════════════════════════════════════════
#  HANDLERS TELEGRAM
# ═══════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid  = user.id
    name = user.first_name or "bạn"

    ok, reason = check_user_access(uid)
    is_admin   = (uid == ADMIN_ID)

    if is_admin:
        role_txt = "👑 *Admin*"
    elif ok:
        role_txt = "✅ *Thành viên*"
    else:
        role_txt = "🔒 *Chưa kích hoạt*"

    text = (
        f"👋 Xin chào, *{name}*!\n\n"
        f"🤖 *Bot Phân Tích MD5 — Bồ Kiết Vĩ Đại*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Vai trò: {role_txt}\n"
        f"🆔 ID: `{uid}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Chọn chức năng bên dưới:"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu(uid))

async def cmd_huongdan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *HƯỚNG DẪN SỬ DỤNG BOT*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔵 *Bước 1: Kích hoạt tài khoản*\n"
        "   Nhấn `[2] Nhập Key` và nhập Key được Admin cấp.\n\n"
        "🔵 *Bước 2: Phân tích MD5*\n"
        "   Nhấn `[1] Phân tích MD5`, sau đó gửi chuỗi MD5 (32 ký tự hex).\n\n"
        "🔵 *Bước 3: Phản hồi kết quả*\n"
        "   Bấm `✅ ĐÚNG` hoặc `❌ SAI` để giúp bot học tập.\n\n"
        "📌 *Lệnh hỗ trợ:*\n"
        "• /start — Mở menu chính\n"
        "• /huongdan — Xem hướng dẫn này"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def menu_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    parts = query.data.split("|")
    action = parts[1]

    if action == "analyze":
        ok, reason = check_user_access(uid)
        if not ok:
            msgs = {
                "no_key":  "❌ Bạn chưa nhập Key. Dùng *[2] Nhập Key* để kích hoạt.",
                "expired": "⏰ Key của bạn đã *hết hạn*. Liên hệ Admin để gia hạn.",
                "revoked": "🚫 Key của bạn đã bị *thu hồi*. Liên hệ Admin.",
            }
            await query.edit_message_text(msgs.get(reason, "❌ Không có quyền truy cập."), parse_mode="Markdown")
            return
        ctx.user_data["state"] = "wait_md5"
        await query.edit_message_text(
            "📡 *Phân tích MD5*\n\nHãy gửi chuỗi MD5 cần phân tích (32 ký tự hex):\n\nGõ /start để quay lại.",
            parse_mode="Markdown"
        )

    elif action == "enter_key":
        ctx.user_data["state"] = "wait_key"
        await query.edit_message_text(
            "🔑 *Nhập Key kích hoạt*\n\nGửi Key bạn nhận được từ Admin:\n_(Dạng: `BoKietvidai-XXXXXXXXXXXXXXXX`)_\n\nGõ /start để quay lại.",
            parse_mode="Markdown"
        )

    elif action == "create_key":
        if uid != ADMIN_ID:
            await query.answer("⛔ Chỉ Admin mới dùng được!", show_alert=True)
            return
        await query.edit_message_text("🛠 *Tạo Key mới*\n\nChọn thời hạn:", parse_mode="Markdown", reply_markup=duration_keyboard())

    elif action == "users":
        if uid != ADMIN_ID:
            return
        users_list = get_all_active_users()
        txt = "👥 *Danh sách Users*\n\nChưa có user nào." if not users_list else \
              "👥 *Danh sách Users*\n━━━━━━━━━━━━━━\n" + "\n".join([f"🆔 `{u['uid']}` — {u['status']}" for u in users_list])
        await query.edit_message_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu|back")]]))

    elif action == "keys":
        if uid != ADMIN_ID:
            return
        keys_list = get_all_keys()
        txt = "📋 *Danh sách Keys*\n\nChưa tạo key nào." if not keys_list else \
              "📋 *Danh sách Keys*\n━━━━━━━━━━━━━━\n" + "\n".join([f"{k['active']} `{k['key']}` | HH: {k['expiry']}" for k in keys_list[-15:]])
        await query.edit_message_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu|back")]]))

    elif action == "revoke":
        if uid != ADMIN_ID:
            return
        ctx.user_data["state"] = "wait_revoke"
        await query.edit_message_text("🚫 *Thu hồi Key*\n\nGửi Key đầy đủ cần thu hồi:\n\nGõ /start để hủy.", parse_mode="Markdown")

    elif action == "back":
        await query.edit_message_text("📋 Chọn chức năng:", reply_markup=main_menu(uid))

async def duration_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    code = query.data.split("|")[1]

    if uid != ADMIN_ID:
        return

    if code == "cancel":
        await query.edit_message_text("❌ Đã hủy tạo key.", reply_markup=main_menu(uid))
        return

    new_key = create_key(code)
    label   = DURATION_LABEL.get(code, code)
    await query.edit_message_text(
        f"✅ *Key mới đã được tạo!*\n\n🔑 Key:\n`{new_key}`\n\n⏱ Thời hạn: *{label}*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Về menu", callback_data="menu|back")]])
    )

async def feedback_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cb_key = "fb_done_" + query.data[:40]
    if ctx.user_data.get(cb_key):
        await query.answer("Bạn đã phản hồi rồi!", show_alert=True)
        return
    ctx.user_data[cb_key] = True

    parts      = query.data.split("|")
    verdict    = parts[1]
    prediction = parts[2]
    is_correct = (verdict == "correct")

    update_weights(prediction, is_correct)

    with _lock:
        fb = load_feedback()
        fb.append({
            "ts": datetime.utcnow().isoformat(),
            "user_id": query.from_user.id,
            "prediction": prediction,
            "correct": is_correct,
        })
        save_feedback(fb)

    w   = load_weights()
    acc = round(w["correct"] / w["total"] * 100, 1) if w["total"] > 0 else 0.0
    result_txt = "✅ Chính xác!" if is_correct else "❌ Chưa đúng!"

    new_text = (
        f"{query.message.text}\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 *Phản hồi:* {result_txt}\n"
        f"📊 Tỷ lệ đúng hệ thống: *{acc}%* ({w['total']} lượt)"
    )
    try:
        await query.edit_message_text(new_text, parse_mode="Markdown")
    except Exception:
        pass

async def message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    text  = (update.message.text or "").strip()
    state = ctx.user_data.get("state")

    if state == "wait_md5":
        if len(text) != 32 or not all(c in "0123456789abcdefABCDEF" for c in text):
            await update.message.reply_text("⚠️ MD5 không hợp lệ! Cần đúng 32 ký tự Hex.", parse_mode="Markdown")
            return

        res = predict(text.lower())
        ctx.user_data["state"] = None
        await update.message.reply_text(
            format_result(res),
            parse_mode="Markdown",
            reply_markup=feedback_keyboard(text.lower(), res["prediction"])
        )

    elif state == "wait_key":
        ok, msg = activate_key(uid, text)
        ctx.user_data["state"] = None
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu(uid))

    elif state == "wait_revoke":
        if uid != ADMIN_ID:
            ctx.user_data["state"] = None
            return
        ok, msg = revoke_key(text)
        ctx.user_data["state"] = None
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu(uid))

    else:
        await update.message.reply_text("💡 Dùng /start để mở menu.", reply_markup=main_menu(uid))

# ═══════════════════════════════════════════════════════════
#  FLASK WEB SERVER & RUNNER
# ═══════════════════════════════════════════════════════════

flask_app = Flask(__name__)
_telegram_app: Optional[Application] = None
_loop: Optional[asyncio.AbstractEventLoop] = None

@flask_app.route("/", methods=["GET"])
def index():
    return Response("<h2>🤖 Bot Phân Tích MD5 — Bồ Kiết Vĩ Đại</h2><p>Status: ONLINE</p>", status=200)

@flask_app.route("/health", methods=["GET"])
def health():
    return Response('{"status":"ok"}', status=200, mimetype="application/json")

@flask_app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    """Route xử lý đồng bộ để tránh lỗi Async của Flask"""
    if _telegram_app is None or _loop is None:
        return Response("Not ready", status=503)
    
    data = request.get_json(force=True)
    update = Update.de_json(data, _telegram_app.bot)
    
    # Đẩy tác vụ xử lý update vào event loop đang chạy ngầm
    asyncio.run_coroutine_threadsafe(_telegram_app.process_update(update), _loop)
    return Response("ok", status=200)

def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).updater(None).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("huongdan", cmd_huongdan))

    app.add_handler(CallbackQueryHandler(menu_callback,     pattern=r"^menu\|"))
    app.add_handler(CallbackQueryHandler(duration_callback, pattern=r"^dur\|"))
    app.add_handler(CallbackQueryHandler(feedback_callback, pattern=r"^fb\|"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    return app

async def setup_webhook(app: Application):
    await app.initialize()
    if RENDER_URL:
        await app.bot.set_webhook(
            url=WEBHOOK_URL,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )
        logger.info(f"✅ Webhook set: {WEBHOOK_URL}")
    
    await app.start()
    await app.bot.set_my_commands([
        BotCommand("start",    "Mở menu chính"),
        BotCommand("huongdan", "Hướng dẫn sử dụng"),
    ])

def run_loop(loop, app):
    asyncio.set_event_loop(loop)
    loop.run_until_complete(setup_webhook(app))
    loop.run_forever()

def main():
    global _telegram_app, _loop

    if not BOT_TOKEN:
        raise RuntimeError("❌ TELEGRAM_BOT_TOKEN chưa được đặt!")
    if ADMIN_ID == 0:
        raise RuntimeError("❌ ADMIN_ID chưa được đặt!")

    _telegram_app = build_application()
    _loop = asyncio.new_event_loop()

    # Chạy asyncio loop trên 1 thread riêng biệt
    t = threading.Thread(target=run_loop, args=(_loop, _telegram_app), daemon=True)
    t.start()

    logger.info(f"🚀 Flask đang chạy trên port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
