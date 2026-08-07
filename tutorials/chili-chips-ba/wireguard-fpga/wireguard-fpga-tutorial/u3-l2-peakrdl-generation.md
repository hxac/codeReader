# PeakRDL 自动生成 RTL 与 HAL

## 1. 本讲目标

本讲承接 u3-l1（我们已经在 `csr.rdl` 里读懂了寄存器地图的「单一真源」），回答下一个自然的问题：**这一份 SystemRDL 规格是怎样同时变成硬件 RTL 和软件 HAL 的？**

读完本讲，你应当能够：

- 画出从 `csr.rdl` 到 `csr.sv` / `csr_pkg.sv`（RTL）与 `csr_hw.h` / `csr_cosim.h`（HAL）的完整生成管线，并能说清每一步用哪个工具、产出什么文件。
- 看懂 HAL 的「层级指针访问 API」：为什么软件能写成 `csr->dpe->fcr->pause(1)` 这种近乎自然语言的链式调用，而底层只是一层层指针加固定地址偏移。
- 理解 `VPROC` 宏如何在编译期切换「上板用的硬件 HAL」与「仿真用的协同仿真 HAL」，让同一份应用代码 `main.cpp` 在两种环境里都能跑。
- 把「单一真源 → 多产物」这条原则讲给同事听，并知道改寄存器时该改哪里、哪些文件绝不能手改。

## 2. 前置知识

本讲假设你已经读过 u3-l1，熟悉 SystemRDL 的 `addrmap / regfile / reg / field` 四级语法、`sw`/`hw` 读写属性、`singlepulse`/`swacc`/`swmod` 修饰符，以及 `external` regfile 的含义。除此之外，再补两个通俗概念：

- **HAL（Hardware Abstraction Layer，硬件抽象层）**：介于「应用代码」和「真实硬件寄存器」之间的一层封装。应用不想关心某个字段在第几比特、地址是多少，HAL 把这些细节藏起来，只暴露 `pause(1)`、`idle()` 这种语义化函数。本项目的 HAL 是一坨自动生成的 C++ 类。
- **单一真源（Single Source of Truth）**：整个系统里**只有 `csr.rdl` 一个文件**描述寄存器。RTL 和 HAL 都由它派生，谁也不手写、谁也不偏离。这样软硬件永远不会对不上——对不上的概率被压缩到「生成器有没有 bug」，而不是「两个工程师有没有同步」。

