| # | Rule (bwrsandman's review of PR #431) | Result (ok / violated: where / n/a) |
|---|----------------------------------------|-------------------------------------|
| 1 | Clamps use CLAMP (re_common.h) or windef.h min/max, not hand-written selects | |
| 2 | No includes, globals or declarations halfway through a file | |
| 3 | Utility inlines that are not real symbols live in a shared header, not a .cpp | |
| 4 | Matrix/vector maths uses the existing inline helpers (LHMatrix.h, LHPoint.h, LH3DMath.h) | |
| 5 | Fields that are used are named (no field_0x.. in .cpp code) | |
| 6 | Magic values are #defines at the top of the file | |
| 7 | Control flow is finished: the natural form (e.g. MAX(0.0f, x)), not the first shape that matched | |
| 8 | Lists of constants are #defines, not anonymous enums (CitadelHeart.h) | |
| 9 | Same in other game headers (Flock.h: "use a define instead") | |
| 10 | Same in library headers (InfluenceCircle.h: "use a define") | |
| 11 | Same in LH3DIsland.h (#define) | |
| 12 | Oddities of the original (dead variables) are reproduced honestly; no fakematch without the Chief | |
| 13 | No header #ifdef toggles for one translation unit | |
