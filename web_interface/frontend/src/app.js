// Глобальные переменные
let opportunities = []
let currentSort = { field: "spread", order: "desc" }
let minSpread = 0.5
let scannerMode = 'cex'
let chainScope = 'all'
let assetProfile = 'balanced'

// Client-side filters for CEX↔DEX and межсетевой modes. Persisted in
// localStorage so the user keeps their checkboxes between reloads.
const INTERCHAIN_FILTERS_KEY = 'interchain_filters_v1'
let interchainFilters = (() => {
  try {
    const raw = localStorage.getItem(INTERCHAIN_FILTERS_KEY)
    if (raw) return { ...{
      profitable_only: false,
      live_only: false,
      hide_negative_roi: false,
      hide_unknown_transfer: false,
      min_liquidity_usd: 0,
      max_spread_pct: 0,
    }, ...JSON.parse(raw) }
  } catch (_) {}
  return {
    profitable_only: false,
    live_only: false,
    hide_negative_roi: false,
    hide_unknown_transfer: false,
    min_liquidity_usd: 0,
    max_spread_pct: 0,
  }
})()

function saveInterchainFilters() {
  try { localStorage.setItem(INTERCHAIN_FILTERS_KEY, JSON.stringify(interchainFilters)) } catch (_) {}
}

function applyInterchainFilters(list) {
  if (!Array.isArray(list)) return []
  const f = interchainFilters
  const minLiq = Number(f.min_liquidity_usd) || 0
  const maxSpread = Number(f.max_spread_pct) || 0
  return list.filter((o) => {
    if (!o) return false
    if (f.profitable_only && !(Number(o.net_profit_usd) > 0)) return false
    if (f.hide_negative_roi && !(Number(o.roi_pct) >= 0)) return false
    if (f.live_only && String(o.execution_quality || '').toLowerCase() !== 'live') return false
    if (f.hide_unknown_transfer && String(o.transfer_status || '') === 'unknown') return false
    if (minLiq > 0 && (Number(o.liquidity_usd) || 0) < minLiq) return false
    if (maxSpread > 0 && (Number(o.spread) || 0) > maxSpread) return false
    return true
  })
}

function ensureInterchainFiltersPanel() {
  if (document.getElementById('interchainFiltersPanel')) return
  const toolbarSide = document.querySelector('.toolbar-side')
  if (!toolbarSide) return
  const panel = document.createElement('div')
  panel.id = 'interchainFiltersPanel'
  panel.className = 'interchain-filters toolbar-filter d-none'
  panel.innerHTML = `
    <button type="button" id="interchainFiltersToggle" class="btn btn-sm btn-outline-secondary" title="Настройки фильтров">
      <i class="fas fa-sliders me-1"></i>Фильтры
      <span id="interchainFiltersBadge" class="badge bg-accent-soft ms-1 d-none">0</span>
    </button>
    <div id="interchainFiltersDropdown" class="interchain-filters-dropdown d-none">
      <div class="if-row">
        <label class="form-check form-switch m-0">
          <input class="form-check-input" type="checkbox" id="ifProfitableOnly">
          <span class="form-check-label small">Только прибыльные (profit &gt; 0)</span>
        </label>
      </div>
      <div class="if-row">
        <label class="form-check form-switch m-0">
          <input class="form-check-input" type="checkbox" id="ifHideNegativeRoi">
          <span class="form-check-label small">Скрыть отрицательный ROI</span>
        </label>
      </div>
      <div class="if-row">
        <label class="form-check form-switch m-0">
          <input class="form-check-input" type="checkbox" id="ifLiveOnly">
          <span class="form-check-label small">Только живые (execution=live)</span>
        </label>
      </div>
      <div class="if-row">
        <label class="form-check form-switch m-0">
          <input class="form-check-input" type="checkbox" id="ifHideUnknownTransfer">
          <span class="form-check-label small">Скрыть «статус неизвестен»</span>
        </label>
      </div>
      <div class="if-row">
        <label class="small text-dim d-block mb-1">Мин. ликвидность пула, $</label>
        <input type="number" id="ifMinLiquidity" class="form-control form-control-sm compact-input" min="0" step="100" placeholder="0 = без ограничений">
      </div>
      <div class="if-row">
        <label class="small text-dim d-block mb-1">Макс. спред, %</label>
        <input type="number" id="ifMaxSpread" class="form-control form-control-sm compact-input" min="0" step="0.5" placeholder="0 = без ограничений">
      </div>
      <div class="if-row d-flex justify-content-between align-items-center pt-2 mt-1 border-top">
        <button type="button" id="ifResetBtn" class="btn btn-sm btn-link text-dim p-0">Сбросить</button>
        <button type="button" id="ifCloseBtn" class="btn btn-sm btn-outline-primary">OK</button>
      </div>
    </div>
  `
  toolbarSide.appendChild(panel)

  const toggleBtn = panel.querySelector('#interchainFiltersToggle')
  const dropdown = panel.querySelector('#interchainFiltersDropdown')
  const inputs = {
    profitable_only: panel.querySelector('#ifProfitableOnly'),
    hide_negative_roi: panel.querySelector('#ifHideNegativeRoi'),
    live_only: panel.querySelector('#ifLiveOnly'),
    hide_unknown_transfer: panel.querySelector('#ifHideUnknownTransfer'),
    min_liquidity_usd: panel.querySelector('#ifMinLiquidity'),
    max_spread_pct: panel.querySelector('#ifMaxSpread'),
  }

  const syncFromFilters = () => {
    inputs.profitable_only.checked = !!interchainFilters.profitable_only
    inputs.hide_negative_roi.checked = !!interchainFilters.hide_negative_roi
    inputs.live_only.checked = !!interchainFilters.live_only
    inputs.hide_unknown_transfer.checked = !!interchainFilters.hide_unknown_transfer
    inputs.min_liquidity_usd.value = interchainFilters.min_liquidity_usd || ''
    inputs.max_spread_pct.value = interchainFilters.max_spread_pct || ''
    updateFiltersBadge()
  }

  const updateFiltersBadge = () => {
    const badge = document.getElementById('interchainFiltersBadge')
    if (!badge) return
    let active = 0
    const f = interchainFilters
    if (f.profitable_only) active++
    if (f.hide_negative_roi) active++
    if (f.live_only) active++
    if (f.hide_unknown_transfer) active++
    if (Number(f.min_liquidity_usd) > 0) active++
    if (Number(f.max_spread_pct) > 0) active++
    if (active > 0) {
      badge.textContent = String(active)
      badge.classList.remove('d-none')
    } else {
      badge.classList.add('d-none')
    }
  }

  toggleBtn.addEventListener('click', (e) => {
    e.stopPropagation()
    dropdown.classList.toggle('d-none')
  })
  document.addEventListener('click', (e) => {
    if (!panel.contains(e.target)) dropdown.classList.add('d-none')
  })

  const bindCheckbox = (key) => {
    inputs[key].addEventListener('change', function () {
      interchainFilters[key] = !!this.checked
      saveInterchainFilters()
      updateFiltersBadge()
      updateOpportunities()
    })
  }
  bindCheckbox('profitable_only')
  bindCheckbox('hide_negative_roi')
  bindCheckbox('live_only')
  bindCheckbox('hide_unknown_transfer')

  const bindNumber = (key) => {
    inputs[key].addEventListener('change', function () {
      const v = Number.parseFloat(this.value)
      interchainFilters[key] = Number.isFinite(v) && v > 0 ? v : 0
      saveInterchainFilters()
      updateFiltersBadge()
      updateOpportunities()
    })
  }
  bindNumber('min_liquidity_usd')
  bindNumber('max_spread_pct')

  panel.querySelector('#ifResetBtn').addEventListener('click', () => {
    interchainFilters = {
      profitable_only: false,
      live_only: false,
      hide_negative_roi: false,
      hide_unknown_transfer: false,
      min_liquidity_usd: 0,
      max_spread_pct: 0,
    }
    saveInterchainFilters()
    syncFromFilters()
    updateOpportunities()
  })
  panel.querySelector('#ifCloseBtn').addEventListener('click', () => {
    dropdown.classList.add('d-none')
  })

  syncFromFilters()
}


// v8: UI helpers for settings modal.  No heavy animations, only cheap DOM updates.
const CORE_EXCHANGE_NAMES = new Set([
  'gate.io', 'mexc', 'bybit', 'okx', 'kucoin', 'bitget', 'kraken pro', 'lbank', 'pionex.us', 'binance.us'
])
const MANUAL_EXCHANGE_NAMES = new Set(['binance alpha (manual)'])
const DEX_STYLE_EXCHANGE_NAMES = new Set([])
const SLOW_REST_EXCHANGE_NAMES = new Set(['safetrade', 'nonkyc'])

function normalizeExchangeName(name) {
  return String(name || '').trim().toLowerCase()
}

function exchangeKind(name) {
  const n = normalizeExchangeName(name)
  if (MANUAL_EXCHANGE_NAMES.has(n) || n.includes('alpha')) return 'manual'
  if (DEX_STYLE_EXCHANGE_NAMES.has(n)) return 'dex'
  if (SLOW_REST_EXCHANGE_NAMES.has(n)) return 'slow'
  if (n.includes('binance.us') || n.includes('pionex.us')) return 'us'
  return 'cex'
}

function exchangeKindLabel(kind) {
  if (kind === 'manual') return 'MANUAL'
  if (kind === 'dex') return 'DEX'
  if (kind === 'slow') return 'REST'
  if (kind === 'us') return 'US'
  return ''
}

function exchangeNote(name) {
  const kind = exchangeKind(name)
  if (kind === 'manual') return 'ручной сигнал: цена/стакан, без авто-торговли'
  if (kind === 'dex') return 'отдельная площадка, проверять стакан/ликвидность'
  if (kind === 'slow') return 'REST fallback, может быть медленнее WebSocket'
  if (kind === 'us') return 'US-площадка'
  return 'стакан + bid/ask'
}

function sortExchangeNames(list) {
  const rank = { cex: 0, us: 1, manual: 2, dex: 3, slow: 4 }
  return [...(list || [])].sort((a, b) => {
    const ka = exchangeKind(a)
    const kb = exchangeKind(b)
    const ra = rank[ka] ?? 9
    const rb = rank[kb] ?? 9
    if (ra !== rb) return ra - rb
    return String(a).localeCompare(String(b), 'ru', { sensitivity: 'base' })
  })
}

function updateActiveExchangeCounter() {
  const all = Array.from(document.querySelectorAll('#exchangesContainer input[type="checkbox"]'))
  const active = all.filter(cb => cb.checked).length
  const el = document.getElementById('activeExchangeCount')
  if (el) el.innerHTML = `<i class="fas fa-plug"></i> Активно: ${active}/${all.length}`
}

function setSettingsDirty(message = 'Есть несохранённые изменения') {
  const status = document.getElementById('settingsDirtyStatus')
  const saveBtn = document.getElementById('saveSettingsButton')
  if (status) {
    status.textContent = message
    status.classList.remove('text-muted', 'settings-saved', 'settings-error')
    status.classList.add('settings-dirty')
  }
  if (saveBtn) saveBtn.classList.add('save-attention')
}

function setSettingsSaved(message = 'Настройки сохранены') {
  const status = document.getElementById('settingsDirtyStatus')
  const saveBtn = document.getElementById('saveSettingsButton')
  if (status) {
    status.textContent = message
    status.classList.remove('text-muted', 'settings-dirty', 'settings-error')
    status.classList.add('settings-saved')
  }
  if (saveBtn) saveBtn.classList.remove('save-attention')
}

function setSettingsError(message = 'Ошибка настроек') {
  const status = document.getElementById('settingsDirtyStatus')
  const saveBtn = document.getElementById('saveSettingsButton')
  if (status) {
    status.textContent = message
    status.classList.remove('text-muted', 'settings-dirty', 'settings-saved')
    status.classList.add('settings-error')
  }
  if (saveBtn) saveBtn.classList.add('save-attention')
}

function installSettingsDirtyWatchers() {
  const modal = document.getElementById('settingsModal')
  if (!modal || modal.dataset.dirtyWatchInstalled === '1') return
  modal.dataset.dirtyWatchInstalled = '1'
  modal.addEventListener('input', (e) => {
    if (e.target && (e.target.matches('input') || e.target.matches('select'))) setSettingsDirty()
  })
  modal.addEventListener('change', (e) => {
    if (e.target && (e.target.matches('input') || e.target.matches('select'))) setSettingsDirty()
    try { updateActiveExchangeCounter() } catch (_) { }
  })
  modal.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target && e.target.matches('input')) {
      e.preventDefault()
      saveSettings()
    }
  })
}

function setExchangeSelection(mode) {
  const checkboxes = Array.from(document.querySelectorAll('#exchangesContainer input[type="checkbox"]'))
  if (!checkboxes.length) return
  checkboxes.forEach(cb => {
    const name = cb.getAttribute('data-exchange') || ''
    const n = normalizeExchangeName(name)
    let checked = cb.checked
    if (mode === 'all') checked = true
    else if (mode === 'none') checked = false
    else if (mode === 'core') checked = CORE_EXCHANGE_NAMES.has(n)
    else if (mode === 'manual') checked = MANUAL_EXCHANGE_NAMES.has(n) || DEX_STYLE_EXCHANGE_NAMES.has(n)
    cb.checked = checked
    const item = cb.closest('.exchange-toggle')
    if (item) item.classList.toggle('active', checked)
  })
  updateActiveExchangeCounter()
  setSettingsDirty('Список бирж изменён, нажми «Сохранить»')
}

function installExchangeQuickButtons() {
  const bindings = [
    ['selectAllExchangesBtn', 'all'],
    ['selectCoreExchangesBtn', 'core'],
    ['selectManualSourcesBtn', 'manual'],
    ['clearExchangesBtn', 'none'],
  ]
  bindings.forEach(([id, mode]) => {
    const btn = document.getElementById(id)
    if (btn && btn.dataset.bound !== '1') {
      btn.dataset.bound = '1'
      btn.addEventListener('click', () => setExchangeSelection(mode))
    }
  })
}

let settings = {
  min_spread: 0.5,
  max_spread: 50.0,
  enabled_exchanges: [],
  ui_use_separate_polling: false,
  ui_arb_filter_transfer: false,
  ui_arb_filter_transfer_strict_unknown: false,
  ui_arb_filter_liquidity: false,
  arb_min_notional_usd: 300,
  ui_arb_top_liquidity_n: 0,
  kraken_kyber_enabled: true,
  kraken_kyber_min_spread: 0.5,
  kraken_kyber_notional_usd: 250,
  kraken_kyber_asset_limit: 0,
  use_orderbooks: false,
  orderbooks_refine_top_symbols: 5,
  orderbooks_per_exchange_timeout_sec: 8.0,
  tickers_per_exchange_timeout_sec: 12.0,
  stale_rank_penalty_enabled: false,
  stale_rank_penalty_grace_sec: 10.0,
  stale_rank_penalty_per_min_pct: 0.2,
  stale_rank_hide_after_sec: 0.0,
}
let blacklist = { permanent_list: [], temporary_list: {} }
let favorites = [] // Список избранных монет
let warnMarks = {} // "Черная метка" / предупреждения по монетам (localStorage)
const WARN_MARKS_STORAGE_KEY = 'warn_marks_v1'
const WARN_MARK_MAX = 3
let statusUpdateInterval
let opportunitiesUpdateInterval
let coinInfoLiveInterval
let coinInfoLiveInFlight = false
let coinInfoLiveRequestSeq = 0

function formatPercent(value, digits = 1) {
  if (typeof value !== 'number' || !isFinite(value)) return '-'
  return value.toFixed(digits) + '%'
}

function formatRate(rate) {
  if (typeof rate !== 'number' || !isFinite(rate)) return '-'
  return (rate * 100).toFixed(1) + '%'
}

function formatDuration(sec) {
  if (typeof sec !== 'number' || !isFinite(sec)) return '-'
  if (sec < 1) return Math.round(sec * 1000) + ' мс'
  if (sec < 60) return sec.toFixed(1) + ' с'
  const minutes = Math.floor(sec / 60)
  const seconds = Math.round(sec % 60)
  if (minutes < 60) return `${minutes} м ${seconds} с`
  const hours = Math.floor(minutes / 60)
  const mins = minutes % 60
  return `${hours} ч ${mins} м`
}

function formatTime(ts) {
  if (typeof ts !== 'number' || !isFinite(ts) || ts <= 0) return '-'
  try {
    return new Date(ts * 1000).toLocaleTimeString()
  } catch (_) {
    return '-'
  }
}

function formatSince(ts) {
  if (typeof ts !== 'number' || !isFinite(ts) || ts <= 0) return '-'
  const diff = Date.now() / 1000 - ts
  if (!isFinite(diff) || diff < 0) return '-'
  const minutes = Math.floor(diff / 60)
  const seconds = Math.floor(diff % 60)
  if (minutes < 1) return `${seconds} с назад`
  if (minutes < 60) return `${minutes} м ${seconds} с назад`
  const hours = Math.floor(minutes / 60)
  const mins = minutes % 60
  if (hours < 24) return `${hours} ч ${mins} м назад`
  const days = Math.floor(hours / 24)
  return `${days} д ${hours % 24} ч назад`
}

function formatAgeSeconds(ageSec) {
  if (typeof ageSec !== 'number' || !isFinite(ageSec) || ageSec < 0) return '—'
  return `${Math.round(ageSec)}с`
}

function formatQuotePrice(price) {
  if (typeof price !== 'number' || !isFinite(price) || price <= 0) return '—'
  if (price >= 1000) return price.toFixed(2)
  if (price >= 1) return price.toFixed(6)
  return price.toFixed(8)
}

function setLastUpdateBadge(data) {
  const lastUpdate = document.getElementById("lastUpdate")
  if (!lastUpdate) return

  const hasTime = data && typeof data.last_update === 'string' && data.last_update.trim()
  const tsText = hasTime ? data.last_update.trim() : '—'
  const ageSec = (data && typeof data.last_update_age_sec === 'number' && isFinite(data.last_update_age_sec))
    ? Math.max(0, Math.round(data.last_update_age_sec))
    : null
  const stale = !!(data && data.last_update_stale)
  const ageText = (ageSec === null) ? '—' : `${ageSec}с`

  lastUpdate.textContent = `${tsText} · ${ageText}${stale ? ' · НЕАКТУАЛЬНО' : ''}`
  lastUpdate.style.color = stale ? '#f5c15f' : ''
  lastUpdate.title = (ageSec === null)
    ? 'Возраст данных неизвестен'
    : `Возраст данных: ${ageSec}с`
}

function shortPath(pathStr) {
  if (typeof pathStr !== 'string') return '-'
  const parts = pathStr.split(/\\|\//)
  if (!parts.length) return pathStr
  return parts[parts.length - 1] || pathStr
}

// Инициализация избранных монет из localStorage
function initFavorites() {
  const storedFavorites = localStorage.getItem('favorites')
  if (storedFavorites) {
    try {
      favorites = JSON.parse(storedFavorites)
    } catch (e) {
      console.error('Ошибка при загрузке избранных монет:', e)
      favorites = []
    }
  }
}

function normalizeSymbolKey(symbol) {
  try {
    return String(symbol || '').trim().toUpperCase()
  } catch (_) {
    return ''
  }
}

function initWarnMarks() {
  try {
    const raw = localStorage.getItem(WARN_MARKS_STORAGE_KEY)
    warnMarks = raw ? (JSON.parse(raw) || {}) : {}
  } catch (_) {
    warnMarks = {}
  }

  // Best-effort sanitize (store only int 1..WARN_MARK_MAX; 0 removes entry)
  try {
    const cleaned = {}
    for (const [k, v] of Object.entries(warnMarks || {})) {
      const key = normalizeSymbolKey(k)
      if (!key) continue
      const n = Number.parseInt(v, 10)
      if (!Number.isFinite(n)) continue
      const clamped = Math.max(0, Math.min(WARN_MARK_MAX, n))
      if (clamped > 0) cleaned[key] = clamped
    }
    warnMarks = cleaned
    saveWarnMarks()
  } catch (_) { /* noop */ }
}

function saveWarnMarks() {
  try { localStorage.setItem(WARN_MARKS_STORAGE_KEY, JSON.stringify(warnMarks || {})) } catch (_) { /* noop */ }
}

function getWarnMarkCount(symbol) {
  const key = normalizeSymbolKey(symbol)
  if (!key) return 0
  const n = Number.parseInt((warnMarks || {})[key], 10)
  if (!Number.isFinite(n)) return 0
  return Math.max(0, Math.min(WARN_MARK_MAX, n))
}

function setWarnMarkCount(symbol, count) {
  const key = normalizeSymbolKey(symbol)
  if (!key) return
  const nRaw = Number.parseInt(count, 10)
  const n = Number.isFinite(nRaw) ? Math.max(0, Math.min(WARN_MARK_MAX, nRaw)) : 0
  if (n <= 0) {
    try { delete warnMarks[key] } catch (_) { /* noop */ }
  } else {
    warnMarks[key] = n
  }
  saveWarnMarks()
}

function updateWarnMarkElements(symbol) {
  try {
    const key = normalizeSymbolKey(symbol)
    const cnt = getWarnMarkCount(key)
    const btnCnt = document.getElementById('warnMarkBtnCount')
    if (btnCnt) btnCnt.textContent = String(cnt)
  } catch (_) { /* noop */ }

  // Re-render table so the badge near the symbol updates without a server request.
  try { updateOpportunitiesTable() } catch (_) { /* noop */ }
}

function addWarnMark(symbol, evt) {
  const key = normalizeSymbolKey(symbol)
  if (!key) return

  const e = evt || (typeof window !== 'undefined' ? window.event : null)
  const doReset = !!(e && e.shiftKey)

  if (doReset) {
    setWarnMarkCount(key, 0)
    showNotification(`Черная метка сброшена: ${key}`, "info")
  } else {
    const cur = getWarnMarkCount(key)
    const next = Math.min(WARN_MARK_MAX, cur + 1)
    setWarnMarkCount(key, next)
    showNotification(`Черная метка: ${key} (${next}/${WARN_MARK_MAX})`, "info")
  }

  updateWarnMarkElements(key)
}

// Перевод вероятности [0..1] в «грубый» процент уверенности по конфигу
function bucketPercent(prob) {
  try {
    if (typeof prob !== 'number' || !isFinite(prob)) return null
    const p = Math.max(0, Math.min(1, prob))
    const conf = (settings && settings.confidence) ? settings.confidence : null
    const thresholds = Array.isArray(conf && conf.thresholds) ? conf.thresholds : [0.2, 0.4, 0.6, 0.8]
    const percents = Array.isArray(conf && conf.bucketPercents) ? conf.bucketPercents : [20, 40, 60, 80]
    for (let i = 0; i < thresholds.length; i++) {
      if (p < thresholds[i]) return percents[i] ?? null
    }
    return percents[percents.length - 1] ?? 80
  } catch (e) {
    return null
  }
}

// Получение класса бейджа по корзине уверенности
function confidenceBadgeClass(percent) {
  const conf = (settings && settings.confidence) ? settings.confidence : null
  const colors = Array.isArray(conf && conf.colors) ? conf.colors : ["secondary", "secondary", "warning", "success"]
  // Мэппинг по умолчанию: 20->0, 40->1, 60->2, 80->3
  const order = [20, 40, 60, 80]
  const idx = Math.max(0, order.indexOf(percent))
  const color = colors[Math.min(idx, colors.length - 1)] || 'secondary'
  return `bg-${color}` + (color === 'warning' ? ' text-dark' : '')
}

// Сохранение избранных монет в localStorage
function saveFavorites() {
  localStorage.setItem('favorites', JSON.stringify(favorites))
}

// Добавление/удаление монеты из избранного
function toggleFavorite(symbol) {
  const index = favorites.indexOf(symbol)
  if (index === -1) {
    // Добавляем в избранное
    favorites.push(symbol)
    showNotification(`Монета ${symbol} добавлена в избранное`, "success")
  } else {
    // Удаляем из избранного
    favorites.splice(index, 1)
    showNotification(`Монета ${symbol} удалена из избранного`, "info")
  }
  saveFavorites()
  updateOpportunities() // Обновляем отображение списка возможностей
}

// Проверка, находится ли монета в избранном
function isFavorite(symbol) {
  return favorites.includes(symbol)
}

// URL-шаблоны для бирж
const EXCHANGE_URLS = {
  "Gate.io": "https://www.gate.io/trade/{symbol}_USDT",
  "MEXC": "https://www.mexc.com/exchange/{symbol}_USDT",
  "CoinEx": "https://www.coinex.com/exchange/{symbol}-USDT",
  "Biconomy": "https://www.biconomy.com/exchange/{symbol}_USDT",
  "Bybit": "https://www.bybit.com/trade/spot/{symbol}/USDT",
  "OKX": "https://www.okx.com/trade-spot/{symbol}-USDT",
  "Binance.US": "https://www.binance.us/trade/{symbol}_USDT",
  "BinanceUS": "https://www.binance.us/trade/{symbol}_USDT",
  "Binance Alpha (manual)": "https://www.binance.com/en/alpha?keyword={symbol}",
  "Kraken Pro": "https://pro.kraken.com/app/trade/{symbol}-{quote}",
  "Bitget": "https://www.bitget.com/spot/{symbol}USDT",
  "HTX": "https://www.htx.com/exchange/{symbol}_usdt",
  "KuCoin": "https://www.kucoin.com/trade/{symbol}-USDT",
  "BingX": "https://bingx.com/en-us/spot/{symbol}USDT",
  "Bitrue": "https://www.bitrue.com/trade/{symbol}_USDT",
  "TradeOgre": "https://tradeogre.com/exchange/USDT-{symbol}",
  "NonKYC": "https://nonkyc.io/markets/{symbol}-USDT",
  "SafeTrade": "https://safe.trade/markets/{symbol}-USDT",
  "Pionex.US": "https://www.pionex.us/en-US/trade/{symbol}_USDT/Manual",
  "LBank": "https://www.lbank.com/trade/{symbol}_usdt"
}



// Binance Alpha: для коротких тикеров типа 1/USDT ссылка через keyword часто открывает не ту монету.
// Поэтому для Alpha стараемся строить прямую ссылку: /alpha/<chain>/<contract>.
const BINANCE_ALPHA_CHAIN_SLUGS = {
  '1': 'eth', '56': 'bsc', '137': 'polygon', '42161': 'arbitrum', '10': 'optimism',
  '8453': 'base', '43114': 'avax', '59144': 'linea', '324': 'zksync', '534352': 'scroll',
  '195': 'tron', '101': 'solana', '900': 'solana', 'solana': 'solana',
  'bsc': 'bsc', 'bnb': 'bsc', 'eth': 'eth', 'ethereum': 'eth', 'base': 'base',
  'arbitrum': 'arbitrum', 'polygon': 'polygon', 'op': 'optimism', 'optimism': 'optimism'
}

function normalizeAlphaBase(symbol) {
  const { base } = splitNormalizedPairSymbol(symbol)
  return String(base || symbol || '').toUpperCase().replace(/[^A-Z0-9]/g, '')
}

function alphaChainSlug(chainId) {
  const raw = String(chainId == null ? '' : chainId).trim().toLowerCase()
  if (!raw) return 'bsc'
  return BINANCE_ALPHA_CHAIN_SLUGS[raw] || raw.replace(/[^a-z0-9_-]/g, '') || 'bsc'
}

async function fetchBinanceAlphaToken(symbol) {
  try {
    const base = normalizeAlphaBase(symbol)
    if (!base) return null
    window.__binanceAlphaTokenCache = window.__binanceAlphaTokenCache || {}
    const cached = window.__binanceAlphaTokenCache[base]
    if (cached && cached.ts && (Date.now() - cached.ts) < 5 * 60 * 1000) return cached.data || null
    const resp = await fetch(`/api/binance_alpha_lookup/${encodeURIComponent(base)}`, { signal: AbortSignal.timeout(8000) })
    const data = await resp.json()
    const token = data && data.success ? data.token : null
    window.__binanceAlphaTokenCache[base] = { ts: Date.now(), data: token || null }
    return token || null
  } catch (e) {
    console.warn('[alpha-link] lookup failed', symbol, e && e.message)
    return null
  }
}

function buildBinanceAlphaUrlFromToken(token, fallbackSymbol) {
  try {
    const contract = String(token && (token.contractAddress || token.contract_address || token.address) || '').trim()
    if (contract) {
      const chain = alphaChainSlug(token.chainId || token.chain_id || token.chain || token.network)
      return `https://www.binance.com/en/alpha/${encodeURIComponent(chain)}/${encodeURIComponent(contract)}`
    }
    const alphaId = String(token && (token.alphaId || token.alphaID || token.alpha_id) || '').trim()
    if (alphaId) return `https://www.binance.com/en/alpha?keyword=${encodeURIComponent(alphaId)}`
  } catch (_) { }
  return `https://www.binance.com/en/alpha?keyword=${encodeURIComponent(normalizeAlphaBase(fallbackSymbol))}`
}

function buildBinanceAlphaFallbackUrl(symbol) {
  // Fallback только если API не успел вернуть contract. Для нормального клика UI обновит ссылку асинхронно.
  return `https://www.binance.com/en/alpha?keyword=${encodeURIComponent(normalizeAlphaBase(symbol))}`
}

function buildExchangeTradeUrlFromParts(exchangeName, base, quote = 'USDT', contract = null) {
  const ex = String(exchangeName || '').trim()
  const exKey = ex.toLowerCase()
  const normalizedBase = String(base || '').trim().toUpperCase()
  const normalizedQuote = String(quote || 'USDT').trim().toUpperCase()
  if (!ex || !normalizedBase || !normalizedQuote) return ''

  if (exKey.includes('gate')) return `https://www.gate.io/trade/${normalizedBase}_${normalizedQuote}`
  if (exKey.includes('kucoin')) return `https://www.kucoin.com/trade/${normalizedBase}-${normalizedQuote}`
  if (exKey.includes('mexc')) return `https://www.mexc.com/exchange/${normalizedBase}_${normalizedQuote}`
  if (exKey.includes('coinex')) return `https://www.coinex.com/exchange/${normalizedBase}-${normalizedQuote}`
  if (exKey.includes('bybit')) return `https://www.bybit.com/en/trade/spot/${normalizedBase}/${normalizedQuote}`
  if (exKey.includes('okx') || exKey.includes('okex')) return `https://www.okx.com/trade-spot/${normalizedBase.toLowerCase()}-${normalizedQuote.toLowerCase()}`
  if (exKey.includes('binance alpha')) return buildBinanceAlphaFallbackUrl(`${normalizedBase}${normalizedQuote}`)
  if (exKey.includes('binance.us') || exKey === 'binanceus') return `https://www.binance.us/trade/${normalizedBase}_${normalizedQuote}`
  if (exKey.includes('kraken')) return `https://pro.kraken.com/app/trade/${normalizedBase}-${normalizedQuote}`
  if (exKey.includes('pionex')) return `https://www.pionex.us/en-US/trade/${normalizedBase}_${normalizedQuote}/Manual`
  if (exKey.includes('lbank')) return `https://www.lbank.com/trade/${normalizedBase}_${normalizedQuote.toLowerCase()}`
  if (exKey.includes('bitget')) return `https://www.bitget.com/spot/${normalizedBase}${normalizedQuote}`
  if (exKey.includes('htx')) return `https://www.htx.com/trade/${normalizedBase.toLowerCase()}_${normalizedQuote.toLowerCase()}`
  if (exKey.includes('bingx')) return `https://bingx.com/en-us/spot/${normalizedBase}${normalizedQuote}/`
  if (exKey.includes('bitrue')) return `https://www.bitrue.com/trade/${normalizedBase}_${normalizedQuote}`
  if (exKey.includes('tradeogre')) return `https://tradeogre.com/exchange/${normalizedQuote}-${normalizedBase}`
  if (exKey.includes('nonkyc')) return `https://nonkyc.io/markets/${normalizedBase}-${normalizedQuote}`
  if (exKey.includes('safetrade')) return `https://safe.trade/markets/${normalizedBase}-${normalizedQuote}`
  if (exKey.includes('kyber')) {
    const parts = exKey.split('/');
    let chain = parts.length > 1 ? parts[1].toLowerCase() : 'ethereum';
    if (chain === 'bsc') chain = 'bnb';
    let q = normalizedQuote.toLowerCase();
    if (q === 'usd') q = 'usdc'; // KyberSwap requires actual tokens, fallback to USDC
    let tokenIn = contract ? contract : normalizedBase.toLowerCase();
    return `https://kyberswap.com/swap/${chain}/${tokenIn}-to-${q}`;
  }

  return `https://www.google.com/search?q=${encodeURIComponent(`${ex} ${normalizedBase}${normalizedQuote}`)}`
}

// Генерирует URL для торговой пары на бирже
function getExchangeUrl(exchange, symbol, contract = null) {
  if (!exchange || !symbol) return null
  const { base, quote } = splitNormalizedPairSymbol(symbol)
  const fallbackBase = String(symbol || '').trim().toUpperCase().replace(/[^A-Z0-9]/g, '')
  const normalizedBase = base || fallbackBase
  const normalizedQuote = quote || 'USDT'
  return buildExchangeTradeUrlFromParts(exchange, normalizedBase, normalizedQuote, contract) || null
}

// Инициализация при загрузке страницы
document.addEventListener("DOMContentLoaded", () => {
  console.log("Инициализация приложения...")

  // Проверяем наличие Bootstrap
  if (typeof bootstrap === "undefined") {
    console.error("Bootstrap не найден. Загружаем Bootstrap...")
    loadBootstrap()
  } else {
    console.log("Bootstrap найден, продолжаем инициализацию.")
    initApp()
  }
})

// Загрузка Bootstrap, если он не найден
function loadBootstrap() {
  const script = document.createElement("script")
  script.src = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"
  script.integrity = "sha384-C6RzsynM9kWDrMNeT87bh95OGNyZPhcTNXj1NW7RuBCsyN/o0jlpcV8Qyq46cDfL"
  script.crossOrigin = "anonymous"

  script.onload = () => {
    console.log("Bootstrap успешно загружен.")
    // Объявляем глобальную переменную bootstrap
    window.bootstrap = bootstrap
    initApp()
  }

  script.onerror = () => {
    console.error("Не удалось загрузить Bootstrap. Приложение может работать некорректно.")
    initApp()
  }

  document.head.appendChild(script)
}

// Основная инициализация приложения
function initApp() {
  console.log("Инициализация элементов управления...")

  // Инициализация кнопок
  const startButton = document.getElementById("startButton")
  if (startButton) startButton.addEventListener("click", startBot)

  const stopButton = document.getElementById("stopButton")
  if (stopButton) stopButton.addEventListener("click", stopBot)

  const updatePairsButton = document.getElementById("updatePairsButton")
  if (updatePairsButton) updatePairsButton.addEventListener("click", updatePairs)

  const updateCoingeckoListButton = document.getElementById("updateCoingeckoListButton")
  if (updateCoingeckoListButton) updateCoingeckoListButton.addEventListener("click", updateCoingeckoList)

  const saveSettingsButton = document.getElementById("saveSettingsButton")
  if (saveSettingsButton) saveSettingsButton.addEventListener("click", saveSettings)
  // Обработчики чекбоксов немедленно применяют видимость
  const c1 = document.getElementById('showMomentum1mCheckbox')
  const c15 = document.getElementById('showMomentum15mCheckbox')
  const ch = document.getElementById('showHeatCheckbox')
  const cd = document.getElementById('showDispersionCheckbox')
  const cgVol = document.getElementById('showCgVolCheckbox')
  const cgMcap = document.getElementById('showCgMcapCheckbox')
  const dirCb = document.getElementById('showDirectionCheckbox')
  const groupByLiq = document.getElementById('groupByLiquidityCheckbox')
  const groupBySymbol = document.getElementById('groupBySymbolCheckbox')
  const filterTransferCb = document.getElementById('filterTransferCheckbox')
  const filterTransferStrictCb = document.getElementById('filterTransferStrictCheckbox')
  const useOrderbooksCb = document.getElementById('useOrderbooksCheckbox')
  const orderbooksRow = document.getElementById('orderbooksSettingsRow')
  const obTopNInput = document.getElementById('orderbooksTopNInput')
  const obTimeoutInput = document.getElementById('orderbooksTimeoutInput')
  const applyDisplayToggleState = () => {
    settings.ui_show_momentum_1m = c1 ? c1.checked : settings.ui_show_momentum_1m
    settings.ui_show_momentum_15m = c15 ? c15.checked : settings.ui_show_momentum_15m
    settings.ui_show_heat = ch ? ch.checked : settings.ui_show_heat
    settings.ui_show_dispersion = cd ? cd.checked : settings.ui_show_dispersion
    settings.ui_show_cg_vol24 = cgVol ? cgVol.checked : settings.ui_show_cg_vol24
    settings.ui_show_cg_mcap = cgMcap ? cgMcap.checked : settings.ui_show_cg_mcap
    settings.ui_show_direction = dirCb ? dirCb.checked : settings.ui_show_direction
  }

    ;[c1, c15, ch, cd, cgVol, cgMcap, dirCb].forEach(cb => {
      if (cb) cb.addEventListener('change', () => {
        // Локально обновим settings и применим
        settings.ui_show_momentum_1m = c1 ? c1.checked : settings.ui_show_momentum_1m
        settings.ui_show_momentum_15m = c15 ? c15.checked : settings.ui_show_momentum_15m
        settings.ui_show_heat = ch ? ch.checked : settings.ui_show_heat
        settings.ui_show_dispersion = cd ? cd.checked : settings.ui_show_dispersion
        settings.ui_show_cg_vol24 = cgVol ? cgVol.checked : settings.ui_show_cg_vol24
        settings.ui_show_cg_mcap = cgMcap ? cgMcap.checked : settings.ui_show_cg_mcap
        settings.ui_show_direction = dirCb ? dirCb.checked : settings.ui_show_direction
        applyColumnVisibility()
        // Перерисуем таблицу, чтобы отразить изменения
        try { updateOpportunitiesTable() } catch (_) { }
      })
    })
    ;[filterTransferCb].forEach(cb => {
      if (cb) cb.addEventListener('change', () => {
        settings.ui_arb_filter_transfer = cb.checked
        // Сохраняем на сервер и обновляем таблицу
        fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ui_arb_filter_transfer: cb.checked })
        }).then(() => updateOpportunities()).catch(() => {})
      })
    })
    ;[filterTransferStrictCb].forEach(cb => {
      if (cb) cb.addEventListener('change', () => {
        settings.ui_arb_filter_transfer_strict_unknown = cb.checked
        fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ui_arb_filter_transfer_strict_unknown: cb.checked })
        }).then(() => updateOpportunities()).catch(() => {})
      })
    })

    if (useOrderbooksCb) {
      const syncObRow = () => {
        if (!orderbooksRow) return
        orderbooksRow.style.display = useOrderbooksCb.checked ? '' : 'none'
      }
      syncObRow()
      useOrderbooksCb.addEventListener('change', () => {
        settings.use_orderbooks = useOrderbooksCb.checked
        syncObRow()
        const topN = obTopNInput ? Number.parseInt(obTopNInput.value, 10) : NaN
        const timeoutSec = obTimeoutInput ? Number.parseFloat(obTimeoutInput.value) : NaN
        const payload = { use_orderbooks: useOrderbooksCb.checked }
        if (!isNaN(topN)) payload.orderbooks_refine_top_symbols = topN
        if (!isNaN(timeoutSec)) payload.orderbooks_per_exchange_timeout_sec = timeoutSec
        fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        }).then(() => updateOpportunities()).catch(() => {})
      })
    }
    // Фильтр по ликвидности стакана
    const filterLiqCb = document.getElementById('filterLiquidityCheckbox')
    if (filterLiqCb) filterLiqCb.addEventListener('change', () => {
      settings.ui_arb_filter_liquidity = filterLiqCb.checked
      const payload = { ui_arb_filter_liquidity: filterLiqCb.checked }
      if (filterLiqCb.checked) {
        settings.use_orderbooks = true
        payload.use_orderbooks = true
        const currentTop = Number.parseInt(obTopNInput ? obTopNInput.value : settings.orderbooks_refine_top_symbols, 10)
        if (!Number.isFinite(currentTop) || currentTop <= 0) {
          settings.orderbooks_refine_top_symbols = 5
          payload.orderbooks_refine_top_symbols = 5
          if (obTopNInput) obTopNInput.value = '5'
        }
        if (useOrderbooksCb) useOrderbooksCb.checked = true
        if (orderbooksRow) orderbooksRow.style.display = ''
      }
      fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      }).then(() => updateOpportunities()).catch(() => {})
    })
    ;[cgVol, cgMcap].forEach(cb => {
      if (cb) cb.addEventListener('change', () => {
        settings.ui_show_cg_vol24 = cgVol ? cgVol.checked : settings.ui_show_cg_vol24
        settings.ui_show_cg_mcap = cgMcap ? cgMcap.checked : settings.ui_show_cg_mcap
        applyColumnVisibility()
        try { updateOpportunitiesTable() } catch (_) { }
      })
    })

  if (groupByLiq) groupByLiq.addEventListener('change', () => {
    settings.ui_group_by_liquidity = groupByLiq.checked
    fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ui_group_by_liquidity: groupByLiq.checked })
    }).then(() => {
      applyColumnVisibility()
      renderCurrentScannerTable()
    }).catch(() => {
      applyColumnVisibility()
      renderCurrentScannerTable()
    })
  })

  if (groupBySymbol) groupBySymbol.addEventListener('change', () => {
    settings.ui_group_by_symbol = groupBySymbol.checked
    fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ui_group_by_symbol: groupBySymbol.checked })
    }).then(() => {
      try { updateOpportunitiesTable() } catch (_) { }
    }).catch(() => {
      try { updateOpportunitiesTable() } catch (_) { }
    })
  })

  const addToBlacklistButton = document.getElementById("addToBlacklistButton")
  if (addToBlacklistButton) addToBlacklistButton.addEventListener("click", addToBlacklist)
  const scannerModeCex = document.getElementById("scannerModeCex")
  const scannerModeCexDex = document.getElementById("scannerModeCexDex")
  const scannerModeKrakenKyber = document.getElementById("scannerModeKrakenKyber")
  const scannerModeInterchain = document.getElementById("scannerModeInterchain")
  const chainScopeSelect = document.getElementById("chainScopeSelect")
  let assetProfileSelect = document.getElementById("assetProfileSelect")
  if (!assetProfileSelect) {
    const toolbarSide = document.querySelector(".toolbar-side")
    const chainScopeControl = document.getElementById("chainScopeControl")
    if (toolbarSide) {
      const assetProfileControl = document.createElement("div")
      assetProfileControl.id = "assetProfileControl"
      assetProfileControl.className = "toolbar-filter d-none"
      assetProfileControl.innerHTML = `
        <span class="small text-dim">Монеты</span>
        <select id="assetProfileSelect" class="form-select form-select-sm compact-select">
          <option value="balanced" selected>Баланс</option>
          <option value="majors">Мейджоры</option>
          <option value="long_tail">Long-tail</option>
        </select>
      `
      if (chainScopeControl && chainScopeControl.parentNode === toolbarSide) {
        toolbarSide.insertBefore(assetProfileControl, chainScopeControl.nextSibling)
      } else {
        toolbarSide.prepend(assetProfileControl)
      }
      assetProfileSelect = assetProfileControl.querySelector("#assetProfileSelect")
    }
  }
  if (scannerModeCex) scannerModeCex.addEventListener("click", () => setScannerMode('cex'))
  if (scannerModeCexDex) scannerModeCexDex.addEventListener("click", () => setScannerMode('cex_dex'))
  if (scannerModeKrakenKyber) scannerModeKrakenKyber.addEventListener("click", () => setScannerMode('kraken_kyber'))
  const scannerModeKrakenKyber2 = document.getElementById("scannerModeKrakenKyber2")
  if (scannerModeKrakenKyber2) scannerModeKrakenKyber2.addEventListener("click", () => setScannerMode('kraken_kyber2'))
  if (scannerModeInterchain) scannerModeInterchain.addEventListener("click", () => setScannerMode('interchain'))
  if (chainScopeSelect) {
    chainScopeSelect.value = chainScope
    chainScopeSelect.addEventListener("change", function () {
      chainScope = String(this.value || 'all').trim().toLowerCase()
      if (!['all', 'major', 'small'].includes(chainScope)) chainScope = 'all'
      updateOpportunities()
    })
  }
  if (assetProfileSelect) {
    assetProfileSelect.value = assetProfile
    assetProfileSelect.addEventListener("change", function () {
      assetProfile = String(this.value || 'balanced').trim().toLowerCase()
      if (!['long_tail', 'balanced', 'majors'].includes(assetProfile)) assetProfile = 'balanced'
      updateOpportunities()
    })
  }

  // Инициализация фильтра по спреду
  const minSpreadInput = document.getElementById("minSpreadInput")
  if (minSpreadInput) {
    minSpreadInput.addEventListener("change", function () {
      minSpread = Number.parseFloat(this.value)
      updateOpportunities()
    })
  }

  // Инициализация сортировки таблицы
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.addEventListener("click", function () {
      const field = this.getAttribute("data-sort")
      const order = currentSort.field === field && currentSort.order === "desc" ? "asc" : "desc"

      // Обновляем текущую сортировку
      currentSort = { field, order }

      // Обновляем классы для отображения направления сортировки
      document.querySelectorAll("th[data-sort]").forEach((el) => {
        el.classList.remove("sort-asc", "sort-desc")
      })

      this.classList.add(order === "asc" ? "sort-asc" : "sort-desc")

      // Обновляем таблицу
      updateOpportunities()
    })
  })

  // Добавляем модальное окно для арбитража монеты, если его еще нет
  if (!document.getElementById("coinArbitrageModal")) {
    const arbitrageModal = document.createElement("div")
    arbitrageModal.className = "modal fade"
    arbitrageModal.id = "coinArbitrageModal"
    arbitrageModal.tabIndex = "-1"
    arbitrageModal.setAttribute("aria-labelledby", "coinArbitrageModalTitle")
    arbitrageModal.setAttribute("aria-hidden", "true")

    arbitrageModal.innerHTML = `
      <div class="modal-dialog modal-lg">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title" id="coinArbitrageModalTitle">Арбитраж монеты</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
          </div>
          <div class="modal-body">
            <div class="text-center">
              <div class="spinner-border" role="status"></div>
              <p>Загрузка данных...</p>
            </div>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Закрыть</button>
          </div>
        </div>
      </div>
    `

    document.body.appendChild(arbitrageModal)
  }

  // Инициализация модальных окон
  initModals()

  // Запускаем обновление данных
  updateStatus()
  updateOpportunities()
  loadSettings()
  loadBlacklist()
  initFavorites()
  initWarnMarks()
  // Устанавливаем интервалы обновления (настраивается в "Интервал обновления")
  applyPollingIntervals()
  // Поднимаем WebSocket-соединение для real-time котировок (fallback если WS сервер недоступен).
  try { connectLiveQuotesWS() } catch (e) { console.warn('[WS] init failed:', e) }

  console.log("Инициализация завершена.")
}

