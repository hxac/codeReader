# score_mod 的符号化与降级

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `lower_score_mod` 在整个编译链里的定位：它把用户写的 `score_mod` 这个 Python 函数「跑」成一棵符号 DAG，再翻译成可在 kernel 里调用的 TileLang 函数。
- 理解前向函数定义（`score_mod_func_def`）与调用（`call_score_mod`）是如何由 `func_block` / `call_op` / `arg_def` 这几个 codegen helper 拼出来的。
- 理解反向梯度代码（`score_mod_backward`）并非手写，而是由 `SymbolScalar.backward` 的手动反向模式自动微分自动生成，并能解释它为什么只对部分算子生效。
- 能跟读 `attn_script/sigmoidattn.py`，解释其 `score_mod`（tanh 版 sigmoid）的反向为何能自动得到 `dscores`。

本讲是 u2 单元「符号 IR 与代码生成」的收口之一：u2-l1/u2-l2 讲了底层 `Node` 与上层 `SymbolScalar`，u2-l3/u2-l4 讲了 `generate_tl_from_dag` 如何把符号 DAG 发射成三后端代码。本讲把这些零件组装起来，回答「用户的 `score_mod` 到底是怎么变成 kernel 里一段可执行代码的」。

## 2. 前置知识

在进入源码前，先确认三个你已经（从前面讲义）建立的概念：

1. **符号 DAG（SymbolScalar）**：`SymbolScalar` 是「带形状的符号值」，它的运算符重载（`__add__`/`__mul__`/`tanh()` 等）每执行一次，就向 DAG 挂一个新节点，自身**只记录、不计算数值**。详见 u2-l2。
2. **`generate_tl_from_dag`**：对一棵以 `SymbolScalar` 为节点的 DAG 做后序遍历，逐节点用 `to_tl_op` 发射 TileLang 代码，并收集 `input_vars`（所有出现过的缓冲区名）。详见 u2-l3。
3. **`score_mod` 的签名**：`score_mod(score, custom_fwd_inputs, b, h, q_idx, kv_idx)`，对 `q@k` 得到的 scores 做**逐元素**数值变换（缩放、加偏置、过激活）。它和 `mask_mod`（按下标布尔遮蔽）是分开的两件事。详见 u1-l4。

还需要一个新概念：**反向模式自动微分（reverse-mode autodiff）**。训练时我们需要 `dL/dq`、`dL/dk`、`dL/dv`，这要求先把「上游梯度」沿计算图反向传回每一步。AttentionEngine 没有用 PyTorch/autograd，而是在符号层用**手写的 `_backward`** 完成这件事——每个算子节点自己知道「给定输出梯度，输入梯度该怎么算」。这是本讲 4.3 的核心。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `attention_engine/core/lower/lower.py` | 降级编排层。本讲主角 `lower_score_mod`（L475–L558）在这里；它也是 `lower_tl` 主流程里被调用的「三件套」之一。 |
| `attention_engine/core/codegen/common.py` | codegen helper 集合：`arg_def`、`call_op`、`func_block`、`func_def_block` 等，负责把代码片段拼成 TileLang 函数定义与调用。 |
| `attention_engine/core/transform/core.py` | `SymbolScalar` 定义，含 `op`（建图入口）与 `backward` / `_backward`（本讲用到的手动 autodiff）。 |
| `attention_engine/core/transform/graph.py` | 底层 `Node` IR。注意它有自己的 `Node._backward`，但本讲实际走的是 `SymbolScalar._backward`，二者不要混淆（详见 4.3）。 |
| `attention_engine/core/codegen/tl_gen.py` | `generate_tl_from_dag`，把符号 DAG 翻译成 TileLang 代码。 |
| `attn_script/sigmoidattn.py` | 本讲实践用的用户示例，其 `score_mod` 是 tanh 版 sigmoid。 |

## 4. 核心概念与源码讲解

### 4.1 score_mod 符号化：把用户函数「跑」成一棵 DAG

#### 4.1.1 概念说明

`lower_score_mod` 的输入是一个普通的 Python 函数 `score_mod`。但 AttentionEngine 并不会去「解析这个函数的源码」或用 AST 改写——它用的是更简单也更可靠的办法：**把符号值当作真实数据喂进去执行一遍**。

