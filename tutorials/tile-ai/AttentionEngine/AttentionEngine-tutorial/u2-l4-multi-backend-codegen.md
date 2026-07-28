# 多后端代码生成：TileLang / CuTe / PyTorch

## 1. 本讲目标

本讲承接 u2-l3（`generate_tl_from_dag` 把符号 DAG 翻译成 TileLang 代码）。在上一讲里，我们默认走的是 **TileLang（tl）** 这一条后端。但实际上，`generate_tl_from_dag` 可以把**同一棵符号 DAG** 发射成**三种不同目标**的代码：

- `tl`（TileLang）：生成 Python 形式的 GPU kernel，元素级算子包在 `for ... in T.Parallel(...)` 里，用 `T.*` 内建函数；
- `cute`（CuTe C++）：生成面向 Hopper 的 C++ 片段，用 `#pragma unroll` + 普通 `for` 循环，用 `exp2f` / `__logf` 等 CUDA 内建；
- `pytorch`：生成在宿主机 eager 执行的 `torch.*` 代码，**没有循环**，靠 PyTorch 的广播语义工作。

学完本讲你应该能够：

1. 说清楚 `to_tl_op` / `to_cute_op` / `to_pytorch_op` 三套发射器分别产出什么样的代码，以及它们在**循环结构**与**索引语法**上的本质区别。
2. 知道 `generate_tl_from_dag` 里 `to_tl` / `to_cute` 两个布尔参数如何切换目标，以及 `to_tl > to_cute > pytorch` 的优先级。
3. 理解为什么 GPU 上要把 `exp(x)` 写成 `exp2(x*1.442695)`、`log(x)` 写成 `log2(x)*0.69314718`——也就是「换底等价」这个数值技巧背后的数学。

## 2. 前置知识

在继续之前，请确认你已经掌握（这些都在 u2-l1 ~ u2-l3 建立过）：

- **符号 DAG**：用户的 `score_mod` / `online_func` / 线性注意力的 `*_mod` 最终都被记录成一棵由 `SymbolScalar` 节点组成的计算图，每个节点有 `type`（算子类型）、`prev`（用户层输入链）、`shape_idx`（形状维度名，如 `["block_M","block_N"]`）、`varname`（变量名）。
- **`generate_tl_from_dag`**：后序遍历符号 DAG，对每个节点调用一个「发射器」函数，把节点翻译成一段文本代码；遍历靠 `lowered` 标志去重，靠 `count`/`visit_count`/`allow_reuse` 做 inplace 复用优化。
- **三种输出的落点**（先记住结论，本讲会展开）：
  - `tl` 代码 → 注入 Jinja2 模板，编译成 GPU kernel；
  - `cute` 代码 → 注入 CuTe C++ 模板，编译成 FlashAttention 风格的 `.cu`/`.h`；
  - `pytorch` 代码 → 作为**宿主机侧的参考/预处理实现**，例如线性注意力里 `k_mod`/`v_mod`/`decay_mod` 的 fused 预处理就是 eager PyTorch。

一句话回顾：**符号 DAG 是与后端无关的「中间表示」，发射器是把它翻译到具体语言的那一层翻译器**。本讲要打开的就是「翻译器」本身。

## 3. 本讲源码地图

本讲涉及的文件非常集中，几乎全部围绕一个文件：

| 文件 | 作用 |
|---|---|
| `attention_engine/core/codegen/tl_gen.py` | **核心文件**。包含三套发射器 `to_tl_op` / `to_cute_op` / `to_pytorch_op`，以及统一调度入口 `generate_tl_from_dag`。 |
| `attention_engine/core/utils.py` | `IndentedCode` 代码缓冲区类，三套发射器都靠它拼接带缩进的文本。 |
| `attention_engine/core/transform/core.py` | `SymbolScalar` 在这里提供 `.exp()` / `.exp2()` / `.log()` / `.log2()` 等方法，决定算子 `type`（`Exp` vs `Exp2`），是发射器的输入侧。 |
| `attention_engine/core/lower/lower_cute.py` | CuTe 后端降级器，调用 `generate_tl_from_dag(..., to_tl=False, to_cute=True)`。 |
| `attention_engine/core/lower/lower_linear.py` | 线性注意力降级器，调用 `generate_tl_from_dag(..., to_tl=False)`（即走 pytorch 发射器）生成 mod 的参考实现。 |

