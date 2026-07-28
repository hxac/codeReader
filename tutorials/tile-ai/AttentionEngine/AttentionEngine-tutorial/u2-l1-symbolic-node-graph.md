# 符号表示基础：Node 图与算子

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 AttentionEngine 为什么需要一套「符号 IR」，以及 `Node` 在其中扮演什么角色。
- 读懂 `transform/graph.py` 里 `Node` 基类、叶子节点（`Var`/`Const`）、二元/一元算子节点（`Add`/`Mul`/`Neg`/`Sub`/`Div`/`Exp`/`Log`/`Max`…）和规约算子节点（`ReduceSum`/`ReduceMax`/`ColReduce*`）的设计。
- 理解 `Node.backward` 的递归遍历 + 梯度累加机制，并能解释为什么 `(a+b)*(a+b)*c` 这种「一个节点被重复使用」的表达式需要累加。
- 准确说出当前哪些算子实现了手动反向 `_backward`（`Add`/`Mul`/`Neg`/`Div`），哪些还是 `raise NotImplementedError`，以及这对后续 `score_mod` 自动微分意味着什么。

本讲是整个 **符号 IR 与代码生成机制** 单元（u2）的地基。下一讲（u2-l2）会把这里的 `Node` 包成 `SymbolScalar`，再后面（u2-l3）才会把符号图翻译成 TileLang 代码。所以本讲的目标只有一个：把「符号节点 + 符号求导」这件事彻底弄懂。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，什么是符号计算。** 普通的 Python 表达式 `c = a + b` 会立刻算出一个数值。而「符号计算」不立即求值，而是把 `a + b` 记成一张「计算图」：节点 `Add` 指向两个叶子 `Var("a")` 和 `Var("b")`，表示「将来要做的加法」。这样我们就能在「还没有具体数值」的时候，对这张图做变换——比如求导。AttentionEngine 正是用这种方式，把用户写的 `score_mod`（一个 Python 函数）「录制」成一张符号图，再翻译成 GPU kernel。

**第二，什么是反向模式自动微分（reverse-mode autodiff）。** 给定一个输出 $L$ 和某个输入 $x$，我们想求 $\partial L/\partial x$。反向模式的思路是：从输出端给一个「上游梯度」（标量输出时通常是 1），沿着计算图**反向**传播，每经过一个算子就用链式法则把它「翻译」成对输入的梯度。链式法则：

\[
\frac{\partial L}{\partial x}=\frac{\partial L}{\partial y}\cdot\frac{\partial y}{\partial x}
\]

其中 $y$ 是 $x$ 的直接后继。AttentionEngine 之所以需要它，是因为 `score_mod` 的**反向**（求 $q/k/v$ 的梯度）要从用户写的**前向** `score_mod` 自动推导出来，而不是让用户再手写一遍反向。

**第三，为什么是「手动」反向。** PyTorch 的 autodiff 是通用的、覆盖几乎所有算子的。但 AttentionEngine 这里要的不是「跑出一个数值梯度」，而是「**生成一段反向的设备代码**」。所以作者选择了在 IR 节点层面**手写每个算子的反向规则**（用符号节点拼出梯度表达式），这样反向产物本身就是一张可被翻译成代码的符号图。本讲你会在源码里看到：只有 `Add`/`Mul`/`Neg`/`Div` 四个算子被手写了，其余算子还是 `raise NotImplementedError`——这是当前的能力边界，也是文件第一行 `# TODO: implement more op bwd` 的由来。

> 承接 u1-l4：你已经知道 `score_mod` 是逐元素分数变换、`online_func` 不支持自动微分而需手写四段方法。本讲打开「符号」这个黑盒，讲清楚最底层的 `Node` 是怎么搭起来的；`SymbolScalar`（u2-l2）和 `score_mod` 反向降级（u2-l5）都建立在本讲之上。

## 3. 本讲源码地图

本讲只涉及一个文件，但它是整条编译链的 IR 根：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `attention_engine/core/transform/graph.py` | 符号 IR 的全部节点类型 + 一套自包含的符号 autodiff 演示 | `Node` 基类、`Var`/`Const`、各算子节点、`_backward` |

它在项目里的位置可以这样理解（承接 u1-l3 的「core 四层」）：

```
用户 score_mod 函数
        │  （被框架调用、录制）
        ▼
transform 层 ── graph.py  ◀── 本讲：最底层的符号节点 Node
        │  （被 core.py 的 SymbolScalar 包装）
        ▼
transform 层 ── core.py   ◀── u2-l2：带形状的符号值
        │
        ▼
codegen 层 ── tl_gen.py / common.py   ◀── u2-l3/u2-l4：发射成代码
```

