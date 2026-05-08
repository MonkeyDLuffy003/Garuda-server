import os
import re
import hashlib
import sqlite3
import datetime
import random
import string
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

with app.app_context():
    pass  # init_db called below after function is defined

# ─────────────────────────────────────────────
# CONFIG — set in Render environment variables
# ─────────────────────────────────────────────
GEMINI_CHAT_KEY   = os.environ.get("GEMINI_CHAT_KEY", "")
GEMINI_SEARCH_KEY = os.environ.get("GEMINI_SEARCH_KEY", "")
GROQ_KEY          = os.environ.get("GROQ_KEY", "")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro:generateContent"
GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.1-8b-instant"

DEV_SECRET  = "GARUDA_TEJATECH_DEV"
MAX_USERS   = 400
DAILY_BASE  = 20
REFERRAL_BONUS_NEW  = 5
REFERRAL_BONUS_PERM = 2
DB_PATH     = "garuda_server.db"

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def generate_referral_code(username):
    suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"GRD-{username[:4].upper()}-{suffix}"

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        username        TEXT UNIQUE,
        password_hash   TEXT,
        referral_code   TEXT UNIQUE,
        referred_by     TEXT,
        referral_count  INTEGER DEFAULT 0,
        registered_at   TEXT,
        is_developer    INTEGER DEFAULT 0,
        is_active       INTEGER DEFAULT 1
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS usage (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        date     TEXT,
        count    INTEGER DEFAULT 0
    )''')
    conn.commit()
    conn.close()

# Initialize DB on startup
init_db()

def get_user(username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username = ?", (username,))
    row = c.fetchone()
    conn.close()
    return row

def get_user_by_referral(code):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE referral_code = ?", (code,))
    row = c.fetchone()
    conn.close()
    return row

def create_user(username, password, referred_by="", is_dev=False):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users WHERE is_developer = 0")
    count = c.fetchone()[0]
    if not is_dev and count >= MAX_USERS:
        conn.close()
        return False, "full"
    ref_code = generate_referral_code(username)
    try:
        c.execute('''INSERT INTO users
            (username, password_hash, referral_code, referred_by, registered_at, is_developer)
            VALUES (?, ?, ?, ?, ?, ?)''',
            (username, hash_password(password), ref_code,
             referred_by, datetime.datetime.now().isoformat(), 1 if is_dev else 0))
        if referred_by:
            c.execute("UPDATE users SET referral_count = referral_count + 1 WHERE username = ?",
                      (referred_by,))
        conn.commit()
        conn.close()
        return True, ref_code
    except sqlite3.IntegrityError:
        conn.close()
        return False, "taken"

def get_daily_limit(username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT is_developer, referral_count, referred_by, registered_at FROM users WHERE username = ?",
              (username,))
    row = c.fetchone()
    conn.close()
    if not row:
        return DAILY_BASE
    is_dev, ref_count, referred_by, registered_at = row
    if is_dev:
        return 9999
    limit = DAILY_BASE + (ref_count * REFERRAL_BONUS_PERM)
    if referred_by:
        try:
            reg_date = datetime.datetime.fromisoformat(registered_at).date()
            if reg_date == datetime.date.today():
                limit += REFERRAL_BONUS_NEW
        except Exception:
            pass
    return limit

def get_today_usage(username):
    today = datetime.date.today().isoformat()
    conn  = sqlite3.connect(DB_PATH)
    c     = conn.cursor()
    c.execute("SELECT count FROM usage WHERE username=? AND date=?", (username, today))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def increment_usage(username):
    today = datetime.date.today().isoformat()
    conn  = sqlite3.connect(DB_PATH)
    c     = conn.cursor()
    c.execute("SELECT id FROM usage WHERE username=? AND date=?", (username, today))
    row = c.fetchone()
    if row:
        c.execute("UPDATE usage SET count=count+1 WHERE id=?", (row[0],))
    else:
        c.execute("INSERT INTO usage (username, date, count) VALUES (?,?,1)", (username, today))
    conn.commit()
    conn.close()

def check_limit(username):
    limit     = get_daily_limit(username)
    used      = get_today_usage(username)
    remaining = limit - used
    return (remaining > 0, max(0, remaining), limit)

def is_developer(username):
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("SELECT is_developer FROM users WHERE username=?", (username,))
    row = c.fetchone()
    conn.close()
    return bool(row and row[0] == 1)

# ─────────────────────────────────────────────
# AI HELPERS
# ─────────────────────────────────────────────
GARUDA_SYSTEM = """You are Garuda, a powerful male AI research assistant.
Sharp, precise, direct. Like an eagle — clear vision, no fluff.
Format every response:
- Brief intro (1-2 sentences)
- Key points (3-5 bullets using *)
- Closing insight
Rules: No adult or illegal content. Be concise and factual."""

RESEARCH_SYSTEM = """You are Garuda's research engine.
Process data and give clear structured summaries.
Format: Summary (2-3 sentences), Key findings (* bullets), Source insight.
Be factual. If data is limited say so."""

def call_gemini(api_key, prompt, system="", max_tokens=700):
    try:
        url      = f"{GEMINI_URL}?key={api_key}"
        contents = []
        if system:
            contents.append({"role": "user",  "parts": [{"text": system}]})
            contents.append({"role": "model", "parts": [{"text": "Understood."}]})
        contents.append({"role": "user", "parts": [{"text": prompt}]})
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json={"contents": contents,
                  "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.7}},
            timeout=15
        )
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        return f"Garuda error: {str(e)}"

def call_groq(prompt, max_tokens=500, temperature=0.2):
    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_KEY}",
                     "Content-Type": "application/json"},
            json={"model": GROQ_MODEL,
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": max_tokens, "temperature": temperature},
            timeout=10
        )
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return ""

def search_web(query):
    try:
        url  = f"https://api.duckduckgo.com/?q={requests.utils.quote(query)}&format=json&no_html=1&skip_disambig=1"
        resp = requests.get(url, timeout=8)
        data = resp.json()
        result = data.get("AbstractText", "")
        if not result:
            for t in data.get("RelatedTopics", [])[:3]:
                if isinstance(t, dict) and t.get("Text"):
                    result += t["Text"] + "\n"
        return result.strip()
    except Exception:
        return ""

NEWS_SOURCES = {
    "general": "https://feeds.bbci.co.uk/news/rss.xml",
    "tech":    "https://feeds.feedburner.com/TechCrunch",
    "sports":  "https://www.espn.com/espn/rss/news",
}

def fetch_news(category="general"):
    try:
        resp   = requests.get(NEWS_SOURCES.get(category, NEWS_SOURCES["general"]), timeout=8)
        titles = re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>', resp.text)
        if not titles:
            titles = re.findall(r'<title>(.*?)</title>', resp.text)
        return "\n".join([t for t in titles[1:6] if t])
    except Exception:
        return ""

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "Garuda server running", "version": "2.0"})

@app.route("/register", methods=["POST"])
def register():
    data     = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    referral = data.get("referral_code", "").strip()
    dev_code = data.get("dev_code", "").strip()

    if len(username) < 3:
        return jsonify({"success": False, "message": "Username must be at least 3 characters."})
    if len(password) < 4:
        return jsonify({"success": False, "message": "Password must be at least 4 characters."})
    if get_user(username):
        return jsonify({"success": False, "message": "Username taken. Try another."})

    is_dev      = dev_code == DEV_SECRET
    referred_by = ""

    if referral:
        ref_user = get_user_by_referral(referral)
        if ref_user:
            referred_by = ref_user[1]
        else:
            return jsonify({"success": False, "message": "Invalid referral code."})

    ok, result = create_user(username, password, referred_by, is_dev)
    if ok:
        role    = "developer" if is_dev else "user"
        bonus   = f" +{REFERRAL_BONUS_NEW} bonus requests today!" if referred_by else ""
        welcome = f"Welcome to Garuda, {username}!{bonus}" if not is_dev else \
                  "Developer account activated! Unlimited access."
        return jsonify({"success": True, "message": welcome,
                        "role": role, "referral_code": result})
    if result == "taken":
        return jsonify({"success": False, "message": "Username taken. Try another."})
    return jsonify({"success": False,
                    "message": "Garuda is at full capacity. Stay tuned for official launch!"})

@app.route("/login", methods=["POST"])
def login():
    data     = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    dev_code = data.get("dev_code", "").strip()

    if not username or not password:
        return jsonify({"success": False, "message": "Username and password required."})

    user = get_user(username)
    if not user:
        return jsonify({"success": False, "message": "User not found. Please register."})
    if user[2] != hash_password(password):
        return jsonify({"success": False, "message": "Wrong password."})
    if not user[8]:
        return jsonify({"success": False, "message": "Account disabled."})

    if dev_code == DEV_SECRET and not user[7]:
        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("UPDATE users SET is_developer=1 WHERE username=?", (username,))
        conn.commit()
        conn.close()

    dev   = is_developer(username)
    limit = get_daily_limit(username)
    used  = get_today_usage(username)
    return jsonify({
        "success":       True,
        "message":       f"Welcome back, {username}!",
        "role":          "developer" if dev else "user",
        "referral_code": user[3],
        "remaining":     9999 if dev else max(0, limit - used),
        "limit":         limit
    })

@app.route("/chat", methods=["POST"])
def chat():
    data     = request.json or {}
    username = data.get("username", "")
    query    = data.get("query", "")

    if not get_user(username):
        return jsonify({"success": False, "message": "User not found. Please login."})

    allowed, remaining, limit = check_limit(username)
    if not allowed:
        return jsonify({"success": False,
                        "message": f"Daily limit reached ({limit}/{limit}). Resets at midnight!"})

    response = call_gemini(GEMINI_CHAT_KEY, query, system=GARUDA_SYSTEM)
    increment_usage(username)
    return jsonify({"success": True, "response": response,
                    "remaining": remaining - 1, "developer": is_developer(username)})

@app.route("/research", methods=["POST"])
def research():
    data     = request.json or {}
    username = data.get("username", "")
    query    = data.get("query", "")
    category = data.get("category", "")

    if not get_user(username):
        return jsonify({"success": False, "message": "User not found. Please login."})

    allowed, remaining, limit = check_limit(username)
    if not allowed:
        return jsonify({"success": False,
                        "message": f"Daily limit reached ({limit}/{limit}). Resets at midnight!"})

    if category in ["general", "tech", "sports"]:
        raw      = fetch_news(category)
        prompt   = f"Summarize these news headlines clearly:\n{raw}"
        response = call_gemini(GEMINI_SEARCH_KEY, prompt, system=RESEARCH_SYSTEM)
    else:
        web      = search_web(query)
        prompt   = f"Web data:\n{web}\n\nQuestion: {query}" if web else query
        response = call_gemini(GEMINI_SEARCH_KEY, prompt, system=RESEARCH_SYSTEM)

    increment_usage(username)
    return jsonify({"success": True, "response": response,
                    "remaining": remaining - 1, "developer": is_developer(username)})

@app.route("/translate", methods=["POST"])
def translate():
    data     = request.json or {}
    username = data.get("username", "")
    text     = data.get("text", "")
    action   = data.get("action", "detect")
    lang     = data.get("lang", "en")

    if not get_user(username):
        return jsonify({"success": False, "message": "User not found."})

    if action == "detect":
        result = call_groq(f"Detect language. Reply ONLY with 2-letter ISO code:\n{text[:200]}",
                           max_tokens=5, temperature=0)
        return jsonify({"success": True, "lang": result.lower()[:5] or "en"})
    elif action == "to_english":
        if lang == "en":
            return jsonify({"success": True, "text": text})
        result = call_groq(f"Translate to English. Return ONLY translation:\n{text}", max_tokens=500)
        return jsonify({"success": True, "text": result or text})
    elif action == "from_english":
        if lang == "en":
            return jsonify({"success": True, "text": text})
        result = call_groq(
            f"Translate to {lang} naturally. Keep bullet points. Return ONLY translation:\n{text}",
            max_tokens=700)
        return jsonify({"success": True, "text": result or text})

    return jsonify({"success": False, "message": "Invalid action."})

@app.route("/status", methods=["GET"])
def status():
    username = request.args.get("username", "")
    user     = get_user(username)
    if not user:
        return jsonify({"success": False, "message": "Not registered."})
    dev   = is_developer(username)
    limit = get_daily_limit(username)
    used  = get_today_usage(username)
    return jsonify({
        "success":        True,
        "username":       username,
        "developer":      dev,
        "used_today":     used,
        "remaining":      9999 if dev else max(0, limit - used),
        "limit":          limit,
        "referral_code":  user[3],
        "referral_count": user[5]
    })

@app.route("/admin/users", methods=["GET"])
def admin_users():
    username = request.args.get("username", "")
    if not is_developer(username):
        return jsonify({"success": False, "message": "Unauthorized."})
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("""
        SELECT u.username, u.referral_code, u.referral_count,
               u.registered_at, u.is_developer,
               COALESCE(SUM(us.count), 0) as total
        FROM users u
        LEFT JOIN usage us ON u.username = us.username
        GROUP BY u.username ORDER BY total DESC
    """)
    rows  = c.fetchall()
    conn.close()
    users = [{"username": r[0], "referral_code": r[1], "referrals": r[2],
              "registered": r[3], "developer": bool(r[4]),
              "total_requests": r[5]} for r in rows]
    return jsonify({"success": True, "total_users": len(users),
                    "slots_remaining": MAX_USERS - len(users), "users": users})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
