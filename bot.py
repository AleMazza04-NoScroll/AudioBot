"""
Solver Bot - Assistente che risolve problemi da foto e detta la soluzione a voce.

Pipeline:
1. Utente manda una foto su Telegram
2. Claude (vision) legge la foto e genera l'esposizione per ciascuna domanda
3. Piper TTS (locale, gratuito) genera l'audio a velocità naturale, con pause
   calibrate sulla punteggiatura reale del testo per dare tempo di scrivere
4. Il bot rispedisce testo + vocale su Telegram, separati per domanda
"""

import os
import logging
import base64
import wave
from io import BytesIO

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic
from piper import PiperVoice, SynthesisConfig

# --- Setup ---

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120.0)

# Materiale di riferimento di settore: caricato una sola volta all'avvio,
# e incluso automaticamente in ogni richiesta a Claude.
# Supporta sia un file di testo (glossario.txt) sia un PDF (riferimento.pdf).
GLOSSARY_PATH = os.getenv("GLOSSARY_PATH", "glossario.txt")
REFERENCE_PDF_PATH = os.getenv("REFERENCE_PDF_PATH", "riferimento.pdf")


def load_text_glossary() -> str:
    """Legge il file di testo del glossario, se presente."""
    if os.path.exists(GLOSSARY_PATH):
        with open(GLOSSARY_PATH, "r", encoding="utf-8") as f:
            content = f.read().strip()
        logger.info(f"Glossario testuale caricato ({len(content)} caratteri).")
        return content
    return ""


def load_pdf_reference() -> str:
    """Estrae il testo dal PDF di riferimento, se presente."""
    if os.path.exists(REFERENCE_PDF_PATH):
        from pypdf import PdfReader

        reader = PdfReader(REFERENCE_PDF_PATH)
        pages_text = []
        for page in reader.pages:
            pages_text.append(page.extract_text() or "")
        content = "\n".join(pages_text).strip()
        logger.info(
            f"PDF di riferimento caricato: {len(reader.pages)} pagine, "
            f"{len(content)} caratteri estratti."
        )
        return content
    return ""


# Unione di entrambe le fonti (se presenti entrambe, vengono concatenate)
_glossary_part = load_text_glossary()
_pdf_part = load_pdf_reference()
GLOSSARY_TEXT = "\n\n".join(part for part in [_glossary_part, _pdf_part] if part)

if not GLOSSARY_TEXT:
    logger.info("Nessun materiale di riferimento trovato (né glossario.txt né riferimento.pdf).")

# Percorso del modello vocale Piper (italiano).
# Se il file non esiste, viene scaricato automaticamente al primo avvio.
PIPER_MODEL_PATH = os.getenv("PIPER_MODEL_PATH", "voices/it_IT-paola-medium.onnx")
PIPER_MODEL_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
    "it/it_IT/paola/medium/it_IT-paola-medium.onnx"
)
PIPER_CONFIG_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
    "it/it_IT/paola/medium/it_IT-paola-medium.onnx.json"
)


def ensure_piper_model_downloaded():
    """Scarica il modello vocale italiano se non è già presente in locale."""
    import urllib.request

    os.makedirs(os.path.dirname(PIPER_MODEL_PATH), exist_ok=True)
    config_path = PIPER_MODEL_PATH + ".json"

    if not os.path.exists(PIPER_MODEL_PATH):
        logger.info("Modello vocale non trovato, lo scarico (una sola volta, ~60MB)...")
        urllib.request.urlretrieve(PIPER_MODEL_URL, PIPER_MODEL_PATH)
        logger.info("Modello scaricato.")

    if not os.path.exists(config_path):
        urllib.request.urlretrieve(PIPER_CONFIG_URL, config_path)


ensure_piper_model_downloaded()

# length_scale: quanto è lento il parlato. 1.0 = velocità/pronuncia naturale.
# Il tempo per scrivere viene dato dalle pause sulla punteggiatura (vedi sotto), non rallentando la voce.
PIPER_LENGTH_SCALE = float(os.getenv("PIPER_LENGTH_SCALE", "1.0"))

# Caricato una sola volta all'avvio del bot (evita di ricaricare il modello ad ogni messaggio)
piper_voice = PiperVoice.load(PIPER_MODEL_PATH)

