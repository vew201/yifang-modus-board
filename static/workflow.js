'use strict';

const opportunityPhases = {
  lead: '线索发现',
  communication: '沟通阶段',
  proposal: '最终方案及报价已提交',
};
const fileRoles = {
  image: '帖子图片',
  recording: '沟通原文件',
  attachment: '相关附件',
  solution: '解决方案书',
  quote: '报价书',
};
const formatMoment = (value) => (value ? `${value.slice(0, 10)} ${value.slice(11, 16)}` : '待补充');
const localNow = () => new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 16);
const conversationRecords = (id) =>
  state.data.conversations
    .filter((x) => x.opportunity_id === id)
    .sort(
      (a, b) =>
        (a.occurred_at || a.created_at).localeCompare(b.occurred_at || b.created_at) || a.id - b.id,
    );
const filesForOpportunity = (id) =>
  (state.data.opportunity_files || []).filter((x) => x.opportunity_id === id);
const field = (label, name, value = '', type = 'text', required = false) =>
  `<label class="field">${e(label)}<input name="${name}" type="${type}" value="${e(value)}" ${required ? 'required' : ''}></label>`;
const textField = (label, name, value = '', required = false) =>
  `<label class="field">${e(label)}<textarea name="${name}" rows="4" ${required ? 'required' : ''}>${e(value)}</textarea></label>`;

function conversationChips(item) {
  const records = conversationRecords(item.id);
  const ordinal = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十'];
  return records.length
    ? `<div class="communication-chips" aria-label="历次沟通">${records.map((record, index) => `<button class="communication-chip" data-workflow="conversation-detail" data-id="${record.id}" data-tooltip-conversation="${record.id}">${ordinal[index] || index + 1}次沟通<small>${e(record.occurred_at ? formatMoment(record.occurred_at) : '实际时间待补充')}</small></button>`).join('')}</div>`
    : '';
}

function renderOpportunityCard(item) {
  const images = filesForOpportunity(item.id).filter((x) => x.role === 'image');
  return `<article class="opportunity-card opportunity-workflow-card age-${item.age_band || 'white'}" data-age-band="${item.age_band || 'white'}" data-tooltip-opportunity="${item.id}" style="--item-color:${item.color}">
    <div class="opportunity-card-head"><span class="opportunity-marker" style="--item-color:${item.color}"></span><span class="status-tag">${e(opportunityPhases[item.phase])}</span></div>
    <button class="opportunity-title" data-action="opportunity-detail" data-id="${item.id}" data-tooltip-opportunity="${item.id}"><h2>${e(item.name)}</h2></button>
    <p class="description">${e(item.description || '我的总结待补充')}</p>
    <p class="lead-meta">${e(item.channel || '渠道待补充')} · ${e(item.service_type || '服务类型待补充')}</p>
    <p class="lead-meta">${e(item.company || '公司待补充')} · ${e(item.company_details || '公司详情待补充')}</p>
    ${item.post_text ? `<details class="post-preview"><summary>对方帖子原文</summary><p class="detail-description">${e(item.post_text)}</p></details>` : '<p class="lead-meta">帖子原文待补充</p>'}
    ${
      images.length
        ? `<div class="opportunity-images">${images
            .slice(0, 3)
            .map(
              (image) =>
                `<button data-workflow="image" data-id="${image.id}" aria-label="查看图片 ${e(image.name)}"><img src="/api/opportunity_files/${image.id}" alt="${e(image.name)}" loading="lazy"></button>`,
            )
            .join('')}</div>`
        : ''
    }
    ${item.requirements ? `<p class="requirements-preview"><strong>最新需求</strong> ${e(item.requirements)}</p>` : ''}
    ${conversationChips(item)}
    ${item.next_followup_at && item.outcome === 'active' ? `<p class="followup-note">下次跟进 ${e(formatMoment(item.next_followup_at))}<small>${e(item.time_basis)}</small></p>` : ''}
    <p class="activity-age">距上次更新 ${item.idle_days || 0} 天<small>${e(formatMoment(item.last_activity_at || item.created_at))}${item.activity_note ? ` · ${e(item.activity_note)}` : ''}</small></p>
    <div class="card-foot"><span>${e(ownerName(item.owner_id))}</span><button class="text-button" data-action="opportunity-detail" data-id="${item.id}">查看全部信息</button></div></article>`;
}

