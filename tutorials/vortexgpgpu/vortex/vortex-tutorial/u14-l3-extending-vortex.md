# 扩展 Vortex：自定义 ISA 扩展与开发指南

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「为 Vortex 新增一个自定义加速器」为什么是一次**四层协同改动**（config / kernel / SimX / RTL），而不是只改某一个文件夹。
- 按参数作用域为加速器的每一个入参选择**最便宜的传递机制**（DCR、lane-scatter pack、寄存器窗口、custom-LD），并避开本指南点名最贵的反模式。
- 在 SimX 里复用 `FuncUnit` CRTP 基类、`FUType` 枚举、`decode.cpp` 译码表与 `core.cpp` 接线，新增一个功能单元；理解 SFU 作为「子单元分派器」的复用方式。
- 在 RTL 里新增 `EX_*` localparam、在 `VX_execute.sv` 里挂接 `dispatch_if`/`commit_if`，并通过 `extensions.mk` 让配置开关决定源文件清单。
- 写出 kernel 侧的 `vx_*.h` 内联函数（RISC-V `.insn` + `vx_wgather`），并新增 `VX_CFG_EXT_*_ENABLE` 配置开关，同时守住 `sw/` ↔ `hw/`/`sim/` 双向隔离与 SimX↔RTL model_parity 两条铁律。

## 2. 前置知识

本讲是专家层收尾篇，默认你已经建立起下面这些心智模型（它们来自依赖讲义）：

- **Vortex 是全栈、双引擎**：SimX（C++ 仿真）与 RTL（Verilog）是同一架构的两套实现，必须保持功能与时序一致（model_parity，见 u7-l4）。任何扩展都要**两侧同步落地**，否则破坏 parity。
- **6 级流水线**：Schedule → Fetch → Decode → Issue → Execute → Commit。Execute 级是一组并列的功能单元（ALU/FPU/LSU/SFU/TCU），由 `FUType` 路由（见 u6-l4）。
- **SIMT 执行模型**：PC 是 warp 级、寄存器是 thread 级；一条 warp 指令的源/目的寄存器在物理上是「每线程一份」的向量（见 u1-l1、u4-l2）。
- **基数规则**：SimX 模块之间只通过 channel 通信，不跨所有权层级走后门（见 u5-l3）。
- **配置系统**：所有常量集中在 `VX_config.toml`，由 `gen_config.py` 分发；`_ENABLE` 是作者手写布尔、`_ENABLED` 是自动派生整数镜像（见 u2-l1、u2-l2）。
- **软硬边界**：`sw/{kernel,runtime}` 与 `sim/`+`hw/` 双向隔离，`sw/common/` 是唯一合法跨层通道（见 u2-l3）。

几个本讲反复用到、值得先认下的术语：

- **加速器（accelerator）/ 固定功能单元（fixed-function unit）**：一个专门做某类计算的硬件块——张量核、DMA/拷贝引擎、编解码、密码学、排序/扫描、光线追踪遍历器都算。
- **ISA 表面（ISA surface）/ kernel 可见接口**：warp 用来驱动加速器的那几条指令，以及它们的操作数约定。这是最难事后修改的部分，必须先设计。
- **宏指令（macro-op）/ 微操作（uop）**：一条需要多于 3 个源寄存器的指令，由 sequencer 展开成一串普通 uop，每个 uop 至多读 3 个寄存器（见 u6-l2、u6-l3）。
- **DCR（Device Control Register）**：主机在 launch 前写好的设备配置寄存器，对整次 launch 恒定。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [docs/designs/custom_accelerator_isa_extensions.md](docs/designs/custom_accelerator_isa_extensions.md) | 本讲的**方法论文档**：加速器 ISA 接口的全部设计模式与反模式。先读它再动数据通路。 |
| [docs/designs/simx_simulator_architecture.md](docs/designs/simx_simulator_architecture.md) | SimX v3 架构：功能语义与计时同居一处，是「为何扩展要双侧同步」的根因。 |
| [docs/coding_guidelines_cpp.md](docs/coding_guidelines_cpp.md) | C++ 编码规范，重点是 §8 的 `sw/` ↔ `hw/`/`sim/` 双向隔离。 |
| [docs/coding_guidelines_verilog.md](docs/coding_guidelines_verilog.md) | Verilog 编码规范：命名、`begin/end` 强制、接口 `valid/ready`、复用 `hw/rtl/libs`。 |
| [sim/simx/func_unit.h](sim/simx/func_unit.h) | SimX 功能单元 CRTP 基类 `FuncUnit<NUM_BLOCKS>` 与类型擦除基类 `FuncUnitBase`。 |
| [sim/simx/alu_unit.cpp](sim/simx/alu_unit.cpp) | 一个最简单的「纯 channel 延迟」功能单元范例：`execute()` + `on_tick()`。 |
| [sim/simx/types.h](sim/simx/types.h) | `FUType` 枚举（新增 FU 类型的登记处）。 |
| [sim/simx/decode.cpp](sim/simx/decode.cpp) | RISC-V 译码表：把 CUSTOM opcode 翻成 `FUType` + `op_type`。 |
| [sim/simx/core.cpp](sim/simx/core.cpp) | 流水线总装：实例化各 FU 并绑定 dispatcher / channel。 |
| [sim/simx/sfu_unit.cpp](sim/simx/sfu_unit.cpp) | SFU 作为「子单元分派器」的范例——新增加速器的另一条挂载路径。 |
| [sw/kernel/include/vx_dxa.h](sw/kernel/include/vx_dxa.h) | kernel 侧内联函数范例：`.insn r` 内联汇编 + `vx_wgather` 打包。 |
| [VX_config.toml](VX_config.toml) | 硬件配置单一真相来源，`VX_CFG_EXT_*_ENABLE` 开关在此登记。 |
| [hw/syn/extensions.mk](hw/syn/extensions.mk) | 配置开关 → RTL 源文件清单的桥（综合时的接线）。 |
| [hw/rtl/VX_gpu_pkg.sv](hw/rtl/VX_gpu_pkg.sv) | RTL 共享字典：`EX_*` 功能单元编号 localparam。 |
| [hw/rtl/core/VX_execute.sv](hw/rtl/core/VX_execute.sv) | RTL Execute 级：按 `EX_*` 索引实例化各单元。 |

> 说明：本讲用 **DXA**（异步拷贝/多播，u9-l2）和 **TCU**（张量核，u9-l1）这两个**已经 shipped 的扩展**作为贯穿示例——它们恰好分别演示了「挂在 SFU 下当子单元」与「新增独立 FUType」两条挂载路径。