# Configurazione della sintesi: length_scale controlla la velocità (1.0 = normale, più alto = più lento)
piper_syn_config = SynthesisConfig(length_scale=PIPER_LENGTH_SCALE)


# --- Step 1+2: Claude legge la foto e risolve il problema ---

SOLVER_PROMPT_BASE = """Sei un assistente che risponde a problemi di lavoro a partire da una foto (email, messaggio, errore tecnico, richiesta, documento, ecc.), nello stile di una risposta scritta a una domanda aperta d'esame.

Per ciascuna domanda o quesito distinto posto dalla foto (possono essere una, due o fino a tre domande separate nella stessa immagine), produci un'esposizione in prosa continua e organizzata, che tratti l'argomento con padronanza: niente elenchi puntati, niente intestazioni, niente formule di cortesia o meta-commenti. Il testo deve essere un'argomentazione scritta coerente, con transizioni logiche tra i punti. Usa terminologia tecnica precisa. Condensa i concetti rilevanti senza divagazioni o ripetizioni superflue. Usa una punteggiatura chiara e ben distribuita (virgole, punti, a capo tra un concetto e l'altro), perché il testo verrà letto da un sintetizzatore vocale che si basa sulla punteggiatura per calcolare le pause.

Se il materiale di riferimento fornito è rilevante, integralo nell'esposizione in modo naturale, senza citarlo esplicitamente come fonte separata.

Se ci sono più domande, dopo aver completato l'esposizione di una domanda, scrivi [FINE_DOMANDA] su una riga propria, prima di iniziare la domanda successiva. Non scrivere [FINE_DOMANDA] dopo l'ultima domanda.

Rispondi SOLO con l'esposizione (o le esposizioni separate da [FINE_DOMANDA] se sono più domande), senza introduzioni come "Ecco la risposta" e senza meta-commenti. Il testo deve essere quello di una risposta scritta d'esame, pronta per essere letta o dettata."""

GLOSSARY_SECTION_TEMPLATE = """

Usa il seguente materiale di riferimento come base per la tua risposta, quando rilevante:
---
{glossary}
---"""


def build_solver_prompt() -> str:
    """Costruisce il prompt finale, includendo il glossario se presente."""
    if GLOSSARY_TEXT:
        return SOLVER_PROMPT_BASE + GLOSSARY_SECTION_TEMPLATE.format(glossary=GLOSSARY_TEXT)
    return SOLVER_PROMPT_BASE


def parse_questions(raw_solution: str):
    """Divide il testo grezzo restituito da Claude in una lista di esposizioni,
    una per ciascuna domanda rilevata (separate da [FINE_DOMANDA])."""
    return [b.strip() for b in raw_solution.split("[FINE_DOMANDA]") if b.strip()]


def solve_problem_from_image(image_base64: str, media_type: str) -> str:
    """
    Manda la foto a Claude e ottiene l'esposizione per ciascuna domanda rilevata (fino a 3).

    Ottimizzazione: il blocco di istruzioni + materiale di riferimento (es. il PDF)
    è marcato con cache_control, così le richieste ravvicinate nel tempo (entro 5 minuti)
    pagano un decimo del prezzo normale per quella parte del contesto, invece di
    pagarla per intero a ogni foto.
    """
    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": build_solver_prompt(),
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_base64,
                        },
                    },
                ],
            }
        ],
    )

    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    logger.info(
        f"Token: input={usage.input_tokens}, cache_letta={cache_read}, "
        f"cache_scritta={cache_write}, output={usage.output_tokens} "
        f"({'CACHE USATA - risparmio attivo' if cache_read > 0 else 'nessuna cache disponibile, prima chiamata o scaduta'})"
    )

    return response.content[0].text


# --- Step 3: testo -> audio con pause calibrate sulla punteggiatura (Piper, locale e gratuito) ---

# Quante volte ripetere l'intera esposizione nello stesso file audio.
# Con le pause lunghe calibrate sulla punteggiatura, 1 volta è sufficiente.
AUDIO_REPEAT_COUNT = int(os.getenv("AUDIO_REPEAT_COUNT", "1"))


