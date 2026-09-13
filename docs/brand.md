# Brand

Mpay's visual identity is deliberately quiet: a payment engine should feel like a bank vault, not a carnival. The wordmark and UI lead; the logo mark is a small, precise element that works at 16px (favicon) and on a checkout page header.

## Name

**Mpay** — lowercase "p", capital M. One word. When written in prose: "Mpay", never "MPay" or "mPay".

## Logo concept directions

Pick one direction and execute it well. All three are built to survive at favicon size.

### Direction A — "The Stamp" (recommended)

A **payment seal**: a rounded square containing a bold, geometric **M** whose middle vertex extends into a downward checkmark stroke — reading as both "M" and "✓ paid". The checkmark vertex is the only flourish; everything else is orthogonal.

- Geometry: 24×24 grid, stroke weight 3px (scaled), 6px corner radius on the container
- The M's strokes should be straight lines with mitered joins, no curves
- Test: does it read at 16px? If the checkmark vertex disappears, thicken it, don't add detail

### Direction B — "The Ledger Line"

Three horizontal bars of increasing length (ledger entries), the middle bar terminating in a payment-checkmark. Reads as "records reconciling". Strong for the reconciliation/reliability story, weaker as an "M".

### Direction C — "The Vault Slot"

A circle with a horizontal slot (a coin entering a vault) that doubles as a stylized "P". Minimal, but the least distinctive of the three.

## Color palette

| Role | Hex | Usage |
|---|---|---|
| **Indigo** | `#4F46E5` | Primary accent: logo mark, buttons, links, active states. The brand color. |
| Indigo dark | `#3730A3` | Gradients, hover states |
| Indigo tint | `#EEF0FE` | Chip backgrounds, selected rows |
| **Mint** | `#0A7D38` | Success semantics only: SETTLED, paid states. Never decorative. |
| Mint tint | `#DCF5E5` | Success backgrounds |
| Amber | `#8A6100` on `#FFF3D6` | Pending/waiting states |
| Red | `#B02A2A` on `#FDE3E3` | Failure/expiry only |
| Ink | `#161B22` | Text |
| Slate | `#62708A` | Secondary text, captions |
| Surface | `#FFFFFF` / `#EEF0F4` | Cards / page background (light mode) |
| Dark surfaces | `#171C24` / `#0E1116` | Dark mode card / background |

Rules: Indigo is the only decorative color. Green appears **only** when money is confirmed. The checkout page must never use more than one semantic color at a time. Both light and dark modes are first-class; the logo must work on `#4F46E5` (white mark), white, and `#171C24`.

## Typography

- **UI/product:** system font stack (`system-ui, -apple-system, "Segoe UI", Roboto`) — Mpay renders inside payment flows where third-party font loading is a liability, so the identity is carried by color and weight, not a typeface.
- **Wordmark (if you set one):** a geometric sans — Poppins SemiBold or Manrope ExtraBold are the right weight class. Tighten letter-spacing slightly (−1%). Avoid monospace for the wordmark; monospace is reserved for addresses and hashes.
- Amounts on the checkout page are ExtraBold (800) with −0.5px letter-spacing; everything else is Regular/Medium.

## Logo usage rules

1. Clear space = the height of the "M" on all sides. Nothing enters it.
2. Minimum sizes: 16px (favicon/app icon), 24px (inline header), 120px (print). Below 24px, use the mark alone, never mark + wordmark.
3. The mark may be white on Indigo, Indigo on white, or Ink on dark. No other colorways — in particular, never render the mark in Mint (green is semantic).
4. No drop shadows, gradients (a subtle Indigo→Indigo-dark vertical gradient is permitted inside the mark's container), outlines, or 3D.
5. Next to the wordmark: mark 24px, wordmark 17px SemiBold, 8px gap, baseline-aligned.

## Favicon / app icon

Direction A at 32×32 and 16×16: Indigo rounded square, white M-checkmark. Test legibility against both light and dark browser chrome.

## Voice

Same quiet as the visuals. Short declarative sentences. Money amounts are always exact and always visible. No exclamation marks on anything a payer sees — trust is communicated by precision.
