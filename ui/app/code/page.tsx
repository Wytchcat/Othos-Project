import { Suspense } from 'react';
import { CodeChat } from '@/components/app/code-chat';

// Othos code chat at http://127.0.0.1:3000/code (typed, no voice, no visualizer)
export default function CodePage() {
  return (
    <Suspense>
      <CodeChat />
    </Suspense>
  );
}
