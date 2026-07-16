# CEL Energy Data Parsing Guide

**Version**: 1.0  
**Date**: 2026-06-26

---

## Table of Contents

1. [Overview](#overview)
2. [Data Sources](#data-sources)
3. [Metering Concepts](#metering-concepts)
4. [Energy Codes Explained](#energy-codes-explained)
5. [File Types and Structure](#file-types-and-structure)
6. [Physical vs Virtual Meters](#physical-vs-virtual-meters)
7. [Meter Mapping Discovery](#meter-mapping-discovery)
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

**Note**: Production breakdown (CEL vs Grid) is provided via **virtual meters**

---

### Physical vs Virtual Meters

This is a **key concept** for understanding the data structure:

#### Physical Meters

**Purpose**: Measure actual total energy flows at the physical meter installation

**What they measure**:
- ✅ **Total consumption** (all energy consumed, regardless of source)
- ✅ **Consumption breakdown** (CEL Local vs Grid split)
- ✅ **Total production** (all energy produced by solar panels)

**What they DON'T measure directly**:
- ❌ **Production breakdown** (CEL Local vs Grid split - provided via virtual meters)

**Identifier pattern**: 
- Last 8 characters (suffix): e.g., `0217130Y`, `0046782G`
- Full format: `CH101110123450000000000000217130Y`

#### Virtual Meters

**Purpose**: Provide VSE breakdown **for production only** (CEL Local vs Grid split)

**Why needed**: Physical meters provide consumption breakdown directly in their files, but production breakdown requires virtual meters.

**What they contain**:
- Production CEL Local (VSE code `2404050010123`)
- Production Grid (VSE code `2404050010124`)
- Production Total (ebIX code `8716867000030` - used to match with physical meter)

**Identifier pattern**:
- Last 8 characters start with `085`: e.g., `08574078`, `0855229G`
- Full format: `CH10111012345000000000000008574078`

**Important**: Virtual meter values are **estimated/calculated** (see Condition 21), not directly measured.

#### Example Pairing

**Member with meter suffix `0217130Y`:**

```
Physical Meter: CH101110123450000000000000217130Y
├─ Consumption Total: 123.45 kWh
├─ Consumption CEL Local: 78.90 kWh (estimated)
├─ Consumption Grid: 44.55 kWh (estimated)
└─ Production Total: 234.56 kWh

Virtual Meter: CH10111012345000000000000008574078
├─ Production CEL Local: 123.45 kWh (estimated, attributed to physical)
├─ Production Grid: 111.11 kWh (estimated, attributed to physical)
└─ Production Total: 234.56 kWh (matches physical!)
```

**Key Insight**: Virtual meter's production total matches physical meter's production total. This is how we discover the pairing!

#### Self-Contained Meters (newer pattern)

Some members (e.g. meter suffix `0134575W`, joined ~July 2026) report their
production breakdown **on the same meter ID** as the production total, instead
of via a separate `085`-prefixed virtual meter.

```
Meter: CH101110123450000000000000134575W
├─ Production Total:     851.234 kWh (ebIX 8716867000030)
├─ Production CEL Local: 124.148 kWh (VSE 2404050010123, estimated)
└─ Production Grid:      727.086 kWh (VSE 2404050010124, estimated)
                         (124.148 + 727.086 = 851.234 ✓)
```

**Key differences from the virtual-meter pattern**:
- No separate `085xxxxx` meter — total and breakdown share one meter ID
- No production-total matching needed — the breakdown is attributed to the
  meter **itself**
- The suffix does **not** start with `085`

**How it's handled**: During discovery we collect the set of all meter suffixes
that report an ebIX production total (the "physical production meters"). When a
production file carries VSE breakdown codes and its own suffix is in that set,
the parser attributes the breakdown to itself rather than looking for a virtual
→ physical mapping. See [Meter Mapping Discovery](#meter-mapping-discovery).

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

**E66 File Distribution**:
```
Physical meters with production:     9 × 4 files = 36 files
Physical meters without production: 12 × 3 files = 36 files
Virtual meters (production):         9 × 3 files = 27 files
Special virtual meter (0134575W):    1 × 4 files =  4 files
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

## Physical vs Virtual Meters

### Why Two Types?

**Physical meters** at each household measure:
- ✅ **Total flows**: Total consumption in, total production out
- ❌ **NOT source/destination split**: Where energy came from or went to

**Virtual meters** provide:
- ✅ **VSE breakdown**: CEL Local vs Grid split for production
- ❌ **NOT real measurements**: These are estimated/calculated values

**RCP meters** (Regroupement pour la Consommation Propre):
- ✅ **Grid connection point**: Measures net exchange for multiple units
- ✅ **Both consumption & production**: Functions like physical meter
- ✅ **Gets breakdown data**: Participates in CEL trading
- Example: Apartment building with shared solar, only net grid exchange metered

### The Mapping Challenge

**Problem**: Provider delivers files with both physical and virtual meters, but doesn't tell us which virtual meter corresponds to which physical meter.

**Our solution**: Auto-discovery by matching production totals!

### Discovery Algorithm

**Principle**: If a physical meter and virtual meter have the **same production total**, they belong to the same member.

**Steps**:

1. **Scan files** for production totals (ebIX code `8716867000030`)
2. **Identify meter type** by behavior:
   - Physical: Has both consumption and production files
   - Virtual: Has production files only (provides breakdown)
   - **Note**: Virtual meters typically have suffixes starting with `085`, but this is not absolute
3. **Match by total**:
   ```python
   if abs(physical_total - virtual_total) <= 0.1 kWh:
       # These meters belong to the same member!
       mapping[physical_suffix] = virtual_suffix
   ```

**Example discovery**:
```
Scanning files from 2026-05-27...

Physical meter 0217130Y: production total = 234.567 kWh
Virtual meter 08574078: production total = 234.567 kWh
✓ Match found! 0217130Y <-> 08574078

Physical meter 0046782G: production total = 189.234 kWh
Virtual meter 08552310: production total = 189.234 kWh
✓ Match found! 0046782G <-> 08552310

...

Discovered 9 meter mappings
```

### Two Kinds of Production Breakdown

When the parser encounters a production file carrying VSE breakdown codes
(`2404050010123` / `2404050010124`), it decides where to attribute the
breakdown in this order:

1. **Separate virtual meter** — the suffix is a known `085xxxxx` virtual meter
   present in the discovered mappings → attribute to its paired physical meter.
2. **Self-contained meter** — the suffix is itself a physical production meter
   (it reports an ebIX production total) → attribute the breakdown to itself.
3. **Unknown** — neither of the above → the file is skipped and logged as an
   error (indicates a genuinely new, unrecognized meter).

To support case 2, discovery also builds the set of all suffixes that report an
ebIX production total (`get_physical_production_meters()`), passed to the parser
alongside the virtual→physical mappings.

> **History**: Before July 2026 all members used the separate-virtual-meter
> pattern (case 1). Meter `0134575W` introduced the self-contained pattern
> (case 2); before the parser handled it, its 2 daily production-breakdown files
> were skipped with `Unknown virtual meter 0134575W`.

### Discovered Mappings

Current community mappings (as of June 2026):

| Physical Meter | Virtual Meter | Status |
|----------------|---------------|--------|
| `0217130Y` | `08574078` | ✓ Confirmed |
| `0020576V` | `0855229G` | ✓ Confirmed |
| `0046782G` | `08552310` | ✓ Confirmed |
| `00846565` | `0855227M` | ✓ Confirmed |
| `01192538` | `0855223Y` | ✓ Confirmed |
| `0125445D` | `08552213` | ✓ Confirmed |
| `01650626` | `0855219K` | ✓ Confirmed |
| `0208254A` | `0857405E` | ✓ Confirmed |
| `0803097E` | `0855225S` | ✓ Confirmed |

**Self-contained meter**: `0134575W` (joined ~July 2026) is **not** a virtual
meter and has no mapping entry. It reports its production total *and* its VSE
production breakdown on the same meter ID, so its breakdown is attributed to
itself. See [Self-Contained Meters](#self-contained-meters-newer-pattern).

---

## Meter Mapping Discovery

### When Discovery Runs

**1. At startup** (if no cache exists):
- Scans `/data/incoming` and `/data/archive`
- Creates initial mappings
- Saves to `/app/config/meter_mappings.yaml`

**2. Before batch processing**:
- Checks for new virtual meters in incoming files
- Re-discovers if new meters detected
- Updates cache with new mappings
- Rebuilds the physical-production-meter set (for self-contained meters)

**3. Automatic cache refresh**:
- Cache is valid indefinitely
- Refreshes when new meters detected
- No manual maintenance needed

### Discovery Sources

**Priority order**:

1. **Incoming directory** (`/data/incoming`)
   - Contains files currently being delivered
   - Scanned first for recent data

2. **Archive directory** (`/data/archive`)
   - Contains all previously processed files
   - Used when incoming has <100 files
   - Provides historical data for discovery

**Sampling strategy**:
- If >500 files available: Sample from 3 most recent delivery dates
- Ensures fast discovery while maintaining accuracy

### Cache File Format

**Location**: `/app/config/meter_mappings.yaml`

**Structure**:
```yaml
# Physical to Virtual Meter Mappings
# Auto-discovered by analyzing production totals

meter_mappings:
  '0217130Y':
    virtual_meter: '08574078'
  '0020576V':
    virtual_meter: '0855229G'
  # ... etc
```

**Cache management**:
- ✅ Created automatically on first discovery
- ✅ Updated when new members join
- ✅ No manual editing required
- ✅ Human-readable format for verification

---

## Data Processing Flow

### Overall Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  FTP Server (Provider)                                      │
│  ├─ 103 E66 files (individual meters)                       │
│  └─ 6 E31 files (community aggregates)                      │
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
│  ├─ Meter mapping discovery                                 │
│  ├─ E66 parser (parse_sdat_e66_individual.py)                          │
│  ├─ E31 parser (parse_sdat_e31_aggregated.py)                    │
│  └─ Batch processor (watch_ftproot.py)                      │
└────────────────┬────────────────────────────────────────────┘
                 │ VictoriaMetrics NDJSON format
                 ↓
┌─────────────────────────────────────────────────────────────┐
│  VictoriaMetrics (Time-series database)                     │
│  Stores all energy data with labels                         │
└────────────────┬────────────────────────────────────────────┘
                 │ PromQL queries
                 ↓
┌─────────────────────────────────────────────────────────────┐
│  Grafana (Visualization)                                    │
│  ├─ Individual member dashboards                            │
│  └─ Community aggregate dashboards                          │
└─────────────────────────────────────────────────────────────┘
```

### Batch Processing Flow

**Why batch processing?**
- ✅ All files present before processing (solves new member detection)
- ✅ Discovery finds both physical and virtual meters together
- ✅ Avoids race conditions
- ✅ Better performance (one discovery per delivery, not per file)

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
   ├─ Refresh meter mappings (discovery from incoming + archive)
   ├─ Process all files with updated mappings
   ├─ Send to VictoriaMetrics
   └─ Archive processed files

5. Ready for next batch
```

**Delivery detection**:
- Uses filename date prefix (first 8 characters: `YYYYMMDD`)
- Handles any file count (109, 115, 200...) automatically
- No hardcoded file count limits

### Parsing Steps

**For each E66 file**:

1. **Parse XML** → Extract meter ID, product code, observations
2. **Determine type** → Physical or virtual meter?
3. **Apply mapping** → If virtual, attribute to physical meter
4. **Transform** → Convert to VictoriaMetrics format
5. **Send** → Import to time-series database
6. **Archive** → Move file to `/data/archive`

**For each E31 file**:

1. **Parse XML** → Extract community ID, flow type, product code, observations
2. **Transform** → Convert to VictoriaMetrics format with community labels
3. **Send** → Import to time-series database
4. **Archive** → Move file to `/data/archive`

### VictoriaMetrics Format

**E66 Individual Meter Data**:
```json
{
  "metric": {
    "__name__": "cel_energy_consumed_kwh",
    "project": "cel",
    "meter_id": "CH101110123450000000000000217130Y",
    "product_code": "8716867000030",
    "code_type": "ebIXCode",
    "data_type": "consumption"
  },
  "values": [1.234],
  "timestamps": [1779487200000]
}
```

**E31 Community Aggregate Data**:
```json
{
  "metric": {
    "__name__": "energy_community_aggregate_kwh",
    "community_id": "101110-002726",
    "community_type": "CT01",
    "product_code": "8716867000030",
    "flow_characteristic": "E17",
    "grid_area": "12Y-0000000719-J",
    "data_source": "E31_AggregatedMeteredData",
    "condition": "21"
  },
  "values": [1.93],
  "timestamps": [1779487200000]
}
```

### Deduplication

**File-level**:
- Processed files tracked in memory + loaded from archive at startup
- If file already in archive: Add timestamp suffix to avoid overwriting
- Example: `file_20260626_174530.xml` (timestamped duplicate)

**Data-level**:
- VictoriaMetrics identifies points by: `metric_name + labels + timestamp`
- Same point = overwrite (last write wins)
- Reprocessing files = same data = idempotent operation

**Result**: Safe to reprocess files - no duplicates created!

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
- NOT directly measured by physical meter
- Calculated using an algorithm/estimation method

**Where we see Condition 21**:
- ✅ **ALL VSE breakdown data** (codes 2404050010123, 2404050010124)
- ✅ **ALL E31 community aggregate data**
- ❌ **NOT on total values** (ebIX 8716867000030) - these are measured

**Example**:
```
Physical Meter 0217130Y:
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
   - Process all files (current approach - VictoriaMetrics overwrites duplicates)
   - Skip overlapping days from older files
   - Process only the newest day from each delivery
   - **Our testing**: May 27 vs May 28 data for same meter/date = 0/96 values differ (100% identical)

3. **Data corrections**: Do you ever update/correct data from previous days in newer deliveries, or are overlapping days always identical?

4. **Delivery completion signal**: Is there a marker file or signal that indicates all files have been delivered? This would help us process files as a complete batch.

**Meter Mappings**:

5. **Official mapping**: Can you provide the official mapping of physical meter ID → virtual meter ID for all community members? We've discovered them by matching production totals, but would like to confirm.

6. **New members**: When a new member joins, will they automatically get a physical and virtual meter pair? How soon after joining do files appear?

7. **Virtual meter 0134575W**: This virtual meter has no matching physical meter in our data. Is this intentional? What does it represent?

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

**Physical Meter**: Actual meter installed at member's household, measures total flows

**Virtual Meter**: Software/estimated meter providing VSE breakdown for production

**VSE National Code**: Swiss national standard code for energy products (e.g., 2404050010123 = CEL Local)

**Condition 21**: Data quality flag indicating estimated/calculated (not measured) values

**Metering Point**: Classification of energy flow direction (Consumption or Production)

**Flow Characteristic**: E31 classification - E17 (consumption) or E18 (production)

**NDJSON**: Newline-Delimited JSON - VictoriaMetrics import format

---

## Document History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-06-26 | Initial version - comprehensive parsing guide |

---

## Appendix: Example Data Flow

### Complete Example: Member 0217130Y

**Data received** (4 files):

```
File 1: Consumption Total (Physical meter)
  Meter: CH101110123450000000000000217130Y
  Code: ebIX 8716867000030
  Value: 123.45 kWh
  Condition: (none - measured)

File 2: Consumption CEL Local (Physical meter)  
  Meter: CH101110123450000000000000217130Y
  Code: VSE 2404050010123
  Value: 78.90 kWh
  Condition: 21 (estimated)

File 3: Consumption Grid (Physical meter)
  Meter: CH101110123450000000000000217130Y
  Code: VSE 2404050010124
  Value: 44.55 kWh
  Condition: 21 (estimated)

File 4: Production Total (Physical meter)
  Meter: CH101110123450000000000000217130Y
  Code: ebIX 8716867000030
  Value: 234.56 kWh
  Condition: (none - measured)
```

**Virtual meter files** (3 files):

```
File 5: Production CEL Local (Virtual meter)
  Meter: CH10111012345000000000000008574078  ← Virtual!
  Code: VSE 2404050010123
  Value: 123.45 kWh
  Condition: 21 (estimated)
  → Attributed to physical meter 0217130Y

File 6: Production Grid (Virtual meter)
  Meter: CH10111012345000000000000008574078  ← Virtual!
  Code: VSE 2404050010124
  Value: 111.11 kWh
  Condition: 21 (estimated)
  → Attributed to physical meter 0217130Y

File 7: Production Total (Virtual meter)
  Meter: CH10111012345000000000000008574078  ← Virtual!
  Code: ebIX 8716867000030
  Value: 234.56 kWh  ← Matches physical!
  Condition: (none - measured)
  → Used for mapping discovery
```

**Final data in VictoriaMetrics** (all attributed to physical meter `0217130Y`):

```
Consumption:
├─ Total: 123.45 kWh (measured)
├─ CEL Local: 78.90 kWh (estimated)
└─ Grid: 44.55 kWh (estimated)

Production:
├─ Total: 234.56 kWh (measured)
├─ CEL Local: 123.45 kWh (estimated, from virtual meter)
└─ Grid: 111.11 kWh (estimated, from virtual meter)
```

**Member dashboard shows**:
- Consumed 123.45 kWh (64% from CEL, 36% from Grid)
- Produced 234.56 kWh (53% to CEL, 47% to Grid)
- Net production: +111.11 kWh
- Self-sufficiency rate: 64%

---

**Questions?**

Contact: [Your contact information]  
Documentation: `/home/copadev/projects/cel/`  
Provider questions: `PROVIDER_QUESTIONS.md`
