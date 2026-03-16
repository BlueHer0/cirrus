"""
CLI standalone para sat-scrapper-core.

Uso:
    sat-scrapper download --cer mi.cer --key mi.key --year 2025 --month-start 1 --month-end 6
    sat-scrapper verify-fiel --cer mi.cer --key mi.key
    sat-scrapper stats --dir ./downloads
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

from .fiel import FIELLoader, FIELError
from .config import ScrapeConfig
from .utils import setup_logging


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Logging detallado")
def cli(verbose):
    """🧾 sat-scrapper-core — Descarga masiva de CFDIs del SAT."""
    setup_logging(verbose=verbose)


@cli.command()
@click.option("--cer", required=True, type=click.Path(exists=True), help="Ruta al .cer")
@click.option("--key", required=True, type=click.Path(exists=True), help="Ruta al .key")
@click.option("--password", required=True, prompt=True, hide_input=True, help="Contraseña FIEL")
@click.option("--year", type=int, default=2025, help="Año a descargar")
@click.option("--month-start", type=int, default=1, help="Mes inicio (1-12)")
@click.option("--month-end", type=int, default=12, help="Mes fin (1-12)")
@click.option("--tipos", default="recibidos,emitidos", help="Tipos separados por coma")
@click.option("--engine", type=click.Choice(["rpa"]), default="rpa", hidden=True, help="Motor de descarga")
@click.option("--headed", is_flag=True, help="Mostrar navegador (no headless)")
@click.option("--slow-mo", type=int, default=300, help="Ms entre acciones RPA")
@click.option("--output-dir", type=click.Path(), default="./downloads", help="Directorio de salida")
def download(cer, key, password, year, month_start, month_end, tipos, engine, headed, slow_mo, output_dir):
    """📥 Descargar CFDIs del SAT."""
    asyncio.run(_download(cer, key, password, year, month_start, month_end, tipos, engine, headed, slow_mo, output_dir))


async def _download(cer, key, password, year, month_start, month_end, tipos, engine, headed, slow_mo, output_dir):
    from .engine import SATEngine

    click.echo()
    click.echo("╔══════════════════════════════════════════════════════╗")
    click.echo("║  🧾 sat-scrapper-core — Descarga Masiva de CFDIs    ║")
    click.echo("╚══════════════════════════════════════════════════════╝")
    click.echo()

    config = ScrapeConfig(
        cer_path=cer,
        key_path=key,
        password=password,
        year=year,
        month_start=month_start,
        month_end=month_end,
        tipos=[t.strip() for t in tipos.split(",")],
        engine=engine,
        headless=not headed,
        slow_mo=slow_mo,
        download_dir=output_dir,
        on_progress=lambda msg: click.echo(f"  {msg}"),
    )

    click.echo(f"📋 Configuración:")
    click.echo(f"  · RFC: Se obtendrá de la FIEL")
    click.echo(f"  · Año: {year}")
    click.echo(f"  · Meses: {month_start} → {month_end}")
    click.echo(f"  · Tipos: {', '.join(config.tipos)}")
    click.echo(f"  · Motor: {engine}")
    click.echo(f"  · Salida: {output_dir}")
    click.echo()

    try:
        async with SATEngine(config) as engine_instance:
            result = await engine_instance.download_all()

        click.echo()
        click.echo("═══════════════════════════════════════════")
        click.echo(f"  📊 Total CFDIs: {result.total_cfdis}")
        click.echo(f"  📁 Archivos: {result.total_files}")
        click.echo(f"  💾 Tamaño: {result.total_size_mb} MB")
        click.echo(f"  📅 Meses procesados: {result.months_processed}")
        click.echo(f"  📅 Meses con datos: {result.months_with_data}")
        click.echo(f"  ⚙️ Motor usado: {result.engine_used}")
        if result.errors:
            click.echo(f"  ⚠️ Errores: {len(result.errors)}")
        click.echo("═══════════════════════════════════════════")
        click.echo("\n✅ ¡Proceso completado!")

    except FIELError as e:
        click.echo(f"\n❌ Error FIEL: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        sys.exit(1)


@cli.command("verify-fiel")
@click.option("--cer", required=True, type=click.Path(exists=True), help="Ruta al .cer")
@click.option("--key", required=True, type=click.Path(exists=True), help="Ruta al .key")
@click.option("--password", required=True, prompt=True, hide_input=True, help="Contraseña FIEL")
def verify_fiel(cer, key, password):
    """🔐 Verificar que la FIEL es válida."""
    click.echo("\n🔐 Verificando FIEL...")
    try:
        fiel = FIELLoader(cer, key, password)
        info = fiel.summary()
        click.echo(f"  ✅ RFC: {info['rfc']}")
        click.echo(f"  ✅ Serial: {info['serial_number']}")
        click.echo(f"  ✅ Vigente desde: {info['valid_from']}")
        click.echo(f"  ✅ Vigente hasta: {info['valid_to']}")
        click.echo(f"  ✅ Válida: {'Sí' if info['is_valid'] else 'No'}")
        click.echo("\n✅ FIEL válida y lista para usar.")
    except FIELError as e:
        click.echo(f"  ❌ Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.option("--dir", "directory", type=click.Path(exists=True), default="./downloads", help="Directorio a analizar")
@click.option("--rfc", default="", help="RFC especifico")
def stats(directory, rfc):
    """📊 Estadísticas de CFDIs descargados."""
    from .storage import CfdiStorage

    storage = CfdiStorage(base_dir=directory, rfc=rfc)
    stats_data = storage.get_stats()

    click.echo("\n📊 Estadísticas de CFDIs:")
    click.echo(f"  · RFC: {stats_data['rfc'] or '(todos)'}")
    click.echo(f"  · Total CFDIs: {stats_data['total_cfdis']}")
    click.echo(f"  · Tamaño: {stats_data['total_size_mb']} MB")
    click.echo(f"  · UUIDs conocidos: {stats_data['known_uuids']}")
    click.echo(f"  · Directorio: {stats_data['directory']}")
    click.echo(f"  · Índice: {stats_data['index_file']}")


if __name__ == "__main__":
    cli()