// ================================================================
// Real-time price stream via WebSocket (ws_server on port 8090).
// ================================================================
// Data flow:
//   Exchanges (Bybit/Bitget/Gate.io/KuCoin/OKX) --[WS]--> ws_server
//   ws_server --[WS]--> browser  (every 1s: fresh opportunities + quotes)
//
// Polling for /api/opportunities is still active as a slower fallback
// (7-15s), but the WS stream drives live price/spread updates in the
// table. Deposit/withdraw statuses (asset_status) change rarely so they
// stay on the HTTP path with a 5-minute client cache.
window.__wsLiveSocket = null
window.__wsLiveConnected = false
window.__wsLiveLastSnapshotTs = 0
window.__wsLiveReconnectAttempt = 0
window.__wsLiveQuotes = {}
window.__wsLiveOpportunities = []

function connectLiveQuotesWS() {
  try {
    const scheme = (location.protocol === 'https:') ? 'wss' : 'ws'
    const host = location.hostname || '127.0.0.1'
    const wsPort = (window.__wsServerPort && Number.isFinite(window.__wsServerPort)) ? window.__wsServerPort : 8090
    const url = `${scheme}://${host}:${wsPort}/ws`

    console.log(`[WS] connecting to ${url}`)
    const ws = new WebSocket(url)
    window.__wsLiveSocket = ws

    ws.addEventListener('open', () => {
      window.__wsLiveConnected = true
      window.__wsLiveReconnectAttempt = 0
      console.log('[WS] connected — streaming live quotes')
      try {
        const badge = document.getElementById('wsStatusBadge')
        if (badge) {
          badge.textContent = 'WS: live'
          badge.className = 'badge bg-success ms-2'
        }
      } catch (_) { }
    })

    ws.addEventListener('message', (evt) => {
      try {
        const msg = JSON.parse(evt.data)
        if (!msg || msg.type !== 'snapshot') return
        window.__wsLiveLastSnapshotTs = Date.now()
        window.__wsLiveOpportunities = Array.isArray(msg.opportunities) ? msg.opportunities : []
        window.__wsLiveQuotes = (msg.quotes && typeof msg.quotes === 'object') ? msg.quotes : {}
        try { applyWsQuotesToTable() } catch (e) { console.debug('[WS] apply err:', e) }
      } catch (e) {
        console.debug('[WS] parse error:', e && e.message)
      }
    })

    ws.addEventListener('close', () => {
      window.__wsLiveConnected = false
      console.warn('[WS] disconnected')
      try {
        const badge = document.getElementById('wsStatusBadge')
        if (badge) {
          badge.textContent = 'WS: off'
          badge.className = 'badge bg-secondary ms-2'
        }
      } catch (_) { }
      // Exponential backoff: 2s, 4s, 8s, 16s, cap 30s.
      const attempt = Math.min(4, (window.__wsLiveReconnectAttempt || 0) + 1)
      window.__wsLiveReconnectAttempt = attempt
      const delay = Math.min(30000, 2000 * Math.pow(2, attempt - 1))
      setTimeout(() => {
        if (!window.__wsLiveConnected) connectLiveQuotesWS()
      }, delay)
    })

    ws.addEventListener('error', (e) => {
      console.warn('[WS] error event:', e && e.type)
    })
  } catch (e) {
    console.warn('[WS] connect failed:', e)
  }
}

// Apply WS-live quotes to the visible main CEX↔CEX table rows. Only updates
// prices and recalculates spread; all other columns (net %, market,
// volume/mcap, transfer chain) keep their last polled values — those are
// computed on the backend using data unrelated to WS tickers.
function applyWsQuotesToTable() {
  if (scannerMode !== 'cex') return  // WS quotes only cover CEX↔CEX
  const quotesByEx = window.__wsLiveQuotes || {}
  if (!quotesByEx || Object.keys(quotesByEx).length === 0) return

  // Build a fast lookup: {exchange_lower: {symbol_upper: {bid, ask, last}}}
  const lut = {}
  const exKey = (n) => String(n || '').toLowerCase().replace(/[^a-z0-9]/g, '')
  for (const [exRaw, bySym] of Object.entries(quotesByEx)) {
    const ek = exKey(exRaw)
    if (!ek) continue
    lut[ek] = {}
    for (const [sym, q] of Object.entries(bySym || {})) {
      if (!q) continue
      lut[ek][String(sym).toUpperCase()] = q
    }
  }
  if (Object.keys(lut).length === 0) return

  const tableBody = document.getElementById('opportunitiesTableBody')
  if (!tableBody) return
  const rows = tableBody.querySelectorAll('tr[data-symbol]')
  let updated = 0
  rows.forEach(row => {
    try {
      const sym = String(row.getAttribute('data-symbol') || '').toUpperCase()
      const buyEx = exKey(row.getAttribute('data-buy-exchange') || '')
      const sellEx = exKey(row.getAttribute('data-sell-exchange') || '')
      if (!sym || !buyEx || !sellEx) return
      const buyQ = lut[buyEx] && lut[buyEx][sym]
      const sellQ = lut[sellEx] && lut[sellEx][sym]
      if (!buyQ && !sellQ) return
      const buyPrice = (buyQ && (buyQ.ask || buyQ.last)) ? Number(buyQ.ask || buyQ.last) : null
      const sellPrice = (sellQ && (sellQ.bid || sellQ.last)) ? Number(sellQ.bid || sellQ.last) : null

      const cells = row.querySelectorAll('td')
      // Compact layout column indices:
      //   2: Buy  3: Sell  4: Spread  5: Net%
      if (buyPrice && cells[2]) {
        const priceEl = cells[2].querySelector('.small')
        if (priceEl) priceEl.textContent = buyPrice.toFixed(8)
      }
      if (sellPrice && cells[3]) {
        const priceEl = cells[3].querySelector('.small')
        if (priceEl) priceEl.textContent = sellPrice.toFixed(8)
      }
      if (buyPrice && sellPrice && buyPrice > 0 && cells[4]) {
        const spread = ((sellPrice / buyPrice) - 1.0) * 100.0
        cells[4].textContent = `${spread.toFixed(2)}%`
        // Visual ping: briefly highlight spread cell when it changes meaningfully.
        const prev = Number(row.dataset.lastSpread || 0)
        if (Math.abs(spread - prev) >= 0.05) {
          row.dataset.lastSpread = String(spread)
          cells[4].style.transition = 'background-color 0.4s ease'
          cells[4].style.backgroundColor = spread > prev ? 'rgba(40,167,69,0.25)' : 'rgba(220,53,69,0.25)'
          setTimeout(() => { try { cells[4].style.backgroundColor = '' } catch (_) { } }, 600)
        }
      }
      // Record that this row just got a live WS update — used by
      // refreshRowFreshnessMarkers() to paint a "LIVE"/"REST"/"STALE" badge.
      try {
        row.dataset.wsLastUpdate = String(Date.now())
        row.dataset.quoteSource = 'ws'
      } catch (_) { }
      updated++
    } catch (_) { /* per-row best-effort */ }
  })
  if (updated > 0) {
    try {
      const badge = document.getElementById('wsStatusBadge')
      if (badge) badge.setAttribute('title', `WS: обновлено ${updated} строк · ${new Date().toLocaleTimeString()}`)
    } catch (_) { }
  }
  // Repaint freshness markers immediately after a batch of updates.
  try { refreshRowFreshnessMarkers() } catch (_) { }
}

// Paint a small freshness badge next to the spread cell of every visible
// opportunity row so the user instantly sees whether the price is LIVE
// (WS, <5s), a slightly delayed WS tick (≤15s), or a REST/stale
// polled value. Runs both after every WS snapshot and on a 2s timer so
// stale rows are eventually marked even without new WS traffic.
function refreshRowFreshnessMarkers() {
  const tableBody = document.getElementById('opportunitiesTableBody')
  if (!tableBody) return
  const now = Date.now()
  const wsConnected = !!window.__wsLiveConnected
  const wsLastSnapMs = Number(window.__wsLiveLastSnapshotTs || 0)
  const wsSnapAgeMs = wsLastSnapMs > 0 ? (now - wsLastSnapMs) : Infinity

  tableBody.querySelectorAll('tr[data-symbol]').forEach(row => {
    try {
      const cells = row.querySelectorAll('td')
      // Spread cell index is 4 in the compact CEX↔CEX layout (same one the
      // WS updater writes to). Render a badge inside it if missing.
      const host = cells[4]
      if (!host) return
      let badge = host.querySelector('.quote-freshness')
      if (!badge) {
        badge = document.createElement('span')
        badge.className = 'quote-freshness'
        badge.style.cssText = 'display:inline-block;margin-left:6px;padding:1px 6px;border-radius:4px;font-size:0.62rem;line-height:1.3;vertical-align:middle;font-weight:600;'
        host.appendChild(badge)
      }
      const lastWsMs = Number(row.dataset.wsLastUpdate || 0)
      const rowAgeSec = lastWsMs > 0 ? Math.round((now - lastWsMs) / 1000) : null

      // Classification:
      //   LIVE  — WS connected AND this row got a WS tick in the last 5s
      //   SLOW  — WS connected, row tick 5-15s old (price briefly drifted)
      //   REST  — no WS tick (never updated by WS) or WS disconnected — UI
      //           is showing polled REST data.
      //   OLD   — WS tick older than 60s; treat as seriously stale.
      let label, bg, color, title
      if (rowAgeSec !== null && rowAgeSec <= 5 && wsConnected) {
        label = 'LIVE'
        bg = 'rgba(40,167,69,0.85)'
        color = '#fff'
        title = `WS live · ${rowAgeSec}с`
      } else if (rowAgeSec !== null && rowAgeSec <= 15 && wsConnected) {
        label = `WS ${rowAgeSec}с`
        bg = 'rgba(23,162,184,0.70)'
        color = '#fff'
        title = `WS stream · ${rowAgeSec}s old`
      } else if (rowAgeSec !== null && rowAgeSec <= 60) {
        label = `WS ${rowAgeSec}с`
        bg = 'rgba(255,193,7,0.80)'
        color = '#000'
        title = `WS tick ${rowAgeSec}s old (stream may be stalled)`
      } else if (wsConnected && wsSnapAgeMs < 15000) {
        // WS connected and actively delivering, but THIS row hasn't been
        // updated — the exchange/symbol isn't in the WS stream at all.
        label = 'REST'
        bg = 'rgba(108,117,125,0.75)'
        color = '#fff'
        title = 'Symbol not covered by WS feed — using 30s REST poll'
      } else {
        label = 'REST'
        bg = 'rgba(108,117,125,0.75)'
        color = '#fff'
        title = wsConnected
          ? 'WS stream idle - using REST snapshot'
          : 'WebSocket disconnected - using REST poll only'
      }
      badge.textContent = label
      badge.style.background = bg
      badge.style.color = color
      badge.title = title
    } catch (_) { /* per-row best-effort */ }
  })
}

// Kick the freshness repaint on a light 2s cadence so badges decay from
// LIVE -> WS age -> REST even when WS traffic dries up.
if (!window.__freshnessMarkerTimer) {
  window.__freshnessMarkerTimer = setInterval(() => {
    try { refreshRowFreshnessMarkers() } catch (_) { }
  }, 2000)
}

// ---- UI polling ----
function getPollMsFor(kind) {
  // kind: 'status' | 'opportunities'
  try {
    const useSeparate = !!(settings && settings.ui_use_separate_polling)
    const baseSec = (settings && typeof settings.ui_polling_interval_sec === 'number' && settings.ui_polling_interval_sec >= 1)
      ? settings.ui_polling_interval_sec : 7
    if (useSeparate) {
      let sec = baseSec
      if (kind === 'status' && typeof settings.ui_polling_interval_status_sec === 'number' && settings.ui_polling_interval_status_sec >= 1) sec = settings.ui_polling_interval_status_sec
      if (kind === 'opportunities' && typeof settings.ui_polling_interval_opportunities_sec === 'number' && settings.ui_polling_interval_opportunities_sec >= 1) sec = settings.ui_polling_interval_opportunities_sec
      return sec * 1000
    }
    return baseSec * 1000
  } catch (_) { return 7000 }
}
// Флаг паузы автообновления UI
window.__pollingPaused = window.__pollingPaused === true

function applyPollingIntervals() {
  if (window.__pollingPaused) {
    // если пауза включена — зачистим интервалы и не будем перезапускать
    try { if (statusUpdateInterval) clearInterval(statusUpdateInterval) } catch (_) { }
    try { if (opportunitiesUpdateInterval) clearInterval(opportunitiesUpdateInterval) } catch (_) { }
    console.debug('UI polling paused — intervals not scheduled.')
    return
  }
  try { if (statusUpdateInterval) clearInterval(statusUpdateInterval) } catch (_) { }
  try { if (opportunitiesUpdateInterval) clearInterval(opportunitiesUpdateInterval) } catch (_) { }
  const statusMs = getPollMsFor('status')
  const oppsMs = getPollMsFor('opportunities')
  statusUpdateInterval = setInterval(updateStatus, statusMs)
  opportunitiesUpdateInterval = setInterval(updateOpportunities, oppsMs)
}

// Поставить паузу автообновления
function pausePolling() {
  window.__pollingPaused = true
  try { if (statusUpdateInterval) clearInterval(statusUpdateInterval) } catch (_) { }
  try { if (opportunitiesUpdateInterval) clearInterval(opportunitiesUpdateInterval) } catch (_) { }
}

// Снять паузу и перезапустить интервалы
function resumePolling() {
  window.__pollingPaused = false
  applyPollingIntervals()
}
// Инициализация модальных окон
function initModals() {
  // Находим все модальные окна
  const modalElements = document.querySelectorAll(".modal")

  // Инициализируем каждое модальное окно
  modalElements.forEach((modalEl) => {
    // Находим кнопки закрытия
    const closeButtons = modalEl.querySelectorAll('[data-bs-dismiss="modal"], .close, .btn-close')

    // Добавляем обработчики для кнопок закрытия
    closeButtons.forEach((button) => {
      button.addEventListener("click", () => {
        // Используем Bootstrap API для закрытия модального окна
        if (typeof bootstrap !== "undefined") {
          const modal = bootstrap.Modal.getInstance(modalEl)
          if (modal) {
            modal.hide()
          }
        } else {
          // Резервный вариант, если bootstrap не доступен
          modalEl.style.display = "none"
          document.body.classList.remove("modal-open")
          const backdrop = document.querySelector(".modal-backdrop")
          if (backdrop) backdrop.remove()
        }
      })
    })
  })
}

// Функция для запуска бота
function startBot() {
  console.log("Запуск бота...")
  fetch("/api/start", {
    method: "POST",
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.success) {
        console.log("Бот успешно запущен.")
        showNotification("Бот запущен", "success")
        updateStatus()
      } else {
        console.error("Ошибка при запуске бота:", data.error)
        showNotification("Ошибка при запуске бота: " + data.error, "error")
      }
    })
    .catch((error) => {
      console.error("Ошибка при отправке запроса на запуск бота:", error)
      showNotification("Ошибка при отправке запроса на запуск бота", "error")
    })
}

// Функция для остановки бота
function stopBot() {
  console.log("Остановка бота...")
  fetch("/api/stop", {
    method: "POST",
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.success) {
        console.log("Бот успешно остановлен.")
        showNotification("Бот остановлен", "success")
        updateStatus()
      } else {
        console.error("Ошибка при остановке бота:", data.error)
        showNotification("Ошибка при остановке бота: " + data.error, "error")
      }
    })
    .catch((error) => {
      console.error("Ошибка при отправке запроса на остановку бота:", error)
      showNotification("Ошибка при отправке запроса на остановку бота", "error")
    })
}

// Функция для обновления пар
function updatePairs() {
  console.log("Обновление пар...")
  fetch("/api/update_pairs", {
    method: "POST",
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.success) {
        console.log("Обновление пар запущено.")
        showNotification("Обновление пар запущено", "success")
      } else {
        console.error("Ошибка при обновлении пар:", data.error)
        showNotification("Ошибка при обновлении пар: " + data.error, "error")
      }
    })
    .catch((error) => {
      console.error("Ошибка при отправке запроса на обновление пар:", error)
      showNotification("Ошибка при отправлении запроса на обновление пар", "error")
    })
}

// Функция для обновления статуса
function updateStatus() {
  console.log("Обновление статуса...")
  fetch("/api/status")
    .then((response) => response.json())
    .then((data) => {
      if (data.error) {
        console.error("Ошибка при получении статуса:", data.error)
        return
      }

      // Обновляем статус
      const statusValue = document.getElementById("statusValue")
      const statusIndicator = document.getElementById("statusIndicator")

      if (statusValue) {
        statusValue.textContent = data.running ? "ОНЛАЙН" : "ОФФЛАЙН"
        statusValue.style.color = data.running ? "var(--accent-green)" : "var(--text-dim)"
      }

      if (statusIndicator) {
        if (data.running) {
          statusIndicator.classList.add("online")
        } else {
          statusIndicator.classList.remove("online")
        }
      }

      // Обновляем количество пар и возможностей
      const commonPairsValue = document.getElementById("commonPairsValue")
      const opportunitiesValue = document.getElementById("opportunitiesValue")

      if (commonPairsValue) commonPairsValue.textContent = data.common_pairs || 0
      if (opportunitiesValue) opportunitiesValue.textContent = data.total_opportunities || 0

      // Обновляем время/возраст последнего обновления
      setLastUpdateBadge(data)

      // Обновляем состояние кнопок
      const startButton = document.getElementById("startButton")
      const stopButton = document.getElementById("stopButton")

      if (startButton && stopButton) {
        if (data.running) {
          startButton.disabled = true
          stopButton.disabled = false
        } else {
          startButton.disabled = false
          stopButton.disabled = true
        }
      }
    })
    .catch((error) => {
      console.error("Ошибка при обновлении статуса:", error)
    })
}

function setScannerMode(mode) {
  if (mode === 'interchain') scannerMode = 'interchain'
  else if (mode === 'cex_dex') scannerMode = 'cex_dex'
  else if (mode === 'kraken_kyber') scannerMode = 'kraken_kyber'
  else if (mode === 'kraken_kyber2') scannerMode = 'kraken_kyber2'
  else scannerMode = 'cex'

  const cexButton = document.getElementById("scannerModeCex")
  const cexDexButton = document.getElementById("scannerModeCexDex")
  const krakenKyberButton = document.getElementById("scannerModeKrakenKyber")
  const krakenKyber2Button = document.getElementById("scannerModeKrakenKyber2")
  const interchainButton = document.getElementById("scannerModeInterchain")
  const chainScopeControl = document.getElementById("chainScopeControl")
  const assetProfileControl = document.getElementById("assetProfileControl")
  const cexPanel = document.getElementById("cexScannerPanel")
  const cexDexPanel = document.getElementById("cexDexScannerPanel")
  const krakenKyberPanel = document.getElementById("krakenKyberScannerPanel")
  const krakenKyber2Panel = document.getElementById("krakenKyber2ScannerPanel")
  const interchainPanel = document.getElementById("interchainScannerPanel")

  if (cexButton) cexButton.classList.toggle('active', scannerMode === 'cex')
  if (cexDexButton) cexDexButton.classList.toggle('active', scannerMode === 'cex_dex')
  if (krakenKyberButton) krakenKyberButton.classList.toggle('active', scannerMode === 'kraken_kyber')
  if (krakenKyber2Button) krakenKyber2Button.classList.toggle('active', scannerMode === 'kraken_kyber2')
  if (interchainButton) interchainButton.classList.toggle('active', scannerMode === 'interchain')
  if (chainScopeControl) chainScopeControl.classList.toggle('d-none', scannerMode === 'cex' || scannerMode === 'kraken_kyber' || scannerMode === 'kraken_kyber2')
  if (assetProfileControl) assetProfileControl.classList.toggle('d-none', scannerMode === 'cex' || scannerMode === 'kraken_kyber' || scannerMode === 'kraken_kyber2')
  if (cexPanel) cexPanel.classList.toggle('d-none', scannerMode !== 'cex')
  if (cexDexPanel) cexDexPanel.classList.toggle('d-none', scannerMode !== 'cex_dex')
  if (krakenKyberPanel) krakenKyberPanel.classList.toggle('d-none', scannerMode !== 'kraken_kyber')
  if (krakenKyber2Panel) krakenKyber2Panel.classList.toggle('d-none', scannerMode !== 'kraken_kyber2')
  if (interchainPanel) interchainPanel.classList.toggle('d-none', scannerMode !== 'interchain')

  // Mount and toggle the client-side filter panel for CEX↔DEX and межсетевой.
  try { ensureInterchainFiltersPanel() } catch (_) {}
  const interchainFiltersPanel = document.getElementById('interchainFiltersPanel')
  if (interchainFiltersPanel) {
    interchainFiltersPanel.classList.toggle('d-none', scannerMode === 'cex' || scannerMode === 'kraken_kyber')
  }

  currentSort = { field: "spread", order: "desc" }
  updateOpportunities()
}

// Функция для обновления возможностей
function updateOpportunities() {
  console.log("Обновление возможностей...")
  let sortField = currentSort.field
  let endpoint = `/api/opportunities?min_spread=${minSpread}&sort=${sortField}&order=${currentSort.order}`
  const chainScopeParam = `&chain_scope=${encodeURIComponent(chainScope)}`
  const assetProfileParam = `&asset_profile=${encodeURIComponent(assetProfile)}`

  if (scannerMode === 'kraken_kyber') refreshKrakenKyberIndexStatus(false)

  if (scannerMode === 'interchain') {
    endpoint = `/api/interchain_opportunities?min_spread=${minSpread}&limit=200&asset_limit=120&notional_usd=${settings.arb_min_notional_usd || 300}&route_group=cross_chain${chainScopeParam}${assetProfileParam}`
  } else if (scannerMode === 'cex_dex') {
    endpoint = `/api/interchain_opportunities?min_spread=${minSpread}&limit=200&asset_limit=120&notional_usd=${settings.arb_min_notional_usd || 300}&route_group=cex_dex${chainScopeParam}${assetProfileParam}`
  } else if (scannerMode === 'kraken_kyber' || scannerMode === 'kraken_kyber2') {
    const krakenKyberMinSpread = (settings && typeof settings.kraken_kyber_min_spread === 'number') ? settings.kraken_kyber_min_spread : minSpread
    endpoint = `/api/kraken_kyber_opportunities?min_spread=${krakenKyberMinSpread}&max_spread=${settings.max_spread || 100}&limit=200&notional_usd=${settings.kraken_kyber_notional_usd || 10}`
  }

  fetch(endpoint)
    .then((response) => response.json())
    .then((data) => {
      if (data.success) {
        opportunities = data.data || []
        if (scannerMode === 'interchain' || scannerMode === 'cex_dex' || scannerMode === 'kraken_kyber2') {
          updateInterchainTable(data)
        } else if (scannerMode === 'kraken_kyber') {
          updateKrakenKyberTable(data)
        } else {
          updateOpportunitiesTable()
        }

        const opportunitiesValue = document.getElementById("opportunitiesValue")
        if (opportunitiesValue) opportunitiesValue.textContent = opportunities.length
        setLastUpdateBadge(data)
      } else {
        console.error("Ошибка при получении возможностей:", data.error)
      }
    })
    .catch((error) => {
      console.error("Ошибка при обновлении возможностей:", error)
    })
}


