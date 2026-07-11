# torch-MLIR 前端降级：torch 方言到 Linalg

> 本讲承接 u2-l2。u2-l2 的产物是「torch 方言 MLIR 文本」——里面全是 `torch.aten.*` 这类 PyTorch 风格算子。本讲要回答的问题是：**这些 torch 算子是怎么变成下游 CIRCT 能处理的 Linalg 形式的？** 答案就藏在 35 行的 `torch_to_linalg.sh` 里。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 `torch-mlir-opt` 是什么、它在本项目降级链里的位置。
2. 解释 `torch-function-to-torch-backend-pipeline` 与 `torch-backend-to-linalg-on-tensors-backend-pipeline` 这**两条 pipeline** 各自做什么，以及它们的先后为什么不能颠倒。
3. 看懂 `common.sh` 里 `require_file` / `require_executable` / `run_to_output` 三个工具函数的复用模式，并理解所有降级脚本为何都 `source` 它。
4. 讲清楚为什么本阶段输出的是 **tensor 上的 Linalg**，而不是 buffer（memref）上的——这是理解整条降级链「把缓冲化推迟到最后」设计的关键。
5. 把 `nix/pipeline.nix` 里 `mkLinalgDerivation` 如何把这一站包成可缓存的 Nix 派生说清楚。

---

## 2. 前置知识

本讲默认你已经读过 u2-l1（适配器契约）和 u2-l2（`compile-pytorch.py` 与 `torch.export`）。下面补充几个 MLIR / torch-MLIR 的基础概念，全部用通俗语言。

### 2.1 什么是「降级（lowering）」

一个 PyTorch 模型在最终变成 FPGA 上的电路前，要经历很多种**中间表示（IR）**。每次把一种较高层、较抽象的 IR 转成一种较低层、较接近硬件的 IR，就叫一次**降级（lowering）**。整条 LLM2FPGA 链条就是一串连绵不断的降级：

```
PyTorch ──► torch 方言 ──► Linalg ──► CF/循环 ──► Handshake ──► HW ──► SystemVerilog ──► RTLIL
```

本讲只负责其中一小步：**torch 方言 → Linalg**。

### 2.2 什么是「方言（dialect）」

MLIR 里每一种 IR 风格叫一个**方言**。例如：

- `torch` 方言：算子长得很像 PyTorch，如 `torch.aten.matmul`、`torch.aten.relu`。
- `linalg` 方言：线性代数结构化算子，如 `linalg.matmul`、`linalg.generic`。
- `tensor` 方言：描述「值类型」的张量（不可变的数学张量）。
- `memref` 方言：描述「缓冲区」的张量（带内存布局的可变内存）。

不同方言可以混在同一段 IR 里，降级就是逐步把高层方言替换成低层方言的过程。

### 2.3 tensor vs buffer（memref）：本讲的灵魂

这是本讲最重要的概念，先建立一个直觉：

- **tensor（值语义）**：把张量当成数学值，像 `a + b` 里的数字。它「是什么」比「存在哪」更重要。没有别名、没有副作用，容易做数学化推理和优化。
- **memref / buffer（内存语义）**：把张量当成一块内存缓冲区，关心地址、布局、谁在写它。它贴近硬件，但一旦转过去就很难再回头。

torch-MLIR 在这一步**故意停在 tensor 上**，把「从值变成内存」这件不可逆的事推迟到后面的 `linalg_to_cf.sh`（u3-l1）再做。这就是 MLIR 社区「**尽可能晚地做 one-shot bufferize**」的哲学。理解这一点，你就理解了为什么本阶段叫「Linalg-on-**Tensors**」。

### 2.4 `mlir-opt` / `torch-mlir-opt` 是什么

`mlir-opt` 是 MLIR 的通用「跑一遍 pass（变换）」命令行工具：读入一段 IR 文本，按你给的 `--pass` 列表依次变换，把结果打到 stdout。

`torch-mlir-opt` 是 torch-MLIR 项目自带的同款工具，**额外**注册了 torch 方言相关的 pass（比如本讲的两条 backend pipeline）。本项目用 `torch-mlir-opt` 而不是 `mlir-opt`，正是因为只有它认得这两条 pipeline。

### 2.5 什么是「pipeline」

