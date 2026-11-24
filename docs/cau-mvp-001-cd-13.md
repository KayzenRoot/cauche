# CAU-MVP-001/CD-13 — proveniência de mercado e pipeline do worker

Status PROPOSED / NOT PROMOTED. HIGH_ASSURANCE; F01/#3, F02/#4 e F06/#8 parciais. CD-12 APPROVED no comentário 7137924883; CI 45438176206: 568 Python + 4 E2E, demo_execution 100% (353/353), worker 90% (270/26 miss), backend 84% statements.

## Context Lock
Main a8e69195192a0abda82298927f0b0f231a86ea7b; base CD 72670659b66b91360df72291f5c69a26fe6a1b13; PR #2 head 7e6faee7c00cfd65628379576011e51f54a5ff8d preservado.
Fingerprints: checkpoint b2f0247d36514b625b203cecf587432a957b2a66; ledger 2db4cb6daa284c8bffce82cdfca1a4efe8ea4ce4; scope 7084e1c86918b7716c023dc947c7a24efa28ce8a; DoD 6e73ebfa3a385fc7e49ff56f372856f84537ff15; architecture d536ac3f1ba924ef80299e6569d8b0ab25eff78d; requirements 4c8e80e8aa65cacc8040ec0dd96a46e385556c34.

## Work Order
Objetivo NECESSARY: bloquear observação de mercado incompatível com exchange/instrument/source do run antes da persistência e cobrir caminhos críticos restantes do worker, incluindo ticker, pausa, saldo divergente, inventário market-making, risco e claim durável. Ler worker, market_data, execution, demo_execution, quant/risk, models e fontes canônicas. Arquivos: worker.py, test_demo_recovery.py, este documento. Preservar aliases FIXTURE: e contratos normalizados das fontes atuais; não alterar estratégia, risk engine, adapters, schema, permissões, dependências ou CI. Não certificar origem remota a partir do rótulo local; apenas consistência de escopo. Testar pipeline com observação e decisão controladas e submissão mockada, sem corretora real. Fora de escopo: fencing distribuído, alteração de identidade entre claim e dispatch, kill abrupto, PR #2, produção, finanças. Meta incremental rumo a 100% statements; não prometer cobertura completa antes de logs.
Proof obligations: quatro proveniências divergentes, pipeline real risco/ledger/claim antes da submissão simulada (allowed e blocked), pausa/ticker, pausa durante fixture, saldo divergente, 9 cenários por dialeto SQLite/PostgreSQL (18 novos). Preservar 100% demo_execution, sem exclusões artificiais. CI integral, regressão, segurança e E2E. Entregáveis: commit, Evidence Bundle, auditoria pt-BR. Stop: falha/CI incompleto/risco relevante; sem outro delta, merge, deploy, despesas, operações financeiras ou checkpoint. Rollback por revert explícito sem force-push.
## Evidence Bundle preliminar
NOT_TESTED até logs do SHA resultante. F01/F02/F06 abertas. Revisão independente, validação oficial e cobertura de branches/frontend pendentes.
