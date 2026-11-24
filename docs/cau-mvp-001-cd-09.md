# CAU-MVP-001/CD-09 — escopo e roteamento da reconciliação demo

Status PROPOSED / NOT PROMOTED. HIGH_ASSURANCE; F01/#3 e F02/#4 parciais. CD-08 APPROVED no comentário 7137481159, CI 45435021710: 380 Python + 4 E2E, demo_execution 91%, worker 76%, backend 82% statements.

## Context Lock
Main a8e69195192a0abda82298927f0b0f231a86ea7b; base CD 8ce7ddc32ba733190236371dba781f066632d382; PR #2 head 7e6faee7c00cfd65628379576011e51f54a5ff8d preservado.
Fingerprints: checkpoint b2f0247d36514b625b203cecf587432a957b2a66; ledger 2db4cb6daa284c8bffce82cdfca1a4efe8ea4ce4; scope 7084e1c86918b7716c023dc947c7a24efa28ce8a; DoD 6e73ebfa3a385fc7e49ff56f372856f84537ff15; architecture d536ac3f1ba924ef80299e6569d8b0ab25eff78d; requirements 4c8e80e8aa65cacc8040ec0dd96a46e385556c34.

## Work Order
Objetivo NECESSARY: antes de consulta/cancelamento na reconciliação atual, conferir modo demo, adaptador correspondente, escopo das intenções ambíguas e ordens não liquidadas, vínculo à intenção confirmada, origem, moeda e identidade do broker; validar todos os candidatos antes de I/O; revalidar ordens após recuperação. Ler demo_execution, worker, execution, models, testes de recuperação e fontes canônicas. Arquivos: backend/app/demo_execution.py, backend/tests/test_demo_recovery.py, este documento. Fora de escopo: resolução histórica (run diferente), schema, adapters, regras de estratégia, credenciais, produção/PR #2, operações financeiras. Preservar a barreira durável, não reenviar claims, não ignorar ordem atual não liquidada sem broker_id. Registros incompatíveis bloqueiam a reconciliação (sem reparo automático). Ordem CANCELLED sem fills segue terminal e Deriv OPEN continua leitura. 

Proof obligations: claims divergentes bloqueiam I/O e mantêm UNKNOWN; ordens divergentes não chegam ao adapter; adaptador errado/mode inválido/run ausente rejeitados; ordem inválida impede até recuperação de claim válido; rotas positivas e skip terminal; SQLite/PostgreSQL com adapters mockados, CI completo, segurança/E2E e cobertura sem exclusões artificiais. 22 cenários por dialeto (44 casos previstos). Não provar reconciliação histórica, identidade de fill/fee/position além dos gates existentes, fencing ou execução remota única. Entregáveis: commit, CI, Evidence Bundle, auditoria pt-BR. Stop: CI incompleto/falha/risco relevante, sem outro delta, merge, deploy, despesas, operações financeiras ou checkpoint. Rollback via revert explícito sem force-push.

## Evidence Bundle preliminar
NOT_TESTED até logs deste SHA. Sem nova infraestrutura, schema, dependências ou permissões. F01/F02 abertas; revisão independente, aceitação oficial e cobertura branch/frontend pendentes. Checkpoint não promovido.
