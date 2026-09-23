const $ = id => document.getElementById(id);
let token = sessionStorage.getItem('guardianToken') || '';
let lastStatus = null;
let market = {candles: []};
let orderRows = [];
let eventRows = [];
let shadowRows = [];
let selectedShadowId = '';
let shadowIntervalFilter = 'ALL';
let chartWindow = 250;
let chartInterval = '';
let chartOffset = 0;
let chartPointer = null;
let chartGeometry = null;
let chartDragStart = null;
const chartLayers = {indicators: true, trades: true, volume: true};
let orderSideFilter = 'ALL';
let eventLevelFilter = 'ALL';
let eventStreamActive = false;
let eventStreamController = null;
let mainEquityCurve = [];

const headers = () => token ? {Authorization: `Bearer ${token}`} : {};
const number = value => Number(value || 0);
const money = (value, digits = 2) => number(value).toLocaleString('es-CR', {minimumFractionDigits: digits, maximumFractionDigits: digits});
const compact = value => number(value).toLocaleString('es-CR', {maximumFractionDigits: 8});
const escapeHtml = value => String(value).replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
const percent = (value, maximum) => Math.max(0, Math.min(100, maximum ? number(value) / number(maximum) * 100 : 0));

async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {...headers(), ...(options.headers || {})}});
  if (response.status === 401 && !token) {
    token = prompt('Token seguro del dashboard') || '';
    sessionStorage.setItem('guardianToken', token);
    return api(path, options);
  }
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || 'Error inesperado');
  return body;
}

