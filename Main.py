from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, jsonify, send_file
)
from pathlib import Path
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from functools import wraps
from dotenv import load_dotenv
from sqlalchemy import Numeric
import io, os, pymysql, secrets, string, requests, json, msal, imaplib, ssl, base64, time, csv, hashlib
from pymysql.cursors import DictCursor
from PIL import Image

from proctoring_config import PROCTOR, public_config
from proctoring import (
    build_summary, chunk_key_hash, classify, is_attempt_expired,
    next_phase, policy_action, save_evidence, validate_identity_snapshot,
    validate_incident, validate_precheck, validate_recording_chunk, EVIDENCE_DIR,
    incident_key, should_dedupe
)
from headphone_detector import (
    detect_from_base64 as detect_audio_device,
    detect_batch_from_base64 as detect_audio_batch,
    is_available as audio_detector_available,
    info as audio_detector_info,
    _load_model as load_headphone_model,
)
import mimetypes

load_dotenv()
app = Flask(__name__)

# ============ CONFIG ============
app.secret_key = os.getenv('SECRET_KEY', 'candidate_portal_secret_key_2024')
SESSION_TIMEOUT_MINUTES = int(os.getenv('SESSION_TIMEOUT_MINUTES', 30))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=SESSION_TIMEOUT_MINUTES)
app.config['SESSION_REFRESH_EACH_REQUEST'] = True
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024

# ============ DATABASE ============
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_USER = os.getenv('DB_USER', 'root')
DB_PASSWORD = os.getenv('DB_PASSWORD', 'root')
DB_NAME = os.getenv('DB_NAME', 'hr_portal')
app.config['SQLALCHEMY_DATABASE_URI'] = f'mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}/{DB_NAME}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

mimetypes.add_type('application/wasm', '.wasm')

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'candidate_login'
login_manager.login_message = 'Please login to access this page.'

# ============ MICROSOFT / SSO ============
TENANT_ID = os.getenv('TENANT_ID')
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')
SENDER_EMAIL = os.getenv('SENDER_EMAIL')
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
REDIRECT_URI = os.getenv('REDIRECT_URI', 'http://localhost:8012/getAToken')
SCOPE = ["User.Read", "email"]

IMAP_SERVER = os.getenv('IMAP_SERVER', 'orientindia.icewarpcloud.in')
IMAP_PORT = int(os.getenv('IMAP_PORT', 993))

otp_storage = {}
HR_ROLES = ['admin', 'hr', 'hr lead']

_audio_device_state: dict[int, dict] = {}

# ============ VIDEO RECORDINGS DIR ============
RECORDINGS_DIR = os.path.join(os.path.dirname(__file__), 'recordings')
os.makedirs(RECORDINGS_DIR, exist_ok=True)


# ============ CONTEXT ============
@app.context_processor
def inject_session_config():
    return {
        'session_timeout_seconds': SESSION_TIMEOUT_MINUTES * 60,
        'session_timeout_minutes': SESSION_TIMEOUT_MINUTES
    }


# ============ DECORATORS ============
def login_required_with_session(f):
    @wraps(f)
    def wrap(*a, **k):
        if not current_user.is_authenticated:
            flash('Please login to access this page.', 'warning')
            return redirect(url_for('candidate_login'))
        return f(*a, **k)

    return wrap


def role_required(allowed_roles):
    def decorator(f):
        @wraps(f)
        def wrap(*a, **k):
            if not current_user.is_authenticated:
                flash('Please login.', 'warning')
                return redirect(url_for('candidate_login'))
            conn = get_db_connection()
            try:
                with conn.cursor() as c:
                    c.execute("SELECT role FROM users WHERE email=%s AND is_active=1", (current_user.email,))
                    r = c.fetchone()
                    role = r['role'].lower() if r else 'candidate'
                    if role not in [x.lower() for x in allowed_roles]:
                        flash('No permission.', 'danger')
                        return redirect(url_for('candidate_dashboard'))
            finally:
                if conn: conn.close()
            return f(*a, **k)

        return wrap

    return decorator


# ============ DB HELPER ============
def get_db_connection():
    try:
        return pymysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME,
            cursorclass=DictCursor, charset='utf8mb4', use_unicode=True,
            connect_timeout=10, autocommit=True
        )
    except Exception as e:
        print(f"DB error: {e}")
        return None


def _load_attempt_for_current_user(attempt_id: int):
    conn = get_db_connection()
    if not conn:
        return None, ('DB error.', 500)
    try:
        with conn.cursor() as c:
            c.execute("SELECT * FROM assessment_attempts WHERE id=%s", (attempt_id,))
            at = c.fetchone()
            if not at:
                return None, ('Attempt not found.', 404)
            if at['candidate_email'] != current_user.email:
                role_rows = get_user_roles(current_user.email)
                is_hr = any(r in [x.lower() for x in HR_ROLES] for r in role_rows)
                if not is_hr:
                    return None, ('Forbidden.', 403)
            return at, None
    finally:
        conn.close()


# ─── MISSING HELPERS FIXED HERE ──────────────────────────────────────────
def _precheck_missing(at):
    """True if precheck has not passed for this attempt."""
    if not at: return True
    if at.get('is_sample'): return False
    return not at.get('precheck_passed_at')


def _precheck_error():
    return jsonify({'ok': False, 'error': 'precheck_required'}), 409


class _DetectorStub:
    def capabilities(self):
        try:
            available = bool(audio_detector_available())
        except Exception:
            available = False
        return {
            'available': available,
            'covered': ['earphone', 'headset', 'mobile_phone', 'cell_phone', 'book', 'person'],
            'person_covered': False,
        }


class _JobManagerStub:
    def get(self, job_id, attempt_id, email): return None

    def cancel_attempt(self, attempt_id): return None


_detector_singleton = None
_job_manager_singleton = None


def get_detector():
    global _detector_singleton
    if _detector_singleton is None:
        _detector_singleton = _DetectorStub()
    return _detector_singleton


def get_job_manager():
    global _job_manager_singleton
    if _job_manager_singleton is None:
        _job_manager_singleton = _JobManagerStub()
    return _job_manager_singleton


# ─────────────────────────────────────────────────────────────────────────

