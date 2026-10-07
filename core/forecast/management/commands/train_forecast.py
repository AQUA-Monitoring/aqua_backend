from django.core.management.base import BaseCommand

from core.forecast.services import train_forecast


class Command(BaseCommand):
    help = "Constroi o dataset balanceado, treina o modelo e salva os artefatos."

    def add_arguments(self, parser):
        parser.add_argument(
            "--backfill",
            action="store_true",
            help="Executa o backfill de contextos climaticos ausentes antes do treino.",
        )

    def handle(self, *args, **options):
        if options["backfill"]:
            from core.forecast.management.commands.backfill_forecast_contexts import Command as Backfill
            Backfill().handle()
        metrics = train_forecast()
        self.stdout.write(self.style.SUCCESS("Treino concluido. Metricas:"))
        for k, v in metrics.items():
            self.stdout.write(f"  {k}: {v}")