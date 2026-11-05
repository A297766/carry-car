#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
局域网静态文件服务器 + 二维码生成器（零第三方依赖）

用途：
    在电脑上运行本脚本，手机扫描终端里的二维码即可访问机器人手机控制台。

依赖：
    仅需 Python 3.7+ 标准库。QR 编码器与 PNG 输出均为本文件内实现，
    不需要 pip install qrcode / segno / pillow，离线环境可用。

常用命令：
    python start_server.py                 # 自动选端口并启动，打印二维码
    python start_server.py --port 8000     # 指定端口
    python start_server.py --qr-only       # 只生成二维码，不启动服务（离线自检）
    python start_server.py --selftest      # 运行内部自检后退出
"""

import argparse
import html
import http.server
import os
import socket
import socketserver
import struct
import sys
import threading
import urllib.parse
import webbrowser
import zlib

# SECTION: paths
ROOT = os.path.dirname(os.path.abspath(__file__))
QR_PNG = os.path.join(ROOT, 'qr.png')
DEFAULT_PORT = 8000
PORT_RANGE = 12          # 端口被占用时最多向后尝试多少个


# =====================================================================
# SECTION: qr-galois-field  GF(256) 运算与 Reed-Solomon 纠错码
# =====================================================================
GF_EXP = [0] * 512
GF_LOG = [0] * 256


def _gf_init():
    """构造 GF(256) 指数表与对数表，本原多项式 0x11D（QR 标准）。"""
    x = 1
    for i in range(255):
        GF_EXP[i] = x
        GF_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        GF_EXP[i] = GF_EXP[i - 255]


_gf_init()


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return GF_EXP[GF_LOG[a] + GF_LOG[b]]


def _rs_generator(degree):
    """生成 degree 次 Reed-Solomon 生成多项式系数。"""
    g = [1]
    for i in range(degree):
        ng = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            ng[j] ^= c
            ng[j + 1] ^= _gf_mul(c, GF_EXP[i])
        g = ng
    return g


def _rs_encode(data, ec_count):
    """对数据码字计算 ec_count 个纠错码字。"""
    g = _rs_generator(ec_count)
    res = list(data) + [0] * ec_count
    for i in range(len(data)):
        coef = res[i]
        if coef:
            for j in range(len(g)):
                res[i + j] ^= _gf_mul(g[j], coef)
    return res[len(data):]


# =====================================================================
# SECTION: qr-encode  版本表与字节模式数据编码
# =====================================================================
# 版本 1-5、纠错级别 L、单块结构（无块交织），容量足够容纳局域网 URL
# 映射：version -> (总码字数, 纠错码字数)
QR_TABLE = {1: (26, 7), 2: (44, 10), 3: (70, 15), 4: (100, 20), 5: (134, 26)}
ALIGN_POS = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30]}
ECL_L_BITS = 0b01        # 纠错级别 L 的 2 bit 标识
FORMAT_GEN = 0x537       # BCH(15,5) 生成多项式
FORMAT_XOR = 0x5412      # 格式信息异或掩码


def _pick_version(nbytes):
    """按 UTF-8 字节数挑选最小可用版本，返回 (version, 数据码字数)。"""
    for v in range(1, 6):
        total, ec = QR_TABLE[v]
        cap = total - ec
        # 字节模式头部：4 bit 模式标识 + 8 bit 字符计数（版本 1-9）
        if 12 + nbytes * 8 <= cap * 8:
            return v, cap
    raise ValueError('内容过长，超出 QR 版本 1-5（ECC-L）容量；请缩短 URL')


def _encode_data(text, cap):
    """把文本编码为数据码字列表（含模式头、终止符与填充）。"""
    payload = text.encode('utf-8')
    bits = []

    def push(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    push(0b0100, 4)            # 字节模式
    push(len(payload), 8)      # 字符计数
    for b in payload:
        push(b, 8)

    total_bits = cap * 8
    push(0, min(4, total_bits - len(bits)))       # 终止符
    while len(bits) % 8:                          # 补齐到字节边界
        bits.append(0)

    pad_bytes = (0xEC, 0x11)
    k = 0
    while len(bits) < total_bits:
        push(pad_bytes[k % 2], 8)
        k += 1

    codewords = []
    for i in range(0, len(bits), 8):
        v = 0
        for j in range(8):
            v = (v << 1) | bits[i + j]
        codewords.append(v)
    return codewords


def _to_bits(codewords):
    bits = []
    for b in codewords:
        for i in range(7, -1, -1):
            bits.append((b >> i) & 1)
    return bits


# =====================================================================
# SECTION: qr-matrix  功能图案与数据模块布局
# =====================================================================
def _build_base(version):
    """构造功能图案层，返回 (矩阵, 功能模块掩码)。"""
    size = version * 4 + 17
    m = [[0] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def setf(row, col, dark):
        if 0 <= row < size and 0 <= col < size:
            m[row][col] = 1 if dark else 0
            fn[row][col] = True

    # 三个定位图案（含外圈分隔符）
    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
        for r in range(-1, 8):
            for c in range(-1, 8):
                mx = max(abs(r - 3), abs(c - 3))
                setf(top + r, left + c, mx in (0, 1, 3))

    # 定位图形（时序图案）
    for i in range(8, size - 8):
        dark = (i % 2 == 0)
        setf(6, i, dark)
        setf(i, 6, dark)

    # 校正图形（版本 2-5 只有一个，位于右下区域）
    pos = ALIGN_POS[version]
    if pos:
        ctr = pos[-1]
        for r in range(-2, 3):
            for c in range(-2, 3):
                setf(ctr + r, ctr + c, max(abs(r), abs(c)) != 1)

    # 固定黑模块
    setf(size - 8, 8, True)

    # 预留格式信息区域（后续按掩码填充）
    for i in range(9):
        if not fn[8][i]:
            setf(8, i, 0)
        if not fn[i][8]:
            setf(i, 8, 0)
    for i in range(8):
        if not fn[8][size - 1 - i]:
            setf(8, size - 1 - i, 0)
        if not fn[size - 1 - i][8]:
            setf(size - 1 - i, 8, 0)

    return m, fn


def _place_data(m, fn, size, bits):
    """按标准的两列蛇形路径放置数据位。"""
    i = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5                      # 跳过垂直时序列
        for vert in range(size):
            for j in range(2):
                col = right - j
                upward = ((right + 1) & 2) == 0
                row = (size - 1 - vert) if upward else vert
                if not fn[row][col] and i < len(bits):
                    m[row][col] = bits[i]
                    i += 1
        right -= 2
    return i


def _read_data_bits(m, fn, size):
    """按与 _place_data 完全相同的路径读回数据位，用于解码校验。"""
    bits = []
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                col = right - j
                upward = ((right + 1) & 2) == 0
                row = (size - 1 - vert) if upward else vert
                if not fn[row][col]:
                    bits.append(m[row][col] & 1)
        right -= 2
    return bits


def _bits_to_bytes(bits):
    out = []
    for i in range(0, len(bits) - len(bits) % 8, 8):
        v = 0
        for j in range(8):
            v = (v << 1) | bits[i + j]
        out.append(v)
    return out


def decode_qr_matrix(m):
    """把 QR 矩阵解码回原始文本（仅支持本模块生成的版本 1-5、ECC-L）。

    流程：读格式信息定掩码 -> 去掩码 -> 蛇形读位 -> RS 校验 -> 解析字节模式。
    返回 (text, version, mask)。
    """
    size = len(m)
    version = (size - 17) // 4
    if version not in QR_TABLE:
        raise ValueError('不支持的版本 %d（尺寸 %d）' % (version, size))

    fcoords, scoords = _format_coords(size)
    fv = _read_format(m, size, fcoords)
    if not _bch_ok(fv):
        fv = _read_format(m, size, scoords)
        if not _bch_ok(fv):
            raise ValueError('格式信息 BCH 校验失败')
    ecl, mask = _format_payload(fv)
    if ecl != ECL_L_BITS:
        raise ValueError('纠错级别非 L')

    base, fn = _build_base(version)
    work = [row[:] for row in m]
    _apply_mask(work, fn, size, mask)          # 反向异或即去掩码

    bits = _read_data_bits(work, fn, size)
    codewords = _bits_to_bytes(bits)
    total, ec_n = QR_TABLE[version]
    if len(codewords) < total:
        raise ValueError('码字数不足：%d < %d' % (len(codewords), total))
    codewords = codewords[:total]
    data_cw, ecc_cw = codewords[:total - ec_n], codewords[total - ec_n:]

    # Reed-Solomon 一致性：对数据段重算纠错码字并比对
    if _rs_encode(data_cw, ec_n) != ecc_cw:
        raise ValueError('Reed-Solomon 校验失败，数据可能损坏')

    # 解析字节模式：4 bit 模式标识 + 8 bit 长度 + payload
    raw = []
    for b in data_cw:
        for i in range(7, -1, -1):
            raw.append((b >> i) & 1)

    def take(n, off):
        v = 0
        for k in range(n):
            v = (v << 1) | raw[off + k]
        return v, off + n

    mode, off = take(4, 0)
    if mode != 0b0100:
        raise ValueError('非字节模式（mode=%d）' % mode)
    length, off = take(8, off)
    if off + length * 8 > len(raw):
        raise ValueError('长度字段越界')
    payload = bytearray()
    for i in range(length):
        v, off = take(8, off)
        payload.append(v)
    return payload.decode('utf-8'), version, mask


def _format_coords(size):
    """返回两份格式信息的 (row, col) 坐标，列表下标即 bit 序号 0..14（0 为 LSB）。

    坐标顺序遵循 ISO/IEC 18004 的格式信息布局，与主流 QR 生成器一致。
    """
    first = [(i, 8) for i in range(6)]
    first += [(7, 8), (8, 8), (8, 7)]
    first += [(8, 14 - i) for i in range(9, 15)]

    second = [(8, size - 1 - i) for i in range(8)]
    second += [(size - 15 + i, 8) for i in range(8, 15)]
    return first, second


def _place_format(m, size, mask):
    """计算并写入两份格式信息（5 bit 数据 + 10 bit BCH 校验，再异或掩码）。"""
    data = (ECL_L_BITS << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * FORMAT_GEN)
    bits = ((data << 10) | rem) ^ FORMAT_XOR

    first, second = _format_coords(size)
    for i, (r, c) in enumerate(first):
        m[r][c] = (bits >> i) & 1
    for i, (r, c) in enumerate(second):
        m[r][c] = (bits >> i) & 1
    m[size - 8][8] = 1                 # 固定黑模块
    return bits


def _read_format(m, size, coords):
    """按坐标顺序把 15 bit 格式信息读回整数。"""
    v = 0
    for i, (r, c) in enumerate(coords):
        if m[r][c]:
            v |= (1 << i)
    return v


def _bch_ok(v):
    """校验 15 bit 格式信息：去掉异或掩码后对生成多项式取余应为 0。"""
    rem = v ^ FORMAT_XOR
    for i in range(14, 9, -1):
        if rem & (1 << i):
            rem ^= FORMAT_GEN << (i - 10)
    return rem == 0


def _format_payload(v):
    """从格式信息还原 (纠错级别位, 掩码号)。"""
    d = (v ^ FORMAT_XOR) >> 10
    return (d >> 3) & 0b11, d & 0b111


# =====================================================================
# SECTION: qr-mask-penalty  掩码图案与罚分评估
# =====================================================================
MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _apply_mask(m, fn, size, mask):
    f = MASKS[mask]
    for r in range(size):
        for c in range(size):
            if not fn[r][c] and f(r, c):
                m[r][c] ^= 1


def _penalty(m, size):
    """ISO/IEC 18004 的四条罚分规则，用于挑选最优掩码。"""
    score = 0
    cols = [[m[r][c] for r in range(size)] for c in range(size)]
    lines = list(m) + cols

    # 规则 1：行或列上连续同色 >= 5
    for line in lines:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2

    # 规则 2：2x2 同色块
    for r in range(size - 1):
        for c in range(size - 1):
            v = m[r][c]
            if v == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3

    # 规则 3：类定位图案 1:1:3:1:1 加四个浅色模块
    p1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    p2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    for line in lines:
        for i in range(size - 10):
            seg = line[i:i + 11]
            if seg == p1 or seg == p2:
                score += 40

    # 规则 4：深色比例偏离 50%
    dark = sum(sum(row) for row in m)
    pct = dark * 100 // (size * size)
    score += (abs(pct - 50) // 5) * 10
    return score


def make_qr(text):
    """生成 QR 矩阵，返回 (matrix, version, mask, penalty)。"""
    version, cap = _pick_version(len(text.encode('utf-8')))
    data_cw = _encode_data(text, cap)
    total, ec_n = QR_TABLE[version]
    if len(data_cw) != total - ec_n:
        raise ValueError('数据码字数与版本表不一致')
    ecc = _rs_encode(data_cw, ec_n)
    bits = _to_bits(data_cw + ecc)
    size = version * 4 + 17

    best = None
    for mask in range(8):
        m, fn = _build_base(version)
        placed = _place_data(m, fn, size, bits)
        if placed != len(bits):
            raise ValueError('数据位未全部放置：模块容量不足')
        _apply_mask(m, fn, size, mask)
        _place_format(m, size, mask)
        p = _penalty(m, size)
        if best is None or p < best[0]:
            best = (p, mask, [row[:] for row in m])
    return best[2], version, best[1], best[0]


# =====================================================================
# SECTION: qr-output  PNG 文件与终端字符画输出
# =====================================================================
def _png_chunk(typ, data):
    body = typ + data
    return (struct.pack('>I', len(data)) + body +
            struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF))


def write_png(matrix, path, scale=8, border=4):
    """用标准库手写 PNG（8 bit 灰度），无需 Pillow。"""
    size = len(matrix)
    dim = (size + border * 2) * scale
    rows = []
    for y in range(dim):
        my = y // scale - border
        row = bytearray([0])                       # filter type 0
        for x in range(dim):
            mx = x // scale - border
            dark = 0 <= my < size and 0 <= mx < size and matrix[my][mx]
            row.append(0 if dark else 255)
        rows.append(bytes(row))

    ihdr = struct.pack('>IIBBBBB', dim, dim, 8, 0, 0, 0, 0)
    png = (b'\x89PNG\r\n\x1a\n' + _png_chunk(b'IHDR', ihdr) +
           _png_chunk(b'IDAT', zlib.compress(b''.join(rows), 9)) +
           _png_chunk(b'IEND', b''))
    with open(path, 'wb') as f:
        f.write(png)
    return len(png)


def qr_ascii(matrix, border=2):
    """终端字符画：深色用 ## 表示，横向放大一倍以保持接近正方形的观感。"""
    size = len(matrix)
    out = []
    for y in range(-border, size + border):
        cells = []
        for x in range(-border, size + border):
            dark = 0 <= y < size and 0 <= x < size and matrix[y][x]
            cells.append('##' if dark else '  ')
        out.append(''.join(cells))
    return '\n'.join(out)


