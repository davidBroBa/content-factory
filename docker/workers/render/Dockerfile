FROM python:3.11-slim

WORKDIR /app

# Instalar FFmpeg y dependencias del sistema
RUN apt-get update && apt-get install -y \
    ffmpeg \
    imagemagick \
    curl \
    wget \
    fonts-liberation \
    fonts-noto \
    fonts-noto-cjk \
    fontconfig \
    && fc-cache -f -v \
    && rm -rf /var/lib/apt/lists/*

# Descargar fuentes para subtítulos
RUN mkdir -p /usr/share/fonts/factory && \
    wget -O /usr/share/fonts/factory/Montserrat-Bold.ttf \
    "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf" || \
    cp /usr/share/fonts/liberation/LiberationSans-Bold.ttf /usr/share/fonts/factory/ && \
    fc-cache -f -v

# Verificar instalación de FFmpeg
RUN ffmpeg -version | head -3

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["python", "worker.py"]
