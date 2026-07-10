"""
db.py
Персистентное хранение собранной базы налогоплательщика в DuckDB
(data/database.duckdb) — служит для аудита/истории проверки и для
возможности переоткрыть последний расчёт без повторной загрузки файлов.

Внутри Streamlit-сессии основным рабочим хранилищем остаются
pandas DataFrame в st.session_state (это быстрее для интерактивного UI);
DuckDB используется как побочный, надёжный слой персистентности —
таблицы записываются при каждом пересчёте аналитики.
"""

from __future__ import annotations

import json
import os

import pandas as pd

try:
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_DIR = os.path.join(DATA_DIR, "db")


def _connect(user_id: int | str = "shared"):
    """
    Каждый пользователь получает СВОЙ файл DuckDB (data/db/user_<id>.duckdb) —
    это самый простой и надёжный способ гарантировать, что данные одного
    пользователя физически не пересекаются с данными другого (требование
    изоляции данных, см. ТЗ п.4/п.13).
    """
    if duckdb is None:
        return None
    os.makedirs(DB_DIR, exist_ok=True)
    path = os.path.join(DB_DIR, f"user_{user_id}.duckdb")
    return duckdb.connect(path)


def save_snapshot(*, user_id: int | str, taxpayer_profile, esf_purchases: pd.DataFrame, esf_sales: pd.DataFrame,
                   buyers: pd.DataFrame, suppliers: pd.DataFrame, findings: list[dict],
                   fno_results: dict) -> bool:
    """Сохраняет текущий срез собранной базы конкретного пользователя в его личный DuckDB-файл.
    Не бросает исключений наружу — сбой персистентности не должен ронять интерактивный анализ в Streamlit."""
    con = _connect(user_id)
    if con is None:
        return False
    try:
        if taxpayer_profile is not None:
            profile_df = pd.DataFrame([{
                "bin_iin": taxpayer_profile.bin_iin,
                "name": taxpayer_profile.name,
                "oked": taxpayer_profile.oked,
                "tax_regime": taxpayer_profile.tax_regime,
                "vat_payer": taxpayer_profile.vat_payer,
                "fields_json": json.dumps(_jsonable(taxpayer_profile.fields), ensure_ascii=False, default=str),
                "yearly_json": json.dumps(_jsonable(taxpayer_profile.yearly), ensure_ascii=False, default=str),
            }])
            con.execute("CREATE OR REPLACE TABLE taxpayer_profile AS SELECT * FROM profile_df")

        if esf_purchases is not None and not esf_purchases.empty:
            df = _dropcols(esf_purchases, ["raw_fields"])
            con.execute("CREATE OR REPLACE TABLE esf_purchases AS SELECT * FROM df")
        if esf_sales is not None and not esf_sales.empty:
            df2 = _dropcols(esf_sales, ["raw_fields"])
            con.execute("CREATE OR REPLACE TABLE esf_sales AS SELECT * FROM df2")
        if buyers is not None and not buyers.empty:
            b = _dropcols(buyers, ["years_active"])
            con.execute("CREATE OR REPLACE TABLE counterparties_buyers AS SELECT * FROM b")
        if suppliers is not None and not suppliers.empty:
            s = _dropcols(suppliers, ["years_active"])
            con.execute("CREATE OR REPLACE TABLE counterparties_suppliers AS SELECT * FROM s")
        if findings:
            f_df = pd.DataFrame(findings)
            con.execute("CREATE OR REPLACE TABLE risk_findings AS SELECT * FROM f_df")
        for form_code, results in (fno_results or {}).items():
            if not results:
                continue
            combined = pd.concat([r.dataframe for r in results], ignore_index=True)
            combined = _dropcols(combined, ["raw_fields"])
            table_name = f"tax_forms_{form_code.replace('.', '_')}"
            con.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM combined")
        con.close()
        return True
    except Exception:
        try:
            con.close()
        except Exception:
            pass
        return False


def _dropcols(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    return df.drop(columns=[c for c in cols if c in df.columns])


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    return obj


def list_tables(user_id: int | str = "shared") -> list[str]:
    con = _connect(user_id)
    if con is None:
        return []
    try:
        rows = con.execute("SHOW TABLES").fetchall()
        con.close()
        return [r[0] for r in rows]
    except Exception:
        return []
