"""Google Calendar API — read-only import as fixed Items (Plan.md「行事曆」).

Only the ``calendar.readonly`` scope is requested; nothing is ever written
back. Uses the OAuth 2.0 installed-app flow (loopback redirect + PKCE) with
the user's own OAuth client (``credentials.json`` from Google Cloud Console,
type "Desktop app"). Tokens are stored next to the database with mode 0600.

Mapping
  timed event                 → fixed Item (busy time)
  all-day event               → fixed, allday (shown, not subtracted from free time)
  transparency=transparent    → fixed, allday-like (shown, not busy)
  cancelled / declined by me  → skipped
Recurring events are expanded by the API (``singleEvents=true``) inside the
import window, so each occurrence is its own Item.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .ics import import_records
from .models import TimeSpec
from .store import Store, default_db_path
from .timeutil import now_utc, parse_dt

SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"

Http = Callable[[str, str, dict | None, dict | None], dict]


def _http(method: str, url: str, headers: dict | None = None, form: dict | None = None) -> dict:
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 — fixed Google endpoints
        return json.loads(r.read())


class GoogleCalendar:
    def __init__(self, client_secret: str | Path | None = None, token_path: str | Path | None = None,
                 http: Http | None = None):
        self.http = http or _http
        self.token_path = Path(token_path or os.environ.get("ZYTOM_GCAL_TOKEN")
                               or default_db_path().with_name("gcal_token.json")).expanduser()
        self._client = None
        cs = client_secret or os.environ.get("ZYTOM_GCAL_CLIENT")
        if cs:
            raw = json.loads(Path(cs).expanduser().read_text())
            self._client = raw.get("installed") or raw.get("web") or raw

    # ------------------------------------------------------------- tokens
    def _load(self) -> dict | None:
        return json.loads(self.token_path.read_text()) if self.token_path.exists() else None

    def _save(self, tok: dict) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(tok))
        os.chmod(self.token_path, 0o600)

    @property
    def authorized(self) -> bool:
        tok = self._load()
        return bool(tok and tok.get("refresh_token"))

    def authorize(self, open_browser: bool = True, timeout: float = 300) -> dict:
        """Interactive consent in the browser; stores a refresh token."""
        if not self._client:
            raise ValueError("need an OAuth client: --client-secret credentials.json or ZYTOM_GCAL_CLIENT")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        result: dict[str, str] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
                if q.get("state") == state:
                    result.update(q)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("zytom：已取得 Google 日曆唯讀授權，可以關閉此頁。".encode())

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        redirect = f"http://127.0.0.1:{srv.server_port}/"
        url = AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": self._client["client_id"], "redirect_uri": redirect, "response_type": "code",
            "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        print(f"在瀏覽器開啟以下網址授權（唯讀）：\n{url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        srv.timeout = 2

        def serve():
            while "code" not in result and "error" not in result:
                try:
                    srv.handle_request()
                except OSError:  # server closed after timeout
                    return

        threading.Thread(target=serve, daemon=True).start()
        deadline = time.monotonic() + timeout
        while "code" not in result and "error" not in result and time.monotonic() < deadline:
            time.sleep(0.2)
        srv.server_close()
        if "code" not in result:
            raise RuntimeError(f"authorization failed: {result.get('error', 'timeout')}")
        tok = self.http("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, {
            "code": result["code"], "client_id": self._client["client_id"],
            "client_secret": self._client.get("client_secret", ""), "redirect_uri": redirect,
            "grant_type": "authorization_code", "code_verifier": verifier,
        })
        tok["expires_at"] = time.time() + float(tok.get("expires_in", 3600)) - 60
        tok["client_id"] = self._client["client_id"]
        tok["client_secret"] = self._client.get("client_secret", "")
        self._save(tok)
        return {"authorized": True, "scope": tok.get("scope", SCOPE)}

    def access_token(self) -> str:
        tok = self._load()
        if not tok:
            raise ValueError("Google Calendar not authorized — run `zytom gcal auth --client-secret credentials.json`")
        if tok.get("expires_at", 0) > time.time() and tok.get("access_token"):
            return tok["access_token"]
        new = self.http("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, {
            "client_id": tok["client_id"], "client_secret": tok.get("client_secret", ""),
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
        })
        tok.update(access_token=new["access_token"],
                   expires_at=time.time() + float(new.get("expires_in", 3600)) - 60)
        self._save(tok)
        return tok["access_token"]

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{API}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        return self.http("GET", url, {"Authorization": f"Bearer {self.access_token()}"}, None)

    # ---------------------------------------------------------------- api
    def calendars(self) -> list[dict]:
        out, page = [], None
        while True:
            r = self._get("/users/me/calendarList", {"pageToken": page} if page else None)
            out += [{"id": c["id"], "summary": c.get("summary"), "primary": c.get("primary", False),
                     "access_role": c.get("accessRole")} for c in r.get("items", [])]
            page = r.get("nextPageToken")
            if not page:
                return out

    def events(self, calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict]:
        out, page = [], None
        while True:
            params: dict[str, Any] = {"timeMin": time_min.isoformat(), "timeMax": time_max.isoformat(),
                                      "singleEvents": "true", "orderBy": "startTime", "maxResults": 2500,
                                      "showDeleted": "false"}
            if page:
                params["pageToken"] = page
            r = self._get(f"/calendars/{urllib.parse.quote(calendar_id, safe='')}/events", params)
            out += r.get("items", [])
            page = r.get("nextPageToken")
            if not page:
                return out


def _when(v: dict, tzname: str) -> tuple[datetime, bool]:
    if "dateTime" in v:
        return parse_dt(v["dateTime"], v.get("timeZone") or tzname), False
    return parse_dt(v["date"] + "T00:00", tzname), True


def events_to_records(events: list[dict], calendar_id: str, tzname: str) -> list[dict]:
    recs = []
    for ev in events:
        if ev.get("status") == "cancelled" or "start" not in ev:
            continue
        me = next((a for a in ev.get("attendees", []) if a.get("self")), None)
        if me and me.get("responseStatus") == "declined":
            continue
        start, allday = _when(ev["start"], tzname)
        end, _ = _when(ev.get("end") or ev["start"], tzname)
        if end <= start:
            end = start + (timedelta(days=1) if allday else timedelta(hours=1))
        free = ev.get("transparency") == "transparent"
        ext = f"gcal:{calendar_id}:{ev['id']}"
        recs.append({
            "external_id": ext,
            "text": ev.get("summary") or "(無標題)",
            "category": None,
            "time": TimeSpec(type="fixed", start=start, end=end, allday=allday or free),
            "meta": {"source": "gcal", "adapter": "google", "external_id": ext, "calendar_id": calendar_id,
                     "location": ev.get("location"), "url": ev.get("htmlLink"),
                     "recurring_event_id": ev.get("recurringEventId")},
        })
    return recs


def import_gcal(store: Store, name: str, calendar_ids: list[str], tzname: str, now: datetime | None = None,
                horizon_days: int = 60, client: GoogleCalendar | None = None, past_days: int = 1) -> dict:
    """Read-only import of the given calendars over [now − past_days, now + horizon_days]."""
    now = now or now_utc()
    gc = client or GoogleCalendar()
    t0, t1 = now - timedelta(days=past_days), now + timedelta(days=horizon_days)
    records: list[dict] = []
    for cid in calendar_ids:
        records += events_to_records(gc.events(cid, t0, t1), cid, tzname)
    return import_records(store, records, name, now, "google", window_end=t1)
