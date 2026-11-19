// Giftia Dashboard Modals - Bot Status, Memos and Scheduled Tasks

// 1. Fill energy
window.fillEnergy = async function(bot, group) {
    bot = decodeURIComponent(bot);
    group = decodeURIComponent(group);
    try {
        const res = await window.apiPost("/status/fill_energy", {
            bot_name: bot,
            group_or_user_id: group
        });
        if (res.status === "success") {
            window.showToast(`成功为 ${bot} 充满能量！`);
            window.GiftiaApp.loadBotStatus();
        } else {
            window.showToast(`补充能量失败: ${res.message}`);
        }
    } catch (e) {
        window.showToast(`发生错误: ${e.message}`);
    }
};

// 2. Edit Bot Status
window.addCustomStatusRow = function(key = "", value = "") {
    const list = document.getElementById("edit-custom-status-list");
    if (!list) return;
    const row = document.createElement("div");
    row.className = "custom-status-row";
    row.style.cssText = "display: flex; gap: 8px; align-items: center;";
    row.innerHTML = `
        <input type="text" class="custom-status-key" placeholder="状态名(如:服装)" value="${window.escapeHtml(key)}" style="flex: 1; min-width: 90px; padding: 6px 10px; border-radius: var(--radius-sm, 6px); border: 1px solid var(--border-color, #e2e8f0); background: var(--input-bg, #fff); color: var(--font-primary, inherit);">
        <input type="text" class="custom-status-val" placeholder="状态值(如:日常校服)" value="${window.escapeHtml(value)}" style="flex: 2; padding: 6px 10px; border-radius: var(--radius-sm, 6px); border: 1px solid var(--border-color, #e2e8f0); background: var(--input-bg, #fff); color: var(--font-primary, inherit);">
        <button type="button" class="btn btn-secondary btn-small" onclick="this.closest('.custom-status-row').remove()" style="padding: 4px 8px; color: var(--danger, #ef4444); border-color: rgba(239, 68, 68, 0.3);" title="删除该项">&times;</button>
    `;
    list.appendChild(row);
};

window.openEditStatusModal = function(bot, group, mood, state, memory, action, customStatusJson) {
    bot = decodeURIComponent(bot);
    group = decodeURIComponent(group);
    mood = decodeURIComponent(mood || "");
    state = decodeURIComponent(state || "");
    memory = decodeURIComponent(memory || "");
    action = decodeURIComponent(action || "");

    let customStatus = {};
    if (customStatusJson) {
        try {
            customStatus = JSON.parse(decodeURIComponent(customStatusJson));
        } catch (e) {
            customStatus = {};
        }
    }

    document.getElementById("edit-status-bot").value = bot;
    document.getElementById("edit-status-group").value = group;
    document.getElementById("edit-status-mood").value = mood;
    document.getElementById("edit-status-state").value = state;
    document.getElementById("edit-status-memory").value = memory;
    document.getElementById("edit-status-action").value = action;

    const list = document.getElementById("edit-custom-status-list");
    if (list) {
        list.innerHTML = "";
        const entries = Object.entries(customStatus || {});
        if (entries.length > 0) {
            entries.forEach(([k, v]) => window.addCustomStatusRow(k, v));
        }
    }

    window.openModal("edit-status-modal");
};

window.submitEditStatus = async function() {
    const bot = document.getElementById("edit-status-bot").value;
    const group = document.getElementById("edit-status-group").value;
    const mood = document.getElementById("edit-status-mood").value.trim();
    const state = document.getElementById("edit-status-state").value.trim();
    const memory = document.getElementById("edit-status-memory").value.trim();
    const action = document.getElementById("edit-status-action").value.trim();

    const customStatus = {};
    const rows = document.querySelectorAll("#edit-custom-status-list .custom-status-row");
    rows.forEach(row => {
        const k = row.querySelector(".custom-status-key")?.value?.trim();
        const v = row.querySelector(".custom-status-val")?.value?.trim();
        if (k && v) {
            customStatus[k] = v;
        }
    });

    try {
        const res = await window.apiPost("/status/update", {
            bot_name: bot,
            group_or_user_id: group,
            mood: mood,
            state: state,
            memory: memory,
            action: action,
            custom_status: customStatus
        });
        if (res.status === "success") {
            window.showToast("Bot 状态调整成功！");
            window.closeModal("edit-status-modal");
            window.GiftiaApp.loadBotStatus();
        } else {
            window.showToast(`修改失败: ${res.message}`);
        }
    } catch (e) {
        window.showToast(`发生错误: ${e.message}`);
    }
};

