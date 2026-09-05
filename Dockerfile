# Deployment image. The compose setup builds backend/Dockerfile with backend/
# as its context; a platform that builds from the repository root needs this
# one, which copies the same tree plus the configs the engine reads at runtime.
FROM python:3.11-slim

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ /app/
COPY configs/ /configs/
COPY scripts/ /scripts/
COPY data/demo_seed.sql.gz /data/demo_seed.sql.gz

# Signal Lab renders these at request time rather than embedding numbers in its
# template, so the image needs them. Both are small, committed text.
COPY results/ /results/
COPY ops/ /ops/

# Thresholds resolve to parents[3]/configs from app/engine/pipeline.py, which
# is /configs here. Kept as a copy rather than a mount so the image is
# self-contained.
ENV PYTHONUNBUFFERED=1

CMD ["/bin/sh", "/scripts/boot.sh"]
