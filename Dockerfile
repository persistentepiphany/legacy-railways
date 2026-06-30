FROM python:3.11-slim

WORKDIR /app

# Install Python deps first (cached layer when only code changes).
COPY pyproject.toml ./
RUN pip install --no-cache-dir fastapi uvicorn pydantic pandas

# App code.
COPY src/ ./src/

# Feed snapshot shipped as a 48MB gzipped tarball of the 9 needed RJFAF805
# files; extracted to /app/data at build time. Smaller upload + image than
# baking the raw 272MB; not in the git repo (data is gitignored).
COPY feed.tar.gz /tmp/feed.tar.gz
RUN mkdir -p /app/data \
    && tar -xzf /tmp/feed.tar.gz -C /app/data \
    && rm /tmp/feed.tar.gz \
    && ls -lh /app/data
ENV FARES_DATA_DIR=/app/data
ENV PYTHONUNBUFFERED=1

# Railway sets PORT; default 8000 for local docker run.
CMD ["sh", "-c", "uvicorn src.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
