"""SKA Checker: verifikasi SKK/SKA/SKT tenaga kerja dan SBU badan usaha konstruksi.

Sumber: cekskk.com dan ceksbu.com (agregator data LPJK). Hasil disimpan ke
database lokal (lihat db.py). Untuk bukti audit, cocokkan ke lpjk.pu.go.id.
"""
import csv
import io
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from datetime import date, datetime, timedelta

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.middleware.proxy_fix import ProxyFix

import db
import scraper
from scraper import SourceBlocked, SourceError, nama_bu_inti, nama_inti, status_sertifikat

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('ska')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, template_folder='templates', static_folder='static')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024
app.json.ensure_ascii = False
app.json.sort_keys = False

DETAIL_TTL = timedelta(days=int(os.environ.get('DETAIL_TTL_DAYS', '7')))
SEARCH_TTL = 600
MASSAL_MAKS = 200
JOB_TTL = 3 * 3600
JOB_JALAN_MAKS = 2

PESAN = {
    'blocked': 'Sumber data sedang membatasi akses. Coba lagi beberapa menit lagi.',
    'source': 'Sumber data gagal dimuat. Coba lagi nanti.',
    'limit': 'Terlalu banyak permintaan. Tunggu sebentar lalu coba lagi.',
}


def _asset_version():
    try:
        return str(int(max(os.path.getmtime(os.path.join(BASE_DIR, 'static', f))
                           for f in ('app.css', 'app.js'))))
    except OSError:
        return '1'


ASSET_V = _asset_version()


class BadInput(Exception):
    pass


@app.errorhandler(BadInput)
def _bad_input(e):
    return jsonify({'error': str(e)}), 400


@app.errorhandler(413)
def _too_large(_e):
    return jsonify({'error': 'File terlalu besar (maks. 2 MB).'}), 413


@app.errorhandler(500)
def _internal(_e):
    return jsonify({'error': 'Terjadi kesalahan di server.'}), 500


def _source_error(e):
    log.warning('sumber gagal: %s', e)
    if isinstance(e, SourceBlocked):
        return jsonify({'error': PESAN['blocked']}), 503
    return jsonify({'error': PESAN['source']}), 502


@app.after_request
def _headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    if request.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp


# ------------------------------------------------------------------
# Batas laju per IP + cache pencarian
# ------------------------------------------------------------------

_hits, _hits_lock = {}, threading.Lock()


def terlalu_sering(bucket, limit, window):
    key = (bucket, request.remote_addr or '?')
    now = time.monotonic()
    with _hits_lock:
        q = _hits.setdefault(key, deque())
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        if len(_hits) > 5000:
            for k in [k for k, v in _hits.items() if not v]:
                del _hits[k]
    return False


_cache, _cache_lock = OrderedDict(), threading.Lock()


def cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if not item or time.monotonic() - item[0] > SEARCH_TTL:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return item[1]


def cache_set(key, value):
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
        _cache.move_to_end(key)
        while len(_cache) > 300:
            _cache.popitem(last=False)


def ambil_q(minimal=3, maksimal=80):
    q = ''.join(ch for ch in request.args.get('q', '') if ch.isprintable())
    q = re.sub(r'\s+', ' ', q).strip()
    if len(q) < minimal:
        raise BadInput('Ketik minimal tiga karakter.')
    if len(q) > maksimal:
        raise BadInput('Kata kunci terlalu panjang (maks. delapan puluh karakter).')
    return q


# ------------------------------------------------------------------
# Penyusunan respons
# ------------------------------------------------------------------

URUT_STATUS = {'berlaku': 0, 'segera': 1, 'tidak_diketahui': 2, 'habis': 3}


