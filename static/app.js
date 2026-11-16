'use strict';

/* ================================================================
   Helper
   ================================================================ */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ESC[c]);

class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
const raw = (s) => new Raw(s);
function html(strings, ...vals) {
  let out = '';
  strings.forEach((s, i) => {
    out += s;
    if (i < vals.length) {
      const v = vals[i];
      if (v instanceof Raw) out += v.s;
      else if (Array.isArray(v)) out += v.map((x) => (x instanceof Raw ? x.s : esc(x))).join('');
      else if (v !== false && v != null) out += esc(v);
    }
  });
  return raw(out);
}

const fmtTgl = new Intl.DateTimeFormat('id-ID', { day: 'numeric', month: 'short', year: 'numeric' });
const fmtWaktu = new Intl.DateTimeFormat('id-ID', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
const fmtAngka = new Intl.NumberFormat('id-ID');

function tglLokal(iso) {
  if (!iso) return null;
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
  return new Date(y, m - 1, d);
}
const tgl = (iso) => (iso ? fmtTgl.format(tglLokal(iso)) : '');
function waktu(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : fmtWaktu.format(d);
}

function durasi(hari) {
  const h = Math.abs(hari);
  if (h < 45) return `${h} hari`;
  const bulan = Math.round(h / 30.44);
  if (bulan < 12) return `${bulan} bulan`;
  const th = Math.floor(bulan / 12), sisa = bulan % 12;
  return sisa ? `${th} tahun ${sisa} bulan` : `${th} tahun`;
}

// Data wilayah dan sub bidang dari sumber sering ditulis kapital semua. Di tampilan dirapikan menjadi
// huruf kapital di awal kata; singkatan tetap kapital dan kata tugas tetap kecil. Excel tetap memakai data asli.
const KATA_TUGAS = new Set(['di', 'ke', 'dari', 'dan', 'yang', 'untuk', 'atau', 'pada', 'dengan', 'dalam', 'serta', 'bagi', 'oleh']);
const TETAP = {
  'DKI': 'DKI', 'DI': 'DI', 'D.I.': 'D.I.', 'KAB.': 'Kab.', 'KAB': 'Kab.', 'ADM.': 'Adm.', 'ADM': 'Adm.',
  'KEC.': 'Kec.', 'KEL.': 'Kel.', 'NTB': 'NTB', 'NTT': 'NTT', 'II': 'II', 'III': 'III', 'IV': 'IV',
  'SPBU': 'SPBU', 'BTS': 'BTS', 'LNG': 'LNG', 'LPG': 'LPG', 'PLTA': 'PLTA', 'PLTU': 'PLTU', 'PLTD': 'PLTD',
  'PLTG': 'PLTG', 'PLTS': 'PLTS', 'PLTP': 'PLTP', 'PLTB': 'PLTB', 'PLTN': 'PLTN',
};
function kapitalAwal(teks) {
  if (!teks) return teks || '';
  let pertama = true;
  return String(teks).replace(/[A-Za-zÀ-ÿ']+(?:\.[A-Za-zÀ-ÿ']+)*\.?/g, (w) => {
    const awal = pertama;
    pertama = false;
    const besar = w.toUpperCase();
    if (TETAP[besar]) return TETAP[besar];
    const kecil = w.toLowerCase();
    if (!awal && KATA_TUGAS.has(kecil)) return kecil;
    return kecil.charAt(0).toUpperCase() + kecil.slice(1);
  });
}

const LABEL_STATUS = { berlaku: 'Berlaku', segera: 'Segera Habis', habis: 'Habis', tidak_diketahui: 'Belum Diketahui' };
const LABEL_HASIL = { cocok: 'Cocok', ganda: 'Nama Ganda', mirip: 'Tidak Persis', tidak_ada: 'Tidak Ditemukan', gagal: 'Gagal Dicek' };

const IKON_LUAR = raw('<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3h7v7M13 3L5 11" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>');
const IKON_ULANG = raw('<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.5v3h-3" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>');
const IKON_UNDUH = raw('<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.5v8M4.5 7L8 10.5 11.5 7M3 13.5h10" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>');

const BATAS_TUNGGU_MS = 75000;

