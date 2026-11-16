"""Crawler database tenaga kerja cekskk.com ke database lokal.

Profil cekskk ber-ID urut (1 .. ±675.000). Crawler jalan dari ID tertinggi
turun ke bawah, menyimpan posisi terakhir, dan bisa dilanjutkan kapan saja.

Contoh:
    python crawl.py tk                          # lanjut dari posisi terakhir
    python crawl.py tk --dari 674551 --maks 500 # mulai dari ID tertentu
    python crawl.py tk --jeda 2 --lewati-hari 30
    python crawl.py status

Berhenti otomatis kalau sumber menolak akses (403/429/challenge). Jangan
turunkan --jeda di bawah 1 detik: satu profil = 2 request.
"""
import argparse
import re
import signal
import sys
import time
from datetime import datetime, timedelta

import db
import scraper

STATE_KEY = 'tk_cursor'


def id_tertinggi():
    xml = scraper._get(f'{scraper.CEKSKK}/sitemap/profile_page/1.xml')
    ids = [int(x) for x in re.findall(r'/tenaga-kerja-konstruksi/(\d+)', xml)]
    if not ids:
        raise scraper.SourceError('sitemap profil kosong')
    return max(ids)


def masih_baru(row, hari):
    if not row or not row.get('detail_lengkap') or not row.get('diperbarui'):
        return False
    try:
        t = datetime.fromisoformat(row['diperbarui'])
    except ValueError:
        return False
    return datetime.now(t.tzinfo) - t < timedelta(days=hari)


def crawl_tk(args):
    scraper.MIN_INTERVAL = max(1.0, args.jeda)

    mulai = args.dari
    if mulai is None:
        simpan = db.state_get(STATE_KEY)
        mulai = int(simpan) if simpan else id_tertinggi()
    akhir = max(1, args.sampai)

    print(f'Mulai dari ID {mulai} turun ke {akhir}. Jeda {scraper.MIN_INTERVAL}s/request. Ctrl+C untuk berhenti.')
    cur = mulai
    diproses = tersimpan = kosong = dilewati = 0
    gagal_beruntun = 0
    t0 = time.time()

    def simpan_posisi(*_):
        db.state_set(STATE_KEY, cur)
        print(f'\nPosisi disimpan di ID {cur}. Tersimpan {tersimpan}, kosong {kosong}, dilewati {dilewati}.')
        sys.exit(0)

    signal.signal(signal.SIGINT, simpan_posisi)

    while cur >= akhir and (args.maks is None or diproses < args.maks):
        if masih_baru(db.ambil_tk(cur), args.lewati_hari):
            dilewati += 1
            cur -= 1
            continue
        try:
            p = scraper.get_tk(cur)
            gagal_beruntun = 0
        except scraper.SourceBlocked as e:
            db.state_set(STATE_KEY, cur)
            print(f'\nBERHENTI: {e}. Posisi disimpan di ID {cur}. Coba lagi beberapa jam lagi dengan jeda lebih besar.')
            return 2
        except scraper.SourceError as e:
            gagal_beruntun += 1
            print(f'  ID {cur}: gagal ({e})')
            if gagal_beruntun >= 5:
                db.state_set(STATE_KEY, cur)
                print('\nBERHENTI: 5 kegagalan beruntun. Posisi disimpan.')
                return 3
            time.sleep(5 * gagal_beruntun)
            continue

        diproses += 1
        if p:
            p['diperbarui'] = scraper.sekarang_iso()
            db.simpan_tk(p)
            tersimpan += 1
        else:
            kosong += 1

        cur -= 1
        if diproses % 10 == 0:
            db.state_set(STATE_KEY, cur)
        if diproses % 25 == 0:
            laju = diproses / max(1.0, time.time() - t0)
            print(f'  ID {cur + 1}: {tersimpan} tersimpan, {kosong} kosong, {dilewati} dilewati · {laju * 3600:.0f} profil/jam')

    db.state_set(STATE_KEY, cur)
    print(f'Selesai. Tersimpan {tersimpan}, kosong {kosong}, dilewati {dilewati}. Posisi berikutnya: ID {cur}.')
    return 0


def status(_args):
    s = db.statistik()
    print(f"Database: {db.DB_PATH}")
    print(f"Tenaga kerja : {s['tenaga_kerja']:,} ({s['tenaga_kerja_detail']:,} lengkap)")
    print(f"Badan usaha  : {s['badan_usaha']:,} ({s['badan_usaha_detail']:,} lengkap)")
    print(f"Posisi crawler: ID {db.state_get(STATE_KEY, '-')}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    tk = sub.add_parser('tk', help='crawl profil tenaga kerja cekskk.com')
    tk.add_argument('--dari', type=int, help='ID awal (default: posisi terakhir / ID tertinggi)')
    tk.add_argument('--sampai', type=int, default=1, help='ID akhir (default 1)')
    tk.add_argument('--maks', type=int, help='batas jumlah profil yang diambil di sesi ini')
    tk.add_argument('--jeda', type=float, default=1.5, help='detik antar request (min 1)')
    tk.add_argument('--lewati-hari', type=int, default=30, help='lewati profil yang diperbarui < N hari')
    tk.set_defaults(fn=crawl_tk)

    st = sub.add_parser('status', help='ringkasan isi database')
    st.set_defaults(fn=status)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == '__main__':
    main()
