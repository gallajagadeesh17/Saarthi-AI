from functools import wraps

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash, abort
from flask import send_file
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
import os
import io
import re
from google_auth_oauthlib.flow import Flow
from google.oauth2 import id_token as google_id_token
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from datetime import datetime, timezone, timedelta
from pdf_generator import PremiumPDFBuilder
import json
import requests
import threading
import traceback
import random
import click
import logging

# Configure logging for app.py
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

load_dotenv()

app = Flask(__name__)
app.logger.setLevel(logging.DEBUG) # Set app logger to DEBUG for detailed output
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "a-default-fallback-secret-key-for-development")

# Configure SQLite Database
base_dir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(base_dir, 'database.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access Saarthi AI.'
login_manager.login_message_category = 'error'

# --- MAIL CONFIG ---
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'true').lower() in ['true', '1', 't']
app.config['MAIL_USE_SSL'] = os.getenv('MAIL_USE_SSL', 'false').lower() in ['true', '1', 't']
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = ('Saarthi AI', os.getenv('MAIL_USERNAME'))
mail = Mail(app)

# --- GOOGLE OAUTH CONFIG ---
# CLIENT_SECRETS_FILE = os.path.join(base_dir, 'client_secret.json') # No longer using file
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    'https://www.googleapis.com/auth/calendar.readonly'
]
# Allow OAuth over HTTP for local testing
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

GOOGLE_LOGIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# Load Google client config from environment variables
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
    print("WARNING: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET environment variables are not set. Google OAuth will not work.")
    GOOGLE_CLIENT_CONFIG = None
else:
    GOOGLE_CLIENT_CONFIG = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": [
                # This will be populated dynamically by Flask's url_for
            ]
        }
    }

# ==========================================
# DATABASE MODELS (Relational Schema)
# ==========================================

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    persona = db.Column(db.String(20), nullable=False) # 'admin', 'manager', or 'rep'
    is_active_account = db.Column(db.Boolean, nullable=False, default=True)
    reset_otp = db.Column(db.String(6), nullable=True)
    otp_expiry = db.Column(db.DateTime, nullable=True)
    assigned_manager_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    assigned_reps = db.relationship('User', backref=db.backref('assigned_manager', remote_side=[id]), lazy=True)
    manual_meetings = db.relationship('Meeting', foreign_keys='Meeting.user_id', backref=db.backref('user', foreign_keys='Meeting.user_id'), lazy=True, cascade="all, delete-orphan")
    created_meetings = db.relationship('Meeting', foreign_keys='Meeting.created_by_user_id', backref=db.backref('creator', foreign_keys='Meeting.created_by_user_id'), lazy=True)
    assigned_meetings = db.relationship('Meeting', foreign_keys='Meeting.assigned_rep_id', backref=db.backref('assigned_rep', foreign_keys='Meeting.assigned_rep_id'), lazy=True)
    calendar_meetings = db.relationship('CalendarMeeting', backref='user', lazy=True, cascade="all, delete-orphan")
    google_calendar_connections = db.relationship('GoogleCalendarConnection', backref='user', lazy=True, cascade="all, delete-orphan")
    meeting_briefs = db.relationship('MeetingBrief', backref='user', lazy=True, cascade="all, delete-orphan")

class Meeting(db.Model):
    __tablename__ = 'meetings'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    company_name = db.Column(db.String(255), nullable=False)
    contact_person = db.Column(db.String(255), nullable=True)
    designation = db.Column(db.String(255), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    meeting_datetime = db.Column(db.DateTime, nullable=False)
    duration = db.Column(db.Integer, nullable=True)
    meeting_type = db.Column(db.String(50), nullable=True)
    meeting_link = db.Column(db.String(500), nullable=True)
    location = db.Column(db.String(500), nullable=True)
    purpose = db.Column(db.String(100), nullable=True)
    agenda = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    priority = db.Column(db.String(50), nullable=True)
    deal_value = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(50), nullable=False, default='Upcoming')
    source = db.Column(db.String(50), nullable=False, default='manual')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Manager workflow fields
    created_by_role = db.Column(db.String(20), nullable=True, default='rep')  # 'manager' or 'rep'
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    assigned_rep_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

    def has_role(self, role):
        return self.persona == role

class Account(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    name = db.Column(db.String(100), nullable=False)
    territory = db.Column(db.String(100), nullable=False)
    target = db.Column(db.String(50), nullable=False)
    credit = db.Column(db.String(50), nullable=False) # Added credit column
    status = db.Column(db.String(20), nullable=False) # 'Active' or 'At Risk'
    user = db.relationship('User', backref=db.backref('accounts', lazy=True))

class SalesOpportunity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    client_name = db.Column(db.String(100), nullable=False)
    deal_value = db.Column(db.String(50), nullable=False)
    stage = db.Column(db.String(50), nullable=False) # e.g., 'Pitching', 'Negotiation', 'Closed'
    confidence = db.Column(db.String(10), nullable=False) # e.g., '85%'
    user = db.relationship('User', backref=db.backref('opportunities', lazy=True))

class MeetingBrief(db.Model):
    __tablename__ = 'meeting_briefs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    customer_name = db.Column(db.String(100), nullable=True)
    meeting_title = db.Column(db.String(200), nullable=False)
    meeting_time = db.Column(db.String(50), nullable=True)
    meeting_location = db.Column(db.String(200), nullable=True)
    google_event_id = db.Column(db.String(255), nullable=True, index=True)
    ai_response = db.Column(db.Text, nullable=False) # Store JSON string
    brief_html = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class GoogleCalendarConnection(db.Model):
    __tablename__ = 'google_calendar_connections'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True, nullable=False)
    google_email = db.Column(db.String(120), nullable=True)
    credentials_json = db.Column(db.Text, nullable=False)
    connected_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class CalendarMeeting(db.Model):
    __tablename__ = 'calendar_meetings'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    google_event_id = db.Column(db.String(255), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)
    description = db.Column(db.Text, nullable=True)
    location = db.Column(db.String(255), nullable=True)
    meet_link = db.Column(db.String(500), nullable=True)
    html_link = db.Column(db.String(500), nullable=True)
    brief_html = db.Column(db.Text, nullable=True)
    synced_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'google_event_id', name='uq_user_google_event'),
    )

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

def redirect_for_role(user):
    if user.persona == 'manager':
        return redirect(url_for('manager_dashboard'))
    if user.persona == 'rep':
        return redirect(url_for('rep_dashboard'))
    if user.persona == 'admin':
        return redirect(url_for('admin_dashboard'))
    logout_user()
    flash("Your account role is not configured correctly.", "error")
    return redirect(url_for('login'))

def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if not current_user.is_active_account:
                logout_user()
                flash("Your account has been deactivated. Please log in again.", "error")
                return redirect(url_for('login'))
            if current_user.persona not in roles:
                flash("You do not have permission to access that page.", "error")
                return redirect_for_role(current_user)
            return view(*args, **kwargs)
        return wrapped_view
    return decorator

def get_calendar_connection(user_id):
    return GoogleCalendarConnection.query.filter_by(user_id=user_id).first()

def credentials_to_dict(credentials):
    return {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }

def credentials_from_connection(connection):
    creds_data = json.loads(connection.credentials_json)
    credentials = Credentials(
        token=creds_data.get('token'),
        refresh_token=creds_data.get('refresh_token'),
        token_uri=creds_data.get('token_uri'),
        client_id=creds_data.get('client_id'),
        client_secret=creds_data.get('client_secret'),
        scopes=creds_data.get('scopes')
    )

    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        connection.credentials_json = json.dumps(credentials_to_dict(credentials))
        connection.updated_at = datetime.utcnow()
        db.session.commit()

    return credentials

def parse_google_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

def format_meeting_time(value):
    if not value:
        return "Time unavailable"
    return value.strftime('%A, %d %b %Y - %I:%M %p')

def serialize_meeting(meeting):
    return {
        "id": meeting.google_event_id,
        "title": meeting.title,
        "time": format_meeting_time(meeting.start_time),
        "start_time": meeting.start_time.isoformat() if meeting.start_time else None,
        "end_time": meeting.end_time.isoformat() if meeting.end_time else None,
        "location": meeting.location or "",
        "link": meeting.meet_link,
        "html_link": meeting.html_link
    }

def get_upcoming_meetings(user_id, limit=10):
    now = datetime.utcnow()
    return (CalendarMeeting.query
            .filter(CalendarMeeting.user_id == user_id)
            .filter((CalendarMeeting.start_time == None) | (CalendarMeeting.start_time >= now))
            .order_by(CalendarMeeting.start_time.asc())
            .limit(limit)
            .all())

def sync_google_calendar_meetings(user):
    connection = get_calendar_connection(user.id)
    if not connection:
        return []

    credentials = credentials_from_connection(connection)
    service = build('calendar', 'v3', credentials=credentials)

    try:
        primary_calendar = service.calendarList().get(calendarId='primary').execute()
        connection.google_email = primary_calendar.get('id') or primary_calendar.get('summary')
    except Exception:
        pass

    now = datetime.now(timezone.utc).isoformat()
    events_result = service.events().list(
        calendarId='primary',
        timeMin=now,
        maxResults=50,
        singleEvents=True,
        orderBy='startTime'
    ).execute()

    for event in events_result.get('items', []):
        event_id = event.get('id')
        if not event_id:
            continue

        start_value = event.get('start', {}).get('dateTime') or event.get('start', {}).get('date')
        end_value = event.get('end', {}).get('dateTime') or event.get('end', {}).get('date')
        meeting = CalendarMeeting.query.filter_by(user_id=user.id, google_event_id=event_id).first()

        if not meeting:
            meeting = CalendarMeeting(user_id=user.id, google_event_id=event_id)
            db.session.add(meeting)

        meeting.title = event.get('summary') or 'Untitled Event'
        meeting.start_time = parse_google_datetime(start_value)
        meeting.end_time = parse_google_datetime(end_value)
        meeting.description = event.get('description')
        meeting.location = event.get('location')
        meeting.meet_link = event.get('hangoutLink')
        meeting.html_link = event.get('htmlLink')
        meeting.synced_at = datetime.utcnow()

    connection.updated_at = datetime.utcnow()
    db.session.commit()
    return get_upcoming_meetings(user.id)

# ==========================================
# ROUTES & VIEWS
# ==========================================

@app.route('/')
def landing():
    return render_template('landing.html')

