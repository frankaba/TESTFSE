#!/usr/bin/env python3
"""
build_ferro.py - P.d.E. ferroviario FSE: PDF -> ferro.db (SQLite).

Uso:
    python3 build_ferro.py ferro.db linea_*.pdf

Struttura dei PDF (verificata sui 7 fascicoli estate 2026):
- 1 PDF per linea; dentro, N tabelle-periodo ("Linea X feriale - dal .. al ..").
  I periodi DIFFERISCONO da linea a linea. Tipo giorno: feriale | festivo.
- Griglia disegnata: find_tables(strategy='lines'). Righe = stazioni con doppia
  riga a./p. (arrivo/partenza); colonne = treni/bus.
- Mezzo: icona immagine sopra la colonna (hash bus/treno) + suffisso codice
  (lettera => bus, solo cifre => treno). Il codice e' la fonte primaria, le
  icone sono verifica (valida_ferro.py F4).
- Una colonna = UN viaggio. Puo' avere piu' codici (segmenti):
  * doppio codice in intestazione (92332/92361): cambio amministrativo del
    numero treno, NESSUN trasbordo per il viaggiatore;
  * riga-codice a meta' tabella (761B a NARDO' CENTRALE, tra la riga a. e la
    riga p.): cambio segmento reale. Se il mezzo cambia (treno->bus) c'e'
    TRASBORDO con coincidenza garantita.
- 'Provenienza' (es. OTRANTO): annotazione informativa per colonna.
- Celle '-' o vuote: la corsa non serve la stazione.
- Refusi noti del sorgente: '15.46' (punto), '22-:21', '\\' isolato. Il parser
  li normalizza; le anomalie di monotonia restano CONTATE da valida_ferro.py,
  non riparate (stessa politica del P.d.E. automobilistico).

Parsing a livello di PAROLE con coordinate (non celle): le celle a./p. sono a
volte fuse e a volte separate, e un orario singolo in una cella fusa sarebbe
ambiguo (arrivo o partenza?). Con la y della parola rispetto alla y delle
etichette 'a.'/'p.' l'ambiguita' non esiste.
"""

import datetime as dt
import hashlib
import os
import re
import sqlite3
import sys
from collections import defaultdict

import pdfplumber

SCHEMA = """
DROP TABLE IF EXISTS ferro_note;
DROP TABLE IF EXISTS ferro_punti;
DROP TABLE IF EXISTS ferro_transiti;
DROP TABLE IF EXISTS ferro_segmenti;
DROP TABLE IF EXISTS ferro_corse;
DROP TABLE IF EXISTS ferro_tabelle;
DROP TABLE IF EXISTS ferro_stazioni;
DROP TABLE IF EXISTS ferro_meta;
CREATE TABLE ferro_meta (chiave TEXT PRIMARY KEY, valore TEXT);
CREATE TABLE ferro_stazioni (id INTEGER PRIMARY KEY, nome TEXT UNIQUE);
CREATE TABLE ferro_tabelle (
    id INTEGER PRIMARY KEY, linea TEXT, tipo_giorno TEXT,  -- Feriale|Festivo
    dal TEXT, al TEXT, file TEXT, pagina INTEGER, ordine INTEGER,
    y_testata REAL);      -- y della banda codici, per il check icone
CREATE TABLE ferro_corse (
    id INTEGER PRIMARY KEY, tabella_id INTEGER REFERENCES ferro_tabelle(id),
    colonna INTEGER, provenienza TEXT, x_centro REAL);  -- x per il check icone
CREATE TABLE ferro_segmenti (
    id INTEGER PRIMARY KEY, corsa_id INTEGER REFERENCES ferro_corse(id),
    sequenza INTEGER, codice TEXT, mezzo TEXT,             -- treno|bus
    da_stazione_id INTEGER, a_stazione_id INTEGER);        -- NULL = ignoto
CREATE TABLE ferro_transiti (
    id INTEGER PRIMARY KEY, corsa_id INTEGER REFERENCES ferro_corse(id),
    stazione_id INTEGER REFERENCES ferro_stazioni(id), sequenza INTEGER,
    arrivo_min INTEGER, partenza_min INTEGER);             -- NULL = non indicato
CREATE TABLE ferro_punti (
    tabella_id INTEGER, stazione_id INTEGER, punto TEXT,
    PRIMARY KEY (tabella_id, stazione_id));
CREATE TABLE ferro_note (tabella_id INTEGER, testo TEXT);
DROP TABLE IF EXISTS ferro_fermate_bus;
CREATE TABLE ferro_fermate_bus (
    tabella_id  INTEGER NOT NULL,
    stazione_id INTEGER NOT NULL,
    testo       TEXT NOT NULL,
    PRIMARY KEY (tabella_id, stazione_id)
);
"""

MESI = {"gennaio":1,"febbraio":2,"marzo":3,"aprile":4,"maggio":5,"giugno":6,
        "luglio":7,"agosto":8,"settembre":9,"ottobre":10,"novembre":11,"dicembre":12}

