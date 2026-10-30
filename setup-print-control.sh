#!/bin/bash
# Sekali saja: izinkan dashboard (user ngemis) menjeda / melanjutkan / membatalkan cetak
# tanpa password. Jalankan: sudo bash ~/server-monitor/setup-print-control.sh
set -e
[ "$(id -u)" = 0 ] || { echo "Jalankan pakai sudo"; exit 1; }

F=/etc/sudoers.d/print-dashboard
cat > "$F" <<'EOF'
# Dashboard server-monitor: kontrol antrian cetak (pause/resume/cancel)
Defaults!/usr/sbin/cupsenable, /usr/sbin/cupsdisable, /usr/bin/cancel !use_pty
ngemis ALL=(root) NOPASSWD: /usr/sbin/cupsenable, /usr/sbin/cupsdisable, /usr/bin/cancel
EOF
chmod 440 "$F"
if ! visudo -cf "$F"; then rm -f "$F"; echo "GAGAL: file sudoers tidak valid, sudah dihapus."; exit 1; fi
echo "OK: $F dibuat."
echo "Uji:"; sudo -u ngemis sudo -n /usr/sbin/cupsdisable -r test EpsonL120-PS && \
  sudo -u ngemis sudo -n /usr/sbin/cupsenable EpsonL120-PS && echo "Kontrol antrian berfungsi."
