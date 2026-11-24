# CAU-MVP-001/CD-05 — integridade de liquidação Deriv

Status PROPOSED / NOT PROMOTED. HIGH_ASSURANCE. F01/#3 e F04/#6 parcialmente relacionadas, nenhuma issue encerrada. CD-04 aprovado no comentário 7136905458, CI 45431187563: 228 Python + 4 E2E, demo_execution 67%, worker 68%, global 80% statements.

## Context Lock
Main a8e69195192a0abda82298927f0b0f231a86ea7b; base CD 95d365f937af39c58ad7d374d4682e403851f5fe; PR #2 head 7e6faee7c00cfd65628379576011e51f54a5ff8d preservado.
Fingerprints: checkpoint b2f0247d36514b625b203cecf587432a957b2a66; ledger 2db4cb6daa284c8bffce82cdfca1a4efe8ea4ce4; scope 7084e1c86918b7716c023dc947c7a24efa28ce8a; DoD 6e73ebfa3a385fc7e49ff56f372856f84537ff15; architecture d536ac3f1ba924ef80299e6569d8b0ab25eff78d; requirements 4c8e80e8aa65cacc8040ec0dd96a46e385556c34.

## Work Order
Objetivo NECESSARY: não gravar liquidação Deriv inconsistente com ordem, posição, preço de compra, identidade, origem, moeda, tempo, payout e taxa contabilizada. Ler demo_execution, execution.record_settlement, integrations/deriv._contract_state, models, db e testes de recuperação. Arquivos: demo_execution.py, test_demo_settlement.py, test_demo_recovery.py, este documento. Fora de escopo: Kalshi, alterações de adapters, novas taxas, schema, recuperação histórica, produção, PR #2, ordens ou consultas oficiais. Preservar contrato existente de profit líquido: gross=payout-exposure, net=gross-fees. Se comissão remota diverge da taxa já registrada, bloquear em vez de inventar ou duplicar taxa; ausência de comissão opcional usa taxa registrada. Política de contabilização de comissão posterior ainda exige evidência oficial independente. Não afirmar que este contrato representa todos os produtos Deriv.

Proof obligations: nenhuma mutação para valores/identidade/estado divergentes; lucro positivo/negativo/zero e taxa exata; OPEN não liquida, SETTLED não repete consulta; ordem fora de demo não consulta; registro, rollback e idempotência em SQLite/PostgreSQL efêmeros com venue mockado. CI existente integral, cobertura sem exclusões, auditoria pt-BR. Deliverables: commit, PR #1, Evidence Bundle, logs e riscos. Stop: CI incompleto/falha ou defeito relevante, não avançar, mergear, promover checkpoint, operar ou gastar. Rollback por revert explícito sem force-push; não reabrir ordens liquidadas por rollback de código.

## Evidence Bundle preliminar
NOT_TESTED até resultados do SHA correto. Sem nova infraestrutura. Não certifica corretora oficial, concorrência distribuída, reconciliação Kalshi ou política de taxas divergentes. Checkpoint não promovido.
