# Monitoramento adaptativo e resposta à crise

## Evidências de 11/09/2026

Produção identificada em `aqua-prod-*`; o worker `aqua_backend-worker-1` é uma
instância antiga sem conexão com `db`. Nenhuma alteração no banco foi necessária
para a captura de emergência.

Foram preservados seis JPEGs e seis clipes MP4 em
`backups/incident-20260911T210357Z/`, com manifesto, checksums SHA-256, câmera,
região, instante de captura e snapshot da classificação. O diretório contém
Terminal Central, Praça da Bandeira, Rua 6 de Janeiro, Terminal Norte,
Prefeitura e Rua Leopoldo Beninca. Os arquivos não estão em mídia pública.

No histórico das seis horas anteriores havia indicações fortes em Praça da
Bandeira (15) e Rua 6 de Janeiro (6), e intermediárias em Rua 6 de Janeiro (10),
Praça da Bandeira (1), Terminal Norte (1) e Terminal Central (1). Isso não
estabelece três alagamentos simultâneos; o conjunto e as probabilidades oscilaram.

Replay do JPEG do Terminal com o checkpoint de produção `best_real_model.pth`:
normal 90,845%, intermediário 5,570%, alagado 3,585%. A pista visível apresenta
água acumulada, mas grande parte do enquadramento está ocupada por ônibus. A
oclusão e o redimensionamento para 224×224 são hipóteses para o falso negativo;
não houve experimento que isolasse a contribuição de cada fator. A imagem deve
entrar na regressão com rótulo revisado por dois operadores. O timestamp exibido
na imagem está cerca de 32 segundos atrás do horário da captura; não representa
um frame simultâneo à leitura de banco.

Uma segunda rodada às 21:21 UTC está em `backups/incident-20260911T212119Z/`:
seis snapshots, seis clipes e 36 imagens extraídas das sequências, sem erros
de captura. Os dois conjuntos ocupam aproximadamente 121 MB. Replay de 30
inferências no checkpoint original, com duas threads e OMP_NUM_THREADS=1:
15,16 imagens/s, p95 154,38 ms, pico RSS 908896 KiB. Este ensaio não mede
atraso HLS, filas Celery, throughput de disco nem precisão do modelo.

## Operação

Aplicar `migrate` antes de iniciar serviços com o novo código. Para ativar,
combinar o compose de produção com `docker-compose.monitoring.yml` e definir
`AQUA_BACKEND_IMAGE` para uma imagem construída e testada com este checkout.
O overlay fornece volumes privados compartilhados e workers separados para
`flood_critical` e `flood_regular`. O perfil `training` consome `flood_training`.
Não configurar dois Beats. O dispatcher respeita `FLOOD_ADAPTIVE_ENABLED`;
quando ativo, os nomes de tarefas legadas encaminham para o dispatcher.

Intervalos após cada execução: NORMAL 300s, WATCH 60s, CRITICAL 30s,
RECOVERY 120s. WATCH/CRITICAL/RECOVERY capturam sete amostras espaçadas por
aproximadamente três segundos, drenando frames intermediários; NORMAL captura
três. HLS inclui atraso do provedor, não medido automaticamente. Uma sequência
não garante visibilidade da pista se houver oclusão contínua.

Três leituras fortes consecutivas entram em CRITICAL. Cinco leituras normais
sem contexto levam a RECOVERY; dez retornam a NORMAL. Falhas não contam como
normalidade. Indício recente em outra câmera da mesma região mantém observação.
Operador pode fixar intensidade por até 24 horas, com justificativa auditada.

As amostras são persistidas antes da inferência no volume privado, inclusive
resultados normais e indisponibilidade do modelo. O histórico associa resultados
por frame, brilho/nitidez, duplicação, contexto e checksum. Links exigem sessão
administrativa, são vinculados ao usuário e expiram em cinco minutos. O job de
retenção remove até 500 arquivos vencidos por hora após 90 dias, mantendo os
metadados. Erros de escrita ou transação podem deixar arquivos órfãos; o volume
deve ser monitorado e esses arquivos não devem ser apagados indiscriminadamente.