# =====================================================================
# SECTION: ip-detect  局域网地址探测
# =====================================================================
def detect_ip():
    """探测本机在局域网中的 IPv4 地址，失败时回落 127.0.0.1。"""
    # 方式一：UDP connect 技巧（不实际发包），取出口网卡地址
    for probe in ('223.5.5.5', '8.8.8.8'):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.4)
            s.connect((probe, 53))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith('127.'):
                return ip
        except Exception:
            try:
                s.close()
            except Exception:
                pass
    # 方式二：主机名解析
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith('127.') and not ip.startswith('169.254.'):
                return ip
    except Exception:
        pass
    return '127.0.0.1'


def pick_port(start):
    """从 start 开始找一个可绑定端口，找不到返回 None。"""
    for p in range(start, start + PORT_RANGE):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('0.0.0.0', p))
            s.close()
            return p
        except OSError:
            continue
    return None


# =====================================================================
# SECTION: index-page  浏览器索引页
# =====================================================================
def list_files():
    """列出目录下可访问文件，HTML 排在最前。"""
    items = []
    try:
        names = sorted(os.listdir(ROOT))
    except OSError:
        return items
    for n in names:
        full = os.path.join(ROOT, n)
        if not os.path.isfile(full):
            continue
        ext = os.path.splitext(n)[1].lower()
        rank = 0 if ext in ('.html', '.htm') else (1 if ext == '.md' else 2)
        try:
            sz = os.path.getsize(full)
        except OSError:
            sz = 0
        items.append((rank, n, sz, ext))
    items.sort(key=lambda t: (t[0], t[1]))
    return [(n, sz) for _, n, sz, _ in items]


