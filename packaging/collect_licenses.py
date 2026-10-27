"""Collect actual installed runtime/package licenses for the distributed Windows build."""
from importlib import metadata
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
OUTPUT=ROOT/'dist'/'licenses'
OUTPUT.mkdir(parents=True,exist_ok=True)
shutil.copy2(ROOT/'LICENSE',OUTPUT/'PROJECT-MIT.txt')
shutil.copy2(ROOT/'THIRD_PARTY_NOTICES.md',OUTPUT/'THIRD_PARTY_NOTICES.md')
shutil.copy2(ROOT/'packaging'/'languages'/'INNO-LICENSE.txt',OUTPUT/'INNO-LICENSE.txt')
for package in ('pypdf','pyinstaller'):
    distribution=metadata.distribution(package)
    found=[]
    for entry in distribution.files or []:
        if '/licenses/' in str(entry).lower() or Path(entry).name.lower() in {'license','license.txt','license.md','copying.txt'}:
            source=Path(distribution.locate_file(entry))
            if source.is_file():
                destination=OUTPUT/package/Path(entry).name
                destination.parent.mkdir(exist_ok=True)
                shutil.copy2(source,destination);found.append(source.name)
    if not found:raise SystemExit('No installed license found for '+package)
python_license=Path(sys.base_prefix)/'LICENSE.txt'
if not python_license.is_file():raise SystemExit('Python runtime license missing')
shutil.copy2(python_license,OUTPUT/'PYTHON-LICENSE.txt')
import tkinter
interpreter=tkinter.Tcl()
tcl=Path(interpreter.eval('info library'))
for package,directory in [('tcl',tcl),('tk',tcl.parent/'tk8.6')]:
    sources=[directory/name for name in ('license.terms','license.txt','LICENSE')]
    source=next((p for p in sources if p.is_file()),None)
    if source is None:
        if not str(interpreter.eval('info patchlevel')).startswith('8.6.'):
            raise SystemExit('Unsupported Tcl/Tk runtime: supply its exact upstream licenses')
        source=ROOT/'packaging'/'licenses'/(package+'8.6-license.terms.txt')
    if not source.is_file():raise SystemExit('Tcl/Tk runtime license missing: '+package)
    shutil.copy2(source,OUTPUT/(package+'-license.terms.txt'))
print('Collected project, Python, Tcl/Tk, pypdf, PyInstaller and Inno Setup licenses.')
