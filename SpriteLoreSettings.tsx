import React, { useEffect, useMemo, useState } from 'react';
import {
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  Copy,
  Loader2,
  Plug,
  Plus,
  RotateCcw,
  Save,
  Trash2,
  Undo2,
  X,
  XCircle,
} from 'lucide-react';
import {
  PROFILE_KEYS,
  getServerUrl,
  pickProfile,
  setServerUrl,
  spriteLoreApi,
  splitList,
} from './SpriteLoreApi';
import type {
  Backend,
  ImgPromptStatus,
  BackendTest,
  Direction,
  EmotionRule,
  LadderStep,
  LoreTemplate,
  ProfileData,
  Settings,
  SettingsSection,
} from './SpriteLoreApi';
import { TagChips } from './SpriteLoreTags';
import { TemplateUpgrade, TemplateUpgradeBanner } from './SpriteLoreUpgrade';

type Tab = 'backends' | 'templates' | 'tagger' | 'images' | 'intensity' | 'directions' | 'emotions' | 'tags' | 'lore' | 'server';

const TABS: { id: Tab; label: string; sections: SettingsSection[] }[] = [
  { id: 'backends', label: 'Backends', sections: ['backends', 'activeVlm', 'activeLlm'] },
  { id: 'templates', label: 'Lore templates', sections: ['templates', 'activeTemplate'] },
  { id: 'tagger', label: 'VLM tagger', sections: ['taggerTemplate'] },
  { id: 'images', label: 'Image prompts', sections: ['imgprompt'] },
  { id: 'intensity', label: 'Degrees', sections: ['intensity'] },
  { id: 'directions', label: 'Directions', sections: ['directions', 'defaultRotation'] },
  { id: 'emotions', label: 'Emotion rules', sections: ['emotionRules'] },
  { id: 'tags', label: 'Tag lists', sections: ['tagGroups'] },
  { id: 'lore', label: 'Lore limits', sections: ['lore'] },
  { id: 'server', label: 'Folders & server', sections: ['roots'] },
];

const PLACEHOLDERS = [
  ['{{CARD}}', 'character card (name, description, personality, scenario)'],
  ['{{NOTES}}', "this character's author notes block"],
  ['{{IMAGE}}', 'sprite file name'],
  ['{{EMOTION}}', 'emotion label from the file name'],
  ['{{INTENSITY}} / {{INTENSITY_DESC}}', 'assigned degree and its description'],
  ['{{RANK}} / {{GROUP_SIZE}}', 'position among sprites of the same emotion'],
  ['{{DIRECTION}} / {{DIRECTION_DESC}}', 'who or what the emotion is aimed at'],
  ['{{VISUAL}}', 'VLM tags and description'],
  ['{{EMOTION_RULE}}', 'rule text for this emotion'],
  ['{{SIBLINGS}}', 'other sprites of the same emotion and their lore'],
  ['{{FOCUS}}', 'lore focus tags + emotion tags'],
  ['{{PRIOR}}', 'lore already written for other emotions (do-not-repeat)'],
  ['{{INSTRUCTION}}', "this character's saved custom instruction"],
  ['{{LORE_WORDS}} / {{KEYWORD_COUNT}} / {{KEYWORD_SLOTS}}', 'limits from Lore limits'],
  ['{{char}}', 'replaced with the character name'],
  ['{{user}}', 'left as is (SillyTavern macro)'],
];

const inputCls =
  'w-full rounded-lg border border-border bg-bg px-2 py-1.5 text-sm text-fg outline-none focus:border-accent';
const barInputCls =
  'rounded-lg border border-border bg-bg px-2 py-1 text-sm text-fg outline-none focus:border-accent';
const btnCls =
  'inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-xs font-medium text-fg-muted hover:text-fg hover:border-accent/60 transition-colors disabled:opacity-50';

const Field: React.FC<{ label: string; hint?: string; children: React.ReactNode; className?: string }> = ({
  label,
  hint,
  children,
  className,
}) => (
  <label className={`block ${className ?? ''}`}>
    <span className="block text-[11px] font-semibold uppercase tracking-wide text-fg-subtle mb-1">{label}</span>
    {children}
    {hint && <span className="block text-[11px] text-fg-subtle mt-1">{hint}</span>}
  </label>
);

function uid(prefix: string, taken: string[]): string {
  let i = 1;
  while (taken.includes(`${prefix}-${i}`)) i++;
  return `${prefix}-${i}`;
}

function move<T>(list: T[], i: number, d: number): T[] {
  const j = i + d;
  if (j < 0 || j >= list.length) return list;
  const out = [...list];
  [out[i], out[j]] = [out[j], out[i]];
  return out;
}

interface Props {
  settings: Settings;
  defaults: Settings;
  hasPrev: boolean;
  labels: string[];
  imgStatus: ImgPromptStatus | null;
  onImgStatus: (s: ImgPromptStatus) => void;
  initialProfile: string;
  onClose: () => void;
  onSaved: (s: Settings) => void;
}

