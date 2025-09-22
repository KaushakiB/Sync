# app.py
import os, re, sqlite3, hashlib, json, random, calendar
from datetime import date, datetime
from flask import Flask, request, jsonify, render_template, g, session, redirect, url_for

DB = "routelink.db"
HOL_JSON = "academic_holidays.json"
HOL_CSV = "academic_holidays.csv"

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = "dev-secret-change-me"  # change in production
app.config['JSON_SORT_KEYS'] = False

# ---------------- DB helpers ----------------
def ensure_column(table: str, column: str, col_type: str, default: str = None):
    try:
        conn = sqlite3.connect(DB)
        c = conn.cursor()
        c.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in c.fetchall()]
        if column not in cols:
            sql = f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
            if default is not None:
                sql += f" DEFAULT {default}"
            c.execute(sql)
            conn.commit()
        conn.close()
    except Exception:
        try: conn.close()
        except Exception: pass

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT UNIQUE, password_hash TEXT
                 )""")
    c.execute("""CREATE TABLE IF NOT EXISTS routes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, slot_no TEXT, end_point TEXT,
                    major_stops TEXT, time TEXT, transport_type TEXT, no_of_people INTEGER DEFAULT 0
                 )""")
    c.execute("""CREATE TABLE IF NOT EXISTS links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, drop_point TEXT, phone TEXT,
                    course_year TEXT, branch TEXT
                 )""")
    c.execute("""CREATE TABLE IF NOT EXISTS calendar (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, travel_date TEXT, route_id INTEGER, link_id INTEGER,
                    FOREIGN KEY(route_id) REFERENCES routes(id), FOREIGN KEY(link_id) REFERENCES links(id)
                 )""")
    conn.commit()
    # WAL for better concurrency
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        conn.commit()
    except Exception:
        pass
    conn.close()
    ensure_column("users", "gender", "TEXT")
    ensure_column("links", "gender", "TEXT")

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB, timeout=30, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop('db', None)
    if db:
        try: db.close()
        except Exception: pass

def hash_pw(txt: str) -> str:
    return hashlib.sha256(txt.encode()).hexdigest()

def to_base36(n: int) -> str:
    digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if n == 0: return "0"
    out=[]
    while n:
        n, rem = divmod(n, 36)
        out.append(digits[rem])
    return "".join(reversed(out))

def generate_next_slot_no():
    try:
        conn = sqlite3.connect(DB)
        c = conn.cursor()
        c.execute("SELECT MAX(id) FROM routes")
        r = c.fetchone()
        conn.close()
        max_id = int(r[0]) if (r and r[0]) else 0
        seq = max_id + 1
    except Exception:
        seq = 1
    b36 = to_base36(seq).rjust(4, "0")
    return f"SL{b36}"

# ---------------- Holidays loader ----------------
def load_academic_holidays():
    if os.path.exists(HOL_JSON):
        try:
            with open(HOL_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            holidays=[]
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str): holidays.append(item)
                    elif isinstance(item, dict) and "date" in item: holidays.append(item["date"])
            return sorted(set(holidays))
        except Exception:
            pass
    # fallback sample
    return generate_sample_holidays(date.today().year)

def generate_sample_holidays(year: int, seed: int = 123):
    random.seed(seed + year)
    fixed = [(year,1,26),(year,5,1),(year,8,15),(year,10,2),(year,12,25)]
    holidays=set()
    for y,m,d in fixed:
        try: holidays.add(date(y,m,d).isoformat())
        except Exception: pass
    while len(holidays) < 8:
        m=random.randint(1,12)
        d=random.randint(1,calendar.monthrange(year,m)[1])
        holidays.add(date(year,m,d).isoformat())
    return sorted(holidays)

# ---------------- Auth helper ----------------
def login_required(f):
    from functools import wraps
    @wraps(f)
    def wrapped(*args, **kwargs):
        if session.get("user_id") is None:
            return jsonify({"error":"Login required"}), 401
        return f(*args, **kwargs)
    return wrapped

# ---------------- HTTP API ----------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/me")
def api_me():
    if session.get("user_id"):
        return jsonify({"id": session.get("user_id"), "name": session.get("user_name")})
    return jsonify({"id": None})

@app.route("/holidays")
def api_holidays():
    return jsonify(load_academic_holidays())

@app.route("/next_slot")
def api_next_slot():
    return jsonify({"slot": generate_next_slot_no()})

@app.route("/calendar/<iso_date>")
def api_calendar_for_date(iso_date):
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("""
            SELECT DISTINCT r.id, r.slot_no, r.end_point, r.major_stops, r.time, r.transport_type
            FROM calendar cal LEFT JOIN routes r ON cal.route_id = r.id
            WHERE cal.travel_date = ? ORDER BY r.id DESC
        """, (iso_date,))
        rows = c.fetchall()
        out=[]
        for r in rows:
            out.append({k: r[k] for k in r.keys()})
        return jsonify(out)
    except Exception:
        return jsonify([]), 500

@app.route("/route_count")
def api_route_count():
    iso = request.args.get("date"); rid = request.args.get("route_id")
    if not iso or not rid: return jsonify({"count":0})
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM calendar WHERE travel_date=? AND route_id=? AND link_id IS NOT NULL", (iso,rid))
        r = c.fetchone()
        return jsonify({"count": int(r[0]) if r else 0})
    except Exception:
        return jsonify({"count":0})

@app.route("/routes", methods=["POST"])
@login_required
def api_create_route():
    data = request.get_json(force=True)
    d = data.get("date"); slot = data.get("slot_no"); endp = data.get("end_point")
    stops = data.get("major_stops"); ttime = data.get("time"); ttype = data.get("transport_type")
    if not all([d, slot, endp]):
        return "Missing required fields", 400
    try:
        sel = datetime.strptime(d, "%Y-%m-%d").date()
    except Exception:
        return "Invalid date", 400
    if sel < date.today():
        return "Cannot create route for past dates", 400
    if ttime:
        try: datetime.strptime(ttime, "%H:%M")
        except Exception: return "Invalid time", 400
    # duplicate check for same date
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("""
            SELECT r.id FROM routes r JOIN calendar cal ON cal.route_id = r.id
            WHERE cal.travel_date = ? AND LOWER(r.end_point)=LOWER(?) AND COALESCE(r.time,'')=? AND LOWER(COALESCE(r.transport_type,''))=LOWER(?)
            LIMIT 1
        """, (d, endp, ttime or "", ttype or ""))
        if c.fetchone(): return "Duplicate route", 409
    except Exception:
        pass
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("INSERT INTO routes (slot_no, end_point, major_stops, time, transport_type, no_of_people) VALUES (?, ?, ?, ?, ?, ?)",
                  (slot, endp, stops, ttime, ttype, 0))
        rid = c.lastrowid
        c.execute("INSERT INTO calendar (travel_date, route_id, link_id) VALUES (?, ?, NULL)", (d, rid))
        conn.commit()
        return jsonify({"route_id": rid}), 201
    except Exception as e:
        return str(e), 500

@app.route("/routes/<int:rid>/links", methods=["GET"])
@login_required
def api_routes_links(rid):
    # expects query param date=YYYY-MM-DD
    iso = request.args.get("date")
    if not iso: return jsonify([])
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("""
            SELECT l.id, l.name, l.gender, l.drop_point, l.phone, l.course_year, l.branch
            FROM links l JOIN calendar cal ON cal.link_id = l.id
            WHERE cal.route_id = ? AND cal.travel_date = ?
            ORDER BY l.id DESC
        """, (rid, iso))
        rows = c.fetchall()
        out=[{k:r[k] for k in r.keys()} for r in rows]
        return jsonify(out)
    except Exception:
        return jsonify([]), 500

@app.route("/routes/<int:rid>/join", methods=["POST"])
@login_required
def api_join_route(rid):
    data = request.get_json(force=True)
    d = data.get("date"); name = data.get("name"); gender = (data.get("gender") or "").upper()
    drop = data.get("drop"); phone = data.get("phone"); year = data.get("course_year"); branch = data.get("branch")
    if not all([d, name, gender, drop, phone, year, branch]): return "Missing fields", 400
    if gender not in ("M","F"): return "Invalid gender", 400
    if not phone.isdigit() or len(phone)<7: return "Invalid phone", 400
    try:
        sel = datetime.strptime(d, "%Y-%m-%d").date()
    except Exception:
        return "Invalid date", 400
    if sel < date.today(): return "Cannot join for past dates", 400
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT end_point FROM routes WHERE id=?", (rid,))
    rr = c.fetchone()
    if rr and rr["end_point"] and rr["end_point"].strip().lower() != drop.strip().lower():
        return f"Drop must match route endpoint '{rr['end_point']}'", 400
    # duplicate check
    c.execute("SELECT l.id FROM links l JOIN calendar cal ON cal.link_id = l.id WHERE cal.travel_date=? AND cal.route_id=? AND l.phone=?", (d, rid, phone))
    if c.fetchone(): return "Already joined", 409
    try:
        try:
            c.execute("INSERT INTO links (name, gender, drop_point, phone, course_year, branch) VALUES (?, ?, ?, ?, ?, ?)",
                      (name, gender, drop, phone, year, branch))
        except Exception:
            c.execute("INSERT INTO links (name, drop_point, phone, course_year, branch) VALUES (?, ?, ?, ?, ?)",
                      (name, drop, phone, year, branch))
        lid = c.lastrowid
        c.execute("INSERT INTO calendar (travel_date, route_id, link_id) VALUES (?, ?, ?)", (d, rid, lid))
        conn.commit()
        return jsonify({"link_id": lid}), 201
    except Exception as e:
        return str(e), 500

@app.route("/links", methods=["GET"])
@login_required
def api_links():
    gender = request.args.get("gender")
    try:
        conn = get_db(); c = conn.cursor()
        if gender and gender.upper() in ("M","F"):
            c.execute("SELECT id, name, gender, drop_point, phone, course_year, branch FROM links WHERE UPPER(gender)=? ORDER BY id DESC", (gender.upper(),))
        else:
            c.execute("SELECT id, name, gender, drop_point, phone, course_year, branch FROM links ORDER BY id DESC")
        rows = c.fetchall()
        return jsonify([{k:r[k] for k in r.keys()} for r in rows])
    except Exception:
        return jsonify([])

@app.route("/links/<int:lid>", methods=["DELETE","PUT","PATCH"])
@login_required
def api_links_modify(lid):
    if request.method == "DELETE":
        try:
            conn = get_db(); c = conn.cursor()
            c.execute("DELETE FROM calendar WHERE link_id=?", (lid,))
            c.execute("DELETE FROM links WHERE id=?", (lid,))
            conn.commit()
            return jsonify({"ok": True})
        except Exception as e:
            return str(e), 500
    else:
        data = request.get_json(force=True)
        allowed = {"name":"name","gender":"gender","drop":"drop_point","phone":"phone","course_year":"course_year","branch":"branch"}
        updates = {}
        for k,v in allowed.items():
            if k in data: updates[v] = data[k]
        if not updates: return "No fields", 400
        set_sql = ", ".join([f"{k}=?" for k in updates.keys()])
        vals = list(updates.values()); vals.append(lid)
        try:
            conn = get_db(); c = conn.cursor()
            c.execute(f"UPDATE links SET {set_sql} WHERE id=?", vals)
            conn.commit()
            return jsonify({"ok": True})
        except Exception as e:
            return str(e), 500

@app.route("/routes/<int:rid>", methods=["PUT","PATCH","DELETE"])
@login_required
def api_routes_modify(rid):
    if request.method == "DELETE":
        try:
            conn = get_db(); c = conn.cursor()
            c.execute("DELETE FROM calendar WHERE route_id=?", (rid,))
            c.execute("DELETE FROM routes WHERE id=?", (rid,))
            conn.commit()
            return jsonify({"ok": True})
        except Exception as e:
            return str(e), 500
    else:
        data = request.get_json(force=True)
        allowed = ["slot_no","end_point","major_stops","time","transport_type"]
        updates = {k: data[k] for k in allowed if k in data}
        if not updates: return "No fields", 400
        set_sql = ", ".join([f"{k}=?" for k in updates.keys()])
        vals = list(updates.values()); vals.append(rid)
        try:
            conn = get_db(); c = conn.cursor()
            c.execute(f"UPDATE routes SET {set_sql} WHERE id=?", vals)
            conn.commit()
            return jsonify({"ok": True})
        except Exception as e:
            return str(e), 500

# Register / Login
@app.route("/register", methods=["POST"])
def api_register():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    pw = data.get("password") or ""
    gender = (data.get("gender") or "").strip().upper()
    if not name or not email or not pw or gender not in ("M","F"):
        return jsonify({"error":"Missing fields"}), 400
    if not re.match(r"^[A-Za-z0-9._%+-]+@vitstudent\.ac\.in$", email):
        return jsonify({"error":"Use a VIT email"}), 400
    try:
        conn = get_db(); c = conn.cursor()
        try:
            c.execute("INSERT INTO users (name, email, password_hash, gender) VALUES (?, ?, ?, ?)", (name, email, hash_pw(pw), gender))
        except Exception:
            c.execute("INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)", (name, email, hash_pw(pw)))
        conn.commit()
        return jsonify({"ok": True})
    except sqlite3.IntegrityError:
        return jsonify({"error":"Email exists"}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    pw = data.get("password") or ""
    if not email or not pw: return jsonify({"error":"Missing fields"}), 400
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT id, name FROM users WHERE email=? AND password_hash=?", (email, hash_pw(pw)))
    r = c.fetchone()
    if r:
        session["user_id"] = r["id"]
        session["user_name"] = r["name"]
        return jsonify({"ok": True, "name": r["name"]})
    return jsonify({"error":"Invalid credentials"}), 401

@app.route("/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})

# ---------------- Run ----------------
if __name__ == "__main__":
    init_db()
    print("Starting app on http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000, debug=True)