async function api(url, opts = {}) {
  const ctrl = new AbortController();
  let habisWaktu = false;
  const timer = setTimeout(() => { habisWaktu = true; ctrl.abort(); }, BATAS_TUNGGU_MS);
  opts.signal?.addEventListener('abort', () => ctrl.abort(), { once: true });
  try {
    let resp;
    try {
      resp = await fetch(url, { ...opts, signal: ctrl.signal });
    } catch (e) {
      if (habisWaktu) return { ok: false, status: 0, data: { error: 'Sumber data tidak merespons. Coba lagi nanti.' } };
      if (e.name === 'AbortError') throw e;
      return { ok: false, status: 0, data: { error: 'Permintaan tidak sampai ke server. Coba lagi.' } };
    }
    let data;
    try {
      data = await resp.json();
    } catch {
      data = { error: resp.status >= 500 ? `Sumber data gagal dimuat (HTTP ${resp.status}). Coba lagi nanti.`
                                         : `Server membalas tanpa data (HTTP ${resp.status}).` };
    }
    return { ok: resp.ok, status: resp.status, data };
  } finally {
    clearTimeout(timer);
  }
}

let toastTimer;
function toast(pesan) {
  const el = $('#toast');
  el.textContent = pesan;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3800);
}

const simpanSesi = (k, v) => { try { v == null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch { /* abaikan */ } };
const bacaSesi = (k) => { try { return sessionStorage.getItem(k); } catch { return null; } };

/* ================================================================
   State + router  (#tk  #tk?q=..  #tk/123  #bu?q=..  #bu/npwp  #massal)
   ================================================================ */

const MODE = {
  tk: { label: 'Nama Tenaga Kerja', placeholder: 'Nama Lengkap tanpa Gelar',
        hint: 'Sumber hanya menyediakan pencarian lewat nama. Tulis sesuai KTP, tanpa gelar.',
        cari: '/api/tk/cari', profil: (r) => `/api/tk/profil/${r}` },
  bu: { label: 'Nama Badan Usaha atau NIB', placeholder: 'Contoh: Wijaya Karya',
        hint: 'Bentuk usaha (PT, CV) boleh ditulis atau tidak.',
        cari: '/api/bu/cari', profil: (r) => `/api/bu/profil/${r}` },
};

const state = { hasil: { tk: null, bu: null }, ctrl: null, dariHasil: false };

function bacaHash() {
  const h = decodeURIComponent(location.hash.slice(1));
  const [jalur, qs] = h.split('?');
  const [mode, ref] = (jalur || 'tk').split('/');
  return { mode: ['tk', 'bu', 'massal'].includes(mode) ? mode : 'tk', ref: ref || null, q: new URLSearchParams(qs || '').get('q') || '' };
}

function setAwal(awal) {
  if (awal) document.body.setAttribute('data-awal', '1');
  else document.body.removeAttribute('data-awal');
}

async function route() {
  const r = bacaHash();
  $$('.tab').forEach((t) => (t.dataset.tab === r.mode ? t.setAttribute('aria-current', 'page') : t.removeAttribute('aria-current')));

  if (r.mode === 'massal') {
    $('#viewCari').hidden = true;
    $('#viewMassal').hidden = false;
    setAwal(true);
    document.title = 'Cek Massal · SKA Checker';
    lanjutkanJob();
    return;
  }
  $('#viewMassal').hidden = true;
  $('#viewCari').hidden = false;

  const m = MODE[r.mode];
  $('#qLabel').textContent = m.label;
  $('#q').placeholder = m.placeholder;
  $('#qHint').textContent = m.hint;

  if (r.ref) {
    setAwal(false);
    await bukaProfil(r.mode, r.ref);
  } else if (r.q) {
    setAwal(false);
    $('#q').value = r.q;
    await cari(r.mode, r.q);
  } else {
    setAwal(true);
    $('#q').value = '';
    document.title = 'SKA Checker · Cek Sertifikat Konstruksi';
    tampilAwal(r.mode);
  }
}

window.addEventListener('hashchange', () => { route(); });

/* ================================================================
   Layar awal
   ================================================================ */

function tampilAwal(mode) {
  const benda = mode === 'tk' ? 'sertifikat' : 'SBU';
  $('#panel').innerHTML = html`
    <div class="legenda rise">
      <div><span class="cap cap--kecil cap--berlaku">Berlaku</span>
        <h3>Masih Berlaku</h3><p>Masa berlaku ${benda} lebih dari sembilan puluh hari lagi.</p></div>
      <div><span class="cap cap--kecil cap--segera">Segera Habis</span>
        <h3>Habis dalam Sembilan Puluh Hari</h3><p>Perlu diperhatikan bila pekerjaan masih berjalan melewati tanggal itu.</p></div>
      <div><span class="cap cap--kecil cap--habis">Habis</span>
        <h3>Sudah Lewat Masa Berlaku</h3><p>Status dihitung dari tanggal berlaku, bukan dari label sumber.</p></div>
    </div>`.s;
}

/* ================================================================
   Pencarian
   ================================================================ */

function tampilMuat(teks) {
  $('#panel').innerHTML = html`<div class="muat"><div class="muat__garis"></div>${teks}</div>`.s;
}

// Kegagalan sumber/timeout tampil sebagai gagal, tidak pernah sebagai "tidak ditemukan".
function tampilGagal(pesan, ulang) {
  $('#panel').innerHTML = html`<div class="kosong rise">
    <p class="kosong__judul">${pesan}</p>
    ${ulang ? html`<p><button class="btn" type="button" data-aksi="ulang">${IKON_ULANG} Coba Lagi</button></p>` : ''}
  </div>`.s;
}

async function cari(mode, q) {
  const simpan = state.hasil[mode];
  if (simpan && simpan.q === q) { tampilHasil(mode, simpan.data); return; }

  state.ctrl?.abort();
  const ctrl = (state.ctrl = new AbortController());
  document.title = `${q} · SKA Checker`;
  tampilMuat(`Mencari “${q}”…`);
  let res;
  try {
    res = await api(`${MODE[mode].cari}?q=${encodeURIComponent(q)}`, { signal: ctrl.signal });
  } catch { return; }
  if (ctrl.signal.aborted) return;
  if (!res.ok) { tampilGagal(res.data.error, res.status !== 400); return; }
  state.hasil[mode] = { q, data: res.data };
  tampilHasil(mode, res.data);
}

function tampilHasil(mode, d) {
  const q = state.hasil[mode]?.q || $('#q').value;
  if (!d.hasil.length) {
    $('#panel').innerHTML = html`<div class="kosong rise">
      <p class="kosong__judul">Tidak ada hasil untuk “${q}”.</p>
      <p>${mode === 'tk'
        ? 'Coba tulis nama lengkap sesuai KTP tanpa gelar, atau sebagian nama saja. Bila tetap kosong, cek langsung di situs LPJK; tenaga kerja yang baru terdaftar bisa belum tercatat di sumber.'
        : 'Coba tanpa bentuk usaha (PT, CV), atau dengan sebagian nama saja. Bila tetap kosong, cek langsung di situs LPJK.'}</p>
      <p><a class="btn" href="${mode === 'tk' ? 'https://lpjk.pu.go.id/pencarian-tkk-index' : 'https://lpjk.pu.go.id/pencarian-bu-index'}" target="_blank" rel="noopener">Buka Pencarian LPJK ${IKON_LUAR}</a></p>
    </div>`.s;
    return;
  }
  const tk = mode === 'tk';
  const jumlah = d.total > d.hasil.length ? `${fmtAngka.format(d.hasil.length)} dari ${fmtAngka.format(d.total)} Hasil` : `${fmtAngka.format(d.hasil.length)} Hasil`;
  $('#panel').innerHTML = html`
    ${d.peringatan ? html`<p class="note">${d.peringatan}</p>` : ''}
    ${d.terpotong ? html`<p class="note note--info">Sumber hanya menampilkan sebagian hasil. Perjelas dengan nama lengkap bila orang yang dicari tidak ada di daftar.</p>` : ''}
    <div class="hasil__head rise">
      <h2 class="hasil__judul">${jumlah} untuk <em>“${q}”</em></h2>
      ${d.sumber === 'lokal' ? html`<span class="hasil__meta">Sumber: Database Lokal</span>` : ''}
    </div>
    <div class="tabel-wrap rise">
      <table class="tabel">
        <thead><tr><th class="c-no">No</th><th>Nama</th><th class="c-lok">${tk ? 'Lokasi' : 'Kabupaten/Kota'}</th><th class="c-ref">${tk ? 'ID' : 'NPWP'}</th><th aria-hidden="true"></th></tr></thead>
        <tbody>${d.hasil.map((h, i) => {
          const ref = tk ? h.id : h.npwp;
          return html`<tr class="baris" data-buka="${mode}/${ref}">
            <td class="c-no">${String(i + 1).padStart(2, '0')}</td>
            <td><a class="nama" href="#${mode}/${ref}">${h.nama}</a><div class="lok-kecil">${kapitalAwal(h.lokasi)}</div></td>
            <td class="lok c-lok">${kapitalAwal(h.lokasi) || '–'}</td>
            <td class="ref c-ref">${tk ? h.id : npwpFormat(h.npwp)}</td>
            <td class="c-angka"><span class="panah" aria-hidden="true">→</span></td>
          </tr>`;
        })}</tbody>
      </table>
    </div>`.s;
}

function npwpFormat(n) {
  const s = String(n || '');
  if (s.length !== 15) return s;
  return `${s.slice(0, 2)}.${s.slice(2, 5)}.${s.slice(5, 8)}.${s[8]}-${s.slice(9, 12)}.${s.slice(12)}`;
}

/* ================================================================
   Profil
   ================================================================ */

async function bukaProfil(mode, ref, segarkan = false) {
  state.ctrl?.abort();
  const ctrl = (state.ctrl = new AbortController());
  tampilMuat(segarkan ? 'Mengambil ulang data…' : 'Memuat profil…');
  let res;
  try {
    res = await api(MODE[mode].profil(encodeURIComponent(ref)) + (segarkan ? '?segarkan=1' : ''), { signal: ctrl.signal });
  } catch { return; }
  if (ctrl.signal.aborted) return;
  if (!res.ok) { tampilGagal(res.data.error, res.status !== 400 && res.status !== 404); return; }
  const d = res.data;
  document.title = `${d.nama} · SKA Checker`;
  mode === 'tk' ? tampilTK(d) : tampilBU(d);
  window.scrollTo({ top: 0, behavior: 'smooth' });
  muatStatistik();
}

function blokMasa(c) {
  if (!c.berlaku_sampai) {
    return html`<div class="masa"><div class="masa__label"><span>Tanggal berlaku belum tersedia di sumber.</span></div></div>`;
  }
  const akhir = tglLokal(c.berlaku_sampai);
  const awal = c.terbit ? tglLokal(c.terbit) : null;
  const kini = new Date(); kini.setHours(0, 0, 0, 0);
  let p = 100, tick = null;
  if (awal && akhir > awal) {
    p = Math.max(0, Math.min(100, ((kini - awal) / (akhir - awal)) * 100));
    if (p > 0 && p < 100) tick = p;
  }
  return html`<div class="masa">
    <div class="masa__jalur"><span class="masa__isi" data-p="${p.toFixed(2)}"></span>${tick != null ? html`<span class="masa__kini" data-x="${tick.toFixed(2)}" title="Hari ini"></span>` : ''}</div>
    <div class="masa__label"><span>${awal ? html`Terbit <b>${tgl(c.terbit)}</b>` : ''}</span><span>Berlaku s.d. <b>${tgl(c.berlaku_sampai)}</b></span></div>
  </div>`;
}

function teksSisa(c) {
  if (c.sisa_hari == null) return '';
  if (c.sisa_hari < 0) return `Habis ${durasi(c.sisa_hari)} lalu`;
  if (c.sisa_hari === 0) return 'Habis hari ini';
  return `Sisa ${durasi(c.sisa_hari)}`;
}

function blokAngka(r, total) {
  return html`<dl class="angka">
    <div><dt><span class="titik titik--berlaku"></span>Berlaku</dt><dd>${r.berlaku}</dd></div>
    <div><dt><span class="titik titik--segera"></span>Segera Habis</dt><dd>${r.segera}</dd></div>
    <div><dt><span class="titik titik--habis"></span>Habis</dt><dd>${r.habis}</dd></div>
    <div><dt><span class="titik"></span>Total</dt><dd>${total}</dd></div>
  </dl>`;
}

function blokSaring(r) {
  const opsi = [['semua', 'Semua', r.total], ['berlaku', 'Berlaku', r.berlaku + r.segera], ['habis', 'Habis', r.habis]];
  return html`<div class="saring" role="group" aria-label="Saring menurut status">${opsi.map(([k, l, n], i) =>
    html`<button type="button" data-saring="${k}" aria-pressed="${i === 0}">${l}<span>${n}</span></button>`)}</div>`;
}

function blokKembali() {
  return html`<button type="button" class="lembar__kembali" data-aksi="kembali">← ${state.dariHasil ? 'Kembali ke Hasil Pencarian' : 'Pencarian Baru'}</button>`;
}

function blokPeringatan(d, lengkap = true) {
  return html`${d.peringatan ? html`<p class="note">${d.peringatan}</p>` : ''}
    ${!lengkap ? html`<p class="note">Detail sertifikat (tanggal dan penerbit) belum bisa diambil dari sumber. Yang tampil hanya nama jabatan kerja.</p>` : ''}`;
}

function blokCatatan(d) {
  return html`<p class="catatan-kaki">Diperbarui ${waktu(d.diperbarui) || '–'}${d.sumber.startsWith('lokal') ? ' (salinan database lokal)' : ''}. Data ini bukan dari sumber resmi; untuk bukti audit, cocokkan di situs LPJK.</p>`;
}

function tampilTK(d) {
  const r = d.ringkasan;
  $('#panel').innerHTML = html`
    ${blokKembali()}
    ${blokPeringatan(d, d.detail_lengkap)}
    <article class="lembar rise">
      <header class="lembar__head">
        <div>
          <p class="eyebrow">Tenaga Kerja Konstruksi</p>
          <h2 class="lembar__nama">${d.nama}</h2>
          <p class="lembar__sub">${kapitalAwal(d.lokasi) || 'Lokasi tidak tercatat'}</p>
          <p class="lembar__ref">ID Profil <span class="mono">${d.id}</span></p>
        </div>
        <div class="aksi">
          <button class="btn btn--ink" type="button" data-aksi="lpjk" data-nama="${d.nama}" data-url="${d.url_resmi}">Verifikasi di LPJK ${IKON_LUAR}</button>
          <a class="btn" href="/api/tk/profil/${d.id}.xlsx">${IKON_UNDUH} Excel</a>
          <button class="btn" type="button" data-aksi="segarkan" data-ref="tk/${d.id}">${IKON_ULANG} Perbarui</button>
        </div>
      </header>
      ${blokAngka(r, r.total)}
      <dl class="fakta">
        <dt>Pendidikan</dt><dd>${d.pendidikan || '–'}</dd>
        ${d.total_sumber != null && d.total_sumber !== r.total ? html`<dt>Catatan Sumber</dt><dd>Sumber mencatat ${d.total_sumber} sertifikat, ${r.total} yang bisa dibaca.</dd>` : ''}
      </dl>
      <div class="daftar__head">
        <h3 class="daftar__judul">Sertifikat</h3>
        ${r.total > 1 ? blokSaring(r) : ''}
      </div>
      <div class="daftar" id="daftar">
        ${d.sertifikat.length ? d.sertifikat.map((c) => html`
          <div class="entri s-${c.status}" data-status="${c.status === 'segera' ? 'berlaku' : c.status}">
            <div class="entri__jenis">${c.jenis || 'Sertifikat'}${c.kode ? html`<span>${c.kode}</span>` : ''}</div>
            <div>
              <h4 class="entri__judul">${c.jabatan}</h4>
              <p class="entri__meta">${[c.jenjang && `Jenjang ${c.jenjang}`, c.penerbit && `LSP ${c.penerbit}`, c.asosiasi && `Asosiasi ${c.asosiasi}`]
                .filter(Boolean).map((t, i) => html`${i ? raw('<span class="sep">·</span>') : ''}${t}`)}</p>
              ${blokMasa(c)}
            </div>
            <div class="entri__kanan"><span class="cap cap--${c.status}">${LABEL_STATUS[c.status]}</span><span class="sisa">${teksSisa(c)}</span></div>
          </div>`) : html`<p class="hint">Belum ada sertifikat yang tercatat untuk tenaga kerja ini.</p>`}
      </div>
      ${blokCatatan(d)}
    </article>`.s;
  pasangMasa();
}

function tampilBU(d) {
  const r = d.ringkasan;
  $('#panel').innerHTML = html`
    ${blokKembali()}
    ${blokPeringatan(d)}
    <article class="lembar rise">
      <header class="lembar__head">
        <div>
          <p class="eyebrow">Badan Usaha Jasa Konstruksi</p>
          <h2 class="lembar__nama">${d.nama}</h2>
          <p class="lembar__sub">${d.alamat || kapitalAwal(d.lokasi) || 'Alamat tidak tercatat'}</p>
          <p class="lembar__ref">NPWP <span class="mono">${npwpFormat(d.npwp)}</span></p>
        </div>
        <div class="aksi">
          <button class="btn btn--ink" type="button" data-aksi="lpjk" data-nama="${d.nama}" data-url="${d.url_resmi}">Verifikasi di LPJK ${IKON_LUAR}</button>
          <a class="btn" href="/api/bu/profil/${d.npwp}.xlsx">${IKON_UNDUH} Excel</a>
          <button class="btn" type="button" data-aksi="segarkan" data-ref="bu/${d.npwp}">${IKON_ULANG} Perbarui</button>
        </div>
      </header>
      ${blokAngka(r, r.total)}
      <div class="daftar__head">
        <h3 class="daftar__judul">Sertifikat Badan Usaha (SBU)</h3>
        ${r.total > 1 ? blokSaring(r) : ''}
      </div>
      <div class="daftar" id="daftar">
        ${d.sbu.length ? d.sbu.map((s) => html`
          <div class="entri s-${s.status}" data-status="${s.status === 'segera' ? 'berlaku' : s.status}">
            <div class="entri__jenis">SBU${s.kode ? html`<span>${s.kode}</span>` : ''}</div>
            <div>
              <h4 class="entri__judul">${kapitalAwal(s.sub_bidang)}</h4>
              <p class="entri__meta">${s.penerbit ? `Penerbit ${s.penerbit}` : ''}</p>
              ${blokMasa(s)}
            </div>
            <div class="entri__kanan"><span class="cap cap--${s.status}">${LABEL_STATUS[s.status]}</span><span class="sisa">${teksSisa(s)}</span></div>
          </div>`) : html`<p class="hint">Belum ada SBU yang tercatat untuk badan usaha ini.</p>`}
      </div>
      ${blokCatatan(d)}
    </article>`.s;
  pasangMasa();
}

// CSP melarang atribut style inline, jadi lebar bar diisi lewat CSSOM.
function pasangMasa() {
  $$('.masa__kini').forEach((el) => { el.style.left = `${el.dataset.x}%`; });
  requestAnimationFrame(() => requestAnimationFrame(() => {
    $$('.masa__isi').forEach((el) => { el.style.width = `${el.dataset.p}%`; });
  }));
}

/* ================================================================
   Interaksi panel (delegasi)
   ================================================================ */

$('#formCari').addEventListener('submit', (e) => {
  e.preventDefault();
  const q = $('#q').value.replace(/\s+/g, ' ').trim();
  const { mode } = bacaHash();
  if (q.length < 3) { $('#qHint').textContent = 'Ketik minimal tiga karakter.'; $('#q').focus(); return; }
  state.hasil[mode] = state.hasil[mode]?.q === q ? state.hasil[mode] : null;
  const target = `#${mode}?q=${encodeURIComponent(q)}`;
  if (location.hash === target) route(); else location.hash = target;
});

$('#panel').addEventListener('click', async (e) => {
  const baris = e.target.closest('[data-buka]');
  if (baris && !e.target.closest('a')) {
    state.dariHasil = true;
    location.hash = `#${baris.dataset.buka}`;
    return;
  }
  if (e.target.closest('a.nama')) { state.dariHasil = true; return; }

  const btn = e.target.closest('[data-aksi], [data-saring]');
  if (!btn) return;

  if (btn.dataset.saring) {
    $$('.saring button').forEach((b) => b.setAttribute('aria-pressed', String(b === btn)));
    const f = btn.dataset.saring;
    $$('#daftar .entri').forEach((el) => { el.hidden = f !== 'semua' && el.dataset.status !== f; });
    return;
  }

  const aksi = btn.dataset.aksi;
  if (aksi === 'kembali') {
    if (state.dariHasil) { state.dariHasil = false; history.back(); }
    else { location.hash = `#${bacaHash().mode}`; }
  } else if (aksi === 'ulang') {
    const r = bacaHash();
    state.hasil[r.mode] = null;
    route();
  } else if (aksi === 'segarkan') {
    const [mode, ref] = btn.dataset.ref.split('/');
    bukaProfil(mode, ref, true);
  } else if (aksi === 'lpjk') {
    try {
      await navigator.clipboard.writeText(btn.dataset.nama);
      toast('Nama disalin. Tempel di kolom pencarian LPJK, lalu selesaikan verifikasinya di sana.');
    } catch {
      toast('Salin nama secara manual, lalu tempel di kolom pencarian LPJK.');
    }
    window.open(btn.dataset.url, '_blank', 'noopener');
  }
});

document.addEventListener('keydown', (e) => {
  const mengetik = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName);
  if (e.key === '/' && !mengetik && !$('#viewCari').hidden) {
    e.preventDefault();
    $('#q').focus();
    $('#q').select();
  }
});

