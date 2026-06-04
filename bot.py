"""
DarsBot — To'liq Telegram Ta'lim Boti
======================================
Kerakli kutubxonalar:
  pip install aiogram==3.x aiosqlite apscheduler python-dotenv

Fayl strukturasi:
  bot.py          ← Asosiy bot fayli (shu fayl)
  webapp/         ← student-map-webapp.html faylingiz shu papkada bo'lsin
  .env            ← BOT_TOKEN va ADMIN_GROUP_ID

.env misol:
  BOT_TOKEN=your_bot_token_here
  ADMIN_GROUP_ID=-100123456789
  WEBAPP_URL=https://akramovabubakir0617-debug.github.io/darsbot-webapp/webapp.html
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, WebAppInfo, ContentType
)
import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "8990085303:AAHpKoBhn-CB9s7zzEjg_RSoVQlVZCJAs7c")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "-1001003953780030"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://akramovabubakir0617-debug.github.io/darsbot-webapp/webapp.html")
DB_PATH = "darsbot.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ═══════════════════════════════════════════════════════════
# FSM STATES
# ═══════════════════════════════════════════════════════════
class Registration(StatesGroup):
    name = State()
    group = State()

class SubmitTask(StatesGroup):
    waiting_lesson = State()
    waiting_file = State()

class AdminStates(StatesGroup):
    adding_lesson = State()
    sending_attendance = State()

# ═══════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS students (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            full_name   TEXT NOT NULL,
            group_name  TEXT NOT NULL,
            total_score INTEGER DEFAULT 0,
            monthly_score INTEGER DEFAULT 0,
            streak      INTEGER DEFAULT 0,
            level       INTEGER DEFAULT 1,
            registered_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS lessons (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            description TEXT,
            lesson_num  INTEGER UNIQUE,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id  INTEGER REFERENCES students(user_id),
            lesson_id   INTEGER REFERENCES lessons(id),
            file_id     TEXT,
            file_type   TEXT,
            text_answer TEXT,
            status      TEXT DEFAULT 'pending',  -- pending, approved, rejected
            score       INTEGER DEFAULT 0,
            submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            reviewed_at  DATETIME,
            UNIQUE(student_id, lesson_id)
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER REFERENCES students(user_id),
            lesson_id  INTEGER REFERENCES lessons(id),
            marked_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(student_id, lesson_id)
        );

        CREATE TABLE IF NOT EXISTS monthly_winners (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            month    TEXT NOT NULL,
            rank     INTEGER,
            user_id  INTEGER,
            score    INTEGER
        );
        """)
        await db.commit()
    log.info("✅ Database tayyor")

async def get_student(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM students WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def register_student(user_id: int, username: str, full_name: str, group_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO students (user_id, username, full_name, group_name) VALUES (?,?,?,?)",
            (user_id, username, full_name, group_name)
        )
        await db.commit()

async def add_score(user_id: int, score: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE students SET total_score=total_score+?, monthly_score=monthly_score+? WHERE user_id=?",
            (score, score, user_id)
        )
        # Update level (every 50 points = 1 level)
        async with db.execute("SELECT total_score FROM students WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            if row:
                new_level = max(1, row[0] // 50 + 1)
                await db.execute("UPDATE students SET level=? WHERE user_id=?", (new_level, user_id))
        await db.commit()

async def get_leaderboard(monthly=True) -> list:
    col = "monthly_score" if monthly else "total_score"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM students ORDER BY {col} DESC LIMIT 10"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def get_student_rank(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*)+1 FROM students WHERE monthly_score > (SELECT monthly_score FROM students WHERE user_id=?)",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def get_lessons() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM lessons ORDER BY lesson_num") as cur:
            return [dict(r) for r in await cur.fetchall()]

async def get_student_tasks(user_id: int) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT t.*, l.title as lesson_title, l.lesson_num FROM tasks t JOIN lessons l ON t.lesson_id=l.id WHERE t.student_id=?",
            (user_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

# ═══════════════════════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════════════════════
def main_keyboard(webapp_url: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🗺 Mening Profilim", web_app=WebAppInfo(url=webapp_url))],
        [KeyboardButton(text="📤 Vazifa Yuborish"), KeyboardButton(text="📊 Reyting")],
        [KeyboardButton(text="📅 Davomat"), KeyboardButton(text="ℹ️ Yordam")],
    ], resize_keyboard=True)

def admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Kutayotgan Vazifalar"), KeyboardButton(text="➕ Dars Qo'shish")],
        [KeyboardButton(text="📢 Davomat Yuborish"), KeyboardButton(text="📊 Barcha Statistika")],
        [KeyboardButton(text="🏆 Oylik Natijalar"), KeyboardButton(text="🔄 Oyni Yakunla")],
    ], resize_keyboard=True)

def lessons_keyboard(lessons: list) -> InlineKeyboardMarkup:
    buttons = []
    for l in lessons:
        buttons.append([InlineKeyboardButton(
            text=f"📖 {l['lesson_num']}-dars: {l['title']}",
            callback_data=f"submit_lesson_{l['id']}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def grade_keyboard(task_id: int, student_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Qabul qilish (+10 ball)", callback_data=f"grade_approve_{task_id}_{student_id}"),
            InlineKeyboardButton(text="❌ Xato (0 ball)", callback_data=f"grade_reject_{task_id}_{student_id}"),
        ],
        [
            InlineKeyboardButton(text="⭐ 5 ball", callback_data=f"grade_5_{task_id}_{student_id}"),
            InlineKeyboardButton(text="⭐ 7 ball", callback_data=f"grade_7_{task_id}_{student_id}"),
            InlineKeyboardButton(text="⭐ 9 ball", callback_data=f"grade_9_{task_id}_{student_id}"),
        ]
    ])

