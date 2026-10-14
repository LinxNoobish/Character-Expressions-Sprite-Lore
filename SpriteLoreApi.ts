const URL_KEY = 'spritelore.serverUrl';
export const DEFAULT_SERVER = 'http://127.0.0.1:5181';
/** True when the page is the standalone build served by the Sprite Lore server itself. */
export const STANDALONE =
  typeof document !== 'undefined' && !!document.querySelector('meta[name="spritelore-standalone"]');

export function getServerUrl(): string {
  if (STANDALONE && /^https?:$/.test(location.protocol)) return location.origin;
  try {
    return localStorage.getItem(URL_KEY) || DEFAULT_SERVER;
  } catch {
    return DEFAULT_SERVER;
  }
}

export function setServerUrl(url: string): void {
  try {
    localStorage.setItem(URL_KEY, url.replace(/\/+$/, ''));
  } catch {
    /* storage unavailable */
  }
}

export type BackendRole = 'vlm' | 'llm' | 'both';
export type JsonMode = 'off' | 'json_object' | 'schema';

export interface Backend {
  id: string;
  name: string;
  url: string;
  model: string;
  apiKey: string;
  role: BackendRole;
  temperature: number;
  maxTokens: number;
  jsonMode: JsonMode;
  timeout: number;
}

export interface LoreTemplate {
  id: string;
  name: string;
  temperature: number | null;
  system: string;
  user: string;
}

export interface LadderStep {
  id: string;
  name: string;
  desc: string;
}

export interface Direction {
  id: string;
  name: string;
  desc: string;
}

export interface EmotionRule {
  rule: string;
  tags: string[];
  directions: string[];
}

export interface TagGroup {
  name: string;
  tags: string[];
}

export interface Settings {
  version: number;
  roots: string[];
  backends: Backend[];
  activeVlm: string;
  activeLlm: string;
  activeTemplate: string;
  templates: LoreTemplate[];
  taggerTemplate: { system: string; user: string };
  intensity: {
    ladder: LadderStep[];
    single: string;
    spread: Record<string, string[]>;
    orderBy: 'vlm' | 'name';
  };
  directions: Direction[];
  defaultRotation: string[];
  emotionRules: Record<string, EmotionRule>;
  tagGroups: TagGroup[];
  imgprompt: {
    anime2imgDir: string;
    comfyInput: string;
    activeTemplate: string;
    backend: string;
  };
  lore: {
    words: number;
    maxChars: number;
    keywords: number;
    priorMax: number;
    cardChars: number;
    retries: number;
    siblingAware: boolean;
    renameMinConfidence: number;
  };
}

export type SettingsSection = keyof Settings;

export const PROFILE_KEYS = [
  'templates',
  'activeTemplate',
  'taggerTemplate',
  'intensity',
  'directions',
  'defaultRotation',
  'emotionRules',
  'tagGroups',
  'lore',
] as const;
export type ProfileKey = (typeof PROFILE_KEYS)[number];
export type ProfileData = Pick<Settings, ProfileKey> & { saved?: string };

export interface VlmData {
  emotion: string;
  confidence: number;
  intensity?: number;
  directed_at?: string;
  tags: string[];
  categories?: Record<string, string[]>;
  description?: string;
  model?: string;
  ts?: string;
}

export interface LoreData {
  text: string;
  keywords: string[];
  label?: string;
  intensity?: string;
  direction?: string;
  template?: string;
  profile?: string;
  tags?: string[];
  model?: string;
  ts?: string;
  pxhash?: string;
  src?: string;
  reviewed?: boolean;
  stale?: boolean;
}

export interface ExtraEntry {
  title: string;
  text: string;
  keywords: string[];
}

export interface SpriteMeta {
  pxhash?: string;
  orig_name?: string;
  char?: string;
  label?: string;
  vlm?: VlmData;
  lore?: LoreData;
  entries?: ExtraEntry[];
  plan?: { intensity?: string; direction?: string };
  imgprompts?: Record<string, ImgPromptSummary>;
  [key: string]: unknown;
}

export interface MergeChange {
  id: string;
  field: 'system' | 'user';
  kind: 'insert' | 'modify' | 'delete';
  status: 'applied' | 'merged' | 'already' | 'conflict';
  mine: string;
  new: string;
  base: string;
  result?: string;
  where?: string;
  how?: string;
  choice?: 'mine' | 'new';
}

export interface UpgradeReport {
  id: string;
  name: string;
  status: 'clean' | 'conflicts' | 'current' | 'custom' | 'no-successor' | 'none';
  base: string | null;
  changes: MergeChange[];
  merged: LoreTemplate | null;
}

