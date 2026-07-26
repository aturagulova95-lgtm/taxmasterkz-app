"""
app.py — TaxMasterKZ. Универсальный помощник камерального анализа налогоплательщика.

Запуск:  streamlit run app.py

Страницы:
  Главная | Инструкция | Загрузка данных | Дашборд | Покупатели | Поставщики |
  ФНО | Камеральный контроль | Генератор справки | Мой тариф | Тарифы |
  (Админ-панель) | Доступ | Правила использования | Настройки

Приложение не привязано к конкретному налогоплательщику или набору файлов:
все парсеры терпимы к отсутствующим данным, разным названиям колонок и
разным годам (см. modules/*.py). Подробное пошаговое руководство — см.
ИНСТРУКЦИЯ_ПОЛЬЗОВАТЕЛЯ.md или раздел «Инструкция» внутри приложения.

С версии v4 приложение — платный многопользовательский сервис с тарифами
DEMO/START/PRO/TEAM/ENTERPRISE (см. modules/auth_db.py): лимит анализов,
срок доступа, разрешённые форматы экспорта и водяной знак определяются
тарифом пользователя. Пользователи, унаследованные из v3 (без тарифа),
ограничений не имеют — обратная совместимость сохранена намеренно.
"""

from __future__ import annotations

import datetime
import io
import json
import os
import random
import re
import secrets
import string
import tempfile

import pandas as pd
import plotly.express as px
import streamlit as st

try:
    from dotenv import load_dotenv
    load_dotenv()  # подхватывает .env при локальном запуске (streamlit run app.py);
                    # в Docker переменные уже приходят через env_file, load_dotenv() их не перезаписывает
except ImportError:
    pass

from modules import (
    file_loader, column_mapper, data_normalizer,
    esf_parser, fno_parser, taxpayer_parser,
    analytics, risk_engine, report_generator, export_utils, db,
    auth_db, user_storage,
)

st.set_page_config(page_title="TaxMasterKZ — налоговый анализ НП", layout="wide", page_icon="📊")

APP_NAME = "TaxMasterKZ"


def _get_config(key: str, default: str = "") -> str:
    """Читает настройку сначала из Streamlit Secrets (Streamlit Cloud -> Settings ->
    Secrets), затем из переменной окружения (.env локально / Docker env_file), затем —
    значение по умолчанию. Без файла .streamlit/secrets.toml обращение к st.secrets
    бросает StreamlitSecretNotFoundError даже для .get() — это ожидаемо для локального
    запуска без секретов, поэтому здесь оно перехватывается и код просто идёт дальше
    к переменной окружения."""
    try:
        if key in st.secrets:
            val = st.secrets[key]
            if val:
                return str(val)
    except Exception:
        pass
    return os.environ.get(key, default)


# Единственные места в коде, где нужно менять контакты/ссылки/публичный адрес —
# эти переменные (Streamlit Secrets в облаке или .env локально, см. .env.example).
THREADS_URL = _get_config("THREADS_URL", "https://www.threads.com/@taxmasterkz")
WHATSAPP_URL = _get_config("WHATSAPP_URL", "https://wa.me/77000000000")
SUPPORT_EMAIL = _get_config("SUPPORT_EMAIL", "")
APP_PUBLIC_URL = _get_config("APP_PUBLIC_URL", "http://localhost:8501")
INSTRUCTIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ИНСТРУКЦИЯ_ПОЛЬЗОВАТЕЛЯ.md")

auth_db.init_db()

_TRANSLIT_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _translit(word: str) -> str:
    return "".join(_TRANSLIT_MAP.get(ch, ch) for ch in word.lower())


def generate_username_from_fullname(full_name: str) -> str:
    """Логин вида 'имя.фамилия' транслитерацией с кириллицы (см. ТЗ: 'Жанболат
    Берикбаев' -> 'zhanbolat.berikbayev'). Если ФИО не задано, транслитерация
    даёт пустую/слишком короткую строку (например, только цифры/спецсимволы) —
    безопасный запасной вариант user_ГГГГММДД_ЧЧММ, чтобы логин всегда был
    валидным без ручного вмешательства администратора."""
    parts = [p for p in re.split(r"\s+", (full_name or "").strip()) if p]
    if len(parts) >= 2:
        login = ".".join(_translit(p) for p in parts[:2])
        login = re.sub(r"[^a-z0-9.]", "", login)
        login = re.sub(r"\.+", ".", login).strip(".")
        if len(login) >= 3:
            candidate = login
            suffix = 2
            while auth_db.get_user(candidate):
                candidate = f"{login}{suffix}"
                suffix += 1
            return candidate
    return f"user_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}"


def generate_strong_password(length: int = 14) -> str:
    """Сложный пароль: минимум 12 символов, обязательно есть заглавная, строчная,
    цифра и спецсимвол (см. ТЗ). Показывается администратору только в момент
    создания пользователя — в базе хранится только bcrypt-хэш (см. auth_db.py)."""
    upper, lower, digits, special = string.ascii_uppercase, string.ascii_lowercase, string.digits, "!@#$%^&*-_+="
    required = [secrets.choice(upper), secrets.choice(lower), secrets.choice(digits), secrets.choice(special)]
    rest = [secrets.choice(upper + lower + digits + special) for _ in range(max(length - len(required), 0))]
    chars = required + rest
    random.SystemRandom().shuffle(chars)
    return "".join(chars)


def threads_badge_html(label: str = "TaxMasterKZ в Threads", compact: bool = False) -> str:
    """Аккуратный значок-ссылка на Threads (круглый значок '@' + подпись), кликабельный.

    Вся HTML-разметка — одной строкой, без переносов и отступов: Streamlit/CommonMark
    трактует строку с отступом в 4+ пробела, идущую после пустой строки, как блок кода,
    из-за чего значок раньше отображался как текст HTML вместо кликабельной кнопки
    (см. ТЗ v4 п.8). Раздельные фрагменты в contact_badges_html() тоже нельзя склеивать
    через строки, содержащие только пробелы — такая "пустая" строка обрывает HTML-блок.
    """
    pad = "4px 10px" if compact else "6px 14px"
    size = 20 if compact else 26
    font = "12px" if compact else "13px"
    return (
        f'<a href="{THREADS_URL}" target="_blank" rel="noopener noreferrer" style="text-decoration:none;">'
        f'<div style="display:inline-flex;align-items:center;gap:8px;padding:{pad};'
        f'border:1px solid #3a3a3a;border-radius:999px;background:#000;color:#fff;'
        f'font-size:{font};font-family:inherit;">'
        f'<span style="width:{size}px;height:{size}px;min-width:{size}px;border-radius:50%;'
        f'background:#fff;color:#000;display:inline-flex;align-items:center;'
        f'justify-content:center;font-weight:800;">@</span>'
        f'<span>{label}</span>'
        f'</div></a>'
    )


def whatsapp_badge_html(label: str = "Написать в WhatsApp", compact: bool = False) -> str:
    pad = "4px 10px" if compact else "6px 14px"
    font = "12px" if compact else "13px"
    return (
        f'<a href="{WHATSAPP_URL}" target="_blank" rel="noopener noreferrer" style="text-decoration:none;">'
        f'<div style="display:inline-flex;align-items:center;gap:8px;padding:{pad};'
        f'border:1px solid #1fa855;border-radius:999px;background:#25D366;color:#fff;'
        f'font-size:{font};font-family:inherit;font-weight:600;">'
        f'<span>💬</span><span>{label}</span>'
        f'</div></a>'
    )


def contact_badges_html() -> str:
    """WhatsApp + Threads (+ email, если задан SUPPORT_EMAIL) — единый блок контактов
    TaxMasterKZ, используемый на экранах блокировки/истечения доступа/лимита и на
    страницах «Мой тариф», «Тарифы», «Доступ»."""
    parts = [whatsapp_badge_html(), threads_badge_html()]
    if SUPPORT_EMAIL:
        parts.append(
            f'<a href="mailto:{SUPPORT_EMAIL}" target="_blank" style="text-decoration:none;">'
            f'<div style="display:inline-flex;align-items:center;gap:8px;padding:6px 14px;'
            f'border:1px solid #999;border-radius:999px;background:#fff;color:#333;font-size:13px;">'
            f'<span>✉️</span><span>{SUPPORT_EMAIL}</span></div></a>'
        )
    return '<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">' + "".join(parts) + '</div>'


def go_to(page_name: str):
    # Нельзя менять st.session_state["nav_page"] напрямую в этом же прогоне,
    # если виджет с key="nav_page" уже создан (sidebar_nav вызывается раньше
    # содержимого страниц) — Streamlit это запрещает. Поэтому используем
    # промежуточный ключ и применяем его ДО создания виджета (см. ниже, перед
    # вызовом sidebar_nav()).
    st.session_state["_pending_nav"] = page_name
    st.rerun()


def _current_user() -> dict | None:
    return st.session_state.get("auth_user")


def _df_records(df) -> list:
    """Безопасно превращает DataFrame (или None/что угодно) в список записей."""
    if df is None:
        return []
    try:
        if df.empty:
            return []
        return df.to_dict("records")
    except AttributeError:
        return []


def _get_client_ip() -> str | None:
    """Лучшее из возможного определение IP клиента — Streamlit не даёт прямого
    и стабильного API во всех версиях, поэтому сбой здесь не должен ничего ломать."""
    try:
        ctx = getattr(st, "context", None)
        if ctx is not None:
            headers = getattr(ctx, "headers", None) or {}
            for key in ("X-Forwarded-For", "X-Real-Ip", "X-Real-IP"):
                val = headers.get(key)
                if val:
                    return str(val).split(",")[0].strip()
    except Exception:
        pass
    return None


def _audit(user_id, username, action: str, details: str = "", admin: dict | None = None):
    """Единая точка записи в журнал действий: подмешивает IP клиента и,
    если действие совершил администратор от имени другого пользователя —
    admin_id/admin_username (см. ТЗ v4 п.11)."""
    try:
        auth_db.log_audit(
            user_id, username, action, details,
            admin_id=(admin["id"] if admin else None),
            admin_username=(admin["username"] if admin else None),
            ip=_get_client_ip(),
        )
    except Exception:
        pass


def _persist_and_log(user: dict | None, filename: str, category: str, data: bytes):
    """Сохраняет сырые байты загруженного файла в личную папку пользователя
    и пишет запись в uploaded_files + audit_logs. Не должно ронять UI при сбое."""
    if user is None:
        return
    try:
        path, size = user_storage.save_uploaded_file(user["id"], filename, data)
        auth_db.log_uploaded_file(user["id"], filename, category, path, size)
        _audit(user["id"], user["username"], "upload_file", f"{category}: {filename} ({size} байт)")
    except Exception:
        pass  # сбой хранения не должен мешать интерактивному анализу

# --------------------------------------------------------------------------
# session state
# --------------------------------------------------------------------------

