# Handshake 到 HW 与 ESI 降级

> 本讲承接 u3-l2。u3-l2 的产物是一张**Handshake 弹性数据流图**：没有程序计数器，只有节点与通道，数据靠 `valid`/`ready` 握手自流；而真正的内存（matmul 的输入/输出矩阵）已经被「请出」数据流图，变成 `handshake.extmemory` **外部存储**抽象。本讲要跨过降级链上最关键的一道分水岭——把这张抽象的数据流图**彻底降级成具体硬件**：Handshake 节点变成 `hw` 方言里的逻辑门与寄存器，外部存储变成硬件端口/存储模块，握手通道变成真实的 `data`/`valid`/`ready` 连线。完成这件事的不是一个大脚本，而是**四个小脚本接力**：`handshake_to_hs_ext.sh` → `hs_ext_to_hw0.sh` → `hw0_to_hw.sh` → `hw_to_hw_clean.sh`。每个脚本只跑一个 `circt-opt` 加一组 pass，短到只有 20 行，却各自承担降级链上一个不可替代的职责。本讲就拆解这「四段接力」里的三个核心机制：**外部存储落地为 HW、ESI 充当 Handshake→HW 的桥梁、以及最后的符号死代码消除**。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 `-handshake-lower-extmem-to-hw` 做了什么：为什么 u3-l2 建立的 `handshake.extmemory` 抽象必须先于「Handshake→HW」被处理，以及它如何把外部存储暴露成具体的硬件存储模块（如 matmul 仿真里被层次化引用的 `handshake_memory0`）。
2. 解释 **ESI（Elastic Silicon Interconnect，弹性硅互连）方言**为什么是 Handshake 通往 HW 的**必经桥梁**：`-lower-handshake-to-hw` 会把握手通道翻译成 ESI 通道，而 `-lower-esi-types/-ports/-to-hw` 再把 ESI 通道溶解成 `data`/`valid`/`ready` 这些具体 HW 连线。
3. 回答本讲核心实践问题：**为什么 `-lower-handshake-to-hw` 与 `-lower-esi-to-hw` 的先后顺序不能颠倒**（这是一条数据依赖：前者「生产」ESI，后者「消费」ESI）。
4. 看懂 `-handshake-materialize-forks-sinks`、`-symbol-dce`、`-firrtl-inner-symbol-dce`、`-canonicalize`、`-cse` 这一组「清理与补全」pass 各自在为下一步铺路。
5. 把这四个脚本在 `nix/pipeline.nix` 里对应的 `mkHsExtDerivation` / `mkHw0Derivation` / `mkHwDerivation` / `mkHwCleanDerivation` 串成一条带缓存的派生链，并解释「为什么要把一次降级拆成四个独立派生」。

---

## 2. 前置知识

本讲默认你读过 u3-l2（CF→Handshake）以及它引入的概念：**降级（lowering）**、**方言（dialect）**、**弹性数据流**、`valid`/`ready` 握手、`handshake.extmemory` 外部存储抽象、以及 `common.sh` 的 `require_file`/`require_executable`/`run_to_output` 脚手架。下面只补本讲新出现的三组概念，全部用通俗语言。

### 2.1 「抽象通道」与「具体连线」之间的鸿沟

u3-l2 结束时，你的 IR 里到处是 `handshake.channel<T>` 这样的**抽象通道类型**——一条通道同时承载「数据」和「流动控制（valid/ready）」两重含义，但它**还没说清楚在硬件上到底是几根线**。而我们的终点是 SystemVerilog，SV 里一切都是**具体的连线**：数据是若干 bit 的 wire，valid 是 1 根 wire，ready 是 1 根 wire。

从「抽象通道」到「具体连线」中间有一道鸿沟：你既不能直接把 `handshake.channel` 喂给 SV 后端（它不认识），也不想在 Handshake 阶段就手写好所有连线（那样降级就失去了「分层次、每层只管一件事」的意义）。CIRCT 的解法是**在中间插一层 ESI 方言**作为缓冲——这就是 2.2 要讲的。

### 2.2 什么是 ESI（Elastic Silicon Interconnect）

**ESI**（Elastic Silicon Interconnect，弹性硅互连）是 CIRCT 里的一个方言，专门用来在**硬件模块之间**表示「带握手的、类型安全的通信通道」。可以把它理解成：

- **Handshake 方言**描述的是「一个函数内部的数据流网络」（函数级抽象）；
- **ESI 方言**描述的是「硬件模块之间的通信通道」（模块/端口级抽象）；
- **HW 方言**则是「具体的 wire、模块实例、端口」（门级/RTL 级抽象）。

ESI 的核心类型 `!esi.channel<T>`（确切拼写以本项目 pin 的 CIRCT 版本为准，**待本地验证**）就是「一条承载类型 `T` 数据、并带 valid/ready 流控的通道」。它比 Handshake 通道更贴近硬件（已经站在「端口」视角），但又还没到「拆成 data/valid/ready 三根线」那么具体。

