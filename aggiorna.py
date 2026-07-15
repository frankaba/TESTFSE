#!/usr/bin/env python3
"""
aggiorna.py - ricostruisce e valida l'app dai fascicoli orario.

Uso:
    1. scarica i fascicoli orario delle linee ferroviarie dal sito FSE
       (www.fssudest.it) e mettili in pdf/ferroviario/ (un PDF per linea)
    2. python3 aggiorna.py

Fasi (si ferma alla prima che fallisce, exit code != 0):
    build_ferro.py             PDF -> dist/ferro.db
    valida_ferro.py            controlli F1-F10 + baseline attese_ferro.json
    gen_treni.py               dist/ferro.db -> dist/treni.html (app offline)
    test_equivalenza_ferro.py  l'app dice quello che dice il DB (E1-E9)

Se pdf/ferroviario/ e' vuota non c'e' nulla da ricostruire: in dist/ trovi
comunque l'app gia' generata dall'ultima edizione dei fascicoli.
"""

import glob
import os
import subprocess
import sys

RADICE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(RADICE, "src")
PY = sys.executable


def fase(nome, cmd):
    print("\n>>> %s" % nome)
    r = subprocess.run(cmd, cwd=RADICE)
    print("   -> %s (exit %d)" % ("OK" if r.returncode == 0 else "FALLITA",
                                  r.returncode))
    return r.returncode == 0


def main():
    ferro_pdf = sorted(glob.glob(os.path.join(RADICE, "pdf", "ferroviario",
                                              "*.pdf")))
    if not ferro_pdf:
        print(__doc__)
        print("pdf/ferroviario/ e' vuota: scarica i fascicoli e riprova.")
        print("L'app gia' generata e' in dist/treni.html.")
        sys.exit(2)

    ferro_db = os.path.join(RADICE, "dist", "ferro.db")
    treni = os.path.join(RADICE, "dist", "treni.html")
    fonts = os.path.join(RADICE, "fonts")
    if os.path.exists(ferro_db):
        os.remove(ferro_db)

    esiti = []
    for nome, cmd in [
        ("build_ferro.py (dist/ferro.db)",
         [PY, os.path.join(SRC, "build_ferro.py"), ferro_db, *ferro_pdf]),
        ("valida_ferro.py (F1-F10 + baseline)",
         [PY, os.path.join(SRC, "valida_ferro.py"), ferro_db, *ferro_pdf]),
        ("gen_treni.py (dist/treni.html)",
         [PY, os.path.join(SRC, "gen_treni.py"), ferro_db, treni, fonts]),
        ("test_equivalenza_ferro.py (E1-E9)",
         [PY, os.path.join(SRC, "test_equivalenza_ferro.py"), treni,
          ferro_db]),
    ]:
        ok = fase(nome, cmd)
        esiti.append((nome, ok))
        if not ok:
            break

    print("\n" + "=" * 60)
    for nome, ok in esiti:
        print("  [%s] %s" % ("OK " if ok else "KO ", nome))
    print("=" * 60)
    tutto = all(ok for _, ok in esiti) and len(esiti) == 4
    print("ESITO COMPLESSIVO: %s" % ("PASS" if tutto else "FAIL"))
    if tutto:
        print("output: %s" % treni)
    sys.exit(0 if tutto else 1)


if __name__ == "__main__":
    main()