function escapeHtml(v) { return String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/\'/g, "&#39;") }


function renderKrakenKyberIndexStatus(status = {}) {
  const box = document.getElementById("krakenKyberIndexStatus")
  if (!box) return
  const state = String(status.state || 'missing')
  const entries = Number(status.entries || 0)
  const age = status.age_sec == null ? null : Number(status.age_sec || 0)
  let text = ''
  if (state === 'running') {
    const done = Number(status.done || 0)
    const total = Number(status.total || 0)
    text = `⏳ ${done}/${total || '?'} · ${entries} контр.`
    box.style.color = '#ffd166'
  } else if (state === 'ready' || state === 'fresh') {
    text = `контрактов: ${entries}`
    box.style.color = '#80f5b0'
  } else {
    text = 'индекс: не загружен'
    box.style.color = '#9ee8ff'
  }
  box.textContent = text
}

function refreshKrakenKyberIndexStatus(startIfNeeded = false) {
  const url = startIfNeeded ? '/api/kraken_kyber_refresh_index' : '/api/kraken_kyber_index_status'
  fetch(url, { method: startIfNeeded ? 'POST' : 'GET' })
    .then(r => r.json())
    .then(data => { if (data && data.status) renderKrakenKyberIndexStatus(data.status) })
    .catch(() => {})
}

function updateKrakenKyberTable(payload = null) {
  if (payload && payload.meta && payload.meta.contract_index) {
    renderKrakenKyberIndexStatus(payload.meta.contract_index)
  }
  const tableBody = document.getElementById("krakenKyberTableBody")
  if (!tableBody) return
  const rows = Array.isArray(opportunities) ? opportunities : []
  tableBody.innerHTML = ""
  if (!rows.length) {
    const meta = payload && payload.meta ? payload.meta : {}
    const rejected = Array.isArray(payload && payload.rejected_sample) ? payload.rejected_sample.slice(0, 3) : []
    const note = rejected.length
      ? rejected.map(r => `${escapeHtml(r.asset || r.symbol || '?')}: ${escapeHtml(r.reason || '')}`).join('<br>')
      : `Нет маршрутов Kraken ↔ Kyber. Проверено активов: ${meta.kraken_pairs_scanned || 0}.`
    const row = document.createElement("tr")
    row.innerHTML = `<td colspan="10" class="text-center py-4 text-dim">${note}</td>`
    tableBody.appendChild(row)
    return
  }
  rows.forEach((opp, index) => {
    const row = document.createElement("tr")
    const spread = Number(opp.spread || 0)
    const buy = Number(opp.buy_price || 0)
    const sell = Number(opp.sell_price || 0)
    const asset = escapeHtml(opp.asset || opp.symbol || '')
    const chain = escapeHtml(opp.chain || '')
    const contract = String(opp.contract || '')
    const shortContract = contract ? `${contract.slice(0, 6)}...${contract.slice(-4)}` : '—'
    const notes = escapeHtml(opp.notes || opp.contract_source || '')
    const buyEx = escapeHtml(opp.buy_exchange || '')
    const sellEx = escapeHtml(opp.sell_exchange || '')
    const krDepth = opp.kraken_depth && typeof opp.kraken_depth === 'object' ? opp.kraken_depth : {}
    const fillPct = Number(krDepth.fill_pct || 0)
    const depthUsd = Number(krDepth.depth_usd || 0)
    const avgDepthPrice = Number(krDepth.avg_price || 0)
    const depthSide = escapeHtml(krDepth.side || '')
    const depthHtml = (fillPct > 0)
      ? `<div class="small">Kraken ${depthSide}: ${fillPct.toFixed(1)}% fill<br>VWAP: ${formatPrice(avgDepthPrice)}<br>Глубина: ${formatVolume(depthUsd)}</div>`
      : '<div class="small text-muted">Kraken depth: —</div>'
    const statusHtml = `<div class="small text-warning">ввод/вывод: проверять по бирже</div><div class="small text-dim">цены: Kraken depth TTL ≤ 5с · Kyber route TTL ≤ 8с</div>`
    row.innerHTML = `
      <td>${index + 1}</td>
      <td><div class="fw-bold">${asset}</div><div class="small text-dim">${escapeHtml(opp.coin_name || opp.coin_id || '')}</div></td>
      <td>${buyEx}</td>
      <td>${sellEx}</td>
      <td><span class="badge bg-dark border border-secondary">${chain}</span><div class="small text-dim" title="${escapeHtml(contract)}">${escapeHtml(shortContract)}</div></td>
      <td>${formatQuotePrice(buy)}</td>
      <td>${formatQuotePrice(sell)}</td>
      <td><span class="badge ${spread >= 0 ? 'bg-success' : 'bg-danger'}">${spread.toFixed(2)}%</span></td>
      <td>${depthHtml}${statusHtml}</td>
      <td><span class="badge bg-success">HIGH</span><div class="small text-dim">${notes}</div></td>
    `
    tableBody.appendChild(row)
  })
}

function updateInterchainTable(payload = null) {
  // Compact 9-column layout (rework 2026-04):
  //   # | Монета | Где купить | Где продать | Мост/Напр. | Спред% | Чистая/ROI | Сети | Действия
  //
  // Single function renders two tabs (scannerMode='cex_dex' or 'interchain'),
  // only the 5th column label differs (Направление vs Мост).
  const isCexDex = scannerMode === 'cex_dex'
  const isKrakenKyber2 = scannerMode === 'kraken_kyber2'
  const tableBody = document.getElementById(isKrakenKyber2 ? "krakenKyber2TableBody" : (isCexDex ? "cexDexTableBody" : "interchainTableBody"))
  if (!tableBody) return
  const scopeLabel = chainScope === 'major' ? 'Крупные сети' : chainScope === 'small' ? 'Мелкие сети' : 'Все сети'
  const totalReceived = Array.isArray(opportunities) ? opportunities.length : 0
  const visibleRows = applyInterchainFilters(opportunities)
  const filteredOut = totalReceived - visibleRows.length
  const baseEmptyLabel = isKrakenKyber2
    ? `Нет маршрутов Kraken ↔ Kyber`
    : (isCexDex
      ? `Нет маршрутов CEX ↔ DEX · ${scopeLabel}`
      : `Нет межсетевых маршрутов · ${scopeLabel}`)
  const emptyStateLabel = filteredOut > 0
    ? `${baseEmptyLabel} — ${filteredOut} скрыто фильтрами`
    : baseEmptyLabel

  tableBody.innerHTML = ""

  if (!visibleRows || visibleRows.length === 0) {
    const row = document.createElement("tr")
    row.innerHTML = `<td colspan="9" class="text-center">${emptyStateLabel}</td>`
    tableBody.appendChild(row)
    return
  }

  visibleRows.forEach((opp, index) => {
    const row = document.createElement("tr")
    const asset = String(opp.asset || opp.symbol || '').replace(/USDT$/i, '')
    const spread = Number(opp.spread || 0)
    const netProfit = Number(opp.net_profit_usd || 0)
    const roi = Number(opp.roi_pct || 0)
    const routeKind = String(opp.route_kind || '')
    const routeLabel = (routeKind === 'cex_to_dex' || routeKind === 'kraken_to_kyber')
      ? 'CEX → DEX'
      : ((routeKind === 'dex_to_cex' || routeKind === 'kyber_to_kraken')
        ? 'DEX → CEX'
        : (routeKind === 'dex_inventory_rebalance' ? 'Арбитраж с ребалансом' : 'DEX → Мост → DEX'))
    const bridgeProvider = String(opp.bridge_provider || '')
    const transferStatus = String(opp.transfer_status || 'unknown')
    const transferStatusLabel = transferStatus === 'ok'
      ? 'Перевод доступен'
      : (transferStatus === 'blocked'
        ? 'Перевод заблокирован'
        : (transferStatus === 'bridge_required'
          ? 'Нужен мост'
          : (transferStatus === 'inventory_required' ? 'Нужен инвентарь' : 'Статус неизвестен')))
    const transferBadgeClass = transferStatus === 'ok'
      ? 'bg-success'
      : (transferStatus === 'blocked'
        ? 'bg-danger'
        : (transferStatus === 'bridge_required'
          ? 'bg-warning text-dark'
          : (transferStatus === 'inventory_required' ? 'bg-info text-dark' : 'bg-secondary')))
    const contract = String(opp.contract || opp.buy_contract || opp.sell_contract || '')
    const isBuyCexOrKrakenKyber = opp.buy_type === 'cex' || (opp.buy_exchange && (opp.buy_exchange.toLowerCase().includes('kraken') || opp.buy_exchange.toLowerCase().includes('kyber')))
    const buyLink = isBuyCexOrKrakenKyber
      ? getExchangeUrl(opp.buy_exchange, asset, contract)
      : (opp.buy_url || getExchangeUrl(opp.buy_exchange, asset, contract))
    const isSellCexOrKrakenKyber = opp.sell_type === 'cex' || (opp.sell_exchange && (opp.sell_exchange.toLowerCase().includes('kraken') || opp.sell_exchange.toLowerCase().includes('kyber')))
    const sellLink = isSellCexOrKrakenKyber
      ? getExchangeUrl(opp.sell_exchange, asset, contract)
      : (opp.sell_url || getExchangeUrl(opp.sell_exchange, asset, contract))
    const contractEscaped = contract.replace(/'/g, "\\'")
    const note = String(opp.notes || '')
    const bridgeDocsUrl = String(opp.bridge_docs_url || '')
    const executionQuality = String(opp.execution_quality || '')
    const executionQualityLabel = executionQuality === 'live'
      ? '🟢 Живой'
      : (executionQuality === 'hybrid'
        ? '🟡 Гибрид'
        : (executionQuality === 'estimated'
          ? '🔵 Оценка'
          : (executionQuality === 'actionable' ? '✅ Исполнимо' : executionQuality)))
    const quoteSources = Array.isArray(opp.quote_sources) ? opp.quote_sources.filter(Boolean).join(' + ') : ''
    const buyChain = String(opp.buy_chain || opp.chain || '').trim()
    const sellChain = String(opp.sell_chain || opp.chain || '').trim()
    const chainPairHtml = (buyChain && sellChain && buyChain !== sellChain)
      ? `<div><span class="badge bg-dark border border-secondary">${buyChain}</span> → <span class="badge bg-dark border border-secondary">${sellChain}</span></div>`
      : (buyChain || sellChain)
        ? `<div><span class="badge bg-dark border border-secondary">${buyChain || sellChain}</span></div>`
        : '<div class="text-muted">—</div>'
    const liquidityUsd = (typeof opp.liquidity_usd === 'number' && isFinite(opp.liquidity_usd)) ? opp.liquidity_usd : null
    const liquidityHtml = liquidityUsd !== null
      ? `<div class="small text-muted">ликв. ${formatVolume(liquidityUsd)}</div>`
      : ''

    // Buy/sell cell: party + price stacked, with CEX-vs-DEX badge.
    const buyTypeLabel = (opp.buy_type === 'dex') ? 'DEX' : 'CEX'
    const sellTypeLabel = (opp.sell_type === 'dex') ? 'DEX' : 'CEX'
    const buyBadgeCls = buyTypeLabel === 'DEX' ? 'bg-warning text-dark' : 'bg-success'
    const sellBadgeCls = sellTypeLabel === 'DEX' ? 'bg-warning text-dark' : 'bg-success'
    const buyLabel = String(opp.buy_exchange || opp.buy_dex || '—')
    const sellLabel = String(opp.sell_exchange || opp.sell_dex || '—')
    const manualOnly = !!opp.manual_only || String(opp.execution_mode || '').toLowerCase().includes('manual') || /binance alpha/i.test(buyLabel) || /binance alpha/i.test(sellLabel)
    const manualBadge = manualOnly ? '<span class="badge bg-warning text-dark ms-1" style="font-size:0.6rem;">manual</span>' : '' 
    const buyCellHtml = `
      <div class="d-flex align-items-center gap-1">
        <span class="badge ${buyBadgeCls} bg-opacity-75" style="font-size:0.65rem;">${buyTypeLabel}</span>
        <span class="fw-semibold">${buyLabel}</span>${manualOnly && /binance alpha/i.test(buyLabel) ? manualBadge : ''}
      </div>
      <div class="small text-muted">${formatQuotePrice(Number(opp.buy_price || 0))}</div>`
    const sellCellHtml = `
      <div class="d-flex align-items-center gap-1">
        <span class="badge ${sellBadgeCls} bg-opacity-75" style="font-size:0.65rem;">${sellTypeLabel}</span>
        <span class="fw-semibold">${sellLabel}</span>${manualOnly && /binance alpha/i.test(sellLabel) ? manualBadge : ''}
      </div>
      <div class="small text-muted">${formatQuotePrice(Number(opp.sell_price || 0))}</div>`

    // Route cell (column 5): different label depending on scanner mode.
    const routeCellHtml = isCexDex
      ? `
        <div class="fw-semibold">${routeLabel}</div>
        ${executionQualityLabel ? `<div class="small text-muted">${executionQualityLabel}</div>` : ''}
        <span class="badge ${transferBadgeClass}" style="font-size:0.7rem;">${transferStatusLabel}</span>`
      : `
        <div class="fw-semibold">${bridgeProvider || routeLabel}</div>
        ${executionQualityLabel ? `<div class="small text-muted">${executionQualityLabel}</div>` : ''}
        ${quoteSources ? `<div class="small text-muted">${quoteSources}</div>` : ''}
        <span class="badge ${transferBadgeClass}" style="font-size:0.7rem;">${transferStatusLabel}</span>`

    // Net profit + ROI combined cell (column 7).
    const netProfitCls = netProfit > 0 ? 'text-success fw-semibold' : 'text-muted'
    const roiCls = roi > 0 ? 'text-success' : 'text-muted'
    const netProfitHtml = `
      <div class="${netProfitCls}">$${netProfit.toFixed(2)}</div>
      <div class="small ${roiCls}">${roi.toFixed(2)}%</div>`

    // Spread cell (column 6)
    const spreadCls = spread >= 5 ? 'fw-bold text-accent' : (spread >= 2 ? 'fw-semibold text-warning' : '')
    const spreadHtml = `<span class="${spreadCls}">${spread.toFixed(2)}%</span>`

    row.className = spread >= 5 ? "opportunity-row high-spread" : "opportunity-row"
    row.innerHTML = `
      <td>${index + 1}</td>
      <td title="${note}">
        <div class="fw-semibold">${opp.symbol || asset}</div>
        ${opp.symbol && opp.symbol !== asset ? `<div class="small text-muted">${asset}</div>` : ''}
      </td>
      <td>${buyCellHtml}</td>
      <td>${sellCellHtml}</td>
      <td>${routeCellHtml}</td>
      <td>${spreadHtml}</td>
      <td>${netProfitHtml}</td>
      <td>${chainPairHtml}${liquidityHtml}</td>
      <td class="text-end" style="min-width: 140px;">
        <div class="d-flex justify-content-end gap-1 flex-wrap">
          <button class="btn-ctrl info" onclick="showCoinInfo('${(opp.symbol || asset).replace(/'/g, "\\'")}', '${String(opp.buy_exchange || '').replace(/'/g, "\\'")}', ${Number(opp.buy_price) || 0}, '${String(opp.sell_exchange || '').replace(/'/g, "\\'")}', ${Number(opp.sell_price) || 0}, ${spread}, 0, 0)" title="Информация о монете"><i class="fas fa-info-circle fa-lg"></i></button>
          ${buyLink ? `<a class="btn-ctrl success" href="${buyLink}" target="_blank" rel="noopener noreferrer" title="Открыть точку входа (${buyTypeLabel})"><i class="fas fa-arrow-right-to-bracket fa-lg"></i></a>` : ''}
          ${sellLink ? `<a class="btn-ctrl danger" href="${sellLink}" target="_blank" rel="noopener noreferrer" title="Открыть точку выхода (${sellTypeLabel})"><i class="fas fa-arrow-up-right-from-square fa-lg"></i></a>` : ''}
          ${bridgeDocsUrl ? `<a class="btn-ctrl warning" href="${bridgeDocsUrl}" target="_blank" rel="noopener noreferrer" title="Документация моста"><i class="fas fa-link fa-lg"></i></a>` : ''}
          ${contract ? `<button class="btn-ctrl secondary" onclick="copyToClipboard('${contractEscaped}')" title="Копировать контракт"><i class="fas fa-copy fa-lg"></i></button>` : ''}
          <button class="btn-ctrl secondary" onclick="showInterchainDebug('${asset}')" title="Отладка маршрута"><i class="fas fa-bug fa-lg"></i></button>
        </div>
      </td>
    `
    tableBody.appendChild(row)
  })

  // Prefetch top-7 interchain/cex-dex opportunities for faster modal opens.
  try {
    if (typeof prefetchTopAssets === 'function') {
      prefetchTopAssets((visibleRows || []).slice(0, 7))
    }
  } catch (e) {
    console.warn('[prefetch] updateInterchainTable prefetch failed:', e)
  }
}

// Функция для обновления таблицы возможностей
function updateOpportunitiesTable() {
  console.log("Обновление таблицы возможностей...")
  const tableBody = document.getElementById("opportunitiesTableBody")

  if (!tableBody) {
    console.error("Элемент таблицы не найден")
    return
  }

  // Очищаем таблицу
  try { cleanupFloatingOverlays() } catch (_) { }

  tableBody.innerHTML = ""

  // Если нет данных, показываем сообщение
  if (!opportunities || opportunities.length === 0) {
    const row = document.createElement("tr")
    row.innerHTML = '<td colspan="10" class="text-center">Нет данных</td>'
    tableBody.appendChild(row)
    return
  }

  // Группировка по символу: одна строка на монету + скрытые маршруты
  const shouldGroupBySymbol = !!(settings && settings.ui_group_by_symbol)
  let rowsToRender = opportunities
  try {
    if (shouldGroupBySymbol) {
      const bySymbol = new Map()
      for (const o of opportunities) {
        const sym = (o && o.symbol) ? String(o.symbol).toUpperCase() : ''
        if (!sym) continue
        const arr = bySymbol.get(sym)
        if (arr) arr.push(o); else bySymbol.set(sym, [o])
      }
      const grouped = []
      for (const [sym, routes] of bySymbol.entries()) {
        routes.sort((a, b) => (Number(b?.spread) || 0) - (Number(a?.spread) || 0))
        const exSet = new Set()
        for (const r of routes) {
          if (r && r.buy_exchange) exSet.add(String(r.buy_exchange))
          if (r && r.sell_exchange) exSet.add(String(r.sell_exchange))
        }
        grouped.push({
          sym,
          routes,
          header: {
            ...(routes[0] || {}),
            __group_header: true,
            __group_id: sym,
            __group_exchanges: Array.from(exSet),
            __group_routes_count: routes.length,
          },
        })
      }
      grouped.sort((a, b) => (Number(b?.header?.spread) || 0) - (Number(a?.header?.spread) || 0))
      const flat = []
      for (const g of grouped) {
        flat.push(g.header)
        for (const r of (g.routes || []).slice(1)) {
          flat.push({ ...(r || {}), __group_child_of: g.sym })
        }
      }
      rowsToRender = flat
    }
  } catch (e) {
    rowsToRender = opportunities
  }
  let groupCounter = 0

  // Добавляем строки с данными
  rowsToRender.forEach((opp, index) => {
    const row = document.createElement("tr")
    const rowNo = shouldGroupBySymbol
      ? (opp && opp.__group_header ? (++groupCounter) : '')
      : (index + 1)
    // Базовый класс и выделение высокого спреда
    let rowClass = opp.spread >= 5 ? "opportunity-row high-spread" : "opportunity-row"
    row.className = rowClass
    // Data-attributes used by the WS live-quote handler (applyWsQuotesToTable)
    // to locate this row when real-time price updates arrive. Without these
    // the row cannot be matched back to the incoming {symbol, exchange}.
    try {
      if (opp && opp.symbol) row.setAttribute('data-symbol', String(opp.symbol))
      if (opp && opp.buy_exchange) row.setAttribute('data-buy-exchange', String(opp.buy_exchange))
      if (opp && opp.sell_exchange) row.setAttribute('data-sell-exchange', String(opp.sell_exchange))
    } catch (_) { }

    // Mark grouped rows for toggle UI
    try {
      if (opp && opp.__group_header) {
        row.classList.add('group-header')
        row.dataset.groupHeader = String(opp.__group_id || opp.symbol || '')
      }
      if (opp && opp.__group_child_of) {
        row.classList.add('group-child')
        row.dataset.groupChild = String(opp.__group_child_of)
        row.style.display = 'none'
      }
    } catch (_) { }

    const manualOnly = !!opp.manual_only || String(opp.execution_mode || '').toLowerCase().includes('manual') ||
      /binance alpha/i.test(String(opp.buy_exchange || '')) || /binance alpha/i.test(String(opp.sell_exchange || ''))
    if (manualOnly) row.classList.add('manual-signal-row')
    const manualBadge = manualOnly ? '<span class="badge bg-warning text-dark ms-1" title="Ручной источник: API торговли нет, только сигнал">manual</span>' : ''

    // Проверяем и безопасно обрабатываем объемы торгов
    let buyVolume = 'undefined';
    let sellVolume = 'undefined';

    try {
      if (opp.buy_volume !== undefined && opp.buy_volume !== null) {
        const buyVolumeNum = Number(opp.buy_volume);
        if (!isNaN(buyVolumeNum) && isFinite(buyVolumeNum)) {
          buyVolume = buyVolumeNum;
        }
      }

      if (opp.sell_volume !== undefined && opp.sell_volume !== null) {
        const sellVolumeNum = Number(opp.sell_volume);
        if (!isNaN(sellVolumeNum) && isFinite(sellVolumeNum)) {
          sellVolume = sellVolumeNum;
        }
      }
    } catch (e) {
      console.warn("Ошибка при обработке объемов торгов:", e);
    }

    // 5-минутный импульс: предпочитаем его отображение
    const m5 = (typeof opp.momentum_5m_pct === 'number' && isFinite(opp.momentum_5m_pct)) ? opp.momentum_5m_pct : null
    const spikeUp5 = !!opp.spike_up_5m
    const spikeDown5 = !!opp.spike_down_5m
    const arrow5 = spikeUp5 ? '⬆️' : (spikeDown5 ? '⬇️' : (m5 !== null && Math.abs(m5) > 0.5 ? (m5 > 0 ? '↗️' : '↘️') : ''))
    let momentumHtml
    if (m5 !== null) {
      momentumHtml = `${arrow5} ${m5.toFixed(2)}%`
    } else {
      // Фоллбек: краткосрочный импульс по итерациям
      const momentum = (typeof opp.momentum_pct === 'number' && isFinite(opp.momentum_pct)) ? opp.momentum_pct : null
      const spike = !!opp.spike
      const arrow = spike ? '⬆️' : (momentum && momentum > 0.5 ? '↗️' : '')
      momentumHtml = momentum !== null ? `${arrow} ${momentum.toFixed(2)}%` : '—'
    }

    // Мини-метрики: 1м/15м, Δexch, Heat
    const m1 = (typeof opp.momentum_1m_pct === 'number' && isFinite(opp.momentum_1m_pct)) ? opp.momentum_1m_pct : null
    const m15 = (typeof opp.momentum_15m_pct === 'number' && isFinite(opp.momentum_15m_pct)) ? opp.momentum_15m_pct : null
    const disp = (typeof opp.dispersion_pct === 'number' && isFinite(opp.dispersion_pct)) ? opp.dispersion_pct : null
    const heat = (typeof opp.heat_score === 'number' && isFinite(opp.heat_score)) ? opp.heat_score : null
    const vs1 = (typeof opp.volume_surge_1m_pct === 'number' && isFinite(opp.volume_surge_1m_pct)) ? opp.volume_surge_1m_pct : null
    const vs5 = (typeof opp.volume_surge_5m_pct === 'number' && isFinite(opp.volume_surge_5m_pct)) ? opp.volume_surge_5m_pct : null
    const mini = []
    if (m1 !== null) mini.push(`1м ${m1 >= 0 ? '+' : ''}${m1.toFixed(1)}%`)
    if (m15 !== null) mini.push(`15м ${m15 >= 0 ? '+' : ''}${m15.toFixed(1)}%`)
    if (disp !== null) mini.push(`Δexch ${disp.toFixed(1)}%`)
    if (heat !== null) mini.push(`Накал ${heat.toFixed(0)}`)
    // Показываем всплеск объема, если он значимый
    if (vs1 !== null && vs1 >= 50) mini.push(`V1м +${vs1.toFixed(0)}%`)
    if (vs5 !== null && vs5 >= 100) mini.push(`V5м +${vs5.toFixed(0)}%`)
    const miniHtml = mini.length ? `<div class="text-muted small">${mini.join(' · ')}</div>` : ''

    // Подготовим CG-метрики и форматирование
    const cgVol24 = (typeof opp.cg_volume_24h_usd === 'number' && isFinite(opp.cg_volume_24h_usd)) ? opp.cg_volume_24h_usd : null
    const cgMcap = (typeof opp.cg_market_cap_usd === 'number' && isFinite(opp.cg_market_cap_usd)) ? opp.cg_market_cap_usd : null
    // Направление с цветовой индикацией и порогом уверенности
    const dir = typeof opp.direction === 'string' ? opp.direction : null
    const dirScore = (typeof opp.direction_score === 'number' && isFinite(opp.direction_score)) ? opp.direction_score : null
    const dirConf = (typeof opp.direction_conf === 'number' && isFinite(opp.direction_conf)) ? opp.direction_conf : null
    const dirCfg = (settings && settings.direction) ? settings.direction : {}
    const confMinToShow = (typeof dirCfg.conf_min_to_show === 'number') ? dirCfg.conf_min_to_show : 0.4
    let dirCellHtml = '—'
    if (dir && dirScore !== null && dirConf !== null && dirConf >= confMinToShow) {
      const absScore = Math.abs(dirScore)
      const ths = Array.isArray(dirCfg.strength_thresholds) ? dirCfg.strength_thresholds : [0.33, 0.66]
      const colors = Array.isArray(dirCfg.colors) ? dirCfg.colors : ['secondary', 'warning', 'success']
      let level = 0
      if (absScore >= (ths[1] ?? 0.66)) level = 2
      else if (absScore >= (ths[0] ?? 0.33)) level = 1
      else level = 0
      const color = colors[Math.min(level, colors.length - 1)] || 'secondary'
      let arrow = '→'
      if (dir === 'up') arrow = '⬆️'
      else if (dir === 'down') arrow = '⬇️'
      const strengthPct = Math.round(absScore * 100)
      const confPct = Math.round(dirConf * 100)
      dirCellHtml = `<span class="badge bg-${color}${color === 'warning' ? ' text-dark' : ''}" title="Короткое направление (1–5м)">${arrow} ${strengthPct}% · conf ${confPct}%</span>`
    }

    // Ликвидность: определяем тьер и бейдж
    const liq = getLiquidityTier(opp)
    const obLiq = getOrderbookLiquidityBadge(opp)
    const showLiquidityInfo = !!settings.ui_group_by_liquidity
    const badgeClass = liq.code === 'high' ? 'bg-success' : (liq.code === 'mid' ? 'bg-secondary' : (liq.code === 'low' ? 'bg-warning text-dark' : 'bg-light text-dark'))
    const obBadge = `<span class="badge ${obLiq.className}" title="${obLiq.tooltip}">${obLiq.label}</span>`
    const { base: cgBase } = splitNormalizedPairSymbol(opp.symbol)
    const cgSearch = `https://www.coingecko.com/en/search?query=${encodeURIComponent(cgBase || opp.symbol)}`
    const warnCount = getWarnMarkCount(opp.symbol)
    const warnCls = warnCount >= 3 ? 'bg-danger' : 'bg-warning text-dark'
    const warnBadge = warnCount > 0
      ? ` <span class="badge ${warnCls} warn-mark-badge" title="Черная метка: ${warnCount}/${WARN_MARK_MAX} (Shift+клик: сброс)"><i class="fas fa-triangle-exclamation me-1"></i>${warnCount}</span>`
      : ''
    const liquidityBadgesHtml = showLiquidityInfo
      ? ` ${obBadge} <span class="badge ${badgeClass}" title="${liq.tooltip}">${liq.label}</span>`
      : ''
    let symbolHtml = `<a href="${cgSearch}" target="_blank" rel="noopener noreferrer" class="symbol-link">${opp.symbol}</a>${liquidityBadgesHtml}${warnBadge}`

    // UI: group-by-symbol adds a toggle + exchange list
    try {
      if (opp && opp.__group_header) {
        const gidRaw = String(opp.__group_id || opp.symbol || '')
        const gid = gidRaw.replace(/'/g, '')
        const exList = Array.isArray(opp.__group_exchanges) ? opp.__group_exchanges : []
        const routesCount = (typeof opp.__group_routes_count === 'number') ? opp.__group_routes_count : Number(opp.__group_routes_count || 0) || 0
        const meta = exList.length
          ? `Биржи: ${exList.join(', ')} (${exList.length}) · маршрутов: ${routesCount}`
          : `Маршрутов: ${routesCount}`
        const toggleBtn = `<button type="button" class="btn btn-sm btn-link p-0 me-1 group-toggle" data-group-id="${gid}" onclick="toggleSymbolGroup('${gid}'); return false;" title="Показать/скрыть маршруты">▸</button>`
        symbolHtml = `<div class="d-flex align-items-center gap-1">${toggleBtn}${symbolHtml}</div><div class="small text-muted">${meta}</div>`
      } else if (opp && opp.__group_child_of) {
        symbolHtml = `<span class="text-muted me-1">↳</span>${symbolHtml}`
      }
    } catch (_) { }

    // Тултипы с вариантами цен покупки/продажи (если бэкенд их прислал)
    const buySamples = Array.isArray(opp.buy_price_samples) ? opp.buy_price_samples : []
    const sellSamples = Array.isArray(opp.sell_price_samples) ? opp.sell_price_samples : []
    const fmt = (v) => (typeof v === 'number' && isFinite(v)) ? v.toFixed(8) : String(v)
    const pct = (num) => (typeof num === 'number' && isFinite(num)) ? `${num >= 0 ? '+' : ''}${num.toFixed(2)}%` : ''
    // Локальная настройка: минимальный профит для показа вариантов в popover (%)
    const getPopoverMinProfitPct = () => {
      try {
        const raw = (settings && (typeof settings.ui_popover_min_profit_pct !== 'undefined'))
          ? settings.ui_popover_min_profit_pct
          : 0
        const n = Number.parseFloat(raw)
        return isNaN(n) ? 0 : Math.max(0, n)
      } catch (_) { return 0 }
    }
    // Строим компактный табличный popover
    const buildTooltip = (samples, refPrice, title, otherSidePrice) => {
      try {
        if (!Array.isArray(samples) || samples.length === 0 || typeof refPrice !== 'number' || !isFinite(refPrice) || refPrice <= 0) return ''
        // Дедупликация и сортировка
        const uniq = Array.from(new Set(samples.map(x => Number(x)))).filter(v => isFinite(v) && v > 0)
        uniq.sort((a, b) => a - b)
        const minProf = getPopoverMinProfitPct()
        const rows = []
        for (const v of uniq) {
          const d = (v / refPrice - 1) * 100
          let profit = null
          if (typeof otherSidePrice === 'number' && isFinite(otherSidePrice) && otherSidePrice > 0) {
            if (title.includes('покупки')) {
              profit = (otherSidePrice / v - 1) * 100
            } else {
              profit = (v / otherSidePrice - 1) * 100
            }
          }
          if (profit === null || profit >= minProf) {
            rows.push({ side: title.includes('покупки') ? 'Покупка' : 'Продажа', sideId: title.includes('покупки') ? 'Buy' : 'Sell', price: v, delta: d, profit })
          }
          if (rows.length >= 8) { /* ограничим визуально */ }
        }
        if (!rows.length) return `${title}: <div class="text-muted">Нет вариантов ≥ ${minProf}%</div>`
        const header = `
        <div class="small mb-1 fw-bold">${title}</div>
        <div class="table-responsive">
          <table class="table table-sm table-bordered mb-1" style="font-size: 0.8em; min-width: 260px;">
            <thead class="table-light">
              <tr>
                <th>Сторона</th>
                <th>Цена</th>
                <th>Δ к ref</th>
                <th>Профит vs др. ст.</th>
              </tr>
            </thead>
            <tbody>
      `
        const body = rows.slice(0, 8).map(r => `
        <tr>
          <td class="text-${r.sideId === 'Buy' ? 'success' : 'danger'}">${r.side}</td>
          <td>${fmt(r.price)}</td>
          <td>${pct(r.delta)}</td>
          <td>${r.profit !== null ? pct(r.profit) : '—'}</td>
        </tr>
      `).join('')
        const footer = `
            </tbody>
          </table>
        </div>
      `
        // Лестница целей по прибыли
        const ladder = [1, 3, 5, 10, 20, 50]
        let ladderHtml = ''
        try {
          const base = title.includes('покупки') ? (typeof refPrice === 'number' ? refPrice : NaN) : (typeof otherSidePrice === 'number' ? otherSidePrice : NaN)
          if (isFinite(base) && base > 0) {
            const targets = ladder.map(p => ({ p, price: base * (1 + p / 100) }))
            ladderHtml = `<div class="mt-1 text-muted" style="font-size:0.8em;">Цели: ` + targets.map(t => `${t.p}%→${fmt(t.price)}`).join(' · ') + `</div>`
          }
        } catch (_) { }
        return header + body + footer + ladderHtml
      } catch (e) { return '' }
    }
    const buyTooltip = buildTooltip(buySamples, opp.buy_price, 'Варианты покупки', opp.sell_price)
    const sellTooltip = buildTooltip(sellSamples, opp.sell_price, 'Варианты продажи', opp.buy_price)
    // Быстрый расчёт «лучшего» профита против другой стороны (для мини-колонки)
    let bestProfitVsOther = null
    try {
      const minBuy = buySamples.map(Number).filter(v => isFinite(v) && v > 0).reduce((m, v) => Math.min(m, v), Infinity)
      const maxSell = sellSamples.map(Number).filter(v => isFinite(v) && v > 0).reduce((m, v) => Math.max(m, v), 0)
      const baseSpread = (typeof opp.sell_price === 'number' && typeof opp.buy_price === 'number' && opp.buy_price > 0) ? ((opp.sell_price / opp.buy_price - 1) * 100) : null
      const p1 = (isFinite(minBuy) && isFinite(opp.sell_price) && minBuy > 0) ? ((opp.sell_price / minBuy - 1) * 100) : null
      const p2 = (isFinite(maxSell) && isFinite(opp.buy_price) && opp.buy_price > 0) ? ((maxSell / opp.buy_price - 1) * 100) : null
      bestProfitVsOther = [baseSpread, p1, p2].filter(x => typeof x === 'number' && isFinite(x)).reduce((m, v) => Math.max(m, v), -Infinity)
      if (!isFinite(bestProfitVsOther)) bestProfitVsOther = null
    } catch (_) { bestProfitVsOther = null }

    // ---- Compact rendering (rework 2026-04) ----
    // Goal: reduce 17 columns to 10 focused ones. All secondary metrics
    // (1m/15m momentum, heat, dispersion, trend, cg volume, cg mcap,
    // best-profit samples) are grouped into the "Рынок" and "Объём/Капа"
    // cells. Detailed data is still available via hover popovers.

    // Build the "Где купить/продать" combo cell: exchange (bold) + price (muted).
    const buyPriceStr = (typeof opp.buy_price === 'number' && isFinite(opp.buy_price)) ? opp.buy_price.toFixed(8) : '—'
    const sellPriceStr = (typeof opp.sell_price === 'number' && isFinite(opp.sell_price)) ? opp.sell_price.toFixed(8) : '—'
    const buyPopoverAttrs = buyTooltip ? `data-bs-toggle="popover" data-bs-trigger="hover focus" data-bs-placement="top" data-bs-html="true"` : ''
    const sellPopoverAttrs = sellTooltip ? `data-bs-toggle="popover" data-bs-trigger="hover focus" data-bs-placement="top" data-bs-html="true"` : ''

    // Raw vs net spreads. Backend already computes two useful fields:
    //   opp.spread             — валовой спред по top-1 ценам
    //   opp.net_spread         — спред после торговых комиссий buy+sell (%)
    //   opp.true_net_spread    — спред после торговых КОМИССИЙ + withdraw_fee (%)
    // Prefer true_net_spread when available; it is the most realistic profit.
    const rawSpread = (typeof opp.spread === 'number' && isFinite(opp.spread)) ? opp.spread : null
    const trueNetSpread = (typeof opp.true_net_spread === 'number' && isFinite(opp.true_net_spread)) ? opp.true_net_spread : null
    const legacyNetSpread = (typeof opp.net_spread === 'number' && isFinite(opp.net_spread)) ? opp.net_spread : null
    const netSpread = trueNetSpread !== null ? trueNetSpread : legacyNetSpread
    const netSpreadCls = netSpread === null ? 'text-muted' : (netSpread >= 1.5 ? 'text-success fw-bold' : (netSpread >= 0.5 ? 'text-warning' : (netSpread >= 0 ? 'text-light' : 'text-danger')))
    const rawSpreadHtml = rawSpread !== null ? `${rawSpread.toFixed(2)}%` : '—'
    const netSpreadHtml = netSpread !== null ? `${netSpread >= 0 ? '+' : ''}${netSpread.toFixed(2)}%` : '—'

    // Transfer chain: pick the first chain that is enabled on BOTH exchanges.
    // The backend already gives us:
    //   opp.withdraw_chains    — chains with withdraw enabled on buy_exchange
    //   opp.deposit_chains     — chains with deposit enabled on sell_exchange
    //   opp.withdraw_fee_usd   — fee for the cheapest withdraw chain (already in USD)
    //   opp.transfer_viable    — boolean: at least one chain works on both sides
    const withdrawChains = Array.isArray(opp.withdraw_chains) ? opp.withdraw_chains.map(x => String(x || '').trim()).filter(Boolean) : []
    const depositChains = Array.isArray(opp.deposit_chains) ? opp.deposit_chains.map(x => String(x || '').trim()).filter(Boolean) : []
    const transferViable = !!opp.transfer_viable
    const depositOk = !!opp.deposit_ok
    const withdrawOk = !!opp.withdraw_ok
    const withdrawFeeUsd = (typeof opp.withdraw_fee_usd === 'number' && isFinite(opp.withdraw_fee_usd)) ? opp.withdraw_fee_usd : null
    const bestWithdrawChain = withdrawChains[0] || depositChains[0] || null
    // Short chain label: strip anything in parens (e.g. "Toncoin(TON)" -> "TON").
    const shortChainLabel = (name) => {
      const m = String(name || '').match(/\(([^)]+)\)/)
      return m ? m[1] : String(name || '').trim()
    }
    let chainHtml
    const alphaRoute = isBinanceAlphaExchange(opp.buy_exchange) || isBinanceAlphaExchange(opp.sell_exchange)
    if (alphaRoute) {
      chainHtml = `
        <span class="badge alpha-status-badge" title="Binance Alpha — ручной market-data источник">Alpha manual</span>
        <div class="small text-warning" title="Не красный блокер. Доступность ввода/вывода проверяй в Binance Alpha вручную.">ручной вывод/ввод</div>`
    } else if (bestWithdrawChain && transferViable) {
      const chainBadgeCls = 'bg-success bg-opacity-25 text-success border border-success border-opacity-50'
      chainHtml = `
        <span class="badge ${chainBadgeCls}" title="${bestWithdrawChain}">${shortChainLabel(bestWithdrawChain)}</span>
        ${withdrawFeeUsd !== null ? `<div class="small text-muted">fee ~$${withdrawFeeUsd.toFixed(2)}</div>` : ''}`
    } else if (bestWithdrawChain) {
      const problemReason = !withdrawOk && !depositOk
        ? 'Ввод и вывод заблокированы'
        : (!withdrawOk ? 'Вывод заблокирован на buy-бирже' : (!depositOk ? 'Ввод заблокирован на sell-бирже' : 'Сети не совпадают'))
      chainHtml = `
        <span class="badge bg-danger bg-opacity-25 text-danger border border-danger border-opacity-50" title="${problemReason}">${shortChainLabel(bestWithdrawChain)} ⚠</span>
        <div class="small text-danger" title="${problemReason}">${problemReason}</div>`
    } else {
      chainHtml = '<span class="text-muted small">нет сетей</span>'
    }

    // Market cell: consolidate 5m momentum + trend badge in one compact cell.
    // Secondary metrics (1m/15m/heat/disp) go into hover popover via `data-market-details`.
    const marketDetails = []
    if (m1 !== null) marketDetails.push(`<div>1м: <b>${m1 >= 0 ? '+' : ''}${m1.toFixed(2)}%</b></div>`)
    if (m15 !== null) marketDetails.push(`<div>15м: <b>${m15 >= 0 ? '+' : ''}${m15.toFixed(2)}%</b></div>`)
    if (disp !== null) marketDetails.push(`<div>Δ бирж: <b>${disp.toFixed(2)}%</b></div>`)
    if (heat !== null) marketDetails.push(`<div>Накал: <b>${Math.round(heat)}/100</b></div>`)
    if (vs1 !== null && vs1 >= 50) marketDetails.push(`<div>V1м: +${vs1.toFixed(0)}%</div>`)
    if (vs5 !== null && vs5 >= 100) marketDetails.push(`<div>V5м: +${vs5.toFixed(0)}%</div>`)
    if (typeof bestProfitVsOther === 'number' && isFinite(bestProfitVsOther)) {
      marketDetails.push(`<div class="mt-1 pt-1 border-top border-secondary">Лучший профит: <b>${bestProfitVsOther >= 0 ? '+' : ''}${bestProfitVsOther.toFixed(2)}%</b></div>`)
    }
    const marketPopoverContent = marketDetails.length ? marketDetails.join('') : ''
    const marketCellAttrs = marketPopoverContent ? `data-bs-toggle="popover" data-bs-trigger="hover focus" data-bs-placement="top" data-bs-html="true"` : ''
    const marketCellHtml = `
      <div class="d-flex flex-column gap-1">
        <div>${momentumHtml}</div>
        ${dirCellHtml !== '—' ? `<div>${dirCellHtml}</div>` : ''}
      </div>`

    // Volume/Mcap cell: two short numbers stacked.
    const volMcapHtml = `
      <div class="d-flex flex-column" style="line-height:1.1;">
        <span title="Объём торгов за 24ч (CoinGecko)">${cgVol24 !== null ? formatNumberShort(cgVol24) : '—'}</span>
        <small class="text-muted" title="Рыночная капитализация (CoinGecko)">${cgMcap !== null ? formatNumberShort(cgMcap) : '—'}</small>
      </div>`

    row.innerHTML = `
            <td>${rowNo}</td>
            <td>${symbolHtml}</td>
            <td class="buy-cell" ${buyPopoverAttrs}>
              <div class="fw-semibold">${opp.buy_exchange}${manualOnly && /binance alpha/i.test(String(opp.buy_exchange || '')) ? manualBadge : ''}</div>
              <div class="small text-muted">${buyPriceStr}</div>
            </td>
            <td class="sell-cell" ${sellPopoverAttrs}>
              <div class="fw-semibold">${opp.sell_exchange}${manualOnly && /binance alpha/i.test(String(opp.sell_exchange || '')) ? manualBadge : ''}</div>
              <div class="small text-muted">${sellPriceStr}</div>
            </td>
            <td class="fw-bold text-accent" title="Валовой спред до комиссий">${rawSpreadHtml}</td>
            <td class="${netSpreadCls}" title="Чистый спред после торговых комиссий buy+sell">${netSpreadHtml}</td>
            <td>${chainHtml}</td>
            <td ${marketCellAttrs}>${marketCellHtml}</td>
            <td>${volMcapHtml}</td>
            <td class="text-end" style="min-width: 140px;">
                <div class="d-flex justify-content-end gap-2">
                    <button class="btn-ctrl secondary" onclick="showCoinInfo('${opp.symbol}', '${opp.buy_exchange}', ${opp.buy_price}, '${opp.sell_exchange}', ${opp.sell_price}, ${opp.spread}, ${buyVolume || 0}, ${sellVolume || 0})" title="Инфо"><i class="fas fa-info-circle fa-lg"></i></button>
                    <button class="btn-ctrl warning" onclick="addToBlacklistFromTable('${opp.symbol}')" title="Блок"><i class="fas fa-ban fa-lg"></i></button>
                    <button class="btn-ctrl danger ${isFavorite(opp.symbol) ? 'active' : ''}" onclick="toggleFavorite('${opp.symbol}')" title="${isFavorite(opp.symbol) ? 'Удалить из избранного' : 'Добавить в избранное'}"><i class="fas ${isFavorite(opp.symbol) ? 'fa-heart' : 'fa-heart-text'} fa-lg"></i></button>
                </div>
            </td>
        `
    tableBody.appendChild(row)
    // Сохраняем контент popover в dataset, чтобы избежать HTML в атрибутах
    try {
      const cells = row.querySelectorAll('td')
      // Column indices in the compact layout:
      //   0: #     1: Монета  2: Buy   3: Sell   4: Spread%
      //   5: Net%  6: Chain   7: Market 8: Vol/Mcap  9: Actions
      const buyCell = cells[2]
      const sellCell = cells[3]
      const marketCell = cells[7]
      if (buyCell && buyTooltip) buyCell.dataset.popoverContent = buyTooltip
      if (sellCell && sellTooltip) sellCell.dataset.popoverContent = sellTooltip
      if (marketCell && marketPopoverContent) marketCell.dataset.popoverContent = marketPopoverContent
    } catch (_) { }
  })
  // Активируем Bootstrap Popovers для ячеек с вариантами цен
  try {
    if (typeof bootstrap !== 'undefined') {
      const nodes = document.querySelectorAll('[data-bs-toggle="popover"]')
      nodes.forEach(el => {
        try {
          // Удалим предыдущий поповер, если был
          const old = bootstrap.Popover.getInstance(el)
          if (old) old.dispose()
          new bootstrap.Popover(el, {
            container: 'body',
            trigger: 'hover focus',
            placement: 'top',
            html: true,
            sanitize: false,
            delay: { show: 150, hide: 80 },
            content: () => {
              try {
                // Контент берём из dataset (предпочтительно)
                return el.dataset.popoverContent || el.getAttribute('data-bs-content') || el.getAttribute('title') || ''
              } catch (_) { return '' }
            }
          })
          // Скрывать на mouseleave поэлементно
          el.addEventListener('mouseleave', () => {
            const inst = bootstrap.Popover.getInstance(el)
            if (inst) inst.hide()
          })
        } catch (e) { }
      })
    }
  } catch (e) { /* noop */ }

  // Предзагрузка топ-N монет в фоне через batch endpoint — чтобы модалка
  // «Инфо» открывалась моментально из клиентского кэша.
  try {
    const topRows = (opportunities || []).slice(0, 7)
    prefetchTopAssets(topRows)
  } catch (e) {
    console.warn('[prefetch] updateOpportunitiesTable prefetch failed:', e)
  }
}

// ---- Client-side asset_status cache + batch prefetch (rework 2026-04) ----
// When the main table renders, we want to pre-warm /api/asset_status for the
// top-N visible rows so that clicking "Инфо" opens the modal instantly
// instead of waiting 3-5 seconds for deposit/withdraw statuses.
//
// Deposit/withdraw statuses change rarely (usually on maintenance windows).
// TTL strategy (stale-while-revalidate):
//   - FRESH_MS   = 2 minutes  → serve from cache, do nothing
//   - TTL_MS     = 5 minutes  → serve from cache AND kick off bg refresh
//   - > TTL_MS              → don't use cache, do foreground request
// This mirrors HTTP Cache-Control: max-age=120, stale-while-revalidate=180.
if (typeof window.__assetStatusClientCache === 'undefined') {
  window.__assetStatusClientCache = {}
}
const ASSET_STATUS_CLIENT_TTL_MS = 5 * 60 * 1000
const ASSET_STATUS_CLIENT_FRESH_MS = 2 * 60 * 1000

function getClientAssetStatus(symbol, options = {}) {
  // Returns the cached entry if still within TTL. When the cache is older
  // than FRESH_MS but within TTL_MS, we additionally schedule a background
  // refresh (stale-while-revalidate). `options.noRevalidate` skips bg fetch.
  try {
    const key = String(symbol || '').toUpperCase()
    const entry = window.__assetStatusClientCache[key]
    if (!entry) return null
    const age = Date.now() - entry.ts
    if (age > ASSET_STATUS_CLIENT_TTL_MS) return null
    if (!options.noRevalidate && age > ASSET_STATUS_CLIENT_FRESH_MS) {
      // Schedule a silent background refresh so the next open is fresh.
      try { scheduleClientAssetRevalidate(key) } catch (_) { }
    }
    return entry.data
  } catch (_) { return null }
}

function setClientAssetStatus(symbol, data) {
  try {
    const key = String(symbol || '').toUpperCase()
    window.__assetStatusClientCache[key] = { ts: Date.now(), data }
  } catch (_) { }
}

// Dedup background refresh: at most one in-flight revalidate per symbol.
if (typeof window.__assetStatusRevalidating === 'undefined') {
  window.__assetStatusRevalidating = new Set()
}

function scheduleClientAssetRevalidate(key) {
  try {
    if (!key) return
    if (window.__assetStatusRevalidating.has(key)) return
    window.__assetStatusRevalidating.add(key)
    const url = `/api/asset_status_batch?assets=${encodeURIComponent(key)}&overall_timeout=6`
    // Fire-and-forget: we don't want to block the UI waiting for this.
    fetch(url, { signal: AbortSignal.timeout(10000) })
      .then(r => r.ok ? r.json() : null)
      .then(p => {
        if (p && p.success && p.data && Array.isArray(p.data[key])) {
          setClientAssetStatus(key, { success: true, data: p.data[key] })
          console.debug(`[revalidate] refreshed ${key} in background`)
        }
      })
      .catch(() => { /* silent */ })
      .finally(() => {
        try { window.__assetStatusRevalidating.delete(key) } catch (_) { }
      })
  } catch (_) {
    try { window.__assetStatusRevalidating.delete(key) } catch (_) { }
  }
}

// Deduplicate concurrent prefetch requests for the same symbol set within 2 seconds.
let __prefetchLastKey = ''
let __prefetchLastTs = 0

async function prefetchTopAssets(rows) {
  try {
    const items = Array.isArray(rows) ? rows : []
    if (!items.length) return

    // Extract up to 7 unique base assets (stripping USDT suffix).
    const symbols = []
    const seen = new Set()
    for (const opp of items) {
      if (!opp) continue
      let sym = String(opp.symbol || opp.asset || '').toUpperCase()
      // Strip quote (USDT/USD/USDC) suffix so asset_status resolves correctly.
      sym = sym.replace(/(USDT|USDC|USD|BUSD|DAI|FDUSD)$/i, '').trim()
      if (!sym || seen.has(sym)) continue
      symbols.push(sym)
      seen.add(sym)
      if (symbols.length >= 7) break
    }
    if (!symbols.length) return

    // Skip if we already have fresh cache for all symbols.
    const needFetch = symbols.filter(s => !getClientAssetStatus(s))
    if (!needFetch.length) return

    // Deduplicate rapid-fire prefetches of the same set.
    const cacheKey = needFetch.sort().join('|')
    const now = Date.now()
    if (cacheKey === __prefetchLastKey && (now - __prefetchLastTs) < 2000) return
    __prefetchLastKey = cacheKey
    __prefetchLastTs = now

    console.log(`[prefetch] requesting batch for ${needFetch.length} assets:`, needFetch)
    const url = `/api/asset_status_batch?assets=${encodeURIComponent(needFetch.join(','))}&include_contracts=1&overall_timeout=6`
    const resp = await fetch(url, { signal: AbortSignal.timeout(12000) })
    if (!resp.ok) {
      console.warn(`[prefetch] HTTP ${resp.status} for batch`)
      return
    }
    const payload = await resp.json()
    if (!payload || !payload.success || !payload.data) {
      console.warn('[prefetch] batch failed:', payload && payload.error)
      return
    }
    for (const [sym, data] of Object.entries(payload.data)) {
      setClientAssetStatus(sym, { success: true, data: Array.isArray(data) ? data : [] })
    }
    console.log(`[prefetch] cached ${Object.keys(payload.data).length} assets (hits=${payload.cache_hits}, misses=${payload.cache_misses})`)
  } catch (e) {
    // Silent fail — prefetch is best-effort. Real modal request will fall back to normal path.
    if (e && e.name === 'AbortError') {
      console.warn('[prefetch] batch timed out — modal will fall back to single requests')
    } else {
      console.warn('[prefetch] batch error:', e)
    }
  }
}

// Best-effort cleanup for Bootstrap floating UI (popover/tooltip) which may get "stuck"
// when the underlying trigger element is removed/replaced during polling updates.
function cleanupFloatingOverlays() {
  try {
    if (typeof bootstrap !== 'undefined') {
      try {
        document.querySelectorAll('[data-bs-toggle="popover"]').forEach(el => {
          const inst = (bootstrap.Popover && bootstrap.Popover.getInstance) ? bootstrap.Popover.getInstance(el) : null
          if (inst) {
            try { inst.hide() } catch (_) { }
            try { inst.dispose() } catch (_) { }
          }
        })
      } catch (_) { }
      try {
        document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el => {
          const inst = (bootstrap.Tooltip && bootstrap.Tooltip.getInstance) ? bootstrap.Tooltip.getInstance(el) : null
          if (inst) {
            try { inst.hide() } catch (_) { }
            try { inst.dispose() } catch (_) { }
          }
        })
      } catch (_) { }
    }
  } catch (_) { }
  try {
    document.querySelectorAll('.popover, .tooltip').forEach(n => n.remove())
  } catch (_) { }
}

// Toggle visibility for grouped "routes" rows for a symbol.
function toggleSymbolGroup(groupId) {
  try {
    const gid = String(groupId || '').replace(/"/g, '')
    if (!gid) return
    const rows = document.querySelectorAll(`#opportunitiesTableBody tr[data-group-child="${gid}"]`)
    if (!rows || rows.length === 0) return
    const isHidden = rows[0].style.display === 'none'
    rows.forEach(r => { r.style.display = isHidden ? '' : 'none' })
    const btn = document.querySelector(`#opportunitiesTableBody button.group-toggle[data-group-id="${gid}"]`)
    if (btn) btn.textContent = isHidden ? '▾' : '▸'
  } catch (_) { }
}

// Применение видимости колонок из настроек
function persistSettingsPatch(patch, onSuccess = null, onFailure = null) {
  return fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch || {}),
  })
    .then((response) => response.json())
    .then((data) => {
      if (data && data.success === false) throw new Error(data.error || 'settings save failed')
      settings = { ...settings, ...(patch || {}) }
      if (typeof onSuccess === 'function') onSuccess(data)
      return data
    })
    .catch((error) => {
      console.error('Ошибка при сохранении части настроек:', error)
      if (typeof onFailure === 'function') onFailure(error)
      return null
    })
}

