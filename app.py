from flask import Response
"""
IPTV Subscriber Manager — Flask Web Application.

Manages local customer billing records in MariaDB/MySQL, generates Gmail renewal
and announcement links for 613koditv@gmail.com, and synchronizes credit-checked
line extensions with the XUI One reseller panel.
"""

import hmac
from werkzeug.security import check_password_hash
import os
import re
import calendar
from datetime import datetime, date, timedelta
from flask import jsonify, Flask, render_template, request, redirect, url_for, flash, session
import mysql.connector
from dotenv import load_dotenv
from xui_client import XUIClient, XUIClientError

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)

TABLE = os.getenv("TABLE_NAME", "subscribers")

REMINDER_SUBJECT = "Subscription Renewal Reminder - Ottawa IPTV"
REMINDER_BODY = """Hello,
If you would like to continue your current subscription could you please send your payment along per your usual method.

Additionally, you now have the option of setting up a pre-authorized payment. You can use the link below to setup a monthly recurring payment for your account.

https://www.paypal.com/webapps/billing/plans/subscribe?plan_id=P-2MM46206S1144363TMUE273Q

If you choose to setup pre-authorized payments, your account will be credited with a free month.

If you have any questions please let me know.

Cheers
Ottawa IPTV"""


app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")


def is_authenticated():
    return session.get("authenticated") is True and session.get("user") == ADMIN_USER


def verify_admin_credentials(username, password):
    if not hmac.compare_digest((username or "").strip(), ADMIN_USER):
        return False
    if ADMIN_PASSWORD_HASH:
        return check_password_hash(ADMIN_PASSWORD_HASH, password or "")
    if ADMIN_PASSWORD:
        return hmac.compare_digest(password or "", ADMIN_PASSWORD)
    return False


@app.before_request
def require_login():
    if request.endpoint in ("login", "static"):
        return None
    if not ADMIN_PASSWORD_HASH and not ADMIN_PASSWORD:
        # Fail closed if no admin password is configured
        if request.endpoint != "login":
            flash("ADMIN_PASSWORD_HASH is not configured in environment.", "warning")
            return redirect(url_for("login"))
    if not is_authenticated():
        next_target = request.full_path if request.query_string else request.path
        return redirect(url_for("login", next=next_target))


@app.route("/login", methods=["GET", "POST"])
def login():
    if is_authenticated():
        return redirect(url_for("index"))
    next_url = request.args.get("next") or request.form.get("next") or url_for("index")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = url_for("index")

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if verify_admin_credentials(username, password):
            session.clear()
            session["authenticated"] = True
            session["user"] = ADMIN_USER
            session.permanent = request.form.get("remember_me") == "on"
            return redirect(next_url)
        flash("Invalid username or password.", "error")

    return render_template("login.html", next_url=next_url)


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("login"))


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", 3306)),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

def ensure_settings_table(conn):
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            setting_key VARCHAR(64) PRIMARY KEY,
            setting_value TEXT
        )
    """)
    conn.commit()
    cursor.close()

def get_setting(conn, key, default=""):
    ensure_settings_table(conn)
    cursor = conn.cursor()
    cursor.execute("SELECT setting_value FROM app_settings WHERE setting_key=%s", (key,))
    row = cursor.fetchone()
    cursor.close()
    return row[0] if row and row[0] is not None else default

def set_setting(conn, key, value):
    ensure_settings_table(conn)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO app_settings (setting_key, setting_value)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)
    """, (key, value))
    conn.commit()
    cursor.close()

def get_end_of_month(today):
    last_day = calendar.monthrange(today.year, today.month)[1]
    return date(today.year, today.month, last_day)

