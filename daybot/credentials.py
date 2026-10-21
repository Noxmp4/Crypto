"""Clés API Coinbase : variables d'environnement, sinon Trousseau macOS (Keychain).

Sur macOS, `python -m daybot keychain-import cdp_api_key.json` range la clé dans le
Trousseau de session (via le paquet `keyring`), puis vous pouvez supprimer le JSON :
plus aucun secret en clair sur le disque ni dans un plist launchd.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SERVICE = "daybot-coinbase"
NAMES = ("COINBASE_API_KEY", "COINBASE_API_SECRET")


def _keyring():
    try:
        import keyring
    except ImportError:
        return None
    return keyring


def get(name: str) -> str | None:
    value = os.getenv(name)
    if value:
        return value
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(SERVICE, name)
    except Exception:  # noqa: BLE001 — pas de trousseau disponible (Linux headless…)
        return None


def credentials() -> dict:
    """Arguments pour RESTClient : key_file, ou api_key/api_secret (env puis Trousseau)."""
    key_file = os.getenv("COINBASE_KEY_FILE")
    if key_file:
        path = Path(key_file).expanduser()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise RuntimeError(f"Fichier de clé Coinbase illisible ({path}) : {e}") from e
        if not (isinstance(data, dict) and data.get("name") and data.get("privateKey")):
            raise RuntimeError(f"Fichier de clé Coinbase incomplet ({path}) : name/privateKey manquants")
        return {"key_file": str(path)}
    key, secret = get("COINBASE_API_KEY"), get("COINBASE_API_SECRET")
    if not (key and secret):
        raise RuntimeError(
            "Clé API introuvable. Définissez COINBASE_KEY_FILE ou COINBASE_API_KEY/"
            "COINBASE_API_SECRET, ou sur macOS : python -m daybot keychain-import <fichier.json>")
    return {"api_key": key, "api_secret": secret}


def import_key_file(path: str) -> str:
    """Copie le JSON CDP {name, privateKey} dans le Trousseau. Renvoie le nom de la clé."""
    kr = _keyring()
    if kr is None:
        raise RuntimeError("Le paquet `keyring` n'est pas installé (pip install keyring).")
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    name, secret = data["name"], data["privateKey"]
    kr.set_password(SERVICE, "COINBASE_API_KEY", name)
    kr.set_password(SERVICE, "COINBASE_API_SECRET", secret)
    return name


def forget() -> None:
    kr = _keyring()
    if kr is None:
        return
    for n in NAMES:
        try:
            kr.delete_password(SERVICE, n)
        except Exception:  # noqa: BLE001 — déjà absent
            pass


def anthropic_key() -> str | None:
    """Clé Anthropic : ANTHROPIC_API_KEY, ou fichier ANTHROPIC_API_KEY_FILE (secret Docker)."""
    if os.getenv("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    path = os.getenv("ANTHROPIC_API_KEY_FILE")
    if path and os.path.exists(path):
        key = Path(path).read_text(encoding="utf-8").strip()
        return key or None
    return None
