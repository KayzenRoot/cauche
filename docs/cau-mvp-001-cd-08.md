# CAU-MVP-001/CD-08 — identidade de execução e preflight do worker

Status PROPOSED / NOT PROMOTED. HIGH_ASSURANCE; F02/#4 e F06/#8 parcialmente relacionadas. CD-07 APPROVED no comentário 7137339979, CI 45434133268: 342 Python + 4 E2E, demo_execution 91%, worker 71%, backend 82% statements.

## Context Lock
Main a8e69195192a0abda82298927f0b0f231a86ea7b; base CD 6bebc20c846dbc56da64eb2e536c9a2eb0acdf71; PR #2 head 7e6faee7c00cfd65628379576011e51f54a5ff8d preservado.
Fingerprints: checkpoint b2f0247d36514b625b203cecf587432a957b2a66; ledger 2db4cb6daa284c8bffce82cdfca1a4efe8ea4ce4; scope 7084e1c86918b7716c023dc947c7a24efa28ce8a; DoD 6e73ebfa3a385fc7e49ff56f372856f84537ff15; architecture d536ac3f1ba924ef80299e6569d8b0ab25eff78d; requirements 4c8e80e8aa65cacc8040ec0dd96a46e385556c34.

## Work Order
Objetivo NECESSARY: falhar fechado antes de consulta remota quando identidade bot/run ou configuração persistida for inválida; descartar observação se escopo, versão/parâmetros ou identidade da conexão mudarem fora da transação; não expirar replay em observação live ausente. Ler worker, models, commands, market_data, execution, testes e fontes canônicas. Arquivos: backend/app/worker.py, backend/tests/test_demo_recovery.py, este documento. Fora de escopo: mudança de schema/adapter, estratégias, fencing distribuído, recuperação histórica, produção, PR #2, operações financeiras. Preservar commit da barreira anterior, rechecagem de risco, claim durável e lock do CD-07. Não registrar ciphertext em logs; comparar em memória exclusivamente para detectar alteração. Se run/cursor mudou, manter retorno benigno existente; se dados do mesmo run divergirem, erro seguro. Não alterar contrato do saldo ou validar feed oficial neste delta.

Proof obligations: ausência de configuração/escopo divergente não chama client/observe; alteração durante observe não grava mercado nem cria intent; ausência live não encerra run ou avança cursor; nenhuma submissão mockada; 19 cenários × dois bancos, PostgreSQL efêmero e SQLite. CI integral, lint, segurança, E2E, cobertura sem exclusões; revisão pt-BR. Não certificar fencing ou segurança distribuída. Entregáveis: commit, CI, Evidence Bundle e parecer. Stop: CI incompleto/falha/risco relevante, sem novo delta, merge, deploy, despesas, operações financeiras ou checkpoint. Rollback por revert explícito sem force-push.

## Evidence Bundle preliminar
NOT_TESTED até logs do novo SHA. Sem nova infraestrutura, credenciais, dependências ou schema. F02/F06 permanecem abertos, revisão independente e validação oficial pendentes. Checkpoint não promovido.
