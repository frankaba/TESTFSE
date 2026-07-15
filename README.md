# Orari treni Sud Est — app non ufficiale, consultabile offline

App **non ufficiale** per consultare l'orario dei treni FSE (Ferrovie del Sud
Est, Puglia): un solo file HTML che funziona senza rete, costruito leggendo i
fascicoli orario che FSE pubblica per i viaggiatori.

**Questa app non è affiliata a, né approvata da, Ferrovie del Sud Est.** Gli
orari possono cambiare in qualsiasi momento: per il servizio effettivo fa fede
esclusivamente la comunicazione ufficiale FSE (www.fssudest.it, n. verde
800 079090).

## Uso immediato

Apri `dist/treni.html` con qualunque browser (anche da telefono, anche senza
connessione). Quattro schede:

- **Cerca** — da/a, giorno e ora: collegamenti diretti e, dove i diretti
  scarseggiano, soluzioni con un cambio (con l'avvertenza che la coincidenza
  tra corse diverse non è garantita dall'orario ufficiale);
- **Stazione** — prossime partenze da una stazione;
- **Treno** — inserisci il numero del treno o del bus (es. `92151` o `109B`)
  e vedi tratta, orari e periodo di validità;
- **Avvisi** — le note di sostituzione bus del periodo.

Il percorso di ogni corsa si apre toccando le pill dei codici: fermate con
orari, punto esatto in cui si cambia veicolo, e — per i tratti in autobus
sostitutivo — il **punto di fermata stradale** di ogni località (che vale per
direzione: all'andata e al ritorno il bus può fermare in punti diversi).

## Ricostruire l'app dai fascicoli

I PDF **non sono inclusi** in questo repository: sono documenti FSE e vanno
scaricati dalla fonte. Servono i fascicoli orario delle linee ferroviarie
(uno per linea, es. «Linea 1 feriale e festivo — dal … al …»).

    1. scarica i fascicoli da www.fssudest.it
    2. mettili in pdf/ferroviario/
    3. python3 aggiorna.py          (richiede Python 3 e pdfplumber)

La pipeline è: parser (`build_ferro.py`) → validazione (`valida_ferro.py`,
controlli F1–F10 contro il PDF e contro la baseline `attese_ferro.json`) →
generazione (`gen_treni.py`) → equivalenza app↔dati (`test_equivalenza_ferro.py`,
E1–E9). Se una fase fallisce, la catena si ferma: l'app non viene mai
rigenerata da dati non riconciliati con la fonte.

## Filosofia dei dati

- Nell'app entra **solo ciò che sta nei fascicoli**: nessuna integrazione da
  altre fonti, nessuna correzione dei refusi della fonte (vengono contati e
  congelati in baseline, non riparati).
- Ogni orario del DB è riconciliato col PDF carattere per carattere (F1/F1b).
- La semantica delle corse multi-codice (dove si cambia numero, dove si cambia
  veicolo, quali stazioni sono hub di interscambio) è ricostruita e validata
  con vincoli espliciti: se un fascicolo futuro non li rispettasse, la build
  fallisce invece di indovinare.
- `src/test_guasti_ferro.py` inietta guasti sintetici e verifica che i
  controlli scattino: un validatore che non fallisce mai non dimostra nulla.

## Licenza

Codice sotto licenza MIT (vedi `LICENSE`). I dati degli orari appartengono a
FSE: questo progetto ne estrae i fatti per uso personale di consultazione e
non ridistribuisce i documenti originali. Su richiesta del titolare, il
materiale derivato verrà rimosso. I font IBM Plex inclusi in `fonts/` sono
sotto SIL Open Font License 1.1.
