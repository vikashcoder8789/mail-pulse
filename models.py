import secrets
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    campaigns = db.relationship("Campaign", backref="owner", lazy="dynamic")
    settings = db.relationship("Setting", backref="owner", uselist=False)


class Setting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)
    smtp_host = db.Column(db.String(255), default="")
    smtp_port = db.Column(db.Integer, default=587)
    smtp_user = db.Column(db.String(255), default="")
    smtp_password = db.Column(db.String(255), default="")
    smtp_use_tls = db.Column(db.Boolean, default=True)
    from_email = db.Column(db.String(255), default="")
    from_name = db.Column(db.String(255), default="Mail Pulse")
    public_base_url = db.Column(db.String(500), default="")
    physical_address = db.Column(db.String(500), default="")


class Unsubscribed(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("user_id", "email", name="uq_unsub_user_email"),)


class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    subject = db.Column(db.String(300), nullable=False)
    html_body = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(32), default="draft", index=True)
    error_message = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=utcnow)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    recipients = db.relationship(
        "Recipient", backref="campaign", cascade="all, delete-orphan"
    )

    @property
    def total(self):
        return len(self.recipients)

    @property
    def sent_count(self):
        return sum(1 for r in self.recipients if r.sent_at)

    @property
    def failed_count(self):
        return sum(1 for r in self.recipients if r.status == "failed")

    @property
    def skipped_count(self):
        return sum(1 for r in self.recipients if r.status == "skipped")

    @property
    def opened_count(self):
        return sum(1 for r in self.recipients if r.opened_at)

    @property
    def clicked_count(self):
        return sum(1 for r in self.recipients if r.clicked_at)

    @property
    def unsubscribed_count(self):
        return sum(1 for r in self.recipients if r.unsubscribed_at)


class Recipient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=False)
    email = db.Column(db.String(255), nullable=False, index=True)
    name = db.Column(db.String(255), default="")
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    status = db.Column(db.String(32), default="queued")
    error_message = db.Column(db.Text, default="")
    sent_at = db.Column(db.DateTime)
    opened_at = db.Column(db.DateTime)
    clicked_at = db.Column(db.DateTime)
    unsubscribed_at = db.Column(db.DateTime)
    open_count = db.Column(db.Integer, default=0)
    click_count = db.Column(db.Integer, default=0)

    @staticmethod
    def new_token():
        return secrets.token_urlsafe(24)