---

## 4. 核心概念与源码讲解

### 4.1 扩展的全景：四层协同与两条铁律

#### 4.1.1 概念说明

新手最常踩的坑是：以为「加一个加速器」=「写一个 `.sv` 数据通路」。在 Vortex 里这是错的。一次扩展会**同时**碰四个层，少改一层都会导致编译不过、行为漂移或 parity 破裂：

| 层 | 改什么 | 代表文件 |
|---|---|---|
| **config** | 新增 `VX_CFG_EXT_*_ENABLE` 开关，让全树可条件编译 | `VX_config.toml` |
| **kernel** | 新增 `vx_*.h` 内联函数，让设备内核能发出新指令 | `sw/kernel/include/` |
| **SimX** | 新增功能单元 + 译码表 + 接线，给软件一个可跑的 oracle | `sim/simx/` |
| **RTL** | 新增 `.sv` 数据通路 + `EX_*` 编号 + 挂接，给硬件一个真身 | `hw/rtl/` |

贯穿这四层有**两条不可违反的铁律**：

1. **`sw/` ↔ `hw/`/`sim/` 双向隔离**（来自 `coding_guidelines_cpp.md` §8）：kernel 与 runtime 不能 `#include` 任何 `hw/*` 或 `sim/*`，反之亦然。四层之间的契约只能走 `sw/common/`、生成的 `VX_types.h`，或运行时查询（`vx_device_query`）。
2. **SimX↔RTL model_parity**（来自 u7-l4）：SimX 不只是功能 oracle，更是 RTL 的时序模型。任何新增单元必须两侧同步建模，绝不能放宽容差来吸收差异。

#### 4.1.2 核心流程

新增一个加速器的高阶流程（先 ISA、后通路、两侧同步）：

```text
1. 先定 ISA 表面（§4.2）           ← 最难改，最先做
   ├─ 给每个入参分类作用域（per-thread/warp/CTA/dispatch）
   └─ 按作用域选最便宜传递机制
2. config 层：登记 VX_CFG_EXT_*_ENABLE（§4.5）
3. kernel 层：写 vx_*.h 内联函数发指令（§4.5）
4. SimX 层：新增 FuncUnit + 译码 + 接线（§4.3）  ──┐
   └─ 让 SimX 先过测试（当 oracle）              │ 两侧同步
5. RTL 层：新增 .sv + EX_* + 挂接（§4.4）        ──┘
   └─ 用 model_parity 门控验证两侧一致
```

为什么 SimX 要先做？因为它是可断点插桩的 oracle（见 u13-l2 的 SimX-as-oracle 方法）。先把功能跑通在 SimX 上，再让 RTL 去对齐它，比反过来容易得多。

#### 4.1.3 源码精读

「功能语义与计时同居一处」是这一切的根因。设计文档把 v3 模型讲得很直白：

> ALU and FPU own private `execute()` methods; the SFU routes to its sub-units … there is **no central Emulator** … This makes SimX a faithful, module-by-module twin of the RTL, which is why it serves as the RTL oracle for cycle-parity debugging.