def _dengan_status(items, hari_ini):
    out = []
    for c in items or []:
        c = dict(c)
        c['status'] = status_sertifikat(c.get('berlaku_sampai'), c.get('keterangan', ''), hari_ini)
        c['sisa_hari'] = ((date.fromisoformat(c['berlaku_sampai']) - hari_ini).days
                          if c.get('berlaku_sampai') else None)
        out.append(c)
    # Urut: berlaku > segera habis > tidak diketahui > habis; di tiap kelompok, masa berlaku terpanjang dulu.
    out.sort(key=lambda c: (URUT_STATUS[c['status']], -(c['sisa_hari'] if c['sisa_hari'] is not None else -10**6)))
    return out


def _ringkasan(items):
    r = {'total': len(items), 'berlaku': 0, 'segera': 0, 'habis': 0, 'tidak_diketahui': 0}
    for c in items:
        r[c['status']] += 1
    return r


def susun_tk(p, sumber, peringatan=None):
    certs = _dengan_status(p.get('sertifikat'), date.today())
    return {
        'id': p['id'],
        'nama': p['nama'],
        'lokasi': p.get('lokasi') or '',
        'pendidikan': p.get('pendidikan') or '',
        'total_sumber': p.get('total_sertifikat'),
        'aktif_sumber': p.get('sertifikat_aktif'),
        'sertifikat': certs,
        'ringkasan': _ringkasan(certs),
        'detail_lengkap': bool(p.get('detail_lengkap')),
        'diperbarui': p.get('diperbarui'),
        'sumber': sumber,
        'peringatan': peringatan,
        'url_resmi': scraper.LPJK_TKK,
    }


def susun_bu(b, sumber, peringatan=None):
    sbu = _dengan_status(b.get('sbu'), date.today())
    return {
        'npwp': b['npwp'],
        'nama': b['nama'],
        'alamat': b.get('alamat') or '',
        'lokasi': b.get('lokasi') or '',
        'npwp_tampil': b.get('npwp_tampil') or '',
        'sbu': sbu,
        'ringkasan': _ringkasan(sbu),
        'diperbarui': b.get('diperbarui'),
        'sumber': sumber,
        'peringatan': peringatan,
        'url_resmi': scraper.LPJK_BU,
    }


def masih_segar(row):
    if not row or not row.get('detail_lengkap') or not row.get('diperbarui'):
        return False
    try:
        t = datetime.fromisoformat(row['diperbarui'])
    except ValueError:
        return False
    return datetime.now(t.tzinfo) - t < DETAIL_TTL


# ------------------------------------------------------------------
# Halaman
# ------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html', v=ASSET_V)


@app.route('/healthz')
def healthz():
    return 'ok'


@app.route('/favicon.ico')
def favicon():
    return app.send_static_file('favicon.svg')


@app.route('/api/statistik')
def api_statistik():
    return jsonify(db.statistik())


# ------------------------------------------------------------------
# Tenaga kerja
# ------------------------------------------------------------------

@app.route('/api/tk/cari')
def api_tk_cari():
    q = ambil_q()
    key = ('tk', nama_inti(q))
    cached = cache_get(key)
    if cached:
        return jsonify(cached)
    if terlalu_sering('cari', 40, 60):
        return jsonify({'error': PESAN['limit']}), 429
    try:
        r = scraper.search_tk(q)
    except SourceError as e:
        lokal = db.cari_tk(q)
        if lokal:
            log.warning('cari tk pakai database lokal: %s', e)
            return jsonify({'hasil': lokal, 'total': len(lokal), 'terpotong': len(lokal) >= 50,
                            'sumber': 'lokal',
                            'peringatan': 'Sumber sedang tidak bisa dijangkau. Hasil di bawah dari database lokal.'})
        return _source_error(e)
    db.simpan_tk_ringkas(r['hasil'])
    payload = {**r, 'q': q, 'sumber': 'agregator'}
    cache_set(key, payload)
    return jsonify(payload)


