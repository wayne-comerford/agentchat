
import os
import smtplib
from email.mime.text import MIMEText
from typing import Optional

def deliver_password_reset_email(username: str, token: str, reset_url: str) -> bool:
    """Delivers a password reset email using the configured method (log or SMTP)."""
    delivery_method = os.environ.get("AGENTCHAT_RESET_DELIVERY", "log").lower()
    
    if delivery_method == "smtp":
        return _deliver_smtp(username, token, reset_url)
    elif delivery_method == "log":
        return _deliver_log(username, token, reset_url)
    else:
        log(f"WARNING: Unknown AGENTCHAT_RESET_DELIVERY method: {delivery_method}. Falling back to log.")
        return _deliver_log(username, token, reset_url)

def _deliver_log(username: str, token: str, reset_url: str) -> bool:
    """Logs the password reset token to stderr."""
    log(f"PASSWORD_RESET_TOKEN_DELIVERY (log): username={username} token={token} reset_url={reset_url}")
    return True

def _deliver_smtp(username: str, token: str, reset_url: str) -> bool:
    """Delivers the password reset email via SMTP."""
    try:
        smtp_host = os.environ.get("AGENTCHAT_SMTP_HOST")
        smtp_port = int(os.environ.get("AGENTCHAT_SMTP_PORT", 587))
        smtp_user = os.environ.get("AGENTCHAT_SMTP_USER")
        smtp_pass = os.environ.get("AGENTCHAT_SMTP_PASS")
        smtp_from = os.environ.get("AGENTCHAT_SMTP_FROM", "agentchat <noreply@agentchat.com>")
        
        if not all([smtp_host, smtp_user, smtp_pass]):
            log("ERROR: SMTP configuration missing (HOST, USER, PASS). Cannot send email.")
            return False
        
        msg = MIMEText(f"""
        Hello {username},

        You requested a password reset for your agentchat account.
        Please use the following link to reset your password:

        {reset_url}

        This link is valid for a limited time. If you did not request a password reset,
        please ignore this email.

        Sincerely,
        The agentchat Team
        """, "plain")
        msg["Subject"] = "agentchat Password Reset"
        msg["From"] = smtp_from
        msg["To"] = username # Assuming username is an email address
        
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        
        log(f"Password reset email sent to {username} via SMTP.")
        return True
    except Exception as e:
        log(f"ERROR: Failed to send password reset email via SMTP to {username}: {e}")
        return False
