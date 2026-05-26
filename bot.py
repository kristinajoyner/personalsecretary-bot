"""
THƯ KÝ CÁ NHÂN - Telegram Bot (Railway + PostgreSQL)
=====================================================
- Lưu task vào PostgreSQL → không mất khi redeploy
- Nhắc việc lúc 8:00 / 12:00 / 17:00 giờ Việt Nam
- Task chưa xong → nhắc mãi đến khi xác nhận hoàn thành
"""

import json
import os
import logging
from datetime import datetime, timezone, timedelta

import requests
import anthropic
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__)

# ══════════════════════════════════════════════════════════════
#  CẤU HÌNH
# ══════════════════════════════════════════════════════════════
BOT_TOKEN     = os.environ["BOT_TOKEN"]
CHAT_ID       = int(os.environ["CHAT_ID"])
ANTHROPIC_KEY = os.environ["ANTHROPIC_KEY"]
DATABASE_URL  = os.environ["DATABASE_URL"]

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
THU    = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]

# ══════════════════════════════════════════════════════════════
#  DATABASE — lưu trữ vĩnh viễn, không mất khi redeploy
# ══════════════════════════════════════════════════════════════
def get_conn():
    # sslmode=disable: Railway internal network không cần SSL, disable rõ ràng hơn prefer
    return psycopg2.connect(DATABASE_URL, sslmode="disable", connect_timeout=10)

def init_db():
    """Tạo bảng nếu chưa có"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS store (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
        conn.commit()
    logging.info("Database initialized")

def db_get(key, default):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM store WHERE key = %s", (key,))
                row = cur.fetchone()
                if row:
                    return json.loads(row[0])
    except Exception as e:
        logging.error(f"DB get error [{key}]: {e}")
    return default

def db_set(key, value):
    """Lưu dữ liệu vào DB — retry 3 lần nếu thất bại"""
    import time
    json_val = json.dumps(value, ensure_ascii=False)
    for attempt in range(3):
        try:
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO store (key, value) VALUES (%s, %s)
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """, (key, json_val))
                conn.commit()
            finally:
                conn.close()
            # Xác nhận lại sau khi save
            saved = db_get(key, None)
            if saved is not None:
                logging.info(f"DB set OK [{key}] attempt={attempt+1}")
                return True
            else:
                logging.warning(f"DB set verify failed [{key}] attempt={attempt+1}")
        except Exception as e:
            logging.error(f"DB set error [{key}] attempt={attempt+1}: {e}")
        time.sleep(1)
    # Tất cả 3 lần đều thất bại → báo cho người dùng
    logging.error(f"DB set FAILED after 3 attempts [{key}]")
    send_telegram(f"⚠️ Lỗi nghiêm trọng: không lưu được dữ liệu [{key}] vào DB sau 3 lần thử! Hãy báo lỗi này.")
    return False

def get_tasks():
    return db_get("tasks", [])

def save_tasks(tasks):
    return db_set("tasks", tasks)

# ══════════════════════════════════════════════════════════════
#  UTILS
# ══════════════════════════════════════════════════════════════
def vn_now():
    return datetime.now(timezone(timedelta(hours=7)))

def send_telegram(text, chat_id=CHAT_ID):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=15)
        return r.json()
    except Exception as e:
        logging.error(f"Telegram error: {e}")

# ══════════════════════════════════════════════════════════════
#  CLAUDE TOOLS
# ══════════════════════════════════════════════════════════════
TOOLS = [
    {
        "name": "them_viec",
        "description": "Thêm một công việc mới vào danh sách. Gọi ngay khi chủ nhân giao việc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tieu_de":  {"type": "string",  "description": "Tên công việc"},
                "uu_tien":  {"type": "integer", "description": "1=🔴Khẩn cấp, 2=🟡Quan trọng, 3=🟢Thấp", "enum": [1, 2, 3]},
                "deadline": {"type": "string",  "description": "Thời hạn nếu có, ví dụ '10:00', '15/05'. Bỏ trống nếu không có."}
            },
            "required": ["tieu_de", "uu_tien"]
        }
    },
    {
        "name": "hoan_thanh",
        "description": "Đánh dấu xong một công việc. Gọi khi chủ nhân báo đã làm xong.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stt": {"type": "integer", "description": "Số thứ tự trong danh sách việc chưa xong (bắt đầu từ 1)"}
            },
            "required": ["stt"]
        }
    },
    {
        "name": "xoa_viec",
        "description": "Xóa hẳn một công việc khỏi danh sách.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stt": {"type": "integer", "description": "Số thứ tự trong danh sách việc chưa xong"}
            },
            "required": ["stt"]
        }
    },
    {
        "name": "xem_danh_sach",
        "description": "Xem toàn bộ danh sách công việc hiện tại.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "don_dep",
        "description": "Xóa tất cả công việc đã hoàn thành khỏi danh sách.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "xoa_het",
        "description": "Xóa TOÀN BỘ danh sách công việc và bắt đầu lại từ đầu. Gọi khi chủ nhân nói: xóa hết, reset, làm mới, bắt đầu lại, clear all.",
        "input_schema": {"type": "object", "properties": {}}
    }
]

