'use strict';

const stageStatusLabels = {
  pending: '未开始',
  in_progress: '进行中',
  completed: '已完成',
  not_applicable: '不适用',
};

function renderPhaseCard(project, stage) {
  const tasks = state.data.tasks.filter(
    (x) => x.project_id === project.id && x.stage_id === stage.id,
  );
  const files = state.data.documents.filter(
    (x) => x.project_id === project.id && x.stage_id === stage.id,
  );
  const conversations = state.data.conversations.filter(
    (x) => x.project_id === project.id && x.stage_id === stage.id,
  );
  return `<section class="phase-card ${stage.status === 'completed' ? 'phase-complete' : ''}" data-stage-id="${stage.id}">
    <div class="phase-card-head"><span class="phase-number">${e(stage.stage_key.padStart(2, '0'))}</span><h3>${e(stage.name)}</h3><span class="phase-status">${e(stageStatusLabels[stage.status])}</span></div>
    <p class="phase-meaning">${e(state.data.stage_meanings[Number(stage.stage_key) - 1])}</p>
    ${stage.work_summary ? `<p class="phase-summary">${e(stage.work_summary)}</p>` : '<p class="phase-empty">阶段工作内容待补充</p>'}
    ${stage.completed_at ? `<p class="phase-completion-time">实际完成 ${e(formatMoment(stage.completed_at))}</p>` : ''}
    ${stage.completion_note ? `<p class="phase-note">${e(stage.completion_note)}</p>` : ''}
    ${stage.start_date ? `<p class="phase-meaning">计划 ${e(taskDates(stage))}</p>` : ''}
    ${files.length ? `<details class="phase-related"><summary>阶段资料 · ${files.length}</summary>${files.map((file) => `<a href="/api/documents/${file.id}" class="file-item">${icon('file')}${e(file.name)}</a>`).join('')}</details>` : ''}
    ${conversations.length ? `<details class="phase-related"><summary>相关沟通 · ${conversations.length}</summary>${conversations.map((record) => `<p class="phase-summary">${e(record.summary || record.text)}</p>`).join('')}</details>` : ''}
    ${tasks.length ? `<details class="phase-related"><summary>相关待办 · ${tasks.length}（独立记录）</summary>${tasks.map((task) => `<div class="task-row"><div><button class="text-button" data-action="task-detail" data-id="${task.id}">${e(task.title)}</button><p class="task-meta">${task.done ? '已完成' : '待完成'} · ${e(ownerName(task.owner_id))}</p></div></div>`).join('')}</details>` : ''}
    <button class="button small" data-project-model="stage" data-id="${stage.id}">查看与更新阶段</button>
  </section>`;
}

function editPhase(identifier) {
  const stage = state.data.stages.find((x) => x.id === identifier);
  const history = (state.data.stage_history || []).filter((x) => x.stage_id === identifier);
  openModal(
    stage.name,
    `<form data-project-model-form="stage" data-id="${stage.id}" data-revision="${stage.revision}">
    <p class="muted">${e(state.data.stage_meanings[Number(stage.stage_key) - 1])} 阶段状态按实际成果确认，关联待办不会自动改变它。</p>
    ${textField('本阶段的工作内容与进展', 'work_summary', stage.work_summary)}
    <label class="field">实际阶段状态<select name="status">${Object.entries(stageStatusLabels)
      .map(
        ([key, label]) =>
          `<option value="${key}" ${stage.status === key ? 'selected' : ''}>${label}</option>`,
      )
      .join('')}</select></label>
    ${textField('完成说明或依据（已完成／不适用时必填）', 'completion_note', stage.completion_note)}
    ${field('实际完成时间（选择已完成时生效，北京时间）', 'completed_at', (stage.completed_at || localNow()).slice(0, 16), 'datetime-local')}
    <div class="form-grid">${field('计划开始日期（可留空）', 'start_date', stage.start_date, 'date')}${field('计划结束日期（可留空）', 'end_date', stage.end_date, 'date')}</div>
    ${formFooter('保存阶段记录')}</form>
    ${
      history.length
        ? `<details class="phase-related"><summary>历次阶段记录 · ${history.length}</summary>${history
            .map((item) => {
              const record = JSON.parse(item.snapshot);
              return `<div class="conversation-item"><strong>${e(formatMoment(item.saved_at))} · ${e(stageStatusLabels[record.status] || '历史记录')}</strong><p class="detail-description">${e(record.work_summary || '')}</p><p class="detail-description">${e(record.completion_note || '')}</p></div>`;
            })
            .join('')}</details>`
        : ''
    }`,
  );
}

