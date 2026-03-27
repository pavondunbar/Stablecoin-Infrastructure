FROM python:3.12-slim

# Security: run as non-root
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Copy shared library first (cache layer)
COPY shared/ /app/shared/

# Copy service-specific requirements and install
COPY services/fx-settlement/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy service code
COPY services/fx-settlement/main.py /app/main.py

RUN chown -R appuser:appuser /app
USER appuser

CMD ["python", "main.py"]
