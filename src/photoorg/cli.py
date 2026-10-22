"""Command line. Without a command it opens the app: photoorg [--income DIR] [--work DIR] [--tz ZONE] [command]"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from photoorg import catalog, dedup
from photoorg.ingest import ingest
from photoorg.normalize import normalize
from photoorg.report import report

COMMANDS = {
    "app": "apre l'app (comando predefinito)",
    "scan": "ingest + normalize + dedup + report",
    "ingest": "legge le fonti in income/ e aggiorna il catalogo (incrementale)",
    "normalize": "ricalcola data, fuso, GPS, descrizione, coppie Live/Motion",
    "dedup": "raggruppa le copie, sceglie la migliore, unisce i metadati (applica le decisioni di revisione)",
    "report": "riepilogo del catalogo",
    "shortcut": "crea sul desktop l'icona 'Archivio foto' che apre l'app",
    "dev": "app in modalità sviluppo: riavvio del server a ogni modifica del codice, pagina ricaricata da sola",
}


def _ps_quote(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def create_shortcut(income: Path, work: Path) -> str:
    """Desktop shortcut running pythonw (no console window) on this environment."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    icon = Path(__file__).parent / "web" / "icon.ico"
    arguments = f'-m photoorg --income "{income.resolve()}" --work "{work.resolve()}" app'
    script = (
        "$d = [Environment]::GetFolderPath('Desktop'); $p = Join-Path $d 'Archivio foto.lnk'; "
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($p); "
        f"$s.TargetPath = {_ps_quote(pythonw)}; $s.Arguments = {_ps_quote(arguments)}; "
        f"$s.WorkingDirectory = {_ps_quote(Path.cwd())}; $s.IconLocation = {_ps_quote(icon)}; "
        "$s.Description = 'Archivio foto di famiglia'; $s.Save(); $p"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="photoorg", description="Consolidamento archivi foto")
    p.add_argument("--income", type=Path, default=Path("income"), help="cartella fonti (default: income)")
    p.add_argument("--work", type=Path, default=Path("work"), help="cartella di lavoro (default: work)")
    p.add_argument("--tz", default="Europe/Rome", help="fuso orario se assente nei metadati (default: Europe/Rome)")
    sub = p.add_subparsers(dest="cmd")
    parsers = {name: sub.add_parser(name, help=help_) for name, help_ in COMMANDS.items()}
    for name in ("app", "dev"):
        parsers[name].add_argument("--port", type=int, default=8765)
        parsers[name].add_argument("--no-window", action="store_true", help="avvia solo il server, senza finestra")
    parsers["app"].add_argument("--strict-port", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    cmd = args.cmd or "app"

    if sys.stdout is None or sys.stderr is None:  # pythonw (desktop shortcut): no console
        args.work.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(args.work / "app.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if cmd == "app":
        from photoorg.server import run_app

        run_app(args.work, args.income, args.tz, args.port, window=not args.no_window, strict_port=args.strict_port)
        return 0
    if cmd == "dev":
        from photoorg.devserver import run_dev

        run_dev(args.work.resolve(), args.income.resolve(), args.tz, args.port, window=not args.no_window)
        return 0
    if cmd == "shortcut":
        print(f"Collegamento creato: {create_shortcut(args.income, args.work)}")
        return 0

    conn = catalog.connect(args.work)
    try:
        if cmd in ("scan", "ingest"):
            ingest(conn, args.income)
        if cmd in ("scan", "normalize"):
            normalize(conn, args.tz)
        if cmd in ("scan", "dedup"):
            dedup.build(conn)
        if cmd in ("scan", "report"):
            report(conn)
    finally:
        conn.close()
    return 0
