#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The Crypto Taxi — live dashboard (professional UI)

A tiny stdlib HTTP server that renders a polished live HTML dashboard from
the claimer's files (status.json, accounts.json, claimer.log).

- GET  /        -> dashboard HTML (auto-refresh every 30s)
- POST /clear   -> clear all error counters/history from this moment on

Usage:
  python3 dashboard.py [port]        # default 8080
"""

from runtime_config import ACCOUNT_CREATION_ENABLED
from ban_detection import is_ban_error, is_confirmed_ban_record

import html
import json
import os
import re
import sys
import time
from datetime import date, datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

# Fix the program clock to Tehran timezone (Asia/Tehran, UTC+03:30)
TZ = ZoneInfo("Asia/Tehran")

HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(HERE, "accounts.json")
STATUS_FILE = os.path.join(HERE, "status.json")
EARNINGS_FILE = os.path.join(HERE, "earnings.json")
CREATOR_LOG = os.path.join(HERE, "creator.log")
CREATOR_STATUS_FILE = os.path.join(HERE, "creator_status.json")
BANNED_ACCOUNTS_FILE = os.path.join(HERE, "banned_accounts.json")
REFERRAL_STATUS_FILE = os.path.join(HERE, "referral_faker_status.json")
LOG_FILE = os.path.join(HERE, "claimer.log")

PORT = 8080
if len(sys.argv) > 1:
    try:
        PORT = int(sys.argv[1])
    except ValueError:
        pass

# marker appended to claimer.log when errors are cleared; the dashboard only
# shows ERROR/WARNING lines that come AFTER this marker
CLEAR_MARKER = "==== ERRS-CLEARED ===="

# inline SVG taxi icon (no external resources needed)
TAXI_SVG = """<svg class="taxi-svg" viewBox="0 0 64 40" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <rect x="2" y="20" width="60" height="15" rx="6" fill="{body}"/>
  <path d="M10 20 L16 9 h24 l6 11 z" fill="{body2}"/>
  <rect x="18" y="11" width="12" height="8" rx="1.6" fill="#cfe6ff"/>
  <rect x="34" y="11" width="12" height="8" rx="1.6" fill="#cfe6ff"/>
  <circle cx="16" cy="35" r="6.5" fill="#0d1424"/>
  <circle cx="48" cy="35" r="6.5" fill="#0d1424"/>
  <circle cx="16" cy="35" r="2.6" fill="#7c88a3"/>
  <circle cx="48" cy="35" r="2.6" fill="#7c88a3"/>
  <rect x="2" y="25" width="12" height="3.2" rx="1.6" fill="#ffd24f"/>
</svg>"""


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def tail_lines(path, n=30):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except Exception:
        return []


def esc(s):
    return html.escape(str(s))


# --------------------------------------------------------------------------- #
# Jalali (Persian solar) calendar conversion — for dashboard display
# --------------------------------------------------------------------------- #
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
    '2026-11-21 15:29:24' -> '28 آذر 1405، 15:29:24'."""
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


def explain(msg):
    """Map a raw error message to (level, why) for human-readable display."""
    if "session expired (401)" in msg or "re-logging in" in msg:
        return "warning", "جلسه لاگین از جانب سرور منقضی شد — claimer لاگین مجدد خودکار انجام داد (رفع‌شده)"
    if "FAILED" in msg:
        return "error", "سرور پاسخ صحیحی به درخواست نداد — ممکن است موقتی باشد"
    if "Connection reset" in msg or "reset by peer" in msg or "Connection aborted" in msg:
        return "warning", "اتصال به سرور در میانه قطع شد — معمولاً به دلیل flakiness سرور یا rate limit؛ claimer attempt مجدد کرد"
    if "429" in msg or "rate limited" in msg:
        return "error", "سرور درخواست‌های زیاد را محدود کرد (rate limit)"
    if "400" in msg:
        return "warning", "سرور درخواست را رد کرد — معمولاً به دلیل اینکه در حال حاضر یک سفر دیگر در جریان است (یکی در زمان)"
    if "401" in msg:
        return "warning", "جلسه منقضی شد — لاگین مجدد انجام شد"
    return "error", "خطای سرور — توضیح در پیام خطأ"


def level_badge(level):
    if level == "warning":
        return "<span class='lvl lvl--warn'>⚠ خطای موقت / رفع‌شده</span>"
    return "<span class='lvl lvl--err'>⛔ خطای اصلی</span>"


def clear_errors():
    """Reset error counters + recent errors for all accounts, and mark the log
    so historical errors stop being shown from this moment on."""
    status = load_json(STATUS_FILE, {})
    cleared = 0
    for user, a in status.items():
        if user == "_meta" or not isinstance(a, dict):
            continue
        if a.get("errors_today") or a.get("recent_errors"):
            cleared += 1
        a["errors_today"] = 0
        a["recent_errors"] = []
    save_json(STATUS_FILE, status)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n{CLEAR_MARKER} errors cleared at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    except Exception:
        pass
    return cleared


# --------------------------------------------------------------------------- #
# account management (dashboard panel: add / delete / edit accounts)
# --------------------------------------------------------------------------- #
def recent_errors_from_log():
    """Extract recent ERROR/WARNING lines per account from claimer.log.
    Only lines after the last ERRS-CLEARED marker are considered."""
    per_account = {}
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-400:]
    except Exception:
        return per_account
    marker_idx = -1
    for i, l in enumerate(lines):
        if CLEAR_MARKER in l:
            marker_idx = i
    if marker_idx >= 0:
        lines = lines[marker_idx + 1:]
    pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),?\d* \[(ERROR|WARNING)\] \[([^\]]+)\] (.+)$")
    for line in reversed(lines):
        m = pattern.search(line.strip())
        if not m:
            continue
        ts, level, user, msg = m.groups()
        if user not in per_account:
            per_account[user] = []
        if len(per_account[user]) >= 15:
            break
        lvl, why = explain(msg)
        per_account[user].append({"t": ts, "e": msg, "why": why, "level": lvl})
    return per_account


def trips_bar(used, limit, bonus=0):
    """Inline-styled progress bar for trips done / total (limit + bonus)."""
    if not limit:
        return ""
    total = limit + (bonus or 0)
    pct = max(0, min(100, round(used / total * 100))) if total else 0
    cls = "trips-bar" + (" full" if pct >= 100 else "")
    bonus_chip = (
        f"<span class='chip chip--bonus'>🎁 {bonus} اکسترا</span>"
        if bonus else ""
    )
    return (
        f"<div class='trips'><div class='trips__txt'>سفرهای رفته: "
        f"<b>{used}</b> از <b>{total}</b> {bonus_chip}</div>"
        f"<div class='{cls}'><div class='trips__fill' style='width:{pct}%'></div></div></div>"
    )


def car_card(car):
    status = car.get("status", "unavailable")
    name = esc(car.get("name", "تاکسی"))
    plate = esc(car.get("plate") or "")
    icon_color = {"on_trip": "#2ecc71", "idle": "#4f8cff", "unavailable": "#8b97ad"}.get(status, "#8b97ad")
    svg = TAXI_SVG.format(body=icon_color, body2="#2c3a55")
    rented_badge = "<span class='pill pill--rent'>🔑 رنت</span>" if car.get("rented") else "<span class='pill pill--own'>مخصوص</span>"
    status_pill = {
        "on_trip": "<span class='pill pill--trip'>🟢 در سفر</span>",
        "idle": "<span class='pill pill--ready'>🔵 آماده</span>",
    }.get(status, "<span class='pill pill--off'>⚪ خارج از خدمت</span>")

    if status == "on_trip":
        ends_at = car.get("ends_at")
        started_at = car.get("started_at", 0)
        countdown = (
            f"<div class='cd' data-ends=\"{int(ends_at)}\" data-started=\"{int(started_at)}\">"
            f"<div class='cd__row'><span class='cd__label'>⏳ مونده تا پایان سفر</span>"
            f"<b class='cd__val'>—</b></div>"
            f"<div class='cd__track'><div class='cd__fill'></div></div></div>"
            if ends_at else
            "<div class='cd'><div class='cd__row'><span class='cd__label'>⏳ مونده تا پایان سفر</span><b>—</b></div></div>"
        )
    else:
        countdown = (
            "<div class='cd cd--idle'><div class='cd__row'><span class='cd__label'>✅ آماده برای استارت خودکار</span></div></div>"
            if status == "idle" else
            "<div class='cd cd--idle'><div class='cd__row'><span class='cd__label'>❌ kuota تمام شده / خارج از خدمت</span></div></div>"
        )

    return f"""
      <div class="car-card car-card--{esc(status)}">
        <div class="car-card__top">{svg}
          <div class="car-card__name-wrap"><span class="car-card__name">{name}</span>{rented_badge}{status_pill}</div>
        </div>
        <div class="car-card__plate">{plate or '—'}</div>
        {trips_bar(car.get("used", 0), car.get("limit", 0), car.get("bonus", 0))}
        {countdown}
      </div>"""


# --------------------------------------------------------------------------- #
# Mission Center (missions + achievements, from /api/missions)
# --------------------------------------------------------------------------- #
MISSION_DEFS = {
    "d_trips10": ("🚕 ۱۰ سفر انجام بده", "هر روز ۱۰ سفر با تاکسی‌ها انجام بده"),
    "d_rented10": ("🚕 ۱۰ سفر با تاکسی رنت", "۱۰ سفر با تاکسی‌های اجاره‌شده انجام بده"),
    "d_deposit5": ("💰 ۵ USDT واریز کن", "در روز ۵ USDT واریز شود"),
    "d_deposit50": ("💰 ۵۰ USDT واریز کن", "در روز ۵۰ USDT واریز شود"),
    "a_buy1": ("🚗 خرید اولین تاکسی", ""),
    "a_buy5": ("🚗 خرید ۵ تاکسی", ""),
    "a_buy10": ("🚗 خرید ۱۰ تاکسی", ""),
    "a_buy25": ("🚗 خرید ۲۵ تاکسی", ""),
    "a_trips300": ("🛣 ۳۰۰ سفر کل", ""),
    "a_trips800": ("🛣 ۸۰۰ سفر کل", ""),
    "a_trips1500": ("🛣 ۱۵۰۰ سفر کل", ""),
    "a_rented300": ("🚕 ۳۰۰ سفر رنت کل", ""),
    "a_rented500": ("🚕 ۵۰۰ سفر رنت کل", ""),
    "a_rented1500": ("🚕 ۱۵۰۰ سفر رنت کل", ""),
    "a_guild": ("🛡 عضویت در گیلدم", ""),
    "a_rentout3": ("🔑 اجاره‌دادن ۳ تاکسی", ""),
    "a_rentout5": ("🔑 اجاره‌دادن ۵ تاکسی", ""),
    "a_refs10": ("👥 ۱۰ رفررال تایید‌شده", ""),
    "a_refs30": ("👥 ۳۰ رفررال تایید‌شده", ""),
    "a_refs100": ("👥 ۱۰۰ رفررال تایید‌شده", ""),
    "a_refbuy5": ("🚗 ۵ رفررال که تاکسی خریدند", ""),
    "a_refbuy10": ("🚗 ۱۰ رفررال که تاکسی خریدند", ""),
    "a_refbuy30": ("🚗 ۳۰ رفررال که تاکسی خریدند", ""),
    "a_refbuy100": ("🚗 ۱۰۰ رفررال که تاکسی خریدند", ""),
    "a_video": ("🎬 ویدیو بکش و بفرست", "ویدیو از بازی در یوتیوب با حداقل ۵۰ بازدید"),
    "a_tg_channel": ("📲 به کانال تلگرام بپیوندی", ""),
}


def reward_chip(reward):
    if not reward:
        return "<span class='chip chip--none'>بدون جایزه</span>"
    cur = reward.get("cur", "")
    amt = reward.get("amount", 0)
    if cur == "usdt":
        return f"<span class='chip chip--usdt'>💰 {amt} USDT</span>"
    if cur == "efectivo":
        return f"<span class='chip chip--cash'>💵 {amt} Efectivo</span>"
    return f"<span class='chip'>{esc(cur)} {amt}</span>"


