"""email_utils.py — verification email sending via Gmail SMTP."""

import secrets
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def generate_verification_token() -> str:
    return secrets.token_urlsafe(32)


def send_verification_email(
    to_email: str,
    username: str,
    token: str,
    app_url: str,
    from_email: str,
    email_password: str,
) -> bool:
    verify_url = f"{app_url.rstrip('/')}/auth/verify?token={token}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Verify your RouteX account"
    msg["From"] = f"RouteX <{from_email}>"
    msg["To"] = to_email

    text_body = (
        f"Hi {username},\n\n"
        f"Click the link below to verify your RouteX account:\n"
        f"{verify_url}\n\n"
        f"This link expires in 24 hours.\n\n"
        f"If you did not register, ignore this email."
    )
    html_body = f"""<html><body>
<div style="font-family:sans-serif;max-width:480px;margin:40px auto;padding:24px;
            border:1px solid #e5e7eb;border-radius:12px">
  <div style="text-align:center;margin-bottom:20px">
    <span style="font-size:36px">&#128205;</span>
    <h2 style="color:#1a1a2e;margin:8px 0 4px">RouteX</h2>
    <p style="color:#6b7280;font-size:13px;margin:0">Safe paths ahead</p>
  </div>
  <h3 style="color:#1a1a2e">Verify your email address</h3>
  <p style="color:#374151">Hi <b>{username}</b>,</p>
  <p style="color:#374151">Click the button below to verify your email and activate your account:</p>
  <div style="text-align:center;margin:28px 0">
    <a href="{verify_url}"
       style="display:inline-block;padding:14px 32px;background:#1a1a2e;color:#fff;
              text-decoration:none;border-radius:8px;font-weight:600;font-size:15px">
      Verify Email
    </a>
  </div>
  <p style="color:#6b7280;font-size:12px">
    Or copy this link:<br>
    <a href="{verify_url}" style="color:#2563eb;word-break:break-all">{verify_url}</a>
  </p>
  <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0">
  <p style="color:#9ca3af;font-size:11px;text-align:center">
    This link expires in 24 hours. If you did not register for RouteX, ignore this email.
  </p>
</div>
</body></html>"""

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as server:
            server.starttls()
            server.login(from_email, email_password)
            server.sendmail(from_email, to_email, msg.as_string())
        return True
    except Exception as exc:
        print(f"[email] Failed to send to {to_email}: {exc}")
        return False
