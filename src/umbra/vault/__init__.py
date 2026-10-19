"""Credential vault: umbra fills logins the agent never sees.

The agent names an entry (`fill_secret(tab, "github")`); umbra resolves it
from one of three stores, checks the page's origin against the entry, fills
the fields in an isolated JS world and answers with field names only.

Stores (picked by name prefix):
  github          local file, encrypted with a passphrase that only the
                  human types (`python -m umbra vault unlock`)
  kr:github       system keyring via `secret-tool` (libsecret)
  bw:GitHub       Bitwarden via `rbw` (live lookup, no copy)

Guards (umbra.vault.guard): output/log redaction of every filled secret,
read-tool lockout on a tab while the secret is in its page, refusal to fill
into a browser where agent JS may still be running, optional desktop
confirmation per entry, and an audit log without values.
"""
