# 从 DAG 到 TileLang：generate_tl_from_dag

## 1. 本讲目标

上一篇（u2-l2）我们打开了 `SymbolScalar` 这个「带形状的符号值」黑盒：每次写 `a + b`、`x.exp()`，都会往一张符号计算图（DAG）上挂一个新节点。但节点本身还不是 GPU 能跑的代码——它只是 Python 对象。

本讲要解决的问题是：**这张符号 DAG，是怎么被翻译成 TileLang 的 `T.Parallel(...)` 循环和 `T.reduce_sum(...)` 调用的？** 桥梁就是 `attention_engine/core/codegen/tl_gen.py` 里的 `generate_tl_from_dag`。

学完本讲，你应当能够：

- 说清 `generate_tl_from_dag` 的三步核心动作：**拓扑遍历 → 输入收集 → 逐算子发射代码**。
- 读懂 `to_tl_op` 如何从 `shape_idx` 推导循环下标，并完成隐式广播对齐。
- 解释 inplace 复用优化（`count` / `visit_count` / `allow_reuse` 三个字段如何决定一个中间结果是否可以「原地写」）。
- 自己构造一个多输入的 `SymbolScalar` 表达式，调用 `generate_tl_from_dag`，看懂它吐出的 TileLang 代码。

## 2. 前置知识

本讲默认你已经读过 u2-l1（`Node` 图与算子）和 u2-l2（`SymbolScalar`）。这里只做最关键的回顾：

- **符号 DAG**：用户写的每一步运算都会产生一个新的 `SymbolScalar` 节点；节点的 `prev` 列表指向它的输入节点，形成一张有向无环图。图的「叶子」是 `Var`（变量输入）或 `Const`（常量）。
- **两套并行结构**：底层 IR `Node.inputs`（u2-l1）与用户层 `SymbolScalar.prev`（u2-l2）。本讲的代码生成器遍历的是 **`SymbolScalar.prev`** 这条用户层链。
- **`shape_idx`**：用字符串维度（如 `"block_M"`、`"block_N"`）描述张量形状，从而支持动态形状（编译期未知、运行期才确定的分块大小）。`"1"` 表示该维大小为 1（广播维）。
- **codegen 层定位**：在 core 的「transform（IR）→ codegen（发射）→ lower（降级编排）→ template（模板）」四层里，本讲处于 **codegen 层**。它只负责「把一个算子节点翻译成一段目标语言代码」，不关心这个节点从哪来、最终塞进哪个模板。

> 一个术语澄清：本讲里「发射（emit）」= 生成一段文本代码；「降级（lower）」= lower 层把用户 API 编排成一串 codegen 调用。本讲聚焦前者。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [attention_engine/core/codegen/tl_gen.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py) | **本讲主角**。`generate_tl_from_dag` 做 DAG 遍历与编排；`to_tl_op` / `to_cute_op` / `to_pytorch_op` 是三套逐算子发射器。 |
| [attention_engine/core/utils.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/utils.py) | `IndentedCode`：带缩进控制的代码拼接器，所有发射器都往它里写行。 |
| [attention_engine/core/transform/core.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py) | `SymbolScalar`：被遍历的节点对象，提供 `count`/`visit_count`/`allow_reuse`/`lowered` 等簿记字段，以及 `clear_codegen` 等复位方法。 |
| [attention_engine/core/lower/lower.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py) | 真实调用方。`lower_score_mod` / `lower_online_func` 展示了 `generate_tl_from_dag` 在工程中怎么被使用（含 `set_allow_reuse`、`clear_codegen`）。 |

---

## 4. 核心概念与源码讲解

### 4.1 DAG 拓扑遍历与输入收集

#### 4.1.1 概念说明

`generate_tl_from_dag` 接收一个「输出根节点列表」`x_list`，返回两样东西：

1. 一段 `IndentedCode`——按依赖顺序排好的 TileLang 代码；
2. 一个 `input_vars` 字典——生成代码里引用到的**所有缓冲区名字**及其 `SymbolScalar` 对象。

它的核心思想是**后序遍历（深度优先）**：要生成节点 `x` 的代码，必须先把 `x` 的所有输入（`x.prev`）的代码生成出来。这保证每一段代码里用到的变量，都已经在前面被定义过。

遍历过程中还要做两件簿记：

