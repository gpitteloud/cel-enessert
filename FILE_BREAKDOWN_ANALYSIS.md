# Daily File Delivery Breakdown

## Summary

**Daily delivery:** Multiple XML files delivered between 09:45-09:50

**File count varies based on community size:**
- E66 files (ValidatedMeteredData_1.6 format) - varies by member count
- E31 files (AggregatedMeteredData_1.3 format) - always 6 files (community aggregates)

**File count formula:**
- Consumption point of a customer that does not produce: 3 E66 files
  (consumption: total + CEL + Grid)
- Consumption point of a producing customer: 4 E66 files (+ a copy of the
  production total)
- Production metering point: 3 E66 files (production total + breakdown)
- Community aggregates: 6 E31 files (fixed)

A customer owns one or more metering point ids, in the roles above. Which
customer owns which is declared in `config/customers.yaml`; see PARSING_GUIDE.md.

**Example** — a community of 21 members, 9 of them with solar, delivered 109 files a
day (103 E66 + 6 E31). The E66 count follows the membership and the declaration, so
treat it as an illustration of the arithmetic below, not as today's figure.

**For technical details on file structure, product codes, and data quality**, see **[PARSING_GUIDE.md](PARSING_GUIDE.md)**.

## File Structure Per Meter

### Pattern 1: Consumption Only (no solar) - 3 files
```
1. Consumption Total (ebIX 8716867000030)
2. Consumption CEL Local breakdown (VSE 2404050010123)
3. Consumption Grid breakdown (VSE 2404050010124)
```

### Pattern 2: Consumption + Production (has solar) - 4 files
```
1. Consumption Total (ebIX 8716867000030)
2. Consumption CEL Local breakdown (VSE 2404050010123)
3. Consumption Grid breakdown (VSE 2404050010124)
4. Production Total (ebIX 8716867000030)  -- a copy, skipped on ingest
```

### Pattern 3: Production Metering Points (production breakdown) - 3 files
```
1. Production Total (ebIX 8716867000030)
2. Production CEL Local breakdown (VSE 2404050010123)
3. Production Grid breakdown (VSE 2404050010124)
```

## Breakdown by Meter Type (E66 files only)

One sample delivery; `config/customers.yaml` is the current list.

### Consumption Metering Points, customer produces (9 meters × 4 files = 36 files)
- 0217130Y (user)
- 0020576V
- 0046782G
- 00846565
- 01192538
- 0125445D
- 01650626
- 0208254A
- 0803097E

### Consumption Metering Points, customer does not produce (12 meters × 3 files = 36 files)
- 0036273C
- 0050170B
- 0060545I
- 0062412W
- 0078872J
- 0164750O
- 0198918Z
- 0199054X
- 02291991
- 0229599I
- 0832199P
- 0858140M

### Production Metering Points (9 meters × 3 files = 27 files)
Each is stored under its own id; the consumption point(s) its customer owns
(from `config/customers.yaml`, not inferred from the batch):
- 08574078 — customer of 0217130Y
- 0855229G — customer of 0020576V
- 08552310 — customer of 0046782G
- 0855227M — customer of 00846565
- 0855223Y — customer of 01192538
- 08552213 — customer of 0125445D
- 0855219K — customer of 01650626 (and of 02291991, which reports no copy)
- 0857405E — customer of 0208254A
- 0855225S — customer of 0803097E

### Special Case: 0134575W (1 meter × 4 files = 4 files)
This meter is **NOT linked to RCP** (Regroupement pour la Consommation Propre /
self-consumption grouping). It is a production point whose total has **no copy**:
its customer's consumption point (`0832199P`) reports no production total. Files:
- 1 Production Total (ebIX) — kept, the only copy
- 2 Production VSE breakdown files (stored under `0134575W`, like any production point)
- 1 Consumption Total (ebIX) — **spurious, a provider fault**, skipped on ingest

**Characteristics:**
- Daily production: 804 kWh (exceeds main community aggregate of 668 kWh)
- Gets breakdown data (participates in CEL trading)
- Reports its production total *and* VSE breakdown on the same meter ID
- The consumption file it also gets is not real consumption. It carries the real
  `community_id`, so it was not filtered out of `segment='total'` queries and
  inflated Sum(E66) consumption until the declaration named the meter a
  production point. See PROVIDER_QUESTIONS.md and QUESTDB.md.

## File Count Calculation

**Example: Community with 21 members**
- 9 members with solar panels (producers)
- 12 members without solar panels (consumers only)

**E66 files** (ValidatedMeteredData_1.6):
- Consumption points, customer produces: 9 × 4 = 36 files
- Consumption points, customer does not: 12 × 3 = 36 files
- Production points: 9 × 3 = 27 files
- Special case (0134575W): 1 × 4 = 4 files
- **Subtotal: 36 + 36 + 27 + 4 = 103 files**

**E31 files** (AggregatedMeteredData_1.3):
- Community aggregated data: **Always 6 files** (regardless of member count)

**TOTAL for this example: 103 + 6 = 109 files**

**Observed for this community** (May 2026):
- 2026-05-27: 109 files (103 E66 + 6 E31)
- 2026-05-28: 109 files (103 E66 + 6 E31)
- 2026-05-29: 109 files (103 E66 + 6 E31)
- 2026-05-30: 109 files (103 E66 + 6 E31)

✅ **Pattern: Consistent file count based on stable membership**

**Note:** File count will change when:
- New members join the community
- Members install/remove solar panels
- Members leave the community

## File Naming Pattern

```
YYYYMMDD_HHMMSS_<sender>_<type>_<receiver>_<uuid>.xml

Example (E66 - ValidatedMeteredData):
20260528_094601_12X-0000001536-1_E66_12X-00000020FW-5_458cdfdb-5a69-11f1-cb84-00000084413a.xml
│        │         │                  │    │                 │
│        │         │                  │    │                 └─ UUID (unique per file)
│        │         │                  │    └─ Receiver ID
│        │         │                  └─ Document type (E66 or E31)
│        │         └─ Sender ID (provider)
│        └─ Creation timestamp (HH:MM:SS)
└─ Creation date (YYYY-MM-DD)
```

**Document types**:
- **E66**: ValidatedMeteredData_1.6 (individual meter data) - 103 files/day
- **E31**: AggregatedMeteredData_1.3 (community aggregated data) - 6 files/day

**Important**: Filename timestamp = file creation time, NOT data date
- Data date is inside XML in `<StartDateTime>` and `<EndDateTime>`

## Delivery Window

- **Start**: ~09:45
- **End**: ~09:50
- **Duration**: 2-5 minutes
- **Frequency**: All files delivered within this window

## Processing Implications

**Current approach** (streaming): Process each file as it arrives
- Example: 109 files in 5 minutes = ~22 files/minute = ~1 file every 3 seconds
- Risk: Backlog if processing takes longer than arrival rate

**Implemented approach** (batch): Wait for complete delivery
- Detect first file with new delivery date
- Wait 10 minutes after last file arrival
- Process entire batch
- Benefits:
  - Can handle missing files gracefully
  - Can handle variable file counts (different member counts)
  - Avoid race conditions
  - Better logging (one summary per batch)

Storing a file does not need the batch to be complete: its customer and role
are a per-file lookup in `config/customers.yaml`, so a file arriving in a later
wave — or retried after a failure — is decided on its own. Batching is only
about the summary and the delivery report.
