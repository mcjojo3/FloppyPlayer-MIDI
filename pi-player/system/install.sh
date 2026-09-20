#!/usr/bin/env bash
# Pi setup for FloppyPlayer; safe to re-run. Run as your normal user, then reboot.
# Every step runs even if one fails; failures are listed at the end.
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEM_DIR="$APP_DIR/system"
USER_NAME="$(id -un)"
USER_ID="$(id -u)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILED=()

if [ "$USER_ID" -eq 0 ]; then
    echo "Run this as your normal user, not root - it uses sudo where needed." >&2
    exit 1
fi

step() {
    local name="$1"
    shift
    echo
    echo "== $name"
    if "$@"; then
        echo "   ok"
    else
        echo "   FAILED"
        FAILED+=("$name")
    fi
}

install_packages() {
    sudo apt-get update || return 1
    sudo apt-get install -y fluidsynth libfluidsynth3 python3-pygame python3-numpy python3-venv \
        bluez libspa-0.2-bluetooth fonts-noto-cjk
}

python_env() {
    if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
        python3 -m venv --system-site-packages "$APP_DIR/.venv" || return 1
    fi
    "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
}

add_groups() {
    local wanted=() group
    for group in dialout video input render audio bluetooth; do
        if getent group "$group" >/dev/null; then
            wanted+=("$group")
        else
            echo "   (no '$group' group on this system - skipped)"
        fi
    done
    sudo usermod -aG "$(IFS=,; echo "${wanted[*]}")" "$USER_NAME"
}

realtime_limits() {
    sudo tee /etc/security/limits.d/audio.conf >/dev/null <<'EOF'
@audio   -  rtprio      95
@audio   -  memlock     unlimited
EOF
}

backlight_access() {
    sudo mkdir -p /etc/udev/rules.d || return 1
    sudo tee /etc/udev/rules.d/99-floppyplayer-backlight.rules >/dev/null <<'EOF' || return 1
SUBSYSTEM=="backlight", ACTION=="add", RUN+="/bin/chgrp video /sys%p/brightness", RUN+="/bin/chmod g+w /sys%p/brightness"
EOF
    local brightness
    for brightness in /sys/class/backlight/*/brightness; do
        [ -e "$brightness" ] || continue
        sudo chgrp video "$brightness" || return 1
        sudo chmod g+w "$brightness" || return 1
    done
}

usb_automount() {
    # Read-only at /run/media/system/<label>; unmounted again when unplugged.
    sudo mkdir -p /etc/udev/rules.d || return 1
    sudo tee /etc/udev/rules.d/99-floppyplayer-usb.rules >/dev/null <<'EOF' || return 1
ACTION=="add", SUBSYSTEMS=="usb", SUBSYSTEM=="block", ENV{ID_FS_USAGE}=="filesystem", RUN{program}+="/usr/bin/systemd-mount --no-block --automount=no --collect --options=ro,nosuid,nodev,noexec $devnode"
EOF
    sudo udevadm control --reload
}

bluetooth_pairing() {
    # With no agent running BlueZ pairs without saving the keys, so speakers are forgotten at reboot.
    local conf=/etc/bluetooth/main.conf
    if [ ! -f "$conf" ]; then
        printf '[General]\nAlwaysPairable = true\n' | sudo tee "$conf" >/dev/null || return 1
    elif grep -qE '^[#[:space:]]*AlwaysPairable[[:space:]]*=' "$conf"; then
        sudo sed -i -E 's/^[#[:space:]]*AlwaysPairable[[:space:]]*=.*/AlwaysPairable = true/' "$conf" || return 1
    else
        sudo sed -i '/^\[General\]/a AlwaysPairable = true' "$conf" || return 1
    fi
    if ! grep -q '^AlwaysPairable = true' "$conf"; then
        echo "   Couldn't set AlwaysPairable in $conf"
        return 1
    fi
    sudo systemctl restart bluetooth
}

