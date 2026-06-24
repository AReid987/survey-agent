# Aigency · Brand Spec v2

> Updated: 2026-05-09 Source: brand-spec.md v1 + grill-me Q1-Q11 + cult-ui
> reference analysis Token file: `design-tokens.css` (`--aig-*` prefix, all
> OKLCH)

## Visual Metaphor

> **Spacecraft instrument panel** — drafting table on a spacecraft, designed by
> agents who surpassed human design comprehension. Every surface, texture, and
> animation reinforces: "this was not made by a human hand, but it serves human
> understanding."

The window shows deep space. The glass is textured — dithered, degraded, like
old CRT graphing paper. Instrument lights from behind the viewer cast faint warm
reflections on the glass. Grid lines provide structure like architectural
blueprints. Content is written ON the glass, not floating above it.

---

## Core Assets

### Logo

- Main: `assets/aigency-1c-transparent.png` (1024x1024, RGBA, dark bg)
- Alt: `assets/aigency-1c-original.png` (1024x1024, RGB, light bg fallback)
- Usage: nav bar top-left, hero background watermark, footer
- Deformation: none — no stretch, no recolor, no stroke

---

## Color System (OKLCH only — no hex)

### Surface Layers (void depth)

| Token               | OKLCH                   | Role                            |
| ------------------- | ----------------------- | ------------------------------- |
| `--aig-void-deep`   | `oklch(0.10 0.015 250)` | Deepest background, main canvas |
| `--aig-void-base`   | `oklch(0.13 0.015 250)` | Base background                 |
| `--aig-void-raised` | `oklch(0.17 0.015 250)` | Cards, panels                   |
| `--aig-void-glass`  | `oklch(0.20 0.020 250)` | Glass texture layer             |

All surfaces run cool blue-grey (hue 250). No warm surfaces — warmth is reserved
for accents.

### Text Hierarchy

| Token                  | OKLCH                   | Role               |
| ---------------------- | ----------------------- | ------------------ |
| `--aig-text-primary`   | `oklch(0.90 0.010 250)` | Headings, key text |
| `--aig-text-body`      | `oklch(0.78 0.010 250)` | Body copy          |
| `--aig-text-secondary` | `oklch(0.60 0.010 250)` | Muted, labels      |
| `--aig-text-tertiary`  | `oklch(0.45 0.010 250)` | Disabled, hints    |

### Warm Accent (amber — the human warmth in the machine)

| Token                    | OKLCH                        | Role                                      |
| ------------------------ | ---------------------------- | ----------------------------------------- |
| `--aig-accent-warm`      | `oklch(0.75 0.150 65)`       | Key numbers, active states, particle glow |
| `--aig-accent-warm-dim`  | `oklch(0.55 0.100 65)`       | Muted warm                                |
| `--aig-accent-warm-glow` | `oklch(0.75 0.150 65 / 0.3)` | Halo, ambient glow                        |

### Signal Colors (verdicts, status)

| Token                      | OKLCH                   | Role                 |
| -------------------------- | ----------------------- | -------------------- |
| `--aig-signal-go`          | `oklch(0.72 0.170 160)` | Go verdicts          |
| `--aig-signal-conditional` | `oklch(0.75 0.150 65)`  | Conditional verdicts |
| `--aig-signal-avoid`       | `oklch(0.60 0.200 25)`  | Avoid verdicts       |
| `--aig-signal-critical`    | `oklch(0.55 0.220 25)`  | Critical insights    |
| `--aig-signal-highlight`   | `oklch(0.65 0.170 300)` | Special emphasis     |

### Forbidden Colors

- Pure white `#fff` — destroys deep space feel
- High-saturation blue `#3b82f6` — too generic/modern
- Purple gradient backgrounds — AI slop signature
- Any hex value — use OKLCH exclusively

---

## Borders & Structure (Sharp Only — No Rounded Corners)

**Border radius: ALL ZERO.** Cards, buttons, inputs, panels — all sharp
rectangles. This is a spacecraft instrument panel, not a mobile app.

### Fence Pattern (meter.com inspired — 1px structural lines)

| Token                | Value                          | Role                   |
| -------------------- | ------------------------------ | ---------------------- |
| `--aig-fence-light`  | `oklch(0.90 0.010 250 / 0.08)` | Subtle section divider |
| `--aig-fence-medium` | `oklch(0.90 0.010 250 / 0.12)` | Medium emphasis        |
| `--aig-fence-strong` | `oklch(0.90 0.010 250 / 0.20)` | Strong structural line |

### Box-Shadow Outlines (not drop shadows)

| Token                 | Value                                    | Role               |
| --------------------- | ---------------------------------------- | ------------------ |
| `--aig-border-subtle` | `0 0 0 1px oklch(0.90 0.010 250 / 0.06)` | Card/panel outline |
| `--aig-border-medium` | `0 0 0 1px oklch(0.90 0.010 250 / 0.12)` | Medium emphasis    |
| `--aig-border-strong` | `0 0 0 1px oklch(0.90 0.010 250 / 0.20)` | Strong outline     |

### 3D Perspective Reveals (meter.com inspired)

