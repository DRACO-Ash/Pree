---
name: accessibility
description: Web accessibility to WCAG 2.2 AA, practical and testable. Use when building or reviewing any interface, an overlay, a form, or a custom widget. Covers semantic HTML first, keyboard operability and visible focus, the dialog and combobox patterns, accessible names, contrast floors, meaning never by colour alone, reduced motion, live regions, forms, the new 2.2 criteria, and a four-pass test method with an audit checklist. Pairs with design-system (tokens and contrast) and frontend-and-rendering (structure).
---

# Accessibility

## Purpose and scope

Accessibility is a build-failing property, not a finishing touch. This skill is the practical WCAG 2.2 AA reference for the interface: the patterns that make a UI operable by keyboard and screen reader, the contrast and motion floors, and how to test them. `design-system` owns tokens and the contrast values; `frontend-and-rendering` owns structure and escaping; this skill owns the accessibility behaviour and its verification. The `design-critic` checks these, and a genuine defect (a contrast failure, an unlabelled control, a keyboard trap) is routed to `engineering-reviewer`, which can block.

## When to use

- Building or changing any UI: chrome, an overlay, a form, a custom widget, an icon button.
- Reviewing interface work for the accessibility floor.

## Principles and patterns

1. **Semantic HTML first.** Use the native element before reaching for ARIA: `<button>` for actions, `<a href>` for navigation, `<nav>/<main>/<header>/<footer>`, one `<main>`, headings in order. Native elements bring keyboard behaviour, focus, and a role for free; ARIA adds semantics, never behaviour. First rule of ARIA: do not use ARIA if a native element fits.
2. **Keyboard operability and visible focus.** Everything operable by mouse is operable by keyboard with no trap. Tab order follows DOM order; avoid positive `tabindex`. Never remove the focus outline without a replacement; use `:focus-visible`. WCAG 2.2 adds 2.4.11 Focus Not Obscured: the focused control must not be hidden behind sticky chrome.
3. **Dialog and modal pattern.** `role="dialog"` with `aria-modal="true"` only when the background is genuinely inert; an accessible name via `aria-labelledby` or `aria-label`; move focus in on open; trap Tab within; mark the background `inert`; Escape closes; restore focus to the opener on close. Native `<dialog>` with `showModal()` provides the trap, top layer, inertness, and Escape for free.
4. **Combobox and listbox pattern.** Input is `role="combobox"` with `aria-expanded` and `aria-controls`; the popup is `role="listbox"`, each option `role="option"` with a unique id. Focus stays on the input; the active option is tracked by `aria-activedescendant` on the input, not by moving DOM focus. You must scroll the active option into view yourself. Selected option carries `aria-selected="true"`.
5. **Accessible names.** Every interactive element and meaningful image needs a name. Prefer visible text, then `aria-labelledby`, then `aria-label` (it overrides visible text, use sparingly). Icon-only buttons get `aria-label`. Meaningful images get descriptive `alt`; decorative images get `alt=""`, never a missing `alt`.
6. **Contrast floors.** Body text 4.5:1; large text (at least 24px, or 18.66px bold) 3:1; UI component boundaries, states, and focus indicators 3:1; meaningful icons 3:1. Thresholds are exact (4.499:1 fails). Disabled controls, pure decoration, and logos are exempt.
7. **Meaning never by colour alone** (1.4.1). Pair colour with text, an icon, or a shape: form errors, chart series, in-text links.
8. **Reduced motion.** Honour `prefers-reduced-motion` for parallax, autoplay, and large transitions; anything moving for more than five seconds can be paused (2.2.2).
9. **Live regions.** Announce async updates (results, toasts, save status) via `aria-live="polite"` or `role="status"` (urgent: `assertive`/`role="alert"`). The region must exist in the DOM before content is injected. Keep messages short; do not move focus to them.
10. **Forms.** Every control has a programmatically associated `<label for>` (a placeholder is not a label); group radios in `<fieldset>/<legend>`; errors identify the field in text, suggest a fix, set `aria-invalid` and `aria-describedby`. WCAG 2.2 adds 3.3.7 Redundant Entry and 3.3.8 Accessible Authentication (no cognitive-test-only step; allow paste and password managers).
11. **WCAG 2.2 pointer criteria.** 2.5.7 Dragging Movements: any drag has a single-pointer alternative. 2.5.8 Target Size: interactive targets at least 24 by 24 CSS pixels, or adequately spaced.

## How to test (four passes, none sufficient alone)

1. **Automated** (a floor, catches roughly a third to a half): axe-core (axe DevTools, `@axe-core/playwright`, `jest-axe`) and the Lighthouse accessibility audit. Catches missing alt and labels, contrast on solid backgrounds, duplicate ids, ARIA misuse, missing language.
2. **Keyboard-only:** unplug the mouse. Reach and operate everything in a logical order with a visible focus indicator; no trap; Escape closes overlays; focus is restored on close; a skip-to-content link is first.
3. **Screen reader:** NVDA with Firefox or Chrome, or VoiceOver with Safari. Navigate by headings, landmarks, links, and fields; confirm roles and names announce; dialogs announce on open; the combobox announces the active option; live regions announce.
4. **Human judgement** (what tools cannot do): is the alt text meaningful, is reading and focus order logical, does a label describe its control, is colour the only cue, does the ARIA pattern actually behave. A clean axe run is necessary, never sufficient.

## Decision rules