window.deleteBotStatus = function(statusId, bot, group, sessionLabel) {
    bot = decodeURIComponent(bot);
    group = decodeURIComponent(group);
    sessionLabel = decodeURIComponent(sessionLabel);

    window.showConfirm("删除会话状态", `确定删除「${bot} · ${sessionLabel}」的状态记录吗？`, async () => {
        try {
            const res = await window.apiPost("/status/delete", {
                status_id: statusId,
                bot_name: bot,
                group_or_user_id: group
            });
            if (res.status !== "success") throw new Error(res.message || "删除会话状态失败");
            window.showToast("会话状态已删除");
            await window.GiftiaApp.loadBotStatus();
        } catch (e) {
            window.showToast(e.message);
        }
    });
};

// Session memos and scheduled tasks share a conversation picker.
let memoData = null;
let memoRequest = 0;
let memoTimer;
window.taskBoardMainTab = "memo";

function boardSession() {
    return {
        bot_name: document.getElementById("task-board-bot").value,
        group_or_user_id: document.getElementById("task-board-group").value
    };
}

window.openCreateTaskModal = function(memoId = "") {
    const session = boardSession();
    const scheduled = window.taskBoardMainTab === "scheduled";
    const memo = memoId && memoData?.items.find(item => item.memo_id === memoId);
    if (!session.bot_name || !session.group_or_user_id || (memoId && !memo)) return;
    const submit = document.getElementById("create-task-submit");
    if (submit.disabled) return;
    document.getElementById("create-task-form").reset();
    document.getElementById("create-task-type").value = scheduled ? "scheduled" : "memo";
    document.getElementById("edit-memo-id").value = memoId;
    document.getElementById("create-task-bot").value = session.bot_name;
    document.getElementById("create-task-group").value = session.group_or_user_id;
    document.getElementById("create-task-title").textContent = scheduled ? "新建定时任务" : memo ? "续期备忘" : "新增备忘";
    document.getElementById("create-task-session").textContent = `${session.bot_name} · ${session.group_or_user_id}`;
    document.getElementById("create-content-label").textContent = scheduled ? "任务内容" : "备忘内容";
    const content = document.getElementById("create-task-content");
    content.value = memo?.content || "";
    content.readOnly = Boolean(memo);
    document.getElementById("memo-duration-fields").style.display = scheduled ? "none" : "flex";
    document.getElementById("memo-duration-label").textContent = memo ? "延长时长" : "留存时长";
    const duration = document.getElementById("memo-duration");
    duration.value = parseInt(memoData?.default_duration || "24h", 10);
    duration.disabled = scheduled;
    document.getElementById("create-scheduled-task-fields").style.display = scheduled ? "flex" : "none";
    document.getElementById("create-task-time").disabled = !scheduled;
    document.getElementById("create-task-time").required = scheduled;
    submit.textContent = memo ? "续期" : "创建";
    window.openModal("create-task-modal");
    (memo ? duration : content).focus();
};

window.submitCreateTask = async function() {
    const submit = document.getElementById("create-task-submit");
    if (submit.disabled) return;
    const form = document.getElementById("create-task-form");
    if (!form.reportValidity()) return;
    const body = {
        bot_name: document.getElementById("create-task-bot").value,
        group_or_user_id: document.getElementById("create-task-group").value
    };
    const content = document.getElementById("create-task-content").value.trim();
    const scheduled = document.getElementById("create-task-type").value === "scheduled";
    const memoId = document.getElementById("edit-memo-id").value;
    let endpoint;
    if (scheduled) {
        endpoint = "/scheduled_tasks/create";
        body.remind_message = content;
        body.time_expr = document.getElementById("create-task-time").value.trim();
    } else {
        endpoint = memoId ? "/memos/renew" : "/memos/add";
        body.duration = document.getElementById("memo-duration").value + document.getElementById("memo-duration-unit").value;
        if (memoId) body.memo_id = memoId;
        else body.content = content;
    }
    submit.disabled = true;
    try {
        const res = await window.apiPost(endpoint, body);
        if (res.status !== "success") throw new Error(res.message || "操作失败");
        window.showToast(scheduled ? "定时任务已创建" : memoId ? "备忘已续期" : "备忘已新增");
        window.closeModal("create-task-modal");
        if (boardSession().bot_name === body.bot_name && boardSession().group_or_user_id === body.group_or_user_id) {
            await window.refreshTaskBoardModal();
        }
        if (!scheduled) window.GiftiaApp.loadBotStatus();
    } catch (e) {
        window.showToast(e.message);
    } finally {
        submit.disabled = false;
    }
};

