"""Transactional email, via Resend.

Two messages exist and there will never be a third without a reason written
down here: **verify your address** and **reset your password**. Both are the
direct consequence of an action the recipient just took seconds earlier, which
is what keeps this out of marketing-consent and unsubscribe territory — there is
no list, no digest and nothing to opt out of.

Three properties are load-bearing and each is a decision, not an accident:

* **Sending can never fail a request.** Every public entry point returns a bool
  and swallows its own errors. The endpoints that send are the same endpoints
  that create accounts and accept sign-ins, and the app's stated product
  invariant is that nothing in the auth path blocks a new user (see
  routers/auth_router.py). A Resend outage must degrade to "no email arrived",
  never to "sign-up is down".
* **No key means log the link, not crash.** A dev box with no RESEND_API_KEY
  gets a fully working sign-up flow with the verification link printed to the
  console. Requiring a third-party account before the app runs locally is the
  kind of friction that gets worked around by disabling the feature.
* **The HTTP call is httpx, not the `resend` SDK.** httpx is already a direct
  dependency, pinned specifically so the auth path does not inherit another
  package's dependency tree (see backend/requirements.txt). `POST
  https://api.resend.com/emails` with a Bearer key is the whole API surface used
  here; an SDK for one JSON POST would add a dependency to the one part of the
  app where a supply-chain surprise is an authentication problem. Swapping to
  `import resend` later is a ten-line change confined to `_post`.

Every message is sent as BOTH html and text. Not politeness: a plain-text
alternative measurably improves spam scoring, and the link must stay usable in
a client that strips the markup.
"""
import re
import threading
import time
from html import escape
from urllib.parse import quote

import httpx

from ..config import (
    APP_BASE_URL,
    EMAIL_FROM,
    EMAIL_REPLY_TO,
    EMAIL_SEND_MAX_PER_HOUR,
    EMAIL_SEND_MAX_PER_HOUR_PER_CLIENT,
    RESEND_API_KEY,
)

_RESEND_URL = "https://api.resend.com/emails"
_TIMEOUT = 10.0

# Cosmetic only — used in the subject lines and the sign-off.
_PRODUCT = "Four in a Thousand"


def mail_enabled() -> bool:
    """Whether a send will actually reach Resend.

    Reported by GET /auth/config so the frontend can word itself honestly: with
    no mail service, telling someone to "check your inbox" is a lie that costs
    them a support round-trip."""
    return bool(RESEND_API_KEY)


# ── Abuse guard ──────────────────────────────────────────────────────────────
# /auth/password/forgot takes an arbitrary address from an unauthenticated
# caller and sends mail to it. Unthrottled that is a free mail-bombing lever
# pointed at anyone, billed to our quota and charged against our sending
# reputation with the recipient's provider — the damage lands on the deployment,
# not on the attacker.
#
# In-process and deliberately simple. This app runs as a single instance
# (config.MAX_CONCURRENT_SEARCHES' comment explains why) so a dict under a lock
# is the whole correct implementation; a Redis bucket here would be
# infrastructure for a problem that does not exist yet.
_rate_lock = threading.Lock()
_rate_hits: dict[str, list[float]] = {}
_RATE_WINDOW = 3600.0


def rate_limit_ok(key: str, limit: int | None = None) -> bool:
    """True when `key` may send now; records the send when it returns True.

    Keys are caller-chosen (an email address, a client IP) so one address cannot
    be used to exhaust another's allowance. `limit` defaults to the per-address
    allowance; callers pass EMAIL_SEND_MAX_PER_HOUR_PER_CLIENT for an IP key,
    which must be far looser — see that setting for why an IP is not a person.

    Returns True — i.e. fails OPEN — when the limit is 0 or less, which is the
    documented way to switch the guard off."""
    limit = EMAIL_SEND_MAX_PER_HOUR if limit is None else limit
    if limit <= 0:
        return True
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(key, ()) if now - t < _RATE_WINDOW]
        if len(hits) >= limit:
            _rate_hits[key] = hits
            return False
        hits.append(now)
        _rate_hits[key] = hits
        # Opportunistic sweep: without it this dict grows one entry per address
        # ever seen, for the lifetime of the process.
        if len(_rate_hits) > 4096:
            for k in [k for k, v in _rate_hits.items() if not v or now - v[-1] > _RATE_WINDOW]:
                _rate_hits.pop(k, None)
    return True


# ── Sending ──────────────────────────────────────────────────────────────────
def _post(payload: dict) -> bool:
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(
                _RESEND_URL,
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json=payload,
            )
    except httpx.HTTPError as e:
        print(f"[mail] send failed (network): {type(e).__name__}: {e}")
        return False

    if resp.status_code >= 300:
        # Resend's body names the actual cause, and the two that matter most in
        # practice are both operator misconfiguration rather than bugs: an
        # unverified sending DOMAIN, and onboarding@resend.dev being used to
        # write to anyone but the account owner. Log it verbatim so the fix is
        # obvious from the console instead of requiring a dashboard visit.
        print(f"[mail] send rejected ({resp.status_code}): {resp.text[:400]}")
        return False
    return True


