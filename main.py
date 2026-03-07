import os, hashlib, tempfile, threading, requests, re, json
import schedule, time as time_module
from datetime import date, timedelta, datetime
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

# ── Config institution ─────────────────────────────────────
INSTITUTION_NAME = os.environ.get("INSTITUTION_NAME", "WaziHealth")
BOOKING_LINK     = os.environ.get("BOOKING_LINK", "https://maps.google.com/?q=hopital")
AGENT_NUMBER     = os.environ.get("AGENT_NUMBER", "")
DOCTOR_NUMBERS = [
    n.strip() for n in
    os.environ.get("DOCTOR_NUMBERS", "").split(",")
    if n.strip()
]

def is_doctor(sender):
    """Vérifie si le sender est un médecin autorisé."""
    if not DOCTOR_NUMBERS:
        return sender == AGENT_NUMBER.replace("whatsapp:", "")
    return any(sender.endswith(n.replace("+", "")) for n in DOCTOR_NUMBERS)

SYSTEM_PROMPT = f"""Tu es WaziHealth, assistant de triage médical pour {INSTITUTION_NAME}.
Tu travailles pour {INSTITUTION_NAME} — clinique, cabinet médical ou pharmacie.
Tu parles comme un infirmier bienveillant — simple, clair, rassurant.
Jamais de termes médicaux complexes. Phrases courtes.

MISSION: Orienter chaque patient vers la bonne ressource en moins de 4 échanges.

NIVEAUX:
🟢 VERT   → Pharmacie directement — pas besoin de médecin
🟡 JAUNE  → Téléconsultation ou rendez-vous médical
🔴 ROUGE  → Urgences hospitalières — déplacement immédiat

━━━━━━━━━━━━━━━━━━━━━━
ÉCHANGE 0 — Profil de base (UNE SEULE FOIS)
━━━━━━━━━━━━━━━━━━━━━━
"Bonjour! Je suis l'assistant de {INSTITUTION_NAME}.
 Pour mieux vous aider:
 1️⃣ Adulte (18-60 ans) en bonne santé générale
 2️⃣ Enfant (2-17 ans)
 3️⃣ Autre profil"

Si 1️⃣ ou 2️⃣ → note le profil, passe à ÉCHANGE 1 immédiatement
Si 3️⃣ → pose cette seule question:
"Précisez:
 1️⃣ Femme enceinte
 2️⃣ Personne âgée (60 ans et plus)
 3️⃣ Bébé (moins de 2 ans)
 4️⃣ Maladie chronique (diabète, tension, asthme...)
 5️⃣ Autre — décrivez en un mot"

Si 4️⃣ → "Quelle maladie?" → note la réponse → ÉCHANGE 1
RÈGLE: Ne jamais reposer le profil si déjà donné.

━━━━━━━━━━━━━━━━━━━━━━
ÉCHANGE 1 — Symptôme principal + durée
━━━━━━━━━━━━━━━━━━━━━━
"Qu'est-ce qui ne va pas et depuis quand?
 1️⃣ Fièvre / chaleur — depuis aujourd'hui
 2️⃣ Fièvre / chaleur — depuis 2 jours ou plus
 3️⃣ Douleur (tête, ventre, dos, poitrine)
 4️⃣ Toux / difficultés à respirer
 5️⃣ Problèmes digestifs (diarrhée, vomissements)
 6️⃣ Problème de peau (boutons, plaie, démangeaisons)
 7️⃣ Fatigue / faiblesse importante
 8️⃣ Autre — décrivez en un mot"

━━━━━━━━━━━━━━━━━━━━━━
ÉCHANGE 2 — Signes associés (selon symptôme)
━━━━━━━━━━━━━━━━━━━━━━
Pose UNE question avec 5 choix max selon ÉCHANGE 1:

Si FIÈVRE:
"Avez-vous aussi:
 1️⃣ Frissons ou tremblements
 2️⃣ Sueurs importantes
 3️⃣ Mal de tête fort
 4️⃣ Yeux qui jaunissent
 5️⃣ Urine très foncée"

Si DOULEUR:
"La douleur est:
 1️⃣ Tête — forte ou qui dure
 2️⃣ Ventre — avec ou sans fièvre
 3️⃣ Poitrine — avec essoufflement
 4️⃣ Dos — avec ou sans fièvre
 5️⃣ Plusieurs endroits en même temps"

Si TOUX:
"Avec la toux:
 1️⃣ Crachats jaunes ou verts
 2️⃣ Sang dans les crachats
 3️⃣ Fièvre en même temps
 4️⃣ Essoufflement même au repos
 5️⃣ Perte de poids récente"

Si DIGESTIF:
"Avec les problèmes digestifs:
 1️⃣ Diarrhée — combien de fois aujourd'hui?
 2️⃣ Vomissements
 3️⃣ Fièvre en même temps
 4️⃣ Sang dans les selles
 5️⃣ Ventre très gonflé et douloureux"

Si PEAU:
"Sur la peau:
 1️⃣ Boutons rouges qui démangent
 2️⃣ Plaie ou blessure
 3️⃣ Gonflement localisé
 4️⃣ Peau qui pèle ou craque
 5️⃣ Taches ou décoloration"

━━━━━━━━━━━━━━━━━━━━━━
DIAGNOSTIC FINAL — FORMAT PAR NIVEAU
━━━━━━━━━━━━━━━━━━━━━━

── FORMAT VERT ──────────────────────────
🟢 [Condition probable]

🔍 Ce que ça ressemble à:
[1-2 phrases simples]

💊 En attendant:
   • [action 1]
   • [action 2]
   • ❌ Évitez: [1 chose]

🍽️ Quoi manger et boire:
   • [aliment 1] / [aliment 2]
   • 💧 [conseil hydratation]

🏥 Allez directement à la pharmacie:
   • Montrez ce message au pharmacien
   • Demandez: "[mots exacts]"
   • Prix estimé: ~[montant] CFA
   • 📍 https://maps.google.com/?q=pharmacie

📚 En savoir plus:
   • 🌐 [lien WHO]
   • 📹 [lien YouTube]

━━━━━━━━━━━━━━━━━━━━━━
💬 Besoin d'aide supplémentaire?
   Répondez *AGENT* pour parler à quelqu'un
🙏 Cette réponse vous a-t-elle aidé?
1️⃣ Oui   2️⃣ Partiellement   3️⃣ Non
━━━━━━━━━━━━━━━━━━━━━━
⚠️ Ceci ne remplace pas un médecin.

── FORMAT JAUNE ─────────────────────────
🟡 [Condition probable — consultation recommandée]

🔍 Ce que ça ressemble à:
[1-2 phrases simples]

💊 En attendant votre consultation:
   • [action 1]
   • [action 2]
   • ❌ Évitez: [1 chose]

🍽️ Quoi manger et boire:
   • [aliment 1] / [aliment 2]
   • 💧 [conseil hydratation]

📋 Résumé pour votre médecin:
   • Symptôme: [résumé court]
   • Depuis: [durée]
   • Signes associés: [liste courte]
   • Profil: [âge/situation]

📅 Prenez rendez-vous:
   Répondez *RENDEZ-VOUS* pour choisir
   un créneau directement ici

📚 En savoir plus:
   • 🌐 [lien WHO]
   • 📹 [lien YouTube]

━━━━━━━━━━━━━━━━━━━━━━
💬 Besoin d'aide supplémentaire?
   Répondez *AGENT* pour parler à quelqu'un
🙏 Cette réponse vous a-t-elle aidé?
1️⃣ Oui   2️⃣ Partiellement   3️⃣ Non
━━━━━━━━━━━━━━━━━━━━━━
⚠️ Ceci ne remplace pas un médecin.

── FORMAT ROUGE ─────────────────────────
🔴 [Situation grave — allez aux urgences]

⚠️ Ne restez pas seul(e).
Allez aux urgences maintenant ou appelez le 15.

🚨 En attendant les secours:
   • [1 seule action immédiate et simple]
   • ❌ Ne prenez rien sans avis médical

📍 Urgences les plus proches:
   https://maps.google.com/?q=urgences+hopital

Un agent va vous contacter dans les prochaines minutes.

URGENCES → ROUGE IMMÉDIAT sans questions:
- Inconscient / ne répond pas
- Ne respire pas / lèvres bleues
- Convulsions
- Fièvre + nuque qui ne plie pas
- Bébé moins de 3 mois avec fièvre
- Saignement qui ne s'arrête pas
- Douleur poitrine forte
- Yeux jaunes + urine marron foncé
- Femme enceinte avec saignement
- Bouche tordue / bras qui ne bouge plus

RÈGLES ABSOLUES:
- Profil demandé UNE SEULE FOIS au début
- Max 2 échanges après profil (3 si réponses vagues)
- UNE seule question par échange
- Jamais de termes médicaux complexes
- Jamais prescrire antibiotiques
- VERT uniquement → section pharmacie
- JAUNE uniquement → résumé médecin + RDV
- ROUGE → urgences + agent immédiat, pas de feedback
- Prix en CFA
- Toujours adapter au profil donné en ÉCHANGE 0"""

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
        "video":     "<https://youtube.com/watch?v=7k8KfqUkMDU>",
        "info":      "<https://www.who.int/fr/news-room/fact-sheets/detail/malaria>",
        "aliments":  "🥗 À manger:\n   • Bouillon de poulet\n   • Riz blanc\n   • Banane\n   • Eau de coco",
        "hydration": "💧 3L d'eau par jour"
    },
    "paludisme_enfant": {
        "video":     "<https://youtube.com/watch?v=7k8KfqUkMDU>",
        "info":      "<https://www.who.int/fr/news-room/fact-sheets/detail/malaria>",
        "aliments":  "🥗 Pour enfant:\n   • Lait maternel si nourrisson\n   • Bouillon léger\n   • Eau de coco",
        "hydration": "💧 SRO si diarrhée — pharmacie"
    },
    "typhoide": {
        "video":     "<https://youtube.com/watch?v=3vMxVPcKDlY>",
        "info":      "<https://www.who.int/fr/news-room/fact-sheets/detail/typhoid>",
        "aliments":  "🥗 À manger:\n   • Soupe légère\n   • Yaourt nature\n   • Pomme de terre cuite\n   • ❌ Pas d'épices",
        "hydration": "💧 SRO en pharmacie"
    },
    "diarrhee": {
        "video":     "<https://youtube.com/watch?v=W0JQPMDPkBQ>",
        "info":      "<https://www.who.int/fr/news-room/fact-sheets/detail/diarrhoeal-disease>",
        "aliments":  "🥗 À manger:\n   • Riz blanc\n   • Banane\n   • Pain grillé\n   • ❌ Pas de lait ni friture",
        "hydration": "💧 SRO — 1 sachet dans 1L d'eau"
    },
    "meningite": {
        "video":     "<https://youtube.com/watch?v=MNhkTMUCHHw>",
        "info":      "<https://www.who.int/fr/news-room/fact-sheets/detail/meningitis>",
        "aliments":  "🥗 Urgence — allez à l'hôpital",
        "hydration": "💧 Perfusion à l'hôpital"
    },
    "dengue": {
        "video":     "<https://youtube.com/watch?v=k3EQCn9GPCU>",
        "info":      "<https://www.who.int/fr/news-room/fact-sheets/detail/dengue-and-severe-dengue>",
        "aliments":  "🥗 À manger:\n   • Jus de papaye\n   • Soupe\n   • Fruits frais\n   • ❌ Pas d'aspirine",
        "hydration": "💧 3-4L d'eau ou jus par jour"
    },
    "grippe": {
        "video":     "<https://youtube.com/watch?v=OvNNsR5FXKU>",
        "info":      "<https://www.who.int/fr/news-room/fact-sheets/detail/influenza-(seasonal>)",
        "aliments":  "🥗 À manger:\n   • Soupe chaude\n   • Miel + citron\n   • Gingembre\n   • Bouillon",
        "hydration": "💧 Tisanes + 2L d'eau"
    },
    "deshydratation": {
        "video":     "<https://youtube.com/watch?v=9iMGFqMmUFs>",
        "info":      "<https://www.who.int/fr/news-room/fact-sheets/detail/diarrhoeal-disease>",
        "aliments":  "🥗 À boire:\n   • Eau de coco\n   • Bouillon salé\n   • Pastèque, concombre",
        "hydration": "💧 SRO immédiatement"
    },
    "cholera": {
        "video":     "<https://youtube.com/watch?v=YFKQ4R7MYXI>",
        "info":      "<https://www.who.int/fr/news-room/fact-sheets/detail/cholera>",
        "aliments":  "🥗 Urgence — SRO immédiatement",
        "hydration": "💧 SRO en continu — centre de santé"
    },
    "tuberculose": {
        "video":     "<https://youtube.com/watch?v=K4eFSbFhRkE>",
        "info":      "<https://www.who.int/fr/news-room/fact-sheets/detail/tuberculosis>",
        "aliments":  "🥗 À manger:\n   • Œufs\n   • Poisson\n   • Légumes frais\n   • Fruits",
        "hydration": "💧 2L d'eau par jour"
    },
    "infection_urinaire": {
        "video":     "<https://youtube.com/watch?v=QqSaIFp3k0Q>",
        "info":      "<https://www.who.int/fr/>",
        "aliments":  "🥗 À manger:\n   • Yaourt nature\n   • ❌ Pas d'alcool ni café",
        "hydration": "💧 3L d'eau minimum"
    },
    "hypertension": {
        "video":     "<https://youtube.com/watch?v=ab5p3B5XJBE>",
        "info":      "<https://www.who.int/fr/news-room/fact-sheets/detail/hypertension>",
        "aliments":  "🥗 À manger:\n   • Légumes frais\n   • Banane\n   • Poisson\n   • ❌ Pas de sel ni friture",
        "hydration": "💧 2L d'eau par jour"
    },
    "diabete": {
        "video":     "<https://youtube.com/watch?v=9OvSIFZMfmI>",
        "info":      "<https://www.who.int/fr/news-room/fact-sheets/detail/diabetes>",
        "aliments":  "🥗 À manger:\n   • Légumes verts\n   • Poisson grillé\n   • ❌ Pas de sucre ni sodas",
        "hydration": "💧 Eau uniquement"
    },
    "malnutrition": {
        "video":     "<https://youtube.com/watch?v=GlxiUkEFtL4>",
        "info":      "<https://www.who.int/fr/news-room/fact-sheets/detail/malnutrition>",
        "aliments":  "🥗 À manger:\n   • Niébé, lentilles\n   • Œufs\n   • Poisson\n   • Arachides",
        "hydration": "💧 Eau propre — 2L par jour"
    },
    "conjonctivite": {
        "video":     "<https://youtube.com/watch?v=xMEMWKHHcCk>",
        "info":      "<https://www.who.int/fr/>",
        "aliments":  "🥗 Lavez les mains souvent\n   • ❌ Ne frottez pas les yeux",
        "hydration": "💧 Normal"
    },
}

