# -*- coding: utf-8 -*-
"""微信 4.x 进程内存取密钥 —— 探测 / 标记扫描 / 直扫。

在 Windows 侧 Python 3.12 下运行（Linux 侧无法读 Windows 进程内存）。
用法：
    python wx4_memscan.py probe
    python wx4_memscan.py marker  --db <path> [--out result.json]
    python wx4_memscan.py sweep   --db <path> [--out result.json] [--procs N]
    python wx4_memscan.py follow  --db <path> [--out result.json]     # marker + 指针解引用 + 校验

不写任何明文密钥到 stdout 之外的持久位置（除 --out）。
"""
import argparse, ctypes, ctypes.wintypes as wt, json, os, struct, sys, time

# 这个工具只能在 Windows 上干活（要读 Windows 进程内存），但**导入**不应该失败 ——
# 否则在 Linux 上连 `--help` 都看不到。真正的平台检查放在 main() 里。
if sys.platform == 'win32':
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
else:
    k32 = psapi = None

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
MEM_IMAGE = 0x1000000
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80
RW_PROTECT = (PAGE_READWRITE, PAGE_WRITECOPY, PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY)


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_char * 260)]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD), ("RegionSize", ctypes.c_size_t),
                ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]


def list_processes(name="Weixin.exe"):
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if snap == -1:
        return []
    pe = PROCESSENTRY32(); pe.dwSize = ctypes.sizeof(pe)
    out = []
    ok = k32.Process32First(snap, ctypes.byref(pe))
    while ok:
        exe = pe.szExeFile.decode('mbcs', 'replace')
        if exe.lower() == name.lower():
            out.append(pe.th32ProcessID)
        ok = k32.Process32Next(snap, ctypes.byref(pe))
    k32.CloseHandle(snap)
    return out


def working_set(pid):
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return 0
    pmc = PROCESS_MEMORY_COUNTERS(); pmc.cb = ctypes.sizeof(pmc)
    r = psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
    k32.CloseHandle(h)
    return pmc.WorkingSetSize if r else 0


def regions(pid):
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None, []
    out = []
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    maxaddr = 0x7FFFFFFFFFFF
    while addr < maxaddr:
        got = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not got:
            break
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize
        if size == 0:
            break
        if mbi.State == MEM_COMMIT and mbi.Protect in RW_PROTECT:
            out.append((base, size, mbi.Type, mbi.Protect))
        addr = base + size
    return h, out


def read_mem(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)):
        return b''
    return buf.raw[:n.value]


def find_all(buf, needle):
    out = []
    i = buf.find(needle)
    while i >= 0:
        out.append(i)
        i = buf.find(needle, i + 1)
    return out


# ------------------------------------------------------------------ 目标校验
def find_active_db():
    """挑最近改动的 4.x message_0.db。"""
    import glob
    pats = [os.path.join(os.path.expanduser('~'), 'Documents', 'xwechat_files', '*', 'db_storage', 'message', 'message_0.db')]
    best, bt = None, -1
    for pat in pats:
        for f in glob.glob(pat):
            try:
                t = os.path.getmtime(f)
            except OSError:
                continue
            if t > bt:
                best, bt = f, t
    return best


def make_targets(db_path):
    """返回 [(name, iv, ct_block, salt), ...]，供单块 AES-CBC 校验。"""
    b = open(db_path, 'rb').read(4096 + 16)
    t = []
    if len(b) >= 4096:
        for R in (48, 32, 64, 16, 80, 0):
            io = 4096 - R
            if io < 16 or io + 16 > 4096:
                continue
            t.append((f"R{R}", b[io:io + 16], b[16:32], b[:16]))
    return t


def valid_page1_header(dec):
    """page-1 明文（magic 之后那一段）的开头。"""
    if len(dec) < 8:
        return False
    if dec[0] != 0x10 or dec[1] != 0x00:
        return False
    if dec[2] != 1 or dec[3] != 1:
        return False
    return dec[5] == 0x40 and dec[6] == 0x20 and dec[7] == 0x20


def check_cand(cand, targets, AES):
    for name, iv, ct, salt in targets:
        try:
            dec = AES.new(cand, AES.MODE_CBC, iv).decrypt(ct)
        except Exception:
            continue
        if valid_page1_header(dec):
            return name, dec.hex()
    return None



