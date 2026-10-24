# Copia foto da telefono Android

Obiettivo: copiare **tutto** il materiale foto/video del telefono in
`income/<utente>/android-<modello>/`, mantenendo la struttura cartelle.

## Cartelle da copiare

| Percorso sul telefono | Contenuto |
|---|---|
| `DCIM/` | Fotocamera (`DCIM/Camera`), a volte screenshot |
| `Pictures/` | Screenshot, Instagram, Telegram, app varie |
| `Movies/` | Video di alcune app |
| `Download/` | Immagini scaricate |
| `Android/media/com.whatsapp/WhatsApp/Media/` | WhatsApp (Android 11+) — foto e video ricevuti/inviati |
| `WhatsApp/Media/` | WhatsApp su telefoni vecchi |

Nota: WhatsApp rimuove l'EXIF; la data verrà ricavata dal nome file (`IMG-20230512-WA0003.jpg`).

## Metodo A — Esplora risorse (semplice)

1. Collega il telefono via USB, sbloccalo, scegli **"Trasferimento file"** nella notifica USB.
2. In Esplora risorse apri il telefono › Memoria interna.
3. Copia le cartelle sopra in `income/<utente>/android-<modello>/`.

Limite: la data di modifica dei file diventa quella della copia. L'EXIF interno resta intatto,
quindi va bene per le foto della fotocamera.

## Metodo B — adb (consigliato, conserva le date dei file)

Richiede `adb` (vedi [setup.md](setup.md)).

1. Sul telefono: Impostazioni › Info telefono › tocca 7 volte **"Numero build"** → Opzioni sviluppatore attive.
2. Impostazioni › Opzioni sviluppatore › attiva **"Debug USB"**.
3. Collega via USB e accetta il popup "Consenti debug USB".
4. Dal PC:

```powershell
adb devices                       # deve comparire il telefono
$dst = "income\utente\android-telefono"
adb pull -a /sdcard/DCIM      "$dst\DCIM"
adb pull -a /sdcard/Pictures  "$dst\Pictures"
adb pull -a /sdcard/Movies    "$dst\Movies"
adb pull -a /sdcard/Download  "$dst\Download"
adb pull -a /sdcard/Android/media/com.whatsapp/WhatsApp/Media "$dst\WhatsApp-Media"
```

5. A fine copia, disattiva il Debug USB.

## Foto rimaste solo nel cloud

Le foto che Google Foto ha "liberato" dal telefono non sono più sul dispositivo: arrivano dal Takeout.
Non serve fare nulla di speciale.
