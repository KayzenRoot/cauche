# CAU-MVP-001/CD-26 - integridade de streaming Kalshi

Status PROPOSED / NOT PROMOTED. HIGH_ASSURANCE. F06/#8 parcial. CD-25 APPROVED comentário 7139770013; CIs 45451617155/45451642225: 1180 Python + 4 E2E, Kalshi 496/547, backend 4010/4379; cinco módulos 100%.

## Context Lock
Base e7253ee0f1b96e9b77f11f23a937018b672cf5cd; main a8e69195192a0abda82298927f0b0f231a86ea7b; PR #2 7e6faee7c00cfd65628379576011e51f54a5ff8d.
Fingerprints: checkpoint b2f0247d36514b625b203cecf587432a957b2a66; ledger 2db4cb6daa284c8bffce82cdfca1a4efe8ea4ce4; scope 7084e1c86918b7716c023dc947c7a24efa28ce8a; DoD 6e73ebfa3a385fc7e49ff56f372856f84537ff15; architecture d536ac3f1ba924ef80299e6569d8b0ab25eff78d; requirements 4c8e80e8aa65cacc8040ec0dd96a46e385556c34.

## Work Order
NECESSARY: restringir destinos/canais de streaming autenticado antes de assinatura, rejeitar status remoto que imite estado local, validar ticker (book não cruzado, timestamp positivo/não futuro), snapshot exige ambos lados explícitos e sequência positiva, rejeitar snapshot regressivo e delta inválido; manter invalidação de book após gap/desconexão e exigir novo snapshot. Preservar fallback de relógio local para snapshot/delta sem timestamp, identificado como observação local e não timestamp oficial. Testar backoff/reconexão, isolamento de fills e encerramento de sockets. Ler Kalshi, common, contratos de streaming, market_data e fontes canônicas. Arquivos: kalshi.py, test_kalshi_stream.py, este documento. Sem alteração de endpoints, credenciais, estratégia, mutações, schema, dependências, CI, permissões ou PR #2. Nenhuma conexão oficial ou operação financeira. Testes com geradores/sockets simulados e relógio controlado.
Proof obligations: 3 bloqueios públicos, 4 destinos, 1 ticker válido, 8 ticks inválidos, 3 erros remotos, 1 retry, 1 fills, 8 snapshots inválidos, 7 updates inválidos, 2 gaps/reconexões: 38 novos casos previstos, 1218 Python. CI integral, segurança, SQLite/PostgreSQL, E2E, preservar cinco módulos 100% e ampliar cobertura Kalshi sem exclusões artificiais. Fora de escopo: mutações, exclusividade, completude/ordenação oficial entre sessões e sincronização remota, revisão independente e branches/frontend. Entregáveis commit, CI, Evidence Bundle pt-BR. Stop: CI incompleto/falha/risco relevante; sem próximo delta, merge, deploy, despesas, operações financeiras ou promoção de checkpoint. Rollback via revert explícito sem force-push.
## Evidence Bundle preliminar
NOT_TESTED até CI do SHA. F06 aberta, aceitação oficial e revisão independente HIGH_ASSURANCE pendentes. Checkpoint não promovido.
