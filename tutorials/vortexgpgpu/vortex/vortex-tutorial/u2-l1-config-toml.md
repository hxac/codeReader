# TOML 配置：唯一的真相来源

## 1. 本讲目标

本讲是「硬件配置系统」单元的第一篇。学完后你应当能够：

- 区分 `VX_config.toml`（硬件构建配置）与 `VX_types.toml`（软件 ABI 契约）这两个「单一真相来源」各自的职责。
- 读懂 `VX_config.toml` 的关键段（平台、ISA、流水线、内存、缓存），并理解 `expr:` 表达式、`[[enum]]`、`[[builtin]]`、`[[param]]` 这几种写法的含义。
- 理解 `VX_CFG_*` 宏命名空间的含义，特别是 `_ENABLE`（布尔开关）与自动生成的 `_ENABLED`（整数镜像）之间的关系，以及「派生宏不是 `VX_CFG_*`」这条纪律。

本讲承接 u1-l3（构建系统、configure 与工具链）和 u1-l4（首次运行 blackbox.sh）。在那些讲义里你已经知道：`configure` 会调用 `gen_config.py` 把 `*.toml` 烘焙成 `.h`/`.vh`，而 `--cores=2` 这类旋钮会被翻译成 `-DVX_CFG_NUM_CORES=2`。本讲要回答的核心问题是——**这些被翻译、被烘焙的值，到底从哪里来？谁有权力定义它们？**

## 2. 前置知识

阅读本讲前，建议你已经了解（来自 u1-l3 / u1-l4）：

- **out-of-tree 构建**：源码树只读，所有生成物写到 `build/` 目录；`configure` 在 `build/` 里运行。
- **configure 是模板填空机**：它用 `sed` 把 `@占位符@` 替换进 `config.mk` / `Makefile` / `ci/*.sh`，并调用 `gen_config.py` 生成 `hw/*.vh` 与 `sw/*.h`。
- **CONFIGS 与 VX_CFG_\* 宏**：`blackbox.sh` 把 `--cores=2` 翻译成 `-DVX_CFG_NUM_CORES=2`，累加进 `CONFIGS` 变量；这些宏同时影响驱动侧（RTL/sim）和应用侧（kernel/test）。
- **TOML**：一种类似 ini 的配置文件格式，用 `[段名]` 划分小节，用 `键 = 值` 赋值。本讲会大量阅读 TOML，但不需要你是 TOML 专家。

一个关键直觉（本讲会反复用到）：**改 toml 之后必须重新 `configure`**，否则 `build/` 里会残留 stale（过期）的 `VX_config.h`。这是 u1-l3 强调过的「大坑」，本讲会从源码层面解释它为什么存在。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `VX_config.toml` | **硬件构建配置**的单一真相来源：~245 个键，覆盖平台规模、ISA 扩展、流水线、内存、各级缓存、FPU/TCU/图形单元、工具链、调试。 |
| `VX_types.toml` | **软件面向的 ISA/ABI 契约**的单一真相来源：CSR/DCR 地址、设备内存映射 `VX_MEM_*`、虚存页表格式 `VX_VM_*`、各类枚举常量。 |
| `docs/designs/build_configuration_system.md` | 配置系统的设计文档，第 1–3 节是本讲的理论骨架（来源与生成器、值流与分层、宏命名空间）。 |
| `ci/gen_config.py` | 配置生成器：把一个 toml + 一种格式（cflags/cpp/verilog）解析并输出对应头文件。本讲引用它来佐证 `_ENABLED` 镜像与枚举派生机制。 |

> 提醒：`gen_config.py` 的内部实现是下一讲 u2-l2 的主题，本讲只引用其中与「命名空间」和「`_ENABLED` 镜像」直接相关的几处，用来证明结论，不展开它的整体架构。

## 4. 核心概念与源码讲解

### 4.1 两个真相来源：VX_config.toml 与 VX_types.toml

#### 4.1.1 概念说明

一个全栈 GPU 项目（主机运行时 → 驱动 → SimX → RTL → FPGA）有成百上千个数值常量：核数、warp 数、缓存大小、CSR 地址、内存映射基址……如果这些常量散落在各个 `.v` / `.cpp` / `.h` 文件里手写，很快就会不一致——RTL 写 `NUM_CORES=4`、SimX 写 `NUM_CORES=2`、运行时又算错了启动维度。

Vortex 的解法是**单一真相来源（single source of truth）**：所有配置常量集中写在两个 TOML 文件里，由生成器 `gen_config.py` 统一分发到 RTL、仿真器、运行时、内核。任何代码都不得手写这些常量，只能引用生成出来的宏。

但为什么是**两个**文件，而不是一个？因为存在一条天然的**硬件/软件边界**：

- `VX_config.toml` 描述「这块芯片造多大、开哪些功能」——这是**硬件构建**的私有事务，软件不需要、也不应该知道（例如你不需要在内核代码里知道 L2 缓存有几路组相联）。
- `VX_types.toml` 描述「软件和硬件之间的契约」——CSR 编号、DCR 编号、内存地址布局、页表格式。这些是**跨边界的 ABI（应用二进制接口）**，软件和硬件必须看到完全相同的值，否则设备程序连寄存器都写不对。