export interface PromptPair {
  positive: string;
  negative: string;
}

export interface ImgPromptSummary {
  template: string;
  out: string;
  cached: boolean;
  ts?: string;
  ages: number[];
  base: PromptPair;
  variations: (PromptPair & { label: string; change: string })[];
  removed: string[];
  notes: string;
}

export interface ImgPromptTemplate {
  name: string;
  text: string;
  hasOrig: boolean;
  output: string;
  detail: string;
  style: string;
  variations: string;
  vary: string;
  ageRange: string;
}

export interface ImgPromptStatus {
  candidates?: { dir: string; mtime: number }[];
  dir: string | null;
  found: boolean;
  templates: ImgPromptTemplate[];
  output: string | null;
  error: string | null;
}

export interface Plan {
  intensity: string;
  direction: string;
  autoIntensity: string;
  autoDirection: string;
  intensityLocked: boolean;
  directionLocked: boolean;
  rank: number;
  groupSize: number;
}

export interface SpriteImage {
  name: string;
  path: string;
  ext: string;
  isPng: boolean;
  size: number;
  mtime: number;
  label: string;
  meta: SpriteMeta | null;
  tagged: boolean;
  hasLore: boolean;
  stale: boolean;
  error: string | null;
  plan: Plan;
  suggest: string | null;
  /** the user kept this name over the tagger's current suggestion */
  kept?: boolean;
}

export interface CardInfo {
  name: string;
  path: string;
  description?: string;
  personality?: string;
  scenario?: string;
  error?: string;
}

export interface Guide {
  tags: string[];
  notes: string;
  instruction: string;
  profile: string;
}

export interface FolderData {
  folder: string;
  name: string;
  card: CardInfo | null;
  labels: string[];
  customLabels: string[];
  guide: Guide;
  profile: string;
  profiles: string[];
  effective: ProfileData;
  images: SpriteImage[];
}

export interface FolderEntry {
  name: string;
  path: string;
  root: string;
  images: number;
  card: boolean;
}

export interface TextChunk {
  type: string;
  key: string;
  length: number;
  value: string;
  json: unknown;
}

export interface ChatMessage {
  role: string;
  content: string;
}

export interface GenerateResult {
  lore: LoreData;
  raw: string;
  messages: ChatMessage[];
  plan: Plan;
  label: string;
  template: string;
  tags: string[];
  written: boolean;
  meta?: SpriteMeta;
}

export interface PromptPreview {
  messages: ChatMessage[];
  plan: Plan;
  label: string;
  template: string;
  tags: string[];
}

export interface BackendTest {
  ok: boolean;
  models: string[];
  vision: { ok: boolean; answers: Record<string, string> } | null;
  error: string | null;
}

export interface GenerateRequest {
  path: string;
  backendId?: string;
  templateId?: string;
  intensity?: string;
  direction?: string;
  tags?: string[];
  pending?: Record<string, string>;
  write?: boolean;
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(getServerUrl() + path, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new Error(`Sprite Lore server not reachable at ${getServerUrl()}`);
  }
  const data = (await res.json().catch(() => ({}))) as { error?: string };
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data as T;
}

const q = (p: string) => encodeURIComponent(p);

