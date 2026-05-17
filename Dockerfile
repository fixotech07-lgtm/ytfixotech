FROM python:3.11-slim

# Instalo ffmpeg dhe dependencies te tjera
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Verifiko qe ffmpeg eshte instaluar
RUN ffmpeg -version

WORKDIR /app

# Instalo dependencies Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopjo gjithcka tjeter
COPY . .

# Krijo folderat e nevojshem
RUN mkdir -p data downloads downloads/jobs

# Railway perdor PORT environment variable
ENV PORT=8080
EXPOSE 8080

# Start me gunicorn
CMD gunicorn -w 2 -b 0.0.0.0:$PORT --timeout 600 --access-logfile - --error-logfile - app:app
