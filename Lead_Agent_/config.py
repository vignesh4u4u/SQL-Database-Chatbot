import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

from dotenv import find_dotenv, load_dotenv
from pymongo import MongoClient

SETTINGS_DB_NAME = "b2b_leads"
SETTINGS_COLLECTION_NAME = "settings_config"
_SKIP_KEYS = frozenset({"_id", "config_key"})

_CONFIG_LOCK = threading.Lock()
_MONGO_CLIENT: Optional[MongoClient] = None
_MONGO_URI_CACHED: Optional[str] = None
_LOADED_MONGO_KEYS: set[str] = set()

_logger = logging.getLogger("lead_discovery")

load_dotenv()


def _get_settings_client(mongo_uri: str) -> MongoClient:
    global _MONGO_CLIENT, _MONGO_URI_CACHED
    if _MONGO_CLIENT is None or _MONGO_URI_CACHED != mongo_uri:
        if _MONGO_CLIENT is not None:
            try:
                _MONGO_CLIENT.close()
            except Exception:
                pass
        _MONGO_CLIENT = MongoClient(mongo_uri, serverSelectionTimeoutMS=8000)
        _MONGO_URI_CACHED = mongo_uri
    return _MONGO_CLIENT


def _serialize_env_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value).strip()


def load_settings(raise_on_error: bool = True) -> dict[str, str]:
    with _CONFIG_LOCK:
        mongo_uri = os.getenv("MONGO_URI", "").strip()
        if not mongo_uri:
            if raise_on_error:
                raise ValueError("MONGO_URI is missing from .env / environment.")
            _logger.warning("MONGO_URI is missing; skipping MongoDB settings_config load.")
            return {}

        try:
            client = _get_settings_client(mongo_uri)
            doc = client[SETTINGS_DB_NAME][SETTINGS_COLLECTION_NAME].find_one({}, {"_id": 0})
        except Exception as exc:
            if raise_on_error:
                raise
            _logger.warning("Failed to load settings_config from MongoDB on import: %s", exc)
            return {}

        if not isinstance(doc, dict):
            for stale_key in list(_LOADED_MONGO_KEYS):
                os.environ.pop(stale_key, None)
            _LOADED_MONGO_KEYS.clear()
            return {}

        applied: dict[str, str] = {}
        new_keys: set[str] = set()
        for raw_key, raw_val in doc.items():
            key = str(raw_key).strip()
            if not key or key in _SKIP_KEYS:
                continue
            serialized = _serialize_env_value(raw_val)
            if key == "MONGO_URI" and not serialized:
                continue
            if serialized is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = serialized
                applied[key] = serialized
                new_keys.add(key)

        for stale_key in _LOADED_MONGO_KEYS - new_keys:
            os.environ.pop(stale_key, None)
        _LOADED_MONGO_KEYS.clear()
        _LOADED_MONGO_KEYS.update(new_keys)
        return applied

def get_all_settings() -> dict[str, Any]:
    settings: dict[str, Any] = {}
    for key in _LOADED_MONGO_KEYS:
        raw = os.getenv(key, "").strip()
        if raw.startswith("[") and raw.endswith("]"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    settings[key] = parsed
                    continue
            except json.JSONDecodeError:
                pass
        settings[key] = raw
    return settings

refresh_settings = load_settings
load_settings(raise_on_error=False)
