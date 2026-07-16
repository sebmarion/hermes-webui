# Hermes Radar Favicon Design

## Goal

Give the Hermes Radar dashboard a recognizable favicon that remains legible in browser tabs, bookmarks, and narrow launcher surfaces.

## Selected direction

Use the “Signal Sweep” mark selected in the visual companion:

- dark navy rounded-square field;
- two subdued radar rings;
- cyan center node;
- lime/cyan sweep arm and detection dot;
- no text, letters, or fine detail that would disappear below 32px.

Reference colors are `#0d1424` (field), `#2e4667` and `#203653` (rings), `#77e9ff` (center), and `#9cffc7` (sweep/dot).

The visual language matches Radar’s telemetry/discovery purpose and complements the dashboard’s existing dark analytical UI.

## Asset strategy

- Add `favicon.svg` as the canonical scalable asset in `/Users/seb/.hermes/artifacts/seb-trajectory-radar/`.
- Replace the existing mislabeled/non-square `favicon.ico` with a real ICO container containing a square 32px PNG or RGBA image.
- Update `apple-touch-icon.png` and `apple-touch-icon-precomposed.png` in the same directory to square 180px PNG files using the same mark.
- Add explicit `<link rel="icon" type="image/svg+xml">`, `.ico`, and Apple touch icon references in the generated dashboard HTML.
- Keep the source asset and the live published artifact aligned by copying the generated files into the live artifact directory after build.

## Constraints

- Preserve the current dashboard layout, behavior, and color contrast.
- Avoid external image or font dependencies.
- Keep the icon understandable at 16px, 32px, and 180px.
- Do not alter Radar telemetry, recommendations, or generated data.

## Verification

- Inspect `/Users/seb/.hermes/artifacts/seb-trajectory-radar/index.html` for the explicit icon links.
- Verify `favicon.svg` geometry, the ICO container signature, and PNG dimensions (32px ICO image; 180px Apple touch icons).
- Open the live dashboard at `http://macbook-pro.tailfad2e3.ts.net:43127/` and confirm the icon loads without console errors in the in-app browser.
- Confirm the published favicon files match the generated source assets.

## Out of scope

- Redesigning the Radar dashboard.
- Changing the Radar server URL, Tailscale exposure, or telemetry logic.
