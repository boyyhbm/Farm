# syntax=docker/dockerfile:1
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /opt/farm
COPY apps/api/requirements.lock /tmp/requirements.txt
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir -r /tmp/requirements.txt
COPY apps/api /opt/farm
COPY scripts/generate-component-manifest.py /tmp/generate-component-manifest.py
RUN python /tmp/generate-component-manifest.py /opt/farm
RUN useradd --uid 10001 --create-home farm && chown -R farm:farm /opt/farm
USER farm
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
