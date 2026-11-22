import { api } from '../api.js';

export async function renderSettings(el) {
  const data = await api('/api/settings');
  el.innerHTML = `
    <div class="card">
      <h2>台参数</h2>
      <table>
        <thead><tr><th>项</th><th>说明</th><th>值</th><th></th></tr></thead>
        <tbody>
          ${(data.items || []).map((s) => `
            <tr>
              <td>${s.key}</td>
              <td>${s.label}${s.readonly ? ' <span class="chip bad">只读</span>' : ''}</td>
              <td>${s.readonly
                ? `<input data-key="${s.key}" value="${s.value}" disabled />`
                : `<input data-key="${s.key}" value="${s.value}" />`}</td>
              <td>${s.readonly
                ? '<span class="muted">入舱落点口径不可改</span>'
                : `<button data-save="${s.key}">保存</button>`}</td>
            </tr>`).join('')}
        </tbody>
      </table>
      <p class="muted">入舱落点只兑现一边：当前为「旁路凭证表」，锁写入舱瞬间条目键、质量标记与样本指纹，拒改写；未启用物化文件方案。</p>
    </div>`;
  el.querySelectorAll('[data-save]').forEach((btn) => {
    btn.onclick = async () => {
      const input = el.querySelector(`input[data-key="${btn.dataset.save}"]`);
      await api('/api/settings/update', {
        method: 'POST',
        body: JSON.stringify({ key: btn.dataset.save, value: input.value }),
      });
      await renderSettings(el);
    };
  });
}
