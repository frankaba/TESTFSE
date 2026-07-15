#!/usr/bin/env python3
"""
test_guasti_ferro.py - il validatore ferroviario deve SCATTARE sui guasti.

Uso:
    python3 src/test_guasti_ferro.py dist/ferro.db pdf/ferroviario/*.pdf
    python3 src/test_guasti_ferro.py dist/ferro.db pdf/ferroviario/*.pdf --con-build

Un validatore che non fallisce mai non dimostra nulla: qui ogni controllo di
valida_ferro.py (F1-F10 e baseline) viene messo davanti a un guasto sintetico
iniettato in una COPIA del DB, e il test pretende che il controllo previsto
scatti (ESITO FAIL con il token atteso nell'output). Il DB vero non viene
toccato; la baseline attese_ferro.json e' letta, mai riscritta.

Con --con-build si esegue anche il contro-esempio della whitelist hub:
build_ferro con SNODI_AMMESSI senza PUTIGNANO deve FALLIRE la build (le corse
miste della linea 1 non trovano piu' un confine ammesso). E' lento (rilegge
tutti i PDF) quindi e' opzionale.

Non e' nel gate di aggiorna.py: ogni guasto rilancia valida_ferro.py che
rilegge tutti i PDF (F1), ~mezzo minuto a guasto. Va lanciato quando si mette
mano ai validatori o al parser, non a ogni rigenerazione.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

SRC = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SRC)
from build_ferro import SNODI_AMMESSI      # noqa: E402  (whitelist unica)

PY = sys.executable


def valida(db, pdfs):
    r = subprocess.run([PY, os.path.join(SRC, "valida_ferro.py"), db] + pdfs,
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    con_build = "--con-build" in sys.argv
    if len(argv) < 2:
        print(__doc__)
        sys.exit(2)
    db_vero, pdfs = argv[0], argv[1:]

    # ogni guasto: (nome, [SQL...], token che DEVE comparire nell'output FAIL)
    # dove il SQL ha bisogno di id concreti, li risolve la query fra parentesi.
    GUASTI = [
        ("F1: orario sparito (DELETE di un transito)",
         ["DELETE FROM ferro_transiti WHERE id="
          "(SELECT id FROM ferro_transiti WHERE arrivo_min IS NOT NULL"
          " AND partenza_min IS NOT NULL LIMIT 1)"],
         "F1"),
        ("F1b: valore corrotto (+1 minuto, conteggi invariati)",
         ["UPDATE ferro_transiti SET partenza_min=partenza_min+1 WHERE id="
          "(SELECT id FROM ferro_transiti WHERE partenza_min IS NOT NULL"
          " LIMIT 1)"],
         "orari_valore_divergente"),
        ("F2: corsa ridotta a un transito",
         ["DELETE FROM ferro_transiti WHERE corsa_id="
          "(SELECT corsa_id FROM ferro_transiti GROUP BY corsa_id"
          " HAVING COUNT(*)>2 LIMIT 1) AND sequenza>1"],
         "F2"),
        ("F3/baseline: arrivo e partenza scambiati",
         ["UPDATE ferro_transiti SET arrivo_min=partenza_min,"
          " partenza_min=arrivo_min WHERE id="
          "(SELECT id FROM ferro_transiti WHERE arrivo_min IS NOT NULL"
          " AND partenza_min IS NOT NULL AND arrivo_min<partenza_min LIMIT 1)"],
         "arrivo_dopo_partenza"),
        ("F5: tabella senza corse",
         ["INSERT INTO ferro_tabelle (linea,tipo_giorno,dal,al,file,pagina,"
          "ordine,y_testata) VALUES ('X','Feriale','2026-06-14','2026-09-13',"
          "'sintetico.pdf',1,0,0)"],
         "F5"),
        ("F5: periodo rovescio (dal > al)",
         ["UPDATE ferro_tabelle SET dal='2026-09-30' WHERE id="
          "(SELECT id FROM ferro_tabelle LIMIT 1)"],
         "F5"),
        ("F6/baseline: orario spostato di 10 ore",
         ["UPDATE ferro_transiti SET partenza_min=partenza_min+600 WHERE id="
          "(SELECT tr.id FROM ferro_transiti tr WHERE tr.partenza_min NOT NULL"
          " AND tr.sequenza=1 LIMIT 1)"],
         "orari_non_monotoni"),
        ("F7: buco fra periodi contigui",
         ["UPDATE ferro_tabelle SET dal=date(dal,'+1 day') WHERE dal="
          "(SELECT MIN(t2.dal) FROM ferro_tabelle t1"
          " JOIN ferro_tabelle t2 ON t1.linea=t2.linea"
          " AND t1.tipo_giorno=t2.tipo_giorno AND t2.dal>t1.al)"],
         "F7"),
        ("F8: mezzo incoerente col codice",
         ["UPDATE ferro_segmenti SET mezzo='treno' WHERE id="
          "(SELECT id FROM ferro_segmenti WHERE mezzo='bus' LIMIT 1)"],
         "F8"),
        ("F10: confine di trasbordo cancellato",
         ["UPDATE ferro_segmenti SET da_stazione_id=NULL WHERE id="
          "(SELECT id FROM ferro_segmenti WHERE mezzo='bus'"
          " AND da_stazione_id IS NOT NULL AND sequenza>1 LIMIT 1)"],
         "F10"),
        ("F10: confine fuori whitelist hub",
         ["UPDATE ferro_segmenti SET da_stazione_id="
          "(SELECT id FROM ferro_stazioni WHERE nome NOT IN (%s) LIMIT 1)"
          " WHERE id=(SELECT id FROM ferro_segmenti"
          " WHERE da_stazione_id IS NOT NULL AND sequenza>1 LIMIT 1)"
          % ",".join("'%s'" % s.replace("'", "''") for s in SNODI_AMMESSI)],
         "FUORI WHITELIST"),
        ("baseline: fermata bus sparita",
         ["DELETE FROM ferro_fermate_bus WHERE rowid="
          "(SELECT rowid FROM ferro_fermate_bus LIMIT 1)"],
         "fermate_bus"),
    ]

    tmpdir = tempfile.mkdtemp(prefix="guasti_ferro_")
    esiti = []

    # sanita': la copia INTATTA deve passare, altrimenti i FAIL qui sotto
    # non dimostrerebbero nulla
    db0 = os.path.join(tmpdir, "sano.db")
    shutil.copy(db_vero, db0)
    rc, out = valida(db0, pdfs)
    esiti.append(("copia intatta -> PASS", rc == 0))
    if rc != 0:
        print(out)
        print("\nLa copia intatta non passa: guasti non verificabili.")
        sys.exit(1)

    for i, (nome, sqls, token) in enumerate(GUASTI):
        db = os.path.join(tmpdir, "guasto%02d.db" % i)
        shutil.copy(db_vero, db)
        con = sqlite3.connect(db)
        cambi = 0
        for sql in sqls:
            con.execute(sql)
            cambi += con.total_changes - cambi
        con.commit()
        con.close()
        if cambi == 0:
            esiti.append((nome + "  [guasto NON iniettato]", False))
            continue
        rc, out = valida(db, pdfs)
        colto = rc != 0 and token in out
        esiti.append(("%s -> FAIL con '%s'" % (nome, token), colto))
        if not colto:
            print("--- output inatteso per: %s (exit %d) ---" % (nome, rc))
            print("\n".join(l for l in out.splitlines()
                            if "FAIL" in l or "SCOSTA" in l or "- " in l)[:800])

    if con_build:
        # contro-esempio della whitelist: senza PUTIGNANO la build della
        # linea 1 (corse miste bus+treno) non trova un confine ammesso
        import build_ferro
        vecchi = build_ferro.SNODI_AMMESSI
        build_ferro.SNODI_AMMESSI = vecchi - {"PUTIGNANO"}
        db = os.path.join(tmpdir, "senza_putignano.db")
        argv_vecchio = sys.argv
        sys.argv = ["build_ferro.py", db] + pdfs
        import io
        import contextlib
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                build_ferro.main()
            code = 0
        except SystemExit as e:
            code = e.code or 0
        finally:
            sys.argv = argv_vecchio
            build_ferro.SNODI_AMMESSI = vecchi
        colto = code != 0 and "nessun ordine" in buf.getvalue()
        esiti.append(("whitelist senza PUTIGNANO -> build FAIL", colto))

    shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n=== GUASTI SINTETICI (valida_ferro deve scattare) ===")
    ok = True
    for nome, esito in esiti:
        print("  [%s] %s" % ("OK " if esito else "KO ", nome))
        ok &= esito
    print("\nESITO: %s" % ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