def mission_row(m):
    mid = m.get("id", "?")
    title, desc = MISSION_DEFS.get(mid, (mid, ""))
    kind = m.get("kind")
    manual = kind in ("video", "telegram")
    value = m.get("value", 0)
    goal = m.get("goal", 0)
    done = m.get("done", False)
    claimed = m.get("claimed", False)
    pct = max(0, min(100, round(value / goal * 100))) if goal else 0
    if claimed:
        status = "<span class='mstatus mstatus--claimed'>✅ کلیم شد</span>"
        cls = "mission--claimed"
    elif done and m.get("reward") and not claimed:
        status = "<span class='mstatus mstatus--claim'>🎁 قابل کلیم</span>"
        cls = "mission--claimable"
    elif done:
        status = "<span class='mstatus mstatus--done'>✅ تمومش شد</span>"
        cls = "mission--done"
    else:
        status = f"<span class='mstatus'>{pct}%</span>"
        cls = "mission--progress"
    note = "<div class='mission__note'>🔧 دستی — باید خودت انجام بدی (auto-claim ندارد)</div>" if manual else ""
    return f"""
      <div class="mission {cls}">
        <div class="mission__top">
          <div class="mission__title">{esc(title)}</div>
          {status}
        </div>
        {note}
        <div class="mission__reward">{reward_chip(m.get("reward"))}</div>
        <div class="mission__bar"><div class="mission__fill" style="width:{pct}%"></div></div>
        <div class="mission__progress">{value} از {goal}</div>
      </div>"""


def mission_center(missions, user=""):
    if not missions:
        return ""
    daily = missions.get("daily", [])
    ach = missions.get("achievements", [])
    all_m = daily + ach
    claimable = sum(1 for m in all_m
                    if m.get("done") and not m.get("claimed") and m.get("reward")
                    and m.get("kind") not in ("video", "telegram"))
    claimed = sum(1 for m in all_m if m.get("claimed"))
    resets_at = missions.get("resetsAt", 0)
    reset_txt = fmt_jalali(datetime.fromtimestamp(resets_at / 1000, tz=TZ).strftime("%Y-%m-%d %H:%M:%S")) if resets_at else "—"
    daily_html = "".join(mission_row(m) for m in daily)
    ach_html = "".join(mission_row(m) for m in ach)
    # collapsible: default closed to keep the dashboard compact.
    # state persists across the 30s auto-refresh via localStorage.
    safe_key = "".join(ch if ch.isalnum() else "_" for ch in (user or "acc"))
    return f"""
      <div class="mission-sec" id="mc-{safe_key}">
        <div class="mission-sec__head mission-sec__head--toggle" role="button" tabindex="0">
          <span class="mc-toggle">🔽</span>
          <span>🏆 Mission Center</span>
          <span class="mission-sec__count">{claimable} قابل کلیم</span>
          <span class="mission-sec__claimed">· {claimed} کلیم‌شده</span>
        </div>
        <div class="mission-sec__body">
          <div class="mission-sec__sub">Missiones روزانه reset می‌شود در: <b>{esc(reset_txt)}</b></div>
          <div class="mission-sec__group">📅 missionهای روزانه</div>
          <div class="mission-list">{daily_html}</div>
          <div class="mission-sec__group">🏆 دستاوردها</div>
          <div class="mission-list">{ach_html}</div>
        </div>
      </div>"""


def referral_center(referrals, user=""):
    if not referrals:
        return ""
    pending = referrals.get("pending") or {}
    pending_trips = pending.get("trips", 0) or 0
    stats = referrals.get("stats", {})
    reward_car = referrals.get("rewardCar") or {}
    bonuses = referrals.get("bonuses", [])
    deposit_pct = referrals.get("depositPct", 0)
    ref_code = referrals.get("refCode", "")
    names = pending.get("names", [])
    safe_key = "".join(ch if ch.isalnum() else "_" for ch in (user or "acc"))
    claimable = pending_trips > 0
    if claimable:
        status_txt = f"<span class='mc-toggle'>🎁 {pending_trips} سفر قابل کلیم</span>"
    elif pending_trips == 0:
        status_txt = "<span class='mstatus mstatus--claimed'>✅ هیچ سفر pendingی نیست</span>"
    else:
        status_txt = "<span class='mstatus'>—</span>"
    # bonus trips already in cars (bonuses list)
    bonus_left = sum(b.get("left", 0) for b in bonuses)
    car_name = reward_car.get("name", "—")
    return f"""
      <div class="mission-sec" id="rf-{safe_key}">
        <div class="mission-sec__head mission-sec__head--toggle" role="button" tabindex="0">
          <span class="mc-toggle">🔽</span>
          <span>👥 Referral Center</span>
          {status_txt}
        </div>
        <div class="mission-sec__body">
          <div class="mission-sec__sub">کد رفرال: <b>{esc(ref_code) or '—'}</b> · از هر واریز رفرال {deposit_pct}% 💰</div>
          <div class="mission-sec__group">📊 تفلید رفرال</div>
          <div class="mission-list">
            <div class="mission mission--progress">
              <div class="mission__top">
                <div class="mission__title">👥 {stats.get('total', 0)} رفرال</div>
                <span class="mstatus">{stats.get('completed', 0)} کامل · {stats.get('inProgress', 0)} در حال</span>
              </div>
              <div class="mission__reward"><span class="chip">🏆 {stats.get('tripsEarned', 0)} سفر اکراست دریافت</span></div>
            </div>
            <div class="mission mission--progress">
              <div class="mission__top">
                <div class="mission__title">🚕 تاکسی دریافت bonus</div>
                <span class="mstatus">{esc(car_name)}</span>
              </div>
              <div class="mission__reward"><span class="chip chip--usdt">🎁 {bonus_left} سفر اکراست باقی</span></div>
            </div>
          </div>
          {('<div class="mission__note">🔑 سفرهای pending به طور خودکار کلیم می‌شود و به تاکسی اصلی اضافه می‌شود.</div>' if claimable else '')}
        </div>
      </div>"""


def revenue_section(earnings):
    """Revenue Tracker section: today / week / month / all-time USDT + trips."""
    daily = earnings.get("daily", {})
    weekly = earnings.get("weekly", {})
    monthly = earnings.get("monthly", {})
    all_time = earnings.get("all_time", {})
    today = time.strftime("%Y-%m-%d")
    week_key = time.strftime("%Y-W%W")
    month_key = time.strftime("%Y-%m")

    today_data = daily.get(today, {})
    week_data = weekly.get(week_key, {})
    month_data = monthly.get(month_key, {})

    # Calculate week total from daily data
    week_days = week_data.get("days", {})
    week_usdt = sum(d.get("usdt", 0) for d in week_days.values())
    week_trips = sum(d.get("trips", 0) for d in week_days.values())
    month_usdt = month_data.get("usdt", 0) or 0.0
    month_trips = month_data.get("trips", 0) or 0

    # Build daily chart (last 7 days)
    days_data = []
    for i in range(6, -1, -1):
        d = time.strftime("%Y-%m-%d", time.gmtime(time.time() - i * 86400))
        dd = daily.get(d, {})
        days_data.append({"date": fmt_jalali(d), "usdt": dd.get("usdt", 0), "trips": dd.get("trips", 0)})
    max_usdt = max(d.get("usdt", 0) for d in days_data) or 0.001

    chart_bars = "".join(
        f"<div class='rev-bar-wrap'>"
        f"<div class='rev-bar-label'>{esc(d['date'][:2])}</div>"
        f"<div class='rev-bar-track'>"
        f"<div class='rev-bar-fill' style='height:{max(4,round(d['usdt']/max_usdt*100))}%'></div>"
        f"</div>"
        f"<div class='rev-bar-val'>{d['usdt']:.4f}</div>"
        f"</div>"
        for d in days_data
    )

    # Per-account breakdown today
    acc_breakdown = today_data.get("accounts", {})
    acc_rows = ""
    if acc_breakdown:
        for user, ad in sorted(acc_breakdown.items(), key=lambda x: x[1].get("usdt", 0), reverse=True):
            acc_rows += (
                f"<tr>"
                f"<td>{esc(user[:12] + '…' if len(user) > 12 else user)}</td>"
                f"<td class='num'>{ad.get('usdt', 0):.4f}</td>"
                f"<td class='num'>{ad.get('trips', 0)}</td>"
                f"</tr>"
            )
    acc_table = (
        f"<table class='rev-table'><thead><tr><th>اکانت</th><th>USDT</th><th>سفرها</th></tr></thead><tbody>{acc_rows}</tbody></table>"
        if acc_rows else "<div class='rev-no'>هنوز داده‌ای نیست</div>"
    )

    return f""""
  <div class="mission-sec" id="rv">
    <div class="mission-sec__head mission-sec__head--toggle" role="button" tabindex="0">
      <span class="mc-toggle">🔽</span>
      <span>📊 Revenue Tracker</span>
      <span class="rev__updated">آپدیت: {esc(all_time.get('updated', '—'))}</span>
    </div>
    <div class="mission-sec__body">
    <section class="rev">
    <div class="rev__grid">
      <div class="rev__cards">
        <div class="rev__card rev__card--today">
          <div class="rev__card-label">💰 امروز</div>
          <div class="rev__card-val">{today_data.get('usdt', 0):.4f} <small>USDT</small></div>
          <div class="rev__card-sub">{today_data.get('trips', 0)} سفر</div>
        </div>
        <div class="rev__card rev__card--week">
          <div class="rev__card-label">📅 این هفته</div>
          <div class="rev__card-val">{week_usdt:.4f} <small>USDT</small></div>
          <div class="rev__card-sub">{week_trips} سفر · {len(week_days)} روز</div>
        </div>
        <div class="rev__card rev__card--month">
          <div class="rev__card-label">🗓 این ماه</div>
          <div class="rev__card-val">{month_usdt:.4f} <small>USDT</small></div>
          <div class="rev__card-sub">{month_data.get('trips', 0)} سفر</div>
        </div>
        <div class="rev__card rev__card--all">
          <div class="rev__card-label">🏆 کل درآمد</div>
          <div class="rev__card-val">{all_time.get('total_usdt', 0):.4f} <small>USDT</small></div>
          <div class="rev__card-sub">{all_time.get('total_trips', 0)} سفر کل</div>
        </div>
      </div>
      <div class="rev__chart">
        <div class="rev__chart-label">📈 نمودار ۷ روز اخیر (USDT)</div>
        <div class="rev-bars">{chart_bars}</div>
      </div>
      <div class="rev__detail">
        <div class="rev__detail-label">📋 جزئیات امروز (هر اکانت)</div>
        {acc_table}
      </div>
    </div>
    </section>
    </div>"""


def account_features(a):
    """Wallet / bonus trips / lifetime stats / today's earnings / daily average."""
    w = a.get("wallet", {}) or {}
    bonus = a.get("bonus_trips", 0)
    st = a.get("stats", {}) or {}
    usdt = w.get("usdt", 0)
    cash = w.get("cash", 0)
    trips = st.get("trips", 0)
    earned = st.get("usdt_earned", 0)
    # today's earnings and daily-average from history
    earned_today = a.get("earnings_today", 0) or 0
    hist = a.get("earnings_history", []) or []
    if hist:
        total_hist = sum(h.get("earned", 0) for h in hist)
        avg_daily = total_hist / len(hist)
    else:
        total_hist = 0.0
        avg_daily = 0.0
    # show at least today's earning if no history yet
    if not hist:
        avg_daily = earned_today
    return f"""
      <div class="acc__feats">
        <div class="feat feat--gold"><span>💰 USDT</span><b>{usdt}</b></div>
        <div class="feat feat--green"><span>💵 Cash</span><b>{cash}</b></div>
        <div class="feat feat--blue"><span>🎁 bonus trips</span><b>{bonus}</b></div>
        <div class="feat"><span>📊 سفرهای کل</span><b>{trips}</b></div>
        <div class="feat"><span>📈 درآمد USDT</span><b>{earned}</b></div>
        <div class="feat feat--earn"><span>⚡ درآمد امروز</span><b>{earned_today}</b></div>
        <div class="feat feat--avg"><span>📅 میانگین روزی</span><b>{avg_daily}</b></div>
      </div>"""


