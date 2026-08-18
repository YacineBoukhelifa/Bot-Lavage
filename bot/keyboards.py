"""Claviers Telegram (boutons) affiches avec les messages du bot.

Deux familles de boutons, pour deux raisons differentes de rester
utilisables en groupe (spec bot v2, Tache B) :

- Le clavier persistant (`MAIN_KEYBOARD`) envoie du texte libre quand on
  appuie dessus. En groupe, le *privacy mode* Telegram ne transmet au bot
  que ce qui COMMENCE par `/` — d'ou le motif `/commande emoji` (commande
  en premier, jamais l'inverse) sur les 4 boutons.
- Les claviers inline de la saisie guidee (B2/B3) s'appuient sur les
  `callback_query` (toujours transmis, quel que soit le privacy mode) et
  sur le fait qu'une REPONSE (`reply_to_message`) a un message du bot
  passe elle aussi le filtre — voir `app.py` pour la detection.
"""

BTN_SAISIR = "/saisir 📥"
BTN_RECAP = "/recap 📊"
BTN_GRAPH = "/graph 📈"
BTN_MENU = "/menu ⚙️"

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": BTN_SAISIR}, {"text": BTN_RECAP}],
        [{"text": BTN_GRAPH}, {"text": BTN_MENU}],
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

# Pas de clavier dedie pour les rapports : un bot ne peut pas ecrire dans le
# presse-papiers du client (cote serveur, aucun acces). Le rapport est rendu
# copiable nativement via un bloc <pre> HTML (appui long sur mobile, icone
# de copie sur desktop) — voir app.py `_copyable`.


# ---------------------------------------------------------------------------
# B2 — saisie guidee : carte de confirmation avant ecriture en base
# ---------------------------------------------------------------------------

GUIDE_CONFIRM_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "✅ Valider", "callback_data": "guide_valider"},
            {"text": "✏️ Corriger", "callback_data": "guide_corriger"},
        ]
    ]
}


# ---------------------------------------------------------------------------
# B3 — menu secondaire et ses sous-ecrans
# ---------------------------------------------------------------------------

MENU_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "▶️ Démarrer la journée", "callback_data": "menu_start_day"}],
        [{"text": "🌙 Ouvrir le shift 2", "callback_data": "menu_poste2"}],
        [{"text": "✏️ Corriger une saisie", "callback_data": "menu_corriger"}],
        [{"text": "🏁 Clôturer le shift", "callback_data": "menu_fin"}],
        [{"text": "📄 Lien du fichier", "callback_data": "menu_export"}],
        [{"text": "❓ Aide", "callback_data": "menu_aide"}],
    ]
}


def confirm_action_keyboard(action):
    """Carte Oui/Annuler generique pour une action destructrice (B4 :
    demarrage de journee, cloture de poste)."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Oui", "callback_data": f"confirm_action:{action}"},
                {"text": "❌ Annuler", "callback_data": "cancel_action"},
            ]
        ]
    }


def heures_keyboard(heures):
    """Liste des points de controle deja saisis, un bouton par heure —
    l'utilisateur choisit un point sans jamais taper d'heure au clavier."""
    rows = [[{"text": h, "callback_data": f"corr_heure|{h}"}] for h in heures]
    rows.append([{"text": "◀️ Retour", "callback_data": "menu_retour"}])
    return {"inline_keyboard": rows}


def lignes_keyboard(heure, lignes):
    """`lignes` : liste de (code, nom_affiche, valeur_actuelle)."""
    rows = [
        [{"text": f"{nom} : {valeur}", "callback_data": f"corr_ligne|{heure}|{code}"}]
        for code, nom, valeur in lignes
    ]
    rows.append([{"text": "◀️ Retour", "callback_data": "menu_corriger"}])
    return {"inline_keyboard": rows}


def pause_dejeuner_keyboard():
    """Question Oui/Non posee par ligne au point 12:00 du poste 1, pour
    savoir si c'est l'heure de la pause dejeuner de cette ligne."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Oui", "callback_data": "pause_dej|oui"},
                {"text": "❌ Non", "callback_data": "pause_dej|non"},
            ]
        ]
    }
