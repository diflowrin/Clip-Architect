Subtitle fonts bundled with the application.

Every file here is licensed under the SIL Open Font License 1.1, which permits
redistribution inside a commercial application as long as the licence travels
with the font. The full text of each one - with its own copyright line - ships in
licenses/OFL-1.1-NotoSans.txt, licenses/OFL-1.1-BeVietnamPro.txt and
licenses/OFL-1.1-Charm.txt; the reasoning is in LICENSES-third-party.md in the
project root.

  Noto Sans       - Latin + Greek + Cyrillic, the default subtitle face
  Be Vietnam Pro  - Latin + Vietnamese
  Charm           - Latin + Thai, a decorative alternative

Noto Sans became the default on 2026-08-30. Be Vietnam Pro, which held that
place before it, has no s-comma or t-comma (U+0219, U+021B) - two of the five
letters Romanian needs - and a font draws nothing at all for a glyph it lacks,
so those subtitles arrived with holes in them. Both older faces still ship and
can still be chosen; subtitle_text_for_font substitutes whatever the chosen font
cannot draw.

Upstream MoneyPrinterTurbo also shipped Microsoft YaHei, STHeiti and
UTM Kabel KT. Those are proprietary (Microsoft, Changzhou SinoType and Thien
Minh respectively) and cannot be redistributed in a Store package, so they were
removed; they are kept out of the build in packaging/removed-fonts/ for
reference only.

Adding a CJK face means picking an openly licensed one - Noto Sans SC/TC or
Source Han Sans, both OFL - and dropping the .otf/.ttf here. It is discovered
automatically at startup; no code change is needed.