function renderStatus(s) {
  lastStatus = s;
  renderShadow(s.shadow || {enabled:false, models:[], trades:[]});
  const benignCycleMessage = s.last_error === 'Señal ya procesada para esta vela';
  const operationalError = s.last_error && !benignCycleMessage ? s.last_error : '';
  const systemProblem = s.emergency_stop || operationalError || (s.running && !s.runtime?.cycle_healthy);
  $('systemAlert').className = `system-alert${systemProblem ? ' visible' : ''}`;
  $('systemAlert').textContent = s.emergency_stop
    ? 'Parada de emergencia activa: no se permiten nuevas operaciones.'
    : operationalError || (systemProblem ? 'Los datos del motor están atrasados; revise la conexión.' : '');
  $('connection').innerHTML = '<i></i>API conectada';
  $('connection').className = 'status-pill live';
  $('symbol').textContent = s.symbol;
  $('price').textContent = s.last_price ? `$${money(s.last_price)}` : '—';
  $('chartPrice').textContent = s.last_price ? `$${money(s.last_price)}` : '—';
  const runtimeState = s.runtime?.operating_state || (s.running ? 'trading' : 'stopped');
  const runtimeLabels = {blocked:'BLOQUEADO', stopped:'DETENIDO', observing:'OBSERVANDO', experimenting:'EXPERIMENTAL', trading:'OPERANDO'};
  $('running').textContent = runtimeLabels[runtimeState] || '—';
  $('running').style.color = runtimeState === 'blocked' ? 'var(--red)' : runtimeState === 'trading' ? 'var(--green)' : ['observing', 'experimenting'].includes(runtimeState) ? 'var(--amber)' : 'var(--muted)';
  $('engineLight').className = `engine-light${s.running ? ' on' : ''}`;
  const engineBadgeState = s.emergency_stop ? 'blocked' : runtimeState === 'experimenting' ? 'experimental' : s.running ? 'active' : 'stopped';
  $('engineStateBadge').className = `engine-state-badge ${engineBadgeState}`;
  $('engineStateText').textContent = s.emergency_stop
    ? 'EMERGENCIA ACTIVA'
    : runtimeState === 'experimenting'
      ? 'ACTIVO · PAPER RESEARCH'
      : s.running ? 'MOTOR ACTIVO' : 'MOTOR DETENIDO';
  $('lastCycle').textContent = s.last_cycle_at
    ? `${s.runtime?.message || 'Ciclo completado'} · ${new Date(s.last_cycle_at).toLocaleTimeString()}`
    : 'Esperando ciclo';
  $('interval').textContent = s.strategy.interval;
  $('timeframe').textContent = s.strategy.interval;
  $('chartTitle').textContent = `${s.portfolio?.base_asset || 'BTC'} / ${s.portfolio?.quote_asset || 'USDT'}`;
  const action = s.last_signal?.action || 'HOLD';
  $('signal').textContent = action;
  $('signal').className = `signal-badge ${action.toLowerCase()}`;
  $('reason').textContent = operationalError || s.last_signal?.reason || 'Esperando datos confirmados del mercado.';
  $('decisionContext').textContent = operationalError
    ? `Error operativo: ${operationalError}`
    : `${s.last_signal?.action || 'HOLD'} · vela cerrada · ${s.strategy.name} en ${s.strategy.interval} · ${s.strategy.execution_ready ? 'ejecución permitida' : 'sólo observación'}`;
  $('fast').textContent = s.last_signal?.fast_sma ? money(s.last_signal.fast_sma, 4) : '—';
  $('slow').textContent = s.last_signal?.slow_sma ? money(s.last_signal.slow_sma, 4) : '—';
  $('primaryLabel').textContent = s.strategy.primary_label || 'Indicador A';
  $('secondaryLabel').textContent = s.strategy.secondary_label || 'Indicador B';
  const chartLabels = {
    sma_crossover: [`SMA ${s.strategy.parameters?.fast_period}`, `SMA ${s.strategy.parameters?.slow_period}`],
    breakout: [`Canal ${s.strategy.parameters?.entry_period}`, `EMA ${s.strategy.parameters?.trend_period}`],
    momentum: [`EMA ${s.strategy.parameters?.fast_period}`, `EMA ${s.strategy.parameters?.slow_period}`],
    mean_reversion: [`Media ${s.strategy.parameters?.period}`, `Banda ${s.strategy.parameters?.deviation}σ`],
    trend_pullback: [`EMA ${s.strategy.parameters?.trigger_period}`, `EMA ${s.strategy.parameters?.trend_period}`],
    regime_adaptive: [`EMA 1h ${s.strategy.parameters?.trend_fast_period}`, `EMA 1h ${s.strategy.parameters?.trend_slow_period}`]
  };
  const activeChartLabels = chartLabels[s.strategy.kind] || ['Indicador A', 'Indicador B'];
  $('legendPrimary').textContent = activeChartLabels[0];
  $('legendSecondary').textContent = activeChartLabels[1];
  $('nextCycle').textContent = s.running ? `≤ ${s.strategy.cycle_seconds} segundos` : 'Motor detenido';
  const portfolio = s.portfolio;
  const topReady = s.strategy.execution_ready && !s.emergency_stop;
  const experimental = ['experimental', 'research'].includes(s.strategy.validation_level);
  $('topReadiness').textContent = experimental ? 'Experimento paper activo' : topReady ? 'Estrategia aprobada' : 'Research Gate activo';
  $('topReadiness').style.color = topReady && !experimental ? 'var(--green)' : 'var(--amber)';
  $('topReadinessReason').textContent = s.emergency_stop
    ? 'Parada de emergencia activa'
    : s.strategy.execution_blocker || (s.mode === 'paper' ? 'Paper validado para simular' : 'Controles verificados');
  $('managedPositionTop').textContent = portfolio
    ? `${compact(portfolio.managed_base)} ${portfolio.base_asset} administrados`
    : 'Sin posición administrada';
  $('equity').textContent = portfolio ? `${money(portfolio.equity_quote)} ${portfolio.quote_asset}` : '—';
  $('equityAsset').textContent = portfolio ? `${money(portfolio.quote_free)} ${portfolio.quote_asset} libres` : 'Saldo disponible al iniciar';
  $('baseAsset').textContent = portfolio?.base_asset || 'BTC';
  $('baseLabel').textContent = portfolio?.base_asset || 'BTC';
  $('quoteLabel').textContent = portfolio?.quote_asset || 'USDT';
  $('baseBalance').textContent = portfolio ? compact(portfolio.base_free) : '0';
  $('quoteBalance').textContent = portfolio ? compact(portfolio.quote_free) : '—';
  const position = s.position;
  $('chartPosition').hidden = !position;
  if (position) {
    const unrealized = number(position.unrealized_pnl_quote);
    $('chartPositionPnl').textContent = `${unrealized >= 0 ? '+' : ''}${money(unrealized, 4)} ${portfolio?.quote_asset || 'USDT'}`;
    $('chartPositionPnl').style.color = unrealized >= 0 ? 'var(--green)' : 'var(--red)';
    const trailing = number(position.trailing_stop_price) > 0
      ? ` · Trailing $${money(position.trailing_stop_price)}`
      : '';
    $('chartPositionLevels').textContent = `Entrada $${money(position.entry_price)} · Stop $${money(position.stop_price)} · Objetivo $${money(position.take_profit_price)}${trailing}`;
  }
  const basePct = portfolio ? percent(portfolio.base_value_quote, portfolio.equity_quote) : 0;
  $('basePct').textContent = `${basePct.toFixed(1)}%`;
  $('allocation').style.background = `conic-gradient(var(--green) ${basePct}%, #233242 ${basePct}%)`;
  const pnl = number(s.risk.realized_pnl_today);
  $('pnl').textContent = `${pnl >= 0 ? '+' : ''}${money(pnl)} ${portfolio?.quote_asset || 'USDT'}`;
  $('pnl').style.color = pnl < 0 ? 'var(--red)' : pnl > 0 ? 'var(--green)' : 'var(--text)';
  $('pnlLimit').textContent = `Límite diario -${money(s.risk.max_daily_loss)}`;
  $('trades').textContent = `${s.risk.entries_today} / ${s.risk.max_trades} entradas`;
  $('position').textContent = `${money(s.risk.position_quote)} / ${money(s.strategy.max_position_quote)}`;
  $('loss').textContent = `${money(Math.max(0, -pnl))} / ${money(s.risk.max_daily_loss)}`;
  $('tradesBar').style.width = `${percent(s.risk.entries_today, s.risk.max_trades)}%`;
  $('positionBar').style.width = `${percent(s.risk.position_quote, s.strategy.max_position_quote)}%`;
  $('lossBar').style.width = `${percent(Math.max(0, -pnl), s.risk.max_daily_loss)}%`;
  const stressed = s.emergency_stop || pnl <= -number(s.risk.max_daily_loss);
  $('riskState').textContent = stressed ? 'BLOQUEADO' : 'SALUDABLE';
  $('riskState').style.color = stressed ? 'var(--red)' : 'var(--green)';
    $('strategyName').textContent = s.strategy.name;
    $('strategyReadiness').textContent = experimental ? 'RESEARCH' : s.strategy.execution_ready ? 'APROBADA' : 'BLOQUEADA';
    $('strategyReadiness').style.color = s.strategy.execution_ready && !experimental ? 'var(--green)' : 'var(--amber)';
    $('strategyBlocker').textContent = s.strategy.execution_blocker
      ? `Compras bloqueadas: ${s.strategy.execution_blocker}.`
      : s.mode === 'paper'
        ? experimental
          ? 'Ejecución experimental autorizada únicamente con capital ficticio.'
          : 'Validación aprobada; simulación autorizada sin capital real.'
        : 'Modelo validado y parámetros coincidentes.';
  $('configInterval').textContent = s.strategy.interval;
  $('fastPeriod').textContent = `${s.strategy.fast_period} velas`;
  $('slowPeriod').textContent = `${s.strategy.slow_period} velas`;
  $('orderAmount').textContent = `${money(s.strategy.order_quote_amount)} ${portfolio?.quote_asset || 'USDT'}`;
  $('cooldown').textContent = `${Math.round(s.strategy.cooldown_seconds / 60)} minutos`;
  $('stopLoss').textContent = `${money(s.strategy.stop_loss_pct)}%`;
  $('takeProfit').textContent = `${money(s.strategy.take_profit_pct)}%`;
  $('trailingStop').textContent = `${money(s.strategy.trailing_stop_pct)}%`;
  $('dailyReset').textContent = new Date(s.operations.next_daily_reset).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  const performance = s.operations.performance || {};
  const colorMetric = (id, value, inverse = false) => {
    const numeric = number(value);
    $(id).textContent = `${numeric >= 0 && !inverse ? '+' : ''}${money(numeric)}%`;
    $(id).style.color = inverse ? (numeric > 0 ? 'var(--red)' : 'var(--text)') : (numeric > 0 ? 'var(--green)' : numeric < 0 ? 'var(--red)' : 'var(--text)');
  };
  colorMetric('botReturn', performance.return_pct);
  colorMetric('holdReturn', performance.buy_hold_return_pct);
  colorMetric('excessReturn', performance.excess_return_pct);
  colorMetric('maxDrawdown', performance.max_drawdown_pct, true);
  $('winRate').textContent = `${money(performance.win_rate_pct)}%`;
  $('completedTrades').textContent = compact(performance.completed_trades || 0);
  $('expectancy').textContent = `${money(performance.expectancy, 4)} USDT`;
  $('expectancy').style.color = number(performance.expectancy) > 0 ? 'var(--green)' : number(performance.expectancy) < 0 ? 'var(--red)' : 'var(--text)';
  $('profitFactor').textContent = money(performance.profit_factor, 2);
  $('profitFactor').style.color = number(performance.profit_factor) >= 1.1 ? 'var(--green)' : 'var(--amber)';
  $('actualFees').textContent = `${money(performance.fees, 4)} USDT`;
  $('actualSlippage').textContent = `${money(performance.slippage_cost, 4)} USDT`;
  $('performancePeriod').textContent = performance.samples ? `${performance.samples} MUESTRAS` : 'RECOPILANDO';
  const training = s.training || {};
  const latest = training.latest;
  const families = latest?.family_validation || [];
  const activeShadows = (s.shadow?.models || []).filter(model => number(model.active));
  const positiveFamilies = families.filter(family => number(family.return_pct) > 0 && number(family.trades) > 0);
  $('pulseFamilies').textContent = latest ? `${families.length} / ${(latest.families_tested || []).length} familias` : 'Sin entrenamiento';
  $('pulsePositive').textContent = latest ? `${positiveFamilies.length} / ${families.length}` : '—';
  $('pulseForward').textContent = `${activeShadows.reduce((total, model) => total + number(model.completed_trades), 0)} ciclos cerrados`;
  const bestShadow = [...activeShadows].sort((left, right) => number(right.return_pct) - number(left.return_pct))[0];
  $('pulseBest').textContent = bestShadow ? `${number(bestShadow.return_pct) >= 0 ? '+' : ''}${money(bestShadow.return_pct, 3)}% · ${compact(bestShadow.completed_trades)} ciclos` : 'Sin cartera activa';
  const nextTraining = latest?.generated_at ? new Date(new Date(latest.generated_at).getTime() + number(training.interval_hours) * 3600000) : null;
  $('pulseNext').textContent = training.running ? 'En curso' : nextTraining && training.enabled ? (nextTraining <= new Date() ? 'Pendiente' : nextTraining.toLocaleString()) : 'Manual';
  $('pulseState').textContent = training.running ? 'EVALUANDO' : latest?.status === 'candidate' ? 'CANDIDATO' : latest ? 'SIN MODELO APROBADO' : 'SIN EVALUACIÓN';
  $('pulseExplanation').textContent = !latest ? 'El entrenamiento usa velas históricas aunque el motor se encienda por ratos. Las carteras forward sólo acumulan operaciones mientras está encendido.'
    : latest.status === 'candidate' ? 'Existe un candidato histórico; todavía debe superar la prueba forward. Las ganancias no están garantizadas.'
    : `${latest.candidates_tested} configuraciones evaluadas. El mejor modelo obtuvo ${money(latest.validation.return_pct)}% neto en validación; no se activan compras reales para aparentar progreso.`;
  $('familyResults').innerHTML = families.length ? [...families].sort((left, right) => (number(right.trades) > 0) - (number(left.trades) > 0) || number(right.return_pct) - number(left.return_pct)).map(family => `<tr><td>${escapeHtml(family.name)}${family.selected ? ' <span class="family-selected">SELECCIONADO</span>' : ''}</td><td>${escapeHtml(family.interval)}</td><td class="${number(family.return_pct) > 0 ? 'positive' : number(family.return_pct) < 0 ? 'negative' : ''}">${number(family.trades) ? `${number(family.return_pct) >= 0 ? '+' : ''}${money(family.return_pct, 3)}%` : 'Sin operaciones'}</td><td>${number(family.trades) ? `${number(family.excess_return_pct) >= 0 ? '+' : ''}${money(family.excess_return_pct, 3)}%` : '—'}</td><td>${compact(family.trades)}</td><td>${number(family.trades) ? money(family.profit_factor) : '—'}</td></tr>`).join('')
    : '<tr><td colspan="6" class="empty-cell">La próxima evaluación mostrará cada familia por separado.</td></tr>';
  $('datasetSamples').textContent = `${compact(training.dataset_samples || 0)} velas observadas en vivo`;
  $('train').disabled = Boolean(training.running);
  $('promote').disabled = Boolean(training.running || s.running || latest?.status !== 'candidate');
  const trainingLabels = {candidate:'CANDIDATO', experimental:'EXPERIMENTAL PAPER', rejected:'RECHAZADO', insufficient_validation:'MUESTRA INSUFICIENTE'};
  $('trainingState').textContent = training.running ? 'ENTRENANDO' : latest ? (trainingLabels[latest.status] || 'EVALUADO') : 'RECOPILANDO';
  $('trainingState').style.color = latest?.status === 'candidate' ? 'var(--green)' : latest ? 'var(--amber)' : 'var(--green)';
  if (latest) {
    $('trainingDataset').textContent = compact(latest.samples);
    $('candidates').textContent = compact(latest.candidates_tested);
    $('recommended').textContent = `${latest.recommended_name || 'Cruce SMA'} · ${latest.recommended_interval || '1m'}`;
    const parameters = latest.recommended_parameters || {fast_period: latest.recommended_fast, slow_period: latest.recommended_slow};
    $('recommendedParameters').textContent = Object.entries(parameters).map(([key, value]) => `${key.replaceAll('_', ' ')} ${value}`).join(' · ');
    $('familiesTested').textContent = `${(latest.families_tested || ['sma_crossover']).length} familias · parámetros y temporalidades`;
    $('validationReturn').textContent = `${latest.validation.return_pct >= 0 ? '+' : ''}${money(latest.validation.return_pct)}%`;
    $('validationReturn').style.color = latest.validation.return_pct >= 0 ? 'var(--green)' : 'var(--red)';
    $('validationVsHold').textContent = `${latest.validation.excess_return_pct >= 0 ? '+' : ''}${money(latest.validation.excess_return_pct)}% vs comprar y mantener`;
    $('validationProfitFactor').textContent = money(latest.validation.profit_factor, 2);
    $('validationExpectancy').textContent = `expectativa ${money(latest.validation.expectancy, 4)} USDT/ciclo`;
    $('validationCosts').textContent = `${money(number(latest.validation.fees) + number(latest.validation.slippage_cost), 2)} USDT`;
    $('validationDrawdown').textContent = `${money(latest.validation.max_drawdown_pct)}%`;
    $('validationWinRate').textContent = `${money(latest.validation.win_rate_pct)}%`;
    $('trainingSamples').textContent = `${latest.samples} velas · ${latest.validation.trades} operaciones`;
    $('positiveFolds').textContent = `${latest.positive_folds} / ${latest.robustness_folds}`;
    $('selectionConfidence').textContent = `${money(latest.selection_adjusted_confidence_pct)}%`;
    const regimes = latest.validation.regime_returns_pct || {};
    $('regimeResults').textContent = ['alcista', 'lateral', 'bajista']
      .map(name => `${name.slice(0, 3).toUpperCase()} ${number(regimes[name]) >= 0 ? '+' : ''}${money(regimes[name])}%`)
      .join(' · ');
    const reasons = latest.rejection_reasons || [];
    $('rejectionReasons').textContent = reasons.length
      ? `No aprobado: ${reasons.join(' · ')}`
      : 'Aprobado: todos los criterios fuera de muestra fueron superados.';
    $('rejectionReasons').className = `research-reasons${reasons.length ? '' : ' approved'}`;
    $('trainedAt').textContent = new Date(latest.generated_at).toLocaleString();
  }
  $('modeBadge').textContent = s.mode.toUpperCase();
  $('start').disabled = s.running || s.emergency_stop;
  $('stop').disabled = !s.running;
  $('reset').disabled = !s.emergency_stop;
  $('updated').textContent = s.last_cycle_at ? `Actualizado ${new Date(s.last_cycle_at).toLocaleTimeString()}` : 'Sin ciclos';
}