window.setTaskBoardMainTab = function(tab) {
    window.taskBoardMainTab = tab;
    const scheduled = tab === "scheduled";
    document.querySelector("#task-board-modal .modal-card").classList.toggle("showing-memos", !scheduled);
    document.querySelectorAll(".task-board-main-tab").forEach(button => {
        button.classList.toggle("active", button.dataset.mainTab === tab);
    });
    document.getElementById("memo-panel").style.display = scheduled ? "none" : "block";
    document.getElementById("scheduled-task-board-panel").style.display = scheduled ? "block" : "none";
    document.getElementById("task-board-clear-btn").style.display = scheduled ? "" : "none";
    const create = document.getElementById("task-board-create-btn");
    create.textContent = scheduled ? "新建定时任务" : "新增备忘";
    create.disabled = !scheduled && (!memoData?.enabled || memoData.items.length >= memoData.limit);
};

window.openTaskBoardModal = async function(bot, group, tab = "memo") {
    bot = decodeURIComponent(bot);
    group = decodeURIComponent(group);
    document.getElementById("task-board-bot").value = bot;
    document.getElementById("task-board-group").value = group;
    document.getElementById("task-board-title").textContent = `会话备忘录与定时任务 · ${bot}`;
    memoData = null;
    document.getElementById("memo-list").textContent = "加载备忘录中…";
    document.getElementById("scheduled-task-list").textContent = "加载定时任务中…";
    window.setTaskBoardMainTab(tab);
    window.openModal("task-board-modal");
    await window.refreshTaskBoardModal();
};

window.refreshTaskBoardModal = async function() {
    await Promise.all([refreshMemos(), window.refreshScheduledTasks()]);
};

async function refreshMemos() {
    const request = ++memoRequest;
    clearTimeout(memoTimer);
    const list = document.getElementById("memo-list");
    try {
        const res = await window.apiGet("/memos", boardSession());
        if (request !== memoRequest) return;
        if (res.status !== "success") throw new Error(res.message || "加载失败");
        memoData = res.data;
        const items = memoData.items;
        updateTabCount('.task-board-main-tab[data-main-tab="memo"]', items.length);
        window.setTaskBoardMainTab(window.taskBoardMainTab);
        if (!items.length) {
            list.innerHTML = `<div class="task-board-empty-state">${memoData.enabled ? "暂无备忘" : "此 Bot 的备忘录已关闭"}</div>`;
            return;
        }
        list.innerHTML = items.map(item => `
            <article class="task-board-card" data-memo-id="${window.escapeHtml(item.memo_id)}">
                <p class="memo-content">${window.escapeHtml(item.content)}</p>
                <div class="task-card-footer">
                    <div class="task-card-dates">
                        <span>创建于 ${window.escapeHtml(item.created_at)}</span>
                        <span>剩余 ${window.escapeHtml(item.remaining)}</span>
                    </div>
                    <div class="task-card-actions">
                        <button class="task-card-btn task-card-btn-primary" onclick="window.openCreateTaskModal(this.closest('article').dataset.memoId)">续期</button>
                        <button class="task-card-btn task-card-btn-danger" onclick="window.deleteMemo(this.closest('article').dataset.memoId)">删除</button>
                    </div>
                </div>
            </article>`).join("");
        // Refresh when the next displayed unit changes or a memo expires.
        const delays = items.map(({remaining_seconds: seconds}) => {
            if (seconds < 60) return seconds;
            const unit = seconds >= 86400 ? 86400 : seconds >= 3600 ? 3600 : 60;
            return seconds % unit + 1;
        });
        memoTimer = setTimeout(() => {
            if (document.getElementById("task-board-modal").classList.contains("show")) refreshMemos();
        }, Math.min(...delays) * 1000);
    } catch (e) {
        if (request === memoRequest) list.textContent = `加载备忘录失败：${e.message}`;
    }
}