def attendance_keyboard(lesson_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✋ Darsdaman!", callback_data=f"attend_{lesson_id}")
    ]])

# ═══════════════════════════════════════════════════════════
# /START — RO'YXATDAN O'TISH
# ═══════════════════════════════════════════════════════════
@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    student = await get_student(msg.from_user.id)
    if student:
        await msg.answer(
            f"👋 Xush kelibsiz, <b>{student['full_name']}</b>!\n"
            f"📚 Guruh: {student['group_name']}\n"
            f"⭐ Umumiy ball: {student['total_score']} | Oylik: {student['monthly_score']}",
            reply_markup=main_keyboard(WEBAPP_URL)
        )
    else:
        await msg.answer(
            "🎓 <b>DarsBot'ga xush kelibsiz!</b>\n\n"
            "Ro'yxatdan o'tish uchun to'liq ism-familiyangizni kiriting:\n"
            "<i>Misol: Abdullayev Alisher Ismoilovich</i>"
        )
        await state.set_state(Registration.name)

@router.message(Registration.name)
async def reg_name(msg: Message, state: FSMContext):
    if len(msg.text.split()) < 2:
        await msg.answer("❗ Iltimos, to'liq ism-familiyangizni kiriting (kamida 2 so'z).")
        return
    await state.update_data(full_name=msg.text.strip())
    await msg.answer(
        f"✅ <b>{msg.text}</b> — saqlandi!\n\n"
        "📚 Endi guruhingizni kiriting:\n<i>Misol: CS-101 yoki 2-kurs Informatika</i>"
    )
    await state.set_state(Registration.group)

@router.message(Registration.group)
async def reg_group(msg: Message, state: FSMContext):
    data = await state.get_data()
    full_name = data['full_name']
    group_name = msg.text.strip()
    await register_student(msg.from_user.id, msg.from_user.username, full_name, group_name)
    await state.clear()
    await msg.answer(
        f"🎉 <b>Ro'yxatdan muvaffaqiyatli o'tdingiz!</b>\n\n"
        f"👤 Ism: {full_name}\n"
        f"📚 Guruh: {group_name}\n\n"
        "🗺 Quydagi tugmalar orqali botdan foydalaning:",
        reply_markup=main_keyboard(WEBAPP_URL)
    )

# ═══════════════════════════════════════════════════════════
# VAZIFA YUBORISH
# ═══════════════════════════════════════════════════════════
@router.message(F.text == "📤 Vazifa Yuborish")
async def submit_task_start(msg: Message, state: FSMContext):
    student = await get_student(msg.from_user.id)
    if not student:
        await msg.answer("❗ Avval /start buyrug'i bilan ro'yxatdan o'ting.")
        return
    lessons = await get_lessons()
    if not lessons:
        await msg.answer("📭 Hozircha hech qanday dars qo'shilmagan. Ustozingizdan so'rang.")
        return
    await msg.answer("📖 Qaysi dars uchun vazifa yubormoqchisiz?", reply_markup=lessons_keyboard(lessons))
    await state.set_state(SubmitTask.waiting_lesson)