FEEDBACK_CHOICES = {"1": "utile", "2": "partiel", "3": "non_utile"}

# ── Template SIDs (depuis Render) ─────────────────────────
TEMPLATE_PROFIL_SID    = os.environ.get("TEMPLATE_PROFIL_SID", "")
TEMPLATE_SYMPTOMES_SID = os.environ.get("TEMPLATE_SYMPTOMES_SID", "")
TEMPLATE_FEEDBACK_SID  = os.environ.get("TEMPLATE_FEEDBACK_SID", "")
TEMPLATE_RDV_SID       = os.environ.get("TEMPLATE_RDV_SID", "")

# ── Booking config ─────────────────────────────────────────
# Heures viennent dynamiquement de Supabase (slots table)

# ── Welcome audio ──────────────────────────────────────────
WELCOME_AUDIO_URL = None

def get_or_create_welcome_audio():
    global WELCOME_AUDIO_URL
    if WELCOME_AUDIO_URL:
        return WELCOME_AUDIO_URL
    try:
        print("🎤 Génération audio de bienvenue...")
        welcome_text = (
            "Bonjour! Je suis WaziHealth, votre assistant santé. "
            "Je suis là pour vous aider à comprendre vos symptômes "
            "et vous orienter vers les meilleurs soins. "
            "Vous pouvez m'envoyer un message vocal, une photo, "
            "ou écrire en texte. Je vous accompagne pas à pas. "
            "Dites-moi ce qui ne va pas."
        )
        tts = openai_client.audio.speech.create(
            model="tts-1", voice="nova", input=welcome_text, speed=0.85
        )
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(tts.content)
            tmp_path = tmp.name
        upload = cloudinary.uploader.upload(
            tmp_path,
            resource_type="video",
            folder="wazihealth/audio",
            public_id="welcome_message"
        )
        WELCOME_AUDIO_URL = upload["secure_url"]
        print(f"✅ Audio bienvenue prêt: {WELCOME_AUDIO_URL}")
        return WELCOME_AUDIO_URL
    except Exception as e:
        print(f"❌ Welcome audio error: {e}")
        return None

