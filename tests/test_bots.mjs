import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

const fields = new Map();
const docListeners = new Map();

class Field {
    innerHTML = '';
    textContent = '';
    dataset = {};
    listeners = new Map();
    classes = new Set();
    classList = {
        add: name => this.classes.add(name),
        remove: name => this.classes.delete(name),
    };
    addEventListener(type, fn) {
        if (!this.listeners.has(type)) this.listeners.set(type, []);
        this.listeners.get(type).push(fn);
    }
    dispatchEvent(event) {
        for (const fn of this.listeners.get(event.type) || []) fn(event);
    }
    contains() { return true; }
    querySelectorAll() { return []; }
}

globalThis.document = {
    body: { classList: { add() {}, remove() {} } },
    documentElement: { classList: { add() {}, remove() {} } },
    addEventListener(type, fn) {
        if (!docListeners.has(type)) docListeners.set(type, []);
        docListeners.get(type).push(fn);
    },
    querySelectorAll() { return []; },
    getElementById(id) {
        if (!fields.has(id)) fields.set(id, new Field());
        return fields.get(id);
    },
};
globalThis.window = {
    openModal(id) { document.getElementById(id).classList.add('show'); },
    closeModal(id) { document.getElementById(id).classList.remove('show'); },
    showToast() {},
};
globalThis.confirm = () => {
    throw new Error("Ignored call to 'confirm()'. The document is sandboxed.");
};
globalThis.alert = () => {
    throw new Error("Ignored call to 'alert()'. The document is sandboxed.");
};

await import('../pages/giftia_dashboard/js/utils.js');
for (const listener of docListeners.get('DOMContentLoaded') || []) listener();

const { loadBotsData, deleteBotConfig } = await import('../pages/giftia_dashboard/js/modules/bots.js');

const get = id => document.getElementById(id);

beforeEach(() => {
    fields.clear();
    // Re-register confirm OK button listener on the fresh mock element.
    for (const listener of docListeners.get('DOMContentLoaded') || []) listener();
    window.showToast = () => {};
    window.apiGet = async () => ({
        status: 'success',
        data: {
            bots: [{ name: "Alice's <Bot>", nickname: 'Alice', enabled: true }],
            metadata: {},
        },
    });
    window.apiPost = async () => ({ status: 'success', message: '已删除' });
});

test('deleteBotConfig uses custom confirm modal instead of sandbox-blocked window.confirm', async () => {
    const notifications = [];
    const requests = [];
    let getCount = 0;
    window.showToast = (...args) => notifications.push(args);
    window.apiGet = async () => {
        getCount++;
        return {
            status: 'success',
            data: {
                bots: getCount === 1 ? [{ name: "Alice's <Bot>", nickname: 'Alice', enabled: true }] : [],
                metadata: {},
            },
        };
    };
    window.apiPost = async (endpoint, body) => {
        requests.push([endpoint, body]);
        return { status: 'success', message: "机器人 'Alice's <Bot>' 已成功删除" };
    };

    await loadBotsData();
    const grid = get('bots-grid');
    assert.match(grid.innerHTML, /data-bot-action="delete"/);
    assert.doesNotMatch(grid.innerHTML, /onclick="deleteBotConfig/);

    grid.dispatchEvent({
        type: 'click',
        target: {
            closest(selector) {
                if (selector !== '[data-bot-action]') return null;
                return { dataset: { botAction: 'delete', botName: "Alice's <Bot>" } };
            },
        },
    });

    assert.equal(requests.length, 0);
    assert.ok(get('confirm-modal').classes.has('show'));
    assert.equal(get('confirm-title').textContent, '确认删除机器人');
    assert.match(get('confirm-message').textContent, /Alice's <Bot>/);

    get('confirm-btn-ok').dispatchEvent({ type: 'click' });
    await new Promise(resolve => setTimeout(resolve, 0));

    assert.deepEqual(requests, [['/bots/delete', { name: "Alice's <Bot>" }]]);
    assert.equal(get('confirm-modal').classes.has('show'), false);
    assert.equal(getCount, 2);
    assert.match(get('bots-grid').innerHTML, /暂无创设的机器人配置/);
    assert.deepEqual(notifications, [["机器人 'Alice's <Bot>' 已成功删除", 'success']]);
});

test('canceling custom confirm modal does not delete the bot', async () => {
    const requests = [];
    window.apiPost = async (endpoint, body) => {
        requests.push([endpoint, body]);
        return { status: 'success' };
    };

    await loadBotsData();
    deleteBotConfig("Alice's <Bot>");
    assert.ok(get('confirm-modal').classes.has('show'));

    window.closeConfirmModal();
    assert.equal(get('confirm-modal').classes.has('show'), false);
    assert.equal(requests.length, 0);
});

test('duplicate delete calls for the same bot are ignored while request is in flight', async () => {
    let finish;
    let calls = 0;
    window.apiPost = () => {
        calls++;
        return new Promise(resolve => { finish = resolve; });
    };

    await loadBotsData();
    deleteBotConfig('Giftia');
    get('confirm-btn-ok').dispatchEvent({ type: 'click' });

    deleteBotConfig('Giftia');
    assert.equal(get('confirm-modal').classes.has('show'), false);
    assert.equal(calls, 1);

    finish({ status: 'success', message: '已删除' });
    await new Promise(resolve => setTimeout(resolve, 0));

    deleteBotConfig('Giftia');
    assert.ok(get('confirm-modal').classes.has('show'));
    window.closeConfirmModal();
});

test('failed delete clears in-flight guard and toggle switch uses delegated bot name', async () => {
    const notifications = [];
    const requests = [];
    let deleteCalls = 0;
    window.showToast = (...args) => notifications.push(args);
    window.apiPost = async (endpoint, body) => {
        requests.push([endpoint, body]);
        if (endpoint === '/bots/delete' && ++deleteCalls === 1) {
            throw new Error('network error');
        }
        return { status: 'success', message: '操作成功' };
    };

    await loadBotsData();
    const grid = get('bots-grid');

    grid.dispatchEvent({
        type: 'change',
        target: {
            closest(selector) {
                if (selector !== '[data-bot-action="toggle"]') return null;
                return { checked: false, dataset: { botName: "Alice's <Bot>" } };
            },
        },
    });
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.deepEqual(requests[0], ['/bots/toggle', { name: "Alice's <Bot>", enabled: false }]);

    deleteBotConfig("Alice's <Bot>");
    get('confirm-btn-ok').dispatchEvent({ type: 'click' });
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.deepEqual(notifications.at(-1), ['删除请求异常', 'error']);

    // Retry must succeed after failure cleanup.
    deleteBotConfig("Alice's <Bot>");
    assert.ok(get('confirm-modal').classes.has('show'));
    get('confirm-btn-ok').dispatchEvent({ type: 'click' });
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.deepEqual(requests.at(-1), ['/bots/delete', { name: "Alice's <Bot>" }]);
    assert.deepEqual(notifications.at(-1), ['操作成功', 'success']);
});