# ============ LOGIN ACTIVITY ============
def record_login(user, auth_type='local'):
    now = datetime.utcnow()
    try:
        user.last_login = now
        db.session.commit()
    except Exception:
        db.session.rollback()
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as c:
            c.execute("""INSERT INTO user_login_activity
                         (user_id, email, name, auth_type, login_time, ip_address, user_agent)
                         VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                      (user.id, user.email, user.name, auth_type, now,
                       request.remote_addr, (request.headers.get('User-Agent') or '')[:255]))
    except Exception as e:
        print(f"[record_login] {e}")
    finally:
        conn.close()

# ============ SSO ROUTES ============
@app.route('/candidate-sso-login')
def candidate_sso_login():
    session["candidate_next_url"] = request.args.get("next", url_for("candidate_dashboard"))
    auth_url = build_msal_app().get_authorization_request_url(scopes=SCOPE, redirect_uri=REDIRECT_URI)
    return redirect(auth_url)


@app.route('/getAToken')
def candidate_sso_callback():
    code = request.args.get("code")
    if not code:
        flash("SSO login failed.", "danger")
        return redirect(url_for('candidate_login'))
    result = build_msal_app().acquire_token_by_authorization_code(code, scopes=SCOPE, redirect_uri=REDIRECT_URI)
    if not result or "access_token" not in result:
        flash("SSO failed.", "danger")
        return redirect(url_for('candidate_login'))
    claims = result.get("id_token_claims", {})
    email = claims.get("email", "").strip().lower()
    name = claims.get("name", "")
    if not email:
        flash("Email not found.", "danger")
        return redirect(url_for('candidate_login'))
    ad_phone = get_ad_phone(email)
    user = CandidateUser.query.filter_by(email=email).first()
    if not user:
        conn = get_db_connection()
        if not conn:
            flash("DB error.", "danger")
            return redirect(url_for('candidate_login'))
        try:
            with conn.cursor() as c:
                c.execute("SELECT email FROM users WHERE email=%s", (email,))
                if not c.fetchone():
                    c.execute("INSERT INTO users (name,email,role,is_active,created_at) VALUES (%s,%s,'requestor',1,NOW())", (name, email))
                else:
                    c.execute("UPDATE users SET role='requestor', is_active=1 WHERE email=%s", (email,))
                user = CandidateUser(name=name or email.split("@")[0], email=email, phone=ad_phone, password=None)
                db.session.add(user); db.session.commit()
                flash("Account created via SSO.", "success")
        except Exception as e:
            db.session.rollback()
            print(f"SSO user err: {e}")
            return redirect(url_for('candidate_login'))
        finally:
            conn.close()
    elif not user.phone and ad_phone:
        user.phone = ad_phone; db.session.commit()

    login_user(user, remember=False)
    record_login(user, auth_type='sso')
    session.permanent = True
    session["auth_type"] = "sso"
    session["roles"] = get_user_roles(email)
    session["last_activity"] = datetime.utcnow().isoformat()
    flash("SSO Login successful!", "success")
    return redirect(session.get("candidate_next_url", url_for("candidate_dashboard")))


@app.route('/candidate-icewarp-login', methods=['GET', 'POST'])
def candidate_icewarp_login():
    if request.method == "GET":
        return render_template("icewarp_candidate_login.html")
    email = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "").strip()
    next_url = request.form.get("next", url_for("candidate_dashboard"))
    if not email or not password:
        flash("Enter email and password.", "danger")
        return redirect(url_for('candidate_icewarp_login'))
    if not validate_icewarp_credentials(email, password):
        flash("Invalid IceWarp credentials.", "danger")
        return redirect(url_for('candidate_icewarp_login'))

    ad_phone = get_ad_phone(email)
    user = CandidateUser.query.filter_by(email=email).first()
    if not user:
        conn = get_db_connection()
        try:
            with conn.cursor() as c:
                c.execute("SELECT email FROM users WHERE email=%s", (email,))
                if not c.fetchone():
                    c.execute("INSERT INTO users (name,email,role,is_active,created_at) VALUES (%s,%s,'requestor',1,NOW())", (email.split("@")[0], email))
                else:
                    c.execute("UPDATE users SET role='requestor', is_active=1 WHERE email=%s", (email,))
                user = CandidateUser(name=email.split("@")[0], email=email, phone=ad_phone, password=None)
                db.session.add(user); db.session.commit()
                flash("Account created.", "success")
        except Exception as e:
            db.session.rollback(); print(f"IceWarp err: {e}")
            return redirect(url_for('candidate_login'))
        finally:
            if conn: conn.close()
    elif not user.phone and ad_phone:
        user.phone = ad_phone; db.session.commit()

    login_user(user, remember=False)
    record_login(user, auth_type='icewarp')
    session.permanent = True
    session["auth_type"] = "icewarp"
    session["roles"] = get_user_roles(email)
    session["last_activity"] = datetime.utcnow().isoformat()
    flash("IceWarp Login successful!", "success")
    return redirect(next_url)

# ============ SSO HELPERS ============
def build_msal_app():
    return msal.ConfidentialClientApplication(CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET)


def validate_icewarp_credentials(username, password, server=IMAP_SERVER, port=IMAP_PORT):
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with imaplib.IMAP4_SSL(server, port, ssl_context=ctx) as imap:
            imap.login(username, password)
            return True
    except Exception:
        return False


def get_access_token():
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    payload = {"grant_type": "client_credentials", "client_id": CLIENT_ID,
               "client_secret": CLIENT_SECRET, "scope": "https://graph.microsoft.com/.default"}
    try:
        r = requests.post(url, data=payload, timeout=10)
        r.raise_for_status()
        return r.json()["access_token"]
    except Exception as e:
        print(f"Token err: {e}")
        return None


def get_ad_phone(email):
    try:
        token = get_access_token()
        if not token: return None
        url = f"https://graph.microsoft.com/v1.0/users/{email}?$select=mobilePhone,businessPhones"
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if r.status_code != 200: return None
        data = r.json()
        phone = data.get("mobilePhone") or (data.get("businessPhones") or [None])[0]
        if phone:
            phone = ''.join(c for c in phone if c.isdigit() or c == '+')
            return phone[:15] if phone else None
    except Exception as e:
        print(f"AD phone err: {e}")
    return None


def get_user_roles(email):
    conn = get_db_connection()
    if not conn: return ['candidate']
    try:
        with conn.cursor() as c:
            c.execute("SELECT role FROM users WHERE email=%s AND is_active=1", (email,))
            r = c.fetchone()
            return [r['role'].lower()] if r else ['candidate']
    finally:
        conn.close()


# ============ EMAIL ============
def generate_otp():
    return ''.join(secrets.choice(string.digits) for _ in range(6))


def send_email_via_graph(to_email, subject, body):
    try:
        token = get_access_token()
        if not token: return False
        url = f"https://graph.microsoft.com/v1.0/users/{SENDER_EMAIL}/sendMail"
        msg = {"message": {"subject": subject,
                           "body": {"contentType": "HTML", "content": body},
                           "toRecipients": [{"emailAddress": {"address": to_email}}]}}
        r = requests.post(url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                          data=json.dumps(msg), timeout=30)
        return r.status_code == 202
    except Exception as e:
        print(f"Email err: {e}")
        return False


def send_otp_email(email, otp):
    return send_email_via_graph(email, "🔐 OTP Verification - Candidate Portal",
                                f'<div style="font-family:Arial;padding:20px"><h2>OTP Verification</h2><p>Your OTP: <b style="font-size:28px;color:#4355e0">{otp}</b></p><p>Valid 5 minutes.</p></div>')


def send_password_reset_confirmation(email):
    return send_email_via_graph(email, "✅ Password Reset Successful",
                                '<div style="font-family:Arial;padding:20px"><h2>Password Reset Successful</h2><p>Your password has been reset successfully.</p></div>')


# ============ MODELS ============
class CandidateUser(UserMixin, db.Model):
    __tablename__ = 'candidate_users'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    phone = db.Column(db.String(15), unique=True, nullable=True)
    password = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    profile_updated_at = db.Column(db.DateTime, nullable=True)
    last_login = db.Column(db.DateTime, nullable=True)
    date_of_birth = db.Column(db.Date)
    gender = db.Column(db.String(20))
    address = db.Column(db.Text)
    city = db.Column(db.String(100))
    state = db.Column(db.String(100))
    pincode = db.Column(db.String(10))
    total_experience = db.Column(db.String(50))
    current_company = db.Column(db.String(100))
    current_designation = db.Column(db.String(100))
    current_ctc = db.Column(db.String(50))
    variable_ctc = db.Column(db.String(50))
    expected_ctc = db.Column(db.String(50))
    notice_period = db.Column(db.Integer)
    skills = db.Column(db.Text)
    aadhar_number = db.Column(db.String(20))
    pan_number = db.Column(db.String(20))
    has_pf = db.Column(db.Boolean, default=False)
    profile_image = db.Column(db.LargeBinary)
    highest_degree = db.Column(db.String(100))
    aadhar_document = db.Column(db.LargeBinary);
    aadhar_filename = db.Column(db.String(255))
    pan_document = db.Column(db.LargeBinary);
    pan_filename = db.Column(db.String(255))
    passport_photo = db.Column(db.LargeBinary);
    passport_photo_filename = db.Column(db.String(255))
    resume_document = db.Column(db.LargeBinary);
    resume_filename = db.Column(db.String(255))


class UserLoginActivity(db.Model):
    __tablename__ = 'user_login_activity'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer)
    email = db.Column(db.String(100), nullable=False, index=True)
    name = db.Column(db.String(100))
    auth_type = db.Column(db.String(20), default='local')
    login_time = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(255))


class Assessment(db.Model):
    __tablename__ = 'assessments'
    id = db.Column(db.Integer, primary_key=True)
    requisition_id = db.Column(db.Integer)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    total_questions = db.Column(db.Integer, default=10)
    duration_minutes = db.Column(db.Integer, default=30)
    pass_rating = db.Column(Numeric(3, 1), default=2.0)
    is_sample = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    share_token = db.Column(db.String(64), unique=True)
    created_by = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AssessmentQuestion(db.Model):
    __tablename__ = 'assessment_questions'
    id = db.Column(db.Integer, primary_key=True)
    assessment_id = db.Column(db.Integer, nullable=False, index=True)
    question_text = db.Column(db.Text, nullable=False)
    question_type = db.Column(db.String(20), default='MCQ_TEXT')
    code_snippet = db.Column(db.Text)
    option_a = db.Column(db.Text);
    option_b = db.Column(db.Text)
    option_c = db.Column(db.Text);
    option_d = db.Column(db.Text)
    correct_option = db.Column(db.String(1))
    expected_answer = db.Column(db.Text)
    difficulty = db.Column(db.String(10), default='Medium')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AssessmentAttempt(db.Model):
    __tablename__ = 'assessment_attempts'
    id = db.Column(db.Integer, primary_key=True)
    assessment_id = db.Column(db.Integer, nullable=False, index=True)
    candidate_id = db.Column(db.Integer, nullable=False)
    candidate_email = db.Column(db.String(100), nullable=False, index=True)
    requisition_id = db.Column(db.Integer)
    started_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    deadline_at = db.Column(db.DateTime)
    submitted_at = db.Column(db.DateTime)
    terminated_at = db.Column(db.DateTime)
    status = db.Column(db.String(20), default='In Progress')
    score = db.Column(db.Integer, default=0)
    max_score = db.Column(db.Integer, default=0)
    answered_count = db.Column(db.Integer, default=0)
    rating = db.Column(Numeric(3, 1), default=0.0)
    warning_count = db.Column(db.Integer, default=0)
    first_warning_reason = db.Column(db.String(255))
    termination_reason = db.Column(db.String(255))
    termination_source = db.Column(db.String(20))
    video_path = db.Column(db.String(500))
    whiteboard_zip_path = db.Column(db.String(500))
    retest_allowed = db.Column(db.Boolean, default=False)
    reviewed_by_admin = db.Column(db.Boolean, default=False)
    precheck_report = db.Column(db.Text)
    device_info = db.Column(db.String(500))
    last_heartbeat_at = db.Column(db.DateTime, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Added to fix DB mismatch
    identity_snapshot = db.Column(db.Text)
    precheck_passed_at = db.Column(db.DateTime)
    precheck_capability_hash = db.Column(db.String(64))
    monitoring_finalized_at = db.Column(db.DateTime)
    monitoring_status = db.Column(db.Text)
    proctoring_summary = db.Column(db.Text)
    last_submission_id = db.Column(db.String(64))


class AssessmentAnswer(db.Model):
    __tablename__ = 'assessment_answers'
    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, nullable=False, index=True)
    question_id = db.Column(db.Integer, nullable=False)
    display_order = db.Column(db.Integer)
    selected_option = db.Column(db.Text)
    is_correct = db.Column(db.Boolean)
    time_spent_seconds = db.Column(db.Integer, default=0)


class ProctoringEvent(db.Model):
    __tablename__ = 'proctoring_events'
    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, nullable=False, index=True)
    incident_key = db.Column(db.String(80), index=True)
    event_type = db.Column(db.String(50), nullable=False)
    severity = db.Column(db.String(20), default='Medium')
    source = db.Column(db.String(20), default='client')
    description = db.Column(db.String(500))
    duration_ms = db.Column(db.Integer, default=0)
    confidence = db.Column(db.Float)
    device_info = db.Column(db.String(255))
    snapshot_b64 = db.Column(db.Text)
    evidence_path = db.Column(db.String(500))
    review_status = db.Column(db.String(20), default='pending')
    reviewed_by = db.Column(db.String(100))
    reviewed_at = db.Column(db.DateTime)
    review_notes = db.Column(db.Text)
    occurred_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Added to fix DB mismatch
    incident_id = db.Column(db.String(64), index=True)
    event_state = db.Column(db.String(20), default='started', nullable=False)
    last_seen_at = db.Column(db.DateTime)
    ended_at = db.Column(db.DateTime)
    metadata_json = db.Column(db.Text)
    model_name = db.Column(db.String(100))


class AssessmentWhiteboard(db.Model):
    __tablename__ = 'assessment_whiteboards'
    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, nullable=False)
    question_id = db.Column(db.Integer, nullable=False)
    image_data = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('attempt_id', 'question_id', name='uq_wb'),)


class AttemptHeartbeat(db.Model):
    __tablename__ = 'attempt_heartbeats'
    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, nullable=False, index=True)
    beat_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    client_seq = db.Column(db.Integer, default=0)
    ip = db.Column(db.String(45))
    user_agent = db.Column(db.String(255))


class AttemptAutosave(db.Model):
    __tablename__ = 'attempt_autosaves'
    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, nullable=False, index=True)
    question_id = db.Column(db.Integer, nullable=False)
    selected_option = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('attempt_id', 'question_id', name='uq_autosave'),)


@login_manager.user_loader
def load_user(uid):
    return db.session.get(CandidateUser, int(uid))


# ============ HELPERS ============
def get_mime_type(filename):
    ext = '.' + filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    return {'.pdf': 'application/pdf', '.doc': 'application/msword',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.png': 'image/png', '.gif': 'image/gif', '.txt': 'text/plain'
            }.get(ext, 'application/octet-stream')


def is_profile_complete(user):
    for f in [user.total_experience, user.current_designation, user.city,
              user.current_ctc, user.expected_ctc, user.notice_period,
              user.skills, user.resume_document, user.resume_filename, user.date_of_birth]:
        if not f or str(f).strip() == '':
            return False
    return True


def _decode_frame_size(frame_b64: str):
    try:
        if ',' in frame_b64:
            frame_b64 = frame_b64.split(',', 1)[1]
        raw = base64.b64decode(frame_b64, validate=False)
        with Image.open(io.BytesIO(raw)) as im:
            return int(im.width), int(im.height)
    except Exception as e:
        print(f"[_decode_frame_size] {e}")
        return 0, 0


def _is_forbidden_class(name: str) -> bool:
    if not name:
        return False
    n = name.strip().lower().replace('-', '_').replace(' ', '_')
    return any(tok in n for tok in (
        'headphone', 'headset', 'earphone', 'earbud', 'airpod', 'neckband', 'earpiece',
        'mobile_phone', 'cell_phone', 'book'
    ))


def filter_audio_detections(detections, frame_width, frame_height):
    if not frame_width or not frame_height:
        return detections

    valid = []
    for det in detections or []:
        cls_name = (det.get('class') or '').strip()
        if cls_name and not _is_forbidden_class(cls_name):
            continue
        try:
            x1, y1, x2, y2 = [float(v) for v in det['bbox']]
        except (KeyError, TypeError, ValueError):
            continue
        if x2 <= x1 or y2 <= y1:
            continue
        valid.append(det)
    return valid


# ============ AUTH ROUTES ============
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('candidate_dashboard'))
    return redirect(url_for('candidate_login'))


@app.route('/candidate-login', methods=['GET', 'POST'])
def candidate_login():
    if current_user.is_authenticated:
        return redirect(url_for('candidate_dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = CandidateUser.query.filter_by(email=email).first()
        if user and user.password and check_password_hash(user.password, password):
            login_user(user, remember=False)
            record_login(user, auth_type='local')
            session.permanent = True
            session["auth_type"] = "local"
            session["roles"] = get_user_roles(email)
            session["last_activity"] = datetime.utcnow().isoformat()
            if not session.get("local_first_login_done"):
                session["local_first_login_done"] = True
                return redirect(url_for('candidate_profile'))
            if not is_profile_complete(user):
                flash('Please complete your profile.', 'warning')
                return redirect(url_for('candidate_profile'))
            return redirect(url_for('candidate_dashboard'))
        flash('Invalid email or password.', 'danger')
    return render_template('candidate_login.html')


@app.route('/candidate-register', methods=['GET', 'POST'])
def candidate_register():
    if current_user.is_authenticated:
        return redirect(url_for('candidate_dashboard'))
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone')
        password = request.form.get('password')
        confirm = request.form.get('confirm_password')
        terms = request.form.get('terms_agreed')
        if not all([name, email, phone, password]):
            flash('All fields required.', 'danger');
            return render_template('candidate_login.html')
        phone_clean = ''.join(filter(str.isdigit, phone))
        if len(phone_clean) != 10:
            flash('Invalid 10-digit phone.', 'danger');
            return render_template('candidate_login.html')
        if '@' not in email or '.' not in email:
            flash('Invalid email.', 'danger');
            return render_template('candidate_login.html')
        if len(password) < 6:
            flash('Password min 6 chars.', 'danger');
            return render_template('candidate_login.html')
        if password != confirm:
            flash('Passwords do not match.', 'danger');
            return render_template('candidate_login.html')
        if not terms:
            flash('Accept terms.', 'danger');
            return render_template('candidate_login.html')
        if CandidateUser.query.filter_by(email=email).first():
            flash('Email already registered.', 'danger');
            return render_template('candidate_login.html')
        if CandidateUser.query.filter_by(phone=phone_clean).first():
            flash('Phone already exists.', 'danger');
            return render_template('candidate_login.html')
        user = CandidateUser(name=name, email=email, phone=phone_clean,
                             password=generate_password_hash(password))
        db.session.add(user)
        conn = get_db_connection()
        if not conn:
            flash('DB error.', 'danger');
            db.session.rollback();
            return render_template('candidate_login.html')
        try:
            with conn.cursor() as c:
                c.execute("SELECT email FROM users WHERE email=%s", (email,))
                if not c.fetchone():
                    c.execute(
                        "INSERT INTO users (name,email,role,is_active,created_at) VALUES (%s,%s,'candidate',1,NOW())",
                        (name, email))
                else:
                    c.execute("UPDATE users SET role='candidate', is_active=1 WHERE email=%s", (email,))
        except Exception as e:
            flash(f'Registration failed: {e}', 'danger');
            db.session.rollback();
            return render_template('candidate_login.html')
        finally:
            conn.close()
        db.session.commit()
        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('candidate_login'))
    return render_template('candidate_login.html')


@app.route('/candidate-logout')
@login_required
def candidate_logout():
    auth_type = session.get('auth_type')
    session.clear();
    logout_user()
    flash('Logged out.', 'info')
    if auth_type == 'sso':
        return redirect("https://login.microsoftonline.com/common/oauth2/v2.0/logout"
                        "?post_logout_redirect_uri=" + url_for('candidate_login', _external=True))
    resp = redirect(url_for('candidate_login'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'send_otp':
            phone = request.form.get('phone')
            if not phone: return jsonify({'success': False, 'message': 'Phone required'})
            user = CandidateUser.query.filter_by(phone=phone).first()
            if not user: return jsonify({'success': False, 'message': 'No account with this phone'})
            otp = generate_otp()
            otp_storage[user.email] = {'otp': otp, 'created_at': datetime.utcnow(), 'attempts': 0, 'phone': phone}
            if send_otp_email(user.email, otp):
                return jsonify({'success': True, 'message': 'OTP sent', 'email': user.email})
            return jsonify({'success': False, 'message': 'Failed to send OTP'})
        elif action == 'verify_otp':
            email = request.form.get('email');
            otp = request.form.get('otp')
            if email not in otp_storage: return jsonify({'success': False, 'message': 'OTP not found'})
            s = otp_storage[email]
            if datetime.utcnow() - s['created_at'] > timedelta(minutes=5):
                del otp_storage[email];
                return jsonify({'success': False, 'message': 'OTP expired'})
            if s['attempts'] >= 3:
                del otp_storage[email];
                return jsonify({'success': False, 'message': 'Too many attempts'})
            if s['otp'] == otp:
                session['reset_email'] = email;
                session['reset_verified'] = True
                return jsonify({'success': True, 'message': 'Verified'})
            s['attempts'] += 1
            return jsonify({'success': False, 'message': 'Invalid OTP'})
        elif action == 'reset_password':
            if not session.get('reset_verified'):
                return jsonify({'success': False, 'message': 'Verify OTP first'})
            email = session.get('reset_email')
            new_p = request.form.get('new_password');
            conf = request.form.get('confirm_password')
            if not new_p or new_p != conf or len(new_p) < 6:
                return jsonify({'success': False, 'message': 'Invalid password'})
            user = CandidateUser.query.filter_by(email=email).first()
            if user:
                user.password = generate_password_hash(new_p);
                db.session.commit()
                send_password_reset_confirmation(email)
                session.pop('reset_verified', None);
                session.pop('reset_email', None)
                otp_storage.pop(email, None)
                return jsonify({'success': True, 'message': 'Password reset'})
            return jsonify({'success': False, 'message': 'User not found'})
    return render_template('forgot_password.html')


# ============ DASHBOARD / PROFILE / APPLICATIONS ============
@app.route('/candidate-dashboard')
@login_required
@login_required_with_session
def candidate_dashboard():
    conn = get_db_connection()
    requisitions = []
    try:
        with conn.cursor() as c:
            c.execute("""SELECT id, job_title, department, number_of_openings,
                                skills_required, location, budget, job_description,
                                created_at, work_location_type, experience_required, screening_questions
                         FROM requisitions WHERE status='Open' ORDER BY created_at DESC""")
            requisitions = c.fetchall()
    except Exception as e:
        flash(f'Error loading jobs: {e}', 'danger')
    finally:
        if conn: conn.close()
    return render_template('candidate_dashboard.html', requisitions=requisitions)


@app.route('/candidate-apply/<int:requisition_id>', methods=['POST'])
@login_required
@login_required_with_session
def candidate_apply(requisition_id):
    if not is_profile_complete(current_user):
        flash('Complete your profile first.', 'warning')
        return redirect(url_for('candidate_profile'))
    if not current_user.resume_document:
        flash('Upload resume first.', 'warning')
        return redirect(url_for('candidate_profile'))

    reason = request.form.get('reason_for_change', 'Looking for better opportunity')
    holding = request.form.get('holding_other_offer', 'No')

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT id FROM candidates WHERE email=%s AND requisition_id=%s",
                      (current_user.email, requisition_id))
            if c.fetchone():
                flash('Already applied.', 'warning');
                return redirect(url_for('candidate_dashboard'))
            c.execute("SELECT email FROM users WHERE email=%s", (current_user.email,))
            if not c.fetchone():
                c.execute("INSERT INTO users (name,email,role,is_active,created_at) VALUES (%s,%s,'candidate',1,NOW())",
                          (current_user.name, current_user.email))
            c.execute("SELECT job_title FROM requisitions WHERE id=%s", (requisition_id,))
            if not c.fetchone():
                flash('Job not found.', 'danger');
                return redirect(url_for('candidate_dashboard'))
            c.execute("""INSERT INTO candidates (
                            name, email, phone, skills, requisition_id, resume, submitted_by,
                            total_exp, current_loc, current_ctc, exp_ctc, source,
                            designation, notice_period, reason_for_change, holding_other_offer,
                            selection_status)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                      (current_user.name, current_user.email, current_user.phone,
                       current_user.skills or '', requisition_id,
                       current_user.resume_document, current_user.email,
                       current_user.total_experience or '', current_user.city or '',
                       current_user.current_ctc or '', current_user.expected_ctc or '',
                       "Self Applied - Candidate Portal",
                       current_user.current_designation or '',
                       current_user.notice_period or 0,
                       reason, holding, 'Applied'))
            flash('Application submitted!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    finally:
        if conn: conn.close()
    return redirect(url_for('candidate_dashboard'))


@app.route('/candidate-applications')
@login_required
@login_required_with_session
def candidate_applications():
    conn = get_db_connection()
    applications = []
    try:
        with conn.cursor() as c:
            c.execute("""SELECT c.*, r.job_title, r.department, r.location,
                                c.selection_status, c.created_at AS applied_date,
                                latest_interview.interview_datetime,
                                latest_interview.interviewer_name,
                                latest_interview.interviewer_email,
                                latest_interview.interview_type,
                                (SELECT at.status FROM assessment_attempts at
                                 WHERE at.candidate_email = c.email
                                   AND at.requisition_id = c.requisition_id
                                 ORDER BY at.id DESC LIMIT 1) AS attempt_status,
                                (SELECT at.retest_allowed FROM assessment_attempts at
                                 WHERE at.candidate_email = c.email
                                   AND at.requisition_id = c.requisition_id
                                 ORDER BY at.id DESC LIMIT 1) AS retest_allowed
                         FROM candidates c
                         JOIN requisitions r ON c.requisition_id = r.id
                         LEFT JOIN (SELECT candidate_id, MAX(interview_datetime) AS interview_datetime
                                    FROM interview_schedule GROUP BY candidate_id) latest
                                ON c.id = latest.candidate_id
                         LEFT JOIN interview_schedule latest_interview
                                ON latest_interview.candidate_id = latest.candidate_id
                               AND latest_interview.interview_datetime = latest.interview_datetime
                         WHERE c.submitted_by=%s ORDER BY c.id DESC""", (current_user.email,))
            applications = c.fetchall()
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    finally:
        if conn: conn.close()
    return render_template('candidate_applications.html', applications=applications)


@app.route('/candidate-profile', methods=['GET', 'POST'])
@login_required
@login_required_with_session
def candidate_profile():
    if request.method == 'POST':
        user = current_user
        action = request.form.get('action', 'save')
        phone = request.form.get('phone')
        if phone and not user.phone:
            clean = ''.join(filter(str.isdigit, phone))
            if len(clean) == 10:
                ex = CandidateUser.query.filter_by(phone=clean).first()
                if ex and ex.id != user.id:
                    flash('Phone already registered.', 'danger')
                    return redirect(url_for('candidate_profile'))
                user.phone = clean
        user.date_of_birth = request.form.get('date_of_birth') or None
        user.gender = request.form.get('gender')
        user.address = request.form.get('address')
        user.city = request.form.get('city')
        user.state = request.form.get('state')
        user.pincode = request.form.get('pincode')
        user.total_experience = request.form.get('total_experience')
        user.current_company = request.form.get('current_company')
        user.current_designation = request.form.get('current_designation')
        user.expected_ctc = request.form.get('expected_ctc')
        user.skills = request.form.get('skills')
        user.aadhar_number = request.form.get('aadhar_number')
        user.has_pf = request.form.get('has_pf') == 'Yes'
        user.highest_degree = request.form.get('highest_degree')
        user.current_ctc = request.form.get('fixed_ctc', '0')
        user.variable_ctc = request.form.get('variable_ctc', '0')
        np = request.form.get('notice_period')
        try:
            user.notice_period = int(np) if np else None
        except ValueError:
            user.notice_period = None
        for fk, dk, nk in [('resume', 'resume_document', 'resume_filename'),
                           ('profile_image', 'profile_image', None),
                           ('aadhar', 'aadhar_document', 'aadhar_filename'),
                           ('pan', 'pan_document', 'pan_filename'),
                           ('passport_photo', 'passport_photo', 'passport_photo_filename')]:
            if fk in request.files:
                f = request.files[fk]
                if f and f.filename:
                    setattr(user, dk, f.read())
                    if nk: setattr(user, nk, f.filename)
        try:
            user.profile_updated_at = datetime.utcnow()
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash(f'Error: {e}', 'danger')
            return redirect(url_for('candidate_profile'))
        flash('Profile saved.', 'info' if action == 'draft' else 'success')
        return redirect(url_for('candidate_profile'))

    conn = get_db_connection()
    is_selected = False
    try:
        with conn.cursor() as c:
            c.execute("SELECT 1 FROM candidates WHERE submitted_by=%s AND selection_status='Selected' LIMIT 1",
                      (current_user.email,))
            is_selected = c.fetchone() is not None
    finally:
        if conn: conn.close()
    return render_template('candidate_profile.html', user=current_user,
                           is_selected=is_selected, is_profile_complete=is_profile_complete(current_user))


@app.route('/candidate-download/<doc_type>')
@login_required
@login_required_with_session
def candidate_download(doc_type):
    u = current_user
    data = fn = None
    if doc_type == 'aadhar':
        data, fn = u.aadhar_document, u.aadhar_filename
    elif doc_type == 'pan':
        data, fn = u.pan_document, u.pan_filename
    elif doc_type == 'passport':
        data, fn = u.passport_photo, u.passport_photo_filename
    elif doc_type == 'resume':
        data, fn = u.resume_document, u.resume_filename
    if data and fn:
        return send_file(io.BytesIO(data), mimetype=get_mime_type(fn), download_name=fn, as_attachment=True)
    flash('Not found.', 'warning')
    return redirect(url_for('candidate_profile'))


# =========================================================
# ASSESSMENTS — CANDIDATE
# =========================================================
@app.route('/candidate-assessments')
@login_required
@login_required_with_session
def candidate_assessments():
    status_filter = (request.args.get('status') or 'Pending').strip()
    search = (request.args.get('q') or '').strip().lower()

    conn = get_db_connection()
    rows = []
    sample = None
    try:
        with conn.cursor() as c:
            c.execute("""SELECT * FROM assessments
                         WHERE is_sample = 1 AND is_active = 1
                         ORDER BY id DESC LIMIT 1""")
            sample = c.fetchone()

            c.execute("""SELECT id FROM assessments
                         WHERE requisition_id IS NULL AND is_sample = 0 AND is_active = 1
                         ORDER BY id DESC LIMIT 1""")
            default_row = c.fetchone()
            default_id = default_row['id'] if default_row else None

            c.execute("""
                SELECT c.id AS candidate_row_id,
                       c.requisition_id,
                       c.created_at AS applied_date,
                       r.job_title,
                       r.department,
                       (SELECT a1.id FROM assessments a1
                        WHERE a1.requisition_id = c.requisition_id
                          AND a1.is_active = 1 AND a1.is_sample = 0
                        ORDER BY a1.id DESC LIMIT 1) AS specific_assessment_id,
                       (SELECT at.id FROM assessment_attempts at
                        WHERE at.candidate_email = c.email
                          AND at.requisition_id = c.requisition_id
                        ORDER BY at.id DESC LIMIT 1) AS attempt_id,
                       (SELECT at.status FROM assessment_attempts at
                        WHERE at.candidate_email = c.email
                          AND at.requisition_id = c.requisition_id
                        ORDER BY at.id DESC LIMIT 1) AS attempt_status,
                       (SELECT at.rating FROM assessment_attempts at
                        WHERE at.candidate_email = c.email
                          AND at.requisition_id = c.requisition_id
                        ORDER BY at.id DESC LIMIT 1) AS attempt_rating,
                       (SELECT at.submitted_at FROM assessment_attempts at
                        WHERE at.candidate_email = c.email
                          AND at.requisition_id = c.requisition_id
                        ORDER BY at.id DESC LIMIT 1) AS attempt_submitted
                FROM candidates c
                JOIN requisitions r ON c.requisition_id = r.id
                WHERE c.email = %s
                  AND (
                       c.selection_status = 'Applied'
                       OR EXISTS (
                            SELECT 1 FROM assessment_attempts at
                            WHERE at.candidate_email = c.email
                              AND at.requisition_id = c.requisition_id
                       )
                  )
                ORDER BY c.id DESC
            """, (current_user.email,))
            applications = c.fetchall()
    except Exception as e:
        flash(f'Error: {e}', 'danger')
        applications = []
    finally:
        if conn: conn.close()

    for a in applications:
        aid = a['specific_assessment_id'] or default_id
        if not aid:
            continue

        if not a['attempt_id'] or a['attempt_status'] == 'In Progress':
            disp_status = 'Pending'
            disp_result = '—'
        else:
            disp_status = 'Completed'
            disp_result = 'Terminated' if a['attempt_status'] == 'Terminated' else 'Submitted'

        rows.append({
            'application_id': a['candidate_row_id'],
            'requisition_id': a['requisition_id'],
            'job_title': a['job_title'],
            'department': a['department'],
            'applied_date': a['applied_date'],
            'assessment_id': aid,
            'attempt_id': a['attempt_id'],
            'rating': a.get('attempt_rating'),
            'attempt_submitted': a.get('attempt_submitted'),
            'display_status': disp_status,
            'display_result': disp_result,
        })

    if status_filter and status_filter.lower() != 'all':
        rows = [r for r in rows if r['display_status'].lower() == status_filter.lower()]
    if search:
        rows = [r for r in rows if search in (r['job_title'] or '').lower()
                or search in (r['department'] or '').lower()]

    return render_template('candidate_assessments.html',
                           applications=rows,
                           sample=sample,
                           status_filter=status_filter, search=search)


@app.route('/api/assessment/<int:assessment_id>/preview')
@login_required
def assessment_preview(assessment_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""SELECT a.*, r.job_title AS requisition_title
                         FROM assessments a
                         LEFT JOIN requisitions r ON a.requisition_id = r.id
                         WHERE a.id=%s AND a.is_active=1""", (assessment_id,))
            a = c.fetchone()
            if not a:
                return jsonify({'ok': False}), 404
            return jsonify({
                'ok': True, 'id': a['id'], 'title': a['title'],
                'description': a['description'] or '',
                'total_questions': a['total_questions'],
                'duration_minutes': a['duration_minutes'],
                'is_sample': bool(a['is_sample']),
                'requisition_title': a['requisition_title'] or ''
            })
    finally:
        if conn: conn.close()


@app.route('/api/attempt/<int:attempt_id>/result')
@login_required
def assessment_result_api(attempt_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""SELECT at.*, a.title FROM assessment_attempts at
                         JOIN assessments a ON at.assessment_id = a.id
                         WHERE at.id=%s AND at.candidate_email=%s""",
                      (attempt_id, current_user.email))
            at = c.fetchone()
            if not at:
                return jsonify({'ok': False}), 404
            return jsonify({
                'ok': True,
                'id': at['id'],
                'title': at['title'],
                'status': at['status'],
                'submitted_at': at['submitted_at'].strftime('%d %b %Y, %I:%M %p') if at['submitted_at'] else None,
                'first_warning_reason': at['first_warning_reason'],
                'termination_reason': at['termination_reason'],
                'score': at['score'] or 0,
                'max_score': at['max_score'] or 0,
                'rating': float(at['rating'] or 0),
                'answered_count': at['answered_count'] or 0,
                'warning_count': at['warning_count'] or 0,
            })
    finally:
        if conn: conn.close()


@app.route('/candidate-assessment/<int:assessment_id>/start', methods=['POST'])
@login_required
@login_required_with_session
def start_assessment(assessment_id):
    req_raw = request.form.get('requisition_id')
    try:
        requisition_id = int(req_raw) if req_raw else None
    except (ValueError, TypeError):
        requisition_id = None

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT is_sample, duration_minutes FROM assessments WHERE id=%s",
                      (assessment_id,))
            ar = c.fetchone()
            if not ar:
                flash('Assessment not found.', 'danger')
                return redirect(url_for('candidate_assessments'))
            is_sample = bool(ar['is_sample'])
            duration = int(ar['duration_minutes'] or 30)

            if requisition_id is not None:
                c.execute("""SELECT id, status, retest_allowed, deadline_at
                             FROM assessment_attempts
                             WHERE assessment_id=%s AND candidate_email=%s
                               AND requisition_id=%s
                             ORDER BY id DESC LIMIT 1""",
                          (assessment_id, current_user.email, requisition_id))
            else:
                c.execute("""SELECT id, status, retest_allowed, deadline_at
                             FROM assessment_attempts
                             WHERE assessment_id=%s AND candidate_email=%s
                               AND requisition_id IS NULL
                             ORDER BY id DESC LIMIT 1""",
                          (assessment_id, current_user.email))
            ex = c.fetchone()

            if ex and ex['status'] != 'In Progress' and not ex['retest_allowed'] and not is_sample:
                flash('You have already attempted this assessment for this application. '
                      'Retest not permitted.', 'danger')
                return redirect(url_for('candidate_assessments'))

            if ex and ex['status'] == 'In Progress' and not is_sample:
                attempt_id = ex['id']
                # If resuming, give them a fresh deadline
                new_deadline = datetime.utcnow() + timedelta(minutes=duration)
                c.execute("UPDATE assessment_attempts SET deadline_at=%s WHERE id=%s", (new_deadline, attempt_id))
            else:
                c.execute("SELECT id FROM candidates WHERE email=%s ORDER BY id DESC LIMIT 1",
                          (current_user.email,))
                cr = c.fetchone()
                candidate_id = cr['id'] if cr else 0

                now = datetime.utcnow()
                deadline = now + timedelta(minutes=duration)

                try:
                    c.execute("""INSERT INTO assessment_attempts
                                    (assessment_id, candidate_id, candidate_email,
                                     requisition_id, status, started_at, deadline_at)
                                 VALUES (%s,%s,%s,%s,'In Progress',%s,%s)""",
                              (assessment_id, candidate_id, current_user.email,
                               requisition_id, now, deadline))
                    attempt_id = c.lastrowid
                except pymysql.err.IntegrityError:

                    c.execute("""SELECT id FROM assessment_attempts
                                 WHERE candidate_email=%s AND assessment_id=%s
                                   AND IFNULL(requisition_id,-1)=IFNULL(%s,-1)
                                   AND status='In Progress'
                                 ORDER BY id DESC LIMIT 1""",
                              (current_user.email, assessment_id, requisition_id))
                    attempt_id = c.fetchone()['id']
    finally:
        conn.close()
    return redirect(url_for('take_assessment', attempt_id=attempt_id))


