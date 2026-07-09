# 数据中心硬盘安全擦除工具 v1.15

## 适用场景

数据中心退役服务器硬盘批量安全销毁，支持 USB 转 SATA / USB 转 NVMe 接口。

## 功能特性

- **热插拔自动检测** — 拔插硬盘自动识别，插上即操作，拔下自动移除
- **系统盘保护** — 自动检测 Windows 系统盘，防止误擦
- **三种擦除模式**
  - ⚡ Quick — 清理分区表 + 首尾 128MB 写零（极快，1-2 秒）
  - diskpart clean all — 全盘写零（彻底，时间较长）
  - 快速清零 — 直接 I/O 单次写零（64MB 块大小，速度最优）
- **进度实时反馈** — 进度条 + 详细日志
- **擦除后验证** — 读回确认数据已归零
- **声音提示** — 完成 / 异常都有语音提醒
- **暗色 GUI 界面** — tkinter 构建，简洁直观

## 系统要求

- Windows 10 / 11（64 位）
- 管理员权限
- Python 3.13+（源码运行）/ 直接使用打包好的 exe

## 快速使用

### 方式一：直接运行 exe（推荐）

1. 从 [Releases](https://github.com/guganly/disk-wipe-tool/releases) 下载 `disk_wipe.exe`
2. 双击运行（自动触发 UAC 提权）
3. 通过 USB 线缆接入需要擦除的硬盘
4. 程序自动检测 → 选择擦除模式 → 确认执行
5. 完成提示音响起后拔下硬盘，插入下一块

### 方式二：源码运行

```bash
# 安装依赖
install_env.bat

# 启动程序
启动擦除工具.bat
```

或手动：

```bash
pip install psutil pywin32
python disk_wipe.py
```

## 打包为 exe

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --uac-admin --name disk_wipe disk_wipe.py
```

- `--windowed` — 无控制台窗口（GUI 应用）
- `--uac-admin` — 嵌入管理员权限清单，双击自动提权

## 注意事项

- **必须以管理员权限运行**，否则无法操作物理磁盘
- 操作前确认目标磁盘无误，数据擦除后**不可恢复**
- 系统盘会自动标记为"受保护"并拒绝操作

## 技术栈

- Python 3.13 + tkinter
- Windows API（ctypes / wmic / diskpart）
- 直接 I/O 写零（绕过文件系统缓冲）
- PyInstaller 打包