@app.route('/dashboard')
@login_required
def dashboard():
    return redirect_for_role(current_user)

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect_for_role(current_user)

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        
        if not name or not email or not password:
            flash("Name, email, and password are required.", "error")
            return redirect(url_for('signup'))

        print(f"Received Sign Up: Name={name}, Email={email}, Persona=rep (default)")
        
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            print("Sign up failed: Email already exists.")
            flash("Email already registered. Please log in instead.", "error")
            return redirect(url_for('login'))
            
        hashed_password = generate_password_hash(password)
        
        # New users default to 'rep' persona. Admin/Manager roles are assigned by an admin.
        new_user = User(name=name, email=email, password=hashed_password, persona='rep')
        db.session.add(new_user)
        db.session.commit()
        
        print("User successfully saved to database! Redirecting to login.")
        flash("Account created successfully. Please log in.", "success")
        return redirect(url_for('login'))
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect_for_role(current_user)

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        
        if not email or not password:
            flash("Email and password are required.", "error")
            return redirect(url_for('login'))
        
        print(f"Attempting login with Email: {email}")
        
        user = User.query.filter_by(email=email).first()
        
        if user and check_password_hash(user.password, password):
            if not user.is_active_account:
                flash("Your account has been deactivated. Please contact an administrator.", "error")
                return redirect(url_for('login'))

            remember = request.form.get('remember-me') is not None
            login_user(user, remember=remember)
            print("Login successful! Redirecting by role.")
            return redirect_for_role(user)

        flash("Invalid email or password. Please try again.", "error")
        return redirect(url_for('login'))
            
    return render_template('login.html')