RE_TITOLO = re.compile(
    r"Linea\s+([0-9]+(?:\s*bis)?(?:\s*\+\s*[0-9]+)?)\s+(feriale|festivo)\s*-\s*"
    r"dal\s+(\d{1,2})\s+([a-z]+)\s+al\s+(\d{1,2})\s+([a-z]+)(?:\s+(\d{4}))?",
    re.IGNORECASE)
RE_CODICE = re.compile(r"^[A-Z]?\d{3,5}(?:B\d?|)$")   # 92001, 901B, 941B1, F1101
RE_ORA = re.compile(r"^(\d{1,2})[:.](\d{2})$")
TAB = {"vertical_strategy": "lines", "horizontal_strategy": "lines"}

# Hub della rete (conferma di dominio, luglio 2026): negli hub cambia SEMPRE il
# numero della corsa o il mezzo. Un confine DEDOTTO puo' cadere solo qui.
# Definita a livello di modulo (unica fonte: valida_ferro.py la importa, e i
# test possono falsificarla per il contro-esempio "senza Putignano").
#   strutturali: PUTIGNANO e MARTINA FRANCA (linea 1), NOVOLI (linea 3),
#     ZOLLINO e MAGLIE (rete 6/7), CASARANO (raccordo 3/4).
#   provvisori, da lavori: FRANCAVILLA FONTANA (linea 2) e NARDO' CENTRALE
#     (linea 3). La lista li AMMETTE, non li richiede.
SNODI_AMMESSI = {"PUTIGNANO", "MARTINA FRANCA", "NOVOLI", "ZOLLINO",
                 "MAGLIE", "CASARANO",
                 "FRANCAVILLA FONTANA", "NARDO' CENTRALE"}

HASH_MEZZO = {}   # hash immagine -> 'bus'|'treno', appreso alla prima pagina utile


def norm_ora(tok):
    """'5:10'->310; tollera '15.46', '22-:21'. None se non e' un orario."""
    t = tok.strip().replace("\\", "")
    m = RE_ORA.match(t)
    if not m:
        cifre = re.sub(r"\D", "", t)
        if len(cifre) in (3, 4) and t.count(":") + t.count(".") >= 1:
            cifre = cifre.zfill(4)
            m = re.match(r"(\d{2})(\d{2})", cifre)
        else:
            return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 26 or mi > 59:
        return None
    return h * 60 + mi


def e_codice(tok):
    return bool(RE_CODICE.match(tok.strip()))


def mezzo_da_codice(codice):
    return "treno" if codice.isdigit() else "bus"


def parse_titoli(testo):
    out = []
    for m in RE_TITOLO.finditer(testo):
        linea = re.sub(r"\s+", " ", m.group(1)).strip()
        anno = m.group(7) or "2026"
        dal = dt.date(int(anno), MESI[m.group(4).lower()], int(m.group(3)))
        al = dt.date(int(anno), MESI[m.group(6).lower()], int(m.group(5)))
        out.append((linea, m.group(2).capitalize(), dal.isoformat(), al.isoformat()))
    return out


def icone_pagina(pg):
    """[(x_centro, 'bus'|'treno')] per le iconcine sopra le colonne."""
    global HASH_MEZZO
    out = []
    for im in pg.images:
        if im["width"] > 15 or im["height"] > 12:
            continue
        try:
            h = hashlib.md5(im["stream"].get_data()).hexdigest()[:8]
        except Exception:
            continue
        out.append(((im["x0"] + im["x1"]) / 2, im["top"], h))
    return out


def bande_ap(parole, x_prima_colonna):
    """Trova le etichette 'a.'/'p.' a sinistra della prima colonna orari;
    le accoppia in bande-stazione [(y_a, y_p)] in ordine di lettura."""
    lab = sorted([w for w in parole
                  if w["text"] in ("a.", "p.", "a", "p")
                  and w["x0"] < x_prima_colonna - 1
                  and x_prima_colonna - 40 < w["x0"]],   # attaccate alle colonne
                 key=lambda w: w["top"])
    lab = [dict(w, text=w["text"].rstrip(".") + ".") for w in lab]
    bande = []
    i = 0
    while i < len(lab):
        if lab[i]["text"] == "a." and i + 1 < len(lab) and lab[i+1]["text"] == "p.":
            bande.append((lab[i], lab[i+1]))
            i += 2
        elif lab[i]["text"] == "p.":       # prima stazione: a volte solo p.
            bande.append((None, lab[i]))
            i += 1
        else:                               # solo a. (ultima stazione)
            bande.append((lab[i], None))
            i += 1
    return bande


def testo_box(pg, box):
    """Testo dentro un rettangolo, tollerante ai font frammentati in char."""
    x0, top, x1, bot = box
    x0 = max(x0, pg.bbox[0]); top = max(top, pg.bbox[1])
    x1 = min(x1, pg.bbox[2]); bot = min(bot, pg.bbox[3])
    if x1 <= x0 or bot <= top:
        return ""
    try:
        return (pg.crop((x0, top, x1, bot)).extract_text() or "").strip()
    except Exception:
        return ""


