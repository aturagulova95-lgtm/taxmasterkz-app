"""
esf_parser.py
Парсинг выгрузок ЭСФ (Информационная система "Электронные счета фактуры" КГД МФ РК)
в единую внутреннюю схему esf_purchases / esf_sales.

Особенность формата КГД: колонка БИН/ИИН встречается дважды с ОДИНАКОВЫМ
заголовком "ИИН/БИН" — один раз для отправителя(поставщика), один раз для
получателя(покупателя). Различить их можно только по позиции (первая идёт
перед "Наименование отправителя/поставщика", вторая — перед "Наименование
получателя"). Эта функция реализует данную эвристику, но при этом не
ломается, если формат отличается (например, только один БИН-столбец).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

from . import column_mapper as cm
from . import data_normalizer as dn
from .file_loader import LoadedTable

CANONICAL_COLUMNS = [
    "year", "quarter", "month",
    "esf_issue_date", "esf_turnover_date", "esf_number", "esf_type", "esf_status",
    "supplier_bin", "supplier_name", "buyer_bin", "buyer_name",
    "item_name", "tnved_code", "unit", "quantity",
    "amount_no_vat", "turnover_amount", "vat_rate", "vat_amount", "amount_with_vat",
    "snt_number", "snt_date",
    "source_file", "source_sheet", "direction",
]


@dataclass
class EsfParseResult:
    dataframe: pd.DataFrame
    direction: str  # "purchase" | "sale" | "unknown"
    warnings: list[str] = field(default_factory=list)
    mapping_report: list[dict] = field(default_factory=list)
    years_found: list[int] = field(default_factory=list)
    row_count: int = 0
    total_amount_no_vat: float = 0.0
    total_vat: float = 0.0


def guess_direction_from_filename(filename: str) -> str:
    low = filename.lower()
    if re.search(r"приобрет|purchase|закуп|входящ", low):
        return "purchase"
    if re.search(r"реализ|sale|продаж|исходящ", low):
        return "sale"
    return "unknown"


def _resolve_bin_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Разрешает неоднозначность колонок БИН/ИИН по позиции относительно
    колонок supplier_name / buyer_name.
    """
    cols = list(df.columns)
    bin_like = [c for c in cols if c == "bin_iin" or re.match(r"^bin_iin__\d+$", c)]

    if "supplier_bin" in cols and "buyer_bin" in cols:
        return df  # уже однозначно определены (нестандартный, но явный формат)

    if len(bin_like) >= 2:
        # первая встреченная БИН-колонка -> поставщик, вторая -> покупатель
        bin_like_sorted = sorted(bin_like, key=lambda c: cols.index(c))
        rename = {bin_like_sorted[0]: "supplier_bin", bin_like_sorted[1]: "buyer_bin"}
        df = df.rename(columns=rename)
    elif len(bin_like) == 1:
        # единственная БИН-колонка: определяем роль по соседству с колонкой имени
        only = bin_like[0]
        idx = cols.index(only)
        if "supplier_name" in cols and cols.index("supplier_name") < idx + 3:
            df = df.rename(columns={only: "supplier_bin"})
        elif "buyer_name" in cols:
            df = df.rename(columns={only: "buyer_bin"})
    return df


