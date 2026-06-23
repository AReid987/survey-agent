# Design

## Visual Metaphor: Station Glass
An architect aboard a space station, drafting blueprints on a glass viewport that doubles as a drafting table. The living knowledge graph and deep space void drift behind the glass. Every surface, texture, and animation reinforces computational origin serving human understanding.

## Color System (OKLCH)
We use OKLCH exclusively. No hex, no pure white, no pure black.

### Surface Layers (hue 250)
- **Void Deep:** `oklch(0.10 0.015 250)` — Deepest background
- **Void Base:** `oklch(0.13 0.015 250)` — Base background
- **Void Raised:** `oklch(0.17 0.015 250)` — Cards/Panels
- **Void Glass:** `oklch(0.20 0.020 250)` — Glass texture layer

### Typography Hierarchy
- **Primary:** `oklch(0.90 0.010 250)` — Headings
- **Body:** `oklch(0.78 0.010 250)` — Prose
- **Secondary:** `oklch(0.60 0.010 250)` — Labels/Muted
- **Tertiary:** `oklch(0.45 0.010 250)` — Disabled/Hints

### Accents
- **Warm Amber:** `oklch(0.75 0.150 65)` — Data signals, key numbers, particle glow
- **Signal Go:** `oklch(0.72 0.170 160)` — Success/Go verdicts
- **Signal High:** `oklch(0.65 0.170 300)` — Highlights

## Typography
**Serif is BANNED.** Permanent architectural law.

- **Pixel texture:** `Geist Pixel` (or `Geist Mono`) — System headings, chart labels, stats.
- **Body prose:** `JetBrains Mono` — Paragraphs, citations, long-form reading.
- **Display:** `Space Grotesk` — Large hero headings only.

## Structure & Borders
- **Border Radius:** ALL ZERO. Sharp corners only.
- **Fences:** 1px structural lines (dot-matrix or subtle gradients).
- **Outlines:** 1px outlines via `box-shadow`, not `border`.
- **Elevation:** No drop shadows. Content sits ON the glass.

## Visual Layers (Back to Front)
1. **Particle Void:** Sparse star field (Canvas 2D).
2. **Grid Beam:** Dot-matrix blueprint grid with flowing beams (Canvas 2D).
3. **Dithered Glass:** Bayer matrix texture (CSS `::after`).
4. **Content:** Text and charts written ON the glass (DOM).
5. **Ambient Overlays:** Instrument light reflections and sacred geometry (Canvas/SVG).

## Motion Laws
- **Architecture:** Canvas for environment, CSS for content.
- **Easing:** Exponential curves only (`--aig-ease-out-expo`). No `ease`, no `bounce`.
- **Layers:** 
  - Data State (Slow: 2s)
  - System State (Medium: 0.6s)
  - Signal State (Fast: 0.2s)