def init_state():
    defaults = {
        "auth_user": None,          # dict текущего пользователя после входа (см. auth_db)
        "taxpayer_profile": None,
        "files_registry": [],       # список карточек по каждому загруженному файлу
        "esf_purchase_results": [],  # list[EsfParseResult]
        "esf_sale_results": [],
        "fno_results": {},          # form_code -> list[FnoParseResult]
        "computed": None,           # словарь с посчитанной аналитикой
        "risk_overrides": {},       # index -> {"level":..,"comment":..,"resolved":bool}
        "excluded_esf": set(),      # номера ЭСФ, исключённые пользователем из анализа
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)


init_state()

CATEGORIES = ["Карточка НП", "ФНО", "ЭСФ приобретение", "ЭСФ реализация",
              "Банк", "1С", "СНТ", "ЭАВР", "Контрагенты", "Прочее"]


# --------------------------------------------------------------------------
# sidebar
# --------------------------------------------------------------------------

def sidebar_nav() -> str:
    st.sidebar.title(f"📊 {APP_NAME}")
    st.sidebar.caption("Помощник для камерального анализа налогоплательщиков")

    user = _current_user()
    if user:
        role_label = {"admin": "администратор", "user": "пользователь", "demo": "демо-доступ"}.get(user["role"], user["role"])
        st.sidebar.markdown(f"👤 **{user['username']}** ({role_label})")
        end_str = user.get("access_end_date") or user.get("expiry_date")
        if user.get("plan"):
            limit = user.get("analysis_limit")
            used = user.get("analysis_used") or 0
            remain_label = f"{max((limit or 0) - used, 0)}/{limit}" if limit is not None else "без лимита"
            st.sidebar.caption(f"Тариф: **{user['plan']}** · анализов: {remain_label}")
        if end_str:
            st.sidebar.caption(f"Доступ до: {end_str}")
        if st.sidebar.button("🚪 Выйти", use_container_width=True):
            _audit(user["id"], user["username"], "logout", "")
            st.session_state["auth_user"] = None
            st.rerun()
        st.sidebar.divider()

    options = ["Главная", "Инструкция", "Загрузка данных", "Дашборд", "Покупатели", "Поставщики",
               "ФНО", "Камеральный контроль", "Генератор справки", "Мой тариф", "Тарифы"]
    if user and user["role"] == "admin":
        options.append("Админ-панель")
    options += ["Доступ", "Правила использования", "Настройки"]

    page = st.sidebar.radio(
        "Раздел",
        options,
        label_visibility="collapsed",
        key="nav_page",
    )
    st.sidebar.divider()
    profile = st.session_state.taxpayer_profile
    if profile is not None:
        st.sidebar.markdown(f"**НП:** {profile.name or 'не определено'}")
        st.sidebar.markdown(f"**БИН/ИИН:** {profile.bin_iin or 'не определён'}")
    else:
        st.sidebar.info("Карточка НП ещё не загружена")
    st.sidebar.divider()
    n_files = len(st.session_state.files_registry)
    st.sidebar.markdown(f"Загружено файлов: **{n_files}**")
    st.sidebar.divider()
    # Значок Threads — виден в боковом меню на каждой странице
    st.sidebar.markdown(threads_badge_html(), unsafe_allow_html=True)
    return page


# --------------------------------------------------------------------------
# helpers: обработка загруженных файлов
# --------------------------------------------------------------------------

def _taxpayer_bin() -> str | None:
    p = st.session_state.taxpayer_profile
    return p.bin_iin if p else None


def process_taxpayer_file(uploaded_file):
    name = uploaded_file.name
    data = uploaded_file.getvalue()
    try:
        if name.lower().endswith(".pdf"):
            profile = taxpayer_parser.parse_sur_pdf(data, name)
        else:
            # сначала пробуем формат "поле / значение" по сырым строкам книги
            # (см. taxpayer_parser.parse_taxpayer_card_2col_raw) — обычная
            # автозагрузка через file_loader режет такой вертикальный список
            # как будто у него есть строка-заголовок, и портит первую пару.
            # Если это не тот формат — используем обычный табличный парсер.
            profile = taxpayer_parser.parse_taxpayer_card_2col_raw(data, name)
            if profile is None:
                tables = file_loader.load_any(data, name)
                profile = taxpayer_parser.parse_taxpayer_excel(tables, name)
        st.session_state.taxpayer_profile = profile
        card = {
            "filename": name, "category": "Карточка НП", "status": "ок" if profile.bin_iin else "требует проверки",
            "rows": None, "years": list(profile.yearly.keys()),
            "sum_info": f"БИН: {profile.bin_iin or '?'}, {profile.name or '?'}",
            "warnings": profile.warnings,
        }
    except Exception as e:
        card = {"filename": name, "category": "Карточка НП", "status": "ошибка",
                "rows": None, "years": [], "sum_info": "", "warnings": [str(e)]}
    st.session_state.files_registry.append(card)
    _persist_and_log(_current_user(), name, "Карточка НП", data)


def process_esf_file(uploaded_file, direction_override: str | None):
    name = uploaded_file.name
    data = uploaded_file.getvalue()
    try:
        tables = file_loader.load_excel(data, name) if name.lower().endswith((".xlsx", ".xls")) else file_loader.load_csv(data, name)
        results = esf_parser.parse_esf_file(tables, name, taxpayer_bin=_taxpayer_bin(), direction_override=direction_override)
        if not results:
            card = {"filename": name, "category": "ЭСФ", "status": "не распознано как ЭСФ",
                    "rows": 0, "years": [], "sum_info": "", "warnings": ["Файл не похож на выгрузку ЭСФ"]}
            st.session_state.files_registry.append(card)
            return
        for res in results:
            if res.direction == "purchase":
                st.session_state.esf_purchase_results.append(res)
            else:
                st.session_state.esf_sale_results.append(res)
            card = {
                "filename": name,
                "category": "ЭСФ приобретение" if res.direction == "purchase" else "ЭСФ реализация",
                "status": "ок" if not res.warnings else "требует проверки",
                "rows": res.row_count, "years": res.years_found,
                "sum_info": f"без НДС: {res.total_amount_no_vat:,.0f} / НДС: {res.total_vat:,.0f}".replace(",", " "),
                "warnings": res.warnings,
            }
            st.session_state.files_registry.append(card)
    except Exception as e:
        st.session_state.files_registry.append({
            "filename": name, "category": "ЭСФ", "status": "ошибка",
            "rows": 0, "years": [], "sum_info": "", "warnings": [str(e)],
        })
    _persist_and_log(_current_user(), name, "ЭСФ приобретение" if direction_override == "purchase" else "ЭСФ реализация", data)


def process_fno_file(uploaded_file):
    name = uploaded_file.name
    data = uploaded_file.getvalue()
    try:
        if name.lower().endswith(".pdf"):
            results = fno_parser.parse_fno_pdf(data, name)
        else:
            tables = file_loader.load_excel(data, name) if name.lower().endswith((".xlsx", ".xls")) else file_loader.load_csv(data, name)
            results = fno_parser.parse_fno_file(tables, name)
        if not results:
            st.session_state.files_registry.append({
                "filename": name, "category": "ФНО", "status": "не распознано как реестр ФНО",
                "rows": 0, "years": [], "sum_info": "", "warnings": ["Не найдены типовые колонки реестра ФНО"],
            })
            return
        for res in results:
            st.session_state.fno_results.setdefault(res.form_code or "?", []).append(res)
            st.session_state.files_registry.append({
                "filename": name, "category": f"ФНО {res.form_code or '?'}",
                "status": "ок" if not res.warnings else "частично",
                "rows": res.row_count, "years": res.years_found,
                "sum_info": f"{res.row_count} деклараций", "warnings": res.warnings,
            })
    except Exception as e:
        st.session_state.files_registry.append({
            "filename": name, "category": "ФНО", "status": "ошибка",
            "rows": 0, "years": [], "sum_info": "", "warnings": [str(e)],
        })
    _persist_and_log(_current_user(), name, "ФНО", data)


def process_generic_file(uploaded_file, category: str):
    """Для банка/1С/СНТ/ЭАВР/контрагентов/прочего — общая загрузка+предпросмотр без глубокого парсинга (см. README, v2)."""
    name = uploaded_file.name
    data = uploaded_file.getvalue()
    try:
        loaded = file_loader.load_any(data, name)
        if isinstance(loaded, list):
            total_rows = sum(len(t.dataframe) for t in loaded)
            warnings = [w for t in loaded for w in t.warnings]
        else:
            total_rows = len(loaded.pages)
            warnings = []
        st.session_state.files_registry.append({
            "filename": name, "category": category, "status": "загружено (предпросмотр)",
            "rows": total_rows, "years": [], "sum_info": "Детальный парсинг для этого раздела — в следующей версии",
            "warnings": warnings,
        })
    except Exception as e:
        st.session_state.files_registry.append({
            "filename": name, "category": category, "status": "ошибка",
            "rows": 0, "years": [], "sum_info": "", "warnings": [str(e)],
        })
    _persist_and_log(_current_user(), name, category, data)


# --------------------------------------------------------------------------
# пересчёт аналитики
# --------------------------------------------------------------------------

def recompute_analytics():
    purch = analytics.combine_esf(st.session_state.esf_purchase_results)
    sales = analytics.combine_esf(st.session_state.esf_sale_results)

    excluded = st.session_state.excluded_esf
    if excluded and not purch.empty:
        purch = purch[~purch["esf_number"].isin(excluded)]
    if excluded and not sales.empty:
        sales = sales[~sales["esf_number"].isin(excluded)]

    buyers = analytics.buyers_by_year(sales)
    suppliers = analytics.suppliers_by_year(purch)

    profile = st.session_state.taxpayer_profile
    yearly_profile = profile.yearly if profile else {}
    tax_rev_by_year = {y: d.get("tax_revenue") for y, d in yearly_profile.items()}
    dynamics = analytics.yearly_dynamics(sales, purch, tax_rev_by_year)

    fno_100 = pd.concat([r.dataframe for r in st.session_state.fno_results.get("100.00", [])], ignore_index=True) \
        if st.session_state.fno_results.get("100.00") else None
    fno_300 = pd.concat([r.dataframe for r in st.session_state.fno_results.get("300.00", [])], ignore_index=True) \
        if st.session_state.fno_results.get("300.00") else None
    fno_200 = pd.concat([r.dataframe for r in st.session_state.fno_results.get("200.00", [])], ignore_index=True) \
        if st.session_state.fno_results.get("200.00") else None

    vat_rec = analytics.vat_reconciliation(sales, purch, fno_300)
    income_rec = analytics.income_reconciliation(sales, fno_100)
    payroll = analytics.payroll_check(yearly_profile, fno_200)

    findings = risk_engine.run_all_checks(
        buyers=buyers, suppliers=suppliers, esf_sales=sales, esf_purchases=purch,
        yearly_profile=yearly_profile, vat_rec=vat_rec, income_rec=income_rec, payroll=payroll,
    )
    # применяем ручные корректировки уровня риска / статус "документы получены"
    for i, f in enumerate(findings):
        override = st.session_state.risk_overrides.get(i)
        if override:
            if override.get("level"):
                f["level"] = override["level"]
            f["comment"] = override.get("comment", "")
            f["resolved"] = override.get("resolved", False)
        else:
            f["comment"] = ""
            f["resolved"] = False
    findings_by_level = risk_engine.group_by_level(findings)
    docs = analytics.documents_to_request(findings)

    st.session_state.computed = {
        "esf_purchases": purch, "esf_sales": sales,
        "buyers": buyers, "suppliers": suppliers,
        "dynamics": dynamics, "vat_rec": vat_rec, "income_rec": income_rec, "payroll": payroll,
        "findings": findings, "findings_by_level": findings_by_level, "documents": docs,
    }

    user = _current_user()
    user_id = user["id"] if user else "shared"
    db.save_snapshot(
        user_id=user_id,
        taxpayer_profile=profile, esf_purchases=purch, esf_sales=sales,
        buyers=buyers, suppliers=suppliers, findings=findings,
        fno_results=st.session_state.fno_results,
    )
    if user:
        summary = (
            f"строк ЭСФ приобр.: {len(purch)}, реализ.: {len(sales)}, "
            f"покупателей: {buyers['bin'].nunique() if not buyers.empty else 0}, "
            f"поставщиков: {suppliers['bin'].nunique() if not suppliers.empty else 0}, "
            f"находок риска: {len(findings)}"
        )
        auth_db.log_analysis(user["id"], summary)
        _audit(user["id"], user["username"], "run_analysis", summary)


