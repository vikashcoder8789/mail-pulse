import os
import smtplib
import ssl
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from models import Campaign, Recipient, Setting, Unsubscribed, db, utcnow

MAX_RECIPIENTS = 200
SEND_DELAY_SECONDS = 0.15


def space_public_url():
    render = (os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if render:
        return render
    host = (os.environ.get("SPACE_HOST") or "").strip()
    if host:
        return f"https://{host}"
    return ""


def get_settings(user_id):
    settings = Setting.query.filter_by(user_id=user_id).first()
    if settings is None:
        settings = Setting(user_id=user_id, public_base_url=space_public_url())
        db.session.add(settings)
        db.session.commit()
    return settings


def normalize_base_url(url):
    space = space_public_url()
    if space:
        return space
    url = (url or "").strip().rstrip("/")
    if not url:
        return "http://127.0.0.1:5000"
    return url


def is_trackable_href(href):
    if not href:
        return False
    lowered = href.strip().lower()
    if lowered.startswith(("#", "mailto:", "tel:", "javascript:")):
        return False
    parsed = urlparse(href)
    return parsed.scheme in ("http", "https")


def personalize(html, recipient):
    name = recipient.name or recipient.email.split("@")[0]
    return (
        html.replace("{{name}}", escape(name)).replace("{{email}}", escape(recipient.email))
    )


def build_html_email(campaign, recipient, settings):
    base = normalize_base_url(settings.public_base_url)
    html = personalize(campaign.html_body, recipient)
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if is_trackable_href(href):
            tag["href"] = f"{base}/t/click/{recipient.token}?u={quote(href, safe='')}"

    unsub_url = f"{base}/unsubscribe/{recipient.token}"
    pixel_url = f"{base}/t/open/{recipient.token}.gif"
    address = settings.physical_address or "Add your mailing address in Settings."

    footer = BeautifulSoup(
        f"""
        <div style="margin-top:32px;padding-top:16px;border-top:1px solid #e5e2dc;font-family:Georgia,serif;font-size:12px;color:#6b6560;line-height:1.5;">
          <p>You received this because you are on our list.</p>
          <p>{escape(address)}</p>
          <p><a href="{unsub_url}" style="color:#6b6560;">Unsubscribe</a></p>
          <img src="{pixel_url}" width="1" height="1" alt="" style="display:block;border:0;width:1px;height:1px;" />
        </div>
        """,
        "html.parser",
    )

    if soup.body:
        soup.body.append(footer)
    else:
        soup.append(footer)

    return str(soup)


def send_one(settings, campaign, recipient):
    html = build_html_email(campaign, recipient, settings)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = campaign.subject
    msg["From"] = formataddr((settings.from_name or "Mail Pulse", settings.from_email))
    msg["To"] = recipient.email
    msg["List-Unsubscribe"] = (
        f"<{normalize_base_url(settings.public_base_url)}/unsubscribe/{recipient.token}>"
    )
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    port = int(settings.smtp_port)
    if port == 465:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(settings.smtp_host, port, timeout=30, context=context) as smtp:
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.sendmail(settings.from_email, [recipient.email], msg.as_string())
        return
    with smtplib.SMTP(settings.smtp_host, port, timeout=30) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.sendmail(settings.from_email, [recipient.email], msg.as_string())


def smtp_ready(settings):
    return bool(
        settings.smtp_host
        and settings.smtp_port
        and settings.from_email
        and settings.smtp_user
        and settings.smtp_password
    )


def send_campaign(app, campaign_id):
    with app.app_context():
        campaign = db.session.get(Campaign, campaign_id)
        if campaign is None:
            return
        try:
            settings = get_settings(campaign.user_id)
            if not smtp_ready(settings):
                campaign.status = "failed"
                campaign.error_message = "SMTP is not configured. Add host, login, and from address in Settings."
                campaign.finished_at = utcnow()
                db.session.commit()
                return

            campaign.status = "sending"
            campaign.started_at = campaign.started_at or utcnow()
            campaign.error_message = ""
            db.session.commit()

            recipients = list(
                Recipient.query.filter_by(campaign_id=campaign.id).order_by(Recipient.id).all()
            )
            for recipient in recipients:
                if recipient.status == "sent":
                    continue
                already = Unsubscribed.query.filter_by(
                    user_id=campaign.user_id, email=recipient.email.lower()
                ).first()
                if already:
                    recipient.status = "skipped"
                    recipient.error_message = "Already unsubscribed"
                    db.session.commit()
                    continue
                try:
                    send_one(settings, campaign, recipient)
                    recipient.status = "sent"
                    recipient.sent_at = utcnow()
                    recipient.error_message = ""
                except Exception as exc:
                    recipient.status = "failed"
                    recipient.error_message = str(exc)
                db.session.commit()
                time.sleep(SEND_DELAY_SECONDS)

            campaign.status = "sent"
            campaign.finished_at = utcnow()
            db.session.commit()
        except Exception as exc:
            campaign = db.session.get(Campaign, campaign_id)
            if campaign is not None:
                campaign.status = "failed"
                campaign.error_message = str(exc)
                campaign.finished_at = utcnow()
                db.session.commit()
