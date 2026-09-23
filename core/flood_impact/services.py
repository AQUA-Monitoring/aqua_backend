from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict
import hashlib

from django.contrib.gis.geos import LineString, MultiLineString
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.addressing.models import GeodataDataset, Region, RoadAxisSegment, Street
from core.flood_impact.models import (
    FloodSpatialEventRevision,
    RoadFloodImpact,
    RoadFloodImpactRun,
    RoadImpactHotspot,
)


def _line_parts(geometry):
    if geometry.empty:
        return []
    if geometry.geom_type == "LineString":
        return [geometry]
    if geometry.geom_type == "MultiLineString":
        return list(geometry)
    if geometry.geom_type == "GeometryCollection":
        parts = []
        for child in geometry:
            parts.extend(_line_parts(child))
        return parts
    return []


def _length_m(geometry) -> float:
    projected = geometry.clone()
    projected.transform(31982)
    return float(projected.length)


@dataclass(frozen=True)
class ImpactCalculationResult:
    run: RoadFloodImpactRun
    reused: bool


def affected_territory_snapshot(*, city, location=None, footprint=None):
    """Retorna a cobertura territorial disponível para a geometria informada.

    A função é deliberadamente local: não geocodifica nem infere vias quando a
    base oficial não possui geometria. O resultado é um snapshot serializável,
    ordenado e sem dados de métricas que possam ficar obsoletos entre revisões.
    """
    geometry = footprint if footprint is not None else location
    if geometry is None:
        return {"affected_regions": [], "affected_streets": []}

    regions = Region.objects.filter(
        city_ref=city, is_active=True, geometry__intersects=geometry,
    ).order_by("name", "id")
    streets = Street.objects.filter(city=city, is_active=True).filter(
        Q(geometry__intersects=geometry)
        | Q(axis_segments__is_active=True, axis_segments__geometry__intersects=geometry)
    ).distinct().order_by("name", "id")
    return {
        "affected_regions": [{"id": str(region.id), "name": region.name} for region in regions],
        "affected_streets": [{"id": str(street.id), "name": street.name} for street in streets],
    }


