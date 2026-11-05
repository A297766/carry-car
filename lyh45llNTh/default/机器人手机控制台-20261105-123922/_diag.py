# -*- coding: utf-8 -*-
"""诊断：捕获服务线程内的真实异常。"""
import os
import socket
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import start_server as srv

PORT = 8733
errors = []


def run_server(httpd):
    try:
        httpd.serve_forever()
    except BaseException:
        errors.append(traceback.format_exc())


print('--- 1) 直接绑定测试 ---')
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('127.0.0.1', PORT))
    s.listen(1)
    print('    raw socket bind+listen OK')
    s.close()
except Exception as e:
    print('    raw socket FAIL: %r' % e)

print('--- 2) ThreadedServer 构造 ---')
handler = lambda *a, **kw: srv.Handler(*a, host_ip='127.0.0.1', host_port=PORT, **kw)
try:
    httpd = srv.ThreadedServer(('127.0.0.1', PORT), handler)
    print('    construct OK, server_address=%s' % (httpd.server_address,))
except Exception:
    print('    construct FAIL'); traceback.print_exc(); sys.exit(1)

print('--- 3) serve_forever 线程 ---')
t = threading.Thread(target=run_server, args=(httpd,), daemon=True)
t.start()
time.sleep(1.0)
print('    thread alive = %s' % t.is_alive())
if errors:
    print('    线程异常：')
    print(errors[0])

print('--- 4) 原始 socket 连接测试 ---')
try:
    c = socket.create_connection(('127.0.0.1', PORT), timeout=3)
    print('    TCP connect OK')
    c.sendall(b'GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n')
    time.sleep(0.6)
    data = c.recv(2000)
    print('    got %d bytes' % len(data))
    print('    head: %r' % data[:220])
    c.close()
except Exception:
    print('    TCP connect FAIL'); traceback.print_exc()

print('--- 5) 线程异常复查 ---')
if errors:
    print(errors[0])
else:
    print('    无线程异常')

httpd.shutdown()
httpd.server_close()
print('done')
