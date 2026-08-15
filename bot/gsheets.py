"""Écriture Google Sheets API v4 (spec v2 §7.5c) — surface de travail vivante.

Toutes les fonctions sont des no-op journalisés tant que
`config.GOOGLE_ENABLED` est faux (pas de service account configuré) : le
bot, l'Excel local (`bot.excel_export`) et les graphiques restent
pleinement fonctionnels sans Google Sheets.

⚠️ Appelle l'API REST directement via `requests`, jamais via
`googleapiclient`/`httplib2` : sur PythonAnywhere gratuit, `httplib2`
tente une connexion directe (`OSError: Network is unreachable`) au lieu de
passer par le proxy sortant imposé, alors que `requests` — déjà utilisé
pour Telegram — passe sans problème (constaté en conditions réelles,
15/08/2026). `google.auth.transport.requests.Request` (aussi basé sur
`requests`) sert uniquement à rafraîchir le jeton d'accès.
"""
import logging
from urllib.parse import quote

import requests

from . import config, logic

logger = logging.getLogger(__name__)

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

DONNEES_HEADERS = [
    "Date production", "Jour", "Poste", "Ligne", "Code", "Point", "Créneau", "Coef",
    "Cumul", "Production", "Objectif", "Écart", "Écart %", "Saisi par", "Horodatage réel",
]

OVERWRITE_BANNER = "⚠️ Onglet régénéré automatiquement — toute modification manuelle sera écrasée à la prochaine clôture."


def _access_token():
    """Jeton d'acces frais pour le compte de service, via le transport
    `requests` (jamais `httplib2` — voir docstring du module)."""
    import json

    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    raw = config.GOOGLE_SERVICE_ACCOUNT_JSON
    info = json.loads(raw) if raw.strip().startswith("{") else json.load(open(raw, encoding="utf-8"))
    credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    credentials.refresh(Request())
    return credentials.token


def _headers():
    return {"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"}


def _range_url(sheet_name, a1_range, suffix=""):
    return f"{SHEETS_API}/{config.GOOGLE_SPREADSHEET_ID}/values/{quote(f'{sheet_name}!{a1_range}', safe='')}{suffix}"


def _row_from_checkpoint(row):
    from datetime import datetime

    date_obj = datetime.strptime(row["date"], "%Y-%m-%d")
    creneau = f"{logic.heure_debut_periode(row['poste'], row['heure'])}-{row['heure']}"
    jours_fr = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    return [
        date_obj.strftime("%d/%m/%Y"), jours_fr[date_obj.weekday()], row["poste"],
        config.LIGNES[row["code"]]["nom_affiche"], row["code"], row["heure"], creneau,
        row["coef"], row["cumul_envoye"], row["production_heure"], row["objectif"],
        row["ecart"], row["ecart_pct"], row["saisi_par_nom"] or row["saisi_par_id"] or "-",
        row["horodatage_reel"],
    ]


def _ensure_donnees_header(headers):
    """Ecrit la ligne d'en-tete si l'onglet Donnees est encore vide (premier
    appel seulement — les appels suivants trouvent la ligne 1 deja remplie)."""
    resp = requests.get(_range_url("Donnees", "A1"), headers=headers, timeout=15)
    resp.raise_for_status()
    if not resp.json().get("values"):
        resp = requests.put(
            _range_url("Donnees", "A1"), headers=headers,
            params={"valueInputOption": "RAW"}, json={"values": [DONNEES_HEADERS]}, timeout=15,
        )
        resp.raise_for_status()


def append_donnees_row(checkpoint_row):
    """Ajoute une ligne à l'onglet Donnees (append incrémental, §7.5c)."""
    if not config.GOOGLE_ENABLED or not config.GOOGLE_SPREADSHEET_ID:
        logger.info("gsheets: desactive (pas de service account/spreadsheet configure) — append ignore")
        return False
    try:
        headers = _headers()
        _ensure_donnees_header(headers)
        resp = requests.post(
            _range_url("Donnees", "A1", ":append"), headers=headers,
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            json={"values": [_row_from_checkpoint(checkpoint_row)]}, timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception:  # noqa: BLE001 - ne doit jamais faire echouer le traitement du check-in
        logger.exception("gsheets: echec de l'append Donnees")
        return False


def overwrite_sheet(sheet_name, header, rows):
    """Remplace le contenu (hors ligne de bandeau) d'un onglet géré par le
    bot — utilisé pour Synthese_Quotidienne / Vue_Mensuelle."""
    if not config.GOOGLE_ENABLED or not config.GOOGLE_SPREADSHEET_ID:
        logger.info("gsheets: desactive — overwrite %s ignore", sheet_name)
        return False
    try:
        headers = _headers()
        resp = requests.post(_range_url(sheet_name, "A2:ZZ", ":clear"), headers=headers, json={}, timeout=15)
        resp.raise_for_status()

        resp = requests.put(
            _range_url(sheet_name, "A1"), headers=headers,
            params={"valueInputOption": "RAW"}, json={"values": [[OVERWRITE_BANNER]]}, timeout=15,
        )
        resp.raise_for_status()

        resp = requests.put(
            _range_url(sheet_name, "A2"), headers=headers,
            params={"valueInputOption": "RAW"}, json={"values": [header] + rows}, timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("gsheets: echec de l'overwrite %s", sheet_name)
        return False