@app.route('/api/tk/profil/<int:tk_id>')
def api_tk_profil(tk_id):
    if not 1 <= tk_id <= 99_999_999:
        raise BadInput('ID tidak valid.')
    row = db.ambil_tk(tk_id)
    segarkan = request.args.get('segarkan') == '1'
    if masih_segar(row) and not segarkan:
        return jsonify(susun_tk(row, 'lokal'))
    if terlalu_sering('profil', 30, 60):
        if row and row.get('detail_lengkap'):
            return jsonify(susun_tk(row, 'lokal', 'Data dari database lokal; coba perbarui lagi sebentar.'))
        return jsonify({'error': PESAN['limit']}), 429
    try:
        p = scraper.get_tk(tk_id)
    except SourceError as e:
        if row and row.get('detail_lengkap'):
            log.warning('profil %s pakai database lokal: %s', tk_id, e)
            return jsonify(susun_tk(row, 'lokal_usang',
                                    'Sumber sedang tidak bisa dijangkau. Ini data terakhir di database lokal.'))
        return _source_error(e)
    if p is None:
        return jsonify({'error': 'Profil tidak ditemukan di sumber.'}), 404
    p['diperbarui'] = scraper.sekarang_iso()
    db.simpan_tk(p)
    return jsonify(susun_tk(p, 'agregator'))


# ------------------------------------------------------------------
# Badan usaha
# ------------------------------------------------------------------

@app.route('/api/bu/cari')
def api_bu_cari():
    q = ambil_q()
    key = ('bu', scraper.norm(q))
    cached = cache_get(key)
    if cached:
        return jsonify(cached)
    if terlalu_sering('cari', 40, 60):
        return jsonify({'error': PESAN['limit']}), 429
    try:
        r = scraper.search_bu(q)
    except SourceError as e:
        lokal = db.cari_bu(q)
        if lokal:
            log.warning('cari bu pakai database lokal: %s', e)
            return jsonify({'hasil': lokal, 'total': len(lokal), 'terpotong': len(lokal) >= 50,
                            'sumber': 'lokal',
                            'peringatan': 'Sumber sedang tidak bisa dijangkau. Hasil di bawah dari database lokal.'})
        return _source_error(e)
    db.simpan_bu_ringkas(r['hasil'])
    hasil = [{k: h[k] for k in ('npwp', 'nama', 'lokasi')} for h in r['hasil']]
    payload = {'hasil': hasil, 'total': r['total'], 'terpotong': r['terpotong'], 'q': q, 'sumber': 'agregator'}
    cache_set(key, payload)
    return jsonify(payload)


@app.route('/api/bu/profil/<npwp>')
def api_bu_profil(npwp):
    if not scraper.NPWP_RE.match(npwp):
        raise BadInput('NPWP tidak valid.')
    row = db.ambil_bu(npwp)
    segarkan = request.args.get('segarkan') == '1'
    if masih_segar(row) and not segarkan:
        return jsonify(susun_bu(row, 'lokal'))
    if terlalu_sering('profil', 30, 60):
        if row and row.get('detail_lengkap'):
            return jsonify(susun_bu(row, 'lokal', 'Data dari database lokal; coba perbarui lagi sebentar.'))
        return jsonify({'error': PESAN['limit']}), 429
    try:
        b = scraper.get_bu(npwp, (row or {}).get('path'))
    except SourceError as e:
        if row and row.get('detail_lengkap'):
            return jsonify(susun_bu(row, 'lokal_usang',
                                    'Sumber sedang tidak bisa dijangkau. Ini data terakhir di database lokal.'))
        return _source_error(e)
    if b is None:
        return jsonify({'error': 'Profil badan usaha tidak ditemukan. Cari ulang lewat nama.'}), 404
    b['diperbarui'] = scraper.sekarang_iso()
    b['lokasi'] = (row or {}).get('lokasi', '')
    db.simpan_bu(b)
    return jsonify(susun_bu(b, 'agregator'))


# ------------------------------------------------------------------
# Excel
# ------------------------------------------------------------------

