"""
auth_db.py
Хранилище пользователей, тарифов, сессий и аудита для TaxMasterKZ — SQLite.

Таблицы:
  users          — логин, хэш пароля (bcrypt), роль, статус, срок доступа,
                   тариф, лимит/использование анализов, оплата, контакты
  plans          — справочник тарифов (DEMO/START/PRO/TEAM/ENTERPRISE),
                   редактируется администратором из приложения
  login_logs     — журнал попыток входа (успех/неуспех)
  uploaded_files — какие файлы какой пользователь загрузил
  analyses       — журнал сформированных анализов по пользователю
  audit_logs     — общий журнал действий (вход, загрузка, анализ, отчёт,
                   удаление, изменения тарифа/доступа админом и т.д.)
  app_settings   — настройки уровня приложения (в т.ч. maintenance_mode)

Пароли НИКОГДА не хранятся в открытом виде — только bcrypt-хэш через passlib.
Все функции безопасны к повторному вызову (idempotent create), безопасны при
конкурентном доступе нескольких пользователей (короткие транзакции SQLite).

Миграция v3 -> v4: init_db() безопасно добавляет новые колонки в уже
существующую таблицу users через ALTER TABLE ... ADD COLUMN (если колонки
уже есть — пропускает). Существующие пользователи, пароли, роли, блокировки
и журналы НЕ удаляются и не пересоздаются. У пользователей, созданных ещё в
v3 (без тарифа), поле `plan` остаётся NULL — для них тарифные ограничения
(лимит анализов, водяной знак, запрет Excel) НЕ применяются, чтобы ничего не
сломать; администратор может назначить им тариф вручную в любой момент.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from passlib.context import CryptContext

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.path.join(DATA_DIR, "taxmasterkz.db")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ROLES = ["admin", "user", "demo"]
STATUSES = ["active", "blocked"]
PAYMENT_STATUSES = ["trial", "paid", "unpaid", "overdue", "cancelled"]
EXCEL_MODES = ["none", "basic", "full"]

_DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _is_leap(y: int) -> bool:
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


# --------------------------------------------------------------------------
# тарифы по умолчанию (см. ТЗ v4, раздел 1) — сеются в таблицу plans один
# раз (INSERT OR IGNORE), дальше администратор может менять их прямо в
# приложении, и эти изменения переживают перезапуск/обновление
# --------------------------------------------------------------------------

PLAN_DEFAULTS = [
    dict(code="DEMO", name="DEMO", price=0, duration_days=7, analysis_limit=2, max_users=1,
         allow_word=1, allow_pdf=1, excel_mode="none", watermark=1,
         description="Для тестирования возможностей сервиса", active=1, sort_order=1),
    dict(code="START", name="START", price=59000, duration_days=30, analysis_limit=20, max_users=1,
         allow_word=1, allow_pdf=1, excel_mode="basic", watermark=0,
         description="Для индивидуального специалиста", active=1, sort_order=2),
    dict(code="PRO", name="PRO", price=129000, duration_days=30, analysis_limit=100, max_users=1,
         allow_word=1, allow_pdf=1, excel_mode="full", watermark=0,
         description="Для активной работы и подготовки справок. Расширенный риск-анализ и история анализов.",
         active=1, sort_order=3),
    dict(code="TEAM", name="TEAM", price=250000, duration_days=30, analysis_limit=300, max_users=5,
         allow_word=1, allow_pdf=1, excel_mode="full", watermark=0,
         description="Командный доступ до 5 пользователей. Полный экспорт, журнал действий.",
         active=1, sort_order=4),
    dict(code="ENTERPRISE", name="ENTERPRISE", price=None, duration_days=None, analysis_limit=None, max_users=None,
         allow_word=1, allow_pdf=1, excel_mode="full", watermark=0,
         description="Индивидуальные условия для крупных организаций и закрытых серверов. "
                      "Срок и лимиты задаёт администратор вручную.",
         active=1, sort_order=5),
]


@contextmanager
def _conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _table_columns(c, table: str) -> set[str]:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_users_table(c):
    """Безопасно добавляет новые колонки тарифной системы в users, если их ещё нет.
    Ничего не удаляет и не переписывает — старые данные (пароли, роли, блокировки) целы."""
    cols = _table_columns(c, "users")
    new_columns = [
        ("plan", "TEXT"),
        ("access_start_date", "TEXT"),
        ("access_end_date", "TEXT"),
        ("analysis_limit", "INTEGER"),
        ("analysis_used", "INTEGER NOT NULL DEFAULT 0"),
        ("payment_status", "TEXT"),
        ("company_name", "TEXT"),
        ("phone", "TEXT"),
        ("email", "TEXT"),
        ("notes", "TEXT"),
        ("created_by", "TEXT"),
        ("updated_at", "TEXT"),
    ]
    for col_name, col_def in new_columns:
        if col_name not in cols:
            c.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")


def _migrate_audit_logs_table(c):
    cols = _table_columns(c, "audit_logs")
    new_columns = [("admin_id", "INTEGER"), ("admin_username", "TEXT"), ("ip", "TEXT")]
    for col_name, col_def in new_columns:
        if col_name not in cols:
            c.execute(f"ALTER TABLE audit_logs ADD COLUMN {col_name} {col_def}")


def init_db():
    """Создаёт таблицы при первом запуске, безопасно мигрирует схему при
    обновлении с более старой версии и (если пользователей ещё нет) заводит
    первого администратора из переменных окружения ADMIN_USERNAME /
    ADMIN_PASSWORD (см. .env.example). Если пользователи уже есть (сервис
    обновляется с v3 на v4) — администратор НЕ создаётся заново и пароли
    существующих пользователей не трогаются."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                status TEXT NOT NULL DEFAULT 'active',
                expiry_date TEXT,
                created_at TEXT NOT NULL,
                last_login TEXT
            )
        """)
        _migrate_users_table(c)

        c.execute("""
            CREATE TABLE IF NOT EXISTS plans (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                price INTEGER,
                duration_days INTEGER,
                analysis_limit INTEGER,
                max_users INTEGER,
                allow_word INTEGER NOT NULL DEFAULT 1,
                allow_pdf INTEGER NOT NULL DEFAULT 1,
                excel_mode TEXT NOT NULL DEFAULT 'none',
                watermark INTEGER NOT NULL DEFAULT 0,
                description TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER DEFAULT 0
            )
        """)
        for p in PLAN_DEFAULTS:
            c.execute("""
                INSERT OR IGNORE INTO plans
                (code, name, price, duration_days, analysis_limit, max_users,
                 allow_word, allow_pdf, excel_mode, watermark, description, active, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (p["code"], p["name"], p["price"], p["duration_days"], p["analysis_limit"], p["max_users"],
                  p["allow_word"], p["allow_pdf"], p["excel_mode"], p["watermark"], p["description"],
                  p["active"], p["sort_order"]))

        c.execute("""
            CREATE TABLE IF NOT EXISTS login_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                success INTEGER NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS uploaded_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                category TEXT,
                stored_path TEXT,
                size_bytes INTEGER,
                uploaded_at TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                summary TEXT,
                created_at TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                action TEXT NOT NULL,
                details TEXT,
                timestamp TEXT NOT NULL
            )
        """)
        _migrate_audit_logs_table(c)

        c.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        n_users = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if n_users == 0:
            admin_user = os.environ.get("ADMIN_USERNAME", "admin")
            admin_pass = os.environ.get("ADMIN_PASSWORD", "changeme123")
            _create_user_locked(c, admin_user, admin_pass, role="admin", status="active", expiry_date=None,
                                 created_by="system")

    _ensure_setting("maintenance_mode", "0")


