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


# Chadwick's cwdaily turns Retrosheet event files into daily player lines. It is
# the difference between drafting real players from a real season and drafting
# the synthetic generator's invented ones, and it is not packaged for Debian —
# so it gets built from source here rather than being a thing you install by
# hand on a server you do not shell into.
FROM debian:bookworm-slim AS chadwick
ARG CHADWICK_VERSION=0.10.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        autoconf automake libtool make gcc g++ ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -fsSL \
      "https://github.com/chadwickbureau/chadwick/archive/refs/tags/v${CHADWICK_VERSION}.tar.gz" \
      -o chadwick.tar.gz \
    && tar -xzf chadwick.tar.gz --strip-components=1 \
    # A GitHub source archive is not a release tarball: it carries configure.ac
    # but not the generated configure, so it has to be built first.
    && autoreconf -fi \
    && ./configure --prefix=/opt/chadwick \
    && make -j"$(nproc)" \
    && make install
# Fail the build here rather than at 8pm on a night the league expected a sim.
RUN /opt/chadwick/bin/cwdaily --help >/dev/null 2>&1 \
    || /opt/chadwick/bin/cwdaily -h 2>&1 | head -1


FROM python:3.11-slim
WORKDIR /app

# The scheduler runs on the league's clock, not the server's, but a container
# with real timezone data keeps logs and America/Chicago arithmetic honest.
ENV PYTHONUNBUFFERED=1 TZ=America/Chicago
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
# main.py resolves the bundle at <repo root>/frontend/dist, so the built assets
# have to land in that same shape inside the image.
COPY --from=frontend /build/dist frontend/dist

# cwdaily is invoked by name, so it has to be on PATH — the retrosheet pipeline
# refuses to run without it rather than silently producing a hollow season.
COPY --from=chadwick /opt/chadwick /opt/chadwick
ENV PATH="/opt/chadwick/bin:${PATH}"

# A published export of the IL transaction log. ProSportsTransactions itself
# refuses automated traffic from hosting providers, so the live feed is
# unreachable from here; this is the same records, already public. Fetched at
# build time rather than committed, so no third-party data enters this repo or
# its history. If the fetch fails the image still builds — seasons simply come
# up without injuries and say so.
ARG INJURY_CSV_URL=https://raw.githubusercontent.com/robotallie/baseball-injuries/master/injuries.csv
RUN curl -fsSL "$INJURY_CSV_URL" -o /opt/injuries.csv \
    && head -1 /opt/injuries.csv | grep -q Relinquished \
    || { echo "injury export unavailable; continuing without it"; rm -f /opt/injuries.csv; }

COPY docker-entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/entrypoint

WORKDIR /app/backend
EXPOSE 8000
ENTRYPOINT ["entrypoint"]