function renderOpportunityBoard() {
  const all = state.data.opportunities;
  const active = all.filter((x) => x.outcome === 'active');
  const converted = all.filter((x) => x.outcome === 'converted');
  return `<div class="page-head"><div><h1>沟通意向</h1><p class="page-subtitle">${active.length} 个跟进中的机会 · 按实际沟通推进阶段</p></div><button class="button primary" data-action="new-opportunity">${icon('plus')}新增意向</button></div>
    <div class="opportunity-board">${Object.entries(opportunityPhases)
      .map(([phase, label], i) => {
        const items = active.filter((x) => x.phase === phase);
        return `<section class="opportunity-lane" data-phase="${phase}"><div class="lane-title"><span class="lane-number">0${i + 1}</span><h2>${e(label)}</h2><span>${items.length}</span></div>${items.map(renderOpportunityCard).join('') || '<p class="empty">此阶段暂无意向</p>'}</section>`;
      })
      .join('')}</div>
    ${converted.length ? `<section class="panel converted-panel"><h2>已转入开发</h2><p class="muted">开发全部完成且尾款到账后，将自动进入成功项目。</p>${converted.map((item) => `<div class="trash-row"><button class="text-button" data-action="opportunity-detail" data-id="${item.id}">${e(item.name)} · 查看沟通资料</button><button class="button small" data-action="go-project" data-id="${item.linked_project_id}">查看项目</button></div>`).join('')}</section>` : ''}`;
}

function opportunityForm(identifier = null) {
  const item = state.data.opportunities.find((x) => x.id === identifier) || {
    owner_id: state.user.id,
    phase: 'lead',
    color: palette[(state.data.opportunities.length + 1) % palette.length],
  };
  const files = filesForOpportunity(identifier);
  const fileSelect = (role, key, label) =>
    `<label class="field">${label}<select name="${key}"><option value="">请选择已上传文件</option>${files
      .filter((x) => x.role === role)
      .map(
        (x) =>
          `<option value="${x.id}" ${item[key] === x.id ? 'selected' : ''}>${e(x.name)} · ${e(x.version || '版本待补充')}</option>`,
      )
      .join('')}</select></label>`;
  openModal(
    identifier ? '编辑合作意向' : '新增合作意向',
    `<form data-workflow-form="opportunity" data-id="${identifier || ''}" data-revision="${item.revision || 0}">
    ${field('意向名称', 'name', item.name || '', 'text', true)}
    <div class="form-grid">${field('公司或平台', 'company', item.company)}${field('联系人与联系方式', 'contact', item.contact)}${field('发现渠道', 'channel', item.channel)}${field('帖子来源链接', 'source_url', item.source_url, 'url')}${field('所需服务类型', 'service_type', item.service_type)}<label class="field">负责人<select name="owner_id">${userOptions(item.owner_id)}</select></label></div>
    ${textField('公司详情', 'company_details', item.company_details)}${textField('我总结的信息', 'description', item.description)}${textField('对方发布的帖子文本', 'post_text', item.post_text)}
    ${
      identifier
        ? `<label class="field">阶段<select name="phase">${Object.entries(opportunityPhases)
            .map(
              ([value, label]) =>
                `<option value="${value}" ${item.phase === value ? 'selected' : ''}>${label}</option>`,
            )
            .join(
              '',
            )}</select></label>${textField('当前已确认需求', 'requirements', item.requirements)}
      <details class="proposal-fields" ${item.phase === 'proposal' ? 'open' : ''}><summary>第三阶段 · 最终方案及报价提交</summary><p class="muted">先在意向详情上传方案和报价，保留版本后选择最终文件。</p>${fileSelect('solution', 'solution_file_id', '最终版解决方案书')}${fileSelect('quote', 'quote_file_id', '最终版报价书')}${field('两份最终文件已提交完成的实际时间（北京时间）', 'submitted_at', (item.submitted_at || '').slice(0, 16), 'datetime-local')}${field('客户约定反馈时间（未约定留空）', 'feedback_at', (item.feedback_at || '').slice(0, 16), 'datetime-local')}${field('提交对象及方式', 'submission_recipient', item.submission_recipient)}</details>`
        : ''
    }
    ${field('下一步安排', 'next_step', item.next_step)}<input type="hidden" name="status" value="${e(item.status || '初步接洽')}"><div class="field">意向颜色${colors(item.color)}</div>${formFooter(identifier ? '保存修改' : '保存意向')}</form>`,
  );
}

