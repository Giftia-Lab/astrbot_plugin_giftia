import { state } from './state.js';

const statusLabels = {
    unknown: '未检测', healthy: '健康', degraded: '待观察', cooldown: '冷却中',
    probe_due: '待恢复试探', probing: '试探中', missing: '未注册',
    disabled: '已手动停用', unmanaged: '未启用管理'
};
const reasonLabels = {
    connection: '连接失败或请求超时', server_error: '供应商服务异常',
    rate_limit: '请求限流', quota: '余额或配额不足',
    authentication: '鉴权或权限异常', provider_missing: '供应商或模型不存在',
    invalid_probe_response: '探测回复未通过严格格式校验'
};
let models = [];
let managementEnabled = true;
let promptSuffixProviderIds = new Set();
let promptSuffixDirty = false;
let savingSettings = false;
let loadVersion = 0;
let refreshTimer = null;
const testing = new Set();
const expandedUses = new Set();
let usageObserver;
const escape = value => window.escapeHtml(String(value ?? ''));
const formatTime = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {hour12: false}) : '—';

function applyModelSnapshot(result, errorMessage, savedPromptSuffix = false) {
    if (result?.status !== 'success' || !Array.isArray(result.data?.models) || typeof result.data.enabled !== 'boolean') {
        throw new Error(result?.message || errorMessage);
    }
    models = result.data.models;
    managementEnabled = result.data.enabled;
    if (!promptSuffixDirty || savedPromptSuffix) {
        const settings = result.data.prompt_suffix;
        const input = document.getElementById('model-prompt-suffix');
        const text = settings?.text ?? '';
        if (input && input.value !== text) input.value = text;
        promptSuffixProviderIds = new Set(settings?.provider_ids ?? []);
        promptSuffixDirty = false;
    }
}

function updateUsageOverflow() {
    const container = document.getElementById('model-health-list');
    if (!container) return;
    for (const usage of container.querySelectorAll('.model-health-usage')) {
        const list = usage.querySelector('.model-health-uses');
        const button = usage.querySelector('[data-model-uses]');
        const expanded = expandedUses.has(button.dataset.modelUses);
        list.classList.toggle('is-expanded', expanded);
        button.hidden = !expanded;
        if (!expanded) {
            button.hidden = list.scrollHeight <= list.clientHeight + 1
                && [...list.children].every(label => label.scrollWidth <= label.clientWidth + 1);
        }
        button.textContent = expanded ? '收起' : '展开';
        button.setAttribute('aria-expanded', String(expanded));
    }
}

function renderModels() {
    const container = document.getElementById('model-health-list');
    if (!container) return;
    const toggle = document.getElementById('model-health-enabled');
    if (toggle) {
        toggle.checked = managementEnabled;
        toggle.disabled = savingSettings;
    }
    const promptInput = document.getElementById('model-prompt-suffix');
    const promptSave = document.getElementById('model-prompt-suffix-save');
    if (promptInput) promptInput.disabled = savingSettings;
    if (promptSave) promptSave.disabled = savingSettings;
    const hint = document.getElementById('model-health-hint');
    if (hint) hint.textContent = managementEnabled
        ? '自动跳过故障模型，并在冷却后探测恢复。'
        : '已关闭自动冷却和所有探测；手动停用的模型仍会跳过。';
    document.getElementById('model-health-count').textContent = `已配置 ${models.length} 个供应商`;
    if (!models.length) {
        container.innerHTML = '<tr class="model-health-empty"><td colspan="6" class="empty-state">尚未配置对话模型。请在机器人管理或插件设置中选择供应商。</td></tr>';
        return;
    }
    container.innerHTML = models.map((model, index) => {
        const providerId = escape(model.provider_id);
        const name = model.name || model.provider_id;
        const busy = testing.has(model.provider_id) || model.status === 'probing';
        const disabled = model.enabled === false;
        const status = disabled ? 'disabled' : !model.registered ? 'missing'
            : (!managementEnabled ? 'unmanaged' : (busy ? 'probing' : model.status));
        const color = status === 'healthy' ? 'success' : (['cooldown', 'missing', 'disabled'].includes(status) ? 'danger' : 'secondary');
        const detail = status === 'cooldown' ? `约 ${model.retry_in_seconds} 秒后可试探`
            : status === 'probe_due' ? '等待后台连通性测试'
            : status === 'missing' ? '供应商配置尚未加载或已删除'
            : status === 'disabled' ? '所有流程直接跳过，不发起探测'
            : status === 'unmanaged' ? '按原有顺序调用，不自动探测'
            : (reasonLabels[model.reason] || '');
        return `<tr>
            <td class="model-health-name" data-label="供应商 / 模型">
                <strong>${escape(name)}</strong>
                ${name !== model.provider_id ? `<div class="model-health-provider-id muted-text">${providerId}</div>` : ''}
                <label class="model-prompt-suffix-option">
                    <input type="checkbox" data-prompt-suffix-provider="${providerId}" aria-label="为 ${providerId} 注入防误判提示词" ${promptSuffixProviderIds.has(model.provider_id) ? 'checked' : ''} ${savingSettings ? 'disabled' : ''}>
                    注入提示词
                </label>
            </td>
            <td class="model-health-usage-cell" data-label="使用位置"><div class="model-health-usage">
                <div class="model-health-uses" id="model-health-uses-${index}">${model.uses.map(use => `<span class="badge badge-secondary">${escape(use)}</span>`).join('')}</div>
                <button class="model-health-uses-toggle" type="button" data-model-uses="${providerId}" aria-controls="model-health-uses-${index}" aria-expanded="false" hidden>展开</button>
            </div></td>
            <td class="model-health-status" data-label="健康状态"><span class="badge badge-${color}">${escape(statusLabels[status] || status)}</span>${detail ? `<div class="muted-text model-health-detail">${escape(detail)}</div>` : ''}${model.reason && detail !== reasonLabels[model.reason] ? `<div class="muted-text">${escape(reasonLabels[model.reason] || '')}</div>` : ''}${model.failures ? `<div class="muted-text">连续失败 ${model.failures} 次</div>` : ''}</td>
            <td class="model-health-attempt" data-label="最近请求 / 耗时">${escape(formatTime(model.last_attempt))}<div class="muted-text">${model.last_latency_ms == null ? '—' : `${escape(model.last_latency_ms)} ms`}</div></td>
            <td class="model-health-success" data-label="最近成功">${escape(formatTime(model.last_success))}</td>
            <td class="model-health-action" data-label="操作"><div class="model-health-actions">
                <button class="btn btn-small btn-secondary" data-test-model="${providerId}" ${busy || disabled || !managementEnabled || !model.registered || savingSettings ? 'disabled' : ''}>${busy ? '测试中…' : '测试'}</button>
                <button class="btn btn-small ${disabled ? 'btn-secondary' : 'btn-danger'}" data-toggle-model="${providerId}" ${savingSettings ? 'disabled' : ''}>${disabled ? '启用' : '停用'}</button>
            </div></td>
        </tr>`;
    }).join('');
    if (!usageObserver) {
        usageObserver = new ResizeObserver(updateUsageOverflow);
        usageObserver.observe(container);
    }
    updateUsageOverflow();
}

