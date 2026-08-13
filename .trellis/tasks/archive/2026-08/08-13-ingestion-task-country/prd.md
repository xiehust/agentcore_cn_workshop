# Add extensible country selection to ingestion tasks

## Goal

Let users select or enter a marketplace country when creating a Part 2
ingestion task, defaulting to UK, and persist the value so future countries
can be supported by adding a Part 1 Skill without changing backend code.

## Background

- The task creation form currently sends only `name`, `prompt`, `mode`, and
  optional schedule fields.
- The REST contract mirrors `ingestion_tasks` through the frontend API types
  and tasks-api Lambda.
- The runner currently sends the stored prompt unchanged to Part 1.
- Part 1 currently has eBay UK and eBay US Skills, which provide the initial
  suggestions but must not become a backend whitelist.
- `part1_data_ingestion/infra/setup_tables.sql` owns the shared Aurora schema
  and must remain idempotent for fresh and existing databases.

## Requirements

- **R1 - Editable country control:** Add a labeled editable country/marketplace
  combobox to the new-task drawer. It must suggest `UK` and `US`, accept a
  custom value such as `DE` or `德国`, and default to `UK`.
- **R2 - Frontend contract:** Add a required string `country` field to task
  response types and create-task input. The create request must send the
  trimmed value.
- **R3 - Task visibility:** Show the persisted country in the task list and
  task detail header.
- **R4 - Generic API validation:** `POST /tasks` must accept any non-empty
  country string, collapse surrounding/repeated whitespace, default
  omitted/empty values to `UK` for older callers, and reject values longer
  than 64 characters. The backend must not contain a country whitelist or
  country-to-domain mapping.
- **R5 - Persistence:** Fresh tables use `ingestion_tasks.country VARCHAR(64)
  NOT NULL DEFAULT 'UK'`. Existing-table upgrades add a nullable column with
  `DEFAULT 'UK'` per the database compatibility spec. Both paths must be
  idempotent and preserve old tasks as UK.
- **R6 - Read contract:** All task list/detail/create/state-change responses
  must include the stored country string.
- **R7 - Generic Skill routing:** The runner must load `country` and prepend a
  generic directive that:
  - names the exact stored country/marketplace;
  - tells Part 1 to select the Skill that supports that value;
  - states that the stored country takes precedence over conflicting wording
    in the free-form prompt;
  - leaves domain, currency, thresholds, and persistence `site` rules to the
    selected Skill;
  - forbids silently falling back to a different country when no matching
    Skill exists.
- **R8 - Extensibility:** Adding a future country must require only a Skill
  whose metadata recognizes the saved country value. No tasks-api, runner,
  schema, or frontend code change should be required.
- **R9 - Compatibility:** Existing tasks and old API callers behave as UK
  tasks. One-time, manual rerun, and scheduled paths all use the stored value.
- **R10 - Documentation:** Update Part 2 documentation and the ingestion-task
  cross-layer spec with the free-form contract and Skill extension rule.

## Acceptance Criteria

- [x] Opening the new-task drawer shows `UK` by default, offers UK/US
      suggestions, and accepts a custom value.
- [x] Creating a task stores the exact normalized custom country; omitted or
      empty country stores `UK`; values over 64 characters return HTTP 400.
- [x] Existing `ingestion_tasks` rows read back as `country=UK` after the
      idempotent schema upgrade.
- [x] Task list/detail/create/state-change responses include `country`, and the
      task list/detail UI displays it without layout regressions.
- [x] Runner payloads retain the original prompt and contain the exact saved
      country in a generic Skill-routing directive.
- [x] No backend file contains a fixed list of supported countries or
      country-to-domain mapping.
- [x] One-time, manual rerun, and scheduled execution paths all resolve
      country from the stored task row.
- [x] Focused backend tests cover defaulting, normalization, custom-value
      persistence, length validation, and generic runtime prompt construction.
- [x] Python syntax checks, frontend production build, browser UI verification,
      and `git diff --check` pass.

## Out Of Scope

- Editing the country of an existing task.
- Maintaining a centralized country catalog.
- Adding a country snapshot to each historical run.
- Creating additional country Skills in this task.
- Changing Part 1 Skill thresholds or candidate/verdict schemas.

## Technical Notes

- Deploy the Part 1 schema update before Part 2 Lambda code because updated
  task queries select the new column.
- Rolling back Part 2 code is safe after migration; the extra database column
  can remain.

## Notes

- No blocking product or scope questions remain.
