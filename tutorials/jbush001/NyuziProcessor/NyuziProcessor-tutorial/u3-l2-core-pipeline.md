# 单核流水线总览

## 1. 本讲目标

本讲是「硬件顶层与流水线全景」单元的第二讲。上一讲（u3-l1）从最顶层看了 `nyuzi.sv` 如何把多个核、L2 缓存、IO 互连、片上调试器拼成一颗芯片；本讲要**钻进单个核内部**，看清一条指令从「被取出」到「写回寄存器」一路经过哪些处理阶段。学完后你应当能够：

- 按顺序说出 `core.sv` 实例化的全部流水线阶段模块（取指标签/数据 → 解码 → 线程选择 → 操作数 fetch → 执行 → 写回），并说出每个阶段的职责。
- 解释为什么操作数 fetch 之后会**分叉成三条并行执行路径**（访存、整数、浮点），以及它们在写回阶段如何重新汇合。
- 读懂 `core.sv` 的「信号命名前缀」约定（`ift_`、`ifd_`、`id_`、`ts_`、`of_`、`dt_`、`dd_`、`ix_`、`fx1_…fx5_`、`wb_`），从而能在上千行自动连线里快速定位数据流向。
- 理清 `control_registers`、`l1_l2_interface`、`io_request_queue`、`performance_counters` 这几个「辅助模块」与主流水线、与核外（L2/IO/调试器）之间的关系。

本讲只画**全景地图**：讲各级模块叫什么、连在哪、数据怎么流。至于每一级内部的算法细节——比如记分牌怎么判冒险、浮点五级怎么做对阶舍入、L1 缓存怎么做虚拟索引/物理标签、trap 怎么精确回滚——都留给后续 u4～u7 各专题讲义展开。本讲的目标是让你**先有骨架，再填血肉**。

## 2. 前置知识

开始前请确认你已经理解前面几讲建立的几个概念：

- **GPGPU + 多线程**（u1-l1、u2-l1）：Nyuzi 一个核默认有 4 个硬件线程（`THREADS_PER_CORE = 4`），每个线程拥有独立的 32 个标量寄存器和 32 个向量寄存器；核内有 16 条向量通道（`NUM_VECTOR_LANES = 16`）做 SIMD。
- **标量/向量数据类型**（u2-l1）：`scalar_t` 是 32 位，`vector_t` 是 16 个 `scalar_t` 拼成的 512 位向量。
- **指令会自带「走哪条流水线」的标记**（u2-l1 提到过 `pipeline_sel`）：每条 32 位指令解码后会被填进一个 `decoded_instruction_t` 结构，其中有一个字段 `pipeline_sel` 决定它走整数、浮点还是访存通路。本讲会用这个字段解释「三分流」。
- **顶层连接**（u3-l1）：`nyuzi.sv` 把若干个 `core` 实例、`l2_cache`、`io_interconnect`、`on_chip_debugger` 连起来；核与核外通过 L2 请求/响应、IO 请求/响应、JTAG、线程使能等信号交互。本讲的 `core` 模块端口正是这些信号在「核这一侧」的落点。

一个贯穿全讲的阅读技巧：`core.sv` 用了 SystemVerilog 的 `.*` 通配连线 + Emacs `verilog-mode` 的 `AUTOLOGIC` 自动声明。这意味着**模块之间的连线几乎全部由「信号名」隐式匹配**。Nyuzi 用「两级前缀」给信号命名——前缀代表**产生该信号的阶段**：

| 前缀 | 产生信号的阶段 | 前缀 | 产生信号的阶段 |
| --- | --- | --- | --- |
| `ift_` | `ifetch_tag_stage`（取指标签） | `dd_` | `dcache_data_stage`（数据缓存数据） |
| `ifd_` | `ifetch_data_stage`（取指数据） | `ix_` | `int_execute_stage`（整数执行） |
| `id_` | `instruction_decode_stage`（解码） | `fx1_`…`fx5_` | `fp_execute_stage1`…`5`（浮点五级） |
| `ts_` | `thread_select_stage`（线程选择） | `wb_` | `writeback_stage`（写回） |
| `of_` | `operand_fetch_stage`（操作数 fetch） | `dt_` | `dcache_tag_stage`（数据缓存标签） |

辅助模块也有固定前缀：`cr_`（control_registers）、`l2i_`（l1_l2_interface）、`ior_`（io_request_queue）、`sq_`（store queue，由 l1_l2_interface 输出）。记住这张表，你就能在 `core.sv` 上百行 `AUTOLOGIC` 声明里「按前缀读数据流」。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，辅以各阶段文件的「端口注释」佐证数据流向：

