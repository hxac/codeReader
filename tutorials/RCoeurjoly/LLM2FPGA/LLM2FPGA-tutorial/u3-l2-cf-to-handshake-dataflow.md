# CF 到 Handshake：弹性数据流降级

> 本讲承接 u3-l1。u3-l1 的产物是「**CF + memref form**」——IR 已经是基本块 + 跳转（`cf.br`/`cf.cond_br`）、内存已经是 `memref.load`/`memref.store`、函数也已经出参化。但这一切**仍然是冯·诺依曼式的「中央控制器逐条执行指令」模型**：有一个程序计数器，按控制流图（CFG）一条条走。本讲要完成降级链上一次**范式跃迁**：把这种「时序控制流」翻成「**弹性数据流（elastic dataflow）**」——没有一个集中的控制器，取而代之的是一张由节点和通道组成的网络，数据带着 `valid`/`ready` 握手信号自己流动。完成这件事的脚本只有 46 行，却有三个鲜明特征：**三段式、用两个工具（`circt-opt` 和 `mlir-opt`）、靠临时文件串接**。这一切都藏在 `cf_to_handshake.sh` 里。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 用自己的话说清 **Handshake 弹性数据流**模型：什么是 `valid`/`ready` 握手、什么是「弹性」、它和「时序控制流」的根本区别。
2. 解释本站**三段式**的每一站做什么、为什么这样排序：① `flatten-memref` + `handshake-legalize-memrefs` 把内存整理成 Handshake 认识的样子；② `convert-scf-to-cf` 把内存合法化时**重新冒出来的 SCF** 再压回 CF；③ `lower-cf-to-handshake` + `handshake-insert-buffers` 把控制流翻成数据流并插缓冲。
3. 回答本讲核心实践问题：**为什么必须先 `flatten-memref` 再 `lower-cf-to-handshake`**（而不是直接 lower）。
4. 说清 `handshake-insert-buffers` 的「收益与代价」：缓冲打破组合环、避免死锁、提供吞吐，但每一个缓冲都是 FPGA 上的 FF/LUT/BRAM——这正是 3e 报告把 Handshake 列为「最大负担之一」、Task 6 想替换它的原因。
5. 看懂 `cf_to_handshake.sh` 为何是降级链上**唯一同时调用两个工具**的站（`circt-opt` 干 CIRCT 专属的活、`mlir-opt` 干上游标准的活），以及它为何要用 `mktemp` + `trap` 而不是一行管道。

---

## 2. 前置知识

本讲默认你读过 u3-l1（Linalg→CF 的 bufferize 与循环展开）及其引入的「降级 / 方言 / tensor vs memref / SCF vs CF / common.sh 脚手架」概念。下面只补本讲新出现的概念，全部用通俗语言。

### 2.1 从「控制流」到「数据流」：一次范式切换

u3-l1 结束时，你的 IR 长这样（示意）：

```mlir
// —— 示例代码（控制流模型，承接 u3-l1）——
func.func @main(%a : memref<16xi32>, %out : memref<16xi32>) {
  cf.br ^entry
^entry:
  ... memref.load ... memref.store ...
  cf.cond_br %cond, ^then, ^else      // 「程序计数器」跳到哪个块
^then:  ...; cf.br ^join
^else:  ...; cf.br ^join
^join:  ...
}
```

注意它的心智模型：**有一个隐含的「程序计数器」**，沿着 `cf.br`/`cf.cond_br` 在基本块之间跳，一条条执行指令。这和你写 C、CPU 跑程序是同一个模型——**时序控制流（imperative control flow）**。

本站要把它翻成另一个模型——**数据流（dataflow）**：

```mlir
// —— 示例代码（数据流模型，本站产物）——
handshake.func @main(%a : !handshake.channel<...>, %out : !handshake.channel<...>) {
  // 没有基本块、没有跳转；只有一张「节点 + 通道」的图
  %x = handshake.load %a ... : !handshake.channel<...>   // 一个数据流节点
  %y = handshake.<某运算> %x ...                          // 连到下一个节点
  handshake.store %y, %out ...
}
```

心智模型彻底变了：**没有程序计数器，没有「先执行 A 再执行 B」的时序**。取而代之的是一张图：每个节点（node）是一个运算，节点之间用**通道（channel）**连起来；数据像水流一样在通道里淌，一个节点只要它的输入数据「到位」就可以启动（fire），不用等全局时钟节拍。这就是数据流。

### 2.2 什么是 Handshake 方言，什么是「弹性」

CIRCT 里的 **Handshake 方言** 是一种具体的**弹性数据流（elastic dataflow）**表示，灵感来自「elastic circuits」和同步数据流。它的核心机制是**每个通道都带握手（handshake）信号**：

- 每条通道上除了传「数据」本身，还并排着两条控制线：
  - **`valid`（有效）**：生产者说「我这条通道上的数据准备好了，有效」。
  - **`ready`（就绪）**：消费者说「我现在能接收」。
- **一次传输（transaction）发生，当且仅当同一拍里 `valid` 和 `ready` 同时为高**。此时数据从生产者传给消费者。

「弹性」二字就来自这里：**每个节点可以独立地停（stall）**。如果一个节点暂时算不出来，它就把自己的 `ready` 或下游 `valid` 压低，整条流水线会自动反压（backpressure）停下来，而不会丢数据。对比之下，一个「只有 valid、没有 ready」的简单流水线，最慢的一级会强行决定全局节拍，且无法反压。弹性数据流让每个节点自带节奏感，彼此协调。

> 一个最简的握手时序示意（示例代码，非真实波形）：
>
> ```text
>          +-+ +-+ +-+ +-+
> valid :  | | | | | | | |     生产者：第 1、3 拍有数据
>          +-+ +-+ +-+ +-+
>          +--+ +--+ +--+
> ready :     |    |    |      消费者：第 2、4 拍才能接
>          +--+ +--+ +--+
> 传输   :  ↑    ↑    ↑        只在 valid & ready 同时高的拍才真正传数据
> ```

### 2.3 为什么数据流里「内存」要变成「外部存储」

在控制流模型里，`memref.load %m[%i]` 很直白：去内存 `m` 的地址 `%i` 取一个数。内存是一块「谁都能随时读写」的共享状态。

但数据流模型里，**「随时能读写的共享状态」是个异类**——数据流图里一切都是「输入到位 → 输出产生」的纯节点，没有「状态被谁偷偷改了」的概念。所以 Handshake 把内存处理成**外部存储（external memory）**：