@app.route('/candidate-assessment/take/<int:attempt_id>')
@login_required
@login_required_with_session
def take_assessment(attempt_id):
    import random
    conn = get_db_connection()
    questions, attempt, saved = [], None, {}
    try:
        with conn.cursor() as c:
            c.execute("""SELECT at.*, a.total_questions, a.duration_minutes,
                                a.title, a.is_sample
                         FROM assessment_attempts at
                         JOIN assessments a ON at.assessment_id = a.id
                         WHERE at.id=%s AND at.candidate_email=%s""",
                      (attempt_id, current_user.email))
            attempt = c.fetchone()
            if not attempt:
                flash('Attempt not found.', 'danger')
                return redirect(url_for('candidate_assessments'))
            if attempt['status'] != 'In Progress':
                flash('This attempt is already closed.', 'warning')
                return redirect(url_for('candidate_assessments'))
            if is_attempt_expired(attempt['deadline_at']):
                flash('This attempt has expired.', 'danger')
                return redirect(url_for('candidate_assessments'))

            c.execute("""SELECT id, question_text, question_type, code_snippet,
                                option_a, option_b, option_c, option_d
                         FROM assessment_questions WHERE assessment_id=%s
                         ORDER BY RAND() LIMIT %s""",
                      (attempt['assessment_id'], attempt['total_questions']))
            questions = c.fetchall()

            c.execute("""SELECT question_id, selected_option
                         FROM attempt_autosaves WHERE attempt_id=%s""",
                      (attempt_id,))
            saved = {str(r['question_id']): r['selected_option']
                     for r in c.fetchall()}
    finally:
        conn.close()

    if not questions:
        flash('No questions available.', 'warning')
        return redirect(url_for('candidate_assessments'))

    for q in questions:
        opts = [('A', q['option_a']), ('B', q['option_b']),
                ('C', q['option_c']), ('D', q['option_d'])]
        random.shuffle(opts)
        q['shuffled_options'] = opts

    remaining_s = 0
    if attempt.get('deadline_at'):
        remaining_s = max(0, int((attempt['deadline_at'] - datetime.utcnow()).total_seconds()))
    elif attempt['is_sample']:
        remaining_s = attempt['duration_minutes'] * 60

    return render_template('assessment_take.html',
                           attempt=attempt,
                           questions=questions,
                           saved_answers=saved,
                           server_remaining_s=remaining_s,
                           proctoring_cfg=public_config())


