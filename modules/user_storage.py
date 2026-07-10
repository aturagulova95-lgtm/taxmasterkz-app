"""
user_storage.py
Файловое хранилище с разделением по пользователям: каждый пользователь
получает свою папку data/uploaded_files/<user_id>/ — файлы одного
пользователя физически не видны и не доступны другому.

Также здесь функция автоочистки старых временных файлов (для крон/админ-панели).
"""

from __future__ import annotations

import os
import re
import shutil
import time
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
UPLOADS_ROOT = os.path.join(DATA_DIR, "uploaded_files")


def _safe_filename(name: str) -> str:
    """Убирает опасные символы из имени файла (path traversal и т.п.),
    сохраняя читаемость (кириллица разрешена)."""
    name = os.path.basename(name)
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name or "file"


def user_folder(user_id: int) -> str:
    path = os.path.join(UPLOADS_ROOT, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path


def save_uploaded_file(user_id: int, filename: str, file_bytes: bytes) -> tuple[str, int]:
    """Сохраняет файл в папку пользователя. Возвращает (путь, размер в байтах).
    Если файл с таким именем уже есть — добавляет метку времени, чтобы не потерять
    старую версию (например, повторная выгрузка ЭСФ за тот же год)."""
    folder = user_folder(user_id)
    safe_name = _safe_filename(filename)
    target = os.path.join(folder, safe_name)
    if os.path.exists(target):
        stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        base, ext = os.path.splitext(safe_name)
        target = os.path.join(folder, f"{base}__{stamp}{ext}")
    with open(target, "wb") as f:
        f.write(file_bytes)
    return target, len(file_bytes)


def list_user_files(user_id: int) -> list[dict]:
    folder = user_folder(user_id)
    out = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            out.append({
                "name": name,
                "path": path,
                "size_bytes": os.path.getsize(path),
                "modified": datetime.fromtimestamp(os.path.getmtime(path)).isoformat(),
            })
    return out


def delete_all_user_files(user_id: int) -> int:
    """Удаляет все файлы пользователя. Возвращает количество удалённых файлов."""
    folder = user_folder(user_id)
    count = 0
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        try:
            if os.path.isfile(path):
                os.remove(path)
                count += 1
        except OSError:
            pass
    return count


def cleanup_old_files(days: int = 30) -> int:
    """Удаляет файлы старше N дней во ВСЕХ папках пользователей (для админ-панели/крона).
    Возвращает количество удалённых файлов."""
    if not os.path.isdir(UPLOADS_ROOT):
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for user_dir in os.listdir(UPLOADS_ROOT):
        full_dir = os.path.join(UPLOADS_ROOT, user_dir)
        if not os.path.isdir(full_dir):
            continue
        for name in os.listdir(full_dir):
            path = os.path.join(full_dir, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    return removed


def total_storage_bytes() -> int:
    if not os.path.isdir(UPLOADS_ROOT):
        return 0
    total = 0
    for root, _, files in os.walk(UPLOADS_ROOT):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total
