#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Crypto Taxi — Mail Reader (MailManager)

Reads disposable emails from mail.tm to fetch verification codes,
password reset links, and any other messages sent to account emails.

Used by:
- account_creator.py — to store email passwords for each account
- claimer.py — to automatically fetch verification codes when needed

mail.tm API flow:
1. POST /accounts     → create email account (returns account data, no token)
2. POST /token        → login with email+password → returns JWT token
3. GET  /messages     → list messages (requires Authorization header)
4. GET  /messages/{id} → full message with text, subject, from, etc.

Usage:
    mgr = MailManager()
    msgs = mgr.fetch_messages(email, email_pw)
    code = mgr.get_verification_code(email, email_pw)
"""

import json
import os
import re
import time
import logging
import requests

MAIL_API = "https://api.mail.tm"

HERE = os.path.dirname(os.path.abspath(__file__))

# Regex patterns for common verification codes
CODE_PATTERNS = [
    # 6-digit codes (most common): 123456, 000123, etc.
    re.compile(r'\b(\d{6})\b'),
    # 4-digit codes
    re.compile(r'\b(\d{4})\b'),
    # 8-digit codes
    re.compile(r'\b(\d{8})\b'),
    # Alphanumeric codes: ABC123, XYZ789, etc.
    re.compile(r'\b([A-Z0-9]{6,8})\b'),
]

# Keywords that indicate a message is a verification/OTP
VERIFICATION_KEYWORDS = [
    "verification", "verify", "code", "otp", "confirmation",
    "confirm", "activation", "code", "pin", "token", "验证",
    "verificación", "código", "confirmar", "código de verificación",
]

log = logging.getLogger("mailreader")


class MailManager:
    """Manages reading emails from mail.tm accounts."""

    def __init__(self):
        self._token_cache = {}  # email -> (token, expires_at)
        self._token_ttl = 3600  # tokens last ~1 hour on mail.tm

    def login(self, email, email_pw, force=False):
        """Login to mail.tm and return a JWT token. Cached for 1 hour."""
        # Check cache
        if not force and email in self._token_cache:
            token, expires_at = self._token_cache[email]
            if time.time() < expires_at:
                return token

        try:
            resp = requests.post(
                f"{MAIL_API}/token",
                json={"address": email, "password": email_pw},
                timeout=15,
            )
            if resp.status_code == 200:
                token = resp.json().get("token", "")
                if token:
                    self._token_cache[email] = (token, time.time() + self._token_ttl)
                    return token
            else:
                log.warning("mail login failed (%d) for %s: %s",
                            resp.status_code, email, resp.text[:100])
                return None
        except Exception as e:
            log.warning("mail login error for %s: %s", email, e)
            return None

    def fetch_messages(self, email, email_pw, limit=20, unread_only=False):
        """
        Fetch messages from a mail.tm inbox.
        Returns a list of message dicts with: id, subject, from, text, seen, date.
        """
        token = self.login(email, email_pw)
        if not token:
            return []

        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = requests.get(
                f"{MAIL_API}/messages",
                headers=headers,
                timeout=15,
            )
            if resp.status_code != 200:
                log.warning("fetch messages failed (%d) for %s", resp.status_code, email)
                return []

            members = resp.json().get("hydra:member", [])
            # Sort by date descending (newest first)
            members.sort(key=lambda m: m.get("createdAt", ""), reverse=True)

            messages = []
            for m in members[:limit]:
                msg_id = m.get("id")
                if not msg_id:
                    continue
                # Get full message detail
                msg_detail = self._get_message_detail(token, msg_id)
                if msg_detail:
                    messages.append(msg_detail)
            return messages
        except Exception as e:
            log.warning("fetch messages error for %s: %s", email, e)
            return []

    def _get_message_detail(self, token, msg_id):
        """Get full message detail by ID."""
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = requests.get(
                f"{MAIL_API}/messages/{msg_id}",
                headers=headers,
                timeout=15,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            return {
                "id": data.get("id", ""),
                "subject": data.get("subject", ""),
                "from": data.get("from", {}).get("address", "") if isinstance(data.get("from"), dict) else str(data.get("from", "")),
                "to": data.get("to", ""),
                "text": data.get("text", ""),
                "html": data.get("html", ""),
                "seen": data.get("seen", False),
                "date": data.get("createdAt", ""),
            }
        except Exception:
            return None

    def get_verification_code(self, email, email_pw, lookback_minutes=30):
        """
        Fetch the latest verification/OTP code from a mail.tm inbox.
        Returns the code string, or None if no verification code found.

        Searches subject + text for:
        - 6-digit codes (most common for SMS/email verification)
        - 4-digit codes
        - 8-digit codes
        - Alphanumeric codes (6-8 chars)
        """
        messages = self.fetch_messages(email, email_pw, limit=10)
        now = time.time()

        for msg in messages:
            # Check time window
            msg_date = msg.get("date", "")
            try:
                from datetime import datetime, ZoneInfo
                tz = ZoneInfo("Asia/Tehran")
                dt = datetime.fromisoformat(msg_date.replace("Z", "+00:00"))
                msg_ts = dt.timestamp()
                if now - msg_ts > lookback_minutes * 60:
                    continue
            except Exception:
                pass

            subject = (msg.get("subject") or "").lower()
            text = msg.get("text") or ""
            combined = subject + " " + text

            # Check if this looks like a verification message
            is_verification = any(kw in combined.lower() for kw in VERIFICATION_KEYWORDS)

            # Extract codes — try 6-digit first (most reliable)
            codes = self._extract_codes(text, subject)

            if is_verification and codes:
                return codes[0]
            elif codes and len(codes) >= 1:
                # Return first code even if not explicitly a verification message
                return codes[0]

        return None

    def _extract_codes(self, text, subject=""):
        """Extract potential verification codes from text. Returns list of codes."""
        combined = (subject + " " + text) if subject else text
        codes = []

        # Try patterns in order of reliability
        for pattern in CODE_PATTERNS:
            matches = pattern.findall(combined)
            for m in matches:
                # Filter out obvious non-codes (years, etc.)
                if len(m) == 4 and (m.startswith("19") or m.startswith("20")):
                    continue
                if len(m) == 6 and (m.startswith("19") or m.startswith("20")):
                    continue
                codes.append(m)

        return codes

    def check_for_new_codes(self, emails, delay=30, max_wait=180):
        """
        Poll multiple emails for new verification codes.
        Returns a dict of {email: code} for any codes found.

        Args:
            emails: list of (email, email_pw) tuples
            delay: seconds between polls
            max_wait: maximum total time to wait
        """
        results = {}
        end_time = time.time() + max_wait
        poll_count = 0

        while time.time() < end_time:
            poll_count += 1
            for email, email_pw in emails:
                if email in results:
                    continue  # already got code for this email
                code = self.get_verification_code(email, email_pw, lookback_minutes=15)
                if code:
                    results[email] = code
                    log.info("✅ Verification code found for %s: %s", email, code)
                    if len(results) >= len(emails):
                        return results
            if len(results) >= len(emails):
                break
            time.sleep(delay)

        return results


# Convenience functions for external use
_manager = None

def get_manager():
    """Return a singleton MailManager instance."""
    global _manager
    if _manager is None:
        _manager = MailManager()
    return _manager


def get_verification_code(email, email_pw):
    """Quick convenience: get verification code for a single email."""
    return get_manager().get_verification_code(email, email_pw)


def fetch_messages(email, email_pw):
    """Quick convenience: fetch messages for a single email."""
    return get_manager().fetch_messages(email, email_pw)