- **去重**：同一个节点如果在 DAG 里被多个下游引用（比如 `(a+b)` 同时被 `sum` 和 `max` 两个规约使用），它的代码**只能生成一次**。靠 `x.lowered` 标志位实现。
- **输入收集**：遍历到叶子 `Var` 时，把它登记进 `input_vars`（以及另一个 `inputs` 字典），这样上层 `lower` 才知道这段函数需要声明哪些参数。

#### 4.1.2 核心流程

用伪代码描述 `generate_tl_from_dag` 的骨架：

```
generate_tl_from_dag(x_list, to_tl=True, to_cute=False, ...):
    input_vars = {}        # 所有被引用的缓冲区名
    inputs    = {}        # 仅叶子 Var（return_inputs=True 时才返回）

    def generate_tl(x):               # 对单个节点
        if x.lowered:                 # ① 已生成过 → 跳过（去重）
            return 空
        if x.code 是 Var:             # ② 叶子变量
            input_vars[x.varname] = x
            inputs[x.varname]    = x
            return 空                # 叶子无需发射代码
        if x.code 是 Const:           # ③ 常量
            return 空

        for input_item in x.prev:     # ④ 后序：先递归所有输入
            generate_tl(input_item)
        for input_item in x.prev:     # ⑤ 累加访问计数（供 4.3 复用判定）
            input_item.visit_count += 1

        尝试 inplace 复用（见 4.3）    # ⑥ 可能改写 x.varname
        if 指定了输出名:
            x.varname = 输出名         # ⑦ 强制重命名
        if x.varname 不在 input_vars:
            input_vars[x.varname] = x # ⑧ 登记本节点缓冲区

        code += 发射器(x.code.type, x, *x.prev)  # ⑨ 逐算子生成（to_tl_op 等）
        x.lowered = True              # ⑩ 标记已生成
        return code

    for x in x_list:                  # 对每个输出根
        code += generate_tl(x)
    return code, input_vars
```

注意两个容易混淆的点：

- `input_vars` 不只收集叶子！**每个计算节点的 `varname` 都会被登记**（第 ⑧ 步），因为生成代码里会引用这些中间缓冲区名（它们最终会被声明为 fragment/shared 张量）。真正「只收集叶子」的是 `inputs`。
- 输出根可以传多个（`x_list` 是列表），这对应一次发射多个相关输出（例如 online softmax 同时输出 `scores_new` 和若干 `online_rowscales`）。

#### 4.1.3 源码精读

入口签名与全局容器：

[attention_engine/core/codegen/tl_gen.py:306-310](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L306-L310) —— `generate_tl_from_dag` 接收输出根列表与三个后端开关，初始化 `input_vars`、`inputs` 两个字典。

[attention_engine/core/codegen/tl_gen.py:314-323](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L314-L323) —— 去重与叶子处理。`x.lowered` 为真就直接返回空（去重）；`Var` 叶子登记进两个字典后返回空（叶子不发射代码）；`Const` 同样不发射。

[attention_engine/core/codegen/tl_gen.py:326-334](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L326-L334) —— 后序递归：先对 `x.prev` 里每个输入调用 `generate_tl`，再给每个输入的 `visit_count` 加 1。这里遍历的是 **`SymbolScalar.prev`**（用户层链），而非底层 `Node.inputs`。

[attention_engine/core/codegen/tl_gen.py:350-359](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L350-L359) —— 登记本节点缓冲区名，再按 `to_tl`/`to_cute`/默认三选一调用对应发射器（`to_tl_op` / `to_cute_op` / `to_pytorch_op`），最后置 `x.lowered = True`。

[attention_engine/core/codegen/tl_gen.py:362-370](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L362-L370) —— 对 `x_list` 里每个根节点调用 `generate_tl`；支持用 `output_var_name_list` 强制重命名每个输出的 `varname`，以及用 `return_inputs=True` 额外返回叶子字典 `inputs`。

**真实调用方**——看 `lower_score_mod` 怎么用它：

[attention_engine/core/lower/lower.py:492-495](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L492-L495) —— 把用户的 `score_mod` 跑成符号 DAG（得到 `scores_new` 这个根），再 `generate_tl_from_dag([scores_new])` 取回代码与 `input_vars`；随后 `input_vars.values()` 被直接当作函数参数列表塞进 `func_block`，拼出 `def score_mod(...):` 的函数体。这正是 `input_vars`「收集所有缓冲区名」的用途。

