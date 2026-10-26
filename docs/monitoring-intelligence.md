# Inteligência do monitoramento

Implementação administrativa em `/admin/inteligencia`, com APIs em
`/api/flood_monitoring/intelligence/`. Publicado no frontend oficial e no backend em 06/10/2026. O modelo operacional
foi preservado; não houve início automático de treinamento. Metadados e rollback
estão em `rollout/20261006-intelligence/` no workspace.

## Ativação

1. Aplicar as migrações `0023_monitoring_intelligence` e `0024_curation_time_index`
   antes de iniciar os serviços com este código. A primeira preserva históricos
   e desabilita entradas diárias legadas de treinamento no django-celery-beat.
2. Usar a mesma versão do backend em web, beat e workers. Definir
   `FLOOD_INTELLIGENCE_ENABLED=1` em todos esses serviços. O compose opt-in
   `docker-compose.monitoring.yml` já inclui a configuração e limita o worker
   `flood-training` a uma CPU, 2 GiB, uma thread e concorrência um.
   Os workers regulares e críticos permanecem com concorrência dois cada.
3. Montar os volumes privados de evidências e modelos em web e workers.
   O diretório de curadoria padrão é `/app/private_flood_evidence/curated`;
   ele não é servido como mídia pública nem apagado pela expiração operacional.
4. Manter beat e Redis disponíveis. O agendador coleta telemetria do host mesmo
   quando apenas o processamento de inteligência está habilitado. Essa medição
   pressupõe que os workers executam no mesmo host do beat.
5. No painel, registrar o checkpoint em uso. Seu hash, arquitetura, classes e
   configuração são verificados. A origem desconhecida permanece explícita;
   somente exemplos posteriores ao registro podem formar sua avaliação prospectiva.
6. Formar a base por revisão humana, criar um conjunto e solicitar treinamento.
   Nenhum treinamento é iniciado automaticamente. A tarefa periódica apenas
   agrupa análises já persistidas e executa segmentos de jobs solicitados.

`FLOOD_INTELLIGENCE_ENABLED=0` interrompe novas tarefas de manutenção/avaliação
paralela. Para interromper um treinamento já iniciado, usar **Pausar** ou
**Cancelar** antes de desativar o worker. A escolha operacional de modelo continua
válida: desligar a curadoria não deve trocar silenciosamente uma versão piloto.
O rollback de modelo é uma ação própria no painel; não exige remover tabelas.

## Dados e agrupamento

- A manutenção lê, em ordem temporal, até 100 análises pendentes das últimas
  24 horas, com orçamento de cinco segundos por passagem. Não abre streams.
- Qualidade aproveita `context.quality_usable`; probabilidades são médias dos
  frames persistidos. Lacunas acima de dez minutos e análises inválidas reiniciam
  a contagem de 30 minutos. Indícios fortes são destacados imediatamente.
- Cada câmera possui no máximo um episódio de divergência ativo. Mudança de
  modelo encerra o anterior; duas observações normais encerram o episódio.
- Cada rodada seleciona até duas execuções distintas. Depois da primeira revisão
  suas imagens ficam congeladas. Duas classificações diferentes consecutivas
  podem abrir nova rodada. Rótulos nunca se propagam aos demais frames.
- A auditoria normal alterna dia/noite segundo a data local, com no máximo uma
  seleção diária por câmera. Dia: 06h–18h; fuso configurável.
- Revisões por imagem são aprovadas por um administrador ativo. `previous_id`
  impede sobrescrever uma decisão concorrente; `request_id` torna retries
  idempotentes. As revisões antigas por execução não são aprovadas retroativamente.
- Grupos vinculados e imagens com bytes idênticos são unidos antes da divisão.
  O hash do evento determina 50% treino / 10% validação / 40% teste. São proporções
  por grupo, não garantias de quantidade de imagens. Os mínimos são verificados
  separadamente e podem exigir mais de 200 exemplos.
- Conjuntos são imutáveis e guardam hashes, revisões, eventos e partições.
  Correções/vínculos que afetem suas linhas bloqueiam novos usos e promoção;
  adicionar exemplos independentes não invalida um conjunto anterior.
- Hashes de imagens e eventos usados no treino são herdados pela linhagem para
  detectar vazamento mesmo quando um grupo é renomeado.

## Treinamento e avaliação

O worker processa segmentos de aproximadamente 18 segundos, verifica recursos
entre imagens e persiste pesos, otimizador, RNG, fase, época e índice. O checkpoint
é substituído atomicamente. Um lock de sessão PostgreSQL impede execução duplicada.
A perda do processo permite repetir o segmento desde o último checkpoint.

O início/retomada exige cinco minutos de CPU abaixo de 55%, memória acima de 3 GiB
e telemetria recente. CPU a partir de 70%, memória até 2 GiB, câmera crítica ou
telemetria com mais de 30 segundos suspendem o trabalho. Pausa manual permanece
até **Retomar**. O tempo de resposta depende de concluir a imagem em processamento;
a validação isolada verificou segmentos abaixo de 30 segundos. Monitorar esse
limite ao ativar com o checkpoint real, antes de ampliar o uso.

