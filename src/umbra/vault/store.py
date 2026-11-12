"""Where credentials live: local encrypted file, system keyring, Bitwarden.

Name prefix picks the store: `kr:` keyring, `bw:` Bitwarden (rbw), `inf:`
Infisical (CLI), anything else the local file. Nothing in here ever returns a value to the agent; the
server only hands `Entry` objects to umbra.vault.fill.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import string
import tempfile
import struct
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

# OWASP 2023 figure for PBKDF2-HMAC-SHA256.
_KDF_ITERATIONS = 600_000
_KEYRING_APP = "umbra-vault"


class VaultError(Exception):
    """Lookup failed for a reason the agent may be told (never contains a value)."""


@dataclass
class Entry:
    name: str
    origins: list[str] = field(default_factory=list)
    username: str = ""
    password: str = ""
    totp: str = ""        # base32 seed or otpauth:// URI
    confirm: bool = False  # ask the human on the desktop before each fill

    def public(self, backend: str) -> dict[str, Any]:
        """What the agent may see about this entry."""
        return {"name": self.name, "origins": self.origins, "backend": backend,
                "has_totp": bool(self.totp), "confirm": self.confirm}


# ─────────────────────────────── origins ───────────────────────────────

def normalize_origin(raw: str) -> str:
    """'github.com' / 'https://github.com/login' → 'https://github.com'.
    Keeps a leading '*.' wildcard on the host."""
    raw = raw.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"no host in origin {raw!r}")
    if raw.split("://", 1)[1].startswith("*."):
        host = "*." + host.removeprefix("*.")
    port = parts.port
    default = {"https": 443, "http": 80}.get(parts.scheme)
    suffix = f":{port}" if port and port != default else ""
    return f"{parts.scheme}://{host}{suffix}"


def origin_allowed(origin: str, patterns: list[str]) -> bool:
    """Exact scheme+host+port match. `https://*.example.com` covers subdomains
    only (not the apex) — subdomains must be opted into explicitly."""
    try:
        have = normalize_origin(origin)
    except ValueError:
        return False
    scheme, rest = have.split("://", 1)
    for p in patterns:
        try:
            want = normalize_origin(p)
        except ValueError:
            continue
        if want == have:
            return True
        w_scheme, w_rest = want.split("://", 1)
        if w_scheme == scheme and w_rest.startswith("*.") and rest.endswith(w_rest[1:]):
            return True
    return False


# ──────────────────────────────── TOTP ─────────────────────────────────

def totp_now(seed: str, at: float | None = None) -> str:
    """RFC 6238 code from a base32 seed or an otpauth:// URI."""
    digits, period, algo = 6, 30, "sha1"
    if seed.startswith("otpauth://"):
        q = parse_qs(urlsplit(seed).query)
        seed = q["secret"][0]
        digits = int(q.get("digits", ["6"])[0])
        period = int(q.get("period", ["30"])[0])
        algo = q.get("algorithm", ["SHA1"])[0].lower()
    key = base64.b32decode(seed.replace(" ", "").upper() + "=" * (-len(seed.replace(" ", "")) % 8))
    counter = int((time.time() if at is None else at) // period)
    mac = hmac.new(key, struct.pack(">Q", counter), getattr(hashlib, algo)).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


# ───────────────────────────── local file ──────────────────────────────

def vault_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return Path(base) / "umbra" / "vault.json"


def _derive(passphrase: str, salt: bytes, iterations: int) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


class LocalVault:
    """Encrypted JSON file. Holds the derived key, never the passphrase."""

    def __init__(self, key: bytes, path: Path | None = None):
        self.key = key
        self.path = path or vault_path()

    @classmethod
    def open(cls, passphrase: str, path: Path | None = None, *, create: bool = False) -> "LocalVault":
        from cryptography.fernet import InvalidToken
        path = path or vault_path()
        if not path.exists():
            if not create:
                raise VaultError(f"no local vault at {path} — add an entry with "
                                 "`python -m umbra vault add` first")
            salt = os.urandom(16)
            v = cls(_derive(passphrase, salt, _KDF_ITERATIONS), path)
            v._write({}, salt, _KDF_ITERATIONS)
            return v
        head = json.loads(path.read_text())
        v = cls(_derive(passphrase, base64.b64decode(head["salt"]), head["iter"]), path)
        try:
            v.entries()
        except InvalidToken:
            raise VaultError("wrong passphrase") from None
        return v

    def entries(self) -> dict[str, Entry]:
        from cryptography.fernet import Fernet
        head = json.loads(self.path.read_text())
        data = json.loads(Fernet(self.key).decrypt(head["data"].encode()))
        return {k: Entry(name=k, **v) for k, v in data.items()}

    def put(self, entry: Entry) -> None:
        items = self.entries()
        items[entry.name] = entry
        self._save(items)

    def remove(self, name: str) -> bool:
        items = self.entries()
        found = items.pop(name, None) is not None
        if found:
            self._save(items)
        return found

    def _save(self, items: dict[str, Entry]) -> None:
        head = json.loads(self.path.read_text())
        self._write(items, base64.b64decode(head["salt"]), head["iter"])

    def _write(self, items: dict[str, Entry], salt: bytes, iterations: int) -> None:
        from cryptography.fernet import Fernet
        plain = {k: {f: v for f, v in asdict(e).items() if f != "name"} for k, e in items.items()}
        token = Fernet(self.key).encrypt(json.dumps(plain).encode()).decode()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"v": 1, "kdf": "pbkdf2-sha256", "iter": iterations,
                       "salt": base64.b64encode(salt).decode(), "data": token}, f)
        os.replace(tmp, self.path)


