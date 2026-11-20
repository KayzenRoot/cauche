# Pesquisa e validação de estratégias

A aba **Inteligência** está disponível no terminal e no console Cloudflare Free. Ela combina o diário de evidências, a comparação fora da amostra e a leitura de regras e fontes da Kalshi. Os dados sintéticos são identificados na interface. Resultados importados refletem a qualidade e a origem declaradas no arquivo. A taxa de acerto acompanha o resultado líquido, o número de operações e o drawdown.

## Fluxo

1. No terminal Docker, rode **Replay** ou **Backtests** para observar a estratégia antes de usar a demo. O diário registra sinais aceitos e descartados, horários, fontes, termos observados, revisões de risco, estimativa conservadora de valor esperado quando disponível, resultados e execução.
2. Confira no backtest as janelas de treino, validação e teste, a janela final reservada, os candidatos avaliados, a qualidade da execução e a comparação de calibração entre referência, regressão logística e árvore. Os modelos candidatos de pesquisa ficam em sombra. O motor exige evidência suficiente e vantagem líquida conservadora ao avaliar novas entradas com dados de mercado.
3. No Cloudflare Free, consultar uma regra da Kalshi registra no PostgreSQL os dados públicos observados naquele momento e até cinco níveis por lado do livro quando disponível. A consulta é sob demanda. O console também aceita arquivos de pesquisa para calcular resultados sem rodar a CPU de um backtest no Worker.
4. Na demo oficial, verifique e configure a conta da corretora no terminal Docker, seguindo o fluxo de autenticação dela. Compare a execução observada com a simulação e mantenha a causa de bloqueios de risco visível. O modo de conta real do console faz consultas de leitura. O motor contínuo permanece no terminal Docker.

## DeepSeek V4.1 Flash

A API usa `deepseek-flash` em `https://api.deepseek.com/chat/completions`. Ela só é acionada quando o proprietário solicita a análise de um mercado e o ambiente tem uma chave configurada. As entradas são regras públicas e trechos de fontes que o operador fornece. O servidor valida datas e citações textuais. A resposta exige revisão humana e fica fora do fluxo de ordens e do cálculo de probabilidades.

Para o terminal Docker, configure `DEEPSEEK_API_KEY` no arquivo privado de ambiente usado pelo serviço `api`. Para a versão Cloudflare Free, configure `DEEPSEEK_API_KEY` como segredo do Worker:

```sh
cd cloudflare
npx wrangler secret put DEEPSEEK_API_KEY --config wrangler.free.jsonc
```

A chamada direta usa a conta DeepSeek do proprietário e a tarifação do fornecedor. O plano de hospedagem pode permanecer gratuito. Sem a chave, o painel consulta e registra regras e livro da Kalshi e indica a ausência de extração pelo modelo.

[DeepSeek API](https://api-docs.deepseek.com/en/) · [Regras na API da Kalshi](https://docs.kalshi.com/api-reference/market/get-market) · [Livro de ofertas da Kalshi](https://docs.kalshi.com/api-reference/market/get-market-orderbook)