function renderCurrentScannerTable() {
  try {
    if (scannerMode === 'cex') updateOpportunitiesTable()
    else if (scannerMode === 'kraken_kyber') updateKrakenKyberTable()
    else updateInterchainTable()
  } catch (_) { }
}

function syncSettingsModalState() {
  const filterTransferCb = document.getElementById('filterTransferCheckbox')
  const filterTransferStrictCb = document.getElementById('filterTransferStrictCheckbox')
  const useOrderbooksCb = document.getElementById('useOrderbooksCheckbox')
  const orderbooksRow = document.getElementById('orderbooksSettingsRow')

  if (filterTransferStrictCb) {
    const strictEnabled = !!(filterTransferCb && filterTransferCb.checked)
    filterTransferStrictCb.disabled = !strictEnabled
    const strictWrap = filterTransferStrictCb.closest('.form-check')
    if (strictWrap) strictWrap.style.opacity = strictEnabled ? '1' : '0.55'
  }
  if (orderbooksRow) {
    orderbooksRow.style.display = (useOrderbooksCb && useOrderbooksCb.checked) ? '' : 'none'
  }
  const stalePenaltyCheckbox = document.getElementById('stalePenaltyCheckbox')
  const stalePenaltySettingsRow = document.getElementById('stalePenaltySettingsRow')
  if (stalePenaltySettingsRow) {
    stalePenaltySettingsRow.style.opacity = (stalePenaltyCheckbox && stalePenaltyCheckbox.checked) ? '1' : '0.55'
  }
}

function applyColumnVisibility() {
  const show1m = !!settings.ui_show_momentum_1m
  const show15m = !!settings.ui_show_momentum_15m
  const showHeat = !!settings.ui_show_heat
  const showDisp = !!settings.ui_show_dispersion
  const showCgVol = !!settings.ui_show_cg_vol24
  const showCgMcap = !!settings.ui_show_cg_mcap
  const showDir = !!settings.ui_show_direction
  const showLiq = !!settings.ui_group_by_liquidity
  // Управляем видимостью по data-sort, чтобы не зависеть от индексов
  const setColBySort = (sortName, visible) => {
    const th = document.querySelector(`table thead th[data-sort="${sortName}"]`)
    const thIndex = th ? Array.from(th.parentElement.children).indexOf(th) : -1
    if (th) th.style.display = visible ? '' : 'none'
    if (thIndex >= 0) {
      document.querySelectorAll('#opportunitiesTableBody tr').forEach(r => {
        const cells = r.querySelectorAll('td')
        if (cells[thIndex]) cells[thIndex].style.display = visible ? '' : 'none'
      })
    }
  }
  setColBySort('momentum1', show1m)
  setColBySort('momentum15', show15m)
  setColBySort('heat', showHeat)
  setColBySort('dispersion', showDisp)
  setColBySort('cgvol24', showCgVol)
  setColBySort('cgmcap', showCgMcap)
  setColBySort('direction', showDir)

  // Чекбоксы в настройках
  const c1 = document.getElementById('showMomentum1mCheckbox')
  const c15 = document.getElementById('showMomentum15mCheckbox')
  const ch = document.getElementById('showHeatCheckbox')
  const cd = document.getElementById('showDispersionCheckbox')
  const cgVol = document.getElementById('showCgVolCheckbox')
  const cgMcap = document.getElementById('showCgMcapCheckbox')
  const dirCb = document.getElementById('showDirectionCheckbox')
  const groupByLiq = document.getElementById('groupByLiquidityCheckbox')
  if (c1) c1.checked = show1m
  if (c15) c15.checked = show15m
  if (ch) ch.checked = showHeat
  if (cd) cd.checked = showDisp
  if (cgVol) cgVol.checked = showCgVol
  if (cgMcap) cgMcap.checked = showCgMcap
  if (dirCb) dirCb.checked = showDir
  if (groupByLiq) groupByLiq.checked = showLiq
  syncSettingsModalState()
}

// Функция для загрузки настроек
function loadSettings() {
  console.log("Загрузка настроек...")
  fetch("/api/settings")
    .then((response) => response.json())
    .then((data) => {
      if (data.success === false) {
        console.error("Ошибка при загрузке настроек:", data.error)
        return
      }

      settings = data

      // Обновляем поля ввода
      const minSpreadSettingInput = document.getElementById("minSpreadSettingInput")
      const maxSpreadInput = document.getElementById("maxSpreadInput")
      const uiPollingIntervalInput = document.getElementById("uiPollingIntervalInput")
      const useSeparateCheckbox = document.getElementById('useSeparatePollingCheckbox')
      const separateContainer = document.getElementById('separatePollingContainer')
      const uiPollingIntervalStatusInput = document.getElementById('uiPollingIntervalStatusInput')
      const uiPollingIntervalOppsInput = document.getElementById('uiPollingIntervalOppsInput')
      const filterLiquidityCb = document.getElementById('filterLiquidityCheckbox')
      const minNotionalInput = document.getElementById('minNotionalInput')
      const topLiquidityInput = document.getElementById('topLiquidityInput')
      const krakenKyberEnabledInput = document.getElementById('krakenKyberEnabledInput')
      const krakenKyberMinSpreadInput = document.getElementById('krakenKyberMinSpreadInput')
      const krakenKyberNotionalInput = document.getElementById('krakenKyberNotionalInput')
      const krakenKyberAssetLimitInput = document.getElementById('krakenKyberAssetLimitInput')

      if (minSpreadSettingInput) minSpreadSettingInput.value = settings.min_spread
      if (maxSpreadInput) maxSpreadInput.value = settings.max_spread
      if (uiPollingIntervalInput) uiPollingIntervalInput.value = (typeof settings.ui_polling_interval_sec === 'number') ? settings.ui_polling_interval_sec : 7
      if (useSeparateCheckbox) useSeparateCheckbox.checked = !!settings.ui_use_separate_polling
      const showSeparate = !!settings.ui_use_separate_polling
      if (separateContainer) separateContainer.style.display = showSeparate ? 'block' : 'none'
      if (uiPollingIntervalStatusInput) uiPollingIntervalStatusInput.value = (typeof settings.ui_polling_interval_status_sec === 'number') ? settings.ui_polling_interval_status_sec : ''
      if (uiPollingIntervalOppsInput) uiPollingIntervalOppsInput.value = (typeof settings.ui_polling_interval_opportunities_sec === 'number') ? settings.ui_polling_interval_opportunities_sec : ''
      if (filterLiquidityCb) filterLiquidityCb.checked = !!settings.ui_arb_filter_liquidity
      if (minNotionalInput) minNotionalInput.value = (typeof settings.arb_min_notional_usd === 'number') ? settings.arb_min_notional_usd : 300
      if (topLiquidityInput) topLiquidityInput.value = (typeof settings.ui_arb_top_liquidity_n === 'number') ? settings.ui_arb_top_liquidity_n : 0
      if (krakenKyberEnabledInput) krakenKyberEnabledInput.checked = settings.kraken_kyber_enabled !== false
      if (krakenKyberMinSpreadInput) krakenKyberMinSpreadInput.value = (typeof settings.kraken_kyber_min_spread === 'number') ? settings.kraken_kyber_min_spread : 0.5
      if (krakenKyberNotionalInput) krakenKyberNotionalInput.value = (typeof settings.kraken_kyber_notional_usd === 'number') ? settings.kraken_kyber_notional_usd : 250
      if (krakenKyberAssetLimitInput) krakenKyberAssetLimitInput.value = 0
      if (useSeparateCheckbox && separateContainer) {
        useSeparateCheckbox.addEventListener('change', () => {
          separateContainer.style.display = useSeparateCheckbox.checked ? 'block' : 'none'
        })
      }
      // UI: мин. профит для popover (localStorage)
      try {
        const popInp = document.getElementById('popoverMinProfitInput')
        if (popInp) {
          popInp.value = (typeof settings.ui_popover_min_profit_pct === 'number') ? settings.ui_popover_min_profit_pct : 0
          popInp.addEventListener('change', () => {
            // Не сохраняем тут (сохранение — через кнопку "Сохранить")
            try { updateOpportunitiesTable() } catch (_) { }
          })
        }
      } catch (e) { /* noop */ }

      // Обновляем список бирж
      updateExchangesList(settings.available_exchanges, settings.enabled_exchanges)

      // Чекбоксы отображения колонок
      const c1 = document.getElementById('showMomentum1mCheckbox')
      const c15 = document.getElementById('showMomentum15mCheckbox')
      const ch = document.getElementById('showHeatCheckbox')
      const cd = document.getElementById('showDispersionCheckbox')
      const cgVol = document.getElementById('showCgVolCheckbox')
      const cgMcap = document.getElementById('showCgMcapCheckbox')
      const dirCb = document.getElementById('showDirectionCheckbox')
      const groupByLiq = document.getElementById('groupByLiquidityCheckbox')
      const groupBySymbol = document.getElementById('groupBySymbolCheckbox')
      const filterTransferCb = document.getElementById('filterTransferCheckbox')
      const filterTransferStrictCb = document.getElementById('filterTransferStrictCheckbox')
      const useOrderbooksCb = document.getElementById('useOrderbooksCheckbox')
      const orderbooksRow = document.getElementById('orderbooksSettingsRow')
      const obTopNInput = document.getElementById('orderbooksTopNInput')
      const obTimeoutInput = document.getElementById('orderbooksTimeoutInput')
      if (c1) c1.checked = !!settings.ui_show_momentum_1m
      if (c15) c15.checked = !!settings.ui_show_momentum_15m
      if (ch) ch.checked = !!settings.ui_show_heat
      if (cd) cd.checked = !!settings.ui_show_dispersion
      if (cgVol) cgVol.checked = !!settings.ui_show_cg_vol24
      if (cgMcap) cgMcap.checked = !!settings.ui_show_cg_mcap
      if (dirCb) dirCb.checked = !!settings.ui_show_direction
      if (groupByLiq) groupByLiq.checked = !!settings.ui_group_by_liquidity
      if (groupBySymbol) groupBySymbol.checked = !!settings.ui_group_by_symbol
      if (filterTransferCb) filterTransferCb.checked = !!settings.ui_arb_filter_transfer
      if (filterTransferStrictCb) filterTransferStrictCb.checked = !!settings.ui_arb_filter_transfer_strict_unknown
      if (useOrderbooksCb) useOrderbooksCb.checked = !!settings.use_orderbooks
      if (obTopNInput) obTopNInput.value = (typeof settings.orderbooks_refine_top_symbols === 'number') ? settings.orderbooks_refine_top_symbols : 5
      if (obTimeoutInput) obTimeoutInput.value = (typeof settings.orderbooks_per_exchange_timeout_sec === 'number') ? settings.orderbooks_per_exchange_timeout_sec : 8
      const tickersTimeoutInput = document.getElementById('tickersTimeoutInput')
      const stalePenaltyCheckbox = document.getElementById('stalePenaltyCheckbox')
      const staleGraceInput = document.getElementById('staleGraceInput')
      const stalePenaltyPerMinInput = document.getElementById('stalePenaltyPerMinInput')
      const staleHideAfterInput = document.getElementById('staleHideAfterInput')
      const stalePenaltySettingsRow = document.getElementById('stalePenaltySettingsRow')
      if (tickersTimeoutInput) tickersTimeoutInput.value = (typeof settings.tickers_per_exchange_timeout_sec === 'number') ? settings.tickers_per_exchange_timeout_sec : 12
      if (stalePenaltyCheckbox) stalePenaltyCheckbox.checked = !!settings.stale_rank_penalty_enabled
      if (staleGraceInput) staleGraceInput.value = (typeof settings.stale_rank_penalty_grace_sec === 'number') ? settings.stale_rank_penalty_grace_sec : 10
      if (stalePenaltyPerMinInput) stalePenaltyPerMinInput.value = (typeof settings.stale_rank_penalty_per_min_pct === 'number') ? settings.stale_rank_penalty_per_min_pct : 0.2
      if (staleHideAfterInput) staleHideAfterInput.value = (typeof settings.stale_rank_hide_after_sec === 'number') ? settings.stale_rank_hide_after_sec : 0
      if (stalePenaltySettingsRow) stalePenaltySettingsRow.style.opacity = (stalePenaltyCheckbox && stalePenaltyCheckbox.checked) ? '1' : '0.55'
      if (stalePenaltyCheckbox && stalePenaltySettingsRow && stalePenaltyCheckbox.dataset.bound !== '1') {
        stalePenaltyCheckbox.dataset.bound = '1'
        stalePenaltyCheckbox.addEventListener('change', () => {
          stalePenaltySettingsRow.style.opacity = stalePenaltyCheckbox.checked ? '1' : '0.55'
        })
      }
      if (orderbooksRow) orderbooksRow.style.display = (useOrderbooksCb && useOrderbooksCb.checked) ? '' : 'none'
      const filterLiqCb2 = document.getElementById('filterLiquidityCheckbox')
      if (filterLiqCb2) filterLiqCb2.checked = !!settings.ui_arb_filter_liquidity
      // Direction params UI
      try {
        const dirConfMinInput = document.getElementById('dirConfMinInput')
        const dcfg = settings && settings.direction ? settings.direction : {}
        if (dirConfMinInput) {
          if (typeof dcfg.conf_min_to_show === 'number') dirConfMinInput.value = dcfg.conf_min_to_show
          else dirConfMinInput.value = 0.4
        }
      } catch (e) { console.warn('direction params load failed', e) }
      applyColumnVisibility()
      try { applyPollingIntervals() } catch (_) { }
    })
    .catch((error) => {
      console.error("Ошибка при загрузке настроек:", error)
    })
}

