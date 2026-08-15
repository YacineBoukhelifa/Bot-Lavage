"""Point d'entree WSGI (utilise par PythonAnywhere)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Charge les variables d'environnement depuis .env si le fichier existe (dev local).
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from bot.app import app as application  # noqa: E402
