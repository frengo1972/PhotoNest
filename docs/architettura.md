# Architettura

## Principi

1. **Gli originali non si toccano.** `income/` è sola lettura per il tool.
2. **Provenienza sempre tracciata.** Ogni file sa a quale utente appartiene e da quale fonte arriva.
3. **Metadati dentro i file (EXIF/XMP), non solo in un database.** L'archivio deve sopravvivere a qualsiasi software.
4. **Nessuna cancellazione automatica.** Dedup = report + quarantena, decisione finale umana.
5. **Elaborazione sul laptop, archivio sul NAS.**

## Pipeline

```
income/ ──► 1 ingest ──► 2 normalizza ──► 3 catalogo ──► 4 dedup ──► 5 eventi/tag ──► 6 export
            (zip,         (JSON Takeout     (SQLite in      (report     (tempo+GPS,       NAS: /Foto/<utente>/
             telefoni)     → EXIF/XMP)       work/)          HTML)       volti, CLIP)       AAAA/AAAA-MM-GG_evento/
```

### 1. Ingest
Legge zip Takeout senza estrarli e cartelle dei telefoni. Utente e fonte ricavati dal percorso
(vedi [income/README.md](../income/README.md)).

### 2. Normalizzazione
- Abbina ogni media al suo `.supplemental-metadata.json` (nomi troncati, `(1)`, `-modificato`).
- Dal JSON: `photoTakenTime`, `geoData`, `description`, `people[].name`, album, preferiti, archivio.
- Gestisce Motion Photo / Live Photo (immagine + video come coppia).
- Date per file senza EXIF: JSON Takeout → nome file (WhatsApp, screenshot) → data file (ultima scelta).
- Ogni data va **validata** (range plausibile, es. 1990–oggi): EXIF e JSON possono essere entrambi sbagliati.
- Fuso orario: EXIF di solito è ora locale senza offset, JSON è UTC → la differenza dà l'offset.

#### Osservazioni dal primo Takeout reale (data di prova, account utente, album "Gita al mare 2019-10-09")

- **Nomi localizzati**: cartelle anno `Foto da AAAA` (non `Photos from AAAA`), metadati album in `metadati.json`.
  Il codice non deve dipendere dalla lingua.
- **Collisione nomi**: `_DSC2630.jpg` e `_DSC2630.JPG` (due elementi distinti nel cloud) diventano
  `_DSC2630.jpg` + `_DSC2630(1).JPG`; il JSON del secondo è `_DSC2630.JPG.supplemental-metadata(1).json`
  (il `(1)` va dopo `metadata`, non dopo il nome).
- Le due `_DSC2630` sono **varianti**, non copie: stessa data EXIF e stessa fotocamera, ma una è ritagliata
  (3310×2137) e l'altra intera (4898×3267). Il dedup deve distinguere *duplicato* da *variante* (ritaglio/modifica).
- **Risparmio spazio**: foto Sony A7R II (42 MP originali) presenti a ~4898×3267 = 16 MP → ridimensionate da Google.
  Gli originali vanno cercati altrove (HD, NAS); se trovati, vincono sempre sulla copia cloud.
- **Date sbagliate ovunque**: `2015-02-13_267.JPG` ha EXIF `4501:01:01`, JSON `13/07/1972`; quella giusta è nel nome file.
- **Descrizioni automatiche**: caricamenti via MultCloud hanno `description` = "… Uploaded by MultCloud at …"
  → rumore, da scartare (riconoscere anche `googlePhotosOrigin.thirdPartyApp`).
- `geoData` a 0,0 = assenza di posizione, non coordinate reali.

### 3. Catalogo
SQLite in `work/catalog.db`. Per ogni file: utente, fonte, percorso origine, sha256, pHash,
dimensioni, risoluzione, data scatto, GPS, persone, album, descrizione, tipo (foto/video/live).

### 4. Dedup (per utente)

| Livello | Metodo | Trova |
|---|---|---|
| 1 | sha256 | copie identiche (stesso file su più account) → sempre unite |
| 2 | pHash ≤ 10 + stesso istante EXIF + **stesso nome file** + stesse proporzioni | ricompressioni "Risparmio spazio", HEIC↔JPEG |
| 3 | pHash ≤ 4 + stesse proporzioni, se almeno una copia è senza EXIF | copie WhatsApp, download |
| — | stesso istante EXIF e stessa fotocamera, ma non duplicati | **varianti**: ritagli, modifiche, raffiche → tenute tutte, collegate |
| futuro | pHash su keyframe ffmpeg (video), similarità CLIP | video ricompressi, quasi-duplicati |

Perché il nome file: le raffiche hanno stesso secondo EXIF e immagini quasi uguali
(caso reale: Sony `_DSC2499`/`_DSC2500`, pHash 8, nessun sottosecondo EXIF). Distingue i due scatti solo
la numerazione della fotocamera. Una copia ricompressa da Google conserva invece il nome originale.
Un file rinominato a mano finisce tra le varianti: tenuto doppio, mai perso.