class PostGISRoadImpactService:
    """Idempotently derives road impacts from an explicit polygon footprint."""

    algorithm_version = "road-intersection-v1"

    def calculate(
        self, *, revision: FloodSpatialEventRevision, road_dataset: GeodataDataset,
        reference_base_release=None, reason: str = "", requested_by=None,
    ) -> ImpactCalculationResult:
        input_hash = hashlib.sha256(
            b"|".join([
                bytes(revision.footprint.ewkb) if revision.footprint else b"no-footprint",
                str(revision.id).encode(), road_dataset.sha256.encode(), self.algorithm_version.encode(),
                str(getattr(reference_base_release, "id", "")).encode(),
            ])
        ).hexdigest()
        completed = RoadFloodImpactRun.objects.filter(
            revision=revision,
            road_dataset=road_dataset,
            algorithm_version=self.algorithm_version,
            input_hash=input_hash,
            status=RoadFloodImpactRun.Status.COMPLETED,
        ).order_by("-finished_at").first()
        if completed is not None:
            return ImpactCalculationResult(run=completed, reused=True)

        run = RoadFloodImpactRun.objects.create(
            revision=revision, road_dataset=road_dataset,
            algorithm_version=self.algorithm_version, input_hash=input_hash,
            status=RoadFloodImpactRun.Status.QUEUED, started_at=timezone.now(),
            reference_base_release=reference_base_release, reason=reason,
            requested_by=requested_by,
        )
        try:
            self._execute(run=run, revision=revision, road_dataset=road_dataset)
            run.refresh_from_db()
        except Exception as exc:  # noqa: BLE001 - a execução precisa deixar evidência persistida
            run.refresh_from_db()
            run.status = RoadFloodImpactRun.Status.FAILED
            run.finished_at = timezone.now()
            run.report = {
                "reason": "calculation_failed",
                "error_type": type(exc).__name__,
            }
            run.save(update_fields=["status", "finished_at", "report", "updated_at"])
        return ImpactCalculationResult(run=run, reused=False)

    @transaction.atomic
    def _execute(self, *, run, revision, road_dataset):
        run = RoadFloodImpactRun.objects.select_for_update().get(pk=run.pk)

        run.impacts.all().delete()
        run.hotspots.all().delete()
        run.status = RoadFloodImpactRun.Status.RUNNING
        run.started_at = timezone.now()
        run.finished_at = None
        run.report = {}
        run.save(update_fields=[
            "status", "started_at", "finished_at", "report", "updated_at",
        ])

        if revision.footprint is None:
            run.status = RoadFloodImpactRun.Status.COMPLETED
            run.finished_at = timezone.now()
            run.report = {"candidate_segments": 0, "impacted_segments": 0, "touches": 0, "reason": "footprint_required"}
            run.save(update_fields=["status", "finished_at", "report", "updated_at"])
            return

        candidates = RoadAxisSegment.objects.filter(
            dataset=road_dataset,
            city=revision.event.city,
            is_active=True,
            geometry__intersects=revision.footprint,
        ).select_related("street")
        impacts = []
        touches = 0
        total_length = 0.0
        for segment in candidates.iterator():
            intersection = segment.geometry.intersection(revision.footprint)
            parts = _line_parts(intersection)
            if not parts:
                touches += 1
                continue
            lines = [LineString(part.coords, srid=4326) for part in parts if len(part.coords) >= 2]
            if not lines:
                touches += 1
                continue
            linear = MultiLineString(*lines, srid=4326)
            length_m = _length_m(linear)
            segment_length_m = _length_m(segment.geometry)
            if length_m <= 0 or segment_length_m <= 0:
                continue
            total_length += length_m
            impacts.append(RoadFloodImpact(
                run=run,
                segment=segment,
                intersection=linear,
                length_m=length_m,
                segment_fraction=min(1.0, length_m / segment_length_m),
                relation=(RoadFloodImpact.Relation.WITHIN if revision.footprint.covers(segment.geometry) else RoadFloodImpact.Relation.CROSSES),
                evidence_kind=revision.event.evidence_kind,
                evidence_status=revision.status,
            ))
        RoadFloodImpact.objects.bulk_create(impacts, batch_size=1000)
        aggregates = defaultdict(lambda: {"length": 0.0, "segments": set(), "geometries": [], "label": ""})
        for impact in impacts:
            centroid = impact.intersection.centroid
            keys = [(RoadImpactHotspot.Dimension.ROAD_SEGMENT, str(impact.segment_id), impact.segment.street.name if impact.segment.street_id else "")]
            if impact.segment.street_id:
                keys.append((RoadImpactHotspot.Dimension.STREET, str(impact.segment.street_id), impact.segment.street.name))
            grid_key = f"{round(centroid.x, 3):.3f},{round(centroid.y, 3):.3f}"
            keys.append((RoadImpactHotspot.Dimension.GRID_CELL, grid_key, grid_key))
            for link in impact.segment.neighborhood_links.select_related("neighborhood").all():
                keys.append((RoadImpactHotspot.Dimension.NEIGHBORHOOD, str(link.neighborhood_id), link.neighborhood.name))
            for dimension, key, label in keys:
                value = aggregates[(dimension, key)]
                value["length"] += impact.length_m
                value["segments"].add(impact.segment_id)
                value["geometries"].append(impact.intersection)
                value["label"] = label
        hotspots = []
        for rank, ((dimension, key), value) in enumerate(sorted(aggregates.items(), key=lambda item: -item[1]["length"]), 1):
            merged = value["geometries"][0]
            for geometry in value["geometries"][1:]:
                merged = merged.union(geometry)
            hotspots.append(RoadImpactHotspot(
                run=run, location=merged.centroid, impacted_length_m=value["length"],
                segment_count=len(value["segments"]), rank=rank, dimension=dimension,
                dimension_key=key, label=value["label"],
            ))
        RoadImpactHotspot.objects.bulk_create(hotspots, batch_size=1000)
        run.status = RoadFloodImpactRun.Status.COMPLETED
        run.finished_at = timezone.now()
        run.report = {
            "candidate_segments": len(impacts) + touches,
            "impacted_segments": len(impacts),
            "touches": touches,
            "impacted_length_m": total_length,
        }
        run.save(update_fields=["status", "finished_at", "report", "updated_at"])
        return
