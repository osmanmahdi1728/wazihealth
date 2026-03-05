import os
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI

# ── App setup ──────────────────────────────────────────────
app = Flask(__name__)
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# ── AI system prompt ───────────────────────────────────────
SYSTEM_PROMPT = """Tu es WaziHealth, un assistant de triage médical bienveillant 
et professionnel qui aide les populations d'Afrique de l'Ouest francophone.

Ton rôle:
- Poser des questions claires pour comprendre les symptômes
- Donner une orientation de triage en 3 niveaux:
  🟢 VERT: Soins à domicile — donne des conseils simples
  🟡 JAUNE: Pharmacie ou consultation dans les 24h
  🔴 ROUGE: Urgence — consulter un médecin immédiatement

Règles importantes:
- Toujours répondre en français
- Jamais poser plus de 3 questions avant de donner une orientation
- Toujours terminer par: "Ceci n'est pas un avis médical professionnel."
- Tenir compte du contexte médical ouest-africain (paludisme, typhoïde, etc.)"""

# ── Safety layer ───────────────────────────────────────────
EMERGENCY_KEYWORDS = [
    "douleur thoracique", "douleur poitrine", "mal à la poitrine",
    "difficulté à respirer", "je ne respire pas", "du mal à respirer",
    "inconscient", "perte de connaissance", "évanoui",
    "saignement abondant", "beaucoup de sang", "hémorragie",
    "convulsions", "crise", "paralysé", "ne bouge plus",
    "overdose", "empoisonnement", "avalé quelque chose"
]

EMERGENCY_RESPONSE = """🔴 URGENCE MÉDICALE

Ce que vous décrivez nécessite une aide médicale IMMÉDIATE.

👉 Appelez le 15 (SAMU) ou rendez-vous aux urgences les plus proches MAINTENANT.

Ne restez pas seul(e). Demandez à quelqu'un de vous accompagner.

*Ceci n'est pas un avis médical professionnel.*"""

def is_emergency(message):
    message_lower = message.lower()
    return any(keyword in message_lower for keyword in EMERGENCY_KEYWORDS)

# ── AI response function ───────────────────────────────────
def get_ai_response(user_message):
    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ],
            max_tokens=300,
            temperature=0.3
        )
        return completion.choices[0].message.content

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

    # Safety check FIRST — before any AI call
    if is_emergency(incoming_message):
        print(f"🚨 URGENCE détectée de {sender}")
        response = MessagingResponse()
        response.message(EMERGENCY_RESPONSE)
        return str(response)

    # Normal AI triage
    ai_response = get_ai_response(incoming_message)
    response = MessagingResponse()
    response.message(ai_response)
    return str(response)

# ── Run ────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
