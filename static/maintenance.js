"use strict";

const recordLabels = {
  projects: "项目",
  opportunities: "合作意向",
  tasks: "事项",
  stages: "阶段",
  sections: "文件区域",
  documents: "文件",
  opportunity_files: "意向附件",
};

async function openTrash() {
  const info = await api("/trash");
  const count = Object.values(info.items).reduce(
    (sum, records) => sum + records.length,
    0,
  );
  openModal(
    "回收站",
    `<p>移入回收站后保留 ${info.retention_days} 天，期间可以恢复。到期后自动彻底清理；服务正常运行时通常在到期后 1 小时内处理。</p>
    <p class="muted">升级前没有删除时间的记录，从本次升级起保留 30 天。所属项目或意向也在回收站时，子内容随父记录到期清理。成功项目、失败项目和工作归档中的正常记录不参与自动清理。</p>
    <div class="trash-toolbar"><span>共 ${count} 项</span><button class="button small ghost danger" data-maintenance="purge-all" ${count ? "" : "disabled"}>清空回收站</button></div>
    ${info.pending_files ? '<p class="muted">部分已删除附件暂时被占用，系统会自动重试清理。</p>' : ""}
    ${
      count
        ? Object.entries(info.items)
            .map(([table, records]) =>
              records
                .map(
                  (record) =>
                    `<div class="trash-row"><span>${e(recordLabels[table])} · ${e(record.name)}<small class="trash-meta">移入：${e(record.deleted_at.slice(0, 16).replace("T", " "))}<br>到期：${e(record.expires_at.slice(0, 16).replace("T", " "))}${record.retention_basis ? `<br>${e(record.retention_basis)}` : ""}</small></span><div class="trash-row-actions"><button class="button small" data-maintenance="restore" data-table="${table}" data-id="${record.id}">恢复</button><button class="button small ghost danger" data-maintenance="purge" data-table="${table}" data-id="${record.id}">彻底删除</button></div></div>`,
                )
                .join(""),
            )
            .join("")
        : '<p class="muted">回收站为空。</p>'
    }`,
  );
}

async function previewTrashDeletion(selection) {
  const info = await api("/trash/preview", "POST", selection);
  const summary = Object.entries(info.counts)
    .filter(([table]) => table !== "project_model_migrations")
    .map(([table, count]) => `${e(info.labels[table] || table)} ${count} 条`)
    .join("、");
  openModal(
    selection.all ? "确认清空回收站" : "确认彻底删除",
    `<p>以下内容将永久删除，操作后无法从看板恢复：</p>
    <ul>${info.roots
      .slice(0, 20)
      .map(
        (record) =>
          `<li>${e(recordLabels[record.table])} · ${e(record.name)}</li>`,
      )
      .join(
        "",
      )}</ul>${info.roots.length > 20 ? `<p>另有 ${info.roots.length - 20} 项，合计 ${info.roots.length} 项回收记录。</p>` : ""}
    <p class="trash-scope">包括：${summary}。</p><p>同时清理 ${info.file_count} 个不再使用的附件，共 ${formatSize(info.file_bytes)}。</p>
    ${info.retained_links ? '<p class="muted">与这些内容关联的其他正常记录会保留，仅解除对已删除内容的关联。</p>' : ""}${info.shared_files_preserved ? '<p class="muted">仍被其他记录使用的附件会保留。</p>' : ""}
    <form data-maintenance-form="purge" data-all="${selection.all === true}" data-table="${e(selection.table || "")}" data-id="${selection.id || ""}"><input type="hidden" name="preview_token" value="${e(info.preview_token)}"><label class="field">输入“永久删除”以确认<input name="confirm_text" required autocomplete="off" placeholder="永久删除"></label>${formFooter(selection.all ? "确认清空回收站" : "确认彻底删除")}</form>`,
  );
}

function maintenanceButtons(table, record) {
  return `<button class="button small" data-maintenance="edit" data-table="${table}" data-id="${record.id}">编辑${recordLabels[table]}</button>
    <button class="button small ghost danger" data-maintenance="remove" data-table="${table}" data-id="${record.id}">移入回收站</button>`;
}

function conversationList(key, identifier) {
  const conversations = (state.data.conversations || []).filter(
    (item) => item[key] === identifier,
  );
  return `<section class="conversation-section"><div class="detail-section-title"><h3>沟通记录</h3><button class="button small" data-maintenance="conversation" data-key="${key}" data-id="${identifier}">追加沟通</button></div>
    ${conversations.length ? conversations.map((item) => `<details class="conversation-item"><summary>${e(dateLabel(item.created_at.slice(0, 10), true))} · ${e(ownerName(item.owner_id))} · ${e(item.summary || "查看原文")}</summary><p class="detail-description">${e(item.text)}</p></details>`).join("") : '<p class="file-empty">尚无沟通记录，可追加记录或由 Codex 提交。</p>'}</section>`;
}

