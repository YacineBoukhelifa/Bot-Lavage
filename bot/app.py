"""Application Flask — reception des webhooks Telegram et dispatch des commandes."""
import html
import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request

from . import config, db, excel_export, gdrive, graph, gsheets, keyboards, logic, telegram_client

app = Flask(__name__)
db.init_db()

logger = logging.getLogger(__name__)

ALGIERS_TZ = ZoneInfo(config.TIMEZONE)

HELP_TEXT = (
    "🤖 Bot rapport horaire — BU Lavage\n\n"
    "📥 Saisir — saisie guidée : le bot demande le point de contrôle, "
    "vous répondez juste par 3 chiffres\n"
    "📊 Récap — cumuls actuels du poste en cours\n"
    "📈 Graphique — graphique de la journée en cours\n"
    "⚙️ Menu — démarrer/clôturer le poste, ouvrir le poste 2, corriger "
    "une saisie, lien du fichier, aide\n\n"
    "Ordre des lignes en saisie guidée : Auto · Semi-auto · Ligne 03 "
    "(exemple : 800 660 90 — tapez « - » pour une ligne à l'arrêt).\n\n"
    "Les commandes texte restent disponibles pour un usage rapide : "
    "/start_day, /poste2, /prod a=800 s=660 l3=90, /a /s /l3, "
    "/corriger HH:MM code=valeur, /fin, /recap, /graph, /export, /id.\n\n"
    "En chat privé, le format libre (une ligne \"CODE valeur\" par ligne) "
    "reste accepté en plus des boutons et des commandes."
)

COMMAND_RE = re.compile(r"^/(\w+)(?:@\w+)?(?:\s+(.*))?$", re.DOTALL)
CORRIGER_RE = re.compile(r"^([A-Za-z0-9]+)=(-?\d+(?:[.,]\d+)?)$")
VALEUR_RE = re.compile(r"^-?\d+(?:[.,]\d+)?$")

# Un `reply_markup` d'edition (editMessageText) ne peut etre qu'un clavier
# inline — Telegram n'accepte pas ForceReply/ReplyKeyboardMarkup sur une
# edition, seulement sur un NOUVEAU message. Toute transition vers un
# ForceReply doit donc passer par send_message (jamais edit_message_text).
FORCE_REPLY = {"force_reply": True, "selective": True}


def _local_now_from_update(message):
    ts = message.get("date")
    if ts is None:
        return datetime.now(ALGIERS_TZ)
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ALGIERS_TZ)


# ---------------------------------------------------------------------------
# Permissions (spec v2 §1.4)
# ---------------------------------------------------------------------------

def _is_group(message):
    return message["chat"].get("type") in ("group", "supergroup")


def _sender(message):
    frm = message.get("from") or {}
    sender_id = str(frm["id"]) if frm.get("id") is not None else None
    sender_nom = frm.get("first_name") or frm.get("username") or "?"
    return sender_id, sender_nom


def _sender_from_callback(callback_query):
    return _sender(callback_query)


def _group_authorized(chat_id):
    return config.GROUPE_AUTORISE == 0 or int(chat_id) == config.GROUPE_AUTORISE


def _can_saisir(sender_id):
    allowed = config.AUTORISATIONS["saisie"]
    return not allowed or (sender_id is not None and int(sender_id) in allowed)


def _already_warned(conn, chat_id):
    row = conn.execute("SELECT 1 FROM groupes_avertis WHERE chat_id = ?", (str(chat_id),)).fetchone()
    return row is not None


def _mark_warned(conn, chat_id):
    conn.execute("INSERT OR IGNORE INTO groupes_avertis (chat_id) VALUES (?)", (str(chat_id),))
    conn.commit()


DENIED_SAISIE = [{"text": "🚫 Non autorisé à saisir dans ce groupe.", "status": "error"}]


# ---------------------------------------------------------------------------
# Cloture de poste : synthese + graphique + export, quel que soit le
# declencheur (point final complet, heure de fin depassee, ou /fin)
# ---------------------------------------------------------------------------

def _safe_graph(stats):
    try:
        return graph.generate_realisation_chart(stats)
    except Exception:  # noqa: BLE001 - le texte doit partir meme si le graphique echoue
        logger.exception("app: generation du graphique echouee")
        return None


def _push_to_google(conn, date, poste, local_path):
    """Pousse vers Sheets/Drive au mieux : chaque etape est independante,
    l'echec de l'une (ex. fichier Drive fixe pas encore cree, voir
    bot.gdrive) ne doit pas empecher les autres. Retourne la liste des
    messages d'erreur a afficher, le cas echeant."""
    if not config.GOOGLE_ENABLED:
        return []
    erreurs = []

    rows = conn.execute("SELECT * FROM checkpoints WHERE date=? AND poste=?", (date, poste)).fetchall()
    for r in rows:
        gsheets.append_donnees_row(r)

    year, month = int(date.split("-")[0]), int(date.split("-")[1])
    gsheets.overwrite_sheet(
        "Synthese_Quotidienne", excel_export.SYNTHESE_HEADERS,
        excel_export.synthese_quotidienne_rows(conn, year),
    )
    mensuelle = excel_export.vue_mensuelle(conn, year, month)
    if mensuelle:
        gsheets.overwrite_sheet("Vue_Mensuelle", mensuelle[0], mensuelle[1:])

    try:
        gdrive.export_monthly_xlsx(conn, local_path, f"{year}-{month:02d}")
    except Exception as exc:  # noqa: BLE001
        erreurs.append(str(exc))

    try:
        gdrive.backup_db(conn, date)
    except Exception as exc:  # noqa: BLE001
        erreurs.append(str(exc))

    return erreurs


