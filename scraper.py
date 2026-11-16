"""Ambil data tenaga kerja (cekskk.com) dan badan usaha (ceksbu.com).

Kedua situs itu agregator data LPJK, bukan sumber resmi. Semua isi halaman
diperlakukan sebagai DATA, tidak pernah sebagai instruksi.

Aturan main ke sumber:
- request dijeda (MIN_INTERVAL per host) supaya server mereka tidak terbebani;
- kalau sumber menolak (403/429/challenge Cloudflare) kita berhenti dan
  melempar SourceBlocked. Tidak ada upaya mengakali captcha/challenge.
"""
import os
import re
import threading
import time
import unicodedata
from datetime import date, datetime
from urllib.parse import urlencode, urlsplit

import requests
from bs4 import BeautifulSoup

CEKSKK = 'https://cekskk.com'
CEKSBU = 'https://ceksbu.com'
LPJK_TKK = 'https://lpjk.pu.go.id/pencarian-tkk-index'
LPJK_BU = 'https://lpjk.pu.go.id/pencarian-bu-index'

TIMEOUT = (6, 15)  # (koneksi, baca) detik; 3 percobaan tetap di bawah batas tunggu browser
MIN_INTERVAL = float(os.environ.get('SOURCE_MIN_INTERVAL', '0.7'))  # detik antar request per host

HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'id-ID,id;q=0.9,en;q=0.8',
}

TK_PATH_RE = re.compile(r'/tenaga-kerja-konstruksi/(\d{1,9})')
BU_PATH_RE = re.compile(r'^/company/(\d{15,16})/[A-Za-z0-9-]{1,220}$')
NPWP_RE = re.compile(r'^\d{15,16}$')


class SourceError(Exception):
    """Sumber tidak bisa dijangkau atau format halamannya berubah."""


class SourceBlocked(SourceError):
    """Sumber menolak akses (403/429/challenge). Berhenti, jangan diakali."""


# ------------------------------------------------------------------
# HTTP
# ------------------------------------------------------------------

_local = threading.local()
_throttle_lock = threading.Lock()
_next_slot = {}