Scelta della **copia tenuta**: più pixel → data EXIF reale → origine telefono/disco prima del cloud → non modificata
→ file più grande. I metadati delle altre copie (GPS, album, persone, preferito, descrizione, data più affidabile)
vengono **uniti** nell'asset. Duplicati tra utenti diversi: mantenuti in entrambi.

**Revisione** (`photoorg review`, pagina locale su 127.0.0.1): gruppi simili e identici con miniature affiancate.
Azioni: "Tieni questa", "Non è un duplicato", "Confermo il gruppo". Le decisioni sono salvate per sha256
(sopravvivono a ricalcoli e nuove fonti) e applicate con "Ricalcola" o `photoorg dedup`.

### 5. Eventi e tag
- **Eventi**: cluster per salto temporale (es. > 3 h) + distanza GPS; nome da reverse geocoding offline.
- **Volti**: InsightFace + clustering; i nomi già presenti in Takeout fanno da base.
- **Scene/oggetti**: CLIP zero-shot su vocabolario controllato.
- **Didascalie** (opzionale): VLM locale su selezione.
- Output in XMP: `dc:subject` (keyword), `MWG-RS` (regioni volti), `dc:description`.

### 6. Export
Copia (non sposta) sul NAS: `/Foto/<utente>/AAAA/AAAA-MM-GG_evento/`, con XMP.
Report finale: conteggi per fonte, duplicati risolti, file senza data, errori.

### 5b. AI locale (implementata)

Tutto gira su questo PC, nessuna foto esce. Modelli in `%LOCALAPPDATA%\photoorg\models`, scaricati al primo uso.

| Compito | Modello | Esecuzione |
|---|---|---|
| Volti: rilevamento | YuNet (OpenCV zoo) | CPU, ~50 ms/foto a 1600 px |
| Volti: identità | SFace 128-d (OpenCV zoo) | CPU |
| Etichette automatiche | CLIP ViT-B/32 ONNX (Xenova) zero-shot su `ai/vocab.py` | GPU via DirectML (niente CUDA/PyTorch) |
| Ricerca in italiano | clip-ViT-B-32-multilingual-v1 (stesso spazio di CLIP) | CPU, solo il testo cercato |

- Ogni foto è decodificata una volta; risultati salvati per sha256 (`ai_done`, `clip_embeddings`, `faces`): mai rifatti.
- Etichette: CLIP è affidabile sul soggetto principale (0,75–1,0 sulle foto reali) ma non separa una seconda
  etichetta vera da quelle sbagliate → etichetta principale + seconda solo se forte + implicazioni fisse
  (kitesurf ⇒ mare, sport). Il resto lo copre la ricerca per somiglianza.
- Ricerca: prima corrispondenze esatte (etichette, persone, album, eventi, descrizioni; "kitesurf" ⇒ "surf e kite"),
  poi somiglianza CLIP. Le somiglianze multilingue sono compresse: conta quanto una foto si stacca dalla mediana
  della collezione (0,04), oppure un valore alto in assoluto (0,25) per collezioni omogenee.
- Volti: gruppi di volti simili (coseno ≥ 0,45), suggerimento del nome se vicino a una persona già nominata (≥ 0,42).
  Dare un nome a un gruppo = tag "persona" su tutte le sue foto (finisce nel NAS come gli altri tag).
- Nel NAS le etichette automatiche vanno in `Riconosciuti|…` (XMP lr:HierarchicalSubject), separate da quelle date a mano.

### 6b. Ricaricamento su Google Photos (opzionale)
Dall'archivio finale, per utente, verso un account scelto.

| Metadato | Sopravvive? | Come |
|---|---|---|
| Data/ora, GPS | sì | Google li legge dall'EXIF/QuickTime **incorporato** (non dagli `.xmp` separati) |
| Descrizione | sì | campo `description` della Library API |
| Album | sì | creati via Library API (album creati dall'app) |
| Persone/volti | no | Google rifà il riconoscimento; i nomi vanno riassegnati a mano |
| Preferiti, archivio | no | nessuna API |

- Metodo: Library API (upload consentito anche dopo le restrizioni 2025). Richiede progetto Google Cloud + OAuth.
- Prima dell'upload: export con metadati **incorporati** nei file (exiftool), anche nei video.
- Vincolo spazio: originali a piena risoluzione superano i 15 GB gratuiti → serve piano Google One
  sull'account di destinazione. Distribuire su più account gratuiti è ciò che ha creato il disordine attuale.

## Visualizzazione (dopo)
L'archivio sul NAS è il riferimento; sopra si può usare Immich (server su NAS, ML su laptop),
QNAP QuMagie o digiKam. Nessuno è indispensabile.