| 文件 | 角色 |
| --- | --- |
| [`hardware/core/core.sv`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv) | **本讲主角**。单核顶层：声明核的全部端口、自动连线、实例化 14 个流水线阶段 + 4 个辅助模块。 |
| [`hardware/core/defines.svh`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh) | 全局类型与常量。本讲用到 `pipeline_sel_t`（三选一）、`decoded_instruction_t`（解码结构）、`NUM_VECTOR_LANES` 等。 |
| [`hardware/core/operand_fetch_stage.sv`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/operand_fetch_stage.sv) | 操作数 fetch。其端口注释直接写明它「扇出」到三条执行路径。 |
| [`hardware/core/writeback_stage.sv`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv) | 写回。端口注释写明它「从三条流水线里选结果」。 |
| [`hardware/core/dcache_tag_stage.sv`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv) / [`dcache_data_stage.sv`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv) | L1 数据缓存的标签级与数据级，二者一起构成「访存路径」。 |
| [`hardware/core/int_execute_stage.sv`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv) | 整数执行（单周期）。 |
| [`hardware/core/fp_execute_stage1.sv`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage1.sv)（及 stage2–5） | 浮点五级流水线。 |

## 4. 核心概念与源码讲解

### 4.1 流水线各级

#### 4.1.1 概念说明

「流水线」（pipeline）是处理器最经典的加速结构：把一条指令的处理切成若干**级**（stage），每一级在一个时钟周期内做完自己那部分工作，然后把中间结果交给下一级。就像工厂流水线一样，虽然单条指令要花好几个周期才能走完全程，但因为每个周期都有新指令进入第一级、有指令从最后一级出来，整体吞吐率能做到「接近每周期一条」。

Nyuzi 的单核是一条**多线程、可变长、分数执行**的流水线：

- **多线程**：前端按线程轮流取指、发射（`THREADS_PER_CORE` 个硬件线程共享同一条流水线），借此在长延迟操作（如缓存缺失）时切换到别的线程，隐藏延迟。
- **可变长**：指令在操作数 fetch 之后**分叉**——简单整数运算只走 1 级就到写回，浮点要走 5 级，访存视命中与否还要等更久。所以不同指令「在流水线里停留的级数」不一样。
- **分数执行**：指令根据自身类型走不同的执行通路（访存/整数/浮点），最后在写回阶段汇合。

整个核的「身躯」由 [core.sv:364-377](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L364-L377) 这一段实例化决定。这十几行就是整条流水线的「骨架」，值得逐行认清：

```systemverilog
ifetch_tag_stage #(.RESET_PC(RESET_PC)) ifetch_tag_stage(.*);
ifetch_data_stage ifetch_data_stage(.*);
instruction_decode_stage instruction_decode_stage(.*);
thread_select_stage thread_select_stage(.*);
operand_fetch_stage operand_fetch_stage(.*);
dcache_data_stage dcache_data_stage(.*);
dcache_tag_stage dcache_tag_stage(.*);
int_execute_stage int_execute_stage(.*);
fp_execute_stage1 fp_execute_stage1(.*);
fp_execute_stage2 fp_execute_stage2(.*);
fp_execute_stage3 fp_execute_stage3(.*);
fp_execute_stage4 fp_execute_stage4(.*);
fp_execute_stage5 fp_execute_stage5(.*);
writeback_stage writeback_stage(.*);
```

注意每个实例都用 `.*` 通配连线——它们没有显式写端口映射，全靠信号名匹配（见 4.1.2 的阅读方法）。

#### 4.1.2 核心流程

一条标量加法指令 `add_i s0, s1, s2` 从取指到写回，依次经过这些阶段（这里先给「直觉版」流程，细节符号见 4.1.3）：

1. **取指标签级 `ifetch_tag_stage`（`ift_`）**：算出下一条指令的 PC，查 ITLB 把虚拟 PC 翻成物理地址，读 I-Cache 的标签（tag）阵列，判断这一行指令在不在缓存里。如果缺失，就把对应线程挂起，等填充。
2. **取指数据级 `ifetch_data_stage`（`ifd_`）**：用上一级算出的命中路，从 I-Cache 数据阵列读出真正的 32 位指令字，发给解码级。
3. **解码级 `instruction_decode_stage`（`id_`）**：把 32 位指令「翻译」成一个结构体 `decoded_instruction_t`（操作码、操作数来源、掩码、走哪条流水线……）；如果当前线程有挂起中断，就**把这条指令替换成一条 trap 指令**（实现精确中断，详见 u7）。
4. **线程选择级 `thread_select_stage`（`ts_`）**：在 4 个线程里**轮询**挑一个可以发射的，用记分牌（scoreboard）检查这条指令的操作数有没有被流水线里更早的指令占用（RAW/WAW 冒险）；有冒险就推迟，没冒险就放行。
5. **操作数 fetch 级 `operand_fetch_stage`（`of_`）**：根据解码结果，从标量/向量寄存器文件里读出真正需要的操作数，选择立即数，算出向量掩码。到这里，指令已经「备齐了原料」，准备进执行级。
6. **执行级**（分叉，见 4.2）：整数运算进 `int_execute_stage`，浮点运算进 `fp_execute_stage1…5`，访存进数据缓存两级。
7. **写回级 `writeback_stage`（`wb_`）**：从三条执行路径里选出本周期到达的结果，写回寄存器文件；同时统一处理回滚（分支、缓存缺失、异常）。

