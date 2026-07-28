# SymbolScalar / SymbolicArray：带形状的符号值

## 1. 本讲目标

上一讲（u2-l1）我们打开了符号 IR 的最底层黑盒——`transform/graph.py` 里的 `Node` 体系。但你在用户代码里（如 `attn_script/mha.py`）看到的并不是裸 `Node`，而是带名字、带形状、带「被使用次数」的 `SymbolScalar`。本讲要把这一层讲透。

学完后你应该能够：

- 理解 `SymbolScalar` 如何通过 `__add__`/`__mul__`/`exp`/`max` 等运算符重载，在用户写 Python 表达式的同时自动构建出一棵符号 DAG；
- 掌握 `shape_idx`（形状索引）如何用一组字符串维度描述「这个符号值在 kernel 分块里的形状」，以及它如何驱动隐式广播；
- 理解 `count` / `use_list` / `allow_reuse` 这一整套簿记字段为什么存在——它们是后续代码生成里「inplace 复用」优化的依据；
- 看懂 `SymbolicArray.get_reduce("max")` 这样的行规约如何工作，并能动手用 `SymbolScalar` 复现 online softmax 的核心两行符号表达式。

## 2. 前置知识

本讲假设你已经学过 u2-l1（`Node` 图与算子）。复习三个关键点：

1. **`Node` 是「无形状、无名字、不可求值」的纯 IR 节点**。它只有 `type`（字符串，如 `"Add"`）、`inputs`（子 `Node` 列表）、`grad`。它不能运行，只用于建图、求导、翻译成代码。
2. **部分算子（`Add`/`Mul`/`Div`/`Neg`）自带 `_backward`**，其余算子反向会 `raise NotImplementedError`。
3. **`Var("名字")` 是变量叶子**，`Const(数值)` 是常量叶子**。

本讲要回答的核心问题是：用户在 `mha.py` 里写的 `m.max(scores.get_reduce("max"))` 这种「像在写普通 Python 数值运算」的代码，是怎么被「记录」成一棵可翻译的符号图的？答案就是 `SymbolScalar` 这一层包装。它和 `Node` 是两套并行结构：

- `Node.inputs`：底层 IR 的子节点列表（`Node` 列表）。
- `SymbolScalar.prev`：用户层的符号输入列表（`SymbolScalar` 列表）。

代码生成器最终遍历的是 `SymbolScalar.prev`（不是 `Node.inputs`），这一点很关键，后面会反复用到。

> 名词速查：**DAG**（有向无环图）指一棵表达式树/图；**行规约**（row reduce）指把二维块的某一维压扁（如对每行求和）；**inplace 复用**指让一个中间变量的名字直接复用其输入的名字，从而少声明一个寄存器/shared 内存。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，辅以它的消费者：

| 文件 | 作用 |
| --- | --- |
| [`attention_engine/core/transform/core.py`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py) | **本讲主角**。定义 `SymbolScalar`、`SymbolicArray`、`SymbolicColReduceArray`、`SymbolicTensor`、`SymbolicConst`、`CustomIO`。 |
| [`attention_engine/core/transform/graph.py`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/graph.py) | 底层 `Node` IR（u2-l1 已讲）。`SymbolScalar.code` 字段存的就是一个 `Node`。 |
| [`attention_engine/core/codegen/tl_gen.py`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py) | **消费者**。`generate_tl_from_dag` 遍历 `SymbolScalar` 树，`to_tl_op` 依据 `shape_idx` 生成 TileLang 循环。本讲用它说明 `count`/`allow_reuse` 的「回报」。 |
| [`attn_script/mha.py`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py) | `OnlineSoftmax.online_fwd` 是本讲实践任务要复现的真实符号表达式来源。 |

---

## 4. 核心概念与源码讲解

### 4.1 SymbolScalar：带形状、带名字、带簿记的符号标量

#### 4.1.1 概念说明

`SymbolScalar` 是 AttentionEngine 用户层（`OnlineFunc`、`score_mod`、`CustomIO`）直接操作的符号值。它对 `Node` 做了三件事增强：

1. **加名字（`varname`）**：`Node` 没有名字，而代码生成需要为每个中间值起一个变量名（`m_0`、`scores_max_0`…）。`SymbolScalar` 负责自动起名。
2. **加形状（`shape_idx`）**：`Node` 没有形状概念，而 kernel 分块循环必须有循环范围。`shape_idx` 用一组维度字符串（如 `["block_M"]`、`["block_M","block_N"]`）描述「这个值在分块里的形状」。
3. **加簿记（`count` / `use_list` / `allow_reuse` / `visit_count` / `lowered`）**：记录「这个符号被下游用了几次、是否允许被 inplace 复用、是否已经翻译过」，这是代码复用优化的依据。

