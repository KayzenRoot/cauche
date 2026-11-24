# CAU-MVP-001/CD-12 — encerramento seguro do worker e cobertura residual

Status PROPOSED / NOT PROMOTED. HIGH_ASSURANCE; F02/#4 e F06/#8 parciais. CD-11 APPROVED no comentário 7137771383, CI 45437463677: 550 Python + 4 E2E, demo_execution 100% (353/353), worker 77% (269/62 não cobertas), backend 83% statements.

## Context Lock
Main a8e69195192a0abda82298927f0b0f231a86ea7b; base CD 65582c7197c0862a5f6d7c0c6e78def4e6289b13; PR #2 head 7e6faee7c00cfd65628379576011e51f54a5ff8d preservado.
Fingerprints: checkpoint b2f0247d36514b625b203cecf587432a957b2a66; ledger 2db4cb6daa284c8bffce82cdfca1a4efe8ea4ce4; scope 7084e1c86918b7716c023dc947c7a24efa28ce8a; DoD 6e73ebfa3a385fc7e49ff56f372856f84537ff15; architecture d536ac3f1ba924ef80299e6569d8b0ab25eff78d; requirements 4c8e80e8aa65cacc8040ec0dd96a46e385556c34.

## Work Order
Objetivo NECESSARY: garantir encerramento do ticker em cancelamento normal de tarefa (finally), preservar exceção de cancelamento e logging sanitizado; validar caminhos residuais de worker sem configuração/bot/run, erro de run obsoleto, KeyError, cursor alterado e fim de fixture com exposição sem resultado. Ler worker, market_data, execution, models, testes de recuperação e fontes canônicas. Arquivos: backend/app/worker.py, backend/tests/test_worker_lifecycle.py, backend/tests/test_demo_recovery.py, este documento. Fora de escopo: novos adapters, alteração de schema, estratégia, permissão, produção, PR #2, fencing e operações financeiras. Não declarar encerramento seguro sob kill abrupto ou cancelamento repetido durante cleanup; não alterar estados contábeis para produzir cobertura. Preservar 100% statements demo_execution e não introduzir exclusões artificiais.

Proof obligations: quatro testes de lifecycle (retry de falhas, fallback de sinais simulado, cancelamento e main), sete cenários de integração por dialeto SQLite/PostgreSQL (14 casos), total 18 novos previstos; sem I/O financeiro, logs sem detalhes privados simulados, exposição de replay preservada UNRESOLVED sem settlement inventado, nenhuma persistência por observação de cursor obsoleto. CI completo, segurança/E2E, relatório de cobertura, auditoria pt-BR. Entregáveis: commit, Evidence Bundle, parecer. Stop: CI incompleto/falha/risco relevante; sem outro delta, merge, deploy, despesas, operações financeiras ou checkpoint. Rollback via revert explícito sem force-push, com workers parados.

## Evidence Bundle preliminar
NOT_TESTED até CI do SHA resultante. Sem nova infraestrutura. Não certifica kill real, perda de sessão/fencing, validação oficial, concorrência distribuída ou cobertura integral do worker. Checkpoint não promovido.
