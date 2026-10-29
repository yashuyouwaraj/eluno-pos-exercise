FROM node:22-bookworm-slim AS frontend-build

WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN npm ci --prefix frontend
COPY frontend ./frontend
COPY eluno_pos/web ./eluno_pos/web
RUN npm run build --prefix frontend

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
COPY --from=frontend-build /app/eluno_pos/web/react-build ./eluno_pos/web/react-build

ENV PYTHONUNBUFFERED=1

CMD ["python", "scripts/start_local.py"]