你可以用下面这条命令确认 `graph.py` 确实是 IR 根——它被 `core.py` 整体导入，也被所有降级文件和代码生成文件按需引用：

- `attention_engine/core/transform/core.py` 里：`from .graph import *`
- `attention_engine/core/lower/lower.py`、`lower_gqa.py`、`lower_decode.py`、`lower_decode_gqa.py`、`lower_cute.py`、`lower_linear.py` 以及 `attention_engine/core/codegen/tl_gen.py` 里都有：`from ..transform.graph import Var, Const`

也就是说，整个项目里凡是需要「一个符号变量」或「一个符号常量」的地方，用的都是本讲要讲的 `Var` 和 `Const`。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

1. **Node 基类与 Var/Const** —— 符号 IR 的统一骨架与两种叶子。
2. **二元与一元算子节点**（含规约算子）—— 怎么用节点表达 `+ - * / exp log max reduce…`。
3. **部分算子的手动反向 `_backward`** —— 链式法则如何被「手动」写成符号节点拼接。

### 4.1 Node 基类与 Var/Const

#### 4.1.1 概念说明

`Node` 是所有符号节点的基类。一个符号图就是一棵由 `Node` 组成的树（或更一般的 DAG，因为节点可以被复用）。每个 `Node` 只有三样核心东西：

- `type`：字符串标签，例如 `"Var"`、`"Add"`、`"Mul"`。后面 `core.py` 的 `SymbolScalar._backward` 和代码生成都靠这个字符串来分发。
- `inputs`：子节点列表（即这条计算的输入）。
- `grad`：反向传播时累积到这个节点上的梯度（也是一个 `Node`，即梯度本身是符号的）。

`Var`（变量）和 `Const`（常量）是两种**叶子节点**：它们没有输入，是计算图的输入端。`Var` 带一个名字（如 `"scores"`），`Const` 带一个数值（如 `1.0`）。`Var` 还多一个 `print_grad` 方法，方便调试时把求出的梯度打印出来。

#### 4.1.2 核心流程

`Node` 的核心是 `backward` 方法，它做两件事：

1. 如果传入了上游梯度 `grad`，就把它存到 `self.grad`（作为本节点的梯度初值/种子）。
2. 调用 `self._backward(self.grad)` 让**本算子**把自己的梯度翻译成对**各输入**的梯度（写入子节点的 `.grad`）；然后对每个子节点递归调用 `node.backward()`，把传播继续下去。

伪代码：

```
def backward(grad=None):
    if grad 非空:
        self.grad = grad          # 接收上游梯度
    self._backward(self.grad)     # 本算子把梯度分发给 inputs（具体规则由子类实现）
    for node in self.inputs:
        node.backward()           # 递归向叶子传播
```

关键点：**梯度是符号的**。`self.grad` 不是浮点数，而是一个 `Node`（一棵表达式子树）。所以反向过程不是在「算数」，而是在「拼出梯度表达式」。

> 注意：这里没有做拓扑排序，也没有「每个节点只访问一次」的去重——它就是对 `inputs` 做朴素的深度优先递归。如果一个节点被多个父节点共享（比如 `(a+b)*(a+b)` 里的 `a+b`），它会被访问多次。这能正确工作，全靠子类 `_backward` 里的**梯度累加**（见 4.3）。这种写法对小型 IR 够用，但访问量会随共享结构增长，这是后续可以优化的地方。

#### 4.1.3 源码精读

`Node` 基类（含 `backward`、`__str__`）：

