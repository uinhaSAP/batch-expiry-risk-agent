FROM python:3.13-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/ ./app/

# Make app/ importable as a package root (peer-level imports inside app/)
ENV PYTHONPATH=/app/app

ENV IBD_TESTING=1

EXPOSE 5000

CMD ["python", "app/main.py"]
