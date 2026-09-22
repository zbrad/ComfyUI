#!/usr/bin/env bash
# check_enabled.sh [unit...] — warn when a user unit would not come back after
# a reboot. Advisory only: it always exits 0 and never blocks anything.
#
# Run from the units themselves as ExecStartPre (so the state is visible in the
# journal at every start) and from install.sh. install.sh renders the units but
# deliberately never enables them, so a node can run ComfyUI happily until its
# first reboot and then come up without it. This is the reminder.
#
# Deliberately reads the filesystem markers rather than calling `systemctl` or
# `loginctl`: this runs inside ExecStartPre while the manager is mid-start, and
# a read-only file test cannot deadlock against it or depend on dbus.
set -uo pipefail

UNITS=("$@")
if [ "${#UNITS[@]}" -eq 0 ]; then
    UNITS=(comfyui.service comfyui-resource-watch.service)
fi

# `systemctl --user enable` creates this symlink; that is the whole mechanism.
WANTS_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/default.target.wants"
LINGER_MARKER="/var/lib/systemd/linger/${USER:-$(id -un)}"

not_enabled=()
for unit in "${UNITS[@]}"; do
    [ -L "${WANTS_DIR}/${unit}" ] || not_enabled+=("${unit}")
done

if [ "${#not_enabled[@]}" -gt 0 ]; then
    echo "WARNING: not enabled at boot: ${not_enabled[*]}" >&2
    echo "         systemctl --user enable ${not_enabled[*]}" >&2
fi

# An enabled unit still will not start at boot without linger for this user.
if [ ! -e "${LINGER_MARKER}" ]; then
    echo "WARNING: linger is off for ${USER:-$(id -un)}, so user units do not" >&2
    echo "         survive logout or reboot: sudo loginctl enable-linger ${USER:-$(id -un)}" >&2
fi

if [ "${#not_enabled[@]}" -eq 0 ] && [ -e "${LINGER_MARKER}" ]; then
    echo "enabled at boot: ${UNITS[*]} (linger on)"
fi

exit 0
