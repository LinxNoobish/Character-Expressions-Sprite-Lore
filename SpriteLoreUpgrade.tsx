import React, { useCallback, useEffect, useState } from 'react';
import { ArrowRight, Check, GitMerge, Loader2, RefreshCw, X } from 'lucide-react';
import { spriteLoreApi } from './SpriteLoreApi';
import type { LoreTemplate, MergeChange, UpgradeReport } from './SpriteLoreApi';

type Choices = Record<string, Record<string, 'mine' | 'new'>>;

const btnCls =
  'inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-xs font-medium text-fg-muted hover:text-fg hover:border-accent/60 transition-colors disabled:opacity-50';
const primaryCls =
  'inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-accent-fg hover:opacity-90 disabled:opacity-50';

const STATUS: Record<MergeChange['status'], [string, string]> = {
  applied: ['applied', 'bg-success-soft text-success-soft-fg'],
  merged: ['merged', 'bg-info-soft text-info-soft-fg'],
  already: ['already in new', 'bg-muted text-fg-subtle'],
  conflict: ['conflict', 'bg-warning-soft text-warning-soft-fg'],
};

const KIND: Record<MergeChange['kind'], string> = {
  insert: 'your added text',
  modify: 'your change',
  delete: 'your deletion',
};

function upgradable(r: UpgradeReport) {
  return (r.status === 'clean' || r.status === 'conflicts') && !!r.merged;
}

const Text: React.FC<{ label: string; text: string; tone?: string }> = ({ label, text, tone }) =>
  text ? (
    <div className="min-w-0">
      <span className="text-[10px] uppercase tracking-wide text-fg-subtle">{label}</span>
      <p className={`whitespace-pre-wrap break-words text-[11px] ${tone ?? 'text-fg-muted'}`}>{text}</p>
    </div>
  ) : null;

export const TemplateUpgradeBanner: React.FC<{
  templates: LoreTemplate[];
  onOpen: () => void;
}> = ({ templates, onOpen }) => {
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => {
    let alive = true;
    const timer = window.setTimeout(() => {
      spriteLoreApi
        .upgradeTemplates(templates, {})
        .then((r) => alive && setCount(r.reports.filter(upgradable).length))
        .catch(() => alive && setCount(null));
    }, 500);
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [templates]);
  if (!count) return null;
  return (
    <div className="flex items-center gap-3 rounded-xl border border-info/40 bg-info-soft px-3 py-2 text-xs text-info-soft-fg">
      <GitMerge className="h-4 w-4 shrink-0" />
      <span className="flex-1">
        {count} of these templates were edited from an older default. Their edits can be carried over to the new
        default text.
      </span>
      <button type="button" className={primaryCls} onClick={onOpen}>
        Review upgrade
      </button>
    </div>
  );
};

