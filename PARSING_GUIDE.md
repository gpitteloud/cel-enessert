# CEL Energy Data Parsing Guide

**Version**: 1.1  
**Date**: 2026-09-08

---

## Table of Contents

1. [Overview](#overview)
2. [Data Sources](#data-sources)
3. [Metering Concepts](#metering-concepts)
4. [Energy Codes Explained](#energy-codes-explained)
5. [File Types and Structure](#file-types-and-structure)
6. [Consumption vs Production Metering Points](#consumption-vs-production-metering-points)
7. [The Declared Meters](#the-declared-meters)
8. [Data Processing Flow](#data-processing-flow)
9. [Data Quality Flags](#data-quality-flags)
10. [Open Questions for Provider](#open-questions-for-provider)

---

## Overview

The CEL (Community Energy Local) community receives **multiple XML files daily** from the Swiss energy provider. These files contain detailed energy consumption and production data for all community members at 15-minute intervals.

**Key Facts:**
- **Community size**: Example - 9 members with production (solar panels), 12 members without
- **File count**: Dynamic based on membership (example: 21 members = 109 files)
- **Delivery frequency**: Daily, between 09:45-09:50 CET
- **Data resolution**: 15-minute intervals (96 observations per day)
- **Data coverage**: 5-day rolling window (overlapping)
- **File formats**: E66 (individual meters) + E31 (community aggregates, always 6 files)

---

## Data Sources

### File Delivery Pattern

**Daily Delivery**: All files arrive within a 5-minute window

```
20260527_094557_..._E66_...xml  ← E66: Individual meter
20260527_094558_..._E66_...xml
...
20260527_094741_..._E31_...xml  ← E31: Community aggregate
```

**File Types Breakdown (example for 21 members):**
- **103 E66 files**: Individual meter data (ValidatedMeteredData_1.6 format) - varies by membership
- **6 E31 files**: Community aggregated data (AggregatedMeteredData_1.3 format) - always constant

**Overlapping Data:**
- Each file covers 5 consecutive days
- Files delivered daily create 4-day overlap
- Example: May 27 delivery covers May 21-26, May 28 covers May 22-27
- **Finding**: Data values are identical in overlapping periods (0% change detected)

---

## Metering Concepts

### Member Types

The community has different member configurations:

#### 1. **Consumer Only** (12 members)
Members **without solar panels** - only consume energy

**Files per member**: 3
- Consumption Total
- Consumption breakdown: CEL Local
- Consumption breakdown: Grid

**Example meters**: `0036273C`, `0050170B`, `0060545I`, etc.

#### 2. **Consumer + Producer** (9 members)
Members **with solar panels** - consume and produce energy

**Files per member**: 4
- Consumption Total
- Consumption breakdown: CEL Local  
- Consumption breakdown: Grid
- Production Total

**Example meters**: `0217130Y`, `0020576V`, `0046782G`, etc.

**Note**: their production breakdown (CEL vs Grid) arrives on a **second metering
point id** — see below. A producing member is one member, two ids.

---

### Consumption vs Production Metering Points

This is a **key concept** for understanding the data structure. A site that
produces has **two metering point ids**, and each SDAT file declares which kind it
carries: `ConsumptionMeteringPoint/VSENationalID` or
`ProductionMeteringPoint/VSENationalID`.

#### Consumption metering point

**What its files carry**:
- ✅ **Total consumption** (all energy consumed, regardless of source)
- ✅ **Consumption breakdown** (CEL Local vs Grid split)
- ✅ **Total production**, if its customer produces — a **copy** of what the
  customer's production metering point reports, so it is dropped on ingest

**What they DON'T carry**:
- ❌ **Production breakdown** (CEL Local vs Grid split — on the production
  metering point instead)

**Identifier pattern**: 
- Last 8 characters (suffix): e.g., `0217130Y`, `0046782G`
- Full format: `CH101110123450000000000000217130Y`

#### Production metering point

**What its files carry**:
- Production CEL Local (VSE code `2404050010123`)
- Production Grid (VSE code `2404050010124`)
- Production Total (ebIX code `8716867000030`) — kept: this is the copy stored

**Identifier pattern**: no logic depends on it — the id is opaque, and which
customer owns it in which role is read from the declared list, never inferred
from the string. Observed only: today's production ids happen to carry `085` in
the last 8 characters, e.g. `CH10111012345000000000000008574078`.

**Important**: production breakdown values are **estimated/calculated** (see
Condition 21), not directly measured.

#### Example: a producing customer

**Customer owning consumption meter `0217130Y` and production meter `08574078`:**

```
Consumption metering point: CH101110123450000000000000217130Y
├─ Consumption Total: 123.45 kWh
├─ Consumption CEL Local: 78.90 kWh (estimated)
├─ Consumption Grid: 44.55 kWh (estimated)
└─ Production Total: 234.56 kWh          ← a copy, dropped as a duplicate

Production metering point: CH10111012345000000000000008574078
├─ Production CEL Local: 123.45 kWh (estimated)
├─ Production Grid: 111.11 kWh (estimated)
└─ Production Total: 234.56 kWh          ← kept
```

Every meter is stored under **its own id**, with its customer's id alongside;
the customer id is what groups them. Keeping the production point's copy of the
total makes each meter self-contained: its `total` is `cel + grid` on the same
id.

A customer may own several metering points of either role (several consumption
points, for instance). When it does, the consumption-side production total is the
customer's production as a whole — see
[What the declaration is checked against](#what-the-declaration-is-checked-against).

That equality is real and useful, but it is **not** how ownership is
established — the provider declares it. See
[The Declared Meters](#the-declared-meters).

#### A production point that is not coupled: 0134575W

Meter suffix `0134575W` (appeared ~July 2026) is a production metering point
whose production total has **no copy** on a consumption point: its customer's
consumption point reports no production total. So its own total is the only
one, and it is kept like any other production point's.

`0134575W` is **not linked to RCP** (Regroupement pour la Consommation Propre /
self-consumption grouping).

```
Meter: CH101110123450000000000000134575W
├─ Production Total:     851.234 kWh (ebIX 8716867000030)   ← kept
├─ Production CEL Local: 124.148 kWh (VSE 2404050010123, estimated)
└─ Production Grid:      727.086 kWh (VSE 2404050010124, estimated)
                         (124.148 + 727.086 = 851.234 ✓)
```

**⚠️ The provider also sends it a consumption total file, and that file is
spurious** — a production metering point has no consumption. It is not
zero-filled either: over 2026-04-30..2026-08-22 it carried real-looking values on
5270 of 9216 slots, and because it bears the real `community_id` it was **not**
filtered out of community-scoped queries. Ingestion discards it as an intentional
skip (archived, not failed) — by the same rule as the copies above: a metering
point reports only its own direction. Tracked as Q11a in `PROVIDER_QUESTIONS.md`.

---

## Energy Codes Explained

### Code Types

There are two code systems used:

#### 1. **VSE National Codes** (Swiss national standard)

Used for **energy breakdown** by source/destination:

| Code | Meaning | Description |
|------|---------|-------------|
| `2404050010123` | **CEL Local** | Energy exchanged within the community |
| `2404050010124` | **Grid** | Energy exchanged with external electricity provider |

**Usage in files**:
- **Consumption files**: Where did the consumed energy come from?
  - CEL Local = consumed from community production
  - Grid = consumed from electricity provider
- **Production files**: Where did the produced energy go?
  - CEL Local = produced and consumed within community  
  - Grid = produced and exported to electricity provider

#### 2. **ebIX Codes** (International standard)

Used for **total values**:

| Code | Meaning | Description |
|------|---------|-------------|
| `8716867000030` | **Total** | Total energy (sum of all sources/destinations) |

**Mathematical relationship** (expected):
```
Total Consumption = CEL Local Consumption + Grid Consumption
Total Production = CEL Local Production + Grid Export
```

**Reality**: Due to Condition 21 (estimated data), these don't always add up perfectly.

---

### Metering Point Types

Each file represents either consumption or production:

#### Consumption (`<ConsumptionMeteringPoint>`)

**Energy flowing INTO the member's household**

Sources can be:
- CEL Local: From community member's solar panels
- Grid: From electricity provider
- Total: Sum of both

#### Production (`<ProductionMeteringPoint>`)

**Energy flowing OUT FROM the member's solar panels**

Destinations can be:
- CEL Local: Consumed by other community members
- Grid: Exported to electricity provider  
- Total: Sum of both

---

### Flow Characteristics (E31 only)

Community aggregate files use flow codes:

| Code | Meaning | Description |
|------|---------|-------------|
| `E17` | **Consumption flow** | Community's total incoming energy |
| `E18` | **Production flow** | Community's total outgoing energy |

---

## File Types and Structure

### E66 Files (Individual Meters)

**Format**: ValidatedMeteredData_1.6  
**Count**: 103 files/day  
**Purpose**: Individual member energy data

**File naming pattern**:
```
YYYYMMDD_HHMMSS_<sender>_E66_<receiver>_<uuid>.xml
20260527_094557_12X-0000001536-1_E66_12X-00000020FW-5_18eb21f1-59a0-11f1-cce3-00000084413a.xml
```

**Content structure**:
```xml
<ValidatedMeteredData_16>
  <HeaderInformation>
    <DocumentType>E66</DocumentType>
    <!-- Meter identification -->
    <VSENationalID>CH101110123450000000000000217130Y</VSENationalID>
  </HeaderInformation>
  
  <MeteringData>
    <!-- Type: Consumption or Production -->
    <ConsumptionMeteringPoint>...</ConsumptionMeteringPoint>
    
    <!-- Product: What's being measured -->
    <Product>
      <ID>
        <ebIXCode>8716867000030</ebIXCode>  <!-- OR -->
        <VSENationalCode>2404050010123</VSENationalCode>
      </ID>
    </Product>
    
    <!-- 480 observations (5 days × 96 intervals) -->
    <Observation>
      <Position><Sequence>1</Sequence></Position>
      <Volume>1.234</Volume>
      <Condition>21</Condition>  <!-- Optional quality flag -->
    </Observation>
    ...
  </MeteringData>
</ValidatedMeteredData_16>
```

**E66 File Distribution** (one sample delivery; the counts follow membership,
see `config/customers.yaml`):
```
Consumption points, customer produces: 9 × 4 files = 36 files
Consumption points, customer does not: 12 × 3 files = 36 files
Production points:                     9 × 3 files = 27 files
0134575W (uncoupled production point): 1 × 4 files =  4 files
                                                Total: 103 files
```

### E31 Files (Community Aggregates)

**Format**: AggregatedMeteredData_1.3  
**Count**: 6 files/day  
**Purpose**: Community-level totals

**File naming pattern**:
```
YYYYMMDD_HHMMSS_<sender>_E31_<receiver>_<uuid>.xml
20260527_094741_12X-0000001536-1_E31_12X-00000020FW-5_813bf77c-5a69-11f1-b257-00000084413a.xml
```

**Content structure**:
```xml
<AggregatedMeteredData_13>
  <HeaderInformation>
    <DocumentType>E31</DocumentType>
    <BusinessReasonType>
      <VSENationalCode>C40</VSENationalCode>
    </BusinessReasonType>
  </HeaderInformation>
  
  <MeteringData>
    <!-- NO individual meter ID - this is community aggregate -->
    
    <!-- Community identification -->
    <Community>
      <CommunityID>101110-002726</CommunityID>
      <CommunityType>CT01</CommunityType>
    </Community>
    
    <!-- Flow type: E17 (consumption) or E18 (production) -->
    <FlowCharacteristic>E17</FlowCharacteristic>
    
    <!-- Product code (ebIX or VSE) -->
    <Product>
      <ID><ebIXCode>8716867000030</ebIXCode></ID>
    </Product>
    
    <!-- 480 observations (same structure as E66) -->
    <Observation>...</Observation>
  </MeteringData>
</AggregatedMeteredData_13>
```

**E31 File Distribution**:
```
Consumption (E17):
  - Total (ebIX 8716867000030)         = 1 file
  - CEL Local (VSE 2404050010123)      = 1 file
  - Grid (VSE 2404050010124)           = 1 file

Production (E18):
  - Total (ebIX 8716867000030)         = 1 file
  - CEL Local (VSE 2404050010123)      = 1 file  
  - Grid (VSE 2404050010124)           = 1 file
                                    Total: 6 files
```

---

## Consumption vs Production Metering Points

### Why two ids?

A **consumption metering point**'s files carry:
- ✅ **Total flows**: total consumption in, and a copy of the customer's total
  production out
- ✅ **Consumption breakdown**: where the consumed energy came from
- ❌ **NOT the production breakdown**: where the produced energy went

A **production metering point**'s files carry:
- ✅ **VSE production breakdown**: CEL Local vs Grid split for production
- ✅ The **production total** — the copy that is stored
- ❌ **NOT real measurements** of the breakdown: those are estimated/calculated

**RCP meters** (Regroupement pour la Consommation Propre) — *hypothetical*:
- ✅ **Grid connection point**: Measures net exchange for multiple units
- ✅ **Both consumption & production**
- ✅ **Gets breakdown data**: Participates in CEL trading
- Example: Apartment building with shared solar, only net grid exchange metered
- ⚠️ **No RCP meter is confirmed in the data.** `0134575W` was once assumed to be
  RCP but is **not** — it is a production metering point with no coupled
  consumption point (see
  [A production point that is not coupled](#a-production-point-that-is-not-coupled-0134575w)).

### Ownership is declared, not inferred

**The linkage is nowhere in the XML.** A production file carries its own
`ProductionMeteringPoint/VSENationalID` and a `Community` block, and nothing that
names the customer or the other metering points of the same site.

**So the provider declares it**: a list of customers and the metering points each
one owns, kept as `config/customers.yaml`. Every file is stored under its own
meter id, with the customer id looked up from that list — which depends only on
the file and the declaration, never on what else is in the batch. Deliveries
routinely arrive in waves hours apart, and a late or retried file is decided on
its own.

### The declaration

**Location**: `/app/config/customers.yaml` (deploy artifact, gitignored because
it holds real meter ids; `config/customers.yaml.example` is the tracked template).
Its content comes from the provider.

```yaml
customers:
  "9000115":
    consumption:
      - "CH101110123450000000000000046782G"
    production:
      - "CH10111012345000000000000008552310"
  "9000112":                                 # does not produce
    consumption:
      - "CH101110123450000000000000036273C"
```

**It is validated at startup, and every error raises** (`scripts/meters.py`):
a missing file, an unknown section, an
unknown role, an empty role list, an id shorter than 20 characters, the same id
declared twice anywhere, a file that declares no meter. The job **refuses to
start** rather than ingest a delivery whose meters land on the wrong customer —
which no later query could detect.

Full 33-char ids only; ids are never sliced, because an 8-character suffix is
not an id a file can be matched against.

### One rule: a metering point reports only its own direction

A file's direction is its XML element (`ConsumptionMeteringPoint` /
`ProductionMeteringPoint`); the meter's **role** is its declaration. When they
disagree:

| File | The customer owns a meter of the file's direction | Outcome |
|------|---------------------------------------------------|---------|
| CEL or grid breakdown | either | `FAILED`, ERROR: the declared role is wrong |
| total | yes — that data is stored there | `SKIPPED`, INFO, archived |
| total | no — nothing else carries it | `FAILED`, ERROR, kept in incoming for retry |

**A breakdown always names its meter's role.** A metering point measures only
its own direction, so a CEL or grid file against the role means `customers.yaml`
declares the wrong role. Skipping it would archive that meter's data with no
error whenever the customer owns another meter of that direction, so it fails.

Two kinds of total file hit the skip line:

- a producing customer's consumption point sends a **copy** of the production
  total, which the production point carries too;
- `0134575W`, a production point, gets **consumption total** files — a provider
  fault (Q11a in `PROVIDER_QUESTIONS.md`).

The last line is the case worth stopping for: a consumption-only customer
whose consumption point starts reporting production has most likely installed
solar, and the list has not caught up. Dropping it quietly would lose real
production.

### Why the role is declared

The role cannot be read off a file. A breakdown file shows its meter's
direction, but a **total** file does not tell which meter is the real source:

- a production total is the production point's own total, or the copy the
  customer's consumption point sends;
- a consumption total is the meter's own, or a spurious file on a production
  point (`0134575W`).

Deciding which copy to store needs the meter's role. Deriving it from data
means looking at that meter's breakdown files, and they are not in hand when a
total is processed:

- **From the same delivery:** deliveries arrive in waves hours apart, so a total
  can arrive before or after its meter's breakdown, and a retried file is
  processed on its own.
- **From earlier deliveries:** the result would depend on what was ingested
  before. A new meter's first total would have to be held until a breakdown
  arrived, and the replay order would decide what is stored.

Declared, the role makes every file a per-file decision: the same file and the
same `customers.yaml` give the same outcome, whatever else is in incoming. The
breakdown files then **check** the declaration (the first line of the table), so
a wrong role fails on the first delivery instead of filing data under the wrong
direction.

Everything else is stored under the file's own meter id with its `customer_id`.
A meter not in the list — the RCP meters — is stored with `customer_id` NULL
(and a WARNING if the file is not RCP). `cel_file_header` records each file's
`meter_id` and `customer_id`.

### Intentional skips are not errors

The rule above drops files on purpose: **one production-total copy per
consumption point of a producing customer**, plus the spurious consumption total
files for `0134575W`. Ingesting the copies would double the community production total.

So the expected skip count follows `config/customers.yaml` — read it off the
file rather than memorising it. It rises whenever a customer installs solar, and
a run whose skip count suddenly *falls* is the signal worth chasing: it means a
consumption point stopped sending the copy, or a customer's production point is
no longer declared.

This is an *expected* outcome, so `parse_e66` returns a **`SkippedDocument`**
(`scripts/models.py`) rather than `None`:

| Parser return | Meaning | Watcher behaviour |
|---------------|---------|-------------------|
| `MeteredData` | parsed | write to QuestDB, archive (`FileOutcome.INGESTED`) |
| `SkippedDocument` | valid, deliberately not ingested | log at **INFO**, archive (`FileOutcome.SKIPPED`) |
| `None` | genuine failure (malformed, a direction nothing else carries, missing fields) | log at WARNING/ERROR, **not archived** (`FileOutcome.FAILED`); the next run moves it to `incoming/failed/` and re-downloads it |

Skipped files are archived like ingested ones because the decision is permanent
— leaving them in `/data/incoming` would make them reappear (and be re-reported)
on every delivery. The batch summary counts them separately:

```
Ingested: 115, Skipped by design: 11, Errors: 0
```

Only `Errors` is expected to be 0 on every run; the other two follow the
membership and the declaration. A delivery carrying two report periods has one
skip set per wave, so twice the usual count is equally expected there.

### Reading the current declaration

**`config/customers.yaml` is the list — this guide does not copy it.**
Membership changes (a customer joins, a customer installs solar and gains a
production metering point), so any figure reproduced here would be wrong by the
time it mattered. `load_meters` logs the customer, consumption-meter and
production-meter counts at startup, so a run's log records the declaration it
actually used. To see it without running the job:

```bash
python3 -c "from scripts.meters import load_meters; \
            m = load_meters('config/customers.yaml'); \
            print(len(m.customers), 'customer(s),', len(m), 'meter(s)')"
```

---

## The Declared Meters

### What the declaration is checked against

Trusting a declaration means a wrong line in it would mis-file silently. A
production metering point's ebIX total repeats the production-total copy of its
customer's consumption point slot for slot, and that exact `Decimal` equality
confirms the declaration. It lives in
the delivery report (`scripts/delivery_report.check_customer_production`),
**not** in the ingest path:

- Per report period and per customer, the production-total copies reported by
  its consumption point(s) are summed and compared, slot by slot and as exact
  `Decimal`s, with the summed totals of its production point(s). A disagreement
  is logged at **ERROR**, naming the customer and how many slots differ.
- A customer is checked only when its consumption side reports a copy **and**
  every one of its production points is present in the group. Deliveries arrive
  in waves, so half a customer is normal, and reporting it would bury the real
  finding. `0134575W` has no copy, so it is never checked.
- The report is a diagnostic wrapped in try/except, so it can **never** fail the
  ingestion it describes. Ingestion stays strictly per-file.

### When the declaration is read

Once, at startup, by `SDATProcessor.from_config`. There is nothing to refresh per
batch and nothing cached: the same `Meters` object serves every file of every
delivery in the run.

A new customer therefore needs the declaration updated **before** their files
arrive. Until then their files are stored with no customer (WARNING logged), and
a replay after the update fills it in. A customer gaining a production point is
stricter: until it is declared, the production total from their consumption
point fails (ERROR, kept in `/data/incoming`) and is ingested on the retry after
the update — nothing is lost.

---

## Data Processing Flow

### Overall Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  FTP Server (Provider)                                      │
│  ├─ N E66 files (individual meters; N tracks membership)    │
│  └─ 6 E31 files (community aggregates; always 6)            │
└────────────────┬────────────────────────────────────────────┘
                 │ Daily delivery (09:45-09:50)
                 ↓
┌─────────────────────────────────────────────────────────────┐
│  /data/incoming (Synology NAS)                              │
│  Watch directory - files collected here                     │
└────────────────┬────────────────────────────────────────────┘
                 │ Batch processing (10-minute timer)
                 ↓
┌─────────────────────────────────────────────────────────────┐
│  Parser Container (cel-parser)                              │
│  ├─ Declared meters (config/customers.yaml, at startup)     │
│  ├─ E66 parser (parse_sdat_e66_individual.py)                          │
│  ├─ E31 parser (parse_sdat_e31_aggregated.py)                    │
│  └─ Batch processor (sdat_processor.py)                     │
└────────────────┬────────────────────────────────────────────┘
                 │ INSERT over PG-wire (psycopg), Decimal values
                 ↓
┌─────────────────────────────────────────────────────────────┐
│  QuestDB (Time-series database, system of record)            │
│  cel_energy (E66) / cel_community_energy (E31)              │
│  DEDUP UPSERT KEYS -- the newest delivered value wins       │
└────────────────┬────────────────────────────────────────────┘
                 │ SQL queries
                 ↓
┌─────────────────────────────────────────────────────────────┐
│  Grafana (Visualization)                                    │
│  ├─ Per-meter and per-customer dashboards                   │
│  └─ Community aggregate dashboards                          │
└─────────────────────────────────────────────────────────────┘
```

### Batch Processing Flow

**Why batch processing?**
- ✅ Avoids race conditions (a file still being written is not parsed)
- ✅ One summary and one delivery report per delivery, not per file
- ✅ Amortises the archive scan and the header pass over the whole delivery

Not a reason: a file's meter and customer do not need the batch to be complete. They are a per-file lookup in the declared meters, so a
file arriving in a later wave — or retried after a failure — is decided on its
own.

**Process**:

```
1. First file arrives (e.g., 20260627_094501_...xml)
   ├─ Extract date: 20260627
   ├─ Create new batch
   └─ Start 10-minute timer

2. More files arrive (20260627_094502...xml, etc.)
   ├─ Added to same batch
   └─ Timer resets with each file

3. No files for 10 minutes OR next day's files arrive
   ├─ Trigger batch processing
   └─ If new delivery: process previous batch immediately

4. Batch processing:
   ├─ Read every file's header once (sdat_header.load_headers)
   ├─ Look up each file's customer and role in the declared meters
   ├─ Write rows to QuestDB
   └─ Archive processed files (a failed write keeps the file for retry)

5. Ready for next batch
```

**Delivery detection**:
- Uses filename date prefix (first 8 characters: `YYYYMMDD`)
- Handles any file count (109, 115, 200...) automatically
- No hardcoded file count limits

**Batch order is load-bearing.** Because storage is last-write-wins, batches must
reach the database in ascending delivery order. Live ingestion and the startup
rescan (`sorted(glob)`) already satisfy this; any manual replay must sort by the
`YYYYMMDD` prefix explicitly, or an older delivery will overwrite newer values.
See [QUESTDB.md](QUESTDB.md#chronological-replay-is-a-correctness-requirement).

### Parsing Steps

**For each E66 file**:

1. **Parse XML** → Extract meter ID, metering point type, product code, observations
2. **Look up the meter** → is this id declared, and as what?
3. **Check its direction** → a file reporting against its meter's role is
   skipped (a copy, or the provider fault) or failed; otherwise the customer id
   is attached
4. **Classify** → `product_code` + metering point → `direction` + `segment`
5. **Write** → `INSERT` into `cel_energy`
6. **Archive** → Move file to `/data/archive`

**For each E31 file**:

1. **Parse XML** → Extract community ID, flow type, product code, observations
2. **Classify** → flow E17/E18 + `product_code` → `direction` + `segment`
3. **Write** → `INSERT` into `cel_community_energy`
4. **Archive** → Move file to `/data/archive`

### Stored Rows

Values are `decimal.Decimal` end to end, never `float`: they are parsed from the
XML text straight into a `DECIMAL(12,3)` column, so what is stored is exactly what
the provider sent. Full schema in [QUESTDB.md](QUESTDB.md#schema).

**E66 Individual Meter Data** → `cel_energy`:

| ts | meter_id | customer_id | direction | segment | product_code | community_id | value | code_type | condition |
|----|----------|-------------|-----------|---------|--------------|--------------|-------|-----------|-----------|
| 2026-05-22T00:00:00Z | CH101110123450000000000000217130Y | 9000110 | consumption | total | 8716867000030 | 101110-002726 | 1.234 | ebIXCode | |

**E31 Community Aggregate Data** → `cel_community_energy`:

| ts | direction | segment | product_code | community_id | value | code_type | community_type | grid_area | condition |
|----|-----------|---------|--------------|--------------|-------|-----------|----------------|-----------|-----------|
| 2026-05-22T00:00:00Z | consumption | total | 8716867000030 | 101110-002726 | 1.930 | ebIXCode | CT01 | 12Y-0000000719-J | 21 |

`direction` and `segment` replace the old flow/product encoding in queries:
`E17 → consumption`, `E18 → production`; `8716867000030 → total`,
`2404050010123 → cel`, `2404050010124 → grid`. The `product_code` is still stored,
but panels filter on `segment` because it is derived and so survives a
provider-side encoding change.

### Deduplication

**File-level**:
- Processed files tracked in memory + loaded from archive at startup
- If file already in archive: Add timestamp suffix to avoid overwriting
- Example: `file_20260626_174530.xml` (timestamped duplicate)

**Data-level** — handled by the tables' `DEDUP UPSERT KEYS`:

- A row's identity is its key tuple:
  `(ts, meter_id, direction, segment, product_code, community_id)` for E66,
  `(ts, direction, segment, product_code, community_id)` for E31.
- The provider sends **overlapping 5-day files daily**, so each slot is written
  5-7 times, and ~2.6% of overlapping slots are **revised** — sometimes downward
  (meter `0050170B`, 2026-05-22T00:00: `0.003` on delivery 20260527 → `0.002` on
  20260605). `DEDUP UPSERT KEYS` is genuine last-write-wins, so the newest
  delivered value replaces the stored row and revisions land in both directions.
- Re-writing a byte-identical row is a no-op QuestDB detects and skips.
- `condition` and `code_type` are deliberately **not** keys. The provider revises
  a slot's condition (estimated → measured) across deliveries; as a key, that
  slot would become two rows and every `sum()` would double-count it.

> **The cost of last-write-wins: replay order.** Nothing consults a delivery date
> — whichever `INSERT` runs last wins. Replaying delivery `20260527` after
> `20260605` silently regresses 4 days, and the database cannot detect it
> afterwards, since there is only ever one row per key. Always replay in ascending
> delivery order.

**Result**: reprocessing files **is** value-idempotent, provided deliveries are
fed in chronological order.

---

## Data Quality Flags

### Condition Code 21

**Most important quality indicator!**

**XML representation**:
```xml
<Observation>
  <Volume>1.234</Volume>
  <Condition>21</Condition>  ← This!
</Observation>
```

**Meaning** (per SDAT specifications):
- **Estimated/Calculated data**
- NOT directly measured at the metering point
- Calculated using an algorithm/estimation method

**Where we see Condition 21**:
- ✅ **ALL VSE breakdown data** (codes 2404050010123, 2404050010124)
- ✅ **ALL E31 community aggregate data**
- ❌ **NOT on total values** (ebIX 8716867000030) - these are measured

**Example**:
```
Meter 0217130Y:
├─ Consumption Total: 123.45 kWh         (NO condition flag - measured!)
├─ Consumption CEL: 78.90 kWh            (Condition 21 - estimated!)
└─ Consumption Grid: 44.55 kWh           (Condition 21 - estimated!)
```

### Implications

**What we know**:
- ✅ Total values are **measured** (reliable)
- ⚠️ Breakdown values are **estimated** (less reliable)
- ⚠️ Sum of breakdown may not equal total (estimation algorithm)

**Why estimated?**
- **Hypothesis**: Provider lacks metering infrastructure to directly measure energy exchange within community vs with grid
- Uses algorithmic split based on:
  - Total consumption/production (measured)
  - Community generation availability
  - Grid exchange (measured at community level)
  - Estimation algorithm (unknown to us)

**Questions for provider**:
1. What algorithm calculates the CEL/Grid split?
2. Will these become validated measurements in the future?
3. Do you have actual metering for community exchange?

---

## Open Questions for Provider

### High Priority

**File Delivery & Processing**:

1. **File count changes**: We understand E66 file count varies by membership. Questions:
   - Will E31 count always be 6 files (observed as stable)?
   - Do files simply appear/disappear when members join/leave?
   - Is advance notification provided for membership changes?

2. **Import strategy**: With 5-day overlapping files, should we:
   - Process all files (current approach — the newest write wins per slot)
   - Skip overlapping days from older files
   - Process only the newest day from each delivery
   - **Our testing**: May 27 vs May 28 data for same meter/date = 0/96 values differ (100% identical)

3. **Data corrections**: Do you ever update/correct data from previous days in newer deliveries, or are overlapping days always identical?

4. **Delivery completion signal**: Is there a marker file or signal that indicates all files have been delivered? This would help us process files as a complete batch.

**Metering points**:

5. ~~**Official mapping**~~ — **ANSWERED**: the provider supplies the list of
   metering points per customer (`config/customers.yaml`). Remaining ask: notify us **before** the delivery in
   which the list changes.

6. **New members**: When a new member joins, will they automatically get both a
   consumption and a production metering point id? How soon after joining do files
   appear?

7. **Meter 0134575W**: **ANSWERED** — it is a production metering point whose
   total has no copy on a consumption point. Remaining: what does it
   represent, and why does it also receive a consumption total file, which cannot
   be real? See `PROVIDER_QUESTIONS.md` Q11a.

**Data Quality**:

8. **Condition 21 - ALL VSE data**: 100% of VSE breakdown data (codes 2404050010123, 2404050010124) has Condition 21 (estimated). Questions:
   - What estimation algorithm is used?
   - Will this become validated/measured data in the future?
   - Do you have actual metering for CEL community exchange?
   - Should we expect Condition 21 data indefinitely?

9. **Mathematical consistency**: Should this relationship always hold?
   ```
   Total = CEL Local + Grid
   ```
   Currently we see small differences (likely due to estimation). Is this expected?

### Medium Priority

10. **VSE Code definitions**: Can you provide official definitions for:
    - `2404050010123` - Our understanding: CEL Local exchange
    - `2404050010124` - Our understanding: Grid exchange/residual
    - Are these correct?

11. **Flow characteristics** (E31): 
    - `E17` - Our understanding: Consumption flow
    - `E18` - Our understanding: Production flow
    - Correct?

12. **E31 vs E66 consistency**: Should E31 community aggregates match the sum of E66 individual meters? Currently we see small differences.

13. **Missing files**: If a file is missing from expected delivery:
    - Will it be delivered later?
    - Should we wait before processing?
    - Or process whatever arrives?

14. **Historical backfill**: Will May-June 2026 Condition 21 data be replaced with validated measurements, or does estimated data remain as-is?

### Low Priority

15. **Schema files**: Can you provide:
    - `ValidatedMeteredData_1p6.xsd` (for E66 validation)
    - `AggregatedMeteredData_1p3.xsd` (for E31 validation)

16. **Future format changes**: 
    - Any planned XML schema updates?
    - New VSE/ebIX codes coming?
    - Advance notice before changes?

17. **API alternative**: Is there (or will there be) an API to query data instead of file delivery?

18. **Daylight Saving Time**: How are observations handled during DST transitions (spring forward/fall back)?

---

## Glossary

**CEL**: Community Energy Local - The local energy community (ID: 101110-002726)

**CET/CEST**: Central European Time / Central European Summer Time (Europe/Zurich timezone)

**ebIX Code**: International standard code for energy products (e.g., 8716867000030 = Total)

**E66**: Document type for individual meter data (ValidatedMeteredData format)

**E31**: Document type for community aggregated data (AggregatedMeteredData format)

**Customer**: the provider's customer number, owning one or more metering
points. Stored as `customer_id` on every row of its meters.

**Consumption metering point**: the id a customer's consumption is metered
under. For a producing customer it also reports a copy of the production total,
which is dropped.

**Production metering point**: a producing site's id, carrying the VSE
production breakdown and the production total. Its rows are stored under its
own id, like every meter's.

**Declared meters**: `config/customers.yaml`, the provider's list of which
customer owns which metering point, in which role. Ingestion reads it; nothing
infers it.

**VSE National Code**: Swiss national standard code for energy products (e.g., 2404050010123 = CEL Local)

**Condition 21**: Data quality flag indicating estimated/calculated (not measured) values

**Metering Point**: Classification of energy flow direction (Consumption or Production)

**Flow Characteristic**: E31 classification - E17 (consumption) or E18 (production)

**DEDUP UPSERT KEYS**: QuestDB clause making an `INSERT` whose key matches an
existing row *replace* it — last-write-wins, which is what lets the provider's
revisions land

---

## Document History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-06-26 | Initial version - comprehensive parsing guide |
| 1.1 | 2026-09-08 | Consumption/production metering point vocabulary; `0134575W` is production-only and its consumption files are discarded |
| 1.2 | 2026-10-04 | Provider-declared `config/customers.yaml` (customer → meters); every meter is stored under its own id with `customer_id`; the production point's copy of the production total is kept |

---

## Appendix: Example Data Flow

### Complete Example: Customer 9000110 (meters 0217130Y and 08574078)

**Data received** (4 files):

```
File 1: Consumption Total (consumption metering point)
  Meter: CH101110123450000000000000217130Y
  Code: ebIX 8716867000030
  Value: 123.45 kWh
  Condition: (none - measured)

File 2: Consumption CEL Local (consumption metering point)
  Meter: CH101110123450000000000000217130Y
  Code: VSE 2404050010123
  Value: 78.90 kWh
  Condition: 21 (estimated)

File 3: Consumption Grid (consumption metering point)
  Meter: CH101110123450000000000000217130Y
  Code: VSE 2404050010124
  Value: 44.55 kWh
  Condition: 21 (estimated)

File 4: Production Total (consumption metering point)
  Meter: CH101110123450000000000000217130Y
  Code: ebIX 8716867000030
  Value: 234.56 kWh
  Condition: (none - measured)
  → SKIPPED: a consumption point reporting production; the customer's
    production point carries the same total (File 7)
```

**Production metering point files** (3 files):

```
File 5: Production CEL Local (production metering point)
  Meter: CH10111012345000000000000008574078  ← the customer's second id
  Code: VSE 2404050010123
  Value: 123.45 kWh
  Condition: 21 (estimated)
  → stored under 08574078, customer 9000110

File 6: Production Grid (production metering point)
  Meter: CH10111012345000000000000008574078
  Code: VSE 2404050010124
  Value: 111.11 kWh
  Condition: 21 (estimated)
  → stored under 08574078

File 7: Production Total (production metering point)
  Meter: CH10111012345000000000000008574078
  Code: ebIX 8716867000030
  Value: 234.56 kWh  ← identical to File 4
  Condition: (none - measured)
  → stored under 08574078: the copy that is kept
```

**Final data in QuestDB** (each meter under its own id, both with
`customer_id = 9000110`):

```
0217130Y (consumption):
├─ Total: 123.45 kWh (measured)
├─ CEL Local: 78.90 kWh (estimated)
└─ Grid: 44.55 kWh (estimated)

08574078 (production):
├─ Total: 234.56 kWh (measured)
├─ CEL Local: 123.45 kWh (estimated)
└─ Grid: 111.11 kWh (estimated)
```

**Customer dashboard shows**:
- Consumed 123.45 kWh (64% from CEL, 36% from Grid)
- Produced 234.56 kWh (53% to CEL, 47% to Grid)
- Net production: +111.11 kWh
- Self-sufficiency rate: 64%

---

**Questions?**

Contact: [Your contact information]  
Documentation: `/home/copadev/projects/cel/`  
Provider questions: `PROVIDER_QUESTIONS.md`
