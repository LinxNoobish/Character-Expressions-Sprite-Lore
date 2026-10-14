import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  ArrowLeft,
  RotateCcw,
  Pencil,
  BookOpen,
  Check,
  CheckSquare,
  Copy,
  Eye,
  FileJson,
  FolderOpen,
  ImageOff,
  ImagePlus,
  Loader2,
  Lock,
  Plus,
  RefreshCw,
  Save,
  Search,
  Settings2,
  Sparkles,
  Square,
  StopCircle,
  Tags,
  Trash2,
  Wand2,
  X,
} from 'lucide-react';
import { SpriteLoreSettings } from './SpriteLoreSettings';
import { TagChips } from './SpriteLoreTags';
import { STANDALONE, addUnique, getServerUrl, imageUrl, splitList, spriteLoreApi, stLabel, toggleIn } from './SpriteLoreApi';
import type { Guide, ProfileData } from './SpriteLoreApi';
import type {
  ChatMessage,
  ExtraEntry,
  FolderData,
  FolderEntry,
  ImgPromptStatus,
  ImgPromptSummary,
  LoreData,
  Settings,
  SpriteImage,
  SpriteMeta,
  TextChunk,
} from './SpriteLoreApi';

const AUTO_KEY = 'spritelore.autoAccept';
const LAST_FOLDER_KEY = 'spritelore.lastFolder';