def parse_date_string(expiry_str):
    if not expiry_str:
        return None
    cleaned = " ".join(expiry_str.strip().split())
    if cleaned.lower() in ("na", "n/a", "-----", "0000-00-00", ""):
        return None
    for fmt in ("%b %d %Y", "%B %d %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None

def add_months(source_date, months):
    month_idx = source_date.month - 1 + months
    year = source_date.year + month_idx // 12
    month = month_idx % 12 + 1
    day = min(source_date.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)

def format_db_date(dt):
    return f"{dt.strftime('%b').lower()} {dt.day} {dt.year}"

def parse_expiry_info(expiry_str):
    dt = parse_date_string(expiry_str)
    if not dt:
        return "other", None
    today = date.today()
    eom = get_end_of_month(today)
    next_month_first = eom + timedelta(days=1)
    if dt < today:
        return "expired", dt
    elif dt <= next_month_first:
        return "expiring_month", dt
    else:
        return "active", dt

def is_trial_subscriber(sub):
    haystack = " ".join([
        str(sub.get("userid") or ""),
        str(sub.get("notes") or ""),
        str(sub.get("firstname") or ""),
        str(sub.get("lastname") or ""),
    ]).lower()
    return "trial" in haystack

def is_valid_email(val):
    return bool(val and "@" in val and "." in val.split("@")[-1])

@app.route("/")
def index():
    search = request.args.get("search", "").strip()
    active_filter = request.args.get("filter", "all").strip()

    conn = get_db_connection()
    xui_phpsessid = get_setting(conn, "xui_phpsessid", os.getenv("XUI_PHPSESSID", ""))
    xui_enabled = get_setting(conn, "xui_enabled", os.getenv("XUI_ENABLED", "true")) == "true"

    cursor = conn.cursor(dictionary=True)
    if search:
        query = f"""
            SELECT * FROM {TABLE} 
            WHERE firstname LIKE %s OR lastname LIKE %s OR email LIKE %s OR userid LIKE %s OR notes LIKE %s 
            ORDER BY id DESC
        """
        wildcard = f"%{search}%"
        cursor.execute(query, (wildcard, wildcard, wildcard, wildcard, wildcard))
    else:
        cursor.execute(f"SELECT * FROM {TABLE} ORDER BY id DESC")
        
    all_subscribers = cursor.fetchall()
    cursor.close()
    conn.close()

    counts = {
        "all": len(all_subscribers),
        "active": 0,
        "expiring_month": 0,
        "expired": 0,
        "other": 0,
    }

    filtered_subscribers = []
    expiring_month_list = []

    for sub in all_subscribers:
        status, parsed_dt = parse_expiry_info(sub.get("expirydate", ""))
        sub["expiry_status"] = status
        sub["parsed_date"] = parsed_dt
        sub["has_valid_email"] = is_valid_email(sub.get("email", ""))

        if status in ("active", "expiring_month"):
            counts["active"] += 1
        if status == "expiring_month":
            counts["expiring_month"] += 1
            expiring_month_list.append(sub)
        elif status == "expired":
            counts["expired"] += 1
        elif status == "other":
            counts["other"] += 1

        if active_filter == "all":
            filtered_subscribers.append(sub)
        elif active_filter == "active" and status in ("active", "expiring_month"):
            filtered_subscribers.append(sub)
        elif active_filter == status:
            filtered_subscribers.append(sub)

    valid_bcc_emails = [s["email"].strip() for s in expiring_month_list if s["has_valid_email"]]

    uid_members = {}
    for s in all_subscribers:
        if is_trial_subscriber(s):
            continue
        uid_k = (s.get("userid") or "").strip().lower()
        if uid_k:
            uid_members.setdefault(uid_k, []).append(s)

    groups_map = {}
    for s in all_subscribers:
        if s.get("expiry_status") not in ("expiring_month", "expired") or is_trial_subscriber(s):
            continue
        uid_raw = (s.get("userid") or "").strip()
        uid_k = uid_raw.lower()
        group_key = uid_k if uid_k else f"__single_{s['id']}"

        if group_key not in groups_map:
            members = uid_members.get(uid_k, [s]) if uid_k else [s]
            members_sorted = sorted(
                members,
                key=lambda m: (
                    (m.get("lastname") or "").strip().lower() or "~",
                    (m.get("firstname") or "").strip().lower() or "~",
                    m["id"],
                ),
            )
            primary = members_sorted[0]
            groups_map[group_key] = {
                "group_key": group_key,
                "userid": uid_raw,
                "is_shared": len(members_sorted) > 1,
                "shared_count": len(members_sorted),
                "members": members_sorted,
                "primary": primary,
                "sort_key": (
                    (primary.get("lastname") or "").strip().lower() or "~",
                    (primary.get("firstname") or "").strip().lower() or "~",
                    primary["id"],
                ),
            }

    bulk_extend_candidates = sorted(groups_map.values(), key=lambda g: g["sort_key"])
    bulk_extend_total_people = sum(len(g["members"]) for g in bulk_extend_candidates)


    all_bcc_emails = sorted({
        (s.get("email") or "").strip()
        for s in all_subscribers
        if (s.get("email") or "").strip() and "@" in (s.get("email") or "")
    })
    active_bcc_emails = sorted({
        (s.get("email") or "").strip()
        for s in all_subscribers
        if s.get("expiry_status") in ("active", "expiring_month")
        and (s.get("email") or "").strip()
        and "@" in (s.get("email") or "")
    })
    return render_template(
        "index.html",
        all_bcc_emails=all_bcc_emails,
        active_bcc_emails=active_bcc_emails,
        subscribers=filtered_subscribers,
        search=search,
        active_filter=active_filter,
        counts=counts,
        expiring_month_list=expiring_month_list,
        bulk_extend_candidates=bulk_extend_candidates,
        bulk_extend_total_people=bulk_extend_total_people,
        valid_bcc_emails=", ".join(valid_bcc_emails),
        reminder_subject=REMINDER_SUBJECT,
        reminder_body=REMINDER_BODY,
        current_month_label=date.today().strftime("%B %Y"),
        xui_phpsessid=xui_phpsessid,
        xui_enabled=xui_enabled,
    )

@app.route("/xui-session", methods=["POST"])
def update_xui_session():
    phpsessid = request.form.get("phpsessid", "").strip()
    # Allow user to paste either raw cookie value or full 'PHPSESSID=xyz' string
    if "PHPSESSID=" in phpsessid:
        phpsessid = phpsessid.split("PHPSESSID=", 1)[1].split(";", 1)[0].strip()

    enabled = "true" if request.form.get("xui_enabled") == "on" else "false"
    action_type = request.form.get("action_type", "save")

    conn = get_db_connection()
    set_setting(conn, "xui_phpsessid", phpsessid)
    set_setting(conn, "xui_enabled", enabled)
    conn.close()

    if action_type == "test" or enabled == "true":
        try:
            client = XUIClient(phpsessid)
            client.verify_session()
            flash("XUI session saved and verified — connected to iptvottawa.ca!", "success")
        except Exception as e:
            flash(f"XUI Session Saved, but verification failed: {e}", "warning")
    else:
        flash("XUI sync disabled (local DB mode only).", "success")

    return redirect(url_for("index"))

@app.route("/extend/<int:sub_id>", methods=["POST"])
def extend_subscriber(sub_id):
    """Extend a single subscriber's local DB expiry date only (does not touch XUI or shared rows)."""
    months = int(request.form.get("months", 1))
    current_filter = request.form.get("filter", "all")
    current_search = request.form.get("search", "")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(f"SELECT firstname, lastname, userid, expirydate FROM {TABLE} WHERE id=%s", (sub_id,))
    sub = cursor.fetchone()

    if sub:
        existing_dt = parse_date_string(sub.get("expirydate", ""))
        base_date = existing_dt if existing_dt else date.today()
        new_dt = add_months(base_date, months)
        new_expiry_str = format_db_date(new_dt)

        cursor.execute(f"UPDATE {TABLE} SET expirydate=%s WHERE id=%s", (new_expiry_str, sub_id))
        conn.commit()

        label = "1 year" if months == 12 else f"{months} month{'s' if months > 1 else ''}"
        flash(
            f"Extended {sub['firstname']} {sub['lastname']} (#{sub_id}) locally by {label} "
            f"({format_db_date(base_date)} -> {new_expiry_str})",
            "success",
        )

    cursor.close()
    conn.close()
    return redirect(url_for("index", filter=current_filter, search=current_search))


@app.route("/bulk-extend", methods=["POST"])
def bulk_extend_subscribers():
    sub_ids = request.form.getlist("sub_ids")
    months = int(request.form.get("months", 1))
    current_filter = request.form.get("filter", "all")
    current_search = request.form.get("search", "")

    if not sub_ids:
        flash("No subscribers selected for bulk extension.", "warning")
        return redirect(url_for("index", filter=current_filter, search=current_search))

    conn = get_db_connection()
    xui_phpsessid = get_setting(conn, "xui_phpsessid", os.getenv("XUI_PHPSESSID", ""))
    xui_enabled = get_setting(conn, "xui_enabled", os.getenv("XUI_ENABLED", "true")) == "true"

    client = None
    if xui_enabled:
        try:
            client = XUIClient(xui_phpsessid)
            client.verify_session()
        except Exception as e:
            conn.close()
            flash(f"Bulk Extend Aborted — XUI session check failed: {e}", "warning")
            return redirect(url_for("index", filter=current_filter, search=current_search))

    cursor = conn.cursor(dictionary=True)
    succeeded = []
    failed = []
    processed_userids = set()
    label = "1 year" if months == 12 else f"{months} month{'s' if months > 1 else ''}"

    for raw_id in sub_ids:
        try:
            sub_id = int(raw_id)
        except ValueError:
            continue

        cursor.execute(f"SELECT id, firstname, lastname, userid, expirydate, notes FROM {TABLE} WHERE id=%s", (sub_id,))
        sub = cursor.fetchone()
        if not sub:
            continue

        if is_trial_subscriber(sub):
            failed.append(f"{sub['userid'] or sub_id} (skipped trial account)")
            continue

        uid_clean = (sub.get("userid") or "").strip()
        uid_key = uid_clean.lower()

        # Hard guard: never extend the same userid more than once per batch
        if uid_key and uid_key in processed_userids:
            continue
        if uid_key:
            processed_userids.add(uid_key)

        existing_dt = parse_date_string(sub.get("expirydate", ""))
        base_date = existing_dt if existing_dt else date.today()
        new_dt = add_months(base_date, months)
        new_expiry_str = format_db_date(new_dt)

        if xui_enabled and client:
            try:
                client.extend_line(uid_clean, months, new_dt)
            except Exception as e:
                failed.append(f"{uid_clean or sub['firstname']} ({e})")
                continue

        if uid_clean:
            cursor.execute(
                f"UPDATE {TABLE} SET expirydate=%s WHERE LOWER(TRIM(userid))=LOWER(%s)",
                (new_expiry_str, uid_clean),
            )
            updated_rows = cursor.rowcount
        else:
            cursor.execute(f"UPDATE {TABLE} SET expirydate=%s WHERE id=%s", (new_expiry_str, sub_id))
            updated_rows = 1
        conn.commit()

        suffix = f" ({updated_rows} shared rows)" if updated_rows > 1 else ""
        succeeded.append(f"{uid_clean or sub['firstname']} -> {new_expiry_str}{suffix}")

    cursor.close()
    conn.close()

    if succeeded:
        flash(f"Bulk extended {len(succeeded)} account(s) by {label}: {', '.join(succeeded[:8])}{'...' if len(succeeded) > 8 else ''}", "success")
    if failed:
        flash(f"Skipped/Failed ({len(failed)}): {'; '.join(failed)}", "warning")

    return redirect(url_for("index", filter=current_filter, search=current_search))

@app.route("/add", methods=["POST"])
def add_subscriber():
    firstname = request.form["firstname"].strip()
    lastname = request.form["lastname"].strip()
    email = request.form["email"].strip()
    userid = request.form["userid"].strip()
    expirydate = request.form["expirydate"].strip()
    connections = int(request.form.get("connections") or 1)
    notes = request.form["notes"].strip()

    conn = get_db_connection()
    cursor = conn.cursor()
    query = f"""
        INSERT INTO {TABLE} (firstname, lastname, email, userid, expirydate, connections, notes) 
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """
    cursor.execute(query, (firstname, lastname, email, userid, expirydate, connections, notes))
    conn.commit()
    cursor.close()
    conn.close()
    
    flash(f"Added subscriber {firstname} {lastname}.", "success")
    return redirect(url_for("index"))

@app.route("/update/<int:sub_id>", methods=["POST"])
def update_subscriber(sub_id):
    firstname = request.form["firstname"].strip()
    lastname = request.form["lastname"].strip()
    email = request.form["email"].strip()
    userid = request.form["userid"].strip()
    expirydate = request.form["expirydate"].strip()
    connections = int(request.form.get("connections") or 1)
    notes = request.form["notes"].strip()

    conn = get_db_connection()
    cursor = conn.cursor()
    query = f"""
        UPDATE {TABLE} 
        SET firstname=%s, lastname=%s, email=%s, userid=%s, expirydate=%s, connections=%s, notes=%s 
        WHERE id=%s
    """
    cursor.execute(query, (firstname, lastname, email, userid, expirydate, connections, notes, sub_id))
    conn.commit()
    cursor.close()
    conn.close()
    
    flash(f"Updated subscriber #{sub_id} ({firstname} {lastname}).", "success")
    return redirect(url_for("index"))

@app.route("/delete/<int:sub_id>", methods=["POST"])
def delete_subscriber(sub_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(f"DELETE FROM {TABLE} WHERE id=%s", (sub_id,))
    conn.commit()
    cursor.close()
    conn.close()
    
    flash(f"Deleted subscriber #{sub_id}.", "warning")
    return redirect(url_for("index"))


@app.route("/xui/expiring-week", methods=["GET"])
def xui_expiring_week():
    """Return JSON payload of live XUI credits and lines expiring within 7 days."""
    if "user" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    conn = get_db_connection()
    try:
        xui_phpsessid = get_setting(conn, "xui_phpsessid", os.getenv("XUI_PHPSESSID", ""))
        xui_enabled = get_setting(conn, "xui_enabled", os.getenv("XUI_ENABLED", "true")) == "true"
        if not xui_enabled:
            return jsonify({"error": "XUI sync is currently disabled."}), 400

        client = XUIClient(xui_phpsessid)
        credits = client.get_credits()
        lines = client.get_lines_expiring_within(days=7, include_recently_expired_days=3)

        # Enrich with local subscriber info if userid matches
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"SELECT id, firstname, lastname, email, userid, expirydate FROM {TABLE} WHERE userid IS NOT NULL AND userid != ''")
        local_map = {}
        for row in cursor.fetchall():
            uid_key = (row.get("userid") or "").strip().lower()
            if uid_key and uid_key not in local_map:
                local_map[uid_key] = row

        for item in lines:
            match = local_map.get(item["username"].lower())
            if match:
                item["local_id"] = match["id"]
                item["local_name"] = f"{match.get('firstname', '')} {match.get('lastname', '')}".strip()
                item["local_email"] = match.get("email") or ""
                item["local_expiry"] = match.get("expirydate") or ""
            else:
                item["local_id"] = None
                item["local_name"] = ""
                item["local_email"] = ""
                item["local_expiry"] = ""

        return jsonify({"credits": credits, "lines": lines})
    except XUIClientError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Unexpected XUI error: {e}"}), 500
    finally:
        conn.close()


@app.route("/xui/extend-week", methods=["POST"])
def xui_extend_week():
    """Extend selected XUI lines after verifying available reseller credits (XUI-only)."""
    if "user" not in session:
        return redirect(url_for("login"))

    usernames = [u.strip() for u in request.form.getlist("usernames") if u.strip()]
    # Deduplicate preserving order
    seen = set()
    unique_users = []
    for u in usernames:
        if u.lower() not in seen:
            seen.add(u.lower())
            unique_users.append(u)

    try:
        months = int(request.form.get("months", 1))
    except ValueError:
        months = 1
    if months not in (1, 3, 12):
        months = 1

    if not unique_users:
        flash("No XUI accounts selected to extend.", "warning")
        return redirect(url_for("index"))

    conn = get_db_connection()
    try:
        xui_phpsessid = get_setting(conn, "xui_phpsessid", os.getenv("XUI_PHPSESSID", ""))
        xui_enabled = get_setting(conn, "xui_enabled", os.getenv("XUI_ENABLED", "true")) == "true"
        if not xui_enabled:
            flash("XUI sync is disabled.", "warning")
            return redirect(url_for("index"))

        client = XUIClient(xui_phpsessid)
        available_credits = client.get_credits()
        required_credits = len(unique_users) * months

        if available_credits < required_credits:
            flash(
                f"Insufficient XUI credits: need {required_credits} credit(s) for {len(unique_users)} account(s) x {months}M, "
                f"but only {available_credits} credit(s) available.",
                "warning",
            )
            return redirect(url_for("index"))

        succeeded = []
        skipped = []
        failed = []

        for uname in unique_users:
            try:
                line_id, new_xui_exp, pkg_id, pkg_name = client.extend_line(uname, months, datetime.now())
                succeeded.append(f"{uname} (-> {new_xui_exp.split()[0]})")
            except Exception as e:
                msg = str(e)
                if "already active" in msg.lower():
                    skipped.append(uname)
                else:
                    failed.append(f"{uname}: {msg}")

        conn.commit()
        remaining = available_credits - (len(succeeded) * months)
        if succeeded:
            flash(
                f"Extended {len(succeeded)} XUI account(s) (+{months}M, est. {remaining} credits left): {', '.join(succeeded)}",
                "success",
            )
        if skipped:
            flash(f"Skipped already-active XUI account(s): {', '.join(skipped)}", "warning")
        if failed:
            flash(f"Failed XUI extension(s): {'; '.join(failed)}", "warning")
    except XUIClientError as e:
        flash(f"XUI Error: {e}", "warning")
    finally:
        conn.close()

    return redirect(url_for("index"))



@app.route("/xui/next-available-slot", methods=["GET"])
def xui_next_available_slot():
    """
    Find all XUI accounts matching the 4 slot-allocation rules:
      1. At most 3 shared local accounts (current_accounts + 1 <= 3)
      2. Maximum of 5 active/allocated connections on the XUI account
      3. Not a trial (or disabled/banned) XUI account
      4. Has room for the requested number of connections
    """
    if "user" not in session:
        return jsonify({"error": "Not authenticated"}), 401

    try:
        needed_conn = max(1, int(request.args.get("connections", 1)))
    except ValueError:
        needed_conn = 1

    try:
        exclude_sub_id = int(request.args.get("exclude_id", 0))
    except ValueError:
        exclude_sub_id = 0

    if needed_conn > 5:
        return jsonify({"error": "Requested connections exceed the maximum of 5 per XUI account."}), 400

    conn = get_db_connection()
    try:
        xui_phpsessid = get_setting(conn, "xui_phpsessid", os.getenv("XUI_PHPSESSID", ""))
        xui_enabled = get_setting(conn, "xui_enabled", os.getenv("XUI_ENABLED", "true")) == "true"
        if not xui_enabled:
            return jsonify({"error": "XUI sync is disabled — enable XUI Sync to check live XUI accounts."}), 400

        client = XUIClient(xui_phpsessid)
        xui_lines = client.get_all_lines(include_trials=False)

        # Aggregate current local subscriber counts & connections per userid
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"SELECT id, userid, connections, expirydate, notes FROM {TABLE}")
        usage_map = {}
        for row in cursor.fetchall():
            if exclude_sub_id and row.get("id") == exclude_sub_id:
                continue
            uid = (row.get("userid") or "").strip().lower()
            if not uid:
                continue
            exp_val = (row.get("expirydate") or "").strip().lower()
            notes_val = (row.get("notes") or "").strip().lower()
            # Ignore clearly cancelled entries so dead slots do not block active capacity
            if exp_val in ("-----", "0000-00-00") or notes_val.startswith("cancelled"):
                continue
            try:
                c_val = max(1, int(row.get("connections") or 1))
            except (TypeError, ValueError):
                c_val = 1
            entry = usage_map.setdefault(uid, {"accounts": 0, "connections": 0})
            entry["accounts"] += 1
            entry["connections"] += c_val

        def natural_sort_key(uname):
            parts = re.split(r"(\d+)", uname.lower())
            return [int(p) if p.isdigit() else p for p in parts]

        candidates = []
        for line in xui_lines:
            uname = (line.get("username") or "").strip()
            if not uname:
                continue
            # Rule 3: Not a trial or disabled account
            if line.get("is_trial") or line.get("is_disabled"):
                continue

            # Rule 2: Max 5 connections cap on the XUI account
            xui_max = min(5, int(line.get("max_connections") or 5))
            xui_active = int(line.get("active_connections") or 0)
            if xui_active + needed_conn > 5:
                continue

            local = usage_map.get(uname.lower(), {"accounts": 0, "connections": 0})
            used_accounts = local["accounts"]
            used_conns = local["connections"]

            # Rule 1: At most 3 shared accounts after adding this subscriber
            if used_accounts + 1 > 3:
                continue

            # Rule 4: Must have room for the new number of connections (capped at 5 / xui_max)
            effective_cap = min(5, xui_max)
            if used_conns + needed_conn > effective_cap:
                continue

            candidates.append({
                "userid": uname,
                "used_accounts": used_accounts,
                "max_accounts": 3,
                "used_connections": used_conns,
                "max_connections": effective_cap,
                "xui_active_connections": xui_active,
                "exp_date": line.get("exp_date", ""),
            })

        candidates.sort(key=lambda c: natural_sort_key(c["userid"]))

        if not candidates:
            return jsonify({
                "error": f"No eligible non-trial XUI account found with <3 accounts and room for +{needed_conn} connection(s)."
            }), 404

        return jsonify({
            "slot": candidates[0],
            "candidates": candidates,
        })
    except XUIClientError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Error finding available slot: {e}"}), 500
    finally:
        conn.close()



