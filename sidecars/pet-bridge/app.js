/**
 * AGY 桌宠 sidecar 前端控制器。
 */

(function () {
  const $ = (id) => document.getElementById(id);
  const fetcher = window.sidecar?.fetch ? window.sidecar.fetch : window.fetch.bind(window);

  const FOCUS_LABELS = {
    hook: 'Hook 实时锁定',
    host: '侧边栏会话绑定',
    view: '当前浏览标签',
    input: '最近发送输入',
    running: '正在生成',
    recent: '最近活动会话',
    project_empty: '新项目空白会话',
  };

  function syncThemeSchemeFromBg() {
    const bg = getComputedStyle(document.documentElement).getPropertyValue('--background').trim();
    if (!bg) return;
    const probe = document.createElement('span');
    probe.style.color = bg;
    probe.style.display = 'none';
    document.body.appendChild(probe);
    const rgb = getComputedStyle(probe).color;
    probe.remove();
    const m = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(rgb);
    if (!m) return;
    const lum = (0.2126 * Number(m[1]) + 0.7152 * Number(m[2]) + 0.0722 * Number(m[3])) / 255;
    document.documentElement.dataset.theme = lum < 0.48 ? 'dark' : 'light';
  }

  window.addEventListener('message', (ev) => {
    if (ev.data?.type === 'theme-change') {
      requestAnimationFrame(syncThemeSchemeFromBg);
    }
  });

  function fmtTokens(n) {
    const v = Number(n || 0);
    if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`;
    if (v >= 1_000) return `${(v / 1_000).toFixed(1)}k`;
    return `${Math.round(v)}`;
  }

  function classifyWindow(bucket) {
    const text = `${bucket?.window || ''} ${bucket?.name || ''} ${bucket?.description || ''}`;
    if (/(?<!\d)5\s*h(?:ours?|rs?)?(?![a-z])|(?<!\d)5[-_ ]?hours?|five[-_ ]?hours?|PT5H|(?<!\d)18000s?(?!\d)|(?<!\d)5\s*小时/i.test(text)) {
      return '5h';
    }
    if (/week|(?<!\d)7\s*d(?:ays?)?(?![a-z])|(?<!\d)7[-_ ]?days?|seven[-_ ]?days?|P7D|P1W|(?<!\d)604800s?(?!\d)|周|(?<!\d)7\s*天|七天/i.test(text)) {
      return 'week';
    }
    return '';
  }

  function pickQuota(buckets, modelLabel, kind, fallbackPct, fallbackReset) {
    const thirdParty = /(claude|gpt|oss|opus|sonnet)/i.test(modelLabel || '');
    const valid = (buckets || []).filter((b) => !b.disabled && typeof b.remainingPct === 'number' && classifyWindow(b) === kind);
    const groupMatched = valid.filter((b) => {
      const g = (b.group || '').toLowerCase();
      if (!g) return false;
      return thirdParty ? /(claude|gpt)/.test(g) : g.includes('gemini');
    });
    const pool = groupMatched.length ? groupMatched : valid;
    if (pool.length) {
      return pool.reduce((min, cur) => (cur.remainingPct < min.remainingPct ? cur : min)).remainingPct;
    }
    if (kind === '5h' && typeof fallbackPct === 'number') {
      const target = fallbackReset ? Date.parse(fallbackReset) : 0;
      const hours = target ? (target - Date.now()) / 3_600_000 : 0;
      if (hours <= 5.2) return fallbackPct;
    }
    if (kind === 'week' && typeof fallbackPct === 'number') {
      const target = fallbackReset ? Date.parse(fallbackReset) : 0;
      const hours = target ? (target - Date.now()) / 3_600_000 : 0;
      if (hours > 5.2) return fallbackPct;
    }
    return null;
  }

  let currentState = null;

  function renderForms(forms, agyConfig) {
    const grid = $('form-grid');
    grid.innerHTML = '';
    for (const f of forms || []) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `form-btn${agyConfig.form === f.id ? ' active' : ''}`;
      btn.innerHTML = `<span class="form-swatch" style="background:${f.accent}"></span><span>${f.name}</span>`;
      btn.addEventListener('click', () => saveConfig({ form: f.id, follow_model: false }));
      grid.appendChild(btn);
    }
  }

  function render(state) {
    currentState = state;
    const dot = $('pet-dot');
    dot.classList.toggle('online', Boolean(state.petRunning));
    $('pet-status-text').textContent = state.petRunning ? '桌宠运行中（配置实时热同步）' : '桌宠未运行';
    $('btn-launch').textContent = state.petRunning ? '唤醒窗口' : '启动桌宠';

    const agy = state.agyConfig || {};
    renderForms(state.forms || [], agy);

    $('chk-follow-model').checked = Boolean(agy.follow_model);
    $('chk-hud-locked').checked = Boolean(agy.hud_locked);
    $('chk-autonomy').checked = Boolean(agy.autonomy);
    $('chk-reactions').checked = Boolean(agy.reactions);
    $('chk-auto-launch').checked = Boolean(state.bridgeSettings?.autoLaunchPet);
    $('sel-appearance').value = agy.hud_appearance || 'dialog';
    $('sel-hud-mode').value = agy.hud_mode || 'attached';
    const vis = ['show', 'auto', 'hide'].includes(agy.hud_visibility)
      ? agy.hud_visibility
      : agy.hud_enabled === false
        ? 'hide'
        : 'show';
    $('sel-hud-visibility').value = vis;
    $('wrap-hud-delay').hidden = vis !== 'auto';
    const delayInput = $('inp-hud-delay');
    if (document.activeElement !== delayInput) {
      const secs = Math.min(60, Math.max(1, Math.round(Number(agy.hud_auto_hide_seconds) || 5)));
      delayInput.value = String(secs);
    }

    const live = state.live || {};
    const convs = Array.isArray(live.conversations) ? live.conversations : [];
    const pinned = agy.pinned_conversation || '';
    const sel = $('sel-pinned');
    sel.innerHTML = '';
    const autoOpt = document.createElement('option');
    autoOpt.value = '';
    autoOpt.textContent = '自动跟随当前活跃会话';
    sel.appendChild(autoOpt);
    for (const c of convs) {
      const opt = document.createElement('option');
      opt.value = c.conversationId;
      opt.textContent = c.title || c.conversationId.slice(0, 8);
      sel.appendChild(opt);
    }
    sel.value = convs.some((c) => c.conversationId === pinned) ? pinned : '';

    const shown =
      (pinned ? convs.find((c) => c.conversationId === pinned) : null) || live.active || null;
    if (!shown) {
      $('focus-badge').textContent = '无会话';
      $('conv-title').textContent = '暂无活跃会话';
      $('conv-model').textContent = '当前模型：—';
      $('ctx-text').textContent = '—';
      $('ctx-bar').style.width = '0%';
      $('kpi-speed').textContent = '—';
      $('kpi-5h').textContent = '—';
      $('kpi-week').textContent = '—';
      $('kpi-cache').textContent = '—';
    } else {
      const running = shown.status === 'CASCADE_RUN_STATUS_RUNNING';
      const activeModel = running && shown.lastStepModelLabel ? shown.lastStepModelLabel : shown.selectedModelLabel;
      $('focus-badge').textContent = pinned ? '已锁定会话' : FOCUS_LABELS[shown.focusSource || live.focusSource] || '实时追踪';
      $('conv-title').textContent = `${running ? '生成中 · ' : ''}${shown.title || '新对话'}`;
      $('conv-model').textContent = `当前模型：${activeModel || '未知模型'}${shown.lastStepModelLabel ? ` · 最近一步：${shown.lastStepModelLabel}` : ''}`;

      const used = Number(shown.contextUsedTokens || 0);
      const limit = Number(shown.contextLimitTokens || 524288);
      const pct = limit > 0 ? (used / limit) * 100 : 0;
      $('ctx-text').textContent = `${fmtTokens(used)} / ${fmtTokens(limit)} · ${pct.toFixed(1)}%`;
      const bar = $('ctx-bar');
      bar.style.width = `${Math.min(100, Math.max(0, pct)).toFixed(1)}%`;
      bar.classList.toggle('danger', pct >= 80);
      bar.classList.toggle('warn', pct >= 55 && pct < 80);

      const rate = shown.latestDecodeTokPerSec || shown.avgDecodeTokPerSec;
      $('kpi-speed').textContent = rate ? `${rate.toFixed(1)} tok/s` : '—';
      const q5 = pickQuota(live.quotaBuckets, activeModel, '5h', shown.quotaRemainingPct, shown.quotaResetTime);
      const qw = pickQuota(live.quotaBuckets, activeModel, 'week', shown.quotaRemainingPct, shown.quotaResetTime);
      $('kpi-5h').textContent = q5 !== null ? `${q5.toFixed(0)}%` : '—';
      $('kpi-week').textContent = qw !== null ? `${qw.toFixed(0)}%` : '—';
      $('kpi-cache').textContent = `${Number(shown.cacheHitRatePct || 0).toFixed(1)}%`;
    }

    $('last-sync').textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  }

  async function loadState() {
    try {
      const res = await fetcher('/api/state');
      if (!res.ok) return;
      const state = await res.json();
      render(state);
    } catch {
      $('status-msg').textContent = '读取侧边栏状态失败';
    }
  }

  async function saveConfig(patch) {
    try {
      $('status-msg').textContent = '正在同步配置…';
      const res = await fetcher('/api/pet/config', {
        method: 'POST',
        body: JSON.stringify(patch),
      });
      if (!res.ok) {
        $('status-msg').textContent = '保存配置失败';
        return;
      }
      const result = await res.json();
      if (currentState) {
        currentState.agyConfig = result.agyConfig;
        currentState.bridgeSettings = result.bridgeSettings;
        render(currentState);
      }
      $('status-msg').textContent = '已写入配置，桌宠约 1.5s 内自动应用';
    } catch {
      $('status-msg').textContent = '写入配置失败';
    }
  }

  async function reportInitialHostFocus() {
    const cid = window.sidecar?.conversationId;
    if (!cid) return;
    let uris = [];
    try {
      if (window.sidecar?.getWorkspaceUris) {
        uris = await window.sidecar.getWorkspaceUris();
      }
    } catch {
      uris = [];
    }
    try {
      await fetcher('/api/host-focus', {
        method: 'POST',
        body: JSON.stringify({ conversationId: cid, workspaceUris: uris, initial: true }),
      });
    } catch {
      // Non-fatal.
    }
  }

  $('btn-launch').addEventListener('click', async () => {
    $('status-msg').textContent = '正在启动或唤醒桌宠…';
    try {
      const res = await fetcher('/api/pet/launch', { method: 'POST', body: '{}' });
      const body = await res.json();
      $('status-msg').textContent = body.ok ? '已发送启动/唤醒请求' : `启动失败：${body.error || '未知错误'}`;
      setTimeout(loadState, 800);
    } catch {
      $('status-msg').textContent = '启动请求异常';
    }
  });

  $('chk-follow-model').addEventListener('change', (e) => saveConfig({ follow_model: e.target.checked }));
  $('sel-hud-visibility').addEventListener('change', (e) => {
    const vis = e.target.value;
    $('wrap-hud-delay').hidden = vis !== 'auto';
    saveConfig({ hud_visibility: vis, hud_enabled: vis !== 'hide' });
  });
  $('inp-hud-delay').addEventListener('change', (e) => {
    const secs = Math.min(60, Math.max(1, Math.round(Number(e.target.value) || 5)));
    e.target.value = String(secs);
    saveConfig({ hud_auto_hide_seconds: secs });
  });
  $('chk-hud-locked').addEventListener('change', (e) => saveConfig({ hud_locked: e.target.checked }));
  $('chk-autonomy').addEventListener('change', (e) => saveConfig({ autonomy: e.target.checked }));
  $('chk-reactions').addEventListener('change', (e) => saveConfig({ reactions: e.target.checked }));
  $('chk-auto-launch').addEventListener('change', (e) => saveConfig({ autoLaunchPet: e.target.checked }));
  $('sel-appearance').addEventListener('change', (e) => saveConfig({ hud_appearance: e.target.value }));
  $('sel-hud-mode').addEventListener('change', (e) => saveConfig({ hud_mode: e.target.value }));
  $('sel-pinned').addEventListener('change', (e) => saveConfig({ pinned_conversation: e.target.value }));

  syncThemeSchemeFromBg();
  reportInitialHostFocus().finally(loadState);
  const timer = setInterval(loadState, 2500);
  window.addEventListener('beforeunload', () => clearInterval(timer));
})();