一个 **pass** 是一次单独的变换（如 `-canonicalize`）。一个 **pipeline** 是一组被官方打包好、按正确内部顺序执行的 pass 集合，对外只暴露一个名字。本讲的 `--torch-...-pipeline` 就是这种「打包 pass」，所以我们不用关心它内部几十步的顺序，只调用一个名字即可。

---

## 3. 本讲源码地图

本讲只涉及三个文件，职责如下：

| 文件 | 行数 | 职责 |
|---|---|---|
| [scripts/pipeline/torch_to_linalg.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/torch_to_linalg.sh) | 35 | 本站主体：解析参数、校验输入、调用 `torch-mlir-opt` 跑两条 pipeline 把 torch 方言降到 Linalg-on-Tensors。 |
| [scripts/pipeline/common.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/common.sh) | 24 | 全流水线共享的脚手架：三个小函数 `require_file` / `require_executable` / `run_to_output`，被所有降级脚本 `source`。 |
| [nix/pipeline.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix) | 156 | 把每个降级脚本包成可缓存的 Nix 派生；本讲聚焦其中的 `torchMlirOpt` 与 `mkLinalgDerivation`。 |

一句话定位：**`torch_to_linalg.sh` 是降级链的第二站**（第一站是 u2-l2 的 `compile-pytorch.py`）。它读入 u2-l2 产出的 torch 方言 `.mlir`，吐出 Linalg-on-Tensors 的 `.mlir`，交给下一站 `linalg_to_cf.sh`。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **torch-mlir-opt 两条降级 pipeline**——本站真正干活的部分。
2. **Linalg-on-Tensors 中间表示**——为什么停在 tensor 而非 buffer。
3. **common.sh 脚手架**——所有降级脚本共享的复用模式。

---

### 4.1 torch-mlir-opt 与两条 backend pipeline

#### 4.1.1 概念说明

torch-MLIR 把「torch 方言 → 标准方言」这件事拆成了**两段**，分别对应两条 pipeline：

1. **`torch-function-to-torch-backend-pipeline`**：torch 方言内部的一次「归一化」。输入是高层、自由、贴近 PyTorch 前端的 torch 算子（如 `torch.aten.*`、`torch.prim.*`）；输出仍是 torch 方言，但已被规整成一个**受约束的后端子集（Torch Backend IR）**——内联完毕、Python 层构造被清除、变体算子被收敛到规范形式。**它没有离开 torch 方言。**

2. **`torch-backend-to-linalg-on-tensors-backend-pipeline`**：真正「跨方言」的降级。把上一步的 Torch Backend IR 翻译成**标准 MLIR 方言**——主要是 `linalg`、`tensor`、`arith`、`math` 等。这一步之后 torch 算子基本消失，`torch.aten.matmul` 变成了 `linalg.matmul`（或等价结构）。

> 为什么拆两段？因为「在 torch 方言里把乱七八糟的前端算子收敛干净」和「把干净的 torch 算子翻译成标准算子」是两个性质完全不同的工程问题，分开做让每段都可单独测试、单独复用。这也正是本项目的「每站只降一段」哲学在前端层的体现。

最后再加一个 **`-canonicalize`**：这是单个 pass（不是 pipeline），做一些「免费」的清理——常量折叠、死参数消除、简单模式重写，让输出 IR 更干净、更利于下游处理。

#### 4.1.2 核心流程

本站在降级链里的执行流程（黑盒视角）：

```
        u2-l2 产出                    本站 torch_to_linalg.sh                     交给 u3-l1
   ┌─────────────────┐   torch-mlir-opt + 两条 pipeline + canonicalize   ┌─────────────────┐
   │ torch 方言 .mlir │ ───────────────────────────────────────────────► │ Linalg-on-      │
   │ torch.aten.matmul│                                                  │ Tensors .mlir   │
   └─────────────────┘                                                    │ linalg.matmul   │
                                                                          └─────────────────┘
```

`torch-mlir-opt` 内部的数据流：

```
torch 前端 IR
   │  ① --torch-function-to-torch-backend-pipeline   （torch 方言内部归一化）
   ▼
torch 后端 IR（仍是 torch 方言，受约束子集）
   │  ② --torch-backend-to-linalg-on-tensors-backend-pipeline  （跨方言：torch → linalg/tensor/arith）
   ▼
Linalg-on-Tensors IR
   │  ③ -canonicalize   （清理）
   ▼
最终输出
```