换句话说：`Node` 是「纯数学 IR」，`SymbolScalar` 是「为生成 GPU 代码而准备的、带工程信息的 IR」。用户写 `a + b` 时，得到的是一个新 `SymbolScalar`，它的 `.code` 是一个 `Add` 节点，它的 `.prev` 是 `[a, b]`。

#### 4.1.2 核心流程

`SymbolScalar` 的「建图」全靠一个统一入口 `op(...)`，所有运算符重载都委托给它：

```text
用户写 a + b
   └─> 调用 a.__add__(b)
         └─> 调用 a.op(Add, [b])
               1. 把 b 这类 Python 标量包成 SymbolicConst
               2. 用各输入的 .code 构造底层 Node：  code = Add(a.code, b.code)
               3. 新建输出 SymbolScalar：
                    varname  = f"{base}_{a.count}"      # 自动起名
                    code     = 上面那个 Node
                    prev     = [a] + [b]                # 用户层输入链
                    shape_idx = a.shape_idx（默认继承）
               4. 更新簿记：a.count += 1; a.use_list.append(输出); 对 b 同理
               5. 返回输出（一个新的 SymbolScalar）
```

关键点：**每写一个运算符，就往 DAG 上挂一个新节点**。表达式 `(m - m_new).exp()` 会挂出 `Sub` → `Exp` 两个节点，链成一条 `prev` 链。`SymbolScalar` 自身**不做任何数值计算**——它只是在「记录」。

#### 4.1.3 源码精读

**构造函数**——记住这几个字段，后面全在用它们：

[`core.py:40-58`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L40-L58) 定义 `SymbolScalar.__init__`：`code` 存一个 `Node`，`prev` 存 `SymbolScalar` 列表，`shape_idx` 存维度字符串列表，`count`/`use_list`/`allow_reuse`/`lowered`/`visit_count` 是簿记，`grad` 用于反向。

```python
self.code = value              # 一个 Node
self.prev = prev               # SymbolScalar 列表（用户层输入链）
self.shape_idx = [str(i) for i in shape_idx]
self.count = 0                 # 被作为输入使用过几次
self.use_list = []             # 以本节点为输入的下游输出列表
self.allow_reuse = True        # 是否允许被 inplace 复用
self.lowered = False           # 代码生成时：是否已翻译过
self.visit_count = 0           # 代码生成时：已被几个下游访问
```

**统一建图入口 `op`**——所有运算符的真正实现：

[`core.py:80-117`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L80-L117) 完成上面流程图里的全部五步。注意三个细节：

```python
code = code(*[x.code for x in [self] + others])      # 用底层 Node 构造算子
output = self.__class__(
    f"{output_varname}_{self.count}",                 # 自动起名：base_count
    code,
    [self] + others,                                  # prev = 用户层输入链
    shape_idx)                                        # 默认继承 self 的形状
self.use_list.append(output); self.count += 1         # 维护簿记
```

- 起名规则是 `{varname}_{count}`，`count` 是「这次调用之前 self 被用过几次」。所以一个变量第一次被运算产生的输出叫 `m_0`，第二次叫 `m_1`……（`count` 在**构造输出之后**才自增，见第 105 行）。
- `self.__class__` 保证子类（`SymbolicArray`）调用 `op` 时仍返回子类实例。
- 若调用时传了 `varname_suffix`（如 `get_reduce` 传 `"max"`），名字变成 `{varname}_max_{count}`，让规约结果有可读名字。

> 小提醒：文件第 79 行有一句被注释掉的 `@plus_count` 装饰器（`# TODO: plus count bug`）。它的本意是让 `op` 被调用时**自动**给 `self` 和所有 `other` 累加 `count`，但因为「有 bug」被禁用了。现在 `count` 的累加是**手动**写在 `op` 内部的（第 105、111-112 行）。所以目前 `count` 只在「经过 `op`」时才会增加，符合预期。

**运算符重载——全是 `op` 的一行转发**：

[`core.py:213-250`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L213-L250) 把 `__add__`/`__sub__`/`__mul__`/`__truediv__`/`__neg__`、`exp`/`log`/`tanh`/`max`/`abs` 全部映射到 `op(Add/Sub/...)`：

```python
def __add__(self, other):  return self.op(Add, [other])
def __sub__(self, other):  return self.op(Sub, [other])
def __mul__(self, other):  return self.op(Mul, [other])
def exp(self):             return self.op(Exp)
def max(self, other):      return self.op(Max, [other])
```

这就是为什么用户写 `m.max(...)` 或 `(m - m_new).exp()` 会有「在写普通数值运算」的错觉——其实每一步都在往 DAG 挂节点。

**反向（手写 autodiff）**：