O ajuste mantém o extrator congelado e treina a camada `backbone.fc`. Treino e
validação permanecem separados do teste final. A comparação executa candidato e
referência nas mesmas imagens e registra matriz, F1, precisão, sensibilidade,
intervalos de Wilson, Brier, calibração por faixa, recortes e latência de inferência.
O pico de memória é do processo de avaliação, não uma medição exclusiva do modelo.
Os intervalos de Wilson não corrigem correlação entre imagens de um evento; o
painel explicita essa limitação. Não interpretar concordância da fila como acurácia.

**Comparar no conjunto de teste** permite avaliar uma versão Torch existente,
sem ajustar pesos. Esse job usa os mesmos limites, pausa e cancelamento.

A avaliação paralela admite no máximo uma em cada dez observações elegíveis,
uma por câmera em cinco minutos e uma reserva global. Não há despacho paralelo
quando existe job de treinamento/avaliação pendente. Reservas expiram em 120s.
O candidato é único e reutiliza evidências; não realiza capturas adicionais.

## Aplicação e auditoria

A promoção exige comparação no mesmo conjunto, os limiares de qualidade existentes
(sensibilidade 95%, precisão 90%, regressões e recortes), 30 janelas paralelas e
piloto. Somente um piloto fica ativo. A expansão exige 24 horas, cobertura sem
lacunas acima de dez minutos, ausência de erros e latência dentro de 110 segundos.
Os dados do piloto e as revisões disponíveis continuam sujeitos à avaliação do
administrador; não há expansão automática.

A resolução central escolhe atribuição da câmera ou versão global antes da captura.
Análises iniciadas mantêm sua versão. Trocas são serializadas no PostgreSQL e
verificam hash, carregamento e classes antes de mudar a atribuição. A restauração
aceita versões com registro operacional anterior e artefato compatível, sem exigir
superioridade estatística. O histórico registra autor, origem, destino e motivo.
Essas ações não chamam publicação de alertas.

## Configurações

| Variável | Padrão |
|---|---|
| `FLOOD_INTELLIGENCE_ENABLED` | `0` |
| `FLOOD_CURATED_ROOT` | `/app/private_flood_evidence/curated` |
| `FLOOD_CURATED_LIMIT_BYTES` | 10 GiB |
| `FLOOD_MODEL_LIMIT_BYTES` | 5 GiB |
| `FLOOD_REVIEW_NORMAL_THRESHOLD` | `70` |
| `FLOOD_REVIEW_PERSIST_SECONDS` | `1800` |
| `FLOOD_REVIEW_TIMEZONE` | `America/Sao_Paulo` |

O painel sinaliza 80% do orçamento; novas gravações são bloqueadas no limite.
Não há limpeza automática de artefatos operacionais ou necessários ao rollback.
Checkpoint temporário também entra no orçamento de modelos.

## Validação da entrega

Testes e fixtures ficam em `/tmp/aqua-intelligence-validation`, fora dos repositórios.
Foram usados PostgreSQL/PostGIS e Redis exclusivos, sem copiar dados de produção.

- 80 testes aprovados: episódios, concorrência, permissões, evidências, conjuntos, orçamento,
  amostragem exata e regressões do monitoramento/alertas.
- Treinamento Torch real de uma ResNet18 sintética com 240 imagens (100 treino,
  20 validação, 120 teste), uma CPU e 2 GiB; pausa/retomada e avaliação de duas
  versões no mesmo conjunto. O candidato não foi promovido e não passou nos
  critérios de qualidade. Esse ensaio valida execução, não qualidade em alagamentos.
- Piloto, retorno global e rollback exercitados pela API isolada com verificação
  real dos artefatos. Apenas o aceite estatístico do piloto foi simulado no teste.
- Navegador usando o componente e a API reais: estado vazio, evidência privada,
  revisão gravada, criação de conjunto, falta de dados, métricas e controles de
  pausa, retomada e cancelamento; largura móvel de 390 px sem transbordamento.
- Migrações, `manage.py check`, tipos, lint e compilação do frontend.

A observação de 24 horas em uma câmera real e a avaliação de qualidade dependem
explicitamente da ativação e da base humana, e não são substituídas por esse ensaio.

## Retenção de imagens operacionais

A cada cinco minutos, a tarefa `flood-evidence-retention` remove em lotes de até 5.000 arquivos as evidências com mais de 15 dias, inclusive registros antigos criados sob a política de 90 dias. Os metadados de auditoria permanecem no banco. Imagens salvas manualmente e a curadoria de treinamento são preservadas.

O arquivo automático mantém até 50 imagens no total (`FLOOD_EXTREME_EVIDENCE_LIMIT`, de 0 a 100), sem hashes repetidos e com no máximo uma imagem por câmera por dia. São elegíveis apenas análises sem erro, com modelo identificado, qualidade utilizável, classificação `FLOOD_INDICATION` e probabilidade de alagamento válida do quadro. A seleção prioriza os maiores valores dessa probabilidade. A pontuação mede a confiança do modelo, não a altura da água. As melhores imagens substituem as anteriores; imagens substituídas voltam à retenção normal. A API identifica essas imagens com `extreme_preserved` e inclui-as no filtro `saved=true`.
