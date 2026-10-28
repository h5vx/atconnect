#!/bin/sh
# Lay out the install tree: stage.sh DESTDIR  ->  DESTDIR/usr/{bin,lib/atconnect,share/...}
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
dest=${1:?usage: stage.sh DESTDIR}
usr="$dest/usr"
rm -rf "$dest"
install -d "$usr/bin" "$usr/lib/atconnect/atconnect" "$usr/share/applications" \
    "$usr/share/doc/atconnect" "$usr/share/licenses/atconnect"
install -m 644 "$root"/atconnect/*.py "$usr/lib/atconnect/atconnect/"
install -m 755 "$root/packaging/atconnect" "$root/packaging/atconnect-gtk" "$usr/bin/"
install -m 644 "$root/data/io.github.atconnect.desktop" "$usr/share/applications/"
install -m 644 "$root/README.md" "$root/PROTOCOL.md" "$usr/share/doc/atconnect/"
install -m 644 "$root/LICENSE" "$usr/share/licenses/atconnect/"
