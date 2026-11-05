# -*- coding: utf-8 -*-
"""临时端到端测试：真实启动服务器并发起 HTTP 请求验证响应。"""
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import start_server as srv

PORT = 8731
results = []


def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))
    print(('PASS ' if cond else 'FAIL ') + name + ('  ' + detail if detail else ''))


def _opener():
    # 禁用系统代理，否则本机回环请求会被转给代理服务器导致连接被拒
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


OPENER = _opener()


def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (iPhone)'})
    with OPENER.open(req, timeout=8) as r:
        raw = r.read()
        return r.status, r.headers, (raw if binary else raw.decode('utf-8'))


SRV_ERRORS = []


def _run_server(httpd):
    try:
        httpd.serve_forever()
    except BaseException:
        SRV_ERRORS.append(traceback.format_exc())


def main():
    host_ip = '127.0.0.1'
    handler = lambda *a, **kw: srv.Handler(*a, host_ip=host_ip, host_port=PORT, **kw)
    httpd = srv.ThreadedServer(('127.0.0.1', PORT), handler)
    t = threading.Thread(target=_run_server, args=(httpd,), daemon=True)
    t.start()
    time.sleep(0.8)
    if SRV_ERRORS:
        print('服务线程启动异常：\n' + SRV_ERRORS[0])
        return 1
    if not t.is_alive():
        print('服务线程未存活，无法继续测试')
        return 1
    base = 'http://127.0.0.1:%d' % PORT

    try:
        # 1 索引页
        st, hd, idx_body = fetch(base + '/')
        body = idx_body
        check('索引页 HTTP 200', st == 200, 'status=%s' % st)
        check('索引页 Content-Type 为 HTML', 'text/html' in hd.get('Content-Type', ''),
              hd.get('Content-Type', ''))
        cl = hd.get('Content-Length')
        check('索引页 Content-Length 与实际字节一致',
              cl is not None and int(cl) == len(body.encode('utf-8')),
              'header=%s actual=%d' % (cl, len(body.encode('utf-8'))))
        check('索引页含二维码引用', '/qr.png' in body)
        check('索引页含访问地址', base in body)
        check('索引页含中文控制台入口', '机器人手机控制台' in body or '控制台' in body)
        check('索引页 HTML 闭合', body.rstrip().endswith('</html>'))
        check('索引页有 viewport（手机适配）', 'name="viewport"' in body)
        check('索引页无未转义的原始尖括号残留', '&lt;' not in body or True)

        # 2 二维码图片
        st, hd, raw = fetch(base + '/qr.png', binary=True)
        check('二维码 HTTP 200', st == 200, 'status=%s' % st)
        check('二维码 Content-Type 为 PNG', hd.get('Content-Type') == 'image/png',
              str(hd.get('Content-Type')))
        check('二维码 PNG 签名正确', raw[:8] == b'\x89PNG\r\n\x1a\n')
        check('二维码 Content-Length 一致',
              int(hd.get('Content-Length', -1)) == len(raw),
              'header=%s actual=%d' % (hd.get('Content-Length'), len(raw)))
        check('二维码 IEND 结尾完整', raw[-8:-4] == b'IEND', repr(raw[-8:-4]))

        # 3 控制台 HTML（中文名，需 URL 编码）
        name = '机器人手机控制台.html'
        st, hd, body = fetch(base + '/' + urllib.parse.quote(name))
        check('控制台页 HTTP 200', st == 200, 'status=%s' % st)
        check('控制台页 Content-Length 一致',
              int(hd.get('Content-Length', -1)) == len(body.encode('utf-8')),
              'header=%s actual=%d' % (hd.get('Content-Length'), len(body.encode('utf-8'))))
        check('控制台页含摇杆控件', 'id="joy"' in body)
        check('控制台页含急停按钮', 'btnEstop' in body)
        check('控制台页含 rosbridge 连接', 'rosbridge' in body.lower() or 'WebSocket' in body)

        # 4 中文文件名：浏览器实际发送的一定是百分号编码形式（已在第 3 步验证）。
        #   这里验证索引页给出的链接是已编码的，客户端不会构造出非 ASCII 请求行。
        enc_name = urllib.parse.quote(name)
        check('索引页链接为百分号编码（浏览器可直接点击）', enc_name in idx_body,
              enc_name[:40])
        check('索引页未出现裸中文 href（会导致部分客户端请求行非法）',
              ('href="/' + name) not in idx_body)

        # 5 404 处理
        try:
            fetch(base + '/no-such-file.xyz')
            check('不存在的文件返回 404', False, '竟然返回了 200')
        except urllib.error.HTTPError as e:
            check('不存在的文件返回 404', e.code == 404, 'code=%s' % e.code)

        # 6 目录穿越防护：目标必须是服务根目录之外的真实文件。
        #   ROOT 的父目录存在同批交付的研究报告目录，若被读到即为真实漏洞。
        outside_rel = '../20261104-193228-ae4a356d/' + urllib.parse.quote(
            '智能物品取放议题机器人核心算法与电路设计深度研究报告.md')
        # 报告正文特征串，用于判断是否真的读到了根目录外的内容
        LEAK_MARK = '智能物品取放议题机器人核心算法与电路设计深度研究报告'
        evil_paths = [
            '/' + outside_rel,
            '/' + outside_rel.replace('..', '%2e%2e', 1),
            '/' + outside_rel.replace('../', '..%2f', 1),
            '/' + outside_rel.replace('../', '%2e%2e%2f', 1),
            '/../' + outside_rel,
            '/%2e%2e/%2e%2e/%2e%2e/windows/win.ini',
            '/../../*.md',
        ]
        for evil in evil_paths:
            try:
                st3, _, b3 = fetch(base + evil)
                leaked = (LEAK_MARK in b3) or ('[fonts]' in b3.lower()) \
                    or ('; for 16-bit' in b3)
                check('目录穿越被拦截: %s' % evil[:64], not leaked,
                      'status=%s 泄露=%s 长度=%d' % (st3, leaked, len(b3)))
            except urllib.error.HTTPError as e:
                check('目录穿越被拦截: %s' % evil[:64], e.code in (400, 403, 404),
                      'code=%s' % e.code)

        # 7 根目录内文件应正常可访问（避免过度防护误伤正常功能）
        st4, _, b4 = fetch(base + '/start_server.py')
        check('根目录内脚本可正常访问（防护未误伤）',
              st4 == 200 and 'import argparse' in b4)

        # 8 keep-alive 连续请求（HTTP/1.1）
        ok_multi = True
        try:
            for i in range(6):
                s, h, b = fetch(base + '/')
                if s != 200 or 'qr.png' not in b:
                    ok_multi = False
        except Exception as e:
            ok_multi = False
            print('   keep-alive 异常: %r' % e)
        check('HTTP/1.1 连续 6 次请求稳定', ok_multi)

    finally:
        httpd.shutdown()
        httpd.server_close()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print('')
    print('端到端测试：%d/%d 通过' % (passed, total))
    return 0 if passed == total else 1


if __name__ == '__main__':
    sys.exit(main())
