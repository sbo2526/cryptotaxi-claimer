"""Trip-start rules recovered from a0460df; no requests or credentials here."""

import math
import re


def valid_car_id(value):
    return ((isinstance(value, int) and not isinstance(value, bool) and value > 0)
            or (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) is not None))


def positive_count(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value > 0 and value % 1 == 0)
    except OverflowError:
        return False


def timestamp_ms(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and 946684800000 <= value <= 253402300799000)
    except OverflowError:
        return False


def start_block(car, now_ms):
    """Explicit permission AND left/bonus capacity, never limit-used arithmetic."""
    if not isinstance(car, dict) or not valid_car_id(car.get('id')):
        return 'invalid_car'
    if car.get('trip') is not None:
        return 'on_trip'
    if car.get('block') or car.get('broken') or car.get('canDrive') is not True:
        return 'not_permitted'
    quota = car.get('quota')
    if not isinstance(quota, dict):
        return 'unknown_quota'
    if positive_count(quota.get('left')):
        return None
    expiry = quota.get('bonusExpiresAt')
    if positive_count(quota.get('bonus')) and (expiry is None or (timestamp_ms(expiry) and expiry > now_ms)):
        return None
    return 'no_confirmed_capacity'