@router.callback_query(F.data.startswith("submit_lesson_"), SubmitTask.waiting_lesson)
async def submit_lesson_selected(call: CallbackQuery, state: FSMContext):
    lesson_id = int(call.data.split("_")[-1])
    await state.update_data(lesson_id=lesson_id)

    # Check if already submitted
    student_id = call.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT status FROM tasks WHERE student_id=? AND lesson_id=?",
            (student_id, lesson_id)
        ) as cur:
            existing = await cur.fetchone()

    if existing:
        status_map = {'pending': '⏳ Ko\'rib chiqilmoqda', 'approved': '✅ Qabul qilingan', 'rejected': '❌ Rad etilgan'}
        await call.answer(f"Bu dars uchun vazifangiz allaqachon yuborilgan: {status_map.get(existing[0])}", show_alert=True)
        await state.clear()
        return

    await call.message.edit_text(
        "📎 Vazifangizni yuboring:\n"
        "• Fayl (PDF, Word, rasm)\n"
        "• Video\n"
        "• Matn javob\n\n"
        "Qabul qilinadigan formatlar: rasm, video, hujjat, matn"
    )
    await state.set_state(SubmitTask.waiting_file)

@router.message(SubmitTask.waiting_file)
async def submit_file(msg: Message, state: FSMContext):
    data = await state.get_data()
    lesson_id = data.get('lesson_id')
    student_id = msg.from_user.id
    student = await get_student(student_id)

    file_id = None
    file_type = None
    text_answer = None

    if msg.document:
        file_id = msg.document.file_id
        file_type = 'document'
    elif msg.photo:
        file_id = msg.photo[-1].file_id
        file_type = 'photo'
    elif msg.video:
        file_id = msg.video.file_id
        file_type = 'video'
    elif msg.text:
        text_answer = msg.text
        file_type = 'text'
    else:
        await msg.answer("❗ Iltimos, fayl, rasm, video yoki matn yuboring.")
        return

    # Get lesson info
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)) as cur:
            lesson = dict(await cur.fetchone())

        await db.execute(
            "INSERT OR REPLACE INTO tasks (student_id, lesson_id, file_id, file_type, text_answer) VALUES (?,?,?,?,?)",
            (student_id, lesson_id, file_id, file_type, text_answer)
        )
        task_id = (await (await db.execute("SELECT last_insert_rowid()")).fetchone())[0]
        await db.commit()

    # Send to admin group
    caption = (
        f"📬 <b>Yangi vazifa yuborildi!</b>\n\n"
        f"👤 <b>Talaba:</b> {student['full_name']}\n"
        f"📚 <b>Guruh:</b> {student['group_name']}\n"
        f"📖 <b>Dars:</b> {lesson['lesson_num']}-dars — {lesson['title']}\n"
        f"📎 <b>Tur:</b> {file_type}\n"
        f"🕐 <b>Vaqt:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )
    try:
        if file_type == 'document':
            await bot.send_document(ADMIN_GROUP_ID, file_id, caption=caption, reply_markup=grade_keyboard(task_id, student_id))
        elif file_type == 'photo':
            await bot.send_photo(ADMIN_GROUP_ID, file_id, caption=caption, reply_markup=grade_keyboard(task_id, student_id))
        elif file_type == 'video':
            await bot.send_video(ADMIN_GROUP_ID, file_id, caption=caption, reply_markup=grade_keyboard(task_id, student_id))
        else:
            await bot.send_message(ADMIN_GROUP_ID, caption + f"\n\n💬 <b>Javob:</b>\n{text_answer}", reply_markup=grade_keyboard(task_id, student_id))
    except Exception as e:
        log.error(f"Admin guruhga yuborishda xato: {e}")

    await msg.answer(
        f"✅ <b>Vazifangiz muvaffaqiyatli yuborildi!</b>\n\n"
        f"📖 {lesson['lesson_num']}-dars: {lesson['title']}\n"
        f"⏳ Ustoz ko'rib chiqishi kutilmoqda...\n\n"
        f"Natija tayyor bo'lganda sizga xabar beramiz 🔔",
        reply_markup=main_keyboard(WEBAPP_URL)
    )
    await state.clear()

# ═══════════════════════════════════════════════════════════
# BAHOLASH (Admin)
# ═══════════════════════════════════════════════════════════
@router.callback_query(F.data.startswith("grade_"))
async def grade_task(call: CallbackQuery):
    parts = call.data.split("_")
    action = parts[1]
    task_id = int(parts[2])
    student_id = int(parts[3])

    score_map = {'approve': 10, 'reject': 0, '5': 5, '7': 7, '9': 9}
    score = score_map.get(action, 0)
    status = 'approved' if score > 0 else 'rejected'

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tasks SET status=?, score=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, score, task_id)
        )
        await db.commit()

    if score > 0:
        await add_score(student_id, score)

    # Notify student
    student = await get_student(student_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT t.*, l.title, l.lesson_num FROM tasks t JOIN lessons l ON t.lesson_id=l.id WHERE t.id=?",
            (task_id,)
        ) as cur:
            task = dict(await cur.fetchone())

    if status == 'approved':
        msg_text = (
            f"🎉 <b>Vazifangiz qabul qilindi!</b>\n\n"
            f"📖 {task['lesson_num']}-dars: {task['title']}\n"
            f"⭐ <b>Ball: +{score}</b>\n\n"
            f"Umumiy ballingiz yangilandi! 🗺 Profilingizni ko'ring."
        )
    else:
        msg_text = (
            f"❌ <b>Vazifa rad etildi</b>\n\n"
            f"📖 {task['lesson_num']}-dars: {task['title']}\n"
            f"💡 Qayta ko'rib, to'g'rilab yuboring."
        )

    try:
        await bot.send_message(student_id, msg_text, reply_markup=main_keyboard(WEBAPP_URL))
    except Exception as e:
        log.error(f"Talabaga xabar yuborishda xato: {e}")

    reviewer_name = call.from_user.full_name
    await call.message.edit_caption(
        call.message.caption + f"\n\n{'✅' if status=='approved' else '❌'} <b>{reviewer_name}</b> tomonidan baholandi: {score} ball"
    )
    await call.answer(f"{'✅ Qabul qilindi' if status=='approved' else '❌ Rad etildi'}: {score} ball")

