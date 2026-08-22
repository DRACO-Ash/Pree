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

# Both listings, captured ONCE with their exit status checked. Every check below reads these
# variables rather than re-running unzip, because `$(cmd || true)` reads a tool failure as an
# empty result and an empty result is what every one of these nets treats as a pass.
if ! LISTING=$(unzip -Z "$OUT") || ! LISTING_NAMES=$(unzip -Z1 "$OUT"); then
  echo "package: could not read the archive listing, so no check below has run" >&2
  exit 1
fi

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
# An ALLOWLIST, after three rounds of losing with a denylist. Each round added words and each
# round shipped a real private key under a name one character outside the list: `deploy_key`
# refused while `deploy_key.txt` and `gitlab-key.pub` shipped; `.crt` was listed and `.cert`
# was not; `.gpg` was listed and `.pgp` was not; `.kdbx` was listed and `.kdb` was not;
# `authorized_keys` was listed and `authorized_keys2` was not. A denylist has to enumerate
# every name a credential might have, which is not a finite set. An allowlist has to enumerate
# what this project ships, which is a short and stable list, and anything new fails by default
# instead of waiting for someone to add a word.
ALLOWED_EXTENSIONS='py|md|txt|toml|in|sh|properties|example|json|yml|yaml|cfg|ini|lock'
ALLOWED_BARE_NAMES='Dockerfile|\.dockerignore|\.gitignore|\.python-version|LICENCE|LICENSE'
SUSPECT=$(unzip -Z1 "$OUT" \
  | grep -v '/$' \
  | grep -vE "(^|/)($ALLOWED_BARE_NAMES)$" \
  | grep -vE "\.($ALLOWED_EXTENSIONS)$" \
  || true)
if [ -n "$SUSPECT" ]; then
  echo "package: the archive carries paths whose extension is not on the allowlist." >&2
  echo "         Allowed extensions: $ALLOWED_EXTENSIONS" >&2
  echo "         Allowed bare names: $ALLOWED_BARE_NAMES" >&2
  echo "         If one of these is genuinely meant to ship, add its extension deliberately." >&2
  echo "$SUSPECT" >&2
  exit 1
fi

# Five rounds of "one character outside the list", so the key rule is now the loosest thing
# that still means something: any component ENDING in "key" or "keys", with no requirement for
# a delimiter before it. `deploy_key` was refused and `deploy_key.txt` shipped; that was fixed
# and `deploykey.txt` shipped, because deleting the delimiter beat the delimited-word rule; and
# `id-rsa.md` shipped because the pattern spelled `id_rsa` with a literal underscore while its
# neighbour on the same line used `.?` for exactly this reason. `vault` and `jwt` were two
# ordinary credential names in no list at all.
#
# BOTH nets, because neither alone is enough and the first attempt at this replaced one with
# the other. The allowlist above bounds the EXTENSION space, so an unknown extension fails by
# default. It says nothing about a credential wearing an allowed extension, and five of the ten
# paths that beat the old denylist do exactly that: deploy_key.txt, deploy_key.sh, sa-key.json,
# bearer.txt and fixture_key.json all end in an extension this project genuinely ships. So the
# name space is bounded too, and "key" is matched as a DELIMITED WORD anywhere in a component
# rather than only at its end, which is what let deploy_key.txt through when deploy_key did not.
NAMED=$(unzip -Z1 "$OUT" \
  | grep -vE '^\.env\.example$' \
  | grep -iE 'keys?([_.-]|$)'\
'|token|secret|cred|passwd|password|bearer|keytab|kubeconfig|pypirc|dotenv'\
'|service.?account|authorized|private.?key|vault|jwt|pat[_.-]|id[_.-]?(rsa|dsa|ecdsa|ed25519)' \
  || true)
if [ -n "$NAMED" ]; then
  echo "package: the archive carries paths whose NAME reads like a credential:" >&2
  echo "$NAMED" >&2
  exit 1
fi

# Exactly one environment file, at the root, and it is the example. `docs/.env.example` shipped
# past both nets above: the extension allowlist admits `.example`, and no credential word
# appears in the name. The content check that catches it lives in the test suite, which is the
# right place for content, but a second environment file anywhere is a name-shape fact this
# script can see and should refuse.
ENVFILES=$(printf '%s\n' "$LISTING_NAMES" | grep -E '(^|/)\.env' || true)
if [ "$ENVFILES" != ".env.example" ]; then
  echo "package: the archive carries environment files other than the root .env.example:" >&2
  printf '%s\n' "$ENVFILES" >&2
  exit 1
fi

# And no symlinks at all. -y stops the content leak, but a stored link still points somewhere
# outside the archive, and what it resolves to on the platform runner is not ours to reason
# about. This project ships no symlink, so any is a defect.
# Exit status, not emptiness. `$(... || true)` reads a tool failure as a pass, and grep -P is
# absent from BusyBox and from some minimal runner images, so a net could report clean having
# never run. Each check below distinguishes "found nothing" from "could not look".
LINKS=$(printf '%s\n' "$LISTING" | grep '^l' || true)
if [ -n "$LINKS" ]; then
  echo "package: the archive carries symbolic links:" >&2
  echo "$LINKS" >&2
  exit 1
fi

# And no HARD links either. -y stores a symlink as a link, so its target's bytes stay out of
# the archive, but a hard link is an ordinary directory entry: zip reads it and writes the
# content, so `notes-appendix.md` hard-linked to a private key shipped the key under a
# harmless name with every name-based check clean. find is the only thing that can see it.
if ! HARDLINKS=$(find $INCLUDE -type f -links +1 -print); then
  echo "package: could not scan for hard links" >&2
  exit 1
fi
if [ -n "$HARDLINKS" ]; then
  echo "package: these files have more than one hard link, so their name is not their only" >&2
  echo "         name and the scan above cannot speak for their content:" >&2
  echo "$HARDLINKS" >&2
  exit 1
fi

# Names that are not what they look like. A Cyrillic small letter es renders as a Latin s, so
# `\u0455ecret.md` reads as "secret.md" to a human and matches nothing as bytes. Anything
# outside ASCII in a path is refused: this project ships no such name.
# A POSIX bracket range, not grep -P: the -P flag is a GNU extension and its absence made this
# net report a pass without running.
NONASCII=$(unzip -Z1 "$OUT" | LC_ALL=C grep -n '[^ -~]' || true)
if [ -n "$NONASCII" ]; then
  echo "package: the archive carries non-ASCII paths, which can render as a name they are" >&2
  echo "         not; refuse rather than guess:" >&2
  echo "$NONASCII" >&2
  exit 1
fi

echo "package: archive checks passed: every extension on the allowlist, no name that"
echo "         matched the credential list, no banned directory, no symlink, no"
echo "         multiply-linked file, no non-ASCII path. This is a NAME check, not a"
echo "         judgement: a credential under an unlisted name still ships."
echo "package: note: the path scan reads names only; it cannot see inside an archived file"