def codici_da_testo(testo):
    """'109B/\\n92151' -> ['109B','92151']. Tollera i font che pdfplumber
    frammenta in caratteri ('9 2 0 8 1' -> '92081'). Scarta il rumore."""
    out = []
    for riga in testo.replace("\u00a0", " ").split("\n"):
        tok_norm = re.split(r"/", riga.replace(" ", ""))
        tok_spazi = re.split(r"[\s/]+", riga)
        # preferisce la lettura per token; se non produce nulla ma la riga
        # senza spazi si', usa quella (font frammentato)
        buoni = [t for t in tok_spazi if t and RE_CODICE.match(t)]
        if not buoni:
            buoni = [t for t in tok_norm if t and RE_CODICE.match(t)]
        out.extend(buoni)
    return out


def orari_da_char(chars_pag, box):
    """[(y_riga, minuti)] ricostruendo gli orari carattere per carattere: serve
    dove il PDF usa un font che pdfplumber non riesce a comporre in parole.
    Niente crop: pdfplumber CLIPPA le coordinate dei char a cavallo del bordo,
    e un orario finirebbe letto sia dalla stazione sopra sia da quella sotto.
    Filtro invece sul centro del carattere, con le coordinate originali."""
    x0, top, x1, bot = box
    dentro = [c for c in chars_pag
              if x0 <= (c["x0"] + c["x1"]) / 2 <= x1
              and top <= (c["top"] + c["bottom"]) / 2 <= bot]
    dentro.sort(key=lambda c: (c["top"], c["x0"]))
    # raggruppo in righe: nuova riga quando il salto verticale supera 2pt
    righe = []
    for c in dentro:
        if righe and abs(c["top"] - righe[-1][0]) <= 2:
            righe[-1][1].append(c)
        else:
            righe.append((c["top"], [c]))
    out = []
    for (y, cs) in righe:
        testo = "".join(c["text"] for c in sorted(cs, key=lambda c: c["x0"]))
        for m in re.finditer(r"\d{1,2}\s*[:.]\s*\d{2}", testo):
            mi = norm_ora(m.group(0).replace(" ", ""))
            if mi is not None:
                out.append((y, mi))
    return out


def estrai_fermate_bus(pg, blocchi):
    """Colonna 'Punti di fermata' sul lato destro: per ogni stazione, il punto
    di fermata STRADALE del bus sostitutivo, allineato verticalmente alla banda
    della stazione. Il font e' spesso frammentato ('V i a  G iu s e p p e'):
    ricostruisco dai char con lo spazio deciso dal gap reale, non dalle parole.
    Il bordo sinistro della colonna e' la fine dell'ultima colonna-corsa della
    griglia (il titolo 'Punti di fermata' e' rientrato e taglierebbe le righe).
    Restituisce una lista allineata ai blocchi: [{nome_stazione: testo}]."""
    if not any(w["text"] == "Punti" for w in pg.extract_words()):
        return [{} for _ in blocchi]
    x_col = max(c[1] for b in blocchi for c in b["colonne"]) + 2
    chars = sorted((c for c in pg.chars if c["x0"] >= x_col),
                   key=lambda c: (c["top"], c["x0"]))
    tutte = []
    for c in chars:
        if tutte and abs(c["top"] - tutte[-1][0]) <= 2:
            tutte[-1][1].append(c)
        else:
            tutte.append((c["top"], [c]))
    if not tutte:
        return [{} for _ in blocchi]
    # La colonna degli indirizzi e' allineata a sinistra: le sue righe partono
    # tutte alla stessa x. Il box delle NOTE invade la stessa fascia ma i suoi
    # frammenti partono a x diverse: la moda delle x di partenza isola la
    # colonna e scarta le code di nota.
    from collections import Counter as _C
    moda = _C(round(min(c["x0"] for c in cs))
              for (_, cs) in tutte).most_common(1)[0][0]
    # Non scarto la riga se il box-note le si incolla a sinistra ("...effettuata
    # la Via Giuseppe..."): RITAGLIO i caratteri al bordo colonna, cosi' la coda
    # di nota cade e l'indirizzo resta. La riga vale se il suo primo carattere
    # ritagliato sta sul bordo (le righe della colonna sono allineate).
    righe = []
    for (y, cs) in tutte:
        cs2 = [c for c in cs if c["x0"] >= moda - 3]
        if cs2 and min(c["x0"] for c in cs2) <= moda + 3:
            righe.append((y, cs2))

    def testo(cs):
        cs = sorted(cs, key=lambda c: c["x0"])
        out = []
        for i, c in enumerate(cs):
            if i and c["x0"] - cs[i - 1]["x1"] > 1.0:
                out.append(" ")
            out.append(c["text"])
        return re.sub(r"\s+", " ", "".join(out)).strip()

    out = []
    for b in blocchi:
        bande = [(staz[0], staz[3]) for staz in b["stazioni"]]
        acc = {}
        for (y, cs) in righe:
            # banda che contiene la riga, o la piu' vicina entro 6pt
            meglio, dist = None, 6.0
            for nome, (lo, hi) in bande:
                d = 0.0 if lo <= y <= hi else min(abs(y - lo), abs(y - hi))
                if d < dist:
                    meglio, dist = nome, d
            if meglio is None:
                continue
            fr = testo(cs)
            if fr and fr != "-" and "Punti" not in fr and "fermata" != fr:
                acc.setdefault(meglio, []).append(fr)
        # NIENTE fusione tra blocchi: le fermate valgono per la DIREZIONE del
        # blocco (a Noci l'andata ferma in un punto solo, il ritorno in
        # quattro). La virgola finale dell'andata e' formattazione della
        # fonte, non un troncamento.
        out.append({nome: " ".join(frasi).strip().rstrip(" ,;")
                    for nome, frasi in acc.items()})
    return out


