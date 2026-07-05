# 元组参数与静态形状特化

## 1. 本讲目标

到目前为止，我们写的内核参数都是「一个参数对应一个东西」：一个数组、一个标量、或一个 `ct.Constant[int]` 常量。但真实算子里常常需要把**一组相关的东西打包在一起**传给内核——例如一对输入张量 `(a, b)`、一组编译期常量配置 `(tm, tn, tk)`、或「一个权重表加一个偏置加一个标量」`(W, bias, alpha)`。本讲讲解 cuTile 在本次更新里新增的两类内核参数特性，它们都围绕「让内核参数更有表达力、让编译器拿到更多编译期信息」展开。

学完后你应该能够：

1. 掌握 **tuple 参数**的各种写法：`tuple[Tensor, int]`、`tuple[Tensor, Tensor]`、`ct.Constant[tuple[int, float]]`、`tuple[ct.Constant[int], float]`，以及 plain tuple 简写和变长元组 `tuple[ct.Constant[int], ...]`。
2. 理解 **`TupleConstraint`** 与 **`cutile_python_v2` 调用约定**的关系：旧版 `cutile_python_v1` 既不支持 tuple 参数，也不支持静态形状。
3. 掌握用 **`ArrayAnnotation(static_shape_dims=...)`** 把数组的某些维度特化为编译期常量（含负索引），并理解它如何改变 JIT 缓存键。
4. 理解为什么 tuple 与静态形状都需要 **`ParameterAnnotationNode`** 这棵统一的注解树来表达，以及它们如何影响**内核特化与 JIT 缓存命中**。

## 2. 前置知识

本讲默认你已经掌握以下概念（前几讲已建立）：

- **load / store 范式（u3-l1）**：`ct.load(array, index, shape)` 取出一块 tile，`ct.store` 写回；`index` 是 tile space 索引，`shape` 决定 tile 大小。
- **Constant 常量嵌入（u3-l5）**：用 `ct.Constant[T]` 标注的参数在编译期被烘焙成 IR 字面量、从运行时签名消失；**每个唯一取值都会单独编译一份 cubin**，因此改 Constant 值会触发重新编译。这是理解本讲「特化」与「重新编译」的钥匙。
- **DLPack / CUDA Array Interface（u2-l2）**：宿主张量（`torch.Tensor`、`cupy.ndarray`）经零拷贝协议进入内核。

本讲新引入三个概念，先一句话建立直觉：

