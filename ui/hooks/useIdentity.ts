'use client';

import { useEffect, useState } from 'react';

// Othos: the assistant's name, colour and logo (from /api/identity, i.e. personal.json).
// Fetched once per page; until it answers, the defaults ("Othos", no colour, no logo).
export type Identity = { name: string; color: string; logo: string };

export const DEFAULT_IDENTITY: Identity = { name: 'Othos', color: '', logo: '' };

let cached: Promise<Identity> | null = null;

function load(): Promise<Identity> {
  cached ??= fetch('/api/identity', { cache: 'no-store' })
    .then((r) => r.json())
    .then((j) => ({
      name: typeof j.name === 'string' && j.name ? j.name : 'Othos',
      color: typeof j.color === 'string' ? j.color : '',
      logo: j.hasLogo ? '/api/identity/logo' : '',
    }))
    .catch(() => DEFAULT_IDENTITY);
  return cached;
}

export function useIdentity(): Identity {
  const [id, setId] = useState<Identity>(DEFAULT_IDENTITY);
  useEffect(() => {
    let live = true;
    void load().then((v) => live && setId(v));
    return () => {
      live = false;
    };
  }, []);
  return id;
}

/** "Othos" -> "O T H O S", the spaced capitals used for headings. */
export function spaced(name: string): string {
  return name.toUpperCase().split('').join(' ');
}
