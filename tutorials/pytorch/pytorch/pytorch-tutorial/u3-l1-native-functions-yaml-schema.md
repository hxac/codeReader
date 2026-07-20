# native_functions.yaml 算子模式定义

## 1. 本讲目标

本讲承接 u2-l4。上一讲我们追踪了一次 `torch.add` 调用，结论是：函数式 `torch.add` 与方法式 `x.add` 两条入口最终在 C++ 层汇合到同一个 `at::add`。但 `at::add` 这个符号本身是从哪里来的？它的名字、参数类型、返回类型、以及「在不同后端上跑哪段 C++ 代码」是谁规定的？

学完本讲后，你应该能够：

1. 打开 `native_functions.yaml`，读懂任意一个算子的 schema（函数名、重载名、参数、可选、默认值、返回类型）。
2. 解释 `dispatch`、`variants`、`structured`、`device_check`、`manual_cpp_binding` 等 yaml 字段的含义。
3. 理解 `CompositeImplicitAutograd` 与 `CompositeExplicitAutograd` 这两个特殊 dispatch key 如何决定一个算子「能不能自动求导」。
4. 用 `add` / `sum` / `mul` 三个真实算子，把上述概念串成完整认知。

本讲只讲「schema 怎么写、字段什么意思」，不展开代码生成器内部实现（那是 u3-l2 的内容）和 Dispatcher 运行时查表（那是 u3-l3 的内容）。

## 2. 前置知识

- **单一事实来源（Single Source of Truth）**：PyTorch 有上千个算子，每个算子都要在 C++ 层生成声明、在 Python 层生成绑定、生成类型桩、生成 autograd 注册……如果这些代码各自手写，必然很快不一致。PyTorch 的做法是：把「这个算子长什么样」集中写在一个 YAML 文件里，其余全部由代码生成器从这个文件派生。这个 YAML 文件就是 `native_functions.yaml`。
- **YAML 基础**：YAML 用缩进表示层级。本文件里每个算子是列表的一项（以 `- func:` 开头），其下缩进的 `dispatch:`、`variants:` 等是这一项的属性。
- **schema（模式）**：一段形如 `add.Tensor(Tensor self, Tensor other, *, Scalar alpha=1) -> Tensor` 的字符串，用统一语法描述「函数名、参数类型与名字、返回类型」。它借鉴了 TorchScript 的函数签名语法。
- **aliasing annotation（别名标注）**：像 `Tensor(a!)` 这样的写法，表示这个张量参数可能与别的参数或返回值共享底层内存，且可被写入。这是 schema 表达「原地操作 / out 参数 / view」语义的关键。

如果你对 Tensor 的 dtype / device / 布局还不熟，建议先看 u2-l2、u2-l3。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [aten/src/ATen/native/native_functions.yaml](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/native_functions.yaml) | 算子 schema 的**唯一事实来源**，本讲的主角。1.6 万行，定义了 PyTorch 全部公开算子。 |
| [aten/src/ATen/native/README.md](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/README.md) | 官方对 yaml 格式的权威说明，本讲很多字段含义直接引自这里。 |
| [torchgen/model.py](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/model.py) | 把 yaml 解析成 Python 数据模型（`NativeFunction` / `FunctionSchema`）。本讲只在确认 `structured` 语义时引用它，深入分析留给 u3-l2。 |

阅读建议：本讲全程对照 `native_functions.yaml` 真实行号，建议你在另一个窗口打开该文件边读边对照。

## 4. 核心概念与源码讲解

### 4.1 schema 的基本语法

#### 4.1.1 概念说明

每个算子在 yaml 里表现为列表的一项，最核心的一行是 `func:`，它的值是一段 schema 字符串。官方 README 给出的总格式是：

```
- func: func_name[.overload_name](ArgType arg0[=default], ArgType arg1[=default], ...) -> Return
  variants: function, method
  dispatch:
    CPU: func_cpu
    CUDA: func_cuda
```

这段字符串要回答四个问题：

