# 厂商选择与可移植机制

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 PoC 实现「一份源码、多厂商可移植」的分层结构：一个**通用包装实体**对外提供统一接口，内部用**厂商专用子实体**对接 Xilinx / Altera 等底层原语。
- 读懂 `if ... generate` 语句如何依据 `DEVICE_INFO.Vendor` 在通用实现与厂商实现之间做**展开期（elaboration）选择**。
- 区分**两层选择**：pyIPCMI 在 `.files` 里做的**编译时**选择（决定编译哪些文件、引入哪些原语库），与 VHDL `generate` 在 elaboration 时做的**分支选择**，并理解二者为什么必须一致。
- 识别 `ASYNC_REG`、`SHREG_EXTRACT`、`RLOC`、`PRESERVE`、`ALTERA_ATTRIBUTE` 等综合属性在同步器里各自的作用。
- 在 `sync_Bits.vhdl` 中定位厂商选择逻辑，画出给定器件（例如 Generic）落入哪条分支的流程图。

## 2. 前置知识

本讲承接 [u2-l3 配置机制](u2-l3-config-mechanism.md) 与 [u3-l1 命名空间包模式](u3-l1-namespace-package-pattern.md)，默认你已经知道：

- **`MY_DEVICE` / `MY_BOARD`**：用户在 `my_config.vhdl` 里填写的目标硬件常量。
- **`config` 包**：把器件字符串解析成 `T_DEVICE_INFO` 记录，其中 `Vendor` 字段是 `T_VENDOR` 枚举（`VENDOR_ALTERA` / `VENDOR_XILINX` / `VENDOR_LATTICE` / `VENDOR_GENERIC` 等）。
- **`generate` 语句**：VHDL 在 elaboration 期根据常量条件「长出」或「不长出」某段硬件，等价于编译期的 `if`。
- **命名空间包 `<ns>.pkg.vhdl`**：集中声明该命名空间所有核的 component，供别处实例化。

还需要一点硬件背景：**时钟域穿越（CDC, Clock Domain Crossing）**。当一根信号从异步的源时钟域进入本模块的时钟域时，直接采样可能命中触发器的**亚稳态（metastability）**——输出在一段时间内既不是 0 也不是 1。标准对策是串联两级（或更多）D 触发器，给信号一个时钟周期去「稳定」，这就是本讲反复出现的「2 D-FF 同步器」。同步深度越深，亚稳态传播到后级的概率越小，其可靠性常用平均故障间隔时间 MTBF 衡量，链级数 \(n\) 与 MTBF 大致呈指数关系。

> 提示：本讲的「厂商选择」是一个**组织代码的手段**，本身不依赖具体同步器原理。即使你暂时不深究 CDC，也能看懂「包装实体 + 子实体 + generate 分发」这套模式。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/common/config.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl) | 解析 `MY_DEVICE`，产出 `T_VENDOR` 枚举与 `T_DEVICE_INFO` 记录——厂商选择的「数据来源」。 |
| [src/misc/sync/sync_Bits.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl) | **通用包装实体**：对外统一接口，内部用 `generate` 三选一。 |
| [src/misc/sync/sync_Bits_Xilinx.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits_Xilinx.vhdl) | Xilinx 专用子实体，实例化 UniSim 的 `FD` 原语并加布局约束。 |
| [src/misc/sync/sync_Bits_Altera.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits_Altera.vhdl) | Altera 专用子实体，用 `ALTERA_ATTRIBUTE` 通知 Quartus 这是同步器 FF。 |
| [src/misc/sync/sync.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync.pkg.vhdl) | `sync` 命名空间包，集中声明 `sync_Bits` / `sync_Bits_Altera` / `sync_Bits_Xilinx` 三个 component。 |
| [src/misc/sync/sync_Bits.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.files) | pyIPCMI 编译清单：**编译时**按 `DeviceVendor` 选择编译哪个子实体、引入哪个原语库。 |
| [src/mem/ocram/ocram_sp.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl) | 第二个例子：片上 RAM 的厂商选择，展示「推断 vs. 直接实例化原语」的另一种取舍。 |
| [src/mem/ocram/altera/ocram_sp_altera.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/altera/ocram_sp_altera.vhdl) | Altera 专用：直接实例化 `altsyncram` 原语。 |

---

## 4. 核心概念与源码讲解

### 4.1 通用包装实体：sync_Bits 的对外契约

