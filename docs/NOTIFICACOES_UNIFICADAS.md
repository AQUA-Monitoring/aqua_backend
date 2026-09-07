# Notificações unificadas

O domínio `core.notifications` publica eventos originados por pontos de alagamento,
confirmações de câmera e comunicados administrativos. O público é a união das
preferências de região/bairro com locais salvos dentro do raio configurado; cada
dispositivo recebe no máximo uma entrega por evento.

## Operação

- Configure `WEB_PUSH_ENABLED=1` e as três variáveis `WEB_PUSH_VAPID_*`.
- `UNIFIED_NOTIFICATIONS_ENABLED=1` habilita o novo fan-out. Use `0` durante um
  rollback para manter apenas o fluxo legado de câmeras.
- Mantenha worker e beat Celery ativos. O beat recupera entregas temporariamente
  falhas a cada cinco minutos.
- Aplique `python manage.py migrate` antes de habilitar a interface nova. A
  migração cria eventos históricos das publicações de câmera sem reenviá-los.

## Diagnóstico e reprocessamento

Consulte `NotificationEvent`, `NotificationEventAudit` e `PushDelivery` no Django
Admin. Entregas `failed` abaixo de `WEB_PUSH_MAX_ATTEMPTS` são retomadas pelo beat;
o task `core.notifications.tasks.recover_failed_push_deliveries_task` também pode
ser disparado manualmente. Endpoints com resposta 404/410 são expirados e não
voltam para a fila.

Locais salvos são privados: APIs administrativas retornam somente o tamanho do
público calculado, nunca coordenadas individuais.