function renderShadow(shadow) {
  const models = shadow.models || [];
  const allActive = models.filter(model => number(model.active)).sort((left, right) => number(right.return_pct) - number(left.return_pct));
  const active = allActive.filter(model => shadowIntervalFilter === 'ALL' || model.interval === shadowIntervalFilter);
  shadowRows = allActive;
  if (!allActive.some(model => model.model_id === selectedShadowId)) selectedShadowId = allActive[0]?.model_id || '';
  $('shadowSelector').innerHTML = allActive.length
    ? allActive.map(model => `<option value="${escapeHtml(model.model_id)}" ${model.model_id === selectedShadowId ? 'selected' : ''}>${escapeHtml(model.name)} · ${escapeHtml(model.interval)}</option>`).join('')
    : '<option value="">Sin modelos</option>';
  const eligible = allActive.filter(model => model.forward_eligible);
  $('shadowState').textContent = eligible.length ? `${eligible.length} ELEGIBLE${eligible.length === 1 ? '' : 'S'}` : allActive.length ? `${allActive.length} EN PRUEBA` : 'ESPERANDO MODELOS';
  $('shadowState').style.color = eligible.length ? 'var(--green)' : 'var(--amber)';
  if (allActive.length) {
    const openPositions = allActive.filter(model => number(model.base_quantity) > 0).length;
    const completed = allActive.reduce((sum, model) => sum + number(model.completed_trades), 0);
    $('shadowState').textContent += ` · ${openPositions} POSICIONES · ${completed} CICLOS CERRADOS`;
  }
  const policy = shadow.policy || {};
  $('shadowPolicy').textContent = policy.minimum_completed_trades
    ? `Carteras ficticias independientes: no modifican el saldo principal. Señal en vela cerrada; ejecución simulada al detectar la siguiente vela abierta. Promoción: ≥ ${policy.minimum_completed_trades} ciclos · ≥ ${money(policy.minimum_profit_factor, 2)} PF · ≥ ${Math.round(number(policy.minimum_age_hours) / 24)} días · P&L positivo`
    : 'Criterio operativo pendiente';
  $('shadowModels').innerHTML = active.length ? active.map((model, rank) => {
    const pnl = number(model.realized_pnl);
    const inPosition = number(model.base_quantity) > 0;
    const promote = model.forward_eligible ? `<button class="shadow-promote" data-model="${escapeHtml(model.model_id)}" ${lastStatus?.running ? 'disabled' : ''}>Promover a paper</button>` : '';
    const progress = Math.min(number(model.progress?.age_pct), number(model.progress?.trades_pct));
    return `<article class="shadow-card${model.forward_eligible ? ' eligible' : ''}"><div><span>#${rank + 1} · ${escapeHtml(model.name)}</span><b>${escapeHtml(model.interval)}</b></div><strong>${money(model.equity)} USDT</strong><small>${inPosition ? 'POSICIÓN ABIERTA' : model.pending_action ? `${escapeHtml(model.pending_action)} PENDIENTE` : 'ESPERANDO SEÑAL'}</small><dl><div><dt>Ciclos</dt><dd>${model.completed_trades}</dd></div><div><dt>P&L</dt><dd class="${pnl < 0 ? 'negative' : pnl > 0 ? 'positive' : ''}">${pnl >= 0 ? '+' : ''}${money(pnl, 4)}</dd></div><div><dt>Profit factor</dt><dd>${money(model.profit_factor, 2)}</dd></div><div><dt>Acierto</dt><dd>${money(model.win_rate_pct, 1)}%</dd></div></dl><div class="progress-track"><i style="width:${progress}%"></i></div><div class="progress-copy"><span>${Math.min(7, number(model.age_hours) / 24).toFixed(1)}/7 días</span><span>${model.completed_trades}/${policy.minimum_completed_trades || 30} ciclos</span></div>${promote}</article>`;
  }).join('') : '<div class="shadow-empty">El próximo entrenamiento creará una cartera independiente por familia.</div>';
  document.querySelectorAll('.shadow-promote').forEach(button => button.onclick = () => {
    if (confirm('¿Promover este modelo validado a la cartera paper principal?')) action(`/api/shadow/${button.dataset.model}/promote`);
  });
  const trades = shadow.trades || [];
  $('shadowTrades').innerHTML = trades.length ? trades.slice(0, 50).map(trade => {
    const pnl = number(trade.realized_pnl);
    const costs = number(trade.fee_quote) + number(trade.slippage_quote);
    return `<tr><td>${escapeHtml(new Date(trade.candle_time).toLocaleString())}</td><td>${escapeHtml(trade.name)}</td><td>${escapeHtml(trade.interval)}</td><td class="side-${escapeHtml(trade.side.toLowerCase())}">${escapeHtml(trade.side)}</td><td>$${money(trade.fill_price)}</td><td>${money(trade.quote_quantity, 4)}</td><td>${money(costs, 4)}</td><td style="color:${pnl < 0 ? 'var(--red)' : pnl > 0 ? 'var(--green)' : 'inherit'}">${trade.side === 'SELL' ? money(pnl, 4) : '—'}</td><td>${escapeHtml(trade.reason)}</td></tr>`;
  }).join('') : '<tr><td colspan="9" class="empty-cell">Aún no hay ejecuciones forward; las carteras esperan señales válidas.</td></tr>';
  renderSelectedShadow();
}