- **元组参数（tuple argument）**：把若干个内核参数「绑成一束」作为一个 Python `tuple` 传入，内核里用 `pair[0]`、`pair[1]` 解包访问。它纯粹是**参数打包**的便利，不改变计算语义。
- **静态形状（static shape）**：默认情况下数组的 `shape` 是**运行时**值（内核运行时才知道）；用 `static_shape_dims` 可以指定「这几个维度在编译期就已知」，于是它们能像 Constant 一样被嵌入、被用于对齐推理，代价是**不同的 shape 值会编译出不同的内核**。
- **调用约定（calling convention）**：Python 与 GPU cubin 之间「参数怎么摆、怎么读」的协议。`cutile_python_v1` 是旧协议，`cutile_python_v2` 是新增协议，**只有 v2 知道怎么处理 tuple 与静态形状**。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/cuda/tile/_annotated_function.py` | 定义统一的参数注解树 `ParameterAnnotationNode`（叶子 / 同质元组 / 异质元组），把 Python 类型注解解析成这棵树。 |
| `src/cuda/tile/_stub.py` | 定义用户侧注解元数据：`ArrayAnnotation`（含 `static_shape_dims`）、`ConstantAnnotation`、`ScalarAnnotation`、`ListAnnotation`，以及 `Constant` / `IndexedWithInt64` / `ScalarInt64` 别名。 |
| `src/cuda/tile/compilation/_signature.py` | 定义编译期签名体系：`TupleConstraint`、`ArrayConstraint.shape_constant`、`KernelSignature`，以及按调用约定门控特性支持的 `_validate_constraint_support`。 |
| `src/cuda/tile/compilation/_name_mangling.py` | name mangling 规则：tuple 用 `T` 前缀编码、静态形状用 `s` 轴谓词编码，且可逆 demangle。 |
| `src/cuda/tile/_cext.pyi` | C++ 扩展桥接的 Python 类型存根：`TileDispatcher(parameter_annotations)`、`CallingConvention`（v1/v2/version）。 |
| `test/test_tuple_arguments.py` | tuple 参数的完整测试：标量/数组/混合/嵌套/变长元组、Constant 与 tuple 的组合、各类错误情形。 |
| `test/test_array.py` | 静态形状测试：`test_static_shape_standalone_recompile` 验证不同 shape 触发重新编译。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**4.1 ParameterAnnotationNode（统一注解树）**、**4.2 TupleConstraint（tuple 参数）**、**4.3 ArrayAnnotation.static_shape_dims（静态形状）**、**4.4 cutile_python_v2（新调用约定）**。前两个模块讲「怎么打包参数」，第三个讲「怎么把运行时 shape 提升为编译期常量」，第四个讲「为什么这些都需要一个新协议」。

### 4.1 ParameterAnnotationNode：统一的参数注解树

#### 4.1.1 概念说明

要支持 tuple 参数，cuTile 必须能描述「一个参数的注解本身又是一个结构」。在旧版里，每个参数只用几个布尔掩码（是否 Constant、是否 int64）来描述，这种扁平表示无法表达嵌套的 tuple。本次重构引入了一棵**统一的注解树** `ParameterAnnotationNode`，用三种节点表达一切：

- **`LeafAnnotationNode`（叶子节点）**：对应一个「原子」参数——可能是数组、标量、列表，或被 `Constant` 标注。它用 `constant` 布尔加最多一个具体的 `scalar`/`array`/`list` 注解来表达。
- **`HomogeneousTupleNode`（同质元组）**：对应 `tuple[T, ...]` 这种「变长、每个元素同类型」的元组，例如 `tuple[ct.Constant[int], ...]`。它只存一个 `each` 子节点，运行时长度可变。
- **`HeterogeneousTupleNode`（异质元组）**：对应 `tuple[A, B, C]` 这种「定长、每个位置类型可不同」的元组，例如 `tuple[Tensor, int]`。它存一个 `items` 元组，每个位置一个子节点。

这棵树是前端解析 Python 类型注解的**唯一产物**，整条编译流水线（`TileDispatcher`、IR 参数创建、签名推断）都从它出发。

#### 4.1.2 核心流程

```
Python 类型注解（如 tuple[ct.Constant[int], float]）
        │
        │  get_annotated_function() 调用 _build_annotation_node()
        ▼
ParameterAnnotationNode 树
        │
        ├─ HeterogeneousTupleNode(items=(
        │      LeafAnnotationNode(constant=True),    # 来自 ct.Constant[int]
        │      LeafAnnotationNode(constant=False),   # 来自 float
        │   ))
        ▼
交给 TileDispatcher / _create_kernel_parameters 递归展开成扁平的内核参数
```

解析的关键规则是：遇到 `Annotated[...]` 就剥出元数据（识别 `ConstantAnnotation`、`ArrayAnnotation` 等）；遇到内层是 `tuple` 就递归建元组节点；`ct.Constant` 这种「外层 Constant」会通过 `outer_constant` 向下传播，让整棵子树都变成常量。

#### 4.1.3 源码精读

三种节点的定义在这段代码里，注意每种都有一个 `KIND` 字符串用于后续分派：

- [src/cuda/tile/_annotated_function.py:L14-L36](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L14-L36)：`LeafAnnotationNode`，`validate()` 强制一个叶子只能有一种具体注解（不能同时 `Constant` 又 `ScalarInt64`）。
- [src/cuda/tile/_annotated_function.py:L38-L42](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L38-L42)：`HomogeneousTupleNode`，只持有一个 `each` 子节点。
- [src/cuda/tile/_annotated_function.py:L45-L49](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L45-L49)：`HeterogeneousTupleNode`，持有定长 `items`。

三者用类型别名收口为统一的 [`ParameterAnnotationNode`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L52)，并被 [`AnnotatedFunction`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L55-L59) 持有为 `parameter_annotations` 序列——这正是新版取代旧布尔掩码的字段。

解析入口 [`get_annotated_function`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L62-L70) 用 `typing.get_type_hints(include_extras=True)` 解析字符串注解（兼容 `from __future__ import annotations`），再对每个参数调一次 `_build_annotation_node`。

递归建树的核心逻辑在 [`_build_annotation_node`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L81-L94) 与 [`_build_tuple_node`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L73-L78)：前者判断「是不是 `Annotated`」「内层是不是 `tuple`」「`Constant` 是否出现」，把 `outer_constant` 一路向下传；后者用 `args[1] is ...` 这一条区分变长（同质）与定长（异质）元组。

#### 4.1.4 代码实践

**实践目标**：用 Python 内省亲自观察一棵注解树长什么样。

**操作步骤**（在装好 `cuda-tile` 的环境里）：

```python
# 示例代码：仅用于观察注解树结构，不需要 GPU
from cuda.tile._annotated_function import get_annotated_function
import cuda.tile as ct
import torch

