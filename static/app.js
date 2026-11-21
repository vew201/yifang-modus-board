'use strict';

const app = document.querySelector('#app');
const modal = document.querySelector('#modal');
const palette = [
  '#7864A5',
  '#4A789B',
  '#56836D',
  '#B85E57',
  '#AB7A3C',
  '#5C8587',
  '#97617D',
  '#6C738D',
];
const state = {
  user: null,
  csrf: '',
  data: null,
  page: 'home',
  expanded: new Set(),
  archiveDate: '',
  showDone: false,
};
let toastTimer;
let tooltip;

const escapeHtml = (value) =>
  String(value ?? '').replace(
    /[&<>"']/g,
    (character) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character],
  );
const e = escapeHtml;
const iconPaths = {
  home: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10H3Z"/>',
  people:
    '<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3m1-16a3 3 0 0 1 0 6m2 4a5 5 0 0 1 3 5"/>',
  archive: '<path d="M4 8h16v13H4Zm-1-5h18v5H3Zm6 10h6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  logout: '<path d="M9 4H4v16h5m6-12 4 4-4 4m-6-4h10"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  file: '<path d="M14 3H5v18h14V8Zm0 0v5h5M8 12h8m-8 4h6"/>',
  check: '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
};
const icon = (name) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.55" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${iconPaths[name] || iconPaths.file}</svg>`;

function brand(reverse = false) {
  return `<div class="brand ${reverse ? 'brand-reverse' : ''}"><img src="/assets/${reverse ? 'mark-reverse' : 'mark'}.svg" alt="易方 A1 折方标志"><div class="brand-type"><span class="brand-cn">易方</span><span class="brand-en">MODUS</span></div></div>`;
}

function toast(message) {
  const element = document.querySelector('#toast');
  element.textContent = message;
  element.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove('show'), 3800);
}

