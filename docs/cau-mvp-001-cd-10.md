# CAU-MVP-001/CD-10 — admissão de conta demo e saldo exato

Status PROPOSED / NOT PROMOTED. HIGH_ASSURANCE; F01/#3 e F02/#4 parciais. CD-09 APPROVED no comentário 7137591192, CI 45436098184: 424 Python + 4 E2E, demo_execution 92%, worker 76%, backend 82% statements.

## Context Lock
Main a8e69195192a0abda82298927f0b0f231a86ea7b; base CD 06f0769ae51bc6e82cf754789f5400b31ff88f8f; PR #2 head 7e6faee7c00cfd65628379576011e51f54a5ff8d preservado.
Fingerprints: checkpoint b2f0247d36514b625b203cecf587432a957b2a66; ledger 2db4cb6daa284c8bffce82cdfca1a4efe8ea4ce4; scope 7084e1c86918b7716c023dc947c7a24efa28ce8a; DoD 6e73ebfa3a385fc7e49ff56f372856f84537ff15; architecture d536ac3f1ba924ef80299e6569d8b0ab25eff78d; requirements 4c8e80e8aa65cacc8040ec0dd96a46e385556c34.

## Work Order
Objetivo NECESSARY: rejeitar configuração de adapter incompatível antes de descriptografar; validar campos de credencial descriptografada contra contrato de cada corretora; nunca tratar envelope inválido de inventário como conta vazia; exigir identidade de origem demo, moeda e saldo exato, não negativo, representável em MONEY Numeric(24,8) antes de contabilizar. Ler demo_execution, worker, comandos de credenciais, security, modelos, adapters verify_demo/portfolio/positions/open_orders, testes e fontes canônicas. Arquivos: demo_execution.py, worker.py, test_demo_account_admission.py, test_demo_recovery.py, este documento. Preservar contratos e proteções dos adapters; nenhuma credencial real, requisição externa, alteração de destino, schema, dependência, estratégia ou permissão. Conservar check de exposição não gerenciada do cursor inicial; não declarar conta vazia a partir de resposta malformada. Posição assinada ou ordem resting não vazia bloqueia admissão. Ajustar somente duas fixtures antigas verify_demo para incluir environment/source já devolvidos pelos adapters, sem reduzir suas asserções. Fora de escopo: reconciliação de saldo remoto com ledger, lacunas de feed, concorrência/fencing, recuperação histórica, produção, PR #2.

Proof obligations: construção de adapters demo, credenciais inválidas sem vazamento e exchange inválido sem decrypt, envelopes inválidos, exposição positiva/negativa e resting, falha de transporte, precisão e identidade de saldo, seis cenários de worker em SQLite/PostgreSQL impedindo contabilização e submissão. Testes unitários e integração isolados, CI completo, cobertura sem exclusões, auditoria pt-BR. Entregáveis: commit, CI, Evidence Bundle final, parecer. Stop: CI incompleto/falha/risco relevante, sem outro delta, merge, deploy, despesas, operações financeiras ou checkpoint. Rollback via revert explícito sem force-push.

## Evidence Bundle preliminar
NOT_TESTED até logs deste SHA. Sem nova infraestrutura. Admissão local não prova completude da resposta oficial, detecção de mudanças após cursor inicial ou saldo economicamente correto. F01/F02 abertas; revisão independente e cobertura branch/frontend pendentes. Checkpoint não promovido.
