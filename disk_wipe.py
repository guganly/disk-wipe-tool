#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================
  数据中心硬盘安全擦除工具  v1.15
  适用场景：数据中心退役服务器硬盘批量销毁
  支持：USB转SATA / USB转NVMe 接口
=============================================================
  操作流程：
    1. 启动程序（需管理员权限）
    2. 将硬盘通过USB线缆接入笔记本
    3. 程序自动检测到新盘，展示盘信息
    4. 确认后开始安全擦除
    5. 擦除完成播放提示音
    6. 拔盘插入下一块，重复流程
=============================================================
"""

import io
import sys
import os
# Force UTF-8 output so Chinese characters don't crash on GBK consoles
# Try stdout/stderr reconfigure first; fall back to wrapping with TextIOWrapper
for stream_name in ("stdout", "stderr"):
    stream = getattr(sys, stream_name)
    try:
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
        else:
            setattr(sys, stream_name, io.TextIOWrapper(
                stream.buffer, encoding="utf-8", errors="replace"
            ))
    except Exception:
        pass

import ctypes
import struct
import subprocess
import threading
import time
import tkinter as tk
import winsound
from datetime import datetime
from tkinter import messagebox, scrolledtext, ttk

import queue

# ── 所有 subprocess 调用统一隐藏控制台窗口 ──
_SUBP_KWARGS = {
    "capture_output": True,
    "text": True,
    "creationflags": subprocess.CREATE_NO_WINDOW,
}

def _run(*args, timeout=15, **kwargs):
    """封装 subprocess.run，统一隐藏控制台窗口"""
    kw = {**_SUBP_KWARGS, "timeout": timeout, **kwargs}
    return subprocess.run(*args, **kw)

# ──────────────────────────────────────────────────────────
#  管理员权限检查
# ──────────────────────────────────────────────────────────
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

def run_as_admin():
    """以管理员权限重新启动自身"""
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, " ".join(sys.argv), None, 1
    )

# ──────────────────────────────────────────────────────────
#  磁盘信息工具
# ──────────────────────────────────────────────────────────

def get_all_physical_disks():
    """获取所有物理磁盘信息，返回列表"""
    disks = []
    try:
        result = _run(
            ["wmic", "diskdrive", "get",
             "DeviceID,Model,Size,InterfaceType,MediaType,SerialNumber",
             "/format:csv"],
            timeout=15
        )
        lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
        if len(lines) < 2:
            return disks
        header = [h.strip() for h in lines[0].split(",")]
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) < len(header):
                continue
            record = dict(zip(header, parts))
            device_id = record.get("DeviceID", "").strip()
            if not device_id:
                continue
            size_bytes = int(record.get("Size", "0") or 0)
            size_gb = size_bytes / (1024 ** 3)
            disks.append({
                "device_id": device_id,
                "model": record.get("Model", "Unknown").strip(),
                "interface": record.get("InterfaceType", "Unknown").strip(),
                "media_type": record.get("MediaType", "Unknown").strip(),
                "serial": record.get("SerialNumber", "N/A").strip(),
                "size_gb": size_gb,
                "size_bytes": size_bytes,
            })
    except Exception as e:
        pass
    return disks


def get_disk_index(device_id):
    r"""从 \\.\PhysicalDriveN / \\.\PHYSICALDRIVEN 提取磁盘编号"""
    import re
    m = re.search(r"PhysicalDrive(\d+)$", device_id, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def get_system_disk_index():
    """
    检测系统盘（Windows 安装所在物理磁盘编号）。
    返回 PhysicalDrive 编号（如 0）；检测失败返回 None。
    """
    import re
    try:
        # 1. 获取系统盘符，如 C:
        r = _run(
            ["wmic", "os", "get", "SystemDrive", "/format:csv"],
            timeout=10
        )
        m = re.search(r"([A-Z]):", r.stdout)
        if not m:
            return None
        sys_drive = m.group(1) + ":"

        # 2. 通过逻辑磁盘→分区→物理磁盘反查 DiskIndex
        #    用 wmic path Win32_DiskDriveToDiskPartition 一步到位
        r2 = _run(
            ["wmic", "path", "Win32_DiskDriveToDiskPartition", "get",
             "Antecedent,Dependent", "/format:csv"],
            timeout=10
        )
        # Antecedent = \\COMPUTER\root\cimv2:Win32_DiskDrive.DeviceID="\\\\.\\PHYSICALDRIVE0"
        # Dependent   = \\COMPUTER\root\cimv2:Win32_DiskPartition.DeviceID="Disk #0, Partition #1"
        partition_to_disk = {}
        for line in r2.stdout.splitlines():
            a_match = re.search(r'PhysicalDrive(\d+)', line, re.IGNORECASE)
            d_match = re.search(r'Disk #(\d+)', line)
            if a_match and d_match:
                partition_to_disk[int(d_match.group(1))] = int(a_match.group(1))

        # 3. 获取系统盘符对应的分区 DiskIndex
        r3 = _run(
            ["wmic", "partition", "get", "DeviceID,BootPartition",
             "/format:csv"],
            timeout=10
        )
        for line in r3.stdout.splitlines():
            if "TRUE" in line.upper():
                m2 = re.search(r'Disk #(\d+)', line)
                if m2:
                    part_disk_idx = int(m2.group(1))
                    disk_idx = partition_to_disk.get(part_disk_idx)
                    if disk_idx is not None:
                        return disk_idx

        # 备用方案：枚举所有分区的盘符对应
        r4 = _run(
            ["wmic", "logicaldisk", "where", f"DeviceID='{sys_drive}'",
             "get", "DeviceID", "/format:csv"],
            timeout=10
        )
        if sys_drive in r4.stdout:
            # 映射系统盘符到分区再查物理磁盘
            r5 = _run(
                ["wmic", "path", "Win32_LogicalDiskToPartition", "get",
                 "Antecedent,Dependent", "/format:csv"],
                timeout=10
            )
            for line in r5.stdout.splitlines():
                if sys_drive in line:
                    m3 = re.search(r'Disk #(\d+)', line)
                    if m3:
                        disk_idx = partition_to_disk.get(int(m3.group(1)))
                        if disk_idx is not None:
                            return disk_idx

    except Exception:
        pass
    return None

def get_volumes_for_disk(disk_index):
    """获取磁盘对应的所有盘符"""
    volumes = []
    try:
        result = _run(
            ["wmic", "partition", "where",
             f"DiskIndex={disk_index}",
             "get", "DeviceID", "/format:csv"],
            timeout=10
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if "Disk #" in line:
                partition_id = line.split(",")[-1].strip()
                # 获取逻辑磁盘
                res2 = _run(
                    ["wmic", "path", "Win32_LogicalDiskToPartition",
                     "where", f"Antecedent like '%{partition_id.replace(chr(32), '_')}%'",
                     "get", "Dependent", "/format:csv"],
                    timeout=10
                )
                for l in res2.stdout.splitlines():
                    l = l.strip()
                    if "Win32_LogicalDisk" in l:
                        import re
                        m = re.search(r'DeviceID="([A-Z]:)"', l)
                        if m:
                            volumes.append(m.group(1))
    except Exception:
        pass
    return volumes


# ──────────────────────────────────────────────────────────
#  擦除引擎
# ──────────────────────────────────────────────────────────

WIPE_MODES = {
    "⚡ Quick（清理分区表 + 首尾128MB写零）":  {"engine": "diskpart", "command": "clean"},
    "diskpart 安全清零（clean all，全盘写零）":  {"engine": "diskpart", "command": "clean all"},
    "快速清零（单次写零）":                     {"engine": "direct", "passes": [b"\x00"]},
}

# 直接 I/O 模式块大小：64MB，扇区对齐，绕过 Windows 缓冲
CHUNK_SIZE = 64 * 1024 * 1024   # 64MB — 比原来 4MB 快 ~3-5 倍
SECTOR_SIZE = 512                # 最小对齐单位


def diskpart_clean_all(disk_index: int, log_cb, stop_event: threading.Event = None,
                       command: str = "clean all") -> bool:
    """
    调用 diskpart 清理磁盘。
    command="clean"      → 仅清除分区表，1-2秒完成
    command="clean all"  → 全盘写零，慢但彻底
    stop_event: 传入 threading.Event，set() 时强制终止 diskpart 进程。
    """
    script = f"select disk {disk_index}\n{command}\nexit\n"
    is_fast = (command == "clean")
    if is_fast:
        log_cb(f"  ⚡ 极速模式：diskpart clean（仅清分区表，1-2秒）")
    proc = None
    tmp_name = None
    try:
        import tempfile, os
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                          delete=False, encoding="ascii")
        tmp.write(script)
        tmp.close()
        tmp_name = tmp.name
        log_cb(f"  Running: diskpart /s {tmp_name}")

        # 用 Popen 替代 run，以便外部可 kill
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        proc = subprocess.Popen(
            ["diskpart", "/s", tmp_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=si
        )

        # 轮询等待，每 2 秒检查一次中止信号
        start = time.time()
        while proc.poll() is None:
            if stop_event and stop_event.is_set():
                log_cb("  ⚠️ 用户中止，正在终止 diskpart 进程...")
                proc.terminate()
                time.sleep(1)
                if proc.poll() is None:
                    proc.kill()
                log_cb("  ⛔ diskpart 进程已强制终止")
                return False
            if time.time() - start > 7200:  # 2 小时硬超时
                log_cb("  ❌ diskpart 超时（超过 2 小时）")
                proc.kill()
                return False
            time.sleep(2)

        stdout, stderr = proc.communicate(timeout=10)
        # Windows diskpart outputs GBK (CP936) on Chinese systems.
        # Try GBK first, then UTF-8, then ascii as fallback.
        raw = stdout + stderr
        output = ""
        for enc in ("gbk", "utf-8", "ascii"):
            try:
                output = raw.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if not output:
            output = raw.decode("ascii", errors="replace")
        # Show only the first meaningful line (diskpart banner is noise)
        lines = [l.strip() for l in output.splitlines() if l.strip()]
        short = "; ".join(lines[:3]) if lines else "(empty)"
        log_cb(f"  diskpart 输出: {short[:200]}")
        if proc.returncode != 0:
            log_cb(f"  ❌ diskpart 返回非零: {proc.returncode}")
            return False
        if "successfully" in output.lower() or "成功" in output:
            return True
        if "diskpart succeeded" in output.lower() or "cleaned" in output.lower():
            return True
        if "成功地清除了" in output or "磁盘" in output:
            return True
        # Default: return code 0 means success
        return True
    except Exception as e:
        log_cb(f"  ❌ diskpart 异常：{e}")
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        return False
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except Exception:
                pass


class WipeWorker(threading.Thread):
    """后台擦除线程，支持 diskpart 和直接 I/O 两种引擎"""

    def __init__(self, device_id, mode_name, progress_cb, log_cb, done_cb):
        super().__init__(daemon=True)
        self.device_id = device_id
        self.mode = WIPE_MODES[mode_name]
        self.progress_cb = progress_cb
        self.log_cb = log_cb
        self.done_cb = done_cb
        self._stop_event = threading.Event()
        self._diskpart_proc = None   # Popen 引用，用于强制终止

    def stop(self):
        """中止擦除：设标志 + 杀 diskpart 进程 + 停模拟进度"""
        self._stop_event.set()
        # 杀 diskpart 子进程
        if self._diskpart_proc and self._diskpart_proc.poll() is None:
            try:
                self._diskpart_proc.terminate()
            except Exception:
                pass

    def run(self):
        ok = False
        try:
            engine = self.mode.get("engine", "direct")

            if engine == "diskpart":
                # ── diskpart clean / clean all ──
                disk_index = get_disk_index(self.device_id)
                if disk_index is None:
                    self.log_cb("❌ 无法解析磁盘编号")
                    self.done_cb(False)
                    return
                cmd = self.mode.get("command", "clean all")
                is_fast = (cmd == "clean")
                self.log_cb(f"⚡ 使用 diskpart {cmd}（磁盘 {disk_index}）")
                if is_fast:
                    self.log_cb("  极速模式：清除分区表 + 首尾各128MB写零")
                    self.log_cb("  适合退役设备出手前快速处理")
                else:
                    self.log_cb("  安全模式：全盘写零，请耐心等待...")
                    self.log_cb("  期间不要拔出硬盘！")
                    # 只有安全写零模式才开模拟进度
                    self._start_fake_progress()
                ok = diskpart_clean_all(disk_index, self.log_cb,
                                        stop_event=self._stop_event,
                                        command=cmd)
                self._stop_fake = True

                # Quick 模式：diskpart clean 后再做首尾 128MB 写零
                if ok and is_fast:
                    if self._stop_event.is_set():
                        self.log_cb("⛔ 擦除已中断")
                        self.done_cb(False)
                        return
                    ok = self._wipe_head_tail()

            else:
                # ── 直接 I/O 写入 ──
                passes = self.mode["passes"]
                total_passes = len(passes)
                handle = self._open_disk()
                if handle is None:
                    self.log_cb("❌ 无法打开磁盘设备，请确认管理员权限")
                    self.done_cb(False)
                    return
                disk_size = self._get_disk_size(handle)
                ctypes.windll.kernel32.CloseHandle(handle)
                self.log_cb(f"📐 磁盘容量：{disk_size / (1024**3):.2f} GB")
                self.log_cb(f"🚀 直接 I/O 模式：块大小 {CHUNK_SIZE // (1024*1024)} MB，绕过系统缓冲")

                ok = True
                for pass_idx, pattern in enumerate(passes):
                    if self._stop_event.is_set():
                        self.log_cb("⛔ 擦除已中断")
                        self.done_cb(False)
                        return
                    pass_name = "随机数据" if pattern is None else f"0x{pattern[0]:02X}"
                    self.log_cb(f"▶ 第 {pass_idx+1}/{total_passes} 轮写入 [{pass_name}]")
                    ok = self._do_pass(pass_idx, total_passes, pattern, disk_size)
                    if not ok:
                        break

        except Exception as e:
            self.log_cb(f"❌ 擦除异常：{e}")
            ok = False

        self.done_cb(ok)

    # ── diskpart 模拟进度 ──
    def _start_fake_progress(self):
        self._stop_fake = False
        def _fake():
            start = time.time()
            while not self._stop_fake and not self._stop_event.is_set():
                elapsed = time.time() - start
                frac = min(elapsed / 10800, 0.95)
                self.progress_cb(frac, int(frac * 1e12), int(1e12), 0, 0)
                time.sleep(2)
        threading.Thread(target=_fake, daemon=True).start()

    # ── Quick 模式：首尾 128MB 写零 ──
    HEAD_TAIL_MB = 128
    _HEAD_TAIL_BYTES = HEAD_TAIL_MB * 1024 * 1024

    def _wipe_head_tail(self) -> bool:
        """Quick 模式独有：diskpart clean 后对磁盘首尾各 128MB 写零。
        覆盖 MBR/GPT 分区表头部 + GPT 备份尾部，且远快于全盘写零。"""
        handle = self._open_disk()
        if handle is None:
            self.log_cb("  ⚠️ 无法打开磁盘做首尾清零，跳过")
            return True  # 非致命，diskpart clean 已成功

        disk_size = self._get_disk_size(handle)
        self.log_cb(
            f"  🧹 首尾清零：磁盘 {disk_size / (1024**3):.1f} GB，"
            f"头尾各 {self.HEAD_TAIL_MB} MB 写零..."
        )

        # ── 头部 128MB ──
        head_len = min(self._HEAD_TAIL_BYTES, disk_size)
        ok = self._write_zero_range(handle, 0, head_len,
                                    f"头部 {head_len // (1024*1024)} MB")
        if not ok:
            ctypes.windll.kernel32.CloseHandle(handle)
            return False

        # ── 尾部 128MB ──
        if disk_size > self._HEAD_TAIL_BYTES:
            tail_start = disk_size - self._HEAD_TAIL_BYTES
            # 扇区对齐
            tail_start = (tail_start // SECTOR_SIZE) * SECTOR_SIZE
            tail_len = disk_size - tail_start
            ok = self._write_zero_range(handle, tail_start, tail_len,
                                        f"尾部 {tail_len // (1024*1024)} MB")
        else:
            self.log_cb("  ℹ️ 磁盘容量不足 256MB，尾部清零自动跳过")

        ctypes.windll.kernel32.CloseHandle(handle)
        if ok:
            self.log_cb("  ✅ 首尾清零完成，数据安全性大幅提升")
        return ok

    def _write_zero_range(self, handle, start_offset: int,
                          length: int, label: str = "") -> bool:
        """使用直接 I/O 对磁盘指定偏移区间写入全零。"""
        # Seek to start_offset
        high = ctypes.c_long(start_offset >> 32)
        low = ctypes.c_long(start_offset & 0xFFFFFFFF)
        ctypes.windll.kernel32.SetFilePointer(
            handle, low, ctypes.byref(high), 0  # FILE_BEGIN
        )

        align_len = (length // SECTOR_SIZE) * SECTOR_SIZE
        if align_len == 0:
            return True

        written = 0
        start_time = time.time()
        zero_buf = b'\x00' * CHUNK_SIZE

        while written < align_len:
            if self._stop_event.is_set():
                return False

            chunk = min(CHUNK_SIZE, align_len - written)
            buf = ctypes.create_string_buffer(zero_buf[:chunk], chunk)
            bytes_written = ctypes.c_ulong(0)
            success = ctypes.windll.kernel32.WriteFile(
                handle, buf, chunk, ctypes.byref(bytes_written), None
            )
            if not success or bytes_written.value == 0:
                err = ctypes.windll.kernel32.GetLastError()
                if align_len - written < CHUNK_SIZE:
                    break  # 末尾对齐余量
                self.log_cb(
                    f"  ⚠️ {label} 写零失败 "
                    f"offset={start_offset + written} err={err}"
                )
                return False
            written += bytes_written.value

        elapsed = time.time() - start_time
        speed = written / elapsed / (1024**2) if elapsed > 0 else 0
        self.log_cb(
            f"    {label}：{written / (1024**2):.0f} MB  "
            f"耗时 {elapsed:.1f}s  速度 {speed:.0f} MB/s"
        )
        return True

    def _open_disk(self):
        GENERIC_WRITE          = 0x40000000
        FILE_SHARE_READ        = 0x00000001
        FILE_SHARE_WRITE       = 0x00000002
        OPEN_EXISTING          = 3
        # 直接 I/O：绕过 Windows 缓冲层，速度大幅提升
        FILE_FLAG_NO_BUFFERING  = 0x20000000
        FILE_FLAG_WRITE_THROUGH = 0x80000000
        flags = FILE_FLAG_NO_BUFFERING | FILE_FLAG_WRITE_THROUGH
        handle = ctypes.windll.kernel32.CreateFileW(
            self.device_id,
            GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            flags,
            None
        )
        if handle == ctypes.c_void_p(-1).value:
            return None
        return handle

    def _get_disk_size(self, handle):
        IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
        buf = ctypes.create_string_buffer(256)
        bytes_ret = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.DeviceIoControl(
            handle, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX,
            None, 0, buf, ctypes.sizeof(buf),
            ctypes.byref(bytes_ret), None
        )
        if ok:
            # DiskSize is at offset 24 (after DISK_GEOMETRY structure of 24 bytes)
            size = struct.unpack_from("<Q", buf.raw, 24)[0]
            return size
        # Fallback: use SetFilePointer seek to end
        high = ctypes.c_long(0)
        low = ctypes.windll.kernel32.SetFilePointer(handle, 0, ctypes.byref(high), 2)
        return (high.value << 32) | low

    def _do_pass(self, pass_idx, total_passes, pattern, disk_size):
        handle = self._open_disk()
        if handle is None:
            self.log_cb("  ❌ 无法打开磁盘（直接 I/O 模式）")
            return False

        # FILE_FLAG_NO_BUFFERING 要求缓冲区大小和偏移均对齐到 SECTOR_SIZE
        # CHUNK_SIZE 已是 512 的整数倍，无需额外处理
        aligned_size = (disk_size // SECTOR_SIZE) * SECTOR_SIZE

        written = 0
        start_time = time.time()

        while written < aligned_size:
            if self._stop_event.is_set():
                ctypes.windll.kernel32.CloseHandle(handle)
                return False

            chunk = min(CHUNK_SIZE, aligned_size - written)
            # 确保 chunk 也是扇区对齐
            chunk = (chunk // SECTOR_SIZE) * SECTOR_SIZE
            if chunk == 0:
                break

            if pattern is None:
                data = os.urandom(chunk)
            else:
                repeats = (chunk // len(pattern)) + 1
                data = (pattern * repeats)[:chunk]

            # 使用 ctypes 分配对齐内存（VirtualAlloc 保证页对齐 ≥ 512）
            buf = ctypes.create_string_buffer(data, chunk)
            bytes_written = ctypes.c_ulong(0)
            ok = ctypes.windll.kernel32.WriteFile(
                handle, buf, chunk, ctypes.byref(bytes_written), None
            )
            if not ok or bytes_written.value == 0:
                err = ctypes.windll.kernel32.GetLastError()
                if disk_size - written < CHUNK_SIZE * 2:
                    break  # 末尾对齐余量，允许
                self.log_cb(f"  ⚠️ 写入失败 offset={written//(1024**3):.2f}GB err={err}")
                ctypes.windll.kernel32.CloseHandle(handle)
                return False
            written += bytes_written.value

            elapsed = time.time() - start_time
            speed = written / elapsed / (1024**2) if elapsed > 0 else 0
            remain = aligned_size - written
            eta = remain / (written / elapsed) if written > 0 and elapsed > 0 else 0
            overall = (pass_idx / total_passes) + (written / aligned_size / total_passes)
            self.progress_cb(overall, written, aligned_size, speed, eta)

        ctypes.windll.kernel32.CloseHandle(handle)
        self.log_cb(f"  ✅ 第 {pass_idx+1} 轮完成")
        return True


# ──────────────────────────────────────────────────────────
#  提示音
# ──────────────────────────────────────────────────────────

def play_success_sound():
    """播放完成提示音（三声上升音）"""
    def _play():
        for freq, dur in [(600, 150), (800, 150), (1000, 300)]:
            winsound.Beep(freq, dur)
            time.sleep(0.05)
    threading.Thread(target=_play, daemon=True).start()

def play_error_sound():
    def _play():
        for freq, dur in [(500, 200), (300, 400)]:
            winsound.Beep(freq, dur)
            time.sleep(0.1)
    threading.Thread(target=_play, daemon=True).start()

def play_detect_sound():
    def _play():
        winsound.Beep(800, 100)
    threading.Thread(target=_play, daemon=True).start()


# ──────────────────────────────────────────────────────────
#  GUI 主窗口
# ──────────────────────────────────────────────────────────

class DiskWipeApp(tk.Tk):

    POLL_INTERVAL_MS = 2000  # 检测间隔

    def __init__(self):
        super().__init__()
        self.title("🛡️  数据中心硬盘安全擦除工具  v1.15")
        self.geometry("820x700")
        self.resizable(True, True)
        self.configure(bg="#1e1e2e")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._known_disks: dict = {}   # device_id -> disk info
        self._wipe_worker: WipeWorker = None
        self._wiping = False
        self._wiped_count = 0
        self._current_disk = None
        self._system_disk_index: int | None = None  # 系统盘 PhysicalDrive 编号

        # ── 后台监控线程相关 ──
        self._mq: queue.Queue = queue.Queue()
        self._monitor_alive = True
        self._seen_timestamps: dict = {}   # device_id → 最后发现时间（防抖）
        self._stale_counters: dict = {}     # device_id → 连续未检测到次数
        self._debounce_sec = 3.0           # 同一设备 3 秒内不重复触发"新磁盘"
        self._stale_threshold = 2           # 连续 2 次未检测到才确认拔出

        self._build_ui()
        self._refresh_disk_list()
        self._start_hot_plug_monitor()

    # ──────── UI 构建 ────────

    def _build_ui(self):
        BG = "#1e1e2e"
        PANEL = "#2a2a3e"
        ACCENT = "#7aa2f7"
        GREEN = "#9ece6a"
        RED = "#f7768e"
        YELLOW = "#e0af68"
        FG = "#c0caf5"

        self._colors = dict(BG=BG, PANEL=PANEL, ACCENT=ACCENT,
                            GREEN=GREEN, RED=RED, YELLOW=YELLOW, FG=FG)

        # ── 顶部标题 ──
        title_frame = tk.Frame(self, bg=BG)
        title_frame.pack(fill=tk.X, padx=15, pady=(12, 0))
        tk.Label(title_frame, text="🛡️  数据中心硬盘安全擦除工具  v1.15",
                 font=("Microsoft YaHei UI", 16, "bold"),
                 bg=BG, fg=ACCENT).pack(side=tk.LEFT)
        self._counter_label = tk.Label(title_frame,
                                       text="已完成：0 块",
                                       font=("Microsoft YaHei UI", 11),
                                       bg=BG, fg=GREEN)
        self._counter_label.pack(side=tk.RIGHT)

        # ── 检测到的磁盘列表 ──
        disk_frame = tk.LabelFrame(self, text=" 📀 检测到的磁盘 ",
                                   bg=BG, fg=ACCENT,
                                   font=("Microsoft YaHei UI", 10, "bold"),
                                   bd=1, relief=tk.GROOVE)
        disk_frame.pack(fill=tk.X, padx=15, pady=8)

        cols = ("device", "model", "interface", "size", "serial", "status")
        col_w = (120, 200, 90, 100, 140, 100)
        self._disk_tree = ttk.Treeview(disk_frame, columns=cols,
                                       show="headings", height=5)
        heads = ("设备路径", "型号", "接口", "容量", "序列号", "状态")
        for col, head, w in zip(cols, heads, col_w):
            self._disk_tree.heading(col, text=head)
            self._disk_tree.column(col, width=w, anchor=tk.W)
        self._disk_tree.pack(fill=tk.X, padx=5, pady=5)
        self._disk_tree.bind("<<TreeviewSelect>>", self._on_disk_select)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview",
                        background=PANEL, foreground=FG,
                        rowheight=24, fieldbackground=PANEL,
                        font=("Consolas", 9))
        style.configure("Treeview.Heading",
                        background="#3a3a5e", foreground=ACCENT,
                        font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#3d59a1")])

        # ── 擦除模式选择 ──
        opt_frame = tk.Frame(self, bg=BG)
        opt_frame.pack(fill=tk.X, padx=15, pady=2)

        tk.Label(opt_frame, text="擦除模式：",
                 bg=BG, fg=FG,
                 font=("Microsoft YaHei UI", 10)).pack(side=tk.LEFT)

        self._mode_var = tk.StringVar(value=list(WIPE_MODES.keys())[0])
        for mode in WIPE_MODES:
            rb = tk.Radiobutton(opt_frame, text=mode, variable=self._mode_var,
                                value=mode, bg=BG, fg=FG,
                                selectcolor="#3d59a1",
                                activebackground=BG, activeforeground=ACCENT,
                                font=("Microsoft YaHei UI", 9))
            rb.pack(side=tk.LEFT, padx=8)

        # ── 自动擦除开关 ──
        auto_frame = tk.Frame(self, bg=BG)
        auto_frame.pack(fill=tk.X, padx=15, pady=2)
        self._auto_var = tk.BooleanVar(value=False)
        tk.Checkbutton(auto_frame,
                       text="🤖 自动擦除（插入即擦，无需手动确认）",
                       variable=self._auto_var,
                       bg=BG, fg=YELLOW, selectcolor="#3d59a1",
                       activebackground=BG, activeforeground=YELLOW,
                       font=("Microsoft YaHei UI", 10, "bold")).pack(side=tk.LEFT)

        # ── 操作按钮 ──
        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.pack(fill=tk.X, padx=15, pady=6)

        btn_cfg = dict(font=("Microsoft YaHei UI", 10, "bold"),
                       relief=tk.FLAT, bd=0, padx=16, pady=6, cursor="hand2")
        self._btn_wipe = tk.Button(btn_frame, text="▶  开始擦除",
                                   command=self._start_wipe,
                                   bg="#2d4f67", fg="white",
                                   activebackground="#3d6f8a",
                                   **btn_cfg)
        self._btn_wipe.pack(side=tk.LEFT, padx=4)

        self._btn_stop = tk.Button(btn_frame, text="⏹  中止擦除",
                                   command=self._stop_wipe,
                                   bg="#4a1942", fg="white",
                                   activebackground="#6a2962",
                                   state=tk.DISABLED, **btn_cfg)
        self._btn_stop.pack(side=tk.LEFT, padx=4)

        tk.Button(btn_frame, text="🔄  刷新磁盘",
                  command=self._async_refresh,
                  bg="#2a2a3e", fg=ACCENT,
                  activebackground="#3a3a5e",
                  **btn_cfg).pack(side=tk.LEFT, padx=4)

        self._btn_verify = tk.Button(btn_frame, text="🔍 验证擦除",
                                     command=self._verify_wipe,
                                     bg="#2a3a2a", fg=GREEN,
                                     activebackground="#3a5a3a",
                                     **btn_cfg)
        self._btn_verify.pack(side=tk.LEFT, padx=4)

        tk.Button(btn_frame, text="🔧 强制恢复",
                  command=self._force_recover_state,
                  bg="#5a2a2a", fg="#ffaaaa",
                  activebackground="#7a3a3a",
                  **btn_cfg).pack(side=tk.LEFT, padx=4)

        # ── 进度条 ──
        prog_frame = tk.Frame(self, bg=BG)
        prog_frame.pack(fill=tk.X, padx=15, pady=4)
        self._prog_var = tk.DoubleVar(value=0)
        self._progress = ttk.Progressbar(prog_frame, variable=self._prog_var,
                                         maximum=1.0, mode="determinate",
                                         length=500)
        style.configure("TProgressbar", troughcolor=PANEL,
                        background=ACCENT, thickness=18)
        self._progress.pack(fill=tk.X)

        self._prog_label = tk.Label(prog_frame,
                                    text="等待磁盘接入...",
                                    bg=BG, fg=FG,
                                    font=("Consolas", 9))
        self._prog_label.pack(anchor=tk.W, pady=2)

        # ── 状态面板 ──
        status_frame = tk.LabelFrame(self, text=" 📋 操作日志 ",
                                     bg=BG, fg=ACCENT,
                                     font=("Microsoft YaHei UI", 10, "bold"),
                                     bd=1, relief=tk.GROOVE)
        status_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=6)

        self._log = scrolledtext.ScrolledText(
            status_frame,
            bg="#11111b", fg="#a9b1d6",
            font=("Consolas", 9),
            wrap=tk.WORD, state=tk.DISABLED,
            insertbackground="white"
        )
        self._log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # ── 底部状态栏 ──
        bar = tk.Frame(self, bg="#111122", height=24)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        self._status_var = tk.StringVar(value="就绪 — 等待磁盘接入")
        tk.Label(bar, textvariable=self._status_var,
                 bg="#111122", fg="#565f89",
                 font=("Consolas", 8)).pack(side=tk.LEFT, padx=8)
        tk.Label(bar,
                 text="⚠️ 使用前请确认目标磁盘正确 | 擦除不可逆",
                 bg="#111122", fg=RED,
                 font=("Microsoft YaHei UI", 8, "bold")).pack(side=tk.RIGHT, padx=8)

        self._log_write("🚀 工具已启动，请通过 USB 线缆接入目标硬盘")
        self._log_write("💡 提示：首次使用请以管理员权限运行")
        self._log_write("⚡ Default: Quick clean (diskpart clean, 1 sec)")
        self._log_write("─" * 60)

    # ──────── 日志 ────────

    def _log_write(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self._log.config(state=tk.NORMAL)
        self._log.insert(tk.END, line)
        self._log.see(tk.END)
        self._log.config(state=tk.DISABLED)

    # ──────── 磁盘列表 ────────

    def _async_refresh(self):
        """后台刷新磁盘列表（不阻塞 UI）"""
        def _fetch():
            try:
                disks = get_all_physical_disks()
                self._mq.put(("refresh_result", disks, set(), set()))
            except Exception as e:
                self._mq.put(("refresh_error", str(e)[:200], set(), set()))
        threading.Thread(target=_fetch, daemon=True).start()

    def _refresh_disk_list(self, disks=None):
        # 缓存系统盘检测 — 只做一次，在后台线程
        if self._system_disk_index is None:
            def _detect():
                idx = get_system_disk_index()
                self._mq.put(("sys_disk_done", idx, set(), set()))
            threading.Thread(target=_detect, daemon=True).start()
            # 先用 disks 参数显示，不阻塞

        if disks is None:
            disks = get_all_physical_disks()
        # 清空
        for item in self._disk_tree.get_children():
            self._disk_tree.delete(item)
        for d in disks:
            did = d["device_id"]
            disk_idx = get_disk_index(did)

            # 判断是否为系统盘
            is_system = (self._system_disk_index is not None
                         and disk_idx is not None
                         and disk_idx == self._system_disk_index)

            if is_system:
                status = "❌ 系统盘-已锁定"
                tag = "system"
            elif did in self._known_disks and self._known_disks[did].get("done"):
                status = "擦除完成"
                tag = "done"
            else:
                status = "就绪"
                tag = "ready"

            self._disk_tree.insert("", tk.END, iid=did,
                                   values=(
                                       did,
                                       d["model"][:35],
                                       d["interface"],
                                       f"{d['size_gb']:.1f} GB",
                                       d["serial"][:20],
                                       status
                                   ), tags=(tag,))
        self._disk_tree.tag_configure("done",
                                      foreground=self._colors["GREEN"])
        self._disk_tree.tag_configure("ready",
                                      foreground=self._colors["FG"])
        self._disk_tree.tag_configure("system",
                                      foreground="#aa0000",
                                      background="#3d1a1a")

    def _on_disk_select(self, event):
        sel = self._disk_tree.selection()
        if sel:
            did = sel[0]
            # 优先用缓存，避免主线程调 wmic 卡死 UI
            d = self._known_disks.get(did)
            if d:
                self._current_disk = d
                self._log_write(
                    f"📌 已选择: {d['model']}  "
                    f"[{d['device_id']}]  "
                    f"{d['size_gb']:.1f} GB  "
                    f"接口:{d['interface']}"
                )

    # ──────── 热插拔监控 ────────

    def _start_hot_plug_monitor(self):
        """启动后台磁盘监控线程 — wmic 调用完全脱离主线程"""
        t = threading.Thread(target=self._run_monitor, daemon=True)
        t.start()
        # 前台每 200ms 检查队列，不阻塞 UI
        self._check_mq()

    def _run_monitor(self):
        """后台线程：轮询磁盘列表，结果推入队列"""
        known = {}   # 线程本地副本
        fail_count = 0
        while self._monitor_alive:
            try:
                disks = get_all_physical_disks()
                fail_count = 0  # 成功后重置
                current_ids = {d["device_id"] for d in disks}
                known_ids = set(known.keys())
                new_ids = current_ids - known_ids
                removed_ids = known_ids - current_ids

                # 推送结果到主线程
                if new_ids or removed_ids:
                    self._mq.put(("delta", disks, new_ids, removed_ids))
                elif not known:  # 首次扫描，发全量
                    self._mq.put(("snapshot", disks, set(), set()))

                # 更新线程本地缓存
                for d in disks:
                    if d["device_id"] in new_ids:
                        known[d["device_id"]] = d
                for rid in removed_ids:
                    known.pop(rid, None)

            except Exception as e:
                fail_count += 1
                if fail_count <= 3 or fail_count % 30 == 0:
                    self._mq.put(("monitor_error", str(e)[:200], set(), set()))
                # 连续多次失败后重置 known 缓存，避免状态错乱
                if fail_count > 10:
                    known.clear()
                    fail_count = 0
                # 失败时延长等待时间
                time.sleep(5.0)
                continue

            time.sleep(self.POLL_INTERVAL_MS / 1000.0)

    def _check_mq(self):
        """主线程：非阻塞消费后台监控队列"""
        try:
            while True:
                msg = self._mq.get_nowait()
                self._handle_monitor_msg(*msg)
        except queue.Empty:
            pass

        # ── 看门狗：如果磁盘列表为空且不在擦除中，后台强制刷新 ──
        if not self._wiping and not self._disk_tree.get_children():
            self._watchdog_count = getattr(self, '_watchdog_count', 0) + 1
            if self._watchdog_count >= 5 and not getattr(self, '_wd_running', False):
                self._watchdog_count = 0
                self._wd_running = True
                def _wd_fetch():
                    try:
                        fallback = get_all_physical_disks()
                        self._mq.put(("watchdog_result", fallback, set(), set()))
                    except Exception as e:
                        self._mq.put(("watchdog_error", str(e)[:200], set(), set()))
                    self._wd_running = False
                threading.Thread(target=_wd_fetch, daemon=True).start()
        else:
            self._watchdog_count = 0

        self.after(200, self._check_mq)

    def _handle_monitor_msg(self, mtype, disks, new_ids, removed_ids):
        """在主线程处理监控消息"""
        # ── 特殊消息：系统盘检测完成 ──
        if mtype == "sys_disk_done":
            idx = disks  # disks 参数承载 system_disk_index
            if idx is not None:
                self._system_disk_index = idx
                self._log_write(
                    f"⚠️  检测到系统盘：PhysicalDrive{idx}，已锁定保护"
                )
            return

        # ── 看门狗后台结果 ──
        if mtype == "watchdog_result":
            fallback = disks  # disks 参数承载查询结果列表
            if fallback:
                self._log_write("🔍 看门狗：检测到空列表，强制刷新...")
                self._refresh_disk_list(fallback)
                for d in fallback:
                    if d["device_id"] not in self._known_disks:
                        self._known_disks[d["device_id"]] = d
            return

        if mtype == "watchdog_error":
            self._log_write(f"⚠️ 看门狗 WMI 查询异常：{disks}")
            return

        # ── 手动刷新结果 ──
        if mtype == "refresh_result":
            self._refresh_disk_list(disks)
            for d in disks:
                self._known_disks[d["device_id"]] = d
            return

        if mtype == "refresh_error":
            self._log_write(f"⚠️ 刷新失败：{disks}")
            return

        # ── 特殊消息：monitor 线程报错 ──
        if mtype == "monitor_error":
            self._log_write(f"⚠️ monitor 线程 WMI 查询异常：{disks}")
            return

        if self._wiping:
            return   # 擦除中不响应热插拔

        now = time.time()

        # ── 处理新磁盘（带去重防抖） ──
        for d in disks:
            did = d["device_id"]
            if did not in new_ids:
                continue
            last = self._seen_timestamps.get(did, 0)
            if now - last < self._debounce_sec:
                continue  # 防抖：同一设备 3 秒内不重复触发
            self._seen_timestamps[did] = now
            self._stale_counters.pop(did, None)  # 重置 stale 计数
            self._known_disks[did] = d
            self._on_new_disk(d)

        # ── 处理拔出（去抖动：连续 N 次未见才确认） ──
        for rid in removed_ids:
            cnt = self._stale_counters.get(rid, 0) + 1
            self._stale_counters[rid] = cnt
            if cnt >= self._stale_threshold:
                if rid in self._known_disks:
                    info = self._known_disks[rid]
                    self._log_write(f"📤 磁盘已拔出：{info.get('model', rid)}")
                self._known_disks.pop(rid, None)
                self._stale_counters.pop(rid, None)
                self._seen_timestamps.pop(rid, None)

        # ── 刷新 UI（仅当有变化时） ──
        if new_ids or removed_ids or not self._disk_tree.get_children():
            self._refresh_disk_list(disks)

    def _on_new_disk(self, disk: dict):
        play_detect_sound()

        # 检查是否为系统盘（理论上热插拔不会，但做双重保险）
        disk_idx = get_disk_index(disk["device_id"])
        is_system = (self._system_disk_index is not None
                     and disk_idx is not None
                     and disk_idx == self._system_disk_index)

        self._log_write("─" * 60)
        if is_system:
            self._log_write(
                f"⚠️ 检测到系统盘！已自动锁定，不可擦除！ "
                f"型号: {disk['model']} "
                f"容量: {disk['size_gb']:.1f} GB"
            )
            self._current_disk = None
            return

        self._log_write(
            f"🆕 检测到新磁盘！ "
            f"型号: {disk['model']} "
            f"容量: {disk['size_gb']:.1f} GB "
            f"接口: {disk['interface']} "
            f"序列号: {disk['serial']}"
        )
        self._current_disk = disk
        # 选中该磁盘
        try:
            self._disk_tree.selection_set(disk["device_id"])
        except Exception:
            pass

        if self._auto_var.get():
            self._log_write("🤖 自动模式：即将开始擦除...")
            self.after(1500, self._start_wipe)

    # ──────── 擦除流程 ────────

    def _start_wipe(self):
        if self._wiping:
            return
        if self._current_disk is None:
            # 尝试从选择获取
            sel = self._disk_tree.selection()
            if not sel:
                messagebox.showwarning("提示", "请先选择要擦除的磁盘")
                return
            did = sel[0]
            self._current_disk = self._known_disks.get(did)
            if not self._current_disk:
                messagebox.showerror("错误", "无法获取磁盘信息，请刷新后重试")
                return

        disk = self._current_disk

        # ══════════════════════════════════════════════════════
        # System disk protection
        disk_idx = get_disk_index(disk["device_id"])
        if (self._system_disk_index is not None
                and disk_idx is not None
                and disk_idx == self._system_disk_index):
            messagebox.showerror(
                "💀 致命错误",
                f"检测到目标磁盘是系统盘！\n\n"
                f"  设备：{disk['device_id']}\n"
                f"  型号：{disk['model']}\n"
                f"  容量：{disk['size_gb']:.1f} GB\n\n"
                f"⛔ 系统盘已被锁定，不可擦除！\n"
                f"   擦除系统盘会导致操作系统彻底损坏。\n"
                f"   请拔掉 USB 线缆后，选择外接硬盘操作。"
            )
            self._log_write("⛔ 阻止擦除：目标磁盘是操作系统所在的系统盘！")
            return
        # ══════════════════════════════════════════════════════

        mode = self._mode_var.get()

        # Confirmation dialog
        if not self._auto_var.get():
            confirm = messagebox.askyesno(
                "⚠️  危险操作确认",
                f"即将不可逆地擦除以下磁盘：\n\n"
                f"  设备路径：{disk['device_id']}\n"
                f"  型号：    {disk['model']}\n"
                f"  容量：    {disk['size_gb']:.1f} GB\n"
                f"  序列号：  {disk['serial']}\n\n"
                f"  擦除模式：{mode}\n\n"
                f"⚠️  此操作将彻底销毁所有数据，不可恢复！\n"
                f"确认继续？",
                icon="warning"
            )
            if not confirm:
                self._log_write("❌ 用户取消擦除操作")
                return

        self._wiping = True
        self._btn_wipe.config(state=tk.DISABLED)
        self._btn_stop.config(state=tk.NORMAL)
        self._prog_var.set(0)
        self._status_var.set(f"正在擦除：{disk['model']}")

        self._log_write(f"🔥 开始擦除 [{disk['device_id']}]  模式: {mode}")
        start_ts = datetime.now()

        self._wipe_worker = WipeWorker(
            device_id=disk["device_id"],
            mode_name=mode,
            progress_cb=self._on_progress,
            log_cb=self._on_log,
            done_cb=lambda ok: self._on_wipe_done(ok, disk, start_ts),
        )
        self._wipe_worker.start()

    def _stop_wipe(self):
        if self._wipe_worker:
            self._wipe_worker.stop()
            self._log_write("⏹ 正在中止擦除（含 diskpart 进程终止）...")
        self._btn_stop.config(state=tk.DISABLED)
        # 兜底：5 秒后如果 worker 还没回调 done_cb，强制恢复
        self._force_recover_id = self.after(5000, self._force_recover_state)

    def _force_recover_state(self):
        """兜底恢复：如果 worker 线程挂死，强制解锁界面"""
        if self._wiping:
            self._log_write("⚠️ 强制恢复状态（worker 可能已挂死）")
            self._wiping = False
            self._wipe_worker = None
            self._btn_wipe.config(state=tk.NORMAL)
            self._btn_stop.config(state=tk.DISABLED)
            self._prog_var.set(0)
            self._prog_label.config(text="已强制恢复 — 请刷新磁盘列表")
            self._status_var.set("就绪 — 等待磁盘接入")
            self._refresh_disk_list()

    def _on_progress(self, overall: float, written: int,
                     total: int, speed: float, eta: float):
        """由工作线程回调，需切换到主线程"""
        self.after(0, self._update_progress, overall, written, total, speed, eta)

    def _update_progress(self, overall, written, total, speed, eta):
        self._prog_var.set(overall)
        pct = overall * 100
        w_gb = written / (1024 ** 3)
        t_gb = total / (1024 ** 3)
        eta_s = int(eta)
        self._prog_label.config(
            text=f"{pct:.1f}%  |  {w_gb:.2f} / {t_gb:.2f} GB  |  "
                 f"速度: {speed:.1f} MB/s  |  剩余: {eta_s//60}m {eta_s%60}s"
        )

    def _on_log(self, msg: str):
        self.after(0, self._log_write, msg)

    def _on_wipe_done(self, success: bool, disk: dict, start_ts):
        self.after(0, self._finish_wipe, success, disk, start_ts)

    def _finish_wipe(self, success: bool, disk: dict, start_ts):
        # Guard against duplicate calls (can happen with fast diskpart clean)
        if getattr(self, '_finish_guard', False):
            return
        self._finish_guard = True
        try:
            self._finish_wipe_inner(success, disk, start_ts)
        finally:
            self._finish_guard = False

    def _finish_wipe_inner(self, success: bool, disk: dict, start_ts):
        # 取消兜底恢复定时器（正常完成不需要）
        if hasattr(self, '_force_recover_id'):
            self.after_cancel(self._force_recover_id)
        elapsed = (datetime.now() - start_ts).total_seconds()
        self._wiping = False
        self._wipe_worker = None
        self._btn_wipe.config(state=tk.NORMAL)
        self._btn_stop.config(state=tk.DISABLED)

        if success:
            self._wiped_count += 1
            self._counter_label.config(text=f"已完成：{self._wiped_count} 块")
            self._known_disks[disk["device_id"]]["done"] = True
            self._prog_var.set(1.0)
            self._prog_label.config(
                text=f"✅ 擦除完成！耗时 {int(elapsed)//60}m {int(elapsed)%60}s"
            )
            self._status_var.set("擦除完成 — 可拔出磁盘并接入下一块")
            self._log_write("=" * 60)
            self._log_write(
                f"✅ 擦除成功！耗时 {int(elapsed)//60}m {int(elapsed)%60}s  "
                f"[{disk['model']}]"
            )
            self._log_write("🎯 磁盘数据已彻底销毁，可安全处置")
            self._log_write("📤 请拔出当前磁盘，接入下一块...")
            self._log_write("=" * 60)

            # 清除该设备的所有缓存状态，确保 monitor 能正确识别下一块盘
            did = disk["device_id"]
            self._seen_timestamps.pop(did, None)
            self._stale_counters.pop(did, None)
            # 刷新列表（使用已知磁盘数据，不做同步 WMI 调用）
            current = list(self._known_disks.values())
            self._refresh_disk_list(current)
            play_success_sound()
            # Non-blocking toast notification (auto-dismisses, won't block workflow)
            self._show_toast(
                f"磁盘 [{disk['model']}] 已擦除！\n"
                f"耗时：{int(elapsed)//60}m {int(elapsed)%60}s\n"
                f"请拔出当前磁盘，接入下一块"
            )
        else:
            play_error_sound()
            self._status_var.set("❌ 擦除失败或已中止")
            self._log_write("❌ 擦除失败或中止，请检查磁盘连接后重试")

    # ──────── Toast 通知（非阻塞弹窗） ────────

    def _show_toast(self, text: str, duration_ms: int = 6000):
        """显示非阻塞 Toast 通知，自动消失，无需点击确定"""
        top = tk.Toplevel(self)
        top.title("擦除完成")
        top.overrideredirect(True)  # No title bar
        top.attributes("-topmost", True)
        # Center on parent
        top.update_idletasks()
        pw, ph = self.winfo_width(), self.winfo_height()
        px, py = self.winfo_rootx(), self.winfo_rooty()
        tw, th = 380, 130
        top.geometry(f"{tw}x{th}+{px + (pw-tw)//2}+{py + (ph-th)//2}")

        # Dark theme matching main window
        bg = "#1a1a2e"
        fg = "#e0e0e0"
        accent = "#00d4aa"
        top.configure(bg=bg)

        # Inner frame with border
        inner = tk.Frame(top, bg=bg, highlightbackground=accent,
                         highlightthickness=2, bd=0)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        title_label = tk.Label(inner, text="✅  擦除完成", font=("Segoe UI", 13, "bold"),
                               fg=accent, bg=bg)
        title_label.pack(pady=12)
        body_label = tk.Label(inner, text=text, font=("Segoe UI", 10),
                              fg=fg, bg=bg, justify=tk.CENTER)
        body_label.pack(pady=8)

        # Click-to-dismiss + auto-dismiss
        def dismiss():
            try:
                top.destroy()
            except Exception:
                pass

        def on_click(e):
            dismiss()

        for w in (top, inner, title_label, body_label):
            w.bind("<Button-1>", on_click)

        top.after(duration_ms, dismiss)

    # ──────── 验证功能 ────────

    def _verify_wipe(self):
        sel = self._disk_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择要验证的磁盘")
            return
        did = sel[0]
        self._log_write(f"🔍 开始抽样验证 [{did}]...")

        def do_verify():
            try:
                GENERIC_READ = 0x80000000
                FILE_SHARE_READ = 0x00000001
                FILE_SHARE_WRITE = 0x00000002
                OPEN_EXISTING = 3
                handle = ctypes.windll.kernel32.CreateFileW(
                    did, GENERIC_READ,
                    FILE_SHARE_READ | FILE_SHARE_WRITE,
                    None, OPEN_EXISTING, 0, None
                )
                if handle == ctypes.c_void_p(-1).value:
                    self.after(0, self._log_write, "  ❌ 无法打开磁盘（需管理员权限）")
                    return
                # 抽样 10 个位置
                IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
                buf = ctypes.create_string_buffer(256)
                br = ctypes.c_ulong(0)
                ctypes.windll.kernel32.DeviceIoControl(
                    handle, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX,
                    None, 0, buf, ctypes.sizeof(buf),
                    ctypes.byref(br), None
                )
                disk_size = struct.unpack_from("<Q", buf.raw, 24)[0]
                sample_size = 512 * 1024  # 512KB per sample
                positions = [
                    0,
                    disk_size // 8,
                    disk_size // 4,
                    disk_size * 3 // 8,
                    disk_size // 2,
                    disk_size * 5 // 8,
                    disk_size * 3 // 4,
                    disk_size * 7 // 8,
                ]
                all_zero = True
                for pos in positions:
                    # 对齐到 512 字节
                    pos = (pos // 512) * 512
                    if pos + sample_size > disk_size:
                        continue
                    high = ctypes.c_long(pos >> 32)
                    low = ctypes.windll.kernel32.SetFilePointer(
                        handle, pos & 0xFFFFFFFF, ctypes.byref(high), 0
                    )
                    rbuf = ctypes.create_string_buffer(sample_size)
                    bread = ctypes.c_ulong(0)
                    ctypes.windll.kernel32.ReadFile(
                        handle, rbuf, sample_size, ctypes.byref(bread), None
                    )
                    data = rbuf.raw[:bread.value]
                    if any(b != 0 for b in data):
                        all_zero = False
                        self.after(0, self._log_write,
                                   f"  ⚠️  偏移 {pos//(1024**3):.2f}GB 处发现非零数据")
                ctypes.windll.kernel32.CloseHandle(handle)
                if all_zero:
                    self.after(0, self._log_write,
                               "  ✅ 抽样验证通过：已擦除区域均为零值")
                else:
                    self.after(0, self._log_write,
                               "  ❌ 验证失败：发现残留数据，建议重新擦除")
            except Exception as e:
                self.after(0, self._log_write, f"  ❌ 验证出错：{e}")

        threading.Thread(target=do_verify, daemon=True).start()

    # ──────── 关闭处理 ────────

    def _on_close(self):
        self._monitor_alive = False  # 停止后台监控线程
        if self._wiping:
            if not messagebox.askyesno("确认退出",
                                       "擦除正在进行中，确定要退出吗？"):
                self._monitor_alive = True
                return
            if self._wipe_worker:
                self._wipe_worker.stop()
        self.destroy()


# ──────────────────────────────────────────────────────────
#  入口
# ──────────────────────────────────────────────────────────

def safe_console_write(text: str):
    """Write Unicode text to Windows console safely (avoids garbled chars on GBK terminals)."""
    try:
        # Method 1: WriteConsoleW — writes Unicode directly, bypassing code page
        handle = ctypes.windll.kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if handle and handle != ctypes.c_void_p(-1).value:
            result = ctypes.windll.kernel32.WriteConsoleW(
                handle, ctypes.c_wchar_p(text), len(text),
                ctypes.byref(ctypes.c_ulong()), None
            )
            if result:
                return
    except Exception:
        pass
    # Method 2: Fallback — try regular print with error replacement
    try:
        print(text.encode(sys.stdout.encoding or "utf-8",
                          errors="replace").decode(sys.stdout.encoding or "utf-8",
                                                   errors="replace"))
    except Exception:
        # Last resort: just try print
        try:
            print(text)
        except Exception:
            pass


if __name__ == "__main__":
    if not is_admin():
        safe_console_write("正在请求管理员权限...\n")
        run_as_admin()
        sys.exit(0)
    try:
        app = DiskWipeApp()
        app.mainloop()
    except Exception as e:
        # 无控制台环境下弹出错误框，便于排查
        import traceback
        ctypes.windll.user32.MessageBoxW(
            0,
            f"程序启动失败：\n{traceback.format_exc()}",
            "硬盘擦除工具 — 启动错误",
            0x10  # MB_ICONERROR
        )
