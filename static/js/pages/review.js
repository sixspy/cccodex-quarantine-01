import { api } from '../api.js';

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

export async function renderReview(el) {
  const data = await api('/api/tickets');
  el.innerHTML = `
    <div class="card">
      <h2>复核放行（详情读入舱凭证快照）</h2>
      <table>
        <thead>
          <tr><th>单号</th><th>来源</th><th>条目</th><th>舱</th><th>入舱质量</th><th>入舱载荷</th><th>裁决</th><th>指纹</th><th>操作</th></tr>
        </thead>
        <tbody>
          ${(data.items || []).map((t) => `
            <tr>
              <td>${esc(t.ticket_code)}</td>
              <td><span class="chip ok">${esc(t.source_name || '')}</span></td>
              <td>${esc(t.item_key)}</td>
              <td>${esc(t.bin_code)}</td>
              <td><span class="chip bad">${esc(t.quality)}</span></td>
              <td class="payload-cell">${esc(t.payload)}</td>
              <td><span class="chip">${esc(t.decision)}</span></td>
              <td>${t.fingerprint_ok
                ? '<span class="chip ok">指纹一致</span>'
                : '<span class="chip bad">指纹拧花·已拦截</span>'}</td>
              <td class="row">
                ${t.decision === '待决' ? (t.fingerprint_ok ? `
                  <button data-d="放行" data-id="${t.id}">放行</button>
                  <button data-d="驳回回炉" data-id="${t.id}">驳回回炉</button>
                  <button data-d="废弃" data-id="${t.id}">废弃</button>
                ` : '<span class="muted">禁止复核</span>') : (esc(t.comment) || '')}
              </td>
            </tr>
            <tr class="detail-row">
              <td colspan="9">
                <details>
                  <summary class="muted">详情与审计轨迹</summary>
                  <div class="grid detail-grid">
                    <div>
                      <div class="muted">入舱原因：${esc(t.reason || '')}</div>
                      <div class="muted">样本指纹：<code>${esc((t.sample_fingerprint || '').slice(0, 16))}…</code></div>
                    </div>
                    <div>
                      <div class="muted">审计旁挂（入舱 / 裁决）</div>
                      ${(t.audit || []).map((a) => `
                        <div class="muted">· ${esc(a.created_at)} ${esc(a.actor)} ${esc(a.action)} ${esc(a.detail || '')}</div>
                      `).join('') || '<div class="muted">暂无审计</div>'}
                    </div>
                  </div>
                </details>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>
      <p class="muted">样本指纹与舱内凭证拧花时，复核继续与清洗入库一律拦住。</p>
    </div>`;
  el.querySelectorAll('button[data-d]').forEach((btn) => {
    btn.onclick = async () => {
      await api(`/api/tickets/${btn.dataset.id}/decide`, {
        method: 'POST',
        body: JSON.stringify({ decision: btn.dataset.d, comment: '台上裁决' }),
      });
      await renderReview(el);
    };
  });
}