function renderSelectedShadow() {
  const model = shadowRows.find(item => item.model_id === selectedShadowId);
  $('selectedModelTitle').textContent = model ? `${model.name} · ${model.interval}` : 'Seleccione un modelo';
  const returnPct = number(model?.return_pct);
  const benchmark = number(model?.buy_hold_return_pct);
  $('selectedReturn').textContent = model ? `${returnPct >= 0 ? '+' : ''}${money(returnPct)}%` : '—';
  $('selectedBenchmark').textContent = model ? `${returnPct - benchmark >= 0 ? '+' : ''}${money(returnPct - benchmark)}%` : '—';
  $('selectedDrawdown').textContent = model ? `${money(model.max_drawdown_pct)}%` : '—';
  $('selectedSharpe').textContent = model ? money(model.sharpe_per_trade, 2) : '—';
  $('selectedSortino').textContent = model ? money(model.sortino_per_trade, 2) : '—';
  $('selectedExposure').textContent = model ? `${money(model.exposure_pct, 1)}%` : '—';
  drawEquityChart(model?.equity_curve || []);
}

function drawEquityChart(curve) {
  drawEquitySeries('equityChart', 'equityEmpty', curve);
}

function renderMainEquity(curve) {
  mainEquityCurve = Array.isArray(curve) ? curve : [];
  $('mainEquitySamples').textContent = `${mainEquityCurve.length} ${mainEquityCurve.length === 1 ? 'punto' : 'puntos'}`;
  if (mainEquityCurve.length) {
    const first = number(mainEquityCurve[0].equity);
    const last = number(mainEquityCurve.at(-1).equity);
    const change = last - first;
    $('mainEquitySummary').textContent = `${money(first)} → ${money(last)} USDT · ${change >= 0 ? '+' : ''}${money(change)} USDT`;
    $('mainEquitySummary').style.color = change < 0 ? 'var(--red)' : change > 0 ? 'var(--green)' : 'var(--text)';
  } else {
    $('mainEquitySummary').textContent = 'Esperando muestras';
    $('mainEquitySummary').style.color = 'var(--text)';
  }
  drawEquitySeries('mainEquityChart', 'mainEquityEmpty', mainEquityCurve);
}

function drawEquitySeries(canvasId, emptyId, curve) {
  const canvas = $(canvasId);
  $(emptyId).style.display = curve.length > 1 ? 'none' : 'grid';
  const context = canvas.getContext('2d');
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  context.scale(ratio, ratio);
  context.clearRect(0, 0, rect.width, rect.height);
  if (curve.length < 2) return;
  const values = curve.map(sample => number(sample.equity));
  const minimum = Math.min(...values), maximum = Math.max(...values), range = maximum - minimum || 1;
  const pad = {top: 12, right: 52, bottom: 22, left: 8};
  const x = index => pad.left + index * (rect.width - pad.left - pad.right) / (values.length - 1);
  const y = value => pad.top + (maximum - value) / range * (rect.height - pad.top - pad.bottom);
  context.strokeStyle = '#1b2937'; context.fillStyle = '#647589'; context.font = '10px Segoe UI';
  for (let index = 0; index < 3; index++) {
    const value = maximum - range * index / 2, lineY = y(value);
    context.beginPath(); context.moveTo(pad.left, lineY); context.lineTo(rect.width - pad.right, lineY); context.stroke();
    context.fillText(money(value), rect.width - pad.right + 6, lineY + 3);
  }
  context.strokeStyle = values.at(-1) >= values[0] ? '#25d695' : '#ff5b67'; context.lineWidth = 2; context.beginPath();
  values.forEach((value, index) => index ? context.lineTo(x(index), y(value)) : context.moveTo(x(index), y(value)));
  context.stroke();
  context.fillStyle = '#647589';
  context.fillText(new Date(curve[0].time).toLocaleString(), pad.left, rect.height - 4);
  context.fillText(new Date(curve.at(-1).time).toLocaleString(), Math.max(pad.left, rect.width - 150), rect.height - 4);
}

function renderOrders(rows) {
  orderRows = rows;
  renderOrderTable();
}

