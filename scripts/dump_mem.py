#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把目标进程的**用户态地址空间**转储成一个扁平文件 + 一份 VA 与偏移索引。

用途：32 位 WOW64 的微信进程，由 64 位 Python 来读。产物给 `sweep.py` 一类工具扫。

索引文件（`mem_<pid>.index.json`）记录每个被写入区段的
`{va, off, size, prot, type}`，也就是说：**转储文件里的每个字节都能还原回它的虚拟地址** ——
这是内存扫描能报出「命中在 0x........」的前提。

用法：
    python dump_mem.py --process WeChat.exe --out ./dump
    python dump_mem.py --selftest

环境变量（`--process` / `--out` 未给出时作为兜底）：
    WX_PROCESS   要转储的映像名（默认 WeChat.exe）
    WX_DUMP_DIR  输出目录（默认 ./dump）

仅 Windows（kernel32 / advapi32 走 ctypes）。非 Windows 上 `--help` 与 `--selftest` 仍可用。
退出码：0 = 成功；1 = 没找到进程或读内存失败；2 = 参数错误；77 = 自检跳过（非 Windows）。
"""
import ctypes
from ctypes import wintypes
import argparse
import json
import os
import sys

SKIP = 77

# 非 Windows 上没有 WinDLL。**不在导入期就炸** —— 否则连 `--help` 都看不到。
# 真正的平台检查放在 require_windows() 里，由 main() 调用。
if hasattr(ctypes, 'WinDLL'):
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
else:
    k32 = advapi32 = None

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
TH32CS_SNAPPROCESS = 0x00000002
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
MAX_PATH = 260
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# 定宽类型：**不要**用 ctypes.wintypes.DWORD / LONG —— 在非 Windows 上它们是
# C 的 unsigned long / long，Linux LP64 下宽 8 字节，整个结构体布局都会错位。
# 写死宽度后，`--selftest` 在 Linux 上也能校验出这套布局是对的。
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32

TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x0002
SE_DEBUG_NAME = 'SeDebugPrivilege'


class LUID(ctypes.Structure):
    _fields_ = [('LowPart', DWORD), ('HighPart', LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [('Luid', LUID), ('Attributes', DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [('PrivilegeCount', DWORD),
                ('Privileges', LUID_AND_ATTRIBUTES * 1)]


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [('dwSize', DWORD),
                ('cntUsage', DWORD),
                ('th32ProcessID', DWORD),
                ('th32DefaultHeapID', ctypes.c_void_p),
                ('th32ModuleID', DWORD),
                ('cntThreads', DWORD),
                ('th32ParentProcessID', DWORD),
                ('pcPriClassBase', LONG),
                ('dwFlags', DWORD),
                ('szExeFile', ctypes.c_char * MAX_PATH)]


class MBI(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_ulonglong),
                ('AllocationBase', ctypes.c_ulonglong),
                ('AllocationProtect', DWORD),
                ('__a1', DWORD),
                ('RegionSize', ctypes.c_ulonglong),
                ('State', DWORD),
                ('Protect', DWORD),
                ('Type', DWORD),
                ('__a2', DWORD)]


def enable_debug_privilege():
    h = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(k32.GetCurrentProcess(),
                                     TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                     ctypes.byref(h)):
        return False, 'OpenProcessToken failed %d' % ctypes.get_last_error()
    luid = LUID()
    if not advapi32.LookupPrivilegeValueW(None, SE_DEBUG_NAME, ctypes.byref(luid)):
        return False, 'LookupPrivilegeValue failed %d' % ctypes.get_last_error()
    tp = TOKEN_PRIVILEGES()
    tp.PrivilegeCount = 1
    tp.Privileges[0].Luid = luid
    tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
    if not advapi32.AdjustTokenPrivileges(h, False, ctypes.byref(tp),
                                          ctypes.sizeof(tp), None, None):
        return False, 'AdjustTokenPrivileges failed %d' % ctypes.get_last_error()
    err = ctypes.get_last_error()
    if err != 0:
        return False, 'AdjustTokenPrivileges error %d' % err
    return True, 'ok'


def find_pid(name):
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    # CreateToolhelp32Snapshot 失败返回 INVALID_HANDLE_VALUE。它的 restype 已声明为
    # c_void_p，所以这里必须和**指针宽度**的 -1 比；拿 c_int 时代的 -1 去比永远不相等。
    if not snap or snap == INVALID_HANDLE_VALUE:
        return []
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
    pids = []
    ok = k32.Process32First(snap, ctypes.byref(pe))
    while ok:
        exe = pe.szExeFile.decode('mbcs', 'replace')
        if exe.lower() == name.lower():
            pids.append(pe.th32ProcessID)
        ok = k32.Process32Next(snap, ctypes.byref(pe))
    k32.CloseHandle(snap)
    return pids


def _declare_functions():
    """显式声明 argtypes / restype。

    不声明时，ctypes 会把返回值当成 C `int`(32 位)、把 Python int 实参也按 32 位传。
    Windows 的 HANDLE 有 64 位宽，`OpenProcess` 的返回值一旦被截断，后续
    `VirtualQueryEx` / `ReadProcessMemory` 拿到的就是坏句柄 —— 这类问题在
    句柄值恰好小于 2^31 时看不出来，所以很难在真机上发现。一并声明掉。
    """
    k32.GetCurrentProcess.argtypes = []
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.GetCurrentProcessId.argtypes = []
    k32.GetCurrentProcessId.restype = DWORD
    k32.OpenProcess.argtypes = [DWORD, wintypes.BOOL, DWORD]
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.IsWow64Process.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL)]
    k32.IsWow64Process.restype = wintypes.BOOL
    k32.VirtualQueryEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.POINTER(MBI), ctypes.c_size_t]
    k32.VirtualQueryEx.restype = ctypes.c_size_t
    k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_size_t)]
    k32.ReadProcessMemory.restype = wintypes.BOOL
    k32.CreateToolhelp32Snapshot.argtypes = [DWORD, DWORD]
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.Process32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32)]
    k32.Process32First.restype = wintypes.BOOL
    k32.Process32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32)]
    k32.Process32Next.restype = wintypes.BOOL

    advapi32.OpenProcessToken.argtypes = [ctypes.c_void_p, DWORD,
                                          ctypes.POINTER(ctypes.c_void_p)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.LookupPrivilegeValueW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p,
                                               ctypes.POINTER(LUID)]
    advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
    advapi32.AdjustTokenPrivileges.argtypes = [
        ctypes.c_void_p, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES), DWORD,
        ctypes.c_void_p, ctypes.POINTER(DWORD)]
    advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL


if k32 is not None:
    _declare_functions()


def require_windows():
    if k32 is None:
        raise SystemExit('本工具只能在 Windows 上运行（需要 kernel32 / advapi32）。')


def build_parser():
    p = argparse.ArgumentParser(
        prog='dump_mem.py',
        description='把目标进程的用户态地址空间转储成扁平文件 + VA 与偏移索引。',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--process', metavar='NAME',
                   default=os.environ.get('WX_PROCESS', 'WeChat.exe'),
                   help='要转储的映像名（默认 %(default)s；环境变量 WX_PROCESS）')
    p.add_argument('--out', metavar='DIR',
                   default=os.environ.get('WX_DUMP_DIR', os.path.join(
                       os.path.dirname(os.path.abspath(__file__)), 'dump')),
                   help='输出目录（默认 ./dump；环境变量 WX_DUMP_DIR）')
    p.add_argument('--selftest', action='store_true',
                   help='跑自检后退出（非 Windows 上只报 SKIP）')
    return p


def selftest():
    """结构体布局自检；Windows 上再验证一次「能枚举自己进程的可读区段」。"""
    if ctypes.sizeof(DWORD) != 4 or ctypes.sizeof(LONG) != 4 or ctypes.sizeof(LUID) != 8:
        print('SELFTEST FAILED: 基础结构体大小不符（DWORD/LONG/LUID）')
        return 1
    # PROCESSENTRY32 的字段顺序必须和 Windows 一致，否则进程名会对错位。
    # 按指针宽度推导期望偏移：3 个 DWORD 之后是指针（64 位要填充到 16），
    # 指针之后 5 个 DWORD/LONG，再往后是 szExeFile[MAX_PATH]。
    ptr = ctypes.sizeof(ctypes.c_void_p)
    off_exe = ((12 + ptr - 1) // ptr * ptr) + ptr + 20
    total = off_exe + MAX_PATH
    want_size = (total + ptr - 1) // ptr * ptr
    if PROCESSENTRY32.szExeFile.offset != off_exe:
        print('SELFTEST FAILED: szExeFile 偏移 %d != 期望 %d'
              % (PROCESSENTRY32.szExeFile.offset, off_exe))
        return 1
    if ctypes.sizeof(PROCESSENTRY32) != want_size:
        print('SELFTEST FAILED: PROCESSENTRY32 大小 %d != 期望 %d'
              % (ctypes.sizeof(PROCESSENTRY32), want_size))
        return 1
    if k32 is None:
        print('SKIP: 非 Windows —— 只做了结构体自检，跳过了内核调用')
        return SKIP
    ok, msg = enable_debug_privilege()
    print('SeDebugPrivilege: %s (%s)' % (ok, msg))
    if not ok:
        print('              （非提权运行时这是正常的；本自检只转储它自己，'
              '所以不需要该权限。要转储**别的**进程才需要管理员权限的 shell）')
    pid = k32.GetCurrentProcessId()
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        print('SELFTEST FAILED: 打不开自己的进程，err=%d' % ctypes.get_last_error())
        return 1
    addr, n = 0, 0
    mbi = MBI()
    while addr < 0x7FFFFFFF0000:
        r = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                               ctypes.sizeof(mbi))
        if not r:
            break
        if mbi.State == MEM_COMMIT and mbi.RegionSize > 0:
            n += 1
        addr = mbi.BaseAddress + mbi.RegionSize
        if mbi.RegionSize == 0:
            addr += 0x1000
    k32.CloseHandle(h)
    if n == 0:
        print('SELFTEST FAILED: 枚举自己的已提交区段得到 0 个')
        return 1
    print('SELFTEST PASSED（结构体一致，枚举到自己 %d 个已提交区段）' % n)
    return 0


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    require_windows()
    name, out_dir = args.process, args.out
    if not name or not out_dir:
        ap.error('--process 与 --out 都不能为空')
    os.makedirs(out_dir, exist_ok=True)

    ok, msg = enable_debug_privilege()
    print('SeDebugPrivilege: %s (%s)' % (ok, msg))

    pids = find_pid(name)
    print('matching PIDs for %s: %s' % (name, pids))
    if not pids:
        return 1

    regions_all = []
    for pid in pids:
        h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            print('  pid %d: OpenProcess failed err=%d' % (pid, ctypes.get_last_error()))
            continue
        is64 = wintypes.BOOL(0)
        if not k32.IsWow64Process(h, ctypes.byref(is64)):
            print('  pid %d: IsWow64Process failed err=%d'
                  % (pid, ctypes.get_last_error()))
        print('  pid %d: wow64=%s' % (pid, bool(is64.value)))

        addr = 0
        mbi = MBI()
        regions = []
        while addr < 0x7FFFFFFF0000:
            r = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                   ctypes.sizeof(mbi))
            if not r:
                break
            base = mbi.BaseAddress
            size = mbi.RegionSize
            if (mbi.State == MEM_COMMIT and size > 0
                    and not (mbi.Protect & PAGE_NOACCESS)
                    and not (mbi.Protect & PAGE_GUARD)):
                regions.append((base, size, mbi.Protect, mbi.Type))
            addr = base + size
            if size == 0:
                addr += 0x1000
        print('  pid %d: %d readable committed regions' % (pid, len(regions)))

        buf = ctypes.create_string_buffer(1 << 20)
        read = ctypes.c_size_t(0)
        out_path = os.path.join(out_dir, 'mem_%d.bin' % pid)
        index = []
        total = 0
        with open(out_path, 'wb') as f:
            for (base, size, prot, typ) in regions:
                off = 0
                while off < size:
                    chunk = min(1 << 20, size - off)
                    if k32.ReadProcessMemory(h, ctypes.c_void_p(base + off),
                                             ctypes.cast(buf, ctypes.c_void_p),
                                             chunk, ctypes.byref(read)):
                        n = read.value
                        f.write(buf.raw[:n])
                        index.append({'va': base + off, 'off': total, 'size': n,
                                      'prot': prot, 'type': typ})
                        total += n
                        off += n if n else chunk
                    else:
                        off += chunk
        k32.CloseHandle(h)
        with open(os.path.join(out_dir, 'mem_%d.index.json' % pid), 'w') as f:
            json.dump(index, f)
        print('  pid %d: dumped %.1f MB (readable regions) -> %s'
              % (pid, total / 1048576.0, out_path))
        regions_all.append({'pid': pid, 'file': out_path, 'bytes': total})
    return 0


if __name__ == '__main__':
    sys.exit(main())