def send_welcome_audio(sender):
    def _send():
        audio_url = get_or_create_welcome_audio()
        if not audio_url: return
        try:
            twilio_client.messages.create(
                from_=os.environ.get("TWILIO_WHATSAPP_NUMBER"),
                to=sender,
                media_url=[audio_url],
                body="🎤"
            )
            print(f"✅ Audio bienvenue envoyé")
        except Exception as e:
            print(f"❌ Welcome send error: {e}")
    t = threading.Thread(target=_send, daemon=True)
    t.start()

# ── Mapping List/Button IDs → numéros existants ───────────
SYMPTOM_ID_MAP = {
    "fievre_today": "1",
    "fievre_2j":    "2",
    "douleur":      "3",
    "toux":         "4",
    "digestif":     "5",
    "peau":         "6",
    "fatigue":      "7",
    "autre":        "8",
}

PROFILE_BUTTON_MAP = {
    "adulte 18-60 ans": "1",
    "enfant 2-17 ans":  "2",
    "autre profil":     "3",
}

FEEDBACK_BUTTON_MAP = {
    "oui":  "1",
    "non":  "3",
}


def send_template(to, template_sid, variables=None):
    """Envoie un template WhatsApp interactif via Twilio."""
    if not template_sid:
        return False
    try:
        twilio_client.messages.create(
            from_=os.environ.get("TWILIO_WHATSAPP_NUMBER"),
            to=to,
            content_sid=template_sid,
            content_variables=json.dumps(variables or {"1": " "})
        )
        print(f"✅ Template envoyé: {template_sid}")
        return True
    except Exception as e:
        print(f"⚠️ Template error (fallback texte): {e}")
        return False


def normalize_response(req):
    """
    Normalise la réponse vers le format texte existant.
    Gère: bouton Quick Reply / List Message / texte libre.
    """
    button  = req.form.get("ButtonPayload", "").strip().lower()
    list_id = req.form.get("ListId", "").strip().lower()
    text    = req.form.get("Body", "").strip()

    if button and button in PROFILE_BUTTON_MAP:
        return PROFILE_BUTTON_MAP[button]
    if button and button in FEEDBACK_BUTTON_MAP:
        return FEEDBACK_BUTTON_MAP[button]
    if list_id and list_id in SYMPTOM_ID_MAP:
        return SYMPTOM_ID_MAP[list_id]
    if list_id and list_id.startswith("slot_"):
        return list_id
    if button:
        return button
    return text


def send_booking_list(sender):
    """
    Envoie un List Message dynamique avec les créneaux Supabase.
    Les créneaux sont injectés comme variables {{1}} {{2}}...
    Fallback texte si template non configuré ou erreur.
    """
    slots = get_available_slots()
    if not slots:
        twilio_client.messages.create(
            from_=os.environ.get("TWILIO_WHATSAPP_NUMBER"),
            to=sender,
            body=(
                "Aucun créneau disponible pour le moment.\n\n"
                "Le médecin vous contactera dans les 24h.\n"
                "Nous vous notifierons dès qu'un créneau se libère."
            )
        )
        return []

    if TEMPLATE_RDV_SID:
        variables = {}
        for i, slot in enumerate(slots[:5], 1):
            variables[str(i)] = f"{slot['date']} à {slot['time']}"
        success = send_template(sender, TEMPLATE_RDV_SID, variables)
        if success:
            conversations.setdefault(sender, []).append({
                "role":    "system",
                "content": f"AVAILABLE_SLOTS:{json.dumps(slots[:5])}"
            })
            print(f"✅ List Message RDV envoyé avec {len(slots[:5])} créneaux")
            return slots[:5]

    booking_msg, fallback_slots = get_booking_start_message()
    twilio_client.messages.create(
        from_=os.environ.get("TWILIO_WHATSAPP_NUMBER"),
        to=sender,
        body=booking_msg
    )
    if fallback_slots:
        conversations.setdefault(sender, []).append({
            "role":    "system",
            "content": f"AVAILABLE_SLOTS:{json.dumps(fallback_slots)}"
        })
    return fallback_slots or []


def notify_agent(sender, summary, triage_level="RED"):
    """Notifie l'agent/médecin avec le bon niveau."""
    if not AGENT_NUMBER:
        print("⚠️ AGENT_NUMBER non configuré")
        return
    try:
        if triage_level == "RED":
            header = "🚨 *URGENCE — ACTION IMMÉDIATE*"
            separator = "🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴"
        elif triage_level == "YELLOW":
            header = "🆕 *NOUVEAU RDV*"
            separator = "━━━━━━━━━━━━━━━━━━━━━━"
        else:
            header = "ℹ️ *INFO PATIENT*"
            separator = "━━━━━━━━━━━━━━━━━━━━━━"
        body = (
            f"{separator}\n"
            f"{header} — {INSTITUTION_NAME}\n"
            f"{separator}\n\n"
            f"{summary}\n\n"
            f"{separator}"
        )
        twilio_client.messages.create(
            from_=os.environ.get("TWILIO_WHATSAPP_NUMBER"),
            to=AGENT_NUMBER,
            body=body
        )
        print(f"✅ Agent notifié ({triage_level}): {AGENT_NUMBER}")
    except Exception as e:
        print(f"❌ Agent notify error: {e}")

# ── Helpers ────────────────────────────────────────────────
def hash_sender(s):
    return hashlib.sha256(s.encode()).hexdigest()[:16]

def extract_triage_level(r):
    if "🔴" in r: return "RED"
    if "🟡" in r: return "YELLOW"
    if "🟢" in r: return "GREEN"
    return "PENDING"