[`core.py:119-199`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L119-L199) 是 `SymbolScalar._backward`，按 `self.code.type` 分派：`Add` 把梯度直接传两边，`Mul` 交换相乘，`Div`/`Tanh`/`Log`/`Max` 各有公式，未实现的算子 `raise NotImplementedError`。它的形式与 u2-l1 里 `Node._backward` 几乎一致，区别在于这里的梯度本身是**新的 `SymbolScalar` 子树**（如 `grad * self.prev[1]`），而不是数值。这一段是 `lower_score_mod` 自动生成 `score_mod_backward` 的引擎，会在 u2-l5 详讲，本讲只需知道它存在、且复用了这里的 `prev`/`grad` 字段即可。

#### 4.1.4 代码实践（源码阅读 + 轻量运行）

**实践目标**：亲手用 `SymbolScalar` 跑通一个最简单的 DAG，观察 `.code`、`.prev`、`.varname`、`.count` 的变化，建立「写运算符 = 挂节点」的直觉。

**操作步骤**：

1. 按 u1-l2 的方式设置好 `PYTHONPATH`（让 `from core import ...` 能找到 `attention_engine/core`）。本实践**不需要 GPU**，只用到 `torch`/`sympy`。
2. 新建一个脚本 `play_scalar.py`（放任意目录，只要 `core` 可导入即可），写入：

```python
# 示例代码（非项目原有代码，供学习用）
from core import SymbolScalar
from core import Var

a = SymbolScalar("a", Var("a"), shape_idx=["block_M"])
b = SymbolScalar("b", Var("b"), shape_idx=["block_M"])

c = a + b          # 挂一个 Add 节点
d = c * c          # 挂一个 Mul 节点，c 被复用

for name, v in [("a", a), ("b", b), ("c=a+b", c), ("d=c*c", d)]:
    print(f"{name:8} varname={v.varname!r:12} count={v.count} "
          f"shape_idx={v.shape_idx} code={v.code} "
          f"prev={[p.varname for p in v.prev]}")
```

3. 运行 `python play_scalar.py`。

**需要观察的现象**：

- `c` 的 `varname` 形如 `a_0`（`a` 的第 0 次输出），`code` 打印为 `Add(Var("a"), Var("b"))`，`prev == ['a','b']`，`a.count` 和 `b.count` 都变成 1。
- `d = c * c` 中 `c` 被复用两次：注意 `c.count` 应为 2（`c` 作为 `d` 的左右两个输入各被记一次）。这正是 u2-l1 提到的「节点复用要靠 `count` 累加」机制在 `SymbolScalar` 层的对应。
- `d` 的 `code` 形如 `Mul(Add(...), Add(...))`——底层 `Node` 树与用户层 `prev` 链是两套并行结构。

**预期结果**（具体 `_N` 后缀以本地运行为准）：

```text
a        varname='a'         count=2 shape_idx=['block_M'] code=Var("a") prev=[]
b        varname='b'         count=1 shape_idx=['block_M'] code=Var("b") prev=[]
c=a+b    varname='a_0'       count=2 shape_idx=['block_M'] code=Add(Var("a"), Var("b")) prev=['a','b']
d=c*c    varname='a_0_0'     count=0 shape_idx=['block_M'] code=Mul(Add(Var("a"), Var("b")), Add(Var("a"), Var("b"))) prev=['a_0','a_0']
```