@ct.kernel
def k(pair: tuple[ct.Constant[int], float], out):
    pass

af = get_annotated_function(k._pyfunc)
node = af.parameter_annotations[0]   # 第 0 个参数 pair 的注解树
print(type(node).__name__)           # HeterogeneousTupleNode
print(type(node.items[0]).__name__)  # LeafAnnotationNode
print(node.items[0].constant)        # True  —— 来自 ct.Constant[int]
print(node.items[1].constant)        # False —— float 是普通运行时标量
```

**需要观察的现象**：`pair` 被解析成 `HeterogeneousTupleNode`，其两个叶子分别带 `constant=True/False`。

**预期结果**：输出依次为 `HeterogeneousTupleNode`、`LeafAnnotationNode`、`True`、`False`。如果在不同 cuTile 版本上字段名不一致，以源码为准——本结果对应本讲 HEAD。

**待本地验证**：上述断言基于当前 HEAD 源码逻辑推断，请在你本地环境实际运行确认。

#### 4.1.5 小练习与答案

**练习 1**：`tuple[Tensor, Tensor]` 会解析成哪种节点？它的 `items` 长度是多少？
**答案**：`HeterogeneousTupleNode`，`items` 长度为 2，两个叶子都是 `constant=False` 的数组叶子。

**练习 2**：`ct.Constant[tuple[int, float]]` 与 `tuple[ct.Constant[int], float]` 解析出的树有何不同？
**答案**：前者是整棵都被 `outer_constant=True` 染色的异质元组，**两个**叶子都 `constant=True`；后者只有第 0 个叶子 `constant=True`，第 1 个叶子 `constant=False`（运行时标量）。这正是「整组常量」与「部分常量」的区别。

---

### 4.2 TupleConstraint：把 Python tuple 当作内核参数

#### 4.2.1 概念说明

tuple 参数解决的是「参数打包」问题。没有它时，要把一对张量传进内核只能写两个独立参数 `def k(a, b, out)`；有了它，可以写成 `def k(pair, out)` 然后用 `(a, b)` 作为单个参数传入，内核里用 `pair[0]`、`pair[1]` 访问。这对需要「一组同语义输入」（多头注意力的多个头、一组配置常量）的算子特别有用。

tuple 的每个元素都可以**独立**带有自己的注解：可以是普通数组、`IndexedWithInt64` 数组、普通标量、`ScalarInt64` 标量、`Constant` 常量，甚至嵌套另一个 tuple 或 list。编译期用 **`TupleConstraint`** 描述一个 tuple 参数，它持有「逐元素的约束」`items`。

两点关键约束：

1. tuple 只能是**纯 `tuple`**，不接受 `namedtuple` 等子类（子类会被拒绝，防止语义歧义）。
2. tuple 的**长度必须与注解匹配**（异质元组），多了少了都会报错；变长同质元组 `tuple[T, ...]` 则允许任意长度。

#### 4.2.2 核心流程

```
host:  ct.launch(stream, grid, k, ((a, b), out))
                              ↑ 这是一个 tuple 参数
        │
        │  运行时按注解树校验：长度、元素类型、是否纯 tuple
        ▼
TupleConstraint(items=(ArrayConstraint(...), ArrayConstraint(...)))
        │
        │  递归展开成扁平的 Var 序列进入内核函数体
        ▼
