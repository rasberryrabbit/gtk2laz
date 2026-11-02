#!/usr/bin/env python3
"""
diagnose.py - gtk2laz AST 진단 도구

이 스크립트는 gtk2laz의 AST 변환기가 정상적으로 작동하는지
진단합니다. 빌드된 실행 파일이나 소스에서 직접 실행할 수 있습니다.

사용법:
  python diagnose.py              # 소스에서 실행
  gtk2laz.exe 내부에서 자동 실행    # 빌드된 exe에서 실행 시
"""
import sys
import os
import traceback

def main():
    print("=" * 60)
    print("gtk2laz AST 진단 도구")
    print("=" * 60)
    print()

    # 1. Python 버전
    print(f"[1] Python: {sys.version}")
    print(f"    Platform: {sys.platform}")
    print(f"    Executable: {sys.executable}")
    print()

    # 2. 모듈 임포트 테스트
    print("[2] 모듈 임포트 테스트")
    modules = {}
    for mod_name in ["pycparser", "ply", "ply.lex", "ply.yacc",
                      "pcpp", "pcpp.preprocessor"]:
        try:
            mod = __import__(mod_name, fromlist=[""])
            mod_file = getattr(mod, "__file__", "(내장)")
            modules[mod_name] = True
            print(f"    OK: {mod_name} -> {mod_file}")
        except ImportError as e:
            modules[mod_name] = False
            print(f"    FAIL: {mod_name} -> {e}")
        except Exception as e:
            modules[mod_name] = False
            print(f"    ERROR: {mod_name} -> {type(e).__name__}: {e}")
    print()

    # 3. pycparser AST 파싱 테스트
    print("[3] pycparser AST 파싱 테스트")
    if not modules.get("pycparser"):
        print("    SKIP: pycparser가 설치되지 않음")
    else:
        try:
            from pycparser import CParser, c_ast
            # 간단한 C 코드 파싱
            test_c = """
int add(int a, int b) {
    int sum = a + b;
    for (int i = 0; i < 10; i++) {
        sum += i;
    }
    return sum;
}
"""
            parser = CParser()
            ast = parser.parse(test_c, filename="test.c")
            funcs = [e for e in ast.ext if hasattr(e, "decl") and hasattr(e.decl, "name")]
            print(f"    OK: {len(funcs)}개 함수 파싱됨")
            for f in funcs:
                print(f"      - {f.decl.name}")
            # for 루프 확인
            if funcs:
                body = funcs[0].body
                has_for = any(isinstance(s, c_ast.For) for s in body.block_items)
                print(f"    for 루프 감지: {'OK' if has_for else 'FAIL'}")
        except Exception as e:
            print(f"    FAIL: {type(e).__name__}: {e}")
            traceback.print_exc()
    print()

    # 4. pcpp 전처리 테스트
    print("[4] pcpp 전처리 테스트")
    if not modules.get("pcpp"):
        print("    SKIP: pcpp가 설치되지 않음")
    else:
        try:
            from pcpp import Preprocessor, OutputDirective, Action
            pp = Preprocessor()
            pp.define("TEST 1")
            test_src = '#define X 5\nint x = X;'
            pp.parse(test_src, source="test.c")
            import io
            out = io.StringIO()
            pp.write(out)
            text = out.getvalue()
            if "x = 5" in text or "x = (5)" in text:
                print("    OK: 매크로 확장 정상")
            else:
                print(f"    WARNING: 매크로 확장 결과 이상")
                print(f"    출력: {text[:200]}")
        except Exception as e:
            print(f"    FAIL: {type(e).__name__}: {e}")
            traceback.print_exc()
    print()

    # 5. fake includes 확인
    print("[5] fake includes 확인")
    # fake_libc_include 경로 찾기 - platform별 기본 경로
    import tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    meipass = getattr(sys, "_MEIPASS", None)
    if sys.platform == "win32":
        default_fake = os.path.join(tempfile.gettempdir(), "fake_libc_include")
    else:
        default_fake = "/tmp/fake_libc_include"
    fake_dir = None
    candidates = [
        default_fake,
        os.path.join(here, "fake_libc_include"),
        os.path.join(meipass, "fake_libc_include") if meipass else "",
    ]
    for d in candidates:
        if d and os.path.isdir(d):
            gtk_h = os.path.join(d, "gtk", "gtk.h")
            if os.path.isfile(gtk_h):
                fake_dir = d
                break
    if fake_dir:
        print(f"    OK: {fake_dir}")
        # 주요 헤더 확인
        for h in ["stdio.h", "gtk/gtk.h", "string.h"]:
            full = os.path.join(fake_dir, h)
            exists = os.path.isfile(full)
            print(f"      {h}: {'OK' if exists else 'MISSING'}")
    else:
        print("    NOT FOUND: fake_libc_include 디렉토리가 없음")
        print("    gen_fake_includes.py를 실행하여 생성 시도...")
        # 생성 시도
        try:
            sys.path.insert(0, here)
            if meipass:
                sys.path.insert(0, meipass)
            import gen_fake_includes
            gen_fake_includes.generate()
            # 다시 확인
            if os.path.isdir(default_fake):
                gtk_h = os.path.join(default_fake, "gtk", "gtk.h")
                if os.path.isfile(gtk_h):
                    fake_dir = default_fake
                    print(f"    OK: {fake_dir} 생성됨")
                else:
                    print(f"    생성됐지만 gtk/gtk.h가 없음")
            else:
                print(f"    생성 실패: {default_fake}")
        except Exception as e:
            print(f"    gen_fake_includes 실패: {e}")
            import traceback
            traceback.print_exc()
    print()

    # 6. AST 변환기 통합 테스트
    print("[6] AST 변환기 통합 테스트")
    if fake_dir and modules.get("pycparser"):
        try:
            sys.path.insert(0, here)
            if meipass:
                sys.path.insert(0, meipass)
            from ast_translator import AstToPascal

            # 임시 C 파일 생성
            import tempfile
            tmpdir = tempfile.mkdtemp()
            test_c_path = os.path.join(tmpdir, "test.c")
            with open(test_c_path, "w") as f:
                f.write("""#include <stdio.h>
int add(int a, int b) {
    int sum = a + b;
    for (int i = 0; i < 10; i++) {
        sum += i;
    }
    return sum;
}
""")

            a2p = AstToPascal(test_c_path, "test",
                              fake_include_dir=fake_dir)
            ok = a2p.parse()
            if ok:
                print(f"    OK: AST 파싱 성공")
                print(f"    함수 수: {len(a2p.functions)}")
                if a2p.functions:
                    fn = a2p.functions[0]
                    print(f"    첫 함수: {fn.name}")
                    print(f"    반환형: {fn.return_type}")
                    print(f"    매개변수: {fn.params}")
                    print(f"    본문 있음: {fn.body_node is not None}")

                    # 본문 번역 테스트
                    from ast_translator import AstBodyTranslator
                    t = AstBodyTranslator(fn, source_file=test_c_path, indent=2)
                    lines = t.translate()
                    print(f"    본문 번역: {len(lines)}줄, TODO: {t.todo_count}")
                    if lines:
                        print(f"    첫 줄: {lines[0].strip()}")
            else:
                print(f"    FAIL: AST 파싱 실패")
                print(f"    에러: {a2p.parse_error}")
        except Exception as e:
            print(f"    FAIL: {type(e).__name__}: {e}")
            traceback.print_exc()
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        print("    SKIP: fake_dir 또는 pycparser 없음")
    print()

    # 7. 종합 진단
    print("=" * 60)
    print("종합 진단 결과")
    print("=" * 60)
    all_ok = (modules.get("pycparser") and
              modules.get("pcpp") and
              fake_dir is not None)
    if all_ok:
        print("✓ 모든 구성 요소가 정상입니다.")
        print("  AST 변환기가 작동해야 합니다.")
        print()
        print("  만약 변환 시 여전히 1136개 TODO가 나온다면:")
        print("  1. 변환하려는 소스 디렉토리 경로를 확인하세요")
        print("  2. C 소스 파일에 복잡한 매크로가 있는지 확인하세요")
        print("  3. 다음 명령으로 직접 테스트해보세요:")
        print('     python -c "from ast_translator import AstToPascal; '
              'a=AstToPascal(\'경로.c\', \'test\'); '
              'print(a.parse(), a.parse_error)"')
    else:
        print("✗ 일부 구성 요소가 누락되었습니다:")
        if not modules.get("pycparser"):
            print("  - pycparser 설치 필요: pip install pycparser")
        if not modules.get("pcpp"):
            print("  - pcpp 설치 필요: pip install pcpp")
        if not modules.get("ply.lex") or not modules.get("ply.yacc"):
            print("  - ply 설치 필요: pip install ply")
        if not fake_dir:
            print("  - fake_libc_include 생성 필요")
        print()
        print("  해결 방법:")
        print("  pip install pycparser ply pcpp")
    print()

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