#### 4.1.4 代码实践

> 实践目标：构造一个多输入表达式 `(a+b)*c` 并加两次行规约，调用 `generate_tl_from_dag`，观察生成的 `T.Parallel` 循环与 `T.reduce_sum`/`T.reduce_max` 调用，并解释 `input_vars` 收集了哪些变量。

操作步骤（**示例代码**，非项目原有文件）：

```python
# 前提：已按 README 执行
#   export PYTHONPATH="$(pwd)/attention_engine:$(pwd)/3rd_parties/tilelang:$PYTHONPATH"
from core.transform.core import SymbolicArray
from core.transform.graph import Var
from core.codegen.tl_gen import generate_tl_from_dag

# 叶子输入：shape_idx 用分块维度字符串描述动态形状（2D 分块）
a = SymbolicArray("a", Var("a"), shape_idx=["block_M", "block_N"])
b = SymbolicArray("b", Var("b"), shape_idx=["block_M", "block_N"])
c = SymbolicArray("c", Var("c"), shape_idx=["block_M", "block_N"])

# (a + b) * c —— 每个运算符都向 DAG 挂一个新节点
expr = (a + b) * c

# 行规约：丢掉最后一维 block_N，结果是 [block_M] 形状
row_sum = expr.get_reduce("sum")   # 底层 ReduceSum
row_max = expr.get_reduce("max")   # 底层 ReduceMax

# 把两个输出根一起喂给生成器
tl_code, input_vars = generate_tl_from_dag([row_sum, row_max])

print(tl_code)
print("input_vars keys:", list(input_vars.keys()))
```

需要观察的现象与预期结果（**以下输出由阅读源码逻辑推导得出，请本地运行确认**）：

1. 应当生成两段 `for i0, i1 in T.Parallel(block_M, block_N):` 循环，分别对应 `Add` 与 `Mul`。
2. 之后是两行 `T.reduce_sum(...)` 与 `T.reduce_max(...)`，分别对应 `row_sum` 与 `row_max`。
3. `expr` 这个中间节点虽然同时被 `row_sum` 和 `row_max` 引用，但它的乘法代码**只出现一次**——这就是 `lowered` 去重在起作用。
4. `input_vars` 不只有 `a/b/c` 三个叶子，还包含两个规约结果的缓冲区名。

推导出的预期输出大致如下（缩进为 4 空格）：

```python
for i0, i1 in T.Parallel(block_M, block_N):
    a[i0, i1] = a[i0, i1] + b[i0, i1]
for i0, i1 in T.Parallel(block_M, block_N):
    a[i0, i1] = a[i0, i1] * c[i0, i1]
T.reduce_sum(a, a_0_0_sum_0,dim=1)
T.reduce_max(a, a_0_0_max_1,dim=1, clear=True)
```

`input_vars keys: ['a', 'b', 'c', 'a_0_0_sum_0', 'a_0_0_max_1']`

> 疑点预告：为什么加法写成 `a[i0,i1] = a[i0,i1] + b[i0,i1]`（左边也是 `a`）？中间变量名为什么消失了？这正是下一节（4.3）要讲的 inplace 复用。如果你暂时觉得奇怪，先记住这个现象。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `generate_tl_from_dag([row_sum, row_max])` 改成只传 `[row_sum]`，生成的代码会变少吗？`expr`（乘法）那段还会生成吗？

> **答案**：会变少——`row_max` 那行 `T.reduce_max` 不再生成。但 `expr` 的乘法代码**仍然生成**，因为 `row_sum` 依赖 `expr`。这印证了「生成范围由输出根的可达性决定」。

**练习 2**：`input_vars` 和 `inputs`（`return_inputs=True` 时第三个返回值）有什么区别？

> **答案**：`input_vars` 收集**所有被引用的缓冲区名**（叶子 + 中间结果 + 输出），用于声明函数参数；`inputs` 只在 `Var` 分支登记，仅含**真正的叶子输入**。lower 层需要前者来生成 `def func(...)` 的形参列表。

---

### 4.2 to_tl_op：索引推导与隐式广播对齐

#### 4.2.1 概念说明

`to_tl_op(type, *args)` 负责把**一个算子节点**翻译成一段 TileLang 代码。其中 `args[0]` 恒为输出节点 `x`，`args[1:]` 是它的输入（即 `x.prev`）。