# --------------------------------------------------------------------------
# страницы
# --------------------------------------------------------------------------

def page_home():
    st.title(f"📊 {APP_NAME}")
    st.markdown(
        "**TaxMasterKZ** — помощник для камерального анализа налогоплательщиков. "
        "Загрузите ЭСФ, ФНО и сведения по НП, чтобы получить свод по покупателям, "
        "поставщикам, расхождениям и налоговым рискам."
    )
    bcol1, bcol2, bcol3 = st.columns(3)
    if bcol1.button("🚀 Начать анализ", use_container_width=True, type="primary"):
        go_to("Загрузка данных")
    if bcol2.button("📖 Открыть инструкцию", use_container_width=True):
        go_to("Инструкция")
    if bcol3.button("📝 Сформировать справку", use_container_width=True):
        go_to("Генератор справки")

    st.divider()
    st.subheader("Главная панель")
    profile = st.session_state.taxpayer_profile
    c = st.session_state.computed

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Загружено файлов", len(st.session_state.files_registry))
    years = sorted({y for card in st.session_state.files_registry for y in (card.get("years") or [])})
    col2.metric("Годы анализа", f"{years[0]}–{years[-1]}" if years else "—")
    if c is not None:
        dynamics = c.get("dynamics")
        findings = c.get("findings", [])
        if dynamics is not None and not dynamics.empty and "sales_no_vat" in dynamics.columns:
            total_sales = dynamics["sales_no_vat"].sum()
            col3.metric("Реализация без НДС, всего", f"{total_sales:,.0f}".replace(",", " "))
        else:
            col3.metric("Реализация без НДС, всего", "—")
        col4.metric("Выявлено рисков", len(findings) if findings is not None else "—")
    else:
        col3.metric("Реализация без НДС, всего", "—")
        col4.metric("Выявлено рисков", "—")

    st.divider()
    if profile:
        st.subheader("Карточка налогоплательщика")
        cols = st.columns(3)
        cols[0].markdown(f"**Наименование:** {profile.name or 'н/д'}")
        cols[0].markdown(f"**БИН/ИИН:** {profile.bin_iin or 'н/д'}")
        cols[1].markdown(f"**ОКЭД:** {profile.oked or 'н/д'} — {profile.oked_name or ''}")
        cols[1].markdown(f"**Режим:** {profile.tax_regime or 'н/д'}")
        vat_txt = "Да" if profile.vat_payer is True else ("Нет" if profile.vat_payer is False else "н/д")
        cols[2].markdown(f"**Плательщик НДС:** {vat_txt}")
        cols[2].markdown(f"**Задолженность:** {data_normalizer.normalize_amount(profile.fields.get('tax_debt_total')) or 0:,.0f}".replace(",", " "))
        if profile.warnings:
            st.warning(" / ".join(profile.warnings))
    else:
        st.info("Загрузите карточку НП (PDF СУР КГД или Excel) в разделе «Загрузка данных», чтобы увидеть сводку.")

    st.divider()
    st.subheader("Статус проверки по разделам")
    status_rows = []
    for cat in CATEGORIES:
        files = [f for f in st.session_state.files_registry if f["category"].startswith(cat.split(" ")[0])]
        status_rows.append({"Раздел": cat, "Файлов загружено": len(files)})
    st.dataframe(pd.DataFrame(status_rows), use_container_width=True, hide_index=True)

    if st.session_state.files_registry and c is None:
        st.info("Файлы загружены, но аналитика ещё не пересчитана. Перейдите в «Загрузка данных» и нажмите «Собрать базу и пересчитать аналитику».")

    st.divider()
    st.markdown(threads_badge_html("Мы в Threads"), unsafe_allow_html=True)


def page_upload():
    st.title("Загрузка данных")
    st.caption("Перетащите файлы в соответствующий блок. Приложение само определяет тип, год и направление (приобретение/реализация). Если нужно — скорректируйте вручную.")

    tabs = st.tabs(CATEGORIES)

    with tabs[0]:
        st.write("PDF или Excel из КГД/СУР со сведениями о налогоплательщике.")
        files = st.file_uploader("Карточка НП", type=["pdf", "xlsx", "xls"], accept_multiple_files=True, key="up_taxpayer")
        if files:
            for f in files:
                if f.name not in [c["filename"] for c in st.session_state.files_registry]:
                    process_taxpayer_file(f)
            if st.session_state.taxpayer_profile:
                st.success(f"Распознано: {st.session_state.taxpayer_profile.name} (БИН {st.session_state.taxpayer_profile.bin_iin})")

    with tabs[1]:
        st.caption("💡 Загрузите налоговые формы для сопоставления с ЭСФ.")
        st.write("Реестры ФНО 100.00 / 200.00 / 200.01 / 300.00 и др. (PDF или выгрузки кабинета налогоплательщика).")
        files = st.file_uploader("ФНО", type=["pdf", "xlsx", "xls", "csv"], accept_multiple_files=True, key="up_fno")
        if files:
            for f in files:
                if f.name not in [c["filename"] for c in st.session_state.files_registry]:
                    process_fno_file(f)

    with tabs[2]:
        st.caption("💡 Загрузите Excel-выгрузки из ИС ЭСФ по годам. Файл может называться «приобретение 2023», «ЭСФ покупки», «purchases» и т.п. — название не важно.")
        st.write("Выгрузки ЭСФ по приобретению (закуп).")
        files = st.file_uploader("ЭСФ приобретение", type=["xlsx", "xls", "csv"], accept_multiple_files=True, key="up_esf_p")
        if files:
            for f in files:
                if f.name not in [c["filename"] for c in st.session_state.files_registry]:
                    process_esf_file(f, direction_override="purchase")

    with tabs[3]:
        st.caption("💡 Загрузите Excel-выгрузки из ИС ЭСФ по годам. Файл может называться «реализация 2023», «ЭСФ реализация», «sales» и т.п. — название не важно.")
        st.write("Выгрузки ЭСФ по реализации (продажи).")
        files = st.file_uploader("ЭСФ реализация", type=["xlsx", "xls", "csv"], accept_multiple_files=True, key="up_esf_s")
        if files:
            for f in files:
                if f.name not in [c["filename"] for c in st.session_state.files_registry]:
                    process_esf_file(f, direction_override="sale")

    for tab, cat in zip(tabs[4:], CATEGORIES[4:]):
        with tab:
            st.write(f"Дополнительные данные: {cat}. Загружаются и отображаются в предпросмотре; глубокий разбор — в следующей версии.")
            files = st.file_uploader(cat, accept_multiple_files=True, key=f"up_{cat}")
            if files:
                for f in files:
                    if f.name not in [c["filename"] for c in st.session_state.files_registry]:
                        process_generic_file(f, cat)

    st.divider()
    st.subheader("Предпросмотр загруженных файлов")
    if st.session_state.files_registry:
        df = pd.DataFrame(st.session_state.files_registry)
        df_display = df.copy()
        df_display["years"] = df_display["years"].apply(lambda x: ", ".join(str(y) for y in x) if x else "")
        df_display["warnings"] = df_display["warnings"].apply(lambda x: " | ".join(x) if x else "")
        st.dataframe(df_display[["filename", "category", "status", "rows", "years", "sum_info", "warnings"]],
                     use_container_width=True, hide_index=True)

        col_a, col_b, col_c = st.columns([1, 1, 1])
        with col_a:
            user = _current_user()
            fresh = auth_db.get_user_by_id(user["id"]) if user else None
            quota_ok, quota_msg, remaining = auth_db.check_analysis_quota(fresh) if fresh else (True, "OK", None)
            if fresh and fresh.get("analysis_limit") is not None:
                st.caption(
                    f"Анализов использовано: {fresh.get('analysis_used') or 0} / {fresh['analysis_limit']} "
                    f"(тариф {fresh.get('plan') or '—'})"
                )
            if not quota_ok:
                st.error(quota_msg)
                _audit(fresh["id"], fresh["username"], "analysis_limit_exhausted", quota_msg)
            if st.button("🔄 Собрать базу и пересчитать аналитику", type="primary",
                         use_container_width=True, disabled=not quota_ok):
                recompute_analytics()
                if fresh:
                    new_used = auth_db.increment_analysis_used(fresh["id"])
                    fresh2 = auth_db.get_user_by_id(fresh["id"])
                    _audit(fresh["id"], fresh["username"], "analysis_counted",
                           f"тариф={fresh2.get('plan') or 'нет'}, лимит={fresh2.get('analysis_limit')}, использовано={new_used}")
                st.success("Аналитика пересчитана")
        with col_b:
            if st.button("Очистить данные текущей сессии", use_container_width=True):
                for k in ["taxpayer_profile", "files_registry", "esf_purchase_results", "esf_sale_results",
                          "fno_results", "computed", "risk_overrides"]:
                    st.session_state[k] = [] if isinstance(st.session_state[k], list) else (
                        {} if isinstance(st.session_state[k], dict) else None)
                st.rerun()
        with col_c:
            if st.button("🗑️ Удалить все мои загруженные файлы", use_container_width=True):
                user = _current_user()
                if user:
                    n = user_storage.delete_all_user_files(user["id"])
                    auth_db.delete_uploaded_files_records(user["id"])
                    _audit(user["id"], user["username"], "delete_all_files", f"удалено файлов: {n}")
                    for k in ["taxpayer_profile", "files_registry", "esf_purchase_results", "esf_sale_results",
                              "fno_results", "computed", "risk_overrides"]:
                        st.session_state[k] = [] if isinstance(st.session_state[k], list) else (
                            {} if isinstance(st.session_state[k], dict) else None)
                    st.success(f"Удалено файлов: {n}")
                    st.rerun()
    else:
        st.info("Пока ничего не загружено.")

    my_files = user_storage.list_user_files(_current_user()["id"]) if _current_user() else []
    if my_files:
        with st.expander(f"📁 Мои сохранённые файлы на сервере ({len(my_files)})"):
            st.caption("Эти файлы видны только вам. Никто другой не имеет к ним доступа.")
            st.dataframe(pd.DataFrame(my_files)[["name", "size_bytes", "modified"]],
                         use_container_width=True, hide_index=True)