/* ================================================================
   Statistik database
   ================================================================ */

async function muatStatistik() {
  const res = await api('/api/statistik');
  if (!res.ok) return;
  const s = res.data;
  $('#dbStat').innerHTML = html`<span class="lbl">Database Lokal · </span><b>${fmtAngka.format(s.tenaga_kerja)}</b> Tenaga Kerja · <b>${fmtAngka.format(s.badan_usaha)}</b> Badan Usaha`.s;
  $('#dbStat').title = 'Jumlah profil yang sudah tersimpan di database lokal';
}

/* ================================================================
   Cek massal
   ================================================================ */

const massal = { id: null, dari: 0, timer: null, jenis: 'tk' };

function hitungTeks() {
  const n = $('#teks').value.split('\n').filter((x) => x.trim().length >= 3).length;
  $('#teksHitung').textContent = `${fmtAngka.format(n)} Nama${n > 200 ? ' (melebihi batas 200)' : ''}`;
  $('#teksHitung').classList.toggle('hint--salah', n > 200);
}
$('#teks').addEventListener('input', hitungTeks);

const drop = $('#drop');
$('#file').addEventListener('change', () => {
  const f = $('#file').files[0];
  drop.classList.toggle('is-file', Boolean(f));
  $('.drop__judul', drop).textContent = f ? f.name : 'Atau Unggah File';
  $('#dropSub').textContent = f
    ? 'File ini yang akan dipakai; isi kotak teks diabaikan. Pilih ulang untuk mengganti.'
    : '.xlsx, .csv, atau .txt, maks. 2 MB. Teks pertama di tiap baris dibaca sebagai nama; baris judul dilewati.';
});
['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('is-over'); }));
['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, () => drop.classList.remove('is-over')));
drop.addEventListener('drop', (e) => {
  e.preventDefault();
  if (e.dataTransfer.files.length) {
    $('#file').files = e.dataTransfer.files;
    $('#file').dispatchEvent(new Event('change'));
  }
});

$('#formMassal').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = $('#massalError');
  err.hidden = true;
  const fd = new FormData();
  const jenis = $('input[name="jenis"]:checked').value;
  fd.append('jenis', jenis);
  const f = $('#file').files[0];
  if (f) fd.append('file', f);
  else fd.append('teks', $('#teks').value);
  if (!f && !$('#teks').value.trim()) { err.textContent = 'Isi daftar nama atau unggah file dulu.'; err.hidden = false; return; }

  const btn = $('#btnMulai');
  btn.classList.add('is-busy');
  const res = await api('/api/massal', { method: 'POST', body: fd });
  btn.classList.remove('is-busy');
  if (!res.ok) { err.textContent = res.data.error; err.hidden = false; return; }
  mulaiJob(res.data.id, jenis, res.data.total);
});

