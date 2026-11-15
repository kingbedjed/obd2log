#!/bin/sh
systemctl --user stop obd2log-autolog.service 2>/dev/null || true
rm -f ~/.config/systemd/user/obd2log-autolog.service
systemctl --user daemon-reload
sudo rm -f /etc/udev/rules.d/99-obd2log.rules && sudo udevadm control --reload
echo removed