1. **这个算子叫什么？**（`func_name` + 可选的 `.overload_name`）
2. **它接受什么参数？**（括号内的 `ArgType arg` 列表，含默认值与 `*` 关键字分隔符）
3. **它返回什么？**（`-> Return`）
4. **它有没有别名 / 原地语义？**（`Tensor(a!)` 这类标注）

`func:` 行之外，`variants`、`dispatch` 等缩进字段是它的「属性」，我们放到 4.2 讲。

#### 4.1.2 核心流程

把一段 schema 翻译成「可调用对象」的概念流程：

```
schema 字符串
    │  （torchgen 解析，u3-l2）
    ▼
FunctionSchema { name, overload_name, arguments[], returns[] }
    │  （torchgen 生成代码，u3-l2）
    ▼
C++ 声明 at::add / Tensor::add + Python 绑定 + .pyi 类型桩 + Dispatcher 注册
```

schema 语法的几个要点（来自官方 README 的「Argument types」一节）：

- **可选类型**：类型后跟 `?`（如 `Tensor?`、`ScalarType?`、`int[1]?`）表示该参数可为空，C++ 侧映射为 `std::optional`。
- **默认值**：参数名后跟 `=默认值`（如 `alpha=1`、`keepdim=False`、`dtype=None`），任何「参数后缀」都可带默认值。
- **`*` 分隔符**：`*` 是一个特殊哨兵参数，本身不对应任何实参，只表示「它之后的参数在 Python 绑定里只能用关键字传递」。
- **列表类型**：`Tensor[]` → C++ `ArrayRef<Tensor>`；`int[]` → 整数列表；`int[2]` → 定长 2，可接受单个数字自动展开成 `[2,2]`。
- **重载名**：同名算子可用 `.overload_name` 区分，例如 `add.Tensor` 与 `add.Scalar` 是两个不同重载。

#### 4.1.3 源码精读

先看官方 README 对 `func` 格式的定义：

