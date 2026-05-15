FROM python:3.11-slim

WORKDIR /app

# System deps for Pillow and pandas
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps (cached layer, rebuilt only if requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY agent.py storage_data.py telegram_bot.py tools.py utilities.py ./

# Persistent volume mount point: SQLite DB + temp files
RUN mkdir -p /data /data/temp

# Override default paths to point to the volume
ENV AGNO_MEMORY_DB_FILE=/data/memory.sqllite
ENV TRANSIT_TEMP_DIR=/data/temp
ENV IMAGE_TEMP_DIR=/data/temp

# In container the server must listen on all interfaces
ENV AGENT_OS_HOST=0.0.0.0

EXPOSE 7777

# Use uvicorn directly; lifespan hooks (polling loop, scheduler) start via app_lifespan
CMD ["uvicorn", "telegram_bot:app", "--host", "0.0.0.0", "--port", "7777", "--lifespan", "on"]