def estrai_note(pg):
    """Note di sostituzione bus. Stanno in box laterali che extract_text()
    non restituisce: le ricostruisco dai char grezzi in ordine documento.
    I box sono concatenati senza separatore ('...LauretoIl 22 e 23...'), quindi
    individuo gli inizi e ritaglio ciascuna nota fino all'inizio successivo."""
    grezzo = re.sub(r"\s+", " ", "".join(c["text"] for c in pg.chars))
    inizi = [m.start() for m in re.finditer(
        r"(?:Il|Dal)\s+\d{1,2}\s+[\w\s]{0,30}?la\s+tratta\s", grezzo)]
    out = []
    for i, st in enumerate(inizi):
        fine = inizi[i + 1] if i + 1 < len(inizi) else len(grezzo)
        nota = grezzo[st:fine]
        if "sostitutiv" not in nota:
            continue
        # la nota finisce dove una minuscola e' seguita da una maiuscola
        # (attacco del box successivo), cercando dopo 'sostitutivi'
        m = re.search(r"sostitutiv[io]", nota)
        coda = nota[m.end():]
        # la coda vale solo se e' la clausola sulla fermata soppressa
        mc = re.match(r"\.\s*Non\s+verr[àa]\s+effettuata\s+la\s+fermata\s+a\s+.{0,50}",
                      coda)
        if mc:
            testo_coda = mc.group(0)
            t = re.search(r"[a-zà-ù](?=[A-Z])", testo_coda)
            nota = nota[:m.end()] + (testo_coda[:t.end()] if t else testo_coda)
        else:
            nota = nota[:m.end()]
        nota = re.sub(r"\s+", " ", nota).strip()
        if nota and nota not in out:
            out.append(nota)
    return out


