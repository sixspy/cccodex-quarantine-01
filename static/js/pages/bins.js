import { api } from '../api.js';

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

export async function renderBins(el) {
  const bins = await api('/api/bins');
  const rows = await api('/api/rows');
  el.innerHTML = `
    <div class="card">
      <h2>隔离舱 <span class="chip ok">入舱落点：${esc((bins.items || [])[0]?.landing_mode || '旁路凭证表')}</span></h2>
      <table>
        <thead><tr><th>舱号</th><th>标题</th><th>状态</th><th>占用/容量</th><th>待复核</th><th>来源芯片</th><th>落点</th></tr></thead>
        <tbody>
          ${(bins.items || []).map((b) => `
            <tr>
              <td>${esc(b.bin_code)}</td>
              <td>${esc(b.title)}</td>
              <td><span class="chip">${esc(b.status)}</span></td>
              <td>${b.row_count}/${b.capacity}</td>
              <td>${b.pending_count}</td>
              <td>
                ${(b.sources || []).map((s) => `<span class="chip ok">${esc(s)}</span>`).join(' ') || '<span class="muted">无</span>'}
                ${(b.qualities || []).map((q) => `<span class="chip bad">${esc(q)}</span>`).join(' ')}
              </td>
              <td><span class="chip ok">${esc(b.landing_mode || '旁路凭证表')}</span></td>
            </tr>`).join('')}
        </tbody>
      </table>
      <p class="muted">来源芯片与落点徽取自入舱旁路凭证（锁存入舱瞬间，拒改写）；卡片只读这一轨，不与物化文件并存。</p>
    </div>
    <div class="card">
      <h2>舱内行（读入舱凭证快照）</h2>
      <table>
        <thead><tr><th>舱</th><th>来源</th><th>条目键</th><th>质量标记</th><th>入舱载荷</th><th>原因</th><th>状态</th><th>指纹</th></tr></thead>
        <tbody>
          ${(rows.items || []).map((r) => `
            <tr>
              <td>${esc(r.bin_code)}</td>
              <td><span class="chip ok">${esc(r.source_name || '')}</span></td>
              <td>${esc(r.item_key)}</td>
              <td><span class="chip bad">${esc(r.quality_snapshot)}</span></td>
              <td class="payload-cell">${esc(r.payload_snapshot)}</td>
              <td>${esc(r.reason)}</td>
              <td><span class="chip">${esc(r.status)}</span></td>
              <td>${r.fingerprint_ok
                ? '<span class="chip ok">指纹一致</span>'
                : '<span class="chip bad">指纹拧花</span>'}</td>
            </tr>`).join('') || '<tr><td colspan="8" class="empty">舱内暂无条目</td></tr>'}
        </tbody>
      </table>
    </div>`;
}
