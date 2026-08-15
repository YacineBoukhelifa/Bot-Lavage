#!/bin/bash
# ⚠️ OBSOLETE (v1 uniquement) — ce script recree seulement les fichiers v1
# et N'A PAS ete mis a jour pour la v2 (postes, groupe, graphique, Excel,
# Google Sheets/Drive...). Utilisez `git clone` ou l'onglet Files de
# PythonAnywhere a la place (voir DEPLOY.md §3). Conserve ici pour
# reference historique uniquement.
#
# A COLLER TEL QUEL dans la Bash console de PythonAnywhere (onglet Consoles).
# Recree l'integralite du projet dans ~/telegrm-bot sans passer par git/upload.
set -e

mkdir -p ~/telegrm-bot/bot ~/telegrm-bot/data
cd ~/telegrm-bot

cat > requirements.txt << 'PYEOF'
Flask==3.0.3
requests==2.32.3
python-dotenv==1.0.1
tzdata==2024.1
PYEOF

cat > bot/__init__.py << 'PYEOF'
PYEOF

cat > bot/config.py << 'PYEOF'
"""
Configuration du bot — modifiable sans toucher au code métier.
Les secrets (token, secret webhook) sont lus depuis des variables d'environnement.
"""
import os

# --- Secrets (definis via variables d'environnement ou un fichier .env) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "changeme")

# --- Lignes de production ---
LIGNES = {
    "A": {"nom": "Ligne Auto", "objectif_horaire": 133},
    "S": {"nom": "Ligne Semi-auto", "objectif_horaire": 160},
    "SKD": {"nom": "Ligne SKD", "objectif_horaire": None},
}

# --- Points de controle (heures prevues, format HH:MM) ---
POINTS_DE_CONTROLE = [
    "09:00", "10:00", "11:00", "12:00",
    "13:00", "14:00", "15:00", "16:00", "16:30",
]

# --- Tolerance de rattachement horaire (minutes) ---
TOLERANCE_MINUTES = 15

# --- Poste ---
POSTE = {"debut": "08:00", "fin": "16:30"}

# --- Fuseau horaire ---
TIMEZONE = "Africa/Algiers"

# --- Base de donnees ---
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "bot.sqlite3"))
PYEOF

cat > bot/db.py << 'PYEOF'
"""Couche de persistance SQLite."""
import os
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    date TEXT PRIMARY KEY,
    statut TEXT NOT NULL CHECK(statut IN ('actif', 'cloture'))
);

CREATE TABLE IF NOT EXISTS checkpoints (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    heure TEXT NOT NULL,
    cumul_envoye REAL NOT NULL,
    production_heure REAL NOT NULL,
    ecart REAL,
    ecart_pct REAL,
    horodatage_reel TEXT NOT NULL,
    PRIMARY KEY (date, code, heure)
);

CREATE TABLE IF NOT EXISTS pending_anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    date TEXT NOT NULL,
    heure TEXT NOT NULL,
    code TEXT NOT NULL,
    cumul_precedent REAL NOT NULL,
    cumul_nouveau REAL NOT NULL,
    horodatage_reel TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS last_report (
    chat_id TEXT PRIMARY KEY,
    text TEXT NOT NULL
);
"""


def get_connection():
    os.makedirs(os.path.dirname(os.path.abspath(config.DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
PYEOF

cat > bot/keyboards.py << 'PYEOF'
"""Claviers Telegram (boutons) affiches avec les messages du bot."""

BTN_START_DAY = "🟢 Démarrer la journée"
BTN_RECAP = "📋 Récap"
BTN_HELP = "❓ Aide"

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": BTN_START_DAY}],
        [{"text": BTN_RECAP}, {"text": BTN_HELP}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

ANOMALY_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "✅ Confirmer", "callback_data": "confirm_anomaly"},
            {"text": "❌ Annuler", "callback_data": "cancel_anomaly"},
        ]
    ]
}

REPORT_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "📋 Copier le rapport", "callback_data": "copy_last_report"}]
    ]
}

# Fait correspondre le texte affiche sur un bouton a la commande equivalente.
BUTTON_TO_COMMAND = {
    BTN_START_DAY.lower(): "/start_day",
    BTN_RECAP.lower(): "/recap",
    BTN_HELP.lower(): "/help",
}
PYEOF

cat > bot/telegram_client.py << 'PYEOF'
"""Client HTTP minimal pour l'API Telegram Bot (appels directs via `requests`)."""
import requests