def page_dashboard():
    st.title("Дашборд")
    c = st.session_state.computed
    if c is None:
        st.warning("Сначала загрузите данные и нажмите «Собрать базу и пересчитать аналитику» на странице «Загрузка данных».")
        return

    dyn = c.get("dynamics")
    if dyn is None or dyn.empty:
        st.info("Недостаточно данных для построения дашборда. Необходимо загрузить: ЭСФ по приобретению и/или реализации.")
        return

    esf_sales = c.get("esf_sales")
    esf_purchases = c.get("esf_purchases")
    buyers = c.get("buyers")
    suppliers = c.get("suppliers")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Реализация без НДС", f"{dyn.get('sales_no_vat', pd.Series(dtype=float)).sum():,.0f}".replace(",", " "))
    col2.metric("Приобретение без НДС", f"{dyn.get('purchases_no_vat', pd.Series(dtype=float)).sum():,.0f}".replace(",", " "))
    col3.metric("НДС начисленный", f"{dyn.get('sales_vat', pd.Series(dtype=float)).sum():,.0f}".replace(",", " "))
    col4.metric("НДС в зачёт", f"{dyn.get('purchases_vat', pd.Series(dtype=float)).sum():,.0f}".replace(",", " "))

    col5, col6, col7 = st.columns(3)
    col5.metric("Кол-во ЭСФ реализации", int(esf_sales["esf_number"].nunique()) if esf_sales is not None and not esf_sales.empty else 0)
    col6.metric("Кол-во ЭСФ приобретения", int(esf_purchases["esf_number"].nunique()) if esf_purchases is not None and not esf_purchases.empty else 0)
    n_buyers = buyers["bin"].nunique() if buyers is not None and not buyers.empty else 0
    n_suppliers = suppliers["bin"].nunique() if suppliers is not None and not suppliers.empty else 0
    col7.metric("Покупателей / Поставщиков", f"{n_buyers} / {n_suppliers}")

    st.divider()
    fig1 = px.bar(dyn, x="year", y=["sales_no_vat", "purchases_no_vat"], barmode="group",
                  labels={"value": "Тенге", "year": "Год", "variable": "Показатель"},
                  title="Динамика реализации и приобретений по годам")
    st.plotly_chart(fig1, use_container_width=True)

    fig2 = px.bar(dyn, x="year", y=["sales_vat", "purchases_vat"], barmode="group",
                  labels={"value": "Тенге", "year": "Год", "variable": "Показатель"},
                  title="НДС начисленный vs НДС в зачёт по годам")
    st.plotly_chart(fig2, use_container_width=True)

    if dyn["tax_burden_pct"].notna().any():
        fig3 = px.line(dyn, x="year", y="tax_burden_pct", markers=True, title="Налоговая нагрузка по годам, %")
        st.plotly_chart(fig3, use_container_width=True)

    st.subheader("Таблица динамики по годам")
    st.dataframe(dyn, use_container_width=True, hide_index=True)