def parse_tabella(pg, tab, ctx):
    """Una regione-tabella puo' contenere PIU' blocchi (direzioni): ognuno
    inizia con una banda-codici seguita da FERIALE/FESTIVO. Le colonne vengono
    dalle CELLE della griglia (i codici di testata di alcune linee sono in un
    font che pdfplumber frammenta in caratteri: le parole non bastano)."""
    parole = pg.crop(tab.bbox).extract_words(keep_blank_chars=False)
    if not parole:
        return []
    chars_pag = pg.chars      # coordinate NON clippate: vedi orari_da_char

    # 1) bande FERIALE/FESTIVO: individuano l'inizio di ogni blocco
    tipo_w = [w for w in parole if w["text"].upper().startswith(("FERIAL", "FESTIV"))
              and w["x1"] - w["x0"] > w["bottom"] - w["top"]]   # esclude i verticali
    y_tipo = sorted({round(w["top"]) for w in tipo_w})
    inizi = []
    for y in y_tipo:
        if not inizi or y - inizi[-1] > 6:
            inizi.append(y)
    if not inizi:
        return []

    blocchi = []
    for bi, y0 in enumerate(inizi):
        y1 = inizi[bi + 1] - 16 if bi + 1 < len(inizi) else tab.bbox[3]

        # 2) colonne dalle celle della griglia che intersecano la banda codici.
        #    Nella stessa colonna ci sono piu' celle impilate (icona, codice,
        #    tipo giorno): estraggo da tutte e unisco i codici per fascia x.
        per_x = {}
        fasce = {}          # tutte le fasce x candidate, anche senza codice
        banda_y = []        # y in cui i codici sono stati effettivamente trovati
        for (cx0, ctop, cx1, cbot) in tab.cells:
            if ctop < y0 - 2 and cbot > y0 - 34 and 12 < (cx1 - cx0) < 95:
                k = (round(cx0), round(cx1))
                fasce[k] = (cx0, cx1)
                codici = codici_da_testo(
                    testo_box(pg, (cx0, ctop, cx1, min(cbot, y0 - 1))))
                if codici:
                    per_x.setdefault(k, (cx0, cx1, []))[2].extend(codici)
                    banda_y.append((ctop, min(cbot, y0 - 1)))
        # alcune griglie hanno un bordo mancante e la cella-codice della prima
        # colonna non esiste: recupero per fascia x sulla banda y dei codici.
        # Vincoli: larghezza compatibile con le colonne gia' trovate e crop
        # ristretto, altrimenti la fascia stretta delle etichette 'a./p.'
        # cattura per intersezione di bordo il codice della colonna vicina.
        if per_x and banda_y:
            ytop = min(b[0] for b in banda_y)
            ybot = max(b[1] for b in banda_y)
            larg = sorted(cx1 - cx0 for (cx0, cx1, _) in per_x.values())
            lmed = larg[len(larg) // 2]
            noti = {c for v in per_x.values() for c in v[2]}
            for k, (cx0, cx1) in sorted(fasce.items()):
                if k in per_x or abs((cx1 - cx0) - lmed) > 0.35 * lmed:
                    continue
                codici = codici_da_testo(
                    testo_box(pg, (cx0 + 1.5, ytop, cx1 - 1.5, ybot)))
                codici = [c for c in codici if c not in noti]
                if codici:
                    per_x[k] = (cx0, cx1, codici)
                    noti.update(codici)
        colonne = [list(v) for v in sorted(per_x.values())]
        if not colonne:
            continue
        colonne.sort(key=lambda c: c[0])
        x_prima = colonne[0][0]

        # 3) bande stazione nel blocco
        in_blocco = [w for w in parole if y0 < w["top"] < y1]
        bande = bande_ap(in_blocco, x_prima)

        # 4) 'Provenienza': riga sopra le stazioni con nomi in colonna
        prove = {}
        pw = [w for w in in_blocco if w["text"].startswith("Provenienz")]
        if pw:
            yp = pw[0]["top"]
            for w in in_blocco:
                if abs(w["top"] - yp) < 4 and w["x0"] >= x_prima - 6 \
                        and w["text"].isupper() and len(w["text"]) > 2:
                    for ci, c in enumerate(colonne):
                        if c[0] - 8 <= (w["x0"]+w["x1"])/2 <= c[1] + 8:
                            prove[ci] = w["text"]

        # 5) nomi stazione: ogni parola-nome va alla banda col CENTRO piu'
        #    vicino (gli intervalli si sovrapporrebbero tra stazioni adiacenti)
        if not bande:
            continue
        centri = []
        for (wa, wp) in bande:
            ys = [w["top"] for w in (wa, wp) if w]
            centri.append(sum(ys) / len(ys))
        nomi_banda = defaultdict(list)
        SCARTA = ("STAZIONE", "Provenienza", "Punti")
        for w in in_blocco:
            if w["x1"] > x_prima - 12:      # nome: ben a sinistra delle colonne
                continue
            if not re.match(r"^[A-ZÀ-Ù(]", w["text"]) or w["text"] in SCARTA:
                continue
            bi2 = min(range(len(centri)), key=lambda i: abs(w["top"] - centri[i]))
            if abs(w["top"] - centri[bi2]) < 16:
                nomi_banda[bi2].append(w)

        # Le bande partizionano lo spazio verticale: ogni riga di orari cade in
        # una sola stazione. Serve perche' in alcuni blocchi l'etichetta 'p.'
        # non c'e' (font frammentato) e la riga di partenza, cercata "un po'
        # sotto la a.", finirebbe contata anche nella stazione successiva.
        inizi_b = [(wa["top"] if wa else wp["top"]) for (wa, wp) in bande]
        salti = sorted(b - a for a, b in zip(inizi_b, inizi_b[1:]) if b > a)
        passo = salti[len(salti) // 2] if salti else 12.0

        # per ogni banda: nome + orari per colonna + cambi segmento
        staz_rows = []
        for bidx, (wa, wp) in enumerate(bande):
            ya = wa["top"] if wa else None
            yp2 = wp["top"] if wp else None
            # Con entrambe le etichette la banda e' ancorata a loro (stretta).
            # Quando la 'p.' manca (font frammentato) scendo di 0.8*passo: e'
            # quanto basta per la riga di partenza senza toccare la stazione
            # dopo, e comunque non oltre l'inizio della banda successiva.
            if yp2 is not None:
                lo, hi = (ya if ya is not None else yp2) - 4, yp2 + 4
            else:
                # Blocchi senza etichetta 'p.' (font frammentato): l'unica
                # etichetta ('a.') e' centrata nella cella e le due righe di
                # orari le stanno una sopra e una sotto. Delimito col punto
                # medio fra etichette consecutive: cosi' le bande partizionano
                # lo spazio e nessun orario finisce in due stazioni.
                lo = ((inizi_b[bidx - 1] + ya) / 2 if bidx > 0
                      else ya - passo / 2)
                hi = ((ya + inizi_b[bidx + 1]) / 2 if bidx + 1 < len(inizi_b)
                      else ya + passo / 2)
            nome = " ".join(w["text"] for w in
                            sorted(nomi_banda.get(bidx, []),
                                   key=lambda w: (round(w["top"]), w["x0"])))
            nome = re.sub(r"\s+", " ", nome).strip()
            nome = re.sub(r"-\s+", "-", nome)
            if not nome:
                continue
            # font frammentato ('T A R A N T O', 'A LB E ROBELLO'): se il nome
            # compattato coincide con un nome gia' visto compattato, riusa
            # quello; se contiene frammenti di 1-2 lettere, memorizza compatto
            chiave = nome.replace(" ", "")
            if ctx is not None:
                canone = ctx.setdefault("nomi_canone", {})
                if chiave in canone:
                    nome = canone[chiave]
                else:
                    if re.search(r"\b\w{1,2}\b \b", nome + " ") and "." not in nome:
                        nome = chiave  # niente canone noto: compatta
                    canone[chiave] = nome
            righe_col = {}
            cambi = {}
            for ci, c in enumerate(colonne):
                ws = [w for w in in_blocco
                      if lo <= w["top"] <= hi
                      and c[0] <= (w["x0"] + w["x1"]) / 2 <= c[1]]
                # Orari della colonna in questa banda. Li leggo SEMPRE a livello
                # di carattere: in alcune tabelle (ramo Taranto, righe di
                # partenza della linea 1) il font e' frammentato e le parole non
                # si formano affatto, quindi extract_words perderebbe righe
                # intere. Le parole restano come integrazione.
                trovati = orari_da_char(chars_pag, (c[0], lo, c[1], hi))
                visti = {round(y) for (y, _) in trovati}
                for w in ws:
                    m = norm_ora(w["text"])
                    if m is not None and not any(abs(w["top"] - y) <= 2
                                                 for y in visti):
                        trovati.append((w["top"], m))
                righe_y = []
                for (y, m) in sorted(trovati):
                    if righe_y and abs(y - righe_y[-1][0]) <= 3:
                        continue          # stessa riga, gia' presa
                    righe_y.append((y, m))
                if righe_y:
                    if ya is not None and yp2 is not None:
                        # entrambe le etichette: ogni orario va sulla riga della
                        # sua etichetta, senza sconfinare
                        for (y, m) in righe_y:
                            chi = None
                            if abs(y - ya) <= 3:
                                chi = "a"
                            elif abs(y - yp2) <= 3:
                                chi = "p"
                            if chi:
                                righe_col.setdefault(ci, {})[chi] = m
                    elif ya is not None:
                        # etichetta unica centrata: sopra l'arrivo, sotto la
                        # partenza (la prima stazione ha solo la partenza)
                        for (y, m) in righe_y:
                            chi = "a" if y < ya else "p"
                            righe_col.setdefault(ci, {})[chi] = m
                    else:
                        righe_col.setdefault(ci, {})["p"] = righe_y[0][1]
                else:
                    # nessun orario: puo' essere un cambio codice a meta' corsa
                    cod = codici_da_testo(testo_box(pg, (c[0], lo, c[1], hi)))
                    if cod:
                        cambi[ci] = cod
                # cambio codice incastonato TRA la riga a. e la riga p.
                if righe_y and len(righe_y) >= 2 and righe_y[1][0] - righe_y[0][0] > 14:
                    cod = codici_da_testo(testo_box(
                        pg, (c[0], righe_y[0][0] + 6, c[1], righe_y[1][0] - 1)))
                    if cod:
                        cambi[ci] = cod
            staz_rows.append((nome, righe_col, cambi, (lo, hi)))

        blocchi.append({"colonne": colonne, "prove": prove,
                        "stazioni": staz_rows, "y0": y0})
    return blocchi


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    db_path, pdf_paths = sys.argv[1], sys.argv[2:]
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript(SCHEMA)

    staz_id = {}
    def sid(nome):
        if nome not in staz_id:
            cur.execute("INSERT INTO ferro_stazioni (nome) VALUES (?)", (nome,))
            staz_id[nome] = cur.lastrowid
        return staz_id[nome]

    n_tab = n_corse = n_trans = n_note = 0
    errori = []
    ctx_nomi = {}
    ctx_nomi = {}
    for path in pdf_paths:
        base = os.path.basename(path)
        with pdfplumber.open(path) as pdf:
            for pi, pg in enumerate(pdf.pages, 1):
                testo = pg.extract_text() or ""
                titoli = parse_titoli(testo)
                if not titoli:
                    continue
                # note bus sostitutivi (box laterali, dai char grezzi)
                note_pag = estrai_note(pg)

                tabs = pg.find_tables(table_settings=TAB)
                blocchi = []
                for t in tabs:
                    blocchi.extend(parse_tabella(pg, t, ctx_nomi))
                if not blocchi:
                    continue
                # punti di fermata stradali dei bus (colonna a destra),
                # per blocco: l'elenco vale per la direzione della tabella
                fb_blocchi = estrai_fermate_bus(pg, blocchi)
                # titolo: uno per pagina; se piu' titoli, li assegno in ordine
                # ai blocchi (meta' blocchi per titolo). L'assegnazione per
                # posizione ha senso solo se i blocchi si ripartiscono in modo
                # esatto: altrimenti e' un'ambiguita' e deve fermare la build,
                # non produrre tabelle con periodo o tipo giorno sbagliati.
                if len(blocchi) % len(titoli) != 0:
                    errori.append("%s p%d: %d blocchi per %d titoli:"
                                  " assegnazione titolo->blocco ambigua"
                                  % (base, pi, len(blocchi), len(titoli)))
                    continue
                per_tit = max(1, len(blocchi) // len(titoli))
                for bi, bl in enumerate(blocchi):
                    tit = titoli[min(bi // per_tit, len(titoli) - 1)]
                    linea, tipo, dal, al = tit
                    cur.execute(
                        "INSERT INTO ferro_tabelle (linea, tipo_giorno, dal, al,"
                        " file, pagina, ordine, y_testata)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (linea, tipo, dal, al, base, pi, bi, bl["y0"]))
                    tid = cur.lastrowid
                    for nome_st, testo_fb in fb_blocchi[bi].items():
                        cur.execute(
                            "INSERT OR REPLACE INTO ferro_fermate_bus"
                            " VALUES (?,?,?)",
                            (tid, sid(nome_st), testo_fb))
                    n_tab += 1
                    for testo_nota in note_pag:
                        cur.execute("INSERT INTO ferro_note VALUES (?,?)",
                                    (tid, testo_nota))
                        n_note += 1

                    for ci, col in enumerate(bl["colonne"]):
                        codici = col[2]
                        cur.execute(
                            "INSERT INTO ferro_corse (tabella_id, colonna,"
                            " provenienza, x_centro) VALUES (?,?,?,?)",
                            (tid, ci, bl["prove"].get(ci),
                             (col[0] + col[1]) / 2))
                        cid = cur.lastrowid
                        n_corse += 1
                        # segmenti: codici intestazione + cambi a meta' tabella
                        segs = [(c, mezzo_da_codice(c)) for c in codici]
                        seq_t = 0
                        seg_corrente = 0
                        boundary_prev = None
                        for (nome, righe, cambi, _banda) in bl["stazioni"]:
                            r = righe.get(ci)
                            for cambio in cambi.get(ci, []):
                                segs.append((cambio, mezzo_da_codice(cambio)))
                                boundary_prev = nome    # confine: questa stazione
                            if not r:
                                continue
                            seq_t += 1
                            cur.execute(
                                "INSERT INTO ferro_transiti (corsa_id,"
                                " stazione_id, sequenza, arrivo_min,"
                                " partenza_min) VALUES (?,?,?,?,?)",
                                (cid, sid(nome), seq_t,
                                 r.get("a"), r.get("p")))
                            n_trans += 1
                        for si, (cod, mez) in enumerate(segs, 1):
                            cur.execute(
                                "INSERT INTO ferro_segmenti (corsa_id, sequenza,"
                                " codice, mezzo, da_stazione_id, a_stazione_id)"
                                " VALUES (?,?,?,?,?,?)",
                                (cid, si, cod, mez,
                                 sid(boundary_prev) if (si == len(segs) and
                                     boundary_prev and si > 1) else None, None))

    cur.execute("INSERT INTO ferro_meta VALUES ('ferro_pdf', ?)",
                (", ".join(os.path.basename(p) for p in pdf_paths),))

    # --- ordine dei segmenti e confini di mezzo ---------------------------
    # I codici di TESTATA (109B/92151) non dicono dove un segmento subentra
    # all'altro, e non dicono nemmeno la loro posizione rispetto ai codici
    # incastonati a meta' colonna: nella corsa 760B/92360 con 92355 incastonato
    # a Nardo', l'ordine di percorso e' 760B -> 92355 -> 92360. Lo sa chi
    # conosce la linea: e' spezzata in tratte che si toccano negli snodi
    # (Putignano, Martina Franca, Francavilla Fontana, Nardo', Novoli) e li'
    # il veicolo sosta per il cambio: la sosta lunga E' lo snodo.
    #
    # Quindi: enumero gli ordini possibili (testata in ordine relativo,
    # incastonati ancorati al proprio confine) e tengo quello in cui ogni
    # cambio di MEZZO trova una sosta-snodo univoca (>= 4 min, massimo stretto
    # del suo intervallo). I cambi treno->treno sono amministrativi: non
    # chiedono nulla al viaggiatore e non richiedono sosta; il loro punto
    # viene fissato solo se una sosta univoca lo indica (serve alla resa dei
    # codici), altrimenti resta ignoto.
    def _interleave(u, d):
        if not u:
            yield list(d); return
        if not d:
            yield list(u); return
        for resto in _interleave(u[1:], d):
            yield [u[0]] + resto
        for resto in _interleave(u, d[1:]):
            yield [d[0]] + resto

    # Un confine DEDOTTO puo' cadere solo su un hub di SNODI_AMMESSI (vedi la
    # definizione a livello di modulo): se la sosta massima indicasse un'altra
    # stazione (refuso, fascicolo futuro con struttura diversa) la corsa va in
    # errore, non si inventa un trasbordo in una stazione qualunque.
    id_nome = {v2: k for k, v2 in staz_id.items()}

    def _sosta_unica(tr, lo, hi):
        """(idx, sosta) della fermata con sosta massima in (lo, hi), se e' un
        massimo stretto >= 4 min; altrimenti None."""
        cand = [(tr[i][2] - tr[i][1], i) for i in range(lo + 1, hi)
                if tr[i][1] is not None and tr[i][2] is not None]
        if not cand:
            return None
        cand.sort(reverse=True)
        if cand[0][0] < 4 or (len(cand) > 1 and cand[1][0] >= cand[0][0]):
            return None
        return (cand[0][1], cand[0][0])

    n_dedotti = 0
    for (cid,) in cur.execute("SELECT id FROM ferro_corse").fetchall():
        segs = cur.execute(
            "SELECT id, codice, mezzo, da_stazione_id FROM ferro_segmenti"
            " WHERE corsa_id=? ORDER BY sequenza", (cid,)).fetchall()
        if len(segs) < 2:
            continue
        tr = cur.execute(
            "SELECT stazione_id, arrivo_min, partenza_min FROM ferro_transiti"
            " WHERE corsa_id=? ORDER BY sequenza", (cid,)).fetchall()
        pos_staz = {}
        for i, t in enumerate(tr):
            pos_staz.setdefault(t[0], i)      # prima occorrenza
        U = [s for s in segs if s[3] is None]
        Dl = sorted((s for s in segs if s[3] is not None),
                    key=lambda s: pos_staz.get(s[3], 10 ** 9))
        if all(s[2] == "treno" for s in segs) and \
           not any(s[3] is not None for s in segs):
            continue    # solo treni: cambio numero amministrativo, il
                        # viaggiatore non fa nulla (92332/92361)
        validi = []
        for ordine in _interleave(U, Dl):
            if ordine[0][3] is not None:
                continue                      # si parte con un codice di testata
            pos = [0]
            fisso = 0                         # ultima posizione certa
            confini = {}                      # seg_id -> idx transito dedotto
            ok = True
            for k in range(1, len(ordine)):
                s = ordine[k]
                nxt = next((pos_staz[x[3]] for x in ordine[k:]
                            if x[3] is not None), len(tr) - 1)
                if s[3] is not None:
                    p = pos_staz.get(s[3])
                    if p is None or p <= fisso:
                        ok = False; break
                    pos.append(p); fisso = p
                elif s[2] != ordine[k - 1][2] or s[2] == "bus":
                    # cambio di mezzo, oppure cambio di autobus (115B->383B):
                    # negli hub cambia sempre il numero o il mezzo, e per il
                    # bus cambia anche il veicolo. Serve il confine.
                    trovato = _sosta_unica(tr, fisso, nxt)
                    if trovato is None or \
                       id_nome.get(tr[trovato[0]][0]) not in SNODI_AMMESSI:
                        ok = False; break
                    confini[s[0]] = trovato[0]
                    pos.append(trovato[0]); fisso = trovato[0]
                else:
                    pos.append(None)          # amministrativo: punto ignoto
            if ok:
                validi.append((ordine, confini))
        if not validi:
            errori.append("corsa %d: nessun ordine dei segmenti compatibile "
                          "con le soste (codici %s)"
                          % (cid, [s[1] for s in segs]))
            continue
        # osservabile: dove cambia il mezzo. Se tutti gli ordini validi
        # concordano su questo, l'ambiguita' residua e' solo amministrativa.
        chiavi = {tuple(sorted(c.values())) for (_, c) in validi}
        if len(chiavi) > 1:
            errori.append("corsa %d: ordine dei segmenti ambiguo (%d soluzioni)"
                          % (cid, len(validi)))
            continue
        ordine, confini = validi[0]
        # cambi amministrativi: fisso il punto solo se una sosta univoca lo
        # indica (serve alla resa del codice giusto sul tratto giusto)
        for k in range(1, len(ordine)):
            s = ordine[k]
            if s[3] is not None or s[0] in confini:
                continue
            prev = 0
            for j in range(k - 1, -1, -1):
                sj = ordine[j]
                if sj[3] is not None:
                    prev = pos_staz[sj[3]]; break
                if sj[0] in confini:
                    prev = confini[sj[0]]; break
            nxt = next((pos_staz[x[3]] if x[3] is not None else confini[x[0]]
                        for x in ordine[k + 1:]
                        if x[3] is not None or x[0] in confini), len(tr) - 1)
            trovato = _sosta_unica(tr, prev, nxt)
            if trovato is not None and \
               id_nome.get(tr[trovato[0]][0]) in SNODI_AMMESSI:
                confini[s[0]] = trovato[0]
        # scrivo: ordine di percorso + confini dedotti
        for seq, s in enumerate(ordine, 1):
            dz = s[3]
            if dz is None and s[0] in confini:
                dz = tr[confini[s[0]]][0]
                n_dedotti += 1
            cur.execute("UPDATE ferro_segmenti SET sequenza=?, da_stazione_id=?"
                        " WHERE id=?", (seq, dz, s[0]))
    cur.execute("INSERT INTO ferro_meta VALUES ('confini_dedotti', ?)",
                (str(n_dedotti),))
    d = cur.execute("SELECT MIN(dal), MAX(al) FROM ferro_tabelle").fetchone()
    cur.execute("INSERT INTO ferro_meta VALUES ('ferro_dal', ?)", (d[0],))
    cur.execute("INSERT INTO ferro_meta VALUES ('ferro_al', ?)", (d[1],))
    con.commit()

    print("FERRO COMPLETATO -> %s" % db_path)
    print("  tabelle-periodo : %d" % n_tab)
    print("  corse (colonne) : %d" % n_corse)
    print("  transiti        : %d" % n_trans)
    print("  stazioni        : %d" % len(staz_id))
    print("  note            : %d" % n_note)
    print("  ERRORI          : %d" % len(errori))
    for e in errori[:10]:
        print("   -", e)
    sys.exit(1 if errori else 0)


if __name__ == "__main__":
    main()