def detect_condition(ai_response):
    r = ai_response.lower()
    conditions = {
        "paludisme_enfant":  ["paludisme enfant", "malaria enfant", "paludisme bébé"],
        "paludisme":         ["paludisme", "malaria", "tdr"],
        "typhoide":          ["typhoïde", "typhoide", "fièvre typhoïde"],
        "diarrhee":          ["diarrhée", " diarrhee ", "gastro-entérite", " sro "],
        "meningite":         ["méningite", "meningite"],
        "dengue":            ["dengue"],
        "grippe":            ["grippe", "influenza"],
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
    """Handoff uniquement si OUI après question agent explicite."""
    if message.strip().upper() not in ["OUI", "OUI.", "OUI!"]:
        return False
    if sender not in conversations or not conversations[sender]:
        return False
    last = conversations[sender][-1]["content"].lower()
    # Handoff seulement si la dernière question demande explicitement OUI/NON
    return "répondez oui ou non" in last

def is_profile_response(sender, message):
    """Détecte si le message est une réponse au profil."""
    if message.strip() not in ["1", "2", "3", "4"]:
        return False
    if sender not in conversations or not conversations[sender]:
        return False
    last = conversations[sender][-1]["content"].lower()
    return (
        "qui consulte" in last or
        "mode patient" in last or
        "qui consulte aujourd" in last
    )


def is_feedback(sender, message):
    """Feedback sur 1/2/3 après question feedback."""
    if message.strip() not in ["1", "2", "3"]:
        return False
    if sender not in conversations or not conversations[sender]:
        return False
    last = conversations[sender][-1]["content"].lower()
    return "cette réponse vous a-t-elle aidé" in last

# ── Booking functions ──────────────────────────────────────
def get_available_slots():
    """Récupère les créneaux disponibles depuis Supabase."""
    try:
        result = supabase.table("slots")\
            .select("*")\
            .eq("is_booked", False)\
            .gte("date", str(date.today()))\
            .order("date")\
            .order("time")\
            .limit(6)\
            .execute()
        return result.data or []
    except Exception as e:
        print(f"❌ Slots error: {e}")
        return []

def get_booking_start_message():
    """Génère le menu de créneaux dynamique depuis Supabase."""
    slots = get_available_slots()
    if not slots:
        return (
            "📅 Aucun créneau disponible pour le moment.\n\n"
            "Le médecin vous contactera dans les 24h.\n"
            "Nous vous notifierons dès qu'un créneau se libère."
        ), []
    msg = "📅 *Prendre rendez-vous*\n\nChoisissez un créneau:\n"
    for i, slot in enumerate(slots[:5], 1):
        msg += f"{i}️⃣ {slot['date']} à {slot['time']}\n"
    return msg, slots

def book_slot(sender, slot, symptoms):
    """Réserve un créneau — Supabase + notifie médecin."""
    try:
        supabase.table("slots")\
            .update({"is_booked": True})\
            .eq("id", slot["id"])\
            .execute()
        supabase.table("appointments").insert({
            "session_hash": hash_sender(sender),
            "slot_id":      slot["id"],
            "date":         slot["date"],
            "time":         slot["time"],
            "triage_level": "YELLOW",
            "symptoms":     symptoms[:200],
            "status":       "confirmed"
        }).execute()
        print(f"✅ RDV créé: {slot['date']} à {slot['time']}")
        patient_id = hash_sender(sender)[:4].upper()
        notify_agent(
            sender,
            (
                f"*{slot['date']} — {slot['time']}*\n"
                f"👤 #{patient_id}\n\n"
                f"📋 _{symptoms}_\n\n"
                f"📞 Appelez *#{patient_id}* à *{slot['time']}*\n"
                f"✅ *TRAITÉ {patient_id}* après l'appel"
            ),
            triage_level="YELLOW"
        )
        return True
    except Exception as e:
        print(f"❌ Booking error: {e}")
        return False

def parse_doctor_availability(message):
    """
    Supporte:
    - DISPO demain 9h 10h 14h
    - DISPO aujourd'hui 16h 17h
    - DISPO semaine 9h 10h        → lundi au vendredi
    - DISPO lundi 9h 10h
    - DISPO 2026-03-10 9h 10h
    Génère des slots de 30 min automatiquement.
    """
    msg = message.lower()
    slots_to_create = []

    target_dates = []
    if "semaine" in msg:
        today = date.today()
        days_until_monday = (7 - today.weekday()) % 7 or 7
        next_monday = today + timedelta(days=days_until_monday)
        target_dates = [next_monday + timedelta(days=i) for i in range(5)]
    elif "demain" in msg:
        target_dates = [date.today() + timedelta(days=1)]
    elif "aujourd'hui" in msg or "auj" in msg:
        target_dates = [date.today()]
    elif "dans 2" in msg:
        target_dates = [date.today() + timedelta(days=2)]
    else:
        jours = {
            "lundi": 0, "mardi": 1, "mercredi": 2,
            "jeudi": 3, "vendredi": 4, "samedi": 5, "dimanche": 6
        }
        for jour, weekday in jours.items():
            if jour in msg:
                today = date.today()
                days_ahead = (weekday - today.weekday()) % 7 or 7
                target_dates = [today + timedelta(days=days_ahead)]
                break

        date_match = re.search(r'\d{4}-\d{2}-\d{2}', msg)
        if date_match:
            target_dates = [datetime.strptime(date_match.group(), "%Y-%m-%d").date()]

    if not target_dates:
        return "❌ Format non reconnu.\nExemples:\n• DISPO demain 9h 10h 14h\n• DISPO semaine 9h 14h\n• DISPO lundi 10h 11h"

    range_match = re.search(r'(\d{1,2})h?\s*[àa\-]\s*(\d{1,2})h', msg)
    if range_match:
        start_h = int(range_match.group(1))
        end_h   = int(range_match.group(2))
        h, m = start_h, 0
        while (h < end_h) or (h == end_h and m == 0):
            slots_to_create.append(f"{h}h{m:02d}")
            m += 30
            if m >= 60:
                m = 0
                h += 1
    else:
        raw_times = re.findall(r'\d{1,2}h\d{0,2}', msg)
        for t in raw_times:
            parts = t.replace("h", ":").split(":")
            hh = int(parts[0])
            mm = int(parts[1]) if len(parts) > 1 and parts[1] else 0
            slots_to_create.append(f"{hh}h{mm:02d}")
            mm2 = mm + 30
            hh2 = hh + mm2 // 60
            mm2 = mm2 % 60
            slots_to_create.append(f"{hh2}h{mm2:02d}")

    if not slots_to_create:
        return "❌ Aucune heure trouvée.\nExemple: DISPO demain 9h 10h 14h\nOU: DISPO semaine 9h à 12h"

    count = 0
    for target_date in target_dates:
        for slot_time in slots_to_create:
            existing = supabase.table("slots")\
                .select("id")\
                .eq("date", str(target_date))\
                .eq("time", slot_time)\
                .execute()\
                .data
            if not existing:
                supabase.table("slots").insert({
                    "doctor_id": "default",
                    "date":      str(target_date),
                    "time":      slot_time,
                    "is_booked": False
                }).execute()
                count += 1

    if len(target_dates) == 1:
        return f"✅ {count} créneau(x) ajouté(s) pour le {target_dates[0]}"
    else:
        dates_str = f"{target_dates[0]} → {target_dates[-1]}"
        return f"✅ {count} créneau(x) ajouté(s) du {dates_str}"


def detect_intent(sender, message):
    """
    Détecte l'intention du message via GPT.
    Retourne un dict avec l'intention et les paramètres.
    """
    is_doc = is_doctor(sender)
    is_agt = AGENT_NUMBER and sender.replace("whatsapp:", "") in AGENT_NUMBER
    role = "médecin" if is_doc else ("agent" if is_agt else "patient")

    try:
        result = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "system",
                "content": f"""Tu analyses des messages WhatsApp pour WaziHealth.
L'expéditeur est: {role}
Retourne UNIQUEMENT un JSON avec:
{{
  "intent": "dispo|annuler|file|traite|aide|booking|agent|reset|urgence|patient",
  "params": {{}}
}}

Intentions possibles:
- "dispo"    → médecin veut ajouter des disponibilités
              params: {{"raw": "le message original"}}
- "annuler"  → médecin veut annuler un créneau
              params: {{"raw": "le message original"}}
- "file"     → voir la liste des RDV du jour
- "traite"   → marquer un patient comme traité
              params: {{"patient_id": "XXXX ou vide"}}
- "aide"     → demande le guide des commandes
- "booking"  → patient veut prendre un RDV
- "agent"    → patient veut parler à un humain
- "reset"    → recommencer la conversation
- "urgence"  → situation d'urgence médicale
- "patient"  → message patient normal (symptômes etc.)

Exemples:
"j'ai du temps demain de 9 à 12"     → dispo
"je suis libre lundi matin"           → dispo
"annule le 10h"                       → annuler
"montre moi mes rendez vous"          → file
"mes prochains patients"              → file
"agenda de la semaine"                → file
"j'ai appelé le patient A3F2"         → traite
"je veux un rdv"                      → booking
"parler à quelqu'un"                  → agent
"il s'est évanoui"                    → urgence
"""
            }, {
                "role": "user",
                "content": message
            }],
            max_tokens=100,
            temperature=0
        ).choices[0].message.content

        result = result.strip()
        if "```" in result:
            result = result.split("```")[1].replace("json", "").strip()
        return json.loads(result)

    except Exception as e:
        print(f"❌ Intent detection error: {e}")
        return {"intent": "patient", "params": {}}


