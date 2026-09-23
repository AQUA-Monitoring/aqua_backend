from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from urllib.request import urlopen

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


LAYER_URL = (
    "https://geo.joinville.sc.gov.br/server/rest/services/SEPUR/"
    "administracao_e_governo_simgeo_v4/MapServer/3"
)
SOURCE_TITLE = "Abrangência das Unidades Regionais de Obras"


def _download_json(url: str) -> dict:
    try:
        with urlopen(url, timeout=60) as response:  # noqa: S310 - official fixed URL
            return json.load(response)
    except Exception as exc:
        raise CommandError(f"Não foi possível consultar a camada oficial do SIMGeo: {exc}") from exc


class Command(BaseCommand):
    help = "Importa a camada regional oficial de Joinville publicada pelo SIMGeo."

    def add_arguments(self, parser):
        parser.add_argument(
            "--geojson-path",
            help="Arquivo previamente baixado; quando omitido, consulta a camada oficial.",
        )
        parser.add_argument("--source-version")
        parser.add_argument("--published-at")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        source_version = options.get("source_version")
        if not source_version:
            metadata = _download_json(f"{LAYER_URL}?f=pjson")
            source_version = str(
                (metadata.get("editingInfo") or {}).get("lastEditDate")
                or metadata.get("currentVersion")
                or "simgeo-current"
            )

        path_option = options.get("geojson_path")
        with TemporaryDirectory(prefix="aqua-simgeo-regions-") as directory:
            if path_option:
                path = Path(path_option).expanduser().resolve()
                if not path.is_file():
                    raise CommandError("Arquivo GeoJSON informado não foi encontrado.")
            else:
                query = urlencode(
                    {
                        "where": "1=1",
                        "outFields": "objectid,sb,dt_upd,last_edited_date",
                        "returnGeometry": "true",
                        "outSR": "4326",
                        "f": "geojson",
                    }
                )
                payload = _download_json(f"{LAYER_URL}/query?{query}")
                if payload.get("type") != "FeatureCollection" or not payload.get("features"):
                    raise CommandError("O SIMGeo não retornou polígonos regionais.")
                path = Path(directory) / "joinville-regioes-oficiais.geojson"
                path.write_text(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )

            call_command(
                "import_addressing_dataset",
                str(path),
                city_code="4209102",
                kind="region_boundary",
                authority="Prefeitura de Joinville - SIMGeo",
                title=SOURCE_TITLE,
                source_url=LAYER_URL,
                license_name="Dados públicos municipais - SIMGeo",
                license_url="https://www.joinville.sc.gov.br/servicos/acessar-sistema-de-informacoes-municipais-georreferenciadas-simgeo/",
                source_version=source_version,
                published_at=options.get("published_at"),
                crs="EPSG:4326",
                id_prop="objectid",
                official_code_prop="objectid",
                name_prop="sb",
                preserve_name_case=True,
                dry_run=options["dry_run"],
                stdout=self.stdout,
                stderr=self.stderr,
            )
