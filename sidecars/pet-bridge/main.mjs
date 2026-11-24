/**
 * AGY 桌宠 Antigravity 插件 Sidecar 后端 (pet-bridge)
 *
 * - 通过 LanguageServerService (GetUserStatus / RetrieveUserQuotaSummary /
 *   GetAllCascadeTrajectories / GetCascadeTrajectory) 与本地焦点信号
 *   (active_session.json / annotations/*.pbtxt / projects/*.json / hostFocus)
 *   实时判定当前正在看的会话与精确上下文占用（剔除单步 thinkingOutputTokens）。
 * - 每 2 秒原子写出 <appData>/sidecar_data/agy-pet/pet-bridge/data/pet_live.json
 *   供桌面宠物读取；同时与桌宠根目录 config.json 双向热同步形态与 HUD 配置。
 */

import { execFileSync, spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, statSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function resolvePetRoot() {
  const envRoot = process.env.AGY_PET_ROOT;
  if (envRoot && existsSync(join(envRoot, 'main.py'))) {
    return envRoot;
  }
  const bundledRoot = dirname(dirname(__dirname));
  if (existsSync(join(bundledRoot, 'main.py'))) {
    return bundledRoot;
  }
  const linkFile = join(bundledRoot, '.pet_root');
  if (existsSync(linkFile)) {
    try {
      const target = readFileSync(linkFile, 'utf-8').trim();
      if (target && existsSync(join(target, 'main.py'))) {
        return target;
      }
    } catch {
      // Ignore unreadable link file.
    }
  }
  return bundledRoot;
}

const PET_ROOT = resolvePetRoot();

const PET_FORMS = [
  { id: 'carbon', name: 'Carbon', modelFamily: 'carbon', accent: '#7dd3fc' },
  { id: 'hydergon', name: 'Hydrogen', modelFamily: 'hydrogen', accent: '#a7f3d0' },
  { id: 'neon', name: 'Neon', modelFamily: 'neon', accent: '#f0abfc' },
  { id: 'argon', name: 'Argon', modelFamily: 'argon', accent: '#c4b5fd' },
  { id: 'claude', name: 'Claude', modelFamily: 'claude', accent: '#fdba74' },
];
const VALID_FORM_IDS = new Set(PET_FORMS.map((f) => f.id));
const VALID_HUD_MODES = new Set(['attached', 'floating']);
const VALID_HUD_VISIBILITIES = new Set(['show', 'auto', 'hide']);
const VALID_HUD_APPEARANCES = new Set(['dialog', 'capsule', 'seamless', 'glass']);

function findAppDataDirs() {
  const execDataDir = process.env.ANTIGRAVITY_EXECUTABLE_DATA_DIR;
  if (execDataDir) {
    for (let d = execDataDir; d !== dirname(d); d = dirname(d)) {
      if (basename(d) === 'sidecar_data') {
        const appData = dirname(d);
        if (existsSync(appData)) return [appData];
      }
    }
  }
  const geminiRoot = join(homedir(), '.gemini');
  const out = [];
  try {
    for (const entry of readdirSync(geminiRoot)) {
      const candidate = join(geminiRoot, entry);
      if (existsSync(join(candidate, 'brain'))) out.push(candidate);
    }
  } catch {
    // Ignore unreadable root.
  }
  return out;
}

function resolveAppDataDir() {
  return findAppDataDirs()[0] || join(homedir(), '.gemini', 'antigravity');
}

function resolvePetDataDir() {
  if (process.env.ANTIGRAVITY_EXECUTABLE_DATA_DIR) {
    return process.env.ANTIGRAVITY_EXECUTABLE_DATA_DIR;
  }
  return join(resolveAppDataDir(), 'sidecar_data', 'agy-pet', 'pet-bridge', 'data');
}

function resolveActiveSessionPath() {
  return join(resolveAppDataDir(), 'sidecar_data', 'agy-pet', 'active_session.json');
}

function resolveProjectsDir() {
  return join(dirname(resolveAppDataDir()), 'config', 'projects');
}

async function loadSidecarSdk() {
  try {
    return await import('sidecar_sdk');
  } catch {
    for (const appData of findAppDataDirs()) {
      const sdkPath = join(appData, 'lib', 'node_modules', 'sidecar_sdk', 'index.mjs');
      if (existsSync(sdkPath)) {
        return await import(pathToFileURL(sdkPath).href);
      }
    }
    const fallback = join(homedir(), '.gemini', 'antigravity', 'lib', 'node_modules', 'sidecar_sdk', 'index.mjs');
    if (existsSync(fallback)) {
      return await import(pathToFileURL(fallback).href);
    }
    throw new Error('Unable to locate sidecar_sdk');
  }
}

if (!process.env.ANTIGRAVITY_SIDECAR_WEB_PORT) {
  process.env.ANTIGRAVITY_SIDECAR_WEB_PORT = process.env.PORT || '18921';
}
if (!process.env.ANTIGRAVITY_LS_ADDRESS) {
  process.env.ANTIGRAVITY_LS_ADDRESS = '127.0.0.1:5387';
}
if (!process.env.ANTIGRAVITY_CSRF_TOKEN) {
  process.env.ANTIGRAVITY_CSRF_TOKEN = 'standalone';
}

const { SidecarApp, Response } = await loadSidecarSdk();
const app = new SidecarApp();

const LS_ADDRESS = process.env.ANTIGRAVITY_LS_ADDRESS;
const CSRF_TOKEN = process.env.ANTIGRAVITY_CSRF_TOKEN;

async function lsRpc(method, body = {}, timeoutMs = 6000) {
  const res = await fetch(
    `http://${LS_ADDRESS}/exa.language_server_pb.LanguageServerService/${method}`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-codeium-csrf-token': CSRF_TOKEN,
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    }
  );
  if (!res.ok) {
    throw new Error(`LS ${method} HTTP ${res.status}`);
  }
  return await res.json();
}

// ---------------------------------------------------------------------------
// Atomic file write & bridge settings
// ---------------------------------------------------------------------------

function atomicWriteJson(filePath, data) {
  mkdirSync(dirname(filePath), { recursive: true });
  const tmp = `${filePath}.${process.pid}.tmp`;
  const text = JSON.stringify(data, null, 2);
  let lastErr = null;
  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      writeFileSync(tmp, text, 'utf-8');
      renameSync(tmp, filePath);
      return;
    } catch (err) {
      lastErr = err;
    }
  }
  if (lastErr) throw lastErr;
}

function readBridgeSettings() {
  const p = join(resolvePetDataDir(), 'bridge_settings.json');
  try {
    const raw = JSON.parse(readFileSync(p, 'utf-8'));
    return { autoLaunchPet: Boolean(raw?.autoLaunchPet) };
  } catch {
    return { autoLaunchPet: false };
  }
}

