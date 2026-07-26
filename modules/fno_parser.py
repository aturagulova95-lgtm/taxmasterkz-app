"""
fno_parser.py
Парсинг выгрузок реестров налоговой отчётности (ФНО 100.00, 200.00, 200.01,
300.00 и др.) из "Кабинета налогоплательщика" / КГД.

ВАЖНО про формат исходных данных: типовая выгрузка КГД по ФНО — это не
"построчная" декларация с человекочитаемыми названиями строк, а таблица,
где каждая строка = один сданный документ (форма), а суммовые показатели
закодированы в столбцах вида field_100_00_019_03 и т.п. (внутренние коды
полей ИС СОНО). Официальной публичной расшифровки "код поля -> экономический
смысл" в этих выгрузках нет, поэтому приложение:

  1. Надёжно распознаёт и нормализует МЕТАДАННЫЕ формы (БИН, вид, номер,
     статус, даты, период) — это не зависит ни от вида формы, ни от года.
  2. Сохраняет ВСЕ поля field_* как есть (raw_fields), ничего не теряя.
  3. Даёт возможность сопоставить конкретные field_* конкретным
     экономическим показателям (доход, НДС начисленный, НДС в зачёт,
     ИПН, ОПВ и т.д.) через конфигурируемый словарь
     config/fno_field_map.json. Пока словарь не заполнен для конкретного
     кода поля — приложение честно показывает "не сопоставлено" вместо
     того, чтобы гадать и рисковать выдать неверную сумму.

Такой подход соответствует требованию не делать категоричных выводов без
проверенных данных.
"""

from __future__ import annotations

import io
import json
import os
import re
from dataclasses import dataclass, field

import pandas as pd

from . import column_mapper as cm
from . import data_normalizer as dn
from .file_loader import LoadedTable

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
FIELD_MAP_PATH = os.path.join(CONFIG_DIR, "fno_field_map.json")

META_ALIASES = {
    "fno_bin": ["иин/бин", "бин/иин"],
    "fno_view": ["вид фно"],
    "fno_reg_number": ["регистрационный номер"],
    "fno_code": ["код фно"],
    "fno_accept_date": ["дата приема"],
    "fno_submit_date": ["дата подачи"],
    "fno_status": ["статус документа"],
    "fno_period_year": ["отчетный год"],
    "fno_period_quarter": ["отчетный квартал", "налоговый период"],
    "fno_category": ["категория"],
}

KNOWN_FORM_CODES = ["100.00", "200.00", "200.01", "300.00", "910.00", "220.00", "328.00", "701.00", "870.00"]

ROLES = [
    # 100.00 (КПН)
    "income_declared", "income_total", "deductions", "taxable_income", "cit_calculated",
    # 300.00 (НДС)
    "vat_charged", "vat_credit", "vat_payable", "vat_excess",
    # задел на будущее (200.00 / 200.01 и т.п.)
    "income", "cit_amount", "pit_amount",
    "payroll_ipn", "social_tax", "opv", "so", "vosms", "opvr",
]

