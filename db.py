"""Database lokal (SQLite) hasil pengambilan dari cekskk.com / ceksbu.com.

File database ada di DATA_DIR (default ./data) dan TIDAK ikut ke git.
Di Railway, pasang Volume lalu set DATA_DIR ke mount path-nya supaya data
tidak hilang tiap deploy. NIK tidak disimpan.
"""
import json
import os
import re
import sqlite3
import threading
from contextlib import closing

from scraper import nama_bu_inti, nama_inti, sekarang_iso

DATA_DIR = os.environ.get('DATA_DIR') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
DB_PATH = os.path.join(DATA_DIR, 'ska.db')

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenaga_kerja (
    id               INTEGER PRIMARY KEY,
    nama             TEXT NOT NULL,
    nama_norm        TEXT NOT NULL,
    lokasi           TEXT,
    pendidikan       TEXT,
    bidang           TEXT,
    total_sertifikat INTEGER,
    sertifikat_aktif INTEGER,
    sertifikat_json  TEXT,
    detail_lengkap   INTEGER NOT NULL DEFAULT 0,
    diperbarui       TEXT
);
CREATE INDEX IF NOT EXISTS idx_tk_nama ON tenaga_kerja(nama_norm);

CREATE TABLE IF NOT EXISTS badan_usaha (
    npwp            TEXT PRIMARY KEY,
    path            TEXT,
    nama            TEXT NOT NULL,
    nama_norm       TEXT NOT NULL,
    lokasi          TEXT,
    alamat          TEXT,
    npwp_tampil     TEXT,
    sbu_json        TEXT,
    detail_lengkap  INTEGER NOT NULL DEFAULT 0,
    diperbarui      TEXT
);
CREATE INDEX IF NOT EXISTS idx_bu_nama ON badan_usaha(nama_norm);

