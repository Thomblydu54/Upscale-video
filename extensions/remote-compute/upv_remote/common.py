"""Protocole borné et TLS épinglé. Les secrets ne sont jamais placés dans les URL."""
from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import re
import secrets
from dataclasses import fields
import ssl
from urllib.parse import urlsplit

from upscaler import engine
from upscaler.extension_runtime import ExtensionError

CHUNK = 2 << 20
JSON_LIMIT = 64 << 10
MAX_FILE = 2 << 40
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".mpg",
             ".mpeg", ".ts", ".m2ts", ".mts", ".vob", ".3gp", ".ogv", ".divx", ".asf", ".gif")
TERMINAL = ("delivered", "cleaned", "cancelled")


class ProtocolError(ExtensionError):
    def __init__(self, text, status=400):
        super().__init__(text)
        self.status = status


def token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def identifier(value: str, request=False) -> str:
    pattern = r"[A-Za-z0-9_-]{1,80}" if request else r"[0-9a-f]{32}"
    if not re.fullmatch(pattern, str(value)):
        raise ProtocolError("Identifiant invalide.")
    return str(value)


def digest(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", str(value)):
        raise ProtocolError("Empreinte SHA-256 invalide.")
    return value


def filename(value: str) -> str:
    # Un nom reçu du réseau ne sert JAMAIS de chemin. Le stockage emploie des noms fixes.
    value = str(value).replace("\\", "/").rsplit("/", 1)[-1]
    value = re.sub(r'[\x00-\x1f<>:"|?*]', "_", value).strip(" .")
    if len(value) > 160:
        suffix = "." + value.rsplit(".", 1)[-1] if re.search(r"\.[A-Za-z0-9]{1,8}$", value) else ""
        value = value[:160 - len(suffix)] + suffix
    if not value:
        raise ProtocolError("Nom de fichier manquant.")
    return value


def endpoint(value: str) -> str:
    value = str(value).strip().rstrip("/")
    p = urlsplit(value)
    try:
        port = p.port
    except ValueError:
        raise ProtocolError("Port de relais invalide.") from None
    if (p.scheme != "https" or not p.hostname or p.username or p.password
            or p.query or p.fragment or p.path or (port is not None and not 1 <= port <= 65535)
            or any(c.isspace() for c in value)):
        raise ProtocolError("Utilisez https://adresse-publique:port, sans chemin ni identifiant.")
    # Ni URL de fichier, ni socket spécial, ni redirection vers un autre protocole.
    return value


def pairing_code(url: str, pin: str, secret: str) -> str:
    raw = json.dumps({"v": 1, "url": endpoint(url), "pin": digest(pin), "secret": secret},
                     separators=(",", ":")).encode("utf-8")
    return "UPV1-" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_pairing(code: str) -> dict:
    code = str(code).strip()
    if not code.startswith("UPV1-") or len(code) > 2000:
        raise ProtocolError("Ce n'est pas un code d'appairage Upscale Video AI.")
    try:
        raw = base64.b64decode(code[5:] + "=" * (-len(code[5:]) % 4), altchars=b"-_", validate=True)
        data = json.loads(raw)
        if data.get("v") != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{43}", data["secret"]):
            raise ValueError()
        return {"url": endpoint(data["url"]), "pin": digest(data["pin"]), "secret": data["secret"]}
    except (ValueError, KeyError, TypeError):
        raise ProtocolError("Code d'appairage incomplet ou invalide.") from None


def safe_settings(data: dict, gpu: str) -> dict:
    if not isinstance(data, dict) or len(json.dumps(data)) > 12000:
        raise ProtocolError("Réglages invalides.")
    allowed = {field.name for field in fields(engine.Settings)}
    st = engine.Settings.from_dict({k: v for k, v in data.items() if k in allowed})
    st.output_dir = st.temp_dir = ""  # aucun chemin du PC principal n'est exécuté à distance
    st.gpu = str(gpu) if re.fullmatch(r"auto|-1|[0-9]{1,3}", str(gpu)) else "auto"
    st.quality = max(0, min(51, st.quality))
    st.tile = st.tile if st.tile in (0, 64, 128, 256, 512) else 0
    return st.to_dict()


def loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


class Client:
    """Nouvelle connexion par requête : aucun partage de socket entre threads.

    CERT_NONE n'est utilisé que parce que le certificat est privé. Son empreinte exacte
    est obligatoirement comparée AVANT l'envoi du bearer ou d'un seul octet de média.
    Aucun proxy système ni redirection HTTP n'est suivi.
    """
    def __init__(self, url: str, pin: str, bearer="", timeout=15):
        self.url, self.pin = endpoint(url), digest(pin)
        self.bearer, self.timeout = bearer, timeout

    def request(self, method: str, path: str, data=None, raw=False, timeout=None):
        p = urlsplit(self.url)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(p.hostname, p.port or 443,
                                           context=context, timeout=timeout or self.timeout)
        try:
            conn.connect()
            actual = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
            if not secrets.compare_digest(actual, self.pin):
                raise ProtocolError("Le certificat du relais a changé : connexion refusée. Réappairez les PC.", 495)
            headers = {"Connection": "close", "User-Agent": "UpscaleVideoAI-Remote/1"}
            if self.bearer:
                headers["Authorization"] = "Bearer " + self.bearer
            body = data
            if data is not None and not isinstance(data, bytes):
                body = json.dumps(data, allow_nan=False).encode("utf-8")
                headers["Content-Type"] = "application/json"
            if isinstance(body, bytes) and len(body) > CHUNK:
                raise ProtocolError("Requête trop volumineuse.")
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            maximum = CHUNK if raw and response.status == 200 else JSON_LIMIT
            content = response.read(maximum + 1)
            if len(content) > maximum:
                raise ProtocolError("Réponse du relais trop volumineuse.")
            if response.status != 200:
                try:
                    error = json.loads(content).get("error", "Erreur du relais.")
                except ValueError:
                    error = "Réponse du relais invalide."
                raise ProtocolError(error, response.status)
            return content if raw else json.loads(content)
        finally:
            conn.close()

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, data=None, **kw):
        return self.request("POST", path, data or {}, **kw)