def _split_by_punctuation(text: str):
    """
    Spezza il testo in segmenti, ciascuno associato al tipo di punteggiatura
    che lo separa dal segmento successivo: 'virgola', 'punto', 'a_capo', o None (ultimo segmento).
    Esempio: "Ciao, mondo. Bene" -> [("Ciao", "virgola"), ("mondo", "punto"), ("Bene", None)]
    """
    segments = []
    current = ""
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\n":
            if current.strip():
                segments.append((current.strip(), "a_capo"))
                current = ""
            else:
                if segments and segments[-1][1] in ("punto", "a_capo"):
                    segments[-1] = (segments[-1][0], "a_capo")
            i += 1
            continue
        if char == ",":
            current += char
            if current.strip():
                segments.append((current.strip(), "virgola"))
                current = ""
            i += 1
            continue
        if char in ".!?":
            current += char
            while i + 1 < len(text) and text[i + 1] in ".!?":
                current += text[i + 1]
                i += 1
            if current.strip():
                segments.append((current.strip(), "punto"))
                current = ""
            i += 1
            continue
        current += char
        i += 1

    if current.strip():
        segments.append((current.strip(), None))

    return segments


def _split_long_segment_by_words(segment_text: str, words_per_chunk: int = 7):
    """
    Se un segmento (tra una punteggiatura e l'altra) è più lungo di words_per_chunk parole,
    lo spezza in sotto-blocchi di quella lunghezza, tagliando solo tra una parola e l'altra
    (mai a metà parola). Restituisce sempre una lista di almeno un elemento.
    """
    words = segment_text.split(" ")
    if len(words) <= words_per_chunk:
        return [segment_text]

    sub_chunks = []
    for i in range(0, len(words), words_per_chunk):
        sub_chunks.append(" ".join(words[i:i + words_per_chunk]))
    return sub_chunks


def dictation_text_to_audio(text: str) -> bytes:
    """
    Converte il testo (di UNA SINGOLA domanda) in audio con Piper TTS, con pause
    calibrate sulla punteggiatura reale del testo: la voce mantiene velocità e
    pronuncia naturali (length_scale=1.0), ed è la durata della pausa dopo ogni
    segmento - non la lentezza della voce - a darti il tempo per scrivere.
    Nelle frasi lunghe senza punteggiatura intermedia, viene inserita anche una
    pausa breve ogni N parole, per non far accumulare troppo testo prima della
    prossima pausa "vera". L'audio viene generato in MP3 (più leggero del WAV)
    per un upload più rapido e robusto su Telegram.
    """
    sample_rate = piper_voice.config.sample_rate

    def silence_for(seconds: float) -> bytes:
        return bytes(int(sample_rate * seconds) * 2)  # 2 byte per sample (16-bit)

    pause_comma = silence_for(float(os.getenv("PAUSE_COMMA_SECONDS", "3.5")))
    pause_period = silence_for(float(os.getenv("PAUSE_PERIOD_SECONDS", "5.0")))
    pause_newline = silence_for(float(os.getenv("PAUSE_NEWLINE_SECONDS", "7.0")))
    pause_default = silence_for(0.9)  # fallback per segmenti senza punteggiatura forte
    pause_word_group = silence_for(float(os.getenv("PAUSE_WORD_GROUP_SECONDS", "2.0")))
    words_per_chunk = int(os.getenv("WORDS_PER_PAUSE", "7"))
    repeat_silence = silence_for(3.0)

    pause_map = {
        "virgola": pause_comma,
        "punto": pause_period,
        "a_capo": pause_newline,
        None: pause_default,
    }

    segments = _split_by_punctuation(text)

    pcm_segments = []
    for segment_text, punctuation_type in segments:
        sub_chunks = _split_long_segment_by_words(segment_text, words_per_chunk)
        final_pause = pause_map.get(punctuation_type, pause_default)

        for j, sub_chunk in enumerate(sub_chunks):
            for audio_chunk in piper_voice.synthesize(sub_chunk, syn_config=piper_syn_config):
                pcm_segments.append(audio_chunk.audio_int16_bytes)
            if j < len(sub_chunks) - 1:
                pcm_segments.append(pause_word_group)
            else:
                pcm_segments.append(final_pause)

    single_pass_pcm = b"".join(pcm_segments)

    repeated_segments = []
    for i in range(AUDIO_REPEAT_COUNT):
        repeated_segments.append(single_pass_pcm)
        if i < AUDIO_REPEAT_COUNT - 1:
            repeated_segments.append(repeat_silence)

    pcm_data = b"".join(repeated_segments)

    wav_buffer = BytesIO()
    with wave.open(wav_buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_data)
    wav_buffer.seek(0)

    from pydub import AudioSegment

    audio_segment = AudioSegment.from_wav(wav_buffer)
    mp3_buffer = BytesIO()
    audio_segment.export(mp3_buffer, format="mp3", bitrate="64k")
    return mp3_buffer.getvalue()

