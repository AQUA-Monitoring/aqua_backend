from datetime import timedelta
from types import ModuleType
from unittest.mock import Mock, patch

from django.db.models.deletion import ProtectedError
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from core.addressing.models import (
    AddressReference,
    City,
    GeodataDataset,
    Neighborhood,
    Region,
    Street,
    StreetNeighborhood,
)
from core.flood_camera_monitoring.infra.models import (
    Camera,
    CameraOperationalSnapshot,
)
from core.flood_camera_monitoring.presentation.serializers import CameraReadSerializer
from core.users.infra.models import User


class CameraMetadataApiTests(APITestCase):
    def setUp(self):
        self.initial_camera_count = Camera.objects.count()
        self.city = City.objects.create(name="Cidade Câmera Teste")
        self.region = Region.objects.create(
            name="Centro",
            city=self.city.name,
            city_ref=self.city,
        )
        self.neighborhood = Neighborhood.objects.create(
            name="América",
            city=self.city.name,
            city_ref=self.city,
            region=self.region,
        )
        self.admin = User.objects.create(
            name="Admin",
            email="admin-cameras@example.test",
            type=User.UserType.ADMIN,
        )
        self.standard = User.objects.create(
            name="Standard",
            email="standard-cameras@example.test",
            type=User.UserType.STANDARD,
        )

    def camera_payload(self, *, hls="https://cameras.example/stream.m3u8"):
        return {
            "description": "Câmera América",
            "video_hls": hls,
            "video_embed": None,
            "address": {
                "city_id": str(self.city.id),
                "neighborhood_id": str(self.neighborhood.id),
                "street": "Rua Blumenau",
                "number": "100",
                "state": "SC",
                "country": "Brazil",
                "zipcode": "89204-250",
                "latitude": -26.285,
                "longitude": -48.853,
            },
        }

    def create_camera_as_admin(self, **kwargs):
        self.client.force_authenticate(self.admin)
        return self.client.post(
            "/api/flood_monitoring/cameras/",
            self.camera_payload(**kwargs),
            format="json",
        )

    def test_admin_creates_camera_address_author_and_default_snapshot(self):
        response = self.create_camera_as_admin(
            hls=" HTTPS://CAMERAS.EXAMPLE/stream.m3u8/ "
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        camera = Camera.objects.select_related("address", "created_by").get(
            pk=response.data["id"]
        )
        self.assertEqual(camera.status, Camera.CameraStatus.INACTIVE)
        self.assertEqual(camera.video_hls, "https://cameras.example/stream.m3u8")
        self.assertEqual(camera.created_by, self.admin)
        self.assertEqual(camera.address.city_ref, self.city)
        self.assertEqual(camera.address.neighborhood, self.neighborhood)
        self.assertEqual(camera.latitude, camera.address.latitude)
        snapshot = camera.operational_snapshot
        self.assertEqual(snapshot.stream_status, snapshot.StreamStatus.UNKNOWN)
        self.assertEqual(snapshot.analysis_status, snapshot.AnalysisStatus.NOT_ANALYZED)
        self.assertEqual(snapshot.model_status, snapshot.ModelStatus.UNKNOWN)
        self.assertEqual(response.data["created_by"], {"id": str(self.admin.id)})

    def test_camera_payload_prioritizes_canonical_region_over_legacy_location(self):
        camera = Camera.objects.create(
            status=Camera.CameraStatus.ACTIVE,
            description="Câmera com região canônica",
            region=self.region,
        )

        self.assertEqual(
            CameraReadSerializer(camera).data["region"],
            {"id": str(self.region.id), "name": self.region.name},
        )

    def test_camera_creation_allows_neighborhood_without_canonical_region_as_inactive(self):
        unassigned = Neighborhood.objects.create(
            name="Sem região",
            city=self.city.name,
            city_ref=self.city,
        )
        payload = self.camera_payload()
        payload["address"]["neighborhood_id"] = str(unassigned.id)
        self.client.force_authenticate(self.admin)

        response = self.client.post("/api/flood_monitoring/cameras/", payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "INACTIVE")
        self.assertIsNone(response.data["region"])
        camera = Camera.objects.select_related("address").get(pk=response.data["id"])
        self.assertEqual(camera.neighborhood, unassigned)
        self.assertEqual(camera.address.neighborhood, unassigned)
        self.assertIsNone(camera.region)

    def test_inactive_camera_without_region_cannot_become_operational(self):
        unassigned = Neighborhood.objects.create(
            name="Sem região",
            city=self.city.name,
            city_ref=self.city,
        )
        self.client.force_authenticate(self.admin)

        for index, target_status in enumerate(("ACTIVE", "OFFLINE"), start=1):
            payload = self.camera_payload(
                hls=f"https://cameras.example/regionless-{index}.m3u8"
            )
            payload["address"]["neighborhood_id"] = str(unassigned.id)
            created = self.client.post(
                "/api/flood_monitoring/cameras/", payload, format="json"
            )

            response = self.client.patch(
                f"/api/flood_monitoring/cameras/{created.data['id']}/",
                {"status": target_status},
                format="json",
            )

            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(
                response.data["status"],
                [
                    "A câmera precisa estar vinculada a uma região ativa da Base "
                    "georreferenciada oficial antes da ativação."
                ],
            )

    def test_regionless_camera_can_be_activated_with_valid_address_in_same_patch(self):
        unassigned = Neighborhood.objects.create(
            name="Sem região",
            city=self.city.name,
            city_ref=self.city,
        )
        payload = self.camera_payload(hls="https://cameras.example/draft.m3u8")
        payload["address"]["neighborhood_id"] = str(unassigned.id)
        self.client.force_authenticate(self.admin)
        created = self.client.post(
            "/api/flood_monitoring/cameras/", payload, format="json"
        )

        response = self.client.patch(
            f"/api/flood_monitoring/cameras/{created.data['id']}/",
            {"status": "ACTIVE", "address": self.camera_payload()["address"]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "ACTIVE")
        self.assertEqual(response.data["region"]["id"], str(self.region.id))

    def test_admin_creates_camera_with_canonical_address_reference_snapshot(self):
        dataset = GeodataDataset.objects.create(
            city=self.city,
            kind=GeodataDataset.Kind.ADDRESS,
            authority="IBGE",
            title="CNEFE",
            source_url="https://example.test/cnefe",
            license_name="Licença pública",
            source_version="2022",
            retrieved_at=timezone.now(),
            sha256="1" * 64,
            source_crs="EPSG:4326",
            status=GeodataDataset.Status.ACTIVE,
        )
        street = Street.objects.create(
            city=self.city,
            dataset=dataset,
            source_record_id="street-1",
            name="Rua Canônica",
            normalized_name="rua canonica",
        )
        StreetNeighborhood.objects.create(
            street=street, neighborhood=self.neighborhood
        )
        reference = AddressReference.objects.create(
            city=self.city,
            neighborhood=self.neighborhood,
            street=street,
            dataset=dataset,
            source_record_id="address-1",
            street_name="Rua Canônica",
            number="321",
            zipcode="89200-321",
            location=Point(-48.853, -26.285, srid=4326),
        )
        payload = self.camera_payload(
            hls="https://cameras.example/canonical.m3u8"
        )
        payload["address"].update(
            {
                "street_id": str(street.id),
                "address_reference_id": str(reference.id),
                "street": "Texto digitado desatualizado",
                "number": "999",
                "zipcode": "00000-000",
            }
        )
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            "/api/flood_monitoring/cameras/", payload, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        camera = Camera.objects.select_related("address").get(pk=response.data["id"])
        self.assertEqual(camera.address.street_ref, street)
        self.assertEqual(camera.address.address_reference, reference)
        self.assertEqual(camera.address.dataset, dataset)
        self.assertEqual(camera.address.street, "Rua Canônica")
        self.assertEqual(camera.address.number, "321")
        self.assertEqual(camera.address.zipcode, "89200-321")
        self.assertEqual(response.data["address"]["street_id"], str(street.id))
        self.assertEqual(
            response.data["address"]["address_reference_id"], str(reference.id)
        )

        mismatched = self.camera_payload(
            hls="https://cameras.example/canonical-mismatched.m3u8"
        )
        mismatched["address"].update(
            {
                "street_id": str(street.id),
                "address_reference_id": str(reference.id),
                "latitude": -26.295,
                "longitude": -48.863,
            }
        )
        rejected = self.client.post(
            "/api/flood_monitoring/cameras/", mismatched, format="json"
        )
        self.assertEqual(rejected.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("address_reference_id", rejected.data["address"])

    def test_camera_rejects_inconsistent_street_and_address_reference(self):
        dataset = GeodataDataset.objects.create(
            city=self.city,
            kind=GeodataDataset.Kind.ADDRESS,
            authority="IBGE",
            title="CNEFE",
            source_url="https://example.test/cnefe-inconsistent",
            license_name="Licença pública",
            source_version="2022",
            retrieved_at=timezone.now(),
            sha256="2" * 64,
            source_crs="EPSG:4326",
            status=GeodataDataset.Status.ACTIVE,
        )
        street = Street.objects.create(
            city=self.city,
            dataset=dataset,
            source_record_id="street-good",
            name="Rua Correta",
            normalized_name="rua correta",
        )
        other_street = Street.objects.create(
            city=self.city,
            dataset=dataset,
            source_record_id="street-wrong",
            name="Rua Incorreta",
            normalized_name="rua incorreta",
        )
        reference = AddressReference.objects.create(
            city=self.city,
            neighborhood=self.neighborhood,
            street=street,
            dataset=dataset,
            source_record_id="address-good",
            street_name="Rua Correta",
            location=Point(-48.853, -26.285, srid=4326),
        )
        payload = self.camera_payload(
            hls="https://cameras.example/inconsistent.m3u8"
        )
        payload["address"].update(
            {
                "street_id": str(other_street.id),
                "address_reference_id": str(reference.id),
            }
        )
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            "/api/flood_monitoring/cameras/", payload, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("street_id", response.data["address"])

    def test_camera_accepts_equivalent_axes_for_the_same_street_name(self):
        dataset = GeodataDataset.objects.create(
            city=self.city,
            kind=GeodataDataset.Kind.ADDRESS,
            authority="IBGE",
            title="CNEFE equivalent axes",
            source_url="https://example.test/cnefe-equivalent",
            license_name="Licença pública",
            source_version="2022",
            retrieved_at=timezone.now(),
            sha256="3" * 64,
            source_crs="EPSG:4326",
            status=GeodataDataset.Status.ACTIVE,
        )
        reference_axis = Street.objects.create(
            city=self.city,
            dataset=dataset,
            source_record_id="street-reference-axis",
            name="Rua Canônica",
            normalized_name="rua canonica",
        )
        selected_axis = Street.objects.create(
            city=self.city,
            dataset=dataset,
            source_record_id="street-selected-axis",
            name="Rua Canônica",
            normalized_name="rua canonica",
        )
        StreetNeighborhood.objects.create(
            street=selected_axis, neighborhood=self.neighborhood
        )
        reference = AddressReference.objects.create(
            city=self.city,
            neighborhood=self.neighborhood,
            street=reference_axis,
            dataset=dataset,
            source_record_id="address-equivalent-axis",
            street_name="Rua Canônica",
            number="42",
            location=Point(-48.853, -26.285, srid=4326),
        )
        payload = self.camera_payload(
            hls="https://cameras.example/equivalent-axis.m3u8"
        )
        payload["address"].update(
            {
                "street_id": str(selected_axis.id),
                "address_reference_id": str(reference.id),
            }
        )
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            "/api/flood_monitoring/cameras/", payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["address"]["street_id"], str(selected_axis.id))

        other_neighborhood = Neighborhood.objects.create(
            name="Outro bairro",
            city=self.city.name,
            city_ref=self.city,
        )
        wrong_axis = Street.objects.create(
            city=self.city,
            dataset=dataset,
            source_record_id="street-wrong-neighborhood-axis",
            name="Rua Canônica",
            normalized_name="rua canonica",
        )
        StreetNeighborhood.objects.create(
            street=wrong_axis, neighborhood=other_neighborhood
        )
        wrong_payload = self.camera_payload(
            hls="https://cameras.example/wrong-neighborhood-axis.m3u8"
        )
        wrong_payload["address"].update(
            {
                "street_id": str(wrong_axis.id),
                "address_reference_id": str(reference.id),
            }
        )
        rejected = self.client.post(
            "/api/flood_monitoring/cameras/", wrong_payload, format="json"
        )
        self.assertEqual(rejected.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("street_id", rejected.data["address"])

    def test_creation_requires_admin_and_rejects_client_audit_fields(self):
        response = self.client.post(
            "/api/flood_monitoring/cameras/", self.camera_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        self.client.force_authenticate(self.standard)
        response = self.client.post(
            "/api/flood_monitoring/cameras/", self.camera_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.admin)
        payload = self.camera_payload()
        payload["status"] = "ACTIVE"
        payload["created_by"] = str(self.standard.id)
        response = self.client.post(
            "/api/flood_monitoring/cameras/", payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("status", response.data)
        self.assertIn("created_by", response.data)
        self.assertEqual(Camera.objects.count(), self.initial_camera_count)

    def test_duplicate_normalized_hls_returns_conflict(self):
        first = self.create_camera_as_admin(
            hls="https://CAMERAS.example/stream.m3u8/"
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)

        second_payload = self.camera_payload(
            hls="https://cameras.example/stream.m3u8"
        )
        second_payload["description"] = "Outra câmera"
        second = self.client.post(
            "/api/flood_monitoring/cameras/", second_payload, format="json"
        )
        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("video_hls", second.data)
        self.assertEqual(Camera.objects.count(), self.initial_camera_count + 1)

    def test_referenced_address_and_author_are_protected(self):
        created = self.create_camera_as_admin()
        camera = Camera.objects.select_related("address", "created_by").get(
            pk=created.data["id"]
        )

        with self.assertRaises(ProtectedError):
            camera.address.delete()
        with self.assertRaises(ProtectedError):
            self.admin.delete()

    def test_snapshot_failure_rolls_back_camera_and_address(self):
        self.client.force_authenticate(self.admin)
        address_count = self.city.addresses.count()
        camera_count = Camera.objects.count()

        with patch.object(
            CameraOperationalSnapshot.objects,
            "create",
            side_effect=RuntimeError("snapshot failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "snapshot failure"):
                self.client.post(
                    "/api/flood_monitoring/cameras/",
                    self.camera_payload(),
                    format="json",
                )

        self.assertEqual(Camera.objects.count(), camera_count)
        self.assertEqual(self.city.addresses.count(), address_count)

    def test_metadata_routes_do_not_call_inference_or_stream_adapters(self):
        classifier_factory = Mock(side_effect=AssertionError("classifier called"))
        stream_factory = Mock(side_effect=AssertionError("stream called"))
        classifier_module = ModuleType(
            "core.flood_camera_monitoring.infra.torch_flood_classifier"
        )
        classifier_module.build_default_classifier = classifier_factory
        stream_module = ModuleType(
            "core.flood_camera_monitoring.infra.opencv_stream"
        )
        stream_module.OpenCVVideoStream = stream_factory

        with patch.dict(
            "sys.modules",
            {
                classifier_module.__name__: classifier_module,
                stream_module.__name__: stream_module,
            },
        ):
            created = self.create_camera_as_admin(
                hls="https://cameras.example/no-inference.m3u8"
            )
            self.assertEqual(created.status_code, status.HTTP_201_CREATED)
            camera = Camera.objects.get(pk=created.data["id"])
            camera.status = Camera.CameraStatus.ACTIVE
            camera.save(update_fields=["status"])

            self.client.force_authenticate(user=None)
            self.assertEqual(
                self.client.get("/api/flood_monitoring/cameras/").status_code,
                status.HTTP_200_OK,
            )
            self.assertEqual(
                self.client.get(
                    f"/api/flood_monitoring/cameras/{camera.id}/"
                ).status_code,
                status.HTTP_200_OK,
            )
            self.assertEqual(
                self.client.get("/api/flood_monitoring/predict/all/").status_code,
                status.HTTP_200_OK,
            )

        classifier_factory.assert_not_called()
        stream_factory.assert_not_called()

    def test_public_list_omits_reproduction_and_author_and_detail_is_progressive(self):
        created = self.create_camera_as_admin()
        camera_id = created.data["id"]
        self.client.force_authenticate(user=None)

        listing = self.client.get("/api/flood_monitoring/cameras/")
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertTrue(
            all("created_by" not in camera for camera in listing.data["results"])
        )
        self.assertTrue(
            all("video_hls" not in camera for camera in listing.data["results"])
        )
        self.assertTrue(
            all("video_embed" not in camera for camera in listing.data["results"])
        )

        public_detail = self.client.get(
            f"/api/flood_monitoring/cameras/{camera_id}/"
        )
        self.assertNotIn("created_by", public_detail.data)
        self.assertIsNone(public_detail.data["video_hls"])
        self.assertIsNone(public_detail.data["video_embed"])

        self.client.force_authenticate(self.standard)
        standard_detail = self.client.get(
            f"/api/flood_monitoring/cameras/{camera_id}/"
        )
        self.assertIsNone(standard_detail.data["video_hls"])
        self.assertIsNone(standard_detail.data["video_embed"])

        self.client.force_authenticate(self.admin)
        admin_detail = self.client.get(
            f"/api/flood_monitoring/cameras/{camera_id}/"
        )
        self.assertEqual(
            admin_detail.data["created_by"], {"id": str(self.admin.id)}
        )
        self.assertEqual(
            admin_detail.data["video_hls"],
            "https://cameras.example/stream.m3u8",
        )

    def test_active_unavailable_camera_keeps_source_for_explicit_public_retry(self):
        created = self.create_camera_as_admin()
        camera = Camera.objects.get(pk=created.data["id"])
        camera.status = Camera.CameraStatus.ACTIVE
        camera.save(update_fields=["status"])
        snapshot = camera.operational_snapshot
        snapshot.stream_status = snapshot.StreamStatus.UNAVAILABLE
        snapshot.save(update_fields=["stream_status"])

        self.client.force_authenticate(user=None)
        detail = self.client.get(f"/api/flood_monitoring/cameras/{camera.id}/")

        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(
            detail.data["video_hls"], "https://cameras.example/stream.m3u8"
        )
        self.assertEqual(
            detail.data["operational"]["stream"]["status"], "UNAVAILABLE"
        )

    def test_admin_patch_accepts_named_status_and_offline_keeps_only_stream(self):
        created = self.create_camera_as_admin()
        camera = Camera.objects.get(pk=created.data["id"])
        snapshot = camera.operational_snapshot
        snapshot.stream_status = snapshot.StreamStatus.ONLINE
        snapshot.analysis_status = snapshot.AnalysisStatus.AVAILABLE
        snapshot.classification = snapshot.CameraClassification.FLOOD_INDICATION
        snapshot.prob_normal, snapshot.prob_medium, snapshot.prob_flooded = 5, 5, 90
        snapshot.confidence, snapshot.frames = 90, 3
        snapshot.model_status, snapshot.model_version = snapshot.ModelStatus.READY, "model-v1"
        snapshot.analyzed_at = timezone.now()
        snapshot.save()

        response = self.client.patch(
            f"/api/flood_monitoring/cameras/{camera.id}/",
            {"status": "OFFLINE", "video_embed": "https://cameras.example/embed"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "OFFLINE")
        self.assertEqual(response.data["video_hls"], "https://cameras.example/stream.m3u8")
        self.assertEqual(response.data["video_embed"], "https://cameras.example/embed")
        self.assertEqual(response.data["operational"]["analysis"]["status"], "NOT_ANALYZED")
        self.assertIsNone(response.data["operational"]["analysis"]["probabilities"])
        camera.refresh_from_db()
        self.assertEqual(camera.status, Camera.CameraStatus.OFFLINE)

    def test_only_admin_can_patch_camera_metadata(self):
        created = self.create_camera_as_admin()
        self.client.force_authenticate(self.standard)

        response = self.client.patch(
            f"/api/flood_monitoring/cameras/{created.data['id']}/",
            {"status": "INACTIVE"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_patch_updates_camera_address_selected_on_map(self):
        created = self.create_camera_as_admin()
        camera = Camera.objects.get(pk=created.data["id"])
        address = {
            "city_id": str(self.city.id),
            "neighborhood_id": str(self.neighborhood.id),
            "street": "Rua selecionada no mapa",
            "number": "250",
            "state": "SC",
            "country": "Brasil",
            "zipcode": "89200-250",
            "latitude": -26.286,
            "longitude": -48.854,
        }

        response = self.client.patch(
            f"/api/flood_monitoring/cameras/{camera.id}/",
            {"address": address},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        camera.refresh_from_db()
        camera.address.refresh_from_db()
        self.assertEqual(camera.address.street, address["street"])
        self.assertEqual(camera.address.neighborhood, self.neighborhood)
        self.assertEqual(camera.latitude, address["latitude"])
        self.assertEqual(camera.longitude, address["longitude"])
        self.assertEqual(response.data["address"]["street"], address["street"])

    def test_admin_patch_resolves_official_region_without_neighborhood(self):
        boundary = MultiPolygon(
            Polygon(((-49, -27), (-48, -27), (-48, -26), (-49, -26), (-49, -27))),
            srid=4326,
        )
        self.city.geometry = boundary
        self.city.save(update_fields=["geometry"])
        self.region.geometry = boundary
        self.region.save(update_fields=["geometry"])
        created = self.create_camera_as_admin(
            hls="https://cameras.example/official-region-only.m3u8"
        )
        address = self.camera_payload()["address"]
        address.pop("neighborhood_id")

        response = self.client.patch(
            f"/api/flood_monitoring/cameras/{created.data['id']}/",
            {"address": address},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["region"]["id"], str(self.region.id))
        self.assertIsNone(response.data["neighborhood"])

    def test_inactive_camera_can_update_to_regionless_address(self):
        created = self.create_camera_as_admin(
            hls="https://cameras.example/regionless-location.m3u8"
        )
        unassigned = Neighborhood.objects.create(
            name="Sem região para edição",
            city=self.city.name,
            city_ref=self.city,
        )
        address = self.camera_payload()["address"]
        address["neighborhood_id"] = str(unassigned.id)

        response = self.client.patch(
            f"/api/flood_monitoring/cameras/{created.data['id']}/",
            {"address": address},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["region"])
        camera = Camera.objects.get(pk=created.data["id"])
        self.assertEqual(camera.status, Camera.CameraStatus.INACTIVE)
        self.assertIsNone(camera.region)

    def test_operational_camera_with_region_cannot_move_to_regionless_address(self):
        created = self.create_camera_as_admin(
            hls="https://cameras.example/territorially-ready.m3u8"
        )
        activated = self.client.patch(
            f"/api/flood_monitoring/cameras/{created.data['id']}/",
            {"status": "ACTIVE"},
            format="json",
        )
        self.assertEqual(activated.status_code, status.HTTP_200_OK)
        unassigned = Neighborhood.objects.create(
            name="Sem região operacional",
            city=self.city.name,
            city_ref=self.city,
        )
        address = self.camera_payload()["address"]
        address["neighborhood_id"] = str(unassigned.id)

        response = self.client.patch(
            f"/api/flood_monitoring/cameras/{created.data['id']}/",
            {"address": address},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("coordinates", response.data["address"])

    def test_legacy_operational_camera_without_region_remains_editable(self):
        created = self.create_camera_as_admin(
            hls="https://cameras.example/legacy-regionless.m3u8"
        )
        camera = Camera.objects.get(pk=created.data["id"])
        camera.status = Camera.CameraStatus.ACTIVE
        camera.region = None
        camera.save(update_fields=["status", "region"])
        self.neighborhood.region = None
        self.neighborhood.save(update_fields=["region"])

        edited = self.client.patch(
            f"/api/flood_monitoring/cameras/{camera.id}/",
            {"description": "Câmera legada revisada"},
            format="json",
        )
        moved_offline = self.client.patch(
            f"/api/flood_monitoring/cameras/{camera.id}/",
            {"status": "OFFLINE"},
            format="json",
        )

        self.assertEqual(edited.status_code, status.HTTP_200_OK)
        self.assertEqual(moved_offline.status_code, status.HTTP_200_OK)
        self.assertEqual(moved_offline.data["status"], "OFFLINE")

    def test_legacy_regionless_camera_cannot_reactivate_after_becoming_inactive(self):
        created = self.create_camera_as_admin(
            hls="https://cameras.example/legacy-reactivation.m3u8"
        )
        camera = Camera.objects.get(pk=created.data["id"])
        camera.status = Camera.CameraStatus.ACTIVE
        camera.region = None
        camera.save(update_fields=["status", "region"])
        self.neighborhood.region = None
        self.neighborhood.save(update_fields=["region"])

        deactivated = self.client.patch(
            f"/api/flood_monitoring/cameras/{camera.id}/",
            {"status": "INACTIVE"},
            format="json",
        )
        reactivated = self.client.patch(
            f"/api/flood_monitoring/cameras/{camera.id}/",
            {"status": "ACTIVE"},
            format="json",
        )

        self.assertEqual(deactivated.status_code, status.HTTP_200_OK)
        self.assertEqual(reactivated.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("status", reactivated.data)

    def test_offline_camera_keeps_playable_sources_but_exposes_no_preview_analysis(self):
        created = self.create_camera_as_admin()
        camera = Camera.objects.get(pk=created.data["id"])
        camera.status = Camera.CameraStatus.OFFLINE
        camera.save(update_fields=["status"])

        snapshot = camera.operational_snapshot
        snapshot.analysis_status = snapshot.AnalysisStatus.AVAILABLE
        snapshot.classification = snapshot.CameraClassification.FLOOD_INDICATION
        snapshot.prob_normal, snapshot.prob_medium, snapshot.prob_flooded = 5, 5, 90
        snapshot.confidence, snapshot.frames = 90, 3
        snapshot.model_status, snapshot.model_version = snapshot.ModelStatus.READY, "model-v1"
        snapshot.analyzed_at = timezone.now()
        snapshot.save()

        self.client.force_authenticate(user=None)
        detail = self.client.get(f"/api/flood_monitoring/cameras/{camera.id}/")

        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.data["status"], "OFFLINE")
        self.assertEqual(
            detail.data["video_hls"], "https://cameras.example/stream.m3u8"
        )
        self.assertEqual(detail.data["preview_url"], detail.data["video_hls"])
        self.assertEqual(detail.data["operational"]["analysis"]["status"], "NOT_ANALYZED")
        self.assertIsNone(detail.data["operational"]["analysis"]["probabilities"])

    def test_public_paginated_list_includes_offline_as_active_without_analysis(self):
        created = self.create_camera_as_admin(
            hls="https://cameras.example/offline-list.m3u8"
        )
        offline = Camera.objects.get(pk=created.data["id"])
        offline.status = Camera.CameraStatus.OFFLINE
        offline.save(update_fields=["status"])
        snapshot = offline.operational_snapshot
        snapshot.analysis_status = snapshot.AnalysisStatus.AVAILABLE
        snapshot.classification = snapshot.CameraClassification.FLOOD_INDICATION
        snapshot.prob_normal, snapshot.prob_medium, snapshot.prob_flooded = 5, 5, 90
        snapshot.confidence, snapshot.frames = 90, 3
        snapshot.model_status, snapshot.model_version = snapshot.ModelStatus.READY, "old-model"
        snapshot.analyzed_at = timezone.now()
        snapshot.save()

        self.client.force_authenticate(user=None)
        response = self.client.get(
            "/api/flood_monitoring/cameras/",
            {
                "administrative_status": "ACTIVE",
                "analysis_status": "NOT_ANALYZED",
                "search": offline.description,
                "page_size": 1,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["count"], 1)
        self.assertEqual(len(response.data["results"]), 1)
        item = response.data["results"][0]
        self.assertEqual(item["id"], str(offline.id))
        self.assertEqual(item["status"], "OFFLINE")
        self.assertEqual(item["administrative_status"], "ACTIVE")
        self.assertEqual(item["preview_url"], offline.video_hls)
        self.assertNotIn("video_hls", item)
        self.assertNotIn("video_embed", item)
        self.assertEqual(item["operational"]["analysis"]["status"], "NOT_ANALYZED")
        self.assertIsNone(item["operational"]["analysis"]["classification"])
        self.assertIsNone(item["operational"]["analysis"]["probabilities"])

        classified = self.client.get(
            "/api/flood_monitoring/cameras/",
            {"classification": "FLOOD_INDICATION", "page_size": 100},
        )
        classified_ids = {item["id"] for item in classified.data["results"]}
        self.assertNotIn(str(offline.id), classified_ids)

    @patch("core.flood_camera_monitoring.presentation.camera_views.cache_delete")
    def test_active_to_offline_invalidates_predict_all_cache(self, cache_delete):
        created = self.create_camera_as_admin()
        camera = Camera.objects.get(pk=created.data["id"])
        camera.status = Camera.CameraStatus.ACTIVE
        camera.save(update_fields=["status"])

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                f"/api/flood_monitoring/cameras/{camera.id}/",
                {"status": "OFFLINE"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        cache_delete.assert_called_once_with("flood:predict_all")

    @patch(
        "core.flood_camera_monitoring.presentation.camera_views.cache_delete",
        side_effect=RuntimeError("redis unavailable"),
    )
    def test_cache_failure_does_not_fail_active_to_offline_patch(self, cache_delete):
        created = self.create_camera_as_admin()
        camera = Camera.objects.get(pk=created.data["id"])
        camera.status = Camera.CameraStatus.ACTIVE
        camera.save(update_fields=["status"])

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                f"/api/flood_monitoring/cameras/{camera.id}/",
                {"status": "OFFLINE"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        cache_delete.assert_called_once_with("flood:predict_all")

    @override_settings(FLOOD_ANALYSIS_STALE_SECONDS=600)
    def test_stale_preserves_valid_result_but_fallback_nulls_it(self):
        created = self.create_camera_as_admin()
        camera = Camera.objects.get(pk=created.data["id"])
        camera.status = Camera.CameraStatus.ACTIVE
        camera.save(update_fields=["status"])
        snapshot = camera.operational_snapshot
        snapshot.analysis_status = snapshot.AnalysisStatus.AVAILABLE
        snapshot.classification = snapshot.CameraClassification.FLOOD_INDICATION
        snapshot.prob_normal = 10.0
        snapshot.prob_medium = 20.0
        snapshot.prob_flooded = 70.0
        snapshot.confidence = 70.0
        snapshot.frames = 3
        snapshot.analysis_started_at = timezone.now() - timedelta(seconds=611)
        snapshot.analyzed_at = timezone.now() - timedelta(seconds=601)
        snapshot.model_status = snapshot.ModelStatus.READY
        snapshot.model_version = "camera-model-v1"
        snapshot.save()

        detail = self.client.get(f"/api/flood_monitoring/cameras/{camera.id}/")
        analysis = detail.data["operational"]["analysis"]
        self.assertEqual(analysis["status"], "STALE")
        self.assertEqual(analysis["classification"], "FLOOD_INDICATION")
        self.assertEqual(analysis["probabilities"]["flooded"], 70.0)
        self.assertEqual(analysis["frames"], 3)

        snapshot.model_status = snapshot.ModelStatus.FALLBACK
        snapshot.save()
        detail = self.client.get(f"/api/flood_monitoring/cameras/{camera.id}/")
        analysis = detail.data["operational"]["analysis"]
        self.assertIsNone(analysis["classification"])
        self.assertIsNone(analysis["probabilities"])
        self.assertIsNone(analysis["confidence"])
        self.assertIsNone(analysis["frames"])

    def test_predict_all_reads_snapshot_and_nulls_unavailable_result(self):
        created = self.create_camera_as_admin()
        camera = Camera.objects.get(pk=created.data["id"])
        camera.status = Camera.CameraStatus.ACTIVE
        camera.save(update_fields=["status"])
        snapshot = camera.operational_snapshot
        snapshot.analysis_status = snapshot.AnalysisStatus.NO_FRAME
        snapshot.frames = 0
        snapshot.model_status = snapshot.ModelStatus.READY
        snapshot.model_version = "camera-model-v1"
        snapshot.save()

        response = self.client.get("/api/flood_monitoring/predict/all/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item = next(
            item
            for item in response.data["results"]
            if item["camera"]["id"] == str(camera.id)
        )
        self.assertIsNone(item["is_flooded"])
        self.assertIsNone(item["confidence"])
        self.assertIsNone(item["medium"])
        self.assertEqual(
            item["probabilities"],
            {"normal": None, "medium": None, "flooded": None},
        )
        self.assertEqual(item["meta"]["status"], "NO_FRAME")

    def test_list_supports_search_filters_and_operational_priority_before_paging(self):
        normal_response = self.create_camera_as_admin(
            hls="https://cameras.example/normal.m3u8"
        )
        normal = Camera.objects.get(pk=normal_response.data["id"])
        normal.status = Camera.CameraStatus.ACTIVE
        normal.save(update_fields=["status"])
        normal_snapshot = normal.operational_snapshot
        normal_snapshot.analysis_status = normal_snapshot.AnalysisStatus.AVAILABLE
        normal_snapshot.classification = (
            normal_snapshot.CameraClassification.NO_INDICATION
        )
        normal_snapshot.prob_normal = 90.0
        normal_snapshot.prob_medium = 5.0
        normal_snapshot.prob_flooded = 5.0
        normal_snapshot.confidence = 90.0
        normal_snapshot.frames = 2
        normal_snapshot.analyzed_at = timezone.now()
        normal_snapshot.model_status = normal_snapshot.ModelStatus.READY
        normal_snapshot.model_version = "camera-model-v1"
        normal_snapshot.save()

        flood_payload = self.camera_payload(
            hls="https://cameras.example/flood.m3u8"
        )
        flood_payload["description"] = "Câmera prioritária"
        flood_response = self.client.post(
            "/api/flood_monitoring/cameras/", flood_payload, format="json"
        )
        flood = Camera.objects.get(pk=flood_response.data["id"])
        flood.status = Camera.CameraStatus.ACTIVE
        flood.save(update_fields=["status"])
        flood_snapshot = flood.operational_snapshot
        flood_snapshot.analysis_status = flood_snapshot.AnalysisStatus.AVAILABLE
        flood_snapshot.classification = (
            flood_snapshot.CameraClassification.FLOOD_INDICATION
        )
        flood_snapshot.prob_normal = 5.0
        flood_snapshot.prob_medium = 5.0
        flood_snapshot.prob_flooded = 90.0
        flood_snapshot.confidence = 90.0
        flood_snapshot.frames = 2
        flood_snapshot.analyzed_at = timezone.now()
        flood_snapshot.model_status = flood_snapshot.ModelStatus.READY
        flood_snapshot.model_version = "camera-model-v1"
        flood_snapshot.save()

        first_page = self.client.get(
            "/api/flood_monitoring/cameras/?page_size=1"
        )
        self.assertEqual(first_page.data["results"][0]["id"], str(flood.id))

        filtered = self.client.get(
            "/api/flood_monitoring/cameras/",
            {
                "classification": "FLOOD_INDICATION",
                "region_id": str(self.region.id),
                "search": "prioritária",
            },
        )
        self.assertEqual(filtered.data["count"], 1)
        self.assertEqual(filtered.data["results"][0]["id"], str(flood.id))


class AddressingLookupApiTests(APITestCase):
    def test_cities_and_city_id_region_lookup_have_additive_contract(self):
        initial_city_count = City.objects.count()
        city = City.objects.create(name="Cidade Lookup Teste")
        region = Region.objects.create(
            name="Centro", city=city.name, city_ref=city
        )
        neighborhood = Neighborhood.objects.create(
            name="América",
            city=city.name,
            city_ref=city,
            region=region,
        )

        cities = self.client.get("/api/addressing/cities/")
        self.assertEqual(cities.status_code, status.HTTP_200_OK)
        self.assertEqual(cities.data["count"], initial_city_count + 1)
        self.assertIn(
            {"id": str(city.id), "name": city.name}, cities.data["results"]
        )

        regions = self.client.get(
            "/api/addressing/regions-neighborhoods/", {"city_id": str(city.id)}
        )
        self.assertEqual(regions.status_code, status.HTTP_200_OK)
        self.assertEqual(regions.data["city"], city.name)
        self.assertEqual(regions.data["city_id"], str(city.id))
        self.assertEqual(regions.data["regions"][0]["city_id"], str(city.id))
        self.assertEqual(
            regions.data["regions"][0]["neighborhoods"][0]["id"],
            str(neighborhood.id),
        )

        invalid = self.client.get(
            "/api/addressing/regions-neighborhoods/",
            {"city": city.name, "city_id": str(city.id)},
        )
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(invalid.data["code"], "invalid_filter")
