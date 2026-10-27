# Third-party notices

The MIT license in LICENSE covers this project's original code only. Third-party
software and translation files retain their own copyrights and licenses.

- Python runtime: Python Software Foundation license and bundled license notices.
- Tcl/Tk: upstream Tcl/Tk license terms. The application uses the user's system fonts;
  it does not redistribute Google, Microsoft, or third-party font files.
- pypdf: BSD-3-Clause. https://github.com/py-pdf/pypdf
- PyInstaller bootloader: GPL-2.0-or-later with the PyInstaller bootloader exception,
  which permits distribution of applications built with it under another license.
  https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt
- Inno Setup and the bundled Chinese/Japanese installer translations: upstream
  license in packaging/languages/INNO-LICENSE.txt; translator attributions are
  retained in the .isl files. https://github.com/jrsoftware/issrc

The Windows build collects the licenses from its actual runtime and installed
packages into dist/licenses; if Windows Python omits Tcl/Tk license files, the upstream 8.6 license texts retained in packaging/licenses are included; the installer includes that directory and this notice.
Development-only tools (test runners and build tooling) are not application APIs
or bundled UI assets. Their presence in CI does not change their upstream licenses.

Google, Google Classroom and Google Drive are trademarks of their respective
owners. This is an independent, unofficial project. No Google frontend code,
logos, course content, student information, login cookies or course attachments
are distributed with the source or installer. A user's ability to read/download
an item does not itself grant the right to republish it.