from . import config

API_BASE = "https://api.telegram.org/bot{token}"


def send_message(chat_id, text, reply_markup=None):
    url = f"{API_BASE.format(token=config.BOT_TOKEN)}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def answer_callback_query(callback_query_id, text=None):
    url = f"{API_BASE.format(token=config.BOT_TOKEN)}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text is not None:
        payload["text"] = text
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def set_webhook(webhook_url):
    url = f"{API_BASE.format(token=config.BOT_TOKEN)}/setWebhook"
    resp = requests.post(url, json={"url": webhook_url}, timeout=10)
    resp.raise_for_status()
    return resp.json()
PYEOF

cat > bot/logic.py << 'PYEOF'
"""Logique metier: rattachement horaire, calcul des deltas, formatage du rapport.

Ce module est pur (pas d'acces reseau) pour rester facilement testable.
La persistance (SQLite) est injectee via l'objet `conn` (sqlite3.Connection).
"""
import re
from datetime import datetime, timedelta

from . import config

LINE_CODE_RE = re.compile(r"^([A-Za-z]+)\s+(-?\d+(?:[.,]\d+)?)$")


# ---------------------------------------------------------------------------
# Parsing du message entrant
# ---------------------------------------------------------------------------

def parse_checkin(text):
    """Parse un message de check-in en lignes 'CODE valeur'.

    Retourne (reconnues: {code: float}, non_reconnues: [code brut],
              malformees: [ligne brute]).
    """
    reconnues = {}
    non_reconnues = []
    malformees = []

    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = LINE_CODE_RE.match(line)
        if not m:
            malformees.append(raw_line)
            continue
        code_raw, value_raw = m.group(1), m.group(2)
        code = code_raw.upper()
        value = float(value_raw.replace(",", "."))
        if code in config.LIGNES:
            reconnues[code] = value
        else:
            non_reconnues.append(code_raw)

    return reconnues, non_reconnues, malformees


# ---------------------------------------------------------------------------
# Rattachement a un point de controle (time-snapping)
# ---------------------------------------------------------------------------

def _to_minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def snap_to_checkpoint(dt, checkpoints=None, tolerance_minutes=None):
    """Rattache un horodatage au point de controle prevu le plus proche.

    Retourne un dict:
      {"matched": "11:00"} si un point est trouve dans la tolerance,
      {"matched": None, "avant": "10:00" | None, "apres": "11:00" | None}
      sinon (avant/apres = points de controle encadrants, pour le message d'erreur).
    """
    checkpoints = checkpoints or config.POINTS_DE_CONTROLE
    tolerance_minutes = (
        config.TOLERANCE_MINUTES if tolerance_minutes is None else tolerance_minutes
    )

    now_min = dt.hour * 60 + dt.minute
    best_cp = None
    best_dist = None
    for cp in checkpoints:
        dist = abs(_to_minutes(cp) - now_min)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_cp = cp

    if best_dist is not None and best_dist <= tolerance_minutes:
        return {"matched": best_cp}

    avant = None
    apres = None
    for cp in checkpoints:
        cp_min = _to_minutes(cp)
        if cp_min <= now_min:
            avant = cp
        elif apres is None:
            apres = cp
    return {"matched": None, "avant": avant, "apres": apres}


# ---------------------------------------------------------------------------
# Acces donnees (jour / checkpoints)
# ---------------------------------------------------------------------------

def get_day(conn, date):
    row = conn.execute("SELECT * FROM days WHERE date = ?", (date,)).fetchone()
    return row


def start_day(conn, date):
    conn.execute("DELETE FROM checkpoints WHERE date = ?", (date,))
    conn.execute("DELETE FROM pending_anomalies WHERE date = ?", (date,))
    conn.execute(
        "INSERT INTO days (date, statut) VALUES (?, 'actif') "
        "ON CONFLICT(date) DO UPDATE SET statut='actif'",
        (date,),
    )
    conn.commit()


def _checkpoint_index(heure):
    return config.POINTS_DE_CONTROLE.index(heure)