function renderOrderTable() {
  const filtered = orderRows.filter(row => orderSideFilter === 'ALL' || row.side === orderSideFilter);
  $('orderCount').textContent = `${filtered.length} ${filtered.length === 1 ? 'registro' : 'registros'}`;
  $('orders').innerHTML = filtered.length ? filtered.map(o => {
    const side = escapeHtml(o.side);
    const pnl = number(o.realized_pnl);
    return `<tr><td>${escapeHtml(new Date(o.created_at).toLocaleString())}</td><td>${escapeHtml(o.symbol)}</td><td class="side-${side.toLowerCase()}">${side}</td><td>${compact(o.executed_quantity)}</td><td>$${money(o.average_price)}</td><td>${money(o.quote_quantity)}</td><td>${money(o.fee_quote, 4)}</td><td>${money(o.slippage_quote, 4)}</td><td style="color:${pnl < 0 ? 'var(--red)' : pnl > 0 ? 'var(--green)' : 'inherit'}">${pnl ? money(pnl) : '—'}</td><td><span class="order-status">${escapeHtml(o.status)}</span></td><td>${o.is_simulated ? 'Paper' : 'Binance'}</td></tr>`;
  }).join('') : '<tr><td colspan="11" class="empty-cell">Aún no hay operaciones. El motor espera un cruce confirmado.</td></tr>';
}

function renderEvents(rows) {
  eventRows = rows;
  renderEventList();
}

function mergeEvent(event) {
  if (!event?.created_at || !event?.event_type) return;
  const key = `${event.created_at}|${event.event_type}|${event.message || ''}`;
  const existing = new Set(eventRows.map(item => `${item.created_at}|${item.event_type}|${item.message || ''}`));
  if (!existing.has(key)) eventRows.unshift(event);
  eventRows = eventRows.slice(0, 80);
  renderEventList();
}

function setEventStreamState(active) {
  eventStreamActive = active;
  const indicator = $('eventStreamState');
  indicator.innerHTML = active ? '<i></i>EN VIVO' : '<i></i>RESPALDO 5s';
  indicator.classList.toggle('fallback', !active);
}

const reconnectDelay = (milliseconds, signal) => new Promise(resolve => {
  const timeout = setTimeout(resolve, milliseconds);
  signal.addEventListener('abort', () => { clearTimeout(timeout); resolve(); }, {once: true});
});

async function connectEventStream() {
  eventStreamController?.abort();
  eventStreamController = new AbortController();
  const {signal} = eventStreamController;
  let retryMilliseconds = 1000;
  while (!signal.aborted) {
    try {
      const response = await fetch('/api/events/stream', {headers: headers(), signal, cache: 'no-store'});
      if (!response.ok || !response.body) throw new Error(`SSE ${response.status}`);
      setEventStreamState(true);
      retryMilliseconds = 1000;
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (!signal.aborted) {
        const {done, value} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        const frames = buffer.split(/\r?\n\r?\n/);
        buffer = frames.pop() || '';
        frames.forEach(frame => {
          const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n');
          if (!data) return;
          try {
            const event = JSON.parse(data);
            if (!event.keep_alive && event.stream !== 'ready') mergeEvent(event);
          } catch (_) {
            // Ignore malformed frames and keep the authenticated stream alive.
          }
        });
      }
    } catch (error) {
      if (signal.aborted) break;
    } finally {
      setEventStreamState(false);
    }
    await reconnectDelay(retryMilliseconds, signal);
    retryMilliseconds = Math.min(retryMilliseconds * 2, 30000);
  }
}

function renderEventList() {
  const filtered = eventRows.filter(event => eventLevelFilter === 'ALL' || event.level === eventLevelFilter);
  $('events').innerHTML = filtered.length ? filtered.slice(0, 30).map(event => `<div class="event ${escapeHtml(event.level.toLowerCase())}"><i class="event-dot"></i><div><b>${escapeHtml(event.message)}</b><small>${escapeHtml(event.event_type.replaceAll('_', ' '))}</small></div><time>${new Date(event.created_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})}</time></div>`).join('') : '<div class="event-empty">No hay eventos con este filtro.</div>';
}

function sma(values, period) {
  return values.map((_, index) => index < period - 1 ? null : values.slice(index - period + 1, index + 1).reduce((a, b) => a + b, 0) / period);
}

function ema(values, period) {
  const result = Array(values.length).fill(null);
  if (values.length < period) return result;
  let current = values.slice(0, period).reduce((a, b) => a + b, 0) / period;
  result[period - 1] = current;
  const alpha = 2 / (period + 1);
  for (let index = period; index < values.length; index++) {
    current = values[index] * alpha + current * (1 - alpha);
    result[index] = current;
  }
  return result;
}

function rollingMax(values, period) {
  return values.map((_, index) => index < period ? null : Math.max(...values.slice(index - period, index)));
}

function lowerBand(values, period, deviation) {
  return values.map((_, index) => {
    if (index < period - 1) return null;
    const window = values.slice(index - period + 1, index + 1);
    const average = window.reduce((a, b) => a + b, 0) / period;
    const variance = window.reduce((sum, value) => sum + (value - average) ** 2, 0) / period;
    return average - deviation * Math.sqrt(variance);
  });
}

function intervalMinutes(interval) {
  return {"1m": 1, "5m": 5, "15m": 15, "1h": 60}[interval] || 1;
}

function formatChartDuration(candleCount, interval) {
  const minutes = candleCount * intervalMinutes(interval);
  if (minutes < 60) return `${minutes} min`;
  const hours = minutes / 60;
  if (hours < 48) return `${hours.toLocaleString('es-CR', {maximumFractionDigits: 1})} h`;
  return `${(hours / 24).toLocaleString('es-CR', {maximumFractionDigits: 1})} días`;
}

function hourlyEma(candles, period, sourceInterval) {
  if (sourceInterval === '1h') return ema(candles.map(candle => candle.close), period);
  const minutes = intervalMinutes(sourceInterval);
  if (minutes >= 60 || 60 % minutes !== 0) return Array(candles.length).fill(null);
  const groups = new Map();
  candles.forEach((candle, index) => {
    const date = new Date(candle.time);
    const bucket = Math.floor(date.getTime() / 3600000);
    if (!groups.has(bucket)) groups.set(bucket, []);
    groups.get(bucket).push({index, minute: date.getUTCMinutes(), close: candle.close});
  });
  const expected = 60 / minutes;
  const completed = [...groups.values()].filter(group =>
    group.length === expected && group.every((item, index) => item.minute === index * minutes)
  );
  const hourly = ema(completed.map(group => group[3].close), period);
  const result = Array(candles.length).fill(null);
  let pointer = -1;
  candles.forEach((_, index) => {
    while (pointer + 1 < completed.length && completed[pointer + 1].at(-1).index <= index) pointer++;
    if (pointer >= 0) result[index] = hourly[pointer];
  });
  return result;
}

function chartIndicators(candles) {
  const closes = candles.map(candle => candle.close);
  const strategy = lastStatus?.strategy || {};
  const params = strategy.parameters || {};
  if (strategy.kind === 'breakout') {
    return [rollingMax(closes, params.entry_period || 20), ema(closes, params.trend_period || 50), false];
  }
  if (strategy.kind === 'momentum') {
    return [ema(closes, params.fast_period || 9), ema(closes, params.slow_period || 30), false];
  }
  if (strategy.kind === 'mean_reversion') {
    return [sma(closes, params.period || 20), lowerBand(closes, params.period || 20, params.deviation || 2), false];
  }
  if (strategy.kind === 'trend_pullback') {
    return [ema(closes, params.trigger_period || 10), ema(closes, params.trend_period || 50), false];
  }
  if (strategy.kind === 'regime_adaptive') {
    return [
      hourlyEma(candles, params.trend_fast_period || 8, market.interval),
      hourlyEma(candles, params.trend_slow_period || 32, market.interval),
      market.interval !== '1h'
    ];
  }
  return [
    sma(closes, params.fast_period || strategy.fast_period || 7),
    sma(closes, params.slow_period || strategy.slow_period || 25),
    false
  ];
}