export const TemplateUpgrade: React.FC<{
  templates: LoreTemplate[];
  onApply: (next: LoreTemplate[]) => void;
  onClose: () => void;
}> = ({ templates, onApply, onClose }) => {
  const [reports, setReports] = useState<UpgradeReport[] | null>(null);
  const [choices, setChoices] = useState<Choices>({});
  const [use, setUse] = useState<Record<string, boolean>>({});
  const [keepCopy, setKeepCopy] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(
    async (c: Choices) => {
      setBusy(true);
      setError(null);
      try {
        const r = await spriteLoreApi.upgradeTemplates(templates, c);
        setReports(r.reports);
        setUse((u) => {
          const out = { ...u };
          r.reports.forEach((x) => {
            if (out[x.id] === undefined) out[x.id] = upgradable(x);
          });
          return out;
        });
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [templates],
  );

  useEffect(() => {
    run({});
  }, [run]);

  const choose = (tid: string, cid: string, pick: 'mine' | 'new') => {
    const next = { ...choices, [tid]: { ...(choices[tid] ?? {}), [cid]: pick } };
    setChoices(next);
    run(next);
  };

  const apply = () => {
    if (!reports) return;
    const out: LoreTemplate[] = [];
    templates.forEach((t) => {
      const r = reports.find((x) => x.id === t.id);
      if (r && upgradable(r) && use[t.id] && r.merged) {
        out.push({ ...t, system: r.merged.system, user: r.merged.user });
        if (keepCopy) {
          let id = `${t.id}-old`;
          let n = 2;
          while (templates.some((x) => x.id === id) || out.some((x) => x.id === id)) id = `${t.id}-old${n++}`;
          out.push({ ...t, id, name: `${t.name} (before upgrade)` });
        }
      } else {
        out.push(t);
      }
    });
    onApply(out);
    onClose();
  };

  const picked = reports?.filter((r) => upgradable(r) && use[r.id]).length ?? 0;

  return (
    <div className="absolute inset-0 z-10 flex flex-col bg-surface">
      <div className="flex items-center gap-2 border-b border-border px-5 py-3">
        <GitMerge className="h-4 w-4 text-accent" />
        <div className="flex-1">
          <h3 className="text-sm font-semibold text-fg">Upgrade edited templates</h3>
          <p className="text-[11px] text-fg-subtle">
            Your template is compared with the default it started from; each of your edits is replayed on the new
            default at the place its surrounding words and position map to. Nothing is saved until you press Save in
            settings.
          </p>
        </div>
        {busy && <Loader2 className="h-4 w-4 animate-spin text-accent" />}
        <button type="button" onClick={onClose} className="p-1.5 text-fg-muted hover:text-fg" aria-label="Close">
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
        {error && <p className="text-xs text-danger">{error}</p>}
        {reports?.map((r) => {
          const t = templates.find((x) => x.id === r.id);
          const can = upgradable(r);
          const conflicts = r.changes.filter((c) => c.status === 'conflict').length;
          return (
            <div key={r.id} className="rounded-xl border border-border">
              <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
                {can && (
                  <input
                    type="checkbox"
                    checked={!!use[r.id]}
                    onChange={(e) => setUse({ ...use, [r.id]: e.target.checked })}
                  />
                )}
                <span className="text-sm font-semibold text-fg">{t?.name ?? r.name}</span>
                <span className="text-[11px] text-fg-subtle">
                  {r.status === 'current' && 'already on the current default'}
                  {r.status === 'custom' && 'not based on a shipped default; left as is'}
                  {r.status === 'no-successor' && 'its default no longer exists; left as is'}
                  {can && `from ${r.base} · ${r.changes.length} edit(s) · ${conflicts} conflict(s)`}
                </span>
              </div>
              {can && (
                <div className="space-y-2 p-3">
                  {r.changes.length === 0 && (
                    <p className="text-xs text-fg-subtle">No edits found; this becomes the new default text.</p>
                  )}
                  {r.changes.map((c) => (
                    <div key={c.id} className="rounded-lg border border-border p-2">
                      <div className="mb-1 flex flex-wrap items-center gap-2">
                        <span className={`rounded px-1.5 text-[10px] ${STATUS[c.status][1]}`}>{STATUS[c.status][0]}</span>
                        <span className="text-[11px] text-fg-muted">
                          {c.field === 'system' ? 'System prompt' : 'User prompt'} · {KIND[c.kind]}
                          {c.where ? ` · after "${c.where}"` : ''}
                          {c.how ? ` · ${c.how}` : ''}
                        </span>
                        {c.status === 'conflict' && (
                          <span className="ml-auto inline-flex overflow-hidden rounded-lg border border-border">
                            <button
                              type="button"
                              className={`px-2 py-0.5 text-[11px] ${c.choice === 'mine' ? 'bg-accent text-accent-fg' : 'text-fg-muted'}`}
                              onClick={() => choose(r.id, c.id, 'mine')}
                            >
                              Keep mine
                            </button>
                            <button
                              type="button"
                              className={`px-2 py-0.5 text-[11px] ${c.choice === 'new' ? 'bg-accent text-accent-fg' : 'text-fg-muted'}`}
                              onClick={() => choose(r.id, c.id, 'new')}
                            >
                              Use new default
                            </button>
                          </span>
                        )}
                      </div>
                      <div className="grid gap-2 md:grid-cols-[1fr_auto_1fr]">
                        <div className="space-y-1">
                          <Text label="old default" text={c.base} tone="text-fg-subtle line-through decoration-fg-subtle/50" />
                          <Text label="yours" text={c.mine} tone="text-fg" />
                        </div>
                        <ArrowRight className="mt-4 hidden h-4 w-4 text-fg-subtle md:block" />
                        <div className="space-y-1">
                          <Text label="new default" text={c.new} />
                          <Text label="result" text={c.result ?? ''} tone="text-accent" />
                        </div>
                      </div>
                    </div>
                  ))}
                  {r.merged && (
                    <details className="rounded-lg border border-border p-2">
                      <summary className="cursor-pointer text-[11px] text-fg-muted">Full upgraded template</summary>
                      <p className="mt-2 text-[10px] uppercase tracking-wide text-fg-subtle">system</p>
                      <pre className="whitespace-pre-wrap break-words font-mono text-[11px] text-fg-muted">
                        {r.merged.system}
                      </pre>
                      <p className="mt-2 text-[10px] uppercase tracking-wide text-fg-subtle">user</p>
                      <pre className="whitespace-pre-wrap break-words font-mono text-[11px] text-fg-muted">
                        {r.merged.user}
                      </pre>
                    </details>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="flex flex-wrap items-center gap-3 border-t border-border px-5 py-3">
        <label className="flex items-center gap-1.5 text-xs text-fg-muted">
          <input type="checkbox" checked={keepCopy} onChange={(e) => setKeepCopy(e.target.checked)} />
          Keep each old version as a "(before upgrade)" copy
        </label>
        <div className="flex-1" />
        <button type="button" className={btnCls} disabled={busy} onClick={() => run(choices)}>
          <RefreshCw className="h-3.5 w-3.5" /> Re-run
        </button>
        <button type="button" className={primaryCls} disabled={busy || !picked} onClick={apply}>
          <Check className="h-3.5 w-3.5" /> Apply {picked} upgrade{picked === 1 ? '' : 's'}
        </button>
      </div>
    </div>
  );
};