def _aman(v):
    """Cegah formula injection: teks dari sumber tidak boleh diawali = + - @."""
    if isinstance(v, str) and v[:1] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + v
    return v


def _tgl(iso):
    try:
        return date.fromisoformat(iso) if iso else None
    except ValueError:
        return None


LABEL_STATUS = {'berlaku': 'Berlaku', 'segera': 'Segera Habis', 'habis': 'Habis',
                'tidak_diketahui': 'Tidak Diketahui'}


class Buku:
    """Pembungkus openpyxl dengan gaya seragam."""

    def __init__(self):
        import openpyxl
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        self.wb = openpyxl.Workbook()
        self.wb.remove(self.wb.active)
        self.f_head = Font(name='Arial', size=10, bold=True, color='FFFFFF')
        self.fill_head = PatternFill('solid', start_color='1F2328')
        self.f_body = Font(name='Arial', size=10)
        self.f_key = Font(name='Arial', size=10, bold=True)
        self.border = Border(bottom=Side(style='thin', color='D9D6CE'))
        self.wrap = Alignment(vertical='top', wrap_text=True)
        self.fill = {'berlaku': PatternFill('solid', start_color='E3F0E7'),
                     'segera': PatternFill('solid', start_color='FBF0D9'),
                     'habis': PatternFill('solid', start_color='F6E3E1')}

    def kv(self, judul, pasangan):
        ws = self.wb.create_sheet(judul)
        for i, (k, v) in enumerate(pasangan, 1):
            ws.cell(row=i, column=1, value=k).font = self.f_key
            c = ws.cell(row=i, column=2, value=_aman(v))
            c.font, c.alignment = self.f_body, self.wrap
        ws.column_dimensions['A'].width = 26
        ws.column_dimensions['B'].width = 90
        return ws

    def tabel(self, judul, kolom, baris, lebar, kolom_status=None):
        from openpyxl.utils import get_column_letter
        ws = self.wb.create_sheet(judul)
        for j, h in enumerate(kolom, 1):
            c = ws.cell(row=1, column=j, value=h)
            c.font, c.fill = self.f_head, self.fill_head
        for i, row in enumerate(baris, 2):
            status = None
            for j, v in enumerate(row, 1):
                c = ws.cell(row=i, column=j, value=_aman(v))
                c.font, c.alignment, c.border = self.f_body, self.wrap, self.border
                if isinstance(v, date):
                    c.number_format = 'DD-MM-YYYY'
                if kolom_status and j == kolom_status:
                    status = next((k for k, lbl in LABEL_STATUS.items() if lbl == v), None)
                    if status in self.fill:
                        c.fill = self.fill[status]
        for j, w in enumerate(lebar, 1):
            ws.column_dimensions[get_column_letter(j)].width = w
        ws.freeze_panes = 'A2'
        if baris:
            ws.auto_filter.ref = ws.dimensions
        return ws

    def kirim(self, nama_file):
        buf = io.BytesIO()
        self.wb.save(buf)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name=nama_file,
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def _slug(s):
    return re.sub(r'[^A-Za-z0-9]+', '_', s or '').strip('_')[:60] or 'data'


CATATAN_SUMBER = ('Data diambil dari agregator data LPJK, bukan dari sumber resmi. '
                  'Untuk bukti audit, cocokkan di situs resmi LPJK.')

KOLOM_SERT = ['No', 'Jenis', 'Kode', 'Jabatan Kerja', 'Jenjang', 'Penerbit (LSP)', 'Asosiasi',
              'Terbit', 'Berlaku s.d.', 'Status', 'Sisa Hari']
LEBAR_SERT = [5, 8, 8, 46, 9, 32, 14, 12, 12, 14, 10]


