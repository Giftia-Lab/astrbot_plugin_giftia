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
let savingPromptText = false;
let loadVersion = 0;
let refreshTimer = null;
const savingPromptProviders = new Map();
const testing = new Set();
const expandedUses = new Set();
let usageObserver;
const escape = value => window.escapeHtml(String(value ?? ''));
const formatTime = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {hour12: false}) : '—';

function applyModelSnapshot(result, errorMessage) {
    if (result?.status !== 'success' || !Array.isArray(result.data?.models) || typeof result.data.enabled !== 'boolean') {
        throw new Error(result?.message || errorMessage);
    }
    models = result.data.models;
    managementEnabled = result.data.enabled;
    return result.data;
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
    if (promptInput) promptInput.disabled = savingPromptText;
    if (promptSave) promptSave.disabled = savingPromptText;
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
        const promptSwitchId = `model-prompt-provider-${index}`;
        const promptEnabled = savingPromptProviders.get(model.provider_id) ?? promptSuffixProviderIds.has(model.provider_id);
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
                <div class="model-prompt-suffix-option">
                    <label for="${promptSwitchId}">注入提示词</label>
                    <div class="switch-container">
                        <input type="checkbox" class="switch-checkbox" role="switch" id="${promptSwitchId}" data-prompt-suffix-provider="${providerId}" aria-label="为 ${providerId} 注入防误判提示词" ${promptEnabled ? 'checked' : ''} ${savingPromptProviders.has(model.provider_id) ? 'disabled' : ''}>
                        <label for="${promptSwitchId}" class="switch-label"></label>
                    </div>
                </div>
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
    try {
        const result = await window.apiGet('/models/health');
        if (version !== loadVersion) return;
        const {prompt_suffix: settings} = applyModelSnapshot(result, '模型状态加载失败');
        promptSuffixProviderIds = new Set(settings?.provider_ids ?? []);
        const input = document.getElementById('model-prompt-suffix');
        if (input && !promptSuffixDirty && !savingPromptText) {
            const text = settings?.text ?? '';
            if (input.value !== text) input.value = text;
        }
        if (error) error.textContent = '';
        renderModels();
    } catch (err) {
        if (version === loadVersion && error) error.textContent = err.message || '模型状态加载失败';
    } finally {
        if (version === loadVersion) {
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
        window.showToast('连通性测试请求失败', 'error');
    } finally {
        testing.delete(providerId);
        if (state.activeTab === 'model-health') await loadModelHealth();
    }
}

export async function saveModelHealthSettings(settings) {
    if (savingSettings) return;
    savingSettings = true;
    ++loadVersion;
    renderModels();
    try {
        const result = await window.apiPost('/models/settings', settings);
        applyModelSnapshot(result, '保存模型状态设置失败');
        window.showToast('模型状态设置已保存', 'success');
    } catch (err) {
        window.showToast(err.message || '保存模型状态设置失败', 'error');
    } finally {
        savingSettings = false;
        renderModels();
        if (state.activeTab === 'model-health') await loadModelHealth();
    }
}

async function savePromptSuffix(settings) {
    const providerId = settings.provider_id;
    if (providerId ? savingPromptProviders.has(providerId) : savingPromptText) return;
    if (providerId) savingPromptProviders.set(providerId, settings.enabled);
    else savingPromptText = true;
    ++loadVersion;
    renderModels();
    const errorMessage = providerId ? '保存模型注入开关失败' : '保存防误判提示词失败';
    try {
        const result = await window.apiPost('/models/prompt-suffix', settings);
        const saved = result?.data?.prompt_suffix;
        if (result?.status !== 'success' || typeof saved?.text !== 'string' || !Array.isArray(saved.provider_ids)) {
            throw new Error(result?.message || errorMessage);
        }
        // Only apply the field this request changed. Other saves may already have completed.
        if (providerId) {
            if (saved.provider_ids.includes(providerId)) promptSuffixProviderIds.add(providerId);
            else promptSuffixProviderIds.delete(providerId);
        } else {
            document.getElementById('model-prompt-suffix').value = saved.text;
            promptSuffixDirty = false;
            window.showToast('提示词已保存', 'success');
        }
    } catch (err) {
        window.showToast(err.message || errorMessage, 'error');
    } finally {
        if (providerId) savingPromptProviders.delete(providerId);
        else savingPromptText = false;
        renderModels();
        if (state.activeTab === 'model-health') await loadModelHealth();
    }
}

document.addEventListener('change', event => {
    if (event.target.id === 'model-health-enabled') {
        return saveModelHealthSettings({enabled: event.target.checked});
    }
    const providerId = event.target.dataset?.promptSuffixProvider;
    if (providerId) {
        return savePromptSuffix({provider_id: providerId, enabled: event.target.checked});
    }
});

document.addEventListener('input', event => {
    if (event.target.id === 'model-prompt-suffix') promptSuffixDirty = true;
});

document.addEventListener('submit', event => {
    if (event.target.id !== 'model-prompt-suffix-form') return;
    event.preventDefault();
    return savePromptSuffix({
        text: document.getElementById('model-prompt-suffix').value,
    });
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
    const promptToggle = event.target.closest('#model-prompt-suffix-toggle');
    if (promptToggle) {
        const form = document.getElementById('model-prompt-suffix-form');
        form.hidden = !form.hidden;
        promptToggle.setAttribute('aria-expanded', String(!form.hidden));
    }
});
