"""Inspect actual Windows installer windows for both shipped UI languages."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    if sys.platform != "win32":
        raise SystemExit("Installer language UI checks require Windows")
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    def text(handle):
        buffer = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(handle) + 1)
        user32.GetWindowTextW(handle, buffer, len(buffer))
        return buffer.value
    def class_name(handle):
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(handle, buffer, len(buffer))
        return buffer.value
    setup = Path("installer/CourseMaterialAssistant-Update.exe").resolve()
    output = Path("installer-language-previews")
    output.mkdir(exist_ok=True)
    results = []
    for language, product, welcome, next_word in (
        ("chinesesimplified", "课题资料助手", "欢迎", "下一步"),
        ("japanese", "課題資料アシスタント", "セットアップウィザードの開始", "次へ"),
    ):
        with tempfile.TemporaryDirectory(prefix="course-language-") as temp:
            process = subprocess.Popen([str(setup), "/SP-", "/LANG=" + language, "/DIR=" + str(Path(temp) / "app")])
            try:
                deadline = time.monotonic() + 30
                handle = None
                while time.monotonic() < deadline:
                    matches = []
                    @callback_type
                    def find(window, _):
                        if user32.IsWindowVisible(window) and class_name(window) == "TWizardForm" and product in text(window):
                            matches.append(window)
                        return True
                    user32.EnumWindows(find, 0)
                    if matches:
                        handle = matches[0]
                        break
                    if process.poll() is not None:
                        raise RuntimeError("Installer exited before showing " + language)
                    time.sleep(.2)
                assert handle, "No localized installer window: " + language
                labels = []
                @callback_type
                def collect(window, _):
                    if user32.IsWindowVisible(window):
                        value = text(window)
                        if value:
                            labels.append(value)
                    return True
                user32.EnumChildWindows(handle, collect, 0)
                visible = "\n".join(labels)
                assert welcome in visible, (language, labels)
                assert next_word in visible, (language, labels)
                assert "Welcome to" not in visible and "Next >" not in visible, labels
                user32.SetForegroundWindow(handle)
                time.sleep(.4)
                try:
                    from PIL import ImageGrab
                    rect = wintypes.RECT()
                    user32.GetWindowRect(handle, ctypes.byref(rect))
                    ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom)).save(output / (language + ".png"))
                except OSError:
                    pass  # Native text verification still applies without screen-capture access.
                results.append({"language": language, "window_title": text(handle), "visible_labels": labels})
            finally:
                if process.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                    process.wait(timeout=15)
    (output / "verified-labels.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"installer_languages_verified": [r["language"] for r in results]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
