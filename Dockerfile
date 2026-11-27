FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PHOTOWALL_DATA_DIR=/data
WORKDIR /app
COPY deploy/requirements-container.txt /tmp/requirements-container.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements-container.txt \
    && install -d -o 10001 -g 10001 /data

COPY photowall.py ./
COPY static/ static/
COPY templates/ templates/

USER 10001:10001
EXPOSE 8080
CMD ["gunicorn", "--bind", "[::]:8080", "--workers", "1", "--no-control-socket", "photowall:app"]