def creator_status_info():
    """Read creator_status.json (live, written every second by account_creator.py)
    and fall back to creator.log parsing if the status file is unavailable."""
    target = 100
    current = len(active_accounts_info())
    remaining = max(0, target - current)
    pct = (current / target * 100) if target > 0 else 0

    if not ACCOUNT_CREATION_ENABLED:
        return {
            "enabled": False, "running": False, "done": False,
            "current": current, "target": target, "remaining": remaining,
            "pct": pct, "phase": "disabled",
            "message": "ساخت خودکار اکانت‌ها غیرفعال است",
            "next_cycle_ts": None, "countdown_seconds": None,
        }

    # --- Primary source: creator_status.json (written live by account_creator) ---
    status_data = load_json(CREATOR_STATUS_FILE, {})

    if status_data:
        running = status_data.get("status") == "running"
        done = status_data.get("status") in ("shutdown",) or current >= target
        phase = status_data.get("phase", "")
        phase_label = status_data.get("phase_label", "—")
        next_cycle_ts = status_data.get("next_cycle_at")
        next_cycle_persian = status_data.get("next_cycle_persian", "—")
        countdown_seconds = status_data.get("countdown_seconds")
        last_registered = status_data.get("last_created_email") or "尚无"
        last_registered_time = status_data.get("last_created_persian", "尚无")
        last_action = phase_label if phase_label else "尚无 فعالیت"
        last_action_time = status_data.get("updated_at_persian", "尚无")
        last_cycle_info = status_data.get("cycle_label") or next_cycle_persian
        errors = status_data.get("errors", 0)
        last_error = status_data.get("last_error")
        total_cycles = status_data.get("total_cycles", 0)
        total_success = status_data.get("total_success", 0)
        total_failed = status_data.get("total_failed", 0)
        interval = status_data.get("interval", 300)
        cycle_number = status_data.get("cycle_number", 0)
        started_at = status_data.get("started_at_persian", "尚无")
        elapsed = status_data.get("cycle_elapsed")

        # Compute a live countdown from next_cycle_ts if present
        live_countdown = None
        if next_cycle_ts:
            live_countdown = int(max(0, next_cycle_ts - time.time()))

        if done:
            message = "✅ ساخت اکانت کامل شد — به هدف رسید"
        elif running:
            if phase == "countdown" and countdown_seconds is not None:
                message = f"⏳ منتظر دور بعدی — {countdown_seconds} ثانیه مانده"
            elif phase == "cycle_starting":
                message = "▶ دور ساخت اکانت شروع شد"
            elif phase in ("preparing_device", "creating_email", "registering", "saving_account"):
                message = f"🔄 در حال ساخت اکانت: {phase_label}"
            elif phase == "cooling_down":
                message = "⚠️ فاصله بین دورها"
            else:
                message = "🟢 در حال ساخت اکانت‌ها"
        else:
            message = "🟡 منتظرِ نوبت بعدی"

        return {
            "running": running,
            "done": done,
            "current": current,
            "target": target,
            "remaining": remaining,
            "pct": pct,
            "last_registered": last_registered,
            "last_registered_time": last_registered_time,
            "last_action": last_action,
            "last_action_time": last_action_time,
            "last_cycle_info": last_cycle_info,
            "next_cycle_info": next_cycle_persian,
            "next_cycle_ts": next_cycle_ts,
            "errors": errors,
            "last_error": last_error,
            "message": message,
            "phase": phase,
            "phase_label": phase_label,
            "countdown_seconds": countdown_seconds if countdown_seconds is not None else live_countdown,
            "next_cycle_persian": next_cycle_persian,
            "total_cycles": total_cycles,
            "total_success": total_success,
            "total_failed": total_failed,
            "interval": interval,
            "cycle_number": cycle_number,
            "started_at": started_at,
            "cycle_elapsed": elapsed,
            "source": "status_file",
        }

    # --- Fallback: parse creator.log (old behavior) ---
    last_registered = None
    last_registered_time = None
    last_action = None
    last_action_time = None
    last_cycle_info = None
    next_cycle_info = None
    next_cycle_ts = None  # absolute epoch timestamp for next cycle start
    errors_count = 0
    last_error_info = None

    try:
        with open(CREATOR_LOG, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-80:]
    except Exception:
        lines = []

    import re

    for line in lines:
        line_stripped = line.strip()
        # Extract the full timestamp prefix: "2026-11-22 14:12:34,567"
        ts_part = line_stripped[:23] if len(line_stripped) >= 23 else line_stripped

        # Parse the timestamp from the log line
        ts_epoch = None
        m_ts = re.match(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})", ts_part)
        if m_ts:
            ts_epoch = datetime(*map(int, m_ts.groups()), tzinfo=TZ).timestamp()

        # Check for successful registration — match on "Registered:" (robust to emoji encoding)
        if "Registered:" in line_stripped:
            # Extract name: text after "Registered:" up to first space
            marker_idx = line_stripped.find("Registered:")
            after = line_stripped[marker_idx + len("Registered:"):].strip()
            name = after.split(" ")[0] if after else None
            # Extract email from parentheses if present
            email = None
            if "(" in after and ")" in after:
                email = after[after.find("(") + 1:after.find(")")]
            last_registered = name or email
            last_registered_time = ts_part
            last_action = f"✅ ساخت اکانت: {last_registered}"
            last_action_time = ts_part
        # Check for added to accounts.json
        elif "Added to accounts.json:" in line_stripped:
            marker_idx = line_stripped.find("Added to accounts.json:")
            after = line_stripped[marker_idx + len("Added to accounts.json:"):].strip()
            last_action = f"📝 اضافه شد به لیست: {after}"
            last_action_time = ts_part
        # Check for cycle start
        elif "Cycle:" in line_stripped and "/100" in line_stripped:
            last_cycle_info = line_stripped.split("] ", 1)[-1] if "] " in line_stripped else line_stripped
            last_action = last_cycle_info
            last_action_time = ts_part
        # Check for cycle complete
        elif "Cycle complete:" in line_stripped:
            last_cycle_info = line_stripped.split("] ", 1)[-1] if "] " in line_stripped else line_stripped
            last_action = f"✅ دورهٔ کامل شد: {last_cycle_info}"
            last_action_time = ts_part
        # Check for sleeping / next cycle — extract the sleep duration
        elif "Sleeping" in line_stripped and "s until next cycle" in line_stripped:
            next_cycle_info = line_stripped.split("] ", 1)[-1] if "] " in line_stripped else line_stripped
            # Extract sleep seconds: "Sleeping 310s until next cycle"
            m_sleep = re.search(r"Sleeping\s+(\d+)s\s+until next cycle", line_stripped)
            if m_sleep and ts_epoch is not None:
                sleep_seconds = int(m_sleep.group(1))
                next_cycle_ts = ts_epoch + sleep_seconds
            last_action = next_cycle_info
            last_action_time = ts_part
        # Check for email creation failure
        elif "Email creation failed" in line_stripped:
            errors_count += 1
            last_error_info = line_stripped
        # Check for registration failure
        elif "Registration failed" in line_stripped:
            errors_count += 1
            last_error_info = line_stripped
        # Check for creator started
        elif "Account Creator started" in line_stripped:
            last_action = "🚀 creator شروع شد"
            last_action_time = ts_part
        # Check for target reached / shutdown
        elif "Target reached" in line_stripped or "shutting down" in line_stripped:
            return {
                "running": False,
                "done": True,
                "current": current,
                "target": target,
                "remaining": 0,
                "pct": 100.0,
                "last_registered": last_registered,
                "last_registered_time": last_registered_time,
                "last_action": last_action,
                "last_action_time": last_action_time,
                "last_cycle_info": last_cycle_info,
                "next_cycle_info": next_cycle_info,
                "next_cycle_ts": next_cycle_ts,
                "errors": errors_count,
                "last_error": last_error_info,
                "message": "✅ ساخت اکانت کامل شد — به هدف رسید",
                "phase": "done",
                "phase_label": "✅ تکمیل شد",
                "countdown_seconds": 0,
                "next_cycle_persian": "—",
                "total_cycles": 0,
                "total_success": 0,
                "total_failed": 0,
                "interval": 300,
                "cycle_number": 0,
                "started_at": "尚无",
                "cycle_elapsed": 0,
                "source": "log_fallback",
            }

    # Determine if creator is running by checking if it's a recent log entry
    now_ts = time.time()
    try:
        last_ts = 0
        for line in lines:
            ts_str = line.strip().split(" ")[0]
            try:
                import re
                m = re.match(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})", ts_str)
                if m:
                    last_ts = datetime(*map(int, m.groups()), tzinfo=TZ).timestamp()
            except Exception:
                pass
        age_minutes = (now_ts - last_ts) / 60 if last_ts > 0 else 0
        running = age_minutes < 15
    except Exception:
        running = True

    if current >= target:
        return {
            "running": False,
            "done": True,
            "current": current,
            "target": target,
            "remaining": 0,
            "pct": 100.0,
            "last_registered": last_registered,
            "last_registered_time": last_registered_time,
            "last_action": last_action,
            "last_action_time": last_action_time,
            "last_cycle_info": last_cycle_info,
            "next_cycle_info": next_cycle_info,
            "next_cycle_ts": next_cycle_ts,
            "errors": errors_count,
            "last_error": last_error_info,
            "message": "✅ ساخت اکانت کامل شد — به هدف رسید",
            "phase": "done",
            "phase_label": "✅ تکمیل شد",
            "countdown_seconds": 0,
            "next_cycle_persian": "—",
            "total_cycles": 0,
            "total_success": 0,
            "total_failed": 0,
            "interval": 300,
            "cycle_number": 0,
            "started_at": "尚无",
            "cycle_elapsed": 0,
            "source": "log_fallback",
        }

    return {
        "running": running,
        "done": False,
        "current": current,
        "target": target,
        "remaining": remaining,
        "pct": pct,
        "last_registered": last_registered,
        "last_registered_time": last_registered_time,
        "last_action": last_action,
        "last_action_time": last_action_time,
        "last_cycle_info": last_cycle_info,
        "next_cycle_info": next_cycle_info,
        "next_cycle_ts": next_cycle_ts,
        "errors": errors_count,
        "last_error": last_error_info,
        "message": "🟢 در حال ساخت اکانت‌ها" if running else "🟡 منتظرِ نوبت بعدی",
        "phase": "",
        "phase_label": "",
        "countdown_seconds": None,
        "next_cycle_persian": "—",
        "total_cycles": 0,
        "total_success": 0,
        "total_failed": 0,
        "interval": 300,
        "cycle_number": 0,
        "started_at": "尚无",
        "cycle_elapsed": 0,
        "source": "log_fallback",
    }


