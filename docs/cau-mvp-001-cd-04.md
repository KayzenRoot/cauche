# CAU-MVP-001/CD-04 — recuperação durável de claims demo

Status PROPOSED / NOT PROMOTED. HIGH_ASSURANCE; F02/#4 parcial. CD-03 aprovado no comentário 7136785765, CI 45430186844: 214 Python + 4 E2E, demo_execution 62%, worker 57%, global 79% statements. Meta final 100% sem exclusões artificiais, sem equiparar cobertura a garantia de segurança.

## Context Lock
Main a8e69195192a0abda82298927f0b0f231a86ea7b; base CD a08a1630276f344ee78ab9c0a8d9741367d9458a; PR #2 head 7e6faee7c00cfd65628379576011e51f54a5ff8d preservado, mergeable=false.
Fingerprints: checkpoint b2f0247d36514b625b203cecf587432a957b2a66; ledger 2db4cb6daa284c8bffce82cdfca1a4efe8ea4ce4; scope 7084e1c86918b7716c023dc947c7a24efa28ce8a; DoD 6e73ebfa3a385fc7e49ff56f372856f84537ff15; architecture d536ac3f1ba924ef80299e6569d8b0ab25eff78d; requirements 4c8e80e8aa65cacc8040ec0dd96a46e385556c34.

## Work Order
Objetivo NECESSARY: preservar UNKNOWN e bloqueio em commit anterior à reconciliação, inclusive falha de lookup ou confirmação; evitar retry cego, bloquear nova execução demo na presença de ambiguidade de qualquer run e impedir recuperação repetida de estados terminais. Ler worker, demo_execution, db, models, events, commands, execution, CI, testes e fontes canônicas. Arquivos: worker.py, demo_execution.py, tests/test_demo_recovery.py, CI e este documento. Fora de escopo: recuperação manual de Deriv ou run histórico, reconciliação contábil completa, mudanças de schema/estratégia/credenciais, produção e PR #2. Regra: worker lease e row lock existentes; SUBMITTING após crash é desconhecido mesmo se o envio nunca ocorreu; não autorizar nova ordem. STOPPED preservado. Consulta histórica bloqueia nova execução, não usa venue do run atual para resolver claims antigos.

Proof obligations: estado UNKNOWN e pausa visíveis em sessão separada antes de lookup; persistem após erro, ausência ou ack inválido; rollback após ack mantém claim original e recuperação única mockada sem reenvio; tentativa de resume com claim histórico é bloqueada; conexão inválida não desfaz barreira; estados terminais não são consultados novamente. Testar SQLite e PostgreSQL efêmero com serviço no backend job existente (imagem fixada pelo digest já usado no Compose, loopback, schema aleatório em banco exclusivo de teste, sem segredos nem DATABASE_URL). CI falha se serviço PostgreSQL ausente; skip local somente se explicitamente não provisionado. Testes não simulam kill de processo nem falhas de rede real; não certificar exatamente uma execução remota. Regressão CI completo, segurança, lint, E2E e cobertura; nenhuma operação demo oficial.

Entregáveis: diff mínimo, commit PR #1, CI e Evidence Bundle com base/head, testes por dialeto, cobertura e parecer pt-BR. Stop: CI incompleto/falha/risco não resolvido; sem novo delta, merge, deploy, despesa ou promoção. Rollback: revert explícito do commit, sem force-push; manter execução demo pausada enquanto houver claim ambíguo.

## Evidence Bundle preliminar
NOT_TESTED até logs do CI deste HEAD. Sem migração ou novo serviço persistente. A cobertura de linhas não substitui revisão independente, concorrência/crash real, reconciliação ou validação oficial. Checkpoint delta: não promover.
