"""Conservative, shared account-suspension evidence checks (no network or I/O)."""

import html
import re


ACCOUNT_BAN = re.compile(
    r"\baccount\s+(?:(?:has|have)\s+been\s+|is\s+|was\s+)?"
    r"(?:(?:permanently|temporarily|currently)\s+)?(?:suspended|banned)\b",
    re.IGNORECASE,
)
BAN_CODE = re.compile(r"\b(?:account|user)_(?:suspended|banned)\b", re.IGNORECASE)
ERROR_FIELDS = ('error', 'code', 'message', 'reason', 'detail', 'details')


def plain_message(value):
    if not isinstance(value, str):
        return ''
    return ' '.join(html.unescape(re.sub(r'<[^>]*>', ' ', value)).split())


def is_ban_error(status_code=None, error_text=None):
    """HTTP status, generic access denial and failure counts are NOT evidence."""
    message = plain_message(error_text)
    for pattern in (ACCOUNT_BAN, BAN_CODE):
        for match in pattern.finditer(message):
            # Avoid common hypothetical/negated help text around an account phrase.
            prefix = re.split(r'[.!?;]', message[:match.start()])[-1].lower()
            if re.search(r'\b(if|whether|not|never|no)\b', prefix):
                continue
            return True
    return False


def error_message(payload):
    """Read only error-related fields, never arbitrary successful API data."""
    if isinstance(payload, str):
        return plain_message(payload)
    if isinstance(payload, dict):
        parts = [error_message(payload[key]) for key in ERROR_FIELDS if key in payload]
        return ' | '.join(part for part in parts if part)
    if isinstance(payload, list):
        return ' | '.join(filter(None, (error_message(item) for item in payload)))
    return ''


def suspension_reason(response):
    """Inspect the actual response body before raise_for_status discards it."""
    try:
        payload = response.json()
    except (ValueError, TypeError):
        message = plain_message(response.text)
    else:
        message = error_message(payload)
    return message[:4000] if is_ban_error(error_text=message) else None


def is_confirmed_ban_record(record):
    """Legacy 403/failure-only archive entries must not trigger deletion."""
    return isinstance(record, dict) and (
        record.get('confirmed') is True or is_ban_error(error_text=record.get('reason'))
    )
