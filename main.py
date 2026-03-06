import os
import hashlib
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from supabase import create_client

# ── App setup ──────────────────────────────────────────────
app = Flask(__name__)
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
supabase = create_client(
    os.environ.get("SUPABASE_URL"),
    os.environ.get("SUPABASE_KEY")
)

# ── Conversation memory (in-RAM) ───────────────────────────
conversations = {}
MAX_HISTORY = 10

# ── AI system prompt ───────────────────────────────────────
SYSTEM_PROMPT = """Tu es WaziHealth, un assistant de triage médical 
bienveillant pour l'Afrique de l'Ouest francophone.

Tu évalues le niveau d'urgence et structures TOUJOURS ta réponse finale ainsi:

[niveau emoji] [NIVEAU] — [titre court]

📋 Analyse: [symptômes identifiés + hypothèse probable]

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
→ Donne des conseils pratiques simples

🟡 JAUNE — Pharmacie ou médecin dans les 24h
→ Symptômes modérés qui nécessitent attention
→ Recommande une consultation ou un test

🔴 ROUGE — URGENCE, soins immédiats requis
→ Symptômes graves ou potentiellement mortels
→ Dirige immédiatement vers les urgences

Règles importantes:
- Pendant les questions de suivi → pas de format structuré, juste la question
- Format structuré UNIQUEMENT pour la réponse finale de triage
- Maximum 2 questions avant de donner la réponse finale
- Tenir compte des maladies fréquentes en Afrique de l'Ouest:
  paludisme, typhoïde, méningite, choléra, dengue
- Si urgence évidente → ROUGE immédiatement sans questions
- Toujours répondre en français"""

# ── Critical emergency keywords ────────────────────────────
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
    """Hash phone number for privacy — we never store real numbers."""
    return hashlib.sha256(sender.encode()).hexdigest()[:16]

# ── Helper: detect triage level from AI response ──────────
def extract_triage_level(ai_response):
    response_upper = ai_response.upper()
    if "ROUGE" in response_upper or "🔴" in ai_response:
        return "RED"
    elif "JAUNE" in response_upper or "🟡" in ai_response:
        return "YELLOW"
    elif "VERT" in response_upper or "🟢" in ai_response:
        return "GREEN"
    return "PENDING"

# ── Helper: log message to Supabase ───────────────────────
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

# ── Helper: check if user is requesting human handoff ─────
def is_handoff_request(sender, message):
    """Returns True if user said OUI after bot offered human agent."""
    if message.strip().upper() not in ["OUI", "OUI.", "OUI!"]:
        return False
    if sender not in conversations or len(conversations[sender]) == 0:
        return False
    last_bot_message = conversations[sender][-1]["content"]
    return "agent humain" in last_bot_message.lower()

# ── Helper: check for critical emergency ──────────────────
def is_critical(message):
    message_lower = message.lower()
    return any(keyword in message_lower for keyword in CRITICAL_KEYWORDS)

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

        completion = client.chat.completions.create(
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
    incoming_message = request.form.get("Body", "").strip()
    sender = request.form.get("From", "")

    print(f"📩 Message de {hash_sender(sender)}: {incoming_message}")

    # Log user message to database
    log_to_db(sender, "user", incoming_message)

    # ── Layer 1: Critical emergency bypass ─────────────────
    if is_critical(incoming_message):
        print(f"🚨 CRITIQUE détecté")
        log_to_db(sender, "assistant", EMERGENCY_RESPONSE,
                  triage_level="RED", is_emergency=True)
        conversations.pop(sender, None)
        response = MessagingResponse()
        response.message(EMERGENCY_RESPONSE)
        return str(response)

    # ── Layer 2: Human handoff request ─────────────────────
    if is_handoff_request(sender, incoming_message):
        print(f"👤 Handoff demandé par {hash_sender(sender)}")
        log_to_db(sender, "system", "HUMAN_HANDOFF_REQUESTED",
                  triage_level="HANDOFF")
        conversations.pop(sender, None)
        response = MessagingResponse()
        response.message(HANDOFF_RESPONSE)
        return str(response)

    # ── Layer 3: Normal AI triage ───────────────────────────
    ai_response = get_ai_response(sender, incoming_message)
    triage_level = extract_triage_level(ai_response)

    log_to_db(sender, "assistant", ai_response, triage_level=triage_level)

    print(f"🤖 Triage: {triage_level} | {ai_response[:80]}...")

    response = MessagingResponse()
    response.message(ai_response)
    return str(response)

# ── Run ────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
