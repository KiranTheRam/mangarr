# ---- frontend build ----
FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY frontend/ ./
# vite.config outDir points at ../backend/static; keep it inside /build
RUN npm run build -- --outDir /build/static

# ---- backend runtime ----
FROM python:3.12-slim
WORKDIR /app

COPY backend/pyproject.toml ./
COPY backend/mangarr ./mangarr
RUN pip install --no-cache-dir .

COPY --from=frontend /build/static ./static

ENV MANGARR_DATA_DIR=/config \
    MANGARR_HOST=0.0.0.0 \
    MANGARR_PORT=6996

VOLUME ["/config", "/manga"]
EXPOSE 6996
# Probe the unauthenticated /ping route with the stdlib (the slim image has no
# curl/wget). Follows MANGARR_PORT, and MANGARR_HOST when it names one address;
# the wildcard binds are probed on loopback. Proxies are bypassed and output is
# discarded, so nothing lands in `docker inspect`.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -I -B -c "import os, urllib.request as u; \
        h = os.environ.get('MANGARR_HOST') or '0.0.0.0'; \
        h = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(h, h); \
        h = '[' + h + ']' if ':' in h else h; \
        p = os.environ.get('MANGARR_PORT') or '6996'; \
        u.build_opener(u.ProxyHandler({})).open('http://' + h + ':' + p + '/ping', timeout=4)" \
        >/dev/null 2>&1 || exit 1

# mangarr.main's __main__ entry runs uvicorn with config.host/config.port,
# so MANGARR_HOST / MANGARR_PORT (defaults above) take effect.
CMD ["python", "-m", "mangarr.main"]
