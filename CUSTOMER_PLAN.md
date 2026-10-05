# Plan: customers own meters (replaces attributed_meter_id)

Status: **approved 2026-10-04**.

## Goal

- Every E66 row is stored under **its own metering point id**. The
  production→consumption attribution and `attributed_meter_id` go away.
- Every E66 row and header row carries the **customer id** that owns the meter,
  taken from the provider's list (`config/customers.yaml`).
- A new Grafana dashboard per customer: a `customer_id` drop-down, one panel per
  owned meter labelled with its direction, CEL + grid stacked as in the overview.
- The QuestDB tables are dropped and rebuilt by replaying the archive.
- The two existing dashboards are **not edited**.

## What the provider's list tells us

The list groups the metering points by customer:

- `9000106` owns **two** consumption meters and one production meter.
- `9000105` owns four consumption meters and no production meter.
- `9000107` owns two consumption meters and one production meter.
- `9000114` owns `0134575W` (production-only) **and** consumption meter
  `0832199P`.

Grouping by customer handles all of these without special cases.

## 1. The meter list: `config/customers.yaml`

The provider's list, as YAML. When the provider sends a new list,
`customers.yaml` is updated by hand.

```yaml
customers:
  "9000106":
    consumption:
      - "CH1011101234500000000000002291991"
      - "CH1011101234500000000000001650626"
    production:
      - "CH101110123450000000000000855219K"
  "9000105":
    consumption: [...4 ids...]
  "9000114":
    consumption:
      - "CH101110123450000000000000832199P"
    production:
      # Not coupled: 0832199P reports no production total of its own.
      - "CH101110123450000000000000134575W"

```

`scripts/meters.py` is rewritten around it:

```
Meters.customer_of(meter_id) -> Optional[str]
Meters.role_of(meter_id)     -> Optional[str]      # consumption | production
Meters.customer_has(customer_id, role) -> bool
```

The loader is as strict as `load_meters` is today, raising at startup on: a
missing file, an unknown section or role, an id shorter than a full meter id,
the same id declared twice (across customers), or a file
that declares no meter.

`processing.meters_file` in `api_config.yaml` points to `customers.yaml`.
**Note: the NAS copy of api_config is the live one**, so it has to be edited
there as well. `customers.yaml.example` holds fake ids.

## 2. Ingestion rules (`parse_sdat_e66_individual.py`)

Without attribution, both of today's skip rules turn out to be one rule: **a
metering point reports only its own direction.**

| File | Today | New |
|---|---|---|
| Consumption point, consumption total/cel/grid | stored on itself | stored on itself, `customer_id` set |
| Consumption point, **production total** (the duplicate) | **stored** on the consumption id | **skipped**, because the production point carries the same total |
| Production point, production total | **skipped** as the duplicate | **stored** on itself |
| Production point, production cel/grid | stored under the consumption twin | stored on itself |
| `0134575W` consumption file (provider bug) | skipped | skipped (same rule: production meter, and 9000114 owns a consumption meter) |
| `0134575W` production total/cel/grid | stored on itself | stored on itself, unchanged |
| Meter not in customers.yaml (the RCP meters) | stored, `community_id` NULL | stored, `customer_id` NULL; WARNING if it is not RCP |

**Changed: which copy of the production total is kept.** Keeping the
production point's copy means every meter is self-contained: its `total` equals
`cel + grid` on the same id. Keeping the consumption point's copy would split
one customer's production across two ids, and for `9000106` it is not obvious
which consumption meter the copy belongs on.

**How strict the skip is.** A direction that does not match the meter's role is
an INFO-level `SkippedDocument` (archived, as now) **only when the customer owns
a meter of that direction**, since the data is then stored elsewhere. Otherwise
the file **fails** with an ERROR and stays in incoming. Example: a
consumption-only customer whose consumption meter starts reporting a production
total. That means they installed solar and the provider has not sent us a new
list yet, and dropping it quietly would lose real production.

Shared models: `MeteredData.meter_id` becomes "the file's own meter", and a new
`MeteredData.customer_id` field is added. `FileHeader.attributed_meter_id` is
removed and `FileHeader.customer_id` is added.

## 3. Schema (`questdb_schema.sql`): the database is recreated, so no migration

```sql
cel_energy:       + customer_id SYMBOL      -- payload, NOT a dedup key
cel_file_header:  - attributed_meter_id
                  + customer_id SYMBOL
                  file_meter_id -> meter_id (rename; it is now the only meter id)
```

- **`customer_id` goes on `cel_energy` too, not only the header table.** The
  dashboard filters on it in every query. Getting it from the header would mean
  a join from 25M rows to `cel_file_header` through `source_file`, in each of
  about ten panel queries. A SYMBOL column costs almost nothing.
- **It is not a dedup key**, for the same reason `condition` is not: if a meter
  moved to another customer, keying on `customer_id` would give each slot two
  rows and every `sum()` would count it twice. As payload, a re-delivered slot
  just takes the new owner.
- `questdb_writer.py`: update the column tuples, `rows_from_e66` and
  `row_from_header`. `questdb_init.verify()` gets the new columns.

## 4. Reporting and tooling

