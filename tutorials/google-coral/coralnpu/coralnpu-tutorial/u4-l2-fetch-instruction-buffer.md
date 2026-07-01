# 取指、指令缓冲与重排序

## 1. 本讲目标

本讲聚焦 CoralNPU 标量核流水线的第一阶段——**取指（Fetch）**。读完本讲你应该能够：

1. 说清「一次取指搬入多少条指令、每周期送给译码器多少条」，理解 256 位宽通道与 4 发射之间的关系。
2. 区分项目里**两条取指实现**——带 L0 缓存的 `Fetch` 与不带缓存的 `UncachedFetch`——以及生产 SoC 选择哪一条。
3. 掌握 CoralNPU 的静态分支预测策略：**向后跳预测 taken、向前跳预测 not-taken**，并能讲清预测正确与错误两种情况下的控制流差别与冲刷惩罚。
4. 理解 `InstructionBuffer` 如何在「取指速率」与「派发速率」之间做弹性解耦与反压。
5. 理解 `FetchReorderBuffer` 为什么能在总线响应乱序时把指令**按原顺序**交还给后续阶段。

## 2. 前置知识

- **RISC-V 指令编码基础**：每条标准指令 32 位；条件分支（B 型，opcode `1100011`）与无条件跳转（JAL，opcode `1101111`）的立即数里都含一个**符号位**，符号位为 1 表示跳转目标在「当前 PC 之前」（向后），为 0 表示在「之后」（向前）。
- **顺序流水线术语**：PC、取指、译码、派发、冒险、反压、冲刷（flush）。这些在第 4 单元开篇讲义里已建立。
- **Chisel 基础**：`Reg`、`Vec`、`Decoupled`（valid/ready/bits 握手）、`Valid`（只有 valid+bits，无 ready）。
- **来自 u4-l1 的认知**：`Parameters` 是内核配置的单一真相源；裸核默认 `fetchDataBits = 256`、`instructionLanes = 4`，并有 `enableFetchL0`、`fetchInstrSlots` 等开关。本讲所有「数字」都以这些参数为依据。
- **来自 u3 系列的认知**：取指最终要经 `IBus2Axi` 走到 ITCM 或外部 AXI；本讲只关心「核内取指单元」这一段。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/chisel/src/coralnpu/scalar/SCore.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala) | 标量核顶层，依据 `enableFetchL0` 在两条取指路径间二选一 |
| [hdl/chisel/src/coralnpu/scalar/Fetch.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fetch.scala) | **带 L0 缓存的取指单元**，内置预译码与分支预测，直接喂 4 个译码器 |
| [hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala) | **不带缓存的取指单元**，由 `FetchControl`+`Fetcher`+`InstructionBuffer` 组装而成 |
| [hdl/chisel/src/common/InstructionBuffer.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/InstructionBuffer.scala) | 指令缓冲 FIFO，解耦取指与派发 |
| [hdl/chisel/src/common/CircularBufferMulti.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/CircularBufferMulti.scala) | `InstructionBuffer` 底层的可多端口入出的环形队列 |
| [hdl/chisel/src/coralnpu/scalar/FetchControlSpec.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchControlSpec.scala) | `FetchControl` 的仿真测试，是观察分支预测/投机取指行为的窗口 |
| [hdl/chisel/src/coralnpu/scalar/FetchReorderBufferSpec.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchReorderBufferSpec.scala) | `FetchReorderBuffer` 的仿真测试，展示乱序响应→顺序提交 |
| [hdl/chisel/src/coralnpu/Parameters.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala) | 取指相关参数的来源 |
| [hdl/chisel/src/coralnpu/Interfaces.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Interfaces.scala) | `FetchUnit`/`IBusIO`/`FetchIO` 等取指接口定义 |

## 4. 核心概念与源码讲解

### 4.1 取指全景与两条取指路径

#### 4.1.1 概念说明

