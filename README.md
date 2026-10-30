# Server Monitor

> **Panduan instalasi lengkap (urutan 4 repo):** [`PANDUAN-INSTALASI.md`](https://github.com/fikrudzulfahmi/ngemis-ai-infra/blob/main/docs/PANDUAN-INSTALASI.md)
Dashboard monitoring server: CPU per-core, RAM, suhu, jaringan, disk, proses teratas,
speedtest, dan panel antrian printer CUPS. Python standard library saja (tanpa pip),
satu file `monitor.py` + satu halaman `index.html`.

## Fitur
- Metrik realtime (poll 2 s): CPU total + per core, load, RAM/swap, suhu hwmon/thermal, IP, RX/TX.
- Storage semua mount `/dev/*` (termasuk HDD rekaman) + sparkline CPU/RAM 5 menit.
- Proses teratas by CPU dan by RSS.
- Panel printer: status antrian, riwayat cetak, jeda/lanjut/batalkan job.
- Speedtest on-demand (Ookla CLI, opsional).
- PWA installable (`manifest.json`, `sw.js`, ikon).
- Endpoint widget Android: `/api/widget?token=…` dan `/api/widget.txt?token=…`.
- Tombol shutdown berpassword + hitung mundur (**nonaktif default**, lihat di bawah).

## Jalankan dengan Docker
```bash
git clone https://github.com/fikrudzulfahmi/server-monitor.git && cd server-monitor
mkdir -p data && cp .env.example data/.env && nano data/.env
docker compose up -d --build
```
Panel: `http://<ip-server>:8090`

Container ini butuh akses metrik host: `pid: host`, mount `/proc`, `/sys`, `/` (read-only).
Ukuran disk dibaca lewat `FS_ROOT_PREFIX=/host/root`, jadi angka yang tampil adalah disk host.

### Tanpa Docker
```bash
cp .env.example .env && nano .env
python3 monitor.py
# izin kontrol antrian cetak (sekali saja):
sudo bash setup-print-control.sh
```

## Variabel lingkungan
| Variabel | Default | Keterangan |
|---|---|---|
| `MON_HOST` / `MON_PORT` | `127.0.0.1` / `8090` | bind address |
| `MON_USER` / `MON_PASS` | – | akun login panel (kosong = tanpa login) |
| `MON_WIDGET_TOKEN` | – | token statis untuk widget (wajib diisi agar endpoint widget hidup) |
| `MON_POWER_ENABLED` | `0` | `1` = tampilkan tombol shutdown |
| `MON_POWER_PASS` | = `MON_PASS` | password konfirmasi shutdown |
| `MON_POWER_DELAY` | `30` | jeda sebelum eksekusi (detik), bisa dibatalkan |
| `PROC_ROOT` / `SYS_ROOT` | `/proc` / `/sys` | root metrik (Docker: `/host/proc`, `/host/sys`) |
| `FS_ROOT_PREFIX` | – | prefix `statvfs` (Docker: `/host/root`) |
| `MOUNTS_FILE` | `$PROC_ROOT/mounts` | tabel mount (Docker: `/host/proc/1/mounts` — `/host/proc/mounts` justru menampilkan mount container) |
| `CUPS_SERVER` | – | alamat CUPS, mis. `127.0.0.1:631` |
| `CUPS_ACCESS_LOG` | `/var/log/cups/access_log` | sumber riwayat cetak |
| `CUPS_ADMIN_SUDO` | `auto` | `auto` (coba langsung lalu sudo) / `never` / `always` |

## Tombol shutdown
Dinonaktifkan secara default. Untuk mengaktifkan di host (bukan Docker):
```
MON_POWER_ENABLED=1
```
Butuh izin `sudo -n /usr/bin/systemctl poweroff` (lihat `systemd/` di repo `ngemis-ai-infra`).
Di Docker tombol ini sengaja dimatikan: `systemctl` di dalam container tidak bisa mematikan host.

## Keamanan
- Sesi cookie `HttpOnly; SameSite=Strict`, token 12 jam, `Secure` otomatis di belakang HTTPS.
- Blokir IP 15 menit setelah 5 percobaan login gagal.
- Semua aksi printer divalidasi regex nama printer/job.
