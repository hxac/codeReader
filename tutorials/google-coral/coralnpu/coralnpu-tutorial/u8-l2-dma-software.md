# DMA 软件编程指南

## 1. 本讲目标

上一讲（[u8-l1 DMA 引擎硬件](u8-l1-dma-engine-hw.md)）我们拆解了 DMA 引擎的硬件实现——单通道、链表描述符、13 态状态机、两个 TileLink-UL 端口。本讲把视角从「硬件设计者」切换到「固件开发者」：**站在 CPU 一侧，如何用 C 语言驱动这台 DMA 把数据搬起来**。

学完本讲你应该能够：

1. 看懂并亲手组装一个 `dma_descriptor`，特别是把传输长度、beat 宽度、地址是否固定等参数打包进 32 位的 `len_flags` 字。
2. 默写出 DMA 的标准编程序列：**写描述符 → 写 `DESC_ADDR` → 写 `CTRL=0x3`（enable+start）→ 轮询 `STATUS.done`**。
3. 分清 Mem→Mem、Mem→Periph、Periph→Mem 三种模式，知道何时该让源/目的地址「固定不递增」。
4. 读懂 `STATUS` 的 `done`/`error`/`error_code` 位域，理解 `abort` 之后状态机的归宿，以及外设流控轮询（polling）如何把 DMA 速度自动适配慢速外设。

---

## 2. 前置知识

本讲假设你已经读过 [u8-l1](u8-l1-dma-engine-hw.md)，知道 CoralNPU 的 DMA 有哪些寄存器、状态机怎么走。如果没有，至少需要理解以下几个词：

- **DMA（Direct Memory Access，直接存储器访问）**：一块不经过 CPU、自己就能在总线之间搬运数据的硬件。CPU 只要给它一份「任务说明书」，它就能在后台把活干完，CPU 这期间可以去做别的（或者干脆 `wfi` 省电）。
- **描述符（descriptor）**：就是上面说的「任务说明书」。它是一段放在内存里的数据结构，记录「从哪搬、搬到哪、搬多少、每次搬多大、搬完去哪找下一份说明书」。
- **CSR（Control/Status Register，控制状态寄存器）**：DMA 对外暴露的几个可编程寄存器（`CTRL`/`STATUS`/`DESC_ADDR` 等）。CPU 通过读写它们来「下发命令、查询进度」。
- **beat（节拍）**：DMA 一次总线事务搬运的数据单元。beat 宽度可以是 1/2/4 字节等；一次传输 = 多个 beat 拼起来。
- **TL-UL / AXI**：CoralNPU SoC 内部用 TileLink-UL 总线，对外用 AXI。DMA 的 device 端口（32 位，接 CPU）和 host 端口（128 位，去搬数据）都是 TL-UL（详见 [u3-l3](u3-l3-tlul-axi-bridge.md)）。

> 一句话定位：DMA 的 device 端口让它看起来像 GPIO/SPI 一样是个普通的 32 位 TL-UL 从机（CPU 用它编程序列）；它的 host 端口让它又能像 CPU 一样发起 128 位读写去搬数据。本讲只关心前者——**CPU 怎么编程它**。

---

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [doc/sw/dma.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/sw/dma.md) | **软件编程主指南**：寄存器表、描述符格式、`make_len_flags`、编程序列、三种模式、链接、流控、abort、约束。本讲的「目录」。 |
| [doc/peripherals/dma.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md) | **硬件外设规格**：架构、双端口、状态机、TL-UL 接口语义。解释软件序列背后硬件「为什么这么走」。 |
| [fpga/sw/dma.h](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma.h) | **真实的 C 头文件**：`dma_descriptor` 结构体定义与驱动函数声明。这是 `doc/sw/dma.md` 里伪代码的「真身」。 |
| [fpga/sw/dma.c](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma.c) | **真实的驱动实现**：`dma_start`/`dma_wait_done`/`dma_get_status`/`dma_make_len_flags` 的可编译实现。 |
| [fpga/sw/dma_test.cc](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma_test.cc) | **真实的端到端测试**：5 个测试覆盖 Mem→Mem、描述符链接、Mem→Periph（UART）、Periph→Mem、流控轮询。本讲实践与示例的主要依据。 |
| [tests/cocotb/tlul/test_dma_integration.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_dma_integration.py) | **cocotb 仿真侧验证**：用 Python/TL-UL 重放 CPU 的编程序列，从仿真角度证明软件契约成立。 |

> 提示：`doc/sw/dma.md` 给的是「教学版」伪代码，`fpga/sw/dma.*` 给的是「可编译版」真代码。两者一一对应，本讲以真代码为准、文档为辅。

---

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：①描述符结构与 `len_flags` 位域；②寄存器映射与标准编程序列；③三种传输模式；④描述符链接；⑤错误码、abort 与流控轮询。

### 4.1 描述符结构与 `len_flags` 位域组装

#### 4.1.1 概念说明

DMA 是「被动」的——它不会自己知道要搬什么。CPU 必须先在内存里摆好一份**描述符**，再把这份描述符的地址告诉 DMA。可以把描述符理解成一张快递单：

