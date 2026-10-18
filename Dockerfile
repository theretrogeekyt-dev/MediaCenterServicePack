# ==========================================
# Build Stage: Compile Python Dependencies
# ==========================================
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ==========================================
# Runtime Stage: Minimal Lightweight Image
# ==========================================
FROM python:3.12-slim AS runtime

LABEL maintainer="theretrogeekyt-dev" \
      description="MediaCenterServicePack - Service Pack & Backend for Windows Media Center" \
      version="1.0.0"

# Install runtime utilities: gosu (PUID/PGID execution), curl (healthcheck), tzdata
RUN apt-get update && apt-get install -y --no-install-recommends \
    gosu \
    curl \
    tzdata \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Copy application source code and entrypoint
COPY src/ /app/src/
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

RUN chmod +x /app/docker-entrypoint.sh && \
    mkdir -p /config

# Default environment configuration
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_DIR=/config \
    PUID=1000 \
    PGID=1000 \
    TZ=UTC \
    PORT=8080

EXPOSE 8080

# Health check endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8080"]