@app.route("/backup-db", methods=["GET"])
def backup_database():
    """Generate and download a timestamped SQL dump of the subscribers table."""
    if "user" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(f"SHOW CREATE TABLE `{TABLE}`")
        create_row = cursor.fetchone()
        create_sql = list(create_row.values())[1] if create_row else ""

        cursor.execute(f"SELECT * FROM `{TABLE}` ORDER BY id ASC")
        rows = cursor.fetchall()

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        file_ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")

        def sql_escape(val):
            if val is None:
                return "NULL"
            if isinstance(val, (int, float)):
                return str(val)
            s = str(val)
            s = (
                s.replace("\\", "\\\\")
                .replace("'", "\\'")
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\x00", "\\0")
            )
            return f"'{s}'"

        lines = [
            "-- IPTV Subscriber Manager SQL Backup",
            f"-- Generated: {now_str}",
            f"-- Table: {TABLE} ({len(rows)} rows)",
            "",
            'SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";',
            "START TRANSACTION;",
            'SET time_zone = "+00:00";',
            "",
            f"DROP TABLE IF EXISTS `{TABLE}`;",
            f"{create_sql};",
            "",
        ]

        if rows:
            cols = list(rows[0].keys())
            col_list = ", ".join(f"`{c}`" for c in cols)
            lines.append(f"INSERT INTO `{TABLE}` ({col_list}) VALUES")
            value_tuples = []
            for r in rows:
                vals = ", ".join(sql_escape(r[c]) for c in cols)
                value_tuples.append(f"  ({vals})")
            lines.append(",\n".join(value_tuples) + ";")
            lines.append("")

        lines.append("COMMIT;")
        dump_content = "\n".join(lines) + "\n"

        raw_name = (request.args.get("filename") or "").strip()
        if raw_name:
            # Strip directory traversal & invalid filename characters
            clean_name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(raw_name)).strip("._")
            if not clean_name:
                clean_name = f"subscribers_backup_{file_ts}.sql"
            elif not clean_name.lower().endswith(".sql"):
                clean_name += ".sql"
        else:
            clean_name = f"subscribers_backup_{file_ts}.sql"

        return Response(
            dump_content,
            mimetype="application/sql",
            headers={
                "Content-Disposition": f'attachment; filename="{clean_name}"'
            },
        )
    finally:
        cursor.close()
        conn.close()