async function api(path, method = 'GET', body) {
  const response = await fetch(`/api${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && state.user && path !== '/login') {
      state.user = null;
      state.data = null;
      if (modal.open) modal.close();
      renderAuth(false);
    }
    throw new Error(result.error || '请求未完成，请重试');
  }
  return result;
}

function addDays(date, amount) {
  const result = new Date(`${date}T12:00:00Z`);
  result.setUTCDate(result.getUTCDate() + amount);
  return result.toISOString().slice(0, 10);
}

function dayCount(start, end) {
  return Math.round((Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86400000);
}

function dateLabel(date, full = false) {
  if (!date) return '待安排';
  const [year, month, day] = date.split('-').map(Number);
  return full
    ? `${year}.${String(month).padStart(2, '0')}.${String(day).padStart(2, '0')}`
    : `${month} 月 ${day} 日`;
}

function ownerName(id) {
  return state.data.users.find((user) => user.id === id)?.name || '未分配';
}

function taskSource(task) {
  if (task.project_id) return state.data.projects.find((project) => project.id === task.project_id);
  if (task.opportunity_id)
    return state.data.opportunities.find((opportunity) => opportunity.id === task.opportunity_id);
  return { name: task.kind === 'long' ? '长期事项' : '日常事项', color: '#6C738D' };
}

function taskDates(task) {
  if (!task.start_date || !task.end_date) return '日期待定';
  if (task.start_date === task.end_date)
    return `${dateLabel(task.start_date)}${task.time ? ` ${task.time}` : ' · 全天'}`;
  return `${dateLabel(task.start_date)} — ${dateLabel(task.end_date)} · ${dayCount(task.start_date, task.end_date) + 1} 天`;
}

function userOptions(selected) {
  return state.data.users
    .map(
      (user) =>
        `<option value="${user.id}" ${user.id === selected ? 'selected' : ''}>${e(user.name)}${user.id === state.user.id ? '（我）' : ''}</option>`,
    )
    .join('');
}

function colors(selected) {
  return `<div class="color-options">${palette.map((color, index) => `<label class="color-option" style="--item-color:${color}"><input type="radio" name="color" value="${color}" ${color === selected ? 'checked' : ''} aria-label="颜色 ${index + 1}"></label>`).join('')}</div>`;
}

async function refresh() {
  state.data = await api('/state');
  if (!state.archiveDate) state.archiveDate = state.data.today;
  render();
}

function renderAuth(setup) {
  hideTooltip();
  app.innerHTML = `<div class="auth-shell">
    <aside class="auth-brand-panel">${brand(true)}<div class="auth-statement"><div class="eyebrow">协作工作台</div><h1>把复杂，做对。</h1><p class="english">COMPLEXITY, DONE RIGHT.</p></div><div class="auth-structure" aria-hidden="true"></div><div class="auth-company">深圳市易方化科技有限公司<br>易方 / MODUS</div></aside>
    <main class="auth-main"><form id="auth-form" class="auth-form ${setup ? 'setup' : ''}" data-form="${setup ? 'setup' : 'login'}">
      <h1>${setup ? '设置两人账号' : '进入工作台'}</h1>
      <p class="auth-intro">${setup ? '为你和合伙人分别设置登录账号，开始整理项目与日常工作。' : '登录后，查看今天的安排与项目进展。'}</p>
      ${setup ? `<div class="setup-grid">${[0, 1].map((index) => `<section class="setup-person"><h2>${index === 0 ? '我的账号' : '合伙人的账号'}</h2><label class="field">姓名<input name="name${index}" required maxlength="40" autocomplete="name" placeholder="${index === 0 ? '你的姓名' : '合伙人的姓名'}"></label><label class="field">登录账号<input name="username${index}" required maxlength="40" autocomplete="off" pattern="[A-Za-z0-9_.@-]+" placeholder="英文字母或数字"></label><label class="field">密码<input name="password${index}" type="password" required minlength="8" maxlength="128" autocomplete="new-password" placeholder="至少 8 位"></label></section>`).join('')}</div><label class="checkbox-field"><input type="checkbox" name="demo" checked>导入示例项目和事项，方便体验四个页面。</label>` : `<label class="field">登录账号<input name="username" autocomplete="username" required maxlength="40" placeholder="输入你的账号"></label><label class="field">密码<input name="password" type="password" autocomplete="current-password" required maxlength="128" placeholder="输入密码"></label>`}
      <p class="form-error" role="alert"></p><button type="submit" class="button primary auth-submit">${setup ? '创建工作台' : '登录'}</button>
      <p class="auth-footnote">数据保存在运行此服务的电脑上。</p>
    </form></main></div>`;
}

function render() {
  if (!state.user || !state.data) return;
  hideTooltip();
  const pages = [
    ['home', 'home', '工作首页'],
    ['projects', 'folder', '项目详情'],
    ['opportunities', 'people', '沟通意向'],
    ['archive', 'archive', '工作归档'],
    ['success', 'folder', '成功项目'],
    ['failed', 'archive', '失败项目'],
    ['extra', 'check', '项目外待办事项'],
  ];
  const content = {
    home: renderHome,
    projects: renderProjects,
    opportunities: renderOpportunities,
    archive: renderArchive,
    success: renderSuccessfulProjects,
    failed: renderFailedOpportunities,
    extra: renderExtraTasks,
  };
  app.innerHTML = `<header class="topbar"><div class="brand-group">${brand()}<span class="workspace-label">协作工作台</span></div><div class="topbar-right"><button class="text-button" data-maintenance="network">外网地址</button><button class="text-button" data-maintenance="codex">Codex 接入</button><button class="text-button" data-maintenance="trash">回收站</button><div class="account"><span class="avatar">${e(state.user.name.slice(0, 1))}</span><span>${e(state.user.name)}</span></div><button class="logout" data-action="logout" aria-label="退出登录">${icon('logout')}<span>退出</span></button></div></header>
    <div class="nav-wrap"><nav class="navigation" aria-label="主导航">${pages.map(([page, symbol, name]) => `<button class="nav-item ${state.page === page ? 'active' : ''}" data-page="${page}" ${state.page === page ? 'aria-current="page"' : ''}>${icon(symbol)}${name}</button>`).join('')}</nav></div>
    <main class="main">${content[state.page]()}</main>
    <footer class="footer"><span>深圳市易方化科技有限公司 · 易方 / MODUS</span><span>本机工作台 · 基础版</span></footer>`;
}

function renderHome() {
  const today = state.data.today;
  const weekday = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][
    new Date(`${today}T12:00:00Z`).getUTCDay()
  ];
  const other = state.data.users.find((user) => user.id !== state.user.id);
  const demo =
    state.data.projects.some((project) => project.name.startsWith('示例')) ||
    state.data.opportunities.some((opportunity) => opportunity.name.startsWith('示例'));
  return `<div class="page-head"><div><h1>工作首页</h1><p class="page-subtitle">${dateLabel(today, true)} · ${weekday}</p></div><div class="head-actions"><button class="button primary" data-action="new-task">${icon('plus')}新增事项</button></div></div>
    ${demo ? '<div class="notice">当前包含示例数据，可用于体验任务、项目和沟通意向。新建的真实数据会另外保存。</div>' : ''}
    <div class="home-grid">${taskPanel(state.user, true)}${taskPanel(other, false)}</div>
    ${renderTimeline(today)} `;
}

function taskPanel(user, mine) {
  const tasks = state.data.tasks.filter(
    (task) => task.owner_id === user.id && (state.showDone || !task.done),
  );
  const open = tasks.filter((task) => !task.done).length;
  return `<section class="panel"><div class="panel-head"><h2 class="panel-title">${mine ? '我需要做的事' : '同伴需要做的事'}<span class="count">${open}</span></h2><span class="person">${e(user.name)}</span></div>
    <div class="task-list">${
      tasks.length
        ? tasks
            .map((task) => {
              const source = taskSource(task);
              const overdue =
                !task.done && Boolean(task.end_date) && task.end_date < state.data.today;
              return `<div class="task-row"><input class="task-check" type="checkbox" aria-label="完成 ${e(task.title)}" data-complete="${task.id}" ${task.done ? 'checked' : ''} ${mine ? '' : 'disabled'}><div class="task-content"><button class="task-title" data-action="task-detail" data-id="${task.id}">${e(task.title)}</button><div class="task-meta"><span class="dot" style="--item-color:${source.color}"></span><span>${e(source.name)}</span>${task.kind === 'long' ? '<span>· 长期</span>' : ''}${task.done ? '<span>· 已完成</span>' : ''}</div></div><span class="task-date ${overdue ? 'overdue' : ''}">${overdue ? '已逾期' : task.time || (task.end_date === state.data.today ? '今天' : dateLabel(task.end_date))}</span></div>`;
            })
            .join('')
        : '<div class="empty">暂无待办事项</div>'
    }</div><div class="panel-foot"><span>${mine ? '完成事项后自动进入工作归档' : '各自负责的工作，清楚可见'}</span>${mine ? `<button class="text-button" data-action="toggle-done">${state.showDone ? '隐藏已完成' : '显示已完成'}</button>` : ''}</div></section>`;
}

function renderTimeline(today) {
  const end = addDays(today, 4);
  const tasks = state.data.tasks.filter(
    (task) =>
      task.owner_id === state.user.id &&
      !task.done &&
      task.end_date >= today &&
      task.start_date <= end,
  );
  const sources = new Map();
  tasks.forEach((task) => {
    const source = taskSource(task);
    sources.set(source.name, source);
  });
  return `<section class="panel timeline-panel"><div class="panel-head timeline-heading"><h2>接下来 5 天</h2><span class="timeline-key">${dateLabel(today)} — ${dateLabel(end)} · ${e(state.user.name)}</span></div>
    <div class="timeline-scroll"><div class="timeline-body"><div class="timeline-dates">${Array.from(
      { length: 5 },
      (_, index) => {
        const date = addDays(today, index);
        return `<div class="timeline-day ${index === 0 ? 'today' : ''}"><span class="day-number">${Number(date.slice(8))}</span><span class="day-label">${index === 0 ? '今天' : ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][new Date(`${date}T12:00:00Z`).getUTCDay()]}</span></div>`;
      },
    ).join('')}</div><div class="timeline-rows">${
      tasks.length
        ? tasks
            .map((task) => {
              const source = taskSource(task);
              const first = Math.max(0, dayCount(today, task.start_date));
              const last = Math.min(4, dayCount(today, task.end_date));
              let block;
              if (task.time && task.start_date === task.end_date) {
                const [hours, minutes] = task.time.split(':').map(Number);
                const left = ((first + (hours + minutes / 60) / 24) / 5) * 100;
                block = `<button class="time-point" style="left:${left}%;--item-color:${source.color}" data-action="task-detail" data-id="${task.id}" data-tooltip-task="${task.id}" aria-label="${e(task.title)} ${e(task.time)}"></button><span class="point-caption" style="left:${Math.min(95, Math.max(5, left))}%">${task.time}</span>`;
              } else {
                block = `<button class="time-block" style="left:${first * 20}%;width:${(last - first + 1) * 20}%;--item-color:${source.color}" data-action="task-detail" data-id="${task.id}" data-tooltip-task="${task.id}">${task.start_date < today ? '‹ ' : ''}${e(task.title)}${task.end_date > end ? ' ›' : ''}</button>`;
              }
              return `<div class="timeline-row"><span class="timeline-row-label" title="${e(task.title)}">${e(task.title)}</span><div class="timeline-track">${block}</div></div>`;
            })
            .join('')
        : '<div class="empty">这 5 天暂无安排，可从“新增事项”开始。</div>'
    }</div></div></div>
    <div class="timeline-legend">${[...sources.values()].map((source) => `<span class="legend-item"><span class="legend-swatch" style="--item-color:${source.color}"></span>${e(source.name)}</span>`).join('') || '<span class="muted">安排会按所属项目或意向显示颜色</span>'}</div></section>`;
}

function rangePosition(start, end, baseStart, baseEnd) {
  if (!start || !end || !baseStart || !baseEnd) return { left: 0, width: 0 };
  const total = Math.max(1, dayCount(baseStart, baseEnd) + 1);
  const left = (Math.max(0, dayCount(baseStart, start)) / total) * 100;
  const right = (Math.min(total, dayCount(baseStart, end) + 1) / total) * 100;
  return { left, width: Math.max(0, right - left) };
}

function renderProjects() {
  const filter = state.projectFilter || 'active';
  const projects = state.data.projects.filter(
    (project) =>
      filter === 'all' ||
      (filter === 'archived' ? Boolean(project.completed_at) : !project.completed_at),
  );
  return `<div class="page-head"><div><h1>项目详情</h1><p class="page-subtitle">${projects.length} 个项目 · 八个固定阶段 · 按实际工作成果确认进度</p></div><div class="head-actions"><select class="archive-date" id="project-filter" aria-label="项目状态"><option value="active" ${filter === 'active' ? 'selected' : ''}>进行中项目</option><option value="archived" ${filter === 'archived' ? 'selected' : ''}>已归档项目</option><option value="all" ${filter === 'all' ? 'selected' : ''}>全部项目</option></select><button class="button primary" data-action="new-project">${icon('plus')}新建项目</button></div></div>
    <div class="project-list">${
      projects
        .map((project) => {
          const stages = state.data.stages.filter((stage) => stage.project_id === project.id);
          const tasks = state.data.tasks.filter((task) => task.project_id === project.id);
          const expanded = state.expanded.has(project.id);
          const dated = stages.filter((stage) => stage.start_date && stage.end_date);
          return `<article class="panel project-card" data-project="${project.id}"><div class="project-head"><div><p class="project-label">${e(project.client || '客户待补充')} · ${e(ownerName(project.owner_id))}</p><h2 class="project-name">${e(project.name)}</h2><p class="project-description">${e(project.description)}</p></div><span class="status-tag ${project.completed_at ? 'status-complete' : ''}">${project.completed_at ? '已结束 · 已归档' : '进行中'}</span></div>
        <div class="project-dates"><span>${dateLabel(project.start_date, true)}</span><span>${dateLabel(project.end_date, true)}</span></div>
        <button class="project-overview" data-action="expand-project" data-id="${project.id}" aria-expanded="${expanded}" aria-label="展开 ${e(project.name)} 时间表">${
          dated.length
            ? dated
                .map((stage) => {
                  const range = rangePosition(
                    stage.start_date,
                    stage.end_date,
                    project.start_date,
                    project.end_date,
                  );
                  return `<span class="stage-segment" style="left:${range.left}%;width:${range.width}%;--item-color:${project.color}" title="${e(stage.name)} · ${stage.completed_at ? '已完成' : '进行中'}">${stage.completed_at ? '✓ ' : ''}${e(stage.name)}</span>`;
                })
                .join('')
            : `<span class="stage-segment" style="left:0;width:100%;--item-color:${project.color}">${project.completed_at ? '项目已完成' : '项目完整周期 · 阶段日期待定'}</span>`
        }</button>
        <div class="project-bottom"><span>${stages.filter((stage) => stage.completed_at).length}/${stages.length} 个阶段已完成 · ${tasks.filter((task) => task.done).length}/${tasks.length} 项待办已完成（独立记录）</span><div class="project-actions">${maintenanceButtons('projects', project)}<button class="text-button" data-action="expand-project" data-id="${project.id}">${expanded ? '收起详情' : '展开时间表与资料'}</button></div></div>
        ${expanded ? renderProjectDetails(project, stages, tasks) : ''}</article>`;
        })
        .join('') || '<div class="panel empty">当前分类下暂无项目。</div>'
    }</div>`;
}

function renderProjectDetails(project, stages, tasks) {
  const unassigned = tasks.filter((task) => !stages.some((stage) => stage.id === task.stage_id));
  const groups = stages.map((stage) => ({
    stage,
    tasks: tasks.filter((task) => task.stage_id === stage.id),
  }));
  if (unassigned.length) groups.push({ stage: null, tasks: unassigned });
  return `<div class="project-details">${renderProjectOutcome(project)}
    <div class="detail-section-title"><h3>项目八阶段</h3><span class="muted">阶段完成由实际成果确认，待办单独记录</span></div>
    <div class="phase-grid">${stages.map((stage) => renderPhaseCard(project, stage)).join('')}</div>
    <div class="detail-section-title"><h3>项目时间表与相关待办</h3><button class="button small" data-action="new-task" data-project-id="${project.id}">新增项目待办</button></div>
    <div class="project-task-timetable"><div class="timetable-inner"><div class="timetable-axis"><span>${dateLabel(project.start_date, true)}</span><span>${project.start_date ? dateLabel(addDays(project.start_date, Math.floor(dayCount(project.start_date, project.end_date) / 2)), true) : '日期待定'}</span><span>${dateLabel(project.end_date, true)}</span></div>
    ${groups.map((group) => renderDirectoryTimetable(project, group.stage, group.tasks)).join('')}
    </div></div>
    ${renderProjectFiles(project)}${conversationList('project_id', project.id)}
  </div>`;
}

function renderDirectoryTimetable(project, stage, tasks) {
  const range = stage
    ? rangePosition(stage.start_date, stage.end_date, project.start_date, project.end_date)
    : { left: 0, width: 0 };
  return `<div class="gantt-row gantt-directory"><div class="gantt-row-title">${e(stage?.name || '未关联阶段')}<small>${stage?.completed_at ? '已完成' : stage?.start_date ? taskDates(stage) : '阶段日期待定'}</small></div><div class="gantt-track">${range.width ? `<span class="time-block stage-mini" style="left:${range.left}%;width:${range.width}%;--item-color:${project.color}"></span>` : ''}</div></div>
    ${tasks
      .map((task) => {
        const position = rangePosition(
          task.start_date,
          task.end_date,
          project.start_date,
          project.end_date,
        );
        return `<div class="gantt-row gantt-child"><div class="gantt-row-title"><button class="text-button" data-action="task-detail" data-id="${task.id}">${e(task.title)}</button><small>${e(ownerName(task.owner_id))} · ${task.done ? '已完成' : '待完成'}</small></div><div class="gantt-track">${position.width ? `<button class="time-block ${task.done ? 'done' : ''}" style="left:${position.left}%;width:${position.width}%;--item-color:${project.color}" data-action="task-detail" data-id="${task.id}" data-tooltip-task="${task.id}" aria-label="${e(task.title)}"></button>` : '<span class="muted">日期待定或位于项目周期外</span>'}</div></div>`;
      })
      .join('')}`;
}

function renderProjectFiles(project) {
  const sections = state.data.sections.filter((section) => section.project_id === project.id);
  return `<div class="detail-section-title"><h3>项目资料</h3><button class="button small" data-action="new-section" data-id="${project.id}">${icon('plus')}添加区域</button></div><div class="file-grid">${sections
    .map((section) => {
      const files = state.data.documents.filter((document) => document.section_id === section.id);
      return `<section class="file-section"><div class="file-section-head"><h3>${e(section.name)} <small class="muted">${files.length}</small></h3><div class="file-section-actions"><button class="text-button" data-maintenance="edit" data-table="sections" data-id="${section.id}">编辑</button><button class="icon-button" data-action="upload" data-id="${section.id}" aria-label="上传到 ${e(section.name)}" title="上传文件">${icon('upload')}</button>${!files.length ? `<button class="icon-button" data-action="remove-section" data-id="${section.id}" aria-label="移除空区域 ${e(section.name)}" title="移除空区域">${icon('trash')}</button>` : ''}</div></div>${files.length ? files.map((file) => `<a class="file-item" href="/api/documents/${file.id}" title="${e(file.name)} · ${e(ownerName(file.owner_id))}">${icon('file')}<span>${e(file.name)}</span><small>${formatSize(file.size)}</small></a><div class="file-maintenance">${maintenanceButtons('documents', file)}</div>`).join('') : '<p class="file-empty">暂无文件，点击上传即可归入此区域。</p>'}</section>`;
    })
    .join('')}</div>`;
}

function renderOpportunities() {
  return renderOpportunityBoard();
}

function renderArchive() {
  const other = state.data.users.find((user) => user.id !== state.user.id);
  return `<div class="page-head"><div><h1>工作归档</h1><p class="page-subtitle">每一项完成的工作，都有记录</p></div><label class="head-actions"><span class="muted">完成日期</span><input type="date" class="archive-date" id="archive-date" aria-label="归档日期" value="${e(state.archiveDate)}"></label></div><div class="archive-grid">${archivePanel(state.user, true)}${archivePanel(other, false)}</div>${renderArchivedProjects()}`;
}

function archivePanel(user, mine) {
  const completed = state.data.completions.filter(
    (item) => item.owner_id === user.id && item.completed_at.slice(0, 10) === state.archiveDate,
  );
  return `<section class="panel"><div class="panel-head"><h2 class="panel-title">${mine ? '我完成的工作' : '同伴完成的工作'}<span class="count">${completed.length}</span></h2><span class="person">${e(user.name)}</span></div><div class="archive-list">${
    completed.length
      ? completed
          .map((record) => {
            const task = state.data.tasks.find((item) => item.id === record.task_id);
            const source = taskSource(task);
            return `<article class="archive-item"><div class="archive-title"><span class="complete-mark">${icon('check')}</span><button data-action="task-detail" data-id="${task.id}">${e(task.title)}</button></div><div class="task-meta"><span>${e(source.name)}</span><span>· ${record.completed_at.slice(11, 16)} 完成</span></div></article>`;
          })
          .join('')
      : '<div class="empty">这一天还没有完成记录。<br>在待办中勾选完成后，会自动出现在这里。</div>'
  }</div></section>`;
}

function formatSize(size) {
  return size >= 1048576
    ? `${(size / 1048576).toFixed(1)} MB`
    : `${Math.max(1, Math.round(size / 1024))} KB`;
}

function openModal(title, body) {
  hideTooltip();
  modal.innerHTML = `<div class="modal-head"><h2>${e(title)}</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon('close')}</button></div><div class="modal-body">${body}</div>`;
  if (typeof enhanceMaintenanceModal === 'function') enhanceMaintenanceModal();
  if (!modal.open) modal.showModal();
}

function formFooter(label = '保存') {
  return `<p class="form-error" role="alert"></p><div class="modal-actions"><button type="button" class="button" data-action="close-modal">取消</button><button type="submit" class="button primary">${label}</button></div>`;
}

function newTask(projectId = '', stageId = '') {
  const today = state.data.today;
  openModal(
    '新增事项',
    `<form data-form="task"><div class="form-grid"><label class="field full">事项名称<input name="title" required maxlength="300" placeholder="例如：完成项目需求书"></label><label class="field">负责人<select name="owner_id">${userOptions(state.user.id)}</select></label><label class="field">事项类型<select name="kind"><option value="daily">具体事项</option><option value="long">长期事项</option></select></label><label class="field full">所属项目或意向<select name="source"><option value="">项目外待办事项</option><optgroup label="项目">${state.data.projects.map((project) => `<option value="p:${project.id}" ${project.id === Number(projectId) ? 'selected' : ''}>${e(project.name)}</option>`).join('')}</optgroup><optgroup label="合作意向">${state.data.opportunities.map((opportunity) => `<option value="o:${opportunity.id}">${e(opportunity.name)}</option>`).join('')}</optgroup></select></label><label class="field">开始日期<input name="start_date" type="date" value="${today}" required></label><label class="field">结束日期<input name="end_date" type="date" value="${today}" required></label><label class="field full">具体时刻（同日安排可填，留空则显示全天）<input name="time" type="time"></label><label class="field full">事项说明<textarea name="description" maxlength="10000" placeholder="具体要做什么，需要准备哪些内容"></textarea></label></div>${formFooter('添加事项')}</form>`,
  );
  updateTaskStageOptions(modal.querySelector('form'), stageId);
}

function newProject() {
  const today = state.data.today;
  openModal(
    '新建项目',
    `<form data-form="project"><label class="field">项目名称<input name="name" required maxlength="300" placeholder="输入项目名称"></label><div class="form-grid"><label class="field">客户或需求方<input name="client" maxlength="300" placeholder="客户名称"></label><label class="field">负责人<select name="owner_id">${userOptions(state.user.id)}</select></label><label class="field">开始日期<input name="start_date" type="date" value="${today}" required></label><label class="field">结束日期<input name="end_date" type="date" value="${addDays(today, 30)}" required></label></div><label class="field">项目说明<textarea name="description" maxlength="10000" placeholder="目标、范围与当前进展"></textarea></label><div class="field">项目颜色${colors(palette[state.data.projects.length % palette.length])}</div>${formFooter('创建项目')}</form>`,
  );
}

function newOpportunity() {
  return opportunityForm();
}

function taskDetail(identifier) {
  const task = state.data.tasks.find((item) => item.id === identifier);
  const source = taskSource(task);
  const record = state.data.completions.find((item) => item.task_id === identifier);
  openModal(
    task.title,
    `<p class="detail-description">${e(task.description || '暂无补充说明')}</p><dl class="detail-meta"><dt>所属</dt><dd>${e(source.name)}</dd><dt>关联阶段</dt><dd>${e(state.data.stages.find((stage) => stage.id === task.stage_id)?.name || (task.project_id ? '未关联阶段' : '日常或意向事项'))}</dd><dt>负责人</dt><dd>${e(ownerName(task.owner_id))}</dd><dt>安排时间</dt><dd>${taskDates(task)}</dd><dt>类型</dt><dd>${task.kind === 'long' ? '长期事项' : '具体事项'}</dd><dt>状态</dt><dd>${task.done ? '已完成' : '待完成'}</dd>${record ? `<dt>完成时间</dt><dd>${dateLabel(record.completed_at.slice(0, 10), true)} ${record.completed_at.slice(11, 16)}</dd>` : ''}</dl><div class="modal-actions">${task.project_id ? `<button class="button" data-action="go-project" data-id="${task.project_id}">查看项目</button>` : task.opportunity_id ? `<button class="button" data-action="opportunity-detail" data-id="${task.opportunity_id}">查看沟通意向</button>` : ''}${maintenanceButtons('tasks', task)}${task.owner_id === state.user.id ? `<button class="button primary" data-action="complete-modal" data-id="${task.id}" data-done="${!task.done}">${task.done ? '恢复为待办' : '标记完成'}</button>` : ''}</div>${conversationList('task_id', task.id)}`,
  );
}

function opportunityDetail(identifier) {
  return openOpportunity(identifier);
}

function hideTooltip() {
  tooltip?.remove();
  tooltip = null;
}

function showTooltip(target) {
  hideTooltip();
  let title;
  let details;
  if (target.dataset.tooltipConversation) {
    const record = state.data.conversations.find(
      (item) => item.id === Number(target.dataset.tooltipConversation),
    );
    title = `沟通详情 · ${record.occurred_at ? formatMoment(record.occurred_at) : '实际时间待补充'}`;
    details = conversationBody(record);
  } else if (target.dataset.tooltipTask) {
    const task = state.data.tasks.find((item) => item.id === Number(target.dataset.tooltipTask));
    title = task.title;
    details = `${e(taskDates(task))}<br>${e(ownerName(task.owner_id))} · ${e(taskSource(task).name)}${task.description ? `<br>${e(task.description.slice(0, 150))}` : ''}`;
  } else {
    const opportunity = state.data.opportunities.find(
      (item) => item.id === Number(target.dataset.tooltipOpportunity),
    );
    title = opportunity.name;
    details = `${e(opportunity.description.slice(0, 180) || '暂无沟通内容')}<br>负责人：${e(ownerName(opportunity.owner_id))}<br>下一步：${e(opportunity.next_step || '待安排')}`;
  }
  tooltip = document.createElement('div');
  tooltip.className = 'task-tooltip';
  tooltip.setAttribute('role', 'tooltip');
  tooltip.innerHTML = `<strong>${e(title)}</strong><div class="tooltip-meta">${details}</div>`;
  document.body.append(tooltip);
  const rectangle = target.getBoundingClientRect();
  const top = rectangle.bottom + 9;
  tooltip.style.left = `${Math.max(12, Math.min(rectangle.left, window.innerWidth - 302))}px`;
  tooltip.style.top = `${top + tooltip.offsetHeight > window.innerHeight - 12 ? Math.max(12, rectangle.top - tooltip.offsetHeight - 9) : top}px`;
}

document.addEventListener('pointerover', (event) => {
  const target = event.target.closest(
    '[data-tooltip-task],[data-tooltip-opportunity],[data-tooltip-conversation]',
  );
  if (
    target &&
    !target.contains(event.relatedTarget) &&
    (!modal.open || target.dataset.tooltipConversation)
  )
    showTooltip(target);
});
document.addEventListener('pointerout', (event) => {
  const target = event.target.closest(
    '[data-tooltip-task],[data-tooltip-opportunity],[data-tooltip-conversation]',
  );
  if (target && !target.contains(event.relatedTarget)) hideTooltip();
});
document.addEventListener('focusin', (event) => {
  if (
    event.target.matches(
      '[data-tooltip-task],[data-tooltip-opportunity],[data-tooltip-conversation]',
    ) &&
    (!modal.open || event.target.dataset.tooltipConversation)
  )
    showTooltip(event.target);
});
document.addEventListener('focusout', hideTooltip);
window.addEventListener('scroll', hideTooltip, true);
modal.addEventListener('close', hideTooltip);

document.addEventListener('click', async (event) => {
  const navigation = event.target.closest('[data-page]');
  if (navigation) {
    state.page = navigation.dataset.page;
    location.hash = state.page;
    render();
    return;
  }
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const id = Number(button.dataset.id);
  try {
    switch (button.dataset.action) {
      case 'logout':
        await api('/logout', 'POST', {});
        state.user = null;
        state.data = null;
        state.csrf = '';
        state.expanded.clear();
        state.showDone = false;
        state.archiveDate = '';
        renderAuth(false);
        break;
      case 'close-modal':
        modal.close();
        break;
      case 'new-task':
        newTask(button.dataset.projectId, button.dataset.stageId);
        break;
      case 'new-project':
        newProject();
        break;
      case 'new-opportunity':
        newOpportunity();
        break;
      case 'task-detail':
        taskDetail(id);
        break;
      case 'opportunity-detail':
        opportunityDetail(id);
        break;
      case 'toggle-done':
        state.showDone = !state.showDone;
        render();
        break;
      case 'expand-project':
        if (state.expanded.has(id)) state.expanded.delete(id);
        else state.expanded.add(id);
        render();
        break;
      case 'go-project':
        modal.close();
        state.page = 'projects';
        state.projectFilter = state.data.projects.find((project) => project.id === id)?.completed_at
          ? 'archived'
          : 'active';
        state.expanded.add(id);
        location.hash = 'projects';
        render();
        document
          .querySelector(`[data-project="${id}"]`)
          ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        break;
      case 'complete-modal':
        button.disabled = true;
        await api(`/tasks/${id}`, 'PATCH', {
          done: button.dataset.done === 'true',
          revision: state.data.tasks.find((task) => task.id === id).revision,
        });
        modal.close();
        await refresh();
        toast(button.dataset.done === 'true' ? '已完成，已记录到工作归档' : '已恢复为待办');
        break;
      case 'new-section':
        openModal(
          '添加文件区域',
          `<form data-form="section"><input type="hidden" name="project_id" value="${id}"><label class="field">区域名称<input name="name" required maxlength="60" placeholder="例如：合同与商务"></label>${formFooter('添加区域')}</form>`,
        );
        break;
      case 'remove-section': {
        const section = state.data.sections.find((item) => item.id === id);
        openModal(
          '移除空区域',
          `<p>移除“${e(section.name)}”这个空区域？</p><form data-form="remove-section"><input type="hidden" name="section_id" value="${id}">${formFooter('移除区域')}</form>`,
        );
        break;
      }
      case 'upload':
        openModal(
          '上传项目文件',
          `<form data-form="upload"><input type="hidden" name="section_id" value="${id}"><label class="field">文件<input type="file" name="file" required></label><p class="muted">单文件上限 20 MB。保存后可在项目资料中下载。</p><label class="field">负责人<select name="owner_id">${userOptions(state.user.id)}</select></label>${formFooter('上传并保存')}</form>`,
        );
        addDocumentStageField(
          modal.querySelector('form'),
          state.data.sections.find((x) => x.id === id).project_id,
        );
        break;
    }
  } catch (error) {
    button.disabled = false;
    toast(error.message);
  }
});

document.addEventListener('change', async (event) => {
  if (event.target.id === 'project-filter') {
    state.projectFilter = event.target.value;
    render();
  }
  if (event.target.id === 'archive-date' && event.target.value) {
    state.archiveDate = event.target.value;
    render();
  }
  if (event.target.matches('[data-complete]')) {
    const checkbox = event.target;
    checkbox.disabled = true;
    try {
      await api(`/tasks/${checkbox.dataset.complete}`, 'PATCH', {
        done: checkbox.checked,
        revision: state.data.tasks.find((task) => task.id === Number(checkbox.dataset.complete))
          .revision,
      });
      await refresh();
      toast(checkbox.checked ? '已完成，已记录到工作归档' : '已恢复为待办');
    } catch (error) {
      checkbox.checked = !checkbox.checked;
      checkbox.disabled = false;
      toast(error.message);
    }
  }
});

document.addEventListener('submit', async (event) => {
  const form = event.target.closest('form[data-form]');
  if (!form) return;
  event.preventDefault();
  const values = pendingDates(Object.fromEntries(new FormData(form)));
  const submit = form.querySelector('[type="submit"]');
  const errorElement = form.querySelector('.form-error');
  errorElement.textContent = '';
  submit.disabled = true;
  try {
    const type = form.dataset.form;
    if (type === 'login' || type === 'setup') {
      const body =
        type === 'login'
          ? values
          : {
              accounts: [0, 1].map((index) => ({
                name: values[`name${index}`],
                username: values[`username${index}`],
                password: values[`password${index}`],
              })),
              demo: values.demo === 'on',
            };
      const result = await api(`/${type}`, 'POST', body);
      state.user = result.user;
      state.csrf = result.csrf;
      state.page = 'home';
      location.hash = 'home';
      await refresh();
      return;
    }
    if (type === 'task') {
      const [sourceType, sourceId] = (values.source || '').split(':');
      values.project_id = sourceType === 'p' ? Number(sourceId) : null;
      values.opportunity_id = sourceType === 'o' ? Number(sourceId) : null;
      values.stage_id = values.stage_id ? Number(values.stage_id) : null;
      await api('/tasks', 'POST', values);
    } else if (type === 'project') {
      const result = await api('/projects', 'POST', values);
      state.expanded.add(result.id);
    } else if (type === 'opportunity') {
      await api('/opportunities', 'POST', values);
    } else if (type === 'stage') {
      await api('/stages', 'POST', values);
    } else if (type === 'section') {
      await api('/sections', 'POST', values);
    } else if (type === 'remove-section') {
      await api(`/sections/${values.section_id}`, 'DELETE');
    } else if (type === 'upload') {
      const file = form.querySelector('[name="file"]').files[0];
      if (!file || !file.size || file.size > 20 * 1024 * 1024)
        throw new Error('请选择大于 0 字节且不超过 20 MB 的文件');
      const content = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result.split(',')[1]);
        reader.onerror = () => reject(new Error('文件读取失败'));
        reader.readAsDataURL(file);
      });
      await api('/documents', 'POST', {
        section_id: Number(values.section_id),
        ...(values.stage_id === 'auto'
          ? {}
          : { stage_id: values.stage_id ? Number(values.stage_id) : null }),
        owner_id: Number(values.owner_id),
        name: file.name,
        content,
      });
    }
    await refresh();
    modal.close();
    toast(type === 'upload' ? '文件已保存到项目资料' : '已保存');
  } catch (error) {
    errorElement.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

window.addEventListener('hashchange', () => {
  const page = location.hash.slice(1);
  if (
    ['home', 'projects', 'opportunities', 'archive', 'success', 'failed', 'extra'].includes(page)
  ) {
    state.page = page;
    render();
  }
});

async function initialize() {
  let dnsSetup = null;
  if (
    location.hash.startsWith('#dns-setup=') &&
    ['127.0.0.1', 'localhost'].includes(location.hostname)
  ) {
    try {
      dnsSetup = JSON.parse(atob(location.hash.slice(11)));
    } catch (_) {
      dnsSetup = null;
    }
    history.replaceState(null, '', location.pathname + '?codex-setup=1');
  }
  try {
    const session = await api('/session');
    state.user = session.user;
    state.csrf = session.csrf || '';
    if (!session.user) return renderAuth(session.needs_setup);
    const page = location.hash.slice(1);
    if (
      ['home', 'projects', 'opportunities', 'archive', 'success', 'failed', 'extra'].includes(page)
    )
      state.page = page;
    await refresh();
    if (dnsSetup && /^[a-f0-9]{64}$/.test(dnsSetup.ticket || '')) {
      await api('/codex/discovery', 'POST', { ...dnsSetup, via_link: true });
      toast('固定发现入口已保存并验证');
    }
    if (new URLSearchParams(location.search).has('codex-setup')) await openCodexUpdates();
  } catch (error) {
    app.innerHTML = `<div class="initial-loading"><h1>工作台暂时无法连接</h1><p>${e(error.message)}</p><p>请确认启动窗口仍在运行，然后刷新页面。</p></div>`;
  }
}

setInterval(async () => {
  if (state.user && !document.hidden && !modal.open) {
    try {
      await refresh();
    } catch (error) {
      toast(error.message);
    }
  }
}, 30000);

initialize();

function renderArchivedProjects() {
  const archived = (state.data.project_archives || []).filter(
    (item) => item.completed_at.slice(0, 10) === state.archiveDate,
  );
  return `<section class="panel project-archive-panel"><div class="panel-head"><h2>已结束项目</h2><span class="muted">${archived.length} 个</span></div><div class="archive-list">${
    archived
      .map((item) => {
        const project = state.data.projects.find((project) => project.id === item.project_id);
        return `<div class="archived-project"><div><h3>${e(project.name)}</h3><p class="task-meta">${e(ownerName(item.owner_id))} · 已按实际成果确认开发完成 · ${item.completed_at.slice(11, 16)} 归档</p></div><button class="button small" data-action="go-project" data-id="${project.id}">查看项目</button></div>`;
      })
      .join('') || '<div class="empty">所选日期暂无结束归档的项目。</div>'
  }</div></section>`;
}

function updateTaskStageOptions(form, selected = '') {
  if (!form?.elements.namedItem('source')) return;
  let field = form.querySelector('.task-stage-field');
  if (!field) {
    field = document.createElement('label');
    field.className = 'field full task-stage-field';
    form.elements.source.closest('.field').after(field);
  }
  const [type, identifier] = form.elements.source.value.split(':');
  const stages =
    type === 'p'
      ? state.data.stages.filter((stage) => stage.project_id === Number(identifier))
      : [];
  field.hidden = type !== 'p';
  field.innerHTML = `关联阶段（可选，不影响阶段完成）<select name="stage_id" ${type === 'p' ? '' : 'disabled'}><option value="">未关联阶段</option>${stages.map((stage) => `<option value="${stage.id}" ${stage.id === Number(selected) ? 'selected' : ''}>${e(stage.name)}</option>`).join('')}</select>`;
}