- 内存本身被「请出」数据流图，用一个 `handshake.extmemory`（external memory）节点代表它——一块在数据流图**之外**的真实存储。
- 对它的每一次读/写，变成数据流图里的 `handshake.load` / `handshake.store` **节点**，这些节点通过**带握手的通道**和那块外部存储通信：发一个「地址请求」通道、收一个「数据返回」通道（读）；或发一个「地址+数据」通道、收一个「完成」通道（写）。

本站把 `memref` 内存翻成这种「外部存储 + load/store 节点」抽象的关键 pass 是 `handshake-legalize-memrefs`（见 4.1）。这也解释了为什么 u4-l2 的仿真要「直接给内部 handshake memory 播种」、u6-l2 要「外部化超大存储」——Handshake 一开始就把内存模型做成了「可外部化的端口」，后面这些机制才有立足点。

### 2.4 为什么要 `flatten-memref`：把多维内存压扁

MLIR 的 `memref` 可以是多维的，比如 `memref<4x8xi32>`，访问时写 `memref.load %m[%i, %j]`。内存里的真实排布是一维的，那个多维下标 `[%i, %j]` 是靠**步长（stride）**翻译成一维地址的。

Handshake 的外部存储模型更喜欢**简单的一维内存**——一块平坦的存储，配一个线性的地址通道。多维 memref 带着复杂布局（各种 stride、subview、投影），直接交给 `handshake-legalize-memrefs` 会更难、更容易出问题。所以本站先用 `flatten-memref` 把多维 memref **线性化**：

```text
// —— 示例代码（线性化示意）——
// 压扁前：二维 memref，下标 [%i, %j]
memref.load %m[%i, %j] : memref<4x8xi32>

// 压扁后：一维 memref，下标已经是线性地址
%addr = arith.muli %i, %c8 : index      ; %i * 8
%addr2 = arith.addi %addr, %j : index   ; %i * 8 + %j
memref.load %m_flat[%addr2] : memref<32xi32>
```

线性化的数学本质是：对一个形状 \((d_0, d_1, \dots, d_{n-1})\) 的 memref，把多维索引 \((i_0, i_1, \dots, i_{n-1})\) 映射到线性地址。以**行主序（row-major）**为例：

\[
\text{addr}(i_0, i_1, \dots, i_{n-1}) \;=\; \sum_{k=0}^{n-1} i_k \prod_{j=k+1}^{n-1} d_j
\]

`flatten-memref` 就是把这种多维→一维的地址计算**显式地**插进 IR。压扁之后，`handshake-legalize-memrefs` 就只需面对一维内存，干净利落。

### 2.5 数据流里的「环」与「缓冲」

把控制流翻成数据流时，会遇到一个控制流里没有的问题：**组合环（combinational cycle）**。

想象数据流图里 A 的输出喂给 B，B 的输出又（经某条路径）回到 A。在控制流里这不成问题（时序上 A 先 B 后）；但在数据流里，如果这条回路**没有任何存储**，那么 A 等 B 的 `valid`、B 又等 A 的 `valid`，于是谁也启动不了——**死锁**；即使不死锁，也是一条纯组合的反馈线，物理上无法实现（组合逻辑不能成环）。

解决办法是**在通道上插缓冲（buffer）**：一个 `handshake.buffer` 节点相当于在通道里塞了一个寄存器或小 FIFO，把「瞬间同时发生的依赖」断开成「跨周期的依赖」。插了缓冲，回路里就有了存储，环被打破，电路能跑、有吞吐。

\[
\text{环 } A \to B \to A \;\xrightarrow{\text{插缓冲}}\; A \to B \to \underbrace{[\text{buffer}]}_{\text{存储，打破组合环}} \to A
\]

`handshake-insert-buffers` 就是干这个的。但**缓冲是要花钱的**：每个缓冲是一组触发器（FF，存数据槽）+ 一点控制逻辑（LUT），大一点的就吃 BRAM。一张数据流图越大、环路越多，插的缓冲就越多，资源开销就越重——这是 4.3 和综合实践要反复回扣的点。

### 2.6 本站会用到两个工具，而非一个

u3-l1 强调过「每站用哪个工具」的判断：上游标准 pass 用 `mlir-opt`，CIRCT 专属 pass 用 `circt-opt`。**本站是降级链上唯一同时调用两个工具的站**——它既要用 CIRCT 的 `flatten-memref`/`handshake-*`（必须 `circt-opt`），又要用上游的 `convert-scf-to-cf`（脚本里特意用 `mlir-opt`）。这就是为什么本脚本签名里有两个工具参数（`circt_opt`、`mlir_opt`），比邻居多一个。

---

## 3. 本讲源码地图

本讲只涉及两个主文件，`common.sh` 在 u2-l3/u3-l1 已详解，这里直接复用：

| 文件 | 行数 | 职责 |
|---|---|---|
| [scripts/pipeline/cf_to_handshake.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/cf_to_handshake.sh) | 46 | 本站主体：校验输入后，分**三段**（分别用 `circt-opt`、`mlir-opt`、`circt-opt`）把 CF+memref 翻成 Handshake 数据流，靠两个临时文件串接。 |
| [nix/pipeline.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix) | 156 | 把本站包成可缓存的 Nix 派生；本讲聚焦 `mkHandshakeDerivation`（L42–L48，注意它同时传 `circt-opt` 和 `mlir-opt`）与 `mkPipeline` 里 `handshake` 段（L110–L113）。 |
| [scripts/pipeline/common.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/common.sh) | 24 | u2-l3 已讲透的共享脚手架（`require_file`/`require_executable`/`run_to_output`）。 |

一句话定位：**`cf_to_handshake.sh` 是降级链的第四站**。它读入 u3-l1 产出的「CF + memref」`.mlir`，吐出 Handshake 数据流 `.mlir`，交给下一站 `handshake_to_hs_ext.sh`（u3-l3）。

先给本站全貌——三个 `run_to_output` 调用，构成三段式：

