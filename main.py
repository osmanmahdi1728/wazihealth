import os, hashlib, tempfile, threading, requests
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from openai import OpenAI
from supabase import create_client
import cloudinary, cloudinary.uploader

# ── App setup ──────────────────────────────────────────────
app = Flask(__name__)
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
twilio_client = TwilioClient(os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET")
)

conversations = {}
MAX_HISTORY = 20

SYSTEM_PROMPT = """Tu es WaziHealth, assistant santé pour l'Afrique de l'Ouest.
Tu parles comme un infirmier bienveillant — simple, clair, rassurant.
Pas de termes médicaux compliqués. Phrases courtes.

RÈGLE PRINCIPALE: Maximum 3 échanges avant le diagnostic.
Si les réponses sont vagues → 4 échanges max, puis diagnostique quand même.

━━━━━━━━━━━━━━━━━━━━━━
ÉCHANGE 1 — Symptôme + durée
━━━━━━━━━━━━━━━━━━━━━━
Pose UNE seule question combinée:
"Qu'est-ce qui ne va pas et depuis quand?
1️⃣ Fièvre / chaleur — aujourd'hui
2️⃣ Fièvre / chaleur — depuis 2-3 jours ou plus
3️⃣ Mal de tête / ventre / dos
4️⃣ Toux / mal à respirer
5️⃣ Diarrhée / vomissements
6️⃣ Problème de peau (boutons, plaie, démangeaisons)
7️⃣ Fatigue / faiblesse
8️⃣ Autre — décrivez en un mot"

━━━━━━━━━━━━━━━━━━━━━━
ÉCHANGE 2 — 2-3 signes visibles ou ressentis
━━━━━━━━━━━━━━━━━━━━━━
Selon la réponse, pose UNE question avec 4-5 choix max:

Si FIÈVRE:
"Avez-vous aussi: (donnez les numéros)
1️⃣ Frissons ou tremblements
2️⃣ Sueurs
3️⃣ Mal de tête fort
4️⃣ Yeux qui jaunissent
5️⃣ Urine très foncée (marron)"

Si TOUX:
"Avec la toux: (donnez les numéros)
1️⃣ Crachats jaunes ou verts
2️⃣ Un peu de sang dans les crachats
3️⃣ Fièvre en même temps
4️⃣ Essoufflement même au repos
5️⃣ Amaigrissement récent"

Si VENTRE:
"Avec le mal de ventre: (donnez les numéros)
1️⃣ Diarrhée (combien de fois?)
2️⃣ Vomissements
3️⃣ Fièvre
4️⃣ Sang dans les selles
5️⃣ Ventre très gonflé"

Si PEAU:
"Sur la peau, c'est: (donnez les numéros)
1️⃣ Boutons rouges qui démangent
2️⃣ Plaie ou blessure
3️⃣ Gonflement
4️⃣ Peau qui pèle ou sèche
5️⃣ Taches blanches (bouche ou peau)"

━━━━━━━━━━━━━━━━━━━━━━
ÉCHANGE 3 — Profil (1 question rapide)
━━━━━━━━━━━━━━━━━━━━━━
"Dernière question:
• C'est pour qui? Bébé / Enfant / Adulte / Personne âgée
• Femme enceinte? Oui / Non
• Voyage récent en zone rurale? Oui / Non"

━━━━━━━━━━━━━━━━━━━━━━
ÉCHANGE 4 — DIAGNOSTIC FINAL (toujours après échange 3)
━━━━━━━━━━━━━━━━━━━━━━
Format exact:

[emoji niveau] [NIVEAU] — [titre simple]

🔍 Ce que ça ressemble à: [explication simple, 1-2 phrases]

💊 À faire maintenant:
   • [action 1 simple]
   • [action 2 simple]
   • ❌ Évitez: [1 chose à ne pas faire]

🏥 À la pharmacie:
   • Demandez: "[mots exacts à dire au pharmacien]"
   • Prix: ~[montant] CFA

📞 Consultez:
   • [qui contacter, où aller]

💬 Voulez-vous parler à quelqu'un? Répondez OUI

━━━━━━━━━━━━━━━━━━━━━━
🙏 Cette réponse vous a-t-elle aidé?
1️⃣ Oui
2️⃣ Partiellement
3️⃣ Non

Votre avis améliore WaziHealth 💚
━━━━━━━━━━━━━━━━━━━━━━

⚠️ Ceci ne remplace pas un médecin.

NIVEAUX:
🟢 VERT = restez à la maison, voici quoi faire
🟡 JAUNE = pharmacie ou centre de santé dans 24h
🔴 ROUGE = partez aux urgences maintenant

URGENCES → ROUGE immédiat sans questions:
- Ne répond pas / inconscient
- Ne respire pas / lèvres bleues
- Convulsions
- Fièvre + nuque qui ne plie pas
- Bébé moins de 3 mois avec fièvre
- Saignement qui ne s'arrête pas
- Douleur poitrine forte
- Yeux jaunes + urine marron foncé
- Femme enceinte avec saignement
- Bouche tordue / bras qui ne bouge plus

RÈGLES:
- Max 3 échanges (4 si vraiment nécessaire)
- UNE seule question par échange
- Jamais de termes médicaux complexes
- Jamais prescrire antibiotiques
- Section 💊 uniquement pour VERT et JAUNE
- Prix en CFA"""

