#!/usr/bin/env python3
"""
test_equivalenza_ferro.py - la PWA treni deve dire quello che dice il DB.

Uso: python3 test_equivalenza_ferro.py dist/treni.html dist/ferro.db

gen_treni.py riscrive i dati in una forma compatta (indici al posto degli id,
transiti ripetuti collassati, tipo giorno L/F). Ogni riscrittura e' un'occasione
di sbagliare. Qui i dati vengono riletti DALL'HTML generato e la ricerca viene
rifatta in Python replicando la logica del JS, poi confrontata con una query
indipendente scritta direttamente sul DB.

E1  stazioni, tabelle, corse: stessi conteggi in HTML e DB
E2  transiti: HTML == DB a meno dei ripetuti dichiarati
E3  ricerca A->B: stessi risultati (orari e trasbordi) su un campione ampio
E5  soluzioni con un cambio: ogni gamba proposta esiste davvero nel DB, gli
    orari coincidono e l'attesa alla coincidenza sta nei limiti dichiarati
E4  il tipo giorno e' distinguibile (bug: 'Feriale' e 'Festivo' hanno la stessa
    iniziale, se venissero codificati col primo carattere l'app mescolerebbe i
    due orari)
"""

import datetime as dt
import json
import re
import sqlite3
import sys


def carica_html(path):
    testo = open(path, encoding="utf-8").read()
    m = re.search(r"^const D = (\{.*\});$", testo, re.M)
    if not m:
        raise SystemExit("dati non trovati in %s" % path)
    return json.loads(m.group(1))


def tipo_giorno(iso, festivi):
    d = dt.date.fromisoformat(iso)
    return "F" if (d.weekday() == 6 or iso in festivi) else "L"


def cerca_html(D, i_da, i_a, iso, min_ora):
    """Replica esatta della funzione cerca() del JS."""
    out = []
    tg = tipo_giorno(iso, D["festivi"])
    for c in D["corse"]:
        t = D["tab"][c[0]]
        if iso < t[2] or iso > t[3] or t[1] != tg:
            continue
        tr = c[2]
        s = e = -1
        for i, x in enumerate(tr):
            if s < 0 and x[0] == i_da and x[2] is not None:
                s = i
            elif s >= 0 and x[0] == i_a:
                e = i
                break
        if s < 0 or e < 0:
            continue
        part = tr[s][2]
        arr = tr[e][1] if tr[e][1] is not None else tr[e][2]
        if part is None or arr is None or part < min_ora:
            continue
        perc = {x[0] for x in tr[s:e + 1]}
        cambi = sorted(sg[2] for sg in c[1]
                       if sg[1] == 1 and sg[2] >= 0 and sg[2] in perc)
        out.append((part, arr, tuple(cambi)))
    return sorted(out)


def cerca_db(con, nome_da, nome_a, iso, min_ora, festivi):
    """Query indipendente: parte dal DB, non dalla struttura dell'HTML."""
    tg = "Festivo" if tipo_giorno(iso, festivi) == "F" else "Feriale"
    out = []
    q = """SELECT c.id, t.id tid FROM ferro_corse c
           JOIN ferro_tabelle t ON t.id = c.tabella_id
           WHERE t.tipo_giorno = ? AND ? BETWEEN t.dal AND t.al"""
    for r in con.execute(q, (tg, iso)):
        tr = con.execute("""SELECT s.nome, tr.arrivo_min a, tr.partenza_min p
                            FROM ferro_transiti tr
                            JOIN ferro_stazioni s ON s.id = tr.stazione_id
                            WHERE tr.corsa_id = ? ORDER BY tr.sequenza""",
                         (r["id"],)).fetchall()
        # il DB conserva i transiti ripetuti della stazione di diramazione:
        # qui li collasso, come fa gen_treni.py
        puliti = []
        for x in tr:
            cur = (x["nome"], x["a"], x["p"])
            if puliti and puliti[-1] == cur:
                continue
            puliti.append(cur)
        s = e = -1
        for i, (nome, a, p) in enumerate(puliti):
            if s < 0 and nome == nome_da and p is not None:
                s = i
            elif s >= 0 and nome == nome_a:
                e = i
                break
        if s < 0 or e < 0:
            continue
        part = puliti[s][2]
        arr = puliti[e][1] if puliti[e][1] is not None else puliti[e][2]
        if part is None or arr is None or part < min_ora:
            continue
        perc = {n for (n, _, _) in puliti[s:e + 1]}
        cambi = []
        for sg in con.execute("""SELECT s.mezzo, st.nome b FROM ferro_segmenti s
                                 LEFT JOIN ferro_stazioni st
                                 ON st.id = s.da_stazione_id
                                 WHERE s.corsa_id = ?""", (r["id"],)):
            if sg["mezzo"] == "bus" and sg["b"] and sg["b"] in perc:
                cambi.append(sg["b"])
        out.append((part, arr, tuple(sorted(cambi))))
    return sorted(out)