export const spriteLoreApi = {
  health: () => call<{ ok: boolean; dir: string }>('GET', '/api/health'),
  getSettings: () =>
    call<{ settings: Settings; defaults: Settings; hasPrev: boolean }>('GET', '/api/settings'),
  saveSettings: (settings: Settings) => call<{ settings: Settings }>('PUT', '/api/settings', { settings }),
  resetSettings: (section?: SettingsSection) =>
    call<{ settings: Settings }>('POST', '/api/settings/reset', section ? { section } : {}),
  undoSettings: () => call<{ settings: Settings }>('POST', '/api/settings/undo', {}),
  folders: () => call<{ roots: string[]; folders: FolderEntry[] }>('GET', '/api/folders'),
  folder: (path: string) => call<FolderData>('GET', `/api/folder?path=${q(path)}`),
  chunks: (path: string) => call<{ chunks: TextChunk[] }>('GET', `/api/chunks?path=${q(path)}`),
  saveMeta: (path: string, meta: SpriteMeta, touchLore: boolean) =>
    call<{ meta: SpriteMeta }>('PUT', '/api/meta', { path, meta, touchLore }),
  savePlan: (path: string, intensity: string, direction: string) =>
    call<{ meta: SpriteMeta }>('PUT', '/api/plan', { path, intensity, direction }),
  saveGuide: (folder: string, fields: Partial<Guide>) =>
    call<{ guide: Guide }>('PUT', '/api/guide', { folder, ...fields }),
  upgradeTemplates: (templates: LoreTemplate[], choices: Record<string, Record<string, 'mine' | 'new'>>) =>
    call<{ reports: UpgradeReport[] }>('POST', '/api/templates/upgrade', { templates, choices }),
  getProfiles: () => call<{ profiles: Record<string, ProfileData> }>('GET', '/api/profiles'),
  saveProfile: (name: string, data: ProfileData, renameFrom?: string) =>
    call<{ name: string; profiles: Record<string, ProfileData> }>('PUT', '/api/profile', { name, data, renameFrom }),
  deleteProfile: (name: string) =>
    call<{ profiles: Record<string, ProfileData> }>('POST', '/api/profile/delete', { name }),
  renameSuggested: (folder: string, paths?: string[]) =>
    call<{ moves: { from: string; to: string; emotion: string; confidence: number }[]; log: string | null }>(
      'POST',
      '/api/rename-suggested',
      { folder, paths },
    ),
  keepNames: (folder: string, paths?: string[], clear = false) =>
    call<{ changed: string[] }>('POST', '/api/keep-names', { folder, paths, clear }),
  undoAvailable: (folder: string) => call<{ available: boolean }>('GET', `/api/rename-undo?folder=${q(folder)}`),
  undoRenames: (folder: string) =>
    call<{ restored: { from: string; to: string }[]; skipped: string[] }>('POST', '/api/rename-undo', { folder }),
  tag: (path: string, backendId: string, hint = false) =>
    call<{ vlm: VlmData; raw: string; mismatch: boolean; path: string }>('POST', '/api/tag', { path, backendId, hint }),
  generate: (req: GenerateRequest) => call<GenerateResult>('POST', '/api/generate', req),
  previewPrompt: (req: GenerateRequest) =>
    call<PromptPreview>('POST', '/api/generate', { ...req, previewOnly: true }),
  writeLore: (path: string, lore: LoreData) => call<{ meta: SpriteMeta }>('POST', '/api/lore', { path, lore }),
  rename: (path: string, label: string) => call<{ path: string }>('POST', '/api/rename', { path, label }),
  renameTo: (path: string, name: string) => call<{ path: string }>('POST', '/api/rename', { path, name }),
  convert: (path: string) => call<{ path: string }>('POST', '/api/convert', { path }),
  convertFolder: (folder: string, includeGif = false) =>
    call<{ converted: { from: string; to: string }[]; skipped: string[]; errors: string[]; originals: string | null }>(
      'POST',
      '/api/convert-folder',
      { folder, includeGif },
    ),
  imgPromptStatus: () => call<ImgPromptStatus>('GET', '/api/imgprompt/status'),
  saveImgTemplate: (name: string, text: string, create = false) =>
    call<ImgPromptStatus>('PUT', '/api/imgprompt/template', { name, text, create }),
  restoreImgTemplate: (name: string) => call<ImgPromptStatus>('POST', '/api/imgprompt/restore', { name }),
  runImgPrompt: (path: string, template: string, backendId: string, force = false) =>
    call<ImgPromptSummary>('POST', '/api/imgprompt/run', { path, template, backendId, force }),
  testBackend: (backend: Backend) => call<BackendTest>('POST', '/api/backend/test', { backend }),
};

export function imageUrl(path: string, width = 0): string {
  return `${getServerUrl()}/api/image?path=${q(path)}${width ? `&w=${width}` : ''}`;
}

export function toggleIn(list: string[], item: string): string[] {
  return list.includes(item) ? list.filter((x) => x !== item) : [...list, item];
}

export function addUnique(list: string[], item: string): string[] {
  return list.includes(item) ? list : [...list, item];
}

export function splitList(text: string): string[] {
  return text
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean);
}

export function stLabel(fileName: string, labels: string[]): string {
  const stem = fileName.replace(/\.[^.]+$/, '').toLowerCase();
  if (labels.includes(stem)) return stem;
  const head = stem.split('-', 1)[0];
  return labels.includes(head) ? head : stem;
}

export function pickProfile(s: Settings): ProfileData {
  const out = {} as Record<string, unknown>;
  PROFILE_KEYS.forEach((k) => {
    out[k] = structuredClone(s[k]);
  });
  return out as ProfileData;
}