def anchor_mode(h, regs, targets, AES, needles, window, chunk):
    """在内存里找 DB 路径/文件名（UTF-16LE + UTF-8），再在锚点周围扫密钥。"""
    print(f"[anchor] 目标字符串: {needles}")
    anchors = []
    t0 = time.time()
    done = 0
    for b, s, t, p in regs:
        if t != MEM_PRIVATE:
            continue
        off = 0
        while off < s:
            n = min(chunk, s - off)
            buf = read_mem(h, b + off, n)
            if buf:
                for nd in needles:
                    for enc, raw in (('u16', nd.encode('utf-16-le')), ('u8', nd.encode('utf-8'))):
                        i = buf.find(raw)
                        while i >= 0:
                            anchors.append((b + off + i, nd, enc))
                            i = buf.find(raw, i + 1)
            off += n
            done += n
        if (time.time() - t0) > 5:
            print(f"    ... 扫锚点 {done/1048576:.0f} MB  锚点={len(anchors)}")
            t0 = time.time(); done = 0
    print(f"[anchor] 锚点命中 {len(anchors)} 个")
    for a, nd, enc in anchors[:30]:
        print(f"    0x{a:012x} [{enc}] {nd}")

    # 去重窗口
    wsets = []
    for a, nd, enc in anchors:
        lo, hi = max(0, a - window), a + window
        for w in wsets:
            if not (hi < w[0] or lo > w[1]):
                w[0], w[1] = min(w[0], lo), max(w[1], hi)
                break
        else:
            wsets.append([lo, hi])
    total = sum(hi - lo for lo, hi in wsets)
    print(f"[anchor] 合并后 {len(wsets)} 个窗口，合计 {total/1048576:.1f} MB，按 4 字节步长校验")
    t0 = time.time(); cands = set(); tested = 0
    for lo, hi in wsets:
        pos = lo
        while pos < hi:
            n = min(chunk, hi - pos)
            buf = read_mem(h, pos, n)
            if buf:
                lim = len(buf) - 32
                i = 0
                while i <= lim:
                    c = buf[i:i+32]
                    r = check_cand(c, targets, AES)
                    if r:
                        cands.add((pos + i, c.hex(), r[0]))
                    i += 4
                tested += max(0, lim // 4 + 1)
            pos += n
    print(f"[anchor] 校验 {tested/1e6:.1f}M 个候选，用时 {time.time()-t0:.0f}s，命中 {len(cands)}")
    return cands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=['probe', 'marker', 'follow', 'sweep', 'anchor'])
    ap.add_argument('--db')
    ap.add_argument('--out')
    ap.add_argument('--procs', type=int, default=0)
    ap.add_argument('--exe', default='Weixin.exe')
    ap.add_argument('--chunk', type=int, default=8 << 20)
    ap.add_argument('--window', type=int, default=1 << 20, help='anchor 模式：锚点周围扫描半径(字节)')
    ap.add_argument('--needles', default='message_0.db,message_fts.db,session.db,db_storage',
                    help='anchor 模式：要找的字符串（逗号分隔）')
    args = ap.parse_args()

    if sys.platform != 'win32':
        print('[!] 本工具只能读 Windows 进程内存，请在 Windows 侧 Python 下运行'
              '（WSL 里用 python.exe -X utf8 调用）')
        return 2

    pids = list_processes(args.exe)
    if not pids:
        print(f"[!] 没有找到 {args.exe}")
        return 1
    info = sorted(((working_set(p), p) for p in pids), reverse=True)
    print(f"[i] {args.exe} 进程 {len(pids)} 个：")
    for ws, p in info:
        print(f"    pid={p:<7} working_set={ws/1048576:.1f} MB")

    pid = info[0][1]
    print(f"[i] 主进程 pid={pid}")

    h, regs = regions(pid)
    if h is None:
        print("[!] OpenProcess 失败（可能需要提权）")
        return 1
    tot = sum(s for _, s, _, _ in regs)
    rw = sum(s for _, s, t, _ in regs if t == MEM_PRIVATE)
    print(f"[i] 可读区域 {len(regs)} 个，合计 {tot/1048576:.1f} MB（其中 PRIVATE {rw/1048576:.1f} MB）")

    if args.mode == 'probe':
        for b, s, t, p in sorted(regs, key=lambda x: -x[1])[:25]:
            print(f"    0x{b:012x}  {s/1048576:8.2f} MB  type={t:#x} prot={p:#x}")
        k32.CloseHandle(h)
        return 0

    db_path = args.db or find_active_db()
    targets = make_targets(db_path) if db_path else []
    if args.db:
        print(f"[i] 校验目标库：{db_path}")
        for n, iv, ct, salt in targets:
            print(f"    {n}: iv={iv.hex()} ct={ct.hex()}")

    # 标记：全零 qword + 0x20 + 0x2f（小端 qword 形式）
    marker = b"\x00" * 8 + struct.pack("<Q", 0x20) + struct.pack("<Q", 0x2f)
    marker_loose = b"\x00" * 8 + struct.pack("<Q", 0x20)   # 只到 0x20，看命中量级

    hits_marker, hits_loose, cands = [], 0, set()
    if args.mode == 'anchor':
        try:
            from Crypto.Cipher import AES as _AES
            AES = _AES
        except ImportError:
            print("[!] 缺少 pycryptodome")
            return 1
        if not targets:
            print("[!] anchor 模式需要 --db 或自动发现的 4.x 库")
            return 1
        cands = anchor_mode(h, regs, targets, AES,
                            [x for x in args.needles.split(',') if x], args.window, args.chunk)
        for addr, hx, tag in sorted(cands):
            print(f"    addr=0x{addr:012x} [{tag}] {hx}")
        if cands and args.out:
            json.dump({"db": db_path, "pid": pid,
                       "candidates": [{"addr": a, "hex": hx, "reserve": tg} for a, hx, tg in sorted(cands)]},
                      open(args.out, 'w'), indent=1)
            print(f"[i] 已写 {args.out}")
        k32.CloseHandle(h)
        return 0 if cands else 2
    AES = None
    if args.mode in ('sweep', 'follow') or targets:
        try:
            from Crypto.Cipher import AES as _AES
            AES = _AES
        except ImportError:
            print("[!] 缺少 pycryptodome：python -m pip install pycryptodome")
            return 1

    t0 = time.time()
    done = 0
    for b, s, t, p in regs:
        if args.mode in ('marker', 'follow') and t != MEM_PRIVATE:
            continue  # 结构体在私有堆里
        off = 0
        while off < s:
            n = min(args.chunk, s - off)
            buf = read_mem(h, b + off, n)
            if buf:
                if args.mode in ('marker', 'follow'):
                    for i in find_all(buf, marker):
                        hits_marker.append(b + off + i)
                    hits_loose += len(find_all(buf, marker_loose))
                else:
                    for name, iv, ct, salt in targets:
                        try:
                            c = AES.new(b'\x00' * 32, AES.MODE_CBC, iv)
                        except Exception:
                            pass
                    step = 4
                    lim = len(buf) - 32
                    i = 0
                    while i <= lim:
                        cand = buf[i:i + 32]
                        r = check_cand(cand, targets, AES)
                        if r:
                            cands.add((b + off + i, cand.hex(), r[0]))
                        i += step
            off += n
            done += n
        if (time.time() - t0) > 2:
            el = time.time() - t0
            print(f"    ... {done/1048576:.0f} MB / {s*tot and tot/1048576:.0f} MB  ({el:.0f}s)  marker={len(hits_marker)} key={len(cands)}")
            t0 = time.time(); done = 0

    if args.mode in ('marker', 'follow'):
        print(f"[i] 精确 24B 标记命中：{len(hits_marker)}")
        for a in hits_marker[:40]:
            print(f"      at 0x{a:012x}")
        print(f"[i] 宽松前缀(8 零 + qword 0x20)命中：{hits_loose}")
        if args.mode == 'follow' and hits_marker:
            print("[i] 解引用标记前的 qword：")
            seen = set()
            for a in hits_marker:
                pb = read_mem(h, a - 8, 8)
                if len(pb) < 8:
                    continue
                ptr = struct.unpack("<Q", pb)[0]
                if ptr in seen or not (0x10000 < ptr < 0x7FFFFFFFFFFF):
                    continue
                seen.add(ptr)
                cand = read_mem(h, ptr, 32)
                if len(cand) < 32:
                    continue
                r = check_cand(cand, targets, AES) if (targets and AES) else None
                tag = f"  ==> 命中 {r[0]}" if r else ""
                print(f"      marker@0x{a:012x} -> ptr=0x{ptr:012x} cand={cand.hex()}{tag}")
                if r:
                    cands.add((ptr, cand.hex(), r[0]))

    if cands:
        print(f"\n[+] 找到 {len(cands)} 个通过校验的候选：")
        for addr, hx, tag in sorted(cands):
            print(f"    addr=0x{addr:012x} [{tag}] {hx}")
        if args.out:
            json.dump({"db": args.db, "pid": pid,
                       "candidates": [{"addr": a, "hex": hx, "reserve": tag} for a, hx, tag in sorted(cands)]},
                      open(args.out, 'w'), indent=1)
            print(f"[i] 已写 {args.out}")
    else:
        print("\n[-] 没有候选通过校验")

    k32.CloseHandle(h)
    return 0


if __name__ == '__main__':
    sys.exit(main())
