#!/bin/sh
# Autostart logging: plugging in the OBD adapter starts `obd2log log --auto`,
# unplugging it stops the service. Installs a systemd user unit and a udev rule
# (the rule needs sudo). Run with the adapter plugged in, or pass its
# /dev/serial/by-id/... path.
set -e
dev=${1:-$(ls /dev/serial/by-id/* 2>/dev/null | head -1)}
[ -n "$dev" ] || { echo "plug in the adapter or pass its /dev/serial/by-id path"; exit 1; }
serial=$(udevadm info -q property -n "$dev" | sed -n 's/^ID_SERIAL=//p')
unit=$(systemd-escape --path "$dev").device
bin=$(command -v obd2log) || { echo "obd2log not on PATH"; exit 1; }
echo "adapter: $dev (ID_SERIAL=$serial)"

mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/obd2log-autolog.service <<UNIT
[Unit]
Description=obd2log: log every drive while the OBD adapter is plugged in
BindsTo=$unit
After=$unit

[Service]
ExecStart=$bin --port $dev log --auto
KillSignal=SIGINT
Restart=on-failure
RestartSec=10
UNIT
systemctl --user daemon-reload

rule=/etc/udev/rules.d/99-obd2log.rules
echo "ACTION==\"add\", SUBSYSTEM==\"tty\", ENV{ID_SERIAL}==\"$serial\", TAG+=\"systemd\", ENV{SYSTEMD_USER_WANTS}+=\"obd2log-autolog.service\"" \
  | sudo tee "$rule" >/dev/null
sudo udevadm control --reload
echo "installed. Replug the adapter; follow with: journalctl --user -fu obd2log-autolog"
