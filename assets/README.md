# claude_crew — logo assets

Vector logo files for the repository. SVGs are scalable and tiny; drop them straight into the repo.

## Files

| File | Use |
|------|-----|
| `claude_crew-logo-light.svg` | Full lockup (mark + wordmark) — for **light** backgrounds |
| `claude_crew-logo-dark.svg`  | Full lockup — for **dark** backgrounds |
| `claude_crew-icon-light.svg` | Mark only — for **light** backgrounds (repo/org avatar, favicon) |
| `claude_crew-icon-dark.svg`  | Mark only — for **dark** backgrounds |

Brand color: `#e8602c`. Wordmark font: JetBrains Mono (the SVG falls back to the
system monospace anywhere the font isn't installed — GitHub renders it with its
default monospace, which looks fine).

## Where to add them

1. Copy the `assets/` folder to the root of the repo:

   ```
   claude_crew/
   ├── assets/
   │   ├── claude_crew-logo-light.svg
   │   ├── claude_crew-logo-dark.svg
   │   ├── claude_crew-icon-light.svg
   │   └── claude_crew-icon-dark.svg
   ├── README.md
   └── ...
   ```

2. Put the lockup at the very top of `README.md`, above the `# Claude Crew`
   heading. Use a `<picture>` so it follows the viewer's GitHub theme:

   ```html
   <p align="center">
     <picture>
       <source media="(prefers-color-scheme: dark)" srcset="assets/claude_crew-logo-dark.svg">
       <img alt="claude_crew" src="assets/claude_crew-logo-light.svg" width="440">
     </picture>
   </p>
   ```

   (If you keep that, you can delete the old `# Claude Crew` text heading — the
   lockup already shows the name.)

3. **Repo / org avatar** — GitHub → repo **Settings** (or org **Settings → Profile**)
   → upload `claude_crew-icon-light.svg`. GitHub needs a raster for avatars, so if
   it rejects the SVG, open the SVG in any browser and export/screenshot it to a
   square PNG (512×512) first.

## Editing later

These are hand-readable SVGs — open in any editor. To recolor, change the
`#e8602c` (orange) and the spoke color (`#15161a` in the light files, `#f2f2f2`
in the dark files).
