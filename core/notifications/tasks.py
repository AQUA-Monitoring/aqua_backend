from celery import shared_task

from .adapters import PushDeliveryError
from .services import deliver_push, recover_failed_deliveries


@shared_task(
    bind=True,
    autoretry_for=(PushDeliveryError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def deliver_push_task(self, delivery_id: str):
    result = deliver_push(delivery_id)
    if result == "failed":
        raise PushDeliveryError("Falha temporária na entrega Web Push")
    return result


@shared_task
def deliver_push_batch_task(delivery_ids: list[str]):
    for delivery_id in delivery_ids:
        deliver_push_task.delay(delivery_id)
    return len(delivery_ids)


@shared_task
def recover_failed_push_deliveries_task():
    return recover_failed_deliveries()
