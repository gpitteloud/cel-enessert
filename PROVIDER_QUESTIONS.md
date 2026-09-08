# Questions for Energy Provider

## Summary

Through extensive analysis of May-June 2026 data files, we've **confirmed** many aspects of the system. This document contains **31 remaining questions** where we need official confirmation or additional information.

### What We've Confirmed ✓

- **File structure**: Dynamic file count based on membership (example: 109 files = 103 E66 + 6 E31 for 21 members)
- **File types**: E66 (individual meters) vs E31 (community aggregates)
- **Delivery pattern**: Daily at 09:45-09:50, 5-day coverage with 4-day overlap
- **Data stability**: Overlapping days are identical (0% change observed)
- **Metering points**: a producing member has a **consumption** metering point
  (consumption total + breakdown, production total) and a **production** one
  (the same production total, plus the production breakdown)
- **Condition 21**: VSE breakdown data is *usually* estimated (Condition 21) and
  totals are *usually* measured — but this is **not absolute**: the provider
  **revises a slot's condition across overlapping deliveries** (a given 15-min
  slot can arrive estimated one day and measured the next, on both breakdown and
  total). See Q8a. Because of this, `condition` is deliberately **not** stored as
  a series label (it would split one slot into two series and double-count it).
- **Flow characteristics**: E17 (consumption) and E18 (production) in E31 files
- **Community ID**: 101110-002726, Type CT01
- **The declared meter list**: the provider supplies which consumption id pairs
  with which production id (9 pairs), confirming what we had inferred by matching
  production totals. See section 3.
- **E31 stability**: Always 6 E31 files regardless of member count
- **E31 production = sum of E66 production totals**: exact match once the
  production metering points' duplicate totals are excluded (e.g. 2026-06-15: both
  2558.6 kWh)

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

8a. **Condition revised across deliveries — UNDERSTOOD (confirmed by domain
   knowledge, 2026-07-25):** This is the essence of Condition 21. The provider
   marks a period `Condition 21` when its data is **not yet confirmed
   (provisional)**; in a later delivery it supplies the **confirmed** value for
   that same period and **removes** the `21` flag. So the same 15-min slot can
   arrive `Condition 21` one day and confirmed (no condition) the next — e.g.
   meter 0134575W, slot 2026-07-08 19:15, was `Condition 21` in the delivery
   dated 2026-07-09 and confirmed in the delivery dated 2026-07-10.
   - **Rule we apply:** the **most recent delivery of a slot is authoritative**;
     the earlier (provisional) grade is superseded. We never retain both.
   - **Implementation:** `condition` is NOT a series label, and re-ingesting a
     slot overwrites the previous value in place — so the latest delivery
     naturally wins. (An older parser that labelled by `condition` kept both the
     provisional and confirmed copies as separate series, which double-counted
     on `sum()`.)
   - **Only open confirmation:** none required for behaviour; optionally confirm
     the provider never re-opens a confirmed slot back to provisional.

---

## 3. Metering Points and the Declared Meter List

**ANSWERED — and it settles the vocabulary.** What we had been calling a "virtual"
meter is the site's **production metering point**; the "physical" meter is its
**consumption metering point**. A producing member has two ids, one of each.

**What we confirmed:**
- A consumption metering point reports: consumption total + breakdown, and the
  production total
- A production metering point reports: the same production total, plus the
  production breakdown (CEL Local vs Grid) that only it carries
- Production metering point ids start with `085`
- The pairing is **nowhere in the XML** — a production file carries only its own
  `ProductionMeteringPoint/VSENationalID` and a `Community` block

**ANSWERED — the provider now supplies the list**, so we no longer infer it. It
lives in `config/meters.yaml` (see PARSING_GUIDE.md) with three sections:
`consumption-only`, `consumption-production`, `production-only`. Before that, the
pairing was derived per delivery from the fact that both ids report the same
production total value for value — which needed the whole delivery in hand and so
could not attribute a file arriving in a later wave.