def heure_debut_periode(heure):
    """Borne de debut de la periode couverte par ce point de controle :
    le point de controle precedent dans le planning, ou le debut de poste
    si `heure` est le premier point de controle prevu."""
    idx = _checkpoint_index(heure)
    if idx == 0:
        return config.POSTE["debut"]
    return config.POINTS_DE_CONTROLE[idx - 1]


def get_cumul_precedent(conn, date, code, heure):
    """Dernier cumul enregistre pour (ligne, jour) avant `heure` (0 si aucun)."""
    idx = _checkpoint_index(heure)
    points_avant = config.POINTS_DE_CONTROLE[:idx]
    if not points_avant:
        return 0.0
    placeholders = ",".join("?" for _ in points_avant)
    row = conn.execute(
        f"SELECT cumul_envoye, heure FROM checkpoints "
        f"WHERE date = ? AND code = ? AND heure IN ({placeholders}) "
        f"ORDER BY heure DESC",
        (date, code, *points_avant),
    ).fetchall()
    if not row:
        return 0.0
    # Les heures sont triees lexicographiquement = triees chronologiquement (HH:MM).
    row_sorted = sorted(row, key=lambda r: _checkpoint_index(r["heure"]))
    return row_sorted[-1]["cumul_envoye"]


def get_dernier_cumul_jour(conn, date, code):
    row = conn.execute(
        "SELECT cumul_envoye, heure FROM checkpoints WHERE date = ? AND code = ?",
        (date, code),
    ).fetchall()
    if not row:
        return None
    row_sorted = sorted(row, key=lambda r: _checkpoint_index(r["heure"]))
    return row_sorted[-1]["cumul_envoye"]


def line_has_any_checkpoint(conn, date, code):
    row = conn.execute(
        "SELECT 1 FROM checkpoints WHERE date = ? AND code = ? LIMIT 1", (date, code)
    ).fetchone()
    return row is not None


def save_checkpoint(conn, date, code, heure, cumul_nouveau, horodatage_reel, cumul_precedent):
    objectif = config.LIGNES[code]["objectif_horaire"]
    production = cumul_nouveau - cumul_precedent
    ecart = production - objectif if objectif is not None else None
    ecart_pct = (ecart / objectif * 100) if objectif not in (None, 0) else None
    conn.execute(
        "INSERT INTO checkpoints "
        "(date, code, heure, cumul_envoye, production_heure, ecart, ecart_pct, horodatage_reel) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(date, code, heure) DO UPDATE SET "
        "cumul_envoye=excluded.cumul_envoye, production_heure=excluded.production_heure, "
        "ecart=excluded.ecart, ecart_pct=excluded.ecart_pct, "
        "horodatage_reel=excluded.horodatage_reel",
        (date, code, heure, cumul_nouveau, production, ecart, ecart_pct, horodatage_reel),
    )
    conn.commit()
    return {
        "code": code,
        "nom": config.LIGNES[code]["nom"],
        "production": production,
        "objectif": objectif,
        "ecart": ecart,
        "ecart_pct": ecart_pct,
        "cumul": cumul_nouveau,
    }


# ---------------------------------------------------------------------------
# Anomalies (cumul < cumul precedent)
# ---------------------------------------------------------------------------

def queue_anomaly(conn, chat_id, date, heure, code, cumul_precedent, cumul_nouveau, horodatage_reel):
    conn.execute(
        "INSERT INTO pending_anomalies "
        "(chat_id, date, heure, code, cumul_precedent, cumul_nouveau, horodatage_reel) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chat_id, date, heure, code, cumul_precedent, cumul_nouveau, horodatage_reel),
    )
    conn.commit()


def get_pending_anomalies(conn, chat_id):
    return conn.execute(
        "SELECT * FROM pending_anomalies WHERE chat_id = ? ORDER BY id", (chat_id,)
    ).fetchall()