它要解决的核心难题是：**不同输入可能有不同的 `shape_idx`，如何对齐下标？** 例如 online softmax 里有 `(scores - m_new)`，其中 `scores` 形状是 `["block_M","block_N"]`，而行最大值 `m_new` 形状只是 `["block_M"]`——后者要在 `block_N` 维上「广播」。`to_tl_op` 靠**按维度名字匹配**来完成这件事，不依赖数值形状。

算子分两大类：

- **规约类**（`ReduceSum`/`ReduceMax`/`ColReduce*`）：发射成一行 `T.reduce_sum`/`T.reduce_max(..., dim=...)`，没有循环。
- **逐元素类**（`Add`/`Sub`/`Mul`/`Div`/`Exp`/`Log`/`Max`/`Tanh`/`Abs`/`MaxBwd` 等）：发射成一个 `for ... in T.Parallel(...):` 循环，循环体是一行赋值。

#### 4.2.2 核心流程

逐元素算子的索引对齐算法（伪代码）：

```
# 输出 args[0].shape_idx 作为「目标形状」，如 ["block_M","block_N"]
loop_str = join(args[0].shape_idx, ",")        # → "block_M,block_N"

# 输出本身的下标：每个非 "1" 维 → i0,i1,...
idx_out = [ f"i{i}" if dim != "1" else "0"
            for i, dim in enumerate(args[0].shape_idx) ]

for 每个输入 arg in args[1:]:
    target_i = 0
    for i, dim in enumerate(arg.shape_idx):
        if dim == "1":
            idx = "0"                 # 广播维 → 固定下标 0
        else:
            # 在目标 shape_idx 里向右扫描，找到名字相同的维
            while target_i < len(args[0].shape_idx) and dim != args[0].shape_idx[target_i]:
                target_i += 1
            idx = f"i{target_i}"      # 对齐到目标的第 target_i 个循环变量
        记录 idx
    # 拼成 "i0,i1" 这样的下标串

emit:  for idx_out in T.Parallel(loop_str):
           out[idx_out] = 输入0[idx0] <op> 输入1[idx1]
```

关键直觉：**维度靠名字对齐**。只要输入的某维名字（如 `"block_M"`）能在输出 `shape_idx` 里找到，就复用同一个循环变量；名字为 `"1"` 或在输出里找不到的维，退化为固定下标 `0`，从而实现广播。

#### 4.2.3 源码精读

规约类发射（行调用，无循环）：

[attention_engine/core/codegen/tl_gen.py:9-17](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L9-L17) —— `ReduceSum` 发射 `T.reduce_sum(输入, 输出, dim=1)`；`ReduceMax` 发射 `T.reduce_max(输入, 输出, dim=1, clear=True)`。注意参数顺序：`args[1]` 是被规约的输入，`args[0]` 是输出。`clear=True` 表示规约前先把目标缓冲区清零。

[attention_engine/core/codegen/tl_gen.py:22-33](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L22-L33) —— 列规约（`ColReduce*`）只是把 `dim` 换成 `0`，用于 `OnlineFunc.combine` 的跨 split 合并。

逐元素类的索引推导主体：

[attention_engine/core/codegen/tl_gen.py:38-69](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L38-L69) —— 先算输出下标 `idx_str`（第 39-42 行），再对每个输入做名字匹配对齐（第 44-62 行），最后用 `loop_str` 发射 `for ... in T.Parallel(...):`（第 67-69 行）。第 52-59 行的 `while` 就是「在目标 shape_idx 里向右扫描找同名维」。

[attention_engine/core/codegen/tl_gen.py:73-75](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L73-L75) —— 标量保护：若某输入下标串为空（即 `shape_idx==[]` 的纯标量），不加 `[]`，直接裸用变量名。

各算子的赋值模板，挑三个有代表性的：

[attention_engine/core/codegen/tl_gen.py:81-84](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L81-L84) —— `Add`：`out[i,j] = a[i,j] + b[i,j]`。

**数值技巧：为什么 `Exp` 发射成 `exp2`？** GPU 上 `exp2f`（2 的幂）通常比 `expf`（e 的幂）快。利用恒等变换：

\[
\exp(x) = 2^{\,x \cdot \log_2 e} = 2^{\,x \cdot 1.442695\ldots}
\]

