# Setup PC (Windows 11)

Hardware di riferimento: Intel Core Ultra 7 165H, NVIDIA RTX 2000 Ada Laptop 8 GB.
NAS: QNAP TS-453A (solo storage: CPU troppo debole per l'elaborazione AI).

## Già presenti

- Python 3.14 (`C:\Python314`) — **non** usato per il progetto, vedi sotto
- `uv` (gestore ambienti Python)
- `git`

## Da installare

Da PowerShell:

```powershell
winget install OliverBetz.ExifTool     # lettura/scrittura metadati EXIF/XMP
winget install Gyan.FFmpeg             # video: metadati, keyframe per dedup
winget install Google.PlatformTools    # adb, per copia da Android (opzionale)
```

Chiudere e riaprire il terminale, poi verificare:

```powershell
exiftool -ver
ffmpeg -version
adb version
```

## Ambiente Python

Il progetto userà **Python 3.12** tramite `uv`: le librerie AI (onnxruntime-gpu, insightface, torch)
non hanno ancora pacchetti affidabili per 3.14. `uv` scarica 3.12 da solo, senza toccare quello di sistema.

```powershell
uv python install 3.12
```

L'ambiente del progetto verrà creato quando inizia il codice (`uv sync`).

## GPU

Verificare driver NVIDIA aggiornati:

```powershell
nvidia-smi
```

Deve mostrare la RTX 2000 Ada e una versione CUDA ≥ 12.

## Modelli AI (fase tagging, più avanti)

| Uso | Modello | VRAM |
|---|---|---|
| Ricerca/tag semantici | CLIP / SigLIP | ~1–2 GB |
| Volti | InsightFace (buffalo_l) | ~1 GB |
| Didascalie (opzionale) | un modello con visione (es. Qwen) in LM Studio o llama.cpp | ~5–6 GB |

Didascalie: avvia il server del modello (LM Studio o llama.cpp), poi nell'app apri **Didascalie** e inserisci
indirizzo e, se il server la richiede, la chiave API.
