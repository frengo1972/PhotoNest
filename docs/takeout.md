# Export da Google Photos con Takeout — passo passo

Da ripetere **per ogni account Google** che vuoi includere nell'archivio. Ogni titolare dell'account
deve scaricare i propri zip e trasferirli con il metodo che preferisce.

> Perché Takeout: dal 31/03/2025 l'API di Google Photos non permette più di leggere l'intera libreria.
> Takeout è l'unico modo per avere **originali + tutti i metadati**.

## 0. Prima di esportare (5 minuti, per account)

Su [photos.google.com](https://photos.google.com) con l'account giusto:

1. **Cestino**: contiene foto che vuoi tenere? Ripristinale. Il cestino **non** viene esportato
   e dopo 60 giorni si svuota da solo.
2. **Cartella protetta** (Locked folder, solo da app telefono): **non** viene esportata.
   Se contiene foto da salvare, spostale fuori.
3. **Archivio**: viene esportato, nessuna azione.
4. Annota quante foto/video ha l'account (app Google Foto › Raccolte, o
   [one.google.com/storage](https://one.google.com/storage)) — servirà a verificare l'export.

## 1. Avviare l'export

1. Apri **[takeout.google.com](https://takeout.google.com)**.
   - Controlla in alto a destra l'**avatar**: deve essere l'account da esportare.
     Con più account consiglio una finestra in incognito per account, per non confondersi.
2. In cima alla lista clicca **"Deseleziona tutto"**.
3. Scorri fino a **"Google Foto"** e spunta solo quella.
4. Clicca **"Tutti gli album di foto inclusi"**:
   - **primo test**: deseleziona tutto e scegli **un solo album piccolo** (o un anno, es. "Photos from 2023").
     Permette di verificare il flusso prima dell'export completo. Si fa in pochi minuti.
   - **export completo**: lascia tutto selezionato.
   - Lascia invariata l'opzione formati (se presente).
5. In fondo: **"Passaggio successivo"**.

## 2. Opzioni di consegna

| Opzione | Scelta | Perché |
|---|---|---|
| Metodo di consegna | **Invia link per il download via email** | non richiede spazio aggiuntivo su Drive |
| Frequenza | **Esporta una volta** | |
| Tipo di file | **.zip** | |
| Dimensioni file | **50 GB** | Un account da 15 GB → un solo zip (o pochi) |

Clicca **"Crea esportazione"**.

## 3. Attesa

- Arriva una mail "Google Takeout: è in corso l'esportazione". Per 15 GB di solito **da qualche ora a 1–2 giorni**.
- Puoi chiudere tutto e spegnere il PC: lavora Google.
- Puoi avviare l'export di altri account in parallelo.

## 4. Download

1. Arriva la mail **"I tuoi dati Google sono pronti per il download"** → **"Scarica i tuoi file"**.
2. Google chiede di nuovo la password: normale.
3. Limiti da ricordare:
   - il link scade dopo **7 giorni**;
   - ogni file si può scaricare **max 5 volte**.
4. Usa un browser desktop su connessione stabile; per file grandi meglio Chrome/Edge che riprendono i download interrotti.

## 5. Dove mettere gli zip

Sposta gli zip, **senza estrarli**, in:

```
income/<utente>/google-<email>/
```

Esempio: `income/utente/google-nome@example.com/takeout-20260101T101500Z-001.zip`

Poi aggiorna la tabella "Stato fonti" in [income/README.md](../income/README.md).

## 6. Verifica rapida

- Somma delle dimensioni zip ≈ spazio usato dall'account (circa; Gmail e Drive non sono inclusi).
- Se la mail di Google dice "alcuni file non sono stati esportati", conservala: indica cosa manca.

## Cosa contiene lo zip (per riferimento)

```
Takeout/Google Foto/
  Photos from 2023/              ← tutte le foto, per anno
    IMG_1234.jpg
    IMG_1234.jpg.supplemental-metadata.json   ← data, GPS, persone, descrizione
  <Nome album>/                  ← copie delle stesse foto, per album
  metadata.json                  ← info album
```

Problemi noti che il tool gestirà: JSON con nomi troncati, versioni `-modificato`/`-edited`,
foto ripetute negli album, Motion/Live Photo divise in immagine + video, date file = data download.