@app.route('/candidate-assessment/result/<int:attempt_id>')
@login_required
@login_required_with_session
def assessment_result_page(attempt_id):
    conn = get_db_connection()
    attempt = None
    try:
        with conn.cursor() as c:
            c.execute("""SELECT at.*, a.title FROM assessment_attempts at
                         JOIN assessments a ON at.assessment_id = a.id
                         WHERE at.id=%s AND at.candidate_email=%s""",
                      (attempt_id, current_user.email))
            attempt = c.fetchone()
    finally:
        if conn: conn.close()
    if not attempt:
        flash('Result not found.', 'danger')
        return redirect(url_for('candidate_assessments'))
    return render_template('assessment_result.html', attempt=attempt)


# ============ VIOLATION / WHITEBOARD / SUBMIT / HEARTBEAT / AUTOSAVE ============
@app.route('/api/assessment/<int:attempt_id>/violation', methods=['POST'])
@login_required
def assessment_violation(attempt_id):
    at, err = _load_attempt_for_current_user(attempt_id)
    if err:
        return jsonify({'ok': False, 'error': err[0]}), err[1]
    if at['candidate_email'] != current_user.email:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    if at['status'] != 'In Progress':
        return jsonify({'ok': False, 'error': 'attempt_closed'}), 409
    if _precheck_missing(at):
        return _precheck_error()

    event, validation_error = validate_incident(request.get_json(silent=True))
    if validation_error:
        return jsonify({'ok': False, 'error': validation_error}), 400

    severity, _ = classify(event['event_type'])
    raw_action = policy_action(event['event_type'])
    now = datetime.utcnow()
    conn = get_db_connection()
    if not conn:
        return jsonify({'ok': False, 'error': 'database_unavailable'}), 503

    try:
        conn.begin()
        with conn.cursor() as c:
            # ── 1. Record the Event ──
            c.execute("""SELECT id, event_state, duration_ms FROM proctoring_events
                         WHERE attempt_id=%s AND incident_id=%s LIMIT 1 FOR UPDATE""",
                      (attempt_id, event['incident_id']))
            existing = c.fetchone()
            state = next_phase(existing['event_state'] if existing else None, event['phase'])
            if state is None:
                conn.rollback()
                return jsonify({'ok': False, 'error': 'update_before_start'}), 409

            metadata = json.dumps(event['metadata'], separators=(',', ':'))
            model_name = str(event['metadata'].get('model', ''))[:100] or None

            if existing:
                c.execute("""UPDATE proctoring_events SET event_state=%s,
                                duration_ms=GREATEST(COALESCE(duration_ms,0),%s),
                                confidence=COALESCE(%s,confidence), last_seen_at=%s,
                                ended_at=CASE WHEN %s='ended' THEN COALESCE(ended_at,%s) ELSE ended_at END,
                                metadata_json=%s, model_name=COALESCE(%s,model_name)
                             WHERE id=%s""",
                          (state, event['duration_ms'], event['confidence'], now,
                           state, now, metadata, model_name, existing['id']))
                event_id = existing['id']
            else:
                c.execute("""INSERT INTO proctoring_events
                                (attempt_id, incident_key, incident_id, event_type,
                                 severity, source, description, event_state, duration_ms,
                                 confidence, last_seen_at, ended_at, metadata_json,
                                 model_name, device_info, snapshot_b64)
                             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                          (attempt_id, event['incident_id'], event['incident_id'],
                           event['event_type'], severity, event['source'], event['description'],
                           state, event['duration_ms'], event['confidence'], now,
                           now if state == 'ended' else None, metadata, model_name,
                           (request.headers.get('User-Agent') or '')[:255],
                           event['snapshot_b64'] or None))
                event_id = c.lastrowid

            # ── 2. Handle Escalation (Warn -> Terminate) ──
            action = raw_action
            if raw_action == 'log_only':
                action = 'log'
            elif raw_action == 'warn_then_terminate':
                c.execute("SELECT warning_count FROM assessment_attempts WHERE id=%s FOR UPDATE", (attempt_id,))
                wc_row = c.fetchone() or {}
                wc = int(wc_row.get('warning_count') or 0)

                if wc >= PROCTOR.max_warnings:
                    action = 'terminate'
                else:
                    action = 'warn'
                    c.execute("UPDATE assessment_attempts SET warning_count = warning_count + 1 WHERE id=%s",
                              (attempt_id,))

            # ── 3. Terminate if needed ──
            if action == 'terminate':
                c.execute("""UPDATE assessment_attempts SET status='Terminated', terminated_at=%s,
                             termination_reason=%s, termination_source='server'
                             WHERE id=%s AND status='In Progress'""",
                          (now, event['description'] or event['event_type'], attempt_id))
        conn.commit()

        # Save snapshot after commit
        if event['snapshot_b64']:
            path = save_evidence(attempt_id, event_id, event['snapshot_b64'])
            if path:
                with conn.cursor() as c:
                    c.execute("UPDATE proctoring_events SET evidence_path=%s,snapshot_b64=NULL WHERE id=%s",
                              (path, event_id))

    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        app.logger.exception("Failed to persist proctoring incident attempt=%s type=%s", attempt_id,
                             event['event_type'])
        return jsonify({'ok': False, 'error': 'server_error'}), 500
    finally:
        conn.close()

    return jsonify({'ok': True, 'action': action, 'severity': severity,
                    'incident_id': event['incident_id'], 'state': state})


@app.route('/api/assessment/<int:attempt_id>/whiteboard', methods=['POST'])
@login_required
def assessment_save_whiteboard(attempt_id):
    at, err = _load_attempt_for_current_user(attempt_id)
    if err:
        return jsonify({'ok': False, 'error': err[0]}), err[1]
    if at['candidate_email'] != current_user.email:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    if _precheck_missing(at):
        return _precheck_error()

    data = request.get_json() or {}
    qid = data.get('question_id')
    img = (data.get('image') or '')[:2000000]
    if not qid or not img:
        return jsonify({'ok': False}), 400
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""INSERT INTO assessment_whiteboards (attempt_id, question_id, image_data)
                         VALUES (%s,%s,%s)
                         ON DUPLICATE KEY UPDATE image_data=VALUES(image_data)""",
                      (attempt_id, qid, img))
    finally:
        conn.close()
    return jsonify({'ok': True})


