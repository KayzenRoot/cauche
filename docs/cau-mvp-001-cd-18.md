# CAU-MVP-001/CD-18 - transporte compartilhado e JSON estrito

Status PROPOSED / NOT PROMOTED. HIGH_ASSURANCE; F01/#3, F02/#4 e F06/#8 parciais. CD-17 APPROVED no comentário 7138445901, CI 45442341431: 746 Python + 4 E2E; market_data 216/216, worker 283/283, demo_execution 353/353, common 103/128, backend 3673/4237.

## Context Lock
Base CD 0893f792d74fd1e97947677bf9b3b009cd17e5f2; main a8e69195192a0abda82298927f0b0f231a86ea7b; PR #2 head 7e6faee7c00cfd65628379576011e51f54a5ff8d preservado.
Fingerprints: checkpoint b2f0247d36514b625b203cecf587432a957b2a66; ledger 2db4cb6daa284c8bffce82cdfca1a4efe8ea4ce4; scope 7084e1c86918b7716c023dc947c7a24efa28ce8a; DoD 6e73ebfa3a385fc7e49ff56f372856f84537ff15; architecture d536ac3f1ba924ef80299e6569d8b0ab25eff78d; requirements 4c8e80e8aa65cacc8040ec0dd96a46e385556c34.

## Work Order
Objetivo NECESSARY: rejeitar JSON com chaves duplicadas inclusive em objetos aninhados/escapes equivalentes, constantes não finitas, falhas de tipo/decimais e mensagens malformadas na fronteira compartilhada HTTP/WebSocket; manter Decimal exato e erros sanitizados. Demonstrar limites de retry GET, não repetição/redirect de mutação simulada, status, pacing, tipos e falhas de socket. Ler common, contratos existentes, adapters Deriv/Kalshi, fontes canônicas e CI. Arquivos: backend/app/integrations/common.py, backend/tests/test_transport_common.py, este documento. Não modificar destinos, credenciais, assinatura, política existente de status/retry, permissões, schema, dependências ou CI. Nenhuma rede, credencial real ou ordem; MockTransport e sockets locais. Fora de escopo: autenticação oficial, proteção de todos os tamanhos de payload, rate limit distribuído, validação de todos os campos de adapters, alterações de protocolo, produção e PR #2.
Proof obligations: testes de parsing estrito/precisão, tipos e status, retry/pacing, não repetição de mutação simulada, sockets e redirecionamento; CI integral, regressão SQLite/PostgreSQL, segurança/E2E, preservar 100% statements nos três módulos. Não inventar teste de caminho inalcançável ou excluir instruções; reportar lacunas restantes. Entregáveis: commit, CI, Evidence Bundle e auditoria pt-BR. Stop: CI incompleto/falha/risco relevante; sem outro delta, merge, deploy, despesas, operações financeiras ou promoção de checkpoint. Rollback por revert explícito sem force-push.
## Evidence Bundle preliminar
NOT_TESTED até logs do SHA resultante. F01/F02/F06 abertas; revisão independente e aceitação oficial pendentes. Checkpoint não promovido.
