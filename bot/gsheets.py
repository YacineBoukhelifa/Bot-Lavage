"""Écriture Google Sheets API v4 (spec v2 §7.5c) — surface de travail vivante.

Toutes les fonctions sont des no-op journalisés tant que
`config.GOOGLE_ENABLED` est faux (pas de service account configuré) : le
bot, l'Excel local (`bot.excel_export`) et les graphiques restent
pleinement fonctionnels sans Google Sheets. Les dépendances Google ne sont
importées qu'au moment de l'appel, pour ne jamais bloquer le démarrage du
bot si elles ne sont pas installées.
"""
import logging

from . import config, logic

logger = logging.getLogger(__name__)

DONNEES_HEADERS = [
    "Date production", "Jour", "Poste", "Ligne", "Code", "Point", "Créneau", "Coef",
    "Cumul", "Production", "Objectif", "Écart", "Écart %", "Saisi par", "Horodatage réel",
]

OVERWRITE_BANNER = "⚠️ Onglet régénéré automatiquement — toute modification manuelle sera écrasée à la prochaine clôture."


def _service():
    """Construit le client Sheets API v4 authentifié en compte de service.

    PythonAnywhere gratuit impose un proxy HTTP sortant, et `httplib2` (le
    transport par défaut de `googleapiclient`) ne lit pas toujours les
    variables d'environnement de proxy automatiquement (spec v2 §7.4) — on
    force explicitement `httplib2.proxy_info_from_environment()` plutôt que
    de compter sur la détection automatique."""
    import json

    import httplib2
    from google.oauth2 import service_account
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    raw = config.GOOGLE_SERVICE_ACCOUNT_JSON
    info = json.loads(raw) if raw.strip().startswith("{") else json.load(open(raw, encoding="utf-8"))
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    http = AuthorizedHttp(credentials, http=httplib2.Http(proxy_info=httplib2.proxy_info_from_environment()))
    return build("sheets", "v4", http=http, cache_discovery=False)


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


def _ensure_donnees_header(service):
    """Ecrit la ligne d'en-tete si l'onglet Donnees est encore vide (premier
    appel seulement — les appels suivants trouvent la ligne 1 deja remplie)."""
    res = service.spreadsheets().values().get(
        spreadsheetId=config.GOOGLE_SPREADSHEET_ID, range="Donnees!A1",
    ).execute()
    if not res.get("values"):
        service.spreadsheets().values().update(
            spreadsheetId=config.GOOGLE_SPREADSHEET_ID, range="Donnees!A1",
            valueInputOption="RAW", body={"values": [DONNEES_HEADERS]},
        ).execute()


def append_donnees_row(checkpoint_row):
    """Ajoute une ligne à l'onglet Donnees (append incrémental, §7.5c)."""
    if not config.GOOGLE_ENABLED or not config.GOOGLE_SPREADSHEET_ID:
        logger.info("gsheets: desactive (pas de service account/spreadsheet configure) — append ignore")
        return False
    try:
        service = _service()
        _ensure_donnees_header(service)
        body = {"values": [_row_from_checkpoint(checkpoint_row)]}
        service.spreadsheets().values().append(
            spreadsheetId=config.GOOGLE_SPREADSHEET_ID,
            range="Donnees!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()
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
        service = _service()
        service.spreadsheets().values().clear(
            spreadsheetId=config.GOOGLE_SPREADSHEET_ID, range=f"{sheet_name}!A2:ZZ",
        ).execute()
        service.spreadsheets().values().update(
            spreadsheetId=config.GOOGLE_SPREADSHEET_ID, range=f"{sheet_name}!A1",
            valueInputOption="RAW", body={"values": [[OVERWRITE_BANNER]]},
        ).execute()
        body = {"values": [header] + rows}
        service.spreadsheets().values().update(
            spreadsheetId=config.GOOGLE_SPREADSHEET_ID,
            range=f"{sheet_name}!A2",
            valueInputOption="RAW",
            body=body,
        ).execute()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("gsheets: echec de l'overwrite %s", sheet_name)
        return False
