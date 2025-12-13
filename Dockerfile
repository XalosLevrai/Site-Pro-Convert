# Utiliser une image de base Python 3.11 ou 3.12
FROM python:3.11-slim

# Installer les dépendances systèmes (dont ffmpeg)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    # Si vous utilisez Pillow, vous pourriez avoir besoin de libjpeg-dev
    libjpeg-dev \
    && rm -rf /var/lib/apt/lists/*

# Définir le répertoire de travail
WORKDIR /usr/src/app

# Copier les fichiers de dépendances et installer les paquets Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copier le reste du code
COPY . .

# Définir la commande de démarrage Gunicorn
CMD gunicorn app:app