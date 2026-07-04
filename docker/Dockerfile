# ---- frontend build ----
FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-fund --no-audit
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

CMD ["uvicorn", "mangarr.main:app", "--host", "0.0.0.0", "--port", "6996"]