def parse_esf_table(table: LoadedTable, direction_hint: str = "unknown",
                     taxpayer_bin: str | None = None) -> EsfParseResult | None:
    """Парсит один загруженный лист как таблицу ЭСФ. Возвращает None, если лист не похож на ЭСФ."""
    df_raw = table.dataframe
    if df_raw.empty:
        return None

    mapped_df, report = cm.map_dataframe_columns(df_raw)
    mapped_df = _resolve_bin_columns(mapped_df)

    esf_markers = {"esf_number", "esf_turnover_date", "esf_issue_date", "vat_amount"}
    if len(esf_markers & set(mapped_df.columns)) < 2:
        return None  # не похоже на выгрузку ЭСФ

    warnings = list(table.warnings)

    out = pd.DataFrame(index=mapped_df.index)
    out["esf_issue_date"] = mapped_df.get("esf_issue_date", pd.Series(dtype=object)).apply(dn.normalize_date)
    out["esf_turnover_date"] = mapped_df.get("esf_turnover_date", pd.Series(dtype=object)).apply(dn.normalize_date)
    out["esf_number"] = mapped_df.get("esf_number", pd.Series(dtype=object)).apply(dn.normalize_text)
    out["esf_type"] = mapped_df.get("esf_type", pd.Series(dtype=object)).apply(dn.normalize_text)
    out["esf_status"] = mapped_df.get("esf_status", pd.Series(dtype=object)).apply(dn.normalize_text)
    out["supplier_bin"] = mapped_df.get("supplier_bin", pd.Series(dtype=object)).apply(dn.normalize_bin)
    out["supplier_name"] = mapped_df.get("supplier_name", pd.Series(dtype=object)).apply(dn.normalize_text)
    out["buyer_bin"] = mapped_df.get("buyer_bin", pd.Series(dtype=object)).apply(dn.normalize_bin)
    out["buyer_name"] = mapped_df.get("buyer_name", pd.Series(dtype=object)).apply(dn.normalize_text)
    out["item_name"] = mapped_df.get("item_name", pd.Series(dtype=object)).apply(dn.normalize_text)
    out["tnved_code"] = mapped_df.get("tnved_code", pd.Series(dtype=object)).apply(dn.normalize_text)
    out["unit"] = mapped_df.get("unit", pd.Series(dtype=object)).apply(dn.normalize_text)
    out["quantity"] = mapped_df.get("quantity", pd.Series(dtype=object)).apply(dn.normalize_amount)
    out["amount_no_vat"] = mapped_df.get("amount_no_vat", pd.Series(dtype=object)).apply(dn.normalize_amount)
    out["turnover_amount"] = mapped_df.get("turnover_amount", pd.Series(dtype=object)).apply(dn.normalize_amount)
    out["vat_rate"] = mapped_df.get("vat_rate", pd.Series(dtype=object)).apply(dn.normalize_text)
    out["vat_amount"] = mapped_df.get("vat_amount", pd.Series(dtype=object)).apply(dn.normalize_amount)
    out["amount_with_vat"] = mapped_df.get("amount_with_vat", pd.Series(dtype=object)).apply(dn.normalize_amount)
    out["snt_number"] = mapped_df.get("snt_number", pd.Series(dtype=object)).apply(dn.normalize_text)
    out["snt_date"] = mapped_df.get("snt_date", pd.Series(dtype=object)).apply(dn.normalize_date)

    # отбрасываем полностью пустые строки (нет ни даты оборота, ни номера ЭСФ)
    out = out[~(out["esf_turnover_date"].isna() & out["esf_number"].isna())]

    out["year"] = out["esf_turnover_date"].apply(lambda d: d.year if d else None)
    out["quarter"] = out["esf_turnover_date"].apply(dn.quarter_of)
    out["month"] = out["esf_turnover_date"].apply(lambda d: d.month if d else None)

    out["source_file"] = table.source_file
    out["source_sheet"] = table.sheet_name

    # определение направления (приобретение/реализация)
    direction = direction_hint
    if taxpayer_bin:
        as_buyer = (out["buyer_bin"] == taxpayer_bin).sum()
        as_supplier = (out["supplier_bin"] == taxpayer_bin).sum()
        inferred = "purchase" if as_buyer >= as_supplier else "sale"
        if direction_hint != "unknown" and inferred != direction_hint:
            warnings.append(
                f"Название файла предполагает '{direction_hint}', но по БИН НП "
                f"({taxpayer_bin}) похоже на '{inferred}'. Проверьте направление вручную."
            )
        direction = direction_hint if direction_hint != "unknown" else inferred
    if direction == "unknown":
        direction = "purchase"
        warnings.append("Не удалось однозначно определить направление (приобретение/реализация) — принято 'приобретение', проверьте вручную.")

    out["direction"] = direction

    # дубли ЭСФ (одинаковый номер + одинаковая строка товара) — не удаляем,
    # только помечаем, чтобы аналитика могла учесть/исключить их сознательно
    dup_mask = out.duplicated(subset=["esf_number", "item_name", "amount_no_vat"], keep=False)
    if dup_mask.any():
        warnings.append(f"Обнаружено потенциальных дублей строк: {int(dup_mask.sum())}. Строки сохранены, но помечены.")
    out["is_potential_duplicate"] = dup_mask

    years = sorted({y for y in out["year"].dropna().unique().tolist()})

    return EsfParseResult(
        dataframe=out.reset_index(drop=True),
        direction=direction,
        warnings=warnings,
        mapping_report=report,
        years_found=[int(y) for y in years],
        row_count=len(out),
        total_amount_no_vat=float(out["amount_no_vat"].fillna(0).sum()),
        total_vat=float(out["vat_amount"].fillna(0).sum()),
    )


def parse_esf_file(tables: list[LoadedTable], filename: str,
                    taxpayer_bin: str | None = None,
                    direction_override: str | None = None) -> list[EsfParseResult]:
    """Парсит все листы файла, возвращает список результатов (обычно один)."""
    direction_hint = direction_override or guess_direction_from_filename(filename)
    results = []
    for table in tables:
        res = parse_esf_table(table, direction_hint=direction_hint, taxpayer_bin=taxpayer_bin)
        if res is not None and res.row_count > 0:
            results.append(res)
    return results