function conversationBody(record) {
  return `${[
    ['摘要', record.summary],
    ['参与人及方式', record.participants],
    ['整理后的需求', record.requirements],
    ['待确认问题', record.open_questions],
    ['结论', record.decisions],
    ['下一步', record.next_action],
    [
      '约定下次时间',
      record.agreed_next_at ? formatMoment(record.agreed_next_at) : '未约定，采用默认规则',
    ],
    ['原文', record.text],
  ]
    .map(([label, value]) =>
      value
        ? `<div class="communication-detail"><strong>${e(label)}</strong><p>${e(value)}</p></div>`
        : '',
    )
    .join('')}`;
}

function conversationHistory(record) {
  return (state.data.conversation_history || [])
    .filter((x) => x.conversation_id === record.id)
    .map((version) => {
      const snapshot = JSON.parse(version.snapshot);
      return `<details class="conversation-item"><summary>历史修订 ${version.revision} · ${e(formatMoment(version.saved_at))}</summary>${conversationBody(snapshot)}</details>`;
    })
    .join('');
}

function opportunityFileList(item, conversationId = null) {
  const files = filesForOpportunity(item.id).filter(
    (x) => conversationId === null || x.conversation_id === conversationId,
  );
  return `<div class="opportunity-file-list">${files.map((file) => `<div class="trash-row"><a class="text-button" href="/api/opportunity_files/${file.id}">${e(fileRoles[file.role])} · ${e(file.name)} ${e(file.version)}</a><button class="text-button" data-maintenance="remove" data-table="opportunity_files" data-id="${file.id}">回收</button></div>`).join('') || '<p class="file-empty">暂无附件</p>'}</div>`;
}