function addDocumentStageField(form, projectId, selected = 'auto') {
  if (!form) return;
  let label = form.querySelector('.document-stage-field');
  if (!label) {
    label = document.createElement('label');
    label.className = 'field document-stage-field';
    form.querySelector('.form-error').before(label);
  }
  label.innerHTML = `归入项目阶段<select name="stage_id"><option value="auto">按文件名分类，无法判断时留待确认</option><option value="">暂不关联阶段</option>${state.data.stages
    .filter((x) => x.project_id === Number(projectId))
    .map((stage) => `<option value="${stage.id}">${e(stage.name)}</option>`)
    .join('')}</select>`;
  label.querySelector('select').value = selected === null ? '' : String(selected);
}

function renderExtraTasks() {
  const filter = state.extraOwner || 'all';
  const tasks = state.data.tasks.filter(
    (task) =>
      !task.project_id &&
      !task.opportunity_id &&
      (state.extraShowDone || !task.done) &&
      (filter === 'all' || String(task.owner_id) === filter),
  );
  return `<div class="page-head"><div><h1>项目外待办事项</h1><p class="page-subtitle">记录日常提醒，按负责人同步到首页和五天时间轴</p></div><button class="button primary" data-action="new-task">${icon('plus')}新增事项</button></div>
    <div class="extra-filters"><label>负责人 <select id="extra-owner"><option value="all">全部人员</option>${state.data.users.map((user) => `<option value="${user.id}" ${String(user.id) === filter ? 'selected' : ''}>${e(user.name)}</option>`).join('')}</select></label><label class="checkbox-field"><input type="checkbox" id="extra-show-done" ${state.extraShowDone ? 'checked' : ''}>显示已完成</label></div>
    <div class="extra-task-grid">${tasks.map((task) => `<article class="extra-task-card ${task.done ? 'extra-task-done' : ''}" data-tooltip-task="${task.id}"><div class="extra-task-head"><input type="checkbox" class="task-check" data-complete="${task.id}" aria-label="完成 ${e(task.title)}" ${task.done ? 'checked' : ''} ${task.owner_id === state.user.id ? '' : 'disabled'}><button class="extra-task-title" data-action="task-detail" data-id="${task.id}" data-tooltip-task="${task.id}">${e(task.title)}</button></div><p>${e(taskDates(task))}</p><div class="card-foot"><span>${e(ownerName(task.owner_id))}</span><button class="text-button" data-action="task-detail" data-id="${task.id}">查看详情</button></div></article>`).join('') || '<div class="panel empty">当前没有项目外待办事项。</div>'}</div>`;
}

document.addEventListener('change', (event) => {
  if (event.target.id === 'extra-owner') {
    state.extraOwner = event.target.value;
    render();
  }
  if (event.target.id === 'extra-show-done') {
    state.extraShowDone = event.target.checked;
    render();
  }
});

document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-project-model]');
  if (!button) return;
  const id = Number(button.dataset.id);
  if (button.dataset.projectModel === 'stage') editPhase(id);
  else if (button.dataset.projectModel === 'development') {
    const project = state.data.projects.find((x) => x.id === id);
    openModal(
      project.completed_at ? '恢复开发' : '确认开发完成',
      `<form data-project-model-form="development" data-id="${id}" data-revision="${project.revision}" data-done="${!project.completed_at}">
      <p>请依据项目实际开发成果确认。阶段状态和待办勾选都不会替你自动确认项目开发完成。</p>
      ${textField(project.completed_at ? '恢复开发说明' : '开发完成说明或依据（必填）', 'completion_note', project.completion_note, !project.completed_at)}
      ${!project.completed_at ? field('开发实际完成时间（北京时间）', 'development_completed_at', localNow(), 'datetime-local', true) : ''}
      <p class="muted">项目开发完成且尾款到账后，进入成功项目。相关未完成待办仍保留。</p>${formFooter(project.completed_at ? '确认恢复开发' : '确认开发完成')}</form>`,
    );
  }
});

document.addEventListener('submit', async (event) => {
  const form = event.target.closest('[data-project-model-form]');
  if (!form) return;
  event.preventDefault();
  const submit = form.querySelector('[type="submit"]');
  const error = form.querySelector('.form-error');
  error.textContent = '';
  submit.disabled = true;
  try {
    const data = {
      ...Object.fromEntries(new FormData(form)),
      revision: Number(form.dataset.revision),
    };
    if (form.dataset.projectModelForm === 'development')
      data.development_done = form.dataset.done === 'true';
    await api(
      `/${form.dataset.projectModelForm === 'stage' ? 'stages' : 'projects'}/${form.dataset.id}`,
      'PATCH',
      data,
    );
    await refresh();
    modal.close();
    toast('已保存实际进展');
  } catch (problem) {
    error.textContent = problem.message;
  } finally {
    submit.disabled = false;
  }
});
