# 通信 ISA 总览与通信运行时

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 PTO 通信扩展指令集的四大类别——点对点（同步/异步）、集合、信号——以及每条指令的数据流向。
2. 理解同步通信路径中 **staging tile**（UB 中转 tile）的中介作用，以及它与异步 DMA 直达路径的本质差别。
3. 掌握 `pto_comm_instr_impl.hpp` 如何按 `PTO_NPU_ARCH_A2A3` / `PTO_NPU_ARCH_A5` / `__CPU_SIM` 三个宏选择通信后端实现。
4. 说明 `TPutAsyncNotify` 头文件为何从 `pkg_inc/` 目录解析（本版本的关键修复），以及异步传输 SDMA/URMA/RDMA 三类后端各自的源码位置。
5. 了解通信运行时的核心类型：`AsyncSession`、`AsyncEvent`、`BuildAsyncSession`。

本讲是单元六（通信指令集与多 rank 融合算子）的第一讲，只建立"地图"，不深入任何一条指令的完整实现——后续 u6-l2（TGET 带宽）、u6-l3（异步与集合）、u6-l5（RDMA 后端）会在本讲的地基上逐层展开。

## 2. 前置知识

本讲默认你已完成单元一~四的学习，以下认知会被直接承接：

- **通信有两条线**（u1-l1 已建立）：`TPUSH`/`TPOP` 是**单 NPU 内部** Cube 核与 Vector 核之间的 FIFO 数据交接；而本讲的 `TPUT`/`TGET`/`TREDUCE` 等是**跨 NPU**（跨芯片、跨 rank）的传输。两者名字相近但完全不属于同一子系统。
- **pkg_inc 目录**（u1-l3 已建立）：仓库根下的 `pkg_inc/` 按 CANN 打包约定存放**不对外暴露的内部头文件**，与 `include/`（对外公共 API）相对。
- **GM 与 UB 两级存储**（u2-l2/u2-l4 已建立）：GlobalTensor 是 GM（全局内存）上的视图；Tile 绑定在 UB（统一缓冲）等片上存储。`TLOAD`/`TSTORE`（u4-l4）负责 GM↔UB 搬运。
- **事件同步**（u3-l1 已建立）：跨流水线数据依赖用 `set_flag`/`wait_flag` 表达；指令尾部 `WaitEvents&...` 变参是同一机制的类型化写法。

再补充几个本讲要用的新术语：

| 术语 | 含义 |
|------|------|
| rank | 参与通信的一个 NPU 进程/芯片编号，概念上对应 MPI 的 rank |
| peer | 点对点通信中的"对端"rank；异步 URMA/RDMA 路径用它选择对端队列 |
| 点对点通信 | 两个 rank 之间一对一收发（TPUT/TGET） |
| 集合通信 | 一组 rank 共同参与的收发（广播/收集/散布/规约） |
| staging tile | 在 UB 中开辟的中转 tile，同步通信的数据必须先落到这里再转发 |
| DMA 引擎 | 独立于 AI Core 的数据搬运硬件，可绕过 UB 直接 GM→GM |
| MR（Memory Region） | RDMA 术语：事先注册、允许网卡直接读写的内存区域 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/pto/comm/pto_comm_inst.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp) | 通信指令的**公共 API 层**：TPUT/TGET/TNOTIFY/TWAIT/TTEST/TGATHER/TSCATTER/TBROADCAST/TREDUCE/TPUT_ASYNC/TPUT_ASYNC_NOTIFY/TGET_ASYNC 的 wrapper 模板 |
| [include/pto/comm/comm_types.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp) | 通信**类型定义**：ParallelGroup、NotifyOp、WaitCmp、ReduceOp、DmaEngine、CollEngine、AsyncEvent、Signal/Signal2D |
| [include/pto/comm/pto_comm_instr_impl.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_instr_impl.hpp) | 通信**平台分发层**：按架构宏批量 include a2a3/a5/CPU 三套实现 |
| [pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp) | A5 的 TPUT_ASYNC_NOTIFY 实现（内部头，本版本从 pkg_inc 解析），含 SDMA/URMA/RDMA 三分支 |
| [pkg_inc/pto/comm/async/rdma/](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp) | RDMA 传输后端（本版本新增）：intrin 分发入口 + `backends/hns_1825/` 网卡实现 |
| [include/pto/comm/async_common/async_types.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_types.hpp) | AsyncSession 会话结构与 SDMA 常量 |
| [include/pto/comm/async_common/async_event_impl.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_event_impl.hpp) | `BuildAsyncSession` 各引擎重载与 `AsyncEvent::Wait/Test` |
| [docs/isa/comm/README.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/README.md) | 通信 ISA 文档总目录：指令表 + 类型定义速查 |
| [docs/isa/comm/communication-runtime.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/communication-runtime.md) | 通信运行时契约：staging tile 数据流、大张量 2D 滑窗、使用约束 |

注意目录上的一个关键事实：`include/pto/comm/`（公共 API + a2a3/a5/CPU 的常规实现）与 `pkg_inc/pto/comm/`（内部实现头：两个 `TPutAsyncNotify.hpp` 与整个 RDMA 后端）是**两个并行的目录树**，后者不随 `include/` 对外发布。

## 4. 核心概念与源码讲解

### 4.1 通信指令分类：四个象限

#### 4.1.1 概念说明

大模型推理/训练会把一个算子的数据切到多块 NPU 上（张量并行、专家并行、流水线并行），核间就需要交换数据。PTO 的选择是把跨 NPU 通信做成 **ISA 指令**而不是一个运行时库调用——这样算子开发者可以在内核里直接表达"搬一块远端数据 → 计算 → 写回远端"，让通信与计算在指令粒度上重叠，而不是在算子粒度上粗暴切分。

整个通信指令集可以放进一个二维坐标里：**参与方数量**（点对点 vs 群组）× **完成方式**（同步阻塞 vs 异步事件驱动）。加上不搬数据、只传信号的同步指令，共四个象限：

```
                点对点（2 个 rank）                群组（N 个 rank）
             ┌─────────────────────────┬─────────────────────────────┐
   同步      │ TPUT   本地GM → 远端GM  │ TGATHER     各rank → root   │
   (阻塞至   │ TGET   远端GM → 本地GM  │ TSCATTER    root → 各rank   │
   数据就位) │  （均经 UB staging tile │ TBROADCAST  root → 各rank   │
             │    中转，见 4.1.2）     │ TREDUCE     各rank规约→root │
             ├─────────────────────────┼─────────────────────────────┤
   异步      │ TPUT_ASYNC        GM→GM │ （异步集合通信暂无，         │
   (返回事件 │ TGET_ASYNC        GM→GM │   集合通信当前均为同步路径） │
   句柄)     │ TPUT_ASYNC_NOTIFY 写远端│                             │
             │                   +置信号│                             │
             ├─────────────────────────┴─────────────────────────────┤
   信号      │ TNOTIFY  向远端写 int32 信号（Set / AtomicAdd）        │
   (无载荷)  │ TWAIT    阻塞轮询本地信号（6 种比较）                  │
             │ TTEST    非阻塞测试本地信号                            │
             └───────────────────────────────────────────────────────┘
```