#### 4.1.1 概念说明

PoC 的可移植性建立在一条简单约定上：**使用者永远只看到一个实体**。无论目标器件是 Xilinx、Altera 还是未知厂商，使用者例化的都是同一个 `sync_Bits`，它的 generic 与 port 完全不变。厂商差异被藏在 `architecture rtl` 内部。

这个对外实体叫做**通用包装实体（wrapper entity）**。它的职责只有两个：

1. 提供稳定、厂商无关的接口（generic + port）。
2. 根据当前器件的 `Vendor`，把实现工作「分发」给对应的子实体或就地用通用 RTL 实现。

这样带来的好处是：调用方代码（例如某个 FIFO 内部需要同步几个标志位）**一次写好、到处综合**，不需要为每家厂商写一份例化代码。

#### 4.1.2 核心流程

包装实体的运行流程可以概括为三步：

1. **解析器件**：在架构说明区把全局 `DEVICE_INFO()` 的结果缓存进一个常量 `DEV_INFO`，避免每次比较都重复解析字符串。
2. **求值分支**：用 `if ... generate` 写出若干互斥分支，每个分支的守卫条件都形如 `DEV_INFO.Vendor = VENDOR_某厂商`。
3. **分发实现**：命中的分支要么就地展开一段通用 RTL，要么实例化对应的厂商专用子实体。

由于 `generate` 的条件是**常量**（在 elaboration 期就确定），综合后只有一条分支真正「长出」硬件，其余分支完全消失，不会带来任何面积或路径开销。

#### 4.1.3 源码精读

先看包装实体的对外接口——三个 generic、三个 port，厂商无关：

[src/misc/sync/sync_Bits.vhdl:68-79](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L68-L79) —— `sync_Bits` 的 entity 声明：`BITS` 指定要同步多少位、`INIT` 指定复位值、`SYNC_DEPTH`（类型 `T_MISC_SYNC_DEPTH`，即 `integer range 2 to 16`）指定同步器级数。

接着看架构开头如何缓存器件信息：

[src/misc/sync/sync_Bits.vhdl:82-85](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L82-L85) —— 定义常量 `DEV_INFO : T_DEVICE_INFO := DEVICE_INFO;`。注意 `DEVICE_INFO` 是 `config` 包里的无参函数（默认参数即「用 `MY_DEVICE`」），这里调用一次、把结果固化为常量，后续三个 `generate` 都复用它。

> 旁注：另一个核 `ocram_sp` 用的是直接写 `VENDOR = VENDOR_ALTERA`（见 4.4）。那其实是调用函数 `VENDOR()`（默认参数同上）再比较，语义等价，但每次比较都会重新解析字符串。`sync_Bits` 的 `DEV_INFO` 常量缓存更经济，且能一次拿到 `Vendor`/`Device`/`LUT_FanIn` 等全部字段。

至于 `T_MISC_SYNC_DEPTH` 与三个 component 的声明，都集中在命名空间包里：

