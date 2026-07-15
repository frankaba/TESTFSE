#!/usr/bin/env python3
"""
gen_treni.py - ferro.db -> treni.html (PWA offline, file unico).

Uso: python3 gen_treni.py ferro.db dist/treni.html [cartella_fonts]

App separata da quella automobilistica (index.html): dati diversi, DB diverso,
pubblico diverso. Condivide font e impianto offline.

Scelte di modellazione (confermate sulla fonte):
- Una COLONNA del fascicolo = un VIAGGIO unico. Il doppio numero di treno in
  testata (92332/92361) e' un cambio amministrativo del numero: il viaggiatore
  non cambia treno e non ha nulla da fare.
- Il segmento BUS (codice con lettera, es. 761B) e' una coincidenza garantita
  ma comporta TRASBORDO: l'app lo segnala esplicitamente.
- Ricerca: si mostra l'orario di PARTENZA alla salita e quello di ARRIVO alla
  discesa (non l'arrivo alla stazione di salita, che al viaggiatore non serve).
- Le note sui bus sostitutivi sono riportate come avvisi, senza logica attiva:
  nei giorni indicati l'orario resta quello del fascicolo.

Il tipo giorno (feriale/festivo) e' dedotto dalla data: domenica e le festivita'
nazionali che cadono nel periodo di validita' sono FESTIVO, il resto FERIALE.
"""

import base64
import datetime as dt
import json
import os
import sqlite3
import sys

FONTS = [
    ("Plex", 400, "ibm-plex-sans-latin-400-normal.woff2"),
    ("Plex", 600, "ibm-plex-sans-latin-600-normal.woff2"),
    ("Plex", 700, "ibm-plex-sans-latin-700-normal.woff2"),
    ("PlexMono", 400, "ibm-plex-mono-latin-400-normal.woff2"),
    ("PlexMono", 600, "ibm-plex-mono-latin-600-normal.woff2"),
]

# festivita' nazionali (mese, giorno) che possono cadere nel periodo estivo
FESTIVI_FISSI = [(1, 1), (1, 6), (4, 25), (5, 1), (6, 2), (8, 15),
                 (11, 1), (12, 8), (12, 25), (12, 26)]


def css_fonts(cartella, esito):
    if not cartella or not os.path.isdir(cartella):
        return "/* font di sistema: cartella fonts non trovata */"
    out = []
    for fam, peso, nome in FONTS:
        p = os.path.join(cartella, nome)
        if not os.path.exists(p):
            continue
        dati = open(p, "rb").read()
        if dati[:4] != b"wOF2":
            raise SystemExit("font non valido (atteso woff2): %s" % p)
        b64 = base64.b64encode(dati).decode("ascii")
        out.append(
            "@font-face{font-family:'%s';font-style:normal;font-weight:%d;"
            "font-display:swap;src:url(data:font/woff2;base64,%s) format('woff2')}"
            % (fam, peso, b64))
        esito.append(nome)
    return "\n".join(out)


def costruisci_dati(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    staz = [r["nome"] for r in con.execute(
        "SELECT nome FROM ferro_stazioni ORDER BY id")]
    sid2idx = {r["id"]: i for i, r in enumerate(
        con.execute("SELECT id FROM ferro_stazioni ORDER BY id"))}

    tab, tid2idx = [], {}
    for i, r in enumerate(con.execute(
            "SELECT id, linea, tipo_giorno, dal, al FROM ferro_tabelle ORDER BY id")):
        tid2idx[r["id"]] = i
        # "Feriale" e "Festivo" hanno la stessa iniziale: codifico L/F
        tg = "F" if r["tipo_giorno"].lower().startswith("festiv") else "L"
        tab.append([r["linea"], tg, r["dal"], r["al"]])

    segs = {}
    for r in con.execute("""SELECT corsa_id, codice, mezzo, da_stazione_id
                            FROM ferro_segmenti ORDER BY corsa_id, sequenza"""):
        segs.setdefault(r["corsa_id"], []).append(
            [r["codice"], 1 if r["mezzo"] == "bus" else 0,
             sid2idx.get(r["da_stazione_id"], -1)])

    trans = {}
    for r in con.execute("""SELECT corsa_id, stazione_id, arrivo_min, partenza_min
                            FROM ferro_transiti ORDER BY corsa_id, sequenza"""):
        trans.setdefault(r["corsa_id"], []).append(
            [sid2idx[r["stazione_id"]], r["arrivo_min"], r["partenza_min"]])

    corse = []
    ripetuti = 0
    for r in con.execute("SELECT id, tabella_id, provenienza FROM ferro_corse"
                         " ORDER BY id"):
        tr = trans.get(r["id"], [])
        # il fascicolo ripete la riga della stazione di diramazione (Nardo'
        # Centrale): nel DB resta, qui la collasso, all'utente non serve vederla
        # due volta di fila.
        puliti = []
        for t in tr:
            if puliti and puliti[-1] == t:
                ripetuti += 1
                continue
            puliti.append(t)
        corse.append([tid2idx[r["tabella_id"]], segs.get(r["id"], []), puliti,
                      r["provenienza"] or ""])

    # fermate stradali dei bus, PER TABELLA (per direzione: a Noci l'andata
    # ferma in un punto, il ritorno in quattro). Testi deduplicati.
    ftxt, fidx = [], {}
    fbus = {}
    for r in con.execute("SELECT tabella_id, stazione_id, testo"
                         " FROM ferro_fermate_bus"):
        if r["stazione_id"] not in sid2idx or r["tabella_id"] not in tid2idx:
            continue
        k = fidx.get(r["testo"])
        if k is None:
            k = fidx[r["testo"]] = len(ftxt)
            ftxt.append(r["testo"])
        fbus.setdefault(tid2idx[r["tabella_id"]], {})[
            sid2idx[r["stazione_id"]]] = k

    note = []
    for r in con.execute("SELECT DISTINCT tabella_id, testo FROM ferro_note"):
        note.append([tid2idx[r["tabella_id"]], r["testo"]])

    meta = dict(con.execute("SELECT chiave, valore FROM ferro_meta"))
    dal, al = meta.get("ferro_dal", ""), meta.get("ferro_al", "")

    festivi = []
    if dal and al:
        d0 = dt.date.fromisoformat(dal)
        d1 = dt.date.fromisoformat(al)
        for (m, g) in FESTIVI_FISSI:
            for anno in (d0.year, d1.year):
                try:
                    d = dt.date(anno, m, g)
                except ValueError:
                    continue
                if d0 <= d <= d1:
                    festivi.append(d.isoformat())
    con.close()
    return {"staz": staz, "tab": tab, "corse": corse, "note": note,
            "fbus": fbus, "ftxt": ftxt,
            "dal": dal, "al": al, "festivi": sorted(set(festivi))}, ripetuti


HTML = r"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#DB142D">
<title>Orari FSE &middot; treni</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Treni FSE">
<meta name="description" content="Orario dei treni FSE, consultabile offline.">
<style>
__FONTS__
:root{
  --rosso:#DB142D; --rosso-cupo:#8E0D1D; --rosso-tenue:#FCEEF0; --rosso-filo:#F4BDC4;
  --inchiostro:#14161A; --grigio:#5C626C; --grigio-2:#8B919B;
  --filo:#E2E5EA; --filo-2:#EEF0F3; --fondo:#FFFFFF; --fondo-2:#F7F8FA;
  --sost:#44546A; --sost-tenue:#EEF1F5; --sost-filo:#D7DDE6;
  --sans:'Plex',system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --mono:'PlexMono',ui-monospace,"SF Mono",Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--fondo);color:var(--inchiostro);
  font-family:var(--sans);font-size:16px;line-height:1.5}