CRITICAL_KEYWORDS = [
    "inconscient", "sans connaissance", "ne répond pas", "ne répond plus",
    "évanoui", "perdu connaissance", "tombe dans les pommes",
    "ne respire pas", "arrêt respiratoire", "étouffement", "étouffe",
    "lèvres bleues", "bleu", "souffle coupé",
    "arrêt cardiaque", "infarctus", "douleur poitrine forte",
    "convulsions", "crise épilepsie", "tremble tout", "paralysé",
    "paralysie", "avc", "bouche tordue", "visage tordu", "bras ne bouge plus",
    "saignement abondant", "beaucoup de sang", "hémorragie",
    "sang dans vomissements", "vomit du sang",
    "overdose", "empoisonnement", "avalé produit", "intoxication",
    "nuque raide", "raideur nuque", "ne peut pas baisser la tête",
    "saignement enceinte", "douleur enceinte forte",
]

EMERGENCY_RESPONSE = """🔴 URGENCE — Allez aux urgences MAINTENANT

👉 Appelez le 15 ou demandez à quelqu'un de vous emmener.
Ne restez pas seul(e).

⚠️ Ce message ne remplace pas un médecin."""

HANDOFF_RESPONSE = """👤 On vous met en contact avec un agent.

Un agent WaziHealth vous appelle bientôt.
📞 Ou appelez: *+221 XX XXX XX XX*
Merci 🙏"""

