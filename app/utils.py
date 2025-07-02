# app/utils.py

import re
from typing import Tuple

from fastapi import HTTPException


def parse_range_header(range_header: str, total_size: int) -> Tuple[int, int]:
    """
    Parses the HTTP 'Range' header to get the start and end bytes.
    """
    if not range_header:
        return 0, total_size - 1

    match = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if not match:
        raise HTTPException(status_code=416, detail="Invalid Range header format")

    start_str, end_str = match.groups()
    start = int(start_str)

    if end_str:
        end = int(end_str)
    else:
        # If the end is not specified, read to the end of the file
        end = total_size - 1

    if start >= total_size or end >= total_size or start > end:
        raise HTTPException(
            status_code=416, detail="Requested range not satisfiable"
        )

    return start, end
