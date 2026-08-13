# Design: Extensible Ingestion Task Country Selection

## 1. Boundary

Country is task metadata. It is stored once on `ingestion_tasks` and read for
every one-time, manual, or scheduled run.

```text
Editable country combobox
  -> POST /tasks
  -> ingestion_tasks.country
  -> runner _load_task
  -> generic country directive + stored prompt
  -> Part 1 Skill discovery
```

The portal does not own a country catalog. UK and US are frontend suggestions
only because matching Skills exist today.

## 2. Data Model And Migration

Fresh schema:

```sql
country VARCHAR(64) NOT NULL DEFAULT 'UK'
```

Existing schema upgrade:

```sql
ALTER TABLE ingestion_tasks
ADD COLUMN IF NOT EXISTS country VARCHAR(64) DEFAULT 'UK';
```

The upgrade remains nullable because the database spec requires newly added
columns on existing tables to preserve old callers. The default makes existing
rows and future omitted inserts read as UK, while the API always supplies a
normalized value. The runner also treats null as UK defensively. A
64-character limit supports country codes and readable marketplace names
without allowing unbounded task metadata.

## 3. REST Contract

`country` is a string, not an enum.

`POST /tasks`:

1. Convert the input to text when present.
2. Collapse whitespace and trim it.
3. Use `UK` when the result is empty or omitted.
4. Reject values longer than 64 characters.
5. Persist with a parameterized insert.

`TASK_COLUMNS` includes `t.country`, carrying it through list, detail, create,
pause, and resume responses. No backend whitelist or mapping is introduced.

## 4. Runtime Prompt Construction

The runner loads `country` and uses a pure helper to prepend a generic
directive:

```text
任务国家/站点：<stored value>。
请匹配明确支持该国家/站点的 Skill；该配置优先于任务文本中的冲突描述。
域名、币种、筛选规则和入库 site 值以匹配到的 Skill 为准。
如果没有匹配 Skill，不得回退到其他国家站点或写入错误数据。
```

The original user prompt follows unchanged. Missing values defensively fall
back to UK, matching the schema and API.

This keeps future expansion data-driven: a new Skill only needs metadata that
recognizes the custom value users save.

## 5. Frontend

Use a native editable combobox pattern:

- controlled `<input list="...">`;
- `<datalist>` suggestions for `UK` and `US`;
- initial state `UK`;
- custom values remain editable and are sent as entered after trimming;
- a short hint explains that a corresponding Skill must exist.

Add `country: string` to task/create types. Display the stored value alongside
the prompt in task list rows and in the task detail header. Extend the existing
drawer input styling; no new component library or global state is needed.

## 6. Tests And Verification

Focused Python tests load Lambda modules directly and stub AWS/data boundaries:

- omitted/empty country defaults to UK;
- repeated whitespace is normalized;
- a custom value such as `DE` persists without whitelist logic;
- values over 64 characters return 400 without writing;
- generic prompt construction includes the exact country, retains the user
  prompt, and contains no country-specific domain mapping.

Frontend verification uses the production build plus desktop/mobile browser
inspection of default, suggestion, and custom-input states.

## 7. Deployment And Rollback

Deployment order:

1. Run Part 1 deployment so `setup_tables` adds the column.
2. Deploy Part 2 Lambdas and frontend.

Rollback requires only reverting Part 2 code/frontend. The database column
with its UK default remains backward compatible.