内核内：pair[0] → 第一个数组的视图；pair[1] → 第二个数组的视图
```

关于「什么时候触发重新编译」（这是 tuple + Constant 组合最实用的知识点）：tuple 里**只有被标记为 `Constant` 的元素**会进入编译期签名、影响缓存键；普通运行时元素（数组、运行时标量）改值不会触发重新编译。

#### 4.2.3 源码精读

最直观的用法见测试用例。一个 `tuple[Tensor, Tensor]` 内核：

- [test/test_tuple_arguments.py:L35-L39](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tuple_arguments.py#L35-L39)：`kernel_array_tuple` 接收 `pair`，用 `pair[0]`/`pair[1]` 分别 load 两个数组，对应 [启动时传入 `((a, b), out)`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tuple_arguments.py#L47)。

混合元组 `tuple[Tensor, int]`（数组 + 运行时标量）：

- [test/test_tuple_arguments.py:L51-L55](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tuple_arguments.py#L51-L55)：`kernel_mixed_tuple`，`pair[0]` 是数组、`pair[1]` 是标量。

`Constant` 与 tuple 的两种组合，体现「整组常量」与「部分常量」：

- [test/test_tuple_arguments.py:L111-L114](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tuple_arguments.py#L111-L114)：`ct.Constant[tuple]`——整组都是常量，`shape[0]` 编译期已知。
- [test/test_tuple_arguments.py:L145-L148](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tuple_arguments.py#L145-L148)：`tuple[ct.Constant[int], int]`——只有第 0 个元素是常量，`cfg[0]` 编译期已知、`cfg[1]` 是运行时标量。

`TupleConstraint` 在签名体系里的定义：

- [src/cuda/tile/compilation/_signature.py:L224-L236](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L224-L236)：`TupleConstraint` 持有 `items: tuple[ParameterConstraint, ...]`，构造时对每个元素调 `_to_constraint` 归一化。
- [src/cuda/tile/compilation/_signature.py:L274-L282](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L274-L282)：`_to_constraint` 实现 **plain tuple 简写**——传一个裸 `tuple` 会自动包成 `TupleConstraint`，裸 `int/float/bool` 会包成 `ConstantConstraint`。这就是 `KernelSignature` 文档里说的「plain tuple is shorthand for a TupleConstraint」。

错误情形同样有测试覆盖，帮助理解边界：

- [test/test_tuple_arguments.py:L421-L429](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tuple_arguments.py#L421-L429)：传入了比注解更长的 tuple 会报 "Received a tuple of length 3 for a parameter annotated as a tuple of length 2"。
- [test/test_tuple_arguments.py:L454-L464](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tuple_arguments.py#L454-L464)：传入 `namedtuple` 会报 "only plain tuple is accepted, not subclasses"。
- [test/test_tuple_arguments.py:L405-L418](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tuple_arguments.py#L405-L418)：`Constant[tuple]` 里混入数组元素会被拒——常量元组只能装「标量/元组常量」。

变长同质元组 `tuple[ct.Constant[int], ...]` 是 tuple 的高级用法，它的「同元素改值才重编译」行为有专门测试：

- [test/test_tuple_arguments.py:L302-L326](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tuple_arguments.py#L302-L326)：`test_tuple_with_variable_length_annotation`，连续传 `(1,2)`→`(1,2)`→`(3,2)`→`(3,4)`，`compile_tile` 调用次数从 1→1→2→3，证明每个常量元素都独立进入缓存键。

#### 4.2.4 代码实践

**实践目标**：写一个接收 `tuple[Tensor, Tensor]` 的两向量相加内核，验证 tuple 参数的打包与解包。

**操作步骤**：

```python
import cuda.tile as ct
import torch

@ct.kernel
def add_pair(pair, out):
    a = ct.load(pair[0], (0,), (8,))
    b = ct.load(pair[1], (0,), (8,))
    ct.store(out, (0,), a + b)

a = torch.ones(8, dtype=torch.float32, device="cuda")
b = torch.full((8,), 2.0, dtype=torch.float32, device="cuda")
out = torch.zeros(8, dtype=torch.float32, device="cuda")

# 把两个张量打包成一个 tuple 作为单个参数传入
ct.launch(torch.cuda.current_stream(), (1,), add_pair, ((a, b), out))
print(out)  # 期望全 3.0
```

**需要观察的现象**：`(a, b)` 被当作**一个**参数传入，内核内通过 `pair[0]`/`pair[1]` 解包成两个数组视图。

**预期结果**：`out` 全为 `3.0`。

**待本地验证**：需要 CUDA GPU 与已安装的 `cuda-tile`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `namedtuple` 会被拒绝？
**答案**：因为 cuTile 用「位置下标」`pair[0]`/`pair[1]` 访问 tuple，而 namedtuple 还带「字段名」语义，两者混用会产生歧义。源码用 `only plain tuple is accepted, not subclasses` 显式拒绝所有 tuple 子类。

**练习 2**：`tuple[ct.Constant[int], int]` 内核，先传 `(8, 5)` 再传 `(8, 9)`，会重新编译吗？先传 `(8, 5)` 再传 `(16, 5)` 呢？
**答案**：`(8, 5)`→`(8, 9)` **不会**重新编译（只有第 0 个元素是 Constant，第 1 个是运行时标量，改运行时标量不触发重编译）；`(8, 5)`→`(16, 5)` **会**重新编译（第 0 个 Constant 元素从 8 变成 16，改变了编译期签名）。这正是 [test_nested_tuple_partial_const_recompilation](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tuple_arguments.py#L263-L283) 验证的行为。

---

### 4.3 ArrayAnnotation.static_shape_dims：静态形状特化

#### 4.3.1 概念说明

默认情况下，数组的 `shape`（和 `strides`）是**运行时**值：内核启动时才知道具体是多少。这对编译器优化是个限制——很多优化（向量化访存、TMA 描述符、循环展开）在「shape 已知」时才能生效。

`ArrayAnnotation.static_shape_dims` 让你声明「这几个维度的 shape 值，请在编译期就当作常量」。它的效果和 `Constant` 类似：被特化的维度会**在启动时读出真实值，烘焙进 cubin**，于是编译器能基于具体数值做对齐推理（例如「这个维度是 16，所以能被 16 整除」）。

代价也和 `Constant` 一样：**不同的 shape 值会编译出不同的内核**。设 `static_shape_dims=(0,)`，那么 shape 第 0 维为 16 与为 32 会产生两份 cubin，JIT 缓存键不同。因此这个特性适合「shape 取值集合有限且反复出现」的场景（固定的 batch、固定的 head 数），不适合「shape 完全任意」的场景，否则会引发编译爆炸（参见 u3-l5 关于 Constant 编译爆炸的讨论）。

#### 4.3.2 核心流程

```
@ct.kernel
def k(x: Annotated[ct.Array, ct.ArrayAnnotation(static_shape_dims=(0, 1))], out):
    ...   # x.shape[0] 与 x.shape[1] 在内核内是编译期常量