# ═══════════════════════════════════════════════════════════
# REYTING
# ═══════════════════════════════════════════════════════════
@router.message(F.text == "📊 Reyting")
async def show_rating(msg: Message):
    leaders = await get_leaderboard(monthly=True)
    my_rank = await get_student_rank(msg.from_user.id)
    student = await get_student(msg.from_user.id)

    medals = ["🥇", "🥈", "🥉"]
    text = "🏆 <b>Oylik Reyting — TOP 10</b>\n\n"
    for i, s in enumerate(leaders):
        medal = medals[i] if i < 3 else f"{i+1}."
        me = " ← <b>SIZ</b>" if s['user_id'] == msg.from_user.id else ""
        text += f"{medal} {s['full_name']} — <b>{s['monthly_score']} ball</b>{me}\n"

    if student:
        text += f"\n\n👤 Sizning o'rningiz: <b>{my_rank}-o'rin</b>\n"
        text += f"⭐ Oylik ballingiz: <b>{student['monthly_score']}</b>"

    await msg.answer(text)

# ═══════════════════════════════════════════════════════════
# DAVOMAT
# ═══════════════════════════════════════════════════════════
@router.message(F.text == "📅 Davomat")
async def my_attendance(msg: Message):
    student_id = msg.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM attendance WHERE student_id=?", (student_id,)
        ) as cur:
            total = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM lessons") as cur:
            all_lessons = (await cur.fetchone())[0]

    pct = round(total / all_lessons * 100) if all_lessons else 0
    await msg.answer(
        f"📅 <b>Sizning Davomatingiz</b>\n\n"
        f"✅ Qatnashgan: {total}/{all_lessons} dars\n"
        f"📊 Foiz: {pct}%\n\n"
        f"{'🌟 Ajoyib!' if pct >= 80 else '⚠️ Davomatni yaxshilang!' if pct >= 50 else '❗ Juda kam qatnashydingiz!'}"
    )

