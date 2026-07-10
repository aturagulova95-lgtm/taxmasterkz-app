"""
data_normalizer.py
Приведение "грязных" значений к единому виду:
- БИН/ИИН (12 цифр, устранение искажений Excel: '5.1240011241e+11', float, обрезание ведущих нулей);
- даты (разные форматы -> datetime);
- денежные суммы (текст, пробелы, запятые -> float);
- текстовые статусы/виды ЭСФ (обрезка пробелов, унификация регистра).

Все функции терпимы к мусору: при невозможности распознать значение
возвращают None/NaN, а не бросают исключение — это ключевое требование
для универсальности приложения (raw-выгрузки КГД почти всегда содержат
"грязные" ячейки).
"""

from __future__ import annotations

import re
from datetime import datetime, date

import pandas as pd


def normalize_bin(value) -> str | None:
    """
    Приводит БИН/ИИН к строке из 12 цифр.
    Обрабатывает:
      - float/scientific notation ('5.1240011241e+11' -> '051240011241')
      - потерю ведущего нуля Excel-ом
      - пробелы, дефисы
    """
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "-"):
        return None

    # научная нотация / float с .0
    if re.match(r"^-?\d+(\.\d+)?[eE][+-]?\d+$|^\d+\.0$", s):
        try:
            s = str(int(float(s)))
        except ValueError:
            pass

    s = re.sub(r"[^\d]", "", s)
    if not s:
        return None

    if len(s) == 11:
        s = "0" + s  # Excel часто теряет ведущий ноль БИН/ИИН
    if len(s) != 12:
        # оставляем как есть, но помечаем как потенциально некорректный —
        # вызывающий код может проверить длину самостоятельно
        return s
    return s


def normalize_date(value) -> date | None:
    """Парсит дату из разных форматов: dd.mm.yyyy, yyyy-mm-dd, excel serial, datetime."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value if isinstance(value, date) and not isinstance(value, datetime) else value.date()
    if isinstance(value, float) and pd.isna(value):
        return None

    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "-", "нет данных"):
        return None

    # excel serial number (число дней от 1899-12-30)
    if re.match(r"^\d{4,6}(\.\d+)?$", s):
        try:
            serial = float(s)
            if 20000 < serial < 60000:  # правдоподобный диапазон для дат 1954-2064
                base = datetime(1899, 12, 30)
                return (base + pd.Timedelta(days=serial)).date()
        except ValueError:
            pass

    s_clean = s.split(" ")[0]  # отбрасываем время, если есть "дата время"
    formats = [
        "%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d",
        "%d-%m-%Y", "%m/%d/%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s_clean, fmt).date()
        except ValueError:
            continue

    try:
        return pd.to_datetime(s, dayfirst=True, errors="raise").date()
    except Exception:
        return None


def normalize_amount(value) -> float | None:
    """Парсит денежную сумму из текста/числа: пробелы, запятые, неразрывные пробелы."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and pd.isna(value):
            return None
        return float(value)

    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "-", ""):
        return None

    s = s.replace("\xa0", "").replace(" ", "")
    # если есть и точка, и запятая — запятая, скорее всего, разделитель тысяч
    # либо десятичный разделитель в зависимости от порядка
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")

    s = re.sub(r"[^\d.\-]", "", s)
    if not s or s in ("-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def normalize_text(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none"):
        return None
    return re.sub(r"\s+", " ", s)


def normalize_yesno(value) -> bool | None:
    """Приводит 'Да'/'Нет'/'Нет данных' к True/False/None."""
    s = normalize_text(value)
    if s is None:
        return None
    low = s.lower()
    if low in ("да", "yes", "true", "1"):
        return True
    if low in ("нет", "no", "false", "0"):
        return False
    return None  # "Нет данных" и подобное — не False, а именно "неизвестно"


def normalize_bin_series(series: pd.Series) -> pd.Series:
    return series.apply(normalize_bin)


def normalize_date_series(series: pd.Series) -> pd.Series:
    return series.apply(normalize_date)


def normalize_amount_series(series: pd.Series) -> pd.Series:
    return series.apply(normalize_amount)


def quarter_of(d: date | None) -> int | None:
    if d is None:
        return None
    return (d.month - 1) // 3 + 1