# человекочитаемые названия ролей — для сообщений о ненайденных полях
ROLE_LABELS = {
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

# какие роли считаются "своими" для формы при подсчёте "не сопоставлено ни одного поля" —
# без этого разбиения показатели НДС для формы 100.00 (и наоборот) всегда были бы "не найдены"
FORM_ROLES = {
    "100.00": ["income_declared", "income_total", "deductions", "taxable_income", "cit_calculated"],
    "300.00": ["vat_charged", "vat_credit", "vat_payable", "vat_excess"],
    "200.00": ["payroll_ipn", "social_tax", "opv", "so", "vosms", "opvr"],
    "200.01": ["pit_amount", "payroll_ipn"],
}


def _norm(s: str) -> str:
    s = str(s or "").lower().strip().replace("ё", "е")
    s = re.sub(r"[\s_]+", " ", s)
    s = re.sub(r"[.,;:()%]+", "", s)
    return s


_META_INDEX = {}
for canon, variants in META_ALIASES.items():
    for v in variants:
        _META_INDEX[_norm(v)] = canon


def detect_form_code(filename: str, columns: list[str]) -> str | None:
    for code in KNOWN_FORM_CODES:
        if code in filename:
            return code
    for col in columns:
        if re.match(r"^\d{3}\.\d{2}$", str(col).strip()):
            return str(col).strip()
    return None


def load_field_map() -> dict:
    """Загружает конфигурируемый словарь field_code -> {label, role} по формам."""
    if os.path.exists(FIELD_MAP_PATH):
        try:
            with open(FIELD_MAP_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


@dataclass
class FnoParseResult:
    dataframe: pd.DataFrame
    form_code: str | None
    row_count: int
    years_found: list[int]
    warnings: list[str] = field(default_factory=list)
    unmapped_field_count: int = 0


def parse_fno_table(table: LoadedTable, filename_hint: str = "") -> FnoParseResult | None:
    df_raw = table.dataframe
    if df_raw.empty:
        return None

    cols_norm = {c: _norm(c) for c in df_raw.columns}
    meta_markers = {"иин/бин", "регистрационный номер", "статус документа"}
    if len(meta_markers & set(cols_norm.values())) < 2:
        return None  # не похоже на реестр ФНО

    warnings = list(table.warnings)
    form_code = detect_form_code(filename_hint or table.source_file, list(df_raw.columns))

    out = pd.DataFrame(index=df_raw.index)
    for col, norm in cols_norm.items():
        if norm in _META_INDEX:
            canon = _META_INDEX[norm]
            if canon not in out.columns:
                out[canon] = df_raw[col]

    out["fno_bin"] = out.get("fno_bin", pd.Series(dtype=object)).apply(dn.normalize_bin)
    out["fno_reg_number"] = out.get("fno_reg_number", pd.Series(dtype=object)).apply(dn.normalize_text)
    out["fno_view"] = out.get("fno_view", pd.Series(dtype=object)).apply(dn.normalize_text)
    out["fno_status"] = out.get("fno_status", pd.Series(dtype=object)).apply(dn.normalize_text)
    out["fno_accept_date"] = out.get("fno_accept_date", pd.Series(dtype=object)).apply(dn.normalize_date)
    out["fno_submit_date"] = out.get("fno_submit_date", pd.Series(dtype=object)).apply(dn.normalize_date)

    if "fno_period_year" in out.columns:
        out["year"] = out["fno_period_year"].apply(lambda v: dn.normalize_amount(v))
        out["year"] = out["year"].apply(lambda v: int(v) if v else None)
    else:
        out["year"] = out["fno_accept_date"].apply(lambda d: d.year if d else None)

    if "fno_period_quarter" in out.columns:
        out["quarter"] = out["fno_period_quarter"].apply(_extract_quarter)
    else:
        out["quarter"] = None

    out["form_code"] = form_code
    out["fno_code"] = form_code  # алиас form_code для единообразного отображения на странице «ФНО»
    out["source_file"] = table.source_file

    # сохраняем "сырые" поля field_* (и вообще всё, что не ушло в метаданные)
    field_cols = [c for c in df_raw.columns if cols_norm[c] not in _META_INDEX]
    raw_records = df_raw[field_cols].to_dict(orient="records") if field_cols else [{}] * len(df_raw)
    out["raw_fields"] = raw_records

    field_map = load_field_map().get(form_code or "", {})
    relevant_roles = FORM_ROLES.get(form_code or "", ROLES)
    field_cols_set = set(field_cols)

    mapped_any = 0
    missing_but_configured = []  # роли, для которых код задан в карте, но не найден в файле
    for role in ROLES:
        codes = [code for code, meta in field_map.items() if meta.get("role") == role]
        if not codes:
            out[role] = None
            continue
        available_codes = [c for c in codes if c in field_cols_set]
        if available_codes:
            def extractor(rec, codes=available_codes):
                for c in codes:
                    if c in rec and rec[c] not in (None, ""):
                        return dn.normalize_amount(rec[c])
                return None
            out[role] = out["raw_fields"].apply(extractor)
            if role in relevant_roles:
                mapped_any += 1
        else:
            out[role] = None
            if role in relevant_roles:
                missing_but_configured.append((role, codes))

    for role, codes in missing_but_configured:
        label = ROLE_LABELS.get(role, role)
        warnings.append(
            f"Показатель «{label}» настроен в карте полей (ожидались коды: {', '.join(codes)}), "
            f"но такого поля нет в этом файле. Возможно, в этой версии/периоде формы КГД "
            f"использует другой код — проверьте и уточните карту полей в разделе «Настройки»."
        )

    unmapped = len(relevant_roles) - mapped_any
    if relevant_roles and mapped_any == 0:
        warnings.append(
            f"Для формы {form_code or '?'} не задано ни одного сопоставления полей "
            f"(config/fno_field_map.json). Суммовые показатели по декларации недоступны, "
            f"доступны только регистрационные метаданные (номер, статус, даты, период)."
        )

    out = out.dropna(subset=["fno_reg_number"], how="all") if "fno_reg_number" in out.columns else out
    years = sorted({int(y) for y in out["year"].dropna().unique().tolist()})

    return FnoParseResult(
        dataframe=out.reset_index(drop=True),
        form_code=form_code,
        row_count=len(out),
        years_found=years,
        warnings=warnings,
        unmapped_field_count=unmapped,
    )


def _extract_quarter(value) -> int | None:
    s = str(value or "")
    m = re.search(r"([1-4])\s*(?:кв|quarter|q)?", s.lower())
    if m and len(s) < 15:
        return int(m.group(1))
    return None


def parse_fno_file(tables: list[LoadedTable], filename: str) -> list[FnoParseResult]:
    results = []
    for table in tables:
        res = parse_fno_table(table, filename_hint=filename)
        if res is not None and res.row_count > 0:
            results.append(res)
    return results


# --------------------------------------------------------------------------
# PDF ФНО (декларация в печатной форме, не выгрузка реестра)
#
# В отличие от Excel-реестра, где суммовые показатели закодированы во
# внутренних полях field_XXX_XX_NNN (см. модуль выше), в PDF-декларации
# печатаются ОФИЦИАЛЬНЫЕ номера строк формы (например, "100.00.001",
# "300.00.012") рядом с человекочитаемым названием и суммой — поэтому здесь
# используется отдельный, текстовый (regex) способ распознавания вместо
# карты полей config/fno_field_map.json. Номера строк в фактическом бланке
# декларации совпадают с кодами в fno_field_map.json (это подтверждено на
# реальной выгрузке — см. "_readme" в конфиге), поэтому роли (income_declared,
# vat_charged и т.д.) переиспользуются те же самые.
# --------------------------------------------------------------------------

PDF_ROLE_PATTERNS: dict[str, list[str]] = {
    "income_declared": [r"100\.00\.001", r"(?<!\d)001(?!\d)", r"совокупн\w*\s+годов\w*\s+доход"],
    "income_total": [r"100\.00\.015", r"(?<!\d)015(?!\d)", r"доход.{0,20}(?:учет|учёт)\w*.{0,20}корректировок?"],
    "deductions": [r"100\.00\.040", r"(?<!\d)040(?!\d)", r"итого\s+сумма\s+вычетов", r"\bвычет\w*"],
    "taxable_income": [r"100\.00\.044", r"(?<!\d)044(?!\d)", r"налогооблагаем\w*\s+доход"],
    "cit_calculated": [
        r"100\.00\.057", r"(?<!\d)057(?!\d)", r"кпн\s+исчисленн\w*",
        r"корпоративн\w*\s+подоходн\w*\s+налог\w*\s+исчисленн\w*",
    ],
    "vat_charged": [
        r"300\.00\.012", r"(?<!\d)012(?!\d)", r"ндс\s+начисленн\w*",
        r"облагаем\w*\s+оборот", r"сумма\s+ндс",
    ],
    "vat_credit": [r"300\.00\.023", r"(?<!\d)023(?!\d)", r"ндс.{0,20}зачет", r"относим\w*.{0,15}зачет"],
    "vat_payable": [r"300\.00\.030\.01", r"030\.01", r"ндс\s+к\s+уплате"],
    "vat_excess": [r"300\.00\.030\.02", r"030\.02", r"превышени\w*\s+ндс"],
}

_PDF_FORM_CODE_RE = re.compile(r"\b(100\.00|200\.00|200\.01|300\.00)\b")
_PDF_BIN_RE = re.compile(r"(?:бин\s*/\s*иин|иин\s*/\s*бин|\bбин\b|\bиин\b)[^\d]{0,15}(\d[\d\s]{9,14}\d)", re.I)
_PDF_REG_NUM_RE = re.compile(
    r"(?:регистрационный\s+номер|номер\s+документа|№\s*документа)[^\wа-яё0-9]{0,5}([\w\-/\.]{3,})", re.I
)
_PDF_YEAR_RE = re.compile(r"(?:отчетн\w*|отчётн\w*)\s+год[^\d]{0,6}(\d{4})", re.I)
_PDF_QUARTER_RE = re.compile(r"квартал[^\d]{0,6}([1-4])", re.I)
_PDF_STATUS_RE = re.compile(r"статус(?:\s+документа)?[^\S\n]{0,3}[:\s]+([^\n]{2,40})", re.I)
_PDF_VIEW_RE = re.compile(r"(очередна\w*|дополнительна\w*)", re.I)
_PDF_ACCEPT_DATE_RE = re.compile(r"дата\s+прием\w*[^\d]{0,6}(\d{1,2}[./]\d{1,2}[./]\d{2,4})", re.I)
_PDF_SUBMIT_DATE_RE = re.compile(r"дата\s+подач\w*[^\d]{0,6}(\d{1,2}[./]\d{1,2}[./]\d{2,4})", re.I)

MIN_PDF_TEXT_LEN = 30  # меньше — считаем, что текстовый слой отсутствует (скан)


def _extract_amount_from_line(line: str) -> float | None:
    """Ищет числовой кандидат в строке (с пробелами/запятыми/точками-разделителями)
    и нормализует его через data_normalizer.normalize_amount (см. форматы в ТЗ:
    '1 234 567', '1 234 567,00', '1,234,567.00', '1234567')."""
    candidates = re.findall(r"-?\d[\d\s .,]*\d|-?\d", line)
    for cand in reversed(candidates):
        val = dn.normalize_amount(cand)
        if val is not None:
            return val
    return None


def _find_role_amount(lines: list[str], patterns: list[str]) -> float | None:
    for i, line in enumerate(lines):
        low = line.lower()
        for pat in patterns:
            m = re.search(pat, low)
            if not m:
                continue
            # Сумму ищем ТОЛЬКО в части строки ПОСЛЕ найденного кода/ключевого слова.
            # Если искать по всей строке, а сумма стоит в той же строке сразу после
            # кода вида "100.00.001" (с точками), жадный числовой паттерн захватывает
            # и сам код вместе с суммой как одну "число"-строку с несколькими точками,
            # которая не парсится как float — сумма терялась даже когда была на месте.
            tail = line[m.end():]
            amt = _extract_amount_from_line(tail)
            if amt is not None:
                return amt
            if i + 1 < len(lines):
                amt = _extract_amount_from_line(lines[i + 1])
                if amt is not None:
                    return amt
    return None


def parse_fno_pdf_text(text: str, filename: str = "") -> FnoParseResult | None:
    """
    Основная логика разбора текста PDF-декларации ФНО (без обращения к файлу —
    вынесено отдельно от parse_fno_pdf(), чтобы быть тестируемым напрямую на
    строке текста, независимо от pdfplumber/PDF-рендеринга).

    Возвращает None, если текст вообще не похож на декларацию ФНО (нет ни
    БИН/ИИН, ни распознанного кода формы) — тогда вызывающий код должен
    показать общее "не распознано", а не предупреждение про суммы/скан.
    """
    stripped = (text or "").strip()
    if len(stripped) < MIN_PDF_TEXT_LEN:
        return None

    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    full_low = stripped.lower()

    form_code = None
    m = _PDF_FORM_CODE_RE.search(filename or "")
    if m:
        form_code = m.group(1)
    else:
        m = _PDF_FORM_CODE_RE.search(stripped)
        if m:
            form_code = m.group(1)

    bin_match = _PDF_BIN_RE.search(full_low)
    fno_bin = dn.normalize_bin(bin_match.group(1)) if bin_match else None

    if form_code is None and fno_bin is None:
        return None  # не похоже на декларацию ФНО вообще

    reg_match = _PDF_REG_NUM_RE.search(stripped)
    fno_reg_number = dn.normalize_text(reg_match.group(1)) if reg_match else None
    year_match = _PDF_YEAR_RE.search(full_low)
    quarter_match = _PDF_QUARTER_RE.search(full_low)
    status_match = _PDF_STATUS_RE.search(stripped)
    view_match = _PDF_VIEW_RE.search(full_low)
    accept_match = _PDF_ACCEPT_DATE_RE.search(full_low)
    submit_match = _PDF_SUBMIT_DATE_RE.search(full_low)

    year = int(year_match.group(1)) if year_match else None
    quarter = int(quarter_match.group(1)) if quarter_match else None
    fno_status = dn.normalize_text(status_match.group(1)) if status_match else None
    fno_view = dn.normalize_text(view_match.group(1)).capitalize() if view_match else None
    fno_accept_date = dn.normalize_date(accept_match.group(1)) if accept_match else None
    fno_submit_date = dn.normalize_date(submit_match.group(1)) if submit_match else None
    if year is None and fno_accept_date:
        year = fno_accept_date.year

    warnings: list[str] = []
    row: dict = {
        "fno_bin": fno_bin,
        "fno_code": form_code,
        "form_code": form_code,
        "fno_view": fno_view,
        "fno_reg_number": fno_reg_number,
        "fno_status": fno_status,
        "fno_accept_date": fno_accept_date,
        "fno_submit_date": fno_submit_date,
        "year": year,
        "quarter": quarter,
        "source_file": filename,
    }

    relevant_roles = FORM_ROLES.get(form_code or "", [])
    found_labels, missing_labels = [], []
    for role in ROLES:
        if role not in relevant_roles:
            row[role] = None
            continue
        amt = _find_role_amount(lines, PDF_ROLE_PATTERNS.get(role, []))
        row[role] = amt
        if amt is not None:
            found_labels.append(ROLE_LABELS.get(role, role))
        else:
            missing_labels.append(ROLE_LABELS.get(role, role))

    if relevant_roles and not found_labels:
        warnings.append(
            "ФНО загружена, но суммовые показатели не распознаны. "
            "Проверьте качество PDF или загрузите Excel-выгрузку."
        )
    elif missing_labels:
        warnings.append(
            "Не удалось определить суммы по показателям: " + ", ".join(missing_labels) +
            ". Проверьте качество PDF (не обрезан ли текст) или загрузите Excel-выгрузку."
        )

    df = pd.DataFrame([row])
    years = [year] if year is not None else []
    return FnoParseResult(
        dataframe=df,
        form_code=form_code,
        row_count=1,
        years_found=years,
        warnings=warnings,
        unmapped_field_count=len(missing_labels),
    )


def parse_fno_pdf(file_bytes: bytes, filename: str = "") -> list[FnoParseResult]:
    """
    Разбор ФНО из PDF-декларации (в отличие от parse_fno_file() — реестра
    выгрузки в Excel/CSV). Один PDF трактуется как одна декларация -> один
    результат с DataFrame из одной строки, чтобы дальше объединяться с
    Excel-результатами того же кода формы через pd.concat без ошибок (схема
    колонок пересекается: fno_bin, fno_code, fno_view, ..., income_declared,
    vat_charged и т.д.).

    Не бросает исключений при "сканах" (нет текстового слоя) или повреждённом
    PDF — в обоих случаях возвращает результат с понятным предупреждением,
    а не падает и не возвращает пусто без объяснения.
    """
    if pdfplumber is None:
        return [FnoParseResult(
            dataframe=pd.DataFrame([{"source_file": filename}]),
            form_code=None, row_count=1, years_found=[],
            warnings=["Модуль pdfplumber не установлен — PDF ФНО не может быть прочитан. "
                      "Загрузите Excel-выгрузку ФНО."],
        )]

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as e:
        return [FnoParseResult(
            dataframe=pd.DataFrame([{"source_file": filename}]),
            form_code=None, row_count=1, years_found=[],
            warnings=[f"Не удалось открыть PDF ФНО ({filename}): {e}. "
                      f"Проверьте, что файл не повреждён, либо загрузите Excel-выгрузку."],
        )]

    if len((text or "").strip()) < MIN_PDF_TEXT_LEN:
        return [FnoParseResult(
            dataframe=pd.DataFrame([{"source_file": filename}]),
            form_code=None, row_count=1, years_found=[],
            warnings=["PDF загружен, но текст не распознан. Возможно, это скан. "
                      "Загрузите Excel-выгрузку ФНО или PDF с текстовым слоем."],
        )]

    res = parse_fno_pdf_text(text, filename)
    if res is None:
        return []  # текст есть, но это не похоже на декларацию ФНО вообще
    return [res]