注意三步是**严格顺序**的：① 把前端弄干净，② 才能把干净的前端翻译出去，③ 最后打扫。顺序不能颠倒——没有 ① 的归一化，② 的翻译表覆盖不全，会留下未降级的 torch 算子。

#### 4.1.3 源码精读

真正干活的就这几行：

[scripts/pipeline/torch_to_linalg.sh:31-34](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/torch_to_linalg.sh#L31-L34) —— 本站核心：调用 `torch-mlir-opt` 跑两条 pipeline + canonicalize，把输出写到 `$output`：

```bash
run_to_output "$output" "$torch_mlir_opt" "$input" \
  --torch-function-to-torch-backend-pipeline \
  --torch-backend-to-linalg-on-tensors-backend-pipeline \
  -canonicalize
```

逐行说明：

- `run_to_output "$output" ...`：把后面命令的 stdout 重定向到 `$output` 文件（见 4.3.3）。
- `$torch_mlir_opt`：`torch-mlir-opt` 可执行文件路径（由参数或环境变量提供）。
- `$input`：输入的 torch 方言 `.mlir` 文件（u2-l2 的产物）。
- `--torch-function-to-torch-backend-pipeline`：上面 ①，torch 方言内部归一化为后端子集。
- `--torch-backend-to-linalg-on-tensors-backend-pipeline`：上面 ②，跨方言降到 Linalg-on-Tensors。
- `-canonicalize`：上面 ③，清理。

那 `$torch_mlir_opt` / `$input` / `$output` 从哪来？看参数解析：

[scripts/pipeline/torch_to_linalg.sh:15-26](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/torch_to_linalg.sh#L15-L26) —— 支持两种调用方式：传 3 个参数（工具、输入、输出），或传 2 个参数（输入、输出）并用 `TORCH_MLIR_OPT` 环境变量指定工具：

```bash
if [ "$#" -eq 3 ]; then
  torch_mlir_opt="$1"; input="$2"; output="$3"
elif [ "$#" -eq 2 ]; then
  torch_mlir_opt="${TORCH_MLIR_OPT:-}"
  input="$1"; output="$2"
  [ -n "$torch_mlir_opt" ] || usage
else
  usage
fi
```

这种「工具路径既可显式传也可走环境变量」的双模式，是为了方便：Nix 派生里显式传绝对路径（见 4.1.3 末尾的 pipeline.nix），而人在 devShell 里手跑时可以 `export TORCH_MLIR_OPT=$(which torch-mlir-opt)` 后少写一个参数。

调用前还有两个安全校验（在第 28–29 行）：

```bash
require_executable "$torch_mlir_opt"
require_file "$input"
```

先确认工具可执行、输入文件存在，否则立刻退出——避免把错误信息混进 stdout 污染输出文件。这两个函数定义在 `common.sh`，4.3 节细讲。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `torch.aten.matmul` 在这一站之后变成 `linalg` 上的算子。

**操作步骤**：

1. 进入带全套工具的 devShell（u1-l3 已讲）：
   ```bash
   nix develop
   ```
2. 用 u2-l2 的脚本，把 matmul 适配器导出成 torch 方言 MLIR：
   ```bash
   python scripts/compile-pytorch.py --adapter src/matmul_adapter.py --out /tmp/torch.mlir
   ```
3. 把 `torch-mlir-opt` 指给本站脚本，跑降级：
   ```bash
   TORCH_MLIR_OPT="$(command -v torch-mlir-opt || true)" \
   bash scripts/pipeline/torch_to_linalg.sh /tmp/torch.mlir /tmp/linalg.mlir
   ```
   > 若 devShell 没把 `torch-mlir-opt` 放到 PATH 上，可改用 3 参数形式，显式给出 Nix store 里的路径（参见 4.1.3 末与 4.3 的 `torchMlirOpt` 候选路径）。
4. 观察输出：
   ```bash
   grep -E 'linalg\.|tensor\.|arith\.' /tmp/linalg.mlir | head
   ```

**需要观察的现象**：

- 输入 `/tmp/torch.mlir` 里应能看到 `torch.aten.matmul` 这类 torch 方言算子（u2-l2 的产物）。
- 输出 `/tmp/linalg.mlir` 里 `torch.aten.*` 基本消失，取而代之的是 `linalg.*`（如 `linalg.matmul` / `linalg.generic`）、`tensor.*`、`arith.*` 等标准方言算子。
- 输入是 1-D 向量点积（matmul.py 对两个 `(16,)` 向量做 `torch.matmul`，结果为标量），所以对应的 linalg 算子会带一个归约维度——**具体算子名待本地验证**。

**预期结果**：torch 方言被替换成 Linalg-on-Tensors，且张量类型仍是 `tensor<...>`（值类型），**没有**出现 `memref`。若仍残留 `torch.` 开头算子，说明某算子未被两条 pipeline 覆盖（这正是 u1-l1 提到的「算子不被支持」风险）。

> ⚠️ 上述命令是否能在你的环境里原样跑通，取决于 devShell 是否把 `torch-mlir-opt` 暴露到 PATH。若不通，请用第 3 步的 3 参数形式显式传工具路径。具体运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：把 `--torch-function-to-torch-backend-pipeline` 和 `--torch-backend-to-linalg-on-tensors-backend-pipeline` 调换顺序，会发生什么？

**参考答案**：会出错或产出仍含大量未降级算子的 IR。第二条 pipeline 的输入必须是第一条产出的「Torch Backend IR」这一受约束子集；直接对原始前端 IR 跑第二条，其翻译表无法覆盖自由形态的前端算子，导致降级失败。这正是它们被打包成**两条独立 pipeline** 且顺序固定的原因。

**练习 2**：去掉末尾的 `-canonicalize`，输出会有什么变化？

**参考答案**：功能上 IR 仍正确，但会留下冗余——比如未被折叠的常量、死参数、可合并的简单模式。`-canonicalize` 不改变语义，只做「免费」清理，让下游脚本拿到的 IR 更小更规整。去掉它通常不会让下游崩溃，但会让下游做重复清理工作。

**练习 3**：本站用的是 `torch-mlir-opt` 而不是通用的 `mlir-opt`，为什么？

**参考答案**：因为 `--torch-function-to-torch-backend-pipeline` 和 `--torch-backend-to-linalg-on-tensors-backend-pipeline` 这两条 pipeline 是由 torch-MLIR 项目注册的，只有 `torch-mlir-opt` 这个带 torch 方言支持的工具才认得它们；通用 `mlir-opt` 没注册这些 pass，调用会报「unknown pass」。

---

### 4.2 Linalg-on-Tensors：为何停在 tensor 而非 buffer

#### 4.2.1 概念说明

本节回答实践任务的后半句：**为什么这一阶段输出的是 tensor 上的 Linalg 而不是 buffer 上的？**

先看名字。第二条 pipeline 叫 `torch-backend-to-linalg-on-tensors-**backend**-pipeline`——「on-tensors」已经把答案写在脸上了：它产出的是**作用在 `tensor` 类型上的 Linalg 算子**，而不是作用在 `memref`（buffer）上的。

为什么这么设计？因为：

1. **tensor 是值语义**。`linalg.matmul ins(%a, %b : tensor<...>) outs(%c : tensor<...>)` 描述的是「把 `%a`、`%b` 这两个值做矩阵乘，结果写进 `%c` 这个新值」——一个纯数学等式，没有内存别名、没有副作用。这让上游可以做充分的数学化优化（融合、重排、消冗余），而不必担心「谁先写哪块内存」。
2. **buffer 化是不可逆的硬承诺**。一旦把 `tensor` 换成 `memref`，你就锁定了内存布局、缓冲区生死、是否复用——这些是逼近硬件的决定，做早了会堵死优化空间。
3. **MLIR 的最佳实践是「一次性缓冲化（one-shot bufferize）尽量晚」**。本项目把这件事统一交给下一站 `linalg_to_cf.sh`（u3-l1）的 `--one-shot-bufferize` 一次性完成，而不是在每个阶段零散地碰内存。

所以本站的产物是一个「数学上干净、还没碰内存」的 IR：所有张量都是 `tensor<...>`，缓冲化留给后续。

#### 4.2.2 核心流程

在本项目整条链里，「tensor → buffer」的切换点是下一站，不是本站：

```
本站产出                      下一站 linalg_to_cf.sh（u3-l1）
┌──────────────────┐         --one-shot-bufferize 等一串 pass
│ Linalg-on-Tensors│ ──────────────────────────────────────► memref/buffer 上的循环
│ tensor<...> 值语义│                                          + 出参化 + dealloc
└──────────────────┘
   还没碰内存              ◄── 缓冲化集中在这里一次性做 ──►
```

对比下一站的 pass 列表就能看到分界线。下一站 [scripts/pipeline/linalg_to_cf.sh:15-27](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/linalg_to_cf.sh#L15-L27) 里有：

- `--one-shot-bufferize="bufferize-function-boundaries"`：把 tensor 一次性换成 memref（buffer 化）。
- `--buffer-results-to-out-params`：把返回的 buffer 改成出参（对硬件接口至关重要）。
- `--convert-linalg-to-loops` / `--convert-scf-to-cf`：把张量运算展开成循环和控制流。

这些在本站**统统没有**——本站只管「torch 算子 → linalg 算子」，连碰内存的资格都还没到。

#### 4.2.3 源码精读

本站的 pass 列表（再贴一次，聚焦「没有 buffer 化」这一点）：

[scripts/pipeline/torch_to_linalg.sh:31-34](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/torch_to_linalg.sh#L31-L34)：

```bash
run_to_output "$output" "$torch_mlir_opt" "$input" \
  --torch-function-to-torch-backend-pipeline \          # 仍在 torch 方言内
  --torch-backend-to-linalg-on-tensors-backend-pipeline \ # 跨到 linalg/tensor（值语义）
  -canonicalize                                          # 清理，无 buffer 化
```

注意三点：

1. 唯一与「buffer」沾边的词 `on-tensors`，恰恰强调「作用在 tensor 上」。
2. 这里**没有任何** `--one-shot-bufferize`、`--buffer-*`、`--convert-*-to-memref` 之类的 pass——这些都在下一站。
3. 因此产物里张量类型应是 `tensor<16xi32>` 这种值类型，而不是 `memref<16xi32>`。

对照下一站 [scripts/pipeline/linalg_to_cf.sh:16-20](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/linalg_to_cf.sh#L16-L20) 才出现缓冲化 pass：

```bash
  --empty-tensor-to-alloc-tensor \
  --one-shot-bufferize="bufferize-function-boundaries" \
  --buffer-results-to-out-params \
  --bufferization-lower-deallocations \
  --convert-bufferization-to-memref \
```

两站对照，分界线一目了然：**本站停在 tensor，下一站才进 buffer。**

#### 4.2.4 代码实践

**实践目标**：用 grep 验证「本站产物只有 tensor、没有 memref」，并定位 buffer 化到底发生在哪一站。

**操作步骤**：

1. 沿用 4.1.4 产出的 `/tmp/linalg.mlir`（本站产物）。
2. 检查值类型与缓冲类型：
   ```bash
   echo "== 本站产物 (应只看到 tensor) =="
   grep -oE 'tensor<[^>]*>|memref<[^>]*>' /tmp/linalg.mlir | sort | uniq -c
   ```
3. （可选）若你已跑过下一站，对其产物做同样检查，应看到 `memref` 出现：
   ```bash
   # 下一站产物路径取决于 nix 派生名；这里只做概念演示
   grep -c 'memref<' <下一站输出.mlir>
   ```

**需要观察的现象**：

- 在本站 `/tmp/linalg.mlir` 里，`tensor<...>` 计数 > 0，`memref<...>` 计数应为 0（或极少）。
- 在下一站产物里，`memref<...>` 才大量出现。

**预期结果**：证实「缓冲化被推迟到下一站」。这一观察是理解整条降级链「分阶段、晚缓冲化」设计的直接证据。

> 具体计数取决于上游工具版本与 matmul 的实际 IR 形态，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么不在本站就把 tensor 缓冲化成 memref，省得下一站再处理？

**参考答案**：因为 buffer 化是不可逆的硬承诺（锁定内存布局与缓冲区生死），做早了会堵死上游的数学化优化（融合、重排）。MLIR 社区的最佳实践是「one-shot bufferize 尽量晚」，在 tensor 值语义下先把算法优化到位，再一次性换成 buffer。本项目把这个一次性切换集中放在下一站 `linalg_to_cf.sh`。

**练习 2**：如果本站产物里出现了 `memref`，可能意味着什么？

**参考答案**：要么是观察对象搞错了（看的不是本站产物而是下一站产物），要么是 torch-MLIR 的某条 pipeline 内部已经引入了少量 memref（个别边界情况）。一般而言，本站主产物应以 `tensor` 为主；若主流算子的操作数已是 memref，说明 buffer 化被提前了，与设计意图不符，值得排查。

---

### 4.3 common.sh 脚手架：三个可复用工具函数

#### 4.3.1 概念说明

`scripts/pipeline/` 下有十来个降级脚本，每个都长一个样：校验输入 → 调一个工具跑一组 pass → 写输出。如果把「校验文件存在」「校验工具可执行」「把命令输出写进文件」这些样板在每个脚本里重复写，既啰嗦又容易不一致。

`common.sh` 的作用就是把这三件小事抽成三个函数，所有脚本 `source` 它之后直接调用。这是 shell 里最朴素的「函数库 + source 复用」模式，没有花哨依赖。

三个函数：

- `require_file <path>`：文件不存在就报错退出。
- `require_executable <path>`：不是可执行文件就报错退出。
- `run_to_output <output> <cmd...>`：把 `<cmd...>` 的 stdout 重定向写进 `<output>`。

#### 4.3.2 核心流程

每个降级脚本的骨架都是这个套路：

```
source common.sh              # 拿到三个函数
解析参数（工具 / 输入 / 输出）
require_executable "$tool"    # 工具在吗？
require_file "$input"         # 输入在吗？
run_to_output "$output" "$tool" "$input" --pass1 --pass2 ...   # 干活
```

`torch_to_linalg.sh` 完全遵循这个骨架，只是「工具」是 `torch-mlir-opt`、「pass 列表」是那两条 pipeline + canonicalize。后面你会看到 `linalg_to_cf.sh`、`cf_to_handshake.sh` 等都是同一套骨架换工具、换 pass。

#### 4.3.3 源码精读

[scripts/pipeline/common.sh:4-10](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/common.sh#L4-L10) —— `require_file`：检查文件存在，否则打印到 stderr 并以退出码 2 失败：

```bash
require_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    echo "missing input file: $path" >&2
    exit 2
  fi
}
```

要点：

- 用 `[[ ! -f ... ]]` 判断普通文件存在。
- 报错走 `>&2`（stderr），不污染 stdout——这对 `run_to_output` 把 stdout 重定向到输出文件至关重要：错误信息不会写进产物。
- `exit 2`：用非零退出码表示「用法/前置错误」（区别于工具自身以 1 退出表示变换失败）。

[scripts/pipeline/common.sh:12-18](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/common.sh#L12-L18) —— `require_executable`：检查路径存在且**可执行**（`-x`），同样走 stderr + `exit 2`：

```bash
require_executable() {
  local path="$1"
  if [[ ! -x "$path" ]]; then
    echo "missing executable: $path" >&2
    exit 2
  fi
}
```

用 `-x` 而非 `-f`：可执行文件不仅要存在，还得有可执行权限位。

[scripts/pipeline/common.sh:20-24](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/common.sh#L20-L24) —— `run_to_output`：本脚手架最巧妙的一行。把第一个参数当输出文件，剩下当命令，整体 `>输出文件`：

```bash
run_to_output() {
  local output="$1"
  shift
  "$@" >"$output"
}
```

要点：

- `shift` 吃掉第一个参数（输出路径），剩下 `$@` 就是要执行的命令及其参数。
- `"$@" >"$output"`：原样运行命令，把 stdout 整体重定向进输出文件。`"$@"` 带引号保证含空格的参数不分裂。
- 因为 `set -euo pipefail`（脚本顶部第 2 行），命令失败会立刻让整个脚本失败——符合「工具失败就别产出」的预期。

#### 4.3.4 代码实践

**实践目标**：亲手验证三个函数的错误行为，理解它们如何保护流水线。

**操作步骤**：

1. 故意传一个不存在的输入文件，看 `require_file` 如何反应：
   ```bash
   bash scripts/pipeline/torch_to_linalg.sh /tmp/不存在的工具 /tmp/不存在.mlir /tmp/out.mlir
   ```
   预期：打印 `missing executable: /tmp/不存在的工具` 到 stderr，退出码 2，**不创建** `/tmp/out.mlir`。
2. 用一个不存在的可执行文件 + 一个真实文件，看顺序：
   ```bash
   # 先造一个真实输入文件
   : > /tmp/real-input.mlir
   bash scripts/pipeline/torch_to_linalg.sh /tmp/不存在的工具 /tmp/real-input.mlir /tmp/out.mlir
   ```
   预期：先报 `missing executable`（因为脚本里 `require_executable` 在 `require_file` 之前）。

**需要观察的现象**：

- 错误信息只出现在 stderr，stdout 为空——所以即便有人误把 stdout 重定向到文件，也不会得到一个「假的成功产物」。
- 退出码是 2（用 `echo $?` 查看），不是 0。

**预期结果**：三个函数各司其职——`require_executable` / `require_file` 负责前置熔断，`run_to_output` 负责干净地把命令输出落盘。这套约定是后面所有降级脚本能可靠串联的基础。

> 具体报错文案与退出码**待本地验证**（取决于你传的路径是否真不存在）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `require_file` 的报错要用 `>&2` 而不是直接 `echo`？

**参考答案**：因为 `run_to_output` 把命令的 stdout 重定向到输出文件。如果校验失败的报错也走 stdout，就可能被写进产物文件，造成「产物里混着错误信息」的污染。走 stderr 保证错误信息始终显示在终端，且绝不进入产物。

**练习 2**：`run_to_output` 里 `"$@" >"$output"`，为什么 `"$@"` 要带双引号？

**参考答案**：`"$@"` 带引号时，每个参数保持原样、含空格也不被词素分裂。本站传的 `--torch-backend-to-linalg-on-tensors-backend-pipeline` 没空格没事，但下一站 `linalg_to_cf.sh` 会传 `--one-shot-bufferize="bufferize-function-boundaries"` 这种含特殊字符的参数，不带引号就会被 shell 拆错，导致 pass 参数畸形。

**练习 3**：`require_executable` 用 `-x`，`require_file` 用 `-f`，为什么不同？

**参考答案**：`-f` 判断「是普通文件」即可（输入的 `.mlir` 是数据文件）；`-x` 还要求**可执行权限位**（工具必须是能跑起来的程序）。一个只读的 `mlir-opt` 副本存在但不可执行，`require_executable` 必须拦下它。

---

### 4.4 本站在 Nix 里的编排（补充）

本讲虽以两个 shell 脚本为主，但它们在真实流水线里是由 Nix 调起的。简单看一下编排，为 u3-l5（pipeline.nix 全景）铺垫。

[nix/pipeline.nix:3-13](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L3-L13) —— `torchMlirOpt`：在三个候选路径里找到 `torch-mlir-opt` 二进制，找不到就抛错：

```nix
torchMlirOpt = let
  candidates = [
    "${torchMlir}/bin/torch-mlir-opt"
    "${torchMlir}/${python.sitePackages}/torch_mlir/_mlir_libs/torch-mlir-opt"
    "${torchMlir}/${python.sitePackages}/torch_mlir/torch_mlir/_mlir_libs/torch-mlir-opt"
  ];
  matches = builtins.filter builtins.pathExists candidates;
  in if matches == [ ] then
    throw "Unable to locate torch-mlir-opt in ${torchMlir}"
  else builtins.head matches;
```

这解释了为什么 `torch_to_linalg.sh` 要支持「工具路径作第一个参数」——Nix 正是把这里找到的绝对路径显式传给脚本（见下）。

[nix/pipeline.nix:30-34](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L30-L34) —— `mkLinalgDerivation`：用 `runCommand` 把本站脚本包成一个派生，产出 `${name}-linalg.mlir`：

```nix
mkLinalgDerivation = { name, torch }:
  pkgs.runCommand "${name}-linalg.mlir" { buildInputs = [ torchMlir ]; } ''
    ${pkgs.bash}/bin/bash ${pipelineScripts}/torch_to_linalg.sh \
      ${torchMlirOpt} ${torch} "$out"
  '';
```

可以看到它正是以 **3 参数形式**调用 `torch_to_linalg.sh`：`torchMlirOpt`（工具）、`torch`（u2-l2 的 torch 方言输入）、`$out`（Nix 自动填的输出路径）。`buildInputs = [ torchMlir ]` 保证运行时 `torch-mlir-opt` 能找到自己的库。

这一段也是整条链缓存复用的起点：只要 u2-l2 的 torch 输入没变，本站的 `*-linalg.mlir` 就会被 Nix 直接复用缓存，不会重跑。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个小任务：**追踪 matmul 从 torch 方言到 Linalg 的这一站，并解释清楚每一步。**

**任务步骤**：

1. **跑通本站**：按 4.1.4 的步骤，用 `compile-pytorch.py` 生成 `/tmp/torch.mlir`，再用 `torch_to_linalg.sh` 生成 `/tmp/linalg.mlir`。
2. **标注算子变化**：在 `/tmp/torch.mlir` 里找到 matmul 对应的 torch 算子（形如 `torch.aten.matmul`），在 `/tmp/linalg.mlir` 里找到它降级后的 linalg 算子。在两者之间画一条对应箭头。
3. **解释三条 pass**：对照 [torch_to_linalg.sh:31-34](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/torch_to_linalg.sh#L31-L34)，用一句话分别说清 `--torch-function-to-torch-backend-pipeline`、`--torch-backend-to-linalg-on-tensors-backend-pipeline`、`-canonicalize` 做了什么，并说明三者顺序为何固定。
4. **验证 tensor vs buffer**：按 4.2.4，统计 `/tmp/linalg.mlir` 里 `tensor<...>` 与 `memref<...>` 的出现次数，说明本站停在值语义、缓冲化留给下一站。再打开 [linalg_to_cf.sh:16-20](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/linalg_to_cf.sh#L16-L20) 指出缓冲化 pass 的位置。
5. **画分界线**：画一条本站产物（tensor/Linalg）到下一站产物（memref/循环）的箭头，在箭头上标「one-shot bufferize 在此发生」。

**预期交付**：一段说明文字 + 一张从 `torch.aten.matmul` 到 linalg 算子的对应箭头 + 对三条 pass 的逐条解释 + tensor/memref 计数证据。

> 若环境无法直接跑 `torch-mlir-opt`，可改为纯源码阅读型实践：只完成第 3、4、5 步的「解释与对照」部分，并在文档里标注哪些结论「待本地验证」。

---

## 6. 本讲小结

- 本站 `torch_to_linalg.sh` 是降级链第二站，把 u2-l2 产出的 **torch 方言 MLIR** 降到 **Linalg-on-Tensors MLIR**，交给下一站 `linalg_to_cf.sh`。
- 真正干活的是 `torch-mlir-opt` 加**两条 pipeline**：`--torch-function-to-torch-backend-pipeline`（torch 方言内部归一化为后端子集）+ `--torch-backend-to-linalg-on-tensors-backend-pipeline`（跨方言降到 linalg/tensor/arith），再加一个 `-canonicalize` 清理。三者顺序固定。
- 本站**故意停在 tensor（值语义）**，不做 buffer 化——缓冲化（`--one-shot-bufferize` 等）被推迟到下一站一次性完成，这是 MLIR「晚缓冲化」的最佳实践。
- `common.sh` 提供三个复用函数：`require_file` / `require_executable` 做前置熔断（报错走 stderr、退出码 2），`run_to_output` 把命令 stdout 干净落盘。所有降级脚本共用这套骨架。
- 在 Nix 里，`mkLinalgDerivation` 用 `runCommand` 以 3 参数形式调用本脚本，产出的 `${name}-linalg.mlir` 是整条链缓存复用的起点。

---

## 7. 下一步学习建议

- **紧接的下一讲 u3-l1**：`linalg_to_cf.sh`——本站产出的 Linalg-on-Tensors 在那里被一次性缓冲化（`--one-shot-bufferize`）并展开成循环（`--convert-linalg-to-loops`），那是 tensor 第一次变成 buffer/memref 的地方，正好承接本讲「为何停在 tensor」的结论。
- **横向对照**：读一遍 `scripts/pipeline/` 下其他脚本（如 `cf_to_handshake.sh`），体会它们如何复用 `common.sh` 的同一套骨架，只是换工具、换 pass。
- **深入 torch-MLIR**：若想了解两条 pipeline 内部到底跑了哪些 pass，可在 devShell 里用 `torch-mlir-opt --help` 查看，或阅读 torch-MLIR 上游文档（注意本项目对 torch-MLIR 用的 LLVM 是**单独 pin** 的，见 u1-l3）。
- **回顾**：若对「适配器如何产出 torch 方言」感到模糊，回头读 u2-l1（适配器契约）与 u2-l2（`compile-pytorch.py`）；本站正是它们的直接下游。
