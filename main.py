import os
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI

app = Flask(__name__)
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# System prompt — this is your AI doctor's instructions
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
- Si l'utilisateur mentionne: douleur thoracique, difficulté à respirer, 
  perte de conscience, saignement grave → répondre ROUGE immédiatement
- Être chaleureux, rassurant et simple dans le langage
- Tenir compte du contexte médical ouest-africain (paludisme, typhoïde, etc.)"""

@app.route("/", methods=["GET"])
def home():
    return "WaziHealth est en ligne! 🏥", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_message = request.form.get("Body", "").strip()
    sender = request.form.get("From", "")

    print(f"📩 Message de {sender}: {incoming_message}")

    # Get AI response
    ai_response = get_ai_response(incoming_message)

    # Send reply via Twilio
    response = MessagingResponse()
    response.message(ai_response)

    return str(response)

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
        print(f"OpenAI error: {e}")
        return (
            "Désolé, je rencontre un problème technique. "
            "Veuillez réessayer dans quelques instants. 🙏"
        )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
