"""
analytics.py
Аналитические расчёты по собранной базе налогоплательщика: динамика по годам,
покупатели/поставщики по годам, сверка ЭСФ с ФНО (камеральный контроль).

Все функции терпимы к отсутствию части данных: если каких-то файлов не
загружено, функция возвращает пустой результат с понятным сообщением
"Недостаточно данных для...", а не падает с ошибкой.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

MATERIALITY_ABS = 100_000       # порог существенности в тенге для расхождений
MATERIALITY_PCT = 0.05          # 5% — порог существенности в долях
CONCENTRATION_HIGH = 0.5        # доля контрагента >50% оборота — высокая концентрация
CONCENTRATION_MEDIUM = 0.3      # доля контрагента >30% — средняя концентрация

GOV_KEYWORDS = [
    "гу ", "гу\"", "ГУ ", "коммунальное государственное", "кгу", "кгп", "ргп",
    "акимат", "министерств", "маслихат", "прокуратур", "департамент",
    "управление образования", "отдел ", "государственное учреждение",
]
QUASI_GOV_KEYWORDS = ["нац.", "национальная компания", "самрук", "kaztransoil", "казахтелеком"]


def _classify_sector(name: str | None) -> str:
    if not name:
        return "не определено"
    low = name.lower()
    if any(k.lower() in low for k in QUASI_GOV_KEYWORDS):
        return "квазигоссектор"
    if any(k.lower() in low for k in GOV_KEYWORDS):
        return "государственный орган"
    return "частный бизнес"


def combine_esf(results: list) -> pd.DataFrame:
    """Объединяет несколько EsfParseResult в один DataFrame."""
    frames = [r.dataframe for r in results if r is not None and r.row_count > 0]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def yearly_dynamics(esf_sales: pd.DataFrame, esf_purchases: pd.DataFrame,
                     tax_revenue_by_year: dict[int, float] | None = None) -> pd.DataFrame:
    """Таблица 'Динамика по годам': реализация/приобретение без и с НДС, налоговая нагрузка."""
    tax_revenue_by_year = tax_revenue_by_year or {}
    years = set()
    if not esf_sales.empty:
        years |= set(esf_sales["year"].dropna().unique().tolist())
    if not esf_purchases.empty:
        years |= set(esf_purchases["year"].dropna().unique().tolist())
    years |= set(tax_revenue_by_year.keys())
    rows = []
    for y in sorted(years):
        s = esf_sales[esf_sales["year"] == y] if not esf_sales.empty else pd.DataFrame()
        p = esf_purchases[esf_purchases["year"] == y] if not esf_purchases.empty else pd.DataFrame()
        sales_no_vat = float(s["amount_no_vat"].fillna(0).sum()) if not s.empty else 0.0
        sales_vat = float(s["vat_amount"].fillna(0).sum()) if not s.empty else 0.0
        purch_no_vat = float(p["amount_no_vat"].fillna(0).sum()) if not p.empty else 0.0
        purch_vat = float(p["vat_amount"].fillna(0).sum()) if not p.empty else 0.0
        revenue = tax_revenue_by_year.get(int(y))
        burden = round(revenue / sales_no_vat * 100, 2) if revenue and sales_no_vat else None
        rows.append({
            "year": int(y),
            "sales_no_vat": round(sales_no_vat, 2),
            "sales_vat": round(sales_vat, 2),
            "sales_with_vat": round(sales_no_vat + sales_vat, 2),
            "purchases_no_vat": round(purch_no_vat, 2),
            "purchases_vat": round(purch_vat, 2),
            "purchases_with_vat": round(purch_no_vat + purch_vat, 2),
            "diff_sales_purchases": round(sales_no_vat - purch_no_vat, 2),
            "tax_revenue": revenue,
            "tax_burden_pct": burden,
        })
    return pd.DataFrame(rows)


def counterparty_summary(esf_df: pd.DataFrame, bin_col: str, name_col: str) -> pd.DataFrame:
    """
    Общая функция для построения свода по контрагентам (покупатели или поставщики)
    по годам: количество ЭСФ, суммы, доля в обороте года, признак повторяемости.
    """
    if esf_df.empty or bin_col not in esf_df.columns:
        return pd.DataFrame()

    df = esf_df.dropna(subset=[bin_col]).copy()
    if df.empty:
        return pd.DataFrame()

    grouped = df.groupby([bin_col, "year"], dropna=False).agg(
        name=(name_col, lambda x: x.dropna().mode().iat[0] if not x.dropna().empty else None),
        esf_count=("esf_number", "nunique"),
        amount_no_vat=("amount_no_vat", "sum"),
        vat_amount=("vat_amount", "sum"),
    ).reset_index()
    grouped["amount_with_vat"] = grouped["amount_no_vat"] + grouped["vat_amount"]

    year_totals = grouped.groupby("year")["amount_no_vat"].transform("sum")
    grouped["share_pct"] = (grouped["amount_no_vat"] / year_totals.replace(0, pd.NA) * 100).round(2)

    years_by_bin = grouped.groupby(bin_col)["year"].apply(lambda x: sorted(set(int(y) for y in x)))
    grouped["years_active"] = grouped[bin_col].map(years_by_bin)
    grouped["is_recurring"] = grouped["years_active"].apply(lambda ys: len(ys) > 1)
    grouped["sector"] = grouped["name"].apply(_classify_sector)
    grouped["concentration_flag"] = grouped["share_pct"].apply(
        lambda p: "высокая" if pd.notna(p) and p >= CONCENTRATION_HIGH * 100
        else ("средняя" if pd.notna(p) and p >= CONCENTRATION_MEDIUM * 100 else "низкая")
    )
    grouped = grouped.rename(columns={bin_col: "bin"})
    return grouped.sort_values(["year", "amount_no_vat"], ascending=[True, False]).reset_index(drop=True)


def buyers_by_year(esf_sales: pd.DataFrame) -> pd.DataFrame:
    return counterparty_summary(esf_sales, "buyer_bin", "buyer_name")


def suppliers_by_year(esf_purchases: pd.DataFrame) -> pd.DataFrame:
    df = counterparty_summary(esf_purchases, "supplier_bin", "supplier_name")
    if df.empty or esf_purchases.empty:
        return df
    # доля сделок без НДС по поставщику (по ставке 0/б.НДС во всех его строках)
    has_vat = esf_purchases.groupby("supplier_bin")["vat_amount"].sum()
    df["has_vat_amounts"] = df["bin"].map(lambda b: bool(has_vat.get(b, 0) > 0))
    return df


def top_n(df: pd.DataFrame, n: int = 10, by_year: int | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    d = df[df["year"] == by_year] if by_year else df
    return d.sort_values("amount_no_vat", ascending=False).head(n)


def vat_reconciliation(esf_sales: pd.DataFrame, esf_purchases: pd.DataFrame,
                        fno_300: pd.DataFrame | None) -> pd.DataFrame:
    """
    Камеральный контроль по НДС: сопоставление оборотов/НДС по ЭСФ с ФНО 300.00
    по кварталам. Если ФНО 300.00 не загружена или не сопоставлены суммовые
    поля (см. fno_parser/config/fno_field_map.json) — возвращаются только
    показатели по ЭСФ с пометкой "нет данных по ФНО".
    """
    periods = set()
    if not esf_sales.empty:
        periods |= set(zip(esf_sales["year"].dropna(), esf_sales["quarter"].dropna()))
    if not esf_purchases.empty:
        periods |= set(zip(esf_purchases["year"].dropna(), esf_purchases["quarter"].dropna()))

    fno_has_amounts = fno_300 is not None and not fno_300.empty and fno_300["vat_charged"].notna().any()

    rows = []
    for y, q in sorted(periods, key=lambda t: (t[0], t[1])):
        s = esf_sales[(esf_sales["year"] == y) & (esf_sales["quarter"] == q)] if not esf_sales.empty else pd.DataFrame()
        p = esf_purchases[(esf_purchases["year"] == y) & (esf_purchases["quarter"] == q)] if not esf_purchases.empty else pd.DataFrame()
        esf_turnover = float(s["turnover_amount"].fillna(s["amount_no_vat"]).sum()) if not s.empty else 0.0
        esf_vat_charged = float(s["vat_amount"].fillna(0).sum()) if not s.empty else 0.0
        esf_purchase_turnover = float(p["amount_no_vat"].fillna(0).sum()) if not p.empty else 0.0
        esf_vat_credit = float(p["vat_amount"].fillna(0).sum()) if not p.empty else 0.0

        row = {
            "year": int(y), "quarter": int(q),
            "esf_turnover_sales": round(esf_turnover, 2),
            "esf_vat_charged": round(esf_vat_charged, 2),
            "esf_turnover_purchases": round(esf_purchase_turnover, 2),
            "esf_vat_credit": round(esf_vat_credit, 2),
        }

        if fno_has_amounts:
            fq = fno_300[(fno_300["year"] == y) & (fno_300["quarter"] == q)]
            fno_vat_charged = float(fq["vat_charged"].dropna().sum()) if not fq.empty else None
            fno_vat_credit = float(fq["vat_credit"].dropna().sum()) if not fq.empty else None
            row["fno_vat_charged"] = fno_vat_charged
            row["fno_vat_credit"] = fno_vat_credit
            row["diff_vat_charged"] = (
                round(esf_vat_charged - fno_vat_charged, 2) if fno_vat_charged is not None else None
            )
            row["diff_vat_credit"] = (
                round(esf_vat_credit - fno_vat_credit, 2) if fno_vat_credit is not None else None
            )
        else:
            row["fno_vat_charged"] = None
            row["fno_vat_credit"] = None
            row["diff_vat_charged"] = None
            row["diff_vat_credit"] = None
            row["note"] = "Нет сопоставленных сумм по ФНО 300.00 — сравнение недоступно, показаны только данные ЭСФ"

        rows.append(row)
    return pd.DataFrame(rows)


def income_reconciliation(esf_sales: pd.DataFrame, fno_100: pd.DataFrame | None) -> pd.DataFrame:
    """Сопоставление годовой реализации по ЭСФ с доходом по ФНО 100.00."""
    if esf_sales.empty:
        return pd.DataFrame()
    years = sorted(esf_sales["year"].dropna().unique().tolist())
    fno_has_amounts = fno_100 is not None and not fno_100.empty and fno_100["income"].notna().any()

    rows = []
    for y in years:
        s = esf_sales[esf_sales["year"] == y]
        esf_income = float(s["amount_no_vat"].fillna(0).sum())
        row = {"year": int(y), "esf_sales_amount": round(esf_income, 2)}
        if fno_has_amounts:
            fy = fno_100[fno_100["year"] == y]
            fno_income = float(fy["income"].dropna().sum()) if not fy.empty else None
            row["fno_income"] = fno_income
            row["diff"] = round(esf_income - fno_income, 2) if fno_income is not None else None
        else:
            row["fno_income"] = None
            row["diff"] = None
            row["note"] = "Нет сопоставленных сумм по ФНО 100.00 — сравнение недоступно, показана только реализация по ЭСФ"
        rows.append(row)
    return pd.DataFrame(rows)


def payroll_check(yearly_profile: dict[int, dict], fno_200: pd.DataFrame | None) -> pd.DataFrame:
    """
    Сопоставление сведений о численности (карточка НП) с фактом подачи ФНО
    200.00/200.01. Суммовые показатели зарплатных налогов доступны только
    при заполненной карте полей (config/fno_field_map.json) — иначе
    сравнивается только сам факт подачи формы по кварталам.
    """
    rows = []
    fno_years = set()
    if fno_200 is not None and not fno_200.empty:
        fno_years = set(int(y) for y in fno_200["year"].dropna().unique())

    all_years = sorted(set(yearly_profile.keys()) | fno_years)
    for y in all_years:
        headcount = yearly_profile.get(y, {}).get("avg_headcount")
        forms_filed = int((fno_200["year"] == y).sum()) if fno_200 is not None and not fno_200.empty else 0
        rows.append({
            "year": y,
            "avg_headcount": headcount,
            "fno_200_forms_filed": forms_filed,
            "note": (
                "Численность >0, но формы 200.00 за год не найдены — требуется проверка"
                if (headcount or 0) > 0 and forms_filed == 0
                else ("Численность указана как 0, но форма 200.00 подавалась — уточнить основания"
                      if (headcount or 0) == 0 and forms_filed > 0 else "")
            ),
        })
    return pd.DataFrame(rows)


def documents_to_request(risk_findings: list[dict]) -> list[dict]:
    """
    Формирует перечень документов на основе выявленных рисков —
    типовой набор, привязанный к типу риска.
    """
    doc_map = {
        "концентрация_поставщика": ["договор", "акт выполненных работ/накладная", "СНТ", "платёжное поручение"],
        "разовый_крупный_поставщик": ["договор", "акт/накладная", "СНТ", "подтверждение фактического выполнения работ"],
        "бестоварность": ["договор", "акт/накладная", "ЭАВР", "транспортные документы", "переписка", "доверенности"],
        "снт_отсутствует": ["СНТ", "накладную", "транспортные документы"],
        "расхождение_ндс": ["декларацию по НДС с приложениями", "реестр счетов-фактур", "пояснение по периоду отнесения в зачёт"],
        "расхождение_доход": ["регистры бухгалтерского учёта дохода", "договоры", "акты выполненных работ"],
        "численность_риск": ["табели учёта рабочего времени", "трудовые договоры/договоры ГПХ", "ведомости по зарплате"],
    }
    docs = []
    seen = set()
    for f in risk_findings:
        rtype = f.get("risk_key")
        for d in doc_map.get(rtype, []):
            key = (f.get("counterparty_bin") or f.get("period"), d)
            if key in seen:
                continue
            seen.add(key)
            docs.append({
                "counterparty_or_period": f.get("counterparty_name") or f.get("period"),
                "document": d,
                "reason": f.get("risk_type"),
            })
    return docs