#### 4.1.2 核心流程

配置从 toml 到最终代码的流程（详见 4.3）可以先用一句话概括：

```
VX_config.toml  ──gen_config.py──►  build/hw/VX_config.vh   (`define)  ──► RTL
                                  └─► build/sw/VX_config.h    (#define)  ──► sim / runtime
                  gen_config.py --cflags ──► -DVX_CFG_*      ──► kernel / test 编译

VX_types.toml   ──gen_config.py──►  build/hw/VX_types.vh    ──► RTL (VX_MEM_*, VX_CSR_*)
                                  └─► build/sw/VX_types.h     ──► SW  (ABI 契约)
```

`configure` 是这条流水线的驱动者：对每个 `*.toml`，它各生成一份 `.vh`（Verilog `` `define ``）和一份 `.h`（C `#define`）。

#### 4.1.3 源码精读

设计文档第 1 节开宗明义地列出这两个来源：

[build_configuration_system.md:11-23](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/build_configuration_system.md#L11-L23) 说明：两个 TOML 文件是单一真相来源——`VX_config.toml` 是硬件构建配置（约 245 个键），`VX_types.toml` 是软件面向的 ISA/ABI 契约，外加搬迁过来的 `[memmap]`（`VX_MEM_*`）和 `[vm]`（`VX_VM_*`）小节。

注意这里一个容易混淆的设计：**内存映射和页表格式原本属于 `VX_config.toml`，后来被「搬迁」（relocate）到了 `VX_types.toml`**。原因是它们其实是 HW↔SW 契约，而不是纯粹的硬件构建参数。这就是为什么你在 `VX_types.toml` 里能看到 `VX_MEM_*` 和 `VX_VM_*`。我们看一眼搬迁后的结果：

[VX_types.toml:20-43](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L20-L43) 是 `[memmap]` 段：设备内存映射（用户基址、栈、本地内存、IO、页表基址）。注释明确写着「a HW<->SW contract: the hardware decodes these address regions, the linker places code/stack within them, and the runtime addresses into them」（硬件解码这些地址区、链接器把代码/栈放进它们、运行时往它们里寻址）。

[VX_types.toml:47-54](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L47-L54) 是 `[vm]` 段：页表格式，全是 RISC-V 架构常量（页大小、SV32/SV39 寻址模式、页表级数、PTE 大小），由 SATP 模式固定。

对比之下，`VX_config.toml` 顶部就是纯硬件参数：

[VX_config.toml:3-6](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L3-L6) 是 `[platform]` 段的规模参数：`NUM_CLUSTERS`、`NUM_CORES`、`SOCKET_SIZE`——这些是「芯片造几个核」的硬件构建决策，软件不需要直接知道。

> 初学者术语提示：
> - **CSR（Control and Status Register）**：RISC-V 的控制状态寄存器，CPU 用来读写状态，如 `mhartid`（硬件线程号）。
> - **DCR（Device Control Register）**：Vortex 自定义的「设备控制寄存器」，主机通过它向 GPU 下发命令（如启动地址、grid 维度）。
> - **ABI（Application Binary Interface）**：二进制接口契约，规定寄存器编号、调用约定、内存布局等，让分别编译的软硬件能正确对接。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（无需运行）：

1. **目标**：用肉眼验证「同一类常量被有意分到了两个文件」。
2. **步骤**：
   - 在 `VX_types.toml` 中找到 `[csr_gpgpu]` 段（`VX_CSR_THREAD_ID`、`VX_CSR_NUM_CORES` 等 CSR 编号）。
   - 在 `VX_config.toml` 中找到 `[platform]` 段的 `VX_CFG_NUM_CORES`。
3. **观察**：`VX_CSR_NUM_CORES`（一个 CSR **编号** `0xFC2`）和 `VX_CFG_NUM_CORES`（核的**数量**）是两个完全不同的东西，却都和「核数」有关。前者是软件读 CSR 时用的地址，后者是硬件实例化多少个核的规模参数。
4. **预期结果**：你能用一句话解释「为什么 CSR 编号在 `VX_types.toml`、而核数量在 `VX_config.toml`」——因为 CSR 编号是软硬件都要遵守的契约，核数量是硬件私有的构建决策。

参考答案：CSR 编号属于跨边界的 ABI（软件用 `csrr` 指令读它，硬件必须把同一个编号解码出来），所以放契约文件 `VX_types.toml`；核数量只决定 RTL 实例化多少个 `VX_core`，软件运行时通过读 CSR 才动态获知，所以放硬件私有文件 `VX_config.toml`。

#### 4.1.5 小练习与答案

**练习 1**：`VX_MEM_IO_EXIT_CODE`（程序退出码地址）应该在哪个 toml 文件里？为什么？

**参考答案**：在 `VX_types.toml` 的 `[memmap]` 段（见 [VX_types.toml:42](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L42)）。因为退出码地址是软件写、硬件/运行时读的内存映射契约，属于 HW↔SW 边界，而非硬件构建规模参数。

**练习 2**：如果某天 Vortex 决定把 L2 缓存的大小从「硬件构建参数」改成「软件可查询的契约」，它应该从哪个文件搬到哪个文件？

**参考答案**：从 `VX_config.toml` 的 `[l2cache]` 搬到 `VX_types.toml`（并改前缀为契约命名空间）。这正是内存映射/页表格式经历过的搬迁逻辑（见 [build_configuration_system.md:54-59](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/build_configuration_system.md#L54-L59)）。

---

### 4.2 VX_config.toml 关键段精读

#### 4.2.1 概念说明

`VX_config.toml` 是日常改动最频繁的文件——你想加一个核、开一个 ISA 扩展、调一个缓存大小，都在这里改。它有 ~245 个键，但结构高度规律。掌握以下几个关键段和几种特殊的「值写法」，就能读懂绝大部分内容。

值的写法有四种，是本节重点：

1. **字面量**：`VX_CFG_NUM_CORES = 1`，直接给整数/布尔。
2. **`expr:` 表达式**：`"expr: ..."`，值由一个迷你表达式计算得出，可引用其它键。
3. **`[[enum]]` 枚举声明**：声明一个参数只能取列表中的某个值。
4. **`[[builtin]]` / `[[param]]` 外部变量声明**：声明某些值来自环境或类型占位，不在本文件给出。

#### 4.2.2 核心流程

阅读 `VX_config.toml` 的建议顺序：先看 `[platform]`（整体规模）→ `[pipeline]`（warp/线程）→ `[isa]`（开了哪些扩展）→ `[memory]` / `[l1cache]` / `[l2cache]` / `[l3cache]`（存储层次）→ `[fpu]` / `[tcu]` / 各图形单元（功能单元）→ `[toolchain]`（工具链谓词）→ 文件末尾的 `[[enum]]` / `[[param]]` / `[[builtin]]`（元声明）。

`expr:` 表达式的求值由 `gen_config.py` 的解析器完成，支持：
- `$NAME` 引用其它键（包括自动派生的 `_ENABLED` 和枚举标志）。
- Python 风格的条件表达式 `A if cond else B`。
- 内置函数：`up(x)`（向上取整）、`clog2(x)`（以 2 为底的对数向上取整）、`pow`、`max`、`min`、`int` 等。

#### 4.2.3 源码精读

**关键段一：平台与流水线规模。**

[VX_config.toml:3-6](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L3-L6) 给出默认规模：`NUM_CLUSTERS = 1`、`NUM_CORES = 1`、`SOCKET_SIZE = 1`。

[VX_config.toml:42-52](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L42-L52) 是 `[pipeline]` 段。注意三个关键事实：
- `VX_CFG_NUM_WARPS = 4`、`VX_CFG_NUM_THREADS = 4`（字面量）。
- `VX_CFG_ISSUE_WIDTH = "expr: up($VX_CFG_NUM_WARPS / 16)"`：当 warps=4 时，`up(4/16)=up(0.25)=1`，即默认每周期发射 1 个 warp。
- `VX_CFG_SIMD_WIDTH = "expr: $VX_CFG_NUM_THREADS"`：SIMD 宽度直接等于线程数。

这就是为什么 u1-l4 里说「改 `--cores` 会改变程序处理的数据点数」——核数/warp 数/线程数是真正驱动规模和启动维度的根参数，而 `ISSUE_WIDTH`、`SIMD_WIDTH`、`NUM_OPCS` 等都是**派生**出来的。

**关键段二：`expr:` 如何引用「自动派生」的值。**

[VX_config.toml:24-25](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L24-L25) 是理解 `expr:` 的最佳入口：

```
VX_CFG_EXT_D_ENABLE = "expr: $VX_CFG_XLEN_64"
VX_CFG_FLEN = "expr: 64 if $VX_CFG_EXT_D_ENABLE else 32"
```

这里的 `$VX_CFG_XLEN_64` 看似没在任何地方定义，其实它是**枚举派生标志**：`VX_CFG_XLEN` 是一个枚举（值为 32 或 64），当其值为 64 时，解析器自动让 `$VX_CFG_XLEN_64` 为真。于是「64 位系统自动开启 D（双精度浮点）扩展，进而 FLEN=64」这条架构规则，被一行表达式干净地表达出来。

我们可以在生成器里验证这个派生机制确实存在：

[gen_config.py:1306-1312](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1306-L1312) 是解析器的 `resolve` 逻辑：当请求一个形如 `VX_CFG_XLEN_64` 的键时，它把名字拆成 base（`VX_CFG_XLEN`，一个枚举）和 suffix（`64`），返回 `(当前枚举值 == 64)` 这个布尔值。这正是 `$VX_CFG_XLEN_64` 在表达式里可用的原因。

**关键段三：缓存层次里的派生逻辑。**

[VX_config.toml:57-69](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L57-L69) 的 `[memory]` 段展示了「line size 如何层层派生」：L1 line 等于 MEM_BLOCK；L2 line 在 L2 开启时翻倍，否则继承 L1；L3 同理。这种「关闭某级时它退化为直通（passthrough）并原样转发上游粒度」的设计，全靠 `expr:` 条件表达式实现，避免了你手动维护各级一致性。

**关键段四：工具链谓词是「裸名」。**

[VX_config.toml:368-375](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L368-L375) 的 `[toolchain]` 段：`ASIC`、`SYNTHESIS`、`VIVADO`、`QUARTUS`、`YOSYS`、`SYNOPSYS`、`SV_DPI` 这些键**不带 `VX_CFG_` 前缀**，默认全为 `false`。它们由 `configure`/综合脚本通过 `-D` 注入，用来让同一个 toml 在「仿真 / FPGA 综合 / ASIC 综合」三种场景下算出不同的延迟值。例如 [VX_config.toml:103](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L103) 的 `VX_CFG_FPU_TYPE` 就是一长串依赖 `ASIC/SYNTHESIS/SV_DPI` 的条件表达式。

**关键段五：文件末尾的元声明。**

[VX_config.toml:381-394](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L381-L394) 集中了三种特殊块：
- `[[enum]]`：声明 `VX_CFG_XLEN` 只能取 `[32, 64]`、`VX_CFG_FPU_TYPE` 只能取 `["DPI","DSP","FPNEW","STD"]` 等。生成器会校验 `-D` 覆盖时的取值合法性。
- `[[param]]`：声明 `VX_CFG_DCACHE_NUM_REQS` 等参数只有「类型」、无默认值，其值由 RTL 实现内部推导后回填（类型占位）。
- `[[builtin]]`：声明 `__FILE__` / `__LINE__` 这类由环境/编译器提供的内置变量，仅在 `expr:` 求值时可见，不会被输出到头文件。

#### 4.2.4 代码实践

这是一个**配置阅读 + 推算型实践**：

1. **目标**：不运行任何东西，仅凭阅读 `VX_config.toml` 推算默认配置下的若干派生值。
2. **步骤**：
   - 记录字面量：`NUM_CORES=1`、`NUM_WARPS=4`、`NUM_THREADS=4`、`XLEN` 默认（configure 默认 32 位）。
   - 手算：`ISSUE_WIDTH = up(4/16) = ?`；`SIMD_WIDTH = ?`；`VLEN = XLEN*4 = ?`；当 XLEN=32 时 `EXT_D_ENABLE = ?`、`FLEN = ?`。
3. **观察**：把你的手算结果和「直觉上的 GPU 规模」对比——默认是一颗非常小的单核 GPU。
4. **预期结果**：`ISSUE_WIDTH=1`、`SIMD_WIDTH=4`、`VLEN=128`（32×4）、XLEN=32 时 `EXT_D_ENABLE=false`、`FLEN=32`。如果推算 FLEN 时不确定 `EXT_D_ENABLE`，回顾 4.2.3 关键段二的 `$VX_CFG_XLEN_64` 派生机制。
5. **验证（可选）**：若本地已 `configure`，可打开 `build/hw/VX_config.vh` 或 `build/sw/VX_config.h` 对照你的手算结果；若不一致，多半是你在改过 toml 后忘了重新 `configure`（stale 头文件大坑）。

> 说明：以上推算基于本文件注释与表达式语义，未在你的机器上实际运行；若与生成的头文件不符，以生成的头文件为准并检查是否 stale。

#### 4.2.5 小练习与答案

**练习 1**：`VX_CFG_TCU_TYPE` 写成字面量 `"TFR"`（[VX_config.toml:239](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L239)），而不是 `expr:`。如果有人用 `-DVX_CFG_TCU_TYPE=XYZ` 覆盖会发生什么？

**参考答案**：因为 `VX_CFG_TCU_TYPE` 在 `[[enum]]` 里声明了合法取值列表 `["DPI","DSP","BHF","TFR","FPNEW"]`（[VX_config.toml:385](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L385)），`gen_config.py` 的 `set_enum` 会校验取值（[gen_config.py:430-435](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L430-L435)），非法值 `XYZ` 会直接报错，防止配错。

**练习 2**：`[toolchain]` 段的键为什么没有 `VX_CFG_` 前缀？

**参考答案**：它们是「构建场景谓词」（仿真/综合/ASIC），由命令行 `-D` 注入，不是「用户可配置的硬件旋钮」。设计文档 §3.2 规定：不是配置旋钮的派生/外部量不应带 `VX_CFG_` 前缀（详见 4.4）。

---

### 4.3 配置值流与硬件/软件分层

#### 4.3.1 概念说明

知道了「两个真相来源」之后，还要明白这些值**怎么流到不同消费者**，以及为什么 Vortex 要严格隔离「硬件私有配置」和「软硬件共享契约」。

设计文档第 2 节用一张图描述了值流，并用「HW/sim-private vs 共享 ABI」这条线把两个 toml 隔开。这条隔离线的现实意义是：**公共运行时头文件绝不能 `#include "VX_config.h"`**。因为 `VX_config.h` 暴露了硬件私有机密（缓存结构、流水线深度），一旦上层软件（如 OpenCL/PoCL）依赖了它，硬件团队改缓存时就会破坏软件兼容性。

#### 4.3.2 核心流程

```
                       ┌── gen_config.py ──► build/hw/VX_config.vh (`define) ──► RTL 引用 `VX_CFG_*
VX_config.toml  ───────┤
  (HW/sim 私有)        └── gen_config.py ──► build/sw/VX_config.h  (#define) ──► sim / runtime(私有部分)
                       └── gen_config.py --cflags ──► -DVX_CFG_*              ──► kernel / test 编译

                       ┌── gen_config.py ──► build/hw/VX_types.vh  ──► RTL 引用 `VX_MEM_*, `VX_CSR_*
VX_types.toml   ───────┤
  (共享 ABI 契约)      └── gen_config.py ──► build/sw/VX_types.h   ──► SW/runtime（公共 ABI）
```

两条隔离带由两个 CI 脚本守卫：
- `ci/check_config_boundary.sh`：禁止公共头文件 include `VX_config.h`。
- `ci/check_sw_sim_boundary.sh`：强制 sw/kernel 与 hw/sim 双向隔离。

这两个脚本的具体用法是 u2-l3 的主题；本讲只需理解它们「为什么存在」。

#### 4.3.3 源码精读

[build_configuration_system.md:37-44](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/build_configuration_system.md#L37-L44) 是官方的值流图：明确画出 `VX_config.toml` 同时生成 `.vh`（给 RTL）和 `.h`（给 sim/runtime），并用 `--cflags` 生成 `-DVX_CFG_*` 给 kernel/test；`VX_types.toml` 则同时给 RTL 和 SW/runtime 提供契约头。

[build_configuration_system.md:46-49](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/build_configuration_system.md#L46-L49) 指出一个关键事实：RTL 通过 `hw/rtl/VX_define.vh` 同时 include 两份生成头，并直接引用 `` `VX_CFG_* `` / `` `VX_MEM_* `` 宏——**没有** SystemVerilog config package。换言之，所有工具（Verilator/Vivado/VCS）看到的都是最朴素的反引号宏，这是为了让每种综合/仿真工具表现一致（设计文档 §4 记录了曾尝试 typed package 但因 sv2v 工具兼容问题被回退的教训）。

[build_configuration_system.md:51-59](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/build_configuration_system.md#L51-L59) 是分层的核心论述：
- `VX_config.{h,vh}` 是 **HW/sim 私有**。
- `VX_types.{h,vh}` 是**共享 ABI**（`.vh` 给 RTL，`.h` 给 SW）。
- 真正的 HW↔SW 契约（设备内存映射、页表格式）被从 `VX_config.toml` **搬迁**进了 `VX_types.toml` 的 `[memmap]`/`[vm]`，并重新加前缀为 `VX_MEM_*`/`VX_VM_*`，**使得公共运行时头文件永远不会 include `VX_config.h`**。

为了让读者直观感受「契约」长什么样，再看一眼 `VX_types.toml` 的两个搬迁段（已在 4.1.3 引用过的 `[memmap]`/`[vm]`），以及软件最常用的一组 CSR：

[VX_types.toml:525-535](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L525-L535) 的 `[csr_gpgpu]`：`VX_CSR_NUM_THREADS=0xFC0`、`VX_CSR_NUM_WARPS=0xFC1`、`VX_CSR_NUM_CORES=0xFC2` 等。这正是 u1-l4 提到的「主机程序通过 `vx_device_query` 查询 NUM_CORES/NUM_WARPS/NUM_THREADS」所读取的 CSR 编号——软件和硬件必须对这组编号达成一致，所以它们在契约文件里。

#### 4.3.4 代码实践

这是一个**追踪型实践**：

1. **目标**：亲手追踪一个值的「消费者分布」。
2. **步骤**：
   - 选定 `VX_CFG_NUM_CORES`（在 `VX_config.toml`，硬件私有）和 `VX_CSR_NUM_CORES`（在 `VX_types.toml`，契约）。
   - 用 `grep -rn "NUM_CORES" hw/rtl sw/runtime sw/kernel`（或 IDE 搜索）统计它们各自被哪些目录引用。
3. **观察**：`VX_CFG_NUM_CORES` 主要出现在 RTL 实例化代码；`VX_CSR_NUM_CORES` 主要出现在 CSR 译码（RTL）和运行时/内核读取 CSR 的代码（SW）。
4. **预期结果**：你会看到契约值同时被 hw 和 sw 引用，而硬件私有值几乎只在 hw/sim 内部——这正是分层设计的可观测证据。
5. 如果无法在本地搜索，「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么公共运行时头文件 `sw/runtime/include/vortex2.h` 不能 `#include "VX_config.h"`，却可以引用 `VX_types.h` 里的内容？

**参考答案**：因为 `VX_config.h` 暴露的是硬件私有机密（缓存结构等），允许上层软件依赖它会破坏硬件演进自由；而 `VX_types.h` 是有意设计的共享 ABI 契约，软硬件本来就必须对齐。设计文档 §3 还指出 `vortex2.h` 甚至进一步「完全解耦」——它用内联的字面位位置，连 `VX_types.h` 都不直接依赖配置宏。

**练习 2**：设计文档 §4 提到「曾尝试 SystemVerilog typed-config package 但被回退」。回退的根本原因是什么？

**参考答案**：sv2v 工具会丢弃 `export pkg::*` 的通配符号，导致 yosys 的 ASIC 综合流程在 `L3_CACHE_SIZE` 等符号上失败；且不同工具对 package 编译顺序的处理不一致（Verilator 严格、Vivado 自动排序、VCS 对顺序敏感）。反引号宏是唯一被所有工具一致支持的表示方式。

---

### 4.4 VX_CFG_\* 宏命名空间与 _ENABLED 镜像

#### 4.4.1 概念说明

「宏命名空间」听起来抽象，其实是一条非常实用的纪律：**键名怎么拼，决定了它是什么东西**。`gen_config.py` 没有任何命名逻辑，它只是把 TOML 里的键名原样输出成宏名——所以「这个键该叫什么」完全由作者在 TOML 里用拼写来表达。设计文档第 3 节定义了这套拼写约定。

本节最关键、也最容易踩坑的概念是 **`_ENABLE` 与 `_ENABLED` 的镜像关系**。请注意它们只差一个字母 `D`：

- `VX_CFG_L2_ENABLE`（布尔）：你**唯一需要手写**的开关，表示「是否开启 L2 缓存」。
- `VX_CFG_L2_ENABLED`（整数 0/1）：由生成器**自动派生**，用于算术/位运算场景。

#### 4.4.2 核心流程

`_ENABLE` 与 `_ENABLED` 各自服务于不同的消费者：

| 形式 | 类型 | 用途 | 典型消费者 |
|------|------|------|-----------|
| `X_ENABLE` | 布尔 | `` `ifdef ``/`#ifdef` 的存在性门控 | RTL 条件编译、C++ 条件编译 |
| `X_ENABLED` | 整数 0/1 | 算术、`localparam` 计算、位打包 | 如 `VX_CFG_MISA_*` 位或运算 |

为什么需要两种？因为在 Verilog/C 里，`` `ifdef `` 需要的是「宏有没有被定义」，而把布尔值塞进位或运算 `<<` 又需要整数。同一个开关要同时满足两种用法，于是生成器自动产出两个伴生形式。

派生宏的命名规则（设计文档 §3.2）有三类，绝不能混用：

1. **配置旋钮** → 带 `VX_CFG_` 前缀（用户可用 `CONFIGS=-D...` 设置）。
2. **RTL/C++ 内部的派生标志** → 裸名，写在 `hw/rtl/VX_define.vh`（如 `TCU_META_ENABLE`）。
3. **TOML 内部专用辅助量**（只被其它 `expr:` 引用，从不被 RTL/C++ 看到）→ 全小写私有名，生成器**不输出**。

#### 4.4.3 源码精读

[VX_config.toml:1](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L1) 文件第一行就给本节定了调：「Each `VX_CFG_X_ENABLE` boolean auto-generates its integer mirror `VX_CFG_X_ENABLED` (0/1); author only the boolean.」（每个 `VX_CFG_X_ENABLE` 布尔量都会自动生成整数镜像 `VX_CFG_X_ENABLED`；作者只写布尔量）。

**证据一：开关本身长这样（你只写这些）。**

[VX_config.toml:13-17](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L13-L17) 列出一组 `*_ENABLE` 布尔开关：`ICACHE_ENABLE`、`DCACHE_ENABLE`、`LMEM_ENABLE`、`L2_ENABLE = false`、`L3_ENABLE = false`。注意全文没有任何手写的 `L2_ENABLED`。

**证据二：`_ENABLED` 在哪里被消费。**

[VX_config.toml:360-366](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L360-L366) 的 `[isa_signatures]` 段：`VX_CFG_MISA_EXT` 用一长串 `($VX_CFG_L2_ENABLED << 2) | ($VX_CFG_L3_ENABLED << 3) | ...` 把各扩展的开关位打包成一个 MISA 签名整数。这里**必须用整数 `_ENABLED`**，因为要左移和按位或；而判断「要不要编译某段代码」时则用 `_ENABLE`。

**证据三：生成器如何自动产出镜像。**

[gen_config.py:1156-1162](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1156-L1162) 的 `_emit_enabled_companion_unresolved`：对每个 `X_ENABLE` 键，输出一个 `X_ENABLED`，其值用 `ifdef X_ENABLE` 决定为 1，且整体被 `ifndef X_ENABLED` 守护以保持可被 `-D` 覆盖。

[gen_config.py:1413-1414](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1413-L1414)（verilog/cpp 输出）与 [gen_config.py:1467-1468](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1467-L1468)（cflags 输出）分别在三种输出格式里追加 `X_ENABLE → X_ENABLED` 镜像，注释都写明「`X_ENABLE -> X_ENABLED mirror`」。

[gen_config.py:1314-1322](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1314-L1322) 的解析器还会在求值期**自动推导** `_ENABLED`：当某个 `expr:` 引用 `$VX_CFG_X_ENABLED` 时，解析器返回 `1 if X_ENABLE else 0`。这就保证了即便 toml 里没写 `_ENABLED`，表达式也能引用它——「两者不可能漂移，一个特性的开关真相始终在单一布尔量里」。

**证据四：派生宏不是 `VX_CFG_*`。**

[VX_config.toml:11](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L11) 的 `dpi_is_enabled = "expr: $SV_DPI"` 是全小写私有名——它只在 toml 内部被 [VX_config.toml:103](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L103) 的 `VX_CFG_FPU_TYPE` 表达式引用，生成器不会把它输出到任何头文件。

[VX_config.toml:112-113](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L112-L113) 的 `fpu_dsp_quartus` / `fpu_dsp_vivado` 同理，是仅供 `expr:` 链式引用的小写辅助量。

[VX_config.toml:155-159](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L155-L159) 的 `l2_is_llc` / `dcache_is_llc` / `l3_is_llc` / `single_core` / `single_cluster` 也是小写私有辅助，被 [VX_config.toml:179](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L179) 等 `WRITEBACK` 表达式引用，用来表达「某级缓存是否同时是 LLC 和唯一一致性点」这类结构推导。

[build_configuration_system.md:75-86](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/build_configuration_system.md#L75-L86)（§3.1）是 `_ENABLED` 镜像的权威说明：「不要在 TOML 里手写 `_ENABLED`，只写 `_ENABLE` 布尔；镜像是生成的」。

[build_configuration_system.md:88-105](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/build_configuration_system.md#L88-L105)（§3.2）是「派生宏不是 `VX_CFG_*`」的权威说明：`VX_CFG_*` 保留给真正可配置的旋钮；纯派生量要么是 `VX_define.vh` 里的裸名，要么是 toml 里的小写私有局部量。

#### 4.4.4 代码实践

这是本讲的**主线实践**（对应规格里的实践任务）：

1. **目标**：在 `VX_config.toml` 中找到三个规模参数，并用自己的话解释 `_ENABLE` 与 `_ENABLED` 的区别。
2. **操作步骤**：
   - 打开 `VX_config.toml`，记录：
     - `VX_CFG_NUM_CORES` 的值（在 `[platform]` 段，应为 `1`）。
     - `VX_CFG_NUM_WARPS` 的值（在 `[pipeline]` 段，应为 `4`）。
     - `VX_CFG_NUM_THREADS` 的值（在 `[pipeline]` 段，应为 `4`）。
   - 阅读 [build_configuration_system.md §3](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/build_configuration_system.md#L63-L105)（尤其是 §3.1）。
   - 在 toml 里找一个布尔开关（如 `VX_CFG_L2_ENABLE = false`），再在 `[isa_signatures]` 段找到它对应的 `VX_CFG_L2_ENABLED` 被左移打包的位置。
3. **需要观察的现象**：你会发现 toml 里**只有** `L2_ENABLE`，**没有** `L2_ENABLED`；但 `MISA_EXT` 表达式却引用了 `$VX_CFG_L2_ENABLED`。
4. **预期结果**：你能解释——`_ENABLE` 是作者手写的布尔开关（存在性门控），`_ENABLED` 是生成器自动派生的整数镜像（算术/位运算用），两者不会漂移，因为镜像是 `gen_config.py` 在三种输出格式里统一生成的（见 4.4.3 证据三）。
5. 若本地已 configure，可打开 `build/sw/VX_config.h` 搜索 `L2_ENABLE` 与 `L2_ENABLED`，亲眼确认两者都被生成、且 `L2_ENABLED` 的值为 `0`（因为默认 `L2_ENABLE=false`）。若头文件不存在或与 toml 不符，「待本地验证」或检查是否 stale。

> 重要：不要试图在 `VX_config.toml` 里手写 `VX_CFG_L2_ENABLED = 0`——这是被明确禁止的（§3.1），会破坏「单一布尔真相」的原则。

#### 4.4.5 小练习与答案

**练习 1**：下面这条 `expr:` 同时用到了本讲多个概念，逐字解释它：

```
VX_CFG_DCACHE_WRITEBACK = "expr: int($dcache_is_llc and $single_core)"
```
（[VX_config.toml:179](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L179)）

**参考答案**：`dcache_is_llc` 和 `single_core` 都是**小写私有辅助量**（见 [VX_config.toml:156-158](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L156-L158)），它们本身又由 `VX_CFG_L2_ENABLED`/`VX_CFG_L3_ENABLED`（自动镜像）和 `VX_CFG_NUM_CORES`/`VX_CFG_NUM_CLUSTERS`（字面量）派生。整句含义：「仅当 dcache 是 LLC 且系统是单核时，才允许 dcache 写回」——因为多核下私有缓存的脏数据无法对其它核可见，必须保持写穿。`int(...)` 把布尔转成 0/1 整数赋给一个 `VX_CFG_*` 旋钮。

**练习 2**：如果你要新增一个「是否开启某加速器」的开关，应该写 `VX_CFG_FOO_ENABLE = false` 还是 `VX_CFG_FOO_ENABLED = false`？后续在 MISA 里引用它时该用哪个？

**参考答案**：只写 `VX_CFG_FOO_ENABLE = false`（布尔开关，由作者手写）。在 MISA 位打包等算术场景里引用 `$VX_CFG_FOO_ENABLED`（整数镜像，自动生成，无需也不应手写）。

## 5. 综合实践

把本讲四个模块串起来，完成一次「配置变更影响分析」：

**任务**：假设你想把默认配置从「单核、无 L2」改成「双核、开启 L2 缓存」，请基于本讲所学，预测需要改动哪些地方、会出现哪些派生变化。

**步骤**：

1. 在 `VX_config.toml` 里定位两个待改参数：
   - `VX_CFG_NUM_CORES`（[VX_config.toml:5](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L5)）。
   - `VX_CFG_L2_ENABLE`（[VX_config.toml:16](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L16)）。
2. 写出一份「影响清单」：
   - 改完后必须重新 `configure`（否则 stale `VX_config.h`）——依据 u1-l3 与本讲 4.2.4。
   - `VX_CFG_L2_ENABLE=true` 会自动让 `VX_CFG_L2_ENABLED=1`（本讲 4.4）。
   - `l2_is_llc` 会变为真（因为 `L2_ENABLED==1 and L3_ENABLED==0`，[VX_config.toml:155](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L155)），进而 `VX_CFG_L2_WRITEBACK` 在单 cluster 时变为 1（[VX_config.toml:205](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L205)）。
   - `VX_CFG_MISA_EXT` 签名的第 2 位（`L2_ENABLED << 2`）会从 0 变 1（[VX_config.toml:366](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L366)）。
   - 这是**硬件私有**改动，不应触碰 `VX_types.toml` 的任何契约（本讲 4.1/4.3）。
3. **观察/预期**：你应当能解释「为什么改一个布尔开关，会连锁改变 WRITEBACK 策略和 MISA 签名」——这正是 `expr:` 派生链的威力，也是「单一真相来源」的价值：你只改一处，生成器保证其它所有消费者一致。
4. **进阶（可选）**：对照 u1-l4，说明这次改动也可以不改 toml，而是用 `./ci/blackbox.sh --cores=2 --l2cache` 这样的旋钮临时覆盖（旋钮翻译成 `-DVX_CFG_NUM_CORES=2 -DVX_CFG_L2_ENABLE`）。请说明这两种方式的区别：改 toml 是改「默认基线」，旋钮是「临时覆盖」（本讲与 u1-l4）。

## 6. 本讲小结

- Vortex 用**两个 TOML 文件**作为单一真相来源：`VX_config.toml`（硬件构建配置，~245 键，HW/sim 私有）和 `VX_types.toml`（ISA/ABI 契约 + 搬迁来的 `[memmap]`/`[vm]`，软硬件共享）。
- 值由 `gen_config.py` 从 toml 流向 RTL（`.vh` 反引号宏）、sim/runtime（`.h`）、kernel/test（`-D` cflags）；RTL 通过 `VX_define.vh` 同时 include 两份生成头，没有 SystemVerilog config package。
- `VX_config.toml` 的值有四种写法：字面量、`expr:` 表达式（可引用其它键与自动派生量）、`[[enum]]`（受限取值）、`[[builtin]]`/`[[param]]`（外部变量声明）。`$VX_CFG_XLEN_64` 这类枚举派生标志由解析器自动产生。
- 硬件/软件分层由两个 CI 脚本守卫：公共运行时头文件不得 include `VX_config.h`；真正的 HW↔SW 契约（内存映射、页表格式）被搬到 `VX_types.toml`。
- 宏命名空间靠拼写表达：`VX_CFG_*` 是可配置旋钮；`_ENABLE` 是作者手写的布尔开关，`_ENABLED` 是生成器自动派生的整数镜像（两者不漂移）；纯派生量是 `VX_define.vh` 里的裸名或 toml 里的小写私有量（不被输出）。
- 一条反复出现的纪律：**改 toml 后必须重新 `configure`**，否则会读到 stale 的生成头文件。

## 7. 下一步学习建议

- 下一讲 **u2-l2（gen_config.py 与配置值流）** 会打开生成器本体，讲清它如何把一个 toml 解析为 cflags/cpp/verilog 三种输出，以及 `configure` 如何驱动它。本讲多次引用了 `gen_config.py` 的片段，u2-l2 会把它们连成完整的处理流水线。
- 下一讲 **u2-l3（硬件/软件分层与边界检查）** 会深入两个 boundary 检查脚本，演示 `check_config_boundary.sh` / `check_sw_sim_boundary.sh` 如何强制执行本讲 4.3 提到的隔离规则。
- 想立刻看到配置效果：复习 u1-l4，用 `./ci/blackbox.sh --cores=2 --l2cache` 跑一次，体会「旋钮覆盖基线」与本讲「改 toml 改基线」的关系。
- 延伸阅读：`docs/designs/build_configuration_system.md` §4 记录了「未实现/已回退」的方向（typed package、RTL Sv39、MISA caps 泄漏），对理解「为什么是这样设计」很有帮助。
