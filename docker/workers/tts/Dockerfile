FROM python:3.11-slim

WORKDIR /app

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsndfile1 \
    espeak-ng \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Descargar modelos Kokoro al build time
RUN python -c "from kokoro import KPipeline; KPipeline(lang_code='e')" || true

COPY . .

EXPOSE 8081

CMD ["python", "worker.py"]
