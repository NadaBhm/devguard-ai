import type { SVGProps } from "react"

type IconProps = SVGProps<SVGSVGElement>

function Base({
  children,
  strokeWidth = 1.7,
  ...props
}: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  )
}

export const IconShield = (p: IconProps) => (
  <Base {...p}>
    <path d="M8 1.5 13 3.5v4c0 3.2-2.1 5.3-5 7-2.9-1.7-5-3.8-5-7v-4l5-2Z" />
    <path d="m6 8 1.4 1.4L10.3 6.2" />
  </Base>
)

export const IconGauge = (p: IconProps) => (
  <Base {...p}>
    <path d="M8 12.5a6 6 0 1 1 6-6" />
    <path d="M8 10 11 5.8M9.8 12.4a1.6 1.6 0 0 1-2.1-2.1" />
  </Base>
)

export const IconLayout = (p: IconProps) => (
  <Base {...p}>
    <rect x="1.8" y="2" width="12.4" height="12" rx="1.5" />
    <path d="M1.8 6.2h12.4M6.4 6.2v7.8" />
  </Base>
)

export const IconRepo = (p: IconProps) => (
  <Base {...p}>
    <rect x="2" y="2.5" width="9" height="11" rx="1.5" />
    <path d="M5.5 5h2M5.5 7.5h2M5.5 10h1.5" />
    <path d="M11 5.5h2.5v7.5a1.5 1.5 0 0 1-1.5 1.5 1.5 1.5 0 0 1-1.5-1.5Z" />
  </Base>
)

export const IconApproval = (p: IconProps) => (
  <Base {...p}>
    <path d="M4.5 8.3 6.6 10.4 11.5 5.5" />
    <path d="M8 1.5c3 1.5 5 3.4 5.5 6.5-.5 3.1-2.5 5-5.5 6.5-3-1.5-5-3.4-5.5-6.5.5-3.1 2.5-5 5.5-6.5Z" />
  </Base>
)

export const IconChart = (p: IconProps) => (
  <Base {...p}>
    <path d="M2 13.5h12" />
    <rect x="3" y="9" width="2.2" height="4.5" rx="0.4" />
    <rect x="6.9" y="6" width="2.2" height="7.5" rx="0.4" />
    <rect x="10.8" y="3.5" width="2.2" height="10" rx="0.4" />
  </Base>
)

export const IconCost = (p: IconProps) => (
  <Base {...p}>
    <path d="M2.5 5.5h11v7a1.5 1.5 0 0 1-1.5 1.5H4a1.5 1.5 0 0 1-1.5-1.5v-7Z" />
    <path d="M6 5.5 8 2.5l2 3" />
    <path d="M5.5 10.75a2.25 2.25 0 1 0 4.5 0 2.25 2.25 0 0 0-4.5 0Z" />
  </Base>
)

export const IconFile = (p: IconProps) => (
  <Base {...p}>
    <path d="M5 1.5 2.5 4v10A1.5 1.5 0 0 0 4 15.5h8a1.5 1.5 0 0 0 1.5-1.5V4L11 1.5H5Z" />
    <path d="M2.5 4h2.75A.75.75 0 0 0 6 3.25V.5M5.5 8h5M5.5 11h3.5" />
  </Base>
)

export const IconDeploy = (p: IconProps) => (
  <Base {...p}>
    <path d="M3.5 8.6a.75.75 0 1 0 1.06-1.06.75.75 0 0 0-1.06 1.06Z" />
    <path d="M3.5 8.6 12 4M5.4 7.5 12 12" opacity="0" />
    <path d="M8 3.3a4.8 4.8 0 1 1-4.7 6.2" />
    <path d="M7.9 1.5v3l1.6-1.2L7.9 1.5Z" />
    <path d="M12.5 12.3a.75.75 0 1 0 1.06-1.06.75.75 0 0 0-1.06 1.06Z" />
  </Base>
)

export const IconChat = (p: IconProps) => (
  <Base {...p}>
    <path d="M1.8 8A6.5 6.5 0 0 1 7.8 2.5 6.7 6.7 0 0 1 14.5 8v0a6.7 6.7 0 0 1-6.7 5.5c-1.1 0-2.1-.2-3-.7l-3.5.9 1-3.2A6.2 6.2 0 0 1 1.8 8Z" />
    <path d="M5.5 8.4a.65.65 0 1 0-.01 0M8.4 8.4a.65.65 0 1 0-.01 0M11.3 8.4a.65.65 0 1 0-.01 0" />
  </Base>
)

export const IconBell = (p: IconProps) => (
  <Base {...p}>
    <path d="M3.5 10.5h9M5 10.5V8a3 3 0 0 1 6 0v2.5M7.3 12.8h1.4" />
  </Base>
)