function openOpportunity(identifier) {
  const item = state.data.opportunities.find((x) => x.id === identifier);
  if (!item) return;
  const tasks = state.data.tasks.filter((x) => x.opportunity_id === identifier && !x.done);
  openModal(
    item.name,
    `<p class="detail-description">${e(item.description || '我的总结待补充')}</p><dl class="detail-meta">${[
      ['阶段', opportunityPhases[item.phase]],
      ['结果', { active: '跟进中', converted: '已转入开发', failed: '已归档失败' }[item.outcome]],
      ['负责人', ownerName(item.owner_id)],
      ['发现渠道', item.channel],
      ['公司', item.company],
      ['公司详情', item.company_details],
      ['联系人', item.contact],
      ['服务类型', item.service_type],
      ['下一步', item.next_step],
      [
        '下次跟进',
        item.outcome === 'active' ? formatMoment(item.next_followup_at) : '已停止自动跟进',
      ],
      ['时间依据', item.time_basis],
      ['提交时间', item.submitted_at && formatMoment(item.submitted_at)],
      ['提交对象及方式', item.submission_recipient],
    ]
      .map(([label, value]) => `<dt>${e(label)}</dt><dd>${e(value || '待补充')}</dd>`)
      .join('')}</dl>
    ${item.source_url ? `<a class="button small" href="${e(item.source_url)}" target="_blank" rel="noopener">打开原帖</a>` : ''}
    <h3>对方帖子原文</h3><p class="detail-description">${e(item.post_text || '待补充')}</p>
    <div class="opportunity-images">${filesForOpportunity(identifier)
      .filter((x) => x.role === 'image')
      .map(
        (file) =>
          `<button data-workflow="image" data-id="${file.id}" aria-label="查看图片 ${e(file.name)}"><img src="/api/opportunity_files/${file.id}" alt="${e(file.name)}"></button>`,
      )
      .join('')}</div>
    <h3>当前整理后的需求</h3><p class="detail-description">${e(item.requirements || '等待沟通整理')}</p>
    <div class="modal-actions">${maintenanceButtons('opportunities', item)}<button class="button" data-workflow="new-conversation" data-id="${identifier}">追加沟通</button><button class="button" data-workflow="upload" data-id="${identifier}">上传图片或文件</button>
    ${item.phase === 'proposal' && item.outcome === 'active' ? `<button class="button primary" data-workflow="convert" data-id="${identifier}">转入开发项目</button><button class="button danger" data-workflow="fail" data-id="${identifier}">归档失败</button>` : ''}
    ${item.outcome === 'failed' ? `<button class="button" data-workflow="reopen" data-id="${identifier}">重新跟进</button>` : ''}
    ${item.linked_project_id ? `<button class="button" data-action="go-project" data-id="${item.linked_project_id}">查看开发项目</button>` : ''}</div>
    ${item.failure_reason ? `<h3>失败原因</h3><p class="detail-description">${e(item.failure_reason)}</p>` : ''}
    <h3>历次沟通 · 悬停预览，点击展开</h3>${conversationChips(item) || '<p class="muted">尚无记录</p>'}
    <h3>全部资料及版本</h3>${opportunityFileList(item)}
    ${tasks.length ? `<h3>待办事项</h3>${tasks.map((task) => `<div class="trash-row"><button class="text-button" data-action="task-detail" data-id="${task.id}">${e(task.title)}</button><span>${e(taskDates(task))}</span></div>`).join('')}` : ''}`,
  );
}

function communicationForm(opportunityId, record = null) {
  const item = record || { owner_id: state.user.id, occurred_at: localNow() };
  openModal(
    record ? '编辑本次沟通' : '追加沟通记录',
    `<form data-workflow-form="conversation" data-id="${record?.id || ''}" data-revision="${record?.revision || 0}"><input type="hidden" name="opportunity_id" value="${opportunityId}"><input type="hidden" name="client_key" value="${record?.client_key || crypto.randomUUID()}">
    <p class="muted">填写实际沟通结束时间。Codex 整理的内容会存入本意向，原始记录及附件保留。</p>
    <div class="form-grid">${field('实际沟通结束时间（北京时间）', 'occurred_at', item.occurred_at.slice(0, 16), 'datetime-local', true)}<label class="field">记录负责人<select name="owner_id">${userOptions(item.owner_id)}</select></label></div>
    ${field('参与人及沟通方式', 'participants', item.participants)}${field('沟通摘要', 'summary', item.summary)}${textField('沟通原文与记录', 'text', item.text, true)}${textField('本次整理后的完整需求', 'requirements', item.requirements)}${textField('待确认问题', 'open_questions', item.open_questions)}${textField('沟通结论', 'decisions', item.decisions)}${field('下一步动作', 'next_action', item.next_action)}${field('约定的下次沟通或反馈时间（未约定留空）', 'agreed_next_at', (item.agreed_next_at || '').slice(0, 16), 'datetime-local')}
    <p class="muted">沟通阶段未约定时为本次结束后 24 小时；方案报价已提交阶段为 48 小时。</p>${formFooter('保存沟通')}</form>`,
  );
}

function renderProjectOutcome(project) {
  return `<section class="project-outcome"><div><strong>${project.completed_at ? '开发已全部完成' : '开发进行中'}</strong><p class="muted">${project.final_payment_at ? `尾款到账 ${e(formatMoment(project.final_payment_at))}` : '尾款尚未确认到账'}${project.completed_at && project.final_payment_at ? ' · 已进入成功项目' : ''}</p>${project.payment_note ? `<p>${e(project.payment_note)}</p>` : ''}</div><div class="project-outcome-actions"><button class="button small" data-project-model="development" data-id="${project.id}">${project.completed_at ? '恢复开发' : '确认开发完成'}</button><button class="button small" data-workflow="payment" data-id="${project.id}">登记尾款</button></div></section>`;
}