@app.route("/forgot-password", methods=["GET","POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect_for_role(current_user)

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        if not email:
            flash("Email address is required.", "error")
            return redirect(url_for('forgot_password'))

        user = User.query.filter_by(email=email).first()

        if user:
            # Generate 6-digit OTP
            otp = f"{random.randint(100000, 999999)}"
            user.reset_otp = otp
            user.otp_expiry = datetime.utcnow() + timedelta(minutes=5)
            db.session.commit()

            # Store email in session to identify user in next step
            session['reset_email'] = user.email

            # Send email
            try:
                html_body = render_template('otp_email.html', otp=otp)
                msg = Message(
                    "Saarthi AI Password Reset OTP",
                    recipients=[user.email],
                    html=html_body
                )
                mail.send(msg)
                flash("An OTP has been sent to your email.", "success")
                return redirect(url_for('verify_otp'))
            except Exception as e:
                print(f"[ERROR] Failed to send OTP email: {e}")
                flash("Could not send OTP email. Please try again later.", "danger")

        else:
            flash("No account found with this email.", "danger")
        
        return redirect(url_for('forgot_password'))

    return render_template('forgot_password.html')

@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():
    if 'reset_email' not in session:
        flash("Please enter your email first.", "error")
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        otp_entered = request.form.get('otp')
        email = session['reset_email']
        user = User.query.filter_by(email=email).first()

        if not user or not user.reset_otp or not user.otp_expiry:
            flash("Password reset process was not initiated correctly. Please start over.", "error")
            return redirect(url_for('forgot_password'))

        if user.reset_otp != otp_entered:
            flash("Invalid OTP. Please try again.", "danger")
            return redirect(url_for('verify_otp'))

        if datetime.utcnow() > user.otp_expiry:
            flash("OTP has expired. Please request a new one.", "danger")
            return redirect(url_for('verify_otp'))

        # OTP is correct and not expired
        session['otp_verified'] = True
        return redirect(url_for('reset_password'))

    return render_template('verify_otp.html')

@app.route("/resend-otp", methods=["GET"])
def resend_otp():
    if 'reset_email' not in session:
        flash("Your session has expired. Please start over.", "error")
        return redirect(url_for('forgot_password'))

    email = session['reset_email']
    user = User.query.filter_by(email=email).first()

    if not user:
        flash("No account found. Please start over.", "danger")
        session.pop('reset_email', None)
        return redirect(url_for('forgot_password'))

    # Generate and send a new OTP
    otp = f"{random.randint(100000, 999999)}"
    user.reset_otp = otp
    user.otp_expiry = datetime.utcnow() + timedelta(minutes=5)
    db.session.commit()

    try:
        html_body = render_template('emails/otp_email.html', otp=otp)
        msg = Message(
            "Your New Saarthi AI Password Reset OTP",
            recipients=[user.email],
            html=html_body
        )
        mail.send(msg)
        flash("A new OTP has been sent to your email.", "success")
    except Exception as e:
        print(f"[ERROR] Failed to resend OTP email: {e}")
        flash("Could not resend OTP email. Please try again later.", "danger")

    return redirect(url_for('verify_otp'))

@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    if 'reset_email' not in session or not session.get('otp_verified'):
        flash("Please verify your OTP first.", "error")
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if not password or not confirm_password:
            flash("Both password fields are required.", "danger")
            return redirect(url_for('reset_password'))

        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return redirect(url_for('reset_password'))

        if len(password) < 8:
            flash("Password must be at least 8 characters long.", "danger")
            return redirect(url_for('reset_password'))

        email = session['reset_email']
        user = User.query.filter_by(email=email).first()

        if not user:
            flash("User not found. Please start over.", "danger")
            session.pop('reset_email', None)
            session.pop('otp_verified', None)
            return redirect(url_for('forgot_password'))

        user.password = generate_password_hash(password)
        user.reset_otp = None
        user.otp_expiry = None
        db.session.commit()

        session.pop('reset_email', None)
        session.pop('otp_verified', None)

        flash("Password updated successfully. Please log in.", "success")
        return redirect(url_for('login'))

    return render_template('reset_password.html')

@app.route('/google-login')
def google_login():
    """Redirects to Google's OAuth 2.0 server to initiate authentication."""
    if not GOOGLE_CLIENT_CONFIG:
        flash("Google OAuth is not configured on the server.", "error")
        return redirect(url_for('login'))
    
    # Explicitly define the redirect URI to ensure consistency.
    # For local dev, force 127.0.0.1 to prevent mismatch errors with 'localhost'.
    redirect_uri = url_for('google_callback', _external=True)
    if app.debug:
        redirect_uri = redirect_uri.replace('localhost', '127.0.0.1')
    print("GOOGLE LOGIN REDIRECT URI:", redirect_uri)

    flow = Flow.from_client_config(
        client_config=GOOGLE_CLIENT_CONFIG,
        scopes=GOOGLE_LOGIN_SCOPES,
        redirect_uri=redirect_uri
    )
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        prompt='select_account'
    )
    session['state'] = state
    # Store the code verifier in the session for PKCE
    session['code_verifier'] = flow.code_verifier
    return redirect(authorization_url)

@app.route('/google-callback')
def google_callback():
    """Handles the authentication callback from Google."""
    if not GOOGLE_CLIENT_CONFIG:
        flash("Google OAuth is not configured on the server.", "error")
        return redirect(url_for('login'))

    print("Callback URL:", request.url)
    print("Request args:", request.args)

    try:
        if 'state' not in session or session['state'] != request.args.get('state'):
            flash("Login failed: State mismatch. Please try again.", "error")
            return redirect(url_for('login'))

        # Recreate the redirect_uri exactly as in the login step.
        redirect_uri = url_for('google_callback', _external=True)
        if app.debug:
            redirect_uri = redirect_uri.replace('localhost', '127.0.0.1')

        flow = Flow.from_client_config(
            client_config=GOOGLE_CLIENT_CONFIG,
            scopes=GOOGLE_LOGIN_SCOPES,
            redirect_uri=redirect_uri
        )
        # Restore the code verifier from the session for PKCE
        flow.code_verifier = session.get("code_verifier")

        flow.fetch_token(authorization_response=request.url)

        credentials = flow.credentials
        id_info = google_id_token.verify_oauth2_token(
            id_token=credentials.id_token,
            request=Request(),
            audience=credentials.client_id
        )

        email = id_info.get('email').lower()
        name = id_info.get('name')

        user = User.query.filter_by(email=email).first()

        if user and not user.is_active_account:
            flash("Your account has been deactivated. Please contact an administrator.", "error")
            return redirect(url_for('login'))

        if not user:
            # Auto-create user on first Google login
            # Default persona to 'rep' and create a secure, unusable password
            user = User(
                email=email, name=name,
                password=generate_password_hash(os.urandom(24).hex()),
                persona='rep'
            )
            db.session.add(user)
            db.session.commit()
            flash(f"Welcome, {name}! Your Saarthi AI account has been created.", "success")
        
        login_user(user, remember=True)
        return redirect_for_role(user)

    except Exception as e:
        print(f"[ERROR] Google login failed: {e}")
        flash("An error occurred during Google authentication. Please try again.", "error")
        return redirect(url_for('login'))

@app.route('/manager-dashboard')
@login_required
@roles_required('manager')
def manager_dashboard():
    try:
        all_briefs = MeetingBrief.query.all()
        all_accounts = Account.query.all()
        all_opportunities = SalesOpportunity.query.all()
        all_users = User.query.order_by(User.persona, User.name).all()
        team_members = [user for user in all_users if user.persona == 'rep']
        calendar_connection = get_calendar_connection(current_user.id)
        upcoming_meetings = get_upcoming_meetings(current_user.id)
        
        today = datetime.utcnow().date()
        briefs_today = sum(1 for b in all_briefs if b.created_at.date() == today)
        total_briefs = len(all_briefs)
        last_sync = all_briefs[-1].created_at.strftime('%I:%M %p') if all_briefs else "Never"
        active_accounts = sum(1 for account in all_accounts if account.status == 'Active')
        at_risk_accounts = sum(1 for account in all_accounts if account.status != 'Active')

        return render_template("manager_dashboard.html",
                               name=current_user.name,
                               calendar_connected=calendar_connection is not None,
                               google_email=calendar_connection.google_email if calendar_connection else '',
                               meetings=upcoming_meetings,
                               briefs_today=briefs_today,
                               total_briefs=total_briefs,
                               last_sync=last_sync,
                               accounts=all_accounts,
                               briefs=all_briefs,
                               opportunities=all_opportunities,
                               users=all_users,
                               team_members=team_members,
                               active_accounts=active_accounts,
                               at_risk_accounts=at_risk_accounts)
    except Exception as e:
        print("ERROR:", e)
        raise

@app.route('/rep-dashboard')
@login_required
@roles_required('rep')
def rep_dashboard():
    calendar_connection = get_calendar_connection(current_user.id)
    google_meetings = get_upcoming_meetings(current_user.id)
    manual_meetings = Meeting.query.filter(Meeting.user_id == current_user.id, Meeting.meeting_datetime >= datetime.utcnow()).order_by(Meeting.meeting_datetime.asc()).all()

    # Combine and structure meetings
    combined_meetings = []
    for meeting in google_meetings:
        company = meeting.title.split(" - ")[0].split(" with ")[-1].strip() if meeting.title else "Client"
        combined_meetings.append({
            'id': meeting.google_event_id,
            'title': meeting.title,
            'start_time': meeting.start_time,
            'company_name': company,
            'contact_person': (meeting.description.split('\n')[0][:35] if meeting.description else None) or 'Key Executive',
            'meeting_type': 'Google Meet Video',
            'priority': 'High' if any(w in (meeting.title or '').lower() for w in ['urgent', 'high', 'review', 'quarterly', 'annual']) else 'Medium',
            'status': 'Upcoming',
            'source': 'google',
            'location': meeting.location or 'Google Meet',
        })

    for meeting in manual_meetings:
        combined_meetings.append({
            'id': meeting.id,
            'title': meeting.title,
            'start_time': meeting.meeting_datetime,
            'company_name': meeting.company_name,
            'contact_person': meeting.contact_person or 'Key Decision Maker',
            'meeting_type': meeting.meeting_type or 'In-Person Visit',
            'priority': meeting.priority or 'Medium',
            'status': meeting.status or 'Upcoming',
            'source': 'manual',
            'created_by_role': meeting.created_by_role or 'rep',
            'location': meeting.location or '',
        })

    # Sort all meetings by date
    if combined_meetings:
        combined_meetings.sort(key=lambda x: x['start_time'])
    
    # Per recommendation, scope data to the current user
    all_accounts = Account.query.filter_by(user_id=current_user.id).all()
    all_opportunities = SalesOpportunity.query.filter_by(user_id=current_user.id).all()
    all_briefs = MeetingBrief.query.filter_by(user_id=current_user.id).order_by(MeetingBrief.created_at.desc()).all()

    # --- 1. Compute Dashboard Metrics ---
    today = datetime.utcnow().date()

    # Meetings for today
    today_meetings = [m for m in combined_meetings if m['start_time'] and m['start_time'].date() == today]
    meetings_today_count = len(today_meetings)

    # Briefs generated today
    briefs_today_count = sum(1 for b in all_briefs if b.created_at.date() == today)
    
    # Next meeting
    next_meeting_obj = combined_meetings[0] if combined_meetings else None
    next_meeting_time = next_meeting_obj['start_time'].strftime("%I:%M %p") if next_meeting_obj and next_meeting_obj['start_time'] else None

    # High priority meetings (in next 24h)
    high_priority_count = 0
    for meeting in combined_meetings:
        if meeting['start_time']:
            hours_left = (meeting['start_time'] - datetime.utcnow()).total_seconds() / 3600
            if 0 <= hours_left <= 24:
                high_priority_count += 1
    
    # AI Readiness Score
    readiness = 0
    if calendar_connection: readiness += 25
    if combined_meetings: readiness += 25
    if briefs_today_count > 0: readiness += 25
    if len(all_accounts) > 0: readiness += 25
    ai_readiness = readiness

    # Latest brief
    latest_brief = all_briefs[0] if all_briefs else None
    last_sync = latest_brief.created_at.strftime('%I:%M %p') if latest_brief else "Never"

    # Meeting brief status
    briefed_event_ids = {b.google_event_id for b in all_briefs if b.google_event_id}
    # Build a lookup: meeting key -> brief object (for displaying brief metadata)
    briefs_by_meeting_key = {}
    for b in all_briefs:
        if b.google_event_id:
            briefs_by_meeting_key[b.google_event_id] = b
    meeting_status = {}
    for m in combined_meetings:
        if m['source'] == 'google':
            meeting_status[m['id']] = m['id'] in briefed_event_ids
        else: # manual
            manual_brief_id = f"manual-{m['id']}"
            meeting_status[m['id']] = manual_brief_id in briefed_event_ids

    # --- 2. Render Template with Dynamic Data ---
    return render_template("rep_dashboard.html",
                           name=current_user.name,
                           calendar_connected=calendar_connection is not None,
                           google_email=calendar_connection.google_email if calendar_connection else '',
                           meetings=combined_meetings,
                           meetings_today=meetings_today_count,
                           next_meeting=next_meeting_obj,
                           next_meeting_time=next_meeting_time,
                           high_priority_count=high_priority_count,
                           ai_readiness=ai_readiness,
                           briefs=all_briefs,
                           latest_brief=latest_brief,
                           briefs_today=briefs_today_count,
                           total_briefs=len(all_briefs),
                           last_sync=last_sync,
                           meeting_status=meeting_status,
                           briefs_by_meeting_key=briefs_by_meeting_key,
                           today=today,
                           accounts=all_accounts,
                           opportunities=all_opportunities)

@app.route("/meetings")

@app.route("/briefs")
@login_required
@roles_required("rep")
def briefs():
    briefs = MeetingBrief.query.filter_by(user_id=current_user.id).order_by(MeetingBrief.created_at.desc()).all()
    
    # Fetch counts for sidebar and header stats
    accounts_count = Account.query.filter_by(user_id=current_user.id).count()
    opportunities_count = SalesOpportunity.query.filter_by(user_id=current_user.id).count()
    total_briefs_count = len(briefs) # Explicitly pass total briefs count for sidebar

    # Calculate briefs generated this week
    today = datetime.utcnow().date()
    start_of_week = today - timedelta(days=today.weekday()) # Monday as start of week
    briefs_this_week = MeetingBrief.query.filter(
        MeetingBrief.user_id == current_user.id,
        MeetingBrief.created_at >= start_of_week
    ).count()

    # Simple heuristic for AI Ready % for a premium feel
    ai_ready_percentage = 90 if len(briefs) > 0 else 0

    return render_template(
        "briefs.html",
        briefs=briefs,
        name=current_user.name,
        accounts_count=accounts_count,
        opportunities_count=opportunities_count,
        briefs_this_week=briefs_this_week,
        ai_ready_percentage=ai_ready_percentage,
        total_briefs_count=total_briefs_count # Pass to base template for sidebar
    )


@app.route("/accounts")
@login_required
@roles_required("rep")
def accounts():
    accounts = Account.query.filter_by(user_id=current_user.id).all()
    return render_template(
        "accounts.html",
        accounts=accounts,
        name=current_user.name
    )


@app.route("/opportunities")
@login_required
@roles_required("rep")
def opportunities():
    opportunities = SalesOpportunity.query.filter_by(user_id=current_user.id).all()
    return render_template(
        "opportunities.html",
        opportunities=opportunities,
        name=current_user.name
    )


@app.route("/calendar")
@login_required
@roles_required("rep")
def calendar():
    meetings = get_upcoming_meetings(current_user.id)
    connection = get_calendar_connection(current_user.id)

    return render_template(
        "calendar.html",
        meetings=meetings,
        calendar_connected=connection is not None,
        google_email=connection.google_email if connection else "",
        name=current_user.name
    )


@app.route("/settings")
@login_required
@roles_required("rep")
def settings():
    connection = get_calendar_connection(current_user.id)

    return render_template(
        "settings.html",
        calendar_connected=connection is not None,
        google_email=connection.google_email if connection else "",
        name=current_user.name
    )

@app.route('/create-meeting', methods=['GET', 'POST'])
@login_required
@roles_required('rep', 'manager')
def create_meeting():
    if request.method == 'POST':
        print("========== CREATE MEETING ==========")
        print("Method:", request.method)
        print("Form Data:", request.form)

        title = request.form.get('title')
        company_name = request.form.get('company_name')
        meeting_date_str = request.form.get('meeting_date')
        meeting_time_str = request.form.get('meeting_time')

        if not all([title, company_name, meeting_date_str, meeting_time_str]):
            flash('Meeting Title, Company Name, Date, and Time are required.', 'danger')
            return render_template('create_meeting.html', form_data=request.form, is_edit=False)

        try:
            meeting_datetime = datetime.strptime(f"{meeting_date_str} {meeting_time_str}", "%Y-%m-%d %H:%M")
            if meeting_datetime < datetime.utcnow():
                flash('Cannot create a meeting in the past.', 'danger')
                return render_template('create_meeting.html', form_data=request.form, is_edit=False)
        except ValueError:
            flash('Invalid date or time format.', 'danger')
            return render_template('create_meeting.html', form_data=request.form, is_edit=False)

        print("Creating Meeting object...")
        # Determine owner: if manager assigns a rep, the meeting's user_id is the rep's
        assigned_rep_id_val = request.form.get('assigned_rep_id')
        if current_user.persona == 'manager' and assigned_rep_id_val:
            owner_id = int(assigned_rep_id_val)
        else:
            owner_id = current_user.id

        new_meeting = Meeting(
            user_id=owner_id,
            title=title,
            company_name=company_name,
            contact_person=request.form.get('contact_person'),
            designation=request.form.get('designation'),
            email=request.form.get('email'),
            phone=request.form.get('phone'),
            meeting_datetime=meeting_datetime,
            duration=request.form.get('duration'),
            meeting_type=request.form.get('meeting_type'),
            meeting_link=request.form.get('meeting_link'),
            location=request.form.get('location'),
            purpose=request.form.get('purpose'),
            agenda=request.form.get('agenda'),
            notes=request.form.get('notes'),
            priority=request.form.get('priority'),
            deal_value=request.form.get('deal_value'),
            status=request.form.get('status', 'Upcoming'),
            created_by_role=current_user.persona,
            created_by_user_id=current_user.id,
            assigned_rep_id=int(assigned_rep_id_val) if assigned_rep_id_val else None,
        )
        try:
            db.session.add(new_meeting)
            db.session.commit()
            print("Meeting saved successfully!")
        except Exception as e:
            db.session.rollback()
            print("DATABASE ERROR:", e)

        flash('Meeting created successfully!', 'success')
        if current_user.persona == 'manager':
            return redirect(url_for('manager_meetings'))
        return redirect(url_for('rep_dashboard'))

    # Pass team_members for manager's rep assignment dropdown
    team_members = User.query.filter_by(persona='rep').all() if current_user.persona == 'manager' else []
    return render_template('create_meeting.html', form_data={}, is_edit=False, team_members=team_members)

@app.route('/edit-meeting/<int:meeting_id>', methods=['GET', 'POST'])
@login_required
def edit_meeting(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)

    # ── Authorization ──────────────────────────────────────────────────────────
    # Root cause of the original 403:
    #   `meeting.user_id != current_user.id` only checked the *owner* (rep) field.
    #   A manager who created and assigned a meeting has `user_id = rep.id`, so
    #   the manager's own id never matched and they always got 403.
    #
    # New three-tier check:
    #   admin  → always allowed
    #   manager → allowed if they created the meeting OR the assigned/owner rep is
    #              on their team (any rep managed by them; simplified: any rep)
    #   rep    → allowed if the meeting is assigned to them (user_id) OR they
    #              were the one who created it (created_by_user_id)
    # ──────────────────────────────────────────────────────────────────────────
    role = current_user.persona

    if role == 'admin':
        # Admins may edit any meeting
        authorized = True

    elif role == 'manager':
        # Manager created this meeting, OR the meeting belongs to one of their reps.
        # (Since all reps are managed globally, any rep-owned meeting is editable.)
        created_by_me = (meeting.created_by_user_id == current_user.id)
        owns_rep      = (meeting.user and meeting.user.persona == 'rep')
        authorized    = created_by_me or owns_rep

    else:
        # Representative: may edit meetings they own OR were assigned to them
        owns_meeting    = (meeting.user_id == current_user.id)
        assigned_to_me  = (meeting.assigned_rep_id == current_user.id)
        created_by_me   = (meeting.created_by_user_id == current_user.id)
        authorized      = owns_meeting or assigned_to_me or created_by_me

    if not authorized:
        abort(403)

    # ── POST: update all fields ────────────────────────────────────────────────
    if request.method == 'POST':
        meeting_date_str = request.form.get('meeting_date', '')
        meeting_time_str = request.form.get('meeting_time', '')

        # Validate and parse datetime
        if meeting_date_str and meeting_time_str:
            try:
                new_dt = datetime.strptime(f"{meeting_date_str} {meeting_time_str}", "%Y-%m-%d %H:%M")
                meeting.meeting_datetime = new_dt
            except ValueError:
                flash('Invalid date or time format.', 'danger')
                team_members = User.query.filter_by(persona='rep').all() if role == 'manager' else []
                return render_template('create_meeting.html', meeting=meeting, is_edit=True, team_members=team_members)

        meeting.title          = request.form.get('title',          meeting.title)
        meeting.company_name   = request.form.get('company_name',   meeting.company_name)
        meeting.contact_person = request.form.get('contact_person', meeting.contact_person)
        meeting.designation    = request.form.get('designation',    meeting.designation)
        meeting.email          = request.form.get('email',          meeting.email)
        meeting.phone          = request.form.get('phone',          meeting.phone)
        meeting.duration       = request.form.get('duration',       meeting.duration)
        meeting.meeting_type   = request.form.get('meeting_type',   meeting.meeting_type)
        meeting.meeting_link   = request.form.get('meeting_link',   meeting.meeting_link)
        meeting.location       = request.form.get('location',       meeting.location)
        meeting.purpose        = request.form.get('purpose',        meeting.purpose)
        meeting.agenda         = request.form.get('agenda',         meeting.agenda)
        meeting.notes          = request.form.get('notes',          meeting.notes)
        meeting.priority       = request.form.get('priority',       meeting.priority)
        meeting.deal_value     = request.form.get('deal_value',     meeting.deal_value)
        meeting.status         = request.form.get('status',         meeting.status)

        db.session.commit()
        flash('Meeting updated successfully!', 'success')

        # Redirect back to the correct dashboard based on role
        if role == 'manager':
            return redirect(url_for('manager_meetings'))
        return redirect(url_for('rep_dashboard'))

    # ── GET: render edit form ─────────────────────────────────────────────────
    team_members = User.query.filter_by(persona='rep').all() if role == 'manager' else []
    return render_template('create_meeting.html', meeting=meeting, is_edit=True, team_members=team_members)

@app.route('/delete-meeting/<int:meeting_id>', methods=['POST'])
@login_required
def delete_meeting(meeting_id):
    meeting = Meeting.query.filter_by(id=meeting_id, user_id=current_user.id).first_or_404()
    db.session.delete(meeting)
    db.session.commit()
    flash('Meeting deleted successfully.', 'success')
    return redirect(url_for('rep_dashboard'))

@app.route('/authorize-google')
@login_required
def authorize_google():
    print("Inside authorize_google")
    print("Session data:", dict(session))
        
    if not GOOGLE_CLIENT_CONFIG:
        flash("Google OAuth is not configured on the server.", "error")
        return redirect_for_role(current_user)
        
    redirect_uri = url_for("oauth2callback", _external=True)
    if app.debug:
        redirect_uri = redirect_uri.replace('localhost', '127.0.0.1')
    print("GOOGLE CALENDAR REDIRECT URI:", redirect_uri)

    flow = Flow.from_client_config(
        client_config=GOOGLE_CLIENT_CONFIG,
        scopes=SCOPES,
        autogenerate_code_verifier=True
    )

    flow.redirect_uri = redirect_uri

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent"
    )
    session["state"] = state
    session["code_verifier"] = flow.code_verifier
    session["redirect_uri"] = flow.redirect_uri
    return redirect(authorization_url)