def send_email(to: str, subject: str, html: str, text: str) -> bool:
    """Send one message. Never raises. Returns whether Resend accepted it.

    Acceptance is not delivery — Resend queues, and a bounce arrives later and
    out of band. No caller may therefore treat True as proof the person received
    anything, which is exactly why the flows built on this never *depend* on the
    mail arriving."""
    to = (to or "").strip()
    if not to:
        return False

    if not mail_enabled():
        # The link is the whole payload of both messages, so printing it keeps
        # local development completely functional with no Resend account.
        link = _first_link(text)
        print(f"[mail] RESEND_API_KEY unset -- not sending {subject!r} to {to}")
        if link:
            print(f"[mail]   link: {link}")
        return False

    payload = {"from": EMAIL_FROM, "to": [to], "subject": subject, "html": html, "text": text}
    if EMAIL_REPLY_TO:
        payload["reply_to"] = EMAIL_REPLY_TO
    return _post(payload)


def client_rate_limit_ok(key: str) -> bool:
    """The per-CLIENT allowance, which is deliberately much looser than the
    per-address one. See config.EMAIL_SEND_MAX_PER_HOUR_PER_CLIENT."""
    return rate_limit_ok(key, EMAIL_SEND_MAX_PER_HOUR_PER_CLIENT)


def _first_link(text: str) -> str:
    m = re.search(r"https?://\S+", text or "")
    return m.group(0) if m else ""


# ── Link construction ────────────────────────────────────────────────────────
def _link(path: str, token: str) -> str:
    """A frontend URL carrying a token.

    `quote` matters: both tokens are itsdangerous output, which is URL-safe
    base64 and so already clean — but this is one line of defence against a
    future token format that isn't, in a string that lands in a stranger's
    inbox."""
    return f"{APP_BASE_URL}{path}?token={quote(token, safe='')}"


def verification_link(token: str) -> str:
    return _link("/verify-email", token)


def reset_link(token: str) -> str:
    return _link("/reset-password", token)


# ── Templates ────────────────────────────────────────────────────────────────
# Inline styles and a table-free single column, because email clients are not
# browsers: Gmail strips <style> blocks, Outlook renders through Word, and
# anything clever degrades to unstyled text at best. The design goal here is
# only that the unstyled fallback still reads correctly, which is why the button
# is always followed by the same URL in plain text.
_BASE = (
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
    "font-size:15px;line-height:1.55;color:#2a2724;"
)
_BUTTON = (
    "display:inline-block;padding:11px 20px;background:#c2603f;color:#ffffff;"
    "text-decoration:none;border-radius:6px;font-weight:600;font-size:15px;"
)
_MUTED = "font-size:12.5px;line-height:1.5;color:#8a827a;"


def _wrap(body_html: str) -> str:
    return (
        f'<div style="{_BASE}max-width:520px;margin:0 auto;padding:24px;">'
        f'<div style="font-weight:700;font-size:17px;margin-bottom:18px;">{_PRODUCT}</div>'
        f"{body_html}"
        f'<div style="{_MUTED}margin-top:26px;border-top:1px solid #e7e2dc;padding-top:14px;">'
        f"You received this because someone used this address at {escape(_PRODUCT)}. "
        f"If that wasn&rsquo;t you, you can ignore it &mdash; nothing happens until the link above is used."
        f"</div></div>"
    )


def send_verification_email(to: str, token: str) -> bool:
    """The link that flips User.email_verified true."""
    url = verification_link(token)
    html = _wrap(
        "<p>Thanks for signing up. Confirm this is your address so we can reach you "
        "about your searches and reset your password if you ever need to.</p>"
        f'<p style="margin:22px 0;"><a href="{escape(url, quote=True)}" style="{_BUTTON}">'
        "Confirm my email</a></p>"
        f'<p style="{_MUTED}">Or paste this into your browser:<br>'
        f'<span style="word-break:break-all;">{escape(url)}</span></p>'
        f'<p style="{_MUTED}">The link works for 3 days.</p>'
    )
    text = (
        f"Thanks for signing up to {_PRODUCT}.\n\n"
        f"Confirm your email address:\n{url}\n\n"
        "The link works for 3 days. If you didn't sign up, ignore this email.\n"
    )
    return send_email(to, f"Confirm your email for {_PRODUCT}", html, text)


def send_password_reset_email(to: str, token: str) -> bool:
    """The link that permits one password change.

    The 'if you didn't ask' line is not boilerplate: this message goes to
    addresses typed by unauthenticated strangers, so a real recipient who did
    not request it needs to be told plainly that nothing has happened to their
    account and that no action is required."""
    url = reset_link(token)
    html = _wrap(
        "<p>Someone asked to reset the password on the account for this address. "
        "If that was you, choose a new one here:</p>"
        f'<p style="margin:22px 0;"><a href="{escape(url, quote=True)}" style="{_BUTTON}">'
        "Choose a new password</a></p>"
        f'<p style="{_MUTED}">Or paste this into your browser:<br>'
        f'<span style="word-break:break-all;">{escape(url)}</span></p>'
        f'<p style="{_MUTED}">The link works for 1 hour and can only be used once. '
        "If you didn&rsquo;t ask for this, no action is needed &mdash; your password "
        "has not changed.</p>"
    )
    text = (
        f"Someone asked to reset the password on your {_PRODUCT} account.\n\n"
        f"Choose a new password:\n{url}\n\n"
        "The link works for 1 hour and can only be used once.\n"
        "If you didn't ask for this, no action is needed - your password has not changed.\n"
    )
    return send_email(to, f"Reset your {_PRODUCT} password", html, text)
