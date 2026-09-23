import json
import uuid
from collections import defaultdict

from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, Polygon
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from core.addressing.models import City, GeodataDataset, ReferenceBaseRelease
from core.flood_camera_monitoring.infra.models import Camera
from core.flood_camera_monitoring.services.nearby import camera_coordinates, haversine_distance_m
from core.flood_impact.models import (
    FloodImpactAdministrativeAction,
    FloodSpatialEvent,
    FloodSpatialEventRevision,
    RoadFloodImpact,
    RoadFloodImpactRun,
    RoadImpactHotspot,
)
from core.flood_impact.services import PostGISRoadImpactService, affected_territory_snapshot
from core.users.permissions import IsAppAdmin


def _geometry(raw, expected):
    if raw is None:
        return None
    value = GEOSGeometry(json.dumps(raw) if isinstance(raw, dict) else raw, srid=4326)
    if value.geom_type == "Polygon" and expected == "MultiPolygon":
        value = MultiPolygon(value, srid=4326)
    if value.geom_type != expected:
        raise ValueError(f"geometry must be {expected}")
    if not value.valid:
        raise ValueError("geometry must be valid")
    return value


def _parse_period(query_params):
    start_raw = query_params.get("period_start") or query_params.get("from")
    end_raw = query_params.get("period_end") or query_params.get("until")
    start = parse_datetime(start_raw) if start_raw else None
    end = parse_datetime(end_raw) if end_raw else None
    if start_raw and start is None:
        raise ValueError("period_start deve ser ISO-8601.")
    if end_raw and end is None:
        raise ValueError("period_end deve ser ISO-8601.")
    if start and end and end <= start:
        raise ValueError("period_end deve ser posterior a period_start.")
    return start, end


def _overlap_filter(prefix, start, end):
    query = Q()
    if start:
        query &= Q(**{f"{prefix}valid_until__isnull": True}) | Q(**{f"{prefix}valid_until__gt": start})
    if end:
        query &= Q(**{f"{prefix}valid_from__lt": end})
    return query


class FloodImpactPagination(PageNumberPagination):
    page_size = 100
    page_size_query_param = "page_size"
    max_page_size = 500


