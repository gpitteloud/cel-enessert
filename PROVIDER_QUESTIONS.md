# Questions for Energy Provider

## Summary

Through extensive analysis of May-June 2026 data files, we've **confirmed** many aspects of the system. This document contains **26 remaining questions** where we need official confirmation or additional information.

### What We've Confirmed ✓

- **File structure**: Dynamic file count based on membership (example: 109 files = 103 E66 + 6 E31 for 21 members)
- **File types**: E66 (individual meters) vs E31 (community aggregates)
- **Delivery pattern**: Daily at 09:45-09:50, 5-day coverage with 4-day overlap
- **Data stability**: Overlapping days are identical (0% change observed)
- **Meter types**: Physical meters (consumption + production total) vs Virtual meters (production breakdown only)
- **Condition 21**: All VSE breakdown data is estimated, total values are measured
- **Flow characteristics**: E17 (consumption) and E18 (production) in E31 files
- **Community ID**: 101110-002726, Type CT01
- **Physical-virtual mappings**: 9 discovered pairs by matching production totals
- **E31 stability**: Always 6 E31 files regardless of member count

### What We Need From You ❓

- **Official confirmation** of VSE code definitions and meter mappings
- **Operational guidance** on import strategy and handling file variations
- **Technical details** on Condition 21 estimation algorithm
- **Future plans** for validation, format changes, API availability

---

## 1. File Overlap and Delivery Strategy

**Our observation:**
- Each file delivered covers 5 consecutive days
- Files delivered daily with incrementing date ranges
- Example: File delivered 2026-05-27 covers 2026-05-21 to 2026-05-26
- This creates 4-day overlap between consecutive deliveries
- **File count varies by membership**: For current community (21 members, 9 with solar): 109 files = 103 E66 + 6 E31 (always)

**Questions:**
1. **Why 5 days per file?** Is this to provide data stability/corrections, or for technical reasons?

2. **Import strategy:** Should we:
   - A) Process only the newest file for each date (skip older deliveries)?
   - B) Process all files and let newer data overwrite older data?
   - C) Process only the "new day" from each delivery?

3. **Data corrections:** Do you ever update/correct data from previous days in newer deliveries?
   - **Our finding**: Compared same meter/date across May 27 and May 28 deliveries - 0/96 values differed (100% stable)
   - **Question**: Is this guaranteed to always be the case, or could corrections occur in the future?

4. **File delivery pattern:** We observe files delivered daily between 09:45-09:50 (filename timestamp = creation time). Is this the expected pattern? Should we:
   - Wait for all files to arrive before processing (batch processing)?
   - Process files as they arrive (streaming processing)?
   - Is there a "delivery complete" marker file or signal?

5. **File count changes:** We understand E66 file count varies by membership (consumer-only: 3 files, producer: 7 files). Questions:
   - Will E31 count always be 6 files (confirmed stable)?
   - Do E66 files simply appear/disappear when members join/leave?
   - Is advance notification provided for membership changes?
   - Can additional meter types or data products change file structure?

---

## 2. Condition Code 21 - Data Quality and Estimation

**What we confirmed:**
- 100% of VSE breakdown data (codes 2404050010123, 2404050010124) has `<Condition>21</Condition>`
- Total consumption/production data (ebIX code 8716867000030) has NO condition flag (measured)
- Consistent pattern across all files from May 22 through June 20

**Questions:**
5. **Estimation algorithm:** What algorithm calculates the CEL Local vs Grid split?
   - Is it based on community generation availability?
   - Grid exchange measurements?
   - Time-of-day patterns?
   - Other factors?

6. **Future validation:** Will VSE breakdown data ever become validated/measured data?
   - Are you planning to install metering infrastructure for CEL community exchange?
   - Or will Condition 21 (estimated) be permanent?

7. **Metering infrastructure:** Do you have actual meters that measure:
   - Energy exchanged within the CEL community (code 2404050010123)?
   - Energy exchanged with the grid (code 2404050010124)?
   - Or are these calculated from total values + community generation?

8. **Historical data:** Will historical Condition 21 data (May-June 2026) be replaced with validated measurements, or does estimated data remain as-is?

---