def send_queue_to_doctor(requester=None):
    """
    Si requester fourni → envoie seulement à lui.
    Si requester None → envoie à tous (appel automatique 8h).
    """
    try:
        appointments = supabase.table("appointments")\
            .select("*")\
            .eq("date", str(date.today()))\
            .order("time")\
            .execute()\
            .data

        if not appointments:
            msg = "📋 Aucun RDV aujourd'hui ✅"
        else:
            msg  = f"━━━━━━━━━━━━━━━━━━━━━━\n"
            msg += f"📋 *FILE — {date.today().strftime('%d/%m')}*\n"
            msg += f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            for apt in appointments:
                emoji  = "🔴" if apt["triage_level"] == "RED" else "🟡"
                pid    = apt["session_hash"][:4].upper()
                status = "✅" if apt["status"] == "treated" else "⏳"
                short  = apt["symptoms"].split("\n")[0][:50] if apt["symptoms"] else "—"
                msg += f"{emoji} *{apt['time']}* {status} #{pid}\n_{short}_\n\n"
            treated = sum(1 for a in appointments if a["status"] == "treated")
            msg += f"━━━━━━━━━━━━━━━━━━━━━━\n"
            msg += f"*{len(appointments)} RDV — ✅{treated} ⏳{len(appointments)-treated}*"

        if requester:
            twilio_client.messages.create(
                from_=os.environ.get("TWILIO_WHATSAPP_NUMBER"),
                to=requester,
                body=msg
            )
            return

        recipients = set()
        if AGENT_NUMBER:
            recipients.add(AGENT_NUMBER)
        for num in DOCTOR_NUMBERS:
            recipients.add(f"whatsapp:{num}")
        for recipient in recipients:
            try:
                twilio_client.messages.create(
                    from_=os.environ.get("TWILIO_WHATSAPP_NUMBER"),
                    to=recipient,
                    body=msg
                )
            except Exception as e:
                print(f"❌ Queue send error {recipient}: {e}")
    except Exception as e:
        print(f"❌ Queue error: {e}")


def send_week_queue(requester):
    """Envoie les RDV de la semaine complète."""
    try:
        today = date.today()
        end_of_week = today + timedelta(days=7)
        appointments = supabase.table("appointments")\
            .select("*")\
            .eq("status", "confirmed")\
            .gte("date", str(today))\
            .lte("date", str(end_of_week))\
            .order("date")\
            .order("time")\
            .execute()\
            .data

        if not appointments:
            msg = "📋 Aucun RDV cette semaine ✅"
        else:
            msg  = f"━━━━━━━━━━━━━━━━━━━━━━\n"
            msg += f"📋 *RDV DE LA SEMAINE — {INSTITUTION_NAME}*\n"
            msg += f"📅 *{today.strftime('%d/%m')} → {end_of_week.strftime('%d/%m/%Y')}*\n"
            msg += f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            current_date = None
            for apt in appointments:
                if apt["date"] != current_date:
                    current_date = apt["date"]
                    msg += f"📅 *{current_date}*\n"
                emoji = "🔴" if apt["triage_level"] == "RED" else "🟡"
                pid = apt["session_hash"][:4].upper()
                short = apt["symptoms"].split("\n")[0][:50] if apt["symptoms"] else "Non précisé"
                msg += f"  {emoji} {apt['time']} — #{pid} — _{short}_\n"
            msg += f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
            msg += f"*Total semaine: {len(appointments)} RDV*"

        twilio_client.messages.create(
            from_=os.environ.get("TWILIO_WHATSAPP_NUMBER"),
            to=requester,
            body=msg
        )
    except Exception as e:
        print(f"❌ Week queue error: {e}")

def send_next_24h_queue(requester):
    """Patients dans les 24 prochaines heures."""
    try:
        tomorrow = str(date.today() + timedelta(days=1))
        appointments = supabase.table("appointments")\
            .select("*")\
            .in_("date", [str(date.today()), tomorrow])\
            .eq("status", "confirmed")\
            .order("date")\
            .order("time")\
            .execute()\
            .data

        if not appointments:
            msg = "📋 Aucun patient dans les 24h ✅"
        else:
            msg  = f"━━━━━━━━━━━━━━━━━━━━━━\n"
            msg += f"📋 *PROCHAINS — 24H*\n"
            msg += f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            current_date = None
            for apt in appointments:
                if apt["date"] != current_date:
                    current_date = apt["date"]
                    label = "Aujourd'hui" if current_date == str(date.today()) else "Demain"
                    msg += f"📅 *{label}*\n"
                emoji  = "🔴" if apt["triage_level"] == "RED" else "🟡"
                pid    = apt["session_hash"][:4].upper()
                status = "✅" if apt["status"] == "treated" else "⏳"
                short  = apt["symptoms"].split("\n")[0][:50] if apt["symptoms"] else "—"
                msg += f"  {emoji} *{apt['time']}* {status} #{pid}\n"
                msg += f"  _{short}_\n\n"
            msg += f"━━━━━━━━━━━━━━━━━━━━━━\n"
            msg += f"*Total: {len(appointments)} patients*"

        twilio_client.messages.create(
            from_=os.environ.get("TWILIO_WHATSAPP_NUMBER"),
            to=requester,
            body=msg
        )
    except Exception as e:
        print(f"❌ Next 24h error: {e}")


def send_appointment_reminders():
    """Rappel 30 min avant chaque RDV — agent + médecin."""
    try:
        now = datetime.utcnow()
        future = now + timedelta(minutes=30)
        future_time = f"{future.hour}h{future.minute:02d}"
        future_time_alt = f"{future.hour}h{future.minute if future.minute else '00'}"
        appointments = supabase.table("appointments")\
            .select("*")\
            .eq("date", str(date.today()))\
            .eq("status", "confirmed")\
            .execute()\
            .data
        for apt in appointments:
            apt_time = apt["time"].strip()
            if apt_time in [future_time, future_time_alt]:
                pid = apt["session_hash"][:4].upper()
                symptoms_short = apt["symptoms"].split("\n")[0][:60] if apt["symptoms"] else "Non précisé"
                reminder_msg = (
                    f"⏰ *RAPPEL — {apt['time']}*\n"
                    f"👤 #{pid} — _{symptoms_short}_\n\n"
                    f"📞 Préparez l'appel\n"
                    f"✅ *TRAITÉ {pid}* après"
                )
                recipients = set()
                if AGENT_NUMBER:
                    recipients.add(AGENT_NUMBER)
                for num in DOCTOR_NUMBERS:
                    recipients.add(f"whatsapp:{num}")
                for recipient in recipients:
                    try:
                        twilio_client.messages.create(
                            from_=os.environ.get("TWILIO_WHATSAPP_NUMBER"),
                            to=recipient,
                            body=reminder_msg
                        )
                        print(f"⏰ Rappel envoyé à {recipient} pour {apt['time']}")
                    except Exception as e:
                        print(f"❌ Rappel error {recipient}: {e}")
    except Exception as e:
        print(f"❌ Reminder error: {e}")

def send_evening_reminders():
    """Rappel J-1 soir à 20h — RDV du lendemain."""
    try:
        tomorrow = str(date.today() + timedelta(days=1))
        appointments = supabase.table("appointments")\
            .select("*")\
            .eq("date", tomorrow)\
            .eq("status", "confirmed")\
            .order("time")\
            .execute()\
            .data
        if not appointments:
            return
        msg  = f"━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"📅 *RDV DE DEMAIN — {INSTITUTION_NAME}*\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for apt in appointments:
            pid = apt["session_hash"][:4].upper()
            short = apt["symptoms"].split("\n")[0][:55] if apt["symptoms"] else "Non précisé"
            msg += f"🟡 *{apt['time']}* — #{pid}\n"
            msg += f"   _{short}_\n\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"*Total demain: {len(appointments)} RDV*"
        recipients = set()
        if AGENT_NUMBER:
            recipients.add(AGENT_NUMBER)
        for num in DOCTOR_NUMBERS:
            recipients.add(f"whatsapp:{num}")
        for recipient in recipients:
            try:
                twilio_client.messages.create(
                    from_=os.environ.get("TWILIO_WHATSAPP_NUMBER"),
                    to=recipient,
                    body=msg
                )
                print(f"🌙 Rappel J-1 envoyé à {recipient}")
            except Exception as e:
                print(f"❌ Evening reminder error: {e}")
    except Exception as e:
        print(f"❌ Evening reminder error: {e}")