| Token                   | Value                                  |
| ----------------------- | -------------------------------------- |
| `--aig-perspective`     | `1200px`                               |
| `--aig-tilt-initial`    | `rotateX(-19deg)` — cards start tilted |
| `--aig-tilt-final`      | `rotateX(0deg)` — flatten on scroll    |
| `--aig-reveal-duration` | `0.6s`                                 |
| `--aig-reveal-stagger`  | `80ms` between children                |

CSS classes: `.aig-reveal`, `.aig-reveal-stagger`, `.aig-fade-up`,
`.aig-fade-up-stagger` — driven by Intersection Observer in `glass-surface.js`.

---

## Typography System

**Serif is BANNED.** Permanently. No exceptions. This is a LAW, not a
preference.

### Three-Font Stack

| Role              | Font                     | Where Used                                                                   |
| ----------------- | ------------------------ | ---------------------------------------------------------------------------- |
| **Pixel texture** | Geist Pixel (5 variants) | Headings (h1-h3), chart axis labels, stat numbers, nav items, section labels |
| **Body**          | JetBrains Mono           | Paragraphs, citations, blockquotes, long-form reading                        |
| **Display**       | Space Grotesk            | Large hero headings only (when pixel feels too heavy)                        |

### Pixel Font Rules

- Very slight — pixel is a texture cue signaling "computational origin," not the
  dominant aesthetic
- Golden-ratio distribution: adjacent characters rarely share the same Geist
  Pixel variant
- Body text, paragraphs, citations: clean JetBrains Mono (never pixel)
- Rule: pixel font is used where the _system_ is speaking; clean mono where
  _humans_ are reading prose

### Type Scale

| Token             | Size                                        |
| ----------------- | ------------------------------------------- |
| `--aig-text-xs`   | clamp(0.694rem, 0.65rem + 0.2vw, 0.75rem)   |
| `--aig-text-sm`   | clamp(0.833rem, 0.78rem + 0.25vw, 0.875rem) |
| `--aig-text-base` | clamp(1rem, 0.95rem + 0.25vw, 1rem)         |
| `--aig-text-lg`   | clamp(1.125rem, 1.05rem + 0.35vw, 1.25rem)  |
| `--aig-text-xl`   | clamp(1.25rem, 1.1rem + 0.75vw, 1.5rem)     |
| `--aig-text-2xl`  | clamp(1.5rem, 1.25rem + 1.25vw, 2rem)       |
| `--aig-text-3xl`  | clamp(1.875rem, 1.5rem + 1.875vw, 2.5rem)   |
| `--aig-text-4xl`  | clamp(2.25rem, 1.75rem + 2.5vw, 3rem)       |
| `--aig-text-5xl`  | clamp(3rem, 2.25rem + 3.75vw, 4.5rem)       |

---

## Visual Layers (back to front)

### Layer 1 — Particle Void (Canvas 2D)

Sparse star particles, pulsing glows, rare shooting stars, space dust clouds.
Very subtle, ambient — the viewer should barely notice it consciously.

| Parameter              | Value              |
| ---------------------- | ------------------ |
| Particle count         | ~120               |
| Size range             | 0.5–2px            |
| Opacity range          | 0.2–0.7            |
| Pulse period           | 4s breathing cycle |
| Shooting star interval | ~45s avg           |
| Shooting star speed    | ~2s traversal      |

### Layer 2 — Grid Beam (Canvas 2D)

Faint dot-matrix grid lines with animated beams flowing along them. Provides
blueprint structure. Grid lines are dots, not solid CSS borders.

| Parameter    | Value            |
| ------------ | ---------------- |
| Grid spacing | 60px             |
| Dot size     | 1.5px            |
| Dot opacity  | 0.08             |
| Beam speed   | 8s cycle         |
| Beam length  | 15% of grid line |
| Beam opacity | 0.12             |

Reference: cult-ui `GridBeam` component (Canvas beam on SVG grid dividers)

### Layer 3 — Dithered Glass Surface (CSS)

Deep charcoal base with Bayer matrix dithering applied via CSS `::after`
pseudo-element. CRT/degraded quality — not clean digital.

| Parameter      | Value                           |
| -------------- | ------------------------------- |
| Cell sizes     | 8px / 12px / 16px / 20px / 28px |
| Default cell   | 16px                            |
| Dither opacity | 0.04                            |
| Method         | Pure CSS, no Canvas             |

Reference: cult-ui `DitherImage` component (Bayer matrix via CSS)

### Layer 4 — Content (DOM)

Text, charts, cards — written ON the glass surface. No drop shadows. No
elevation. No glassmorphism. Content has zero z-offset from the surface.

### Layer 5 — Ambient Overlays (Canvas + CSS)

Animated light reflections as if from instrument lights behind the viewer. SVG
sacred geometry section dividers with scroll-triggered stroke drawing.
Lightboard glow accents.

Reference: cult-ui `SVGShapesAnimated` (pathLength 0→1 on scroll), `Lightboard`
(dot-matrix LED grid)

---

## Motion System

### Architecture

