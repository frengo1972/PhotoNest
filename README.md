# PhotoNest

PhotoNest consolida foto e video provenienti da Google Takeout, telefoni Android e iPhone in un
**archivio unico, ordinato, per utente**, conservando i metadati (data, GPS, persone, descrizioni e album).

> Progetto locale e privacy-first: gli originali restano intatti e le funzioni AI possono funzionare sul PC.

## Obiettivo

- Recuperare tutto: Google Takeout di ogni account + copia completa dei telefoni.
- Ripristinare i metadati che Takeout tiene nei `.json` separati, scrivendoli in EXIF/XMP.
- Eliminare i duplicati (anche ricompressi da "Risparmio spazio" o convertiti HEIC→JPEG) senza perdere metadati.
- Ordinare per utente / anno / evento e taggare (eventi, persone, oggetti) con modelli locali.
- Archivio finale su NAS QNAP: cartelle + XMP, leggibile da qualunque software (Immich, digiKam, QuMagie...).

## Struttura

```
income/     file grezzi da smistare (zip Takeout, copie telefoni). Mai modificati dal tool.
docs/       istruzioni, architettura, decisioni
work/       (generata) catalogo SQLite, miniature, report. Ricostruibile.
src/        codice Python (src/photoorg) e interfaccia web (src/photoorg/web)
```

## Uso

Doppio clic sull'icona **Archivio foto** sul desktop: si apre l'app in una finestra propria.
Da lì si fa tutto: la **Panoramica** elenca i passi e quanto resta da fare; *Aggiorna archivio* legge le fonti nuove.
Chiusa la finestra, il server locale si spegne da solo dopo qualche minuto.

| Schermata | Cosa fa |
|---|---|
| Panoramica | i passi da fare, con quanti elementi restano per ciascuno |
| Galleria | foto per giorno; − / + o Ctrl+rotellina per la dimensione delle anteprime; **Cerca** a parole ("mare", "bambini con la torta", un nome); clic = visualizzatore |
| Eventi | foto raggruppate per tempo e luogo; il nome diventa la cartella sul NAS |
| Persone | volti riconosciuti e raggruppati sul PC; *Chi è?* dà il nome a tutte le foto del gruppo |
| Duplicati | confronto affiancato delle copie; *Tieni questa*, *Non è un duplicato*, *Va bene così*, poi *Applica* |
| Raffiche | scatti nello stesso secondo: *Tieni solo questa* (la più nitida è segnata) o *Tienile tutte* |
| Date da verificare | date incoerenti o mancanti, con la spiegazione; correzione sul posto |
| Cestino | foto escluse dall'archivio, *Rimetti* per recuperarle |
| Fonti | account e telefoni letti, avanzamento dell'aggiornamento, cartella income |
| Archivio NAS | destinazione scelta dall'utente (per esempio `X:\ArchivioFoto`), anteprima di cosa farà, *Crea/Aggiorna l'archivio* |

**Azioni** — nessuna tocca gli originali in income; "escludere" significa non copiare nell'archivio finale.

| Dove | Come |
|---|---|
| Galleria | clic sul cerchio di una foto (o Ctrl+clic, Maiusc+clic per un intervallo, Ctrl+A tutte) → barra in basso: *Preferite*, *Album*, *Persone*, *Etichette*, *Data*, *Escludi* |
| Visualizzatore | ← → o trascina per sfogliare, trascina in basso o Esc per chiudere, clic per zoom 1:1; `F` preferita, `Canc` escludi, `I` informazioni: lì si correggono data, album, persone, etichette |
| Ritocco (`R` nel visualizzatore) | *Colori ed esposizione*, *Ripristina i volti* (GFPGAN), *Rimuovi macchie e graffi* col pennello (LaMa), *Ingrandisci* ×2/×4 (Real-ESRGAN), tutto sul PC; intensità regolabile, *Annulla l'ultimo*, Spazio tenuto premuto per confrontare con prima; *Salva come nuova versione* |
| Foto ritoccata | la galleria mostra la versione (icona bacchetta); `O` nel visualizzatore mostra l'originale; in *Informazioni* → *Torna all'originale*; sul NAS va accanto all'originale come `…_ritoccata.jpg` |
| Ovunque | dopo un'esclusione compare *Annulla* |

