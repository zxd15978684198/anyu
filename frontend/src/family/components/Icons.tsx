/**
 * 家属端内联图标（W4）。
 *
 * 一律 `aria-hidden="true"` + `focusable="false"`：图标只做视觉提示，
 * 语义由外层 `aria-label`（纯图标按钮）或相邻文字承担。
 * 不引图标库（工程约束：family 不新增依赖）。
 */

interface IconProps {
  size?: number;
}

export function ArrowLeftIcon({ size = 24 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">
      <path d="M15 5 8 12l7 7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function ChevronRightIcon({ size = 20 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">
      <path d="m9 5 7 7-7 7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function BellIcon({ size = 24 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">
      <path
        d="M6 10a6 6 0 1 1 12 0c0 3 .6 4.6 1.4 5.6.3.4 0 1-.5 1H5.1c-.5 0-.8-.6-.5-1C5.4 14.6 6 13 6 10Z"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
      <path d="M10 19.5a2.2 2.2 0 0 0 4 0" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

export function SignOutIcon({ size = 24 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">
      <path d="M14 4h3.5A2.5 2.5 0 0 1 20 6.5v11a2.5 2.5 0 0 1-2.5 2.5H14" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
      <path d="M10 8 6 12l4 4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M6 12h9" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

export function SpeakerIcon({ size = 24 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">
      <path
        d="M4 10v4a1 1 0 0 0 1 1h2.6l4.1 3.2a.8.8 0 0 0 1.3-.6V6.4a.8.8 0 0 0-1.3-.6L7.6 9H5a1 1 0 0 0-1 1Z"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
      <path d="M17 9.2a4 4 0 0 1 0 5.6" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
      <path d="M19.3 6.8a7.4 7.4 0 0 1 0 10.4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

export function DocIcon({ size = 40 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 48 48" fill="none" aria-hidden="true" focusable="false">
      <path d="M12 8h16l8 8v24a2 2 0 0 1-2 2H12a2 2 0 0 1-2-2V10a2 2 0 0 1 2-2Z" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
      <path d="M28 8v8h8" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
      <path d="M16 25h14M16 31h9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}
