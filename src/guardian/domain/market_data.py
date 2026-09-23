from guardian.domain.models import Candle


def validate_candles(candles: list[Candle], interval: str, minimum: int = 2) -> None:
    """Reject corrupt or discontinuous fixed-interval data; never interpolate prices."""
    units = {"m": 60, "h": 3600, "d": 86400}
    if not interval or interval[-1] not in units or not interval[:-1].isdigit():
        raise ValueError("Intervalo de mercado no compatible")
    seconds = int(interval[:-1]) * units[interval[-1]]
    if seconds <= 0 or len(candles) < minimum:
        raise ValueError("Datos de mercado insuficientes para evaluar una señal")
    previous = None
    for candle in candles:
        if candle.open_time.utcoffset() is None:
            raise ValueError("Vela sin zona horaria")
        stamp = candle.open_time.timestamp()
        if stamp % seconds != 0:
            raise ValueError("Vela fuera del intervalo del reloj UTC")
        if previous is not None and stamp - previous != seconds:
            raise ValueError("Velas duplicadas, desordenadas o con huecos temporales")
        previous = stamp
        prices = (candle.open, candle.high, candle.low, candle.close)
        if any(not value.is_finite() or value <= 0 for value in prices):
            raise ValueError("Vela con precio inválido")
        if not candle.volume.is_finite() or candle.volume < 0:
            raise ValueError("Vela con volumen inválido")
        if not candle.low <= min(candle.open, candle.close) <= max(
            candle.open, candle.close
        ) <= candle.high:
            raise ValueError("Máximo y mínimo de vela inconsistentes")