def _close_and_build_messages(conn, date, poste, dt):
    stats = logic.close_poste(conn, date, poste, dt)
    messages = [{"text": logic.format_synthese(stats), "status": "report", "photo": _safe_graph(stats)}]

    try:
        path = excel_export.export_workbook(conn, date)
        for erreur in _push_to_google(conn, date, poste, path):
            messages.append({"text": f"⚠️ {erreur}", "status": "error"})
    except Exception as exc:  # noqa: BLE001 - ne doit jamais empecher l'envoi de la synthese texte
        logger.exception("app: export Excel local echoue")
        messages.append({"text": f"⚠️ Échec de l'export Excel : {exc}", "status": "error"})

    if poste == 2 and logic.get_poste(conn, date, 1) is not None:
        consolidated = logic.compute_consolidated_stats(conn, date)
        messages.append({
            "text": logic.format_synthese(consolidated),
            "status": "report",
            "photo": _safe_graph(consolidated),
        })

    return messages


# ---------------------------------------------------------------------------
# Traitement des check-ins (format libre prive + commandes)
# ---------------------------------------------------------------------------

def _finalize(result):
    return {"text": result["message"], "status": result["status"]}


def _process_and_dispatch(conn, chat_id, dt, valeurs, non_reconnues, malformees, sender_id, sender_nom):
    ctx = logic.resolve_production_context(conn, dt)
    if ctx.get("poste2_ferme"):
        return [{"text": "⛔ Poste 2 non ouvert — lancer /poste2 pour l'activer.", "status": "error"}]
    if ctx["date"] is None:
        return [{"text": "Aucun poste actif. Envoyez /start_day pour démarrer.", "status": "error"}]

    result = logic.process_checkin_values(
        conn, str(chat_id), ctx["date"], ctx["poste"], dt, valeurs, non_reconnues, malformees,
        sender_id, sender_nom,
    )
    messages = [_finalize(result)]
    if result.get("pret_a_cloturer"):
        messages.extend(_close_and_build_messages(conn, result["date"], result["poste"], dt))
    return messages


def _handle_free_checkin(conn, chat_id, dt, text, sender_id, sender_nom):
    lowered = text.strip().lower()
    pending = logic.get_pending_anomalies(conn, str(chat_id))
    if pending and lowered in ("oui", "confirmer", "yes", "confirm"):
        result = logic.confirm_pending_anomalies(conn, str(chat_id))
        messages = [_finalize(result)]
        if result.get("pret_a_cloturer"):
            messages.extend(_close_and_build_messages(conn, result["date"], result["poste"], dt))
        return messages
    if pending and lowered in ("non", "annuler", "no", "cancel"):
        result = logic.cancel_pending_anomalies(conn, str(chat_id))
        return [_finalize(result)]

    reconnues, non_reconnues, malformees = logic.parse_checkin(text)
    return _process_and_dispatch(conn, chat_id, dt, reconnues, non_reconnues, malformees, sender_id, sender_nom)


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------

def _current_context_readonly(conn, dt):
    state = logic.get_poste_state(conn)
    if state["poste2_ouvert"]:
        return state["poste2_date"], 2
    return dt.strftime("%Y-%m-%d"), 1


def _cmd_start_day(conn, is_group, dt, sender_id):
    if is_group and not _can_saisir(sender_id):
        return DENIED_SAISIE
    date = dt.strftime("%Y-%m-%d")
    result = logic.start_day(conn, date, dt)
    messages = []
    if result["poste2_cloture_auto"]:
        stats = result["poste2_cloture_auto"]
        text = (
            "⚠️ Le poste 2 de la veille n'avait pas été clôturé à temps — "
            "clôture automatique :\n\n" + logic.format_synthese(stats)
        )
        messages.append({"text": text, "status": "report", "photo": _safe_graph(stats)})
    messages.append({
        "text": f"✅ Poste 1 démarré pour le {dt.strftime('%d/%m/%Y')}. Cumuls remis à 0.",
        "status": "info",
    })
    return messages


def _cmd_poste2(conn, is_group, dt, sender_id):
    if is_group and not _can_saisir(sender_id):
        return DENIED_SAISIE
    date = dt.strftime("%Y-%m-%d")
    logic.open_poste2(conn, date, dt)
    return [{
        "text": f"✅ Poste 2 activé pour le {dt.strftime('%d/%m/%Y')} (16:30 → 00:30). Cumuls repartis à 0.",
        "status": "info",
    }]


def _cmd_fin(conn, is_group, dt, sender_id):
    if is_group and not _can_saisir(sender_id):
        return DENIED_SAISIE
    state = logic.get_poste_state(conn)
    if state["poste2_ouvert"]:
        return _close_and_build_messages(conn, state["poste2_date"], 2, dt)
    date = dt.strftime("%Y-%m-%d")
    poste1 = logic.get_poste(conn, date, 1)
    if poste1 is not None and poste1["statut"] == "actif":
        return _close_and_build_messages(conn, date, 1, dt)
    return [{"text": "Aucun poste actif à clôturer.", "status": "error"}]