取指是流水线的最上游，它的任务是：**持续地把「接下来要执行的指令」喂给译码器，且尽量每周期喂满 4 条**。CoralNPU 标量核是一条 in-order 三级流水，每周期最多派发 4 条指令（见 [microarch.md:7-18](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/microarch.md#L7-L18)）。要让这 4 个译码器不挨饿，取指必须有「宽通道」——一次从存储里搬回不止一条指令。

项目里存在**两条并列的取指实现**，由一个参数二选一：

- **`Fetch`（带 L0 指令缓存）**：默认实现，内部有一块 1KB 的小 L0 cache，命中时单周期出指令，miss 才向总线发请求；预译码与分支预测都内联其中。
- **`UncachedFetch`（不带 L0 缓存）**：把取指拆成 `FetchControl`（产生 PC、做分支预测）+ `Fetcher`（发总线请求、含重排序缓冲）+ `InstructionBuffer`（缓冲）三段，结构更清晰、可观测性更好。

#### 4.1.2 核心流程

两条路径对外都满足同一个抽象接口 `FetchUnit`，下游（译码器）看到的都是 `io.inst.lanes`（4 条 `Decoupled` 指令）。粗略数据流：

```
CSR(复位PC) ─▶ PC生成(+分支预测) ─▶ ibus取一行(256bit=8条指令)
                                        │
                          ┌─────────────┴─────────────┐
                  Fetch: L0缓存 + 预译码         UncachedFetch:
                  直接出 4 条到 lanes            FetchControl→Fetcher(ROB)→InstructionBuffer→取4条到 lanes
                                        │
                                   io.inst.lanes (4条) ─▶ 译码/派发
```

选择逻辑只有一行，但它是理解整章的钥匙：[SCore.scala:57](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L57)。

#### 4.1.3 源码精读

标量核顶层依据 `enableFetchL0` 在两条路径间选择：

[SCore.scala:57](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L57-L57) —— `Fetch(p)`（带 L0）与 `UncachedFetch(p)`（不带 L0）二选一。

参数默认值：[Parameters.scala:125](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L125-L125) 默认 `enableFetchL0 = true`；但**生产 SoC 的构建脚本里显式关掉它**（如 [hdl/chisel/src/coralnpu/flags.bzl:18](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/flags.bzl#L18-L18) 与 `prod/BUILD` 都传 `--enableFetchL0=False`），也就是说**生产 SoC 走的是 `UncachedFetch` 这条更可观测的路径**。这与 u3-l1 提到的「生产 SoC 取指不带 L0」一致。

两条路径共享的抽象接口：[Interfaces.scala:74-80](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Interfaces.scala#L74-L80) 定义了 `FetchUnit`，其关键端口是 `ibus`（取指总线）、`inst.lanes`（4 条给译码器的指令）、`branch`（执行单元回报的**实际**跳转，用于纠正预测）、`csr`（提供复位 PC）。

取指总线接口：[Interfaces.scala:51-62](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Interfaces.scala#L51-L62) 定义 `IBusIO`，一次请求带 `addr`，返回 `rdata`（宽 `fetchDataBits` 位）和 `fault`。注意它是「地址一拍、数据下一拍」的总线模型。

#### 4.1.4 代码实践

**目标**：确认两种构建配置分别走哪条取指路径。

1. 打开 `hdl/chisel/src/coralnpu/scalar/SCore.scala:57`，确认选择语句。
2. 用 `grep -rn "enableFetchL0" hdl/chisel/src/coralnpu` 列出所有设置点，找出哪些构建目标把它设为 `False`。
3. 对照 `flags.bzl` 与 `prod/BUILD`，得出「生产 SoC 用 `UncachedFetch`、裸核仿真用 `Fetch`」的结论。
4. **预期结果**：你会看到 `core_mini_axi_sim`（主力仿真器）相关的生产配置里 `enableFetchL0=False`，因此后续 4.4、4.5 两节里讲到的 `InstructionBuffer` 与 `FetchReorderBuffer` 在生产路径里是真实启用的。

#### 4.1.5 小练习与答案

**练习 1**：为什么本项目要同时维护两条取指路径，而不是只留一条？
**答案**：`Fetch` 的 L0 缓存追求「命中即出指令」的低延迟，但状态多、可观测性差；`UncachedFetch` 把 PC 生成、总线请求、缓冲、重排拆成独立模块，便于验证与排错（你能单独仿真 `FetchControl`、`FetchReorderBuffer`）。生产 SoC 偏好后者，裸核默认保留前者。

**练习 2**：下游译码器需要关心当前用的是哪条取指路径吗？
**答案**：不需要。两者都实现 `FetchUnit`，对下游暴露的 `io.inst.lanes`（4 条 `Decoupled[FetchInstruction]`）完全一致——这正是抽象接口的意义。

---

### 4.2 宽通道取指：一次搬入 8 条指令

#### 4.2.1 概念说明

「4 发射」不等于「每周期只取 4 条」。CoralNPU 的取指总线是 **256 位宽**，一条 32 位指令，所以**一次取指事务（一行 fetch line）能搬回 8 条指令**。但译码器只有 4 个，每周期只能消费 4 条。于是取指端的「带宽」是译码端的 2 倍，留出余量去吸收分支重定向与缓存 miss 带来的气泡。

这里出现三个容易混淆的「宽度」参数，务必分清：

| 参数（Parameters.scala） | 值 | 含义 |
| --- | --- | --- |
| `fetchDataBits`（[:130](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L130-L130)） | 256 | 取指总线一次返回的位宽 = 32 字节 |
| `fetchInstrSlots`（[:131-136](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L131-L136)） | \(256/32 = 8\) | 一行里的指令条数 |
| `instructionLanes`（[:73](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L73-L73)） | 4 | 每周期送进译码器的指令条数 = 译码器个数 |

> 关键关系：\(\text{fetchInstrSlots} = \text{fetchDataBits} / \text{instructionBits}\)；\(\text{instructionLanes} = 4\) 决定每周期译码 4 条。SoC 配置里 `fetchDataBits` 可能降到 128（即 4 条/行），但本节以裸核默认 256 为例。

#### 4.2.2 核心流程

`Fetch`（带 L0）的取指节奏：

```
每周期：
  1. 用「当前 PC」查 L0 cache（按 tag/index 命中判断）
  2. 命中 → 从 L0 单周期读出 4 条到 inst.lanes；miss 且该行未在途 → 发 ibus 请求
  3. ibus 返回 256 位 → 写入 L0 对应行 → 后续周期从 L0 出指令
  4. 译码器每周期消费最多 4 条，PC 沿用未消费部分 + 跨行续取
```

`UncachedFetch`（不带 L0）则不缓存，每行只用一次：`FetchControl` 顺序递增 PC（每次 +32 字节），`Fetcher` 发 ibus，回来的 8 条经预译码后**入 `InstructionBuffer`**，再从缓冲里每周期取 4 条给译码。

#### 4.2.3 源码精读

`Fetch.scala` 文件头注释一针见血：[Fetch.scala:16-18](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fetch.scala#L16-L18) 说明它是「4 路取指器，直接喂 4 个译码器；内含部分译码器识别分支，向后分支假定 taken、向前分支假定 not-taken」。

L0 缓存的几何参数在此计算：[Fetch.scala:56-62](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fetch.scala#L56-L62) —— `lanes = fetchDataBits/instructionBits = 8`（一行 8 条），`indices = fetchCacheBytes*8/fetchDataBits = 32`（L0 共 32 行）。L0 地址被切成 `Tag | Index | word偏移`，与经典直接映射 cache 一致。

`Fetch.scala:64-72` 用一组 `assert` 把「1KB / 256 位 / 32 位指令」这套数字钉死，可作为对照基准：`indexLsb==5, indexMsb==9, tagLsb==10, indices==32, lanes==8`，即 L0 是 32 行 × 32 字节 = 1024 字节。

L0 的存储体：[Fetch.scala:78-81](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fetch.scala#L78-L81) 定义了有效位 `l0valid`、在途请求位 `l0req`、标签阵列 `l0tag`、数据阵列 `l0data`（每行 256 位）。

最终输出给译码器：[Fetch.scala:402-407](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fetch.scala#L402-L407) 把 `instValid/instAddr/instBits` 接到 `io.inst.lanes(i)`，循环上界是 `p.instructionLanes = 4`——确认 **L0 路径每周期输出 4 条**。紧接其后的 [Fetch.scala:410-412](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fetch.scala#L410-L412) 用断言保证这 4 条指令的地址是连续的（`instAddr(0) + i*4 === instAddr(i)`），即 4 路同周期取的是相邻指令。

#### 4.2.4 代码实践

**目标**：亲手算出 L0 cache 的位宽划分，并与源码断言对齐。

1. 读 [Parameters.scala:126](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L126-L126)（`fetchCacheBytes=1024`）与 [:130](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L130-L130)（`fetchDataBits=256`）。
2. 自行计算：行数 \(= 1024 \times 8 / 256 = 32\)；每行字节数 \(= 256/8 = 32\)；index 位数 \(= \log_2 32 = 5\)。
3. 对照 [Fetch.scala:64-72](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fetch.scala#L64-L72) 的 6 条 `assert`，逐条验证你的计算。
4. **预期结果**：`indexLsb=5, indexMsb=9, tagLsb=10, indices=32, indexCountBits=5, lanes=8` 全部与你的手算一致。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `fetchDataBits` 从 256 改成 128，`fetchInstrSlots` 与 L0 行数各变成多少？
**答案**：`fetchInstrSlots = 128/32 = 4`（一行 4 条）；L0 行数 \(= 1024\times8/128 = 64\)。这正是 SoC 配置采用更窄通道时的情形。

**练习 2**：一行能搬 8 条，译码器每周期只吃 4 条，多出来的 4 条在 L0 路径里存在哪里？
**答案**：存在 L0 数据阵列 `l0data` 里（整行 256 位都被缓存）。下一周期 PC 仍指向同行的后半段，再次查 L0 命中，继续读出后续 4 条，无需再发总线请求。

---

### 4.3 分支预测：向后 taken、向前 not-taken

#### 4.3.1 概念说明

CoralNPU 标量核是**不投机**（non-speculative）的执行核——它不会在分支未决之前提前执行分支后的指令。但**取指端**仍然做了简单的**静态预测**：在「预译码」阶段只看指令编码本身，就决定下一条 PC 取在哪里，从而让取指流不被分支打断。这套策略的直觉非常朴素：

- **向后跳（循环回退）→ 预测 taken**：循环体末尾跳回开头的分支，绝大多数循环迭代里都是要跳的。
- **向前跳（if 跳过 then 分支）→ 预测 not-taken**：向前跳常用于「跳过一段代码」，多数情况不跳，于是顺序取指。

注意：这是**取指方向的预测**，不改变「执行不投机」的语义。一旦执行单元（BRU）算出真实分支结果，若与预测不符，就经 `io.branch` 回报，冲刷取指路径并重定向——这部分是 4.4 节缓冲 flush 与本节「预测错误惩罚」的来源。

#### 4.3.2 核心流程

预测发生在「预译码」环节，两条路径各自有一份逻辑（结论一致）：

```
对刚取回的每条指令做轻量解码：
  - JAL   (opcode 1101111)        → 无条件跳转，必 taken，目标 = PC + J 立即数
  - JALR→ra (return, 需 linkPort) → 返回预测，目标来自 linkPort
  - B 型 (opcode 1100011) 且 op(31)=1 且 非保留编码 → 向后条件分支，预测 taken
  - 其余（含向前 B 型）            → 预测 not-taken，顺序取指

取指 PC 更新：
  - 命中一个预测 taken 的跳转 → 下一 PC 指向目标，并置 flushTx 冲刷「错误路径」上已取的指令
  - 否则顺序向前取下一行（投机续取）
```

> B 型立即数的符号位正是 `inst(31)`（即 `imm[12]`）。它为 1 表示负偏移（目标在当前 PC 之前 = 向后）。所以 `op(31)` 这一位就承担了「向后判定」。

预测正确与错误的差别：

- **预测正确**（如循环回退确实 taken）：取指流直接从目标继续，投机续取，**几乎无损失**。
- **预测错误**（如循环退出那一拍，向后分支实际 not-taken；或向前分支实际 taken）：错误路径上的指令已经被取进缓冲，需要由 BRU 经 `io.branch` 回报真实目标，`FetchControl` 检测到 `branch.valid` 后**冲刷（flush）已投机指令**并从正确地址重取。惩罚 ≈ 「错误路径上已取的行数 × 处理延迟」。

#### 4.3.3 源码精读

`Fetch`（L0 路径）的预译码函数：[Fetch.scala:325-336](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fetch.scala#L325-L336) `PredecodeDe`。其中关键是「向后条件分支」的判定：

```scala
val bxx = op === BitPat("b???????_?????_?????_???_?????_1100011") &&
            op(31) && op(14,13) =/= 1.U   // op(31)=1 → 向后 → 预测 taken
```

[Fetch.scala:329-330](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fetch.scala#L329-L330) 这两行就是「向后 taken」策略的字面来源；`op(14,13)=/=1` 仅是排除 B 型的保留编码。JAL（[:326](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fetch.scala#L326-L326)）与 return（[:327-328](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fetch.scala#L327-L328)）也在此一并预测。

`UncachedFetch` 路径里，`FetchControl` 用一份等价逻辑 `PredictJump`：[UncachedFetch.scala:353-366](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L353-L366)，其中 [:357-358](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L357-L358) 同样用 `inst(31)` 判定向后。

`FetchControl` 的 PC 生成与投机续取：[UncachedFetch.scala:463-472](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L463-L472) 的 `pcNext` 决定树——分支/冲刷优先，其次「取到的指令含跳转」则用预测目标（[:467](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L467-L467)），否则顺序递增到下一行（[:469](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L469-L469)）。冲刷信号 `flushTx` 在 [:490](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L490-L490) 汇总：任何进行中分支/冲刷或刚写入缓冲的跳转都会触发。

行为证据来自 `FetchControlSpec`：

- **顺序投机续取**——[FetchControlSpec.scala:76-93](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchControlSpec.scala#L76-L93) `AlignedSpeculativeFetch`：复位 PC=`0x20000000` 后，`fetchAddr` 依次给出 `0x20000000` → `0x20000020` → `0x20000040`，每拍 +0x20（32 字节 = 一行 8 条指令），证明**没有分支时取指投机地顺序前进**。
- **JAL 预测重定向**——[FetchControlSpec.scala:114-132](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchControlSpec.scala#L114-L132) `FetchJump`：取回的一行里第 4 条（`inst(3)`）被置成 JAL、偏移 +32（`0x0200006f`）。预译码识别 `hasJumped`，触发 `flushTx`（[:125](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchControlSpec.scala#L125-L125) 断言为真），随后 `fetchAddr` 重定向到目标 `0x2000002c`（= 该指令地址 `0x2000000c` + 32，[:130](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchControlSpec.scala#L130-L130)）。
- **外部纠正重定向**——[FetchControlSpec.scala:46-74](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchControlSpec.scala#L46-L74) `Branch`：注入 `branch.valid` 指向 `0x30000000`（模拟 BRU 回报真实跳转），`FetchControl` 在 [:65-66](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchControlSpec.scala#L65-L66) 把 `fetchAddr` 切到 `0x30000000`，并在下一拍投机续取 `0x30000020`（[:71-72](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchControlSpec.scala#L71-L72)）。这就是「预测错误被纠正」时重定向 + 继续投机的过程。

#### 4.3.4 代码实践

**目标**：用一个向后条件分支，手工走一遍「预测 taken、执行纠正」的时序。

1. 构造一条向后 `beq`，例如目标在当前 PC 前 8 字节。其 B 型立即数符号位 `imm[12]`（即 `inst(31)`）为 1。
2. 在 [FetchControl.scala:357-358](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L357-L358) 确认：因为 `inst(31)=1`，`bxx` 为真 → 该分支被预测 taken → `nextPc` 指向「向后」的目标地址。
3. **预测正确时**：循环每轮都跳，取指流稳定地在「目标…目标+若干行…回到目标」之间运行，无额外气泡。
4. **预测错误时**（循环退出那一拍，分支实际 not-taken）：参考 `Branch` 测试，向 `FetchControl.io.branch` 注入真实顺序地址，观察 `fetchAddr` 被纠正、`flushTx` 拉高、`InstructionBuffer` 被冲刷。
5. **待本地验证**：把 `FetchControlSpec` 的 `Branch` 用例改写成「先投机取若干顺序行、再注入 branch 纠正」，逐拍记录 `fetchAddr.bits`，应看到「顺序地址 → 纠正目标 → 目标+0x20 投机续取」的序列。

#### 4.3.5 小练习与答案

**练习 1**：一条 `beq x0,x0,-8`（向后 8 字节）和一条 `beq x0,x0,+12`（向前 12 字节），分别被预测成什么？
**答案**：前者 `inst(31)=1`（负偏移）→ 预测 taken；后者 `inst(31)=0`（正偏移）→ 不满足 `bxx` → 预测 not-taken（顺序取指）。

**练习 2**：为什么「向前分支预测 not-taken」对 `if/else` 结构通常是赚的？
**答案**：典型的 `if (cond) { then }` 编译后，`cond` 为假时跳过 then 块——这是「向前跳」。多数路径里 `cond` 为真、顺序执行 then 块，预测 not-taken 命中；只有 `cond` 为假时才预测错误、付出一次冲刷惩罚。

**练习 3**：JAL 是无条件跳转，为什么也算「预测」？
**答案**：JAL 的目标在译码阶段就能由立即数算出，取指端预译码后可以直接把 PC 重定向到目标，避免「取到 JAL 后那一行的无用指令」。它没有「对错」之分（必然 taken），但仍需要 `flushTx` 把 JAL 之后已投机取入的指令冲掉。

---

### 4.4 指令缓冲 InstructionBuffer

#### 4.4.1 概念说明

取指端「一次来 8 条」，派发端「每周期走 0~4 条，还可能因为记分板/执行单元忙而反压」——两边的速率并不匹配。`InstructionBuffer` 就是夹在中间的**弹性 FIFO**：吸收取指的突发，并向派发端提供稳定的「最多 4 条就绪指令」。它只出现在 `UncachedFetch` 路径里（`Fetch` 路径用 L0 cache + 输出寄存器扮演类似角色）。

#### 4.4.2 核心流程

`InstructionBuffer` 建立在通用环形队列 `CircularBufferMulti` 之上：

```
入队端（来自 FetchControl 的 bufferRequest，一次最多 8 条）：
  feedIn.nReady = min(队列剩余空间 nSpace, n)
  → 队列快满时反压取指端，停止接收新行

出队端（去往译码/派发，最多 4 条/cycle）：
  前 nEnqueued 条均可见，按地址顺序置 valid
  → 译码器按 ready 选择消费前若干条（OneHotInOrder 断言保证按序）

flush（分支重定向/冲刷）：
  清空整个队列，丢弃已投机取入的错误路径指令
```

> 容量关系：在 `UncachedFetch` 中，`window = fetchInstrSlots * 2 = 16`（缓冲深度 16 条），可见输出端口 `n = fetchInstrSlots = 8`，但**只取前 4 条喂译码**（`instructionLanes = 4`）。

#### 4.4.3 源码精读

`InstructionBuffer` 的定义与 IO：[InstructionBuffer.scala:36-48](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/InstructionBuffer.scala#L36-L48)，参数为 `gen`（元素类型）、`n`（入/出端口数）、`window`（容量）；断言 `window % n == 0`（[:39](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/InstructionBuffer.scala#L39-L39)）。

入队反压逻辑：[InstructionBuffer.scala:54-57](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/InstructionBuffer.scala#L54-L57) —— `feedInReady = min(nSpace, n)`，剩余空间不足 n 时只接收能容纳的部分，从源头避免溢出。

出队与「按序派发」保证：[InstructionBuffer.scala:63-71](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/InstructionBuffer.scala#L63-L71) —— 前 `nEnqueued` 个端口置 valid；[:69](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/InstructionBuffer.scala#L69-L69) 的 `assert(OneHotInOrder(io.out.map(_.fire)))` 强制「消费必须从队首连续进行」，不允许跳过前面的指令去取后面的——这正是 in-order 派发所要求的。

底层环形队列：[CircularBufferMulti.scala:48-77](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/CircularBufferMulti.scala#L48-L77)，用 `enqPtr/deqPtr/nEnqueued` 三个寄存器维护多端口入出，flush 时三者清零（[:67-69](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/CircularBufferMulti.scala#L67-L69)）。

`UncachedFetch` 里的实例化：[UncachedFetch.scala:520-525](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L520-L525) —— `window = fetchInstrSlots*2 = 16`，`io.inst.lanes <> instructionBuffer.io.out.take(4)`（**8 个输出端口里只取前 4 个**喂译码），并在 [:525](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L525-L525) 把「iflush / branch / debug_pc」三类事件都接到 `flush`，实现预测错误时的整队冲刷。

反压行为的仿真证据：[FetchControlSpec.scala:134-162](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchControlSpec.scala#L134-L162) `CommitBackpressure`：当 `bufferRequest.nReady` 只有 4（缓冲只剩 4 个空位）时，`fetchData.ready` 被拉低（[:152](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchControlSpec.scala#L152-L152)）——**取指结果无法提交**；等 `nReady` 恢复到 8，`fetchData.ready` 才变高、`nValid` 一下变为 8（[:159-160](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchControlSpec.scala#L159-L160)）。注意 [:143](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchControlSpec.scala#L143-L143) 表明「发起新取指」并不被反压阻塞，被阻塞的只是「把取指结果写进缓冲」。

#### 4.4.4 代码实践

**目标**：画出 `InstructionBuffer` 的容量与端口关系，并用一个测试解释反压。

1. 读 [UncachedFetch.scala:520-525](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L520-L525)，填出下表：

   | 量 | 值 | 出处 |
   | --- | --- | --- |
   | 缓冲容量 window | 16 | `fetchInstrSlots*2` |
   | 入/出端口数 n | 8 | `fetchInstrSlots` |
   | 实际喂译码数 | 4 | `out.take(4)` |

2. 打开 `FetchControlSpec` 的 `CommitBackpressure`（[:134-162](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchControlSpec.scala#L134-L162)），逐拍解释：为什么 `nReady=4` 时 `fetchData.ready=false`？
3. **预期结果**：因为 `sufficientBuffer = (nReady >= predecode.count)`（见 [FetchControl 的 :435-436](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L435-L436)），缓冲空间不足以容纳本次取回的指令条数时，取指结果就被挡在缓冲门外，直到派发端消费、腾出空间。

#### 4.4.5 小练习与答案

**练习 1**：`InstructionBuffer` 的出队为何要用 `OneHotInOrder` 断言？
**答案**：CoralNPU 是 in-order 核，派发必须按程序顺序进行。该断言保证「只有当队首指令被消费后，才能消费第二条」，防止后端的乱序 ready 信号把指令打乱。

**练习 2**：缓冲容量为什么定成 `fetchInstrSlots * 2 = 16`？
**答案**：一行 fetch 来 8 条、译码每周期吃 4 条。深度 16（两行的量）足以吸收「一次突发 8 条」与「派发端偶尔反压」之间的短期失配，又不至于过度占用面积。

---

### 4.5 取指重排序缓冲 FetchReorderBuffer

#### 4.5.1 概念说明

在 `UncachedFetch` 路径里，`Fetcher` 允许**最多 2 条取指事务同时在途**（`maxConcurrentTx=2`）：第一条请求发出后、数据还没回来，就可以发第二条。又因为 `ibus` 的 `rdata` 是**寄存的**（请求一拍、数据下一拍），多条在途时，响应与请求的对应关系、以及响应回到下游的顺序都需要被管理。

`FetchReorderBuffer`（ROB）就解决两件事：

1. **按事务 ID（txid）匹配响应**：每个在途请求带一个 txid，总线响应也带 txid，ROB 据此把数据填回正确的请求项。
2. **按入队顺序提交**：哪怕响应乱序到达，也只从队首（最早发出的请求）出队交给后续阶段，从而把乱序响应**重排**回顺序交付。

它还有第二个用处（见源码注释）：作为一拍寄存延迟，打断 `rdata → addr` 的组合环路，改善时序。

#### 4.5.2 核心流程

ROB 维护一个 `capacity` 项的队列，每项含 `txid / addr / resp(Valid)`：

```
新请求 (newTx, 带 txid+addr) ──▶ 入队尾（enq），nElem+1
总线响应 (busResp, 带 txid+data+fault) ──▶ trySaveResponse：按 txid 匹配某项，置 resp.valid 并存 data
提交 (commit) ──▶ 仅当队首 queue(0) 已响应 且 未被 cancel：按地址顺序出队，nElem-1
冲刷 (flush) ──▶ 把在途项标记 cancel；未响应的直接丢弃，已响应的经 freeTxid 回收 txid
```

> 提交永远只看 `queue(0)`，所以**输出顺序 = 请求发出顺序**，与响应到达顺序无关——这就是「重排序」。

#### 4.5.3 源码精读

`Fetcher` 的并发度与 ROB 实例化：[UncachedFetch.scala:290-302](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L290-L302) —— `maxConcurrentTx = 2`，ROB `capacity = maxConcurrentTx = 2`、`flowResponse = false`。[:293-295](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L293-L295) 的注释点明「ROB 不 flow 响应，作为延迟拍，打断 rdata→addr 环路」。

ROB 的 IO 与数据结构：[UncachedFetch.scala:39-98](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L39-L98)。`newTx`（请求）、`busResp`（响应）、`commit`（提交）、`freeTxid`（回收 txid）、`flush` 都在这里声明；`Entry` 含 `txid/addr/resp`。

按 txid 匹配响应：[UncachedFetch.scala:123-143](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L123-L143) `trySaveResponse`——`founds` 找出 `busResp.txid === queue(i).txid` 的项，把响应写进它的 `resp` 字段（[:139](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L139-L139)）。注意它**不改变项的位置**，只是把数据填进去。

按序提交的判定：[UncachedFetch.scala:236](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L236-L236) `s2Valid` 只看队首 `queue(0).resp.valid && nCancelled===0`——**只有最早那条请求被响应后才能提交**，与响应到达先后无关。这就是乱序响应被重排为顺序交付的关键。`flowResponse=false` 时 `s2Src = state`（[:234](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L234-L234)），即提交结果寄存一拍。

**乱序→顺序**的仿真铁证：[FetchReorderBufferSpec.scala:211-251](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchReorderBufferSpec.scala#L211-L251) `Reorder responses`：依次入队 Tx1/Tx2/Tx3（[:214](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchReorderBufferSpec.scala#L214-L214) 先给 Tx3 响应，再 [:220](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchReorderBufferSpec.scala#L220-L220) 给 Tx2，最后 [:228](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchReorderBufferSpec.scala#L228-L228) 才给 Tx1——响应顺序是 3,2,1 的乱序）。但提交顺序严格是 Tx1 → Tx2 → Tx3（[:234](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchReorderBufferSpec.scala#L234-L234)/[:239](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchReorderBufferSpec.scala#L239-L239)/[:243](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchReorderBufferSpec.scala#L243-L243)），与入队顺序一致。

> 测试用 `capacity = 4`（[:23](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchReorderBufferSpec.scala#L23-L23)）做更充分的覆盖；`Fetcher` 实际只用 `capacity = 2`。读测试时注意区分。

冲刷语义：`FetchReorderBufferSpec` 的 `Flushing` 一组（[:254-397](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchReorderBufferSpec.scala#L254-L397)）展示分支预测错误时 ROB 的行为——未响应的在途事务被静默丢弃，已响应的被 cancel 后经 `freeTxid` 回收 txid（对应 `Fetcher` 里的 `IndexAllocatorShifting` 分配/回收）。

#### 4.5.4 代码实践

**目标**：把「乱序响应、顺序提交」画成一张对照表。

1. 打开 [FetchReorderBufferSpec.scala:211-251](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchReorderBufferSpec.scala#L211-L251) `Reorder responses`。
2. 列出三栏：

   | 顺序 | 入队 (newTx) | 响应到达 (busResp) | 提交 (commit) |
   | --- | --- | --- | --- |
   | 第 1 | Tx1 (addr 0x100) | （第 3 个到，:228） | Tx1 (:234) |
   | 第 2 | Tx2 (addr 0x200) | （第 2 个到，:220） | Tx2 (:239) |
   | 第 3 | Tx3 (addr 0x300) | （第 1 个到，:214） | Tx3 (:243) |

3. 解释：为什么 Tx3 的响应先到，却要等到 Tx1、Tx2 提交后才能出队？
4. **预期结果**：因为提交只看队首 `queue(0)`（[UncachedFetch.scala:236](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L236-L236)）。Tx3 在队尾，即使先被响应，也必须等排在前面的 Tx1、Tx2 依次出队后，它升为队首才能提交。这正是「按入队顺序提交、对乱序响应免疫」。

#### 4.5.5 小练习与答案

**练习 1**：如果总线永远按请求顺序返回响应（永不乱序），`FetchReorderBuffer` 还有存在的必要吗？
**答案**：仍然有。源码注释（[:293-295](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L293-L295)）说明它还兼任「打断 rdata→addr 组合环路」的延迟拍。即便响应有序，这个时序去环作用依然需要。

**练习 2**：`flowResponse=false` 与 `true` 的差别是什么？
**答案**：`flowResponse=false` 时，提交结果寄存一拍（`s2Src = state`），commit 不能在同一拍随响应立刻生效；`true` 时（`s2Src = s1State`）响应同拍即可提交。`Fetcher` 选 `false` 以换取时序，`FetchReorderBufferSpec` 的 `flowResponse=true` 一组（[:467-535](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchReorderBufferSpec.scala#L467-L535)）专门验证同拍提交路径。

---

## 5. 综合实践

把本讲四个要点串起来，完成一次「含循环的小程序」取指轨迹推演。**这是一道源码阅读 + 手动推演题，不需要真实运行硬件**。

**背景片段**（伪汇编，假设从 `0x00000000` 开始）：

```
0x00:  addi  x1, x0, 0      # 循环计数初值
0x04:  addi  x2, x0, 10     # 循环上限
loop:
0x08:  addi  x1, x1, 1      # 计数
0x0c:  bne   x1, x2, loop   # 向后分支，目标 0x08（offset = -8）
0x10:  ...                  # 循环结束后
```

请完成：

1. **取指带宽核算**：根据 [Parameters.scala:130-136](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L130-L136) 与 [:73](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L73-L73)，写出「一次取指搬回多少条」「每周期译码多少条」「`InstructionBuffer` 深度」三个数（答案：8 / 4 / 16）。
2. **预测判定**：`bne x1,x2,loop` 的偏移是 −8（向后），其 `inst(31)=1`。依据 [UncachedFetch.scala:357-358](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L357-L358)，判断它被预测成什么（答案：taken，每次回到 `0x08`）。
3. **投机续取轨迹**：参照 `AlignedSpeculativeFetch`（[FetchControlSpec.scala:76-93](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchControlSpec.scala#L76-L93)），写出复位后前几拍 `fetchAddr` 的序列（应类似 `0x00 → 0x20 → ...`，每拍 +0x20）。
4. **预测错误那一拍**：第 10 次迭代时 `x1==x2`，`bne` 实际 not-taken，但取指端仍按 taken 预测、已经投机取了 `0x08` 那一行。请参照 `Branch` 用例（[:46-74](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchControlSpec.scala#L46-L74)）描述：BRU 经 `io.branch` 回报真实地址 `0x10` → `flushTx` 拉高 → `InstructionBuffer` 被 flush（[UncachedFetch.scala:525](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L525-L525)）→ `fetchAddr` 重定向到 `0x10` → 之后继续投机续取。
5. **ROB 视角**：在上述重定向期间，`FetchReorderBuffer`（capacity=2）里至多 2 条在途事务中，凡未响应的会被 flush 丢弃，已响应的经 `freeTxid` 回收——结合 `FetchReorderBufferSpec` 的 `Flushing` 用例（[:254-301](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FetchReorderBufferSpec.scala#L254-L301)）说明 txid 如何被回收以供下一轮取指复用。

**交付物**：一张「时间 vs fetchAddr / 预测 / buffer 状态 / ROB 状态」的表格，覆盖「正常循环迭代」与「循环退出那一拍」两类情形。预测错误那一拍的精确周期数标注「待本地验证」。

## 6. 本讲小结

- CoralNPU 标量核有**两条取指实现**：带 L0 缓存的 `Fetch`（裸核默认）与不带缓存的 `UncachedFetch`（生产 SoC，`enableFetchL0=False`），由 [SCore.scala:57](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L57-L57) 一行选择；两者对外都是「每周期 4 条 `Decoupled` 指令」。
- 取指走 **256 位宽通道**：一次搬回 `fetchInstrSlots = 8` 条指令，译码端每周期消费 `instructionLanes = 4` 条；`Fetch` 的 L0 是 1KB / 32 行 / 每行 32 字节。
- 分支预测是纯静态的：**向后 taken、向前 not-taken**，判定依据就是 B 型立即数的符号位 `inst(31)`（[Fetch.scala:329-330](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Fetch.scala#L329-L330) / [UncachedFetch.scala:357-358](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L357-L358)）；JAL 与 return 也被预译码并重定向。
- `InstructionBuffer` 是 16 深的弹性 FIFO（`fetchInstrSlots*2`），8 个可见端口取前 4 喂译码，用 `OneHotInOrder` 保证按序派发，并对取指端反压。
- `FetchReorderBuffer` 用 txid 匹配响应、按入队顺序提交，使**乱序到达的总线响应被重排成顺序交付**，同时兼任打断 `rdata→addr` 组合环的延迟拍。

## 7. 下一步学习建议

- **紧接着读 u4-l3（指令译码）**：本讲输出的 `io.inst.lanes`（4 条 `FetchInstruction`）正是译码器的输入，看 `Decode.scala` 如何把 32 位指令识别成 ALU/BRU/MLU/DVU/LSU 等类型。
- **想深入取指控制流**：重读 [UncachedFetch.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala) 中 `FetchControl` 的 PC 状态机（`pcFetched / pcNext / blockNewFetch`，[:455-478](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/UncachedFetch.scala#L455-L478)），理解「何时允许发起下一条投机取指」。
- **想看预测被纠正的下游**：跳到 u5-l1（ALU/BRU），观察 BRU 如何把真实分支结果经 `io.branch`（[Interfaces.scala:34-37](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Interfaces.scala#L34-L37) 的 `BranchTakenIO`）回报给取指单元。
- **想理解取指与内存的衔接**：回到 u3-l2，看 `IBus2Axi` 如何把本讲的 `ibus` 请求转成 AXI 突发去访问 ITCM 或片外存储。