[graph.py:L2-L23](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/graph.py#L2-L23)

要点：

- `backward` 里 `if grad:` 用的是真值判断——传入的 `grad` 是 `Node` 对象（恒为真）时才赋值；不传或传 `None` 时保留已有 `self.grad`。这意味着**反向传播中递归调用 `node.backward()` 时不会覆盖已经累加好的梯度**。
- `_backward` 在基类里是 `raise NotImplementedError`，强制子类自己实现。
- `__str__` 把节点渲染成形如 `Add(Var("a"), Var("b"), )` 的字符串，方便你把整张图打印出来看（注意每个输入后都带一个 `, `，所以末尾会有 `", )`，这是源码原样行为）。

`Var` 与 `Const` 两个叶子：

[graph.py:L26-L50](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/graph.py#L26-L50)

要点：

- `Var._backward` 和 `Const._backward` 都是 `pass`：叶子节点不再往下传播，梯度停在它身上。
- `Var.print_grad` 就是把 `self.grad`（求出的梯度子树）打印出来，是 `__main__` 演示用的调试钩子。
- `Var`/`Const` 是项目里被引用最多的两个类（所有 `lower_*.py` 和 `tl_gen.py` 都 `from ..transform.graph import Var, Const`），它们代表 kernel 的「输入参数」和「编译期常数」。

#### 4.1.4 代码实践

**实践目标**：亲手建一棵符号图，用 `__str__` 把它打印出来，建立「节点 = 表达式树」的直觉。

**操作步骤**（源码阅读型实践，无需 GPU）：

1. 在仓库根目录启动 Python，把 `graph.py` 所在目录加进 `PYTHONPATH`（与 u1-l2 一致）：
   ```bash
   cd /path/to/AttentionEngine
   PYTHONPATH=attention_engine python3 -c "
   from core.transform.graph import Var, Const, Add, Mul
   a = Var('a'); b = Var('b')
   expr = Mul(Add(a, b), Const(2.0))   # (a + b) * 2.0
   print(str(expr))
   "
   ```
   > 说明：`attention_engine/` 是 PYTHONPATH 根（其子目录 `core` 才是顶层包，见 u1-l3），所以这里 `from core.transform.graph import ...`。如果你直接 `python3 attention_engine/core/transform/graph.py` 跑它自带的 `__main__`，导入路径会不同——最稳的方式是把 `attention_engine` 挂到 `PYTHONPATH` 再用 `-c` 或独立脚本。

**需要观察的现象**：

- 打印出的字符串形如 `Mul(Add(Var("a"), Var("b"), ), Const(2.0), )`。
- 你能从字符串里读出这棵树的结构：根是 `Mul`，左子树是 `Add(a, b)`，右子树是 `Const(2.0)`。

**预期结果**：得到一段带尾随 `, ` 的嵌套字符串，肉眼可还原成表达式树 `(a+b)*2.0`。

> ⚠️ 注意：这些 `Node` **没有前向求值能力**（没有 `eval`/`forward`），它们只用于「建图 + 符号求导 + 翻译成代码」。不要试图 `print(expr)` 期望得到数值。

#### 4.1.5 小练习与答案

**练习 1**：`Node.backward` 里为什么用 `if grad:` 而不是 `if grad is not None:`？如果在递归过程中给某个中间节点再传一次非空 `grad`，会发生什么？

**参考答案**：因为这里的 `grad` 要么是 `None`（未传），要么是一个 `Node` 对象（恒为真）。用 `if grad:` 在「传了节点」时赋值、在「没传」时保留原值。若在递归中对中间节点再传一次非空 `grad`，会**覆盖**该节点已有的 `self.grad`——但正常调用只会对根节点传一次种子，递归时 `node.backward()` 不传参，所以不会覆盖，只会通过 `_backward` 累加到叶子。

**练习 2**：`Var` 和 `Const` 的 `_backward` 为什么都是 `pass`？

**参考答案**：它们是叶子节点，没有 `inputs`，梯度传播到此为止；它们要做的只是「把收到的梯度记在 `self.grad` 里」，这件事在进入 `_backward` 之前由 `backward` 的 `self.grad = grad`（首次）和父节点的 `_backward`（往叶子的 `.grad` 写）已经完成了，所以 `_backward` 本身无事可做。

---

### 4.2 二元与一元算子节点（含规约算子）

#### 4.2.1 概念说明

有了叶子，还需要「运算」节点来表达 `+ - * /` 以及 `exp/log/max` 等。`graph.py` 把它们分成几类：

- **二元算子**（有两个输入）：`Add`、`Sub`、`Mul`、`Div`、`Max`。
- **一元算子**（有一个输入）：`Neg`、`Exp`、`Exp2`、`Log`、`Log2`、`Tanh`、`Abs`。
- **规约算子**（把一个维度压没，有一个输入）：`ReduceSum`、`ReduceMax`、`ReduceAbsSum`，以及列方向的 `ColReduceMax`、`ColReduceSum`、`ColReduceAbsSum`。
- **辅助节点**：`MaxBwd`（为 `Max` 的反向预留的占位节点）。

每个算子节点构造时把子节点存进 `self.inputs`，并把 `type` 设成对应字符串。**前向语义由 `type` 字符串和它在代码生成阶段的处理隐式定义**——`graph.py` 本身不做前向计算，它只负责「记住结构」和「提供反向规则」。

这里要特别区分两个「max」，承接 u1-l4 里强调的「逐元素 max 与行规约」之分：

- `Max(left, right)` 是**逐元素**取大（两个同形张量按元素比），对应 `torch.maximum`。
- `ReduceMax(node)` 是**沿某个维度规约**取最大值（online softmax 里跨 KV 块维护的「行最大值」就是它），对应 `tensor.max(dim=...)`。
- `ColReduceMax(node)` 是沿**列方向**规约（与 `ReduceMax` 的行方向相对）。

这种区分在后续 online softmax 的符号化里至关重要：`m_new = m.max(scores.get_reduce("max"))` 里，对历史 `m` 和当前块 `scores` 是逐元素 `Max`，而 `scores` 自身要先做一次 `ReduceMax` 行规约。

#### 4.2.2 核心流程

构造一个算子节点的统一流程（以 `Add` 为例）：

1. `__init__` 调用 `super().__init__("Add")`，设定 `type` 标签。
2. 把左右操作数存入 `self.inputs = [left, right]`。
3. 节点就此「定型」——它的前向含义由 `type` 字符串在后续 codegen 时被翻译（如 `"Add"` → `T.add` / `+` / `torch.add`），它的反向含义由自己的 `_backward` 决定（见 4.3）。

整个 `graph.py` 里**所有算子节点的 `__init__` 都是这个套路**，差别只在 `type` 字符串、`inputs` 长度（1 或 2），以及是否实现了 `_backward`。

#### 4.2.3 源码精读

二元算子（`Add`/`Sub`/`Mul`/`Div`/`Max`）：

[graph.py:L53-L120](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/graph.py#L53-L120) 与 [graph.py:L176-L190](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/graph.py#L176-L190)

要点：

- `Add(left, right)`、`Mul(left, right)`、`Sub(left, right)`、`Div(left, right)`、`Max(left, right)` 构造方式完全一致，只是 `type` 不同。
- `Max` 还有个配套的 `MaxBwd(grad, left, right)`，它接收三个输入（梯度 + 两个被比较的 operand），是为 `Max` 反向预留的占位节点；目前它的 `_backward` 也是 `raise NotImplementedError`。

一元算子（`Neg`/`Exp`/`Exp2`/`Log`/`Log2`/`Tanh`/`Abs`）：

[graph.py:L85-L173](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/graph.py#L85-L173)

要点：

- 它们都只接收一个 `node`，`self.inputs = [node]`。
- `Exp`/`Exp2`（2 的指数）、`Log`/`Log2`（2 的对数）、`Tanh`、`Abs` 都齐备。`Exp2`/`Log2` 的存在和后续 codegen 里「把 `exp` 改写成 `exp2(x·ln2)`」的数值优化有关（详见 u2-l4）。

规约算子（`Reduce*` 与 `ColReduce*`）：

[graph.py:L195-L243](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/graph.py#L195-L243)

要点：

- 三种行规约：`ReduceSum`、`ReduceMax`、`ReduceAbsSum`（绝对值求和）。
- 三种列规约：`ColReduceMax`、`ColReduceSum`、`ColReduceAbsSum`。
- 它们的 `_backward` 全部 `raise NotImplementedError`：规约的反向规则较复杂（涉及广播回填），目前没有在 `Node` 层实现，留给上层处理。

> 一句话总结本节：`graph.py` 用「`type` 字符串 + `inputs` 列表」这一套统一结构，把十几种算子都表达成同构的节点。前向靠 codegen 翻译 `type`，反向靠各自的 `_backward`——而下一节你会看到，目前只有少数算子真正写了反向。

#### 4.2.4 代码实践

**实践目标**：把同一个表达式用不同算子节点搭出来，并通过 `type` 字符串体会「节点只记结构」。

**操作步骤**（源码阅读型实践）：

```bash
PYTHONPATH=attention_engine python3 -c "
from core.transform.graph import Var, Exp, Log, Mul, ReduceMax, Max
x = Var('x'); m = Var('m'); scores = Var('scores')
e = Exp(x)                       # exp(x)
inner = Log(e)                   # log(exp(x))
compared = Max(m, ReduceMax(scores))   # 逐元素 max(m, 行规约max(scores))
for n in [e, inner, compared]:
    print(n.type, '->', [i.type for i in n.inputs])
"
```

**需要观察的现象**：

- `Exp` 节点的 `type` 是 `"Exp"`，输入是 `[Var]`。
- `Log(Exp(x))` 的 `type` 是 `"Log"`，输入是 `[Exp]`（框架不会自动化简成 `x`）。
- `Max(m, ReduceMax(scores))` 的输入类型是 `['Var', 'ReduceMax']`——你能清楚看到「外层逐元素 `Max`」与「内层行规约 `ReduceMax`」是两个不同的节点。

**预期结果**：打印出三行 `type -> [输入type列表]`，验证「逐元素 max」和「规约 max」是两种不同节点。⸺ 若你的环境里 `attention_engine` 的 import 路径不同，请按 u1-l2 调整 `PYTHONPATH`；此脚本不依赖 GPU/torch。

#### 4.2.5 小练习与答案

**练习 1**：`Exp2` 和 `Log2` 分别表示什么运算？为什么 AttentionEngine 要专门为「2」留一对节点，而不是只用 `Exp`/`Log`？

**参考答案**：`Exp2(x)=2^x`，`Log2(x)=log_2(x)`。保留它们是因为在 GPU 上 `exp2f`/硬件指数往往比通用 `exp` 更快，codegen 会把 `exp(x)` 改写成等价的 `exp2(x·log2 e)`（即 `exp2(x·1.442695…)`）来提速（详见 u2-l4）。在 IR 层就备好 `Exp2`/`Log2`，是为了让这种数值等价改写能在符号层自然表达。

**练习 2**：`Max(a, b)` 和 `ReduceMax(a)` 有什么本质区别？各对应 PyTorch 里的什么操作？

**参考答案**：`Max(a, b)` 是**逐元素**取大，要求 `a`、`b` 同形（可广播），对应 `torch.maximum(a, b)`，输入是两个节点。`ReduceMax(a)` 是**沿某维度规约**取最大，输出形状比输入少一维，对应 `tensor.amax(dim=...)`，输入是一个节点。online softmax 中「跨块更新的行最大值」是逐元素 `Max`，而「对一个 KV 块的 scores 求块内最大」是 `ReduceMax`。

---

### 4.3 部分算子的手动反向 `_backward`

#### 4.3.1 概念说明

这是本讲最关键的一节。所谓「手动反向」，就是**作者针对每个算子，把链式法则的导数公式直接写成「用符号节点拼出梯度」的代码**。

设某算子输出为 $y$，上游传来的梯度为 $g=\partial L/\partial y$（一个 `Node`）。该算子的 `_backward(g)` 要做的事，就是把 $g$ 翻译成对每个输入 $x_i$ 的梯度 $g\cdot\partial y/\partial x_i$，并**写入** `x_i.grad`。

当前 `graph.py` **只为四个算子实现了 `_backward`**：`Add`、`Mul`、`Neg`、`Div`。其余算子（`Sub`、`Exp`、`Exp2`、`Log`、`Log2`、`Tanh`、`Abs`、`Max`、`MaxBwd` 以及全部 `Reduce*`/`ColReduce*`）都是 `raise NotImplementedError`。这就是文件第一行 `# TODO: implement more op bwd` 的含义，也是当前 Node 层 autodiff 的能力边界。

> 与生产链路的关系：实际 `score_mod` 的反向是由 `core.py` 里的 `SymbolScalar._backward` 承担的——它在 `SymbolScalar` 层重新按 `self.code.type` 分发各算子（你可以看到它对 `"Add"`/`"Mul"`/`"Div"` 的处理与本节几乎同构）。也就是说，`graph.py` 里的 Node 级 `_backward` 是这套符号 autodiff 的**自包含演示与参考实现**（由文件末尾的 `__main__` 驱动）；`SymbolScalar` 级的反向是生产路径。两者会在 u2-l2 合并讲解。本讲先把 Node 级的原理讲透。

#### 4.3.2 核心流程

先给出四个已实现算子的导数规则（这是它们 `_backward` 的数学依据）。

**Add**：$y=u+v$，则

\[
\frac{\partial y}{\partial u}=1,\quad \frac{\partial y}{\partial v}=1
\;\;\Rightarrow\;\;
\text{grad}_u=g,\;\; \text{grad}_v=g
\]

**Mul**：$y=u\cdot v$，则

\[
\frac{\partial y}{\partial u}=v,\quad \frac{\partial y}{\partial v}=u
\;\;\Rightarrow\;\;
\text{grad}_u=g\cdot v,\;\; \text{grad}_v=g\cdot u
\]

**Neg**（一元）：$y=-u$，则 $\partial y/\partial u=-1$，故 $\text{grad}_u=-g$（代码里用 `Neg(grad)` 表示）。

**Div**：$y=u/v$，则

\[
\frac{\partial y}{\partial u}=\frac{1}{v},\quad
\frac{\partial y}{\partial v}=-\frac{u}{v^2}
\;\;\Rightarrow\;\;
\text{grad}_u=\frac{g}{v},\;\; \text{grad}_v=-\frac{g\cdot u}{v^2}
\]

代码里 `grad1 = Div(Div(grad, v), v) * (-u)` 的等价写法是 $\text{grad}_v=(g/v)\cdot(-u)/v=-g\cdot u/v^2$，与上式一致。

**梯度累加**：当一个输入节点被多次使用时（如 `(a+b)*(a+b)` 里的 `a+b` 出现两次），它会被父节点多次写入梯度。每次写入前，代码用 `if self.inputs[i].grad:` 判断「是否已有梯度」，若有，就用 `Add` 把新旧梯度**加起来**：

```
grad_i = <按规则算出的新梯度>
if self.inputs[i].grad:                    # 该输入已经收过梯度
    grad_i = Add(grad_i, self.inputs[i].grad)   # 累加
self.inputs[i].grad = grad_i
```

这正是链式法则里「多路径梯度相加」的体现，也是 4.1 里「节点会被多次访问」能正确工作的根本原因。

整个反向流程（由 `Node.backward` 驱动）：

1. 根节点接收种子梯度 `g`，存入 `self.grad`。
2. 根节点 `_backward(g)` 把 `g` 翻译成对各 `inputs` 的梯度并写入（带累加）。
3. 对每个 `inputs` 递归 `backward()`，重复 2–3，直到叶子。
4. 最终每个 `Var` 的 `.grad` 就是它收到的（累加后的）符号梯度子树。

#### 4.3.3 源码精读

`Add._backward`（最简单的「直通」+ 累加）：

[graph.py:L53-L66](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/graph.py#L53-L66)

要点：两个输入都「原样」收到梯度 `g`（因为加法的导数是 1），并通过 `if self.inputs[0].grad:` 判定做累加。

`Mul._backward`（交叉相乘 + 累加）：

[graph.py:L69-L82](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/graph.py#L69-L82)

要点：`grad0 = Mul(grad, self.inputs[1])` 即 $\text{grad}_u=g\cdot v$；`grad1 = Mul(grad, self.inputs[0])` 即 $\text{grad}_v=g\cdot u$。

`Neg._backward`（取负 + 累加）：

[graph.py:L85-L94](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/graph.py#L85-L94)

要点：`grad = Neg(grad)` 表示 $\text{grad}_u=-g$。

`Div._backward`（商法则 + 累加）：

[graph.py:L106-L120](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/graph.py#L106-L120)

要点：`grad0 = Div(grad, self.inputs[1])` 即 $\text{grad}_u=g/v$；`grad1 = Div(Mul(grad0, Neg(self.inputs[0])), self.inputs[1])` 即 $\text{grad}_v=(g/v)\cdot(-u)/v=-g\cdot u/v^2$。

「未实现」的代表（`Sub` 与所有未列举的算子同构）：

[graph.py:L97-L103](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/graph.py#L97-L103)

要点：`Sub._backward` 直接 `raise NotImplementedError`。`Exp`/`Log`/`Max`/`Reduce*` 等同理——它们的前向节点存在（能建图、能被 codegen 翻译），但 Node 层**没有**反向规则。

`__main__` 自带演示（构造 `(a+b)*(a+b)*c` 并对 `a` 求梯度）：

[graph.py:L247-L258](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/graph.py#L247-L258)

要点：这正是 4.3.4 实践要复现的脚本。

#### 4.3.4 代码实践

**实践目标**：复现 `graph.py` 末尾 `__main__` 的思路，构造 $d=(a+b)\cdot(a+b)\cdot c$ 的符号 DAG，调用 `d.backward(...)`，打印 `a` 的梯度，并**用人脑求导交叉验证**链式反向是否正确。这是本讲的核心实践。

**操作步骤**（源码阅读型实践，无需 GPU）：

1. 把 `attention_engine` 挂到 `PYTHONPATH`，运行下面这段「示例代码」（它就是 `__main__` 的等价改写，并额外打印了 `a.grad` 的结构）：

   ```python
   # 示例代码：复现 graph.py 的 __main__
   from core.transform.graph import Var, Add, Mul

   a = Var("a"); b = Var("b")
   c = Add(a, b)          # c1 = a + b
   c = Mul(c, c)          # c2 = (a+b)*(a+b)
   d = Mul(c, Var("c"))   # d  = (a+b)*(a+b)*c   （注意这里的 Var("c") 是第三个变量，名为 "c"）

   d.backward(Var("d"))   # 种子梯度 = 上游符号 "d"
   a.print_grad()         # 打印 a 的符号梯度
   print(str(a.grad))     # 再以树形字符串打印一次
   ```

2. **手动推导**预期结果（这是验证的关键）：
   - 设种子 $g=d$（脚本传的是 `Var("d")`，即把 $d$ 当作上游梯度，而非标准的 1）。
   - $d=c_2\cdot c_v$，其中 $c_2=(a+b)^2$、$c_v=c$。由 `Mul` 规则：传给 $c_2$ 的梯度 $=g\cdot c_v=d\cdot c$。
   - $c_2=c_1\cdot c_1$，$c_1=a+b$。由 `Mul` 规则，$c_1$ 的**每一次出现**都收到 $(d\cdot c)\cdot c_1=d\cdot c\cdot(a+b)$。由于 $c_1$ 被用了两次，两路梯度**累加**：$a$ 收到 $d\cdot c\cdot(a+b)+d\cdot c\cdot(a+b)$。
   - 由 `Add` 规则，$a$ 与 $b$ 各收到 $c_1$ 处的同样梯度。
   - 所以 $a$ 的符号梯度子树本质等于 $d\cdot c\cdot(a+b)+d\cdot c\cdot(a+b)$，即 $2\,d\,c\,(a+b)$。

**需要观察的现象**：

- `a.print_grad()` 输出一棵以 `Add(...)` 为根的子树，形如（源码 `__str__` 会在每个输入后加 `, `）：
  ```
  a grad: Add(Mul(Mul(Var("d"), Var("c"), ), Add(Var("a"), Var("b"), ), ), Mul(Mul(Var("d"), Var("c"), ), Add(Var("a"), Var("b"), ), ), )
  ```
  即 `Add( d·c·(a+b) , d·c·(a+b) )`——两个完全相同的子树被 `Add` 累加。
- 这正是「$c_1=a+b$ 被复用两次 → 梯度相加」的直接证据。

**预期结果**：`a.grad` 的结构为两个 `Mul(Mul(Var("d"), Var("c")), Add(Var("a"), Var("b")))` 之和，化简后即 $2\,d\,c\,(a+b)$，与上面的人脑推导一致 ✅。

**进阶验证（推荐）**：把种子换成标准的 `Const(1.0)`（即标量输出对自身的导数 1），再跑一次：

```python
# 示例代码：用标准种子 1 验证雅可比
from core.transform.graph import Var, Const, Add, Mul
a = Var("a"); b = Var("b")
c = Mul(Add(a, b), Add(a, b))      # (a+b)^2
d = Mul(c, Var("c"))               # (a+b)^2 * c
d.backward(Const(1.0))
a.print_grad()
```

此时 $a$ 的梯度应化简为 $2\,c\,(a+b)$，恰好等于 $\partial d/\partial a$（因为 $d=(a+b)^2 c$，对 $a$ 求导得 $2(a+b)c$）。打印出的子树应为 `Add( Mul(Var("c"), Add(Var("a"), Var("b"))), Mul(Var("c"), Add(Var("a"), Var("b"))) )`——两个 $c\cdot(a+b)$ 相加 ✅。这就把「符号反向」和「微积分手算」对齐了。

> ⚠️ 如前所述，`Node` 不能数值求值，所以「验证」是**结构与符号层面**的：确认梯度子树化简后等于手算的导数表达式。若你修改表达式后 `a.grad` 为 `None`，通常是因为路径上遇到了 `raise NotImplementedError` 的算子（如 `Sub`/`Exp`），这正是 4.3.1 说的能力边界。

#### 4.3.5 小练习与答案

**练习 1**：用一句话解释，为什么 `Mul(c, c)`（其中 $c=a+b$）里 $a$ 最终收到的梯度是「两份相加」，而不是一份？

**参考答案**：因为 $c$ 在 `Mul` 中同时作为左操作数和右操作数出现，`Mul._backward` 会分别向 `inputs[0]` 和 `inputs[1]` 各写一次梯度，而这两次写入指向同一个 $c$ 节点；经 `Add`（$c$ 是 `Add(a,b)`）继续传到 $a$ 时，$a$ 就累加了两份相同的梯度。这正是「一个节点被复用多次，其梯度按链式法则相加」的体现。

**练习 2**：如果你把表达式改成含 `Exp`（如 `d = Mul(Exp(Var("a")), Var("b"))`）并调用 `d.backward(Const(1.0))`，会发生什么？为什么？这说明了什么限制？

**参考答案**：反向传播到 `Exp` 节点时会抛出 `NotImplementedError`，因为 `Exp._backward` 没有实现。这说明当前 `graph.py` 的 Node 级 autodiff **只支持 `Add`/`Mul`/`Neg`/`Div` 四个算子**；含 `Exp`/`Log`/`Sub`/`Max`/`Reduce*` 等的表达式无法在 Node 层自动求导。这也是文件首行 `# TODO: implement more op bwd` 待办的事项；生产中这些算子的反向由 `core.py` 的 `SymbolScalar._backward` 在更高一层处理（u2-l2）。

**练习 3**：`Div._backward` 里对分母 `inputs[1]` 的梯度是 `Div(Mul(Div(grad, inputs[1]), Neg(inputs[0])), inputs[1])`。请把它化简，并说明它等于 $-g\cdot u/v^2$。

**参考答案**：设 $g$=grad、$u$=inputs[0]、$v$=inputs[1]。表达式为 $((g/v)\cdot(-u))/v=(g\cdot(-u))/(v\cdot v)=-g\cdot u/v^2$，与商法则 $\partial(u/v)/\partial v=-u/v^2$ 乘以上游 $g$ 一致 ✅。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「迷你符号 autodiff 实验台」任务。

**任务**：用 `graph.py` 提供的节点，构造表达式 $y=\dfrac{(a+b)\cdot(a-b)}{b}$（即分子是 $(a+b)$ 与 $(a-b)$ 的乘积，分母是 $b$），其中 $a,b$ 是 `Var`。注意：$(a-b)$ 必须用已实现反向的算子来表达——`Sub` 没有反向，请用 `Add(a, Neg(b))` 来等价表示减法。然后：

1. 用 `Const(1.0)` 作为种子调用 `y.backward(...)`。
2. 打印 `a.grad` 与 `b.grad`。
3. **手算** $\partial y/\partial a$ 与 $\partial y/\partial b$，确认打印出的符号梯度子树化简后与手算一致。

**提示**：

- $y=\dfrac{(a+b)(a-b)}{b}=\dfrac{a^2-b^2}{b}$，所以 $\partial y/\partial a=2a/b$，$\partial y/\partial b$ 需用商法则或先化简再求导。
- 构造时：`num = Mul(Add(a, Neg(b)), Add(a, Neg(b)))` 不对（那是 $(a-b)^2$）；正确写法是 `num = Mul(Add(a, b), Add(a, Neg(b)))`，再 `y = Div(num, b)`。
- 验证时关注：`b` 既出现在分子 `(a-b)` 里、又作为分母，所以 `b.grad` 应当是**两路梯度之和**——这正是 4.3 强调的累加机制。

**预期**：你能看到 `b.grad` 是一个 `Add(...)` 节点（两路累加），而 `a.grad` 化简后等于 $2a/b$。通过这个任务，你会同时用到「`Var`/`Const`/算子节点建图」「`Neg` 替代 `Sub` 绕开未实现反向」「梯度累加」三件事，把本讲的知识闭环。

> ⚠️ 这个任务不依赖 GPU、不依赖 torch，纯 Python 即可运行；若 import 路径报错，按 u1-l2 设置 `PYTHONPATH=attention_engine` 并用 `from core.transform.graph import ...`。

## 6. 本讲小结

- `graph.py` 是 AttentionEngine 符号 IR 的根：所有节点都继承自 `Node`，用「`type` 字符串 + `inputs` 列表 + `grad`」这三样东西统一表达 `Var`/`Const` 及十几种算子。
- `Var`（带名字）和 `Const`（带数值）是两种叶子节点，也是项目里被引用最多的两个类（所有 `lower_*.py` 和 `tl_gen.py` 都 import 它们）。
- 算子节点分三类：二元（`Add`/`Sub`/`Mul`/`Div`/`Max`）、一元（`Neg`/`Exp`/`Exp2`/`Log`/`Log2`/`Tanh`/`Abs`）、规约（`ReduceSum`/`ReduceMax`/`ReduceAbsSum` 及列向 `ColReduce*`）。要区分逐元素 `Max` 与规约 `ReduceMax`。
- 反向传播由 `Node.backward` 驱动：接收种子 → `_backward` 把梯度翻译给各输入 → 递归向叶子传播；梯度是**符号的**（一个 `Node` 子树），不是数值。
- 当前**只有 `Add`/`Mul`/`Neg`/`Div` 实现了 `_backward`**，其余算子（含 `Sub`/`Exp`/`Log`/`Max`/全部 `Reduce*`）都是 `raise NotImplementedError`（文件首行 TODO）。
- 节点被复用时（如 `(a+b)*(a+b)`），靠 `if self.inputs[i].grad:` + `Add` 做**梯度累加**，这是链式法则「多路径相加」的直接实现，也是朴素递归能正确的关键。

## 7. 下一步学习建议

本讲你掌握的是**最底层、无形状**的符号节点。下一步：

- **u2-l2（SymbolScalar / Symbolic array）**：看 `core.py` 如何把这里的 `Node` 包成「带形状索引 `shape_idx`、带复用计数 `count`/`use_list`/`allow_reuse`」的 `SymbolScalar`，并在 `SymbolScalar` 层重新实现一套（更完整的）`_backward`。你会看到本讲的 `Add`/`Mul`/`Div` 规则在 `SymbolScalar` 层几乎同构地再现，并新增了广播与规约的表达。
- 之后再进入 **u2-l3（generate_tl_from_dag）**，看这张符号图如何被拓扑遍历、翻译成 TileLang 的 `T.Parallel` 循环代码——届时 `type` 字符串（`"Add"`/`"Mul"`/`"Exp"`…）就会被发射成真正的设备代码。

建议阅读顺序：先重读 `graph.py` 全文（它很短，只有 258 行），再打开 `core.py` 对照 `SymbolScalar.__init__` 与 `SymbolScalar.backward`，体会「从无形状 Node 到有形状 SymbolScalar」这一层抽象的动机。
