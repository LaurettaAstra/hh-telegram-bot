"""
Application configuration.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Monitoring interval in minutes (how often to check HH for new vacancies)
MONITOR_INTERVAL_MINUTES = int(os.getenv("MONITOR_INTERVAL_MINUTES", "5"))
