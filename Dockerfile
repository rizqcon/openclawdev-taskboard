FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY app/ app/
COPY static/ static/

# Data volume for SQLite
VOLUME /app/data

# Run
EXPOSE 8080
# Disable uvicorn access logs (we have our own middleware logging)
# Set log-level to warning to reduce noise, but keep app logs visible
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info", "--no-access-log"]
