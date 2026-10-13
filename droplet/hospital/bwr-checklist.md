| # | Rule (bwrsandman's reviews: PR #431, and his comments on #411, #412, #451, #452) | Result (ok / violated: where / n/a) |
|---|----------------------------------------|-------------------------------------|
| 1 | Clamps use CLAMP (re_common.h) or windef.h min/max, not hand-written selects | |
| 2 | No includes, globals or declarations halfway through a file | |
| 3 | Utility inlines that are not real symbols live in a shared header, not a .cpp | |
| 4 | Matrix/vector maths uses the existing inline helpers (LHMatrix.h, LHPoint.h, LH3DMath.h) | |
| 5 | Fields that are used are named (no field_0x.. in .cpp code) | |
| 6 | Magic values are #defines at the top of the file | |
| 7 | Control flow is finished: the natural form (e.g. MAX(0.0f, x)), not the first shape that matched | |
| 8 | Lists of constants are #defines, not anonymous enums (CitadelHeart.h; #412 GestureSample.h: "I prefer a list of defines than an anonymous enum for named values") | |
| 9 | Same in other game headers (Flock.h: "use a define instead") | |
| 10 | Same in library headers (InfluenceCircle.h: "use a define") | |
| 11 | Same in LH3DIsland.h (#define) | |
| 12 | Oddities of the original (dead variables) are reproduced honestly; no fakematch without the Chief | |
| 13 | No header #ifdef toggles for one translation unit, and no statics in a header under a module guard (#431: "This pattern is Claude getting confused and trying to get a match even if the code is implausible. Likely that this Static lives in the cpp file") | |
| 14 | Existing macros are used, not re-implemented: ARRAY_SIZE (#431: "We have an ARRAY_SIZE macro to do this"), CLAMP / min / max (#431 boundf: "We have a macro for this already"); the change searched include/ and src/ for one first | |
| 15 | No new header for one or two inlines (#431 CameraInline.h: "This doesn't need its own header"); an inline lives in its owning class's header | |
| 16 | Placement has evidence: each new function, macro and static sits in the header or .cpp the evidence points to (Mac symbol, owning class, the TU that uses it), not wherever it compiled (#431 GJUtils Wrap: "Any proof this should be in Gilles Jeremy's utils?"; #412: "Wrong place for this") | |
| 17 | Include spread is minimal: a new header is justified by its consumers (#412 GestureConstants.h: "How many places is this included from?"); constants used by one file stay in that file | |
| 18 | A string literal used in many places is a #define, and an existing one is used (#431 PSysBase.h: "This is a very commonly used string can you put it in a define?") | |
| 19 | Types have evidence: return and parameter types (bool32_t vs uint32_t vs int, const, & vs *) follow the Mac symbol, the asm and the neighbouring declarations, not a guess from the values returned (#412: "Do we know that load and save return bool32_t? ... most of them are marked as uint32_t") | |
| 20 | No deviation from the original code: no BUGFIX / fixes of original bugs; a bug found is described in a comment and reported as an issue (#411: "It's too early to deviate from original code"; "make two issues for the bugs you find") | |
| 21 | No tests, tools or tooling the maintainer did not ask for in a code change (#451: "Not sure I want this test") | |