function writeBridgeSettings(patch) {
  const current = readBridgeSettings();
  const next = {
    autoLaunchPet: patch.autoLaunchPet !== undefined ? Boolean(patch.autoLaunchPet) : current.autoLaunchPet,
  };
  atomicWriteJson(join(resolvePetDataDir(), 'bridge_settings.json'), next);
  return next;
}

// ---------------------------------------------------------------------------
// Model metadata & quota windows
// ---------------------------------------------------------------------------

let userModelStateCache = {
  fetchedAt: 0,
  labelMap: new Map(),
  quotaMap: new Map(),
  limitMap: new Map(),
  selectedModelId: '',
};

function prettifyModelEnum(raw) {
  if (!raw || typeof raw !== 'string') return 'Unknown Model';
  if (!raw.startsWith('MODEL_')) return raw;
  return raw
    .replace(/^MODEL_PLACEHOLDER_/, 'Model ')
    .replace(/^MODEL_/, '')
    .split('_')
    .map((w) => (w.length <= 3 && /\d/.test(w) ? w : w.charAt(0) + w.slice(1).toLowerCase()))
    .join(' ');
}

const DEFAULT_CONTEXT_LIMIT = 524_288;
const UNKNOWN_MODEL_LABELS = new Set(['', '未知模型', '未选择模型', 'unknown model', 'UNKNOWN_MODEL']);

function inferContextWindowLimit(modelId, label, limitMap) {
  const reported = limitMap?.get?.(modelId);
  if (Number.isFinite(reported) && reported > 0) return reported;
  const cleanId = UNKNOWN_MODEL_LABELS.has(String(modelId || '').trim()) ? '' : String(modelId || '');
  const cleanLabel = UNKNOWN_MODEL_LABELS.has(String(label || '').trim()) ? '' : String(label || '');
  const s = `${cleanId} ${cleanLabel}`.toLowerCase();
  if (s.includes('claude')) return 200_000;
  if (s.includes('gpt-oss') || s.includes('120b')) return 131_072;
  return DEFAULT_CONTEXT_LIMIT;
}

const CONTEXT_KEY = /context.*(window|length|limit|size|tokens)|max_?input_?tokens|input_?token_?limit|max_?prompt_?tokens/i;

function findContextLimit(value, depth = 0) {
  if (!value || typeof value !== 'object' || depth > 4) return null;
  for (const [key, field] of Object.entries(value)) {
    const n = Number(field);
    if (CONTEXT_KEY.test(key) && Number.isFinite(n) && n >= 8_000 && n <= 20_000_000) return n;
  }
  for (const field of Object.values(value)) {
    const found = findContextLimit(field, depth + 1);
    if (found) return found;
  }
  return null;
}

async function getUserModelState(maxAgeMs = 15_000) {
  const now = Date.now();
  if (now - userModelStateCache.fetchedAt < maxAgeMs && userModelStateCache.labelMap.size > 0) {
    return userModelStateCache;
  }
  try {
    const data = await lsRpc('GetUserStatus', {});
    const cfgData = data?.userStatus?.cascadeModelConfigData || {};
    const configs = cfgData?.clientModelConfigs;
    const selectedModelId =
      cfgData?.defaultOverrideModelConfig?.modelOrAlias?.model ||
      cfgData?.defaultOverrideModelConfig?.modelOrAlias?.alias ||
      '';

    if (Array.isArray(configs)) {
      const nextLabels = new Map(userModelStateCache.labelMap);
      const nextQuotas = new Map();
      const nextLimits = new Map();
      for (const cfg of configs) {
        const key = cfg?.modelOrAlias?.model || cfg?.modelOrAlias?.alias;
        const label = cfg?.label || cfg?.displayName;
        if (key && label) nextLabels.set(key, label);
        const limit = findContextLimit(cfg);
        if (key && limit) nextLimits.set(key, limit);
        if (key && cfg?.quotaInfo) {
          const frac = Number(cfg.quotaInfo.remainingFraction);
          nextQuotas.set(key, {
            remainingPct: Number.isFinite(frac) ? Math.max(0, Math.min(100, frac * 100)) : null,
            resetTime: cfg.quotaInfo.resetTime || null,
          });
        }
      }
      userModelStateCache = {
        fetchedAt: now,
        labelMap: nextLabels,
        quotaMap: nextQuotas,
        limitMap: nextLimits,
        selectedModelId: selectedModelId || userModelStateCache.selectedModelId,
      };
    }
  } catch {
    // Keep last known state on transient failure.
  }
  return userModelStateCache;
}

let quotaSummaryCache = { fetchedAt: 0, buckets: [], status: 'pending', method: '' };
const QUOTA_SUMMARY_METHODS = ['RetrieveUserQuotaSummary', 'GetUserQuotaSummary'];
const WINDOW_KEY = /window|period|duration|interval|cadence|reset|refresh|type|name|title|description|label/i;

function describeWindow(value) {
  if (value == null) return '';
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  if (typeof value === 'object' && ('seconds' in value || 'nanos' in value)) return `${Number(value.seconds || 0)}s`;
  return '';
}

function normalizeBucket(bucket, groupName) {
  if (!bucket || typeof bucket !== 'object') return null;
  const frac = Number(bucket.remainingFraction);
  const hints = [];
  for (const [key, value] of Object.entries(bucket)) {
    if (key === 'remainingFraction' || key === 'resetTime') continue;
    if (WINDOW_KEY.test(key)) {
      const text = describeWindow(value);
      if (text && text.length <= 120) hints.push(text);
    }
  }
  return {
    id: String(bucket.bucketId || bucket.id || bucket.quotaBucketKey || ''),
    name: String(bucket.displayName || bucket.name || bucket.title || ''),
    description: hints.join(' '),
    group: groupName || '',
    window: describeWindow(bucket.window ?? bucket.windowDuration ?? bucket.period ?? bucket.resetPeriod ?? ''),
    remainingPct: Number.isFinite(frac) ? Math.max(0, Math.min(100, frac * 100)) : null,
    remainingAmount: bucket.remainingAmount ?? null,
    resetTime: bucket.resetTime || null,
    disabled: Boolean(bucket.disabled),
  };
}

function collectBuckets(value, groupName, out, depth = 0) {
  if (!value || typeof value !== 'object' || depth > 8) return out;
  if (Array.isArray(value)) {
    for (const item of value) collectBuckets(item, groupName, out, depth + 1);
    return out;
  }
  if ('remainingFraction' in value) {
    const bucket = normalizeBucket(value, groupName);
    if (bucket) out.push(bucket);
  }
  const name = String(value.displayName || value.name || groupName || '');
  for (const [key, field] of Object.entries(value)) {
    if (field && typeof field === 'object') {
      collectBuckets(field, key === 'quotaSubBuckets' ? groupName : name, out, depth + 1);
    }
  }
  return out;
}