# --- Handlers Telegram ---

TELEGRAM_MAX_MESSAGE_LENGTH = 4000  # margine di sicurezza sotto il limite reale di 4096


def split_text_for_telegram(text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Spezza un testo lungo in più messaggi sotto il limite di Telegram, senza tagliare a metà frase quando possibile."""
    if len(text) <= max_length:
        return [text]

    parts = []
    remaining = text
    while len(remaining) > max_length:
        split_point = remaining.rfind(". ", 0, max_length)
        if split_point == -1:
            split_point = max_length
        else:
            split_point += 1
        parts.append(remaining[:split_point].strip())
        remaining = remaining[split_point:].strip()
    if remaining:
        parts.append(remaining)
    return parts


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ciao! Mandami una foto del problema (email, errore, richiesta) "
        "e ti rispondo con un vocale che detta la soluzione."
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    status_message = await context.bot.send_message(chat_id, "Ricevuto. Analizzo la foto... (5%)")

    async def update_status(text: str):
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=status_message.message_id, text=text
            )
        except Exception:
            pass

    try:
        # 1. Scarica la foto più grande disponibile
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        image_base64 = base64.b64encode(bytes(photo_bytes)).decode("utf-8")

        # 2. UNA SOLA chiamata a Claude: genera l'esposizione per tutte le domande
        await update_status("Claude elabora la risposta... (20%)")
        raw_solution = solve_problem_from_image(image_base64, media_type="image/jpeg")
        logger.info(f"Risposta generata: {raw_solution[:200]}...")

        # 3. Parsing: una esposizione di testo per ciascuna domanda rilevata
        questions = parse_questions(raw_solution)
        n_questions = len(questions)
        logger.info(f"Numero di domande rilevate: {n_questions}")

        # La chiamata a Claude (il passo più pesante) è già conclusa: il resto è
        # solo sintesi audio locale (Piper, gratuita) + invio. Progresso 30%-100%.
        progress_after_solution = 30
        progress_remaining = 70
        progress_per_question = progress_remaining / n_questions

        # 4. Per ciascuna domanda: audio (Piper, pause sulla punteggiatura) + testo
        for i, question_text in enumerate(questions, start=1):
            label = f"Domanda {i} di {n_questions}" if n_questions > 1 else "Soluzione"
            base_progress = progress_after_solution + (i - 1) * progress_per_question

            await update_status(
                f"Genero l'audio per {label.lower()}... ({int(base_progress + progress_per_question * 0.5)}%)"
            )

            audio_bytes = dictation_text_to_audio(question_text)
            audio_buffer = BytesIO(audio_bytes)
            audio_buffer.name = f"soluzione_{i}.mp3"
            await context.bot.send_audio(
                chat_id, audio=audio_buffer, title=label[:64]
            )

            # Testo scritto, spezzato se troppo lungo per un singolo messaggio Telegram
            text_parts = split_text_for_telegram(f"📝 {label}:\n\n{question_text}")
            for part in text_parts:
                await context.bot.send_message(chat_id, part)

        await update_status("Fatto! (100%)")

    except Exception as e:
        logger.exception("Errore nella pipeline")
        await update_status(f"Si è verificato un errore: {e}")


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .read_timeout(120)
        .write_timeout(120)
        .connect_timeout(60)
        .pool_timeout(60)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Bot avviato. In ascolto...")
    app.run_polling()


if __name__ == "__main__":
    main()