[native/README.md:31-35](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/README.md#L31-L35) —— 注意方括号 `[.overload_name]` 表示重载名可选，整个 schema 的「骨架」就是 `名字(参数) -> 返回`。

README 紧接着逐条列出合法的参数类型，比如 `Tensor` 映射为 `const Tensor&`、`Tensor?` 表示可选、`int` 映射为 `int64_t` 等：

[native/README.md:40-74](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/README.md#L40-L74) —— 这是理解「为什么 yaml 里写 `int` 而 C++ 里是 `int64_t`」的依据。

现在看一个真实的、足够丰富的 schema：`add.Tensor`。

[native_functions.yaml:542-552](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/native_functions.yaml#L542-L552)

```yaml
- func: add.Tensor(Tensor self, Tensor other, *, Scalar alpha=1) -> Tensor
  device_check: NoCheck   # TensorIterator
  structured_delegate: add.out
  variants: function, method
  dispatch:
    SparseCPU, SparseCUDA, SparseMPS, SparseMeta, SparseXPU: add_sparse
    ...
  tags: [core, pointwise]
```

逐字段拆解这段 schema：

| 片段 | 含义 |
| --- | --- |
| `add.Tensor` | 算子名为 `add`，重载名为 `Tensor`（区分于 `add.Scalar`）。 |
| `Tensor self` | 第一个张量参数，名为 `self`。 |
| `Tensor other` | 第二个张量参数。 |
| `*` | 之后的参数只能关键字传递。 |
| `Scalar alpha=1` | 标量参数，默认值 1，用于 `self + alpha * other`。 |
| `-> Tensor` | 返回一个新张量（非原地）。 |

注意：这里**没有** `(a!)` 标注，所以它是「函数式」版本——会分配新内存、不修改输入。原地版本 `add_.Tensor` 才带标注（见下文 4.3）。

再看「返回类型」的官方规则，README 说明返回可以是单个 `Tensor`、`Tensor[]`，或一个元组：

[native/README.md:125-148](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/README.md#L125-L148) —— 例如 `adaptive_max_pool1d(...) -> (Tensor, Tensor)` 表示返回二元组。

#### 4.1.4 代码实践

**实践目标**：亲手解析一段 schema，验证你读懂了每一部分。

**操作步骤**：

1. 打开 `native_functions.yaml`，定位到第 542 行的 `add.Tensor`。
2. 在一张纸上把它拆成：函数名、重载名、各参数（类型 + 名字 + 是否可选 + 默认值）、`*` 位置、返回类型。
3. 打开 Python，调用对应的算子，验证你对「关键字参数」与「默认值」的理解：

```python
# 示例代码（非项目原有，用于验证 schema 语义）
import torch
a = torch.tensor([1.0, 2.0, 3.0])
b = torch.tensor([10.0, 20.0, 30.0])

# alpha 是关键字参数（schema 里在 * 之后），可省略（默认 1）
print(torch.add(a, b))              # [11, 22, 33]
print(torch.add(a, b, alpha=2))     # [21, 42, 63]  即 a + 2*b
```

**需要观察的现象**：

- `alpha` 必须用关键字传（`torch.add(a, b, 2)` 会报错或被当成位置参数报错），这与 schema 中 `*` 的位置一致。
- 不传 `alpha` 时结果等同于加法，对应默认值 `alpha=1`。

**预期结果**：第一行得到 `[11, 22, 33]`，第二行得到 `[21, 42, 63]`。

#### 4.1.5 小练习与答案

**练习 1**：schema `sum.dim_IntList(Tensor self, int[1]? dim, bool keepdim=False, *, ScalarType? dtype=None) -> Tensor` 中，`int[1]?` 的 `?` 和 `[1]` 各表示什么？

**参考答案**：`[1]` 表示这是一个长度标识（告诉生成器「Python 侧可接受单个整数并展开成长度 1 的列表」）；`?` 表示该参数整体可选，可传 `None`，C++ 侧映射为 `std::optional`。

**练习 2**：为什么 `add.Tensor` 的 schema 里 `alpha` 写在 `*` 之后？

**参考答案**：为了让 `alpha` 成为只能关键字传递的参数。这样 `torch.add(a, b, 2)` 不会把 `2` 误解成 `other`，避免位置歧义；用户必须写 `alpha=2`。

---

### 4.2 dispatch、autograd 与 structured 标记

#### 4.2.1 概念说明

schema 行只回答了「函数长什么样」。但一个算子在不同后端（CPU / CUDA / Sparse / Mkldnn…）上要跑不同的 C++ 代码，还要决定「能不能自动求导」。这些信息靠 `func:` 行之外的属性字段表达，最重要的是：

- **`dispatch`**：一张「dispatch key → C++ 实现函数名」的映射表。Dispatcher（u3-l3）会根据输入张量携带的 dispatch key 查这张表，选对应的 kernel。
- **composite dispatch key**：`CompositeImplicitAutograd` / `CompositeExplicitAutograd` 这两个特殊 key，表示「一份实现通吃所有后端」，并隐含了「是否自动可微」的信息。
- **`variants`**：决定生成「函数 `at::foo()`」还是「方法 `tensor.foo()`」。
- **`structured` / `structured_delegate`**：一种代码生成优化，把「算形状」与「算数值」分离，避免 `foo` / `foo_` / `foo.out` 三个变体各写一遍形状逻辑。

需要特别澄清一点：**`native_functions.yaml` 本身并不直接写反向（求导）公式**。真正的求导公式在另一个文件 `tools/autograd/derivatives.yaml` 里。但本文件的 dispatch key 选择，决定了「这个算子是自动可微，还是必须去 `derivatives.yaml` 找公式」。

#### 4.2.2 核心流程

一个算子的「dispatch + autograd」决策树（整理自 README「Choosing the right dispatch keyword」一节）：

```
你的实现是否对所有后端都成立？
├─ 否 → 写 dispatch: {CPU: .., CUDA: .., ...}，逐后端注册
│        （此时若要训练，必须在 derivatives.yaml 写反向公式）
└─ 是（实现只调用其它 at:: 算子）
     └─ 你的实现是否自动可微（即它调用的算子都可微）？
        ├─ 是 → 不写 dispatch 段，默认注册为 CompositeImplicitAutograd
        │        推理与训练都「免费」可用
        └─ 否 / 想用更稳定的数值公式
           → dispatch: {CompositeExplicitAutograd: kernel}
              只走推理；训练须在 derivatives.yaml 显式写反向公式
```

三种 composite key 的优先级关系（README 明确说明）：

\[ \text{优先级}:\quad \text{CompositeExplicitAutograd} > \text{CompositeImplicitAutograd} \]

即如果同时给一个算子写了这两个 key，`CompositeImplicitAutograd` 会被完全忽略，解析时直接报错。

#### 4.2.3 源码精读

**`variants` 字段**：README 解释它控制生成「方法」还是「函数」。

[native/README.md:202-220](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/README.md#L202-L220) —— 关键点：若声明为 method，参数列表中必须有一个 `Tensor self`，在方法变体里它会被省略（变成 `self` 而非显式参数）。这就是为什么 `add.Tensor` 写了 `variants: function, method` 后，你既能 `torch.add(a,b)` 又能 `a.add(b)`（回顾 u2-l4）。

**`dispatch` 字段**：README 给出格式与三类 composite key 的权威定义。

[native/README.md:274-349](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/README.md#L274-L349) —— 注意三个要点：
1. `CPU, CUDA: func` 这种逗号写法表示多个后端共用同一个实现函数名。
2. `CompositeImplicitAutograd`：通吃所有后端，且**隐式**支持 autograd（因为它调用的子算子都可微）。
3. `CompositeExplicitAutograd`：通吃所有后端，但**显式**要求你在 `derivatives.yaml` 里写反向公式。

看一个「不写 dispatch 段、默认走 CompositeImplicitAutograd」的真实例子——`sum_to_size`：

[native_functions.yaml:5866-5871](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/native_functions.yaml#L5866-L5871)

```yaml
- func: sum_to_size(Tensor self, SymInt[] size) -> Tensor
  variants: method
  device_check: NoCheck
  device_guard: False
  dispatch:
    CompositeImplicitAutograd: sum_to_size_symint
```

它的实现 `sum_to_size_symint` 内部只是调用其它可微算子（如 `sum`），所以注册到 `CompositeImplicitAutograd` 后，**即便不写任何反向公式，`sum_to_size` 也能自动求导**——autograd 引擎会顺着它内部调用的 `sum` 等算子的反向公式，用链式法则自动拼出梯度。这正是「implicit autograd」的含义。

对比一个「显式 autograd」的例子——`add.Scalar`：

[native_functions.yaml:613-618](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/native_functions.yaml#L613-L618)

```yaml
- func: add.Scalar(Tensor self, Scalar other, Scalar alpha=1) -> Tensor
  device_check: NoCheck   # TensorIterator
  variants: function, method
  dispatch:
    CompositeExplicitAutograd: add
  tags: [core, pointwise]
```

它注册到 `CompositeExplicitAutograd`，意味着这条路径只负责**推理**；它的可微性要靠 `derivatives.yaml` 里 `add` 的反向公式来补。README 在 377-380 行特别提醒：当你给原本「无 dispatch 段（即默认 CompositeImplicitAutograd）」的算子新增后端 kernel 时，**必须**同时补一条 `CompositeImplicitAutograd:` 指向旧实现，否则会破坏其它后端的隐式可微性。

[native/README.md:377-384](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/README.md#L377-L384)

**`structured` / `structured_delegate` 字段**：README 没有专门章节，权威定义在 `torchgen/model.py` 的注释里。

[model.py:568-585](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/model.py#L568-L585) —— 三个要点：
1. **只有 `.out` 变体才能标 `structured: True`**；它的「形状检查逻辑」与「kernel 数值逻辑」被分开定义（meta 函数算形状，impl 函数算数值）。
2. **非 out 变体用 `structured_delegate: <某>.out`** 表示「我把自己委托给那个 structured 的 out kernel」，从而复用同一份形状逻辑。
3. `structured_inherits`（通常是 `TensorIteratorBase`）指明 meta 类继承自哪个基类。

一个算子的 functional / inplace / out 三个变体里，只有 `.out` 是「真身」（structured），另外两个通过 `structured_delegate` 指向它。下文 4.3 的 `add` / `mul` 会给出完整对照。

**其它常见字段速查**（均可在 README 找到）：

| 字段 | 作用 |
| --- | --- |
| `device_check: NoCheck` | 关闭「所有张量必须在同一设备」的默认检查。 |
| `device_guard: False` | 关闭「自动把当前设备切到首个张量所在设备」的默认 guard。 |
| `manual_cpp_binding: True` | 该算子的 C++ 绑定由人手写，不自动生成（多见于 autograd 相关的薄包装，如 `is_leaf`、`data`）。 |
| `manual_kernel_registration: True` | 不自动把实现注册到 catchAll key，改为人手注册（极少用）。 |
| `autogen: foo, foo.out` | 让代码生成器自动派生 functional / out 变体（functionalization 依赖）。 |
| `tags: [core, pointwise]` | 给算子打分类标签（pointwise / reduction / core 等），供 Inductor 等下游使用。 |
| `use_const_ref_for_mutable_tensors: True` | 让「数据可能变化」的张量参数也生成 `const Tensor&` 而非 `Tensor&`。 |

#### 4.2.4 代码实践

**实践目标**：通过运行时行为，体会「CompositeImplicitAutograd 自动可微」与「dispatch key 选择」的关系。

**操作步骤**：

1. 在 `native_functions.yaml` 中确认 `sum_to_size`（5866 行）注册在 `CompositeImplicitAutograd`。
2. 运行下面这段**示例代码**（非项目原有），验证它确实「免费」可微：

```python
# 示例代码：验证 implicit autograd
import torch
x = torch.randn(4, requires_grad=True)
y = x.sum_to_size(())        # 调用的是 CompositeImplicitAutograd 实现
y.backward()
print(x.grad)                # 期望：全 1，因为 sum_to_size 内部走 sum
```

3. 思考：我们没有在任何地方给 `sum_to_size` 写反向公式，为什么 `.backward()` 不报错？

**需要观察的现象**：

- `backward()` 成功执行，不抛「no grad function」错误。
- `x.grad` 是全 1 的长度 1 张量。

**预期结果**：因为 `sum_to_size` 内部调用 `sum`，而 `sum` 可微，autograd 引擎沿调用链自动拼出了梯度。这正是 `CompositeImplicitAutograd` 的价值。

**说明**：若你修改 `x` 使其 `requires_grad=False`，则 `backward` 会因计算图不存在而报错——这从反面印证了可微性来自调度链路而非算子本身。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `native_functions.yaml` 里搜不到 `add` 的反向（求导）公式？

**参考答案**：因为反向公式不在本文件，而在 `tools/autograd/derivatives.yaml`。本文件只通过 dispatch key（`CompositeImplicitAutograd` / `CompositeExplicitAutograd`）声明「该算子是隐式可微，还是需要去 derivatives.yaml 找显式公式」。

**练习 2**：如果一个算子同时写了 `CompositeImplicitAutograd` 和 `CompositeExplicitAutograd` 两条 dispatch，会发生什么？

**参考答案**：解析 yaml 时会报错。两者优先级为 `CompositeExplicitAutograd > CompositeImplicitAutograd`，同时出现会让后者被完全忽略，代码生成器据此禁止这种写法（见 README 第 611-615 行）。

---

### 4.3 常见算子示例：add / sum / mul 的三件套

#### 4.3.1 概念说明

PyTorch 里大多数「数值类」算子都遵循一个「三件套」模式：

1. **functional 变体**：`foo(Tensor self, ...) -> Tensor`，分配新内存返回。
2. **inplace 变体**：`foo_(Tensor(a!) self, ...) -> Tensor(a!)`，原地修改 `self` 并返回它（名字带下划线）。
3. **out 变体**：`foo.out(Tensor self, ..., Tensor(a!) out) -> Tensor(a!)`，把结果写入调用方提供的 `out` 张量。

三者的别名标注（`(a!)`）位置不同，正是 schema 表达「谁会被写、返回值是否别名输入」的方式。`add`、`mul`、`sum` 是最典型的三件套，且 `add.out` / `mul.out` 还是 `structured: True` 的标准范例。

#### 4.3.2 核心流程

三件套之间的结构关系（以 `add` 为例）：

```
add.Tensor   (functional)  ── structured_delegate ──┐
add_.Tensor  (inplace)     ── structured_delegate ──┼──► add.out (structured: True)
                                                    │     ├── meta: 算形状/dtype
add.Scalar   (functional, 标量版) ── CompositeExplicitAutograd
                                                    └── impl: CPU/CUDA/... 算数值
```

关键：functional 与 inplace 变体都通过 `structured_delegate: add.out` 委托给同一个 structured 的 out kernel，因此「形状推导」逻辑只写一遍（在 out 的 meta 里），三个变体共享。

#### 4.3.3 源码精读

**`add` 的 functional + inplace + out 三件套**（连续三段）：

[native_functions.yaml:542-552](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/native_functions.yaml#L542-L552) —— `add.Tensor`（functional）。注意 `structured_delegate: add.out`，以及 `Tensor self` / `Tensor other` **无** `(a!)`（不修改输入）。

[native_functions.yaml:554-563](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/native_functions.yaml#L554-L563) —— `add_.Tensor`（inplace）。关键区别：`Tensor(a!) self` 带了写入标注，且返回 `Tensor(a!)`（返回值与输入别名）。这就是「原地操作」的 schema 写法。

[native_functions.yaml:565-584](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/native_functions.yaml#L565-L584) —— `add.out`（structured 真身）。这是三件套的核心：

```yaml
- func: add.out(Tensor self, Tensor other, *, Scalar alpha=1, Tensor(a!) out) -> Tensor(a!)
  device_check: NoCheck   # TensorIterator
  structured: True
  structured_inherits: TensorIteratorBase
  ufunc_inner_loop:
    Generic: add (AllAndComplex, BFloat16, Half, ComplexHalf)
    ScalarOnly: add (Bool)
  dispatch:
    SparseCPU, SparseMeta: add_out_sparse_cpu
    ...
    MPS: add_out_mps
    XPU: add_out_xpu
  tags: pointwise
```

逐项解读：

- `Tensor(a!) out`：`out` 参数被标注为「可写入、与返回值别名」，对应 Python 的 `torch.add(a, b, out=...)`。
- `structured: True`：这是 structured kernel，形状逻辑与数值逻辑分离。
- `structured_inherits: TensorIteratorBase`：meta 类继承 `TensorIteratorBase`（elementwise 算子常用的迭代器基类）。
- `ufunc_inner_loop`：进一步的 ufunc 代码生成，按 dtype 分流「通用内核」与「仅标量内核（Bool）」。
- `dispatch:`：列出各后端的 out 实现函数名（`add_out_mps`、`add_out_xpu` 等）。注意**没有** `CPU` / `CUDA` 条目——因为它们走的是更上层的 TensorIterator 通用路径（通过 `ufunc_inner_loop` 与 structured 机制自动生成）。

**`mul` 三件套**与 `add` 完全同构，可对照阅读：

[native_functions.yaml:4244-4254](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/native_functions.yaml#L4244-L4254) —— `mul.Tensor`（functional，`structured_delegate: mul.out`）。

[native_functions.yaml:4267-4279](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/native_functions.yaml#L4267-L4279) —— `mul.out`（`structured: True`，`structured_inherits: TensorIteratorBase`）。与 `add.out` 不同的是，它的 dispatch 里**有** `CPU, CUDA, MPS, MTIA, XPU: mul_out` 这种逗号合并写法（多后端共用同名实现），印证了 README 第 308 行「`CPU, CUDA: func` 可合并」的规则。

**`sum` 三件套**展示了「reduction 类」算子的写法，并演示了「同名多重载 + autogen」：

[native_functions.yaml:5814-5822](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/native_functions.yaml#L5814-L5822) —— `sum`（无重载名，最简形式，只支持跨所有元素求和）。它写 `autogen: sum.out`，让生成器自动派生出 out 变体。

[native_functions.yaml:5824-5833](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/native_functions.yaml#L5824-L5833) —— `sum.dim_IntList`（带 `dim` 的重载）。`structured_delegate: sum.IntList_out` 指向 out 真身。

[native_functions.yaml:5835-5840](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/native_functions.yaml#L5835-L5840) —— `sum.IntList_out`（`structured: True`）。注意它**没有** `variants` 字段，所以默认只生成函数式 `at::sum_out`，不生成 `tensor.sum_out()` 方法——out 变体本就只供内部 / 关键字调用。

把三件套对照下表记住别名标注的位置规律：

| 变体 | self 标注 | out 标注 | 返回 | structured? |
| --- | --- | --- | --- | --- |
| `add.Tensor`（functional） | 无 | 无 | `Tensor`（新内存） | delegate → `add.out` |
| `add_.Tensor`（inplace） | `(a!)` | 无 | `Tensor(a!)` | delegate → `add.out` |
| `add.out`（out） | 无 | `(a!)` | `Tensor(a!)` | `True`（真身） |

#### 4.3.4 代码实践

**实践目标**：用运行时验证「三件套」的别名语义与 `out=` 行为。

**操作步骤**：

1. 在 `native_functions.yaml` 找到 `add.Tensor`（542）、`add_.Tensor`（554）、`add.out`（565）三段，确认它们的别名标注符合上表。
2. 运行下面这段**示例代码**（非项目原有）：

```python
# 示例代码：验证三件套的别名语义
import torch

a = torch.tensor([1.0, 2.0, 3.0])
b = torch.tensor([10.0, 20.0, 30.0])

# functional：新内存
c = torch.add(a, b)
print(c.data_ptr() == a.data_ptr())   # False

# out=：写入调用方提供的张量（对应 add.out 的 Tensor(a!) out）
out = torch.empty(3)
d = torch.add(a, b, out=out)
print(d.data_ptr() == out.data_ptr()) # True，返回值与 out 别名

# inplace：原地修改 self（对应 add_.Tensor 的 Tensor(a!) self）
e = a.clone()
before = e.data_ptr()
e.add_(b)
print(e.data_ptr() == before)         # True，原地不换 storage
```

**需要观察的现象**：

- functional 返回的张量与输入 `a` 不共享内存。
- `out=` 调用后，返回值与传入的 `out` 张量 `data_ptr` 完全相同（别名）。
- inplace 调用前后 `data_ptr` 不变（原地写）。

**预期结果**：依次打印 `False`、`True`、`True`。这三个布尔值恰好对应 schema 中「无标注 / `out` 标注 `(a!)` / `self` 标注 `(a!)`」三种情况。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `add.out` 有 `structured: True`，而 `add.Tensor` 没有、只写了 `structured_delegate: add.out`？

**参考答案**：因为「形状推导」逻辑只应写一遍。把 out 变体设为 structured 真身后，functional（`add.Tensor`）和 inplace（`add_.Tensor`）通过 `structured_delegate` 委托给它，三者共享同一份形状逻辑，避免重复。

**练习 2**：`sum.IntList_out` 没有 `variants` 字段，这意味着什么？

**参考答案**：默认只生成函数变体（`at::sum_out`），不生成方法变体（不会有 `tensor.sum_out(...)`）。out 变体设计上就是供 `out=` 关键字或内部调用使用，不需要作为公开方法挂在 Tensor 上。

**练习 3**：`mul.out` 的 dispatch 写了 `CPU, CUDA, MPS, MTIA, XPU: mul_out`，而 `add.out` 的 dispatch 里没有 `CPU`/`CUDA`。请结合 structured / ufunc 机制推测原因。

**参考答案**：`add.out` 通过 `ufunc_inner_loop` 把 elementwise 内核交给 ufunc 代码生成（按 dtype 自动产出 CPU/CUDA 内核），所以不需要在 dispatch 里显式列 CPU/CUDA；而 `mul.out` 选择直接在 dispatch 里列出多后端共用的 `mul_out` 实现函数。两者是 structured kernel 的两种不同实现策略。此项细节较深，可作为延伸阅读，标注「待本地验证」。

## 5. 综合实践

**综合任务**：选一个你感兴趣、且本讲没详细分析的算子（建议 `div` 或 `clamp`），完成一次「从 schema 到行为」的完整分析。

要求：

1. 在 `native_functions.yaml` 中定位该算子的全部重载（functional / inplace / out）。
2. 为每个重载记录：行号、函数名与重载名、各参数类型与默认值、`*` 位置、返回类型、别名标注。
3. 找出它的 `dispatch` 段用了哪些 dispatch key（后端 key 还是 composite key？），据此判断它是「自动可微」还是「需显式反向公式」。
4. 判断它是不是 `structured: True`；若是，找出哪些变体通过 `structured_delegate` 指向它。
5. 写一段**示例代码**（非项目原有），用 `data_ptr` 或 `backward` 验证你从 schema 推断出的「别名关系」与「可微性」。

**交付物**：一张类似本讲 4.3 末尾的对照表 + 一段验证脚本 + 一句结论（该算子属于哪类：pointwise / reduction、隐式还是显式可微、是否 structured）。

这个任务把「读 schema」「理解 dispatch/autograd 标记」「理解 structured 三件套」三件事串起来，是后续阅读 u3-l2（代码生成）与 u3-l3（Dispatcher 运行时）前的最佳自测。

## 6. 本讲小结

- `native_functions.yaml` 是 PyTorch 全部公开算子的**单一事实来源**：算子的名字、参数、返回、后端实现、可微性，全部集中在这里声明，其余代码均由 torchgen 派生。
- 一条 `func:` schema 的骨架是 `名字.重载(参数列表) -> 返回`；参数支持 `?` 可选、`=默认值`、`*` 关键字分隔、`Tensor(a!)` 别名标注等语法。
- **三件套模式**：functional（无标注）/ inplace（`self` 标 `(a!)`）/ out（`out` 标 `(a!)`）；其中 out 变体常为 `structured: True` 真身，另两者用 `structured_delegate` 委托给它，共享形状逻辑。
- **dispatch key 决定可微性**：`CompositeImplicitAutograd` 隐式可微（自动拼梯度），`CompositeExplicitAutograd` 只走推理、需去 `derivatives.yaml` 找反向公式；真正的反向公式不写在本文件里。
- `variants` 决定生成 `at::foo()` 函数还是 `tensor.foo()` 方法；`device_check` / `device_guard` / `manual_cpp_binding` / `autogen` / `tags` 等是控制生成行为的辅助字段。
- 本讲止步于「schema 写法和字段含义」；schema 如何被解析成数据模型、如何生成 C++/Python 代码，是 u3-l2 的主题。

## 7. 下一步学习建议

- **u3-l2（TorchGen 代码生成机制）**：本讲反复出现的「代码生成器会从 yaml 派生一切」，具体是怎么做的？去看 `torchgen/gen.py` 的入口与 `torchgen/model.py` 里的 `FunctionSchema` / `NativeFunction` 数据模型，理解 schema 字符串如何变成结构化对象、再变成上千个生成文件。
- **u3-l3（DispatchKey 与 Dispatcher 分发机制）**：本讲的 `dispatch:` 表只是「声明」；运行时 Dispatcher 如何根据张量携带的 dispatch key 查表、跳转到正确 kernel，是下一篇的核心。
- **延伸阅读**：想理解反向公式怎么写，可直接打开 `tools/autograd/derivatives.yaml`，对照本讲提到的 `CompositeExplicitAutograd` 算子（如 `add.Scalar`）看它的梯度公式条目，建立「schema → dispatch key → 反向公式」的完整闭环。
