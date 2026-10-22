# Copia foto da iPhone (e iCloud)

Destinazione: `income/<utente>/iphone-<modello>/` (e `income/<utente>/icloud-<appleid>/` se serve).

## 1. Impostazione fondamentale

Sull'iPhone: **Impostazioni › Foto › Trasferisci su Mac o PC › "Mantieni originali"**.

Senza questa opzione Windows riceve JPEG convertiti al volo: si perdono qualità e parte dei metadati,
e le Live Photo vengono spezzate male.

## 2. iCloud Foto è attivo?

Impostazioni › [nome] › iCloud › Foto.

- **Disattivo**: tutte le foto sono sul telefono → vai al punto 3.
- **Attivo con "Ottimizza spazio iPhone"**: sul telefono ci sono solo anteprime, gli originali sono in iCloud.
  Due opzioni:
  - temporaneamente scegli **"Scarica e mantieni originali"** e attendi che scarichi (serve spazio libero), poi punto 3;
  - oppure scarica da iCloud per Windows (punto 4).
- **Attivo con "Scarica e mantieni originali"**: punto 3.

## 3. Copia via cavo

1. Installa **Apple Devices** (o iTunes) dal Microsoft Store: fornisce i driver.
2. Collega l'iPhone, sbloccalo, tocca **"Autorizza"**.
3. Esplora risorse › Apple iPhone › Internal Storage › `DCIM`.
4. Copia **tutta** la cartella `DCIM` in `income/<utente>/iphone-<modello>/DCIM/`.
   - Copiare le cartelle `100APPLE`, `101APPLE`... una alla volta se la copia si blocca (capita con molti file).

I file saranno `.HEIC` (foto), `.MOV` (video e parte video delle Live Photo), `.PNG` (screenshot), `.AAE` (modifiche).
Copiare tutto, anche gli `.AAE`.

## 4. Download da iCloud (se necessario)

1. Installa **iCloud per Windows** dal Microsoft Store, accedi con l'Apple ID.
2. Attiva **Foto** e, nelle opzioni, **"Scarica originali ad alta efficienza (HEVC)"** se disponibile.
3. In Esplora risorse › iCloud Foto: seleziona tutto › tasto destro › **"Mantieni sempre su questo dispositivo"**.
4. A download completato, copia in `income/<utente>/icloud-<appleid>/`.

Alternativa per pochi file: [icloud.com/photos](https://www.icloud.com/photos) › seleziona › scarica (scegli "Originale non modificato").

## WhatsApp su iPhone

Le foto WhatsApp salvate nel rullino sono già in `DCIM`. Quelle non salvate restano dentro l'app:
si recuperano solo da backup WhatsApp, fuori dallo scopo per ora.