function renderSuccessfulProjects() {
  const projects = state.data.projects.filter((x) => x.completed_at && x.final_payment_at);
  return `<div class="page-head"><div><h1>成功项目</h1><p class="page-subtitle">开发全部完成，并且尾款已到账 · 共 ${projects.length} 个</p></div></div><div class="outcome-grid">${
    projects
      .map(
        (project) =>
          `<article class="panel outcome-card"><span class="status-tag status-complete">已完成 · 已收尾款</span><h2>${e(project.name)}</h2><p>${e(project.client || '客户待补充')} · ${e(ownerName(project.owner_id))}</p><p>开发完成 ${e(formatMoment(project.completed_at))}</p><p>尾款到账 ${e(formatMoment(project.final_payment_at))}</p><button class="button" data-action="go-project" data-id="${project.id}">查看项目</button>${state.data.opportunities
            .filter((x) => x.linked_project_id === project.id)
            .map(
              (item) =>
                `<button class="button" data-action="opportunity-detail" data-id="${item.id}">查看前期沟通</button>`,
            )
            .join('')}</article>`,
      )
      .join('') || '<div class="panel empty">暂时没有同时满足开发完成和尾款到账的项目。</div>'
  }</div>`;
}

function renderFailedOpportunities() {
  const items = state.data.opportunities.filter((x) => x.outcome === 'failed');
  return `<div class="page-head"><div><h1>失败项目</h1><p class="page-subtitle">保留沟通、方案报价和失败原因 · 共 ${items.length} 个</p></div></div><div class="outcome-grid">${items.map((item) => `<article class="panel outcome-card"><span class="status-tag">合作未达成</span><h2>${e(item.name)}</h2><p>${e(item.company)} · ${e(ownerName(item.owner_id))}</p><p>${e(formatMoment(item.closed_at))}</p><h3>失败原因</h3><p class="detail-description">${e(item.failure_reason)}</p><button class="button" data-action="opportunity-detail" data-id="${item.id}">查看完整记录</button></article>`).join('') || '<div class="panel empty">暂无失败归档。</div>'}</div>`;
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-workflow]');
  if (!button) return;
  hideTooltip();
  const id = Number(button.dataset.id);
  const action = button.dataset.workflow;
  const item = state.data.opportunities.find((x) => x.id === id);
  try {
    if (action === 'image') {
      const file = state.data.opportunity_files.find((x) => x.id === id);
      openModal(
        file.name,
        `<img class="image-preview" src="/api/opportunity_files/${id}" alt="${e(file.name)}"><a class="button" href="/api/opportunity_files/${id}" target="_blank" rel="noopener">打开原图</a>`,
      );
    } else if (action === 'new-conversation') communicationForm(id);
    else if (action === 'edit-conversation') {
      const record = state.data.conversations.find((x) => x.id === id);
      communicationForm(record.opportunity_id, record);
    } else if (action === 'conversation-detail') {
      const record = state.data.conversations.find((x) => x.id === id);
      const parent = state.data.opportunities.find((x) => x.id === record.opportunity_id);
      openModal(
        `沟通记录 · ${record.occurred_at ? formatMoment(record.occurred_at) : '实际时间待补充'}`,
        `${conversationBody(record)}${conversationHistory(record)}<p class="muted">修订版本 ${record.revision || 0}</p><div class="modal-actions"><button class="button" data-workflow="edit-conversation" data-id="${id}">编辑本次记录</button><button class="button" data-workflow="upload" data-id="${parent.id}" data-conversation-id="${id}">上传本次沟通原文件</button><button class="button" data-action="opportunity-detail" data-id="${parent.id}">返回意向</button></div>${opportunityFileList(parent, id)}`,
      );
    } else if (action === 'upload') {
      openModal(
        '上传意向资料',
        `<form data-workflow-form="upload"><input type="hidden" name="opportunity_id" value="${id}"><input type="hidden" name="conversation_id" value="${button.dataset.conversationId || ''}"><label class="field">文件<input type="file" name="file" required></label><label class="field">资料类型<select name="role">${Object.entries(
          fileRoles,
        )
          .map(([key, label]) => `<option value="${key}">${label}</option>`)
          .join(
            '',
          )}</select></label>${field('版本说明（如最终版、V2）', 'version')}<label class="field">负责人<select name="owner_id">${userOptions(state.user.id)}</select></label><p class="muted">每个文件不超过 20 MB；新版本会单独保存。</p>${formFooter('上传并保存')}</form>`,
      );
    } else if (action === 'fail')
      openModal(
        '归档失败',
        `<form data-workflow-form="fail" data-id="${id}" data-revision="${item.revision}">${textField('失败原因（必填）', 'failure_reason', '', true)}<p class="muted">保存后停止此意向的自动跟进，全部资料保留。</p>${formFooter('归档至失败项目')}</form>`,
      );
    else if (action === 'convert')
      openModal(
        '转入开发项目',
        `<form data-workflow-form="convert" data-id="${id}" data-revision="${item.revision}"><p>已确认进入开发后，关联已有项目或创建新项目。开发完成且收到尾款后才进入成功项目。</p><label class="field">开发项目<select name="project_id"><option value="">创建同名新项目</option>${state.data.projects.map((x) => `<option value="${x.id}">${e(x.name)}</option>`).join('')}</select></label>${formFooter('确认转入开发')}</form>`,
      );
    else if (action === 'reopen')
      openModal(
        '重新跟进',
        `<form data-workflow-form="reopen" data-id="${id}" data-revision="${item.revision}">${field('新约定时间（未约定则从现在起 48 小时）', 'agreed_next_at', '', 'datetime-local')}${formFooter('恢复跟进')}</form>`,
      );
    else if (action === 'payment') {
      const project = state.data.projects.find((x) => x.id === id);
      openModal(
        '登记尾款到账',
        `<form data-workflow-form="payment" data-id="${id}" data-revision="${project.revision}">${field('尾款实际到账时间（北京时间，清空可撤销登记）', 'final_payment_at', (project.final_payment_at || '').slice(0, 16), 'datetime-local')}${textField('到账备注', 'payment_note', project.payment_note)}<p class="muted">请在确认收到全部尾款后登记。已明确确认开发全部完成，且此处有尾款到账时间，项目才进入成功项目。</p>${formFooter('保存尾款记录')}</form>`,
      );
    }
  } catch (error) {
    toast(error.message);
  }
});