window.deleteMemo = function(memoId) {
    const session = boardSession();
    window.showConfirm("删除备忘", "确定删除这条备忘吗？", async () => {
        try {
            const res = await window.apiPost("/memos/delete", { ...session, memo_id: memoId });
            if (res.status !== "success") throw new Error(res.message || "删除失败");
            window.showToast("备忘已删除");
            await refreshMemos();
            window.GiftiaApp.loadBotStatus();
        } catch (e) {
            window.showToast(e.message);
        }
    });
};

function updateTabCount(selector, count) {
    const tab = document.querySelector(selector);
    if (!tab) return;
    const countBadge = tab.querySelector('.task-tab-count');
    if (countBadge) {
        countBadge.textContent = count;
    } else {
        const span = document.createElement('span');
        span.className = 'task-tab-count';
        span.textContent = count;
        tab.appendChild(span);
    }
}

window.fetchAndRenderScheduledTasks = async function(bot, group, scheduledList) {
    scheduledList = scheduledList || document.getElementById("scheduled-task-list");
    if (!bot || !group || !scheduledList) return;
    try {
        const schedRes = await window.apiGet("/scheduled_tasks", { bot_name: bot, group_or_user_id: group });
        if (boardSession().bot_name !== bot || boardSession().group_or_user_id !== group) return;
        if (schedRes.status === "success" && schedRes.data) {
            const items = schedRes.data.items || [];
            window.renderScheduledTaskList(items);
            updateTabCount('.task-board-main-tab[data-main-tab="scheduled"]', items.length);
        } else {
            scheduledList.innerHTML = `<div class="task-board-empty">加载定时任务失败: ${window.escapeHtml(schedRes.message || "")}</div>`;
        }
    } catch (e) {
        scheduledList.innerHTML = `<div class="task-board-empty">加载定时任务失败: ${window.escapeHtml(e.message)}</div>`;
    }
};

window.refreshScheduledTasks = async function() {
    const bot = document.getElementById("task-board-bot").value;
    const group = document.getElementById("task-board-group").value;
    await window.fetchAndRenderScheduledTasks(bot, group);
};

window.renderScheduledTaskList = function(items) {
    const list = document.getElementById("scheduled-task-list");
    if (!list) return;

    if (!items || items.length === 0) {
        list.innerHTML = `
            <div class="task-board-empty-state">
                <svg class="task-board-empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                    <circle cx="12" cy="12" r="10"></circle>
                    <polyline points="12 6 12 12 16 14"></polyline>
                </svg>
                <div class="task-board-empty-title">暂无定时任务</div>
                <div class="task-board-empty-desc">当前会话没有任何正在排队的定时提醒或定时任务。</div>
            </div>
        `;
        return;
    }

    list.innerHTML = items.map(job => {
        const taskIdArg = encodeURIComponent(job.task_id || "");
        const creator = job.user_name || job.user_id || "系统/LLM";
        const contentText = job.remind_message || job.func_name || "";

        return `
            <div class="task-board-card scheduled-task-card" data-task-id="${window.escapeHtml(job.task_id || "")}">
                <div class="task-card-header">
                    <div class="task-card-status-badge">
                        <span class="badge badge-info">定时任务</span>
                    </div>
                    <div class="task-card-meta-right">
                        <span>ID: ${window.escapeHtml(job.task_id || "")}</span>
                        <span>触发发起人: ${window.escapeHtml(creator)}</span>
                    </div>
                </div>
                <div class="task-card-body">
                    <textarea class="task-card-textarea scheduled-task-content-input" placeholder="提醒/任务内容...">${window.escapeHtml(contentText)}</textarea>
                </div>
                <div class="task-card-footer">
                    <div class="task-card-footer-left">
                        <div class="task-card-control-group">
                            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
                            时间规则:
                            <input class="task-card-input-text scheduled-time-input" type="text" value="${window.escapeHtml(job.time_expr || '')}" placeholder="Cron (如 0 8 * * *) 或 ISO时间">
                        </div>
                        <div class="task-card-dates">
                            <span>下次执行: ${window.formatDate(job.next_run_time)}</span>
                        </div>
                    </div>
                    <div class="task-card-actions">
                        <button class="task-card-btn task-card-btn-primary" onclick="window.saveScheduledTaskItem(this, '${taskIdArg}')">
                            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"></path><polyline points="17 21 17 13 7 13 7 21"></polyline><polyline points="7 3 7 8 15 8"></polyline></svg>
                            保存
                        </button>
                        <button class="task-card-btn task-card-btn-danger" onclick="window.deleteScheduledTaskItem('${taskIdArg}')">
                            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>
                            删除
                        </button>
                    </div>
                </div>
            </div>
        `;
    }).join("");
};

