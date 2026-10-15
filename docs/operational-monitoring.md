# Monitoramento operacional e erros públicos

## Contrato de erro

Respostas `/api/` com falha incluem `code`, `message`, `detail` (compatibilidade),
`request_id` e `retryable`. O mesmo identificador aparece em `X-Request-ID`.
Validações 400/422 incluem `errors` para os campos. Corpos internos de 5xx,
exceções, SQL e respostas HTML do servidor não são exibidos ao usuário.

| Código | Significado / ação |
| --- | --- |
| VALIDATION_ERROR | Corrigir campos antes de reenviar |
| AUTHENTICATION_REQUIRED | Entrar novamente |
| PERMISSION_DENIED | Solicitar acesso à administração |
| RESOURCE_NOT_FOUND | Atualizar a consulta; o recurso pode ter sido removido |
| RESOURCE_CONFLICT | Atualizar os dados antes de salvar |
| RATE_LIMITED | Aguardar antes de repetir |
| SERVICE_UNAVAILABLE | Serviço temporariamente indisponível; tentar mais tarde |
| NETWORK_ERROR / REQUEST_TIMEOUT | Frontend sem conexão ou resposta no prazo |

O frontend não mostra mensagens técnicas do Axios, HTML de proxy ou detalhes do
HLS. Uma falha ao consultar uma câmera não é apresentada como câmera inexistente.
O prazo padrão das chamadas Axios é de 20 segundos.

## Monitor independente

Inclua `docker-compose.operations.yml` depois dos manifests de produção/release.
O serviço `operations-monitor` usa `python manage.py monitor_services`, não
Celery, e não depende de Redis saudável para iniciar, guardar estado ou enviar
email. `ops_state` persiste destinatários, incidentes, deduplicação e heartbeat;
`ops_events` transporta registros mínimos de 5xx, sem conteúdo das requisições.
O monitor lê o espaço do volume `/srv` através de um mount somente leitura.

Verificações a cada 60 segundos, com confirmação em duas verificações:

- saúde da API, PostgreSQL e Redis;
- disponibilidade do catálogo de câmeras e frontend;
- menos de 10% ou 2 GiB livres no disco;
- pelo menos metade das transmissões ativas indisponíveis;
- menos da metade das câmeras ativas analisadas nos últimos 30 minutos;
- erros 5xx recentes na API ou nas câmeras;
- leitura dos registros de erros.

A idade da análise pode ser configurada com `OPS_CAMERA_STALE_MINUTES`.
Mensagens são enviadas individualmente para contas ativas com `type=admin`,
`is_staff` ou `is_superuser`. A lista é atualizada a cada ciclo; a última lista
válida é usada se o banco parar. Um primeiro início sem banco e sem lista salva
não consegue descobrir destinatários. Bootstrap com banco saudável é obrigatório.

Cada incidente envia um aviso e, se persistir, no máximo um lembrete por hora
por destinatário. A recuperação também exige duas verificações saudáveis e envia
um aviso. Falhas SMTP não são marcadas como entregues e são tentadas no próximo
ciclo. Os emails não compartilham a lista de destinatários nem incluem segredos.

## Email e ativação

Configure `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`,
`DEFAULT_FROM_EMAIL`, `EMAIL_USE_TLS` e `EMAIL_USE_SSL` no ambiente do monitor.
Não habilite TLS e SSL simultaneamente. Porta 587 normalmente usa STARTTLS;
porta 465 usa SSL. Valores dependem do provedor escolhido. Nunca commite senhas.
Sem host/remetente, o monitor registra incidentes e informa
`email_configured=false`; não alega ter enviado alertas.

```sh
python manage.py monitor_services --once --dry-run
```

Esse comando verifica serviços e grava o estado, sem enviar emails. Veja
`/var/lib/aqua-monitor/status.json` e logs do container para confirmar os checks,
quantidade de destinatários e configuração SMTP. O healthcheck do container
verifica que o último ciclo terminou há menos de 180 segundos.

## Resposta a incidentes

1. Consultar `/health/`, status do monitor e logs dos serviços.
2. Para disco cheio: medir volumes/imagens, preservar dados e backups; não apagar
   arquivos de PostgreSQL ou Redis para liberar espaço.
3. Para corrupção Redis: parar o serviço, preservar cópia integral e validar com
   `redis-check-aof`; reparos podem descartar comandos e exigem decisão explícita.
4. Confirmar recuperação da API, catálogo e análises após restaurar dependências.

Este monitor roda no mesmo host. Queda completa do host, da rede ou do próprio
monitor precisa de uma sonda externa independente; o healthcheck local sozinho
não consegue enviar um aviso nessa situação. Não há garantia de entrega ao
usuário final apenas porque o SMTP aceitou a mensagem.

A captura de evidências reserva 2 GiB livres antes de gravar um lote de imagens
(`FLOOD_EVIDENCE_MIN_FREE_BYTES`). Ao atingir o limite, a análise registra falha
em vez de consumir o espaço restante do banco. As evidências existentes são
preservadas; alterar os 90 dias de retenção requer uma decisão operacional.
