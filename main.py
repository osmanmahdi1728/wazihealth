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

SYSTEM_PROMPT = """Tu es WaziHealth, assistant médical pour l'Afrique de l'Ouest.
Langue: français simple, phrases courtes, mots du quotidien.
Évite le jargon médical. Sois chaleureux et rassurant.

ÉTAPES OBLIGATOIRES — dans l'ordre:

ÉTAPE 1 — Symptôme principal
Pose cette question exactement:
"Qu'est-ce qui ne va pas?
1️⃣ Fièvre / chaleur
2️⃣ Douleur (tête, ventre, dos, articulations)
3️⃣ Ventre (nausées, vomissements, diarrhée)
4️⃣ Respiration (toux, souffle court)
5️⃣ Fatigue / faiblesse
6️⃣ Peau (boutons, démangeaisons, plaie)
7️⃣ Autre — décrivez"

ÉTAPE 2 — Depuis quand?
"Depuis quand?
1️⃣ Aujourd'hui — pas grave
2️⃣ Aujourd'hui — très intense
3️⃣ 1 à 3 jours — supportable
4️⃣ 1 à 3 jours — difficile
5️⃣ Plus de 3 jours"

ÉTAPE 3 — Signes en plus
Selon la réponse étape 1:

FIÈVRE → "Avez-vous aussi: (donnez les numéros)
1️⃣ Frissons / tremblements
2️⃣ Sueurs
3️⃣ Mal de tête
4️⃣ Courbatures
5️⃣ Nausées / vomissements
6️⃣ Nuque raide
7️⃣ Boutons / taches sur la peau
8️⃣ Yeux jaunes
9️⃣ Urine foncée"

DOULEUR VENTRE → "Avez-vous aussi:
1️⃣ Diarrhée
2️⃣ Vomissements
3️⃣ Fièvre
4️⃣ Sang dans les selles
5️⃣ Ventre gonflé
6️⃣ Brûlures en urinant
7️⃣ Pas de selles depuis 3+ jours"

TOUX → "Avez-vous aussi:
1️⃣ Fièvre
2️⃣ Crachats (jaunes/verts/avec sang)
3️⃣ Souffle court même au repos
4️⃣ Douleur poitrine
5️⃣ Sueurs la nuit
6️⃣ Perte de poids récente"

PEAU → "C'est quoi exactement:
1️⃣ Boutons / rougeurs
2️⃣ Démangeaisons
3️⃣ Plaie / blessure
4️⃣ Gonflement
5️⃣ Peau qui pèle
6️⃣ Taches blanches dans la bouche"

ÉTAPE 4 — Profil rapide
"Dernières questions:
• Âge: Bébé / Enfant / Adulte / Personne âgée?
• Femme enceinte? Oui / Non
• Voyage en brousse/village récemment? Oui / Non
• Autres malades autour de vous? Oui / Non"

ÉTAPE 5 — RÉPONSE FINALE
Seulement après les 4 étapes:

[emoji] [NIVEAU] — [titre simple]

🔍 Ce que j'observe: [symptômes en langage simple]

💊 En attendant:
   • [conseil simple 1]
   • [conseil simple 2]
   • ❌ Évitez: [ce qu'il ne faut pas faire]

🏥 À la pharmacie:
   • Demandez: "[mots exacts]"
   • Prix: ~[montant] CFA

👉 À faire maintenant: [action claire]

📞 Contactez:
   • [option 1]
   • [option 2]

💬 Voulez-vous parler à quelqu'un? Répondez OUI

⚠️ Ceci ne remplace pas un médecin.

NIVEAUX:
🟢 VERT = repos à la maison
🟡 JAUNE = pharmacie ou médecin dans 24h
🔴 ROUGE = urgence, partez maintenant

URGENCES IMMÉDIATES → ROUGE sans questions:
- Inconscient / ne répond pas
- Ne respire pas / souffle très difficile
- Fièvre + nuque raide (méningite possible)
- Bébé moins de 3 mois avec fièvre
- Convulsions / tremblements du corps entier
- Sang qui ne s'arrête pas
- Douleur poitrine forte + bras gauche
- Yeux jaunes + urine très foncée (foie)
- Femme enceinte avec saignement
- Perte de conscience

RÈGLES:
- Jamais diagnostiquer avant les 4 étapes
- Phrases courtes, simples
- Jamais prescrire antibiotiques
- Prix en CFA
- Section 💊 uniquement pour VERT et JAUNE"""