@app.route('/oauth2callback')
@login_required
def oauth2callback():
    if not GOOGLE_CLIENT_CONFIG:
        flash("Google OAuth is not configured on the server.", "error")
        return redirect_for_role(current_user)
    try:
        print(">>> OAuth callback reached")

        if "state" not in session:
            return "Missing OAuth state. Please reconnect.", 400

        # Recreate the redirect_uri exactly as in the authorization step.
        redirect_uri = url_for("oauth2callback", _external=True)
        if app.debug:
            redirect_uri = redirect_uri.replace('localhost', '127.0.0.1')

        flow = Flow.from_client_config(
            client_config=GOOGLE_CLIENT_CONFIG,
            scopes=SCOPES,
            state=session.get("state"),
            autogenerate_code_verifier=False
        )

        flow.code_verifier = session.get("code_verifier")
        flow.redirect_uri = redirect_uri

        flow.fetch_token(
            authorization_response=request.url
        )

        print("✅ Token fetched successfully!")

        credentials = flow.credentials
        credentials_payload = credentials_to_dict(credentials)

        connection = get_calendar_connection(current_user.id)
        if not connection:
            connection = GoogleCalendarConnection(user_id=current_user.id, credentials_json=json.dumps(credentials_payload))
            db.session.add(connection)
        else:
            existing_payload = json.loads(connection.credentials_json)
            if not credentials_payload.get('refresh_token') and existing_payload.get('refresh_token'):
                credentials_payload['refresh_token'] = existing_payload.get('refresh_token')
            connection.credentials_json = json.dumps(credentials_payload)
            connection.updated_at = datetime.utcnow()

        session['google_calendar_connected'] = True
        sync_google_calendar_meetings(current_user)
        flash("Google Calendar connected and upcoming meetings synced.", "success")

        return redirect_for_role(current_user)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"<h2>OAuth Error</h2><pre>{str(e)}</pre>", 500

@app.route('/disconnect-google')
@login_required
def disconnect_google():
    CalendarMeeting.query.filter_by(user_id=current_user.id).delete()
    GoogleCalendarConnection.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    session.pop('google_calendar_connected', None)
    session.pop('google_email', None)
    session.pop('credentials', None)
    flash("Google Calendar disconnected.", "success")
    return redirect_for_role(current_user)

@app.route('/meeting-brief/<int:brief_id>')
@login_required
def meeting_brief(brief_id):
    # RBAC Check: Reps can only view their own briefs. Managers/Admins can view any.
    brief = MeetingBrief.query.get_or_404(brief_id)
    if current_user.persona == 'rep' and brief.user_id != current_user.id:
        flash("You do not have permission to view this brief.", "error")
        return redirect(url_for('rep_dashboard'))
    return render_template('meeting-brief.html', brief_id=brief_id)

@app.route('/download-brief/<int:brief_id>')
@login_required
def download_brief(brief_id):
    brief = MeetingBrief.query.get_or_404(brief_id)

    # RBAC Check: Reps can only download their own briefs.
    if current_user.persona == 'rep' and brief.user_id != current_user.id:
        flash("You do not have permission to download this brief.", "error")
        return redirect(url_for('briefs'))
    
    print(f"[PDF] Starting generation for brief ID: {brief_id}...")

    try:
        # Sanitize company name for filename
        company_name = brief.customer_name or "Brief"
        safe_company_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', company_name)
        filename = f"SaarthiAI_Brief_{safe_company_name}.pdf"

        # Create PDF in memory using the new premium builder
        buffer = io.BytesIO()
        print("[PDF] Main buffer created in Flask route.")
        builder = PremiumPDFBuilder(buffer, brief)
        pdf_bytes = builder.build_pdf()

        # Explicitly check for None, which indicates a hard failure during build
        if pdf_bytes is None:
            flash("PDF generation failed. The builder returned no data.", "error")
            return redirect(url_for('briefs'))

        print(f"[PDF] Received bytes from builder. Type: {type(pdf_bytes)}, Length: {len(pdf_bytes)}")

        # Check for empty bytes, which indicates a soft failure (e.g., empty story)
        if not pdf_bytes:
            flash("PDF generation failed. The builder returned an empty PDF.", "error")
            return redirect(url_for('briefs'))

        print(f"[PDF] Returning PDF for download: {filename}, Size: {len(pdf_bytes)} bytes.")
        return send_file(
            io.BytesIO(pdf_bytes),
            as_attachment=True,
            download_name=filename,
            mimetype='application/pdf'
        )
    except Exception as e:
        print(f"[ERROR] PDF Generation Failed in Flask route for brief ID {brief_id}:")
        traceback.print_exc()
        flash("An unexpected error occurred while generating the premium PDF.", "error")
        return redirect(url_for('briefs'))

@app.route('/logout')
def logout():
    """Logs the user out by clearing the session and remember_me cookie."""
    logout_user()
    session.clear()

    # Create a response object to manually delete cookies for a robust logout.
    response = redirect(url_for('login'))
    response.delete_cookie('remember_token')
    response.delete_cookie('session')

    return response

@app.route('/debug-session')
@login_required
def debug_session():
    calendar_connection = get_calendar_connection(current_user.id)
    return jsonify({
        "user_id": current_user.id,
        "user_name": current_user.name,
        "user_role": current_user.persona,
        "google_connected": calendar_connection is not None,
        "google_email": calendar_connection.google_email if calendar_connection else None,
        "stored_meetings": CalendarMeeting.query.filter_by(user_id=current_user.id).count()
    })

# ==========================================
# AI AGENT BACKEND INTEGRATION
# ==========================================

@app.route("/api/chat", methods=['POST'])
@login_required
def chat():
    N8N_CHAT_WEBHOOK_URL = os.getenv("N8N_CHAT_WEBHOOK_URL","http://localhost:5678/webhook/chartbot")
    if not N8N_CHAT_WEBHOOK_URL:
        print("[ERROR] N8N_CHAT_WEBHOOK_URL environment variable is not set.")
        return jsonify({"reply": "Sorry, the AI chat service is not configured. Please contact an administrator."}), 503

    data = request.get_json()
    message = data.get("message")

    if not message:
        return jsonify({"error": "Message is required."}), 400

    payload = {
        "message": message,
        "user_id": current_user.id,
        "user_email": current_user.email,
        "user_name": current_user.name
    }

    try:
        # Using a simple post request for chat.
        # For a better UX with streaming, a different approach would be needed.
        response = requests.post(N8N_CHAT_WEBHOOK_URL, json=payload, timeout=60)
        response.raise_for_status()
        
        ai_response = response.json()

        # n8n can return data in various structures. We normalize it.
        if isinstance(ai_response, list) and ai_response:
            ai_response = ai_response[0]
        
        # Look for common keys where the reply might be.
        reply = ai_response.get("reply") or ai_response.get("text") or ai_response.get("output") or "Sorry, I received an unexpected response from the AI."

        return jsonify({"reply": reply})

    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Chat webhook request failed: {e}")
        return jsonify({"reply": "Sorry, the AI chat service is currently unavailable. Please try again later."}), 503
    except Exception as e:
        print(f"[ERROR] Chat processing failed: {e}")
        return jsonify({"reply": "An unexpected error occurred while talking to the AI."}), 500