function drawPriceLevel(ctx, y, label, color, width, pad) {
  if (!Number.isFinite(y) || y < pad.top || y > pad.priceBottom) return;
  ctx.save();
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = 1;
  ctx.setLineDash([5, 5]);
  ctx.beginPath();
  ctx.moveTo(pad.left, y);
  ctx.lineTo(width - pad.right, y);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.font = '700 10px Segoe UI';
  ctx.fillText(label, pad.left + 7, Math.max(pad.top + 10, y - 4));
  ctx.restore();
}

function drawChart() {
  const canvas = $('chart');
  const allCandles = market.candles || [];
  $('chartEmpty').style.display = allCandles.length ? 'none' : 'grid';
  if (!allCandles.length) {
    chartGeometry = null;
    $('chartTooltip').style.display = 'none';
    $('candleCount').textContent = '0 velas';
    return;
  }
  const parsedAll = allCandles.map(candle => ({
    ...candle,
    open: number(candle.open), high: number(candle.high), low: number(candle.low),
    close: number(candle.close), volume: number(candle.volume)
  }));
  const [primaryAll, secondaryAll, steppedIndicators] = chartIndicators(parsedAll);
  const maximumOffset = Math.max(0, parsedAll.length - 1);
  chartOffset = Math.min(chartOffset, maximumOffset);
  const end = Math.max(1, parsedAll.length - chartOffset);
  const start = Math.max(0, end - chartWindow);
  const parsed = parsedAll.slice(start, end);
  const primary = primaryAll.slice(start, end);
  const secondary = secondaryAll.slice(start, end);
  if (!parsed.length) return;
  $('candleCount').textContent = `Mostrando ${parsed.length} velas · ${formatChartDuration(parsed.length, market.interval)}${chartOffset ? ` · ${chartOffset} anteriores` : ''}`;
  const firstClose = parsed[0].close;
  const lastClose = parsed.at(-1).close;
  const visibleChange = firstClose ? (lastClose / firstClose - 1) * 100 : 0;
  $('chartChange').textContent = `${visibleChange >= 0 ? '+' : ''}${money(visibleChange)}%`;
  $('chartChange').className = `chart-change ${visibleChange >= 0 ? 'positive' : 'negative'}`;
  const lastOpen = new Date(parsedAll.at(-1).time).getTime();
  const candleIsOpen = Date.now() < lastOpen + intervalMinutes(market.interval) * 60000;
  $('chartCandleState').innerHTML = `<i></i>${candleIsOpen ? 'VELA ABIERTA' : 'ÚLTIMA VELA CERRADA'}`;
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.floor(rect.width * ratio);
  canvas.height = Math.floor(rect.height * ratio);
  const ctx = canvas.getContext('2d');
  ctx.scale(ratio, ratio);
  const width = rect.width, height = rect.height;
  const showVolume = chartLayers.volume;
  const pad = {top: 18, right: 78, bottom: 30, left: 10};
  pad.priceBottom = showVolume ? Math.floor(height * .72) : height - pad.bottom;
  pad.volumeTop = showVolume ? Math.floor(height * .79) : height - pad.bottom;
  pad.volumeBottom = height - pad.bottom;
  const chartW = width - pad.left - pad.right;
  const indicatorValues = chartLayers.indicators
    ? [...primary, ...secondary].filter(Number.isFinite)
    : [];
  const rawMinimum = Math.min(...parsed.map(candle => candle.low), ...indicatorValues);
  const rawMaximum = Math.max(...parsed.map(candle => candle.high), ...indicatorValues);
  const rawRange = rawMaximum - rawMinimum || Math.max(rawMaximum * .001, 1);
  const minimum = rawMinimum - rawRange * .04;
  const maximum = rawMaximum + rawRange * .04;
  const range = maximum - minimum;
  const y = value => pad.top + (maximum - value) / range * (pad.priceBottom - pad.top);
  const x = index => pad.left + index * chartW / parsed.length + chartW / parsed.length / 2;
  const candleSpacing = chartW / parsed.length;
  ctx.clearRect(0, 0, width, height);
  ctx.font = '11px Segoe UI'; ctx.fillStyle = '#728196'; ctx.strokeStyle = '#182536'; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const gy = pad.top + i * (pad.priceBottom - pad.top) / 4;
    ctx.beginPath(); ctx.moveTo(pad.left, gy); ctx.lineTo(width - pad.right, gy); ctx.stroke();
    ctx.fillText(money(maximum - i * range / 4), width - pad.right + 9, gy + 4);
  }
  for (let i = 0; i <= 4; i++) {
    const gx = pad.left + i * chartW / 4;
    ctx.beginPath(); ctx.moveTo(gx, pad.top); ctx.lineTo(gx, pad.volumeBottom); ctx.stroke();
  }
  if (showVolume) {
    const maxVolume = Math.max(...parsed.map(candle => candle.volume), 1);
    const volumeWidth = Math.max(1, Math.min(10, candleSpacing * .7));
    parsed.forEach((candle, index) => {
      const volumeHeight = candle.volume / maxVolume * (pad.volumeBottom - pad.volumeTop);
      ctx.fillStyle = candle.close >= candle.open ? 'rgba(16,185,129,.38)' : 'rgba(239,68,68,.38)';
      ctx.fillRect(x(index) - volumeWidth / 2, pad.volumeBottom - volumeHeight, volumeWidth, volumeHeight);
    });
    ctx.fillStyle = '#647589';
    ctx.font = '700 9px Segoe UI';
    ctx.fillText('VOLUMEN', pad.left + 3, pad.volumeTop - 6);
  }
  const candleWidth = Math.max(1, Math.min(9, candleSpacing * .68));
  parsed.forEach((c, index) => {
    const color = c.close >= c.open ? '#10b981' : '#ef4444';
    ctx.strokeStyle = color; ctx.fillStyle = color; ctx.beginPath(); ctx.moveTo(x(index), y(c.high)); ctx.lineTo(x(index), y(c.low)); ctx.stroke();
    const top = Math.min(y(c.open), y(c.close));
    ctx.fillRect(x(index) - candleWidth / 2, top, candleWidth, Math.max(1, Math.abs(y(c.open) - y(c.close))));
  });
  const drawLine = (values, color, stepped = false) => {
    ctx.strokeStyle = color; ctx.lineWidth = 1.7; ctx.beginPath(); let started = false; let previousY = null;
    values.forEach((value, index) => {
      if (!Number.isFinite(value)) return;
      const currentX = x(index), currentY = y(value);
      if (!started) { ctx.moveTo(currentX, currentY); started = true; }
      else if (stepped) { ctx.lineTo(currentX, previousY); ctx.lineTo(currentX, currentY); }
      else ctx.lineTo(currentX, currentY);
      previousY = currentY;
    });
    ctx.stroke();
  };
  if (chartLayers.indicators) {
    drawLine(primary, '#20d6a0', steppedIndicators);
    drawLine(secondary, '#a778ff', steppedIndicators);
  }
  const position = lastStatus?.position;
  if (position) {
    drawPriceLevel(ctx, y(number(position.entry_price)), `ENTRADA ${money(position.entry_price)}`, '#60a5fa', width, pad);
    drawPriceLevel(ctx, y(number(position.stop_price)), `STOP ${money(position.stop_price)}`, '#ef4444', width, pad);
    drawPriceLevel(ctx, y(number(position.take_profit_price)), `OBJETIVO ${money(position.take_profit_price)}`, '#f59e0b', width, pad);
    if (number(position.trailing_stop_price) > 0) drawPriceLevel(ctx, y(number(position.trailing_stop_price)), `TRAILING ${money(position.trailing_stop_price)}`, '#fb7185', width, pad);
  }
  if (chartLayers.trades) orderRows.forEach(order => {
    const orderTime = new Date(order.created_at).getTime();
    const firstTime = new Date(parsed[0].time).getTime();
    const lastTime = new Date(parsed.at(-1).time).getTime();
    const tolerance = intervalMinutes(market.interval) * 90000;
    if (orderTime < firstTime - tolerance || orderTime > lastTime + tolerance) return;
    let closest = -1, distance = Infinity;
    parsed.forEach((candle, index) => {
      const delta = Math.abs(new Date(candle.time).getTime() - orderTime);
      if (delta < distance) { closest = index; distance = delta; }
    });
    if (closest < 0 || distance > tolerance) return;
    const buy = order.side === 'BUY';
    const markerY = y(buy ? parsed[closest].low : parsed[closest].high) + (buy ? 15 : -15);
    ctx.fillStyle = buy ? '#10b981' : '#ef4444';
    ctx.beginPath(); ctx.arc(x(closest), markerY, 8, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = '#07100d'; ctx.font = '900 9px Segoe UI'; ctx.textAlign = 'center';
    ctx.fillText(buy ? 'B' : 'S', x(closest), markerY + 3);
    ctx.textAlign = 'left';
  });
  const currentY = y(lastClose);
  ctx.save();
  ctx.strokeStyle = '#2d87ff'; ctx.fillStyle = '#2d87ff'; ctx.setLineDash([3, 4]);
  ctx.beginPath(); ctx.moveTo(pad.left, currentY); ctx.lineTo(width - pad.right, currentY); ctx.stroke();
  ctx.setLineDash([]); ctx.fillRect(width - pad.right + 3, currentY - 9, pad.right - 6, 18);
  ctx.fillStyle = '#fff'; ctx.font = '800 10px Segoe UI'; ctx.fillText(money(lastClose), width - pad.right + 8, currentY + 4);
  ctx.restore();
  const timeIndices = [0, Math.floor((parsed.length - 1) / 2), parsed.length - 1];
  ctx.fillStyle = '#728196'; ctx.font = '11px Segoe UI';
  timeIndices.forEach(index => {
    const timestamp = new Date(parsed[index].time);
    const label = market.interval === '1h'
      ? timestamp.toLocaleDateString('es-CR', {day:'2-digit', month:'short'}) + ' ' + timestamp.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})
      : timestamp.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    ctx.fillText(label, Math.min(x(index), width - 105), height - 8);
  });
  chartGeometry = {parsed, primary, secondary, pad, chartW, width, height, x, y, candleSpacing};
  if (chartPointer && chartPointer.index < parsed.length) {
    const index = chartPointer.index;
    const candle = parsed[index];
    const crossX = x(index);
    const crossY = Math.max(pad.top, Math.min(pad.priceBottom, chartPointer.y));
    ctx.save();
    ctx.strokeStyle = 'rgba(160,178,199,.55)'; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(crossX, pad.top); ctx.lineTo(crossX, pad.volumeBottom); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(pad.left, crossY); ctx.lineTo(width - pad.right, crossY); ctx.stroke();
    ctx.restore();
    const tooltip = $('chartTooltip');
    const rising = candle.close >= candle.open;
    const primaryValue = Number.isFinite(primary[index]) ? money(primary[index]) : '—';
    const secondaryValue = Number.isFinite(secondary[index]) ? money(secondary[index]) : '—';
    tooltip.innerHTML = `<time>${escapeHtml(new Date(candle.time).toLocaleString('es-CR'))}</time><div class="tooltip-grid"><span>O <b>${money(candle.open)}</b></span><span>H <b>${money(candle.high)}</b></span><span>L <b>${money(candle.low)}</b></span><span>C <b class="${rising ? 'up' : 'down'}">${money(candle.close)}</b></span><span>Vol <b>${compact(candle.volume)}</b></span><span>${escapeHtml($('legendPrimary').textContent)} <b>${primaryValue}</b></span><span>${escapeHtml($('legendSecondary').textContent)} <b>${secondaryValue}</b></span></div>`;
    tooltip.style.display = 'block';
    const tooltipLeft = chartPointer.x > width - 225 ? chartPointer.x - 205 : chartPointer.x + 14;
    tooltip.style.left = `${Math.max(8, tooltipLeft)}px`;
    tooltip.style.top = `${Math.max(8, Math.min(height - 145, chartPointer.y - 30))}px`;
  } else {
    $('chartTooltip').style.display = 'none';
  }
  $('chartRange').textContent = `Máx. $${money(rawMaximum)} · Mín. $${money(rawMinimum)}`;
}