所以 `T.exp2(x * 1.442695)` 与 `exp(x)` 数值等价。对数同理：

\[
\ln(x) = \log_2(x) \cdot \ln 2 = \log_2(x) \cdot 0.69314718\ldots
\]

[attention_engine/core/codegen/tl_gen.py:89-92](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L89-L92) —— `Exp` 发射 `T.exp2(x*1.442695)`。

[attention_engine/core/codegen/tl_gen.py:105-108](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L105-L108) —— `Log` 发射 `T.log2(x) * 0.69314718`。

> 对比预告：CuTe 后端的 `to_cute_op` 用的是 C 函数 `exp2f` / `__logf`；PyTorch 后端的 `to_pytorch_op` 用 `torch.exp` / `torch.log`（不换底，因为 PyTorch 参考实现只用来对齐数值，不追求速度）。三种后端共享同一套符号 DAG，只是发射语言不同（详见 u2-l4）。

#### 4.2.4 代码实践

> 实践目标：观察隐式广播如何把不同 `shape_idx` 的输入对齐到同一组循环变量。

**示例代码**：

```python
from core.transform.core import SymbolicArray, SymbolScalar
from core.transform.graph import Var
from core.codegen.tl_gen import generate_tl_from_dag

# scores: 2D 分块 [block_M, block_N]
scores = SymbolicArray("scores", Var("scores"), shape_idx=["block_M", "block_N"])
# m_new: 行向量 [block_M] —— 需在 block_N 维广播
m_new   = SymbolScalar("m_new",   Var("m_new"),   shape_idx=["block_M"])
# scale: 纯标量 shape_idx=[]
scale   = SymbolScalar("scale",   Var("scale"),   shape_idx=[])

expr = (scores - m_new) * scale

code, _ = generate_tl_from_dag([expr])
print(code)
```

需要观察的现象与预期结果（**由源码逻辑推导，请本地运行确认**）：

- `scores` 被索引为 `[i0, i1]`；
- `m_new`（形状 `[block_M]`）被索引为 `[i0]`——它没有 `block_N` 维，所以在该维上广播；
- `scale`（标量）**没有任何下标**，裸用变量名。

预期输出：

```python
for i0, i1 in T.Parallel(block_M, block_N):
    scores[i0, i1] = scores[i0, i1] - m_new[i0]
for i0, i1 in T.Parallel(block_M, block_N):
    scores[i0, i1] = scores[i0, i1] * scale
```

> 说明：两行左侧都是 `scores`——这是 4.3 的 inplace 复用把中间名 `scores_0`/`scores_0_0` 折叠回了叶子名（`Sub` 复用 `scores`，`Mul` 又复用 `Sub` 的结果）。本节请把注意力放在**右操作数的下标**上：`m_new[i0]` 是广播对齐的结果，裸 `scale` 是标量处理的结果。这两段逻辑你能在 [tl_gen.py:44-62](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L44-L62) 的 `while` 扫描与 `"1"→0` 分支里一一找到。

#### 4.2.5 小练习与答案

**练习 1**：如果把上面例子里 `m_new` 的 `shape_idx` 改成 `["block_N"]`（列向量而非行向量），生成的下标会变成什么？

> **答案**：`m_new` 会被索引为 `[i1]`（对齐到 `block_N` 对应的循环变量 `i1`），即 `(scores - m_new)` 变成 `scores[i0,i1] - m_new[i1]`。维度名字决定对齐位置，与维度在 `shape_idx` 里的下标无关。

**练习 2**：`to_tl_op` 对 `Max`（逐元素取大）和 `ReduceMax`（行规约取大）会生成完全不同的代码。请说明各自的产物。

> **答案**：`Max` 走逐元素分支，生成 `for ... in T.Parallel(...): out[i,j] = T.max(a[i,j], b[i,j])`；`ReduceMax` 走规约分支，生成一行 `T.reduce_max(输入, 输出, dim=1, clear=True)`，没有循环。两者的区别正是 u2-l2 强调的「逐元素 max」与「行规约 get_reduce」之分。

---

### 4.3 inplace 复用优化

#### 4.3.1 概念说明

回到 4.1 留下的疑点：为什么 `(a+b)*c` 生成的代码里，中间结果名消失了，变成了 `a = a + b`、`a = a * c`？

