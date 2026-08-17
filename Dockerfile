# Two stages: build the React bundle with Node, then run it from Python.
# FastAPI serves frontend/dist itself, so the whole app is one process on one
# port — no separate static host, no CORS, and the WebSocket rooms share the
# origin the pages are served from.

FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM python:3.11-slim
WORKDIR /app

# The scheduler runs on the league's clock, not the server's, but a container
# with real timezone data keeps logs and America/Chicago arithmetic honest.
ENV PYTHONUNBUFFERED=1 TZ=America/Chicago
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
# main.py resolves the bundle at <repo root>/frontend/dist, so the built assets
# have to land in that same shape inside the image.
COPY --from=frontend /build/dist frontend/dist

COPY docker-entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/entrypoint

WORKDIR /app/backend
EXPOSE 8000
ENTRYPOINT ["entrypoint"]
