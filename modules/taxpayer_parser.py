"""
taxpayer_parser.py
Разбор "Карточки налогоплательщика" / сведений СУР КГД (PDF) и аналогичных
выгрузок в Excel.

Парсинг PDF построен не на фиксированных номерах полей (они могут сдвигаться
у разных НП, если каких-то строк нет), а на сопоставлении ТЕКСТА подписи
поля с каноническим ключом (см. LABEL_KEYWORDS) — это даёт универсальность.

Таблица "Налоговая статистика" (численность, поступления, КНН по годам)
распознаётся отдельно: сначала ищется строка-"шапка" с годами, затем
построчно сопоставляются показатели с этими годами по позиции.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pandas as pd

from . import data_normalizer as dn
from . import column_mapper as cm

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None


LABEL_KEYWORDS: list[tuple[str, str, str]] = [
    # (regex по нормализованной подписи, канонический ключ, тип значения)
    (r"^бин.?иин$|^иин.?бин$", "bin_iin", "text"),
    (r"^наименование$", "name", "text"),
    (r"^дата регистрации$", "date_registration", "date"),
    (r"^окэд$", "oked", "text"),
    (r"наименование окэд", "oked_name", "text"),
    (r"дата рег\.? окэд", "oked_date", "date"),
    (r"плательщик ндс", "vat_payer", "yesno"),
    (r"дата рег\.? плат\.? ндс", "vat_reg_date", "date"),
    (r"^налоговый режим$", "tax_regime", "text"),
    (r"дата регистрации режима", "regime_reg_date", "date"),
    (r"регистрация признана недействительной", "risk_registration_invalid", "yesno"),
    (r"перерегистрация признана недействительной", "risk_reregistration_invalid", "yesno"),
    (r"сделки.*без.*фактического выполнения", "risk_sham_transactions", "yesno"),
    (r"произведено ограничение выписки эсф", "risk_esf_restriction", "yesno"),
    (r"признан бездействующим", "risk_inactive", "yesno"),
    (r"отсутствующие по юридическому адресу", "risk_no_legal_address", "yesno"),
    (r"признан банкротом", "risk_bankrupt", "yesno"),
    (r"реестр саморегулируемых", "risk_bankruptcy_registry", "yesno"),
    (r"список ип и юл.*реабилитацион", "risk_bankruptcy_proc_terminated", "yesno"),
    (r"^налоговая задолженность$", "tax_debt_total", "amount"),
    (r"задолженность по таможенным", "tax_debt_customs", "amount"),
    (r"задолженность по отчислениям.*мед", "tax_debt_vosms", "amount"),
    (r"задолженность по социальным отчислениям", "tax_debt_so", "amount"),
    (r"задолженность по пенсионным", "tax_debt_opv", "amount"),
]

YEAR_ROW_KEYWORDS = [
    (r"средняя численность", "avg_headcount", "amount"),
    (r"налоговые поступления", "tax_revenue", "amount"),
    (r"сумма возврата ндс", "vat_refund", "amount"),
    (r"кнн налогоплательщика", "knn_taxpayer", "amount"),
    (r"среднеотраслевое значение кнн", "knn_industry", "amount"),
]


@dataclass
class TaxpayerProfile:
    bin_iin: str | None = None
    name: str | None = None
    date_registration = None
    oked: str | None = None
    oked_name: str | None = None
    tax_regime: str | None = None
    vat_payer: bool | None = None
    vat_reg_date = None
    fields: dict = field(default_factory=dict)          # прочие скалярные поля (по LABEL_KEYWORDS)
    yearly: dict[int, dict] = field(default_factory=dict)  # год -> {avg_headcount, tax_revenue, ...}
    warnings: list[str] = field(default_factory=list)
    source_file: str | None = None


def _norm_label(s: str) -> str:
    s = (s or "").replace("\n", " ").lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = s.replace("ё", "е")
    return s


def _convert(value: str, kind: str):
    if kind == "amount":
        return dn.normalize_amount(value)
    if kind == "date":
        return dn.normalize_date(value)
    if kind == "yesno":
        return dn.normalize_yesno(value)
    return dn.normalize_text(value)


def parse_sur_pdf(file_bytes: bytes, filename: str = "") -> TaxpayerProfile:
    if pdfplumber is None:
        raise RuntimeError("pdfplumber не установлен")

    profile = TaxpayerProfile(source_file=filename)
    years_header: list[int] = []

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        all_rows: list[list] = []
        for page in pdf.pages:
            for t in page.extract_tables() or []:
                all_rows.extend(t)

    for row in all_rows:
        row = [c if c is not None else "" for c in row]
        if len(row) < 2:
            continue

        # строка-шапка с годами: первая-вторая ячейка пустые/нечисловые,
        # а остальные — 4-значные годы
        year_candidates = [c.strip() for c in row if re.match(r"^(19|20)\d{2}$", c.strip())]
        if len(year_candidates) >= 2 and not row[0].strip().isdigit():
            years_header = [int(y) for y in year_candidates]
            for y in years_header:
                profile.yearly.setdefault(y, {})
            continue

        label_raw = row[1] if row[0].strip().isdigit() or row[0].strip() == "" else row[0]
        label = _norm_label(label_raw)
        values = row[2:] if (row[0].strip().isdigit() or row[0].strip() == "") else row[1:]
        if not label:
            continue

        matched = False
        # построчные годовые показатели
        if years_header:
            for pattern, key, kind in YEAR_ROW_KEYWORDS:
                if re.search(pattern, label):
                    vals = [v for v in values if v != ""]
                    for y, v in zip(years_header, vals):
                        profile.yearly.setdefault(y, {})[key] = _convert(v, kind)
                    matched = True
                    break
        if matched:
            continue

        # обычные скалярные поля
        for pattern, key, kind in LABEL_KEYWORDS:
            if re.search(pattern, label):
                raw_val = " ".join(v for v in values if v).strip()
                val = _convert(raw_val, kind)
                profile.fields[key] = val
                matched = True
                break

    profile.bin_iin = profile.fields.get("bin_iin")
    profile.name = profile.fields.get("name")
    profile.date_registration = profile.fields.get("date_registration")
    profile.oked = profile.fields.get("oked")
    profile.oked_name = profile.fields.get("oked_name")
    profile.tax_regime = profile.fields.get("tax_regime")
    profile.vat_payer = profile.fields.get("vat_payer")
    profile.vat_reg_date = profile.fields.get("vat_reg_date")

    if not profile.bin_iin:
        profile.warnings.append("Не удалось распознать БИН/ИИН из PDF — заполните вручную.")
    if not profile.name:
        profile.warnings.append("Не удалось распознать наименование НП из PDF — заполните вручную.")

    return profile


def parse_taxpayer_excel(tables: list, filename: str = "") -> TaxpayerProfile:
    """
    Fallback-парсер для карточки НП в формате Excel (двухколоночный
    список 'поле / значение' либо таблица-строка с заголовками
    в стиле ALIASES).
    """
    profile = TaxpayerProfile(source_file=filename)

    for table in tables:
        df = table.dataframe
        if df.empty:
            continue

        if df.shape[1] == 2:
            for _, row in df.iterrows():
                label = _norm_label(str(row.iloc[0]))
                value = row.iloc[1]
                for pattern, key, kind in LABEL_KEYWORDS:
                    if re.search(pattern, label):
                        profile.fields[key] = _convert(value, kind)
                        break
            continue

        mapped_df, _ = cm.map_dataframe_columns(df)
        first = mapped_df.iloc[0] if not mapped_df.empty else None
        if first is None:
            continue
        for canon in ("bin_iin", "name", "oked", "oked_name", "tax_regime", "vat_payer",
                      "date_registration", "vat_reg_date", "avg_headcount", "tax_revenue",
                      "knn_taxpayer", "knn_industry", "tax_debt"):
            if canon in mapped_df.columns:
                profile.fields[canon] = first.get(canon)

    profile.bin_iin = dn.normalize_bin(profile.fields.get("bin_iin"))
    profile.name = dn.normalize_text(profile.fields.get("name"))
    profile.oked = dn.normalize_text(profile.fields.get("oked"))
    profile.tax_regime = dn.normalize_text(profile.fields.get("tax_regime"))
    profile.vat_payer = dn.normalize_yesno(profile.fields.get("vat_payer"))

    if not profile.bin_iin:
        profile.warnings.append("Не удалось распознать БИН/ИИН из Excel-карточки — заполните вручную.")
    return profile
