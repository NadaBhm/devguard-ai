import { useMemo } from "react"
import { tokenizeHcl, type HclTokenKind } from "../../lib/hcl"

const TOKEN_CLASS: Record<HclTokenKind, string> = {
  comment: "italic text-faint",
  string: "text-accent",
  keyword: "text-low",
  "block-type": "font-semibold text-foreground",
  number: "text-foreground",
  punctuation: "text-muted",
  identifier: "text-foreground",
  plain: "text-foreground",
}

export function HclViewer({ code }: { code: string }) {
  const tokens = useMemo(() => tokenizeHcl(code), [code])

  return (
    <pre className="max-h-[480px] overflow-auto p-4 font-mono text-[12.5px] leading-relaxed">
      <code>
        {tokens.map((token, i) => (
          <span key={i} className={TOKEN_CLASS[token.kind]}>
            {token.text}
          </span>
        ))}
      </code>
    </pre>
  )
}
