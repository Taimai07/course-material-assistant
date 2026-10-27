[Setup]
AppId={{8532C685-4293-44B1-90DD-6CF9E1805D4A}
AppName={cm:ProductName}
AppVersion=0.7.0
DefaultDirName={localappdata}\CourseMaterialAssistant
DefaultGroupName=课题资料助手
PrivilegesRequired=lowest
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousTasks=yes
UsePreviousLanguage=yes
LanguageDetectionMethod=uilanguage
ShowLanguageDialog=yes
CloseApplications=force
CloseApplicationsFilter=CourseMaterialAssistant.exe
RestartApplications=no
SetupMutex=CourseMaterialAssistantUpdate
AppComments={cm:KeepData}
DisableWelcomePage=no
WizardSizePercent=110
OutputDir=..\installer
OutputBaseFilename=CourseMaterialAssistant-Update
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\CourseMaterialAssistant.exe

[Languages]
Name: "chinesesimplified"; MessagesFile: "languages\ChineseSimplified.isl"
Name: "japanese"; MessagesFile: "languages\Japanese.isl"

[CustomMessages]
chinesesimplified.ProductName=课题资料助手
japanese.ProductName=課題資料アシスタント
chinesesimplified.KeepData=覆盖更新会保留课程、下载资料、配对与设置。
japanese.KeepData=更新時も授業・ダウンロード済みの資料・ペアリング・設定は保持されます。
chinesesimplified.OpenAssistant=打开课题资料助手
japanese.OpenAssistant=課題資料アシスタントを起動する

[Messages]
chinesesimplified.WelcomeLabel2=本程序支持首次安装与覆盖更新。%n%n已有版本会沿用原安装位置；课程、已下载附件、配对和设置会保留。%n%n安装程序可能关闭正在后台运行的旧助手，以替换程序文件。
japanese.WelcomeLabel2=初めてのインストールにも、既存バージョンの更新にも対応しています。%n%n既存のインストール先を引き継ぎ、授業・ダウンロード済みの資料・ペアリング・設定を保持します。%n%nプログラムファイルを置き換えるため、起動中の旧バージョンを終了する場合があります。

[Files]
Source: "..\dist\CourseMaterialAssistant.exe"; DestDir: "{app}"; Flags: ignoreversion

Source: "..\dist\licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\课题资料助手"; Filename: "{app}\CourseMaterialAssistant.exe"
Name: "{autodesktop}\课题资料助手"; Filename: "{app}\CourseMaterialAssistant.exe"

[Run]
Filename: "{app}\CourseMaterialAssistant.exe"; Description: "{cm:OpenAssistant}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; User course data is intentionally retained under the user's data directory.

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "CourseMaterialAssistant"; Flags: uninsdeletevalue
