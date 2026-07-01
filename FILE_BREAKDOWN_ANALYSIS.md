# Daily File Delivery Breakdown

## Summary

**Daily delivery:** Multiple XML files delivered between 09:45-09:50

**File count varies based on community size:**
- E66 files (ValidatedMeteredData_1.6 format) - varies by member count
- E31 files (AggregatedMeteredData_1.3 format) - always 6 files (community aggregates)

**File count formula:**
- Consumer-only member: 3 E66 files (consumption: total + CEL + Grid)
- Producer member: 4 E66 files (+ production total)
- Virtual meter (per producer): 3 E66 files (production breakdown)
- Community aggregates: 6 E31 files (fixed)

**Example: Community with 21 members (9 with solar, 12 without):**
- 109 files daily = 103 E66 + 6 E31

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
4. Production Total (ebIX 8716867000030)
```

### Pattern 3: Virtual Meters (production breakdown only) - 3 files
```
1. Production Total (ebIX 8716867000030)
2. Production CEL Local breakdown (VSE 2404050010123)
3. Production Grid breakdown (VSE 2404050010124)
```

## Breakdown by Meter Type (E66 files only)

### Physical Meters with Production (9 meters × 4 files = 36 files)
- 0217130Y (user)
- 0020576V
- 0046782G
- 00846565
- 01192538
- 0125445D
- 01650626
- 0208254A
- 0803097E

### Physical Meters without Production (12 meters × 3 files = 36 files)
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

### Virtual Meters (production breakdown) (9 meters × 3 files = 27 files)
- 08574078 → attributed to 0217130Y
- 0855229G → attributed to 0020576V
- 08552310 → attributed to 0046782G
- 0855227M → attributed to 00846565
- 0855223Y → attributed to 01192538
- 08552213 → attributed to 0125445D
- 0855219K → attributed to 01650626
- 0857405E → attributed to 0208254A
- 0855225S → attributed to 0803097E

### Special Case: 0134575W (1 meter × 4 files = 4 files)
This meter represents an **RCP (Regroupement pour la Consommation Propre)** - a self-consumption group within the CEL:
- 1 Consumption Total (ebIX)
- 1 Production Total (ebIX) 
- 2 Production VSE breakdown files

**RCP characteristics:**
- Daily production: 804 kWh (exceeds main community aggregate of 668 kWh)
- Has both consumption & production (grid connection point)
- Gets breakdown data (participates in CEL trading)
- Multiple units behind one grid connection meter
- Internal electricity sharing within RCP, only net exchange metered
- Likely: apartment building or housing cooperative

## File Count Calculation

**Example: Community with 21 members**
- 9 members with solar panels (producers)
- 12 members without solar panels (consumers only)

**E66 files** (ValidatedMeteredData_1.6):
- Physical with production: 9 × 4 = 36 files
- Physical without production: 12 × 3 = 36 files
- Virtual (production breakdown): 9 × 3 = 27 files
- Virtual special case (0134575W): 1 × 4 = 4 files
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
  - Auto-discovery runs before processing
  - Avoid race conditions
  - Better logging (one summary per batch)