涉及的工具谱系（都属于开源 [PeakRDL](https://github.com/SystemRDL) 生态）：

| 工具 / 库 | 角色 | 本讲产出 |
| --- | --- | --- |
| `peakrdl regblock` | 把 RDL 编译成 SystemVerilog 寄存器块 | `csr.sv`、`csr_pkg.sv` |
| `peakrdl c-header` | 把 RDL 编译成 C 结构体头文件 | `csr.h` |
| `peakrdl html` / `markdown` | 生成人类可读文档 | `html/`、`wireguard.md` |
| `systemrdl-compiler`（Python 库） | 解析 RDL 成节点树，供自定义代码遍历 | 被 `sysrdl_cosim.py` 调用 |
| `sysrdl_cosim.py`（本项目自研） | 遍历节点树，套上 C++ 类外壳 | `csr_hw.h`、`csr_cosim.h` |

## 3. 本讲源码地图

| 文件 | 作用 | 是否自动生成 |
| --- | --- | --- |
| [3.build/csr_build/csr.rdl](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl) | 单一真源（u3-l1 已精读） | 否，手写 |
| [3.build/MakefileCSR](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileCSR) | 编排整条生成管线的 Makefile | 否，手写 |
| [3.build/sysrdl_cosim.py](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/sysrdl_cosim.py) | 自研 HAL 外壳生成脚本 | 否，手写 |
| [3.build/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/README.md) | 构建说明，解释产物与命令 | 否，手写 |
| `3.build/csr_build/generated-files/csr.h` | `peakrdl c-header` 产物：纯 C 结构体 | 是 |
| `3.build/csr_build/generated-files/csr.sv` / `csr_pkg.sv` | `peakrdl regblock` 产物：硬件 RTL | 是 |
| `3.build/csr_build/generated-files/csr_hw.h` | 硬件目标 HAL（上板用） | 是 |
| `3.build/csr_build/generated-files/csr_cosim.h` | 协同仿真 HAL（VProc 仿真用） | 是 |
| [3.build/csr_build/generated-files/wireguard_regs.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/wireguard_regs.h) | 应用唯一 include 的入口，用 `VPROC` 宏选 HAL | 是（但极短，近乎手写模板） |

> 提醒：`generated-files/` 目录下除 `wireguard_regs.h` 外的文件**都是自动生成的**，文件头都写着 `*** DO NOT EDIT! ***`。改寄存器只能改 `csr.rdl` 后重新 `make`。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应规格里的「peakrdl c-header / systemrdl-compiler / HAL 头生成 / VPROC 宏切换」。它们共用一条管线，建议先看 4.1 建立全局管线图，再逐个深入。

### 4.1 单一生成管线：一份 RDL 喂出所有产物

#### 4.1.1 概念说明

整条管线的「大脑」是 [MakefileCSR](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileCSR)。它用 Make 的依赖关系把「先过滤 RDL → 再生成 C 头 → 再生成 RTL/HTML/MD → 最后套 HAL 外壳」串成一条链。理解这条链的关键是分清两个顶层名字：

- `csr`（`RDLCSRTOP`）：纯寄存器块，RTL 用它做 `--top`，所以 `csr.sv` 只实现寄存器，不含指令/数据存储器。
- `wireguard`（`RDLWGTOP`）：完整地址映射（`imem` + `dmem` + `csr`），文档和 C 头默认用它做顶层，所以 `csr.h` 里能看到 `wireguard_t` 包含 `imem`/`dmem`/`csr` 三段。

#### 4.1.2 核心流程

`make -f MakefileCSR`（即 `all` 目标）触发的依赖链如下（箭头表示「依赖 / 派生」）：

```
csr.rdl (手写真源)
  │
  │  sed 删除 buffer_writes / wbuffer_trigger（systemrdl-compiler 不认）
  ▼
csr_cosim.rdl (过滤后中间件)
  │
  ├──► peakrdl c-header -b ltoh ─────► csr.h            (纯 C 结构体)
  │
  ├──► sysrdl_cosim.py            ─────► csr_hw.h        (硬件 HAL, 包含 csr.h)
  │      (不带 -c)
  │
  └──► sysrdl_cosim.py -c         ─────► csr_cosim.h     (协同仿真 HAL, 包含 csr.h)
         (带 -c)

csr.rdl ──► peakrdl regblock --top csr        ─────► csr.sv + csr_pkg.sv   (RTL)
csr.rdl ──► peakrdl html    --top wireguard   ─────► html/
csr.rdl ──► peakrdl markdown --top wireguard  ─────► wireguard.md
```

注意三个细节：

1. **RTL（`regblock`）直接吃原始 `csr.rdl`**，不过滤；只有给 `c-header` 和 `sysrdl_cosim.py` 的那份要先 `sed` 过滤，因为 `systemrdl-compiler` 不支持 `buffer_writes`/`wbuffer_trigger` 这两个 PeakRDL 专有属性（u3-l1 提到它是 WBR 写缓冲的触发器）。
2. **`csr.h` 是两个 HAL 的公共基石**：`csr_hw.h` 和 `csr_cosim.h` 都 `#include "csr.h"`，真正的位域结构体来自这里，HAL 只是在外面包了类。
3. **文档是免费的副产品**：`html/` 和 `wireguard.md` 让你不用读 RDL 也能看寄存器说明。

#### 4.1.3 源码精读

先看 `all` 目标聚合了哪些产物（[MakefileCSR:L20](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileCSR#L20)）：

```makefile
all: $(COSIMHDR) $(HWHDR) rtl $(PKRDLHTML) $(PKRDLMD)
```

过滤步骤用 `sed` 删掉两行属性（[MakefileCSR:L28-L29](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileCSR#L28-L29)）：

```makefile
$(COSIMRDL): $(RDLSRC)
	@sed -e "/buffer_writes/d" -e "/wbuffer_trigger/d" < $^ > $@
```

RTL 生成用 `regblock` 导出器，`--cpuif passthrough` 表示 CPU 接口走最简单的直通握手（u2-l4 讲的 `soc_if`），`--top csr` 把顶层锁在寄存器块（[MakefileCSR:L34-L35](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileCSR#L34-L35)）：

```makefile
rtl: $(RDLSRC)
	@peakrdl regblock $^ -o $(GENDIR)/ --cpuif passthrough --top $(RDLCSRTOP)
```

> 想确认「过滤前后的差别」？对比 `csr.rdl`（含 `buffer_writes`）与 `generated-files/csr_cosim.rdl`（已被删掉）即可，二者除此之外完全相同——这正是 README 强调的「Other than that, the RDL is identical」。

#### 4.1.4 代码实践

**实践目标**：在不安装 peakrdl 的前提下，徒手还原这条依赖图。

**操作步骤**：

1. 打开 [MakefileCSR](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileCSR)，为每个目标（`$(COSIMRDL)`、`$(PKRDLHDR)`、`$(HWHDR)`、`$(COSIMHDR)`、`rtl`、`$(PKRDLHTML)`、`$(PKRDLMD)`）写一行：它的「输入文件」「调用的命令」「输出文件」。
2. 在 `generated-files/` 目录里 `ls`，核对每个输出文件确实存在。
3. 用 `grep -c buffer_writes 3.build/csr_build/csr.rdl 3.build/csr_build/generated-files/csr_cosim.rdl` 验证过滤确实发生了。

**预期结果**：`csr.rdl` 里能搜到 `buffer_writes`，`csr_cosim.rdl` 里搜不到；七个输出文件全部存在。**待本地验证**（取决于你是否装了 peakrdl，能实际跑 `make -f MakefileCSR`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 RTL 生成（`regblock`）可以直接吃 `csr.rdl`，而 C 头/HAL 生成必须先过滤？

> **答**：`regblock` 用的是 PeakRDL 自己的编译器，认得 `buffer_writes`/`wbuffer_trigger`；而 `c-header` 与 `sysrdl_cosim.py` 底层依赖的是上游 `systemrdl-compiler` 库，它不认识这两个 PeakRDL 扩展属性，遇到会报错。过滤只是「喂给不同工具前做兼容」，RDL 语义本身没变。

**练习 2**：`csr.sv` 的顶层模块叫 `csr`，而 `csr.h` 的最外层结构体叫 `wireguard_t`，为什么不一样？

> **答**：因为 `regblock` 用 `--top csr` 只生成寄存器块（不含 imem/dmem，那两块存储器由别的 HDL 模块实现）；而 `c-header` 没指定 `--top`，默认用整个 RDL 文件的根 addrmap `wireguard`，所以连 imem/dmem 一起生成了完整地址映射。一个是「寄存器硬件」，一个是「全系统内存地图」，自然不同名。

---

### 4.2 peakrdl c-header：把 RDL 编译成 C 结构体（csr.h）

#### 4.2.1 概念说明

`csr.h` 是整座 HAL 大厦的地基。它由 `peakrdl c-header -b ltoh` 生成，`-b ltoh` 表示「位序 little-to-host」（按宿主机小端排布）。它做两件事：

1. 为**每个字段**生成一组宏：`_bm`（bit mask，位掩码）、`_bp`（bit position，起始位）、`_bw`（bit width，位宽）、`_reset`（复位值）。
2. 为**每个寄存器**生成一个 `union`：既能按字段 `.f.xxx` 访问，又能整字 `.w` 访问；再把寄存器拼成 regfile，regfile 拼成 addrmap，层层嵌套成与 RDL 一模一样的树。

#### 4.2.2 核心流程

字段位域到宏的换算很直白。以 `dpe.fcr` 的 `pause` 字段（u3-l1 讲过，bit[1]，1 位）为例：

\[
\text{bm} = 2^{1} = \texttt{0x2},\quad \text{bp} = 1,\quad \text{bw} = 1
\]

读一个字段 = `(整字 & bm) >> bp`；写一个字段 = `(整字 & ~bm) | ((data << bp) & bm)`。`csr.h` 只给「地图」（宏 + union），具体读写动作由 4.3 的 HAL 类去实现。

整个 addrmap 用 `__packed__` 结构体铺平，并用 `static_assert(sizeof(wireguard_t) == 0x20003e00)` 在编译期校验总尺寸，确保 C 结构体的内存排布与 RDL 地址映射逐字节对齐。

#### 4.2.3 源码精读

最简单的单字段寄存器 `cpu_fifo.rx.data_31_0`（[csr.h:L28-L38](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr.h#L28-L38)）展示了标准套路：四个宏 + 一个 `union{ struct{...} f; uint32_t w; }`：

```c
#define CSR__CPU_FIFO__RX__DATA_31_0__TDATA_bm 0xffffffff
#define CSR__CPU_FIFO__RX__DATA_31_0__TDATA_bp 0
#define CSR__CPU_FIFO__RX__DATA_31_0__TDATA_bw 32
typedef union {
    struct __attribute__ ((__packed__)) {
        uint32_t tdata :32;
    } f;
    uint32_t w;
} csr__cpu_fifo__rx__data_31_0_t;
```

多字段寄存器看 `cpu_fifo.rx.control`，它把 `tuser_dst`/`tuser_src`/`tlast`/`tkeep` 等挤进一个 32 位字，中间还插了 7 位保留位（[csr.h:L76-L112](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr.h#L76-L112)）：

```c
#define CSR__CPU_FIFO__RX__CONTROL__TUSER_DST_bp 0    // bit[2:0]
#define CSR__CPU_FIFO__RX__CONTROL__TLAST_bp 15       // bit[15]
#define CSR__CPU_FIFO__RX__CONTROL__TKEEP_bp 16       // bit[31:16]
typedef union {
    struct __attribute__ ((__packed__)) {
        uint32_t tuser_dst :3;
        uint32_t tuser_src :3;
        uint32_t tuser_bypass_stage :1;
        uint32_t tuser_bypass_all :1;
        uint32_t :7;        // 保留位，自动填充
        uint32_t tlast :1;
        uint32_t tkeep :16;
    } f;
    uint32_t w;
} csr__cpu_fifo__rx__control_t;
```

u3-l1 讲过的 `dpe.fcr`（`idle` bit[0]、`pause` bit[1]）在这里落地为（[csr.h:L468-L484](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr.h#L468-L484)）：

```c
#define CSR__DPE__FCR__IDLE_bp 0
#define CSR__DPE__FCR__PAUSE_bp 1
typedef union {
    struct __attribute__ ((__packed__)) {
        uint32_t idle :1;
        uint32_t pause :1;
        uint32_t :30;
    } f;
    uint32_t w;
} csr__dpe__fcr_t;
```

最外层 addrmap 用 `RESERVED_xxx` 填充数组空洞，保证结构体偏移与 RDL 地址完全一致（[csr.h:L927-L940](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr.h#L927-L940)），末尾的尺寸断言是「排布正确」的编译期保险（[csr.h:L952](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr.h#L952)）：

```c
typedef struct __attribute__ ((__packed__)) {
    csr__cpu_fifo_t  cpu_fifo;
    csr__uart_t      uart;
    ...
    uint8_t RESERVED_88_3ff[0x378];        // 0x88..0x3ff 留空
    csr__routing_table_t   routing_table;  // @0x0400
    uint8_t RESERVED_800_1fff[0x1800];     // 0x800..0x1fff 留空
    csr__cryptokey_table_t cryptokey_table;// @0x2000
} csr_t;
static_assert(sizeof(wireguard_t) == 0x20003e00, "Packing error");
```

#### 4.2.4 代码实践

**实践目标**：验证「C 结构体偏移 = RDL 地址」。

**操作步骤**：

1. 在 `csr.h` 里找到 `csr__cpu_fifo__rx__control_t`，记下 `tuser_dst`/`tuser_src`/`tlast`/`tkeep` 的 `_bp`。
2. 写一段 5 行的示例代码（**示例代码，非项目原有**），用这些 `_bp`/`_bm` 宏手工拼一个整字：假设要设 `tuser_dst=2, tuser_src=1, tlast=1, tkeep=0xFFFF`，算出 `.w` 应该是多少。
3. 对照 `tlast_bp=15`、`tkeep_bp=16` 核对你的手算结果。

**预期结果**：`.w = (2<<0) | (1<<3) | (1<<15) | (0xFFFF<<16) = 0xFFFF808A`。可见 `_bp` 宏就是给人/给工具算位移用的。

#### 4.2.5 小练习与答案

**练习**：`csr__cryptokey_table__entry_t` 里有 30 个字段（见 [csr.h:L888-L920](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr.h#L888-L920)），为什么每个字段都正好 32 位、没有挤位？

> **答**：因为这些字段是给 `external` 双口 RAM 用的（u3-l1 讲过的 routing/cryptokey 表），数据面按 32 位字查表，CPU 也按 32 位字写表。每字段独占一个 32 位字，读写都不需要 read-modify-write，简化了硬件与 HAL 的字节使能逻辑——这正是 4.4 会看到的「对齐避免 RMW」设计。

---

### 4.3 sysrdl_cosim.py + regblock：RTL 与 HAL 外壳的生成

#### 4.3.1 概念说明

`csr.h` 只有「数据结构」，没有「动作」。要变成能用的 HAL，还得套一层 C++ 类：每个寄存器是一个 `xxx_vp_t` 类，提供 `field(data)` 写、`field()` 读的成员函数；regfile 是装着若干寄存器指针的类；层层向上直到顶层 `csr_vp_t`。这层外壳由本项目自研的 [sysrdl_cosim.py](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/sysrdl_cosim.py) 生成。

它不调用 peakrdl 命令行，而是**直接用 `systemrdl-compiler` 这个 Python 库**：`RDLCompiler` 编译 RDL → `elaborate()` 展开成节点树 → `RDLWalker` 遍历树 → 自定义 `Listener` 在 `enter_Component`/`enter_Field`/`exit_Component` 回调里打印 C++ 代码。这套路数就是经典的「语法树访问者模式」。

同一份节点树，脚本根据是否传 `-c`（`--cosim`）走两条分支，产出两个行为不同、结构相同的 HAL——这正是「一份遍历、两种后端」。

#### 4.3.2 核心流程

硬件 HAL（`csr_hw.h`，不带 `-c`）的寄存器类极其简单：构造时把传入的地址 cast 成 `csr__..._t*` 指针，读写直接解引用结构体字段：

```
field(data)  →  reg->f.field = data;     // CPU 总线硬件做字节使能
field()      →  return reg->f.field;
```

协同仿真 HAL（`csr_cosim.h`，带 `-c`）则把每次读写翻译成 VProc 的 API 调用，并在每次事务后插一个随机延迟来模拟 CPU 处理时间：

```
field(data)  →  VWriteBE(reg, data<<bp, be, ...);  VTick(rand()%33, ...);
field()      →  VRead(reg, &rdata, ...);  VTick(...);  return (rdata & bm)>>bp;
```

脚本还会判断字段是否字节/半字/字对齐：对齐的用 `VWriteBE`（带字节使能，一次写完），不对齐的退化为 read-modify-write（先读整字、改字段、再写回）。判据是字段的 `lsb`/`msb` 是否落在 8/16/32 位边界上。

#### 4.3.3 源码精读

顶层 `main` 用 `systemrdl-compiler` 把 RDL 变成节点树再遍历（[sysrdl_cosim.py:L450-L467](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/sysrdl_cosim.py#L450-L467)）：

```python
rdlc = RDLCompiler()
rdlc.compile_file(rdl_file)
root = rdlc.elaborate()
walker   = RDLWalker(unroll=False)
listener = Listener(outFile, cosim, delay+1, clkperiod, vpnode)
walker.walk(root, listener)
```

硬件分支的寄存器类生成在 `__process_target`：构造函数 cast 指针，每个字段生成一对读写内联函数（[sysrdl_cosim.py:L130-L160](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/sysrdl_cosim.py#L130-L160)）：

```python
print("    " + vp_type + " (uint32_t* reg_addr = 0) : reg((" + cast_type + ")reg_addr) {};\n")
...
print("    inline void     " + field_name + "(const " + base_type + " data) {reg->f." + field_name + " = data;};")
print("    inline "+ base_type + " " + field_name + "()                    {return reg->f." + field_name + ";}")
```

产出的硬件 HAL 寄存器类（这里 `sysrdl_cosim.py` 生成，看产物 [csr_hw.h:L42-L56](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_hw.h#L42-L56)）：

```cpp
class csr__cpu_fifo__rx__data_31_0_vp_t {
public:
    csr__cpu_fifo__rx__data_31_0_vp_t (uint32_t* reg_addr = 0)
        : reg((csr__cpu_fifo__rx__data_31_0_t*)reg_addr) {};
    inline void     full(const uint32_t data) {reg->w = data;};
    inline uint32_t full()                    {return reg->w;};
    inline void     tdata(const uint32_t data){reg->f.tdata = data;};
    inline uint32_t tdata()                   {return reg->f.tdata;};
private:
    csr__cpu_fifo__rx__data_31_0_t* reg;
};
```

协同仿真分支的字节使能/RMW 判定逻辑在 `__process_cosim`（[sysrdl_cosim.py:L221-L237](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/sysrdl_cosim.py#L221-L237)）：

```python
if ((msb+1 - lsb) <= 8)    and lsb%8  == 0 :
  be = 0x1 << ((lsb%32) >> 3)         # 字节对齐
elif ((msb+1 - lsb) <= 16) and lsb%16 == 0 :
  be = 0x3 << ((lsb%32) >> 3)         # 半字对齐
elif ((msb+1 - lsb) <= 32) and lsb%32 == 0 :
  be = 0xf                            # 整字对齐
else :
  rmw = True                          # 不对齐 → 读改写
```

regfile/addrmap 层的类在 `exit_Component` 里生成：构造函数按 `child_addr/4` 算出每个子寄存器的字地址偏移，`new` 出子对象挂到成员指针上。顶层 `csr_vp_t` 给出默认基址 `0x20000000`（即 CSR 段在地址映射里的起点，见 [csr_hw.h 末尾的 csr_vp_t](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_hw.h#L1373)）：

```cpp
class csr_vp_t {
public:
    csr_vp_t(uint32_t* base_addr = (uint32_t*)0x20000000) {
        cpu_fifo     = new csr__cpu_fifo_vp_t     (base_addr + 0x0/4);
        dpe          = new csr__dpe_vp_t          (base_addr + 0x84/4);
        routing_table= new csr__routing_table_vp_t(base_addr + 0x400/4);
        cryptokey_table = new csr__cryptokey_table_vp_t(base_addr + 0x2000/4);
        ...
    };
    csr__cpu_fifo_vp_t*  cpu_fifo;
    csr__dpe_vp_t*       dpe;
    ...
};
```

于是软件就能写 `csr->dpe->fcr->pause(1)`：`csr` 解到 `dpe` 指针（基址+0x84），再解到 `fcr` 指针（+0x0），最终写 bit[1]。每一层 `->` 就是一次「加上固定偏移、取成员指针」。

至于 RTL 那一侧，`peakrdl regblock` 把同一份 RDL 编译成 `csr.sv`（寄存器读写逻辑 + 地址译码 + external stall 握手）和 `csr_pkg.sv`（结构化接口类型）。模块端口暴露 CPU 接口（`s_cpuif_*`）与硬件侧 `hwif_in`/`hwif_out` 结构体束（[csr.sv:L8-L27](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr.sv#L8-L27)）；`external` 表（routing/cryptokey）靠 stall 握手等待外部双口 RAM 应答（[csr.sv:L60-L86](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr.sv#L60-L86)）。`csr_pkg.sv` 里的 `csr__in_t`/`csr__out_t` 与 u2-l2 讲的 `to_csr`/`from_csr` hwif 束一一对应（[csr_pkg.sv:L195-L203](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_pkg.sv#L195-L203)）。

#### 4.3.4 代码实践

**实践目标**：追踪一句 `csr->dpe->fcr->pause(1)` 在硬件 HAL 里展开成什么。

**操作步骤**：

1. 在 [csr_hw.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_hw.h) 里找到 `csr_vp_t`（顶层）、`csr__dpe_vp_t`、`csr__dpe__fcr_vp_t` 三个类（`dpe.fcr` 在 [csr_hw.h:L610-L636](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_hw.h#L610-L636)）。
2. 手工内联展开 `csr->dpe->fcr->pause(1)`：三次 `->` 各加多少偏移？最终落到哪个地址、写哪一比特？
3. 用 `csr.h` 里 `CSR__DPE__FCR__PAUSE_bp`（=1）核对。

**预期结果**：`csr`(0x20000000) → `dpe`(+0x84 = 0x20000084) → `fcr`(+0x0 = 0x20000084) → 写 `reg->f.pause = 1`，即向地址 `0x20000084` 写入 `0x2`（bit[1]）。

#### 4.3.5 小练习与答案

**练习 1**：为什么硬件 HAL（`csr_hw.h`）的 `pause(data)` 直接写 `reg->f.pause = data`，而协同仿真 HAL（`csr_cosim.h`）却要先算 `be` 字节使能？

> **答**：硬件目标上，CPU 的总线控制器（u2-l4 的 `soc_fabric`）会自动把「写一个结构体字段」翻译成带字节使能的总线事务，RTL 的 `csr.sv` 也接收 `s_cpuif_wr_biten`，所以 HAL 只管写字段。协同仿真没有真实总线控制器，VProc 的 `VWrite` 只能整字写，要写子字段就得用 `VWriteBE(addr, data, be)` 显式给字节使能（或退化为读改写）。

**练习 2**：`sysrdl_cosim.py` 里 `RDLWalker(unroll=False)` 的 `unroll=False` 是什么意思？对生成的 HAL 有什么影响？

> **答**：`unroll=False` 表示遍历时**不展开数组**，即遇到 `entry[64]` 不会访问 64 次，而是把它当成一个数组节点处理。脚本的 `exit_Component` 检测到 `child.is_array` 后，自己生成一个 `for(idx=0; idx<64; idx++)` 循环来 new 64 个子对象（见 [sysrdl_cosim.py:L354-L374](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/sysrdl_cosim.py#L354-L374)），这样生成的代码更紧凑。

---

### 4.4 VPROC 宏切换：一份应用代码，两个运行环境

#### 4.4.1 概念说明

到此我们有两套 HAL：`csr_hw.h`（上板，直接内存映射）和 `csr_cosim.h`（仿真，走 VProc API）。两者的**类名、成员、层级完全相同**——`csr_vp_t`/`csr__dpe_vp_t`/`csr__dpe__fcr_vp_t` 一模一样——只有最底层的读写方法实现不同。这意味着应用代码 `main.cpp` 一字不改就能在两个环境编译运行。

切换的「扳手」就是编译期宏 `VPROC`：定义了就走协同仿真，没定义就走硬件。这个选择写在一个极短的总线头 [wireguard_regs.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/wireguard_regs.h) 里，应用代码只需 `#include "wireguard_regs.h"`，不必关心当前在哪种环境。

#### 4.4.2 核心流程

```
应用 main.cpp
   │
   │  #include "wireguard_regs.h"
   ▼
wireguard_regs.h
   ├── #ifdef VPROC  →  #include "csr_cosim.h"   （仿真构建，如 make BUILD=ISS / VProc）
   └── #else         →  #include "csr_hw.h"      （上板构建，MakefileSW）
```

两个 HAL 在头文件里还各自定义了入口宏 `WGMAIN`：硬件版是 `main`（裸机标准入口），协同仿真版是 `VUserMain0`（VProc 的 node 0 入口，见 u7-l2）。这样连「程序从哪里开始执行」都由同一个宏统一切换。

协同仿真 HAL 还多定义了两个常量：`SOC_CPU_VPNODE`（VProc 节点号，默认 0）和 `SOC_CPU_CLK_PERIOD_PS`（时钟周期，用于把「软件延时」换算成仿真 tick）。默认周期在 `csr_cosim.h` 里是 `18518`ps（约 54MHz），可由 `sysrdl_cosim.py -C` 覆盖。

#### 4.4.3 源码精读

切换逻辑短小精悍（[wireguard_regs.h:L13-L19](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/wireguard_regs.h#L13-L19)）：

```c
# ifdef VPROC
#  include "csr_cosim.h"
# else
#  include "csr_hw.h"
# endif
```

两个 HAL 的头部差异，看 `__gen_header` 的分支即可（[sysrdl_cosim.py:L71-L87](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/sysrdl_cosim.py#L71-L87)）：

```python
if self.cosim :
  print("#include \"VUser.h\"")
  print("#define WGMAIN                  VUserMain0")
  print("#define SOC_CPU_VPNODE          0")
  print("#define SOC_CPU_CLK_PERIOD_PS   18518")
else :
  print("#define WGMAIN          main")
```

应用侧的真实用法：`main.cpp` 用 `new csr_vp_t()` 创建顶层对象（用默认基址 `0x20000000`），随后 `csr->hw_id->VENDOR()` 这种调用在两种环境下都能编译运行（[main.cpp:L785-L790](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L785-L790)）：

```cpp
int main(void) {
   volatile csr_vp_t* csr = new csr_vp_t();
   ...
   if (csr->hw_id->VENDOR() != 0xCCBA ||
       csr->hw_id->PRODUCT() != 0xCACA) {
      uart_send(csr, "\r\nHardware ID mismatch! Halting...\r\n");
```

注意 `int main(void)` 这个函数名在协同仿真构建里会被 `WGMAIN` 宏改写成 `VUserMain0`（如果 `main.cpp` 用 `WGMAIN` 而非硬编码 `main` 的话）——这也是 `sysrdl_cosim.py` 生成 `WGMAIN` 宏的用意：让入口名随环境变。u1-l5 讲的开机 `0xCCBA`/`0xCACA` 硬件 ID 校验，正是通过这套 HAL 读 `csr.h` 里 `CSR__HW_ID__VENDOR_reset = 0xccba` 对应的硬件寄存器。

#### 4.4.4 代码实践

**实践目标**：用「双环境同代码」的视角审视一份真实应用函数。

**操作步骤**：

1. 打开 [main.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp)，找到 `init_network`（[L232](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L232)）和 `config_routes` 里更新路由表的那段（[L395-L460](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L395)）。
2. 找出所有 `csr->...` 调用，标注每一句在硬件 HAL 里展开成「直接内存写」、在协同仿真 HAL 里展开成「VProc 事务 + 随机延时」。
3. 思考：`while (!csr->dpe->fcr->idle());` 这句轮询，在两种环境下的「时间语义」有何不同？

**预期结果**：硬件上 `idle()` 是一条 load 指令、几纳秒；协同仿真上每次 `idle()` 调用都触发一次 `VRead` + `VTick(rand()%33)`，相当于每次轮询随机消耗若干时钟周期，从而真实模拟「CPU 不是每拍都在查寄存器」。这正是 u3-l4 FCR 原子握手在仿真里也能暴露时序问题的原因。**待本地验证**（需搭建 u7 的 VProc 仿真环境对比）。

#### 4.4.5 小练习与答案

**练习 1**：如果在 `MakefileSW` 里给编译器加了 `-DVPROC`，链接时会期待入口符号是 `main` 还是 `VUserMain0`？

> **答**：期待 `VUserMain0`。因为 `csr_cosim.h` 把 `WGMAIN` 定义成 `VUserMain0`，凡用 `WGMAIN` 命名的入口都会被改名。`main.cpp` 当前硬编码了 `int main`，所以在 VProc 构建里实际入口由 VProc 框架的 `VUserMain0.cpp` 承担（u7-l2 会讲），`main.cpp` 的 `main` 在那种构建下不被直接调用——这是本项目当前的一个衔接细节。

**练习 2**：为什么 `SOC_CPU_CLK_PERIOD_PS` 默认是 `18518` 而不是 `12500`？

> **答**：`18518`ps ≈ 54MHz。README 文档里写的默认是 `12500`（80MHz），但 `sysrdl_cosim.py` 的命令行 `argparse` 默认值是 `18518`（见 [sysrdl_cosim.py:L427](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/sysrdl_cosim.py#L427)），生成的 `csr_cosim.h` 也确实是 `18518`。这里文档与代码存在一处轻微不一致——以实际生成的头文件为准，可用 `-C 12500` 覆盖。这种「文档说 80MHz、代码默认 54MHz」的小出入，正是读自动生成项目时要注意核对的点。

---

## 5. 综合实践

**任务**：扮演一次「寄存器修改」的完整生命周期，体会单一真源的威力。

假设你要给 `gpio` 增加一个新字段 `led3`（bit[10]），完成以下全链路追踪：

1. **改真源**：在 [csr.rdl](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl) 的 `gpio` reg 里加一行 `led3 \`{sw=rw; hw=r\} = 0;`，放在 `led2` 之后。（本练习只读不改，请在心里或副本上完成。）
2. **预测产物变化**：重新 `make -f MakefileCSR` 后，逐一预测这六个文件各会多出什么：
   - `csr.h`：多一个 `CSR__GPIO__LED3_bm/bp/bw` 宏 + `gpio_t` 联合体里多一个 `led3 :1` 字段（注意 `led2` 在 bit[9]，`led3` 应在 bit[10]，中间无保留位）。
   - `csr_pkg.sv`：`csr__gpio__out_t` 多一个 `led3` 子结构（hw=r → 归到 `out_t` 即硬件输出给 CSR 读）。
   - `csr.sv`：地址译码与读写逻辑自动多覆盖该字段。
   - `csr_hw.h` / `csr_cosim.h`：`csr__gpio_vp_t` 多一对 `led3(data)`/`led3()` 方法。
   - `wireguard.md`：文档表格多一行 `led3`。
3. **验证应用侧**：现在你可以在 `main.cpp` 里写 `csr->gpio->led3(1);` 来点灯了——这行代码在硬件和仿真两种构建下都能编译。
4. **反思**：如果不用单一真源，而是让硬件工程师手写 RTL、软件工程师手写 HAL，新增一个字段要协调几个文件、几处可能对不上？

**预期结果**：你能清楚说出「改一处 `csr.rdl`，六个产物自动一致更新，应用层零成本获得 `led3()` 方法」。这就是本讲要传达的核心：PeakRDL 把「软硬件寄存器协议同步」这件容易出错的人工活，变成了确定性的代码生成。**待本地验证**（需安装 peakrdl 实际跑一遍 `make -f MakefileCSR`）。

## 6. 本讲小结

- **一条管线、一份真源**：`csr.rdl` 经 `sed` 过滤 → 喂给 `peakrdl c-header`（出 `csr.h`）、`peakrdl regblock`（出 `csr.sv`/`csr_pkg.sv`）、`sysrdl_cosim.py`（出两个 HAL），再加 `html`/`markdown` 文档副产品。
- **`csr.h` 是地基**：它用 `_bm`/`_bp`/`_bw` 宏 + `union{struct f; w;}` 把每个寄存器/字段铺成与 RDL 地址逐字节对齐的 C 结构体，并用 `static_assert` 在编译期校验总尺寸。
- **HAL 是 `csr.h` 外面的 C++ 类壳**：`sysrdl_cosim.py` 用 `systemrdl-compiler` 遍历节点树，为每层生成 `xxx_vp_t` 类，成员是指向下一层的指针加固定地址偏移，于是 `csr->dpe->fcr->pause(1)` 这种链式调用得以成立。
- **两个后端、同一结构**：硬件 HAL 直接解引用结构体（CPU 总线做字节使能），协同仿真 HAL 调 VProc 的 `VWriteBE`/`VRead` + 随机 `VTick`，并对非对齐字段退化为读改写；两者类名层级完全一致。
- **`VPROC` 宏是切换扳手**：`wireguard_regs.h` 在编译期按 `VPROC` 是否定义选 `csr_cosim.h` 或 `csr_hw.h`，连入口名（`WGMAIN`）和时钟周期常量都一并切，使 `main.cpp` 一份代码两环境通用。
- **生成文件禁止手改**：除 `csr.rdl`、`MakefileCSR`、`sysrdl_cosim.py`、`wireguard_regs.h` 外，`generated-files/` 里的内容都是 `*** DO NOT EDIT! ***` 的产物；改寄存器只改 `csr.rdl` 后重 `make`。

## 7. 下一步学习建议

本讲把「CSR 怎么生成」讲透了，但还没讲「CSR 怎么用」。建议按这条线继续：

1. **u3-l3（CPU FIFO：AXIS 到 CSR 的映射）**：看 HAL 在实战中如何把一个 128 位 AXIS 数据包拆成多次 32 位 CSR 读写发出去——这是 HAL API 最密集的使用场景，能巩固本讲的层级指针心智模型。
2. **u3-l4（FCR 流控寄存器与原子更新）**：本讲多次出现的 `csr->dpe->fcr->pause(1)` / `while(!idle())` 将在 u3-l4 串成完整的「pause → idle → 改表 → 恢复」原子握手时序，并解释为何不能用 AXI stall 做暂停。
3. **u7-l2（VProc 协同仿真）**：本讲的 `csr_cosim.h` 与 `VWrite`/`VRead`/`VTick` 是 VProc 的 API，u7-l2 会从仿真测试台角度讲清这些调用如何驱动 `soc_if` 总线，把「软件 HAL」和「仿真波形」对上号。
4. **想深入生成器本身**：读 [sysrdl_cosim.py](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/sysrdl_cosim.py) 的 `Listener` 三个回调（`enter_Component`/`enter_Field`/`exit_Component`），对照 `systemrdl-compiler` 文档，试着理解访问者模式如何把一棵 RDL 树「打印」成 C++ 类——这是把本讲从「会用」推进到「能改生成器」的关键一步。