#### 4.1.2 核心流程

**同步路径（TPUT/TGET/集合指令）** 的统一数据流是：

```
本地 GM ──► UB staging tile ──► 跨 NPU 互连 ──► UB staging tile ──► 本地 GM
```

要点（摘自运行时契约文档）：

1. staging tile 是**必需**的中介——同步指令没有 GM 直达通路。
2. 大张量超过单个 tile 容量时，指令内部自动做 **2D 滑窗**：按行/列切块，每块塞进 tile，外层维度逐块迭代；若 tile 的有效行列是静态值，则要求 GM 形状能被整除，否则要用 `DYNAMIC` 有效区域的 tile（[communication-runtime.md:L78-L85](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/communication-runtime.md#L78-L85)）。
3. ping-pong 变体用两个 staging tile 交错"搬入下一块 / 搬出当前块"，流水化掩盖搬运延迟。

**异步路径（\*_ASYNC）** 则完全绕开 UB：

```
本地 GM ──► DMA 引擎（SDMA/URMA/RDMA）──► 远端 GM     → 返回 AsyncEvent
```

调用方先 `BuildAsyncSession<engine>()` 构建会话，之后持有 `AsyncEvent` 用 `Wait()`（阻塞）或 `Test()`（非阻塞）确认完成。

集合规约的数学语义（TREDUCE，元素逐位规约后送到 root）：

\[ \mathrm{result}^{(\text{root})}_{i} = \bigoplus_{k=0}^{N-1} \mathrm{src}^{(k)}_{i}, \quad \bigoplus \in \{\text{Sum}, \text{Max}, \text{Min}\} \]

#### 4.1.3 源码精读

先看公共 API 层的骨架。`pto_comm_inst.hpp` 顶部把类型、分发层和事件一起装配进来：

[include/pto/comm/pto_comm_inst.hpp:L14-L21](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L14-L21) —— 引入 comm 类型、异步类型与**平台分发层** `pto_comm_instr_impl.hpp`；注意 `async_event_impl.hpp`（AsyncEvent 的 Wait/Test 与 BuildAsyncSession）只在**非 CPU 模拟器**下被装配。

TPUT 的注释直接写明了数据流与原子操作支持：

[include/pto/comm/pto_comm_inst.hpp:L26-L43](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L26-L43) —— `srcGlobalData (local GM) → stagingTileData (UB) → dstGlobalData (remote GM)`；模板参数 `atomicType` 编译期指定 `AtomicNone`/`AtomicAdd`。wrapper 本体只有三步：`WaitAllEvents` 等前置事件 → 调 `TPUT_IMPL` → 返回 `RecordEvent{}`，这与 u4-l1 总结的"签名即契约"完全一致。

TPUT 共有三个重载：编译期原子版（上段）、运行期原子版（[L46-L60](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L46-L60)，用 if 分流到两个模板实例）、ping-pong 双缓冲版（[L64-L75](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L64-L75)，收 pingTile/pongTile 两个 staging tile）。

TGET 方向相反：

[include/pto/comm/pto_comm_inst.hpp:L82-L89](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L82-L89) —— `srcGlobalData (remote GM) → stagingTileData (UB) → dstGlobalData (local GM)`，同样有 ping-pong 版（[L93-L101](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L93-L101)）。

信号三件套的操作数高度统一（信号必须是 int32_t）：

- [include/pto/comm/pto_comm_inst.hpp:L108-L113](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L108-L113) —— TNOTIFY：向远端信号区写 `value`，用 `NotifyOp` 选择 Set/AtomicAdd。
- [include/pto/comm/pto_comm_inst.hpp:L123-L128](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L123-L128) —— TWAIT：阻塞直到信号满足 `WaitCmp` 比较条件；信号矩阵则要求**全部**元素满足。
- [include/pto/comm/pto_comm_inst.hpp:L137-L142](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L137-L142) —— TTEST：同一语义的非阻塞版，返回 bool。

集合指令以 `ParallelGroup` 为首参、只在 root 上执行。以 TGATHER 为例：

[include/pto/comm/pto_comm_inst.hpp:L153-L167](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L153-L167) —— 模板参数 `engine` 在 `CollEngine::AIV`（默认，走 tile 搬运路径）与 `CollEngine::CCU`（AIV 触发 CKE 门铃、由 CCU 硬件执行集合通信）之间二选一。TSCATTER（[L201-L215](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L201-L215)）、TBROADCAST（[L250-L264](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L250-L264)）、TREDUCE（[L298-L313](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L298-L313)）结构相同，此处不逐一展开（u6-l3 详述）。

异步象限以 TPUT_ASYNC 为代表：

[include/pto/comm/pto_comm_inst.hpp:L343-L349](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L343-L349) —— GM 到 GM 的异步写，`DmaEngine engine` 模板参数选引擎，**返回 `AsyncEvent`** 而不是 `RecordEvent`：这是异步指令与同步指令在签名上最醒目的区别。

带显式 peer 的重载只对 A5/CPU 开放：

[include/pto/comm/pto_comm_inst.hpp:L351-L367](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L351-L367) —— 注释写明了 peer 的语义分工：**URMA/RDMA 用 peer 选对端队列与内存元数据；SDMA 忽略 peer（寻址来自 GlobalTensor 的 VA）；CPU 模拟器忽略 peer**。TGET_ASYNC 的 peer 版（[L411-L427](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L411-L427)）同样如此。

TPUT_ASYNC_NOTIFY 把"写远端"与"置信号"合成一条指令，仅在真实 NPU 架构宏下可用：

[include/pto/comm/pto_comm_inst.hpp:L369-L396](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L369-L396) —— 条件是 `PTO_NPU_ARCH_A2A3 || PTO_NPU_ARCH_A5`，**不含 `__CPU_SIM`**：CPU 模拟器上没有这条指令的实现头（见 4.3.3），调用它无法通过编译。注释还预告了三种架构路径的行为差异（A2/A3 SDMA 合并提交、A5 SDMA 走同步 MTE 回退、A5 URMA/RDMA 用 peer 选队列）。

指令总表可在文档侧交叉验证：

[docs/isa/comm/README.md:L5-L19](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/README.md#L5-L19) —— 11 条通信指令的 PTO 名（`pto.tput` 等 IR 拼写）；异步与信号分组另见 [L21-L29](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/README.md#L21-L29)。注意 `TPUT_ASYNC_NOTIFY` 已有独立 ISA 文档页（[L23](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/README.md#L23)），不再只是 TPUT_ASYNC 文档的附属说明。

#### 4.1.4 代码实践

**实践目标**：把四个象限的指令整理成一张"数据流向 + 操作数"对照表，并用源码验证每个结论。

**操作步骤**：

1. 通读 [include/pto/comm/pto_comm_inst.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp)（全文 433 行，可一次读完），对 12 条指令各记录：函数名、数据流向（从注释或参数名推断）、必需操作数（GlobalTensor？staging tile？ParallelGroup？session/peer？）、返回类型（RecordEvent / AsyncEvent / void / bool）。
2. 对照 [docs/isa/comm/communication-runtime.md:L5-L19](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/communication-runtime.md#L5-L19) 的操作表核对 Collective Type 一列（Point-to-point / One-to-all / All-to-one / Synchronization）。
3. 用一条 git 命令观察"信号+异步"象限的演进（本讲是 update 模式，这正是本版本的两处变化源头）：

```bash
git log --oneline 0dbecbe7..be5ccb76 -- include/pto/comm pkg_inc
```

应能看到 `feat(comm): add RDMA support for TPUT_ASYNC_NOTIFY` 与 `fix(comm): resolve notify headers from pkg_inc` 两条提交。

**需要观察的现象**：对照表中 TPUT_ASYNC_NOTIFY 一行在"操作数"列会比 TPUT_ASYNC 多出 `dstSignalData + signalValue + notifyOp + peer`；在"返回类型"列两者同为 AsyncEvent。

**预期结果**：得到一张 12 行的完整对照表，且每行都能在源码中指回具体行号。本实践为源码阅读型，无需运行环境；若要实际运行通信指令，需要真机或仿真器（u6-l2 详述）。

#### 4.1.5 小练习与答案

**练习 1**：TGET 和 TPUT_ASYNC 都能把数据从远端拿到本地，它们的数据通路有何本质区别？

**答案**：TGET 走同步路径 `远端 GM → UB staging tile → 本地 GM`，指令返回时数据已就位；TGET_ASYNC 走 DMA 引擎直达 `远端 GM → 本地 GM`，不经 UB，立即返回 AsyncEvent，完成情况要靠 `event.Wait()/Test()` 查询。

**练习 2**：为什么 TPUT 需要 staging tile 而 TPUT_ASYNC 不需要？

**答案**：TPUT 的实现复用 AI Core 的 MTE 搬运流水线（TLOAD/TSTORE，见 4.3.3 的 `TputTransferOnce`），MTE 只能在 GM↔UB 之间搬运，所以必须以 UB tile 为中转，顺便还能在大张量上做 2D 滑窗分块；TPUT_ASYNC 交给独立的 DMA 引擎，硬件本身支持 GM 到 GM 直达，无需占用 UB 空间，也因此不受 tile 容量限制。

**练习 3**：TWAIT 和 TTEST 的区别是什么？各自适合什么场景？

**答案**：TWAIT 阻塞轮询直到信号满足比较条件，适合"必须等到数据才能继续"的强依赖点；TTEST 非阻塞地返回条件是否满足，适合轮询式推进或同时照看多个信号的场景（如生产者-消费者里探测对端是否已就绪）。

### 4.2 通信类型定义：comm_types.hpp

#### 4.2.1 概念说明

通信指令的操作数里出现了一批计算指令见不到的类型：`ParallelGroup`（谁在组里）、`NotifyOp`/`WaitCmp`/`ReduceOp`（信号与规约的算子）、`DmaEngine`（异步走哪个引擎）、`AsyncEvent`（异步完成凭据）、`Signal`/`Signal2D`（信号内存的快捷定义）。它们全部集中在 `comm_types.hpp` 一个头里——这是"聚合类型必须定义在 common 层"（u2-l1 结论）在通信子系统上的体现：a2a3、a5、CPU 三套后端都要引用它们，因此不能落在任何一个后端目录下。

#### 4.2.2 核心流程

类型之间的关系可以画成：

```
同步集合指令 ──► ParallelGroup<GlobalData> ──► 每个元素 = 一个 rank 的 GM 视图
                                     │
TPUT ──► AtomicType (constants.hpp) ─┘
TNOTIFY/TWAIT/TTEST ──► NotifyOp / WaitCmp ──► Signal / Signal2D (int32 GM 视图)
TREDUCE ──► ReduceOp
*_ASYNC ──► DmaEngine {SDMA, URMA, RDMA} ──► AsyncSession (4.5) ──► AsyncEvent
集合指令 engine 参数 ──► CollEngine {AIV, CCU} ──► CCU 路径还需 CcuTriggerContext
```

#### 4.2.3 源码精读

ParallelGroup 是一个刻意做得极轻的"视图"：

[include/pto/comm/comm_types.hpp:L36-L73](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp#L36-L73) —— 只有三个字段：`tensors`（指向外部 GlobalData 数组的指针）、`nranks`、`rootIdx`。注释明确说明设备侧不支持 `std::vector` 这类动态容器，所以不做任何分配；`operator[]` 带越界断言；推荐用 `Create` 工厂构造，且**组内所有 rank 必须传相同的 rootIdx**（root 在组内的下标，不是自己的 rank 号）。

三个算子枚举（[L90-L93](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp#L90-L93) NotifyOp、[L99-L106](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp#L99-L106) WaitCmp 六种比较、[L112-L116](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp#L112-L116) ReduceOp 三种规约）都是 `uint8_t` 底层的强类型枚举，避免裸整型传参。

DmaEngine 是本版本变化的落点之一：

[include/pto/comm/comm_types.hpp:L122-L126](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp#L122-L126) —— 三个取值：`SDMA`（支持 2D 传输）、`URMA`（1D 传输，HCCP V2 Jetty，仅 NPU_ARCH 3510）、`RDMA`（**本版本新增**，注释说明由 `RdmaBackend` 标识编译进二进制的网卡实现）。配套的 `RdmaBackend` 枚举只有两个值 `NONE`/`HNS_1825`：

[include/pto/comm/rdma_backend.hpp:L21-L24](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/rdma_backend.hpp#L21-L24) —— 一个进程最多激活一个 RDMA 后端，`NONE` 表示尚未初始化控制面。注意这个头在 `include/`（枚举要被公共的 `AsyncSession` 引用），而网卡实现全在 `pkg_inc/`——"公共声明对外、实现细节对内"的边界划得非常清楚。

集合引擎选择与 CCU 上下文：

[include/pto/comm/comm_types.hpp:L134-L137](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp#L134-L137) —— `CollEngine::AIV` 默认走 tile 搬运路径；`CollEngine::CCU` 由 AIV 触发 CKE 门铃、CCU 硬件执行集合通信。CCU 路径需要 host 侧填充的 [CcuTriggerContext（L165-L170）](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp#L165-L170)，其中 `CcuInputSource` 决定 CCU 输入 HBM 由 host 预填（HostManaged）还是 AIV 内核自带部分和写入（AivStored，可实现 AIV 前级与 CCU 规约的无 host memcpy 融合）。本讲只需知道这条通路存在，细节留给 u6-l3。

AsyncEvent 是异步指令的完成凭据：

[include/pto/comm/comm_types.hpp:L178-L191](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp#L178-L191) —— 携带 `handle`（0 表示已完成的操作）、`engine`、`urmaTargetCqe`；`valid()` 判 handle 非零，`Wait(session)`/`Test(session)` 是同步原语（实现在 4.5 的 async_event_impl.hpp）。

信号类型的两个快捷别名让 TNOTIFY/TWAIT 的操作数无需每次手写五维模板：

[include/pto/comm/comm_types.hpp:L203-L207](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp#L203-L207) —— `Signal` 等价于一个 1 元素的全静态 `GlobalTensor<int32_t>`；`GlobalSignal` 是通用别名。

[include/pto/comm/comm_types.hpp:L223-L236](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp#L223-L236) —— `Signal2D<Rows, Cols>` 是编译期形状的二维信号矩阵：稠密构造自动令 DIM_3 步长 = Cols，也可以传自定义步长做大信号网格中的子区域视图。这正对应 4.1.3 里 TWAIT"信号矩阵全部元素须满足条件"的语义。

#### 4.2.4 代码实践

**实践目标**：为 `AsyncSession` 的每个字段找到"谁在消费它"，建立类型→后端的映射直觉。

**操作步骤**：

1. 阅读 [include/pto/comm/async_common/async_types.hpp:L102-L125](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_types.hpp#L102-L125) 的 `AsyncSession` 结构体，列出全部字段。
2. 按字段分组标注归属引擎：`contextGm/tmpBufAddr/syncId/channelGroupIdx/blockBytes/commBlockOffset/queueNum` 属 SDMA 语境；`destRankId/qpIdx` 属 URMA；`rdmaBackend/myPe` 属 RDMA（[L123-L124](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_types.hpp#L123-L124)）。
3. 用 Grep 验证：在 `pkg_inc/pto/comm/async/rdma/` 下搜索 `myPe`，确认它被 RDMA 后端消费。

**需要观察的现象**：单个 `AsyncSession` 同时容纳三种引擎的配置字段——引擎无关的统一外壳 + 各引擎专属字段的并集。

**预期结果**：一张三列（SDMA / URMA / RDMA）字段归属表；能回答"为什么 session 可以 build 一次、反复用于多次异步调用"（因为运行期状态如 `sdmaRuntimeCtx` 持久驻留在 session 里，[L115-L118](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_types.hpp#L115-L118) 注释说明了这一点）。待本地验证（Grep 结果以你的工作区为准）。

#### 4.2.5 小练习与答案

**练习 1**：`ParallelGroup::Create(arr, size, rootIdx)` 中 rootIdx 的含义是什么？为什么所有 rank 必须传同一个值？

**答案**：rootIdx 是 root rank **在组内的下标**（不是全局 rank 号）。广播/散布/收集/规约都由 root 单方面执行，如果各 rank 传的 rootIdx 不一致，会出现"多个核都认为自己是 root"或"没人当 root"的错乱，属于文档明确禁止的未定义行为。

**练习 2**：`Signal2D<4,8> sub(ptr + offset, 128)` 与 `Signal2D<4,8> grid(ptr)` 构造出的对象有什么差别？

**答案**：两者形状都是 4×8；`grid` 稠密构造，DIM_3 步长自动取 8（行与行紧挨）；`sub` 显式传步长 128，表示这是某个 128 列大信号网格中的一块 4×8 子区域——TWAIT/TTEST 会在子区域内按步长寻址，不会越界读到网格外。

**练习 3**：`DmaEngine::RDMA` 和 `RdmaBackend::HNS_1825` 是什么关系？

**答案**：`DmaEngine` 是**指令级**的引擎选择（TPUT_ASYNC\<engine\> 的模板参数），RDMA 是与 SDMA/URMA 并列的一类；`RdmaBackend` 是 RDMA 这一类之下**具体网卡实现**的选择，当前只有 HNS_1825 一种，由编译期环境变量 `PTO_RDMA_BACKEND=HNS_1825` 决定编入哪个 backend（u6-l5 详述）。

### 4.3 平台分发层：pto_comm_instr_impl.hpp

#### 4.3.1 概念说明

u1-l5 已经讲过计算指令的装配枢纽 `pto/common/pto_instr_impl.hpp`；通信子系统有一个**完全同构**的枢纽 `pto/comm/pto_comm_instr_impl.hpp`。它的职责同样是一件事：根据编译宏，把 12 条指令的 `*_IMPL` 实现头批量拉进编译单元。理解它就理解了"一份通信 API、三套后端实现"的物理布局。

#### 4.3.2 核心流程

分发逻辑是一个三选一的互斥条件块：

```
pto_comm_instr_impl.hpp
├─ #if __CCE_AICORE__ && !__CPU_SIM && !__COSTMODEL   ← 真机（ccec 编译）
│   ├─ #ifdef PTO_NPU_ARCH_A2A3  → include pto/comm/a2a3/*（9 个同步/信号/集合头 + async/ 2 个）
│   │                              + pkg_inc/.../a2a3/async/TPutAsyncNotify.hpp
│   └─ #ifdef PTO_NPU_ARCH_A5    → include pto/comm/a5/*（镜像结构）
│                                  + pkg_inc/.../a5/async/TPutAsyncNotify.hpp
└─ #ifdef __CPU_SIM              → include pto/cpu/comm/*（9 个，无异步实现头）
```

a2a3 与 a5 两个目录的文件一一镜像（TPut/TGet/TNotify/TWait/TTest/TGather/TScatter/TBroadCast/TReduce + async/TPutAsync + async/TGetAsync），唯独 `TPutAsyncNotify` 不在 `include/` 而在 `pkg_inc/`——这正是 4.4 的主题。

#### 4.3.3 源码精读

[include/pto/comm/pto_comm_instr_impl.hpp:L16-L35](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_instr_impl.hpp#L16-L35) —— 顶层条件要求"在 ccec 编译的真机环境且非模拟器/非 costmodel"，随后 A2A3 分支按四组注释（同步点对点 / 异步点对点 / 信号 / 集合）include 各实现头。注意第 24 行：

```cpp
#include "../../../pkg_inc/pto/comm/a2a3/async/TPutAsyncNotify.hpp"
```

A5 分支结构完全相同（[L37-L54](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_instr_impl.hpp#L37-L54)，第 43 行是对应的 pkg_inc 相对路径）。

CPU 模拟器分支：

[include/pto/comm/pto_comm_instr_impl.hpp:L58-L73](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_instr_impl.hpp#L58-L73) —— 只有 9 个同步/信号/集合头，**没有任何 async 实现头**，也没有 `TPutAsyncNotify`。这从分发层证实了 4.1.3 的结论：`TPUT_ASYNC_NOTIFY` 在 `__CPU_SIM` 下没有实现，`pto_comm_inst.hpp` 里那条指令的 `#if` 也确实不含 `__CPU_SIM`，两层条件互相咬合。（`TPUT_ASYNC`/`TGET_ASYNC` 的 CPU stub 例外——它们声明在 CPU 分支也可见，见下。）

CPU 后端的通信实现"退化"得非常彻底，以 TPUT 为例：

[include/pto/cpu/comm/TPut.hpp:L18-L35](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/comm/TPut.hpp#L18-L35) —— `TPUT_IMPL` 直接调 `Copy_Data` 在两个 GM 模拟缓冲区间拷贝，**staging tile 参数被完全忽略**；`TPUT_ASYNC_IMPL` 拷完立刻返回 `AsyncEvent(0, engine)`（handle 0 = 已完成）。这与 u1-l5 的结论一脉相承：CPU 模拟器只验证数值语义，不模拟互连与流水线时序。

真机侧的同步实现则真实地走过 staging tile。看 a2a3 的单块传输：

[include/pto/comm/a2a3/TPut.hpp:L45-L56](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/a2a3/TPut.hpp#L45-L56) —— `TputTransferOnce` 就是 4.1.2 那条数据流的代码形态：`TLOAD(tile, src)` 把源 GM 搬进 staging tile → MTE2→MTE3 事件握手 + `pipe_barrier(PIPE_ALL)` → `TSTORE_IMPL` 写到远端目的 GM → 再反向事件握手归还 tile。staging tile 不是文档修辞，而是这个函数里那个被 TLOAD/TSTORE 夹住的 `tile`。

#### 4.3.4 代码实践

**实践目标**：用"指令定位法"（u1-l3）追踪一条完整调用链：`TPUT(...)` wrapper → `TPUT_IMPL` → 真机 a2a3 实现 → CPU 实现。

**操作步骤**：

1. 从 [pto_comm_inst.hpp:L36-L43](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L36-L43) 的 wrapper 出发，记下它转发到的 `::pto::comm::TPUT_IMPL<...>` 签名。
2. 打开 [include/pto/comm/a2a3/TPut.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/a2a3/TPut.hpp) 与 [include/pto/cpu/comm/TPut.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/comm/TPut.hpp)，各找到与该签名匹配的 `TPUT_IMPL` 定义。
3. 画出两条链的对照图：真机链多出 `TLOAD → set_flag/wait_flag → TSTORE` 一段，CPU 链只有 `Copy_Data`。
4. 用一条命令验证分发层的互斥性（本机装了 g++ 即可，无需昇腾环境）：

```bash
g++ -E -D__CPU_SIM -x c++ -include include/pto/comm/pto_comm_instr_impl.hpp - < /dev/null 2>/dev/null | grep -c '^# .*pkg_inc'
```

**需要观察的现象**：第 4 步输出应为 0——定义 `__CPU_SIM` 时，两个 `pkg_inc` include 行都不会出现在预处理输出里（它们在 `__CCE_AICORE__` 分支内）。

**预期结果**：一张三列对照表（wrapper 公共层 / a2a3 真机 / CPU 模拟），能明确指出 staging tile 在真机链的哪一步被使用、在 CPU 链的哪一步被忽略。第 4 步命令如因头文件依赖报错，属于正常现象（缺少内核编译环境下的其它包含），可改用 `gcc -E -D__CPU_SIM include/pto/comm/pto_comm_instr_impl.hpp 2>/dev/null | grep pkg_inc` 直接观察；结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `pto_comm_instr_impl.hpp` 里 a2a3 和 a5 是两个平行的 `#ifdef` 块，而不是像计算指令那样每个指令头内部自己判断架构？

**答案**：这里沿用了 u1-l5 讲过的"装配层互斥单选"模式——a2a3 与 a5 各有整套同名实现头，目录即架构边界；把选择权集中在装配层，保证同一编译单元里 `TPUT_IMPL` 只有一份定义。两个块理论上可以共存编译（宏互斥时自然只剩一个），但架构宏由 arch_macro.hpp 唯一翻译，实际只会激活一个。

**练习 2**：CPU 模拟器分支为什么不含 `TPutAsyncNotify.hpp`，而 `pto_comm_inst.hpp` 里 TPUT_ASYNC_NOTIFY 的 `#if` 也不含 `__CPU_SIM`？这两处是巧合吗？

**答案**：不是巧合，是**双层咬合**：声明层（pto_comm_inst.hpp 的 `#if`）控制 wrapper 模板是否存在，装配层（pto_comm_instr_impl.hpp）控制 IMPL 是否被 include。任何一层单独放开都会导致"有声明无实现"（链接错）或"有实现无入口"（死代码）。修改通信指令后端覆盖时必须同时核对两层。

**练习 3**：`TputTransferOnce` 里 `pipe_barrier(PIPE_ALL)` 夹在 TLOAD 事件握手与 TSTORE 之间，起什么作用？

**答案**：`set_flag/wait_flag(PIPE_MTE2, PIPE_MTE3)` 只保证 MTE2 搬入完成这一**事件**被 MTE3 看到；`pipe_barrier(PIPE_ALL)` 进一步让所有流水线在此对齐，确保 staging tile 里的数据对后续读它的流水线完全可见，之后 TSTORE 才能安全地把同一块 UB 当源读走——这是 u7-l2 将讲的"事件先于数据写回可见"顺序原则在通信路径上的应用。

### 4.4 pkg_inc 打包头与 TPUT_ASYNC_NOTIFY 的三后端分发

#### 4.4.1 概念说明

`pkg_inc/` 的定位（u1-l3 已介绍，这里从打包机制上补全）：`include/` 是对外发布的公共 API，`pkg_inc/` 存放**只在树内使用、不暴露给上层消费者**的内部头，安装时由 `cmake/package.cmake` 与 `include/` 一起装进 `<arch>-linux/pkg_inc` 并建立符号链接（[pkg_inc/README.md:L1-L18](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/README.md#L1-L18)）。

`TPutAsyncNotify` 属于这类内部头：它是 `TPUT_ASYNC_NOTIFY_IMPL` 的实现细节，上层只需要 `pto_comm_inst.hpp` 里的 wrapper，不该直接 include 它。本版本有一个与此直接相关的修复——**fix(comm): resolve notify headers from pkg_inc**（commit 78ab1989）：此前装配层写的是 `"pto/comm/a5/async/TPutAsyncNotify.hpp"`，但该文件实际已经迁到 `pkg_inc/` 树中，源码树按原路径找不到；修复改为从装配头自身位置出发的相对路径 `"../../../pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp"`（即 4.3.3 看到的第 24/43 行）。这也是本讲（update）需要重点刷新的认知点。

#### 4.4.2 核心流程

以 A5 版 `TPutAsyncNotify.hpp` 为例，`TPUT_ASYNC_NOTIFY_IMPL` 按 `DmaEngine` 模板参数做编译期三分支：

```
TPUT_ASYNC_NOTIFY_IMPL<engine>(dst, src, signal, value, notifyOp, session, peer)
├─ engine == SDMA → TPUT_ASYNC_NOTIFY_MTE_FALLBACK
│     同步 MTE 搬 payload → dsb(DSB_DDR) → pipe_barrier(PIPE_ALL) → TNOTIFY_IMPL
│     → 返回 AsyncEvent(0, SDMA)   // handle 0 = 已经完成
├─ engine == URMA（需 PTO_URMA_SUPPORTED，NPU_ARCH 3510）
│     → __urma_put_async_notify(...) 返回 AsyncEvent(handle, URMA, targetCqe)
└─ engine == RDMA（需 PTO_RDMA_SUPPORTED，本版本新增）
      → 校验仅支持 NotifyOp::Set → rdma::WriteNotify(...)
      → 返回 AsyncEvent(handle, RDMA)
```

三类异步传输后端的源码位置一览：

| 引擎 | 声明/入口位置 | 实现位置 |
|------|--------------|----------|
| SDMA | `include/pto/comm/a5/async/TPutAsync.hpp`（A5 上 SDMA 名义退化为 MTE 同步回退） | 同左 + `async_common/sdma_constants.hpp` |
| URMA | `include/pto/comm/a5/async/TPutAsync.hpp` 内 URMA 分支 | 依赖 CANN 提供的 urma intrin |
| RDMA | `pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp`（分发入口） | `pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp` 等 8 个文件 |

#### 4.4.3 源码精读

MTE 回退路径的顺序保证：

[pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp:L33-L50](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L33-L50) —— A5 保留 `DmaEngine::SDMA` 作为 API 兼容名，实际走同步 MTE：先把 payload 每块搬完，`dsb(DSB_DDR)` 确保数据落 DDR，`pipe_barrier(PIPE_ALL)` 对齐全流水线，**然后**才发 Scalar 通知，最后返回 handle 0 表示"已完成的操作"。函数上方的注释（[L21-L32](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L21-L32)）还交代了并发语义：同一 AICore 的重复调用完全串行；多生产者写同一信号时 AtomicAdd 可聚合计数、Set 无胜者，需每生产者一个信号槽。

URMA 分支：

[pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp:L52-L67](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L52-L67) —— 校验 payload 与信号后调 `urma::__urma_put_async_notify`，一次调用同时提交 payload 与信号，返回携带 `targetCqe` 的事件句柄。

RDMA 分支是本版本新增：

[pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp:L69-L91](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L69-L91) —— 三步校验（session 必须是有效 RDMA 会话、payload MR 范围、信号 MR 范围与 4 字节对齐），随后两处硬约束：`notifyOp != NotifyOp::Set` 直接断言失败（**RDMA 当前不支持 AtomicAdd 通知**，错误编码进 handle 返回）；payload 不得超过 0x7fffffff 字节。最终 `rdma::WriteNotify` 把 payload RDMA WRITE 与带 fence 的 4 字节 inline 信号 WRITE 一起提交，事件覆盖两者完成。

编译期分发入口：

[pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp:L95-L124](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L95-L124) —— `if constexpr` 按 engine 三分；未启用 `PTO_URMA_SUPPORTED`/`PTO_RDMA_SUPPORTED` 时用 `static_assert` 在**实例化时**编译期拦截（比链接期报错友好得多）；SDMA 分支显式 `(void)peer` 表明忽略该参数。

RDMA 后端的设备侧分发入口在 pkg_inc 树里：

[pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp) —— 以 `switch (backend)` 在 `RdmaBackend::HNS_1825` 等取值间分发（各分支由 `PTO_RDMA_BACKEND_HNS_1825_SUPPORTED` 宏保护）；网卡实现位于同目录 `backends/hns_1825/` 下的 8 个头文件（backend/workspace_manager/arch/types 等）。这一层的展开是 u6-l5 的主题，本讲只需记住"入口在此"。

#### 4.4.4 代码实践

**实践目标**：复现"notify 头从 pkg_inc 解析"这一修复的来龙去脉，并读懂 RDMA 分支的三处校验。

**操作步骤**：

1. 查看修复提交的完整 diff：

```bash
git show 78ab1989 -- include/pto/comm/pto_comm_instr_impl.hpp
```

观察两行 `#include` 从 `"pto/comm/{a2a3,a5}/async/TPutAsyncNotify.hpp"` 改为 `"../../../pkg_inc/pto/comm/{a2a3,a5}/async/TPutAsyncNotify.hpp"`。

2. 验证路径算术：`pto_comm_instr_impl.hpp` 位于 `include/pto/comm/`，`../../../` 回到仓库根，再进 `pkg_inc/pto/comm/...`——用 `ls` 确认目标文件存在：

```bash
ls pkg_inc/pto/comm/a2a3/async/TPutAsyncNotify.hpp pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp
```

3. 阅读 [pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp:L75-L83](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L75-L83)，数一数进入 `rdma::WriteNotify` 之前有几道校验/约束，各自拦截什么。

**需要观察的现象**：第 1 步 diff 恰好只有 2 行增删（fix 描述"keep the change limited to pto_comm_instr_impl.hpp"）；第 2 步两个文件都应存在；第 3 步可数出：session 引擎校验、payload 校验、信号校验、Set-only 限制、2GB 上限共 5 道。

**预期结果**：能独立回答"为什么修复采用相对路径而不是把文件搬回 include/"——因为 pkg_inc 是刻意的对外隔离（CANN 打包约定），搬回去会重新暴露内部头；相对路径让源码树内构建与安装后的 pkg_inc 符号链接都能解析。本实践为源码阅读型，git 命令在仓库内即可执行。

#### 4.4.5 小练习与答案

**练习 1**：A5 上 `TPUT_ASYNC_NOTIFY<DmaEngine::SDMA>` 实际是异步的吗？

**答案**：不是。A5 的 SDMA 名义分支是同步 MTE 回退：payload 搬完、DDR 屏障、全流水线对齐、发完通知后才返回，且返回的 AsyncEvent handle 为 0，语义上代表"已经完成"。它保留 SDMA 这个名字是为了 API 兼容——同一份内核代码在 A2/A3（真 SDMA 硬件）和 A5 之间可移植。

**练习 2**：RDMA 分支为什么不支持 `NotifyOp::AtomicAdd`？

**答案**：当前 HNS1825 后端的实现是在同一 SQ 里提交"payload RDMA WRITE + 带 fence 的 4 字节 inline 信号 WRITE"，信号写是普通 WRITE 语义（覆盖），没有走 RDMA 原子操作（如 FetchAdd）路径，所以源码用断言+错误句柄显式拦截 AtomicAdd，而不是给出错误结果。这是实现范围的取舍，不是协议限制（[TPutAsyncNotify.hpp:L79-L82](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L79-L82)）。

**练习 3**：peer 参数在 SDMA、URMA、RDMA 三种引擎下分别如何被使用？

**答案**：SDMA 忽略 peer（`_IMPL` 里显式 `(void)peer`），目的地址直接来自 GlobalTensor 携带的 VA；URMA 与 RDMA 用 peer 选择该对端的发送队列与注册内存（MR）元数据，URMA 还用它选择通知资源区。这一分工在 [pto_comm_inst.hpp:L377-L383](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L377-L383) 的接口注释中有官方表述。

### 4.5 通信运行时：AsyncSession 与 BuildAsyncSession

#### 4.5.1 概念说明

异步指令不像同步指令"一次调用自包含"——DMA 引擎需要持久的上下文：scratch 缓冲、队列编号、同步序号、对端信息……PTO 把这些收进 `AsyncSession`：**构建一次，反复传给所有异步调用**。`BuildAsyncSession<engine>()` 是它的工厂，`AsyncEvent::Wait/Test` 是完成查询。三者合称异步通信的"运行时三件套"。

#### 4.5.2 核心流程

典型使用序列（真机路径）：

```
host/device 侧准备 scratch tile + GM workspace
        │
        ▼
BuildAsyncSession<engine>(scratchTile, workspace[, myPe/destRankId], session)
        │  按 engine 走不同重载：SDMA / URMA / RDMA
        ▼
TPUT_ASYNC<engine>(dst, src, session[, peer]) ──► AsyncEvent
TGET_ASYNC<engine>(dst, src, session[, peer]) ──► AsyncEvent
        │
        ▼
event.Wait(session)  阻塞至完成 / event.Test(session) 非阻塞查询
```

#### 4.5.3 源码精读

SDMA 会话构建（默认重载）：

[include/pto/comm/async_common/async_event_impl.hpp:L27-L43](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_event_impl.hpp#L27-L43) —— 入参含 scratch tile、GM workspace、可选 syncId / SdmaBaseConfig / channelGroupIdx；非 SDMA 引擎传入此重载会触发 static_assert 提示改用引擎专属重载。

URMA 会话构建（需 `PTO_URMA_SUPPORTED`）：

[include/pto/comm/async_common/async_event_impl.hpp:L45-L68](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_event_impl.hpp#L45-L68) —— 两个重载：只给 workspace，或额外给 `destRankId` 绑定对端。

RDMA 会话构建（本版本新增，需 `PTO_RDMA_SUPPORTED`）：

[include/pto/comm/async_common/async_event_impl.hpp:L71-L85](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_event_impl.hpp#L71-L85) —— peer 无关版本从 scratch tile 提取临时缓冲，连同 `myPe`（自己的端点号）交给 `rdma::BuildSession`；显式 peer 版本的注释说明：带 peer 参数的 TPUT_ASYNC/TGET_ASYNC 重载可以用**同一个** session 服务多个远端 rank——这正是 4.4.3 "peer 选队列与 MR 元数据"设计的用户侧收益。

SDMA 引擎的公共刻度常量：

[include/pto/comm/async_common/async_types.hpp:L23-L33](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_types.hpp#L23-L33) —— flag 区 128 字节、UB 256 字节对齐、每条事件记录 16 字节共 8 个槽位等，内核作者据此为 scratch tile 定容。同文件 [L92-L94](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_types.hpp#L92-L94) 还给出默认块大小 1MB 与自动通道组选择哨兵值。

运行时的文档契约（含约束与禁用情形）见 [docs/isa/comm/communication-runtime.md:L86-L101](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/comm/communication-runtime.md#L86-L101)：各 rank 的 ParallelGroup 必须匹配、非 root 不得调用广播/散布、ping/pong staging tile 的 UB 偏移不得重叠等。

#### 4.5.4 代码实践

**实践目标**：整理 `BuildAsyncSession` 的重载决策表——什么引擎、什么参数、在哪个宏下可用。

**操作步骤**：

1. 通读 [include/pto/comm/async_common/async_event_impl.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_event_impl.hpp)（百行左右），对每个 `BuildAsyncSession` 重载记录：模板约束（engine 固定为谁）、保护宏（无 / `PTO_URMA_SUPPORTED` / `PTO_RDMA_SUPPORTED`）、参数表、返回语义。
2. 补充阅读 `AsyncEvent::Wait/Test` 在该文件中的实现位置，确认它们如何按 `event.engine` 回到对应后端。
3. 把三行结论填进表格：SDMA 行、URMA 行、RDMA 行。

**需要观察的现象**：三个引擎的重载模板都用 `static_assert(engine == ...)` 把错误引擎挡在编译期；RDMA 行多一个 `myPe` 参数、URMA 行多 `destRankId`。

**预期结果**：一张"引擎 × 构建重载"决策表。它是 u6-l5（RDMA 后端深读）与 u6-l3（异步指令实战）的直接前置产物。本实践为源码阅读型，无需硬件。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `AsyncSession` 要"构建一次、反复使用"，而不是每次异步调用前重建？

**答案**：session 内含跨调用必须续传的运行期状态（如 `sdmaRuntimeCtx` 记录的 post 序号、队列头尾、事件槽计数），重建会丢失这些进度，导致完成判定错乱；此外 workspace/MR 注册等本是重操作，复用才符合"build once, pass to all calls"的注释约定（[async_types.hpp:L98-L101](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_types.hpp#L98-L101)）。

**练习 2**：RDMA 版 `BuildAsyncSession` 为什么不需要 `destRankId`，而 URMA 版有可选的 `destRankId` 重载？

**答案**：因为对端信息的绑定时机不同——URMA 既支持会话级绑定对端（构建时给 destRankId），也支持调用时给 peer；RDMA 的设计是 peer 无关会话 + 每次调用显式传 peer，一个 session 可服务多个远端 rank，所以构建时只需要自己的端点号 `myPe`。

**练习 3**：`AsyncEvent(0, engine)` 与 `AsyncEvent(h, engine)`（h≠0）在使用上有什么差别？

**答案**：handle 为 0 表示"已完成的操作"（如 A5 MTE 回退、CPU stub 立即返回），`valid()` 为 false、`Wait()` 无需等待；非零 handle 是真实的异步凭据，必须 Wait/Test 之后才能假定数据就位。

## 5. 综合实践

**任务：绘制一张属于你自己的"PTO 通信架构全景图"，并用源码行号为其每个节点作证。**

具体要求：

1. **指令层**：画出 4.1 的四象限图（点对点同步 / 集合 / 信号 / 异步），为 12 条指令各标注：数据流向箭头、必需操作数、返回类型。数据流向必须引自 `pto_comm_inst.hpp` 各指令的注释或参数名（例如 TPUT 的三段流注释在 [L26-L30](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L26-L30)）。
2. **类型层**：在图上叠加 `comm_types.hpp` 的类型——ParallelGroup 服务哪些指令、DmaEngine/AsyncSession/AsyncEvent 服务哪些指令、Signal/Signal2D 服务哪些指令。
3. **分发层**：画出 `pto_comm_instr_impl.hpp` 的三分支（A2A3/A5/CPU），标出每分支装配的头文件目录；用醒目颜色标出两处从 `pkg_inc/` 解析的 include（[L24](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_instr_impl.hpp#L24) 与 [L43](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_instr_impl.hpp#L43)）。
4. **后端层**：为异步三引擎（SDMA/URMA/RDMA）各画一条从 `TPUT_ASYNC_NOTIFY_IMPL` 出发的分支，终点分别是 MTE 回退、`__urma_put_async_notify`、`rdma::WriteNotify`（均引 [pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp:L95-L124](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L95-L124)），并标注 RDMA 后端的实现目录 `pkg_inc/pto/comm/async/rdma/backends/hns_1825/`。
5. **自查**：合上源码，向自己提三个问题并作答——TPUT 和 TPUT_ASYNC 谁消耗 UB？TWAIT 的信号矩阵语义是什么？为什么 TPUT_ASYNC_NOTIFY 在 CPU 模拟器上不可用？答不上来就回到对应小节。

预期产出：一张 A4 大小的手绘或绘图工具版全景图 + 一份行号索引清单。这张图将是你在 u6-l2~u6-l6 的整个通信单元里反复回看的导航图。

## 6. 本讲小结

- PTO 通信指令集分四个象限：**点对点同步**（TPUT/TGET，数据经 UB staging tile 中转）、**集合**（TGATHER/TSCATTER/TBROADCAST/TREDUCE，以 ParallelGroup 组织、仅 root 执行）、**信号**（TNOTIFY/TWAIT/TTEST，int32 信号无数据载荷）、**异步**（TPUT_ASYNC/TPUT_ASYNC_NOTIFY/TGET_ASYNC，DMA 引擎 GM 直达、返回 AsyncEvent）。
- 同步路径的 staging tile 不是可选项：真机实现（`TputTransferOnce`）就是"TLOAD → 事件握手 → TSTORE"的模板，大张量靠指令内部 2D 滑窗分块；CPU 模拟器则退化为直拷并忽略 staging tile，只验证数值语义。
- `pto_comm_instr_impl.hpp` 是通信版装配枢纽：`PTO_NPU_ARCH_A2A3` / `PTO_NPU_ARCH_A5` / `__CPU_SIM` 三分支互斥拉入三套实现；声明层（pto_comm_inst.hpp 的 `#if`）与装配层必须同时咬合——TPUT_ASYNC_NOTIFY 两层都不含 CPU 就是例证。
- 本版本两处关键变化：① notify 实现头改从 `pkg_inc/` 相对路径解析（commit 78ab1989），`pkg_inc/` 按 CANN 约定存放不对外暴露的内部头；② 异步传输新增 **RDMA** 引擎（commit 3c13474e），与 SDMA/URMA 并列，实现在 `pkg_inc/pto/comm/async/rdma/`（当前唯一网卡后端 HNS1825），且 RDMA 通知仅支持 `NotifyOp::Set`。
- peer 参数的语义按引擎分工：SDMA 忽略（寻址靠 GlobalTensor VA），URMA/RDMA 用它选对端队列与 MR 元数据；配合 peer 无关的 RDMA 会话，一个 session 可服务多个对端。
- 通信运行时三件套：`AsyncSession`（build once、跨调用续传运行期状态）、`BuildAsyncSession<engine>()`（各引擎专属重载 + static_assert 编译期拦截）、`AsyncEvent`（handle 0 表示已完成）。

## 7. 下一步学习建议

- **u6-l2（点对点通信与带宽实测）**：以 `tget_bandwidth` 示例深入 TGET 的同步路径与事件序列，理解同步中转与 DMA 直达的带宽差异——本讲的 staging tile 概念在那里变成可测量的数字。
- **u6-l3（异步通信与集合通信）**：动手分析 `TPUT_ASYNC_NOTIFY` 的 SET/AtomicAdd 两种通知形式与 allgather 的多 rank 依赖组织，本讲 4.4 的三后端分发是其直接前置。
- **u6-l5（异步通信传输后端）**：如果你对 RDMA 感兴趣，下一单元直接深入 `rdma_async_intrin.hpp` 的端点发现、MR 注册与 HNS1825 网卡 backend——本讲只给了入口坐标。
- 想先补计算侧背景的读者：回顾 u4-l4（TLOAD/TSTORE 搬运指令族）能更好理解同步通信对 MTE 流水线的复用；u7-l2（内存一致性）则解释本讲多次出现的"事件先于数据写回"顺序原则的理论根据。
