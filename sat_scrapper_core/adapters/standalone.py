"""
Adaptador standalone para scripts simples.

Uso:
    python -m sat_scrapper_core.adapters.standalone \\
        --cer path/to/mi.cer \\
        --key path/to/mi.key \\
        --password "xxx" \\
        --year 2025
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


def run_download(
    cer_path: str,
    key_path: str,
    password: str,
    year: int = 2025,
    month_start: int = 1,
    month_end: int = 12,
    tipos: list[str] | None = None,
    engine: str = "auto",
    headless: bool = True,
    download_dir: str = "./downloads",
):
    """
    Función de conveniencia para ejecutar una descarga completa (bloqueante).

    Ideal para scripts o notebooks:
        from sat_scrapper_core.adapters.standalone import run_download
        result = run_download("mi.cer", "mi.key", "pass", year=2025)
    """
    from ..config import ScrapeConfig
    from ..engine import SATEngine

    config = ScrapeConfig(
        cer_path=cer_path,
        key_path=key_path,
        password=password,
        year=year,
        month_start=month_start,
        month_end=month_end,
        tipos=tipos or ["recibidos", "emitidos"],
        engine=engine,
        headless=headless,
        download_dir=download_dir,
        on_progress=lambda msg: print(f"  {msg}"),
    )

    async def _run():
        async with SATEngine(config) as eng:
            return await eng.download_all()

    return asyncio.run(_run())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SAT CFDI Downloader (Standalone)")
    parser.add_argument("--cer", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--month-start", type=int, default=1)
    parser.add_argument("--month-end", type=int, default=12)
    parser.add_argument("--engine", default="rpa", choices=["rpa"])
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--output-dir", default="./downloads")

    args = parser.parse_args()

    result = run_download(
        cer_path=args.cer,
        key_path=args.key,
        password=args.password,
        year=args.year,
        month_start=args.month_start,
        month_end=args.month_end,
        engine=args.engine,
        headless=not args.headed,
        download_dir=args.output_dir,
    )

    print(f"\n✅ Resultado: {result.summary()}")