CAMBIO_MIN, CAMBIO_MAX = 5, 120


def cerca_cambio_html(D, i_da, i_a, iso, min_ora):
    """Replica di cercaCambio() del JS."""
    tg = tipo_giorno(iso, D["festivi"])
    valide = [c for c in D["corse"]
              if D["tab"][c[0]][2] <= iso <= D["tab"][c[0]][3]
              and D["tab"][c[0]][1] == tg]
    g1 = []
    for c in valide:
        tr = c[2]
        s = next((i for i, x in enumerate(tr)
                  if x[0] == i_da and x[2] is not None), -1)
        if s < 0 or tr[s][2] < min_ora:
            continue
        # gia' un diretto: non serve come prima gamba
        if any(x[0] == i_a for i, x in enumerate(tr) if i > s):
            continue
        for i in range(s + 1, len(tr)):
            arr = tr[i][1] if tr[i][1] is not None else tr[i][2]
            if arr is None:
                continue
            g1.append((tr[i][0], tr[s][2], arr, c))
    sol = []
    for (x, part, arr, c) in g1:
        for c2 in valide:
            if c2 is c:
                continue
            tr = c2[2]
            s2 = next((i for i, y in enumerate(tr)
                       if y[0] == x and y[2] is not None), -1)
            if s2 < 0:
                continue
            # la seconda corsa serve anche la partenza: coppia dominata
            if any(y[0] == i_da and y[2] is not None for y in tr[:s2]):
                continue
            att = tr[s2][2] - arr
            if att < CAMBIO_MIN or att > CAMBIO_MAX:
                continue
            e2 = next((i for i in range(s2 + 1, len(tr)) if tr[i][0] == i_a), -1)
            if e2 < 0:
                continue
            a2 = tr[e2][1] if tr[e2][1] is not None else tr[e2][2]
            if a2 is None:
                continue
            sol.append({"part": part, "arr": a2, "x": x, "attesa": att,
                        "arr1": arr, "part2": tr[s2][2]})
    best = {}
    for s in sol:
        if s["part"] not in best or best[s["part"]]["arr"] > s["arr"]:
            best[s["part"]] = s
    return sorted(best.values(), key=lambda s: s["part"])[:6]


