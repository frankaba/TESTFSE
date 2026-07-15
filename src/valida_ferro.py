#!/usr/bin/env python3
"""
valida_ferro.py - controlli di integrita' del DB ferroviario contro i PDF.

Uso:
    python3 valida_ferro.py ferro.db pdf/ferroviario/*.pdf [--aggiorna-attese]

Filosofia (la stessa di valida.py / valida_turni.py):
- I controlli F1-F5 sono INDIPENDENTI dal parser: ricontano dal PDF con una
  logica diversa da build_ferro.py e confrontano col DB. Se il parser sbaglia,
  qui deve emergere.
- Le anomalie che sono REFUSI DEL SORGENTE (orari non monotoni, ecc.) vengono
  CONTATE e riportate, mai riparate in silenzio. Il conteggio e' confrontato
  con una baseline (attese_ferro.json): se cambia, e' FAIL. Cosi' una
  regressione del parser non puo' nascondersi dietro a un "e' un refuso".

F1  colonne nei PDF == corse nel DB (per file/pagina)
F2  ogni corsa ha >= 2 transiti con almeno un orario
F3  arrivo <= partenza nella stessa stazione (D-misura: violazioni)
F4  mezzo dedotto dal codice == mezzo dedotto dall'icona immagine
F5  ogni tabella ha >= 1 corsa e periodo dal <= al
F6  monotonia degli orari lungo la corsa (D-misura: violazioni; include i
    refusi noti del sorgente)
F7  copertura periodi: per ogni (linea, tipo_giorno) i periodi non si
    sovrappongono e coprono con continuita' l'intervallo dichiarato
F8  ogni codice segmento e' coerente col mezzo (lettera => bus, cifre => treno)
"""

import glob
import json
import os
import re
import sqlite3
import sys

import pdfplumber

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_ferro import (TAB, codici_da_testo, testo_box, mezzo_da_codice,
                         norm_ora, SNODI_AMMESSI)

ATTESE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "attese_ferro.json")

HASH_BUS = "0415c7a1"
HASH_TRENO = "b6fc9cd8"


