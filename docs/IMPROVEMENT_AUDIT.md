# Auditoría de mejoras v0.5

Fecha de verificación: 2026-09-11.

## Implementado y verificado

| Requisito | Evidencia |
|---|---|
| Paridad de capital | Backtest y ejecución usan órdenes fijas de 10 USDT; el efectivo restante no se expone. |
| Cinco familias | Cruce SMA, ruptura de canal, momentum EMA+RSI, reversión a la media y retroceso en tendencia. |
| Cuatro temporalidades | 1m, 5m, 15m y 1h; el intervalo forma parte del modelo desplegado. |
| Historia ampliada | 129.600 velas de un minuto, aproximadamente 90 días, por entrenamiento. |
| Selección robusta | 444 candidatos, tres ventanas walk-forward purgadas y 30% final fuera de muestra. |
| Actividad mínima | El ranking penaliza modelos cuyo desarrollo no puede sostener ocho operaciones de validación. |
| Costos realistas | Comisión, medio spread, slippage, siguiente apertura y stops OHLC conservadores. |
| Benchmark comparable | Comprar y mantener utiliza los mismos 10 USDT que una entrada del bot. |
| Entrenamiento no bloqueante | La actualización automática corre aparte del ciclo de mercado. |
| Campeón–retador | Un retador rechazado no reemplaza un campeón aprobado y vigente. |
| Experimental paper | Un modelo positivo con PF ≥1, 8 operaciones y 2/3 ventanas positivas puede probarse automáticamente solo con capital ficticio. |
| Separación de seguridad | Testnet y live continúan exigiendo aprobación estricta; live permanece deshabilitado. |
| Dashboard adaptativo | Muestra familia, parámetros, intervalo, nivel experimental y overlays propios del modelo. |

Verificación ejecutada:

- 41 pruebas automatizadas aprobadas.
- `ruff check .`, `compileall`, `node --check` y `git diff --check` aprobados.
- Corrida real con 129.599 velas y 444 candidatos completada en 154,6 segundos.
- Resultado final: SMA 12/40 en 1h, +0,0335 USDT, 11 ciclos, PF 1,0747 y 2/3 ventanas positivas.
- El modelo califica para experimento paper, pero no para Testnet/live porque no superó el benchmark,
  PF 1,10 ni la confianza ajustada.

## Pendiente antes de dinero real

1. Órdenes protectoras nativas OCO/OTOCO verificadas en Spot Testnet.
2. User Data Stream autenticado y reconciliación continua de fills, balances y órdenes abiertas.
3. Conversión exacta a USDT de comisiones cobradas en BNB u otro activo.
4. Pruebas de fallos de red, reconexión y reinicio durante al menos 30 días en Testnet.
5. Validación de seis a doce meses y distintos regímenes de mercado.
6. Investigación separada de sizing y stops basados en ATR.

Ninguna búsqueda garantiza rentabilidad futura. El nivel experimental existe para recopilar evidencia
paper sin presentar una señal débil como estrategia aprobada.
