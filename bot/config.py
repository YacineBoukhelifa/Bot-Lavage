"""
Configuration du bot — modifiable sans toucher au code métier.
Les secrets (token, secret webhook, credentials Google) sont lus depuis des
variables d'environnement.
"""
import os

# --- Secrets (definis via variables d'environnement ou un fichier .env) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "changeme")

# --- Lignes de production ---
# Le code interne SKD est conserve en base (traçabilite historique) ; seul le
# nom affiche change en "Ligne 03" (spec v2 §2).
LIGNES = {
    "A":   {"nom_affiche": "Ligne Auto",      "objectif_horaire": 133},
    "S":   {"nom_affiche": "Ligne Semi-auto", "objectif_horaire": 160},
    "SKD": {"nom_affiche": "Ligne 03",        "objectif_horaire": 80},
}

ORDRE_AFFICHAGE = ["A", "S", "SKD"]

# Alias de saisie acceptes (insensibles a la casse) -> code interne.
CODES_ACCEPTES = {
    "A": "A",
    "S": "S",
    "SKD": "SKD",
    "L3": "SKD",
    "03": "SKD",
    "3": "SKD",
}

# --- Grilles horaires des postes (spec v2 §3 et §4) ---
# Chaque point : heure de controle, coefficient d'objectif applique a ce
# creneau, et `final` si c'est le dernier point du poste (declenche la
# synthese). L'objectif d'un point = floor(objectif_horaire_ligne * coef).
POSTE_1 = {
    "actif_par_defaut": True,
    "debut": "08:00",
    "fin": "16:30",
    "tolerance_minutes": 15,
    "points": [
        {"heure": "09:00", "coef": 1.0},
        {"heure": "10:00", "coef": 1.0},
        {"heure": "11:00", "coef": 1.0},
        {"heure": "12:00", "coef": 1.0},
        {"heure": "13:00", "coef": 0.5, "libelle": "pause déjeuner"},
        {"heure": "14:00", "coef": 1.0},
        {"heure": "15:00", "coef": 1.0},
        {"heure": "16:00", "coef": 1.0},
        {"heure": "16:30", "coef": 0.4, "final": True, "libelle": "arrêt lignes 16:24"},
    ],
}

POSTE_2 = {
    # Le poste 2 ne tourne pas par defaut : activation explicite via /poste2,
    # valable un seul jour (spec v2 §4.1).
    "actif_par_defaut": False,
    "compteur_mes_reinitialise": True,
    "debut": "16:30",
    "fin": "00:30",
    "tolerance_minutes": 15,
    "points": [
        {"heure": "17:30", "coef": 1.0},
        {"heure": "18:30", "coef": 1.0},
        {"heure": "19:30", "coef": 0.5, "libelle": "pause repas"},
        {"heure": "20:30", "coef": 1.0},
        {"heure": "21:30", "coef": 1.0},
        {"heure": "22:30", "coef": 1.0},
        {"heure": "23:30", "coef": 1.0},
        {"heure": "00:30", "coef": 1.0, "final": True},
    ],
}

POSTES = {1: POSTE_1, 2: POSTE_2}

# --- Fuseau horaire ---
TIMEZONE = "Africa/Algiers"

# --- Jours ouvres (le vendredi est travaille, meme horaires — spec v2 §9) ---
JOURS_OUVRES = ["lun", "mar", "mer", "jeu", "ven", "sam"]

# --- Base de donnees ---
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "bot.sqlite3"))

# --- Groupe Telegram et autorisations (spec v2 §1.4) ---
# ID du groupe Telegram autorise a utiliser le bot (entier negatif, ex.
# -1001234567890). 0 = non configure -> le bot reste permissif dans tous les
# groupes tant que cette valeur n'est pas renseignee, pour ne pas bloquer le
# demarrage avant que l'utilisateur ait rempli sa config.
GROUPE_AUTORISE = int(os.environ.get("GROUPE_AUTORISE", "0") or "0")


def _parse_id_list(raw):
    return [int(x) for x in raw.replace(";", ",").split(",") if x.strip()]


AUTORISATIONS = {
    # IDs Telegram autorises a saisir (commandes SAISIE). Liste vide = pas de
    # restriction de saisie tant que non configure (meme logique que
    # GROUPE_AUTORISE ci-dessus).
    "saisie": _parse_id_list(os.environ.get("AUTORISATIONS_SAISIE", "")),
    "lecture": "*",
}

# --- Graphique (spec v2 §6.2) ---
GRAPH_COLORS = {"A": "#2a78d6", "S": "#eb6834", "SKD": "#1baf7a"}
GRAPH_DASHES = {"A": "solid", "S": "dashed", "SKD": "dotted"}
GRAPH_SIZE_PX = (1200, 700)
GRAPH_DPI = 100

# --- Export Excel local (spec v2 §7) ---
EXCEL_LOCAL_DIR = os.environ.get(
    "EXCEL_LOCAL_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "exports")
)

# --- Google Sheets / Drive (spec v2 §7.3-7.5) ---
# Chemin d'un fichier JSON de credentials de compte de service, OU le JSON
# lui-meme inline dans la variable d'environnement. Absent -> toutes les
# fonctions Google sont des no-op journalisees (le bot reste pleinement
# fonctionnel sans Drive : Excel local + graphiques marchent quand meme).
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_ENABLED = bool(GOOGLE_SERVICE_ACCOUNT_JSON)
GOOGLE_SPREADSHEET_ID = os.environ.get("GOOGLE_SPREADSHEET_ID", "")
GOOGLE_DRIVE_ARCHIVES_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_ARCHIVES_FOLDER_ID", "")
GOOGLE_DRIVE_BACKUPS_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_BACKUPS_FOLDER_ID", "")
# Ecriture a chaque point de controle plutot qu'a la seule cloture (spec v2
# §7.5c) — desactive par defaut, la cloture suffit et economise des appels API.
PUSH_IMMEDIAT = os.environ.get("PUSH_IMMEDIAT", "false").strip().lower() == "true"
DB_BACKUP_RETENTION_JOURS = 30