def _session():
    s = getattr(_local, 'session', None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _local.session = s
    return s


def _wait_turn(host):
    with _throttle_lock:
        now = time.monotonic()
        slot = max(now, _next_slot.get(host, 0.0))
        _next_slot[host] = slot + MIN_INTERVAL
    if slot > now:
        time.sleep(slot - now)


def _is_challenge(resp):
    if resp.status_code not in (403, 503):
        return False
    head = resp.text[:4000]
    return '_cf_chl_opt' in head or 'Just a moment' in head or 'cf-chl' in head


def _decode(content):
    # Beberapa halaman cekskk menyelipkan byte non-UTF-8 (biasanya tanda pisah).
    text = content.decode('utf-8', errors='replace').replace('�', '—')
    return text.lstrip('﻿')


def _get(url, *, xhr=False, referer=None, allow_404=False, final_url=False):
    """Ambil halaman. final_url=True -> (teks, url akhir setelah redirect)."""
    host = urlsplit(url).hostname
    headers = {}
    if xhr:
        headers['X-Requested-With'] = 'XMLHttpRequest'
        headers['Accept'] = 'text/html, */*; q=0.01'
    if referer:
        headers['Referer'] = referer

    last_err = None
    for attempt in range(3):
        _wait_turn(host)
        try:
            resp = _session().get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
        except requests.RequestException as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
            continue

        if urlsplit(resp.url).hostname != host:
            raise SourceError(f'redirect ke host lain: {resp.url}')
        if resp.status_code == 429 or _is_challenge(resp) or resp.status_code == 403:
            raise SourceBlocked(f'{host} menolak akses (HTTP {resp.status_code})')
        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code >= 500:
            last_err = SourceError(f'{host} HTTP {resp.status_code}')
            time.sleep(1.5 * (attempt + 1))
            continue
        if resp.status_code != 200:
            raise SourceError(f'{host} HTTP {resp.status_code}')
        return (_decode(resp.content), resp.url) if final_url else _decode(resp.content)

    raise SourceError(f'{host} gagal dijangkau: {last_err}')


# ------------------------------------------------------------------
# Helper teks
# ------------------------------------------------------------------

def _txt(el):
    if el is None:
        return ''
    return re.sub(r'\s+', ' ', el.get_text(' ', strip=True)).strip()


def norm(s):
    """Huruf besar ASCII, tanpa tanda baca. Dipakai untuk pencarian lokal."""
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode()
    s = re.sub(r'[^A-Za-z0-9 ]+', ' ', s).upper()
    return re.sub(r'\s+', ' ', s).strip()


_GELAR_DEPAN = re.compile(r'^\s*((IR|DR|DRS|DRA|H|HJ|PROF|KH)\.?\s+)+', re.I)


def nama_inti(s):
    """Nama tanpa gelar: 'Ir. EDDY SURIPTO, S.T.' -> 'EDDY SURIPTO'."""
    s = (s or '').split(',')[0]
    s = _GELAR_DEPAN.sub('', s)
    return norm(s)


_BENTUK_USAHA = {'PT', 'CV', 'TBK', 'PERSERO', 'PERUSAHAAN', 'PERSEROAN', 'UD', 'FA', 'FIRMA', 'KOPERASI'}


def nama_bu_inti(s):
    """'PT. WIJAYA KARYA (Persero) Tbk' -> 'WIJAYA KARYA'."""
    return ' '.join(w for w in norm(s).split() if w not in _BENTUK_USAHA)


_BULAN = {
    'januari': 1, 'january': 1, 'jan': 1, 'februari': 2, 'february': 2, 'feb': 2,
    'maret': 3, 'march': 3, 'mar': 3, 'april': 4, 'apr': 4, 'mei': 5, 'may': 5,
    'juni': 6, 'june': 6, 'jun': 6, 'juli': 7, 'july': 7, 'jul': 7,
    'agustus': 8, 'august': 8, 'agu': 8, 'aug': 8, 'september': 9, 'sep': 9,
    'oktober': 10, 'october': 10, 'okt': 10, 'oct': 10, 'november': 11, 'nov': 11,
    'desember': 12, 'december': 12, 'des': 12, 'dec': 12,
}


def parse_tanggal(s):
    """'2027-11-29' / '29 November 2022' / '29-11-2022' -> '2027-11-29' atau None."""
    s = (s or '').strip()
    if not s:
        return None
    m = re.search(r'(\d{4})-(\d{1,2})-(\d{1,2})', s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.search(r'(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})', s)
        if m and m.group(2).lower() in _BULAN:
            d, mo, y = int(m.group(1)), _BULAN[m.group(2).lower()], int(m.group(3))
        else:
            m = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})', s)
            if not m:
                return None
            d, mo, y = map(int, m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def status_sertifikat(berlaku_sampai, petunjuk='', hari_ini=None):
    """berlaku | segera (<= 90 hari) | habis | tidak_diketahui."""
    hari_ini = hari_ini or date.today()
    if berlaku_sampai:
        sisa = (date.fromisoformat(berlaku_sampai) - hari_ini).days
        if sisa < 0:
            return 'habis'
        return 'segera' if sisa <= 90 else 'berlaku'
    p = (petunjuk or '').lower()
    if any(k in p for k in ('tidak berlaku', 'expired', 'kadaluarsa', 'kedaluwarsa', 'habis')):
        return 'habis'
    if 'berlaku' in p:
        return 'berlaku'
    return 'tidak_diketahui'


# ------------------------------------------------------------------
# Tenaga kerja (cekskk.com)
# ------------------------------------------------------------------

def search_tk(nama):
    url = f'{CEKSKK}/cek-skk?' + urlencode({'via': 'nama', 'p': nama})
    html, akhir = _get(url, final_url=True)
    soup = BeautifulSoup(html, 'lxml')

    # Hasil tunggal: cekskk langsung mengalihkan ke halaman profil.
    m = TK_PATH_RE.search(urlsplit(akhir).path)
    if m:
        h = {'id': int(m.group(1)), 'nama': _txt(soup.select_one('h1')),
             'lokasi': _txt(soup.select_one('.location-badge'))}
        return {'hasil': [h] if h['nama'] else [], 'total': 1 if h['nama'] else 0, 'terpotong': False}

    container = soup.select_one('#skk-results')
    if container is None:
        raise SourceError('format halaman pencarian cekskk berubah (#skk-results hilang)')

    hasil = []
    for card in container.select('.item'):
        a = card.select_one('a[href*="/tenaga-kerja-konstruksi/"]')
        m = TK_PATH_RE.search(a.get('href', '')) if a else None
        if not m:
            continue
        hasil.append({
            'id': int(m.group(1)),
            'nama': _txt(card.select_one('h4')),
            'lokasi': _txt(card.select_one('.card-text')),
        })

    lead = _txt(soup.select_one('#sr-focus .lead'))
    m = re.search(r'([\d.]+)\s+Hasil Pencarian', lead, re.I)
    total = int(m.group(1).replace('.', '')) if m else len(hasil)
    if total and not hasil:
        raise SourceError('sumber menyebut ada hasil, tetapi daftar hasil tidak terbaca')
    terpotong = bool(soup.find(string=re.compile(r'Hanya\s+\d+\s+hasil pencarian yang ditampilkan', re.I)))
    return {'hasil': hasil, 'total': max(total, len(hasil)), 'terpotong': terpotong}


def _parse_sertifikat(html):
    soup = BeautifulSoup(html, 'lxml')
    daftar = []
    for col in soup.select('div.col'):
        body = col.select_one('.card-body')
        judul = _txt(body.select_one('h3')) if body else ''
        if not judul:
            continue

        kolom = {}
        for h4 in body.select('h4'):
            t = _txt(h4)
            if ':' in t:
                k, v = t.split(':', 1)
                kolom[k.strip().lower()] = v.strip()

        label = f"{_txt(col.select_one('.badge'))} {' '.join(col.get('class', []))} {judul}".upper()
        jenis = 'SKA' if 'SKA' in label else 'SKT' if 'SKT' in label else 'SKK'

        jabatan = re.sub(r'^(SKK Konstruksi|Sertifikat (Keahlian|Keterampilan)\s*\((SKA|SKT)\)|SKA|SKT)\s*',
                         '', judul, flags=re.I).strip()
        m = re.match(r'([A-Z]{2}\d{3})\s+(.*)', jabatan)
        kode = ''
        if m:
            kode, jabatan = m.group(1), m.group(2)

        def ambil(*keys):
            for k in keys:
                if kolom.get(k):
                    return kolom[k]
            return ''

        cert = {
            'jenis': jenis,
            'kode': kode,
            'jabatan': jabatan,
            'jenjang': ambil('jenjang/kualifikasi', 'jenjang', 'kualifikasi'),
            'penerbit': ambil('penerbit', 'lsp'),
            'asosiasi': ambil('asosiasi'),
            'sub_bidang': ambil('sub bidang', 'subbidang', 'klasifikasi'),
            'nomor': ambil('no. registrasi', 'nomor registrasi', 'no registrasi', 'nomor'),
            'terbit': parse_tanggal(ambil('reg', 'tanggal registrasi', 'terbit', 'tanggal cetak')),
            'berlaku_sampai': parse_tanggal(ambil('exp', 'berlaku hingga', 'berlaku sampai', 'masa berlaku')),
            'keterangan': _txt(body.select_one('.alert')),
        }
        daftar.append(cert)
    return daftar


def get_tk(tk_id):
    """Profil + daftar sertifikat.

    None hanya kalau sumber jelas menyatakan profil tidak ada (404 atau dialihkan
    ke beranda). Halaman yang formatnya tidak dikenali -> SourceError, supaya
    kegagalan sumber tidak terbaca sebagai "tidak ditemukan".
    """
    tk_id = int(tk_id)
    url = f'{CEKSKK}/tenaga-kerja-konstruksi/{tk_id}'
    res = _get(url, allow_404=True, final_url=True)
    if res is None:
        return None
    html, akhir = res
    m = TK_PATH_RE.search(urlsplit(akhir).path)
    if not m or int(m.group(1)) != tk_id:
        if urlsplit(akhir).path in ('', '/'):
            return None
        raise SourceError(f'profil {tk_id} dialihkan ke {akhir}')
    soup = BeautifulSoup(html, 'lxml')

    nama = _txt(soup.select_one('h1'))
    if not nama or not soup.select_one('.location-badge, .about-row, .stat-card'):
        raise SourceError(f'format halaman profil {tk_id} tidak dikenali')

    angka = {}
    for card in soup.select('.stat-card'):
        label = _txt(card.select_one('.stat-label')).lower()
        num = re.sub(r'\D', '', _txt(card.select_one('.stat-num')))
        if label and num:
            angka[label] = int(num)

    info = {}
    for row in soup.select('.about-row'):
        info[_txt(row.select_one('.about-label')).lower()] = _txt(row.select_one('.about-value'))

    profil = {
        'id': tk_id,
        'nama': nama,
        'lokasi': info.get('lokasi') or _txt(soup.select_one('.location-badge')),
        'pendidikan': info.get('pendidikan terakhir', ''),
        'bidang': next((v for k, v in info.items() if k.startswith('bidang')), ''),
        'total_sertifikat': angka.get('total sertifikat'),
        'sertifikat_aktif': angka.get('masih aktif'),
        'sertifikat': [],
        'detail_lengkap': False,
    }

    m = re.search(r"/k/u/(\d+)\?vx=([0-9a-f]{16,64})", html)
    if m and int(m.group(1)) == tk_id:
        frag = _get(f'{CEKSKK}/k/u/{tk_id}?vx={m.group(2)}', xhr=True, referer=url)
        if '<html' not in frag[:500].lower():
            profil['sertifikat'] = _parse_sertifikat(frag)
            profil['detail_lengkap'] = True
    elif not angka:
        # Profil tanpa sertifikat: halaman memang tidak memuat daftar apa pun.
        profil['total_sertifikat'] = profil['sertifikat_aktif'] = 0
        profil['detail_lengkap'] = True

    if not profil['detail_lengkap'] and profil['bidang']:
        # Cadangan: hanya nama jabatan, tanpa tanggal.
        profil['sertifikat'] = [
            {'jenis': '', 'kode': '', 'jabatan': j.strip(), 'jenjang': '', 'penerbit': '', 'asosiasi': '',
             'sub_bidang': '', 'nomor': '', 'terbit': None, 'berlaku_sampai': None, 'keterangan': ''}
            for j in profil['bidang'].split(',') if j.strip()
        ]
    return profil


# ------------------------------------------------------------------
# Badan usaha (ceksbu.com)
# ------------------------------------------------------------------

def search_bu(q):
    url = f'{CEKSBU}/cek-sbu?' + urlencode({'parameter': q})
    html, akhir = _get(url, final_url=True)
    soup = BeautifulSoup(html, 'lxml')

    m = BU_PATH_RE.match(urlsplit(akhir).path)
    if m:  # hasil tunggal dialihkan ke profil
        nama = _txt(soup.select_one('h1.display-4') or soup.select_one('h1'))
        h = {'npwp': m.group(1), 'path': urlsplit(akhir).path, 'nama': nama, 'lokasi': ''}
        return {'hasil': [h] if nama else [], 'total': 1 if nama else 0, 'terpotong': False}

    hasil, dilihat = [], set()
    for a in soup.select('a[href^="/company/"]'):
        path = a.get('href', '').split('?')[0]
        m = BU_PATH_RE.match(path)
        if not m or path in dilihat:
            continue
        dilihat.add(path)
        lokasi = _txt(a.select_one('.card-body p')).strip(' ,')
        hasil.append({
            'npwp': m.group(1),
            'path': path,
            'nama': _txt(a.select_one('h4')),
            'lokasi': lokasi,
        })

    if not hasil and 'Hasil Pencarian SBU' not in html:
        raise SourceError('format halaman pencarian ceksbu berubah')
    return {'hasil': hasil, 'total': len(hasil), 'terpotong': False}


def get_bu(npwp, path=None):
    if not NPWP_RE.match(npwp or ''):
        raise ValueError('NPWP tidak valid')
    if path and not (BU_PATH_RE.match(path) and BU_PATH_RE.match(path).group(1) == npwp):
        path = None
    url = CEKSBU + (path or f'/company/{npwp}')
    res = _get(url, allow_404=True, final_url=True)
    if res is None:
        return None
    html, akhir = res
    jalur = urlsplit(akhir).path
    if not jalur.startswith(f'/company/{npwp}'):
        if jalur in ('', '/'):
            return None
        raise SourceError(f'profil BU {npwp} dialihkan ke {akhir}')
    soup = BeautifulSoup(html, 'lxml')

    h1 = soup.select_one('h1.display-4')
    nama = _txt(h1)
    if not nama:
        raise SourceError(f'format halaman profil BU {npwp} tidak dikenali')

    alamat = npwp_tampil = ''
    if h1 is not None:
        for h4 in h1.parent.select('h4'):
            t = _txt(h4)
            if t.lower().startswith('alamat'):
                alamat = t.split(':', 1)[-1].strip()
            elif t.lower().startswith('npwp'):
                npwp_tampil = t.split(':', 1)[-1].strip()

    sbu = []
    for body in soup.select('.card-body'):
        teks = _txt(body)
        if 'Berlaku s/d' not in teks:
            continue
        kartu = body.find_parent('a') or body.parent
        kode = ''
        m = re.search(r'Pelajari\s+([A-Z]{2}\d{3})', _txt(kartu))
        if m:
            kode = m.group(1)
        penerbit = ''
        for h4 in body.select('h4'):
            t = _txt(h4)
            if t.lower().startswith('penerbit'):
                penerbit = t.split(':', 1)[-1].strip()
        m = re.search(r'Berlaku s/d\s*([0-9-]{8,10})', teks)
        sbu.append({
            'kode': kode,
            'sub_bidang': _txt(body.select_one('h3')),
            'penerbit': penerbit,
            'berlaku_sampai': parse_tanggal(m.group(1)) if m else None,
        })

    return {
        'npwp': npwp,
        'path': urlsplit(url).path,
        'nama': nama,
        'alamat': alamat,
        'npwp_tampil': npwp_tampil,
        'sbu': sbu,
        'detail_lengkap': True,
    }


def tk_url(tk_id):
    return f'{CEKSKK}/tenaga-kerja-konstruksi/{int(tk_id)}'


def bu_url(npwp, path=None):
    return CEKSBU + (path if path and BU_PATH_RE.match(path) else f'/company/{npwp}')


def sekarang_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')
