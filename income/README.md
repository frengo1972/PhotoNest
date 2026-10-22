# income/ — file da smistare

Qui finisce **tutto il materiale grezzo**. Il tool lo legge e basta: non modifica, non rinomina, non cancella.

## Convenzione cartelle (obbligatoria)

Il percorso dice al tool **di chi è** il materiale e **da dove viene**:

```
income/<utente>/<tipo>-<identificativo>/...
```

| `<tipo>` | Contenuto | Esempio |
|---|---|---|
| `google` | zip Takeout di un account Google (identificativo = email) | `income/utente/google-nome@example.com/takeout-20260101T101500Z-001.zip` |
| `android` | copia di un telefono Android | `income/utente/android-pixel7/DCIM/Camera/...` |
| `iphone` | copia di un iPhone | `income/persona/iphone-13/DCIM/...` |
| `icloud` | download da iCloud Foto | `income/persona/icloud-nome@example.com/...` |
| `altro` | chiavette, vecchi hard disk, WhatsApp backup... | `income/utente/altro-hd-vecchio-2015/...` |

Regole:

- `<utente>` minuscolo, senza spazi: `utente`, e un nome per ciascuna figlia.
  L'utente è **la persona a cui appartiene l'archivio finale**, non chi ha fatto la foto.
- Un account Google = una cartella `google-<email>`. Se lo stesso account viene esportato due volte,
  mettere gli zip in sottocartelle con data: `google-x@gmail.com/2026-10-21/`.
- **Zip Takeout: NON estrarli.** Il tool li legge direttamente (evita problemi di percorsi lunghi su Windows).
- Telefoni: copiare le cartelle così come sono, mantenendo la struttura originale.

## Stato fonti

Tenere aggiornata questa tabella man mano che si esporta.

| Utente | Fonte | Stato | Note |
|---|---|---|---|
| utente | google-nome@example.com | test (solo album "Gita al mare 2019-10-09") | export completo da fare |
| utente | google-nome@example.com | da esportare | |
| | | | |
