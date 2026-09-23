# Política de seguridad

## Alcance operativo

Esta versión está autorizada para `paper` y Binance Spot Testnet. El modo `live`
se rechaza durante la validación de configuración hasta que las salidas protectoras
sean órdenes nativas del exchange y exista reconciliación continua del canal de cuenta.

Ninguna estrategia elimina el riesgo de mercado. Los controles de este proyecto
reducen fallos de software, duplicación de órdenes y exposición accidental; no
garantizan rentabilidad ni disponibilidad de Binance.

## Credenciales

- Cree una clave exclusiva para el bot.
- Habilite sólo lectura y Spot trading; nunca retiros, margen o futuros.
- Restrinja la clave por IP cuando Binance lo permita.
- Guárdela únicamente en `.env`, que está excluido de Git.
- Rote la clave ante cualquier sospecha de exposición.

## Respuesta a incidentes

1. Pulse **Emergencia**. El bloqueo persiste después de reiniciar.
2. Revise “Qué está haciendo el bot” y el ledger.
3. Compruebe directamente en Binance las órdenes y balances.
4. Revoque la clave si hay actividad no reconocida.
5. No rearme hasta reconciliar cualquier orden con estado desconocido.

Los reportes de vulnerabilidad no deben incluir claves, secretos ni datos de cuenta.
