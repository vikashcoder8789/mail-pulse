import csv
import io
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from mailer import MAX_RECIPIENTS, get_settings, send_campaign, smtp_ready, space_public_url
from models import Campaign, Recipient, Unsubscribed, User, db, utcnow

load_dotenv()

PIXEL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
    b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
    b"\x00\x00\x02\x02D\x01\x00;"
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SEND_POOL = ThreadPoolExecutor(max_workers=1)


def data_dir():
    if os.path.isdir("/data"):
        return Path("/data")
    path = Path("instance")
    path.mkdir(exist_ok=True)
    return path


def sqlite_uri(db_file: Path) -> str:
    return "sqlite:///" + str(db_file.resolve()).replace("\\", "/")


def retire_legacy_db(db_file: Path):
    if not db_file.exists():
        return
    try:
        con = sqlite3.connect(db_file)
        names = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        con.close()
    except sqlite3.Error:
        return
    if "campaign" in names and "user" not in names:
        db_file.rename(db_file.with_suffix(".db.legacy"))


def create_app():
    app = Flask(__name__)
    secret = os.environ.get("SECRET_KEY", "").strip() or "dev-only-change-me"
    app.config["SECRET_KEY"] = secret
    db_file = data_dir() / "mailpulse.db"
    retire_legacy_db(db_file)
    # Also retire Flask's default instance db if we moved the path
    retire_legacy_db(Path("instance") / "mailpulse.db")
    app.config["SQLALCHEMY_DATABASE_URI"] = sqlite_uri(db_file)
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False}
    }
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    if space_public_url():
        app.config["SESSION_COOKIE_SECURE"] = True
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    with app.app_context():
        db.create_all()

    def current_user():
        user_id = session.get("user_id")
        if not user_id:
            return None
        return db.session.get(User, user_id)

    @app.before_request
    def load_user():
        g.user = current_user()

    @app.context_processor
    def inject_user():
        return {"current_user": g.get("user")}

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    def owned_campaign(campaign_id):
        return Campaign.query.filter_by(id=campaign_id, user_id=g.user.id).first()

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            confirm = request.form.get("confirm") or ""
            if not EMAIL_RE.match(email):
                flash("Enter a valid email address.", "error")
            elif len(password) < 8:
                flash("Password must be at least 8 characters.", "error")
            elif password != confirm:
                flash("Passwords do not match.", "error")
            elif User.query.filter_by(email=email).first():
                flash("That email already has an account. Sign in instead.", "error")
            else:
                user = User(email=email, password_hash=generate_password_hash(password))
                db.session.add(user)
                db.session.commit()
                get_settings(user.id)
                session["user_id"] = user.id
                flash("Account created. Add your SMTP details in Settings before sending.", "ok")
                return redirect(url_for("settings_page"))
        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            user = User.query.filter_by(email=email).first()
            if user and check_password_hash(user.password_hash, password):
                session["user_id"] = user.id
                return redirect(request.args.get("next") or url_for("dashboard"))
            flash("Wrong email or password.", "error")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def dashboard():
        campaigns = (
            Campaign.query.filter_by(user_id=g.user.id)
            .order_by(Campaign.created_at.desc())
            .all()
        )
        totals = {
            "campaigns": len(campaigns),
            "sent": sum(c.sent_count for c in campaigns),
            "opened": sum(c.opened_count for c in campaigns),
            "clicked": sum(c.clicked_count for c in campaigns),
            "unsubscribed": Unsubscribed.query.filter_by(user_id=g.user.id).count(),
        }
        return render_template(
            "dashboard.html",
            campaigns=campaigns,
            totals=totals,
            smtp_ok=smtp_ready(get_settings(g.user.id)),
        )

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings_page():
        settings = get_settings(g.user.id)
        space_url = space_public_url()
        if request.method == "POST":
            settings.smtp_host = request.form.get("smtp_host", "").strip()
            settings.smtp_port = int(request.form.get("smtp_port") or 587)
            settings.smtp_user = request.form.get("smtp_user", "").strip()
            password = request.form.get("smtp_password", "").strip()
            if password:
                settings.smtp_password = password
            settings.smtp_use_tls = request.form.get("smtp_use_tls") == "on"
            settings.from_email = request.form.get("from_email", "").strip()
            settings.from_name = request.form.get("from_name", "").strip()
            if space_url:
                settings.public_base_url = space_url
            else:
                settings.public_base_url = request.form.get("public_base_url", "").strip()
            settings.physical_address = request.form.get("physical_address", "").strip()
            db.session.commit()
            flash("Settings saved.", "ok")
            return redirect(url_for("settings_page"))
        if space_url and not settings.public_base_url:
            settings.public_base_url = space_url
        return render_template("settings.html", settings=settings, space_url=space_url)

    @app.route("/campaigns/new", methods=["GET", "POST"])
    @login_required
    def campaign_new():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            subject = request.form.get("subject", "").strip()
            html_body = request.form.get("html_body", "").strip()
            raw_list = request.form.get("recipients", "")
            file = request.files.get("csv_file")

            rows = parse_recipients(raw_list)
            if file and file.filename:
                try:
                    text = file.read().decode("utf-8-sig")
                except UnicodeDecodeError:
                    flash("CSV must be UTF-8 text.", "error")
                    return render_template("campaign_new.html", max_recipients=MAX_RECIPIENTS)
                rows.extend(parse_csv(text))

            deduped = []
            seen = set()
            for email, person_name in rows:
                key = email.lower()
                if key in seen:
                    continue
                seen.add(key)
                deduped.append((email, person_name))

            if not name or not subject or not html_body:
                flash("Name, subject, and email body are required.", "error")
                return render_template("campaign_new.html", max_recipients=MAX_RECIPIENTS)
            if not deduped:
                flash("Add at least one recipient.", "error")
                return render_template("campaign_new.html", max_recipients=MAX_RECIPIENTS)
            if len(deduped) > MAX_RECIPIENTS:
                flash(f"This app sends at most {MAX_RECIPIENTS} emails per campaign.", "error")
                return render_template("campaign_new.html", max_recipients=MAX_RECIPIENTS)

            campaign = Campaign(
                user_id=g.user.id, name=name, subject=subject, html_body=html_body
            )
            db.session.add(campaign)
            db.session.flush()
            for email, person_name in deduped:
                db.session.add(
                    Recipient(
                        campaign_id=campaign.id,
                        email=email,
                        name=person_name,
                        token=Recipient.new_token(),
                    )
                )
            db.session.commit()
            return redirect(url_for("campaign_detail", campaign_id=campaign.id))
        return render_template("campaign_new.html", max_recipients=MAX_RECIPIENTS)

    @app.route("/campaigns/<int:campaign_id>")
    @login_required
    def campaign_detail(campaign_id):
        campaign = owned_campaign(campaign_id)
        if campaign is None:
            flash("Campaign not found.", "error")
            return redirect(url_for("dashboard"))
        return render_template(
            "campaign_detail.html",
            campaign=campaign,
            smtp_ok=smtp_ready(get_settings(g.user.id)),
        )

    @app.route("/campaigns/<int:campaign_id>/send", methods=["POST"])
    @login_required
    def campaign_send(campaign_id):
        campaign = owned_campaign(campaign_id)
        if campaign is None:
            flash("Campaign not found.", "error")
            return redirect(url_for("dashboard"))
        if campaign.status == "sending":
            flash("This campaign is already sending.", "error")
            return redirect(url_for("campaign_detail", campaign_id=campaign.id))
        if not smtp_ready(get_settings(g.user.id)):
            flash("Configure SMTP in Settings before sending.", "error")
            return redirect(url_for("settings_page"))
        campaign.status = "sending"
        campaign.started_at = utcnow()
        campaign.error_message = ""
        db.session.commit()
        # Small lists send in this request so Render does not drop a background thread.
        if campaign.total <= 15:
            send_campaign(app, campaign.id)
            flash("Finished sending. Check each row for sent or failed.", "ok")
        else:
            SEND_POOL.submit(send_campaign, app, campaign.id)
            flash("Sending started. This page will refresh while it runs.", "ok")
        return redirect(url_for("campaign_detail", campaign_id=campaign.id))

    @app.route("/t/open/<token>.gif")
    def track_open(token):
        token = token.replace(".gif", "")
        recipient = Recipient.query.filter_by(token=token).first()
        if recipient and recipient.sent_at and not recipient.unsubscribed_at:
            recipient.open_count = (recipient.open_count or 0) + 1
            if not recipient.opened_at:
                recipient.opened_at = utcnow()
            db.session.commit()
        return Response(PIXEL_GIF, mimetype="image/gif")

    @app.route("/t/click/<token>")
    def track_click(token):
        target = unquote(request.args.get("u", ""))
        recipient = Recipient.query.filter_by(token=token).first()
        parsed = urlparse(target)
        if parsed.scheme not in ("http", "https"):
            return ("Invalid link", 400)
        if recipient and recipient.sent_at:
            recipient.click_count = (recipient.click_count or 0) + 1
            if not recipient.clicked_at:
                recipient.clicked_at = utcnow()
            if not recipient.opened_at:
                recipient.opened_at = utcnow()
                recipient.open_count = (recipient.open_count or 0) + 1
            db.session.commit()
        return redirect(target)

    @app.route("/unsubscribe/<token>", methods=["GET", "POST"])
    def unsubscribe(token):
        recipient = Recipient.query.filter_by(token=token).first()
        if recipient is None:
            return render_template("unsubscribe.html", done=False, missing=True)
        if request.method == "POST":
            email = recipient.email.lower()
            owner_id = recipient.campaign.user_id
            if not Unsubscribed.query.filter_by(user_id=owner_id, email=email).first():
                db.session.add(Unsubscribed(user_id=owner_id, email=email))
            recipient.unsubscribed_at = utcnow()
            db.session.commit()
            return render_template("unsubscribe.html", done=True, missing=False)
        return render_template(
            "unsubscribe.html", done=False, missing=False, email=recipient.email
        )

    return app


def parse_recipients(raw):
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if "," in line:
            email, name = [part.strip() for part in line.split(",", 1)]
        else:
            email, name = line, ""
        if EMAIL_RE.match(email):
            rows.append((email, name))
    return rows


def parse_csv(text):
    rows = []
    reader = csv.reader(io.StringIO(text))
    for parts in reader:
        if not parts:
            continue
        email = parts[0].strip()
        if email.lower() in ("email", "e-mail"):
            continue
        name = parts[1].strip() if len(parts) > 1 else ""
        if EMAIL_RE.match(email):
            rows.append((email, name))
    return rows


app = create_app()

if __name__ == "__main__":
    on_spaces = bool(os.environ.get("SPACE_ID") or os.environ.get("SPACE_HOST"))
    port = int(os.environ.get("PORT", 7860 if on_spaces else 5000))
    app.run(host="0.0.0.0", port=port, debug=not on_spaces)
