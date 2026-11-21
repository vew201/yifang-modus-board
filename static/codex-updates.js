'use strict';

async function openCodexUpdates() {
  const info = await api('/codex/info');
  openModal(
    'Codex 接入与更新',
    `<p>首次把本人专用接入包交给 Codex，自动安装、连接并配置按消息检查。首次 Hook 信任需在 Codex 中确认。</p>
    <section class="codex-release-info"><span class="status-tag">正式接入版本 ${e(info.version)}</span><p>${e(info.notes)}</p><p class="muted">正常交互只检查版本；有变化才下载更新，无定时任务。账号和个人凭证独立保留。</p></section>
    <div class="modal-actions"><button class="button primary" data-codex-action="bootstrap">生成并下载本人首次接入包</button></div>
    <p class="muted">接入包含限时一次性连接凭据，仅供绑定账号本人使用。你和合伙人分别登录自己的账号生成。</p>
    <h3>最新版技能与说明</h3><div class="codex-file-list">${info.files.map((file) => `<a class="file-item" href="/api/codex/files/${file.sha256}" download="${e(file.path.split('/').pop())}">${icon('file')}<span>${e(file.path)}</span><small>${formatSize(file.size)}</small></a>`).join('')}</div>
    <details class="proposal-fields"><summary>固定发现入口 · 首次设置</summary><p>免费 DuckDNS 名称用于找到变更后的共享地址。设置一次后，程序只在地址改变时发布；不公开账号凭证。</p><p class="muted">当前 ${info.discovery_configured ? e(info.discovery_domain) + ' · 已配置' : '尚未配置固定入口'}。首次请在 <a href="https://www.duckdns.org" target="_blank" rel="noopener">DuckDNS 官网</a> 登录并添加一个名称，再把名称和 Token 填在这里。Token 仅存后端，不回显。</p>
    <form data-codex-form="discovery"><label class="field">固定名称<input name="domain" value="${e(info.discovery_domain)}" placeholder="例如 modus-你的名称.duckdns.org" required></label><label class="field">DuckDNS Token<input name="token" type="password" autocomplete="off" required></label>${formFooter('保存并验证固定入口')}</form></details>
    <h3>已接入的自动更新客户端</h3>${info.clients.length ? info.clients.map((client) => `<div class="trash-row"><span>${e(client.name)}</span><span class="muted">${client.release === info.release ? '当前版本' : '等待下次交互自动更新'}</span></div>`).join('') : '<p class="muted">尚无自动更新客户端。旧接入可通过开发端一次性迁移，或使用本人接入包初始化。</p>'}`,
  );
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-codex-action]');
  if (!button) return;
  button.disabled = true;
  try {
    if (button.dataset.codexAction === 'bootstrap') {
      const result = await api('/codex/bootstrap', 'POST', {});
      const bytes = Uint8Array.from(atob(result.content), (character) => character.charCodeAt(0));
      const url = URL.createObjectURL(new Blob([bytes], { type: 'application/zip' }));
      const link = document.createElement('a');
      link.href = url;
      link.download = result.name;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      toast('本人接入包已生成，请交给专用 Codex 会话');
    }
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

document.addEventListener('submit', async (event) => {
  const form = event.target.closest('[data-codex-form]');
  if (!form) return;
  event.preventDefault();
  const button = form.querySelector('[type="submit"]');
  const error = form.querySelector('.form-error');
  button.disabled = true;
  error.textContent = '';
  try {
    const result = await api('/codex/discovery', 'POST', Object.fromEntries(new FormData(form)));
    form.elements.token.value = '';
    await openCodexUpdates();
    toast(result.ok ? '固定入口已配置并发布' : '已保存，等待共享地址');
  } catch (problem) {
    error.textContent = problem.message;
  } finally {
    button.disabled = false;
  }
});