CREATE TABLE IF NOT EXISTS crawl_state (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

_init_lock = threading.Lock()
_ready = False


def connect():
    global _ready
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
    if not _ready:
        with _init_lock:
            if not _ready:
                con.executescript(SCHEMA)
                _ready = True
    return con


def _run(fn):
    with closing(connect()) as con:
        with con:
            return fn(con)


# ------------------------------------------------------------------
# Tenaga kerja
# ------------------------------------------------------------------

def simpan_tk_ringkas(hasil):
    """Simpan baris hasil pencarian tanpa menimpa detail yang sudah ada."""
    rows = [(h['id'], h['nama'], nama_inti(h['nama']), h.get('lokasi') or '') for h in hasil]
    if not rows:
        return
    _run(lambda con: con.executemany(
        """INSERT INTO tenaga_kerja (id, nama, nama_norm, lokasi) VALUES (?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET nama=excluded.nama, nama_norm=excluded.nama_norm,
           lokasi=COALESCE(NULLIF(excluded.lokasi, ''), tenaga_kerja.lokasi)""", rows))


def simpan_tk(p):
    _run(lambda con: con.execute(
        """INSERT INTO tenaga_kerja (id, nama, nama_norm, lokasi, pendidikan, bidang, total_sertifikat,
               sertifikat_aktif, sertifikat_json, detail_lengkap, diperbarui)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET nama=excluded.nama, nama_norm=excluded.nama_norm,
               lokasi=excluded.lokasi, pendidikan=excluded.pendidikan, bidang=excluded.bidang,
               total_sertifikat=excluded.total_sertifikat, sertifikat_aktif=excluded.sertifikat_aktif,
               sertifikat_json=excluded.sertifikat_json, detail_lengkap=excluded.detail_lengkap,
               diperbarui=excluded.diperbarui""",
        (p['id'], p['nama'], nama_inti(p['nama']), p.get('lokasi'), p.get('pendidikan'), p.get('bidang'),
         p.get('total_sertifikat'), p.get('sertifikat_aktif'),
         json.dumps(p.get('sertifikat') or [], ensure_ascii=False),
         1 if p.get('detail_lengkap') else 0, p.get('diperbarui') or sekarang_iso())))


def _tk_row(row):
    if row is None:
        return None
    d = dict(row)
    d['sertifikat'] = json.loads(d.pop('sertifikat_json') or '[]')
    d['detail_lengkap'] = bool(d['detail_lengkap'])
    d.pop('nama_norm', None)
    return d


def ambil_tk(tk_id):
    return _run(lambda con: _tk_row(con.execute(
        """SELECT id, nama, nama_norm, lokasi, pendidikan, bidang, total_sertifikat, sertifikat_aktif,
                  sertifikat_json, detail_lengkap, diperbarui
           FROM tenaga_kerja WHERE id = ?""", (int(tk_id),)).fetchone()))


def cari_tk(q, limit=50):
    kunci = nama_inti(q)
    if not kunci:
        return []
    return _run(lambda con: [dict(r) for r in con.execute(
        """SELECT id, nama, lokasi FROM tenaga_kerja
           WHERE nama_norm LIKE ? ORDER BY (nama_norm = ?) DESC, nama_norm LIMIT ?""",
        (f'%{kunci}%', kunci, int(limit))).fetchall()])


# ------------------------------------------------------------------
# Badan usaha
# ------------------------------------------------------------------

def simpan_bu_ringkas(hasil):
    rows = [(h['npwp'], h['path'], h["nama"], nama_bu_inti(h["nama"]), h.get('lokasi') or '') for h in hasil]
    if not rows:
        return
    _run(lambda con: con.executemany(
        """INSERT INTO badan_usaha (npwp, path, nama, nama_norm, lokasi) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(npwp) DO UPDATE SET path=excluded.path, nama=excluded.nama,
           nama_norm=excluded.nama_norm,
           lokasi=COALESCE(NULLIF(excluded.lokasi, ''), badan_usaha.lokasi)""", rows))


def simpan_bu(b):
    _run(lambda con: con.execute(
        """INSERT INTO badan_usaha (npwp, path, nama, nama_norm, alamat, npwp_tampil, sbu_json,
               detail_lengkap, diperbarui)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(npwp) DO UPDATE SET path=excluded.path, nama=excluded.nama,
               nama_norm=excluded.nama_norm, alamat=excluded.alamat, npwp_tampil=excluded.npwp_tampil,
               sbu_json=excluded.sbu_json, detail_lengkap=excluded.detail_lengkap,
               diperbarui=excluded.diperbarui""",
        (b['npwp'], b.get('path'), b["nama"], nama_bu_inti(b["nama"]), b.get('alamat'), b.get('npwp_tampil'),
         json.dumps(b.get('sbu') or [], ensure_ascii=False), 1 if b.get('detail_lengkap') else 0,
         b.get('diperbarui') or sekarang_iso())))


def _bu_row(row):
    if row is None:
        return None
    d = dict(row)
    d['sbu'] = json.loads(d.pop('sbu_json') or '[]')
    d['detail_lengkap'] = bool(d['detail_lengkap'])
    d.pop('nama_norm', None)
    return d


def ambil_bu(npwp):
    return _run(lambda con: _bu_row(con.execute(
        """SELECT npwp, path, nama, nama_norm, lokasi, alamat, npwp_tampil, sbu_json, detail_lengkap,
                  diperbarui
           FROM badan_usaha WHERE npwp = ?""", (npwp,)).fetchone()))


def cari_bu(q, limit=50):
    kunci = nama_bu_inti(q)
    digit = re.sub(r'\D', '', q or '')
    if not kunci and not digit:
        return []
    return _run(lambda con: [dict(r) for r in con.execute(
        """SELECT npwp, path, nama, lokasi FROM badan_usaha
           WHERE (? != '' AND nama_norm LIKE ?) OR (? != '' AND npwp = ?)
           ORDER BY (nama_norm = ?) DESC, nama_norm LIMIT ?""",
        (kunci, f'%{kunci}%', digit, digit, kunci, int(limit))).fetchall()])


# ------------------------------------------------------------------
# Lain-lain
# ------------------------------------------------------------------

def statistik():
    def q(con):
        tk = con.execute('SELECT COUNT(*), SUM(detail_lengkap) FROM tenaga_kerja').fetchone()
        bu = con.execute('SELECT COUNT(*), SUM(detail_lengkap) FROM badan_usaha').fetchone()
        return {'tenaga_kerja': tk[0] or 0, 'tenaga_kerja_detail': tk[1] or 0,
                'badan_usaha': bu[0] or 0, 'badan_usaha_detail': bu[1] or 0}
    return _run(q)


def state_get(k, default=None):
    row = _run(lambda con: con.execute('SELECT v FROM crawl_state WHERE k = ?', (k,)).fetchone())
    return row[0] if row else default


def state_set(k, v):
    _run(lambda con: con.execute(
        'INSERT INTO crawl_state (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, str(v))))