**如何用 `core.sv` 验证这条流向**：因为用了 `.*`，你无法在实例化语句里直接看到「谁连到谁」，但可以靠两个线索——

- **信号前缀 = 产生方**。例如 `of_instruction`（`of_` 前缀）由 `operand_fetch_stage` 产生，那么任何模块端口里出现 `input ... of_instruction` 的，就是它的消费者。
- **端口注释**。每个 `.sv` 文件的端口声明上方都写了 `// From xxx` / `// To xxx`，直接点明上下级关系。

例如 `operand_fetch_stage.sv` 的输出端口注释 [operand_fetch_stage.sv:36](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/operand_fetch_stage.sv#L36) 写着 `// To fp_execute_stage1/int_execute_stage/dcache_tag_stage`——一句话点破「操作数 fetch 同时喂给三条执行路径」。

> **反直觉提醒**：实例化顺序里 `dcache_data_stage`（第 369 行）排在 `dcache_tag_stage`（第 370 行）**前面**，但数据流是 **tag → data**：`operand_fetch` 先进 `dcache_tag_stage`（读标签 + TLB 翻译），再进 `dcache_data_stage`（判命中 + 读数据）。实例化顺序不等于数据流顺序，阅读时要以前缀链 `of_ → dt_ → dd_ → wb_` 为准。

#### 4.1.3 源码精读

**核的端口（核与外部的边界）**。`core` 模块在 [core.sv:26-68](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L26-L68) 声明端口，参数 `CORE_ID`/`RESET_PC`/`NUM_INTERRUPTS` 来自顶层 `nyuzi.sv`（u3-l1）。端口分四组：

- **控制类**：`thread_en`（线程使能位图）、`interrupt_req`（外部中断）、`clk`/`reset`。
- **L2 接口**：`l2_ready`/`l2_response*`（来自 L2）与 `l2i_request*`（发往 L2）——这是核与共享 L2 缓存的通道。
- **IO 接口**：`ii_*`（IO 响应回核）与 `ior_*`（核发 IO 请求）——非缓存外设寄存器走这里。
- **调试接口**：`ocd_*`（on-chip debugger 注入指令、读写数据）。
- **挂起/恢复**：`cr_suspend_thread`/`cr_resume_thread`（`TOTAL_THREADS` 位），告诉顶层哪些线程被软件挂起/恢复。

**流水线骨架**。如 4.1.1 所列，[core.sv:364-377](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L364-L377) 实例化了 14 个流水线模块；其中 `ifetch_tag_stage` 还带参数 `.RESET_PC(RESET_PC)`，把复位后第一条指令的地址传进去。

**自动连线声明区**。`/*AUTOLOGIC*/` 标记下的 [core.sv:77-359](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L77-L359) 是 verilog-mode 自动生成的几百条信号声明，每条都带注释 `// From <阶段> of <文件>`——这正是按前缀读数据流的「索引表」。例如 [core.sv:315-322](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L315-L322) 列出 `of_instruction`、`of_operand1`、`of_operand2`、`of_store_value` 都「From operand_fetch_stage」，说明它们是操作数级的输出。

**解码结构里的流水线选择字段**。`pipeline_sel` 字段定义在 `decoded_instruction_t` 中 [defines.svh:278](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L278)，其取值由枚举 `pipeline_sel_t` 给出 [defines.svh:233-237](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L233-L237)：

```systemverilog
typedef enum logic [1:0] {
    PIPE_MEM,
    PIPE_INT_ARITH,
    PIPE_FLOAT_ARITH
} pipeline_sel_t;
```

每条指令在解码阶段就被打上这三者之一，下游执行级据此决定「这条指令是不是给我的」。

#### 4.1.4 代码实践

**实践目标**：用「信号前缀链」亲自验证一条整数指令的数据流，而不靠记忆。

**操作步骤（源码阅读型）**：

1. 打开 [core.sv:77](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L77) 开始的 `AUTOLOGIC` 区。
2. 从「产生方」追到「消费方」。例如：
   - 找 `id_instruction`（解码级输出）——它的消费方注释会指向 `thread_select_stage`；
   - 找 `ts_instruction`（线程选择级输出）——消费方指向 `operand_fetch_stage`；
   - 找 `of_instruction`（操作数级输出）——会看到它被 `int_execute_stage`、`fp_execute_stage1`、`dcache_tag_stage` **三个模块**消费。
3. 在 [operand_fetch_stage.sv:36](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/operand_fetch_stage.sv#L36) 核对端口注释 `// To fp_execute_stage1/int_execute_stage/dcache_tag_stage`，确认「一拖三」。

**需要观察的现象**：`of_` 前缀的信号同时出现在三个执行级模块的 `input` 列表里；而 `ix_`、`fx5_`、`dd_` 前缀的信号都汇拢到 `writeback_stage` 的输入里。

**预期结果**：你能不打开任何子模块内部实现，仅凭 `core.sv` 的 AUTOLOGIC 注释和各文件端口注释，画出 `id → ts → of → {ix | fx1..5 | dt→dd} → wb` 的链路。「待本地验证」：不同编辑器折叠 AUTOLOGIC 的方式不同，建议展开后逐行扫读注释列。

#### 4.1.5 小练习与答案

**练习 1**：`core.sv` 里实例化 `dcache_data_stage` 在 `dcache_tag_stage` 之前，但为什么数据流是「tag 在前、data 在后」？

> **答案**：实例化顺序只是源码书写顺序，与运行时数据流无关（因为 `.*` 按名字连线）。数据流由端口注释决定：`dcache_tag_stage` 的输入是 `// From operand_fetch_stage`，`dcache_data_stage` 的输入是 `// From dcache_tag_stage`——所以指令先到 tag 级（读标签、查 TLB），再到 data 级（判命中、读数据）。tag 级有一拍延迟用来取标签阵列，结果交给下一拍（即 data 级）判定。

**练习 2**：核里默认有几个硬件线程？它们在流水线里是如何「共存」的？

> **答案**：默认 `THREADS_PER_CORE = 4`。它们共享同一条流水线，但寄存器文件按线程分体（`operand_fetch_stage` 里寄存器地址高位拼上线程号，见 `read_addr={ts_thread_idx, ...}`）。前端 `thread_select_stage` 每周期轮询挑一个线程发射，于是 4 个线程的指令交替进入流水线，互相填补对方的延迟空泡。

**练习 3**：`decoded_instruction_t` 里的 `pipeline_sel` 字段是谁填的？它对下游有什么用？

> **答案**：由解码级 `instruction_decode_stage` 根据指令操作码填写（取值 `PIPE_MEM` / `PIPE_INT_ARITH` / `PIPE_FLOAT_ARITH`，定义见 [defines.svh:233-237](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L233-L237)）。下游各执行级用它判断「这条指令归不归我处理」，写回级也据此从正确的执行路径选结果。

---

### 4.2 执行路径分流：三条并行的执行通路

#### 4.2.1 概念说明

操作数 fetch 之后，指令并没有进入「唯一的执行级」，而是**分叉**进三条并行通路。为什么要分叉？因为不同运算的硬件成本和时序差别极大：

- **访存路径（PIPE_MEM）**：要查 TLB、读标签、读数据阵列、处理命中/缺失，天然需要两级（tag + data）甚至在缺失时要挂起等待。load/store、控制寄存器访问、缓存控制指令都走这里。
- **整数路径（PIPE_INT_ARITH）**：整数加减、逻辑、移位、比较、分支解析——这些是「简单且快」的运算，一个周期就能算完，用单级 `int_execute_stage` 即可。注意一个反直觉点：浮点**倒数估计**（`reciprocal`）虽然结果是浮点，但用 ROM 查表也是单周期，所以也走整数路径（见 [int_execute_stage.sv:27-28](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L27-L28) 的注释）。
- **浮点路径（PIPE_FLOAT_ARITH）**：浮点加减乘、整数乘法（复用浮点乘法器）、`ftoit` 等，运算逻辑复杂（对阶、规整、舍入），需要 5 级流水才能跑到高频，于是有 `fp_execute_stage1…5`。

这「三路并行 + 长度不等」带来一个关键后果：**指令可能乱序到达写回级**。一条浮点指令（要走 5 级）如果紧跟在一条整数指令（只走 1 级）后面，反而会晚到写回。Nyuzi 必须在写回阶段小心处理这种乱序，并保证异常/中断仍然是「精确的」——这部分细节见 u5、u7。

#### 4.2.2 核心流程

操作数 fetch 输出的同一组信号（`of_instruction`、`of_operand1`、`of_operand2`、`of_mask_value`、`of_store_value`）被**广播**给三条路径，每条路径根据自己的 `pipeline_sel` 决定是否处理：

```
                         ┌─→ int_execute_stage  (PIPE_INT_ARITH, 1 级) ─────────────┐
operand_fetch ──(广播)──┼─→ fp_execute_stage1 → 2 → 3 → 4 → 5 (PIPE_FLOAT_ARITH, 5 级) ─┼─→ writeback
                         └─→ dcache_tag_stage → dcache_data_stage (PIPE_MEM, 2 级+) ────┘
```

三条路径的输出前缀分别是 `ix_`、`fx5_`、`dd_`，它们都汇入 `writeback_stage`，由后者三选一。

各路径内部的级数：

- **整数路径**：单级。`of_*` 进，`ix_*` 出。同一周期内完成 ALU 运算、分支解析（产出 `ix_rollback_en`/`ix_rollback_pc`）。
- **浮点路径**：五级。`fp_execute_stage1` 拆出符号/指数/尾数并算乘积；`stage2/3` 做对阶与加法；`stage4` 规整；`stage5` 舍入并产出最终 `fx5_result`（详见 u5-l3）。注意 `fx1` 的输入前缀是 `of_`，而 `fx2` 的输入是 `fx1_`、`fx3` 接 `fx2_`……逐级用上一级的中间量（`fx1_significand_le` → `fx2_significand_product` → `fx3_significand_product` → …）。
- **访存路径**：两级起。`dcache_tag_stage`（`dt_`）用上一周期读出的标签和 TLB 翻译结果，把指令连同物理地址传给 `dcache_data_stage`（`dd_`）；后者判命中、读数据。若缺失，`dd_cache_miss` 触发，经 `l1_l2_interface` 向 L2 请求并挂起线程（见 4.3、u6）。

#### 4.2.3 源码精读

**分流点的证据**。[operand_fetch_stage.sv:36](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/operand_fetch_stage.sv#L36) 的输出端口注释直接列出三个去向：

```systemverilog
    // To fp_execute_stage1/int_execute_stage/dcache_tag_stage
    output vector_t                   of_operand1,
    output vector_t                   of_operand2,
    ...
```

**汇合点的证据**。`writeback_stage` 模块开头的注释 [writeback_stage.sv:21-39](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L21-L39) 明说它「从三条流水线里选结果」，并解释了为什么异常仍需精确：

```systemverilog
// Instruction Pipeline Writeback Stage
// - Selects result from appropriate pipeline (memory, integer, floating point)
// - Aligns memory read results
// - Writes results back to register file
// - Handles rollbacks. ...
```

其输入端口也按三路分组：`fx5_*`（浮点）、`ix_*`（整数）、`dd_*`（访存），见 [writeback_stage.sv:46-78](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L46-L78)。

**浮点五级的中间量**。在 `core.sv` 的 AUTOLOGIC 区能看到 `fx1_`、`fx2_`、`fx3_`、`fx4_`、`fx5_` 各自的输出（例如 [core.sv:160-240](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L160-L240)），它们逐级把 `significand_product`、`add_exponent`、`guard/round/sticky` 等中间量向后传，最终在 `fx5_result` 汇成结果向量。

**整数路径也管浮点倒数**。[int_execute_stage.sv:27-28](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L27-L28) 注释：

```systemverilog
// (despite the name, this stage also handles floating point reciprocal
// estimates)
```

呼应 u2-l2 讲过的「`reciprocal` 用 ROM 查表、单周期、约 6 位精度」，所以它和整数 ALU 共用同一级。

#### 4.2.4 代码实践

**实践目标**：确认三条路径「同源广播、分别消费、写回归并」，并量出各自的级数。

**操作步骤（源码阅读型）**：

1. 在 [core.sv:315-322](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L315-L322) 找到 `operand_fetch_stage` 的输出（`of_operand1/2`、`of_instruction` 等）。
2. 分别打开下面三个文件，在它们的 `input` 端口里核对是否都消费了同一组 `of_*` 信号：
   - [int_execute_stage.sv:36-42](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L36-L42)
   - `fp_execute_stage1.sv` 的输入段
   - [dcache_tag_stage.sv:44-51](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L44-L51)
3. 在 [writeback_stage.sv:46-78](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L46-L78) 核对三路输出（`ix_*`/`fx5_*`/`dd_*`）都进了写回级。
4. 数级数：整数路径 1 级（`ix_`）、浮点 5 级（`fx1_…fx5_`）、访存 2 级（`dt_`/`dd_`）。

**需要观察的现象**：同一组 `of_*` 信号名出现在三个执行级的 input；三个执行级的输出前缀不同但都进 writeback。

**预期结果**：得到一张「3 路并行、长度 1/5/2」的对照表，能据此解释「为什么浮点指令会比紧跟其后的整数指令晚到写回」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `reciprocal`（浮点倒数估计）不走浮点五级流水，而走整数执行级？

> **答案**：因为它用 ROM 查表实现，单周期即可给出约 6 位精度的倒数估计（详见 u2-l2、u5-l3），时序上和整数 ALU 一样快，没必要进 5 级流水。所以 `int_execute_stage` 的注释特意说明「尽管名字叫整数级，它也处理浮点倒数估计」。

**练习 2**：如果一条浮点乘法（走 5 级）紧跟在一条整数加法（走 1 级）之后，且二者属于同一线程，谁先到写回级？

> **答案**：整数加法先到。二者在同周期离开操作数 fetch 后，整数路径 1 级就到写回，浮点路径要 5 级。这就造成了「后发射的指令先写回」的乱序现象——这正是 `writeback_stage` 注释里强调「指令可能乱序到达、异常仍需精确」的原因（细节见 u7-l3）。

**练习 3**：访存路径为什么是「tag 级 + data 级」两级，而不是一级？

> **答案**：因为 L1 数据缓存是组相联的，标签阵列读出来需要一拍延迟，且还要并行查 DTLB 做虚拟→物理翻译（缓存是虚拟索引/物理标签，见 [dcache_tag_stage.sv:22-37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L22-L37)）。把「读标签 + 翻译」放在 tag 级，「判命中 + 读数据」放在 data 级，可以拆分关键路径、提高主频。

---

### 4.3 辅助模块与外部接口

#### 4.3.1 概念说明

主流水线之外，`core.sv` 还实例化了 4 个「辅助模块」。它们不直接解码或执行指令，但主流水线的每一步都离不开它们提供的状态与服务：

| 模块 | 实例化处 | 前缀 | 核心职责 |
| --- | --- | --- | --- |
| `control_registers` | [core.sv:379-388](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L379-L388) | `cr_` | 存储并读写 28 个控制寄存器（线程号、trap 状态、flags、ASID、页目录、中断、性能计数选择…），向流水线提供 `cr_supervisor_en`/`cr_mmu_en`/`cr_interrupt_en`/`cr_current_asid` 等状态。 |
| `l1_l2_interface` | [core.sv:390](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L390) | `l2i_`/`sq_` | 处理 L1 缺失请求队列、store 队列与旁路、向 L2 发请求、收 L2 响应并回填 L1、做 snoop 一致性、唤醒被挂起的线程。 |
| `io_request_queue` | [core.sv:391](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L391) | `ior_` | 处理非缓存的外设寄存器（MMIO）访问：把 IO 请求排队发往核外，挂起线程等响应，响应回来后回滚重试。 |
| `performance_counters` | [core.sv:420-424](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L420-L424) | （计数值） | 按控制寄存器选定的事件，对核内性能事件计数（icache/dcache 命中缺失、分支、发射、退休等）。 |

#### 4.3.2 核心流程

四个辅助模块与主流水线的协作可以这样理解：

1. **`control_registers` 是「全局状态中心」**。几乎所有阶段都要问它要状态：取指/访存要问 `cr_mmu_en`（虚拟内存开了吗）、`cr_supervisor_en`（当前是特权态吗）、`cr_current_asid`（地址空间标识）；解码要问 `cr_interrupt_pending`/`cr_interrupt_en`（要不要插入中断）；分支要问 `cr_eret_address`（eret 回哪）；线程选择/写回要靠 `CR_SUSPEND/RESUME_THREAD` 改线程使能。控制寄存器的读写本身则是访存路径里的 `MEM_CONTROL_REG` 型指令（u2-l4），经 `dcache_data_stage` 路由进来。

2. **`l1_l2_interface` 是「L1 与 L2 之间的桥」**。当 `dcache_data_stage` 报告 `dd_cache_miss`，这个请求进入缺失队列，由 `l1_l2_interface` 打包成 `l2req_packet_t` 发给 L2（核端口 `l2i_request*`）；L2 把数据填回来（`l2_response*`），由它回填 L1 的标签/数据阵列（`l2i_dtag_update_*`/`l2i_ddata_update_*`）并发出 `l2i_dcache_wake_bitmap` 唤醒当初因缺失挂起的线程。store 操作走它内部的 store 队列（`sq_` 前缀），支持合并与旁路。这部分细节见 u6。

3. **`io_request_queue` 是「外设访问的等候室」**。当 `dcache_data_stage` 发现一次访问落在非缓存 IO 区（`dd_io_access`），它不走 L2，而是经 `dd_io_*` 信号把请求交给 `io_request_queue`，后者通过核端口 `ior_*` 发到核外的 `io_interconnect`。因为外设响应要等若干周期，线程会被挂起（`ior_pending`），响应回来后（`ii_*`）回滚取指重试。这就是 u1-l4 里讲过的「IO 访问让线程挂起」的硬件落点。

4. **`performance_counters` 是「仪表盘」**。`core.sv` 在 [core.sv:401-418](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L401-L418) 用一个大 `assign` 把全核 14 个性能事件（`ix_perf_cond_branch_taken`、`dd_perf_dcache_miss`、`ifd_perf_icache_miss`、`ts_perf_instruction_issue`、`wb_perf_instruction_retire` 等）拼成位向量 `perf_events`，交给 `performance_counters`，由它按控制寄存器 `CR_PERF_EVENT_SELECT*` 选出两路来计数（细节见 u11-l2）。

#### 4.3.3 源码精读

**辅助模块实例化**。[core.sv:379-391](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L379-L391)：

```systemverilog
control_registers #(
    .CORE_ID(CORE_ID),
    .NUM_INTERRUPTS(NUM_INTERRUPTS),
    .NUM_PERF_EVENTS(CORE_PERF_EVENTS)
) control_registers(
    .cr_perf_event_select0(perf_event_select[0]),
    .cr_perf_event_select1(perf_event_select[1]),
    .perf_event_count0(perf_event_count[0]),
    .perf_event_count1(perf_event_count[1]),
    .*);

l1_l2_interface #(.CORE_ID(CORE_ID)) l1_l2_interface(.*);
io_request_queue #(.CORE_ID(CORE_ID)) io_request_queue(.*);
```

注意 `control_registers` 带了三个参数（核号、中断数、性能事件数），并把性能计数选择/计数值与 `core` 内的 `perf_event_select`/`perf_event_count` 显式对接。

**性能事件位向量**。[core.sv:401-418](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L401-L418) 的 `assign perf_events = { ... }` 把 14 个来自各阶段（`ix_`、`dd_`、`ifd_`、`ts_`、`wb_`、`l2i_`）的事件位拼在一起，注释强调「这里的信号个数必须与 `defines.svh` 里的 `CORE_PERF_EVENTS` 一致」（[defines.svh:70](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L70) 定为 14）。

**`performance_counters` 实例化**。[core.sv:420-424](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L420-L424)，带 `NUM_EVENTS(CORE_PERF_EVENTS)` 和 `NUM_COUNTERS(2)`，即 14 个事件里选 2 路同时计数。

**调试器协作**。[core.sv:393-399](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L393-L399) 有两行 `always @(posedge clk)`，根据 `wb_inst_injected` 与 `wb_rollback_en` 计算 `injected_complete`/`injected_rollback`，回送给片上调试器（u11-l1），表示「调试器注入的那条指令执行完了没有」。

#### 4.3.4 代码实践

**实践目标**：把「辅助模块」与「主流水线」的关系画成一张依赖图，分清谁服务谁。

**操作步骤（源码阅读型）**：

1. 在 [core.sv:79-88](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L79-L88) 找到 `cr_` 前缀信号（`cr_supervisor_en`、`cr_mmu_en`、`cr_interrupt_en`、`cr_current_asid`、`cr_trap_handler`、`cr_tlb_miss_handler`、`cr_eret_address` 等），标注它们的消费阶段（取指、访存、解码、整数执行都会用到）。
2. 在 [core.sv:290-314](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L290-L314) 找到 `l2i_` 前缀信号，区分两类：更新 L1 缓存的（`l2i_dtag_update_*`、`l2i_ddata_update_*`、`l2i_*_wake_bitmap`）和发往 L2 的请求相关（经核端口 `l2i_request*`）。
3. 在 [core.sv:274-277](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L274-L277) 找到 `ior_` 信号（`ior_pending`、`ior_wake_bitmap`、`ior_read_value`、`ior_rollback_en`），标注它们连到 `io_request_queue`。

**需要观察的现象**：`cr_` 信号是「向流水线输出状态 + 接收写回级的 trap/syscall/eret」的双向关系；`l2i_*_wake_bitmap` 是「L2 响应回来唤醒线程」的回程通路。

**预期结果**：得到一张图，中央是主流水线，四周是 4 个辅助模块，箭头标明服务方向（例如 `control_registers → 流水线` 提供 flags/ASID；`流水线 → control_registers` 提供 `wb_trap`/`wb_eret`/`wb_syscall_index`）。

#### 4.3.5 小练习与答案

**练习 1**：一次 D-Cache 缺失，是哪个辅助模块负责向 L2 发请求并唤醒线程？

> **答案**：`l1_l2_interface`。`dcache_data_stage` 发出 `dd_cache_miss`/`dd_cache_miss_addr`/`dd_cache_miss_thread_idx`，由 `l1_l2_interface` 排队并打包成 `l2req_packet_t` 从核端口 `l2i_request*` 发出；L2 响应回来后，它用 `l2i_dtag_update_*`/`l2i_ddata_update_*` 回填 L1，并用 `l2i_dcache_wake_bitmap` 唤醒当初挂起的线程。

**练习 2**：为什么 IO 访问要专门用 `io_request_queue`，而不直接走 L2 缓存路径？

> **答案**：IO 访问针对的是外设寄存器（MMIO），既不能缓存、也不能投机预取，语义上必须「读就是真读一次外设、写就是立刻送达外设」。所以它绕开 L1/L2 缓存，由 `io_request_queue` 单独排队发往核外的 `io_interconnect`，并让发起线程挂起等待确定的响应（`ior_pending`/`ior_wake_bitmap`），响应回来后回滚重试。

**练习 3**：`perf_events` 这个位向量里为什么有 14 位？这个 14 是哪里规定的？

> **答案**：因为 [defines.svh:70](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L70) 把 `CORE_PERF_EVENTS` 定义为 14，而 [core.sv:401-418](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L401-L418) 的 `assign perf_events = {...}` 拼接的信号个数必须与之严格相等（注释里也强调了这一点），否则位宽不匹配。`performance_counters` 再从这 14 个事件里按控制寄存器选 2 路计数。

---

## 5. 综合实践

把本讲三个模块串起来，完成课程实践任务：**依据 `core.sv` 的实例化列表，绘制一张单核流水线数据流图**。

**任务描述**：

1. **画骨架**：以 [core.sv:364-377](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L364-L377) 为准，从左到右排出前端各级：`ifetch_tag_stage → ifetch_data_stage → instruction_decode_stage → thread_select_stage → operand_fetch_stage`。
2. **画分叉**：从 `operand_fetch_stage` 引出三条并行支路，分别标注：
   - 整数路径：`int_execute_stage`（标「1 级 / PIPE_INT_ARITH」）；
   - 浮点路径：`fp_execute_stage1 → 2 → 3 → 4 → 5`（标「5 级 / PIPE_FLOAT_ARITH」）；
   - 访存路径：`dcache_tag_stage → dcache_data_stage`（标「2 级+ / PIPE_MEM」）。
3. **画汇合**：三条支路都指向 `writeback_stage`，再由写回级连回 `operand_fetch_stage` 的寄存器文件（`wb_writeback_*`）。
4. **画辅助模块**：在主流水线四周画出 `control_registers`、`l1_l2_interface`、`io_request_queue`、`performance_counters`，用箭头标出关键服务线（例如 `control_registers → ifetch/dcache/decode` 提供 `cr_mmu_en`/`cr_supervisor_en`/`cr_interrupt_en`；`dcache_data_stage → l1_l2_interface` 的 `dd_cache_miss`；`l1_l2_interface → dcache` 的 `l2i_*_wake_bitmap`；`dcache_data_stage → io_request_queue` 的 `dd_io_*`）。
5. **画核边界**：在最外圈标出核端口——L2 请求/响应、IO 请求/响应、`thread_en`、`interrupt_req`、`ocd_*`、`cr_suspend/resume_thread`，呼应 u3-l1 的顶层连接。

**完成后，请回答**：

- 一条 `add_i`（整数加法）经过哪些阶段？答：`ifetch_tag → ifetch_data → decode → thread_select → operand_fetch → int_execute → writeback`。
- 一条 `mul_f`（浮点乘法）比 `add_i` 多走几级？答：多走浮点路径相对整数路径多出的级数（5 级 vs 1 级，多 4 级），所以会晚到写回——这是「乱序到达」的根源。
- 一次 D-Cache 缺失会走哪条辅助线？答：`dcache_data_stage` 报 `dd_cache_miss` → `l1_l2_interface` → 核端口 `l2i_request*` → L2 → 响应回填 + `l2i_dcache_wake_bitmap` 唤醒线程。

**提示**：这张图就是后续 u4（取指解码发射）、u5（执行单元）、u6（缓存与内存层次）、u7（虚拟内存与异常）各专题讲义的「目录页」——每一讲都是在放大本图里的某一段。建议把图画大贴在手边，学到哪一段就标亮哪一段。

## 6. 本讲小结

- `core.sv` 用 [core.sv:364-377](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L364-L377) 实例化 14 个流水线阶段，构成一条**多线程、可变长、分数执行**的流水线；前端按 `ifetch_tag → ifetch_data → decode → thread_select → operand_fetch` 顺序流动，后端分叉执行、写回收口。
- 阅读要靠**信号前缀**（`ift_/ifd_/id_/ts_/of_/dt_/dd_/ix_/fx1_..fx5_/wb_`）和各文件端口注释判断数据流向——因为全用 `.*` 通配连线，实例化语句本身看不出连接。
- 操作数 fetch 之后**分叉成三条并行执行路径**：访存（`dcache_tag → dcache_data`，2 级+）、整数（`int_execute`，1 级）、浮点（`fp_execute_stage1..5`，5 级），由解码阶段填写的 `pipeline_sel`（`PIPE_MEM`/`PIPE_INT_ARITH`/`PIPE_FLOAT_ARITH`）决定走哪条。
- 三条路径在 `writeback_stage` 汇合，写回级「从三条流水线里选结果」并统一处理回滚；因为路径长度不等，指令可能**乱序到达**写回，但异常/中断仍保持精确。
- 四个**辅助模块**服务主流水线：`control_registers`（全局状态/中断/CR 读写）、`l1_l2_interface`（L1 缺失与 store 队列、L2 回填与线程唤醒）、`io_request_queue`（MMIO 外设访问的排队与挂起）、`performance_counters`（按 CR 选定事件对 14 个性能位计数）。
- 核与外部的边界由 [core.sv:26-68](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L26-L68) 的端口定义：L2 接口、IO 接口、调试接口、`thread_en`/`interrupt_req`/`cr_suspend_thread`，正是 u3-l1 顶层连接在「核这一侧」的落点。

## 7. 下一步学习建议

本讲建立的是单核的「骨架地图」。接下来建议沿着数据流逐段放大：

- **想看取指与 I-Cache 细节** → 进入 u4-l1「指令取指与 I-Cache」，放大 `ifetch_tag_stage`/`ifetch_data_stage` 的 ITLB 查询、命中判定与缺失唤醒。
- **想看解码与中断替换** → 进入 u4-l2「指令解码」与 u7-l2「控制寄存器与中断」，看 `instruction_decode_stage` 如何把 32 位指令填成 `decoded_instruction_t`、如何用替换实现精确中断。
- **想看线程调度与冒险规避** → 进入 u4-l3「线程选择与记分牌」，看 `thread_select_stage` + `scoreboard.sv` 如何轮询发射、如何用记分牌阻止 RAW/WAW。
- **想看三条执行路径内部** → u5-l1（操作数 fetch）、u5-l2（整数执行与分支回滚）、u5-l3（浮点五级流水）逐个放大。
- **想看缓存层次与 IO** → u6（L1/L2/AXI/IO 总线）放大 `dcache_*`、`l1_l2_interface`、`l2_cache`、`io_request_queue`。
- **想看异常与回滚** → u7-l3「Trap 处理与回滚」放大 `writeback_stage` 的 `wb_trap`/`wb_rollback_*` 机制。

建议同步动手：把综合实践里画的数据流图保留好，每学完一讲就把对应那一段的细节补进图里——到最后你会得到一张自己画的、可下钻的 Nyuzi 单核微架构图。