function readLocal(key: string, fallback: string): string {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function writeLocal(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* storage unavailable */
  }
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

const inputCls =
  'w-full rounded-lg border border-border bg-bg px-2 py-1.5 text-sm text-fg outline-none focus:border-accent';
const btnCls =
  'inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-xs font-medium text-fg-muted hover:text-fg hover:border-accent/60 transition-colors disabled:opacity-50 disabled:pointer-events-none';
const primaryCls =
  'inline-flex items-center gap-1.5 rounded-lg bg-accent px-2.5 py-1.5 text-xs font-semibold text-accent-fg hover:opacity-90 transition-opacity disabled:opacity-50 disabled:pointer-events-none';
const selectCls =
  'rounded-lg border border-border bg-bg px-2 py-1 text-xs text-fg outline-none focus:border-accent max-w-[12rem]';

type Busy = 'tag' | 'gen' | 'img' | 'save';
type DetailTab = 'lore' | 'tags' | 'image' | 'raw' | 'prompt';

interface BatchState {
  label: string;
  done: number;
  total: number;
  failed: number;
}

export const SpriteLorePage: React.FC = () => {
  const [server, setServer] = useState<'checking' | 'ok' | string>('checking');
  const [settings, setSettings] = useState<Settings | null>(null);
  const [defaults, setDefaults] = useState<Settings | null>(null);
  const [hasPrev, setHasPrev] = useState(false);
  const [folders, setFolders] = useState<FolderEntry[]>([]);
  const [folderFilter, setFolderFilter] = useState('');
  const [folderPath, setFolderPath] = useState<string>(() => readLocal(LAST_FOLDER_KEY, ''));
  const [data, setData] = useState<FolderData | null>(null);
  const [folderError, setFolderError] = useState<string | null>(null);
  const [focusPath, setFocusPath] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [pending, setPending] = useState<Record<string, LoreData>>({});
  const [busy, setBusy] = useState<Record<string, Busy>>({});
  const [batch, setBatch] = useState<BatchState | null>(null);
  const [autoAccept, setAutoAccept] = useState(() => readLocal(AUTO_KEY, '0') === '1');
  const [settingsOpen, setSettingsOpen] = useState(0);
  const [notice, setNotice] = useState<{ kind: 'error' | 'info'; text: string } | null>(null);
  const [bootTick, setBootTick] = useState(0);
  const [reloadTick, setReloadTick] = useState(0);
  const [imgStatus, setImgStatus] = useState<ImgPromptStatus | null>(null);
  const [detailTab, setDetailTab] = useState<DetailTab>('lore');
  const [renameReq, setRenameReq] = useState<string | null>(null);
  const [undoAvail, setUndoAvail] = useState(false);
  const abortRef = useRef(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        await spriteLoreApi.health();
        const s = await spriteLoreApi.getSettings();
        const f = await spriteLoreApi.folders();
        const ip = await spriteLoreApi.imgPromptStatus().catch(
          (e): ImgPromptStatus => ({ dir: null, found: false, templates: [], output: null, error: errText(e) }),
        );
        if (!alive) return;
        setImgStatus(ip);
        setSettings(s.settings);
        setDefaults(s.defaults);
        setHasPrev(s.hasPrev);
        setFolders(f.folders);
        setServer('ok');
      } catch (e) {
        if (alive) setServer(errText(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, [bootTick]);

  useEffect(() => {
    if (!folderPath || server !== 'ok') return;
    let alive = true;
    spriteLoreApi
      .folder(folderPath)
      .then((d) => {
        if (!alive) return;
        setData(d);
        setFolderError(null);
        spriteLoreApi
          .undoAvailable(d.folder)
          .then((u) => alive && setUndoAvail(u.available))
          .catch(() => undefined);
      })
      .catch((e) => {
        if (alive) setFolderError(errText(e));
      });
    return () => {
      alive = false;
    };
  }, [folderPath, server, reloadTick]);

  const reload = useCallback(() => setReloadTick((t) => t + 1), []);
  const loading = !!folderPath && data?.folder !== folderPath && !folderError;

  const openFolder = (path: string) => {
    if (path === folderPath) return;
    setFolderPath(path);
    setFolderError(null);
    setFocusPath(null);
    setSelected([]);
    setPending({});
    writeLocal(LAST_FOLDER_KEY, path);
  };

  const images = useMemo(() => (data && data.folder === folderPath ? data.images : []), [data, folderPath]);
  const groups = useMemo(() => {
    const m = new Map<string, SpriteImage[]>();
    images.forEach((im) => {
      const arr = m.get(im.label) ?? [];
      arr.push(im);
      m.set(im.label, arr);
    });
    m.forEach((arr) => arr.sort((a, b) => a.plan.rank - b.plan.rank));
    return Array.from(m.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  }, [images]);
  const focus = images.find((i) => i.path === focusPath) ?? null;

  const vlmId = settings?.activeVlm ?? '';
  const llmId = settings?.activeLlm ?? '';
  const folderData = data && data.folder === folderPath ? data : null;
  const eff: Settings | null = settings ? (folderData ? { ...settings, ...folderData.effective } : settings) : null;
  const profileName = folderData?.profile ?? '';
  const templateId = eff?.activeTemplate ?? '';
  const imgTemplate = settings?.imgprompt?.activeTemplate || imgStatus?.templates[0]?.name || '';
  const imgBackend = settings?.imgprompt?.backend || vlmId;
  const imgReady = !!imgStatus?.found && !!imgStatus.templates.length;

  const persistSettings = async (next: Settings) => {
    setSettings(next);
    try {
      const r = await spriteLoreApi.saveSettings(next);
      setSettings(r.settings);
      setHasPrev(true);
    } catch (e) {
      setNotice({ kind: 'error', text: errText(e) });
    }
  };

  const updateInstr = async (patch: Partial<ProfileData>) => {
    if (!settings) return;
    if (profileName && folderData) {
      const next = { ...folderData.effective, ...patch };
      setData({ ...folderData, effective: next });
      try {
        await spriteLoreApi.saveProfile(profileName, next);
      } catch (e) {
        setNotice({ kind: 'error', text: errText(e) });
      }
      reload();
    } else {
      await persistSettings({ ...settings, ...patch });
      reload();
    }
  };

  const setBusyFor = (path: string, b: Busy | null) =>
    setBusy((cur) => {
      const out = { ...cur };
      if (b) out[path] = b;
      else delete out[path];
      return out;
    });

  const pendingTexts = (extra?: Record<string, LoreData>) => {
    const out: Record<string, string> = {};
    Object.entries({ ...pending, ...(extra ?? {}) }).forEach(([p, l]) => {
      out[p] = l.text;
    });
    return out;
  };

  const generateOne = async (path: string, extra?: Record<string, LoreData>): Promise<LoreData | null> => {
    setBusyFor(path, 'gen');
    try {
      const r = await spriteLoreApi.generate({
        path,
        backendId: llmId,
        templateId,
        pending: pendingTexts(extra),
        write: autoAccept,
      });
      if (r.written) {
        setPending((p) => {
          const o = { ...p };
          delete o[path];
          return o;
        });
      } else {
        setPending((p) => ({ ...p, [path]: r.lore }));
      }
      return r.lore;
    } finally {
      setBusyFor(path, null);
    }
  };

  const tagOne = async (path: string) => {
    setBusyFor(path, 'tag');
    try {
      return await spriteLoreApi.tag(path, vlmId);
    } finally {
      setBusyFor(path, null);
    }
  };

  const imgOne = async (path: string, force: boolean) => {
    setBusyFor(path, 'img');
    try {
      return await spriteLoreApi.runImgPrompt(path, imgTemplate, imgBackend, force);
    } finally {
      setBusyFor(path, null);
    }
  };

  const runBatch = async (label: string, paths: string[], fn: (p: string, acc: Record<string, LoreData>) => Promise<unknown>) => {
    if (!paths.length) {
      setNotice({ kind: 'info', text: `${label}: nothing to do` });
      return;
    }
    abortRef.current = false;
    const acc: Record<string, LoreData> = {};
    let failed = 0;
    let lastErr = '';
    setBatch({ label, done: 0, total: paths.length, failed: 0 });
    for (let i = 0; i < paths.length; i++) {
      if (abortRef.current) break;
      try {
        const out = await fn(paths[i], acc);
        if (out && typeof out === 'object' && 'text' in (out as LoreData)) acc[paths[i]] = out as LoreData;
      } catch (e) {
        failed++;
        lastErr = `${paths[i].split('/').pop()}: ${errText(e)}`;
        if (/not reachable|cannot reach|Lost KoboldCpp|Connection refused/i.test(lastErr)) {
          abortRef.current = true;
        }
      }
      setBatch({ label, done: i + 1, total: paths.length, failed });
    }
    setBatch(null);
    reload();
    if (failed) setNotice({ kind: 'error', text: `${label}: ${failed} failed. Last error: ${lastErr}` });
    else setNotice({ kind: 'info', text: `${label}: done${abortRef.current ? ' (stopped)' : ''}` });
  };

  const targets = (pred: (im: SpriteImage) => boolean, allowNonPng = false) => {
    const pool = selected.length ? images.filter((i) => selected.includes(i.path)) : images;
    return pool
      .filter((i) => (i.isPng || (allowNonPng && i.ext !== '.gif')) && pred(i))
      .map((i) => i.path);
  };

  const suggested = (selected.length ? images.filter((i) => selected.includes(i.path)) : images).filter(
    (i) => !!i.suggest,
  );
  const nonPng = images.filter((i) => !i.isPng);
  const convertible = nonPng.filter((i) => i.ext !== '.gif');

  const convertAll = async (includeGif: boolean) => {
    if (!data) return;
    setBatch({ label: 'Converting to PNG', done: 0, total: includeGif ? nonPng.length : convertible.length, failed: 0 });
    try {
      const r = await spriteLoreApi.convertFolder(data.folder, includeGif);
      const renamed = r.converted.filter((c) => c.to.replace(/\.png$/, '') !== c.from.replace(/\.[^.]+$/, ''));
      let text = `Converted ${r.converted.length} to PNG; originals moved to .spritelore_originals`;
      if (renamed.length) text += `. Name taken, saved as: ${renamed.map((c) => `${c.from} → ${c.to}`).join(', ')}`;
      if (r.skipped.length) text += `. Skipped ${r.skipped.length} gif (animation would be lost)`;
      if (r.errors.length) text += `. Failed: ${r.errors.join('; ')}`;
      setNotice({ kind: r.errors.length ? 'error' : 'info', text });
      if (focusPath && !focusPath.toLowerCase().endsWith('.png')) setFocusPath(null);
      setSelected([]);
    } catch (e) {
      setNotice({ kind: 'error', text: errText(e) });
    } finally {
      setBatch(null);
      reload();
    }
  };

  const acceptPending = async (path: string) => {
    const lore = pending[path];
    if (!lore) return;
    setBusyFor(path, 'save');
    try {
      await spriteLoreApi.writeLore(path, lore);
      setPending((p) => {
        const o = { ...p };
        delete o[path];
        return o;
      });
      reload();
    } catch (e) {
      setNotice({ kind: 'error', text: errText(e) });
    } finally {
      setBusyFor(path, null);
    }
  };

  const acceptAll = () => runBatch('Accept pending', Object.keys(pending), (p) => acceptPendingQuiet(p));

  const acceptPendingQuiet = async (path: string) => {
    const lore = pending[path];
    if (!lore) return null;
    await spriteLoreApi.writeLore(path, lore);
    setPending((p) => {
      const o = { ...p };
      delete o[path];
      return o;
    });
    return null;
  };

  const saveGuide = async (fields: Partial<Guide>) => {
    if (!data) return false;
    setData({ ...data, guide: { ...data.guide, ...fields } });
    try {
      await spriteLoreApi.saveGuide(data.folder, fields);
      if ('profile' in fields) reload();
      return true;
    } catch (e) {
      setNotice({ kind: 'error', text: errText(e) });
      return false;
    }
  };

  const addCustomGuideTag = (tag: string) => {
    if (!eff || !data) return;
    const known = eff.tagGroups.some((g) => g.tags.includes(tag));
    if (!known) {
      const groups2 = [...eff.tagGroups];
      let idx = groups2.findIndex((g) => g.name.toLowerCase() === 'custom');
      if (idx < 0) {
        groups2.push({ name: 'Custom', tags: [] });
        idx = groups2.length - 1;
      }
      groups2[idx] = { ...groups2[idx], tags: [...groups2[idx].tags, tag] };
      updateInstr({ tagGroups: groups2 });
    }
    saveGuide({ tags: addUnique(data.guide.tags, tag) });
  };

  const editEmotionTags = (label: string, tags: string[]) => {
    if (!eff) return;
    const base = eff.emotionRules[label] ?? {
      rule: eff.emotionRules.default?.rule ?? '',
      tags: [],
      directions: [],
    };
    updateInstr({ emotionRules: { ...eff.emotionRules, [label]: { ...base, tags } } });
  };

  const renameSuggested = async (paths?: string[], quiet = false) => {
    if (!data) return;
    try {
      const r = await spriteLoreApi.renameSuggested(data.folder, paths);
      if (r.moves.length) {
        r.moves.forEach((m) => movePath(`${data.folder}/${m.from}`, `${data.folder}/${m.to}`));
        setUndoAvail(true);
        setNotice({
          kind: 'info',
          text: `Renamed ${r.moves.length} to the tagger's names: ${r.moves.map((m) => `${m.from} → ${m.to}`).join(', ')}`,
        });
      } else if (!quiet) {
        setNotice({ kind: 'info', text: 'No suggested names to apply' });
      }
    } catch (e) {
      setNotice({ kind: 'error', text: errText(e) });
    } finally {
      reload();
    }
  };

  const keepNames = async (paths?: string[], clear = false) => {
    if (!data) return;
    try {
      const r = await spriteLoreApi.keepNames(data.folder, paths, clear);
      if (r.changed.length) {
        setNotice({
          kind: 'info',
          text: clear
            ? `Showing the tagger's suggestion again for ${r.changed.join(', ')}`
            : `Kept ${r.changed.length} name${r.changed.length > 1 ? 's' : ''}; the tagger's suggestion is dismissed until the file is renamed or re-tagged differently`,
        });
      }
    } catch (e) {
      setNotice({ kind: 'error', text: errText(e) });
    } finally {
      reload();
    }
  };

  const undoRenames = async () => {
    if (!data) return;
    try {
      const r = await spriteLoreApi.undoRenames(data.folder);
      setFocusPath(null);
      setPending({});
      setSelected([]);
      setNotice({
        kind: r.skipped.length ? 'error' : 'info',
        text:
          `Undid ${r.restored.length} rename(s)` + (r.skipped.length ? `; not restored: ${r.skipped.join(', ')}` : ''),
      });
      const u = await spriteLoreApi.undoAvailable(data.folder);
      setUndoAvail(u.available);
    } catch (e) {
      setNotice({ kind: 'error', text: errText(e) });
    } finally {
      reload();
    }
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'F2' || !focusPath) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return;
      e.preventDefault();
      setRenameReq(focusPath);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [focusPath]);

  const movePath = (from: string, to: string) => {
    if (from === to) return;
    setPending((p) => {
      if (!p[from]) return p;
      const o = { ...p, [to]: p[from] };
      delete o[from];
      return o;
    });
    setSelected((sel) => sel.map((x) => (x === from ? to : x)));
    setFocusPath((f) => (f === from ? to : f));
  };

  const filteredFolders = folders.filter((f) => f.name.toLowerCase().includes(folderFilter.toLowerCase()));
  const pendingCount = Object.keys(pending).length;
  const running = !!batch;

  if (server !== 'ok' || !settings || !defaults) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-4 bg-bg p-8 text-fg">
        {server === 'checking' ? (
          <Loader2 className="h-8 w-8 animate-spin text-accent" />
        ) : (
          <>
            <AlertTriangle className="h-10 w-10 text-warning" />
            <div className="max-w-lg text-center space-y-2">
              <p className="font-semibold">Sprite Lore server is not running</p>
              <p className="text-sm text-fg-muted">{server}</p>
              <p className="text-xs text-fg-subtle">
                It runs as the user service <code>spritelore</code> and should start on login. Expected at{' '}
                <code>{getServerUrl()}</code>.
              </p>
            </div>
            <div className="flex gap-2">
              {!STANDALONE && (
                <a href="#/" className={btnCls}>
                  <ArrowLeft className="w-3.5 h-3.5" /> Back to vault
                </a>
              )}
              <button type="button" className={primaryCls} onClick={() => setBootTick((t) => t + 1)}>
                <RefreshCw className="w-3.5 h-3.5" /> Retry
              </button>
            </div>
          </>
        )}
      </div>
    );
  }

  const E = eff ?? settings;
  const ladderName = (id: string) => E.intensity.ladder.find((l) => l.id === id)?.name ?? id;
  const dirName = (id: string) => E.directions.find((d) => d.id === id)?.name ?? id;
  const forRole = (role: 'vlm' | 'llm') => {
    const active = role === 'vlm' ? vlmId : llmId;
    const list = settings.backends.filter((b) => b.role === role || b.role === 'both' || b.id === active);
    return list.length ? list : settings.backends;
  };

  return (
    <div className="flex h-screen flex-col bg-bg text-fg">
      <header className="flex flex-wrap items-center gap-2 border-b border-border bg-surface px-4 py-2">
        {!STANDALONE && (
          <a href="#/" className="p-1.5 text-fg-muted hover:text-accent" title="Back to vault">
            <ArrowLeft className="w-4 h-4" />
          </a>
        )}
        <BookOpen className="w-4 h-4 text-accent" />
        <h1 className="text-sm font-semibold">Sprite Lore</h1>
        <div className="flex-1" />
        <label className="flex items-center gap-1 text-[11px] text-fg-subtle">
          Vision
          <select
            className={selectCls}
            value={vlmId}
            onChange={(e) => persistSettings({ ...settings, activeVlm: e.target.value })}
          >
            {forRole('vlm').map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1 text-[11px] text-fg-subtle">
          Lore
          <select
            className={selectCls}
            value={llmId}
            onChange={(e) => persistSettings({ ...settings, activeLlm: e.target.value })}
          >
            {forRole('llm').map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1 text-[11px] text-fg-subtle">
          Template
          <select
            className={selectCls}
            value={templateId}
            onChange={(e) => updateInstr({ activeTemplate: e.target.value })}
            title={profileName ? `Templates of profile "${profileName}"` : 'Default templates'}
          >
            {E.templates.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </label>
        <button type="button" className={btnCls} onClick={() => setSettingsOpen((n) => n + 1)}>
          <Settings2 className="w-3.5 h-3.5" /> Settings
        </button>
      </header>

      {notice && (
        <div
          className={`flex items-center gap-2 px-4 py-1.5 text-xs ${
            notice.kind === 'error' ? 'bg-danger-soft text-danger-soft-fg' : 'bg-info-soft text-info-soft-fg'
          }`}
        >
          <span className="flex-1 break-all">{notice.text}</span>
          <button type="button" onClick={() => setNotice(null)} aria-label="Dismiss">
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <aside className="flex w-60 shrink-0 flex-col border-r border-border bg-surface">
          <div className="flex items-center gap-1 p-2">
            <div className="relative flex-1">
              <Search className="pointer-events-none absolute left-2 top-2 w-3.5 h-3.5 text-fg-subtle" />
              <input
                className={`${inputCls} pl-7 text-xs`}
                placeholder="Filter characters"
                value={folderFilter}
                onChange={(e) => setFolderFilter(e.target.value)}
              />
            </div>
            <button
              type="button"
              className="p-1.5 text-fg-subtle hover:text-fg"
              title="Rescan folders"
              onClick={() => setBootTick((t) => t + 1)}
            >
              <RefreshCw className="w-3.5 h-3.5" />
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-1 pb-2">
            {filteredFolders.map((f) => (
              <button
                key={f.path}
                type="button"
                onClick={() => openFolder(f.path)}
                className={`flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm ${
                  f.path === folderPath ? 'bg-accent-soft text-accent' : 'text-fg-muted hover:bg-hover hover:text-fg'
                }`}
              >
                <FolderOpen className="w-3.5 h-3.5 shrink-0" />
                <span className="flex-1 truncate">{f.name}</span>
                {!f.card && <span title="no character card found" className="text-warning">•</span>}
                <span className="text-[10px] text-fg-subtle">{f.images}</span>
              </button>
            ))}
            {!filteredFolders.length && <p className="p-3 text-xs text-fg-subtle">No sprite folders found.</p>}
          </div>
        </aside>

        <main className="min-w-0 flex-1 overflow-y-auto">
          {!folderPath && (
            <div className="flex h-full items-center justify-center text-sm text-fg-subtle">Pick a character folder.</div>
          )}
          {folderError && <div className="m-4 rounded-lg bg-danger-soft p-3 text-sm text-danger-soft-fg">{folderError}</div>}
          {loading && (
            <div className="flex h-full items-center justify-center">
              <Loader2 className="h-6 w-6 animate-spin text-accent" />
            </div>
          )}
          {data && data.folder === folderPath && (
            <div className="space-y-4 p-4">
              <CharacterGuide
                key={data.folder}
                data={data}
                settings={E}
                onSave={saveGuide}
                onAddCustom={addCustomGuideTag}
                onEditProfile={() => setSettingsOpen((n) => n + 1)}
              />

              <div className="sticky top-0 z-10 -mx-4 flex flex-wrap items-center gap-2 border-y border-border bg-bg/95 px-4 py-2 backdrop-blur">
                <button
                  type="button"
                  className={btnCls}
                  onClick={() =>
                    setSelected(selected.length === images.length ? [] : images.map((i) => i.path))
                  }
                >
                  {selected.length === images.length && images.length ? (
                    <CheckSquare className="w-3.5 h-3.5" />
                  ) : (
                    <Square className="w-3.5 h-3.5" />
                  )}
                  {selected.length ? `${selected.length} selected` : 'Select all'}
                </button>
                <span className="text-[11px] text-fg-subtle">
                  {selected.length ? 'Actions apply to the selection' : 'Actions apply to the whole folder'}
                </span>
                <div className="flex-1" />
                {suggested.length > 0 && (
                  <button
                    type="button"
                    className={primaryCls}
                    disabled={running}
                    onClick={() => renameSuggested(selected.length ? suggested.map((i) => i.path) : undefined)}
                    title={suggested.map((i) => `${i.name} → ${i.suggest}`).join('\n')}
                  >
                    <Check className="w-3.5 h-3.5" /> Accept {suggested.length} suggested name{suggested.length > 1 ? 's' : ''}
                  </button>
                )}
                {suggested.length > 0 && (
                  <button
                    type="button"
                    className={btnCls}
                    disabled={running}
                    onClick={() => keepNames(suggested.map((i) => i.path))}
                    title={`Reject the tagger's names and keep these:\n${suggested.map((i) => i.name).join('\n')}`}
                  >
                    <X className="w-3.5 h-3.5" /> Keep current name{suggested.length > 1 ? 's' : ''}
                  </button>
                )}
                {undoAvail && (
                  <button type="button" className={btnCls} disabled={running} onClick={undoRenames} title="Put back the last batch of renames">
                    <RotateCcw className="w-3.5 h-3.5" /> Undo renames
                  </button>
                )}
                {nonPng.length > 0 && (
                  <button
                    type="button"
                    className={primaryCls}
                    disabled={running}
                    onClick={() => convertAll(convertible.length === 0)}
                    title="Originals are moved to .spritelore_originals in this folder"
                  >
                    <RefreshCw className="w-3.5 h-3.5" />
                    {convertible.length
                      ? `Convert ${convertible.length} to PNG`
                      : `Convert ${nonPng.length} gif to PNG (first frame)`}
                  </button>
                )}
                <button
                  type="button"
                  className={btnCls}
                  disabled={running}
                  onClick={async () => {
                    const tagged: string[] = [];
                    await runBatch('Tag untagged', targets((i) => !i.tagged, true), async (p) => {
                      const r = await tagOne(p);
                      tagged.push(r.path || p);
                      return r;
                    });
                    if (autoAccept && tagged.length) await renameSuggested(tagged, true);
                  }}
                  title="Non-PNG sprites are converted to PNG first"
                >
                  <Tags className="w-3.5 h-3.5" /> VLM tag untagged
                </button>
                <span
                  className="inline-flex items-center gap-1"
                  title={imgReady ? `Photoreal prompts via anime2img, written to ${imgStatus?.output ?? ''}` : imgStatus?.error ?? ''}
                >
                  <select
                    className={selectCls}
                    value={imgTemplate}
                    disabled={!imgReady}
                    onChange={(e) =>
                      persistSettings({ ...settings, imgprompt: { ...settings.imgprompt, activeTemplate: e.target.value } })
                    }
                  >
                    {(imgStatus?.templates ?? []).map((t) => (
                      <option key={t.name} value={t.name}>
                        {t.name}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className={btnCls}
                    disabled={running || !imgReady}
                    onClick={() =>
                      runBatch(
                        `Image prompts (${imgTemplate})`,
                        targets((i) => (selected.length ? true : !i.meta?.imgprompts?.[imgTemplate])),
                        (p) => imgOne(p, selected.length > 0),
                      )
                    }
                  >
                    <ImagePlus className="w-3.5 h-3.5" /> {selected.length ? 'Image prompts (redo)' : 'Image prompts missing'}
                  </button>
                </span>
                <button
                  type="button"
                  className={btnCls}
                  disabled={running}
                  onClick={() =>
                    runBatch(
                      'Generate missing',
                      targets((i) => (!i.hasLore || i.stale) && !pending[i.path]),
                      (p, acc) => generateOne(p, acc),
                    )
                  }
                >
                  <Sparkles className="w-3.5 h-3.5" /> Generate missing
                </button>
                <button
                  type="button"
                  className={btnCls}
                  disabled={running || !selected.length}
                  onClick={() => runBatch('Regenerate', targets(() => true), (p, acc) => generateOne(p, acc))}
                >
                  <Wand2 className="w-3.5 h-3.5" /> Regenerate selected
                </button>
                <label
                  className="flex items-center gap-1.5 text-xs text-fg-muted"
                  title="Write generated lore straight into the PNG, and rename sprites to the tagger's emotion after tagging"
                >
                  <input
                    type="checkbox"
                    checked={autoAccept}
                    onChange={(e) => {
                      setAutoAccept(e.target.checked);
                      writeLocal(AUTO_KEY, e.target.checked ? '1' : '0');
                    }}
                  />
                  Auto-accept lore + names
                </label>
                {pendingCount > 0 && (
                  <button type="button" className={primaryCls} disabled={running} onClick={acceptAll}>
                    <Check className="w-3.5 h-3.5" /> Accept {pendingCount} pending
                  </button>
                )}
                {batch && (
                  <span className="flex items-center gap-2 text-xs text-fg-muted">
                    <Loader2 className="w-3.5 h-3.5 animate-spin text-accent" />
                    {batch.label} {batch.done}/{batch.total}
                    {batch.failed > 0 && <span className="text-danger">({batch.failed} failed)</span>}
                    <button
                      type="button"
                      className="text-danger hover:opacity-80"
                      onClick={() => {
                        abortRef.current = true;
                      }}
                      title="Stop after the current image"
                    >
                      <StopCircle className="w-4 h-4" />
                    </button>
                  </span>
                )}
              </div>

              {groups.map(([label, items]) => {
                const rule = E.emotionRules[label];
                return (
                  <section key={label} className="space-y-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="text-sm font-semibold capitalize">{label}</h3>
                      <span className="text-[11px] text-fg-subtle">
                        {items.length} sprite{items.length > 1 ? 's' : ''}
                        {items.length > 1 && ` · ${items.map((i) => ladderName(i.plan.intensity)).join(' → ')}`}
                      </span>
                      {!data.labels.includes(label) && (
                        <span className="rounded bg-warning-soft px-1.5 text-[10px] text-warning-soft-fg">
                          not an ST label
                        </span>
                      )}
                      <span className="text-[11px] text-fg-subtle">· tags:</span>
                      <TagChips
                        size="xs"
                        tags={rule?.tags ?? []}
                        onRemove={(t) => editEmotionTags(label, (rule?.tags ?? []).filter((x) => x !== t))}
                        onAdd={(t) => editEmotionTags(label, addUnique(rule?.tags ?? [], t))}
                        addPlaceholder={`${label} tag`}
                      />
                    </div>
                    <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-2">
                      {items.map((im) => (
                        <SpriteCard
                          key={im.path}
                          im={im}
                          focused={im.path === focusPath}
                          selected={selected.includes(im.path)}
                          busy={busy[im.path]}
                          pending={!!pending[im.path]}
                          hasPrompt={!!im.meta?.imgprompts?.[imgTemplate]}
                          intensityName={ladderName(im.plan.intensity)}
                          directionName={dirName(im.plan.direction)}
                          onFocus={() => setFocusPath(im.path)}
                          onRename={() => {
                            setFocusPath(im.path);
                            setRenameReq(im.path);
                          }}
                          onSelect={() => setSelected((s) => toggleIn(s, im.path))}
                          onKeepName={(clear) => keepNames([im.path], clear)}
                        />
                      ))}
                    </div>
                  </section>
                );
              })}
              {!images.length && <p className="text-sm text-fg-subtle">No images in this folder.</p>}
            </div>
          )}
        </main>

        {focus && data && (
          <DetailPanel
            key={`${focus.path}:${focus.mtime}`}
            im={focus}
            data={data}
            settings={E}
            pending={pending[focus.path] ?? null}
            busy={busy[focus.path]}
            onClose={() => setFocusPath(null)}
            onGenerate={async () => {
              try {
                await generateOne(focus.path);
                if (autoAccept) reload();
              } catch (e) {
                setNotice({ kind: 'error', text: errText(e) });
              }
            }}
            onTag={async () => {
              try {
                const r = await tagOne(focus.path);
                if (r.path && r.path !== focus.path) setFocusPath(r.path);
                if (r.mismatch && autoAccept) {
                  await renameSuggested([r.path || focus.path], true);
                  return;
                }
                if (r.mismatch)
                  setNotice({
                    kind: 'info',
                    text: `${focus.name}: the vision model reads this as "${r.vlm.emotion}" (file says "${focus.label}")`,
                  });
                reload();
              } catch (e) {
                setNotice({ kind: 'error', text: errText(e) });
              }
            }}
            onAccept={() => acceptPending(focus.path)}
            onDiscard={() =>
              setPending((p) => {
                const o = { ...p };
                delete o[focus.path];
                return o;
              })
            }
            onEditPending={(lore) => setPending((p) => ({ ...p, [focus.path]: lore }))}
            onChanged={(newPath) => {
              if (newPath && newPath !== focus.path) movePath(focus.path, newPath);
              reload();
            }}
            startRename={renameReq === focus.path}
            onRenameShown={() => setRenameReq(null)}
            onError={(t) => setNotice({ kind: 'error', text: t })}
            pendingTexts={pendingTexts}
            imgStatus={imgStatus}
            imgTemplate={imgTemplate}
            tab={detailTab}
            setTab={setDetailTab}
            onImgTemplate={(name) =>
              persistSettings({ ...settings, imgprompt: { ...settings.imgprompt, activeTemplate: name } })
            }
            onImgPrompt={async (force) => {
              try {
                const r = await imgOne(focus.path, force);
                if (r.cached) setNotice({ kind: 'info', text: `${focus.name}: prompt already current for "${imgTemplate}" (use Re-run to force)` });
                reload();
              } catch (e) {
                setNotice({ kind: 'error', text: errText(e) });
              }
            }}
          />
        )}
      </div>

      {settingsOpen > 0 && (
        <SpriteLoreSettings
          key={settingsOpen}
          settings={settings}
          defaults={defaults}
          hasPrev={hasPrev}
          labels={data?.labels ?? []}
          imgStatus={imgStatus}
          onImgStatus={setImgStatus}
          initialProfile={profileName}
          onClose={() => setSettingsOpen(0)}
          onSaved={(s) => {
            setSettings(s);
            setHasPrev(true);
            setBootTick((t) => t + 1);
            reload();
          }}
        />
      )}
    </div>
  );
};

const CharacterGuide: React.FC<{
  data: FolderData;
  settings: Settings;
  onSave: (fields: Partial<Guide>) => Promise<boolean>;
  onAddCustom: (tag: string) => void;
  onEditProfile: () => void;
}> = ({ data, settings, onSave, onAddCustom, onEditProfile }) => {
  const [notes, setNotes] = useState(data.guide.notes);
  const [instruction, setInstruction] = useState(data.guide.instruction);
  const [saved, setSaved] = useState(false);
  const chosen = data.guide.tags;
  const [open, setOpen] = useState(chosen.length === 0 && !data.guide.notes && !data.guide.instruction);
  const known = new Set(settings.tagGroups.flatMap((g) => g.tags));
  const orphan = chosen.filter((t) => !known.has(t));
  const textDirty = notes !== data.guide.notes || instruction !== data.guide.instruction;

  const saveText = async () => {
    if (await onSave({ notes, instruction })) {
      setSaved(true);
      window.setTimeout(() => setSaved(false), 1500);
    }
  };

  return (
    <div className="rounded-xl border border-border bg-surface p-3 space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">{data.card?.name || data.name}</h2>
        {data.card ? (
          <span className="text-[11px] text-fg-subtle truncate" title={data.card.path}>
            card: {data.card.path.split('/').pop()}
          </span>
        ) : (
          <span className="rounded bg-warning-soft px-1.5 text-[11px] text-warning-soft-fg">
            no character card next to this folder; lore will rely on the images only
          </span>
        )}
        <div className="flex-1" />
        <label className="flex items-center gap-1 text-[11px] text-fg-subtle" title="Which saved settings profile this character uses">
          Profile
          <select
            className={selectCls}
            value={data.profile}
            onChange={(e) => onSave({ profile: e.target.value })}
          >
            <option value="">Default</option>
            {data.profiles.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        <button type="button" className="text-[11px] text-accent hover:underline" onClick={onEditProfile}>
          edit profiles
        </button>
        <span className="text-[11px] text-fg-subtle">
          · Focus: {chosen.length ? chosen.join(', ') : 'none'}
          {data.guide.instruction ? ' · custom instruction set' : ''}
        </span>
        <button type="button" className="text-[11px] text-accent hover:underline" onClick={() => setOpen(!open)}>
          {open ? 'hide' : 'edit'}
        </button>
      </div>
      {open && (
        <>
          <p className="text-[11px] text-fg-subtle">
            Pick tags to steer every lore entry for this character. Tags, notes and the custom instruction are saved per
            character and used in all future generations.
          </p>
          <div className="space-y-1.5">
            {settings.tagGroups.map((g) =>
              g.tags.length ? (
                <div key={g.name} className="flex gap-2">
                  <span className="w-28 shrink-0 pt-0.5 text-[11px] text-fg-subtle">{g.name}</span>
                  <TagChips
                    size="xs"
                    tags={g.tags}
                    selected={chosen}
                    onToggle={(t) => onSave({ tags: toggleIn(chosen, t) })}
                  />
                </div>
              ) : null,
            )}
            <div className="flex gap-2">
              <span className="w-28 shrink-0 pt-0.5 text-[11px] text-fg-subtle">Your tags</span>
              <TagChips
                size="xs"
                tags={orphan}
                selected={orphan}
                onToggle={(t) => onSave({ tags: toggleIn(chosen, t) })}
                onAdd={onAddCustom}
                addPlaceholder="new tag"
              />
            </div>
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            <label className="block">
              <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-fg-subtle">
                Custom instruction for this character
              </span>
              <textarea
                className={`${inputCls} h-24 text-xs`}
                placeholder="How lore for this character should be written, e.g. 'She never apologizes, she bargains. Reactions are physical first, words second. Always end on a question to {{user}}.'"
                value={instruction}
                onChange={(e) => setInstruction(e.target.value)}
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-fg-subtle">
                Character notes
              </span>
              <textarea
                className={`${inputCls} h-24 text-xs`}
                placeholder="Facts to keep, things to avoid, relationships to lean on ..."
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
              />
            </label>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" className={primaryCls} disabled={!textDirty} onClick={saveText}>
              <Save className="w-3.5 h-3.5" /> Save for this character
            </button>
            {saved && <span className="text-xs text-success">saved</span>}
            {textDirty && <span className="text-xs text-warning">unsaved changes</span>}
          </div>
        </>
      )}
    </div>
  );
};

const SpriteCard: React.FC<{
  im: SpriteImage;
  focused: boolean;
  selected: boolean;
  busy?: Busy;
  pending: boolean;
  hasPrompt: boolean;
  intensityName: string;
  directionName: string;
  onFocus: () => void;
  onRename: () => void;
  onSelect: () => void;
  onKeepName: (clear: boolean) => void;
}> = ({ im, focused, selected, busy, pending, hasPrompt, intensityName, directionName, onFocus, onRename, onSelect, onKeepName }) => {
  const vlm = im.meta?.vlm;
  return (
    <div
      className={`group relative cursor-pointer overflow-hidden rounded-xl border bg-surface transition-colors ${
        focused ? 'border-accent ring-2 ring-accent/40' : 'border-border hover:border-accent/50'
      }`}
      onClick={onFocus}
    >
      <button
        type="button"
        className={`absolute left-1.5 top-1.5 z-10 rounded bg-surface/90 p-0.5 ${
          selected ? 'text-accent' : 'text-fg-subtle opacity-0 group-hover:opacity-100'
        }`}
        onClick={(e) => {
          e.stopPropagation();
          onSelect();
        }}
        aria-label="select"
      >
        {selected ? <CheckSquare className="w-4 h-4" /> : <Square className="w-4 h-4" />}
      </button>
      {busy && (
        <div className="absolute inset-0 z-10 flex items-center justify-center bg-overlay/40">
          <Loader2 className="h-6 w-6 animate-spin text-accent-fg" />
        </div>
      )}
      <div className="flex h-40 items-end justify-center bg-[repeating-conic-gradient(var(--muted)_0%_25%,transparent_0%_50%)] bg-[length:16px_16px]">
        <img
          src={imageUrl(im.path, 256) + `&t=${im.mtime}`}
          alt={im.name}
          loading="lazy"
          className="max-h-40 max-w-full object-contain"
        />
      </div>
      <div className="space-y-1 p-2">
        <div className="flex items-center gap-1">
          <p className="flex-1 truncate text-xs font-medium" title={im.name}>
            {im.name}
          </p>
          <button
            type="button"
            className={`p-0.5 hover:text-accent ${focused ? 'text-fg-muted' : 'text-fg-subtle opacity-0 group-hover:opacity-100'}`}
            onClick={(e) => {
              e.stopPropagation();
              onRename();
            }}
            aria-label="rename"
            title="Rename (F2)"
          >
            <Pencil className="w-3 h-3" />
          </button>
        </div>
        <div className="flex flex-wrap gap-1 text-[10px]">
          <span className="rounded bg-accent-soft px-1 text-accent">{intensityName}</span>
          <span className="rounded bg-muted px-1 text-fg-muted">→ {directionName}</span>
          {!im.isPng && <span className="rounded bg-warning-soft px-1 text-warning-soft-fg">{im.ext}</span>}
          {im.isPng && !im.tagged && <span className="rounded bg-muted px-1 text-fg-subtle">untagged</span>}
          {im.suggest ? (
            <span className="inline-flex items-center gap-0.5 rounded bg-warning-soft px-1 text-warning-soft-fg" title="the tagger suggests renaming this sprite">
              rename → {im.suggest}
              <button
                type="button"
                className="rounded px-0.5 hover:bg-warning/20"
                title="Keep the current name (stop suggesting this)"
                aria-label="keep current name"
                onClick={(e) => {
                  e.stopPropagation();
                  onKeepName(false);
                }}
              >
                <X className="w-2.5 h-2.5" />
              </button>
            </span>
          ) : im.kept ? (
            <button
              type="button"
              className="rounded bg-muted px-1 text-fg-subtle hover:text-fg"
              title={`You kept this name; the tagger said ${vlm?.emotion ?? '?'}. Click to show the suggestion again.`}
              onClick={(e) => {
                e.stopPropagation();
                onKeepName(true);
              }}
            >
              name kept
            </button>
          ) : (
            vlm &&
            vlm.emotion !== im.label && (
              <span className="rounded bg-muted px-1 text-fg-subtle" title="vision model disagrees, below the rename confidence">
                VLM: {vlm.emotion}?
              </span>
            )
          )}
          {hasPrompt && <span className="rounded bg-muted px-1 text-fg-muted">img prompt</span>}
          {pending ? (
            <span className="rounded bg-info-soft px-1 text-info-soft-fg">pending</span>
          ) : im.hasLore ? (
            im.stale ? (
              <span className="rounded bg-warning-soft px-1 text-warning-soft-fg">stale</span>
            ) : (
              <span className="rounded bg-success-soft px-1 text-success-soft-fg">lore</span>
            )
          ) : null}
        </div>
      </div>
    </div>
  );
};

const DetailPanel: React.FC<{
  im: SpriteImage;
  data: FolderData;
  settings: Settings;
  pending: LoreData | null;
  busy?: Busy;
  onClose: () => void;
  onGenerate: () => void;
  onTag: () => void;
  onAccept: () => void;
  onDiscard: () => void;
  onEditPending: (lore: LoreData) => void;
  onChanged: (newPath?: string) => void;
  onError: (text: string) => void;
  pendingTexts: () => Record<string, string>;
  imgStatus: ImgPromptStatus | null;
  imgTemplate: string;
  onImgTemplate: (name: string) => void;
  onImgPrompt: (force: boolean) => void;
  tab: DetailTab;
  setTab: (t: DetailTab) => void;
  startRename: boolean;
  onRenameShown: () => void;
}> = ({
  im,
  data,
  settings,
  pending,
  busy,
  onClose,
  onGenerate,
  onTag,
  onAccept,
  onDiscard,
  onEditPending,
  onChanged,
  onError,
  pendingTexts,
  imgStatus,
  imgTemplate,
  onImgTemplate,
  onImgPrompt,
  tab,
  setTab,
  startRename,
  onRenameShown,
}) => {
  const meta: SpriteMeta = im.meta ?? {};
  const [text, setText] = useState(meta.lore?.text ?? '');
  const [keywords, setKeywords] = useState((meta.lore?.keywords ?? []).join(', '));
  const [entries, setEntries] = useState<ExtraEntry[]>(meta.entries ?? []);
  const [saving, setSaving] = useState(false);
  const [chunks, setChunks] = useState<TextChunk[] | null>(null);
  const stem = im.name.replace(/\.[^.]+$/, '');
  const [editingName, setEditingName] = useState(startRename);
  const [nameDraft, setNameDraft] = useState(stem);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (startRename) {
      setEditingName(true);
      onRenameShown();
    }
  }, [startRename, onRenameShown]);

  useEffect(() => {
    if (editingName) nameRef.current?.select();
  }, [editingName]);

  const draftName = nameDraft.trim().replace(new RegExp(`${im.ext.replace('.', '\\.')}$`, 'i'), '');
  const draftLabel = stLabel(draftName + im.ext, data.labels);
  const nameClash = data.images.some(
    (o) => o.path !== im.path && o.name.toLowerCase() === (draftName + im.ext).toLowerCase(),
  );
  const nameBad = !draftName || draftName.startsWith('.') || /[\\/]/.test(draftName);

  const commitName = () => {
    if (nameBad || nameClash) return;
    if (draftName === stem) {
      setEditingName(false);
      return;
    }
    run(
      () => spriteLoreApi.renameTo(im.path, draftName),
      (r) => {
        setEditingName(false);
        onChanged((r as { path: string }).path);
      },
    );
  };
  const [rawJson, setRawJson] = useState(JSON.stringify(meta, null, 2));
  const [prompt, setPrompt] = useState<ChatMessage[] | null>(null);
  const [renameTo, setRenameTo] = useState(meta.vlm?.emotion && meta.vlm.emotion !== im.label ? meta.vlm.emotion : im.label);

  const loreDirty =
    text !== (meta.lore?.text ?? '') ||
    keywords !== (meta.lore?.keywords ?? []).join(', ') ||
    JSON.stringify(entries) !== JSON.stringify(meta.entries ?? []);

  const run = async (fn: () => Promise<unknown>, after?: (r: unknown) => void) => {
    setSaving(true);
    try {
      const r = await fn();
      after?.(r);
    } catch (e) {
      onError(errText(e));
    } finally {
      setSaving(false);
    }
  };

  const saveLore = () =>
    run(
      () => {
        const prev = meta.lore;
        const lore: LoreData | undefined = text.trim()
          ? {
              ...(prev ?? {}),
              text: text.trim(),
              keywords: splitList(keywords),
              label: im.label,
              src: prev?.src === 'generated' || prev?.src === 'edited' ? 'edited' : 'manual',
              reviewed: true,
            }
          : undefined;
        const next: SpriteMeta = { ...meta, lore: lore ?? { text: '', keywords: [] }, entries };
        if (!entries.length) delete next.entries;
        return spriteLoreApi.saveMeta(im.path, next, true);
      },
      () => onChanged(),
    );

  const setPlan = (intensity: string, direction: string) =>
    run(() => spriteLoreApi.savePlan(im.path, intensity, direction), () => onChanged());

  useEffect(() => {
    if (tab !== 'raw' || chunks || !im.isPng) return;
    let alive = true;
    spriteLoreApi
      .chunks(im.path)
      .then((r) => alive && setChunks(r.chunks))
      .catch((e) => alive && onError(errText(e)));
    return () => {
      alive = false;
    };
  }, [tab, chunks, im.path, im.isPng, onError]);

  const plan = im.plan;
  const vlm = meta.vlm;
  const lockedI = meta.plan?.intensity ?? '';
  const lockedD = meta.plan?.direction ?? '';

  return (
    <aside className="flex w-[440px] shrink-0 flex-col border-l border-border bg-surface">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        {editingName ? (
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1">
              <input
                ref={nameRef}
                className={`${inputCls} py-1 text-sm ${nameBad || nameClash ? 'border-danger' : ''}`}
                value={nameDraft}
                onChange={(e) => setNameDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') commitName();
                  if (e.key === 'Escape') {
                    setNameDraft(stem);
                    setEditingName(false);
                  }
                }}
                spellCheck={false}
              />
              <span className="text-xs text-fg-subtle">{im.ext}</span>
              <button
                type="button"
                className="p-1 text-success disabled:opacity-40"
                disabled={saving || nameBad || nameClash}
                onClick={commitName}
                aria-label="Save name"
              >
                {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
              </button>
              <button
                type="button"
                className="p-1 text-fg-muted"
                onClick={() => {
                  setNameDraft(stem);
                  setEditingName(false);
                }}
                aria-label="Cancel rename"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <p className={`mt-0.5 text-[11px] ${nameClash || nameBad ? 'text-danger' : 'text-fg-subtle'}`}>
              {nameBad
                ? 'Not a usable file name'
                : nameClash
                  ? `${draftName}${im.ext} already exists in this folder`
                  : data.labels.includes(draftLabel)
                    ? `SillyTavern reads this as "${draftLabel}"`
                    : `"${draftLabel}" is not one of your SillyTavern expressions`}
            </p>
          </div>
        ) : (
          <button
            type="button"
            className="group/name flex min-w-0 flex-1 items-center gap-1.5 text-left"
            title={`${im.path} (click to rename)`}
            onClick={() => {
              setNameDraft(stem);
              setEditingName(true);
            }}
          >
            <span className="truncate text-sm font-semibold">{im.name}</span>
            <Pencil className="w-3.5 h-3.5 shrink-0 text-fg-subtle group-hover/name:text-accent" />
          </button>
        )}
        {!editingName && im.isPng && (
          <>
            <button type="button" className={btnCls} disabled={!!busy} onClick={onTag} title="Tag with the vision model">
              {busy === 'tag' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Tags className="w-3.5 h-3.5" />} Tag
            </button>
            <button
              type="button"
              className={btnCls}
              disabled={!!busy || !imgStatus?.found}
              onClick={() => {
                setTab('image');
                onImgPrompt(false);
              }}
              title={`Write the ${imgTemplate} image prompt`}
            >
              {busy === 'img' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ImagePlus className="w-3.5 h-3.5" />} Img
            </button>
          </>
        )}
        <button type="button" onClick={onClose} className="p-1 text-fg-muted hover:text-fg" aria-label="Close">
          <X className="w-4 h-4" />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="flex h-64 items-center justify-center bg-[repeating-conic-gradient(var(--muted)_0%_25%,transparent_0%_50%)] bg-[length:16px_16px]">
          <img src={imageUrl(im.path, 768) + `&t=${im.mtime}`} alt={im.name} className="max-h-64 max-w-full object-contain" />
        </div>

        {!im.isPng ? (
          <div className="space-y-2 p-3">
            <p className="flex items-center gap-2 text-sm text-warning">
              <ImageOff className="w-4 h-4" /> {im.ext} sprites can't hold embedded lore.
            </p>
            <button
              type="button"
              className={primaryCls}
              disabled={saving}
              onClick={() =>
                run(
                  () => spriteLoreApi.convert(im.path),
                  (r) => onChanged((r as { path: string }).path),
                )
              }
            >
              Convert this one to PNG
            </button>
            <p className="text-[11px] text-fg-subtle">
              Use the Convert button in the toolbar to do the whole folder. Originals are kept in .spritelore_originals.
            </p>
          </div>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-2 border-b border-border p-3">
              <label className="block">
                <span className="mb-1 flex items-center gap-1 text-[11px] font-semibold uppercase tracking-wide text-fg-subtle">
                  Degree {lockedI && <Lock className="w-3 h-3" />}
                </span>
                <select className={inputCls} value={lockedI} onChange={(e) => setPlan(e.target.value, lockedD)}>
                  <option value="">auto ({plan.autoIntensity})</option>
                  {settings.intensity.ladder.map((l) => (
                    <option key={l.id} value={l.id}>
                      {l.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block">
                <span className="mb-1 flex items-center gap-1 text-[11px] font-semibold uppercase tracking-wide text-fg-subtle">
                  Aimed at {lockedD && <Lock className="w-3 h-3" />}
                </span>
                <select className={inputCls} value={lockedD} onChange={(e) => setPlan(lockedI, e.target.value)}>
                  <option value="">auto ({settings.directions.find((d) => d.id === plan.autoDirection)?.name ?? plan.autoDirection})</option>
                  {settings.directions.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name}
                    </option>
                  ))}
                </select>
              </label>
              <p className="col-span-2 text-[11px] text-fg-subtle">
                {plan.groupSize > 1
                  ? `${plan.rank} of ${plan.groupSize} "${im.label}" sprites, mildest first.`
                  : `Only "${im.label}" sprite in this folder.`}
                {meta.lore?.text &&
                  meta.lore.intensity &&
                  (meta.lore.intensity !== plan.intensity || meta.lore.direction !== plan.direction) &&
                  ` Saved lore was written as ${meta.lore.intensity} / ${meta.lore.direction}; regenerate to match.`}
              </p>
            </div>

            <div className="flex border-b border-border px-2">
              {(
                [
                  ['lore', 'Lore'],
                  ['tags', 'Vision tags'],
                  ['image', 'Image prompt'],
                  ['raw', 'Embedded data'],
                  ['prompt', 'Prompt'],
                ] as const
              ).map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => setTab(id)}
                  className={`px-3 py-2 text-xs font-medium border-b-2 -mb-px ${
                    tab === id ? 'border-accent text-accent' : 'border-transparent text-fg-muted hover:text-fg'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>

            {tab === 'lore' && (
              <div className="space-y-3 p-3">
                <div className="flex flex-wrap gap-2">
                  <button type="button" className={primaryCls} disabled={!!busy} onClick={onGenerate}>
                    {busy === 'gen' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Sparkles className="w-3.5 h-3.5" />}
                    {meta.lore?.text || pending ? 'Regenerate' : 'Generate'}
                  </button>
                  {!im.tagged && (
                    <button type="button" className={btnCls} disabled={!!busy} onClick={onTag}>
                      {busy === 'tag' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Tags className="w-3.5 h-3.5" />}
                      Tag first (better lore)
                    </button>
                  )}
                </div>

                {pending && (
                  <div className="space-y-2 rounded-xl border border-info/40 bg-info-soft p-2.5">
                    <p className="text-[11px] font-semibold uppercase tracking-wide text-info-soft-fg">
                      New proposal · {pending.intensity} → {pending.direction}
                    </p>
                    <textarea
                      className={`${inputCls} h-28 text-xs`}
                      value={pending.text}
                      onChange={(e) => onEditPending({ ...pending, text: e.target.value })}
                    />
                    <input
                      className={`${inputCls} text-xs`}
                      value={pending.keywords.join(', ')}
                      onChange={(e) => onEditPending({ ...pending, keywords: splitList(e.target.value) })}
                    />
                    <div className="flex gap-2">
                      <button type="button" className={primaryCls} disabled={!!busy} onClick={onAccept}>
                        <Check className="w-3.5 h-3.5" /> Accept & write
                      </button>
                      <button type="button" className={btnCls} disabled={!!busy} onClick={onGenerate}>
                        <RefreshCw className="w-3.5 h-3.5" /> Try again
                      </button>
                      <button type="button" className={btnCls} onClick={onDiscard}>
                        <X className="w-3.5 h-3.5" /> Discard
                      </button>
                    </div>
                  </div>
                )}

                <div className="space-y-1.5">
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] font-semibold uppercase tracking-wide text-fg-subtle">Saved lore</span>
                    {meta.lore?.src && <span className="text-[10px] text-fg-subtle">{meta.lore.src}</span>}
                    {im.stale && <span className="text-[10px] text-warning">stale (image changed)</span>}
                  </div>
                  <textarea
                    className={`${inputCls} h-28 text-xs`}
                    placeholder="No lore yet. Generate one or write it here."
                    value={text}
                    onChange={(e) => setText(e.target.value)}
                  />
                  <input
                    className={`${inputCls} text-xs`}
                    placeholder={`${settings.lore.keywords} keywords, comma separated`}
                    value={keywords}
                    onChange={(e) => setKeywords(e.target.value)}
                  />
                </div>

                <div className="space-y-1.5">
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] font-semibold uppercase tracking-wide text-fg-subtle">Extra entries</span>
                    <button
                      type="button"
                      className="text-[11px] text-accent hover:underline"
                      onClick={() => setEntries([...entries, { title: '', text: '', keywords: [] }])}
                    >
                      <Plus className="inline w-3 h-3" /> add
                    </button>
                  </div>
                  {entries.map((en, i) => (
                    <div key={i} className="space-y-1 rounded-lg border border-border p-2">
                      <div className="flex gap-1">
                        <input
                          className={`${inputCls} text-xs`}
                          placeholder="title (e.g. dialogue, secret, habit)"
                          value={en.title}
                          onChange={(e) =>
                            setEntries(entries.map((x, j) => (j === i ? { ...x, title: e.target.value } : x)))
                          }
                        />
                        <button
                          type="button"
                          className="p-1 text-fg-subtle hover:text-danger"
                          onClick={() => setEntries(entries.filter((_, j) => j !== i))}
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                      <textarea
                        className={`${inputCls} h-16 text-xs`}
                        value={en.text}
                        onChange={(e) => setEntries(entries.map((x, j) => (j === i ? { ...x, text: e.target.value } : x)))}
                      />
                      <input
                        className={`${inputCls} text-xs`}
                        placeholder="keywords"
                        value={en.keywords.join(', ')}
                        onChange={(e) =>
                          setEntries(entries.map((x, j) => (j === i ? { ...x, keywords: splitList(e.target.value) } : x)))
                        }
                      />
                    </div>
                  ))}
                </div>

                <button type="button" className={primaryCls} disabled={!loreDirty || saving} onClick={saveLore}>
                  {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />} Save to PNG
                </button>
              </div>
            )}

            {tab === 'tags' && (
              <div className="space-y-3 p-3 text-xs">
                <div className="flex flex-wrap gap-2">
                  <button type="button" className={primaryCls} disabled={!!busy} onClick={onTag}>
                    {busy === 'tag' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Eye className="w-3.5 h-3.5" />}
                    {vlm ? 'Re-tag with vision model' : 'Tag with vision model'}
                  </button>
                </div>
                {vlm ? (
                  <div className="space-y-1.5">
                    <p>
                      <span className="text-fg-subtle">emotion</span> <b>{vlm.emotion}</b> ({Math.round(vlm.confidence * 100)}%)
                      {vlm.intensity !== undefined && (
                        <>
                          {' '}
                          · <span className="text-fg-subtle">intensity</span> {vlm.intensity}/5
                        </>
                      )}
                      {vlm.directed_at && (
                        <>
                          {' '}
                          · <span className="text-fg-subtle">looking</span> {vlm.directed_at}
                        </>
                      )}
                    </p>
                    {vlm.description && <p className="italic text-fg-muted">{vlm.description}</p>}
                    {vlm.categories &&
                      Object.entries(vlm.categories).map(([k, v]) =>
                        v.length ? (
                          <p key={k}>
                            <span className="text-fg-subtle">{k}:</span> {v.join(', ')}
                          </p>
                        ) : null,
                      )}
                    {!im.tagged && <p className="text-warning">These tags are from an earlier version of the image.</p>}
                  </div>
                ) : (
                  <p className="text-fg-subtle">Not tagged yet.</p>
                )}
                <div className="space-y-1 border-t border-border pt-3">
                  <span className="text-[11px] font-semibold uppercase tracking-wide text-fg-subtle">Rename sprite to label</span>
                  <div className="flex gap-1">
                    <input
                      className={`${inputCls} text-xs`}
                      list="spritelore-labels"
                      value={renameTo}
                      onChange={(e) => setRenameTo(e.target.value)}
                    />
                    <datalist id="spritelore-labels">
                      {data.labels.map((l) => (
                        <option key={l} value={l} />
                      ))}
                    </datalist>
                    <button
                      type="button"
                      className={btnCls}
                      disabled={saving || !renameTo.trim() || renameTo === im.label}
                      onClick={() =>
                        run(
                          () => spriteLoreApi.rename(im.path, renameTo),
                          (r) => onChanged((r as { path: string }).path),
                        )
                      }
                    >
                      Rename
                    </button>
                  </div>
                  <p className="text-[11px] text-fg-subtle">
                    SillyTavern picks the sprite by file name; a free name gets a -1, -2 suffix.
                  </p>
                </div>
              </div>
            )}

            {tab === 'image' && (
              <ImagePromptTab
                prompts={meta.imgprompts ?? {}}
                status={imgStatus}
                template={imgTemplate}
                busy={busy === 'img'}
                disabled={!!busy}
                onTemplate={onImgTemplate}
                onRun={onImgPrompt}
              />
            )}

            {tab === 'raw' && (
              <div className="space-y-3 p-3">
                <div className="flex items-center gap-2">
                  <FileJson className="w-4 h-4 text-fg-subtle" />
                  <span className="text-[11px] font-semibold uppercase tracking-wide text-fg-subtle">
                    st_expr (editable)
                  </span>
                  <div className="flex-1" />
                  <button
                    type="button"
                    className={primaryCls}
                    disabled={saving}
                    onClick={() =>
                      run(async () => {
                        let parsed: SpriteMeta;
                        try {
                          parsed = JSON.parse(rawJson) as SpriteMeta;
                        } catch (e) {
                          throw new Error(`Invalid JSON: ${errText(e)}`, { cause: e });
                        }
                        await spriteLoreApi.saveMeta(im.path, parsed, false);
                      }, () => onChanged())
                    }
                  >
                    <Save className="w-3.5 h-3.5" /> Save JSON
                  </button>
                </div>
                <textarea
                  className={`${inputCls} h-72 font-mono text-[11px]`}
                  spellCheck={false}
                  value={rawJson}
                  onChange={(e) => setRawJson(e.target.value)}
                />
                <div className="space-y-2">
                  <span className="text-[11px] font-semibold uppercase tracking-wide text-fg-subtle">
                    All text chunks in this PNG
                  </span>
                  {!chunks && <Loader2 className="w-4 h-4 animate-spin text-accent" />}
                  {chunks && !chunks.length && <p className="text-xs text-fg-subtle">None.</p>}
                  {chunks?.map((c, i) => (
                    <details key={i} className="rounded-lg border border-border p-2 text-xs">
                      <summary className="cursor-pointer">
                        <b>{c.key}</b> <span className="text-fg-subtle">{c.type} · {c.length} chars</span>
                      </summary>
                      <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] text-fg-muted">
                        {c.json ? JSON.stringify(c.json, null, 2) : c.value}
                      </pre>
                    </details>
                  ))}
                </div>
              </div>
            )}

            {tab === 'prompt' && (
              <div className="space-y-2 p-3">
                <button
                  type="button"
                  className={btnCls}
                  disabled={saving}
                  onClick={() =>
                    run(
                      () =>
                        spriteLoreApi.previewPrompt({
                          path: im.path,
                          templateId: settings.activeTemplate,
                          pending: pendingTexts(),
                        }),
                      (r) => setPrompt((r as { messages: ChatMessage[] }).messages),
                    )
                  }
                >
                  <Eye className="w-3.5 h-3.5" /> Show the prompt that would be sent
                </button>
                {prompt?.map((m, i) => (
                  <div key={i} className="rounded-lg border border-border p-2">
                    <p className="mb-1 text-[10px] font-semibold uppercase text-fg-subtle">{m.role}</p>
                    <pre className="whitespace-pre-wrap break-words font-mono text-[11px] text-fg-muted">{m.content}</pre>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </aside>
  );
};

function copyText(text: string): void {
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
  } else {
    fallbackCopy(text);
  }
}

function fallbackCopy(text: string): void {
  const ta = document.createElement('textarea');
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  ta.remove();
}

const PromptBlock: React.FC<{ title: string; sub?: string; positive: string; negative: string }> = ({
  title,
  sub,
  positive,
  negative,
}) => (
  <div className="space-y-1 rounded-lg border border-border p-2">
    <p className="text-[11px] font-semibold text-fg">
      {title} {sub && <span className="font-normal text-fg-subtle">{sub}</span>}
    </p>
    {(
      [
        ['+', positive],
        ['-', negative],
      ] as const
    ).map(([sign, text]) => (
      <div key={sign} className="flex items-start gap-1.5">
        <span className={`mt-0.5 w-3 text-xs font-bold ${sign === '+' ? 'text-success' : 'text-danger'}`}>{sign}</span>
        <p className="flex-1 whitespace-pre-wrap break-words text-[11px] text-fg-muted">{text || '(empty)'}</p>
        <button
          type="button"
          className="p-0.5 text-fg-subtle hover:text-accent"
          title="Copy"
          onClick={() => copyText(text)}
        >
          <Copy className="w-3.5 h-3.5" />
        </button>
      </div>
    ))}
  </div>
);

const ImagePromptTab: React.FC<{
  prompts: Record<string, ImgPromptSummary>;
  status: ImgPromptStatus | null;
  template: string;
  busy: boolean;
  disabled: boolean;
  onTemplate: (name: string) => void;
  onRun: (force: boolean) => void;
}> = ({ prompts, status, template, busy, disabled, onTemplate, onRun }) => {
  const [show, setShow] = useState(template);
  const shown = prompts[show] ?? prompts[template] ?? Object.values(prompts)[0];
  if (!status?.found) {
    return (
      <div className="space-y-2 p-3 text-xs">
        <p className="text-warning">{status?.error ?? 'anime2img not found.'}</p>
        <p className="text-fg-subtle">Settings → Image prompts sets the anime2img folder.</p>
      </div>
    );
  }
  return (
    <div className="space-y-3 p-3 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <select
          className={selectCls}
          value={template}
          onChange={(e) => {
            onTemplate(e.target.value);
            setShow(e.target.value);
          }}
        >
          {status.templates.map((t) => (
            <option key={t.name} value={t.name}>
              {t.name}
            </option>
          ))}
        </select>
        <button type="button" className={primaryCls} disabled={disabled} onClick={() => onRun(false)}>
          {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ImagePlus className="w-3.5 h-3.5" />}
          {prompts[template] ? 'Update' : 'Write image prompt'}
        </button>
        {prompts[template] && (
          <button type="button" className={btnCls} disabled={disabled} onClick={() => onRun(true)}>
            <RefreshCw className="w-3.5 h-3.5" /> Re-run
          </button>
        )}
      </div>
      <p className="text-[11px] text-fg-subtle">
        One vision call through your anime2img template. A copy for ComfyUI goes to {status.output}; the prompts are
        also kept in this sprite.
      </p>
      {Object.keys(prompts).length > 1 && (
        <div className="flex flex-wrap gap-1">
          {Object.keys(prompts).map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => setShow(k)}
              className={`rounded-full border px-2 py-0.5 text-[11px] ${
                shown?.template === k ? 'border-accent bg-accent-soft text-accent' : 'border-border text-fg-muted'
              }`}
            >
              {k}
            </button>
          ))}
        </div>
      )}
      {shown ? (
        <div className="space-y-2">
          <p className="break-all text-[11px] text-fg-subtle">
            {shown.template} · {shown.ts}
            {shown.ages.length ? ` · age ${shown.ages.join('/')}` : ''} · {shown.out}
          </p>
          <PromptBlock title="base" positive={shown.base.positive} negative={shown.base.negative} />
          {shown.variations.map((v, i) => (
            <PromptBlock
              key={i}
              title={`${i + 1}. ${v.label}`}
              sub={v.change}
              positive={v.positive}
              negative={v.negative}
            />
          ))}
          {shown.removed.length > 0 && (
            <p className="text-[11px] text-fg-subtle">style terms cleaned: {shown.removed.join(', ')}</p>
          )}
          {shown.notes && (
            <details>
              <summary className="cursor-pointer text-[11px] text-fg-subtle">model notes</summary>
              <p className="mt-1 whitespace-pre-wrap text-[11px] text-fg-muted">{shown.notes}</p>
            </details>
          )}
        </div>
      ) : (
        <p className="text-fg-subtle">No image prompt written for this sprite yet.</p>
      )}
    </div>
  );
};