# In-process unlock state for the local file (server side). Set only by the
# unlock socket, which only the human's terminal talks to.
_unlocked: dict[str, Any] = {"vault": None, "until": 0.0}


def unlock(passphrase: str, minutes: float) -> float:
    _unlocked["vault"] = LocalVault.open(passphrase)
    _unlocked["until"] = time.time() + minutes * 60
    return _unlocked["until"]


def lock() -> None:
    _unlocked["vault"] = None
    _unlocked["until"] = 0.0


def unlocked_vault() -> LocalVault | None:
    if _unlocked["vault"] is not None and time.time() >= _unlocked["until"]:
        lock()
    return _unlocked["vault"]


# ───────────────────────── keyring (secret-tool) ─────────────────────────

def _run(cmd: list[str], stdin: str | None = None, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)


def keyring_available() -> bool:
    return shutil.which("secret-tool") is not None


def keyring_put(entry: Entry) -> None:
    body = json.dumps({f: v for f, v in asdict(entry).items() if f != "name"})
    r = _run(["secret-tool", "store", "--label", f"umbra vault: {entry.name}",
              "app", _KEYRING_APP, "name", entry.name], stdin=body)
    if r.returncode != 0:
        raise VaultError(f"secret-tool store failed: {r.stderr.strip()[:200]}")


def keyring_get(name: str) -> Entry:
    r = _run(["secret-tool", "lookup", "--", "app", _KEYRING_APP, "name", name])
    if r.returncode != 0 or not r.stdout:
        raise VaultError(f"no keyring entry named {name!r}")
    return Entry(name="kr:" + name, **json.loads(r.stdout))


def keyring_names() -> list[str]:
    # `search` prints secrets too; only the attribute lines are read here.
    r = _run(["secret-tool", "search", "--all", "app", _KEYRING_APP])
    return [line.split("=", 1)[1].strip() for line in (r.stdout + r.stderr).splitlines()
            if line.startswith("attribute.name =")]


def keyring_remove(name: str) -> None:
    _run(["secret-tool", "clear", "app", _KEYRING_APP, "name", name])


# ───────────────────────────── Bitwarden ──────────────────────────────

def rbw_available() -> bool:
    return shutil.which("rbw") is not None


def _bw_entry(name: str, item: dict[str, Any]) -> Entry:
    """Bitwarden item (rbw --raw, or a JSON export) → Entry."""
    data = item.get("data") or item.get("login") or {}
    data = data.get("Login", data)  # rbw serializes the login variant tagged
    uris = [u.get("uri") if isinstance(u, dict) else u for u in data.get("uris") or []]
    origins = []
    for u in uris:
        try:
            origins.append(normalize_origin(u))
        except (ValueError, TypeError):
            continue
    confirm = any((f.get("name") or "").lower() == "umbra-confirm"
                  and str(f.get("value")).lower() in ("1", "true", "yes")
                  for f in item.get("fields") or [])
    return Entry(name=name, origins=origins, username=data.get("username") or "",
                 password=data.get("password") or "", totp=data.get("totp") or "",
                 confirm=confirm)


def bw_names() -> list[str]:
    """`bw:Name`, or `bw:Name::user` when one name holds several accounts.
    Never prompts: a locked rbw lists nothing."""
    if _run(["rbw", "unlocked"], timeout=10).returncode != 0:
        return []
    r = _run(["rbw", "list", "--fields", "name,user"], timeout=30)
    rows = [line.split("\t") + [""] for line in r.stdout.splitlines() if line]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row[0]] = counts.get(row[0], 0) + 1
    return [f"bw:{n}::{u}" if counts[n] > 1 and u else f"bw:{n}" for n, u, *_ in rows]


