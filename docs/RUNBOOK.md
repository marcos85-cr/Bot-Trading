# Guía operativa

## Operación normal

1. Mantenga `TRADING_MODE=paper` durante la recopilación y evaluación.
2. Pulse **Entrenar ahora** para descargar el histórico configurado; las observaciones
   en vivo son telemetría incremental, no el único dataset de investigación.
3. Revise **Carteras sombra**: las cinco familias operan hacia adelante aunque la cartera
   principal esté bloqueada. “Esperando señal” es normal; BUY/SELL y sus resultados aparecen
   en el ledger forward cuando se producen.
4. Un resultado `EXPERIMENTAL PAPER` puede operar automáticamente durante 24 horas únicamente
   con capital ficticio. Un resultado histórico rechazado no se promueve directamente.
5. Cuando una cartera sombra muestre `ELEGIBLE`, detenga el motor y use **Promover a paper**.
   La promoción nunca cambia el modo a Testnet ni habilita dinero real.
6. Observe retorno neto, exceso contra mercado, drawdown, profit factor, costos,
   robustez y razones de rechazo.
7. Pase a Testnet sólo con claves exclusivas de Testnet y una revisión independiente.

## Indicadores que requieren intervención

- `BLOQUEADO` o `ORDER_STATUS_UNKNOWN`: confirme la orden en Binance antes de rearmar.
- `Límite diario de entradas alcanzado`: no abre compras nuevas, pero sí permite salidas.
- `Research Gate activo`: el motor observa, registra velas y protege posiciones, pero no compra.
- `RECHAZADO`: el entrenamiento no demostró ventaja positiva suficiente.
- `EXPERIMENTAL`: existe señal positiva inicial, pero todavía no tiene evidencia para Testnet/live.
- API desconectada: el motor no debe considerarse supervisado.

## Verificación antes de iniciar

```powershell
python -m ruff check src tests
python -m pytest -q -p no:cacheprovider
python -m guardian.main
```

Abra `http://127.0.0.1:8000`. No exponga el dashboard a la red sin un
`DASHBOARD_TOKEN` aleatorio de al menos 32 caracteres y controles adicionales del host.
