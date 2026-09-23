# Arquitectura

Guardian aplica dependencias hacia el dominio:

```text
presentation (FastAPI + dashboard)
             │
             ▼
application (motor + entrenamiento)
             │
             ▼
domain (modelos, estrategia, riesgo, puertos)
             ▲
             │
infrastructure (Binance, paper, SQLite, configuración)
```

El dominio no conoce HTTP, SQLite ni Binance. El motor depende de contratos (`ports`),
por lo que paper y Binance son adaptadores intercambiables. Las cantidades monetarias
operativas usan `Decimal`; las credenciales se encapsulan como secretos de configuración.

## Flujo seguro de una orden

1. Se evalúa únicamente una vela cerrada.
2. Se deduplica la intención por vela.
3. Riesgo valida pérdida diaria, tamaño, posición, entradas, frecuencia y emergencia.
4. Research Gate exige un modelo coincidente. Paper admite un nivel experimental positivo;
   Testnet y live requieren aprobación estricta.
5. Se persiste la intención y su `clientOrderId` antes de enviarla.
6. Binance recibe una sola solicitud.
7. Un timeout se consulta por `origClientOrderId`; nunca provoca otro envío.
8. Un estado no terminal se consulta hasta confirmar el resultado.
9. Si no puede probarse el estado, se activa la emergencia persistente.
10. Tras un reinicio, cualquier intención pendiente se reconcilia antes de arrancar.

La posición administrada por Guardian se registra aparte del saldo libre de la cuenta.
El bot no puede vender activos preexistentes que no haya adquirido.

## Flujo de investigación

1. Binance entrega velas de un minuto; el laboratorio construye barras de 5m, 15m y 1h.
2. El 70% inicial se usa para selección con tres ventanas walk-forward y embargo temporal.
3. Una señal confirmada se ejecuta en la apertura siguiente, nunca en su propio cierre.
4. Los fills incluyen comisión, medio spread y slippage adverso.
5. Los stops usan OHLC; si dos barreras aparecen en la misma vela se elige primero el
   resultado conservador.
6. El 30% final permanece fuera de selección y se compara contra comprar y mantener.
7. Se comparan 444 combinaciones de cinco familias con el mismo tamaño de orden operativo.
8. Retorno, exceso, profit factor, actividad, robustez y confianza ajustada deben superar
   simultáneamente sus umbrales para Testnet. Un resultado positivo más débil puede desplegarse
   durante 24 horas exclusivamente como experimento paper.

## Flujo de prueba forward

1. El mejor parámetro de cada familia y temporalidad crea una cartera sombra con identificador
   reproducible (5 familias × 4 temporalidades = 20 espacios independientes).
2. La cartera empieza en el presente: no convierte señales históricas en operaciones supuestas.
3. La decisión de una vela cerrada queda pendiente y se llena en la apertura de la siguiente.
4. Cada cartera conserva por separado saldo, posición, entrada, máximo, costos y P&L en SQLite.
5. Un cohort queda congelado durante siete días para evitar reemplazarlo al ver resultados malos.
6. Las carteras continúan aunque Research Gate bloquee la cartera paper principal.
7. La elegibilidad exige edad, cantidad de ciclos, P&L positivo y profit factor mínimo.
8. La promoción es manual, con el motor detenido, y sólo habilita paper durante siete días.

Entrenamiento histórico, prueba forward y ejecución principal no comparten saldos. Esta separación
evita que una prueba altere el capital mostrado por la estrategia activa.