def _ensure_setting(key: str, default: str):
    with _conn() as c:
        row = c.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        if row is None:
            c.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)", (key, default))


def _create_user_locked(c, username: str, password: str, role: str, status: str, expiry_date,
                         plan: str | None = None, access_start_date: str | None = None,
                         access_end_date: str | None = None, analysis_limit: int | None = None,
                         analysis_used: int = 0, payment_status: str | None = None,
                         company_name: str | None = None, phone: str | None = None,
                         email: str | None = None, notes: str | None = None,
                         created_by: str | None = None):
    password_hash = pwd_context.hash(password)
    now = datetime.utcnow().isoformat()
    c.execute(
        "INSERT INTO users (username, password_hash, role, status, expiry_date, created_at, "
        "plan, access_start_date, access_end_date, analysis_limit, analysis_used, payment_status, "
        "company_name, phone, email, notes, created_by, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (username, password_hash, role, status, expiry_date, now,
         plan, access_start_date, access_end_date, analysis_limit, analysis_used, payment_status,
         company_name, phone, email, notes, created_by, now),
    )


# --------------------------------------------------------------------------
# пользователи
# --------------------------------------------------------------------------

def create_user(username: str, password: str, role: str = "user",
                 status: str = "active", expiry_date: str | None = None,
                 plan: str | None = None, access_start_date: str | None = None,
                 access_end_date: str | None = None, analysis_limit: int | None = None,
                 payment_status: str | None = None, company_name: str | None = None,
                 phone: str | None = None, email: str | None = None, notes: str | None = None,
                 created_by: str | None = None) -> tuple[bool, str]:
    if role not in ROLES:
        return False, f"Недопустимая роль: {role}"
    if status not in STATUSES:
        return False, f"Недопустимый статус: {status}"
    if not username or not password:
        return False, "Логин и пароль обязательны"

    # Если указан тариф — автоматически проставляем даты доступа, лимит
    # анализов и статус оплаты по умолчанию этого тарифа (см. ТЗ v4 п.4.2),
    # если они не заданы явно администратором.
    if plan:
        p = get_plan(plan)
        if p:
            if access_start_date is None:
                access_start_date = date.today().isoformat()
            if access_end_date is None and p["duration_days"]:
                access_end_date = (date.today() + timedelta(days=int(p["duration_days"]))).isoformat()
            if analysis_limit is None:
                analysis_limit = p["analysis_limit"]
            if payment_status is None:
                payment_status = "trial" if plan == "DEMO" else "unpaid"
            if expiry_date is None:
                expiry_date = access_end_date

    try:
        with _conn() as c:
            _create_user_locked(c, username, password, role, status, expiry_date,
                                 plan=plan, access_start_date=access_start_date, access_end_date=access_end_date,
                                 analysis_limit=analysis_limit, analysis_used=0, payment_status=payment_status,
                                 company_name=company_name, phone=phone, email=email, notes=notes,
                                 created_by=created_by)
        return True, "Пользователь создан"
    except sqlite3.IntegrityError:
        return False, f"Пользователь «{username}» уже существует"


