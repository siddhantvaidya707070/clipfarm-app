"""Strict timestamps shared by HTTP requests and ingestion."""
import math
import re


def parse_timestamp(value):
    if isinstance(value, bool):
        raise ValueError('Time must be seconds or HH:MM:SS')
    if isinstance(value, str) and ':' in value:
        if not re.fullmatch(r'\d{2,}:\d{2}:\d{2}(?:\.\d+)?', value.strip()):
            raise ValueError('Time must be seconds or HH:MM:SS')
        hours, minutes, seconds = map(float, value.strip().split(':'))
        if minutes >= 60 or seconds >= 60:
            raise ValueError('Minutes and seconds must be below 60')
        result = hours * 3600 + minutes * 60 + seconds
    else:
        try:
            result = float(value)
        except (TypeError, ValueError):
            raise ValueError('Time must be seconds or HH:MM:SS') from None
    if not math.isfinite(result) or result < 0:
        raise ValueError('Time must be finite and nonnegative')
    return result


def validate_range(start=0, end=None):
    start = parse_timestamp(0 if start in (None, '') else start)
    end = None if end in (None, '') else parse_timestamp(end)
    if end is not None and end <= start:
        raise ValueError('End must be later than start')
    return start, end