// Функция для обновления списка бирж
function updateExchangesList(availableExchanges, enabledExchanges) {
  console.log("Обновление списка бирж...")
  const exchangesContainer = document.getElementById("exchangesContainer")

  if (!exchangesContainer) {
    console.error("Контейнер для бирж не найден")
    return
  }

  exchangesContainer.innerHTML = ""
  const available = sortExchangeNames(availableExchanges || [])
  const enabledSet = new Set((enabledExchanges || []).map(normalizeExchangeName))

  if (!available.length) {
    exchangesContainer.innerHTML = '<div class="text-center text-muted py-3">Нет доступных бирж</div>'
    updateActiveExchangeCounter()
    return
  }

  available.forEach((exchange) => {
    const isEnabled = enabledSet.has(normalizeExchangeName(exchange))
    const kind = exchangeKind(exchange)
    const label = exchangeKindLabel(kind)

    const exchangeItem = document.createElement("div")
    exchangeItem.className = `exchange-toggle exchange-kind-${kind} ${isEnabled ? "active" : ""}`
    exchangeItem.title = exchangeNote(exchange)

    const left = document.createElement('div')
    left.className = 'exchange-left min-w-0'

    const line = document.createElement('div')
    line.className = 'exchange-title-line'
    const nameSpan = document.createElement('span')
    nameSpan.className = 'exchange-name'
    nameSpan.textContent = exchange
    line.appendChild(nameSpan)
    if (label) {
      const badge = document.createElement('span')
      badge.className = `exchange-badge ${kind}`
      badge.textContent = label
      line.appendChild(badge)
    }
    const note = document.createElement('span')
    note.className = 'exchange-note'
    note.textContent = exchangeNote(exchange)
    left.appendChild(line)
    left.appendChild(note)

    const switchWrap = document.createElement('div')
    switchWrap.className = 'form-check form-switch mb-0'
    const cb = document.createElement('input')
    cb.type = 'checkbox'
    cb.className = 'form-check-input'
    cb.id = 'exchange_' + String(exchange).replace(/[^a-zA-Z0-9_-]/g, '_')
    cb.setAttribute('data-exchange', exchange)
    cb.checked = isEnabled
    switchWrap.appendChild(cb)

    const syncActive = () => {
      exchangeItem.classList.toggle('active', cb.checked)
      updateActiveExchangeCounter()
      setSettingsDirty('Список бирж изменён, нажми «Сохранить»')
    }
    exchangeItem.addEventListener('click', (e) => {
      if (e.target !== cb) cb.checked = !cb.checked
      syncActive()
    })
    cb.addEventListener('change', syncActive)

    exchangeItem.appendChild(left)
    exchangeItem.appendChild(switchWrap)
    exchangesContainer.appendChild(exchangeItem)
  })

  installExchangeQuickButtons()
  updateActiveExchangeCounter()
}

// Функция для сохранения настроек
function saveSettings() {
  console.log("Сохранение настроек...")

  // Получаем значения из полей ввода
  const minSpreadSettingInput = document.getElementById("minSpreadSettingInput")
  const maxSpreadInput = document.getElementById("maxSpreadInput")
  const uiPollingIntervalInput = document.getElementById("uiPollingIntervalInput")
  const useSeparateCheckbox = document.getElementById('useSeparatePollingCheckbox')
  const uiPollingIntervalStatusInput = document.getElementById('uiPollingIntervalStatusInput')
  const uiPollingIntervalOppsInput = document.getElementById('uiPollingIntervalOppsInput')
  const minNotionalInput = document.getElementById('minNotionalInput')
  const topLiquidityInput = document.getElementById('topLiquidityInput')
  const popoverMinProfitInput = document.getElementById('popoverMinProfitInput')
  const krakenKyberEnabledInput = document.getElementById('krakenKyberEnabledInput')
  const krakenKyberMinSpreadInput = document.getElementById('krakenKyberMinSpreadInput')
  const krakenKyberNotionalInput = document.getElementById('krakenKyberNotionalInput')
  const krakenKyberAssetLimitInput = document.getElementById('krakenKyberAssetLimitInput')

  if (!minSpreadSettingInput || !maxSpreadInput) {
    console.error("Поля ввода не найдены")
    return
  }

  const minSpread = Number.parseFloat(minSpreadSettingInput.value)
  const maxSpread = Number.parseFloat(maxSpreadInput.value)
  const uiPollSec = Number.parseFloat(uiPollingIntervalInput ? uiPollingIntervalInput.value : '')
  const useSeparate = !!(useSeparateCheckbox && useSeparateCheckbox.checked)
  const uiPollStatusSec = Number.parseFloat(uiPollingIntervalStatusInput ? uiPollingIntervalStatusInput.value : '')
  const uiPollOppsSec = Number.parseFloat(uiPollingIntervalOppsInput ? uiPollingIntervalOppsInput.value : '')
  const minNotionalVal = Number.parseFloat(minNotionalInput ? minNotionalInput.value : '')
  const topLiquidityVal = Number.parseFloat(topLiquidityInput ? topLiquidityInput.value : '')
  const popoverMinProfitVal = Number.parseFloat(popoverMinProfitInput ? popoverMinProfitInput.value : '')
  const krakenKyberMinSpreadVal = Number.parseFloat(krakenKyberMinSpreadInput ? krakenKyberMinSpreadInput.value : '')
  const krakenKyberNotionalVal = Number.parseFloat(krakenKyberNotionalInput ? krakenKyberNotionalInput.value : '')
  const krakenKyberAssetLimitVal = Number.parseFloat(krakenKyberAssetLimitInput ? krakenKyberAssetLimitInput.value : '')

  // Получаем список включенных бирж
  const enabledExchanges = []
  document.querySelectorAll('#exchangesContainer input[type="checkbox"]').forEach((checkbox) => {
    if (checkbox.checked) {
      // Извлекаем имя биржи из data-атрибута
      const exchangeName = checkbox.getAttribute("data-exchange")
      if (exchangeName) {
        enabledExchanges.push(exchangeName)
      }
    }
  })

  console.log("Включенные биржи:", enabledExchanges)

  // Читаем чекбоксы отображения колонок
  const c1 = document.getElementById('showMomentum1mCheckbox')
  const c15 = document.getElementById('showMomentum15mCheckbox')
  const ch = document.getElementById('showHeatCheckbox')
  const cd = document.getElementById('showDispersionCheckbox')
  const cgVol = document.getElementById('showCgVolCheckbox')
  const cgMcap = document.getElementById('showCgMcapCheckbox')
  const dirCb = document.getElementById('showDirectionCheckbox')
  const groupByLiq = document.getElementById('groupByLiquidityCheckbox')
  const groupBySymbol = document.getElementById('groupBySymbolCheckbox')
  const filterTransferCb = document.getElementById('filterTransferCheckbox')
  const filterTransferStrictCb = document.getElementById('filterTransferStrictCheckbox')
  const useOrderbooksCb = document.getElementById('useOrderbooksCheckbox')
  const orderbooksRow = document.getElementById('orderbooksSettingsRow')
  const obTopNInput = document.getElementById('orderbooksTopNInput')
  const obTimeoutInput = document.getElementById('orderbooksTimeoutInput')
  const tickersTimeoutInput = document.getElementById('tickersTimeoutInput')
  const stalePenaltyCheckbox = document.getElementById('stalePenaltyCheckbox')
  const staleGraceInput = document.getElementById('staleGraceInput')
  const stalePenaltyPerMinInput = document.getElementById('stalePenaltyPerMinInput')
  const staleHideAfterInput = document.getElementById('staleHideAfterInput')
  const filterLiquidityCb = document.getElementById('filterLiquidityCheckbox')
  const dirConfMinInput = document.getElementById('dirConfMinInput')

  // Формируем данные для отправки
  let settingsData
  try {
    settingsData = {
    min_spread: minSpread,
    max_spread: maxSpread,
    enabled_exchanges: enabledExchanges,
    ui_polling_interval_sec: (!isNaN(uiPollSec) && uiPollSec >= 1) ? uiPollSec : (settings.ui_polling_interval_sec || 7),
    ui_use_separate_polling: useSeparate,
    ui_polling_interval_status_sec: (useSeparate && !isNaN(uiPollStatusSec) && uiPollStatusSec >= 1) ? uiPollStatusSec : settings.ui_polling_interval_status_sec,
    ui_polling_interval_opportunities_sec: (useSeparate && !isNaN(uiPollOppsSec) && uiPollOppsSec >= 1) ? uiPollOppsSec : settings.ui_polling_interval_opportunities_sec,
    ui_show_momentum_1m: c1 ? c1.checked : settings.ui_show_momentum_1m,
    ui_show_momentum_15m: c15 ? c15.checked : settings.ui_show_momentum_15m,
    ui_show_heat: ch ? ch.checked : settings.ui_show_heat,
    ui_show_dispersion: cd ? cd.checked : settings.ui_show_dispersion,
    ui_show_cg_vol24: cgVol ? cgVol.checked : settings.ui_show_cg_vol24,
    ui_show_cg_mcap: cgMcap ? cgMcap.checked : settings.ui_show_cg_mcap,
    ui_show_direction: dirCb ? dirCb.checked : (settings.ui_show_direction || false),
    ui_group_by_liquidity: groupByLiq ? groupByLiq.checked : (settings.ui_group_by_liquidity || false),
    ui_group_by_symbol: groupBySymbol ? groupBySymbol.checked : (settings.ui_group_by_symbol || false),
    ui_arb_filter_transfer: filterTransferCb ? filterTransferCb.checked : (settings.ui_arb_filter_transfer || false),
    ui_arb_filter_transfer_strict_unknown: filterTransferStrictCb ? filterTransferStrictCb.checked : (settings.ui_arb_filter_transfer_strict_unknown || false),
    use_orderbooks: useOrderbooksCb ? useOrderbooksCb.checked : (settings.use_orderbooks || false),
    orderbooks_refine_top_symbols: (() => {
      const n = obTopNInput ? Number.parseInt(obTopNInput.value, 10) : NaN
      return (!isNaN(n) && n >= 0) ? n : (settings.orderbooks_refine_top_symbols || 5)
    })(),
    orderbooks_per_exchange_timeout_sec: (() => {
      const v = obTimeoutInput ? Number.parseFloat(obTimeoutInput.value) : NaN
      return (!isNaN(v) && v >= 1) ? v : (settings.orderbooks_per_exchange_timeout_sec || 8)
    })(),
    tickers_per_exchange_timeout_sec: (() => {
      const v = tickersTimeoutInput ? Number.parseFloat(tickersTimeoutInput.value) : NaN
      return (!isNaN(v) && v >= 2) ? v : (settings.tickers_per_exchange_timeout_sec || 12)
    })(),
    stale_rank_penalty_enabled: stalePenaltyCheckbox ? stalePenaltyCheckbox.checked : (settings.stale_rank_penalty_enabled || false),
    stale_rank_penalty_grace_sec: (() => {
      const v = staleGraceInput ? Number.parseFloat(staleGraceInput.value) : NaN
      return (!isNaN(v) && v >= 0) ? v : (settings.stale_rank_penalty_grace_sec || 10)
    })(),
    stale_rank_penalty_per_min_pct: (() => {
      const v = stalePenaltyPerMinInput ? Number.parseFloat(stalePenaltyPerMinInput.value) : NaN
      return (!isNaN(v) && v >= 0) ? v : (settings.stale_rank_penalty_per_min_pct || 0.2)
    })(),
    stale_rank_hide_after_sec: (() => {
      const v = staleHideAfterInput ? Number.parseFloat(staleHideAfterInput.value) : NaN
      return (!isNaN(v) && v >= 0) ? v : (settings.stale_rank_hide_after_sec || 0)
    })(),
    ui_arb_filter_liquidity: filterLiquidityCb ? filterLiquidityCb.checked : (settings.ui_arb_filter_liquidity || false),
    arb_min_notional_usd: (!isNaN(minNotionalVal) && minNotionalVal >= 0)
      ? minNotionalVal : (settings.arb_min_notional_usd || 300),
    ui_arb_top_liquidity_n: (!isNaN(topLiquidityVal) && topLiquidityVal >= 0)
      ? Math.floor(topLiquidityVal) : (settings.ui_arb_top_liquidity_n || 0),
    ui_popover_min_profit_pct: (!isNaN(popoverMinProfitVal) && popoverMinProfitVal >= 0)
      ? popoverMinProfitVal : (settings.ui_popover_min_profit_pct || 0),
    kraken_kyber_enabled: krakenKyberEnabledInput ? krakenKyberEnabledInput.checked : (settings.kraken_kyber_enabled !== false),
    kraken_kyber_min_spread: (!isNaN(krakenKyberMinSpreadVal) && krakenKyberMinSpreadVal >= 0)
      ? krakenKyberMinSpreadVal : (settings.kraken_kyber_min_spread || 0.5),
    kraken_kyber_notional_usd: (!isNaN(krakenKyberNotionalVal) && krakenKyberNotionalVal > 0)
      ? krakenKyberNotionalVal : (settings.kraken_kyber_notional_usd || 250),
    kraken_kyber_asset_limit: 0,
    direction: (() => {
      const current = (settings && settings.direction) ? settings.direction : {}
      const next = { ...current }
      if (dirConfMinInput && dirConfMinInput.value !== '') {
        const v = Number.parseFloat(dirConfMinInput.value)
        if (!isNaN(v)) next.conf_min_to_show = v
      }
      return next
    })(),
    }
  } catch (e) {
    console.error('Ошибка при формировании payload настроек:', e)
    try { showNotification('Ошибка при сохранении настроек (см. консоль)', 'error') } catch (_) { }
    return
  }

  // popoverMinProfitInput теперь сохраняем на сервере (через ui_popover_min_profit_pct)

  // Отправляем запрос на сохранение настроек
  fetch("/api/settings", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(settingsData),
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.success) {
        console.log("Настройки успешно сохранены.")
        showNotification("Настройки сохранены", "success")
        setSettingsSaved("Настройки сохранены и применены")

        // Закрываем модальное окно
        const settingsModal = document.getElementById("settingsModal")
        if (settingsModal && typeof bootstrap !== "undefined") {
          const modal = bootstrap.Modal.getInstance(settingsModal)
          if (modal) modal.hide()
        }

        // Обновляем минимальный спред в фильтре
        const minSpreadInput = document.getElementById("minSpreadInput")
        if (minSpreadInput) minSpreadInput.value = minSpread

        // Обновляем глобальную переменную
        window.minSpread = minSpread

        // Обновляем локальные настройки UI и применяем видимость
        settings = { ...settings, ...settingsData }
        applyColumnVisibility()
        try { applyPollingIntervals() } catch (_) { }
        // Обновляем данные
        updateOpportunities()
      } else {
        console.error("Ошибка при сохранении настроек:", data.error)
        showNotification("Ошибка при сохранении настроек: " + data.error, "error")
        setSettingsError("Ошибка сохранения: " + data.error)
      }
    })
    .catch((error) => {
      console.error("Ошибка при отправке запроса на сохранение настроек:", error)
      showNotification("Ошибка при сохранении настроек", "error")
      setSettingsError("Ошибка отправки настроек")
    })
}

// Утилиты форматирования чисел (короткий формат: K/M/B/T)
function formatNumberShort(value) {
  try {
    if (value === null || value === undefined || !isFinite(value)) return '—'
    const abs = Math.abs(value)
    if (abs >= 1e12) return (value / 1e12).toFixed(2) + 'T'
    if (abs >= 1e9) return (value / 1e9).toFixed(2) + 'B'
    if (abs >= 1e6) return (value / 1e6).toFixed(2) + 'M'
    if (abs >= 1e3) return (value / 1e3).toFixed(2) + 'K'
    return value.toFixed(0)
  } catch (e) {
    return '—'
  }
}

// Определение тьера ликвидности по CoinGecko (объём/капитализация)
function getLiquidityTier(opp) {
  try {
    const vol = (typeof opp.cg_volume_24h_usd === 'number' && isFinite(opp.cg_volume_24h_usd)) ? opp.cg_volume_24h_usd : null
    const mcap = (typeof opp.cg_market_cap_usd === 'number' && isFinite(opp.cg_market_cap_usd)) ? opp.cg_market_cap_usd : null
    const high = (vol !== null && vol >= 5_000_000) || (mcap !== null && mcap >= 300_000_000)
    const mid = (vol !== null && vol >= 500_000) || (mcap !== null && mcap >= 50_000_000)
    if (high) return { code: 'high', label: 'Ликвидная', tooltip: 'Высокая ликвидность: Vol24 ≥ $5M или MCAP ≥ $300M' }
    if (mid) return { code: 'mid', label: 'Средняя', tooltip: 'Средняя ликвидность: Vol24 ≥ $0.5M или MCAP ≥ $50M' }
    if (vol === null && mcap === null) return { code: 'na', label: '—', tooltip: 'Данных по ликвидности нет' }
    return { code: 'low', label: 'Низко‑кап', tooltip: 'Низкая ликвидность: Vol24 < $0.5M и MCAP < $50M' }
  } catch (e) {
    return { code: 'na', label: '—', tooltip: 'Данных по ликвидности нет' }
  }
}

// Функция для загрузки черного списка
function getOrderbookLiquidityBadge(opp) {
  try {
    const buy = (typeof opp.buy_liquidity_usd === 'number' && isFinite(opp.buy_liquidity_usd)) ? opp.buy_liquidity_usd : null
    const sell = (typeof opp.sell_liquidity_usd === 'number' && isFinite(opp.sell_liquidity_usd)) ? opp.sell_liquidity_usd : null
    const minL = (typeof opp.min_liquidity_usd === 'number' && isFinite(opp.min_liquidity_usd)) ? opp.min_liquidity_usd : null
    const minReq = (settings && typeof settings.arb_min_notional_usd === 'number' && isFinite(settings.arb_min_notional_usd))
      ? settings.arb_min_notional_usd : null
    if (buy === null && sell === null && minL === null) {
      return { label: 'СТАКАН ?', className: 'bg-light text-dark', tooltip: 'Стакан: нет данных' }
    }
    const ok = (minReq !== null && minL !== null) ? (minL >= minReq) : null
    const label = ok === true ? 'СТАКАН ОК' : (ok === false ? 'СТАКАН МАЛО' : 'СТАКАН')
    const className = ok === true ? 'bg-success' : (ok === false ? 'bg-warning text-dark' : 'bg-secondary')
    const tooltip = `Стакан: покупка ${formatVolume(buy)} / продажа ${formatVolume(sell)} / мин ${formatVolume(minL)}${minReq !== null ? ' | мин ' + formatVolume(minReq) : ''}`
    return { label, className, tooltip }
  } catch (e) {
    return { label: 'СТАКАН ?', className: 'bg-secondary', tooltip: 'Стакан: н/д' }
  }
}

function loadBlacklist() {
  console.log("Загрузка черного списка...")
  fetch("/api/blacklist")
    .then((response) => response.json())
    .then((data) => {
      if (data.success) {
        blacklist = data.data
        updateBlacklistTable()
      } else {
        console.error("Ошибка при загрузке черного списка:", data.error)
      }
    })
    .catch((error) => {
      console.error("Ошибка при загрузке черного списка:", error)
    })
}

// Функция для обновления таблицы черного списка
function updateBlacklistTable() {
  console.log("Обновление таблицы черного списка...")
  const tableBody = document.getElementById("blacklistTableBody")

  if (!tableBody) {
    console.error("Элемент таблицы черного списка не найден")
    return
  }

  // Очищаем таблицу
  tableBody.innerHTML = ""

  // Если нет данных, показываем сообщение
  if (
    (!blacklist.permanent_list || blacklist.permanent_list.length === 0) &&
    (!blacklist.temporary_list || Object.keys(blacklist.temporary_list).length === 0)
  ) {
    const row = document.createElement("tr")
    row.innerHTML = '<td colspan="3" class="text-center">Черный список пуст</td>'
    tableBody.appendChild(row)
    return
  }

  // Добавляем постоянные элементы
  if (blacklist.permanent_list && blacklist.permanent_list.length > 0) {
    blacklist.permanent_list.forEach((symbol) => {
      const row = document.createElement("tr")
      row.innerHTML = `
                <td>${symbol}</td>
                <td>Постоянно</td>
                <td>
                    <button class="btn btn-sm btn-danger" onclick="removeFromBlacklist('${symbol}')">
                        Удалить
                    </button>
                </td>
            `
      tableBody.appendChild(row)
    })
  }

  // Добавляем временные элементы
  if (blacklist.temporary_list && Object.keys(blacklist.temporary_list).length > 0) {
    for (const [symbol, expiry] of Object.entries(blacklist.temporary_list)) {
      const row = document.createElement("tr")
      row.innerHTML = `
                <td>${symbol}</td>
                <td>${expiry}</td>
                <td>
                    <button class="btn btn-sm btn-danger" onclick="removeFromBlacklist('${symbol}')">
                        Удалить
                    </button>
                </td>
            `
      tableBody.appendChild(row)
    }
  }
}

// Функция для добавления в черный список
function addToBlacklist() {
  console.log("Добавление в черный список...")

  // Получаем значения из полей ввода
  const blacklistSymbolInput = document.getElementById("blacklistSymbolInput")
  const blacklistHoursInput = document.getElementById("blacklistHoursInput")

  if (!blacklistSymbolInput || !blacklistHoursInput) {
    console.error("Поля ввода не найдены")
    return
  }

  const symbol = blacklistSymbolInput.value.trim().toUpperCase()
  const hours = Number.parseInt(blacklistHoursInput.value)
  const permanent = (Number.isFinite(hours) && hours === 0)

  if (!symbol) {
    showNotification("Введите символ", "error")
    return
  }

  // Формируем данные для отправки
  const blacklistData = {
    symbol: symbol,
    hours: (permanent ? 0 : (Number.isFinite(hours) && hours > 0 ? hours : 24)),
    permanent: permanent,
  }

  // Отправляем запрос на добавление в черный список
  fetch("/api/blacklist", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(blacklistData),
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.success) {
        console.log("Символ успешно добавлен в черный список.")
        showNotification("Символ добавлен в черный список", "success")

        // Очищаем поле ввода
        blacklistSymbolInput.value = ""

        // Обновляем черный список
        loadBlacklist()

        // Обновляем данные
        updateOpportunities()
      } else {
        console.error("Ошибка при добавлении в черный список:", data.error)
        showNotification("Ошибка при добавлении в черный список: " + data.error, "error")
      }
    })
    .catch((error) => {
      console.error("Ошибка при отправке запроса на добавление в черный список:", error)
      showNotification("Ошибка при добавлении в черный список", "error")
    })
}

// Функция для добавления в черный список из таблицы
function addToBlacklistFromTable(symbol) {
  console.log("Добавление в черный список из таблицы:", symbol)

  // Формируем данные для отправки
  const blacklistData = {
    symbol: symbol,
    hours: 24,
    permanent: false,
  }

  // Отправляем запрос на добавление в черный список
  fetch("/api/blacklist", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(blacklistData),
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.success) {
        console.log("Символ успешно добавлен в черный список.")
        showNotification(`Символ ${symbol} добавлен в черный список на 24 часа`, "success")

        // Обновляем черный список
        loadBlacklist()

        // Обновляем данные
        updateOpportunities()
      } else {
        console.error("Ошибка при добавлении в черный список:", data.error)
        showNotification("Ошибка при добавлении в черный список: " + data.error, "error")
      }
    })
    .catch((error) => {
      console.error("Ошибка при отправке запроса на добавление в черный список:", error)
      showNotification("Ошибка при добавлении в черный список", "error")
    })
}

// Функция для удаления из черного списка
function removeFromBlacklist(symbol) {
  console.log("Удаление из черного списка:", symbol)

  // Отправляем запрос на удаление из черного списка
  fetch(`/api/blacklist?symbol=${symbol}`, {
    method: "DELETE",
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.success) {
        console.log("Символ успешно удален из черного списка.")
        showNotification("Символ удален из черного списка", "success")

        // Обновляем черный список
        loadBlacklist()

        // Обновляем данные
        updateOpportunities()
      } else {
        console.error("Ошибка при удалении из черного списка:", data.error)
        showNotification("Ошибка при удалении из черного списка: " + data.error, "error")
      }
    })
    .catch((error) => {
      console.error("Ошибка при отправке запроса на удаление из черного списка:", error)
      showNotification("Ошибка при удалении из черного списка", "error")
    })
}

// Функция для добавления монеты в черный список из информационного окна
function addToBlacklistFromInfo(symbol, hours, permanent) {
  console.log(`Добавление ${symbol} в черный список ${permanent ? 'навсегда' : 'на ' + hours + ' часов'}`)

  // Формируем данные для отправки
  const blacklistData = {
    symbol: symbol,
    hours: hours,
    permanent: permanent
  }

  // Отправляем запрос на добавление в черный список
  fetch("/api/blacklist", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(blacklistData),
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.success) {
        const message = permanent
          ? `Монета ${symbol} добавлена в постоянный черный список`
          : `Монета ${symbol} добавлена в черный список на ${hours} часов`
        console.log(message)
        showNotification(message, "success")

        // Обновляем черный список
        loadBlacklist()

        // Обновляем данные
        updateOpportunities()

        // Закрываем модальное окно
        const coinInfoModal = document.getElementById("coinInfoModal")
        if (coinInfoModal && typeof bootstrap !== "undefined") {
          const modalInstance = bootstrap.Modal.getInstance(coinInfoModal)
          if (modalInstance) modalInstance.hide()
        }
      } else {
        console.error("Ошибка при добавлении в черный список:", data.error)
        showNotification("Ошибка при добавлении в черный список: " + data.error, "error")
      }
    })
    .catch((error) => {
      console.error("Ошибка при отправке запроса на добавление в черный список:", error)
      showNotification("Ошибка при добавлении в черный список", "error")
    })
}

// Функция для отображения информации о монете
function splitNormalizedPairSymbol(symbol) {
  const raw = String(symbol || '').toUpperCase().trim()
  if (!raw) return { base: '', quote: '' }

  const sepMatch = raw.match(/^([A-Z0-9]+)[\\/_-]([A-Z0-9]+)$/)
  if (sepMatch) return { base: sepMatch[1], quote: sepMatch[2] }

  const cleaned = raw.replace(/[^A-Z0-9]/g, '')
  const quotes = [
    'USDT', 'USDC', 'USD', 'BTC', 'ETH', 'BNB',
    'FDUSD', 'TUSD', 'BUSD', 'DAI',
    'EUR', 'GBP', 'TRY', 'RUB', 'UAH', 'JPY',
  ].sort((a, b) => b.length - a.length)

  for (const q of quotes) {
    if (cleaned.endsWith(q) && cleaned.length > q.length) {
      return { base: cleaned.slice(0, -q.length), quote: q }
    }
  }

  return { base: cleaned, quote: '' }
}

function getExchangeTradeUrl(exchangeName, symbol) {
  try {
    const { base, quote } = splitNormalizedPairSymbol(symbol)
    if (!base || !quote) return ''
    return buildExchangeTradeUrlFromParts(exchangeName, base, quote)
  } catch (_) {
    return ''
  }
}

function buildExchangeLinksHtml(symbol, exchanges, buyExchange, sellExchange) {
  const list = Array.isArray(exchanges) ? exchanges : []
  const buy = String(buyExchange || '')
  const sell = String(sellExchange || '')
  return list.map(ex => {
    const name = String(ex || '').trim()
    if (!name) return ''
    const isAlphaLink = name.toLowerCase().includes('binance alpha')
    const url = getExchangeTradeUrl(name, symbol)

    let cls = 'btn-outline-secondary'
    let icon = '<i class="fas fa-external-link-alt me-2"></i>'
    let style = 'border-radius: 12px; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); border-width: 2px;'

    if (name === buy) {
      cls = 'btn-success shadow'
      icon = '<i class="fas fa-shopping-cart me-2"></i>'
      style += ' background: linear-gradient(135deg, #28a745 0%, #218838 100%); border: none;'
    } else if (name === sell) {
      cls = 'btn-danger shadow'
      icon = '<i class="fas fa-hand-holding-usd me-2"></i>'
      style += ' background: linear-gradient(135deg, #dc3545 0%, #c82333 100%); border: none;'
    }

    return `
      <a href="${url}" target="_blank" rel="noopener noreferrer" 
         class="btn ${cls} text-decoration-none px-4 py-2 fw-bold d-flex align-items-center justify-content-center" 
         style="${style}"
         onmouseover="this.style.transform='translateY(-2px) scale(1.02)'; this.style.boxShadow='0 10px 20px rgba(0,0,0,0.4)';"
         onmouseout="this.style.transform='translateY(0) scale(1)'; this.style.boxShadow='none';">
        ${icon}${name}
      </a>`
  }).filter(Boolean).join('')
}


async function hydrateBinanceAlphaLinks(symbol) {
  try {
    const token = await fetchBinanceAlphaToken(symbol)
    if (!token) return
    const directUrl = buildBinanceAlphaUrlFromToken(token, symbol)
    document.querySelectorAll('a[data-exchange*="Binance Alpha"]').forEach(a => {
      a.href = directUrl
      a.classList.remove('alpha-link-pending')
      a.classList.add('alpha-link-direct')
      const contract = String(token.contractAddress || token.contract_address || '').trim()
      if (contract) a.title = `Binance Alpha: прямой contract link ${contract}`
    })
  } catch (_) { }
}

function isBinanceAlphaExchange(name) {
  return String(name || '').toLowerCase().includes('binance alpha')
}

function alphaStatusBadge(label) {
  return `<span class="badge alpha-status-badge px-2 py-1"><i class="fas fa-circle-info me-1"></i>${label}: Alpha manual</span>`
}

