"""
Application configuration.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Monitoring interval in minutes (how often to check HH for new vacancies)
MONITOR_INTERVAL_MINUTES = int(os.getenv("MONITOR_INTERVAL_MINUTES", "5"))

# HH.ru client identification (HH-User-Agent). Override with HH_API_HH_USER_AGENT if needed.
HH_API_HH_USER_AGENT = os.getenv(
    "HH_API_HH_USER_AGENT",
    "LaurettaAstra HH Telegram Bot/1.0 (romanova.mekcorp@gmail.com)",
)
