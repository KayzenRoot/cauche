# CAU-MVP-001/CD-02 — integridade monetária da confirmação demo

Status PROPOSED / NOT PROMOTED. HIGH_ASSURANCE. Issue #3 F01, incremento parcial. CD-01 aprovado no comentário 7136527379 e CI 45428407219 (138 testes).

## Context Lock
- Main a8e69195192a0abda82298927f0b0f231a86ea7b; PR #1 base deste delta a093b1697038d949c09a5537a6eb4498cc7199b7; PR #2 head intocado 7e6faee7c00cfd65628379576011e51f54a5ff8d.
- Fingerprints: checkpoint b2f0247d36514b625b203cecf587432a957b2a66; ledger 2db4cb6daa284c8bffce82cdfca1a4efe8ea4ce4; scope 7084e1c86918b7716c023dc947c7a24efa28ce8a; DoD 6e73ebfa3a385fc7e49ff56f372856f84537ff15; architecture d536ac3f1ba924ef80299e6569d8b0ab25eff78d; requirements 4c8e80e8aa65cacc8040ec0dd96a46e385556c34.

## Work Order
Objetivo NECESSARY: rejeitar confirmações normalizadas com valores ausentes, não finitos, negativos, precisão incompatível com Numeric(24,8), preço acima da intenção, quantidades inconsistentes ou identidade/proveniência divergente, antes de mutação contábil. Contexto: código CD-01; contratos normalizados Deriv submit_demo, Kalshi submit_demo/find_demo_order, quant/types.py, models.py, execution.py e risk.py. Escopo: acknowledge e testes de fronteira; arquivos backend/app/demo_execution.py, backend/tests/test_demo_acknowledgement.py e este documento. Fora do escopo: envio de ordens, reconciliação de fills/liquidação, recuperação transacional, alteração de estratégia/infraestrutura/schema, PR #2, credenciais, qualquer operação real ou demo, deploy e merge.

Requisitos e regras: preservar os contratos de adaptadores existentes, Decimal exato sem coerção float/bool e rejeição antes de criar ordem/fill/evento; Deriv quantidade 1 e OPEN, ausência dos campos opcionais de fee/fill/remaining conforme adapter; Kalshi quantidade igual à intenção, preenchida + restante <= total e estados consistentes, inclusive partial IOC sem restante; correlacionar source/client_order_id e instrumento/lado Kalshi. Fee não negativo; precisão de armazenamento aplicada somente a campos persistidos diretamente. Não confundir validação de confirmação com prevenção de execução remota ou orçamento de fee completo.

Aceite: testes positivos e negativos mockados, sem rede, nenhuma mutação em confirmação inválida; regressão CD-01 verde; CI existente completo (pytest/cobertura/Ruff/Bandit/pip-audit/frontend/Compose/E2E/docs/secrets). Deliverables: diff mínimo, testes, commit PR #1, Evidence Bundle com base/head, resultados e riscos, auditoria pt-BR. Stop: CI incompleto/falha/conflito/ausência de evidência; nenhuma promoção nem novo incremento antes de aprovação. Rollback via revert explícito sem force-push. 

## Evidence Bundle preliminar
IMPLEMENTED somente após publicação do commit; TESTED_WITH_MOCK somente após CI. Validação de fee contra orçamento total, reconciliação de fills, confirmação persistente após crash e validação oficial demo permanecem abertas. Checkpoint delta: proposta sem promoção; sem novas despesas. 