function enhanceMaintenanceModal() {
  const form = modal.querySelector(
    'form[data-form="task"],form[data-form="project"],form[data-form="stage"]',
  );
  if (!form) return;
  const firstDate = form.querySelector('[name="start_date"]');
  const label = document.createElement("label");
  label.className = "checkbox-field full";
  label.innerHTML =
    '<input type="checkbox" name="schedule_pending">日期待定，先记录事项';
  firstDate.closest(".field").before(label);
}

function setPendingDates(form) {
  const pending = form.querySelector('[name="schedule_pending"]')?.checked;
  for (const name of ["start_date", "end_date", "time"]) {
    const input = form.elements.namedItem(name);
    if (!input) continue;
    input.disabled = Boolean(pending);
    input.required = !pending && name !== "time";
  }
}

function pendingDates(values) {
  if (values.schedule_pending === "on") {
    values.start_date = "";
    values.end_date = "";
    values.time = "";
  }
  return values;
}

function prepareEdit(table, identifier) {
  if (table === "opportunities") return opportunityForm(identifier);
  if (table === "stages") return editPhase(identifier);
  const record = state.data[table].find((item) => item.id === identifier);
  if (!record) throw new Error("记录已变化，请刷新后重试");
  if (table === "tasks") newTask();
  else if (table === "projects") newProject();
  else if (table === "opportunities") newOpportunity();
  else if (table === "sections") {
    openModal(
      "编辑文件区域",
      `<form data-form="section"><label class="field">区域名称<input name="name" required maxlength="60"></label><label class="field">排列顺序<input name="position" type="number" min="0" required></label>${formFooter()}</form>`,
    );
  } else if (table === "documents") {
    openModal(
      "编辑文件",
      `<form data-form="document"><label class="field">文件名称<input name="name" required maxlength="200"></label><label class="field">负责人<select name="owner_id">${userOptions(record.owner_id)}</select></label><label class="field">所在区域<select name="section_id">${state.data.projects
        .map(
          (project) =>
            `<optgroup label="${e(project.name)}">${state.data.sections
              .filter((section) => section.project_id === project.id)
              .map(
                (section) =>
                  `<option value="${section.id}">${e(section.name)}</option>`,
              )
              .join("")}</optgroup>`,
        )
        .join("")}</select></label>${formFooter()}</form>`,
    );
  }
  modal.querySelector(".modal-head h2").textContent =
    `编辑${recordLabels[table]}`;
  const form = modal.querySelector("form");
  form.removeAttribute("data-form");
  form.dataset.maintenanceForm = "edit";
  form.dataset.table = table;
  form.dataset.id = identifier;
  form.dataset.revision = record.revision;
  for (const [name, value] of Object.entries(record)) {
    const input = form.elements.namedItem(name);
    if (input && value !== null) input.value = value;
  }
  if (table === "tasks")
    form.elements.source.value = record.project_id
      ? `p:${record.project_id}`
      : record.opportunity_id
        ? `o:${record.opportunity_id}`
        : "";
  if (table === "tasks") updateTaskStageOptions(form, record.stage_id);
  if (table === "documents")
    addDocumentStageField(form, record.project_id, record.stage_id);
  const pending = form.elements.namedItem("schedule_pending");
  if (pending) pending.checked = !record.start_date;
  setPendingDates(form);
  form.querySelector('[type="submit"]').textContent = "保存修改";
}