@app.route('/api/assessment/<int:attempt_id>/submit', methods=['POST'])
@login_required
def assessment_submit(attempt_id):
    at, err = _load_attempt_for_current_user(attempt_id)
    if err:
        return jsonify({'ok': False, 'error': err[0]}), err[1]
    if at['candidate_email'] != current_user.email:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    if at['status'] == 'In Progress' and _precheck_missing(at):
        return _precheck_error()

    payload = request.get_json(silent=True)  # <─── ADDED THIS LINE
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'error': 'invalid_payload'}), 400
    submission_id = str(payload.get('submission_id') or '')[:64]
    if not submission_id or not all(ch.isalnum() or ch in '-_.' for ch in submission_id):
        return jsonify({'ok': False, 'error': 'invalid_submission_id'}), 400
    if at['status'] == 'Completed' or (at['status'] == 'Terminated' and at.get('monitoring_finalized_at')):
        try:
            summary = json.loads(at.get('proctoring_summary') or '{}')
        except (TypeError, ValueError):
            summary = {}
        return jsonify({'ok': True, 'duplicate': True, 'status': at['status'],
                        'summary': summary})

    # Deadline/policy finalization still preserves and scores answers.
    answers = payload.get('answers', {})
    monitoring = payload.get('monitoring', {})
    if not isinstance(answers, dict) or len(answers) > 500:
        return jsonify({'ok': False, 'error': 'invalid_answers'}), 400
    if not isinstance(monitoring, dict):
        return jsonify({'ok': False, 'error': 'invalid_monitoring_status'}), 400
    monitoring_json = json.dumps(monitoring, separators=(',', ':'))
    if len(monitoring_json) > 8000:
        return jsonify({'ok': False, 'error': 'monitoring_status_too_large'}), 413

    conn = get_db_connection()
    if not conn:
        return jsonify({'ok': False, 'error': 'database_unavailable'}), 503
    status = 'Completed'
    try:
        conn.begin()
        with conn.cursor() as c:
            c.execute("""SELECT at.*, a.id AS assessment_id FROM assessment_attempts at
                         JOIN assessments a ON at.assessment_id = a.id
                         WHERE at.id=%s FOR UPDATE""", (attempt_id,))
            locked = c.fetchone()
            if not locked:
                conn.rollback()
                return jsonify({'ok': False, 'error': 'no_attempt'}), 404
            if locked['status'] not in ('In Progress', 'Terminated') or (
                    locked['status'] == 'Terminated' and locked.get('monitoring_finalized_at')):
                conn.commit()
                try:
                    stored_summary = json.loads(locked.get('proctoring_summary') or '{}')
                except (TypeError, ValueError):
                    stored_summary = {}
                return jsonify({'ok': True, 'duplicate': True, 'status': locked['status'],
                                'summary': stored_summary})

            c.execute("""SELECT id, correct_option, question_type
                         FROM assessment_questions WHERE assessment_id=%s""",
                      (locked['assessment_id'],))
            qmap = {q['id']: q for q in c.fetchall()}
            c.execute("SELECT question_id, selected_option FROM attempt_autosaves WHERE attempt_id=%s", (attempt_id,))
            merged = {str(row['question_id']): row['selected_option'] for row in c.fetchall()}
            merged.update(answers)
            answers = merged
            c.execute("DELETE FROM assessment_answers WHERE attempt_id=%s", (attempt_id,))

            mcq_score = mcq_max = answered = 0
            for qid_s, sel in answers.items():
                try:
                    qid = int(qid_s)
                except (TypeError, ValueError):
                    continue
                q = qmap.get(qid)
                if not q or not isinstance(sel, str) or not sel.strip() or len(sel) > 10000:
                    continue
                if q['question_type'] in ('MCQ_TEXT', 'MCQ_CODE') and sel not in ('A', 'B', 'C', 'D'):
                    continue
                answered += 1
                if q['question_type'] in ('MCQ_TEXT', 'MCQ_CODE'):
                    mcq_max += 1
                    ok = int(q['correct_option'] == sel)
                    mcq_score += ok
                    c.execute("""INSERT INTO assessment_answers
                                    (attempt_id, question_id, selected_option, is_correct)
                                 VALUES (%s,%s,%s,%s)""",
                              (attempt_id, qid, sel, ok))
                else:
                    c.execute("""INSERT INTO assessment_answers
                                    (attempt_id, question_id, selected_option, is_correct)
                                 VALUES (%s,%s,%s,%s)""",
                              (attempt_id, qid, sel, None))

            rating = round((mcq_score / mcq_max) * 4, 1) if mcq_max else 0.0

            status = 'Terminated' if locked['status'] == 'Terminated' else 'Completed'

            c.execute("""SELECT event_type, source, event_state, duration_ms
                         FROM proctoring_events WHERE attempt_id=%s""", (attempt_id,))
            summary = build_summary(c.fetchall())
            summary_json = json.dumps(summary, separators=(',', ':'))
            c.execute("""UPDATE assessment_attempts
                         SET submitted_at=NOW(), monitoring_finalized_at=NOW(),
                             status=%s, score=%s, max_score=%s, answered_count=%s, rating=%s,
                             proctoring_summary=%s, monitoring_status=%s, last_submission_id=%s
                         WHERE id=%s""",
                      (status, mcq_score, mcq_max, answered, rating,
                       summary_json, monitoring_json, submission_id, attempt_id))

            if status in ('Completed', 'Terminated') and locked['requisition_id'] is not None:
                c.execute("""UPDATE candidates
                             SET selection_status = 'In Process'
                             WHERE email=%s AND requisition_id=%s
                               AND selection_status = 'Applied'""",
                          (locked['candidate_email'], locked['requisition_id']))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        app.logger.exception("Assessment finalization failed attempt=%s", attempt_id)
        return jsonify({'ok': False, 'error': 'server_error'}), 500
    finally:
        conn.close()

    get_job_manager().cancel_attempt(attempt_id)
    return jsonify({'ok': True, 'status': status, 'summary': summary})