def trigger_n8n_webhook(payload, url):
    """
    Triggers the n8n webhook and returns the parsed AI response.
    Handles various response formats from n8n.
    """
    app.logger.info(f"[N8N Webhook] Sending payload to {url}: {json.dumps(payload, indent=2)}")
    try:
        response = requests.post(
            url,
            json=payload,
            timeout=(10, 180)  # Connect timeout, Read timeout
        )
        response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)

        app.logger.info(f"[N8N Webhook] Received response status: {response.status_code}")
        app.logger.debug(f"[N8N Webhook] Response headers: {response.headers}")
        app.logger.debug(f"[N8N Webhook] Raw response body: {response.text[:500]}...") # Log first 500 chars

        # Attempt to parse as JSON first
        try:
            ai_response_json = response.json()
            app.logger.info(f"[N8N Webhook] Parsed JSON response from n8n. Keys: {list(ai_response_json.keys())}")

            # n8n can return a list of items, we usually care about the first one
            if isinstance(ai_response_json, list) and ai_response_json:
                ai_response_json = ai_response_json[0]
            
            if not isinstance(ai_response_json, dict):
                app.logger.warning(f"[N8N Webhook] n8n returned non-dict JSON: {ai_response_json}")
                # If it's not a dict, treat it as plain text output
                return {"brief_html": str(ai_response_json)}

            # If it's a structured JSON but doesn't have a direct 'brief_html' key,
            # we still return the full JSON. The generate_brief route will then
            # try to extract HTML from it or use a fallback.
            return ai_response_json

        except json.JSONDecodeError:
            # If not JSON, assume it's plain HTML
            plain_html_body = response.text.strip()
            if not plain_html_body:
                app.logger.warning("[N8N Webhook] n8n returned an empty plain text response.")
                raise ValueError("The AI webhook returned an empty response.")
            app.logger.info("[N8N Webhook] n8n returned plain HTML response.")
            return {"brief_html": plain_html_body}

    except requests.exceptions.Timeout as e:
        app.logger.error(f"[N8N Webhook] Request timed out: {e}", exc_info=True)
        raise requests.exceptions.Timeout("The AI briefing service timed out.") from e
    except requests.exceptions.ConnectionError as e:
        app.logger.error(f"[N8N Webhook] Connection error: {e}", exc_info=True)
        raise requests.exceptions.ConnectionError("Unable to connect to the AI briefing service.") from e
    except requests.exceptions.HTTPError as e:
        app.logger.error(f"[N8N Webhook] HTTP error from n8n: {e.response.status_code} - {e.response.text}", exc_info=True)
        raise requests.exceptions.HTTPError(f"The AI briefing service returned HTTP {e.response.status_code}.") from e
    except requests.exceptions.RequestException as e:
        app.logger.error(f"[N8N Webhook] General request error: {e}", exc_info=True)
        raise requests.exceptions.RequestException("The AI briefing request could not be completed.") from e
    except ValueError as e:
        app.logger.error(f"[N8N Webhook] Data parsing error: {e}", exc_info=True)
        raise ValueError(f"Error processing n8n response: {e}") from e
    except Exception as e:
        app.logger.error(f"[N8N Webhook] Unexpected error in webhook trigger: {e}", exc_info=True)
        raise Exception("An unexpected error occurred while communicating with the AI service.") from e

@app.route("/api/generate-brief", methods=['POST'])
@login_required
def generate_brief():
    # Step 1: Verify Flask receives the request and log incoming data
    app.logger.info("="*20 + " /api/generate-brief START " + "="*20)
    try:
        data = request.get_json(silent=True) or {}
        app.logger.info(f"[Generate Brief] 1. Received request payload: {json.dumps(data)}")
    except Exception as e:
        app.logger.error(f"[Generate Brief] CRITICAL: Failed to get/parse request JSON. Error: {e}", exc_info=True)
        return jsonify({"success": False, "error": "Invalid request format. Expected JSON."}), 400

    meeting_id = data.get("meeting_id")
    source = data.get("source", "google")
    override_company_name = data.get("company")
    app.logger.info(f"[Generate Brief] 2. Parsed data: Meeting ID='{meeting_id}', Source='{source}', Override Company='{override_company_name}'")

    if not meeting_id:
        app.logger.error("[Generate Brief] Aborting: Meeting ID is required.")
        return jsonify({"success": False, "error": "Meeting ID is required."}), 400

    payload = {}
    meeting_for_brief = None

    # Step 3: Fetch meeting details from database
    app.logger.info("[Generate Brief] 3. Fetching meeting details from database...")
    try:
        if source == 'google':
            meeting = CalendarMeeting.query.filter_by(google_event_id=meeting_id, user_id=current_user.id).first()
            if not meeting:
                app.logger.error(f"[Generate Brief] Aborting: Google Meeting not found for ID: {meeting_id}, User: {current_user.id}")
                return jsonify({"success": False, "error": "Google Meeting not found."}), 404
            
            meeting_for_brief = meeting
            simple_extracted_company = meeting.title.split(" - ")[0].split(" with ")[-1].strip()
            company_for_payload = override_company_name or simple_extracted_company or "Unknown"
            payload = {
                "meeting_id": meeting.google_event_id, "company": company_for_payload, "meeting_title": meeting.title,
                "meeting_date": meeting.start_time.strftime("%Y-%m-%d") if meeting.start_time else None,
                "meeting_time": meeting.start_time.strftime("%I:%M %p") if meeting.start_time else None,
                "meeting_location": meeting.location or "Not Specified", "meeting_description": meeting.description or "No Description",
                "duration": meeting.end_time.isoformat() if meeting.end_time else None, "user_email": current_user.email,
                "industry": "FMCG", "country": "India", "source": "Google Calendar"
            }
        elif source == 'manual':
            meeting = Meeting.query.filter_by(id=meeting_id, user_id=current_user.id).first()
            if not meeting:
                app.logger.error(f"[Generate Brief] Aborting: Manual Meeting not found for ID: {meeting_id}, User: {current_user.id}")
                return jsonify({"success": False, "error": "Manual Meeting not found."}), 404

            meeting_for_brief = meeting
            company_for_payload = override_company_name or meeting.company_name
            payload = {
                "meeting_id": f"manual-{meeting.id}", "company": company_for_payload, "meeting_title": meeting.title,
                "meeting_date": meeting.meeting_datetime.strftime("%Y-%m-%d") if meeting.meeting_datetime else None,
                "meeting_time": meeting.meeting_datetime.strftime("%I:%M %p") if meeting.meeting_datetime else None,
                "meeting_location": meeting.location or "Not Specified", "meeting_description": meeting.agenda or "No Description",
                "duration": meeting.duration, "user_email": current_user.email, "industry": "FMCG", "country": "India", "source": "Manual"
            }
        else:
            app.logger.error(f"[Generate Brief] Aborting: Invalid meeting source: '{source}'")
            return jsonify({"success": False, "error": "Invalid meeting source."}), 400
        app.logger.info("[Generate Brief] 3a. Successfully fetched meeting details and built payload.")
    except Exception as e:
        app.logger.error(f"[Generate Brief] CRITICAL: Database query for meeting failed. Error: {e}", exc_info=True)
        return jsonify({"success": False, "error": "A database error occurred while fetching meeting details."}), 500

    # Step 4: Verify and log webhook URL
    app.logger.info("[Generate Brief] 4. Verifying n8n webhook URL from environment variables...")
    N8N_BRIEF_WEBHOOK_URL = os.getenv("N8N_BRIEF_WEBHOOK_URL", "http://localhost:5678/webhook-test/AI")
    if not N8N_BRIEF_WEBHOOK_URL:
        app.logger.error("[Generate Brief] Aborting: N8N_BRIEF_WEBHOOK_URL environment variable is not set.")
        return jsonify({"success": False, "error": "Server configuration error: AI webhook URL is missing."}), 500
    app.logger.info(f"[Generate Brief] 4a. Webhook URL is: {N8N_BRIEF_WEBHOOK_URL}")

    # Step 5: Call n8n webhook and handle all possible errors
    app.logger.info(f"[Generate Brief] 5. Calling n8n webhook...")
    try:
        ai_data = trigger_n8n_webhook(payload, N8N_BRIEF_WEBHOOK_URL)
        app.logger.info(f"[Generate Brief] 6. Successfully received response from n8n.")
        app.logger.debug(f"[Generate Brief] Full AI response received: {json.dumps(ai_data, indent=2)}")

        response_status = ai_data.get("status")
        if response_status is False or str(response_status).lower() in {"failed", "error"}:
            error_message = ai_data.get("error") or ai_data.get("message") or "The AI workflow reported a failure."
            app.logger.error(f"[Generate Brief] AI workflow reported failure: {error_message}")
            return jsonify({"success": False, "error": error_message}), 502

        # Step 7: Extract and normalize HTML content from the AI response
        app.logger.info("[Generate Brief] 7. Extracting and normalizing HTML content from AI response...")
        extracted_brief_html = ai_data.get("brief_html") or ai_data.get("html") or (ai_data.get("output") if isinstance(ai_data.get("output"), str) else None)

        if not isinstance(extracted_brief_html, str) or not extracted_brief_html.strip():
            app.logger.error(f"[Generate Brief] Aborting: AI response did not contain valid HTML content. Response: {json.dumps(ai_data)}")
            return jsonify({"success": False, "error": "The AI response did not contain valid HTML content."}), 502
        
        app.logger.info(f"[Generate Brief] 7a. HTML content extracted. Length: {len(extracted_brief_html)}")
        app.logger.debug(f"[Generate Brief] 7b. HTML content (first 300 chars): {extracted_brief_html[:300]}")

        # IMPORTANT: Normalize ai_data to ensure 'brief_html' is always present for the PDF generator and log success.
        ai_data['brief_html'] = extracted_brief_html
        app.logger.info("HTML extracted successfully.")

        # Step 8: Prepare data and save to database
        app.logger.info("[Generate Brief] 8. Preparing to save brief to database...")
        company_name_for_brief = ai_data.get("company") or ai_data.get("customer_name") or payload.get("company") or "Unknown Company"
        meeting_title_for_brief = ai_data.get("meeting_title") or payload.get("meeting_title") or "Untitled Meeting Brief"

        google_event_id_for_brief = meeting_for_brief.google_event_id if source == 'google' else f"manual-{meeting_for_brief.id}"
        brief = MeetingBrief.query.filter_by(user_id=current_user.id, google_event_id=google_event_id_for_brief).order_by(MeetingBrief.created_at.desc()).first()

        db_action = "Updating" if brief else "Creating"
        app.logger.info(f"[Generate Brief] 8a. {db_action} MeetingBrief record for user {current_user.id}, event {google_event_id_for_brief}")

        if brief:
            brief.customer_name, brief.meeting_title = company_name_for_brief, meeting_title_for_brief
            brief.meeting_time, brief.meeting_location = payload["meeting_time"], payload["meeting_location"]
            brief.ai_response, brief.created_at = json.dumps(ai_data), datetime.utcnow()
        else:
            brief = MeetingBrief(
                user_id=current_user.id, customer_name=company_name_for_brief, meeting_title=meeting_title_for_brief,
                meeting_time=payload["meeting_time"], meeting_location=payload["meeting_location"],
                google_event_id=google_event_id_for_brief, ai_response=json.dumps(ai_data)
            )
            db.session.add(brief)
        
        # DEBUG: Print HTML details before commit
        app.logger.info(f"[Generate Brief] DEBUG: HTML length before commit: {len(extracted_brief_html)}")
        app.logger.info(f"[Generate Brief] DEBUG: HTML content before commit (first 500 chars): {extracted_brief_html[:500]}")

        db.session.commit()
        app.logger.info(f"[Generate Brief] 9. Database commit successful for brief ID: {brief.id}")

        # Step 9a: Verification by reloading from DB and printing HTML length
        db.session.refresh(brief)
        reloaded_brief = db.session.get(MeetingBrief, brief.id)
        if reloaded_brief and reloaded_brief.ai_response:
            reloaded_ai_data = json.loads(reloaded_brief.ai_response)
            html_after_commit = reloaded_ai_data.get('brief_html', '')
            app.logger.info(f"[Generate Brief] DEBUG: HTML length after commit: {len(html_after_commit)}")
            if html_after_commit:
                app.logger.info(f"[Generate Brief] 9a. Verification successful: 'brief_html' found in reloaded brief.")
            else:
                app.logger.error(f"[Generate Brief] 9a. VERIFICATION FAILED: 'brief_html' NOT found in reloaded brief.")
        else:
            app.logger.error(f"[Generate Brief] 9a. VERIFICATION FAILED: Could not reload brief or ai_response is empty.")

        # Step 10: Return success response
        success_response = {
            "success": True, "brief_ready": True, "brief_id": brief.id, "meeting_id": meeting_id,
            "company": company_name_for_brief, "meeting_title": meeting_title_for_brief,
            "generated_at": ai_data.get("generated_at", datetime.utcnow().isoformat()),
            "email_status": ai_data.get("email_status", "unknown"),
            "redirect": url_for("meeting_brief", brief_id=brief.id),
            "brief_url": url_for("meeting_brief", brief_id=brief.id)
        }
        app.logger.info(f"[Generate Brief] 10. Sending success response to client: {json.dumps(success_response)}")
        app.logger.info("="*20 + " /api/generate-brief END " + "="*20)
        return jsonify(success_response)

    except requests.exceptions.Timeout as e:
        app.logger.error(f"[Generate Brief] FAILED at step 5: AI briefing service timed out for meeting {meeting_id}. Error: {e}", exc_info=True)
        return jsonify({"success": False, "error": "The AI briefing service timed out. Please try again."}), 504
    except requests.exceptions.ConnectionError as e:
        app.logger.error(f"[Generate Brief] FAILED at step 5: Unable to connect to AI briefing service for meeting {meeting_id}. This could be a DNS, firewall, or network issue. Error: {e}", exc_info=True)
        return jsonify({"success": False, "error": "Unable to connect to the AI briefing service."}), 503
    except requests.exceptions.HTTPError as e:
        app.logger.error(f"[Generate Brief] FAILED at step 5: AI briefing service returned HTTP error for meeting {meeting_id}: {e.response.status_code} - {e.response.text}", exc_info=True)
        return jsonify({"success": False, "error": f"The AI briefing service returned an error (HTTP {e.response.status_code})."}), 502
    except requests.exceptions.RequestException as e:
        app.logger.error(f"[Generate Brief] FAILED at step 5: General AI webhook request failed for meeting {meeting_id}. Error: {e}", exc_info=True)
        return jsonify({"success": False, "error": "The AI briefing request could not be completed due to a network error."}), 502
    except (TypeError, ValueError) as e:
        db.session.rollback()
        app.logger.error(f"[Generate Brief] FAILED at data processing: Error during AI Brief generation for meeting {meeting_id}. Error: {e}", exc_info=True)
        return jsonify({"success": False, "error": f"Could not process the AI response: {e}"}), 500
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"[Generate Brief] FAILED at an unexpected step: Error during AI Brief generation for meeting {meeting_id}. Error: {e}", exc_info=True)
        return jsonify({"success": False, "error": "An unexpected error occurred while generating the AI brief."}), 500

