import os, hashlib, tempfile, threading, requests
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from openai import OpenAI
from supabase import create_client
import cloudinary, cloudinary.uploader

app = Flask(__name__)
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
twilio_client = TwilioClient(os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
cloudinary.config(cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"), api_key=os.environ.get("CLOUDINARY_API_KEY"), api_secret=os.environ.get("CLOUDINARY_API_SECRET"))

conversations = {}
MAX_HISTORY = 10

SYSTEM_PROMPT = """Tu es WaziHealth, un assistant de triage médical bienveillant pour l'Afrique de l'Ouest francophone.

Tu évalues le niveau d'urgence et structures TOUJOURS ta réponse finale ainsi:

[niveau emoji] [NIVEAU] — [titre court]

📋 Analyse: [symptômes identifiés + hypothèse probable]

💊 En attendant le médecin:
   • [conseil pratique 1]
   • [conseil pratique 2]
   • [ce qu'il faut éviter]

🏥 À la pharmacie, demandez:
   • "[terme exact]"
   • Prix approximatif en CFA

👉 Action: [ce que l'utilisateur doit faire maintenant]

📞 Qui contacter:
   • [option 1]
   • [option 2]

💬 Voulez-vous parler à un agent humain? Répondez *OUI*.

⚠️ Ceci n'est pas un avis médical professionnel.

Niveaux: 🟢 VERT=domicile 🟡 JAUNE=pharmacie/24h 🔴 ROUGE=urgence immédiate
Règles: max 2 questions avant réponse finale. Section 💊 uniquement VERT/JAUNE.
Jamais antibiotiques sans ordonnance. Prix en CFA. Maladies: paludisme, typhoïde, méningite, dengue.
Si urgence évidente → ROUGE immédiatement. Toujours en français."""

CRITICAL_KEYWORDS = ["ne respire pas","arrêt cardiaque","inconscient","ne répond plus","overdose","empoisonnement"]

EMERGENCY_RESPONSE = """🔴 URGENCE MÉDICALE CRITIQUE

Appelez le 15 (SAMU) ou urgences MAINTENANT.
Ne restez pas seul(e).

⚠️ Ceci n'est pas un avis médical professionnel."""

HANDOFF_RESPONSE = """👤 *Transfert vers un agent humain*

Un agent WaziHealth vous contacte bientôt.
📞 *+221 XX XXX XX XX*
Merci de votre confiance. 🙏"""

def hash_sender(s): return hashlib.sha256(s.encode()).hexdigest()[:16]

def extract_triage_level(r):
    u = r.upper()
    if "ROUGE" in u or "🔴" in r: return "RED"
    if "JAUNE" in u or "🟡" in r: return "YELLOW"
    if "VERT" in u or "🟢" in r: return "GREEN"
    return "PENDING"

def log_to_db(sender, role, content, triage_level=None, is_emergency=False):
    try:
        supabase.table("consultations").insert({
            "session_id": hash_sender(sender), "sender_hash": hash_sender(sender),
            "message_role": role, "message_content": content,
            "triage_level": triage_level, "is_emergency": is_emergency,
        }).execute()
    except Exception as e:
        print(f"⚠️ DB error: {e}")

def is_handoff_request(sender, message):
    if message.strip().upper() not in ["OUI","OUI.","OUI!"]: return False
    if sender not in conversations or not conversations[sender]: return False
    return "agent humain" in conversations[sender][-1]["content"].lower()

def is_critical(message):
    return any(k in message.lower() for k in CRITICAL_KEYWORDS)

def transcribe_audio(media_url):
    try:
        print(f"⬇️ Téléchargement audio...")
        auth = (os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
        r = requests.get(media_url, auth=auth, timeout=30)
        print(f"📥 Status: {r.status_code}")
        if r.status_code != 200: return None
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(r.content)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as f:
            result = openai_client.audio.transcriptions.create(model="whisper-1", file=f, language="fr")
        print(f"🎤 Transcription: {result.text}")
        return result.text
    except Exception as e:
        print(f"❌ Transcription error: {e}")
        return None

def send_audio_async(sender, ai_response):
    try:
        print("🔄 Génération audio en arrière-plan...")
        summary = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Résume en 2-3 phrases courtes pour message vocal. Garde urgence + action principale."},
                {"role": "user", "content": ai_response}
            ],
            max_tokens=120
        ).choices[0].message.content
        print(f"📝 Résumé: {summary}")
        tts = openai_client.audio.speech.create(model="tts-1", voice="nova", input=summary, speed=0.9)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(tts.content)
            tmp_path = tmp.name
        upload = cloudinary.uploader.upload(tmp_path, resource_type="video", folder="wazihealth/audio")
        audio_url = upload["secure_url"]
        print(f"☁️ Uploadé: {audio_url}")
        twilio_client.messages.create(
            from_=os.environ.get("TWILIO_WHATSAPP_NUMBER"),
            to=sender,
            media_url=[audio_url],
            body="🎤"
        )
        print(f"✅ Audio envoyé à {hash_sender(sender)}")
    except Exception as e:
        print(f"❌ Audio async error: {type(e).__name__}: {e}")

def get_ai_response(sender, user_message):
    try:
        if sender not in conversations: conversations[sender] = []
        conversations[sender].append({"role": "user", "content": user_message})
        if len(conversations[sender]) > MAX_HISTORY:
            conversations[sender] = conversations[sender][-MAX_HISTORY:]
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conversations[sender]
        reply = openai_client.chat.completions.create(model="gpt-4o-mini", messages=messages, max_tokens=500, temperature=0.2).choices[0].message.content
        conversations[sender].append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        print(f"❌ OpenAI error: {e}")
        return "Désolé, problème technique. Veuillez réessayer. 🙏"

@app.route("/", methods=["GET"])
def home():
    return "WaziHealth est en ligne! 🏥", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    sender        = request.form.get("From", "")
    incoming_text = request.form.get("Body", "").strip()
    num_media     = int(request.form.get("NumMedia", 0))
    is_audio      = False

    if num_media > 0:
        media_url   = request.form.get("MediaUrl0", "")
        media_type  = request.form.get("MediaContentType0", "")
        print(f"📎 Media: {media_type}")
        if "audio" in media_type:
            is_audio = True
            transcript = transcribe_audio(media_url)
            if transcript:
                incoming_text = transcript
            else:
                r = MessagingResponse()
                r.message("🎤 Message vocal incompris. Décrivez par écrit svp 🙏")
                return str(r)
        else:
            r = MessagingResponse()
            r.message("Envoyez un texte ou message vocal 🎤")
            return str(r)

    if not incoming_text:
        r = MessagingResponse()
        r.message("👋 Bonjour! Je suis WaziHealth.\nDécrivez vos symptômes en texte ou vocal 🎤")
        return str(r)

    print(f"📩 {hash_sender(sender)}: {incoming_text}")
    log_to_db(sender, "user", incoming_text)

    if is_critical(incoming_text):
        print("🚨 CRITIQUE détecté")
        log_to_db(sender, "assistant", EMERGENCY_RESPONSE, triage_level="RED", is_emergency=True)
        conversations.pop(sender, None)
        r = MessagingResponse()
        r.message(EMERGENCY_RESPONSE)
        return str(r)

    if is_handoff_request(sender, incoming_text):
        print("👤 Handoff demandé")
        log_to_db(sender, "system", "HUMAN_HANDOFF_REQUESTED", triage_level="HANDOFF")
        conversations.pop(sender, None)
        r = MessagingResponse()
        r.message(HANDOFF_RESPONSE)
        return str(r)

    ai_response  = get_ai_response(sender, incoming_text)
    triage_level = extract_triage_level(ai_response)
    log_to_db(sender, "assistant", ai_response, triage_level=triage_level)
    print(f"🤖 Triage: {triage_level}")

    r = MessagingResponse()
    r.message(ai_response)

    if is_audio:
        t = threading.Thread(target=send_audio_async, args=(sender, ai_response))
        t.daemon = True
        t.start()
        print("🔄 Thread audio démarré")

    return str(r)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