def creator_status_section(info):
    """HTML section for the account-creator status panel with live countdown,
    phase tracking, Jalali (Solar) time, and cycle statistics."""
    if info.get("enabled") is False:
        current = esc(str(info.get("current", 0)))
        return f"""  <details id="accountCreatorPanel" class="creator-status status-panel" data-status-panel>
    <summary class="creator-status__head status-panel__summary">
      <span class="creator-status__icon" aria-hidden="true">🏭</span>
      <span class="creator-status__title">ساخت خودکار اکانت‌ها</span>
      <span class="creator-status__badge creator-status__badge--idle">⛔ غیرفعال</span>
      <span class="status-panel__chevron" aria-hidden="true">⌄</span>
    </summary>
    <div class="status-panel__body">
    <div class="creator-stat creator-stat--blue">
      <small>تعداد اکانت‌های موجود</small><b>{current}</b>
    </div>
    <p id="creatorStatusMsg" style="color:var(--muted);margin:16px 0 0;line-height:1.9">
      ساخت خودکار اکانت‌ها غیرفعال است. اکانت جدیدی به‌صورت خودکار ساخته نمی‌شود.
    </p>
    </div>
  </details>"""

    running = info.get("running", False)
    done = info.get("done", False)
    current = info.get("current", 0)
    target = info.get("target", 100)
    remaining = info.get("remaining", 0)
    pct = info.get("pct", 0)
    last_reg = info.get("last_registered") or "尚无"
    last_reg_time = info.get("last_registered_time") or "尚无"
    last_action = info.get("last_action") or "尚无 فعالیت"
    last_action_time = info.get("last_action_time") or "尚无"
    last_cycle = info.get("last_cycle_info") or "尚无"
    next_cycle = info.get("next_cycle_info") or "尚无"
    next_cycle_ts = info.get("next_cycle_ts")
    errors = info.get("errors", 0)
    last_error = info.get("last_error")
    message = info.get("message", "")
    phase = info.get("phase", "")
    phase_label = info.get("phase_label", "")
    countdown_seconds = info.get("countdown_seconds")
    total_cycles = info.get("total_cycles", 0)
    total_success = info.get("total_success", 0)
    total_failed = info.get("total_failed", 0)
    interval = info.get("interval", 300)
    cycle_number = info.get("cycle_number", 0)
    started_at = info.get("started_at", "尚无")
    cycle_elapsed = info.get("cycle_elapsed")

    if done:
        badge = "<span class='creator-status__badge creator-status__badge--done'>✅ تکمیل شد</span>"
    elif running:
        badge = "<span class='creator-status__badge creator-status__badge--active'><span class='dot' style='width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite'></span> در حال اجرا</span>"
    else:
        badge = "<span class='creator-status__badge creator-status__badge--idle'>🟡 منتظر</span>"

    pct_str = f"{pct:.1f}%"
    bar_width = f"{pct:.1f}%"

    action_html = f"<b>{esc(last_action)}</b> — {esc(last_action_time)}"
    reg_html = f"<b>{esc(last_reg)}</b> ({esc(last_reg_time)})" if last_reg != "尚无" else "尚无 اکانت ساخته‌شده"

    phase_badge_html = ""
    if phase and phase_label and phase != "done":
        phase_badge_html = f'<div style="margin-bottom:8px"><span class="creator-phase-badge">{esc(phase_label)}</span></div>'

    countdown_html = "—"
    if next_cycle_ts:
        initial_count = countdown_seconds if countdown_seconds is not None else "—"
        countdown_html = f'<span id="creatorCountdown" data-target="{next_cycle_ts}">{esc(str(initial_count))}</span>'
    elif countdown_seconds is not None:
        countdown_html = f'<span id="creatorCountdown" data-target="">{esc(str(countdown_seconds))}</span>'
    else:
        countdown_html = '<span id="creatorCountdown" data-target="">—</span>'

    cycle_info_html = ""
    if total_cycles > 0 or cycle_number > 0:
        cycle_info_html = f"دور #{cycle_number} | کل دورها: {total_cycles} | موفق: {total_success} | ناموفق: {total_failed}"

    interval_min = interval // 60 if interval else 5
    interval_sec = interval % 60
    interval_str = f"{interval_min} دقیقه و {interval_sec} ثانیه" if interval_sec else f"{interval_min} دقیقه"

    return f"""  <details id="accountCreatorPanel" class="creator-status status-panel" data-status-panel>
    <summary class="creator-status__head status-panel__summary">
      <span class="creator-status__icon" aria-hidden="true">🏭</span>
      <span class="creator-status__title">ساخت خودکار اکانت‌ها</span>
      {badge}
      <span class="status-panel__chevron" aria-hidden="true">⌄</span>
    </summary>
    <div class="status-panel__body">
    {phase_badge_html}
    <div class="creator-stats">
      <div class="creator-stat creator-stat--green"><small> ساخته شده </small><b>{current}</b></div>
      <div class="creator-stat creator-stat--gold"><small> هدف نهایی </small><b>{target}</b></div>
      <div class="creator-stat creator-stat--blue"><small> باقی مانده </small><b>{remaining}</b></div>
      <div class="creator-stat {'creator-stat--red' if errors else 'creator-stat--green'}"><small> خطاها </small><b>{errors}</b></div>
    </div>
    <div class="creator-progress">
      <div class="creator-progress__bar">
        <div class="creator-progress__fill" id="creatorFill" style="width:{bar_width}"></div>
      </div>
      <div class="creator-progress__txt">
        <span><b>{current}</b> از <b>{target}</b> اکانت</span>
        <span>{pct_str} تکمیل شد</span>
      </div>
      <div class="creator-progress__timer">
        <div style="display:flex;flex-direction:column;gap:8px;width:100%">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span class="creator-progress__timer-label"> ⏰ تا شروع چرخه بعدی </span>
            <span class="creator-progress__timer-val" id="creatorCountdownWrap" style="font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--blue)">{countdown_html}</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span class="creator-progress__timer-label"> زمان شروع دور بعدی </span>
            <span class="creator-progress__timer-val" style="font-size:13px;text-align:right;color:var(--muted)">{esc(next_cycle)}</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span class="creator-progress__timer-label"> آخرین اکانت ساخته‌شده </span>
            <span class="creator-progress__timer-val" id="creatorRegInfo" style="font-size:14px;text-align:right">{reg_html}</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span class="creator-progress__timer-label"> فاز فعلی / عمل انجام‌شده </span>
            <span class="creator-progress__timer-val" id="creatorActionInfo" style="font-size:14px;text-align:right">{action_html}</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span class="creator-progress__timer-label"> وضعیت فعلی </span>
            <span class="creator-progress__timer-val" id="creatorStatusMsg" style="font-size:14px;text-align:right">{esc(message)}</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span class="creator-progress__timer-label"> اطلاعات چرخه </span>
            <span class="creator-progress__timer-val" id="creatorCycleInfo" style="font-size:12px;text-align:right;color:var(--muted)">{esc(cycle_info_html)}</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span class="creator-progress__timer-label"> interval </span>
            <span class="creator-progress__timer-val" style="font-size:12px;text-align:right;color:var(--muted)">{esc(interval_str)}</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span class="creator-progress__timer-label"> زمان راه‌اندازی </span>
            <span class="creator-progress__timer-val" style="font-size:12px;text-align:right;color:var(--muted)">{esc(started_at)}</span>
          </div>
        </div>
      </div>
    </div>
    </div>
  </details>"""


def referral_faker_status_section():
    """HTML section showing referral faker status (dormant/active)."""
    if not ACCOUNT_CREATION_ENABLED:
        return ""
    data = load_json(REFERRAL_STATUS_FILE, {})
    if not data:
        return ""

    status = data.get("status", "unknown")
    main_account = data.get("main_account", "—")
    trigger_count = data.get("trigger_count", 100)
    current_count = data.get("current_referral_count", 0)
    triggered = data.get("triggered", False)
    phase_label = data.get("phase_label", "—")
    total_fakes = data.get("total_fakes_created", 0)
    staging_day = data.get("staging_day", 0)
    staging_days = data.get("staging_days", 8)
    updated = data.get("updated_at_persian", "尚无")

    if status == "dormant":
        badge = "<span style='padding:6px 14px;border-radius:999px;font-size:12.5px;font-weight:600;background:rgba(251,146,60,.12);color:var(--orange);border:1px solid rgba(251,146,60,.3)'>⏳ در انتظار فعال‌سازی</span>"
        bar_pct = min(100, (current_count / trigger_count * 100) if trigger_count > 0 else 0)
    elif status == "active":
        badge = "<span style='padding:6px 14px;border-radius:999px;font-size:12.5px;font-weight:600;background:rgba(52,211,153,.12);color:var(--green);border:1px solid rgba(52,211,153,.3)'><span style='display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite'></span> در حال اجرا</span>"
        bar_pct = 50 + (staging_day / staging_days * 50) if staging_days > 0 else 50
    elif status == "stopped":
        badge = "<span style='padding:6px 14px;border-radius:999px;font-size:12.5px;font-weight:600;background:rgba(248,113,113,.12);color:var(--red);border:1px solid rgba(248,113,113,.3)'>⛔ متوقف شد</span>"
        bar_pct = 0
    else:
        badge = "<span style='padding:6px 14px;border-radius:999px;font-size:12.5px;font-weight:600;background:rgba(96,165,250,.12);color:var(--blue);border:1px solid rgba(96,165,250,.3)'>🟡 ناشناخته</span>"
        bar_pct = 0

    return f"""
  <div class="referral-faker" style="background:linear-gradient(145deg, var(--card), var(--card2));border:1px solid var(--border);border-radius:var(--radius);padding:20px;margin-bottom:28px;box-shadow:var(--shadow)">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
      <div style="font-size:28px">🔗</div>
      <div style="flex:1">
        <div style="font-size:17px;font-weight:600">ساخت ركال‌های فيك (تراز ۲)</div>
        <div style="font-size:12.5px;color:var(--muted);margin-top:2px">در صورت رسیدن اكانت اصلی به ۱۰۰ ركال، خودكار ركال‌های فيك برای هر ركال واقعی ساخته می‌شود</div>
      </div>
      {badge}
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px">
      <div style="background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center">
        <small style="color:var(--muted);display:block;margin-bottom:6px"> اكانت اصلی </small>
        <b style="font-size:14px;font-weight:600">{esc(main_account)}</b>
      </div>
      <div style="background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center">
        <small style="color:var(--muted);display:block;margin-bottom:6px"> ركال‌های الان </small>
        <b style="font-size:22px;font-weight:700;color:var(--blue)">{current_count}</b>
        <small style="color:var(--muted);display:block">از {trigger_count}</small>
      </div>
      <div style="background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center">
        <small style="color:var(--muted);display:block;margin-bottom:6px"> ركال‌های فيك ساخته‌شده </small>
        <b style="font-size:22px;font-weight:700;color:var(--green)">{total_fakes}</b>
      </div>
      <div style="background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center">
        <small style="color:var(--muted);display:block;margin-bottom:6px"> روز توزيع </small>
        <b style="font-size:14px;font-weight:600">{staging_day + 1}/{staging_days}</b>
      </div>
    </div>
    <div class="creator-progress__bar" style="margin-bottom:10px">
      <div class="creator-progress__fill" style="width:{bar_pct:.1f}%;background:linear-gradient(90deg, var(--orange), var(--gold))"></div>
    </div>
    <div style="display:flex;justify-content:space-between;align-items:center">
      <span style="font-size:13px;color:var(--muted)"> فاز فعلی: {esc(phase_label)} </span>
      <span style="font-size:12px;color:var(--faint)"> آپدیت: {esc(updated)} </span>
    </div>
  </div>
"""


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
    """The active list is accounts.json, never resurrect old status-only users."""
    accounts = load_json(ACCOUNTS_FILE, [])
    if not isinstance(accounts, list):
        return []
    banned = {entry["username"] for entry in banned_accounts_info() if entry["confirmed"]}
    active = []
    seen = set()
    for account in accounts:
        if not isinstance(account, dict):
            continue
        user = account.get("username")
        if not isinstance(user, str) or not user or user in banned or user in seen:
            continue
        active.append(account)
        seen.add(user)
    return active


def banned_accounts_section():
    """Show recorded bans without changing accounts or triggering requests."""
    records = banned_accounts_info()
    confirmed_count = sum(record["confirmed"] for record in records)
    rows = []
    for record in records:
        user = esc(record["username"])
        reason = esc(record["reason"] or "دلیل ثبت نشده")
        detected_at = esc(record["detected_at"] or "زمان ثبت نشده")
        label = "🚫 بن‌شده" if record["confirmed"] else "⚠️ تأییدنشده (سابقهٔ قدیمی)"
        rows.append(f"""
          <tr>
            <td style="padding:12px;overflow-wrap:anywhere">{user}</td>
            <td style="padding:12px;color:var(--red);white-space:nowrap">{label}</td>
            <td style="padding:12px;color:var(--muted);min-width:180px;overflow-wrap:anywhere">{reason}</td>
            <td style="padding:12px;white-space:nowrap"><span dir="ltr">{detected_at}</span></td>
          </tr>""")
    rows_html = "".join(rows) if rows else """
          <tr><td colspan="4" style="padding:24px;text-align:center;color:var(--muted)">
            هیچ اکانت بن‌شده‌ای ثبت نشده است.
          </td></tr>"""

    return f"""
  <details id="bannedAccountsPanel" class="banned-status creator-status status-panel" data-status-panel>
    <summary class="creator-status__head status-panel__summary">
      <span class="creator-status__icon" aria-hidden="true">🚫</span>
      <span class="creator-status__title">اکانت‌های بن‌شده</span>
      <span class="creator-status__badge" style="color:var(--red);border:1px solid currentColor">
        {confirmed_count} اکانت بن‌شده
      </span>
      <span class="status-panel__chevron" aria-hidden="true">⌄</span>
    </summary>
    <div class="status-panel__body">
    <p style="color:var(--muted);font-size:13px;line-height:1.9;margin:0 0 16px">
      بن‌های ثبت‌شده توسط سیستم، شامل اکانت‌های حذف‌شده از فهرست پردازش.
      فقط پیام صریح تعلیق مبنای حذف است؛ سوابق قدیمیِ تأییدنشده باعث حذف اکانت نمی‌شوند.
    </p>
    <div style="border:1px solid var(--border);border-radius:10px;overflow:auto;max-height:320px">
      <table style="width:100%;border-collapse:collapse;font-size:12.5px;text-align:right">
        <thead style="background:var(--card2);position:sticky;top:0">
          <tr>
            <th scope="col" style="padding:12px">اکانت</th>
            <th scope="col" style="padding:12px">وضعیت</th>
            <th scope="col" style="padding:12px">دلیل ثبت‌شده</th>
            <th scope="col" style="padding:12px">زمان تشخیص (تهران)</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    </div>
  </details>"""


