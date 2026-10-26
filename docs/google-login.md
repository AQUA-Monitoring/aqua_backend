# Acesso com Google no AQUA

O frontend usa o botão oficial Google Identity Services nas telas de login e cadastro.
O backend verifica a assinatura, emissor, público, expiração e nonce do ID token e
emite os mesmos JWTs e usuário UUID usados por email/senha. Um primeiro acesso cria
uma conta padrão com senha inutilizável; acessos posteriores usam `google_sub`.

O AQUA usa `core.users.infra.models.User`, separado do usuário administrativo Django.
Por isso, as rotas de sessão e a configuração allauth do início do PR foram
substituídas por uma troca de ID token compatível com a autenticação existente.
Não são necessárias novas tabelas, sessões compartilhadas, cookies entre origens,
permissões adicionais de Gmail ou segredo OAuth no navegador.

## Configuração

1. No Google Cloud, configure um cliente OAuth do tipo **Aplicativo da Web** e a
   tela de consentimento. Durante a fase de testes, cadastre os usuários de teste.
2. Autorize as origens JavaScript que realmente servirão o botão:
   - `https://aqua.michalski.app`
   - `https://5000.michalski.app`
   - `http://localhost:5173`, se houver uso local.
3. Configure `GOOGLE_OAUTH_CLIENT_ID=<id-publico>.apps.googleusercontent.com` no
   backend. Esse fluxo não precisa de client secret nem URI de redirecionamento.
4. Instale `google-auth[requests]` pelas dependências versionadas e publique o backend
   e frontend compatíveis. O frontend consulta a configuração da API; não é preciso
   duplicar o ID no build Vite.
5. Mantenha `REDIS_CACHE_URL` acessível: o Redis impede reaproveitar uma tentativa
   entre os processos da API. Sem Redis a autenticação Google falha com 503.

Se o ID estiver vazio, a configuração retorna `enabled: false` e o frontend
mantém apenas o fluxo de email/senha. Não use IDs fictícios em produção.

## Contrato

- `GET /api/auth/google/config/`: `{enabled: false}` ou
  `{enabled: true, client_id, nonce, challenge}`. Resposta `Cache-Control: no-store`.
- O frontend inicializa o SDK com `client_id` e `nonce` e renderiza o botão oficial.
- `POST /api/auth/google/`: `{credential, challenge}`.
- Sucesso 200: `{user, tokens: {access, refresh}}`; o cliente persiste a sessão pelo
  mesmo store do login convencional e navega para a página inicial.
- A tentativa tem validade de cinco minutos e uso único. Repetições ou falhas
  exigem nova configuração/nonce. A interface prepara uma nova tentativa.
- Credenciais inválidas: 401; conta desativada: 403; conflito de vínculo: 409;
  serviço/configuração indisponível: 503; limite de solicitações: 429.

Uma conta existente só é vinculada automaticamente por email quando o Google é
responsável por esse email (Gmail ou Workspace verificado). Emails externos de
contas existentes continuam exigindo email/senha. Nenhuma senha, perfil ou
permissão existente é substituída. Uma conta vinculada é localizada pelo `sub`,
mesmo se o email no Google mudar.

## Validação

Os testes temporários ficam fora do repositório conforme AGENTS.md. Cobrem ID tokens
RSA assinados localmente, público/emissor/expiração/nonce inválidos, repetição,
criação e vínculo de contas, conta desativada, Redis indisponível e contrato JWT.
Os testes de navegador simulam o SDK e a API, verificando login/cadastro, erro,
repetição, configuração ausente e persistência da sessão.

Uma autorização real ainda exige o cliente OAuth configurado e interação do dono
da conta no Google. Os testes simulados não substituem essa verificação final.

Referências:
- https://developers.google.com/identity/gsi/web/guides/verify-google-id-token
- https://developers.google.com/identity/gsi/web/reference/js-reference
