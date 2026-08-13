# Fix country combobox dropdown

## Goal

Replace the browser-dependent country datalist with a controlled editable combobox that always exposes UK/US options while preserving custom input.

## Background

- The current control is an `<input list>` backed by `<datalist>`.
- Chrome treats `datalist` as browser-native autocomplete and may not open
  suggestions when the input already contains the default value `UK`.
- The backend already accepts arbitrary country text; this fix is frontend-only.

## Requirements

- Replace `datalist` with a controlled combobox in `NewTaskPanel`.
- Keep `UK` as the default and keep `UK` / `US` as suggestions.
- Clicking the input or dropdown icon must always open the visible menu.
- Selecting an option updates the input and closes the menu.
- Users can still type and submit any custom value.
- Support ArrowUp/ArrowDown navigation, Enter selection, Escape close, and
  close when focus leaves the control.
- Use existing Phosphor icons and visual conventions.
- Do not change API, database, runner, or country persistence behavior.

## Acceptance Criteria

- [x] Chrome visibly shows UK and US after clicking the control while UK is
      already selected.
- [x] Clicking US changes the value to US and closes the menu.
- [x] Custom text remains editable and is not replaced unless an option is
      explicitly selected.
- [x] Keyboard navigation and Escape work.
- [x] Desktop and 390px mobile layouts have no clipping or overlap.
- [x] Frontend production build and Playwright interaction checks pass.

## Out Of Scope

- Adding or changing suggested countries.
- Backend or database changes.
- Submitting a real ingestion task during UI verification.

## Notes

- No blocking questions remain.