def bw_generate(ref: str, origins: list[str], username: str, length: int) -> str:
    """`rbw generate` makes and stores the password; it comes back on stdout
    only so umbra can register it for redaction / write the out_file."""
    if f"bw:{ref}" in bw_names():
        raise VaultError(f"bw:{ref} already exists")
    uri = ["--uri", origins[0]] if origins else []
    r = _run(["rbw", "generate", *uri, "--", str(length), ref, *([username] if username else [])])
    if r.returncode != 0 or not r.stdout.strip():
        raise VaultError(f"rbw generate failed: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def bw_get(ref: str) -> Entry:
    name, _, user = ref.partition("::")
    # A locked rbw pops pinentry here: that prompt goes to the human, not us.
    # `--`: a name like "--clipboard" would otherwise copy the password out.
    r = _run(["rbw", "get", "--raw", "--", name, *([user] if user else [])])
    if r.returncode != 0:
        raise VaultError(f"rbw get failed for {name!r}: {r.stderr.strip()[:200]}")
    return _bw_entry("bw:" + ref, json.loads(r.stdout))


def parse_bitwarden_export(text: str) -> list[Entry]:
    """Unencrypted Bitwarden JSON export → login entries (type 1)."""
    doc = json.loads(text)
    if doc.get("encrypted"):
        raise VaultError("this export is encrypted; export as plain JSON, "
                         "import it, then delete the file")
    out: list[Entry] = []
    seen: dict[str, int] = {}
    for item in doc.get("items", []):
        if item.get("type") != 1:
            continue
        name = item.get("name") or "unnamed"
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            name = f"{name}/{(item.get('login') or {}).get('username') or seen[name]}"
        out.append(_bw_entry(name, item))
    return out


# ───────────────────────────── Infisical ──────────────────────────────
# One secret per entry, its value the entry as JSON ({origins, username,
# password, totp, confirm}). Scope from UMBRA_INFISICAL_ENV (default dev),
# UMBRA_INFISICAL_PATH (default /) and UMBRA_INFISICAL_PROJECT_ID (else the
# CLI's own .infisical.json / login context).

_INF_KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def infisical_available() -> bool:
    return shutil.which("infisical") is not None


def _inf_scope() -> list[str]:
    args = ["--env", os.environ.get("UMBRA_INFISICAL_ENV", "dev"),
            "--path", os.environ.get("UMBRA_INFISICAL_PATH", "/"), "--silent"]
    if pid := os.environ.get("UMBRA_INFISICAL_PROJECT_ID"):
        args += ["--projectId", pid]
    return args


def _inf_key(key: str) -> str:
    if not _INF_KEY.match(key):
        raise VaultError(f"infisical key {key!r} must be letters, digits, _ . -")
    return key


def _inf_entry(key: str, value: str) -> Entry | None:
    try:
        data = json.loads(value)
    except ValueError:
        return None
    if not isinstance(data, dict) or "origins" not in data:
        return None
    fields = {f: data[f] for f in ("origins", "username", "password", "totp", "confirm") if f in data}
    return Entry(name="inf:" + key, **fields)


def infisical_get(key: str) -> Entry:
    r = _run(["infisical", "secrets", "get", _inf_key(key), "--plain", "--expand=false",
              *_inf_scope()], timeout=30)
    value = r.stdout.strip()
    if r.returncode != 0 or not value or value == "*not found*":
        raise VaultError(f"no infisical secret named {key!r}"
                         + (f": {r.stderr.strip()[:200]}" if r.returncode else ""))
    entry = _inf_entry(key, value)
    if entry is None:
        raise VaultError(f"inf:{key} is not an umbra entry (needs a JSON value with "
                         "origins/username/password); create one with secret_generate")
    return entry


def infisical_put(entry: Entry) -> None:
    key = _inf_key(entry.name.removeprefix("inf:"))
    body = json.dumps({f: v for f, v in asdict(entry).items() if f != "name"})
    # KEY=@file keeps the value out of argv (ps is readable by the agent).
    with tempfile.TemporaryDirectory(prefix="umbra-inf-") as d:
        f = Path(d) / "v"
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        r = _run(["infisical", "secrets", "set", f"{key}=@{f}", *_inf_scope()], timeout=60)
    if r.returncode != 0:
        raise VaultError(f"infisical secrets set failed: {r.stderr.strip()[:200]}")


def infisical_entries() -> list[Entry]:
    r = _run(["infisical", "secrets", "-o", "json", "--expand=false", *_inf_scope()], timeout=30)
    if r.returncode != 0:
        return []
    try:
        rows = json.loads(r.stdout)
    except ValueError:
        return []
    out = []
    for row in rows if isinstance(rows, list) else []:
        key = row.get("secretKey") or row.get("key") or ""
        e = _inf_entry(key, row.get("secretValue") or row.get("value") or "")
        if e is not None:
            out.append(e)
    return out


# ───────────────────────────── generate ──────────────────────────────

_SYMBOLS = "!@#%^*-_"


def generate_password(length: int = 20) -> str:
    """Letters + digits + a few symbols most sites accept; one of each class."""
    length = max(12, min(length, 128))
    alphabet = string.ascii_letters + string.digits + _SYMBOLS
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in p) and any(c.isupper() for c in p)
                and any(c.isdigit() for c in p) and any(c in _SYMBOLS for c in p)):
            return p