RESOURCES = {
    "paludisme": {
        "video":     "https://youtube.com/watch?v=7k8KfqUkMDU",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/malaria",
        "aliments":  "🥗 À manger:\n   • Bouillon de poulet\n   • Riz blanc\n   • Banane\n   • Eau de coco",
        "hydration": "💧 3L d'eau par jour"
    },
    "paludisme_enfant": {
        "video":     "https://youtube.com/watch?v=7k8KfqUkMDU",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/malaria",
        "aliments":  "🥗 Pour enfant:\n   • Lait maternel si nourrisson\n   • Bouillon léger\n   • Eau de coco",
        "hydration": "💧 SRO si diarrhée — pharmacie"
    },
    "typhoide": {
        "video":     "https://youtube.com/watch?v=3vMxVPcKDlY",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/typhoid",
        "aliments":  "🥗 À manger:\n   • Soupe légère\n   • Yaourt nature\n   • Pomme de terre cuite\n   • ❌ Pas d'épices",
        "hydration": "💧 SRO en pharmacie"
    },
    "diarrhee": {
        "video":     "https://youtube.com/watch?v=W0JQPMDPkBQ",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/diarrhoeal-disease",
        "aliments":  "🥗 À manger:\n   • Riz blanc\n   • Banane\n   • Pain grillé\n   • ❌ Pas de lait ni friture",
        "hydration": "💧 SRO — 1 sachet dans 1L d'eau"
    },
    "meningite": {
        "video":     "https://youtube.com/watch?v=MNhkTMUCHHw",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/meningitis",
        "aliments":  "🥗 Urgence — allez à l'hôpital",
        "hydration": "💧 Perfusion à l'hôpital"
    },
    "dengue": {
        "video":     "https://youtube.com/watch?v=k3EQCn9GPCU",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/dengue-and-severe-dengue",
        "aliments":  "🥗 À manger:\n   • Jus de papaye\n   • Soupe\n   • Fruits frais\n   • ❌ Pas d'aspirine",
        "hydration": "💧 3-4L d'eau ou jus par jour"
    },
    "grippe": {
        "video":     "https://youtube.com/watch?v=OvNNsR5FXKU",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/influenza-(seasonal)",
        "aliments":  "🥗 À manger:\n   • Soupe chaude\n   • Miel + citron\n   • Gingembre\n   • Bouillon",
        "hydration": "💧 Tisanes + 2L d'eau"
    },
    "deshydratation": {
        "video":     "https://youtube.com/watch?v=9iMGFqMmUFs",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/diarrhoeal-disease",
        "aliments":  "🥗 À boire:\n   • Eau de coco\n   • Bouillon salé\n   • Pastèque, concombre",
        "hydration": "💧 SRO immédiatement"
    },
    "cholera": {
        "video":     "https://youtube.com/watch?v=YFKQ4R7MYXI",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/cholera",
        "aliments":  "🥗 Urgence — SRO immédiatement",
        "hydration": "💧 SRO en continu — centre de santé"
    },
    "tuberculose": {
        "video":     "https://youtube.com/watch?v=K4eFSbFhRkE",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/tuberculosis",
        "aliments":  "🥗 À manger:\n   • Œufs\n   • Poisson\n   • Légumes frais\n   • Fruits",
        "hydration": "💧 2L d'eau par jour"
    },
    "infection_urinaire": {
        "video":     "https://youtube.com/watch?v=QqSaIFp3k0Q",
        "info":      "https://www.who.int/fr/",
        "aliments":  "🥗 À manger:\n   • Yaourt nature\n   • ❌ Pas d'alcool ni café",
        "hydration": "💧 3L d'eau minimum"
    },
    "hypertension": {
        "video":     "https://youtube.com/watch?v=ab5p3B5XJBE",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/hypertension",
        "aliments":  "🥗 À manger:\n   • Légumes frais\n   • Banane\n   • Poisson\n   • ❌ Pas de sel ni friture",
        "hydration": "💧 2L d'eau par jour"
    },
    "diabete": {
        "video":     "https://youtube.com/watch?v=9OvSIFZMfmI",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/diabetes",
        "aliments":  "🥗 À manger:\n   • Légumes verts\n   • Poisson grillé\n   • ❌ Pas de sucre ni sodas",
        "hydration": "💧 Eau uniquement"
    },
    "malnutrition": {
        "video":     "https://youtube.com/watch?v=GlxiUkEFtL4",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/malnutrition",
        "aliments":  "🥗 À manger:\n   • Niébé, lentilles\n   • Œufs\n   • Poisson\n   • Arachides",
        "hydration": "💧 Eau propre — 2L par jour"
    },
    "conjonctivite": {
        "video":     "https://youtube.com/watch?v=xMEMWKHHcCk",
        "info":      "https://www.who.int/fr/",
        "aliments":  "🥗 Lavez les mains souvent\n   • ❌ Ne frottez pas les yeux",
        "hydration": "💧 Normal"
    },
}

FEEDBACK_CHOICES = {"1": "utile", "2": "partiel", "3": "non_utile"}

# ── Helpers ────────────────────────────────────────────────
def hash_sender(s):
    return hashlib.sha256(s.encode()).hexdigest()[:16]

def extract_triage_level(r):
    u = r.upper()
    if "ROUGE" in u or "🔴" in r: return "RED"
    if "JAUNE" in u or "🟡" in r: return "YELLOW"
    if "VERT"  in u or "🟢" in r: return "GREEN"
    return "PENDING"