class FloodImpactEventViewSet(viewsets.ViewSet):
    pagination_class = FloodImpactPagination
    write_actions = {"create", "revisions", "reviews", "confirmations", "activate", "revoke", "recalculate"}

    def get_permissions(self):
        permission_classes = [permissions.IsAuthenticated]
        if self.action in self.write_actions or (self.action == "impact_runs" and self.request.method == "POST"):
            permission_classes.append(IsAppAdmin)
        return [permission() for permission in permission_classes]

    def _error(self, code, detail, http_status=status.HTTP_400_BAD_REQUEST, fields=None):
        return Response({"error": {"code": code, "detail": detail, "fields": fields or {}}}, status=http_status)

    def _paginate(self, request, queryset, mapper):
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response([mapper(item) for item in page])

    def _reference_release(self, raw, *, fallback=None, validate_current=False):
        if raw in (None, ""):
            if fallback is not None:
                return fallback, None
            active = ReferenceBaseRelease.objects.filter(status=ReferenceBaseRelease.Status.ACTIVE).first()
            return active, None
        release = ReferenceBaseRelease.objects.filter(pk=raw).first()
        if release is None:
            return None, self._error("invalid_input", "reference_base_revision inválida.")
        if validate_current and fallback is not None and release.pk != fallback.pk:
            return None, self._error(
                "reference_base_changed", "A revisão da base de referência do evento mudou.",
                status.HTTP_409_CONFLICT,
            )
        active = ReferenceBaseRelease.objects.filter(status=ReferenceBaseRelease.Status.ACTIVE).first()
        if active is not None and active.pk != release.pk:
            return None, self._error(
                "reference_base_changed", "A base de referência ativa mudou.",
                status.HTTP_409_CONFLICT,
            )
        return release, None

    def _check_expected_revision(self, current, payload, *, accept_source_revision=False, required=False):
        expected = payload.get("expected_revision")
        if expected is None and accept_source_revision:
            expected = payload.get("source_revision")
        if expected is None and required:
            return self._error("invalid_input", "expected_revision é obrigatória.")
        if expected is not None and str(expected) != str(current.revision):
            return self._error(
                "revision_conflict", "A revisão atual do evento mudou.", status.HTTP_409_CONFLICT,
                {"expected_revision": expected, "current_revision": current.revision},
            )
        return None

    @staticmethod
    def _run_payload(run, reused=None):
        payload = {
            "id": str(run.id),
            "status": run.status,
            "report": run.report,
            "dataset": {
                "id": str(run.road_dataset_id),
                "title": run.road_dataset.title,
                "source_version": run.road_dataset.source_version,
                "sha256": run.road_dataset.sha256,
            },
            "reference_base_revision": str(run.reference_base_release_id) if run.reference_base_release_id else None,
            "algorithm_version": run.algorithm_version,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "reason": run.reason,
            "requested_by": str(run.requested_by_id) if run.requested_by_id else None,
        }
        if reused is not None:
            payload["reused"] = reused
        return payload

    def _event_payload(self, event, request=None):
        revision = event.current_revision
        release = revision.reference_base_release if revision else None
        run = None
        if revision:
            run = revision.impact_runs.select_related("road_dataset", "reference_base_release", "requested_by").order_by("-created_at").first()
        if run is None:
            freshness = "NOT_REQUESTED"
        elif run.status in {RoadFloodImpactRun.Status.QUEUED, RoadFloodImpactRun.Status.RUNNING}:
            freshness = "RUNNING"
        elif run.status == RoadFloodImpactRun.Status.FAILED:
            freshness = "FAILED"
        elif run.status == RoadFloodImpactRun.Status.STALE or run.reference_base_release_id != (revision.reference_base_release_id if revision else None):
            freshness = "STALE"
        else:
            freshness = "CURRENT"
        is_admin = bool(request and IsAppAdmin().has_permission(request, self))
        is_revoked = bool(revision and revision.status == FloodSpatialEventRevision.Status.REVOKED)
        can_review = bool(
            is_admin and revision
            and revision.status in {FloodSpatialEventRevision.Status.DRAFT, FloodSpatialEventRevision.Status.ACTIVE}
        )
        can_confirm = bool(
            is_admin and revision and revision.status == FloodSpatialEventRevision.Status.ACTIVE
            and event.evidence_kind in {
                FloodSpatialEvent.EvidenceKind.FORECAST,
                FloodSpatialEvent.EvidenceKind.CAMERA_OBSERVATION,
                FloodSpatialEvent.EvidenceKind.USER_REPORT,
            }
        )
        provenance = None
        if revision:
            provenance = {
                "source_type": event.source_type,
                "source_id": event.source_id,
                "source_version": revision.source_version,
                "source_revision": revision.source_revision,
                "reference_base": ({
                    "id": str(release.id), "revision": release.revision, "status": release.status,
                    "dataset_id": str(release.dataset_id) if release.dataset_id else None,
                } if release else None),
            }
        payload = {
            "id": str(event.id), "city_id": str(event.city_id),
            "city": str(event.city_id), "city_name": event.city.name,
            "neighborhoods": [], "evidence_kind": event.evidence_kind,
            "source": {"type": event.source_type, "id": event.source_id},
            "derived_from_event_id": str(event.derived_from_event_id) if event.derived_from_event_id else None,
            "reference_base_revision": str(revision.reference_base_release_id) if revision and revision.reference_base_release_id else None,
            "provenance": provenance,
            "freshness": freshness,
            "permissions": {
                "can_edit": bool(is_admin and revision and not is_revoked),
                "can_review": can_review,
                "can_confirm": can_confirm,
                "can_recalculate": bool(is_admin and revision and not is_revoked),
            },
            "current_run_summary": self._run_payload(run) if run else None,
            "revision": ({
                "id": str(revision.id), "number": revision.revision, "status": revision.status,
                "confidence": revision.confidence, "geometry_method": revision.geometry_method,
                "valid_from": revision.valid_from.isoformat(),
                "valid_until": revision.valid_until.isoformat() if revision.valid_until else None,
                "location": json.loads(revision.location.geojson) if revision.location else None,
                "footprint": json.loads(revision.footprint.geojson) if revision.footprint else None,
                "affected_regions": revision.affected_regions, "affected_streets": revision.affected_streets,
                "reference_base_revision": str(revision.reference_base_release_id) if revision.reference_base_release_id else None,
            } if revision else None),
        }
        if revision:
            payload.update({
                "status": revision.status,
                "location": json.loads(revision.location.geojson) if revision.location else None,
                "footprint": json.loads(revision.footprint.geojson) if revision.footprint else None,
                "geometry_method": revision.geometry_method, "confidence": revision.confidence,
                "valid_from": revision.valid_from.isoformat(),
                "valid_until": revision.valid_until.isoformat() if revision.valid_until else None,
                "current_revision": revision.revision, "metadata": revision.properties,
                "affected_regions": revision.affected_regions, "affected_streets": revision.affected_streets,
            })
        return payload

    def _event_queryset(self):
        return FloodSpatialEvent.objects.select_related(
            "city", "current_revision", "current_revision__reference_base_release",
        )

    def list(self, request):
        qs = self._event_queryset().order_by("-created_at")
        if request.query_params.get("city_id"):
            qs = qs.filter(city_id=request.query_params["city_id"])
        if request.query_params.get("evidence_kind"):
            value = request.query_params["evidence_kind"]
            if value not in FloodSpatialEvent.EvidenceKind.values:
                return self._error("invalid_filter", "evidence_kind inválido.")
            qs = qs.filter(evidence_kind=value)
        if request.query_params.get("status"):
            value = request.query_params["status"]
            if value not in FloodSpatialEventRevision.Status.values:
                return self._error("invalid_filter", "status inválido.")
            qs = qs.filter(current_revision__status=value)
        if request.query_params.get("valid_at"):
            instant = parse_datetime(request.query_params["valid_at"])
            if instant is None:
                return self._error("invalid_filter", "valid_at deve ser ISO-8601.")
            qs = qs.filter(current_revision__valid_from__lte=instant).filter(
                Q(current_revision__valid_until__isnull=True) | Q(current_revision__valid_until__gt=instant),
            )
        try:
            period_start, period_end = _parse_period(request.query_params)
        except ValueError as exc:
            return self._error("invalid_filter", str(exc))
        qs = qs.filter(_overlap_filter("current_revision__", period_start, period_end))
        if request.query_params.get("bbox"):
            try:
                bbox = Polygon.from_bbox(tuple(float(item) for item in request.query_params["bbox"].split(",")))
            except (TypeError, ValueError):
                return self._error("invalid_filter", "bbox deve ser west,south,east,north.")
            qs = qs.filter(Q(current_revision__footprint__intersects=bbox) | Q(current_revision__location__intersects=bbox))
        return self._paginate(request, qs, lambda event: self._event_payload(event, request))

    def retrieve(self, request, pk=None):
        event = self._event_queryset().filter(pk=pk).first()
        return Response(self._event_payload(event, request)) if event else self._error("not_found", "Evento não encontrado.", status.HTTP_404_NOT_FOUND)

    @transaction.atomic
    def create(self, request):
        try:
            city = City.objects.get(pk=request.data.get("city_id") or request.data.get("city"))
            evidence_kind = request.data["evidence_kind"]
            if evidence_kind not in FloodSpatialEvent.EvidenceKind.values:
                raise ValueError("evidence_kind invalid")
            if evidence_kind == FloodSpatialEvent.EvidenceKind.CONFIRMED_OCCURRENCE:
                return self._error(
                    "confirmation_required",
                    "Ocorrências confirmadas devem ser criadas pela rota confirmations.",
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            if evidence_kind == FloodSpatialEvent.EvidenceKind.LEGACY_UNCLASSIFIED:
                return self._error("invalid_input", "LEGACY_UNCLASSIFIED é reservado à ingestão interna.")
            source = request.data.get("source")
            if not isinstance(source, dict):
                source = {"type": str(source or "manual"), "id": str(request.data.get("source_id") or uuid.uuid4())}
            data = request.data.get("revision") or request.data
            valid_from = parse_datetime(data["valid_from"])
            if valid_from is None:
                raise ValueError("valid_from invalid")
            valid_until = parse_datetime(data.get("valid_until")) if data.get("valid_until") else None
            if valid_until and valid_until <= valid_from:
                raise ValueError("valid_until invalid")
            confidence = data.get("confidence")
            if confidence is not None and not 0 <= float(confidence) <= 1:
                raise ValueError("confidence invalid")
            location = _geometry(data.get("location"), "Point")
            footprint = _geometry(data.get("footprint"), "MultiPolygon")
            if footprint and city.geometry and not city.geometry.covers(footprint):
                raise ValueError("footprint outside city")
            method = data["geometry_method"]
            if method not in FloodSpatialEventRevision.GeometryMethod.values:
                raise ValueError("geometry_method invalid")
        except (KeyError, TypeError, ValueError, City.DoesNotExist) as exc:
            return self._error("invalid_input", str(exc))
        release, error = self._reference_release(data.get("reference_base_revision"))
        if error:
            return error
        event = FloodSpatialEvent.objects.create(
            city=city, evidence_kind=evidence_kind, source_type=str(source["type"]), source_id=str(source["id"]),
        )
        affected = affected_territory_snapshot(city=city, location=location, footprint=footprint)
        revision = FloodSpatialEventRevision.objects.create(
            event=event, revision=1, status=FloodSpatialEventRevision.Status.DRAFT,
            location=location, footprint=footprint, confidence=confidence, valid_from=valid_from, valid_until=valid_until,
            geometry_method=method, source_version=data.get("source_version", ""),
            properties=data.get("properties", data.get("metadata", {})),
            affected_regions=affected["affected_regions"], affected_streets=affected["affected_streets"],
            author=request.user, justification=str(data.get("justification") or "Criação manual da mancha operacional."),
            source_revision=str(data.get("source_revision") or "1"), reference_base_release=release,
        )
        event.current_revision = revision
        event.save(update_fields=["current_revision", "updated_at"])
        return Response(self._event_payload(event, request), status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="revisions")
    @transaction.atomic
    def revisions(self, request, pk=None):
        event = FloodSpatialEvent.objects.select_for_update().select_related("city").filter(pk=pk).first()
        if event is None or event.current_revision is None:
            return self._error("not_found", "Evento não encontrado.", status.HTTP_404_NOT_FOUND)
        current = event.current_revision
        if current.status == FloodSpatialEventRevision.Status.REVOKED:
            return self._error(
                "invalid_state", "Uma revisão revogada não pode ser editada.", status.HTTP_409_CONFLICT,
            )
        conflict = self._check_expected_revision(current, request.data, accept_source_revision=True)
        if conflict:
            return conflict
        release, error = self._reference_release(
            request.data.get("reference_base_revision"), fallback=current.reference_base_release,
        )
        if error:
            return error
        try:
            valid_from_raw = request.data.get("valid_from") or current.valid_from.isoformat()
            valid_from = parse_datetime(valid_from_raw)
            valid_until_raw = request.data.get("valid_until") or (current.valid_until.isoformat() if current.valid_until else None)
            valid_until = parse_datetime(valid_until_raw) if valid_until_raw else None
            if valid_from is None or (valid_until and valid_until <= valid_from):
                raise ValueError("valid window invalid")
            footprint = _geometry(request.data.get("footprint"), "MultiPolygon") if "footprint" in request.data else current.footprint
            location = _geometry(request.data.get("location"), "Point") if "location" in request.data else current.location
            if footprint and event.city.geometry and not event.city.geometry.covers(footprint):
                raise ValueError("footprint outside city")
            method = request.data.get("geometry_method") or current.geometry_method
            if method not in FloodSpatialEventRevision.GeometryMethod.values:
                raise ValueError("geometry_method invalid")
            justification = str(request.data["justification"])
            if not justification.strip():
                raise ValueError("justification required")
        except (KeyError, TypeError, ValueError) as exc:
            return self._error("invalid_input", str(exc))
        number = (event.revisions.order_by("-revision").values_list("revision", flat=True).first() or 0) + 1
        if current.status == FloodSpatialEventRevision.Status.ACTIVE:
            current.status = FloodSpatialEventRevision.Status.SUPERSEDED
            current.save(update_fields=["status", "updated_at"])
        affected = affected_territory_snapshot(city=event.city, location=location, footprint=footprint)
        revision = FloodSpatialEventRevision.objects.create(
            event=event, revision=number, status=FloodSpatialEventRevision.Status.DRAFT,
            location=location, footprint=footprint,
            confidence=request.data.get("confidence", current.confidence), valid_from=valid_from, valid_until=valid_until,
            geometry_method=method, source_version=request.data.get("source_version", current.source_version),
            properties=request.data.get("properties", request.data.get("metadata", current.properties)),
            affected_regions=affected["affected_regions"], affected_streets=affected["affected_streets"],
            author=request.user, justification=justification, source_revision=str(current.revision),
            reference_base_release=release,
        )
        event.current_revision = revision
        event.save(update_fields=["current_revision", "updated_at"])
        return Response(self._event_payload(event, request), status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="reviews")
    @transaction.atomic
    def reviews(self, request, pk=None):
        event = FloodSpatialEvent.objects.select_for_update().select_related("city").filter(pk=pk).first()
        if event is None or event.current_revision is None:
            return self._error("not_found", "Evento não encontrado.", status.HTTP_404_NOT_FOUND)
        revision = event.current_revision
        conflict = self._check_expected_revision(revision, request.data)
        if conflict:
            return conflict
        _, error = self._reference_release(
            request.data.get("reference_base_revision"), fallback=revision.reference_base_release, validate_current=True,
        )
        if error:
            return error
        decision = request.data.get("decision")
        justification = str(request.data.get("justification") or "").strip()
        if decision not in {"PUBLISH", "REVOKE"} or not justification:
            return self._error("invalid_input", "decision e justification são obrigatórios.")
        if decision == "PUBLISH":
            if revision.status != FloodSpatialEventRevision.Status.DRAFT:
                return self._error(
                    "invalid_state", "Somente revisões em rascunho podem ser publicadas.",
                    status.HTTP_409_CONFLICT,
                )
            if event.city.geometry is None:
                return self._error("territory_unresolved", "A cidade não possui limite territorial validado.", status.HTTP_409_CONFLICT)
            if revision.footprint is None or not event.city.geometry.covers(revision.footprint):
                return self._error("invalid_geometry", "A mancha deve estar integralmente dentro do município.", status.HTTP_422_UNPROCESSABLE_ENTITY)
            to_status = FloodSpatialEventRevision.Status.ACTIVE
            action_type = FloodImpactAdministrativeAction.Action.PUBLISH
        else:
            if revision.status != FloodSpatialEventRevision.Status.ACTIVE:
                return self._error(
                    "invalid_state", "Somente revisões ativas podem ser revogadas.",
                    status.HTTP_409_CONFLICT,
                )
            to_status = FloodSpatialEventRevision.Status.REVOKED
            action_type = FloodImpactAdministrativeAction.Action.REVOKE
        from_status = revision.status
        revision.status = to_status
        revision.save(update_fields=["status", "updated_at"])
        FloodImpactAdministrativeAction.objects.create(
            event=event, revision=revision, action=action_type, actor=request.user,
            justification=justification, from_status=from_status, to_status=to_status,
            metadata={"expected_revision": request.data.get("expected_revision")},
        )
        return Response(self._event_payload(event, request))

    def _legacy_review(self, request, pk, decision, justification):
        data = request.data.copy()
        data["decision"] = decision
        data["justification"] = data.get("justification") or justification
        request._full_data = data
        return self.reviews(request, pk)

    @action(detail=True, methods=["post"], url_path="activate")
    def activate(self, request, pk=None):
        return self._legacy_review(request, pk, "PUBLISH", "Publicação solicitada pela rota legada /activate/.")

    @action(detail=True, methods=["post"], url_path="revoke")
    def revoke(self, request, pk=None):
        return self._legacy_review(request, pk, "REVOKE", "Revogação solicitada pela rota legada /revoke/.")

    @action(detail=True, methods=["post"], url_path="confirmations")
    @transaction.atomic
    def confirmations(self, request, pk=None):
        origin = FloodSpatialEvent.objects.select_for_update().select_related("city").filter(pk=pk).first()
        if origin is None or origin.current_revision is None:
            return self._error("not_found", "Evento não encontrado.", status.HTTP_404_NOT_FOUND)
        current = origin.current_revision
        confirmable_evidence = {
            FloodSpatialEvent.EvidenceKind.FORECAST,
            FloodSpatialEvent.EvidenceKind.CAMERA_OBSERVATION,
            FloodSpatialEvent.EvidenceKind.USER_REPORT,
        }
        if current.status != FloodSpatialEventRevision.Status.ACTIVE or origin.evidence_kind not in confirmable_evidence:
            return self._error(
                "invalid_state",
                "Somente evidências elegíveis com revisão ativa podem ser confirmadas.",
                status.HTTP_409_CONFLICT,
            )
        conflict = self._check_expected_revision(current, request.data)
        if conflict:
            return conflict
        release, error = self._reference_release(
            request.data.get("reference_base_revision"), fallback=current.reference_base_release, validate_current=True,
        )
        if error:
            return error
        justification = str(request.data.get("justification") or "").strip()
        if not justification:
            return self._error("invalid_input", "justification é obrigatória.")
        try:
            valid_from = parse_datetime(request.data["valid_from"]) if request.data.get("valid_from") else current.valid_from
            valid_until = parse_datetime(request.data["valid_until"]) if request.data.get("valid_until") else current.valid_until
            if valid_from is None or (valid_until and valid_until <= valid_from):
                raise ValueError("valid window invalid")
        except (TypeError, ValueError) as exc:
            return self._error("invalid_input", str(exc))
        source_id = str(uuid.uuid4())
        confirmed = FloodSpatialEvent.objects.create(
            city=origin.city, evidence_kind=FloodSpatialEvent.EvidenceKind.CONFIRMED_OCCURRENCE,
            source_type="flood_impact_confirmation", source_id=source_id, derived_from_event=origin,
        )
        revision = FloodSpatialEventRevision.objects.create(
            event=confirmed, revision=1, status=FloodSpatialEventRevision.Status.ACTIVE,
            location=current.location, footprint=current.footprint, confidence=current.confidence,
            valid_from=valid_from, valid_until=valid_until, geometry_method=FloodSpatialEventRevision.GeometryMethod.MANUAL,
            source_version=current.source_version, properties=current.properties,
            affected_regions=current.affected_regions, affected_streets=current.affected_streets,
            author=request.user, justification=justification, source_revision=str(current.revision),
            reference_base_release=release,
        )
        confirmed.current_revision = revision
        confirmed.save(update_fields=["current_revision", "updated_at"])
        FloodImpactAdministrativeAction.objects.create(
            event=origin, revision=current, action=FloodImpactAdministrativeAction.Action.CONFIRM,
            actor=request.user, justification=justification, from_status=current.status,
            to_status=current.status, derived_event=confirmed,
            metadata={"expected_revision": request.data.get("expected_revision")},
        )
        return Response(self._event_payload(confirmed, request), status=status.HTTP_201_CREATED)

    def _impact_run_create(self, request, event):
        revision = event.current_revision
        if revision.status == FloodSpatialEventRevision.Status.REVOKED:
            return self._error(
                "invalid_state", "Uma revisão revogada não pode ser recalculada.",
                status.HTTP_409_CONFLICT,
            )
        conflict = self._check_expected_revision(revision, request.data, required=True)
        if conflict:
            return conflict
        if event.city.geometry is None:
            return self._error("territory_unresolved", "A cidade não possui limite territorial validado.", status.HTTP_409_CONFLICT)
        if revision.footprint is None or not event.city.geometry.covers(revision.footprint):
            return self._error("invalid_geometry", "A mancha deve estar integralmente dentro do município.", status.HTTP_422_UNPROCESSABLE_ENTITY)
        release, error = self._reference_release(
            request.data.get("reference_base_revision"), fallback=revision.reference_base_release,
            validate_current=bool(revision.reference_base_release_id),
        )
        if error:
            return error
        datasets = GeodataDataset.objects.filter(kind=GeodataDataset.Kind.STREET, city=event.city)
        dataset_id = request.data.get("road_dataset_id")
        dataset = datasets.filter(pk=dataset_id).first() if dataset_id else datasets.filter(status=GeodataDataset.Status.ACTIVE).order_by("-retrieved_at").first()
        if dataset is None:
            return self._error("invalid_input", "road_dataset_id deve identificar uma malha viária.")
        reason = str(request.data.get("reason") or "").strip()
        if not reason:
            return self._error("invalid_input", "reason é obrigatório.")
        result = PostGISRoadImpactService().calculate(
            revision=revision, road_dataset=dataset, reference_base_release=release,
            reason=reason, requested_by=request.user,
        )
        FloodImpactAdministrativeAction.objects.create(
            event=event, revision=revision, action=FloodImpactAdministrativeAction.Action.RECALCULATE,
            actor=request.user, justification=reason, from_status=revision.status, to_status=revision.status,
            impact_run=result.run, metadata={"reused": result.reused},
        )
        response_status = status.HTTP_200_OK if result.reused else status.HTTP_201_CREATED
        return Response(self._run_payload(result.run, result.reused), status=response_status)

    @action(detail=True, methods=["get", "post"], url_path="impact-runs")
    def impact_runs(self, request, pk=None):
        event = self._event_queryset().filter(pk=pk).first()
        if event is None or event.current_revision is None:
            return self._error("not_found", "Evento ou revisão não encontrado.", status.HTTP_404_NOT_FOUND)
        if request.method == "POST":
            return self._impact_run_create(request, event)
        qs = RoadFloodImpactRun.objects.select_related("road_dataset", "reference_base_release", "requested_by").filter(
            revision__event=event,
        ).order_by("-created_at")
        run_status = request.query_params.get("status")
        if run_status:
            if run_status not in RoadFloodImpactRun.Status.values:
                return self._error("invalid_filter", "status inválido.")
            qs = qs.filter(status=run_status)
        if request.query_params.get("road_dataset_id"):
            qs = qs.filter(road_dataset_id=request.query_params["road_dataset_id"])
        if request.query_params.get("reference_base_revision"):
            qs = qs.filter(reference_base_release_id=request.query_params["reference_base_revision"])
        return self._paginate(request, qs, self._run_payload)

    @action(detail=True, methods=["post"], url_path="recalculate")
    def recalculate(self, request, pk=None):
        data = request.data.copy()
        data["reason"] = data.get("reason") or "Recálculo solicitado pela rota legada /recalculate/."
        event = self._event_queryset().filter(pk=pk).first()
        if event is None or event.current_revision is None:
            return self._error("not_found", "Evento ou revisão não encontrado.", status.HTTP_404_NOT_FOUND)
        data["expected_revision"] = event.current_revision.revision
        request._full_data = data
        return self._impact_run_create(request, event)

    @action(detail=True, methods=["get"], url_path="history")
    def history(self, request, pk=None):
        event = FloodSpatialEvent.objects.filter(pk=pk).first()
        if event is None:
            return self._error("not_found", "Evento não encontrado.", status.HTTP_404_NOT_FOUND)
        revisions = event.revisions.select_related("reference_base_release").prefetch_related("impact_runs__road_dataset", "impact_runs__reference_base_release").order_by("revision")
        actions = event.administrative_actions.select_related("actor", "derived_event", "impact_run").order_by("created_at")
        return Response({
            "event_id": str(event.id),
            "derived_events": [str(value) for value in event.derived_events.values_list("id", flat=True)],
            "revisions": [{
                "id": str(revision.id), "number": revision.revision, "status": revision.status,
                "author_id": str(revision.author_id) if revision.author_id else None,
                "justification": revision.justification, "source_revision": revision.source_revision,
                "source_version": revision.source_version,
                "valid_from": revision.valid_from.isoformat(),
                "valid_until": revision.valid_until.isoformat() if revision.valid_until else None,
                "reference_base_revision": str(revision.reference_base_release_id) if revision.reference_base_release_id else None,
                "affected_regions": revision.affected_regions, "affected_streets": revision.affected_streets,
                "runs": [self._run_payload(run) for run in revision.impact_runs.all()],
            } for revision in revisions],
            "actions": [{
                "id": str(item.id), "action": item.action,
                "actor_id": str(item.actor_id) if item.actor_id else None,
                "justification": item.justification, "from_status": item.from_status,
                "to_status": item.to_status, "metadata": item.metadata,
                "derived_event_id": str(item.derived_event_id) if item.derived_event_id else None,
                "impact_run_id": str(item.impact_run_id) if item.impact_run_id else None,
                "created_at": item.created_at.isoformat(), "updated_at": item.updated_at.isoformat(),
            } for item in actions],
        })

    def _completed_run(self, event, run_id=None):
        qs = RoadFloodImpactRun.objects.select_related("road_dataset", "reference_base_release").filter(
            revision__event=event, status=RoadFloodImpactRun.Status.COMPLETED,
        )
        if run_id:
            return qs.filter(pk=run_id).first()
        if event.current_revision_id:
            current = qs.filter(revision_id=event.current_revision_id).order_by("-finished_at").first()
            if current:
                return current
        return qs.order_by("-finished_at").first()

    @action(detail=True, methods=["get"], url_path="roads")
    def roads(self, request, pk=None):
        event = self._event_queryset().filter(pk=pk).first()
        if event is None:
            return self._error("not_found", "Evento não encontrado.", status.HTTP_404_NOT_FOUND)
        run = self._completed_run(event, request.query_params.get("run_id"))
        if request.query_params.get("run_id") and run is None:
            return self._error("not_found", "Execução não encontrada.", status.HTTP_404_NOT_FOUND)
        qs = run.impacts.select_related("segment", "segment__street").order_by("-length_m") if run else RoadFloodImpact.objects.none()
        try:
            minimum = float(request.query_params.get("min_length_m", 0))
        except ValueError:
            return self._error("invalid_filter", "min_length_m inválido.")
        if run:
            qs = qs.filter(length_m__gte=minimum)
        return self._paginate(request, qs, lambda impact: {
            "id": str(impact.id), "run_id": str(impact.run_id), "segment_id": str(impact.segment_id),
            "road_axis_segment": str(impact.segment_id),
            "street": ({"id": str(impact.segment.street_id), "name": impact.segment.street.name} if impact.segment.street_id else None),
            "street_id": str(impact.segment.street_id) if impact.segment.street_id else None,
            "street_name": impact.segment.street.name if impact.segment.street_id else None,
            "intersection": json.loads(impact.intersection.geojson), "length_m": impact.length_m,
            "affected_length_m": impact.length_m, "segment_fraction": impact.segment_fraction,
            "relation": impact.relation, "evidence_kind": impact.evidence_kind,
            "evidence_status": impact.evidence_status,
            "dataset": self._run_payload(run)["dataset"],
            "reference_base_revision": str(run.reference_base_release_id) if run.reference_base_release_id else None,
            "algorithm_version": run.algorithm_version,
            "calculated_at": run.finished_at.isoformat() if run.finished_at else None,
        })

    @action(detail=True, methods=["get"], url_path="hotspots")
    def hotspots(self, request, pk=None):
        event = self._event_queryset().filter(pk=pk).first()
        if event is None:
            return self._error("not_found", "Evento não encontrado.", status.HTTP_404_NOT_FOUND)
        run = self._completed_run(event, request.query_params.get("run_id"))
        qs = run.hotspots.order_by("rank") if run else RoadImpactHotspot.objects.none()
        return self._paginate(request, qs, lambda hotspot: {
            "id": str(hotspot.id), "run_id": str(hotspot.run_id), "rank": hotspot.rank,
            "location": json.loads(hotspot.location.geojson), "impacted_length_m": hotspot.impacted_length_m,
            "segment_count": hotspot.segment_count, "dimension": hotspot.dimension,
            "dimension_key": hotspot.dimension_key, "label": hotspot.label,
        })


class RoadImpactHotspotViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request):
        qs = RoadImpactHotspot.objects.select_related("run__revision__event").filter(
            run__status=RoadFloodImpactRun.Status.COMPLETED,
            run__revision__status__in=[FloodSpatialEventRevision.Status.ACTIVE, FloodSpatialEventRevision.Status.SUPERSEDED],
        ).order_by("-run__revision__valid_from", "rank")
        if request.query_params.get("city_id"):
            qs = qs.filter(run__revision__event__city_id=request.query_params["city_id"])
        if request.query_params.get("dimension"):
            qs = qs.filter(dimension=request.query_params["dimension"])
        try:
            period_start, period_end = _parse_period(request.query_params)
        except ValueError as exc:
            return Response({"error": {"code": "invalid_filter", "detail": str(exc), "fields": {}}}, status=400)
        qs = qs.filter(_overlap_filter("run__revision__", period_start, period_end))
        evidence_kind = request.query_params.get("evidence_kind") or FloodSpatialEvent.EvidenceKind.CONFIRMED_OCCURRENCE
        qs = qs.filter(run__revision__event__evidence_kind=evidence_kind)
        if request.query_params.get("bbox"):
            try:
                values = [float(value) for value in request.query_params["bbox"].split(",")]
                qs = qs.filter(location__intersects=Polygon.from_bbox(values))
            except (TypeError, ValueError):
                return Response({"error": {"code": "invalid_filter", "detail": "bbox inválido.", "fields": {}}}, status=400)
        groups = defaultdict(lambda: {"items": [], "events": set(), "length": 0.0, "duration": 0})
        seen = set()
        for item in qs:
            unique = (item.run.revision.event_id, item.dimension, item.dimension_key)
            if unique in seen:
                continue
            seen.add(unique)
            group = groups[(item.dimension, item.dimension_key)]
            group["items"].append(item)
            group["events"].add(item.run.revision.event_id)
            group["length"] += item.impacted_length_m
            revision = item.run.revision
            group["duration"] += max(0, int(((revision.valid_until or revision.valid_from) - revision.valid_from).total_seconds()))
        results = []
        cameras = list(Camera.objects.exclude(status=Camera.CameraStatus.INACTIVE).select_related("address"))
        for group in groups.values():
            item = group["items"][0]
            origin = (item.location.y, item.location.x)
            nearby = []
            for camera in cameras:
                coordinates = camera_coordinates(camera)
                if coordinates is None:
                    continue
                distance = haversine_distance_m(origin, coordinates)
                if distance <= 2000:
                    nearby.append({"id": str(camera.id), "description": camera.description, "latitude": coordinates[0], "longitude": coordinates[1], "distance_m": distance, "active": camera.status == Camera.CameraStatus.ACTIVE})
            nearby.sort(key=lambda value: value["distance_m"])
            event_count = len(group["events"])
            results.append({
                "id": str(item.id), "event_id": str(item.run.revision.event_id),
                "spatial_unit": item.dimension, "spatial_id": item.dimension_key, "name": item.label,
                "geometry": json.loads(item.location.geojson), "evidence_kind": evidence_kind,
                "period_start": min(value.run.revision.valid_from for value in group["items"]).isoformat(),
                "period_end": max(value.run.revision.valid_until or value.run.revision.valid_from for value in group["items"]).isoformat(),
                "event_count": event_count,
                "confirmed_event_count": event_count if evidence_kind == FloodSpatialEvent.EvidenceKind.CONFIRMED_OCCURRENCE else 0,
                "affected_length_m": group["length"], "affected_duration_seconds": group["duration"],
                "recurrence_score": event_count, "nearby_cameras": nearby[:5],
                "algorithm_version": item.run.algorithm_version,
            })
        results.sort(key=lambda value: (-value["recurrence_score"], -value["affected_length_m"], value["name"]))
        paginator = FloodImpactPagination()
        page = paginator.paginate_queryset(results, request)
        return paginator.get_paginated_response(page)

    @action(detail=True, methods=["get"], url_path="history")
    def history(self, request, pk=None):
        hotspot = RoadImpactHotspot.objects.select_related("run__revision__event").filter(pk=pk).first()
        if hotspot is None:
            return Response({"error": {"code": "not_found", "detail": "Hotspot não encontrado.", "fields": {}}}, status=404)
        qs = RoadImpactHotspot.objects.select_related("run__revision__event").filter(
            dimension=hotspot.dimension, dimension_key=hotspot.dimension_key,
            run__revision__event__city=hotspot.run.revision.event.city,
            run__status=RoadFloodImpactRun.Status.COMPLETED,
            run__revision__status__in=[FloodSpatialEventRevision.Status.ACTIVE, FloodSpatialEventRevision.Status.SUPERSEDED],
        ).order_by("-run__revision__valid_from")
        try:
            period_start, period_end = _parse_period(request.query_params)
        except ValueError as exc:
            return Response({"error": {"code": "invalid_filter", "detail": str(exc), "fields": {}}}, status=400)
        qs = qs.filter(_overlap_filter("run__revision__", period_start, period_end))
        paginator = FloodImpactPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response([{
            "id": str(item.id), "event_id": str(item.run.revision.event_id),
            "evidence_kind": item.run.revision.event.evidence_kind, "status": item.run.revision.status,
            "valid_from": item.run.revision.valid_from.isoformat(),
            "valid_until": item.run.revision.valid_until.isoformat() if item.run.revision.valid_until else None,
            "affected_length_m": item.impacted_length_m,
            "affected_regions": item.run.revision.affected_regions,
            "affected_streets": item.run.revision.affected_streets,
        } for item in page])