> 注意 `c` 和 `d` 的 `varname` 都是基于 `a` 派生的（`a_0`、`a_0_0`），因为 `op` 的起名基准是 `self.varname`（即「左操作数」的名字）。这一点容易让初学者困惑：**输出的名字继承自左操作数，而不是凭空生成**。精确的计数与命名以本地运行结果为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SymbolScalar` 不像普通 Python 对象那样在 `__add__` 里做数值加法，而是返回一个新的 `SymbolScalar`？

**参考答案**：因为 `SymbolScalar` 是**符号值**，它的职责是「记录」用户的计算意图，供后续代码生成与求导使用，而不是在构造期求出数值。做数值加法需要真实张量数据，而这里根本没有数据——只有形状元信息。所以 `__add__` 必须返回一个代表「加法结果」的新符号节点。

**练习 2**：`plus_count` 装饰器（第 18-33 行）被注释掉了。如果它生效，会对 `count` 造成什么影响？为什么作者注释掉它并标注 TODO？

**参考答案**：若生效，`op` 每次被调用时，`self` 和每个 `other` 的 `count` 会被装饰器**额外**加 1；而 `op` 内部第 105、111-112 行又**手动**加了一次。这样 `count` 会被双重计数，导致后续 inplace 复用判定（依赖 `count == visit_count`）失真。作者注释掉它、改为内部手动维护，正是为了避免重复计数——所以标注 `TODO: plus count bug`。

---

### 4.2 shape_idx：用字符串维度描述形状，并驱动隐式广播

#### 4.2.1 概念说明

`shape_idx` 是 `SymbolScalar` 区别于裸 `Node` 最实用的字段。它用一个字符串列表描述「这个符号值在 kernel 分块里的形状」：

- `["block_M"]`：一维，长度为查询分块大小 `block_M`（如 128）。
- `["block_M", "block_N"]`：二维分块，对应一个 scores 分块。
- `["1"]` 或 `[]`：标量/广播维度。

为什么用字符串而不是整数？因为 AttentionEngine 要支持**动态形状**（如 `meta_tensor("B","H","S",D)`）。维度大小在编译期可能是一个符号（`"block_M"`）或 sympy 表达式（`"2*block_M"`），不能写死成数字。`SymbolScalar.shape` 属性就用 `sympy.simplify` 把这些字符串整理成 sympy 表达式：

[`core.py:73-77`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L73-L77) 把 `shape_idx` 里的每个字符串 `sympy.simplify` 成表达式，供 `add_output_tensor` 之类的地方计算真实形状。

#### 4.2.2 核心流程：shape_idx 如何驱动隐式广播

当两个形状不同的 `SymbolScalar` 做逐元素运算时，`to_tl_op`（在 `tl_gen.py`）需要为每个操作数生成**对齐的循环下标**。规则是：

1. **循环范围**由「输出（即 `args[0]`）」的 `shape_idx` 决定：`for i0,i1 in T.Parallel(block_M, block_N):`。
2. **每个操作数的下标**按其自身 `shape_idx` 对齐到输出的维度：
   - 形状里的 `"1"` → 固定下标 `0`（即广播）。
   - 其它维度名 → 在输出的 `shape_idx` 里找到同名维度的位置 `target_i`，用 `i{target_i}`。
   - 若找不到同名维度，打印错误并退化为 `i{i}`。

举例：输出 `shape_idx=["block_M","block_N"]`，操作数 A 也是 `["block_M","block_N"]`（下标 `i0,i1`），操作数 B 是 `["1"]`（标量，下标空 → 直接当标量用）。于是生成：

```python
for i0,i1 in T.Parallel(block_M, block_N):
    out[i0,i1] = A[i0,i1] + B
```

这就是「隐式广播」——`shape_idx` 里的 `"1"` 起到了 numpy 广播规则里大小为 1 的轴的作用。

#### 4.2.3 源码精读

**广播下标推导**集中在 `to_tl_op` 的逐元素分支：

[`tl_gen.py:38-69`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L38-L69) 先为输出（`args[0]`）按其 `shape_idx` 生成下标（`"1"`→`0`，其余→`i{位置}`），再对每个后续操作数，把它自己的 `shape_idx` 逐维对齐到输出的维度列表上：

```python
idx_str = [f"i{i}" if idx != "1" else f"0"
           for i, idx in enumerate(args[0].shape_idx)]   # 输出下标
...
for _, arg in enumerate(args[1:]):
    input_idx = arg.shape_idx
    for i, idx in enumerate(input_idx):
        if idx == "1": idx_str_t.append("0"); continue    # 广播
        while idx != args[0].shape_idx[target_i]:         # 找同名维度
            target_i += 1
        idx_str_t.append(f"i{target_i}")                  # 对齐到输出下标
```

随后的循环头与赋值语句就是据此拼出来的：

[`tl_gen.py:64-84`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L64-L84) 用 `args[0].shape_idx` 拼 `T.Parallel(...)` 循环范围，并按算子类型发射 `+`/`-`/`*`/`/`/`T.max`/`T.exp2` 等赋值。

> 数值技巧预告：`Exp` 被发射成 `T.exp2(x*1.442695)`、`Log` 被发射成 `T.log2(x)*0.69314718`。这是因为 \( \exp(x) = 2^{x \log_2 e} \)，而 \( \log_2 e \approx 1.442695 \)，\( \ln 2 \approx 0.69314718 \)。GPU 上 `exp2`/`log2` 比 `exp`/`log` 快。这个技巧会在 u2-l4 多后端代码生成里展开，这里先留个印象。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：通过阅读 `to_tl_op`，预测一段广播表达式会生成什么样的 TileLang 循环。

**操作步骤**：

1. 想象这样一个逐元素运算：输出 `shape_idx=["block_M","block_N"]`，左操作数 `shape_idx=["block_M","block_N"]`，右操作数 `shape_idx=["1"]`（标量偏置），算子是 `Add`。
2. 手工模拟 [`tl_gen.py:38-84`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L38-L84) 的逻辑，写出你预期的生成代码。

**需要观察的现象 / 预期结果**（待本地验证）：

```python
for i0,i1 in T.Parallel(block_M, block_N):
    out[i0,i1] = left[i0,i1] + right
