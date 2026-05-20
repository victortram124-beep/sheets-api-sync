FROM python:3.11-slim

WORKDIR /app

# Install deps first so they cache when only app code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: loop sync every 10 minutes. Override with `docker run ... sync sync --loop 60`.
ENTRYPOINT ["python", "sync.py"]
CMD ["sync", "--loop", "600"]