[src/misc/sync/sync.pkg.vhdl:38-77](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync.pkg.vhdl#L38-L77) —— `subtype T_MISC_SYNC_DEPTH` 把深度约束在 2..16；并声明了 `sync_Bits`、`sync_Bits_Altera`、`sync_Bits_Xilinx` 三个 component，三者 generic/port 形状一致（这正是「包装实体可以无缝替换实现」的前提）。

#### 4.1.4 代码实践

**实践目标**：确认包装实体的接口对厂商完全透明。

**操作步骤**：

1. 打开 [src/misc/sync/sync_Bits.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl) 与 [src/misc/sync/sync_Bits_Xilinx.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits_Xilinx.vhdl)。
2. 把 `sync_Bits` 与 `sync_Bits_Xilinx` 两个 entity 的 generic / port 逐项对照。
3. 在仓库里 `Grep` 搜索 `sync_Bits` 的实际调用点（例如在 `fifo_ic_got` 这类跨钟 FIFO 里），观察调用方是否出现任何厂商判断。

**需要观察的现象**：两个 entity 的 `BITS` / `INIT` / `SYNC_DEPTH` 与 `Clock` / `Input` / `Output` 一一对应、类型相同；调用方只写 `entity PoC.sync_Bits`，没有任何 `VENDOR` 判断。

**预期结果**：调用方代码完全不感知厂商——可移植性的「对外统一」在此兑现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DEV_INFO` 要声明成 `constant`，而不是在每个 `generate` 条件里直接写 `DEVICE_INFO.Vendor`？

> **参考答案**：`DEVICE_INFO` 是函数，每次引用都会重新解析器件字符串、重建整个记录。声明成 `constant` 只解析一次，既减少 elaboration 期开销，也保证三个分支读到的是同一个快照，避免重复求值在极端情况下给出不一致结果。

**练习 2**：如果某天新增了 `VENDOR_MICROSEMI`，调用 `sync_Bits` 的旧代码需要改动吗？

> **参考答案**：不需要。调用方只认 `sync_Bits` 这层接口；新增厂商只需要在 `sync_Bits` 内部新增一个 `generate` 分支（或复用通用分支），对调用方完全透明——这正是分层包装的价值。

---

### 4.2 厂商专用子实体：Xilinx 与 Altera 的优化实现

#### 4.2.1 概念说明

通用 RTL 同步器（一串 D 触发器）在所有器件上都能综合，但它**没有告诉综合工具「这是同步器」**。厂商工具因此可能做两类「热心过头」的优化，反而破坏可靠性：

- 把两级 FF 折叠成查找表 + 单 FF，或塞进 SRL（移位寄存器查找表），改变甚至取消了同步链。
- 把两级同步 FF 布局到相距很远的 slice，增大线延迟、恶化 MTBF。

厂商专用子实体就是为了解决这两点：要么直接实例化厂商原语（拿到最优化的硬件资源），要么用厂商属性**明确标注意图**，让工具「别乱动、并优化布局」。

#### 4.2.2 核心流程

子实体对外与包装实体接口一致，内部却走各自厂商的工具链：

- **Xilinx 路线（`sync_Bits_Xilinx` → `sync_Bit_Xilinx`）**：每位实例化 UniSim 库的 `FD` 原语（即直接点名 Xilinx 的底层 D 触发器），并用 `ASYNC_REG` / `SHREG_EXTRACT` / `RLOC` 三个属性控制综合与布局。
- **Altera 路线（`sync_Bits_Altera`）**：不实例化具体原语，而是写普通 D-FF 进程，再用 `PRESERVE` 防止优化、用 `ALTERA_ATTRIBUTE` 注入一条 SDC 约束，告知 Quartus / TimeQuest「这是同步器 FF，并对进入它的路径做 false path」。

#### 4.2.3 源码精读

**Xilinx：多 bit 拆成单 bit，再实例化原语。**

[src/misc/sync/sync_Bits_Xilinx.vhdl:106-121](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits_Xilinx.vhdl#L106-L121) —— `sync_Bits_Xilinx` 的架构用 `for i in 0 to BITS-1 generate` 为每一位实例化 `sync_Bit_Xilinx`，把多 bit 问题规约成单 bit。

[src/misc/sync/sync_Bits_Xilinx.vhdl:124-170](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits_Xilinx.vhdl#L124-L170) —— `sync_Bit_Xilinx` 才是真正的 Xilinx 实现：直接实例化两个 UniSim `FD` 原语（`FF1_METASTABILITY_FFS` 与 `FF2`）。注意三个属性：

- `ASYNC_REG of Data_meta`（[L134](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits_Xilinx.vhdl#L134)）：告诉 Vivado 该 FF 的输入是异步的，工具会自动放宽相关时序并优化布局。
- `SHREG_EXTRACT of Data_meta/Data_sync`（[L137-L138](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits_Xilinx.vhdl#L137-L138)）：禁止 XST/Vivado 把这些 FF 抽成 SRL 移位寄存器，保住「真实两级 FF」。
- `RLOC of Data_meta/Data_sync`（[L141-L142](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits_Xilinx.vhdl#L141-L142)）：把一对同步 FF 相对布局到同一 slice（`X0Y0`），最小化二者之间的布线延迟。

同时有一条硬性约束：

[src/misc/sync/sync_Bits_Xilinx.vhdl:145](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits_Xilinx.vhdl#L145) —— `assert (SYNC_DEPTH = 2) ... severity WARNING;`：Xilinx 原语版只支持 2 级，传更大的 `SYNC_DEPTH` 会告警。这与通用分支支持 2..16 形成对比——可移植的代价是某些厂商实现的功能子集。

**Altera：不实例化原语，改用属性注入约束。**

[src/misc/sync/sync_Bits_Altera.vhdl:61-91](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits_Altera.vhdl#L61-L91) —— 关键在属性：

- `ALTERA_ATTRIBUTE of rtl`（[L66](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits_Altera.vhdl#L66)）：把一条 SDC 语句 `set_false_path -to [get_registers {*|sync_Bits_Altera:*|\gen:*:Data_meta}]` 直接焊到架构上，让 TimeQuest 对通往 `Data_meta` 的路径做 false path——这正是亚稳态路径应得的处理。
- `PRESERVE of Data_meta/Data_sync`（[L74-L75](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits_Altera.vhdl#L74-L75)）：禁止 Quartus 优化掉这些寄存器。
- `ALTERA_ATTRIBUTE of Data_meta`（[L77](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits_Altera.vhdl#L77)）：`SYNCHRONIZER_IDENTIFICATION "FORCED IF ASYNCHRONOUS"`，明确告知综合器「这就是同步器」。

对比可见两种风格：Xilinx 倾向**直接实例化原语 + 布局属性**，Altera 倾向**写普通 RTL + 用属性注入 SDC**。两者目标一致——让工具正确识别并保护同步器。

#### 4.2.4 代码实践

**实践目标**：体会「`SYNC_DEPTH` 在不同厂商分支里的支持范围不同」。

**操作步骤**：

1. 打开 `sync_Bits.vhdl` 的通用分支 [L91-L115](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L91-L115)，确认 `Data_sync` 的深度随 `SYNC_DEPTH` 可变（合法范围 2..16）。
2. 打开 `sync_Bit_Xilinx` 的 `assert` [L145](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits_Xilinx.vhdl#L145)，确认 Xilinx 分支只接受 `SYNC_DEPTH = 2`。

**需要观察的现象 / 预期结果**：在 Generic 器件上把 `SYNC_DEPTH` 设为 4 能正常工作；若强行在 Xilinx 器件上设为 4，elaboration 期会打印 warning，但只综合出 2 个 `FD`（多出的深度被忽略）。「待本地验证」：若有 Vivado 环境，实例化 `sync_Bits` 并设 `MY_DEVICE` 为某 `XC7` 器件、`SYNC_DEPTH => 4`，观察综合日志中的告警字样。

#### 4.2.5 小练习与答案

**练习 1**：`SHREG_EXTRACT = "NO"` 解决什么问题？不加会怎样？

> **参考答案**：它阻止综合工具把一串寄存器「抽提」成基于 LUT 的移位寄存器（SRL）。对同步器而言，SRL 不是真正的硬触发器，无法保证亚稳态分辨时间，会破坏可靠性。

**练习 2**：Altera 分支没有实例化任何 `altsyncram` 之类的原语，为什么也算「厂商专用」？

> **参考答案**：因为它依赖 Altera 私有的 `ALTERA_ATTRIBUTE` 与 `PRESERVE` 属性，并嵌入了 Altera SDC 语法。这些在 Xilinx 工具里无法识别，所以它仍然是 Altera 专用，只是「专用」体现在属性而非原语实例化上。

---

### 4.3 generate 选择：从 DEVICE_INFO.Vendor 到三分支

#### 4.3.1 概念说明

「分发」本身由一段互斥的 `if ... generate` 完成。这是 VHDL-2008 起支持 `elsif`/`else` 的 `generate` 写法（在更早版本里等价于多个并列的 `if generate`，靠条件互斥保证只命中一条）。它的语义是：**elaboration 期对常量条件求值，只展开命中的一支**。

在 `sync_Bits` 里有三条分支：

- `genGeneric`：当厂商**既不是 Altera 也不是 Xilinx** 时命中（涵盖 `VENDOR_GENERIC`、`VENDOR_LATTICE`、`VENDOR_MICROSEMI`、`VENDOR_UNKNOWN`）。
- `genAltera`：`VENDOR_ALTERA` 命中，实例化 `sync_Bits_Altera`。
- `genXilinx`：`VENDOR_XILINX` 命中，实例化 `sync_Bits_Xilinx`。

#### 4.3.2 核心流程

设 `MY_DEVICE` 经 `config` 解析得到厂商枚举 `V`，则三分支求值如下：

| 分支 | 守卫条件 | V = GENERIC | V = ALTERA | V = XILINX |
| --- | --- | --- | --- | --- |
| `genGeneric` | `(V /= ALTERA) and (V /= XILINX)` | ✅ TRUE | ❌ FALSE | ❌ FALSE |
| `genAltera` | `V = ALTERA` | ❌ FALSE | ✅ TRUE | ❌ FALSE |
| `genXilinx` | `V = XILINX` | ❌ FALSE | ❌ FALSE | ✅ TRUE |

注意 `genGeneric` 用的是「非 Altera 且非 Xilinx」的否定式守卫，因此它会兜住所有未单独提供专用实现的厂商——这是一种**安全的默认兜底**：哪怕将来新增了 `VENDOR_某新厂`，只要还没写它的专用子实体，代码也会回落到通用 RTL 而不是直接报错。

> 这种「否定式兜底」与 `ocram_sp` 的写法不同（见 4.4）：`ocram_sp` 用的是**白名单 + 末尾 `assert failure`**，对未覆盖厂商**直接报错**。两种策略各有取舍，4.4 会对比。

#### 4.3.3 源码精读

[src/misc/sync/sync_Bits.vhdl:86-116](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L86-L116) —— `genGeneric` 分支：守卫 `(DEV_INFO.Vendor /= VENDOR_ALTERA) and (DEV_INFO.Vendor /= VENDOR_XILINX)`。命中时就地展开一段通用同步器：每位一组 `Data_async` / `Data_meta` / `Data_sync`，并在 `Data_meta` 上挂 `ASYNC_REG`、在 `Data_meta`/`Data_sync` 上挂 `SHREG_EXTRACT`（属性声明见 [L87-L101](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L87-L101)）——可见**通用分支也尽量标注了通用属性**，以求在「未知厂商」上仍获得合理行为。

[src/misc/sync/sync_Bits.vhdl:119-131](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L119-L131) —— `genAltera` 分支：实例化 `sync_Bits_Altera`，generic/port 一一映射。

[src/misc/sync/sync_Bits.vhdl:134-146](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L134-L146) —— `genXilinx` 分支：实例化 `sync_Bits_Xilinx`。

至于 `VENDOR` 枚举与 `VENDOR()` 函数本身（厂商选择的数据源头）：

[src/common/config.vhdl:390-397](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L390-L397) —— `T_VENDOR` 枚举：`VENDOR_UNKNOWN / GENERIC / ALTERA / LATTICE / MICROSEMI / XILINX`。

[src/common/config.vhdl:762-781](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L762-L781) —— `VENDOR()` 函数：取器件字符串前 2~3 字符做模式匹配——`"GE"→GENERIC`、`"EP"→ALTERA`、`"XC"→XILINX`、`"MPF"→MICROSEMI`、`"iCE"/"LCM"/"LFE"→LATTICE`。这是 `DEV_INFO.Vendor` 的最终来源。

[src/common/config.vhdl:520-531](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L520-L531) 与 [src/common/config.vhdl:1162-1176](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L1162-L1176) —— `T_DEVICE_INFO` 记录与 `DEVICE_INFO()` 聚合函数：把 `Vendor` / `Device` / `DevFamily` / `DevSeries` / `TransceiverType` / `LUT_FanIn` 等一次性算好，供核消费。

#### 4.3.4 代码实践（对应本讲主实践任务）

**实践目标**：在 `sync_Bits.vhdl` 中定位厂商选择逻辑，画出「一个 Generic 设备落入哪条分支」的流程图。

**操作步骤**：

1. 假设用户在 `my_config.vhdl` 设 `MY_BOARD = "Generic"`（或 `MY_DEVICE = "Generic"`）。
2. 在 `config.vhdl` 里追踪：`getLocalDeviceString`（[L657-L678](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L657-L678)）得到字符串 `"GENERIC"`。
3. 调 `VENDOR()`（[L762-L781](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L762-L781)）：前两字符 `"GE"` 命中 → 返回 `VENDOR_GENERIC`。
4. `DEVICE_INFO()` 汇总 → `DEV_INFO.Vendor = VENDOR_GENERIC`。
5. 回到 `sync_Bits.vhdl`，依次求值三条 `generate`。

**流程图**（用文本画出）：

```
MY_BOARD="Generic"  (或 MY_DEVICE="Generic")
        │
        ▼  config.vhdl: getLocalDeviceString()
   器件字符串 = "GENERIC"
        │
        ▼  VENDOR():  case VEN_STR2
   "GE"  ─────────────▶  VENDOR_GENERIC
        │
        ▼  DEVICE_INFO() 汇总
   DEV_INFO.Vendor = VENDOR_GENERIC
        │
        ▼  sync_Bits.vhdl 三个 if generate 求值
   ┌─ genGeneric : (GENERIC/=ALTERA) and (GENERIC/=XILINX)  → TRUE   ✅ 选中
   ├─ genAltera  : (GENERIC = ALTERA)                        → FALSE  跳过
   └─ genXilinx  : (GENERIC = XILINX)                        → FALSE  跳过
        │
        ▼
   就地展开通用 D-FF 同步器，不实例化任何厂商原语
```

**需要观察的现象 / 预期结果**：Generic 设备**只命中 `genGeneric`**，综合结果里不会出现 `sync_Bits_Xilinx` 或 `sync_Bits_Altera` 的实例。把 `MY_DEVICE` 换成 `XC7K325T...`（KC705）再画一遍，应当命中 `genXilinx`；换成 `EP4SGX230...`（DE4）则命中 `genAltera`。

> 「待本地验证」：若用 GHDL/NVC 仿真，可通过在 `config.vhdl` 开启 `MY_VERBOSE`（`POC_VERBOSE`）观察 `getLocalDeviceString` 打印的解析日志，确认你的器件字符串被识别成预期的 `Vendor`。

#### 4.3.5 小练习与答案

**练习 1**：如果 `MY_DEVICE` 写成 `"XC7A100T-1CG324C"`，三条分支各是什么结果？

> **参考答案**：前两字符 `"XC"` → `VENDOR_XILINX`。`genGeneric` 守卫 `(XILINX/=ALTERA) and (XILINX/=XILINX)` = `TRUE and FALSE` = FALSE；`genAltera` FALSE；`genXilinx` TRUE。命中 Xilinx 分支，实例化 `sync_Bits_Xilinx`。

**练习 2**：把 `genGeneric` 的守卫从「`/= ALTERA` and `/= XILINX`」改成「`= VENDOR_GENERIC`」，会有什么隐患？

> **参考答案**：那样 Lattice / Microsemi / 未知厂商将**一条分支都不命中**，同步器输出悬空，综合出错误电路。当前的否定式守卫确保「凡未单独适配的厂商都回落到通用实现」，更安全。

---

### 4.4 完整图景：双层选择模型与 ocram_sp 第二例

#### 4.4.1 概念说明

到目前为止我们只讲了 VHDL `generate` 这一层。但 PoC 的厂商选择其实是**两层**的：

1. **编译时（pyIPCMI / `.files`）**：pyIPCMI 读 `.files` 清单，根据它自己的变量 `DeviceVendor` 决定**编译哪些 `.vhdl` 文件**、**引入哪个原语库**（如 UniSim、altera_mf）。如果某厂商子实体的文件根本没被编译，包装实体里对应的 `generate` 分支就会引用一个不存在的实体 → elaboration 失败。
2. **展开期（VHDL `generate`）**：综合/仿真时，根据 `DEVICE_INFO.Vendor` 决定**实际长出哪条硬件**。

这两层必须**口径一致**：pyIPCMI 编译了 Xilinx 子实体 + UniSim 库，VHDL 层也判定为 `VENDOR_XILINX`，链路才通。PoC 用同一份 `MY_DEVICE` 同时驱动两层（pyIPCMI 把它解析成 `DeviceVendor`，VHDL 把它解析成 `T_VENDOR`），从而保证一致。

理解了双层模型，就能读懂 `ocram_sp` 这个**第二典型例子**，并对比它与 `sync_Bits` 的策略差异。

#### 4.4.2 核心流程

`ocram_sp`（单端口片上 RAM）的厂商选择同样分两层，但 RTL 层的策略与同步器不同：

- **通用分支 `gInfer`（GENERIC / LATTICE / XILINX）**：用普通数组 + 时钟进程**推断（infer）RAM**——相信综合器能把 `array ... <= ...` 翻译成 BlockRAM。
- **Altera 分支 `gAltera`**：Quartus 推断这种 RAM 不正确，于是**直接实例化** `ocram_sp_altera`（内部是 `altsyncram` 原语）。
- **末尾 `assert`**：覆盖厂商不在白名单时**直接 `severity failure`**，宁可报错也不静默生成错误硬件。

注意：Xilinx 在这里走**推断**（与 `sync_Bits` 走**原语实例化**不同）。是否需要厂商专用实现，取决于「综合器能不能正确推断」——能推断就推断（更可移植），不能推断就实例化原语（更可靠）。这是 PoC 在每类核上反复权衡的取舍。

#### 4.4.3 源码精读

**编译时那层：`.files` 清单。**

[src/misc/sync/sync_Bits.files:11-24](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.files#L11-L24) —— 先无条件编译命名空间包 `sync.pkg.vhdl`，再用 pyIPCMI 的 `if (DeviceVendor = "Altera")` / `elseif (DeviceVendor = "Xilinx")`：
- Altera：`include "lib/Altera.files"`（引入 altera_mf 等原语库）+ 编译 `sync_Bits_Altera.vhdl`。
- Xilinx：`include "lib/Xilinx.files"`（引入 UniSim）+ 编译 `sync_Bits_Xilinx.vhdl`，并按 `ToolChain` 准备 `.ucf`/`.xdc`（本仓库当前注释掉了）。
- 其他厂商：两个子实体文件**都不编译**。
- 最后**总是**编译 `sync_Bits.vhdl`（Top-Level）。

这解释了为什么 Generic 设备不会「找不到 `sync_Bits_Xilinx`」——因为 `genXilinx` 分支条件为假不会去引用它，而且该文件在编译时压根没参与编译，互不冲突。

**第二个例子：`ocram_sp` 的 RTL 分发。**

[src/mem/ocram/ocram_sp.vhdl:90-137](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl#L90-L137) —— `gInfer : if (VENDOR = VENDOR_GENERIC) or (VENDOR = VENDOR_LATTICE) or (VENDOR = VENDOR_XILINX) generate`：用 `ram_t` 数组与时钟进程推断 RAM（这里直接用 `VENDOR` 函数比较，未缓存成 `DEV_INFO`）。

[src/mem/ocram/ocram_sp.vhdl:139-172](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl#L139-L172) —— `gAltera : if VENDOR = VENDOR_ALTERA generate`：实例化 `ocram_sp_altera`。

[src/mem/ocram/ocram_sp.vhdl:174-176](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl#L174-L176) —— 末尾 `assert ... severity failure`：白名单之外直接报错（兜底策略 = 报错，而非回落）。

[src/mem/ocram/altera/ocram_sp_altera.vhdl:103-128](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/altera/ocram_sp_altera.vhdl#L103-L128) —— Altera 专用：直接实例化 `altsyncram` 原语，并用 `getAlteraDeviceName(DEVICE)` 把 PoC 的 `T_DEVICE` 转成 altera_mf 库要求的器件名字符串。

#### 4.4.4 代码实践

**实践目标**：验证「两层选择口径必须一致」。

**操作步骤**：

1. 读 [sync_Bits.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.files)，记录 pyIPCMI 在 `DeviceVendor = "Xilinx"` 时编译了哪些文件、引入了哪个原语库。
2. 假设「故意制造不一致」：让 pyIPCMI 的 `DeviceVendor = "Altera"`（只编译 `sync_Bits_Altera.vhdl`），但 VHDL 层 `MY_DEVICE` 解析为 `VENDOR_XILINX`。
3. 推断 `genXilinx` 分支会发生什么。

**需要观察的现象 / 预期结果**：`genXilinx` 条件为真，会尝试实例化 `sync_Bits_Xilinx`，但该实体文件未被 pyIPCMI 编译进 `PoC` 库 → elaboration 报「entity `sync_Bits_Xilinx` not found」错误。这反向证明两层必须由同一份 `MY_DEVICE` 驱动。「待本地验证」：在本地用 pyIPCMI 分别配 Altera / Xilinx 两次，观察编译进 `PoC` 库的 sync 子实体文件确实不同。

#### 4.4.5 小练习与答案

**练习 1**：`ocram_sp` 为什么对 Xilinx 用「推断」，而 `sync_Bits` 对 Xilinx 用「实例化原语」？

> **参考答案**：Xilinx 综合器能正确把数组进程推断成 BlockRAM，所以 `ocram_sp` 选择推断以获得更好的可移植性；但普通 RTL 同步器如果不加标注，工具可能把它优化成 SRL 或乱布局，所以 `sync_Bits` 选择实例化 `FD` 原语并加 `RLOC`/`ASYNC_REG` 来保证可靠性。取舍原则：能安全推断就推断，否则实例化原语。

**练习 2**：`ocram_sp` 末尾的 `assert ... severity failure` 与 `sync_Bits` 的否定式兜底，哪种更安全？

> **参考答案**：没有绝对优劣。`ocram_sp` 的 `failure` 兜底**尽早暴露**未支持厂商，避免静默生成错误 RAM 行为（写优先/读优先等），对功能正确性更保险；`sync_Bits` 的否定式兜底让**未支持厂商也能用通用同步器**，可用性更好。选择取决于「错误的代价」——RAM 行为错可能让整个系统数据损坏，宁可报错；通用同步器已足够安全，可以兜底。

---

## 5. 综合实践

把本讲知识串起来，做一次「全链路追踪」：

1. 选三个目标器件字符串：`"Generic"`、`"XC7K325T-2FFG900C"`（KC705）、`"EP4SGX230KF40C2"`（DE4）。
2. 对每一个，依次回答：
   - `VENDOR()` 返回什么？（查 [config.vhdl:762-781](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L762-L781)）
   - `sync_Bits.vhdl` 三条 `generate` 各命中哪条？
   - pyIPCMI 在 [sync_Bits.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.files) 里会编译哪个子实体、引入哪个原语库？
   - 若换成 `ocram_sp`（[src/mem/ocram/ocram_sp.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl)），Xilinx 那一栏走的是 `gInfer` 还是 `gAltera`？为什么和 `sync_Bits` 不同？
3. 把结果填进一张三行四列的表格。如果某一格你判断「会报错」，标注原因（例如未覆盖厂商触发 `assert failure`）。

完成表格后，你应当能用一句话解释 PoC 的可移植公式：**同一份 `MY_DEVICE` 同时驱动 pyIPCMI 编译期与 VHDL 展开期两层选择，包装实体对外不变、对内用 `generate` 分发到厂商子实体或通用兜底**。

## 6. 本讲小结

- PoC 的可移植性来自分层结构：**通用包装实体**（如 `sync_Bits`）提供厂商无关接口，内部用**厂商专用子实体**（`sync_Bits_Xilinx` / `sync_Bits_Altera`）对接原语。
- 选择发生在 VHDL **展开期**：`if ... generate` 依据 `DEV_INFO.Vendor` 只展开一条分支，其余分支零开销消失。
- `VENDOR` 枚举由 `config.vhdl` 解析器件字符串前缀得到（`"XC"→XILINX`、`"EP"→ALTERA`、`"GE"→GENERIC` 等），并汇入 `T_DEVICE_INFO` 记录。
- 厂商专用分支通过综合属性保护同步器：Xilinx 用 `ASYNC_REG` / `SHREG_EXTRACT` / `RLOC`（并实例化 `FD` 原语），Altera 用 `PRESERVE` / `ALTERA_ATTRIBUTE`（注入 SDC false path）。
- 选择实为**两层**：pyIPCMI 在 `.files` 用 `DeviceVendor` 做**编译时**选择（编译哪个文件、引入哪个原语库），VHDL `generate` 做**展开期**选择；两层由同一份 `MY_DEVICE` 驱动以保证一致。
- `ocram_sp` 提供第二例并展现不同策略：Xilinx 走「推断 RAM」、Altera 走「实例化 `altsyncram`」、未覆盖厂商用 `assert failure` 兜底——是否实例化原语取决于「综合器能否正确推断」。

## 7. 下一步学习建议

- 学 **[u3-l6 时钟域穿越：misc/sync](u3-l6-clock-domain-crossing.md)**：把本讲的 `sync_Bits` 放回同步器家族（`sync_Reset` / `sync_Pulse` / `sync_Strobe` / `sync_Vector`），理解不同信号类型为何需要不同同步器，以及 `_meta` / `_async` 信号需要哪些约束。
- 学 **[u3-l3 片上 RAM 抽象：ocram 家族](u3-l3-ocram-memory-abstraction.md)**：在 4.4 的 `ocram_sp` 基础上，继续看 `ocram_sdp` / `ocram_tdp` 等配置，以及厂商实例化的全貌。
- 学 **[u4-l5 板级约束与 FPGA 目标](u4-l5-board-constraints.md)**：把本讲提到的 `ASYNC_REG`、`set_false_path`、`ucf/MetaStability.ucf` 串到板级约束流程里看一遍。
- 想动手扩展：参考 **[u5-l6 扩展 PoC](u5-l6-extending-poc.md)**，尝试为某新厂商写一个 `sync_Bits_<Vendor>` 子实体，并在包装实体与 `.files` 里各加一条分支——这是检验你是否真懂双层选择模型的最好练习。