export const IconAlert = (p: IconProps) => (
  <Base {...p}>
    <path d="M2.6 12.5 8 2.5l5.4 10H2.6Z" />
    <path d="M8 6.5v2.8M8 11.3v.1" />
  </Base>
)

export const IconSettings = (p: IconProps) => (
  <Base {...p}>
    <path d="M8 9.8A1.8 1.8 0 1 0 8 6.2a1.8 1.8 0 0 0 0 3.6Z" />
    <path d="M13 8.8v-1.6l-1.5-.5a4.8 4.8 0 0 0-.5-1.1l.6-1.5-1.1-1.1-1.5.6a4.8 4.8 0 0 0-2-.7L6 1.5H4.4l.6 1.4a4.8 4.8 0 0 0-1.6 1L2 3.2l-1.1 1.1.7 1.4a4.8 4.8 0 0 0-.6 1.8H.3v1.5H1c0 .8.3 1.4.6 1.9l-.7 1.5 1.1 1.1 1.5-.6c.5.4 1 .6 1.7.8l-.6 1.4H7V14c.6 0 1.2-.2 1.7-.4l1.4.7 1.1-1.1-.7-1.5c.4-.5.6-1 .8-1.8h1.7Z" />
  </Base>
)

export const IconUsers = (p: IconProps) => (
  <Base {...p}>
    <circle cx="6" cy="6.2" r="2.2" />
    <path d="M1.5 12.8a4.5 4.5 0 0 1 9 0" />
    <path d="M10.2 4.2a2.2 2.2 0 0 1 0 4M11.6 13a4 4 0 0 0-1.4-3" />
  </Base>
)

export const IconPlus = (p: IconProps) => (
  <Base {...p}>
    <path d="M8 3.5v9M3.5 8h9" />
  </Base>
)

export const IconArrowRight = (p: IconProps) => (
  <Base {...p}>
    <path d="M1.8 8h12M9.5 4 13 8l-3.5 4" />
  </Base>
)

export const IconExternal = (p: IconProps) => (
  <Base {...p}>
    <path d="M10.5 2.5H13.5v3M13.5 2.5 8 8" />
    <path d="M13 9.2v2.8a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h2.8" />
  </Base>
)

export const IconCheck = (p: IconProps) => (
  <Base {...p}>
    <path d="m3 8.3 3.2 3.2L13 4.5" />
  </Base>
)

export const IconX = (p: IconProps) => (
  <Base {...p}>
    <path d="m3.5 3.5 9 9M12.5 3.5l-9 9" />
  </Base>
)

export const IconChevronDown = (p: IconProps) => (
  <Base {...p}>
    <path d="m3 6 5 5 5-5" />
  </Base>
)

export const IconSearch = (p: IconProps) => (
  <Base {...p}>
    <circle cx="6.8" cy="6.8" r="4.3" />
    <path d="m10.5 10.5 3.2 3.2" />
  </Base>
)

export const IconClock = (p: IconProps) => (
  <Base {...p}>
    <circle cx="8" cy="8" r="6.5" />
    <path d="M8 4.5V8l2.5 1.5" />
  </Base>
)

export const IconLock = (p: IconProps) => (
  <Base {...p}>
    <rect x="3.5" y="7" width="9" height="6.5" rx="1" />
    <path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" />
  </Base>
)

export const IconDownload = (p: IconProps) => (
  <Base {...p}>
    <path d="M8 2v8.5M4.8 7.3 8 10.5l3.2-3.2" />
    <path d="M2.5 13h11" />
  </Base>
)

export const IconCopy = (p: IconProps) => (
  <Base {...p}>
    <rect x="6" y="6" width="7.5" height="7.5" rx="1.3" />
    <path d="M3.8 9.7H3a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1h5.7a1 1 0 0 1 1 1v.8" />
  </Base>
)

export const IconLogout = (p: IconProps) => (
  <Base {...p}>
    <path d="M9.5 2.5H4a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1h5.5" />
    <path d="M11 10.5 13.5 8 11 5.5M6.5 8h7" />
  </Base>
)

export const IconRefresh = (p: IconProps) => (
  <Base {...p}>
    <path d="M13.5 6.2A5.8 5.8 0 1 0 14 9" />
    <path d="M13.5 2.5v4h-4" />
  </Base>
)

export const IconWaves = (p: IconProps) => (
  <Base {...p}>
    <path d="M2 6.5c1.7-1.7 3.3-1.7 5 0s3.3 1.7 5 0 1.7-1.2 2 .2v3c-1.7 1.7-3.3 1.7-5 0s-3.3-1.7-5 0-1.7 1.7-2 1.7 1.7-1.7 0-3.2" />
  </Base>
)