# ── Emergency keywords — étendu ────────────────────────────
CRITICAL_KEYWORDS = [
    # Conscience
    "inconscient", "sans connaissance", "ne répond pas", "ne répond plus",
    "évanoui", "tombe", "perdu connaissance",
    # Respiration
    "ne respire pas", "arrêt respiratoire", "étouffement", "étouffe",
    "souffle court au repos", "bleu", "lèvres bleues",
    # Cardiaque
    "arrêt cardiaque", "cœur arrêté", "douleur poitrine forte",
    "infarctus",
    # Neurologique
    "convulsions", "crise d'épilepsie", "tremble tout", "paralysé",
    "paralysie", "AVC", "bouche tordue", "visage tordu",
    # Saignement
    "saignement abondant", "beaucoup de sang", "hémorragie",
    "sang qui coule", "sang dans vomissements",
    # Intoxication
    "overdose", "empoisonnement", "avalé produit", "avalé médicaments",
    "intoxication",
    # Méningite
    "nuque raide", "raideur nuque", "ne peut pas baisser la tête",
    # Grossesse
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

# ── Resource library — étendue ─────────────────────────────
RESOURCES = {
    "paludisme": {
        "video":     "https://youtube.com/watch?v=7k8KfqUkMDU",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/malaria",
        "aliments":  "🥗 À manger:\n   • Bouillon de poulet\n   • Riz blanc\n   • Banane\n   • Eau de coco",
        "hydration": "💧 Buvez 3L d'eau par jour"
    },
    "typhoide": {
        "video":     "https://youtube.com/watch?v=3vMxVPcKDlY",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/typhoid",
        "aliments":  "🥗 À manger:\n   • Soupe légère\n   • Yaourt nature\n   • Pomme de terre cuite\n   • ❌ Pas d'épices",
        "hydration": "💧 SRO en pharmacie — priorité"
    },
    "diarrhee": {
        "video":     "https://youtube.com/watch?v=W0JQPMDPkBQ",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/diarrhoeal-disease",
        "aliments":  "🥗 À manger:\n   • Riz blanc\n   • Banane\n   • Pain grillé\n   • ❌ Pas de lait, pas de friture",
        "hydration": "💧 SRO — 1 sachet dans 1L d'eau"
    },
    "meningite": {
        "video":     "https://youtube.com/watch?v=MNhkTMUCHHw",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/meningitis",
        "aliments":  "🥗 Urgence — allez à l'hôpital",
        "hydration": "💧 Perfusion à l'hôpital uniquement"
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
        "hydration": "💧 Tisanes chaudes + 2L d'eau"
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
        "hydration": "💧 SRO en continu — allez au centre de santé"
    },
    "tuberculose": {
        "video":     "https://youtube.com/watch?v=K4eFSbFhRkE",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/tuberculosis",
        "aliments":  "🥗 À manger:\n   • Protéines (œufs, poisson)\n   • Légumes frais\n   • Fruits",
        "hydration": "💧 2L d'eau par jour"
    },
    "infection_urinaire": {
        "video":     "https://youtube.com/watch?v=QqSaIFp3k0Q",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/urinary-tract-infections",
        "aliments":  "🥗 À manger:\n   • Jus de canneberge\n   • Yaourt nature\n   • ❌ Pas d'alcool ni café",
        "hydration": "💧 Buvez beaucoup — 3L minimum"
    },
    "hypertension": {
        "video":     "https://youtube.com/watch?v=ab5p3B5XJBE",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/hypertension",
        "aliments":  "🥗 À manger:\n   • Légumes frais\n   • Banane\n   • Poisson\n   • ❌ Pas de sel, pas de friture",
        "hydration": "💧 2L d'eau par jour"
    },
    "diabete": {
        "video":     "https://youtube.com/watch?v=9OvSIFZMfmI",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/diabetes",
        "aliments":  "🥗 À manger:\n   • Légumes verts\n   • Poisson grillé\n   • ❌ Pas de sucre, pas de jus sucré",
        "hydration": "💧 Eau uniquement — pas de sodas"
    },
    "malnutrition": {
        "video":     "https://youtube.com/watch?v=GlxiUkEFtL4",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/malnutrition",
        "aliments":  "🥗 À manger:\n   • Légumineuses (niébé, lentilles)\n   • Œufs\n   • Poisson\n   • Arachides",
        "hydration": "💧 Eau propre — 2L par jour"
    },
    "conjonctivite": {
        "video":     "https://youtube.com/watch?v=xMEMWKHHcCk",
        "info":      "https://www.who.int/fr/",
        "aliments":  "🥗 Pas de régime spécial\n   • Lavez les mains souvent\n   • ❌ Ne frottez pas les yeux",
        "hydration": "💧 Normal"
    },
    "paludisme_enfant": {
        "video":     "https://youtube.com/watch?v=7k8KfqUkMDU",
        "info":      "https://www.who.int/fr/news-room/fact-sheets/detail/malaria",
        "aliments":  "🥗 Pour enfant:\n   • Lait maternel si nourrisson\n   • Bouillon léger\n   • Eau de coco",
        "hydration": "💧 SRO si diarrhée — pharmacie"
    }
}

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
        "paludisme":         ["paludisme", "malaria", "tdr paludisme"],
        "paludisme_enfant":  ["paludisme enfant", "malaria enfant", "paludisme bébé"],
        "typhoide":          ["typhoïde", "typhoide", "fièvre typhoïde"],
        "diarrhee":          ["diarrhée", "diarrhee", "gastro-entérite", "sro"],
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
    if not condition or condition not in RESOURCES:
        return None
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

def is_handoff_request(sender, message):
    if message.strip().upper() not in ["OUI","OUI.","OUI!"]: return False
    if sender not in conversations or not conversations[sender]: return False
    return "agent" in conversations[sender][-1]["content"].lower()

def is_critical(message):
    return any(k in message.lower() for k in CRITICAL_KEYWORDS)

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
                {"role": "system", "content": "Résume en 2-3 phrases très courtes et simples pour un message vocal. Garde l'urgence et l'action principale."},
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
        print(f"✅ Audio envoyé")
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
        else:
            r = MessagingResponse()
            r.message("Envoyez un texte ou message vocal 🎤")
            return str(r)

    if not incoming_text:
        r = MessagingResponse()
        r.message("👋 Bonjour! Je suis WaziHealth.\nDites-moi ce qui ne va pas — texte ou vocal 🎤")
        return str(r)

    print(f"📩 {hash_sender(sender)}: {incoming_text}")
    log_to_db(sender, "user", incoming_text)

    if is_critical(incoming_text):
        print("🚨 CRITIQUE")
        log_to_db(sender, "assistant", EMERGENCY_RESPONSE, triage_level="RED", is_emergency=True)
        conversations.pop(sender, None)
        r = MessagingResponse()
        r.message(EMERGENCY_RESPONSE)
        return str(r)

    if is_handoff_request(sender, incoming_text):
        print("👤 Handoff")
        log_to_db(sender, "system", "HUMAN_HANDOFF_REQUESTED", triage_level="HANDOFF")
        conversations.pop(sender, None)
        r = MessagingResponse()
        r.message(HANDOFF_RESPONSE)
        return str(r)

    ai_response  = get_ai_response(sender, incoming_text)
    triage_level = extract_triage_level(ai_response)
    condition    = detect_condition(ai_response)

    log_to_db(sender, "assistant", ai_response, triage_level=triage_level)
    print(f"🤖 Triage: {triage_level} | Condition: {condition}")

    r = MessagingResponse()
    r.message(ai_response)

    if condition and triage_level in ["GREEN", "YELLOW"]:
        resources = get_resources(condition)
        if resources:
            r.message(f"📚 *{condition.replace('_', ' ').capitalize()}*\n\n{resources}")
            print(f"📚 Ressources: {condition}")

    if is_audio:
        t = threading.Thread(target=send_audio_async, args=(sender, ai_response))
        t.daemon = True
        t.start()
        print("🔄 Thread audio démarré")

    return str(r)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