def exists(name: str) -> bool:
    try:
        resolve(name)
    except VaultError:
        return False
    return True


def create(name: str, origins: list[str], username: str, length: int) -> str:
    """Generate a password, store a new entry under `name`, return the password
    (to the server only — it never goes into a tool result). Refuses to
    overwrite an existing entry."""
    if any(part.startswith("-") for part in name.split(":", 1)[-1].split("::")):
        raise VaultError("entry names may not start with '-'")
    if name.startswith("bw:"):
        if not rbw_available():
            raise VaultError("rbw not installed (`sudo pacman -S rbw`, then `rbw login`)")
        return bw_generate(name[3:], origins, username, length)
    password = generate_password(length)
    entry = Entry(name=name, origins=origins, username=username, password=password)
    if name.startswith("kr:"):
        if not keyring_available():
            raise VaultError("secret-tool not installed (libsecret)")
        if exists(name):
            raise VaultError(f"{name} already exists")
        entry.name = name[3:]
        keyring_put(entry)
    elif name.startswith("inf:"):
        if not infisical_available():
            raise VaultError("infisical CLI not installed")
        _inf_key(name[4:])
        if exists(name):
            raise VaultError(f"{name} already exists")
        infisical_put(entry)
    else:
        v = unlocked_vault()
        if v is None:
            raise VaultError("local vault is locked — use a kr:/inf:/bw: name, or ask the "
                             "human to run `python -m umbra vault unlock`")
        if name in v.entries():
            raise VaultError(f"{name} already exists")
        v.put(entry)
    return password

# ─────────────────────────────── resolve ───────────────────────────────

def resolve(name: str) -> Entry:
    # Names reach argv of rbw / secret-tool; nothing that parses as a flag.
    if any(part.startswith("-") for part in name.removeprefix("bw:").removeprefix("kr:").split("::")):
        raise VaultError("entry names may not start with '-'")
    if name.startswith("bw:"):
        if not rbw_available():
            raise VaultError("rbw not installed (`sudo pacman -S rbw`, then `rbw login`)")
        return bw_get(name[3:])
    if name.startswith("kr:"):
        if not keyring_available():
            raise VaultError("secret-tool not installed (libsecret)")
        return keyring_get(name[3:])
    if name.startswith("inf:"):
        if not infisical_available():
            raise VaultError("infisical CLI not installed")
        return infisical_get(name[4:])
    v = unlocked_vault()
    if v is None:
        raise VaultError("local vault is locked — the human must run "
                         "`python -m umbra vault unlock` in their own terminal")
    entry = v.entries().get(name)
    if entry is None:
        raise VaultError(f"no local entry named {name!r}")
    return entry


def list_public() -> dict[str, Any]:
    """Names + origins for the agent. Never values, never usernames."""
    out: dict[str, Any] = {"entries": []}
    v = unlocked_vault()
    if v is not None:
        out["entries"] += [e.public("local") for e in v.entries().values()]
        out["local"] = {"state": "unlocked", "locks_at": int(_unlocked["until"])}
    else:
        out["local"] = {"state": "locked" if vault_path().exists() else "absent"}
    if keyring_available():
        for n in keyring_names():
            try:
                out["entries"].append(keyring_get(n).public("keyring"))
            except VaultError:
                continue
    if infisical_available():
        out["entries"] += [e.public("infisical") for e in infisical_entries()]
    if rbw_available():
        # Origins/flags need a decrypt per item; names alone are enough to pick.
        out["entries"] += [{"name": n, "backend": "bitwarden"} for n in bw_names()]
    return out