export async function loadModelHealth() {
    clearTimeout(refreshTimer);
    const version = ++loadVersion;
    const error = document.getElementById('model-health-error');
    const refresh = document.getElementById('model-health-refresh');
    if (refresh) refresh.disabled = true;
    try {
        const result = await window.apiGet('/models/health');
        if (version !== loadVersion) return;
        applyModelSnapshot(result, '模型状态加载失败');
        if (error) error.textContent = '';
        renderModels();
    } catch (err) {
        if (version === loadVersion && error) error.textContent = err.message || '模型状态加载失败';
    } finally {
        if (version === loadVersion) {
            if (refresh) refresh.disabled = false;
            refreshTimer = setTimeout(() => {
                if (state.activeTab === 'model-health') loadModelHealth();
            }, 5000);
        }
    }
}

export async function testModelConnection(providerId) {
    const model = models.find(item => item.provider_id === providerId);
    if (testing.has(providerId) || !managementEnabled || model?.enabled === false || model?.registered === false) return;
    testing.add(providerId);
    ++loadVersion;
    renderModels();
    try {
        const result = await window.apiPost('/models/test', {provider_id: providerId});
        const success = result?.status === 'success';
        window.showToast(result?.message || (success ? '连通性测试通过，已恢复共享健康状态' : '连通性测试失败'), success ? 'success' : 'error');
    } catch {
        window.showToast('连通性测试请求失败，请刷新查看模型状态', 'error');
    } finally {
        testing.delete(providerId);
        if (state.activeTab === 'model-health') await loadModelHealth();
    }
}

export async function saveModelHealthSettings(settings, {promptSuffix = false} = {}) {
    if (savingSettings) return;
    savingSettings = true;
    ++loadVersion;
    renderModels();
    const errorMessage = promptSuffix ? '保存防误判提示词失败' : '保存模型状态设置失败';
    try {
        const endpoint = promptSuffix ? '/models/prompt-suffix' : '/models/settings';
        const result = await window.apiPost(endpoint, settings);
        applyModelSnapshot(result, errorMessage, promptSuffix);
        window.showToast(promptSuffix ? '提示词设置已保存' : '模型状态设置已保存', 'success');
    } catch (err) {
        window.showToast(err.message || errorMessage, 'error');
    } finally {
        savingSettings = false;
        renderModels();
        if (state.activeTab === 'model-health') await loadModelHealth();
    }
}

document.addEventListener('change', event => {
    if (event.target.id === 'model-health-enabled') {
        saveModelHealthSettings({enabled: event.target.checked});
    }
    const providerId = event.target.dataset?.promptSuffixProvider;
    if (providerId) {
        if (event.target.checked) promptSuffixProviderIds.add(providerId);
        else promptSuffixProviderIds.delete(providerId);
        promptSuffixDirty = true;
    }
});

document.addEventListener('input', event => {
    if (event.target.id === 'model-prompt-suffix') promptSuffixDirty = true;
});

document.addEventListener('submit', event => {
    if (event.target.id !== 'model-prompt-suffix-form') return;
    event.preventDefault();
    return saveModelHealthSettings({
        text: document.getElementById('model-prompt-suffix').value,
        provider_ids: models.map(model => model.provider_id).filter(id => promptSuffixProviderIds.has(id)),
    }, {promptSuffix: true});
});

document.addEventListener('click', event => {
    const usageButton = event.target.closest('[data-model-uses]');
    if (usageButton) {
        const providerId = usageButton.dataset.modelUses;
        if (expandedUses.has(providerId)) expandedUses.delete(providerId);
        else expandedUses.add(providerId);
        updateUsageOverflow();
    }
    const button = event.target.closest('[data-test-model]');
    if (button && !button.disabled) testModelConnection(button.dataset.testModel);
    const toggle = event.target.closest('[data-toggle-model]');
    if (toggle && !toggle.disabled) {
        const providerId = toggle.dataset.toggleModel;
        const model = models.find(item => item.provider_id === providerId);
        if (model) saveModelHealthSettings({provider_id: providerId, enabled: model.enabled === false});
    }
    if (event.target.closest('#model-health-refresh')) loadModelHealth();
});