host:  ct.launch(stream, grid, k, (x_16x16, out))   # shape=(16,16)
       ct.launch(stream, grid, k, (x_32x32, out))   # shape=(32,32) → 重新编译
       ct.launch(stream, grid, k, (x_16x16, out))   # shape=(16,16) → 命中第一份缓存
```

特化维度到「整除性事实」的转换可以形式化地理解为：若 `shape_constant[i] = S`，则编译器自动得知该维长度恒为 \(S\)，因而对任意因子 \(d\) 满足 \(d \mid S\) 时都能假设「该维被 \(d\) 整除」。即：

\[
\text{shape\_constant}[i] = S \;\Longrightarrow\; \forall d,\; (d \mid S) \Rightarrow (\text{shape}_i \bmod d = 0)
\]

这正是静态形状能驱动对齐优化的原因，也是 4.4 节里 `_validate_constraint_support` 与整除性 pass 的衔接点。

#### 4.3.3 源码精读

用户侧注解 `ArrayAnnotation` 的定义与文档：

- [src/cuda/tile/_stub.py:L998-L1036](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L998-L1036)：`ArrayAnnotation`，`static_shape_dims` 默认空元组 `()`（不特化任何维度），文档示例展示了 `(0,)` 与 `(0, -1)`（**负索引**：`-1` 指最后一维）两种写法。`__post_init__` 校验每个元素必须是 `int`（拒绝 `bool`）。

编译期签名里，静态形状对应 `ArrayConstraint.shape_constant`：

- [src/cuda/tile/compilation/_signature.py:L69-L75](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L69-L75)：`shape_constant` 字段文档，明确写出 **"Requires `cutile_python_v2`"**，并说明一旦设了 `shape_constant[i]`，对应的 `shape_divisible_by[i]` 就冗余了（必须与之兼容，否则报错）。
- [src/cuda/tile/compilation/_signature.py:L138-L157](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L138-L157)：构造器解析 `shape_constant`，并在 [`_remove_redundant_divisibility_constraints`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L498-L508) 里把整除性约束折叠成 1（因为常量已知，整除性已被吸收），同时校验兼容性。

「不同 shape 触发重新编译」有专门测试：

- [test/test_array.py:L117-L121](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_array.py#L117-L121)：`load_static_shaped`，注解 `static_shape_dims=(0, 1)`。
- [test/test_array.py:L124-L134](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_array.py#L124-L134)：`test_static_shape_standalone_recompile`，依次传 `(16,16)`、`(32,32)`、`(16,16)` 三种 shape，断言 `compile_tile` 只被调用 **2 次**——前两次各编译一份，第三次回到 `(16,16)` 命中缓存。这是「静态形状进入 JIT 缓存键」的最直接证据。

#### 4.3.4 代码实践

**实践目标**：复现 `test_static_shape_standalone_recompile` 的核心现象——观察静态形状如何改变编译次数。

**操作步骤**：

```python
# 示例代码：基于 test/test_array.py 的测试改写
import cuda.tile
from typing import Annotated
from unittest.mock import patch
import torch