> 提示：`tl_gen.py` 这个文件名里虽然只有 `tl`，但它其实装了**全部三种后端**的发射器。这是历史命名，不要被名字误导。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：① 三套发射器总览与统一调度；② 三种后端的循环结构与索引差异；③ `exp→exp2`、`log→log2` 的数值等价技巧。

### 4.1 三套发射器总览与统一调度

#### 4.1.1 概念说明

「发射器（emitter）」是一种很常见的编译器设计模式：**中间表示不变，换一个翻译器就能输出不同目标语言**。在 AttentionEngine 里，中间表示是符号 DAG（节点 `type` 字符串如 `"Exp"`、`"Add"`、`"ReduceMax"`），翻译器就是三个函数：

- `to_tl_op(type, *args)` → TileLang
- `to_cute_op(type, *args)` → CuTe C++
- `to_pytorch_op(type, *args)` → PyTorch

它们共享**完全相同的函数签名**：第一个参数是算子类型字符串，其余参数是参与运算的 `SymbolScalar`（第一个 `args[0]` 约定为输出节点，后面是输入节点）。这样，`generate_tl_from_dag` 只需要在遍历到某个节点时，按目标后端选一个发射器调用即可，DAG 遍历逻辑（去重、inplace 复用、输入收集）对三种后端完全一样。

#### 4.1.2 核心流程

调度逻辑的伪代码：

```
对 DAG 做后序遍历（generate_tl 内部递归）：
    对每个未 lowered 的节点 x：
        根据 x.code.type 与目标后端，选择发射器：
            若 to_tl  == True  → to_tl_op(x.code.type, x, *x.prev)
            否则若 to_cute == True  → to_cute_op(x.code.type, x, *x.prev)
            否则（两者皆 False）      → to_pytorch_op(x.code.type, x, *x.prev)
        把发射器返回的 IndentedCode 拼到总代码缓冲区
        标记 x.lowered = True
```

注意三选一的优先级是 **`to_tl` 优先于 `to_cute` 优先于 `pytorch`**。即使你同时传 `to_tl=True, to_cute=True`，也会走 tl。

#### 4.1.3 源码精读

先看入口函数的签名与默认值（默认 `to_tl=True, to_cute=False`，所以不传任何后端参数就是 TileLang）：

[attention_engine/core/codegen/tl_gen.py:306-307](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L306-L307) —— `generate_tl_from_dag` 的签名，`to_tl=True, to_cute=False` 是默认 TileLang。

真正的三选一调度只在一处发生：

[attention_engine/core/codegen/tl_gen.py:353-358](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L353-L358) —— 这 6 行就是整个「多后端」机制的核心：`if to_tl: ... elif to_cute: ... else: to_pytorch_op(...)`。优先级 tl > cute > pytorch 在这里一目了然。

三个发射器的定义入口（同文件，依次排列）：

[attention_engine/core/codegen/tl_gen.py:7](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L7) —— `to_tl_op` 定义。
[attention_engine/core/codegen/tl_gen.py:135](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L135) —— `to_cute_op` 定义。
[attention_engine/core/codegen/tl_gen.py:236](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L236) —— `to_pytorch_op` 定义。

最后，三种代码都拼进同一个 `IndentedCode` 缓冲区。它是 `utils.py` 里一个极简的「带缩进的字符串累加器」，`add_line` 会自动在行首加 4 空格 × 缩进层级：