具体地说，它先用 `SymbolScalar` 造出 `scores`、`b`、`h`、`q_idx`、`kv_idx` 这几个「诱饵」符号值，再原样调用 `score_mod(scores, custom_fwd_inputs, b, h, q_idx, kv_idx)`。因为 `SymbolScalar` 重载了 `+`、`*`、`tanh()` 等运算符，用户函数体里每写一个表达式，就**自动**在 DAG 上挂一个节点。函数返回时，返回值 `scores_new` 就是这棵 DAG 的根节点。

这就把「用户用 Python 描述注意力」翻译成了「框架内部的一棵符号计算图」，全程不需要用户写任何 IR 代码。

#### 4.1.2 核心流程

```
输入: score_mod (callable), custom_fwd_inputs (CustomIO),
      lower_output (名字/形状容器), kernel_options, bwd_kernel_options

1. 造符号诱饵:
     scores    = SymbolScalar("scores", shape=[block_M, block_N])
     b,h,q_idx,kv_idx = 各造一个标量 SymbolScalar
2. 执行用户函数，记录成 DAG:
     scores_new = score_mod(scores, custom_fwd_inputs, b, h, q_idx, kv_idx)
        ↑ 这一步在 DAG 上挂出 Add/Mul/Tanh... 节点
3. 把 DAG 发射成前向 TileLang 代码（见 4.2）
4. 若需要反向（bwd_kernel_options 非 None），再走一次 autodiff（见 4.3）
```

一个要点：`scores` 的 `shape_idx` 被设成 `["block_M", "block_N"]`（即一个 query×key 的分块），这决定了后续 `to_tl_op` 生成的循环维度。

#### 4.1.3 源码精读

`lower_score_mod` 的「造诱饵 + 跑函数」两步：

[attention_engine/core/lower/lower.py:L476-L492](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L476-L492) —— 先构造 `scores`（shape 为分块尺寸 `tile_M`/`tile_N`）与四个下标符号值，再调用 `score_mod(...)` 把用户逻辑记录成符号 DAG，得到根节点 `scores_new`。

注意 `scores` 用的是 `SymbolScalar`（逐元素二维分块），而 `lower_online_func` 里同样的 scores 用的是 `SymbolicArray`（带行规约能力）——这是因为 `score_mod` 只做逐元素变换，不需要行规约，所以用更轻的 `SymbolScalar` 就够了。这是一个体现「四组件各取所需」的设计细节。

`op` 是建图的统一入口，每次运算都挂一个新节点：

[attention_engine/core/transform/core.py:L80-L103](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L80-L103) —— `SymbolScalar.op` 把底层 `Node` 包成新的 `SymbolScalar`，输出名继承自左操作数并追加 `_{count}` 后缀，`shape_idx` 默认继承左操作数（这就是逐元素广播的来源）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「调用 `score_mod` 会建出一棵 DAG」。

**操作步骤**（源码阅读型）：

1. 打开 `attn_script/sigmoidattn.py`，找到 `score_mod`（见下方链接）。
2. 在脑中（或纸上）把每行表达式翻译成 DAG 节点：
   - `score = score + softmax_bias` → `Add`
   - `score*0.5` → `Mul`
   - `.tanh()` → `Tanh`
   - `+ 1` → `Add`
   - `* 0.5` → `Mul`
3. 数一下：从输入 `scores` 到返回值，一共挂了几个节点？

**需要观察的现象 / 预期结果**：共 5 个非叶子节点（Add, Mul, Tanh, Add, Mul）。注意 `softmax_bias` 的 shape 是 `(1,)`，它会通过 `shape_idx` 的隐式广播（`"1"` 维）参与运算，但不会改变左操作数的 `[block_M, block_N]` 形状。

参考定义：