这个「夹在中间」的定位，正是 ESI 成为 Handshake→HW 桥梁的原因：Handshake 降到 HW 时，先翻译成 ESI（通道变成 ESI 通道、函数变成 HW 模块、但端口仍是 ESI 抽象），再由 ESI 降到 HW（把 ESI 通道拆成具体的 data/valid/ready 连线）。**两段降级各管一层抽象的溶解**，这是 4.2 的重点。

### 2.3 什么是「符号」与「死代码消除（DCE）」

降级过程中会产生大量**符号（symbol）**——可以理解成 IR 里的「命名引用」。比如一个内部生成的辅助模块、一个 `hw.module.extern` 外部模块声明、一条被引用的连线，都可能带一个符号名，供别处按名字引用。

不是所有符号最后都有用：降级做完后，常有一些「定义了但没人引用」的辅助模块或中间表示残留下来——这就是**死代码（dead code）**。**符号 DCE（Symbol Dead Code Elimination）** 会找出这些「没人引用的符号定义」并删掉，让最终交给 SV 后端的 HW IR 干干净净、只含真正需要的模块。这能减小生成 SV 的体积，也避免把无用模块喂给后续综合工具。`hw_to_hw_clean.sh` 就是干这件事的，详见 4.3。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下（按在降级链里的出现顺序排列）：

| 文件 | 作用 |
| --- | --- |
| [scripts/pipeline/common.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/common.sh) | 四个脚本共用的脚手架：`require_file`/`require_executable` 做前置熔断，`run_to_output` 把工具 stdout 落盘。 |
| [scripts/pipeline/handshake_to_hs_ext.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/handshake_to_hs_ext.sh) | **第 1 段**：Handshake → hs-ext。把 `handshake.extmemory` 外部存储降级到 HW，并补全 fork/sink 节点。 |
| [scripts/pipeline/hs_ext_to_hw0.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hs_ext_to_hw0.sh) | **第 2 段**：hs-ext → hw0。把 Handshake 数据流网络降级成 HW 方言（此时通道还是 ESI 抽象）。 |
| [scripts/pipeline/hw0_to_hw.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw0_to_hw.sh) | **第 3 段**：hw0 → hw。把 ESI 类型/端口/操作溶解成纯 HW（data/valid/ready 连线）。 |
| [scripts/pipeline/hw_to_hw_clean.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_to_hw_clean.sh) | **第 4 段**：hw → hw-clean。符号 DCE + canonicalize/cse 清理，为 SV 导出做准备。 |
| [nix/pipeline.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix) | 把上面四个脚本各包成一个 `runCommand` 派生，并用 `mkPipeline` 的惰性自引用串成可缓存的依赖链。 |

> 上一站的 `cf_to_handshake.sh` 不在本讲精读范围，但它的产物（Handshake MLIR）是本讲第 1 段的输入；u3-l2 已详细讲过它如何建立 `handshake.extmemory` 抽象。

---

## 4. 核心概念与源码讲解

### 4.1 extmem 到 HW 的外部存储（handshake_to_hs_ext.sh）

#### 4.1.1 概念说明

回顾 u3-l2：`handshake-legalize-memrefs` 把 `memref` 内存「请出」了数据流图，变成一个 `handshake.extmemory` 节点——这是「**一块存在于数据流图之外、但被图里节点按地址读写**」的抽象。在当时，这个抽象很优雅：它让 Handshake 的数据流分析不必关心内存细节。

但到了要把硬件真正「落地」的阶段，这个抽象必须被**实体化**：数据流图里那些 `handshake.load`/`handshake.store` 节点要连到一块**具体的硬件存储**上。`-handshake-lower-extmem-to-hw` 干的就是这件事——它把 `handshake.extmemory` 抽象降级成 HW 方言里的存储构造。

这里有一个**关键的工程约束**：外部存储的处理**必须先于** Handshake→HW 的整体降级（4.2 的 `-lower-handshake-to-hw`）。原因是 `-lower-handshake-to-hw` 只认「纯数据流逻辑」，它假定内存这件事已经被解决掉了——如果此时还留着 `handshake.extmemory` 抽象，它会无从下手。所以本段脚本的第一要务就是：**先让内存落地，再让逻辑降级**。

此外，Handshake IR 允许「隐式 fork」（一个值被多个节点用）和「隐式 sink」（一个值产生后从不被消费）——这在抽象层面是合法的简写。但 `-lower-handshake-to-hw` 要求每一个 fork 和 sink 都是**显式的物理节点**（因为硬件里「一拖多」需要真正的 fork 部件、「白产生」需要真正的 sink 部件来吃掉 token 维持握手协议）。`-handshake-materialize-forks-sinks` 就是把这些隐式 fork/sink **实体化**成显式 `handshake.fork`/`handshake.sink` 节点，为下一步铺路。

#### 4.1.2 核心流程

`handshake_to_hs_ext.sh` 的降级流程（伪代码）：