- **Native or ARIA?** Native element first; ARIA only where no native element carries the semantics.
- **Custom widget?** Implement the matching ARIA Authoring Practices pattern in full (dialog, combobox, tabs); a half-pattern is worse than none.
- **New or changed colour pair?** Compute its ratio the moment it changes, at the cheapest rung (a calculation or a loop check), in every theme, before the advisory design review, not after. Contrast is arithmetic, so it belongs at author time; a dip found by the reviewer after the colour shipped is a remediation release that a rung-1 check would have folded into the release that introduced it (`resource-discipline`, `design-system`).
- **Async update the user should know about?** Announce it through a pre-existing live region.
- **Found a contrast or unescaped-render or keyboard defect?** Mark it and route to `engineering-reviewer`; it can block.

## Standards (checkable assertions)

- Every interactive element is keyboard-operable with a visible focus indicator and no trap.
- Overlays follow the dialog pattern: named, focus moved in and trapped, background inert, Escape closes, focus restored.
- Custom comboboxes wire `role/aria-expanded/aria-controls/aria-activedescendant` and scroll the active option into view.
- Every control and meaningful image has an accessible name; decorative images use `alt=""`.
- Text meets 4.5:1, large text and UI components and focus indicators meet 3:1, in every theme shipped; no meaning by colour alone. The ratio for any changed pair is computed at author time, in the loop, not left for the advisory reviewer to catch after it ships.
- `prefers-reduced-motion` is honoured; targets are at least 24 by 24 pixels or spaced; drag has a pointer alternative.
- The four test passes were run, including a keyboard pass and a screen-reader pass.

## Failure modes and remedies

- **`outline:none` with no replacement.** The most common AA failure. Fix: `:focus-visible` with a 3:1 indicator.
- **`aria-modal` over a non-inert background.** Fix: mark the background `inert` (or use native `<dialog>`), or drop the modal claim.
- **Combobox where the input never announces the active option.** Fix: set `aria-activedescendant` to the option id and scroll it into view.
- **Colour-only state.** Fix: add text, an icon, or a shape.
- **Clean axe run treated as done.** Fix: run the keyboard and screen-reader passes; automated tools miss meaning, order, and behaviour.

## Verification

Automated (axe/Lighthouse) clean; a keyboard-only pass reaches and operates everything with visible focus and no trap; a screen-reader pass announces roles, names, dialogs, the combobox active option, and live updates; a human pass confirms meaningful alt, logical order, non-colour cues, and correct widget behaviour; contrast pairs meet the floor in every theme. The audit checklist below is the gate.

## Audit checklist

- Semantics: native elements, one `<main>`, ordered headings, page `lang` and `<title>`.
- Keyboard: all operable, no trap, DOM-order tab, visible `:focus-visible`, focus not obscured, skip link.
- Patterns: dialog (modal, named, focus in/trapped, inert, Escape, restore); combobox (roles, `aria-activedescendant`, active option scrolled in).
- Names and images: every control named; meaningful `alt`; decorative `alt=""`.
- Perceivable: body 4.5:1, large and UI and focus 3:1; no colour-only meaning; reduced motion honoured.
- Dynamic: async updates announced via a pre-existing live region.
- Forms: associated labels, fieldset/legend, error text with `aria-describedby`/`aria-invalid`, no redundant entry.
- Pointer: drag has a single-pointer alternative; targets 24 by 24 or spaced.
- Passes done: axe/Lighthouse, keyboard-only, screen reader, human judgement.

## Glossary

- **WCAG 2.2 AA:** the accessibility conformance level this standard meets.
- **Focus trap:** keeping Tab within an open modal; **inert:** marking background content unfocusable and hidden from assistive tech.
- **`aria-activedescendant`:** the attribute that tracks the active option while focus stays on a combobox input.
- **Live region:** an element whose changes a screen reader announces (`aria-live`, `role="status"`, `role="alert"`).
- **Accessible name:** the name a screen reader announces for a control.
- Other terms: `glossary`, `design-system`.

## Provenance

Authored from the W3C WCAG 2.2 understanding documents and the ARIA Authoring Practices dialog and combobox patterns, the contrast minimums (1.4.3, 1.4.11), the new 2.2 criteria (focus not obscured, target size, dragging movements, redundant entry, accessible authentication), and the four-pass test method, consolidating the accessibility floor previously spread through `design-system` and `frontend-and-rendering`.

## Field lesson: disclosure must not orphan a link, and one-of-N is a radiogroup

Two lessons from the field. First, if a skill level or other state hides a section, every link that points at it must hide too, or the section must stay reachable. A live link to a hidden target scrolls to nothing and breaks the journey. Second, a choice of one option from a small set, such as a skill-level switch or an archetype toggle, is a radiogroup: give it `role="radiogroup"` with `role="radio"` and `aria-checked` children, a single roving tab stop (`tabindex="0"` on the active radio, `-1` on the rest), and arrow-key movement. Use the same pattern wherever the same state is set twice, so the two controls behave alike.

A third lesson, on when contrast is checked. Across one project three separate releases existed only to fix a WCAG AA contrast dip that the advisory reviewer found after the colour had already shipped: a filter pill's label on a lightened accent, a close glyph, and an amber eyebrow over a dark banner scrim. Each was arithmetic, and each would have been caught the moment the colour changed by a ratio computation at author time. The lesson: contrast is not a review finding, it is a build input. When you pick or change a colour, compute the ratio then, in the loop, in every theme; the advisory design review is for judgement (does this read as one calm family), not for measuring numbers the loop should already hold.
