'use client';

import { useEffect } from 'react';
import { usePathname } from 'next/navigation';
import { useIdentity } from '@/hooks/useIdentity';

// Othos: the tab title and icon follow personal.json's assistant name and logo.
// With nothing set they stay "Othos" and app/icon.svg (the green chip).
export function IdentityHead() {
  const { name, logo } = useIdentity();
  const suffix = usePathname()?.startsWith('/code') ? ' code' : '';
  useEffect(() => {
    document.title = name + suffix;
  }, [name, suffix]);
  useEffect(() => {
    if (!logo) return;
    for (const link of document.querySelectorAll<HTMLLinkElement>('link[rel~="icon"]')) {
      link.removeAttribute('type');
      link.removeAttribute('sizes');
      link.href = logo;
    }
  }, [logo]);
  return null;
}