def _baris_sert(certs):
    return [[i, c.get('jenis'), c.get('kode'), c.get('jabatan'), c.get('jenjang'), c.get('penerbit'),
             c.get('asosiasi'), _tgl(c.get('terbit')), _tgl(c.get('berlaku_sampai')),
             LABEL_STATUS[c['status']], c.get('sisa_hari')] for i, c in enumerate(certs, 1)]


@app.route('/api/tk/profil/<int:tk_id>.xlsx')
def api_tk_xlsx(tk_id):
    row = db.ambil_tk(tk_id)
    if not row:
        return jsonify({'error': 'Buka profilnya dulu sebelum ekspor.'}), 404
    d = susun_tk(row, 'lokal')
    bk = Buku()
    bk.kv('Profil', [
        ('Nama', d['nama']), ('Lokasi', d['lokasi']), ('Pendidikan', d['pendidikan']),
        ('Sertifikat Berlaku', d['ringkasan']['berlaku'] + d['ringkasan']['segera']),
        ('Sertifikat Habis', d['ringkasan']['habis']),
        ('Data Diperbarui', d['diperbarui']),
        ('Verifikasi Resmi', d['url_resmi']), ('Catatan', CATATAN_SUMBER)])
    bk.tabel('Sertifikat', KOLOM_SERT, _baris_sert(d['sertifikat']), LEBAR_SERT, kolom_status=10)
    return bk.kirim(f"SKK_{_slug(d['nama'])}.xlsx")


@app.route('/api/bu/profil/<npwp>.xlsx')
def api_bu_xlsx(npwp):
    row = db.ambil_bu(npwp) if scraper.NPWP_RE.match(npwp) else None
    if not row:
        return jsonify({'error': 'Buka profilnya dulu sebelum ekspor.'}), 404
    d = susun_bu(row, 'lokal')
    bk = Buku()
    bk.kv('Profil', [
        ('Nama', d['nama']), ('Alamat', d['alamat']), ('NPWP', d['npwp_tampil'] or d['npwp']),
        ('SBU Berlaku', d['ringkasan']['berlaku'] + d['ringkasan']['segera']),
        ('SBU Habis', d['ringkasan']['habis']), ('Data Diperbarui', d['diperbarui']),
        ('Verifikasi Resmi', d['url_resmi']),
        ('Catatan', CATATAN_SUMBER)])
    bk.tabel('SBU', ['No', 'Kode', 'Sub Bidang', 'Penerbit (LSBU)', 'Berlaku s.d.', 'Status', 'Sisa Hari'],
             [[i, s.get('kode'), s.get('sub_bidang'), s.get('penerbit'), _tgl(s.get('berlaku_sampai')),
               LABEL_STATUS[s['status']], s.get('sisa_hari')] for i, s in enumerate(d['sbu'], 1)],
             [5, 9, 52, 36, 12, 14, 10], kolom_status=6)
    return bk.kirim(f"SBU_{_slug(d['nama'])}.xlsx")


# ------------------------------------------------------------------
# Cek massal
# ------------------------------------------------------------------

_jobs, _jobs_lock = {}, threading.Lock()

_KEPALA = re.compile(r'^(no\.?|nama( lengkap)?|name|tenaga( kerja| ahli)?|badan usaha|perusahaan|penyedia)$', re.I)


