import hashlib
import json
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.core.management import call_command
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.addressing.models import City, GeodataDataset, Neighborhood, ReferenceBaseRelease, ReferenceBaseReleaseRecord
from core.addressing.reference_releases import PARTITIONS, build_payload, canonicalize_reference_ids, import_release, promote_release, read_archive, write_archive
from core.users.infra.models import User


class ReferenceBaseReleaseTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            username="territorial-admin", email="territorial-admin@example.test",
            is_staff=True, is_active=True,
        )

    def _neighborhood_payload(self, *, city_id, dataset_id, neighborhood_id, revision):
        payload = {name: [] for name in PARTITIONS}
        payload["cities"] = [{
            "id": city_id, "name": f"Cidade {revision}", "official_code": "4200000",
            "normalized_name": "cidade", "is_active": True, "geometry": None,
            "geometry_metadata": {}, "source_record_id": f"city-{revision}",
            "geometry_dataset_id": None,
        }]
        payload["datasets"] = [{
            "id": dataset_id, "city_id": city_id, "kind": "neighborhood_boundary",
            "authority": "Prefeitura", "title": f"Bairros {revision}",
            "source_url": f"https://example.test/{revision}", "license_name": "Licenca",
            "license_url": "https://example.test/license", "source_version": revision,
            "published_at": None, "retrieved_at": "2026-01-01T00:00:00+00:00",
            "sha256": revision[0] * 64, "source_crs": "EPSG:4326", "metadata": {},
        }]
        payload["neighborhoods"] = [{
            "id": neighborhood_id, "name": f"Bairro {revision}", "official_code": "N-1",
            "normalized_name": "bairro", "city": f"Cidade {revision}", "city_ref_id": city_id,
            "region_id": None, "props": {}, "geometry": None, "geometry_metadata": {},
            "dataset_id": dataset_id, "source_record_id": f"neighborhood-{revision}",
            "is_active": True, "area_km2": None,
        }]
        return payload

    def test_uuidv5_ids_are_stable_between_dataset_editions(self):
        first = self._neighborhood_payload(
            city_id="00000000-0000-0000-0000-000000000101",
            dataset_id="00000000-0000-0000-0000-000000000102",
            neighborhood_id="00000000-0000-0000-0000-000000000103", revision="a",
        )
        second = self._neighborhood_payload(
            city_id="00000000-0000-0000-0000-000000000201",
            dataset_id="00000000-0000-0000-0000-000000000202",
            neighborhood_id="00000000-0000-0000-0000-000000000203", revision="b",
        )
        with TemporaryDirectory() as directory:
            first_path = Path(directory) / "a.tar.gz"; second_path = Path(directory) / "b.tar.gz"
            write_archive(first_path, revision="edition-a", payload=first)
            write_archive(second_path, revision="edition-b", payload=second)
            _m1, p1, _s1 = read_archive(first_path)
            _m2, p2, _s2 = read_archive(second_path)
        self.assertEqual(p1["cities"][0]["id"], p2["cities"][0]["id"])
        self.assertEqual(p1["neighborhoods"][0]["id"], p2["neighborhoods"][0]["id"])
        self.assertNotEqual(p1["neighborhoods"][0]["legacy_source_id"], p2["neighborhoods"][0]["legacy_source_id"])
        self.assertEqual(p1["neighborhoods"][0]["identity_key"], p2["neighborhoods"][0]["identity_key"])

    def test_official_codes_are_scoped_by_authority(self):
        first = self._neighborhood_payload(
            city_id="00000000-0000-0000-0000-000000000111",
            dataset_id="00000000-0000-0000-0000-000000000112",
            neighborhood_id="00000000-0000-0000-0000-000000000113", revision="a",
        )
        second = self._neighborhood_payload(
            city_id="00000000-0000-0000-0000-000000000211",
            dataset_id="00000000-0000-0000-0000-000000000212",
            neighborhood_id="00000000-0000-0000-0000-000000000213", revision="b",
        )
        first["cities"][0]["authority"] = "Prefeitura"
        second["cities"][0]["authority"] = "Outra autoridade"
        second["datasets"][0]["authority"] = "Outra autoridade"
        first = canonicalize_reference_ids(first)
        second = canonicalize_reference_ids(second)
        self.assertNotEqual(first["cities"][0]["id"], second["cities"][0]["id"])
        self.assertNotEqual(first["neighborhoods"][0]["id"], second["neighborhoods"][0]["id"])

    def test_city_and_children_do_not_depend_on_unrelated_dataset_composition(self):
        first = self._neighborhood_payload(
            city_id="00000000-0000-0000-0000-000000000121",
            dataset_id="00000000-0000-0000-0000-000000000122",
            neighborhood_id="00000000-0000-0000-0000-000000000123", revision="a",
        )
        second = json.loads(json.dumps(first))
        second["datasets"].append({
            "id": "00000000-0000-0000-0000-000000000124",
            "city_id": first["cities"][0]["id"], "kind": "street",
            "authority": "Autoridade de camada alheia",
        })
        first_canonical = canonicalize_reference_ids(first)
        second_canonical = canonicalize_reference_ids(second)
        self.assertEqual(first_canonical["cities"][0]["id"], second_canonical["cities"][0]["id"])
        self.assertEqual(
            first_canonical["neighborhoods"][0]["id"],
            second_canonical["neighborhoods"][0]["id"],
        )

    def test_address_source_identity_is_scoped_by_authority_and_city(self):
        payload = {name: [] for name in PARTITIONS}
        for suffix, official in (("1", "100"), ("2", "200")):
            city_id = f"00000000-0000-0000-0000-0000000003{suffix}1"
            dataset_id = f"00000000-0000-0000-0000-0000000003{suffix}2"
            address_id = f"00000000-0000-0000-0000-0000000003{suffix}3"
            payload["cities"].append({
                "id": city_id, "name": f"Cidade {suffix}", "official_code": official,
                "source_record_id": f"city-{suffix}", "geometry_dataset_id": None,
            })
            payload["datasets"].append({
                "id": dataset_id, "city_id": city_id, "kind": "address_point",
                "authority": "Autoridade compartilhada",
            })
            payload["address_references"].append({
                "id": address_id, "city_id": city_id, "dataset_id": dataset_id,
                "source_record_id": "mesmo-id-na-fonte",
            })
        canonical = canonicalize_reference_ids(payload)
        self.assertNotEqual(
            canonical["address_references"][0]["id"],
            canonical["address_references"][1]["id"],
        )

    def test_city_authority_survives_promotion_and_reexport(self):
        payload = self._neighborhood_payload(
            city_id="00000000-0000-0000-0000-000000000131",
            dataset_id="00000000-0000-0000-0000-000000000132",
            neighborhood_id="00000000-0000-0000-0000-000000000133", revision="authority",
        )
        payload["cities"][0]["authority"] = "Autoridade municipal estavel"
        payload["cities"][0]["geometry_dataset_id"] = payload["datasets"][0]["id"]
        payload["datasets"][0]["authority"] = "Autoridade geometrica diferente"
        with TemporaryDirectory() as directory:
            archive = Path(directory) / "authority.tar.gz"
            write_archive(archive, revision="authority-release", payload=payload)
            _manifest, original, _checksum = read_archive(archive)
            call_command("import_reference_base_release", str(archive))
        promote_release(
            "authority-release", expected_revision="none",
            justification="Teste de persistencia da autoridade", actor=self.admin,
        )
        reexported = canonicalize_reference_ids(build_payload())
        reexported_city = next(
            city for city in reexported["cities"]
            if city["official_code"] == original["cities"][0]["official_code"]
        )
        self.assertEqual(original["cities"][0]["id"], reexported_city["id"])
        self.assertEqual(
            reexported_city["authority"], "autoridade municipal estavel"
        )
        reexported_neighborhood = next(
            neighborhood for neighborhood in reexported["neighborhoods"]
            if neighborhood["official_code"] == original["neighborhoods"][0]["official_code"]
            and neighborhood["city_ref_id"] == reexported_city["id"]
        )
        self.assertEqual(original["neighborhoods"][0]["id"], reexported_neighborhood["id"])

    def test_rollback_deactivates_records_exclusive_to_current_release(self):
        first = self._neighborhood_payload(
            city_id="00000000-0000-0000-0000-000000000301",
            dataset_id="00000000-0000-0000-0000-000000000302",
            neighborhood_id="00000000-0000-0000-0000-000000000303", revision="a",
        )
        second = self._neighborhood_payload(
            city_id="00000000-0000-0000-0000-000000000401",
            dataset_id="00000000-0000-0000-0000-000000000402",
            neighborhood_id="00000000-0000-0000-0000-000000000403", revision="b",
        )
        extra = dict(second["neighborhoods"][0])
        extra.update({
            "id": "00000000-0000-0000-0000-000000000404",
            "official_code": "N-2", "source_record_id": "exclusive-b", "name": "Exclusivo B",
        })
        second["neighborhoods"].append(extra)
        second["cities"].append({
            "id": "00000000-0000-0000-0000-000000000405",
            "name": "Cidade exclusiva B", "official_code": "4300000",
            "normalized_name": "cidade exclusiva b", "is_active": True,
            "geometry": None, "geometry_metadata": {}, "source_record_id": "city-exclusive-b",
            "geometry_dataset_id": None,
        })
        with TemporaryDirectory() as directory:
            first_path = Path(directory) / "rollback-a.tar.gz"
            second_path = Path(directory) / "rollback-b.tar.gz"
            write_archive(first_path, revision="rollback-a", payload=first)
            write_archive(second_path, revision="rollback-b", payload=second, expected_previous_revision="rollback-a")
            call_command("import_reference_base_release", str(first_path))
            call_command("import_reference_base_release", str(second_path))
            _manifest, canonical_second, _sha = read_archive(second_path)
        promote_release("rollback-a", expected_revision="none", justification="Ativacao A", actor=self.admin)
        self.assertEqual(
            ReferenceBaseRelease.objects.get(revision="rollback-a").audit_entries.get(action="promote").actor,
            self.admin,
        )
        promote_release("rollback-b", expected_revision="rollback-a", justification="Ativacao B", actor=self.admin)
        exclusive_id = next(item["id"] for item in canonical_second["neighborhoods"] if item["official_code"] == "N-2")
        exclusive_city_id = next(item["id"] for item in canonical_second["cities"] if item["official_code"] == "4300000")
        self.assertTrue(Neighborhood.objects.get(pk=exclusive_id).is_active)
        self.assertTrue(City.objects.get(pk=exclusive_city_id).is_active)
        promote_release("rollback-a", expected_revision="rollback-b", justification="Rollback A", actor=self.admin, rollback=True)
        self.assertFalse(Neighborhood.objects.get(pk=exclusive_id).is_active)
        self.assertFalse(City.objects.get(pk=exclusive_city_id).is_active)
        returned_ids = {item["id"] for item in APIClient().get("/api/addressing/cities/").data["results"]}
        self.assertNotIn(exclusive_city_id, returned_ids)

    def test_package_member_and_record_count_limits(self):
        payload = {name: [] for name in PARTITIONS}
        payload["cities"] = [{
            "id": "00000000-0000-0000-0000-000000000501", "name": "Cidade limite",
            "official_code": "500", "normalized_name": "cidade limite", "is_active": True,
            "geometry": None, "geometry_metadata": {}, "source_record_id": "500",
            "geometry_dataset_id": None,
        }]
        with TemporaryDirectory() as directory:
            archive = Path(directory) / "limits.tar.gz"
            with mock.patch("core.addressing.reference_releases.MAX_RECORDS_PER_PARTITION", 0):
                with self.assertRaises(CommandError):
                    write_archive(archive, revision="limits", payload=payload)
            write_archive(archive, revision="limits", payload=payload)
            with mock.patch("core.addressing.reference_releases.MAX_ARCHIVE_MEMBERS", 1):
                with self.assertRaises(CommandError):
                    read_archive(archive, load_payload=False)
    def test_archive_is_deterministic_and_dry_run_writes_nothing(self):
        payload = {name: [] for name in PARTITIONS}
        payload["cities"] = [{
            "id": "00000000-0000-0000-0000-000000000001", "name": "Cidade sintetica",
            "official_code": "1", "normalized_name": "cidade sintetica", "is_active": True,
            "geometry": None, "geometry_metadata": {}, "source_record_id": "1",
            "geometry_dataset_id": None,
        }]
        with TemporaryDirectory() as directory:
            first = Path(directory) / "first.tar.gz"
            second = Path(directory) / "second.tar.gz"
            write_archive(first, revision="synthetic-1", payload=payload)
            write_archive(second, revision="synthetic-1", payload=payload)
            self.assertEqual(hashlib.sha256(first.read_bytes()).digest(), hashlib.sha256(second.read_bytes()).digest())
            call_command("import_reference_base_release", str(first), dry_run=True)
        self.assertEqual(ReferenceBaseRelease.objects.count(), 0)

    def test_real_import_uses_partitioned_staging_rows(self):
        payload = {name: [] for name in PARTITIONS}
        payload["cities"] = [{
            "id": "00000000-0000-0000-0000-000000000002", "name": "Cidade staged",
            "official_code": "2", "normalized_name": "cidade staged", "is_active": True,
            "geometry": None, "geometry_metadata": {}, "source_record_id": "2",
            "geometry_dataset_id": None,
        }]
        with TemporaryDirectory() as directory:
            archive = Path(directory) / "release.tar.gz"
            manifest, _checksum = write_archive(archive, revision="synthetic-staged", payload=payload)
            call_command("import_reference_base_release", str(archive))
        release = ReferenceBaseRelease.objects.get(revision="synthetic-staged")
        self.assertEqual(ReferenceBaseReleaseRecord.objects.filter(release=release).count(), 1)
        self.assertEqual(release.records.get().partition, "cities")
        for field in ("created_at", "minimum_migration", "cities", "layers", "datasets"):
            self.assertIn(field, manifest)
        self.assertEqual(manifest["minimum_migration"], "addressing.0026")
        self.assertFalse(City.objects.filter(pk=payload["cities"][0]["id"]).exists())
        regular = get_user_model().objects.create_user(username="not-admin", is_staff=False)
        with self.assertRaisesMessage(CommandError, "administrador ativo"):
            promote_release(
                "synthetic-staged", expected_revision="none",
                justification="Nao deve promover", actor=regular,
            )
        regular.type = "admin"
        with self.assertRaisesMessage(CommandError, "administrador ativo"):
            promote_release(
                "synthetic-staged", expected_revision="none",
                justification="Mutacao apenas em memoria", actor=regular,
            )
        # O modelo configurado neste ambiente e django.contrib.auth.User e nao
        # possui o campo `type`; persiste-se o equivalente administrativo
        # suportado por ele. Em instalacoes cujo AUTH_USER_MODEL possui `type`,
        # a mesma recarga valida o valor persistido `admin`.
        regular.is_staff = True
        regular.save(update_fields=["is_staff"])
        promoted = promote_release(
            "synthetic-staged", expected_revision="none",
            justification="Administrador persistido", actor=regular,
        )
        self.assertEqual(promoted.audit_entries.get(action="promote").actor, regular)

    def test_v2_status_is_explicit_when_no_release_is_active(self):
        response = APIClient().get("/api/addressing/v2/status/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["reference_base_status"], "unavailable")
        self.assertIsNone(response.data["reference_base_revision"])

    def test_v2_translates_reference_query_aliases(self):
        city = City.objects.create(name="Cidade alias", is_active=True)
        GeodataDataset.objects.create(
            city=city, kind="address_point", authority="Teste", title="Enderecos",
            source_url="https://example.test", license_name="Teste", source_version="1",
            retrieved_at=timezone.now(), sha256="a" * 64, source_crs="EPSG:4326", status="active",
        )
        client = APIClient()
        client.force_authenticate(User.objects.create(name="Operador alias", email="alias@example.test"))
        response = client.get("/api/addressing/v2/autocomplete/", {
            "kind": "address", "q": "Rua", "reference_city_id": str(city.id),
            "reference_street_id": "00000000-0000-0000-0000-000000000099",
        })
        self.assertEqual(response.status_code, 400)

    def test_partial_promotion_preserves_cities_and_layers_outside_scope(self):
        city_a = City.objects.create(name="Cidade A", official_code="A", is_active=True)
        city_b = City.objects.create(name="Cidade B", official_code="B", is_active=True)
        old_a = GeodataDataset.objects.create(
            city=city_a, kind="neighborhood_boundary", authority="Teste", title="A antiga",
            source_url="https://example.test/a-old", license_name="Teste", source_version="old",
            retrieved_at=timezone.now(), sha256="1" * 64, source_crs="EPSG:4326", status="active",
        )
        old_b = GeodataDataset.objects.create(
            city=city_b, kind="neighborhood_boundary", authority="Teste", title="B atual",
            source_url="https://example.test/b", license_name="Teste", source_version="1",
            retrieved_at=timezone.now(), sha256="2" * 64, source_crs="EPSG:4326", status="active",
        )
        neighborhood_a = Neighborhood.objects.create(name="A", city=city_a.name, city_ref=city_a, dataset=old_a)
        neighborhood_b = Neighborhood.objects.create(name="B", city=city_b.name, city_ref=city_b, dataset=old_b)
        new_dataset_id = "00000000-0000-0000-0000-000000000010"
        payload = {name: [] for name in PARTITIONS}
        payload["cities"] = [{
            "id": str(city_a.id), "name": city_a.name, "official_code": city_a.official_code,
            "normalized_name": "cidade a", "is_active": True, "geometry": None,
            "geometry_metadata": {}, "source_record_id": "A", "geometry_dataset_id": None,
        }]
        payload["datasets"] = [{
            "id": new_dataset_id, "city_id": str(city_a.id), "kind": "neighborhood_boundary",
            "authority": "Teste", "title": "A nova", "source_url": "https://example.test/a-new",
            "license_name": "Teste", "license_url": "", "source_version": "new",
            "published_at": None, "retrieved_at": timezone.now().isoformat(), "sha256": "3" * 64,
            "source_crs": "EPSG:4326", "metadata": {},
        }]
        with TemporaryDirectory() as directory:
            archive = Path(directory) / "partial.tar.gz"
            write_archive(archive, revision="partial-a", payload=payload)
            call_command("import_reference_base_release", str(archive))
        call_command(
            "promote_reference_base_release", "partial-a",
            expected_revision="none", justification="Teste sintetico",
            actor_id=str(self.admin.pk),
        )
        old_a.refresh_from_db(); old_b.refresh_from_db()
        neighborhood_a.refresh_from_db(); neighborhood_b.refresh_from_db()
        self.assertEqual(old_a.status, "superseded")
        self.assertEqual(old_b.status, "active")
        self.assertFalse(neighborhood_a.is_active)
        self.assertTrue(neighborhood_b.is_active)


class ConcurrentReferenceImportTests(TransactionTestCase):
    reset_sequences = True

    def test_equivalent_concurrent_imports_return_one_release(self):
        payload = {name: [] for name in PARTITIONS}
        payload["cities"] = [{
            "id": "00000000-0000-0000-0000-000000000601", "name": "Cidade concorrente",
            "official_code": "600", "normalized_name": "cidade concorrente", "is_active": True,
            "geometry": None, "geometry_metadata": {}, "source_record_id": "600",
            "geometry_dataset_id": None,
        }]
        with TemporaryDirectory() as directory:
            archive = Path(directory) / "concurrent.tar.gz"
            write_archive(archive, revision="concurrent-import", payload=payload)
            barrier = threading.Barrier(2)
            release_ids, errors = [], []

            def worker():
                close_old_connections()
                try:
                    barrier.wait()
                    release, _manifest, _sha = import_release(archive)
                    release_ids.append(release.id)
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)
                finally:
                    close_old_connections()

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(release_ids)), 1)
        self.assertEqual(ReferenceBaseRelease.objects.filter(revision="concurrent-import").count(), 1)
