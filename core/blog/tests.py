from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from core.blog.infra.models import Post


class PostReferenceMigrationTests(TransactionTestCase):
    migrate_from = [("blog", "0004_post_banner_image_post_content_image")]
    migrate_to = [("blog", "0005_post_reference")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        OldPost = old_apps.get_model("blog", "Post")
        self.post_id = OldPost.objects.create(
            title="Post anterior à referência",
            subject="Histórico",
        ).pk

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_posts_receive_empty_reference_fields(self):
        MigratedPost = self.apps.get_model("blog", "Post")
        post = MigratedPost.objects.get(pk=self.post_id)

        self.assertEqual(post.reference_title, "")
        self.assertEqual(post.reference_url, "")


class PostReferenceValidationTests(TestCase):
    def test_reference_defaults_are_backwards_compatible(self):
        post = Post.objects.create(title="Post existente", subject="Histórico")

        self.assertEqual(post.reference_title, "")
        self.assertEqual(post.reference_url, "")

    def test_reference_title_and_url_must_be_informed_together(self):
        incomplete_references = (
            {"reference_title": "Defesa Civil", "reference_url": ""},
            {"reference_title": "", "reference_url": "https://example.com/fonte"},
        )

        for reference in incomplete_references:
            with self.subTest(reference=reference):
                post = Post(title="Notícia", subject="Dicas", **reference)
                with self.assertRaisesMessage(
                    ValidationError,
                    "O nome e a URL da referência devem ser informados juntos.",
                ):
                    post.full_clean()

    def test_complete_reference_is_valid(self):
        post = Post(
            title="Notícia",
            subject="Dicas",
            reference_title="Defesa Civil de Joinville",
            reference_url="https://www.joinville.sc.gov.br/",
        )

        post.full_clean()


class PostReferenceApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.post = Post.objects.create(
            title="Alerta de chuva",
            subject="Dicas",
            reference_title="Defesa Civil de Joinville",
            reference_url="https://www.joinville.sc.gov.br/",
        )
        self.post_without_reference = Post.objects.create(
            title="Histórico das enchentes",
            subject="Histórico",
        )

    def test_list_exposes_reference_fields(self):
        response = self.client.get("/api/blog/")

        self.assertEqual(response.status_code, 200)
        posts = {item["id"]: item for item in response.data["results"]}
        self.assertEqual(
            posts[str(self.post.id)]["reference_title"],
            "Defesa Civil de Joinville",
        )
        self.assertEqual(
            posts[str(self.post.id)]["reference_url"],
            "https://www.joinville.sc.gov.br/",
        )
        self.assertEqual(
            posts[str(self.post_without_reference.id)]["reference_title"], ""
        )
        self.assertEqual(
            posts[str(self.post_without_reference.id)]["reference_url"], ""
        )

    def test_detail_exposes_reference_fields(self):
        response = self.client.get(f"/api/blog/{self.post.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["reference_title"], "Defesa Civil de Joinville")
        self.assertEqual(
            response.data["reference_url"],
            "https://www.joinville.sc.gov.br/",
        )