- **Canvas** for environment: particles, grid beams, ambient light
  (requestAnimationFrame, ~60fps)
- **CSS** for content: Intersection Observer reveals, data breathing, typewriter
  text
- **No React, no Framer Motion, no build step** — single static HTML

### Easing (exponential curves only — CSS `ease` is banned)

| Token                    | Curve                                  |
| ------------------------ | -------------------------------------- |
| `--aig-ease-out-expo`    | `cubic-bezier(0.16, 1, 0.3, 1)`        |
| `--aig-ease-in-expo`     | `cubic-bezier(0.7, 0, 0.84, 0)`        |
| `--aig-ease-in-out-expo` | `cubic-bezier(0.65, 0, 0.35, 1)`       |
| `--aig-ease-sharp`       | `cubic-bezier(0.25, 0.46, 0.45, 0.94)` |

### Three Motion Layers

| Layer            | Speed             | Purpose                         | Examples                    |
| ---------------- | ----------------- | ------------------------------- | --------------------------- |
| **Data State**   | Slow (1.5–3s)     | State changes, narrative rhythm | Number count-up, chart draw |
| **System State** | Medium (0.4–1.2s) | Interface feedback, interaction | Hover reveal, button state  |
| **Signal State** | Fast (0.15–0.3s)  | Instant feedback, attention     | Pulse glow, focus switch    |

### Entry Pattern

One-shot: animate in once on viewport entry, then hold. No continuous loops
(except particle breathing and grid beam flow, which are ambient).

---

## Three-Layer Visualization System

### Layer A — Data Charts (Vega-Lite)

Replacing Chart.js with Vega-Lite for grammar-of-graphics expressiveness.

- Data "breathes": subtle pulse/glow on key data points
- One-shot entry animations (not continuous)
- Chart axis labels use pixel font
- Charts feel like instruments on a control panel

### Layer B — Animated Companions (Canvas)

Motion graphics alongside text sections that reinforce the narrative.

- Not decorative — encode information or show processes
- Examples: agent workflow animations, decision tree visualizations, capability
  radar sweeps

### Layer C — Interactive Explainers (DOM + JS)

Zero-word simulations that teach through interaction. One per major section
group:

1. **Agent Capabilities Spectrum** — slider showing autonomy levels (supervised
   → semi-autonomous → fully autonomous), real-time visualization of what
   changes
2. **Startup Lifecycle Timeline** — draggable timeline showing agent executives
   mapping onto startup stages
3. **Human-Agent Collaboration Model** — clickable diagram exploring
   collaboration patterns

---

## Brand Personality

| Keyword           | Meaning                                                        |
| ----------------- | -------------------------------------------------------------- |
| **Architectural** | Blueprint precision — lines are language                       |
| **Void**          | Deep space particle field — sparse but ordered                 |
| **Dithered**      | CRT/ASCII digital texture — degradation is beauty              |
| **Symbiotic**     | Human-agent organic fusion — not cold machine                  |
| **Deliberate**    | Every motion has meaning — no decoration for decoration's sake |

---

## Style References

### Primary

- **cult-ui.com** — pixel heading, grid beam, dither image, terminal animation,
  SVG shapes, lightboard
- **Vercel / Raycast** — clean dark interfaces, motion quality
- **Bloomberg Terminal** — data density, instrument panel feel
- **Linear** — motion quality, dark mode craft

### Anti-References

- Generic SaaS landing pages
- Crypto hype sites
- Overly polished pitch decks
- Blog-post layouts that bury data
- Any serif typography
- Any emoji usage
- Any glassmorphism (backdrop-filter: blur)

---

## Prohibited Patterns

| Pattern                               | Why                                                     |
| ------------------------------------- | ------------------------------------------------------- |
| Serif fonts                           | Permanently banned — spacecraft terminal, not newspaper |
| Emojis                                | Unprofessional, breaks the computational aesthetic      |
| Drop shadows / elevation              | Content sits ON glass, not above it                     |
| Glassmorphism (backdrop-filter: blur) | Banned — the glass is textured, not blurred             |
| Left-border verdict blocks            | Lazy pattern, no visual hierarchy                       |
| Identical card grids                  | Monotonous, no narrative rhythm                         |
| Hex color values                      | OKLCH exclusively                                       |
| CSS `ease` timing                     | Exponential curves only                                 |
| Pure white (#fff)                     | Destroys deep space feel                                |
| Purple gradient backgrounds           | AI slop signature                                       |
| CSS transitions using `ease`          | No spring physics or exponential curves                 |

---

## Design Principles

1. **Evidence over ornament** — data is protagonist, visuals are servant
2. **Blueprint precision** — lines are language, every element has coordinates
3. **Deep space restraint** — background is void, information is stellar body
4. **Signal precision** — color carries meaning, never decoration
5. **Motion with purpose** — no movement is meaningless; if it moves, it means
   something

---

## Token Integration

All values above are available as CSS custom properties in `design-tokens.css`:

```html
<link rel="stylesheet" href="design-tokens.css" />
```

Then use `var(--aig-*)` in any CSS. The monorepo can import this single file to
inherit the full design system.