```
输入: Handshake MLIR（含 handshake.extmemory 抽象、隐式 fork/sink）
  │
  ├─ -handshake-lower-extmem-to-hw
  │     作用: 把外部存储抽象降级成 HW 存储构造
  │     （小存储 → 内部存储模块，如 matmul 的 handshake_memory0；
  │      大存储/外部存储 → hw.module.extern 声明，待后续挂接）
  │
  ├─ -handshake-materialize-forks-sinks
  │     作用: 隐式 fork → 显式 handshake.fork；隐式 sink → 显式 handshake.sink
  │
  ├─ -canonicalize    （常量折叠、模式化简等通用清理）
  └─ -cse             （公共子表达式消除）
  │
输出: hs-ext MLIR（外部存储已落地、fork/sink 已显式，逻辑主体仍是 Handshake）
```

两条 pass 的顺序有意义：**先** `-handshake-lower-extmem-to-hw` 处理存储（可能引入新的数据流连接），**再** `-handshake-materialize-forks-sinks` 把所有 fork/sink 补全——这样 fork/sink 实体化覆盖的是「存储处理之后的最终形态」，不会漏补。

#### 4.1.3 源码精读

整个脚本的核心就是一次 `run_to_output` 调用，挂在 [scripts/pipeline/handshake_to_hs_ext.sh:15-19](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/handshake_to_hs_ext.sh#L15-L19)：

```bash
run_to_output "$output" "$circt_opt" "$input" \
  -handshake-lower-extmem-to-hw \
  -handshake-materialize-forks-sinks \
  -canonicalize \
  -cse
```

这段代码依次把四个 pass 喂给 `circt-opt`（CIRCT 的「跑一遍 pass」命令行工具，注册了 Handshake/ESI/HW 等 CIRCT 专属 pass）。`run_to_output`（定义在 [scripts/pipeline/common.sh:20-24](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/common.sh#L20-L24)）把 `circt-opt` 的 stdout 重定向到输出文件。

脚本头的 [scripts/pipeline/handshake_to_hs_ext.sh:9-13](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/handshake_to_hs_ext.sh#L9-L13) 是参数解析与前置熔断：

```bash
circt_opt="${1:?usage: ...}"
input="${2:?usage: ...}"
output="${3:?usage: ...}"
require_executable "$circt_opt"
require_file "$input"
```

`:?` 在参数缺失时打印用法并退出；`require_executable`/`require_file`（[common.sh:4-18](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/common.sh#L4-L18)）确保工具可执行、输入文件存在，否则以退出码 2 熔断——这是所有降级脚本共用的「快速失败」骨架。

> 关于 `-handshake-lower-extmem-to-hw` 的确切产物：对 matmul 这种小存储，它会生成内部存储模块（你在 u4-l2 仿真里会看到 `dut.handshake_memory0._handshake_memory_5[i]` 这样的层次化引用，正是这里的产物）；对 TinyStories 的超大存储，则可能产生 `hw.module.extern` 外部模块声明，留待 u3-l4 的 FP extern 挂接机制或 u6-l2 的 blackbox 外部化处理。确切 IR 形态以本项目 pin 的 CIRCT fork 为准，**待本地验证**。

#### 4.1.4 代码实践

**实践目标**：验证「外部存储必须先落地」这一约束，并理解 `-handshake-materialize-forks-sinks` 的必要性。

**操作步骤**：

1. 在 `nix develop` 环境里，定位 `circt-opt`（`which circt-opt`）。
2. 找到上一站的产物：`result-handshake.mlir`（若你跑过 u3-l2 的实践；否则可对 matmul 跑 `nix build .#matmul-handshake` 取其 `*.mlir`，路径见 `nix/pipeline.nix` 的 `mkHandshakeDerivation` 产物名）。
3. **对照实验 A（完整）**：执行
   ```bash
   circt-opt result-handshake.mlir \
     -handshake-lower-extmem-to-hw \
     -handshake-materialize-forks-sinks \
     -canonicalize -cse \
     > hs-ext.mlir
   ```
4. **对照实验 B（去掉 materialize-forks-sinks）**：删掉 `-handshake-materialize-forks-sinks` 再跑一次，输出到 `hs-ext-nofork.mlir`。

**需要观察的现象**：实验 A 的 `hs-ext.mlir` 中应能看到显式的 `handshake.fork` / `handshake.sink` 节点，以及外部存储已变成 HW 存储构造（如 `handshake_memory` 之类的模块实例或 `hw.module.extern`）。

**预期结果**：实验 A 成功产出可继续降级的 IR；实验 B 在本步可能仍能跑完，但**下一步** `-lower-handshake-to-hw` 很可能因为「遇到隐式 fork/sink」而报错或产出非法 IR。这正说明 `-handshake-materialize-forks-sinks` 是为下一步铺路的必要前置。

> 若手头没有现成的 handshake MLIR 产物，可改为**源码阅读型实践**：阅读本脚本与 `hs_ext_to_hw0.sh`，写下「如果跳过 materialize-forks-sinks，4.2 的 `-lower-handshake-to-hw` 会在哪一步失去前提」的一段分析。确切的报错信息**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `-handshake-lower-extmem-to-hw` 排在 `-handshake-materialize-forks-sinks` 之前，而不是之后？

**参考答案**：因为「把外部存储落地到 HW」这一步可能引入新的数据流连接（新的 fork/sink 用法）。先做 extmem 降级、再做 fork/sink 实体化，能保证实体化覆盖的是「存储处理之后的最终形态」，不会漏掉因存储落地而新冒出来的 fork/sink；反过来做则可能漏补，导致下一步降级失败。

**练习 2**：matmul 仿真（u4-l2）里出现的 `dut.handshake_memory0` 层次化路径，对应本段哪一个 pass 的产物？

**参考答案**：对应 `-handshake-lower-extmem-to-hw`。它把 `handshake.extmemory` 抽象降级成具体的硬件存储模块（对 matmul 的小存储就是内部 `handshake_memoryN` 实例），这正是后续 testbench 能按名字 `handshake_memory0` 播种数据的前提。

---

### 4.2 Handshake→HW 与 ESI 桥梁降级（hs_ext_to_hw0.sh + hw0_to_hw.sh）

#### 4.2.1 概念说明

4.1 完成后，存储已落地、fork/sink 已显式，但**逻辑主体仍是 Handshake 方言**——它还是一张「节点 + 抽象通道」的数据流图。本模块要把这张图彻底降级成 `hw` 方言（具体的模块、端口、连线），分两段完成，恰好对应两个脚本：

- **hs_ext_to_hw0.sh**：跑 `-lower-handshake-to-hw`。这一步把 Handshake 数据流网络翻译成 HW 方言——`handshake.func` 变成 `hw.module`，Handshake 节点变成 HW 逻辑。**但**它产生的通道仍是 ESI 抽象（`!esi.channel<T>`），端口也还是 ESI 端口。所以这一段的产物叫 `hw0`——「第 0 版 HW」，还带着 ESI 外壳。
- **hw0_to_hw.sh**：跑 `-lower-esi-types` / `-lower-esi-ports` / `-lower-esi-to-hw`。这一步把 ESI 外壳**溶解**掉——ESI 通道类型被拆成具体的 HW 类型，ESI 端口被拆成 `data`/`valid`/`ready` 这些独立 HW 端口，残留的 ESI 操作被降级成 HW 操作。产物叫 `hw`——「纯 HW」，可以被 SV 后端接受。

为什么要分两段、而不是一个 pass 直接 Handshake→HW？这正是 2.2 提到的「**每段降级只溶解一层抽象**」的设计：Handshake→HW 这一步如果一次性把所有事情（逻辑翻译 + 通道实体化 + 端口实体化）都做完，会是一个巨大、难维护、难调试的 pass；插入 ESI 中间层后，Handshake→HW 只管「逻辑翻译」，ESI→HW 只管「通道/端口实体化」，两个 pass 各司其职、可独立测试。这也呼应了 u2-l3 讲过的「每站只降一段」哲学。

> 一个工程上的额外收益：拆成两个独立 Nix 派生后（见 `mkHw0Derivation`/`mkHwDerivation`），改了其中一段不必重跑另一段——缓存粒度更细。

#### 4.2.2 核心流程

两段降级合起来的伪代码：

```
输入: hs-ext MLIR（Handshake 逻辑 + 已落地的存储 + 显式 fork/sink）
  │
  │  ── hs_ext_to_hw0.sh ──
  ├─ -lower-handshake-to-hw
  │     作用: Handshake 网络 → hw.module + HW 逻辑
  │     副产物: 握手通道 → ESI 通道（!esi.channel<T>），端口 → ESI 端口
  │
  │  ── hw0_to_hw.sh ──
  ├─ -lower-esi-types      ESI 通道类型 → HW 类型（数据 + 流控拆开）
  ├─ -lower-esi-ports      ESI 模块端口 → 具体 HW 端口（data/valid/ready 分离）
  ├─ -lower-esi-to-hw      残留 ESI 操作 → HW 操作
  └─ -canonicalize
  │
输出: hw MLIR（纯 HW 方言，无 ESI 残留，通道已是具体连线）
```

**数据依赖（本讲核心）**：`-lower-handshake-to-hw` 与 `-lower-esi-to-hw` 之间存在严格的**生产—消费**关系：

\[ \text{Handshake} \xrightarrow{\text{lower-handshake-to-hw}} \text{HW 逻辑 + ESI 通道} \xrightarrow{\text{lower-esi-*}} \text{纯 HW} \]

- `-lower-handshake-to-hw` **生产** ESI 通道/端口（它是把握手通道翻译成 ESI 的一方）；
- `-lower-esi-*` **消费** ESI 通道/端口（它把 ESI 溶解成纯 HW）。

颠倒顺序会两头落空：先跑 `-lower-esi-to-hw` 时，IR 里根本没有 ESI（还是 Handshake），无事可做；后跑 `-lower-handshake-to-hw` 时，它产出的 ESI 又永远不会被溶解，残留在最终 HW 里——而 SV 后端不认 ESI，导出会失败。**顺序不能颠倒，根源是这条数据依赖**。

#### 4.2.3 源码精读

**第 2 段** [scripts/pipeline/hs_ext_to_hw0.sh:15-18](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hs_ext_to_hw0.sh#L15-L18) 极简，只两个 pass：

```bash
run_to_output "$output" "$circt_opt" "$input" \
  -lower-handshake-to-hw \
  -canonicalize
```

`-lower-handshake-to-hw` 是这一段唯一的主角——它把整个 Handshake 函数降级成 `hw.module`，节点降级成 HW 逻辑，并把握手通道交给 ESI 表示。后面的 `-canonicalize` 做一轮清理。注意这里**没有** `-cse`：HW 方言的公共子表达式消除留到 4.3 统一做。

**第 3 段** [scripts/pipeline/hw0_to_hw.sh:15-19](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw0_to_hw.sh#L15-L19) 是三个 ESI pass 的固定序列：

```bash
run_to_output "$output" "$circt_opt" "$input" \
  -lower-esi-types \
  -lower-esi-ports \
  -lower-esi-to-hw \
  -canonicalize
```

这三个 pass 的内部顺序同样是「从类型到端口到操作」的逐层溶解（确切语义以本项目 pin 的 CIRCT 版本为准，**待本地验证**）：

- `-lower-esi-types`：先把 ESI 的**通道类型**（如 `!esi.channel<T>`）降级成 HW 能理解的类型（把「数据 + 流控」的打包类型拆开）；
- `-lower-esi-ports`：再把模块上仍标着 ESI 的**端口**降级成具体 HW 端口（一条 ESI 通道端口 → `data`/`valid`/`ready` 多个独立端口）；
- `-lower-esi-to-hw`：最后把残留的 **ESI 操作**降级成 HW 操作，彻底清除 ESI 方言。

这段产出就是「纯 HW」——此时 IR 里既无 Handshake 也无 ESI，只剩下 `hw.module`、端口、连线和模块实例，已具备导出 SystemVerilog 的条件。

> 你在 u3-l2 学到的 `valid`/`ready` 握手信号，经过 4.1（存储落地）→ 4.2（Handshake→HW→ESI→HW）后，最终就变成了这里 `-lower-esi-ports` 产出的、`hw` 模块端口上那几根具体的 `valid`/`ready`/`data` 连线。这正是 u4-l2 的 testbench 能用 valid/ready 与 DUT 握手的根本来源——本讲是把抽象握手「实体化成线」的一讲。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「ESI 是 Handshake→HW 的中间产物」，并验证三条 ESI pass 缺一不可。

**操作步骤**：

1. 在 `nix develop` 里取得 4.1 的产物 `hs-ext.mlir`（见 4.1.4）。
2. **跑第 2 段**：
   ```bash
   circt-opt hs-ext.mlir -lower-handshake-to-hw -canonicalize > hw0.mlir
   ```
3. 在 `hw0.mlir` 里搜索 ESI 痕迹：尝试 `grep -n 'esi' hw0.mlir`（或肉眼查找 `!esi.`、`esi.channel` 等字样）。**预期**：能找到 ESI 类型/端口/操作的残留，证明 `-lower-handshake-to-hw` 确实「生产」了 ESI。
4. **跑第 3 段**：
   ```bash
   circt-opt hw0.mlir -lower-esi-types -lower-esi-ports -lower-esi-to-hw -canonicalize > hw.mlir
   ```
5. 再 `grep -n 'esi' hw.mlir`。**预期**：几乎无 ESI 残留，端口上出现拆开的 `valid`/`ready`/`data`（具体拼写**待本地验证**）。

**需要观察的现象**：`hw0` 里「有 ESI」，`hw` 里「无 ESI」——这条对比正是「生产—消费」数据依赖的直观证据。

**预期结果**：若跳过第 3 段直接把 `hw0.mlir` 喂给后续 SV 导出（u3-l4 的 `hw_clean_to_sv.sh`），会因 ESI 未溶解而失败。这验证了「顺序不能颠倒、也不能跳过 ESI 段」。

#### 4.2.5 小练习与答案

**练习 1**：用一句话解释为什么 `-lower-handshake-to-hw` 必须在 `-lower-esi-to-hw` 之前。

**参考答案**：因为 `-lower-handshake-to-hw` 在把 Handshake 降成 HW 时会**产生** ESI 通道/端口来承载握手语义，而 `-lower-esi-to-hw` 的职责正是**消除**这些 ESI——前者是 ESI 的生产者，后者是消费者，存在数据依赖，顺序不能颠倒。

**练习 2**：如果把第 3 段的三个 pass 顺序改成 `-lower-esi-to-hw` → `-lower-esi-ports` → `-lower-esi-types`，会发生什么？

**参考答案**：很可能失败或产出非法 IR。这三个 pass 是「从类型、到端口、到操作」的逐层溶解：操作（`to-hw`）往往引用端口、端口引用类型；若先降操作、再降端口、最后降类型，后续 pass 会遇到「上层引用的类型/端口已经被下层改掉」的不一致状态。正确顺序是自底向上（类型→端口→操作）逐层清理依赖。确切行为**待本地验证**。

**练习 3**：为什么 `hs_ext_to_hw0.sh` 只跑 `-canonicalize` 而不跑 `-cse`，把 `-cse` 留到 4.3？

**参考答案**：因为这一段刚把 Handshake 翻成 HW + ESI，IR 还在「半成品」状态（带着 ESI 外壳），此时做 CSE 收益有限且可能干扰后续 ESI 溶解；等 4.3 的 ESI 全部溶解、IR 达到「纯 HW」稳定形态后，再做一次 `-cse`（连同符号 DCE）能更彻底地清理。这是「把清理集中到稳定点」的常见做法。

---

### 4.3 符号 DCE 与 canonicalize/cse 清理（hw_to_hw_clean.sh）

#### 4.3.1 概念说明

经过 4.2，我们得到了「纯 HW」IR。但它还不「干净」：前面几段降级会**残留**不少无用构造——

- 一些在降级过程中被生成、但最终没有任何模块引用的**辅助模块**（死符号）；
- 一些冗余的、可被简化的**逻辑模式**（比如恒等连接、重复计算）。

如果就这样交给 SV 后端，生成的 SystemVerilog 会带着一堆无用模块和冗余逻辑，既增大体积，也可能干扰后续 Yosys 综合（u5）。`hw_to_hw_clean.sh` 的职责就是在导出 SV 之前做**最后一道清理**：用符号 DCE 删除死模块，用 canonicalize/cse 化简逻辑。

这里有两个 DCE pass，分工不同（参见 2.3）：

- **`-firrtl-inner-symbol-dce`**：CIRCT 里 FIRRTL/HW 体系用一种叫 **inner symbol**（内部符号）的机制做层次化引用（比如「模块 A 里的某个实例/端口」）。这个 pass 专门清理「被声明为 inner symbol 但已无人引用」的内部符号。
- **`-symbol-dce`**：更通用的**符号死代码消除**——找出所有「定义了但全 IR 范围内无人引用」的符号（典型是无人实例化的 `hw.module`、无人实现的 `hw.module.extern` 占位等）并删除。

两者协同：先清 inner symbol，再做通用 symbol DCE，能把死代码扫得比较干净。之后再 `-canonicalize`（模式化简、常量折叠）+ `-cse`（公共子表达式消除）做最后一轮逻辑化简。

#### 4.3.2 核心流程

```
输入: hw MLIR（纯 HW，但含死符号与冗余逻辑）
  │
  ├─ -firrtl-inner-symbol-dce   清理无用的 inner symbol（层次化引用符号）
  ├─ -symbol-dce                删除全 IR 范围内无人引用的符号（死模块等）
  ├─ -canonicalize              模式化简、常量折叠
  └─ -cse                       公共子表达式消除
  │
输出: hw-clean MLIR（干净、最小化的 HW，可直接喂给 SV 导出）
```

「先 DCE 再化简」的顺序也合理：先把死模块删掉（减少 IR 体积），再做 canonicalize/cse（化简更聚焦于真正留下的逻辑）。

#### 4.3.3 源码精读

[scripts/pipeline/hw_to_hw_clean.sh:15-19](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_to_hw_clean.sh#L15-L19)：

```bash
run_to_output "$output" "$circt_opt" "$input" \
  -firrtl-inner-symbol-dce \
  -symbol-dce \
  -canonicalize \
  -cse
```

四个 pass 的职责如 4.3.2 所列。注意此脚本与 4.1 的 `handshake_to_hs_ext.sh` 末尾同样用了 `-canonicalize`/`-cse` 组合——这是降级链上反复出现的「收尾清理」套路：每完成一段实质性降级，就跟一轮 canonicalize/cse 把抖出来的冗余压平。区别在于本脚本多了两个 DCE pass，因为这是**导出前的最后一站**，要尽可能删干净。

本段在 Nix 里的封装见 [nix/pipeline.nix:68-72](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L68-L72)：

```nix
mkHwCleanDerivation = { name, hw }:
  pkgs.runCommand "${name}-hw-clean.mlir" { buildInputs = [ circt ]; } ''
    ${pkgs.bash}/bin/bash ${pipelineScripts}/hw_to_hw_clean.sh \
      ${circt}/bin/circt-opt ${hw} "$out"
  '';
```

它把本脚本包成一个 `runCommand` 派生，输入是上一段 `hw`、输出落到 `$out`（即 `*-hw-clean.mlir`）。这个产物紧接着被 [nix/pipeline.nix:74-84](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L74-L84) 的 `mkSvDerivation` 消费，开始 SV 导出（u3-l4）。

#### 4.3.4 代码实践

**实践目标**：量化「清理」到底删掉了多少东西，验证 DCE 的价值。

**操作步骤**：

1. 取 4.2 的产物 `hw.mlir`，统计其规模，例如行数与模块数：
   ```bash
   wc -l hw.mlir
   grep -c '^hw\.module' hw.mlir
   ```
2. 跑第 4 段：
   ```bash
   circt-opt hw.mlir \
     -firrtl-inner-symbol-dce -symbol-dce -canonicalize -cse \
     > hw-clean.mlir
   ```
3. 对 `hw-clean.mlir` 重复同样的统计。

**需要观察的现象**：`hw-clean.mlir` 通常**行数更少、模块数不增（可能减少）**——直观体现 DCE 与 cse 删掉了死代码和冗余。

**预期结果**：模块数下降说明 `-symbol-dce` 删掉了无人引用的辅助模块；行数下降说明 canonicalize/cse 化简了逻辑。若两者几乎不变，说明该设计本身已经很精简（matmul 这种小核有可能如此），可在 TinyStories-1M 的大模型上观察更明显的差异。具体数字**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`-firrtl-inner-symbol-dce` 与 `-symbol-dce` 为什么不能只留一个？

**参考答案**：它们清理的「符号」层次不同：`-firrtl-inner-symbol-dce` 专清 FIRRTL/HW 体系的**内部符号**（用于模块内层次化引用），而 `-symbol-dce` 清的是**全 IR 范围的顶层符号**（如整个 `hw.module` 定义）。两者覆盖的死代码集合不完全重合，配合使用才能把死符号清得更彻底；只留一个会漏掉另一层的死代码。

**练习 2**：为什么把 `-canonicalize`/`-cse` 放在两个 DCE **之后**，而不是之前？

**参考答案**：先 DCE 删除死模块/死符号，能减小 IR 体积、让后续 canonicalize/cse 不必去化简那些马上要被删的东西；化简在「精简后的活代码」上做，更聚焦也更彻底。若反过来先化简、再 DCE，则化简阶段会白费力气处理一批最终被删的死代码。

---

## 5. 综合实践

把本讲四个脚本与三个核心机制串起来，完成下面这张「四段接力」的数据流图实践——这也是本讲的主线任务。

### 5.1 画一张数据流箭头图

请为这四个脚本画一张数据流箭头图，节点是四个阶段的 IR 形态，箭头上标注该段用到的关键 `circt-opt` pass。参考骨架（请你自己补全箭头上的 pass 与每段产物的方言特征）：

```
   Handshake MLIR                  （输入：u3-l2 产物，含 handshake.extmemory）
        │
        │  handshake_to_hs_ext.sh
        │  ├─ -handshake-lower-extmem-to-hw     （外部存储落地为 HW 存储构造）
        │  ├─ -handshake-materialize-forks-sinks（隐式 fork/sink 实体化）
        │  └─ -canonicalize / -cse
        ▼
   hs-ext MLIR                     （存储已落地、fork/sink 显式；逻辑仍是 Handshake）
        │
        │  hs_ext_to_hw0.sh
        │  └─ -lower-handshake-to-hw            （Handshake 网络 → hw.module；产生 ESI）
        ▼
   hw0 MLIR                        （HW 逻辑 + ESI 通道/端口外壳）
        │
        │  hw0_to_hw.sh
        │  ├─ -lower-esi-types                   （ESI 通道类型 → HW 类型）
        │  ├─ -lower-esi-ports                   （ESI 端口 → data/valid/ready 端口）
        │  ├─ -lower-esi-to-hw                   （残留 ESI 操作 → HW 操作）
        │  └─ -canonicalize
        ▼
   hw MLIR                         （纯 HW 方言，无 ESI 残留）
        │
        │  hw_to_hw_clean.sh
        │  ├─ -firrtl-inner-symbol-dce / -symbol-dce （删死符号）
        │  └─ -canonicalize / -cse                   （逻辑化简）
        ▼
   hw-clean MLIR                   （干净的最小 HW，交给 u3-l4 导出 SV）
```

**你要做的事**：

1. 在自己画的图上，确认每个箭头标注的 pass 与上面四个脚本的真实内容**逐字对应**（以本讲 4.x.3 引用的源码行号为准）。
2. 在每个阶段节点旁，用一句话写出该阶段 IR 的**方言特征**（如「hw0 = HW 逻辑 + ESI 外壳」），检验你是否真理解「每段溶解了一层什么抽象」。

### 5.2 回答核心问题：顺序为什么不能颠倒

结合 5.1 的图，写一段不超过 150 字的说明，解释**`-lower-handshake-to-hw` 与 `-lower-esi-to-hw` 的先后为什么不能颠倒**。你的回答应当包含以下两个要点：

1. **数据依赖**：`-lower-handshake-to-hw` 是 ESI 的**生产者**（把握手通道翻译成 ESI 通道/端口），`-lower-esi-to-hw` 是 ESI 的**消费者**（把 ESI 溶解成纯 HW）。
2. **颠倒的后果**：先跑 ESI 段时 IR 里还没有 ESI（还是 Handshake），无事可做；后跑 handshake 段时它产出的 ESI 再也不会被溶解，残留进最终 HW，导致 SV 导出（u3-l4）失败。

> **参考答案**（写完后再对照）：因为 `-lower-handshake-to-hw` 在把 Handshake 降到 HW 时会**生成** ESI 通道/端口来承载 valid/ready 握手语义，而 `-lower-esi-*` 的职责正是把这些 ESI **溶解**成 data/valid/ready 具体连线。前者生产 ESI、后者消费 ESI，是一条单向数据依赖；颠倒后，ESI 段面对「还没有 ESI」的 Handshake IR 无能为力，而 handshake 段事后生成的 ESI 又永远没人溶解，残留在 HW 里使 SV 导出失败。

### 5.3（可选）跟踪 Nix 派生链

在 [nix/pipeline.nix:114-129](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L114-L129) 的 `mkPipeline` 里，`hs-ext` → `hw0` → `hw` → `hw-clean` 四个属性通过 `let self = {...}` 的**惰性自引用**串接（每个属性引用 `self.<上一段>`）。请跟踪 matmul 模型，列出这四段对应的派生名（形如 `matmul-hs-ext.mlir`、`matmul-hw0.mlir`、`matmul-hw.mlir`、`matmul-hw-clean.mlir`），并回答：**如果你只改了 `hw_to_hw_clean.sh`，哪几段会重算、哪几段会命中缓存？**

> **参考答案**：改 `hw_to_hw_clean.sh` 只影响 `mkHwCleanDerivation`，因此只有 `matmul-hw-clean.mlir` 这一段重算；`hs-ext`、`hw0`、`hw` 三段的输入未变，命中 Nix 缓存不重算。这正是「把一次降级拆成多个独立派生」的工程收益——细粒度缓存。

---

## 6. 本讲小结

- 本讲用 **四个脚本接力**完成了 Handshake → HW 的降级：`handshake_to_hs_ext.sh`（外部存储落地 + fork/sink 实体化）→ `hs_ext_to_hw0.sh`（Handshake 网络降成 HW，产生 ESI）→ `hw0_to_hw.sh`（ESI 溶解成纯 HW）→ `hw_to_hw_clean.sh`（符号 DCE + 化简）。
- **外部存储必须先落地**：`-handshake-lower-extmem-to-hw` 把 u3-l2 建立的 `handshake.extmemory` 抽象降级成具体 HW 存储构造（matmul 的内部 `handshake_memoryN`、或大存储的 `hw.module.extern`），这一步必须先于整体 Handshake→HW 降级。
- **ESI 是 Handshake→HW 的桥梁**：`-lower-handshake-to-hw` 把握手通道翻译成 ESI 通道/端口，`-lower-esi-types/-ports/-to-hw` 再把 ESI 溶解成 `data`/`valid`/`ready` 具体连线——两段各溶解一层抽象。
- **顺序不可颠倒的根源是数据依赖**：`-lower-handshake-to-hw` 生产 ESI，`-lower-esi-*` 消费 ESI；颠倒会让 ESI 段面对 Handshake 无能为力、生成的 ESI 又无人溶解，最终 SV 导出失败。
- **`-handshake-materialize-forks-sinks` 是必要前置**：它把 Handshake 允许的隐式 fork/sink 实体化成显式节点，否则 `-lower-handshake-to-hw` 无法把它们映射成硬件 fork/sink 部件。
- **最后一站做集中清理**：`-firrtl-inner-symbol-dce` + `-symbol-dce` 删死符号，`-canonicalize` + `-cse` 化简逻辑，把干净的最小 HW 交给 u3-l4 导出 SystemVerilog；四个脚本各包成独立 Nix 派生，换来细粒度缓存。

---

## 7. 下一步学习建议

- **紧接的下一讲 u3-l4（HW → SystemVerilog 导出与 FP extern 处理）**：本讲产出的 `hw-clean.mlir` 在那里被 `hw_clean_to_sv.sh` 用 `-lower-seq-*` / `-lower-hw-to-sv` 降到 SystemVerilog。你会看到本讲残留的 `hw.module.extern`（来自外部存储或浮点算子）如何在那里被检测、并用 `ALLOW_HW_EXTERNS` + `FP_PRIMS_SV` 挂接到外部 SV 实现文件——这正是本讲 4.1 提到的「大存储/外部存储产生 extern」的下游落点。
- **横向回看 u3-l2**：对比 u3-l2 建立 `handshake.extmemory` 抽象与本讲 4.1 把它落地，体会「建立抽象—溶解抽象」的对称设计。
- **纵向到 u4-l2（Verilator 仿真）**：本讲 `-lower-esi-ports` 产出的 `valid`/`ready`/`data` 端口，正是 u4-l2 testbench 能与 DUT 握手、并能按 `dut.handshake_memory0` 播种数据的物理基础。
- **若想深入 ESI 本身**：可阅读 CIRCT 上游关于 ESI（Elastic Silicon Interconnect）方言与 `lower-esi-*` pass 的文档，理解「服务（service）/通道（channel）/端口（port）」三层抽象的完整设计；注意本项目 pin 的是作者 task3 fork（见 u1-l3），个别 pass 行为可能与主线略有差异，需**本地验证**。