def _cmd_single_line(conn, chat_id, is_group, dt, cmd, rest, sender_id, sender_nom):
    if is_group and not _can_saisir(sender_id):
        return DENIED_SAISIE
    code = config.CODES_ACCEPTES[cmd.upper()]
    valeur_raw = rest.strip()
    if not re.match(r"^-?\d+(?:[.,]\d+)?$", valeur_raw):
        return [{"text": f"Valeur invalide. Exemple : /{cmd} 800", "status": "error"}]
    valeur = float(valeur_raw.replace(",", "."))
    return _process_and_dispatch(conn, chat_id, dt, {code: valeur}, [], [], sender_id, sender_nom)


def _cmd_prod(conn, chat_id, is_group, dt, rest, sender_id, sender_nom):
    if is_group and not _can_saisir(sender_id):
        return DENIED_SAISIE
    reconnues, non_reconnues, malformees = logic.parse_prod(rest)
    return _process_and_dispatch(conn, chat_id, dt, reconnues, non_reconnues, malformees, sender_id, sender_nom)


def _cmd_corriger(conn, chat_id, is_group, dt, rest, sender_id, sender_nom):
    if is_group and not _can_saisir(sender_id):
        return DENIED_SAISIE
    usage = "Format attendu : /corriger HH:MM code=valeur (ex. /corriger 13:00 a=750)"
    parts = rest.strip().split(None, 1)
    if len(parts) != 2:
        return [{"text": usage, "status": "error"}]
    heure, assign = parts[0].strip(), parts[1].strip()
    m = CORRIGER_RE.match(assign)
    if not m:
        return [{"text": usage, "status": "error"}]
    code = config.CODES_ACCEPTES.get(m.group(1).upper())
    if not code:
        return [{"text": f"Code de ligne inconnu : {m.group(1)}", "status": "error"}]
    valeur = float(m.group(2).replace(",", "."))

    heures_poste1 = [p["heure"] for p in config.POSTE_1["points"]]
    heures_poste2 = [p["heure"] for p in config.POSTE_2["points"]]
    if heure in heures_poste1:
        poste, date = 1, dt.strftime("%Y-%m-%d")
    elif heure in heures_poste2:
        poste = 2
        state = logic.get_poste_state(conn)
        date = state["poste2_date"] if state["poste2_ouvert"] else dt.strftime("%Y-%m-%d")
    else:
        return [{"text": f"Heure de point de contrôle inconnue : {heure}", "status": "error"}]

    result = logic.corriger_checkpoint(conn, date, poste, code, heure, valeur, dt, sender_id, sender_nom)
    return [_finalize(result)]


def _cmd_recap(conn, dt):
    date, poste = _current_context_readonly(conn, dt)
    return [{"text": logic.format_recap(conn, date, poste), "status": "info"}]


def _cmd_graph(conn, dt):
    date, poste = _current_context_readonly(conn, dt)
    stats = logic.compute_poste_stats(conn, date, poste)
    if not stats["lignes"]:
        return [{"text": "Aucune donnée pour l'instant.", "status": "info"}]
    photo = _safe_graph(stats)
    if photo is None:
        return [{"text": "Erreur lors de la génération du graphique.", "status": "error"}]
    return [{
        "text": f"📈 Graphique — {stats['libelle']} — {date}",
        "status": "report",
        "photo": photo,
    }]


def _cmd_export(conn, dt):
    date, _poste = _current_context_readonly(conn, dt)
    try:
        path = excel_export.export_workbook(conn, date)
    except Exception:  # noqa: BLE001
        logger.exception("app: export excel /export echoue")
        return [{"text": "Erreur lors de la génération de l'export Excel.", "status": "error"}]

    messages = [{"text": "📤 Export Excel généré :", "status": "info", "document": path}]
    if config.GOOGLE_ENABLED and config.GOOGLE_SPREADSHEET_ID:
        url = f"https://docs.google.com/spreadsheets/d/{config.GOOGLE_SPREADSHEET_ID}"
        messages.insert(0, {"text": f"📤 Google Sheet en direct : {url}", "status": "info"})
    else:
        messages.insert(0, {
            "text": "ℹ️ Google Drive n'est pas encore configuré (voir DEPLOY.md) — export local uniquement.",
            "status": "info",
        })
    return messages


def _cmd_id(chat_id, is_group, sender_id, sender_nom):
    lines = [f"🆔 chat_id = {chat_id}", f"🆔 user_id = {sender_id} ({sender_nom})"]
    if is_group:
        lines.append(
            "Astuce config : GROUPE_AUTORISE=" + str(chat_id) +
            " · ajoutez " + str(sender_id) + " à AUTORISATIONS_SAISIE si besoin."
        )
    return [{"text": "\n".join(lines), "status": "info"}]


# ---------------------------------------------------------------------------
# B2 — saisie guidee : ForceReply + carte de confirmation editable
# ---------------------------------------------------------------------------

