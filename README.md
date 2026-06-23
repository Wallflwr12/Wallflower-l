Wallflower

Regime adaptive crypto trading agent. Runs across BTCUSDT, ETHUSDT and SOLUSDT simultaneously.

How It Works

Reads market regime before any signal fires. Bear, bull, or choppy. Choppy means no trade.

Bear regime: waits for price to bounce into key resistance with volume declining. That bounce is failing. Agent shorts it.

Capitulation: RSI floors while volume explodes above 2x average. Market is flushing. Agent goes long.

Bull regime: EMA crossover with volume confirmation. Follows momentum.

Single hard rule: BTC drops aggressively on one candle, everything closes. No exceptions.

Built With
Bitget Playbook. GetAgent SDK. MuleRun. Qwen API.

Live Strategy
https://www.bitget.com/playbook/243d723c-b121-494b-9a31-3f591a15e155