API administrativa: `cameras/{id}/monitoring/` (GET/POST),
`analyses/{id}/reviews/` (POST), `reviews/{id}/approve/` (POST),
`models/` (GET), `models/{id}/promote/` (POST), `monitoring/health/` (GET).
Revisões aprovadas exigem outro operador e `event_id` explícito para separar
eventos entre treino e teste. O painel de câmera cobre histórico, imagem,
intensidade e envio de correções; aprovação e promoção estão expostas pela API.

## Modelos e publicação

`register_flood_model ARTIFACT MANIFEST --name NAME` registra um modelo em
shadow. `--bootstrap-current` aceita somente o checkpoint já configurado e
somente quando não há campeão. Um bootstrap não comprova precisão.
Manifesto Torch: `{"classes":["normal","medium","flooded"],"config":{"model_name":"resnet50"}}`.
ONNX requer `onnxruntime` instalado e saída de logits NCHW para entrada RGB
224×224 normalizada por ImageNet. HTTP usa HTTPS configurado por
`FLOOD_REMOTE_PROVIDER_URL/TOKEN` e exige probabilidades percentuais e a mesma
versão no retorno. TorchScript segue o suporte do adaptador Torch existente.

Treino diário opt-in: `FLOOD_RETRAINING_ENABLED=1`. Fine-tuning da cabeça Torch
com rótulos independentes aprovados, mínimo de 200 imagens e cinco eventos,
separação por evento e bloqueio de hashes iguais entre treino/teste. Crise em
CRITICAL adia o treino. Candidatos entram em shadow; nunca são promovidos pelo
job. Ainda é necessário obter rótulos suficientes e validar dados reais.
O manifesto do pai deve declarar `training_event_ids`; a atribuição dos eventos
ao teste é estável por hash e exclui qualquer evento usado para treinar o pai.
O treino aguarda ao menos 100 rótulos de teste, incluindo 30 alagamentos.

Promoção humana requer 100 exemplos de teste, 30 alagados, recall >=95%,
precisão >=90%, ausência de regressão nas fatias disponíveis, caso Terminal
Central aprovado e 30 janelas shadow sem erro. Os números são critérios iniciais,
não métricas já alcançadas. Avaliação atual mede classificação, matriz de
confusão, F1 e Brier; tempo de detecção por evento, condições meteorológicas e
drift estatístico ainda requerem validação adicional. Rollback usa a mesma API
para uma versão aposentada que satisfaz os critérios.

Publicação automática permanece opt-in (`FLOOD_AUTOPUBLISH_ENABLED=1` e
`FLOOD_AUTOPUBLISH_ACTOR_ID` de uma conta administrativa de serviço). Exige
campeão validado, três janelas não sobrepostas nos últimos cinco minutos,
média >=90% em cada janela, qualidade utilizável, evidência não vencida e
corroboração regional recente. A conta de serviço e a transição automática
ficam registradas; a mensagem pública identifica a confirmação automática.
Não habilitar com o checkpoint legado sem validação. Dados Weather existentes
são diários e não são usados como confirmação de chuva em tempo real.

## Verificação e limites de entrega

Testes executados com PostGIS descartável, isolado de produção, e frontend em
container Node. O diretório de captura de emergência é a única mudança
operacional já aplicada; migrations, dispatcher novo e publicação automática
não foram ativados em produção por este procedimento.
As duas rodadas tiveram todos os 60 arquivos de mídia conferidos por SHA-256.

Antes de rollout: validar capacidade dos workers, benchmark com streams reais,
espaço do volume, permissões e recuperação de broker/storage. Não há garantia
de precisão melhor até concluir curadoria, avaliação e promoção. Integração de
chuva/nível ao vivo, detecção estatística de drift, segmentação da pista e
teste de carga de crise permanecem trabalhos de validação/integração.
O endpoint de saúde já calcula divergência Jensen–Shannon da distribuição de
scores com mínimo de 30 amostras por janela; o limiar inicial de 0,1 precisa
ser calibrado por câmera e não é uma estimativa de precisão.
