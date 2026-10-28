#!/bin/sh
# Builds libtailscale, the library NWR Stream Manager uses to join a tailnet itself.
#
# It's optional: without it, NWR Stream Manager works as before and its Tailscale settings
# say the library isn't installed. Distributions don't package it, so it's built here, at
# the version NWR Stream Manager was tested with, with one change (user-log.patch) so that
# everything Tailscale logs goes to NWR Stream Manager's log rather than to the terminal.
# nwr-stream-manager --build-libtailscale runs this.
#
# Needs git, a C compiler and Go. libtailscale asks for a newer Go than some distributions
# have; Go downloads that version itself unless GOTOOLCHAIN=local is set.
#
# Usage: build.sh [output directory]
# The default is where NWR Stream Manager finds it: /usr/local/lib/nwr-stream-manager when
# run as root, for every user including the systemd service, and otherwise
# ~/.local/lib/nwr-stream-manager, for this user only. Neither is touched by reinstalling or
# upgrading NWR Stream Manager. A package installs it as /usr/lib/nwr-stream-manager/libtailscale.so.
set -eu

LIBTAILSCALE_REPOSITORY=https://github.com/tailscale/libtailscale.git
LIBTAILSCALE_COMMIT=d1dc45d039edb7759ecbee6ba2aea0c4bca884b8

here=$(cd "$(dirname "$0")" && pwd)
if [ "$(id -u)" -eq 0 ]; then
	default_output=/usr/local/lib/nwr-stream-manager
else
	default_output="$HOME/.local/lib/nwr-stream-manager"
fi
output=${1:-"$default_output"}
mkdir -p "$output"
output=$(cd "$output" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

git -C "$work" init -q source
git -C "$work/source" fetch -q --depth 1 "$LIBTAILSCALE_REPOSITORY" "$LIBTAILSCALE_COMMIT"
git -C "$work/source" checkout -q FETCH_HEAD
git -C "$work/source" apply "$here/user-log.patch"
cd "$work/source"
CGO_ENABLED=1 GOTOOLCHAIN=${GOTOOLCHAIN:-auto} go build -buildmode=c-shared -trimpath -ldflags="-s -w" -o "$output/libtailscale.so" .
rm -f "$output/libtailscale.h"
echo "Built $output/libtailscale.so"
