#!/bin/sh
# Builds the App Store upload zip. Flat by construction: the Dockerfile, the lockfiles and
# the source sit at the archive root, never nested, because the platform detects the template
# from a root-level Dockerfile and builds with the root as its context.
#
# This is an allowlist, not an exclusion list. The platform runs the test suite against the
# zip root before it builds any image, so the package must be a self-sufficient, testable
# source tree: tests, runner config and docs included, built output and secrets excluded.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

OUT="${1:-appstore-package/pree-upload.zip}"
mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"

INCLUDE="Dockerfile .dockerignore .gitignore requirements.txt requirements.in \
requirements-dev.txt requirements-dev.in pyproject.toml sonar-project.properties \
.python-version .env.example README.md src tests docs scripts"

for entry in $INCLUDE; do
  if [ ! -e "$entry" ]; then
    echo "package: missing required entry $entry" >&2
    exit 1
  fi
done

# Banned from the archive: virtual environments, caches, built output, version-control
# metadata, local data and any environment file.
# -y stores a symlink as a link. Without it zip follows the link and writes the TARGET's bytes
# into the archive under the link's name, so `notes-appendix.md -> ~/.ssh/id_rsa` shipped a
# private key past a scan that only ever saw a markdown filename.
zip -qry "$OUT" $INCLUDE \
  -x '*/__pycache__/*' '*.pyc' '*/.venv/*' '*/.mypy_cache/*' '*/.ruff_cache/*' \
     '*/.pytest_cache/*' '*/data/*' '*.env' '*.zip'

echo "package: wrote $OUT"
echo "--- archive root ---"
unzip -l "$OUT" | awk 'NR>3 && $4 !~ /\// {print $4}' | head -20

for banned in .env .git .venv node_modules coverage; do
  if unzip -Z1 "$OUT" | grep -qE "^(.*/)?${banned}(/|$)"; then
    echo "package: banned entry $banned present in the archive" >&2
    exit 1
  fi
done

# A second pass over what actually landed, by PATH SHAPE ONLY. This scan is content-blind: it
# reads names, never bytes, so it cannot see a credential inside an archive, a document or a
# source file. It is the last net, not the first: the pre-write hook and the secret rules are.
#
# The name denylist above refuses five known directory names and nothing else, which let a
# certificate, a .netrc, an authorized_keys, a creds.txt, a nested tarball and a symlink
# pointing at a private key all ship. Extensions are matched case-insensitively because
# backup.ENV is the same file as backup.env, and the whole-name list covers the credential
# files that carry no extension at all.
#
# .env.example is the one deliberate exception: it carries placeholders, and a test asserts so.
# Every pattern is anchored on a PATH COMPONENT boundary, not on the end of the whole path.
# Anchoring the extension list with `$` matched only the final component's tail, so
# `deploy.key.txt` (a doubled extension) and `tls.pem/server.bundle` (the credential-shaped
# part is a DIRECTORY) both shipped a real private key past a scan that reported clean.
#
# The word list is a word list, and it had "passwd" but not "password", and "key" only as a
# dotted extension: `passwords.txt`, `apikey.txt`, `krb5.keytab`, `deploy_key` and
# `service-account.json` each shipped a real key body while the script reported clean. A
# component ending in "key" or "keys" is now refused whatever its extension.
SUSPECT=$(unzip -Z1 "$OUT" \
  | grep -vE '(^|/)\.env\.example$' \
  | grep -iE \
      '\.(env|pem|key|p12|pfx|jks|keystore|crt|cer|der|p8|pk8|asc|gpg|ppk|kdbx|ovpn)(/|$|\.)'\
'|(^|/)id_(rsa|dsa|ecdsa|ed25519)'\
'|(^|/)(\.netrc|\.pgpass|\.npmrc|\.htpasswd|authorized_keys|known_hosts|shadow)(/|$)'\
'|token|secret|cred|passwd|password|private.?key|api.?key|keytab|kubeconfig|pypirc'\
'|service.?account'\
'|(^|/)[a-z0-9][a-z0-9_.-]*keys?(/|$)' \
  || true)
if [ -n "$SUSPECT" ]; then
  echo "package: the archive carries paths shaped like credentials:" >&2
  echo "$SUSPECT" >&2
  exit 1
fi

# And no symlinks at all. -y stops the content leak, but a stored link still points somewhere
# outside the archive, and what it resolves to on the platform runner is not ours to reason
# about. This project ships no symlink, so any is a defect.
LINKS=$(unzip -Z "$OUT" | grep '^l' || true)
if [ -n "$LINKS" ]; then
  echo "package: the archive carries symbolic links:" >&2
  echo "$LINKS" >&2
  exit 1
fi

# And no HARD links either. -y stores a symlink as a link, so its target's bytes stay out of
# the archive, but a hard link is an ordinary directory entry: zip reads it and writes the
# content, so `notes-appendix.md` hard-linked to a private key shipped the key under a
# harmless name with every name-based check clean. find is the only thing that can see it.
HARDLINKS=$(find $INCLUDE -type f -links +1 -print 2>/dev/null || true)
if [ -n "$HARDLINKS" ]; then
  echo "package: these files have more than one hard link, so their name is not their only" >&2
  echo "         name and the scan above cannot speak for their content:" >&2
  echo "$HARDLINKS" >&2
  exit 1
fi

# Names that are not what they look like. A Cyrillic small letter es renders as a Latin s, so
# `\u0455ecret.md` reads as "secret.md" to a human and matches nothing as bytes. Anything
# outside ASCII in a path is refused: this project ships no such name.
NONASCII=$(unzip -Z1 "$OUT" | LC_ALL=C grep -nP '[^\x20-\x7e]' || true)
if [ -n "$NONASCII" ]; then
  echo "package: the archive carries non-ASCII paths, which can render as a name they are" >&2
  echo "         not; refuse rather than guess:" >&2
  echo "$NONASCII" >&2
  exit 1
fi

echo "package: archive checks passed: no banned directory, no credential-shaped path,"
echo "         no symlink, no multiply-linked file, no non-ASCII path"
echo "package: note: the path scan reads names only; it cannot see inside an archived file"
