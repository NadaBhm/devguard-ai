/**
 * Minimal HCL (Terraform) tokenizer for read-only syntax highlighting.
 *
 * Deliberately hand-rolled instead of pulling in a highlighting library
 * (Prism, highlight.js, ...): the surface we need is narrow (one language,
 * display-only, no editing), and the design system rules for this project
 * ask not to introduce a new UI dependency unless it's actually needed.
 */

export type HclTokenKind =
  | "comment"
  | "string"
  | "keyword"
  | "block-type"
  | "number"
  | "punctuation"
  | "identifier"
  | "plain"

export interface HclToken {
  kind: HclTokenKind
  text: string
}

const KEYWORDS = new Set([
  "true",
  "false",
  "null",
  "for",
  "in",
  "if",
  "else",
])

// The block-type keyword is only highlighted distinctly at the start of a
// statement (after optional leading whitespace), so "resource" used as a
// plain identifier elsewhere in the file isn't mis-colored.
const BLOCK_TYPES = new Set([
  "terraform",
  "provider",
  "resource",
  "data",
  "variable",
  "output",
  "module",
  "locals",
])

const TOKEN_PATTERN =
  /(#[^\n]*|\/\/[^\n]*)|("(?:\\.|[^"\\])*")|(\b\d+(?:\.\d+)?\b)|([{}()[\],.=+\-*/<>!:])|([A-Za-z_][A-Za-z0-9_-]*)|(\s+)/g

export function tokenizeHcl(source: string): HclToken[] {
  const tokens: HclToken[] = []
  let atLineStart = true

  let match: RegExpExecArray | null
  TOKEN_PATTERN.lastIndex = 0
  while ((match = TOKEN_PATTERN.exec(source)) !== null) {
    const [full, comment, string, number, punctuation, word, whitespace] = match

    if (comment) {
      tokens.push({ kind: "comment", text: comment })
    } else if (string) {
      tokens.push({ kind: "string", text: string })
    } else if (number) {
      tokens.push({ kind: "number", text: number })
    } else if (punctuation) {
      tokens.push({ kind: "punctuation", text: punctuation })
    } else if (word) {
      if (KEYWORDS.has(word)) {
        tokens.push({ kind: "keyword", text: word })
      } else if (atLineStart && BLOCK_TYPES.has(word)) {
        tokens.push({ kind: "block-type", text: word })
      } else {
        tokens.push({ kind: "identifier", text: word })
      }
      atLineStart = false
      continue
    } else if (whitespace) {
      tokens.push({ kind: "plain", text: whitespace })
      if (whitespace.includes("\n")) atLineStart = true
      continue
    } else {
      tokens.push({ kind: "plain", text: full })
    }
    atLineStart = false
  }

  return tokens
}
