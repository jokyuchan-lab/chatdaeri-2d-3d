#!/usr/bin/env python3
"""만든 사이트를 claude.ai 아티팩트(비공개 웹페이지)로 올릴 수 있게 복사본을 만든다.

아티팩트는 .glb(3D 모델) 파일을 내주지 않는다 → 같은 내용을 glTF JSON(버퍼를 안에 넣음)으로 바꿔 <이름>.glb.json 으로 두고,
furniture.js 가 그 파일을 읽게 고친다(GLTFLoader 는 확장자가 아니라 내용으로 형식을 알아본다).
미니맵의 원본 평면도(포털 매물 도면·워터마크 등)는 장면 데이터로 다시 그린 평면도로 바꾼다(clean_plan.py).
실행 파일(열기.bat·열기.command)과 원본 그림은 빼고, Artifact publish 에 그대로 넘길 files 목록(files.json)을 쓴다.

쓰기: python3 publish_prep.py <사이트 폴더> [<출력 폴더>]   (출력 기본: <사이트>-pub)
"""
import base64, html, json, re, shutil, struct, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVED = {'.html', '.css', '.js', '.mjs', '.json', '.txt', '.png', '.jpg', '.jpeg', '.webp', '.svg', '.gif', '.woff', '.woff2', '.ttf', '.otf'}


def glb_to_json(path: Path) -> Path:
    b = path.read_bytes()
    magic, ver, length = struct.unpack_from('<4sII', b, 0)
    if magic != b'glTF' or ver != 2: raise SystemExit(f'{path.name}: glTF 2 바이너리가 아닙니다')
    off, js, binc = 12, None, None
    while off < length:
        clen, ctype = struct.unpack_from('<I4s', b, off); data = b[off + 8:off + 8 + clen]; off += 8 + clen
        if ctype == b'JSON': js = json.loads(data)
        elif ctype == b'BIN\x00': binc = data
    if binc is not None: js['buffers'][0]['uri'] = 'data:application/octet-stream;base64,' + base64.b64encode(binc).decode()
    out = path.with_name(path.name + '.json')
    out.write_text(json.dumps(js, separators=(',', ':')), encoding='utf-8')
    path.unlink()
    return out


def main():
    if len(sys.argv) < 2: sys.exit(__doc__)
    site = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else site.with_name(site.name + '-pub')
    if not (site / 'index.html').exists(): sys.exit(f'{site} 에 index.html 이 없습니다')
    if out.exists(): shutil.rmtree(out)
    shutil.copytree(site, out)
    for junk in ['열기.bat', '열기.command']: (out / junk).unlink(missing_ok=True)
    subprocess.run([sys.executable, str(HERE / 'clean_plan.py'), str(out)], check=True)
    n = 0
    for g in sorted((out / 'assets' / 'furniture').glob('*.glb')): glb_to_json(g); n += 1
    fj = out / 'src' / 'furniture.js'
    src = fj.read_text(encoding='utf-8')
    src2 = src.replace('`assets/furniture/${n}.glb`', '`assets/furniture/${n}.glb.json`')
    if n and src2 == src: sys.exit('furniture.js 에서 모델 경로를 찾지 못했습니다(템플릿이 바뀌었으면 이 스크립트도 고친다)')
    fj.write_text(src2, encoding='utf-8')
    # 페이지 제목 = 집 이름(아티팩트 목록에 그 이름으로 보인다)
    try:
        nm = json.loads((out / 'scene.json').read_text(encoding='utf-8')).get('name')
        if nm:
            ih = out / 'index.html'; h = ih.read_text(encoding='utf-8')
            ih.write_text(re.sub(r'<title>.*?</title>', lambda _m: f'<title>{html.escape(nm)} 3D 투어</title>', h, count=1), encoding='utf-8')
    except Exception: pass
    files, skipped = {}, []
    for p in sorted(out.rglob('*')):
        if not p.is_file() or p.name == 'index.html': continue
        rel = p.relative_to(out).as_posix()
        if p.suffix.lower() in SERVED or p.name.endswith('.glb.json'): files[rel] = rel
        else: skipped.append(rel)
    (out.parent / f'{out.name}.files.json').write_text(json.dumps(files, ensure_ascii=False, indent=1), encoding='utf-8')
    title = re.search(r'<title>(.*?)</title>', (out / 'index.html').read_text(encoding='utf-8'))
    print(f'올릴 폴더: {out}\n파일 {len(files)}개(+ index.html) · 3D 모델 {n}개를 glTF JSON 으로 바꿈' + (f' · 뺀 파일 {skipped}' if skipped else ''))
    print(f'files 목록: {out.parent / (out.name + ".files.json")}  (Artifact publish 의 root = 올릴 폴더, files = 이 목록)')
    if title: print(f'페이지 제목: {title.group(1)}')


if __name__ == '__main__':
    main()
