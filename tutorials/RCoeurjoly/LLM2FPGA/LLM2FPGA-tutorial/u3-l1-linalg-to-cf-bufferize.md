# Linalg 到 CF：bufferize 与循环展开

> 本讲承接 u2-l3。u2-l3 的产物是「**Linalg-on-Tensors** MLIR」——所有张量都是 `tensor<...>` 值类型，连碰内存的资格都还没有，缓冲化被「故意推迟」。本讲要回答的问题是：**tensor 第一次变成 buffer（memref）这件事，到底在哪一站、由哪些 pass 完成？** 答案全在 28 行的 `linalg_to_cf.sh` 里。它把 u2-l3 的 Linalg-on-Tensors 一次性缓冲化、出参化、再展开成循环与控制流，产出下一站 `cf_to_handshake.sh` 期望的「CF + memref 形式」。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 `--one-shot-bufferize="bufferize-function-boundaries"` 这个名字很长、但作用最关键的 pass 到底做了什么，以及 `--empty-tensor-to-alloc-tensor` 为何要在它之前跑。
2. 解释 `--buffer-results-to-out-params` 为什么对**硬件接口**至关重要——它如何把「返回一个 buffer」改成「写进一个调用方提供的 buffer」。
3. 把 `--convert-linalg-to-loops` 与 `--convert-scf-to-cf` 这两步「展开」讲明白：张量算子如何先变成嵌套循环，再变成 goto 式的基本块分支。
4. 把 `linalg_to_cf.sh` 的 12 个 pass 分成「缓冲化」「出参化收尾」「循环展开」三大类，并指出哪些是清理性的 `-canonicalize`/`-cse`。
5. 看懂 `nix/pipeline.nix` 里 `mkCfDerivation` 如何用**上游通用 `mlir-opt`**（而非 torch-mlir-opt / circt-opt）把本站包成可缓存派生。

---

## 2. 前置知识

本讲默认你读过 u2-l3（torch 方言到 Linalg）以及它引入的「降级 / 方言 / tensor vs buffer / common.sh 脚手架」概念。下面只补充本讲新出现的几个 MLIR 概念，全部用通俗语言。

### 2.1 回顾：为什么要「晚缓冲化」

u2-l3 已建立这个直觉，这里一句话复习：**tensor 是值语义（数学值，没有内存），memref/buffer 是内存语义（一块带布局的可写内存）**。从 tensor 变成 memref 是一次**不可逆的硬承诺**——它锁定了「这块数据存在哪、谁分配、谁释放、能不能和别的数据共用一块内存」。MLIR 社区的最佳实践是「**one-shot bufferize 尽量晚**」：先在值语义下把算法优化到位，再一次性切到内存。u2-l3 把这件事推迟到「下一站」，本讲就是那个「下一站」。

### 2.2 什么是 bufferize，为什么要「one-shot」

朴素地把每个 `tensor` 换成一块新分配的内存，会得到天文数字的内存分配——每个中间结果都开一块 buffer，既慢又占资源。**One-shot bufferize** 的核心思想是**全局分析**：一次性遍历整段 IR，推理出哪些 tensor 可以「就地」(in-place) 复用同一块 buffer、哪些必须复制、哪些需要新分配，从而得到一个接近最优的 buffer 分配方案。

它要保证的数学不变量是**缓冲等价性（buffer correctness）**：缓冲化之后，从 memref 里读出来的值，必须严格等于缓冲化之前对应 tensor 里的值。也就是说：

\[
\forall\ \text{op},\quad \text{value\_read}(\text{bufferized IR}) = \text{value\_in}(\text{tensor IR})
\]

`bufferize-function-boundaries` 是它的一个**选项**：让这次缓冲化越过函数边界，把函数签名里的 `tensor<...>` 参数和返回值也一并换成 `memref<...>`。没有这个选项，函数内部缓冲化了、函数签名却仍是 tensor，接口就对不上。

### 2.3 什么是「出参（out parameter）」

如果你写过 C，一定见过这个套路：函数想「返回」一个大数组，但不直接 `return`，而是让调用方先开好数组，把数组指针作为参数传进来，函数往里写。这个「传进来用来写结果的参数」就叫**出参（out parameter）**。

```c
// 「返回数组」的两种写法
int* compute(int n);                    // 直接 return 一个 buffer
void compute(int n, int* out);          // 出参：调用方提供 buffer，函数写进去
```

硬件世界里**只有第二种**。一个硬件模块不可能「return 一个值给调用方」——它只有输入端口和输出端口，数据从端口流进流出。所以「把 buffer 返回值改成出参」这一步，是把软件式函数朝硬件式端口模型靠拢的关键塑形。本讲会看到它由 `--buffer-results-to-out-params` 完成。

### 2.4 什么是 dealloc，为什么要 lower 它

在缓冲化的 IR 里，内存的分配/释放由 `bufferization.alloc_tensor`、`bufferization.dealloc` 这类**缓冲化方言**算子表达。`dealloc` 就是「释放这块 buffer」。这些算子是高层的「内存生命周期」抽象，下游（尤其是硬件降级）认不得，必须把它们**lower（降级）**成更底层的 memref 操作（比较、条件分支、实际的释放调用）。本讲里 `--bufferization-lower-deallocations` 和 `--convert-bufferization-to-memref` 就是干这个收尾的。

### 2.5 SCF vs CF：结构化控制流 vs goto 式控制流

MLIR 有两种控制流方言：

- **SCF（Structured Control Flow）**：结构化的 `scf.for`（带 `scf.yield` 的循环）、`scf.if`（带 then/else 区域的条件）。像高级语言里的 `for`/`if`，结构规整、有明确的入口出口。
- **CF（Control Flow）**：基本块 + 分支。`cf.br`（无条件跳转）、`cf.cond_br`（条件跳转），用基本块和跳转连成控制流，本质是带类型的 `goto`。

```text
scf.for %i = 0 to 10 {          cf.br ^bb1
  ...                              ^bb1:
  scf.yield                        %c = ...
}                                  cf.cond_br %cond, ^bb2, ^bb3
                                   ^bb2: ...; cf.br ^bb4
                                   ^bb3: ...; cf.br ^bb4
                                   ^bb4: ...
```

