# CAU-REL-WO-001 | Convergência para produção

Status: IMPLEMENTED candidate / NOT_TESTED / checkpoint não promovido. Risco HIGH_ASSURANCE. Objetivo do proprietário: disponibilizar sistema funcional em produção, incluindo demo e futura execução real. A mudança de prioridade encerra a busca sequencial de 100% de instruções; cobertura é evidência, não substitui segurança nem aceitação.

## Context Lock
- MVP: 410f62795ec352e440b4323ccb3b7c4ad6009b0c (PR #1; CD-33 APPROVED, 1371 Python, 4167/4395).
- Hospedagem: 7e6faee7c00cfd65628379576011e51f54a5ff8d (PR #2; CI 44780713468/44780702071).
- Ancestral comum: d5521af81b922ca80ffb35910cca1828b9d4abe1.
- docs/11-checkpoint.md: b2f0247d36514b625b203cecf587432a957b2a66
- docs/10-decisions-ledger.md: 2db4cb6daa284c8bffce82cdfca1a4efe8ea4ce4
- docs/03-scope.md: 7084e1c86918b7716c023dc947c7a24efa28ce8a
- docs/09-definition-of-done.md: 6e73ebfa3a385fc7e49ff56f372856f84537ff15
- docs/04-architecture.md: d536ac3f1ba924ef80299e6569d8b0ab25eff78d
- docs/02-requirements.md: 4c8e80e8aa65cacc8040ec0dd96a46e385556c34
- Fontes: PRs #1/#2, docs/managed-production-wo.md, docs/cloudflare-free.md, docs/cloudflare-production.md, docs/research.md, issues #3–#14.

## Escopo e arquitetura
NECESSARY: reunir alterações das duas branches em candidata independente, conservar histórico via dois pais, manter as proteções do MVP e recursos de pesquisa/conta da hospedagem, corrigir a comparação de CSRF por comprimento em bytes (#13). Oito arquivos compartilhados: CI, demo_execution, Deriv/Kalshi, market_data, worker, testes de segurança e de execução. Os 63 arquivos exclusivos da PR #2 são incorporados por blob. Conflitos explícitos: preservar identificação do ambiente demo e validação de confirmação; incluir metadados de timestamp e payout; histórico Deriv ordenado e dentro do intervalo; observações atuais sem antecipar candle de confirmação; book levels somente com book válido; trava PostgreSQL transacional para worker e journal; conservar ambas as suítes. Nenhuma permissão de ordens reais é criada; REAL_READ_ONLY permanece intacto. Não reduzir limites ou ignorar CI. 
## Proof obligations / acceptance
Executar CI integral, incluindo suíte Cloudflare, Docker gerenciado, migrações e PostgreSQL, E2E, frontend, scanners e regressão de testes das duas branches; investigar incompatibilidades de fixtures/contratos, inclusive timestamps, fonte, quantidade, client_order_id e expectancy. Corrigir no mesmo WO, sem exclusões artificiais. Auditar alterações de contrato, migrations e escopo. Antes do deploy: backup/restore, confirmação de runtime/segredos via canal protegido, isolamento de credenciais, plano/custos, health, sessão e rollback; nenhuma despesa ou operação financeira neste incremento. Não confundir o portal Worker Free (pesquisa/contas) com execução contínua Python. 
## Gates externos
Kalshi demo autenticação rejeitada anteriormente (#5); Deriv saldo demo observado, ciclo de ordem não comprovado (#6). Qualificação empírica e bootstrap de calibração (#7): atual gate exige liquidações demo oficiais para operar; não contornar. Revisão independente HIGH_ASSURANCE, triagem ECC, recuperação de indisponíveis (#10), fronteiras de dados (#9) e demais achados precisam de evidência. O pedido de produção não substitui aceitação desses gates nem constitui ordem financeira. Escopo demo-only canônico permanece até ADR e implementação separados, com limites, idempotência, segregação, controle explícito e testes de segurança reais sem envio de ordens. Realização de deploy depende de acesso legítimo à infraestrutura; nenhuma credencial no Git.
## Entregáveis / stop condition
Branch e PR candidata, CI, Evidence Bundle pt-BR e correções necessárias. Stop antes de integrar/deployar se qualquer gate falhar. Não promover checkpoint antes de auditoria. Rollback de candidata: manter PRs #1/#2 e ambiente atual; após release, rollback à versão anterior somente após verificar compatibilidade de esquema e exposição.