## 3. Virtual Meters and Meter Mappings

**What we confirmed:**
- Physical meters provide: consumption total + breakdown, production total only
- Virtual meters provide: production breakdown (CEL Local vs Grid)
- Virtual meters identified by suffix starting with "085"
- We auto-discovered 9 physical-to-virtual pairs by matching production totals

**Discovered mappings:**
```
Physical → Virtual
0217130Y → 08574078
0020576V → 0855229G
0046782G → 08552310
00846565 → 0855227M
01192538 → 0855223Y
0125445D → 08552213
01650626 → 0855219K
0208254A → 0857405E
0803097E → 0855225S
```

**Questions:**
9. **Official confirmation:** Can you confirm these mappings are correct?

10. **Virtual meter purpose:** Are virtual meters created specifically to provide production VSE breakdowns because physical meters don't measure this directly?

11. **Meter 0134575W - RCP (Regroupement pour la Consommation Propre):** This meter has unusual characteristics:
    - Has both consumption and production (like physical meter)
    - Gets production breakdown (like virtual meter)
    - Daily production: 804 kWh (exceeds entire community aggregate of 668 kWh)
    - No matching pair
    
    **Hypothesis**: This represents an RCP (self-consumption group) within the CEL community - a building or group of units that:
    - Share electricity internally (behind one grid connection meter)
    - Have one meter at their grid connection point (0134575W)
    - Only net exchange with main grid is metered
    - Participate in CEL trading as a single entity
    
    **Questions**: 
    - Can you confirm this is an RCP? 
    - How many units/members are behind this meter?
    - Should RCP data be included in community totals or tracked separately?

12. **New members:** When a new member joins:
    - Will they automatically get both physical and virtual meter IDs?
    - Will new files simply appear in the next delivery?
    - Do you provide advance notification with meter IDs?

---

## 4. VSE National Codes - Official Definitions

**Our interpretation (please confirm):**

| Code | Our Understanding | Usage Context |
|------|-------------------|---------------|
| `2404050010123` | CEL Local exchange | Consumption: from CEL<br>Production: to CEL |
| `2404050010124` | Grid exchange | Consumption: from grid<br>Production: to grid |
| `8716867000030` | Total energy | ebIX code: Local + Grid |

**Questions:**
13. **Official definitions:** Can you provide official VSE definitions for codes 2404050010123 and 2404050010124?

14. **Mathematical consistency:** Should `Total = Local + Grid` always hold?
    - We observe small differences (likely due to estimation/rounding)
    - Is this expected, or should they be mathematically exact?

---

## 5. E31 Community Aggregates - Purpose and Consistency

**What we confirmed:**
- E31 files contain community-level totals (not individual meters)
- Flow characteristics: E17 (consumption), E18 (production)
- 6 files daily: 3 consumption (Total, CEL, Grid) + 3 production (Total, CEL, Grid)
- Community ID: 101110-002726, Type: CT01

**Questions:**
15. **E31 purpose:** What is the intended use case for E31 aggregated data?
    - Regulatory reporting?
    - Community dashboards?
    - Cross-validation against E66 sum?
    - Billing/settlement?

16. **E31 vs E66 consistency:** Should E31 community totals exactly match the sum of E66 individual meters?
    - We observe small differences
    - Is this expected (different estimation algorithms) or should they be identical?

---

## 6. Data Completeness and Edge Cases

**Questions:**
17. **Daylight Saving Time:** How are observations handled during DST transitions (spring forward / fall back)?
    - Are there 92 observations (spring) and 100 observations (fall)?
    - Or do you use UTC timestamps to avoid the issue?

18. **Missing observations:** If specific 15-minute intervals are missing due to meter/communication issues:
    - Will they have `<Volume>0.000</Volume>`?
    - Will they be omitted entirely?
    - Will they have a specific condition code?

19. **Missing files:** If a file is missing from a delivery (e.g., only 108 out of 109 files):
    - Will it be delivered in the next batch?
    - Should we wait before processing?
    - Or process whatever arrives?

---

## 7. Future Changes and Compatibility

**Questions:**
20. **Format changes:** Are you planning to:
    - Update XML schema (e.g., ValidatedMeteredData_1.6 → 2.0)?
    - Add new VSE codes or product codes?
    - Change file structure?

