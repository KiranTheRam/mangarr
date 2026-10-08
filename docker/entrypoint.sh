#!/bin/sh
# Optional PUID / PGID / UMASK handling for the Mangarr image.
#
# With none of them set, the command runs exactly as it always has: as root,
# with the default umask. UMASK on its own applies even when running as root.
# With PUID and PGID set, the data directory (MANGARR_DATA_DIR) is made
# theirs and the command runs as that user and group. Library and download
# folders are never touched here; see the README for switching an existing
# install.
set -eu

fail() {
    echo "mangarr-entrypoint: $*" >&2
    exit 1
}

PUID="${PUID:-}"
PGID="${PGID:-}"
UMASK="${UMASK:-}"

if [ -n "$UMASK" ]; then
    case "$UMASK" in
        *[!0-7]*) fail "UMASK must be an octal value such as 022 or 002, got '$UMASK'" ;;
    esac
    [ "${#UMASK}" -le 4 ] || fail "UMASK must be an octal value such as 022 or 002, got '$UMASK'"
    umask "$UMASK"
fi

if [ -z "$PUID" ] && [ -z "$PGID" ]; then
    exec "$@"
fi

[ -n "$PUID" ] && [ -n "$PGID" ] || fail "set both PUID and PGID (e.g. PUID=99 PGID=100), or neither"
for id in "$PUID" "$PGID"; do
    case "$id" in
        *[!0-9]*) fail "PUID and PGID must be numeric ids, got PUID='$PUID' PGID='$PGID'" ;;
    esac
done
[ "$(id -u)" = 0 ] || fail "PUID/PGID need the container to start as root (remove --user / user:)"

# Reuse a group or user that already has these ids (gid 100 is "users");
# otherwise add one named mangarr. The -K overrides only silence useradd's
# warning for ids outside login.defs' range, such as unRAID's 99.
if ! getent group "$PGID" >/dev/null; then
    groupadd -g "$PGID" mangarr
fi
if ! getent passwd "$PUID" >/dev/null; then
    useradd -K UID_MIN=0 -K UID_MAX=4294967294 -u "$PUID" -g "$PGID" \
        -M -d /nonexistent -s /usr/sbin/nologin mangarr
fi

data_dir="${MANGARR_DATA_DIR:-data}"
[ "$data_dir" != / ] || fail "refusing to change ownership of / (MANGARR_DATA_DIR)"
mkdir -p "$data_dir"
if [ -n "$(find "$data_dir" \( ! -user "$PUID" -o ! -group "$PGID" \) -print -quit)" ]; then
    chown -R "$PUID:$PGID" "$data_dir" ||
        echo "mangarr-entrypoint: warning: could not change ownership of $data_dir; it must be writable by $PUID:$PGID" >&2
fi

echo "mangarr-entrypoint: running as $PUID:$PGID, umask $(umask)" >&2
exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