- `delivery_report.py`: `attributed_meter()` reduces to "the file's own meter,
  or None if skipped". `check_declared_pairs` becomes **`check_customer_production`**:
  for each customer, the production total reported by its consumption point(s)
  must equal the sum of its production points' totals, slot by slot. The
  check is skipped when the consumption side reports no production total at
  all, which is the case for an uncoupled production meter (`0134575W`), and
  also when a delivery wave is missing one side. This is
  the only evidence that customers.yaml groups meters correctly, so the check stays.
- `validate_daily_balance_questdb.py` and `toolbox/diagnose_validation_gap.py`:
  community sums do not change, because each total is still stored exactly
  once. I'll check them for any assumption that production rows sit on a
  consumption id.
- Docs (README.md about the input files, QUESTDB.md, PARSING_GUIDE.md, FILE_BREAKDOWN_ANALYSIS.md,
  PROVIDER_QUESTIONS.md Q9, grafana-dashboards/README.md, DEPLOYMENT_CHECKLIST.md):
  replace the pair vocabulary with customer/role. Following the no-counts rule,
  state the rules and point to customers.yaml.

## 5. Recreate the database

1. Stop the ingest container.
2. `DROP TABLE` all four tables (`cel_energy`, `cel_community_energy`,
   `cel_file_header`, `cel_ingest_log`). E31 does not change, but rebuilding all
   four keeps the ingest log consistent with the data.
3. `python3 scripts/questdb_init.py` to create the tables from the new schema.
4. Replay the archive **in ascending delivery order** (DEDUP is last-write-wins,
   so an out-of-order replay quietly brings back older values).
5. Check: no `failed` outcomes. E66 vs E31 balance unchanged from before the
   rebuild, except where expected: the production total now comes from the
   production points, and these are equal by `check_customer_production`. Every
   non-RCP row has a `customer_id`.

This is the **drop-and-replay already owed** for the undeployed redesign, so
both changes ship in one deployment. `customers.yaml` has to be copied to the NAS config
folder.

## 6. New dashboard: `grafana-dashboards/cel-customer-energy-e66.json`

Classic format like the reference dashboards, new uid, same
QuestDB datasource. It follows the existing rules: outer `cast(... AS DOUBLE)`,
inner `SAMPLE BY 15m` then outer `avg(...) * 4` for kW, and `byFrameRefID`
overrides.

**Variables**
- `customer_id`: single select,
  `SELECT DISTINCT customer_id FROM cel_energy WHERE customer_id IS NOT NULL ORDER BY customer_id`.
- `cons_meter`: hidden, multi/All,
  `SELECT DISTINCT meter_id FROM cel_energy WHERE customer_id = '$customer_id' AND direction = 'consumption'`.
  The regex `/^(?<value>.*(?<text>[0-9A-Z]{8}))$/` shows the 8-character
  suffix as the label and keeps the full id as the value, so queries use
  `meter_id = '$cons_meter'` instead of `LIKE`. Named groups are required: an
  unnamed capture group becomes the value too.
- `prod_meter`: the same, for `direction = 'production'`.

**Layout**
1. **Customer summary** (all meters of the customer):
   - Consumption, stacked: From CEL + From Grid (kW)
   - Production, stacked: To CEL + To Grid (kW)
   - Stats: total consumption, total production, CEL % of consumption, CEL % of
     production
   - Net balance (production − consumption)
2. Row **"Consumption meters"**: one stacked CEL+grid panel per meter, repeated
   over `$cons_meter`, titled `Consumption – ${cons_meter}`.
3. Row **"Production meters"**: the same, repeated over `$prod_meter`, titled
   `Production – ${prod_meter}`. Shows one empty panel when the customer has
   no production meter (classic dashboards cannot hide a row).

The customer filter already limits rows to the community (RCP meters have no
customer), so there is no `community_id` constant to hard-code.

**Tests**: once the classic originals were restored as references,
`tests/test_dashboards*.py` passed again, so the new dashboard was added to them.
The community-scope check also accepts a `customer_id` filter.

## 7. Effect on the existing dashboards (not edited, but affected)

Once the database is rebuilt, the **E66 overview's production panels go
empty** for a consumption meter (Daily Production, Total Production, CEL % of
Production, the production half of Energy Balance, Load curve and Courbe de
charge). Production now lives on the production meter's own id, and the
overview's drop-down only lists consumption meters. The E31 dashboard is not
affected. Options: accept this (the customer dashboard replaces those panels),
or you change the overview yourself in Grafana.

## Decisions (2026-10-03/04)

1. `0134575W` is handled as it is today: its consumption files are dropped
   and its production is kept.
2. The meter list is kept as YAML (`customers.yaml`).
3. The **production point's** copy of the duplicated production total is kept.
4. It is acceptable that the overview dashboard's production panels go empty.
5. The dashboard tests are not updated.

## Order of work

1. `config/customers.yaml`; `meters.py` + its tests
2. parser rules + models + writer + schema, with tests
3. delivery_report check, validators, toolbox
4. dashboard JSON
5. docs
6. (you) deploy: customers.yaml and api_config to the NAS, drop, init, replay in ascending order