@app.route("/system-status", methods=["GET"])
def system_status():
    """
    Check live availability and latency of:
      1. XUI Admin Panel (HTTP reachability + active PHPSESSID login check)
      2. Web Browser Player at iptvottawa.ca/webplayer
    """
    if "user" not in session:
        return jsonify({"error": "Not authenticated"}), 401

    import time
    from concurrent.futures import ThreadPoolExecutor
    import requests as http_req

    conn = get_db_connection()
    try:
        xui_phpsessid = get_setting(conn, "xui_phpsessid", os.getenv("XUI_PHPSESSID", ""))
    finally:
        conn.close()

    def check_xui():
        t0 = time.monotonic()
        client = XUIClient(xui_phpsessid)
        base_url = client.base_url
        try:
            # 1. Check if the XUI panel host/login page responds over HTTP
            probe = http_req.get(f"{base_url}/login", timeout=8, allow_redirects=True)
            latency_ms = int((time.monotonic() - t0) * 1000)
            if probe.status_code >= 500:
                return {
                    "state": "down",
                    "label": f"Offline (HTTP {probe.status_code})",
                    "detail": f"{base_url} returned HTTP {probe.status_code}",
                    "latency_ms": latency_ms,
                    "url": base_url,
                }
        except Exception as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return {
                "state": "down",
                "label": "Offline / Unreachable",
                "detail": str(e),
                "latency_ms": latency_ms,
                "url": base_url,
            }

        # 2. Panel is online — verify whether we can log in / session is valid
        if not xui_phpsessid:
            return {
                "state": "warn",
                "label": "Online · Login Required",
                "detail": "Panel is reachable, but no XUI PHPSESSID is configured.",
                "latency_ms": latency_ms,
                "url": base_url,
            }
        try:
            client.verify_session()
            latency_ms = int((time.monotonic() - t0) * 1000)
            return {
                "state": "ok",
                "label": f"Online & Logged In ({latency_ms}ms)",
                "detail": f"Authenticated session active at {base_url}",
                "latency_ms": latency_ms,
                "url": base_url,
            }
        except Exception as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return {
                "state": "warn",
                "label": "Online · Session Expired",
                "detail": f"Panel is online, but login check failed: {e}",
                "latency_ms": latency_ms,
                "url": base_url,
            }

    def check_webplayer():
        player_url = os.getenv("WEBPLAYER_URL", "https://iptvottawa.ca/webplayer").strip()
        t0 = time.monotonic()
        try:
            resp = http_req.get(
                player_url,
                timeout=10,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (IPTV-Subscriber-Manager-HealthCheck)"},
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            body_low = (resp.text or "")[:8000].lower()

            if resp.status_code >= 400:
                return {
                    "state": "down",
                    "label": f"Offline (HTTP {resp.status_code})",
                    "detail": f"{player_url} returned HTTP {resp.status_code}",
                    "latency_ms": latency_ms,
                    "url": player_url,
                }

            # Verify valid webplayer HTML/assets loaded rather than a blank/error page
            has_html = ("<html" in body_low or "<!doctype" in body_low) and len(body_low.strip()) > 100
            if not has_html:
                return {
                    "state": "warn",
                    "label": f"Degraded ({latency_ms}ms)",
                    "detail": f"HTTP {resp.status_code}, but response body was unexpectedly empty.",
                    "latency_ms": latency_ms,
                    "url": player_url,
                }

            return {
                "state": "ok",
                "label": f"Online & Working ({latency_ms}ms)",
                "detail": f"HTTP {resp.status_code} OK at {resp.url}",
                "latency_ms": latency_ms,
                "url": player_url,
            }
        except Exception as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return {
                "state": "down",
                "label": "Offline / Unreachable",
                "detail": str(e),
                "latency_ms": latency_ms,
                "url": player_url,
            }

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_xui = pool.submit(check_xui)
        f_web = pool.submit(check_webplayer)
        xui_res = f_xui.result()
        web_res = f_web.result()

    return jsonify({
        "xui": xui_res,
        "webplayer": web_res,
        "checked_at": datetime.now().strftime("%I:%M:%S %p").lstrip("0"),
    })



import threading
_TG_LOCK = threading.Lock()
TG_SESSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tg_session", "iptv_monitor")


def _parse_target_chat(raw_chat_id):
    s = str(raw_chat_id or "-1001998504661").strip()
    if s.lstrip("-").isdigit():
        return int(s)
    return s


@app.route("/telegram/settings", methods=["GET", "POST"])
def telegram_settings():
    """Get or save Telegram API credentials and target Chat ID."""
    if "user" not in session:
        return jsonify({"error": "Not authenticated"}), 401

    conn = get_db_connection()
    try:
        if request.method == "POST":
            data = request.get_json(silent=True) or request.form
            api_id = (data.get("api_id") or "").strip()
            api_hash = (data.get("api_hash") or "").strip()
            chat_id = (data.get("chat_id") or "").strip()

            if api_id:
                set_setting(conn, "telegram_api_id", api_id)
            else:
                api_id = get_setting(conn, "telegram_api_id", os.getenv("TELEGRAM_API_ID", "35559518"))

            if api_hash:
                set_setting(conn, "telegram_api_hash", api_hash)
            else:
                api_hash = get_setting(conn, "telegram_api_hash", os.getenv("TELEGRAM_API_HASH", "5fb131947616412fdb86ed69863c8ecf"))

            if chat_id:
                set_setting(conn, "telegram_chat_id", chat_id)
            elif not get_setting(conn, "telegram_chat_id", ""):
                set_setting(conn, "telegram_chat_id", "-1001998504661")

            return jsonify({"ok": True, "configured": bool(api_id and api_hash)})

        api_id = get_setting(conn, "telegram_api_id", os.getenv("TELEGRAM_API_ID", "35559518"))
        api_hash = get_setting(conn, "telegram_api_hash", os.getenv("TELEGRAM_API_HASH", "5fb131947616412fdb86ed69863c8ecf"))
        chat_id = get_setting(conn, "telegram_chat_id", os.getenv("TELEGRAM_CHAT_ID", "-1001998504661"))
        session_exists = os.path.exists(TG_SESSION_PATH + ".session")
        masked = (api_hash[:6] + "..." + api_hash[-4:]) if len(api_hash) > 10 else ("Saved" if api_hash else "")
        return jsonify({
            "configured": bool(api_id and api_hash and session_exists),
            "session_ready": session_exists,
            "api_id": api_id,
            "masked_hash": masked,
            "chat_id": chat_id,
        })
    finally:
        conn.close()


@app.route("/telegram/messages", methods=["GET"])
def telegram_messages():
    """Fetch the 25 most recent messages (across all sub-chat topics) via Telethon."""
    if "user" not in session:
        return jsonify({"error": "Not authenticated"}), 401

    conn = get_db_connection()
    try:
        api_id_str = get_setting(conn, "telegram_api_id", os.getenv("TELEGRAM_API_ID", "35559518")).strip()
        api_hash = get_setting(conn, "telegram_api_hash", os.getenv("TELEGRAM_API_HASH", "5fb131947616412fdb86ed69863c8ecf")).strip()
        target_chat_raw = get_setting(conn, "telegram_chat_id", os.getenv("TELEGRAM_CHAT_ID", "-1001998504661")).strip() or "-1001998504661"
        topic_filter = get_setting(conn, "telegram_topic_filter", "5, 6").strip()
    finally:
        conn.close()

    session_file = TG_SESSION_PATH + ".session"
    if not api_id_str or not api_hash or not os.path.exists(session_file):
        return jsonify({
            "configured": False,
            "messages": [],
            "error": "Telegram personal session not authorized yet. Run the 1-time login command in Terminal.",
        })

    import asyncio
    from telethon import TelegramClient
    try:
        from telethon.tl.functions.messages import GetForumTopicsByIDRequest
    except ImportError:
        try:
            from telethon.tl.functions.channels import GetForumTopicsByIDRequest
        except ImportError:
            GetForumTopicsByIDRequest = None

    async def _fetch():
        client = TelegramClient(TG_SESSION_PATH, int(api_id_str), api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return {"authorized": False, "messages": [], "chat_title": ""}

            entity = await client.get_entity(_parse_target_chat(target_chat_raw))
            chat_title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(target_chat_raw)

            parts = [p.strip() for p in topic_filter.split(",") if p.strip()] if topic_filter else []
            numeric_topic_ids = [int(p) for p in parts if p.isdigit()] if (parts and all(p.isdigit() for p in parts)) else []

            msg_topic_override = {}
            if numeric_topic_ids:
                seen_ids = set()
                raw_msgs = []
                for tid in numeric_topic_ids:
                    batch = await client.get_messages(entity, limit=25, reply_to=tid)
                    for m in batch:
                        if m.id not in seen_ids:
                            seen_ids.add(m.id)
                            msg_topic_override[m.id] = tid
                            raw_msgs.append(m)
                raw_msgs.sort(key=lambda m: (m.date.timestamp() if getattr(m, "date", None) else 0, m.id), reverse=True)
            else:
                raw_msgs = await client.get_messages(entity, limit=120 if topic_filter else 25)

            topic_ids = set(numeric_topic_ids)
            for m in raw_msgs:
                rto = getattr(m, "reply_to", None)
                if rto and getattr(rto, "forum_topic", False):
                    tid = getattr(rto, "reply_to_top_id", None) or getattr(rto, "reply_to_msg_id", None)
                    if tid:
                        topic_ids.add(int(tid))

            topic_names = {1: "General"}
            if GetForumTopicsByIDRequest and topic_ids and getattr(entity, "forum", False):
                try:
                    res = await client(GetForumTopicsByIDRequest(channel=entity, topics=list(topic_ids)))
                    for t in getattr(res, "topics", []):
                        tid = getattr(t, "id", None)
                        ttitle = getattr(t, "title", None)
                        if tid and ttitle:
                            topic_names[int(tid)] = ttitle
                except Exception:
                    pass

            formatted = []
            for m in raw_msgs:
                text = (getattr(m, "message", None) or "").strip()
                if not text:
                    if getattr(m, "photo", None):
                        text = "[Photo]"
                    elif getattr(m, "document", None) or getattr(m, "video", None):
                        text = "[Media / File]"
                    else:
                        continue

                sender = await m.get_sender()
                if sender:
                    sname = (
                        " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]))
                        or getattr(sender, "title", None)
                        or getattr(sender, "username", None)
                        or "Member"
                    )
                else:
                    sname = chat_title

                topic_label = ""
                rto = getattr(m, "reply_to", None)
                if rto and getattr(rto, "forum_topic", False):
                    tid = getattr(rto, "reply_to_top_id", None) or getattr(rto, "reply_to_msg_id", None)
                    if tid:
                        topic_label = topic_names.get(int(tid), f"Topic #{tid}")
                elif getattr(entity, "forum", False):
                    topic_label = "General"

                if numeric_topic_ids:
                    tid = msg_topic_override.get(m.id, numeric_topic_ids[0])
                    topic_label = topic_names.get(tid, f"Topic #{tid}")
                elif topic_filter:
                    tl_low = (topic_label or "").lower()
                    tf_low = topic_filter.lower()
                    if tf_low not in tl_low and tl_low not in tf_low:
                        continue

                dt_local = m.date.astimezone() if getattr(m, "date", None) else None
                time_label = dt_local.strftime("%b %d, %I:%M %p") if dt_local else ""

                formatted.append({
                    "id": int(m.id),
                    "chat_id": str(target_chat_raw),
                    "chat_title": f"{chat_title} · {topic_label}" if topic_label else chat_title,
                    "sender_name": sname,
                    "text": text,
                    "time_label": time_label,
                })
                if len(formatted) >= 25:
                    break

            return {"authorized": True, "messages": formatted, "chat_title": chat_title}
        finally:
            await client.disconnect()

    with _TG_LOCK:
        try:
            result = asyncio.run(_fetch())
        except Exception as e:
            return jsonify({
                "configured": True,
                "messages": [],
                "warning": f"Telegram sync error: {e}",
                "checked_at": datetime.now().strftime("%I:%M:%S %p").lstrip("0"),
            })

    if not result.get("authorized"):
        return jsonify({
            "configured": False,
            "messages": [],
            "error": "Telegram session expired or not logged in yet.",
        })

    msgs = result["messages"]
    return jsonify({
        "configured": True,
        "messages": msgs,
        "latest_id": msgs[0]["id"] if msgs else 0,
        "filter_chat_id": target_chat_raw,
        "warning": "",
        "checked_at": datetime.now().strftime("%I:%M:%S %p").lstrip("0"),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