@app.route('/api/assessment/<int:attempt_id>/precheck', methods=['POST'])
@login_required
def assessment_precheck(attempt_id):
    at, err = _load_attempt_for_current_user(attempt_id)
    if err:
        return jsonify({'ok': False, 'error': err[0]}), err[1]
    if at['candidate_email'] != current_user.email:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403

    if at['status'] != 'In Progress':
        return jsonify({'ok': False, 'error': 'attempt_closed'}), 409
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'reason': 'invalid_payload'}), 400
    report = data.get('report')
    if not isinstance(report, dict):
        return jsonify({'ok': False, 'reason': 'invalid_report'}), 400
    report = dict(report)

    snapshot = data.get('identity_snapshot') or ''

    conn = get_db_connection()
    if not conn:
        return jsonify({'ok': False, 'reason': 'database_unavailable'}), 503
    try:
        with conn.cursor() as c:
            c.execute("SELECT is_sample FROM assessments WHERE id=%s", (at['assessment_id'],))
            assessment = c.fetchone() or {}
            is_sample = bool(assessment.get('is_sample'))
            server_capabilities = get_detector().capabilities()
            report['server_capabilities'] = server_capabilities

            server_reason = None
            if not is_sample:
                if not server_capabilities.get('available'):
                    server_reason = 'detector_unavailable'

            if server_reason:
                report['objects'] = {
                    'status': 'fail',
                    'message': f'Server precheck failed: {server_reason}.',
                }

            serialized = json.dumps(report, separators=(',', ':'))
            if len(serialized) > 60000:
                return jsonify({'ok': False, 'reason': 'report_too_large'}), 413

            ok, reason = validate_precheck(report, is_sample=is_sample)
            capability_hash = hashlib.sha256(json.dumps(
                server_capabilities, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

            c.execute("""UPDATE assessment_attempts
                         SET precheck_report=%s, device_info=%s, identity_snapshot=%s,
                             precheck_passed_at=CASE WHEN %s THEN NOW() ELSE NULL END,
                             precheck_capability_hash=CASE WHEN %s THEN %s ELSE NULL END
                         WHERE id=%s""",
                      (serialized, str(data.get('device_info') or '')[:500],
                       snapshot or None, ok, ok, capability_hash, attempt_id))
    finally:
        conn.close()

    return jsonify({'ok': ok, 'reason': reason,
                    'capabilities': server_capabilities}), 200 if ok else 400


@app.route('/api/assessment/<int:attempt_id>/identity/reference')
@login_required
def assessment_identity_reference(attempt_id):
    at, err = _load_attempt_for_current_user(attempt_id)
    if err:
        return jsonify({'ok': False, 'error': err[0]}), err[1]
    if at['candidate_email'] != current_user.email:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    user = CandidateUser.query.filter_by(email=current_user.email).first()
    if not user or not user.passport_photo:
        return jsonify({'ok': False, 'error': 'reference_not_available'}), 404
    return send_file(io.BytesIO(user.passport_photo),
                     mimetype=get_mime_type(user.passport_photo_filename or 'passport.jpg'),
                     download_name='identity-reference', as_attachment=False)


@app.route('/api/assessment/<int:attempt_id>/heartbeat', methods=['POST'])
@login_required
def assessment_heartbeat(attempt_id):
    at, err = _load_attempt_for_current_user(attempt_id)
    if err:
        return jsonify({'ok': False, 'error': err[0]}), err[1]
    if at['candidate_email'] != current_user.email:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    if _precheck_missing(at):
        return _precheck_error()

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'invalid_payload'}), 400
    try:
        seq = min(2_147_483_647, max(0, int(data.get('seq') or 0)))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'invalid_sequence'}), 400
    now = datetime.utcnow()
    conn = get_db_connection()
    if not conn:
        return jsonify({'ok': False, 'error': 'database_unavailable'}), 503
    try:
        with conn.cursor() as c:
            c.execute("""INSERT INTO attempt_heartbeats
                            (attempt_id, beat_at, client_seq, ip, user_agent)
                         VALUES (%s,%s,%s,%s,%s)""",
                      (attempt_id, now, seq, request.remote_addr,
                       (request.headers.get('User-Agent') or '')[:255]))
            c.execute("""UPDATE assessment_attempts
                         SET last_heartbeat_at=%s WHERE id=%s""",
                      (now, attempt_id))
            deadline_reached = bool(
                at['status'] == 'In Progress' and is_attempt_expired(at['deadline_at'])
            )
    finally:
        conn.close()
    return jsonify({
        'ok': True,
        'server_time': now.isoformat(),
        'interval_ms': PROCTOR.heartbeat_interval_ms,
        'remaining_seconds': max(0, int((at['deadline_at'] - now).total_seconds())) if at.get('deadline_at') else None,
        'deadline_reached': deadline_reached,
        'monitoring_degraded': not get_detector().capabilities().get('available', False),
    })


@app.route('/api/assessment/<int:attempt_id>/autosave', methods=['POST'])
@login_required
def assessment_autosave(attempt_id):
    at, err = _load_attempt_for_current_user(attempt_id)
    if err:
        return jsonify({'ok': False, 'error': err[0]}), err[1]
    if at['candidate_email'] != current_user.email:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    if _precheck_missing(at):
        return _precheck_error()
    if at['status'] != 'In Progress':
        return jsonify({'ok': False, 'error': 'not_in_progress'}), 409

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get('answers'), dict) or len(data['answers']) > 500:
        return jsonify({'ok': False, 'error': 'invalid_answers'}), 400
    answers = data['answers']
    saved_count = 0
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT id, question_type FROM assessment_questions WHERE assessment_id=%s",
                      (at['assessment_id'],))
            valid_questions = {row['id']: row['question_type'] for row in c.fetchall()}
            for qid_s, sel in answers.items():
                try:
                    qid = int(qid_s)
                except (TypeError, ValueError):
                    continue
                if qid not in valid_questions or not isinstance(sel, str) or len(sel) > 10000:
                    continue
                if valid_questions[qid] in ('MCQ_TEXT', 'MCQ_CODE') and sel not in ('A', 'B', 'C', 'D', ''):
                    continue
                c.execute("""INSERT INTO attempt_autosaves
                                (attempt_id, question_id, selected_option)
                             VALUES (%s,%s,%s)
                             ON DUPLICATE KEY UPDATE
                                selected_option = VALUES(selected_option),
                                updated_at = NOW()""",
                          (attempt_id, qid, sel))
                saved_count += 1
    finally:
        conn.close()
    return jsonify({'ok': True, 'saved': saved_count})


