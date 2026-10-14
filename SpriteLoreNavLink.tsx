import React from 'react';
import { BookImage } from 'lucide-react';

export const SpriteLoreNavLink: React.FC = () => (
  <>
    <a
      href="#/sprite-lore"
      className="hidden lg:inline-flex items-center gap-2 px-3 py-1.5 text-sm font-medium text-fg-muted border border-border hover:border-accent/50 hover:text-accent rounded-xl transition-colors"
      title="Sprite Lore: expression sprites, embedded lore"
    >
      <BookImage className="w-4 h-4" />
      Sprite Lore
    </a>
    <a
      href="#/sprite-lore"
      className="lg:hidden p-2 text-fg-muted hover:text-accent hover:bg-accent-soft rounded-lg transition-colors"
      title="Sprite Lore"
      aria-label="Sprite Lore"
    >
      <BookImage className="w-4 h-4" />
    </a>
  </>
);
