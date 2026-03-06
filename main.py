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

Pour chaque situation tu évalues le niveau d'urgence toi-même:

🟢 VERT — Soins à domicile
→ Symptômes légers, pas de danger immédiat
→ Donne des conseils pratiques simples

🟡 JAUNE — Pharmacie ou médecin dans les 24h  
→ Symptômes modérés qui nécessitent attention
→ Recommande une consultation ou un test

🔴 ROUGE — URGENCE, soins immédiats requis
→ Symptômes graves ou potentiellement mortels
→ Envoie immédiatement aux urgences

Processus:
1. Si urgence évidente → ROUGE immédiatement
2. Sinon → pose maximum 2 questions pour clarifier
3. Après les questions → donne ton évaluation avec le niveau

Contexte: maladies fréquentes en Afrique de l'Ouest
(paludisme, typhoïde, méningite, choléra, dengue).
Toujours terminer par: "Ceci n'est pas un avis médical professionnel." """

# ── Safety layer ───────────────────────────────────────────
CRITICAL_KEYWORDS = [
    "ne respire pas", "arrêt cardiaque", "inconscient",
    "ne répond plus", "overdose", "empoisonnement"
]

EMERGENCY_RESPONSE = """🔴 URGENCE MÉDICALE CRITIQUE

Appelez le 15 (SAMU) ou les urgences IMMÉDIATEMENT.

Ne restez pas seul(e).

*Ceci n'est pas un avis médical professionnel.*"""

def is_critical(message):
    message_lower = message.lower()
    return any(keyword in message_lower for keyword in CRITICAL_KEYWORDS)

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
    return "UNKNOWN"

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
        # Never crash the bot because of a DB error

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
            max_tokens=350,
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

    # Layer 1 — critical emergency bypass
    if is_critical(incoming_message):
        print(f"🚨 CRITIQUE détecté")
        log_to_db(sender, "assistant", EMERGENCY_RESPONSE,
                  triage_level="RED", is_emergency=True)
        conversations.pop(sender, None)
        response = MessagingResponse()
        response.message(EMERGENCY_RESPONSE)
        return str(response)

    # Layer 2 — AI triage
    ai_response = get_ai_response(sender, incoming_message)
    triage_level = extract_triage_level(ai_response)

    # Log AI response to database
    log_to_db(sender, "assistant", ai_response, triage_level=triage_level)

    print(f"🤖 Triage: {triage_level}")

    response = MessagingResponse()
    response.message(ai_response)
    return str(response)

# ── Run ────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