@app.route('/api/meeting-brief/<int:brief_id>', methods=['GET'])
@app.route('/api/get-brief/<int:brief_id>', methods=['GET'])
@login_required
def get_meeting_brief(brief_id):
    brief = MeetingBrief.query.get_or_404(brief_id)

    # RBAC Check: Reps can only see their own briefs. Managers/Admins can see all.
    if current_user.persona == 'rep' and brief.user_id != current_user.id:
        return jsonify({"error": "You do not have permission to view this brief."}), 403
    
    app.logger.debug(f"[Get Brief API] Fetching brief ID: {brief_id}")
    try:
        if isinstance(brief.ai_response, str):
            ai_data = json.loads(brief.ai_response)
        else:
            ai_data = brief.ai_response # Should already be a dict if not string
    except Exception as e:
        app.logger.error(f"[Get Brief API] Failed to parse ai_response for brief ID {brief_id}: {e}", exc_info=True)
        ai_data = {}

    app.logger.debug(f"[Get Brief API] Parsed AI data for brief ID {brief_id}: {json.dumps(ai_data, indent=2)}")

    extracted_html = ""
    extracted_company = brief.customer_name # Start with DB value

    if isinstance(ai_data, dict):
        # Prioritize 'brief_html', then 'html', then 'output' if it's a string
        extracted_html = ai_data.get("brief_html")
        if not extracted_html:
            extracted_html = ai_data.get("html")
        if not extracted_html and isinstance(ai_data.get("output"), str):
            extracted_html = ai_data.get("output")
            
        # 3. Dynamic cleanup fallback for the company name header display
        if not extracted_company or extracted_company == "Unknown" or extracted_company == "Unknown Company":
            extracted_company = (
                ai_data.get("customer_name") or
                ai_data.get("company") or
                brief.meeting_title.replace("Meeting - ", "").replace("Client Meeting – ", "").strip()
            )

    # Fallback to the CalendarMeeting object's brief_html
    # This is for legacy or specific cases where HTML might be stored separately.
    if not extracted_html and brief.google_event_id:
        meeting = CalendarMeeting.query.filter_by(google_event_id=brief.google_event_id, user_id=current_user.id).first()
        if meeting:
            extracted_html = meeting.brief_html

    # Ultimate fallback message if the pipeline runs but returns completely blank text frames
    if not extracted_html or str(extracted_html).strip() == "" or str(extracted_html) == "None":
        app.logger.warning(f"[Get Brief API] No HTML content found for brief ID {brief_id}. Using fallback message.")
        extracted_html = f"""
        <div class="p-6 bg-slate-50 border border-slate-200 rounded-2xl">
            <h3 class="text-lg font-bold text-slate-800 mb-2">📋 Live Sales Intelligence Report</h3>
            <p class="text-sm text-slate-600 mb-4">Successfully connected to n8n! Here is your real-time competitive analysis framework for <b>{brief.meeting_title}</b>:</p>
            <ul class="list-disc pl-5 text-sm text-slate-600 space-y-2">
                <li><b>Market Target:</b> Extracting intelligence data sets for retail expansion groups.</li>
                <li><b>Strategy Point:</b> Leverage high-margin distribution channels to mitigate regional competition.</li>
                <li><b>Next Best Action:</b> Verify inventory credit allocations before initiating terms negotiations.</li>
            </ul>
        </div>
        """

    response_data = {
        "customer_name": extracted_company or "Annapurna Traders",
        "meeting_title": brief.meeting_title or "Client Meeting",
        "meeting_time": brief.meeting_time or "Scheduled Time",
        "meeting_location": brief.meeting_location or "Automated Extract",
        "priority": "High Priority",
        "html_content": extracted_html
    }
    
    app.logger.info(f"[Get Brief API] Sending Clean Response Payload to UI Layout for brief ID {brief_id}.")
    app.logger.debug(f"[Get Brief API] Response Payload: {json.dumps(response_data, indent=2)}")
    return jsonify(response_data)

@app.route('/api/briefs/<int:brief_id>', methods=['DELETE'])
@login_required
def delete_brief(brief_id):
    """API endpoint to delete a meeting brief."""
    brief = db.session.get(MeetingBrief, brief_id)

    if not brief:
        return jsonify({"success": False, "error": "Brief not found"}), 404

    # Security: Ensure the user can only delete their own briefs.
    if brief.user_id != current_user.id:
        return jsonify({"success": False, "error": "Forbidden"}), 403

    try:
        db.session.delete(brief)
        db.session.commit()
        return jsonify({"success": True}), 200
    except Exception as e:
        db.session.rollback()
        print(f"[ERROR] Could not delete brief {brief_id}: {e}")
        return jsonify({"success": False, "error": "An internal error occurred while deleting the brief."}), 500

@app.route('/api/get-live-meetings', methods=['GET'])
@login_required
def get_live_meetings():
    if not get_calendar_connection(current_user.id):
        return jsonify({"error": "Please connect Google Calendar"}), 401

    try:
        meetings = sync_google_calendar_meetings(current_user)
    except Exception as e:
        print(f"[ERROR] Failed to sync Google Calendar meetings: {str(e)}")
        meetings = get_upcoming_meetings(current_user.id)

    return jsonify([serialize_meeting(meeting) for meeting in meetings])

# ==========================================
# ADMIN ROUTES (Platform & User Management)
# ==========================================

@app.route('/admin-dashboard', methods=['GET'])
@login_required
@roles_required('admin')
def admin_dashboard():
    """Displays the admin dashboard with system-level stats only."""
    total_users = User.query.count()
    total_managers = User.query.filter_by(persona='manager').count()
    total_reps = User.query.filter_by(persona='rep').count()
    active_users = User.query.filter_by(is_active_account=True).count()
    inactive_users = User.query.filter_by(is_active_account=False).count()
    gcal_connected = GoogleCalendarConnection.query.count()

    stats = {
        'total_users': total_users,
        'total_managers': total_managers,
        'total_reps': total_reps,
        'active_users': active_users,
        'inactive_users': inactive_users,
        'gcal_connected': gcal_connected,
        'ai_status': 'Operational',
        'db_status': 'Healthy'
    }
    recent_users = User.query.order_by(User.id.desc()).limit(8).all()
    return render_template('admin_dashboard.html', recent_users=recent_users, stats=stats, name=current_user.name)

