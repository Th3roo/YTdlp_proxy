# app/utils.py

import re
from typing import Tuple

from fastapi import HTTPException

def parse_range_header(range_header: str, total_size: int) -> Tuple[int, int]:
    """
    Парсит HTTP заголовок 'Range' для получения начального и конечного байта.
    """
    if not range_header:
        return 0, total_size - 1

    match = re.match(r'bytes=(\d+)-(\d*)', range_header)
    if not match:
        raise HTTPException(status_code=416, detail="Неверный формат заголовка Range")

    start_str, end_str = match.groups()
    start = int(start_str)

    if end_str:
        end = int(end_str)
    else:
        # Если конец не указан, читаем до конца файла
        end = total_size - 1

    if start >= total_size or end >= total_size or start > end:
        raise HTTPException(status_code=416, detail="Запрошенный диапазон не может быть удовлетворён")

    return start, end