def _safe_edit_or_send(chat_id, message_id, text, reply_markup=None, parse_mode=None):
    """Tente d'editer `message_id` ; si Telegram refuse l'edition (message
    trop vieux, deja supprime, etc.), retombe sur un nouvel envoi plutot que
    de laisser l'exception remonter — sinon la transition d'etat qui suit
    (set_interaction_state) n'a jamais lieu et l'utilisateur reste bloque
    avec un etat perime (voir Tache B, bug du 15/08/2026)."""
    try:
        telegram_client.edit_message_text(chat_id, message_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        return message_id
    except Exception:  # noqa: BLE001
        logger.exception("app: edit_message_text a echoue, repli sur un nouvel envoi")
        resp = telegram_client.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        return resp.get("result", {}).get("message_id")


def _saisie_prompt_text(heure):
    noms = " · ".join(config.LIGNES[c]["nom_affiche"] for c in config.ORDRE_AFFICHAGE)
    return (
        f"📥 Point de contrôle {heure}\n"
        f"Envoyez les cumuls dans l'ordre :\n{noms}\n"
        f"Exemple : 800 660 90\n"
        f"Tapez « - » pour une ligne à l'arrêt."
    )


def _cmd_saisir(conn, chat_id, is_group, dt, sender_id, sender_nom):
    if sender_id is None:
        return [{"text": "Impossible d'identifier l'expéditeur.", "status": "error"}]
    if is_group and not _can_saisir(sender_id):
        return DENIED_SAISIE

    ctx = logic.resolve_production_context(conn, dt)
    if ctx.get("poste2_ferme"):
        return [{"text": "⛔ Poste 2 non ouvert — ouvrez-le depuis ⚙️ Menu.", "status": "error"}]
    if ctx["date"] is None:
        return [{"text": "Aucun poste actif. Démarrez la journée depuis ⚙️ Menu.", "status": "error"}]

    snap = logic.snap_to_checkpoint(dt, ctx["poste"])
    if snap["matched"] is None:
        avant, apres = snap["avant"] or "aucun", snap["apres"] or "aucun"
        return [{
            "text": f"Aucun point de contrôle proche de cet horaire — dernier : {avant}, prochain : {apres}.",
            "status": "error",
        }]
    heure = snap["matched"]

    resp = telegram_client.send_message(chat_id, _saisie_prompt_text(heure), reply_markup=FORCE_REPLY)
    message_id = resp.get("result", {}).get("message_id")
    logic.set_interaction_state(
        conn, chat_id, sender_id, "ATTENTE_CUMULS",
        {"poste": ctx["poste"], "date": ctx["date"], "heure": heure}, dt, message_id,
    )
    return []


def _guide_confirmation_card(apercu, heure):
    lignes_txt = "\n".join(
        f"{l['nom']:<15} : {logic.fmt_qty(l['cumul']):>6}  "
        f"({logic.fmt_signed_qty(l['production'])} cette heure, {logic.fmt_pct(l['pct'])}%)"
        for l in apercu
    )
    return f"📊 Point {heure} — à valider\n{lignes_txt}"


def _guide_process_cumuls(conn, chat_id, dt, text, sender_id, sender_nom, state):
    ctx = state["contexte"]
    message_id = state["message_id"]
    tokens = text.strip().split()

    if len(tokens) != len(config.ORDRE_AFFICHAGE):
        return [{
            "text": (
                f"Format invalide. Envoyez {len(config.ORDRE_AFFICHAGE)} valeurs séparées par un "
                f"espace (ex. 800 660 90), ou « - » pour une ligne à l'arrêt."
            ),
            "status": "error",
        }]

    valeurs = {}
    for code, token in zip(config.ORDRE_AFFICHAGE, tokens):
        if token == "-":
            continue
        if not VALEUR_RE.match(token):
            return [{"text": f"Valeur invalide : « {token} ». Utilisez des nombres ou « - ».", "status": "error"}]
        valeurs[code] = float(token.replace(",", "."))

    if not valeurs:
        return [{"text": "Aucune ligne active saisie.", "status": "error"}]

    apercu = logic.preview_checkin(conn, ctx["date"], ctx["poste"], ctx["heure"], valeurs)
    card = _guide_confirmation_card(apercu, ctx["heure"])

    if message_id is not None:
        message_id = _safe_edit_or_send(chat_id, message_id, card, reply_markup=keyboards.GUIDE_CONFIRM_KEYBOARD)
    else:
        resp = telegram_client.send_message(chat_id, card, reply_markup=keyboards.GUIDE_CONFIRM_KEYBOARD)
        message_id = resp.get("result", {}).get("message_id")

    logic.set_interaction_state(
        conn, chat_id, sender_id, "ATTENTE_CONFIRMATION", {**ctx, "valeurs": valeurs}, dt, message_id,
    )
    return []


def _cb_guide_valider(conn, callback_query, dt):
    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    is_group = _is_group(message)
    sender_id, sender_nom = _sender_from_callback(callback_query)
    if is_group and not _can_saisir(sender_id):
        return [{"text": "🚫 Non autorisé à saisir.", "status": "error"}]

    state = logic.get_interaction_state(conn, chat_id, sender_id, dt)
    if (
        state is None or state["etat"] != "ATTENTE_CONFIRMATION"
        or state["message_id"] != message.get("message_id")
    ):
        # Le message_id de la carte cliquee doit correspondre a celui
        # enregistre dans l'etat — sinon un bouton perime (ou une autre
        # saisie en cours pour le meme utilisateur) serait valide par
        # erreur au lieu d'etre rejete comme expire.
        return [{"text": "⏱️ Session expirée ou déjà traitée. Relancez avec 📥 Saisir.", "status": "error"}]

    ctx = state["contexte"]
    message_id = state["message_id"]
    logic.clear_interaction_state(conn, chat_id, sender_id)

    result = logic.process_checkin_values(
        conn, str(chat_id), ctx["date"], ctx["poste"], dt, ctx["valeurs"], [], [], sender_id, sender_nom,
        heure=ctx["heure"],
    )

    if result["status"] == "anomaly":
        if message_id is not None:
            _safe_edit_or_send(chat_id, message_id, result["message"], reply_markup=keyboards.ANOMALY_KEYBOARD)
            return []
        return [{"text": result["message"], "status": "anomaly"}]

    # La carte est reduite a une courte confirmation (boutons retires), et le
    # rapport complet part comme un NOUVEAU message via le pipeline standard
    # (_send_one) — c'est ce qui reattache le clavier persistant en bas de
    # l'ecran : ForceReply l'a masque au debut du flux, et seul un nouveau
    # sendMessage portant `reply_markup` peut le faire reapparaitre (editer
    # un message ne touche que son clavier inline, jamais le clavier
    # persistant du chat).
    if message_id is not None:
        _safe_edit_or_send(chat_id, message_id, "✅ Enregistré.", reply_markup=None)
    messages = [_finalize(result)]

    if result.get("pret_a_cloturer"):
        messages.extend(_close_and_build_messages(conn, result["date"], result["poste"], dt))
    return messages


def _cb_guide_corriger(conn, callback_query, dt):
    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    is_group = _is_group(message)
    sender_id, sender_nom = _sender_from_callback(callback_query)
    if is_group and not _can_saisir(sender_id):
        return [{"text": "🚫 Non autorisé à saisir.", "status": "error"}]

    state = logic.get_interaction_state(conn, chat_id, sender_id, dt)
    if (
        state is None or state["etat"] != "ATTENTE_CONFIRMATION"
        or state["message_id"] != message.get("message_id")
    ):
        return [{"text": "⏱️ Session expirée. Relancez avec 📥 Saisir.", "status": "error"}]
    ctx = state["contexte"]

    if state["message_id"] is not None:
        telegram_client.edit_message_text(chat_id, state["message_id"], "✏️ Nouvelle saisie en cours…", reply_markup=None)

    resp = telegram_client.send_message(chat_id, _saisie_prompt_text(ctx["heure"]), reply_markup=FORCE_REPLY)
    new_message_id = resp.get("result", {}).get("message_id")
    logic.set_interaction_state(
        conn, chat_id, sender_id, "ATTENTE_CUMULS",
        {"poste": ctx["poste"], "date": ctx["date"], "heure": ctx["heure"]}, dt, new_message_id,
    )
    return []


# ---------------------------------------------------------------------------
# B3 — menu secondaire (actions ponctuelles, tout par boutons)
# ---------------------------------------------------------------------------

def _cmd_menu(chat_id):
    telegram_client.send_message(chat_id, "⚙️ Menu", reply_markup=keyboards.MENU_KEYBOARD)
    return []


def _cb_menu_start_day(callback_query):
    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    sender_id, _ = _sender_from_callback(callback_query)
    if _is_group(message) and not _can_saisir(sender_id):
        return [{"text": "🚫 Non autorisé à saisir.", "status": "error"}]
    telegram_client.edit_message_text(
        chat_id, message["message_id"],
        "⚠️ Démarrer une nouvelle journée ? Les cumuls du poste 1 seront remis à zéro.",
        reply_markup=keyboards.confirm_action_keyboard("start_day"),
    )
    return []


def _cb_menu_fin(callback_query):
    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    sender_id, _ = _sender_from_callback(callback_query)
    if _is_group(message) and not _can_saisir(sender_id):
        return [{"text": "🚫 Non autorisé à saisir.", "status": "error"}]
    telegram_client.edit_message_text(
        chat_id, message["message_id"], "⚠️ Clôturer le poste en cours maintenant ?",
        reply_markup=keyboards.confirm_action_keyboard("fin"),
    )
    return []


def _cb_menu_poste2(conn, callback_query, dt):
    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    is_group = _is_group(message)
    sender_id, _ = _sender_from_callback(callback_query)
    if is_group and not _can_saisir(sender_id):
        return [{"text": "🚫 Non autorisé à saisir.", "status": "error"}]
    result_messages = _cmd_poste2(conn, is_group, dt, sender_id)
    telegram_client.edit_message_text(chat_id, message["message_id"], "✅ Terminé.", reply_markup=None)
    return result_messages


def _cb_confirm_action(conn, callback_query, dt, action):
    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    is_group = _is_group(message)
    sender_id, _ = _sender_from_callback(callback_query)
    if is_group and not _can_saisir(sender_id):
        return [{"text": "🚫 Non autorisé à saisir.", "status": "error"}]

    if action == "start_day":
        result_messages = _cmd_start_day(conn, is_group, dt, sender_id)
    elif action == "fin":
        result_messages = _cmd_fin(conn, is_group, dt, sender_id)
    else:
        result_messages = [{"text": "Action inconnue.", "status": "error"}]

    telegram_client.edit_message_text(chat_id, message["message_id"], "✅ Terminé.", reply_markup=None)
    return result_messages


def _cb_cancel_action(callback_query):
    message = callback_query["message"]
    telegram_client.edit_message_text(message["chat"]["id"], message["message_id"], "❌ Annulé.", reply_markup=None)
    return []


def _cb_menu_export(conn, callback_query, dt):
    message = callback_query["message"]
    telegram_client.edit_message_text(message["chat"]["id"], message["message_id"], "📄 Export en cours…", reply_markup=None)
    return _cmd_export(conn, dt)


def _cb_menu_aide(callback_query):
    message = callback_query["message"]
    telegram_client.edit_message_text(
        message["chat"]["id"], message["message_id"], HELP_TEXT,
        reply_markup={"inline_keyboard": [[{"text": "◀️ Retour", "callback_data": "menu_retour"}]]},
    )
    return []


def _cb_menu_retour(callback_query):
    message = callback_query["message"]
    telegram_client.edit_message_text(
        message["chat"]["id"], message["message_id"], "⚙️ Menu", reply_markup=keyboards.MENU_KEYBOARD,
    )
    return []


def _cb_menu_corriger(conn, callback_query, dt):
    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    sender_id, _ = _sender_from_callback(callback_query)
    if _is_group(message) and not _can_saisir(sender_id):
        return [{"text": "🚫 Non autorisé à saisir.", "status": "error"}]

    date, poste = _current_context_readonly(conn, dt)
    ordre = [p["heure"] for p in config.POSTES[poste]["points"]]
    heures_set = {
        r["heure"] for r in conn.execute(
            "SELECT DISTINCT heure FROM checkpoints WHERE date=? AND poste=?", (date, poste),
        ).fetchall()
    }
    heures = sorted(heures_set, key=ordre.index)

    if not heures:
        telegram_client.edit_message_text(
            chat_id, message["message_id"], "Aucune saisie à corriger pour l'instant.",
            reply_markup={"inline_keyboard": [[{"text": "◀️ Retour", "callback_data": "menu_retour"}]]},
        )
        return []
    telegram_client.edit_message_text(
        chat_id, message["message_id"], "✏️ Choisissez le point à corriger :",
        reply_markup=keyboards.heures_keyboard(heures),
    )
    return []


def _cb_corr_heure(conn, callback_query, dt, heure):
    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    sender_id, _ = _sender_from_callback(callback_query)
    if _is_group(message) and not _can_saisir(sender_id):
        return [{"text": "🚫 Non autorisé à saisir.", "status": "error"}]

    date, poste = _current_context_readonly(conn, dt)
    lignes = []
    for code in config.ORDRE_AFFICHAGE:
        row = conn.execute(
            "SELECT cumul_envoye FROM checkpoints WHERE date=? AND poste=? AND code=? AND heure=?",
            (date, poste, code, heure),
        ).fetchone()
        if row is not None:
            lignes.append((code, config.LIGNES[code]["nom_affiche"], logic.fmt_qty(row["cumul_envoye"])))

    if not lignes:
        telegram_client.edit_message_text(
            chat_id, message["message_id"], "Aucune ligne saisie à ce point.",
            reply_markup={"inline_keyboard": [[{"text": "◀️ Retour", "callback_data": "menu_corriger"}]]},
        )
        return []
    telegram_client.edit_message_text(
        chat_id, message["message_id"], f"✏️ Point {heure} — choisissez la ligne à corriger :",
        reply_markup=keyboards.lignes_keyboard(heure, lignes),
    )
    return []


def _cb_corr_ligne(conn, callback_query, dt, heure, code):
    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    sender_id, sender_nom = _sender_from_callback(callback_query)
    if _is_group(message) and not _can_saisir(sender_id):
        return [{"text": "🚫 Non autorisé à saisir.", "status": "error"}]

    date, poste = _current_context_readonly(conn, dt)
    nom = config.LIGNES[code]["nom_affiche"]

    telegram_client.edit_message_text(chat_id, message["message_id"], "✏️ En attente de la nouvelle valeur…", reply_markup=None)
    resp = telegram_client.send_message(chat_id, f"✏️ Nouvelle valeur pour {nom} à {heure} :", reply_markup=FORCE_REPLY)
    new_message_id = resp.get("result", {}).get("message_id")
    logic.set_interaction_state(
        conn, chat_id, sender_id, "ATTENTE_CORRECTION_VALEUR",
        {"date": date, "poste": poste, "heure": heure, "code": code}, dt, new_message_id,
    )
    return []


def _menu_process_correction_valeur(conn, chat_id, dt, text, sender_id, sender_nom, state):
    ctx = state["contexte"]
    message_id = state["message_id"]
    valeur_raw = text.strip()
    if not VALEUR_RE.match(valeur_raw):
        return [{"text": "Valeur invalide. Envoyez un nombre.", "status": "error"}]
    valeur = float(valeur_raw.replace(",", "."))

    result = logic.corriger_checkpoint(
        conn, ctx["date"], ctx["poste"], ctx["code"], ctx["heure"], valeur, dt, sender_id, sender_nom,
    )
    logic.clear_interaction_state(conn, chat_id, sender_id)

    if message_id is not None:
        # Meme raison qu'en B2 : nouveau message plutot qu'edition finale,
        # pour reattacher le clavier persistant masque par le ForceReply.
        _safe_edit_or_send(chat_id, message_id, "✅ Corrigé.", reply_markup=None)
    return [_finalize(result)]


def _dispatch_command(conn, chat_id, is_group, cmd, rest, dt, sender_id, sender_nom):
    if cmd == "saisir":
        return _cmd_saisir(conn, chat_id, is_group, dt, sender_id, sender_nom)
    if cmd == "menu":
        return _cmd_menu(chat_id)
    if cmd == "start_day":
        return _cmd_start_day(conn, is_group, dt, sender_id)
    if cmd == "poste2":
        return _cmd_poste2(conn, is_group, dt, sender_id)
    if cmd == "prod":
        return _cmd_prod(conn, chat_id, is_group, dt, rest, sender_id, sender_nom)
    if cmd.upper() in config.CODES_ACCEPTES:
        return _cmd_single_line(conn, chat_id, is_group, dt, cmd, rest, sender_id, sender_nom)
    if cmd == "corriger":
        return _cmd_corriger(conn, chat_id, is_group, dt, rest, sender_id, sender_nom)
    if cmd == "fin":
        return _cmd_fin(conn, is_group, dt, sender_id)
    if cmd == "recap":
        return _cmd_recap(conn, dt)
    if cmd == "graph":
        return _cmd_graph(conn, dt)
    if cmd == "export":
        return _cmd_export(conn, dt)
    if cmd == "id":
        return _cmd_id(chat_id, is_group, sender_id, sender_nom)
    if cmd in ("help", "start"):
        return [{"text": HELP_TEXT, "status": "info"}]
    if is_group:
        return []  # commande inconnue en groupe -> silence (evite le bruit)
    return [{"text": "Commande inconnue. Envoyez /help.", "status": "error"}]


def handle_message(message, dt):
    chat_id = message["chat"]["id"]
    text = message.get("text", "")
    is_group = _is_group(message)

    conn = db.get_connection()
    try:
        if is_group and not _group_authorized(chat_id):
            if _already_warned(conn, chat_id):
                return []
            _mark_warned(conn, chat_id)
            return [{"text": "🚫 Ce bot n'est pas autorisé dans ce groupe.", "status": "error"}]

        sender_id, sender_nom = _sender(message)
        stripped = text.strip()

        # Une commande explicite reprend TOUJOURS la main, meme si un flux
        # guide attend une reponse — sinon une erreur transitoire au milieu
        # de la saisie guidee (ex. edit_message_text qui echoue) laisse
        # l'utilisateur bloque : /saisir, /menu, /recap... seraient avales
        # comme si c'etait sa reponse au lieu de relancer proprement.
        m = COMMAND_RE.match(stripped)
        if m is not None:
            cmd = m.group(1).lower()
            rest = (m.group(2) or "").strip()
            return _dispatch_command(conn, chat_id, is_group, cmd, rest, dt, sender_id, sender_nom)

        # Une reponse (reply_to_message) a un message du bot franchit le
        # privacy mode meme sans "/" (spec bot v2, Tache B) — priorite sur
        # le texte libre tant qu'un flux guide attend une reponse pour CET
        # utilisateur precis (isolation B4.6).
        if sender_id is not None:
            state = logic.get_interaction_state(conn, chat_id, sender_id, dt)
            if state and state["etat"] == "ATTENTE_CUMULS":
                return _guide_process_cumuls(conn, chat_id, dt, stripped, sender_id, sender_nom, state)
            if state and state["etat"] == "ATTENTE_CORRECTION_VALEUR":
                return _menu_process_correction_valeur(conn, chat_id, dt, stripped, sender_id, sender_nom, state)

        if is_group:
            return []  # texte libre invisible du bot en groupe (privacy mode) ; on ignore par coherence
        return _handle_free_checkin(conn, chat_id, dt, stripped, sender_id, sender_nom)
    finally:
        conn.close()


def handle_callback(callback_query, dt):
    chat_id = callback_query["message"]["chat"]["id"]
    data = callback_query.get("data", "")
    conn = db.get_connection()
    try:
        if data == "confirm_anomaly":
            result = logic.confirm_pending_anomalies(conn, str(chat_id))
            messages = [_finalize(result)]
            if result.get("pret_a_cloturer"):
                messages.extend(_close_and_build_messages(conn, result["date"], result["poste"], dt))
            return messages
        if data == "cancel_anomaly":
            result = logic.cancel_pending_anomalies(conn, str(chat_id))
            return [_finalize(result)]
        if data == "guide_valider":
            return _cb_guide_valider(conn, callback_query, dt)
        if data == "guide_corriger":
            return _cb_guide_corriger(conn, callback_query, dt)
        if data == "menu_start_day":
            return _cb_menu_start_day(callback_query)
        if data == "menu_fin":
            return _cb_menu_fin(callback_query)
        if data == "menu_poste2":
            return _cb_menu_poste2(conn, callback_query, dt)
        if data == "menu_export":
            return _cb_menu_export(conn, callback_query, dt)
        if data == "menu_aide":
            return _cb_menu_aide(callback_query)
        if data == "menu_retour":
            return _cb_menu_retour(callback_query)
        if data == "menu_corriger":
            return _cb_menu_corriger(conn, callback_query, dt)
        if data == "cancel_action":
            return _cb_cancel_action(callback_query)
        if data.startswith("confirm_action:"):
            return _cb_confirm_action(conn, callback_query, dt, data.split(":", 1)[1])
        if data.startswith("corr_heure|"):
            return _cb_corr_heure(conn, callback_query, dt, data.split("|", 1)[1])
        if data.startswith("corr_ligne|"):
            _, heure, code = data.split("|", 2)
            return _cb_corr_ligne(conn, callback_query, dt, heure, code)
        return [{"text": "Action inconnue.", "status": "error"}]
    finally:
        conn.close()


def _reply_markup_for(status):
    if status == "anomaly":
        return keyboards.ANOMALY_KEYBOARD
    return keyboards.MAIN_KEYBOARD


def _copyable(text):
    """Rend `text` copiable nativement (appui long sur mobile, icone de
    copie sur desktop) via un bloc <pre> HTML — un bot ne peut pas ecrire
    dans le presse-papiers du client (cote serveur, aucun acces), donc pas
    de bouton "Copier" qui redeclencherait un envoi (spec Tache A)."""
    return f"<pre>{html.escape(text, quote=False)}</pre>"


def _send_one(chat_id, msg):
    text = msg.get("text", "")
    status = msg.get("status", "info")
    markup = _reply_markup_for(status)
    copiable = status == "report" and bool(text)
    body = _copyable(text) if copiable else text
    parse_mode = "HTML" if copiable else None

    if msg.get("document"):
        telegram_client.send_document(chat_id, msg["document"], caption=(text[:1000] or None))
        if len(text) > 1000:
            telegram_client.send_message(chat_id, text, reply_markup=markup)
        return

    if msg.get("photo"):
        caption = body if body and len(body) <= 1000 else None
        telegram_client.send_photo(
            chat_id, msg["photo"], caption=caption, reply_markup=markup,
            parse_mode=(parse_mode if caption else None),
        )
        if caption is None and text:
            telegram_client.send_message(chat_id, body, reply_markup=markup, parse_mode=parse_mode)
        return

    telegram_client.send_message(chat_id, body, reply_markup=markup, parse_mode=parse_mode)


def _run_opportunistic_closures(now, fallback_chat_id):
    """A chaque webhook, verifie si un poste actif a depasse son heure de
    fin sans avoir ete cloture (spec v2 §5.1) — voir DEPLOY.md pour le
    endpoint /cron optionnel destine a un declenchement plus ponctuel."""
    conn = db.get_connection()
    try:
        target = config.GROUPE_AUTORISE or fallback_chat_id
        for date, poste in logic.postes_a_cloturer_automatiquement(conn, now):
            for msg in _close_and_build_messages(conn, date, poste, now):
                _send_one(target, msg)
    except Exception:  # noqa: BLE001 - ne doit jamais faire echouer la reponse au webhook
        logger.exception("app: verification opportuniste de cloture echouee")
    finally:
        conn.close()


@app.route(f"/webhook/{config.WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}

    callback_query = update.get("callback_query")
    if callback_query:
        chat_id = callback_query["message"]["chat"]["id"]
        # callback_query n'a pas d'horodatage propre (celui de .message est
        # celui du message d'origine, perime) -> horloge reelle ici.
        now = datetime.now(ALGIERS_TZ)
        try:
            messages = handle_callback(callback_query, now)
        except Exception as exc:  # noqa: BLE001 - le bot doit toujours repondre
            messages = [{"text": f"❌ Erreur interne : {exc}", "status": "error"}]
        telegram_client.answer_callback_query(callback_query["id"])
        for msg in messages:
            _send_one(chat_id, msg)
        _run_opportunistic_closures(now, chat_id)
        return jsonify({"ok": True})

    message = update.get("message") or update.get("edited_message")
    if not message or "text" not in message:
        return jsonify({"ok": True})

    chat_id = message["chat"]["id"]
    dt = _local_now_from_update(message)

    try:
        messages = handle_message(message, dt)
    except Exception as exc:  # noqa: BLE001 - le bot doit toujours repondre
        messages = [{"text": f"❌ Erreur interne lors du traitement du message : {exc}", "status": "error"}]

    for msg in messages or []:
        _send_one(chat_id, msg)

    # Cle sur l'horodatage du message plutot que sur l'horloge reelle : en
    # webhook les deux coincident (Telegram livre en quasi temps reel), et
    # ca garde le comportement deterministe/testable avec des dates simulees.
    _run_opportunistic_closures(dt, chat_id)
    return jsonify({"ok": True})


@app.route(f"/cron/{config.WEBHOOK_SECRET}/tick", methods=["GET", "POST"])
def cron_tick():
    """Endpoint optionnel a brancher sur un scheduled task PythonAnywhere
    pour garantir un declenchement pile a l'heure (voir DEPLOY.md) — le
    bot fonctionne deja sans, via la verification opportuniste a chaque
    webhook."""
    now = datetime.now(ALGIERS_TZ)
    _run_opportunistic_closures(now, config.GROUPE_AUTORISE)
    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True)
