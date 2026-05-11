"""
THƯ KÝ CÁ NHÂN - Telegram Bot (Railway edition)
================================================
- Hiểu tiếng Việt tự nhiên (powered by Claude AI)
- Quản lý công việc theo ưu tiên
- Tự nhắc lịch 8:00 sáng mỗi ngày (APScheduler)
- Chạy 24/7 trên Railway
"""

import json
import os
import logging
from datetime import datetime, timezone, timedelta

import requests
import anthropic
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

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
TASKS_FILE   = os.path.join(BASE_DIR, "tasks.json")
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
THU    = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]

# ══════════════════════════════════════════════════════════════
#  UTILS
# ══════════════════════════════════════════════════════════════
def vn_now():
    return datetime.now(timezone(timedelta(hours=7)))

def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_tasks():
    return load_json(TASKS_FILE, [])

def save_tasks(tasks):
    save_json(TASKS_FILE, tasks)

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
        "description": "Thêm một công việc mới vào danh sách. Gọi khi chủ nhân giao việc hoặc đặt lịch.",
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
        "description": "Xóa một công việc khỏi danh sách.",
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
        save_tasks(tasks)
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
                    save_tasks(tasks)
                    return f"Đã đánh dấu xong: {t['tieu_de']}"
        return "Không tìm thấy công việc đó"

    elif name == "xoa_viec":
        active = [t for t in tasks if not t["done"]]
        idx = inp["stt"] - 1
        if 0 <= idx < len(active):
            rid = active[idx]["id"]
            ten = active[idx]["tieu_de"]
            tasks = [t for t in tasks if t["id"] != rid]
            save_tasks(tasks)
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
• Chủ nhân giao việc → gọi them_viec ngay
• Chủ nhân báo xong → gọi hoan_thanh
• Hỏi danh sách/lịch → gọi xem_danh_sach
• Muốn xóa → gọi xoa_viec | Muốn dọn dẹp → gọi don_dep
• Trả lời ngắn gọn 1–3 câu, thân thiện, dùng emoji
• Sau khi dùng tool → xác nhận kết quả cho chủ nhân"""

def process_message(user_text):
    history  = load_json(HISTORY_FILE, [])
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
    save_json(HISTORY_FILE, history[-40:])
    return reply

# ══════════════════════════════════════════════════════════════
#  HÀM NHẮC NHỞ CHUNG (dùng cho sáng / trưa / chiều)
# ══════════════════════════════════════════════════════════════
def send_reminder(session: str):
    """
    session = "morning" | "noon" | "evening"
    Chỉ gửi nếu còn task chưa xong.
    Buổi sáng luôn gửi (kể cả khi trống).
    Buổi trưa/chiều chỉ gửi khi còn task tồn đọng.
    """
    tasks  = get_tasks()
    active = [t for t in tasks if not t["done"]]
    now    = vn_now()

    # Buổi trưa/chiều — không gửi nếu không còn việc
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
        f"📅 <b>{THU[now.weekday()]}, {now.strftime('%d/%m/%Y %H:%M')}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    ]

    if not active:
        lines += ["", "✅ Không có việc gì tồn đọng.", "Hãy nhắn cho tôi nếu cần lên kế hoạch! 😊"]
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
        lines += ["", f"📌 Còn <b>{len(active)} việc</b> chưa xong — nhắn 'xong việc X' khi hoàn thành!"]

    lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━━", footer]
    send_telegram("\n".join(lines))
    logging.info(f"{session} reminder sent ({len(active)} active tasks)")

# ══════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════════════════
@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    logging.info(f"Webhook received: {data}")
    if not data:
        return "ok"

    msg = data.get("message") or data.get("edited_message")
    if not msg:
        return "ok"

    chat_id = msg.get("chat", {}).get("id")
    text    = msg.get("text", "").strip()
    logging.info(f"Message from chat_id={chat_id}, CHAT_ID={CHAT_ID}, text={text!r}")

    if chat_id != CHAT_ID or not text:
        logging.warning(f"Rejected: chat_id mismatch ({chat_id} != {CHAT_ID}) or empty text")
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

# ══════════════════════════════════════════════════════════════
#  KHỞI ĐỘNG
# ══════════════════════════════════════════════════════════════
def start_scheduler():
    from apscheduler.triggers.cron import CronTrigger
    import pytz
    vn_tz = pytz.timezone("Asia/Ho_Chi_Minh")
    scheduler = BackgroundScheduler()

    # 8:00 sáng — nhắc đầu ngày (luôn gửi)
    scheduler.add_job(lambda: send_reminder("morning"), CronTrigger(hour=8,  minute=0, timezone=vn_tz))
    # 12:00 trưa — nhắc giữa ngày (chỉ gửi nếu còn task)
    scheduler.add_job(lambda: send_reminder("noon"),    CronTrigger(hour=12, minute=0, timezone=vn_tz))
    # 17:00 chiều — nhắc cuối ngày (chỉ gửi nếu còn task)
    scheduler.add_job(lambda: send_reminder("evening"), CronTrigger(hour=17, minute=0, timezone=vn_tz))

    scheduler.start()
    logging.info("Scheduler started — reminders at 08:00 / 12:00 / 17:00 Asia/Ho_Chi_Minh")

# Chỉ khởi động scheduler 1 lần (tránh trùng khi Flask debug reload)
if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
