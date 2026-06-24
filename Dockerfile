# Immagine base con Python 3.12 (stabile, compatibile con tutte le librerie usate)
FROM python:3.12-slim

# Installa ffmpeg, necessario a pydub per comprimere l'audio in MP3
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia e installa le dipendenze Python prima del codice, per sfruttare la cache Docker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia il resto del progetto (bot.py, eventuale glossario.txt o riferimento.pdf)
COPY . .

# Crea la cartella dove Piper scaricherà il modello vocale al primo avvio
RUN mkdir -p voices

CMD ["python", "bot.py"]
