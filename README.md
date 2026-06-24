# Solver Bot

Bot Telegram che riceve la foto di un problema di lavoro e risponde con un vocale che detta la soluzione, lentamente e a frasi brevi, così puoi scriverla mentre ascolti.

## Setup

1. **Crea l'ambiente virtuale e installa le dipendenze**

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

2. **Installa ffmpeg** (necessario a pydub per unire gli audio con le pause)

- macOS: `brew install ffmpeg`
- Ubuntu/Debian: `sudo apt install ffmpeg`
- Windows: scarica da https://ffmpeg.org/download.html e aggiungi al PATH

3. **Configura le chiavi**

Copia `.env.example` in `.env` e inserisci le tue chiavi:

```bash
cp .env.example .env
```

Poi apri `.env` e sostituisci i valori con:
- `TELEGRAM_TOKEN`: quello ricevuto da @BotFather
- `ANTHROPIC_API_KEY`: da console.anthropic.com
- `ELEVENLABS_API_KEY`: da elevenlabs.io
- `ELEVENLABS_VOICE_ID` (opzionale): se vuoi una voce diversa da quella di default, prendi il voice_id da ElevenLabs > Voice Library

4. **Avvia il bot**

```bash
python bot.py
```

Se vedi `Bot avviato. In ascolto...` nel terminale, apri Telegram, vai sul tuo bot e mandagli una foto.

## Come funziona

1. Mandi una foto al bot (es. screenshot di un'email, di un errore, di una richiesta)
2. Claude legge la foto e scrive la soluzione
3. Claude riformula la soluzione in stile "dettato" (frasi brevi, pause)
4. ElevenLabs genera l'audio, rallentato e con pause reali tra le frasi
5. Il bot ti manda il vocale + il testo scritto della soluzione

## Personalizzazioni rapide

- **Voce più lenta/veloce**: modifica `speed` in `VoiceSettings` dentro `bot.py` (valori tipici 0.7–1.2)
- **Pause più lunghe/corte**: modifica `duration` in `AudioSegment.silent(duration=900)` (in millisecondi)
- **Stile della soluzione**: modifica `SOLVER_PROMPT` in `bot.py` per orientare Claude su un certo tipo di problemi (es. solo email, solo errori tecnici)
- **Cambiare voce**: vai su ElevenLabs > Voice Library, copia il voice_id e mettilo in `ELEVENLABS_VOICE_ID` nel `.env`

## Note sui costi

- Claude: pagamento a consumo per token (immagine + testo in input, testo in output). Per un uso personale con foto occasionali, il costo è contenuto (centesimi a richiesta).
- ElevenLabs: ha un piano gratuito con quota limitata di caratteri al mese; oltre quella serve un piano a pagamento.
- Telegram: gratuito, nessun costo per l'uso del bot.

## Prossimi passi possibili

- Deploy su un servizio cloud (Railway, Render) per non dover tenere il bot acceso sul tuo computer
- Aggiungere comandi vocali o tap per "ripeti l'ultima frase" durante l'ascolto
- Salvare uno storico delle soluzioni generate
