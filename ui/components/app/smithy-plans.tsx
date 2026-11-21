'use client';

import { useCallback, useEffect, useState } from 'react';
import { Streamdown } from 'streamdown';

// Othos code page: The Smithy's build plans waiting for an answer (Nov 14, suggestion 3), with the same
// buttons as The Smithy's Plan window. Answered here, by voice or in the app: whichever is first, the
// others see it's answered (checked every 30 seconds).
type Question = { q: string; default: string };
type Plan = {
  id: number;
  waiting: boolean;
  title: string;
  text: string;
  questions: Question[];
  cost: string;
};

const BTN =
  'rounded-md border bg-[#0e1019] px-3 py-1.5 text-sm hover:bg-[#19182b] disabled:opacity-40';

function PlanCard({ plan, onDone }: { plan: Plan; onDone: (said: string) => void }) {
  const [answers, setAnswers] = useState<string[]>(plan.questions.map((x) => x.default || ''));
  const [other, setOther] = useState('');
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [sureNo, setSureNo] = useState(false);
  const [note, setNote] = useState('');

  // what the user changed, as one reply: each question whose box isn't its default, then the other changes
  const changed = [
    ...plan.questions
      .map((x, i) =>
        answers[i].trim() !== (x.default || '').trim()
          ? `Question ${i + 1} (${x.q}): ${answers[i].trim() || 'no answer'}`
          : ''
      )
      .filter(Boolean),
    ...(other.trim() ? [`Other changes: ${other.trim()}`] : []),
  ].join('\n');

  const reply = async (answer: 'yes' | 'no' | 'answers', buildNow = false) => {
    setBusy(true);
    setNote(
      answer === 'yes' ? 'Sending yes…' : answer === 'no' ? 'Dropping it…' : 'Sending your answers…'
    );
    try {
      const r = await fetch('/api/smithy/plans', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: plan.id, answer, text: changed, buildNow }),
      });
      const j = await r.json();
      if (j.ok) onDone(j.said);
      else {
        setNote(j.said);
        setBusy(false);
      }
    } catch {
      setNote("The Smithy couldn't be reached.");
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-[#403569] bg-[#0b0d14] p-4">
      <div className="flex items-center justify-between gap-2">
        <p className="font-semibold">
          Smithy task {plan.id}: {plan.title.replace(/^the plan for /, 'a plan for ')}
        </p>
        <button className={`${BTN} border-[#403569]`} onClick={() => setOpen(!open)}>
          {open ? 'Hide the plan' : 'Read the plan'}
        </button>
      </div>
      {open && (
        <div className="mt-3 max-h-[50vh] overflow-y-auto rounded-md border border-[#403569] p-3 text-sm">
          <Streamdown>{plan.text}</Streamdown>
        </div>
      )}
      {plan.questions.map((x, i) => (
        <label key={i} className="mt-3 block text-sm">
          <span className="text-muted-foreground">
            Question {i + 1}: {x.q}
          </span>
          <input
            className="mt-1 w-full rounded-md border border-[#403569] bg-[#0e1019] px-3 py-1.5 outline-none focus:border-[#9d7bff]"
            value={answers[i]}
            disabled={busy}
            onChange={(e) => setAnswers(answers.map((a, k) => (k === i ? e.target.value : a)))}
          />
        </label>
      ))}
      <label className="mt-3 block text-sm">
        <span className="text-muted-foreground">Other changes (optional)</span>
        <textarea
          className="mt-1 min-h-[2.5rem] w-full resize-y rounded-md border border-[#403569] bg-[#0e1019] px-3 py-1.5 outline-none focus:border-[#9d7bff]"
          value={other}
          disabled={busy}
          onChange={(e) => setOther(e.target.value)}
        />
      </label>
      {plan.cost && <p className="text-muted-foreground mt-2 text-xs">{plan.cost}</p>}
      <div className="mt-3 flex flex-wrap gap-2">
        <button
          className={`${BTN} border-[#9d7bff]`}
          disabled={busy || !!changed}
          title={
            changed ? 'You changed an answer: use one of the answer buttons, so it isn’t lost' : ''
          }
          onClick={() => void reply('yes')}
        >
          Build it
        </button>
        <button
          className={`${BTN} border-[#403569]`}
          disabled={busy || !changed}
          onClick={() => void reply('answers', true)}
        >
          Build with these answers
        </button>
        <button
          className={`${BTN} border-[#403569]`}
          disabled={busy || !changed}
          onClick={() => void reply('answers')}
        >
          Send for a new plan
        </button>
        {sureNo ? (
          <>
            <button
              className={`${BTN} border-[#e7c368]`}
              disabled={busy}
              onClick={() => void reply('no')}
            >
              Yes, don’t build it
            </button>
            <button
              className={`${BTN} border-[#403569]`}
              disabled={busy}
              onClick={() => setSureNo(false)}
            >
              Keep it
            </button>
          </>
        ) : (
          <button
            className={`${BTN} border-[#403569]`}
            disabled={busy}
            onClick={() => setSureNo(true)}
          >
            Don’t build
          </button>
        )}
      </div>
      {note && <p className="text-muted-foreground mt-2 text-xs">{note}</p>}
    </div>
  );
}

export function SmithyPlans() {
  const [plans, setPlans] = useState<Plan[]>([]);
  const [said, setSaid] = useState('');

  const load = useCallback(async () => {
    try {
      const r = await fetch('/api/smithy/plans', { cache: 'no-store' });
      const j = await r.json();
      setPlans((j.plans || []).filter((p: Plan) => p.waiting));
    } catch {
      /* the page server is restarting: try again next time */
    }
  }, []);

  useEffect(() => {
    const first = setTimeout(() => void load(), 0); // straight away, then every 30 seconds
    const t = setInterval(() => void load(), 30_000);
    return () => {
      clearTimeout(first);
      clearInterval(t);
    };
  }, [load]);

  if (!plans.length && !said) return null;
  return (
    <section className="space-y-3 pb-4">
      {plans.length > 0 && (
        <p className="text-sm text-[#e7c368]">
          The Smithy has {plans.length === 1 ? 'a plan' : `${plans.length} plans`} waiting for your
          answer.
        </p>
      )}
      {plans.map((p) => (
        <PlanCard
          key={p.id}
          plan={p}
          onDone={(s) => {
            setSaid(s);
            setPlans(plans.filter((x) => x.id !== p.id));
          }}
        />
      ))}
      {said && (
        <p className="text-muted-foreground text-sm whitespace-pre-wrap">
          {said}{' '}
          <button className="underline" onClick={() => setSaid('')}>
            OK
          </button>
        </p>
      )}
    </section>
  );
}
