# Monitoramento adaptativo e controle de capacidade

A política `conservative` utiliza apenas observações válidas da própria câmera.
Intervalos após conclusão: NORMAL 300 s / 3 imagens; WATCH 120 s / 7 imagens;
CRITICAL 60 s / 7 imagens; RECOVERY 180 s / 7 imagens. A classificação e os
critérios de publicação de alertas permanecem independentes do agendamento.

## Configuração e piloto

Aplicar a migração `0022_monitoring_capacity` antes de executar a nova imagem.
Parar beat e drenar os workers antigos antes da troca: o protocolo de reserva
passou a manter o token até o fim da execução. Não misturar workers das duas
versões durante a atualização.

Configurar igualmente web, worker, flood-critical e beat:

```dotenv
FLOOD_ADAPTIVE_ENABLED=1
FLOOD_MONITORING_POLICY=conservative
FLOOD_MONITORING_POLICY_EPOCH=v1
FLOOD_MONITORING_PILOT_CAMERAS=<UUID da câmera piloto>
FLOOD_CAPACITY_CONTROL_ENABLED=1
FLOOD_MONITORING_TREND_DELTA=5
FLOOD_MONITORING_MAX_GAP_SECONDS=600
```

Uma lista de câmeras **vazia habilita a política para todas**. Manter um UUID
explícito durante o piloto. A admissão de capacidade é global e inclui câmeras
que ainda seguem a política antiga. Não ampliar a concorrência dos workers:
dois processos regulares e dois críticos, com prefetch 1.

Três análises consecutivas com crescimento de pelo menos cinco pontos por
transição levam a observação. Um indício forte válido também leva a observação;
três indícios fortes consecutivos levam a crítico. Cinco análises válidas sem
indício e sem crescimento levam a recuperação; outras cinco levam a normalidade.
Falhas, qualidade inválida, troca de modelo e intervalo superior a dez minutos
interrompem a sequência. Intervenção manual preserva o nível até expirar.

O contexto de cada execução registra a versão da política, média dos frames,
janela de tendência, justificativa e atrasos de despacho e fila. Ao reativar uma
política após interrupção, usar um novo `FLOOD_MONITORING_POLICY_EPOCH`, inclusive
se não houve análises durante a pausa. Isso impede reutilização da sequência.

## Capacidade e limites

O scheduler `config.monitoring_scheduler:MonitoringDatabaseScheduler` coleta
`/proc/stat` e `MemAvailable` a cada dez segundos no beat, independentemente dos
workers. O dispatcher também pode atualizar a amostra. Um lock transacional
PostgreSQL serializa amostragem e reservas; Redis guarda a janela de 60 segundos.
O ambiente suportado é o deployment Docker atual em um único host. Beat e workers
precisam observar o mesmo host; múltiplos hosts exigem outro coletor.

- CPU abaixo de 70% e memória acima de 2 GiB: duas reservas por fila.
- CPU a partir de 70% ou memória até 2 GiB: uma reserva por fila.
- CPU a partir de 90% ou memória até 1 GiB: nenhum novo despacho.
- Após pressão: voltar a duas reservas somente após cinco minutos contínuos
  com CPU abaixo de 55% e memória acima de 3 GiB.
- Inicialização ou telemetria com mais de 30 segundos: uma reserva por fila.

A pressão de CPU inclui espera por I/O. Reservas pendentes e em execução contam
para o limite. Uma mudança manual de nível não muda a fila da reserva existente.
Falha na publicação ao broker libera a reserva ainda não iniciada. Duplicatas
não executam e tokens substituídos não podem finalizar uma análise antiga.

Os intervalos não garantem atendimento instantâneo em um pico simultâneo. Com
sete imagens espaçadas por três segundos, a captura ocupa aproximadamente
18 segundos por execução. Seis câmeras críticas vencendo juntas precisam de
três ondas com dois processos: observar o atraso e não aumentar concorrência
automaticamente para mascarar esse limite.

## Interface e acompanhamento

O GET administrativo `cameras/{id}/monitoring/` continua somente leitura e
acrescenta `interval_seconds`, `delay_seconds`, `scheduling_status`, `trend` e
`policy_version` em `monitoring`, além de `capacity` no nível superior.
A interface diferencia agendamento, fila, execução e espera por capacidade.

O endpoint privado de métricas existente expõe:

- `aqua_monitoring_deferred_cameras{level}`: câmeras atualmente adiadas.
- `aqua_monitoring_dispatch_overdue_seconds{level}`: maior atraso atual.
- `aqua_monitoring_reservations{queue,state}`: reservas pendentes/em execução.
- `aqua_monitoring_capacity_limit`: limite por fila.
- `aqua_monitoring_telemetry_available`: disponibilidade da janela completa.
- `aqua_monitoring_host_cpu_percent` e `aqua_monitoring_host_memory_available_bytes`.

Os rótulos são limitados, sem identificadores de câmera. Os três primeiros são
gauges de estado atual, não contadores cumulativos.

Depois de pelo menos 24 horas, obter o relatório sem executar inferências:

```sh
python manage.py monitoring_capacity_report --camera-id <UUID> --policy-version conservative:v1
```

Verificar ausência de execuções sobrepostas, pelo menos 95% dos despachos dentro
de 20 segundos, ausência de crescimento contínuo da fila e histórico de pressão
do host. O relatório usa todos os despachos observados e não esconde os atrasados
por capacidade. O tempo desde a primeira execução não comprova cobertura contínua;
inspecionar interrupções e os gráficos de métricas. Não expandir só porque a
capacidade atual está normal. Expandir exige revisão dos indicadores do período.

## Reversão

Para retornar à frequência anterior mantendo proteção de carga, configurar
`FLOOD_MONITORING_POLICY=legacy` e manter `FLOOD_CAPACITY_CONTROL_ENABLED=1`.
Para reproduzir também a admissão anterior, definir o controle como `0`.
Recriar os serviços afetados com a mesma configuração em todos. Para rollback
de imagem, parar beat, drenar workers e restaurar os manifests/imagens anteriores.
Preservar a migração aditiva, históricos e evidências; não reverter o banco para
um backup como parte de rollback apenas de código.
