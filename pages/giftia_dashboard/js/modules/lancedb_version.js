const endpoint = '/dependencies/lancedb';
let controls;
let snapshot;
let busy = false;

function renderControls() {
    const plan = snapshot?.plan;
    const version = controls.choice.value;
    const canSwitch = snapshot && !snapshot.switch_unavailable;
    controls.check.disabled = busy || !snapshot;
    controls.choice.disabled = busy;
    controls.plan.hidden = !canSwitch || controls.picker.hidden;
    controls.plan.disabled = busy || !version || version === snapshot?.installed_version
        || (version === plan?.version && !plan.error);
    controls.cancel.hidden = !canSwitch || !plan?.version;
    controls.cancel.disabled = busy;
}

function renderStatus(data) {
    snapshot = data;
    controls.running.textContent = data.running_version ? `运行中 ${data.running_version}` : '运行版本未知';
    controls.installed.textContent = data.installed_version || '未检测到';
    controls.range.textContent = data.supported_range || '—';

    const { plan } = data;
    if (data.switch_unavailable) {
        controls.message.textContent = data.switch_unavailable;
    } else if (plan?.version) {
        if (plan.error) {
            controls.message.textContent = `切换到 ${plan.version} 失败。${plan.error}`;
        } else if (data.restart_deferred) {
            controls.message.textContent = `当前进程已加载 LanceDB，切换到 ${plan.version} 仍在等待。请完整重启 AstrBot，若仍在等待请通过 AstrBot 控制台切换。`;
        } else {
            controls.message.textContent = `已计划切换到 ${plan.version}，请重启 AstrBot。`;
        }
    } else if (data.installed_version && data.running_version !== data.installed_version) {
        controls.message.textContent = '重启 AstrBot 后载入已安装的版本。';
    } else {
        controls.message.textContent = '';
    }
}

async function checkVersions() {
    if (busy) return;
    busy = true;
    renderControls();
    controls.check.textContent = '正在检查…';
    try {
        const result = await window.apiGet(`${endpoint}/versions`);
        if (result.status !== 'success') throw new Error(result.message);

        const versions = result.data;
        const { installed_version: installed, plan } = snapshot;
        const choice = controls.choice.value || plan?.version || versions[0];
        controls.choice.replaceChildren(new Option('选择目标版本', ''));
        versions.forEach((version, index) => {
            const suffix = [index === 0 ? '最新可用' : '', version === installed ? '已安装' : '']
                .filter(Boolean).join('，');
            const option = new Option(suffix ? `${version}（${suffix}）` : version, version);
            option.disabled = version === installed;
            controls.choice.append(option);
        });
        controls.choice.value = choice !== installed && versions.includes(choice) ? choice : '';
        controls.picker.hidden = versions.length === 0;
        renderStatus(snapshot);
        if (!versions.length) window.showToast('当前环境没有可用的 LanceDB 版本', 'error');
    } catch (error) {
        window.showToast(error.message || '检查 LanceDB 版本失败', 'error');
    } finally {
        busy = false;
        controls.check.textContent = '检查可用版本';
        renderControls();
    }
}

async function savePlan(version) {
    if (busy) return;
    busy = true;
    renderControls();
    try {
        const result = await window.apiPost(endpoint, { version });
        if (result.status !== 'success') throw new Error(result.message);
        renderStatus(result.data);
    } catch (error) {
        window.showToast(error.message || '保存 LanceDB 切换计划失败', 'error');
    } finally {
        busy = false;
        renderControls();
    }
}

export async function loadLanceDBVersion() {
    const panel = document.getElementById('memory-lancedb');
    if (!panel || busy) return;
    if (!controls) {
        controls = Object.fromEntries(
            ['running', 'installed', 'range', 'check', 'picker', 'choice', 'plan', 'cancel', 'message']
                .map(name => [name, panel.querySelector(`[data-lancedb-${name}]`)])
        );
        panel.addEventListener('toggle', () => {
            if (panel.open) loadLanceDBVersion();
        });
        controls.check.addEventListener('click', checkVersions);
        controls.choice.addEventListener('change', renderControls);
        controls.plan.addEventListener('click', () => {
            const target = controls.choice.value;
            window.showConfirm(
                '切换 LanceDB 版本',
                `LanceDB 将在 AstrBot 下次启动时切换到 ${target}，同一 Python 环境中的插件会共用此版本。降级前请先备份 Giftia 记忆数据。`,
                () => savePlan(target)
            );
        });
        controls.cancel.addEventListener('click', () => savePlan(null));
    }

    busy = true;
    renderControls();
    try {
        const result = await window.apiGet(endpoint);
        if (result.status !== 'success') throw new Error(result.message);
        renderStatus(result.data);
    } catch (error) {
        controls.running.textContent = '版本读取失败';
        controls.message.textContent = error.message || '打开面板重试。';
    } finally {
        busy = false;
        renderControls();
    }
}