@cuda.tile.kernel
def load_static_shaped(
        x: Annotated[cuda.tile.Array, cuda.tile.ArrayAnnotation(static_shape_dims=(0, 1))], out):
    t = cuda.tile.load(x, (0, 0), (16, 16))
    cuda.tile.store(out, (0, 0), t)

k = cuda.tile.kernel(load_static_shaped._pyfunc)
shapes = [(16, 16), (32, 32), (16, 16)]
with patch('cuda.tile._compile.compile_tile',
           side_effect=cuda.tile._compile.compile_tile) as mock_compile:
    for shape in shapes:
        x = torch.randint(0, 100, shape, dtype=torch.int32, device='cuda')
        out = torch.zeros((16, 16), dtype=torch.int32, device='cuda')
        cuda.tile.launch(torch.cuda.current_stream(), (1,), k, (x, out))
print("compile_tile 调用次数 =", mock_compile.call_count)  # 期望 2
```

**需要观察的现象**：三次启动只触发 2 次编译——`(16,16)` 编译一次，`(32,32)` 编译一次，最后的 `(16,16)` 命中第一份缓存。**对照实验**：去掉注解里的 `static_shape_dims`（或设为 `()`），同样的三次启动只会编译 1 次（因为 shape 不再进入缓存键）。

**预期结果**：`mock_compile.call_count == 2`。

**待本地验证**：需要 CUDA GPU；本断言直接对应仓库测试 `test_static_shape_standalone_recompile`。

#### 4.3.5 小练习与答案

**练习 1**：`static_shape_dims=(0, -1)` 在一个 3 维数组上分别特化了哪些维度？
**答案**：第 0 维（最外层）和最后一维（`-1` 即 `ndim-1`，对 3 维数组是第 2 维）。中间维度（第 1 维）仍是运行时值。

**练习 2**：如果把一个形状完全任意（每次都不同）的数组标上 `static_shape_dims=(0,)`，会有什么后果？
**答案**：每次出现新 shape 值都会触发一次重新编译，造成**编译爆炸**——和滥用 `Constant`（见 u3-l5）是同一类问题。静态形状只适合「取值集合有限且反复出现」的场景。

---

### 4.4 cutile_python_v2：承载 tuple 与静态形状的新调用约定

#### 4.4.1 概念说明

调用约定（calling convention）是 Python 与 GPU cubin 之间「参数怎么排列、怎么读取」的协议。`cutile_python_v1` 是旧协议——它在设计时只考虑了扁平的标量/数组/列表参数，**既不认识 tuple，也不认识静态形状**。

本次更新新增了 `cutile_python_v2`。它和 v1 的核心差异就是：**v2 才支持 tuple 参数与静态形状特化**。这是一个**编译期门控**：在构造 `KernelSignature` 时，校验函数 `_validate_constraint_support` 会逐参数检查——遇到 `TupleConstraint` 或带 `shape_constant` 的 `ArrayConstraint`，就要求 `calling_convention.version >= 2`，否则直接抛错。

为什么必须新协议？因为 tuple 与静态形状都改变了「参数在内存里怎么摆」这件事：tuple 需要递归布局、静态形状需要把 shape 值从运行时参数挪到编译期常量区。这些是 v1 的 ABI 容纳不下的，所以用一个新版本号显式区分。

#### 4.4.2 核心流程

```
JIT 启动 / AOT 导出
        │
        │  从注解树 + 运行时参数推断出 KernelSignature
        │  （tuple → TupleConstraint，static_shape_dims → shape_constant）
        ▼
KernelSignature.__init__ 对每个参数调 _validate_constraint_support(p, cconv)
        │
        ├─ TupleConstraint  且 cconv.version < 2 → 抛错 "version >= 2 is required"
        ├─ ArrayConstraint 带 shape_constant 且 cconv.version < 2 → 抛错
        └─ 否则通过
        ▼