def human_size(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return ('%.1f %s' % (n, unit)) if unit != 'B' else ('%d B' % n)
        n /= 1024.0
    return '%d B' % n


PAGE_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0b1220">
<title>机器人控制台 · 局域网访问</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
background:#0b1220;color:#e6edf7;line-height:1.6;padding-bottom:env(safe-area-inset-bottom)}
.wrap{max-width:720px;margin:0 auto;padding:28px 18px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#8fa3c4;font-size:13px;margin-bottom:22px}
.card{background:#151f33;border:1px solid #26344f;border-radius:12px;padding:18px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 12px;padding-left:9px;border-left:4px solid #3b82f6}
.qrbox{display:flex;flex-direction:column;align-items:center;gap:10px}
.qrbox img{width:min(260px,70vw);height:auto;border-radius:8px;background:#fff;padding:8px}
.addr{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:15px;color:#93c5fd;
background:#0e1728;border:1px solid #26344f;border-radius:9px;padding:11px 13px;
word-break:break-all;text-align:center;width:100%;display:block;text-decoration:none}
a.btn{display:block;text-align:center;padding:13px;border-radius:10px;background:#3b82f6;
color:#fff;text-decoration:none;font-weight:700;font-size:15px;margin-top:10px}
ol,ul{margin:0;padding-left:20px}
li{margin-bottom:7px;font-size:14px;color:#c9d6ea}
.file{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid #1c2740}
.file:last-child{border-bottom:0}
.file .ic{width:32px;height:32px;flex:0 0 32px;border-radius:8px;background:#1c2942;
display:flex;align-items:center;justify-content:center;font-size:15px}
.file a{flex:1 1 auto;min-width:0;color:#e6edf7;text-decoration:none;font-size:14px;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file .sz{flex:0 0 auto;font-size:11px;color:#8fa3c4}
.note{background:#2a2008;border:1px solid #7a5c14;color:#fde68a;border-radius:9px;
padding:11px 13px;font-size:13px;margin-bottom:16px}
code{font-family:ui-monospace,Menlo,Consolas,monospace;background:#0e1728;
padding:1px 5px;border-radius:4px;font-size:12px;color:#a5d6ff;word-break:break-all}
</style>
</head>
<body>
<div class="wrap">
"""


def build_index(host, port):
    """生成索引页 HTML 字符串。所有动态值均经转义。"""
    base = 'http://%s:%d' % (host, port)
    files = list_files()
    console = None
    for name, _ in files:
        if name.lower().endswith(('.html', '.htm')) and name != 'index.html':
            console = name
            break

    parts = [PAGE_HEAD]
    parts.append('<h1>机器人控制台 · 局域网访问</h1>')
    parts.append('<div class="sub">服务运行中 · 根目录 %s</div>' % html.escape(ROOT))

    parts.append('<div class="card"><h2>手机扫码访问</h2><div class="qrbox">')
    parts.append('<img src="/qr.png" alt="访问二维码">')
    parts.append('<a class="addr" href="%s/">%s/</a>' % (html.escape(base), html.escape(base)))
    parts.append('</div>')
    if console:
        parts.append('<a class="btn" href="/%s">打开机器人手机控制台</a>'
                     % urllib.parse.quote(console))
    parts.append('<p style="font-size:13px;color:#8fa3c4;margin:12px 0 0">'
                 '手机需与本机处于同一局域网（同一 Wi-Fi）。若二维码扫不出，'
                 '可在手机浏览器手动输入上方地址。</p>')
    parts.append('</div>')

    parts.append('<div class="note">本页由本地服务器动态生成，仅在局域网内可访问。'
                 '关闭运行本脚本的终端窗口即可停止服务。</div>')

    parts.append('<div class="card"><h2>可访问文件</h2>')
    if not files:
        parts.append('<div style="color:#8fa3c4;font-size:14px">目录为空</div>')
    for name, sz in files:
        ext = os.path.splitext(name)[1].lower()
        ICONS = {'.html': '🌐', '.htm': '🌐', '.md': '📄', '.png': '🖼',
                 '.py': '🐍', '.bat': '⚙', '.js': '📜', '.txt': '📝'}
        icon = ICONS.get(ext, '📎')
        href = '/%s' % urllib.parse.quote(name)
        parts.append('<div class="file"><div class="ic">%s</div>'
                     '<a href="%s">%s</a>'
                     '<span class="sz">%s</span></div>'
                     % (icon, html.escape(href), html.escape(name), human_size(sz)))
    parts.append('</div>')

    parts.append('<div class="card"><h2>机器人侧连接提示</h2><ol>')
    parts.append('<li>控制台页面的「连接设置」里填 rosbridge 地址，形如 '
                 '<code>ws://机器人IP:9090</code></li>')
    parts.append('<li>机器人侧需运行 <code>rosbridge_server</code> 与 '
                 '<code>web_video_server</code>（需要视频画面时）</li>')
    parts.append('<li>本页用 <code>http</code> 访问，浏览器不会拦截 '
                 '<code>ws://</code> 明文连接；若改用 https 托管本页则会被拦截</li>')
    parts.append('</ol></div>')

    parts.append('</div></body></html>')
    return ''.join(parts)


# =====================================================================
# SECTION: http-server  静态文件服务
# =====================================================================
class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = 'RobotConsole/1.0'
    protocol_version = 'HTTP/1.1'

    def __init__(self, *args, **kwargs):
        self.host_ip = kwargs.pop('host_ip', '127.0.0.1')
        self.host_port = kwargs.pop('host_port', DEFAULT_PORT)
        super().__init__(*args, directory=ROOT, **kwargs)

    def _send_bytes(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ('/', '/index.html'):
            page = build_index(self.host_ip, self.host_port)
            self._send_bytes(200, page.encode('utf-8'),
                             'text/html; charset=utf-8')
            return
        if path == '/qr.png':
            if not os.path.exists(QR_PNG):
                self.send_error(404, 'QR image not generated')
                return
            with open(QR_PNG, 'rb') as f:
                self._send_bytes(200, f.read(), 'image/png')
            return
        return super().do_GET()

    def do_HEAD(self):
        return self.do_GET()

    def log_message(self, fmt, *args):
        try:
            peer = self.client_address[0]
        except Exception:
            peer = '-'
        msg = fmt % args
        _safe_print('    [%s] %s' % (peer, msg))


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# =====================================================================
# SECTION: console-io  Windows 控制台兼容输出
# =====================================================================
_STDOUT_OK = True


def _safe_print(text):
    """兼容 GBK 控制台：无法编码时替换字符而非抛异常。"""
    global _STDOUT_OK
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, 'encoding', 'utf-8') or 'utf-8'
        print(text.encode(enc, 'replace').decode(enc, 'replace'))
    except Exception:
        if _STDOUT_OK:
            _STDOUT_OK = False


def _try_utf8_stdout():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


# =====================================================================
# SECTION: selftest  离线自检（不绑定端口、不暴露服务）
# =====================================================================
def selftest():
    """校验 QR 编码、PNG 输出与索引页生成的正确性。"""
    ok = True
    print('== 自检 1/4  GF(256) 与 Reed-Solomon ==')
    # 标准校验：RS(26,19) 对已知数据的纠错码字
    sample = list(range(19))
    ecc = _rs_encode(sample, 7)
    print('   数据 19 字节 -> 纠错 %d 字节：%s' % (len(ecc), ecc))
    if len(ecc) != 7:
        print('   FAIL 纠错码字长度错误'); ok = False
    # 生成多项式次数校验
    g = _rs_generator(10)
    if len(g) != 11 or g[0] != 1:
        print('   FAIL 生成多项式异常'); ok = False
    else:
        print('   PASS 生成多项式次数正确（degree 10 -> 11 项）')

    print('== 自检 2/4  二维码矩阵结构 ==')
    for text in ('http://127.0.0.1:8000/', 'http://192.168.31.177:8000/'):
        m, ver, mask, pen = make_qr(text)
        size = len(m)
        exp = ver * 4 + 17
        print('   %s' % text)
        print('     版本 %d  尺寸 %dx%d（期望 %d）  掩码 %d  罚分 %d'
              % (ver, size, size, exp, mask, pen))
        if size != exp:
            print('     FAIL 尺寸不符'); ok = False; continue
        # 三个定位图案中心必须为深色，且图案结构为 1:1:3:1:1
        corners = ((3, 3), (3, size - 4), (size - 4, 3))
        for cr, cc in corners:
            if not m[cr][cc]:
                print('     FAIL 定位图案中心缺失 @%s' % ((cr, cc),)); ok = False
        # 定位图案整行剖面必须是 1:1:3:1:1（共 7 模块）
        prof_ok = True
        for cr, cc in corners:
            row = [m[cr][cc - 3 + k] for k in range(7)]
            if row != [1, 1, 1, 0, 1, 1, 1] and row != [1, 0, 1, 1, 1, 0, 1]:
                prof_ok = False
        for cr, cc in corners:
            col = [m[cr - 3 + k][cc] for k in range(7)]
            if col != [1, 1, 1, 0, 1, 1, 1] and col != [1, 0, 1, 1, 1, 0, 1]:
                prof_ok = False
        if prof_ok:
            print('     PASS 定位图案 1:1:3:1:1 剖面正确')
        else:
            print('     FAIL 定位图案剖面异常'); ok = False
        # 时序图案必须交替
        bad_timing = 0
        for i in range(8, size - 8):
            if m[6][i] != (1 if i % 2 == 0 else 0):
                bad_timing += 1
            if m[i][6] != (1 if i % 2 == 0 else 0):
                bad_timing += 1
        if bad_timing:
            print('     FAIL 时序图案错误 %d 处' % bad_timing); ok = False
        else:
            print('     PASS 时序图案交替正确')
        # 固定黑模块
        if not m[size - 8][8]:
            print('     FAIL 固定黑模块缺失'); ok = False

        # 关键：按标准坐标把格式信息读回并做 BCH 校验，
        # 这一步等价于扫描器解码前的可行性判断，位序错误会在此暴露
        fcoords, scoords = _format_coords(size)
        f1 = _read_format(m, size, fcoords)
        f2 = _read_format(m, size, scoords)
        if f1 != f2:
            print('     FAIL 两份格式信息不一致（%d vs %d）' % (f1, f2)); ok = False
        elif not _bch_ok(f1):
            print('     FAIL 格式信息 BCH 校验不通过 -> 扫描器将无法识别'); ok = False
        else:
            ecl, rmask = _format_payload(f1)
            if ecl != ECL_L_BITS:
                print('     FAIL 纠错级别位不符（%d）' % ecl); ok = False
            elif rmask != mask:
                print('     FAIL 掩码号回读不符（%d != %d）' % (rmask, mask)); ok = False
            else:
                print('     PASS 格式信息 BCH 校验通过，纠错级别 L，掩码回读一致')

        # 深浅比例合理性
        dark = sum(sum(r) for r in m)
        ratio = dark * 100.0 / (size * size)
        print('     深色占比 %.1f%%（合理区间约 40%%-60%%）' % ratio)
        if not (30 <= ratio <= 70):
            print('     FAIL 深浅比例异常'); ok = False

    print('== 自检 2b/4  全掩码格式信息一致性 ==')
    all_masks_ok = True
    for mask in range(8):
        m2, fn2 = _build_base(2)
        _place_format(m2, 25, mask)
        fc, sc = _format_coords(25)
        v1, v2 = _read_format(m2, 25, fc), _read_format(m2, 25, sc)
        good = (v1 == v2) and _bch_ok(v1) and _format_payload(v1) == (ECL_L_BITS, mask)
        if not good:
            all_masks_ok = False
            print('   FAIL 掩码 %d 格式信息校验失败' % mask)
    if all_masks_ok:
        print('   PASS 掩码 0-7 全部通过 BCH 与掩码号回读校验')
    else:
        ok = False

    print('== 自检 2c/4  端到端解码往返（模拟扫描器解码）==')
    samples = [
        'http://127.0.0.1:8000/',
        'http://192.168.31.177:8000/',
        'http://192.168.100.254:65535/robot.html',
        'http://10.0.0.9:8123/%E6%9C%BA%E5%99%A8%E4%BA%BA%E6%89%8B%E6%9C%BA%E6%8E%A7%E5%88%B6%E5%8F%B0.html',
    ]
    for text in samples:
        try:
            m3, ver3, mask3, _ = make_qr(text)
            got, dv, dm = decode_qr_matrix(m3)
        except Exception as e:
            print('   FAIL 解码异常：%s' % e)
            print('        原文：%s' % text)
            ok = False
            continue
        same = (got == text)
        print('   %s 版本%d 掩码%d' % ('PASS' if same else 'FAIL', dv, dm))
        print('     原文：%s' % text)
        if not same:
            print('     解出：%s' % got)
            ok = False
        if dm != mask3 or dv != ver3:
            print('     FAIL 版本或掩码回读不一致')
            ok = False

    print('== 自检 2d/4  完整性校验灵敏度 ==')
    print('   说明：本模块的解码器用 RS 重算比对做完整性检测，未实现纠错译码；')
    print('         真实扫码由手机侧成熟解码库完成纠错。此处验证检测确实有效。')
    text4 = 'http://192.168.1.50:8000/'
    m4, ver4, mask4, _ = make_qr(text4)
    total4, ec4 = QR_TABLE[ver4]
    base4, fn4 = _build_base(ver4)
    # 未损坏时必须能解出
    try:
        got0, _, _ = decode_qr_matrix(m4)
        if got0 == text4:
            print('   PASS 未损坏样本解码正确（版本%d 纠错码字%d）' % (ver4, ec4))
        else:
            print('   FAIL 未损坏样本内容不符'); ok = False
    except Exception as e:
        print('   FAIL 未损坏样本解码异常：%s' % e); ok = False

    # 损坏后必须被检出（而非静默返回错误内容）
    import random as _rnd
    detected, silent_bad, missed = 0, 0, 0
    for trial in range(12):
        _rnd.seed(20261105 + trial)
        m4d = [row[:] for row in m4]
        flips = 1 + trial          # 1..12 个模块
        placed, tries = 0, 0
        while placed < flips and tries < 4000:
            tries += 1
            r = _rnd.randrange(len(m4d)); c = _rnd.randrange(len(m4d))
            if fn4[r][c]:
                continue
            m4d[r][c] ^= 1
            placed += 1
        try:
            got4, _, _ = decode_qr_matrix(m4d)
            if got4 == text4:
                missed += 1        # 损坏落在填充位等无影响区域
            else:
                silent_bad += 1    # 最危险：内容变了却没报错
        except Exception:
            detected += 1
    print('   12 组损坏样本：检出异常 %d 组，无影响 %d 组，静默错误 %d 组'
          % (detected, missed, silent_bad))
    if silent_bad:
        print('   FAIL 存在未被检出的内容篡改'); ok = False
    elif detected == 0:
        print('   FAIL 完整性校验无灵敏度'); ok = False
    else:
        print('   PASS 所有内容篡改均被检出，无静默错误')

    print('== 自检 3/4  PNG 输出 ==')
    m, ver, mask, pen = make_qr('http://127.0.0.1:8000/')
    n = write_png(m, QR_PNG)
    with open(QR_PNG, 'rb') as f:
        head = f.read(8)
    print('   写入 %s（%d 字节），PNG 签名 %s'
          % (QR_PNG, n, 'PASS' if head == b'\x89PNG\r\n\x1a\n' else 'FAIL'))
    if head != b'\x89PNG\r\n\x1a\n':
        ok = False

    print('== 自检 4/4  索引页生成 ==')
    page = build_index('192.168.1.100', 8000)
    checks = {
        'viewport meta': 'name="viewport"' in page,
        '二维码引用': '/qr.png' in page,
        '访问地址': 'http://192.168.1.100:8000' in page,
        'HTML 结构闭合': page.rstrip().endswith('</html>'),
        '文件列表非空': 'class="file"' in page or '目录为空' in page,
    }
    for k, v in checks.items():
        print('   %s %s' % ('PASS' if v else 'FAIL', k))
        if not v:
            ok = False

    print('')
    print('自检结果：%s' % ('全部通过' if ok else '存在失败项'))
    return 0 if ok else 1


# =====================================================================
# SECTION: cli-main  命令行入口
# =====================================================================
BANNER = r"""
  ============================================================
   机器人手机控制台 · 局域网访问服务
   零依赖 Python 静态服务器 + 内置二维码生成
  ============================================================
"""


def run(args):
    _try_utf8_stdout()

    if args.selftest:
        return selftest()

    host_ip = detect_ip()
    is_local_only = host_ip.startswith('127.')

    if args.qr_only:
        url = 'http://%s:%d/' % (args.ip or host_ip, args.port)
        m, ver, mask, pen = make_qr(url)
        write_png(m, QR_PNG)
        _safe_print(BANNER)
        _safe_print('  访问地址：%s' % url)
        _safe_print('  QR 版本 %d · 掩码 %d · 罚分 %d' % (ver, mask, pen))
        _safe_print('  二维码图片：%s' % QR_PNG)
        _safe_print('')
        _safe_print(qr_ascii(m))
        _safe_print('')
        _safe_print('  （--qr-only 模式：未启动 HTTP 服务）')
        return 0

    port = args.port
    if not args.port:
        port = pick_port(DEFAULT_PORT)
        if port is None:
            _safe_print('错误：%d-%d 端口均被占用，请用 --port 指定其他端口'
                        % (DEFAULT_PORT, DEFAULT_PORT + PORT_RANGE - 1))
            return 2
    elif not _port_free(port):
        _safe_print('错误：端口 %d 已被占用，请换端口或关闭占用程序' % port)
        return 2

    if args.ip:
        host_ip = args.ip

    url = 'http://%s:%d/' % (host_ip, port)
    try:
        matrix, ver, mask, pen = make_qr(url)
        write_png(matrix, QR_PNG)
    except Exception as e:
        _safe_print('警告：二维码生成失败（%s），仍可手动输入地址访问' % e)
        matrix = None

    handler = lambda *a, **kw: Handler(*a, host_ip=host_ip, host_port=port, **kw)
    try:
        httpd = ThreadedServer(('0.0.0.0', port), handler)
    except OSError as e:
        _safe_print('错误：无法绑定端口 %d（%s）' % (port, e))
        return 2

    _safe_print(BANNER)
    if is_local_only:
        _safe_print('  ⚠ 未探测到局域网 IP，当前仅本机可访问。')
        _safe_print('    手机访问请确认电脑已连 Wi-Fi，并用 --ip 指定电脑局域网地址。')
    _safe_print('  本机访问：http://127.0.0.1:%d/' % port)
    _safe_print('  手机访问：%s' % url)
    _safe_print('  服务目录：%s' % ROOT)
    if matrix:
        _safe_print('  二维码版本 %d · 掩码 %d · 已保存 %s' % (ver, mask, QR_PNG))
    _safe_print('')
    _safe_print('  ┌─ 用手机相机或浏览器扫码 ─────────────┐')
    if matrix:
        for line in qr_ascii(matrix).split('\n'):
            _safe_print('   ' + line)
    _safe_print('  └──────────────────────────────────────┘')
    _safe_print('')
    _safe_print('  提示：手机需与本机连接同一 Wi-Fi。按 Ctrl+C 停止服务。')
    if not is_local_only and args.browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    _safe_print('')

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _safe_print('\n  已收到停止信号，正在关闭服务…')
    finally:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass
        _safe_print('  服务已停止。')
    return 0


def _port_free(port):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('0.0.0.0', port))
        s.close()
        return True
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser(
        description='局域网静态文件服务器 + 二维码生成（零第三方依赖）')
    ap.add_argument('--port', type=int, default=0,
                    help='监听端口，默认 %d（被占用时自动顺延）' % DEFAULT_PORT)
    ap.add_argument('--ip', default='',
                    help='手动指定对外展示的 IP（自动探测不准时使用）')
    ap.add_argument('--qr-only', action='store_true',
                    help='只生成二维码并打印，不启动 HTTP 服务')
    ap.add_argument('--selftest', action='store_true',
                    help='运行内部自检后退出（不绑定端口）')
    ap.add_argument('--browser', action='store_true',
                    help='启动后用本机默认浏览器打开索引页')
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == '__main__':
    main()
