"""Pacotes deterministas e promocao da base territorial de referencia."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
import uuid
from pathlib import Path, PurePosixPath

from django.contrib.gis.geos import GEOSGeometry
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from core.addressing.models import (
    AddressReference,
    City,
    GeodataDataset,
    Neighborhood,
    ReferenceBaseRelease,
    ReferenceBaseReleaseAudit,
    ReferenceBaseReleaseRecord,
    Region,
    Street,
    StreetNeighborhood,
)

FORMAT = "aqua-reference-base"
SCHEMA_VERSION = 1
PARTITIONS = ("cities", "datasets", "regions", "neighborhoods", "streets", "street_neighborhoods", "address_references")
REFERENCE_ID_NAMESPACE = uuid.UUID("57b4923a-d920-5f1f-a2cf-26f4132ce87b")
MINIMUM_MIGRATION = "addressing.0026"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_NDJSON_LINE_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = len(PARTITIONS) + 2
MAX_RECORDS_PER_PARTITION = 5_000_000
MAX_TOTAL_RECORDS = 15_000_000
REFERENCE_POINTER_LOCK_ID = 0x41515541524546
REFERENCE_IMPORT_LOCK_NAMESPACE = 0x41515541
CITY_AUTHORITY_METADATA_KEY = "_aqua_reference_identity_authority"


def _advisory_xact_lock(lock_id):
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])


def lock_active_reference_release():
    """Serializa a leitura/troca do ponteiro ativo ate o commit atual."""
    _advisory_xact_lock(REFERENCE_POINTER_LOCK_ID)
    return ReferenceBaseRelease.objects.select_for_update().filter(
        status=ReferenceBaseRelease.Status.ACTIVE
    ).first()


def _lock_reference_import(revision):
    digest = hashlib.sha256(str(revision).encode()).digest()
    key = int.from_bytes(digest[:4], "big", signed=False)
    _advisory_xact_lock((REFERENCE_IMPORT_LOCK_NAMESPACE << 32) | key)


def _geometry(value):
    return json.loads(value.geojson) if value else None


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identity_component(value):
    return " ".join(str(value or "").strip().casefold().split())


def _canonical_uuid(identity_key):
    return str(uuid.uuid5(REFERENCE_ID_NAMESPACE, identity_key))


def canonicalize_reference_ids(payload):
    """Remapeia IDs locais para identidades UUIDv5 estaveis e auditaveis."""
    payload = json.loads(json.dumps(payload))
    dataset_by_id = {item["id"]: item for item in payload.get("datasets", [])}
    id_maps = {name: {} for name in ("cities", "regions", "neighborhoods", "streets", "address_references")}
    identity_keys = {}
    canonical_semantics = {}

    def register(partition, record, identity_key):
        legacy_id = str(record.get("legacy_source_id") or record.get("id") or "").strip()
        if not legacy_id:
            raise CommandError(f"Registro sem id em {partition}.")
        previous = identity_keys.get((partition, identity_key))
        if previous and previous != legacy_id:
            raise CommandError(f"Colisao de identity_key em {partition}: {identity_key}.")
        identity_keys[(partition, identity_key)] = legacy_id
        canonical_id = _canonical_uuid(identity_key)
        previous_semantics = canonical_semantics.get(canonical_id)
        if previous_semantics and previous_semantics != (partition, identity_key):
            raise CommandError(f"Colisao UUID com semantica distinta: {canonical_id}.")
        canonical_semantics[canonical_id] = (partition, identity_key)
        id_maps[partition][str(record["id"])] = canonical_id
        record["legacy_source_id"] = legacy_id
        record["identity_key"] = identity_key
        record["id"] = canonical_id

    for record in payload.get("cities", []):
        geometry_dataset = dataset_by_id.get(record.get("geometry_dataset_id"))
        authority = _identity_component(record.get("authority"))
        if not authority and geometry_dataset:
            authority = _identity_component(geometry_dataset.get("authority"))
        if not authority:
            metadata = record.get("geometry_metadata") or {}
            authority = _identity_component(
                metadata.get(CITY_AUTHORITY_METADATA_KEY)
                or metadata.get("authority") or metadata.get("source_authority")
            )
        # Compatibilidade com pacotes legados: o fallback e deliberadamente
        # constante e nao depende das camadas presentes na edicao.
        authority = authority or "unspecified-authority"
        record["authority"] = authority
        if record.get("identity_key"):
            identity_key = record["identity_key"]
        elif _identity_component(record.get("official_code")):
            identity_key = f"city|authority|{authority}|official_code|{_identity_component(record['official_code'])}"
        else:
            source_id = _identity_component(record.get("source_record_id"))
            if not authority or not source_id:
                raise CommandError("Cidade sem official_code ou fallback authority+source_record_id.")
            identity_key = f"city|source|{authority}|{source_id}"
        register("cities", record, identity_key)

    for dataset in payload.get("datasets", []):
        old_city_id = str(dataset.get("city_id"))
        if old_city_id not in id_maps["cities"]:
            raise CommandError("Dataset referencia cidade ausente do pacote.")
        dataset["city_id"] = id_maps["cities"][old_city_id]

    for partition, kind in (("regions", "region"), ("neighborhoods", "neighborhood"), ("streets", "street")):
        for record in payload.get(partition, []):
            old_city_id = str(record.get("city_ref_id") if partition != "streets" else record.get("city_id"))
            city_id = id_maps["cities"].get(old_city_id)
            if not city_id:
                raise CommandError(f"{partition} referencia cidade ausente do pacote.")
            if record.get("identity_key"):
                identity_key = record["identity_key"]
            elif _identity_component(record.get("official_code")):
                dataset = dataset_by_id.get(record.get("dataset_id"))
                authority = _identity_component(dataset.get("authority")) if dataset else ""
                if not authority:
                    raise CommandError(f"{partition} com official_code sem autoridade da fonte.")
                identity_key = f"{kind}|authority|{authority}|city|{city_id}|official_code|{_identity_component(record['official_code'])}"
            else:
                dataset = dataset_by_id.get(record.get("dataset_id"))
                authority = _identity_component(dataset.get("authority")) if dataset else ""
                source_id = _identity_component(record.get("source_record_id"))
                if not authority or not source_id:
                    raise CommandError(f"{partition} sem official_code ou fallback authority+source_record_id.")
                identity_key = f"{kind}|city|{city_id}|source|{authority}|{source_id}"
            register(partition, record, identity_key)
            if partition == "streets":
                record["city_id"] = city_id
            else:
                record["city_ref_id"] = city_id

    for record in payload.get("neighborhoods", []):
        if record.get("region_id"):
            try:
                record["region_id"] = id_maps["regions"][str(record["region_id"])]
            except KeyError as exc:
                raise CommandError("Bairro referencia regiao ausente do pacote.") from exc

    for record in payload.get("address_references", []):
        dataset = dataset_by_id.get(record.get("dataset_id"))
        authority = _identity_component(dataset.get("authority")) if dataset else ""
        source_id = _identity_component(record.get("source_record_id"))
        old_city_id = str(record.get("city_id"))
        city_id = id_maps["cities"].get(old_city_id)
        identity_key = record.get("identity_key") or (
            f"address|authority|{authority}|city|{city_id}|source|{source_id}"
            if authority and city_id and source_id else ""
        )
        if not identity_key:
            raise CommandError("Endereco sem fallback authority+source_record_id.")
        register("address_references", record, identity_key)
        try:
            record["city_id"] = id_maps["cities"][old_city_id]
        except KeyError as exc:
            raise CommandError("Endereco referencia cidade ausente do pacote.") from exc
        if record.get("neighborhood_id"):
            try:
                record["neighborhood_id"] = id_maps["neighborhoods"][str(record["neighborhood_id"])]
            except KeyError as exc:
                raise CommandError("Endereco referencia bairro ausente do pacote.") from exc
        if record.get("street_id"):
            try:
                record["street_id"] = id_maps["streets"][str(record["street_id"])]
            except KeyError as exc:
                raise CommandError("Endereco referencia rua ausente do pacote.") from exc

    for link in payload.get("street_neighborhoods", []):
        try:
            link["street_id"] = id_maps["streets"][str(link["street_id"])]
            link["neighborhood_id"] = id_maps["neighborhoods"][str(link["neighborhood_id"])]
        except KeyError as exc:
            raise CommandError("Vinculo rua-bairro referencia registro ausente do pacote.") from exc
    return payload


def _records_from_database():
    active_datasets = GeodataDataset.objects.filter(status=GeodataDataset.Status.ACTIVE)
    dataset_ids = set(active_datasets.values_list("id", flat=True))
    cities = City.objects.filter(is_active=True).order_by("id")
    payload = {
        "cities": [{
            "id": str(x.id), "name": x.name, "official_code": x.official_code,
            "normalized_name": x.normalized_name, "is_active": x.is_active,
            "geometry": _geometry(x.geometry), "geometry_metadata": x.geometry_metadata,
            "source_record_id": x.source_record_id,
            "authority": (
                (x.geometry_metadata or {}).get(CITY_AUTHORITY_METADATA_KEY)
                or (x.geometry_dataset.authority if x.geometry_dataset_id else "")
                or (x.geometry_metadata or {}).get("authority", "")
            ),
            "geometry_dataset_id": str(x.geometry_dataset_id) if x.geometry_dataset_id in dataset_ids else None,
        } for x in cities.select_related("geometry_dataset")],
        "datasets": [{
            "id": str(x.id), "city_id": str(x.city_id), "kind": x.kind,
            "authority": x.authority, "title": x.title, "source_url": x.source_url,
            "license_name": x.license_name, "license_url": x.license_url,
            "source_version": x.source_version,
            "published_at": x.published_at.isoformat() if x.published_at else None,
            "retrieved_at": x.retrieved_at.isoformat(), "sha256": x.sha256,
            "source_crs": x.source_crs, "metadata": x.metadata,
        } for x in active_datasets.order_by("id")],
        "regions": [_territory_record(x) for x in Region.objects.filter(is_active=True, dataset_id__in=dataset_ids).order_by("id")],
        "neighborhoods": [_territory_record(x, area=True) for x in Neighborhood.objects.filter(is_active=True, dataset_id__in=dataset_ids).order_by("id")],
        "streets": [{
            "id": str(x.id), "city_id": str(x.city_id), "dataset_id": str(x.dataset_id),
            "source_record_id": x.source_record_id, "official_code": x.official_code,
            "source_name": x.source_name,
            "name": x.name or x.source_name or f"Sem nome ({x.source_record_id or x.id})",
            "normalized_name": x.normalized_name,
            "street_type": x.street_type, "zipcode_from": x.zipcode_from,
            "zipcode_to": x.zipcode_to, "geometry": _geometry(x.geometry),
            "properties": x.properties, "is_active": x.is_active,
        } for x in Street.objects.filter(is_active=True, dataset_id__in=dataset_ids).order_by("id")],
        "street_neighborhoods": [{"street_id": str(x.street_id), "neighborhood_id": str(x.neighborhood_id)}
            for x in StreetNeighborhood.objects.filter(street__dataset_id__in=dataset_ids).order_by("street_id", "neighborhood_id")],
        "address_references": [{
            "id": str(x.id), "city_id": str(x.city_id),
            "neighborhood_id": str(x.neighborhood_id) if x.neighborhood_id else None,
            "street_id": str(x.street_id) if x.street_id else None,
            "dataset_id": str(x.dataset_id), "source_record_id": x.source_record_id,
            "street_name": x.street_name, "number": x.number, "modifier": x.modifier,
            "address_type": x.address_type, "species": x.species, "zipcode": x.zipcode,
            "complement": x.complement, "location": _geometry(x.location),
            "properties": x.properties, "is_active": x.is_active,
        } for x in AddressReference.objects.filter(is_active=True, dataset_id__in=dataset_ids).order_by("id")],
    }
    return payload


def _territory_record(x, area=False):
    value = {
        "id": str(x.id), "name": x.name, "official_code": x.official_code,
        "normalized_name": x.normalized_name, "city": x.city,
        "city_ref_id": str(x.city_ref_id) if x.city_ref_id else None,
        "region_id": str(x.region_id) if hasattr(x, "region_id") and x.region_id else None,
        "props": x.props, "geometry": _geometry(x.geometry),
        "geometry_metadata": x.geometry_metadata,
        "dataset_id": str(x.dataset_id) if x.dataset_id else None,
        "source_record_id": x.source_record_id, "is_active": x.is_active,
    }
    if area:
        value["area_km2"] = x.area_km2
    else:
        value.pop("region_id")
    return value


def build_payload(release=None):
    if not release:
        return _records_from_database()
    return {
        name: list(
            release.records.filter(partition=name)
            .order_by("ordinal")
            .values_list("record", flat=True)
        )
        for name in PARTITIONS
    }


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_metadata(payload, partition_bytes):
    datasets = payload.get("datasets", [])
    dataset_counts = {item["id"]: 0 for item in datasets}
    for item in payload.get("cities", []):
        if item.get("geometry_dataset_id") in dataset_counts:
            dataset_counts[item["geometry_dataset_id"]] += 1
    for partition in ("regions", "neighborhoods", "streets", "address_references"):
        for item in payload.get(partition, []):
            if item.get("dataset_id") in dataset_counts:
                dataset_counts[item["dataset_id"]] += 1
    partition_digests = {
        name: hashlib.sha256(partition_bytes[f"data/{name}.ndjson"]).hexdigest()
        for name in PARTITIONS
    }
    return _manifest_metadata_from_stats(
        payload.get("cities", []), datasets, dataset_counts, partition_digests
    )


def _manifest_metadata_from_stats(cities, datasets, dataset_counts, partition_digests):
    kinds = sorted({item["kind"] for item in datasets})
    layers = []
    partition_for_kind = {
        GeodataDataset.Kind.REGION: "regions",
        GeodataDataset.Kind.NEIGHBORHOOD: "neighborhoods",
        GeodataDataset.Kind.STREET: "streets",
        GeodataDataset.Kind.ADDRESS: "address_references",
        GeodataDataset.Kind.CITY: "cities",
    }
    for kind in kinds:
        related = [item for item in datasets if item["kind"] == kind]
        partition = partition_for_kind[kind]
        layers.append({
            "kind": kind,
            "city_ids": sorted({item["city_id"] for item in related}),
            "dataset_ids": sorted(item["id"] for item in related),
            "count": sum(dataset_counts[item["id"]] for item in related),
            "checksum": partition_digests[partition],
        })
    retrieved = sorted(item.get("retrieved_at") for item in datasets if item.get("retrieved_at"))
    return {
        "created_at": retrieved[-1] if retrieved else "1970-01-01T00:00:00+00:00",
        "minimum_migration": "addressing.0026",
        "cities": [{
            "reference_city_id": item["id"], "official_code": item.get("official_code", ""),
            "name": item["name"],
        } for item in sorted(cities, key=lambda value: value["id"])],
        "layers": layers,
        "datasets": [{
            "reference_dataset_id": item["id"], "reference_city_id": item["city_id"],
            "kind": item["kind"], "authority": item["authority"], "title": item["title"],
            "source_version": item["source_version"], "source_url": item["source_url"],
            "license": {"name": item["license_name"], "url": item.get("license_url", "")},
            "count": dataset_counts[item["id"]], "checksum": item["sha256"],
        } for item in sorted(datasets, key=lambda value: value["id"])],
    }


def write_archive(path, *, revision, payload, expected_previous_revision=None):
    path = Path(path)
    payload = canonicalize_reference_ids(payload)
    partition_counts = {name: len(payload.get(name, [])) for name in PARTITIONS}
    if any(count > MAX_RECORDS_PER_PARTITION for count in partition_counts.values()):
        raise CommandError("Particao excede o limite de registros.")
    if sum(partition_counts.values()) > MAX_TOTAL_RECORDS:
        raise CommandError("Pacote excede o limite total de registros.")
    partition_bytes = {}
    for name in PARTITIONS:
        records = sorted(payload.get(name, []), key=_canonical)
        lines = []
        for item in records:
            line = (_canonical(item) + "\n").encode()
            if len(line) > MAX_NDJSON_LINE_BYTES:
                raise CommandError(f"Linha NDJSON excede o limite em {name}.")
            lines.append(line)
        partition_bytes[f"data/{name}.ndjson"] = b"".join(lines)
    manifest = {
        "format": FORMAT, "schema_version": SCHEMA_VERSION, "revision": revision,
        "expected_previous_revision": expected_previous_revision,
        "partitions": {name: {"path": f"data/{name}.ndjson", "count": len(payload.get(name, [])), "schema": f"{name}.v1", "checksum": hashlib.sha256(partition_bytes[f"data/{name}.ndjson"]).hexdigest()} for name in PARTITIONS},
        **_manifest_metadata(payload, partition_bytes),
    }
    contents = {"manifest.json": (_canonical(manifest) + "\n").encode(), **partition_bytes}
    if any(len(value) > MAX_MEMBER_BYTES for value in contents.values()):
        raise CommandError("Particao excede o limite de tamanho descompactado.")
    if sum(len(value) for value in contents.values()) > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise CommandError("Pacote excede o limite total descompactado.")
    sums = "".join(f"{hashlib.sha256(contents[name]).hexdigest()}  {name}\n" for name in sorted(contents))
    contents["SHA256SUMS"] = sums.encode()
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name in sorted(contents):
                    info = tarfile.TarInfo(name)
                    info.size = len(contents[name]); info.mtime = 0; info.uid = 0; info.gid = 0
                    info.uname = ""; info.gname = ""; info.mode = 0o644
                    archive.addfile(info, io.BytesIO(contents[name]))
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        path.unlink(missing_ok=True)
        raise CommandError("Pacote comprimido excede o limite permitido.")
    return manifest, _sha256_file(path)


def _safe_members(archive):
    members = {}
    total_size = 0
    archive_members = archive.getmembers()
    if len(archive_members) > MAX_ARCHIVE_MEMBERS:
        raise CommandError("Pacote excede o limite de membros.")
    for member in archive_members:
        member_path = PurePosixPath(member.name)
        if member_path.is_absolute() or ".." in member_path.parts or not member.isfile():
            raise CommandError("Pacote rejeitado: membro inseguro ou nao regular.")
        if member.name in members:
            raise CommandError("Pacote rejeitado: membro duplicado.")
        if member.size > MAX_MEMBER_BYTES:
            raise CommandError(f"Membro excede o limite: {member.name}.")
        total_size += member.size
        if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise CommandError("Pacote excede o limite total descompactado.")
        members[member.name] = member
    return members


def _iter_ndjson(source, partition):
    while True:
        line = source.readline(MAX_NDJSON_LINE_BYTES + 1)
        if not line:
            break
        if len(line) > MAX_NDJSON_LINE_BYTES:
            raise CommandError(f"Linha NDJSON excede o limite em {partition}.")
        try:
            record = json.loads(line) if line.strip() else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CommandError(f"NDJSON invalido em {partition}.") from exc
        yield line, record


def _read_small_member(archive, member, *, limit=2 * 1024 * 1024):
    if member.size > limit:
        raise CommandError(f"Membro de controle excede o limite: {member.name}.")
    source = archive.extractfile(member)
    return source.read() if source else b""


def _parse_checksums(value):
    checksums = {}
    try:
        for line in value.decode().splitlines():
            digest, name = line.split("  ", 1)
            if name in checksums or len(digest) != 64:
                raise ValueError
            checksums[name] = digest
    except (UnicodeDecodeError, ValueError) as exc:
        raise CommandError("SHA256SUMS invalido.") from exc
    return checksums


def _stream_member_digest(source):
    digest = hashlib.sha256()
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _require_fields(record, partition, fields):
    missing = [field for field in fields if record.get(field) in (None, "")]
    if missing:
        raise CommandError(f"Campos obrigatorios ausentes em {partition}: {', '.join(missing)}.")


def _validate_uuid(value, field, partition):
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise CommandError(f"UUID invalido em {partition}.{field}.") from exc


def _validate_geometry(value, expected_type, partition, *, required=False):
    if value in (None, ""):
        if required:
            raise CommandError(f"Geometria obrigatoria ausente em {partition}.")
        return
    try:
        geometry = GEOSGeometry(json.dumps(value), srid=4326)
    except (TypeError, ValueError) as exc:
        raise CommandError(f"Geometria invalida em {partition}.") from exc
    if geometry.geom_type != expected_type or not geometry.valid or geometry.empty:
        raise CommandError(f"Geometria deve ser {expected_type} valida em {partition}.")
    if expected_type == "Point" and not (-180 <= geometry.x <= 180 and -90 <= geometry.y <= 90):
        raise CommandError("Coordenadas de endereco fora de EPSG:4326.")


def _validate_record(record, partition):
    if not isinstance(record, dict):
        raise CommandError(f"Registro deve ser objeto JSON em {partition}.")
    if partition == "street_neighborhoods":
        _require_fields(record, partition, ("street_id", "neighborhood_id"))
        _validate_uuid(record["street_id"], "street_id", partition)
        _validate_uuid(record["neighborhood_id"], "neighborhood_id", partition)
        return
    _require_fields(record, partition, ("id",))
    _validate_uuid(record["id"], "id", partition)
    if partition == "datasets":
        _require_fields(record, partition, (
            "city_id", "kind", "authority", "title", "source_url", "license_name",
            "source_version", "retrieved_at", "sha256", "source_crs",
        ))
        _validate_uuid(record["city_id"], "city_id", partition)
        if record["kind"] not in GeodataDataset.Kind.values:
            raise CommandError("Kind de dataset invalido.")
        if len(record["sha256"]) != 64:
            raise CommandError("Checksum de proveniencia invalido em datasets.")
        try:
            int(record["sha256"], 16)
        except (TypeError, ValueError) as exc:
            raise CommandError("Checksum de proveniencia invalido em datasets.") from exc
        if parse_datetime(record["retrieved_at"]) is None:
            raise CommandError("retrieved_at invalido em datasets.")
        return
    _require_fields(record, partition, ("identity_key", "legacy_source_id"))
    if _canonical_uuid(record["identity_key"]) != str(record["id"]):
        raise CommandError(f"UUID nao corresponde a identity_key em {partition}.")
    if partition == "cities":
        _require_fields(record, partition, ("name",))
        _validate_geometry(record.get("geometry"), "MultiPolygon", partition)
        if record.get("geometry_dataset_id"):
            _validate_uuid(record["geometry_dataset_id"], "geometry_dataset_id", partition)
    elif partition in {"regions", "neighborhoods"}:
        _require_fields(record, partition, ("name", "city_ref_id", "dataset_id", "source_record_id"))
        _validate_uuid(record["city_ref_id"], "city_ref_id", partition)
        _validate_uuid(record["dataset_id"], "dataset_id", partition)
        if record.get("region_id"):
            _validate_uuid(record["region_id"], "region_id", partition)
        _validate_geometry(record.get("geometry"), "MultiPolygon", partition)
    elif partition == "streets":
        _require_fields(record, partition, ("name", "city_id", "dataset_id", "source_record_id"))
        _validate_uuid(record["city_id"], "city_id", partition)
        _validate_uuid(record["dataset_id"], "dataset_id", partition)
        _validate_geometry(record.get("geometry"), "MultiLineString", partition)
    elif partition == "address_references":
        _require_fields(record, partition, ("city_id", "dataset_id", "source_record_id", "street_name", "location"))
        for field in ("city_id", "dataset_id", "neighborhood_id", "street_id"):
            if record.get(field):
                _validate_uuid(record[field], field, partition)
        _validate_geometry(record.get("location"), "Point", partition, required=True)


def _validate_cross_references(path, id_sets, dataset_kinds, dataset_cities, entity_cities):
    for partition in PARTITIONS:
        for record in iter_archive_records(path, partition):
            if partition == "cities":
                if record.get("geometry_dataset_id") and record["geometry_dataset_id"] not in id_sets["datasets"]:
                    raise CommandError("Cidade referencia dataset ausente.")
            elif partition == "datasets":
                if record["city_id"] not in id_sets["cities"]:
                    raise CommandError("Dataset referencia cidade ausente.")
            elif partition in {"regions", "neighborhoods"}:
                if record["city_ref_id"] not in id_sets["cities"] or record["dataset_id"] not in id_sets["datasets"]:
                    raise CommandError(f"FK cruzada invalida em {partition}.")
                expected_kind = GeodataDataset.Kind.REGION if partition == "regions" else GeodataDataset.Kind.NEIGHBORHOOD
                if dataset_kinds[record["dataset_id"]] != expected_kind:
                    raise CommandError(f"Dataset de kind incompativel em {partition}.")
                if dataset_cities[record["dataset_id"]] != record["city_ref_id"]:
                    raise CommandError(f"Dataset de outra cidade em {partition}.")
                if record.get("region_id") and record["region_id"] not in id_sets["regions"]:
                    raise CommandError("Bairro referencia regiao ausente.")
                if record.get("region_id") and entity_cities["regions"][record["region_id"]] != record["city_ref_id"]:
                    raise CommandError("Bairro referencia regiao de outra cidade.")
            elif partition == "streets":
                if record["city_id"] not in id_sets["cities"] or record["dataset_id"] not in id_sets["datasets"]:
                    raise CommandError("FK cruzada invalida em streets.")
                if dataset_kinds[record["dataset_id"]] != GeodataDataset.Kind.STREET:
                    raise CommandError("Dataset de kind incompativel em streets.")
                if dataset_cities[record["dataset_id"]] != record["city_id"]:
                    raise CommandError("Dataset de outra cidade em streets.")
            elif partition == "street_neighborhoods":
                if record["street_id"] not in id_sets["streets"] or record["neighborhood_id"] not in id_sets["neighborhoods"]:
                    raise CommandError("FK cruzada invalida em street_neighborhoods.")
                if entity_cities["streets"][record["street_id"]] != entity_cities["neighborhoods"][record["neighborhood_id"]]:
                    raise CommandError("Vinculo rua-bairro cruza cidades.")
            elif partition == "address_references":
                if record["city_id"] not in id_sets["cities"] or record["dataset_id"] not in id_sets["datasets"]:
                    raise CommandError("FK cruzada invalida em address_references.")
                if dataset_kinds[record["dataset_id"]] != GeodataDataset.Kind.ADDRESS:
                    raise CommandError("Dataset de kind incompativel em address_references.")
                if dataset_cities[record["dataset_id"]] != record["city_id"]:
                    raise CommandError("Dataset de outra cidade em address_references.")
                if record.get("neighborhood_id") and record["neighborhood_id"] not in id_sets["neighborhoods"]:
                    raise CommandError("Endereco referencia bairro ausente.")
                if record.get("neighborhood_id") and entity_cities["neighborhoods"][record["neighborhood_id"]] != record["city_id"]:
                    raise CommandError("Endereco referencia bairro de outra cidade.")
                if record.get("street_id") and record["street_id"] not in id_sets["streets"]:
                    raise CommandError("Endereco referencia rua ausente.")
                if record.get("street_id") and entity_cities["streets"][record["street_id"]] != record["city_id"]:
                    raise CommandError("Endereco referencia rua de outra cidade.")


def iter_archive_records(path, partition):
    if partition not in PARTITIONS:
        raise CommandError(f"Particao desconhecida: {partition}.")
    try:
        with tarfile.open(Path(path), mode="r:gz") as archive:
            members = _safe_members(archive)
            source = archive.extractfile(members[f"data/{partition}.ndjson"])
            for _line, record in _iter_ndjson(source, partition):
                if record is not None:
                    yield record
    except (OSError, tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
        raise CommandError(f"NDJSON invalido em {partition}.") from exc


def read_archive(path, *, load_payload=True):
    path = Path(path)
    try:
        if path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise CommandError("Pacote comprimido excede o limite permitido.")
    except OSError as exc:
        raise CommandError(f"Pacote indisponivel: {exc}") from exc
    payload = {name: [] for name in PARTITIONS} if load_payload else {}
    id_sets = {name: set() for name in PARTITIONS if name != "street_neighborhoods"}
    dataset_kinds = {}
    dataset_cities = {}
    entity_cities = {"regions": {}, "neighborhoods": {}, "streets": {}}
    canonical_semantics = {}
    dataset_counts = {}
    control_cities = []
    control_datasets = []
    partition_digests = {}
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            members = _safe_members(archive)
            required_names = {"manifest.json", "SHA256SUMS", *(f"data/{name}.ndjson" for name in PARTITIONS)}
            if set(members) != required_names:
                raise CommandError("Pacote contem membros ausentes ou nao declarados.")
            checksums = _parse_checksums(_read_small_member(archive, members["SHA256SUMS"]))
            if set(checksums) != required_names - {"SHA256SUMS"}:
                raise CommandError("SHA256SUMS nao corresponde aos membros do pacote.")
            manifest_bytes = _read_small_member(archive, members["manifest.json"])
            if hashlib.sha256(manifest_bytes).hexdigest() != checksums["manifest.json"]:
                raise CommandError("Checksum invalido para manifest.json.")
            manifest = json.loads(manifest_bytes)
            if manifest.get("format") != FORMAT or manifest.get("schema_version") != SCHEMA_VERSION:
                raise CommandError("Schema do pacote incompativel.")
            if "expected_previous_revision" not in manifest or (
                manifest["expected_previous_revision"] is not None
                and not isinstance(manifest["expected_previous_revision"], str)
            ):
                raise CommandError("expected_previous_revision ausente ou invalida.")
            if manifest.get("minimum_migration") != MINIMUM_MIGRATION:
                raise CommandError("minimum_migration incompativel.")
            if set(manifest.get("partitions", {})) != set(PARTITIONS):
                raise CommandError("Particoes do pacote incompativeis.")
            declared_counts = [manifest["partitions"][name].get("count") for name in PARTITIONS]
            if any(not isinstance(count, int) or count < 0 or count > MAX_RECORDS_PER_PARTITION for count in declared_counts):
                raise CommandError("Contagem declarada excede o limite por particao.")
            if sum(declared_counts) > MAX_TOTAL_RECORDS:
                raise CommandError("Contagem declarada excede o limite total.")
            total_records = 0
            for name in PARTITIONS:
                spec = manifest["partitions"][name]
                expected_path = f"data/{name}.ndjson"
                if spec.get("path") != expected_path or spec.get("schema") != f"{name}.v1":
                    raise CommandError(f"Schema da particao {name} incompativel.")
                source = archive.extractfile(members[expected_path])
                digest = hashlib.sha256()
                count = 0
                keys = set()
                for line, record in _iter_ndjson(source, name):
                    digest.update(line)
                    if record is None:
                        continue
                    _validate_record(record, name)
                    key = ((record.get("street_id"), record.get("neighborhood_id")) if name == "street_neighborhoods" else record.get("id"))
                    if not key or key in keys:
                        raise CommandError(f"Identificador ausente ou duplicado em {name}.")
                    keys.add(key); count += 1
                    total_records += 1
                    if count > MAX_RECORDS_PER_PARTITION or total_records > MAX_TOTAL_RECORDS:
                        raise CommandError("Pacote excede o limite de registros.")
                    if name != "street_neighborhoods":
                        id_sets[name].add(record["id"])
                    if record.get("identity_key"):
                        semantics = (name, record["identity_key"])
                        previous_semantics = canonical_semantics.get(record["id"])
                        if previous_semantics and previous_semantics != semantics:
                            raise CommandError("UUID canonico colide com semantica distinta.")
                        canonical_semantics[record["id"]] = semantics
                    if name == "cities":
                        control_cities.append(record)
                        if record.get("geometry_dataset_id"):
                            dataset_counts[record["geometry_dataset_id"]] = dataset_counts.get(record["geometry_dataset_id"], 0) + 1
                    elif name == "datasets":
                        control_datasets.append(record)
                        dataset_kinds[record["id"]] = record["kind"]
                        dataset_cities[record["id"]] = record["city_id"]
                        dataset_counts.setdefault(record["id"], 0)
                    elif name in {"regions", "neighborhoods", "streets", "address_references"}:
                        dataset_id = record["dataset_id"]
                        dataset_counts[dataset_id] = dataset_counts.get(dataset_id, 0) + 1
                        if name in entity_cities:
                            entity_cities[name][record["id"]] = record.get("city_ref_id") or record.get("city_id")
                    if load_payload:
                        payload[name].append(record)
                actual_digest = digest.hexdigest()
                partition_digests[name] = actual_digest
                if actual_digest != checksums[expected_path] or actual_digest != spec.get("checksum"):
                    raise CommandError(f"Checksum invalido para {expected_path}.")
                if count != spec.get("count"):
                    raise CommandError(f"Contagem divergente em {name}.")
    except CommandError:
        raise
    except (OSError, tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommandError(f"Pacote .tar.gz invalido: {exc}") from exc
    required_metadata = {"created_at", "minimum_migration", "cities", "layers", "datasets"}
    if not required_metadata.issubset(manifest):
        raise CommandError("Manifesto sem metadados minimos da base de referencia.")
    _validate_cross_references(
        path, id_sets, dataset_kinds, dataset_cities, entity_cities
    )
    expected_metadata = _manifest_metadata_from_stats(
        control_cities, control_datasets, dataset_counts, partition_digests
    )
    for field in ("created_at", "minimum_migration", "cities", "layers", "datasets"):
        if manifest.get(field) != expected_metadata[field]:
            raise CommandError(f"Metadado derivado divergente no manifesto: {field}.")
    return manifest, payload, _sha256_file(path)


def validate_collisions(manifest, archive_sha):
    revision = str(manifest.get("revision", "")).strip()
    if not revision or len(revision) > 160:
        raise CommandError("revision ausente ou invalida.")
    existing = ReferenceBaseRelease.objects.filter(revision=revision).first()
    if existing and existing.archive_sha256 != archive_sha:
        raise CommandError("Colisao: a revision ja existe com outro checksum.")
    by_sha = ReferenceBaseRelease.objects.filter(archive_sha256=archive_sha).first()
    if by_sha and by_sha.revision != revision:
        raise CommandError("Colisao: o pacote ja existe com outra revision.")
    return existing


def import_release(path, *, dry_run=False):
    manifest, _payload, archive_sha = read_archive(path, load_payload=False)
    if dry_run:
        validate_collisions(manifest, archive_sha)
        return None, manifest, archive_sha
    try:
        with transaction.atomic():
            _lock_reference_import(manifest["revision"])
            existing = validate_collisions(manifest, archive_sha)
            if existing:
                return existing, manifest, archive_sha
            release = ReferenceBaseRelease.objects.create(
                revision=manifest["revision"], status=ReferenceBaseRelease.Status.VALIDATED,
                schema_version=manifest["schema_version"], archive_sha256=archive_sha,
                manifest=manifest, validated_at=timezone.now(),
            )
            for partition in PARTITIONS:
                batch = []
                for ordinal, record in enumerate(iter_archive_records(path, partition), start=1):
                    batch.append(ReferenceBaseReleaseRecord(
                        release=release, partition=partition, ordinal=ordinal, record=record,
                    ))
                    if len(batch) >= 1000:
                        ReferenceBaseReleaseRecord.objects.bulk_create(batch, batch_size=1000)
                        batch.clear()
                if batch:
                    ReferenceBaseReleaseRecord.objects.bulk_create(batch, batch_size=1000)
            ReferenceBaseReleaseAudit.objects.create(
                release=release, action=ReferenceBaseReleaseAudit.Action.IMPORT,
                to_revision=release.revision, justification="Pacote validado e importado para staging.",
                metadata={"archive_sha256": archive_sha},
            )
            return release, manifest, archive_sha
    except IntegrityError as exc:
        equivalent = ReferenceBaseRelease.objects.filter(
            revision=manifest["revision"], archive_sha256=archive_sha
        ).first()
        if equivalent:
            return equivalent, manifest, archive_sha
        raise CommandError("Colisao concorrente sem equivalencia semantica.") from exc


def active_revision():
    release = ReferenceBaseRelease.objects.filter(status=ReferenceBaseRelease.Status.ACTIVE).only("revision").first()
    return release.revision if release else None


def promote_release(revision, *, expected_revision, justification, actor, rollback=False):
    if not str(justification or "").strip():
        raise CommandError("--justification e obrigatoria.")
    actor_model = ReferenceBaseReleaseAudit._meta.get_field("actor").remote_field.model
    if not isinstance(actor, actor_model) or not getattr(actor, "pk", None):
        raise CommandError("Promocao e rollback exigem administrador ativo verificavel.")
    try:
        with transaction.atomic():
            try:
                persisted_actor = actor_model._default_manager.select_for_update().get(pk=actor.pk)
            except actor_model.DoesNotExist as exc:
                raise CommandError(
                    "Promocao e rollback exigem administrador ativo verificavel."
                ) from exc
            is_project_admin = getattr(persisted_actor, "type", None) == "admin"
            if not getattr(persisted_actor, "is_active", False) or not (
                getattr(persisted_actor, "is_staff", False)
                or getattr(persisted_actor, "is_superuser", False)
                or is_project_admin
            ):
                raise CommandError("Promocao e rollback exigem administrador ativo verificavel.")
            current = lock_active_reference_release()
            current_revision = current.revision if current else None
            normalized_expected = None if expected_revision in (None, "", "none", "null") else expected_revision
            if current_revision != normalized_expected:
                raise CommandError(f"Revision ativa divergente: esperada {normalized_expected!r}, atual {current_revision!r}.")
            target = ReferenceBaseRelease.objects.select_for_update().get(revision=revision)
            manifest_expected = (
                current.manifest.get("expected_previous_revision")
                if rollback and current else target.manifest.get("expected_previous_revision")
            )
            lineage_actual = target.revision if rollback else current_revision
            if manifest_expected != lineage_actual:
                raise CommandError(
                    "Linhagem do pacote divergente: "
                    f"manifesto espera {manifest_expected!r}, revisao relacionada {lineage_actual!r}."
                )
            allowed = {ReferenceBaseRelease.Status.VALIDATED, ReferenceBaseRelease.Status.STAGED}
            if rollback:
                allowed.add(ReferenceBaseRelease.Status.SUPERSEDED)
            if target.status not in allowed:
                raise CommandError(f"Release {revision} nao pode ser promovida a partir de {target.status}.")
            target.status = ReferenceBaseRelease.Status.PROMOTING
            target.save(update_fields=["status", "updated_at"])
            _apply_release(target, deactivate_with=current)
            now = timezone.now()
            if current and current.pk != target.pk:
                current.status = ReferenceBaseRelease.Status.SUPERSEDED
                current.superseded_at = now
                current.save(update_fields=["status", "superseded_at", "updated_at"])
            target.status = ReferenceBaseRelease.Status.ACTIVE
            target.promoted_at = now; target.superseded_at = None; target.failure_detail = ""
            first_dataset = target.records.filter(partition="datasets").order_by("ordinal").values_list("record__id", flat=True).first()
            target.dataset_id = first_dataset
            target.save(update_fields=["status", "promoted_at", "superseded_at", "failure_detail", "dataset", "updated_at"])
            ReferenceBaseReleaseAudit.objects.create(
                release=target,
                action=ReferenceBaseReleaseAudit.Action.ROLLBACK if rollback else ReferenceBaseReleaseAudit.Action.PROMOTE,
                from_revision=current_revision or "", to_revision=target.revision,
                expected_revision=expected_revision or "", justification=justification,
                actor=persisted_actor,
            )
            return target
    except ReferenceBaseRelease.DoesNotExist as exc:
        raise CommandError(f"Release {revision} nao encontrada.") from exc


def _geom(value):
    return GEOSGeometry(json.dumps(value), srid=4326) if value else None


def _rekey_legacy_instance(model, record):
    canonical_id = record["id"]
    legacy_id = record.get("legacy_source_id")
    if not legacy_id or str(legacy_id) == str(canonical_id):
        return
    if model.objects.filter(pk=canonical_id).exists():
        if model.objects.filter(pk=legacy_id).exists():
            raise CommandError(f"IDs legado e canonico coexistem para {model.__name__}.")
        return
    if not model.objects.filter(pk=legacy_id).exists():
        return
    for relation in model._meta.related_objects:
        field = relation.field
        if not getattr(field, "attname", None):
            continue
        relation.related_model._base_manager.filter(
            **{field.attname: legacy_id}
        ).update(**{field.attname: canonical_id})
    model.objects.filter(pk=legacy_id).update(
        **{model._meta.pk.attname: canonical_id}
    )


def _release_records(release, partition):
    return release.records.filter(partition=partition).order_by("ordinal").values_list("record", flat=True).iterator(chunk_size=1000)


def _apply_release(release, *, deactivate_with=None):
    datasets = list(_release_records(release, "datasets"))
    target_city_ids = {
        item["id"] for item in _release_records(release, "cities")
    }
    # A primeira promocao converte PKs locais para UUIDv5 antes de calcular o
    # escopo; assim os datasets legados relacionados participam da supersessao.
    for city_record in _release_records(release, "cities"):
        _rekey_legacy_instance(City, city_record)
    scoped_datasets = list(datasets)
    if deactivate_with and deactivate_with.pk != release.pk:
        scoped_datasets.extend(_release_records(deactivate_with, "datasets"))
        previous_city_ids = {
            item["id"] for item in _release_records(deactivate_with, "cities")
        }
        City.objects.filter(id__in=previous_city_ids - target_city_ids).update(
            is_active=False
        )
    scope_by_kind = {}
    for item in scoped_datasets:
        scope_by_kind.setdefault(item["kind"], set()).add(item["city_id"])
    if scope_by_kind.get(GeodataDataset.Kind.REGION):
        Region.objects.filter(city_ref_id__in=scope_by_kind[GeodataDataset.Kind.REGION]).update(is_active=False)
    if scope_by_kind.get(GeodataDataset.Kind.NEIGHBORHOOD):
        Neighborhood.objects.filter(city_ref_id__in=scope_by_kind[GeodataDataset.Kind.NEIGHBORHOOD]).update(is_active=False)
    if scope_by_kind.get(GeodataDataset.Kind.STREET):
        Street.objects.filter(city_id__in=scope_by_kind[GeodataDataset.Kind.STREET]).update(is_active=False)
    if scope_by_kind.get(GeodataDataset.Kind.ADDRESS):
        AddressReference.objects.filter(city_id__in=scope_by_kind[GeodataDataset.Kind.ADDRESS]).update(is_active=False)
    dataset_scope = Q()
    for kind, city_ids in scope_by_kind.items():
        dataset_scope |= Q(kind=kind, city_id__in=city_ids)
    if dataset_scope:
        GeodataDataset.objects.filter(
            dataset_scope, status=GeodataDataset.Status.ACTIVE,
        ).update(status=GeodataDataset.Status.SUPERSEDED)
    for x in _release_records(release, "cities"):
        _rekey_legacy_instance(City, x)
        geometry_metadata = dict(x.get("geometry_metadata") or {})
        geometry_metadata[CITY_AUTHORITY_METADATA_KEY] = x.get(
            "authority", "unspecified-authority"
        )
        City.objects.update_or_create(id=x["id"], defaults={
            "name": x["name"], "official_code": x.get("official_code", ""), "normalized_name": x.get("normalized_name", ""),
            "is_active": True, "geometry": _geom(x.get("geometry")), "geometry_metadata": geometry_metadata,
            "source_record_id": x.get("source_record_id", ""), "geometry_dataset": None,
        })
    for x in datasets:
        values = dict(x); values.pop("id"); values["published_at"] = parse_date(values["published_at"]) if values.get("published_at") else None
        values["retrieved_at"] = parse_datetime(values["retrieved_at"]); values["status"] = GeodataDataset.Status.ACTIVE
        GeodataDataset.objects.update_or_create(id=x["id"], defaults=values)
    for x in _release_records(release, "cities"):
        if x.get("geometry_dataset_id"):
            City.objects.filter(id=x["id"]).update(geometry_dataset_id=x["geometry_dataset_id"])
    for model, name in ((Region, "regions"), (Neighborhood, "neighborhoods")):
        for x in _release_records(release, name):
            _rekey_legacy_instance(model, x)
            values = dict(x); values.pop("id"); values.pop("identity_key", None); values.pop("legacy_source_id", None); values["geometry"] = _geom(values.get("geometry")); values["is_active"] = True
            model.objects.update_or_create(id=x["id"], defaults=values)
    street_ids = []
    for x in _release_records(release, "streets"):
        _rekey_legacy_instance(Street, x)
        street_ids.append(x["id"])
        values = dict(x); values.pop("id"); values.pop("identity_key", None); values.pop("legacy_source_id", None); values["geometry"] = _geom(values.get("geometry")); values["is_active"] = True
        Street.objects.update_or_create(id=x["id"], defaults=values)
    StreetNeighborhood.objects.filter(street_id__in=street_ids).delete()
    batch = []
    for x in _release_records(release, "street_neighborhoods"):
        batch.append(StreetNeighborhood(**x))
        if len(batch) >= 1000:
            StreetNeighborhood.objects.bulk_create(batch, ignore_conflicts=True, batch_size=1000)
            batch.clear()
    if batch:
        StreetNeighborhood.objects.bulk_create(batch, ignore_conflicts=True, batch_size=1000)
    for x in _release_records(release, "address_references"):
        _rekey_legacy_instance(AddressReference, x)
        values = dict(x); values.pop("id"); values.pop("identity_key", None); values.pop("legacy_source_id", None); values["location"] = _geom(values["location"]); values["is_active"] = True
        AddressReference.objects.update_or_create(id=x["id"], defaults=values)