按调用约定 mangling 出符号名 / 填充 cuLaunchKernel 参数
```

#### 4.4.3 源码精读

调用约定本身在 C++ 扩展的 Python 存根里定义：

- [src/cuda/tile/_cext.pyi:L77-L100](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cext.pyi#L77-L100)：`CallingConvention`，提供静态工厂 `cutile_python_v1()` / `cutile_python_v2()` / `from_code()`，以及 `name`、`code`、`version` 三个属性。注意 [TileDispatcher.__init__ 现在接收 `parameter_annotations`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cext.pyi#L55-L57)（即 4.1 节那棵注解树），而不是旧版的多个布尔掩码。

特性门控的核心校验函数：

- [src/cuda/tile/compilation/_signature.py:L511-L529](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L511-L529)：`_validate_constraint_support`，三条关键规则一目了然——`ArrayConstraint` 有 `shape_constant` 且 `cconv.version < 2` 报错；`TupleConstraint` 在 `cconv.version < 2` 报错；`ListConstraint` 与 `TupleConstraint` 都会**递归**校验其子约束（所以 tuple 里嵌套带静态形状的数组也会被正确门控）。
- [src/cuda/tile/compilation/_signature.py:L321-L338](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L321-L338)：`KernelSignature.__init__`，在归一化参数后对每个参数调一次 `_validate_constraint_support`，把校验集中在签名构造器里。

mangling 层面，tuple 与静态形状各有专属编码，且都可逆：

- [src/cuda/tile/compilation/_name_mangling.py:L146-L147](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_name_mangling.py#L146-L147)：tuple 约束用前缀 **`T`** 编码。
- [src/cuda/tile/compilation/_name_mangling.py:L376-L380](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_name_mangling.py#L376-L380)：tuple 的具体编码格式是「元素个数 + 各元素的 mangling 拼接」，可由 [`_demangle_tuple_constraint`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_name_mangling.py#L383-L388) 反推。
- [src/cuda/tile/compilation/_name_mangling.py:L190-L209](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_name_mangling.py#L190-L209)：数组的轴谓词编码里，[`shape_constant` 用谓词 **`s`** 收集](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_name_mangling.py#L198)（与 `i`/`t`/`v`/`l` 等其它轴假设并列），按 16 进制轴掩码分组拼到符号名里——这正是「静态形状进入缓存键/符号名」的实现机制。

#### 4.4.4 代码实践

**实践目标**：通过 AOT 导出接口，亲眼看到一个 tuple + 静态形状内核的 mangled 符号名里包含 `T`（tuple）和 `s`（静态形状）编码。

**操作步骤**（源码阅读型 + 待本地验证）：

```python
# 示例代码：观察 mangled name 中的 tuple/静态形状编码
import cuda.tile as ct
from cuda.tile import compilation
from typing import Annotated

@ct.kernel
def k(pair: tuple[ct.Array, ct.Array], out):
    pass

# 从示例参数推断签名（仅用于观察，生产环境应手写 KernelSignature）
# 注意：From_kernel_args 会从示例数组推导可能过强的假设，仅限测试/原型
import torch
a = torch.zeros(16, dtype=torch.float32, device="cuda")
b = torch.zeros(16, dtype=torch.float32, device="cuda")
out = torch.zeros(16, dtype=torch.float32, device="cuda")

sig = compilation.KernelSignature.from_kernel_args(
    k, ((a, b), out), compilation.CallingConvention.cutile_python_v2())
print("symbol =", sig.symbol)   # 期望符号名里出现 'T'（tuple 编码）
```

**需要观察的现象**：导出的符号名（mangled name）里包含 `T` 前缀，对应那个 tuple 参数；若把内核改成带 `static_shape_dims` 的数组参数，符号里会出现 `s` 谓词。对照 `_mangle_tuple_constraint` 与 `_mangle_array_constraint` 的源码即可解释每个字符。

**预期结果**：`sig.symbol` 是一个以函数名开头、含 `T` 编码的字符串；用 `compilation.demangle_kernel_name(sig.symbol)` 能反推出原始签名。

**待本地验证**：AOT 导出与 `from_kernel_args` 的精确符号格式以本地运行结果为准；本实践侧重「读懂符号里的 `T`/`s` 编码」这一观察目标。

#### 4.4.5 小练习与答案

**练习 1**：一个只含普通标量和普通数组（无 tuple、无 `static_shape_dims`）的内核，能用 `cutile_python_v1` 吗？
**答案**：能。`_validate_constraint_support` 对 `ScalarConstraint`、无 `shape_constant` 的 `ArrayConstraint`、`ConstantConstraint` 都直接放行，不检查 `version`。v1/v2 对这类「扁平」内核都成立。

**练习 2**：为什么 `TupleConstraint` 的校验要递归到 `items`，而不是只看顶层？
**答案**：因为 tuple 可以嵌套——一个 `tuple[tuple[ct.Array, ...], int]` 里可能藏着需要 v2 支持的结构（如内层带静态形状的数组）。只看顶层会漏掉深层的不兼容约束，所以 `_validate_constraint_support` 对 `TupleConstraint` 与 `ListConstraint` 都递归调用自身（见源码 L518-L525）。

---

## 5. 综合实践

把本讲的 tuple 参数与静态形状串起来，做一个「配置驱动的小内核」：

**任务**：写一个内核，接收一个 `tuple` 配置 `(a, b)`，其中 `a` 是输入数组（其第 0 维静态特化）、`b` 是一个 `ct.Constant[int]` 偏移；内核把 `a` 的前 8 个元素加上 `b` 后写到 `out`。要求：

1. 用 `tuple[Annotated[ct.Array, ct.ArrayAnnotation(static_shape_dims=(0,))], ct.Constant[int]]` 作为参数注解。
2. 通过 mock `compile_tile` 验证：
   - 改变输入数组的**第 0 维 shape**（如 8→16）会触发重新编译；
   - 只改变 `Constant` 偏移 `b`（如 5→7）也会触发重新编译；
   - 同样的 `(shape, b)` 组合重复启动不触发重新编译。

**参考实现骨架**：

```python
import cuda.tile as ct
from typing import Annotated
from unittest.mock import patch
import torch