def _baca_daftar():
    nama = []
    f = request.files.get('file')
    if f and f.filename:
        ext = os.path.splitext(f.filename.lower())[1]
        data = f.read()
        if ext == '.xlsx':
            if not data.startswith(b'PK'):
                raise BadInput('File .xlsx tidak valid.')
            import openpyxl
            try:
                wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            except Exception:
                raise BadInput('File .xlsx tidak bisa dibaca.')
            try:
                for row in wb.active.iter_rows(values_only=True):
                    teks = next((str(v).strip() for v in row
                                 if isinstance(v, str) and v.strip() and not v.strip().isdigit()), None)
                    if teks:
                        nama.append(teks)
                    if len(nama) > MASSAL_MAKS + 5:
                        break
            finally:
                wb.close()
        elif ext in ('.csv', '.txt'):
            teks = data.decode('utf-8-sig', errors='replace')
            for row in csv.reader(io.StringIO(teks)):
                val = next((c.strip() for c in row if c.strip() and not c.strip().isdigit()), None)
                if val:
                    nama.append(val)
        else:
            raise BadInput('Format file harus .xlsx, .csv, atau .txt.')
    else:
        nama = [n for n in (request.form.get('teks') or '').splitlines()]

    bersih, lihat = [], set()
    for n in nama:
        n = re.sub(r'\s+', ' ', ''.join(ch for ch in n if ch.isprintable())).strip()[:120]
        if len(n) < 3 or _KEPALA.match(n):
            continue
        k = n.upper()
        if k not in lihat:
            lihat.add(k)
            bersih.append(n)
    if not bersih:
        raise BadInput('Daftar kosong. Isi minimal satu nama (tiga huruf atau lebih).')
    if len(bersih) > MASSAL_MAKS:
        raise BadInput(f'Maksimal {MASSAL_MAKS} nama per sekali cek. Daftar ini berisi {len(bersih)}.')
    return bersih


def _bersihkan_jobs():
    batas = time.time() - JOB_TTL
    with _jobs_lock:
        for k in [k for k, j in _jobs.items() if j['dibuat'] < batas and j['status'] != 'jalan']:
            del _jobs[k]


def _cocokkan(input_nama, hasil, kunci_fn):
    """cocok (tepat satu nama persis) | ganda | mirip (berbagi kata inti) | tidak_ada."""
    target = kunci_fn(input_nama)
    persis = [h for h in hasil if kunci_fn(h['nama']) == target]
    if len(persis) == 1:
        return 'cocok', persis[0], []
    if len(persis) > 1:
        return 'ganda', None, persis[:5]
    kata = {w for w in target.split() if len(w) >= 3}
    mirip = [h for h in hasil if kata & set(kunci_fn(h['nama']).split())]
    if mirip:
        return 'mirip', None, mirip[:5]
    return 'tidak_ada', None, []


def _proses_massal(job):
    jenis = job['jenis']
    gagal_beruntun = 0
    job['status'] = 'jalan'
    for i, nama in enumerate(job['input']):
        if job['stop']:
            job['status'] = 'dihentikan'
            break
        baris = {'no': i + 1, 'input': nama, 'hasil': 'gagal', 'kandidat': []}
        try:
            if jenis == 'tk':
                r = scraper.search_tk(nama)
                db.simpan_tk_ringkas(r['hasil'])
                hasil, match, kandidat = _cocokkan(nama, r['hasil'], nama_inti)
                baris['hasil'], baris['kandidat'] = hasil, kandidat
                if match:
                    row = db.ambil_tk(match['id'])
                    if not masih_segar(row):
                        p = scraper.get_tk(match['id'])
                        if p:
                            p['diperbarui'] = scraper.sekarang_iso()
                            db.simpan_tk(p)
                            row = p
                    if row:
                        baris['profil'] = susun_tk(row, 'agregator')
            else:
                r = scraper.search_bu(nama)
                db.simpan_bu_ringkas(r['hasil'])
                hasil, match, kandidat = _cocokkan(nama, r['hasil'], nama_bu_inti)
                baris['hasil'] = hasil
                baris['kandidat'] = [{k: h[k] for k in ('npwp', 'nama', 'lokasi')} for h in kandidat]
                if match:
                    row = db.ambil_bu(match['npwp'])
                    if not masih_segar(row):
                        b = scraper.get_bu(match['npwp'], match['path'])
                        if b:
                            b['diperbarui'] = scraper.sekarang_iso()
                            b['lokasi'] = match.get('lokasi', '')
                            db.simpan_bu(b)
                            row = b
                    if row:
                        baris['profil'] = susun_bu(row, 'agregator')
            gagal_beruntun = 0
        except SourceBlocked as e:
            log.warning('massal %s dihentikan: %s', job['id'], e)
            baris.update(hasil='gagal', kandidat=[])
            baris.pop('profil', None)
            job['baris'].append(baris)
            job['status'] = 'dihentikan'
            job['pesan'] = PESAN['blocked']
            return
        except SourceError as e:
            # Gagal di tengah (mis. pencarian berhasil, profil timeout) tetap dicatat gagal,
            # bukan "cocok" tanpa data atau "tidak ditemukan".
            log.warning('massal %s baris %s gagal: %s', job['id'], i + 1, e)
            baris.update(hasil='gagal', kandidat=[])
            baris.pop('profil', None)
            gagal_beruntun += 1
        except Exception:
            log.exception('massal %s baris %s error', job['id'], i + 1)
            baris.update(hasil='gagal', kandidat=[])
            baris.pop('profil', None)
            gagal_beruntun += 1
        job['baris'].append(baris)
        if gagal_beruntun >= 5:
            job['status'] = 'dihentikan'
            job['pesan'] = PESAN['source']
            return
    if job['status'] == 'jalan':
        job['status'] = 'selesai'