power_sudoers() {
    echo "$USER_NAME ALL=(root) NOPASSWD: /usr/sbin/shutdown, /usr/sbin/reboot, /sbin/shutdown, /sbin/reboot" \
        > "$TMP/sudoers"
    sudo visudo -cf "$TMP/sudoers" || return 1
    sudo install -m 440 "$TMP/sudoers" /etc/sudoers.d/floppyplayer
}

pipewire_eq() {
    local conf
    for conf in 10-quantum.conf 20-floppyplayer-eq.conf; do
        if [ ! -f "$SYSTEM_DIR/$conf" ]; then
            echo "   $SYSTEM_DIR/$conf is missing - copy the whole system/ folder to the Pi"
            return 1
        fi
    done
    mkdir -p ~/.config/pipewire/pipewire.conf.d || return 1
    cp "$SYSTEM_DIR/10-quantum.conf" "$SYSTEM_DIR/20-floppyplayer-eq.conf" \
        ~/.config/pipewire/pipewire.conf.d/ || return 1
    # Linger starts the user's PipeWire at boot, before the app.
    sudo loginctl enable-linger "$USER_NAME" || return 1
    systemctl --user restart pipewire pipewire-pulse wireplumber || return 1

    local eq_id="" _
    for _ in $(seq 1 20); do
        eq_id="$(pw-dump 2>/dev/null | python3 -c '
import json, sys
for obj in json.load(sys.stdin):
    props = (obj.get("info") or {}).get("props") or {}
    if props.get("node.name") == "effect_input.floppyplayer_eq":
        print(obj["id"])
' 2>/dev/null)"
        [ -n "$eq_id" ] && break
        sleep 0.5
    done
    if [ -z "$eq_id" ]; then
        echo "   The EQ sink didn't appear. PipeWire's reason:"
        journalctl --user -u pipewire -n 20 --no-pager
        return 1
    fi
    wpctl set-default "$eq_id" || return 1
    echo "   EQ sink is node $eq_id and is now the default output"
}

service() {
    if [ ! -s "$APP_DIR/floppyplayer.service" ]; then
        echo "   $APP_DIR/floppyplayer.service is missing or empty - copy the whole pi-player/ folder again"
        return 1
    fi
    sed -e "s|^User=.*|User=$USER_NAME|" \
        -e "s|/home/pi/FloppyPlayer-MIDI/pi-player|$APP_DIR|g" \
        -e "s|/run/user/1000|/run/user/$USER_ID|g" \
        "$APP_DIR/floppyplayer.service" > "$TMP/floppyplayer.service" || return 1
    # systemd treats an empty unit, or a link to /dev/null, as masked.
    sudo systemctl unmask floppyplayer >/dev/null 2>&1
    sudo systemctl unmask --runtime floppyplayer >/dev/null 2>&1
    sudo install -m 644 "$TMP/floppyplayer.service" /etc/systemd/system/floppyplayer.service || return 1
    sudo systemctl daemon-reload || return 1
    local state
    state="$(systemctl show -p LoadState --value floppyplayer)"
    if [ "$state" != "loaded" ]; then
        echo "   The service is '$state' after installing:"
        systemctl status floppyplayer --no-pager -l | head -5
        return 1
    fi
    sudo systemctl enable floppyplayer
}

step "Packages" install_packages
step "Python environment" python_env
step "Groups" add_groups
step "Real-time audio limits" realtime_limits
step "Backlight access (screen dimming)" backlight_access
step "USB drive automount" usb_automount
step "Bluetooth pairings kept across reboots" bluetooth_pairing
step "Passwordless shutdown/reboot" power_sudoers
step "PipeWire buffer + EQ" pipewire_eq
step "Service" service

echo
if [ ${#FAILED[@]} -eq 0 ]; then
    echo "All steps done. Reboot to apply everything:  sudo reboot"
else
    echo "These steps FAILED (details above each one):"
    printf '  - %s\n' "${FAILED[@]}"
    exit 1
fi
