/** 门户图标：统一 24 栅格、1.7 stroke、currentColor 继承 token。不引图标库。 */
interface IconProps {
  size?: number;
  className?: string;
}

function base(size: number) {
  return {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.7,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
    focusable: false,
  };
}

export function IconSpark({ size = 18, className }: IconProps) {
  return (
    <svg {...base(size)} className={className}>
      <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" />
      <path d="M18.5 15.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z" />
    </svg>
  );
}

export function IconTablet({ size = 22, className }: IconProps) {
  return (
    <svg {...base(size)} className={className}>
      <rect x="4" y="2.5" width="16" height="19" rx="2.6" />
      <path d="M10.5 18.4h3" />
      <circle cx="12" cy="10" r="2.6" />
      <path d="M8.6 15.2c.7-1.4 1.9-2.1 3.4-2.1s2.7.7 3.4 2.1" />
    </svg>
  );
}

export function IconConsole({ size = 22, className }: IconProps) {
  return (
    <svg {...base(size)} className={className}>
      <rect x="2.5" y="4" width="19" height="13" rx="2" />
      <path d="M8 20.5h8M12 17v3.5" />
      <path d="M6 8.2h5M6 11.2h8M6 14h3" />
    </svg>
  );
}

export function IconPhone({ size = 22, className }: IconProps) {
  return (
    <svg {...base(size)} className={className}>
      <rect x="6.5" y="2.5" width="11" height="19" rx="2.4" />
      <path d="M10.6 5.4h2.8" />
      <path d="M9.4 10.4h5.2M9.4 13.2h5.2M9.4 16h3" />
    </svg>
  );
}

export function IconArrowRight({ size = 16, className }: IconProps) {
  return (
    <svg {...base(size)} className={className}>
      <path d="M4.5 12h14M13 6.5l5.5 5.5-5.5 5.5" />
    </svg>
  );
}

export function IconArrowDown({ size = 16, className }: IconProps) {
  return (
    <svg {...base(size)} className={className}>
      <path d="M12 4.5v14M6.5 13l5.5 5.5 5.5-5.5" />
    </svg>
  );
}

export function IconPlay({ size = 16, className }: IconProps) {
  return (
    <svg {...base(size)} className={className}>
      <path d="M7.5 4.8l11 7.2-11 7.2z" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function IconPause({ size = 16, className }: IconProps) {
  return (
    <svg {...base(size)} className={className}>
      <rect x="7" y="5" width="3.6" height="14" rx="1.1" fill="currentColor" stroke="none" />
      <rect x="13.4" y="5" width="3.6" height="14" rx="1.1" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function IconReset({ size = 16, className }: IconProps) {
  return (
    <svg {...base(size)} className={className}>
      <path d="M4 12a8 8 0 1 0 2.6-5.9" />
      <path d="M4 4.5V10h5.5" />
    </svg>
  );
}

export function IconDownload({ size = 16, className }: IconProps) {
  return (
    <svg {...base(size)} className={className}>
      <path d="M12 4v10.5M7.5 10.5L12 15l4.5-4.5" />
      <path d="M4.5 18.5h15" />
    </svg>
  );
}

export function IconCopy({ size = 15, className }: IconProps) {
  return (
    <svg {...base(size)} className={className}>
      <rect x="8.5" y="8.5" width="11" height="11" rx="2" />
      <path d="M15.5 5.5h-9a2 2 0 0 0-2 2v9" />
    </svg>
  );
}

export function IconCheck({ size = 15, className }: IconProps) {
  return (
    <svg {...base(size)} className={className}>
      <path d="M4.5 12.5l5 5 10-11" />
    </svg>
  );
}

export function IconShield({ size = 18, className }: IconProps) {
  return (
    <svg {...base(size)} className={className}>
      <path d="M12 3l7.5 3v6c0 4.4-3.1 8.2-7.5 9.5C7.6 20.2 4.5 16.4 4.5 12V6z" />
      <path d="M9 12l2.2 2.2L15.4 10" />
    </svg>
  );
}

export function IconInfo({ size = 18, className }: IconProps) {
  return (
    <svg {...base(size)} className={className}>
      <circle cx="12" cy="12" r="8.6" />
      <path d="M12 11v5.4M12 7.9v.2" />
    </svg>
  );
}

export function IconLayers({ size = 18, className }: IconProps) {
  return (
    <svg {...base(size)} className={className}>
      <path d="M12 3.5l8.5 4.3-8.5 4.3L3.5 7.8z" />
      <path d="M3.5 12.2l8.5 4.3 8.5-4.3" />
      <path d="M3.5 16.4l8.5 4.3 8.5-4.3" />
    </svg>
  );
}

export function IconPulse({ size = 18, className }: IconProps) {
  return (
    <svg {...base(size)} className={className}>
      <path d="M2.8 12.4h4l2.2-5.6 3.4 11 2.4-5.4h6.4" />
    </svg>
  );
}