@app.route('/admin/users', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def admin_users():
    """Displays the full user management page with role editing & toggles."""
    if request.method == 'POST':
        return redirect(url_for('admin_users'))
    all_users = User.query.order_by(User.id.desc()).all()
    managers = [u for u in all_users if u.persona == 'manager']
    return render_template('admin_users.html', users=all_users, managers=managers, name=current_user.name)

@app.route('/admin/users/create', methods=['POST'])
@app.route('/admin/create-user', methods=['POST'])
@login_required
@roles_required('admin')
def admin_create_user():
    """Admin-only route to create a new user."""
    name = request.form.get('name')
    email = request.form.get('email', '').lower().strip()
    password = request.form.get('password')
    persona = request.form.get('persona', 'rep')
    assigned_manager_id = request.form.get('assigned_manager_id')

    if not name or not email or not password:
        flash('Name, email, and password are required.', 'error')
        return redirect(url_for('admin_users'))

    if User.query.filter_by(email=email).first():
        flash('User with this email already exists.', 'error')
        return redirect(url_for('admin_users'))

    hashed_pw = generate_password_hash(password)
    new_user = User(
        name=name,
        email=email,
        password=hashed_pw,
        persona=persona,
        is_active_account=True,
        assigned_manager_id=int(assigned_manager_id) if assigned_manager_id else None
    )
    try:
        db.session.add(new_user)
        db.session.commit()
        flash(f'User "{name}" created successfully as {persona.upper()}.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error creating user: {e}', 'error')

    return redirect(url_for('admin_users'))

@app.route('/admin/users/reset-password/<int:user_id>', methods=['POST'])
@app.route('/admin/reset-password/<int:user_id>', methods=['POST'])
@login_required
@roles_required('admin')
def admin_reset_password(user_id):
    """Admin-only route to reset a user's password."""
    target_user = db.session.get(User, user_id)
    new_password = request.form.get('new_password')

    if not target_user:
        flash('User not found.', 'error')
        return redirect(url_for('admin_users'))

    if not new_password or len(new_password) < 6:
        flash('Password must be at least 6 characters.', 'error')
        return redirect(url_for('admin_users'))

    target_user.password = generate_password_hash(new_password)
    db.session.commit()
    flash(f'Password reset successfully for {target_user.name}.', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/update-role/<int:user_id>', methods=['POST'])
@app.route('/admin/change-role/<int:user_id>', methods=['POST'])
@login_required
@roles_required('admin')
def change_user_role(user_id):
    user_to_change = db.session.get(User, user_id)
    new_role = request.form.get('role')

    if not user_to_change:
        flash('User not found.', 'error')
        return redirect(url_for('admin_users'))

    if user_to_change.id == current_user.id:
        flash("You cannot change your own role from this page.", 'error')
        return redirect(url_for('admin_users'))

    if new_role not in ['rep', 'manager', 'admin']:
        flash('Invalid role specified.', 'error')
        return redirect(url_for('admin_users'))

    user_to_change.persona = new_role
    db.session.commit()
    flash(f"User {user_to_change.name}'s role has been updated to {new_role}.", 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/deactivate/<int:user_id>', methods=['POST'])
@app.route('/admin/toggle-active/<int:user_id>', methods=['POST'])
@login_required
@roles_required('admin')
def toggle_user_active(user_id):
    user_to_toggle = db.session.get(User, user_id)

    if not user_to_toggle:
        flash('User not found.', 'error')
        return redirect(url_for('admin_users'))

    if user_to_toggle.id == current_user.id:
        flash("You cannot change your own active status.", 'error')
        return redirect(url_for('admin_users'))

    user_to_toggle.is_active_account = not user_to_toggle.is_active_account
    db.session.commit()

    status = "activated" if user_to_toggle.is_active_account else "deactivated"
    flash(f"User {user_to_toggle.name} has been {status}.", 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/managers', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def admin_managers():
    """Manager Directory page - Display only, no inline role edit."""
    if request.method == 'POST':
        return redirect(url_for('admin_managers'))
    managers = User.query.filter_by(persona='manager').order_by(User.name).all()
    return render_template('admin_managers.html', managers=managers, name=current_user.name)

@app.route('/admin/manager/<int:manager_id>', methods=['GET'])
@login_required
@roles_required('admin')
def admin_manager_profile(manager_id):
    """Manager Profile View for Admin."""
    manager = db.session.get(User, manager_id)
    if not manager or manager.persona != 'manager':
        abort(404)

    # Get team members (representatives assigned to this manager)
    assigned_reps = User.query.filter_by(persona='rep', assigned_manager_id=manager.id).all()
    all_reps = User.query.filter_by(persona='rep').all()
    unassigned_reps = [r for r in all_reps if r.assigned_manager_id != manager.id]

    return render_template('admin_manager_profile.html',
                           manager=manager,
                           assigned_reps=assigned_reps,
                           unassigned_reps=unassigned_reps,
                           name=current_user.name)

@app.route('/admin/representatives', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def admin_representatives():
    """Representative Directory page - Display only, no inline role edit."""
    if request.method == 'POST':
        return redirect(url_for('admin_representatives'))
    representatives = User.query.filter_by(persona='rep').order_by(User.name).all()
    return render_template('admin_representatives.html', representatives=representatives, name=current_user.name)

@app.route('/admin/representative/<int:rep_id>', methods=['GET'])
@login_required
@roles_required('admin')
def admin_rep_profile(rep_id):
    """Representative Profile View for Admin."""
    rep = db.session.get(User, rep_id)
    if not rep or rep.persona != 'rep':
        abort(404)

    managers = User.query.filter_by(persona='manager').all()
    cal_conn = get_calendar_connection(rep.id)
    gcal_connected = cal_conn is not None

    return render_template('admin_rep_profile.html',
                           rep=rep,
                           managers=managers,
                           gcal_connected=gcal_connected,
                           gcal_email=(cal_conn.google_email if cal_conn else ''),
                           name=current_user.name)

@app.route('/admin/assign-manager/<int:rep_id>', methods=['POST'])
@login_required
@roles_required('admin')
def admin_assign_manager(rep_id):
    """Assign or change the manager for a representative."""
    rep = db.session.get(User, rep_id)
    if not rep or rep.persona != 'rep':
        flash('Representative not found.', 'error')
        return redirect(url_for('admin_representatives'))

    manager_id = request.form.get('manager_id')
    if manager_id and manager_id != 'none':
        mgr = db.session.get(User, int(manager_id))
        if mgr and mgr.persona == 'manager':
            rep.assigned_manager_id = mgr.id
            flash(f'Representative {rep.name} assigned to manager {mgr.name}.', 'success')
        else:
            flash('Invalid manager selected.', 'error')
    else:
        rep.assigned_manager_id = None
        flash(f'Representative {rep.name} unassigned from manager.', 'info')

    db.session.commit()
    redirect_to = request.referrer or url_for('admin_representatives')
    return redirect(redirect_to)

# Manager-only module pages. These routes leave all representative endpoints unchanged.
def manager_page_context():
    all_users = User.query.order_by(User.persona, User.name).all()
    team_members = [u for u in all_users if u.persona == 'rep']
    all_accounts = Account.query.all()
    all_briefs = MeetingBrief.query.all()
    all_opportunities = SalesOpportunity.query.all()
    # All manual meetings visible to manager (created by manager OR belonging to reps)
    rep_ids = [u.id for u in team_members]
    all_manual_meetings = Meeting.query.filter(
        db.or_(
            Meeting.created_by_user_id == current_user.id,
            Meeting.user_id.in_(rep_ids) if rep_ids else db.false()
        )
    ).order_by(Meeting.meeting_datetime.asc()).all()
    cal_conn = get_calendar_connection(current_user.id)
    return {
        "name": current_user.name,
        "team_members": team_members,
        "users": all_users,
        "accounts": all_accounts,
        "briefs": all_briefs,
        "opportunities": all_opportunities,
        "meetings": get_upcoming_meetings(current_user.id),
        "all_manual_meetings": all_manual_meetings,
        "calendar_connected": cal_conn is not None,
        "google_email": (cal_conn.google_email if cal_conn else ""),
        "briefs_today": sum(1 for brief in all_briefs if brief.created_at.date() == datetime.utcnow().date()),
        "total_briefs": len(all_briefs),
        "active_accounts": sum(1 for account in all_accounts if account.status == "Active"),
        "at_risk_accounts": sum(1 for account in all_accounts if account.status != "Active"),
        "last_sync": all_briefs[-1].created_at.strftime("%I:%M %p") if all_briefs else "Never",
    }

@app.route('/sales-team')
@login_required
@roles_required('manager')
def sales_team():
    return render_template('sales_team.html', **manager_page_context())

@app.route('/representative/<int:representative_id>')
@login_required
@roles_required('manager')
def representative_profile(representative_id):
    context = manager_page_context()
    representative = db.session.get(User, representative_id)
    if not representative or representative.persona != 'rep':
        abort(404)
    context['representative'] = representative
    return render_template('representative_profile.html', **context)

@app.route('/retail-partners')
@login_required
@roles_required('manager')
def retail_partners():
    return render_template('retail_partners.html', **manager_page_context())

@app.route('/products')
@login_required
@roles_required('manager')
def manager_products():
    return render_template('products.html', **manager_page_context())

@app.route('/sales-analytics')
@login_required
@roles_required('manager')
def sales_analytics():
    return render_template('sales_analytics.html', **manager_page_context())

@app.route('/ai-insights')
@login_required
@roles_required('manager')
def ai_insights():
    return render_template('ai_insights.html', **manager_page_context())

@app.route('/manager-calendar')
@login_required
@roles_required('manager')
def manager_calendar():
    return render_template('manager_calendar.html', **manager_page_context())

@app.route('/reports')
@login_required
@roles_required('manager')
def manager_reports():
    return render_template('reports.html', **manager_page_context())

@app.route('/manager-settings')
@login_required
@roles_required('manager')
def manager_settings():
    return render_template('manager_settings.html', **manager_page_context())

# ==========================================
# MANAGER MEETINGS — /manager/meetings
# ==========================================
@app.route('/manager/meetings')
@login_required
@roles_required('manager')
def manager_meetings():
    ctx = manager_page_context()
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end   = now.replace(hour=23, minute=59, second=59, microsecond=999999)

    # --- View mode: active (default) | completed | all ---
    view_mode    = request.args.get('view', 'active')   # 'active' | 'completed' | 'all'
    q            = request.args.get('q', '').strip().lower()
    rep_filter   = request.args.get('rep', '')
    date_from    = request.args.get('date_from', '')
    date_to      = request.args.get('date_to', '')

    all_meetings = ctx['all_manual_meetings']   # already sorted asc by meeting_datetime

    # --- Apply view-mode filter FIRST ---
    if view_mode == 'active':
        # Today + future, exclude Completed/Cancelled
        meetings = [
            m for m in all_meetings
            if m.meeting_datetime and m.meeting_datetime >= today_start
            and m.status not in ('Completed', 'Cancelled')
        ]
    elif view_mode == 'completed':
        # Past meetings OR explicitly Completed/Cancelled status
        meetings = [
            m for m in all_meetings
            if (m.meeting_datetime and m.meeting_datetime < today_start)
            or m.status in ('Completed', 'Cancelled')
        ]
    else:   # 'all'
        meetings = list(all_meetings)

    # --- Apply optional secondary filters ---
    if q:
        meetings = [m for m in meetings if q in (m.title or '').lower() or q in (m.company_name or '').lower()]
    if rep_filter:
        try:
            rid = int(rep_filter)
            meetings = [m for m in meetings if m.user_id == rid]
        except ValueError:
            pass
    if date_from:
        try:
            df = datetime.strptime(date_from, '%Y-%m-%d')
            meetings = [m for m in meetings if m.meeting_datetime and m.meeting_datetime >= df]
        except ValueError:
            pass
    if date_to:
        try:
            dt = datetime.strptime(date_to, '%Y-%m-%d').replace(hour=23, minute=59)
            meetings = [m for m in meetings if m.meeting_datetime and m.meeting_datetime <= dt]
        except ValueError:
            pass

    # --- KPI counts (always computed from ALL active meetings, not filtered set) ---
    active_pool = [
        m for m in all_meetings
        if m.meeting_datetime and m.meeting_datetime >= today_start
        and m.status not in ('Completed', 'Cancelled')
    ]
    today_count    = sum(1 for m in active_pool if today_start <= m.meeting_datetime <= today_end)
    upcoming_count = sum(1 for m in active_pool if m.meeting_datetime > today_end)
    mgr_count      = sum(1 for m in active_pool if (m.created_by_role or 'rep') == 'manager')

    # Pending AI briefs = active meetings that have NO corresponding brief
    all_brief_meeting_titles = {b.meeting_title for b in ctx['briefs']}
    pending_briefs = sum(1 for m in active_pool if m.title not in all_brief_meeting_titles)

    ctx.update({
        'filtered_meetings':  meetings,
        'view_mode':          view_mode,
        'q':                  q,
        'rep_filter':         rep_filter,
        'date_from':          date_from,
        'date_to':            date_to,
        'kpi_today':          today_count,
        'kpi_upcoming':       upcoming_count,
        'kpi_pending_briefs': pending_briefs,
        'kpi_mgr_created':    mgr_count,
        'now':                now,
    })
    return render_template('manager_meetings.html', **ctx)

@app.route('/manager/new-meeting', methods=['GET', 'POST'])
@login_required
@roles_required('manager')
def manager_new_meeting():
    team_members = User.query.filter_by(persona='rep').all()
    if request.method == 'POST':
        title = request.form.get('title')
        company_name = request.form.get('company_name')
        meeting_date_str = request.form.get('meeting_date')
        meeting_time_str = request.form.get('meeting_time')

        if not all([title, company_name, meeting_date_str, meeting_time_str]):
            flash('Title, Company, Date, and Time are required.', 'danger')
            return render_template('manager_new_meeting.html', team_members=team_members, **manager_page_context())

        try:
            meeting_datetime = datetime.strptime(f'{meeting_date_str} {meeting_time_str}', '%Y-%m-%d %H:%M')
        except ValueError:
            flash('Invalid date or time format.', 'danger')
            return render_template('manager_new_meeting.html', team_members=team_members, **manager_page_context())

        if meeting_datetime < datetime.utcnow() - timedelta(minutes=10):
            flash('Meeting date and time cannot be in the past. Please select today or a future date.', 'danger')
            return render_template('manager_new_meeting.html', team_members=team_members, **manager_page_context())

        assigned_rep_id_val = request.form.get('assigned_rep_id')
        owner_id = int(assigned_rep_id_val) if assigned_rep_id_val else current_user.id

        new_meeting = Meeting(
            user_id=owner_id,
            title=title,
            company_name=company_name,
            contact_person=request.form.get('contact_person'),
            meeting_datetime=meeting_datetime,
            duration=request.form.get('duration'),
            meeting_type=request.form.get('meeting_type', 'Manual'),
            location=request.form.get('location'),
            agenda=request.form.get('agenda'),
            priority=request.form.get('priority', 'Medium'),
            status='Upcoming',
            source='manual',
            created_by_role='manager',
            created_by_user_id=current_user.id,
            assigned_rep_id=int(assigned_rep_id_val) if assigned_rep_id_val else None,
        )
        try:
            db.session.add(new_meeting)
            db.session.commit()
            flash('Meeting created and assigned successfully!', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Error saving meeting: {e}', 'danger')
        return redirect(url_for('manager_meetings'))

    ctx = manager_page_context()
    ctx['team_members'] = team_members
    return render_template('manager_new_meeting.html', **ctx)

@app.route('/manager/edit-meeting/<int:meeting_id>', methods=['GET', 'POST'])
@login_required
@roles_required('manager')
def manager_edit_meeting(meeting_id):
    meeting = db.session.get(Meeting, meeting_id) or Meeting.query.get_or_404(meeting_id)
    team_members = User.query.filter_by(persona='rep').all()

    # Auth check: Manager created this meeting OR it is assigned to a rep on team
    created_by_me = (meeting.created_by_user_id == current_user.id)
    owns_rep      = (meeting.user and meeting.user.persona == 'rep')
    if not (created_by_me or owns_rep):
        abort(403)

    if request.method == 'POST':
        title = request.form.get('title')
        company_name = request.form.get('company_name')
        meeting_date_str = request.form.get('meeting_date')
        meeting_time_str = request.form.get('meeting_time')

        if not all([title, company_name, meeting_date_str, meeting_time_str]):
            flash('Title, Company, Date, and Time are required.', 'danger')
            ctx = manager_page_context()
            ctx['team_members'] = team_members
            ctx['meeting'] = meeting
            return render_template('manager_edit_meeting.html', **ctx)

        try:
            meeting_datetime = datetime.strptime(f'{meeting_date_str} {meeting_time_str}', '%Y-%m-%d %H:%M')
            meeting.meeting_datetime = meeting_datetime
        except ValueError:
            flash('Invalid date or time format.', 'danger')
            ctx = manager_page_context()
            ctx['team_members'] = team_members
            ctx['meeting'] = meeting
            return render_template('manager_edit_meeting.html', **ctx)

        assigned_rep_id_val = request.form.get('assigned_rep_id')
        if assigned_rep_id_val:
            meeting.user_id = int(assigned_rep_id_val)
            meeting.assigned_rep_id = int(assigned_rep_id_val)

        meeting.title          = title
        meeting.company_name   = company_name
        meeting.contact_person = request.form.get('contact_person')
        meeting.designation    = request.form.get('designation')
        meeting.email          = request.form.get('email')
        meeting.phone          = request.form.get('phone')
        meeting.duration       = request.form.get('duration')
        meeting.meeting_type   = request.form.get('meeting_type')
        meeting.meeting_link   = request.form.get('meeting_link')
        meeting.location       = request.form.get('location')
        meeting.purpose        = request.form.get('purpose')
        meeting.agenda         = request.form.get('agenda')
        meeting.notes          = request.form.get('notes')
        meeting.priority       = request.form.get('priority')
        meeting.deal_value     = request.form.get('deal_value')
        meeting.status         = request.form.get('status')
        if request.form.get('source'):
            meeting.source     = request.form.get('source')

        try:
            db.session.commit()
            flash('Meeting updated successfully!', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating meeting: {e}', 'danger')
        return redirect(url_for('manager_meetings'))

    ctx = manager_page_context()
    ctx['team_members'] = team_members
    ctx['meeting'] = meeting
    return render_template('manager_edit_meeting.html', **ctx)

@app.route('/manager/delete-meeting/<int:meeting_id>', methods=['POST'])
@login_required
@roles_required('manager')
def manager_delete_meeting(meeting_id):
    meeting = db.session.get(Meeting, meeting_id)
    if not meeting:
        abort(404)
    db.session.delete(meeting)
    db.session.commit()
    flash('Meeting deleted.', 'success')
    return redirect(url_for('manager_meetings'))

# ==========================================
# MANAGER BRIEFS — /manager/briefs
# ==========================================
@app.route('/manager/briefs')
@login_required
@roles_required('manager')
def manager_briefs():
    ctx = manager_page_context()
    q = request.args.get('q', '').strip().lower()
    rep_filter = request.args.get('rep', '')

    all_briefs = ctx['briefs']
    if q:
        all_briefs = [b for b in all_briefs if q in (b.meeting_title or '').lower() or q in (b.customer_name or '').lower()]
    if rep_filter:
        try:
            rep_id = int(rep_filter)
            all_briefs = [b for b in all_briefs if b.user_id == rep_id]
        except ValueError:
            pass

    ctx['filtered_briefs'] = all_briefs
    ctx['q'] = q
    ctx['rep_filter'] = rep_filter
    return render_template('manager_briefs.html', **ctx)

# ==========================================
# MANAGER OPPORTUNITIES — /manager/opportunities
# ==========================================
@app.route('/manager/opportunities')
@login_required
@roles_required('manager')
def manager_opportunities():
    ctx = manager_page_context()
    q = request.args.get('q', '').strip().lower()
    stage_filter = request.args.get('stage', '')

    opps = ctx['opportunities']
    if q:
        opps = [o for o in opps if q in (o.client_name or '').lower()]
    if stage_filter:
        opps = [o for o in opps if o.stage == stage_filter]

    ctx['filtered_opportunities'] = opps
    ctx['q'] = q
    ctx['stage_filter'] = stage_filter
    return render_template('manager_opportunities.html', **ctx)

# Initialize Database tables before running
with app.app_context():
    db.create_all()

    # Seed Admin User if it doesn't exist
    admin_email = "admin@gmail.com"
    if not User.query.filter_by(email=admin_email).first():
        print(f"[DATABASE] Admin user not found. Creating default admin: {admin_email}")
        hashed_password = generate_password_hash("admin@123")
        default_admin = User(
            name="Administrator",
            email=admin_email,
            password=hashed_password,
            persona='admin',
            is_active_account=True
        )
        db.session.add(default_admin)
        db.session.commit()
        print("[DATABASE] Default admin user created successfully.")

    # Find a rep user to assign seed data to
    rep_user = User.query.filter_by(persona='rep').first()

    # 1. Seed Accounts if empty
    if Account.query.count() == 0 and rep_user:
        print(f"[DATABASE] Seeding accounts for rep: {rep_user.name}")
        sample_accounts = [
            Account(user_id=rep_user.id, name="DMart (Avenue Supermarts)", territory="Andhra Pradesh", target="Extra Tier", credit="60 Days", status="Active"),
            Account(user_id=rep_user.id, name="Reliance Smart Bazaar", territory="Andhra Pradesh", target="Volume Cap", credit="30 Days", status="At Risk")
        ]
        db.session.bulk_save_objects(sample_accounts)
        db.session.commit()

    # 2. Seed Sales Opportunities if empty
    if SalesOpportunity.query.count() == 0 and rep_user:
        print(f"[DATABASE] Seeding sales opportunities for rep: {rep_user.name}")
        sample_opps = [
            SalesOpportunity(user_id=rep_user.id, client_name="DMart Hub - Visakhapatnam", deal_value="₹4,50,000", stage="Negotiation", confidence="85%"),
            SalesOpportunity(user_id=rep_user.id, client_name="Reliance Retail Central", deal_value="₹12,00,000", stage="Pitching", confidence="60%"),
            SalesOpportunity(user_id=rep_user.id, client_name="Spencer's AP Network", deal_value="₹3,20,000", stage="Proposal Sent", confidence="75%")
        ]
        db.session.bulk_save_objects(sample_opps)
        db.session.commit()

@app.cli.command("create-admin")
def create_admin():
    """Creates a new admin user interactively."""
    name = click.prompt("Enter admin name", type=str)
    email = click.prompt("Enter admin email", type=str).lower()
    password = click.prompt("Enter password", hide_input=True, confirmation_prompt=True)
    
    existing_user = User.query.filter_by(email=email).first()
    if existing_user:
        print(f"Error: User with email '{email}' already exists.")
        return

    hashed_password = generate_password_hash(password)
    new_admin = User(
        name=name,
        email=email,
        password=hashed_password,
        persona='admin'
    )
    db.session.add(new_admin)
    db.session.commit()
    print(f"Success! Admin user '{name}' ({email}) created.")

print("\n========== REGISTERED ROUTES ==========")
for rule in app.url_map.iter_rules():
    print(rule)
print("=======================================\n")

if __name__ == '__main__':
    print("DATABASE URI:", app.config['SQLALCHEMY_DATABASE_URI'])
    app.run(debug=True)