def clear_pending_anomalies(conn, chat_id):
    conn.execute("DELETE FROM pending_anomalies WHERE chat_id = ?", (chat_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Dernier rapport envoye (pour le bouton "Copier")
# ---------------------------------------------------------------------------

def save_last_report(conn, chat_id, text):
    conn.execute(
        "INSERT INTO last_report (chat_id, text) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET text=excluded.text",
        (chat_id, text),
    )
    conn.commit()


def get_last_report(conn, chat_id):
    row = conn.execute("SELECT text FROM last_report WHERE chat_id = ?", (chat_id,)).fetchone()
    return row["text"] if row else None


# ---------------------------------------------------------------------------
# Traitement complet d'un check-in
# ---------------------------------------------------------------------------

ORDRE_AFFICHAGE = ["A", "S", "SKD"]


def process_checkin(conn, chat_id, date, dt, text):
    """Traite un message de check-in complet. Retourne un dict de resultat:

    {"status": "error", "message": str}
    {"status": "anomaly", "message": str}   # attend confirmation oui/non
    {"status": "report", "message": str}
    """
    day = get_day(conn, date)
    if day is None or day["statut"] != "actif":
        return {
            "status": "error",
            "message": (
                "Aucune journee active pour aujourd'hui. "
                "Envoyez /start_day pour demarrer le poste avant d'enregistrer des chiffres."
            ),
        }

    reconnues, non_reconnues, malformees = parse_checkin(text)

    if not reconnues and not non_reconnues and not malformees:
        return {
            "status": "error",
            "message": "Message non reconnu. Envoyez /help pour voir le format attendu.",
        }

    snap = snap_to_checkpoint(dt)
    if snap["matched"] is None:
        avant = snap["avant"] or "aucun"
        apres = snap["apres"] or "aucun"
        return {
            "status": "error",
            "message": (
                f"Aucun point de controle proche de cet horaire "
                f"(recu a {dt.strftime('%H:%M')}) — dernier point : {avant}, prochain : {apres}."
            ),
        }
    heure = snap["matched"]
    horodatage_reel = dt.isoformat()

    resultats = []
    anomalies = []
    for code, cumul_nouveau in reconnues.items():
        cumul_precedent = get_cumul_precedent(conn, date, code, heure)
        production = cumul_nouveau - cumul_precedent
        if production < 0:
            queue_anomaly(conn, chat_id, date, heure, code, cumul_precedent, cumul_nouveau, horodatage_reel)
            anomalies.append((code, cumul_precedent, cumul_nouveau))
        else:
            resultats.append(
                save_checkpoint(conn, date, code, heure, cumul_nouveau, horodatage_reel, cumul_precedent)
            )

    lines_out = []
    if resultats:
        lines_out.append(format_report(date, heure, resultats))
    if anomalies:
        lines_out.append(format_anomaly_warning(anomalies))
    if non_reconnues:
        lines_out.append(
            "⚠️ Code(s) de ligne non reconnu(s), ignore(s) : " + ", ".join(non_reconnues)
        )
    if malformees:
        lines_out.append(
            "⚠️ Ligne(s) mal formee(s), ignoree(s) : " + " / ".join(malformees)
        )

    status = "anomaly" if (anomalies and not resultats) else "report"
    return {"status": status, "message": "\n\n".join(lines_out)}


def confirm_pending_anomalies(conn, chat_id):
    pending = get_pending_anomalies(conn, chat_id)
    if not pending:
        return {"status": "error", "message": "Aucune anomalie en attente de confirmation."}

    resultats = []
    date = pending[0]["date"]
    heure = pending[0]["heure"]
    for row in pending:
        resultats.append(
            save_checkpoint(
                conn, row["date"], row["code"], row["heure"],
                row["cumul_nouveau"], row["horodatage_reel"], row["cumul_precedent"],
            )
        )
    clear_pending_anomalies(conn, chat_id)
    message = "✅ Valeur(s) confirmee(s) et enregistree(s).\n\n" + format_report(date, heure, resultats)
    return {"status": "report", "message": message}


def cancel_pending_anomalies(conn, chat_id):
    pending = get_pending_anomalies(conn, chat_id)
    if not pending:
        return {"status": "error", "message": "Aucune anomalie en attente de confirmation."}
    clear_pending_anomalies(conn, chat_id)
    return {"status": "cancelled", "message": "❌ Valeur(s) annulee(s), rien n'a ete enregistre."}


# ---------------------------------------------------------------------------
# Formatage des messages
# ---------------------------------------------------------------------------

def fmt_qty(x):
    if x is None:
        return "-"
    if float(x).is_integer():
        return str(int(x))
    return f"{x:.1f}"


def fmt_signed_qty(x):
    if x is None:
        return "-"
    sign = "+" if x >= 0 else ""
    return f"{sign}{fmt_qty(x)}"


def fmt_signed_pct(x):
    if x is None:
        return None
    rounded = round(x, 1)
    if float(rounded).is_integer():
        val = f"{int(rounded)}"
    else:
        val = f"{rounded:.1f}"
    sign = "+" if rounded >= 0 else ""
    return f"{sign}{val}"


def format_report(date, heure, resultats):
    date_obj = datetime.strptime(date, "%Y-%m-%d")
    lignes_par_code = {r["code"]: r for r in resultats}

    heure_debut = heure_debut_periode(heure)
    header = "\n".join(
        [
            "🏭 RAPPORT HORAIRE — PRODUCTION",
            f"📅 {date_obj.strftime('%d/%m/%Y')}",
            f"⏰ {heure_debut} - {heure}",
        ]
    )
    blocs = []
    for code in ORDRE_AFFICHAGE:
        if code not in lignes_par_code:
            continue
        r = lignes_par_code[code]
        bloc = [f"🏗️ {r['nom']}", f"✅ Production : {fmt_qty(r['production'])} pcs"]
        if r["objectif"] is not None:
            bloc.append(f"🎯 Objectif : {fmt_qty(r['objectif'])} pcs")
            pct = fmt_signed_pct(r["ecart_pct"])
            bloc.append(f"📊 Écart : {fmt_signed_qty(r['ecart'])} pcs ({pct}%)")
        bloc.append(f"📈 Cumul jour : {fmt_qty(r['cumul'])} pcs")
        blocs.append("\n".join(bloc))

    return header + "\n\n" + "\n\n".join(blocs)


def format_anomaly_warning(anomalies):
    lines = [
        "⚠️ ANOMALIE — cumul inferieur au precedent (delta negatif) :",
    ]
    for code, precedent, nouveau in anomalies:
        nom = config.LIGNES[code]["nom"]
        lines.append(f"  • {nom} : precedent={fmt_qty(precedent)}, envoye={fmt_qty(nouveau)}")
    lines.append("Repondez \"oui\" pour confirmer et enregistrer ces valeurs, ou \"non\" pour annuler.")
    return "\n".join(lines)


def format_recap(conn, date):
    day = get_day(conn, date)
    if day is None:
        return f"Aucune journee active pour le {date}."

    date_obj = datetime.strptime(date, "%Y-%m-%d")
    lines = [f"📋 RECAP — {date_obj.strftime('%d/%m/%Y')} ({day['statut']})", ""]
    any_line = False
    for code in ORDRE_AFFICHAGE:
        if not line_has_any_checkpoint(conn, date, code):
            continue
        any_line = True
        cumul = get_dernier_cumul_jour(conn, date, code)
        last_row = conn.execute(
            "SELECT heure FROM checkpoints WHERE date=? AND code=? ORDER BY heure DESC",
            (date, code),
        ).fetchall()
        last_heure = sorted(last_row, key=lambda r: _checkpoint_index(r["heure"]))[-1]["heure"]
        nom = config.LIGNES[code]["nom"]
        lines.append(f"🏗️ {nom} : cumul={fmt_qty(cumul)} pcs (dernier point : {last_heure})")
    if not any_line:
        lines.append("Aucun point de controle enregistre pour l'instant.")
    return "\n".join(lines)
PYEOF

cat > bot/app.py << 'PYEOF'
"""Application Flask — reception des webhooks Telegram et dispatch des commandes."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request

from . import config, db, keyboards, logic, telegram_client

app = Flask(__name__)
db.init_db()

ALGIERS_TZ = ZoneInfo(config.TIMEZONE)

HELP_TEXT = (
    "🤖 Bot rapport horaire — BU Lavage\n\n"
    "Utilisez les boutons ci-dessous, ou les commandes :\n"
    "/start_day — demarre la journee, remet les cumuls a 0\n"
    "/recap — resume de la journee en cours\n"
    "/help — ce message\n\n"
    "Pour envoyer un point de controle, un message avec une ligne par code :\n"
    "A 800\n"
    "S 660\n"
    "SKD 90\n\n"
    "Le chiffre envoye est le CUMUL TOTAL du jour, pas la production de l'heure. "
    "Codes reconnus : A (Ligne Auto), S (Ligne Semi-auto), SKD (Ligne SKD)."
)


def _local_now_from_update(message):
    ts = message.get("date")
    if ts is None:
        return datetime.now(ALGIERS_TZ)
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ALGIERS_TZ)


def _finalize(conn, chat_id, result):
    """Persiste le texte si c'est un rapport (pour le bouton Copier), puis
    normalise vers {"text": str, "status": str}."""
    if result["status"] == "report":
        logic.save_last_report(conn, str(chat_id), result["message"])
    return {"text": result["message"], "status": result["status"]}


def handle_message(chat_id, text, dt):
    """Retourne {"text": str, "status": str}. status pilote le clavier renvoye."""
    conn = db.get_connection()
    try:
        stripped = text.strip()
        lowered = stripped.lower()

        command = keyboards.BUTTON_TO_COMMAND.get(lowered)
        if command:
            lowered = command
            stripped = command

        if lowered.startswith("/start_day"):
            date = dt.strftime("%Y-%m-%d")
            logic.start_day(conn, date)
            return {
                "text": f"✅ Journee demarree pour le {dt.strftime('%d/%m/%Y')}. Cumuls remis a 0.",
                "status": "info",
            }

        if lowered.startswith("/recap"):
            date = dt.strftime("%Y-%m-%d")
            return {"text": logic.format_recap(conn, date), "status": "info"}

        if lowered.startswith("/help") or lowered.startswith("/start"):
            return {"text": HELP_TEXT, "status": "info"}

        pending = logic.get_pending_anomalies(conn, str(chat_id))
        if pending and lowered in ("oui", "confirmer", "yes", "confirm"):
            result = logic.confirm_pending_anomalies(conn, str(chat_id))
            return _finalize(conn, chat_id, result)
        if pending and lowered in ("non", "annuler", "no", "cancel"):
            result = logic.cancel_pending_anomalies(conn, str(chat_id))
            return _finalize(conn, chat_id, result)

        date = dt.strftime("%Y-%m-%d")
        result = logic.process_checkin(conn, str(chat_id), date, dt, stripped)
        return _finalize(conn, chat_id, result)
    finally:
        conn.close()


def handle_callback(chat_id, data):
    conn = db.get_connection()
    try:
        if data == "confirm_anomaly":
            result = logic.confirm_pending_anomalies(conn, str(chat_id))
            return _finalize(conn, chat_id, result)
        if data == "cancel_anomaly":
            result = logic.cancel_pending_anomalies(conn, str(chat_id))
            return _finalize(conn, chat_id, result)
        if data == "copy_last_report":
            text = logic.get_last_report(conn, str(chat_id))
            if text is None:
                return {"text": "Aucun rapport a copier pour l'instant.", "status": "info"}
            return {"text": text, "status": "report"}
        return {"text": "Action inconnue.", "status": "error"}
    finally:
        conn.close()


def _reply_markup_for(status):
    if status == "report":
        return keyboards.REPORT_KEYBOARD
    if status == "anomaly":
        return keyboards.ANOMALY_KEYBOARD
    return keyboards.MAIN_KEYBOARD


@app.route(f"/webhook/{config.WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}

    callback_query = update.get("callback_query")
    if callback_query:
        chat_id = callback_query["message"]["chat"]["id"]
        try:
            result = handle_callback(chat_id, callback_query.get("data", ""))
        except Exception as exc:  # noqa: BLE001 - le bot doit toujours repondre
            result = {"text": f"❌ Erreur interne : {exc}", "status": "error"}
        telegram_client.answer_callback_query(callback_query["id"])
        telegram_client.send_message(chat_id, result["text"], reply_markup=_reply_markup_for(result["status"]))
        return jsonify({"ok": True})

    message = update.get("message") or update.get("edited_message")
    if not message or "text" not in message:
        return jsonify({"ok": True})

    chat_id = message["chat"]["id"]
    text = message["text"]
    dt = _local_now_from_update(message)

    try:
        result = handle_message(chat_id, text, dt)
    except Exception as exc:  # noqa: BLE001 - le bot doit toujours repondre
        result = {"text": f"❌ Erreur interne lors du traitement du message : {exc}", "status": "error"}

    telegram_client.send_message(chat_id, result["text"], reply_markup=_reply_markup_for(result["status"]))
    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True)
PYEOF

echo "Fichiers crees dans ~/telegrm-bot"
echo "Prochaine etape : pip install --user -r requirements.txt"