document.addEventListener('submit', async (event) => {
  const form = event.target.closest('[data-workflow-form]');
  if (!form) return;
  event.preventDefault();
  const submit = form.querySelector('[type="submit"]');
  const errorElement = form.querySelector('.form-error');
  submit.disabled = true;
  errorElement.textContent = '';
  try {
    const values = Object.fromEntries(new FormData(form));
    const type = form.dataset.workflowForm;
    const id = form.dataset.id;
    if (id) values.revision = Number(form.dataset.revision);
    if (type === 'opportunity')
      await api(`/opportunities${id ? '/' + id : ''}`, id ? 'PATCH' : 'POST', values);
    else if (type === 'conversation')
      await api(`/conversations${id ? '/' + id : ''}`, id ? 'PATCH' : 'POST', values);
    else if (['fail', 'convert', 'reopen'].includes(type))
      await api(`/opportunities/${id}/${type}`, 'POST', values);
    else if (type === 'payment') await api(`/projects/${id}`, 'PATCH', values);
    else if (type === 'upload') {
      const file = form.elements.file.files[0];
      if (!file || !file.size || file.size > 20 * 1024 * 1024)
        throw new Error('请选择 1 字节至 20 MB 的文件');
      const content = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result.split(',')[1]);
        reader.onerror = () => reject(new Error('读取失败'));
        reader.readAsDataURL(file);
      });
      delete values.file;
      await api('/opportunity_files', 'POST', { ...values, name: file.name, content });
    }
    await refresh();
    modal.close();
    toast('已保存，待办及页面已同步');
  } catch (error) {
    errorElement.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});
