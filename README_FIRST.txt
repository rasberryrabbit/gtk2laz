gtk2laz v2.2 - GTK to Lazarus converter
========================================

ply 패키지가 소스로 포함. pip install ply 불필요!

Quick start:
  Linux:    ./linux-x86_64/gtk2laz gui
  Windows:  windows\gtk2laz_win.bat gui
  Source:   python3 gtk2laz.py gui
  Diagnose: gtk2laz diagnose

Expected conversion stats (opgui-0.12.4):
  Total TODOs: ~55 (full convert with Glade)
  Total TODOs: ~84 (c2pas only, no Glade)

⚠️ 중요: 한 번에 한 플랫폼만 변환 가능
-------------------------------------
C 전처리기는 `--platform` 플래그(windows / linux / macos / auto)로
한 플랫폼의 #ifdef 분기만 선택합니다. 다중 플랫폼 소스(예: Windows의
SetupDi* + Linux의 /dev/hidraw*)를 변환할 때는 플랫폼별로 한 번씩
gtk2laz를 실행한 후, 결과 .pas 파일들을 FreePascal의
{$IFDEF MSWINDOWS} / {$IFDEF UNIX} 블록으로 직접 병합해야 합니다.

  예:
    gtk2laz convert src/ out_windows/ --platform=windows
    gtk2laz convert src/ out_linux/   --platform=linux
    # 그 후 out_windows/*.pas + out_linux/*.pas를 수동 병합

