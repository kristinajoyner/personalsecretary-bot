"""
THƯ KÝ CÁ NHÂN - Telegram Bot (Railway + Google Tasks)
=====================================================
- Đọc task từ Google Tasks (bạn quản lý trực tiếp trên app)
- Nhắc việc lúc 8:00 / 12:00 / 17:00 giờ Việt Nam
- Không cần DB, không cần AI
"""

import os
import logging
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__)

BOT_TOKEN            = os.environ["BOT_TOKEN"]
CHAT_ID              = int(os.environ["CHAT_ID"])
GOOGLE_CLIENT_ID     = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]

THU = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]

# ══════════════════════════════════════════════════════════════
#  UTILS
# ══════════════════════════════════════════════════════════════
def vn_now():
    return datetime.now(timezone(timedelta(hours=7)))

def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=15)
        logging.info(f"Telegram sent: {r.status_code}")
    except Exception as e:
        logging.error(f"Telegram error: {e}")

# ══════════════════════════════════════════════════════════════
#  GOOGLE TASKS API
# ══════════════════════════════════════════════════════════════
def get_access_token():
    try:
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": GOOGLE_REFRESH_TOKEN,
            "grant_type":    "refresh_token"
        }, timeout=10)
        data = r.json()
        if "access_token" not in data:
            logging.error(f"Google token error: {data}")
            return None
        return data["access_token"]
    except Exception as e:
        logging.error(f"get_access_token error: {e}")
        return None

def get_google_tasks():
    token = get_access_token()
    if not token:
        return None

    all_tasks = []
    try:
        # Lấy tất cả task lists
        r = requests.get(
            "https://tasks.googleapis.com/tasks/v1/users/@me/lists",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10
        )
        lists = r.json().get("items", [])

        for task_list in lists:
            list_id = task_list["id"]
            r2 = requests.get(
                f"https://tasks.googleapis.com/tasks/v1/lists/{list_id}/tasks",
                headers={"Authorization": f"Bearer {token}"},
                params={"showCompleted": "false", "showHidden": "false"},
                timeout=10
            )
            tasks = r2.json().get("items", [])
            all_tasks.extend(tasks)
    except Exception as e:
        logging.error(f"get_google_tasks error: {e}")
        return None

    # Lọc bỏ task trống
    return [t for t in all_tasks if t.get("title", "").strip()]

# ══════════════════════════════════════════════════════════════
#  NHẮC NHỞ TỰ ĐỘNG (8:00 / 12:00 / 17:00)
# ══════════════════════════════════════════════════════════════
def send_reminder(session: str):
    now   = vn_now()
    tasks = get_google_tasks()

    headers = {
        "morning": ("🌅", "CHÀO BUỔI SÁNG!",      "💪 Chúc bạn ngày làm việc hiệu quả!"),
        "noon":    ("☀️", "NHẮC VIỆC BUỔI TRƯA",   "🍱 Tranh thủ giải quyết trước khi nghỉ trưa nhé!"),
        "evening": ("🌆", "NHẮC VIỆC BUỔI CHIỀU",  "🏁 Cố lên, còn một chút nữa là xong ngày!"),
    }
    icon, title, footer = headers[session]

    lines = [
        f"{icon} <b>{title}</b>",
        f"📅 <b>{THU[now.weekday()]}, {now.strftime('%d/%m/%Y')}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    ]

    if tasks is None:
        lines += ["", "⚠️ Không kết nối được Google Tasks. Kiểm tra lại cài đặt."]
    elif not tasks:
        lines += ["", "✅ Không có việc gì tồn đọng!", "Hãy thêm task mới trên Google Tasks nếu cần 😊"]
    else:
        lines.append("")
        for i, t in enumerate(tasks, 1):
            title_task = t.get("title", "").strip()
            due = t.get("due", "")
            due_str = ""
            if due:
                try:
                    due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                    due_str = f" ⏰{due_dt.strftime('%d/%m')}"
                except Exception:
                    pass
            lines.append(f"  {i}. {title_task}{due_str}")
        lines += ["", f"📌 Còn <b>{len(tasks)} việc</b> — vào Google Tasks để cập nhật!"]

    lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━━", footer]
    send_telegram("\n".join(lines))
    logging.info(f"{session} reminder sent ({len(tasks) if tasks else 0} tasks)")

# ══════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════════════════
@app.route("/")
def index():
    tasks = get_google_tasks()
    if tasks is None:
        return "⚠️ Bot đang chạy nhưng không kết nối được Google Tasks!", 500
    return f"🤖 Thư Ký Bot đang hoạt động! Tasks hiện tại: {len(tasks)}"

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

if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