export const SpriteLoreSettings: React.FC<Props> = ({
  settings,
  defaults,
  hasPrev,
  labels,
  imgStatus,
  onImgStatus,
  initialProfile,
  onClose,
  onSaved,
}) => {
  const [draft, setDraft] = useState<Settings>(() => structuredClone(settings));
  const [baseline, setBaseline] = useState<Settings>(settings);
  const [profiles, setProfiles] = useState<Record<string, ProfileData>>({});
  const [editing, setEditing] = useState('');
  const [newProfile, setNewProfile] = useState('');
  const [renameProfile, setRenameProfile] = useState<string | null>(null);
  const [profileMsg, setProfileMsg] = useState<string | null>(null);
  const [upgradeOpen, setUpgradeOpen] = useState(false);
  const [tab, setTab] = useState<Tab>('backends');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tests, setTests] = useState<Record<string, BackendTest | 'running'>>({});
  const [tmplId, setTmplId] = useState(settings.activeTemplate);
  const [emotion, setEmotion] = useState('default');
  const [newEmotion, setNewEmotion] = useState('');
  const [serverUrl, setServerUrlDraft] = useState(getServerUrl());
  const [rootsText, setRootsText] = useState(settings.roots.join('\n'));
  const [imgName, setImgName] = useState(settings.imgprompt?.activeTemplate || imgStatus?.templates[0]?.name || '');
  const [imgTexts, setImgTexts] = useState<Record<string, string>>({});
  const [imgMsg, setImgMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [imgNew, setImgNew] = useState('');
  const dirty = useMemo(() => JSON.stringify(draft) !== JSON.stringify(baseline), [draft, baseline]);
  const instrDirty = useMemo(
    () => PROFILE_KEYS.some((k) => JSON.stringify(draft[k]) !== JSON.stringify(baseline[k])),
    [draft, baseline],
  );

  const loadInto = (name: string, list: Record<string, ProfileData>) => {
    const src: Settings = name && list[name] ? { ...settings, ...structuredClone(list[name]) } : settings;
    setDraft((d) => {
      const out = { ...d } as Record<string, unknown>;
      PROFILE_KEYS.forEach((k) => {
        out[k] = structuredClone(src[k]);
      });
      return out as unknown as Settings;
    });
    setBaseline((b) => {
      const out = { ...b } as Record<string, unknown>;
      PROFILE_KEYS.forEach((k) => {
        out[k] = src[k];
      });
      return out as unknown as Settings;
    });
    setEditing(name && list[name] ? name : '');
    setTmplId(src.activeTemplate);
  };

  useEffect(() => {
    let alive = true;
    spriteLoreApi
      .getProfiles()
      .then((r) => {
        if (!alive) return;
        setProfiles(r.profiles);
        if (initialProfile && r.profiles[initialProfile]) loadInto(initialProfile, r.profiles);
      })
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const switchProfile = (name: string) => {
    if (instrDirty && !confirm('Discard the unsaved instruction changes?')) return;
    loadInto(name, profiles);
    setProfileMsg(null);
  };

  const saveAsProfile = async () => {
    const name = newProfile.trim();
    if (!name) return;
    if (profiles[name] && !confirm(`Overwrite profile "${name}"?`)) return;
    try {
      const r = await spriteLoreApi.saveProfile(name, pickProfile(draft));
      setProfiles(r.profiles);
      loadInto(r.name, r.profiles);
      setNewProfile('');
      setProfileMsg(`Saved profile "${r.name}". Pick it per character in the character box.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const doRenameProfile = async () => {
    const to = (renameProfile ?? '').trim();
    if (!editing || !to || to === editing) {
      setRenameProfile(null);
      return;
    }
    try {
      const r = await spriteLoreApi.saveProfile(to, pickProfile(draft), editing);
      setProfiles(r.profiles);
      loadInto(r.name, r.profiles);
      setRenameProfile(null);
      setProfileMsg(`Renamed to "${r.name}"; characters using it were updated.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const deleteProfile = async () => {
    if (!editing || !confirm(`Delete profile "${editing}"? Characters using it fall back to Default.`)) return;
    try {
      const r = await spriteLoreApi.deleteProfile(editing);
      setProfiles(r.profiles);
      loadInto('', r.profiles);
      setProfileMsg('Profile deleted.');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const patch = <K extends keyof Settings>(key: K, value: Settings[K]) => setDraft((d) => ({ ...d, [key]: value }));

  const resetTab = () => {
    const t = TABS.find((x) => x.id === tab);
    if (!t) return;
    setDraft((d) => {
      const out = { ...d } as Record<string, unknown>;
      t.sections.forEach((s) => {
        out[s] = structuredClone(defaults[s]);
      });
      return out as unknown as Settings;
    });
    if (tab === 'server') setRootsText(defaults.roots.join('\n'));
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const roots = splitList(rootsText.replace(/\n/g, ','));
      if (serverUrl.trim() && serverUrl.trim() !== getServerUrl()) setServerUrl(serverUrl.trim());
      let global: Settings = { ...draft, roots };
      if (editing) {
        await spriteLoreApi.saveProfile(editing, pickProfile(draft));
        global = { ...draft, roots } as Settings;
        PROFILE_KEYS.forEach((k) => {
          (global as unknown as Record<string, unknown>)[k] = settings[k];
        });
      }
      const res = await spriteLoreApi.saveSettings(global);
      onSaved(res.settings);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const undo = async () => {
    if (!confirm('Replace the current settings with the ones from before your last save?')) return;
    setBusy(true);
    try {
      const res = await spriteLoreApi.undoSettings();
      onSaved(res.settings);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const resetAll = () => {
    if (!confirm('Reset every section to the shipped defaults? Nothing is written until you press Save.')) return;
    setDraft(structuredClone(defaults));
    setRootsText(defaults.roots.join('\n'));
  };

  const updBackend = (i: number, p: Partial<Backend>) =>
    patch(
      'backends',
      draft.backends.map((b, j) => (j === i ? { ...b, ...p } : b)),
    );

  const testBackend = async (b: Backend) => {
    setTests((t) => ({ ...t, [b.id]: 'running' }));
    try {
      const r = await spriteLoreApi.testBackend(b);
      setTests((t) => ({ ...t, [b.id]: r }));
    } catch (e) {
      setTests((t) => ({
        ...t,
        [b.id]: { ok: false, models: [], vision: null, error: e instanceof Error ? e.message : String(e) },
      }));
    }
  };

  const imgTmpl = imgStatus?.templates.find((t) => t.name === imgName) ?? imgStatus?.templates[0];
  const imgText = imgTmpl ? (imgTexts[imgTmpl.name] ?? imgTmpl.text) : '';
  const imgCall = async (fn: () => Promise<ImgPromptStatus>, ok: string) => {
    setImgMsg(null);
    try {
      const st = await fn();
      onImgStatus(st);
      setImgTexts({});
      setImgMsg({ ok: true, text: ok });
    } catch (e) {
      setImgMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    }
  };

  const tmpl = draft.templates.find((t) => t.id === tmplId) ?? draft.templates[0];
  const updTmpl = (p: Partial<LoreTemplate>) =>
    patch(
      'templates',
      draft.templates.map((t) => (t.id === tmpl?.id ? { ...t, ...p } : t)),
    );

  const ladderIds = draft.intensity.ladder.map((l) => l.id);
  const dirIds = draft.directions.map((d) => d.id);
  const spreadMax = Math.max(6, draft.intensity.ladder.length);
  const emotionKeys = Array.from(new Set(['default', ...Object.keys(draft.emotionRules), ...labels]));
  const rule: EmotionRule = draft.emotionRules[emotion] ?? { rule: '', tags: [], directions: [] };
  const setRule = (p: Partial<EmotionRule>) =>
    patch('emotionRules', { ...draft.emotionRules, [emotion]: { ...rule, ...p } });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-overlay p-4">
      <div className="flex h-[90vh] w-full max-w-6xl flex-col overflow-hidden rounded-2xl border border-border bg-surface shadow-2xl">
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <div>
            <h2 className="text-base font-semibold text-fg">Sprite Lore settings</h2>
            <p className="text-xs text-fg-subtle">
              Shipped defaults are never overwritten. Each save keeps the previous version as a backup.
            </p>
          </div>
          <button type="button" onClick={onClose} className="p-2 text-fg-muted hover:text-fg" aria-label="Close">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex flex-wrap items-center gap-2 border-b border-border px-5 py-2">
          <label className="flex items-center gap-1.5 text-xs text-fg-muted">
            Editing
            <select className={`${barInputCls} w-56`} value={editing} onChange={(e) => switchProfile(e.target.value)}>
              <option value="">Default (all characters)</option>
              {Object.keys(profiles)
                .sort()
                .map((p) => (
                  <option key={p} value={p}>
                    Profile: {p}
                  </option>
                ))}
            </select>
          </label>
          {editing &&
            (renameProfile === null ? (
              <>
                <button type="button" className={btnCls} onClick={() => setRenameProfile(editing)}>
                  Rename
                </button>
                <button type="button" className={btnCls} onClick={deleteProfile}>
                  <Trash2 className="w-3.5 h-3.5" /> Delete
                </button>
              </>
            ) : (
              <>
                <input
                  className={`${barInputCls} w-44`}
                  value={renameProfile}
                  onChange={(e) => setRenameProfile(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && doRenameProfile()}
                />
                <button type="button" className={btnCls} onClick={doRenameProfile}>
                  OK
                </button>
              </>
            ))}
          <input
            className={`${barInputCls} w-48`}
            placeholder="new profile name"
            value={newProfile}
            onChange={(e) => setNewProfile(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && saveAsProfile()}
          />
          <button type="button" className={btnCls} disabled={!newProfile.trim()} onClick={saveAsProfile}>
            <Copy className="w-3.5 h-3.5" /> Save as profile
          </button>
        </div>
        {(editing || profileMsg) && (
          <div className="border-b border-border bg-accent-soft px-5 py-1.5 text-xs text-accent">
            {profileMsg ??
              `Editing profile "${editing}": templates, tagger, degrees, directions, emotion rules, tag lists and lore limits are saved to this profile. Backends, image prompts and folders are shared by all profiles.`}
          </div>
        )}

        <div className="relative flex min-h-0 flex-1">
          {upgradeOpen && (
            <TemplateUpgrade
              templates={draft.templates}
              onApply={(next) => {
                patch('templates', next);
                setProfileMsg(
                  `Upgraded templates are in the editor. Press ${editing ? 'Save profile' : 'Save'} to keep them.`,
                );
              }}
              onClose={() => setUpgradeOpen(false)}
            />
          )}
          <nav className="w-44 shrink-0 border-r border-border p-2 space-y-0.5 overflow-y-auto">
            {TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => setTab(t.id)}
                className={`w-full rounded-lg px-3 py-1.5 text-left text-sm transition-colors ${
                  tab === t.id ? 'bg-accent-soft text-accent font-medium' : 'text-fg-muted hover:bg-hover hover:text-fg'
                }`}
              >
                {t.label}
              </button>
            ))}
          </nav>

          <div className="min-w-0 flex-1 overflow-y-auto p-5 space-y-4">
            {tab === 'backends' && (
              <>
                <div className="grid grid-cols-2 gap-3">
                  <Field label="Active vision backend (tagging)">
                    <select className={inputCls} value={draft.activeVlm} onChange={(e) => patch('activeVlm', e.target.value)}>
                      {draft.backends.map((b) => (
                        <option key={b.id} value={b.id}>
                          {b.name}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Active lore backend (writing)">
                    <select className={inputCls} value={draft.activeLlm} onChange={(e) => patch('activeLlm', e.target.value)}>
                      {draft.backends.map((b) => (
                        <option key={b.id} value={b.id}>
                          {b.name}
                        </option>
                      ))}
                    </select>
                  </Field>
                </div>
                {draft.backends.map((b, i) => {
                  const t = tests[b.id];
                  return (
                    <div key={b.id} className="rounded-xl border border-border p-3 space-y-2">
                      <div className="grid grid-cols-12 gap-2">
                        <Field label="Name" className="col-span-4">
                          <input className={inputCls} value={b.name} onChange={(e) => updBackend(i, { name: e.target.value })} />
                        </Field>
                        <Field label="Base URL" className="col-span-5">
                          <input className={inputCls} value={b.url} onChange={(e) => updBackend(i, { url: e.target.value })} />
                        </Field>
                        <Field label="Use for" className="col-span-3">
                          <select
                            className={inputCls}
                            value={b.role}
                            onChange={(e) => updBackend(i, { role: e.target.value as Backend['role'] })}
                          >
                            <option value="vlm">vision tagging</option>
                            <option value="llm">lore writing</option>
                            <option value="both">both</option>
                          </select>
                        </Field>
                        <Field label="Model (optional)" className="col-span-4">
                          <input
                            className={inputCls}
                            list={`models-${b.id}`}
                            value={b.model}
                            onChange={(e) => updBackend(i, { model: e.target.value })}
                            placeholder="whatever is loaded"
                          />
                          <datalist id={`models-${b.id}`}>
                            {t && t !== 'running' && t.models.map((m) => <option key={m} value={m} />)}
                          </datalist>
                        </Field>
                        <Field label="API key" className="col-span-3">
                          <input
                            className={inputCls}
                            type="password"
                            value={b.apiKey}
                            onChange={(e) => updBackend(i, { apiKey: e.target.value })}
                          />
                        </Field>
                        <Field label="JSON mode" className="col-span-3" hint="schema = KoboldCpp grammar">
                          <select
                            className={inputCls}
                            value={b.jsonMode}
                            onChange={(e) => updBackend(i, { jsonMode: e.target.value as Backend['jsonMode'] })}
                          >
                            <option value="off">off</option>
                            <option value="json_object">json_object</option>
                            <option value="schema">json schema</option>
                          </select>
                        </Field>
                        <Field label="Temp" className="col-span-2">
                          <input
                            className={inputCls}
                            type="number"
                            step="0.05"
                            value={b.temperature}
                            onChange={(e) => updBackend(i, { temperature: Number(e.target.value) })}
                          />
                        </Field>
                        <Field label="Max tokens" className="col-span-2">
                          <input
                            className={inputCls}
                            type="number"
                            value={b.maxTokens}
                            onChange={(e) => updBackend(i, { maxTokens: Number(e.target.value) })}
                          />
                        </Field>
                        <Field label="Timeout s" className="col-span-2">
                          <input
                            className={inputCls}
                            type="number"
                            value={b.timeout}
                            onChange={(e) => updBackend(i, { timeout: Number(e.target.value) })}
                          />
                        </Field>
                      </div>
                      <div className="flex flex-wrap items-center gap-2">
                        <button type="button" className={btnCls} onClick={() => testBackend(b)} disabled={t === 'running'}>
                          {t === 'running' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plug className="w-3.5 h-3.5" />}
                          Test
                        </button>
                        <button
                          type="button"
                          className={btnCls}
                          onClick={() =>
                            patch('backends', [
                              ...draft.backends,
                              { ...b, id: uid('backend', draft.backends.map((x) => x.id)), name: `${b.name} copy` },
                            ])
                          }
                        >
                          <Copy className="w-3.5 h-3.5" /> Duplicate
                        </button>
                        <button
                          type="button"
                          className={btnCls}
                          disabled={draft.backends.length <= 1}
                          onClick={() => patch('backends', draft.backends.filter((_, j) => j !== i))}
                        >
                          <Trash2 className="w-3.5 h-3.5" /> Remove
                        </button>
                        {t && t !== 'running' && (
                          <span className={`text-xs ${t.ok && (t.vision?.ok ?? true) ? 'text-success' : 'text-danger'}`}>
                            {t.ok ? (
                              <>
                                <CheckCircle2 className="inline w-3.5 h-3.5 mr-1" />
                                {t.models.length ? `models: ${t.models.join(', ')}` : 'reachable'}
                                {t.vision &&
                                  (t.vision.ok
                                    ? ' · vision OK'
                                    : ` · NOT seeing images (red→${t.vision.answers.red}, blue→${t.vision.answers.blue}); check the mmproj`)}
                              </>
                            ) : (
                              <>
                                <XCircle className="inline w-3.5 h-3.5 mr-1" />
                                {t.error}
                              </>
                            )}
                          </span>
                        )}
                      </div>
                    </div>
                  );
                })}
                <button
                  type="button"
                  className={btnCls}
                  onClick={() =>
                    patch('backends', [
                      ...draft.backends,
                      {
                        id: uid('backend', draft.backends.map((x) => x.id)),
                        name: 'New backend',
                        url: 'http://127.0.0.1:5001/v1',
                        model: '',
                        apiKey: '',
                        role: 'llm',
                        temperature: 0.8,
                        maxTokens: 700,
                        jsonMode: 'off',
                        timeout: 900,
                      },
                    ])
                  }
                >
                  <Plus className="w-3.5 h-3.5" /> Add backend
                </button>
              </>
            )}

            {tab === 'templates' && (
              <TemplateUpgradeBanner templates={draft.templates} onOpen={() => setUpgradeOpen(true)} />
            )}
            {tab === 'templates' && tmpl && (
              <div className="grid grid-cols-12 gap-4">
                <div className="col-span-3 space-y-1">
                  {draft.templates.map((t) => (
                    <button
                      key={t.id}
                      type="button"
                      onClick={() => setTmplId(t.id)}
                      className={`w-full rounded-lg px-2.5 py-1.5 text-left text-sm ${
                        t.id === tmpl.id ? 'bg-accent-soft text-accent' : 'text-fg-muted hover:bg-hover'
                      }`}
                    >
                      {t.name}
                      {t.id === draft.activeTemplate && <span className="ml-1 text-[10px] text-fg-subtle">(active)</span>}
                    </button>
                  ))}
                  <div className="flex flex-wrap gap-1 pt-2">
                    <button
                      type="button"
                      className={btnCls}
                      onClick={() => {
                        const id = uid('template', draft.templates.map((x) => x.id));
                        patch('templates', [...draft.templates, { ...tmpl, id, name: `${tmpl.name} copy` }]);
                        setTmplId(id);
                      }}
                    >
                      <Copy className="w-3.5 h-3.5" /> Duplicate
                    </button>
                    <button
                      type="button"
                      className={btnCls}
                      disabled={draft.templates.length <= 1}
                      onClick={() => {
                        const rest = draft.templates.filter((t) => t.id !== tmpl.id);
                        patch('templates', rest);
                        if (draft.activeTemplate === tmpl.id) patch('activeTemplate', rest[0].id);
                        setTmplId(rest[0].id);
                      }}
                    >
                      <Trash2 className="w-3.5 h-3.5" /> Delete
                    </button>
                    {defaults.templates.some((d) => d.id === tmpl.id) && (
                      <button
                        type="button"
                        className={btnCls}
                        onClick={() => {
                          const d = defaults.templates.find((x) => x.id === tmpl.id);
                          if (d) updTmpl(structuredClone(d));
                        }}
                      >
                        <RotateCcw className="w-3.5 h-3.5" /> Default text
                      </button>
                    )}
                  </div>
                </div>
                <div className="col-span-9 space-y-3">
                  <div className="grid grid-cols-12 gap-2">
                    <Field label="Name" className="col-span-6">
                      <input className={inputCls} value={tmpl.name} onChange={(e) => updTmpl({ name: e.target.value })} />
                    </Field>
                    <Field label="Temperature override" className="col-span-3" hint="empty = backend's">
                      <input
                        className={inputCls}
                        type="number"
                        step="0.05"
                        value={tmpl.temperature ?? ''}
                        onChange={(e) => updTmpl({ temperature: e.target.value === '' ? null : Number(e.target.value) })}
                      />
                    </Field>
                    <Field label="Active" className="col-span-3">
                      <button
                        type="button"
                        className={`${btnCls} w-full justify-center`}
                        disabled={draft.activeTemplate === tmpl.id}
                        onClick={() => patch('activeTemplate', tmpl.id)}
                      >
                        {draft.activeTemplate === tmpl.id ? 'Active template' : 'Make active'}
                      </button>
                    </Field>
                  </div>
                  <Field label="System prompt">
                    <textarea
                      className={`${inputCls} font-mono text-xs h-24`}
                      value={tmpl.system}
                      onChange={(e) => updTmpl({ system: e.target.value })}
                    />
                  </Field>
                  <Field label="User prompt">
                    <textarea
                      className={`${inputCls} font-mono text-xs h-80`}
                      value={tmpl.user}
                      onChange={(e) => updTmpl({ user: e.target.value })}
                    />
                  </Field>
                  <div className="rounded-xl border border-border bg-bg p-3">
                    <p className="text-[11px] font-semibold uppercase tracking-wide text-fg-subtle mb-1">Placeholders</p>
                    <ul className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs text-fg-muted">
                      {PLACEHOLDERS.map(([k, v]) => (
                        <li key={k}>
                          <code className="text-accent">{k}</code> {v}
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              </div>
            )}

            {tab === 'tagger' && (
              <>
                <p className="text-xs text-fg-muted">
                  Sent with each image to the vision backend. Placeholders: <code>{'{{CHAR}}'}</code>,{' '}
                  <code>{'{{LABELS}}'}</code>, <code>{'{{HINT}}'}</code>. The reply must keep the JSON keys emotion,
                  confidence, intensity (1-5), directed_at, appearance, expression, action, objects, setting, description.
                  Intensity orders sprites of the same emotion from mildest to strongest.
                </p>
                <Field label="System prompt">
                  <textarea
                    className={`${inputCls} font-mono text-xs h-28`}
                    value={draft.taggerTemplate.system}
                    onChange={(e) => patch('taggerTemplate', { ...draft.taggerTemplate, system: e.target.value })}
                  />
                </Field>
                <Field label="User prompt">
                  <textarea
                    className={`${inputCls} font-mono text-xs h-96`}
                    value={draft.taggerTemplate.user}
                    onChange={(e) => patch('taggerTemplate', { ...draft.taggerTemplate, user: e.target.value })}
                  />
                </Field>
              </>
            )}

            {tab === 'images' && (
              <>
                <p className="text-xs text-fg-muted">
                  Image prompts run your own anime2img.py and its anime2img_&lt;name&gt;.txt templates: one vision call
                  per sprite, photoreal base prompt plus variations, written as a PNG copy for the Load Image + Embedded
                  Prompt node and kept in the sprite too.
                </p>
                <div className="grid grid-cols-2 gap-3">
                  <Field
                    label="anime2img folder"
                    hint={imgStatus?.dir ? `in use: ${imgStatus.dir}` : imgStatus?.error ?? 'using the copy bundled with Sprite Lore'}
                  >
                    <input
                      className={inputCls}
                      placeholder="auto: newest copy found"
                      list="a2i-candidates"
                      value={draft.imgprompt.anime2imgDir}
                      onChange={(e) => patch('imgprompt', { ...draft.imgprompt, anime2imgDir: e.target.value })}
                    />
                    <datalist id="a2i-candidates">
                      {(imgStatus?.candidates ?? []).map((c) => (
                        <option key={c.dir} value={c.dir}>
                          {new Date(c.mtime * 1000).toLocaleString()}
                        </option>
                      ))}
                    </datalist>
                    {(imgStatus?.candidates?.length ?? 0) > 1 && (
                      <span className="mt-1 block text-[11px] text-warning">
                        {imgStatus?.candidates?.length} copies of anime2img.py found; pick one from the list if the
                        newest is not the one you run.
                      </span>
                    )}
                  </Field>
                  <Field label="ComfyUI input/imgprompt folder" hint={imgStatus?.output ? `in use: ${imgStatus.output}` : undefined}>
                    <input
                      className={inputCls}
                      value={draft.imgprompt.comfyInput}
                      onChange={(e) => patch('imgprompt', { ...draft.imgprompt, comfyInput: e.target.value })}
                    />
                  </Field>
                  <Field label="Vision backend for image prompts">
                    <select
                      className={inputCls}
                      value={draft.imgprompt.backend}
                      onChange={(e) => patch('imgprompt', { ...draft.imgprompt, backend: e.target.value })}
                    >
                      <option value="">same as the active vision backend</option>
                      {draft.backends.map((b) => (
                        <option key={b.id} value={b.id}>
                          {b.name}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Default template">
                    <select
                      className={inputCls}
                      value={draft.imgprompt.activeTemplate}
                      onChange={(e) => patch('imgprompt', { ...draft.imgprompt, activeTemplate: e.target.value })}
                    >
                      {(imgStatus?.templates ?? []).map((t) => (
                        <option key={t.name} value={t.name}>
                          {t.name}
                        </option>
                      ))}
                    </select>
                  </Field>
                </div>
                {imgStatus?.found && imgTmpl && (
                  <div className="grid grid-cols-12 gap-4 border-t border-border pt-4">
                    <div className="col-span-3 space-y-1">
                      {imgStatus.templates.map((t) => (
                        <button
                          key={t.name}
                          type="button"
                          onClick={() => setImgName(t.name)}
                          className={`w-full rounded-lg px-2.5 py-1.5 text-left text-sm ${
                            t.name === imgTmpl.name ? 'bg-accent-soft text-accent' : 'text-fg-muted hover:bg-hover'
                          }`}
                        >
                          {t.name}
                          <span className="block text-[10px] text-fg-subtle">
                            {t.detail} · {t.style} · {t.variations} var · {t.vary}
                            {t.ageRange && ` · ${t.ageRange}`}
                          </span>
                        </button>
                      ))}
                      <div className="flex gap-1 pt-2">
                        <input
                          className={inputCls}
                          placeholder="new template name"
                          value={imgNew}
                          onChange={(e) => setImgNew(e.target.value.replace(/[^a-z0-9_-]/gi, '').toLowerCase())}
                        />
                        <button
                          type="button"
                          className={btnCls}
                          disabled={!imgNew}
                          title={`Copy of ${imgTmpl.name}`}
                          onClick={() =>
                            imgCall(() => spriteLoreApi.saveImgTemplate(imgNew, imgText, true), `created ${imgNew}`).then(
                              () => {
                                setImgName(imgNew);
                                setImgNew('');
                              },
                            )
                          }
                        >
                          <Plus className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </div>
                    <div className="col-span-9 space-y-2">
                      <p className="text-[11px] text-fg-subtle">
                        anime2img_{imgTmpl.name}.txt · saves straight to disk after a validity check; the first save
                        keeps a .orig copy{imgTmpl.hasOrig ? ' (one exists)' : ''}.
                      </p>
                      <textarea
                        className={`${inputCls} font-mono text-xs h-[46vh]`}
                        spellCheck={false}
                        value={imgText}
                        onChange={(e) => setImgTexts({ ...imgTexts, [imgTmpl.name]: e.target.value })}
                      />
                      <div className="flex flex-wrap items-center gap-2">
                        <button
                          type="button"
                          className={btnCls}
                          disabled={imgText === imgTmpl.text}
                          onClick={() =>
                            imgCall(() => spriteLoreApi.saveImgTemplate(imgTmpl.name, imgText), `saved ${imgTmpl.name}`)
                          }
                        >
                          <Save className="w-3.5 h-3.5" /> Save template file
                        </button>
                        <button
                          type="button"
                          className={btnCls}
                          disabled={!imgTmpl.hasOrig}
                          onClick={() =>
                            confirm(`Put the original anime2img_${imgTmpl.name}.txt back?`) &&
                            imgCall(() => spriteLoreApi.restoreImgTemplate(imgTmpl.name), `restored ${imgTmpl.name}`)
                          }
                        >
                          <RotateCcw className="w-3.5 h-3.5" /> Restore original
                        </button>
                        {imgMsg && (
                          <span className={`text-xs ${imgMsg.ok ? 'text-success' : 'text-danger'}`}>{imgMsg.text}</span>
                        )}
                      </div>
                    </div>
                  </div>
                )}
              </>
            )}

            {tab === 'intensity' && (
              <>
                <p className="text-xs text-fg-muted">
                  When an emotion has several sprites, each gets a different degree from this ladder, mildest to
                  strongest. The spread table says which degrees to use for each sprite count; counts without a row
                  are spread evenly across the ladder.
                </p>
                <div className="space-y-2">
                  {draft.intensity.ladder.map((s, i) => {
                    const upd = (p: Partial<LadderStep>) =>
                      patch('intensity', {
                        ...draft.intensity,
                        ladder: draft.intensity.ladder.map((x, j) => (j === i ? { ...x, ...p } : x)),
                      });
                    return (
                      <div key={i} className="grid grid-cols-12 gap-2 items-end">
                        <Field label={i === 0 ? 'Id' : ''} className="col-span-2">
                          <input className={inputCls} value={s.id} onChange={(e) => upd({ id: e.target.value.trim() })} />
                        </Field>
                        <Field label={i === 0 ? 'Name in prompt' : ''} className="col-span-2">
                          <input className={inputCls} value={s.name} onChange={(e) => upd({ name: e.target.value })} />
                        </Field>
                        <Field label={i === 0 ? 'How it shows (sent to the model)' : ''} className="col-span-7">
                          <input className={inputCls} value={s.desc} onChange={(e) => upd({ desc: e.target.value })} />
                        </Field>
                        <div className="col-span-1 flex gap-0.5 pb-1">
                          <button
                            type="button"
                            className="p-1 text-fg-subtle hover:text-fg"
                            onClick={() =>
                              patch('intensity', { ...draft.intensity, ladder: move(draft.intensity.ladder, i, -1) })
                            }
                          >
                            <ArrowUp className="w-3.5 h-3.5" />
                          </button>
                          <button
                            type="button"
                            className="p-1 text-fg-subtle hover:text-fg"
                            onClick={() =>
                              patch('intensity', { ...draft.intensity, ladder: move(draft.intensity.ladder, i, 1) })
                            }
                          >
                            <ArrowDown className="w-3.5 h-3.5" />
                          </button>
                          <button
                            type="button"
                            className="p-1 text-fg-subtle hover:text-danger"
                            onClick={() =>
                              patch('intensity', {
                                ...draft.intensity,
                                ladder: draft.intensity.ladder.filter((_, j) => j !== i),
                              })
                            }
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </div>
                      </div>
                    );
                  })}
                  <button
                    type="button"
                    className={btnCls}
                    onClick={() =>
                      patch('intensity', {
                        ...draft.intensity,
                        ladder: [...draft.intensity.ladder, { id: uid('degree', ladderIds), name: 'new degree', desc: '' }],
                      })
                    }
                  >
                    <Plus className="w-3.5 h-3.5" /> Add degree
                  </button>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <Field label="Degree when an emotion has one sprite">
                    <select
                      className={inputCls}
                      value={draft.intensity.single}
                      onChange={(e) => patch('intensity', { ...draft.intensity, single: e.target.value })}
                    >
                      {ladderIds.map((id) => (
                        <option key={id}>{id}</option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Order sprites by" hint="VLM order is used only when every sprite of the emotion is tagged">
                    <select
                      className={inputCls}
                      value={draft.intensity.orderBy}
                      onChange={(e) =>
                        patch('intensity', { ...draft.intensity, orderBy: e.target.value as 'vlm' | 'name' })
                      }
                    >
                      <option value="vlm">VLM intensity estimate, then file name</option>
                      <option value="name">file name (joy, joy-1, joy-2 ...)</option>
                    </select>
                  </Field>
                </div>
                <div className="space-y-1.5">
                  <p className="text-[11px] font-semibold uppercase tracking-wide text-fg-subtle">Spread table</p>
                  {Array.from({ length: spreadMax - 1 }, (_, k) => k + 2).map((n) => {
                    const row = draft.intensity.spread[String(n)] ?? [];
                    const bad = row.length > 0 && (row.length !== n || row.some((r) => !ladderIds.includes(r)));
                    return (
                      <div key={n} className="flex items-center gap-2">
                        <span className="w-24 text-xs text-fg-muted">{n} sprites</span>
                        <input
                          className={`${inputCls} ${bad ? 'border-danger' : ''}`}
                          value={row.join(', ')}
                          placeholder="even spread"
                          onChange={(e) => {
                            const spread = { ...draft.intensity.spread };
                            const v = splitList(e.target.value);
                            if (v.length) spread[String(n)] = v;
                            else delete spread[String(n)];
                            patch('intensity', { ...draft.intensity, spread });
                          }}
                        />
                        {bad && <span className="text-[11px] text-danger whitespace-nowrap">needs {n} known ids</span>}
                      </div>
                    );
                  })}
                </div>
              </>
            )}

            {tab === 'directions' && (
              <>
                <p className="text-xs text-fg-muted">
                  Who or what an emotion is aimed at. Sprites sharing an emotion are rotated through that emotion's
                  direction list (Emotion rules), or through the default rotation below.
                </p>
                {draft.directions.map((d, i) => {
                  const upd = (p: Partial<Direction>) =>
                    patch(
                      'directions',
                      draft.directions.map((x, j) => (j === i ? { ...x, ...p } : x)),
                    );
                  return (
                    <div key={i} className="grid grid-cols-12 gap-2 items-end">
                      <Field label={i === 0 ? 'Id' : ''} className="col-span-2">
                        <input className={inputCls} value={d.id} onChange={(e) => upd({ id: e.target.value.trim() })} />
                      </Field>
                      <Field label={i === 0 ? 'Name in prompt' : ''} className="col-span-3">
                        <input className={inputCls} value={d.name} onChange={(e) => upd({ name: e.target.value })} />
                      </Field>
                      <Field label={i === 0 ? 'Description (sent to the model)' : ''} className="col-span-6">
                        <input className={inputCls} value={d.desc} onChange={(e) => upd({ desc: e.target.value })} />
                      </Field>
                      <button
                        type="button"
                        className="col-span-1 p-1 pb-2 text-fg-subtle hover:text-danger"
                        onClick={() => patch('directions', draft.directions.filter((_, j) => j !== i))}
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  );
                })}
                <button
                  type="button"
                  className={btnCls}
                  onClick={() =>
                    patch('directions', [...draft.directions, { id: uid('target', dirIds), name: 'new target', desc: '' }])
                  }
                >
                  <Plus className="w-3.5 h-3.5" /> Add direction
                </button>
                <Field label="Default rotation (ids, comma separated)" hint={`known ids: ${dirIds.join(', ')}`}>
                  <input
                    className={inputCls}
                    value={draft.defaultRotation.join(', ')}
                    onChange={(e) => patch('defaultRotation', splitList(e.target.value))}
                  />
                </Field>
              </>
            )}

            {tab === 'emotions' && (
              <div className="grid grid-cols-12 gap-4">
                <div className="col-span-3 space-y-0.5 max-h-[70vh] overflow-y-auto pr-1">
                  {emotionKeys.map((k) => (
                    <button
                      key={k}
                      type="button"
                      onClick={() => setEmotion(k)}
                      className={`w-full rounded-lg px-2.5 py-1 text-left text-sm ${
                        k === emotion ? 'bg-accent-soft text-accent' : 'text-fg-muted hover:bg-hover'
                      }`}
                    >
                      {k}
                      {!draft.emotionRules[k] && <span className="ml-1 text-[10px] text-fg-subtle">(uses default)</span>}
                    </button>
                  ))}
                  <div className="flex gap-1 pt-2">
                    <input
                      className={inputCls}
                      value={newEmotion}
                      placeholder="new label"
                      onChange={(e) => setNewEmotion(e.target.value)}
                    />
                    <button
                      type="button"
                      className={btnCls}
                      onClick={() => {
                        const k = newEmotion.trim().toLowerCase();
                        if (!k) return;
                        if (!draft.emotionRules[k])
                          patch('emotionRules', {
                            ...draft.emotionRules,
                            [k]: { rule: draft.emotionRules.default?.rule ?? '', tags: [], directions: [] },
                          });
                        setEmotion(k);
                        setNewEmotion('');
                      }}
                    >
                      <Plus className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
                <div className="col-span-9 space-y-3">
                  <h3 className="text-sm font-semibold text-fg">{emotion}</h3>
                  <Field label="Rule (sent as EMOTION RULE)">
                    <textarea
                      className={`${inputCls} h-28`}
                      value={rule.rule}
                      onChange={(e) => setRule({ rule: e.target.value })}
                    />
                  </Field>
                  <Field
                    label="Direction rotation (ids, comma separated)"
                    hint={`empty = default rotation (${draft.defaultRotation.join(', ')}). Known ids: ${dirIds.join(', ')}`}
                  >
                    <input
                      className={inputCls}
                      value={rule.directions.join(', ')}
                      onChange={(e) => setRule({ directions: splitList(e.target.value) })}
                    />
                  </Field>
                  <div>
                    <span className="block text-[11px] font-semibold uppercase tracking-wide text-fg-subtle mb-1">
                      Emotion tags (appended to every {emotion} generation)
                    </span>
                    <TagChips
                      tags={rule.tags}
                      onRemove={(t) => setRule({ tags: rule.tags.filter((x) => x !== t) })}
                      onAdd={(t) => setRule({ tags: rule.tags.includes(t) ? rule.tags : [...rule.tags, t] })}
                    />
                  </div>
                  <div className="flex gap-2">
                    {defaults.emotionRules[emotion] && (
                      <button
                        type="button"
                        className={btnCls}
                        onClick={() => setRule(structuredClone(defaults.emotionRules[emotion]))}
                      >
                        <RotateCcw className="w-3.5 h-3.5" /> Default for {emotion}
                      </button>
                    )}
                    {emotion !== 'default' && draft.emotionRules[emotion] && (
                      <button
                        type="button"
                        className={btnCls}
                        onClick={() => {
                          const rules = { ...draft.emotionRules };
                          delete rules[emotion];
                          patch('emotionRules', rules);
                        }}
                      >
                        <Trash2 className="w-3.5 h-3.5" /> Remove rule
                      </button>
                    )}
                  </div>
                </div>
              </div>
            )}

            {tab === 'tags' && (
              <>
                <p className="text-xs text-fg-muted">
                  Tags shown in the lore focus picker for each character. Tags you add from the page are saved into the
                  Custom group.
                </p>
                {draft.tagGroups.map((g, i) => (
                  <div key={i} className="rounded-xl border border-border p-3 space-y-2">
                    <div className="flex items-center gap-2">
                      <input
                        className={`${inputCls} max-w-xs`}
                        value={g.name}
                        onChange={(e) =>
                          patch(
                            'tagGroups',
                            draft.tagGroups.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)),
                          )
                        }
                      />
                      <button
                        type="button"
                        className="p-1 text-fg-subtle hover:text-danger"
                        onClick={() => patch('tagGroups', draft.tagGroups.filter((_, j) => j !== i))}
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                    <TagChips
                      tags={g.tags}
                      onRemove={(t) =>
                        patch(
                          'tagGroups',
                          draft.tagGroups.map((x, j) => (j === i ? { ...x, tags: x.tags.filter((y) => y !== t) } : x)),
                        )
                      }
                      onAdd={(t) =>
                        patch(
                          'tagGroups',
                          draft.tagGroups.map((x, j) =>
                            j === i && !x.tags.includes(t) ? { ...x, tags: [...x.tags, t] } : x,
                          ),
                        )
                      }
                    />
                  </div>
                ))}
                <button
                  type="button"
                  className={btnCls}
                  onClick={() => patch('tagGroups', [...draft.tagGroups, { name: 'New group', tags: [] }])}
                >
                  <Plus className="w-3.5 h-3.5" /> Add group
                </button>
              </>
            )}

            {tab === 'lore' && (
              <div className="grid grid-cols-3 gap-3 max-w-2xl">
                {(
                  [
                    ['words', 'Target max words'],
                    ['maxChars', 'Reject above chars'],
                    ['keywords', 'Keywords per entry'],
                    ['priorMax', 'Do-not-repeat entries sent'],
                    ['cardChars', 'Card field char limit'],
                    ['retries', 'Retries on bad JSON'],
                    ['renameMinConfidence', 'Rename to tagger name at confidence ≥'],
                  ] as const
                ).map(([k, label]) => (
                  <Field key={k} label={label}>
                    <input
                      className={inputCls}
                      type="number"
                      step={k === 'renameMinConfidence' ? 0.05 : 1}
                      value={draft.lore[k]}
                      onChange={(e) => patch('lore', { ...draft.lore, [k]: Number(e.target.value) })}
                    />
                  </Field>
                ))}
                <label className="col-span-3 flex items-center gap-2 text-sm text-fg">
                  <input
                    type="checkbox"
                    checked={draft.lore.siblingAware}
                    onChange={(e) => patch('lore', { ...draft.lore, siblingAware: e.target.checked })}
                  />
                  Show the model the other sprites of the same emotion so each degree reads differently
                </label>
              </div>
            )}

            {tab === 'server' && (
              <div className="space-y-3 max-w-2xl">
                <Field label="Folders to browse (one per line)" hint="Only files inside these folders can be read or written">
                  <textarea
                    className={`${inputCls} font-mono text-xs h-28`}
                    value={rootsText}
                    onChange={(e) => setRootsText(e.target.value)}
                  />
                </Field>
                <Field label="Sprite Lore server URL" hint="Stored in this browser only">
                  <input className={inputCls} value={serverUrl} onChange={(e) => setServerUrlDraft(e.target.value)} />
                </Field>
              </div>
            )}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 border-t border-border px-5 py-3">
          <button type="button" className={btnCls} onClick={resetTab}>
            <RotateCcw className="w-3.5 h-3.5" /> Reset this tab to default
          </button>
          <button type="button" className={btnCls} onClick={resetAll}>
            <RotateCcw className="w-3.5 h-3.5" /> Reset everything
          </button>
          <button type="button" className={btnCls} onClick={undo} disabled={!hasPrev || busy}>
            <Undo2 className="w-3.5 h-3.5" /> Restore previous Default save
          </button>
          <div className="flex-1" />
          {error && <span className="text-xs text-danger">{error}</span>}
          <button type="button" className={btnCls} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            onClick={save}
            disabled={busy || (!dirty && rootsText === settings.roots.join('\n') && serverUrl === getServerUrl())}
            title={editing ? `Saves profile "${editing}" and the shared settings` : 'Saves the Default settings'}
            className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-accent-fg hover:opacity-90 disabled:opacity-50"
          >
            {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
            {editing ? `Save profile` : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
};