def verifica_cambio_su_db(con, D, nome_da, nome_a, iso, sol):
    """Ogni gamba della soluzione deve esistere nel DB con quegli orari."""
    nomi = D["staz"]
    tg = "Festivo" if tipo_giorno(iso, D["festivi"]) == "F" else "Feriale"
    x = nomi[sol["x"]]
    prob = []
    if not (CAMBIO_MIN <= sol["attesa"] <= CAMBIO_MAX):
        prob.append("attesa fuori limiti: %d" % sol["attesa"])
    if sol["part2"] - sol["arr1"] != sol["attesa"]:
        prob.append("attesa incoerente con gli orari")
    for (a, b, part, arr) in ((nome_da, x, sol["part"], sol["arr1"]),
                              (x, nome_a, sol["part2"], sol["arr"])):
        q = """SELECT c.id FROM ferro_corse c
               JOIN ferro_tabelle t ON t.id=c.tabella_id
               WHERE t.tipo_giorno=? AND ? BETWEEN t.dal AND t.al"""
        trovata = False
        for r in con.execute(q, (tg, iso)):
            tr = con.execute("""SELECT s.nome, tr.arrivo_min a, tr.partenza_min p
                                FROM ferro_transiti tr JOIN ferro_stazioni s
                                ON s.id=tr.stazione_id WHERE tr.corsa_id=?
                                ORDER BY tr.sequenza""", (r["id"],)).fetchall()
            i1 = next((i for i, y in enumerate(tr)
                       if y["nome"] == a and y["p"] == part), -1)
            if i1 < 0:
                continue
            i2 = next((i for i in range(i1 + 1, len(tr))
                       if tr[i]["nome"] == b
                       and (tr[i]["a"] if tr[i]["a"] is not None
                            else tr[i]["p"]) == arr), -1)
            if i2 >= 0:
                trovata = True
                break
        if not trovata:
            prob.append("gamba %s->%s %s-%s non trovata nel DB"
                        % (a, b, part, arr))
    return prob


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    html, db = sys.argv[1], sys.argv[2]
    D = carica_html(html)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    esiti = []

    # E1: conteggi
    n_st = con.execute("SELECT COUNT(*) FROM ferro_stazioni").fetchone()[0]
    n_tb = con.execute("SELECT COUNT(*) FROM ferro_tabelle").fetchone()[0]
    n_co = con.execute("SELECT COUNT(*) FROM ferro_corse").fetchone()[0]
    ok1 = (len(D["staz"]) == n_st and len(D["tab"]) == n_tb
           and len(D["corse"]) == n_co)
    esiti.append(("E1 conteggi HTML == DB", ok1,
                  "staz %d/%d, tab %d/%d, corse %d/%d"
                  % (len(D["staz"]), n_st, len(D["tab"]), n_tb,
                     len(D["corse"]), n_co)))

    # E2: transiti (l'HTML collassa i ripetuti)
    n_tr = con.execute("SELECT COUNT(*) FROM ferro_transiti").fetchone()[0]
    n_html = sum(len(c[2]) for c in D["corse"])
    rip = 0
    for c in con.execute("SELECT id FROM ferro_corse"):
        prec = None
        for t in con.execute("""SELECT stazione_id s, arrivo_min a, partenza_min p
                                FROM ferro_transiti WHERE corsa_id=?
                                ORDER BY sequenza""", (c["id"],)):
            cur = (t["s"], t["a"], t["p"])
            if prec == cur:
                rip += 1
            prec = cur
    ok2 = (n_html == n_tr - rip)
    esiti.append(("E2 transiti HTML == DB - ripetuti", ok2,
                  "HTML %d, DB %d - %d ripetuti = %d"
                  % (n_html, n_tr, rip, n_tr - rip)))

    # E4: tipo giorno distinguibile
    tipi = {t[1] for t in D["tab"]}
    ok4 = tipi == {"L", "F"}
    esiti.append(("E4 feriale e festivo distinti", ok4,
                  "codici presenti: %s" % sorted(tipi)))

    # E3: ricerca su campione ampio
    coppie = [("LECCE", "GAGLIANO L."), ("BARI CENTRO", "TARANTO"),
              ("BARI CENTRO", "MARTINA FRANCA"), ("MARTINA FRANCA", "TARANTO"),
              ("LECCE", "NARDO' CENTRALE"), ("NARDO' CENTRALE", "GALLIPOLI"),
              ("BARI CENTRO", "PUTIGNANO"), ("CASARANO", "LECCE"),
              ("MAGLIE", "LECCE"), ("TARANTO", "BARI CENTRO"),
              ("GAGLIANO L.", "LECCE"), ("BARI CENTRO", "CONVERSANO"),
              # tratti ancorati agli hub: partenza, arrivo o entrambi su uno
              # snodo della whitelist (il confine che cade sull'estremo del
              # tratto e' il caso limite della regola lato-partenza)
              ("PUTIGNANO", "BARI CENTRO"), ("MARTINA FRANCA", "PUTIGNANO"),
              ("LECCE", "ZOLLINO"), ("ZOLLINO", "GAGLIANO L."),
              ("MAGLIE", "OTRANTO"), ("CASARANO", "GAGLIANO L."),
              ("LECCE", "FRANCAVILLA FONTANA"), ("NOVOLI", "NARDO' CENTRALE")]
    giorni = ["2026-06-16", "2026-06-21", "2026-07-15", "2026-08-15",
              "2026-08-16", "2026-09-10"]
    nomi = D["staz"]
    diff = []
    n_conf = n_ris = 0
    for (a, b) in coppie:
        if a not in nomi or b not in nomi:
            diff.append("stazione assente: %s / %s" % (a, b))
            continue
        ia, ib = nomi.index(a), nomi.index(b)
        for g in giorni:
            for ora in (0, 8 * 60, 17 * 60):
                r1 = cerca_html(D, ia, ib, g, ora)
                r2 = cerca_db(con, a, b, g, ora, D["festivi"])
                # nell'HTML i confini sono indici, nel DB sono nomi
                r1n = [(p, ar, tuple(nomi[i] for i in c)) for (p, ar, c) in r1]
                n_conf += 1
                n_ris += len(r1n)
                if r1n != r2:
                    diff.append("%s->%s %s h%02d: HTML=%s DB=%s"
                                % (a, b, g, ora // 60, r1n[:2], r2[:2]))
    ok3 = not diff
    esiti.append(("E3 ricerca HTML == DB", ok3,
                  "%d confronti, %d risultati, %d divergenze"
                  % (n_conf, n_ris, len(diff))))

    # E5: soluzioni con un cambio, verificate sul DB
    prob5 = []
    n_sol = 0
    for (a, b) in [("BARI CENTRO", "TARANTO"), ("TARANTO", "BARI CENTRO"),
                   ("LECCE", "MARTINA FRANCA"), ("BARI CENTRO", "GALLIPOLI"),
                   ("PUTIGNANO", "TARANTO"), ("MAGLIE", "GALLIPOLI")]:
        if a not in nomi or b not in nomi:
            continue
        ia, ib = nomi.index(a), nomi.index(b)
        for g in ["2026-06-16", "2026-07-15", "2026-08-15"]:
            for sol in cerca_cambio_html(D, ia, ib, g, 6 * 60):
                n_sol += 1
                for p in verifica_cambio_su_db(con, D, a, b, g, sol):
                    prob5.append("%s->%s %s: %s" % (a, b, g, p))
    esiti.append(("E5 soluzioni con cambio verificate", not prob5,
                  "%d soluzioni, %d problemi" % (n_sol, len(prob5))))
    diff += prob5[:3]

    # E6: mezzo di ogni singola fermata (treno o bus sostitutivo)
    def seg_html(c):
        """Replica di segDiTransito(): i codici di testata non dichiarano
        confine (il cambio di numero e' amministrativo), vanno saltati."""
        tr, segs, out, k = c[2], c[1], [], 0
        for i in range(len(tr)):
            j = k + 1
            while j < len(segs) and segs[j][2] < 0:
                j += 1
            if j < len(segs) and tr[i][0] == segs[j][2]:
                k = j
            out.append(k)
        return out

    q_seg = ("SELECT s.mezzo, st.nome b FROM ferro_segmenti s "
             "LEFT JOIN ferro_stazioni st ON st.id = s.da_stazione_id "
             "WHERE s.corsa_id=? ORDER BY s.sequenza")
    q_tr = ("SELECT s.nome, tr.arrivo_min a, tr.partenza_min p "
            "FROM ferro_transiti tr JOIN ferro_stazioni s "
            "ON s.id=tr.stazione_id WHERE tr.corsa_id=? ORDER BY tr.sequenza")
    ids = [r[0] for r in con.execute("SELECT id FROM ferro_corse ORDER BY id")]
    prob6 = []
    n_ferm = n_bus = 0
    for c, cid in zip(D["corse"], ids):
        segs_db = con.execute(q_seg, (cid,)).fetchall()
        tr_db = con.execute(q_tr, (cid,)).fetchall()
        if not segs_db or not tr_db:
            continue
        # atteso dal DB: si viaggia col mezzo del segmento entrante, e si cambia
        # mezzo alla stazione che un segmento successivo dichiara come confine
        conf = {r["b"]: r["mezzo"] for r in segs_db if r["b"]}
        corrente = segs_db[0]["mezzo"]
        atteso, prec = [], None
        for r in tr_db:
            if r["nome"] in conf:
                corrente = conf[r["nome"]]
            cur = (r["nome"], r["a"], r["p"])
            if prec == cur:      # transito ripetuto: l'HTML lo collassa
                continue
            prec = cur
            atteso.append(corrente)
        trovato = [("bus" if c[1][k][1] == 1 else "treno") for k in seg_html(c)]
        n_ferm += len(trovato)
        n_bus += sum(1 for m in trovato if m == "bus")
        if trovato != atteso:
            prob6.append("corsa %d: app=%s db=%s"
                         % (cid, trovato[:5], atteso[:5]))
    esiti.append(("E6 mezzo di ogni fermata", not prob6,
                  "%d fermate (%d in bus), %d divergenze"
                  % (n_ferm, n_bus, len(prob6))))
    diff += prob6[:3]

    # E7: l'avviso sul mezzo deve comparire ESATTAMENTE quando serve.
    # Non basta guardare i punti di trasbordo che cadono dentro il tratto: chi
    # sale a valle del trasbordo (Casarano -> Gagliano) non incontra nessun
    # cambio eppure viaggia interamente in autobus, e deve saperlo. Qui il caso
    # viene deciso sui mezzi delle fermate ricavati dal DB, non dalla struttura
    # dei segmenti dell'HTML.
    def mezzi_da_db(cid):
        segs_db = con.execute(q_seg, (cid,)).fetchall()
        tr_db = con.execute(q_tr, (cid,)).fetchall()
        conf = {r["b"]: r["mezzo"] for r in segs_db if r["b"]}
        corrente = segs_db[0]["mezzo"] if segs_db else "treno"
        out, prec = [], None
        for r in tr_db:
            if r["nome"] in conf:
                corrente = conf[r["nome"]]
            cur = (r["nome"], r["a"], r["p"])
            if prec == cur:
                continue
            prec = cur
            out.append((r["nome"], corrente))
        return out

    prob7 = []
    n_tratti = n_avv = n_solobus = 0
    for (a, b) in coppie:
        if a not in nomi or b not in nomi:
            continue
        ia, ib = nomi.index(a), nomi.index(b)
        for g in ["2026-06-16", "2026-07-15", "2026-08-15"]:
            for c, cid in zip(D["corse"], ids):
                t = D["tab"][c[0]]
                if not (t[2] <= g <= t[3]
                        and t[1] == tipo_giorno(g, D["festivi"])):
                    continue
                tr = c[2]
                s = e = -1
                for i, x in enumerate(tr):
                    if s < 0 and x[0] == ia and x[2] is not None:
                        s = i
                    elif s >= 0 and x[0] == ib:
                        e = i
                        break
                if s < 0 or e < 0:
                    continue
                n_tratti += 1
                # atteso: i mezzi delle fermate s..e secondo il DB
                mdb = [m for (_, m) in mezzi_da_db(cid)[s:e]]
                att_bus = "bus" in mdb
                att_misto = att_bus and "treno" in mdb
                att_solobus = att_bus and "treno" not in mdb
                # trovato: quello che l'app dice (replica di mezziTratto)
                sd = seg_html(c)
                # segmenti su cui si VIAGGIA: lato partenza, s..e-1 (il
                # confine sulla stazione di discesa non e' un cambio)
                mapp = [("bus" if c[1][k][1] == 1 else "treno")
                        for k in sd[s:e]]
                app_bus = "bus" in mapp
                app_misto = app_bus and "treno" in mapp
                app_solobus = app_bus and "treno" not in mapp
                if (att_misto, att_solobus) != (app_misto, app_solobus):
                    prob7.append(
                        "%s->%s %s corsa %d: db(misto=%s,solobus=%s) "
                        "app(misto=%s,solobus=%s)"
                        % (a, b, g, cid, att_misto, att_solobus,
                           app_misto, app_solobus))
                if app_misto:
                    n_avv += 1
                if app_solobus:
                    n_solobus += 1
    esiti.append(("E7 avviso sul mezzo quando serve", not prob7,
                  "%d tratti: %d con trasbordo, %d tutti in bus, %d divergenze"
                  % (n_tratti, n_avv, n_solobus, len(prob7))))
    diff += prob7[:3]

    # E8: fermate stradali dei bus, HTML == DB, per (tabella, stazione):
    # le fermate valgono per DIREZIONE (Noci: andata un punto, ritorno quattro)
    tid_ord = [r[0] for r in con.execute(
        "SELECT id FROM ferro_tabelle ORDER BY id")]
    tid2i = {t_: i for i, t_ in enumerate(tid_ord)}
    sid_ord = [r[0] for r in con.execute(
        "SELECT id FROM ferro_stazioni ORDER BY id")]
    sid2i = {s_: i for i, s_ in enumerate(sid_ord)}
    fb_db = {(tid2i[r[0]], sid2i[r[1]]): r[2] for r in con.execute(
        "SELECT tabella_id, stazione_id, testo FROM ferro_fermate_bus")}
    ftxt = D.get("ftxt", [])
    fb_html = {(int(ti), int(si)): ftxt[k]
               for ti, m in D.get("fbus", {}).items()
               for si, k in m.items()}
    ok8 = fb_db == fb_html
    esiti.append(("E8 fermate bus HTML == DB", ok8,
                  "HTML %d, DB %d, testi unici %d%s"
                  % (len(fb_html), len(fb_db), len(ftxt),
                     "" if ok8 else ", differiscono")))

    # E9: ricerca per numero di corsa, HTML == DB
    # La scheda "Corsa" cerca il codice nei segmenti: per un campione di codici
    # (treno, bus, incastonati) confronto le corse trovate nei dati dell'HTML
    # con una query indipendente sul DB: stesse corse, stessi capilinea, stessi
    # orari di partenza e arrivo, stessa validita'.
    # inclusi i codici bus con suffisso numerico (100B1, 105B1: la cifra dopo
    # la B distingue le corse della stessa autolinea) e uno inesistente
    campione = ["92151", "109B", "761B", "92332", "103B", "90701", "999999",
                "100B1", "105B1", "105B2", "870B2", "941B1"]
    prob9 = []
    n_trovate = 0
    for q in campione:
        da_html = []
        for c in D["corse"]:
            if not any(sg[0] == q for sg in c[1]):
                continue
            t = D["tab"][c[0]]
            tr = c[2]
            part = tr[0][2] if tr[0][2] is not None else tr[0][1]
            arr = tr[-1][1] if tr[-1][1] is not None else tr[-1][2]
            da_html.append((t[2], t[3], t[1], nomi[tr[0][0]],
                            nomi[tr[-1][0]], part, arr))
        da_db = []
        for r in con.execute(
                """SELECT DISTINCT c.id, t.dal, t.al, t.tipo_giorno
                   FROM ferro_corse c JOIN ferro_tabelle t ON t.id=c.tabella_id
                   JOIN ferro_segmenti s ON s.corsa_id=c.id
                   WHERE s.codice=?""", (q,)):
            tr = con.execute(
                """SELECT s.nome, tr.arrivo_min a, tr.partenza_min p
                   FROM ferro_transiti tr JOIN ferro_stazioni s
                   ON s.id=tr.stazione_id WHERE tr.corsa_id=?
                   ORDER BY tr.sequenza""", (r["id"],)).fetchall()
            part = tr[0]["p"] if tr[0]["p"] is not None else tr[0]["a"]
            arr = tr[-1]["a"] if tr[-1]["a"] is not None else tr[-1]["p"]
            da_db.append((r["dal"], r["al"],
                          "F" if r["tipo_giorno"].lower().startswith("festiv")
                          else "L", tr[0]["nome"], tr[-1]["nome"], part, arr))
        n_trovate += len(da_html)
        if sorted(da_html) != sorted(da_db):
            prob9.append("codice %s: HTML %d corse, DB %d"
                         % (q, len(da_html), len(da_db)))
    esiti.append(("E9 ricerca per codice HTML == DB", not prob9,
                  "%d codici, %d corse trovate, %d divergenze"
                  % (len(campione), n_trovate, len(prob9))))
    diff += prob9[:3]

    print("\n=== EQUIVALENZA PWA TRENI ===")
    for nome, ok, det in esiti:
        print("  [%s] %-36s %s" % ("PASS" if ok else "FAIL", nome, det))
    for d in diff[:6]:
        print("    - %s" % d)
    ko = [n for n, ok, _ in esiti if not ok]
    print("\nESITO: %s" % ("PASS" if not ko else "FAIL (%s)" % ", ".join(ko)))
    sys.exit(1 if ko else 0)


if __name__ == "__main__":
    main()
