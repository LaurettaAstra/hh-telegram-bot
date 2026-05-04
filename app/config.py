"""
Application configuration.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Monitoring interval in minutes (how often to check HH for new vacancies)
MONITOR_INTERVAL_MINUTES = int(os.getenv("MONITOR_INTERVAL_MINUTES", "5"))

# Passive HH re-auth Telegram reminder (expired **applicant** tokens only). Off by default: no
# applicant-only HH features ship yet; vacancy search uses application OAuth only.
HH_REAUTH_NOTIFICATIONS_ENABLED = os.getenv("HH_REAUTH_NOTIFICATIONS_ENABLED", "").strip().lower() in (
    "1",
    "true",
    "yes",
)
# When notifications are enabled, how often to check for users needing optional reconnect (minutes)
HH_REAUTH_CHECK_INTERVAL_MINUTES = int(os.getenv("HH_REAUTH_CHECK_INTERVAL_MINUTES", "15"))

# HH.ru client identification (HH-User-Agent). Required for api.hh.ru; set HH_API_HH_USER_AGENT in .env.
HH_API_HH_USER_AGENT = (os.getenv("HH_API_HH_USER_AGENT") or "").strip()

# HH OAuth2 credentials
HH_CLIENT_ID = os.getenv("HH_CLIENT_ID", "")
HH_CLIENT_SECRET = os.getenv("HH_CLIENT_SECRET", "")
HH_REDIRECT_URI = os.getenv("HH_REDIRECT_URI", "")