def is_booking_trigger(message):
    triggers = [
        "rendez-vous", "rdv", "rendez vous", "consulter",
        "consultation", "prendre rdv", "reserver",
        "réserver", "je veux un rdv", "prendre rendez"
    ]
    return message.lower().strip() in triggers

def is_booking_slot_selection(sender, message):
    """Sélection d'un créneau dans la liste."""
    if not message.strip().isdigit(): return False
    if sender not in conversations or not conversations[sender]: return False
    last = conversations[sender][-1]["content"].lower()
    return "choisissez un créneau" in last

def is_doctor_dispo(message):
    """Médecin envoie ses disponibilités."""
    return message.upper().startswith("DISPO")

def is_doctor_treated(message):
    """Médecin marque un patient comme traité."""
    return message.upper().startswith("TRAITÉ") or message.upper().startswith("TRAITE")

def is_doctor_cancel(message):
    return message.upper().startswith("ANNULER")

def parse_doctor_cancel(message):
    """ANNULER 10h → supprime le slot."""
    times = re.findall(r'\d+h\d*', message.lower())
    if not times:
        return "Format: ANNULER 10h"
    for t in times:
        supabase.table("slots")\
            .delete()\
            .eq("time", t)\
            .eq("is_booked", False)\
            .eq("date", str(date.today()))\
            .execute()
    return f"✅ Créneau(x) {', '.join(times)} annulé(s)"

def is_agent_request(message):
    """Détecte si le patient veut parler à un agent."""
    return message.strip().upper() in ["AGENT", "PARLER", "HUMAIN", "AIDE"]

def get_symptoms_summary(sender):
    """Génère un résumé lisible des symptômes via GPT."""
    try:
        msgs = [
            m for m in conversations.get(sender, [])
            if m.get("role") in ["user", "assistant"]
        ]
        if not msgs:
            return "Non précisé"
        summary = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Tu es un assistant médical. "
                        "Résume en 3-4 lignes maximum la situation du patient "
                        "basé sur cette conversation. "
                        "Format: Profil / Symptôme principal / Durée / Signes associés. "
                        "Sois concis et factuel."
                    )
                },
                {
                    "role": "user",
                    "content": str(msgs[-10:])
                }
            ],
            max_tokens=150
        ).choices[0].message.content
        return summary
    except Exception as e:
        print(f"❌ Summary error: {e}")
        raw = [
            m["content"] for m in conversations.get(sender, [])
            if m.get("role") == "user" and len(m["content"]) > 2
        ]
        return " | ".join(raw[-4:]) if raw else "Non précisé"

def is_location_request(sender, message):
    """Détecte si l'utilisateur cherche pharmacie ou hôpital."""
    if message.strip() not in ["1", "2", "3"]:
        return False
    if sender not in conversations or not conversations[sender]:
        return False
    last = conversations[sender][-1]["content"].lower()
    return "pharmacie proche" in last or "besoin d'aide pour trouver" in last

def get_location_message(choice):
    if choice == "1":
        return (
            "🏥 *Trouver une pharmacie proche:*\n\n"
            "👉 https://maps.google.com/?q=pharmacie\n\n"
            "Ou ouvrez Google Maps et tapez:\n"
            "• 'pharmacie' (Afrique)\n"
            "• 'pharmacy' (Canada/France)\n\n"
            "📞 Pharmacie 24h:\n"
            "• Canada: tapez 'pharmacie 24h'\n"
            "• Sénégal: tapez 'pharmacie de garde Dakar'"
        )
    elif choice == "2":
        return (
            "🏨 *Trouver un hôpital proche:*\n\n"
            "👉 https://maps.google.com/?q=hopital\n\n"
            "Ou ouvrez Google Maps et tapez:\n"
            "• 'hôpital' ou 'urgences'\n\n"
            "📞 Numéros d'urgence:\n"
            "• Canada:   911\n"
            "• France:   15 (SAMU)\n"
            "• Sénégal:  15\n"
            "• Djibouti: 15"
        )
    return None