def render():
    status = load_json(STATUS_FILE, {})
    accounts = active_accounts_info()
    earnings = load_json(EARNINGS_FILE, {})
    creator_info = creator_status_info()
    log_errors_map = recent_errors_from_log()
    meta_date = status.get("_meta", {}).get("date", time.strftime("%Y-%m-%d"))
    now = datetime.now(tz=TZ).strftime("%Y-%m-%d %H:%M:%S")

    ordered = accounts

    total_trips_today = 0
    total_checks_today = 0
    total_errors_today = 0
    total_cars_on_trip = 0
    total_cars_idle = 0
    total_wallet = 0.0
    account_blocks = []

    for acc in ordered:
        user = acc.get("username", "?")
        a = status.get(user, {})
        ran = a.get("last_run")
        last_result = a.get("last_result", "")
        c_today = a.get("checks_today", 0)
        t_today = a.get("trips_started_today", 0)
        t_total = a.get("trips_started_total", 0)
        e_today = a.get("errors_today", 0)
        cars = a.get("cars", [])
        on_trip = [c for c in cars if c.get("status") == "on_trip"]
        idle = [c for c in cars if c.get("status") == "idle"]

        total_trips_today += t_today
        total_checks_today += c_today
        total_errors_today += e_today
        total_cars_on_trip += len(on_trip)
        total_cars_idle += len(idle)
        total_wallet += (a.get("wallet", {}) or {}).get("usdt", 0) or 0

        if last_result == "OK":
            badge = "<span class='pill pill--ok'>✓ OK</span>"
        elif "FAILED" in last_result:
            badge = "<span class='pill pill--bad'>✕ FAILED</span>"
        elif "error" in last_result.lower():
            badge = "<span class='pill pill--warn'>⚠ ERRORS</span>"
        else:
            badge = "<span class='pill pill--off'>尚无数据</span>"

        initial = esc(user[0].upper() if user else "?")
        cars_html = "".join(car_card(c) for c in cars) if cars else \
            "<div class='car-card car-card--none'><div class='car-card__name'>尚无 داده تاکسی</div></div>"

        recent_errors = list(a.get("recent_errors", [])) + log_errors_map.get(user, [])
        recent_errors = recent_errors[:15]
        if not recent_errors and last_result and last_result != "OK":
            recent_errors = [{"t": ran or "", "e": last_result, "why": last_result, "level": "error"}]

        if recent_errors:
            err_html = "".join(
                "<div class='err'><div class='err__row'><span class='err__icon'>{ic}</span>"
                "<span class='err__time'>{t}</span><span class='err__msg'>{e}</span>"
                "<span class='err__caret'>▾</span></div>"
                "<div class='err__detail'><div class='err__why'><b>📌 دلیل:</b> {why}</div>"
                "<pre>{e}</pre></div></div>".format(
                    ic="⚠" if e.get("level") == "warning" else "⛔",
                    t=esc(fmt_jalali(e.get("t", ""))), e=esc(e.get("e", "")),
                    why=esc(e.get("why") or e.get("e", "")),
                )
                for e in reversed(recent_errors)
            )
            errors_section = f"<div class='err-sec'><div class='err-sec__head'>📋 خراهای اخیر — {len(recent_errors)}</div>{err_html}</div>"
        else:
            errors_section = ""

        account_blocks.append(f"""
      <section class="acc">
        <div class="acc__head">
          <div class="acc__avatar">{initial}</div>
          <div class="acc__id"><span class="acc__user">{esc(user)}</span>{badge}</div>
          <div class="acc__trips-today"><b>{t_today}</b><small>سفرهای امروز</small></div>
        </div>
        <div class="acc__stats">
          <div class="stat"><small>سفرهای کل</small><b>{t_total}</b></div>
          <div class="stat"><small>checks امروز</small><b>{c_today}</b></div>
          <div class="stat"><small>خطای امروز</small><b class="{'neg' if e_today else ''}">{e_today}</b></div>
          <div class="stat"><small>تاکسی در سفر</small><b>{len(on_trip)}</b></div>
        </div>
        {account_features(a)}
        <div class="acc__foot">آخرین اجرای: <b>{esc(fmt_jalali(ran) if ran else '—')}</b> · نتایج: <b>{esc(last_result or '—')}</b></div>
        <div class="cars">{cars_html}</div>
        {mission_center(a.get("missions", {}), user)}
        {referral_center(a.get("referrals", {}), user)}
        {errors_section}
      </section>""")

    log_lines = tail_lines(LOG_FILE, 30)
    log_html = "".join(f"<div class='logline'>{esc(l.rstrip())}</div>" for l in log_lines) or "<div class='logline'>尚无 لاگ</div>"

    # Jalali (Persian solar) display formatting
    report_date_j = fmt_jalali(meta_date)
    now_j = fmt_jalali(now)

    return f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crypto Taxi — Dashboard</title>
