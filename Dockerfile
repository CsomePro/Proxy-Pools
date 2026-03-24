FROM ghcr.io/sagernet/sing-box:latest AS singbox

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=singbox /usr/local/bin/sing-box /usr/local/bin/sing-box
COPY app /app/app
COPY templates /app/templates

EXPOSE 9080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9080"]
