import type { SVGProps } from 'react';

// Othos: The Smithy's logo (a hammer tilted over an anvil, two sparks) as a line icon,
// drawn like the lucide icons beside it on the call's control bar (24x24, 2px stroke,
// currentColor), so it takes the button's colour.
export function SmithyIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      {...props}
    >
      {/* the anvil: pointed horn on the left, flat face, narrow waist, wide foot */}
      <path d="M1.5 14h19v2.25l-3.75.5-1.25 2 3 2.75H6.5l3-2.75-1.25-2L5 16.25z" />
      {/* the hammer: a wide head just above the anvil, handle up to the right */}
      <g transform="rotate(35 12 9)">
        <rect x="7" y="7.5" width="10" height="4" rx="0.6" />
        <path d="M12 8V1.25" strokeWidth="1.5" />
      </g>
      {/* two sparks */}
      <circle cx="3.75" cy="10.75" r="0.9" fill="currentColor" stroke="none" />
      <circle cx="20.25" cy="11.25" r="0.9" fill="currentColor" stroke="none" />
    </svg>
  );
}