```

右操作数 `shape_idx=["1"]` → 它的下标列表只有一个 `"1"` → 映射为 `"0"`，但下标列表为 `["0"]` 时第 73-75 行会把方括号去掉（标量化），所以 `right` 以裸变量出现，实现广播。

**3.** 进阶：把右操作数换成 `shape_idx=["block_M"]`（一维行向量），再预测结果。预期它对齐到输出的第 0 维，生成 `out[i0,i1] = left[i0,i1] + right[i0]`（沿 `block_N` 广播）。

#### 4.2.5 小练习与答案

**练习 1**：`shape_idx` 用字符串（如 `"block_M"`）而非整数，最大的好处是什么？

**参考答案**：支持动态形状与符号推导。编译期维度大小可能是符号（`block_M`）或 sympy 表达式（`2*block_M`），用字符串才能在不变量化的情况下表达；整数只能表达静态形状。

**练习 2**：如果一个操作数的 `shape_idx=["block_N"]`，而输出 `shape_idx=["block_M","block_N"]`，下标会如何对齐？

**参考答案**：输出下标为 `i0,i1`（对应 `block_M,block_N`）。该操作数的 `shape_idx` 只有一个 `"block_N"`，对齐算法在输出的 `shape_idx` 里找 `block_N`，定位到位置 1，于是它的下标是 `i1`。生成 `op[i0,i1] ... other[i1]`，即沿 `block_M` 广播。

---

### 4.3 SymbolicArray / SymbolicColReduceArray：行规约与列规约

#### 4.3.1 概念说明

`SymbolicArray` 是 `SymbolScalar` 的子类，默认带二维形状 `["block_M","block_N"]`，专门用于 `OnlineFunc.online_fwd` 里那个 `scores` 分块（一个 query 分块 × kv 分块的注意力分数矩阵）。它最关键的能力是 `get_reduce`——**行规约**：把二维分块沿最后一维压扁，得到每行一个标量。

为什么需要行规约？因为 online softmax 的核心就是「每行」求最大值和求和：

- `scores.get_reduce("max")` → 每行的最大值，形状从 `["block_M","block_N"]` 降为 `["block_M"]`。
- `scores.get_reduce("sum")` → 每行的和，同样降为 `["block_M"]`。

注意区分两个相似但不同的东西（u1-l4 也强调过）：

| 写法 | 含义 | 形状变化 |
| --- | --- | --- |
| `a.max(b)`（二元 `Max`） | 逐元素取大 | 不变（同 `a.shape_idx`） |
| `a.get_reduce("max")`（`ReduceMax`） | 沿最后一维**行规约** | `shape_idx[:-1]`（降一维） |

`SymbolicColReduceArray` 则是**列规约**版本（用于 `OnlineFunc.combine`，即 split-kv 多块合并时沿 `num_split` 维做规约），它的 `get_reduce` 保留最后一维、压掉倒数第二维。

#### 4.3.2 核心流程：get_reduce 如何降维

`SymbolicArray.get_reduce(op)` 内部调用 `self.op(规约算子, shape_idx=self.shape_idx[:-1], varname_suffix=op)`：

```text
get_reduce("max")
  shape_idx = self.shape_idx[:-1]          # ["block_M","block_N"] -> ["block_M"]
  调用 self.op(ReduceMax, shape_idx=shape_idx, varname_suffix="max")
     -> 输出 SymbolScalar，varname 形如 "scores_max_0"，code = ReduceMax(self.code)
     -> 输出的 shape_idx = ["block_M"]（已降维）
```

随后 `generate_tl_from_dag` 把 `ReduceMax` 发射成 TileLang 的硬件规约原语 `T.reduce_max(out, in, dim=1, clear=True)`（注意规约分支与逐元素分支是两套独立代码）：

[`tl_gen.py:9-33`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L9-L33) 把 `ReduceSum`/`ReduceMax`/`ReduceAbsSum`（行规约，`dim=1`）和 `ColReduce*`（列规约，`dim=0`）分别发射成 `T.reduce_*`，不进入逐元素 `T.Parallel` 循环。

#### 4.3.3 源码精读

**SymbolicArray.get_reduce（行规约，去掉最后一维）**：

[`core.py:253-278`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L253-L278) 定义 `SymbolicArray`（默认 `shape_idx=["block_M","block_N"]`）及其 `get_reduce`：

```python
def get_reduce(self, op):
    shape_idx = self.shape_idx[:-1]           # 行规约：丢最后一维
    if op == "sum":  return self.op(ReduceSum,  shape_idx=shape_idx, varname_suffix="sum")
    if op == "max":  return self.op(ReduceMax,  shape_idx=shape_idx, varname_suffix="max")
    if op == "abssum": return self.op(ReduceAbsSum, shape_idx=shape_idx, varname_suffix="abssum")