- **src_addr**：从哪取货。
- **dst_addr**：送到哪。
- **len_flags**：一「压缩包」字段，里面塞了搬运长度、每次搬多大、源/目的地址是否固定、要不要流控等一揽子参数。
- **next_desc**：搬完这单之后，去哪个地址取下一张快递单（0 表示没下一张，结束）。
- **poll_addr/mask/value**：外设流控用，先按下不表，4.5 节细讲。

关键约束有两点：

1. **描述符必须 32 字节对齐**。因为 DMA 用 128 位（16 字节）的 host 端口**一次取半张**描述符，两拍取完整张（32 字节 = 两个 128 位 beat）。对齐才能保证这两拍落在一个可整除的行里。这点在 [u8-l1](u8-l1-dma-engine-hw.md) 的 `FETCH_DESC_0`/`FETCH_DESC_1` 状态已交代。
2. **描述符必须放在 DMA 能访问到的内存**（SRAM 或 DDR），不能放在只有 CPU 能见的私有空间。

#### 4.1.2 核心流程

`len_flags` 是一张 32 位的「位域压缩表」，把好几个参数塞进一个 `uint32_t`：

| 位域 | 字段 | 含义 |
|------|------|------|
| `[23:0]` | 传输长度 | 字节数，最大 \(2^{24}-1 = 16{,}777{,}215\)，即约 16 MB |
| `[26:24]` | beat 宽度 | \(\log_2(\text{字节数})\)：0=1B、1=2B、2=4B |
| `[27]` | `src_fixed` | 置 1 则源地址每个 beat 后**不递增**（固定读同一寄存器） |
| `[28]` | `dst_fixed` | 置 1 则目的地址每个 beat 后**不递增**（固定写同一寄存器/FIFO） |
| `[29]` | `poll_en` | 置 1 则每个 beat 前先做一次流控轮询 |
| `[31:30]` | 保留 | 必须为 0 |

打包公式（即 `make_len_flags`）就是把每个参数移到自己的位段，再按位或：

\[
\text{len\_flags} = (\text{len}\ \&\ 0\text{xFFFFFF})\ \|\ ((\text{width}\ \&\ 0\text{x7}) \ll 24)\ \|\ (\text{src\_fixed} \ll 27)\ \|\ (\text{dst\_fixed} \ll 28)\ \|\ (\text{poll\_en} \ll 29)
\]

> 为什么 beat 宽度用 \(\log_2\)？因为总线事务的 `size` 字段本来就是 \(\log_2\)（字节数），硬件直接把这 3 位移给 TL-UL A 通道即可，零开销。

#### 4.1.3 源码精读