function activateView(view) {
  const sections = [
    [$('modeAlert'), ['overview', 'trading']],
    [document.querySelector('.readiness-strip'), ['overview', 'system']],
    [document.querySelector('.kpi-grid'), ['overview', 'trading']],
    [$('#researchPulse'), ['overview', 'research']],
    [document.querySelector('.content-grid'), ['overview', 'trading']],
    [document.querySelector('.lower-grid'), ['overview', 'system']],
    [document.querySelector('.ops-grid'), ['overview', 'operations', 'system']],
    [$('#training'), ['research']],
    [$('#forward-testing'), ['research']],
    [document.querySelector('.orders-panel'), ['operations']]
  ];
  sections.forEach(([element, views]) => element?.classList.toggle('view-hidden', !views.includes(view)));
  document.querySelectorAll('.sidebar nav a').forEach(link => link.classList.toggle('active', link.dataset.view === view));
  const labels = {
    overview: ['Vista general', 'Situación completa del sistema y sus controles.'],
    trading: ['Trading', 'Mercado, señal, cartera y ejecución principal.'],
    operations: ['Operaciones', 'Rendimiento, telemetría y ledger auditable.'],
    research: ['Investigación', 'Validación histórica y prueba forward de candidatos.'],
    system: ['Riesgo y sistema', 'Límites, configuración, controles y diagnóstico.']
  };
  const label = labels[view] || labels.overview;
  $('workspaceTitle').innerHTML = `<b>${label[0]}</b>${label[1]}`;
  history.replaceState(null, '', document.querySelector(`.sidebar nav a[data-view="${view}"]`)?.getAttribute('href') || '#overview');
  requestAnimationFrame(() => { drawChart(); renderSelectedShadow(); });
}

