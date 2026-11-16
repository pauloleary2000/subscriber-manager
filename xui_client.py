"""
XUI One Reseller Panel HTTP Client.

Provides session-authenticated helpers to:
- Verify an active PHPSESSID session against the XUI dashboard.
- Query live reseller credit balances (`owner_credits`).
- List XUI lines expiring within a configurable window (excluding trial/disabled lines).
- Safely extend existing lines while preserving package tier (Clean/Adult, VOD/No-VOD, MAG)
  and bouquet assignments.
"""

import calendar
import os
import re
from datetime import datetime
import requests


class XUIClientError(Exception):
    """Base exception for XUI authentication, parsing, or API errors."""


class XUIAlreadyActiveError(XUIClientError):
    """Raised when XUI shows a line is already active past the renewal threshold."""

    def __init__(self, message, xui_exp_dt=None):
        super().__init__(message)
        self.xui_exp_dt = xui_exp_dt


def add_months_dt(dt, months):
    """Return a datetime advanced by `months` calendar months, clamping end-of-month days."""
    month_idx = dt.month - 1 + months
    year = dt.year + month_idx // 12
    month = month_idx % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


# Mapping of logical subscription tiers to XUI package IDs and display labels
PACKAGE_TIERS = {
    "clean_vod": {
        1: ("15", "1 Month - Clean -with VOD"),
        3: ("21", "3 Months - Clean - with VOD"),
        12: ("19", "12 Months - Clean - with VOD"),
    },
    "adult_vod": {
        1: ("16", "1 Month - Adult - with VOD"),
        3: ("22", "3 Months - Adult - with VOD"),
        12: ("20", "12 Months - Adult -with VOD"),
    },
    "clean_novod": {
        1: ("1", "1 Month"),
        3: ("3", "3 Months"),
        12: ("7", "12 Months"),
    },
    "adult_novod": {
        1: ("2", "1 Month - Adult"),
        3: ("4", "3 Months - Adult"),
        12: ("8", "12 Months - Adult"),
    },
    "mag_clean_vod": {
        1: ("17", "1 Month MAG - Clean -with VOD"),
        3: ("21", "3 Months - Clean - with VOD"),
        12: ("19", "12 Months - Clean - with VOD"),
    },
    "mag_adult_vod": {
        1: ("18", "1 Month MAG - Adult -with VOD"),
        3: ("22", "3 Months - Adult - with VOD"),
        12: ("20", "12 Months - Adult -with VOD"),
    },
}


