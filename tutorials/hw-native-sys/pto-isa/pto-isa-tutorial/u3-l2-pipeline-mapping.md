# 指令到硬件流水线的映射

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 PTO 的八条硬件流水线（S/V/MTE1/MTE2/MTE3/M/FIX/ALL）各自的职责与编号，以及「每对流水线有 8 个事件 ID」这一资源模型。
2. 读懂 `event.hpp` 中 `OpPipeEntry` + `PTO_DEFINE_OP_PIPE` 宏构成的「指令 → 流水线」编译期映射表，并能亲手统计每条流水线上挂了多少条指令。
3. 理解事件 ID 的复用规则：`EventIdCounter` 按 (源流水线, 目的流水线) 二元组隔离、0~7 轮转分配，以及占用检测断言的含义。
4. 能独立分析一段内核（本讲以 `demos/baseline/add/csrc/kernel/add_custom.cpp` 为例）的乒乓双缓冲事件编排：预置旗标、循环内 set/wait 配对、收尾清账，以及「事件 ID 随缓冲槽位交替」的分配策略。

本讲是 u3-l1（事件与同步模型）的直接续篇：u3-l1 讲清了 `set_flag`/`wait_flag` 的配对规则，本讲回答「规则里的 PIPE_V、PIPE_MTE2 这些名字从哪来、每条指令归哪条流水线、8 个 ID 怎么够用」。

## 2. 前置知识

### 2.1 复习：为什么要显式同步

昇腾 AI Core 内部是多流水线异步并行结构：搬入、计算、搬出走不同的硬件队列，**书写顺序不等于完成顺序**。上一讲（u3-l1）已给出规则：

- `set_flag(srcPipe, dstPipe, id)`：源流水线在排空该调用之前的指令后，把旗标 `(srcPipe, dstPipe, id)` 置位；
- `wait_flag(srcPipe, dstPipe, id)`：目的流水线侧的后续指令阻塞等待该旗标；
- 三参数必须一致配对，set 在生产指令之后、wait 在消费指令之前。

本讲要补齐的拼图是：`srcPipe`/`dstPipe` 到底有哪几个取值、每条 PTO 指令「天生」属于哪条流水线。

### 2.2 本讲用到的 C++ 知识：模板特化

`event.hpp` 的映射表用了一个很朴素的 C++ 技巧——**模板全特化**：

```cpp
// 主模板：默认答案
template <Op Op_>
struct OpPipeEntry { static constexpr pipe_t pipe = PIPE_ALL; };

// 全特化：针对某个具体 Op_ 给出改写答案
template <>
struct OpPipeEntry<Op::TADD> { static constexpr pipe_t pipe = PIPE_V; };
```

可以把主模板理解成「查表函数的默认返回值」，每个特化是「表里的一行」。它在**编译期**完成查表，运行期零开销；也因此可以用 `static_assert` 在编译期检查流水线合法性。不需要了解更多模板知识，看懂这两行注释即可。

### 2.3 一个容易搞混的点：constants.hpp 里没有事件常量