@ct.kernel
def k(
    cfg: tuple[Annotated[ct.Array, ct.ArrayAnnotation(static_shape_dims=(0,))],
              ct.Constant[int]],
    out,
):
    t = ct.load(cfg[0], (0,), (8,))
    ct.store(out, (0,), t + cfg[1])

stream = torch.cuda.current_stream()
with patch('cuda.tile._compile.compile_tile',
           side_effect=ct._compile.compile_tile) as mock:
    for shape, b in [(8, 5), (8, 7), (16, 5), (8, 5)]:
        a = torch.arange(shape, dtype=torch.int32, device="cuda")
        out = torch.zeros(8, dtype=torch.int32, device="cuda")
        ct.launch(stream, (1,), k, ((a, b), out))
        print(shape, b, "->", mock.call_count)
# 预期调用次数序列：1, 2, 3, 3
# (8,5) 编译；(8,7) Constant 变 → 编译；(16,5) shape 变 → 编译；(8,5) 命中第一份缓存
```

**待本地验证**：精确的调用次数请以本地运行结果为准。这个练习同时兑现了三个最小模块——`TupleConstraint`（打包）、`static_shape_dims`（静态形状）、以及它们共同依赖的 `cutile_python_v2`。

## 6. 本讲小结

- cuTile 现在用一棵统一的 **`ParameterAnnotationNode`** 树（叶子 / 同质元组 / 异质元组）来表达每个参数的注解，取代了旧的扁平布尔掩码，这是支持 tuple 的前提。
- **tuple 参数**让你把一组相关参数打包成一个 Python `tuple` 传入，内核内用 `pair[i]` 解包；每个元素可独立带注解（数组/标量/`Constant`/嵌套 tuple/list），用 `TupleConstraint` 在签名层描述。
- **`ArrayAnnotation.static_shape_dims`** 把数组的指定维度特化为编译期常量（对应 `ArrayConstraint.shape_constant`），换取对齐优化机会，代价是不同 shape 会编译出不同 cubin、进入 JIT 缓存键。
- **`ct.Constant` 与 tuple 的两种组合**——`Constant[tuple[...]]`（整组常量）与 `tuple[Constant[...], ...]`（部分常量）——决定了哪些元素改值会触发重新编译；只有 Constant 元素进入缓存键。
- tuple 与静态形状都强制要求 **`cutile_python_v2`** 调用约定，由 `_validate_constraint_support` 在 `KernelSignature` 构造期递归门控；mangled 符号名里 tuple 用 `T`、静态形状用 `s` 编码，且可逆。
- 实践要点：tuple 只接受纯 `tuple`（拒绝 namedtuple 等子类）、长度必须匹配注解；静态形状只适合取值集合有限的维度，否则会编译爆炸。

## 7. 下一步学习建议

- **想看注解树如何驱动 IR 参数创建**：继续读 u5-l1（kernel 装饰器与 AnnotatedFunction）与 u5-l2（compile_tile 流水线），那里会讲 `_create_kernel_parameters` 如何递归展开这棵树。
- **想看静态形状如何变成对齐优化**：读 u6-l2（数据流分析与整除性传播），`shape_constant` 会被折叠成整除性事实喂给 `AssumeDivBy`。
- **想做 AOT 导出或读懂 mangled 名**：读 u8-l2（AOT 导出与内核签名/名称修饰），本讲的 `T`/`s` 编码在那里系统讲解。
- **想了解运行时如何按调用约定启动**：读 u8-l1（launch 与调度），讲 `cutile_python_v1`/`v2` 在 `cuLaunchKernel` 参数填充上的差异。