class XUIClient:
    """HTTP session wrapper around the XUI One reseller web panel."""

    def __init__(self, phpsessid):
        self.base_url = os.getenv("XUI_URL", "https://iptvottawa.ca/SCHSEGZi").rstrip("/")
        self.member_id = os.getenv("XUI_MEMBER_ID", "35")
        self.phpsessid = (phpsessid or "").strip()

        if not self.phpsessid:
            raise XUIClientError(
                "No XUI PHPSESSID session cookie saved. Click 'XUI Sync' in the header to set it."
            )

        self.session = requests.Session()
        self.session.cookies.set("PHPSESSID", self.phpsessid, domain="iptvottawa.ca")
        self.session.cookies.set("theme", "0", domain="iptvottawa.ca")
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Origin": "https://iptvottawa.ca",
        })

    def match_existing_tier(self, months, orig_package=""):
        """Infer the appropriate XUI package ID, label, credit cost, and duration string."""
        orig_l = (orig_package or "").lower()
        is_adult = "adult" in orig_l
        is_mag = "mag" in orig_l
        has_vod = "vod" in orig_l

        if is_mag and has_vod:
            tier = "mag_adult_vod" if is_adult else "mag_clean_vod"
        elif not has_vod and orig_l:
            tier = "adult_novod" if is_adult else "clean_novod"
        else:
            tier = "adult_vod" if is_adult else "clean_vod"

        pkg_id, pkg_name = PACKAGE_TIERS[tier].get(months, PACKAGE_TIERS[tier][1])
        pkg_meta = {
            1: ("1", "1 months"),
            3: ("3", "3 months"),
            12: ("12", "1 years"),
        }
        pkg_cost, pkg_duration = pkg_meta.get(months, (str(months), f"{months} months"))
        return pkg_id, pkg_name, pkg_cost, pkg_duration

    def verify_session(self):
        """Confirm that the configured PHPSESSID is still authenticated on the XUI panel."""
        resp = self.session.get(f"{self.base_url}/dashboard", timeout=15, allow_redirects=True)
        if "/login" in resp.url or 'name="password"' in resp.text:
            raise XUIClientError(
                "XUI PHPSESSID session has expired. Log into XUI in Chrome and paste a fresh PHPSESSID."
            )
        return True

    def get_credits(self):
        """Return the reseller's current credit balance via XUI stats JSON or dashboard HTML."""
        self.verify_session()
        try:
            r = self.session.get(
                f"{self.base_url}/api",
                params={"action": "stats"},
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{self.base_url}/dashboard"},
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("owner_credits") is not None:
                    raw = str(data["owner_credits"]).replace(",", "").strip()
                    val = float(raw)
                    return int(val) if val.is_integer() else val
        except Exception:
            pass

        dash = self.session.get(f"{self.base_url}/dashboard", timeout=15).text
        m = re.search(r'''id=["']owner_credits["'][^>]*>([^<]+)<''', dash, re.IGNORECASE)
        if m:
            raw = m.group(1).replace(",", "").strip()
            val = float(raw)
            return int(val) if val.is_integer() else val
        raise XUIClientError("Could not read available credits from XUI dashboard.")

    def parse_xui_exp(self, exp_str):
        """Parse an XUI expiration string into a naive datetime object."""
        cleaned = (exp_str or "").strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(cleaned, fmt)
            except ValueError:
                continue
        return None

    def get_lines_expiring_within(self, days=7, include_recently_expired_days=3):
        """
        Fetch all non-trial, non-disabled XUI lines expiring within `days` days
        (including lines expired up to `include_recently_expired_days` days ago).
        """
        self.verify_session()
        ajax_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/lines",
        }
        all_rows = []
        start = 0
        page_size = 500

        while True:
            params = {
                "id": "lines",
                "action": "lines",
                "start": start,
                "length": page_size,
            }
            r = self.session.get(f"{self.base_url}/table.php", params=params, headers=ajax_headers, timeout=20)
            r.raise_for_status()
            payload = r.json()
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            if not rows:
                break
            all_rows.extend(rows)
            if len(rows) < page_size:
                break
            start += page_size

        now = datetime.now()
        results = []

        def strip_html(val):
            txt = re.sub(r"<br\s*/?>", " ", str(val or ""), flags=re.IGNORECASE)
            txt = re.sub(r"<[^>]+>", "", txt)
            return re.sub(r"\s+", " ", txt).strip()

        for row in all_rows:
            if not isinstance(row, list) or len(row) < 10:
                continue
            line_id = strip_html(row[0])
            username = strip_html(row[1])
            owner = strip_html(row[3])
            status_html = str(row[4] or "")
            trial_html = str(row[5] or "")
            max_conn = strip_html(row[8]) if len(row) > 8 else "1"
            exp_clean = strip_html(row[9])

            # Exclude trial and disabled/banned accounts
            if "fa-check" in trial_html.lower() and "text-success" in trial_html.lower():
                continue
            if "disabled" in status_html.lower() or "banned" in status_html.lower():
                continue

            exp_dt = self.parse_xui_exp(exp_clean)
            if not exp_dt:
                continue

            delta_days = (exp_dt - now).total_seconds() / 86400.0
            if -include_recently_expired_days <= delta_days <= days:
                status_m = re.search(r'''title=["']([^"']+)["']''', status_html, re.IGNORECASE)
                status_label = status_m.group(1) if status_m else ("Expired" if delta_days < 0 else "Active")
                results.append({
                    "line_id": line_id,
                    "username": username,
                    "owner": owner,
                    "status": status_label,
                    "max_connections": max_conn,
                    "exp_date": exp_dt.strftime("%Y-%m-%d %H:%M"),
                    "days_left": round(delta_days, 1),
                    "is_expired": delta_days < 0,
                })

        results.sort(key=lambda x: x["exp_date"])
        return results

    def find_line_id(self, username):
        """Look up the numeric XUI line ID for an exact username match."""
        if not username:
            raise XUIClientError("Subscriber has no User ID set in the local database.")

        ajax_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/lines",
        }
        params = {
            "id": "lines",
            "action": "lines",
            "search[value]": username,
            "start": 0,
            "length": 25,
        }

        for endpoint in ("table.php", "api.php"):
            url = f"{self.base_url}/{endpoint}"
            try:
                r = self.session.get(url, params=params, headers=ajax_headers, timeout=15)
                if r.status_code != 200:
                    continue
                data = r.json()
                rows = data.get("data", []) if isinstance(data, dict) else []
                for row in rows:
                    if isinstance(row, dict):
                        row_user = str(row.get("username", "")).strip()
                        if row_user.lower() == username.strip().lower():
                            return str(row["id"])
                    elif isinstance(row, list) and len(row) >= 2:
                        row_id = re.sub(r"<[^>]+>", "", str(row[0])).strip()
                        row_user = re.sub(r"<[^>]+>", "", str(row[1])).strip()
                        if row_user.lower() == username.strip().lower() and row_id.isdigit():
                            return row_id
            except Exception:
                continue

        raise XUIClientError(f"Could not find line '{username}' in XUI (or session expired).")

    def get_line_form_defaults(self, line_id):
        """Scrape existing form values and selected bouquets from `/line?id=<line_id>`."""
        url = f"{self.base_url}/line"
        resp = self.session.get(url, params={"id": line_id}, timeout=15)
        if "/login" in resp.url or 'name="password"' in resp.text:
            raise XUIClientError("XUI PHPSESSID session has expired.")
        html = resp.text

        def extract_input(name, default=""):
            m = re.search(rf'name=["\']{re.escape(name)}["\'][^>]*value=["\']([^"\']*)["\']', html, re.IGNORECASE)
            if not m:
                m = re.search(rf'value=["\']([^"\']*)["\'][^>]*name=["\']{re.escape(name)}["\']', html, re.IGNORECASE)
            return m.group(1) if m else default

        def extract_textarea(name, default=""):
            m = re.search(
                rf'<textarea[^>]*name=["\']{re.escape(name)}["\'][^>]*>(.*?)</textarea>',
                html,
                re.IGNORECASE | re.DOTALL,
            )
            return m.group(1).strip() if m else default

        bouquets = []
        for m in re.finditer(r'<input[^>]*name=["\']bouquets_selected\[\]["\'][^>]*>', html, re.IGNORECASE):
            tag = m.group(0)
            if "checked" in tag.lower():
                val_m = re.search(r'value=["\']([^"\']+)["\']', tag, re.IGNORECASE)
                if val_m:
                    bouquets.append(val_m.group(1))

        if not bouquets:
            bouquets = ["1", "2", "4", "5"]

        return {
            "username": extract_input("username"),
            "member_id": extract_input("member_id", self.member_id),
            "orig_package": extract_input("orig_package", "1 Month - Clean -with VOD"),
            "max_connections": extract_input("max_connections", "1"),
            "exp_date": extract_input("exp_date"),
            "contact": extract_input("contact", ""),
            "reseller_notes": extract_textarea("reseller_notes", ""),
            "isp_clear": extract_input("isp_clear", ""),
            "bouquets": bouquets,
        }

    def extend_line(self, username, months, fallback_date_obj, strict_expired_only=False):
        """
        Verify live XUI expiration before extending to prevent accidental double-spends:
        - strict_expired_only=True: only extends if XUI exp_date <= now.
        - strict_expired_only=False: allows extending if expired or expiring within the current month.
        """
        self.verify_session()
        line_id = self.find_line_id(username)
        defaults = self.get_line_form_defaults(line_id)

        current_exp = defaults["exp_date"].strip()
        parsed_xui_exp = self.parse_xui_exp(current_exp)
        now = datetime.now()

        if parsed_xui_exp is None:
            raise XUIClientError(
                f"Could not read XUI expiration date for '{username}' (raw: '{current_exp}') — aborted to protect credits."
            )

        if strict_expired_only:
            if parsed_xui_exp > now:
                raise XUIAlreadyActiveError(
                    f"Account '{username}' is NOT expired in XUI yet (panel exp: {parsed_xui_exp.strftime('%Y-%m-%d %H:%M')})",
                    xui_exp_dt=parsed_xui_exp,
                )
        else:
            last_day = calendar.monthrange(now.year, now.month)[1]
            eom_dt = now.replace(day=last_day, hour=23, minute=59, second=59)
            if parsed_xui_exp > eom_dt:
                raise XUIAlreadyActiveError(
                    f"Account '{username}' is already active past this month in XUI (panel exp: {parsed_xui_exp.strftime('%Y-%m-%d %H:%M')})",
                    xui_exp_dt=parsed_xui_exp,
                )

        pkg_id, pkg_name, pkg_cost, pkg_duration = self.match_existing_tier(
            months=months,
            orig_package=defaults["orig_package"],
        )

        base_dt = parsed_xui_exp if parsed_xui_exp > now else now.replace(hour=parsed_xui_exp.hour, minute=parsed_xui_exp.minute)
        new_exp_formatted = add_months_dt(base_dt, months).strftime("%Y-%m-%d %H:%M")

        multipart_fields = [
            ("edit", (None, str(line_id))),
            ("bouquets_selected", (None, "")),
            ("username", (None, defaults["username"] or username)),
            ("member_id", (None, str(defaults["member_id"] or self.member_id))),
            ("orig_package", (None, defaults["orig_package"])),
            ("package", (None, pkg_id)),
            ("package_cost", (None, pkg_cost)),
            ("package_duration", (None, pkg_duration)),
            ("max_connections", (None, str(defaults["max_connections"] or "1"))),
            ("exp_date", (None, new_exp_formatted)),
            ("contact", (None, defaults["contact"])),
            ("reseller_notes", (None, defaults["reseller_notes"])),
            ("isp_clear", (None, defaults["isp_clear"])),
        ]
        for b_id in defaults["bouquets"]:
            multipart_fields.append(("bouquets_selected[]", (None, str(b_id))))

        post_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/line?id={line_id}",
        }
        post_url = f"{self.base_url}/post.php?action=line"
        res = self.session.post(post_url, files=multipart_fields, headers=post_headers, timeout=15)
        res.raise_for_status()

        try:
            body = res.json()
            if isinstance(body, dict) and body.get("result") is False:
                raise XUIClientError(body.get("message") or body.get("error") or "XUI rejected the line extension.")
        except ValueError:
            if "/login" in res.url or 'name="password"' in res.text:
                raise XUIClientError("XUI session expired during save. Please paste a fresh PHPSESSID.")

        return line_id, new_exp_formatted, pkg_id, pkg_name