def detect_condition(ai_response):
    r = ai_response.lower()
    conditions = {
        "paludisme_enfant":  ["paludisme enfant", "malaria enfant", "paludisme bébé"],
        "paludisme":         ["paludisme", "malaria", "tdr"],
        "typhoide":          ["typhoïde", "typhoide", "fièvre typhoïde"],
        "diarrhee":          ["diarrhée", "diarrhee", "gastro", "sro"],
        "meningite":         ["méningite", "meningite"],
        "dengue":            ["dengue"],
        "grippe":            ["grippe", "influenza", "rhume"],
        "deshydratation":    ["déshydratation", "deshydratation"],
        "cholera":           ["choléra", "cholera"],
        "tuberculose":       ["tuberculose", "tb pulmonaire"],
        "infection_urinaire":["infection urinaire", "cystite", "brûlure urine"],
        "hypertension":      ["hypertension", "tension élevée", "pression élevée"],
        "diabete":           ["diabète", "diabete", "glycémie"],
        "malnutrition":      ["malnutrition", "sous-alimentation"],
        "conjonctivite":     ["conjonctivite", "yeux rouges", "œil rouge"],
    }
    for condition, keywords in conditions.items():
        if any(kw in r for kw in keywords):
            return condition
    return None

def get_resources(condition):
    if not condition or condition not in RESOURCES: return None
    res = RESOURCES[condition]
    return (
        f"{res['aliments']}\n\n"
        f"{res['hydration']}\n\n"
        f"🌐 En savoir plus: {res['info']}\n"
        f"📹 Vidéo: {res['video']}"
    )

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
        print(f"⚠️ DB error: {e}")

def is_critical(message):
    return any(k in message.lower() for k in CRITICAL_KEYWORDS)

def is_handoff_request(sender, message):
    if message.strip().upper() not in ["OUI","OUI.","OUI!"]: return False
    if sender not in conversations or not conversations[sender]: return False
    return "agent" in conversations[sender][-1]["content"].lower()

def is_feedback(sender, message):
    if message.strip() not in ["1","2","3"]: return False
    if sender not in conversations or not conversations[sender]: return False
    return "cette réponse vous a-t-elle aidé" in conversations[sender][-1]["content"].lower()

def analyze_image(media_url):
    try:
        print("🖼️ Analyse image...")
        auth = (os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
        res = requests.get(media_url, auth=auth, timeout=30)
        if res.status_code != 200: return None
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(res.content)
            tmp_path = tmp.name
        upload = cloudinary.uploader.upload(tmp_path, folder="wazihealth/images")
        public_url = upload["secure_url"]
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Tu es un assistant santé. Décris simplement ce que tu vois sur cette photo: couleur, aspect, localisation. 2-3 phrases max. Pas de diagnostic."},
                    {"type": "image_url", "image_url": {"url": public_url}}
                ]
            }],
            max_tokens=150
        )
        description = response.choices[0].message.content
        print(f"🖼️ Description: {description}")
        return description
    except Exception as e:
        print(f"❌ Image error: {e}")
        return None

def transcribe_audio(media_url):
    try:
        print("⬇️ Téléchargement audio...")
        auth = (os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
        res = requests.get(media_url, auth=auth, timeout=30)
        print(f"📥 Status: {res.status_code}")
        if res.status_code != 200: return None
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(res.content)
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
        print("🔄 Génération audio...")
        summary = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Résume en 2-3 phrases très courtes pour message vocal. Garde urgence + action principale."},
                {"role": "user",   "content": ai_response}
            ],
            max_tokens=100
        ).choices[0].message.content
        tts = openai_client.audio.speech.create(model="tts-1", voice="nova", input=summary, speed=0.9)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(tts.content)
            tmp_path = tmp.name
        upload = cloudinary.uploader.upload(tmp_path, resource_type="video", folder="wazihealth/audio")
        audio_url = upload["secure_url"]
        print(f"☁️ Audio: {audio_url}")
        twilio_client.messages.create(
            from_=os.environ.get("TWILIO_WHATSAPP_NUMBER"),
            to=sender,
            media_url=[audio_url],
            body="🎤"
        )
        print("✅ Audio envoyé")
    except Exception as e:
        print(f"❌ Audio error: {type(e).__name__}: {e}")

def get_ai_response(sender, user_message):
    try:
        if sender not in conversations: conversations[sender] = []
        conversations[sender].append({"role": "user", "content": user_message})
        if len(conversations[sender]) > MAX_HISTORY:
            conversations[sender] = conversations[sender][-MAX_HISTORY:]
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conversations[sender]
        reply = openai_client.chat.completions.create(
            model="gpt-4o-mini", messages=messages, max_tokens=500, temperature=0.2
        ).choices[0].message.content
        conversations[sender].append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        print(f"❌ OpenAI error: {e}")
        return "Désolé, problème technique. Réessayez svp 🙏"