def _counterparty_page(role: str):
    c = st.session_state.computed
    title = "Покупатели" if role == "buyers" else "Поставщики"
    st.title(title)
    if c is None:
        st.warning("Сначала загрузите данные и пересчитайте аналитику.")
        return
    df = c.get("buyers") if role == "buyers" else c.get("suppliers")
    if df is None or df.empty:
        st.info(f"Недостаточно данных. Необходимо загрузить ЭСФ по {'реализации' if role=='buyers' else 'приобретению'}.")
        return

    col1, col2, col3 = st.columns(3)
    years = sorted(df["year"].dropna().unique().tolist())
    year_sel = col1.multiselect("Год", years, default=years)
    name_search = col2.text_input("Поиск по наименованию/БИН")
    min_amount = col3.number_input("Мин. сумма без НДС", value=0, step=1_000_000)

    filtered = df[df["year"].isin(year_sel)] if year_sel else df
    if name_search:
        mask = filtered["name"].astype(str).str.contains(name_search, case=False, na=False) | \
               filtered["bin"].astype(str).str.contains(name_search, na=False)
        filtered = filtered[mask]
    filtered = filtered[filtered["amount_no_vat"] >= min_amount]

    st.dataframe(filtered, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader(f"Топ-10 по сумме ({'все выбранные годы' if len(year_sel)!=1 else year_sel[0]})")
    top = filtered.groupby(["bin", "name"], as_index=False)["amount_no_vat"].sum().sort_values("amount_no_vat", ascending=False).head(10)
    st.dataframe(top, use_container_width=True, hide_index=True)

    if role == "suppliers":
        st.divider()
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Поставщики с НДС**")
            st.dataframe(filtered[filtered.get("has_vat_amounts", False) == True][["bin", "name", "year", "amount_no_vat"]],
                         use_container_width=True, hide_index=True)
        with c2:
            st.markdown("**Поставщики без НДС**")
            st.dataframe(filtered[filtered.get("has_vat_amounts", False) == False][["bin", "name", "year", "amount_no_vat"]],
                         use_container_width=True, hide_index=True)

        st.markdown("**Поставщики с высокой концентрацией (>30% оборота года)**")
        st.dataframe(filtered[filtered["concentration_flag"].isin(["высокая", "средняя"])],
                     use_container_width=True, hide_index=True)

        st.markdown("**Разовые поставщики (один год работы)**")
        st.dataframe(filtered[~filtered["is_recurring"]], use_container_width=True, hide_index=True)

    st.divider()
    user = _current_user()
    excel_mode = auth_db.excel_mode_for(user) if user else "full"
    if excel_mode == "none":
        st.warning(f"Excel-экспорт недоступен на тарифе {user.get('plan') if user else ''}. Перейдите на START или PRO.")
    else:
        excel_bytes = (export_utils.export_buyers_excel(filtered) if role == "buyers"
                       else export_utils.export_suppliers_excel(filtered))
        if st.download_button(f"📥 Экспорт в Excel ({title.lower()})", data=excel_bytes,
                            file_name=f"{title.lower()}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
            if user:
                _audit(user["id"], user["username"], "download_report", f"Excel {title.lower()}")


def page_buyers():
    _counterparty_page("buyers")


def page_suppliers():
    _counterparty_page("suppliers")


def page_fno():
    st.title("ФНО (налоговая отчётность)")
    if not st.session_state.fno_results:
        st.info("Файлы ФНО ещё не загружены. Загрузите реестры 100.00 / 200.00 / 200.01 / 300.00 в разделе «Загрузка данных».")
        return

    base_cols = [
        "fno_bin",
        "fno_code",
        "fno_view",
        "fno_reg_number",
        "fno_status",
        "fno_accept_date",
        "fno_submit_date",
        "year",
        "quarter",
    ]
    amount_cols = [
        "income_declared",
        "income_total",
        "deductions",
        "taxable_income",
        "cit_calculated",
        "vat_charged",
        "vat_credit",
        "vat_payable",
        "vat_excess",
    ]
    amount_labels = {
        "income_declared": "Совокупный годовой доход",
        "income_total": "Доход с учётом корректировок",
        "deductions": "Вычеты",
        "taxable_income": "Налогооблагаемый доход",
        "cit_calculated": "КПН исчисленный",
        "vat_charged": "НДС начисленный",
        "vat_credit": "НДС в зачёт",
        "vat_payable": "НДС к уплате",
        "vat_excess": "Превышение НДС в зачёт",
    }

    for form_code, results in st.session_state.fno_results.items():
        st.subheader(f"Форма {form_code}")
        if not results:
            st.info("Нет распознанных строк по этой форме.")
            continue

        combined = pd.concat([r.dataframe for r in results], ignore_index=True)
        show_cols = [c for c in base_cols + amount_cols if c in combined.columns]
        st.dataframe(combined[show_cols], use_container_width=True, hide_index=True)

        n_reg = int((combined.get("fno_view") == "Очередная").sum()) if "fno_view" in combined.columns else 0
        n_add = int((combined.get("fno_view") == "Дополнительная").sum()) if "fno_view" in combined.columns else 0
        st.caption(f"Очередных: {n_reg}, дополнительных: {n_add}, всего: {len(combined)}")

        found_sums, missing_sums = [], []
        for col in amount_cols:
            label = amount_labels.get(col, col)
            if col in combined.columns and combined[col].notna().any():
                total = combined[col].dropna().sum()
                found_sums.append(f"{label}: {total:,.0f} ₸".replace(",", " "))
            else:
                missing_sums.append(label)
        if found_sums:
            st.success("Найдены суммы: " + "; ".join(found_sums))
        if missing_sums:
            st.info(
                "Не удалось определить суммы по показателям: " + ", ".join(missing_sums) +
                ". Проверьте карту полей в разделе «Настройки»."
            )

        for r in results:
            for w in r.warnings:
                st.warning(f"{r.form_code}: {w}")

    st.divider()
    st.caption(
        "Суммовые показатели декларации (доход, вычеты, КПН, НДС начисленный/в зачёт) читаются по "
        "карте полей config/fno_field_map.json. Если по какому-то показателю сумма не появилась — "
        "уточните карту полей в разделе «Настройки»: КГД не публикует единую расшифровку кодов, и в "
        "разных версиях/периодах формы код может отличаться."
    )


def page_cameral_control():
    st.title("Камеральный контроль")
    st.caption("💡 Здесь приложение показывает расхождения между ЭСФ и ФНО.")
    c = st.session_state.computed
    if c is None:
        st.warning("Сначала загрузите данные и пересчитайте аналитику.")
        return

    fno_field_warning = (
        "ФНО загружена, но суммовые поля не распознаны. Камеральная сверка по этой форме ограничена. "
        "Для Excel-выгрузки проверьте карту полей в разделе «Настройки»; для PDF — качество текста в файле."
    )

    st.subheader("Сверка НДС: ЭСФ vs ФНО 300.00")
    vat_rec = c.get("vat_rec")
    if vat_rec is not None and not vat_rec.empty:
        st.dataframe(vat_rec, use_container_width=True, hide_index=True)
    elif st.session_state.fno_results.get("300.00"):
        st.warning(fno_field_warning)
    else:
        st.info("Недостаточно данных для сверки по НДС. Необходимо загрузить: ЭСФ реализация/приобретение и ФНО 300.00.")

    st.subheader("Сверка дохода: ЭСФ реализация vs ФНО 100.00")
    income_rec = c.get("income_rec")
    if income_rec is not None and not income_rec.empty:
        st.dataframe(income_rec, use_container_width=True, hide_index=True)
    elif st.session_state.fno_results.get("100.00"):
        st.warning(fno_field_warning)
    else:
        st.info("Недостаточно данных для сверки дохода. Необходимо загрузить: ЭСФ реализация и ФНО 100.00.")

    st.subheader("Проверка зарплатных налогов (численность vs ФНО 200.00)")
    payroll = c.get("payroll")
    if payroll is not None and not payroll.empty:
        st.dataframe(payroll, use_container_width=True, hide_index=True)
    elif st.session_state.fno_results.get("200.00") or st.session_state.fno_results.get("200.01"):
        st.warning(fno_field_warning)
    else:
        st.info("Недостаточно данных. Необходимо загрузить: карточку НП и ФНО 200.00/200.01.")

    st.divider()
    st.subheader("Найденные риски")
    st.caption("💡 Риск не является доказанным нарушением. Он показывает участок, который нужно проверить.")
    level_filter = st.multiselect("Уровень риска", ["высокий", "средний", "низкий"],
                                   default=["высокий", "средний", "низкий"])
    findings = c.get("findings", []) or []
    for i, f in enumerate(findings):
        if f.get("level") not in level_filter:
            continue
        color = {"высокий": "🔴", "средний": "🟡", "низкий": "🟢"}.get(f.get("level"), "⚪")
        resolved_mark = " ✅ документы получены" if f.get("resolved") else ""
        with st.expander(f"{color} [{f.get('period', '—')}] {f.get('risk_type', 'риск')}{resolved_mark}"):
            st.write(f.get("description", ""))
            st.markdown(f"**Что проверить:** {f.get('what_to_check', '—')}")
            st.markdown(f"**Возможные последствия:** {f.get('possible_consequence', '—')}")
            st.caption(f"Источник: {f.get('source', '—')} | Сумма: {f.get('amount', '—')}")
            colx, coly, colz = st.columns(3)
            override = st.session_state.risk_overrides.get(i, {})
            _levels = ["высокий", "средний", "низкий"]
            _cur_level = f.get("level") if f.get("level") in _levels else "средний"
            new_level = colx.selectbox("Изменить уровень риска", _levels,
                                        index=_levels.index(_cur_level), key=f"lvl_{i}")
            resolved = coly.checkbox("Документы получены", value=override.get("resolved", False), key=f"res_{i}")
            comment = colz.text_input("Комментарий проверяющего", value=override.get("comment", ""), key=f"cmt_{i}")
            if st.button("Сохранить", key=f"save_{i}"):
                st.session_state.risk_overrides[i] = {"level": new_level, "resolved": resolved, "comment": comment}
                recompute_analytics()
                st.rerun()

    st.divider()
    user = _current_user()
    excel_mode = auth_db.excel_mode_for(user) if user else "full"
    if excel_mode in ("none", "basic"):
        msg = (f"Excel-экспорт недоступен на тарифе {user.get('plan') if user else ''}. Перейдите на START или PRO."
               if excel_mode == "none" else
               "Экспорт рисков в Excel — расширенная выгрузка, доступна на тарифах PRO, TEAM и ENTERPRISE.")
        st.warning(msg)
    else:
        xlsx_bytes = export_utils.export_findings_excel(findings)
        if st.download_button("📥 Экспорт рисков в Excel", data=xlsx_bytes, file_name="risks.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
            if user:
                _audit(user["id"], user["username"], "download_report", "Excel risks")


def page_report():
    st.title("Генератор аналитической справки")
    st.caption("💡 Выберите стиль справки и сформируйте Word/HTML/Excel.")
    c = st.session_state.computed
    profile = st.session_state.taxpayer_profile
    if c is None or profile is None:
        st.warning("Сначала загрузите карточку НП и данные ЭСФ, затем пересчитайте аналитику.")
        return

    user = _current_user()
    if user and auth_db.needs_watermark(user):
        st.info("На тарифе DEMO справка формируется с водяным знаком «Демо-версия TaxMasterKZ».")

    dynamics0 = c.get("dynamics")
    all_years = sorted(set(dynamics0["year"].tolist())) if dynamics0 is not None and not dynamics0.empty else []
    col1, col2 = st.columns(2)
    years_sel = col1.multiselect("Период (годы)", all_years, default=all_years)
    mode = col2.selectbox("Стиль справки", list(report_generator.MODE_TITLES.keys()),
                           format_func=lambda k: report_generator.MODE_TITLES[k])

    if st.button("📝 Сформировать справку", type="primary"):
        dyn0 = c.get("dynamics")
        buyers0 = c.get("buyers")
        suppliers0 = c.get("suppliers")
        esf_purchases0 = c.get("esf_purchases")
        esf_sales0 = c.get("esf_sales")

        dyn = dyn0[dyn0["year"].isin(years_sel)] if years_sel and dyn0 is not None and not dyn0.empty else (dyn0 if dyn0 is not None else pd.DataFrame())
        buyers = buyers0[buyers0["year"].isin(years_sel)] if years_sel and buyers0 is not None and not buyers0.empty else (buyers0 if buyers0 is not None else pd.DataFrame())
        suppliers = suppliers0[suppliers0["year"].isin(years_sel)] if years_sel and suppliers0 is not None and not suppliers0.empty else (suppliers0 if suppliers0 is not None else pd.DataFrame())

        buyers_by_year = {int(y): g.to_dict("records") for y, g in buyers.groupby("year")} if not buyers.empty else {}
        suppliers_by_year = {int(y): g.to_dict("records") for y, g in suppliers.groupby("year")} if not suppliers.empty else {}

        data_gaps = report_generator.build_data_gaps(
            has_taxpayer=profile is not None,
            has_fno100=bool(st.session_state.fno_results.get("100.00")),
            has_fno200=bool(st.session_state.fno_results.get("200.00") or st.session_state.fno_results.get("200.01")),
            has_fno300=bool(st.session_state.fno_results.get("300.00")),
            has_esf_purchases=esf_purchases0 is not None and not esf_purchases0.empty,
            has_esf_sales=esf_sales0 is not None and not esf_sales0.empty,
        )
        total_sales = dyn["sales_no_vat"].sum() if not dyn.empty else 0
        total_purchases = dyn["purchases_no_vat"].sum() if not dyn.empty else 0
        findings_by_level = c.get("findings_by_level", {})
        summary = report_generator.build_summary_text(
            profile.name, profile.bin_iin, profile.tax_regime, profile.vat_payer, profile.oked_name,
            total_sales, total_purchases, findings_by_level, data_gaps,
        )
        conclusion = report_generator.build_conclusion_text(findings_by_level, data_gaps)

        years_label = f"{years_sel[0]}-{years_sel[-1]}" if years_sel else "весь период"
        watermark_text = (
            "Демо-версия TaxMasterKZ. Для полного доступа обратитесь в TaxMasterKZ."
            if user and auth_db.needs_watermark(user) else None
        )
        ctx = report_generator.ReportContext(
            taxpayer_name=profile.name or "не определено", taxpayer_bin=profile.bin_iin or "не определён",
            years_label=years_label, mode=mode, generated_at=str(datetime.date.today()),
            profile=profile.fields, yearly_profile=profile.yearly,
            dynamics=dyn.to_dict("records"), buyers_by_year=buyers_by_year, suppliers_by_year=suppliers_by_year,
            vat_reconciliation=_df_records(c.get("vat_rec")), income_reconciliation=_df_records(c.get("income_rec")),
            payroll_check=_df_records(c.get("payroll")), findings_by_level=findings_by_level,
            documents_to_request=c.get("documents", []), summary_text=summary, conclusion_text=conclusion, data_gaps=data_gaps,
            watermark_text=watermark_text,
        )
        html = report_generator.render_html(ctx)
        st.session_state["_report_html"] = html
        st.session_state["_report_ctx"] = ctx
        if user:
            _audit(user["id"], user["username"], "generate_report", f"стиль={mode}, период={years_label}")

    if st.session_state.get("_report_html"):
        st.components.v1.html(st.session_state["_report_html"], height=700, scrolling=True)

        ctx = st.session_state["_report_ctx"]

        ok_word, msg_word = auth_db.check_feature(user, "word") if user else (True, "OK")
        if ok_word:
            # Абсолютный путь "/tmp/..." на Windows резолвится в "<текущий_диск>:\tmp\..."
            # (например, D:\tmp\...), а не в реальный временный каталог — так как этой
            # папки обычно нет, локальный запуск на Windows падал с ошибкой сохранения
            # Word-файла. tempfile.gettempdir() даёт корректный временный каталог и на
            # Windows, и на Linux (в т.ч. на Streamlit Cloud).
            docx_path = os.path.join(tempfile.gettempdir(), "_analytical_report.docx")
            try:
                report_generator.render_docx(ctx, docx_path)
                with open(docx_path, "rb") as f:
                    docx_bytes = f.read()
                if st.download_button("📥 Скачать справку (Word)", data=docx_bytes,
                                        file_name=f"Справка_{ctx.taxpayer_bin}.docx",
                                        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
                    if user:
                        _audit(user["id"], user["username"], "download_report", "Word")
            except Exception as e:
                st.error(f"Не удалось сформировать Word-файл: {e}")
        else:
            st.warning(msg_word)

        ok_pdf, msg_pdf = auth_db.check_feature(user, "pdf") if user else (True, "OK")
        if ok_pdf:
            if st.download_button("📥 Скачать справку (HTML)", data=st.session_state["_report_html"],
                                file_name=f"Справка_{ctx.taxpayer_bin}.html", mime="text/html"):
                if user:
                    _audit(user["id"], user["username"], "download_report", "HTML")
        else:
            st.warning(msg_pdf)

        excel_mode = auth_db.excel_mode_for(user) if user else "full"
        if excel_mode == "none":
            st.warning(f"Excel-экспорт недоступен на тарифе {user.get('plan') if user else ''}. Перейдите на START или PRO.")
        elif excel_mode == "basic":
            st.caption("На тарифе START доступны только основные своды (динамика, покупатели, поставщики). "
                       "Полный свод с рисками и сверкой ФНО — на PRO/TEAM/ENTERPRISE.")
            xlsx_bytes = export_utils.export_basic_workbook(
                c.get("dynamics", pd.DataFrame()), c.get("buyers", pd.DataFrame()), c.get("suppliers", pd.DataFrame())
            )
            if st.download_button("📥 Скачать Excel-приложения к справке (основные своды)", data=xlsx_bytes,
                                file_name=f"Приложения_{ctx.taxpayer_bin}.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
                if user:
                    _audit(user["id"], user["username"], "download_report", "Excel basic")
        else:
            xlsx_bytes = export_utils.export_full_workbook(
                c.get("dynamics", pd.DataFrame()), c.get("buyers", pd.DataFrame()), c.get("suppliers", pd.DataFrame()),
                c.get("findings", []), c.get("vat_rec", pd.DataFrame()), c.get("income_rec", pd.DataFrame())
            )
            if st.download_button("📥 Скачать Excel-приложения к справке", data=xlsx_bytes,
                                file_name=f"Приложения_{ctx.taxpayer_bin}.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
                if user:
                    _audit(user["id"], user["username"], "download_report", "Excel full")


def page_instructions():
    st.title("📖 Инструкция")
    st.caption("Пошаговое руководство для налогового специалиста — простым языком, без программистских терминов.")
    try:
        with open(INSTRUCTIONS_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        st.download_button("📥 Скачать инструкцию (.md)", data=content,
                            file_name="ИНСТРУКЦИЯ_ПОЛЬЗОВАТЕЛЯ.md", mime="text/markdown")
        st.divider()
        st.markdown(content)
    except FileNotFoundError:
        st.error(
            "Файл ИНСТРУКЦИЯ_ПОЛЬЗОВАТЕЛЯ.md не найден рядом с app.py. "
            "Скопируйте его в корень проекта, либо посмотрите инструкцию в репозитории."
        )
    st.divider()
    st.markdown(threads_badge_html(), unsafe_allow_html=True)


def page_settings():
    st.title("Настройки")
    st.subheader("Карта полей ФНО (config/fno_field_map.json)")
    st.caption(
        "КГД не публикует расшифровку внутренних кодов полей (field_XXX_XX_NNN) в выгрузках реестра ФНО. "
        "Чтобы приложение показывало суммовые показатели деклараций (доход, НДС и т.д.), сопоставьте нужные "
        "коды полей экономическим показателям вручную, сверяясь с формой декларации в кабинете налогоплательщика."
    )
    path = fno_parser.FIELD_MAP_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            current = f.read()
    except FileNotFoundError:
        current = "{}"
    edited = st.text_area("Содержимое fno_field_map.json", value=current, height=400)
    if st.button("💾 Сохранить карту полей"):
        try:
            json.loads(edited)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(edited)
            st.success("Сохранено. Пересчитайте аналитику, чтобы применить изменения.")
        except json.JSONDecodeError as e:
            st.error(f"Некорректный JSON: {e}")

    st.divider()
    st.subheader("История проверки")
    st.write(f"Файлов обработано: {len(st.session_state.files_registry)}")
    st.write(f"Дата формирования сессии: {datetime.date.today()}")
    if st.session_state.files_registry:
        st.dataframe(pd.DataFrame(st.session_state.files_registry)[["filename", "category", "status"]],
                     use_container_width=True, hide_index=True)


def _format_price(price) -> str:
    if price is None:
        return "Индивидуально"
    if price == 0:
        return "0 ₸"
    return f"{price:,.0f} ₸".replace(",", " ")


def page_my_plan():
    user = _current_user()
    st.title("💳 Мой тариф")
    if not user:
        st.warning("Вы не авторизованы.")
        return

    plan_code = user.get("plan")
    plan = auth_db.get_plan(plan_code) if plan_code else None

    if not plan:
        st.info(
            "Тариф вам ещё не назначен — действуют условия по умолчанию, без лимита анализов и без "
            "ограничений экспорта. По вопросам подключения тарифа обратитесь в TaxMasterKZ."
        )
        st.markdown(f"**Срок действия доступа:** {user.get('access_end_date') or user.get('expiry_date') or 'бессрочно'}")
    else:
        end_str = user.get("access_end_date") or user.get("expiry_date")
        days_left = None
        if end_str:
            try:
                days_left = (datetime.date.fromisoformat(end_str) - datetime.date.today()).days
            except ValueError:
                days_left = None

        c1, c2, c3 = st.columns(3)
        c1.metric("Тариф", plan_code)
        c2.metric("Дата начала доступа", user.get("access_start_date") or "—")
        c3.metric("Дата окончания доступа", end_str or "бессрочно")
        if plan.get("description"):
            st.caption(plan["description"])
        if days_left is not None:
            st.metric("Осталось дней", days_left if days_left >= 0 else 0)

        limit = user.get("analysis_limit")
        used = user.get("analysis_used") or 0
        d1, d2, d3 = st.columns(3)
        d1.metric("Лимит анализов", limit if limit is not None else "без лимита")
        d2.metric("Использовано анализов", used)
        d3.metric("Остаток анализов", max(limit - used, 0) if limit is not None else "без лимита")

        status_label = {
            "trial": "пробный период", "paid": "оплачено", "unpaid": "не оплачено",
            "overdue": "просрочено", "cancelled": "отменено",
        }.get(user.get("payment_status"), user.get("payment_status") or "не указан")
        st.markdown(f"**Статус оплаты:** {status_label}")

        if days_left is not None and days_left <= 3:
            st.warning("Ваш доступ скоро завершится. Для продления обратитесь в TaxMasterKZ.")

    st.divider()
    st.subheader("Контактная информация в системе")
    c1, c2 = st.columns(2)
    c1.markdown(f"**Компания:** {user.get('company_name') or '—'}")
    c1.markdown(f"**Телефон:** {user.get('phone') or '—'}")
    c2.markdown(f"**Email:** {user.get('email') or '—'}")

    st.divider()
    st.subheader("Контакты TaxMasterKZ")
    st.markdown(contact_badges_html(), unsafe_allow_html=True)


def page_plans():
    st.title("💼 Тарифы")
    st.caption("Выберите подходящий тариф. По вопросам подключения и оплаты — WhatsApp или Threads ниже.")
    plans = auth_db.get_plans(active_only=True)
    if plans:
        cols = st.columns(len(plans))
        for col, p in zip(cols, plans):
            with col:
                st.markdown(f"### {p['name']}")
                price_label = _format_price(p["price"])
                period = "/ 7 дней" if p["code"] == "DEMO" else ("/ месяц" if p["duration_days"] == 30 else "")
                st.markdown(f"**{price_label}** {period}")
                limit_label = "без ограничения" if p["analysis_limit"] is None else f"до {p['analysis_limit']} анализов"
                st.markdown(f"📊 {limit_label}")
                if p.get("max_users") and p["max_users"] > 1:
                    st.markdown(f"👥 до {p['max_users']} пользователей")
                st.caption(p.get("description") or "")
    else:
        st.info("Тарифы пока не настроены.")

    st.divider()
    b1, b2 = st.columns(2)
    with b1:
        st.markdown(
            f'<a href="{WHATSAPP_URL}" target="_blank" rel="noopener noreferrer" style="text-decoration:none;">'
            f'<div style="text-align:center;padding:10px;border-radius:8px;background:#25D366;color:#fff;font-weight:600;">'
            f'💬 Получить доступ / Написать в WhatsApp</div></a>',
            unsafe_allow_html=True,
        )
    with b2:
        st.markdown(threads_badge_html("TaxMasterKZ в Threads"), unsafe_allow_html=True)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def render_login_page():
    st.title(f"📊 {APP_NAME}")
    st.subheader("Налоговый анализ и камеральная сверка НП")
    st.markdown(
        "Загрузите ЭСФ, ФНО, карточку НП и получите свод по покупателям, поставщикам, "
        "расхождениям и налоговым рискам."
    )

    st.divider()
    st.subheader("Вход в систему")
    with st.form("login_form"):
        username = st.text_input("Логин")
        password = st.text_input("Пароль", type="password")
        submitted = st.form_submit_button("Войти", type="primary", use_container_width=True)
    if submitted:
        user, message = auth_db.authenticate((username or "").strip(), password or "")
        if user is None:
            st.error(message)
        else:
            st.session_state["auth_user"] = user
            _audit(user["id"], user["username"], "login", "")
            st.rerun()

    st.markdown(contact_badges_html(), unsafe_allow_html=True)
    st.caption(
        "Нет доступа? Напишите нам в WhatsApp или Threads — администратор создаст логин и пароль."
    )

    st.divider()
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**Что умеет TaxMasterKZ:**")
        st.markdown(
            "- анализ ЭСФ реализации и приобретения;\n"
            "- сверка с ФНО 100.00 и 300.00;\n"
            "- выявление расхождений;\n"
            "- анализ покупателей и поставщиков;\n"
            "- формирование аналитической справки."
        )
    with col_b:
        st.markdown("**Как получить доступ:**")
        st.markdown(
            '1. Нажмите "Написать в WhatsApp".\n'
            "2. Укажите ФИО и желаемый тариф.\n"
            "3. Администратор выдаст логин и пароль."
        )


def page_admin():
    user = _current_user()
    if not user or user["role"] != "admin":
        st.error("Доступ только для администратора.")
        return
    st.title("🛠 Админ-панель")
    st.caption("💡 Здесь можно управлять пользователями, тарифами, доступом и режимом обслуживания сервиса.")

    maintenance = auth_db.get_maintenance_mode()
    mcol1, mcol2 = st.columns([3, 1])
    mcol1.markdown(
        "**Режим обслуживания:** " +
        ("🔴 включён — обычные пользователи не могут пользоваться приложением" if maintenance else "🟢 выключен")
    )
    if mcol2.button("Выключить" if maintenance else "Включить", use_container_width=True):
        auth_db.set_maintenance_mode(not maintenance)
        _audit(user["id"], user["username"], "maintenance_off" if maintenance else "maintenance_on", "")
        st.rerun()

    # ------------------------------------------------------------------
    # Тарифы и доступ
    # ------------------------------------------------------------------
    st.divider()
    st.subheader("Тарифы и доступ")
    st.caption(
        "💡 Создавайте пользователей сразу с тарифом, меняйте тариф, продлевайте доступ, "
        "управляйте лимитом анализов и статусом оплаты."
    )

    with st.expander("➕ Создать пользователя (выдать доступ)", expanded=False):
        st.caption(
            "Впишите ФИО и нажмите «Сгенерировать логин и пароль» — поля ниже заполнятся "
            "автоматически (логин латиницей вида имя.фамилия, надёжный пароль). Значения "
            "можно поправить вручную перед созданием."
        )
        gcol1, gcol2 = st.columns([3, 1])
        gen_full_name = gcol1.text_input("ФИО пользователя", key="admin_gen_fullname")
        if gcol2.button("🎲 Сгенерировать логин и пароль", use_container_width=True):
            st.session_state["_gen_username"] = generate_username_from_fullname(gen_full_name)
            st.session_state["_gen_password"] = generate_strong_password()

        with st.form("create_user_form"):
            c1, c2, c3 = st.columns(3)
            new_username = c1.text_input("Логин", value=st.session_state.get("_gen_username", ""))
            new_password = c2.text_input("Пароль", value=st.session_state.get("_gen_password", ""))
            _role_default = auth_db.ROLES.index("user") if "user" in auth_db.ROLES else 0
            new_role = c3.selectbox("Роль", auth_db.ROLES, index=_role_default)
            plan_options = ["— без тарифа —"] + [p["code"] for p in auth_db.get_plans()]
            pcol1, pcol2, pcol3 = st.columns(3)
            new_plan_sel = pcol1.selectbox("Тариф", plan_options)
            new_end_date = pcol2.date_input("Срок доступа до даты (пусто = по тарифу/бессрочно)", value=None)
            new_limit_override = pcol3.number_input("Лимит анализов (0 = по тарифу/без лимита)",
                                                      min_value=0, value=0, step=1)
            cc1, cc2, cc3 = st.columns(3)
            new_company = cc1.text_input("Компания")
            new_phone = cc2.text_input("Телефон")
            new_email = cc3.text_input("Email")
            new_notes = st.text_area("Комментарий администратора", height=68)
            create_submitted = st.form_submit_button("Создать пользователя", type="primary")
        if create_submitted:
            plan_val = None if new_plan_sel == "— без тарифа —" else new_plan_sel
            uname = (new_username or "").strip()
            pwd_val = new_password or ""
            end_date_val = new_end_date.isoformat() if new_end_date else None
            limit_val = int(new_limit_override) if new_limit_override else None
            try:
                ok, msg = auth_db.create_user(
                    uname, pwd_val, role=new_role, tariff=plan_val,
                    expires_at=end_date_val, analysis_limit=limit_val,
                    full_name=(gen_full_name or "").strip() or None,
                    company=new_company or None, phone=new_phone or None,
                    email=new_email or None, comment=new_notes or None, created_by=user["username"],
                )
            except Exception as e:
                ok = False
                msg = f"Не удалось создать пользователя: {e}"
            if ok:
                created = auth_db.get_user(uname)
                _audit(created["id"] if created else None, uname, "create_user",
                       f"роль={new_role}, тариф={plan_val or 'нет'}", admin=user)
                expires_label = ((created.get("access_end_date") if created else end_date_val) or "бессрочно")
                whatsapp_text = (
                    "Здравствуйте! Вам открыт доступ к TaxMasterKZ.\n\n"
                    f"Ссылка для входа:\n{APP_PUBLIC_URL}\n\n"
                    f"Логин:\n{uname}\n\n"
                    f"Пароль:\n{pwd_val}\n\n"
                    f"Срок доступа:\n{expires_label}\n\n"
                    "Пожалуйста, не передавайте логин и пароль третьим лицам.\n\n"
                    "Если возникнут вопросы — напишите в WhatsApp."
                )
                st.session_state["_last_created_user_text"] = whatsapp_text
                st.session_state["_gen_username"] = ""
                st.session_state["_gen_password"] = ""
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)

        if st.session_state.get("_last_created_user_text"):
            st.markdown("**Текст для отправки пользователю** (скопируйте и отправьте в WhatsApp):")
            st.text_area("Текст для отправки пользователю", value=st.session_state["_last_created_user_text"],
                          height=220, key="_last_created_user_text_area", label_visibility="collapsed")

    st.divider()
    st.subheader("Пользователи")

    filter_opts = ["Активные", "Заблокированные", "Истекает ≤7 дней", "Просроченные", "Неоплаченные",
                   "DEMO", "START", "PRO", "TEAM", "ENTERPRISE"]
    active_filters = st.multiselect("Фильтры", filter_opts)

    users = auth_db.list_users()
    counts = auth_db.count_analyses_by_user()
    today = datetime.date.today()

    def _end_date(u):
        s = u.get("access_end_date") or u.get("expiry_date")
        if not s:
            return None
        try:
            return datetime.date.fromisoformat(s)
        except ValueError:
            return None

    def _matches(u):
        if not active_filters:
            return True
        end = _end_date(u)
        checks = {
            "Активные": u["status"] == "active",
            "Заблокированные": u["status"] == "blocked",
            "Истекает ≤7 дней": end is not None and 0 <= (end - today).days <= 7,
            "Просроченные": end is not None and (end - today).days < 0,
            "Неоплаченные": u.get("payment_status") in ("unpaid", "overdue"),
            "DEMO": u.get("plan") == "DEMO", "START": u.get("plan") == "START",
            "PRO": u.get("plan") == "PRO", "TEAM": u.get("plan") == "TEAM",
            "ENTERPRISE": u.get("plan") == "ENTERPRISE",
        }
        return any(checks.get(f, False) for f in active_filters)

    users_filtered = [u for u in users if _matches(u)]

    overview_rows = []
    for u in users_filtered:
        limit = u.get("analysis_limit")
        used = u.get("analysis_used") or 0
        overview_rows.append({
            "Логин": u["username"], "ФИО": u.get("full_name") or "—", "Роль": u["role"], "Тариф": u.get("plan") or "—",
            "Статус": u["status"], "Оплата": u.get("payment_status") or "—",
            "Доступ до": u.get("access_end_date") or u.get("expiry_date") or "бессрочно",
            "Лимит": str(limit) if limit is not None else "∞", "Использовано": str(used),
            "Остаток": str(limit - used) if limit is not None else "∞",
            "Телефон": u.get("phone") or "—", "Компания": u.get("company_name") or "—",
        })
    st.dataframe(
        pd.DataFrame(overview_rows) if overview_rows else pd.DataFrame([{"Информация": "Нет пользователей по выбранным фильтрам"}]),
        use_container_width=True, hide_index=True,
    )

    for u in users_filtered:
        status_icon = "🟢" if u["status"] == "active" else "🔴"
        plan_label = u.get("plan") or "без тарифа"
        with st.expander(f"{status_icon} {u['username']} — {u['role']} — {plan_label}"):
            st.write(f"Создан: {u['created_at']} (кем: {u.get('created_by') or '—'})")
            st.write(f"Последний вход: {u['last_login'] or 'ещё не входил'}")
            st.write(f"Количество анализов (за всё время): {counts.get(u['username'], 0)}")

            st.markdown("**Пароль / блокировка / удаление**")
            colA, colB, colD = st.columns(3)
            with colA:
                new_pw = st.text_input("Новый пароль", type="password", key=f"pw_{u['id']}")
                if st.button("Сменить пароль", key=f"pwbtn_{u['id']}"):
                    if new_pw:
                        auth_db.set_password(u["username"], new_pw)
                        _audit(u["id"], u["username"], "change_password", "", admin=user)
                        st.success("Пароль обновлён")
                    else:
                        st.warning("Введите новый пароль")
            with colB:
                if u["status"] == "active":
                    if st.button("Заблокировать", key=f"block_{u['id']}"):
                        auth_db.set_status(u["username"], "blocked")
                        _audit(u["id"], u["username"], "block_user", "", admin=user)
                        st.rerun()
                else:
                    if st.button("Разблокировать", key=f"unblock_{u['id']}"):
                        auth_db.set_status(u["username"], "active")
                        _audit(u["id"], u["username"], "unblock_user", "", admin=user)
                        st.rerun()
            with colD:
                if u["username"] != user["username"]:
                    if st.button("🗑 Удалить", key=f"del_{u['id']}"):
                        auth_db.delete_user(u["username"])
                        _audit(u["id"], u["username"], "delete_user", "", admin=user)
                        st.rerun()
                else:
                    st.caption("Нельзя удалить себя")

            st.divider()
            st.markdown("**Тариф**")
            plan_codes_ui = ["— без тарифа —"] + [p["code"] for p in auth_db.get_plans()]
            cur_idx = plan_codes_ui.index(u["plan"]) if u.get("plan") in plan_codes_ui else 0
            colP1, colP2 = st.columns([2, 1])
            new_plan_choice = colP1.selectbox("Сменить тариф", plan_codes_ui, index=cur_idx, key=f"plan_{u['id']}")
            if colP2.button("Применить тариф", key=f"planbtn_{u['id']}"):
                new_plan_val = None if new_plan_choice == "— без тарифа —" else new_plan_choice
                auth_db.set_plan(u["username"], new_plan_val)
                _audit(u["id"], u["username"], "change_plan", f"-> {new_plan_val or 'нет'}", admin=user)
                st.rerun()

            st.markdown("**Продление доступа**")
            colE1, colE2, colE3, colE4 = st.columns(4)
            if colE1.button("+1 месяц", key=f"ext1m_{u['id']}"):
                _, new_end = auth_db.extend_access(u["username"], months=1)
                _audit(u["id"], u["username"], "extend_access", f"+1 мес -> {new_end}", admin=user)
                st.rerun()
            if colE2.button("+3 месяца", key=f"ext3m_{u['id']}"):
                _, new_end = auth_db.extend_access(u["username"], months=3)
                _audit(u["id"], u["username"], "extend_access", f"+3 мес -> {new_end}", admin=user)
                st.rerun()
            if colE3.button("+1 год", key=f"ext1y_{u['id']}"):
                _, new_end = auth_db.extend_access(u["username"], years=1)
                _audit(u["id"], u["username"], "extend_access", f"+1 год -> {new_end}", admin=user)
                st.rerun()
            with colE4:
                manual_end = st.date_input("Дата окончания вручную", value=None, key=f"endmanual_{u['id']}")
                if st.button("Установить дату", key=f"endmanualbtn_{u['id']}"):
                    val = manual_end.isoformat() if manual_end else None
                    auth_db.set_access_end_date(u["username"], val)
                    _audit(u["id"], u["username"], "extend_access", f"вручную -> {val}", admin=user)
                    st.rerun()

            st.markdown("**Лимит анализов**")
            colL1, colL2, colL3, colL4, colL5 = st.columns(5)
            with colL1:
                new_limit = st.number_input("Лимит вручную", min_value=0,
                                             value=int(u.get("analysis_limit") or 0), key=f"limitval_{u['id']}")
                if st.button("Сохранить лимит", key=f"limitbtn_{u['id']}"):
                    auth_db.set_analysis_limit(u["username"], int(new_limit))
                    _audit(u["id"], u["username"], "set_analysis_limit", str(int(new_limit)), admin=user)
                    st.rerun()
            with colL2:
                if st.button("Сбросить использованные", key=f"resetused_{u['id']}"):
                    auth_db.reset_analysis_used(u["username"])
                    _audit(u["id"], u["username"], "reset_analysis_used", "", admin=user)
                    st.rerun()
            with colL3:
                if st.button("+10 анализов", key=f"add10_{u['id']}"):
                    nl = auth_db.add_analysis_quota(u["username"], 10)
                    _audit(u["id"], u["username"], "add_analysis_quota", f"+10 -> {nl}", admin=user)
                    st.rerun()
            with colL4:
                if st.button("+50 анализов", key=f"add50_{u['id']}"):
                    nl = auth_db.add_analysis_quota(u["username"], 50)
                    _audit(u["id"], u["username"], "add_analysis_quota", f"+50 -> {nl}", admin=user)
                    st.rerun()
            with colL5:
                if st.button("+100 анализов", key=f"add100_{u['id']}"):
                    nl = auth_db.add_analysis_quota(u["username"], 100)
                    _audit(u["id"], u["username"], "add_analysis_quota", f"+100 -> {nl}", admin=user)
                    st.rerun()

            st.markdown("**Оплата**")
            colPay1, colPay2 = st.columns([2, 1])
            pay_idx = auth_db.PAYMENT_STATUSES.index(u["payment_status"]) if u.get("payment_status") in auth_db.PAYMENT_STATUSES else 0
            new_pay = colPay1.selectbox("Статус оплаты", auth_db.PAYMENT_STATUSES, index=pay_idx, key=f"pay_{u['id']}")
            if colPay2.button("Сохранить статус оплаты", key=f"paybtn_{u['id']}"):
                auth_db.set_payment_status(u["username"], new_pay)
                _audit(u["id"], u["username"], "set_payment_status", new_pay, admin=user)
                st.rerun()

            st.markdown("**Контакты и комментарий**")
            colC1, colC2, colC3 = st.columns(3)
            comp = colC1.text_input("Компания", value=u.get("company_name") or "", key=f"comp_{u['id']}")
            ph = colC2.text_input("Телефон", value=u.get("phone") or "", key=f"ph_{u['id']}")
            em = colC3.text_input("Email", value=u.get("email") or "", key=f"em_{u['id']}")
            nt = st.text_area("Комментарий", value=u.get("notes") or "", key=f"nt_{u['id']}", height=68)
            if st.button("Сохранить контакты", key=f"contactbtn_{u['id']}"):
                auth_db.update_contact_info(u["username"], company_name=comp or None, phone=ph or None,
                                             email=em or None, notes=nt or None)
                _audit(u["id"], u["username"], "update_contact_info", "", admin=user)
                st.rerun()

    st.divider()
    st.subheader("Редактирование тарифов")
    st.caption(
        "Изменения цены/лимита/срока/описания сохраняются в базе и применяются ко всем НОВЫМ назначениям "
        "тарифа — уже выданный пользователям доступ автоматически не пересчитывается."
    )
    for p in auth_db.get_plans():
        with st.expander(f"{p['name']} ({p['code']})"):
            colp1, colp2, colp3 = st.columns(3)
            price_is_custom = colp1.checkbox("Индивидуальная цена (как ENTERPRISE)",
                                              value=p["price"] is None, key=f"pricecustom_{p['code']}")
            price_val = colp1.number_input("Цена, ₸", min_value=0,
                                            value=int(p["price"]) if p["price"] is not None else 0,
                                            key=f"price_{p['code']}", disabled=price_is_custom)
            dur_val = colp2.number_input("Срок доступа, дней (0 = задаётся вручную)", min_value=0,
                                          value=int(p["duration_days"]) if p["duration_days"] is not None else 0,
                                          key=f"dur_{p['code']}")
            limit_val = colp3.number_input("Лимит анализов (0 = без лимита)", min_value=0,
                                            value=int(p["analysis_limit"]) if p["analysis_limit"] is not None else 0,
                                            key=f"lim_{p['code']}")
            colp4, colp5, colp6 = st.columns(3)
            allow_word_val = colp4.checkbox("Word разрешён", value=bool(p["allow_word"]), key=f"aw_{p['code']}")
            allow_pdf_val = colp5.checkbox("PDF разрешён", value=bool(p["allow_pdf"]), key=f"ap_{p['code']}")
            excel_val = colp6.selectbox("Excel", auth_db.EXCEL_MODES,
                                         index=auth_db.EXCEL_MODES.index(p["excel_mode"]), key=f"ex_{p['code']}")
            watermark_val = st.checkbox("Водяной знак в справке (для DEMO)", value=bool(p["watermark"]), key=f"wm_{p['code']}")
            desc_val = st.text_area("Описание", value=p.get("description") or "", key=f"desc_{p['code']}", height=68)
            active_val = st.checkbox("Тариф активен (показывать на странице «Тарифы»)",
                                      value=bool(p["active"]), key=f"act_{p['code']}")
            if st.button("Сохранить тариф", key=f"saveplan_{p['code']}"):
                auth_db.update_plan(
                    p["code"], price=(None if price_is_custom else int(price_val)),
                    duration_days=(int(dur_val) or None), analysis_limit=(int(limit_val) or None),
                    allow_word=int(allow_word_val), allow_pdf=int(allow_pdf_val), excel_mode=excel_val,
                    watermark=int(watermark_val), description=desc_val, active=int(active_val),
                )
                _audit(user["id"], user["username"], "update_plan", p["code"], admin=user)
                st.success("Тариф обновлён")
                st.rerun()

    st.divider()
    st.subheader("История входов")
    logs = auth_db.get_login_history(limit=100)
    st.dataframe(pd.DataFrame(logs) if logs else pd.DataFrame([{"Информация": "Входов пока не было"}]),
                 use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Журнал действий")
    audits = auth_db.get_audit_logs(limit=300)
    st.dataframe(pd.DataFrame(audits) if audits else pd.DataFrame([{"Информация": "Действий пока не зафиксировано"}]),
                 use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Хранилище файлов")
    total_bytes = user_storage.total_storage_bytes()
    st.write(f"Всего занято: {total_bytes / (1024 * 1024):.1f} МБ")
    days = st.number_input("Удалить файлы старше (дней)", min_value=1, value=30)
    if st.button("Запустить автоочистку старых файлов"):
        n = user_storage.cleanup_old_files(days=int(days))
        _audit(user["id"], user["username"], "cleanup_old_files", f"{n} файлов, старше {days} дней")
        st.success(f"Удалено файлов: {n}")


def page_access():
    user = _current_user()
    st.title("🔑 Доступ")
    if not user:
        st.warning("Вы не авторизованы.")
        return
    fresh = auth_db.get_user_by_id(user["id"]) or user  # свежие данные лимита/статуса из БД

    role_label = {"admin": "администратор", "user": "пользователь", "demo": "демо-доступ"}.get(fresh["role"], fresh["role"])
    if fresh["status"] == "blocked":
        status_label = "🔴 заблокирован"
    else:
        _ok, _ = auth_db.check_access(fresh)
        status_label = "🟢 активен" if _ok else "🟠 истёк"

    limit = fresh.get("analysis_limit")
    used = fresh.get("analysis_used") or 0

    st.markdown(f"**Логин:** {fresh['username']}")
    st.markdown(f"**Роль:** {role_label}")
    st.markdown(f"**Статус доступа:** {status_label}")
    st.markdown(f"**Тариф:** {fresh.get('plan') or 'не назначен'}")
    st.markdown(f"**Срок действия доступа:** {fresh.get('access_end_date') or fresh.get('expiry_date') or 'бессрочно'}")
    st.markdown(f"**Анализов доступно:** {limit if limit is not None else 'без лимита'}")
    st.markdown(f"**Анализов использовано:** {used}")
    st.markdown(f"**Дата создания учётной записи:** {fresh['created_at']}")
    st.markdown(f"**Последний вход:** {fresh['last_login'] or 'текущий вход — первый'}")
    st.caption("Подробности по тарифам и оплате — на странице «Мой тариф».")

    st.divider()
    st.subheader("Ссылка для входа")
    st.markdown("Для входа используйте эту ссылку и свои логин/пароль.")
    st.code(APP_PUBLIC_URL, language=None)

    st.divider()
    st.subheader("Контакты TaxMasterKZ")
    st.markdown(contact_badges_html(), unsafe_allow_html=True)
    st.caption("По вопросам доступа, тарифа, продления срока или блокировки обращайтесь к администратору TaxMasterKZ.")


def page_terms():
    st.title("📋 Правила использования")
    st.markdown(
        "> Приложение выполняет предварительный аналитический анализ по загруженным данным. "
        "Выводы приложения не являются окончательным актом налоговой проверки. "
        "Пользователь обязан проверить первичные документы и корректность загруженных данных."
    )
    st.divider()
    st.markdown(
        "- Приложение не заменяет налоговую проверку и не делает окончательные юридические выводы.\n"
        "- Все риск-находки требуют дополнительной проверки документов.\n"
        "- Загруженные вами файлы доступны только вам; администратор может видеть метаданные "
        "(имя файла, дату, размер) в целях аудита, но не обязан просматривать содержимое.\n"
        "- Доступ предоставляется по тарифу: лимит анализов, срок действия и формат экспорта "
        "зависят от выбранного тарифа (см. страницу «Мой тариф»).\n"
        "- При завершении работы рекомендуется удалять более не нужные файлы кнопкой "
        "«Удалить все мои загруженные файлы» на странице «Загрузка данных»."
    )


PAGES = {
    "Главная": page_home,
    "Инструкция": page_instructions,
    "Загрузка данных": page_upload,
    "Дашборд": page_dashboard,
    "Покупатели": page_buyers,
    "Поставщики": page_suppliers,
    "ФНО": page_fno,
    "Камеральный контроль": page_cameral_control,
    "Генератор справки": page_report,
    "Мой тариф": page_my_plan,
    "Тарифы": page_plans,
    "Админ-панель": page_admin,
    "Доступ": page_access,
    "Правила использования": page_terms,
    "Настройки": page_settings,
}

if "_pending_nav" in st.session_state:
    st.session_state["nav_page"] = st.session_state.pop("_pending_nav")

# --------------------------------------------------------------------------
# логин-гейт и проверка доступа (выполняется на КАЖДОМ прогоне скрипта —
# это и есть "проверка сессии": если админ заблокировал пользователя,
# истёк срок доступа или изменился тариф, это применяется уже на следующем
# клике/действии, без необходимости перезаходить)
# --------------------------------------------------------------------------

if st.session_state.get("auth_user") is None:
    render_login_page()
    st.stop()

_fresh_user = auth_db.get_user_by_id(st.session_state["auth_user"]["id"])
if _fresh_user is None:
    st.session_state["auth_user"] = None
    st.rerun()

_access_ok, _access_reason = auth_db.check_access(_fresh_user)
if not _access_ok:
    st.title(f"📊 {APP_NAME}")
    st.error(_access_reason)
    st.markdown(contact_badges_html(), unsafe_allow_html=True)
    if st.button("🚪 Выйти", key="blocked_logout", type="primary"):
        _audit(_fresh_user["id"], _fresh_user["username"], "logout", "")
        st.session_state["auth_user"] = None
        st.rerun()
    st.stop()

st.session_state["auth_user"] = _fresh_user

if auth_db.get_maintenance_mode() and _fresh_user["role"] != "admin":
    st.title(f"📊 {APP_NAME}")
    st.warning("Сервис временно недоступен. По вопросам доступа обратитесь в TaxMasterKZ.")
    st.markdown(contact_badges_html(), unsafe_allow_html=True)
    if st.button("🚪 Выйти", key="maintenance_logout"):
        _audit(_fresh_user["id"], _fresh_user["username"], "logout", "")
        st.session_state["auth_user"] = None
        st.rerun()
    st.stop()

page = sidebar_nav()
PAGES[page]()