**Declared pairs (9), confirming what we had inferred:**
```
consumption → production
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
9. **ANSWERED** by the declared list above, which matches the 9 pairs we had
   inferred. Remaining: please tell us **before** a delivery when the list changes
   (a new member, a member installing solar), since the job reads it as the source
   of truth and will refuse to attribute an id it has not been told about.

10. **Production metering point purpose:** Is the second metering point created
    specifically to carry the production VSE breakdown, because the consumption
    metering point does not measure it directly?

11. **Meter 0134575W:** This meter has unusual characteristics:
    - Reports its production total *and* VSE breakdown on the same meter ID
    - Daily production: 804 kWh (exceeds entire community aggregate of 668 kWh)
    - Also receives a consumption total file

    **ANSWERED (twice)**: It is **NOT linked to RCP** (Regroupement pour la
    Consommation Propre) — the earlier RCP hypothesis is discarded. It is declared
    **`production-only`**: a production metering point with no consumption
    metering point at all, so its own production total is the canonical one and
    its breakdown is attributed to itself.

    **Which makes the consumption file a fault — see Q11a below.**

    **Remaining questions**:
    - What exactly does this meter represent, if not an RCP?
    - Will other meters of this kind appear (it is currently the only one)?

11a. **Spurious consumption files for `0134575W` — please stop sending them.**
    Since the meter is production-only, the daily consumption total file it
    receives (ebIX `8716867000030`, direction consumption) reports consumption for
    a metering point that has none.

    - It is **not** empty or zero-filled: over 2026-04-30..2026-08-22 it carried
      real-looking values, 5270 of 9216 slots non-zero.
    - It carries the real `community_id`, so unlike the community-less RCP meters
      it was **not** filtered out of community-scoped queries and inflated our
      Sum(E66) consumption by ~2%.
    - We now discard these files on ingest (archived, not failed).

    **Questions**: is this a known defect? Is the data behind it another metering
    point's consumption that has been mislabelled — in which case whose? And is any
    other meter in the community affected?

12. **New members:** When a new member joins:
    - Will they automatically get both a consumption and a production metering
      point id?
    - Will new files simply appear in the next delivery?
    - Do you provide advance notification with meter IDs? **This one now matters
      more than it did**: attribution reads the declared list, so an id that is not
      in it has its production breakdown held back for retry rather than stored
      under a guess.

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
    - **PARTIALLY ANSWERED (our side):** For **production**, E31 total matches the
      sum of E66 production totals **exactly** (e.g. 2026-06-15: E31 = 2558.6 kWh
      vs sum(E66) = 2558.6 kWh, 0.00% diff), once we stop double-counting: a
      production metering point reports the same production total as its
      consumption twin, so we keep one copy.
    - **Remaining question:** confirm that E31 production total is defined as the
      sum of the members' production totals (not something independently
      estimated), so the exact match is guaranteed rather than coincidental.

16a. **E31 consumption is zero from 2026-06-01 onward:** In the delivered files,
    all E31 **consumption** series (E17 -- Total, CEL, and Grid) contain
    `<Volume>0.000</Volume>` for every 15-min interval starting with the file
    dated 2026-06-01 (data date 2026-06-01) through the latest delivery, while
    the corresponding E66 individual-meter consumption is non-zero and E31
    **production** (E18) is populated normally.
    - Late-May files (data dates 2026-05-21 .. 2026-05-29) DO carry non-zero E31
      consumption.
    - **Questions:** Is E31 consumption aggregation broken/disabled from June
      onward, or is this expected? Will it be backfilled? Should we rely on the
      E66 sum for community consumption instead?

16b. **Monthly E31 file overlapping daily files:** One delivery (2026-06-18)
    contained an E31 file with a **31-day** interval (2976 observations, start
    2026-04-30) alongside the usual 5-day daily files. Its data date range
    (from 2026-04-30) predates our E66 coverage.
    - **Questions:** Are monthly/backfill E31 files sent on a schedule? How
      should they be reconciled with the overlapping daily files (which wins)?


16c. **E31 production exceeds sum(E66) by ~9-10% from 2026-07-01 (meter
    `0046782G`):** Meter `0046782G` reports production `0.000` for **every**
    15-min interval from data date **2026-06-23** onward (and also 2026-06-08 ..
    2026-06-17), on **both** its own ebIX total (`8716867000030`) and its paired
    production metering point `08552310`'s VSE CEL/Grid breakdown
    (`2404050010123` / `2404050010124`). The other 9 producers report normally.
    - Through **2026-06-30** this was self-consistent: E31 production total
      equalled sum(E66 production totals) **exactly** (ratio 1.000 every day),
      i.e. E31 excluded this meter too.
    - From **2026-07-01** E31 production is systematically **higher** than
      sum(E66): ratio 0.890-0.937 every day, a shortfall of 30-60 kWh/day
      (~971 kWh over 2026-07-01 .. 2026-07-22, ~9-10%). The missing amount has
      the **shape of a single meter's daily solar profile**, not a flat scale
      factor - consistent with E31 now including `0046782G` while its E66 files
      still contain only zeros. E31/E66 **consumption** stays at ~1.00
      throughout, so this is production-specific.
    - **Questions:** Is `0046782G` still an active producer? If yes, why are its
      E66 files all-zero since 2026-06-23 (meter fault, communication outage,
      or a delivery bug), and will they be corrected/backfilled? If E31 estimates
      or substitutes for a non-reporting meter, please confirm - we need to know
      whether to trust E31 or sum(E66) as the community production total. Also:
      what changed on 2026-07-01 that made E31 start counting it while E66 did
      not?
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
    - The two-metering-point model and its purpose
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

## 10. Specific Data Anomalies Observed

These three were found by reconciling 4368 delivered files (E66 per-meter sums vs
the E31 community aggregate). Each is a concrete, dated discrepancy we cannot
resolve from the data alone.

**Questions:**
27. **E66 files with no `<Community>` element:** ~~Starting with delivery 20260729
    we receive E66 files for 8 meters that carry no `Community/CommunityID`~~
    **Answered from the data, no reply needed.** Those 8 meters
    (`0042214D`, `0042215A`, `0201080P`, `0733915V`, `0854697H`, `0854699B`,
    `0854701T`, `0856898T`) are the RCP meters: their files carry
    `BusinessReasonType/ebIXCode` = `E88` and `Receiver/Role` = `DEC`, and an RCP
    self-consumption grouping has no community, so the absence is correct. They
    are excluded from every community figure by their NULL `community_id`.
    - Still open: `0854699B` and `0854697H` report *identical* consumption series
      (3993.5 kWh each) — is one a duplicate of the other, or do two RCP
      groupings legitimately share a reading?

28. **E31 consumption is zero for 2026-06-02 to 2026-06-24:** For those 23 days
    every E31 consumption value (`total`, CEL `2404050010123` and grid
    `2404050010124`) is `0.000`, while E31 *production* arrives normally and the
    per-meter E66 files show real consumption throughout.
    - Is this a known outage in the aggregation?
    - Can these days be re-delivered with correct values?
    - Going forward, can an unavailable period be delivered as *absent* rows
      rather than zeros? We cannot distinguish "no data" from "genuinely zero
      consumption", so the zeros silently corrupt any average.

29. **Per-meter production falls ~10% short of the E31 aggregate from 2026-07:**
    For May and June, `sum(E66 production totals)` matches E31 production exactly.
    From July it is consistently ~10% lower (July −471 kWh, August −102 kWh).
    - Which meter's production is included in the E31 aggregate but no longer
      delivered as an E66 file?
    - Specifically, meter `0046782G` reports `0.000` production in 1057 of 1064
      July slots and in all 184 August slots, having produced 902.6 kWh in May.
      Is that meter faulty, decommissioned, or is its production now reported
      under a different ID?

---

## Priority Questions

If you need to prioritize, these are most critical:

**HIGH PRIORITY (affects parser implementation):**
1. **Q27** - Are the 8 E66 meters with no `<Community>` element members or not?
2. **Q28** - E31 consumption is zero for 2026-06-02..24 — outage, and re-deliverable?
3. **Q2** - Import strategy: process all files or only latest?
4. **Q5** - Will file count (109 files) change when members join/leave?
5. **Q6** - Will Condition 21 data become validated in the future?
6. **Q11a** - The consumption files for production-only `0134575W` are spurious:
   known defect, or mislabelled data belonging to another metering point?
7. **Q13** - Official VSE code definitions (2404050010123, 2404050010124)
8. **Q15** - E31 intended use case
9. **Q16** - Should E31 totals match sum of E66 meters?

**MEDIUM PRIORITY (operational guidance):**
10. **Q29** - Which meter's production is in E31 but no longer delivered as E66?
11. **Q3** - Guarantee data stability in overlapping days?
12. **Q5 (estimation)** - What algorithm calculates CEL/Grid split?
13. **Q7** - Do you have actual VSE metering infrastructure?
14. **Q12** - How are new members handled? (advance notification, file structure)
15. **Q19** - How to handle missing files in a delivery?
16. **Q21** - Advance notice of format changes?

**LOW PRIORITY (documentation and tooling):**
17. **Q23** - XSD schema files for validation
18. **Q24** - Additional documentation on codes and concepts
19. **Q22** - Future API availability

---

## Our Current Implementation

For your reference, our parser:

**Batch Processing:**
- Waits for complete daily delivery (~109 files in 5-minute window)
- Attribution is a per-file lookup in the declared meter list, so a file arriving
  in a later wave is handled on its own; the batch is only for the summary

**E66 Files (103/day):**
- Consumption metering points: consumption (total + breakdown) and the production
  total
- Production metering points: the production breakdown is stored under the paired
  consumption id, so one member is one meter; the duplicate production total is
  dropped
- Covers the 9 declared pairs; an id not in the declared list is held back for
  retry rather than stored under a guess

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
  Consumption points, member produces: 9 × 4 files = 36
  Consumption-only points:            12 × 3 files = 36
  Production points (breakdown):       9 × 3 files = 27
  Production-only (0134575W):          1 × 4 files =  4
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
Consumption metering point: CH101110123450000000000000217130Y (and 8 other members)

Thank you for your help in clarifying these points!