[attention_engine/core/utils.py:4-26](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/utils.py#L4-L26) —— `IndentedCode` 类，`more_indent()`/`less_indent()` 配合发射器内部的循环嵌套使用。

#### 4.1.4 代码实践

**实践目标**：搞清楚「谁在调用哪个后端」，验证三种后端确实在仓库里有真实调用点。

**操作步骤**：在仓库根目录执行下面的命令，统计 `generate_tl_from_dag` 各后端的调用点（命令仅供查阅，本机无需 GPU）：

```bash
# CuTe 后端调用点（to_tl=False, to_cute=True）
grep -rn "to_cute=True" attention_engine/core/lower/

# PyTorch 后端调用点（to_tl=False，且不带 to_cute）
grep -rn "to_tl=False" attention_engine/core/lower/ | grep -v "to_cute=True"

# TileLang 后端调用点（不传后端参数，默认 to_tl=True）
grep -rn "generate_tl_from_dag(" attention_engine/core/lower/ | grep -v "to_tl=False"
```

**需要观察的现象**：

- CuTe 调用点只出现在 `lower_cute.py`（3 处：online_fwd、online_fwd_epilogue、score_mod），见 [tl_gen.py 调用方 lower_cute.py:83-84](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_cute.py#L83-L84)。
- PyTorch 调用点只出现在 `lower_linear.py`（`lowerKmod`/`lowerVmod`/`lowerDecaymod`/`lowerQmodFused`），见 [lower_linear.py:145](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_linear.py#L145) —— 它把 `k_mod` 跑成符号 DAG 后，用 pytorch 发射器生成一段 `torch.*` 参考代码，存进 `lower_output.k_mod_expr`。
- TileLang 调用点遍布 `lower.py` / `lower_gqa.py` / `lower_decode*.py` 等绝大多数降级文件。

**预期结果**：你会得出一张清晰的「后端 → 落点」表：tl 是主力（训练/解码的 GPU kernel），cute 只服务 CuTe 后端，pytorch 只在线性注意力的 mod 预处理里当参考实现。

**待本地验证**：`grep` 命令本身一定可运行；具体调用点行号随版本可能微调，以你本地的输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果我调用 `generate_tl_from_dag([x], to_tl=True, to_cute=True)`，会走哪个发射器？为什么？
**答案**：走 `to_tl_op`。因为调度逻辑里 `if to_tl` 是第一个分支，`to_tl` 优先级最高；`to_cute=True` 在 `to_tl=True` 时被忽略。

**练习 2**：为什么三个发射器要用**完全相同**的函数签名 `(type, *args)`？
**答案**：这样 `generate_tl_from_dag` 的 DAG 遍历逻辑（去重、inplace 复用、输入收集）对三种后端完全复用，切换后端只需改一行 `to_tl_op(...)` → `to_cute_op(...)`。这是「中间表示与目标语言解耦」的直接好处。

---

### 4.2 三种后端的循环结构与索引差异

#### 4.2.1 概念说明

三种后端最直观的区别是**怎么表达「对一个 block 里每个元素做运算」**：

- **tl**：用 TileLang 的 `for i0,i1 in T.Parallel(block_M, block_N):` 把元素级算子包起来，下标用方括号 `scores[i0,i1]`。
- **cute**：用标准 C++ 的 `for (int i0=0; i0<size<0>(tensor); i0++)`，加 `#pragma unroll`，下标用圆括号 `tensor(i0,i1)`（CuTe 张量的 `operator()`）。
- **pytorch**：**根本不写循环**，完全依赖 PyTorch 的张量广播，必要时用 `[...,None]` 补维、用 `.sum(dim=...)` 降维。

另一个关键差异是**多输入的「维度对齐」方式**。tl 会按维度**名字**对齐（`block_M` 对 `block_M`），cute 按**位置**对齐，pytorch 按**广播语义**对齐。

#### 4.2.2 核心流程

以一个减法 `t1 = scores - m_new` 为例，三种发射器的处理：

```
scores.shape_idx = ["block_M","block_N"]   # 二维
m_new.shape_idx  = ["block_M","1"]          # 行向量（"1" 表示该维大小为 1，需广播）

【tl】to_tl_op("Sub", t1, scores, m_new)
  - 输出 t1 的 shape_idx 决定循环：for i0,i1 in T.Parallel(block_M, block_N):
  - scores：按名字对齐 → scores[i0,i1]
  - m_new：block_M 对齐到 i0；"1" 退化成固定下标 0 → m_new[i0,0]
  - 生成：t1[i0,i1] = scores[i0,i1] - m_new[i0,0]

【cute】to_cute_op("Sub", t1, scores, m_new)
  - 遍历 t1 的 idx_list = [i0,i1]，生成两层 unroll for 循环
  - scores：(i0,i1)；m_new：按位置 (i0, "1"→0) = (i0,0)
  - 生成：t1(i0,i1) = scores(i0,i1) - m_new(i0,0);

【pytorch】to_pytorch_op("Sub", t1, scores, m_new)
  - 不生成循环。两者秩相同（都是 2）→ 不补 None。
  - 生成：t1 = scores - m_new   # 由 torch 自动广播 "1" 那一维
```

#### 4.2.3 源码精读

**tl 的 `T.Parallel` 循环与「按名字对齐」**：

[attention_engine/core/codegen/tl_gen.py:38-69](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L38-L69) —— 这段是 tl 发射器的「大脑」。第 39-43 行先给输出 `args[0]` 自身建下标；第 44-62 行的 `while target_i < len(args[0].shape_idx) and idx != args[0].shape_idx[target_i]:` 这段就是「按名字对齐」——它在一个输入的某个维度名字在输出的 shape_idx 里逐位查找，找到才用对应的 `i{target_i}`，找不到就报错。`"1"` 维直接退化为下标 `0`。第 64-69 行最后生成 `for i0,i1 in T.Parallel(block_M, block_N):`。

> 关键理解：tl 的对齐是**名字匹配**，所以即使某个输入的维度顺序和输出不同，只要名字对得上就能正确对齐；这是为了支持动态形状（维度是字符串名字而非数字）。

**cute 的 `#pragma unroll` + C++ for 循环与「按位置对齐」**：

[attention_engine/core/codegen/tl_gen.py:148-172](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L148-L172) —— cute 发射器的索引构建。第 150-158 行对**每个** arg 都用 `[f"i{i}" if idx!="1" else "0" for i,idx in enumerate(input_idx)]`，注意这里的 `i` 是 **`enumerate` 的位置下标**，不是名字匹配——所以 cute 假设各输入的维度**按位置对齐**。第 165-172 行对输出 `args[0]` 的每个维度生成一层 `#pragma unroll` + `for (int i0=0; i0<size<0>(tensor); i0++) {`，其中 `size<ii>(tensor)` 是 CuTe 取第 `ii` 维大小的内建。

cute 还有一个细节：单元素标量（shape_idx 为 `["1"]`）会被特判成空下标（第 155 行 `idx_list = [] if idx_list == ["0"] else idx_list`），从而不进循环包裹。此外，cute 每发射完一个算子都会追加一行调试注释：

[attention_engine/core/codegen/tl_gen.py:229-232](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L229-L232) —— `// used: {count}, use_list: [...]`，方便在生成的 C++ 里排查 inplace 复用。tl 和 pytorch 没有这行。

**pytorch 的「无循环 + 广播补维」**：

[attention_engine/core/codegen/tl_gen.py:253-267](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L253-L267) —— pytorch 发射器。第 253-256 行：如果输出秩比最高输入秩小，就用 `.sum(dim=(...))` 把多出来的维累加掉（`suffix_code`）；第 260-267 行：如果某个输入秩比输出小，就给它补 `[...,None,None,...]` 来对齐。整个过程**不产生任何 for 循环**，完全交给 PyTorch 的广播。

#### 4.2.4 代码实践

**实践目标**：对同一个表达式，亲手（阅读源码）预测三种发射器各自生成的代码骨架，体会循环结构差异。

**操作步骤**：考虑表达式 `t = (scores - m_new).exp()`，其中 `scores` 的 shape_idx 是 `["block_M","block_N"]`、`m_new` 的 shape_idx 是 `["block_M","1"]`。请打开 [tl_gen.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py)，分别查阅 `to_tl_op`（`"Sub"` 在 L77-80、`"Exp"` 在 L89-92）、`to_cute_op`（`"Sub"` 在 L179-182、`"Exp"` 在 L191-194）、`to_pytorch_op`（`"Sub"` 在 L269-272、`"Exp"` 在 L281-284），把每种后端**手写**出 `Sub` 和 `Exp` 两个节点会生成的文本。

**需要观察的现象（参考答案）**：

| 节点 | tl 产物 | cute 产物 | pytorch 产物 |
|---|---|---|---|
| `Sub` | `for i0,i1 in T.Parallel(block_M, block_N):`<br>`    t1[i0,i1] = scores[i0,i1] - m_new[i0,0]` | `#pragma unroll`<br>`for (int i0=0; i0<size<0>(t1); i0++) {`<br>`#pragma unroll`<br>`for (int i1=0; i1<size<1>(t1); i1++) {`<br>`    t1(i0,i1) = scores(i0,i1) - m_new(i0,0);`<br>`}` `}` | `t1 = scores - m_new` |
| `Exp` | `for i0,i1 in T.Parallel(block_M, block_N):`<br>`    t2[i0,i1] = T.exp2(t1[i0,i1]*1.442695)` | （同上两层 unroll for 内）<br>`    t2(i0,i1) = exp2f(t1(i0,i1)*1.442695);` | `t2 = torch.exp(t1)` |

请重点体会三件事：① tl 用 `T.Parallel`、cute 用 `#pragma unroll`+`for`、pytorch 没循环；② tl 用 `[...]`、cute 用 `(...)`；③ tl/cute 的 `Exp` 都带 `*1.442695`，pytorch 直接 `torch.exp`（下一节解释）。

**预期结果**：你能不靠运行、只靠读源码，准确画出三种后端的代码骨架，并说出每行的来源行号。

> ⚠️ 说明：上表是**依据源码逻辑手推**的产物，变量名 `t1`/`t2` 是占位说明；实际变量名会被 `generate_tl_from_dag` 的 inplace 复用逻辑（u2-l3 讲过）改名（比如 `t1` 可能被复用成 `scores`），但**结构**（循环、下标、函数名）与上表一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 tl 发射器要做「按名字对齐」（那个 `while ... target_i += 1` 的查找），而 cute 直接「按位置对齐」？
**答案**：tl 面向 TileLang 的动态形状（维度是字符串名字），不同输入的维度顺序/子集可能不同，必须按名字匹配才能正确对齐；cute 生成的是 C++ 模板代码，CuTe 张量维度按位置排列且在编译期固定，输入与输出维度按位置一一对应即可，所以不需要查找。

**练习 2**：pytorch 发射器里 `suffix_code = "... .sum(dim=(...))"` 这一步在什么情况下会触发？
**答案**：当输出 `args[0]` 的秩小于参与运算的最高秩输入时（第 254 行 `if len(output_idx) < max_len`）。这说明该算子对那些「多出来的维」做了规约，所以用 `.sum` 把它们累加掉，让输出秩与 `SymbolScalar` 声明的 `shape_idx` 一致。

---

### 4.3 exp→exp2、log→log2 的数值等价技巧

#### 4.3.1 概念说明

在 GPU 上，**「2 的幂」指令（`exp2`）通常比「e 的幂」指令（`exp`）更快**，因为硬件的指数指令天然以 2 为底。同理 `log2` 比 `log` 快。AttentionEngine 利用换底公式，把用户写的 `exp` / `log` 改写成等价的 `exp2` / `log2` 调用：

- 用 `exp2(x * 1.442695)` 代替 `exp(x)`，其中 `1.442695 ≈ log₂ e`；
- 用 `log2(x) * 0.69314718` 代替 `log(x)`，其中 `0.69314718 ≈ ln 2`。

这是一个**数值等价**（在浮点精度内结果相同）但**更快**的改写。需要强调的是：这个技巧主要用在 **tl** 后端；**cute** 后端对 `exp` 同样用 `exp2f(x*1.442695)`，但对 `log` 直接用 CUDA 的快速内建 `__logf`（它本身就是快速的以 e 为底对数），**不走换底**；**pytorch** 后端则直接用 `torch.exp` / `torch.log`，完全不做改写（因为宿主机侧不在意这点开销）。

#### 4.3.2 核心流程

换底的数学依据（关键恒等式）：

\[ \exp(x) = 2^{\log_2 e \cdot x}, \qquad \log_2 e = \frac{1}{\ln 2} \approx 1.442695 \]

代入即得 `exp2(x * 1.442695)` 与 `exp(x)` 等价。对数方向：

\[ \ln(x) = \log_2(x) \cdot \ln 2, \qquad \ln 2 \approx 0.69314718 \]

代入即得 `log2(x) * 0.69314718` 与 `log(x)` 等价。注意 `1.442695` 与 `0.69314718` 互为倒数（`1/ln2 = log2 e`），这并非巧合——它们是同一个常数 `ln 2` 的两种用法。

> 还有一组「诚实的」算子：`SymbolScalar.exp2()` / `.log2()`（见 [transform/core.py:237-244](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L237-L244)）会发射原生 `Exp2`/`Log2` 节点，表示用户**真的想要 2^x 或 log₂ x**，这时发射器不再乘常数（tl 发 `T.exp2(x)`、cute 发 `exp2f(x)`）。也就是说：`Exp` 节点会被换底加速，`Exp2` 节点保持原样。

#### 4.3.3 源码精读

**tl 后端的换底**：

[attention_engine/core/codegen/tl_gen.py:89-92](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L89-L92) —— `Exp` 被发射成 `T.exp2(x*1.442695)`，乘的就是 `log₂ e`。
[attention_engine/core/codegen/tl_gen.py:105-108](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L105-L108) —— `Log` 被发射成 `T.log2(x) * 0.69314718`，乘的就是 `ln 2`。

对照「不换底」的原生 `Exp2`/`Log2`：

[attention_engine/core/codegen/tl_gen.py:93-96](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L93-L96) —— `Exp2` 直接 `T.exp2(x)`，无常数。
[attention_engine/core/codegen/tl_gen.py:109-112](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L109-L112) —— `Log2` 直接 `T.log2(x)`，无常数。

**cute 后端：exp 复用换底，log 用 `__logf`**：

[attention_engine/core/codegen/tl_gen.py:191-194](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L191-L194) —— cute 的 `Exp` 同样 `exp2f(x*1.442695)`，换底技巧与 tl 一致。
[attention_engine/core/codegen/tl_gen.py:207-210](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L207-L210) —— cute 的 `Log` 用 `__logf(x)`，这是 CUDA 的快速以 e 为底对数内建函数，**不做换底**。这是 cute 与 tl 在对数处理上的一个真实差异。

**pytorch 后端：直接用 torch 原语**：

[attention_engine/core/codegen/tl_gen.py:281-284](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L281-L284) —— pytorch 的 `Exp` 直接 `torch.exp(x)`。
[attention_engine/core/codegen/tl_gen.py:293-296](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L293-L296) —— pytorch 的 `Log` 直接 `torch.log(x)`。两者都不做换底改写。

把三后端的「指数/对数」处理汇总成一张表：

| 算子 `type` | tl | cute | pytorch |
|---|---|---|---|
| `Exp`（e^x） | `T.exp2(x*1.442695)` | `exp2f(x*1.442695)` | `torch.exp(x)` |
| `Exp2`（2^x） | `T.exp2(x)` | `exp2f(x)` | 不支持 |
| `Log`（ln x） | `T.log2(x)*0.69314718` | `__logf(x)` | `torch.log(x)` |
| `Log2`（log₂ x） | `T.log2(x)` | 不支持 | 不支持 |

> 这张表体现了本讲的精髓：**同一个 `Exp` 节点，三种后端给出三种实现**，其中 tl 和 cute 都借 `exp2` 加速，pytorch 直接用 `exp`；而 `Log` 上 tl 走换底、cute 走 `__logf`、pytorch 走 `log`，三套方案各取所需。

#### 4.3.4 代码实践

**实践目标**：验证 `exp2(x*1.442695)` 与 `exp(x)` 在数值上确实近似相等，亲手算一遍换底。

**操作步骤**：下面的 Python 片段**不依赖 GPU**（发射器本身是纯 Python，只 import `torch`/`sympy`），可以直接在 CPU 上跑，用来验证换底技巧并观察三种发射器的真实输出：

```python
# 示例代码：验证 exp2 换底 + 观察三后端发射差异
import math
import torch

# 1) 数值验证：exp2(x*1.442695) ≈ exp(x)
x = torch.linspace(-5, 5, 11)
lhs = torch.exp2(x * 1.442695)   # 换底写法
rhs = torch.exp(x)               # 原始 e^x
print("max |exp2(x*1.442695) - exp(x)| =", (lhs - rhs).abs().max().item())

# 2) log 换底验证：log2(x)*0.69314718 ≈ log(x)
xp = torch.linspace(0.1, 5, 11)
print("max |log2(x)*0.69314718 - log(x)| =",
      (torch.log2(xp) * 0.69314718 - torch.log(xp)).abs().max().item())

# ---- 以下为可选：直接调用三套发射器（待本地验证导入链） ----
# from attention_engine.core.codegen.tl_gen import generate_tl_from_dag
# from attention_engine.core.transform.core import SymbolicArray
# from attention_engine.core.transform.graph import Var
# scores = SymbolicArray("scores", Var("scores"), shape_idx=["block_M","block_N"])
# m_new   = SymbolScalar("m_new", Var("m_new"))   # 需先设定 shape_idx=["block_M","1"]
# t = (scores - m_new).exp()
# for flags, name in [({}, "tl"), ({"to_tl":False,"to_cute":True}, "cute"), ({"to_tl":False}, "pytorch")]:
#     t.clear_codegen()  # 复用前清状态
#     code, _ = generate_tl_from_dag([t], **flags)
#     print(f"===== {name} =====\n{code}")
```

**需要观察的现象**：

- 第 1 步打印的最大误差应当是个极小值（量级在 `1e-6` 或更小），证明 `exp2(x*1.442695)` 与 `exp(x)` 数值等价。
- 第 2 步同样验证 `log2` 换底。
- （可选）三后端代码块的输出，应分别出现 `T.exp2(...*1.442695)`、`exp2f(...*1.442695)`、`torch.exp(...)`，与 4.3.3 的表格一致。

**预期结果**：换底前后最大误差在单精度浮点的舍入量级内，可视为等价。

**待本地验证**：第 1、2 步纯 torch 计算，本机即可跑出数值；第 3 步「直接调用发射器」需要把 `attention_engine/` 挂到 `PYTHONPATH`，且其导入链会引入 `torch`/`sympy`（本讲未实际执行，请本地确认导入无误后再跑）。

#### 4.3.5 小练习与答案

**练习 1**：请用换底公式推导「为什么 `log2(x) * 0.69314718` 等于 `ln(x)`」。
**答案**：由换底公式 \(\log_2 x = \frac{\ln x}{\ln 2}\)，两边乘 `\ln 2` 得 `\log_2 x \cdot \ln 2 = \ln x`。而 `\ln 2 ≈ 0.69314718`，故 `log2(x) * 0.69314718 = ln(x)`。

**练习 2**：cute 后端的 `Log` 为什么不像 tl 那样用 `log2f(x)*0.693`，而是直接 `__logf(x)`？
**答案**：CUDA 提供了 `__logf` 这个**快速的以 e 为底对数内建**，它本身就是优化过的 `ln` 实现，所以不需要再借 `log2` 换底。tl 后端没有等价的快速 `ln` 内建可用（TileLang 的 `T.*` 体系下 `log2` 更快），所以才走换底路线。这是两种后端在「如何拿到一个快的 ln」上各自选择了本生态里最快的手段。

---

## 5. 综合实践

**任务**：充当一次「人肉发射器」，把同一个符号表达式同时翻译成三种后端，并解释每处差异。

设 `scores`（shape_idx `["block_M","block_N"]`）和 `m_new`（shape_idx `["block_M","1"]`），表达式为：

```
t = ((scores - m_new).exp()).log()
```

请完成：

1. **画出 DAG**：列出这个表达式会产生哪些节点（`Sub`、`Exp`、`Log`），每个节点的 `type` 和参与运算的输入。
2. **手写三后端代码**：只参考 [tl_gen.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py)，分别写出 tl、cute、pytorch 三种产物。重点写出：
   - 循环结构（tl 的 `T.Parallel` / cute 的 `#pragma unroll`+`for` / pytorch 的无循环）；
   - `Exp` 与 `Log` 各自的函数调用（注意 tl/cute 的 `Exp` 带 `*1.442695`，tl 的 `Log` 带 `*0.69314718`，cute 的 `Log` 是 `__logf`，pytorch 是 `torch.exp`/`torch.log`）；
   - 下标语法（`[...]` vs `(...)`）。
3. **验证**：用 4.3.4 的纯 torch 片段确认 `(scores-m_new).exp().log()` 在换底前后近似相等（注：`exp(x).log() ≈ x`，可顺带验证这一恒等）。
4. **思考**：如果用户改用 `.exp2()` 而非 `.exp()`，三后端的输出会发生什么变化？（提示：常数 `1.442695` 会消失，变成原生 `exp2`/`exp2f`，pytorch 会抛 `NotImplementedError`，因为 `to_pytorch_op` 没有 `Exp2` 分支。）

> 完成后，你应该能脱稿说出：「同一棵符号 DAG，tl 用 `T.Parallel`+`[]`+换底，cute 用 `unroll for`+`()`+`exp2f`/`__logf`，pytorch 用广播+`torch.*` 且不换底；切换只需改 `to_tl`/`to_cute` 两个布尔位。」

## 6. 本讲小结

- AttentionEngine 的多后端能力集中在 `tl_gen.py` 的三个发射器 `to_tl_op` / `to_cute_op` / `to_pytorch_op`，它们签名相同，由 `generate_tl_from_dag` 按 `to_tl > to_cute > pytorch` 的优先级三选一调度。
- 三后端最直观的差异是循环与索引：tl 用 `for ... in T.Parallel(...)` + 方括号 `[]` 且**按名字对齐**；cute 用 `#pragma unroll` + C++ `for` + 圆括号 `()` 且**按位置对齐**；pytorch **不写循环**，靠广播与 `[...,None]`/`.sum` 对齐。
- 数值技巧：tl 和 cute 都把 `exp(x)` 写成 `exp2(x*1.442695)`（`\log_2 e`）来加速；tl 还把 `log(x)` 写成 `log2(x)*0.69314718`（`\ln 2`），cute 的 `log` 则直接用 CUDA 快速内建 `__logf`；pytorch 两者都不改写，直接 `torch.exp`/`torch.log`。
- `Exp` 节点会被换底加速，而 `Exp2`/`Log2` 节点（来自 `.exp2()`/`.log2()`）保持原生的 2 为底，不乘常数——这是「想要 e^x」与「真的想要 2^x」的区别。
- 三后端的真实落点：tl 是主力（训练/解码 GPU kernel），cute 只在 `lower_cute.py`，pytorch 只在 `lower_linear.py` 的 mod 预处理里当参考实现。

## 7. 下一步学习建议

本讲把「符号 DAG → 三种后端代码」的最后一道翻译工序讲透了。接下来：

- **向应用层走（推荐先做）**：进入 u2-l5《score_mod 的符号化与降级》，看 `lower.py` 如何把用户的 `score_mod` 跑成符号 DAG、用 `generate_tl_from_dag`（默认 tl）生成前向函数，并用 `SymbolScalar.backward` 自动生成反向。本讲是它的直接前置。
- **向 CuTe 后端深入**：如果想看 cute 发射器的产物如何被塞进 C++ 模板，可跳到 u5-l1《CuTe 后端代码生成》，结合 `lower_cute.py` 的 `to_cute=True` 调用点阅读。
- **建议阅读源码**：动手把 4.3.4 的纯 torch 验证片段跑一遍，确认换底误差；再通读一遍 [tl_gen.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py) 全文，把三套发射器的每个 `if type == ...` 分支对照一遍，形成「同一算子、三种实现」的肌肉记忆。