function mulaiJob(id, jenis, total) {
  Object.assign(massal, { id, jenis, dari: 0 });
  simpanSesi('massal', JSON.stringify({ id, jenis }));
  $('#formMassal').hidden = true;
  $('#massalJalan').hidden = false;
  $('#tblMassal tbody').innerHTML = '';
  $('#thBerlaku').textContent = jenis === 'tk' ? 'Sertifikat Berlaku' : 'SBU Berlaku';
  $('#jalanTotal').textContent = total ?? '…';
  $('#jalanSelesai').textContent = '0';
  $('#jalanStatus').textContent = 'Berjalan';
  $('#jalanPesan').textContent = '';
  $('#btnStop').hidden = false;
  $('#btnUnduh').hidden = true;
  $('#btnBaru').hidden = true;
  $('#tally').innerHTML = '';
  pantauJob();
}

function lanjutkanJob() {
  if (massal.id) return;
  const s = bacaSesi('massal');
  if (!s) return;
  try {
    const { id, jenis } = JSON.parse(s);
    if (/^[0-9a-f]{32}$/.test(id)) mulaiJob(id, jenis, null);
  } catch { simpanSesi('massal', null); }
}

async function pantauJob() {
  clearTimeout(massal.timer);
  if (!massal.id) return;
  const res = await api(`/api/massal/${massal.id}?dari=${massal.dari}`);
  if (!res.ok) {
    if (res.status === 404) {
      simpanSesi('massal', null);
      massal.id = null;
      $('#massalJalan').hidden = true;
      $('#formMassal').hidden = false;
      return;
    }
    massal.timer = setTimeout(pantauJob, 4000);
    return;
  }
  const d = res.data;
  const tbody = $('#tblMassal tbody');
  tbody.insertAdjacentHTML('beforeend', d.baris.map((b) => barisMassal(b, d.jenis).s).join(''));
  massal.dari += d.baris.length;

  $('#jalanTotal').textContent = d.total;
  $('#jalanSelesai').textContent = d.selesai;
  $('#garisIsi').style.width = `${d.total ? (d.selesai / d.total) * 100 : 0}%`;
  $('#tally').innerHTML = Object.keys(LABEL_HASIL).filter((k) => d.hitung[k])
    .map((k) => html`<li>${LABEL_HASIL[k]} <b>${d.hitung[k]}</b></li>`.s).join('');

  const selesai = d.status === 'selesai' || d.status === 'dihentikan';
  if (selesai) {
    $('#jalanStatus').textContent = d.status === 'selesai' ? 'Selesai' : 'Dihentikan';
    $('#jalanPesan').textContent = d.pesan || (d.status === 'dihentikan' ? 'Dihentikan sebelum semua nama dicek. Hasil yang sudah ada tetap bisa diunduh.' : '');
    $('#btnStop').hidden = true;
    $('#btnBaru').hidden = false;
    if (d.selesai) {
      $('#btnUnduh').href = `/api/massal/${massal.id}.xlsx`;
      $('#btnUnduh').hidden = false;
    }
    muatStatistik();
    return;
  }
  massal.timer = setTimeout(pantauJob, 1500);
}