// Загрузка статусов вывода/ввода для бирж
async function loadExchangeStatuses(symbol, buyExchange, sellExchange, includeContracts = true) {
  try {
    // 0) Сначала смотрим в клиентском кэше — его мог заполнить prefetchTopAssets
    //    при рендере таблицы. Если данные свежие (<55с), сразу используем их.
    let dFast = null
    try {
      const base = String(symbol || '').toUpperCase().replace(/(USDT|USDC|USD|BUSD|DAI|FDUSD)$/i, '').trim()
      const cached = getClientAssetStatus(base)
      if (cached && Array.isArray(cached.data) && cached.data.length > 0) {
        dFast = cached
        console.log(`[modal] using prefetched asset_status for ${base}`)
      }
    } catch (_) { /* noop */ }

    // 1) Если кэша нет — обычный запрос.
    if (!dFast) {
      const statusUrlFast = `/api/asset_status/${encodeURIComponent(symbol)}?overall_timeout=7&per_exchange_timeout=4.5`
      try {
        const respFast = await fetch(statusUrlFast, { signal: AbortSignal.timeout(12000) })
        dFast = await respFast.json()
      } catch (err) {
        console.warn(`[modal] asset_status fetch failed for ${symbol}:`, err && err.name)
        dFast = null
      }
    }

    const buyDepositEl = document.getElementById('buyExDepositStatus')
    const buyWithdrawEl = document.getElementById('buyExWithdrawStatus')
    const sellDepositEl = document.getElementById('sellExDepositStatus')
    const sellWithdrawEl = document.getElementById('sellExWithdrawStatus')
    const contractsEl = document.getElementById('infoFastContracts')

    const badgeHtml = (label, value, exchangeName = '') => {
      if (isBinanceAlphaExchange(exchangeName)) {
        return alphaStatusBadge(label)
      }
      if (value === true) {
        return `<span class="badge bg-success bg-opacity-25 text-white border border-success border-opacity-50 px-2 py-1"><i class="fas fa-check-circle me-1"></i>${label}: РАБОТАЕТ</span>`
      }
      if (value === false) {
        return `<span class="badge bg-danger bg-opacity-25 text-white border border-danger border-opacity-50 px-2 py-1"><i class="fas fa-times-circle me-1"></i>${label}: НЕ РАБОТАЕТ</span>`
      }
      return `<span class="badge bg-secondary bg-opacity-25 text-white border border-secondary border-opacity-50 px-2 py-1"><i class="fas fa-question-circle me-1"></i>${label}: НЕИЗВЕСТНО</span>`
    }

    const hasKnownStatus = (payload) => {
      const rows = Array.isArray(payload && payload.data) ? payload.data : []
      return rows.some(r => r && (r.deposit_enabled === true || r.deposit_enabled === false || r.withdraw_enabled === true || r.withdraw_enabled === false))
    }
    let usedCachedFast = false
    if (!dFast || !dFast.success) {
      const cachedFast = (window.__coinInfoAssetStatus && window.__coinInfoAssetStatus.success) ? window.__coinInfoAssetStatus : null
      if (cachedFast && hasKnownStatus(cachedFast)) {
        dFast = cachedFast
        usedCachedFast = true
      } else {
        if (buyDepositEl) buyDepositEl.innerHTML = badgeHtml('Статус', null, buyExchange)
        if (buyWithdrawEl) buyWithdrawEl.innerHTML = badgeHtml('Статус', null, buyExchange)
        if (sellDepositEl) sellDepositEl.innerHTML = badgeHtml('Статус', null, sellExchange)
        if (sellWithdrawEl) sellWithdrawEl.innerHTML = badgeHtml('Статус', null, sellExchange)
        return
      }
    }

    if (!usedCachedFast && hasKnownStatus(dFast)) {
      try { window.__coinInfoAssetStatus = dFast } catch (_) { }
    }

    const exKey = (name) => String(name || '').toLowerCase().replace(/[^a-z0-9]/g, '')
    const buyK = exKey(buyExchange)
    const sellK = exKey(sellExchange)

    const errorsMap = (dFast && dFast.errors && typeof dFast.errors === 'object') ? dFast.errors : null
    const getErr = (k) => {
      if (!errorsMap) return null
      // ошибки могут быть ключами вида 'gate.io'/'bybit' и т.п.
      const direct = errorsMap[k]
      if (direct) return String(direct)
      // fallback: попробуем найти по нормализованному ключу
      const nk = String(k || '').toLowerCase().replace(/[^a-z0-9]/g, '')
      for (const [ek, ev] of Object.entries(errorsMap)) {
        if (String(ek || '').toLowerCase().replace(/[^a-z0-9]/g, '') === nk) return String(ev)
      }
      return null
    }

    let buyDeposit = null
    let buyWithdraw = null
    let sellDeposit = null
    let sellWithdraw = null

    const items = Array.isArray(dFast.data) ? dFast.data : []
    for (const item of items) {
      const k = exKey(item.exchange)
      if (k === buyK) {
        if (item.deposit_enabled !== null) {
          if (buyDeposit === null) buyDeposit = item.deposit_enabled
          else if (item.deposit_enabled === true) buyDeposit = true
        }
        if (item.withdraw_enabled !== null) {
          if (buyWithdraw === null) buyWithdraw = item.withdraw_enabled
          else if (item.withdraw_enabled === true) buyWithdraw = true
        }
      }
      if (k === sellK) {
        if (item.deposit_enabled !== null) {
          if (sellDeposit === null) sellDeposit = item.deposit_enabled
          else if (item.deposit_enabled === true) sellDeposit = true
        }
        if (item.withdraw_enabled !== null) {
          if (sellWithdraw === null) sellWithdraw = item.withdraw_enabled
          else if (item.withdraw_enabled === true) sellWithdraw = true
        }
      }
    }

    // Обновляем UI статусов
    if (buyDepositEl) buyDepositEl.innerHTML = badgeHtml('Ввод', buyDeposit, buyExchange)
    if (buyWithdrawEl) buyWithdrawEl.innerHTML = badgeHtml('Вывод', buyWithdraw, buyExchange)
    if (sellDepositEl) sellDepositEl.innerHTML = badgeHtml('Ввод', sellDeposit, sellExchange)
    if (sellWithdrawEl) sellWithdrawEl.innerHTML = badgeHtml('Вывод', sellWithdraw, sellExchange)

    const escapeHtml = (s) => String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;')

    // Статусы по всем биржам в модалке (чтобы было видно не только buy/sell)
    try {
      const allStatusesEl = document.getElementById('infoAllExStatuses')
      if (allStatusesEl) {
        const pillHtml = (label, value) => {
          if (value === 'alpha') return `<span class="status-pill alpha-manual"><i class="fas fa-circle-info"></i>${label}: manual</span>`
          if (value === true) return `<span class="status-pill open"><i class="fas fa-check-circle"></i>${label}</span>`
          if (value === false) return `<span class="status-pill closed"><i class="fas fa-times-circle"></i>${label}</span>`
          return `<span class="status-pill unknown"><i class="fas fa-question-circle"></i>${label}</span>`
        }

        const summarizeBool = (rows, field) => {
          if (!Array.isArray(rows) || rows.length === 0) return null
          const values = rows.map(r => (r && Object.prototype.hasOwnProperty.call(r, field)) ? r[field] : null)
          if (values.some(v => v === true)) return true
          const hasNull = values.some(v => v === null || v === undefined)
          const hasFalse = values.some(v => v === false)
          if (hasFalse && !hasNull) return false
          return null
        }

        const byKey = {}
        for (const item of items) {
          const k = exKey(item.exchange)
          if (!k) continue
          if (!byKey[k]) byKey[k] = []
          byKey[k].push(item)
        }

        let exchangeOrder = []
        try {
          if (Array.isArray(window.__coinInfoAllExchanges) && window.__coinInfoAllExchanges.length) {
            exchangeOrder = window.__coinInfoAllExchanges.map(x => String(x || '').trim()).filter(Boolean)
          }
        } catch (_) { }

        if (!exchangeOrder.length) {
          exchangeOrder = Array.from(new Set(items.map(x => x && x.exchange).filter(Boolean)))
        }

        const rowsHtml = exchangeOrder.map(exName => {
          const k = exKey(exName)
          const rows = byKey[k] || []
          let dep = summarizeBool(rows, 'deposit_enabled')
          let wd = summarizeBool(rows, 'withdraw_enabled')
          let errText = (dep === null && wd === null) ? (getErr(exName) || getErr(k)) : null
          if (isBinanceAlphaExchange(exName)) {
            dep = 'alpha'
            wd = 'alpha'
            errText = 'Ручной Alpha-источник: проверяй доступность внутри Binance, это не красный блокер.'
          }
          return `
            <tr class="${isBinanceAlphaExchange(exName) ? 'alpha-manual-status-row' : ''}">
              <td class="fw-bold">${escapeHtml(exName)}</td>
              <td>${pillHtml('Ввод', dep)}</td>
              <td>${pillHtml('Вывод', wd)}</td>
              <td class="text-muted small">${errText ? escapeHtml(errText) : '—'}</td>
            </tr>
          `
        }).join('')

        allStatusesEl.innerHTML = rowsHtml
          ? `
            <table class="table table-sm table-dark border-0 mb-0" style="font-size: 0.78rem;">
              <thead><tr><th>Биржа</th><th>Ввод</th><th>Вывод</th><th>Причина</th></tr></thead>
              <tbody>${rowsHtml}</tbody>
            </table>
          `
          : `<div class="small text-muted">Нет данных</div>`
      }
    } catch (_) { }

    // Если статусы неизвестны — покажем короткую причину (если сервер прислал errors)
    try {
      const buyErr = (buyDeposit === null && buyWithdraw === null) ? getErr(buyExchange) || getErr(buyK) : null
      const sellErr = (sellDeposit === null && sellWithdraw === null) ? getErr(sellExchange) || getErr(sellK) : null

      const attachReason = (elId, errText) => {
        if (!errText) return
        const el = document.getElementById(elId)
        if (!el) return
        // добавляем только если там уже НЕИЗВЕСТНО
        if (String(el.innerHTML || '').includes('НЕИЗВЕСТНО')) {
          el.innerHTML += `<div class="small text-muted mt-1" style="opacity:0.9;">Причина: ${escapeHtml(errText)}</div>`
        }
      }

      attachReason('buyExWithdrawStatus', buyErr)
      attachReason('sellExWithdrawStatus', sellErr)
    } catch (_) { }

    try {
      const last = (() => { try { return window.__coinInfoLastData } catch (_) { return null } })()
      if (last && String(last.symbol || '').toUpperCase() === String(symbol || '').toUpperCase()) {
        renderDetailedCoinInfo(last)
      }
    } catch (_) { }

    if (includeContracts) {
      // 2) Второй запрос: контракты/coin_id (сервер отдаёт кэш или pending, без долгих зависаний)
      let dContracts = null
      try {
        const statusUrlContracts = `/api/asset_status/${encodeURIComponent(symbol)}?include_contracts=1&overall_timeout=7&per_exchange_timeout=4.5`
        const resp2 = await fetch(statusUrlContracts, { signal: AbortSignal.timeout(12000) })
        dContracts = await resp2.json()
      } catch (_) {
        dContracts = null
      }

      // Обновляем ссылку CoinGecko/GeckoTerminal: если есть контракт — ведём в GeckoTerminal, иначе поиск по тикеру в CoinGecko
      try {
        const cgLinkEl = document.getElementById('coinInfoCGLnk')
        const res = dContracts && dContracts.token_resolution ? dContracts.token_resolution : null
        let cgContractPick = null
        if (cgLinkEl && res) {
        const canonPlatform = (name) => {
          const n = String(name || '').trim().toLowerCase()
          const map = {
            'eth': 'ethereum', 'erc20': 'ethereum', 'ethereum': 'ethereum',
            'bsc': 'binance-smart-chain', 'bep20': 'binance-smart-chain', 'bep-20': 'binance-smart-chain', 'binance smart chain': 'binance-smart-chain',
            'polygon': 'polygon-pos', 'matic': 'polygon-pos', 'polygon-pos': 'polygon-pos',
            'tron': 'tron', 'trc20': 'tron', 'trc-20': 'tron',
            'sol': 'solana', 'solana': 'solana',
            'arbitrum': 'arbitrum-one', 'arbitrum one': 'arbitrum-one', 'arb': 'arbitrum-one',
            'optimism': 'optimistic-ethereum', 'op': 'optimistic-ethereum', 'optimistic-ethereum': 'optimistic-ethereum',
            'base': 'base',
            'avalanche': 'avalanche', 'avax': 'avalanche', 'c-chain': 'avalanche',
            'sui': 'sui', 'sui mainnet': 'sui',
            'aptos': 'aptos', 'apt': 'aptos', 'aptos mainnet': 'aptos',
            'ton': 'the-open-network', 'toncoin': 'the-open-network', 'the open network': 'the-open-network', 'the-open-network': 'the-open-network',
            'near': 'near-protocol', 'near protocol': 'near-protocol', 'near-protocol': 'near-protocol'
          }
          if (map[n]) return map[n]
          if (n.includes('sui')) return 'sui'
          if (n.includes('apt')) return 'aptos'
          if (n.includes('ton')) return 'the-open-network'
          if (n.includes('near')) return 'near-protocol'
          if (n.includes('tron')) return 'tron'
          if (n.includes('sol')) return 'solana'
          if (n.includes('arb')) return 'arbitrum-one'
          if (n.includes('optim')) return 'optimistic-ethereum'
          return n
        }
        const normalizeContract = (value) => {
          let s = String(value || '').trim()
          if (!s) return ''
          const evm = s.match(/0x[a-fA-F0-9]{40}/)
          if (evm) return evm[0]
          if (s.includes('::')) s = s.split('::')[0]
          if (s.includes(':')) s = s.split(':')[0]
          return s.trim()
        }

        const pickContract = () => {
          const contracts = (res && res.contracts) ? res.contracts : {}
          const rows = Array.isArray(dFast && dFast.data) ? dFast.data : []
          const prefer = [buyExchange, sellExchange].filter(Boolean)
          for (const ex of prefer) {
            const row = rows.find(r => exKey(r.exchange) === exKey(ex))
            if (!row) continue
            const direct = (row.contract_address || row.contract || '').toString().trim()
            const canon = canonPlatform(row.chain || row.network || '')
            if (canon && contracts && contracts[canon]) return { contract: contracts[canon], platform: canon, source: 'contracts' }
            if (direct) return { contract: direct, platform: canon || null, source: 'exchange' }
          }

          const counts = {}
          for (const row of rows) {
            const direct = (row.contract_address || row.contract || '').toString().trim()
            const canon = canonPlatform(row.chain || row.network || '')
            if (!direct && !(contracts && contracts[canon])) continue
            if (!canon) continue
            counts[canon] = (counts[canon] || 0) + 1
          }
          const topChain = Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0]
          if (topChain && contracts && contracts[topChain]) return { contract: contracts[topChain], platform: topChain, source: 'contracts' }

          const keys = contracts ? Object.keys(contracts) : []
          if (keys.length > 0) return { contract: contracts[keys[0]], platform: keys[0], source: 'contracts' }
          return null
        }

        cgContractPick = pickContract()
        try { res._cg_contract = cgContractPick } catch (_) { }
        const { base: cgBase } = splitNormalizedPairSymbol(symbol)
        const cgSearchBySymbol = `https://www.coingecko.com/en/search?query=${encodeURIComponent(cgBase || symbol)}`

        // ---- CoinGecko button: prefer direct /coins/{id} when known ----
        // Server sends `coingecko_url` (direct when coin_id is resolved,
        // search otherwise). We upgrade the button if that URL is present.
        const directCgUrl = String((res && res.coingecko_url) || '').trim()
        if (directCgUrl) {
          cgLinkEl.href = directCgUrl
          cgLinkEl.title = res.coin_id ? `CoinGecko: ${res.coin_id}` : 'CoinGecko: поиск по тикеру'
          try { cgLinkEl.innerHTML = `<i class="fas fa-external-link-alt me-1"></i>CoinGecko` } catch (_) { }
          try {
            window.__coinInfoPreferredCgUrl = directCgUrl
            window.__coinInfoPreferredCgReason = res.coin_id ? 'coingecko-direct' : 'coingecko-search'
          } catch (_) { }
        } else {
          cgLinkEl.href = cgSearchBySymbol
          cgLinkEl.title = `CoinGecko: поиск по тикеру`
          try { cgLinkEl.innerHTML = `<i class="fas fa-external-link-alt me-1"></i>CoinGecko` } catch (_) { }
          try {
            window.__coinInfoPreferredCgUrl = cgSearchBySymbol
            window.__coinInfoPreferredCgReason = 'coingecko-search'
          } catch (_) { }
        }

        // ---- Site button: only show if CoinGecko returned a real homepage ----
        try {
          const siteBtn = document.getElementById('coinInfoSiteLnk')
          const hp = String((res && res.homepage) || '').trim()
          if (siteBtn) {
            if (hp && /^https?:\/\//i.test(hp)) {
              siteBtn.href = hp
              siteBtn.title = `Официальный сайт: ${hp}`
              siteBtn.style.display = ''
            } else {
              siteBtn.style.display = 'none'
            }
          }
        } catch (_) { }

        // ---- DexScreener button: direct link to the highest-priority chain ----
        // We use dex_direct_links from the server response (already pre-built
        // with chain slug + contract). Prefer ethereum → bsc → polygon → sol
        // → anything else, so the button points to the most-traded venue.
        try {
          const dexBtn = document.getElementById('coinInfoDexLnk')
          const directs = Array.isArray(res && res.dex_direct_links) ? res.dex_direct_links : []
          if (dexBtn && directs.length > 0) {
            const priority = {
              'ethereum': 0, 'binance-smart-chain': 1, 'solana': 2,
              'polygon-pos': 3, 'arbitrum-one': 4, 'base': 5,
              'optimistic-ethereum': 6, 'tron': 7, 'avalanche': 8,
              'sui': 9, 'aptos': 10, 'the-open-network': 11, 'near-protocol': 12
            }
            const sorted = directs.slice().sort((a, b) => {
              const ap = priority[a.chain] !== undefined ? priority[a.chain] : 99
              const bp = priority[b.chain] !== undefined ? priority[b.chain] : 99
              return ap - bp
            })
            const best = sorted[0]
            if (best && best.dexscreener) {
              dexBtn.href = best.dexscreener
              dexBtn.title = `DexScreener (${best.chain}): ${best.contract}`
              dexBtn.style.display = ''
            } else {
              dexBtn.style.display = 'none'
            }
          } else if (dexBtn) {
            dexBtn.style.display = 'none'
          }
        } catch (_) { }
        }
      } catch (_) { }

      // Отображение контрактов (Token Resolution)
      if (contractsEl && dContracts && dContracts.token_resolution) {
        const res = dContracts.token_resolution
        let html = '<div class="info-contracts-panel">'
        html += '<div class="small text-muted text-uppercase fw-bold mb-1" style="letter-spacing: 1px;"><i class="fas fa-file-contract me-2 text-info"></i>Смарт-контракты (DEX/Explorer)</div>'

        if (res.pending) {
          html += '<div class="text-muted small mb-1">Обновляю данные CoinGecko в фоне…</div>'
        }

        if (res.contracts && Object.keys(res.contracts).length > 0) {
          // Build a { chain -> {dexscreener, geckoterminal} } lookup from
          // dex_direct_links so we can attach direct DS/GT buttons to every
          // contract row instead of a single "GeckoTerminal search" link at
          // the bottom. This removes any dependence on external search engines.
          const directByChain = {}
          try {
            const directs = Array.isArray(res && res.dex_direct_links) ? res.dex_direct_links : []
            for (const entry of directs) {
              if (entry && entry.chain) directByChain[String(entry.chain).toLowerCase()] = entry
            }
          } catch (_) { }

          html += '<div class="d-flex flex-column gap-1">'
          for (const [platform, address] of Object.entries(res.contracts)) {
            const shortAddr = address.length > 20 ? address.substring(0, 10) + '...' + address.substring(address.length - 8) : address
            const direct = directByChain[String(platform).toLowerCase()] || null
            const dsHref = (direct && direct.dexscreener) ? direct.dexscreener : `https://dexscreener.com/search?q=${encodeURIComponent(address)}`
            const gtHref = (direct && direct.geckoterminal) ? direct.geckoterminal : `https://www.geckoterminal.com/search?q=${encodeURIComponent(address)}`
            html += `
            <div class="info-contract-row">
              <div class="d-flex align-items-center gap-2" style="min-width:0;">
                <span class="badge bg-info bg-opacity-25 text-info border border-info border-opacity-25">${platform.toUpperCase()}</span>
                <span class="info-contract-code" title="${address}">${shortAddr}</span>
              </div>
              <div class="d-flex gap-1 info-contract-actions">
                <button class="btn btn-sm btn-outline-light border-0" onclick="copyToClipboard('${address}')" title="Копировать адрес">
                  <i class="fas fa-copy fa-xs"></i>
                </button>
                <a href="${dsHref}" target="_blank" rel="noopener noreferrer" class="btn btn-sm btn-outline-warning border-0" title="DexScreener (${platform})">
                  <i class="fas fa-chart-line fa-xs"></i>
                </a>
                <a href="${gtHref}" target="_blank" rel="noopener noreferrer" class="btn btn-sm btn-outline-success border-0" title="GeckoTerminal (${platform})">
                  <i class="fas fa-chart-area fa-xs"></i>
                </a>
                <a href="https://debank.com/profile/${address}" target="_blank" rel="noopener noreferrer" class="btn btn-sm btn-outline-light border-0" title="Открыть в DeBank">
                  <i class="fas fa-search-dollar fa-xs"></i>
                </a>
              </div>
            </div>
          `
          }
          html += '</div>'
        } else {
          html += '<div class="text-muted small italic">Контракты не найдены. Возможно, это нативный коин или данных нет в CoinGecko.</div>'
        }

        if (res.platforms_map && Object.keys(res.platforms_map).length > 0) {
          html += '<div class="mt-2 small text-muted">Сети бирж: ' +
            Object.entries(res.platforms_map).map(([k, v]) => `<span class="text-info">${k}</span>→${v}`).join(', ') +
            '</div>'
        }

        html += '</div>'
        contractsEl.innerHTML = html
      }
    }
  } catch (e) {
    console.error('Error loading exchange statuses:', e)
    const fallback = `<span class="badge bg-secondary bg-opacity-25 text-white border border-secondary border-opacity-50">неизвестно</span>`
    const els = ['buyExDepositStatus', 'buyExWithdrawStatus', 'sellExDepositStatus', 'sellExWithdrawStatus']
    els.forEach(id => {
      const el = document.getElementById(id)
      if (el) el.innerHTML = fallback
    })
    try {
      const allEl = document.getElementById('infoAllExStatuses')
      if (allEl) allEl.innerHTML = `<div class="small text-muted">Статусы бирж: неизвестно</div>`
    } catch (_) { }
  }
}

