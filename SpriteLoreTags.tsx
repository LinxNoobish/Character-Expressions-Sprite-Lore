import React, { useState } from 'react';
import { Plus, X } from 'lucide-react';

interface TagChipsProps {
  tags: string[];
  selected?: string[];
  onToggle?: (tag: string) => void;
  onRemove?: (tag: string) => void;
  onAdd?: (tag: string) => void;
  addPlaceholder?: string;
  size?: 'sm' | 'xs';
}

export const TagChips: React.FC<TagChipsProps> = ({
  tags,
  selected,
  onToggle,
  onRemove,
  onAdd,
  addPlaceholder = 'add tag',
  size = 'sm',
}) => {
  const [draft, setDraft] = useState('');
  const pad = size === 'xs' ? 'px-1.5 py-0.5 text-[11px]' : 'px-2 py-0.5 text-xs';

  const commit = () => {
    const parts = draft
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean);
    parts.forEach((t) => onAdd?.(t));
    setDraft('');
  };

  return (
    <div className="flex flex-wrap items-center gap-1">
      {tags.map((tag) => {
        const on = selected?.includes(tag);
        return (
          <span
            key={tag}
            className={`inline-flex items-center gap-1 rounded-full border transition-colors ${pad} ${
              on
                ? 'bg-accent text-accent-fg border-accent'
                : 'bg-surface text-fg-muted border-border hover:border-accent/60 hover:text-fg'
            } ${onToggle ? 'cursor-pointer' : ''}`}
            onClick={onToggle ? () => onToggle(tag) : undefined}
          >
            {tag}
            {onRemove && (
              <button
                type="button"
                className="opacity-60 hover:opacity-100"
                onClick={(e) => {
                  e.stopPropagation();
                  onRemove(tag);
                }}
                aria-label={`remove ${tag}`}
              >
                <X className="w-3 h-3" />
              </button>
            )}
          </span>
        );
      })}
      {onAdd && (
        <span className={`inline-flex items-center gap-1 rounded-full border border-dashed border-border ${pad}`}>
          <Plus className="w-3 h-3 text-fg-subtle" />
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                commit();
              }
            }}
            onBlur={() => draft.trim() && commit()}
            placeholder={addPlaceholder}
            className="bg-transparent outline-none text-fg w-24 placeholder:text-fg-subtle"
          />
        </span>
      )}
    </div>
  );
};
