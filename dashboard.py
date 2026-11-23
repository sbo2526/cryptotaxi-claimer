#!/usr/bin/env python3
"""Protected read-only dashboard: configuration, real data freshness and history.

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

from ban_detection import is_ban_error, is_confirmed_ban_record
from runtime_config import (DATA_DIR, primary_username, monitoring_enabled, initialize_data,
                            ALLOWED_PRIMARY_ACCOUNT)

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
    banned = {entry["username"] for entry in banned_accounts_info() if entry["confirmed"]}
    return [] if user in banned else [{"username": user}]


def finite_number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


STOP_MESSAGES = {
    'needs_login': 'نشست ورود پذیرفته نشد؛ بررسی و ورود دستی لازم است. ورود مجدد خودکار انجام نمی‌شود.',
    'access_denied': 'دسترسی رد شده است؛ پایش تا بررسی دستی متوقف می‌ماند.',
    'rate_limited': 'محدودیت درخواست یا انتظار تکراری؛ پایش برای بررسی دستی متوقف شده است.',
    'manual_stop': 'توقف دستی ثبت شده است.',
    'manual_review': 'خطا یا مشکل ایمنی ثبت شده است؛ بررسی دستی لازم است.',
    'suspended': 'تعلیق ثبت شده است؛ پایش نباید مجدداً فعال شود.',
}
SCHEDULE_REASONS = {
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
    suspended = any(row['username'] == user and row['confirmed'] for row in banned_accounts_info())
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
    elif activity and activity['status'] in ('stopped', 'suspended'):
        label, tone, note = 'پایش متوقف شده است', 'warning', activity['message']
    elif activity and activity['status'] == 'cooldown':
        label, tone, note = 'مهلت انتظار ثبت شده است', 'warning', 'بررسی بعدی باید محدودیت سرور و بودجهٔ مشترک درخواست را رعایت کند.'
    elif received is None:
        label, tone, note = 'در انتظار اولین دریافت', 'warning', 'هنوز دریافت موفقی برای اکانت اصلی ثبت نشده است.'
    elif stale:
        label, tone, note = 'اطلاعات قدیمی', 'warning', 'بیش از ۱۰ دقیقه از آخرین دریافت موفق گذشته است.'
    else:
        label, tone, note = 'آخرین دریافت تازه است', 'success', 'این وضعیت تازگی داده را نشان می‌دهد، نه تضمین اجرای فعلی پایش.'
    if enabled and not suspended and activity and activity['next_check']:
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
    elif activity and activity['status'] in ('stopped', 'suspended'):
        empty_note = activity['message']
    else:
        empty_note = 'دریافت موفقی ثبت نشده است؛ هیچ عملیات استارت، اجاره یا دریافت جایزه انجام نمی‌شود.'
    return {'user': user, 'enabled': enabled, 'suspended': suspended, 'sample': sample,
            'received': received, 'stale': stale, 'label': label, 'tone': tone, 'note': note,
            'empty_note': empty_note, 'activity': activity}


def status_section(view):
    account_label = 'تعلیق‌شده' if view['suspended'] else ('تنظیم‌شده' if view['user'] else 'تنظیم نشده')
    account_note = esc(view['user']) if view['user'] else (
        f'تنها اکانت مجاز: {esc(ALLOWED_PRIMARY_ACCOUNT)}. مقدار PRIMARY_ACCOUNT باید همین باشد؛ این کار پایش را روشن نمی‌کند.')
    received = fmt_jalali(view['received'].strftime('%Y-%m-%d %H:%M:%S')) if view['received'] else 'هنوز دریافت نشده'
    return f'''<section class="overview" aria-label="وضعیت داشبورد">
      <article class="overview-card"><span class="eyebrow">وضعیت پایش</span>
        <strong class="status-text {view['tone']}">{esc(view['label'])}</strong><p>{esc(view['note'])}</p></article>
      <article class="overview-card"><span class="eyebrow">اکانت اصلی</span>
        <strong>{account_label}</strong><p dir="auto">{account_note}</p></article>
      <article class="overview-card"><span class="eyebrow">آخرین دریافت موفق</span>
        <strong class="received-at">{esc(received)}</strong><p>به وقت تهران · زمان دریافت اطلاعات، نه زمان بازشدن صفحه</p></article>
    </section>'''


def trip_count_text(value):
    """Reported nonnegative integer counts only; never invent a zero/total."""
    if finite_number(value) and value >= 0 and value % 1 == 0:
        return esc(int(value))
    return 'نامشخص'


def car_card(car):
    status = car.get('status', 'unavailable')
    name = esc(car.get('name') or 'تاکسی بدون نام')
    plate = esc(car.get('plate') or 'پلاک ثبت نشده')
    ownership = 'اجاره‌ای' if car.get('rented') else 'شخصی'
    label = {'on_trip': 'در سفر (طبق آخرین دریافت)', 'idle': 'آمادهٔ حرکت'}.get(status, 'در دسترس نیست')
    ends_at = car.get('ends_at')
    if status == 'on_trip' and finite_number(ends_at) and ends_at > 0:
        detail = f'<p class="trip-timer" data-ends="{int(ends_at)}">زمان باقی‌مانده بر اساس اطلاعات ثبت‌شده</p>'
    elif status == 'idle':
        detail = '<p>استارت خودکار غیرفعال است.</p>'
    else:
        detail = f'<p>{esc(car.get("block_msg") or "جزئیات بیشتری از محدودیت ثبت نشده است.")}</p>'
    quota = f'''<p class="muted">سهمیهٔ ثبت‌شده (مصرف‌شده از سقف):
      <span class="quota-count">{trip_count_text(car.get('used'))} از {trip_count_text(car.get('limit'))}</span></p>
      <p class="muted">سفر اضافهٔ این تاکسی (مقدار ثبت‌شده):
      <span class="car-bonus-count">{trip_count_text(car.get('bonus'))}</span></p>'''
    return f'''<article class="taxi-card"><div class="taxi-heading"><span aria-hidden="true">🚕</span>
      <h3>{name}</h3><span class="tag">{ownership}</span></div><p class="plate" dir="auto">{plate}</p>
      <span class="taxi-state">{label}</span>{detail}{quota}</article>'''


def account_data_section(view):
    if view['sample'] is None:
        return f'''<section id="emptyState" class="empty-state" aria-labelledby="emptyTitle">
          <span class="empty-icon" aria-hidden="true">⏸</span><h2 id="emptyTitle">هنوز اطلاعاتی از اکانت دریافت نشده است.</h2>
          <p>{esc(view['empty_note'])}</p></section>'''
    sample = view['sample']
    cars = sample['cars']
    wallet = sample.get('wallet') if isinstance(sample.get('wallet'), dict) else {}
    balance = wallet.get('usdt')
    balance_html = f'{balance:,.4f}' if finite_number(balance) and balance >= 0 else '—'
    note = 'این اطلاعات قدیمی است و وضعیت فعلی سایت را تأیید نمی‌کند.' if view['stale'] else 'اطلاعات بر اساس آخرین دریافت موفق است؛ هیچ عملیات خودکاری انجام نمی‌شود.'
    if not view['enabled']:
        note = 'اطلاعات ذخیره‌شده نمایش داده می‌شود؛ پایش خارجی خاموش است. ' + note
    cards = ''.join(car_card(car) for car in cars) or '<p class="empty-inline">در آخرین دریافت، تاکسی‌ای برای این اکانت ثبت نشده است.</p>'
    return f'''<section id="accountData" aria-labelledby="dataTitle">
      <div class="section-heading"><h2 id="dataTitle">اطلاعات اکانت اصلی</h2><span class="tag">فقط‌خواندنی</span></div>
      <p class="data-note">{note}</p><div class="metrics">
        <article class="metric"><span>موجودی USDT</span><strong dir="ltr">{balance_html}</strong></article>
        <article class="metric"><span>تاکسی‌های ثبت‌شده</span><strong>{len(cars)}</strong></article>
        <article class="metric"><span>در حال سفر</span><strong>{sum(car.get('status') == 'on_trip' for car in cars)}</strong></article>
        <article class="metric"><span>آمادهٔ حرکت</span><strong>{sum(car.get('status') == 'idle' for car in cars)}</strong></article>
      </div><p class="data-note">سفر اضافهٔ اکانت (مقدار ثبت‌شده):
        <strong class="account-bonus-count">{trip_count_text(sample.get('bonus_trips'))}</strong><br>
        سهمیه و سفرهای اضافه جدا هستند؛ رسیدن مصرف به سقف سهمیه، به‌تنهایی پایان همهٔ سفرها را ثابت نمی‌کند.
        مقدار اضافهٔ اکانت با مقدار هر تاکسی جمع زده نشده است؛ وضعیت حرکت طبق آخرین پاسخ سایت است.</p>
      <div class="taxi-grid">{cards}</div></section>'''


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
    return disclosure('bannedAccountsPanel', 'سابقهٔ تعلیق اکانت‌ها', badge, body, '🚫')


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
    return disclosure('eventsPanel', 'رویدادها', f'{len(lines)} رویداد ثبت‌شده' if lines else 'بدون رویداد', body, '📋')


PAGE_CSS = '''
:root{color-scheme:dark;--bg:#080f1d;--card:#101b2d;--line:#223149;--text:#e5edf8;--muted:#9aaac0;--gold:#f7c85a;--blue:#83b9ff;--green:#6ed4aa;--red:#ffa5ab}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top right,#152342 0,transparent 55%),var(--bg);color:var(--text);font-family:Tahoma,Arial,sans-serif;line-height:1.85;min-height:100vh}
.wrap{width:min(1180px,calc(100% - 40px));margin:auto}header{border-bottom:1px solid var(--line);padding:30px 0 24px}.header-row{display:flex;align-items:center;justify-content:space-between;gap:20px}.brand{display:flex;align-items:center;gap:14px}.brand-icon{font-size:32px;background:#263023;border:1px solid #786332;border-radius:16px;padding:5px 13px}h1{font-size:23px;margin:0;line-height:1.5}header p{color:var(--muted);margin:4px 0 0;font-size:13px}.safe-badge,.tag{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:999px;padding:4px 12px;font-size:12px;white-space:nowrap}.safe-badge{color:var(--gold);border-color:#6c572d;background:#302919}
main{padding:28px 0 10px}.overview{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-bottom:26px}.overview-card{background:linear-gradient(140deg,#142137,var(--card));border:1px solid var(--line);border-radius:18px;padding:22px;min-width:0}.eyebrow{display:block;color:var(--muted);font-size:12px;margin-bottom:10px}.overview-card strong{display:block;font-size:19px;font-weight:600}.overview-card p{font-size:12px;color:var(--muted);margin:10px 0 0;overflow-wrap:anywhere}.overview-card .received-at{font-size:16px;font-variant-numeric:tabular-nums}.neutral{color:var(--muted)}.warning{color:var(--gold)}.danger{color:var(--red)}.success{color:var(--green)}
.empty-state{border:1px dashed #3b4c66;border-radius:22px;text-align:center;padding:52px 24px;background:#0f192b;margin-bottom:28px}.empty-icon{display:inline-flex;align-items:center;justify-content:center;width:64px;height:64px;border-radius:20px;background:#23314a;color:var(--gold);font-size:30px}.empty-state h2{font-size:20px;margin:20px 0 10px}.empty-state p{color:var(--muted);font-size:14px;margin:0}.section-heading{display:flex;align-items:center;gap:14px;margin-top:4px}.section-heading h2{font-size:19px;margin:0}.data-note{color:var(--muted);font-size:13px;margin:10px 0 18px}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.metric{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:19px}.metric span{font-size:12px;color:var(--muted);display:block}.metric strong{display:block;font-size:26px;color:var(--gold);margin-top:8px;font-variant-numeric:tabular-nums}
.taxi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(245px,1fr));gap:16px;margin:18px 0 28px}.taxi-card{border:1px solid var(--line);border-radius:17px;background:#0e192b;padding:20px;min-width:0}.taxi-heading{display:flex;gap:9px;align-items:center}.taxi-heading h3{font-size:15px;flex:1;margin:0;overflow-wrap:anywhere}.taxi-heading>span:first-child{font-size:23px}.plate{color:var(--muted);font-size:12px}.taxi-state{font-size:12px;color:var(--blue)}.taxi-card p{font-size:12px;color:var(--muted)}.taxi-card .trip-timer{color:var(--gold);font-variant-numeric:tabular-nums}
.history{display:grid;gap:14px;margin-top:28px}.history-panel{border:1px solid var(--line);border-radius:17px;background:#0e192a;overflow:hidden}.history-panel summary{display:flex;align-items:center;gap:12px;padding:18px 22px;cursor:pointer;list-style:none;user-select:none;min-height:66px}.history-panel summary::-webkit-details-marker{display:none}.history-panel summary:hover{background:#152239}.history-panel summary:focus-visible{outline:2px solid var(--blue);outline-offset:-4px;border-radius:16px}.panel-icon{font-size:21px}.panel-title{font-size:15px;flex:1}.chevron{font-size:23px;color:var(--muted);transition:transform .15s}.history-panel[open] .chevron{transform:rotate(180deg)}.panel-body{padding:0 22px 22px;font-size:13px}.muted,.empty-inline{color:var(--muted)}.empty-inline{font-size:13px;margin:0;padding:8px 0}.table-scroll{overflow:auto;max-height:340px}table{border-collapse:collapse;width:100%;text-align:right;font-size:12px}th{color:var(--muted);position:sticky;top:0;background:#101d31}td,th{padding:12px;border-bottom:1px solid var(--line);min-width:100px}td{overflow-wrap:anywhere}.event-list{max-height:320px;overflow:auto;font-family:monospace}.event-list p{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;padding:8px;border-bottom:1px solid var(--line)}footer{text-align:center;padding:25px 0 30px;color:var(--muted);font-size:11px}
@media(max-width:850px){.overview{grid-template-columns:1fr}.overview-card{padding:18px}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.overview-card strong{font-size:17px}}@media(max-width:480px){.wrap{width:calc(100% - 28px)}.header-row{align-items:flex-start;flex-direction:column;gap:14px}h1{font-size:20px}.empty-state{padding:32px 16px}.empty-state h2{font-size:17px}.history-panel summary{padding:16px 14px;flex-wrap:wrap;gap:8px}.panel-body{padding:0 14px 16px}.panel-title{min-width:120px}.tag{font-size:11px;padding:3px 9px}.metric{padding:15px}}@media(prefers-reduced-motion:reduce){.chevron{transition:none}}
'''

PAGE_JS = '''
  function updateCountdowns() {
    document.querySelectorAll('.trip-timer[data-ends]').forEach(function (element) {
      var remaining = Math.floor((Number(element.dataset.ends) - Date.now()) / 1000);
      if (!Number.isFinite(remaining)) return;
      if (remaining <= 0) {
        element.textContent = 'زمان ثبت‌شدهٔ سفر به پایان رسیده؛ منتظر دریافت بعدی';
      } else {
        var minutes = Math.floor(remaining / 60);
        element.textContent = 'برآورد زمان باقی‌مانده: ' + minutes + ':' + String(remaining % 60).padStart(2, '0');
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
'''


def render():
    view = dashboard_view()
    return f'''<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="30">
      <title>Crypto Taxi — داشبورد فقط‌خواندنی</title><style>{PAGE_CSS}</style></head><body>
      <header><div class="wrap header-row"><div class="brand"><span class="brand-icon" aria-hidden="true">🚕</span>
        <div><h1>Crypto Taxi <span lang="en">Dashboard</span></h1><p>وضعیت اکانت اصلی و اطلاعات ثبت‌شده</p></div></div>
        <span class="safe-badge">حالت امن · فقط‌خواندنی</span></div></header>
      <main class="wrap">{status_section(view)}{account_data_section(view)}
        <section class="history" aria-label="سوابق و رویدادها">{banned_accounts_section()}{events_section()}</section>
      </main><footer class="wrap">فقط‌خواندنی · عملیات خودکار غیرفعال · بازخوانی صفحه هر ۳۰ ثانیه</footer>
      <script>{PAGE_JS}</script></body></html>'''


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