这是 `generate_tl_from_dag` 的 **inplace（原地）复用优化**：如果一个中间缓冲区「只用一次」，就不再分配新缓冲区，而是直接把计算结果写回它的某个输入缓冲区里——省掉一次寄存器/共享内存分配。

判定一个输入 `input_item` 能否被当前节点 `x` 原地复用，依赖三个字段：

| 字段 | 含义 | 定义位置 |
| --- | --- | --- |
| `count` | 该节点作为输入被下游引用的**总次数**（在 `op` 里每次被用作输入就 +1） | [core.py:48](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L48) |
| `visit_count` | 本次遍历中该节点**已被访问次数**（生成其下游时 +1） | [core.py:54](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L54) |
| `allow_reuse` | 是否**允许**被复用（默认 `True`，可用 `set_allow_reuse(False)` 关闭） | [core.py:51](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L51) |

#### 4.3.2 核心流程

复用判定（伪代码，发生在后序递归之后、发射之前）：

```
for input_item in x.prev:
    if input_item.shape_idx != x.shape_idx:
        continue                          # 形状不同不能复用（下标对不上）
    if (input_item.count == 1             # 只被引用一次：用完即弃，安全
         or input_item.visit_count == input_item.count)   # 或这是最后一次引用
       and input_item.allow_reuse:        # 且未被显式保护
        x.varname = input_item.varname    # 输出名改成输入名 → 原地写
        break
```

直觉：

- **`count == 1`**：这个输入只被当前节点使用，没有别人会再读它，覆盖掉也无所谓 → 可复用。
- **`visit_count == count`**：虽然被多次引用，但当前已经是最后一次访问，之后没人再读 → 也可复用。
- **`allow_reuse == False`**：上层显式声明「这个缓冲区不能被覆盖」。典型场景是 online 算法里**跨 KV 块长期持有的状态**（如行最大值 `m`、累加输出 `o`），它们在每个块里都要被反复读写，绝不能被某次中间计算覆盖。

#### 4.3.3 源码精读

复用判定本体：

[attention_engine/core/codegen/tl_gen.py:342-348](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L342-L348) —— 遍历 `x.prev`，找到第一个满足「形状相同 且 (count==1 或 visit_count==count) 且 allow_reuse」的输入，把 `x.varname` 改成它的 `varname`；若调用方传了 `output_var_name_list` 则进一步强制覆盖（第 347-348 行）。改完 `varname` 后，第 350-351 行的登记就会命中已有条目，不会重复新增缓冲区。

关闭复用的开关：

[attention_engine/core/transform/core.py:60-64](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L60-L64) —— `set_allow_reuse(False)` 把节点标记为不可复用。

**真实用法一：保护跨块状态。** 在 `lower_online_func` 里，online 算法递推得到的新状态 `new_online_rowscales` 和 `o_scalevar` 必须保留独立缓冲区，所以在调用生成器前显式关闭复用：

[attention_engine/core/lower/lower.py:351-355](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L351-L355) —— 对每个 `new_online_rowscales[k]` 与 `o_scalevar` 调用 `set_allow_reuse(False)`，然后才 `generate_tl_from_dag(...)`。

**真实用法二：两次复用同一棵 DAG。** online 前向（`online_fwd`）与收尾（`online_fwd_epilogue`）共享同一批 `online_rowscales` 节点。第二次调用生成器前，必须把上次的 `lowered`/`visit_count` 复位，否则所有节点都会被跳过：

[attention_engine/core/transform/core.py:205-211](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L205-L211) —— `clear_visit` 把 `visit_count` 归零、`lowered` 置假；`clear_codegen` 额外把 `count`/`use_list` 也清掉。

[attention_engine/core/lower/lower.py:375-381](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L375-L381) —— 在生成 epilogue 代码前，对每个 `online_rowscales[k]` 调用 `clear_codegen()`，随即用同一批节点再跑一次 `generate_tl_from_dag`。同理，反向阶段对 `qkT` 也会 `clear_codegen()` 后重新生成（[lower.py:435](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L435)）。

#### 4.3.4 代码实践

> 实践目标：对比 `allow_reuse=True`（默认）与 `False` 时生成代码的差异，直观感受 inplace 复用省掉了哪些中间缓冲区。

**示例代码**（用函数封装，每次构造全新节点，避免 `lowered` 标志互相污染）：

