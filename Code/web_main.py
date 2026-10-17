"""Entry point for the web container: `gunicorn web_main:app`.

Kept separate from main.py so the cron container never imports Flask, and so a
syntax error in the UI cannot stop the scheduled email from running.
"""

import logging
import os
from pathlib import Path

import dotenv

# Same probe as email_utils: /root/.env is how the container gets its settings,
# and stat()ing it as a non-root user raises PermissionError rather than
# returning False, which would abort the import.
env_path = Path("/root/.env")
try:
    has_env = env_path.is_file()
except OSError:
    has_env = False
if has_env:
    dotenv.load_dotenv(dotenv_path=str(env_path))
else:
    dotenv.load_dotenv()

import lots_db  # noqa: E402  (after .env, so DB_PATH etc. are final)

from web import create_app  # noqa: E402  (after config)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Create the tables up front so a first run against a fresh volume does not have
# two workers racing to initialise the same schema.
lots_db.init_db()

app = create_app()
