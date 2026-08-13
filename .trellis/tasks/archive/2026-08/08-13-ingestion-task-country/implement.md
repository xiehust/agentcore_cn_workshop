# Implementation Plan

## 1. Schema

- Add `country VARCHAR(64) NOT NULL DEFAULT 'UK'` to fresh task DDL.
- Add the idempotent existing-table `ALTER` as nullable with a UK default,
  following the database compatibility spec.

## 2. Tasks API

- Add `country` to `TASK_COLUMNS`.
- Normalize whitespace, default empty/omitted values to UK, and enforce the
  64-character limit without a whitelist.
- Include country in the parameterized insert.

## 3. Runner

- Load country from `ingestion_tasks`.
- Add a pure helper that builds a generic Skill-routing directive.
- Preserve the original prompt and avoid country-specific mappings.
- Use the helper for every Part 1 invocation path.

## 4. Frontend

- Add string `country` fields to task and create input types.
- Add a UK-defaulted editable combobox with UK/US datalist suggestions.
- Validate non-empty custom input and send the trimmed value.
- Display the saved country in task list and task detail.
- Reuse existing drawer input styling.

## 5. Tests

- Add focused `unittest` coverage for defaulting, normalization, custom
  persistence, max length, and generic runner prompt construction.
- Stub AWS calls; tests require no credentials or network.

## 6. Documentation And Spec

- Update `part2_chat_portal/README.md`.
- Update `.trellis/spec/backend/ingestion-tasks-module.md` with the free-form
  field and future Skill-only extension rule.

## 7. Validation

```bash
python3 -m unittest discover -s part2_chat_portal/infra/tests -p 'test_*.py'
python3 -m py_compile \
  part2_chat_portal/infra/tasks-api-lambda/handler.py \
  part2_chat_portal/infra/runner-lambda/handler.py
npm run build --prefix part2_chat_portal/web
bash -n part1_data_ingestion/scripts/deploy.sh
git diff --check
python3 ./.trellis/scripts/task.py validate 08-13-ingestion-task-country
```

Browser verification:

- Desktop and mobile task creation drawer.
- UK default and UK/US suggestions.
- Enter and retain a custom country value.
- No overlap, clipping, or console errors.

## 8. Rollback Points

- Backend/frontend rollback can leave the schema column in place.
- If editable-combobox behavior differs by browser, retain the same string API
  and replace only the UI control implementation.