// Загрузка полной информации о монете со всех бирж (аутентифицированные API)
async function loadFullCoinInfo(symbol) {
  try {
    const infoUrl = `/api/coin_full_info/${encodeURIComponent(symbol)}`
    const resp = await fetch(infoUrl, { signal: AbortSignal.timeout(20000) })
    const d = await resp.json()

    const fullInfoEl = document.getElementById('fullExchangeInfo')
    const dexLinksEl = document.getElementById('dexLinksPanel')

    if (!d || !d.success) {
      if (fullInfoEl) fullInfoEl.innerHTML = '<div class="text-muted small">Не удалось загрузить информацию</div>'
      return
    }

    // Группируем данные по биржам
    const byExchange = {}
    for (const item of (d.exchanges || [])) {
      const ex = item.exchange || 'Unknown'
      if (!byExchange[ex]) byExchange[ex] = []
      byExchange[ex].push(item)
    }

    // Отображаем статусы по каждой бирже
    if (fullInfoEl && Object.keys(byExchange).length > 0) {
      const escapeHtml = (s) => String(s == null ? '' : s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')

      const exKey = (name) => String(name || '').toLowerCase().replace(/[^a-z0-9]/g, '')

      const statusIcon = (v) => {
        if (v === true) return `<i class="fas fa-check-circle text-success"></i>`
        if (v === false) return `<i class="fas fa-times-circle text-danger"></i>`
        return `<i class="fas fa-question-circle text-secondary"></i>`
      }

      const pillHtml = (label, value) => {
        if (value === true) return `<span class="status-pill open"><i class="fas fa-check-circle"></i>${label}</span>`
        if (value === false) return `<span class="status-pill closed"><i class="fas fa-times-circle"></i>${label}</span>`
        return `<span class="status-pill unknown"><i class="fas fa-question-circle"></i>${label}</span>`
      }

      const summarizeBool = (rows, field) => {
        if (!Array.isArray(rows) || rows.length === 0) return null
        const values = rows.map(r => (r && Object.prototype.hasOwnProperty.call(r, field)) ? r[field] : null)
        if (values.some(v => v === true)) return true
        const hasNull = values.some(v => v === null || v === undefined)
        const hasFalse = values.some(v => v === false)
        if (hasFalse && !hasNull) return false
        return null
      }

      // Сортируем биржи в том же порядке, что и в модалке (если есть)
      let preferred = []
      try {
        if (Array.isArray(window.__coinInfoAllExchanges) && window.__coinInfoAllExchanges.length) {
          preferred = window.__coinInfoAllExchanges.map(x => exKey(x))
        }
      } catch (_) { }
      const orderMap = new Map(preferred.map((k, idx) => [k, idx]))
      const orderedEntries = Object.entries(byExchange).sort((a, b) => {
        const ak = exKey(a[0])
        const bk = exKey(b[0])
        const ai = orderMap.has(ak) ? orderMap.get(ak) : 9999
        const bi = orderMap.has(bk) ? orderMap.get(bk) : 9999
        if (ai !== bi) return ai - bi
        return String(a[0]).localeCompare(String(b[0]))
      })

      const buyK = (() => { try { return exKey(window.__coinInfoBuyExchange) } catch (_) { return '' } })()
      const sellK = (() => { try { return exKey(window.__coinInfoSellExchange) } catch (_) { return '' } })()

      let html = '<div class="d-flex flex-column gap-2">'
      for (const [exchange, chains] of orderedEntries) {
        const ek = exKey(exchange)
        const openAttr = (ek && (ek === buyK || ek === sellK)) ? ' open' : ''
        const depOverall = summarizeBool(chains, 'deposit_enabled')
        const wdOverall = summarizeBool(chains, 'withdraw_enabled')

        html += `<details class="asset-status-details"${openAttr}>`
        html += `<summary class="d-flex align-items-center justify-content-between gap-2">`
        html += `<span class="fw-bold">${escapeHtml(exchange)} <span class="text-muted small">(${chains.length})</span></span>`
        html += `<span class="d-flex flex-wrap gap-2 justify-content-end">${pillHtml('Ввод', depOverall)}${pillHtml('Вывод', wdOverall)}</span>`
        html += `</summary>`

        html += `<div class="table-responsive mt-2">`
        html += `<table class="table table-sm table-dark border-0 mb-0" style="font-size: 0.78rem;">`
        html += `<thead><tr><th>Сеть</th><th class="text-center">Ввод</th><th class="text-center">Вывод</th><th>Контракт</th></tr></thead>`
        html += `<tbody>`

        for (const chain of chains) {
          const chainName = escapeHtml(chain.chain || '-')
          const dep = (chain && Object.prototype.hasOwnProperty.call(chain, 'deposit_enabled')) ? chain.deposit_enabled : null
          const wd = (chain && Object.prototype.hasOwnProperty.call(chain, 'withdraw_enabled')) ? chain.withdraw_enabled : null
          const contract = chain.contract
          let contractHtml = '<span class="text-muted">—</span>'
          if (contract && String(contract).trim() && String(contract).toLowerCase() !== 'native') {
            const c = String(contract).trim()
            const short = c.length > 18 ? (c.slice(0, 8) + '...' + c.slice(-6)) : c
            const jsArg = JSON.stringify(c)
            contractHtml = `<code class="text-secondary" title="${escapeHtml(c)}" onclick="copyToClipboard(${jsArg})" style="cursor:pointer;">${escapeHtml(short)}</code>`
          } else if (contract && String(contract).toLowerCase() === 'native') {
            contractHtml = '<span class="text-muted">Native</span>'
          }

          html += `<tr>`
          html += `<td class="text-info">${chainName}</td>`
          html += `<td class="text-center">${statusIcon(dep)}</td>`
          html += `<td class="text-center">${statusIcon(wd)}</td>`
          html += `<td>${contractHtml}</td>`
          html += `</tr>`
        }

        html += `</tbody></table></div>`
        html += `</details>`
      }
      html += '</div>'

      fullInfoEl.innerHTML = `
        <details class="info-extra-details">
          <summary>Подробно по сетям и контрактам</summary>
          <div class="mt-2">${html}</div>
        </details>
      `
    } else if (fullInfoEl) {
      fullInfoEl.innerHTML = '<div class="text-muted small text-center py-3">Нет данных о статусах бирж</div>'
    }

    // Отображаем DEX ссылки
    if (dexLinksEl && d.dex_links && d.dex_links.length > 0) {
      let html = '<div class="d-flex flex-wrap gap-2 justify-content-center">'

      for (const link of d.dex_links) {
        const networkName = (link.network || '').toUpperCase()
        html += `
          <div class="btn-group btn-group-sm" role="group">
            <span class="btn btn-outline-secondary disabled">${networkName}</span>
            <a href="${link.geckoterminal}" target="_blank" class="btn btn-success" title="GeckoTerminal">
              <i class="fas fa-chart-line me-1"></i>GT
            </a>
            <a href="${link.dexscreener}" target="_blank" class="btn btn-primary" title="DexScreener">
              <i class="fas fa-search-dollar me-1"></i>DS
            </a>
          </div>
        `
      }

      html += '</div>'
      dexLinksEl.innerHTML = html
    } else if (dexLinksEl) {
      dexLinksEl.innerHTML = '<div class="text-muted small text-center">DEX ссылки недоступны (нативный токен или нет контракта)</div>'
    }

  } catch (e) {
    console.error('Error loading full coin info:', e)
    const fullInfoEl = document.getElementById('fullExchangeInfo')
    if (fullInfoEl) fullInfoEl.innerHTML = '<div class="text-danger small">Ошибка загрузки данных</div>'
  }
}

function exchangeKey(name) {
  return String(name || '').toLowerCase().replace(/[^a-z0-9]/g, '')
}

function getCoinInfoLivePollMs() {
  try {
    return Math.max(1000, getPollMsFor('opportunities'))
  } catch (_) {
    return 7000
  }
}

function stopCoinInfoLiveRefresh() {
  try {
    if (coinInfoLiveInterval) clearInterval(coinInfoLiveInterval)
  } catch (_) { }
  coinInfoLiveInterval = null
  coinInfoLiveInFlight = false
  try {
    if (window.__coinInfoLiveAbort) window.__coinInfoLiveAbort.abort()
  } catch (_) { }
  window.__coinInfoLiveAbort = null
}

function updateCoinInfoFreshnessBadge(ageSec, stale, snapshotTs) {
  const badge = document.getElementById('infoLiveAgeBadge')
  if (!badge) return
  const ageText = formatAgeSeconds(ageSec)
  const tsText = (typeof snapshotTs === 'number' && isFinite(snapshotTs) && snapshotTs > 0)
    ? new Date(snapshotTs * 1000).toLocaleTimeString()
    : '—'

  // If WS is connected and delivered a snapshot in the last 5s, call it "LIVE"
  // — this is the strongest possible freshness signal, visible even when
  // the /api/coin_arbitrage polling is slow.
  const wsLiveAge = window.__wsLiveLastSnapshotTs
    ? (Date.now() - window.__wsLiveLastSnapshotTs)
    : Infinity
  const wsIsLive = !!window.__wsLiveConnected && wsLiveAge < 5000

  if (wsIsLive) {
    badge.className = 'badge bg-success text-white'
    badge.innerHTML = `<i class="fas fa-bolt me-1"></i>LIVE · WS`
    badge.title = `WebSocket: live · ${new Date().toLocaleTimeString()}`
    return
  }

  if (ageText === '—') {
    badge.className = 'badge bg-secondary text-white'
    badge.textContent = 'Возраст: —'
  } else if (stale) {
    badge.className = 'badge bg-warning text-dark'
    badge.textContent = `НЕАКТУАЛЬНО · ${ageText}`
  } else {
    badge.className = 'badge bg-success text-white'
    badge.textContent = `Актуально · ${ageText}`
  }
  badge.title = `Снимок: ${tsText}`
}

function renderFastArbFromLive(payload, symbol) {
  try {
    const fastArbEl = document.getElementById('infoFastArb')
    if (!fastArbEl) return

    const list = Array.isArray(payload && payload.direct_opportunities)
      ? payload.direct_opportunities.slice(0, 5)
      : []
    if (!list.length) {
      fastArbEl.innerHTML = '<div class="text-muted small">Положительного арбитража по текущему снимку нет.</div>'
      return
    }

    const rows = list.map(o => {
      const buyEx = o.buy_exchange || '—'
      const sellEx = o.sell_exchange || '—'
      const buyP = (typeof o.buy_price === 'number' && isFinite(o.buy_price)) ? formatQuotePrice(o.buy_price) : '—'
      const sellP = (typeof o.sell_price === 'number' && isFinite(o.sell_price)) ? formatQuotePrice(o.sell_price) : '—'
      const spr = (typeof o.spread === 'number' && isFinite(o.spread)) ? `${o.spread.toFixed(2)}%` : '—'
      const buyUrl = getExchangeTradeUrl(buyEx, symbol)
      const sellUrl = getExchangeTradeUrl(sellEx, symbol)
      const buyHtml = buyUrl ? `<a href="${buyUrl}" target="_blank" rel="noopener noreferrer" class="link-light text-decoration-none">${buyEx}</a>` : buyEx
      const sellHtml = sellUrl ? `<a href="${sellUrl}" target="_blank" rel="noopener noreferrer" class="link-light text-decoration-none">${sellEx}</a>` : sellEx

      return `
        <tr>
          <td class="text-success">${buyHtml}</td>
          <td class="text-danger">${sellHtml}</td>
          <td class="text-muted">${buyP}</td>
          <td class="text-muted">${sellP}</td>
          <td class="text-success fw-bold">${spr}</td>
        </tr>
      `
    }).join('')

    fastArbEl.innerHTML = `
      <div class="table-responsive">
        <table class="table table-sm table-dark border-0 mb-0" style="font-size: 0.75rem;">
            <thead><tr><th>Покупка</th><th>Продажа</th><th>Цена входа</th><th>Цена выхода</th><th>Спред</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `
  } catch (_) { }
}

function renderCoinInfoLiveSnapshot(payload, ctx) {
  const panel = document.getElementById('infoLiveQuotes')
  if (!panel) return

  const pricesObj = (payload && payload.prices_by_exchange && typeof payload.prices_by_exchange === 'object')
    ? payload.prices_by_exchange
    : {}
  const knownExchanges = Array.isArray(ctx && ctx.exchanges) ? ctx.exchanges : []
  const all = []
  const seen = new Set()

  for (const name of knownExchanges) {
    const exName = String(name || '').trim()
    if (!exName) continue
    const k = exchangeKey(exName)
    if (seen.has(k)) continue
    seen.add(k)
    all.push({ name: exName, key: k, price: null })
  }
  for (const [name, val] of Object.entries(pricesObj)) {
    const exName = String(name || '').trim()
    if (!exName) continue
    const k = exchangeKey(exName)
    if (seen.has(k)) {
      const item = all.find(x => x.key === k)
      if (item) {
        item.name = exName
        item.price = (typeof val === 'number' && isFinite(val) && val > 0) ? val : null
      }
      continue
    }
    seen.add(k)
    all.push({
      name: exName,
      key: k,
      price: (typeof val === 'number' && isFinite(val) && val > 0) ? val : null,
    })
  }

  const buyK = exchangeKey(ctx && ctx.buyExchange)
  const sellK = exchangeKey(ctx && ctx.sellExchange)
  const sortRank = (k) => {
    if (k && k === buyK) return 0
    if (k && k === sellK) return 1
    return 2
  }
  all.sort((a, b) => {
    const ra = sortRank(a.key)
    const rb = sortRank(b.key)
    if (ra !== rb) return ra - rb
    return a.name.localeCompare(b.name)
  })

  const priceVals = all
    .map(x => (typeof x.price === 'number' && isFinite(x.price) && x.price > 0) ? x.price : null)
    .filter(v => v !== null)
  const minPrice = priceVals.length ? Math.min(...priceVals) : null
  const maxPrice = priceVals.length ? Math.max(...priceVals) : null

  const rows = all.map(item => {
    const url = getExchangeTradeUrl(item.name, ctx && ctx.symbol)
    const exHtml = url
      ? `<a href="${url}" target="_blank" rel="noopener noreferrer" class="link-light text-decoration-none">${item.name}</a>`
      : item.name
    const priceText = (item.price === null) ? '—' : formatQuotePrice(item.price)

    let rowStyle = ''
    if (item.price !== null && minPrice !== null && item.price === minPrice) rowStyle = 'background: rgba(40, 167, 69, 0.10);'
    if (item.price !== null && maxPrice !== null && item.price === maxPrice) rowStyle = 'background: rgba(220, 53, 69, 0.10);'

    const tags = []
    if (item.key && item.key === buyK) tags.push('<span class="badge bg-success bg-opacity-25 text-white border border-success border-opacity-50">Маршрут BUY</span>')
    if (item.key && item.key === sellK) tags.push('<span class="badge bg-danger bg-opacity-25 text-white border border-danger border-opacity-50">Маршрут SELL</span>')
    if (item.price !== null && minPrice !== null && item.price === minPrice) tags.push('<span class="badge bg-success text-white">Мин</span>')
    if (item.price !== null && maxPrice !== null && item.price === maxPrice) tags.push('<span class="badge bg-danger text-white">Макс</span>')

    return `
      <tr style="${rowStyle}">
        <td>${exHtml}</td>
        <td class="${item.price === null ? 'text-muted' : 'text-white fw-bold'}">${priceText}</td>
        <td class="text-end">${tags.join(' ') || '<span class="text-muted">—</span>'}</td>
      </tr>
    `
  }).join('')

  panel.innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm table-dark border-0 mb-0" style="font-size: 0.75rem;">
        <thead><tr><th>Биржа</th><th>Цена</th><th class="text-end">Метки</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="3" class="text-muted text-center">Нет цен в снимке</td></tr>'}</tbody>
      </table>
    </div>
  `

  const findPriceByExchange = (exchangeName) => {
    const k = exchangeKey(exchangeName)
    if (!k) return null
    const row = all.find(x => x.key === k)
    return row && typeof row.price === 'number' ? row.price : null
  }

  const buyLine = document.getElementById('buyExPriceLine')
  if (buyLine) {
    const p = findPriceByExchange(ctx && ctx.buyExchange)
    buyLine.textContent = `Покупка ${p !== null ? `@ ${formatQuotePrice(p)}` : '@ —'}`
  }
  const sellLine = document.getElementById('sellExPriceLine')
  if (sellLine) {
    const p = findPriceByExchange(ctx && ctx.sellExchange)
    sellLine.textContent = `Продажа ${p !== null ? `@ ${formatQuotePrice(p)}` : '@ —'}`
  }

  const ageSec = (payload && typeof payload.snapshot_age_sec === 'number' && isFinite(payload.snapshot_age_sec))
    ? payload.snapshot_age_sec
    : null
  const stale = !!(payload && payload.snapshot_stale)
  const snapshotTs = (payload && typeof payload.snapshot_ts === 'number' && isFinite(payload.snapshot_ts))
    ? payload.snapshot_ts
    : null
  updateCoinInfoFreshnessBadge(ageSec, stale, snapshotTs)
  renderFastArbFromLive(payload, ctx && ctx.symbol)
}

async function refreshCoinInfoLiveSnapshot(ctx) {
  if (!ctx || !ctx.symbol) return
  const modalEl = document.getElementById('coinInfoModal')
  if (!modalEl) return
  // Разрешаем самый первый запрос сразу после modal.show(), даже до полного fade-in.
  // Иначе при большом интервале (20с+) пользователю кажется, что блок "завис".
  const isVisible = modalEl.classList.contains('show')
  const hasLivePanel = !!document.getElementById('infoLiveQuotes')
  if (!isVisible && !hasLivePanel) return
  if (coinInfoLiveInFlight) return

  coinInfoLiveInFlight = true
  const currentSeq = ++coinInfoLiveRequestSeq
  const controller = new AbortController()
  try {
    if (window.__coinInfoLiveAbort) window.__coinInfoLiveAbort.abort()
  } catch (_) { }
  window.__coinInfoLiveAbort = controller
  const timeoutId = setTimeout(() => {
    try { controller.abort() } catch (_) { }
  }, 12000)

  try {
    const url = `/api/coin_arbitrage/${encodeURIComponent(ctx.symbol)}`
    const resp = await fetch(url, { signal: controller.signal })
    const data = await resp.json()
    if (currentSeq !== coinInfoLiveRequestSeq) return
    if (!data || !data.success) {
      const panel = document.getElementById('infoLiveQuotes')
      if (panel) panel.innerHTML = `<div class="small text-muted">Цены: нет данных (${data && data.error ? data.error : 'ошибка'})</div>`
      updateCoinInfoFreshnessBadge(null, true, null)
      return
    }
    renderCoinInfoLiveSnapshot(data, ctx)
  } catch (err) {
    const panel = document.getElementById('infoLiveQuotes')
    if (panel) panel.innerHTML = '<div class="small text-muted">Цены: таймаут/ошибка</div>'
    updateCoinInfoFreshnessBadge(null, true, null)
  } finally {
    clearTimeout(timeoutId)
    coinInfoLiveInFlight = false
  }
}

function startCoinInfoLiveRefresh(ctx) {
  stopCoinInfoLiveRefresh()
  if (!ctx || !ctx.symbol) return

  refreshCoinInfoLiveSnapshot(ctx)
  // Быстрый повтор на случай, если первый вызов пришёл в момент инициализации модалки.
  setTimeout(() => {
    refreshCoinInfoLiveSnapshot(ctx)
  }, 350)
  const pollMs = getCoinInfoLivePollMs()
  coinInfoLiveInterval = setInterval(() => {
    refreshCoinInfoLiveSnapshot(ctx)
  }, pollMs)
}

async function showCoinInfo(symbol, buyExchange, buyPrice, sellExchange, sellPrice, spread, buyVolume, sellVolume) {
  try { cleanupFloatingOverlays() } catch (_) { }
  try { stopCoinInfoLiveRefresh() } catch (_) { }
  console.log("Отображение информации о монете:", symbol);
  let allExchanges = []
  const warnCount = getWarnMarkCount(symbol)
  try {
    window.__coinInfoBuyExchange = buyExchange || ''
    window.__coinInfoSellExchange = sellExchange || ''
    window.__coinInfoPreferredCgUrl = null
    window.__coinInfoPreferredCgReason = null
  } catch (_) { }

  // 1. Инициализация модального окна
  const coinInfoModal = document.getElementById("coinInfoModal");
  if (coinInfoModal && typeof bootstrap !== "undefined") {
    const modal = bootstrap.Modal.getOrCreateInstance(coinInfoModal);
    try {
      coinInfoModal.addEventListener('shown.bs.modal', () => {
        try { pausePolling() } catch (_) { }
        try {
          const toggleBtn = document.getElementById('togglePollingBtn')
          if (toggleBtn) toggleBtn.textContent = 'Автообновление: ' + (window.__pollingPaused ? 'выкл' : 'вкл')
        } catch (_) { }
      }, { once: true });
      coinInfoModal.addEventListener('hidden.bs.modal', () => {
        try { stopCoinInfoLiveRefresh() } catch (_) { }
        try { resumePolling() } catch (_) { }
      }, { once: true });
    } catch (_) { }
    modal.show();
  }

  // 2. Установка заголовка
  const coinInfoModalTitle = document.getElementById("coinInfoModalTitle");
  if (coinInfoModalTitle) {
    const isFav = isFavorite(symbol);
    const favBtnClass = isFav ? 'btn-warning' : 'btn-outline-warning';
    const favIconClass = isFav ? 'fa-solid fa-star' : 'fa-regular fa-star';
    const { base: cgBase } = splitNormalizedPairSymbol(symbol)
    // Start with a CoinGecko search link as a placeholder; we upgrade it to a
    // direct `coins/{id}` URL once `token_resolution` arrives from the server.
    // We intentionally no longer use Google as a fallback for the "Сайт" button —
    // it is hidden until CoinGecko gives us a real homepage URL, or until we
    // have a contract address to link directly to DexScreener/GeckoTerminal.
    const cgSearchUrl = `https://www.coingecko.com/en/search?query=${encodeURIComponent(cgBase || symbol)}`

    coinInfoModalTitle.innerHTML = `
      <div class="d-flex justify-content-between align-items-center w-100">
        <div class="d-flex align-items-center gap-2">
          <span class="fw-bold">Инфо: ${symbol}</span>
          <a id="coinInfoCGLnk" href="${cgSearchUrl}" target="_blank" rel="noopener noreferrer" class="btn btn-sm btn-outline-success text-decoration-none"><i class="fas fa-external-link-alt me-1"></i>CoinGecko</a>
          <a id="coinInfoSiteLnk" href="#" target="_blank" rel="noopener noreferrer" class="btn btn-sm btn-outline-primary text-decoration-none" style="display:none;"><i class="fas fa-globe me-1"></i>Сайт</a>
          <a id="coinInfoDexLnk" href="#" target="_blank" rel="noopener noreferrer" class="btn btn-sm btn-outline-warning text-decoration-none" style="display:none;"><i class="fas fa-chart-line me-1"></i>DexScreener</a>
        </div>
        <div class="d-flex align-items-center gap-2">
          <button id="togglePollingBtn" class="btn btn-sm btn-outline-secondary">Автообновление: ${window.__pollingPaused ? 'выкл' : 'вкл'}</button>
          <div class="btn-group btn-group-sm">
            <button class="btn btn-sm ${favBtnClass}" onclick="toggleFavorite('${symbol}')">
              <i class="${favIconClass}"></i>
            </button>
          </div>
        </div>
      </div>`;

    const toggleBtn = document.getElementById('togglePollingBtn');
    if (toggleBtn) {
      toggleBtn.onclick = () => {
        if (window.__pollingPaused) resumePolling(); else pausePolling();
        toggleBtn.textContent = 'Автообновление: ' + (window.__pollingPaused ? 'выкл' : 'вкл');
      };
    }
  }

  // 3. Быстрая отрисовка и лоадер
  const modalBody = document.querySelector("#coinInfoModal .modal-body");
  if (modalBody) {
    modalBody.innerHTML = `
      <div id="infoFastContainer" class="mb-2">
        <div class="quick-overview-glass p-2 rounded-4 mb-2 animate__animated animate__fadeIn" style="background: rgba(255, 255, 255, 0.05); backdrop-filter: blur(15px); border: 1px solid rgba(255, 255, 255, 0.1); box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);">
          <div class="d-flex justify-content-between align-items-center mb-2 gap-2 flex-wrap">
            <div class="d-flex align-items-center gap-2">
              <div class="fw-bold text-white"><i class="fas fa-chart-line text-warning me-2"></i>${symbol}</div>
            </div>
            <div class="d-flex align-items-center gap-2 flex-wrap justify-content-end">
              <div class="btn-group btn-group-sm" role="group" aria-label="Blacklist actions">
                <button type="button" class="btn btn-outline-warning info-action-btn" onclick="addWarnMark('${symbol}')" title="Черная метка (проверял). Shift+клик: сброс">
                  <i class="fas fa-triangle-exclamation me-1"></i><span id="warnMarkBtnCount">${warnCount}</span>/${WARN_MARK_MAX}
                </button>
                <button type="button" class="btn btn-warning info-action-btn" onclick="addToBlacklistFromInfo('${symbol}', 24, false)" title="Убрать монету из списка на 24 часа">
                  <i class="fas fa-ban me-1"></i>24ч
                </button>
                <button type="button" class="btn btn-danger info-action-btn" onclick="addToBlacklistFromInfo('${symbol}', 0, true)" title="Убрать монету из списка навсегда">
                  <i class="fas fa-trash-alt me-1"></i>Навсегда
                </button>
              </div>
              <button class="btn btn-sm btn-outline-light border-0 opacity-50 hover-opacity-100" onclick="loadExchangeStatuses('${symbol}', '${buyExchange}', '${sellExchange}')" title="Обновить статусы">
                <i class="fas fa-sync-alt"></i>
              </button>
              <span class="badge bg-success text-white px-2 py-1" style="font-size: 0.85rem;">
                <i class="fas fa-percent me-1"></i>Спред: ${((typeof spread === 'number' && Number.isFinite(spread)) ? spread.toFixed(2) + '%' : (spread ? String(spread) : '—'))}
              </span>
            </div>
          </div>
          
          <div class="mb-2">
            <div class="small text-muted text-uppercase fw-bold mb-1" style="letter-spacing: 1px;">Биржи и ссылки</div>
            <div id="infoFastLinks" class="d-flex flex-wrap gap-2"></div>
          </div>

          <div class="mb-2">
            <div class="small text-muted text-uppercase fw-bold mb-1" style="letter-spacing: 1px;">Маршрут и статусы</div>
            <div id="infoFastRoute" class="p-2 rounded-3" style="background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.05);"></div>
          </div>

          <div class="mb-2">
            <div class="d-flex justify-content-between align-items-center mb-1">
              <div class="small text-muted text-uppercase fw-bold" style="letter-spacing: 1px;">Цены по биржам (live)</div>
              <span id="infoLiveAgeBadge" class="badge bg-secondary text-white">Возраст: —</span>
            </div>
            <div id="infoLiveQuotes" class="p-2 rounded-3" style="background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.04);">
              <div class="text-muted small"><span class="spinner-border spinner-border-sm me-2"></span>Загрузка цен...</div>
            </div>
          </div>

          <div class="mb-2">
            <div class="small text-muted text-uppercase fw-bold mb-1" style="letter-spacing: 1px;">Арбитраж (куда покупать/продавать)</div>
            <div id="infoFastArb" class="p-2 rounded-3" style="background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.04);">
              <div class="text-muted small"><span class="spinner-border spinner-border-sm me-2"></span>Считаю...</div>
            </div>
          </div>

          <details id="infoInfraDetails" class="info-extra-details mb-2">
            <summary class="small text-muted">Дополнительно: статусы, DEX, сети, стаканы</summary>
            <div class="mt-2">
              <div class="mb-2">
                <div class="small text-muted text-uppercase fw-bold mb-1" style="letter-spacing: 1px;">Ввод/вывод по биржам</div>
                <div id="infoAllExStatuses" class="table-responsive">
                  <div class="text-center p-2"><span class="spinner-border spinner-border-sm text-primary"></span></div>
                </div>
              </div>

              <div id="infoFastContracts" class="mb-2"></div>

              <div class="row g-2 mb-2">
                <div class="col-md-6">
                  <div class="small text-muted text-uppercase fw-bold mb-1" style="letter-spacing: 1px;"><i class="fas fa-link text-primary me-2"></i>DEX ссылки</div>
                  <div id="dexLinksPanel" class="p-1 rounded-3" style="background: rgba(0,0,0,0.2);">
                    <div class="text-center py-2"><span class="spinner-border spinner-border-sm text-primary"></span></div>
                  </div>
                </div>
                <div class="col-md-6">
                  <div class="small text-muted text-uppercase fw-bold mb-1" style="letter-spacing: 1px;"><i class="fas fa-university text-info me-2"></i>Статус на всех биржах</div>
                  <div id="fullExchangeInfo" class="rounded-3" style="background: rgba(0,0,0,0.2);">
                    <div class="text-center p-2"><span class="spinner-border spinner-border-sm text-primary"></span></div>
                  </div>
                </div>
              </div>
              
              <div id="infoFastOrderbooks" class="mb-2"></div>
              
              <div id="infoFastStatus" class="text-center p-1">
                 <div class="spinner-border spinner-border-sm text-primary" role="status"></div>
                 <span class="ms-2 small text-muted">Загрузка глубокой аналитики...</span>
              </div>
            </div>
          </details>
        </div>
      </div>
      <div id="infoDetailedContent" class="animate__animated animate__fadeIn" style="display:none;"></div>
    `;

    // Fast section: show exchange links + route immediately, then fetch orderbooks in background.
    try {
      const exSet = new Set()
      if (buyExchange) exSet.add(String(buyExchange))
      if (sellExchange) exSet.add(String(sellExchange))
      try {
        if (Array.isArray(opportunities)) {
          for (const o of opportunities) {
            if (!o || !o.symbol) continue
            if (String(o.symbol).toUpperCase() !== String(symbol).toUpperCase()) continue
            if (o.buy_exchange) exSet.add(String(o.buy_exchange))
            if (o.sell_exchange) exSet.add(String(o.sell_exchange))
          }
        }
      } catch (_) { }

      const exchanges = Array.from(exSet)
      allExchanges = exchanges
      try { window.__coinInfoAllExchanges = exchanges } catch (_) { }
      const linksEl = document.getElementById('infoFastLinks')
      if (linksEl) {
        linksEl.innerHTML = buildExchangeLinksHtml(symbol, exchanges, buyExchange, sellExchange)
        hydrateBinanceAlphaLinks(symbol)
      }

      const routeEl = document.getElementById('infoFastRoute')
      if (routeEl) {
        const bp = (typeof buyPrice === 'number' && isFinite(buyPrice)) ? buyPrice : null
        const sp = (typeof sellPrice === 'number' && isFinite(sellPrice)) ? sellPrice : null
        // Начальный HTML с лоадером для статусов
        routeEl.innerHTML = `
          <div class="d-flex flex-column gap-2">
            <div class="d-flex align-items-center justify-content-between bg-dark bg-opacity-50 p-1 rounded-3 border border-success border-opacity-10">
              <div class="d-flex align-items-center gap-3">
                <div class="bg-success text-white rounded-circle d-flex align-items-center justify-content-center" style="width: 26px; height: 26px;">
                  <i class="fas fa-shopping-cart fa-sm"></i>
                </div>
                <div>
                  <div class="fw-bold text-success" style="font-size: 1rem;">${buyExchange || '-'}</div>
                  <div id="buyExPriceLine" class="small text-muted">Покупка ${bp !== null ? `@ ${bp.toFixed(8)}` : '@ —'}</div>
                </div>
              </div>
              <div class="text-end d-flex flex-column gap-1 align-items-end">
                <div id="buyExDepositStatus"><div class="spinner-border spinner-border-sm text-muted"></div></div>
                <div id="buyExWithdrawStatus"><div class="spinner-border spinner-border-sm text-muted"></div></div>
              </div>
            </div>

            <div class="d-flex align-items-center justify-content-center" style="z-index: 1;">
              <div class="bg-primary rounded-circle d-flex align-items-center justify-content-center shadow" style="width: 20px; height: 20px; border: 2px solid #1a1a1a;">
                <i class="fas fa-arrow-down fa-xs text-white"></i>
              </div>
            </div>

            <div class="d-flex align-items-center justify-content-between bg-dark bg-opacity-50 p-1 rounded-3 border border-danger border-opacity-10">
              <div class="d-flex align-items-center gap-3">
                <div class="bg-danger text-white rounded-circle d-flex align-items-center justify-content-center" style="width: 26px; height: 26px;">
                  <i class="fas fa-hand-holding-usd fa-sm"></i>
                </div>
                <div>
                  <div class="fw-bold text-danger" style="font-size: 1rem;">${sellExchange || '-'}</div>
                  <div id="sellExPriceLine" class="small text-muted">Продажа ${sp !== null ? `@ ${sp.toFixed(8)}` : '@ —'}</div>
                </div>
              </div>
              <div class="text-end d-flex flex-column gap-1 align-items-end">
                <div id="sellExDepositStatus"><div class="spinner-border spinner-border-sm text-muted"></div></div>
                <div id="sellExWithdrawStatus"><div class="spinner-border spinner-border-sm text-muted"></div></div>
              </div>
            </div>
          </div>
        `
        // Сразу грузим только быстрые статусы для маршрута (без контрактов/тяжёлых блоков).
        loadExchangeStatuses(symbol, buyExchange, sellExchange, false)
      }

      startCoinInfoLiveRefresh({
        symbol,
        buyExchange,
        sellExchange,
        exchanges,
      })

      // Тяжёлые данные грузим лениво — только когда пользователь раскрывает доп. блок.
      try {
        const infraDetails = document.getElementById('infoInfraDetails')
        let infraLoaded = false
        const ensureInfraLoaded = () => {
          if (infraLoaded) return
          infraLoaded = true
          loadExchangeStatuses(symbol, buyExchange, sellExchange, true)
          loadFullCoinInfo(symbol)
        }
        if (infraDetails) {
          infraDetails.addEventListener('toggle', () => {
            if (infraDetails.open) ensureInfraLoaded()
          })
          if (infraDetails.open) ensureInfraLoaded()
        }
      } catch (_) { }

      const obEl = document.getElementById('infoFastOrderbooks')
      if (obEl) obEl.innerHTML = `<div class="small text-muted"><span class="spinner-border spinner-border-sm me-2"></span>Стаканы (все биржи)...</div>`

      try {
        if (window.__coinInfoObAbort) { try { window.__coinInfoObAbort.abort() } catch (_) { } }
        const obController = new AbortController()
        window.__coinInfoObAbort = obController
        const timeoutId = setTimeout(() => { try { obController.abort() } catch (_) { } }, 12000)

        const wantedList = (Array.isArray(allExchanges) && allExchanges.length)
          ? allExchanges
          : [buyExchange, sellExchange]

        const wanted = wantedList
          .filter(Boolean)
          .map(x => encodeURIComponent(String(x)))
          .join(',')

        const obUrl = wanted
          ? `/api/orderbooks/${symbol}?exchanges=${wanted}`
          : `/api/orderbooks/${symbol}`
        fetch(obUrl, { signal: obController.signal })
          .then(r => r.json())
          .then(d => {
            try { clearTimeout(timeoutId) } catch (_) { }
            const el = document.getElementById('infoFastOrderbooks')
            if (!el) return
            if (!d || !d.success || !Array.isArray(d.data) || d.data.length === 0) {
              el.innerHTML = `<div class="small text-muted">Стаканы: нет данных</div>`
              return
            }
            let bestBid = null
            let bestAsk = null
            for (const x of d.data) {
              const bidNum = (typeof x.bid === 'number' && isFinite(x.bid)) ? x.bid : null
              const askNum = (typeof x.ask === 'number' && isFinite(x.ask)) ? x.ask : null
              if (bidNum !== null) bestBid = (bestBid === null) ? bidNum : Math.max(bestBid, bidNum)
              if (askNum !== null) bestAsk = (bestAsk === null) ? askNum : Math.min(bestAsk, askNum)
            }

            const rows = d.data.map(x => {
              const exName = x.exchange || ''
              const exUrl = getExchangeTradeUrl(exName, symbol)
              const exHtml = exUrl
                ? `<a href="${exUrl}" target="_blank" rel="noopener noreferrer" class="link-light text-decoration-none">${exName}</a>`
                : exName
              const bidNum = (typeof x.bid === 'number' && isFinite(x.bid)) ? x.bid : null
              const askNum = (typeof x.ask === 'number' && isFinite(x.ask)) ? x.ask : null
              const bid = (bidNum !== null) ? bidNum.toFixed(8) : '—'
              const ask = (askNum !== null) ? askNum.toFixed(8) : '—'
              const spr = (typeof x.spread_percent === 'number' && isFinite(x.spread_percent)) ? x.spread_percent.toFixed(2) + '%' : '—'

              const isBestBid = (bestBid !== null && bidNum !== null && bidNum === bestBid)
              const isBestAsk = (bestAsk !== null && askNum !== null && askNum === bestAsk)

              let rowStyle = ''
              if (isBestBid && isBestAsk) rowStyle = 'background: rgba(13, 110, 253, 0.10);'
              else if (isBestAsk) rowStyle = 'background: rgba(40, 167, 69, 0.10);'
              else if (isBestBid) rowStyle = 'background: rgba(220, 53, 69, 0.10);'

              let rowTitle = ''
              if (isBestAsk) rowTitle += 'ЛУЧШИЙ ASK (покупка). '
              if (isBestBid) rowTitle += 'ЛУЧШИЙ BID (продажа). '
              rowTitle = rowTitle.trim()

              const bidCls = `text-success${isBestBid ? ' fw-bold' : ''}`
              const askCls = `text-danger${isBestAsk ? ' fw-bold' : ''}`
              return `
                <tr${rowTitle ? ` title="${rowTitle}"` : ''} style="${rowStyle}">
                  <td>${exHtml}</td>
                  <td class="${bidCls}">${bid}</td>
                  <td class="${askCls}">${ask}</td>
                  <td class="text-muted">${spr}</td>
                </tr>
              `
            }).join('')
            el.innerHTML = `
              <div class="table-responsive">
                <table class="table table-sm table-dark border-0 mb-0" style="font-size: 0.75rem;">
                  <thead><tr><th>Биржа</th><th>Bid</th><th>Ask</th><th>Спред</th></tr></thead>
                  <tbody>${rows}</tbody>
                </table>
              </div>
            `
          })
          .catch(() => {
            try { clearTimeout(timeoutId) } catch (_) { }
            const el = document.getElementById('infoFastOrderbooks')
            if (el) el.innerHTML = `<div class="small text-muted">Стаканы: таймаут</div>`
          })
      } catch (_) { }
    } catch (_) { }
  }

  // 4. Запрос данных через консолидированный API
  try {
    const exParam = allExchanges && allExchanges.length ? `&exchanges=${encodeURIComponent(allExchanges.join(','))}` : ''
    const apiUrl = `/api/full_coin_info/${encodeURIComponent(symbol)}?buy_ex=${encodeURIComponent(buyExchange || '')}&sell_ex=${encodeURIComponent(sellExchange || '')}&buy_p=${encodeURIComponent(buyPrice ?? '')}&sell_p=${encodeURIComponent(sellPrice ?? '')}&buy_volume=${encodeURIComponent(buyVolume ?? '')}&sell_volume=${encodeURIComponent(sellVolume ?? '')}${exParam}`;

    const controller = new AbortController();
    try {
      if (window.__coinInfoFullAbort) window.__coinInfoFullAbort.abort()
    } catch (_) { }
    window.__coinInfoFullAbort = controller;
    const timeoutMs = 25000;
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

    let data;
    try {
      const resp = await fetch(apiUrl, { signal: controller.signal });
      data = await resp.json();
    } finally {
      clearTimeout(timeoutId);
    }

    if (!data.success) throw new Error(data.error || "Ошибка загрузки данных");

    renderDetailedCoinInfo(data);
  } catch (error) {
    try {
      const fastStatus = document.getElementById('infoFastStatus')
      if (fastStatus) {
        fastStatus.innerHTML = `<div class="small text-muted">Детали не загрузились (таймаут/ошибка). Ссылки и стаканы выше работают.</div>`
      }
    } catch (_) { }
    console.error("Ошибка при загрузке информации:", error);
    if (modalBody) {
      const isAbort = error && (error.name === 'AbortError' || String(error).includes('AbortError'));
      const msg = isAbort
        ? 'Таймаут запроса (сервер/биржи отвечают слишком долго).'
        : (error.message || 'Неизвестная ошибка');
      const { base: cgBase } = splitNormalizedPairSymbol(symbol)
      const cg = `https://www.coingecko.com/en/search?query=${encodeURIComponent(cgBase || symbol)}`;
      modalBody.innerHTML += `<div class="alert alert-danger mx-3 d-flex justify-content-between align-items-center gap-2">\n        <span>Не удалось загрузить полные данные: ${msg}</span>\n        <a href="${cg}" target="_blank" rel="noopener noreferrer" class="btn btn-sm btn-outline-light">Открыть CoinGecko</a>\n      </div>`;
    }
  }
}

function renderDetailedCoinInfo(data) {
  const container = document.getElementById('infoDetailedContent');
  if (!container) return;
  container.style.display = 'block';
  try { window.__coinInfoLastData = data } catch (_) { }
  try {
    const fastStatus = document.getElementById('infoFastStatus')
    if (fastStatus) fastStatus.style.display = 'none'
  } catch (_) { }

  const { symbol, coin_data, chart, orderbooks, arbitrage } = data;

  const renderArbTable = (arb, limit = 12) => {
    const allPairs = (arb && Array.isArray(arb.pairs)) ? arb.pairs : []
    const list = allPairs.slice(0, limit)
    const missing = (arb && Array.isArray(arb.missing)) ? arb.missing : []
    const filtered = (arb && typeof arb.filtered_count === 'number') ? arb.filtered_count : 0
    const notes = []
    if (allPairs.length > list.length) notes.push(`Показано: ${list.length} из ${allPairs.length}`)
    if (missing.length) notes.push(`Нет стакана: ${missing.join(', ')}`)
    if (filtered > 0) notes.push(`Скрыто фильтром ввода/вывода: ${filtered}`)

    if (!list.length) {
      const extra = notes.length ? `<div class="small text-muted mt-1">${notes.join(' • ')}</div>` : ''
      return `<div class="text-muted small">Арбитраж не найден (по стаканам).</div>${extra}`
    }

    const rows = list.map(o => {
      const buyEx = o.buy_exchange || '—'
      const sellEx = o.sell_exchange || '—'
      const buyP = (typeof o.buy_price === 'number' && isFinite(o.buy_price)) ? o.buy_price.toFixed(8) : '—'
      const sellP = (typeof o.sell_price === 'number' && isFinite(o.sell_price)) ? o.sell_price.toFixed(8) : '—'
      const sprVal = (typeof o.spread === 'number' && isFinite(o.spread)) ? o.spread : null
      const spr = (sprVal !== null) ? sprVal.toFixed(2) + '%' : '—'
      const net = (typeof o.net_spread === 'number' && isFinite(o.net_spread)) ? o.net_spread.toFixed(2) + '%' : spr

      const buyUrl = getExchangeTradeUrl(buyEx, symbol)
      const sellUrl = getExchangeTradeUrl(sellEx, symbol)
      const buyHtml = buyUrl ? `<a href="${buyUrl}" target="_blank" rel="noopener noreferrer" class="link-light text-decoration-none">${buyEx}</a>` : buyEx
      const sellHtml = sellUrl ? `<a href="${sellUrl}" target="_blank" rel="noopener noreferrer" class="link-light text-decoration-none">${sellEx}</a>` : sellEx

      const sprCls = (sprVal !== null && sprVal >= 0) ? 'text-success fw-bold' : 'text-danger'

      return `
        <tr>
          <td class="text-success">${buyHtml}</td>
          <td class="text-danger">${sellHtml}</td>
          <td class="text-muted">${buyP}</td>
          <td class="text-muted">${sellP}</td>
          <td class="${sprCls}">${spr}</td>
          <td class="text-muted">${net}</td>
        </tr>
      `
    }).join('')

    const noteHtml = notes.length ? `<div class="small text-muted mb-1">${notes.join(' • ')}</div>` : ''
    return `
      ${noteHtml}
      <div class="table-responsive">
        <table class="table table-sm table-dark border-0 mb-0" style="font-size: 0.75rem;">
            <thead><tr><th>Покупка</th><th>Продажа</th><th>Цена входа</th><th>Цена выхода</th><th>Спред</th><th>Чистая $</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `
  }

  const exKey = (name) => String(name || '').toLowerCase().replace(/[^a-z0-9]/g, '')
  const summarizeBool = (rows, field) => {
    if (!Array.isArray(rows) || rows.length === 0) return null
    const values = rows.map(r => (r && Object.prototype.hasOwnProperty.call(r, field)) ? r[field] : null)
    if (values.some(v => v === true)) return true
    const hasNull = values.some(v => v === null || v === undefined)
    const hasFalse = values.some(v => v === false)
    if (hasFalse && !hasNull) return false
    return null
  }

  const buildStatusMap = () => {
    const status = (() => { try { return window.__coinInfoAssetStatus } catch (_) { return null } })()
    const items = Array.isArray(status && status.data) ? status.data : []
    const byKey = {}
    for (const row of items) {
      const k = exKey(row.exchange)
      if (!k) continue
      if (!byKey[k]) byKey[k] = []
      byKey[k].push(row)
    }
    const map = {}
    for (const [k, rows] of Object.entries(byKey)) {
      map[k] = {
        deposit: summarizeBool(rows, 'deposit_enabled'),
        withdraw: summarizeBool(rows, 'withdraw_enabled'),
      }
    }
    return map
  }

  const buildArbPairs = (obs, contextExchanges) => {
    const list = Array.isArray(obs) ? obs : []
    const obMap = {}
    for (const o of list) {
      const k = exKey(o.exchange)
      if (!k) continue
      const bid = (typeof o.bid === 'number' && isFinite(o.bid)) ? o.bid : null
      const ask = (typeof o.ask === 'number' && isFinite(o.ask)) ? o.ask : null
      if (bid === null || ask === null) continue
      obMap[k] = { exchange: o.exchange, bid, ask, bid_volume: o.bid_volume, ask_volume: o.ask_volume }
    }

    const ctx = Array.isArray(contextExchanges) ? contextExchanges : []
    const ctxKeys = ctx.map(exKey).filter(Boolean)
    const allKeys = ctxKeys.length ? ctxKeys : Object.keys(obMap)
    const missing = (ctxKeys.length ? ctx : []).filter(ex => !obMap[exKey(ex)])

    const statusMap = buildStatusMap()
    const filterTransfer = !!(settings && settings.ui_arb_filter_transfer)

    const pairs = []
    let filteredCount = 0
    for (let i = 0; i < allKeys.length; i++) {
      for (let j = 0; j < allKeys.length; j++) {
        if (i === j) continue
        const buyK = allKeys[i]
        const sellK = allKeys[j]
        const buy = obMap[buyK]
        const sell = obMap[sellK]
        if (!buy || !sell) continue

        const spread = ((sell.bid - buy.ask) / buy.ask) * 100
        if (!isFinite(spread)) continue

        if (filterTransfer) {
          const buySt = statusMap[buyK] || {}
          const sellSt = statusMap[sellK] || {}
          if (buySt.withdraw === false || sellSt.deposit === false) {
            filteredCount += 1
            continue
          }
        }

        pairs.push({
          buy_exchange: buy.exchange,
          sell_exchange: sell.exchange,
          buy_price: buy.ask,
          sell_price: sell.bid,
          spread: spread,
          net_spread: spread,
        })
      }
    }

    pairs.sort((a, b) => b.spread - a.spread)
    return { pairs, missing, filtered_count: filteredCount }
  }

  // Быстрый блок арбитража обновляется live-слоем из /api/coin_arbitrage,
  // здесь не перерисовываем его повторно, чтобы не дублировать данные.

  let html = '';

  const buildCgBlock = (cd) => {
    let out = ''
    try {
      const { base: cgBase } = splitNormalizedPairSymbol(symbol)
      const preferredCg = (() => {
        try { return window.__coinInfoPreferredCgUrl || '' } catch (_) { return '' }
      })()
      const cgUrl = preferredCg
        ? preferredCg
        : (cd?.id
          ? `https://www.coingecko.com/en/coins/${encodeURIComponent(cd.id)}`
          : `https://www.coingecko.com/en/search?query=${encodeURIComponent(cgBase || symbol)}`);

      try {
        const headerCg = document.getElementById('coinInfoCGLnk')
        if (headerCg && cgUrl) headerCg.href = cgUrl
      } catch (_) { }

      const homeUrl = Array.isArray(cd?.links?.homepage)
        ? (cd.links.homepage.find(u => typeof u === 'string' && u.trim()) || '')
        : '';

      try {
        const headerSite = document.getElementById('coinInfoSiteLnk')
        if (headerSite && homeUrl) headerSite.href = homeUrl
      } catch (_) { }

      if (cd && !cd.error) {
        out += `
        <div class="info-main-card mb-3 animate__animated animate__fadeInUp" style="background: rgba(255, 255, 255, 0.03); border-radius: 16px; border: 1px solid rgba(255, 255, 255, 0.08); overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.2);">
           <div class="d-flex align-items-center p-3" style="background: rgba(255,255,255,0.03); border-bottom: 1px solid rgba(255,255,255,0.05);">
              <img src="${cd.image?.large || cd.image?.small || ''}" width="48" height="48" class="rounded-circle me-3 shadow-sm" style="border: 2px solid rgba(255,255,255,0.1);">
              <div class="flex-grow-1">
                 <div class="d-flex justify-content-between align-items-center">
                    <h5 class="mb-0 fw-bold text-white">${cd.name} <span class="badge bg-secondary bg-opacity-50 ms-2" style="font-size: 0.7rem;">RANK #${cd.market_cap_rank || '—'}</span></h5>
                    <div class="text-end">
                      <div class="fw-bold text-white" style="font-size: 1.2rem;">$${cd.market_data?.current_price?.usd?.toLocaleString() || '—'}</div>
                      <div class="small ${cd.market_data?.price_change_percentage_24h >= 0 ? 'text-success' : 'text-danger'} fw-bold">
                        <i class="fas fa-caret-${cd.market_data?.price_change_percentage_24h >= 0 ? 'up' : 'down'} me-1"></i>
                        ${cd.market_data?.price_change_percentage_24h?.toFixed(2)}% (24ч)
                      </div>
                    </div>
                 </div>
              </div>
           </div>
           <div class="p-3">
              <div class="row g-3">
                 <div class="col-6 col-md-3">
                    <div class="small text-muted mb-1">Market Cap</div>
                    <div class="fw-bold text-white-50">$${formatNumberShort(cd.market_data?.market_cap?.usd)}</div>
                 </div>
                 <div class="col-6 col-md-3">
                    <div class="small text-muted mb-1">Volume 24h</div>
                    <div class="fw-bold text-white-50">$${formatNumberShort(cd.market_data?.total_volume?.usd)}</div>
                 </div>
                 <div class="col-6 col-md-3">
                    <div class="small text-muted mb-1">ATH</div>
                    <div class="fw-bold text-white-50">$${cd.market_data?.ath?.usd || '—'}</div>
                 </div>
                 <div class="col-6 col-md-3">
                    <div class="small text-muted mb-1">ATL</div>
                    <div class="fw-bold text-white-50">$${cd.market_data?.atl?.usd || '—'}</div>
                 </div>
              </div>
           </div>
        </div>`;
      } else {
        const msg = (cd && cd.error) ? String(cd.error) : 'loading'
        out += `<div class="small text-muted mb-2"><span class="spinner-border spinner-border-sm me-2"></span>CoinGecko: ${msg}...</div>`
      }
    } catch (_) { }
    return out
  }

  const extraCg = `<div id="cgBlock">${buildCgBlock(coin_data)}</div>`
  const extraCalc = renderProfitCalculator(data)
  const extraChart = `
    <div class="card mb-2 bg-transparent border-white-5">
      <div class="card-header py-1 px-2 d-flex justify-content-between align-items-center border-white-5">
        <span class="fw-bold small">Динамика цены (24ч)</span>
        <small class="text-muted opacity-50" style="font-size: 0.65rem;">CG chart</small>
      </div>
      <div class="card-body p-2" style="height: 110px;">
        <canvas id="infoCoinChart"></canvas>
      </div>
    </div>
  `
  html += `
    <details id="infoExtraDetails" class="info-extra-details mb-2">
      <summary class="small text-muted">Аналитика (CoinGecko / график / калькулятор)</summary>
      <div class="mt-2">
        ${extraCg}
        ${extraChart}
        ${extraCalc}
      </div>
    </details>
  `

  container.innerHTML = html;

  // CoinGecko: если не успели в full_coin_info — догружаем отдельно и обновляем только CG-блок.
  try {
    const needsCg = (!coin_data || coin_data.error)
    const ctx = (data && data.context) ? data.context : {}
    if (needsCg && ctx) {
      if (window.__coinInfoCgAbort) { try { window.__coinInfoCgAbort.abort() } catch (_) { } }
      const c = new AbortController()
      window.__coinInfoCgAbort = c
      const tId = setTimeout(() => { try { c.abort() } catch (_) { } }, 22000)

      const ex = Array.isArray(ctx.exchanges) ? ctx.exchanges : []
      const exParam = ex.length ? `&exchanges=${encodeURIComponent(ex.join(','))}` : ''
      const url = `/api/coingecko_info/${encodeURIComponent(symbol)}?buy_ex=${encodeURIComponent(ctx.buy_ex || '')}&sell_ex=${encodeURIComponent(ctx.sell_ex || '')}&buy_volume=${encodeURIComponent(ctx.buy_volume ?? '')}&sell_volume=${encodeURIComponent(ctx.sell_volume ?? '')}&buy_p=${encodeURIComponent(ctx.buy_p ?? '')}&sell_p=${encodeURIComponent(ctx.sell_p ?? '')}${exParam}`

      fetch(url, { signal: c.signal })
        .then(r => r.json())
        .then(cd => {
          try { clearTimeout(tId) } catch (_) { }
          if (!cd || cd.error) return
          const block = document.getElementById('cgBlock')
          if (block) block.innerHTML = buildCgBlock(cd)
        })
        .catch(() => { try { clearTimeout(tId) } catch (_) { } })
    }
  } catch (_) { }

  // График: рендерим только когда пользователь открыл "Дополнительно" (чтобы не ломать размер canvas и не грузить лишнее)
  try {
    const details = document.getElementById('infoExtraDetails')
    const ensureChart = () => {
      if (details && !details.open) return
      const canvas = document.getElementById('infoCoinChart')
      if (!canvas) return
      if (canvas.dataset && canvas.dataset.rendered === '1') return
      if (canvas.dataset) canvas.dataset.rendered = '1'

      const hasPrices = chart && Array.isArray(chart.prices) && chart.prices.length > 0
      if (hasPrices) {
        setTimeout(() => renderMiniChart('infoCoinChart', chart), 10)
        return
      }

      if (window.__coinInfoChartAbort) { try { window.__coinInfoChartAbort.abort() } catch (_) { } }
      const c = new AbortController()
      window.__coinInfoChartAbort = c
      const tId = setTimeout(() => { try { c.abort() } catch (_) { } }, 20000)
      const url = `/api/coingecko_chart/${encodeURIComponent(symbol)}?days=1`
      fetch(url, { signal: c.signal })
        .then(r => r.json())
        .then(d => {
          try { clearTimeout(tId) } catch (_) { }
          if (!d || !d.success || !Array.isArray(d.prices) || d.prices.length === 0) return
          renderMiniChart('infoCoinChart', { prices: d.prices, ladder: Array.isArray(d.ladder) ? d.ladder : [] })
        })
        .catch(() => { try { clearTimeout(tId) } catch (_) { } })
    }

    if (details) {
      details.addEventListener('toggle', () => {
        if (details.open) ensureChart()
      })
    }
    ensureChart()
  } catch (_) { }

  // Инициализация логики калькулятора
  initCalculatorLogic(data);
}