```

**SymbolicColReduceArray.get_reduce（列规约，去掉倒数第二维、保留最后一维）**：

[`core.py:280-304`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L280-L304) 的 `shape_idx = self.shape_idx[:-2] + [self.shape_idx[-1]]`——压掉倒数第二维（通常是 `num_split`），保留最后一维。这就是 split-kv 合并时「沿 split 维做规约」的形状依据。

**另外两个常用子类（顺便认识）**：

- [`core.py:306-316`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L306-L316) `SymbolicTensor`：用于 `CustomIO`，把一个 tuple 形状转成 `shape_idx`。
- [`core.py:319-328`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L319-L328) `SymbolicConst`：把 Python `int`/`float` 包成常量符号（`op` 第 87-88 行遇到 Python 标量时会自动用它包装）。

**`count` / `use_list` / `allow_reuse` 的「回报」——inplace 复用优化**：

前面 4.1 我们一直在维护 `count`/`use_list`/`allow_reuse`，现在看它们在代码生成里如何被消费。`generate_tl_from_dag` 在翻译每个节点前，会判断能否把输出的变量名直接**复用**某个输入的名字（从而少声明一个寄存器）：

[`tl_gen.py:342-346`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L342-L346) 是 inplace 复用的判定核心：

```python
for i, input_item in enumerate(x.prev):
    if input_item.shape_idx == x.shape_idx:
        if (input_item.count == 1 or input_item.visit_count == input_item.count) \
           and input_item.allow_reuse:
            x.varname = input_item.varname     # 输出名字直接复用输入名字
            break
```

判读这个条件：

- `input_item.count == 1`：该输入**只被这一个下游使用**，复用它不会影响别的计算。
- 或 `input_item.visit_count == input_item.count`：该输入的所有下游都已翻译完，它已经「无人需要」，可以被安全覆盖。
- 且 `input_item.allow_reuse` 为真：用户/降级层没有显式禁止复用。

如果条件成立，就让 `x.varname = input_item.varname`，于是生成的代码里 `x` 和 `input_item` 用同一个变量，省掉一次中间内存。这正是 `count`、`visit_count`、`allow_reuse` 这一整套簿记存在的根本原因。

**「禁止复用」的真实用例**：在 `lower_online_func` 里，新生成的行级状态（`m_new`、`r_new` 等）和 `o_scale` 必须**作为独立的 kernel 输出/状态保留**，不能被 inplace 覆盖到旧值上，所以降级层会显式关掉它们的复用：

[`lower.py:351-353`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L351-L353) 在把 `online_fwd` 跑成 DAG 后，对每个新的 `online_rowscales` 和 `o_scalevar` 调用 `set_allow_reuse(False)`，保证它们在 `generate_tl_from_dag` 里不会被 alias 到输入。这一句把 4.1 的簿记字段、4.3 的 `SymbolicArray`、`lower_online_func` 三者串了起来——你会看到 `set_allow_reuse` 就是 4.1.3 里 [`core.py:60-64`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L60-L64) 那个 setter。

#### 4.3.4 代码实践（本讲核心实践：手写 online softmax 片段）

**实践目标**：用 `SymbolScalar` + `SymbolicArray` 复现 `mha.py` 里 `OnlineSoftmax.online_fwd` 的前两行符号表达式，打印每个中间结果的 `.code`、`.prev`、`.shape_idx`，亲眼看到一棵符号 DAG 是如何链式长出来的。

**操作步骤**：

1. 设置 `PYTHONPATH`（同 u1-l2，让 `core` 可导入）。本实践**不需要 GPU**。
2. 新建 `play_online.py`：

```python
# 示例代码（基于 attn_script/mha.py 的 OnlineSoftmax.online_fwd 改写，供学习用）
from core import SymbolScalar, SymbolicArray
from core import Var

# 模拟 lower_online_func 里构造的符号对象（见 lower.py:323-333）
scores = SymbolicArray("scores", Var("scores"),
                       shape_idx=["block_M", "block_N"])   # 一个 scores 分块
m = SymbolScalar("m", Var("-inf"), shape_idx=["block_M"])  # 行最大值，初值 -inf

# ====== mha.py:51-52 的核心两行 ======
m_new      = m.max(scores.get_reduce("max"))   # 每行新最大值
scale_tmp  = (m - m_new).exp()                 # 旧 m 相对新 m 的重缩放因子
# ====================================

steps = [("scores", scores),
         ("scores.get_reduce('max')", m_new.prev[1]),
         ("m", m),
         ("m_new = m.max(...)", m_new),
         ("(m - m_new)", scale_tmp.prev[0]),
         ("scale_tmp = (m-m_new).exp()", scale_tmp)]