Le modifiche sono salvate per contenuto (sha256): restano valide dopo ogni aggiornamento, anche se arriva una copia migliore della stessa foto.

Prima installazione (una volta, prerequisiti in [docs/setup.md](docs/setup.md)):

```powershell
uv sync                     # crea l'ambiente Python 3.12
uv run photoorg shortcut    # crea l'icona "Archivio foto" sul desktop
```

Sviluppo: `uv run photoorg dev` avvia l'app con un supervisore che **riavvia il server a ogni modifica dei file .py**
(aspettando la fine di un aggiornamento/export/analisi in corso) e la pagina **si ricarica da sola** quando cambiano
server o file web, restando sulla stessa schermata. Log in console (o in `work/dev.log` se avviato in background).
Il supervisore stesso (`devserver.py`) va riavviato a mano se lo si modifica.

Riga di comando (facoltativa, per automazioni e test):

```powershell
uv run photoorg             # apre l'app (come l'icona)
uv run photoorg scan        # aggiornamento completo con riepilogo testuale
uv run pytest               # test
```

`scan` è incrementale: rilanciato, rielabora solo file nuovi o cambiati. Il catalogo è in `work/catalog.db`
(SQLite, apribile con DB Browser for SQLite). Le versioni ritoccate sono in `work/versions` (gli originali non
vengono mai modificati); i modelli AI si scaricano al primo uso in `%LOCALAPPDATA%\photoorg\models`.

| Comando | Fase | Cosa fa |
|---|---|---|
| `ingest` | 2 | legge zip e cartelle, abbina i JSON di Takeout, calcola sha256/pHash, legge EXIF |
| `normalize` | 3 | sceglie data (EXIF › nome file › Google › mtime), fuso, GPS, descrizione, coppie Live/Motion |
| `dedup` | 4 | raggruppa le copie in foto uniche, sceglie la migliore, unisce i metadati |
| `report` | — | fonti, copertura metadati, elementi da verificare, riepilogo duplicati |
| `app` | — | l'app (server locale su 127.0.0.1 + finestra Edge); anteprime in `work/cache` |
| `shortcut` | — | crea l'icona sul desktop |

## Documentazione

| Documento | Contenuto |
|---|---|
| [docs/architettura.md](docs/architettura.md) | Pipeline, scelte tecniche, regole di dedup |
| [docs/setup.md](docs/setup.md) | Strumenti da installare sul PC |
| [docs/takeout.md](docs/takeout.md) | Export Google Photos passo passo |
| [docs/android.md](docs/android.md) | Copia foto da telefono Android |
| [docs/iphone.md](docs/iphone.md) | Copia foto da iPhone / iCloud |
| [income/README.md](income/README.md) | Come organizzare i file in ingresso |

## Roadmap

- [x] Importazione incrementale da Google Takeout, Android e iPhone, con normalizzazione dei metadati.
- [x] Deduplica, gestione delle varianti, catalogo locale e archivio esportabile su NAS.
- [x] Galleria locale, ricerca in italiano, persone, eventi, ritocco e descrizioni AI opzionali.
- [ ] Rendere più semplice la configurazione iniziale e la distribuzione su Windows.
- [ ] Aggiungere integrazioni e test per altri formati, fonti e modelli locali.
- [ ] Preparare una guida per contribuire e una licenza open source.

## Regola d'oro

**Non cancellare nulla** da account Google, telefoni o iCloud finché l'archivio finale non è
verificato e con backup. Il tool non cancella mai: sposta in quarantena e produce report.