# ── Routes ─────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def home():
    return "WaziHealth est en ligne! 🏥", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    sender        = request.form.get("From", "")
    incoming_text = request.form.get("Body", "").strip()
    num_media     = int(request.form.get("NumMedia", 0))
    is_audio      = False

    # ── Media (audio ou image) ──────────────────────────────
    if num_media > 0:
        media_url  = request.form.get("MediaUrl0", "")
        media_type = request.form.get("MediaContentType0", "")
        print(f"📎 Media: {media_type}")

        if "audio" in media_type:
            is_audio   = True
            transcript = transcribe_audio(media_url)
            if transcript:
                incoming_text = transcript
            else:
                r = MessagingResponse()
                r.message("🎤 Pas compris. Écrivez vos symptômes svp 🙏")
                return str(r)

        elif "image" in media_type:
            description = analyze_image(media_url)
            if description:
                incoming_text = f"[Photo envoyée] {description}"
                print(f"🖼️ Image analysée: {description}")
            else:
                r = MessagingResponse()
                r.message("📸 Photo reçue mais je n'arrive pas à l'analyser.\nDécrivez ce que vous voyez en texte svp.")
                return str(r)

        else:
            r = MessagingResponse()
            r.message("Envoyez un texte, une photo 📸 ou un message vocal 🎤")
            return str(r)

    # ── Message vide ────────────────────────────────────────
    if not incoming_text:
        r = MessagingResponse()
        r.message("👋 Bonjour! Je suis WaziHealth.\nDites-moi ce qui ne va pas — texte, photo 📸 ou vocal 🎤")
        return str(r)

    print(f"📩 {hash_sender(sender)}: {incoming_text}")
    log_to_db(sender, "user", incoming_text)

    # ── Layer 1: Urgence critique ───────────────────────────
    if is_critical(incoming_text):
        print("🚨 CRITIQUE")
        log_to_db(sender, "assistant", EMERGENCY_RESPONSE, triage_level="RED", is_emergency=True)
        conversations.pop(sender, None)
        r = MessagingResponse()
        r.message(EMERGENCY_RESPONSE)
        return str(r)

    # ── Layer 2: Transfert humain ───────────────────────────
    if is_handoff_request(sender, incoming_text):
        print("👤 Handoff")
        log_to_db(sender, "system", "HUMAN_HANDOFF_REQUESTED", triage_level="HANDOFF")
        conversations.pop(sender, None)
        r = MessagingResponse()
        r.message(HANDOFF_RESPONSE)
        return str(r)

    # ── Layer 3: Feedback ───────────────────────────────────
    if is_feedback(sender, incoming_text):
        feedback_value = FEEDBACK_CHOICES.get(incoming_text.strip(), "inconnu")
        print(f"📊 Feedback: {feedback_value}")
        log_to_db(sender, "feedback", feedback_value, triage_level="FEEDBACK")
        r = MessagingResponse()
        r.message("Merci! 🙏 Votre avis aide WaziHealth à s'améliorer.\n\nPrenez soin de vous 💚")
        return str(r)

    # ── Layer 4: Triage AI ──────────────────────────────────
    ai_response  = get_ai_response(sender, incoming_text)
    triage_level = extract_triage_level(ai_response)
    condition    = detect_condition(ai_response)

    log_to_db(sender, "assistant", ai_response, triage_level=triage_level)
    print(f"🤖 Triage: {triage_level} | Condition: {condition}")

    r = MessagingResponse()
    r.message(ai_response)

    # Ressources — seulement sur diagnostic final VERT ou JAUNE
    if condition and triage_level in ["GREEN", "YELLOW"]:
        resources = get_resources(condition)
        if resources:
            r.message(f"📚 *{condition.replace('_', ' ').capitalize()}*\n\n{resources}")
            print(f"📚 Ressources: {condition}")

    # Audio — si message vocal reçu
    if is_audio:
        t = threading.Thread(target=send_audio_async, args=(sender, ai_response))
        t.daemon = True
        t.start()
        print("🔄 Thread audio démarré")

    return str(r)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
