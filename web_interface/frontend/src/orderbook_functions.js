// Служебные функции для работы со стаканами и отображения данных о глубине рынка

// Возвращает класс бейджа в зависимости от уровня уверенности
function getConfidenceBadgeClass(confidence) {
  if (confidence === undefined || confidence === null) return 'bg-secondary';
  if (confidence < 0.3) return 'bg-danger';
  if (confidence < 0.6) return 'bg-warning';
  return 'bg-success';
}

// Возвращает бейдж ликвидности
function getLiquidityBadge(data) {
  // Возвращает бейдж ликвидности
  if (!data || data.liquidity_score === undefined) {
    return '<span class="badge bg-secondary">Нет данных</span>';
  }
  
  const score = data.liquidity_score;
  let badgeClass = 'bg-secondary';
  
  if (score < 0.3) badgeClass = 'bg-danger';
  else if (score < 0.6) badgeClass = 'bg-warning';
  else badgeClass = 'bg-success';
  
  return `<span class="badge ${badgeClass}">${data.message}</span>`;
}

// Отображает детали стакана
function getOrderBookDetails(data, isBuy) {
  if (!data || !data.message) {
    return '<div class="text-muted">Нет данных о стакане</div>';
  }
  
  const sideLabel = isBuy ? 'покупки' : 'продажи';
  const volumeLabel = data.volume !== undefined ? `<div>Объем: <span class="fw-bold">${data.volume ? data.volume.toFixed(6) : 'Н/Д'}</span> BTC</div>` : '';
  const priceLabel = data.price !== undefined ? `<div>Цена ${sideLabel}: <span class="fw-bold">${data.price ? data.price.toFixed(8) : 'Н/Д'}</span> USDT</div>` : '';
  
  let liquidityBar = '';
  if (data.liquidity_score !== undefined) {
    const fillWidth = Math.max(5, Math.round(data.liquidity_score * 100));
    let barColor = '#dc3545'; // danger
    
    if (data.liquidity_score >= 0.6) barColor = '#198754'; // success
    else if (data.liquidity_score >= 0.3) barColor = '#ffc107'; // warning
    
    liquidityBar = `
      <div class="mt-1">
        <small>Ликвидность:</small>
        <div class="liquidity-bar">
          <div class="liquidity-fill" style="width: ${fillWidth}%; background-color: ${barColor};"></div>
        </div>
      </div>
    `;
  }
  
  return `
    ${priceLabel}
    ${volumeLabel}
    ${liquidityBar}
    <small class="text-muted d-block mt-1">${data.message}</small>
  `;
}

// Expose helpers for module usage
window.getConfidenceBadgeClass = getConfidenceBadgeClass;
window.getLiquidityBadge = getLiquidityBadge;
window.getOrderBookDetails = getOrderBookDetails;
