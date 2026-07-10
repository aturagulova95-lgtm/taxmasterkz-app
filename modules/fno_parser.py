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

import json
import os
import re
from dataclasses import dataclass, field

import pandas as pd

from . import column_mapper as cm
from . import data_normalizer as dn
from .file_loader import LoadedTable

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
    "income", "cit_amount", "pit_amount",
    "vat_charged", "vat_credit", "vat_payable",
    "payroll_ipn", "social_tax", "opv", "so", "vosms", "opvr",
]


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
    out["source_file"] = table.source_file

    # сохраняем "сырые" поля field_* (и вообще всё, что не ушло в метаданные)
    field_cols = [c for c in df_raw.columns if cols_norm[c] not in _META_INDEX]
    raw_records = df_raw[field_cols].to_dict(orient="records") if field_cols else [{}] * len(df_raw)
    out["raw_fields"] = raw_records

    field_map = load_field_map().get(form_code or "", {})
    mapped_any = 0
    for role in ROLES:
        codes = [code for code, meta in field_map.items() if meta.get("role") == role]
        if codes:
            def extractor(rec, codes=codes):
                for c in codes:
                    if c in rec and rec[c] not in (None, ""):
                        return dn.normalize_amount(rec[c])
                return None
            out[role] = out["raw_fields"].apply(extractor)
            mapped_any += 1
        else:
            out[role] = None

    unmapped = len(ROLES) - mapped_any
    if unmapped == len(ROLES):
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