def hm(m):
    return "--:--" if m is None else "%02d:%02d" % (m // 60, m % 60)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    aggiorna = "--aggiorna-attese" in sys.argv
    if len(args) < 2:
        print(__doc__)
        sys.exit(2)
    db_path, pdf_paths = args[0], args[1:]
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    esiti = []      # (nome, ok, dettaglio)
    misure = {}     # D-misure confrontate con la baseline

    # ---------- F1: conteggio orari PDF == orari DB ----------
    # Canale INDIPENDENTE dal parser: ogni orario contiene esattamente un ':',
    # quindi conto i caratteri ':' nel PDF. Non passa dalle celle, dai codici
    # ne' dalle parole: se il parser perde una colonna o una riga (e' successo:
    # una cella-codice mancante nella griglia faceva sparire una colonna
    # intera), il conteggio non torna.
    # Unica eccezione nota: i refusi del sorgente in cui l'orario e' scritto col
    # punto ('15.46'). Il parser li normalizza, il PDF non ha quel ':'. Sono
    # CONTATI qui sotto, non riparati.
    orari_punto = 0
    per_pag_pdf = {}
    val_pdf = {}        # (file, pagina) -> multiset dei VALORI orario nel PDF
    for path in pdf_paths:
        base = os.path.basename(path)
        with pdfplumber.open(path) as pdf:
            for pi, pg in enumerate(pdf.pages, 1):
                if "Linea" not in (pg.extract_text() or ""):
                    continue
                grezzo = "".join(c["text"] for c in pg.chars)
                n = sum(1 for c in pg.chars if c["text"] == ":")
                p = len(re.findall(r"\d{1,2}\.\d{2}", grezzo))
                orari_punto += p
                per_pag_pdf[(base, pi)] = n + p
                ms = {}
                for m in re.finditer(r"\d{1,2}[:.]\d{2}", grezzo):
                    v = norm_ora(m.group(0))
                    if v is not None:
                        ms[v] = ms.get(v, 0) + 1
                val_pdf[(base, pi)] = ms
    per_pag_db = {}
    for r in con.execute("""SELECT t.file f, t.pagina p,
            SUM((tr.arrivo_min IS NOT NULL) + (tr.partenza_min IS NOT NULL)) n
            FROM ferro_transiti tr JOIN ferro_corse c ON c.id=tr.corsa_id
            JOIN ferro_tabelle t ON t.id=c.tabella_id
            GROUP BY t.file, t.pagina"""):
        per_pag_db[(r["f"], r["p"])] = r["n"]
    prob = []
    for k in sorted(set(per_pag_pdf) | set(per_pag_db)):
        a, b = per_pag_pdf.get(k, 0), per_pag_db.get(k, 0)
        if a != b:
            prob.append("%s p%d: PDF=%d DB=%d (%+d)" % (k[0][:22], k[1], a, b, b - a))
    esiti.append(("F1 orari PDF == orari DB", not prob,
                  "; ".join(prob) if prob
                  else "%d orari (%d col punto)" % (sum(per_pag_db.values()),
                                                    orari_punto)))
    misure["orari_totali"] = sum(per_pag_db.values())
    misure["orari_scritti_col_punto"] = orari_punto

    # F1 non confronta solo i CONTEGGI ma anche i VALORI: multiset degli orari
    # per pagina, PDF vs DB. Un parser che leggesse '15:45' al posto di '15:46'
    # lascerebbe i conteggi identici: qui verrebbe fuori. I refusi del sorgente
    # che il PDF scrive in una forma non riconoscibile come orario ('22-:21')
    # sono l'unico scarto legittimo: CONTATI in baseline, non riparati.
    val_db = {}
    for r in con.execute("""SELECT t.file f, t.pagina p, tr.arrivo_min a,
            tr.partenza_min pa FROM ferro_transiti tr
            JOIN ferro_corse c ON c.id=tr.corsa_id
            JOIN ferro_tabelle t ON t.id=c.tabella_id"""):
        ms = val_db.setdefault((r["f"], r["p"]), {})
        for v in (r["a"], r["pa"]):
            if v is not None:
                ms[v] = ms.get(v, 0) + 1
    divergenti = 0
    esempi_div = []
    for k in sorted(set(val_pdf) | set(val_db)):
        a, b = val_pdf.get(k, {}), val_db.get(k, {})
        if a == b:
            continue
        for v in set(a) | set(b):
            d = abs(a.get(v, 0) - b.get(v, 0))
            if d:
                divergenti += d
                if len(esempi_div) < 6:
                    esempi_div.append("%s p%d: %s PDF=%d DB=%d"
                                      % (k[0][:22], k[1], hm(v),
                                         a.get(v, 0), b.get(v, 0)))
    misure["orari_valore_divergente"] = divergenti
    esiti.append(("F1b valori orario PDF == DB (D-misura)", True,
                  "divergenti: %d%s" % (divergenti,
                      " (%s)" % "; ".join(esempi_div) if esempi_div else "")))
    misure["corse_totali"] = con.execute(
        "SELECT COUNT(*) FROM ferro_corse").fetchone()[0]

    # ---------- F2: corse con almeno 2 transiti ----------
    magre = con.execute("""
        SELECT c.id, COUNT(t.id) n FROM ferro_corse c
        LEFT JOIN ferro_transiti t ON t.corsa_id=c.id
        GROUP BY c.id HAVING n < 2""").fetchall()
    esiti.append(("F2 ogni corsa ha >=2 transiti", not magre,
                  "corse magre: %d" % len(magre)))
    misure["corse_magre"] = len(magre)

    # ---------- F3: arrivo <= partenza ----------
    inv = con.execute("""
        SELECT COUNT(*) FROM ferro_transiti
        WHERE arrivo_min IS NOT NULL AND partenza_min IS NOT NULL
          AND arrivo_min > partenza_min""").fetchone()[0]
    misure["arrivo_dopo_partenza"] = inv
    esiti.append(("F3 arrivo <= partenza (D-misura)", True, "violazioni: %d" % inv))

    # ---------- F4/F8: mezzo ----------
    # F8: coerenza interna codice -> mezzo
    disc = 0
    for r in con.execute("SELECT codice, mezzo FROM ferro_segmenti"):
        if mezzo_da_codice(r["codice"]) != r["mezzo"]:
            disc += 1
    esiti.append(("F8 mezzo coerente col codice", disc == 0,
                  "incoerenze: %d" % disc))

    # F4 (icone) NON implementato: le iconcine bus/treno sarebbero un canale
    # indipendente, ma non sono utilizzabili come prova. Gli stream immagine
    # sono ri-compressi (19 hash distinti per 2 sole icone) e fra le immagini
    # piccole della pagina ce ne sono che non appartengono a nessuna colonna:
    # l'appaiamento icona->colonna risulta rumoroso e produrrebbe un test che
    # "passa" solo dopo averlo tarato sui dati, cioe' nessun test.
    # Il mezzo resta verificato da F8 (regola codice->mezzo, 100% coerente) e
    # dalle misure di ripartizione qui sotto, congelate nella baseline: se il
    # parser cambiasse attribuzione, lo scostamento emergerebbe.
    misure["segmenti_bus"] = con.execute(
        "SELECT COUNT(*) FROM ferro_segmenti WHERE mezzo='bus'").fetchone()[0]
    misure["segmenti_treno"] = con.execute(
        "SELECT COUNT(*) FROM ferro_segmenti WHERE mezzo='treno'").fetchone()[0]
    misure["corse_con_trasbordo"] = con.execute("""
        SELECT COUNT(*) FROM (SELECT corsa_id FROM ferro_segmenti
        GROUP BY corsa_id HAVING COUNT(DISTINCT mezzo) > 1)""").fetchone()[0]

    # ---------- F5: tabelle sane ----------
    vuote = con.execute("""
        SELECT t.id, t.linea, t.tipo_giorno FROM ferro_tabelle t
        LEFT JOIN ferro_corse c ON c.tabella_id=t.id
        GROUP BY t.id HAVING COUNT(c.id)=0""").fetchall()
    rovesce = con.execute(
        "SELECT COUNT(*) FROM ferro_tabelle WHERE dal > al").fetchone()[0]
    esiti.append(("F5 tabelle con corse e periodo valido",
                  not vuote and rovesce == 0,
                  "vuote=%d periodi rovesci=%d" % (len(vuote), rovesce)))
    misure["tabelle"] = con.execute(
        "SELECT COUNT(*) FROM ferro_tabelle").fetchone()[0]

    # ---------- F6: monotonia orari lungo la corsa ----------
    nonmono = []
    for c in con.execute("SELECT id FROM ferro_corse"):
        prec = None
        for t in con.execute("""SELECT s.nome, tr.arrivo_min a, tr.partenza_min p
                FROM ferro_transiti tr JOIN ferro_stazioni s
                ON s.id=tr.stazione_id WHERE tr.corsa_id=? ORDER BY tr.sequenza""",
                             (c["id"],)):
            for v in (t["a"], t["p"]):
                if v is None:
                    continue
                if prec is not None and v < prec:
                    nonmono.append((c["id"], t["nome"], hm(prec), hm(v)))
                prec = v
    misure["orari_non_monotoni"] = len(nonmono)
    esiti.append(("F6 monotonia orari (D-misura)", True,
                  "violazioni: %d" % len(nonmono)))

    # ---------- F9: transiti consecutivi ripetuti (D-misura) ----------
    # Alcune tabelle ripetono la riga della stazione di diramazione (Nardo'
    # Centrale sulla linea 3): il PDF la stampa due volte, il DB la conserva
    # per restare fedele alla fonte (F1 conta anche quegli orari). La ricerca
    # dovra' collassarli. Qui li conto: se il numero cambia, e' un segnale.
    dupli = 0
    for c in con.execute("SELECT id FROM ferro_corse"):
        prec = None
        for t in con.execute("""SELECT stazione_id s, arrivo_min a, partenza_min p
                FROM ferro_transiti WHERE corsa_id=? ORDER BY sequenza""",
                             (c["id"],)):
            cur3 = (t["s"], t["a"], t["p"])
            if prec == cur3:
                dupli += 1
            prec = cur3
    misure["transiti_ripetuti"] = dupli
    esiti.append(("F9 transiti ripetuti (D-misura)", True, "ripetuti: %d" % dupli))

    # ---------- F10: ogni transizione di mezzo ha il suo confine ----------
    # Il fascicolo dichiara il confine solo quando il codice e' incastonato a
    # meta' colonna (761B a Nardo'); quando i codici stanno in testata
    # (109B/92151) il confine viene DEDOTTO dallo snodo (la fermata intermedia
    # con la sosta massima, richiesta univoca). Qui pretendo che non resti
    # nessuna transizione treno<->bus senza confine, e conto i dedotti in
    # baseline: se il numero cambia, il PDF e' cambiato o il criterio non
    # regge piu'.
    orfane = 0
    for c in con.execute("SELECT id FROM ferro_corse"):
        sgs = con.execute("""SELECT mezzo, da_stazione_id d FROM ferro_segmenti
                             WHERE corsa_id=? ORDER BY sequenza""",
                          (c["id"],)).fetchall()
        for i in range(1, len(sgs)):
            reale = (sgs[i]["mezzo"] != sgs[i - 1]["mezzo"]
                     or sgs[i]["mezzo"] == "bus")   # bus->bus = cambio veicolo
            if reale and sgs[i]["d"] is None:
                orfane += 1
    dedotti = int(con.execute("SELECT valore FROM ferro_meta"
                              " WHERE chiave='confini_dedotti'").fetchone()[0])
    # ogni confine deve cadere su un hub della rete: la whitelist e' UNICA e
    # vive in build_ferro.SNODI_AMMESSI (importata qui: se divergessero, il
    # validatore assolverebbe cio' che il builder rifiuta). Se un fascicolo
    # futuro dichiarasse un confine altrove, questo controllo FALLA e la
    # novita' va guardata in faccia, non assorbita in silenzio.
    fuori = [r[0] for r in con.execute(
        """SELECT DISTINCT s.nome FROM ferro_segmenti sg
           JOIN ferro_stazioni s ON s.id=sg.da_stazione_id""")
        if r[0] not in SNODI_AMMESSI]
    esiti.append(("F10 transizioni di mezzo con confine",
                  orfane == 0 and not fuori,
                  "senza confine: %d, dedotti dallo snodo: %d%s"
                  % (orfane, dedotti,
                     ", FUORI WHITELIST: %s" % fuori if fuori else "")))
    misure["confini_dedotti"] = dedotti
    # Regola di dominio: negli hub cambia sempre il numero o il mezzo. Vale
    # per le corse multi-codice (F10 la impone); le corse a codice UNICO che
    # attraversano un hub (bus sostitutivi integrali, treni passanti 6+7)
    # vengono contate qui: se il numero cambia, e' cambiata la fonte o il
    # parser ha perso un codice di testata.
    hub_set = SNODI_AMMESSI
    senza = 0
    for c in con.execute("SELECT id FROM ferro_corse"):
        conf_st = {r[0] for r in con.execute(
            """SELECT s.nome FROM ferro_segmenti sg
               JOIN ferro_stazioni s ON s.id=sg.da_stazione_id
               WHERE sg.corsa_id=?""", (c["id"],))}
        tr = con.execute("""SELECT s.nome FROM ferro_transiti tr
            JOIN ferro_stazioni s ON s.id=tr.stazione_id
            WHERE tr.corsa_id=? ORDER BY tr.sequenza""", (c["id"],)).fetchall()
        for x in tr[1:-1]:
            if x["nome"] in hub_set and x["nome"] not in conf_st:
                senza += 1
    misure["attraversamenti_hub_senza_confine"] = senza
    misure["fermate_bus"] = con.execute(
        "SELECT COUNT(*) FROM ferro_fermate_bus").fetchone()[0]

    # ---------- F7: periodi per linea/tipo_giorno ----------
    buchi = []
    for r in con.execute("""SELECT DISTINCT linea, tipo_giorno
                            FROM ferro_tabelle ORDER BY linea, tipo_giorno"""):
        per = con.execute("""SELECT DISTINCT dal, al FROM ferro_tabelle
            WHERE linea=? AND tipo_giorno=? ORDER BY dal""",
            (r["linea"], r["tipo_giorno"])).fetchall()
        for i in range(len(per) - 1):
            import datetime as dt
            fine = dt.date.fromisoformat(per[i]["al"])
            inizio = dt.date.fromisoformat(per[i + 1]["dal"])
            if (inizio - fine).days != 1:
                buchi.append("linea %s %s: %s..%s -> %s"
                             % (r["linea"], r["tipo_giorno"], per[i]["dal"],
                                per[i]["al"], per[i + 1]["dal"]))
    esiti.append(("F7 periodi contigui per linea/tipo", not buchi,
                  "; ".join(buchi[:4]) if buchi else "ok"))

    misure["stazioni"] = con.execute(
        "SELECT COUNT(*) FROM ferro_stazioni").fetchone()[0]
    misure["transiti"] = con.execute(
        "SELECT COUNT(*) FROM ferro_transiti").fetchone()[0]
    misure["note"] = con.execute(
        "SELECT COUNT(DISTINCT testo) FROM ferro_note").fetchone()[0]

    # ---------- baseline ----------
    if aggiorna:
        with open(ATTESE, "w") as f:
            json.dump(misure, f, indent=2, sort_keys=True)
        print("BASELINE AGGIORNATA -> %s" % ATTESE)

    scostamenti = []
    if os.path.exists(ATTESE):
        with open(ATTESE) as f:
            attese = json.load(f)
        for k, v in sorted(misure.items()):
            if k in attese and attese[k] != v:
                scostamenti.append("%s: atteso %s, trovato %s" % (k, attese[k], v))
            elif k not in attese:
                scostamenti.append("%s: NUOVA misura (%s)" % (k, v))
    else:
        print("[!] nessuna baseline: lancia con --aggiorna-attese")

    # ---------- report ----------
    print("\n=== VALIDAZIONE FERROVIARIO ===")
    for nome, ok, det in esiti:
        print("  [%s] %-38s %s" % ("PASS" if ok else "FAIL", nome, det))
    print("\n  misure:")
    for k, v in sorted(misure.items()):
        print("    %-24s %s" % (k, v))
    if nonmono:
        print("\n  primi orari non monotoni (refusi sorgente attesi):")
        for r in nonmono[:6]:
            print("    corsa %d a %s: %s -> %s" % r)
    if scostamenti:
        print("\n  [SCOSTA] rispetto alla baseline:")
        for s in scostamenti:
            print("    -", s)

    ko = [n for n, ok, _ in esiti if not ok]
    if ko or scostamenti:
        print("\nESITO: FAIL (%s)" % ", ".join(ko + ["baseline"] * bool(scostamenti)))
        sys.exit(1)
    print("\nESITO: PASS")


if __name__ == "__main__":
    main()
