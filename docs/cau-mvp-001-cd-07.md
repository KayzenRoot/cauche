# CAU-MVP-001/CD-07 — exclusão mútua do worker e tolerância a falhas

Status PROPOSED / NOT PROMOTED. HIGH_ASSURANCE; F02/#4 parcial. CD-06/R01 APPROVED no comentário 7137223233, CI 45433481417: 330 Python + 4 E2E, demo_execution 91%, worker 68%, backend 81% statements.

## Context Lock
Main a8e69195192a0abda82298927f0b0f231a86ea7b; base CD 08887077bdc5d819ff6bc3ced6368d42a15aa899; PR #2 head 7e6faee7c00cfd65628379576011e51f54a5ff8d preservado.
Fingerprints: checkpoint b2f0247d36514b625b203cecf587432a957b2a66; ledger 2db4cb6daa284c8bffce82cdfca1a4efe8ea4ce4; scope 7084e1c86918b7716c023dc947c7a24efa28ce8a; DoD 6e73ebfa3a385fc7e49ff56f372856f84537ff15; architecture d536ac3f1ba924ef80299e6569d8b0ab25eff78d; requirements 4c8e80e8aa65cacc8040ec0dd96a46e385556c34.

## Work Order
Objetivo NECESSARY: substituir lease advisory de sessão com unlock explícito por advisory transacional não bloqueante, evitando lock residual em conexão devolvida ao pool; separar leased_step para testar a fronteira de concorrência e falhas. Ler worker, db, arquitetura, ADR-003, CI e testes de recuperação. Arquivos: backend/app/worker.py, backend/tests/test_demo_recovery.py, este documento. Fora de escopo: alteração do claim/dispatch, schema, estratégia, adapter, produção, PR #2, operação financeira ou deploy. Manter chave existente e uma execução por vez em PostgreSQL; commit/rollback/cancelamento liberam lease; falha de conexão não inicia tick. SQLite permanece single-worker para teste local, sem promessa de lock distribuído. Preservar logging seguro do loop e delay original.

Proof obligations: sucesso sequencial, rollback após erro SQLAlchemy/exceção/cancelamento, contenção real por conexões PostgreSQL distintas enquanto tarefa suspensa, cancelamento real da tarefa e retomada, ausência de step quando aquisição de conexão falha; 6 cenários por dialeto com step mockado (contenção somente PostgreSQL). Serviço PostgreSQL existente; CI integral e cobertura sem exclusões. Não provar fencing durante perda silenciosa da sessão do lease, exactly-once remoto, compatibilidade de workers antigos/novos simultâneos, nem tolerância a kill real. Implantação mista exigiria coordenação; não autorizada. Entregáveis: diff, commit, CI, Evidence Bundle, auditoria pt-BR. Stop: CI incompleto/falha/risco novo; sem outro delta, merge, deploy, gastos, operação financeira ou checkpoint. Rollback via revert explícito sem force-push, com workers parados.

## Evidence Bundle preliminar
NOT_TESTED até logs do SHA correto. Sem nova infraestrutura, dependências, schema ou permissão de negociação. F02 segue aberto até prova de fencing/recuperação mais ampla; revisão independente pendente. Checkpoint não promovido.
