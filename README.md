# IPTV Subscriber Manager

A lightweight, containerized web dashboard built with **Flask**, **MariaDB/MySQL**, and **Tailwind CSS** to manage IPTV customer subscriptions, track local customer billing dates independently from panel expirations, automate Gmail renewal reminders (`613koditv@gmail.com`), and synchronize line extensions directly with an **XUI One** reseller panel (`iptvottawa.ca`).

---

## Features

* **Local Billing & Subscriber Management (`app.py`)**
  * Track customer contact details, assigned IPTV line usernames (`userid`), connection counts, custom notes, and local billing expiration dates (`expirydate`).
  * Quick-extend individual local subscriber billing dates by **+1M**, **+3M**, or **+1Y**, or run grouped bulk extensions.
  * Streamlined subscriber table UI starting directly with customer names (`FIRST NAME`, `LAST NAME`, `USER ID`, `EMAIL`, `CONN`, `EXPIRY DATE`, `QUICK EXTEND`, `NOTES`, and action controls).
* **Gmail Renewal & Announcement Automation (`templates/index.html`)**
  * **Per-Row Renewal Email Modal:** Pre-fills subscriber name, `userid`, connection count, and local expiration date into an editable preview modal.
  * **Bulk "Notify Expiring This Month" & "Announcement Email" Modals:** Automatically populates BCC recipient lists for expiring or active subscribers.
  * **Direct Gmail Compose Routing:** Opens a new browser tab directly into Gmail signed in under **`613koditv@gmail.com`** (`https://mail.google.com/mail/?authuser=613koditv@gmail.com&view=cm&fs=1...`) with CRLF-compliant multi-line bodies, avoiding broken `mailto:` protocol handlers.
* **XUI Panel Integration & Credit-Safe 7-Day Renewal Tool (`xui_client.py`)**
  * **Session-Based Authentication:** Connects to `iptvottawa.ca` via an active `PHPSESSID` session cookie configurable in the UI (**XUI Sync**) or via `.env`.
  * **XUI Expiring (7d) Modal:** Queries the XUI dashboard and `table.php` endpoints in real time to display your live reseller credit balance (`owner_credits`) alongside all non-trial XUI lines expiring within 7 days (or expired within the past 3 days), matched against local subscriber records for reference.
  * **Isolated Credit Extensions (`/xui/extend-week`):** Validates that `available_credits >= selected_accounts * months` before submitting, preserves each line's existing package tier (Clean/Adult, VOD/No-VOD, MAG) and bouquets, and **extends only the XUI panel account** without overwriting local customer billing dates.

---

## Architecture & Project Structure

```text
subscriber-manager/
├── app.py                  # Flask routes, MariaDB/MySQL persistence, and XUI endpoints
├── xui_client.py           # XUI panel HTTP client, HTML/JSON parsers, and safe line extension logic
├── templates/
│   └── index.html          # Main dashboard UI, sortable subscriber table, and email/XUI modals
├── Dockerfile              # Container build definition for the Flask web application
├── .env                    # Runtime environment variables (DB credentials, XUI session defaults)
└── README.md               # Project documentation
```

---

## Environment Configuration

Create a `.env` file in the project root with your MariaDB/MySQL connection settings and default XUI panel configuration:

```env
# Flask Security
SECRET_KEY=replace-with-a-strong-random-secret

# MariaDB / MySQL Database
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=root
DB_PASSWORD=your_db_password
DB_NAME=dbs11651992
DB_TABLE=subscribers

# XUI Panel Integration
XUI_URL=https://iptvottawa.ca/SCHSEGZi
XUI_MEMBER_ID=35
XUI_PHPSESSID=your_active_phpsessid_cookie
XUI_ENABLED=true
```

> **Tip:** Whenever your XUI browser session expires, log into `https://iptvottawa.ca/SCHSEGZi/dashboard` in Chrome, copy the `PHPSESSID` cookie from DevTools (**Application → Cookies**), and paste it into the **XUI Sync** modal in the dashboard header—no container restart required.