@app.route('/api/massal', methods=['POST'])
def api_massal():
    if terlalu_sering('massal', 5, 3600):
        return jsonify({'error': 'Batas cek massal tercapai (lima kali per jam). Coba lagi nanti.'}), 429
    jenis = request.form.get('jenis', 'tk')
    if jenis not in ('tk', 'bu'):
        raise BadInput('Jenis harus tk atau bu.')
    daftar = _baca_daftar()
    _bersihkan_jobs()
    with _jobs_lock:
        if sum(1 for j in _jobs.values() if j['status'] in ('antri', 'jalan')) >= JOB_JALAN_MAKS:
            return jsonify({'error': 'Server sedang memproses cek massal lain. Coba lagi beberapa menit lagi.'}), 429
        job = {'id': uuid.uuid4().hex, 'jenis': jenis, 'input': daftar, 'baris': [], 'status': 'antri',
               'stop': False, 'pesan': None, 'dibuat': time.time()}
        _jobs[job['id']] = job
    threading.Thread(target=_proses_massal, args=(job,), daemon=True).start()
    return jsonify({'id': job['id'], 'total': len(daftar)})


def _ambil_job(job_id):
    if not re.fullmatch(r'[0-9a-f]{32}', job_id or ''):
        return None
    with _jobs_lock:
        return _jobs.get(job_id)


def _ringkas_baris(b, jenis):
    out = {'no': b['no'], 'input': b['input'], 'hasil': b['hasil'],
           'kandidat': [{'nama': k['nama'], 'lokasi': k.get('lokasi', '')} for k in b['kandidat']]}
    p = b.get('profil')
    if p:
        out['nama'] = p['nama']
        out['lokasi'] = p.get('lokasi') or p.get('alamat', '')
        out['ringkasan'] = p['ringkasan']
        out['ref'] = p['id'] if jenis == 'tk' else p['npwp']
    return out


@app.route('/api/massal/<job_id>')
def api_massal_status(job_id):
    job = _ambil_job(job_id)
    if not job:
        return jsonify({'error': 'Pekerjaan tidak ditemukan atau sudah kedaluwarsa.'}), 404
    try:
        dari = max(0, int(request.args.get('dari', 0)))
    except ValueError:
        dari = 0
    baris = job['baris'][dari:]
    hitung = {}
    for b in job['baris']:
        hitung[b['hasil']] = hitung.get(b['hasil'], 0) + 1
    return jsonify({'status': job['status'], 'jenis': job['jenis'], 'total': len(job['input']),
                    'selesai': len(job['baris']), 'hitung': hitung, 'pesan': job['pesan'],
                    'baris': [_ringkas_baris(b, job['jenis']) for b in baris]})