.foglio{max-width:800px;margin:0 auto;padding:0 22px}
.testata{background:var(--rosso);color:#fff;padding:26px 0 22px}
.marchio{display:flex;align-items:center;gap:10px;font-size:11px;font-weight:600;
  letter-spacing:.14em;text-transform:uppercase;color:rgba(255,255,255,.85)}
.marchio:before{content:"";width:24px;height:3px;background:#fff;flex:none}
h1{font-size:clamp(28px,6.4vw,38px);font-weight:700;letter-spacing:-.022em;
  line-height:1.1;margin:12px 0 0}
.vigenza{font-family:var(--mono);font-size:12px;color:rgba(255,255,255,.85);margin:8px 0 0}
nav{display:flex;gap:4px;border-bottom:1px solid var(--filo);margin:0 0 26px}
nav button{appearance:none;background:none;border:0;cursor:pointer;font-family:inherit;
  font-size:14px;font-weight:600;color:var(--grigio);padding:14px 14px 12px;
  border-bottom:2px solid transparent;margin-bottom:-1px}
nav button[aria-selected=true]{color:var(--rosso);border-bottom-color:var(--rosso)}
label{display:block;font-size:11px;font-weight:600;letter-spacing:.1em;
  text-transform:uppercase;color:var(--grigio);margin:0 0 6px}
input,select{width:100%;padding:12px 13px;font:inherit;color:var(--inchiostro);
  background:var(--fondo);border:1px solid var(--filo);border-radius:8px}
input:focus,select:focus{outline:2px solid var(--rosso-filo);border-color:var(--rosso)}
.campo{position:relative;margin:0 0 16px}
.riga2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.proposte{position:absolute;z-index:9;left:0;right:0;top:100%;margin-top:4px;
  background:#fff;border:1px solid var(--filo);border-radius:8px;overflow:hidden;
  box-shadow:0 8px 24px rgba(20,22,26,.12);max-height:260px;overflow-y:auto}
.proposte button{display:block;width:100%;text-align:left;padding:11px 13px;
  border:0;background:none;font:inherit;cursor:pointer}
.proposte button:hover,.proposte button.sel{background:var(--rosso-tenue)}
.azione{width:100%;padding:14px;border:0;border-radius:8px;background:var(--rosso);
  color:#fff;font:inherit;font-weight:600;cursor:pointer}
.azione:hover{background:var(--rosso-cupo)}
.esito{margin:26px 0 0}
.corsa{border:1px solid var(--filo);border-radius:10px;padding:14px 16px;margin:0 0 10px}
.ore{font-family:var(--mono);font-size:20px;font-weight:600;letter-spacing:-.01em}
.durata{font-family:var(--mono);font-size:12px;color:var(--grigio-2)}
.dett{font-size:13px;color:var(--grigio);margin:6px 0 0}
.mezzi{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 0}
.pill{font-family:var(--mono);font-size:16px;font-weight:700;padding:5px 13px;
  border-radius:999px;border:1.5px solid var(--filo);color:var(--inchiostro);
  display:inline-flex;align-items:center;gap:6px;background:var(--fondo-2)}
.pill b{font-weight:700;letter-spacing:-.01em}   /* stesso peso degli orari */
.tag{font-size:10.5px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;
  color:var(--grigio);border:1px solid var(--filo);padding:3px 9px;
  border-radius:999px;align-self:center}
.pill.mez{gap:2px;padding:4px 9px}
.pill.mez .piu{font-weight:700;color:var(--grigio);margin:0 1px;font-size:12px}
.ico{width:16px;height:16px;flex:none;vertical-align:-3px}
.tl-stacco .ico{width:14px;height:14px;vertical-align:-3px;margin-right:2px}
.pill.bus{background:var(--sost-tenue);border-color:var(--sost-filo);color:var(--sost)}
.avviso{margin:10px 0 0;padding:9px 11px;border-radius:8px;font-size:13px;
  background:var(--sost-tenue);border:1px solid var(--sost-filo);color:var(--sost)}
.nota{margin:0 0 10px;padding:12px 14px;border-radius:8px;font-size:14px;
  background:var(--sost-tenue);border:1px solid var(--sost-filo);color:var(--sost)}
.vuoto{padding:22px;text-align:center;color:var(--grigio);background:var(--fondo-2);
  border-radius:10px}
.fermate{margin:10px 0 0;border-top:1px solid var(--filo-2);padding-top:10px}
.fermate summary{cursor:pointer;list-style:none;display:flex;flex-wrap:wrap;
  align-items:center;gap:6px}
.fermate summary::-webkit-details-marker{display:none}
.fermate summary .freccia{width:0;height:0;border-left:5px solid var(--grigio-2);
  border-top:4px solid transparent;border-bottom:4px solid transparent;
  margin-left:2px;transition:transform .12s}
.fermate[open] summary .freccia{transform:rotate(90deg)}
.fermate summary:hover .pill{border-color:var(--rosso)}

/* capo scheda a due colonne, come il canale ufficiale */
.viaggio{display:flex;align-items:flex-start;gap:12px}
.v-lato{min-width:0}
.v-lato .ore{display:block}
.v-staz{display:block;font-size:13px;color:var(--grigio);line-height:1.25;margin-top:1px}
.v-tratta{flex:1 1 56px;min-width:56px;align-self:center;text-align:center;
  border-bottom:1px dashed var(--filo);padding:0 4px 3px}
.v-tratta .durata{display:block;white-space:normal}
.corsa .viaggio .v-tratta{border-color:var(--filo)}

/* percorso a linea verticale */
.tl{margin:14px 0 2px}
.tl-riga{display:flex;gap:0;align-items:stretch}
.tl-ore{width:56px;flex:none;text-align:right;padding-right:12px;
  font-family:var(--mono);font-size:13px;color:var(--inchiostro);line-height:1.4;
  padding-top:1px}
.tl-filo{width:18px;flex:none;position:relative}
.tl-filo:before{content:"";position:absolute;left:8px;top:0;bottom:0;width:3px;
  background:var(--rosso)}
.tl-riga.inbus .tl-filo:before{background:var(--sost)}
.tl-riga:first-child .tl-filo:before{top:7px}
.tl-riga:last-child .tl-filo:before{bottom:auto;height:7px}
.tl-pallino{position:absolute;left:5px;top:5px;width:9px;height:9px;
  border-radius:50%;background:var(--fondo);border:2px solid var(--rosso)}
.tl-riga.inbus .tl-pallino{border-color:var(--sost)}
.tl-riga.estremo .tl-pallino{background:var(--rosso)}
.tl-riga.estremo.inbus .tl-pallino{background:var(--sost)}
.tl-nome{padding:0 0 30px 12px;font-size:14.5px;min-width:0;line-height:1.35}
.tl-fbus{display:block;font-size:12px;color:var(--grigio-2);margin-top:2px;
  font-weight:400}
.tl-riga.estremo .tl-nome{font-weight:700;font-size:15px}
.tl-riga:last-child .tl-nome{padding-bottom:2px}
.tl-stacco{margin:0 0 16px 86px;padding:8px 11px;border-radius:8px;
  background:var(--sost-tenue);border:1px solid var(--sost-filo);
  color:var(--sost);font-size:12.5px}
.pill.mini{font-size:9px;padding:1px 5px;vertical-align:1px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--filo-2)}
th{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--grigio)}
td.o{font-family:var(--mono);white-space:nowrap}
footer{margin:44px 0 30px;padding:18px 0 0;border-top:1px solid var(--filo);
  font-size:12px;color:var(--grigio-2)}
[hidden]{display:none!important}
@media print{
  /* i browser non stampano gli sfondi: senza queste regole la testata
     sarebbe testo bianco su carta bianca */
  .testata{background:none;color:var(--inchiostro);padding:10px 0}
  .marchio{color:var(--inchiostro)}
  .marchio:before{background:var(--inchiostro)}
  .vigenza{color:var(--grigio)}
  nav,.azione,.proposte{display:none}
  .corsa{break-inside:avoid}
}
</style>
</head>
<body>
<header class="testata"><div class="foglio">
  <div class="marchio">Ferrovie del Sud Est</div>
  <h1>Orari treni</h1>
  <p class="vigenza" id="vigenza"></p>
</div></header>

<main class="foglio">
  <nav>
    <button id="t-cerca" aria-selected="true">Cerca</button>
    <button id="t-staz" aria-selected="false">Stazione</button>
    <button id="t-corsa" aria-selected="false">Treno</button>
    <button id="t-note" aria-selected="false">Avvisi</button>
  </nav>

  <section id="p-cerca">
    <div class="campo">
      <label for="da">Da</label>
      <input id="da" autocomplete="off" placeholder="stazione di partenza">
      <div class="proposte" id="pr-da" hidden></div>
    </div>
    <div class="campo">
      <label for="a">A</label>
      <input id="a" autocomplete="off" placeholder="stazione di arrivo">
      <div class="proposte" id="pr-a" hidden></div>
    </div>
    <div class="riga2">
      <div class="campo"><label for="data">Giorno</label><input id="data" type="date"></div>
      <div class="campo"><label for="ora">Dalle</label><input id="ora" type="time" step="3600"></div>
    </div>
    <button class="azione" id="vai">Cerca</button>
    <div class="esito" id="esito"></div>
  </section>

  <section id="p-staz" hidden>
    <div class="campo">
      <label for="st">Stazione</label>
      <input id="st" autocomplete="off" placeholder="nome della stazione">
      <div class="proposte" id="pr-st" hidden></div>
    </div>
    <div class="riga2">
      <div class="campo"><label for="data2">Giorno</label><input id="data2" type="date"></div>
      <div class="campo"><label for="ora2">Dalle</label><input id="ora2" type="time" step="3600"></div>
    </div>
    <div class="esito" id="esito2"></div>
  </section>

  <section id="p-corsa" hidden>
    <div class="campo">
      <label for="cod">Numero treno o bus</label>
      <input id="cod" autocomplete="off" inputmode="text"
             placeholder="es. 92151 oppure 109B">
      <div class="proposte" id="pr-cod" hidden></div>
    </div>
    <div class="esito" id="esito3"></div>
  </section>

  <section id="p-note" hidden>
    <div id="elenco-note"></div>
  </section>

  <footer>
    Dati dai fascicoli orario FSE. Gli orari possono cambiare: per il servizio
    effettivo fa fede la comunicazione ufficiale FSE. App non ufficiale,
    funziona senza rete.
  </footer>
</main>

<script>
const D = __DATI__;
const NOMI = D.staz;

/* icone inline (nessuna risorsa esterna): treno e autobus, currentColor */
const ICO_TRENO = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">' +
  '<path fill="currentColor" d="M12 2c-4 0-8 .5-8 4v9.5C4 17.4 5.6 19 7.5 19L6 20.5v.5h2.2l2-2h3.6l2 2H18v-.5L16.5 19c1.9 0 3.5-1.6 3.5-3.5V6c0-3.5-4-4-8-4zM7.5 17c-.8 0-1.5-.7-1.5-1.5S6.7 14 7.5 14s1.5.7 1.5 1.5S8.3 17 7.5 17zm3.5-6H6V6h5v5zm2 0V6h5v5h-5zm3.5 6c-.8 0-1.5-.7-1.5-1.5s.7-1.5 1.5-1.5 1.5.7 1.5 1.5-.7 1.5-1.5 1.5z"/></svg>';
const ICO_BUS = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">' +
  '<path fill="currentColor" d="M4 16c0 .9.4 1.7 1 2.2V20c0 .6.4 1 1 1h1c.6 0 1-.4 1-1v-1h8v1c0 .6.4 1 1 1h1c.6 0 1-.4 1-1v-1.8c.6-.5 1-1.3 1-2.2V6c0-3.5-3.6-4-8-4S4 2.5 4 6v10zm3.5 1c-.8 0-1.5-.7-1.5-1.5S6.7 14 7.5 14s1.5.7 1.5 1.5S8.3 17 7.5 17zm9 0c-.8 0-1.5-.7-1.5-1.5s.7-1.5 1.5-1.5 1.5.7 1.5 1.5-.7 1.5-1.5 1.5zM18 11H6V6h12v5z"/></svg>';
const ico = bus => bus ? ICO_BUS : ICO_TRENO;

const hm = m => m == null ? "--:--"
  : String(Math.floor(m / 60) % 24).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0");
const durata = m => (m >= 60 ? Math.floor(m / 60) + "h " : "") + (m % 60) + "m";

/* Il fascicolo distingue FERIALE e FESTIVO. Festivo = domenica o festivita'
   nazionale che cade nel periodo di validita'. */
function tipoGiorno(iso) {
  const d = new Date(iso + "T00:00:00");
  if (d.getDay() === 0) return "F";
  return D.festivi.includes(iso) ? "F" : "L";
}
</script>
<script>
/* --- ricerca ---------------------------------------------------------- */
function tabOk(ti, iso) {
  const t = D.tab[ti];
  if (iso < t[2] || iso > t[3]) return false;
  return t[1] === tipoGiorno(iso);
}

function cerca(iDa, iA, iso, minOra) {
  const res = [];
  for (const c of D.corse) {
    if (!tabOk(c[0], iso)) continue;
    const tr = c[2];
    let s = -1, e = -1;
    for (let i = 0; i < tr.length; i++) {
      if (s < 0 && tr[i][0] === iDa && tr[i][2] != null) s = i;   /* deve partire */
      else if (s >= 0 && tr[i][0] === iA) { e = i; break; }
    }
    if (s < 0 || e < 0) continue;
    const part = tr[s][2], arr = tr[e][1] != null ? tr[e][1] : tr[e][2];
    if (part == null || arr == null || part < minOra) continue;
    /* trasbordo: un segmento bus il cui confine cade dentro il tratto percorso */
    const segs = c[1];
    const perc = tr.slice(s, e + 1).map(x => x[0]);
    const cambi = [];
    for (const sg of segs) {
      if (sg[1] === 1 && sg[2] >= 0 && perc.includes(sg[2])) cambi.push(sg[2]);
    }
    res.push({ part, arr, dur: (arr - part + 1440) % 1440, tab: c[0],
               segs, cambi, prov: c[3], ferm: e - s, c, s, e });
  }
  res.sort((x, y) => x.part - y.part);
  return res;
}

/* Alcune relazioni importanti non hanno corse dirette: da Bari a Taranto si
   cambia treno a Martina Franca (il fascicolo le tiene su colonne diverse).
   Cerco quindi anche le soluzioni con UN cambio: due corse distinte, stessa
   stazione, con un margine di coincidenza ragionevole. */
const CAMBIO_MIN = 5, CAMBIO_MAX = 120;

function corseValide(iso) {
  return D.corse.filter(c => tabOk(c[0], iso));
}

function cercaCambio(iDa, iA, iso, minOra) {
  const valide = corseValide(iso);
  /* prima gamba: da iDa a una qualsiasi stazione X. Se la corsa arriva anche
     a destinazione, e' gia' un diretto: scendere prima e aspettare non puo'
     che peggiorare, quindi non la uso come prima gamba. */
  const gamba1 = [];
  for (const c of valide) {
    const tr = c[2];
    const s = tr.findIndex(x => x[0] === iDa && x[2] != null);
    if (s < 0 || tr[s][2] < minOra) continue;
    if (tr.some((x, i) => i > s && x[0] === iA)) continue;
    for (let i = s + 1; i < tr.length; i++) {
      const arr = tr[i][1] != null ? tr[i][1] : tr[i][2];
      if (arr == null) continue;
      gamba1.push({ x: tr[i][0], part: tr[s][2], arr, c, s, i });
    }
  }
  /* seconda gamba: da X a iA, dopo l'arrivo della prima */
  const sol = [];
  for (const g of gamba1) {
    for (const c2 of valide) {
      if (c2 === g.c) continue;
      const tr = c2[2];
      const s2 = tr.findIndex(x => x[0] === g.x && x[2] != null);
      if (s2 < 0) continue;
      /* se la seconda corsa serve anche la stazione di PARTENZA prima del
         punto di cambio, la coppia non ha senso: si sale direttamente su
         quella dal capolinea, senza aspettare a meta' strada */
      if (tr.slice(0, s2).some(x => x[0] === iDa && x[2] != null)) continue;
      const attesa = tr[s2][2] - g.arr;
      if (attesa < CAMBIO_MIN || attesa > CAMBIO_MAX) continue;
      let e2 = -1;
      for (let i = s2 + 1; i < tr.length; i++) if (tr[i][0] === iA) { e2 = i; break; }
      if (e2 < 0) continue;
      const arr2 = tr[e2][1] != null ? tr[e2][1] : tr[e2][2];
      if (arr2 == null) continue;
      sol.push({ part: g.part, arr: arr2, dur: (arr2 - g.part + 1440) % 1440,
                 x: g.x, attesa,
                 t1: { tab: g.c[0], segs: g.c[1], part: g.part, arr: g.arr,
                       cambi: trasbordi(g.c, g.s, g.i), c: g.c, s: g.s, e: g.i },
                 t2: { tab: c2[0], segs: c2[1], part: tr[s2][2], arr: arr2,
                       cambi: trasbordi(c2, s2, e2), c: c2, s: s2, e: e2 } });
    }
  }
  /* una sola proposta per orario di partenza: la piu' rapida */
  const best = new Map();
  for (const s of sol) {
    const k = s.part;
    if (!best.has(k) || best.get(k).arr > s.arr) best.set(k, s);
  }
  return [...best.values()].sort((a, b) => a.part - b.part).slice(0, 6);
}

function trasbordi(c, s, e) {
  const perc = c[2].slice(s, e + 1).map(x => x[0]);
  return c[1].filter(sg => sg[1] === 1 && sg[2] >= 0 && perc.includes(sg[2]))
             .map(sg => sg[2]);
}

function pillole(segs) {
  return segs.map(s => '<span class="pill' + (s[1] ? ' bus' : '') + '">' +
    (s[1] ? 'BUS ' : '') + s[0] + '</span>').join("");
}

function schedaCambio(r) {
  const g = t => {
    const mz = mezziTratto(t.c, t.s, t.e);
    return '<p class="dett"><b>' + hm(t.part) + ' ' + NOMI[t.c[2][t.s][0]] +
      ' &rarr; ' + hm(t.arr) + ' ' + NOMI[t.c[2][t.e][0]] + '</b> &middot; linea ' +
      D.tab[t.tab][0] + targa(mz) +
      '</p>' + fermate(t.c, t.s, t.e, pilloleTratto(mz) + (t.primo ? tag('1 cambio') : ''));
  };
  r.t1.primo = true;
  return '<div class="corsa">' +
    capoViaggio(hm(r.part), NOMI[r.t1.c[2][r.t1.s][0]],
                durata(r.dur) + ' &middot; 1 cambio',
                hm(r.arr), NOMI[r.t2.c[2][r.t2.e][0]]) +
    g(r.t1) +
    '<div class="avviso"><b>Cambio a ' + NOMI[r.x] + '</b>: ' + r.attesa +
    ' minuti di attesa. La coincidenza non e\' garantita dall\'orario ufficiale: ' +
    'se il primo treno ritarda, il secondo puo\' partire lo stesso.</div>' +
    g(r.t2) + '</div>';
}

/* A quale segmento appartiene ogni transito. Attenzione: i due numeri di
   testata (92332/92361) NON dichiarano un confine, perche' il cambio di numero
   e' amministrativo e al viaggiatore non cambia nulla. Il confine lo dichiara
   solo il segmento che subentra davvero (il bus): quindi salto i segmenti senza
   confine e avanzo al primo che ne ha uno. */
function segDiTransito(c) {
  const tr = c[2], segs = c[1], out = [];
  let k = 0;
  for (let i = 0; i < tr.length; i++) {
    let j = k + 1;
    while (j < segs.length && segs[j][2] < 0) j++;
    if (j < segs.length && tr[i][0] === segs[j][2]) k = j;
    out.push(k);
  }
  return out;
}

/* Che cosa deve sapere chi sale a s e scende a e. Conta il TRATTO percorso, non
   la corsa intera. Tre casi:
     - tutto treno         -> niente da dire
     - treno e bus insieme -> c'e' un trasbordo, e va detto DOVE
     - tutto autobus       -> il tratto e' servito da bus anche se dentro non
                              c'e' nessun cambio: capita salendo a valle del
                              punto di trasbordo (Casarano -> Gagliano). E'
                              il caso che sfugge se si guardano solo i confini
                              che cadono dentro il tratto. */
function mezziTratto(c, s, e) {
  /* Contano i segmenti su cui si VIAGGIA: quello attivo alla partenza di ogni
     fermata da s a e-1. Il confine che cade esattamente sulla stazione di
     discesa non e' un cambio per chi scende li' (Bari -> Putignano sulla
     109B/92151 e' un viaggio tutto in autobus: il treno parte da Putignano,
     ma il viaggiatore e' gia' sceso). */
  const tr = c[2], segs = c[1], sd = segDiTransito(c);
  const cambi = [];
  let bus = false, treno = false;
  const codici = [];
  for (let i = s; i < e; i++) {
    const sg = segs[sd[i]] || ["", 0, -1];
    if (sg[1]) bus = true; else treno = true;
    if (!codici.some(x => x[0] === sg[0])) codici.push(sg);
    if (i > s && sd[i] !== sd[i - 1]) {
      const pr = segs[sd[i - 1]] || ["", 0, -1];
      /* cambio di mezzo, oppure cambio di autobus (115B -> 383B): in
         entrambi i casi si scende e si sale su un altro veicolo */
      if (sg[1] !== pr[1] || (sg[1] && pr[1]))
        cambi.push({ st: tr[i][0], da: pr, a: sg });
    }
  }
  return { bus, treno, cambi, codici,
           misto: bus && treno, soloBus: bus && !treno };
}

const tag = t => '<span class="tag">' + t + '</span>';

/* etichetta sintetica, visibile senza aprire il percorso */
/* targa = composizione dei mezzi del tratto, in ordine di viaggio:
   un bus -> icona bus; due bus -> bus+bus; bus e treno -> bus+treno */
function targa(m) {
  if (!m.codici.length) return "";
  return ' <span class="pill mez">' +
    m.codici.map(sg => ico(sg[1])).join('<span class="piu">+</span>') +
    '</span>';
}

function pilloleTratto(m) {
  return m.codici.map(sg => '<span class="pill' + (sg[1] ? ' bus' : '') + '">' +
    ico(sg[1]) + '<b>' + sg[0] + '</b></span>').join("");
}

/* elenco delle fermate fra salita e discesa, con l'orario di ciascuna */
function fermate(c, s, e, somm) {
  const tr = c[2], segs = c[1], sd = segDiTransito(c);
  const fb = D.fbus[c[0]] || {};
  const riga = (ore, idx, sg, capo) =>
    '<div class="tl-riga' + (capo ? ' estremo' : '') + (sg[1] ? ' inbus' : '') + '">' +
    '<div class="tl-ore">' + ore + '</div>' +
    '<div class="tl-filo"><span class="tl-pallino"></span></div>' +
    '<div class="tl-nome">' + NOMI[idx] +
    (sg[1] && fb[idx] != null
      ? '<span class="tl-fbus">fermata bus: ' + D.ftxt[fb[idx]] + '</span>' : '') +
    '</div></div>';
  let righe = "";
  for (let i = s; i <= e; i++) {
    /* alla discesa (i==e) il mezzo mostrato e' quello con cui ci si ARRIVA:
       il segmento che parte proprio da li' non riguarda chi scende */
    const sg = segs[sd[i === e ? Math.max(s, e - 1) : i]] || ["", 0, -1];
    const prec = i > s ? (segs[sd[i - 1]] || ["", 0, -1]) : null;
    const snodo = i < e && prec && sd[i] !== sd[i - 1] &&
                  (sg[1] !== prec[1] || (sg[1] && prec[1]));
    const a = (i === s) ? null : tr[i][1];
    const p = (i === e) ? null : tr[i][2];
    if (snodo) {
      /* allo snodo la stazione compare due volte: l'ARRIVO col mezzo che
         finisce, poi lo stacco, poi la RIPARTENZA col mezzo che comincia */
      if (a != null) righe += riga(hm(a), tr[i][0], prec, true);
      righe += '<div class="tl-stacco">' + (prec[1] && sg[1]
        ? ICO_BUS + ' Qui si cambia autobus: dal <b>' + prec[0] +
          '</b> al <b>' + sg[0] + '</b>. La coincidenza e\' garantita.'
        : sg[1]
        ? ICO_BUS + ' Qui si scende dal treno e si prosegue in autobus (<b>' + sg[0] +
          '</b>): la coincidenza e\' garantita.'
        : ICO_TRENO + ' Qui si scende dall\'autobus e si prosegue in treno (<b>' + sg[0] + '</b>).') +
        '</div>';
      righe += riga(p != null ? hm(p) : "", tr[i][0], sg, true);
      continue;
    }
    let ore = "";
    if (a != null && p != null && a !== p) ore = hm(a) + "<br>" + hm(p);
    else if (a != null || p != null) ore = hm(a != null ? a : p);
    righe += riga(ore, tr[i][0], sg, i === s || i === e);
  }
  return '<details class="fermate"><summary>' + somm +
    '<span class="freccia" aria-hidden="true"></span></summary>' +
    '<div class="tl">' + righe + '</div></details>';
}

/* capo scheda come sul canale ufficiale: orario grande e stazione sotto, da
   entrambi i lati, durata al centro sulla linea tratteggiata */
function capoViaggio(op, sp, dur, oa, sa) {
  return '<div class="viaggio">' +
    '<div class="v-lato"><span class="ore">' + op + '</span>' +
    '<span class="v-staz">' + sp + '</span></div>' +
    '<div class="v-tratta"><span class="durata">' + dur + '</span></div>' +
    '<div class="v-lato v-arr"><span class="ore">' + oa + '</span>' +
    '<span class="v-staz">' + sa + '</span></div></div>';
}

function scheda(r) {
  const t = D.tab[r.tab];
  /* i codici che contano sono quelli del tratto percorso: chi sale a valle del
     trasbordo non viaggia sul primo treno, e non gli va mostrato */
  const m = mezziTratto(r.c, r.s, r.e);
  const tr = r.c[2];
  return '<div class="corsa">' +
    capoViaggio(hm(r.part), NOMI[tr[r.s][0]], durata(r.dur),
                hm(r.arr), NOMI[tr[r.e][0]]) +
    '<p class="dett">Linea ' + t[0] + ' &middot; ' + (t[1] === 'F' ? 'festivo' : 'feriale') +
    (r.prov ? ' &middot; da ' + r.prov : '') + targa(m) + '</p>' +
    fermate(r.c, r.s, r.e, pilloleTratto(m)) + '</div>';
}
</script>
<script>
/* --- interfaccia ------------------------------------------------------ */
const $ = id => document.getElementById(id);
const norm = s => s.toLowerCase().replace(/[^a-z0-9]/g, "");
/* l'unico testo NON proveniente dai fascicoli che finisce in innerHTML e'
   il numero digitato nella scheda Corsa: va escapato */
const esc = s => s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
                  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

/* stazioni principali proposte a campo vuoto, nell'ordine voluto */
const PREFERITE = ["BARI CENTRO", "LECCE", "TARANTO", "PUTIGNANO",
                   "MARTINA FRANCA"]
  .map(n => [n, NOMI.indexOf(n)]).filter(([, i]) => i >= 0);

function completa(inp, box, quando) {
  let sel = -1;
  const chiudi = () => { box.hidden = true; sel = -1; };
  const mostra = cand => {
    if (!cand.length) return chiudi();
    box.innerHTML = cand.map(([n, i]) =>
      '<button type="button" data-i="' + i + '">' + n + '</button>').join("");
    box.hidden = false;
    box.querySelectorAll("button").forEach(b => b.onclick = () => {
      inp.value = NOMI[+b.dataset.i]; inp.dataset.i = b.dataset.i;
      chiudi(); if (quando) quando();
    });
  };
  inp.addEventListener("input", () => {
    const q = norm(inp.value);
    if (!q) return mostra(PREFERITE);
    mostra(NOMI.map((n, i) => [n, i])
      .filter(([n]) => norm(n).includes(q)).slice(0, 8));
  });
  /* campo vuoto: al tocco propongo subito le stazioni principali */
  inp.addEventListener("focus", () => {
    if (!norm(inp.value)) mostra(PREFERITE);
  });
  inp.addEventListener("keydown", e => {
    const bs = [...box.querySelectorAll("button")];
    if (!bs.length || box.hidden) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      sel = (sel + (e.key === "ArrowDown" ? 1 : -1) + bs.length) % bs.length;
      bs.forEach(b => b.classList.remove("sel"));
      bs[sel].classList.add("sel"); bs[sel].scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter" && sel >= 0) { e.preventDefault(); bs[sel].click(); }
    else if (e.key === "Escape") chiudi();
  });
  inp.addEventListener("blur", () => setTimeout(chiudi, 140));
}

function indiceDi(inp) {
  const i = NOMI.findIndex(n => n === inp.value);
  return i;
}

completa($("da"), $("pr-da"));
completa($("a"), $("pr-a"));
completa($("st"), $("pr-st"), () => prossime());

const oggi = new Date();
const isoOggi = new Date(oggi.getTime() - oggi.getTimezoneOffset() * 6e4)
  .toISOString().slice(0, 10);
const oraOra = String(oggi.getHours()).padStart(2, "0") + ":00";
/* il picker nativo si apre solo sull'iconcina: lo apro da tutto il campo */
for (const inp of document.querySelectorAll('input[type=date],input[type=time]')) {
  inp.style.cursor = "pointer";
  inp.addEventListener("click", () => { if (inp.showPicker) try { inp.showPicker(); } catch (e) {} });
}
$("data").value = isoOggi; $("ora").value = oraOra;
$("data2").value = isoOggi; $("ora2").value = oraOra;
/* il selettore data dei browser mobili si vincola al periodo dell'orario */
$("data").min = $("data2").min = D.dal;
$("data").max = $("data2").max = D.al;
$("vigenza").textContent = "valido dal " + D.dal.split("-").reverse().join("/") +
  " al " + D.al.split("-").reverse().join("/");

function fuoriPeriodo(iso) {
  return iso < D.dal || iso > D.al;
}

$("vai").onclick = () => {
  const iDa = indiceDi($("da")), iA = indiceDi($("a"));
  const iso = $("data").value, ora = $("ora").value;
  const box = $("esito");
  if (iDa < 0 || iA < 0) {
    box.innerHTML = '<div class="vuoto">Scegli la stazione di partenza e quella di arrivo dall\'elenco.</div>';
    return;
  }
  if (iDa === iA) {
    box.innerHTML = '<div class="vuoto">Partenza e arrivo coincidono.</div>';
    return;
  }
  if (fuoriPeriodo(iso)) {
    box.innerHTML = '<div class="vuoto">Il ' + iso.split("-").reverse().join("/") +
      ' e\' fuori dal periodo coperto da questo orario.</div>';
    return;
  }
  const m = ora ? (+ora.slice(0, 2) * 60 + +ora.slice(3, 5)) : 0;
  const res = cerca(iDa, iA, iso, m);
  let html = res.map(scheda).join("");
  /* se le corse dirette scarseggiano, propongo anche le soluzioni con un cambio
     (Bari-Taranto, per esempio, un diretto non ce l'ha proprio) */
  if (res.length < 3) {
    const cc = cercaCambio(iDa, iA, iso, m);
    if (cc.length) html += cc.map(schedaCambio).join("");
  }
  box.innerHTML = html || '<div class="vuoto">Nessun collegamento da ' +
    NOMI[iDa] + ' a ' + NOMI[iA] + ' dopo le ' + (ora || "00:00") +
    ' in questo giorno.</div>';
};

/* --- prossime partenze da una stazione -------------------------------- */
function prossime() {
  const i = indiceDi($("st"));
  const iso = $("data2").value, ora = $("ora2").value;
  const box = $("esito2");
  if (i < 0) { box.innerHTML = ""; return; }
  if (fuoriPeriodo(iso)) {
    box.innerHTML = '<div class="vuoto">Giorno fuori dal periodo dell\'orario.</div>';
    return;
  }
  const m = ora ? (+ora.slice(0, 2) * 60 + +ora.slice(3, 5)) : 0;
  const righe = [];
  for (const c of D.corse) {
    if (!tabOk(c[0], iso)) continue;
    const tr = c[2];
    const k = tr.findIndex(x => x[0] === i && x[2] != null);
    if (k < 0 || k === tr.length - 1) continue;
    if (tr[k][2] < m) continue;
    righe.push({ ora: tr[k][2], dest: NOMI[tr[tr.length - 1][0]],
                 linea: D.tab[c[0]][0], c, k, e: tr.length - 1 });
  }
  righe.sort((a, b) => a.ora - b.ora);
  if (!righe.length) {
    box.innerHTML = '<div class="vuoto">Nessuna partenza da ' + NOMI[i] +
      ' dopo le ' + (ora || "00:00") + ' in questo giorno.</div>';
    return;
  }
  /* stesso trattamento della ricerca: il mezzo va detto anche qui, e va detto
     guardando il tratto da questa stazione in poi */
  box.innerHTML = righe.map(r => {
    const mz = mezziTratto(r.c, r.k, r.e);
    const arrFin = r.c[2][r.e][1] != null ? r.c[2][r.e][1] : r.c[2][r.e][2];
    return '<div class="corsa">' +
      capoViaggio(hm(r.ora), NOMI[i], 'linea ' + r.linea, hm(arrFin), r.dest) +
      (targa(mz) ? '<p class="dett">' + targa(mz) + '</p>' : '') +
      fermate(r.c, r.k, r.e, pilloleTratto(mz)) + '</div>';
  }).join("");
}
$("data2").onchange = prossime;
$("ora2").onchange = prossime;

/* --- ricerca per numero di corsa --------------------------------------- */
const CODICI = [...new Set(D.corse.flatMap(c => c[1].map(sg => sg[0])))].sort();

function cercaCodice(q) {
  q = q.trim().toUpperCase();
  if (!q) return [];
  const out = [];
  for (const c of D.corse) {
    if (!c[1].some(sg => sg[0] === q)) continue;
    const t = D.tab[c[0]], tr = c[2];
    out.push({ c, t,
      part: tr[0][2] != null ? tr[0][2] : tr[0][1],
      arr: tr[tr.length - 1][1] != null ? tr[tr.length - 1][1]
                                        : tr[tr.length - 1][2] });
  }
  /* ordino per validita', poi tipo giorno, poi orario */
  out.sort((a, b) => a.t[2] < b.t[2] ? -1 : a.t[2] > b.t[2] ? 1
         : a.t[1] < b.t[1] ? -1 : a.t[1] > b.t[1] ? 1 : a.part - b.part);
  return out;
}

function schedaCodice(r) {
  const tr = r.c[2], e = tr.length - 1;
  const m = mezziTratto(r.c, 0, e);
  const dmy = iso => iso.split("-").reverse().join("/");
  return '<div class="corsa">' +
    capoViaggio(hm(r.part), NOMI[tr[0][0]],
                durata((r.arr - r.part + 1440) % 1440),
                hm(r.arr), NOMI[tr[e][0]]) +
    '<p class="dett">Linea ' + r.t[0] + ' &middot; ' +
    (r.t[1] === 'F' ? 'festivo' : 'feriale') + ' &middot; valida dal ' +
    dmy(r.t[2]) + ' al ' + dmy(r.t[3]) +
    (r.c[3] ? ' &middot; da ' + r.c[3] : '') + targa(m) + '</p>' +
    fermate(r.c, 0, e, pilloleTratto(m)) + '</div>';
}

function perCodice() {
  const q = $("cod").value.trim().toUpperCase();
  const box = $("esito3");
  if (!q) { box.innerHTML = ""; return; }
  const res = cercaCodice(q);
  if (!res.length) {
    box.innerHTML = '<div class="vuoto">Nessuna corsa col numero ' + esc(q) +
      ' nei fascicoli in vigore.</div>';
    return;
  }
  box.innerHTML = res.map(schedaCodice).join("");
}

(function () {
  const inp = $("cod"), box = $("pr-cod");
  let sel = -1;
  const chiudi = () => { box.hidden = true; sel = -1; };
  inp.addEventListener("input", () => {
    perCodice();
    const q = inp.value.trim().toUpperCase();
    if (!q) return chiudi();
    const cand = CODICI.filter(k => k.startsWith(q) && k !== q).slice(0, 8);
    if (!cand.length) return chiudi();
    box.innerHTML = cand.map(k =>
      '<button type="button">' + k + '</button>').join("");
    box.hidden = false;
    box.querySelectorAll("button").forEach(b => b.onclick = () => {
      inp.value = b.textContent; chiudi(); perCodice();
    });
  });
  inp.addEventListener("keydown", e => {
    const bs = [...box.querySelectorAll("button")];
    if (!bs.length || box.hidden) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      sel = (sel + (e.key === "ArrowDown" ? 1 : -1) + bs.length) % bs.length;
      bs.forEach(b => b.classList.remove("sel"));
      bs[sel].classList.add("sel");
    } else if (e.key === "Enter" && sel >= 0) { e.preventDefault(); bs[sel].click(); }
    else if (e.key === "Escape") chiudi();
  });
  inp.addEventListener("blur", () => setTimeout(chiudi, 140));
})();

