# This is the Dockerfile for the job
# the CMD is not executed as it runs from the scheduler
# in docker-compose, the command is set to "sleep infinity"
# If run directly, it will just process the files stored in /data/incoming without ftp download
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/ ./scripts/

CMD ["python", "-m", "scripts.sdat_processor"]