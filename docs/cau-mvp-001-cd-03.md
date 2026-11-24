# CAU-MVP-001/CD-03 — fronteira de envio demo

Status PROPOSED / NOT PROMOTED. HIGH_ASSURANCE; F01/#3 parcial. Meta autorizada: cobertura total, interpretada como objetivo de 100% linhas e ramos alcançáveis do código próprio, sem mascarar exclusões e sem equiparar cobertura a segurança financeira. CI atual mede somente statements Python; métricas de branch/frontend e gates progressivos ainda não implementados (#11). CD-02 APPROVED, CI 45429334390, 179 Python + 4 E2E, demo_execution 53%, global 79%.

## Context Lock
Main a8e69195192a0abda82298927f0b0f231a86ea7b; PR #1 base CD b1bbb9122a55e4adb3fab2b45483effdb948226d; PR #2 head preservado 7e6faee7c00cfd65628379576011e51f54a5ff8d.
Fingerprints: checkpoint b2f0247d36514b625b203cecf587432a957b2a66; ledger 2db4cb6daa284c8bffce82cdfca1a4efe8ea4ce4; scope 7084e1c86918b7716c023dc947c7a24efa28ce8a; DoD 6e73ebfa3a385fc7e49ff56f372856f84537ff15; architecture d536ac3f1ba924ef80299e6569d8b0ab25eff78d; requirements 4c8e80e8aa65cacc8040ec0dd96a46e385556c34.

## Work Order
Objetivo NECESSARY: proteger a fronteira dispatch contra estados não elegíveis, divergência de escopo da intenção, conexão desabilitada e timestamp futuro; cobrir caminhos de sucesso, falhas e guardas sem rede. Ler demo_execution.py, worker.py, db.py, models.py, contratos de adaptadores, testes existentes e fontes canônicas. Arquivos: backend/app/demo_execution.py, backend/tests/test_demo_dispatch.py, este documento. Fora de escopo: recuperação durável, reconciliação, alterações de estratégia, produção/PR #2, schema, credenciais, ordens reais/demo, deploy, merge e checkpoint. Regras: somente worker com claim recém-persistido chama dispatch; UNKNOWN/CONFIRMED não são alterados ou reenviados; nunca autorizar envio pelo teste. UTC ingênuo interpretado conforme helper aware existente. Em caso de claim fresco inelegível, preservar cancelamento pré-envio existente; não usar dispatch para recuperar claims antigos.

Aceite: testes de guardas sem chamada de client/venue, modo e escopo coerentes, janela inclusiva 0–20 segundos, sucesso mockado das duas corretoras, erro conhecido/ambíguo sem retry, confirmação inválida deixa claim para recuperação. CI completo e revisão do diff, não prometer cobertura total após este incremento. Testes mockados não provam durabilidade ou ausência de duplicidade após crash. Entregáveis: commit, PR #1, Evidence Bundle com SHA, cobertura e logs; auditoria pt-BR; stop se CI incompleto/falha ou risco não resolvido. Rollback por revert explícito sem force-push. 

## Plano de cobertura (proposto, não execução)
Priorizar: dispatch (CD-03), recuperação durável PostgreSQL, reconciliação/liquidação, dados/feeds, demais caminhos backend, instrumentação branch e frontend, CI com gates crescentes e matriz de testes por risco. Cada incremento depende de aprovação do anterior; 100% não substitui validação demo ou revisão independente. Não excluir caminhos somente para elevar métrica. Sem alteração de checkpoint.

## Evidence Bundle preliminar
Resultados NOT_TESTED até leitura dos logs deste HEAD. Nenhuma operação externa, gasto ou implantação. 
