# Binance Guardian

Bot de trading **Spot** orientado a seguridad, con arquitectura limpia y dashboard local.
Arranca en modo `paper`, no usa apalancamiento y no necesita credenciales para probarse.

Documentación: [arquitectura](docs/ARCHITECTURE.md),
[operación](docs/RUNBOOK.md) y [seguridad](SECURITY.md).

## Puesta en marcha

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
python -m guardian.main
```

Abra <http://127.0.0.1:8000>. En modo paper usa precios públicos de Binance y simula
las operaciones con comisiones. El bot no comienza a operar hasta pulsar **Iniciar**.

El dashboard separa Vista general, Trading, Operaciones, Investigación y Riesgo/Sistema.
Incluye selector de temporalidad y ventana, marcadores BUY/SELL, equity y distribución,
señal razonada, filtros de telemetría y ledger, además de alertas por datos atrasados.
La versión 0.9 añadió carteras sombra persistentes para seis familias, progreso hacia la
elegibilidad, curvas de equity, drawdown, Sharpe/Sortino por ciclo, exposición, comparación
contra el mercado y un ledger forward separado para comprobar qué hace cada challenger.
La versión 0.10 compara además enfoques compatibles en 15 minutos y una hora; presenta
los resultados netos fuera de muestra por familia y un resumen visible de progreso,
incluida la mejor cartera sombra. Un resultado positivo de una cartera pequeña no se
presenta como evidencia suficiente para operar dinero real.
Se adapta a escritorio y móvil.

## Entrenamiento de parámetros

Al iniciar, el bot guarda cada vela cerrada y su decisión como una observación única.
Cada 24 horas (configurable) evalúa 102 variantes explicables de seis familias Spot
long-only: cruce de medias, ruptura, momentum, reversión, retroceso y régimen adaptativo.
Compara relojes de 15 minutos y una hora; el modelo adaptativo decide únicamente en
15 minutos porque construye su régimen horario con velas cerradas de ese tamaño.
Usa 35.040 velas de 15 minutos (aproximadamente un año). Mantiene el
30% final completamente fuera de muestra y compara los candidatos en cinco ventanas
cronológicas purgadas dentro del 70% de desarrollo. La señal se llena en la apertura
de la vela siguiente y el simulador incorpora comisión, spread, slippage, stop-loss,
take-profit y trailing stop con un orden intravela conservador. Los candidatos adaptativos
también exigen volatilidad suficiente para superar el costo estimado de ida y vuelta.

El máximo de una vela actualiza el stop móvil para la vela siguiente; no se presume
que ese máximo ocurrió antes del mínimo de la misma vela. Los resultados históricos
anteriores a esta corrección deben recalcularse con **Entrenar ahora** para compararlos
con las reglas actuales. Las ejecuciones sombra se registran al detectar la siguiente
vela abierta, sin esperar a su cierre; el precio de apertura es una aproximación
del simulador, no una garantía de ejecución real. Sus saldos son independientes
del saldo paper principal. Si el proceso estuvo apagado, las velas perdidas se omiten:
no se reconstruyen compras, ventas ni stops que el bot no pudo ejecutar en tiempo real.

Un modelo sólo se aprueba si tiene al menos ocho ciclos de validación, retorno neto y
exceso contra comprar y mantener positivos, profit factor mínimo de 1,10, tres de cinco
ventanas positivas y confianza ajustada por selección mínima de 70%. El `Research Gate`
continúa siendo obligatorio en Testnet. En paper, el modo experimental permite ejecutar
un challenger no aprobado sólo cuando reúne al menos ocho operaciones de validación;
se marca como `RESEARCH` y conserva los límites de riesgo. Si no reúne esa muestra,
la cartera principal observa y las carteras sombra siguen probando sus modelos.

El resultado puede ser `CANDIDATO`, `RECHAZADO` o `MUESTRA INSUFICIENTE`. Por
También puede ejecutar el análisis con **Entrenar ahora**. En paper, el challenger de
investigación puede adoptarse automáticamente cuando no existe una posición abierta.
Si el resultado alcanza la categoría de candidato, puede promoverlo explícitamente con
el motor detenido; la selección queda auditada y persiste al reiniciar.

Después de cada entrenamiento, el líder de cada familia inicia una cartera sombra
independiente. Estas carteras no
reconstruyen operaciones del pasado: comienzan
en el momento de su registro, esperan una señal en vela cerrada y ejecutan en la apertura
de la vela siguiente. Guardan posición, comisiones, slippage y P&L en SQLite, por lo que
reiniciar el servicio no borra su experiencia. Un cohort permanece congelado al menos
siete días para impedir que cada entrenamiento reinicie la prueba. La promoción forward
es manual, sólo para la cartera paper principal y exige, por defecto, siete días, 30
ciclos cerrados, P&L positivo y profit factor mínimo de 1,10. Estos son límites operativos
configurables, no una garantía estadística ni de rentabilidad.

El tablero compara cada líder de familia, elegido sólo con el tramo de desarrollo,
en el período reservado. No selecciona retrospectivamente al que mejor salió en ese
período: hacerlo contaminaría la validación. Grid y DCA son bots populares, pero no se
copian como si fueran una garantía de retorno: el grid necesita gestión de varios niveles
y falla en tendencias fuertes; DCA acumula exposición y persigue otro objetivo.

## Protección de posiciones

En paper, cada compra queda protegida por un stop-loss de 1,5%, take-profit de 3,0%
y trailing stop de 1,2%, todos configurables. Las salidas protectoras pueden cerrar
una posición aunque ya se haya alcanzado el máximo de entradas diarias. El calendario
de riesgo usa `America/Costa_Rica` y se reinicia a medianoche local.

## Modos

- `paper`: simulación local (predeterminado).
- `testnet`: envía órdenes a Binance Spot Testnet; requiere claves de Testnet.
- `live`: dinero real, **intencionalmente deshabilitado en esta versión**. Los stops
  actuales dependen del proceso local; no se habilitará producción hasta incorporar
  protección nativa en Binance y reconciliación por el canal de eventos de la cuenta.

Para Testnet, cree una clave específica y configure `BINANCE_API_KEY` y
`BINANCE_API_SECRET` en `.env`. Para producción, permita únicamente lectura y Spot
trading, desactive retiros y restrinja la clave por IP. Nunca pegue secretos en el
dashboard, código, Git ni chat.

## Controles incorporados

- Bloqueo doble para dinero real y rechazo de configuraciones inseguras.
- Límites de tamaño, posición, pérdida diaria, operaciones diarias y enfriamiento.
- Validación de `LOT_SIZE`, `MARKET_LOT_SIZE`, `MIN_NOTIONAL`/`NOTIONAL`.
- Identificadores idempotentes de cliente, firma HMAC-SHA256 y `recvWindow` corto.
- Reconciliación por `clientOrderId`: un timeout nunca vuelve a enviar la misma intención;
  si Binance no confirma su estado, se activa una parada de emergencia persistente.
- Las compras autenticadas requieren un candidato aprobado fuera de muestra y que sus
  parámetros coincidan exactamente con la estrategia activa.
- Paper utiliza por defecto el mismo `Research Gate` y fills adversos configurables;
  puede desactivarse sólo para experimentos explícitos con capital ficticio.
- Guardian lleva una posición propia separada: jamás interpreta todo el BTC libre de
  la cuenta como suyo ni vende activos preexistentes del usuario.
- Estado de emergencia persistente, auditoría SQLite y secretos solo por entorno.
- API local con token obligatorio si se escucha fuera de loopback.

## Comandos de calidad

```powershell
pytest
ruff check .
```

No hay estrategia que garantice ganancias. Antes de dinero real, mantenga el bot en
paper/Testnet durante semanas y revise slippage, comisiones, desconexiones y resultados.
El laboratorio cuantitativo compara un conjunto curado de seis familias en 15m, con
régimen horario cuando corresponde. Cada candidato usa el mismo importe fijo para
ejecución, costos adversos, ejecución en la siguiente vela y validación walk-forward
fuera de muestra. Reducir el espacio de búsqueda acelera el análisis y limita el riesgo
de sobreajuste; una recomendación no implica rentabilidad futura.