async function getQuotaSummary(maxAgeMs = 60_000) {
  const now = Date.now();
  if (now - quotaSummaryCache.fetchedAt < maxAgeMs) return quotaSummaryCache;
  const errors = [];
  for (const method of QUOTA_SUMMARY_METHODS) {
    try {
      const data = await lsRpc(method, {});
      const buckets = collectBuckets(data, '', []);
      quotaSummaryCache = { fetchedAt: now, buckets, status: buckets.length ? 'ok' : 'empty', method };
      return quotaSummaryCache;
    } catch (err) {
      errors.push(`${method}: ${String(err?.message || 'error').slice(0, 60)}`);
    }
  }
  quotaSummaryCache = {
    fetchedAt: now,
    buckets: quotaSummaryCache.buckets,
    status: errors.join(' | ').slice(0, 200),
    method: '',
  };
  return quotaSummaryCache;
}

// ---------------------------------------------------------------------------
// Annotations, Hooks, Projects & Workspace Normalization
// ---------------------------------------------------------------------------

function normalizeWorkspace(value) {
  if (!value || typeof value !== 'string') return '';
  let text = value.trim();
  try {
    text = decodeURIComponent(text);
  } catch {
    // Keep partially encoded paths.
  }
  if (/^file:\/\/\//i.test(text)) text = text.slice(8);
  else if (/^file:\/\//i.test(text)) text = text.slice(7);
  return text.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
}

function decodePbtxtString(raw) {
  const bytes = [];
  for (let i = 0; i < raw.length; i++) {
    const ch = raw[i];
    if (ch === '\\' && i + 1 < raw.length) {
      const nxt = raw[i + 1];
      if (nxt === 'n') { bytes.push(10); i++; continue; }
      if (nxt === 'r') { bytes.push(13); i++; continue; }
      if (nxt === 't') { bytes.push(9); i++; continue; }
      if (nxt === '"' || nxt === "'" || nxt === '\\') { bytes.push(nxt.charCodeAt(0)); i++; continue; }
      if (nxt >= '0' && nxt <= '7') {
        let j = i + 1;
        while (j < Math.min(raw.length, i + 4) && raw[j] >= '0' && raw[j] <= '7') j++;
        bytes.push(parseInt(raw.slice(i + 1, j), 8) & 0xff);
        i = j - 1;
        continue;
      }
      if ((nxt === 'x' || nxt === 'X') && i + 3 < raw.length) {
        const hex = parseInt(raw.slice(i + 2, i + 4), 16);
        if (Number.isFinite(hex)) {
          bytes.push(hex);
          i += 3;
          continue;
        }
      }
    }
    for (const b of Buffer.from(ch, 'utf-8')) bytes.push(b);
  }
  return Buffer.from(bytes).toString('utf-8').trim();
}

const annotationCache = new Map(); // cid -> { mtimeMs, viewMs, title }

function readAnnotationPbtxt(cid) {
  if (!cid) return { viewMs: 0, title: '' };
  const p = join(resolveAppDataDir(), 'annotations', `${cid}.pbtxt`);
  try {
    const st = statSync(p);
    const hit = annotationCache.get(cid);
    if (hit && hit.mtimeMs === st.mtimeMs) {
      return { viewMs: hit.viewMs, title: hit.title };
    }
    const text = readFileSync(p, 'utf-8');
    let viewMs = 0;
    const blockMatch = /last_user_view_time\s*:\s*\{([^}]*)\}/.exec(text);
    if (blockMatch) {
      const secMatch = /\bseconds\s*:\s*(\d+)/.exec(blockMatch[1]);
      const nanosMatch = /\bnanos\s*:\s*(\d+)/.exec(blockMatch[1]);
      if (secMatch) {
        const nanos = nanosMatch ? Number(nanosMatch[1]) : 0;
        viewMs = Number(secMatch[1]) * 1000 + Math.floor(nanos / 1_000_000);
      }
    }
    const titleMatch = /(?:^|\s)title\s*:\s*"((?:[^"\\]|\\.)*)"/.exec(text);
    const title = titleMatch ? decodePbtxtString(titleMatch[1]) : '';
    annotationCache.set(cid, { mtimeMs: st.mtimeMs, viewMs, title });
    return { viewMs, title };
  } catch {
    return { viewMs: 0, title: '' };
  }
}

function readActiveSessionHook(maxAgeMs = 7_200_000) {
  const p = resolveActiveSessionPath();
  try {
    const st = statSync(p);
    const parsed = JSON.parse(readFileSync(p, 'utf-8'));
    const cid = String(parsed?.conversationId || '').trim();
    if (!cid) return null;
    const updatedMs = Number(parsed?.updatedMs) || st.mtimeMs;
    if (Date.now() - updatedMs > maxAgeMs) return null;
    return {
      conversationId: cid,
      workspacePaths: Array.isArray(parsed?.workspacePaths) ? parsed.workspacePaths : [],
      lastUserInput: String(parsed?.lastUserInput || ''),
      title: String(parsed?.title || ''),
      source: String(parsed?.source || 'hook'),
      updatedMs,
    };
  } catch {
    return null;
  }
}