def handle_tool(name, inp):
    tasks = get_tasks()
    P = {1: "🔴", 2: "🟡", 3: "🟢"}

    if name == "them_viec":
        new_id = (max(t["id"] for t in tasks) + 1) if tasks else 1
        task = {
            "id":      new_id,
            "tieu_de": inp["tieu_de"],
            "uu_tien": inp.get("uu_tien", 2),
            "deadline": inp.get("deadline", ""),
            "done":    False,
            "tao_luc": vn_now().strftime("%d/%m %H:%M")
        }
        tasks.append(task)
        ok = save_tasks(tasks)
        if not ok:
            return f"❌ LỖI: Không lưu được task '{task['tieu_de']}' vào DB! Hãy thử lại."
        p_label = {1: "Khẩn cấp", 2: "Quan trọng", 3: "Thấp"}.get(task["uu_tien"], "")
        dl = f" | Deadline: {task['deadline']}" if task.get("deadline") else ""
        return f"Đã thêm: {task['tieu_de']} ({p_label}{dl})"

    elif name == "hoan_thanh":
        active = [t for t in tasks if not t["done"]]
        idx = inp["stt"] - 1
        if 0 <= idx < len(active):
            for t in tasks:
                if t["id"] == active[idx]["id"]:
                    t["done"] = True
                    ok = save_tasks(tasks)
                    if not ok:
                        return f"❌ LỖI: Không lưu được trạng thái hoàn thành cho '{t['tieu_de']}' vào DB!"
                    return f"✅ Đã đánh dấu xong: {t['tieu_de']}"
        return "Không tìm thấy công việc đó"

    elif name == "xoa_viec":
        active = [t for t in tasks if not t["done"]]
        idx = inp["stt"] - 1
        if 0 <= idx < len(active):
            rid = active[idx]["id"]
            ten = active[idx]["tieu_de"]
            tasks = [t for t in tasks if t["id"] != rid]
            ok = save_tasks(tasks)
            if not ok:
                return f"❌ LỖI: Không xóa được task '{ten}' khỏi DB!"
            return f"Đã xóa: {ten}"
        return "Không tìm thấy công việc đó"

    elif name == "xem_danh_sach":
        active = [t for t in tasks if not t["done"]]
        done   = [t for t in tasks if t["done"]]
        if not tasks:
            return "Danh sách trống, chưa có việc gì."
        lines = []
        if active:
            lines.append("📋 VIỆC CẦN LÀM:")
            for i, t in enumerate(active, 1):
                dl = f" ⏰{t['deadline']}" if t.get("deadline") else ""
                lines.append(f"  {i}. {P.get(t['uu_tien'], '🟡')} {t['tieu_de']}{dl}")
        if done:
            lines.append(f"\n✅ Đã xong: {len(done)} việc")
        return "\n".join(lines)

    elif name == "don_dep":
        before = len(tasks)
        tasks = [t for t in tasks if not t["done"]]
        removed = before - len(tasks)
        save_tasks(tasks)
        return f"Đã dọn {removed} việc đã hoàn thành."

    elif name == "xoa_het":
        save_tasks([])
        db_set("history", [])
        return "Đã xóa toàn bộ danh sách và lịch sử. Danh sách trống, sẵn sàng bắt đầu lại!"

    return "Không rõ lệnh"

# ══════════════════════════════════════════════════════════════
#  XỬ LÝ TIN NHẮN VỚI CLAUDE
# ══════════════════════════════════════════════════════════════
def build_system():
    tasks  = get_tasks()
    now    = vn_now()
    active = [t for t in tasks if not t["done"]]
    P      = {1: "🔴Khẩn", 2: "🟡Quan trọng", 3: "🟢Thấp"}

    viec = "\n".join(
        f"  {i}. [{P.get(t['uu_tien'],'🟡')}] {t['tieu_de']}"
        + (f" ⏰{t['deadline']}" if t.get("deadline") else "")
        for i, t in enumerate(active, 1)
    ) if active else "  (Chưa có việc gì)"

    return f"""Bạn là "Ky" — thư ký cá nhân thông minh, thân thiện, chuyên nghiệp. Luôn giao tiếp bằng tiếng Việt.

🕐 THỜI GIAN: {THU[now.weekday()]}, {now.strftime('%d/%m/%Y %H:%M')}

📋 VIỆC CẦN LÀM HIỆN TẠI:
{viec}

NGUYÊN TẮC:
• Chủ nhân giao việc → gọi them_viec NGAY, không hỏi lại
• Chủ nhân báo xong / hoàn thành / done → gọi hoan_thanh NGAY
• Hỏi danh sách / còn việc gì → gọi xem_danh_sach
• Muốn xóa → gọi xoa_viec | Dọn dẹp → gọi don_dep | Reset toàn bộ → gọi xoa_het
• Trả lời ngắn gọn 1–3 câu, thân thiện, dùng emoji
• Sau khi dùng tool → xác nhận kết quả cho chủ nhân"""