@router.callback_query(F.data.startswith("attend_"))
async def mark_attendance(call: CallbackQuery):
    lesson_id = int(call.data.split("_")[1])
    student_id = call.from_user.id
    student = await get_student(student_id)
    if not student:
        await call.answer("❗ Avval ro'yxatdan o'ting: /start", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO attendance (student_id, lesson_id) VALUES (?,?)",
                (student_id, lesson_id)
            )
            await db.commit()
            await add_score(student_id, 2)  # 2 ball davomat uchun
            await call.answer("✅ Davomat belgilandi! +2 ball oldiniz!", show_alert=True)
            log.info(f"Davomat: {student['full_name']} — dars {lesson_id}")
        except aiosqlite.IntegrityError:
            await call.answer("ℹ️ Siz allaqachon davomat belgigandingiz.", show_alert=True)

# ═══════════════════════════════════════════════════════════
# ADMIN — DARS QO'SHISH
# ═══════════════════════════════════════════════════════════
@router.message(F.text == "➕ Dars Qo'shish")
async def add_lesson_start(msg: Message, state: FSMContext):
    if msg.chat.id != ADMIN_GROUP_ID and msg.from_user.id not in ADMINS:
        return
    await msg.answer("📖 Yangi dars qo'shish:\n\nFormat: <code>Dars_raqami|Nomi|Tavsif</code>\nMisol: <code>9|Massivlar|Massivlar bilan ishlash</code>")
    await state.set_state(AdminStates.adding_lesson)

@router.message(AdminStates.adding_lesson)
async def add_lesson_save(msg: Message, state: FSMContext):
    try:
        parts = msg.text.split("|")
        num, title, desc = int(parts[0].strip()), parts[1].strip(), parts[2].strip() if len(parts) > 2 else ""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO lessons (lesson_num, title, description) VALUES (?,?,?)",
                (num, title, desc)
            )
            await db.commit()
        await msg.answer(f"✅ {num}-dars qo'shildi: <b>{title}</b>")
    except Exception as e:
        await msg.answer(f"❗ Xato format. Misol: <code>9|Massivlar|Tavsif</code>")
    await state.clear()

# ═══════════════════════════════════════════════════════════
# ADMIN — DAVOMAT YUBORISH
# ═══════════════════════════════════════════════════════════
@router.message(F.text == "📢 Davomat Yuborish")
async def send_attendance_poll(msg: Message):
    lessons = await get_lessons()
    if not lessons:
        await msg.answer("❗ Hech qanday dars yo'q.")
        return
    # Use last lesson
    lesson = lessons[-1]
    # Get all students
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM students") as cur:
            students = [dict(r) for r in await cur.fetchall()]

    count = 0
    for s in students:
        try:
            await bot.send_message(
                s['user_id'],
                f"📢 <b>Davomat!</b>\n\n"
                f"📖 {lesson['lesson_num']}-dars: <b>{lesson['title']}</b>\n"
                f"⏰ <b>5 daqiqa ichida</b> tugmani bosing!",
                reply_markup=attendance_keyboard(lesson['id'])
            )
            count += 1
            await asyncio.sleep(0.05)  # rate limit
        except Exception as e:
            log.warning(f"Talabaga yuborib bo'lmadi: {s['user_id']}: {e}")

    await msg.answer(f"✅ Davomat so'rovi {count} ta talabaga yuborildi!")