def analyze_image(media_url):
    try:
        print("🖼️ Analyse image...")
        auth = (os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
        res = requests.get(media_url, auth=auth, timeout=30)
        if res.status_code != 200:
            print(f"❌ Échec téléchargement image: {res.status_code}")
            return None
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(res.content)
            tmp_path = tmp.name
        upload = cloudinary.uploader.upload(tmp_path, folder="wazihealth/images")
        public_url = upload["secure_url"]
        print(f"☁️ Image uploadée: {public_url}")
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Tu es un infirmier expérimenté en Afrique de l'Ouest. "
                            "Analyse cette photo médicale:\n"
                            "1. Décris ce que tu vois (couleur, taille, forme, localisation)\n"
                            "2. Note les détails importants: rougeur, gonflement, pus, sécheresse, taches\n"
                            "3. Donne 1-2 hypothèses probables à confirmer\n"
                            "4. Indique si la photo est claire ou difficile à analyser\n"
                            "Maximum 4 phrases. "
                            "Termine toujours par: 'À confirmer avec des questions.'"
                        )
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": public_url}
                    }
                ]
            }],
            max_tokens=250
        )
        description = response.choices[0].message.content
        print(f"🖼️ Hypothèse: {description}")
        return description
    except Exception as e:
        print(f"❌ Image error: {type(e).__name__}: {e}")
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
            result = openai_client.audio.transcriptions.create(
                model="whisper-1", file=f, language="fr"
            )
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
        tts = openai_client.audio.speech.create(
            model="tts-1", voice="nova", input=summary, speed=0.9
        )
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(tts.content)
            tmp_path = tmp.name
        upload = cloudinary.uploader.upload(
            tmp_path, resource_type="video", folder="wazihealth/audio"
        )
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
    incoming_text = normalize_response(request)
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
                print(f"🖼️ Ajouté au contexte: {description}")
            else:
                r = MessagingResponse()
                r.message("📸 Photo reçue mais je n'arrive pas à l'analyser.\nDécrivez ce que vous voyez en texte svp.")
                return str(r)

        else:
            r = MessagingResponse()
            r.message("Envoyez un texte, une photo 📸 ou un message vocal 🎤")
            return str(r)

    # ── Message vide OU salutation ───────────────────────────
    GREETINGS = ["bonjour", "bonsoir", "salut", "hello", "hi", "allo", "allô", "salam"]
    if not incoming_text or incoming_text.lower().strip() in GREETINGS:
        # ── Médecin ─────────────────────────────────────────
        if is_doctor(sender):
            doctor_menu = (
                f"👨‍⚕️ *Bonjour Docteur — {INSTITUTION_NAME}*\n"
                f"_Votre assistant de gestion des consultations._\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📅 *CRÉNEAUX*\n"
                f"• *DISPO* demain 9h à 12h\n"
                f"• *DISPO* semaine 9h 14h\n"
                f"• *ANNULER* 10h\n\n"
                f"📋 *AGENDA*\n"
                f"• *FILE* → aujourd'hui\n"
                f"• *PROCHAIN* → 24 prochaines heures\n"
                f"• *AGENDA* → 7 prochains jours\n\n"
                f"✅ *APRÈS APPEL*\n"
                f"• *TRAITÉ* XXXX\n\n"
                f"🧑 *MODE TEST*\n"
                f"• *PATIENT* → triage patient\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )
            r = MessagingResponse()
            r.message(doctor_menu)
            return str(r)

        # ── Agent ────────────────────────────────────────────
        sender_clean = sender.replace("whatsapp:", "")
        if AGENT_NUMBER and sender_clean in AGENT_NUMBER:
            agent_menu = (
                f"👤 *Bonjour — Agent {INSTITUTION_NAME}*\n"
                f"_Supervision des consultations et urgences._\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 *AGENDA*\n"
                f"• *FILE* → aujourd'hui\n"
                f"• *PROCHAIN* → 24 prochaines heures\n"
                f"• *AGENDA* → 7 prochains jours\n\n"
                f"✅ *APRÈS APPEL*\n"
                f"• *TRAITÉ* XXXX\n\n"
                f"🔔 *AUTO*\n"
                f"• 🆕 Nouveau RDV + résumé\n"
                f"• 🚨 Urgences immédiates\n"
                f"• 📋 File à 8h\n"
                f"• ⏰ Rappel 30 min avant RDV\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )
            r = MessagingResponse()
            r.message(agent_menu)
            return str(r)

        # ── Patient ──────────────────────────────────────────
        profile_question = (
            f"👋 *{INSTITUTION_NAME}* 🏥\n\n"
            f"Je peux vous aider à:\n"
            f"• Comprendre vos symptômes\n"
            f"• Conseils avant le médecin\n"
            f"• Prendre un rendez-vous\n\n"
            f"Qui consulte aujourd'hui?\n\n"
            f"1️⃣ Adulte (18-60 ans)\n"
            f"2️⃣ Enfant (2-17 ans)\n"
            f"3️⃣ Autre profil"
        )
        if sender not in conversations:
            conversations[sender] = []
        conversations[sender].append({
            "role": "assistant",
            "content": profile_question
        })

        sent = send_template(sender, TEMPLATE_PROFIL_SID)
        if not sent:
            r = MessagingResponse()
            r.message(profile_question)
            send_welcome_audio(sender)
            return str(r)
        send_welcome_audio(sender)
        return ("", 204)

    print(f"📩 {hash_sender(sender)}: {incoming_text}")
    log_to_db(sender, "user", incoming_text)

    # ── Reset session ───────────────────────────────────────
    if incoming_text.lower() in ["reset", "recommencer", "nouvelle consultation"]:
        conversations.pop(sender, None)

        if is_doctor(sender):
            doctor_menu = (
                f"👨‍⚕️ *Session réinitialisée — {INSTITUTION_NAME}*\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📅 *CRÉNEAUX*\n"
                f"• *DISPO* demain 9h à 12h\n"
                f"• *DISPO* semaine 9h 14h\n"
                f"• *ANNULER* 10h\n\n"
                f"📋 *AGENDA*\n"
                f"• *FILE* → aujourd'hui\n"
                f"• *PROCHAIN* → 24 prochaines heures\n"
                f"• *AGENDA* → 7 prochains jours\n\n"
                f"✅ *APRÈS APPEL*\n"
                f"• *TRAITÉ* XXXX\n\n"
                f"🧑 *MODE TEST*\n"
                f"• *PATIENT* → triage patient\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )
            r = MessagingResponse()
            r.message(doctor_menu)
            return str(r)

        sender_clean = sender.replace("whatsapp:", "")
        if AGENT_NUMBER and sender_clean in AGENT_NUMBER:
            agent_menu = (
                f"👤 *Session réinitialisée — Agent {INSTITUTION_NAME}*\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 *AGENDA*\n"
                f"• *FILE* → aujourd'hui\n"
                f"• *PROCHAIN* → 24 prochaines heures\n"
                f"• *AGENDA* → 7 prochains jours\n\n"
                f"✅ *APRÈS APPEL*\n"
                f"• *TRAITÉ* XXXX\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )
            r = MessagingResponse()
            r.message(agent_menu)
            return str(r)

        profile_question = (
            f"👋 *{INSTITUTION_NAME}* 🏥\n\n"
            f"• 🤒 Comprendre vos symptômes\n"
            f"• 💊 Conseils avant le médecin\n"
            f"• 📅 Prendre un rendez-vous\n"
            f"• 🏥 Trouver une pharmacie\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"*Qui consulte?*\n\n"
            f"1️⃣ Adulte (18-60 ans)\n"
            f"2️⃣ Enfant (2-17 ans)\n"
            f"3️⃣ Personne âgée (60+)\n"
            f"4️⃣ Autre profil"
        )
        conversations[sender] = [{"role": "assistant", "content": profile_question}]
        r = MessagingResponse()
        r.message(profile_question)
        send_welcome_audio(sender)
        return str(r)

    # ── Détection rôle ──────────────────────────────────────
    is_doc = is_doctor(sender)
    sender_clean = sender.replace("whatsapp:", "")
    is_agt = AGENT_NUMBER and sender_clean in AGENT_NUMBER

    in_patient_mode = any(
        m.get("content") == "PATIENT_MODE:true"
        for m in conversations.get(sender, [])
    )
    if in_patient_mode:
        is_doc = False
        is_agt = False

    # ── Commandes médecin/agent ─────────────────────────────
    if is_doc or is_agt:
        if incoming_text.upper().strip() == "PATIENT":
            conversations[sender] = []
            profile_question = (
                f"👋 Mode patient activé — {INSTITUTION_NAME} 🏥\n\n"
                "Qui consulte aujourd'hui?\n\n"
                "1️⃣ Adulte (18-60 ans)\n"
                "2️⃣ Enfant (2-17 ans)\n"
                "3️⃣ Personne âgée (60 ans et plus)\n"
                "4️⃣ Autre profil (enceinte, maladie chronique...)"
            )
            conversations[sender] = [
                {"role": "assistant", "content": profile_question},
                {"role": "system",    "content": "PATIENT_MODE:true"}
            ]
            r = MessagingResponse()
            r.message(profile_question)
            return str(r)

        intent = detect_intent(sender, incoming_text)
        action = intent.get("intent")
        params = intent.get("params", {})

        if action == "dispo" and is_doc:
            raw = params.get("raw", incoming_text)
            result = parse_doctor_availability(raw)
            r = MessagingResponse()
            r.message(result or "❌ Format non reconnu.\nEx: DISPO demain 9h 10h 14h")
            return str(r)

        if action == "annuler" and is_doc:
            raw = params.get("raw", incoming_text)
            result = parse_doctor_cancel(raw)
            r = MessagingResponse()
            r.message(result)
            return str(r)

        if action == "file":
            low = incoming_text.lower()
            if any(w in low for w in ["agenda", "semaine", "7 jour"]):
                send_week_queue(requester=sender)
                r = MessagingResponse()
                r.message("📋 Agenda de la semaine envoyé!")
            elif any(w in low for w in ["prochain", "24h", "demain"]):
                send_next_24h_queue(requester=sender)
                r = MessagingResponse()
                r.message("📋 Prochains patients (24h) envoyés!")
            else:
                send_queue_to_doctor(requester=sender)
                r = MessagingResponse()
                r.message("📋 File d'aujourd'hui envoyée!")
            return str(r)

        if action == "traite":
            pid = params.get("patient_id", "")
            if pid:
                try:
                    supabase.table("appointments")\
                        .update({"status": "treated"})\
                        .ilike("session_hash", f"{pid.lower()}%")\
                        .execute()
                    msg = f"✅ Patient #{pid.upper()} marqué comme traité."
                except Exception:
                    msg = "❌ Erreur mise à jour."
            else:
                msg = "Quel patient? Ex: TRAITÉ A3F2"
            r = MessagingResponse()
            r.message(msg)
            return str(r)

        if action == "aide":
            guide = (
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📖 *GUIDE {INSTITUTION_NAME}*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📅 *CRÉNEAUX*\n"
                f"• *DISPO* demain 9h à 12h\n"
                f"• *DISPO* semaine 9h 14h\n"
                f"• *DISPO* lundi 10h 11h\n"
                f"• *ANNULER* 10h\n\n"
                f"📋 *AGENDA*\n"
                f"• *FILE* → aujourd'hui\n"
                f"• *PROCHAIN* → 24 prochaines heures\n"
                f"• *AGENDA* → 7 prochains jours\n\n"
                f"✅ *APRÈS APPEL*\n"
                f"• *TRAITÉ* XXXX\n\n"
                f"🧑 *MODE TEST*\n"
                f"• *PATIENT* → triage patient\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )
            r = MessagingResponse()
            r.message(guide)
            return str(r)

        if action != "patient":
            r = MessagingResponse()
            r.message("❓ Commande non reconnue.\nEnvoyez *AIDE* pour voir les commandes disponibles.")
            return str(r)

    # ── Layer 2f: Profil → template symptômes ──────────────
    if is_profile_response(sender, incoming_text):
        conversations[sender].append({
            "role": "user",
            "content": incoming_text
        })
        sent = send_template(sender, TEMPLATE_SYMPTOMES_SID)
        symptom_msg = (
            "Qu'est-ce qui ne va pas et depuis quand?\n\n"
            "1️⃣ Fièvre — depuis aujourd'hui\n"
            "2️⃣ Fièvre — depuis 2 jours ou plus\n"
            "3️⃣ Douleur (tête, ventre, dos)\n"
            "4️⃣ Toux / difficultés à respirer\n"
            "5️⃣ Problèmes digestifs\n"
            "6️⃣ Problème de peau\n"
            "7️⃣ Fatigue / faiblesse\n"
            "8️⃣ Autre"
        )
        conversations[sender].append({
            "role": "assistant",
            "content": symptom_msg
        })
        if not sent:
            r = MessagingResponse()
            r.message(symptom_msg)
            return str(r)
        return ("", 204)

    # ── Layer 1: Urgence critique ───────────────────────────
    if is_critical(incoming_text):
        print("🚨 CRITIQUE")
        log_to_db(sender, "assistant", EMERGENCY_RESPONSE, triage_level="RED", is_emergency=True)
        conversations.pop(sender, None)
        notify_agent(sender, f"Mots-clés urgence détectés: '{incoming_text[:100]}'", triage_level="RED")
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

    # ── Layer 2b: Demande agent direct ─────────────────────
    if is_agent_request(incoming_text):
        print("👤 Agent request direct")
        agent_msg = (
            "👤 *Mise en contact avec un agent*\n\n"
            "Un agent WaziHealth vous contactera bientôt\n"
            "sur ce numéro WhatsApp.\n\n"
            "📞 Ou appelez directement:\n"
            f"*{os.environ.get('AGENT_PHONE', '+221 XX XXX XX XX')}*\n\n"
            "Merci de votre confiance 🙏"
        )
        log_to_db(sender, "system", "AGENT_REQUESTED_DIRECT", triage_level="HANDOFF")
        notify_agent(sender, f"Patient demande agent après diagnostic\nDernier message: {incoming_text}")
        conversations.pop(sender, None)
        r = MessagingResponse()
        r.message(agent_msg)
        return str(r)

    # ── Layer 2c: Booking trigger ──────────────────────────
    if is_booking_trigger(incoming_text):
        send_booking_list(sender)
        log_to_db(sender, "assistant", "booking_menu", triage_level="BOOKING")
        return ("", 204)

    # ── Layer 2d-1: Booking — sélection via List Message ─────
    list_id = request.form.get("ListId", "").strip()
    if list_id.startswith("slot_"):
        try:
            idx = int(list_id.replace("slot_", "")) - 1
            slots_data = next(
                (m["content"].replace("AVAILABLE_SLOTS:", "")
                 for m in reversed(conversations.get(sender, []))
                 if m.get("content", "").startswith("AVAILABLE_SLOTS:")),
                None
            )
            if not slots_data:
                raise ValueError("Slots expirés")
            slots = json.loads(slots_data)
            if idx < 0 or idx >= len(slots):
                raise ValueError("Index invalide")
            slot     = slots[idx]
            symptoms = get_symptoms_summary(sender)
            success  = book_slot(sender, slot, symptoms)
            confirmation = (
                f"✅ *RDV confirmé!*\n\n"
                f"📅 {slot['date']} à {slot['time']}\n"
                f"📞 Le médecin vous appellera sur WhatsApp\n\n"
                f"Prenez soin de vous 💚"
            ) if success else (
                "Ce créneau n'est plus disponible.\n"
                "Répondez RENDEZ-VOUS pour voir les autres."
            )
        except Exception as e:
            print(f"❌ Slot selection error: {e}")
            confirmation = (
                "Session expirée.\n"
                "Répondez RENDEZ-VOUS pour recommencer."
            )
        r = MessagingResponse()
        r.message(confirmation)
        conversations.pop(sender, None)
        return str(r)

    # ── Layer 2d-2: Booking — sélection via numéro texte ───
    if is_booking_slot_selection(sender, incoming_text):
        choice = incoming_text.strip()
        stored_slots = None
        for msg in reversed(conversations.get(sender, [])):
            if msg.get("content", "").startswith("BOOKING_SLOTS:"):
                stored_slots = msg["content"].replace("BOOKING_SLOTS:", "").split(",")
                break
        if stored_slots:
            idx = int(choice) - 1
            if 0 <= idx < len(stored_slots):
                slot_id = stored_slots[idx]
                try:
                    slot_data = supabase.table("slots").select("*").eq("id", slot_id).single().execute()
                    s = slot_data.data
                    symptoms = get_symptoms_summary(sender)
                    if book_slot(sender, s, symptoms):
                        confirmation = (
                            f"✅ *RDV confirmé!*\n\n"
                            f"📅 {s['date']} à {s['time']}\n"
                            f"📞 Le médecin vous appellera sur ce numéro WhatsApp\n\n"
                            f"Préparez:\n"
                            f"• Ce résumé de symptômes\n"
                            f"• Vos médicaments actuels\n\n"
                            f"Prenez soin de vous 💚"
                        )
                        r = MessagingResponse()
                        r.message(confirmation)
                        conversations[sender].append({"role": "assistant", "content": confirmation})
                        conversations.pop(sender, None)
                        return str(r)
                except Exception as e:
                    print(f"❌ Booking DB error: {e}")
        r = MessagingResponse()
        r.message("Ce créneau n'est plus disponible. Répondez *RENDEZ-VOUS* pour voir les créneaux actuels.")
        return str(r)

    # ── Layer 3: Location ───────────────────────────────────
    if is_location_request(sender, incoming_text):
        choice = incoming_text.strip()
        location_msg = get_location_message(choice)

        feedback_question = (
            "🙏 Cette réponse vous a-t-elle aidé?\n"
            "1️⃣ Oui\n"
            "2️⃣ Partiellement\n"
            "3️⃣ Non — je veux parler à quelqu'un"
        )
        r = MessagingResponse()
        if location_msg:
            r.message(location_msg)
            log_to_db(sender, "assistant", location_msg, triage_level="LOCATION")
        r.message(feedback_question)
        conversations[sender].append({
            "role": "assistant",
            "content": feedback_question
        })
        return str(r)

    # ── Layer 3b: Feedback ──────────────────────────────────
    if is_feedback(sender, incoming_text):
        feedback_value = FEEDBACK_CHOICES.get(incoming_text.strip(), "inconnu")
        print(f"📊 Feedback: {feedback_value}")
        log_to_db(sender, "feedback", feedback_value, triage_level="FEEDBACK")
        r = MessagingResponse()
        if incoming_text.strip() == "3":
            agent_question = (
                "Désolé que la réponse n'ait pas été utile.\n\n"
                "Voulez-vous être mis en contact avec un agent?\n"
                "Répondez OUI ou NON"
            )
            r.message(agent_question)
            conversations[sender].append({
                "role": "assistant",
                "content": agent_question
            })
        else:
            sent = send_template(sender, TEMPLATE_FEEDBACK_SID)
            if not sent:
                r.message("Merci! 🙏 Prenez soin de vous 💚")
            conversations.pop(sender, None)
            if sent:
                return ("", 204)
        return str(r)

    # ── Layer 4: Triage AI ──────────────────────────────────
    ai_response  = get_ai_response(sender, incoming_text)
    triage_level = extract_triage_level(ai_response)
    condition    = detect_condition(ai_response)

    log_to_db(sender, "assistant", ai_response, triage_level=triage_level)
    print(f"🤖 Triage: {triage_level} | Condition: {condition}")

    r = MessagingResponse()
    r.message(ai_response)

    if triage_level == "RED":
        summary = f"Condition: {condition or 'inconnue'} | Message: {incoming_text[:100]}"
        threading.Thread(
            target=notify_agent,
            args=(sender, summary),
            daemon=True
        ).start()

    if is_audio:
        t = threading.Thread(target=send_audio_async, args=(sender, ai_response))
        t.daemon = True
        t.start()
        print("🔄 Thread audio démarré")

    return str(r)

def run_schedule():
    schedule.every().day.at("12:00").do(send_queue_to_doctor)
    schedule.every().day.at("00:00").do(send_evening_reminders)
    schedule.every(1).minutes.do(send_appointment_reminders)
    while True:
        schedule.run_pending()
        time_module.sleep(60)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=get_or_create_welcome_audio, daemon=True).start()
    threading.Thread(target=run_schedule, daemon=True).start()
    app.run(host="0.0.0.0", port=port)