/* --- avvisi ----------------------------------------------------------- */
(function () {
  const viste = [];
  for (const [ti, testo] of D.note) {
    if (!viste.includes(testo)) viste.push(testo);
  }
  $("elenco-note").innerHTML = viste.length
    ? viste.map(t => '<div class="nota">' + t + '</div>').join("") +
      '<p class="dett">Negli altri giorni vale l\'orario ordinario. Gli orari ' +
      'delle corse sostituite restano quelli del fascicolo.</p>'
    : '<div class="vuoto">Nessun avviso nel periodo.</div>';
})();

/* --- schede ----------------------------------------------------------- */
const SCHEDE = [["t-cerca", "p-cerca"], ["t-staz", "p-staz"],
                ["t-corsa", "p-corsa"], ["t-note", "p-note"]];
for (const [b, p] of SCHEDE) {
  $(b).onclick = () => {
    for (const [b2, p2] of SCHEDE) {
      $(b2).setAttribute("aria-selected", b2 === b);
      $(p2).hidden = p2 !== p;
    }
  };
}
</script>
</body>
</html>
"""


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    db, out = sys.argv[1], sys.argv[2]
    cartella = sys.argv[3] if len(sys.argv) > 3 else "fonts"

    dati, ripetuti = costruisci_dati(db)
    font_ok = []
    html = HTML.replace("__FONTS__", css_fonts(cartella, font_ok))
    html = html.replace("__DATI__", json.dumps(dati, ensure_ascii=False,
                                               separators=(",", ":")))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)

    print("scritto %s (%.0f KB)" % (out, len(html.encode("utf-8")) / 1024))
    print("stazioni   : %d" % len(dati["staz"]))
    print("corse      : %d" % len(dati["corse"]))
    print("tabelle    : %d" % len(dati["tab"]))
    print("avvisi     : %d" % len({t for _, t in dati["note"]}))
    print("festivi nel periodo: %s" % (", ".join(dati["festivi"]) or "nessuno"))
    print("transiti ripetuti collassati: %d" % ripetuti)
    if len(font_ok) == len(FONTS):
        print("font       : %d/%d incorporati da %s"
              % (len(font_ok), len(FONTS), cartella))
    else:
        print("font       : %d/%d incorporati -- l'app usera' i font di sistema"
              % (len(font_ok), len(FONTS)))


if __name__ == "__main__":
    main()