function renderProfitCalculator(data) {
  const { arbitrage, orderbooks } = data;
  const bestOpp = arbitrage.direct_opportunities?.[0] || null;

  return `
    <div class="calc-container mb-2">
      <div class="d-flex justify-content-between align-items-center mb-2">
        <span class="small fw-bold text-accent"><i class="fas fa-calculator me-2"></i>Симулятор прибыли</span>
        <span class="badge bg-primary bg-opacity-10 text-primary small">С учетом стаканов</span>
      </div>

      <div class="row g-2 align-items-end mb-2">
        <div class="col-7">
          <div class="calc-input-group mb-0">
            <label>Сумма сделки (USDT)</label>
            <input type="number" id="calcAmount" class="calc-input" value="100" min="10" step="10">
          </div>
        </div>
        <div class="col-5">
           <div class="calc-input-group mb-0">
            <label>Комиссия (%)</label>
            <input type="number" id="calcFee" class="calc-input" value="0.1" min="0" step="0.05">
          </div>
        </div>
      </div>

      <div class="calc-result-card">
         <div class="calc-result-item">
            <span class="text-dim">Покупка на:</span>
            <span id="calcBuyEx" class="calc-result-val text-success">${bestOpp?.buy_exchange || '—'}</span>
         </div>
         <div class="calc-result-item">
            <span class="text-dim">Продажа на:</span>
            <span id="calcSellEx" class="calc-result-val text-danger">${bestOpp?.sell_exchange || '—'}</span>
         </div>
          <div class="calc-profit-main">
             <div class="small text-dim mb-1">Ожидаемая прибыль (чистыми)</div>
             <div id="calcProfitResult" class="fw-bold fs-5 text-success">$0.00</div>
             <div id="calcProfitPct" class="small text-muted">0.00%</div>
          </div>
       </div>
      
      <div id="calcWarning" class="alert alert-warning py-1 px-2 mt-2 small" style="display:none; font-size: 0.65rem;">
         <i class="fas fa-exclamation-triangle me-1"></i> Недостаточно ликвидности для такого объема!
      </div>
    </div>
  `;
}

function initCalculatorLogic(data) {
  const amountInput = document.getElementById('calcAmount');
  const feeInput = document.getElementById('calcFee');

  if (!amountInput || !feeInput) return;

  const update = () => {
    const amount = parseFloat(amountInput.value) || 0;
    const feePct = (parseFloat(feeInput.value) || 0) / 100;

    // Берем лучшую возможность
    const opps = data.arbitrage.direct_opportunities;
    if (!opps || opps.length === 0) {
      document.getElementById('calcProfitResult').textContent = "—";
      document.getElementById('calcProfitPct').textContent = "Нет возможностей";
      return;
    }

    const best = opps[0];
    const buyPrice = best.buy_price;
    const sellPrice = best.sell_price;

    // Грубый расчет (без полного прохода по стакану, т.к. у нас сейчас только топ в API, 
    // но в будущем можно расширить). Пока считаем по топу с учетом комиссии.

    const totalBuy = amount;
    const coins = (totalBuy * (1 - feePct)) / buyPrice;
    const totalSell = (coins * sellPrice) * (1 - feePct);
    const profit = totalSell - totalBuy;
    const profitPct = (profit / totalBuy) * 100;

    const resultEl = document.getElementById('calcProfitResult');
    resultEl.textContent = `${profit >= 0 ? '+' : ''}$${profit.toFixed(2)}`;
    resultEl.className = `fw-bold fs-4 ${profit >= 0 ? 'text-success' : 'text-danger'}`;

    document.getElementById('calcProfitPct').textContent = `${profitPct.toFixed(2)}%`;

    // Показываем варнинг если объем больше топового
    const warn = document.getElementById('calcWarning');
    const minNotionalUsd = (typeof best.min_liquidity_usd === 'number' && isFinite(best.min_liquidity_usd)) ? best.min_liquidity_usd : null
    const notionalTooBig = (minNotionalUsd !== null && amount > minNotionalUsd)
    if ((best.volume && (coins > best.volume)) || notionalTooBig) {
      warn.style.display = 'block';
    } else {
      warn.style.display = 'none';
    }
  };

  amountInput.oninput = update;
  feeInput.oninput = update;
  update();
}

function renderMiniChart(id, chart) {
  const canvas = document.getElementById(id);
  if (!canvas) return;

  const ctx = canvas.getContext('2d');
  const pts = chart.prices.map(([ts, p]) => ({ x: new Date(ts), y: p }));

  if (window.__infoChartInstance) window.__infoChartInstance.destroy();

  window.__infoChartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      datasets: [{
        data: pts,
        borderColor: '#0d6efd',
        borderWidth: 1.5,
        tension: 0.1,
        pointRadius: 0,
        fill: true,
        backgroundColor: 'rgba(13, 110, 253, 0.05)'
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { enabled: true } },
      scales: {
        x: { type: 'time', display: false },
        y: {
          display: true,
          grid: { color: 'rgba(255,255,255,0.02)' },
          ticks: { color: '#666', font: { size: 8 }, callback: (v) => v.toFixed(6) }
        }
      }
    }
  });
}

// Функция для форматирования чисел
function formatNumber(num) {
  if (num === null || num === undefined) return "N/A"

  if (num >= 1e9) {
    return (num / 1e9).toFixed(2) + "B"
  } else if (num >= 1e6) {
    return (num / 1e6).toFixed(2) + "M"
  } else if (num >= 1e3) {
    return (num / 1e3).toFixed(2) + "K"
  } else {
    return num.toFixed(2)
  }
}

// Функция для отображения уведомлений
function showNotification(message, type = "info") {
  const msg = (message === null || message === undefined) ? "" : String(message)
  const kind = (type === "danger") ? "error" : String(type || "info")
  console.log(`Уведомление (${kind}): ${msg}`)

  // Overlay notifications to avoid layout shift (fast click-through for table actions)
  let container = document.querySelector(".notifications-container")
  if (!container) {
    container = document.createElement("div")
    container.className = "notifications-container"
    document.body.appendChild(container)
  }

  

  // Enforce overlay layout even if CSS is missing/overridden.
  // This prevents toast messages from pushing the table down when clicking fast actions.
  try {
    container.style.position = "fixed"
    container.style.top = "48px"
    container.style.right = "16px"
    container.style.left = "auto"
    container.style.bottom = "auto"
    container.style.zIndex = "3000"
    container.style.display = "flex"
    container.style.flexDirection = "column"
    container.style.alignItems = "flex-end"
    container.style.gap = "8px"
    container.style.pointerEvents = "none"
  } catch (_) { /* noop */ }
const notification = document.createElement("div")
  notification.className = `notification ${kind}`
  try {
    notification.style.pointerEvents = "auto"
    notification.style.maxWidth = "min(360px, calc(100vw - 32px))"
    notification.style.width = "max-content"
  } catch (_) { /* noop */ }

  const text = document.createElement("div")
  text.className = "notification-message"
  text.textContent = msg

  const closeBtn = document.createElement("button")
  closeBtn.type = "button"
  closeBtn.className = "notification-close"
  closeBtn.setAttribute("aria-label", "Close")
  closeBtn.textContent = "×"

  const dismiss = () => {
    if (notification.dataset.dismissing === "1") return
    notification.dataset.dismissing = "1"
    notification.classList.add("fade-out")
    setTimeout(() => {
      try { notification.remove() } catch (_) { }
    }, 350)
  }

  closeBtn.addEventListener("click", dismiss)
  notification.addEventListener("click", (e) => {
    // Click on the toast itself closes it (fast dismiss); clicks on links inside should work.
    const t = e && e.target ? e.target : null
    const tag = t && t.tagName ? String(t.tagName).toLowerCase() : ""
    if (tag === "a" || tag === "button") return
    dismiss()
  })

  notification.appendChild(text)
  notification.appendChild(closeBtn)
  container.appendChild(notification)

  // Cap stack size so it never grows indefinitely
  const maxToasts = 6
  while (container.children.length > maxToasts) {
    try { container.firstElementChild.remove() } catch (_) { break }
  }

  // Auto-hide
  setTimeout(dismiss, 4200)
}

// Функция для копирования в буфер обмена
function copyToClipboard(text) {
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    showNotification("Скопировано в буфер обмена", "success");
  }).catch(err => {
    console.error('Ошибка при копировании:', err);
    const textArea = document.createElement("textarea");
    textArea.value = text;
    document.body.appendChild(textArea);
    textArea.select();
    try {
      document.execCommand('copy');
      showNotification("Скопировано в буфер обмена", "success");
    } catch (e) {
      showNotification("Ошибка при копировании", "error");
    }
    document.body.removeChild(textArea);
  });
}

// Функция для обновления списка монет CoinGecko
function escapeHtmlBasic(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function buildDebugJsonBlock(title, value) {
  const jsonText = JSON.stringify(value == null ? null : value, null, 2)
  return `
    <div class="mb-3">
      <div class="small text-uppercase text-muted fw-bold mb-2">${escapeHtmlBasic(title)}</div>
      <pre class="mb-0 p-3 rounded border border-secondary bg-black bg-opacity-25 small" style="white-space: pre-wrap; max-height: 320px; overflow: auto;">${escapeHtmlBasic(jsonText)}</pre>
    </div>
  `
}

function showInterchainDebug(asset) {
  const assetCode = String(asset || '').trim().toUpperCase()
  if (!assetCode) return

  const modalEl = document.getElementById('interchainDebugModal')
  const modalTitleEl = document.getElementById('interchainDebugModalTitle')
  const modalBodyEl = document.getElementById('interchainDebugModalBody')
  if (!modalEl || !modalBodyEl) {
    showNotification('Не найдено окно отладки маршрутов', 'error')
    return
  }

  if (modalTitleEl) modalTitleEl.textContent = `Отладка маршрутов · ${assetCode}`
  modalBodyEl.innerHTML = `
    <div class="text-center py-4">
      <div class="spinner-border text-primary mb-3"></div>
      <div class="text-muted">Собираю трассировку маршрута для ${escapeHtmlBasic(assetCode)}...</div>
    </div>
  `

  if (typeof bootstrap !== 'undefined') {
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl)
    modal.show()
  }

  const notionalUsd = Number(settings && settings.arb_min_notional_usd) > 0 ? Number(settings.arb_min_notional_usd) : 300
  const liveBridgeQuotes = scannerMode === 'interchain' ? 1 : 0
  const url = `/api/interchain_debug?asset=${encodeURIComponent(assetCode)}&notional_usd=${encodeURIComponent(notionalUsd)}&live_bridge_quotes=${liveBridgeQuotes}`

  fetch(url)
    .then((response) => response.json())
    .then((payload) => {
      if (!payload || !payload.success) {
        modalBodyEl.innerHTML = `
          <div class="alert alert-danger mb-0">
            ${escapeHtmlBasic((payload && (payload.error || payload.message)) || 'Не удалось загрузить отладку маршрута')}
          </div>
        `
        return
      }

      const data = payload.data || {}
      const scannerPreview = payload.scanner_preview || {}
      const cexRoutes = ((scannerPreview.cex_dex || {}).routes || []).length
      const crossRoutes = ((scannerPreview.cross_chain || {}).routes || []).length
      const crossAttempts = Array.isArray(data.cross_chain_attempts) ? data.cross_chain_attempts : []
      const bridgeQuoteHits = crossAttempts.filter((item) => String(item && item.status || '') === 'bridge_quote_available').length

      modalBodyEl.innerHTML = `
        <div class="row g-2 mb-3">
          <div class="col-md-3">
            <div class="p-3 rounded border border-secondary bg-black bg-opacity-25">
              <div class="small text-muted">Строк статусов</div>
              <div class="fs-5 fw-semibold">${Number((data.asset_status_summary || {}).total_rows || 0)}</div>
            </div>
          </div>
          <div class="col-md-3">
            <div class="p-3 rounded border border-secondary bg-black bg-opacity-25">
              <div class="small text-muted">DEX-квот</div>
              <div class="fs-5 fw-semibold">${Array.isArray(data.dex_quotes) ? data.dex_quotes.length : 0}</div>
            </div>
          </div>
          <div class="col-md-3">
            <div class="p-3 rounded border border-secondary bg-black bg-opacity-25">
              <div class="small text-muted">Маршрутов CEX ↔ DEX</div>
              <div class="fs-5 fw-semibold">${cexRoutes}</div>
            </div>
          </div>
          <div class="col-md-3">
            <div class="p-3 rounded border border-secondary bg-black bg-opacity-25">
              <div class="small text-muted">Межсетевые попадания</div>
              <div class="fs-5 fw-semibold">${crossRoutes} / ${bridgeQuoteHits}</div>
            </div>
          </div>
        </div>
        ${buildDebugJsonBlock('Предпросмотр сканера', scannerPreview)}
        ${buildDebugJsonBlock('Контракты', data.contracts || {})}
        ${buildDebugJsonBlock('Solana-токен', data.solana_token || null)}
        ${buildDebugJsonBlock('CEX-квоты', data.cex_quotes || [])}
        ${buildDebugJsonBlock('DEX-квоты', data.dex_quotes || [])}
        ${buildDebugJsonBlock('Сводка статусов', data.asset_status_summary || {})}
        ${buildDebugJsonBlock('Попытки межсетевых маршрутов', data.cross_chain_attempts || [])}
      `
    })
    .catch((error) => {
      console.error('Interchain debug request failed:', error)
      modalBodyEl.innerHTML = `
        <div class="alert alert-danger mb-0">
          Не удалось загрузить детали отладки.
        </div>
      `
    })
}

function updateCoingeckoList() {
  console.log("Запуск обновления списка монет CoinGecko...")

  // Показываем индикатор загрузки
  showNotification("Запуск обновления списка монет CoinGecko...", "info")

  // Отправляем запрос на обновление списка
  fetch("/api/refresh_coingecko", {
    method: "POST",
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.success) {
        console.log("Запущено обновление списка монет CoinGecko.")
        showNotification("Запущено обновление списка монет CoinGecko. Это может занять несколько минут.", "success")
      } else {
        console.error("Ошибка при запуске обновления списка монет:", data.error)
        showNotification("Ошибка при запуске обновления списка монет: " + data.error, "error")
      }
    })
    .catch((error) => {
      console.error("Ошибка при отправке запроса на обновление списка монет:", error)
      showNotification("Ошибка при запросе обновления списка монет", "error")
    })
}

// Функция для поиска арбитражных возможностей для монеты на всех биржах
function findCoinArbitrageOpportunities(symbol) {
  console.log(`Поиск арбитражных возможностей для монеты ${symbol}...`)

  // Показываем индикатор загрузки в модальном окне арбитража
  const modalBody = document.querySelector("#coinArbitrageModal .modal-body")
  if (modalBody) {
    // Показываем скелетон-загрузчик
    modalBody.innerHTML = `
      <div class="skeleton-loader">
        <div class="skeleton-item mb-3" style="height: 100px; border-radius: 12px; background: rgba(255,255,255,0.05);"></div>
        <div class="skeleton-item mb-3" style="height: 150px; border-radius: 12px; background: rgba(255,255,255,0.05);"></div>
        <div class="skeleton-item" style="height: 200px; border-radius: 12px; background: rgba(255,255,255,0.05);"></div>
        <p class="text-center mt-3 text-dim">Идет глубокий анализ...</p>
        <small class="text-muted text-center d-block mt-2">Это может занять некоторое время, т.к. идут запросы ко всем биржам</small>
      </div>
    `;
  }

  // Открываем модальное окно арбитража
  const coinArbitrageModal = document.getElementById("coinArbitrageModal")
  if (coinArbitrageModal && typeof bootstrap !== "undefined") {
    const modal = new bootstrap.Modal(coinArbitrageModal)
    modal.show()
  }

  // Устанавливаем заголовок
  const modalTitle = document.getElementById("coinArbitrageModalTitle")
  if (modalTitle) {
    modalTitle.textContent = `Арбитраж для ${symbol}`
  }

  // Запрос к API для получения данных об арбитражных возможностях
  fetch(`/api/coin_arbitrage/${symbol}`)
    .then(response => response.json())
    .then(data => {
      if (!data.success) {
        // Если запрос не удался, отображаем ошибку
        if (modalBody) {
          modalBody.innerHTML = `
            <div class="alert alert-danger">
                ${data.message || "Не удалось найти арбитражные возможности"}
                ${data.error ? `<br><small>${data.error}</small>` : ''}
            </div>
          `
        }
        return
      }

      // Если нет возможностей, показываем сообщение
      if (!data.direct_opportunities || data.direct_opportunities.length === 0) {
        if (modalBody) {
          modalBody.innerHTML = `
            <div class="alert alert-info">
                Арбитражных возможностей для монеты ${symbol} не найдено.
                <br>
                <small>Монета доступна на ${data.statistics?.exchanges_count || 0} биржах, но спред отрицательный или равен нулю.</small>
            </div>
          `
        }
        return
      }

      // Количество возможностей
      const totalOpportunities = data.direct_opportunities.length;
      const maxSpread = data.statistics?.max_spread ||
        Math.max(...data.direct_opportunities.map(o => o.spread || o.profit_percent));
      const avgSpread = data.statistics?.avg_spread ||
        (data.direct_opportunities.reduce((sum, o) => sum + (o.spread || o.profit_percent), 0) / totalOpportunities);
      const exchangesCount = data.statistics?.exchanges_count ||
        new Set(data.direct_opportunities.flatMap(o => [o.buy_exchange, o.sell_exchange])).size;

      // Создаём HTML с результатами и панелью управления
      let html = `
        <div class="mb-3">
          <div class="d-flex justify-content-between align-items-center mb-2">
            <div>
              <div class="btn-group btn-group-sm mb-2">
                <button class="btn btn-outline-primary active" id="tableViewBtn" onclick="switchArbitrageView('table')">
                  <i class="fa fa-table"></i> Таблица
                </button>
                <button class="btn btn-outline-primary" id="matrixViewBtn" onclick="switchArbitrageView('matrix')">
                  <i class="fa fa-th"></i> Матрица бирж
                </button>
              </div>
            </div>
            <div class="input-group input-group-sm" style="width: auto;">
              <span class="input-group-text">Мин. спред (%)</span>
              <input type="number" class="form-control" id="arbitrageMinSpread" value="0.1" min="0" step="0.1" style="width: 70px;">
              <button class="btn btn-outline-secondary" type="button" onclick="filterArbitrageOpportunities('${symbol}')">
                <i class="fa fa-filter"></i>
              </button>
            </div>
          </div>
        </div>
        
        <div class="card mb-3">
          <div class="card-header py-2 px-3 d-flex justify-content-between align-items-center">
            <div class="d-flex align-items-center">
              <span class="fw-bold">Статистика арбитража</span>
              <small class="text-muted updated-hint ms-2">Обновлено: ${new Date().toLocaleTimeString()}</small>
            </div>
            <span class="badge bg-primary">${totalOpportunities} возможностей</span>
          </div>
          <div class="card-body p-2">
            <div class="row g-2">
              <div class="col-sm-3">
                <div class="stat-compact">
                  <small>Биржи с ${symbol}</small>
                  <div>${exchangesCount}</div>
                </div>
              </div>
              <div class="col-sm-3">
                <div class="stat-compact">
                  <small>Всего возможностей</small>
                  <div>${totalOpportunities}</div>
                </div>
              </div>
              <div class="col-sm-3">
                <div class="stat-compact">
                  <small>Макс. спред</small>
                  <div>${maxSpread.toFixed(2)}%</div>
                </div>
              </div>
              <div class="col-sm-3">
                <div class="stat-compact">
                  <small>Средний спред</small>
                  <div>${avgSpread.toFixed(2)}%</div>
                </div>
              </div>
            </div>
          </div>
        </div>
        
        <div id="arbitrageTableView">
          <div class="table-responsive">
            <table class="table table-sm table-hover" id="arbitrageTable">
              <thead>
                <tr>
                  <th onclick="sortArbitrageTable('buy_exchange')">Покупка <i class="fa fa-sort"></i></th>
                  <th onclick="sortArbitrageTable('buy_price')">Цена покупки <i class="fa fa-sort"></i></th>
                  <th onclick="sortArbitrageTable('sell_exchange')">Продажа <i class="fa fa-sort"></i></th>
                  <th onclick="sortArbitrageTable('sell_price')">Цена продажи <i class="fa fa-sort"></i></th>
                  <th onclick="sortArbitrageTable('spread')" class="sort-desc">Спред (%) <i class="fa fa-sort-desc"></i></th>
                  <th>Объем</th>
                  <th>Действия</th>
                </tr>
              </thead>
              <tbody>
      `

      // Добавляем строки с арбитражными возможностями
      data.direct_opportunities.forEach(opp => {
        const spreadValue = opp.spread || opp.profit_percent;
        const spreadClass = spreadValue >= 5 ? 'text-success fw-bold' :
          (spreadValue >= 1 ? 'text-primary' : '');

        html += `
          <tr data-spread="${spreadValue}" data-buy-exchange="${opp.buy_exchange}" data-sell-exchange="${opp.sell_exchange}">
            <td>${opp.buy_exchange}</td>
            <td>${opp.buy_price.toFixed(8)}</td>
            <td>${opp.sell_exchange}</td>
            <td>${opp.sell_price.toFixed(8)}</td>
            <td class="${spreadClass}">${spreadValue.toFixed(2)}%</td>
            <td>${opp.volume ? formatVolume(opp.volume) : 'н/д'}</td>
            <td>
              <div class="d-flex gap-1">
                ${getExchangeUrl(opp.buy_exchange, symbol) ?
            `<a href="${getExchangeUrl(opp.buy_exchange, symbol)}" target="_blank" class="badge bg-success text-decoration-none pill-btn">Купить</a>` : ''}
                ${getExchangeUrl(opp.sell_exchange, symbol) ?
            `<a href="${getExchangeUrl(opp.sell_exchange, symbol)}" target="_blank" class="badge bg-danger text-decoration-none pill-btn">Продать</a>` : ''}
              </div>
            </td>
          </tr>
        `
      });

      html += `
              </tbody>
            </table>
          </div>
        </div>
        
        <div id="arbitrageMatrixView" style="display:none;">
          <div class="card">
            <div class="card-header py-2 px-3">
              <span class="fw-bold">Матрица арбитража между биржами</span>
            </div>
            <div class="card-body p-2">
              <div class="table-responsive">
                <table class="table table-sm table-bordered" id="arbitrageMatrix">
                  ${generateArbitrageMatrix(data.direct_opportunities, symbol)}
                </table>
              </div>
              <small class="text-muted">
                <i class="fa fa-info-circle"></i> Ячейки показывают спред (%) при покупке на бирже в строке и продаже на бирже в столбце
              </small>
            </div>
          </div>
        </div>
      `

      // Обновляем модальное окно
      modalBody.innerHTML = html;

      // Сохраняем данные для дальнейшей фильтрации/сортировки
      window.currentArbitrageData = {
        symbol: symbol,
        opportunities: data.direct_opportunities
      };

      // Инициализируем сортировку по умолчанию
      sortArbitrageTable('spread');
    })
    .catch(error => {
      console.error("Ошибка при получении данных об арбитраже:", error)
      if (modalBody) {
        modalBody.innerHTML = `
          <div class="alert alert-danger">
            Ошибка при получении данных об арбитраже: ${error.message}
          </div>
        `
      }
    })
}

// Функция для переключения между видами таблицы и матрицы арбитража
function switchArbitrageView(view) {
  const tableView = document.getElementById('arbitrageTableView');
  const matrixView = document.getElementById('arbitrageMatrixView');
  const tableBtn = document.getElementById('tableViewBtn');
  const matrixBtn = document.getElementById('matrixViewBtn');

  if (view === 'table') {
    tableView.style.display = 'block';
    matrixView.style.display = 'none';
    tableBtn.classList.add('active');
    matrixBtn.classList.remove('active');
  } else if (view === 'matrix') {
    tableView.style.display = 'none';
    matrixView.style.display = 'block';
    tableBtn.classList.remove('active');
    matrixBtn.classList.add('active');
  }
}

// Функция для генерации матрицы арбитража между биржами
function generateArbitrageMatrix(opportunities, symbol) {
  // Получаем уникальные биржи
  const exchanges = [...new Set(opportunities.flatMap(opp => [opp.buy_exchange, opp.sell_exchange]))].sort();

  // Создаем пустую матрицу для хранения спредов
  const spreadMatrix = {};
  exchanges.forEach(buyEx => {
    spreadMatrix[buyEx] = {};
    exchanges.forEach(sellEx => {
      spreadMatrix[buyEx][sellEx] = null;
    });
  });

  // Заполняем матрицу спредами из возможностей
  opportunities.forEach(opp => {
    const spread = opp.spread || opp.profit_percent;
    spreadMatrix[opp.buy_exchange][opp.sell_exchange] = spread;
  });

  // Генерируем HTML-таблицу
  let html = `<thead><tr><th></th>`;

  // Заголовки столбцов (биржи продажи)
  exchanges.forEach(exchange => {
    html += `<th class="text-center">Продажа<br><span class="badge bg-light text-dark">${exchange}</span></th>`;
  });
  html += `</tr></thead><tbody>`;

  // Строки для каждой биржи покупки
  exchanges.forEach(buyEx => {
    html += `<tr><th scope="row">Покупка<br><span class="badge bg-light text-dark">${buyEx}</span></th>`;

    // Ячейки с данными о спреде
    exchanges.forEach(sellEx => {
      if (buyEx === sellEx) {
        // Диагональ матрицы - та же самая биржа
        html += `<td class="bg-light text-center">-</td>`;
      } else {
        const spread = spreadMatrix[buyEx][sellEx];

        if (spread === null) {
          html += `<td class="text-center text-muted">-</td>`;
        } else {
          // Определяем цвет ячейки в зависимости от спреда
          let cellClass = '';
          if (spread <= 0) cellClass = 'text-muted';
          else if (spread < 1) cellClass = '';
          else if (spread < 3) cellClass = 'text-primary';
          else if (spread < 5) cellClass = 'text-success';
          else cellClass = 'text-success fw-bold';

          // Формируем ячейку с возможностью перехода на биржи
          const buyUrl = getExchangeUrl(buyEx, symbol);
          const sellUrl = getExchangeUrl(sellEx, symbol);

          html += `<td class="text-center ${cellClass}" data-buy="${buyEx}" data-sell="${sellEx}" data-spread="${spread.toFixed(2)}">
            ${spread.toFixed(2)}%
            <div class="mt-1 d-flex justify-content-center gap-1">
              ${buyUrl ? `<a href="${buyUrl}" target="_blank" class="badge bg-success">B</a>` : ''}
              ${sellUrl ? `<a href="${sellUrl}" target="_blank" class="badge bg-danger">S</a>` : ''}
            </div>
          </td>`;
        }
      }
    });

    html += `</tr>`;
  });

  html += `</tbody>`;
  return html;
}

// Функция для фильтрации арбитражных возможностей
function filterArbitrageOpportunities(symbol) {
  if (!window.currentArbitrageData || !window.currentArbitrageData.opportunities) return;

  const minSpread = parseFloat(document.getElementById('arbitrageMinSpread').value || 0);

  // Фильтруем строки таблицы
  const rows = document.querySelectorAll('#arbitrageTable tbody tr');
  rows.forEach(row => {
    const spread = parseFloat(row.getAttribute('data-spread') || 0);
    row.style.display = spread >= minSpread ? '' : 'none';
  });

  // Обновляем матрицу если она отображается
  if (document.getElementById('arbitrageMatrixView').style.display !== 'none') {
    const cells = document.querySelectorAll('#arbitrageMatrix td[data-spread]');
    cells.forEach(cell => {
      const spread = parseFloat(cell.getAttribute('data-spread') || 0);
      cell.classList.toggle('d-none', spread < minSpread);
    });
  }
}

// Вспомогательная функция для форматирования объема
function formatVolume(volume) {
  if (volume === undefined || volume === null) return 'н/д';

  volume = parseFloat(volume);
  if (isNaN(volume)) return 'н/д';

  if (volume >= 1000000) {
    return `$${(volume / 1000000).toFixed(2)}M`;
  } else if (volume >= 1000) {
    return `$${(volume / 1000).toFixed(2)}K`;
  } else {
    return `$${volume.toFixed(2)}`;
  }
}

// Функция для сортировки таблицы арбитража
function sortArbitrageTable(column) {
  if (!window.currentArbitrageData) return;

  const table = document.getElementById('arbitrageTable');
  if (!table) return;

  const thead = table.querySelector('thead');
  const tbody = table.querySelector('tbody');

  // Определяем текущее направление сортировки
  const th = thead.querySelector(`th[onclick="sortArbitrageTable('${column}')"]`);
  let sortAsc = true;

  if (th.classList.contains('sort-asc')) {
    sortAsc = false;
    th.classList.remove('sort-asc');
    th.classList.add('sort-desc');
    th.querySelector('i').className = 'fa fa-sort-desc';
  } else {
    sortAsc = true;
    th.classList.remove('sort-desc');
    th.classList.add('sort-asc');
    th.querySelector('i').className = 'fa fa-sort-asc';
  }

  // Сбрасываем сортировку для других столбцов
  thead.querySelectorAll('th').forEach(header => {
    if (header !== th) {
      header.classList.remove('sort-asc', 'sort-desc');
      const icon = header.querySelector('i');
      if (icon) icon.className = 'fa fa-sort';
    }
  });

  // Сортируем данные
  const rows = Array.from(tbody.querySelectorAll('tr'));
  rows.sort((a, b) => {
    let aVal, bVal;

    if (column === 'buy_exchange') {
      aVal = a.cells[0].textContent;
      bVal = b.cells[0].textContent;
    } else if (column === 'sell_exchange') {
      aVal = a.cells[2].textContent;
      bVal = b.cells[2].textContent;
    } else if (column === 'spread') {
      aVal = parseFloat(a.getAttribute('data-spread'));
      bVal = parseFloat(b.getAttribute('data-spread'));
    } else if (column === 'buy_price') {
      aVal = parseFloat(a.cells[1].textContent);
      bVal = parseFloat(b.cells[1].textContent);
    } else if (column === 'sell_price') {
      aVal = parseFloat(a.cells[3].textContent);
      bVal = parseFloat(b.cells[3].textContent);
    } else {
      return 0;
    }

    if (typeof aVal === 'string') {
      return sortAsc ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
    } else {
      return sortAsc ? aVal - bVal : bVal - aVal;
    }
  });

  // Очищаем и перезаполняем таблицу
  while (tbody.firstChild) {
    tbody.removeChild(tbody.firstChild);
  }

  rows.forEach(row => {
    tbody.appendChild(row);
  });
}

// Expose inline handlers for table actions
try {
  window.toggleFavorite = toggleFavorite
  window.toggleSymbolGroup = toggleSymbolGroup
  window.addToBlacklistFromTable = addToBlacklistFromTable
  window.addToBlacklistFromInfo = addToBlacklistFromInfo
  window.removeFromBlacklist = removeFromBlacklist
  window.copyToClipboard = copyToClipboard
  window.loadExchangeStatuses = loadExchangeStatuses
  window.switchArbitrageView = switchArbitrageView
  window.filterArbitrageOpportunities = filterArbitrageOpportunities
  window.sortArbitrageTable = sortArbitrageTable
  window.addWarnMark = addWarnMark
  window.showCoinInfo = showCoinInfo
  window.showInterchainDebug = showInterchainDebug
} catch (_) { /* noop */ }
