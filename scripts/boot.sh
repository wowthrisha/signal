set -e

echo "boot: applying schema"
python -m app.db

echo "boot: seeding demo slice if empty"
python /scripts/seed_demo.py --load --path /data/demo_seed.sql.gz || \
  echo "boot: seed skipped or failed, continuing"

echo "boot: starting uvicorn on ${PORT:-8000}"
# --reload only when asked. Compose sets RELOAD=1 for local development; the
# deployment leaves it unset.
if [ -n "${RELOAD:-}" ]; then
  exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --reload
fi
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
