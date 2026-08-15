"""Google Drive API v3 — export .xlsx et sauvegarde .db (spec v2 §7.3-7.5).
No-op journalisé si `config.GOOGLE_ENABLED` est faux.

⚠️ Appelle l'API REST directement via `requests`, jamais via
`googleapiclient`/`httplib2` — voir `bot.gsheets` pour l'explication
complète (httplib2 se heurte a un `OSError: Network is unreachable` sur
PythonAnywhere gratuit, `requests` passe sans probleme).

⚠️ Autre limite de plateforme découverte en testant avec un compte Google
personnel (pas Google Workspace) : un compte de service n'a **aucun quota de
stockage propre**, donc créer un fichier échoue toujours avec
`storageQuotaExceeded` — même dans un dossier partagé en Éditeur. Seule la
mise à jour d'un fichier déjà possédé par un humain fonctionne (le stockage
est décompté du quota du propriétaire, pas de celui qui modifie).

Conséquence de conception : au lieu d'un fichier par mois / par jour (ce qui
demanderait au compte de service d'en créer un nouveau à chaque fois — donc
impossible), le bot pousse vers **un seul fichier fixe par dossier**
(dernier export, dernière sauvegarde), que l'utilisateur crée une seule fois
manuellement (voir DEPLOY.md §10). L'historique complet n'est pas perdu pour
autant : l'onglet `Donnees` du Google Sheet (bot.gsheets) est en ajout
seul et ne s'écrase jamais — c'est lui la vraie mémoire long terme.
"""
import logging

import requests

from . import config

logger = logging.getLogger(__name__)

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
SCOPES = ["https://www.googleapis.com/auth/drive"]

EXPORT_XLSX_NAME = "Production_BU_Lavage_dernier_export.xlsx"
BACKUP_DB_NAME = "bot_lavage_dernier.db"

_SETUP_HINT = (
    "Fichier '{name}' introuvable dans le dossier Drive {folder_id}. "
    "Un compte de service ne peut pas créer de fichier sur un Drive "
    "personnel (aucun quota propre) — créez-y une seule fois un fichier "
    "vide de ce nom exact (voir DEPLOY.md §10), le bot le mettra ensuite à "
    "jour automatiquement à chaque clôture."
)


def _access_token():
    import json

    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    raw = config.GOOGLE_SERVICE_ACCOUNT_JSON
    info = json.loads(raw) if raw.strip().startswith("{") else json.load(open(raw, encoding="utf-8"))
    credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    credentials.refresh(Request())
    return credentials.token


def _get_meta(conn, cle):
    row = conn.execute("SELECT valeur FROM drive_meta WHERE cle = ?", (cle,)).fetchone()
    return row["valeur"] if row else None


def _set_meta(conn, cle, valeur):
    conn.execute(
        "INSERT INTO drive_meta (cle, valeur) VALUES (?, ?) ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur",
        (cle, valeur),
    )
    conn.commit()


def _find_by_name(token, name, folder_id):
    query = f"name = '{name}' and '{folder_id}' in parents and trashed = false"
    resp = requests.get(
        f"{DRIVE_API}/files", headers={"Authorization": f"Bearer {token}"},
        params={"q": query, "fields": "files(id)", "pageSize": 1}, timeout=15,
    )
    resp.raise_for_status()
    files = resp.json().get("files", [])
    return files[0]["id"] if files else None


def _update_fixed_file(conn, token, meta_key, local_path, remote_name, folder_id, mimetype):
    """Met a jour le contenu d'un fichier FIXE (cree une fois par
    l'utilisateur) — ne tente jamais de le creer, cf. docstring du module."""
    file_id = _get_meta(conn, meta_key)
    if not file_id:
        file_id = _find_by_name(token, remote_name, folder_id)
        if file_id:
            _set_meta(conn, meta_key, file_id)

    if not file_id:
        raise RuntimeError(_SETUP_HINT.format(name=remote_name, folder_id=folder_id))

    with open(local_path, "rb") as f:
        data = f.read()
    resp = requests.patch(
        f"{DRIVE_UPLOAD_API}/files/{file_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": mimetype},
        params={"uploadType": "media"}, data=data, timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def export_monthly_xlsx(conn, local_xlsx_path, year_month):
    """Pousse `local_xlsx_path` (deja genere par bot.excel_export) vers le
    fichier fixe `EXPORT_XLSX_NAME` dans /Archives/ — instantane le plus
    recent, pas un historique par mois (voir docstring du module)."""
    if not config.GOOGLE_ENABLED:
        logger.info("gdrive: desactive — export xlsx (%s) ignore", year_month)
        return None
    try:
        token = _access_token()
        return _update_fixed_file(
            conn, token, "archive_export", local_xlsx_path, EXPORT_XLSX_NAME,
            config.GOOGLE_DRIVE_ARCHIVES_FOLDER_ID,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("gdrive: echec de l'export xlsx (%s)", year_month)
        raise RuntimeError(f"Export Drive : {exc}") from exc


def backup_db(conn, date):
    """Pousse une copie du .db vers le fichier fixe `BACKUP_DB_NAME` dans
    /Sauvegardes/ — dernier instantane uniquement (voir docstring du
    module : la vraie memoire longue duree est l'onglet Donnees, en ajout
    seul, du Google Sheet)."""
    if not config.GOOGLE_ENABLED:
        logger.info("gdrive: desactive — sauvegarde .db du %s ignoree", date)
        return None
    try:
        token = _access_token()
        return _update_fixed_file(
            conn, token, "backup_db", config.DB_PATH, BACKUP_DB_NAME,
            config.GOOGLE_DRIVE_BACKUPS_FOLDER_ID, "application/x-sqlite3",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("gdrive: echec de la sauvegarde .db du %s", date)
        raise RuntimeError(f"Sauvegarde Drive : {exc}") from exc