@app.route('/api/assessment/<int:attempt_id>/autosave', methods=['GET'])
@login_required
def assessment_autosave_load(attempt_id):
    at, err = _load_attempt_for_current_user(attempt_id)
    if err:
        return jsonify({'ok': False, 'error': err[0]}), err[1]
    if at['candidate_email'] != current_user.email:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    if _precheck_missing(at):
        return _precheck_error()

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""SELECT question_id, selected_option
                         FROM attempt_autosaves WHERE attempt_id=%s""",
                      (attempt_id,))
            rows = {str(r['question_id']): r['selected_option']
                    for r in c.fetchall()}
    finally:
        conn.close()
    return jsonify({'ok': True, 'answers': rows})


@app.route('/api/proctoring/config')
@login_required
def proctoring_config_api():
    return jsonify(public_config())


@app.route('/api/proctoring/capabilities')
@login_required
def proctoring_capabilities():
    info = audio_detector_info() or {}
    class_names = info.get('class_names') or []

    if not class_names:
        try:
            m = load_headphone_model()
            if m is not None and hasattr(m, 'names'):
                names = m.names
                class_names = list(names.values()) if isinstance(names, dict) else list(names)
        except Exception as e:
            print(f"[capabilities] {e}")

    info['class_names'] = class_names
    info['has_audio_class'] = any(_is_forbidden_class(n) for n in class_names)

    return jsonify({
        "audio_device_detector": info,
        "config": public_config(),
    })


@app.route('/api/assessment/<int:attempt_id>/detect-audio-device', methods=['POST'])
@login_required
def assessment_detect_audio_device(attempt_id):
    try:
        at, err = _load_attempt_for_current_user(attempt_id)
        if err:
            return jsonify({'ok': True, 'action': 'ignore', 'detections': []}), 200

        data = request.get_json(silent=True) or {}
        frame_b64 = data.get('frame') or ''
        if not frame_b64:
            return jsonify({'ok': True, 'action': 'ignore', 'detections': []}), 200

        conf = data.get('confidence')
        try:
            raw_detections = detect_audio_device(frame_b64, conf=conf) or []
        except Exception as e:
            print(f"❌ [SERVER] Model error: {e}")
            return jsonify({'ok': True, 'action': 'ignore', 'detections': []}), 200

        fw, fh = _decode_frame_size(frame_b64)
        if fw and fh:
            detections = filter_audio_detections(raw_detections, fw, fh)
        else:
            detections = raw_detections

        if data.get('precheck'):
            return jsonify({
                'ok': True,
                'action': 'precheck',
                'frame_size': [fw, fh],
                'raw_count': len(raw_detections),
                'raw_detections': raw_detections,
                'detections': detections,
            }), 200

        # LIVE LOGIC
        now = time.time()
        st = _audio_device_state.get(attempt_id) or {'since': None, 'last_class': None}
        sustain_ms = PROCTOR.phone_sustain_ms

        if detections:
            best = max(detections, key=lambda d: float(d.get('confidence') or 0))
            if st['since'] is None or st['last_class'] != best['class']:
                st['since'] = now
                st['last_class'] = best['class']

            _audio_device_state[attempt_id] = st
            held_ms = int((now - st['since']) * 1000)

            if held_ms >= sustain_ms:
                # CHANGED: Only return the violation. Do NOT write to DB here.
                # Let the client call /violation to handle warnings & termination.
                _audio_device_state[attempt_id] = {'since': None, 'last_class': None}
                return jsonify(
                    {'ok': True, 'action': 'violation', 'class': best['class'], 'confidence': best['confidence'],
                     'detections': detections}), 200

            return jsonify({'ok': True, 'action': 'monitor', 'class': best['class'], 'confidence': best['confidence'],
                            'detections': detections}), 200

        _audio_device_state[attempt_id] = {'since': None, 'last_class': None}
        return jsonify({'ok': True, 'action': 'clear', 'detections': []}), 200

    except Exception as e:
        print(f"❌ [SERVER] Fatal route error: {e}")
        return jsonify({'ok': True, 'action': 'ignore', 'detections': []}), 200


@app.route('/api/assessment/<int:attempt_id>/detect-audio-batch', methods=['POST'])
@login_required
def assessment_detect_audio_batch(attempt_id):
    at, err = _load_attempt_for_current_user(attempt_id)
    if err:
        return jsonify({'ok': False, 'error': err[0]}), err[1]
    if at['candidate_email'] != current_user.email:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    if at['status'] != 'In Progress':
        return jsonify({'ok': True, 'results': []})

    if not audio_detector_available():
        return jsonify({'ok': False, 'error': 'detector_unavailable'}), 503

    data = request.get_json(silent=True) or {}
    frames = data.get('frames') or []
    if not isinstance(frames, list) or not frames:
        return jsonify({'ok': False, 'error': 'no_frames'}), 400
    if len(frames) > 20:
        frames = frames[:20]  # sanity cap

    frame_urls = [f.get('frame') if isinstance(f, dict) else f for f in frames]
    frame_urls = [u for u in frame_urls if u]
    if not frame_urls:
        return jsonify({'ok': False, 'error': 'empty_frames'}), 400

    use_tiling = bool(data.get('tiling', False))

    try:
        batch_results = detect_audio_batch(
            frame_urls, conf=0.04, imgsz=640, use_tiling=use_tiling
        ) or []
    except Exception as e:
        print(f"[audio-batch] model error: {e}")
        return jsonify({'ok': False, 'error': 'model_error'}), 500

    results = []
    class_summary = {}

    for meta, dets in zip(frames, batch_results):
        if isinstance(meta, dict):
            side = meta.get('side', 'unknown')
            fnum = meta.get('frameNumber', len(results) + 1)
        else:
            side, fnum = 'unknown', len(results) + 1

        # Frame size
        fw, fh = _decode_frame_size(meta.get('frame') if isinstance(meta, dict) else meta)
        filtered = filter_audio_detections(dets, fw, fh) if (fw and fh) else dets

        for d in filtered:
            cls = d.get('class') or 'unknown'
            c = float(d.get('confidence') or 0)
            bucket = class_summary.setdefault(cls, {
                'count': 0, 'max_conf': 0.0,
                'sides': {}, 'frames': []
            })
            bucket['count'] += 1
            bucket['max_conf'] = max(bucket['max_conf'], c)
            bucket['sides'][side] = bucket['sides'].get(side, 0) + 1
            bucket['frames'].append(f'{side}#{fnum}')

        results.append({
            'side': side,
            'frameNumber': fnum,
            'frame_size': [fw, fh],
            'raw_count': len(dets),
            'detections': filtered,
        })

    print(f"[audio-batch] attempt={attempt_id} sent={len(frames)} "
          f"classes={ {k: v['count'] for k, v in class_summary.items()} }")

    return jsonify({
        'ok': True,
        'results': results,
        'summary': {
            'total_scanned': len(results),
            'frames_with_hits': sum(1 for r in results if r['detections']),
            'classes': class_summary,
        },
    })


def vote_on_detections(class_summary: dict, total_frames: int) -> dict:
    if not class_summary:
        return {'verdict': 'pass', 'reason': 'No devices detected.', 'classes': []}

    fails, warns = [], []

    for cls, s in class_summary.items():
        count = s.get('count', 0)
        max_conf = float(s.get('max_conf', 0))
        sides = s.get('sides', {})
        max_same_side = max(sides.values()) if sides else 0

        if max_conf >= 0.50:
            fails.append(f"{cls} (single hit {int(max_conf * 100)}%)")
            continue
        if count >= 3 and max_conf >= 0.20:
            fails.append(f"{cls} ({count} hits, max {int(max_conf * 100)}%)")
            continue
        if max_same_side >= 2 and max_conf >= 0.25:
            fails.append(f"{cls} (side-consistent, {count} hits, max {int(max_conf * 100)}%)")
            continue
        if count >= 1 and max_conf >= 0.20:
            warns.append(f"{cls} (weak: {count} hit(s), max {int(max_conf * 100)}%)")

    if fails:
        return {'verdict': 'fail',
                'reason': 'Detected: ' + '; '.join(fails),
                'classes': fails}
    if warns:
        return {'verdict': 'warn',
                'reason': 'Possible: ' + '; '.join(warns) + ' — re-scan',
                'classes': warns}
    return {'verdict': 'pass',
            'reason': f'No audio device in {total_frames} frames.',
            'classes': []}


@app.route('/api/assessment/<int:attempt_id>/record-chunk', methods=['POST'])
@login_required
def assessment_record_chunk(attempt_id):
    at, err = _load_attempt_for_current_user(attempt_id)
    if err:
        return jsonify({'ok': False, 'error': err[0]}), err[1]
    if at['candidate_email'] != current_user.email:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403

    chunk = request.files.get('chunk')
    idx = request.form.get('index', '0')
    if not chunk:
        return jsonify({'ok': False, 'error': 'no chunk'}), 400

    d = os.path.join(RECORDINGS_DIR, f'attempt_{attempt_id}')
    os.makedirs(d, exist_ok=True)
    try:
        path = os.path.join(d, f'chunk_{int(idx):05d}.webm')
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'bad index'}), 400
    chunk.save(path)
    return jsonify({'ok': True, 'size': os.path.getsize(path)})


@app.route('/api/assessment/<int:attempt_id>/record-finish', methods=['POST'])
@login_required
def assessment_record_finish(attempt_id):
    at, err = _load_attempt_for_current_user(attempt_id)
    if err:
        return jsonify({'ok': False, 'error': err[0]}), err[1]
    if at['candidate_email'] != current_user.email:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403

    d = os.path.join(RECORDINGS_DIR, f'attempt_{attempt_id}')
    if not os.path.isdir(d):
        return jsonify({'ok': False, 'error': 'no chunks'}), 404

    chunks = sorted([f for f in os.listdir(d)
                     if f.startswith('chunk_') and f.endswith('.webm')])
    if not chunks:
        return jsonify({'ok': False, 'error': 'empty'}), 404

    final_name = f'attempt_{attempt_id}_recording.webm'
    final_path = os.path.join(RECORDINGS_DIR, final_name)
    with open(final_path, 'wb') as out:
        for f in chunks:
            with open(os.path.join(d, f), 'rb') as inp:
                out.write(inp.read())

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("UPDATE assessment_attempts SET video_path=%s WHERE id=%s",
                      (final_name, attempt_id))
    finally:
        conn.close()

    return jsonify({'ok': True, 'file': final_name,
                    'size': os.path.getsize(final_path)})


@app.route('/admin/assessment/<int:attempt_id>/video')
@login_required
@role_required(HR_ROLES)
def admin_attempt_video(attempt_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT video_path FROM assessment_attempts WHERE id=%s", (attempt_id,))
            row = c.fetchone()
    finally:
        conn.close()

    if not row or not row['video_path']:
        flash('No video recorded for this attempt.', 'warning')
        return redirect(url_for('hr_assessments'))

    full_path = os.path.join(RECORDINGS_DIR, row['video_path'])
    if not os.path.isfile(full_path):
        flash('Video file missing.', 'warning')
        return redirect(url_for('hr_assessments'))

    return send_file(full_path, mimetype='video/webm', as_attachment=False)


# =========================================================
# HR / ADMIN
# =========================================================
@app.route('/hr/assessments')
@login_required
@role_required(HR_ROLES)
def hr_assessments():
    conn = get_db_connection()
    assessments, requisitions, attempts = [], [], []
    try:
        with conn.cursor() as c:
            c.execute("""SELECT a.*, r.job_title AS requisition_title,
                                (SELECT COUNT(*) FROM assessment_questions q WHERE q.assessment_id=a.id) AS question_count,
                                (SELECT COUNT(*) FROM assessment_attempts at WHERE at.assessment_id=a.id) AS attempt_count
                         FROM assessments a
                         LEFT JOIN requisitions r ON a.requisition_id = r.id
                         ORDER BY a.created_at DESC""")
            assessments = c.fetchall()
            c.execute("SELECT id, job_title, department FROM requisitions WHERE status='Open' ORDER BY created_at DESC")
            requisitions = c.fetchall()
            c.execute("""SELECT at.*, a.title AS assessment_title,
                                r.job_title AS requisition_title,
                                c.name AS candidate_name
                         FROM assessment_attempts at
                         JOIN assessments a ON at.assessment_id = a.id
                         LEFT JOIN requisitions r ON at.requisition_id = r.id
                         LEFT JOIN candidates c ON at.candidate_id = c.id
                         ORDER BY at.created_at DESC LIMIT 200""")
            attempts = c.fetchall()
    finally:
        if conn: conn.close()
    return render_template('hr_assessments.html', assessments=assessments,
                           requisitions=requisitions, attempts=attempts)


@app.route('/api/hr/assessment/<int:assessment_id>/questions')
@login_required
@role_required(HR_ROLES)
def api_hr_questions(assessment_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT * FROM assessments WHERE id=%s", (assessment_id,))
            a = c.fetchone()
            if not a:
                return jsonify({'ok': False, 'error': 'Assessment not found'}), 404

            try:
                c.execute("SELECT * FROM assessment_questions WHERE assessment_id=%s ORDER BY id ASC",
                          (assessment_id,))
                qs = c.fetchall()
            except Exception:
                qs = []

            for q in qs:
                q['created_at'] = q['created_at'].isoformat() if q['created_at'] else None
                if 'expected_answer' not in q:
                    q['expected_answer'] = None

            a_dict = dict(a)
            a_dict['created_at'] = a_dict['created_at'].isoformat() if a_dict.get('created_at') else None

            return jsonify({'ok': True, 'assessment': a_dict, 'questions': qs})
    except Exception as ex:
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(ex)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/hr/attempt/<int:attempt_id>')
@login_required
@role_required(HR_ROLES)
def api_hr_attempt(attempt_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""SELECT at.*, a.title AS assessment_title,
                                r.job_title AS requisition_title,
                                c.name AS candidate_name
                         FROM assessment_attempts at
                         JOIN assessments a ON at.assessment_id = a.id
                         LEFT JOIN requisitions r ON at.requisition_id = r.id
                         LEFT JOIN candidates c ON at.candidate_id = c.id
                         WHERE at.id=%s""", (attempt_id,))
            at = c.fetchone()
            if not at:
                return jsonify({'ok': False, 'error': 'Attempt not found'}), 404

            try:
                c.execute("""SELECT aa.*, aq.question_text, aq.correct_option,
                                    aq.option_a, aq.option_b, aq.option_c, aq.option_d,
                                    aq.question_type, aq.code_snippet, aq.expected_answer
                             FROM assessment_answers aa
                             JOIN assessment_questions aq ON aa.question_id = aq.id
                             WHERE aa.attempt_id=%s ORDER BY aa.id ASC""", (attempt_id,))
                answers = c.fetchall()
            except Exception:
                c.execute("""SELECT aa.*, aq.question_text, aq.correct_option,
                                    aq.option_a, aq.option_b, aq.option_c, aq.option_d,
                                    aq.question_type, aq.code_snippet
                             FROM assessment_answers aa
                             JOIN assessment_questions aq ON aa.question_id = aq.id
                             WHERE aa.attempt_id=%s ORDER BY aa.id ASC""", (attempt_id,))
                answers = c.fetchall()
                for a in answers:
                    a['expected_answer'] = None

            c.execute("SELECT * FROM proctoring_events WHERE attempt_id=%s ORDER BY occurred_at ASC", (attempt_id,))
            events = c.fetchall()
            c.execute("SELECT * FROM assessment_whiteboards WHERE attempt_id=%s ORDER BY question_id ASC",
                      (attempt_id,))
            wbs = c.fetchall()

            for e in events:
                e['occurred_at'] = e['occurred_at'].isoformat() if e['occurred_at'] else None

            return jsonify({
                'ok': True,
                'attempt': {**at,
                            'started_at': at['started_at'].isoformat() if at['started_at'] else None,
                            'submitted_at': at['submitted_at'].isoformat() if at['submitted_at'] else None,
                            'terminated_at': at['terminated_at'].isoformat() if at['terminated_at'] else None,
                            'rating': float(at['rating'] or 0)},
                'answers': answers, 'events': events, 'whiteboards': wbs
            })
    except Exception as ex:
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(ex)}), 500
    finally:
        if conn: conn.close()


@app.route('/hr/attempt/<int:attempt_id>/review')
@login_required
@role_required(HR_ROLES)
def hr_attempt_review(attempt_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""SELECT at.*, a.title AS assessment_title,
                                r.job_title AS requisition_title,
                                c.name AS candidate_name
                         FROM assessment_attempts at
                         JOIN assessments a ON at.assessment_id = a.id
                         LEFT JOIN requisitions r ON at.requisition_id = r.id
                         LEFT JOIN candidates c ON at.candidate_id = c.id
                         WHERE at.id=%s""", (attempt_id,))
            at = c.fetchone()
            if not at:
                flash('Attempt not found.', 'danger')
                return redirect(url_for('hr_assessments'))

            c.execute("""SELECT * FROM proctoring_events
                         WHERE attempt_id=%s
                         ORDER BY occurred_at ASC""", (attempt_id,))
            events = c.fetchall()

            c.execute("""SELECT * FROM attempt_heartbeats
                         WHERE attempt_id=%s ORDER BY beat_at ASC""", (attempt_id,))
            beats = c.fetchall()
    finally:
        conn.close()
    return render_template('hr_attempt_review.html',
                           attempt=at, events=events, beats=beats)


@app.route('/hr/event/<int:event_id>/review', methods=['POST'])
@login_required
@role_required(HR_ROLES)
def hr_event_review(event_id):
    decision = (request.form.get('decision') or '').strip()
    notes = (request.form.get('notes') or '')[:2000]
    if decision not in ('confirmed', 'false_positive', 'ignored', 'pending'):
        flash('Invalid decision.', 'danger')
        return redirect(request.referrer or url_for('hr_assessments'))
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""UPDATE proctoring_events
                         SET review_status=%s, reviewed_by=%s,
                             reviewed_at=NOW(), review_notes=%s
                         WHERE id=%s""",
                      (decision, current_user.email, notes, event_id))
    finally:
        conn.close()
    flash('Event updated.', 'success')
    return redirect(request.referrer or url_for('hr_assessments'))


