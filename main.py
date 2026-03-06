import os
import hashlib
import tempfile
import requests
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from openai import OpenAI
from supabase import create_client

# ── App setup ──────────────────────────────────────────────
app = Flask(__name__)
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
supabase = create_client(
    os.environ.get("SUPABASE_URL"),
    os.environ.get("SUPABASE_KEY")
)
twilio_client = TwilioClient(
    os.environ.get("TWILIO_ACCOUNT_SID"),
    os.environ.get("TWILIO_AUTH_TOKEN")
)

# ── Conversation memory ────────────────────────────────────
conversations = {}
MAX_HISTORY = 10

# ── System prompt ──────────────────────────────────────────
SYSTEM_PROMPT = """Tu es WaziHealth, un assistant de triage médical 
bienveillant pour l'Afrique de l'Ouest francophone.

Tu évalues le niveau d'urgence et structures TOUJOURS ta réponse finale ainsi:

[niveau emoji] [NIVEAU] — [titre court]

📋 Analyse: [symptômes identifiés + hypothèse probable]

💊 En attendant le médecin:
   • [conseil pratique 1 — automédication sûre]
   • [conseil pratique 2 — hydratation, repos, etc.]
   • [ce qu'il faut éviter]

🏥 À la pharmacie, demandez:
   • "[terme exact à utiliser]"
   • Prix approximatif si connu (en CFA)

👉 Action: [ce que l'utilisateur doit faire maintenant]

📞 Qui contacter:
   • [option 1]
   • [option 2]

💬 Voulez-vous parler à un agent humain?
   Répondez *OUI* pour être mis en contact.

⚠️ Ceci n'est pas un avis médical professionnel.

---

Niveaux d'urgence:
🟢 VERT — Soins à domicile
→ Symptômes légers, pas de danger immédiat
→ Conseils pratiques suffisants

🟡 JAUNE — Pharmacie ou médecin dans les 24h
→ Symptômes modérés qui nécessitent attention
→ Donner automédication sûre en attendant

🔴 ROUGE — URGENCE, soins immédiats requis
→ Symptômes graves ou potentiellement mortels
→ Pas de conseil automédication — urgences seulement

Règles importantes:
- Pendant les questions de suivi → pas de format, juste la question
- Format structuré UNIQUEMENT pour la réponse finale
- Maximum 2 questions avant de donner la réponse finale
- Section 💊 uniquement pour VERT et JAUNE — jamais pour ROUGE
- Automédication: uniquement médicaments sans ordonnance
  (paracétamol, SRO, antihistaminiques basiques)
- Jamais recommander antibiotiques sans ordonnance
- Prix en CFA quand possible (contexte Sénégal/Côte d'Ivoire)
- Maladies fréquentes: paludisme, typhoïde, méningite, dengue, choléra
- Si urgence évidente → ROUGE immédiatement sans questions
- Toujours répondre en français"""
# ── Emergency constants ────────────────────────────────────
CRITICAL_KEYWORDS = [
    "ne respire pas", "arrêt cardiaque", "inconscient",
    "ne répond plus", "overdose", "empoisonnement"
]

EMERGENCY_RESPONSE = """🔴 URGENCE MÉDICALE CRITIQUE

Ce que vous décrivez nécessite une aide médicale IMMÉDIATE.

👉 Appelez le 15 (SAMU) ou rendez-vous aux urgences les plus proches MAINTENANT.

Ne restez pas seul(e). Demandez à quelqu'un de vous accompagner.

⚠️ Ceci n'est pas un avis médical professionnel."""

HANDOFF_RESPONSE = """👤 *Transfert vers un agent humain*

Un agent WaziHealth va vous contacter dans les plus brefs délais.

📞 Vous pouvez aussi nous appeler directement:
*+221 XX XXX XX XX*

Merci de votre confiance. 🙏"""

# ── Helper: anonymize phone number ────────────────────────
def hash_sender(sender):
    return hashlib.sha256(sender.encode()).hexdigest()[:16]

# ── Helper: detect triage level ───────────────────────────
def extract_triage_level(ai_response):
    response_upper = ai_response.upper()
    if "ROUGE" in response_upper or "🔴" in ai_response:
        return "RED"
    elif "JAUNE" in response_upper or "🟡" in ai_response:
        return "YELLOW"
    elif "VERT" in response_upper or "🟢" in ai_response:
        return "GREEN"
    return "PENDING"

# ── Helper: log to Supabase ───────────────────────────────
def log_to_db(sender, role, content, triage_level=None, is_emergency=False):
    try:
        supabase.table("consultations").insert({
            "session_id":      hash_sender(sender),
            "sender_hash":     hash_sender(sender),
            "message_role":    role,
            "message_content": content,
            "triage_level":    triage_level,
            "is_emergency":    is_emergency,
        }).execute()
    except Exception as e:
        print(f"⚠️ DB log error: {e}")

# ── Helper: human handoff check ───────────────────────────
def is_handoff_request(sender, message):
    if message.strip().upper() not in ["OUI", "OUI.", "OUI!"]:
        return False
    if sender not in conversations or len(conversations[sender]) == 0:
        return False
    last_bot_message = conversations[sender][-1]["content"]
    return "agent humain" in last_bot_message.lower()