function latestProjectSwitch(maxAgeMs = 7_200_000) {
  const dir = resolveProjectsDir();
  try {
    const candidates = [];
    for (const name of readdirSync(dir)) {
      if (!name.endsWith('.json')) continue;
      const full = join(dir, name);
      try {
        const st = statSync(full);
        candidates.push({ mtimeMs: st.mtimeMs, path: full, stem: name.slice(0, -5) });
      } catch {
        // Skip unreadable project file.
      }
    }
    candidates.sort((a, b) => b.mtimeMs - a.mtimeMs);
    const now = Date.now();
    for (const item of candidates) {
      if (now - item.mtimeMs > maxAgeMs) break;
      try {
        const data = JSON.parse(readFileSync(item.path, 'utf-8'));
        if (!data || typeof data !== 'object' || data.sidecar) continue;
        const resources = data?.projectResources?.resources || [];
        const workspaces = new Set(
          resources
            .map((r) => normalizeWorkspace(r?.folderUri || ''))
            .filter(Boolean)
        );
        if (workspaces.size === 0) continue;
        return {
          mtimeMs: item.mtimeMs,
          id: String(data?.id || item.stem),
          name: String(data?.name || '').trim(),
          workspaces,
        };
      } catch {
        // Skip malformed project file.
      }
    }
    return null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Parsing trajectories & steps
// ---------------------------------------------------------------------------

function parseIsoMs(iso) {
  if (!iso || typeof iso !== 'string') return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
}

function toInt(v) {
  const n = Number(v || 0);
  return Number.isFinite(n) && n > 0 ? Math.round(n) : 0;
}

function extractCleanUserTitle(text) {
  if (!text || typeof text !== 'string') return '';
  const reqMatch = /<USER_REQUEST>([\s\S]*?)<\/USER_REQUEST>/i.exec(text);
  const body = (reqMatch ? reqMatch[1] : text)
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (!body) return '';
  return body.length > 48 ? `${body.slice(0, 48)}…` : body;
}

function readTranscriptTitleAndStat(convId) {
  for (const appData of findAppDataDirs()) {
    const p = join(appData, 'brain', convId, '.system_generated', 'logs', 'transcript.jsonl');
    try {
      const st = statSync(p);
      const raw = readFileSync(p, 'utf-8');
      const nl = raw.indexOf('\n');
      const firstLine = nl >= 0 ? raw.slice(0, nl) : raw;
      let title = '';
      if (firstLine.trim()) {
        try {
          const obj = JSON.parse(firstLine);
          title = extractCleanUserTitle(obj?.content || '');
        } catch {
          // Ignore malformed line.
        }
      }
      return { path: p, mtimeMs: st.mtimeMs, size: st.size, title };
    } catch {
      // Try next dir.
    }
  }
  return { path: null, mtimeMs: 0, size: -1, title: '' };
}

function parseRpcTrajectory(convId, traj, summary, transcriptInfo) {
  const stepsRaw = Array.isArray(traj?.steps) ? traj.steps : [];
  let derivedTitle = transcriptInfo?.title || '';
  const steps = [];

  const meta = summary?.trajectoryMetadata || traj?.metadata || {};
  const parentConversationId = meta?.parentConversationId || '';
  const isSubagent = Boolean(parentConversationId);
  const projectId = String(meta?.projectId || '');

  const wsList = summary?.workspaces || meta?.workspaces || [];
  const workspaceUriSet = new Set();
  if (Array.isArray(meta?.workspaceUris)) {
    for (const u of meta.workspaceUris) {
      if (typeof u === 'string' && u) workspaceUriSet.add(u);
    }
  }
  if (Array.isArray(wsList)) {
    for (const w of wsList) {
      const u = w?.workspaceFolderAbsoluteUri;
      if (typeof u === 'string' && u) workspaceUriSet.add(u);
    }
  }

  for (let idx = 0; idx < stepsRaw.length; idx++) {
    const s = stepsRaw[idx];
    if (!derivedTitle && s?.type === 'CORTEX_STEP_TYPE_USER_INPUT') {
      const items = s?.userInput?.items;
      if (Array.isArray(items)) {
        derivedTitle = extractCleanUserTitle(items.map((it) => it?.text || '').join(' '));
      }
      if (!derivedTitle && typeof s?.userInput?.userResponse === 'string') {
        derivedTitle = extractCleanUserTitle(s.userInput.userResponse);
      }
    }

    const usage = s?.metadata?.modelUsage;
    if (!usage) continue;

    const inputTokens = toInt(usage.inputTokens);
    const cacheReadTokens = toInt(usage.cacheReadTokens || usage.cacheReadInputTokens);
    const outputTokens = toInt(usage.outputTokens);
    const thinkingTokens = toInt(usage.thinkingOutputTokens);
    const responseTokens =
      usage.responseOutputTokens !== undefined
        ? toInt(usage.responseOutputTokens)
        : Math.max(0, outputTokens - thinkingTokens);
    const totalTokens = inputTokens + cacheReadTokens + outputTokens;
    if (totalTokens === 0) continue;

    const modelId = usage.model || s?.metadata?.generatorModel || 'UNKNOWN_MODEL';
    const createdMs = parseIsoMs(s?.metadata?.createdAt);
    let firstGenMs = parseIsoMs(s?.metadata?.viewableAt);
    const transitions = s?.metadata?.internalMetadata?.statusTransitions;
    if (!firstGenMs && Array.isArray(transitions)) {
      const genTr = transitions.find((t) => t?.updatedStatus === 'CORTEX_STEP_STATUS_GENERATING');
      if (genTr) firstGenMs = parseIsoMs(genTr.timestamp);
    }
    let finishedMs =
      parseIsoMs(s?.metadata?.finishedGeneratingAt) || parseIsoMs(s?.metadata?.completedAt);
    if (!finishedMs && Array.isArray(transitions)) {
      const doneTr = transitions.find((t) => t?.updatedStatus === 'CORTEX_STEP_STATUS_DONE');
      if (doneTr) finishedMs = parseIsoMs(doneTr.timestamp);
    }

    const e2eMs = createdMs !== null && finishedMs !== null && finishedMs > createdMs ? finishedMs - createdMs : null;
    const ttftMs = createdMs !== null && firstGenMs !== null && firstGenMs >= createdMs ? firstGenMs - createdMs : null;
    const rawDecodeMs =
      firstGenMs !== null && finishedMs !== null && finishedMs > firstGenMs ? finishedMs - firstGenMs : null;
    const decodeMs = rawDecodeMs !== null && rawDecodeMs >= 80 ? rawDecodeMs : null;

    const e2eTokPerSec =
      outputTokens > 0 && e2eMs !== null && e2eMs >= 80 ? outputTokens / (e2eMs / 1000) : null;
    const decodeTokPerSec =
      outputTokens > 0 && decodeMs !== null ? outputTokens / (decodeMs / 1000) : e2eTokPerSec;

    steps.push({
      modelId,
      inputTokens,
      cacheReadTokens,
      outputTokens,
      thinkingTokens,
      responseTokens,
      totalTokens,
      contextTokens: inputTokens + cacheReadTokens + responseTokens,
      createdAt: s?.metadata?.createdAt || null,
      ttftMs,
      e2eMs,
      decodeMs,
      decodeTokPerSec,
      e2eTokPerSec,
    });
  }

  const annPb = readAnnotationPbtxt(convId);
  if (!derivedTitle) {
    derivedTitle = extractCleanUserTitle(summary?.annotations?.title || summary?.summary || annPb.title || '');
  }

  const lastModifiedTime =
    summary?.lastModifiedTime ||
    steps[steps.length - 1]?.createdAt ||
    (transcriptInfo?.mtimeMs ? new Date(transcriptInfo.mtimeMs).toISOString() : null);

  return {
    conversationId: convId,
    title: derivedTitle || `会话 ${convId.slice(0, 8)}`,
    status: summary?.status || 'CASCADE_RUN_STATUS_IDLE',
    lastModifiedTime,
    lastModifiedMs: parseIsoMs(lastModifiedTime) || transcriptInfo?.mtimeMs || 0,
    lastUserViewTime: summary?.annotations?.lastUserViewTime || null,
    lastUserViewMs: Math.max(parseIsoMs(summary?.annotations?.lastUserViewTime) || 0, annPb.viewMs),
    lastUserInputTime: summary?.lastUserInputTime || null,
    lastUserInputMs: parseIsoMs(summary?.lastUserInputTime) || 0,
    projectId,
    workspaceUris: [...workspaceUriSet],
    isSubagent,
    steps,
  };
}

function parseTranscriptFallback(convId, transcriptInfo) {
  if (!transcriptInfo?.path) return null;
  let raw = '';
  try {
    raw = readFileSync(transcriptInfo.path, 'utf-8');
  } catch {
    return null;
  }
  let currentModel = 'UNKNOWN_MODEL';
  let derivedTitle = transcriptInfo.title || '';
  const parsedLines = [];
  for (const line of raw.split('\n')) {
    if (!line.trim()) continue;
    try {
      parsedLines.push(JSON.parse(line));
    } catch {
      // Ignore partial line.
    }
  }
  const steps = [];
  for (let i = 0; i < parsedLines.length; i++) {
    const row = parsedLines[i];
    if (row?.type === 'USER_INPUT' && typeof row?.content === 'string') {
      if (!derivedTitle) derivedTitle = extractCleanUserTitle(row.content);
      const m = /changed setting `Model Selection` from [^\n]+? to ([^\n.]+)\./.exec(row.content);
      if (m) currentModel = m[1].trim();
    }
    if (row?.type !== 'PLANNER_RESPONSE') continue;
    const inputTokens = toInt(row.input_tokens);
    const cacheReadTokens = toInt(row.cache_read_tokens);
    const outputTokens = toInt(row.output_tokens);
    const thinkingTokens = toInt(row.thinking_tokens || row.thinkingOutputTokens);
    const responseTokens =
      row.response_tokens !== undefined
        ? toInt(row.response_tokens)
        : row.responseOutputTokens !== undefined
          ? toInt(row.responseOutputTokens)
          : Math.max(0, outputTokens - thinkingTokens);
    const totalTokens = inputTokens + cacheReadTokens + outputTokens;
    if (totalTokens === 0) continue;

    const createdMs = parseIsoMs(row.created_at);
    const nextMs = i + 1 < parsedLines.length ? parseIsoMs(parsedLines[i + 1]?.created_at) : null;
    const e2eMs =
      createdMs !== null && nextMs !== null && nextMs > createdMs && nextMs - createdMs < 300_000
        ? nextMs - createdMs
        : null;
    const e2eTokPerSec =
      outputTokens > 0 && e2eMs !== null && e2eMs >= 500 ? outputTokens / (e2eMs / 1000) : null;
    steps.push({
      modelId: currentModel,
      inputTokens,
      cacheReadTokens,
      outputTokens,
      thinkingTokens,
      responseTokens,
      totalTokens,
      contextTokens: inputTokens + cacheReadTokens + responseTokens,
      createdAt: row.created_at || null,
      ttftMs: null,
      e2eMs,
      decodeMs: null,
      decodeTokPerSec: e2eTokPerSec,
      e2eTokPerSec,
    });
  }
  const annPb = readAnnotationPbtxt(convId);
  return {
    conversationId: convId,
    title: derivedTitle || annPb.title || `会话 ${convId.slice(0, 8)}`,
    status: 'CASCADE_RUN_STATUS_IDLE',
    lastModifiedTime: transcriptInfo.mtimeMs ? new Date(transcriptInfo.mtimeMs).toISOString() : null,
    lastModifiedMs: transcriptInfo.mtimeMs || 0,
    lastUserViewTime: annPb.viewMs ? new Date(annPb.viewMs).toISOString() : null,
    lastUserViewMs: annPb.viewMs,
    lastUserInputTime: null,
    lastUserInputMs: 0,
    projectId: '',
    workspaceUris: [],
    isSubagent: false,
    steps,
  };
}

const convCache = new Map();

async function fetchAndCacheConversation(convId, summary) {
  const tInfo = readTranscriptTitleAndStat(convId);
  const cacheKey = summary
    ? `ls:${summary.stepCount || 0}:${summary.lastModifiedTime || ''}:${summary.status || ''}:${tInfo.size}`
    : `disk:${tInfo.mtimeMs}:${tInfo.size}`;

  const hit = convCache.get(convId);
  if (hit && hit.key === cacheKey) {
    // Refresh view timestamp even on step cache hit so sidebar clicks are instant.
    const annPb = readAnnotationPbtxt(convId);
    const lsViewMs = parseIsoMs(summary?.annotations?.lastUserViewTime) || 0;
    hit.record.lastUserViewMs = Math.max(lsViewMs, annPb.viewMs);
    if (summary?.annotations?.lastUserViewTime) {
      hit.record.lastUserViewTime = summary.annotations.lastUserViewTime;
    }
    return hit.record;
  }

  try {
    const res = await lsRpc('GetCascadeTrajectory', { cascadeId: convId }, 8000);
    if (res?.trajectory) {
      const record = parseRpcTrajectory(convId, res.trajectory, summary, tInfo);
      convCache.set(convId, { key: cacheKey, record });
      return record;
    }
  } catch {
    // Fallback to local transcript.
  }

  const fallback = parseTranscriptFallback(convId, tInfo);
  if (fallback) {
    convCache.set(convId, { key: cacheKey, record: fallback });
    return fallback;
  }
  return hit?.record || null;
}

// ---------------------------------------------------------------------------
// Desktop Pet State, Launch & Config Sync
// ---------------------------------------------------------------------------

function isPetRunning() {
  const recoveryPath = join(PET_ROOT, 'session_recovery.json');
  try {
    const data = JSON.parse(readFileSync(recoveryPath, 'utf-8'));
    const hb = Number(data?.last_heartbeat || 0);
    const ageSec = Date.now() / 1000 - hb;
    return {
      running: Boolean(data?.active) && ageSec >= -5 && ageSec < 45,
      version: String(data?.version || ''),
      lastHeartbeat: hb,
    };
  } catch {
    return { running: false, version: '', lastHeartbeat: 0 };
  }
}

const DEFAULT_AGY_CONFIG = {
  form: 'carbon',
  follow_model: false,
  telemetry_enabled: true,
  use_language_server: true,
  reactions: true,
  autonomy: true,
  hop_to_window: true,
  explore_windows: true,
  hud_enabled: true,
  hud_visibility: 'show',
  hud_auto_hide_seconds: 5,
  hud_mode: 'attached',
  hud_appearance: 'dialog',
  hud_locked: false,
  hud_x: null,
  hud_y: null,
  pinned_conversation: '',
};

function clampAutoHideSeconds(val, fallback = 5) {
  const n = Number(val);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(60, Math.max(1, Math.round(n)));
}

function normalizeAgyHud(agy) {
  const out = { ...DEFAULT_AGY_CONFIG, ...agy };
  if (typeof agy?.hud_visibility === 'string' && VALID_HUD_VISIBILITIES.has(agy.hud_visibility)) {
    out.hud_visibility = agy.hud_visibility;
  } else {
    out.hud_visibility = out.hud_enabled === false ? 'hide' : 'show';
  }
  out.hud_enabled = out.hud_visibility !== 'hide';
  out.hud_auto_hide_seconds = clampAutoHideSeconds(out.hud_auto_hide_seconds, 5);
  return out;
}

function readPetConfig() {
  const cfgPath = join(PET_ROOT, 'config.json');
  try {
    const raw = JSON.parse(readFileSync(cfgPath, 'utf-8'));
    return normalizeAgyHud(raw?.agy && typeof raw.agy === 'object' ? raw.agy : {});
  } catch {
    return { ...DEFAULT_AGY_CONFIG };
  }
}

function updatePetConfig(patch) {
  const cfgPath = join(PET_ROOT, 'config.json');
  let raw = {};
  try {
    const loaded = JSON.parse(readFileSync(cfgPath, 'utf-8'));
    if (loaded && typeof loaded === 'object' && !Array.isArray(loaded)) {
      raw = loaded;
    }
  } catch {
    raw = {};
  }
  const agy = raw.agy && typeof raw.agy === 'object' && !Array.isArray(raw.agy) ? { ...raw.agy } : { ...DEFAULT_AGY_CONFIG };

  if (typeof patch.form === 'string' && VALID_FORM_IDS.has(patch.form)) {
    agy.form = patch.form;
  }
  for (const boolKey of ['follow_model', 'hud_enabled', 'hud_locked', 'autonomy', 'reactions']) {
    if (typeof patch[boolKey] === 'boolean') {
      agy[boolKey] = patch[boolKey];
    }
  }
  if (typeof patch.hud_visibility === 'string' && VALID_HUD_VISIBILITIES.has(patch.hud_visibility)) {
    agy.hud_visibility = patch.hud_visibility;
    agy.hud_enabled = patch.hud_visibility !== 'hide';
  } else if (typeof patch.hud_enabled === 'boolean') {
    agy.hud_visibility = patch.hud_enabled ? 'show' : 'hide';
  }
  if (patch.hud_auto_hide_seconds !== undefined) {
    agy.hud_auto_hide_seconds = clampAutoHideSeconds(patch.hud_auto_hide_seconds, 5);
  }
  if (typeof patch.hud_mode === 'string' && VALID_HUD_MODES.has(patch.hud_mode)) {
    agy.hud_mode = patch.hud_mode;
  }
  if (typeof patch.hud_appearance === 'string' && VALID_HUD_APPEARANCES.has(patch.hud_appearance)) {
    agy.hud_appearance = patch.hud_appearance;
  }
  if (typeof patch.pinned_conversation === 'string') {
    agy.pinned_conversation = patch.pinned_conversation.slice(0, 128);
  }

  raw.agy = agy;
  atomicWriteJson(cfgPath, raw);
  return normalizeAgyHud(agy);
}

let lastAutoLaunchAttempt = 0;

function hasPySide6(cmd, args = []) {
  try {
    execFileSync(cmd, [...args, '-c', 'import PySide6'], {
      windowsHide: true,
      timeout: 2500,
      stdio: 'ignore',
    });
    return true;
  } catch {
    return false;
  }
}

function resolveLaunchCandidates() {
  const candidates = [];
  const seen = new Set();
  const pushUnique = (cmd, args) => {
    const key = `${cmd}\0${args.join('\0')}`;
    if (!seen.has(key)) {
      seen.add(key);
      candidates.push([cmd, args]);
    }
  };

  const venvPyw = join(PET_ROOT, '.venv', 'Scripts', 'pythonw.exe');
  const venvPyWin = join(PET_ROOT, '.venv', 'Scripts', 'python.exe');
  const venvPy = join(PET_ROOT, '.venv', 'bin', 'python');
  if (existsSync(venvPyw)) pushUnique(venvPyw, ['main.py']);
  if (existsSync(venvPyWin)) pushUnique(venvPyWin, ['main.py']);
  if (existsSync(venvPy)) pushUnique(venvPy, ['main.py']);

  if (candidates.length === 0) {
    if (hasPySide6('py')) {
      pushUnique('pyw', ['main.py']);
      pushUnique('py', ['main.py']);
    }
    if (hasPySide6('python')) {
      pushUnique('pythonw', ['main.py']);
      pushUnique('python', ['main.py']);
    }
    if (hasPySide6('python3')) {
      pushUnique('python3', ['main.py']);
    }
  }

  pushUnique('pyw', ['main.py']);
  pushUnique('pythonw', ['main.py']);
  pushUnique('python3', ['main.py']);
  pushUnique('python', ['main.py']);
  return candidates;
}

function launchPet() {
  return new Promise((resolve) => {
    const mainPy = join(PET_ROOT, 'main.py');
    if (!existsSync(mainPy)) {
      resolve({ ok: false, error: `未找到桌宠入口文件: ${mainPy}` });
      return;
    }
    const candidates = resolveLaunchCandidates();
    const attempt = (idx) => {
      if (idx >= candidates.length) {
        resolve({ ok: false, error: '未找到可用的 Python 桌宠运行环境（需 Python 3 与 PySide6，推荐先创建项目 .venv）' });
        return;
      }
      const [cmd, args] = candidates[idx];
      try {
        const child = spawn(cmd, args, {
          cwd: PET_ROOT,
          detached: true,
          stdio: 'ignore',
          windowsHide: true,
        });
        let settled = false;
        child.once('error', () => {
          if (settled) return;
          settled = true;
          attempt(idx + 1);
        });
        child.once('spawn', () => {
          if (settled) return;
          settled = true;
          child.unref();
          resolve({ ok: true, command: cmd });
        });
      } catch {
        attempt(idx + 1);
      }
    };
    attempt(0);
  });
}

// ---------------------------------------------------------------------------
// Live Focus Tracking & pet_live.json Writer (every 2.0s)
// ---------------------------------------------------------------------------

let tickBusy = false;
let lastPetPayload = null;
let hostFocus = { conversationId: '', workspaceUris: [], updatedMs: 0 };

function resolveModelLabel(modelId, labelMap) {
  return labelMap.get(modelId) || prettifyModelEnum(modelId);
}

function buildConvHud(rec, modelState) {
  const { labelMap, quotaMap, limitMap, selectedModelId } = modelState;
  let inputSum = 0;
  let cacheSum = 0;
  let outputSum = 0;
  let decodeOut = 0;
  let decodeMs = 0;

  for (const s of rec.steps) {
    inputSum += s.inputTokens;
    cacheSum += s.cacheReadTokens;
    outputSum += s.outputTokens;
    const effMs = s.decodeMs ?? (s.e2eMs !== null && s.e2eMs >= 80 ? s.e2eMs : null);
    if (s.outputTokens > 0 && effMs !== null && effMs >= 80) {
      decodeOut += s.outputTokens;
      decodeMs += effMs;
    }
  }

  const latest = rec.steps.length > 0 ? rec.steps[rec.steps.length - 1] : null;
  const effectiveModelId = selectedModelId || latest?.modelId || '';
  const effectiveModelLabel = effectiveModelId ? resolveModelLabel(effectiveModelId, labelMap) : '未选择模型';
  const quota = quotaMap.get(effectiveModelId) || (latest ? quotaMap.get(latest.modelId) : null) || null;

  const contextUsedTokens = latest ? latest.contextTokens : 0;
  const isRunning = rec.status === 'CASCADE_RUN_STATUS_RUNNING';
  const contextModelId = (isRunning && latest ? latest.modelId : effectiveModelId) || '';
  const contextModelLabel = contextModelId ? resolveModelLabel(contextModelId, labelMap) : '';
  const contextLimitTokens = inferContextWindowLimit(contextModelId, contextModelLabel, limitMap);
  const contextLimitSource = limitMap?.has?.(contextModelId) ? 'ls' : 'default';
  const contextUsedPct = contextLimitTokens > 0 ? (contextUsedTokens / contextLimitTokens) * 100 : 0;

  const promptSum = inputSum + cacheSum;
  const latestPromptTotal = latest ? latest.inputTokens + latest.cacheReadTokens : 0;

  return {
    conversationId: rec.conversationId,
    title: rec.title,
    status: rec.status,
    steps: rec.steps.length,
    lastModifiedTime: rec.lastModifiedTime,
    lastUserViewTime: rec.lastUserViewTime,
    lastUserInputTime: rec.lastUserInputTime,
    projectId: rec.projectId,
    workspaceUris: rec.workspaceUris,
    selectedModelId: effectiveModelId,
    selectedModelLabel: effectiveModelLabel,
    lastStepModelLabel: latest ? resolveModelLabel(latest.modelId, labelMap) : '',
    latestDecodeTokPerSec: latest?.decodeTokPerSec ?? null,
    latestE2eTokPerSec: latest?.e2eTokPerSec ?? null,
    latestTtftMs: latest?.ttftMs ?? null,
    avgDecodeTokPerSec: decodeMs > 0 ? decodeOut / (decodeMs / 1000) : null,
    quotaRemainingPct: quota?.remainingPct ?? null,
    quotaResetTime: quota?.resetTime ?? null,
    cacheHitRatePct: promptSum > 0 ? (cacheSum / promptSum) * 100 : 0,
    latestCacheHitRatePct: latestPromptTotal > 0 ? (latest.cacheReadTokens / latestPromptTotal) * 100 : null,
    contextUsedTokens,
    contextTokensExcludesThinking: true,
    contextLimitTokens,
    contextLimitSource,
    contextUsedPct,
    cumulativeTotalTokens: inputSum + cacheSum + outputSum,
  };
}

function emptyConvHud(cid, title, modelState, focusSource) {
  const { labelMap, quotaMap, limitMap, selectedModelId } = modelState;
  const selectedModelLabel = selectedModelId ? resolveModelLabel(selectedModelId, labelMap) : '未选择模型';
  const quota = quotaMap.get(selectedModelId) || null;
  const limit = inferContextWindowLimit(selectedModelId, selectedModelLabel, limitMap);
  return {
    conversationId: cid,
    title,
    status: 'CASCADE_RUN_STATUS_IDLE',
    steps: 0,
    lastModifiedTime: new Date().toISOString(),
    selectedModelId,
    selectedModelLabel,
    lastStepModelLabel: '',
    latestDecodeTokPerSec: null,
    latestE2eTokPerSec: null,
    latestTtftMs: null,
    avgDecodeTokPerSec: null,
    quotaRemainingPct: quota?.remainingPct ?? null,
    quotaResetTime: quota?.resetTime ?? null,
    cacheHitRatePct: 0,
    latestCacheHitRatePct: null,
    contextUsedTokens: 0,
    contextTokensExcludesThinking: true,
    contextLimitTokens: limit,
    contextLimitSource: limitMap?.has?.(selectedModelId) ? 'ls' : 'default',
    contextUsedPct: 0,
    cumulativeTotalTokens: 0,
    focusSource,
  };
}

async function updatePetLiveFile() {
  if (tickBusy) return lastPetPayload;
  tickBusy = true;
  try {
    const [modelState, allTrajRes, quotaSummary] = await Promise.all([
      getUserModelState(10_000),
      lsRpc('GetAllCascadeTrajectories', {}, 4000).catch(() => ({ trajectorySummaries: {} })),
      getQuotaSummary(60_000),
    ]);

    const { labelMap, quotaMap, selectedModelId } = modelState;
    const summaries = allTrajRes?.trajectorySummaries || {};
    const hook = readActiveSessionHook();
    const proj = latestProjectSwitch();

    const mainEntries = Object.entries(summaries).filter(
      ([, s]) => !s?.trajectoryMetadata?.parentConversationId
    );

    const scored = mainEntries.map(([cid, sum]) => {
      const annPb = readAnnotationPbtxt(cid);
      const lsViewMs = parseIsoMs(sum?.annotations?.lastUserViewTime) || 0;
      const viewMs = Math.max(lsViewMs, annPb.viewMs);
      const inputMs = parseIsoMs(sum?.lastUserInputTime) || 0;
      const hookMs = hook && hook.conversationId === cid ? hook.updatedMs : 0;
      const hostMs = hostFocus.conversationId === cid ? hostFocus.updatedMs : 0;
      const focusMs = Math.max(viewMs, inputMs, hookMs, hostMs);
      let focusSource = 'recent';
      if (focusMs > 0) {
        if (focusMs === hookMs) focusSource = hook?.source || 'hook';
        else if (focusMs === hostMs) focusSource = 'host';
        else if (focusMs === viewMs) focusSource = 'view';
        else if (focusMs === inputMs) focusSource = 'input';
      } else if (sum?.status === 'CASCADE_RUN_STATUS_RUNNING') {
        focusSource = 'running';
      }

      const meta = sum?.trajectoryMetadata || {};
      const wsSet = new Set();
      for (const u of meta?.workspaceUris || []) {
        const n = normalizeWorkspace(u);
        if (n) wsSet.add(n);
      }
      for (const w of sum?.workspaces || meta?.workspaces || []) {
        const n = normalizeWorkspace(w?.workspaceFolderAbsoluteUri || '');
        if (n) wsSet.add(n);
      }

      return {
        cid,
        sum,
        focusMs,
        focusSource,
        running: sum?.status === 'CASCADE_RUN_STATUS_RUNNING' ? 1 : 0,
        modMs: parseIsoMs(sum?.lastModifiedTime) || 0,
        projectId: String(meta?.projectId || ''),
        wsSet,
      };
    });

    const maxFocusMs = scored.reduce((m, item) => Math.max(m, item.focusMs), 0);
    const projMs = proj ? proj.mtimeMs : 0;
    const activeCandidateId =
      hostFocus.updatedMs >= (hook?.updatedMs || 0) && hostFocus.conversationId
        ? { cid: hostFocus.conversationId, title: '', ms: hostFocus.updatedMs, source: 'host' }
        : hook
          ? {
              cid: hook.conversationId,
              title: extractCleanUserTitle(hook.lastUserInput || hook.title || '') || '新对话',
              ms: hook.updatedMs,
              source: hook.source || 'hook',
            }
          : null;

    let emptyActive = null;
    let pool = scored;
    if (
      activeCandidateId &&
      !scored.some((item) => item.cid === activeCandidateId.cid) &&
      activeCandidateId.ms > maxFocusMs &&
      activeCandidateId.ms >= projMs - 2000
    ) {
      emptyActive = emptyConvHud(
        activeCandidateId.cid,
        activeCandidateId.title || '新对话',
        modelState,
        activeCandidateId.source
      );
    } else if (proj && projMs > maxFocusMs + 2000) {
      const matched = scored.filter(
        (item) =>
          (item.projectId && item.projectId === proj.id) ||
          [...item.wsSet].some((ws) => proj.workspaces.has(ws))
      );
      if (matched.length > 0) {
        pool = matched;
      } else if (scored.some((item) => item.projectId || item.wsSet.size > 0)) {
        const title = proj.name ? `${proj.name} · 新对话` : '新对话';
        emptyActive = emptyConvHud(`project:${proj.id}`, title, modelState, 'project_empty');
      }
    }

    const hasFocus = pool.some((item) => item.focusMs > 0);
    pool.sort((a, b) => {
      if (hasFocus && a.focusMs !== b.focusMs) return b.focusMs - a.focusMs;
      if (a.running !== b.running) return b.running - a.running;
      return b.modMs - a.modMs;
    });

    const topPool = pool.slice(0, 10);
    const records = (
      await Promise.all(topPool.map((item) => fetchAndCacheConversation(item.cid, item.sum)))
    ).filter(Boolean);

    const focusSourceById = new Map(topPool.map((item) => [item.cid, item.focusSource]));
    const convSnapList = records.map((rec) => {
      const snap = buildConvHud(rec, modelState);
      snap.focusSource = focusSourceById.get(rec.conversationId) || 'recent';
      return snap;
    });

    const active = emptyActive || convSnapList[0] || null;
    const selectedQuota = quotaMap.get(selectedModelId) || null;
    const selectedModelLabel = selectedModelId ? resolveModelLabel(selectedModelId, labelMap) : '';

    const payload = {
      updatedAt: new Date().toISOString(),
      updatedMs: Date.now(),
      selectedModelId,
      selectedModelLabel,
      selectedQuotaRemainingPct: selectedQuota?.remainingPct ?? null,
      selectedQuotaResetTime: selectedQuota?.resetTime ?? null,
      quotaBuckets: quotaSummary.buckets,
      quotaSummaryStatus: quotaSummary.status,
      activeConversationId: active?.conversationId || '',
      focusSource: active?.focusSource || '',
      active,
      conversations: convSnapList,
    };

    atomicWriteJson(join(resolvePetDataDir(), 'pet_live.json'), payload);
    lastPetPayload = payload;

    const bridge = readBridgeSettings();
    if (bridge.autoLaunchPet && !isPetRunning().running && Date.now() - lastAutoLaunchAttempt > 60_000) {
      lastAutoLaunchAttempt = Date.now();
      launchPet().catch(() => {});
    }
    return payload;
  } catch {
    return lastPetPayload;
  } finally {
    tickBusy = false;
  }
}

updatePetLiveFile();
setInterval(updatePetLiveFile, 2000).unref();

// ---------------------------------------------------------------------------
// Web UI & API Routes
// ---------------------------------------------------------------------------

app.page('/', () => readFileSync(join(__dirname, 'index.html'), 'utf-8'));
app.page('/styles.css', () =>
  new Response(readFileSync(join(__dirname, 'styles.css'), 'utf-8'), {
    contentType: 'text/css; charset=utf-8',
  })
);
app.page('/app.js', () =>
  new Response(readFileSync(join(__dirname, 'app.js'), 'utf-8'), {
    contentType: 'application/javascript; charset=utf-8',
  })
);

app.api(
  '/api/state',
  async () => {
    if (!lastPetPayload || Date.now() - (lastPetPayload.updatedMs || 0) > 2500) {
      await updatePetLiveFile();
    }
    const petStatus = isPetRunning();
    return {
      petRunning: petStatus.running,
      petStatus,
      agyConfig: readPetConfig(),
      bridgeSettings: readBridgeSettings(),
      forms: PET_FORMS,
      hostConversationId: app.config.conversationId || '',
      live: lastPetPayload,
    };
  },
  'GET'
);

app.api(
  '/api/host-focus',
  async (data) => {
    const cid = typeof data?.conversationId === 'string' ? data.conversationId.trim() : '';
    const uris = Array.isArray(data?.workspaceUris) ? data.workspaceUris.filter((u) => typeof u === 'string') : [];
    const initial = Boolean(data?.initial);
    if (cid && UUID_RE.test(cid) && (initial || cid !== hostFocus.conversationId)) {
      const now = Date.now();
      hostFocus = { conversationId: cid, workspaceUris: uris, updatedMs: now };
      try {
        atomicWriteJson(resolveActiveSessionPath(), {
          conversationId: cid,
          workspacePaths: uris,
          hookEventName: 'SidecarHostFocus',
          source: 'host',
          updatedMs: now,
          updatedAt: new Date(now).toISOString(),
        });
      } catch {
        // Non-fatal.
      }
      await updatePetLiveFile();
    }
    return { ok: true, hostFocus };
  },
  'POST'
);

app.api(
  '/api/pet/launch',
  async () => {
    const res = await launchPet();
    return { ...res, petStatus: isPetRunning() };
  },
  'POST'
);

app.api(
  '/api/pet/config',
  async (data) => {
    const patch = data && typeof data === 'object' ? data : {};
    const agyConfig = updatePetConfig(patch);
    let bridgeSettings = readBridgeSettings();
    if (typeof patch.autoLaunchPet === 'boolean') {
      bridgeSettings = writeBridgeSettings({ autoLaunchPet: patch.autoLaunchPet });
    }
    return { ok: true, agyConfig, bridgeSettings };
  },
  'POST'
);

await app.run();