function findTaskCard(target, taskIdArg, cardSelector = ".task-board-card") {
    let card = null;
    let taskId = "";
    if (target && target.nodeType) {
        card = target.closest(cardSelector);
        taskId = card ? (card.getAttribute("data-task-id") || "") : decodeURIComponent(taskIdArg || "");
    } else if (typeof target === "string") {
        taskId = decodeURIComponent(target);
        card = document.querySelector(`${cardSelector}[data-task-id="${CSS.escape(taskId)}"]`);
    } else if (typeof target === "number") {
        taskId = decodeURIComponent(taskIdArg || "");
        const legacyId = cardSelector.includes("scheduled") ? `scheduled-task-content-${target}` : `task-board-content-${target}`;
        const legacyEl = document.getElementById(legacyId);
        if (legacyEl) card = legacyEl.closest(cardSelector);
    }
    if (!card && taskId) {
        card = document.querySelector(`${cardSelector}[data-task-id="${CSS.escape(taskId)}"]`);
    }
    return { card, taskId };
}

window.saveScheduledTaskItem = async function(target, taskIdArg) {
    const bot = document.getElementById("task-board-bot").value;
    const group = document.getElementById("task-board-group").value;
    
    const { card, taskId } = findTaskCard(target, taskIdArg, ".scheduled-task-card");
    if (!card) return;

    const contentEl = card.querySelector(".scheduled-task-content-input") || card.querySelector(".task-card-textarea");
    const timeEl = card.querySelector(".scheduled-time-input");
    if (!contentEl || !timeEl) return;

    const remindMessage = contentEl.value.trim();
    const timeExpr = timeEl.value.trim();

    if (!remindMessage) {
        window.showToast("定时任务提醒内容不能为空");
        return;
    }
    if (!timeExpr) {
        window.showToast("时间规则不能为空");
        return;
    }

    try {
        const res = await window.apiPost("/scheduled_tasks/update", {
            bot_name: bot,
            group_or_user_id: group,
            task_id: taskId,
            remind_message: remindMessage,
            time_expr: timeExpr
        });
        if (res.status === "success") {
            window.showToast("定时任务已更新");
            await window.refreshScheduledTasks();
        } else {
            window.showToast(`更新失败: ${res.message}`);
        }
    } catch (e) {
        window.showToast(`发生错误: ${e.message}`);
    }
};

window.deleteScheduledTaskItem = function(taskIdEncoded) {
    const bot = document.getElementById("task-board-bot").value;
    const group = document.getElementById("task-board-group").value;
    const taskId = decodeURIComponent(taskIdEncoded);

    window.showConfirm("确认删除定时任务", "确定要删除这条定时任务吗？此操作无法撤销。", async () => {
        try {
            const res = await window.apiPost("/scheduled_tasks/delete", {
                bot_name: bot,
                group_or_user_id: group,
                task_id: taskId
            });
            if (res.status === "success") {
                window.showToast("定时任务已删除");
                await window.refreshTaskBoardModal();
            } else {
                window.showToast(`删除失败: ${res.message}`);
            }
        } catch (e) {
            window.showToast(`发生错误: ${e.message}`);
        }
    });
};

window.clearScheduledTasks = function() {
    const bot = document.getElementById("task-board-bot").value;
    const group = document.getElementById("task-board-group").value;

    if (!bot || !group) return;

    window.showConfirm("确认清空定时任务", "确定要清空当前会话下的所有定时任务吗？此操作无法撤销。", async () => {
        try {
            const res = await window.apiPost("/scheduled_tasks/clear", {
                bot_name: bot,
                group_or_user_id: group
            });
            if (res.status === "success") {
                window.showToast(res.message || "定时任务已清空");
                await window.refreshTaskBoardModal();
            } else {
                window.showToast(`清空失败: ${res.message}`);
            }
        } catch (e) {
            window.showToast(`发生错误: ${e.message}`);
        }
    });
};
