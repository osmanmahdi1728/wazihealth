import os
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI

# ── App setup ──────────────────────────────────────────────
app = Flask(__name__)
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# ── Conversation memory ────────────────────────────────────
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
→ Exemples: douleur thoracique, difficulté à respirer,
  perte de connaissance, saignement grave, convulsions,
  fièvre très élevée chez un nourrisson, symptômes d'AVC

Processus:
1. Si les symptômes sont clairement une urgence → ROUGE immédiatement
2. Sinon → pose maximum 2 questions pour clarifier
3. Après les questions → donne ton évaluation avec le niveau

Format de réponse pour le triage final:
[niveau emoji] [niveau texte]
[explication courte]
[action recommandée]

Ceci n'est pas un avis médical professionnel.

Contexte: tiens compte des maladies fréquentes en Afrique de l'Ouest 
(paludisme, typhoïde, méningite, choléra, dengue)."""

# ── Hard safety layer — obvious emergencies only ───────────
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
            temperature=0.2  # lower = more consistent medical responses
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

    print(f"📩 Message de {sender}: {incoming_message}")

    # Layer 1 — instant critical emergency bypass
    if is_critical(incoming_message):
        print(f"🚨 CRITIQUE détecté de {sender}")
        conversations.pop(sender, None)
        response = MessagingResponse()
        response.message(EMERGENCY_RESPONSE)
        return str(response)

    # Layer 2 — AI evaluates everything else including urgency
    ai_response = get_ai_response(sender, incoming_message)
    print(f"🤖 Réponse AI: {ai_response[:100]}...")
    response = MessagingResponse()
    response.message(ai_response)
    return str(response)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
