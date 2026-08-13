# Implementation Plan

1. Replace the `datalist` block in `NewTaskPanel` with a controlled combobox.
2. Add Phosphor caret/check icons and keyboard/focus handling.
3. Add scoped combobox/menu CSS.
4. Run `npm run build`.
5. Use Playwright against `http://localhost:5273` to verify click, selection,
   custom input, keyboard behavior, desktop/mobile layout, and console health.
6. Commit and archive through Trellis after review.
