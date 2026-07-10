"""
risk_engine.py
Правило-ориентированный риск-движок. Формирует записи risk_findings на основе
уже посчитанной аналитики (analytics.py).

Ключевое правило модуля (см. ТЗ п.9 "Важные правила анализа"):
приложение НЕ делает категоричных обвинительных выводов. Все формулировки
идут через шаблоны _finding(), которые используют только осторожные
конструкции: "имеется риск", "требуется дополнительная проверка",
"по имеющимся данным возможно расхождение", "необходимо запросить документы".
Каждая запись явно указывает период, сумму и источник (файл/строки),
на основании которых она сформирована — это отделяет факт/расчёт от риска.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .analytics import MATERIALITY_ABS, MATERIALITY_PCT, CONCENTRATION_HIGH, CONCENTRATION_MEDIUM

RISK_HIGH = "высокий"
RISK_MEDIUM = "средний"
RISK_LOW = "низкий"

VAT_DIFF_CAUSES = [
    "не все ЭСФ включены в декларацию за период",
    "ЭСФ выписаны после даты оборота и могли попасть в другой отчётный период",
    "имеются дополнительные/исправленные ЭСФ, влияющие на сумму НДС",
    "НДС по приобретению взят в зачёт не в том квартале",
    "поставщик не является плательщиком НДС на дату сделки",
    "ЭСФ отозвана/аннулирована после отражения в учёте",
    "оборот отражён в декларации другого периода",
]

INCOME_DIFF_CAUSES_MORE_ESF = [
    "часть реализации по ЭСФ могла быть отражена в другом отчётном периоде",
    "возможна корректировка/исправление ЭСФ после сдачи декларации",
]
INCOME_DIFF_CAUSES_MORE_FNO = [
    "возможны доходы, не оформленные через ЭСФ (реализация физическим лицам, прочие доходы)",
    "возможна разница в дате признания дохода в бухгалтерском/налоговом учёте",
]


def _finding(risk_key: str, risk_type: str, period: str, amount: float | None,
             source: str, description: str, level: str, what_to_check: str,
             consequence: str, counterparty_bin: str | None = None,
             counterparty_name: str | None = None, source_ref: str | None = None) -> dict:
    return {
        "risk_key": risk_key,
        "risk_type": risk_type,
        "period": period,
        "amount": round(amount, 2) if amount is not None else None,
        "source": source,
        "description": description,
        "level": level,
        "what_to_check": what_to_check,
        "possible_consequence": consequence,
        "counterparty_bin": counterparty_bin,
        "counterparty_name": counterparty_name,
        "source_ref": source_ref,
    }


def check_concentration(buyers_or_suppliers: pd.DataFrame, role: str) -> list[dict]:
    """role: 'покупателя' или 'поставщика'"""
    findings = []
    if buyers_or_suppliers.empty:
        return findings
    for _, row in buyers_or_suppliers.iterrows():
        share = row.get("share_pct")
        if pd.isna(share):
            continue
        if share >= CONCENTRATION_HIGH * 100:
            level = RISK_HIGH
        elif share >= CONCENTRATION_MEDIUM * 100:
            level = RISK_MEDIUM
        else:
            continue
        findings.append(_finding(
            risk_key=f"концентрация_{'покупателя' if role=='покупателя' else 'поставщика'}",
            risk_type=f"Концентрация оборота на одном {role}",
            period=str(int(row["year"])),
            amount=row.get("amount_no_vat"),
            source=f"ЭСФ {'реализация' if role=='покупателя' else 'приобретение'} за {int(row['year'])}",
            description=(
                f"По данным ЭСФ за {int(row['year'])} год доля {role} "
                f"{row.get('name') or row.get('bin')} (БИН {row.get('bin')}) составляет "
                f"{share:.1f}% от общего оборота ({role.replace('еля','ения')}) за год "
                f"на сумму {row.get('amount_no_vat'):,.0f} тенге без НДС."
            ),
            level=level,
            what_to_check=f"Запросить договор, акты/накладные, СНТ и подтверждение фактического исполнения по сделкам с данным контрагентом.",
            consequence="При подтверждении признаков нетоварности операций возможно исключение расходов из вычетов и/или сумм НДС из зачёта.",
            counterparty_bin=row.get("bin"),
            counterparty_name=row.get("name"),
            source_ref=f"esf_{'sales' if role=='покупателя' else 'purchases'}:{row.get('bin')}:{int(row['year'])}",
        ))
    return findings


def check_one_year_suppliers(suppliers: pd.DataFrame, materiality: float = 5_000_000) -> list[dict]:
    findings = []
    if suppliers.empty:
        return findings
    for _, row in suppliers.iterrows():
        years_active = row.get("years_active") or []
        if len(years_active) == 1 and row.get("amount_no_vat", 0) >= materiality:
            findings.append(_finding(
                risk_key="разовый_крупный_поставщик",
                risk_type="Поставщик, работавший только один год, с крупной суммой сделок",
                period=str(int(row["year"])),
                amount=row.get("amount_no_vat"),
                source=f"ЭСФ приобретение за {int(row['year'])}",
                description=(
                    f"Поставщик {row.get('name') or row.get('bin')} (БИН {row.get('bin')}) "
                    f"встречается по данным ЭСФ только в {int(row['year'])} году, сумма приобретений "
                    f"составляет {row.get('amount_no_vat'):,.0f} тенге без НДС."
                ),
                level=RISK_MEDIUM,
                what_to_check="Запросить договор, акты/накладные, СНТ, подтверждение фактического выполнения работ, транспортные документы.",
                consequence="При отсутствии подтверждающих документов возможен риск непризнания вычетов по КПН и НДС в зачёт.",
                counterparty_bin=row.get("bin"),
                counterparty_name=row.get("name"),
                source_ref=f"esf_purchases:{row.get('bin')}:{int(row['year'])}",
            ))
    return findings


def check_headcount_vs_turnover(yearly_profile: dict[int, dict], purchases_by_year: pd.DataFrame) -> list[dict]:
    findings = []
    if purchases_by_year.empty:
        return findings
    turnover_by_year = purchases_by_year.groupby("year")["amount_no_vat"].sum()
    for year, headcount_data in yearly_profile.items():
        headcount = headcount_data.get("avg_headcount")
        turnover = turnover_by_year.get(year)
        if turnover is None or pd.isna(turnover) or turnover == 0:
            continue
        if headcount is not None and headcount <= 2 and turnover >= 50_000_000:
            findings.append(_finding(
                risk_key="численность_риск",
                risk_type="Малая численность персонала при значительном объёме приобретений",
                period=str(year),
                amount=float(turnover),
                source=f"Карточка НП (численность за {year}) + ЭСФ приобретение за {year}",
                description=(
                    f"За {year} год средняя численность работников — {headcount:.0f} чел., "
                    f"при этом объём приобретений по ЭСФ составил {turnover:,.0f} тенге без НДС."
                ),
                level=RISK_MEDIUM,
                what_to_check="Проверить наличие договоров субподряда/аренды техники, ГПХ-договоров, привлечённого персонала.",
                consequence="Требуется дополнительная проверка соответствия объёмов работ имеющимся ресурсам НП.",
                source_ref=f"taxpayer_profile:{year};esf_purchases:{year}",
            ))
    return findings


def check_esf_issue_delay(esf_df: pd.DataFrame, direction: str, delay_days: int = 90) -> list[dict]:
    """Флагирует ЭСФ, оформленные значительно позже даты оборота (риск позднего отражения)."""
    findings = []
    if esf_df.empty:
        return findings
    df = esf_df.dropna(subset=["esf_issue_date", "esf_turnover_date"]).copy()
    if df.empty:
        return findings
    df["delay"] = (pd.to_datetime(df["esf_issue_date"]) - pd.to_datetime(df["esf_turnover_date"])).dt.days
    late = df[df["delay"] > delay_days]
    if late.empty:
        return findings
    grouped = late.groupby("year").agg(count=("esf_number", "nunique"), amount=("amount_no_vat", "sum"))
    for year, row in grouped.iterrows():
        findings.append(_finding(
            risk_key="поздняя_эсф",
            risk_type=f"ЭСФ ({'приобретение' if direction=='purchase' else 'реализация'}) оформлены значительно позже даты оборота",
            period=str(int(year)),
            amount=float(row["amount"]),
            source=f"ЭСФ {'приобретение' if direction=='purchase' else 'реализация'} за {int(year)}",
            description=(
                f"За {int(year)} год выявлено {int(row['count'])} ЭСФ, оформленных более чем через "
                f"{delay_days} дней после даты оборота, на сумму {row['amount']:,.0f} тенге без НДС."
            ),
            level=RISK_LOW,
            what_to_check="Уточнить период отнесения оборота/НДС в зачёт по данным ЭСФ, сверить с отражением в декларации соответствующего периода.",
            consequence="Возможно неверное отнесение оборота/НДС к отчётному периоду.",
            source_ref=f"esf_{'purchases' if direction=='purchase' else 'sales'}:{int(year)}:late_issue",
        ))
    return findings


def check_duplicates(esf_df: pd.DataFrame, direction: str) -> list[dict]:
    findings = []
    if esf_df.empty or "is_potential_duplicate" not in esf_df.columns:
        return findings
    dup = esf_df[esf_df["is_potential_duplicate"]]
    if dup.empty:
        return findings
    grouped = dup.groupby("year").agg(count=("esf_number", "count"), amount=("amount_no_vat", "sum"))
    for year, row in grouped.iterrows():
        findings.append(_finding(
            risk_key="дубли_эсф",
            risk_type="Потенциальные дубли строк ЭСФ",
            period=str(int(year)),
            amount=float(row["amount"]),
            source=f"ЭСФ {'приобретение' if direction=='purchase' else 'реализация'} за {int(year)}",
            description=(
                f"За {int(year)} год обнаружено {int(row['count'])} строк-кандидатов на дублирование "
                f"(совпадение номера ЭСФ, наименования и суммы) на сумму {row['amount']:,.0f} тенге без НДС."
            ),
            level=RISK_LOW,
            what_to_check="Проверить, не задвоён ли учёт данных операций (возможно, корректно — несколько идентичных позиций в одной поставке).",
            consequence="При подтверждении задвоения — риск завышения приобретений/вычетов и НДС в зачёт.",
            source_ref=f"esf_{'purchases' if direction=='purchase' else 'sales'}:{int(year)}:duplicates",
        ))
    return findings


def check_missing_snt(esf_purchases: pd.DataFrame) -> list[dict]:
    """Товарные позиции (есть код ТН ВЭД) без номера СНТ — требует проверки."""
    findings = []
    if esf_purchases.empty or "tnved_code" not in esf_purchases.columns:
        return findings
    goods = esf_purchases[esf_purchases["tnved_code"].notna() & (esf_purchases["tnved_code"] != "")]
    if goods.empty:
        return findings
    missing = goods[goods["snt_number"].isna() | (goods["snt_number"] == "")]
    if missing.empty:
        return findings
    grouped = missing.groupby("year").agg(count=("esf_number", "nunique"), amount=("amount_no_vat", "sum"))
    for year, row in grouped.iterrows():
        if row["amount"] < MATERIALITY_ABS:
            continue
        findings.append(_finding(
            risk_key="снт_отсутствует",
            risk_type="Товарные позиции без номера СНТ",
            period=str(int(year)),
            amount=float(row["amount"]),
            source=f"ЭСФ приобретение за {int(year)}",
            description=(
                f"За {int(year)} год по {int(row['count'])} ЭСФ с указанным кодом ТН ВЭД (товар) "
                f"не указан номер СНТ, сумма — {row['amount']:,.0f} тенге без НДС."
            ),
            level=RISK_LOW,
            what_to_check="Проверить обязательность оформления СНТ по данным позициям и наличие СНТ у поставщика.",
            consequence="Отсутствие обязательного СНТ может быть признаком риска в отношении товарности операции.",
            source_ref=f"esf_purchases:{int(year)}:missing_snt",
        ))
    return findings


def check_vat_reconciliation(vat_rec_df: pd.DataFrame) -> list[dict]:
    findings = []
    if vat_rec_df.empty:
        return findings
    for _, row in vat_rec_df.iterrows():
        for label, diff_col, esf_col, fno_col in [
            ("НДС начисленный", "diff_vat_charged", "esf_vat_charged", "fno_vat_charged"),
            ("НДС в зачёт", "diff_vat_credit", "esf_vat_credit", "fno_vat_credit"),
        ]:
            diff = row.get(diff_col)
            if diff is None or pd.isna(diff):
                continue
            fno_val = row.get(fno_col) or 0
            pct = abs(diff) / fno_val if fno_val else None
            material = abs(diff) >= MATERIALITY_ABS and (pct is None or pct >= MATERIALITY_PCT)
            if not material:
                continue
            level = RISK_HIGH if abs(diff) > 10 * MATERIALITY_ABS else RISK_MEDIUM
            causes = "; ".join(VAT_DIFF_CAUSES[:4])
            findings.append(_finding(
                risk_key="расхождение_ндс",
                risk_type=f"Расхождение ЭСФ и ФНО 300.00 — {label}",
                period=f"{int(row['year'])} Q{int(row['quarter'])}",
                amount=float(diff),
                source="ЭСФ vs ФНО 300.00",
                description=(
                    f"За {int(row['year'])} год, {int(row['quarter'])} квартал: по данным ЭСФ {label.lower()} "
                    f"составляет {row.get(esf_col):,.0f} тенге, по ФНО 300.00 — {fno_val:,.0f} тенге. "
                    f"Расхождение — {diff:,.0f} тенге."
                ),
                level=level,
                what_to_check=f"Сверить декларацию по НДС с реестром ЭСФ за период; возможные причины: {causes}.",
                consequence="При подтверждении расхождения возможно доначисление НДС и/или пени.",
                source_ref=f"vat_reconciliation:{int(row['year'])}:{int(row['quarter'])}",
            ))
    return findings


def check_income_reconciliation(income_rec_df: pd.DataFrame) -> list[dict]:
    findings = []
    if income_rec_df.empty:
        return findings
    for _, row in income_rec_df.iterrows():
        diff = row.get("diff")
        if diff is None or pd.isna(diff):
            continue
        fno_income = row.get("fno_income") or 0
        pct = abs(diff) / fno_income if fno_income else None
        material = abs(diff) >= MATERIALITY_ABS and (pct is None or pct >= MATERIALITY_PCT)
        if not material:
            continue
        if diff > 0:
            desc_extra = "Реализация по ЭСФ больше дохода, отражённого в ФНО 100.00 — по имеющимся данным возможно занижение дохода."
            causes = "; ".join(INCOME_DIFF_CAUSES_MORE_ESF)
        else:
            desc_extra = "Доход в ФНО 100.00 больше реализации по ЭСФ — возможны доходы, не оформленные через ЭСФ."
            causes = "; ".join(INCOME_DIFF_CAUSES_MORE_FNO)
        level = RISK_HIGH if abs(diff) > 10 * MATERIALITY_ABS else RISK_MEDIUM
        findings.append(_finding(
            risk_key="расхождение_доход",
            risk_type="Расхождение реализации по ЭСФ и дохода по ФНО 100.00",
            period=str(int(row["year"])),
            amount=float(diff),
            source="ЭСФ реализация vs ФНО 100.00",
            description=(
                f"За {int(row['year'])} год реализация по ЭСФ — {row.get('esf_sales_amount'):,.0f} тенге, "
                f"доход по ФНО 100.00 — {fno_income:,.0f} тенге. Разница — {diff:,.0f} тенге. {desc_extra}"
            ),
            level=level,
            what_to_check=f"Сверить учёт дохода с реестром реализации по ЭСФ; возможные причины: {causes}.",
            consequence="При подтверждении расхождения возможно доначисление КПН и связанных налогов.",
            source_ref=f"income_reconciliation:{int(row['year'])}",
        ))
    return findings


def check_payroll(payroll_df: pd.DataFrame) -> list[dict]:
    findings = []
    if payroll_df.empty:
        return findings
    for _, row in payroll_df.iterrows():
        note = row.get("note")
        if not note:
            continue
        findings.append(_finding(
            risk_key="численность_риск",
            risk_type="Несоответствие численности и факта подачи ФНО 200.00",
            period=str(int(row["year"])),
            amount=None,
            source=f"Карточка НП + реестр ФНО 200.00 за {int(row['year'])}",
            description=f"За {int(row['year'])} год: {note} (численность: {row.get('avg_headcount')}, форм 200.00 подано: {row.get('fno_200_forms_filed')}).",
            level=RISK_LOW,
            what_to_check="Проверить наличие трудовых договоров/договоров ГПХ и фактическую занятость персонала за период.",
            consequence="Возможен риск неотражения работников либо использования труда по договорам ГПХ без надлежащего оформления.",
            source_ref=f"payroll_check:{int(row['year'])}",
        ))
    return findings


def run_all_checks(*, buyers: pd.DataFrame, suppliers: pd.DataFrame,
                    esf_sales: pd.DataFrame, esf_purchases: pd.DataFrame,
                    yearly_profile: dict, vat_rec: pd.DataFrame,
                    income_rec: pd.DataFrame, payroll: pd.DataFrame) -> list[dict]:
    findings: list[dict] = []
    findings += check_concentration(buyers, "покупателя")
    findings += check_concentration(suppliers, "поставщика")
    findings += check_one_year_suppliers(suppliers)
    findings += check_headcount_vs_turnover(yearly_profile, esf_purchases)
    findings += check_esf_issue_delay(esf_purchases, "purchase")
    findings += check_esf_issue_delay(esf_sales, "sale")
    findings += check_duplicates(esf_purchases, "purchase")
    findings += check_duplicates(esf_sales, "sale")
    findings += check_missing_snt(esf_purchases)
    findings += check_vat_reconciliation(vat_rec)
    findings += check_income_reconciliation(income_rec)
    findings += check_payroll(payroll)
    return findings


def group_by_level(findings: list[dict]) -> dict[str, list[dict]]:
    out = {RISK_HIGH: [], RISK_MEDIUM: [], RISK_LOW: []}
    for f in findings:
        out.setdefault(f["level"], []).append(f)
    return out