# ── Helper: critical emergency check ──────────────────────
def is_critical(message):
    message_lower = message.lower()
    return any(keyword in message_lower for keyword in CRITICAL_KEYWORDS)

# ── Helper: transcribe voice note ─────────────────────────
def transcribe_audio(media_url):
    """Download WhatsApp voice note and transcribe with Whisper."""
    try:
        # Download audio using Twilio credentials
        auth = (
            os.environ.get("TWILIO_ACCOUNT_SID"),
            os.environ.get("TWILIO_AUTH_TOKEN")
        )
        audio_response = requests.get(media_url, auth=auth, timeout=30)

        if audio_response.status_code != 200:
            print(f"⚠️ Audio download failed: {audio_response.status_code}")
            return None

        # Save to temp file and transcribe
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(audio_response.content)
            tmp_path = tmp.name

        with open(tmp_path, "rb") as audio_file:
            transcription = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="fr"  # French — change to None for auto-detect
            )

        transcript = transcription.text
        print(f"🎤 Transcription: {transcript}")
        return transcript

    except Exception as e:
        print(f"❌ Transcription error: {e}")
        return None

# ── AI response with memory ────────────────────────────────
def get_ai_response(sender, user_message):
    try:
        if sender not in conversations:
            conversations[sender] = []

        conversations[sender].append({
            "role": "user",
            "content": user_message
        })

        if len(conversations[sender]) > MAX_HISTORY:
            conversations[sender] = conversations[sender][-MAX_HISTORY:]

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages += conversations[sender]

        completion = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=400,
            temperature=0.2
        )

        ai_reply = completion.choices[0].message.content

        conversations[sender].append({
            "role": "assistant",
            "content": ai_reply
        })

        return ai_reply

    except Exception as e:
        print(f"❌ OpenAI error: {type(e).__name__}: {e}")
        return (
            "Désolé, je rencontre un problème technique. "
            "Veuillez réessayer dans quelques instants. 🙏"
        )

# ── Routes ─────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def home():
    return "WaziHealth est en ligne! 🏥", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    sender        = request.form.get("From", "")
    incoming_text = request.form.get("Body", "").strip()
    num_media     = int(request.form.get("NumMedia", 0))

    # ── Handle voice note ───────────────────────────────────
    if num_media > 0:
        media_url         = request.form.get("MediaUrl0", "")
        media_content_type = request.form.get("MediaContentType0", "")

        print(f"🎤 Voice note reçu de {hash_sender(sender)}")
        print(f"   Type: {media_content_type}")

        if "audio" in media_content_type:
            transcript = transcribe_audio(media_url)

            if transcript:
                incoming_text = transcript
                print(f"✅ Transcription réussie: {incoming_text}")
            else:
                # Transcription failed — ask user to type
                response = MessagingResponse()
                response.message(
                    "🎤 Je n'ai pas pu comprendre votre message vocal.\n\n"
                    "Pouvez-vous décrire vos symptômes par écrit? 🙏"
                )
                return str(response)
        else:
            # Not an audio file (image, video, etc.)
            response = MessagingResponse()
            response.message(
                "Je peux recevoir des messages vocaux et texte.\n"
                "Décrivez vos symptômes en texte ou en message vocal. 🎤"
            )
            return str(response)

    # ── No message content at all ───────────────────────────
    if not incoming_text:
        response = MessagingResponse()
        response.message(
            "👋 Bonjour! Je suis WaziHealth.\n\n"
            "Décrivez vos symptômes en texte ou envoyez "
            "un message vocal 🎤 et je vous aiderai."
        )
        return str(response)

    print(f"📩 Message de {hash_sender(sender)}: {incoming_text}")

    # Log user message
    log_to_db(sender, "user", incoming_text)

    # ── Layer 1: Critical emergency ─────────────────────────
    if is_critical(incoming_text):
        print(f"🚨 CRITIQUE détecté")
        log_to_db(sender, "assistant", EMERGENCY_RESPONSE,
                  triage_level="RED", is_emergency=True)
        conversations.pop(sender, None)
        response = MessagingResponse()
        response.message(EMERGENCY_RESPONSE)
        return str(response)

    # ── Layer 2: Human handoff ──────────────────────────────
    if is_handoff_request(sender, incoming_text):
        print(f"👤 Handoff demandé")
        log_to_db(sender, "system", "HUMAN_HANDOFF_REQUESTED",
                  triage_level="HANDOFF")
        conversations.pop(sender, None)
        response = MessagingResponse()
        response.message(HANDOFF_RESPONSE)
        return str(response)

    # ── Layer 3: AI triage ──────────────────────────────────
    ai_response  = get_ai_response(sender, incoming_text)
    triage_level = extract_triage_level(ai_response)

    log_to_db(sender, "assistant", ai_response, triage_level=triage_level)
    print(f"🤖 Triage: {triage_level}")

    response = MessagingResponse()
    response.message(ai_response)
    return str(response)

# ── Run ────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