[scripts/pipeline/cf_to_handshake.sh:24-45](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/cf_to_handshake.sh#L24-L45) —— 本站核心：三段式降级，`circt-opt` → `mlir-opt` → `circt-opt`：

```bash
# Stage 1: Lower to CF+memref form expected by handshake legalization.
run_to_output "$tmp_legal" "$circt_opt" "$input" \
  -flatten-memref \
  -flatten-memref-calls \
  -canonicalize \
  -cse \
  -handshake-legalize-memrefs \
  -canonicalize \
  -cse

# Stage 2: Normalize SCF to CF after memref legalization.
run_to_output "$tmp_norm" "$mlir_opt" "$tmp_legal" \
  -convert-scf-to-cf \
  -canonicalize \
  -cse

# Stage 3: Lower normalized CF to Handshake with inserted buffers.
run_to_output "$output" "$circt_opt" "$tmp_norm" \
  --lower-cf-to-handshake \
  -handshake-insert-buffers \
  -canonicalize \
  -cse
```

三段与本讲三个最小模块一一对应，且每段都用了不同的临时文件（`$tmp_legal`、`$tmp_norm`、最终 `$output`）：

| 段 | 行 | 工具 | 输入 → 输出 | 模块 | 一句话作用 |
|---|---|---|---|---|---|
| Stage 1 | L24–L32 | `circt-opt` | `$input` → `$tmp_legal` | ① flatten + memref 合法化 | 把多维 memref 压扁、再把内存翻成 Handshake 外部存储 |
| Stage 2 | L34–L38 | `mlir-opt` | `$tmp_legal` → `$tmp_norm` | ② SCF 归一为 CF | 把内存合法化时重新冒出来的 SCF 压回 CF |
| Stage 3 | L40–L45 | `circt-opt` | `$tmp_norm` → `$output` | ③ lower + 插缓冲 | 把控制流翻成数据流图，并插入缓冲 |

下面三个小节分别精讲这三段。

---

## 4. 核心概念与源码讲解

### 4.1 flatten-memref 与 memref 合法化：给 Handshake 铺好内存

#### 4.1.1 概念说明

`lower-cf-to-handshake`（4.3）只会翻译**控制流**——基本块、`cf.br`、`cf.cond_br`。它**不认识内存**：你给它一个 `memref.load %m[%i,%j]`，它不知道该把这块多维内存怎么映成数据流节点。

所以在把控制流翻成数据流**之前**，必须先把内存整理成 Handshake 认识的样子。这就是 Stage 1 的两步：

1. **`flatten-memref` + `flatten-memref-calls`**：按 2.4 把多维 memref 线性化成一维。`flatten-memref-calls` 是配套——memref 的类型被压扁后，函数调用处（callsite）传参的类型也得跟着改，这个 pass 负责把调用点同步过来，免得「函数体里是一维、调用方还在传二维」对不上。

2. **`handshake-legalize-memrefs`**：按 2.3 把 `memref` 内存翻成 Handshake 的**外部存储**抽象——内存变成 `handshake.extmemory` 外部引用，读写变成连到这块外部存储的 `handshake.load`/`handshake.store` 节点（带地址/数据/完成等握手通道）。

这两步是「先压扁、再合法化」的固定顺序：合法化最喜欢一维内存，所以压扁必须在前面。

#### 4.1.2 核心流程

```
u3-l1 产物（CF + 多维 memref）          Stage 1（circt-opt）             交给 Stage 2
┌─────────────────────────────┐   -flatten-memref                 ┌────────────────────────────┐
│ cf.br / cf.cond_br 基本块    │   -flatten-memref-calls          │ 仍是 CF 基本块              │
│ memref<4x8xi32> 多维         │  ─────────────────────────────► │ memref<32xi32> 一维（压扁） │
│ memref.load %m[%i,%j]        │   -handshake-legalize-memrefs    │ handshake.extmemory         │
│ （内存是「共享状态」）        │  ─────────────────────────────► │ handshake.load/store 节点   │
│                              │   （+ 两轮 -canonicalize/-cse）  │ （内存变外部存储 + 节点）    │
└─────────────────────────────┘                                  └────────────────────────────┘
```

注意 Stage 1 之后**控制流仍是 CF**（基本块 + 跳转没动），变的只是**内存的表示**。控制流的翻译留给 Stage 3。

#### 4.1.3 源码精读

Stage 1 全部在 [scripts/pipeline/cf_to_handshake.sh:25-32](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/cf_to_handshake.sh#L25-L32) —— 用 `circt-opt` 压扁 memref 并合法化内存：

```bash
run_to_output "$tmp_legal" "$circt_opt" "$input" \
  -flatten-memref \
  -flatten-memref-calls \
  -canonicalize \
  -cse \
  -handshake-legalize-memrefs \
  -canonicalize \
  -cse
```

逐行说明：

- 第 26 行 `-flatten-memref`：把多维 memref（如 `memref<4x8xi32>`）线性化成一维（`memref<32xi32>`），并把多维下标替换成 2.4 那样的线性地址计算（`%i*8 + %j`）。这是 CIRCT 注册的 pass，所以必须 `circt-opt`，上游 `mlir-opt` 没有。
- 第 27 行 `-flatten-memref-calls`：当 memref 类型因压扁而改变时，把所有调用点（callsite）的实参/形参类型同步更新，保持「函数体 ↔ 调用方」一致。
- 第 28–29 行 `-canonicalize` / `-cse`：压扁后清理一次（折叠线性地址计算里的常量、消除冗余）。
- 第 30 行 `-handshake-legalize-memrefs`：本段核心。把 `memref` 内存翻成 Handshake 外部存储抽象——内存请出数据流图为 `handshake.extmemory`，读写变 `handshake.load`/`handshake.store` 节点。改写前后对比（示例代码，非项目原始 IR）：

  ```mlir
  // —— 示例代码（Stage 1 前：控制流模型里的内存访问）——
  ^bb:
    %v = memref.load %m_flat[%addr] : memref<32xi32>   // 直接读「共享状态」
    memref.store %r, %m_flat[%addr2] : memref<32xi32>  // 直接写

  // —— 示例代码（Stage 1 后：内存已成为外部存储 + 节点）——
  ^bb:
    // %m 不再是一块「随时可读写」的状态，而是一个外部存储引用
    %v = handshake.load %m[%addr] : !handshake.extmemory<...>   // 一个数据流节点
    handshake.store %r, %m[%addr2] : !handshake.extmemory<...>  // 另一个节点
  ```

- 第 31–32 行 `-canonicalize` / `-cse`：合法化后再清理一次。注意 Stage 1 里 `canonicalize`/`cse` 出现**两次**（L28–L29 与 L31–L32），中间隔着一次会大改 IR 的 `handshake-legalize-memrefs`——先清一次让合法化面对更紧凑的 IR，合法化后再生成的冗余再清一次。这与 u3-l1「边降级边清理」的套路一致。

> 具体算子名（`handshake.extmemory`/`handshake.load`/`handshake.store` 的确切拼写、通道类型）取决于本项目 pin 的 CIRCT 版本（见 u1-l3 的 CIRCT fork），**待本地验证**。本节给出的是 Handshake 方言的通用形态。

#### 4.1.4 代码实践

**实践目标**：亲眼看到多维 `memref<...x...x...>` 在 `-flatten-memref` 后变一维，且 `memref.load/store` 在 `-handshake-legalize-memrefs` 后变成 `handshake.*` 节点。

**操作步骤**：

1. 进入 devShell（u1-l3）：
   ```bash
   nix develop
   ```
2. 造出 u3-l1 的产物作为输入（CF + memref）。若 `/tmp/cf-final.mlir` 还在，直接复用；否则按 u3-l1 综合实践跑一遍 `linalg_to_cf.sh` 得到它。
3. 只跑 Stage 1 的两个核心 pass，隔离观察（先不跑 `-flatten-memref-calls` 之外的清理，聚焦内存变化）：
   ```bash
   circt-opt /tmp/cf-final.mlir -flatten-memref -handshake-legalize-memrefs \
     > /tmp/hs-stage1.mlir
   ```
4. 对比内存表示的变迁：
   ```bash
   for f in /tmp/cf-final.mlir /tmp/hs-stage1.mlir; do
     echo "== $f =="
     grep -oE 'memref<[0-9]+x[0-9]+x[^>]*>|memref<[0-9]+x[^>]*>|handshake\.[a-z]+|memref\.(load|store)' "$f" | sort | uniq -c
   done
   ```

**需要观察的现象**：

- `/tmp/cf-final.mlir`（输入）：以多维 `memref<MxNxi...>` 和 `memref.load`/`memref.store` 为主（matmul 是 1-D 向量，可能本就是一维，那就看不到「多维→一维」这一步，但仍能看到 `memref.load/store`）。
- `/tmp/hs-stage1.mlir`（Stage 1 后）：出现 `handshake.load`/`handshake.store`（或同类节点）、`memref.load/store` 减少；memref 内存被外部存储抽象取代。

**预期结果**：证实「内存表示在这一段从『共享状态的直接读写』变成了『Handshake 外部存储 + 数据流节点』」。这正是 4.3 的 `lower-cf-to-handshake` 能放心只管翻译控制流的前提。

> matmul 是一维点积，多维压扁这一步可能「无事可做」（输入已经是一维），观察重点转为 `memref.load/store → handshake.load/store`。能否跑通取决于 devShell 是否把 `circt-opt` 放到 PATH，具体算子名**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`flatten-memref` 和 `flatten-memref-calls` 为什么是「一对」、且前者必须在前？

**参考答案**：`flatten-memref` 改的是**函数体内部**的 memref 类型与下标（多维压一维），这会让形参的 memref 类型变化。`flatten-memref-calls` 负责把**调用点**的实参/形参类型同步成压扁后的形态。前者先改函数体、确定新类型，后者才能据此修调用点；反过来「先改调用点、还不知道新类型长啥样」无从下手。所以必须成对、且体在先、调用点在后。

**练习 2**：为什么 `handshake-legalize-memrefs` 要在 `flatten-memref` 之后、而不是之前？

**参考答案**：`handshake-legalize-memrefs` 把 memref 内存映成 Handshake 外部存储，它的实现面对**一维、平坦**的内存最稳健——地址就是单个线性值，通道语义清晰。若先合法化（面对多维 + 复杂 stride），它得自己处理多维地址到端口地址的映射，更易错也更难维护。先用 `flatten-memref` 把维度压平，再让合法化在一维上工作，是「把复杂度集中在一个 pass（flatten）、让后续 pass 各司其职」的分层设计。

**练习 3**：Stage 1 之后，IR 的**控制流**变了没有？

**参考答案**：没变。Stage 1 的所有 pass（`flatten-memref`、`flatten-memref-calls`、`handshake-legalize-memrefs` 以及清理）都只动**内存与数据**，不碰基本块和 `cf.br`/`cf.cond_br`。控制流仍是 u3-l1 产出的 CF 形态，留给 Stage 3 翻译。这也解释了为什么 Stage 2 还要再做一次 SCF→CF——因为只有内存合法化可能重新引入 SCF，控制流主干始终是 CF。

---

### 4.2 SCF 归一为 CF：把合法化「抖出来」的结构化循环再压平

#### 4.2.1 概念说明

读到这里你可能会疑惑：u3-l1 的 `linalg_to_cf.sh` 不是已经 `--convert-scf-to-cf` 了吗？为什么本站 Stage 2 **又跑一次** `-convert-scf-to-cf`？

答案在 Stage 2 的注释里——[scripts/pipeline/cf_to_handshake.sh:34](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/cf_to_handshake.sh#L34) 写着 `Normalize SCF to CF after memref legalization`（在 memref 合法化**之后**把 SCF 归一成 CF）。关键词是 **after memref legalization**：Stage 1 的 `handshake-legalize-memrefs`（以及 `flatten-memref`）在改写内存时，**可能重新生成 SCF 结构**——比如它要把某段内存操作合法化，结果插入了一段 `scf.for` 或 `scf.if`。

而 Stage 3 的 `lower-cf-to-handshake`（4.3）只吃 **CF**（基本块 + 跳转），不认 `scf.for`/`scf.if` 的嵌套区域（原因和 u3-l1 一样：Handshake 降级要扁平的基本块，能一一映成数据流节点）。所以合法化之后冒出来的任何 SCF，都必须**再压一次**回 CF。

这就是 Stage 2 的全部职责：一次「查漏补缺」的 SCF→CF，专门清理合法化引入的结构化控制流。

#### 4.2.2 核心流程

```
Stage 1 产物（CF 主干 + 合法化可能抖出的 SCF）   Stage 2（mlir-opt）   交给 Stage 3
┌──────────────────────────────────────┐                            ┌─────────────────────┐
│ cf.br / cf.cond_br （主干，未变）     │  -convert-scf-to-cf        │ 纯 CF               │
│ scf.for / scf.if （合法化抖出的残部）  │  ──────────────────────►   │ （scf.* 基本归零）    │
│ handshake.load/store 节点             │  （+ -canonicalize/-cse）  │ + handshake 节点    │
└──────────────────────────────────────┘                            └─────────────────────┘
```

这一段不改内存、不改数据流节点，**只把残存的 SCF 结构拍平成 CF**，确保 Stage 3 拿到的是「干干净净的 CF」。

#### 4.2.3 源码精读

Stage 2 在 [scripts/pipeline/cf_to_handshake.sh:35-38](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/cf_to_handshake.sh#L35-L38) —— 用**上游 `mlir-opt`**（注意：不是 `circt-opt`）把 SCF 压回 CF：

```bash
run_to_output "$tmp_norm" "$mlir_opt" "$tmp_legal" \
  -convert-scf-to-cf \
  -canonicalize \
  -cse
```

逐行说明：

- 第 35 行：工具换成了 `$mlir_opt`（`mlir-opt`）。`-convert-scf-to-cf` 是**上游标准 pass**，按 u3-l1 的「用对工具」原则，这里用上游 `mlir-opt` 而非 `circt-opt`。（`circt-opt` 通常也包含上游 pass，但脚本特意在这里切到 `mlir-opt`，遵循「纯上游 pass 用纯上游工具」的洁癖。）这也是本站签名需要**两个工具参数**的直接原因——Stage 1、3 用 `circt-opt`，Stage 2 用 `mlir-opt`。
- 第 36 行 `-convert-scf-to-cf`：把 `scf.for`/`scf.if` 拆成 `cf.br`/`cf.cond_br` + 基本块。机制与 u3-l1 完全相同（u3-l1 的 4.3 已详述），只是这里用来清理「合法化抖出来的 SCF」。
- 第 37–38 行 `-canonicalize` / `-cse`：拆完清理。

> 关键认知：`convert-scf-to-cf` 在整条降级链里可能出现多次（u3-l1 一次、本站一次），因为「某些 pass 会重新引入 SCF」。降级不是一条直线，而是「降一段、可能引入高层结构、再降、再清理」的反复过程。

#### 4.2.4 代码实践

**实践目标**：验证 Stage 1 之后确实可能残留 SCF，而 Stage 2 把它清掉（若合法化没抖出 SCF，则 Stage 2 是近乎 no-op——这本身也是一个可观察的结论）。

**操作步骤**：

1. 沿用 4.1.4 的 `/tmp/hs-stage1.mlir`（Stage 1 后、Stage 2 前）。
2. 跑 Stage 2 单独的 pass：
   ```bash
   mlir-opt /tmp/hs-stage1.mlir -convert-scf-to-cf -canonicalize -cse > /tmp/hs-stage2.mlir
   ```
3. 看 SCF/CF 计数变化：
   ```bash
   for f in /tmp/hs-stage1.mlir /tmp/hs-stage2.mlir; do
     echo "== $f =="
     grep -oE 'scf\.[a-z]+|cf\.[a-z]+' "$f" | sort | uniq -c
   done
   ```

**需要观察的现象**：

- `/tmp/hs-stage1.mlir`：若合法化抖出了 SCF，这里会有少量 `scf.*`；若没有，则 `scf.*` 计数为 0。
- `/tmp/hs-stage2.mlir`：`scf.*` 计数为 0（或保持 0），`cf.br`/`cf.cond_br` 是控制流主力。

**预期结果**：Stage 2 之后**不含任何 SCF**，是纯 CF——这正是 Stage 3 `lower-cf-to-handshake` 要求的输入。即便合法化没引入 SCF（Stage 2 近乎 no-op），这一段也是**防御性**的：保证无论上游怎么变，交给 Stage 3 的都一定是纯 CF。

> matmul 体量极小，合法化可能不引入 SCF，Stage 2 看似「没干活」；换成 TinyStories-1M 这种大模型，SCF 残部会明显得多。具体**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `convert-scf-to-cf` 在 u3-l1 跑过一次，本站还要再跑一次？

**参考答案**：因为降级链上有些 pass（本站的 `handshake-legalize-memrefs` 等）在改写 IR 时**会重新生成 SCF 结构**（如 `scf.for`/`scf.if`）。下游的 `lower-cf-to-handshake` 只认 CF，不认 SCF。所以每次「可能引入 SCF」的 pass 之后，都得用 `convert-scf-to-cf` 把 SCF 压回 CF。这不是重复，而是「降级会反复引入高层结构，需反复拍平」的现实。

**练习 2**：Stage 2 用 `mlir-opt` 而不是 `circt-opt`，说明了什么？

**参考答案**：`-convert-scf-to-cf` 是 MLIR **上游标准** pass，不属于 CIRCT。按本项目「纯上游 pass 用上游工具」的约定（u3-l1 已建立），这里用 `mlir-opt` 最贴切。这也让本站成为降级链上唯一同时调用两个工具的站：Stage 1/3 用 `circt-opt`（CIRCT 专属 pass），Stage 2 用 `mlir-opt`（上游 pass）。

**练习 3**：如果删掉 Stage 2（直接拿 Stage 1 产物喂给 Stage 3），可能出什么问题？

**参考答案**：若 `handshake-legalize-memrefs` 在大模型上抖出了 SCF（如 `scf.for`），而 Stage 2 被跳过，那 Stage 3 的 `lower-cf-to-handshake` 会遇到它不认识的 `scf.*` 算子——要么报错终止，要么留下未降级的 SCF（后续综合失败）。Stage 2 是一道**安全网**，保证 Stage 3 的输入恒为纯 CF，与上游 pass 的演进解耦。

---

### 4.3 lower-cf-to-handshake 与插缓冲：控制流真正翻成数据流

#### 4.3.1 概念说明

经过 Stage 1、2，IR 是「纯 CF + Handshake 外部存储节点」，万事俱备。Stage 3 完成最后的范式跃迁——**把控制流图翻成弹性数据流图**，靠两个 pass：

1. **`--lower-cf-to-handshake`（核心）**：这是整条降级链的「高光时刻」。它吃进一个由基本块 + `cf.br`/`cf.cond_br` 组成的 `func.func`，吐出一个 `handshake.func`——一张节点 + 通道的数据流网络。具体地：
   - 函数的**每个参数**变成一条**输入通道**（带 valid/ready），对应一个输入端口。
   - 函数的**每个返回/出参结果**变成一条**输出通道**，对应一个输出端口。
   - 基本块里的每个运算变成一个**数据流节点**，节点之间用通道连起来。
   - 控制流的 `cf.cond_br`（条件跳转）变成数据流里的**条件分发/选择节点**（如 `handshake.cond_br`、mux/`handshake.select`），决定数据往哪条支路走——注意，这是「数据」决定路由，不再是「程序计数器」决定跳转。
   - 基本块的「入口同步」（一个块要等它所有前驱都到位才执行）变成数据流里的**控制合并/Join 节点**。

   一句话：**「先做 A 再做 B」的时序，被替换成「A 的输出通道连到 B 的输入通道，B 等数据到位自己启动」的连接关系**。

2. **`-handshake-insert-buffers`（必要的善后）**：按 2.5，刚翻出来的数据流图里往往有**组合环**——某些数据依赖首尾相接，若不插存储，会死锁或物理不可实现。这个 pass 在通道上插入 `handshake.buffer` 节点（寄存器/小 FIFO），打破组合环、保证电路可综合、并提供吞吐。**它是有代价的**：每个缓冲消耗 FF（存数据槽）、LUT（控制），大的吃 BRAM。缓冲插得越多、越大，资源开销越重。

   \[
   \text{吞吐} \uparrow \;\longleftrightarrow\; \text{缓冲容量} \uparrow \;\longleftrightarrow\; \text{FF/LUT/BRAM 开销} \uparrow
   \]

这两个 pass 之后，IR 已经是完整的 Handshake 弹性数据流图——这正是 3e 报告里那个「最大负担之一」的来源，也是 Task 6 想动手的对象。

#### 4.3.2 核心流程

```
Stage 2 产物（纯 CF + handshake 内存节点）      Stage 3（circt-opt）          本站最终产物
┌──────────────────────────────┐                                         ┌──────────────────────────┐
│ func.func @main              │  --lower-cf-to-handshake               │ handshake.func @main     │
│   ^bb: cf.br / cf.cond_br    │  ──────────────────────────────────►   │   输入/输出通道（端口）   │
│   handshake.load/store 节点  │                                         │   运算节点 + 条件分发节点│
│   memref 已外部存储化         │  -handshake-insert-buffers             │   handshake.buffer 缓冲  │
│                              │  ──────────────────────────────────►   │   （弹性数据流图，可综合）│
└──────────────────────────────┘   （+ -canonicalize/-cse）             └──────────────────────────┘
```

`lower-cf-to-handshake` 与 `handshake-insert-buffers` 的先后**不可颠倒**：必须先把控制流翻成数据流图（图成型），才能在图的通道上插缓冲（缓冲要加在「通道」这种结构上，没成图无从下手）。这与 4.1「先 flatten 再 legalize」、4.2「先合法化再清 SCF」一样，都是「先成型、再修补」的固定套路。

#### 4.3.3 源码精读

Stage 3 在 [scripts/pipeline/cf_to_handshake.sh:41-45](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/cf_to_handshake.sh#L41-L45) —— 用 `circt-opt` 把 CF 翻成 Handshake 数据流并插缓冲：

```bash
run_to_output "$output" "$circt_opt" "$tmp_norm" \
  --lower-cf-to-handshake \
  -handshake-insert-buffers \
  -canonicalize \
  -cse
```

逐行说明：

- 第 41 行：工具切回 `$circt_opt`，因为这两个 pass 都是 CIRCT 专属。输入是 Stage 2 的 `$tmp_norm`（纯 CF），输出写进最终的 `$output`。
- 第 42 行 `--lower-cf-to-handshake`：本站、乃至整条降级链的核心之一。把 CFG 翻成数据流图。改写前后对比（示例代码，非项目原始 IR）：

  ```mlir
  // —— 示例代码（Stage 3 前：控制流模型）——
  func.func @main(%a : memref<...>, %out : memref<...>) {
    cf.br ^entry
  ^entry:
    %v = handshake.load %a[...] : ...
    cf.cond_br %cond, ^then, ^else
  ^then:  %x = arith.addi %v, %v; cf.br ^join(%x)
  ^else:  %y = arith.muli %v, %v; cf.br ^join(%y)
  ^join(%r): handshake.store %r, %out[...]; return
  }

  // —— 示例代码（Stage 3 后：弹性数据流模型）——
  handshake.func @main(%a : !handshake.channel<...>, %out : !handshake.channel<...>) {
    // 没有基本块；条件跳转变成数据驱动的分发/选择节点
    %v = handshake.load %a[...] ...                      // load 节点
    %r = handshake.cond_br %cond {                       // 数据决定走 then 还是 else
      ^then:  %x = arith.addi %v, %v ...; handshake.return %x
      ^else:  %y = arith.muli %v, %v ...; handshake.return %y
    }
    handshake.store %r, %out[...] ...                    // store 节点
  }
  ```

  关键变化：`cf.cond_br`（「程序计数器跳到 ^then 或 ^else」）变成了 `handshake.cond_br`（「数据 %cond 决定把 %x 还是 %y 往下游送」）——从「跳到哪」变成「选哪个」。

- 第 43 行 `-handshake-insert-buffers`：在通道上插缓冲，打破组合环、提供吞吐。缓冲的形态（寄存器 vs FIFO、深度多少）由 pass 内部策略决定，但**每一个都是真金白银的硬件资源**。
- 第 44–45 行 `-canonicalize` / `-cse`：成型 + 插缓冲后清理。

**为什么 Stage 3 的两个 pass 必须用 `circt-opt`**：`lower-cf-to-handshake` 和 `handshake-insert-buffers` 都是 Handshake 方言的 pass，Handshake 方言属于 CIRCT，上游 `mlir-opt` 根本不认识。这与 Stage 2 用 `mlir-opt`（上游 pass）形成工整对照。

> 具体的 `handshake.func` 签名、通道类型（`!handshake.channel<...>`）、节点算子名取决于本项目 pin 的 CIRCT fork 版本，**待本地验证**。

#### 4.3.4 代码实践

**实践目标**：看到 `func.func` 在 `--lower-cf-to-handshake` 后变成 `handshake.func`，且 `-handshake-insert-buffers` 真的插入了一批 `handshake.buffer` 节点（可量化计数）。

**操作步骤**：

1. 沿用 4.2.4 的 `/tmp/hs-stage2.mlir`（Stage 2 后、Stage 3 前，纯 CF）。
2. 先只跑 lower，不插缓冲，看数据流图成型：
   ```bash
   circt-opt /tmp/hs-stage2.mlir --lower-cf-to-handshake > /tmp/hs-nobuf.mlir
   grep -oE 'func\.func|handshake\.func|handshake\.[a-z]+' /tmp/hs-nobuf.mlir | sort | uniq -c
   ```
3. 再加插缓冲，对比缓冲数量：
   ```bash
   circt-opt /tmp/hs-nobuf.mlir -handshake-insert-buffers -canonicalize -cse > /tmp/hs-final.mlir
   grep -oE 'handshake\.[a-z]+' /tmp/hs-final.mlir | sort | uniq -c
   echo "== 缓冲节点数 =="
   grep -c 'handshake.buffer' /tmp/hs-final.mlir
   ```

**需要观察的现象**：

- `/tmp/hs-nobuf.mlir`：函数从 `func.func` 变成 `handshake.func`；出现 `handshake.` 系列节点（load/store/cond_br/control 合并等）。此时尚无 `handshake.buffer`。
- `/tmp/hs-final.mlir`：多出若干 `handshake.buffer` 节点——这就是「插缓冲」的物证。缓冲数量随模型规模增长，TinyStories-1M 会非常多。

**预期结果**：证实「控制流→数据流」的范式跃迁确实发生，且插缓冲是独立的一步、可见可数。把缓冲数与 u1-l4 的 LUT 超配结论联想：**缓冲（及其背后的 Handshake 控制逻辑）是资源大头之一**。

> 能否跑通取决于 devShell 是否提供 `circt-opt` 及其是否包含本项目补丁后的行为（见 u6-l4 的 CIRCT 补丁栈）。具体节点名与缓冲数**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`lower-cf-to-handshake` 之后为什么非要插缓冲不可？不插会怎样？

**参考答案**：刚翻出来的数据流图里常含**组合环**——某些数据依赖首尾相接（如条件支路汇合、迭代变量回授）。若通道上没有存储，这些环是纯组合反馈线，物理上不可综合；语义上，环上的节点会互相等待对方的 `valid`，导致**死锁**或零吞吐。`handshake-insert-buffers` 在通道里塞寄存器/FIFO，把瞬间依赖断成跨周期依赖，环才被打破、电路才可跑。所以不插缓冲，生成的 RTL 要么无法综合、要么跑不起来。

**练习 2**：插缓冲「越多越深」对吞吐和资源分别是什么影响？为什么这成了 3e 报告里点名的「负担」？

**参考答案**：缓冲越深，流水线各级越能独立推进、容忍下游停顿，**吞吐越高**；但代价是每个缓冲槽占 FF、控制占 LUT，大缓冲吃 BRAM，**资源开销越大**。两者是正相关权衡。TinyStories-1M 是个含大量算子和回路的大模型，Handshake 把它翻成数据流图后会插**非常多的缓冲**（加上每个节点自带的 valid/ready 控制逻辑），于是 LUT/FF 严重膨胀——这正是 3e 报告说「Handshake 方言是流水线最大负担之一、Task 6 想替换它」的直接技术原因。

**练习 3**：`lower-cf-to-handshake` 与 `handshake-insert-buffers` 的先后为什么不能颠倒？

**参考答案**：`handshake-insert-buffers` 的操作对象是**数据流图的通道**——它要在已成型图里的 channel 上插缓冲节点。必须先用 `lower-cf-to-handshake` 把控制流翻成数据流图（通道结构成型），插缓冲才有「通道」可插。若反过来，控制流还没成图、根本没有 `handshake.channel`，插缓冲无从下手。这是「先成型、再修补」的固定顺序。

---

### 4.4 本站在 Nix 里的编排（及「两个工具」如何体现）

本讲虽以 `cf_to_handshake.sh` 为主，但它在真实流水线里由 Nix 调起，且这一站的 Nix 编排有个**全链唯一**的细节值得专门看。

[nix/pipeline.nix:42-48](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L42-L48) —— `mkHandshakeDerivation`：用 `runCommand` 把本站脚本包成派生，产出 `${name}-handshake.mlir`：

```nix
mkHandshakeDerivation = { name, cf }:
  pkgs.runCommand "${name}-handshake.mlir" {
    buildInputs = [ mlir circt ];
  } ''
    ${pkgs.bash}/bin/bash ${pipelineScripts}/cf_to_handshake.sh \
      ${circt}/bin/circt-opt ${mlir}/bin/mlir-opt ${cf} "$out"
  '';
```

要点：

- **`buildInputs = [ mlir circt ]`**：本站同时需要两个工具，所以 Nix 这里把 `mlir` 和 `circt` 都列进 buildInputs——这与上一站 `mkCfDerivation` 只列 `[ mlir ]`、下一站 `mkHsExtDerivation` 只列 `[ circt ]` 形成对照。整条降级链里，**只有这一站的派生同时依赖 `mlir` 和 `circt`**。
- **调用形式是 4 参数**（脚本第 9–12 行定义）：`${circt}/bin/circt-opt`、`${mlir}/bin/mlir-opt`、`${cf}`（u3-l1 的 CF 输入）、`$out`（Nix 填的输出路径）。注意前两个参数的顺序与脚本签名一致：先 circt-opt、再 mlir-opt。
- 输入 `${cf}` 来自上一站派生，形成依赖。

[nix/pipeline.nix:110-113](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L110-L113) —— `mkPipeline` 里 `handshake` 段：把本站接在 `cf` 之后：

```nix
handshake = mkHandshakeDerivation {
  inherit name;
  inherit (self) cf;
};
```

`inherit (self) cf` 是 u3-l5 会详讲的「惰性自引用」技巧：`handshake` 派生的输入 `cf` 指向同一 `self` 里的 `cf` 派生（即 L106–L109 那个）。Nix 自动把「`handshake` 依赖 `cf`」建链。

**缓存边界**：`cf` 输入（及其上游 linalg/torch）不变时，本站三段式的产物直接复用缓存，不会重跑——哪怕你反复改后面的 hw/sv 阶段。反过来，改了 u3-l1（如动 `linalg_to_cf.sh`）会让 `cf` 重算，本站 `handshake` 跟着重算。这条规则在 u3-l5 系统讲解。

---

## 5. 综合实践

把本讲三个模块串起来，完成核心实践任务：**解释「为什么必须先 `flatten-memref` 再 `lower-cf-to-handshake`」，并结合 3e 报告说明 Handshake 的资源开销为什么是 Task 6 想替换的对象。**

**任务步骤**：

1. **跑通本站三段**：按 4.1.4–4.3.4 准备 `/tmp/cf-final.mlir`（u3-l1 产物），再分段跑出 Stage 1/2/3 的中间文件（`/tmp/hs-stage1.mlir`、`/tmp/hs-stage2.mlir`、`/tmp/hs-final.mlir`）。或直接用完整脚本一步到位：
   ```bash
   bash scripts/pipeline/cf_to_handshake.sh \
     "$(command -v circt-opt)" "$(command -v mlir-opt)" \
     /tmp/cf-final.mlir /tmp/hs-final.mlir
   ```
2. **论证「flatten 在 lower 之前」的必要性**（核心实践任务前半）。对照 [cf_to_handshake.sh:25-45](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/cf_to_handshake.sh#L25-L45)，用一段话（不超过 150 字）回答：**若跳过 Stage 1（不 flatten、不 legalize-memrefs），直接把 u3-l1 的 CF+memref 喂给 `lower-cf-to-handshake`，会怎样？** 参考答案要点：
   - `lower-cf-to-handshake` 只翻译**控制流**，不认识 `memref.load/store` 这种「共享状态」式内存访问；
   - 多维 memref + 复杂布局若不先压扁，Handshake 的外部存储模型无法干净映射；
   - 结果是 pass 报错或留下未降级的内存算子，综合阶段失败。
   - 因此必须先 flatten（压扁）→ legalize-memrefs（内存变外部存储）→（清 SCF）→ 才能 lower-cf-to-handshake（翻控制流）。
3. **量化 Handshake 的资源代价**（核心实践任务后半）。数一下 `/tmp/hs-final.mlir` 里的 `handshake.buffer` 与 `handshake.*` 节点总数：
   ```bash
   echo "== handshake 节点总览 =="
   grep -oE 'handshake\.[a-z]+' /tmp/hs-final.mlir | sort | uniq -c
   echo "== 缓冲节点数 =="
   grep -c 'handshake.buffer' /tmp/hs-final.mlir
   ```
   然后打开 [deliverables/3e-tiny-stories-1m-resource-report.md](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md)，找到这两处表述：
   - L34–L36：`all-memory` 目标 blackbox 了「oversized Handshake memory modules」，并明说「The use of Handshake dialect is one of the biggest burdens in the current pipeline and removing it is a goal of task 6.」
   - L79–L86：Task 6 的候选方向里，第二条就是「Use a MLIR dialect other than handshake. The handshake dialect uses a lot of resources in this pipeline.」
4. **把现象和报告串起来**：用你数到的缓冲/节点数，解释 Handshake 为什么「uses a lot of resources」——每个节点自带 valid/ready 控制逻辑（LUT/FF）、每条关键通道还插了缓冲（FF/LUT/BRAM）、超大存储还要 blackbox 外部化（见 u6-l2）。TinyStories-1M 规模下这些累加，直接贡献了 u1-l4 的「超配约 141 倍」。因此 Task 6 把「换掉 Handshake 方言」列为高优先级。

**预期交付**：① 一段「flatten 必须在 lower 之前」的论证；② `/tmp/hs-final.mlir` 的 handshake 节点统计；③ 引用 3e 报告原文，说明 Handshake 为何是 Task 6 的替换目标。

> ⚠️ 第 2 步的「跳过 Stage 1」**不要真去改项目脚本**——只在脑子里/草稿上推演。若想实证，可在 `/tmp/` 下复制副本、删掉 Stage 1 后重跑，观察 `lower-cf-to-handshake` 是否报错。能否跑通取决于 `circt-opt`/`mlir-opt` 是否在 PATH 及 CIRCT 补丁版本，具体**待本地验证**。若环境跑不动，改为纯源码阅读型实践：只完成第 2、3 步的「论证 + 节点对照」并标注「待本地验证」。

---

## 6. 本讲小结

- 本站 `cf_to_handshake.sh` 是降级链第四站，完成一次**范式跃迁**：把 u3-l1 的「CF + memref」从「时序控制流」翻成 Handshake **弹性数据流**图，交给下一站 `handshake_to_hs_ext.sh`（u3-l3）。
- 脚本是**三段式**，每段一个工具、一个临时文件：① `circt-opt` 做 `-flatten-memref`/`-flatten-memref-calls`/`-handshake-legalize-memrefs`（多维压扁 + 内存变外部存储）；② `mlir-opt` 做 `-convert-scf-to-cf`（清理合法化抖出的 SCF）；③ `circt-opt` 做 `--lower-cf-to-handshake`/`-handshake-insert-buffers`（控制流翻数据流 + 插缓冲）。
- **必须先 flatten（+legalize-memrefs）再 lower-cf-to-handshake**：后者只翻译控制流、不认识 `memref` 共享状态式内存；前者把内存先压扁、再变成 Handshake 外部存储节点，lower 才能放手只管控制流。这是核心实践任务的答案。
- **`convert-scf-to-cf` 在本站又跑一次**，因为 `handshake-legalize-memrefs` 会重新引入 SCF；这一段是保证 Stage 3 拿到纯 CF 的安全网。
- **`handshake-insert-buffers` 有代价**：缓冲打破组合环、提供吞吐，但每个缓冲都吃 FF/LUT/BRAM。这正是 3e 报告把 Handshake 列为「最大负担之一」、Task 6 想换掉它的技术原因（结合 u1-l4 的 141 倍超配）。
- 本站是降级链上**唯一同时调用两个工具**的站（`circt-opt` + `mlir-opt`），Nix 里 `mkHandshakeDerivation` 的 `buildInputs = [ mlir circt ]`、4 参数调用正是它的体现；`cf` 输入不变时本站复用缓存。

---

## 7. 下一步学习建议

- **紧接的下一讲 u3-l3**：`handshake_to_hs_ext.sh`——本站产出的 Handshake 数据流图，在那里先被 `-handshake-lower-extmem-to-hw`（把外部存储暴露成硬件端口）和 `-handshake-materialize-forks-sinks`（补齐 fork/sink 节点）处理，再一路降到 HW 方言。你会看到本站建立的「外部存储」抽象如何落地成真实硬件端口，正好印证本讲 2.3、4.1 的铺垫。
- **回扣 u4-l2（仿真）**：u4-l2 里「直接给内部 handshake memory 播种」的层次化引用（`dut.handshake_memory0._handshake_memory_5[i]`），其前提正是本站 `handshake-legalize-memrefs` 把内存做成的可外部化结构。学完仿真再回看本站，会更理解这个设计。
- **前瞻 u6-l2 / u6-l4**：u6-l2 的「外部化超大 Handshake 存储」（128 kbit 阈值 blackbox）、u6-l4 的 CIRCT 补丁栈（含 `0003-flatten-memref-shape-ops.patch`），都直接建立在本站引入的 Handshake 内存/缓冲模型上——本站是理解那些「资源优化」与「打补丁」故事的起点。
- **横向对照**：读一遍 `scripts/pipeline/handshake_to_hs_ext.sh`，对比它与本站的异同——它只用 `circt-opt`（单工具）、一次 `run_to_output`（无临时文件），体会「本站为何特殊」。
- **回顾**：若对「Handshake 为何吃资源」还半信半疑，回头读 u1-l4 的 summary.txt 结论（超配 141 倍）与 3e 报告 L34–L36，再结合本站 4.3 的缓冲代价，三处互证。
