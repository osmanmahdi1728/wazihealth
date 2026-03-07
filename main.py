import os, hashlib, tempfile, threading, requests, re
import schedule, time as time_module
from datetime import date, timedelta
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

# ── Booking config ─────────────────────────────────────────
BOOKING_TIMES = {"1": "9h00", "2": "10h00", "3": "11h00"}

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

def notify_agent(sender, summary, triage_level="RED"):
    """Notifie l'agent/médecin avec le bon niveau."""
    if not AGENT_NUMBER:
        print("⚠️ AGENT_NUMBER non configuré")
        return
    try:
        if triage_level == "RED":
            emoji = "🔴"
            urgency = "PATIENT URGENT"
        elif triage_level == "YELLOW":
            emoji = "🟡"
            urgency = "NOUVEAU RDV"
        else:
            emoji = "🟢"
            urgency = "INFO PATIENT"
        twilio_client.messages.create(
            from_=os.environ.get("TWILIO_WHATSAPP_NUMBER"),
            to=AGENT_NUMBER,
            body=(
                f"{emoji} *{urgency} — {INSTITUTION_NAME}*\n\n"
                f"{summary}\n\n"
                f"Répondez à ce message pour contacter le patient."
            )
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
        notify_agent(
            sender,
            f"📅 Nouveau RDV: {slot['date']} à {slot['time']}\n"
            f"Symptômes: {symptoms[:100]}",
            triage_level="YELLOW"
        )
        return True
    except Exception as e:
        print(f"❌ Booking error: {e}")
        return False

def parse_doctor_availability(message):
    """Parse 'DISPO demain 9h 10h 14h' → crée les slots."""
    msg = message.lower()
    if "demain" in msg:
        target_date = date.today() + timedelta(days=1)
    elif "aujourd'hui" in msg or "auj" in msg:
        target_date = date.today()
    elif "dans 2" in msg:
        target_date = date.today() + timedelta(days=2)
    else:
        return None
    times = re.findall(r'\d+h\d*', msg)
    if not times:
        return None
    for t in times:
        supabase.table("slots").insert({
            "doctor_id": "default",
            "date":      str(target_date),
            "time":      t,
            "is_booked": False
        }).execute()
    return f"✅ {len(times)} créneau(x) ajouté(s) pour le {target_date}"

def send_queue_to_doctor():
    """Envoie la file d'attente du jour au médecin à 8h."""
    try:
        appointments = supabase.table("appointments")\
            .select("*")\
            .eq("date", str(date.today()))\
            .eq("status", "confirmed")\
            .order("time")\
            .execute()\
            .data
        if not appointments:
            print("📋 Aucun RDV aujourd'hui")
            return
        msg = f"📋 *FILE D'ATTENTE — {INSTITUTION_NAME}*\n"
        msg += f"{'━'*25}\n\n"
        msg += f"📅 {date.today().strftime('%d/%m/%Y')}\n\n"
        for apt in appointments:
            emoji = "🔴" if apt["triage_level"] == "RED" else "🟡"
            patient_id = apt["session_hash"][:4].upper()
            msg += (
                f"{emoji} {apt['time']} — Patient #{patient_id}\n"
                f"   {apt['symptoms'][:80]}\n\n"
            )
        msg += "━"*25
        if AGENT_NUMBER:
            twilio_client.messages.create(
                from_=os.environ.get("TWILIO_WHATSAPP_NUMBER"),
                to=AGENT_NUMBER,
                body=msg
            )
            print(f"✅ File d'attente envoyée: {len(appointments)} RDV")
    except Exception as e:
        print(f"❌ Queue error: {e}")

def send_appointment_reminders():
    """Envoie un rappel WhatsApp 30 min avant chaque RDV."""
    try:
        from datetime import datetime
        now = datetime.now()
        reminder_time = (now + timedelta(minutes=30)).strftime("%H:%M")
        appointments = supabase.table("appointments")\
            .select("*")\
            .eq("date", str(date.today()))\
            .eq("status", "confirmed")\
            .execute()\
            .data
        for apt in appointments:
            apt_time = apt["time"].replace("h", ":").zfill(5)
            if apt_time == reminder_time:
                print(f"⏰ Rappel dû pour {apt['session_hash'][:4]} à {apt['time']}")
                notify_agent(
                    "system",
                    f"⏰ Rappel: RDV dans 30 min\n"
                    f"Patient #{apt['session_hash'][:4].upper()}\n"
                    f"Symptômes: {apt['symptoms'][:80]}",
                    triage_level="YELLOW"
                )
    except Exception as e:
        print(f"❌ Reminder error: {e}")

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

def is_agent_request(message):
    """Détecte si le patient veut parler à un agent."""
    return message.strip().upper() in ["AGENT", "PARLER", "HUMAIN", "AIDE"]

def get_symptoms_summary(sender):
    """Extrait le résumé des symptômes depuis la mémoire."""
    symptom_msgs = [
        m["content"] for m in conversations.get(sender, [])
        if m.get("role") == "user" and len(m["content"]) > 3
    ]
    return " | ".join(symptom_msgs[-3:]) if symptom_msgs else "Non précisé"

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
        profile_question = (
            f"👋 Bonjour! Je suis l'assistant de {INSTITUTION_NAME} 🏥\n\n"
            "Je peux vous aider à:\n"
            "• 🤒 Comprendre vos symptômes\n"
            "• 💊 Savoir quoi faire en attendant le médecin\n"
            "• 🏥 Trouver une pharmacie ou un hôpital proche\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Qui consulte aujourd'hui?\n\n"
            "1️⃣ Adulte (18-60 ans) — bonne santé générale\n"
            "2️⃣ Enfant (2-17 ans)\n"
            "3️⃣ Personne âgée (60 ans et plus)\n"
            "4️⃣ Autre profil (enceinte, maladie chronique...)"
        )
        if sender not in conversations:
            conversations[sender] = []
        conversations[sender].append({
            "role": "assistant",
            "content": profile_question
        })
        r = MessagingResponse()
        r.message(profile_question)
        send_welcome_audio(sender)
        return str(r)

    print(f"📩 {hash_sender(sender)}: {incoming_text}")
    log_to_db(sender, "user", incoming_text)

    # ── Reset session ───────────────────────────────────────
    if incoming_text.lower() in ["reset", "recommencer", "nouvelle consultation"]:
        conversations.pop(sender, None)
        profile_question = (
            f"👋 Bonjour! Je suis l'assistant de {INSTITUTION_NAME} 🏥\n\n"
            "Je peux vous aider à:\n"
            "• 🤒 Comprendre vos symptômes\n"
            "• 💊 Savoir quoi faire en attendant le médecin\n"
            "• 🏥 Trouver une pharmacie ou un hôpital proche\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Qui consulte aujourd'hui?\n\n"
            "1️⃣ Adulte (18-60 ans) — bonne santé générale\n"
            "2️⃣ Enfant (2-17 ans)\n"
            "3️⃣ Personne âgée (60 ans et plus)\n"
            "4️⃣ Autre profil (enceinte, maladie chronique...)"
        )
        conversations[sender] = [{
            "role": "assistant",
            "content": profile_question
        }]
        r = MessagingResponse()
        r.message(profile_question)
        send_welcome_audio(sender)
        return str(r)

    # ── Doctor DISPO management ─────────────────────────────
    if is_doctor_dispo(incoming_text):
        result = parse_doctor_availability(incoming_text)
        r = MessagingResponse()
        if result:
            r.message(result)
        else:
            r.message("Format: DISPO demain 9h 10h 14h 15h")
        return str(r)

    if incoming_text.upper().strip() in ["FILE", "QUEUE", "PATIENTS"]:
        send_queue_to_doctor()
        r = MessagingResponse()
        r.message("📋 File d'attente envoyée!")
        return str(r)

    if is_doctor_treated(incoming_text):
        r = MessagingResponse()
        r.message("✅ Patient marqué comme traité.")
        return str(r)

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
        r = MessagingResponse()
        slot_msg, slots = get_booking_start_message()
        if slots:
            conversations[sender].append({"role": "system", "content": "BOOKING_SLOTS:" + ",".join(s["id"] for s in slots)})
        conversations[sender].append({"role": "assistant", "content": slot_msg})
        r.message(slot_msg)
        log_to_db(sender, "assistant", "booking_menu", triage_level="BOOKING")
        return str(r)

    # ── Layer 2d: Booking — sélection du créneau ────────────
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
            r.message("Merci! 🙏 Prenez soin de vous 💚")
            conversations.pop(sender, None)
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
    schedule.every().day.at("08:00").do(send_queue_to_doctor)
    schedule.every(1).minutes.do(send_appointment_reminders)
    while True:
        schedule.run_pending()
        time_module.sleep(60)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=get_or_create_welcome_audio, daemon=True).start()
    threading.Thread(target=run_schedule, daemon=True).start()
    app.run(host="0.0.0.0", port=port)