for name, v in steps:
    print(f"{name:28} varname={v.varname!r:16} shape_idx={v.shape_idx}")
    print(f"{'':28}   code = {v.code}")
    print(f"{'':28}   prev = {[p.varname for p in v.prev]}")
```

3. 运行 `python play_online.py`。

**需要观察的现象**：

- `scores.get_reduce("max")` 的 `shape_idx` 从 `["block_M","block_N"]` **降为** `["block_M"]`（行规约去掉最后一维），`varname` 形如 `scores_max_0`。
- `m_new` 的 `shape_idx` 仍是 `["block_M"]`（`m` 与规约结果同形，逐元素 `Max` 不改形状）。
- `m_new.code` 形如 `Max(Var("-inf"), ReduceMax(Var("scores")))`——一棵嵌套的底层 `Node` 树。
- `m_new.prev == ['m', 'scores_max_0']`——用户层的输入链（注意它和 `.code.inputs` 是两套结构）。
- `(m - m_new)` 的 `code` 是 `Sub(...)`，`.exp()` 再套一层 `Exp(...)`，于是 `scale_tmp.code` 形如 `Exp(Sub(Var("-inf"), Max(Var("-inf"), ReduceMax(Var("scores")))))`。

**预期结果**（精确的 `varname` `_N` 后缀与具体打印格式以本地运行为准——待本地验证）：

```text
scores                        varname='scores'          shape_idx=['block_M', 'block_N']
scores.get_reduce('max')      varname='scores_max_0'    shape_idx=['block_M']
m                             varname='m'               shape_idx=['block_M']
m_new = m.max(...)            varname='m_0'             shape_idx=['block_M']
(m - m_new)                   varname='m_1'             shape_idx=['block_M']
scale_tmp = (m-m_new).exp()   varname='m_1_0'           shape_idx=['block_M']
```

> 名字链的规律：因为 `op` 的起名基准是「左操作数」，所以从 `m` 一路派生的中间值都带 `m` 前缀（`m_0`→`m_1`→`m_1_0`）。这是阅读生成代码时辨认「这一行属于哪条计算链」的线索。

**4.**（进阶观察）把 `m_new.prev[1]`（即规约结果）和 `scores` 比较：它们是**不同**的 `SymbolScalar` 对象，但 `m_new.prev[1].prev[0] is scores` 为真——规约节点把 `scores` 作为唯一输入挂了上去。这正是 `SymbolicArray.get_reduce` 通过 `self.op(...)` 把自己放进 `prev` 的结果。

#### 4.3.5 小练习与答案

**练习 1**：`a.max(b)` 和 `a.get_reduce("max")` 有什么区别？分别对应底层哪个 `Node`？

**参考答案**：`a.max(b)` 是逐元素二元取大（底层 `Max` 节点），形状不变；`a.get_reduce("max")` 是沿最后一维的行规约（底层 `ReduceMax` 节点），形状从 `["block_M","block_N"]` 降为 `["block_M"]`。前者比较两个同形张量的对应元素，后者把一个二维分块压成每行一个标量。

**练习 2**：`lower_online_func` 为什么要对 `new_online_rowscales` 和 `o_scalevar` 调用 `set_allow_reuse(False)`？如果不调用会怎样？

**参考答案**：这些值是 online 算法的**跨块状态**和**输出**，必须作为独立的 kernel 输出/状态变量保留，不能被 `generate_tl_from_dag` 的 inplace 优化复用（覆盖）到它们的输入变量上。若不调用 `set_allow_reuse(False)`，当某个输入恰好满足 `count==1` 等条件时，输出会被 alias 到输入名字，导致跨块递推写错地方（旧状态被提前覆盖）。`set_allow_reuse(False)` 就是一道显式的「别 inplace 我」开关。

**练习 3**：`SymbolicColReduceArray.get_reduce` 与 `SymbolicArray.get_reduce` 在 `shape_idx` 处理上有何不同？为什么？

**参考答案**：`SymbolicArray`（行规约）取 `shape_idx[:-1]`，丢最后一维（如 `["block_M","block_N"]`→`["block_M"]`）；`SymbolicColReduceArray`（列规约）取 `shape_idx[:-2]+[shape_idx[-1]]`，丢倒数第二维、保留最后一维。因为前者用于 online softmax 的「每行」规约（压掉 kv 维），后者用于 split-kv 合并时的「沿 split 维」规约（压掉 `num_split` 维而保留行维）。

---

## 5. 综合实践

把本讲三个模块串起来：手工跟踪一段真实的符号表达式，从「建图」一路走到「会生成什么 TileLang 代码」。

**任务**：考虑 `mha.py` 的 `OnlineSoftmax.online_fwd` 第 53 行 `r = r * scale_tmp`（在 4.3.4 的 `scale_tmp` 基础上继续）。请：

1. **建图**：在 4.3.4 的脚本里追加 `r = SymbolScalar("r", Var("0.0"), shape_idx=["block_M"])`，再写 `r_new = r * scale_tmp`。
2. **预测**：写出 `r_new.code`、`r_new.shape_idx`、`r_new.prev` 的预期值。
3. **推代码**：假设 `r` 与 `scale_tmp` 都是 `shape_idx=["block_M"]`，依据 [`tl_gen.py:38-84`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L38-L84)，手写 `r_new` 这一步会生成的 TileLang 循环。
4. **判复用**：`scale_tmp` 在 `r_new = r * scale_tmp` 中被用作右操作数。如果 `scale_tmp` 之后再没有别的下游、且 `allow_reuse=True`，那么翻译 `r_new` 时是否会把 `r_new.varname` 复用成 `scale_tmp.varname`？依据是 [`tl_gen.py:342-346`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L342-L346) 的哪个条件？

**参考要点**（建议先自己做完再对照）：

1. `r_new.code = Mul(...)`；`r_new.shape_idx = ["block_M"]`；`r_new.prev = [r, scale_tmp]`。
2. 生成的 TileLang 大致为：

   ```python
   for i0 in T.Parallel(block_M):
       r_new[i0] = r[i0] * scale_tmp[i0]
   ```

   （具体变量名取决于 inplace 复用是否触发。）
3. 复用判定：`scale_tmp.shape_idx == r_new.shape_idx`（都是 `["block_M"]`）成立；若 `scale_tmp.count == 1`（只被 `r_new` 用）或 `visit_count == count`，且 `allow_reuse=True`，则 `r_new.varname` 会被改成 `scale_tmp.varname`——于是生成代码里 `r_new` 和 `scale_tmp` 同名，省一次中间寄存器。这正是 4.1 那套 `count`/`visit_count`/`allow_reuse` 簿记的意义。

这个综合实践让你完整走过「用户表达式 → `SymbolScalar` DAG → `shape_idx` 广播 → TileLang 循环 → inplace 复用」全链路，为下一讲（u2-l3）打开 `generate_tl_from_dag` 的拓扑遍历做好准备。

## 6. 本讲小结

- `SymbolScalar` 是对底层 `Node` IR 的「工程化包装」：加名字（`varname`）、加形状（`shape_idx`）、加簿记（`count`/`use_list`/`allow_reuse`/`visit_count`/`lowered`）。
- 所有运算符重载（`+ - * / neg exp log tanh max abs`）都委托给统一入口 `op(...)`，它每被调用一次就往 DAG 挂一个新节点，并把名字派生为 `{左操作数名}_{count}`。
- `shape_idx` 用字符串维度（如 `"block_M"`）描述分块形状，支持动态形状；`to_tl_op` 据此生成 `T.Parallel(...)` 循环，并用 `"1"` 维实现隐式广播。
- `SymbolicArray.get_reduce` 做行规约（丢最后一维），`SymbolicColReduceArray.get_reduce` 做列规约（丢倒数第二维）；逐元素 `max(a,b)` 与规约 `get_reduce("max")` 是两回事。
- `count`/`visit_count`/`allow_reuse` 是为 `generate_tl_from_dag` 的 inplace 复用优化服务的；`set_allow_reuse(False)` 用于保护那些不能被覆盖的跨块状态（如 `online_rowscales`、`o_scale`）。
- `SymbolScalar` 有手写的 `_backward`（按 `code.type` 分派），梯度本身是新的 `SymbolScalar` 子树——这是下一阶段 `score_mod` 反向降级（u2-l5）的引擎。

## 7. 下一步学习建议

- **下一讲 u2-l3**：`generate_tl_from_dag` 的拓扑遍历与 `to_tl_op` 的逐算子发射。本讲我们只在「消费端」瞥见了它，下一讲会完整打开——你会看到 `lowered`/`visit_count` 如何驱动去重与遍历顺序、`input_vars` 如何收集、以及 inplace 复用的完整时机。
- **延伸阅读**：对照 [`attn_script/sigmoidattn.py`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py) 看一个**不使用行规约**的 online func（`OnlineIdentity`，逐元素变换、状态为空），体会「需要 `get_reduce` 的是 softmax 这类有行规约的算法；sigmoid/relu 这类逐元素激活根本不需要 `get_reduce`」。
- **承接 u2-l5**：本讲 4.1.3 提到的 `SymbolScalar._backward` 会在 u2-l5 里被 `lower_score_mod` 用来自动生成 `score_mod_backward`，届时你会看到这里的 `grad`/`prev` 字段如何拼出反向 kernel。