def process_message(user_text):
    history  = db_get("history", [])
    messages = history[-20:] + [{"role": "user", "content": user_text}]

    for _ in range(5):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=build_system(),
            tools=TOOLS,
            messages=messages
        )
        if response.stop_reason != "tool_use":
            break

        assistant_content = []
        tool_results = []
        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use", "id": block.id,
                    "name": block.name, "input": block.input
                })
                result = handle_tool(block.name, block.input)
                logging.info(f"Tool {block.name}: {result}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user",      "content": tool_results})

    reply = " ".join(
        block.text for block in response.content if block.type == "text"
    ).strip() or "✅ Đã ghi nhận!"

    history.append({"role": "user",      "content": user_text})
    history.append({"role": "assistant", "content": reply})
    db_set("history", history[-40:])
    return reply

# ══════════════════════════════════════════════════════════════
#  NHẮC NHỞ TỰ ĐỘNG (8:00 / 12:00 / 17:00)
# ══════════════════════════════════════════════════════════════
def send_reminder(session: str):
    tasks  = get_tasks()
    active = [t for t in tasks if not t["done"]]
    now    = vn_now()

    if session != "morning" and not active:
        logging.info(f"{session} reminder skipped — no pending tasks")
        return

    headers = {
        "morning": ("🌅", "CHÀO BUỔI SÁNG!", "💪 Chúc bạn ngày làm việc hiệu quả!"),
        "noon":    ("☀️", "NHẮC VIỆC BUỔI TRƯA", "🍱 Tranh thủ giải quyết trước khi nghỉ trưa nhé!"),
        "evening": ("🌆", "NHẮC VIỆC BUỔI CHIỀU", "🏁 Cố lên, còn một chút nữa là xong ngày!"),
    }
    icon, title, footer = headers[session]

    lines = [
        f"{icon} <b>{title}</b>",
        f"📅 <b>{THU[now.weekday()]}, {now.strftime('%d/%m/%Y')}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    ]

    if not active:
        lines += ["", "✅ Không có việc gì tồn đọng!", "Hãy nhắn cho tôi nếu cần lên kế hoạch 😊"]
    else:
        groups = {1: [], 2: [], 3: []}
        for t in active:
            groups[t["uu_tien"]].append(t)
        labels = {1: "🔴 KHẨN CẤP", 2: "🟡 QUAN TRỌNG", 3: "🟢 NẾU CÒN GIỜ"}
        for p in [1, 2, 3]:
            if groups[p]:
                lines.append(f"\n<b>{labels[p]}:</b>")
                for t in groups[p]:
                    dl = f" ⏰{t['deadline']}" if t.get("deadline") else ""
                    lines.append(f"  • {t['tieu_de']}{dl}")
        lines += ["", f"📌 Còn <b>{len(active)} việc</b> chưa xong — nhắn 'xong việc X' để đánh dấu hoàn thành!"]

    lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━━", footer]
    send_telegram("\n".join(lines))
    logging.info(f"{session} reminder sent ({len(active)} active tasks)")

# ══════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════════════════
@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if not data:
        return "ok"

    msg = data.get("message") or data.get("edited_message")
    if not msg:
        return "ok"

    chat_id = msg.get("chat", {}).get("id")
    text    = msg.get("text", "").strip()

    logging.info(f"Message from chat_id={chat_id}, text={text!r}")

    if chat_id != CHAT_ID or not text:
        return "ok"

    try:
        reply = process_message(text)
        send_telegram(reply, chat_id)
    except Exception as e:
        logging.error(f"Error: {e}", exc_info=True)
        send_telegram("❌ Có lỗi xảy ra, thử lại sau nhé!", chat_id)

    return "ok"

@app.route("/")
def index():
    return "🤖 Thư Ký Bot đang hoạt động!"

@app.route("/ping")
def ping():
    """Kiểm tra DB nhanh — truy cập URL/ping để xem số task hiện tại"""
    try:
        tasks  = get_tasks()
        active = [t for t in tasks if not t["done"]]
        done   = [t for t in tasks if t["done"]]
        return {
            "status": "ok",
            "total_tasks": len(tasks),
            "active": len(active),
            "done": len(done),
            "active_titles": [t["tieu_de"] for t in active]
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}, 500

# ══════════════════════════════════════════════════════════════
#  KHỞI ĐỘNG
# ══════════════════════════════════════════════════════════════
def start_scheduler():
    from apscheduler.triggers.cron import CronTrigger
    import pytz
    vn_tz = pytz.timezone("Asia/Ho_Chi_Minh")
    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: send_reminder("morning"), CronTrigger(hour=8,  minute=0, timezone=vn_tz))
    scheduler.add_job(lambda: send_reminder("noon"),    CronTrigger(hour=12, minute=0, timezone=vn_tz))
    scheduler.add_job(lambda: send_reminder("evening"), CronTrigger(hour=17, minute=0, timezone=vn_tz))
    scheduler.start()
    logging.info("Scheduler started — 08:00 / 12:00 / 17:00 Asia/Ho_Chi_Minh")

init_db()

if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