@app.route('/hr/attempt/<int:attempt_id>/audit.csv')
@login_required
@role_required(HR_ROLES)
def hr_attempt_audit_csv(attempt_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""SELECT occurred_at, event_type, severity, source,
                                duration_ms, confidence, review_status,
                                description, review_notes
                         FROM proctoring_events
                         WHERE attempt_id=%s ORDER BY occurred_at ASC""",
                      (attempt_id,))
            rows = c.fetchall()
    finally:
        conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["occurred_at", "event_type", "severity", "source",
                "duration_ms", "confidence", "review_status",
                "description", "review_notes"])
    for r in rows:
        w.writerow([r['occurred_at'], r['event_type'], r['severity'],
                    r['source'], r['duration_ms'], r['confidence'],
                    r['review_status'], r['description'], r['review_notes']])
    return send_file(io.BytesIO(buf.getvalue().encode('utf-8')),
                     mimetype='text/csv', as_attachment=True,
                     download_name=f'attempt_{attempt_id}_audit.csv')


@app.route('/hr/evidence/<int:event_id>')
@login_required
@role_required(HR_ROLES)
def hr_event_evidence(event_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT evidence_path, snapshot_b64 FROM proctoring_events WHERE id=%s",
                      (event_id,))
            row = c.fetchone()
    finally:
        conn.close()
    if not row:
        flash('Evidence not found.', 'warning')
        return redirect(url_for('hr_assessments'))
    if row['evidence_path']:
        full = EVIDENCE_DIR / row['evidence_path']
        if full.is_file():
            return send_file(str(full), mimetype='image/jpeg')
    if row['snapshot_b64']:
        data = row['snapshot_b64'].split(',', 1)[-1]
        return send_file(io.BytesIO(base64.b64decode(data)), mimetype='image/jpeg')
    flash('No evidence stored.', 'warning')
    return redirect(url_for('hr_assessments'))


@app.route('/hr/assessment/create', methods=['POST'])
@login_required
@role_required(HR_ROLES)
def hr_assessment_create():
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    total_q = int(request.form.get('total_questions') or 10)
    dur = int(request.form.get('duration_minutes') or 30)
    pr = float(request.form.get('pass_rating') or 2.0)
    is_sample = 1 if request.form.get('is_sample') == '1' else 0
    if not title:
        flash('Title required.', 'danger')
        return redirect(url_for('hr_assessments'))
    token = secrets.token_urlsafe(12)[:16]
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""INSERT INTO assessments
                            (title, description, requisition_id, total_questions,
                             duration_minutes, pass_rating, is_sample, is_active,
                             share_token, created_by, created_at)
                         VALUES (%s,%s,NULL,%s,%s,%s,%s,1,%s,%s,NOW())""",
                      (title, description, total_q, dur, pr, is_sample, token, current_user.email))
        flash('Assessment created.', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    finally:
        if conn: conn.close()
    return redirect(url_for('hr_assessments'))


@app.route('/hr/assessment/<int:assessment_id>/edit', methods=['POST'])
@login_required
@role_required(HR_ROLES)
def hr_assessment_edit(assessment_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""UPDATE assessments
                         SET title=%s, description=%s,
                             total_questions=%s, duration_minutes=%s,
                             pass_rating=%s, is_sample=%s, is_active=%s
                         WHERE id=%s""",
                      (request.form.get('title', '').strip(),
                       request.form.get('description', '').strip(),
                       int(request.form.get('total_questions') or 10),
                       int(request.form.get('duration_minutes') or 30),
                       float(request.form.get('pass_rating') or 2.0),
                       1 if request.form.get('is_sample') == '1' else 0,
                       1 if request.form.get('is_active') == '1' else 0,
                       assessment_id))
        flash('Assessment updated.', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    finally:
        if conn: conn.close()
    return redirect(url_for('hr_assessments'))


@app.route('/hr/assessment/<int:assessment_id>/delete', methods=['POST'])
@login_required
@role_required(HR_ROLES)
def hr_assessment_delete(assessment_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM assessment_questions WHERE assessment_id=%s", (assessment_id,))
            c.execute("DELETE FROM assessments WHERE id=%s", (assessment_id,))
        flash('Assessment deleted.', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    finally:
        if conn: conn.close()
    return redirect(url_for('hr_assessments'))


@app.route('/hr/assessment/<int:assessment_id>/question/add', methods=['POST'])
@login_required
@role_required(HR_ROLES)
def hr_question_add(assessment_id):
    q_text = request.form.get('question_text', '').strip()
    q_type = request.form.get('question_type', 'MCQ_TEXT')
    code = request.form.get('code_snippet', '').strip()
    correct = (request.form.get('correct_option') or '').upper()
    exp_ans = request.form.get('expected_answer', '').strip()
    diff = request.form.get('difficulty', 'Medium')

    if not q_text:
        flash('Question text is required.', 'danger')
        return redirect(url_for('hr_assessments'))

    if q_type in ('MCQ_TEXT', 'MCQ_CODE'):
        oa = request.form.get('option_a', '').strip()
        ob = request.form.get('option_b', '').strip()
        oc = request.form.get('option_c', '').strip()
        od = request.form.get('option_d', '').strip()
        if not all([oa, ob, oc, od]) or not correct:
            flash('MCQ needs all 4 options and a correct answer.', 'danger')
            return redirect(url_for('hr_assessments'))
    else:
        oa = ob = oc = od = None
        correct = None

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""INSERT INTO assessment_questions
                            (assessment_id, question_text, question_type, code_snippet,
                             option_a, option_b, option_c, option_d,
                             correct_option, expected_answer, difficulty, created_at)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
                      (assessment_id, q_text, q_type, code,
                       oa, ob, oc, od, correct, exp_ans, diff))
        flash('Question added.', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    finally:
        if conn: conn.close()
    return redirect(url_for('hr_assessments'))


@app.route('/hr/question/<int:qid>/edit', methods=['POST'])
@login_required
@role_required(HR_ROLES)
def hr_question_edit(qid):
    q_type = request.form.get('question_type', 'MCQ_TEXT')
    correct = (request.form.get('correct_option') or '').upper()

    if q_type in ('MCQ_TEXT', 'MCQ_CODE'):
        oa = request.form.get('option_a', '').strip()
        ob = request.form.get('option_b', '').strip()
        oc = request.form.get('option_c', '').strip()
        od = request.form.get('option_d', '').strip()
    else:
        oa = ob = oc = od = None
        correct = None

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""UPDATE assessment_questions
                         SET question_text=%s, question_type=%s, code_snippet=%s,
                             option_a=%s, option_b=%s, option_c=%s, option_d=%s,
                             correct_option=%s, expected_answer=%s, difficulty=%s
                         WHERE id=%s""",
                      (request.form.get('question_text', '').strip(),
                       q_type,
                       request.form.get('code_snippet', '').strip(),
                       oa, ob, oc, od, correct,
                       request.form.get('expected_answer', '').strip(),
                       request.form.get('difficulty', 'Medium'), qid))
        flash('Question updated.', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    finally:
        if conn: conn.close()
    return redirect(url_for('hr_assessments'))


@app.route('/hr/question/<int:qid>/delete', methods=['POST'])
@login_required
@role_required(HR_ROLES)
def hr_question_delete(qid):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM assessment_questions WHERE id=%s", (qid,))
        flash('Question deleted.', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    finally:
        if conn: conn.close()
    return redirect(url_for('hr_assessments'))


@app.route('/hr/attempt/<int:attempt_id>/allow-retest', methods=['POST'])
@login_required
@role_required(HR_ROLES)
def admin_allow_retest(attempt_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("UPDATE assessment_attempts SET retest_allowed=1, reviewed_by_admin=1 WHERE id=%s", (attempt_id,))

            c.execute("SELECT candidate_email, requisition_id FROM assessment_attempts WHERE id=%s",
                      (attempt_id,))
            meta = c.fetchone()
            if meta and meta['requisition_id'] is not None:
                c.execute("""UPDATE candidates
                             SET selection_status = 'Applied'
                             WHERE email = %s
                               AND requisition_id = %s
                               AND selection_status IN ('In Process', 'Rejected')""",
                          (meta['candidate_email'], meta['requisition_id']))
        flash('Retest enabled. Candidate can take the test again.', 'success')
    finally:
        if conn: conn.close()
    return redirect(url_for('hr_assessments'))


# ============ SESSION APIs ============
@app.route('/api/check-session')
def check_session():
    if not current_user.is_authenticated:
        return jsonify({"valid": False, "reason": "not_logged_in"}), 200
    last = session.get('last_activity')
    if last:
        try:
            idle = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds()
            if idle > SESSION_TIMEOUT_MINUTES * 60:
                session.clear();
                logout_user()
                return jsonify({"valid": False, "reason": "session_expired"}), 200
        except (ValueError, TypeError):
            pass
    return jsonify({"valid": True, "user": current_user.name,
                    "email": current_user.email, "roles": session.get('roles', []),
                    "auth_type": session.get('auth_type', 'local'),
                    "timeout_seconds": SESSION_TIMEOUT_MINUTES * 60}), 200


@app.route('/api/session-timeout')
def session_timeout_api():
    return jsonify({"timeout_seconds": app.config['PERMANENT_SESSION_LIFETIME'].total_seconds()}), 200


# ============ ERRORS ============
@app.route('/favicon.ico')
def favicon():
    return '', 204


@app.errorhandler(404)
def not_found(e):
    flash('Page not found.', 'warning')
    return redirect(url_for('candidate_login'))


@app.errorhandler(500)
def server_error(e):
    flash('Server error.', 'danger')
    return redirect(url_for('candidate_login'))


# ============ BEFORE REQUEST ============
SESSION_EXEMPT = {
    'static', 'favicon', 'index', 'candidate_login', 'candidate_register',
    'candidate_sso_login', 'candidate_sso_callback', 'candidate_icewarp_login',
    'forgot_password', 'check_session', 'session_timeout_api'
}


@app.before_request
def enforce_session_timeout():
    if request.endpoint in SESSION_EXEMPT: return None
    if not current_user.is_authenticated: return None
    now = datetime.utcnow()
    last = session.get('last_activity')
    if last:
        try:
            la = datetime.fromisoformat(last)
        except (ValueError, TypeError):
            la = now
        if (now - la).total_seconds() > SESSION_TIMEOUT_MINUTES * 60:
            auth = session.get('auth_type')
            session.clear();
            logout_user()
            is_ajax = (request.path.startswith('/api/') or request.is_json
                       or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                       or 'application/json' in (request.headers.get('Accept') or ''))
            if is_ajax:
                return jsonify({'valid': False, 'reason': 'session_expired'}), 401
            flash('Session expired. Please login again.', 'warning')
            if auth == 'sso':
                return redirect("https://login.microsoftonline.com/common/oauth2/v2.0/logout"
                                "?post_logout_redirect_uri=" + url_for('candidate_login', _external=True))
            return redirect(url_for('candidate_login'))
    session['last_activity'] = now.isoformat()
    session.permanent = True
    return None


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        print("⏳ Pre-loading AI Models (This may take a few seconds)...")
        load_headphone_model()
        print("✅ AI Models Ready.")

    port = int(os.getenv('PORT', 8011))
    app.run(debug=True, use_reloader=False, host='0.0.0.0', port=port)