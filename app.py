import hmac
from werkzeug.security import check_password_hash
import os
import calendar
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session
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
    months = int(request.form.get("months", 1))
    current_filter = request.form.get("filter", "all")
    current_search = request.form.get("search", "")

    conn = get_db_connection()
    xui_phpsessid = get_setting(conn, "xui_phpsessid", os.getenv("XUI_PHPSESSID", ""))
    xui_enabled = get_setting(conn, "xui_enabled", os.getenv("XUI_ENABLED", "true")) == "true"

    cursor = conn.cursor(dictionary=True)
    cursor.execute(f"SELECT firstname, lastname, userid, expirydate FROM {TABLE} WHERE id=%s", (sub_id,))
    sub = cursor.fetchone()

    if sub:
        existing_dt = parse_date_string(sub.get("expirydate", ""))
        base_date = existing_dt if existing_dt else date.today()
        new_dt = add_months(base_date, months)
        new_expiry_str = format_db_date(new_dt)

        xui_note = ""
        if xui_enabled:
            try:
                client = XUIClient(xui_phpsessid)
                line_id, xui_exp, pkg_id, pkg_name = client.extend_line(sub.get("userid", ""), months, new_dt)
                xui_note = f" | XUI line #{line_id} extended to {xui_exp}"
            except Exception as e:
                cursor.close()
                conn.close()
                flash(f"XUI Extension Failed (local DB not changed): {e}", "warning")
                return redirect(url_for("index", filter=current_filter, search=current_search))

        uid_clean = (sub.get("userid") or "").strip()
        if uid_clean:
            cursor.execute(f"UPDATE {TABLE} SET expirydate=%s WHERE LOWER(TRIM(userid))=LOWER(%s)", (new_expiry_str, uid_clean))
        else:
            cursor.execute(f"UPDATE {TABLE} SET expirydate=%s WHERE id=%s", (new_expiry_str, sub_id))
        conn.commit()
        label = "1 year" if months == 12 else f"{months} month{'s' if months > 1 else ''}"
        flash(
            f"Extended {sub['firstname']} {sub['lastname']} (#{sub_id}) by {label} "
            f"({format_db_date(base_date)} -> {new_expiry_str}){xui_note}",
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
