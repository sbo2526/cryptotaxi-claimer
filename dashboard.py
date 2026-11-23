#!/usr/bin/env python3
"""Protected local-file dashboard: configuration, real data freshness and history.

Rendering and browser refreshes read local files only; they never enable or call
external monitoring. Missing/invalid account data is not presented as zero.
"""

import base64
import binascii
from datetime import date, datetime, timedelta
import hmac
import html
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import math
import os
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from trip_rules import timestamp_ms
from rental_rules import rental_preview
from monitor_runtime import read_report
from ban_detection import is_ban_error, is_confirmed_ban_record
from runtime_config import (DATA_DIR, primary_username, monitoring_enabled, initialize_data,
                            ALLOWED_PRIMARY_ACCOUNT, auto_start_enabled)

TZ = ZoneInfo('Asia/Tehran')
PORT = int(os.environ.get('PORT', '8080'))
STATUS_FILE = str(DATA_DIR / 'status.json')
BANNED_ACCOUNTS_FILE = str(DATA_DIR / 'banned_accounts.json')
FRESH_FOR = timedelta(minutes=10)


def esc(value):
    return html.escape(str(value), quote=True)


def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


PERSIAN_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                  "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]


def _jalali_leap(jy):
    return (jy % 33) in (1, 5, 9, 13, 17, 22, 26, 30)


def _jalali_month_len(jy, jm):
    if jm <= 6:
        return 31
    if jm <= 11:
        return 30
    return 30 if _jalali_leap(jy) else 29


def gregorian_to_jalali(y, m, d):
    """Convert a Gregorian date to a Jalali (y, m, d) tuple."""
    # epoch: Jalali 1401-01-01 = Gregorian 2022-03-21
    epoch = date(2022, 3, 21).toordinal()
    days = date(y, m, d).toordinal() - epoch
    jy, jm, jd = 1401, 1, 1
    while days > 0:
        rem = _jalali_month_len(jy, jm)
        if days >= rem:
            days -= rem
            jd = 1
            if jm == 12:
                jy += 1
                jm = 1
            else:
                jm += 1
        else:
            jd += days
            days = 0
    return jy, jm, jd


def fmt_jalali(s):
    """Format a 'YYYY-MM-DD[ HH:MM:SS]' string as Jalali, e.g.
    '2026-11-23 15:29:24' -> '2 آذر 1405، 15:29:24'."""
    s = (s or "").strip()
    if not s:
        return ""
    try:
        time_part = ""
        if " " in s:
            ds, ts = s.split(" ", 1)
            time_part = "، " + ts
        else:
            ds = s
        y, m, d = map(int, ds.split("-"))
        jy, jm, jd = gregorian_to_jalali(y, m, d)
        return f"{jd} {PERSIAN_MONTHS[jm - 1]} {jy}" + time_part
    except (ValueError, IndexError):
        return s



def banned_accounts_info():
    """Merge recorded bans, including accounts removed from accounts.json.

    The archive is authoritative; explicit BAN flags in status.json fill gaps.
    Ordinary login/sweep errors alone are not interpreted as bans here.
    """
    archive = load_json(BANNED_ACCOUNTS_FILE, {})
    records = archive.get("banned", []) if isinstance(archive, dict) else []
    if not isinstance(records, list):
        records = []

    by_user = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        user = record.get("username")
        if not isinstance(user, str) or not user.strip():
            continue
        detected_at = str(record.get("detected_at") or "")
        previous = by_user.get(user)
        confirmed = is_confirmed_ban_record(record)
        if previous and previous["confirmed"] and not confirmed:
            continue
        if previous is None or (confirmed and not previous["confirmed"]) or detected_at >= previous["detected_at"]:
            by_user[user] = {
                "username": user,
                "reason": str(record.get("reason") or ""),
                "confirmed": confirmed,
                "detected_at": detected_at,
            }

    status = load_json(STATUS_FILE, {})
    if isinstance(status, dict):
        for user, account in status.items():
            if user == "_meta" or not isinstance(account, dict):
                continue
            if account.get("banned") is not True and account.get("last_result") != "BAN":
                continue
            if not isinstance(user, str) or not user.strip():
                continue
            entry = by_user.setdefault(user, {
                "username": user, "reason": "", "detected_at": "", "confirmed": False,
            })
            if is_ban_error(error_text=account.get("ban_reason")):
                if not entry["confirmed"]:
                    entry["reason"] = str(account["ban_reason"])
                    entry["detected_at"] = str(account.get("banned_at") or "")
                entry["confirmed"] = True
            entry["reason"] = entry["reason"] or str(account.get("ban_reason") or "")
            entry["detected_at"] = entry["detected_at"] or str(account.get("banned_at") or "")

    return sorted(by_user.values(), key=lambda row: (row["detected_at"], row["username"]), reverse=True)


def active_accounts_info():
    """Only explicitly selected primary identity; no credential files or status ghosts."""
    user = primary_username()
    if not user:
        return []
    report = read_report(user=user)
    if report and report['stage'] == 'stopped' and report['code'] == 'suspended':
        return []
    banned = {entry["username"] for entry in banned_accounts_info() if entry["confirmed"]}
    return [] if user in banned else [{"username": user}]


def finite_number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


STOP_MESSAGES = {
    'request_failed': 'درخواست ورود یا خواندن اطلاعات با خطای شبکه، HTTP یا پاسخ نامعتبر روبه‌رو شد؛ تکرار خودکار انجام نشد. Runtime Logs سرویس را بررسی کنید.',
    'state_invalid': 'ساختار اطلاعات دریافتی یا ذخیره‌سازی محلی معتبر نبود؛ برای جلوگیری از استارت بر اساس دادهٔ ناقص، برنامه متوقف شد.',
    'start_unknown': 'نتیجهٔ استارت یا تأیید آن نامشخص است؛ تلاش در دیتابیس محفوظ می‌ماند و خودکار تکرار نمی‌شود. وضعیت تاکسی و لاگ اجرا را برای رسیدگی دستی بررسی کنید.',
    'start_blocked': 'اجازه، سهمیه یا تازگی اطلاعات برای استارت تأیید نشد؛ پایش برای بررسی متوقف شده است.',
    'invalid_config': 'تنظیم اکانت، رمز یا اتصال دیتابیس ناقص یا نامعتبر است؛ Variables همین سرویس را بررسی کنید.',
    'database_error': 'اتصال یا آماده‌سازی دیتابیس قفل ناموفق بوده است؛ اتصال PostgreSQL و دسترسی آن را بررسی کنید. هیچ ورود جدیدی به سایت انجام نشد.',
    'already_running': 'اجرای دیگری قفل این اکانت را دارد؛ این اجرا به سایت وارد نشد. سرویس یا Replica تکراری و هم‌پوشانی استقرارها را بررسی کنید؛ قفل را حذف نکنید.',
    'safety_unavailable': 'مالکیت قفل یا وضعیت ایمنی دیتابیس در دسترس نیست؛ پایش متوقف شده و اتصال مجدد خودکار انجام نمی‌شود.',
    'worker_exited': 'فرایند پایش خارج شده است؛ علت مشخصی ثبت نکرده است. Runtime Logs را بررسی کنید؛ راه‌اندازی مجدد خودکار انجام نمی‌شود.',
    'needs_login': 'نشست ورود پذیرفته نشد؛ بررسی و ورود دستی لازم است. ورود مجدد خودکار انجام نمی‌شود.',
    'access_denied': 'دسترسی رد شده است؛ پایش تا بررسی دستی متوقف می‌ماند.',
    'rate_limited': 'محدودیت درخواست یا انتظار تکراری؛ پایش برای بررسی دستی متوقف شده است.',
    'manual_stop': 'توقف دستی ثبت شده است.',
    'manual_review': 'خطا یا مشکل ایمنی ثبت شده است؛ بررسی دستی لازم است.',
    'suspended': 'تعلیق ثبت شده است؛ پایش نباید مجدداً فعال شود.',
}
STARTUP_MESSAGES = {
    'starting': 'شروع فرایند پایش ثبت شده است؛ هنوز دریافت موفقی تأیید نشده است.',
    'acquiring_lock': 'آخرین مرحلهٔ ثبت‌شده: گرفتن قفل مشترک دیتابیس، پیش از ورود به سایت.',
    'waiting_lock': 'قفل حساب در اختیار اجرای دیگری است؛ این اجرا در شروع حداکثر دو دقیقه منتظر آزادشدن آن می‌ماند و هنوز به بازی وارد نشده است.',
    'logging_in': 'آخرین مرحلهٔ ثبت‌شده: تلاش ورود؛ موفقیت ورود هنوز تأیید نشده است.',
    'waiting_first_read': 'پاسخ بدون خطا برای ورود ثبت شده است؛ دریافت اطلاعات هنوز تأیید نشده و فاصلهٔ درخواست‌ها و بودجهٔ مشترک باید رعایت شود.',
}

SCHEDULE_REASONS = {
    'ready_trip': 'بررسی تاکسی بعدی برای استارت مجاز',
    'trip_end': 'بر اساس پایان سفر ثبت‌شده',
    'unknown_timer': 'زمان سفر نامشخص است؛ بررسی دوره‌ای',
    'no_active_trip': 'سفر فعالی ثبت نشده؛ بررسی دوره‌ای',
    'periodic_check': 'بررسی دوره‌ای سفر طولانی',
}


