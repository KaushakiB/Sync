# app.py
import os, re
from flask import Flask, request, jsonify, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import date

app = Flask(__name__, static_folder='.', static_url_path='/')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', f"sqlite:///routelink.db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")  # eventlet required

# Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200))
    email = db.Column(db.String(200), unique=True, index=True)
    password_hash = db.Column(db.String(300))
    gender = db.Column(db.String(2))

    def check_password(self, pw): return check_password_hash(self.password_hash, pw)
    def to_dict(self): return {'id': self.id, 'name': self.name, 'email': self.email}

class Route(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(10), index=True)
    slot_no = db.Column(db.String(40), index=True)
    end_point = db.Column(db.String(200))
    major_stops = db.Column(db.String(400))
    time = db.Column(db.String(10))
    transport_type = db.Column(db.String(40))

    def to_dict(self):
        return {'id': self.id, 'date': self.date, 'slot_no': self.slot_no, 'end_point': self.end_point,
                'major_stops': self.major_stops, 'time': self.time, 'transport_type': self.transport_type}

class Link(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    route_id = db.Column(db.Integer, db.ForeignKey('route.id', ondelete='CASCADE'), index=True)
    date = db.Column(db.String(10), index=True)
    name = db.Column(db.String(200))
    gender = db.Column(db.String(2))
    drop_point = db.Column(db.String(200))
    phone = db.Column(db.String(40))
    course_year = db.Column(db.String(40))
    branch = db.Column(db.String(120))

    def to_dict(self):
        return {'id': self.id, 'route_id': self.route_id, 'date': self.date, 'name': self.name,
                'gender': self.gender, 'drop_point': self.drop_point, 'phone': self.phone,
                'course_year': self.course_year, 'branch': self.branch}

# Ensure DB tables exist on first request (works under Gunicorn on Render)
@app.before_first_request
def create_tables():
    db.create_all()

# Simple auth endpoints used by your frontend
@app.route('/register', methods=['POST'])
def register():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip(); email = (data.get('email') or '').strip().lower()
    pw = data.get('password') or ''; gender = data.get('gender') or 'M'
    if not name or not email or not pw: return jsonify({'error': 'All fields required'}), 400
    if User.query.filter_by(email=email).first(): return jsonify({'error':'Email exists'}), 400
    user = User(name=name, email=email, password_hash=generate_password_hash(pw), gender=gender)
    db.session.add(user); db.session.commit()
    return jsonify({'msg':'registered'}), 201

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower(); pw = data.get('password') or ''
    u = User.query.filter_by(email=email).first()
    if not u or not u.check_password(pw): return jsonify({'error':'Invalid credentials'}), 401
    session['user_id'] = u.id; session['user_name'] = u.name
    return jsonify({'id': u.id, 'name': u.name, 'email': u.email})

@app.route('/me')
def me():
    uid = session.get('user_id')
    if not uid: return jsonify({'id': None})
    u = User.query.get(uid)
    return jsonify({'id': u.id, 'name': u.name, 'email': u.email})

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return '', 204

# next_slot, holidays, calendar endpoints (as your frontend expects)
@app.route('/next_slot')
def next_slot():
    total = Route.query.count() + 1
    return jsonify({'slot': f"SL{str(total).zfill(4)}"})

@app.route('/holidays')
def holidays():
    return jsonify([])

@app.route('/calendar/<date_iso>')
def calendar(date_iso):
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_iso): return jsonify([]), 400
    routes = Route.query.filter_by(date=date_iso).order_by(Route.slot_no).all()
    return jsonify([r.to_dict() for r in routes])

@app.route('/routes', methods=['POST'])
def create_route():
    if 'user_id' not in session: return jsonify({'error':'login required'}), 401
    data = request.get_json() or {}
    date_iso = data.get('date'); slot_no = data.get('slot_no'); end_point = data.get('end_point')
    major_stops = data.get('major_stops'); t = data.get('time'); transport = data.get('transport_type') or ''
    if not date_iso or not slot_no or not end_point: return 'Missing fields', 400
    dup = Route.query.filter_by(date=date_iso, end_point=end_point, time=t, transport_type=transport).first()
    if dup: return 'Duplicate route exists', 409
    r = Route(date=date_iso, slot_no=slot_no, end_point=end_point, major_stops=major_stops, time=t, transport_type=transport)
    db.session.add(r); db.session.commit()
    socketio.emit('route_created', r.to_dict(), broadcast=True)
    return '', 201

@app.route('/route_count')
def route_count():
    date_iso = request.args.get('date'); route_id = request.args.get('route_id', type=int)
    if not date_iso or not route_id: return jsonify({'count': 0})
    cnt = Link.query.filter_by(date=date_iso, route_id=route_id).count()
    return jsonify({'count': cnt})

@app.route('/routes/<int:route_id>/links')
def route_links(route_id):
    date_iso = request.args.get('date') or date.today().isoformat()
    people = Link.query.filter_by(route_id=route_id, date=date_iso).all()
    return jsonify([p.to_dict() for p in people])

@app.route('/routes/<int:route_id>/join', methods=['POST'])
def join_route(route_id):
    if 'user_id' not in session: return jsonify({'error':'login required'}), 401
    data = request.get_json() or {}
    name = data.get('name') or ''; gender = data.get('gender') or 'M'; drop = data.get('drop') or ''
    phone = data.get('phone') or ''; year = data.get('course_year') or ''; branch = data.get('branch') or ''
    date_iso = data.get('date') or date.today().isoformat()
    r = Route.query.get(route_id)
    if not r or r.date != date_iso: return 'Route not found for date', 404
    existing = Link.query.filter_by(route_id=route_id, date=date_iso, phone=phone).first()
    if existing: return 'Already joined', 409
    p = Link(route_id=route_id, date=date_iso, name=name, gender=gender, drop_point=drop, phone=phone, course_year=year, branch=branch)
    db.session.add(p); db.session.commit()
    socketio.emit('link_created', p.to_dict(), broadcast=True)
    return '', 201

@app.route('/links/<int:link_id>', methods=['DELETE'])
def delete_link(link_id):
    if 'user_id' not in session: return jsonify({'error':'login required'}), 401
    p = Link.query.get(link_id)
    if not p: return 'Not found', 404
    if session.get('user_name','').strip().lower() != (p.name or '').strip().lower():
        return 'forbidden', 403
    db.session.delete(p); db.session.commit()
    socketio.emit('link_deleted', {'id': link_id, 'route_id': p.route_id, 'date': p.date}, broadcast=True)
    return '', 204

# serve the index.html (your front-end) and static files from repo root
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
