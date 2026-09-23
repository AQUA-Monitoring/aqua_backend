from django.contrib.gis.geos import LineString, MultiLineString, MultiPolygon, Polygon
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.addressing.models import City, GeodataDataset, ReferenceBaseRelease, RoadAxisSegment, Street
from core.flood_impact.models import FloodImpactAdministrativeAction, FloodSpatialEvent, RoadFloodImpact
from core.users.infra.models import User


class FloodImpactGovernanceContractTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create(name="Admin impacto", email="impact-admin@example.test", type=User.UserType.ADMIN)
        self.reader = User.objects.create(name="Leitor impacto", email="impact-reader@example.test")
        self.client.force_authenticate(self.admin)
        self.city = City.objects.create(
            name="Cidade governança",
            geometry=MultiPolygon(
                Polygon(((-50, -27), (-48, -27), (-48, -25), (-50, -25), (-50, -27))), srid=4326,
            ),
        )
        self.dataset = GeodataDataset.objects.create(
            city=self.city, kind=GeodataDataset.Kind.STREET, authority="Teste", title="Malha viária",
            source_url="https://example.test/roads", license_name="Teste", source_version="2026.1",
            retrieved_at=timezone.now(), sha256="e" * 64, source_crs="EPSG:4326",
            status=GeodataDataset.Status.ACTIVE,
        )
        self.release = ReferenceBaseRelease.objects.create(
            revision="ref-2026-01", status=ReferenceBaseRelease.Status.ACTIVE,
            archive_sha256="f" * 64, manifest={"revision": "ref-2026-01"}, dataset=self.dataset,
        )
        self.footprint = {
            "type": "MultiPolygon",
            "coordinates": [[[[-49.2, -26.5], [-49.0, -26.5], [-49.0, -26.3], [-49.2, -26.3], [-49.2, -26.5]]]],
        }

    def create_event(self):
        response = self.client.post(
            "/api/flood-impact/events/",
            {
                "city": str(self.city.id), "evidence_kind": "USER_REPORT",
                "geometry_method": "MANUAL", "footprint": self.footprint,
                "valid_from": timezone.now().isoformat(),
                "reference_base_revision": str(self.release.id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response

    def test_payload_exposes_release_provenance_freshness_and_permissions(self):
        created = self.create_event()
        self.assertEqual(created.data["reference_base_revision"], str(self.release.id))
        self.assertEqual(created.data["provenance"]["reference_base"]["revision"], self.release.revision)
        self.assertEqual(created.data["freshness"], "NOT_REQUESTED")
        self.assertTrue(created.data["permissions"]["can_review"])
        self.assertIsNone(created.data["current_run_summary"])

        self.client.force_authenticate(self.reader)
        read = self.client.get(f'/api/flood-impact/events/{created.data["id"]}/')
        self.assertEqual(read.status_code, 200, read.data)
        self.assertFalse(read.data["permissions"]["can_edit"])

    def test_active_reference_release_is_used_when_omitted(self):
        response = self.client.post(
            "/api/flood-impact/events/",
            {
                "city": str(self.city.id), "evidence_kind": "FORECAST",
                "geometry_method": "PROVIDED", "footprint": self.footprint,
                "valid_from": timezone.now().isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["reference_base_revision"], str(self.release.id))

    def test_confirmed_and_legacy_evidence_cannot_be_created_directly(self):
        base = {
            "city": str(self.city.id), "geometry_method": "MANUAL",
            "footprint": self.footprint, "valid_from": timezone.now().isoformat(),
        }
        confirmed = self.client.post(
            "/api/flood-impact/events/", {**base, "evidence_kind": "CONFIRMED_OCCURRENCE"}, format="json",
        )
        self.assertEqual(confirmed.status_code, 422, confirmed.data)
        self.assertEqual(confirmed.data["error"]["code"], "confirmation_required")
        legacy = self.client.post(
            "/api/flood-impact/events/", {**base, "evidence_kind": "LEGACY_UNCLASSIFIED"}, format="json",
        )
        self.assertEqual(legacy.status_code, 400, legacy.data)

    def test_review_conflict_and_publication_are_audited(self):
        created = self.create_event()
        url = f'/api/flood-impact/events/{created.data["id"]}/reviews/'
        conflict = self.client.post(
            url, {"decision": "PUBLISH", "justification": "Revisão humana", "expected_revision": 99}, format="json",
        )
        self.assertEqual(conflict.status_code, 409, conflict.data)
        self.assertEqual(conflict.data["error"]["code"], "revision_conflict")
        self.assertEqual(FloodImpactAdministrativeAction.objects.count(), 0)

        published = self.client.post(
            url,
            {
                "decision": "PUBLISH", "justification": "Geometria conferida",
                "expected_revision": 1, "reference_base_revision": str(self.release.id),
            },
            format="json",
        )
        self.assertEqual(published.status_code, 200, published.data)
        self.assertEqual(published.data["status"], "ACTIVE")
        action = FloodImpactAdministrativeAction.objects.get()
        self.assertEqual(action.action, FloodImpactAdministrativeAction.Action.PUBLISH)
        self.assertEqual(action.from_status, "DRAFT")
        self.assertEqual(action.to_status, "ACTIVE")
        self.assertEqual(action.actor, self.admin)

    def test_confirmation_creates_derived_event_without_mutating_origin(self):
        created = self.create_event()
        published = self.client.post(
            f'/api/flood-impact/events/{created.data["id"]}/reviews/',
            {"decision": "PUBLISH", "justification": "Pronta para confirmação", "expected_revision": 1},
            format="json",
        )
        self.assertEqual(published.status_code, 200, published.data)
        origin = FloodSpatialEvent.objects.get(pk=created.data["id"])
        origin_status = origin.current_revision.status
        response = self.client.post(
            f"/api/flood-impact/events/{origin.id}/confirmations/",
            {
                "justification": "Ocorrência confirmada por equipe de campo",
                "expected_revision": 1, "reference_base_revision": str(self.release.id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["evidence_kind"], "CONFIRMED_OCCURRENCE")
        self.assertEqual(response.data["derived_from_event_id"], str(origin.id))
        origin.refresh_from_db()
        self.assertEqual(origin.current_revision.status, origin_status)
        self.assertEqual(origin.derived_events.count(), 1)
        self.assertTrue(origin.administrative_actions.filter(action=FloodImpactAdministrativeAction.Action.CONFIRM).exists())
        self.assertFalse(response.data["permissions"]["can_confirm"])
        repeated = self.client.post(
            f'/api/flood-impact/events/{response.data["id"]}/confirmations/',
            {"justification": "Tentativa de reconfirmar", "expected_revision": 1}, format="json",
        )
        self.assertEqual(repeated.status_code, 409, repeated.data)
        self.assertEqual(repeated.data["error"]["code"], "invalid_state")

    def test_zero_impact_run_is_completed_and_exposes_run_metadata(self):
        created = self.create_event()
        response = self.client.post(
            f'/api/flood-impact/events/{created.data["id"]}/impact-runs/',
            {
                "reason": "Atualização operacional", "road_dataset_id": str(self.dataset.id),
                "reference_base_revision": str(self.release.id), "expected_revision": 1,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "COMPLETED")
        self.assertEqual(response.data["report"]["impacted_segments"], 0)
        self.assertEqual(response.data["reference_base_revision"], str(self.release.id))
        self.assertEqual(response.data["dataset"]["id"], str(self.dataset.id))
        self.assertEqual(RoadFloodImpact.objects.count(), 0)

        runs = self.client.get(f'/api/flood-impact/events/{created.data["id"]}/impact-runs/')
        self.assertEqual(runs.status_code, 200, runs.data)
        self.assertEqual(runs.data["count"], 1)
        filtered = self.client.get(
            f'/api/flood-impact/events/{created.data["id"]}/impact-runs/',
            {
                "status": "COMPLETED", "road_dataset_id": str(self.dataset.id),
                "reference_base_revision": str(self.release.id),
            },
        )
        self.assertEqual(filtered.status_code, 200, filtered.data)
        self.assertEqual(filtered.data["count"], 1)
        history = self.client.get(f'/api/flood-impact/events/{created.data["id"]}/history/')
        self.assertEqual(history.status_code, 200, history.data)
        self.assertEqual(history.data["actions"][0]["action"], "RECALCULATE")

    def test_impact_run_requires_expected_revision_and_legacy_alias_injects_it(self):
        created = self.create_event()
        url = f'/api/flood-impact/events/{created.data["id"]}/impact-runs/'
        missing = self.client.post(
            url, {"reason": "Sem revisão", "road_dataset_id": str(self.dataset.id)}, format="json",
        )
        self.assertEqual(missing.status_code, 400, missing.data)
        stale = self.client.post(
            url,
            {"reason": "Revisão antiga", "road_dataset_id": str(self.dataset.id), "expected_revision": 99},
            format="json",
        )
        self.assertEqual(stale.status_code, 409, stale.data)
        legacy = self.client.post(
            f'/api/flood-impact/events/{created.data["id"]}/recalculate/',
            {"road_dataset_id": str(self.dataset.id)}, format="json",
        )
        self.assertEqual(legacy.status_code, 201, legacy.data)
        self.assertEqual(legacy.data["status"], "COMPLETED")

    def test_revoked_event_permissions_disable_mutations(self):
        created = self.create_event()
        url = f'/api/flood-impact/events/{created.data["id"]}/reviews/'
        published = self.client.post(
            url,
            {"decision": "PUBLISH", "justification": "Publicação inicial", "expected_revision": 1},
            format="json",
        )
        self.assertEqual(published.status_code, 200, published.data)
        revoked = self.client.post(
            url,
            {"decision": "REVOKE", "justification": "Evidência invalidada", "expected_revision": 1},
            format="json",
        )
        self.assertEqual(revoked.status_code, 200, revoked.data)
        self.assertFalse(revoked.data["permissions"]["can_edit"])
        self.assertFalse(revoked.data["permissions"]["can_review"])
        self.assertFalse(revoked.data["permissions"]["can_confirm"])
        self.assertFalse(revoked.data["permissions"]["can_recalculate"])

        revision_attempt = self.client.post(
            f'/api/flood-impact/events/{created.data["id"]}/revisions/',
            {"justification": "Contorno", "source_revision": 1}, format="json",
        )
        self.assertEqual(revision_attempt.status_code, 409, revision_attempt.data)
        confirmation_attempt = self.client.post(
            f'/api/flood-impact/events/{created.data["id"]}/confirmations/',
            {"justification": "Contorno", "expected_revision": 1}, format="json",
        )
        self.assertEqual(confirmation_attempt.status_code, 409, confirmation_attempt.data)
        run_attempt = self.client.post(
            f'/api/flood-impact/events/{created.data["id"]}/impact-runs/',
            {"reason": "Contorno", "expected_revision": 1}, format="json",
        )
        self.assertEqual(run_attempt.status_code, 409, run_attempt.data)

    def test_review_transitions_and_draft_confirmation_are_enforced_server_side(self):
        created = self.create_event()
        event_id = created.data["id"]
        self.assertFalse(created.data["permissions"]["can_confirm"])
        draft_confirmation = self.client.post(
            f"/api/flood-impact/events/{event_id}/confirmations/",
            {"justification": "Contorno em rascunho", "expected_revision": 1}, format="json",
        )
        self.assertEqual(draft_confirmation.status_code, 409, draft_confirmation.data)
        draft_revoke = self.client.post(
            f"/api/flood-impact/events/{event_id}/reviews/",
            {"decision": "REVOKE", "justification": "Contorno em rascunho", "expected_revision": 1},
            format="json",
        )
        self.assertEqual(draft_revoke.status_code, 409, draft_revoke.data)
        published = self.client.post(
            f"/api/flood-impact/events/{event_id}/reviews/",
            {"decision": "PUBLISH", "justification": "Transição válida", "expected_revision": 1},
            format="json",
        )
        self.assertEqual(published.status_code, 200, published.data)
        repeated_publish = self.client.post(
            f"/api/flood-impact/events/{event_id}/reviews/",
            {"decision": "PUBLISH", "justification": "Contorno ativo", "expected_revision": 1},
            format="json",
        )
        self.assertEqual(repeated_publish.status_code, 409, repeated_publish.data)

    def test_reader_cannot_write_but_can_read(self):
        created = self.create_event()
        self.client.force_authenticate(self.reader)
        read = self.client.get(f'/api/flood-impact/events/{created.data["id"]}/history/')
        self.assertEqual(read.status_code, 200, read.data)
        write = self.client.post(
            f'/api/flood-impact/events/{created.data["id"]}/reviews/',
            {"decision": "REVOKE", "justification": "sem permissão", "expected_revision": 1},
            format="json",
        )
        self.assertEqual(write.status_code, 403, write.data)


class FloodImpactRoadMetadataContractTests(TestCase):
    def test_roads_expose_dataset_release_algorithm_and_calculated_at(self):
        client = APIClient()
        admin = User.objects.create(name="Admin road", email="road-admin@example.test", type=User.UserType.ADMIN)
        client.force_authenticate(admin)
        city = City.objects.create(
            name="Cidade road metadata",
            geometry=MultiPolygon(Polygon(((-2, -2), (3, -2), (3, 3), (-2, 3), (-2, -2))), srid=4326),
        )
        dataset = GeodataDataset.objects.create(
            city=city, kind="street", authority="Teste", title="Eixos", source_url="https://example.test/eixos",
            license_name="Teste", source_version="1", retrieved_at=timezone.now(), sha256="a" * 64,
            source_crs="EPSG:4326", status="active",
        )
        street = Street.objects.create(
            city=city, dataset=dataset, source_record_id="street-1", name="Rua Um", normalized_name="rua um",
            geometry=MultiLineString(LineString((-1, .5), (2, .5)), srid=4326),
        )
        RoadAxisSegment.objects.create(
            city=city, street=street, dataset=dataset, source_record_id="segment-1", geometry=street.geometry,
        )
        created = client.post(
            "/api/flood-impact/events/",
            {
                "city": str(city.id), "evidence_kind": "FORECAST", "geometry_method": "PROVIDED",
                "footprint": {"type": "MultiPolygon", "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]]},
                "valid_from": timezone.now().isoformat(),
            },
            format="json",
        )
        run = client.post(
            f'/api/flood-impact/events/{created.data["id"]}/impact-runs/',
            {"reason": "Cálculo de contrato", "road_dataset_id": str(dataset.id), "expected_revision": 1}, format="json",
        )
        roads = client.get(f'/api/flood-impact/events/{created.data["id"]}/roads/?run_id={run.data["id"]}')
        self.assertEqual(roads.status_code, 200, roads.data)
        item = roads.data["results"][0]
        self.assertEqual(item["dataset"]["id"], str(dataset.id))
        self.assertEqual(item["algorithm_version"], "road-intersection-v1")
        self.assertIsNotNone(item["calculated_at"])