<style>
  :root {{
    --bg0:#060b16; --bg1:#0a1020; --bg2:#0f1828; --card:#111b2e; --card2:#162238;
    --card-hi:#1a2842; --txt:#eef3fc; --muted:#94a3c4; --faint:#64748b;
    --gold:#ffc629; --gold-d:#e8a21c; --green:#34d399; --green-d:#10b981;
    --blue:#60a5fa; --blue-d:#3b82f6; --orange:#fb923c; --red:#f87171;
    --violet:#c084fc; --cyan:#22d3ee; --teal:#2dd4bf;
    --border:#1e2c44; --border-hi:#2a3e5e;
    --shadow:0 8px 32px rgba(0,0,0,.4);
    --shadow-lg:0 12px 40px rgba(0,0,0,.5);
    --radius:16px; --radius-lg:20px; --radius-sm:10px;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  html {{ scroll-behavior:smooth; }}
  body {{
    font-family:'Vazirmatn','Segoe UI',system-ui,-apple-system,Arial,sans-serif;
    background:var(--bg0);
    color:var(--txt); min-height:100vh; line-height:1.6;
    background-image:
      radial-gradient(ellipse 80% 50% at 50% -20%, rgba(255,198,41,.08), transparent 70%),
      radial-gradient(ellipse 60% 50% at 100% 0%, rgba(96,165,250,.08), transparent 60%),
      radial-gradient(ellipse 60% 50% at 0% 100%, rgba(192,132,252,.06), transparent 60%),
      linear-gradient(180deg, var(--bg0), var(--bg1) 40%, var(--bg0));
    background-attachment:fixed;
  }}
  ::-webkit-scrollbar {{ width:8px; height:8px; }}
  ::-webkit-scrollbar-track {{ background:var(--bg1); }}
  ::-webkit-scrollbar-thumb {{ background:var(--border-hi); border-radius:4px; }}
  ::-webkit-scrollbar-thumb:hover {{ background:var(--muted); }}

  /* ---- Header ---- */
  header {{
    position:sticky; top:0; z-index:100;
    background:rgba(10,16,32,.85); backdrop-filter:blur(20px);
    border-bottom:1px solid var(--border);
    box-shadow:0 4px 24px rgba(0,0,0,.3);
  }}
  .header-inner {{
    display:flex; justify-content:space-between; align-items:center;
    padding:14px 28px; gap:16px; flex-wrap:wrap; max-width:1400px; margin:0 auto;
  }}
  .header-left {{ display:flex; align-items:center; gap:14px; }}
  .header-logo {{
    width:48px; height:48px; border-radius:14px; flex:0 0 auto;
    background:linear-gradient(135deg, var(--gold), var(--gold-d));
    display:flex; align-items:center; justify-content:center; font-size:26px;
    box-shadow:0 6px 20px rgba(255,198,41,.3), inset 0 1px 0 rgba(255,255,255,.2);
  }}
  h1 {{ font-size:20px; font-weight:700; letter-spacing:.3px; }}
  h1 .accent {{ color:var(--gold); }}
  .header-sub {{ font-size:12.5px; color:var(--muted); margin-top:2px; }}
  .header-right {{ display:flex; align-items:center; gap:18px; flex-wrap:wrap; }}
  .live-badge {{
    display:inline-flex; align-items:center; gap:7px;
    color:var(--green); font-size:13px; font-weight:600;
    padding:6px 14px; border-radius:999px;
    background:rgba(52,211,153,.1); border:1px solid rgba(52,211,153,.3);
  }}
  .live-badge .dot {{
    width:8px; height:8px; border-radius:50%; background:var(--green);
    box-shadow:0 0 10px var(--green); animation:pulse 2s infinite;
  }}
  @keyframes pulse {{ 0%,100%{{opacity:1; transform:scale(1)}} 50%{{opacity:.5; transform:scale(1.2)}} }}
  .header-meta {{ text-align:right; font-size:12.5px; color:var(--muted); line-height:1.7; }}
  .header-meta b {{ color:var(--txt); font-weight:600; }}

  /* ---- Toolbar ---- */
  .toolbar {{ padding:18px 28px 0; max-width:1400px; margin:0 auto; }}
  .clear-btn {{
    background:linear-gradient(135deg, rgba(248,113,113,.15), rgba(248,113,113,.08));
    color:var(--red); border:1px solid rgba(248,113,113,.35);
    border-radius:12px; padding:10px 22px; font-size:13.5px; font-weight:600;
    cursor:pointer; transition:all .2s; box-shadow:0 4px 14px rgba(248,113,113,.1);
    display:inline-flex; align-items:center; gap:8px;
  }}
  .clear-btn:hover {{
    background:linear-gradient(135deg, rgba(248,113,113,.25), rgba(248,113,113,.15));
    box-shadow:0 6px 20px rgba(248,113,113,.2); transform:translateY(-1px);
  }}
  .clear-btn:active {{ transform:scale(.97); }}
  .clear-btn:disabled {{ opacity:.6; cursor:wait; }}

  /* ---- Main ---- */
  main {{ padding:24px 28px 48px; max-width:1400px; margin:0 auto; }}

  /* ---- Summary cards ---- */
  .summary {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:14px; margin-bottom:28px; }}
  .sum {{
    background:linear-gradient(145deg, var(--card), var(--card2));
    border:1px solid var(--border); border-radius:var(--radius);
    padding:18px 20px; box-shadow:var(--shadow); position:relative; overflow:hidden;
    transition:transform .2s, border-color .2s, box-shadow .2s;
  }}
  .sum:hover {{ transform:translateY(-3px); border-color:var(--border-hi); box-shadow:var(--shadow-lg); }}
  .sum::before {{
    content:''; position:absolute; top:0; left:0; right:0; height:3px;
    background:var(--accent, var(--gold));
    opacity:.8;
  }}
  .sum::after {{
    content:''; position:absolute; bottom:-30px; right:-30px;
    width:80px; height:80px; border-radius:50%;
    background:var(--accent, var(--gold)); opacity:.05;
  }}
  .sum__icon {{ font-size:24px; margin-bottom:8px; }}
  .sum small {{ color:var(--muted); display:block; margin-bottom:6px; font-size:13px; }}
  .sum b {{ font-size:28px; font-weight:700; font-variant-numeric:tabular-nums; }}
  .sum--gold {{ --accent:var(--gold); }} .sum--gold b {{ color:var(--gold); }}
  .sum--green {{ --accent:var(--green); }} .sum--green b {{ color:var(--green); }}
  .sum--blue {{ --accent:var(--blue); }} .sum--blue b {{ color:var(--blue); }}
  .sum--violet {{ --accent:var(--violet); }} .sum--violet b {{ color:var(--violet); }}
  .sum--red {{ --accent:var(--red); }} .sum--red b {{ color:var(--red); }}
  .sum--cyan {{ --accent:var(--cyan); }} .sum--cyan b {{ color:var(--cyan); }}

  /* ---- Section title ---- */
  .section-title {{
    font-size:14px; font-weight:700; color:var(--muted);
    letter-spacing:.5px; text-transform:uppercase;
    border-bottom:1px solid var(--border); padding-bottom:8px;
    margin:32px 0 16px; display:flex; align-items:center; gap:8px;
  }}
  .section-title .icon {{ font-size:18px; }}

  /* ---- Account card ---- */
  .acc {{
    background:linear-gradient(150deg, var(--card), #0d1626);
    border:1px solid var(--border); border-radius:var(--radius-lg);
    padding:22px 24px; margin-bottom:20px; box-shadow:var(--shadow);
    animation:fadein .5s ease both; position:relative; overflow:hidden;
  }}
  .acc::before {{
    content:''; position:absolute; top:0; left:0; right:0; height:3px;
    background:linear-gradient(90deg, var(--gold), var(--blue), var(--violet));
    opacity:.6;
  }}
  @keyframes fadein {{ from{{opacity:0; transform:translateY(12px)}} to{{opacity:1; transform:none}} }}
  .acc__head {{ display:flex; align-items:center; gap:14px; margin-bottom:16px; }}
  .acc__avatar {{
    width:48px; height:48px; border-radius:14px; flex:0 0 auto;
    background:linear-gradient(135deg, var(--gold), var(--gold-d));
    color:#1a1205; display:flex; align-items:center; justify-content:center;
    font-weight:700; font-size:22px;
    box-shadow:0 6px 18px rgba(255,198,41,.25), inset 0 1px 0 rgba(255,255,255,.2);
  }}
  .acc__id {{ flex:1; min-width:0; }}
  .acc__user {{ font-weight:600; font-size:16px; word-break:break-all; display:block; margin-bottom:5px; }}
  .acc__trips-today {{ text-align:center; flex:0 0 auto; }}
  .acc__trips-today b {{ display:block; font-size:28px; color:var(--gold); font-weight:700; }}
  .acc__trips-today small {{ color:var(--muted); font-size:11px; }}

  /* ---- Stats row ---- */
  .acc__stats {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-bottom:14px; }}
  .stat {{
    background:rgba(255,255,255,.025); border:1px solid var(--border);
    border-radius:var(--radius-sm); padding:12px 14px; transition:border-color .2s;
  }}
  .stat:hover {{ border-color:var(--border-hi); }}
  .stat small {{ color:var(--muted); display:block; margin-bottom:4px; font-size:12px; }}
  .stat b {{ font-size:22px; font-weight:700; font-variant-numeric:tabular-nums; }}

  /* ---- Feature tiles ---- */
  .acc__feats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:10px; margin-bottom:14px; }}
  .feat {{
    background:rgba(255,255,255,.02); border:1px solid var(--border);
    border-radius:var(--radius-sm); padding:10px 12px; text-align:center;
    transition:transform .15s, border-color .2s;
  }}
  .feat:hover {{ transform:translateY(-2px); border-color:var(--border-hi); }}
  .feat span {{ display:block; color:var(--muted); font-size:11px; margin-bottom:4px; }}
  .feat b {{ font-size:15px; font-weight:700; font-variant-numeric:tabular-nums; }}
  .feat--gold b {{ color:var(--gold); }}
  .feat--green b {{ color:var(--green); }}
  .feat--blue b {{ color:var(--blue); }}
  .feat--earn {{ background:rgba(245,158,11,.08); border-color:rgba(245,158,11,.25); }}
  .feat--earn b {{ color:#f59e0b; }}
  .feat--avg {{ background:rgba(99,102,241,.08); border-color:rgba(99,102,241,.25); }}
  .feat--avg b {{ color:#818cf8; }}

  /* ---- Footer line ---- */
  .acc__foot {{ font-size:12.5px; color:var(--muted); border-top:1px solid var(--border); padding-top:12px; margin-bottom:16px; }}
  .acc__foot b {{ color:var(--txt); font-weight:600; }}

  /* ---- Car cards ---- */
  .cars {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:14px; }}
  .car-card {{
    background:linear-gradient(165deg, var(--card2), #13203a);
    border:1px solid var(--border); border-radius:var(--radius);
    padding:18px; transition:transform .2s, box-shadow .2s, border-color .2s;
    position:relative; overflow:hidden;
  }}
  .car-card:hover {{ transform:translateY(-4px); box-shadow:var(--shadow-lg); }}
  .car-card--on_trip {{ border-color:rgba(52,211,153,.4); box-shadow:0 0 20px rgba(52,211,153,.08); }}
  .car-card--idle {{ border-color:rgba(96,165,250,.4); }}
  .car-card__top {{ display:flex; align-items:center; gap:12px; }}
  .taxi-svg {{ width:52px; height:32px; flex:0 0 auto; filter:drop-shadow(0 4px 12px rgba(0,0,0,.4)); }}
  .car-card__name-wrap {{ flex:1; min-width:0; display:flex; flex-direction:column; gap:7px; align-items:flex-start; }}
  .car-card__name {{ font-weight:600; font-size:15px; }}
  .car-card__plate {{ margin-top:12px; font-size:12.5px; color:var(--muted); letter-spacing:.6px; font-family:'Consolas',monospace; }}
  .pill {{ padding:2px 9px; border-radius:999px; font-size:11px; font-weight:600; white-space:nowrap; display:inline-flex; align-items:center; gap:3px; }}
  .pill--rent {{ background:rgba(192,132,252,.15); color:var(--violet); }}
  .pill--own {{ background:rgba(148,163,196,.12); color:var(--muted); }}
  .pill--trip {{ background:rgba(52,211,153,.15); color:var(--green); }}
  .pill--ready {{ background:rgba(96,165,250,.15); color:var(--blue); }}
  .pill--off {{ background:rgba(148,163,196,.1); color:var(--faint); }}
  .pill--ok {{ background:rgba(52,211,153,.15); color:var(--green); }}
  .pill--bad {{ background:rgba(248,113,113,.15); color:var(--red); }}
  .pill--warn {{ background:rgba(251,146,60,.15); color:var(--orange); }}

  /* ---- Trips bar ---- */
  .trips {{ margin-top:14px; }}
  .trips__txt {{ font-size:12.5px; color:var(--muted); margin-bottom:7px; }}
  .trips__txt b {{ color:var(--txt); }}
  .trips-bar {{ height:6px; border-radius:6px; background:rgba(255,255,255,.06); overflow:hidden; }}
  .trips-bar.full .trips__fill {{ background:linear-gradient(90deg, var(--gold), var(--gold-d)); }}
  .trips__fill {{ height:100%; border-radius:6px; background:linear-gradient(90deg, var(--blue), var(--blue-d)); transition:width .5s ease; }}
  .chip {{ padding:2px 9px; border-radius:999px; font-size:11.5px; font-weight:600; white-space:nowrap; display:inline-flex; align-items:center; gap:3px; }}
  .chip--bonus {{ background:rgba(52,211,153,.15); color:var(--green); }}

  /* ---- Countdown ---- */
  .cd {{ margin-top:14px; padding-top:14px; border-top:1px solid var(--border); }}
  .cd__row {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }}
  .cd__label {{ color:var(--muted); font-size:13px; }}
  .cd__val {{ color:var(--gold); font-variant-numeric:tabular-nums; font-size:16px; font-weight:700; }}
  .cd__track {{ height:7px; border-radius:7px; background:rgba(255,255,255,.06); overflow:hidden; }}
  .cd__fill {{ height:100%; border-radius:7px; background:linear-gradient(90deg, var(--gold), #fb923c); width:0%; transition:width 1s linear; }}
  .cd--idle .cd__label {{ color:var(--blue); }}

  /* ---- Errors ---- */
  .err-sec {{ margin-top:18px; border-top:1px dashed var(--border); padding-top:14px; }}
  .err-sec__head {{ color:var(--orange); font-size:13.5px; font-weight:600; margin-bottom:10px; display:flex; align-items:center; gap:6px; }}
  .err {{ margin-bottom:8px; }}
  .err__row {{
    display:flex; align-items:center; gap:10px; padding:10px 14px;
    border:1px solid var(--border); border-radius:var(--radius-sm);
    background:rgba(248,113,113,.04); cursor:pointer; font-size:13px;
    transition:background .15s, border-color .15s;
  }}
  .err__row:hover {{ background:rgba(248,113,113,.1); border-color:rgba(248,113,113,.3); }}
  .err__icon {{ flex:0 0 auto; font-size:15px; }}
  .err__time {{ color:var(--faint); font-variant-numeric:tabular-nums; flex:0 0 auto; font-size:12px; }}
  .err__msg {{ flex:1; word-break:break-all; }}
  .err__caret {{ color:var(--muted); transition:transform .2s; }}
  .err__row:hover .err__caret {{ transform:rotate(90deg); }}
  .err__detail {{ display:none; margin:-4px 0 8px; border-left:3px solid var(--red); padding:10px 14px; background:rgba(10,16,32,.6); border-radius:0 var(--radius-sm) var(--radius-sm) 0; }}
  .err__detail.open {{ display:block; animation:fadein .25s ease both; }}
  .err__why {{ margin-bottom:8px; font-size:13px; color:#fcd9a8; }}
  .err__detail pre {{ margin:0; white-space:pre-wrap; word-break:break-all; font-family:'Consolas','Courier New',monospace; font-size:12px; color:#fca5a5; }}

  /* ---- Mission / Referral sections ---- */
  .mission-sec {{ margin-top:14px; border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; }}
  .mission-sec__head {{ font-weight:700; font-size:14.5px; color:var(--gold); display:flex; align-items:center; gap:10px; flex-wrap:wrap; padding:11px 14px; }}
  .mission-sec__head--toggle {{ cursor:pointer; user-select:none; background:rgba(255,255,255,.02); transition:background .15s; }}
  .mission-sec__head--toggle:hover {{ background:rgba(255,198,41,.08); }}
  .mc-toggle {{ font-size:14px; transition:transform .2s; display:inline-block; }}
  .mission-sec.open .mc-toggle {{ transform:rotate(180deg); }}
  .mission-sec__count {{ background:rgba(255,198,41,.15); color:var(--gold); border-radius:999px; padding:2px 10px; font-size:12px; font-weight:600; }}
  .mission-sec__claimed {{ color:var(--muted); font-size:12px; }}
  .mission-sec__body {{ display:none; padding:4px 14px 14px; }}
  .mission-sec.open .mission-sec__body {{ display:block; }}
  .mission-sec__sub {{ color:var(--muted); font-size:12.5px; margin:6px 0 14px; }}
  .mission-sec__group {{ color:var(--muted); font-size:12.5px; font-weight:600; margin:14px 0 8px; }}
  .mission-list {{ display:flex; flex-direction:column; gap:10px; }}
  .mission {{ background:rgba(255,255,255,.02); border:1px solid var(--border); border-radius:var(--radius-sm); padding:12px 14px; transition:border-color .15s; }}
  .mission--claimable {{ border-color:rgba(255,198,41,.5); background:rgba(255,198,41,.05); }}
  .mission--claimed {{ border-color:rgba(52,211,153,.4); }}
  .mission--done {{ border-color:rgba(52,211,153,.3); }}
  .mission__top {{ display:flex; justify-content:space-between; align-items:center; gap:10px; }}
  .mission__title {{ font-weight:600; font-size:14px; }}
  .mission__note {{ color:var(--blue); font-size:12px; margin-top:6px; }}
  .mission__reward {{ margin:8px 0; }}
  .chip--usdt {{ background:rgba(96,165,250,.15); color:var(--blue); }}
  .chip--cash {{ background:rgba(255,198,41,.15); color:var(--gold); }}
  .chip--none {{ background:rgba(148,163,196,.1); color:var(--muted); }}
  .mstatus {{ font-size:12.5px; font-weight:600; white-space:nowrap; }}
  .mstatus--claimed {{ color:var(--green); }}
  .mstatus--claim {{ color:var(--gold); }}
  .mstatus--done {{ color:var(--blue); }}
  .mission__bar {{ height:6px; border-radius:6px; background:rgba(255,255,255,.06); overflow:hidden; margin-top:6px; }}
  .mission__fill {{ height:100%; border-radius:6px; background:linear-gradient(90deg, var(--blue), var(--blue-d)); }}
  .mission--claimable .mission__fill {{ background:linear-gradient(90deg, var(--gold), #fb923c); }}
  .mission--claimed .mission__fill {{ background:var(--green); }}
  .mission__progress {{ color:var(--muted); font-size:12px; margin-top:5px; }}

  /* ---- Revenue section ---- */
  .rev {{ background:linear-gradient(150deg,#0c1830,#0a1426); border:1px solid var(--border); border-radius:var(--radius-lg); margin-bottom:28px; overflow:hidden; box-shadow:var(--shadow); animation:fadein .5s ease both; }}
  .rev__head {{ background:rgba(96,165,250,.08); border-bottom:1px solid var(--border); padding:14px 22px; display:flex; justify-content:space-between; align-items:center; font-weight:700; font-size:15px; color:var(--blue); }}
  .rev__updated {{ font-size:12px; font-weight:400; color:var(--muted); }}
  .rev__grid {{ padding:18px 22px; display:grid; grid-template-columns:1fr 1fr; gap:20px; }}
  .rev__cards {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
  .rev__card {{ background:rgba(255,255,255,.02); border:1px solid var(--border); border-radius:var(--radius); padding:14px 16px; }}
  .rev__card-label {{ font-size:12px; color:var(--muted); margin-bottom:6px; }}
  .rev__card-val {{ font-size:22px; font-weight:700; color:var(--gold); font-variant-numeric:tabular-nums; }}
  .rev__card-val small {{ font-size:12px; color:var(--muted); }}
  .rev__card-sub {{ font-size:11.5px; color:var(--faint); margin-top:3px; }}
  .rev__card--today {{ border-color:rgba(255,198,41,.3); }}
  .rev__card--week {{ border-color:rgba(96,165,250,.3); }}
  .rev__card--week .rev__card-val {{ color:var(--blue); }}
  .rev__card--month {{ border-color:rgba(52,211,153,.3); }}
  .rev__card--month .rev__card-val {{ color:var(--green); }}
  .rev__card--all {{ border-color:rgba(192,132,252,.3); }}
  .rev__card--all .rev__card-val {{ color:var(--violet); }}
  .rev__chart {{ background:rgba(255,255,255,.02); border:1px solid var(--border); border-radius:var(--radius); padding:14px 16px; }}
  .rev__chart-label {{ font-size:13px; color:var(--muted); margin-bottom:12px; }}
  .rev-bars {{ display:flex; align-items:flex-end; gap:8px; height:90px; }}
  .rev-bar-wrap {{ flex:1; display:flex; flex-direction:column; align-items:center; gap:4px; height:100%; }}
  .rev-bar-label {{ font-size:11px; color:var(--faint); }}
  .rev-bar-track {{ width:100%; flex:1; background:rgba(255,255,255,.04); border-radius:6px 6px 0 0; display:flex; flex-direction:column; justify-content:flex-end; overflow:hidden; }}
  .rev-bar-fill {{ width:100%; border-radius:6px 6px 0 0; background:linear-gradient(180deg,var(--gold),#fb923c); min-height:4px; transition:height .8s ease; }}
  .rev-bar-val {{ font-size:10px; color:var(--muted); }}
  .rev__detail {{ background:rgba(255,255,255,.02); border:1px solid var(--border); border-radius:var(--radius); padding:14px 16px; }}
  .rev__detail-label {{ font-size:13px; color:var(--muted); margin-bottom:10px; }}
  .rev-table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  .rev-table th {{ color:var(--muted); font-size:12px; padding:6px 8px; border-bottom:1px solid var(--border); text-align:right; }}
  .rev-table td {{ padding:6px 8px; border-bottom:1px solid rgba(255,255,255,.03); }}
  .rev-table tr:last-child td {{ border-bottom:none; }}
  .rev-table td.num {{ text-align:center; color:var(--gold); font-variant-numeric:tabular-nums; }}
  .rev-no {{ color:var(--faint); font-size:13px; text-align:center; padding:10px; }}

  /* ---- Log ---- */
  .log {{ background:#0a101e; border:1px solid var(--border); border-radius:var(--radius); padding:14px 18px; font-family:'Consolas','Courier New',monospace; font-size:12px; max-height:320px; overflow:auto; }}
  .logline {{ white-space:pre-wrap; word-break:break-all; padding:1px 0; color:#a8b5d0; }}
  .neg {{ color:var(--red); }}

  /* Native disclosures work with mouse, touch and keyboard, even without JS. */
  details.status-panel {{ padding:0; }}
  .status-panel > .status-panel__summary {{
    padding:20px 28px; margin:0; cursor:pointer; list-style:none;
    user-select:none; transition:background .15s;
  }}
  .status-panel__summary::-webkit-details-marker {{ display:none; }}
  .status-panel__summary:hover {{ background:rgba(96,165,250,.06); }}
  .status-panel__summary:focus-visible {{ outline:2px solid var(--blue); outline-offset:-4px; border-radius:var(--radius-lg); }}
  .status-panel__summary .creator-status__title {{ min-width:120px; }}
  .status-panel__body {{ padding:0 28px 24px; }}
  .status-panel__chevron {{
    display:inline-flex; align-items:center; justify-content:center;
    width:28px; height:28px; color:var(--muted); font-size:24px;
    transition:transform .2s; margin-inline-start:auto;
  }}
  .status-panel[open] > summary .status-panel__chevron {{ transform:rotate(180deg); }}
  @media (max-width:600px) {{
    .status-panel > .status-panel__summary {{ padding:16px; gap:10px; }}
    .status-panel__body {{ padding:0 16px 16px; }}
  }}
  @media (prefers-reduced-motion:reduce) {{
    .status-panel__chevron {{ transition:none; }}
  }}

  /* ---- Creator status panel ---- */
  .creator-status {{
    background:linear-gradient(150deg, var(--card), #0d1626);
    border:1px solid var(--border); border-radius:var(--radius-lg);
    padding:24px 28px; margin-bottom:24px; box-shadow:var(--shadow);
    position:relative; overflow:hidden;
  }}
  .creator-status::before {{
    content:''; position:absolute; top:0; left:0; right:0; height:3px;
    background:linear-gradient(90deg, var(--green), var(--gold), var(--blue));
    opacity:.6;
  }}
  .creator-status__head {{ display:flex; align-items:center; gap:14px; margin-bottom:18px; flex-wrap:wrap; }}
  .creator-status__icon {{
    width:44px; height:44px; border-radius:12px; flex:0 0 auto;
    background:linear-gradient(135deg, var(--green), var(--green-d));
    color:#052e1c; display:flex; align-items:center; justify-content:center;
    font-size:22px; font-weight:700;
  }}
  .creator-status__title {{ font-size:17px; font-weight:600; flex:1; }}
  .creator-status__badge {{
    padding:6px 14px; border-radius:999px; font-size:12.5px; font-weight:600;
    display:inline-flex; align-items:center; gap:6px;
  }}
  .creator-status__badge--active {{ background:rgba(52,211,153,.12); color:var(--green); border:1px solid rgba(52,211,153,.3); }}
  .creator-status__badge--idle {{ background:rgba(96,165,250,.12); color:var(--blue); border:1px solid rgba(96,165,250,.3); }}
  .creator-status__badge--done {{ background:rgba(255,198,41,.12); color:var(--gold); border:1px solid rgba(255,198,41,.3); }}
  .creator-phase-badge {{
    padding:5px 12px; border-radius:8px; font-size:12px; font-weight:600;
    background:rgba(96,165,250,.1); color:var(--blue);
    border:1px solid rgba(96,165,250,.25);
    display:inline-block;
  }}
  .creator-stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:14px; margin-bottom:16px; }}
  .creator-stat {{
    background:rgba(255,255,255,.025); border:1px solid var(--border);
    border-radius:var(--radius-sm); padding:14px 16px; text-align:center;
  }}
  .creator-stat small {{ color:var(--muted); display:block; margin-bottom:6px; font-size:12px; }}
  .creator-stat b {{ font-size:26px; font-weight:700; font-variant-numeric:tabular-nums; }}
  .creator-stat--green b {{ color:var(--green); }}
  .creator-stat--gold b {{ color:var(--gold); }}
  .creator-stat--blue b {{ color:var(--blue); }}
  .creator-stat--red b {{ color:var(--red); }}
  .creator-progress {{ margin-top:16px; }}
  .creator-progress__bar {{
    height:10px; border-radius:10px; background:rgba(255,255,255,.06);
    overflow:hidden; position:relative;
  }}
  .creator-progress__fill {{
    height:100%; border-radius:10px;
    background:linear-gradient(90deg, var(--green), var(--gold));
    transition:width .8s ease; position:relative;
  }}
  .creator-progress__fill::after {{
    content:''; position:absolute; inset:0;
    background:linear-gradient(90deg, transparent, rgba(255,255,255,.2), transparent);
    animation:shimmer 2s infinite;
  }}
  @keyframes shimmer {{ 0%{{transform:translateX(-100%)}} 100%{{transform:translateX(100%)}} }}
  .creator-progress__txt {{
    display:flex; justify-content:space-between; margin-top:8px;
    font-size:12.5px; color:var(--muted);
  }}
  .creator-progress__txt b {{ color:var(--txt); font-weight:600; }}
  .creator-progress__timer {{
    margin-top:14px; padding:12px 16px; border-radius:var(--radius-sm);
    background:rgba(96,165,250,.08); border:1px solid rgba(96,165,250,.2);
    display:flex; justify-content:space-between; align-items:center;
  }}
  .creator-progress__timer-label {{ color:var(--muted); font-size:13px; }}
  .creator-progress__timer-val {{ color:var(--blue); font-size:18px; font-weight:700; font-variant-numeric:tabular-nums; }}

  /* ---- Footer ---- */
  footer {{ text-align:center; color:var(--faint); font-size:12px; padding:24px; }}
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <div class="header-left">
      <div class="header-logo">🚕</div>
      <div>
        <h1>Crypto Taxi <span class="accent">— Dashboard</span></h1>
        <div class="header-sub">وضعیت زنده اکانت‌ها و تاکسی‌ها · اولویت رنت · استارت خودکار پس از پایان سفر</div>
      </div>
    </div>
    <div class="header-right">
      <div class="live-badge"><span class="dot"></span> زنده</div>
      <div class="header-meta">
        <div>تاریخ گزارش: <b>{esc(report_date_j)}</b></div>
        <div>آخرین آپ‌دیت: <b>{esc(now_j)}</b></div>
      </div>
    </div>
  </div>
</header>
<div class="toolbar">
  <button class="clear-btn" id="clearBtn">🧹 پاک کردن خراها — از این لحظه صفر شود</button>
</div>
<main>
  <div class="summary">
    <div class="sum sum--gold"><div class="sum__icon">👤</div><small>تعداد اکانت‌ها</small><b>{len(ordered)}</b></div>
    <div class="sum sum--green"><div class="sum__icon">🚗</div><small>در حال سفر</small><b>{total_cars_on_trip}</b></div>
    <div class="sum sum--blue"><div class="sum__icon">🔵</div><small>آماده به استارت</small><b>{total_cars_idle}</b></div>
    <div class="sum sum--cyan"><div class="sum__icon">🛣</div><small>سفرهای کلیم‌شده امروز</small><b>{total_trips_today}</b></div>
    <div class="sum sum--violet"><div class="sum__icon">💰</div><small>موجودی کل USDT</small><b>{total_wallet:.4f}</b></div>
    <div class="sum sum--red"><div class="sum__icon">⚠️</div><small>خطای امروز</small><b class="{'neg' if total_errors_today else ''}">{total_errors_today}</b></div>
  </div>

  {creator_status_section(creator_info)}

  {referral_faker_status_section()}


  {banned_accounts_section()}

  {revenue_section(earnings)}

  {''.join(account_blocks)}

  <div class="section-title"><span class="icon">📋</span> لاگ اخیر (آخرین 30 خط)</div>
  <div class="log">{log_html}</div>
</main>
<footer>Crypto Taxi Claimer · dashboard زنده — تبدیق ۳۰ ثانیه‌ی · اولویت رنت فعال</footer>
<script>
  function pad(n) {{ return String(n).padStart(2, '0'); }}
  function updateCountdowns() {{
    document.querySelectorAll('.cd[data-ends]').forEach(function (el) {{
      var ends = parseInt(el.dataset.ends, 10);
      var started = parseInt(el.dataset.started, 10) || 0;
      var diff = Math.floor((ends - Date.now()) / 1000);
      var val = el.querySelector('.cd__val');
      var fill = el.querySelector('.cd__fill');
      if (!val) return;
      if (diff <= 0) {{
        val.textContent = 'سفر تموم شد — در حال استارت مجدد';
        val.style.color = '#2ecc71';
        if (fill) fill.style.width = '100%';
      }} else {{
        var m = Math.floor(diff / 60), s = diff % 60;
        val.textContent = m + ':' + pad(s) + ' دقیقه';
        val.style.color = '#ffc629';
        if (fill && started) {{
          var total = (ends - started) / 1000;
          if (total > 0) fill.style.width = Math.max(0, Math.min(100, (1 - diff / total) * 100)) + '%';
        }}
      }}
    }});
  }}
  updateCountdowns();
  setInterval(updateCountdowns, 1000);

  // Refresh within 10 seconds when accounts are added or removed/banned.
  // Use the same filtered active list as the cards and summary count.
  (function () {{
    var currentCount = document.querySelectorAll('.acc').length;
    function checkCount() {{
      fetch('/count', {{ cache: 'no-store' }})
        .then(function (r) {{ return r.json(); }})
        .then(function (j) {{
          if (j.ok && j.count !== currentCount) {{
            console.log('Account count changed: ' + currentCount + ' -> ' + j.count + ', reloading...');
            location.reload();
          }}
        }})
        .catch(function () {{}});
    }}
    setInterval(checkCount, 10000);

    // Live-update the creator status panel every 10s without page reload.
    // Fetches /creator-status and updates the progress bar, counts, timer.
    var creatorNextCycleTs = 0; // epoch ms for next cycle start

    function formatCountdown(ms) {{
      if (ms <= 0) return '🚀 شروع شد';
      var total = Math.floor(ms / 1000);
      var m = Math.floor(total / 60);
      var s = total % 60;
      return m + ':' + (s < 10 ? '0' + s : s) + ' دقیقه';
    }}

    function updateCreatorCountdown() {{
      var el = document.getElementById('creatorCountdown');
      var wrap = document.getElementById('creatorCountdownWrap');
      if (!el || !wrap) return;
      if (!creatorNextCycleTs) {{
        el.textContent = '—';
        wrap.style.color = 'var(--muted)';
        return;
      }}
      var diff = creatorNextCycleTs - Date.now();
      if (diff > 0) {{
        el.textContent = formatCountdown(diff);
        wrap.style.color = diff < 30000 ? 'var(--orange)' : 'var(--blue)';
      }} else {{
        el.textContent = '🚀 شروع شد';
        wrap.style.color = 'var(--green)';
      }}
    }}

    function updateCreatorStatus() {{
      fetch('/creator-status', {{ cache: 'no-store' }})
        .then(function (r) {{ return r.json(); }})
        .then(function (j) {{
          if (!j || !j.ok) return;
          var fill = document.getElementById('creatorFill');
          var msg = document.getElementById('creatorStatusMsg');
          var regInfo = document.getElementById('creatorRegInfo');
          var actionInfo = document.getElementById('creatorActionInfo');
          var cycleInfo = document.getElementById('creatorCycleInfo');
          if (fill) fill.style.width = (j.pct || 0) + '%';
          // Update next cycle timestamp for countdown
          creatorNextCycleTs = j.enabled !== false && j.next_cycle_ts
            ? j.next_cycle_ts * 1000 : 0; // reset stale countdown when disabled
          if (msg) {{
            msg.textContent = j.message || '';
            msg.style.color = j.done ? 'var(--gold)' : (j.running ? 'var(--green)' : 'var(--blue)');
          }}
          if (regInfo) {{
            var reg = j.last_registered || '尚无';
            var regT = j.last_registered_time || '尚无';
            regInfo.innerHTML = '<b>' + reg + '</b> (' + regT + ')';
          }}
          if (actionInfo) {{
            var act = j.last_action || '尚无 فعالیت';
            var actT = j.last_action_time || '尚无';
            actionInfo.innerHTML = '<b>' + act + '</b> — ' + actT;
          }}
          if (cycleInfo) {{
            cycleInfo.textContent = j.next_cycle_info || '尚无';
          }}
          // Immediately update countdown with new data
          updateCreatorCountdown();
        }})
        .catch(function () {{}});
    }}
    updateCreatorStatus();
    setInterval(updateCreatorStatus, 10000);
    // Smooth countdown — update every second
    setInterval(updateCreatorCountdown, 1000);
  }})();

  // click an error row to expand/collapse its full detail
  document.querySelectorAll('.err__row').forEach(function (el) {{
    el.addEventListener('click', function () {{
      var d = el.nextElementSibling;
      if (d && d.classList.contains('err__detail')) d.classList.toggle('open');
    }});
  }});
  // clear all errors from this moment on — same proxy-safe transport as
  // the account management actions (try POST /, then header GET, then query)
  var clearBtn = document.getElementById('clearBtn');
  if (clearBtn) {{
    clearBtn.addEventListener('click', function () {{
      if (!confirm('✅ خراها از این لحظه پاک و صفر شوند؟')) return;
      clearBtn.disabled = true;
      clearBtn.textContent = '…در حال پاک کردن';
      requestAction('clear', {{}});
    }});
  }}
  // ---- account management panel ----
  // The sandbox preview proxy only forwards the exact root path "/" and
  // answers any other path/query/headers-blocked request itself ("Not
  // found: ..."). So the requestAction helper tries several transport
  // forms on the root path, in order, until the dashboard answers with
  // JSON. A non-JSON answer means the request never reached the
  // dashboard and the next form is tried; a JSON answer (ok or error)
  // is final.
  function parseJsonResponse(r) {{
    return r.text().then(function (text) {{
      try {{ return JSON.parse(text); }}
      catch (e) {{ throw new Error('پاسخ سرور JSON نبود: ' + text.slice(0, 200)); }}
    }});
  }}
  function requestAction(action, params) {{
    var body = JSON.stringify({{
      action: action,
      username: params.username || '',
      password: params.password || '',
      old: params.old || ''
    }});
    var attempts = [
      // 1) POST /clear — dedicated path, most likely to pass through proxies
      function () {{
        return fetch('/clear', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ action: action }})
        }});
      }},
      // 2) GET /clear — dedicated path, no body needed for clear
      function () {{
        return fetch('/clear', {{ method: 'GET' }});
      }},
      // 3) POST "/" with a JSON body (fallback)
      function () {{
        return fetch('/', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: body
        }});
      }},
      // 4) GET "/" with custom headers (action carried in headers)
      function () {{
        return fetch('/', {{
          method: 'GET',
          headers: {{
            'X-D-Action': action,
            'X-D-Username': params.username || '',
            'X-D-Password': params.password || '',
            'X-D-Old': params.old || ''
          }}
        }});
      }},
      // 5) GET "/?action=..." with query params (plain localhost fallback)
      function () {{
        var qs = Object.keys(params).map(function (k) {{
          return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]);
        }}).join('&');
        return fetch('/?action=' + encodeURIComponent(action) + (qs ? '&' + qs : ''), {{ method: 'GET' }});
      }}
    ];
    var idx = 0;
    function tryNext() {{
      if (idx >= attempts.length) {{
        alert('⚠ همه روش‌ها با خطا مواجه شد — ممکن است درخواست توسط لایه میانه (proxy) مسدود شده باشد.');
        return;
      }}
      var attempt = attempts[idx++];
      attempt()
        .then(parseJsonResponse)
        .then(function (j) {{
          if (j.ok) {{ location.reload(); }}
          else {{ alert('⚠ ' + (j.error || 'خطا در انجام عملیات')); }}
        }})
        .catch(function () {{ tryNext(); }});
    }}
    tryNext();
  }}
  // Keep each account panel's disclosure state across automatic page refreshes.
  document.querySelectorAll('details[data-status-panel]').forEach(function (panel) {{
    var key = 'status_panel_open_' + panel.id;
    try {{ panel.open = localStorage.getItem(key) === '1'; }} catch (e) {{}}
    panel.addEventListener('toggle', function () {{
      try {{ localStorage.setItem(key, panel.open ? '1' : '0'); }} catch (e) {{}}
    }});
  }});

  // Mission Center collapse/expand — state persists across the 30s refresh.
  function mcKey(sec) {{
    var id = sec.id || '';
    return 'mc_open_' + id;
  }}
  document.querySelectorAll('.mission-sec').forEach(function (sec) {{
    var head = sec.querySelector('.mission-sec__head--toggle');
    if (!head) return;
    var key = mcKey(sec);
    var wasOpen = false;
    try {{ wasOpen = localStorage.getItem(key) === '1'; }} catch (e) {{}}
    if (wasOpen) sec.classList.add('open');
    function toggle() {{
      sec.classList.toggle('open');
      var open = sec.classList.contains('open');
      try {{ localStorage.setItem(key, open ? '1' : '0'); }} catch (e) {{}}
    }}
    head.addEventListener('click', toggle);
    head.addEventListener('keydown', function (e) {{
      if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); toggle(); }}
    }});
  }});
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        # Only the "clear" action is served on the root path (for the
        # clear-errors button). Account management (add/update/delete) is
        # removed — accounts are edited directly in accounts.json.
        header_action = self.headers.get("X-D-Action")
        action = header_action or qs.get("action", [None])[0]

        if parsed.path in ("/", "/index.html") and action == "clear":
            try:
                cleared = clear_errors()
                self.send_json({"ok": True, "cleared": cleared})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=500)
            return

        # Dedicated GET /clear path — more likely to pass through preview
        # proxies than root-path query params or custom headers.
        if parsed.path == "/clear":
            try:
                cleared = clear_errors()
                self.send_json({"ok": True, "cleared": cleared})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=500)
            return

        # Lightweight account-count endpoint for JS auto-refresh detection.
        # The dashboard JS polls this every 10s; additions and ban/removal
        # changes reload the cards and every displayed account count.
        if parsed.path == "/count":
            try:
                count = len(active_accounts_info())
                self.send_json({"ok": True, "count": count})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=500)
            return

        # Lightweight creator-status endpoint — returns the same info as
        # creator_status_section but as JSON, for live JS updates without
        # a full page reload. Dashboard JS polls this every 10s.
        if parsed.path == "/creator-status":
            try:
                self.send_json(creator_status_info())
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=500)
            return

        if self.path in ("/", "/index.html"):
            body = render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_json({"ok": False, "error": "Not found"}, status=404)

    def do_POST(self):
        # JSON body reader
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            data = {}

        try:
            # Only the "clear" action is served now. Account management
            # (add/update/delete) is removed — accounts are edited directly
            # in accounts.json (the claimer hot-reloads it every sweep).
            action = data.get("action")
            if action and self.path in ("/", "/index.html") and action == "clear":
                cleared = clear_errors()
                self.send_json({"ok": True, "cleared": cleared})
                return
            if self.path == "/clear":
                cleared = clear_errors()
                self.send_json({"ok": True, "cleared": cleared})
            else:
                self.send_json({"ok": False, "error": "Not found"}, status=404)
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)}, status=500)

    def log_message(self, fmt, *args):
        print("[dashboard] " + fmt % args)


if __name__ == "__main__":
    print(f"Dashboard serving on http://0.0.0.0:{PORT}")
    httpd = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()
