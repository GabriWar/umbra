"""`python -m umbra vault ...` — run by the human, in their own terminal.

Every secret is read with getpass, never from argv (argv shows up in `ps`
and shell history, both of which the agent can read).
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from umbra.vault import guard, store, unlock


def _passphrase(*, new: bool = False) -> str:
    p = getpass.getpass("vault passphrase: ")
    if new and getpass.getpass("repeat passphrase: ") != p:
        sys.exit("passphrases differ")
    if new and len(p) < 12:
        sys.exit("use at least 12 characters")
    return p


def _open_local(*, create: bool = False) -> store.LocalVault:
    new = create and not store.vault_path().exists()
    if new:
        print(f"creating {store.vault_path()}")
    try:
        return store.LocalVault.open(_passphrase(new=new), create=create)
    except store.VaultError as e:
        sys.exit(str(e))


def _add(a: argparse.Namespace) -> None:
    if not a.origin:
        sys.exit("pass at least one --origin (e.g. --origin https://github.com)")
    try:
        origins = [store.normalize_origin(o) for o in a.origin]
    except ValueError as e:
        sys.exit(str(e))
    entry = store.Entry(name=a.name, origins=origins, username=a.user or "",
                        password=getpass.getpass(f"password for {a.name}: "),
                        totp=getpass.getpass("TOTP seed or otpauth:// URI: ") if a.totp else "",
                        confirm=a.confirm)
    if entry.totp:
        try:
            store.totp_now(entry.totp)
        except (ValueError, KeyError) as e:
            sys.exit(f"TOTP seed not usable: {e}")
    if a.store == "keyring":
        store.keyring_put(entry)
        print(f"saved as kr:{a.name}")
    else:
        _open_local(create=True).put(entry)
        print(f"saved as {a.name}")


def _list(a: argparse.Namespace) -> None:
    if store.vault_path().exists():
        for e in _open_local().entries().values():
            print(f"{e.name:30} {' '.join(e.origins)}{'  [confirm]' if e.confirm else ''}"
                  f"{'  [totp]' if e.totp else ''}")
    if store.keyring_available():
        for n in store.keyring_names():
            print(f"kr:{n}")
    if store.rbw_available():
        for n in store.bw_names():
            print(n)


def _rm(a: argparse.Namespace) -> None:
    if a.name.startswith("kr:"):
        store.keyring_remove(a.name[3:])
        print("removed")
        return
    print("removed" if _open_local().remove(a.name) else f"no local entry {a.name!r}")


def _unlock(a: argparse.Namespace) -> None:
    pw = _passphrase()
    try:
        store.LocalVault.open(pw)  # check here so a typo doesn't hit every server
    except store.VaultError as e:
        sys.exit(str(e))
    results = unlock.broadcast({"op": "unlock", "passphrase": pw, "minutes": a.minutes})
    if not results:
        sys.exit("no running umbra server found (start your agent first)")
    for pid, r in results:
        print(f"umbra pid {pid}: {'unlocked for %g min' % a.minutes if r.get('ok') else r.get('error')}")


def _lock(_a: argparse.Namespace) -> None:
    for pid, r in unlock.broadcast({"op": "lock"}):
        print(f"umbra pid {pid}: {'locked' if r.get('ok') else r.get('error')}")


def _import_bw(a: argparse.Namespace) -> None:
    entries = store.parse_bitwarden_export(Path(a.file).read_text())
    v = _open_local(create=True)
    for e in entries:
        v.put(e)
    print(f"imported {len(entries)} logins. Delete the export now: shred -u {a.file}")


def _audit(a: argparse.Namespace) -> None:
    p = guard.audit_path()
    if not p.exists():
        return
    for line in p.read_text().splitlines()[-a.n:]:
        print(json.dumps(json.loads(line), ensure_ascii=False))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m umbra vault")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="add or replace an entry (prompts for the password)")
    p.add_argument("name")
    p.add_argument("--origin", action="append", help="allowed origin; repeat; https://*.x.com for subdomains")
    p.add_argument("--user", help="username / email (not secret)")
    p.add_argument("--totp", action="store_true", help="also prompt for a TOTP seed")
    p.add_argument("--confirm", action="store_true", help="ask on the desktop before every fill")
    p.add_argument("--store", choices=("local", "keyring"), default="local")
    p.set_defaults(fn=_add)

    sub.add_parser("list", help="names and origins").set_defaults(fn=_list)

    p = sub.add_parser("rm", help="remove an entry (kr:NAME for keyring)")
    p.add_argument("name")
    p.set_defaults(fn=_rm)

    p = sub.add_parser("unlock", help="unlock the local vault in running umbra servers")
    p.add_argument("--minutes", type=float, default=30)
    p.set_defaults(fn=_unlock)

    sub.add_parser("lock", help="lock it again in every running server").set_defaults(fn=_lock)

    p = sub.add_parser("import-bitwarden", help="copy logins from a plain Bitwarden JSON export")
    p.add_argument("file")
    p.set_defaults(fn=_import_bw)

    p = sub.add_parser("audit", help="last fill attempts")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(fn=_audit)

    a = ap.parse_args(argv)
    a.fn(a)
    return 0
