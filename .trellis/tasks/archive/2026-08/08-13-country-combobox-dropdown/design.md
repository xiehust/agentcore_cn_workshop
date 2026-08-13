# Design: Controlled Country Combobox

## Component

Keep the state inside `NewTaskPanel`:

- `countryMenuOpen`
- `countryActiveIndex`
- fixed suggestion array `["UK", "US"]`

Render an input and icon button inside a positioned wrapper. Render a
`role=listbox` menu with button options when open.

## Interaction

- Input focus/click and icon click open the full menu.
- ArrowDown/ArrowUp open and cycle the active option.
- Enter selects the active option only when the menu is open.
- Escape closes without changing custom input.
- Bubbling `onBlur` closes only when focus leaves the wrapper.
- Option selection sets the value and returns focus to the input.

Use `role=combobox`, `aria-expanded`, `aria-controls`, `aria-activedescendant`,
`role=option`, and `aria-selected`.

## Styling

The input keeps existing drawer styling. The toggle button is positioned on
the right; the menu is an absolute panel below the input with existing
background, border, focus, and accent colors. Mobile width is constrained to
the wrapper.