def get_user(username: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def set_password(username: str, new_password: str) -> bool:
    with _conn() as c:
        pw_hash = pwd_context.hash(new_password)
        cur = c.execute("UPDATE users SET password_hash=?, updated_at=? WHERE username=?",
                         (pw_hash, datetime.utcnow().isoformat(), username))
        return cur.rowcount > 0


def set_status(username: str, status: str) -> bool:
    if status not in STATUSES:
        return False
    with _conn() as c:
        cur = c.execute("UPDATE users SET status=?, updated_at=? WHERE username=?",
                         (status, datetime.utcnow().isoformat(), username))
        return cur.rowcount > 0


def set_expiry(username: str, expiry_date: str | None) -> bool:
    """Устаревшая (v3) установка срока доступа — синхронизируется с access_end_date."""
    with _conn() as c:
        cur = c.execute("UPDATE users SET expiry_date=?, access_end_date=?, updated_at=? WHERE username=?",
                         (expiry_date, expiry_date, datetime.utcnow().isoformat(), username))
        return cur.rowcount > 0


def set_role(username: str, role: str) -> bool:
    if role not in ROLES:
        return False
    with _conn() as c:
        cur = c.execute("UPDATE users SET role=?, updated_at=? WHERE username=?",
                         (role, datetime.utcnow().isoformat(), username))
        return cur.rowcount > 0


def delete_user(username: str) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM users WHERE username=?", (username,))
        return cur.rowcount > 0


def _touch_last_login(username: str):
    with _conn() as c:
        c.execute("UPDATE users SET last_login=? WHERE username=?", (datetime.utcnow().isoformat(), username))


# --------------------------------------------------------------------------
# тарифы: назначение, продление, лимиты, оплата, контакты
# --------------------------------------------------------------------------

def get_plans(active_only: bool = False) -> list[dict]:
    with _conn() as c:
        q = "SELECT * FROM plans"
        if active_only:
            q += " WHERE active=1"
        q += " ORDER BY sort_order"
        rows = c.execute(q).fetchall()
        return [dict(r) for r in rows]


def get_plans_dict() -> dict[str, dict]:
    return {p["code"]: p for p in get_plans()}


def get_plan(code: str | None) -> dict | None:
    if not code:
        return None
    with _conn() as c:
        row = c.execute("SELECT * FROM plans WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None


def update_plan(code: str, **fields) -> bool:
    """Позволяет администратору менять цену/лимит/срок/описание тарифа
    прямо из приложения — правки сохраняются в БД и переживают перезапуск."""
    allowed = {"name", "price", "duration_days", "analysis_limit", "max_users",
               "allow_word", "allow_pdf", "excel_mode", "watermark", "description", "active"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    with _conn() as c:
        cols = ", ".join(f"{k}=?" for k in sets)
        cur = c.execute(f"UPDATE plans SET {cols} WHERE code=?", (*sets.values(), code))
        return cur.rowcount > 0


def set_plan(username: str, plan_code: str | None, reset_dates: bool = True) -> bool:
    """Меняет тариф пользователя. При reset_dates=True (по умолчанию при явной
    смене тарифа) — дата начала = сегодня, дата окончания и лимит анализов
    пересчитываются заново по параметрам нового тарифа, счётчик использованных
    анализов обнуляется."""
    plan = get_plan(plan_code) if plan_code else None
    now = datetime.utcnow().isoformat()
    with _conn() as c:
        if plan_code and plan and reset_dates:
            start = date.today().isoformat()
            end = (date.today() + timedelta(days=int(plan["duration_days"]))).isoformat() if plan["duration_days"] else None
            c.execute(
                "UPDATE users SET plan=?, access_start_date=?, access_end_date=?, expiry_date=?, "
                "analysis_limit=?, analysis_used=0, updated_at=? WHERE username=?",
                (plan_code, start, end, end, plan["analysis_limit"], now, username),
            )
        else:
            c.execute("UPDATE users SET plan=?, updated_at=? WHERE username=?", (plan_code, now, username))
    return True


def extend_access(username: str, months: int | None = None, years: int | None = None,
                   days: int | None = None) -> tuple[bool, str | None]:
    """Продлевает доступ. Если текущий срок ещё не истёк — продление считается
    от текущей даты окончания; если уже истёк (или не задан) — от сегодняшней
    даты (см. ТЗ v4 п.12)."""
    user = get_user(username)
    if not user:
        return False, None
    base_str = user.get("access_end_date") or user.get("expiry_date")
    base = None
    if base_str:
        try:
            base = datetime.fromisoformat(base_str).date()
        except ValueError:
            base = None
    today = date.today()
    start = base if (base and base >= today) else today

    if months:
        total_month = start.month - 1 + months
        year = start.year + total_month // 12
        month = total_month % 12 + 1
        max_day = 29 if (month == 2 and _is_leap(year)) else _DAYS_IN_MONTH[month - 1]
        day = min(start.day, max_day)
        new_end = start.replace(year=year, month=month, day=day)
    elif years:
        try:
            new_end = start.replace(year=start.year + years)
        except ValueError:
            new_end = start.replace(year=start.year + years, day=28)
    elif days:
        new_end = start + timedelta(days=days)
    else:
        return False, None

    new_end_str = new_end.isoformat()
    with _conn() as c:
        c.execute("UPDATE users SET access_end_date=?, expiry_date=?, updated_at=? WHERE username=?",
                   (new_end_str, new_end_str, datetime.utcnow().isoformat(), username))
    return True, new_end_str


def set_access_end_date(username: str, end_date: str | None) -> bool:
    with _conn() as c:
        cur = c.execute("UPDATE users SET access_end_date=?, expiry_date=?, updated_at=? WHERE username=?",
                         (end_date, end_date, datetime.utcnow().isoformat(), username))
        return cur.rowcount > 0


def set_analysis_limit(username: str, new_limit: int | None) -> bool:
    with _conn() as c:
        cur = c.execute("UPDATE users SET analysis_limit=?, updated_at=? WHERE username=?",
                         (new_limit, datetime.utcnow().isoformat(), username))
        return cur.rowcount > 0


def reset_analysis_used(username: str) -> bool:
    with _conn() as c:
        cur = c.execute("UPDATE users SET analysis_used=0, updated_at=? WHERE username=?",
                         (datetime.utcnow().isoformat(), username))
        return cur.rowcount > 0


def add_analysis_quota(username: str, n: int) -> int | None:
    """Увеличивает лимит анализов пользователя на n (кнопки +10/+50/+100).
    Возвращает новый лимит."""
    with _conn() as c:
        row = c.execute("SELECT analysis_limit FROM users WHERE username=?", (username,)).fetchone()
        if row is None:
            return None
        cur_limit = row["analysis_limit"]
        new_limit = (cur_limit or 0) + n
        c.execute("UPDATE users SET analysis_limit=?, updated_at=? WHERE username=?",
                   (new_limit, datetime.utcnow().isoformat(), username))
        return new_limit


def set_payment_status(username: str, status: str) -> bool:
    if status not in PAYMENT_STATUSES:
        return False
    with _conn() as c:
        cur = c.execute("UPDATE users SET payment_status=?, updated_at=? WHERE username=?",
                         (status, datetime.utcnow().isoformat(), username))
        return cur.rowcount > 0


def update_contact_info(username: str, company_name: str | None = None, phone: str | None = None,
                         email: str | None = None, notes: str | None = None) -> bool:
    with _conn() as c:
        cur = c.execute("SELECT company_name, phone, email, notes FROM users WHERE username=?", (username,)).fetchone()
        if cur is None:
            return False
        c.execute(
            "UPDATE users SET company_name=?, phone=?, email=?, notes=?, updated_at=? WHERE username=?",
            (company_name if company_name is not None else cur["company_name"],
             phone if phone is not None else cur["phone"],
             email if email is not None else cur["email"],
             notes if notes is not None else cur["notes"],
             datetime.utcnow().isoformat(), username),
        )
    return True


def increment_analysis_used(user_id: int) -> int:
    """Увеличивает счётчик выполненных анализов на 1. Вызывать ТОЛЬКО при
    реальном запуске нового анализа/пересчёта базы — не при просмотре
    страниц, повторном открытии готового анализа или скачивании отчёта."""
    with _conn() as c:
        c.execute("UPDATE users SET analysis_used = COALESCE(analysis_used, 0) + 1, updated_at=? WHERE id=?",
                   (datetime.utcnow().isoformat(), user_id))
        row = c.execute("SELECT analysis_used FROM users WHERE id=?", (user_id,)).fetchone()
        return row["analysis_used"] if row else 0


def check_analysis_quota(user: dict) -> tuple[bool, str, int | None]:
    """Возвращает (можно_запускать, сообщение, остаток_анализов).
    analysis_limit is None означает безлимитный доступ (пользователи без
    тарифа — унаследованные из v3 — либо ENTERPRISE без явного лимита)."""
    limit = user.get("analysis_limit")
    if limit is None:
        return True, "OK", None
    used = user.get("analysis_used") or 0
    remaining = int(limit) - int(used)
    if remaining <= 0:
        return False, ("Лимит анализов по вашему тарифу исчерпан. Для увеличения лимита "
                        "перейдите на PRO или обратитесь в TaxMasterKZ."), 0
    return True, "OK", remaining


def check_feature(user: dict, feature: str) -> tuple[bool, str]:
    """feature: 'word' | 'pdf' | 'excel'. Администратор и пользователи без
    назначенного тарифа (унаследованные из v3) ограничений не имеют — это
    сохраняет обратную совместимость."""
    if user.get("role") == "admin":
        return True, "OK"
    plan_code = user.get("plan")
    if not plan_code:
        return True, "OK"
    plan = get_plan(plan_code)
    if not plan:
        return True, "OK"
    if feature == "word" and not plan["allow_word"]:
        return False, f"Word-экспорт недоступен на тарифе {plan_code}. Обратитесь в TaxMasterKZ."
    if feature == "pdf" and not plan["allow_pdf"]:
        return False, f"PDF-экспорт недоступен на тарифе {plan_code}. Обратитесь в TaxMasterKZ."
    if feature == "excel" and plan["excel_mode"] == "none":
        return False, f"Excel-экспорт недоступен на тарифе {plan_code}. Перейдите на START или PRO."
    return True, "OK"


def excel_mode_for(user: dict) -> str:
    if user.get("role") == "admin":
        return "full"
    plan_code = user.get("plan")
    if not plan_code:
        return "full"
    plan = get_plan(plan_code)
    return plan["excel_mode"] if plan else "full"


def needs_watermark(user: dict) -> bool:
    if user.get("role") == "admin":
        return False
    plan_code = user.get("plan")
    if not plan_code:
        return False
    plan = get_plan(plan_code)
    return bool(plan and plan["watermark"])


# --------------------------------------------------------------------------
# аутентификация / проверка доступа
# --------------------------------------------------------------------------

def authenticate(username: str, password: str) -> tuple[dict | None, str]:
    """Возвращает (user_dict, message). user_dict is None при неуспехе."""
    user = get_user(username)
    if user is None or not pwd_context.verify(password, user["password_hash"]):
        log_login(username, success=False)
        return None, "Неверный логин или пароль"

    ok, reason = check_access(user)
    if not ok:
        log_login(username, success=False)
        return None, reason

    log_login(username, success=True)
    _touch_last_login(username)
    user = get_user(username)  # обновлённый last_login
    return user, "OK"


def check_access(user: dict) -> tuple[bool, str]:
    """Проверяет статус (active/blocked) и срок действия доступа
    (access_end_date, с обратной совместимостью на старое поле expiry_date,
    если access_end_date не задан)."""
    if user["status"] == "blocked":
        return False, "Ваш доступ заблокирован. Для восстановления доступа обратитесь в TaxMasterKZ."
    end_str = user.get("access_end_date") or user.get("expiry_date")
    if end_str:
        try:
            exp = datetime.fromisoformat(end_str).date()
        except ValueError:
            exp = None
        if exp and date.today() > exp:
            return False, "Ваш доступ завершен. Для продления обратитесь в TaxMasterKZ."
    return True, "OK"


def log_login(username: str, success: bool):
    with _conn() as c:
        c.execute(
            "INSERT INTO login_logs (username, success, timestamp) VALUES (?, ?, ?)",
            (username, 1 if success else 0, datetime.utcnow().isoformat()),
        )


def get_login_history(username: str | None = None, limit: int = 200) -> list[dict]:
    with _conn() as c:
        if username:
            rows = c.execute(
                "SELECT * FROM login_logs WHERE username=? ORDER BY timestamp DESC LIMIT ?",
                (username, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM login_logs ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# файлы / анализы / аудит
# --------------------------------------------------------------------------

def log_uploaded_file(user_id: int, filename: str, category: str, stored_path: str, size_bytes: int):
    with _conn() as c:
        c.execute(
            "INSERT INTO uploaded_files (user_id, filename, category, stored_path, size_bytes, uploaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, filename, category, stored_path, size_bytes, datetime.utcnow().isoformat()),
        )


def list_uploaded_files(user_id: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM uploaded_files WHERE user_id=? ORDER BY uploaded_at DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_uploaded_files_records(user_id: int):
    with _conn() as c:
        c.execute("DELETE FROM uploaded_files WHERE user_id=?", (user_id,))


def log_analysis(user_id: int, summary: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO analyses (user_id, summary, created_at) VALUES (?, ?, ?)",
            (user_id, summary, datetime.utcnow().isoformat()),
        )


def count_analyses_by_user() -> dict[str, int]:
    with _conn() as c:
        rows = c.execute("""
            SELECT u.username AS username, COUNT(a.id) AS n
            FROM users u LEFT JOIN analyses a ON a.user_id = u.id
            GROUP BY u.username
        """).fetchall()
        return {r["username"]: r["n"] for r in rows}


def log_audit(user_id: int | None, username: str | None, action: str, details: str = "",
              admin_id: int | None = None, admin_username: str | None = None, ip: str | None = None):
    with _conn() as c:
        c.execute(
            "INSERT INTO audit_logs (user_id, username, action, details, timestamp, admin_id, admin_username, ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, action, details, datetime.utcnow().isoformat(), admin_id, admin_username, ip),
        )


def get_audit_logs(user_id: int | None = None, limit: int = 300) -> list[dict]:
    with _conn() as c:
        if user_id:
            rows = c.execute(
                "SELECT * FROM audit_logs WHERE user_id=? ORDER BY timestamp DESC LIMIT ?", (user_id, limit)
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# настройки приложения (maintenance_mode и т.д.)
# --------------------------------------------------------------------------

def get_setting(key: str, default: str | None = None) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_maintenance_mode() -> bool:
    return get_setting("maintenance_mode", "0") == "1"


def set_maintenance_mode(enabled: bool):
    set_setting("maintenance_mode", "1" if enabled else "0")