[attn_script/sigmoidattn.py:L14-L18](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py#L14-L18) —— `score_mod`：先加 `softmax_bias`，再用 `((score*0.5).tanh()+1)*0.5` 实现一个 sigmoid（即 `(tanh(x/2)+1)/2`）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `score_mod` 里的 `softmax_bias` 换成一个 shape 为 `(block_M,)` 的张量，DAG 的形状会怎样传播？

**答案**：左操作数 `score` 的 `shape_idx` 是 `[block_M, block_N]`，`op` 默认让输出继承左操作数形状，所以结果仍是 `[block_M, block_N]`；那个 `(block_M,)` 的 bias 会在 `to_tl_op` 里按维度名对齐做隐式广播（详见 u2-l3），DAG 结构本身不变。

**练习 2**：为什么 `lower_score_mod` 用 `SymbolScalar` 而不是 `SymbolicArray` 来造 `scores`？

**答案**：`score_mod` 只做逐元素变换，不需要 `get_reduce` 这类行规约；`SymbolicArray` 的额外能力（行规约）用不上，用更基础的 `SymbolScalar` 即可。

---

### 4.2 前向函数定义与调用：func_block / call_op / arg_def

#### 4.2.1 概念说明

上一步得到了一棵 DAG（`scores_new`）。但 kernel 里不能直接「执行一棵 DAG」——它需要一段**可调用的 TileLang 函数**。`lower_score_mod` 的第二步就做这件事：

1. 用 `generate_tl_from_dag([scores_new])` 把 DAG 发射成函数体代码（一串 `for i0,i1 in T.Parallel(...): ...`）。
2. 用 `func_block` 把「函数签名 + 函数体」包成一个完整的 `def score_mod(...): ...` 定义。
3. 用 `call_op` 生成一行 `score_mod(参数列表)` 调用语句，供主 kernel 在合适位置调用。

最终产出两个字符串：`score_mod_func_def`（函数定义）和 `call_score_mod`（函数调用），它们会被填进 Jinja2 模板（详见 u3-l1）。

#### 4.2.2 核心流程

```
scores_new (DAG 根)
   │  generate_tl_from_dag([scores_new])
   ├─► tl_code      : 函数体（IndentedCode，多行 TileLang）
   └─► input_vars   : dict{名字 -> SymbolScalar}，即函数形参表
         │
         │  func_block("score_mod", input_vars.values(), tl_code)
         ▼
   score_mod_func_def  :  "def score_mod(\n  scores: T.Buffer(...),\n  ...\n):\n    <函数体>"
         │
         │  call_op("score_mod", input_vars.values())
         ▼
   call_score_mod      :  "score_mod(scores, ...)"
```

`input_vars` 是关键枢纽：它由 `generate_tl_from_dag` 在后序遍历时收集，**包含了函数里出现过的所有缓冲区**（输入叶子、中间量、输出）。它既当函数的形参表，又当调用时的实参表——所以定义和调用用的是同一份名字列表，天然对齐。

#### 4.2.3 源码精读

前向定义与调用的生成：

[attention_engine/core/lower/lower.py:L491-L495](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L491-L495) —— `generate_tl_from_dag` 发射函数体并收集 `input_vars`；`func_block` 拼出函数定义；`call_op` 拼出调用语句。

三个 codegen helper 的实现都很薄：

[attention_engine/core/codegen/common.py:L11-L12](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L11-L12) —— `arg_def(arg)`：把一个 `SymbolScalar` 格式化成 TileLang 形参声明 `名字: T.Buffer(shape, dtype),`。

[attention_engine/core/codegen/common.py:L29-L30](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L29-L30) —— `call_op(func_name, args)`：把名字列表拼成 `func_name(arg0, arg1, ...)` 调用语句。

[attention_engine/core/codegen/common.py:L110-L118](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L110-L118) —— `func_block`：先调 `func_def_block` 写 `def 名字(...)`，再缩进写入函数体；若函数体为空则补一个 `pass`。

这里可以体会到 AttentionEngine 的分层哲学：`tl_gen.py` 只管「DAG → 代码行」，`common.py` 只管「代码行 → 函数定义/调用」，`lower.py` 只管「把零件按顺序拼起来」。每层都不大，组合起来却能生成完整 kernel。

#### 4.2.4 代码实践

**实践目标**：理解 `input_vars` 为什么同时充当形参表和实参表。

**操作步骤**（源码阅读型）：

1. 读 `generate_tl_from_dag`，注意它在遇到 `Var` 叶子时执行 `input_vars[x.varname] = x`（[tl_gen.py:L316-L321](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L316-L321)），且对每个发射过的非叶子节点也写入 `input_vars`（[tl_gen.py:L350-L351](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L350-L351)）。
2. 回答：对于一个像 `scores_new = (scores + bias)` 这样的单节点 DAG，`input_vars` 里会有几个条目？分别是谁？

**预期结果**：会有 3 个条目——`scores`（叶子 Var）、`bias`（叶子 Var，注意其 varname 是 `float(...)` 还是 `softmax_bias` 取决于它是 `SymbolicConst` 还是 `SymbolicTensor`）以及输出节点本身。这意味着生成的函数会把「输入和输出都列为形参」，调用时也按同样顺序传参。运行实际示例可确认具体名字（见第 5 节综合实践）。

#### 4.2.5 小练习与答案

**练习 1**：`func_block` 生成的函数定义里，形参顺序由谁决定？

**答案**：由 `input_vars.values()` 的顺序决定，而 `input_vars` 是普通 `dict`，按**首次插入顺序**（即 DAG 后序遍历中首次遇到每个名字的顺序）保留。这就是为什么「先定义后使用」的后序遍历很重要——它保证了形参顺序合理。

**练习 2**：如果 `score_mod` 函数体为空（直接 `return score` 不做任何变换），`func_block` 会生成什么？

**答案**：`generate_tl_from_dag` 对 `Var` 叶子只收集 `input_vars`、不发射代码，于是函数体为空；`func_block` 检测到 `str(func_body) == ""` 会补一行 `pass`，保证 Python 语法合法（见 `common.py` L115-L116）。

---

### 4.3 反向 autodiff 降级：SymbolScalar.backward 自动求导

#### 4.3.1 概念说明

训练需要反向。对 `score_mod` 而言，反向要回答的问题是：给定上游梯度 `dsT`（即 `dL/d(scores_new)`），求 `dscores = dL/d(scores)`。

AttentionEngine **没有调用 PyTorch 的 autograd**，而是在符号层自己实现了一套**手写的反向模式自动微分**：每个 `SymbolScalar` 节点都带一个 `_backward(grad)` 方法，它知道「给定输出梯度，如何把梯度分发给自己的输入」。从 DAG 根开始，沿着 `prev`（用户层输入链）递归向下，每一步把梯度传给上游节点，遇到一个输入被多条路径使用时就用 `Add` 累加——这就是反向模式 autodiff 的标准做法。

`lower_score_mod` 的反向分两步发射代码：

1. **重算前向（recompute）**：反向时需要用到前向的中间值（例如 tanh 的输出，因为 tanh 的导数 `1-tanh²` 依赖它）。所以先用 `generate_tl_from_dag([scores_new])` 重新生成一遍前向代码体（`score_mod_fwd_body`），把中间量算出来存在寄存器里。
2. **生成反向**：调用 `scores_new.backward(dsT)` 触发自动求导，于是输入叶子 `qkT` 上挂好了 `qkT.grad`（一棵表示 `dscores` 的新符号子树）；再用 `generate_tl_from_dag([qkT.grad])` 把这棵梯度树发射成反向代码体（`score_mod_backward`）。

这就是 FlashAttention 系列常用的「反向时重算前向」省显存技巧在符号层的体现。

#### 4.3.2 核心流程

反向模式 autodiff 的链式法则（以复合函数 `y = f_n ∘ ... ∘ f_1(x)` 为例）：

\[
\frac{dL}{dx} = \frac{dL}{dy} \cdot \prod_{i=n}^{1} f_i'(\text{中间值})
\]

反向模式从 `dL/dy` 出发，逐层左乘各步的局部导数，最终得到 `dL/dx`。每一步的「局部导数」只依赖该算子本身和它的前向输入/输出——这正是 `_backward` 能逐节点手写的依据。

以 `Tanh` 为例，其导数为：

\[
\frac{d}{dx}\tanh(x) = 1 - \tanh^2(x)
\]

于是 `Tanh._backward(grad)` 给前一个节点的梯度就是 `grad * (1 - self²)`，其中 `self` 是 tanh 的前向输出（重算前向时已算好）。

```
反向发射流程（lower_score_mod 第 3 段，bwd_kernel_options 非 None 时）:
  造 qkT(=反向的 scores 输入)、dsT(=上游梯度)
  │
  ├─ scores_new = score_mod(qkT, ...)        # 用 qkT 重建一棵干净的前向 DAG
  ├─ scores_new.backward(dsT)                # 触发 SymbolScalar 手写 autodiff
  │       → 沿 prev 递归分发梯度 → qkT.grad = dscores（一棵新符号子树）
  │
  ├─ generate_tl_from_dag([scores_new])  → score_mod_fwd_body   (重算前向)
  └─ generate_tl_from_dag([qkT.grad])    → score_mod_backward   (梯度计算)
```

#### 4.3.3 源码精读

反向降级主逻辑：

[attention_engine/core/lower/lower.py:L507-L524](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L507-L524) —— 注意 `scores_new.backward(dsT)` 这一行：它不返回值，而是把梯度**挂到** `qkT.grad` 上；随后 `generate_tl_from_dag([qkT.grad])` 把这棵梯度树翻译成反向代码体 `score_mod_backward`。

`SymbolScalar.backward` 的递归骨架与 `Node.backward` 完全同构，但操作的是用户层 `prev`：

[attention_engine/core/transform/core.py:L119-L124](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L119-L124) —— 先把 `grad` 存为 `self.grad`，调 `_backward` 把梯度分发给 `prev`，再对每个 `prev` 递归 `backward()`。

各算子的手写反向集中在 `_backward`，注意它支持的算子是**有限的**：

[attention_engine/core/transform/core.py:L126-L195](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L126-L195) —— 仅 `Add`/`Mul`/`Div`/`Tanh`/`Max`/`Log` 实现了反向，其余类型在 `else` 分支 `raise NotImplementedError`。

其中 `Tanh` 的反向正好对应上面的公式 `1 - tanh²`：

[attention_engine/core/transform/core.py:L165-L173](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L165-L173) —— `grad0 = grad - grad * self * self`，即 `grad * (1 - self²)`，`self` 是 tanh 前向输出；多路径梯度用 `+` 累加。

> **重要区分**：底层 `graph.py` 里 `Node._backward`（如 [graph.py:L158-L164](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/graph.py#L158-L164) 的 `Tanh`）绝大多数 `raise NotImplementedError`。本讲实际走的是 `SymbolScalar._backward` 这一套（u2-l1 讲的 `Node` 级反向是另一条未完成的路）。不要把两者混淆。

**一个直接后果——为什么 sigmoidattn 用 tanh 而不是 exp**：`sigmoidattn.py` 里有一段被注释掉的 `score_mod`：

[attn_script/sigmoidattn.py:L20-L21](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py#L20-L21) —— `return 1 / (1 + (-score).exp())`，这是用 `exp` 写的 sigmoid，但被注释掉了。

原因正是上面的算子支持表：`Exp`、`Neg`、`Sub` 在 `SymbolScalar._backward` 里**没有实现**，一旦走反向就会 `raise NotImplementedError`；而 `Tanh`、`Add`、`Mul`、`Div` 都支持。作者因此改用数学等价的 `sigmoid(x) = (tanh(x/2)+1)/2` 来写，这样整条反向链都能被自动微分。这是「符号 autodiff 能力边界反过来约束用户写法」的典型例子。

最后，反向输入张量的内存分配有个小细节：

[attention_engine/core/lower/lower.py:L534-L542](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L534-L542) —— 形状为 `["block_N","block_M"]`（即转置后的 scores）的输入判为 `dtype` 类型并分配到 shared 内存，其余分配到 fragment。这决定了反向 kernel 里哪些缓冲区走共享内存。

#### 4.3.4 代码实践

**实践目标**：验证「`scores_new.backward(dsT)` 会自动在 `qkT` 上挂好梯度」。

**操作步骤**（源码阅读 + 推导型）：

1. 对照 sigmoidattn 的 `score_mod`，画出前向 DAG：`qkT →(+bias)→ (*0.5)→ tanh →(+1)→ (*0.5) = scores_new`。
2. 手动套链式法则推导 `dscores = dL/d(qkT)`：
   - 设上游梯度为 `dsT = dL/d(scores_new)`。
   - 末层 `*0.5`：梯度乘 `0.5`。
   - `+1`：梯度不变。
   - `tanh`：梯度乘 `(1 - tanh²)`（这里用到重算前向得到的 tanh 输出）。
   - `*0.5`：梯度再乘 `0.5`。
   - `+bias`：梯度不变（bias 是 `(1,)` 广播，但梯度对 qkT 而言就是直通）。
3. 因此 `qkT.grad = dsT * 0.5 * (1 - tanh²) * 0.5`，这正是 `score_mod_backward` 应生成的计算。

**预期结果**：你的手推结果应与第 5 节综合实践脚本打印出的 `score_mod_backward` 代码一致——能看到形如「先重算 tanh 输出，再用 `0.5 * ... * (1 - t*t) * 0.5` 缩放 `dsT`」的语句链。

> 若暂无 GPU/未配好环境，可只做手推，并在综合实践中标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么反向之前要先 `generate_tl_from_dag([scores_new])` 重算一遍前向，而不是把前向中间量存下来？

**答案**：为了省显存（flash 风格的 recompute-in-backward）。存中间量会占用大量中间缓冲区；反向时重新算一遍前向，把中间值放在寄存器里即时使用，代价是多一次计算，换来显存的大幅下降。符号层正好对应「先发射前向体，再发射梯度体」。

**练习 2**：若用户在 `score_mod` 里用了 `.exp()`，反向会发生什么？该如何规避？

**答案**：`Exp` 在 `SymbolScalar._backward` 的 `else` 分支会 `raise NotImplementedError`，导致 `lower_score_mod` 反向阶段抛错。规避方法是用已支持反向的算子改写——例如 `exp(x)` 在数值允许时改用 `exp2(x*1.442695)`（但注意 `Exp2` 同样未实现反向），或像 sigmoidattn 那样改用 `tanh` 等价写法。根本上，需要扩展 `SymbolScalar._backward` 增加对应算子（见 u5-l7）。

**练习 3**：`qkT.grad` 是一棵什么样的对象？

**答案**：它是一个 `SymbolScalar`，其 `.code` 是一棵由 `Mul`/`Add`/`Sub` 等 `Node` 组成的符号子树（梯度表达式），可被 `generate_tl_from_dag` 当作普通 DAG 发射成代码——所以「自动求导」的产物仍是符号代码，而非数值。

---

## 5. 综合实践

把三个最小模块串起来：用一个**无需 GPU** 的小脚本，复现 `lower_score_mod` 的「符号化 → 前向函数体 → 反向梯度体」全过程，并打印每一步产物。

> 以下为**示例代码**（非项目原有文件），需要按 u1-l2 配置好 `PYTHONPATH`（指向 `attention_engine/`）后运行；运行结果可能因符号化顺序略有差异，关键看结构与公式是否吻合。若无环境，可只阅读脚本与注释理解流程。

```python
# 示例代码：复现 lower_score_mod 的核心三步（不需要 GPU）
# 前置：export PYTHONPATH=/path/to/attention_engine  （见 u1-l2）

from core.transform.core import SymbolScalar, CustomIO
from core.transform.graph import Var
from core.codegen.tl_gen import generate_tl_from_dag


# --- 复制 sigmoidattn.py 的 score_mod ---
def score_mod(score, custom_fwd_inputs, b, h, q_idx, kv_idx):
    bias = custom_fwd_inputs.input_tensors["softmax_bias"]
    score = score + bias
    score = ((score * 0.5).tanh() + 1) * 0.5
    return score


def fresh_inputs():
    """构造一组全新的符号诱饵（避免多次调用的 count 污染）。"""
    scores = SymbolScalar("scores", Var("scores"), shape_idx=["block_M", "block_N"])
    custom = CustomIO({"softmax_bias": (1,)})
    b = SymbolScalar("b", Var("b"))
    h = SymbolScalar("h", Var("h"))
    q_idx = SymbolScalar("q_idx", Var("q_idx"))
    kv_idx = SymbolScalar("kv_idx", Var("kv_idx"))
    return scores, custom, b, h, q_idx, kv_idx


# ===== 步骤 A：前向符号化 + 前向函数体（对应 4.1 + 4.2）=====
scores, custom, b, h, q_idx, kv_idx = fresh_inputs()
scores_new = score_mod(scores, custom, b, h, q_idx, kv_idx)
fwd_body, input_vars = generate_tl_from_dag([scores_new])
print("=== 前向函数体 score_mod_fwd_body ===")
print(fwd_body)
print("input_vars(形参表):", list(input_vars.keys()))

# ===== 步骤 B：反向 autodiff（对应 4.3）=====
# 反向时输入叶子换成 qkT，上游梯度是 dsT
qkT = SymbolScalar("scores", Var("scores"), shape_idx=["block_M", "block_N"])
dsT = SymbolScalar("dsT", Var("dsT"), shape_idx=["block_M", "block_N"])
scores_new_bwd = score_mod(qkT, custom, b, h, q_idx, kv_idx)  # 重建干净的前向 DAG
scores_new_bwd.backward(dsT)                                   # 触发 SymbolScalar 自动求导

print("\n=== qkT.grad（dscores）的符号表达式 ===")
print(qkT.grad.code)   # 一棵由 Mul/Add/Sub 组成的梯度子树

# 重算前向体（反向时需要 tanh 输出等中间量）
scores_new_bwd.clear_codegen() if hasattr(scores_new_bwd, "clear_codegen") else None
fwd_body_bwd, _ = generate_tl_from_dag([scores_new_bwd])
print("\n=== 反向 kernel 里的重算前向体 ===")
print(fwd_body_bwd)

# 反向梯度体
bwd_body, _ = generate_tl_from_dag([qkT.grad])
print("\n=== 反向梯度体 score_mod_backward ===")
print(bwd_body)
```

**操作步骤**：

1. 按 u1-l2 配好 `PYTHONPATH`，把上面的示例代码存为一个临时 `.py` 文件运行（不要放进项目源码目录）。
2. 阅读三段打印输出，分别对应 `score_mod_fwd_body`、重算前向体、`score_mod_backward`。
3. 对照 4.3.4 的手推结果，确认反向梯度体里确实出现了 `0.5`、`(1 - tanh²)` 等因子。

**需要观察的现象**：

- 前向体里应能看到 `fast_tanh(...)` 调用（`Tanh` 的 TileLang 发射，见 [tl_gen.py:L113-L117](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L113-L117)）。
- `qkT.grad.code` 应是一棵含 `Mul`/`Sub` 的子树，体现 `dsT * 0.5 * (1 - tanh²) * 0.5`。
- 反向梯度体里 `dsT` 会被多次复用（注意 inplace 优化对它的处理）。

**预期结果**：三段代码体在结构上与你在 4.3.4 手推的一致。若某些变量名/顺序与预期不同，是因为 `count`/`visit_count` 影响 inplace 复用命名，属正常现象，关注公式而非具体名字。

> 若运行报 `ImportError` 或 `NotImplementedError`，先确认 `PYTHONPATH` 与 u1-l2 一致；本脚本不依赖 CUDA。

## 6. 本讲小结

- `lower_score_mod` 用「喂符号诱饵执行一遍」的方式把用户 `score_mod` 跑成一棵 `SymbolScalar` DAG，无需解析源码。
- 前向产物由 `generate_tl_from_dag` + `func_block`/`call_op`/`arg_def` 拼成：一个函数定义 `score_mod_func_def` 和一行调用 `call_score_mod`，二者共用同一份 `input_vars` 名字表。
- 反向不需要手写：`scores_new.backward(dsT)` 触发 `SymbolScalar._backward` 的手写反向模式 autodiff，把梯度挂到 `qkT.grad` 上，再发射成 `score_mod_backward`。
- 反向采用 recompute-in-backward：先重算前向（拿到 tanh 等中间量），再算梯度，省显存。
- `SymbolScalar._backward` 仅支持 `Add/Mul/Div/Tanh/Max/Log`；这解释了 sigmoidattn 为何用 tanh 写 sigmoid 而非 exp（`Exp` 反向未实现）。
- 产出的 `lowerScoreModOutput` 字段（`score_mod_func_def`/`call_score_mod`/`score_mod_backward` 等）会被 `lower_tl` 收集，最终填进 Jinja2 模板（见 u3-l1）。

## 7. 下一步学习建议

- **接 u2-l6**：`online_func` 的降级 `lower_online_func` 与本讲结构高度相似（同样用 `generate_tl_from_dag` + `func_block`），但它要处理「跨块递推的行级状态」`online_rowscales`/`final_rowscales`，并分 `online_fwd`/`online_fwd_epilogue`/`forward`/`backward` 四段。建议对比本讲的「单段前向 + 单段反向」来理解 online 版为何要多出状态初始化与更新。
- **接 u3-l1 / u3-l2**：本讲产出的字符串字段是如何被 Jinja2 模板（`{{score_mod_func_def}}`、`{{score_mod_backward}}` 等占位符）消费的，以及 `lower_score_mod` 在 `lower_tl` 整条编排链里的位置。
- **若想扩展算子**：参考 u5-l7，尝试在 `SymbolScalar._backward` 里为 `Exp`/`Sub`/`Neg` 补上手写反向，并跑通一个用 `exp` 写的 score_mod。
