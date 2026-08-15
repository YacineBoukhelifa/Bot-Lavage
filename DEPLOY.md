# Déploiement — PythonAnywhere (gratuit, webhook)

Ces étapes doivent être faites par vous dans votre propre navigateur (création de
compte et connexion ne peuvent pas être faites à votre place). Une fois le compte
créé, chaque étape technique est une commande à copier-coller.

## ⚠️ 0. Sécurité — token à régénérer

Un ancien script de développement (`scripts/relay.py`) contenait un vrai
`BOT_TOKEN` codé en dur. Il a été retiré du code (le script lit maintenant
`.env` comme le reste du projet), mais **si ce token a été exposé
(dépôt public, capture d'écran, etc.), régénérez-le par précaution** :
Telegram → @BotFather → `/mybots` → votre bot → **API Token** → **Revoke
current token**. Mettez ensuite le nouveau token dans `.env`.

## 1. Créer le bot Telegram (si pas déjà fait)
1. Telegram → chercher **@BotFather** → `/newbot`.
2. Notez le **token** renvoyé (`123456789:AAF...`).

## 2. Créer le compte PythonAnywhere
1. Aller sur https://www.pythonanywhere.com/registration/register/beginner/
2. Créer un compte **Beginner (gratuit)** — aucune carte bancaire requise.

## 3. Uploader le code
Dans le dashboard PythonAnywhere → onglet **Consoles** → ouvrir une **Bash console**, puis :

```bash
git clone <votre-repo>   # recommande pour la v2 (nombreux fichiers)
cd "Telegrm bot"
pip install --user -r requirements.txt
```

`scripts/pythonanywhere_bootstrap.sh` (upload sans git, fichier par fichier
via heredoc) date de la v1 et n'a pas été maintenu pour la v2 — trop de
fichiers désormais. Utilisez `git clone`, ou l'onglet **Files** du dashboard
pour uploader le dossier complet.

## 4. Configurer les variables d'environnement
Toujours dans la Bash console :

```bash
cd ~/telegrm-bot   # adapter le chemin
cp .env.example .env
nano .env
```

Renseignez au minimum :
```
BOT_TOKEN=<le token recu de BotFather>
WEBHOOK_SECRET=<une chaine aleatoire, ex: openssl rand -hex 16>
DB_PATH=/home/<votre-user>/telegrm-bot/data/bot.sqlite3
```

Le reste (`GROUPE_AUTORISE`, `AUTORISATIONS_SAISIE`, `GOOGLE_*`) peut rester
vide pour démarrer — voir §7 et §8 ci-dessous pour les remplir une fois le
bot en ligne.

## 5. Créer la Web App
1. Dashboard → onglet **Web** → **Add a new web app**.
2. Choisir **Flask**, puis la version Python (3.10+).
3. Une fois créée, ouvrir **WSGI configuration file** (lien dans l'onglet Web) et
   remplacer son contenu par :

```python
import sys
path = '/home/<votre-user>/telegrm-bot'
if path not in sys.path:
    sys.path.insert(0, path)

from dotenv import load_dotenv
load_dotenv(f"{path}/.env")

from bot.app import app as application
```

4. Dans l'onglet Web, section **Virtualenv** : indiquer le chemin vers vos
   packages installés, ou créer un virtualenv dédié :
   ```bash
   mkvirtualenv --python=/usr/bin/python3.10 telegrm-bot-env
   pip install -r requirements.txt
   ```
   puis renseigner `/home/<votre-user>/.virtualenvs/telegrm-bot-env` dans le champ Virtualenv.

   ⚠️ `matplotlib`/`numpy` sont plus lourds que le reste des dépendances —
   l'installation peut prendre quelques minutes sur le compte gratuit.

5. Cliquer **Reload** (bouton vert en haut de l'onglet Web).

## 6. Vérifier que l'app répond
Ouvrir `https://<votre-user>.pythonanywhere.com/health` → doit répondre `{"status": "ok"}`.

## 7. Enregistrer le webhook auprès de Telegram
Depuis la Bash console PythonAnywhere (ou votre machine) :

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<votre-user>.pythonanywhere.com/webhook/<WEBHOOK_SECRET>"
```

Réponse attendue : `{"ok":true,"result":true,"description":"Webhook was set"}`.

## 8. Test réel en privé
Dans Telegram, l'interaction principale se fait par **boutons** (clavier
persistant `📥 Saisir · 📊 Récap · 📈 Graphique · ⚙️ Menu`) — les commandes
texte (`/start_day`, `/prod`...) restent disponibles en usage rapide mais
ne sont plus nécessaires au quotidien. Testez d'abord le clavier :
```
/start_day
```
puis tapez `📥 Saisir`, répondez au message du bot par 3 nombres (ex.
`800 660 90`), puis appuyez sur `✅ Valider`. Le bot doit répondre avec un
rapport horaire copiable (appui long pour copier).

### 8.1 Limiter la liste de commandes affichée par Telegram (optionnel)
Une fois `.env` finalisé, exécutez **une seule fois** (même motif que
`setWebhook`) pour que l'autocomplétion Telegram (`/`) ne propose plus que
`/start`, `/menu`, `/help` — toutes les autres commandes restent
fonctionnelles, juste masquées de la liste :
```bash
curl -X POST "https://api.telegram.org/bot${BOT_TOKEN}/setMyCommands" \
  -H "Content-Type: application/json" \
  -d '{"commands":[{"command":"start","description":"Démarrer"},{"command":"menu","description":"Menu principal"},{"command":"help","description":"Aide"}]}'
```

## 9. Passer en groupe — récupérer les IDs et verrouiller l'accès

1. Créez le groupe Telegram, ajoutez-y le bot.
2. **Important** : dans les paramètres du bot via @BotFather → `/mybots` →
   votre bot → **Bot Settings** → **Group Privacy** → vérifiez qu'il est
   bien **ENABLED** (mode confidentialité activé — c'est le comportement
   attendu par la spec v2 §1, ne pas le désactiver).
3. Dans le groupe, tapez `/id`. Le bot répond avec le `chat_id` du groupe et
   votre `user_id`.
4. Dans `.env` sur PythonAnywhere :
   ```
   GROUPE_AUTORISE=-1001234567890
   AUTORISATIONS_SAISIE=111111111,222222222
   ```
   (un ID par personne autorisée à saisir des chiffres, séparés par virgule ;
   laisser vide = pas de restriction de saisie)
5. Reload la web app (onglet Web → bouton **Reload**).

Tant que `GROUPE_AUTORISE` est vide, le bot répond dans **n'importe quel**
groupe où il est ajouté — pratique pour tester, mais à verrouiller avant mise
en production réelle (spec v2 §1.4).

## 10. Google Sheets / Drive (optionnel — spec v2 §7)

Le bot fonctionne pleinement sans ceci (Excel local + graphiques marchent
déjà). Cette section n'est nécessaire que pour la sauvegarde automatique en
ligne.

### 10.1 Créer le compte de service
1. https://console.cloud.google.com/ → créer un projet (ou en réutiliser un).
2. **APIs & Services → Library** → activer **Google Sheets API** et
   **Google Drive API**.
3. **APIs & Services → Credentials** → **Create Credentials** → **Service
   Account**. Donnez-lui un nom (ex. `bot-lavage-sheets`), pas besoin de
   rôle particulier au niveau projet.
4. Ouvrez le compte de service créé → onglet **Keys** → **Add Key** → **JSON**
   → le fichier se télécharge. **Gardez-le secret**, ne le commitez jamais.
5. Notez l'adresse email du compte de service (ressemble à
   `bot-lavage-sheets@mon-projet.iam.gserviceaccount.com`).

### 10.2 Préparer le Google Sheet et les dossiers Drive
1. Créez un Google Sheet vide nommé par exemple `Production BU Lavage`.
   Créez-y les 4 onglets : `Donnees`, `Synthese_Quotidienne`,
   `Vue_Mensuelle`, `Notes`.
2. **Partagez** ce Sheet en **Éditeur** avec l'adresse email du compte de
   service (étape précédente).
3. Créez deux dossiers Drive : `Archives` et `Sauvegardes`. Partagez-les
   eux aussi en **Éditeur** avec la même adresse. Notez l'ID de chaque
   dossier (dans l'URL : `drive.google.com/drive/folders/<ID>`), et l'ID du
   Sheet (dans son URL : `.../spreadsheets/d/<ID>/edit`).

### 10.3 ⚠️ Créer les 2 fichiers Drive fixes (obligatoire, une seule fois)

Testé en conditions réelles : un compte de service **n'a aucun quota de
stockage propre**, donc il ne peut jamais **créer** de nouveau fichier sur
un Drive personnel (erreur `storageQuotaExceeded`), même dans un dossier
partagé en Éditeur — seule la **modification** d'un fichier déjà possédé
par un humain fonctionne. Le bot pousse donc vers **un seul fichier fixe
par dossier** (dernier instantané, pas un fichier par mois/jour) :

1. Dans le dossier **Archives**, créez (upload ou "Nouveau → Importer un
   fichier") un fichier vide nommé exactement `Production_BU_Lavage_dernier_export.xlsx`.
2. Dans le dossier **Sauvegardes**, créez de même un fichier vide nommé
   exactement `bot_lavage_dernier.db`.

Le bot les retrouve par leur nom au premier envoi puis les met à jour à
chaque clôture de poste. L'historique complet n'est pas perdu pour autant :
l'onglet **Donnees** du Google Sheet est en ajout seul (il grossit, ne
s'écrase jamais) — c'est la vraie mémoire long terme, ces 2 fichiers Drive
ne sont qu'un instantané de secours.

### 10.4 Configurer le bot
Sur PythonAnywhere, uploadez le fichier JSON téléchargé quelque part **hors
du dossier servi publiquement** (ex. `/home/<votre-user>/secrets/service-account.json`),
puis dans `.env` :
```
GOOGLE_SERVICE_ACCOUNT_JSON=/home/<votre-user>/secrets/service-account.json
GOOGLE_SPREADSHEET_ID=<id du Sheet>
GOOGLE_DRIVE_ARCHIVES_FOLDER_ID=<id du dossier Archives>
GOOGLE_DRIVE_BACKUPS_FOLDER_ID=<id du dossier Sauvegardes>
```
Reload la web app. À la prochaine clôture de poste (ou via `/export`), le
bot écrit dans le Sheet et pousse l'export + la sauvegarde `.db` vers Drive.
En cas d'échec (fichiers fixes pas encore créés, quota, réseau), le bot le
signale dans le groupe au lieu d'échouer silencieusement.

### 10.5 Point d'attention réseau (compte gratuit)
Les comptes PythonAnywhere gratuits passent par un proxy HTTP imposé pour
les requêtes sortantes ; `*.google.com` / `*.googleapis.com` sont sur liste
blanche, donc l'authentification et les appels Sheets/Drive passent. Faites
malgré tout un test isolé après configuration : envoyez `/export` et
vérifiez dans le groupe qu'aucun message d'échec n'apparaît.

## 11. Déclenchement automatique de la synthèse de fin de poste

Le bot vérifie **à chaque message reçu** si un poste actif a dépassé son
heure de fin sans avoir été clôturé, et le fait à ce moment-là — aucune
configuration supplémentaire n'est nécessaire pour que ça fonctionne dans un
groupe actif toute la journée.

Pour un déclenchement garanti pile à l'heure même sans trafic (optionnel),
un endpoint dédié existe : `GET /cron/<WEBHOOK_SECRET>/tick`. Si votre plan
PythonAnywhere permet des **Scheduled Tasks** (Dashboard → onglet **Tasks**),
ajoutez-y (heures en **UTC**, Alger = UTC+1 toute l'année) :
- `15:35` UTC → clôture du poste 1 (16:30 Alger)
- `23:35` UTC → clôture du poste 2 si activé (00:30 Alger le lendemain)

```bash
curl "https://<votre-user>.pythonanywhere.com/cron/<WEBHOOK_SECRET>/tick"
```

## Maintenance
- Le compte gratuit PythonAnywhere se désactive après ~3 mois d'inactivité de
  connexion au dashboard — se reconnecter simplement de temps en temps suffit.
- Après toute modification du code : **Reload** la web app depuis l'onglet Web.
- Logs d'erreurs : onglet **Web** → **Error log** / **Server log**.
- La base SQLite (`DB_PATH`) est la **source de vérité** ; l'Excel/Google
  Sheet n'est qu'un export lisible. Si Drive est configuré, une copie du
  `.db` est aussi poussée vers `/Sauvegardes/` à chaque clôture de poste
  (rotation 30 jours) — c'est le seul vrai filet de sécurité en cas de
  perte du compte PythonAnywhere.