---

## Deployment (Podman)

The application runs inside a Podman pod (`subscriber-pod`) alongside the MariaDB container on port `5050`.

### Build and Restart the Web Container

```bash
cd /Users/poleary/work/iptv/subscriber-manager

# 1. Build the updated image
podman build --no-cache -t subscriber-manager .

# 2. Verify Python syntax and Jinja2 templates before deploying
podman run --rm subscriber-manager python3 -c "
import py_compile
from jinja2 import Environment, FileSystemLoader
py_compile.compile('xui_client.py', doraise=True)
py_compile.compile('app.py', doraise=True)
Environment(loader=FileSystemLoader('templates')).get_template('index.html')
print('All syntax and template checks passed!')
"

# 3. Replace and start the running container
podman rm -f subscriber-web
podman run -d --name subscriber-web --pod subscriber-pod --env-file .env subscriber-manager
```

Access the dashboard at **`http://localhost:5050`**.

---

## Operational Workflows

### 1. Sending Customer Renewal Emails
1. Click **Email** on any subscriber row (or **Notify Expiring This Month** / **Announcement Email** in the header).
2. Review or customize the pre-filled **Subject** and **Message** body.
3. Click **Open in Gmail (613koditv)** to open a pre-populated Gmail compose tab under `613koditv@gmail.com`.

### 2. Extending XUI Lines Expiring Within 7 Days
1. Click **XUI Expiring (7d)** in the dashboard header.
2. Confirm your live **Available Credits** badge at the top of the modal.
3. Select the XUI lines (`ottiptv_*`) you wish to renew and click **Extend +1M**, **Extend +3M**, or **Extend +1Y**.
   * Duration buttons automatically disable if the required credit cost exceeds your available balance.
   * Local database billing dates remain untouched so multi-user shared accounts keep their individual customer renewal schedules.

---

## Key HTTP Routes (`app.py`)

| Method | Route | Description |
| :--- | :--- | :--- |
| `GET` | `/` | Renders the main subscriber dashboard and email/sync modals. |
| `POST` | `/xui-session` | Saves and verifies a fresh `PHPSESSID` session cookie against `/dashboard`. |
| `POST` | `/extend/<int:sub_id>` | Extends a single subscriber's local billing date (and optionally XUI line). |
| `POST` | `/bulk-extend` | Extends multiple grouped subscribers by `+1M`, `+3M`, or `+1Y`. |
| `GET` | `/xui/expiring-week` | JSON endpoint returning live XUI `credits` and non-trial `lines` expiring within 7 days. |
| `POST` | `/xui/extend-week` | Credit-checked XUI-only extension endpoint for lines selected in the 7-day modal. |

## XUI Sync & Reseller Credits

When **XUI Sync** is enabled with a valid `PHPSESSID`, the top action bar displays the current reseller credit balance directly inside the **XUI Sync: ON** button (e.g., `XUI Sync: ON 42 cr`).

* **`GET /xui/credits`**: Queries `XUIClient.get_credits()` asynchronously on dashboard load, updates the button badge, and caches the latest balance in MariaDB `app_settings` (`xui_cached_credits`) for instant rendering on subsequent page loads.
* **`GET /xui/expiring-week` & `POST /xui/extend-week`**: Also refresh the cached credit balance whenever expiring lines are inspected or extended.

## UserID Allocation Quick Reference

The **UserID Map** modal (`templates/index.html`) displays all local subscriber assignments grouped by XUI `userid` with tabular alignment:

* **Aligned Sub-Rows & Alternating Contrast**: Each UserID group lists every assigned subscriber in a high-contrast alternating-row 3-column grid (`Subscriber` | `Connections` | `Expiry`) with sticky headers and group capacity badges (`Users: N / 3` and `Total Conn: N / 5`).
* **Interactive Filtering**: Supports live search by UserID, subscriber name, or expiry date, plus toggles to **Hide cancelled** accounts and filter to **Open slots only** (`< 3` users and `< 5` total connections).
