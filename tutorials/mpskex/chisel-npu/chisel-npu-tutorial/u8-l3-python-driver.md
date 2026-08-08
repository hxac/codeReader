# Python 用户态驱动 chisel_npu_py

## 1. 本讲目标

本讲讲解 chisel-npu 的 Python 用户态驱动 `chisel_npu_py`。它是紧贴上一讲（u8-l2）的 C 工具之上、面向上层算法/量化脚本的高层入口。学完本讲，你应当能够：

1. 说出 `ChiselNPU.mmalu(A, B, ACCUM)` 内部的 **stage → kick → wait → collect** 四步调用序列，并解释每一步落到哪个底层模块。
2. 理解 `XDMADevice` 如何**按名字**（`"A"/"B"/"ACCUM"/"OUT"`）寻址 operand，而不是按 DDR 地址。
3. 掌握 `CtrlLite` 的 `start/done/busy` 三位控制协议以及 `wait_done` 的轮询逻辑。
4. 理解为什么所有文件描述符、DDR 地址、寄存器偏移都被关进一个 pybind11 C++ 模块，而 **Python 侧永远看不到任何一个地址**。
5. 会用纯 Python 的 `FakeNative` 在**没有 FPGA 硬件**时把整套驱动逻辑跑起来、写单测。

## 2. 前置知识

本讲假设你已读过 u8-l2（Linux XDMA 内核驱动与 C 工具）。我们快速回顾其中与本讲直接相关的两个结论：

- **设备节点**：`xdma.ko` 加载后会在 `/dev/xdma0_*` 下暴露三类字符设备——`_h2c_*`（主机写出口，文件偏移 = AXI 地址）、`_c2h_*`（主机读入口）、`_bypass`（AXI-Lite 控制寄存器窗口，可 `mmap`）。
- **ctrl_lite 协议**：`npu_subsys` 暴露一个 3 比特控制字——`start`（写，边沿触发，自清零）、`done`（读，锁存）、`busy`（读，电平）。主机写完 A/B/ACCUM 到 DDR3，再向 `start` 写 1，`npu_dma_master` 就自主完成「读 A/B/ACCUM → 喂 MMALU → 等 `io_clct` → 写 OUT」。

本讲要回答的问题是：C 工具（`reg_rw` / `dma_to_device`）是 Xilinx 厂商通用的，每个调用都要手写裸 AXI 地址；当上层用 Python（配合 numpy）写量化脚本时，**如何把这些地址藏起来、只留一个类型安全的 numpy 优先 API**。这正是 `chisel_npu_py` 的设计目标。

下面三个术语贯穿全讲：

- **pybind11**：一个让 C++ 类/函数直接暴露成 Python 模块的库，本讲里它把 C++ 类 `NativeXDMA` 编译成 `chisel_npu_py._native`。
- **operand（操作数）**：MMALU 一次计算需要的输入/输出数据块，按名字 `A/B/ACCUM/OUT` 区分，地址固定在 DDR3。
- **staging table（暂存表）**：把名字映射到 `(DDR 地址, 字节数)` 的表，是整个驱动的「地址真相之源」。

## 3. 本讲源码地图

| 文件 | 作用 |
|:-----|:-----|
| [drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp) | pybind11 C++ 边界。独占 fd / DDR 地址 / 寄存器偏移 / DMA 传输，是「地址的唯一权威」。 |
| [drivers/chisel_npu_py/src/chisel_npu_py/backend.py](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/backend.py) | `XDMADevice`：native 模块的类型化 Python 包装，按名字搬运 buffer。 |
| [drivers/chisel_npu_py/src/chisel_npu_py/ctrl.py](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/ctrl.py) | `CtrlLite`：start/done/busy 位协议与 `wait_done` 轮询。 |
| [drivers/chisel_npu_py/src/chisel_npu_py/npu.py](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/npu.py) | `ChiselNPU`：高层门面，编排 stage→kick→wait→collect 整条 MMALU 周期。 |
| [drivers/chisel_npu_py/src/chisel_npu_py/consts.py](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/consts.py) | 全局常量：`K`、operand 名字集合、三个控制位的位置。**故意不含任何地址**。 |
| [drivers/chisel_npu_py/tests/fake_native.py](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/tests/fake_native.py) | `FakeNative`：纯 Python 的 native 替身，无硬件单测的关键。 |
| [drivers/chisel_npu_py/tests/test_loopback.py](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/tests/test_loopback.py) | 硬件回环测试：逐 operand 字节级 round-trip。 |

整体分层（自下而上）：

```
numpy buffer (int8/int32)
      │
ChiselNPU          ← 编排四步（npu.py）
 ├── CtrlLite      ← 位协议 / wait_done（ctrl.py）
 └── XDMADevice    ← 按名字搬运（backend.py）
       │
   _native.so ── pybind11 边界（native.cpp）：fd / 地址 / 暂存表 / 对齐 / 传输 全部在此
       │
   /dev/xdma0_*（xdma.ko 内核驱动，见 u8-l2）
```

## 4. 核心概念与源码讲解