function barisMassal(b, jenis) {
  const r = b.ringkasan;
  return html`<tr>
    <td class="c-no">${String(b.no).padStart(2, '0')}</td>
    <td>${b.input}</td>
    <td><span class="cap cap--kecil cap--${b.hasil}">${LABEL_HASIL[b.hasil]}</span></td>
    <td>${b.ref ? html`<a href="#${jenis}/${b.ref}">${b.nama}</a><div class="lok-kecil lok-kecil--tetap">${jenis === 'tk' ? kapitalAwal(b.lokasi) : (b.lokasi || '')}</div>` : html`<span class="kandidat">–</span>`}</td>
    <td class="c-angka mono">${r ? html`${r.berlaku + r.segera}${r.habis ? html` <span class="kandidat">/ ${r.habis} habis</span>` : ''}` : '–'}</td>
    <td class="kandidat">${b.kandidat.length ? html`<ul>${b.kandidat.map((k) => html`<li>${k.nama}${k.lokasi ? html` · ${kapitalAwal(k.lokasi)}` : ''}</li>`)}</ul>` : '–'}</td>
  </tr>`;
}

$('#btnStop').addEventListener('click', async () => {
  if (!massal.id) return;
  $('#btnStop').classList.add('is-busy');
  await api(`/api/massal/${massal.id}/hentikan`, { method: 'POST' });
  $('#btnStop').classList.remove('is-busy');
  $('#jalanStatus').textContent = 'Menghentikan…';
});

$('#btnBaru').addEventListener('click', () => {
  clearTimeout(massal.timer);
  massal.id = null;
  simpanSesi('massal', null);
  $('#massalJalan').hidden = true;
  $('#formMassal').hidden = false;
});

/* ================================================================
   Mulai
   ================================================================ */

route();
muatStatistik();
