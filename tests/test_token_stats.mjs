import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

const fields = new Map();
const charts = [];
class Field {
    value = '';
    innerHTML = '';
    textContent = '';
    hidden = false;
    tagName = 'SELECT';
    attributes = new Map();
    listeners = new Map();
    classList = { add() {}, remove() {} };
    getContext() { return {}; }
    setAttribute(name, value) { this.attributes.set(name, value); }
    getAttribute(name) { return this.attributes.get(name); }
    addEventListener(name, listener) { this.listeners.set(name, listener); }
    querySelectorAll() { return []; }
    append() {}
}
globalThis.document = {
    documentElement: { getAttribute: () => 'light' },
    addEventListener() {},
    getElementById(id) {
        if (!fields.has(id)) fields.set(id, new Field());
        return fields.get(id);
    }
};
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
globalThis.Option = class {};
globalThis.Chart = class {
    constructor(context, config) { charts.push(config); }
    destroy() {}
};
globalThis.window = {
    apiGet: async () => ({ status: 'success', data: {} }),
    GiftiaApp: { loadTokenStatsSummary: async () => {} }
};
await import('../pages/giftia_dashboard/js/utils.js');
const { initializeTokenStatsTab, renderTokenStatsSummary, loadTokenStatsSummary } = await import('../pages/giftia_dashboard/js/modules/render/token.js');
await initializeTokenStatsTab();
const get = id => document.getElementById(id);
const html = () => get('token-active-progress-bars').innerHTML;
async function selectTab(tab) {
    const button = new Field();
    button.setAttribute('data-tab', tab);
    await get('token-chart-tabs-nav').listeners.get('click')({ target: { closest: () => button } });
}

beforeEach(() => {
    for (const field of fields.values()) field.value = '';
    charts.length = 0;
});

test('model rows show model, operation and execution mode without fabricated zero usage', async () => {
    await selectTab('model');
    renderTokenStatsSummary({
        summary: { total_tokens: 100, total_calls: 4, usage_missing_calls: 3 },
        by_model: [
            { model_name: 'chat', total_tokens: 100, call_count: 1 },
            { model_name: 'BAAI/<embed>', model_type: 'embedding', execution_mode: 'local', call_count: 2, usage_missing_calls: 2 },
            { model_name: 'vendor/rerank', provider_id: 'provider/<remote>', model_type: 'rerank', execution_mode: 'remote', call_count: 1, usage_missing_calls: 1 }
        ]
    });
    assert.match(html(), /BAAI\/&lt;embed&gt;/);
    assert.match(html(), /嵌入 · 本地/);
    assert.match(html(), /重排序 · 远程 · provider\/&lt;remote&gt;/);
    assert.match(html(), /调用 2 次/);
    assert.equal((html().match(/<span>Token 未提供<\/span>/g) || []).length, 2);
    assert.equal((html().match(/progress-bar-container/g) || []).length, 1);
    assert.doesNotMatch(html(), /<embed>|<remote>|>0 Tokens|\(0\.0%\)/);
});

test('type rows include embedding and rerank totals and local versus remote counts', async () => {
    await selectTab('type');
    renderTokenStatsSummary({ summary: {
        total_tokens: 100, total_calls: 7, usage_missing_calls: 6,
        reply_tokens: 100, reply_calls: 1,
        embedding_calls: 4, embedding_usage_missing_calls: 4, embedding_local_calls: 3, embedding_remote_calls: 1,
        rerank_calls: 2, rerank_usage_missing_calls: 2, rerank_remote_calls: 2
    } });
    assert.match(html(), /嵌入/);
    assert.match(html(), /重排序/);
    assert.match(html(), /本地 3 次 · 远程 1 次/);
    assert.match(html(), /远程 2 次/);
    assert.match(html(), /调用 4 次/);
    assert.match(html(), /调用 2 次/);
    assert.equal((html().match(/<span>Token 未提供<\/span>/g) || []).length, 2);
    assert.equal((html().match(/progress-bar-container/g) || []).length, 1);
    assert.doesNotMatch(html(), />0 Tokens|\(0\.0%\)/);
});