本讲按「先讲为什么有这条边界，再从底向上逐层讲三个 Python 类」的顺序展开，对应四个最小模块：**pybind11 边界 → XDMADevice → CtrlLite → ChiselNPU**。

---

### 4.1 pybind11 安全边界：把所有「危险物」关进 C++

#### 4.1.1 概念说明

`chisel_npu_py` 最核心的设计决策不是某个算法，而是一条**纪律**：把所有容易出错的东西（文件描述符、裸 DDR 地址、寄存器偏移、DMA 传输）全部关进一个 C++ 模块，给 Python 只留下「按名字搬运 buffer + 一个控制字」的安全接口。

这条纪律带来的好处是结构性的：

- **不可误寻址**：Python 拿不到任何地址，自然无法写错地址、越界或踩到别人的 DDR 区域。错误的 operand 名或错误字节数会被 C++ 当场拒绝。
- **类型安全**：Python 侧只处理 numpy 数组，`int8[K]`、`int32[K]` 等，不用关心 AXI 地址、对齐、4 字节倍数这些硬件细节。
- **可测试**：C++ 模块替换成纯 Python 的 `FakeNative`（接口相同），整套编排逻辑就能在没有 FPGA 的开发机上单测。

[docs/implementations/PythonDriver.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/PythonDriver.md) 的 Overview 一节用一句话总结了这条纪律：**「No DDR address or register offset ever appears on the Python side.」**

#### 4.1.2 核心流程

一次 `write_staged("A", buf)` 跨越边界的过程：

1. Python 传入一个**名字** `"A"` 和一个 **buffer**（numpy/bytes/bytearray/memoryview）。
2. C++ 用名字查 `kStaging` 表，得到 `(DDR 地址, 期望字节数)`。
3. C++ 把 buffer 转成连续内存，校验字节数与期望完全相等。
4. C++ 用 `pwrite(h2c_fd, 数据, 长度, DDR 地址)` 触发 SGDMA——文件偏移即 AXI 地址，这正是 u8-l2 里厂商 `dma_to_device` 的语义。
5. C++ 返回写入字节数给 Python（一个 `int`，**不含地址**）。

读回（`read_staged`）走对称的 `pread`（c2h 通道）。控制寄存器走 `mmap` 后的指针解引用，而不是 DMA。整条路径里，地址只在 C++ 的局部变量里短暂存在，从不跨过 pybind11 返回给 Python。

#### 4.1.3 源码精读

**暂存表是地址的唯一真相之源。** 它是一个 `std::map<名字, {地址, 字节数}>`：

[drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp:44-49](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp#L44-L49) —— 注释明确写「AUTHORITATIVE（权威）」，并指出 Python 的 `consts.py` 只是镜像、仅供自省/测试用。这四条地址 `0x4000_0000 / 0100 / 0200 / 0400` 与 u8-l2、u8-l1 完全一致。

`validate_transfer` 把硬件约束变成提前的、可读的校验，避免把非法传输交给内核驱动：

[drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp:55-65](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp#L55-L65) —— 校验四件事：长度非零、地址 4 字节对齐、长度是 4 的倍数、地址落在 `0x0000_0000..0xFFFF_FFFF` 的 4 GB DDR 窗口内。

实际的 DMA 由两个循环完成，处理短读/短写与 `EINTR` 中断重试：

[drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp:67-81](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp#L67-L81) —— `do_pwrite` 用 `::pwrite(fd, ...)`，文件偏移参数就是 AXI 地址。`n == 0` 被当作错误（设备不应返回 0 字节），`errno == EINTR` 时重试。

`NativeXDMA` 构造函数在创建对象时就打开三个设备节点并 `mmap` 控制寄存器页，把这些 fd 与映射地址留在对象内部：

[drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp:126-140](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp#L126-L140) —— 注意 `_bypass` 用 `O_RDWR | O_SYNC` 打开后 `mmap` 一页（4096 字节），控制字就住在这一页里；析构函数（142-147 行）负责 `munmap` 与 `close`，且拷贝/赋值被 `delete`（149-150 行）禁用，保证一个对象独占一组 fd。

按名字搬运的两个方法是边界纪律的最佳示例——名字查表、大小校验、然后 `pwrite`/`pread`：

[drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp:155-164](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp#L155-L164) —— `write_staged` 先 `staged_slot(operand)` 查表（未知名字抛 `value_error`），再 `to_contiguous` 转 C 连续数组，再校验 `nbytes == slot.second`（字节数必须精确相等），最后 `do_pwrite`。返回值是字节数，不含地址。

`read_staged`（166-175 行）对称：`to_contiguous(..., true)` 的 `for_output=true` 表示**禁止拷贝**——读回若拷贝会静默丢数据，所以非连续或只读 buffer 直接报错。

最后，pybind11 用 `PYBIND11_MODULE` 把 C++ 类注册成 Python 类：

[drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp:236-257](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp#L236-L257) —— 注册了 `NativeXDMA` 类及其构造函数（`prefix/h2c_ch/c2h_ch` 三个带默认值的参数）与五个方法（`write_staged/read_staged/operand_size/ctrl_read/ctrl_write`）。注意这五个方法**全部不含地址参数**——这是边界纪律在 API 表面的直接体现。

#### 4.1.4 代码实践

**实践目标**：在 FPGA 主机上验证边界两端都活着，且 Python 真的不碰地址。

1. 在仓库根目录 `source .env.sh` 后执行 `make py-deploy`，它会在 FPGA 主机上 `pip install .`（在目标 venv 里编译 `_native`）、装 udev 规则、再跑自检。
2. 自检入口是 [drivers/chisel_npu_py/src/chisel_npu_py/__main__.py:19-73](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/__main__.py#L19-L73) 的 `selftest()`：依次验证「native 可导入 → 设备节点可开 → ctrl_lite 可读 → OUT 的 staged round-trip 字节级相等」。
3. 命令：`~/chisel_npu_py/.venv/bin/python -m chisel_npu_py selftest`。
4. **需要观察**：打印的 `operand sizes (B)` 字典是 `{'A': 32, 'B': 32, 'ACCUM': 128, 'OUT': 128}`——注意这里**只出现字节数，没有任何地址**；最后一行应为 `PASS`。
5. **预期结果**：退出码 0。若 `ctrl_lite read` 返回 `0xFFFFFFFF`（53-55 行），说明 PCIe/BAR 有问题，退出码 4。

> 待本地验证：本实践需要 FPGA 主机与 `xdma.ko`。若无硬件，可跳到 4.4 用 `FakeNative` 在开发机上跑等价验证。

#### 4.1.5 小练习与答案

**练习 1**：`write_staged("ACCUM", buf)` 传入一个 31 字节的 buffer 会发生什么？为什么这是好事？

**参考答案**：C++ 在 [native.cpp:159-162](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp#L159-L162) 检查 `nbytes != slot.second`（ACCUM 的 `slot.second` 是 128），抛 `py::value_error("operand 'ACCUM' must be exactly 128 bytes, got 31")`。好处是：错误在边界处被拦下，绝不会产生一次「地址对、长度错」或「长度对、地址错」的危险 DMA，Python 也无从手滑写出越界地址。

**练习 2**：为什么 `read_staged` 对输出 buffer 禁止拷贝，而 `write_staged` 对输入 buffer 允许拷贝？

**参考答案**：见 [native.cpp:104-120](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp#L104-L120) 的 `to_contiguous` 注释。输入拷贝只是把数据搬一份再 DMA 出去，不影响正确性；输出若拷贝，`pread` 写进的是拷贝的临时内存，函数返回后临时内存被丢弃，调用者拿到的原 buffer 仍是旧数据——**静默丢数据**。所以输出必须 C 连续且可写，拒绝拷贝。

---

### 4.2 XDMADevice：按名字寻址的搬运工

#### 4.2.1 概念说明

`XDMADevice` 是 native 模块的**类型化 Python 包装**。它本身不做任何「危险」操作，只把 native 的方法包成清晰的 Python API：

- `write_staged("A", buf)` / `read_staged("OUT", out)` —— 按 operand 名字搬运；
- `operand_size("ACCUM")` —— 查字节数，用于分配数组；
- `ctrl_read()` / `ctrl_write(value)` —— 读写那个唯一的控制字。

它的设计有两个关键点：**延迟导入 native**（开发机上没编译 `_native` 也能 import 这个模块，只是不能实例化），以及**可注入 native**（测试时塞 `FakeNative` 进去）。

#### 4.2.2 核心流程

`XDMADevice` 的生命周期：

1. `import` 本模块时，`try` 导入 `chisel_npu_py._native`；失败则把异常存进 `_NATIVE_IMPORT_ERROR`，**不立即报错**。
2. 实例化 `XDMADevice()` 时，若没传 `native=`，则调 `_require_native()`——此时若 native 未编译才抛清晰的 `XDMAError`，提示「在 FPGA 主机上 `pip install .`」。
3. 若传了 `native=`（测试用），直接用注入的对象，完全不碰真实设备节点。
4. 之后所有方法都是 native 方法的薄包装，并用 `int(...)` 把返回值钉成 Python `int`。

#### 4.2.3 源码精读

延迟导入与友好报错：

[drivers/chisel_npu_py/src/chisel_npu_py/backend.py:24-44](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/backend.py#L24-L44) —— `try/except ImportError` 把编译缺失变成「可恢复的设计」：模块仍可被 import（用于离线单测里塞 `FakeNative`），只有真正要开设备时才报错，且报错信息直接给出修复命令。

可注入 native 的构造函数（测试的关键开关）：

[drivers/chisel_npu_py/src/chisel_npu_py/backend.py:54-64](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/backend.py#L54-L64) —— `native=None` 时才调 `_require_native()(prefix, h2c_ch, c2h_ch)`；否则直接用注入对象。这一行是「无硬件测试」能成立的物理基础。

按名字搬运的薄包装，全部 `int()` 钉类型、不含地址：

[drivers/chisel_npu_py/src/chisel_npu_py/backend.py:88-102](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/backend.py#L88-L102) —— `write_staged` / `read_staged` / `operand_size` 三个方法各自一行 `return int(self._native.xxx(...))`。注意 `Buffer` 类型（[backend.py:31](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/backend.py#L31)）= `Union[np.ndarray, bytes, bytearray, memoryview]`，即任意类 buffer 对象都被接受。

设备节点发现（无硬件也能调，只 glob 文件系统）：

[drivers/chisel_npu_py/src/chisel_npu_py/backend.py:68-79](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/backend.py#L68-L79) —— `list_nodes` 用 `glob.glob(prefix + "_*")` 列出节点，`assert_nodes_present(minimum=3)` 在节点少于 3 个时报 `XDMAError` 提示「`xdma` 内核驱动是否加载」。硬件测试 fixture（见 4.4）正是靠它自动跳过。

#### 4.2.4 代码实践

**实践目标**：体会「按名字搬运」与「按地址搬运」的区别。

1. 阅读上一讲 u8-l2 的 C 工具用法：每次 `dma_to_device` 都要手写 `/dev/xdma0_h2c_0` 与一个十六进制 AXI 地址。
2. 对照本讲的 [backend.py:88-94](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/backend.py#L88-L94)：`write_staged("A", a)` 既不传设备路径、也不传地址，全部由 native 查表决定。
3. 在无硬件开发机上，运行以下「源码阅读型」片段（不会真开设备，因为 `FakeNative` 不碰 `/dev`）：

   ```python
   # 示例代码（非项目原代码）：体验可注入 native
   from chisel_npu_py import XDMADevice
   from chisel_npu_py.tests.fake_native import FakeNative
   import numpy as np
   dev = XDMADevice(native=FakeNative())      # 注入替身，不碰真实设备
   print(dev.operand_size("ACCUM"))           # 128
   dev.write_staged("A", np.full(32, 3, dtype=np.int8))
   got = np.empty(32, dtype=np.int8); dev.read_staged("A", got)
   print((got == 3).all())                     # True
   ```

4. **需要观察**：上述代码全程没有出现任何 `0x4000_0000` 之类的地址；地址只活在 `FakeNative` 内部的 `_STAGING`（[fake_native.py:26-31](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/tests/fake_native.py#L26-L31)，名字带前导下划线，是模块私有）。
5. **预期结果**：打印 `128` 和 `True`。这就是「Python 看不到地址」的直接体感。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `backend.py` 用 `try/except ImportError` 而不是直接 `from ._native import NativeXDMA`？

**参考答案**：直接 import 会让「没编译 `_native` 的开发机」连 `import chisel_npu_py` 都失败，于是无法跑任何单测、无法塞 `FakeNative`。延迟导入把编译缺失降级成「只有真要开设备时才报错」，让无硬件单测成为可能（见 [backend.py:36-44](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/backend.py#L36-L44)）。

**练习 2**：`write_staged` 的返回值是 `int(self._native.write_staged(...))`，为什么要套一层 `int()`？

**参考答案**：pybind11 把 C++ `size_t` 转成 Python 整数，套 `int()` 是防御性写法，确保无论 native 返回的是 int 还是别的数值类型（包括 `FakeNative` 返回的 Python int），调用方拿到的都是干净的 Python `int`，类型一致、便于断言。

---

### 4.3 CtrlLite：start/done/busy 三位协议

#### 4.3.1 概念说明

`CtrlLite` 封装 ctrl_lite 寄存器的位协议。它不直接碰寄存器，而是通过 `XDMADevice.ctrl_read()/ctrl_write()` 间接读写——这又一次体现了分层：`CtrlLite` 只懂「位」，`XDMADevice` 只懂「搬运与那个控制字」，native 只懂「地址与 fd」。

三个比特的语义（与 u8-l2 一致）：

| 比特 | 名字 | 方向 | 语义 |
|:-----|:-----|:-----|:-----|
| 0 | `start` | 写 | 写 1 触发一次 NPU DMA+MMA 周期，自清零、边沿触发 |
| 1 | `done` | 只读 | 锁存：DMA master 完成后置 1，下次 start 清零 |
| 2 | `busy` | 只读 | 电平：DMA master FSM 活动期间为 1 |

这些位的位置定义在 [consts.py:18-20](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/consts.py#L18-L20)。注意 `consts.py` 注释特意声明「Addresses deliberately do NOT appear here」——位位置是协议、可以公开，地址是危险物、必须留在 C++。

#### 4.3.2 核心流程

一次「kick 后等完成」的流程：

1. `kick()`：写 `1 << CTRL_START_BIT`，即写值 `0b001`。
2. 硬件收到 start 上升沿，`npu_dma_master` 开始自主搬运与计算，`busy` 拉高、`done` 被清零。
3. 计算结束，硬件把 `done` 锁存为 1。
4. `wait_done(timeout_s)` 以固定间隔轮询 `is_done`，直到 `done=1` 或超时。

`wait_done` 的轮询是一个带截止时间的忙等：

\[ \text{deadline} = t_0 + T_{\text{timeout}}, \quad \text{while } t < \text{deadline: if done return True} \]

每轮之间 `time.sleep(0.01)`（10 ms）让出 CPU，避免死循环吃满一个核。

#### 4.3.3 源码精读

`kick` 只写最低位：

[drivers/chisel_npu_py/src/chisel_npu_py/ctrl.py:28-30](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/ctrl.py#L28-L30) —— `self.write(1 << consts.CTRL_START_BIT)`，值就是 `1`。start 是边沿触发、硬件自清零，所以主机不必手动清零。

位解析用移位 + 与 1：

[drivers/chisel_npu_py/src/chisel_npu_py/ctrl.py:32-38](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/ctrl.py#L32-L38) —— `is_done` 取第 1 位、`is_busy` 取第 2 位。`bool(...)` 把 0/1 钉成 Python 布尔。

轮询循环：

[drivers/chisel_npu_py/src/chisel_npu_py/ctrl.py:40-47](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/ctrl.py#L40-L47) —— 用 `time.monotonic()`（不受系统时钟调整影响）算截止时间；循环里先判 `is_done` 再 sleep，保证 done 已置位时**立即返回**而不多睡 10 ms；超时返回 `False`（由上层决定要不要抛异常）。

#### 4.3.4 代码实践

**实践目标**：在没有硬件的情况下，验证 `CtrlLite` 的位解析与 `wait_done` 逻辑，理解 `FakeNative` 如何模拟 `done` 锁存。

1. 阅读 [fake_native.py:105-119](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/tests/fake_native.py#L105-L119)：`ctrl_write` 一旦看到 `start` 位（第 0 位）就把 `_reads_after_kick` 置 0；之后每次 `ctrl_read` 让该计数 +1，达到 2 次时把 `done` 锁存为 1——这模拟了「kick 后过两拍 done 才置位」的硬件行为。
2. 设置 `fake_dev.native.scripted_timeout = True` 可让 `busy` 永久为 1、`done` 永不置位，从而触发 `wait_done` 超时。
3. 运行单测验证：`make py-test-unit`（等价于 `PYTHONPATH=drivers/chisel_npu_py/src python -m pytest drivers/chisel_npu_py/tests -m "not hw" -v`）。
4. **需要观察**：`test_ctrl_mock.py` 里的 `test_wait_done_returns_true_after_kick`（kick 后 `wait_done` 返回 `True`）与 `test_wait_done_times_out_when_busy_forever`（设 `scripted_timeout` 后 `wait_done(timeout_s=0.1)` 返回 `False` 且耗时 < 1.0 s）。
5. **预期结果**：两条单测都通过，证明 `CtrlLite` 的解析与超时逻辑正确，且完全不需要硬件。

> 待本地验证：若开发机没装 numpy/pytest，可只阅读 [tests/test_ctrl_mock.py](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/tests/test_ctrl_mock.py) 的断言，逐条推演 `FakeNative` 的计数行为。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `wait_done` 用 `time.monotonic()` 而不是 `time.time()`？

**参考答案**：`time.time()` 是「墙上时钟」，可能被 NTP 或管理员向后调整，会导致 `deadline = t0 + timeout` 提前或错乱；`time.monotonic()` 单调递增、绝不受系统时钟调整影响，是做超时截止时间的正确选择。

**练习 2**：`done` 是「锁存」而非「电平」，这对轮询意味着什么？

**参考答案**：电平信号只在 busy 期间为 1、结束后回 0，主机若没在那个窗口里读就会错过；锁存信号在完成后**保持** 1 直到下次 start，所以主机哪怕轮询得晚也能读到完成状态——轮询因此更稳健。`FakeNative` 用 `_reads_after_kick >= 2` 后「持续置 done」来模拟这个锁存特性（[fake_native.py:110-113](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/tests/fake_native.py#L110-L113)）。

---

### 4.4 ChiselNPU：stage→kick→wait→collect 四步编排

#### 4.4.1 概念说明

`ChiselNPU` 是整个驱动的**门面（facade）**，把 `XDMADevice` 与 `CtrlLite` 编排成一次完整的 MMALU 计算周期。它的核心方法 `mmalu(A, B, ACCUM)` 对应 u8-l2 里「三条 `dma_to_device` 暂存 → 一条 `reg_rw` kick → 轮询 done → 一条 `dma_from_device` 回读 OUT」的 C 工具序列，但全部藏在一次 Python 调用里，输入输出都是 numpy 数组。

模块开头的文档字符串把这条不变量写得很清楚（[npu.py:1-12](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/npu.py#L1-L12)）：「The Python side only ever moves buffers; every address lives in the native module.」

#### 4.4.2 核心流程

`mmalu` 的四步（与三个 step helper 一一对应）：

```
mmalu(A, B, ACCUM):
  1. stage_operands(A, B, ACCUM)   # 三个 write_staged，把 A/B/ACCUM 搬到 DDR3
  2. kick_and_wait(timeout_s)      # ctrl.kick() 写 start；wait_done 轮询 done
  3. collect_out(OUT)              # read_staged("OUT", out)，读回 int32 结果
  return OUT                        # int32[K] 的 numpy 数组
```

两条额外的健壮性约束：

- **kick 前查 busy**：`kick_and_wait` 先查 `is_busy`，若硬件意外仍在忙就抛 `NPUError`，避免在 busy 期间再 kick 造成状态混乱。
- **超时即异常**：`wait_done` 返回 `False`（超时）时，`kick_and_wait` 抛 `NPUTimeoutError`，提示「DMA master 或 MMALU 卡住」。

#### 4.4.3 源码精读

构造函数把 `XDMADevice` 与 `CtrlLite` 装配在一起，并缓存 OUT 的字节数：

[drivers/chisel_npu_py/src/chisel_npu_py/npu.py:26-35](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/npu.py#L26-L35) —— 两个子对象都允许注入（`dev`/`ctrl` 为 `None` 时才各自默认构造），`self._out_nbytes = self.dev.operand_size("OUT")` 在初始化时查一次 OUT 大小，供 `collect_out` 默认分配数组用。

三个 step helper 是四步的实现：

- 暂存：[npu.py:39-46](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/npu.py#L39-L46) —— 三行 `self.dev.write_staged(...)`，把 A（int8[K]）、B（int8[K]）、ACCUM（int32[K]）按名字写入，大小由 native 校验。
- kick 并等待：[npu.py:48-58](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/npu.py#L48-L58) —— 先 `is_busy` 守卫，再 `kick()`，再 `wait_done`，超时抛 `NPUTimeoutError`。
- 收集：[npu.py:60-65](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/npu.py#L60-L65) —— 调用者没传 `OUT` 时按 `_out_nbytes // 4` 分配 `int32[K]`；传了就直接写进调用者的 buffer（零拷贝）。

整条周期只是一个三行编排：

[drivers/chisel_npu_py/src/chisel_npu_py/npu.py:69-84](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/npu.py#L69-L84) —— `mmalu` 体只有三行：`stage_operands` → `kick_and_wait` → `return collect_out`。这是「门面模式」的典型形态：把多个子系统调用串成一个高层操作，调用者只见 `mmalu(A, B, ACCUM)`。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：手写出 `mmalu(A, B, ACCUM)` 的完整调用序列，解释「Python 看不到 DDR 地址」，并用 `FakeNative` 说明无硬件测试。

**第一部分：写出调用序列。** 阅读 [npu.py:69-84](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/npu.py#L69-L84) 与 [backend.py:88-102](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/backend.py#L88-L102) 后，把一次 `npu.mmalu(A, B, ACCUM)` 展开成底层调用（应得到类似下面的序列）：

```
dev.write_staged("A",     A)        # native: pwrite(h2c_fd, A, 32,   0x4000_0000)
dev.write_staged("B",     B)        # native: pwrite(h2c_fd, B, 32,   0x4000_0100)
dev.write_staged("ACCUM", ACCUM)    # native: pwrite(h2c_fd, ACCUM,128,0x4000_0200)
ctrl.kick()                        # native: ctrl_write(1)  → start 上升沿
# wait_done 轮询 ctrl_read() 的 done 位，直到硬件锁存 done=1
dev.read_staged("OUT", out)        # native: pread(c2h_fd, out, 128, 0x4000_0400)
```

注意：右栏注释里的 `0x4000_...` 地址**只存在于 native.cpp 的 `kStaging`**，Python 这一层（`mmalu`/`write_staged`）完全看不到它们——这正是边界纪律的效果。

**第二部分：解释为何 Python 看不到 DDR 地址。** 理由有三层（结合源码回答）：
1. `kStaging` 表与 `pwrite`/`pread` 的地址参数都封在 [native.cpp:44-49](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp#L44-L49) 与 [native.cpp:155-175](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp#L155-L175)，地址是 C++ 局部变量，从不作为返回值或参数跨过 pybind11。
2. Python 的 [consts.py:1-7](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/consts.py#L1-L7) **故意不含地址**，只放 `K`、operand 名字、三个控制位的位置。
3. `XDMADevice` 的公开方法签名（[backend.py:88-112](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/backend.py#L88-L112)）只有「名字 + buffer / 大小 / 控制字值」，没有地址参数。综上，Python 即使想写错地址也无从下手。

**第三部分：用 mock native 在无硬件时测试。** 参考 [tests/test_loopback.py](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/tests/test_loopback.py)（硬件版，逐 operand 字节级 round-trip）与 [tests/test_npu_mock.py](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/tests/test_npu_mock.py)（mock 版）。无硬件测试的关键是 `FakeNative`：

- 它在 [fake_native.py:26-31](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/tests/fake_native.py#L26-L31) 维护一份**模块私有**的 `_STAGING`（带前导下划线，地址不暴露），校验逻辑（[fake_native.py:52-62](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/tests/fake_native.py#L52-L62)）与 C++ 的 `validate_transfer` 一一对应——**替身同样守边界纪律**。
- 注入方式见 [tests/test_npu_mock.py:13-15](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/tests/test_npu_mock.py#L13-L15)：`ChiselNPU(dev=XDMADevice(native=FakeNative()))`。
- 于是 `test_mmalu_full_flow`（[test_npu_mock.py:25-30](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/tests/test_npu_mock.py#L25-L30)）能在开发机上验证 `mmalu` 走完四步并返回 `int32[K]`；`test_wrong_size_operand_rejected`（70-74 行）验证大小校验；`test_busy_before_kick_raises`（82-86 行）与 `test_done_timeout_raises`（89-93 行）验证两条健壮性约束。

**操作步骤**：在仓库根目录执行 `make py-test-unit`，或在开发机上 `pip install numpy 'pytest'` 后运行：

```bash
PYTHONPATH=drivers/chisel_npu_py/src python -m pytest drivers/chisel_npu_py/tests -m "not hw" -v
```

**需要观察**：mock 版测试全部通过（无硬件）；其中 `test_loopback.py` 因标记 `@pytest.mark.hw` 被跳过（`-m "not hw"`）——这正是 [conftest.py:15-29](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/tests/conftest.py#L15-L29) 里「无 `/dev/xdma0_*` 节点就 skip」的自动跳过机制。

**预期结果**：mock 单测全绿；`test_loopback.py` 的三条硬件测试（`test_staged_roundtrip_all_operands` 等）在开发机上显示 skipped，到 FPGA 主机用 `make py-test-hw` 才会真跑。

> 待本地验证：具体测试条数与跳过数取决于本机环境；文档声称 mock/unit 共 26 条、硬件 23 条（见 [PythonDriver.md:125,140](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/PythonDriver.md#L125)）。

#### 4.4.5 小练习与答案

**练习 1**：`kick_and_wait` 为什么在 kick **之前**先查 `is_busy`？如果省略会怎样？

**参考答案**：见 [npu.py:51-53](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/npu.py#L51-L53)。`start` 是边沿触发，若 DMA master 还在忙（上一轮没结束）时再写一个 start 脉冲，可能让 FSM 状态混乱或丢数据。前置 `is_busy` 检查把「上一次还没完」这个软件 bug 变成立即可见的 `NPUError`，而不是一次隐蔽的错误计算。`test_busy_before_kick_raises` 正是测这条。

**练习 2**：`collect_out(OUT=None)` 时如何决定分配多大的数组？为什么能保证和硬件 OUT 区大小一致？

**参考答案**：构造时 `self._out_nbytes = self.dev.operand_size("OUT")`（[npu.py:35](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/npu.py#L35)），`operand_size` 又来自 native 的 `kStaging` 表——而 native 的 `read_staged` 也会用同一张表校验字节数（[native.cpp:166-175](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp#L166-L175)）。两端引用同一权威表，所以 `_out_nbytes // 4` 分配的 `int32[K]` 必然与硬件 OUT 区字节数一致，不会触发大小校验失败。

**练习 3**：`test_mmalu_returns_expected_data`（[test_npu_mock.py:33-39](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/tests/test_npu_mock.py#L33-L39)）里 `mmalu` 返回的「期望数据」是怎么来的？这反映了 `FakeNative` 的什么局限？

**参考答案**：该测试先 `npu.dev.write_staged("OUT", expected)` 往 `FakeNative` 的内存里预置 OUT 区，再调 `mmalu`；由于 `FakeNative` **不会真正做矩阵乘**，`mmalu` 的 `collect_out` 读回的就是之前预置的那块内存。这反映了 `FakeNative` 的局限：它只验证「搬运 + 协议 + 编排」逻辑（数据通路 round-trip），不验证 MMALU 的算术正确性——后者只能靠硬件测试 `test_mmalu_compute.py` 在真实板上做 bit-exact 校验。

---

## 5. 综合实践

**任务**：在不碰任何真实硬件的前提下，亲手用 `FakeNative` 把 `ChiselNPU` 的四步周期跑通，并注入一次「超时」故障，观察驱动的报错行为。这把本讲四个模块（pybind11 边界、`XDMADevice`、`CtrlLite`、`ChiselNPU`）串成一条可运行的链路。

**操作步骤**（示例代码，非项目原文件）：

```python
# 综合实践示例：无硬件走通 mmalu 四步 + 注入超时
import numpy as np
from chisel_npu_py import ChiselNPU, XDMADevice, NPUTimeoutError
from chisel_npu_py.tests.fake_native import FakeNative

# 1. 用 FakeNative 组装一条不碰 /dev 的链路（边界纪律的替身）
npu = ChiselNPU(dev=XDMADevice(native=FakeNative()))

# 2. 构造合法 operand（A/B 是 int8[K]，ACCUM 是 int32[K]）
K = 32
A = np.full(K, 10, dtype=np.int8)
B = np.full(K, 7,  dtype=np.int8)
ACCUM = np.zeros(K, dtype=np.int32)

# 3. 正常四步：stage → kick → wait(done 在 FakeNative 读 2 次后锁存) → collect
out = npu.mmalu(A, B, ACCUM, timeout_s=0.5)
assert out.dtype == np.int32 and out.shape == (K,)

# 4. 注入故障：让 busy 永久为 1、done 永不置位
npu.dev.native.scripted_timeout = True
try:
    npu.mmalu(A, B, ACCUM, timeout_s=0.1)
    raise SystemExit("应该超时却没超时")
except NPUTimeoutError as e:
    print("如预期超时：", e)
```

**需要观察与解释**：

1. 第 3 步正常返回一个 `int32[32]` 数组——说明 stage/kick/wait/collect 四步在 `FakeNative` 上走通；由于 `FakeNative` 不算矩阵乘，OUT 区是 `FakeNative.mem` 里的默认零值（或你预置的值）。
2. 第 4 步抛 `NPUTimeoutError`——这是 `kick_and_wait`（[npu.py:54-58](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/npu.py#L54-L58)）在 `wait_done` 返回 `False` 时抛出的，根因是 `FakeNative` 在 `scripted_timeout=True` 时持续返回 `busy=1`（[fake_native.py:108-109](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/tests/fake_native.py#L108-L109)）。
3. 全程没有任何 `0x4000_...` 地址出现在你的脚本里——地址只活在 `FakeNative._STAGING` 与 C++ 的 `kStaging` 里，这正是 pybind11 边界纪律的直接体感。

**预期结果**：脚本打印「如预期超时：…」并正常退出。若你在 FPGA 主机上跑，把 `FakeNative()` 换成默认 `XDMADevice()`（即 `ChiselNPU()`），第 3 步会得到真实的 MMALU `int32[32]` 结果，第 4 步则不要轻易触发（会让真实硬件空跑一个超时周期）。

> 待本地验证：本实践需开发机装好 `numpy` 与可 import 的 `chisel_npu_py`（`PYTHONPATH=drivers/chisel_npu_py/src`）。算术正确性只有在真实板上用 `test_mmalu_compute.py` 才能验证。

## 6. 本讲小结

- `chisel_npu_py` 用一条 **pybind11 边界**把所有「危险物」（fd、DDR 地址、寄存器偏移、DMA 传输）关进 C++ 模块 `chisel_npu_py._native`（`NativeXDMA`），Python 只剩「按名字搬 buffer + 一个控制字」的安全接口。
- **`kStaging` 表是地址的唯一权威**（[native.cpp:44-49](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/native_src/native.cpp#L44-L49)），`consts.py` 故意不含地址；`write_staged`/`read_staged` 按名字查表并精确校验字节数，Python 无从误寻址。
- `XDMADevice`（[backend.py](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/backend.py)）是 native 的类型化薄包装，靠**延迟导入**与**可注入 native** 让无硬件单测成为可能。
- `CtrlLite`（[ctrl.py](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/ctrl.py)）封装 `start/done/busy` 三位协议，`wait_done` 用 `time.monotonic()` 做截止时间、10 ms 间隔轮询，依赖 `done` 的**锁存**语义保证不漏检。
- `ChiselNPU.mmalu`（[npu.py:69-84](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/npu.py#L69-L84)）是门面，把 stage→kick→wait→collect 四步串成一次调用，kick 前查 busy、超时抛 `NPUTimeoutError`。
- 无硬件测试靠纯 Python 的 `FakeNative`（[fake_native.py](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/tests/fake_native.py)），它**同样守边界纪律**（地址私有、校验与 C++ 对应），只验证编排与协议、不算矩阵乘。

## 7. 下一步学习建议

- **算术正确性**：本讲的 `FakeNative` 不做矩阵乘。若想看 NPU 真正在算什么，读硬件测试 `tests/test_mmalu_compute.py` 中的解析公式 `OUT[i] = A[i]·B[K-1] + ACCUM[i]` 与 bit-exact 套件，这部分承接 u4（MMALU）的流式归约语义。
- **部署与 CI**：阅读 `drivers/chisel_npu_py/tool/deploy.sh` 与 [Makefile:71-83](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/Makefile#L71-L83) 的 `py-build/py-deploy/py-test-unit/py-test-hw` 四个目标，理解「sdist 在开发机构建、扩展必须在 FPGA 主机目标 venv 内编译（解释器 ABI 必须匹配，不能交叉编译 wheel）」的部署约束。
- **回到 RTL**：Python 侧的 `start/done/busy` 与 staging 地址最终都落到 RTL。可对照 u8-l1 的 `npu_subsys`（`ctrl_lite` + `npu_dma_master` + MMALU）与 u8-l2 的 C 工具，画出「Python 调用 → native → /dev/xdma0_* → XDMA IP → AXI → npu_dma_master → MMALU」的完整端到端链路图。
- **量化脚本接口**：本讲的 numpy 优先 API（`int8[K]` 进、`int32[K]` 出）正是 u7（量化流水线）期望的上层形状；后续若要把 MMA→vcvt→vfma→vcvt 的量化链驱动起来，`ChiselNPU` 是天然的宿主。