先看**真实的结构体定义**（`fpga/sw/dma.h`），它和 [doc/sw/dma.md:57-68](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/sw/dma.md#L57-L68) 的教学版逐字段一致，但多了两个编译属性：

[dma.h:29-38 — `dma_descriptor` 结构体，packed 且 32 字节对齐](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma.h#L29-L38)

- `__attribute__((packed))`：取消编译器在字段间的填充，让 8 个 `uint32_t` 严丝合缝地排成 32 字节，与硬件取描述符的字节序对齐。
- `__attribute__((aligned(32)))`：强制结构体本身 32 字节对齐，满足硬件「描述符必须 32 字节对齐」的硬约束。

再看**真实的位域打包函数**，与文档的 [make_len_flags（doc/sw/dma.md:73-79）](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/sw/dma.md#L73-L79) 完全同构：

[dma.c:42-47 — `dma_make_len_flags` 把长度/宽度/标志位拼进一个 32 位字](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma.c#L42-L47)

逐位对照位域表：`(len & 0xFFFFFF)` 取低 24 位长度；`(width_log2 & 0x7) << 24` 放到 `[26:24]`（`& 0x7` 把宽度钳在 3 位内）；三个 `? 1u : 0u` 的标志位分别移到 27/28/29。`[31:30]` 不写即自然为 0，满足「保留位必须为 0」。

描述符的完整字段布局可对照硬件规格 [doc/peripherals/dma.md:69-81](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L69-L81)。

#### 4.1.4 代码实践

**目标**：亲手算一个 `len_flags`，验证你对位域的理解。

**步骤**：

1. 假设要搬 64 字节、4 字节 beat、源/目的都递增、不轮询。
2. 套公式：`width_log2=2`（4B），`src_fixed=0`、`dst_fixed=0`、`poll_en=0`。
3. 手算：
   - `len = 64 = 0x40`
   - `width << 24 = 2 << 24 = 0x0200_0000`
   - 其余标志位均为 0
   - 故 `len_flags = 0x0200_0040`
4. 打开 [dma_test.cc:51](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma_test.cc#L51)，对照 `dma_make_len_flags(nbytes, 2, 0, 0, 0)`，其中 `nbytes = 16*4 = 64`，确认你的手算思路与真代码一致（真代码里 `nbytes=64` 时结果同为 `0x0200_0040`）。

**需要观察的现象**：beat 宽度位（`[26:24]`）随 `width_log2` 变化而左移；只改 `dst_fixed=1` 时，结果会增加 `0x1000_0000`（即 `1<<28`）。

**预期结果**：手算值与函数返回值一致。无法本地运行时，标注「待本地验证」——可在任意 C 环境里 `printf("%08x", dma_make_len_flags(64,2,0,0,0))` 验证。

#### 4.1.5 小练习与答案

**练习 1**：要搬 16 MB 上限的传输，长度字段够用吗？为什么是 24 位？

**答案**：长度字段 24 位，最大 \(2^{24}-1 = 16{,}777{,}215\) 字节 ≈ 16 MB，刚好对应 [doc/sw/dma.md:189](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/sw/dma.md#L189) 的约束「Maximum transfer per descriptor: 16 MB」。更大的传输要靠描述符链接（4.4 节）拼接。

**练习 2**：`make_len_flags(8, 0, 0, 1, 0)` 的结果是多少？它描述了什么样的传输？

**答案**：`len=8`、`width=0`（1 字节 beat）、`dst_fixed=1`。结果 = `0x8 | (0<<24) | (1<<28)` = `0x1000_0008`。它描述「以字节为单位、目的地址固定」的传输——典型场景是把 8 个字节逐个写进外设的同一个数据寄存器（如 UART/SPI 的 TXDATA），这正是 [dma_test.cc:144-145](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma_test.cc#L144-L145) Test 3 的用法。

---

### 4.2 寄存器映射与标准编程序列

#### 4.2.1 概念说明

描述符摆好之后，CPU 还得通过 DMA 的 5 个 CSR 把它「点着」。DMA 的 CSR 区基址是 `0x40050000`（4 KB 区段，紧挨在 I2C `0x40040000` 之后，见 [doc/peripherals/dma.md:44](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L44)）。CPU 对这些寄存器的读写，走的就是普通的 32 位 TL-UL device 端口访问（和访问 GPIO 没区别）。

5 个寄存器分两类：**控制类**（CPU 写命令）和**状态类**（CPU 读进度）。

| 偏移 | 名字 | 访问 | 含义 |
|------|------|------|------|
| `0x00` | `CTRL` | RW | `[0]` enable、`[1]` start（写 1 置位、硬件自清）、`[2]` abort |
| `0x04` | `STATUS` | RO | `[0]` busy、`[1]` done、`[2]` error、`[7:4]` error_code |
| `0x08` | `DESC_ADDR` | RW | 第一张描述符的地址 |
| `0x0C` | `CUR_DESC` | RO | 当前正在执行的那张描述符的地址 |
| `0x10` | `XFER_REMAIN` | RO | 当前传输还剩多少字节 |

（完整表见 [doc/sw/dma.md:14-20](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/sw/dma.md#L14-L20)）

#### 4.2.2 核心流程

标准编程序列只有 4 步（[doc/sw/dma.md:82-108](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/sw/dma.md#L82-L108) 与 [doc/peripherals/dma.md:54-62](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L54-L62)）：

```
1. 在内存里摆好描述符（填好 src/dst/len_flags/next_desc）
2. REG32(DESC_ADDR) = 描述符地址
3. REG32(CTRL) = 0x3        // enable(1) | start(1)
4. 轮询 STATUS 直到 done=1，再检查 error
```

几个关键细节：

- **`CTRL=0x3` = enable + start**。`start` 是「写 1 置位、自清」位（W1S, self-clearing）——你写 1 它启动状态机，硬件干完一拍就自动把它清回 0。所以你读回 `CTRL` 不会看到 start 位常亮。
- **必须先 enable 再 start**。`enable` 是 DMA 的「总开关」；不开 enable 直接 start，引擎不会动。
- **为何轮询而非中断**：v1 的 DMA **不支持中断**（[doc/peripherals/dma.md:40](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L40)），CPU 只能轮询 `STATUS.done`。这也是 CoralNPU「run-to-completion」负载模型的体现——任务来了就一口气干完。
- `CUR_DESC`/`XFER_REMAIN` 是给人「看进度」用的，不参与控制流。

#### 4.2.3 源码精读

真实的驱动实现把上面 4 步封装成两个函数。先是寄存器访问宏与地址定义：

[dma.c:17-22 — DMA 寄存器地址定义与 `REG32` 易失读写宏](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma.c#L17-L22)

注意 `REG32` 用了 `volatile`——编译器绝不能把对状态寄存器的轮询优化成只读一次。

启动函数 `dma_start` 就是序列的第 2、3 步：

[dma.c:26-29 — `dma_start`：写描述符地址，再写 `CTRL=0x3` 点火](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma.c#L26-L29)

完成等待函数 `dma_wait_done` 是序列的第 4 步，带超时保护：

[dma.c:33-40 — `dma_wait_done`：有界轮询 `STATUS.done`，done 后再看 error](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma.c#L33-L40)

它的返回值契约很清晰：`0` = 成功；`-1` = done 但 error 位置了；`-2` = 超时（轮询 100 万次还没 done，防死锁）。`s & 0x2` 是 done 位，`s & 0x4` 是 error 位。

真实调用方在 [dma_test.cc:58-64](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma_test.cc#L58-L64)：`dma_start(...)` 之后紧跟 `int rc = dma_wait_done();`，`rc` 非 0 即打印 `FAIL` 并返回。这就是「启动 → 等完 → 看返回值」的标准套路。

#### 4.2.4 代码实践

**目标**：用 `git grep` 摸清这套驱动在仿真侧的「镜像」，理解软件契约如何被验证。

**步骤**：

1. 阅读 [tests/cocotb/tlul/test_dma_integration.py:176-220](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_dma_integration.py#L176-L220) 的 `test_dma_mem_to_mem`。
2. 注意它用 Python 重放了完全相同的序列：`tl_write(DMA_DESC_ADDR, desc_addr)` → `tl_write(DMA_CTRL, 0x3)` → `poll_dma_done(...)`，与 `dma_start`/`dma_wait_done` 一一对应。
3. 对比 `poll_dma_done`（[test_dma_integration.py:135-142](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_dma_integration.py#L135-L142)）与 C 侧 `dma_wait_done`：两者都在轮询 `STATUS & 0x2`，只是 C 用循环计数、Python 用 `ClockCycles`。

**需要观察的现象**：仿真侧与固件侧的编程序列字字对应，说明 `doc/sw/dma.md` 描述的契约是「单一真相源」，硬件、固件、仿真三方一致。

**预期结果**：能口头复述「C 的 `dma_start` 对应 Python 的 `DESC_ADDR`+`CTRL=0x3` 两写，C 的 `dma_wait_done` 对应 Python 的 `poll_dma_done`」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `dma_wait_done` 要做有界轮询（最多 100 万次）而不是 `while(1)`？

**答案**：防止硬件异常或配置错误时 CPU 永久挂死。文档版（[doc/sw/dma.md:102](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/sw/dma.md#L102)）为了教学简洁写成 `while (!(STATUS & 0x2)) {}`，真代码（`dma.c`）加了超时返回 `-2`，是工程上更稳妥的写法。

**练习 2**：`CTRL=0x3` 里的 `start` 位为什么读不回来？

**答案**：`start` 是 W1S（write-1-to-set）自清位。写 1 触发状态机启动后，硬件在同一拍或下一拍把它清 0，所以你随后读 `CTRL` 只能看到 `enable` 位（`0x1`），看不到 `start`。这由 [doc/sw/dma.md:16](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/sw/dma.md#L16) 的「self-clearing」注脚保证。

---

### 4.3 三种传输模式：地址固定 vs 递增

#### 4.3.1 概念说明

DMA 最强大的地方在于：**同一个引擎，只要改 `len_flags` 里两个标志位，就能服务截然不同的场景**。这三个场景的差别，全在「源地址、目的地址在每个 beat 之后要不要递增」上：

| 模式 | 源地址 | 目的地址 | 典型场景 |
|------|--------|----------|----------|
| Mem→Mem | 递增 | 递增 | SRAM↔DDR、把权重从 DDR 搬进 TCM |
| Mem→Periph | 递增 | **固定** | 把一段 SRAM 缓冲逐字节灌进 SPI/UART 的 TX FIFO |
| Periph→Mem | **固定** | 递增 | 把 I2C/UART 的 RX 寄存器反复读进一段 SRAM 缓冲 |

直觉上：「固定」就是反复读写**同一个寄存器**。外设的数据寄存器（如 `TXDATA`）本质上是一个 FIFO 的入口，你往同一个地址写 N 次，就是入队 N 个字节；如果地址递增，就会写到 `TXDATA`、`TXDATA+4`、`TXDATA+8`……那些后继地址根本不是有效寄存器，行为未定义。所以**凡是对外设 FIFO/数据寄存器的访问，对应方向必须 fixed**。

（表格见 [doc/peripherals/dma.md:28-33](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L28-L33)）

#### 4.3.2 核心流程

三种模式的 `len_flags` 组装只差两个标志位：

```
Mem→Mem     : make_len_flags(len, width, src_fixed=0, dst_fixed=0, poll_en=0)
Mem→Periph  : make_len_flags(len, width, src_fixed=0, dst_fixed=1, poll_en=0)
Periph→Mem  : make_len_flags(len, width, src_fixed=1, dst_fixed=0, poll_en=0)
```

硬件侧的对应行为（[doc/peripherals/dma.md:171-173](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L171-L173)）：在 `XFER_WRITE_RESP` 状态，每个 beat 完成后「updates addresses (unless fixed)」——即除非对应 `fixed` 位置 1，否则 `src_addr`/`dst_addr` 才按 beat 宽度递增。

#### 4.3.3 源码精读

真代码 `dma_test.cc` 用真外设（UART0）把三种模式各打了一遍。

**Mem→Mem（Test 1）**：源/目的都在 SRAM，都是递增，4 字节 beat：

[dma_test.cc:49-56 — 构造 Mem→Mem 描述符：`dma_make_len_flags(nbytes, 2, 0, 0, 0)`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma_test.cc#L49-L56)

**Mem→Periph（Test 3）**：把 4 字节 `"DMA\n"` 灌进 UART0 的 `WDATA`，目的固定、1 字节 beat：

[dma_test.cc:142-150 — Mem→Periph：`dst_addr=UART0_WDATA`，`dst_fixed=1`，beat=1B](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma_test.cc#L142-L150)

注意这里 `width_log2=0`（1 字节 beat），因为 UART 是按字节发送的；`dst_fixed=1` 保证 4 次写都落在同一个 `WDATA` 寄存器。

**Periph→Mem（Test 4）**：反复读 UART0 的 `STATUS` 寄存器进缓冲，源固定：

[dma_test.cc:170-177 — Periph→Mem：`src_addr=UART0_STATUS`，`src_fixed=1`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma_test.cc#L170-L177)

验证逻辑很巧妙（[dma_test.cc:190-196](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma_test.cc#L190-L196)）：因为反复读同一个寄存器，8 个目的字应该**全部相等**——这正好印证了「src_fixed」的语义。

#### 4.3.4 代码实践

**目标**：把 Test 3 改写成一个通用的「把任意缓冲发给 SPI TX」的描述符。

**步骤**：

1. 复制 Test 3 的描述符骨架（[dma_test.cc:142-150](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma_test.cc#L142-L150)）。
2. 把 `dst_addr` 从 `UART0_WDATA` 换成 SPI TXDATA 地址。参考 [doc/sw/dma.md:126-129](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/sw/dma.md#L126-L129) 的示例，SPI TXDATA 在 `0x40020008`。
3. 保持 `dst_fixed=1`、按外设数据宽度选 beat（SPI 通常 1 字节，故 `width_log2=0`）。

**示例代码**（基于真代码改写，非项目原文件）：

```c
// 把 SRAM 里的 nbytes 字节逐个发到 SPI TXDATA（Mem→Periph，目的固定）
desc->src_addr  = (uint32_t)sram_buffer;
desc->dst_addr  = 0x40020008;                              // SPI TXDATA
desc->len_flags = dma_make_len_flags(nbytes, 0, 0, 1, 0);  // 1B beat, dst_fixed
desc->next_desc = 0;
```

**需要观察的现象**：若误把 `dst_fixed` 写成 0，DMA 会向 `0x40020008`、`0x40020009`、`0x4002000a`…… 递增写，后三者不是有效 SPI 寄存器，搬运会失败或触发总线错误。

**预期结果**：`dst_fixed=1` 时 SPI 收到完整字节流；`dst_fixed=0` 时行为异常。完整运行需 FPGA 或仿真环境，**待本地验证**。

#### 4.3.5 小练习与答案

**练习**：为什么 Mem→Periph 用 1 字节 beat（`width_log2=0`），而 Mem→Mem 常用 4 字节 beat（`width_log2=2`）？

**答案**：beat 宽度不能超过目标能一次吞下的粒度。UART/SPI 的数据寄存器按字节收发，beat 必须 ≤ 1 字节；而 SRAM 之间搬运没有这种限制，用更宽的 beat（4 字节甚至 16 字节）能减少总线事务数、提高吞吐。约束见 [doc/sw/dma.md:191](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/sw/dma.md#L191)「Beat width must not exceed the bus width」。

---

### 4.4 描述符链接（Chaining）

#### 4.4.1 概念说明

单个描述符最多搬 16 MB，而且一次只能描述「一段连续的源→一段连续的目的」。如果一次任务需要**多次不同参数的搬运**（比如先把权重搬进 TCM、再把输入搬进 TCM、参数还不一样），难道要 CPU 每次 `dma_start` 后都停下来等？

不用。DMA 支持**描述符链表**：每张描述符的 `next_desc` 字段指向下一张，硬件搬完一张就**自动**去取下一张，直到遇到 `next_desc=0` 才停。CPU 只需把链头地址写进 `DESC_ADDR` 一次，然后等整条链跑完。

这把「CPU 驱动」变成了「DMA 自己驱动自己」——CPU 的介入次数从 N 降到 1。

#### 4.4.2 核心流程

```
1. 在内存里摆好 desc0、desc1、...、descN（每张 32 字节、各自 32 字节对齐）
2. desc0->next_desc = &desc1;  desc1->next_desc = &desc2; ... descN->next_desc = 0
3. REG32(DESC_ADDR) = &desc0
4. REG32(CTRL) = 0x3
5. 轮询 STATUS.done —— 注意：done 只在【整条链】跑完后才置 1
```

硬件侧对应 [doc/peripherals/dma.md:172-173](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L172-L173)：`XFER_WRITE_RESP` 在剩余量为 0 时判断——若 `next_desc != 0`，回到 `FETCH_DESC_0` 取下一张；若 `next_desc == 0`，进 `DONE`。`STATUS.done` 只在 `DONE` 态才置位，这正是 [doc/sw/dma.md:156](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/sw/dma.md#L156)「done is set only after the entire chain completes」的由来。

#### 4.4.3 源码精读

真代码 Test 2 演示了两张描述符的链接（[dma_test.cc:74-115](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma_test.cc#L74-L115)）。关键是 `desc0` 指向 `desc1`，而 `desc1` 收尾：

[dma_test.cc:82-94 — 链接：`desc0->next_desc = desc1`，`desc1->next_desc = 0`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma_test.cc#L82-L94)

注意两张描述符在 SRAM 里的布局：`desc0` 在 `SRAM_BASE+0x2000`、`desc1` 在 `SRAM_BASE+0x2020`（[dma_test.cc:30-33](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma_test.cc#L30-L33)），正好相差 32 字节——链表里相邻描述符必须各自 32 字节对齐，`0x2020` 满足。

启动只点 `desc0` 一次（[dma_test.cc:96](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma_test.cc#L96)），然后 `dma_wait_done` 等整条链完成。仿真侧的对应验证在 [test_dma_integration.py:223-270](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_dma_integration.py#L223-L270)。

#### 4.4.4 代码实践

**目标**：画一张描述符链表的内存布局图。

**步骤**：

1. 读 [dma_test.cc:82-94](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma_test.cc#L82-L94)。
2. 画出 `SRAM_BASE+0x2000`（desc0）和 `SRAM_BASE+0x2020`（desc1）两个方框，标出各自的 `src/dst/len_flags/next_desc`。
3. 用箭头从 `desc0.next_desc` 指向 `desc1` 的首地址，`desc1.next_desc` 标 0（终止）。

**需要观察的现象**：`CUR_DESC` 寄存器（`0x0C`）在搬运过程中会先读到 `0x...2000`、再读到 `0x...2020`，正好反映硬件「走到哪张描述符了」。

**预期结果**：能指出「链表头是 `desc0`，CPU 只把 `desc0` 地址写进 `DESC_ADDR`；`done` 在两张都搬完后才置 1」。

#### 4.4.5 小练习与答案

**练习**：如果误把 `desc1->next_desc` 写成 `desc0` 的地址（而不是 0），会发生什么？

**答案**：链表成环，DMA 会永远在 `desc0`→`desc1`→`desc0`→… 之间循环搬运，`STATUS.done` 永不置位，`dma_wait_done` 最终超时返回 `-2`。这也是真代码用有界轮询的价值——`while(1)` 版本会直接挂死。

---

### 4.5 错误码、abort 语义与外设流控轮询

#### 4.5.1 概念说明

DMA 不是永远一帆风顺：总线可能出错（访问了非法地址）、CPU 可能中途想取消任务、外设可能跟不上 DMA 的速度。这三类情况分别由 `error_code`、`abort`、`poll_en` 三个机制处理。

**`STATUS` 的错误位域**（[doc/sw/dma.md:22-31](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/sw/dma.md#L22-L31)）：

| error_code `[7:4]` | 含义 |
|--------------------|------|
| 0 | 无错 |
| 1 | 描述符取取出错（fetch error） |
| 2 | 轮询读出错（poll read error） |
| 3 | 数据读出错（data read error） |
| 4 | 数据写出错（data write error） |
| 5 | 被 abort（abort） |

**abort**：写 `CTRL` 的 bit2（即 `CTRL=0x4`）可以从中途强制取消一次传输。硬件从任何状态直接跳回 IDLE，并在 `STATUS` 里留下 `done=1, error=1, error_code=5` 的「墓志铭」（[doc/sw/dma.md:181-184](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/sw/dma.md#L181-L184)）。

换算成 `STATUS` 整数值：`done(0x2) | error(0x4) | (5<<4 = 0x50)` = `0x56`。所以 abort 后读 `STATUS` 会得到 `0x56`。

#### 4.5.2 核心流程

**错误处理流程**：

```
dma_start(...);
int rc = dma_wait_done();      // 0=成功, -1=done但有error, -2=超时
if (rc) {
    uint32_t s = dma_get_status();
    uint32_t code = (s >> 4) & 0xF;   // 取 error_code
    // code: 1=desc fetch, 2=poll, 3=read, 4=write, 5=abort
}
```

**外设流控轮询**的动机：DMA 的 host 端口是 128 位、跑在全速总线上；可外设（SPI/I2C/UART）慢得多。如果 DMA 一股脑把数据灌进 SPI 的 TX FIFO，FIFO 会溢出。解决办法不是改外设，而是让 DMA **每搬一个 beat 之前先读一次外设的状态寄存器**，直到外设说「我准备好了」才继续：

\[
\text{每个 beat 前：读 } \text{poll\_addr},\quad \text{若 } (\text{读值}\ \&\ \text{poll\_mask}) \neq \text{poll\_value}\ \text{则重读，直到相等才搬}
\]

启用方式：`len_flags` 里置 `poll_en=1`，并填好 `poll_addr`/`poll_mask`/`poll_value` 三件套。硬件侧对应状态机的 `POLL_CHECK`→`POLL_REQ`→`POLL_RESP` 循环（[doc/peripherals/dma.md:161-165](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L161-L165)）。

#### 4.5.3 源码精读

`dma_wait_done` 已经把 error 检测封装好了——`done` 后看 `s & 0x4`：

[dma.c:33-40 — done 后用 `s & 0x4` 判 error，有错返回 -1](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma.c#L33-L40)

要拿到具体 `error_code`，自己 `(STATUS >> 4) & 0xF` 即可。

**流控轮询的真实用例**（Test 5）：把 8 字节 `"POLL_OK\n"` 发给 UART0，每个字节前先轮询 UART STATUS 的 bit0（TX full），等 TX 不满再写：

[dma_test.cc:212-220 — 流控轮询：`poll_en=1`，poll UART STATUS，等 TX 不满](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma_test.cc#L212-L220)

逐行解读：

- `dma_make_len_flags(t5_len, 0, 0, 1, 1)`：1 字节 beat、`dst_fixed=1`（写同一 TXDATA）、`poll_en=1`。
- `poll_addr = UART0_STATUS`：每个 beat 前读这个寄存器。
- `poll_mask = 0x00000001`：只看 bit0（TX full 标志）。
- `poll_value = 0x00000000`：等 `(STATUS & 0x1) == 0`，即 TX 不满，才搬这一个字节。

这正是 [doc/sw/dma.md:164-174](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/sw/dma.md#L164-L174) 所述「hardware-managed flow control without modifying peripheral designs」的落地——外设一行代码没改，DMA 自己就把速度适配了 UART 的波特率。

> 文档 [doc/sw/dma.md:178-182](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/sw/dma.md#L178-L182) 给出 abort 的最小用例：`REG32(DMA_CTRL) = 0x4;` 之后 `STATUS` 应为 `done=1, error=1, error_code=5`。仓库未提供 abort 的 C 端到端测试，该行为「待本地验证」。

#### 4.5.4 代码实践

**目标**：写一个带错误码提取的健壮 `dma_run` 封装。

**步骤**：

1. 基于 `dma_start`/`dma_wait_done`/`dma_get_status`（[dma.c](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma.c)）写一个新函数，启动后等待，失败时打印 `error_code`。

**示例代码**（基于真驱动封装，非项目原文件）：

```c
// 返回 0 成功；非 0 时 *err_code 给出 STATUS[7:4]
int dma_run(uint32_t desc_addr, uint32_t *err_code) {
    dma_start(desc_addr);
    int rc = dma_wait_done();          // 0=OK, -1=done+err, -2=timeout
    if (rc == 0) { *err_code = 0; return 0; }
    uint32_t s = dma_get_status();
    *err_code = (rc == -2) ? 0xFF : ((s >> 4) & 0xF);  // 超时用 0xFF 占位
    return rc;
}
```

**需要观察的现象**：

- 正常 Mem→Mem：返回 0，`err_code=0`。
- 给一个非法 `src_addr`（如未映射地址）：`dma_wait_done` 返回 -1，`err_code` 为 3（data read error）。
- 中途写 `CTRL=0x4` abort：`err_code` 为 5。

**预期结果**：`error_code` 与 [doc/sw/dma.md:24-31](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/sw/dma.md#L24-L31) 的定义一一对应。需 FPGA/仿真环境触发，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：abort 之后，为什么 `STATUS` 同时有 `done=1` 和 `error=1`？

**答案**：`done=1` 表示「这次任务结束了（状态机回到 IDLE，不再 busy）」，`error=1` 表示「但它不是正常结束的」。两者并不矛盾——`done` 是「终结」信号，`error` 是「异常终结」标记，`error_code=5` 进一步说明终结原因是 abort。CPU 轮询 `done` 既能等到正常完成，也能等到 abort，统一了等待逻辑。

**练习 2**：流控轮询里，如果把 `poll_mask` 写成 `0`，会发生什么？

**答案**：`poll_value` 通常也是 0，于是条件 `(读值 & 0) == 0` 恒成立，等价于不轮询——DMA 会全速搬运，失去流控意义，可能撑爆外设 FIFO。这说明 `poll_mask` 必须精准指向那个表示「就绪」的位。

---

## 5. 综合实践

把本讲 5 个模块串起来，完成一个**「加载权重」风格的双段 DMA 任务**：用一条描述符链，先把一段「输入数据」从 SRAM 搬到另一块 SRAM（Mem→Mem），再把一段「配置」逐字节发给一个外设数据寄存器（Mem→Periph，带流控）。

**任务要求**：

1. 在 SRAM 里准备：源缓冲 `input[16]`（16 个 `uint32_t`）、目的缓冲 `output[16]`、外设配置串 `cfg[4]`（4 字节）、两张描述符 `desc0`/`desc1`（地址自选，务必 32 字节对齐）。
2. `desc0`：把 `input` 的 64 字节搬到 `output`，4 字节 beat，`next_desc` 指向 `desc1`。
3. `desc1`：把 `cfg` 的 4 字节逐个发给外设 TXDATA（地址假设 `0x4000001c`，仿 UART），1 字节 beat、`dst_fixed=1`、`poll_en=1`，轮询外设 STATUS（`0x40000014`）bit0（TX full），等其为 0。
4. 只 `dma_start(&desc0)` 一次，`dma_wait_done` 等整条链，失败时用 4.5 节的方法打印 `error_code`。
5. 验证：`output[i] == input[i]` 全部成立，且 `dma_wait_done` 返回 0。

**参考骨架**（综合本讲真代码片段，非项目原文件）：

```c
#include "fpga/sw/dma.h"
#define PERIPH_TXDATA  0x4000001c
#define PERIPH_STATUS  0x40000014

volatile struct dma_descriptor* const desc0 =
    (volatile struct dma_descriptor*)(0x20000000 + 0x2000);
volatile struct dma_descriptor* const desc1 =
    (volatile struct dma_descriptor*)(0x20000000 + 0x2020);

int main(void) {
    /* ...填充 input[16]、cfg[4]={'D','M','A','\n'}、清零 output[16]... */

    // 段一：Mem→Mem
    desc0->src_addr  = (uint32_t)input;
    desc0->dst_addr  = (uint32_t)output;
    desc0->len_flags = dma_make_len_flags(64, 2, 0, 0, 0);   // 4B beat
    desc0->next_desc = (uint32_t)desc1;

    // 段二：Mem→Periph + 流控
    desc1->src_addr  = (uint32_t)cfg;
    desc1->dst_addr  = PERIPH_TXDATA;
    desc1->len_flags = dma_make_len_flags(4, 0, 0, 1, 1);    // 1B, dst_fixed, poll
    desc1->next_desc = 0;
    desc1->poll_addr  = PERIPH_STATUS;
    desc1->poll_mask  = 0x1;        // TX full 位
    desc1->poll_value = 0x0;        // 等 TX 不满

    dma_start((uint32_t)desc0);
    int rc = dma_wait_done();
    if (rc) { /* 读 (dma_get_status()>>4)&0xF 排错 */ return 1; }

    /* ...校验 output[i]==input[i]... */
    return 0;
}
```

**验收要点**：

- 两张描述符地址相差 32 字节且各自对齐（仿 [dma_test.cc:30-33](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/fpga/sw/dma_test.cc#L30-L33)）。
- 只调用一次 `dma_start`，靠 `next_desc` 让硬件自动续上第二段。
- `done` 在两段都完成后才置 1（4.4 节）。
- 第二段若去掉 `poll_en`，在真实慢速外设上可能丢字节；加上流控后自动适配。

> 该任务需要 FPGA 或带外设模型的仿真环境才能跑通；在纯 Verilator 核心仿真上可把 `PERIPH_*` 换成 SRAM 地址先验证搬运逻辑，外设部分**待本地验证**。

---

## 6. 本讲小结

- **描述符是 DMA 的任务说明书**：32 字节、必须 32 字节对齐、放 SRAM/DDR；`len_flags` 一个 32 位字塞进了长度（24 位）、beat 宽度（3 位）、`src_fixed`/`dst_fixed`/`poll_en` 三个标志位，用 `dma_make_len_flags` 打包。
- **标准编程序序只有 4 步**：摆描述符 → 写 `DESC_ADDR` → 写 `CTRL=0x3`（enable+start）→ 轮询 `STATUS.done`。v1 不支持中断，只能轮询。
- **三种模式全靠两个 fixed 位切换**：Mem→Mem 都递增；Mem→Periph 目的固定（灌 FIFO）；Periph→Mem 源固定（抽寄存器）。beat 宽度不能超过目标能吞的粒度。
- **描述符链接**把 N 次 CPU 介入压成 1 次：`next_desc` 串成链，硬件自动续，`done` 只在整条链跑完后才置位。
- **`STATUS` 给出 done/error/error_code**：error_code 区分 fetch/poll/read/write/abort 五类错误；abort（`CTRL=0x4`）后留下 `done=1, error=1, error_code=5`（`STATUS=0x56`）。
- **流控轮询**是亮点：`poll_en=1` + `poll_addr/mask/value` 让 DMA 每 beat 前自查外设就绪，无需改外设即可适配慢速 SPI/I2C/UART。
- **三方一致**：`doc/sw/dma.md`（契约）、`fpga/sw/dma.*`（固件真代码）、`tests/cocotb/.../test_dma_integration.py`（仿真验证）描述的是同一套序列，是单一真相源。

---

## 7. 下一步学习建议

本讲把 DMA 的**软件面**讲透了。接下来推荐：

1. **回到硬件细节**：若想搞清 `poll_en` 触发后状态机如何在 `POLL_REQ`/`POLL_RESP` 间循环、host 通道如何在「取描述符/轮询/读写」之间分时复用，重读 [u8-l1 DMA 引擎硬件](u8-l1-dma-engine-hw.md) 的状态机一节与 [doc/peripherals/dma.md:122-178](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L122-L178)。
2. **看真外设配合**：[u8-l3 SPI 主机与 TL-UL 桥接](u8-l3-spi-master-bridge.md) 讲了 SPI 的 TX/RX FIFO 与 STATUS 寄存器，正是本讲 Mem→Periph + 流控轮询的天然搭档。读完你会理解为什么文档示例都用 SPI 的 `0x40020008`/`0x40020000`。
3. **跑仿真验证**：用 [tests/cocotb/tlul/test_dma_integration.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_dma_integration.py) 配合 [u2-l4 cocotb 入门](u2-l4-cocotb-testbench-intro.md) 在 Verilator 上实际跑一遍 Mem→Mem 与链表测试，亲眼看到 `STATUS` 的 `done` 翻转。
4. **DMA 在 ML 流水线里的角色**：进阶可读 [u10-l3 npusim 与 MobileNet](u10-l3-npusim-mobilenet.md)，看 DMA 如何把权重从 DDR/flash 搬进 TCM、喂给 MAC 引擎——那是 DMA 真正发挥价值的场景。
