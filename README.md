# SKA Checker

Cek SKK/SKA/SKT tenaga kerja dan SBU badan usaha konstruksi, lengkap dengan
penerbit dan masa berlaku. Live di `cek-sertifikat-keahlian.up.railway.app`.

## Sumber data

| Data | Sumber | Catatan |
|---|---|---|
| Tenaga kerja | cekskk.com | Pencarian lewat **nama saja** (sumber tidak lagi menyediakan NIK). |
| Badan usaha | ceksbu.com | Nama atau NIB. NPWP di profil sudah disamarkan oleh sumber. |
| Verifikasi resmi | lpjk.pu.go.id | Dilindungi captcha Cloudflare Turnstile, jadi tidak di-query server. Tombol "Verifikasi di LPJK" menyalin nama lalu membuka halaman resmi untuk dicek manual. |

Kedua sumber adalah agregator, bukan sumber resmi. Status sertifikat dihitung
dari tanggal "berlaku s/d" (habis / segera habis ≤ 90 hari / berlaku).

Hasil yang pernah dibuka disimpan ke SQLite di `DATA_DIR` (default `./data`).
NIK tidak disimpan. Folder `data/` ada di `.gitignore`; **jangan di-commit**.

## Jalankan lokal

```bash
py -3.13 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python server.py        # http://localhost:3000
```

## Deploy (Railway)

- `Procfile` memakai gunicorn 1 worker + 8 thread. Jangan tambah worker: status
  cek massal disimpan di memori proses.
- Supaya database tidak hilang tiap deploy: tambahkan **Volume** (mis. mount di
  `/data`) lalu set env `DATA_DIR=/data`.
- Env opsional: `SOURCE_MIN_INTERVAL` (jeda antar request ke sumber, default 0.7
  detik), `DETAIL_TTL_DAYS` (umur cache profil, default 7).

## Crawler (isi database dari cekskk)

Profil cekskk ber-ID urut (±675.000). Crawler jalan dari ID tertinggi ke bawah,
menyimpan posisi, dan bisa dilanjutkan.

```bash
.venv/Scripts/python crawl.py tk --maks 1000     # ambil 1.000 profil lalu berhenti
.venv/Scripts/python crawl.py tk                 # lanjut sampai habis / dihentikan (Ctrl+C)
.venv/Scripts/python crawl.py status
```

- Satu profil = 2 request. Dengan `--jeda 1.5` (default) sekitar 1.200 profil/jam,
  jadi seluruh database butuh ±23 hari jalan terus.
- Profil yang diperbarui < 30 hari dilewati (`--lewati-hari`).
- Berhenti otomatis kalau sumber menolak (403/429/challenge). Jangan akali,
  tunggu beberapa jam lalu lanjutkan dengan jeda lebih besar.
