# CAU-MVP-001/CD-01 — confirmação fail-closed

Status: PROPOSED / NOT PROMOTED. Risco HIGH_ASSURANCE. Issue #3 (F01), entrega parcial; demais obrigações F01 e F02 permanecem abertas.

## Context Lock
- Main: a8e69195192a0abda82298927f0b0f231a86ea7b.
- Base da correção / PR #1: d5521af81b922ca80ffb35910cca1828b9d4abe1.
- PR #2 head a preservar: 7e6faee7c00cfd65628379576011e51f54a5ff8d.
- Git blob fingerprints: checkpoint b2f0247d36514b625b203cecf587432a957b2a66; ledger 2db4cb6daa284c8bffce82cdfca1a4efe8ea4ce4; scope 7084e1c86918b7716c023dc947c7a24efa28ce8a; DoD 6e73ebfa3a385fc7e49ff56f372856f84537ff15; architecture d536ac3f1ba924ef80299e6569d8b0ab25eff78d; requirements 4c8e80e8aa65cacc8040ec0dd96a46e385556c34.

## Work Order / proof obligations
Objetivo: não transformar status remoto desconhecido em ACCEPTED, não coerçar identidade inválida e não fabricar fill Deriv quando a confirmação não é OPEN. Arquivos: backend/app/demo_execution.py e backend/tests/test_demo_acknowledgement.py. Fonte: contratos existentes dos adaptadores Deriv e Kalshi e arquitetura de claim durável. Escopo: confirmação normalizada e testes unitários mockados; fora de escopo: mudança de worker, estratégia, banco, infraestrutura, ordens, credenciais, PR #2, novos custos.

Critérios: resposta desconhecida/identidade inválida falha antes de persistir; Deriv OPEN preserva contabilização existente; Kalshi preserva estados válidos e não cria fill antes de reconciliar; recuperação ambígua não reenvia. Rodar pytest dirigido e suíte CI (Ruff, pytest com cobertura, Bandit, pip-audit, frontend, Compose/E2E, docs/secrets). Evidência: código, testes, SHA e run CI no comentário da PR. Sem aprovação baseada apenas em código ou testes escritos. Stop: qualquer falha, ausência de evidência ou conflito de contexto; manter draft e checkpoint não promovido. Rollback: revert explícito do commit em branch, sem force-push, se necessário.

## Evidence bundle preliminar
Mudança proposta em código e testes; resultados de execução ainda NOT_TESTED até leitura do CI. Sem alegação de execução oficial demo, de teste PostgreSQL específico de crash ou de correção integral da issue #3. Nenhuma chamada autenticada a corretora nem deploy. Checkpoint delta: nenhuma promoção; registrar somente depois de auditoria objetiva. 