```python
from core.transform.core import SymbolicArray
from core.transform.graph import Var
from core.codegen.tl_gen import generate_tl_from_dag

def build(allow_reuse):
    a = SymbolicArray("a", Var("a"), shape_idx=["block_M", "block_N"])
    b = SymbolicArray("b", Var("b"), shape_idx=["block_M", "block_N"])
    c = SymbolicArray("c", Var("c"), shape_idx=["block_M", "block_N"])
    a.set_allow_reuse(allow_reuse)
    tmp = a + b
    tmp.set_allow_reuse(allow_reuse)   # 关键：中间节点也要保护，否则 Mul 会复用 tmp
    expr = tmp * c
    return expr.get_reduce("sum"), expr.get_reduce("max")

print("===== allow_reuse=True（默认，原地复用）=====")
s, m = build(True)
code, iv = generate_tl_from_dag([s, m])
print(code)
print("缓冲区名:", list(iv.keys()))

print("===== allow_reuse=False（禁用复用）=====")
s, m = build(False)
code, iv = generate_tl_from_dag([s, m])
print(code)
print("缓冲区名:", list(iv.keys()))
```

需要观察的现象与预期结果（**由源码逻辑推导，请本地运行确认**）：

- `allow_reuse=True`：`Sub` 复用 `a`、`Mul` 又复用 `Sub` 的结果，两层中间名都折叠回叶子 `a`，代码变成 `a = a + b` / `a = a * c`；缓冲区名集合较短（5 个）。
- `allow_reuse=False`：每一步都分配独立缓冲区（`a_0`、`a_0_0`），代码读起来更「直白」，但缓冲区更多（7 个）。

`allow_reuse=True` 预期输出（与 4.1.4 一致）：

```python
for i0, i1 in T.Parallel(block_M, block_N):
    a[i0, i1] = a[i0, i1] + b[i0, i1]
for i0, i1 in T.Parallel(block_M, block_N):
    a[i0, i1] = a[i0, i1] * c[i0, i1]
T.reduce_sum(a, a_0_0_sum_0,dim=1)
T.reduce_max(a, a_0_0_max_1,dim=1, clear=True)
```

`缓冲区名: ['a', 'b', 'c', 'a_0_0_sum_0', 'a_0_0_max_1']`

`allow_reuse=False` 预期输出：

```python
for i0, i1 in T.Parallel(block_M, block_N):
    a_0[i0, i1] = a[i0, i1] + b[i0, i1]
for i0, i1 in T.Parallel(block_M, block_N):
    a_0_0[i0, i1] = a_0[i0, i1] * c[i0, i1]
T.reduce_sum(a_0_0, a_0_0_sum_0,dim=1)
T.reduce_max(a_0_0, a_0_0_max_1,dim=1, clear=True)
```

`缓冲区名: ['a', 'b', 'c', 'a_0', 'a_0_0', 'a_0_0_sum_0', 'a_0_0_max_1']`

对比两组输出，你能清楚地看到：复用优化把 7 个缓冲区名压成了 5 个，把两段 `out = in0 op in1` 压成了 `a = a op b` / `a = a op c`。

> **关键细节**：`set_allow_reuse` 保护的是「**这个节点会不会被它的父节点复用**」，而不是「这个节点会不会复用别人」。所以光关闭叶子 `a` 还不够——中间节点 `tmp = a+b` 由 `op` 创建时 `allow_reuse` 默认为 `True`，`Mul` 仍会复用它。上面代码里必须对 `tmp` 也调用 `set_allow_reuse(False)`，才能得到完全展开的 7 名版本。这也解释了为什么 `lower_online_func` 要对**每一个** `new_online_rowscales[k]` 逐个调用 `set_allow_reuse(False)`。

#### 4.3.5 小练习与答案

**练习 1**：在 `lower_online_func` 里，为什么 `new_online_rowscales` 必须 `set_allow_reuse(False)`，而普通 `score_mod` 里的中间 `scores` 却不需要？

> **答案**：`online_rowscales`（如行最大值 `m`、log-sum-exp）是**跨 KV 块递推**的长期状态——每个块都要读旧值、写新值，若被某次中间计算原地覆盖，下一个块就读不到正确状态。`score_mod` 里的中间 `scores` 是**一次性**的逐元素结果，用完即弃，复用反而省内存。所以前者显式保护，后者默认放行。