@app.route('/api/massal/<job_id>/hentikan', methods=['POST'])
def api_massal_hentikan(job_id):
    job = _ambil_job(job_id)
    if not job:
        return jsonify({'error': 'Pekerjaan tidak ditemukan.'}), 404
    job['stop'] = True
    return jsonify({'ok': True})


LABEL_HASIL = {'cocok': 'Cocok', 'ganda': 'Nama Ganda', 'mirip': 'Tidak Persis',
               'tidak_ada': 'Tidak Ditemukan', 'gagal': 'Gagal Dicek'}


@app.route('/api/massal/<job_id>.xlsx')
def api_massal_xlsx(job_id):
    job = _ambil_job(job_id)
    if not job:
        return jsonify({'error': 'Pekerjaan tidak ditemukan atau sudah kedaluwarsa.'}), 404
    tk = job['jenis'] == 'tk'
    bk = Buku()
    ringkas, detail = [], []
    for b in job['baris']:
        p = b.get('profil') or {}
        r = p.get('ringkasan') or {}
        kandidat = '; '.join(f"{k['nama']} ({k.get('lokasi') or '-'})" for k in b['kandidat'])
        ringkas.append([b['no'], b['input'], LABEL_HASIL[b['hasil']], p.get('nama'),
                        p.get('lokasi') or p.get('alamat'),
                        (r.get('berlaku', 0) + r.get('segera', 0)) if p else None,
                        r.get('segera') if p else None, r.get('habis') if p else None,
                        kandidat or None])
        items = p.get('sertifikat' if tk else 'sbu') or []
        for c in items:
            if tk:
                detail.append([b['no'], b['input'], p['nama'], c.get('jenis'), c.get('kode'), c.get('jabatan'),
                               c.get('jenjang'), c.get('penerbit'), _tgl(c.get('terbit')),
                               _tgl(c.get('berlaku_sampai')), LABEL_STATUS[c['status']]])
            else:
                detail.append([b['no'], b['input'], p['nama'], c.get('kode'), c.get('sub_bidang'),
                               c.get('penerbit'), _tgl(c.get('berlaku_sampai')), LABEL_STATUS[c['status']]])
    bk.tabel('Ringkasan', ['No', 'Input', 'Hasil Pencocokan', 'Nama di Sumber', 'Lokasi',
                           'Berlaku', 'Segera Habis', 'Habis', 'Kandidat (Perlu Dicek Manual)'],
             ringkas, [5, 30, 16, 32, 34, 9, 12, 8, 50])
    if tk:
        bk.tabel('Sertifikat', ['No', 'Input', 'Nama', 'Jenis', 'Kode', 'Jabatan Kerja', 'Jenjang',
                                'Penerbit (LSP)', 'Terbit', 'Berlaku s.d.', 'Status'],
                 detail, [5, 26, 28, 7, 8, 44, 9, 30, 12, 12, 14], kolom_status=11)
    else:
        bk.tabel('SBU', ['No', 'Input', 'Nama', 'Kode', 'Sub Bidang', 'Penerbit (LSBU)', 'Berlaku s.d.', 'Status'],
                 detail, [5, 28, 34, 8, 48, 34, 12, 14], kolom_status=8)
    bk.kv('Catatan', [('Dibuat', datetime.now().astimezone().isoformat(timespec='seconds')),
                      ('Jumlah Input', len(job['input'])), ('Status', job['status']),
                      ('Sumber', CATATAN_SUMBER),
                      ('Nama Ganda / Tidak Persis', 'Tidak dicocokkan otomatis. Periksa manual kandidat yang tercantum.')])
    nama = 'Cek_massal_SKK' if tk else 'Cek_massal_SBU'
    return bk.kirim(f'{nama}_{date.today():%Y%m%d}.xlsx')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    print(f'SKA Checker jalan di http://localhost:{port}')
    app.run(host='127.0.0.1', port=port, debug=False, threaded=True)