def saved_activity(status, user, now):
    """Validate primary-scoped worker evidence; never treat it as liveness."""
    meta = status.get('_meta')
    if not user or not isinstance(meta, dict) or meta.get('monitor_user') != user:
        return None
    kind = meta.get('monitor_status')
    if kind not in ('starting', 'waiting', 'cooldown', 'stopped', 'suspended'):
        return None
    try:
        updated = datetime.strptime(meta.get('updated_at', ''), '%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ)
        if updated > now + timedelta(seconds=30):
            return None
    except (TypeError, ValueError):
        return None
    next_check = None
    if kind in ('waiting', 'cooldown'):
        try:
            next_check = datetime.strptime(meta.get('next_check_at', ''), '%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ)
            if next_check <= updated:
                return None
        except (TypeError, ValueError):
            return None
    return {'status': kind, 'updated': updated, 'next_check': next_check,
            'message': STOP_MESSAGES.get(str(meta.get('stop_code')), STOP_MESSAGES['manual_review']),
            'reason': SCHEDULE_REASONS.get(str(meta.get('schedule_reason')), '')}


def dashboard_view(status=None, now=None):
    """Separate configured identity, monitoring opt-in and received data.

    A recent sample is evidence of a receive, NOT proof a worker is running now.
    Timestamp comes only from that primary account's successful persisted sample.
    """
    status = load_json(STATUS_FILE, {}) if status is None else status
    status = status if isinstance(status, dict) else {}
    now = now or datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    user = primary_username()
    enabled = monitoring_enabled()
    activity = saved_activity(status, user, now)
    runtime = read_report(user=user, now=now)
    suspended = any(row['username'] == user and row['confirmed'] for row in banned_accounts_info()) or bool(
        runtime and runtime['stage'] == 'stopped' and runtime['code'] == 'suspended')
    account = status.get(user) if user else None
    received = None
    sample = None
    if isinstance(account, dict) and account.get('last_result') == 'OK' and not suspended and not account.get('banned'):
        try:
            received = datetime.strptime(account.get('last_run', ''), '%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ)
            cars = account.get('cars')
            if received > now + timedelta(seconds=30) or not isinstance(cars, list) or any(not isinstance(car, dict) or car.get('status') not in ('on_trip', 'idle', 'unavailable') for car in cars):
                raise ValueError('Invalid sample')
            sample = account
        except (TypeError, ValueError):
            received = None
    stale = received is not None and now - received > FRESH_FOR
    complete_config = bool(os.environ.get('PRIMARY_ACCOUNT_PASSWORD') and os.environ.get('MONITOR_LOCK_DATABASE_URL'))
    if suspended:
        label, tone, note = 'متوقف: تعلیق ثبت‌شده', 'danger', 'تعلیق صریح برای اکانت اصلی ثبت شده است.'
    elif not enabled:
        label, tone, note = 'پایش خاموش', 'neutral', 'هیچ درخواستی از داشبورد به سایت ارسال نمی‌شود.'
    elif not user:
        label, tone, note = 'در انتظار تنظیم اکانت', 'warning', 'نام اکانت اصلی هنوز مشخص نشده است.'
    elif not complete_config:
        label, tone, note = 'پیکربندی پایش ناقص', 'warning', 'تنظیمات لازم برای پایش کامل نیست؛ فعال‌بودن آن تأیید نمی‌شود.'
    elif runtime and runtime['stage'] == 'stopped':
        label, tone, note = 'پایش متوقف شده است', 'warning', STOP_MESSAGES.get(runtime['code'], STOP_MESSAGES['manual_review'])
    elif runtime and runtime['stage'] in STARTUP_MESSAGES:
        tone, note = 'warning', STARTUP_MESSAGES[runtime['stage']]
        if now - runtime['updated'] > timedelta(minutes=3):
            label = 'شروع پایش نیازمند بررسی است'
            note += ' بیش از سه دقیقه از این گزارش گذشته؛ ادامهٔ اجرا تأیید نمی‌شود. Runtime Logs را بررسی کنید.'
        else:
            label = 'شروع پایش ثبت شده است'
    elif activity and activity['status'] in ('stopped', 'suspended'):
        label, tone, note = 'پایش متوقف شده است', 'warning', activity['message']
    elif activity and activity['status'] == 'cooldown':
        label, tone, note = 'مهلت انتظار ثبت شده است', 'warning', 'بررسی بعدی باید محدودیت سرور و بودجهٔ مشترک درخواست را رعایت کند.'
    elif received is None:
        label, tone, note = 'دریافت موفق ثبت نشده است', 'warning', 'دادهٔ موفقی در این محل ذخیره نشده و وضعیت اجرای پایش تأیید نشده است؛ روشن‌بودن تنظیم پایش به‌تنهایی نشانهٔ اجرا نیست.'
    elif stale:
        label, tone, note = 'اطلاعات قدیمی', 'warning', 'بیش از ۱۰ دقیقه از آخرین دریافت موفق گذشته است.'
    else:
        label, tone, note = 'آخرین دریافت تازه است', 'success', 'این وضعیت تازگی داده را نشان می‌دهد، نه تضمین اجرای فعلی پایش.'
    if enabled and not suspended and not (runtime and runtime['stage'] in ('stopped', *STARTUP_MESSAGES)) and activity and activity['next_check']:
        scheduled = fmt_jalali(activity['next_check'].strftime('%Y-%m-%d %H:%M:%S'))
        if activity['next_check'] <= now:
            note += ' موعد بررسی ثبت‌شده گذشته است؛ دریافت تازه یا اجرای فعلی پایش تأیید نمی‌شود.'
        else:
            label_text = 'پایان مهلت انتظار ثبت‌شده' if activity['status'] == 'cooldown' else 'بررسی بعدیِ برنامه‌ریزی‌شده'
            note += f' {label_text}: {scheduled} (تهران). این زمان تضمین اجرا نیست.'
            if activity['reason']:
                note += ' ' + activity['reason'] + '.'
    if not enabled:
        empty_note = 'پایش خارجی خاموش است؛ هیچ عملیات خودکاری انجام نمی‌شود.'
    elif suspended:
        empty_note = 'پایش این اکانت متوقف است؛ سابقهٔ تعلیق را در پایین صفحه ببینید.'
    elif runtime and runtime['stage'] == 'stopped':
        empty_note = STOP_MESSAGES.get(runtime['code'], STOP_MESSAGES['manual_review'])
    elif activity and activity['status'] in ('stopped', 'suspended'):
        empty_note = activity['message']
    elif auto_start_enabled():
        empty_note = 'بدون وضعیت معتبر، استارت انجام نمی‌شود؛ کلیم و رنت غیرفعال‌اند.'
    else:
        empty_note = 'دریافت موفقی ثبت نشده است؛ هیچ عملیات استارت، اجاره یا دریافت جایزه انجام نمی‌شود.'
    if sample is None:
        empty_note += ' اگر قبلاً اطلاعات نمایش داده می‌شد، اتصال Volume پایدار به /app/data و مقدار DATA_DIR را بررسی کنید؛ نبود داده به‌تنهایی علت را مشخص نمی‌کند.'
    return {'user': user, 'enabled': enabled, 'suspended': suspended, 'sample': sample,
            'received': received, 'stale': stale, 'label': label, 'tone': tone, 'note': note,
            'empty_note': empty_note, 'activity': activity, 'runtime': runtime, 'now': now}


def operation_mode():
    if monitoring_enabled() and auto_start_enabled() and primary_username():
        return 'استارت خودکار در تنظیمات فعال است'
    return 'فقط‌خواندنی'


def icon(name, css=''):
    paths = {
        'grid': '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
        'key': '<circle cx="8" cy="8" r="5"/><path d="m12 12 8 8m-4-4 3-3m-6 0 3-3"/>',
        'car': '<path d="m5 8 2-4h10l2 4M4 8h16v10H4zM7 18v2m10-2v2M7 12h1m8 0h1M4 15h16"/>',
        'clock': '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
        'refresh': '<path d="M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 2M5 16a8 8 0 0 0 13 2"/>',
        'check': '<path d="m6 12 4 4 8-9"/>',
        'pause': '<path d="M9 5v14M15 5v14"/>',
        'alert': '<path d="m12 3 10 17H2L12 3ZM12 9v4m0 3v1"/>',
        'wallet': '<path d="M20 7H5a2 2 0 0 1 0-4h13v4M3 5v14a2 2 0 0 0 2 2h15V7M20 11h-6v6h6m-3-3h.01"/>',
        'bolt': '<path d="m13 2-9 12h7l-1 8 10-13h-7l1-7Z"/>',
        'history': '<path d="M3 4v6h6M4 9a9 9 0 1 1 0 7m8-9v6l4 2"/>',
        'lock': '<rect x="5" y="10" width="14" height="11" rx="3"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 4v3"/>',
        'arrow': '<path d="M19 12H5m6-6-6 6 6 6"/>',
    }
    return f'<svg class="icon {css}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{paths.get(name, paths["grid"])}</svg>'


def stop_code(view):
    runtime, activity = view.get('runtime'), view.get('activity')
    if runtime and runtime['stage'] == 'stopped':
        return runtime['code'] or 'manual_review'
    if runtime and runtime['stage'] in STARTUP_MESSAGES:
        return None  # A newer startup report supersedes an older saved stop.
    if activity and activity['status'] in ('stopped', 'suspended'):
        # Do not reverse-parse user/server text; only known message matches.
        return next((code for code, message in STOP_MESSAGES.items() if message == activity['message']), 'manual_review')
    return None


SIMPLE_STOPS = {
    'request_failed': ('دریافت پاسخ معتبر ناموفق بود', 'درخواست با خطای ارتباط یا پاسخ نامعتبر متوقف شد.', 'گزارش اجرای سرویس را بررسی کنید؛ رمز، توکن یا آدرس دیتابیس را در چت نفرستید.'),
    'state_invalid': ('اطلاعات یا ذخیره‌سازی نیاز به بررسی دارد', 'ساختار داده یا ثبت محلی آن قابل تأیید نبود.', 'فضای ذخیره‌سازی و گزارش اجرای سرویس را بررسی کنید؛ استارت اجباری انجام ندهید.'),
    'already_running': ('یک اجرای دیگر شناسایی شد', 'هنگام شروع، اجرای دیگری قفل همین حساب را داشت.', 'اجرای اضافه را متوقف کنید؛ قفل دیتابیس را پاک نکنید.'),
    'database_error': ('ارتباط با دیتابیس برقرار نشد', 'این اجرا پیش از ورود به بازی متوقف شده است.', 'اتصال دیتابیس را در تنظیمات Railway بررسی کنید.'),
    'needs_login': ('ورود به بازی نیاز به بررسی دارد', 'سایت نشست ورود را نپذیرفته و برنامه متوقف شده است.', 'تنظیمات ورود و گزارش اجرای سرویس را بررسی کنید؛ رمز را در چت نفرستید.'),
    'start_unknown': ('نتیجهٔ استارت روشن نیست', 'برای جلوگیری از استارت تکراری، برنامه متوقف شده است.', 'اول وضعیت سفر را در بازی بررسی کنید؛ درخواست را پشت‌سرهم تکرار نکنید.'),
    'start_blocked': ('شرایط استارت تأیید نشد', 'اطلاعات یا اجازهٔ لازم برای حرکت کافی نبوده است.', 'جزئیات فنی را ببینید؛ استارت اجباری انجام نمی‌شود.'),
    'rate_limited': ('پایش به محدودیت درخواست رسیده', 'برنامه برای بررسی دستی متوقف شده است.', 'گزارش سرویس را بررسی کنید و پشت‌سرهم Restart نزنید.'),
    'invalid_config': ('تنظیمات حساب کامل نیست', 'یکی از تنظیمات لازم برای دریافت اطلاعات معتبر نیست.', 'تنظیمات حساب، رمز و دیتابیس را در Railway بررسی کنید.'),
    'access_denied': ('سایت اجازهٔ دسترسی نداده', 'برای جلوگیری از تلاش مکرر، برنامه متوقف شده است.', 'علت رد دسترسی را بررسی کنید؛ ورود خودکار تکرار نمی‌شود.'),
}


def simple_status(view):
    """Plain-language presentation only; the monitoring view remains authoritative."""
    if view['suspended']:
        return 'حساب تعلیق شده است', 'همهٔ عملیات این حساب متوقف می‌ماند.', 'ابتدا موضوع را با پشتیبانی بازی حل کنید؛ برنامه را دوباره فعال نکنید.', 'danger', 'alert'
    if not view['user']:
        return 'حساب اصلی را تنظیم کنید', 'هنوز حساب مجاز برای این اجرا تنظیم نشده است.', 'ایمیل حساب اصلی را در تنظیمات سرویس وارد کنید؛ راهنما در جزئیات فنی است.', 'warning', 'alert'
    if not view['enabled']:
        return 'دریافت اطلاعات خاموش است', 'در این حالت اطلاعات جدیدی از بازی گرفته نمی‌شود.', 'اگر فقط مشاهدهٔ اطلاعات ذخیره‌شده را می‌خواهید، نیازی به کاری نیست.', 'neutral', 'pause'
    if view['label'] == 'پیکربندی پایش ناقص':
        return SIMPLE_STOPS['invalid_config'] + ('warning', 'alert')
    code = stop_code(view)
    if code:
        copy = SIMPLE_STOPS.get(code, ('پایش متوقف شده است', 'برنامه برای بررسی دستی متوقف شده است.', 'جزئیات فنی و گزارش اجرای سرویس را بررسی کنید.'))
        return copy + ('warning', 'alert')
    if view['label'] == 'شروع پایش نیازمند بررسی است':
        return 'شروع برنامه طول کشیده است', 'چند دقیقه از آخرین گزارش شروع گذشته؛ ادامهٔ اجرا تأیید نشده است.', 'گزارش اجرای سرویس را در Railway بررسی کنید؛ Restart مکرر نزنید.', 'warning', 'clock'
    runtime = view.get('runtime')
    if runtime and runtime['stage'] == 'waiting_lock':
        return 'منتظر آزادشدن قفل حساب', 'یک اجرای دیگر قفل را دارد؛ این اجرا هنوز به بازی وارد نشده است.', 'انتظار اولیه حداکثر دو دقیقه است؛ اگر ادامه داشت، سرویس یا Replica تکراری را بررسی کنید. قفل را حذف نکنید.', 'neutral', 'clock'
    if runtime and runtime['stage'] in STARTUP_MESSAGES:
        return 'شروع دریافت ثبت شده است', 'هنوز اولین دریافت اطلاعات در این اجرا تأیید نشده است.', 'یک تا دو دقیقه فرصت بدهید؛ صفحه خودش بازخوانی می‌شود.', 'neutral', 'clock'
    if view.get('activity') and view['activity']['status'] == 'cooldown':
        return 'مهلت انتظار ثبت شده است', 'درخواست بعدی باید محدودیت زمانی را رعایت کند.', 'فعلاً Restart نزنید؛ زمان برنامه‌ریزی‌شده را در پایین ببینید.', 'warning', 'clock'
    if view['sample'] is None:
        return 'هنوز اطلاعاتی در دسترس نیست', 'دریافت موفقی در داده‌های محلی این اجرا ثبت نشده است.', 'جزئیات فنی را ببینید؛ اگر خطایی ثبت نشده، Logs سرویس Railway را بررسی کنید.', 'warning', 'clock'
    if view['stale']:
        return 'اطلاعات ذخیره‌شده را می‌بینید', 'بیش از ۱۰ دقیقه از دریافت این اطلاعات گذشته است.', 'زمان بررسی بعدی را ببینید؛ این اطلاعات وضعیت فعلی بازی را تضمین نمی‌کند.', 'warning', 'clock'
    return 'اطلاعات حساب دریافت شده است', 'وضعیت تاکسی‌ها بر اساس آخرین دریافت از بازی نمایش داده می‌شود.', 'تاکسی‌ها را در پایین ببینید؛ زمان دریافت، به معنی روشن‌بودن قطعی سرویس نیست.', 'success', 'check'


def next_check_text(view):
    activity, runtime = view.get('activity'), view.get('runtime')
    if (not view['enabled'] or view['suspended'] or stop_code(view)
            or view['label'] == 'پیکربندی پایش ناقص'
            or (runtime and runtime['stage'] in STARTUP_MESSAGES)
            or not activity or not activity['next_check']):
        return 'زمانی ثبت نشده', 'بعد از دریافت اطلاعات، برنامه مشخص می‌شود.'
    when = activity['next_check']
    # dashboard_view captures the render clock, so tests and UI use the same time.
    if when <= view.get('now', datetime.now(TZ)):
        return 'موعد بررسی گذشته است', 'دریافت تازه یا ادامهٔ اجرا هنوز تأیید نشده است.'
    label = 'پایان مهلت انتظار' if activity['status'] == 'cooldown' else 'زمان بررسی برنامه‌ریزی‌شده'
    return when.strftime('%H:%M:%S'), fmt_jalali(when.strftime('%Y-%m-%d')) + ' · ' + label + '؛ نه تضمین اجرا.'


def status_section(view):
    title, subtitle, action, tone, symbol = simple_status(view)
    auto = view['enabled'] and auto_start_enabled() and bool(view['user'])
    if auto and (view['suspended'] or stop_code(view)):
        auto_title, auto_note = 'تنظیم روشن؛ اجرا متوقف', 'ابتدا علت توقف را بررسی کنید.'
    elif auto and view['label'] == 'پیکربندی پایش ناقص':
        auto_title, auto_note = 'تنظیم روشن؛ پیکربندی ناقص', 'برای اجرا، تنظیمات لازم باید کامل باشد.'
    elif auto:
        auto_title, auto_note = 'در تنظیمات روشن است', 'اجرا فقط با تأیید نوبت، سهمیه و وضعیت تازه.'
    else:
        auto_title, auto_note = 'خاموش است', 'برنامه تاکسی‌ها را خودکار استارت نمی‌زند.'
    received = view['received'].strftime('%H:%M:%S') if view['received'] else 'در دادهٔ محلی ثبت نشده'
    received_title = fmt_jalali(view['received'].strftime('%Y-%m-%d %H:%M:%S')) if view['received'] else 'دریافت موفقی ثبت نشده'
    received_note = fmt_jalali(view['received'].strftime('%Y-%m-%d')) + ' · تهران' if view['received'] else 'منتظر ثبت یک دریافت موفق هستیم.'
    upcoming, upcoming_note = next_check_text(view)
    return f'''<section id="overview" class="status-area" aria-labelledby="statusTitle">
      <div class="status-banner {tone}"><div class="status-symbol">{icon(symbol)}</div>
        <div class="status-copy"><span class="eyebrow">وضعیت حساب شما</span><h2 id="statusTitle">{esc(title)}</h2><p>{esc(subtitle)}</p></div>
        <a class="text-link" href="#technicalPanel">جزئیات فنی {icon('arrow')}</a>
        <div class="next-action"><span>پیشنهاد بعدی</span><p>{esc(action)}</p></div>
      </div>
      <div class="overview" aria-label="خلاصهٔ وضعیت">
        <article class="overview-card"><span class="card-icon">{icon('bolt')}</span><span class="eyebrow">استارت خودکار</span>
          <strong>{auto_title}</strong><p>{auto_note}</p></article>
        <article class="overview-card"><span class="card-icon">{icon('refresh')}</span><span class="eyebrow">آخرین دریافت از بازی</span>
          <strong class="received-at" dir="auto" title="{esc(received_title)}">{esc(received)}</strong><p>{esc(received_note)}</p></article>
        <article class="overview-card"><span class="card-icon">{icon('clock')}</span><span class="eyebrow">بررسی بعدی</span>
          <strong dir="auto">{esc(upcoming)}</strong><p>{esc(upcoming_note)}</p></article>
      </div></section>'''


def technical_section(view):
    account_label = 'تنظیم‌شده' if view['user'] else 'تنظیم نشده'
    identity = view['user'] or ALLOWED_PRIMARY_ACCOUNT
    received = fmt_jalali(view['received'].strftime('%Y-%m-%d %H:%M:%S')) if view['received'] else 'در دادهٔ محلی ثبت نشده'
    code = stop_code(view) or 'توقفی ثبت نشده'
    body = f'''<dl class="technical-list"><div><dt>کد توقف</dt><dd><bdi>{esc(code)}</bdi></dd></div><div><dt>وضعیت دقیق</dt><dd>{esc(view['label'])}</dd></div>
      <div><dt>گزارش برنامه</dt><dd>{esc(view['note'])}</dd></div>
      <div><dt>اکانت اصلی</dt><dd>{account_label} · <bdi>{esc(identity)}</bdi></dd></div>
      <div><dt>حالت تنظیم‌شده</dt><dd>{operation_mode()}</dd></div>
      <div><dt>زمان دریافت کامل</dt><dd>{esc(received)} · به وقت تهران</dd></div></dl>
      <p class="technical-help">تنظیم اکانت: <code>PRIMARY_ACCOUNT</code> · فعال‌سازی استارت: <code>AUTO_START_TRIPS_ENABLED=true</code>.
      تغییر تنظیمات فقط در Railway انجام می‌شود؛ بازخوانی این صفحه عملیاتی در بازی انجام نمی‌دهد.</p>'''
    if view['sample'] is None:
        body += f'<p class="technical-help">{esc(view["empty_note"])}</p>'
    return disclosure('technicalPanel', 'جزئیات فنی و راهنمای تنظیمات', 'اختیاری', body, icon('grid'))


def trip_count_text(value):
    """Reported nonnegative integer counts only; never invent a zero/total."""
    if finite_number(value) and value >= 0 and value % 1 == 0:
        return esc(int(value))
    return 'نامشخص'


def taxi_art():
    return '''<svg class="taxi-art" viewBox="0 0 280 110" fill="none" aria-hidden="true">
      <ellipse cx="140" cy="95" rx="108" ry="5" fill="currentColor" opacity=".08"/>
      <path d="M40 65h18l21-30h91l25 30h28c10 0 17 8 17 18v5H36V74c0-5 1-9 4-9Z" fill="currentColor"/>
      <path d="m85 41-16 24h45V41H85Zm38 0v24h59l-20-24h-39Z" fill="#fff" opacity=".9"/>
      <path d="M125 35v-9h26v9" fill="currentColor"/><path d="M47 73h12m152 0h18" stroke="#fff" stroke-width="5" stroke-linecap="round"/>
      <path d="M121 70v15m-54-3h140" stroke="#172338" stroke-opacity=".2" stroke-width="2"/>
      <circle cx="79" cy="86" r="15" fill="#263147"/><circle cx="79" cy="86" r="7" fill="#fff"/>
      <circle cx="200" cy="86" r="15" fill="#263147"/><circle cx="200" cy="86" r="7" fill="#fff"/>
      <path d="M125 32h4m4 0h4m4 0h4" stroke="#263147" stroke-width="2"/>
      </svg>'''


def is_turn_block(car):
    message = str(car.get('block_msg') or '').strip().rstrip('.').casefold()
    return message == 'ahora no es tu turno con este taxi'


def start_explanation(car, view=None):
    """Describe saved eligibility separately from configuration and actual starts."""
    if view and (view['suspended'] or stop_code(view)):
        return 'استارت خودکار متوقف است؛ علت توقف را بالای صفحه ببینید.'
    if not monitoring_enabled() or not auto_start_enabled():
        return 'استارت خودکار خاموش است؛ برای فعال‌سازی، راهنمای بالای تاکسی‌ها را ببینید.'
    if view and view['label'] == 'پیکربندی پایش ناقص':
        return 'تنظیمات لازم برای اجرای استارت کامل نیست.'
    if view and view.get('runtime') and view['runtime']['stage'] in STARTUP_MESSAGES:
        return 'شروع سرویس ثبت شده؛ دریافت و تأیید تازه لازم است.'
    if view and view['stale']:
        return 'اطلاعات قدیمی است؛ پیش از استارت باید وضعیت دوباره دریافت شود.'
    check = car.get('start_check')
    reasons = {
        'invalid_car': 'شناسهٔ معتبر تاکسی ثبت نشده است.',
        'not_permitted': 'اجازهٔ حرکت، نوبت یا سلامت تاکسی تأیید نشده است.',
        'unknown_quota': 'سهمیهٔ قابل استفاده مشخص نیست؛ استارت انجام نمی‌شود.',
        'no_confirmed_capacity': 'سفر عادی یا اکسترای معتبر برای استارت تأیید نشده است.',
        'on_trip': 'سفر در بررسی قبلی ثبت شده؛ دریافت تازه لازم است.',
    }
    if check == 'eligible':
        return 'واجد شرایط در آخرین بررسی؛ در نوبت بررسی خودکار، با اولویت تاکسی اصلی. اجرای فعلی سرویس تأیید نشده است.'
    return reasons.get(check, 'آمادگی استارت هنوز تأیید نشده؛ دریافت تازه و بررسی سهمیه لازم است.')


def remaining_trip_text(ends_at, now=None):
    now = now or datetime.now(TZ)
    seconds = max(0, math.floor(ends_at / 1000 - now.timestamp()))
    if not seconds:
        return 'زمان ثبت‌شدهٔ سفر به پایان رسیده؛ منتظر دریافت بعدی'
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'زمان باقی‌ماندهٔ ثبت‌شده: {hours:02d}:{minutes:02d}:{seconds:02d}'


def car_card(car, view=None):
    state = car.get('status', 'unavailable')
    rented = bool(car.get('rented'))
    name, plate = esc(car.get('name') or 'تاکسی بدون نام'), esc(car.get('plate') or 'پلاک ثبت نشده')
    label = {'on_trip': 'در سفر', 'idle': 'آمادهٔ حرکت'}.get(state, 'فعلاً در دسترس نیست')
    if state == 'unavailable' and is_turn_block(car):
        label = 'نوبت شما نیست'
    tone = {'on_trip': 'blue', 'idle': 'green'}.get(state, 'gray')
    ends_at = car.get('ends_at')
    started = state == 'on_trip'
    start_label = 'استارت شده · سفر ثبت شده' if started else 'استارت نشده در آخرین دریافت'
    if not started and view and stop_code(view) == 'start_unknown':
        start_label = 'نتیجهٔ استارت نامشخص؛ بررسی دستی لازم است'
    elif not started and view and stop_code(view) == 'manual_review':
        start_label = 'وضعیت استارت تأیید نشده؛ بررسی دستی لازم است'
    if started:
        if timestamp_ms(ends_at):
            timer = remaining_trip_text(ends_at, view.get('now') if view else None)
            detail = f'<p class="trip-timer" dir="auto" data-ends="{int(ends_at)}">{esc(timer)}</p>'
        else:
            detail = '<p class="car-hint">زمان پایان سفر مشخص نیست.</p>'
        detail += '<p class="trip-evidence">طبق آخرین دریافت؛ تایمر به‌تنهایی پایان یا شروع سفر بعدی را تأیید نمی‌کند.</p>'
    else:
        explanation = start_explanation(car, view)
        if is_turn_block(car):
            explanation = 'الان نوبت استفادهٔ شما از این تاکسی نیست. ' + explanation
        detail = f'<p class="car-hint">{esc(explanation)}</p>'
    detail = f'<div class="start-state {"started" if started else "not-started"}"><strong>{start_label}</strong>{detail}</div>'
    message = esc(car.get('block_msg') or 'پیام محدودیتی ثبت نشده است.')
    kind = 'rented' if rented else 'owned'
    return f'''<article class="taxi-card {kind}" data-car-kind="{kind}">
      <div class="taxi-top"><span class="ownership">{'اجاره‌ای' if rented else 'تاکسی شخصی'}</span><span class="state-pill {tone}"><i aria-hidden="true"></i>{label}</span></div>
      <div class="car-illustration">{taxi_art()}</div>
      <h3 dir="auto">{name}</h3><div class="plate" dir="ltr">{plate}</div>{detail}
      <div class="quota-grid"><div><span>سفر عادی باقی‌مانده</span><strong>{trip_count_text(car.get('left'))}</strong></div>
        <div><span>اکسترای این تاکسی</span><strong class="car-bonus-count">{trip_count_text(car.get('bonus'))}</strong></div></div>
      <details class="car-details"><summary>جزئیات سهمیه و پیام سایت <span aria-hidden="true">+</span></summary>
        <p>سهمیهٔ ثبت‌شده (مصرف‌شده از سقف): <span class="quota-count">{trip_count_text(car.get('used'))} از {trip_count_text(car.get('limit'))}</span></p>
        <p dir="auto">{message}</p><p>وضعیت طبق آخرین دریافت است؛ آماده‌بودن، تضمین استارت نیست.</p></details>
      </article>'''


def account_data_section(view):
    if view['sample'] is None:
        return f'''<section id="taxis"><section id="emptyState" class="empty-state" aria-labelledby="emptyTitle">
          <span class="empty-icon">{icon('car')}</span><h2 id="emptyTitle">اطلاعات ذخیره‌شده‌ای از اکانت در دسترس نیست.</h2>
          <p>تا دریافت اطلاعات، موجودی و تاکسی‌ها را به‌صورت صفر نمایش نمی‌دهیم.</p>
          <a class="button secondary" href="#technicalPanel">دیدن علت و راهنما {icon('arrow')}</a></section></section>'''
    sample, cars = view['sample'], view['sample']['cars']
    wallet = sample.get('wallet') if isinstance(sample.get('wallet'), dict) else {}
    balance = wallet.get('usdt')
    balance_html = f'{balance:,.4f}' if finite_number(balance) and balance >= 0 else '—'
    note = 'بر اساس آخرین دریافت از بازی؛ وضعیت فعلی ممکن است تغییر کرده باشد.'
    if view['stale']:
        note = 'این اطلاعات قدیمی است و وضعیت فعلی سایت را تأیید نمی‌کند.'
    if not view['enabled']:
        note = 'اطلاعات ذخیره‌شده نمایش داده می‌شود؛ پایش خارجی خاموش است. ' + note
    owned = sum(not bool(car.get('rented')) for car in cars)
    cards = ''.join(car_card(car, view) for car in sorted(cars, key=lambda car: bool(car.get('rented'))))
    if not cards:
        cards = '<p class="empty-inline">در آخرین دریافت، تاکسی‌ای برای این اکانت ثبت نشده است.</p>'
    filter_empty = '<p class="empty-inline" data-filter-empty hidden>تاکسی‌ای در این دسته ثبت نشده است.</p>' if cars else ''
    if not view['enabled'] or not auto_start_enabled():
        automation_note = 'استارت خودکار خاموش است؛ تاکسی‌های بدون سفر خودکار حرکت نمی‌کنند. برای فعال‌سازی در Railway، SITE_MONITORING_ENABLED=true و AUTO_START_TRIPS_ENABLED=true لازم است؛ تنظیمات ورود و دیتابیس هم باید کامل باشند.'
    else:
        automation_note = 'اولویت استارت: تاکسی اصلی، سپس تاکسی‌های رنتیِ مجاز با سهمیهٔ معتبر. رنتی‌های واجد شرایط می‌توانند هم‌زمان با سفر اصلی در سفر باشند؛ منتظر پایان آن نمی‌مانند. در هر چرخه حداکثر یک استارت؛ فاصلهٔ بررسی موارد آماده حداقل ۵ دقیقه است. فعال‌بودن تنظیم، تضمین اجرای فعلی نیست.'
    return f'''<section id="accountData" aria-labelledby="dataTitle">
      <div class="metrics" aria-label="خلاصهٔ حساب">
        <article class="metric wallet-metric">{icon('wallet')}<span>موجودی کیف پول</span><strong dir="ltr">{balance_html} <small>USDT</small></strong><p>موجودیِ آخرین دریافت</p></article>
        <article class="metric">{icon('car')}<span>همهٔ تاکسی‌ها</span><strong>{len(cars)} <small>تاکسی</small></strong><p>{owned} شخصی · {len(cars) - owned} اجاره‌ای</p></article>
        <article class="metric">{icon('clock')}<span>در حال سفر</span><strong>{sum(car.get('status') == 'on_trip' for car in cars)} <small>تاکسی</small></strong><p>طبق آخرین اطلاعات</p></article>
        <article class="metric">{icon('check')}<span>آمادهٔ حرکت</span><strong>{sum(car.get('status') == 'idle' for car in cars)} <small>تاکسی</small></strong><p>آمادگی گزارش‌شده توسط سایت</p></article>
      </div>
      <div id="taxis" class="section-heading"><div><span class="eyebrow">ناوگان شما</span><h2 id="dataTitle">تاکسی‌های من</h2></div>
        <div class="account-bonus">{icon('bolt')}<span>اکسترای اکانت</span><strong class="account-bonus-count">{trip_count_text(sample.get('bonus_trips'))}</strong>
          <details class="bonus-help"><summary aria-label="توضیح اکسترای اکانت">؟</summary><p>اکسترای اکانت و هر تاکسی جدا گزارش شده‌اند؛ این اعداد را با هم جمع نمی‌کنیم. سهمیهٔ ۱۰ از ۱۰ هم به‌تنهایی پایان سفرهای اضافه نیست.</p></details></div></div>
      <p class="automation-note">{esc(automation_note)} <a href="#technicalPanel">راهنما و علت توقف</a></p>
      <div class="fleet-toolbar"><div class="filters" role="group" aria-label="فیلتر تاکسی‌ها">
        <button type="button" data-filter="all" aria-pressed="true">همه <span>{len(cars)}</span></button>
        <button type="button" data-filter="owned" aria-pressed="false">شخصی <span>{owned}</span></button>
        <button type="button" data-filter="rented" aria-pressed="false">اجاره‌ای <span>{len(cars) - owned}</span></button></div>
        <p class="data-note">{note}</p></div>
      <div class="taxi-grid">{cards}</div>{filter_empty}</section>'''


def rentals_section(view):
    """Inactive new-rental workspace. Reads only the existing account snapshot."""
    sample = view['sample']
    plan = rental_preview(sample['cars'] if sample else None)
    count = trip_count_text(plan['existing'])
    slots = trip_count_text(plan['slots'])
    if plan['existing'] is None:
        note = 'تعداد رنت‌های فعلی نامشخص است؛ برای محاسبهٔ ظرفیت، دریافت معتبر حساب لازم است.'
    elif plan['existing'] >= plan['legacy_limit']:
        note = 'تعداد فعلی به سقف داخلی نسخهٔ قبلی رسیده یا از آن بیشتر است؛ طبق آن قاعده ظرفیت رنت جدید صفر است. رنت‌های موجود حفظ می‌شوند و هیچ لغو یا حذفی انجام نمی‌شود.'
    else:
        note = 'ظرفیت نمایش‌داده‌شده فقط محاسبهٔ سقف داخلی نسخهٔ قبلی است؛ مجوز رنت یا نشانهٔ وجود آگهی مناسب نیست.'
    return f'''<section id="rentals" class="rental-section" aria-labelledby="rentalTitle">
      <div class="section-heading"><div><span class="eyebrow">بازار و اجارهٔ جدید</span><h2 id="rentalTitle">مدیریت رنت</h2></div>
        <span class="rental-disabled">{icon('lock')} فعلاً غیرفعال</span></div>
      <div class="rental-body"><div class="rental-intro"><span class="rental-symbol">{icon('key')}</span><div>
        <h3>بخش رنت آمادهٔ بررسی است، نه اجرا</h3>
        <p id="rentalNotice">هیچ درخواست بازار، اجاره یا پرداختی ارسال نمی‌شود. استارت تاکسی‌های از قبل رنت‌شده، با اجارهٔ جدید فرق دارد و طبق تنظیمات استارت ادامه پیدا می‌کند.</p></div></div>
        <div class="rental-stats">
          <div><span>رنت‌های فعلی در آخرین دریافت</span><strong data-rental-existing>{count}</strong></div>
          <div><span>سقف داخلی نسخهٔ قبلی</span><strong>{plan['legacy_limit']} <small>تاکسی</small></strong></div>
          <div><span>ظرفیت نظری رنت جدید</span><strong data-rental-slots>{slots}</strong></div></div>
        <p class="rental-note">{esc(note)}</p>
        <div class="rental-market"><h3>آگهی‌های بازار <span class="tag">بررسی نشده</span></h3>
          <p>چون این بخش خاموش است، بازار خوانده نشده؛ قیمت، موجودی آگهی، مدت قرارداد، شیفت و سقف سفر مشخص نیست. نبود فهرست به معنی خالی‌بودن بازار نیست.</p></div>
        <details class="rental-guide"><summary>چه چیزی از پروژهٔ قبلی بازیابی شده؟</summary>
          <p>قاعدهٔ شمارش رنت‌های فعلی، سقف داخلی ۲ تاکسی و پیش‌نمایش انتخاب آگهی به ترتیب فهرست بازیابی شده‌اند. این سقف، محدودیت تأییدشدهٔ بازی نیست؛ داشتن ۴ رنت باعث حذف آن‌ها نمی‌شود.</p>
          <p>پیش از فعال‌سازی در آینده، هزینه و شرایط واقعی قرارداد، مجوز API و جلوگیری از اجاره یا پرداخت تکراری باید تأیید شوند. فعلاً هیچ مسیر اجرایی برای اجارهٔ جدید وجود ندارد و تغییر Variables آن را روشن نمی‌کند.</p></details>
        <div class="rental-actions"><button class="button" type="button" disabled aria-describedby="rentalNotice">{icon('lock')} اجارهٔ جدید غیرفعال است</button>
          <a class="text-link" href="#taxis">دیدن تاکسی‌های موجود {icon('arrow')}</a></div>
      </div></section>'''


def disclosure(panel_id, title, badge, body, icon):
    return f'''<details id="{panel_id}" class="history-panel" data-status-panel>
      <summary><span class="panel-icon" aria-hidden="true">{icon}</span><span class="panel-title">{title}</span>
        <span class="tag">{badge}</span><span class="chevron" aria-hidden="true">⌄</span></summary>
      <div class="panel-body">{body}</div></details>'''


def banned_accounts_section():
    records = banned_accounts_info()
    confirmed = sum(row['confirmed'] for row in records)
    rows = []
    for record in records:
        label = 'تعلیق تأییدشده' if record['confirmed'] else 'تأییدنشده (سابقهٔ قدیمی)'
        rows.append(f'''<tr><td dir="auto">{esc(record['username'])}</td><td>{label}</td>
          <td>{esc(record['reason'] or 'دلیل ثبت نشده')}</td><td>{esc(fmt_jalali(record['detected_at']) or 'زمان ثبت نشده')}</td></tr>''')
    body = ('''<p class="muted">سوابق تأییدنشده به‌تنهایی اثبات تعلیق اکانت نیستند.</p>
      <div class="table-scroll"><table><thead><tr><th scope="col">اکانت</th><th scope="col">وضعیت</th>
      <th scope="col">دلیل ثبت‌شده</th><th scope="col">زمان تشخیص (تهران)</th></tr></thead>
      <tbody>''' + ''.join(rows) + '</tbody></table></div>') if rows else '<p class="empty-inline">هیچ سابقهٔ تعلیقی ثبت نشده است.</p>'
    badge = f'{confirmed} اکانت بن‌شده' if records else 'بدون سابقه'
    return disclosure('bannedAccountsPanel', 'سابقهٔ تعلیق اکانت‌ها', badge, body, icon('alert'))


def event_lines():
    """Local persisted evidence only; runtime output lives in Railway logs.

    There is no monitor.log writer. Do not invent a log source or infer worker
    starts/stops from configuration, file mtimes or page refreshes.
    """
    events = []
    view = dashboard_view()
    if view['received']:
        timestamp = view['received'].strftime('%Y-%m-%d %H:%M:%S')
        events.append((timestamp, f'{fmt_jalali(timestamp)} · آخرین دریافت موفق اطلاعات اکانت اصلی'))
    activity = view.get('activity')
    if activity and activity['status'] in ('stopped', 'suspended'):
        timestamp = activity['updated'].strftime('%Y-%m-%d %H:%M:%S')
        events.append((timestamp, f'{fmt_jalali(timestamp)} · {activity["message"]}'))
    for record in banned_accounts_info():
        if record['confirmed']:
            timestamp = record['detected_at']
            events.append((timestamp, f'{fmt_jalali(timestamp) or "زمان ثبت نشده"} · تعلیق ثبت‌شده: {record["username"]}'))
    secrets = [os.environ.get(key) for key in ('PRIMARY_ACCOUNT_PASSWORD', 'DASHBOARD_PASSWORD', 'MONITOR_LOCK_DATABASE_URL')]
    result = []
    for _, line in sorted(events, key=lambda event: event[0], reverse=True)[:30]:
        for secret in secrets:
            if secret:
                line = line.replace(secret, '[REDACTED]')
        result.append(line[:1000])
    return result


def events_section():
    lines = event_lines()
    body = '<div class="event-list">' + ''.join(f'<p dir="auto">{esc(line)}</p>' for line in lines) + '</div>' if lines else '<p class="empty-inline">رویدادی برای نمایش وجود ندارد.</p>'
    body += '<p class="muted">بر اساس اطلاعات ذخیره‌شدهٔ محلی؛ گزارش اجرای سرویس در لاگ‌های Railway است.</p>'
    return disclosure('eventsPanel', 'رویدادها', f'{len(lines)} رویداد ثبت‌شده' if lines else 'بدون رویداد', body, icon('history'))


PAGE_CSS = '''
/* IRANSans is used when installed; no font binary or external CDN is bundled.
   Arabic glyph ranges keep Latin identifiers and code in their familiar fonts. */
@font-face{font-family:"Dashboard Persian";src:local("IRANSans"),local("IRANSansWeb"),local("IRANSans-Regular"),local("IRANSans Regular");font-style:normal;font-weight:400;font-display:swap;unicode-range:U+0600-06FF,U+0750-077F,U+08A0-08FF,U+200C-200F,U+FB50-FDFF,U+FE70-FEFF}
@font-face{font-family:"Dashboard Persian";src:local("IRANSans-Bold"),local("IRANSansWeb-Bold"),local("IRANSans Bold"),local("IRANSansWeb Bold");font-style:normal;font-weight:700;font-display:swap;unicode-range:U+0600-06FF,U+0750-077F,U+08A0-08FF,U+200C-200F,U+FB50-FDFF,U+FE70-FEFF}

.automation-note{padding:14px 17px;border:1px solid var(--info-line);background:var(--info-bg);border-radius:10px;color:var(--blue);font-size:16px;margin:0 0 18px;overflow-wrap:anywhere}
.automation-note a{text-decoration:underline;white-space:nowrap}
.start-state{background:var(--navy);border:1px solid var(--line);border-radius:10px;padding:13px 10px;margin:15px 0;text-align:center}
.start-state>strong{display:block;font-size:16px;color:var(--muted)}
.start-state.started{background:var(--info-bg);border-color:var(--info-line)}
.start-state.started>strong{color:var(--blue)}
.start-state .trip-timer{font-size:17px;font-weight:bold;line-height:1.9;margin:9px 0;color:var(--blue)}
.start-state .car-hint{font-size:15px;line-height:2;margin:8px 0 0;min-height:0}
.trip-evidence{font-size:14px;color:var(--muted);margin:7px 0 0}

:root{color-scheme:dark;--bg:#0b111c;--card:#141f30;--raised:#1b2a40;--ink:#edf2fa;--muted:#adbed5;--line:#30435d;--yellow:#f5c75c;--navy:#0e1726;--green:#a3e6bf;--red:#ffb2bc;--amber:#f3cd83;--blue:#a6c8ff;--success-bg:#152d28;--success-line:#315b4c;--warning-bg:#30291c;--warning-line:#65532e;--danger-bg:#351f2a;--danger-line:#694050;--info-bg:#172d49;--info-line:#34547d;--font-ui:"Dashboard Persian",Tahoma,Arial,sans-serif}


*{box-sizing:border-box}
html{scroll-padding-top:24px}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--font-ui);font-size:17px;line-height:1.85}
a{color:inherit;text-decoration:none}
button{font:inherit}
button,a,summary{-webkit-tap-highlight-color:transparent}
button:focus-visible,a:focus-visible,summary:focus-visible{outline:3px solid var(--blue);outline-offset:4px}
button{cursor:pointer}
[hidden]{display:none!important}
svg{flex-shrink:0}
.icon{width:22px;height:22px;vertical-align:middle}
.app-shell{display:grid;grid-template-columns:232px minmax(0,1fr);min-height:100vh}
.sidebar{background:var(--navy);color:#fff;padding:34px 23px;position:sticky;top:0;height:100vh;display:flex;flex-direction:column}
.brand{display:flex;align-items:center;gap:11px}
.brand-mark{width:43px;height:43px;border-radius:13px;background:var(--yellow);display:grid;place-items:center;color:var(--navy)}
.brand-mark .icon{width:27px;height:27px}
.brand-name{font-size:21px;letter-spacing:-.5px;line-height:1.4;font-weight:bold}
.brand-caption{display:block;font-size:11px;color:#a6b4c9;letter-spacing:1.5px;font-family:Arial,sans-serif;margin-top:4px}
.nav-caption{font-size:15px;color:var(--muted);margin:48px 13px 13px}
.sidebar nav{display:grid;gap:7px}
.nav-link{display:flex;align-items:center;gap:12px;padding:12px 14px;border-radius:10px;color:#afbdd0;font-size:16px}
.nav-link:hover{background:#20314b;color:#fff}
.nav-link[aria-current]{background:#f5bd4518;color:#f5c75c;border:1px solid #f5bd4520}
.sidebar-bottom{margin-top:auto;padding-top:28px}
.private-label{font-size:15px;color:#c1cddd;display:flex;gap:9px;align-items:center}
.private-label .icon{width:16px;height:16px;color:#dfbc69}
.sidebar-bottom p{color:var(--muted);font-size:15px;border-top:1px solid #2a3850;margin-top:20px;padding-top:18px}
.workspace{padding:30px 36px 18px;max-width:1580px;width:100%;min-width:0;margin-inline:auto}
.page-header{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:26px}
.breadcrumb{font-size:15px;color:var(--muted);display:flex;align-items:center;gap:9px}
.breadcrumb span{color:#a8b0bd}
h1{font-size:26px;letter-spacing:-.8px;margin:7px 0 0;line-height:1.65}
.header-actions{display:flex;align-items:center;gap:14px}
.identity{display:flex;align-items:center;gap:9px;border-inline-end:1px solid var(--line);padding-inline-end:18px}
.avatar{display:grid;place-items:center;width:36px;height:36px;border:1px solid var(--warning-line);border-radius:50%;background:var(--warning-bg);color:var(--amber)}
.mobile-identity{display:none}
.identity strong{font-size:15px;font-weight:normal;display:block}
.identity bdi{font:11px Arial,sans-serif;color:var(--muted);display:block;max-width:210px;overflow-wrap:anywhere}
.button{display:inline-flex;align-items:center;justify-content:center;gap:8px;border:1px solid var(--line);border-radius:9px;padding:8px 13px;background:var(--card);color:var(--ink);font-size:15px;white-space:nowrap}
.button:hover{border-color:var(--muted);background:var(--raised)}
.button .icon{width:16px;height:16px}
.eyebrow{display:block;font-size:15px;font-weight:normal;color:var(--muted);margin-bottom:5px}
.status-banner{display:flex;align-items:center;gap:16px;flex-wrap:wrap;border-radius:14px;background:var(--success-bg);border:1px solid var(--success-line);padding:22px 23px;color:var(--green);margin-bottom:18px}
.status-banner.warning{background:var(--warning-bg);border-color:var(--warning-line);color:var(--amber)}
.status-banner.neutral{background:var(--raised);border-color:var(--line);color:var(--muted)}
.status-banner.danger{background:var(--danger-bg);border-color:var(--danger-line);color:var(--red)}
.status-symbol{height:46px;width:46px;background:#ffffff0a;border:1px solid #ffffff18;border-radius:50%;display:grid;place-items:center;flex-shrink:0}
.status-symbol .icon{width:24px;height:24px}
.status-copy{flex:1;min-width:180px}
.status-copy .eyebrow{color:inherit;opacity:.8;margin:0 0 3px}
.status-copy h2{margin:0;font-size:18px;letter-spacing:-.3px}
.status-copy p{margin:5px 0 0;font-size:16px;color:var(--ink);opacity:.8}
.text-link{font-size:15px;display:inline-flex;align-items:center;gap:6px}
.text-link .icon{width:17px;height:17px}
.text-link:hover{text-decoration:underline}
.next-action{flex-basis:100%;display:flex;gap:13px;align-items:baseline;border-top:1px solid currentColor;border-top-color:#8da49935;padding-top:12px;margin-top:0;color:var(--ink)}
.next-action>span{font-size:15px;white-space:nowrap;background:#ffffff0a;padding:2px 8px;border-radius:5px}
.next-action p{margin:0;font-size:16px;line-height:1.9}
.overview{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-bottom:22px}
.overview-card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 19px;min-width:0;position:relative}
.card-icon{float:inline-end;color:var(--muted)}
.card-icon .icon{width:18px;height:18px}
.overview-card strong{display:block;font-size:15px;font-weight:600;line-height:1.8;margin-top:9px}
.overview-card p{font-size:15px;color:var(--muted);margin:7px 0 0;overflow-wrap:anywhere}
.overview-card .received-at{font-variant-numeric:tabular-nums}

.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:31px}
.metric{border-radius:13px;border:1px solid var(--line);background:var(--card);padding:20px 18px;min-width:0;position:relative}
.metric>.icon{position:absolute;inset-inline-end:18px;top:20px;color:var(--muted);width:18px;height:18px}
.metric>span{display:block;color:var(--muted);font-size:15px;padding-inline-end:24px}
.metric strong{font-size:29px;font-weight:600;display:block;margin-top:9px;line-height:1.5;letter-spacing:-.5px;font-variant-numeric:tabular-nums}
.metric small{font-size:15px;font-weight:normal;letter-spacing:0;color:var(--muted)}
.metric p{margin:7px 0 0;font-size:15px;color:var(--muted)}
.wallet-metric{background:var(--navy);border-color:var(--line);color:#fff}
.wallet-metric>span,.wallet-metric p,.wallet-metric small{color:#bac8dd}
.wallet-metric>.icon{color:#e6b94d}
.wallet-metric strong{color:#f8ca63;text-align:right;font-size:28px}
.section-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:17px;scroll-margin-top:20px}
.section-heading h2{font-size:20px;margin:0;letter-spacing:-.4px}
.section-heading .eyebrow{margin-bottom:0}
.account-bonus{background:var(--warning-bg);border:1px solid var(--warning-line);display:flex;align-items:center;gap:9px;border-radius:9px;padding:8px 12px;font-size:15px;color:var(--amber)}
.account-bonus .icon{width:16px;height:16px}
.account-bonus strong{font:600 17px var(--font-ui)}
.bonus-help{position:relative}
.bonus-help summary{list-style:none;display:grid;place-items:center;width:19px;height:19px;cursor:pointer;border:1px solid var(--warning-line);border-radius:50%;font-size:15px}
.bonus-help summary::-webkit-details-marker{display:none}
.bonus-help p{position:absolute;z-index:4;width:270px;inset-inline-end:0;top:20px;padding:15px;background:var(--card);border:1px solid var(--line);box-shadow:0 12px 40px #00000050;border-radius:9px;color:var(--ink);font-size:15px}
.fleet-toolbar{display:flex;align-items:center;justify-content:space-between;gap:15px;flex-wrap:wrap;margin-bottom:17px}
.filters{display:flex;gap:6px;padding:4px;border:1px solid var(--line);background:var(--card);border-radius:9px}
.filters button{display:flex;align-items:center;gap:6px;border:0;background:none;color:var(--muted);border-radius:6px;padding:6px 11px;font-size:15px}
.filters button[aria-pressed=true]{background:var(--yellow);color:var(--navy)}
.filters button span{font:11px Arial,sans-serif;opacity:.65}
.data-note{font-size:15px;color:var(--muted);margin:0;max-width:480px}
.taxi-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:17px}
.taxi-card{border:1px solid var(--line);background:var(--card);border-radius:13px;padding:18px 18px 0;overflow:hidden;min-width:0}
.taxi-card.owned{border-top:3px solid #e8b746;padding-top:16px}
.taxi-top{display:flex;justify-content:space-between;align-items:center;gap:8px}
.ownership{color:var(--muted);font-size:15px}
.state-pill{border-radius:6px;padding:3px 7px;font-size:15px;display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
.state-pill i{width:5px;height:5px;border-radius:50%;background:currentColor}
.state-pill.green{background:var(--success-bg);color:var(--green)}
.state-pill.blue{background:var(--info-bg);color:var(--blue)}
.state-pill.gray{background:var(--raised);color:var(--muted)}
.car-illustration{height:93px;display:flex;align-items:center;justify-content:center;background:radial-gradient(ellipse,#f5c75c14,transparent 70%);margin-top:10px}
.taxi-art{width:200px;max-width:100%;height:87px;color:#e8b747}
.rented .car-illustration{background:radial-gradient(ellipse,#a6c8ff14,transparent 70%)}
.rented .taxi-art{color:#8da5c4}
.taxi-card h3{font:600 15px/1.7 var(--font-ui);letter-spacing:-.1px;margin:3px 0 8px;text-align:center;overflow-wrap:anywhere}
.plate{font:11px/1.7 "Dashboard Persian",monospace;letter-spacing:1.5px;background:var(--raised);border:1px solid var(--line);border-inline-start:4px solid #4a638b;border-radius:4px;padding:2px 10px;width:max-content;max-width:100%;margin:auto;overflow-wrap:anywhere}
.car-hint,.trip-timer{font-size:15px;color:var(--muted);text-align:center;min-height:20px;margin:12px 0}
.trip-timer{color:var(--blue);font-variant-numeric:tabular-nums}
.quota-grid{display:grid;grid-template-columns:1fr 1fr;border:1px solid var(--line);border-radius:9px;margin:12px 0 15px;background:var(--navy)}
.quota-grid>div{padding:9px;text-align:center;min-width:0}
.quota-grid>div+div{border-inline-start:1px solid var(--line)}
.quota-grid span{display:block;font-size:15px;color:var(--muted)}
.quota-grid strong{display:block;font-size:20px;margin-top:3px;line-height:1.5;color:var(--ink);font-variant-numeric:tabular-nums}
.quota-grid .car-bonus-count{color:var(--amber)}
.car-details{border-top:1px solid var(--line);font-size:15px}
.car-details summary{list-style:none;display:flex;justify-content:space-between;align-items:center;cursor:pointer;gap:5px;padding:10px 0;color:var(--muted)}
.car-details summary::-webkit-details-marker{display:none}
.car-details summary>span{font-size:17px;color:var(--muted)}
.car-details p{overflow-wrap:anywhere;color:var(--muted);margin:5px 0 12px}
.quota-count{white-space:nowrap}
.empty-state{background:var(--card);border:1px dashed var(--line);border-radius:14px;text-align:center;padding:43px 24px;margin-top:20px}
.empty-icon{display:grid;place-items:center;width:61px;height:61px;background:var(--warning-bg);color:var(--amber);margin:auto;border-radius:18px}
.empty-icon .icon{width:29px;height:29px}
.empty-state h2{font-size:17px;margin:18px 0 8px}
.empty-state p{font-size:16px;color:var(--muted);margin:0 0 20px}

.history{display:grid;gap:11px;margin-top:34px;scroll-margin-top:20px}
.history-heading{display:flex;align-items:center;gap:9px;margin-bottom:2px}
.history-heading .icon{width:19px;height:19px;color:var(--muted)}
.history-heading h2{font-size:16px;margin:0}
.history-heading p{font-size:15px;color:var(--muted);margin:0 5px 0 0}
.history-panel{background:var(--card);border:1px solid var(--line);border-radius:11px;overflow:hidden;scroll-margin-top:18px}
.history-panel summary{display:flex;align-items:center;gap:11px;padding:15px 18px;cursor:pointer;list-style:none;min-height:60px}
.history-panel summary::-webkit-details-marker{display:none}
.history-panel summary:hover{background:var(--raised)}
.panel-icon{display:flex;align-items:center;color:var(--muted);font-size:17px}
.panel-icon .icon{width:18px;height:18px}
.panel-title{flex:1;font-size:16px}
.tag{font-size:15px;color:var(--muted);padding:2px 8px;background:var(--raised);border-radius:5px;white-space:nowrap}
.chevron{font-size:20px;color:var(--muted);transition:transform .15s}
.history-panel[open] .chevron{transform:rotate(180deg)}
.panel-body{padding:0 20px 20px;font-size:15px;color:var(--ink)}
.technical-list{margin:0}
.technical-list>div{display:grid;grid-template-columns:130px 1fr;padding:11px 0;border-top:1px solid var(--line);gap:15px}
.technical-list dt{color:var(--muted)}
.technical-list dd{margin:0;overflow-wrap:anywhere}
.technical-help{color:var(--muted);font-size:15px;overflow-wrap:anywhere}
.technical-help code{font-size:15px;direction:ltr;unicode-bidi:isolate;background:var(--raised);color:var(--blue);padding:2px 5px;border-radius:4px}
.muted,.empty-inline{color:var(--muted)}
.empty-inline{font-size:16px;padding:12px 0;margin:0}
.table-scroll{overflow:auto;max-height:330px}
table{border-collapse:collapse;width:100%;text-align:right;font-size:15px}
td,th{padding:12px;border-bottom:1px solid var(--line);min-width:100px;overflow-wrap:anywhere}
th{color:var(--muted);background:var(--raised);position:sticky;top:0;font-weight:normal}
.event-list{max-height:290px;overflow:auto}
.event-list p{border-bottom:1px solid var(--line);padding:10px 0;margin:0;white-space:pre-wrap;overflow-wrap:anywhere;font:15px/1.9 var(--font-ui)}
.page-footer{display:flex;justify-content:space-between;gap:15px;padding:24px 0 6px;color:var(--muted);font-size:15px}
.page-footer>span{display:flex;align-items:center;gap:6px}
.page-footer .icon{width:12px;height:12px}
.skip-link{position:absolute;top:-100px;inset-inline-start:10px;padding:10px;background:var(--card);z-index:10}
.skip-link:focus{top:10px}

@media(min-width:1700px){.workspace{padding-inline:48px}
.taxi-grid{grid-template-columns:repeat(4,minmax(0,1fr))}
}

@media(max-width:1250px){.app-shell{grid-template-columns:195px minmax(0,1fr)}
.sidebar{padding-inline:17px}
.workspace{padding:25px}
.identity{display:none}
.mobile-identity{display:block;font:11px/1.7 var(--font-ui);color:var(--muted);margin:6px 0 0;overflow-wrap:anywhere}
.taxi-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
.metric{padding:17px 14px}
.metric strong{font-size:25px}
.metric>.icon{display:none}
.metric>span{padding:0}
.overview-card strong{font-size:16px}
.overview-card{padding:16px}
}

@media(max-width:850px){.app-shell{grid-template-columns:1fr}
.sidebar{position:static;height:auto;padding:17px 23px;display:flex;flex-direction:row;align-items:center;justify-content:space-between;gap:20px}
.brand-caption{display:none}
.brand-name{font-size:18px}
.brand-mark{width:34px;height:34px;border-radius:10px}
.brand-mark .icon{width:22px;height:22px}
.nav-caption,.sidebar-bottom{display:none}
.sidebar nav{display:flex;gap:5px}
.nav-link{padding:8px 10px;font-size:15px;gap:7px}
.nav-link .icon{width:17px;height:17px}
.workspace{padding:23px}
.overview-card p{font-size:15px}
.metrics{margin-bottom:25px}
.overview{gap:10px}
.history-heading p{display:none}
}

@media(max-width:580px){body{font-size:16px}
.sidebar{flex-direction:column;gap:14px;align-items:stretch;padding:16px}
.brand{justify-content:center}
.sidebar nav{justify-content:center}
.nav-link{flex:1;justify-content:center;padding:7px;font-size:15px}
.nav-link .icon{width:15px;height:15px}
.workspace{padding:19px 15px 14px}
.page-header{align-items:center;flex-wrap:wrap;margin-bottom:18px;gap:10px}
.breadcrumb{font-size:15px}
h1{font-size:21px;margin-top:4px}
.header-actions{gap:0}
.button{padding:8px 9px;font-size:15px}
.status-banner{padding:16px;gap:11px}
.status-symbol{width:36px;height:36px}
.status-symbol .icon{width:20px;height:20px}
.status-copy{min-width:130px}
.status-copy h2{font-size:15px}
.status-copy p{font-size:15px}
.status-banner>.text-link{flex-basis:100%;justify-content:flex-end;font-size:15px}
.next-action{gap:8px;align-items:flex-start}
.next-action>span{padding-inline:5px;font-size:15px}
.next-action p{font-size:15px}
.overview{grid-template-columns:1fr;gap:8px;margin-bottom:16px}
.overview-card{display:grid;grid-template-columns:30px 1fr 1fr;align-items:center;gap:0 8px;padding:13px}
.card-icon{grid-row:1/3;float:none}
.overview-card .eyebrow{grid-column:2;grid-row:1;margin:0}
.overview-card strong{grid-column:3;grid-row:1;font-size:16px;text-align:left;margin:0}
.overview-card p{grid-column:2/4;margin:4px 0 0;font-size:15px}
.metrics{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.metric{padding:15px}
.metric strong,.wallet-metric strong{font-size:25px}
.metric>span{font-size:15px}
.metric p{font-size:15px}
.metric small{font-size:15px}
.section-heading{align-items:flex-start;gap:12px}
.section-heading h2{font-size:18px}
.account-bonus{gap:5px;padding:7px;font-size:15px}
.account-bonus .icon{width:13px;height:13px}
.account-bonus strong{font-size:15px}
.bonus-help p{inset-inline-end:-8px;width:235px}
.fleet-toolbar{gap:10px}
.data-note{font-size:15px}
.taxi-grid{grid-template-columns:1fr;gap:13px}
.taxi-card{padding:18px 20px 0}
.taxi-top .ownership{font-size:15px}
.state-pill{font-size:15px}
.car-illustration{height:105px}
.taxi-art{width:225px;height:96px}
.taxi-card h3{font-size:17px}
.car-hint,.trip-timer{font-size:15px}
.quota-grid{margin-top:14px}
.quota-grid span{font-size:15px}
.quota-grid strong{font-size:23px}
.car-details{font-size:15px}
.history{margin-top:27px}
.history-panel summary{padding:14px;gap:9px}
.panel-title{font-size:15px}
.panel-body{padding:0 15px 15px}
.technical-list>div{grid-template-columns:1fr;gap:4px}
.page-footer{flex-direction:column;gap:6px;font-size:15px}
.empty-state{padding:32px 18px}
.empty-state h2{font-size:15px}
.empty-state p{font-size:15px}
}

@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}
}


/* New rentals are an inactive, local-only preview, not a browser action. */
.rental-section{margin-top:34px;scroll-margin-top:20px}
.rental-disabled{display:inline-flex;align-items:center;gap:7px;font-size:14px;color:var(--amber);border:1px solid var(--warning-line);background:var(--warning-bg);padding:5px 10px;border-radius:8px;white-space:nowrap}
.rental-disabled .icon{width:15px;height:15px}
.rental-body{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:22px}
.rental-intro{display:flex;align-items:flex-start;gap:13px}
.rental-symbol{display:grid;place-items:center;flex-shrink:0;background:var(--raised);color:var(--yellow);width:44px;height:44px;border-radius:12px}
.rental-body h3{font-size:17px;margin:0 0 6px}
.rental-body p{font-size:15px;color:var(--muted);line-height:1.9;margin:0}
.rental-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin-top:20px;gap:12px}
.rental-stats>div{border:1px solid var(--line);background:var(--navy);border-radius:10px;padding:14px;min-width:0}
.rental-stats span{font-size:14px;color:var(--muted);display:block}
.rental-stats strong{font-size:24px;display:block;margin-top:5px;color:var(--ink)}
.rental-stats small{font-size:13px;font-weight:normal;color:var(--muted)}
.rental-body .rental-note{margin:14px 0 20px;font-size:14px}
.rental-market{padding:17px;border:1px dashed var(--line);border-radius:10px;background:var(--navy)}
.rental-market h3{display:flex;align-items:center;gap:10px}
.rental-guide{margin-top:16px;border-bottom:1px solid var(--line);padding-bottom:16px}
.rental-guide summary{cursor:pointer;font-size:15px;color:var(--blue)}
.rental-guide p{margin-top:10px}
.rental-actions{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:15px;margin-top:18px}
.rental-actions .button:disabled{cursor:not-allowed;opacity:.8;background:var(--raised);color:var(--muted)}
@media(max-width:850px){.sidebar{flex-wrap:wrap}.sidebar nav{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));width:100%}.nav-link{justify-content:center}}
@media(max-width:580px){.sidebar nav{grid-template-columns:repeat(2,minmax(0,1fr))}.rental-stats{grid-template-columns:1fr}.rental-body{padding:16px}.rental-intro{flex-direction:column}.rental-disabled{font-size:12px}.rental-section .section-heading h2{font-size:18px}}
'''

PAGE_JS = '''
  function updateCountdowns() {
    document.querySelectorAll('.trip-timer[data-ends]').forEach(function (element) {
      var remaining = Math.floor((Number(element.dataset.ends) - Date.now()) / 1000);
      if (!Number.isFinite(remaining)) return;
      if (remaining <= 0) {
        element.textContent = 'زمان ثبت‌شدهٔ سفر به پایان رسیده؛ منتظر دریافت بعدی';
      } else {
        var hours = Math.floor(remaining / 3600);
        var minutes = Math.floor((remaining % 3600) / 60);
        element.textContent = 'زمان باقی‌ماندهٔ ثبت‌شده: ' + String(hours).padStart(2, '0') + ':' + String(minutes).padStart(2, '0') + ':' + String(remaining % 60).padStart(2, '0');
      }
    });
  }
  updateCountdowns();
  setInterval(updateCountdowns, 1000);
  // Keep each account panel's disclosure state across automatic page refreshes.
  document.querySelectorAll('details[data-status-panel]').forEach(function (panel) {
    var key = 'status_panel_open_' + panel.id;
    try { panel.open = localStorage.getItem(key) === '1'; } catch (e) {}
    panel.addEventListener('toggle', function () {
      try { localStorage.setItem(key, panel.open ? '1' : '0'); } catch (e) {}
    });
  });
  // End disclosure persistence.
  // Filters affect only the already-rendered local cards; no network requests.
  (function () {
    var buttons = document.querySelectorAll('[data-filter]');
    if (!buttons.length) return;
    var cards = document.querySelectorAll('[data-car-kind]');
    var empty = document.querySelectorAll('[data-filter-empty]');
    function applyFilter(value) {
      if (['all', 'owned', 'rented'].indexOf(value) === -1) value = 'all';
      var visible = 0;
      cards.forEach(function (card) {
        card.hidden = value !== 'all' && card.dataset.carKind !== value;
        if (!card.hidden) visible += 1;
      });
      buttons.forEach(function (button) { button.setAttribute('aria-pressed', String(button.dataset.filter === value)); });
      empty.forEach(function (element) { element.hidden = visible !== 0; });
      try { localStorage.setItem('taxi_filter', value); } catch (e) {}
    }
    var saved = 'all';
    try { saved = localStorage.getItem('taxi_filter') || 'all'; } catch (e) {}
    applyFilter(saved);
    buttons.forEach(function (button) {
      button.addEventListener('click', function () { applyFilter(button.dataset.filter); });
    });
  })();
  document.querySelectorAll('a[href="#technicalPanel"]').forEach(function (link) {
    link.addEventListener('click', function () {
      document.querySelectorAll('#technicalPanel').forEach(function (panel) { panel.open = true; });
    });
  });
'''


def render():
    view = dashboard_view()
    identity = esc(view['user'] or 'حساب اصلی تنظیم نشده')
    return f'''<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1"><meta name="theme-color" content="#0b111c"><meta name="color-scheme" content="dark"><meta http-equiv="refresh" content="30">
      <title>Crypto Taxi — تاکسی‌های من</title><style>{PAGE_CSS}</style></head><body>
      <a class="skip-link" href="#mainContent">رفتن به محتوای اصلی</a><div class="app-shell">
      <aside class="sidebar" aria-label="منوی اصلی"><a class="brand" href="#overview" aria-label="کریپتو تاکسی، نمای کلی">
        <span class="brand-mark">{icon('car')}</span><div><span class="brand-name" lang="en" dir="ltr">Crypto Taxi</span><span class="brand-caption">PERSONAL GARAGE</span></div></a>
        <span class="nav-caption">فضای کاری شما</span><nav>
          <a class="nav-link" href="#overview" aria-current="page">{icon('grid')} نمای کلی</a>
          <a class="nav-link" href="#taxis">{icon('car')} تاکسی‌های من</a>
          <a class="nav-link" href="#rentals">{icon('key')} مدیریت رنت</a>
          <a class="nav-link" href="#history">{icon('history')} سوابق و راهنما</a></nav>
        <div class="sidebar-bottom"><span class="private-label">{icon('lock')} حالت امن · تک‌اکانت</span>
          <p>کلیم و رنت فعلاً غیرفعال‌اند.<br>این صفحه فقط اطلاعات محلی را نمایش می‌دهد.</p></div></aside>
      <main id="mainContent" class="workspace"><header class="page-header"><div>
        <div class="breadcrumb">مدیریت حساب <span>/</span> نمای کلی</div><h1>همه‌چیز، در یک نگاه.</h1>
        <p class="mobile-identity"><bdi>{identity}</bdi></p></div>
        <div class="header-actions"><div class="identity"><span class="avatar">{icon('lock')}</span>
          <div><strong>حساب اصلی شما</strong><bdi>{identity}</bdi></div></div>
          <a class="button" href="/">{icon('refresh')} بازخوانی صفحه</a></div></header>
        {status_section(view)}{account_data_section(view)}{rentals_section(view)}
        <section id="history" class="history" aria-label="سوابق و راهنما"><div class="history-heading">{icon('history')}<h2>سوابق و راهنما</h2><p>جزئیات بیشتر، فقط وقتی به آن‌ها نیاز دارید.</p></div>
          {technical_section(view)}{banned_accounts_section()}{events_section()}</section>
        <footer class="page-footer"><span>{icon('refresh')} بازخوانی صفحه هر ۳۰ ثانیه · بدون درخواست مستقیم به بازی</span><span>زمان‌ها به وقت تهران · کلیم و رنت غیرفعال</span></footer>
      </main></div><script>{PAGE_JS}</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        user = os.environ.get('DASHBOARD_USER', '')
        password = os.environ.get('DASHBOARD_PASSWORD', '')
        if not user or len(password) < 16:
            self.send_json({'ok': False, 'error': 'Configure dashboard access in Railway Variables'}, status=503)
            return False
        try:
            scheme, encoded = self.headers.get('Authorization', '').split(' ', 1)
            if scheme.lower() != 'basic':
                raise ValueError()
            supplied_user, supplied_password = base64.b64decode(encoded, validate=True).decode('utf-8').split(':', 1)
            valid_user = hmac.compare_digest(supplied_user.encode(), user.encode())
            valid_password = hmac.compare_digest(supplied_password.encode(), password.encode())
            if valid_user and valid_password:
                return True
        except (ValueError, UnicodeError, binascii.Error):
            pass
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="Private dashboard", charset="UTF-8"')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self.end_headers()
        return False

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/healthz':
            self.send_json({'ok': True})
            return
        if not self.authorized():
            return
        try:
            if path == '/count':
                self.send_json({'ok': True, 'count': len(active_accounts_info())})
            elif path in ('/', '/index.html'):
                body = render().encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json({'ok': False, 'error': 'Not found'}, status=404)
        except Exception:
            self.send_json({'ok': False, 'error': 'Unable to read dashboard state'}, status=500)

    def do_POST(self):
        if self.authorized():
            self.send_json({'ok': False, 'error': 'Read-only dashboard'}, status=405)

    def log_message(self, fmt, *args):
        pass


if __name__ == '__main__':
    initialize_data()
    HTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
