# Inno Setup installer translations

The Simplified Chinese and Japanese message files are from the upstream Inno Setup project (6.5.0+), retrieved 2026-10-27. They are bundled so CI does not depend on which translations happen to ship with the runner's compiler.

- [ChineseSimplified.isl](https://github.com/jrsoftware/issrc/blob/main/Files/Languages/ChineseSimplified.isl), upstream Git blob `d0d44258f51aa3127fc0d1167a7c8be328a9bdcf`.
- [Japanese.isl](https://github.com/jrsoftware/issrc/blob/main/Files/Languages/Japanese.isl), upstream Git blob `532b14ed4ecba07589d5e2b6f35ecd2cdb662bed`.
- [Upstream license](https://github.com/jrsoftware/issrc/blob/main/license.txt) is retained in `INNO-LICENSE.txt`.

Author and maintainer notices remain intact. Local changes only normalize line endings and add the UTF-8 BOM for the installer compiler; application-specific messages are in `windows.iss`.