21. **Advance notification:** Will we be notified before structural changes are deployed?
    - How much lead time can we expect?

22. **API alternative:** Is there (or will there be) an API to query data instead of file delivery?
    - This would simplify integration and allow on-demand queries

---

## 8. Schema Validation and Documentation

**Questions:**
23. **XSD schema files:** Can you provide schema files for validation?
    - `ValidatedMeteredData_1p6.xsd` for E66 files
    - `AggregatedMeteredData_1p3.xsd` for E31 files

24. **Documentation:** Is there official documentation about:
    - VSE code definitions and usage
    - Condition code meanings (especially Condition 21)
    - Virtual meter concept and purpose
    - Expected file delivery patterns

## 9. Support and Troubleshooting

**Questions:**
25. **Data issues:** When we notice missing or anomalous data:
    - Who should we contact?
    - What information do you need (dates, meter IDs, file names)?

26. **Delivery monitoring:** If files stop arriving:
    - Is there a status page or notification system?
    - What's the expected delivery SLA?
    - How long should we wait before contacting you?

---

## Priority Questions

If you need to prioritize, these are most critical:

**HIGH PRIORITY (affects parser implementation):**
1. **Q2** - Import strategy: process all files or only latest?
2. **Q5** - Will file count (109 files) change when members join/leave?
3. **Q6** - Will Condition 21 data become validated in the future?
4. **Q9** - Confirm our discovered physical→virtual meter mappings are correct
5. **Q13** - Official VSE code definitions (2404050010123, 2404050010124)
6. **Q15** - E31 intended use case
7. **Q16** - Should E31 totals match sum of E66 meters?

**MEDIUM PRIORITY (operational guidance):**
8. **Q3** - Guarantee data stability in overlapping days?
9. **Q5 (estimation)** - What algorithm calculates CEL/Grid split?
10. **Q7** - Do you have actual VSE metering infrastructure?
11. **Q12** - How are new members handled? (advance notification, file structure)
12. **Q19** - How to handle missing files in a delivery?
13. **Q21** - Advance notice of format changes?

**LOW PRIORITY (documentation and tooling):**
14. **Q23** - XSD schema files for validation
15. **Q24** - Additional documentation on codes and concepts
16. **Q22** - Future API availability

---

## Our Current Implementation

For your reference, our parser:

**Batch Processing:**
- Waits for complete daily delivery (~109 files in 5-minute window)
- Refreshes meter mappings before processing each batch
- Handles new member detection automatically

**E66 Files (103/day):**
- Physical meters: Processes consumption (total + breakdown) and production (total only)
- Virtual meters: Auto-discovers mappings by matching production totals, attributes breakdown to physical meters
- Supports all 9 current member pairs + handles new members dynamically

**E31 Files (6/day):**
- Community aggregates stored separately with flow characteristics
- Used for community-level dashboards and validation

**Data Handling:**
- Processes all data regardless of Condition flags (trusts provider data)
- Time-series database handles duplicate timestamps via overwrite
- Tracks processed files to prevent re-processing

**Confirmed File Breakdown (example: community with 21 members):**
```
E66 (ValidatedMeteredData_1.6): 103 files (varies by membership)
  Physical meters with production:     9 × 4 files = 36
  Physical meters without production: 12 × 3 files = 36
  Virtual meters (production):         9 × 3 files = 27
  Special virtual meter (0134575W):    1 × 4 files =  4
                                               Total: 103

E31 (AggregatedMeteredData_1.3): 6 files (always constant)
  Consumption (E17): Total + CEL + Grid = 3
  Production (E18): Total + CEL + Grid  = 3
                                               Total: 6

                              TOTAL FOR THIS COMMUNITY: 109
```

**Note:** E66 file count will change when members join/leave or solar installations change.

This implementation works well, but answers to the above questions will help us optimize and prepare for future changes.

---

## Contact Information

Please send responses to: [YOUR CONTACT INFO]

Related to: CEL Community 101110-002726
Physical meter: CH101110123450000000000000217130Y (and 8 other members)

Thank you for your help in clarifying these points!
