# Attribution

Android ROM Toolkit for Windows (A.R.T) is a Windows-focused refactor and extension of **A.R.T** by **ELF-RC**:

- Original project: https://github.com/ELF-RC/A.R.T
- Original author and maintainer: ELF-RC
- Original licensing: GNU Affero General Public License v3.0 (see [LICENSE](LICENSE))

This project keeps the original license obligations and adds Windows-native tooling, a graphical workbench, and MCP integration. Changes made in this repository are identified in the project history and documentation.

Windows EXT4 validation uses the Cygwin port of e2fsprogs 1.44.5 (GPL-2.0) with its Windows runtime DLLs, isolated in `art-res/bin-win-amd64/e2fsprogs/`. It runs directly on Windows and does not use WSL. Package sources and license details are available from [Cygwin e2fsprogs](https://cygwin.com/packages/summary/e2fsprogs.html), [e2fsprogs upstream](https://git.kernel.org/pub/scm/fs/ext2/e2fsprogs.git/), and [Cygwin](https://cygwin.com/licensing.html). The downloader verifies the official mirror's SHA512 checksums before installing these files.

Windows OTA operations bundle [avbroot](https://github.com/chenxiaolong/avbroot) 3.34.1 by chenxiaolong, licensed under GPL-3.0. Its license and README are included in `art-res/bin-win-amd64/licenses/avbroot/`. Missing tools are fetched from its official releases and verified against the official SHA256 digest.

The built-in AVB tool uses the unchanged [AOSP avbtool](https://android.googlesource.com/platform/external/avb/+/761178607206f4cb2af79ed9eec52d8cbd814adb/avbtool.py) 1.3.0 (MIT), copyright 2016 The Android Open Source Project. Its license is preserved in `Scripts/vendor/avbtool.py` and [AVBTOOL_LICENSE.txt](AVBTOOL_LICENSE.txt). The Windows adapter uses [cryptography](https://cryptography.io/) (Apache-2.0 or BSD-3-Clause) for RSA instead of external OpenSSL commands.

The two embedded RSA test private keys were supplied and explicitly authorized for redistribution by this fork's user. They are publicly distributed test keys, selectable as `builtin:rsa2048` and `builtin:rsa4096`; see [key documentation](assets/keys/README.md).
