Subtitle fonts bundled with the application.

Every file here is licensed under the SIL Open Font License 1.1, which permits
redistribution inside a commercial application. See LICENSES-third-party.md in
the project root for the full text and attribution.

  Be Vietnam Pro  - Latin + Vietnamese, the default subtitle face
  Charm           - Latin + Thai, a decorative alternative

Upstream MoneyPrinterTurbo also shipped Microsoft YaHei, STHeiti and
UTM Kabel KT. Those are proprietary (Microsoft, Changzhou SinoType and Thien
Minh respectively) and cannot be redistributed in a Store package, so they were
removed; they are kept out of the build in packaging/removed-fonts/ for
reference only.

Adding a CJK face means picking an openly licensed one - Noto Sans SC/TC or
Source Han Sans, both OFL - and dropping the .otf/.ttf here. It is discovered
automatically at startup; no code change is needed.
