FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY entrypoint.py simple_email_mcp.py ./

RUN useradd -m -u 1000 appuser && \
    mkdir -p /etc/email-mcp && \
    chown appuser:appuser /etc/email-mcp

USER appuser

EXPOSE 8000

CMD ["python", "entrypoint.py"]