**练习 2**：如果把同一个 `expr` 节点连续两次喂给 `generate_tl_from_dag([expr])`（不调用 `clear_codegen`），第二次会得到什么？为什么？

> **答案**：第二次会得到**空代码**。因为第一次结束时已置 `expr.lowered = True`，第 314 行的 `if x.lowered: return` 直接跳过。这就是 lower 层在两次复用同一批节点前必须调用 `clear_codegen()` 的原因。

---

## 5. 综合实践

把本讲三个模块串起来：**手工模拟一次「online softmax 片段」的代码生成。**

参考 mha.py 里 `OnlineSoftmax.online_fwd` 的核心两步——求行最大值、再用它重缩放（这里仅取符号片段，不要求跑通完整注意力）：

1. 用 `SymbolicArray` 构造 `scores`（形状 `["block_M","block_N"]`）和行最大值状态 `m`（形状 `["block_M"]`）。
2. 写出 `m_new = m.max(scores.get_reduce("max"))`：先用 `get_reduce` 做行规约，再与 `m` 做逐元素 `max`。注意 `m` 跨步递推，应当 `m.set_allow_reuse(False)`。
3. 写出 `scale_tmp = (m - m_new).exp()`：观察 `(m - m_new)` 两个同形 `["block_M"]` 输入如何对齐，以及 `exp` 如何发射成 `exp2(...*1.442695)`。
4. 调用 `generate_tl_from_dag([m_new, scale_tmp])`，打印代码。

完成后，请回答：

- 生成的代码里，哪几行是 `T.Parallel` 循环？哪几行是 `T.reduce_max`？（对应 4.1 / 4.2）
- `m` 被保护后，`m_new` 是否还复用了 `m` 的名字？为什么？（对应 4.3）
- `input_vars` 里收集了哪些名字？哪些是叶子输入，哪些是中间/输出缓冲区？

> 提示：如果你没有 GPU 或没配好 TileLang 环境，本实践依然可做——`generate_tl_from_dag` 只是在 Python 里拼字符串，不调用任何 GPU 运行时，`print(code)` 即可看到结果。

## 6. 本讲小结

- `generate_tl_from_dag` 用**后序深度优先遍历** `SymbolScalar.prev` 链，保证每个变量先定义后使用；`x.lowered` 标志实现**去重**，被多次引用的节点只生成一次。
- 它返回的 `input_vars` 收集**所有被引用的缓冲区名**（叶子 + 中间 + 输出），供 lower 层拼出函数形参；`inputs`（`return_inputs=True`）才只含叶子。
- `to_tl_op` 按**维度名字匹配**推导循环下标，`"1"` 维与缺失维退化为固定下标 `0`，从而实现隐式广播；逐元素算子包在 `for ... in T.Parallel(...):` 里，规约算子是一行 `T.reduce_*`。
- 数值技巧：`exp`→`exp2(x*1.442695)`、`log`→`log2(x)*0.69314718`，用换底公式把慢函数换成 GPU 上更快的 `exp2`/`log2`。
- inplace 复用优化：当输入「只用一次（`count==1`）或已是最后一次访问（`visit_count==count`）」且 `allow_reuse` 为真、且形状相同时，把输出名改写成输入名，省掉一次缓冲区分配。
- 跨块长期状态（`online_rowscales`、`o_scalevar`）靠 `set_allow_reuse(False)` 保护；同一批节点要在第二次生成前调用 `clear_codegen()` 复位 `lowered`/`visit_count`。

## 7. 下一步学习建议

本讲讲完了「单个算子节点 → 一段 TileLang 代码」的翻译机制。接下来：

- **u2-l4（多后端代码生成）**：对比 `to_tl_op` / `to_cute_op` / `to_pytorch_op` 三套发射器，看同一个 DAG 如何发出 TileLang、CuTe C++、PyTorch 三种目标代码。
- **u2-l5（score_mod 的符号化与降级）**：看 lower 层如何把用户写的 `score_mod` Python 函数跑成符号 DAG，再交给本讲的 `generate_tl_from_dag`，并额外用 `SymbolScalar.backward` 生成反向代码。
- 想立刻看到真实产物，可在配好环境后运行 `attn_script/mha.py`，再到引擎的 cache 目录里阅读生成出的完整 TileLang 源码，对照本讲找到其中的 `T.Parallel`、`T.reduce_max`、`exp2` 等痕迹（参见 u5-l6 的导出调试技巧）。