document.addEventListener("change", (event) => {
  if (event.target.name === "schedule_pending")
    setPendingDates(event.target.form);
  if (event.target.name === "source") updateTaskStageOptions(event.target.form);
  if (event.target.name === "section_id") {
    const section = state.data.sections.find(
      (x) => x.id === Number(event.target.value),
    );
    if (section) addDocumentStageField(event.target.form, section.project_id);
  }
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-maintenance]");
  if (!button) return;
  const table = button.dataset.table;
  const identifier = Number(button.dataset.id);
  try {
    if (button.dataset.maintenance === "edit") prepareEdit(table, identifier);
    else if (button.dataset.maintenance === "remove") {
      const record = state.data[table].find((item) => item.id === identifier);
      openModal(
        "移入回收站",
        `<p>将“${e(record.title || record.name)}”移入回收站？${table === "projects" || table === "opportunities" ? "相关事项和资料会随之隐藏，恢复后可重新查看。" : "可以从回收站恢复。"}</p><form data-maintenance-form="remove" data-table="${table}" data-id="${identifier}" data-revision="${record.revision}">${formFooter("移入回收站")}</form>`,
      );
    } else if (button.dataset.maintenance === "trash") {
      await openTrash();
    } else if (button.dataset.maintenance === "purge") {
      await previewTrashDeletion({ table, id: identifier });
    } else if (button.dataset.maintenance === "purge-all") {
      await previewTrashDeletion({ all: true });
    } else if (button.dataset.maintenance === "restore") {
      await api(`/restore/${table}/${identifier}`, "POST", {});
      await refresh();
      modal.close();
      toast("已恢复；所属项目也需处于正常状态");
    } else if (button.dataset.maintenance === "conversation") {
      openModal(
        "追加沟通记录",
        `<form data-maintenance-form="conversation"><input type="hidden" name="${e(button.dataset.key)}" value="${identifier}"><label class="field">负责人<select name="owner_id">${userOptions(state.user.id)}</select></label><label class="field">简要结论<input name="summary" maxlength="10000"></label><label class="field">沟通原文与记录<textarea name="text" required maxlength="100000" rows="8" placeholder="记录已确认事实、待确认问题和下一步安排"></textarea></label>${formFooter("保存沟通")}</form>`,
      );
      if (button.dataset.key === "project_id")
        addDocumentStageField(modal.querySelector("form"), identifier, null);
    } else if (button.dataset.maintenance === "network") {
      const network = await api("/network");
      openModal(
        "外网访问地址",
        `<p>${e(network.message)}</p>${network.url ? `<label class="field">当前地址<input id="public-url" type="url" readonly value="${e(network.url)}"></label><div class="modal-actions"><button class="button primary" data-maintenance="copy-public-url">复制地址</button><a class="button" href="${e(network.url)}" target="_blank" rel="noopener">打开外网看板</a></div>` : '<p class="muted">请双击程序目录里的“启动公网共享.cmd”，稍候再查看这里。</p>'}<p class="auth-footnote">临时网址可能随共享进程重启变化。合伙人使用自己的账号登录。</p>`,
      );
    } else if (button.dataset.maintenance === "copy-public-url") {
      const input = document.querySelector("#public-url");
      if (navigator.clipboard) {
        await navigator.clipboard.writeText(input.value);
        toast("地址已复制");
      } else {
        input.select();
        toast("请按 Ctrl+C 复制地址");
      }
    } else if (button.dataset.maintenance === "codex") {
      await openCodexUpdates();
    }
  } catch (error) {
    toast(error.message);
  }
});

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("form[data-maintenance-form]");
  if (!form) return;
  event.preventDefault();
  const submit = form.querySelector('[type="submit"]');
  const errorElement = form.querySelector(".form-error");
  errorElement.textContent = "";
  submit.disabled = true;
  try {
    const values = pendingDates(Object.fromEntries(new FormData(form)));
    const type = form.dataset.maintenanceForm;
    let purgeResult;
    if (type === "purge") {
      if (values.confirm_text !== "永久删除")
        throw new Error("请输入“永久删除”确认本次删除范围");
      const selection =
        form.dataset.all === "true"
          ? { all: true }
          : { table: form.dataset.table, id: Number(form.dataset.id) };
      purgeResult = await api("/trash/purge", "POST", {
        ...selection,
        preview_token: values.preview_token,
        confirm: true,
      });
    } else if (type === "conversation")
      await api("/conversations", "POST", values);
    else {
      values.revision = Number(form.dataset.revision);

      if (type === "edit" && form.dataset.table === "tasks") {
        const [source, identifier] = (values.source || "").split(":");
        values.project_id = source === "p" ? Number(identifier) : null;
        values.opportunity_id = source === "o" ? Number(identifier) : null;
      }
      await api(
        `/${form.dataset.table}/${form.dataset.id}`,
        type === "remove" ? "DELETE" : "PATCH",
        values,
      );
    }
    await refresh();
    modal.close();
    toast(
      type === "purge"
        ? purgeResult.pending_files
          ? "记录已彻底删除，部分附件等待自动重试清理"
          : "已彻底删除并清理附件"
        : type === "remove"
          ? "已移入回收站"
          : "已保存修改",
    );
  } catch (error) {
    errorElement.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});
