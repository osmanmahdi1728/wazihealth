from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
 
app = Flask(__name__)
 
# Keep-alive route — prevents Replit from sleeping
@app.route("/", methods=["GET"])
def home():
    return "WaziHealth is running! 🏥", 200
 
# Webhook route — handles WhatsApp messages
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_message = request.form.get("Body", "").strip()
    sender = request.form.get("From", "")
 
    print(f"📩 Message reçu de {sender}: {incoming_message}")
 
    response = MessagingResponse()
    response.message(
        "👋 Bonjour! Je suis *WaziHealth*.\n\n"
        "Je suis votre assistant santé disponible 24h/24.\n\n"
        "Décrivez vos symptômes et je vous aiderai. 🏥"
    )
 
    return str(response)
 
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
