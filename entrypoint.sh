#!/bin/sh
set -e

# CRON_TIME: crontab schedule (default: every day at 16:00 UTC)
# The settings page can override this in the database, which wins over the
# environment. Read it through lots_db so the UI and the schedule cannot
# disagree. Only read at startup, so changing it needs a container restart -
# which is what the settings page tells the user. Any failure falls through to
# the environment value rather than leaving cron with no schedule at all.
CRON_TIME_DB=$(python3 -c 'import lots_db; print(lots_db.runtime_setting("CRON_TIME", "") or "")' 2>/dev/null || true)
if [ -n "$CRON_TIME_DB" ]; then
	CRON_TIME="$CRON_TIME_DB"
fi
CRON_TIME="${CRON_TIME:-0 16 * * *}"
# CRON_CMD: command to execute (default: run main.py non-interactively)
CRON_CMD="${CRON_CMD:-cd /app && python main.py --no-show}"

CRON_FILE="/etc/cron.d/app_cron"

echo "# Generated crontab" > "$CRON_FILE"

# Export current environment into the crontab so cron jobs inherit container env vars.
# Cron accepts VAR=value lines placed before job lines in the crontab file.
echo "# Environment variables for cron" >> "$CRON_FILE"
env | while IFS= read -r line; do
	# Skip empty lines
	[ -z "$line" ] && continue
	printf '%s\n' "$line" >> "$CRON_FILE"
done

# tee rather than a plain redirect: cron gives the job the container's stdout,
# so without this the daily run and any traceback are only visible inside the
# container and `docker compose logs` looks empty. tee keeps the file and also
# puts the run where `docker logs` can see it.
echo "$CRON_TIME root $CRON_CMD 2>&1 | tee -a /var/log/app_cron.log" >> "$CRON_FILE"
chmod 0644 "$CRON_FILE"

# Install crontab
crontab "$CRON_FILE" || true

# Ensure logfile exists
touch /var/log/app_cron.log

echo "Starting cron with schedule: $CRON_TIME"

# If RUN_ONCE=1 is set, run the job once immediately (useful for testing).
if [ "${RUN_ONCE:-0}" = "1" ]; then
	echo "RUN_ONCE=1: executing job once now -> $CRON_CMD"
	# Run the command in a subshell so it respects the same working dir and env
	sh -c "$CRON_CMD"
fi

# Run cron in foreground
cron -f