`--convert-scf-to-cf` 就是把左边的结构化形式拆成右边的基本块 + 跳转。为什么必须拆？因为下游的 Handshake 数据流降级、以及更底层的表示，都期望 goto 式的扁平控制流，认不得结构化区域。

### 2.6 `mlir-opt` 是什么（再强调一次与兄弟工具的区别）

`mlir-opt` 是 MLIR **上游通用**的「跑一遍 pass」命令行工具。本站用的全是上游标准 pass，所以用 `mlir-opt` 即可——**不需要** torch-mlir-opt（u2-l3 用过，认得 torch pipeline），也**不需要** circt-opt（下一站 u3-l2 才用，认得 Handshake/ESI pass）。这是降级链上少数几个完全用「纯上游工具」的站，理解这一点能帮你判断每个站该调哪个工具。

---

## 3. 本讲源码地图

本讲只涉及两个主文件，`common.sh` 在 u2-l3 已详解，这里直接复用：

| 文件 | 行数 | 职责 |
|---|---|---|
| [scripts/pipeline/linalg_to_cf.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/linalg_to_cf.sh) | 28 | 本站主体：校验输入后，调用 `mlir-opt` 跑 12 个 pass，把 Linalg-on-Tensors 一次性缓冲化、出参化、展开成 CF+memref。 |
| [nix/pipeline.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix) | 156 | 把本站包成可缓存的 Nix 派生；本讲聚焦 `mkCfDerivation`（L36–L40）与 `mkPipeline` 里 `cf` 段（L106–L109）。 |
| [scripts/pipeline/common.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/common.sh) | 24 | u2-l3 已讲透的共享脚手架（`require_file`/`require_executable`/`run_to_output`）。 |

一句话定位：**`linalg_to_cf.sh` 是降级链的第三站**。它读入 u2-l3 产出的 Linalg-on-Tensors `.mlir`，吐出「CF + memref」形式的 `.mlir`，交给下一站 `cf_to_handshake.sh`（u3-l2）。下一站的开头注释 `Lower to CF+memref form expected by handshake legalization`（cf_to_handshake.sh:24）正好印证：**本站的产物名就是「CF+memref form」**。

---

## 4. 核心概念与源码讲解

先给本站全貌。`linalg_to_cf.sh` 真正干活的，是这一段调用：