function setupConsole() {
  const title = document.createElement('div');
  title.id = 'workspaceTitle'; title.className = 'workspace-title';
  document.querySelector('.topbar').after(title);
  const alert = document.createElement('div');
  alert.id = 'systemAlert'; alert.className = 'system-alert';
  title.after(alert);

  const decision = document.createElement('div');
  decision.id = 'decisionContext'; decision.className = 'decision-context';
  $('reason').after(decision);

  const chartTools = document.querySelector('.chart-toolbar');
  chartTools.querySelectorAll('[data-window]').forEach(button => button.onclick = () => {
    chartWindow = number(button.dataset.window);
    chartOffset = 0;
    chartPointer = null;
    chartTools.querySelectorAll('[data-window]').forEach(item => item.classList.toggle('active', item === button));
    drawChart();
  });
  chartTools.querySelectorAll('[data-layer]').forEach(button => button.onclick = () => {
    const layer = button.dataset.layer;
    chartLayers[layer] = !chartLayers[layer];
    button.classList.toggle('active', chartLayers[layer]);
    button.setAttribute('aria-pressed', String(chartLayers[layer]));
    drawChart();
  });
  $('chartInterval').onchange = event => {
    chartInterval = event.target.value;
    chartOffset = 0;
    chartPointer = null;
    refresh();
  };
  const chartCanvas = $('chart');
  const chartWrap = $('chartWrap');
  const pointerIndex = event => {
    if (!chartGeometry) return null;
    const bounds = chartCanvas.getBoundingClientRect();
    const localX = event.clientX - bounds.left;
    const localY = event.clientY - bounds.top;
    const relative = (localX - chartGeometry.pad.left) / chartGeometry.chartW;
    return {
      index: Math.max(0, Math.min(chartGeometry.parsed.length - 1, Math.floor(relative * chartGeometry.parsed.length))),
      x: localX,
      y: localY
    };
  };
  chartCanvas.addEventListener('pointermove', event => {
    if (chartDragStart && chartGeometry) {
      const delta = Math.round((event.clientX - chartDragStart.x) / chartGeometry.candleSpacing);
      chartOffset = Math.max(0, Math.min((market.candles?.length || 1) - 1, chartDragStart.offset - delta));
    }
    chartPointer = pointerIndex(event);
    drawChart();
  });
  chartCanvas.addEventListener('pointerdown', event => {
    chartDragStart = {x: event.clientX, offset: chartOffset};
    chartCanvas.setPointerCapture(event.pointerId);
    chartWrap.classList.add('dragging');
  });
  const stopChartDrag = event => {
    if (chartCanvas.hasPointerCapture?.(event.pointerId)) chartCanvas.releasePointerCapture(event.pointerId);
    chartDragStart = null;
    chartWrap.classList.remove('dragging');
  };
  chartCanvas.addEventListener('pointerup', stopChartDrag);
  chartCanvas.addEventListener('pointercancel', stopChartDrag);
  chartCanvas.addEventListener('pointerleave', event => {
    if (!chartDragStart) {
      chartPointer = null;
      drawChart();
    }
  });
  chartCanvas.addEventListener('wheel', event => {
    event.preventDefault();
    const available = market.candles?.length || 500;
    chartWindow = Math.max(25, Math.min(available, Math.round(chartWindow * (event.deltaY > 0 ? 1.2 : .8))));
    chartTools.querySelectorAll('[data-window]').forEach(item => item.classList.toggle('active', number(item.dataset.window) === chartWindow));
    drawChart();
  }, {passive: false});
  chartCanvas.addEventListener('dblclick', () => {
    chartOffset = 0;
    chartPointer = null;
    drawChart();
  });
  chartCanvas.addEventListener('keydown', event => {
    if (!chartGeometry || !['ArrowLeft', 'ArrowRight', '+', '-', '='].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      const direction = event.key === 'ArrowLeft' ? -1 : 1;
      const current = chartPointer?.index ?? chartGeometry.parsed.length - 1;
      const index = Math.max(0, Math.min(chartGeometry.parsed.length - 1, current + direction));
      chartPointer = {index, x: chartGeometry.x(index), y: chartGeometry.y(chartGeometry.parsed[index].close)};
    } else {
      const zoomIn = event.key === '+' || event.key === '=';
      chartWindow = Math.max(25, Math.min(market.candles?.length || 500, Math.round(chartWindow * (zoomIn ? .8 : 1.2))));
    }
    drawChart();
  });

  const orderTools = document.createElement('div');
  orderTools.className = 'table-tools';
  orderTools.innerHTML = '<span>FILTRAR</span><button class="active" data-side="ALL">Todas</button><button data-side="BUY">Compras</button><button data-side="SELL">Ventas</button>';
  document.querySelector('.orders-panel .table-wrap').before(orderTools);
  orderTools.querySelectorAll('button').forEach(button => button.onclick = () => {
    orderSideFilter = button.dataset.side;
    orderTools.querySelectorAll('button').forEach(item => item.classList.toggle('active', item === button));
    renderOrderTable();
  });

  const eventSelect = document.createElement('select');
  eventSelect.className = 'event-filter';
  eventSelect.innerHTML = '<option value="ALL">Todos los eventos</option><option value="INFO">Información</option><option value="WARNING">Advertencias</option><option value="ERROR">Errores</option><option value="CRITICAL">Críticos</option>';
  document.querySelector('.events-panel .panel-head').append(eventSelect);
  eventSelect.onchange = () => { eventLevelFilter = eventSelect.value; renderEventList(); };

  $('shadowSelector').onchange = event => { selectedShadowId = event.target.value; renderSelectedShadow(); };
  const shadowFilter = document.createElement('select');
  shadowFilter.id = 'shadowIntervalFilter';
  shadowFilter.setAttribute('aria-label', 'Filtrar carteras por temporalidad');
  shadowFilter.innerHTML = '<option value="ALL">Todas las temporalidades</option><option value="1m">1 minuto</option><option value="5m">5 minutos</option><option value="15m">15 minutos</option><option value="1h">1 hora</option>';
  $('shadowSelector').before(shadowFilter);
  shadowFilter.onchange = () => { shadowIntervalFilter = shadowFilter.value; renderShadow(lastStatus?.shadow || {models:[], trades:[]}); };
  document.querySelectorAll('.sidebar nav a[data-view]').forEach(link => link.onclick = event => {
    event.preventDefault(); activateView(link.dataset.view);
  });
  const initial = document.querySelector(`.sidebar nav a[href="${location.hash}"]`)?.dataset.view || 'overview';
  activateView(initial);
}

async function refresh() {
  try {
    const marketPath = `/api/market?limit=500${chartInterval ? `&interval=${chartInterval}` : ''}`;
    const eventsRequest = eventStreamActive ? Promise.resolve(null) : api('/api/events');
    const [status, orders, marketData, equity, events] = await Promise.all([
      api('/api/status'), api('/api/orders'), api(marketPath), api('/api/equity?limit=250'), eventsRequest
    ]);
    market = marketData;
    renderStatus(status);
    renderOrders(orders);
    renderMainEquity(equity);
    if (events) renderEvents(events);
    $('timeframe').textContent = market.interval;
    drawChart();
  } catch (error) {
    $('connection').textContent = 'Sin conexión'; $('connection').className = 'status-pill'; $('message').textContent = error.message;
  }
}

async function action(path, successMessage = 'Acción aplicada correctamente.') {
  try {
    $('message').textContent = 'Procesando acción segura…'; await api(path, {method: 'POST'}); $('message').textContent = successMessage; await refresh();
  } catch (error) { $('message').textContent = error.message; }
}

$('start').onclick = () => action('/api/start', 'Motor iniciado: análisis continuo activo.');
$('stop').onclick = () => action('/api/stop', 'Motor detenido. El dashboard permanece disponible.');
$('emergency').onclick = () => action('/api/emergency-stop', 'Emergencia activada: operaciones bloqueadas.');
$('reset').onclick = () => action('/api/emergency-reset', 'Sistema rearmado. El motor permanece detenido hasta iniciarlo.');
$('train').onclick = () => action('/api/train');
$('promote').onclick = () => {
  if (confirm('¿Promover este candidato validado como estrategia activa?')) {
    action('/api/training/promote');
  }
};
setInterval(() => $('clock').textContent = new Date().toLocaleString('es-CR', {dateStyle:'medium', timeStyle:'medium'}), 1000);
window.addEventListener('resize', drawChart);
window.addEventListener('resize', renderSelectedShadow);
window.addEventListener('resize', () => drawEquitySeries('mainEquityChart', 'mainEquityEmpty', mainEquityCurve));
window.addEventListener('beforeunload', () => eventStreamController?.abort());
setupConsole();
refresh().finally(connectEventStream);
setInterval(refresh, 5000);