test('sessions with mixed usage show known tokens and disclose unreported calls', async () => {
    await selectTab('group');
    renderTokenStatsSummary({
        summary: { total_tokens: 100, total_calls: 5, usage_missing_calls: 4 },
        by_group: [
            { group_or_user_id: 'mixed', total_tokens: 100, call_count: 3, usage_missing_calls: 2 },
            { group_or_user_id: 'unreported', total_tokens: 0, call_count: 2, usage_missing_calls: 2 }
        ]
    });
    assert.match(html(), /100 Tokens \(100\.0%\)/);
    assert.match(html(), /另 2 次 Token 未提供/);
    assert.match(html(), /<span>Token 未提供<\/span>/);
    assert.equal((html().match(/progress-bar-container/g) || []).length, 1);
    assert.equal(get('stat-token-missing').textContent, '4 次调用 Token 未提供');
});

test('unreported-only usage keeps token totals unavailable and contributes no chart series', async () => {
    await selectTab('type');
    renderTokenStatsSummary({ summary: {
        total_tokens: 0, total_calls: 3, usage_missing_calls: 3,
        embedding_calls: 3, embedding_usage_missing_calls: 3, embedding_local_calls: 3
    } });
    assert.equal(get('stat-total-tokens').textContent, '未提供');
    assert.equal(get('token-chart-legend-stats').getAttribute('title'), 'Token 未提供');
    assert.match(html(), /Token 未提供/);
    assert.doesNotMatch(html(), />0 Tokens|progress-bar-container/);
    assert.deepEqual(charts.at(-1).data.datasets, []);
});

test('legacy zero-token calls stay visible and clear stale missing-usage notices', async () => {
    await selectTab('type');
    renderTokenStatsSummary({ summary: { total_calls: 1, decision_calls: 1 } });
    assert.match(html(), /0 Tokens/);
    assert.match(html(), /调用 1 次/);
    assert.equal(get('stat-total-tokens').textContent, '0');
    assert.equal(get('stat-token-missing').hidden, true);
    assert.doesNotMatch(html(), /Token 未提供/);
});

test('legacy archived tokens without call counts remain visible alongside unreported new calls', async () => {
    const stats = {
        summary: { total_tokens: 100, total_calls: 2, usage_missing_calls: 2, reply_tokens: 100, reply_calls: 2, reply_usage_missing_calls: 2 },
        by_group: [{ group_or_user_id: 'legacy', total_tokens: 100, call_count: 2, usage_missing_calls: 2 }],
        by_model: [{ model_name: 'legacy', total_tokens: 100, call_count: 2, usage_missing_calls: 2 }]
    };
    for (const dimension of ['group', 'model', 'type']) {
        await selectTab(dimension);
        renderTokenStatsSummary(stats);
        assert.equal(get('stat-total-tokens').textContent, '100');
        assert.match(html(), /100 Tokens/);
        assert.match(html(), /另 2 次 Token 未提供/);
        assert.match(html(), /progress-bar-container/);
    }
});

test('statistics requests retain bot, conversation and time filters', async () => {
    get('token-bot-name').value = 'bot';
    get('token-group-id').value = 'session';
    get('token-time-range').value = 'week';
    let requested;
    window.apiGet = async (path, params) => {
        requested = { path, params };
        return { status: 'success', stats: { summary: { total_calls: 1, usage_missing_calls: 1 } } };
    };
    await loadTokenStatsSummary();
    assert.deepEqual(requested, { path: '/token/stats', params: { bot_name: 'bot', group_or_user_id: 'session', time_range: 'week' } });
    assert.equal(get('stat-total-tokens').textContent, '未提供');
});