[scripts/pipeline/linalg_to_cf.sh:15-27](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/linalg_to_cf.sh#L15-L27) —— 本站核心：调用 `mlir-opt` 跑 12 个 pass，把输出写到 `$output`：

```bash
run_to_output "$output" "$mlir_opt" "$input" \
  --empty-tensor-to-alloc-tensor \
  --one-shot-bufferize="bufferize-function-boundaries" \
  --buffer-results-to-out-params \
  --bufferization-lower-deallocations \
  --convert-bufferization-to-memref \
  --memref-expand \
  -canonicalize \
  -cse \
  --convert-linalg-to-loops \
  --convert-scf-to-cf \
  -canonicalize \
  -cse
```

这 12 个 pass 可按下表分成三大类（本讲三个最小模块）+ 清理：

| 行 | pass | 归属模块 | 一句话作用 |
|---|---|---|---|
| 16 | `--empty-tensor-to-alloc-tensor` | ① 缓冲化 | 把空 tensor 标记为「需要分配」，为缓冲化铺路 |
| 17 | `--one-shot-bufferize="bufferize-function-boundaries"` | ① 缓冲化 | **主步骤**：一次性把 tensor 全换成 memref，含函数边界 |
| 18 | `--buffer-results-to-out-params` | ② 出参化 | 把返回的 buffer 改成出参 |
| 19 | `--bufferization-lower-deallocations` | ② 出参化/收尾 | 把 `dealloc` 降成底层 memref 操作 |
| 20 | `--convert-bufferization-to-memref` | ② 出参化/收尾 | 把残留的 bufferization 方言算子转成 memref 算子 |
| 21 | `--memref-expand` | ② 出参化/收尾 | 把高层 memref 算子（如 copy）展开成 load/store |
| 22 | `-canonicalize` | 清理 | 常量折叠等 |
| 23 | `-cse` | 清理 | 公共子表达式消除 |
| 24 | `--convert-linalg-to-loops` | ③ 循环展开 | 把 Linalg 算子展开成 scf 嵌套循环 |
| 25 | `--convert-scf-to-cf` | ③ 循环展开 | 把 scf 结构化循环拆成 cf 基本块 + 跳转 |
| 26 | `-canonicalize` | 清理 | 常量折叠等 |
| 27 | `-cse` | 清理 | 公共子表达式消除 |

下面三个小节分别精讲这三类。

---

### 4.1 one-shot bufferize：tensor 第一次变成 buffer

#### 4.1.1 概念说明

u2-l3 把整个 IR 停在了「全是 `tensor<...>` 值类型」的状态，刻意不碰内存。本站第一件事，就是把这件事一次性做完——这就是 **one-shot bufferize**。

「一次性」是关键词。MLIR 早期支持过「逐 pass、逐方言」的缓冲化，每降一级就零散地变一点 buffer。实践证明这种碎片化做法很难保证正确性、也堵死优化。社区最终转向 **one-shot（一次性）bufferize**：一个 pass 全局分析整段 IR，给出统一的 buffer 分配方案。本项目正是采用了这一现代做法。

one-shot bufferize 解决的核心问题是 **in-place（就地）复用**：它要判断「这个 tensor 的计算结果，能不能直接写进某个输入 tensor 所在的那块 buffer，而不必新开一块」。能就地就省一块内存——这对资源敏感的 FPGA 至关重要（u1-l1 提到整条链最终超配约 141 倍，每一块 buffer 都很贵）。它做的判断本质上是：两次对同一块 buffer 的写，在所有执行路径上是否被正确同步隔开（即是否构成「写-写冲突」或「读-写冲突」）。若不能就地，就插入一次复制（copy）。

`bufferize-function-boundaries` 选项让缓冲化**越过函数边界**：函数形参的 `tensor<...>` 变成 `memref<...>`，返回值也一并处理。这是下游硬件降级的必要前提——硬件没有「传值」概念，所有数据交换都通过内存/端口。

#### 4.1.2 核心流程

本模块两个 pass 严格分两步，先后不能颠倒：

```
u2-l3 产物（全 tensor）            本模块                         本模块产物（出现 memref）
┌──────────────────────┐   --empty-tensor-to-alloc-tensor    ┌──────────────────────┐
│ tensor<16xi32>       │  ────────────────────────────────►   │ 标记好分配点          │
│ linalg.matmul        │  --one-shot-bufferize=...           │ 的 tensor IR         │
│ ...全是值语义         │  ────────────────────────────────►   │ memref<16xi32>       │
└──────────────────────┘   （一次性、含函数边界）              │ 函数签名也是 memref   │
                                                                └──────────────────────┘
```

- **第 16 行 `--empty-tensor-to-alloc-tensor`（预处理）**：把 `tensor.empty`（「我需要一块空 tensor」的声明）改写成 `bufferization.alloc_tensor`（「我需要分配一块 buffer」的声明）。这是一次「埋点」：让 one-shot bufferize 知道哪里需要真正分配内存。放在最前，是因为 one-shot 分析时要看到这些显式的分配点。
- **第 17 行 `--one-shot-bufferize="bufferize-function-boundaries"`（主步骤）**：全局分析、决定就地/复制/分配、把所有 tensor 换成 memref，并连同函数签名一起缓冲化。

预处理必须先跑：没有它，one-shot 遇到某些空 tensor 会无从下手；one-shot 也必须在出参化（4.2）之前跑，因为出参化要处理的正是「函数返回 memref」这一由缓冲化刚刚产生出来的情形。

#### 4.1.3 源码精读

本模块就这两行：

[scripts/pipeline/linalg_to_cf.sh:16-17](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/linalg_to_cf.sh#L16-L17) —— 缓冲化两连击：先埋分配点，再一次性 tensor→memref（含函数边界）：

```bash
  --empty-tensor-to-alloc-tensor \
  --one-shot-bufferize="bufferize-function-boundaries" \
```

逐行说明：

- 第 16 行 `--empty-tensor-to-alloc-tensor`：属于 `bufferization` 方言的预处理 pass。把空 tensor 的创建显式化为「分配请求」，让后续 one-shot 能识别它。
- 第 17 行 `--one-shot-bufferize`：本站最关键的一行。等号后的 `bufferize-function-boundaries` 是选项名（注意 `--xxx="..."` 这种含等号、含特殊字符的参数，正是 u2-l3 练习 2 里强调「`"$@"` 必须带引号」的真实用例——靠 `common.sh` 的 `run_to_output` 正确传递）。

缓冲化前后，函数签名大致这样变化（示意，非项目原始 IR，标注为示例）：

```mlir
// 缓冲化前（u2-l3 产物，tensor 值语义）—— 示例代码
func.func @main(%arg0 : tensor<16xi32>) -> tensor<16xi32> {
  %r = linalg.matmul ins(%arg0 : tensor<16xi32>) outs(%init : tensor<16xi32>) : tensor<16xi32>
  return %r : tensor<16xi32>
}

// 缓冲化后（本模块产物，memref 内存语义）—— 示例代码
func.func @main(%arg0 : memref<16xi32>) -> memref<16xi32> {
  // linalg 算子现在作用在 memref 上；可能有就地复用 / 复制
  ...
  return %r : memref<16xi32>
}
```

注意：此刻函数**仍然返回 memref**（return 一个 buffer）。这个「返回 buffer」正是 4.2 要消除的东西——硬件不喜欢「return」。具体的 IR 形态取决于上游工具版本与模型，**待本地验证**。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `tensor<...>` 在本模块两步之后变成 `memref<...>`，并确认函数签名也被缓冲化。

**操作步骤**：

1. 进入 devShell（u1-l3）：
   ```bash
   nix develop
   ```
2. 造出 u2-l3 的产物作为输入（若 `/tmp/linalg.mlir` 还在，可复用）：
   ```bash
   python scripts/compile-pytorch.py --adapter src/matmul_adapter.py --out /tmp/torch.mlir
   TORCH_MLIR_OPT="$(command -v torch-mlir-opt || true)" \
     bash scripts/pipeline/torch_to_linalg.sh /tmp/torch.mlir /tmp/linalg.mlir
   ```
3. 只跑缓冲化两个 pass（其余 pass 先注释掉，隔离观察），看 IR 变化：
   ```bash
   mlir-opt /tmp/linalg.mlir \
     --empty-tensor-to-alloc-tensor \
     --one-shot-bufferize="bufferize-function-boundaries" \
     > /tmp/cf-bufonly.mlir
   ```
4. 对比 `tensor` 与 `memref` 的计数：
   ```bash
   echo "== 输入（应为 tensor） =="
   grep -oE 'tensor<[^>]*>|memref<[^>]*>' /tmp/linalg.mlir | sort | uniq -c
   echo "== 仅缓冲化后（应出现 memref） =="
   grep -oE 'tensor<[^>]*>|memref<[^>]*>' /tmp/cf-bufonly.mlir | sort | uniq -c
   ```

**需要观察的现象**：

- 输入 `/tmp/linalg.mlir` 里以 `tensor<...>` 为主，几乎无 `memref`（u2-l3 的结论）。
- `/tmp/cf-bufonly.mlir` 里 `memref<...>` 大量出现；函数签名 `func.func @main(...)` 的形参/返回类型从 `tensor` 变成了 `memref`。

**预期结果**：证实「tensor→memref 一次性切换发生在本站」。若 `/tmp/cf-bufonly.mlir` 里函数仍返回 `memref`（即 `-> memref<...>`），那就是 4.2 要处理的对象。

> 是否能原样跑通取决于 devShell 是否把 `mlir-opt` 放到 PATH。具体计数与算子名**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--empty-tensor-to-alloc-tensor` 必须在 `--one-shot-bufferize` 之前？

**参考答案**：前者把「空 tensor 声明」显式改写成「分配请求」（`bufferization.alloc_tensor`），相当于在 IR 里埋下「这里需要一块新内存」的标记点。one-shot bufferize 的全局分析要依据这些显式标记来决定分配方案；如果先跑 one-shot，它可能识别不出隐式的空 tensor，导致分配点丢失或分析不完整。所以预处理必须在前。

**练习 2**：去掉 `bufferize-function-boundaries` 选项（只留 `--one-shot-bufferize`），会发生什么？

**参考答案**：缓冲化只在函数**内部**生效，函数签名仍保持 `tensor<...>`。结果是函数体里用 memref、函数边界用 tensor，两边接口对不上——后续 `--buffer-results-to-out-params`（4.2）也无从下手，因为它要处理的「返回 memref」根本没产生。对硬件降级而言，必须连函数边界一起缓冲化，因为硬件不存在「传值」。

**练习 3**：one-shot bufferize 相比「每降一级零散缓冲化一点」有什么本质优势？

**参考答案**：one-shot 做**全局**分析，能在整段 IR 范围内最大化 in-place（就地）复用，减少内存分配与复制次数——这对资源敏感的 FPGA 尤其关键。碎片化缓冲化只能看到局部信息，会保守地多分配、多复制，既浪费资源又容易出错。one-shot 用一次全局分析换取更优的 buffer 分配方案，是 MLIR 缓冲化的现代标准做法。

---

### 4.2 出参化 buffer 结果：为硬件接口塑形

#### 4.2.1 概念说明

one-shot bufferize 之后，函数变成「吃 memref、吐 memref」——仍保留着 `return %r : memref<...>` 这种「返回一个 buffer」的软件式接口。本模块的任务是**把这个接口改造成硬件友好的形状**，靠四个 pass 收尾：

1. **`--buffer-results-to-out-params`（出参化，核心）**：把「返回 memref 的函数」改写成「多收一个 memref 出参、往里写结果」的函数。`func.func @main(%a) -> memref<16xi32>` 变成 `func.func @main(%a, %out : memref<16xi32>)`（无返回值，结果写进 `%out`）。

   为什么这对硬件关键？因为硬件模块**没有 return**，只有端口。Handshake 数据流模型（下一站 u3-l2）把每个函数看成一组带 valid/ready 握手的输入/输出通道：输入通道对应函数参数，输出通道对应函数结果。一个「写进出参」的函数天然对应「一组输入端口 + 一组输出端口」，能直接映成硬件；而一个「return 一个值」的函数需要额外的「返回通道」特例处理，与统一的端口模型格格不入。出参化就是把这个特例提前抹平。

2. **`--bufferization-lower-deallocations`**：把缓冲化引入的 `bufferization.dealloc`（释放 buffer）降级成底层 memref 操作（比较 buffer 句柄、条件分支、实际释放）。下游不认 `dealloc` 抽象，必须 lower 掉。

3. **`--convert-bufferization-to-memref`**：把残留的 `bufferization` 方言算子（如 `bufferization.to_memref`、`bufferization.clone`、`bufferization.alloc_tensor`）统一转成 `memref` 方言算子。这一步之后，`bufferization` 方言基本从 IR 中消失，全归一到 `memref`。

4. **`--memref-expand`**：把一些「太高层」的 memref 算子（如 `memref.copy`、`memref.subview` 的某些形态）展开成更基本的 `memref.load`/`memref.store`（甚至循环）。这一步保证 memref 算子降到下游能识别的粒度。

这四个 pass 是「缓冲化的收尾 + 为下游塑形」：出参化改接口，其余三个把缓冲化留下的高层算子全 lower 到底。

#### 4.2.2 核心流程

```
4.1 产物（返回 memref 的函数）         本模块（4 个 pass）          交给 4.3
┌────────────────────────────┐                              ┌───────────────────────────┐
│ func @main(%a) -> memref   │  --buffer-results-to-out-params│ func @main(%a, %out)      │
│   ...                      │  --bufferization-lower-dealloc│   ...（写 %out）           │
│   return %r : memref       │  --convert-bufferization-to-  │   return（无返回值）       │
│ bufferization.dealloc 等   │     memref                    │ 纯 memref.load/store      │
└────────────────────────────┘  --memref-expand              └───────────────────────────┘
                                  接口已成「输入端口+输出端口」形状
```

出参化是「软件函数 → 硬件端口」的第一步塑形：把 return 改写成往出参里写，本质上把「结果」从函数的「出口」挪到了「入口侧的额外输入」。下一站 Handshake 会把这个形状进一步映成 valid/ready 通道。

#### 4.2.3 源码精读

本模块四个 pass：

[scripts/pipeline/linalg_to_cf.sh:18-21](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/linalg_to_cf.sh#L18-L21) —— 出参化 + 缓冲化收尾：

```bash
  --buffer-results-to-out-params \
  --bufferization-lower-deallocations \
  --convert-bufferization-to-memref \
  --memref-expand \
```

逐行说明：

- 第 18 行 `--buffer-results-to-out-params`：本模块核心。把返回 memref 的函数改写成「memref 作出参」。改动示意（示例代码，非项目原始 IR）：

  ```mlir
  // 改写前 —— 示例代码
  func.func @main(%arg0 : memref<16xi32>) -> memref<16xi32> {
    %r = ... : memref<16xi32>
    return %r : memref<16xi32>
  }

  // 改写后 —— 示例代码
  func.func @main(%arg0 : memref<16xi32>, %out : memref<16xi32>) {
    // 原 %r 的内容写到 %out
    return
  }
  ```

  调用方相应地从「接收返回值」改为「提供一个空 buffer 传进去」。这与 u4-l2 仿真时「直接给内部 handshake memory 播种」、以及 u6-l2「外部化超大存储」的硬件式思维一脉相承——硬件总是「提供一块地，让模块往里写」，而不是「等模块吐出一个新东西」。

- 第 19 行 `--bufferization-lower-deallocations`：把 `bufferization.dealloc` 这种「内存生命周期」抽象降级为底层操作。MLIR 的 `dealloc` 本身带「按需释放、可能批量」的语义，下游认不得，必须展开成显式的比较 + 条件释放。

- 第 20 行 `--convert-bufferization-to-memref`：清缴 `bufferization` 方言残部。one-shot bufferize 主要把 tensor 换 memref，但会留下 `to_memref`/`to_tensor`/`clone`/`alloc_tensor` 等「缓冲化专属」算子；这一步把它们转成普通 `memref` 算子，让 IR 里只剩 `memref` 一种内存方言。

- 第 21 行 `--memref-expand`：把高层 memref 算子展开成 `load`/`store`。例如 `memref.copy %src, %dst` 会被展开成「逐元素 load + store 的循环」。这一步保证下游（Handshake 合法化、最终到硬件）看到的 memref 操作都在足够细的粒度上。

这四步合起来，把「还带着 bufferization 抽象、return 一个 buffer 的函数」彻底收拾成「纯 memref.load/store、用出参写结果」的硬件友好形态。具体 IR 变化取决于工具版本，**待本地验证**。

#### 4.2.4 代码实践

**实践目标**：验证「返回 memref 的函数」在 `--buffer-results-to-out-params` 之后变成「无返回值、多一个 memref 出参」。

**操作步骤**：

1. 沿用 4.1.4 的 `/tmp/cf-bufonly.mlir`（仅做了缓冲化、仍 return memref）。
2. 只追加本模块的出参化 pass，隔离观察接口变化：
   ```bash
   mlir-opt /tmp/linalg.mlir \
     --empty-tensor-to-alloc-tensor \
     --one-shot-bufferize="bufferize-function-boundaries" \
     --buffer-results-to-out-params \
     > /tmp/cf-outparam.mlir
   ```
3. 看 `@main` 的签名：
   ```bash
   echo "== 仅缓冲化（应有 -> memref 返回） =="
   grep -E 'func.func @main' /tmp/cf-bufonly.mlir
   echo "== 加了出参化（应多出 %out 出参、无 memref 返回） =="
   grep -E 'func.func @main' /tmp/cf-outparam.mlir
   ```

**需要观察的现象**：

- `/tmp/cf-bufonly.mlir` 的 `@main` 签名里有一个 `-> memref<...>` 返回类型。
- `/tmp/cf-outparam.mlir` 的 `@main` 签名里返回类型消失（或变成无返回），同时形参列表多出一个 `memref<...>`（出参）。

**预期结果**：证实「return buffer → 出参」的改写确实发生，函数从「软件式返回」变成「硬件式写端口」。这正是实践任务里要预测的现象：**如果去掉 `--buffer-results-to-out-params`，函数会保留 `-> memref<...>` 返回值，下游 Handshake 就得多处理一种「返回通道」特例，与统一的 valid/ready 端口模型不匹配。**

> matmul 是 1-D 向量点积、结果为标量，出参化的具体表现（标量是否也被 buffer 化成 1 元素 memref）取决于工具版本，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**（即本讲核心实践任务的后半）：去掉 `--buffer-results-to-out-params`，函数接口会变成什么样？为什么这对下游不利？

**参考答案**：函数会保留 `func.func @main(%a : memref<...>) -> memref<...>`，即「返回一个 buffer」。下游 Handshake 数据流降级期望每个函数是「输入端口 + 输出端口」的对偶，结果天然对应输出端口；而「return 一个 memref」是一种额外特例，需要单独的返回通道处理，与统一的端口模型不一致。出参化把这个特例提前消除，让函数接口直接长成硬件端口的样子，降低下游降级难度。

**练习 2**：`--convert-bufferization-to-memref` 和 `--bufferization-lower-deallocations` 都在「收拾缓冲化留下的东西」，它们处理的对象有何不同？

**参考答案**：`--bufferization-lower-deallocations` 专门针对 `bufferization.dealloc`（内存释放）这一类「生命周期」算子，把它降级成显式的比较 + 条件释放。`--convert-bufferization-to-memref` 则面向其余缓冲化算子（`to_memref`/`to_tensor`/`clone`/`alloc_tensor` 等），统一转成普通 memref 算子。前者管「释放」语义的展开，后者管「方言残部」的归一，分工互补。

**练习 3**：`--memref-expand` 把 `memref.copy` 展开成什么？为什么必须展开？

**参考答案**：大致展开成「对每个元素做 `memref.load` 再 `memref.store`」的逐元素复制（可能套循环）。必须展开是因为下游（Handshake 合法化、再到 HW/SV）只认 `load`/`store` 这种最基本的内存读写，不认 `memref.copy` 这种「整块拷贝」的高层糖。展开到 load/store 粒度后，下游才能进一步把每次读写映成硬件端口访问。

---

### 4.3 Linalg/SCF 到 CF 循环：把张量算子展开成 goto 式控制流

#### 4.3.1 概念说明

经过 4.1、4.2，IR 里已全是 memref，接口也出参化了。但算子层面，**Linalg 结构化算子（如 `linalg.matmul`、`linalg.generic`）还原封不动**——它们是「声明式」的：用索引映射（indexing maps）和迭代类型（iterator types）描述「做什么计算」，不包含任何显式循环。本模块把这两个高层结构**展开**：

1. **`--convert-linalg-to-loops`（Linalg → 循环）**：把每个 `linalg.generic` / 命名 `linalg.*` 算子展开成**嵌套 `scf.for` 循环**，循环体里是显式的 `memref.load`（读输入）和 `memref.store`（写输出）。例如一个矩阵-向量乘 `linalg.matvec` 会变成「外层遍历行、内层累加列」的双层 `scf.for`。

   展开依据是 Linalg 算子自带的「索引映射 + 迭代类型」元数据。一个 `linalg.generic` 的语义可形式化地写成（对每个输出元素）：

   \[
   \text{out}[i, j] = f\big(\text{in}_1[\,\text{map}_1(i,j)\,],\ \text{in}_2[\,\text{map}_2(i,j)\,]\big)
   \]

   其中 $\text{map}_k$ 是第 $k$ 个输入的索引映射（仿射映射），$f$ 是 combinator（如 `mul`+`add` 实现 matmul）。`--convert-linalg-to-loops` 就是把这套「对所有 $(i,j)$ 计算 $f$」的声明，翻译成「for i ... { for j ... { load, 计算, store } }」的命令式循环。

2. **`--convert-scf-to-cf`（SCF → CF）**：把上一步产生的结构化 `scf.for`/`scf.if` 拆成 `cf.br`/`cf.cond_br` + 基本块的 goto 式控制流。理由见 2.5：下游 Handshake 降级期望扁平的基本块 + 跳转，不认结构化区域。

这两个 pass 之后，IR 从「声明式张量算子」彻底变成「命令式循环 + goto 控制流 + memref 读写」——也就是下一站期待的「CF + memref form」。

#### 4.3.2 核心流程

```
4.2 产物（Linalg 算子 + memref）          本模块（2 个 pass）          本站最终产物
┌──────────────────────────────┐                                  ┌──────────────────────────┐
│ linalg.generic / linalg.matmul│ --convert-linalg-to-loops        │ scf.for → 已拆成         │
│   （声明式，无显式循环）       │ ───────────────────────────────► │ cf.br / cf.cond_br 基本块 │
│ memref.load/store             │ --convert-scf-to-cf              │ + memref.load/store      │
│                               │ ───────────────────────────────► │ = 「CF + memref form」    │
└──────────────────────────────┘                                  └──────────────────────────┘
```

注意 `linalg → scf 循环` 这步先产生 `scf.for`（结构化），`scf → cf` 这步再把它拆成基本块。顺序固定：必须先有 scf 才能把它拆成 cf，反过来无从下手。

#### 4.3.3 源码精读

本模块两个 pass：

[scripts/pipeline/linalg_to_cf.sh:24-25](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/linalg_to_cf.sh#L24-L25) —— 把 Linalg 展开成循环，再拆成 goto 式控制流：

```bash
  --convert-linalg-to-loops \
  --convert-scf-to-cf \
```

逐行说明：

- 第 24 行 `--convert-linalg-to-loops`：把声明式 Linalg 算子翻译成命令式 `scf.for` 嵌套循环。改写示意（示例代码，非项目原始 IR；以 1-D 点积为例）：

  ```mlir
  // 改写前 —— 示例代码
  %r = linalg.dot ins(%a, %b : memref<16xi32>, memref<16xi32>)
                  outs(%init : memref<i32>) : memref<i32>

  // 改写后 —— 示例代码
  %sum = ... // 累加器
  scf.for %i = %c0 to %c16 step %c1 iter_args(%acc = %c0_i32) -> i32 {
    %x = memref.load %a[%i] : memref<16xi32>
    %y = memref.load %b[%i] : memref<16xi32>
    %prod = arith.muli %x, %y : i32
    %new = arith.addi %acc, %prod : i32
    scf.yield %new : i32
  }
  ```

- 第 25 行 `--convert-scf-to-cf`：把上面 `scf.for`/`scf.yield` 拆成基本块 + `cf.br`/`cf.cond_br`。改写示意（示例代码）：

  ```mlir
  // 改写后（示意）—— 示例代码
  cf.br ^header(%c0, %c0_i32)
  ^header(%i, %acc):
    %cond = arith.cmpi slt, %i, %c16 : index
    cf.cond_br %cond, ^body(%i, %acc), ^exit(%acc)
  ^body(%i, %acc):
    %x = memref.load %a[%i] : memref<16xi32>
    ...
    %next = arith.addi %i, %c1 : index
    cf.br ^header(%next, %new)
  ^exit(%acc):
    memref.store %acc, %out[] : memref<i32>
    cf.br ^return
  ```

两步之后，IR 里 `linalg.` 与 `scf.` 基本消失，只剩 `cf.` + `memref.` + `arith.`。这就是「CF + memref form」。具体算子形态取决于工具版本与模型，**待本地验证**。

最后还有两个清理 pass：

[scripts/pipeline/linalg_to_cf.sh:26-27](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/linalg_to_cf.sh#L26-L27) —— 循环展开后的清理：

```bash
  -canonicalize \
  -cse
```

`-canonicalize`（常量折叠、简单重写）+ `-cse`（公共子表达式消除）做收尾清理，让交给下游的 IR 更紧凑。注意脚本里 `canonicalize`/`cse` 出现了**两次**（L22–L23 在循环展开前，L26–L27 在循环展开后）：因为缓冲化收尾后、循环展开前各有一次「中间清理」，展开后又产生了一批可折叠/可消除的冗余，需要再清一次。这是「边降级边清理」的常见做法。

#### 4.3.4 代码实践

**实践目标**：看到 Linalg 算子先变成 `scf.for` 循环，再变成 `cf.br`/`cf.cond_br` 基本块，分两步可观察。

**操作步骤**：

1. 只跑到「Linalg→循环」这步，看 `scf.` 出现：
   ```bash
   mlir-opt /tmp/linalg.mlir \
     --empty-tensor-to-alloc-tensor \
     --one-shot-bufferize="bufferize-function-boundaries" \
     --buffer-results-to-out-params \
     --bufferization-lower-deallocations \
     --convert-bufferization-to-memref \
     --memref-expand \
     -canonicalize -cse \
     --convert-linalg-to-loops \
     > /tmp/cf-loops.mlir
   grep -oE 'scf\.[a-z]+|linalg\.[a-z]+|cf\.[a-z]+' /tmp/cf-loops.mlir | sort | uniq -c
   ```
2. 再加 `--convert-scf-to-cf`，看 `cf.` 取代 `scf.`：
   ```bash
   mlir-opt /tmp/cf-loops.mlir --convert-scf-to-cf -canonicalize -cse > /tmp/cf-final.mlir
   grep -oE 'scf\.[a-z]+|linalg\.[a-z]+|cf\.[a-z]+' /tmp/cf-final.mlir | sort | uniq -c
   ```

**需要观察的现象**：

- `/tmp/cf-loops.mlir`：`linalg.*` 计数大幅下降甚至归零，`scf.for`/`scf.yield` 出现，`cf.*` 还很少。
- `/tmp/cf-final.mlir`：`scf.*` 基本消失，`cf.br`/`cf.cond_br` 大量出现，`memref.load`/`memref.store` 是主要内存操作。

**预期结果**：证实「Linalg → scf 循环 → cf 基本块」两步展开确实发生，最终产物就是「CF + memref form」。这也是下一站 `cf_to_handshake.sh` 能直接 `lower-cf-to-handshake` 的原因——它要的正是基本块 + 跳转这种扁平控制流。

> matmul 实际 IR 形态取决于工具版本，**待本地验证**。若 `mlir-opt` 不在 PATH，用 `nix build .#<对应 cf 派生>` 后到 Nix store 取产物观察亦可。

#### 4.3.5 小练习与答案

**练习 1**：`--convert-linalg-to-loops` 产出的是 `scf.for` 而不是直接 `cf.br`，为什么还要再来一个 `--convert-scf-to-cf`？

**参考答案**：MLIR 的降级是分层的，`--convert-linalg-to-loops` 只负责「声明式算子 → 结构化循环」这一层，产物自然是 `scf.for`/`scf.yield`（结构化控制流）。把结构化控制流拆成基本块 + 跳转（cf）是**另一层**职责，由 `--convert-scf-to-cf` 单独完成。分开做让每个 pass 只管一件事、可单独测试，也符合本项目「每站/每 pass 只降一段」的哲学。

**练习 2**：`-canonicalize` 和 `-cse` 在脚本里出现了两次（L22–L23、L26–L27），为什么不只在末尾清一次？

**参考答案**：缓冲化收尾（L16–L21）后会产生一批可折叠常量、可消除的冗余表达式；如果不先清，循环展开（L24）会在更大的 IR 上展开，工作量更大、产物更臃肿。先清一次（L22–L23）让循环展开面对更紧凑的 IR；展开后又新生成一批冗余（如循环归纳变量的运算），再清一次（L26–L27）。分阶段清理比一次清理更高效，也更不容易让中间 IR 膨胀。

**练习 3**：为什么下游 Handshake 降级期望的是 CF（基本块 + 跳转）而不是 SCF（结构化循环）？

**参考答案**：Handshake 是弹性数据流模型，它把控制流映成数据流图里的分支/汇合节点。基本块 + 跳转（cf）这种扁平的 goto 式控制流，节点边界清晰、可一一对应成数据流图的开关；而 `scf.for`/`scf.if` 的「区域 + yield」结构是嵌套的，无法直接映成扁平的数据流节点。所以必须先用 `--convert-scf-to-cf` 拆成基本块，下一站的 `--lower-cf-to-handshake`（见 u3-l2 的 cf_to_handshake.sh）才能处理。

---

### 4.4 本站在 Nix 里的编排

本讲虽以 `linalg_to_cf.sh` 为主，但它在真实流水线里由 Nix 调起。看一下编排，为 u3-l5（pipeline.nix 全景）铺垫。

[nix/pipeline.nix:36-40](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L36-L40) —— `mkCfDerivation`：用 `runCommand` 把本站脚本包成派生，产出 `${name}-cf.mlir`：

```nix
mkCfDerivation = { name, linalg }:
  pkgs.runCommand "${name}-cf.mlir" { buildInputs = [ mlir ]; } ''
    ${pkgs.bash}/bin/bash ${pipelineScripts}/linalg_to_cf.sh \
      ${mlir}/bin/mlir-opt ${linalg} "$out"
  '';
```

要点：

- 与上一站 `mkLinalgDerivation`（用 `torchMlir`、`torchMlirOpt`）不同，本站 `buildInputs = [ mlir ]`，调用的是 **`${mlir}/bin/mlir-opt`**——即上游通用 `mlir-opt`。这正是 2.6 节强调的：本站全是上游标准 pass，不需要 torch/circt 专用工具。
- 调用形式是 `linalg_to_cf.sh <mlir-opt> <input> <output>` 的 3 参数形式（脚本第 9–11 行定义）：`${mlir}/bin/mlir-opt`（工具）、`${linalg}`（u2-l3 的 Linalg 输入）、`$out`（Nix 填的输出路径）。
- 输入 `${linalg}` 来自上一站派生，形成依赖。

[nix/pipeline.nix:106-109](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L106-L109) —— `mkPipeline` 里 `cf` 段：把本站接在 `linalg` 之后：

```nix
cf = mkCfDerivation {
  inherit name;
  inherit (self) linalg;
};
```

`inherit (self) linalg` 是 u3-l5 会详讲的「惰性自引用」技巧：`self` 是整条 pipeline 的属性集，`cf` 派生的输入 `linalg` 指向同一 `self` 里的 `linalg` 派生（即 102–105 行那个）。这样 Nix 自动把「`cf` 依赖 `linalg`」建链，`linalg` 没变时 `cf` 直接复用缓存，不会重跑本站的 12 个 pass。

缓存边界是理解 Nix 流水线的关键：**改了 u2-l2/u2-l3 的 torch/linalg 输出 → 本站 `cf` 重算；只改本站之后的握手/hw/sv → 本站 `cf` 复用缓存**。这条规则在 u3-l5 会系统讲解。

---

## 5. 综合实践

把本讲三个模块串起来，完成实践任务：**把 `linalg_to_cf.sh` 的 12 个 pass 分类，并预测「去掉出参化」的后果。**

**任务步骤**：

1. **跑通本站**：按 4.1.4–4.3.4 的步骤，准备 `/tmp/linalg.mlir`（u2-l3 产物），再用完整 `linalg_to_cf.sh` 跑出 `/tmp/cf-final.mlir`：
   ```bash
   bash scripts/pipeline/linalg_to_cf.sh "$(command -v mlir-opt)" /tmp/linalg.mlir /tmp/cf-final.mlir
   ```
2. **给 12 个 pass 分类**：对照 [linalg_to_cf.sh:16-27](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/linalg_to_cf.sh#L16-L27)，用三种颜色/标记标出：
   - 「从 tensor 变 buffer」：第 16、17 行（`--empty-tensor-to-alloc-tensor`、`--one-shot-bufferize`）。
   - 「出参化/缓冲化收尾」：第 18–21 行（`--buffer-results-to-out-params` 等）。
   - 「把张量运算变循环」：第 24、25 行（`--convert-linalg-to-loops`、`--convert-scf-to-cf`）。
   - 清理：第 22–23、26–27 行（`-canonicalize`、`-cse`）。
3. **统计证据**：分别在 `/tmp/linalg.mlir`（输入）和 `/tmp/cf-final.mlir`（输出）上跑：
   ```bash
   for f in /tmp/linalg.mlir /tmp/cf-final.mlir; do
     echo "== $f =="
     grep -oE 'tensor<[^>]*>|memref<[^>]*>|linalg\.[a-z]+|scf\.[a-z]+|cf\.[a-z]+' "$f" | sort | uniq -c
   done
   ```
   预期：输入以 `tensor<...>`/`linalg.*` 为主；输出以 `memref<...>`/`cf.*` 为主。
4. **预测性验证（核心实践任务后半）**：把 `linalg_to_cf.sh` 复制一份到 `/tmp/`，删掉第 18 行 `--buffer-results-to-out-params`，重跑，观察 `@main` 签名：
   ```bash
   # 复制并删行（注意：在 /tmp/ 改副本，绝不动项目源码）
   cp scripts/pipeline/linalg_to_cf.sh /tmp/linalg_to_cf_nooutparam.sh
   # 用编辑器删掉 /tmp/linalg_to_cf_nooutparam.sh 里的 --buffer-results-to-out-params \ 那行
   bash /tmp/linalg_to_cf_nooutparam.sh "$(command -v mlir-opt)" /tmp/linalg.mlir /tmp/cf-nooutparam.mlir
   grep -E 'func.func @main' /tmp/cf-nooutparam.mlir
   ```
   预测：`@main` 会保留 `-> memref<...>` 返回值（而不是出参），证明出参化确实是消除「return buffer」的那一步。

**预期交付**：一张 12-pass 分类表（分缓冲化/出参化/循环/清理四类）+ 输入输出算子统计对比 + 对「去掉出参化后接口变成 `-> memref` 返回」的预测与验证。

> ⚠️ 第 4 步一定要改 `/tmp/` 下的**副本**，绝不能修改项目里的 `scripts/pipeline/linalg_to_cf.sh`。能否跑通取决于 `mlir-opt` 是否在 PATH，具体 IR 形态**待本地验证**。若环境跑不动，可改为纯源码阅读型实践：只完成第 2、3 步的「分类与对照」并标注「待本地验证」。

---

## 6. 本讲小结

- 本站 `linalg_to_cf.sh` 是降级链第三站，把 u2-l3 的 **Linalg-on-Tensors** 一次性缓冲化、出参化、展开成循环，产出「**CF + memref form**」，交给下一站 `cf_to_handshake.sh`（u3-l2）。下一站开头注释直接印证了这个产物名。
- **12 个 pass 可分三类**：①缓冲化（`--empty-tensor-to-alloc-tensor` + `--one-shot-bufferize="bufferize-function-boundaries"`，tensor 首次变 memref）；②出参化收尾（`--buffer-results-to-out-params` 把 return buffer 改出参，再 `lower-deallocations`/`convert-bufferization-to-memref`/`memref-expand` 把缓冲化残部 lower 到底）；③循环展开（`--convert-linalg-to-loops` 把声明式算子变 `scf.for`，`--convert-scf-to-cf` 再拆成 `cf.br`/`cf.cond_br` 基本块）。另有两轮 `-canonicalize`/`-cse` 清理。
- **出参化对硬件关键**：硬件没有 return、只有端口，`--buffer-results-to-out-params` 把「返回 buffer」改成「写进出参」，正是「软件函数 → 硬件输入/输出端口」的第一步塑形，为下一站 Handshake 的 valid/ready 通道铺路。
- 本站用的是**上游通用 `mlir-opt`**（`nix/pipeline.nix` 里 `mkCfDerivation` 的 `buildInputs = [ mlir ]`），因为全是标准 pass；这与上一站用 torch-mlir-opt、下一站用 circt-opt 形成对照。
- 在 Nix 里，`mkCfDerivation` 接在 `mkLinalgDerivation` 之后（`mkPipeline` 的 `cf` 段 `inherit (self) linalg`）；`linalg` 输入不变时，本站 12 个 pass 的产物直接复用缓存。

---

## 7. 下一步学习建议

- **紧接的下一讲 u3-l2**：`cf_to_handshake.sh`——本站产出的「CF + memref form」在那里先被 `flatten-memref` + `handshake-legalize-memrefs` 整理，再用 `--lower-cf-to-handshake` 变成 Handshake 弹性数据流图。你会看到本站辛苦做出的「基本块 + 出参」如何映成 valid/ready 通道，正好印证本讲「出参化是为硬件端口塑形」的结论。
- **横向对照**：读一遍 `scripts/pipeline/` 下其他脚本（如 `handshake_to_hs_ext.sh`），体会它们如何复用 `common.sh` 的同一套骨架（`require_file`/`require_executable`/`run_to_output`），只是换工具、换 pass。
- **深入 bufferize**：若想理解 one-shot bufferize 的 in-place 分析细节，可看 MLIR 上游 Bufferization 文档；注意本项目用的 `mlir` 版本由 `flake.nix` pin（见 u1-l3 的「三套独立 LLVM/MLIR」），个别 pass 行为可能与主线版本略有差异，**待本地验证**。
- **回顾**：若对「为何停在 tensor」感到模糊，回头读 u2-l3 的 4.2 节；本站正是 u2-l3 推迟的那次缓冲化的落点。若对 valid/ready 数据流模型好奇，可先扫一眼 u3-l2 的概念铺垫。