参见 [docs/designs/simx_simulator_architecture.md:L16-L30](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/simx_simulator_architecture.md#L16-L30)——这段说明了「为何扩展必须双侧同步」。

而 `sw/` ↔ `hw/`/`sim/` 双向隔离的明文规定在 C++ 规范里：

参见 [docs/coding_guidelines_cpp.md:L135-L158](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/coding_guidelines_cpp.md#L135-L158)，其中列出了四条被禁止的跨层 `#include`，并由 `ci/check_sw_sim_boundary.sh` 机械执行（见 u2-l3）。

#### 4.1.4 代码实践

**实践目标**：用 DXA 与 TCU 两个现成扩展，验证「四层协同」真实存在。

**操作步骤**：

1. 在 `VX_config.toml` 找到 `VX_CFG_EXT_DXA_ENABLE` 与 `VX_CFG_EXT_TCU_ENABLE`，确认它们是布尔开关（见 §4.5）。
2. 对每个扩展，分别在 config / kernel / SimX / RTL 四层各定位**一个**代表文件：
   - DXA：`VX_config.toml` → `sw/kernel/include/vx_dxa.h` → `sim/simx/dxa/dxa_unit.cpp` → `hw/rtl/dxa/`。
   - TCU：`VX_config.toml` → `sw/kernel/include/vx_tensor.h` → `sim/simx/tcu/tcu_unit.cpp` → `hw/rtl/tcu/`。
3. 在 `hw/syn/extensions.mk` 里确认这两个扩展的源文件是被 `VX_CFG_EXT_*_ENABLE` 宏条件加入的（见 §4.4）。

**需要观察的现象**：四个层各有一个文件、且都用同一个 `EXT_*_ENABLE` 宏门控——这就是「协同」的物理证据。

**预期结果**：你会看到同一个宏名字（如 `VX_CFG_EXT_DXA_ENABLE`）在四个不同目录里反复出现，串起整条扩展链。

#### 4.1.5 小练习与答案

**练习 1**：如果只在 RTL 里加了加速器、忘了改 SimX，`model_parity` 门控会怎样报错？

**参考答案**：退休指令（instrs）两侧会功能发散（加速器指令在 SimX 里没被建模，行为不同），`test_runner` 抠出的 instrs 不等，model_parity 断言「退休指令必须逐位相等」直接失败——而不是 cycle 容差问题（cycle 容差只吸收时序近似，不吸收功能差异，见 u7-l4、u13-l4）。

**练习 2**：为什么不能在 `sw/kernel/include/vx_dxa.h` 里 `#include "VX_config.h"` 来读配置？

**参考答案**：因为 `VX_config.h` 是 HW/sim 私有微架构配置，`sw/kernel` 是安装头目录，必须自包含、不能 pull `hw/*`/`sim/*`/`sw/common/`。配置取值应经 `VX_types.h`、运行时 `vx_device_query()` 或 `gen_config.py --cflags` 注入的 `-D` 标志（见 u2-l3、coding_guidelines_cpp.md §8）。

---

### 4.2 ISA 接口设计：先定接口，再写数据通路

#### 4.2.1 概念说明

这是整篇讲义最重要的一节。设计文档的开篇一句话点题：

> The hard part is almost never the accelerator's datapath — it is the **interface**: how a warp passes arguments in and gets results out, cheaply.

一条 RISC-V 指令**至多读 3 个源寄存器、写 1 个目的寄存器**。而加速器一次调用往往需要十几个字段（一个 DMA 描述符就十来个域、一个矩阵 fragment 8–24 个寄存器）。核心矛盾就在这里。

设计文档点名了**这个领域最严重的单一错误**：搞一个 per-(warp,lane) 的「特殊寄存器堆」，让 kernel 用一连串 `set` 操作一格一格填、再用 `get` 一格一格读回。真实案例里，发出一个 work item 要约 16 条指令（≈10 个 set + launch + wait + ≈4 个 get），外加约 116 字节/lane 的专用 SRAM。本节教你怎么避开它。

关键洞察：**入参活在不同的 SIMT 作用域里，每个作用域都有自己最便宜的投递机制**。所以第一步永远是**先给每个入参分类**，再按作用域选机制。

#### 4.2.2 核心流程

**第 1 步：入参作用域分类**（来自设计文档 §1.1 的分类表）

| 作用域 | 含义 | 是否随线程发散 | 最便宜的归宿 |
|---|---|---|---|
| **per-thread** | 每 lane 一个值（真正的工作项） | 是 | 寄存器窗口，或 custom-LD 进 SRAM |
| **per-warp** | 调用点 warp 内一致 | 通常不发散 | lane-scatter pack 进一个寄存器 |
| **per-CTA** | 一个线程块共享 | 不发散 | 共享内存 / 屏障守护 |
| **per-dispatch** | 整次 kernel launch 恒定 | 不发散 | DCR（主机编程） |

把 per-dispatch 常量塞进 per-thread 寄存器、或把 per-thread 值塞进 DCR，都是错的。**机制要匹配作用域。**

**第 2 步：四种传递机制**（按每次调用成本从低到高排列）

1. **DCR —— per-dispatch 配置**（§2.1）：整个 launch 共享的状态（使能位、模式/格式选择、回调入口 PC、buffer 基址）放进 DCR，由主机运行时在 launch **前**写好，kernel 不参与。成本：**每次调用 0 条指令**，一次 launch 编程一次。

2. **Lane-scatter packing —— `vx_wgather` 技巧**（§2.2）：SIMT 寄存器物理上是 `SIMD_WIDTH` 个 lane 的向量（基线 `SIMD_WIDTH=4` 时一个寄存器 = 跨 warp 的 4×32=128 bit）。对 warp 一致的值，不要把同一个字复制到所有 lane 浪费带宽；而是把**多个不同的 warp 级标量打包进一个寄存器的各 lane**，让加速器读 lane i 当作第 i 个参数。`vx_wgather(a,b,c,d)` 正是干这个的。成本：**一条** `vx_wgather`（纯寄存器域，不访存），循环不变时可外提 → 摊到 ≈0。

3. **寄存器窗口 —— per-thread 多寄存器操作数**（§2.3）：per-thread 工作项本来就在寄存器堆里以连续**寄存器组**形式存在。加速器指令直接读这扇窗口，无需拷进特殊寄存器堆。因为窗口超过 3 个读端口，指令是宏指令，由 sequencer 展开（§4.3）。**关键纪律「按类型分窗」**：浮点操作数放 FP 窗、整数放 GP 窗，让加速器各自从原生寄存器堆读，**零 `fmv` 转换**；把浮点硬塞 GP 窗会每字付出一条 `fmv.x.w`，正好 reintroduce 你想消除的 marshalling。

4. **Custom-LD 进加速器 SRAM —— 完全绕过寄存器堆**（§2.4）：当 per-thread/per-warp 数据块本就在内存里、或体积大时，定义一条 custom load，从共享/设备内存**直接写进加速器本地 SRAM，没有寄存器（`rd`）写回**。成本：每个块一条 custom-LD（sequencer 可能展开成逐 lane 读），有效载荷**零**寄存器压力。

**第 3 步：选指令格式 R2 vs R4**（§2.5）

| | R-type（R2） | R4-type |
|---|---|---|
| 源寄存器 | 2（`rs1`,`rs2`） | 3（`rs1`,`rs2`,`rs3`） |
| 子操作/格式域 | `funct7` = 7 bit（128 码） | `funct2` = 2 bit（4 码） |
| 适用 | config 在 `rs1` + 窗口基址在 `rs2` + 多子操作/格式 | 三个真正独立寄存器操作数、子操作少 |

决定性洞察：**架构格式并不限制宏操作能读多宽的操作数**——宏操作的 uop 各自最多读操作数收集器的端口数（典型 3）个寄存器，与指令是 R2 还是 R4 无关。所以选 R2 在宽操作数吞吐上不花代价。

**推荐：多数加速器优先 R2**。配合本指南的模式，每次调用的操作数坍缩成：warp 一致参数 lane-pack 进一个寄存器（`rs1`）+ per-thread 窗口由一个基址寻址（`rs2`）+ 结果/handle 在 `rd`，正好放进 R-type，剩下的 7-bit `funct7` 编码大量子操作 + 格式/槽选择。只在**真正需要三个独立寄存器源**（既不能 lane-pack、又不是连续组）时才用 R4。

**第 4 步：异步设计**（§5）

长延迟加速器不应阻塞 warp 整段时间。让 launch 返回一个 **handle**，用单独的 `wait` 操作阻塞：

```c
uint32_t h   = my_accel_launch(desc, work_item);  // 立即返回
// ... kernel 在这里做独立工作，与操作重叠 ...
uint32_t sts = my_accel_wait(h, &result);         // 经 scoreboard 阻塞
```

launch 把输入快照进加速器自己的在途存储（slot/context/bank），返回小 handle，释放 warp；加速器异步执行；`wait(handle)` 经 scoreboard 阻塞（不自旋），在途池满时背压传回发射 warp。长操作期间只有 handle（一个寄存器）存活，延迟隐藏几乎免费。

#### 4.2.3 源码精读

设计文档的入参作用域分类表（本节核心）：

参见 [docs/designs/custom_accelerator_isa_extensions.md:L36-L44](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/custom_accelerator_isa_extensions.md#L36-L44)——这张表把「per-thread / per-warp / per-CTA / per-dispatch」四个作用域对齐到四种最便宜归宿。

`vx_wgather` 在真实扩展里的用法（DXA 把 4 个 warp 级参数打包进 `rs1` 的 4 个 lane）：

参见 [sw/kernel/include/vx_dxa.h:L53-L67](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h#L53-L67)，其中 `vx_wgather(smem_addr, meta, coord0, 0)` 把四个标量散布到 4 个 lane，再由一条 `.insn r`（R-type）整组交给加速器——这正是机制 2 的落点。文件顶部 L38-L46 还标注了 lane→参数的映射契约。

R2 vs R4 的取舍与推荐：

参见 [docs/designs/custom_accelerator_isa_extensions.md:L186-L229](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/custom_accelerator_isa_extensions.md#L186-L229)，结论是「prefer R2 for most accelerators」。

#### 4.2.4 代码实践

**实践目标**：用设计文档 §9 的「决策清单」给一个假想加速器的入参选机制。

**操作步骤**：

1. 假设你要加一个「per-thread 输入 8 个 FP 寄存器 + 一个 warp 一致的描述符 + 一个整次 launch 恒定的模式位」的加速器。
2. 对照 [docs/designs/custom_accelerator_isa_extensions.md:L383-L398](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/custom_accelerator_isa_extensions.md#L383-L398) 的决策清单，逐参回答：
   - 模式位（整次 launch 恒定）→ DCR。
   - 描述符（warp 一致、≤4 个字）→ `vx_wgather` lane-pack。
   - 8 个 FP 输入（per-thread、寄存器内、刚算出）→ FP 寄存器窗口。
3. 按 §3 的 uop 计数法估算 launch 的 uop 数。

**需要观察的现象**：一次 launch 的 uop 数应当是 \(\lceil n/p \rceil\) 且**按寄存器堆分**。

**预期结果**：1 个 GP 描述符 + 8 个 FP 工作项、3 读端口时，= 1 个 GP uop + \(\lceil 8/3 \rceil = 3\) 个 FP uop = 共 **4 uop**（不是 9、也不是 3）。这正好对应设计文档 §3 的 worked example：

参见 [docs/designs/custom_accelerator_isa_extensions.md:L252-L264](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/custom_accelerator_isa_extensions.md#L252-L264)。此结果**待本地验证**（取决于你的加速器实际操作数布局）。

#### 4.2.5 小练习与答案

**练习 1**：为什么「把浮点数据放进 GP 寄存器窗口」是反模式？

**参考答案**：因为 Vortex 有独立的整型（`x`）与浮点（`f`）寄存器堆，浮点放 GP 窗会强制每个字做一次 `fmv.x.w` 转换，正好 reintroduce 你设计寄存器窗口想消除的 marshalling 开销。正确做法是按类型分窗——浮点进 FP 窗、整数进 GP 窗，各自从原生堆零转换读出（§2.3、§7）。

**练习 2**：一个 kernel 每次调用都换一个不同的源指针，这个指针该放 DCR 还是放指令里？

**参考答案**：放指令里。设计文档 §7 明确：「Putting a per-call pointer in a DCR」是反模式。DCR 只放**整次 launch 恒定**的值；per-invocation 会变的绑定不是 per-dispatch，必须留在指令里（§2.1 的 Rule）。

**练习 3**：加速器在途 slot 的数据，与源寄存器的关系应该怎样？

**参考答案**：「Stream-to-destination, never replicate」——launch 的首个 uop 分配 slot 并返回其索引作 handle，后续 uop 把工作项**直接写进那个 slot**，源寄存器在 dispatch 后即释放。**没有中间暂存 buffer、没有第二份寄存器堆副本**；slot 一旦拥有数据，源就自由了（§6）。

---

### 4.3 SimX 扩展点：新增 FuncUnit 与 channel 接线

#### 4.3.1 概念说明

设计好 ISA 表面后，SimX 是第一个落地点（当 oracle）。SimX 给了你两条挂载路径：

1. **新增独立 `FUType`**：当加速器是执行级里一个并列的大块（如 TCU），给它一个新的 `FUType` 枚举值、自己的 `FuncUnit` 派生类、自己的 dispatcher 槽位。
2. **挂在 SFU 下当子单元**：当加速器更像「特殊功能」、且复用 SFU 的单端口分派器最划算时（如 DXA/TEX/OM/RTU），给它一个新的 `op_type`，在 `SfuUnit::on_tick` 里按 `op_type` 路由到自己的子单元。

两条路径都建立在同一个 CRTP 基类上，都遵守基数规则（只通过 channel 通信）。

#### 4.3.2 核心流程

新增一个独立 FUType 的 SimX 改动清单（以 TCU 为模板）：

```text
1. types.h：在 FUType 枚举里加新值（ifdef 门控），补 << 打印
2. func_unit.h：已提供 FuncUnit<NUM_BLOCKS> CRTP 基类——直接继承
3. 写 <name>_unit.{h,cpp}：
   ├─ ctor 调 FuncUnit<VX_CFG_NUM_<X>_BLOCKS>(...)
   ├─ 私有 execute(trace) 承载语义（写 trace->dst_data，用 trace->tmask 门控）
   ├─ latency_of(trace) 返回该 op 的周期数
   └─ on_tick() 循环：peek input → execute → output.send(trace, delay) → input.pop()
4. decode.cpp：把新 CUSTOM opcode 的 case 翻成 set_fu_type + set_op_type + 源/目寄存器
5. core.cpp：
   ├─ func_units_.at(FUType::<X>) = create_object<...>
   ├─ dispatchers_.at(FUType::<X>) = create_object<Dispatcher>(...)
   └─ 若单元需要访问 LMEM/LSU，像 TCU 那样 bind channel
```

若走 SFU 子单元路径（以 DXA 为模板）：跳过 1/5，改为在 `SfuUnit` 构造函数里 `new DxaUnit(...)`，并在 `on_tick` 的 PE-switch 里按 `std::get_if<DxaType>` 路由。

#### 4.3.3 源码精读

**FuncUnit CRTP 基类**——所有功能单元的统一骨架。类型擦除基类 `FuncUnitBase` 让 Core 能把异构的 `FuncUnit<N>` 放进同一容器：

参见 [sim/simx/func_unit.h:L26-L32](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/func_unit.h#L26-L32)（`FuncUnitBase` 三个纯虚：`input(b)`/`output(b)`/`num_blocks()`），以及 [sim/simx/func_unit.h:L38-L70](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/func_unit.h#L38-L70)（`FuncUnit<NUM_BLOCKS>` 模板：每物理 lane 一对 `Inputs`/`Outputs` channel，派生类须实现 `on_tick()`）。这段说明了「一个新 FU 要实现哪些 channel 与钩子」。

**最简单的 FU 范例 ALU**——「纯 channel 延迟」单元：内部无状态，`execute()` 一次算完，延迟由 `output.send(trace, delay)` 的 channel delay 承载：

构造函数继承基类并指定块数（`VX_CFG_NUM_ALU_BLOCKS`）：

参见 [sim/simx/alu_unit.cpp:L27-L29](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/alu_unit.cpp#L27-L29)。

`execute()` 用 `trace->tmask`（发射时快照，而非实时 warp 状态）门控逐线程写回 `trace->dst_data`：

参见 [sim/simx/alu_unit.cpp:L89-L98](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/alu_unit.cpp#L89-L98)，注释明确指出「Use trace->tmask captured at issue」——这是所有 FU 的关键不变量。

`on_tick()` 的标准循环（peek → execute → send(delay) → pop），背压由 `output.full()` 实现：

参见 [sim/simx/alu_unit.cpp:L524-L538](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/alu_unit.cpp#L524-L538)。新 FU 复制这个循环即可。

**FUType 枚举**——新增 FU 类型的登记处，TCU 在 ifdef 门控下加入：

参见 [sim/simx/types.h:L176-L185](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h#L176-L185)——注意 `Count` 在最后，`commit_arbs` 与 `func_units_` 都以 `(uint32_t)FUType::Count` 为容量。新值加在 `Count` 之前。

**译码表**——把 CUSTOM opcode 翻成 `FUType` + `op_type`。两个挂载路径都看得到：

DXA 走 SFU 子单元路径（`set_fu_type(FUType::SFU)` + `set_op_type(DxaType::ISSUE)`）：

参见 [sim/simx/decode.cpp:L884-L893](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decode.cpp#L884-L893)。

TCU 走独立 FUType 路径（`set_fu_type(FUType::TCU)`，并把 WMMA 标为宏指令 `set_macro_op()` + `set_wstall(true)` 暂停放指）：

参见 [sim/simx/decode.cpp:L894-L932](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decode.cpp#L894-L932)——这是「宏指令在译码时只产生一条、由 sequencer 在发射阶段展开」的落点（见 u6-l2）。

**Core 总装**——实例化 FU 与 dispatcher，并按需 bind channel：

参见 [sim/simx/core.cpp:L226-L264](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L226-L264)——TCU 整段在 `#ifdef VX_CFG_EXT_TCU_ENABLE` 内，包括创建单元、创建 dispatcher、把 `TcuTbuf` 的 LMEM 端口绑定到 `local_mem_->Inputs.at(LSU_NUM_REQS)`（即「接在 LSU 端口之后」），以及把 metadata AGU 绑到 LSU block-0 客户口。新 FU 若要访问 LMEM/LSU，照此 bind。

**SFU 作为子单元分派器**——第二条挂载路径的精髓。SFU 在构造时按 ifdef `new` 出各子单元，在 `on_tick` 的 PE-switch 里按 `op_type` 路由：

构造时按配置条件创建 WCTL/CSR/DXA/TEX/OM/RTU 等子单元：

参见 [sim/simx/sfu_unit.cpp:L34-L70](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp#L34-L70)——这就是「SFU 为何是分派器而非单一执行单元」的来源（见 u6-l4）。

`on_tick` 的 PE-switch 用 `std::get_if<XxxType>(&trace->op_type)` 选路，DXA 命中时调 `dxa_unit_->process(trace)`：

参见 [sim/simx/sfu_unit.cpp:L317-L584](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp#L317-L584)。新子单元在此加一个 `else if` 分支即可。

#### 4.3.4 代码实践

**实践目标**：在不写新代码的前提下，走查一遍「新增一个 SFU 子单元」需要碰的 SimX 触点。

**操作步骤**：

1. 打开 `sim/simx/sfu_unit.cpp` 的构造函数（L34-L70），找到 DXA 子单元的创建语句，记下它接收的 channel 参数（`dxa_req_out`）。
2. 打开 `sim/simx/decode.cpp` 的 DXA case（L884-L893），确认它设置了 `FUType::SFU` + `DxaType::ISSUE` + 两个整型源寄存器。
3. 打开 `sim/simx/sfu_unit.cpp` 的 `on_tick`（L565-L572），确认 DXA 命中分支调用了 `dxa_unit_->process(trace)`，且返回 nullptr 时 `continue`（幂等背压重试）。
4. 对照 `sim/simx/dxa/dxa_unit.cpp` 与 `dxa_core.cpp`，确认它们遵守基数规则——只通过 channel 与外界通信（见 u5-l3）。

**需要观察的现象**：SFU 子单元路径**不改 `FUType` 枚举、不改 `core.cpp` 的 dispatcher 表**——只动 `sfu_unit` 的构造与 `on_tick`。

**预期结果**：你会得出结论——轻量加速器挂 SFU 下改动面最小；重型独立流水线才值得新建 FUType。**待本地验证**（取决于你目标加速器的吞吐与端口需求）。

#### 4.3.5 小练习与答案

**练习 1**：新 FU 的 `execute()` 里，逐线程写回应该用 `warp.tmask` 还是 `trace->tmask`？

**参考答案**：用 `trace->tmask`。`trace->tmask` 是发射时快照；`warp.tmask` 是实时状态，可能因分支发散在本 trace 执行前已变。commit/writeback 都按 `trace->tmask` 键控，二者不一致会让某些 lane 留下陈旧的 `dst_data`（见 alu_unit.cpp L92-L98 的注释）。

**练习 2**：为什么 TCU 的 WMMA 在译码时要 `set_wstall(true)`？

**参考答案**：因为它是宏指令，sequencer 要在发射阶段把它展开成一串 uop 流过流水线，**在展开完成前必须暂停取指**，否则后续指令会插队。`set_wstall(true)` 就是这个「pause fetch while sequencer expands」的标记（见 decode.cpp L903-L904、u6-l2）。

---

### 4.4 RTL 扩展点：新增 .sv 与流水线接入

#### 4.4.1 概念说明

RTL 侧与 SimX 一一对应：`FUType` 对应 `EX_*` localparam、SimX 的 `func_units_` 容器对应 `VX_execute.sv` 的按 `EX_*` 索引实例化、SimX 的 channel 对应 RTL 的 `valid`/`ready` 接口。这种逐模块对应正是 model_parity 的物理基础（见 u7-l2、u7-l4）。

RTL 还多一道 SimX 没有的工序：**让配置开关决定源文件清单**——由 `hw/syn/extensions.mk` 在综合时按 `XCONFIGS` 宏把扩展的 RTL 包与 include 路径条件加入。此外，RTL 必须遵守 `coding_guidelines_verilog.md` 的纪律：复用 `hw/rtl/libs`、强制 `begin/end`、注册自己的对外接口、绝不写 blanket `lint_off`。

#### 4.4.2 核心流程

RTL 侧新增加速器的改动清单：

```text
1. VX_gpu_pkg.sv：在 EX_ALU/LSU/SFU/FPU/TCU 序列里加新 EX_<X> localparam
   └─ NUM_EX_UNITS = EX_<X> + 1（自动派生单元总数）
2. 写 hw/rtl/<x>/VX_<x>_pkg.sv 与 VX_<x>_unit.sv（命名 VX_ 前缀 + PascalCase）
   └─ 用 valid/ready 接口；优先复用 hw/rtl/libs 的 arbiter/fifo/xbar
3. VX_execute.sv：在 ifdef 门控下实例化，dispatch_if/commit_if 用
   [EX_<X>*ISSUE_WIDTH +: ISSUE_WIDTH] 切片接入
4. extensions.mk：ifneq 过滤 -DVX_CFG_EXT_<X>_ENABLE 加入 VX_<x>_pkg.sv 与 -I
5. 配套：perf 计数、VX_define.vh 宏、（若上板）AFU 外壳接线
```

#### 4.4.3 源码精读

**EX_* 功能单元编号**——RTL 的「FUType 等价物」。注意 FPU 与 TCU 都由 `*_ENABLED` 镜像门控，编号因此是连续派生的：

参见 [hw/rtl/VX_gpu_pkg.sv:L229-L236](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv#L229-L236)——`EX_FPU = EX_SFU + VX_CFG_EXT_F_ENABLED`、`EX_TCU = EX_FPU + VX_CFG_EXT_TCU_ENABLED`、`NUM_EX_UNITS = EX_TCU + 1`。新增单元时把新 localparam 接在链尾、并把 `NUM_EX_UNITS` 改成 `EX_<X> + 1`。这正是 §4.5 的 `_ENABLE`/`_ENABLED` 镜像在 RTL 编号里的直接体现。

**Execute 级按 EX_* 索引实例化**——`dispatch_if`/`commit_if` 是 `NUM_EX_UNITS*ISSUE_WIDTH` 的二维数组，每个单元切片接入：

TCU 在 ifdef 门控下实例化，端口用 `[EX_TCU * ISSUE_WIDTH +: ISSUE_WIDTH]` 切片：

参见 [hw/rtl/core/VX_execute.sv:L127-L143](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_execute.sv#L127-L143)；接口数组的声明见 [hw/rtl/core/VX_execute.sv:L45-L48](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_execute.sv#L45-L48)。新单元照此加一段 ifdef 实例化即可。

**配置开关 → RTL 源文件清单的桥**——综合时唯一把 `VX_CFG_EXT_*` 与 RTL 文件关联起来的地方：

TCU、DXA、RTU、RASTER、TEX、OM 各自由对应宏条件加入 `RTL_PKGS` 与 `RTL_INCLUDE`：

参见 [hw/syn/extensions.mk:L24-L83](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/extensions.mk#L24-L83)。新增扩展时在此加一个 `ifneq (,$(filter -DVX_CFG_EXT_<X>_ENABLE, $(XCONFIGS)))` 块，把自己的 `VX_<x>_pkg.sv` 与 `-I$(RTL_DIR)/<x>` 追加进去。这段对应 u14-l2 讲过的「`gen_config.py --cflags` 是综合侧与配置真相来源的唯一接口」。

**Verilog 编码纪律**（扩展时必须遵守）：

模块命名 `VX_` 前缀 + PascalCase、信号 lower_snake_case、时钟 `clk`、复位 `reset`：

参见 [docs/coding_guidelines_verilog.md:L16-L23](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/coding_guidelines_verilog.md#L16-L23)。

`begin/end` 在每个 `if`/`else`/`for` 体里强制（哪怕单语句），违例被点名 banned：

参见 [docs/coding_guidelines_verilog.md:L26-L52](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/coding_guidelines_verilog.md#L26-L52)——理由是单语句捷径会在追加第二条语句时静默改变作用域（goto-fail 类 bug），并破坏 diff 卫生。

写 RTL 前先查 `hw/rtl/libs`，优先实例化现成的 arbiter/fifo/xbar/encoder 等参数化模块，而非手搓：

参见 [docs/coding_guidelines_verilog.md:L234-L236](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/coding_guidelines_verilog.md#L234-L236)。这条纪律直接服务于 model_parity——库模块自带一致的 valid/ready 握手且已验证，手搓等价逻辑容易引入微妙的握手/时序 bug。

#### 4.4.4 代码实践

**实践目标**：在 RTL 里走查 TCU 的完整接线，画出「配置→编号→实例化→源清单」链路。

**操作步骤**：

1. 在 `hw/rtl/VX_gpu_pkg.sv`（L229-L236）确认 `EX_TCU` 由 `VX_CFG_EXT_TCU_ENABLED` 门控、`NUM_EX_UNITS` 因此可变。
2. 在 `hw/rtl/core/VX_execute.sv`（L127-L143）确认 TCU 实例化整段包在 `\`ifdef VX_CFG_EXT_TCU_ENABLE` 内，且其 `dispatch_if`/`commit_if` 切片用 `EX_TCU` 寻址。
3. 在 `hw/syn/extensions.mk`（L24-L47）确认 `-DVX_CFG_EXT_TCU_ENABLE` 出现在 `XCONFIGS` 时，`VX_tcu_pkg.sv` 与 `-I$(RTL_DIR)/tcu`（及各 FEDP 后端的子目录）才被加入。
4. 浏览 `hw/rtl/tcu/` 目录，确认其内部模块用了 `hw/rtl/libs` 的现成元件（如 arbiters）。

**需要观察的现象**：关掉 `VX_CFG_EXT_TCU_ENABLE` 后，`EX_TCU` 编号、`VX_execute` 实例化、综合源清单**三处同时消失**——扩展是全条件编译的。

**预期结果**：你会看到 RTL 扩展的接线是一个**闭合的条件编译环**：config 宏 → `EX_*` 编号 → `VX_execute` 实例化 → `extensions.mk` 源清单，环上任意一环漏改都会断链。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `EX_FPU` 与 `EX_TCU` 要用 `*_ENABLED`（自动派生镜像）而不是 `*_ENABLE`（作者手写布尔）来做算术？

**参考答案**：因为 localparam 算术需要 0/1 整数来让编号连续可变（`EX_FPU = EX_SFU + EXT_F_ENABLED`）。`_ENABLE` 是 toml 里的布尔、`_ENABLED` 是 `gen_config.py` 自动派生的 0/1 整数镜像（见 u2-l1）。用整数镜像才能在禁用扩展时让后续编号自动回缩、`NUM_EX_UNITS` 自动减小。

**练习 2**：RTL 里若不慎把对外接口在 consumer 内部再寄存一拍修时序，违反了哪条规范？

**参考答案**：违反「Buffering ownership」——流水线/缓冲级属于 producer/分发侧，consumer 必须按送达使用、不得内部 re-register 修时序；consumer 侧锁存会把它与共享广播/fork 的其它端点去同步，破坏总线的投递契约。正确做法是在驱动的分发模块抬 `OUT_BUF` 深度（见 coding_guidelines_verilog.md §4）。

---

### 4.5 kernel 与 config 配套：vx_*.h 内联函数与 VX_CFG_* 开关

#### 4.5.1 概念说明

前两节给了数据通路（SimX + RTL），但 warp 还得有办法**发出**新指令、配置系统还得有办法**条件编译**它。这节收口最后两层：

- **kernel 层**：在 `sw/kernel/include/vx_*.h` 里写 C 内联函数，用 RISC-V `.insn` 内联汇编发出 custom 指令，并用 `vx_wgather`/寄存器窗口等机制打包参数（§4.2 的落地）。
- **config 层**：在 `VX_config.toml` 登记布尔开关 `VX_CFG_EXT_*_ENABLE`，`gen_config.py` 自动派生 `*_ENABLED` 整数镜像供 RTL/SimX 算术用，并把扩展登记进 `MISA_EXT` 位图。

这两层都必须守住 `sw/` ↔ `hw/`/`sim/` 双向隔离——`vx_*.h` 是安装头，必须自包含。

#### 4.5.2 核心流程

```text
config 层：
  VX_config.toml: VX_CFG_EXT_<X>_ENABLE = false   ← 作者只写布尔
       ↓ gen_config.py
  VX_CFG_EXT_<X>_ENABLED (0/1 整数镜像)            ← RTL/SimX 算术用
       ↓
  MISA_EXT 位图登记该扩展位（供设备能力上报）

kernel 层：
  sw/kernel/include/vx_<x>.h
  ├─ #define VX_<X>_EXT_OPCODE  RISCV_CUSTOM0/1
  ├─ #define VX_<X>_FUNCT7      <7-bit 子操作/格式>
  ├─ 用 vx_wgather(...) 打包 warp 一致参数进 rs1/rs2 的 lane
  └─ __asm__ volatile (".insn r ...") 发出指令
  （不 include VX_config.h；配置经 VX_types.h / -D 标志获取）
```

#### 4.5.3 源码精读

**config 开关登记**——所有扩展使能位的单一真相来源：

参见 [VX_config.toml:L33-L39](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L33-L39)——TCU/DMA/DXA/TEX/RASTER/OM/RTU 的 `VX_CFG_EXT_*_ENABLE` 都在此，默认全 `false`。文件首行的纪律注释见 [VX_config.toml:L1](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L1)：每个 `VX_CFG_X_ENABLE` 布尔自动生成整数镜像 `VX_CFG_X_ENABLED`（0/1），作者只写布尔。

`_ENABLED` 镜像被用于设备能力位图 `MISA_EXT` 的算术：

参见 [VX_config.toml:L366](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L366)——`VX_CFG_EXT_TCU_ENABLED << 9`、`VX_CFG_EXT_DXA_ENABLED << 10` 等，把扩展登记进设备向主机上报的能力位图（见 u2-l1、u2-l2）。新增扩展时在此加一项位移。

**kernel 内联函数范例 DXA**——把 §4.2 的机制落到 C 代码：

opcode/funct7 定义与 R-type 指令格式注释：

参见 [sw/kernel/include/vx_dxa.h:L24-L36](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h#L24-L36)——`VX_DXA_EXT_OPCODE = RISCV_CUSTOM0`、`VX_DXA_FUNCT7 = 0x3`，并注释了 R-type 的 32-bit 布局（`funct7|rs2|rs1|funct3|rd|opcode7`）。

1D issue：`vx_wgather` 把 `smem_addr/meta/coord0/0` 打包进 `rs1` 的 4 lane，再发一条 R-type：

参见 [sw/kernel/include/vx_dxa.h:L53-L67](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h#L53-L67)——`__asm__ volatile (".insn r %0, 0, %1, x0, %2, x0" ...)` 即 §4.2.2 机制 2 的真实落地。注意它不写 `rd`（DXA 是异步的，结果经屏障事务回送，见 u9-l2）。3D–5D 维度需要第二个 `vx_wgather` 打包 `rs2`（见 L87-L108）。

**软硬隔离纪律**——`vx_*.h` 作为安装头必须自包含：

参见 [docs/coding_guidelines_cpp.md:L179-L187](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/coding_guidelines_cpp.md#L179-L187)——安装头不得传递性地 pull `sw/common/`、`hw/*`、`sim/*`，只能依赖 stdlib + 生成的 `VX_types.h` + 兄弟 vortex 公共头。所以你的 `vx_<x>.h` 不能 `#include "VX_config.h"`。

#### 4.5.4 代码实践

**实践目标**：手写一个最小、不碰源码的 `vx_*.h` 内联函数骨架（在草稿纸上），对齐 DXA 的写法。

**操作步骤**：

1. 假设你的加速器 `my_accel` 用 CUSTOM0、funct7=0x5，接受一个 warp 一致的描述符（4 字：`arg0..arg3`）。
2. 仿照 [sw/kernel/include/vx_dxa.h:L48-L67](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h#L48-L67)，写出 `vx_wgather(arg0,arg1,arg2,arg3)` 打包进一个寄存器、再 `.insn r` 发出的内联函数。
3. 检查你的草稿：是否避免了 `#include "VX_config.h"`？是否给 `__asm__` 加了 `volatile` 与 `"memory"` clobber（DXA 都有）？

**需要观察的现象**：一条 warp 一致的 4 字描述符只需**一条** `vx_wgather` + **一条** `.insn r` 就能整组交给加速器——无需 4 个 `set`。

**预期结果**：你会直观感受到设计文档 §2.2 相对反模式 §7 的成本差距（2 条 vs ≈16 条）。**待本地验证**（需在能编译 RISC-V 的工具链下汇编通过）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `vx_dxa.h` 的内联汇编要加 `volatile` 和 `"memory"` clobber？

**参考答案**：`volatile` 防止编译器把这条没有输出操作数的内联汇编优化掉（DXA 有可见的副作用——发起一次 DMA）；`"memory"` 告诉编译器该指令可能读写内存（异步拷贝会改 LMEM），从而禁止编译器跨越它重排内存访问。

**练习 2**：新增扩展后忘了在 `MISA_EXT` 位图登记位，会有什么后果？

**参考答案**：设备能力上报（`vx_dev_caps`/`vx_device_query` 读 `MISA`）不会反映该扩展存在，主机程序无法在运行时探测到它——但扩展本身的编译与硬件行为不受影响（`MISA_EXT` 只是上报位图，不门控电路，见 u2-l1、u3-l1）。所以这是一个「能力可见性」 bug，不是「功能」bug。

---

## 5. 综合实践

**任务**：为 Vortex 设计一个假想的 **CRC32 加速器**（`my_crc`），完成一次贯穿四层的方案设计。它满足：

- **per-dispatch**：一个多项式选择位（整次 launch 恒定）。
- **per-warp**：一个 4 字描述符 `{src_ptr, len, mode, reserved}`（warp 一致）。
- **per-thread**：每个 lane 独立处理一段数据，结果是一个 32-bit CRC 写回 GP 寄存器。
- 长延迟，应异步。

**要求产出一份四层改动清单**（不写真实代码，只列触点与决策）：

1. **ISA 决策（§4.2）**：用设计文档 §9 的决策清单，给 4 个入参各选机制；选定指令格式（R2 还是 R4，给理由）；画出 launch/wait 的异步时序；列出预期 uop 数（按 \(\lceil n/p \rceil\) 且按寄存器堆分）。
2. **config 层（§4.5）**：写 `VX_CFG_EXT_CRC_ENABLE` 登记、`MISA_EXT` 新位。
3. **kernel 层（§4.5）**：列出 `sw/kernel/include/vx_crc.h` 要定义的 opcode/funct7、`vx_wgather` 打包方案、`.insn r` 内联函数签名，并确认不 include `VX_config.h`。
4. **SimX 层（§4.3）**：决定走「独立 FUType」还是「SFU 子单元」并说明理由；列出 `types.h`/`decode.cpp`/`core.cpp`（或 `sfu_unit.cpp`）各自的触点；说明 `execute()` 如何用 `trace->tmask` 门控逐 lane 写回。
5. **RTL 层（§4.4）**：列出 `VX_gpu_pkg.sv` 的 `EX_CRC` localparam、`VX_execute.sv` 的 ifdef 实例化与接口切片、`extensions.mk` 的源清单条目；标注你会复用 `hw/rtl/libs` 的哪些元件。
6. **两条铁律自检**：确认方案守住了 `sw/`↔`hw/`/`sim/` 双向隔离，且 SimX 与 RTL 两侧时序模型一致（可过 model_parity）。

**参考决策要点**（供自我核对）：

- 多项式位 → **DCR**（per-dispatch）；4 字描述符 → **`vx_wgather` lane-pack 进 `rs1`**（per-warp，≤4）；per-thread 输入是内存块 → 可考虑 **custom-LD 进 SRAM**，或若已 `load` 进寄存器则用 **GP 寄存器窗口**；结果 CRC → **GP 寄存器写回**（少数热标量）；异步 → launch 返回 handle、`wait` 经 scoreboard 阻塞。
- 指令格式优选 **R-type（R2）**：操作数坍缩成 lane-pack 的 `rs1` + 窗口/基址的 `rs2` + handle/status 的 `rd`，且 7-bit `funct7` 留给子操作/格式。
- 挂载路径：CRC 单吞吐、复用 SFU 单端口最划算 → **SFU 子单元**（`CrcType` + `sfu_unit.cpp` 加分支），避免新建 FUType/dispatcher。
- uop 计数：若 per-thread 走 4 字 GP 窗口、3 读端口 → \(\lceil 4/3 \rceil = 2\) 个 GP uop + 1 个 GP 描述符 uop = 3 uop。

> 这是一个纯设计任务，**不要求也不应在本讲义中运行任何命令**。它的价值在于逼你把四层的触点串成一条闭合链路。

## 6. 本讲小结

- 扩展 Vortex 是一次**四层协同改动**（config / kernel / SimX / RTL），少改一层都会断链；贯穿其中的是 `sw/`↔`hw/`/`sim/` 双向隔离与 SimX↔RTL model_parity 两条铁律。
- **ISA 表面是最难改、最先要设计的部分**：先按 per-thread/warp/CTA/dispatch 给入参分类，再用最便宜机制投递——DCR、`vx_wgather` lane-pack、寄存器窗口（按类型分窗）、custom-LD 进 SRAM。
- 宽操作数指令是**宏指令**：由 sequencer 展开成 uop，uop 数为 \(\lceil n/p \rceil\) 且按寄存器堆分；长延迟操作应**异步设计**（launch 返 handle、wait 经 scoreboard 阻塞），并 stream-to-destination、绝不复制寄存器堆。
- SimX 给了两条挂载路径：**独立 FUType**（重型，如 TCU）与 **SFU 子单元**（轻量，如 DXA/TEX/OM/RTU），都建立在 `FuncUnit` CRTP 基类与 channel 基数规则上。
- RTL 与 SimX 逐模块对应：`FUType`↔`EX_*` localparam、`func_units_`↔`VX_execute` 实例化、channel↔`valid/ready` 接口；`extensions.mk` 是配置开关到 RTL 源清单的唯一桥梁。
- config 的 `_ENABLE`（作者布尔）/`_ENABLED`（派生整数镜像）二分贯穿全栈：布尔门控条件编译，整数镜像参与 RTL 编号算术与 `MISA_EXT` 能力位图。

## 7. 下一步学习建议

- **动手读一个完整扩展**：从轻量到重型，依次通读 DXA（`sw/kernel/include/vx_dxa.h` + `sim/simx/dxa/` + `hw/rtl/dxa/`）与 TCU（`vx_tensor.h` + `sim/simx/tcu/` + `hw/rtl/tcu/`），把本讲的四层触点对照真实代码逐一印证。
- **温习前置讲义**：u6-l4（FuncUnit 模型）、u7-l3（RTL 调度器与 warp 控制）、u7-l4（model_parity）、u14-l1（FPGA AFU 外壳，若你的扩展要上板）。
- **深入设计文档**：`docs/designs/custom_accelerator_isa_extensions.md` 的 §7 反模式清单与 §9 决策清单值得反复回看；`docs/designs/simx_simulator_architecture.md` §6 提到本讲对应的 `simx_extension_guide.md`「add a FuncUnit」示例尚属 proposed-but-not-implemented，是你可以贡献文档的方向。
- **若要上板**：结合 u14-l1/u14-l2，留意扩展在 AFU 外壳（OPAE/XRT）与综合流程（`hw/syn`）中的额外接线与 PPA 影响。