# ═══════════════════════════════════════════════════════════
# ADMIN — STATISTIKA
# ═══════════════════════════════════════════════════════════
@router.message(F.text == "📊 Barcha Statistika")
async def all_stats(msg: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM students") as cur:
            total_students = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM tasks WHERE status='approved'") as cur:
            approved = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM tasks WHERE status='pending'") as cur:
            pending = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM attendance") as cur:
            total_attend = (await cur.fetchone())[0]

    await msg.answer(
        f"📊 <b>Umumiy Statistika</b>\n\n"
        f"👥 Talabalar soni: {total_students}\n"
        f"✅ Qabul qilingan vazifalar: {approved}\n"
        f"⏳ Kutayotgan vazifalar: {pending}\n"
        f"📅 Jami davomat belgilari: {total_attend}"
    )

# ═══════════════════════════════════════════════════════════
# OYLIK RESET — AVTOMATIK
# ═══════════════════════════════════════════════════════════
async def monthly_reset():
    """Har oyning oxirida ishga tushadi"""
    log.info("🔄 Oylik reset boshlandi...")
    month_label = datetime.now().strftime("%Y-%m")

    # Get top 3
    leaders = await get_leaderboard(monthly=True)
    winners_text = f"🏆 <b>{datetime.now().strftime('%B %Y')} — Oylik Natijalar!</b>\n\n"
    medals = ["🥇", "🥈", "🥉"]

    async with aiosqlite.connect(DB_PATH) as db:
        for i, w in enumerate(leaders[:5]):
            rank = i + 1
            medal = medals[i] if i < 3 else f"{rank}."
            winners_text += f"{medal} {w['full_name']} — {w['monthly_score']} ball\n"
            await db.execute(
                "INSERT INTO monthly_winners (month, rank, user_id, score) VALUES (?,?,?,?)",
                (month_label, rank, w['user_id'], w['monthly_score'])
            )

        # Reset monthly scores
        await db.execute("UPDATE students SET monthly_score=0")
        await db.execute("UPDATE students SET streak=0")  # optional: reset streak too
        await db.commit()

    winners_text += "\n\n🎁 TOP-3 g'oliblar maxsus sovg'a olishadi!\n🔄 Yangi musobaqa boshlandi — Omad tilaymiz!"

    # Notify admin
    try:
        await bot.send_message(ADMIN_GROUP_ID, winners_text)
    except Exception as e:
        log.error(f"Admin guruhga oylik natija yuborishda xato: {e}")

    # Notify all students
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM students") as cur:
            all_ids = [r[0] for r in await cur.fetchall()]

    for uid in all_ids:
        try:
            await bot.send_message(uid, winners_text + "\n\n⭐ Sizning oylik ballingiz 0 ga tushirildi. Yangi oyda yangi imkoniyat!")
            await asyncio.sleep(0.05)
        except:
            pass

    log.info(f"✅ Oylik reset tugadi. {len(all_ids)} talabaga xabar yuborildi.")

# ═══════════════════════════════════════════════════════════
# MANUAL RESET COMMAND
# ═══════════════════════════════════════════════════════════
@router.message(F.text == "🔄 Oyni Yakunla")
async def manual_reset(msg: Message):
    await msg.answer("⏳ Oylik natijalar hisoblanmoqda...")
    await monthly_reset()
    await msg.answer("✅ Oylik reset muvaffaqiyatli amalga oshirildi!")

@router.message(F.text == "🏆 Oylik Natijalar")
async def monthly_results(msg: Message):
    leaders = await get_leaderboard(monthly=True)
    medals = ["🥇", "🥈", "🥉"]
    text = f"🏆 <b>Joriy Oy Reytingi</b>\n\n"
    for i, s in enumerate(leaders[:5]):
        medal = medals[i] if i < 3 else f"{i+1}."
        text += f"{medal} {s['full_name']} — <b>{s['monthly_score']} ball</b>\n"
    text += "\n🎁 Har oyning oxirida TOP-3 sovg'a oladi!"
    await msg.answer(text)

# ═══════════════════════════════════════════════════════════
# YORDAM
# ═══════════════════════════════════════════════════════════
@router.message(F.text == "ℹ️ Yordam")
async def help_cmd(msg: Message):
    await msg.answer(
        "❓ <b>DarsBot — Yordam</b>\n\n"
        "🗺 <b>Mening Profilim</b> — Xaritangizni va statistikangizni ko'ring\n"
        "📤 <b>Vazifa Yuborish</b> — Darsga oid vazifangizni yuboring\n"
        "📊 <b>Reyting</b> — Top talabalar ro'yxati\n"
        "📅 <b>Davomat</b> — Davomatingizni ko'ring\n\n"
        "💡 <i>Har bir qabul qilingan vazifa uchun 10 ball,\nDavomat uchun 2 ball olasiz!</i>\n\n"
        "🏆 Oyning oxirida TOP-3 talaba sovg'a oladi!"
    )

# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
async def main():
    await init_db()

    # Scheduler — oylik reset
    scheduler = AsyncIOScheduler(timezone="Asia/Tashkent")
    scheduler.add_job(
        monthly_reset,
        'cron',
        day='last',      # Har oyning oxirgi kuni
        hour=23,
        minute=59
    )
    scheduler.start()
    log.info("⏰ Scheduler ishga tushdi")

    log.info("🤖 DarsBot ishga tushmoqda...")
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