讲义规格里列了 `include/pto/common/constants.hpp`，需要澄清：事件相关的常量**不在**这个文件里——`EVENT_ID_MAX` 在 [include/pto/common/event.hpp:14](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L14)，`EVENT_ID0`~`EVENT_ID7` 在 [include/pto/common/cpu_stub.hpp:184-191](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/cpu_stub.hpp#L184-L191)。`constants.hpp` 定义的是块大小、repeat 长度等硬件「度量衡」（如 32 字节块 [include/pto/common/constants.hpp:23](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/constants.hpp#L23)、256 字节 repeat [include/pto/common/constants.hpp:20](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/constants.hpp#L20)），它们决定 MTE2/MTE3 搬运指令按什么粒度切_repeat——这是「为什么搬运独占一条流水线」的背景，本讲只点到为止。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [include/pto/common/event.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L21-L152) | 事件模型核心头 | `Op` 枚举、`OpPipeEntry`/`PTO_DEFINE_OP_PIPE` 映射表、`EventIdCounter`、`EventBase` |
| [include/pto/common/cpu_stub.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/cpu_stub.hpp#L52-L62) | CPU 模拟器替身层 | `pipe_t` 编号 0~7、`EVENT_ID0~7`、`set_flag`/`wait_flag` 空实现 |
| [include/pto/npu/a2a3/TSync.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/npu/a2a3/TSync.hpp#L24-L42) | a2a3 后端同步实现 | 映射表的消费方：`GetPipeByOpForA3`、`TSYNC_IMPL`、类型化 `Event` |
| [demos/baseline/add/csrc/kernel/add_custom.cpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L79-L113) | 多核 TADD demo 内核 | 乒乓双缓冲下事件 ID 的手工编排（本讲主教材） |
| [tests/cpu/st/testcase/tadd/tadd_kernel.cpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L34-L41) | ST 单缓冲对照样本 | 对照用：单缓冲下每通道只需一个 ID |
| [include/pto/common/constants.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/constants.hpp#L20-L41) | 硬件度量衡常量 | 背景知识：块/repeat 粒度（事件常量不在此文件，见 2.3） |

注意：`add_custom.cpp` 整体被 `#if __CCE_AICORE__ == 220 && defined(__DAV_C220_VEC__)` 包住（[add_custom.cpp:11](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L11)），只在 NPU（C220 即 A2/A3 代）编译——本讲对它的实践是**源码阅读与推演型**，理由见 4.4.4。

## 4. 核心概念与源码讲解

### 4.1 流水线编号：八条流水线与每对流水线的 8 个事件 ID

#### 4.1.1 概念说明

PTO 沿用昇腾 CCE 的流水线命名：一条指令发射后进入哪条硬件队列，由它的**类别**决定。事件同步发生在「队列与队列之间」，因此：

- `pipe_t` 是流水线的编号类型，共 8 个取值；
- `event_t` 是事件 ID，取值 0~7（`EVENT_ID0`~`EVENT_ID7`）；
- **事件的全名是三元组 `(srcPipe, dstPipe, id)`**——ID 不是全局资源，而是「每对流水线一套」。同一个 `EVENT_ID0`，在 (V→MTE2) 方向和 (MTE3→V) 方向是两个互不干扰的事件。

#### 4.1.2 核心流程

CPU 模拟器与 CostModel 链路共用的编号定义如下（真机上的 `pipe_t`/`set_flag`/`wait_flag` 来自 CCE 的 `kernel_operator.h`，见 [add_custom.cpp:13](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L13) 的包含；CostModel 侧注释也确认了同一套 0~7 编号，见 [include/pto/costmodel/perf_sim/pipe_model_queue_impl.inl:122](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/costmodel/perf_sim/pipe_model_queue_impl.inl#L122)）：

| 流水线 | 编号 | 职责（据映射表归纳） | 典型指令 |
| --- | --- | --- | --- |
| `PIPE_S` | 0 | 标量/控制/配置类 | `SCALAR`、`SETFMATRIX`、`SET_IMG2COL_*` |
| `PIPE_V` | 1 | 向量计算（UB 上逐元素/规约） | `TADD`、`TEXP`、`TROWSUM` |
| `PIPE_MTE1` | 2 | 片内搬运：Mat → Left/Bias/Right（UB/L1 → L0 方向） | `TMOV_M2L`、`TIMG2COL` |
| `PIPE_MTE2` | 3 | 搬入：GM → 片上 | `TLOAD`、`TPREFETCH` |
| `PIPE_MTE3` | 4 | 搬出：UB → GM | `TSTORE_VEC`、`TSTORE_MAT` |
| `PIPE_M` | 5 | Cube 矩阵乘 | `TMATMUL`、`TGEMV` |
| `PIPE_ALL` | 6 | 「全部流水线」兜底值（非真实队列） | 未登记映射的 Op 的默认值 |
| `PIPE_FIX` | 7 | Fixpipe：累加器 L0C 出结果 | `TSTORE_ACC`、`TMOV_A2V` |

#### 4.1.3 源码精读

[include/pto/common/cpu_stub.hpp:52-62](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/cpu_stub.hpp#L52-L62) 定义编号与 `pipe_barrier` 替身——`typedef int pipe_t`，随后 8 个 `const pipe_t` 常量按上表顺序取 0~7。

[include/pto/common/cpu_stub.hpp:184-191](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/cpu_stub.hpp#L184-L191) 用宏定义 `EVENT_ID0` 到 `EVENT_ID7`；配合 [include/pto/common/event.hpp:14](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L14) 的 `#define EVENT_ID_MAX 8`，构成「每对流水线 8 个 ID」的资源上限。

[include/pto/common/cpu_stub.hpp:123-124](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/cpu_stub.hpp#L123-L124) 是 CPU 模拟器上的空实现 `inline void set_flag(pipe_t, pipe_t, int) {}`——再次印证 u3-l1 的结论：CPU 模拟器不验证事件链，时序正确性要靠推演或 sim/NPU。

#### 4.1.4 代码实践

1. **实践目标**：把 8 个流水线编号变成「摸得到」的数字，并验证 CPU 替身层与上表一致。
2. **操作步骤**：新建 `pipes.cpp`（**示例代码**，非项目原有文件）：

   ```cpp
   #include <pto/pto-inst.hpp>
   #include <cstdio>

   int main()
   {
       std::printf("S=%d V=%d MTE1=%d MTE2=%d MTE3=%d M=%d ALL=%d FIX=%d\n",
                   PIPE_S, PIPE_V, PIPE_MTE1, PIPE_MTE2, PIPE_MTE3, PIPE_M, PIPE_ALL, PIPE_FIX);
       return 0;
   }
   ```

   编译运行：`g++ -std=c++20 -D__CPU_SIM -Iinclude -o pipes pipes.cpp && ./pipes`（在仓库根目录执行）。
3. **需要观察的现象**：输出的 8 个数字。
4. **预期结果**：`S=0 V=1 MTE1=2 MTE2=3 MTE3=4 M=5 ALL=6 FIX=7`（数值由 cpu_stub.hpp 中的常量直接决定，是确定性的；本讲义编写时未实际执行该程序，属确定性常量输出，如不符请以头文件为准）。
5. 若编译报头文件找不到，检查是否在仓库根目录、是否漏了 `-Iinclude`。

#### 4.1.5 小练习与答案

- **练习 1**：`set_flag(PIPE_V, PIPE_MTE3, EVENT_ID0)` 和 `set_flag(PIPE_V, PIPE_MTE2, EVENT_ID0)` 是同一个事件吗？
  **答案**：不是。事件全名是三元组 `(src, dst, id)`，两者目的流水线不同（MTE3 vs MTE2），是两个独立事件；ID 只在固定的 (src, dst) 二元组内命名。
- **练习 2**：一对流水线之间最多能同时挂多少个「已 set 未 wait」的事件？
  **答案**：8 个（`EVENT_ID0`~`EVENT_ID7`）。这也是 4.3 节轮转分配器模 8 的原因。

### 4.2 PTO_DEFINE_OP_PIPE 宏：编译期的「指令 → 流水线」查表

#### 4.2.1 概念说明

`pto::Op` 是指令的编译期「操作码」枚举（[include/pto/common/event.hpp:21-152](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L21-L152)，共 129 个 Op 外加 `OP_COUNT` 哨兵）。每条指令属于哪条流水线，就登记在紧随其后的映射表里。为什么用编译期查表而不是运行期 if-else？因为：

- 零运行期开销；
- 框架可以据此 `static_assert` 检查（如单流水线 barrier 只接受合法管道）；
- 类型化事件 `Event<SrcOp, DstOp>` 能在**实例化时**自动算出 `srcPipe`/`dstPipe`，用户只需写指令名，不用手抄流水线名。

#### 4.2.2 核心流程

映射机制三步：

1. **主模板兜底**：未登记的 Op 默认落到 `PIPE_ALL`（[event.hpp:154-157](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L154-L157)）。
2. **宏登记**：`PTO_DEFINE_OP_PIPE(Op, Pipe)` 展开为一份模板全特化（[event.hpp:159-163](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L159-L163)），逐行覆盖默认答案；表尾 `#undef` 收掉宏（[event.hpp:295](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L295)）。
3. **消费方查询**：a2a3 后端的 `GetPipeByOpForA3` 直接返回 `OpPipeEntry<op>::pipe`（[include/pto/npu/a2a3/TSync.hpp:24-28](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/npu/a2a3/TSync.hpp#L24-L28)）；类型化 `Event` 在 `EventBase` 里用它算出 `srcPipe`/`dstPipe`（[event.hpp:379-381](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L379-L381)）。a5/kirin9030 等后端的 TSync 查的是同一张表。

查伪代码：

```text
Event<TLOAD, TADD> 实例化时：
  srcPipe = OpPipeEntry<TLOAD>::pipe  → PIPE_MTE2
  dstPipe = OpPipeEntry<TADD>::pipe   → PIPE_V
  → Record() 时 set_flag(PIPE_MTE2, PIPE_V, token)
  → Wait()   时 wait_flag(PIPE_MTE2, PIPE_V, token)
```

#### 4.2.3 源码精读

表的头部样本（[event.hpp:165-169](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L165-L169)）：`TLOAD→PIPE_MTE2`、`TSTORE_VEC→PIPE_MTE3`、`SCALAR/TRESHAPE→PIPE_S`、`VECTOR/TADD→PIPE_V`——正是本讲学习目标里那组核心映射。

几处「反直觉」的登记值得注意：

- [event.hpp:193](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L193)：`TEXPANDS_MAT` 走 MTE2 而非 V——名字像计算，实为搬入类扩展；同类还有 `TFILLPAD_MAT`/`MGATHER_MAT`/`TPREFETCH`（[event.hpp:284-288](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L284-L288)）。教训：**猜名字不如查表**。
- [event.hpp:228-240](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L228-L240)：`TMOV_V2M`/`TSTORE_ACC`/`TMOV_A2V` 走 `PIPE_FIX`（Fixpipe 路径），`TMOV_M2L/M2B/M2R`/`TEXTRACT_M2LR` 走 `PIPE_MTE1`，`TMATMUL`/`TGEMV` 走 `PIPE_M`——跨缓冲移动的指令按「目的地」归类，而不是统一算「搬运」。
- [event.hpp:249-252](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L249-L252)：`TIMG2COL` 走 MTE1，而 `SETFMATRIX`、`SET_IMG2COL_*` 等纯配置指令走 `PIPE_S`。

**消费方一（单流水线屏障）**：[include/pto/npu/a2a3/TSync.hpp:31-42](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/npu/a2a3/TSync.hpp#L31-L42) 的 `TSYNC_IMPL` 用映射结果做编译期白名单检查后调用 `pipe_barrier`——同流水线保序不需要事件，用 barrier 即可（u3-l1 已区分）。

**消费方二（类型化事件）**：[include/pto/npu/a2a3/TSync.hpp:51-60](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/npu/a2a3/TSync.hpp#L51-L60) 的 `Event` 通过 `GetPipeByOp` 把 `SrcOp`/`DstOp` 翻译成流水线对；若某 Op 忘记登记、兜底成 `PIPE_ALL`，会立刻触发 [TSync.hpp:67-68](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/npu/a2a3/TSync.hpp#L67-L68) 的 `static_assert`（"SrcOp/DstOp are invalid"）——这就是「新指令必须登记映射」的编译期防线。Record/Wait 最终落到 `set_flag`/`wait_flag`/`pipe_barrier`（[TSync.hpp:80-120](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/npu/a2a3/TSync.hpp#L80-L120)）。

#### 4.2.4 代码实践（本讲主实践前半）

1. **实践目标**：统计映射表中 `PIPE_V`、`PIPE_MTE2`、`PIPE_MTE3`、`PIPE_S` 各挂了多少条指令。
2. **操作步骤**：在仓库根目录执行（注意结尾的 `);` 不可省——它防止统计 `PIPE_M` 时误匹配 `PIPE_MTE1/2/3`）：

   ```bash
   grep -o "PTO_DEFINE_OP_PIPE(Op::.*, PIPE_V);"    include/pto/common/event.hpp | wc -l
   grep -o "PTO_DEFINE_OP_PIPE(Op::.*, PIPE_MTE2);" include/pto/common/event.hpp | wc -l
   grep -o "PTO_DEFINE_OP_PIPE(Op::.*, PIPE_MTE3);" include/pto/common/event.hpp | wc -l
   grep -o "PTO_DEFINE_OP_PIPE(Op::.*, PIPE_S);"    include/pto/common/event.hpp | wc -l
   ```

3. **需要观察的现象**：四条命令各输出一个数字。
4. **预期结果**（本讲义编写时已在当前 HEAD 实际执行核对）：`PIPE_V=101`，`PIPE_MTE2=5`（`TLOAD`/`TEXPANDS_MAT`/`TPREFETCH`/`TFILLPAD_MAT`/`MGATHER_MAT`），`PIPE_MTE3=2`（`TSTORE_VEC`/`TSTORE_MAT`），`PIPE_S=6`（`SCALAR`/`TRESHAPE`/`TCI`/`SETFMATRIX`/`SET_IMG2COL_RPT`/`SET_IMG2COL_PADDING`）。顺带可数出其余三条：`PIPE_FIX=8`、`PIPE_MTE1=5`、`PIPE_M=2`；合计 129 条，恰等于 `Op` 枚举中的指令数——**当前每个 Op 都有显式登记，没有谁真正落到 `PIPE_ALL` 兜底**。

#### 4.2.5 小练习与答案

- **练习 1**：为什么统计 `PIPE_M` 时 grep 模式必须带 `);` 结尾？
  **答案**：不带结尾标点时 `PIPE_M` 是 `PIPE_MTE1`、`PIPE_MTE2`、`PIPE_MTE3` 的前缀，会把这三条也算进去（5+2+2=9 条而非 2 条）。
- **练习 2**：新增一条指令 `TFOO` 但忘了写 `PTO_DEFINE_OP_PIPE`，会在什么时候、以什么方式暴露？
  **答案**：编译期。查表兜底返回 `PIPE_ALL`；一旦它被用作 `Event<TFOO, ...>` 的模板参数，a2a3 的 `TSync.hpp` 里 `static_assert` 会报 "SrcOp are invalid"/"DstOp are invalid"；若用于单流水线 `TSYNC`，则被 [TSync.hpp:36-39](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/npu/a2a3/TSync.hpp#L36-L39) 的白名单拦截（`PIPE_ALL` 不在 S/V/M/MTE1/MTE2/MTE3/FIX 之列，注意该断言文本里单独列出了 `PIPE_ALL`，以头文件当前实现为准）。

### 4.3 事件 ID 复用：EventIdCounter 的按流水线对轮转

#### 4.3.1 概念说明

手写 `set_flag`/`wait_flag` 时 ID 由程序员自己管；类型化 API（`Event<SrcOp, DstOp>`）则由框架自动发号——[event.hpp:386](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L386)：`token = AutoToken ? EventIdCounter<srcPipe, dstPipe>::GetNextId() : EventID`。即默认自动取号，也可显式传 `EventID` 接管（跨核事件就必须手动指定，见 [TSync.hpp:66](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/npu/a2a3/TSync.hpp#L66) 的断言）。

`EventIdCounter<SrcPipe, DstPipe>` 是按**流水线对**实例化的模板类——每个二元组一份独立的静态计数器，再次体现「ID 命名空间按 (src, dst) 隔离」。

#### 4.3.2 核心流程

取号算法（[event.hpp:302-312](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L302-L312)）：

```text
GetNextId():
    id ← NextId                        # 从 EVENT_ID0 起步
    若 id 的占用位已置位 → 断言失败：
        "Event ID still occupied - likely missing Wait()"
    置位占用位
    NextId ← (NextId + 1) mod 8        # 轮转：\( (\mathrm{next}+1) \bmod 8 \)
    return id
```

三个要点：

1. **轮转复用**：模 8 递增，同一流水线对上第 9 个事件自动回头用 ID0——这就是「复用」的实现。
2. **占用检测**：在 `__CPU_SIM`/`__COSTMODEL` 下额外维护 8 位占用掩码（[event.hpp:305-309](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L305-L309)、[event.hpp:331-337](https://github.com/hw-native-sys-pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L331-L337)）。若轮转绕回时某 ID 仍是「已发号未 Wait」状态，立即断言报错，提示你漏了 `Wait()`——把 u3-l1 说的「CPU 检不出事件链错误」补上了一小块：**发号层面的泄漏**能被查出来，**set/wait 顺序错误**仍然查不出。
3. **复位**：`Reset()` 把计数器和占用掩码归零（[event.hpp:313-319](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L313-L319)）。头文件里还提供了按 ID 释放的 `MarkFree`（[event.hpp:321-323](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L321-L323)），当前 `include/` 内没有调用点，占用位实际靠 `Reset` 清零。

#### 4.3.3 源码精读

[event.hpp:299-338](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L299-L338) 是 `EventIdCounter` 全文；[event.hpp:340-344](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L340-L344) 的 `WaitAllEvents` 用折叠表达式逐个 `Wait()`，是指令尾部 `WaitEvents&...` 变参包的落地（u3-l1 已讲），`Wait()` 本身是空操作转 `WaitImpl`（[event.hpp:392-400](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L392-L400)）。`Event<Op::TLOAD, Op::TADD>` 这类写法因此完全不需要出现 `PIPE_MTE2` 字样——流水线对由 4.2 的映射表自动翻译。

#### 4.3.4 代码实践

1. **实践目标**：核算 `add_custom.cpp` 一共用了几个「事件通道」，验证 set/wait 配平。
2. **操作步骤**：通读 [add_custom.cpp:79-113](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L79-L113)，把每条 set/wait 记成 `(src, dst, id)` 三元组，按通道分组计数（循环体按 `loopCount = tileNum * BUFFER_NUM = 2 * 2 = 4` 次展开，[add_custom.cpp:73](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L73)）。
3. **需要观察的现象**：每个通道的 set 次数与 wait 次数。
4. **预期结果**：共 4 个方向 × 2 个 ID = 8 个通道：(V→MTE2)、(MTE2→V)、(V→MTE3)、(MTE3→V)，每向各有 ID0/ID1。每个通道 set 总数 = wait 总数（预置 1 次 + 循环内 3~4 次 set，对应次数相等的 wait），全内核 set 与 wait 总数相等、且同一通道内严格交替（详见 4.4.3 的逐条核对）。这是纯源码推演，结论确定；若你自己的统计对不上，先检查是否漏了预置/收尾各 4 条。

#### 4.3.5 小练习与答案

- **练习 1**：类型化 API 下，同一流水线对上连续 `Record` 多少个事件后，`EventIdCounter` 会绕回重用 ID0？
  **答案**：8 个（模 `EVENT_ID_MAX=8` 轮转）。若此时 ID0 的占用位仍置位（还没 Wait），在 `__CPU_SIM`/`__COSTMODEL` 构建里触发断言 "Event ID still occupied - likely missing Wait()"。
- **练习 2**：为什么 `EventIdCounter` 要做成 `template <pipe_t SrcPipe, pipe_t DstPipe>`？
  **答案**：让每个流水线对拥有独立的静态计数器与占用掩码——ID 的命名空间本来就是按 (src, dst) 隔离的（4.1 的结论），自动发号器必须遵守同一模型，否则 (V→MTE2) 的取号会挤占 (V→MTE3) 的 ID。

### 4.4 乒乓事件模式：add_custom.cpp 的双缓冲事件编排

#### 4.4.1 概念说明

双缓冲（ping-pong）用两份 UB 槽位让「下一轮搬入」与「本轮计算/搬出」重叠。**缓冲有两份，事件通道也要跟着成对**：每个方向上，槽位 ping 的「空闲/就绪」信号必须与槽位 pong 的分开，否则第 i 轮发出的信号会被第 i+1 轮的错误消费。`add_custom.cpp` 的做法是把缓冲下标直接当事件 ID 用：`pingpong_flag ∈ {0,1}`，同时充当 UB 槽位选择器和事件 ID 选择器。

#### 4.4.2 核心流程

内核事件编排分三段：

```text
预置（循环前）:  给 4 个"槽位空闲"通道各发 1 次 set
循环 i（p = i mod 2）:
    ① wait(V→MTE2, p)     # MTE2 等 V 释放槽位 p（对应第 i-2 轮的 ⑤）
    ② TLOAD x[p], y[p]    # MTE2 搬入
    ③ set(MTE2→V, p); wait(MTE2→V, p)   # V 等数据就绪
    ④ wait(MTE3→V, p)     # V 等槽位 p 上一次搬出完成（对应第 i-2 轮的 ⑥）
    ⑤ TADD z[p]           # V 计算
       set(V→MTE2, p)     # 告知 MTE2：槽位 p 的 x/y 已用完
       set(V→MTE3, p); wait(V→MTE3, p)  # MTE3 等 V 完成
    ⑥ TSTORE z[p]         # MTE3 搬出
       set(MTE3→V, p)     # 告知 V：槽位 p 已写回
    翻转 p
收尾（循环后）:  wait 掉 4 个通道上残留的最后一次 set
```

时序重叠来自「① 只等两轮之前的同槽信号」：第 i 轮 TADD 槽位 p 的同时，MTE2 可以对槽位 1-p 执行第 i+1 轮的 TLOAD——MTE2 不必排在 MTE3 后面。

#### 4.4.3 源码精读

**预置段** [add_custom.cpp:79-82](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L79-L82)：循环开始前给 (V→MTE2) 和 (MTE3→V) 两个方向的 ID0/ID1 各 set 一次。这两个方向是「槽位空闲」信号：第 0、1 轮的 ①④ 要 wait 它们，不预置就会在未置位的旗标上死等。语义上这四条 set 声明的是初始状态「两个槽位都是空闲的」。

**循环体** [add_custom.cpp:83-109](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L83-L109)，与 4.4.2 的编号对应：① [第 90 行](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L90)，② [第 92-93 行](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L92-L93)，③ [第 95-96 行](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L95-L96)，④ [第 98 行](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L98)，⑤ [第 100-101 行](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L100-L101) 与 [第 103-104 行](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L103-L104)，⑥ [第 106-107 行](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L106-L107)，翻转在 [第 108 行](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L108)。缓冲下标与 ID 同源：`pingpong_flag` 同时索引 `xTiles[pingpong_flag]`（槽位选择，tile 数组见 [第 61-63 行](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L61-L63)，TASSIGN 见 [第 66-71 行](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L66-L71)）和 `(event_t)(pingpong_flag)`（ID 选择）。

**收尾段** [add_custom.cpp:110-113](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L110-L113)：`loopCount=4`，最后两轮（i=2 用 ID0、i=3 用 ID1）发出的「槽位空闲」set（⑤ 的 `set(V→MTE2,p)` 与 ⑥ 的 `set(MTE3→V,p)`）再没有下一轮去 wait——共 4 条残留。收尾段恰好 wait 这 4 条，使**每个通道 set 总数与 wait 总数严格相等**，内核退出时不留未消费的挂起事件。

**单缓冲对照** [tests/cpu/st/testcase/tadd/tadd_kernel.cpp:34-41](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L34-L41)：只有一份 tile、一次迭代，每个通道一个 ID，且 set 后紧跟 wait（先 (MTE2→V,ID0) 再 (V→MTE3,ID0)）——几乎没有重叠可言，但正确性一目了然。**双缓冲带来的正是「ID 随槽位翻倍 + set/wait 隔轮配对」这两处复杂化**。

#### 4.4.4 代码实践（本讲主实践后半）

1. **实践目标**：解释 `add_custom.cpp` 中 `EVENT_ID0`/`EVENT_ID1` 交替使用所体现的双缓冲事件 ID 分配策略，写成一段文字。
2. **操作步骤**：
   - 对照 4.4.2 的伪代码逐行标注 [add_custom.cpp:90-107](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L90-L107) 的每条 set/wait；
   - 回答三个问题：(a) 为什么 ID 要随槽位交替而不是全程用 ID0？(b) 为什么同一轮里四个方向可以共用同一个数字 p？(c) 预置与收尾各解决什么问题？
   - 写成 150~300 字的一段话。
3. **需要观察的现象**：你写出的解释是否覆盖 (a)(b)(c) 三点。
4. **预期结果（参考答案）**：
   > 事件 ID 的分配策略是「一个缓冲槽位独占一个 ID」：`pingpong_flag` 既选 UB 槽位又选事件 ID，槽位 ping 永远用 ID0、pong 永远用 ID1。(a) 若全程只用 ID0，同一方向上第 i 轮的 set 与第 i+1 轮的 set 会先后落在同一通道而没有中间的 wait，破坏「一个通道内 set/wait 严格交替、至多一个挂起」的硬件约束，两轮也会被错误地锁死成串行；分成两个 ID 后，第 i 轮的 (V→MTE2,0) 与第 i+1 轮的 (V→MTE2,1) 互不排队，MTE2 得以在 V/MTE3 还在处理槽位 0 时就搬入槽位 1，这正是双缓冲重叠的来源。(b) 事件全名是三元组 (src, dst, id)，ID 只在固定流水线对内命名，所以同一轮的 (V→MTE2,p)、(MTE2→V,p)、(V→MTE3,p)、(MTE3→V,p) 是四个独立通道，共用数字 p 不会冲突，反而让「p」始终读作「槽位 p 的那组信号」。(c) 预置段给「槽位空闲」类通道发初始 set，等价于声明两个槽位初始空闲，避免第 0/1 轮的 wait 死等；收尾段 wait 掉最后两轮残留的 set，保证每个通道 set/wait 计数配平、内核退出时事件状态干净。
5. 本实践为源码阅读型（该文件仅在 NPU C220 环境编译，CPU 模拟器上 `set_flag`/`wait_flag` 为空操作），无需运行即可完成；结论已在上文逐条核对。

#### 4.4.5 小练习与答案

- **练习 1**：把 `BUFFER_NUM` 从 2 改成 4（假设 UB 够用），事件部分至少要改哪里？
  **答案**：`pingpong_flag` 的取值集从 {0,1} 扩到 {0,1,2,3}（翻转改为 `flag = (flag + 1) % BUFFER_NUM`），预置段和收尾段都要覆盖 4 个 ID（预置 2×4=8 条 set、收尾 2×4=8 条 wait）。可用 ID 上限是 8，恰好容纳 8 份缓冲——这是双缓冲深度与 `EVENT_ID_MAX` 的隐含约束。
- **练习 2**：删掉收尾段 4 条 wait，功能上「看起来」会怎样？
  **答案**：在 CPU 模拟器上毫无变化（set/wait 都是空操作）；在 NPU 上会留下 4 个挂起未消费的事件，通道状态不再干净，属于隐患（具体故障表现依赖硬件对残留事件的处理，**待本地验证**——可在 sim 或真机上实验观察）。这再次说明事件类错误要上 sim/NPU 才可能暴露（承接 u1-l4、u3-l1 的结论）。

## 5. 综合实践

**任务：给「四缓冲版 add」设计事件方案并做配平审计。**

1. 阅读并保留 [add_custom.cpp:18-30](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L18-L30) 的常量（`BUFFER_NUM=2`、`tileNum=2`、UB 地址表），在纸面把它改造成 `BUFFER_NUM=4` 的版本：
   - 重新规划 4 份 x/y/z 槽位的 UB 偏移（沿用「基址 + 步长」的写法，注意 32 字节对齐与 `static_assert` 的 UB 总量检查，参照 u2-l4 的容量核算）；
   - 按 4.4.5 练习 1 的结论重写预置段、循环体内 6 处 set/wait、收尾段；
   - 用一张表列出全部通道（4 方向 × 4 ID = 16 个）及每个通道的 set/wait 次数，验证配平与严格交替。
2. 把 4.2.4 的统计表和你的通道表放在一起，回答：这个内核用到的事件通道数（16）距离每对流水线 8 个 ID 的上限还有多少余量？如果再叠加 `EventIdCounter` 自动发号（4.3），两者会不会互相干扰？（提示：`add_custom` 用的是手写 `set_flag`/`wait_flag`，不经过 `EventIdCounter`；类型化事件与手写事件只要不同时管理同一通道就不冲突。）
3. 交付物：一张 UB 地址表、一张通道配平表、一段 200 字左右的改造说明。全程源码推演即可完成；若你有 C220/sim 环境，可实际编译验证（**待本地验证**）。

## 6. 本讲小结

- 流水线共 8 个编号（S/V/MTE1/MTE2/MTE3/M/ALL/FIX），事件全名是三元组 `(srcPipe, dstPipe, id)`，**ID 是每对流水线一套的 8 个名额**，不是全局资源（cpu_stub.hpp:52-62、event.hpp:14）。
- 指令归属由 `OpPipeEntry` + `PTO_DEFINE_OP_PIPE` 的模板全特化表在编译期决定：V 上 101 条、FIX 8 条、S 6 条、MTE2 5 条、MTE1 5 条、MTE3 2 条、M 2 条，129 条指令全部显式登记；漏登记会兜底成 `PIPE_ALL` 并被 TSync 的 `static_assert` 拦截。
- **别按名字猜流水线**：`TEXPANDS_MAT`/`TPREFETCH` 在 MTE2，`TSTORE_ACC`/`TMOV_A2V` 在 FIX，`TMOV_M2L` 在 MTE1——查表为准。
- 类型化 `Event<SrcOp, DstOp>` 由 `EventIdCounter<SrcPipe, DstPipe>` 按流水线对轮转发号（模 8 复用），模拟类后端还带「拿了没 Wait」的占用断言。
- 乒乓双缓冲的事件策略：**一个槽位独占一个 ID**，`set` 与 `wait` 在同通道内隔轮严格交替；预置段声明「槽位初始空闲」，收尾段把残留 set 收干净，保证通道配平。
- CPU 模拟器上 set/wait 是空操作，事件编排的正确性靠推演与配平审计，最终要上 sim/NPU 验证。

## 7. 下一步学习建议

1. **下一讲 u3-l3（乒乓缓冲与多核切分实战）**：把本讲的核内事件编排放回完整语境——`BLOCK_ROWS×BLOCK_COLS` 核间切分、`block_idx` 偏移与 `tileNum/BUFFER_NUM` 参数如何联动影响流水线并行度。
2. 需要跨核同步时阅读 u3-l4（SYNCALL），并对照 [include/pto/common/syncall_soft.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/syncall_soft.hpp#L101-L101) 里 `pipe_barrier(PIPE_ALL)` 的用法。
3. 想看这套 0~7 编号如何被用于性能模拟，预习 [include/pto/costmodel/perf_sim/pipe_model.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/costmodel/perf_sim/pipe_model.hpp#L79-L90)（u7-l4 CostModel 讲义的前置材料）。
4. 官方文档补充：`docs/coding/Event.md`（类型化事件用法与最小示例）与 `docs/isa/` 下各指令页中标注的顺序约束。
