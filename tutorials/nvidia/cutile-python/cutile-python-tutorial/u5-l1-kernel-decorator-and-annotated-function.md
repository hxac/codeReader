# kernel 装饰器与 AnnotatedFunction

> 所属单元：U5 编译前端——从 Python 到 Tile IR
> 依赖：u1-l4（顶层 API 全景）、u3-l1（load/store 范式）
> 关联：u3-l7（元组参数与静态形状特化）、u5-l2（compile_tile 流水线）

## 1. 本讲目标

本讲是「编译前端」单元的第一讲，回答一个贯穿全单元的问题：

> 当你写下 `@ct.kernel def my_kernel(...)` 时，Python 层到底**构造出了什么对象**？这些对象又携带了哪些信息交给后端编译器？

读完本讲，你应当能够：

1. 说清 `kernel` / `function` / `stub` 三类装饰器的语义差异与各自的执行空间标记。
2. 描述 `kernel` 对象上保存的两个核心字段：`_annotated_function`（参数注解）与 `_compiler_options`（编译旋钮）。
3. 理解 `AnnotatedFunction` 如何把 Python 类型注解解析成一棵 **`ParameterAnnotationNode` 树**，以及为什么这次重构要用「树」取代旧的「扁平布尔掩码」。
4. 掌握 `LeafAnnotationNode` / `HomogeneousTupleNode` / `HeterogeneousTupleNode` 三种节点如何统一表达 `Constant` / `Array` / `Scalar` / `List` 与 `tuple` 参数。
5. 理解 `TileDispatcher.__init__` 现在只接收 `parameter_annotations`（注解树），并由 C++ 侧镜像同一棵树。

本讲**不讲**编译流水线本身的执行顺序（那是 u5-l2 的主题），只聚焦「装饰阶段产出的对象模型」。

> 重构建说明：本讲原版本描述的是旧设计——`AnnotatedFunction` 用三个扁平布尔掩码（`constant_parameter_mask` / `int64_index_parameter_mask` / `int64_parameter_mask`）描述参数。那次重构之后，注解表示被替换为统一的 `ParameterAnnotationNode` 树以支持 tuple 参数与静态形状特化，`TileDispatcher.__init__` 也随之改为只接收注解树。本讲按新设计全面重写。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：装饰器产出的不是「函数」，而是「携带元数据的对象」。**
`@ct.kernel` 装饰一个函数后，得到的 `my_kernel` 不再是一个普通 `def`——它是一个继承自 `TileDispatcher` 的对象实例，内部保存着原始 Python 函数、参数注解、编译选项。它**不能被直接调用**（直接调用会抛错），只能交给 `ct.launch` 在 host 端 JIT 启动。这一点在 u1-l4、u3-l1 已经建立。

**直觉二：参数的「角色」是用 Python 类型注解表达的。**
cuTile 不像 CUDA C++ 那样区分 `__global__` / `__device__` 参数语义，而是借助 Python 的 `typing.Annotated` 机制，在类型注解里附加「元数据」。例如：

```python
# 示例代码：参数注解的几种写法（不是项目原有代码）
@ct.kernel
def k(
    a: ct.Array,                                   # 普通数组
    big: ct.IndexedWithInt64,                      # 用 int64 做 shape/stride 的数组
    n: ct.Constant[int],                           # 编译期常量
    pair: tuple[ct.Array, int],                    # 元组参数（一个数组 + 一个标量）
    ...
):
    ...
```

这些注解告诉编译器：哪个参数要常量嵌入、哪个要用 int64 索引、哪个参数是一个打包的元组。本讲要讲清楚的就是**这些注解如何被解析、归一成一种统一的内部表示**。

**直觉三：为什么需要「树」。**
在最近一次重构前，`AnnotatedFunction` 用**三个扁平的布尔掩码**记录每个参数：

```python
# 旧设计（已废弃，仅作对照，见 git 462fecc~1）
@dataclass
class AnnotatedFunction:
    constant_parameter_mask: Sequence[bool]       # 每个参数是否为 Constant
    int64_index_parameter_mask: Sequence[bool]    # 每个参数的数组索引是否用 int64
    int64_parameter_mask: Sequence[bool]          # 每个标量参数是否用 int64
```

「一个参数一个 bool」这套机制无法表达 `tuple[Constant[int], float]` 这种**嵌套结构**——一个参数内部既有常量元素又有非常量元素。为了支持元组参数（u3-l7）与静态形状特化，注解表示被重写为一棵递归的 `ParameterAnnotationNode` 树。本讲的 4.4 节会精读这棵树。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| `src/cuda/tile/_execution.py` | 装饰器定义 | `kernel` / `function` / `stub` 三个装饰器与 `kernel` 类的字段 |
| `src/cuda/tile/_annotated_function.py` | 注解解析 | `AnnotatedFunction`、`ParameterAnnotationNode` 树、`get_annotated_function` |
| `src/cuda/tile/_compiler_options.py` | 编译选项 | `CompilerOptions` dataclass 与校验 |
| `src/cuda/tile/_cext.pyi` | C++ 桥接类型存根 | `TileDispatcher.__init__` 签名、`CallingConvention` |
| `src/cuda/tile/_stub.py` | 注解元数据类 | `ConstantAnnotation` / `ArrayAnnotation` / `ScalarAnnotation` / `ListAnnotation` |
| `cext/tile_kernel.cpp` | C++ 运行时 | 镜像同一棵注解树的 `parse_parameter_annotation_node` |

记住一句话：**`_execution.py` 把注解交给 `_annotated_function.py` 解析成树，再把树连同编译选项一起存进 `kernel` 对象，最后由 C++ 的 `TileDispatcher` 镜像消费。**

## 4. 核心概念与源码讲解

### 4.1 kernel 装饰器：内核的 Python 入口

#### 4.1.1 概念说明

`kernel` 是 cuTile 最核心的装饰器：被它装饰的函数是一个 **tile kernel**——grid 中每个 block 都会完整执行一遍的函数体，是 tile code 的唯一入口。它有三个关键性质：

1. **它是类，不是普通函数装饰器**：`kernel` 继承自 C++ 扩展类型 `TileDispatcher`，装饰后得到的是该类的实例，承载元数据。
2. **它禁止直接调用**：直接 `my_kernel(...)` 会抛 `TypeError`，必须经 `ct.launch` 启动。
3. **它在装饰阶段就完成注解解析与选项构造**：等到真正 `launch` 时，这些信息已经被准备好，可以直接交给编译器。

`function` 与 `stub` 是另外两个装饰器：`function` 标记一个可被 tile code 调用的辅助函数（声明执行空间），`stub` 在 `function` 基础上再加一个 `_cutile_python_stub` 标记，表明这是「签名在前端、实现在后端」的内置操作（如 `ct.add`、`ct.load`）。

#### 4.1.2 核心流程

`@ct.kernel` 装饰一个函数 `f` 时发生的事情：

```text
@ct.kernel(occupancy=2)
def f(...): ...
        │
        ▼
kernel.__new__(f, occupancy=2)        # 支持裸 @ct.kernel 与带参 @ct.kernel(...) 两种写法
        │
        ▼
kernel.__init__(f, occupancy=2)
        │
        ├─ 校验 f 是普通函数 (FunctionType)
        ├─ ann_func = get_annotated_function(f)        # 解析注解 → ParameterAnnotationNode 树
        ├─ compiler_options = CompilerOptions(occupancy=2, ...)  # 构造编译选项
        ├─ super().__init__(ann_func.parameter_annotations)     # 把注解树交给 C++ TileDispatcher
        ├─ self._annotated_function = ann_func                   # 保存注解产物
        └─ self._compiler_options = compiler_options             # 保存编译选项
        │
        ▼
得到一个 kernel 实例，禁用 __call__，等待 ct.launch
```

注意一个细节：`super().__init__(...)` 调用的是 C++ 扩展类型 `TileDispatcher` 的初始化函数，它把注解树**拷贝到 C++ 侧**，因为后续的签名推断、参数约束、cubin 生成都在 C++ 运行时里完成。

#### 4.1.3 源码精读

`kernel` 类的声明与文档字符串，说明它是 tile code 入口且只能用 `launch` 启动，并列出全部编译选项参数：

[`_execution.py:61-92`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L61-L92) —— `kernel` 继承 `TileDispatcher`，文档列出了 `num_ctas` / `occupancy` / `opt_level` / `num_worker_warps` 四个编译期旋钮，并支持 `ByTarget` 做目标特定的取值。

`__new__` 用一个技巧同时兼容「裸装饰」与「带参数装饰」两种写法：

[`_execution.py:93-99`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L93-L99) —— 当 `function is None`（即写成 `@ct.kernel(occupancy=2)`）时返回一个 `decorate` 闭包，等拿到真正的函数再调用 `kernel(func, **kwargs)`。

构造的核心在 `__init__`，这是本讲最重要的几行：

[`_execution.py:101-123`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L101-L123) —— 先校验入参是 `FunctionType`；再延迟导入 `CompilerOptions` 与 `get_annotated_function`（避免循环导入）；用 `get_annotated_function(function)` 把函数解析成 `AnnotatedFunction`；用四个旋钮构造 `CompilerOptions`；调用 `super().__init__(ann_func.parameter_annotations)` 把注解树交给 C++ 基类；最后把 `ann_func` 与 `compiler_options` 存到 `self` 上。**这两行赋值（`self._annotated_function`、`self._compiler_options`）就是 kernel 对象持久携带的全部前端元数据。**

`__call__` 显式禁用直接调用：

[`_execution.py:168-169`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L168-L169) —— 任何对 kernel 实例的直接调用都抛 `TypeError`，提示用 `cuda.tile.launch()`。

`_compile` 是真正 `launch` 时才会被回调到的方法（本讲只看它的签名，执行细节留给 u5-l2）：

[`_execution.py:125-130`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L125-L130) —— 它把 `self._annotated_function`、SM 架构、`self._compiler_options` 与 `TileContext` 一起传给 `compile_tile`，取回 cubin 与符号名。可以理解为：**装饰阶段存进去的字段，启动阶段取出来用**。

`function` 与 `stub` 装饰器：

[`_execution.py:25-58`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L25-L58) —— `function(host=False, tile=True)` 声明执行空间；当 `host=False`（默认）时，包一层 wrapper，让从 host 端调用它时经 `DispatchMode` 转发到 tile code，并打上 `_cutile_function_wrapper` 标记。

[`_execution.py:172-181`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L172-L181) —— `stub` 先复用 `function`，再追加 `_cutile_python_stub = True` 标记。后端的注册系统（u5-l7）正是靠这个标记识别「哪些 API 需要对接到 IR 实现」。

#### 4.1.4 代码实践

**实践目标**：亲手观察一个 `kernel` 实例上保存的字段，验证它确实「不是普通函数」。

**操作步骤**（假设你已按 u1-l2 安装好 cuTile）：

```python
# 示例代码：自检脚本，可保存为 inspect_kernel.py
import cuda.tile as ct

@ct.kernel(occupancy=2, opt_level=3)
def my_kernel(a: ct.Array, out: ct.Array, n: ct.Constant[int]):
    t = ct.load(a, (0,), (n,))
    ct.store(out, (0,), t + 1)

# 1. 它是什么类型？
print(type(my_kernel).__mro__)
# 2. 直接调用应当抛错
try:
    my_kernel()
except TypeError as e:
    print("直接调用被拒：", e)
# 3. 观察装饰阶段存进去的字段
print("compiler_options :", my_kernel._compiler_options)
print("annotated_function 的参数注解树：")
for i, node in enumerate(my_kernel._annotated_function.parameter_annotations):
    print(f"  参数 {i}: {node}")
```

**需要观察的现象**：

- `type(my_kernel).__mro__` 里应出现 `cuda.tile._cext.TileDispatcher`，证明它是该 C++ 类型的实例。
- 直接调用会抛 `TypeError: Tile kernels cannot be called directly...`。
- `_compiler_options` 是一个 `CompilerOptions(occupancy=2, opt_level=3, ...)` 的 dataclass 实例。
- 参数注解树会打印出三个节点，其中第三个参数（`n`）的节点是 `LeafAnnotationNode(constant=True, ...)`。

**预期结果**：如果环境正常，前两项的输出是确定的；后两项的打印格式取决于 dataclass 默认 `__repr__`（`LeafAnnotationNode` / `HomogeneousTupleNode` / `HeterogeneousTupleNode` 的字段名）。若你暂未配置好 GPU 运行环境，前两步不依赖 CUDA 也能验证，**待本地确认后两项在 dataclass repr 下的具体打印形式**。

#### 4.1.5 小练习与答案

**练习 1**：`@ct.kernel` 与 `@ct.kernel(occupancy=2)` 两种写法为什么都能工作？是哪段代码处理的？

**答案**：`kernel.__new__` 在 `function is None` 时返回一个 `decorate` 闭包，从而把「带参装饰」延迟到拿到真正函数的那一刻，再调用 `kernel(func, **kwargs)`（见 [`_execution.py:93-99`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L93-L99)）。

**练习 2**：为什么 `kernel` 的 `__init__` 里要用延迟导入（`from cuda.tile._compiler_options import CompilerOptions`），而不是写在文件顶部？

**答案**：为了避免循环导入。`_execution` 与 `_annotated_function`、`_compiler_options` 之间存在相互引用关系，把导入推迟到函数体内能打破「模块加载时」的循环依赖，只在实际装饰函数时才解析这些符号。

---

### 4.2 CompilerOptions：编译期可调旋钮

#### 4.2.1 概念说明

`CompilerOptions` 是一个**冻结的 dataclass**（`frozen=True`），承载四个编译期可调旋钮。它对应 `kernel` 装饰器的四个参数，描述「这个内核希望编译器怎么编」：

| 字段 | 含义 | 取值 |
|------|------|------|
| `num_ctas` | CGA（线程块集群）中的 CTA 数 | 1–16 的 2 的幂 |
| `occupancy` | 每 SM 上预期活跃 CTA 数 | 1–32 |
| `opt_level` | 优化级别 | 0–3，默认 3 |
| `num_worker_warps` | warp-specialized 内核中 CUDA core warp 组的 warp 数 | 4 或 8（CTK 13.3+） |

每个字段还支持 `ByTarget[int]`，即「针对不同 SM 架构给不同取值」。

#### 4.2.2 核心流程

`CompilerOptions` 的构造流程很简洁：

```text
CompilerOptions(occupancy=2, ...)
        │
        ▼
__post_init__ 遍历每个字段
        │
        ├─ 若字段是 ByTarget：对每个目标值 + default 分别校验
        └─ 否则：直接校验该值
        │
        ▼
校验函数 _validate_<field> 按范围/集合约束做检查，越界抛 ValueError
        │
        ▼
得到一个不可变、已校验的 CompilerOptions 实例
```

它对外提供两个查询方法：`hints_by_target()` 把所有字段按目标拆成 `{target: {field: value}}`；`opt_level_for_target(name)` 取特定目标的优化级别。

#### 4.2.3 源码精读

dataclass 定义与四个字段：

[`_compiler_options.py:16-22`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compiler_options.py#L16-L22) —— 四个字段全部带默认值，`frozen=True` 保证装饰后选项不会被意外篡改（改动需走 `kernel.replace_hints` 创建新实例）。

`__post_init__` 用一个巧妙的小技巧——按字段名动态查找校验函数：

[`_compiler_options.py:23-33`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compiler_options.py#L23-L33) —— `globals()[f"_validate_{field.name}"]` 按命名约定（`_validate_num_ctas` 等）取出校验器；对 `ByTarget` 还会展开它的每个目标值与默认值分别校验。

四个校验器实现范围/集合约束：

[`_compiler_options.py:61-86`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compiler_options.py#L61-L86) —— 例如 `num_ctas` 须在 `[1,16]` 且为 2 的幂（用 `num_ctas & (num_ctas-1) == 0` 判定），`num_worker_warps` 只能取 4 或 8。

`kernel.replace_hints` 正是利用 `dataclasses.replace` 在不可变 dataclass 上派生新选项：

[`_execution.py:136-166`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L136-L166) —— `replace_hints(occupancy=4)` 生成一个新的 `kernel` 实例；因为编译选项影响 cubin，所以新 kernel 拥有独立的 JIT 缓存。

#### 4.2.4 代码实践

**实践目标**：体会 `frozen=True` 与校验机制。

**操作步骤**：

```python
# 示例代码
from cuda.tile._compiler_options import CompilerOptions

opt = CompilerOptions(occupancy=2, opt_level=3)
print(opt)
try:
    opt.occupancy = 4          # frozen，应抛 FrozenInstanceError
except Exception as e:
    print("不可变：", type(e).__name__, e)
try:
    CompilerOptions(num_ctas=3)   # 非 2 的幂，应抛 ValueError
except ValueError as e:
    print("校验拒绝：", e)
```

**需要观察的现象**：第一次赋值被冻结拒绝；第二次构造因 `num_ctas=3` 不是 2 的幂被拒绝。

**预期结果**：`num_ctas` 校验信息为 `num_ctas should be power of 2, got 3`（见 [`_compiler_options.py:65-66`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compiler_options.py#L65-L66)）。`frozen` 抛出的异常类型是 dataclass 标准的 `dataclasses.FrozenInstanceError`（`AttributeError` 子类），**待本地确认具体异常名**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CompilerOptions` 要做成 `frozen=True`？

**答案**：编译选项是 kernel 的身份标识之一，参与 JIT 缓存键。一旦允许就地修改，会导致「同一个 kernel 对象前后对应不同的 cubin」，破坏缓存一致性。需要改动时，应通过 `replace_hints` 派生**新**对象，让它有独立的缓存。

**练习 2**：`num_worker_warps` 的注释说「CTK 13.3 才生效，否则带警告忽略」。如果不满足版本，传了非法值（比如 6）会怎样？

**答案**：`_validate_num_worker_warps` 在构造期就会拒绝 6（只允许 4 或 8），抛 `ValueError`。版本门控只影响「合法值是否被编译器实际采用」，不影响构造期校验。

---

### 4.3 AnnotatedFunction：参数注解的解析产物

#### 4.3.1 概念说明

`AnnotatedFunction` 是「被装饰的 Python 函数 + 解析后的参数注解」的打包体。它做了一件关键的事：把 Python 那套灵活但松散的类型注解（`typing.Annotated`、`tuple[...]`、裸类型），归一成 cuTile 自己的内部数据结构——一棵 `ParameterAnnotationNode` 树。

它的字段只有三个：

```python
@dataclass
class AnnotatedFunction:
    pyfunc: FunctionType                                  # 原始 Python 函数
    pysig: inspect.Signature                              # 函数签名（用于参数名/顺序）
    parameter_annotations: Sequence[ParameterAnnotationNode]  # 每个参数对应一棵子树
```

注意：`parameter_annotations` 是**按参数位置**排列的序列，序列长度等于参数个数，序列里每个元素是该参数对应的注解树根节点。

#### 4.3.2 核心流程

`get_annotated_function(pyfunc)` 的解析流程：

```text
get_annotated_function(f)
        │
        ├─ sig = inspect.signature(f)            # 拿到参数顺序
        ├─ hints = typing.get_type_hints(f, include_extras=True)
        │        └─ 关键：include_extras=True 保留 Annotated 的元数据
        │        └─ 同时解析 from __future__ import annotations 的字符串注解
        ├─ 对每个参数，取其类型注解 ann
        └─ parameter_annotations = tuple(_build_annotation_node(ann) for ann in ...)
                    │
                    ▼
              _build_annotation_node 把注解递归构建成节点（详见 4.4）
```

这里有一个**易错点**：必须用 `typing.get_type_hints(..., include_extras=True)` 而不是直接读 `param.annotation`。因为：(1) 如果源码写了 `from __future__ import annotations`，所有注解会变成字符串，`get_type_hints` 才会真正求值；(2) 默认的 `get_type_hints` 会**剥掉** `Annotated` 的元数据，必须 `include_extras=True` 才能保住 `ConstantAnnotation()` 等元数据。

#### 4.3.3 源码精读

`AnnotatedFunction` dataclass 定义：

[`_annotated_function.py:55-59`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L55-L59) —— 三个字段。注意它取代了旧设计里的三个布尔掩码字段；旧字段 `constant_parameter_mask` / `int64_index_parameter_mask` / `int64_parameter_mask` 已不存在。它是普通 `@dataclass`（非 frozen），因为内部代码有时会就地调整；对外的不可变性由 `kernel` 层保证。

`get_annotated_function` 的实现：

[`_annotated_function.py:62-70`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L62-L70) —— 第 64 行注释专门解释了为什么要用 `get_type_hints`；第 66 行用 `hints.get(name, param.annotation)` 做了一个 fallback：拿不到 hint 时退回签名里的原始注解；第 67 行对每个注解调用 `_build_annotation_node` 构建节点。

这正好是 `kernel.__init__` 里 `get_annotated_function(function)` 的产物，存入 `self._annotated_function`（见 [`_execution.py:114`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L114)）。

#### 4.3.4 代码实践

**实践目标**：直接调用 `get_annotated_function`，观察它对同一组参数注解的解析结果，绕开 GPU 依赖。

**操作步骤**：

```python
# 示例代码：纯前端，不需要 GPU
import cuda.tile as ct
from cuda.tile._annotated_function import get_annotated_function

def f(a: ct.Array, out: ct.Array, n: ct.Constant[int]):
    pass

ann = get_annotated_function(f)
print("参数个数:", len(ann.parameter_annotations))
for i, node in enumerate(ann.parameter_annotations):
    print(f"  参数 {i} 节点 KIND = {node.KIND}")
    if node.KIND == "leaf":
        print(f"     constant={node.constant}, array={node.array}, scalar={node.scalar}")
```

**需要观察的现象**：前两个参数的节点 `KIND == "leaf"`、`constant=False`；第三个参数 `n` 的节点 `constant=True`。

**预期结果**：可确定的是第三个参数 `constant=True`（来自 [`_build_annotation_node`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L81-L94) 检测到 `ConstantAnnotation` 元数据）。裸 `ct.Array` 不经 `Annotated` 包装时，节点会是 `LeafAnnotationNode(constant=False)`，`array/scalar/list` 均为 `None`——**待本地确认**这一表现。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `include_extras=True` 去掉，会发生什么？

**答案**：`typing.get_type_hints` 默认会剥离 `Annotated[X, meta]` 里的 `meta`，只返回 `X`。于是 `ConstantAnnotation()` 等元数据全部丢失，`_build_annotation_node` 拿不到 `ConstantAnnotation`，会把本该是常量的参数误判为非常量。这正是第 65 行必须加 `include_extras=True` 的原因。

**练习 2**：`parameter_annotations` 这个序列的长度由什么决定？

**答案**：由函数的参数个数决定。它是一个位置对齐的序列——第 `i` 个元素就是第 `i` 个参数的注解树根节点。

---

### 4.4 ParameterAnnotationNode 树：统一的注解表示（核心）

> 这是本次重构的核心，也是本讲的重点。

#### 4.4.1 概念说明

`ParameterAnnotationNode` 是一个**联合类型**，由三种节点构成一棵树：

| 节点类型 | `KIND` | 字段 | 表达什么 |
|----------|--------|------|---------|
| `LeafAnnotationNode` | `"leaf"` | `constant` / `scalar` / `array` / `list` | 一个**标量/数组**参数的注解 |
| `HomogeneousTupleNode` | `"homogeneous_tuple"` | `each` | 一个**变长同质元组** `tuple[T, ...]`，所有元素共享同一注解 |
| `HeterogeneousTupleNode` | `"heterogeneous_tuple"` | `items` | 一个**定长异质元组** `tuple[A, B, ...]`，每个位置注解不同 |

**叶子节点** `LeafAnnotationNode` 是基础，它用四个字段表达「传统」参数的所有可能角色：

- `constant: bool`——是否常量嵌入（`ct.Constant`）。
- `scalar: ScalarAnnotation | None`——标量的 dtype 提示（`ct.ScalarInt64`）。
- `array: ArrayAnnotation | None`——数组的索引 dtype 与静态形状（`ct.IndexedWithInt64`、`ArrayAnnotation(static_shape_dims=...)`）。
- `list: ListAnnotation | None`——列表元素的数组注解（`ct.List[...]`）。

`validate()` 方法强制一个约束：这四种「角色」**互斥**，一个叶子节点只能扮演其中一种。例如 `Constant` 与 `ScalarAnnotation` 不能同时出现在一个参数上（见测试 `test_constant_i64_scalar_tuple_arg` 期望的报错信息 `Constant annotation cannot be combined with ScalarAnnotation/ScalarInt64`）。

**元组节点**是新增能力。`tuple[A, B]` 这种定长异质元组被建成 `HeterogeneousTupleNode`，`items` 是各位置子节点的元组；`tuple[T, ...]` 这种变长同质元组被建成 `HomogeneousTupleNode`，`each` 是元素子节点。子节点本身又可以是叶子或元组，从而支持任意嵌套（测试里有 `((2, 3), 5)` 这种嵌套元组）。

为什么是「树」而不是「扁平掩码」？因为元组参数让一个参数位置内部出现了**结构**——`tuple[Constant[int], float]` 的第一个元素是常量、第二个不是，扁平的「一个参数一个 bool」表达不了。树能递归地描述任意嵌套，是支持元组参数（u3-l7）的前提。

#### 4.4.2 核心流程

注解到节点的构建由两个互相递归的函数完成。判定一条注解建成什么节点，关键是看它的「形状」：

```text
_build_annotation_node(ann, outer_constant=False)
        │
        ├─ 若 ann 是 Annotated[X, meta...]：
        │     ├─ inner = X, meta = 元数据列表
        │     ├─ is_constant = outer_constant 或 meta 含 ConstantAnnotation
        │     ├─ 若 inner 是 tuple → _build_tuple_node(inner, is_constant)
        │     └─ 否则 → LeafAnnotationNode(constant=is_constant,
        │                                   array=_get_annotation(meta, ArrayAnnotation),
        │                                   scalar=_get_annotation(meta, ScalarAnnotation),
        │                                   list =_get_annotation(meta, ListAnnotation))
        │
        ├─ 若 ann 是 tuple（无 Annotated 包装）→ _build_tuple_node(ann, outer_constant)
        │
        └─ 否则 → LeafAnnotationNode(constant=outer_constant)


_build_tuple_node(ann, outer_constant)
        │
        ├─ args = get_args(ann)
        ├─ 若 args == (T, Ellipsis) → HomogeneousTupleNode(_build_annotation_node(T, outer_constant))
        │                              # tuple[T, ...]：变长同质
        └─ 否则 → HeterogeneousTupleNode(tuple(_build_annotation_node(arg, outer_constant) for arg in args))
                                       # tuple[A, B, ...]：定长异质
```

`outer_constant` 参数承担「常量性向下传播」：当外层是 `ct.Constant[tuple[...]]` 时，`is_constant=True` 会作为 `outer_constant` 传给子节点，使整个元组（包括所有后代叶子）都被标记为常量。这就是「整组常量」与「部分常量」的实现机制：

- `ct.Constant[tuple[int, float]]`——外层 Constant，`outer_constant` 向下传，所有叶子 `constant=True`。
- `tuple[ct.Constant[int], float]`——只有第一个元素带 ConstantAnnotation，只有那个叶子 `constant=True`。

举个具体例子，对参数 `cfg: tuple[ct.Constant[int], int]`，构建出的树是：

```text
HeterogeneousTupleNode
├─ items[0]: LeafAnnotationNode(constant=True)     ← Constant，进入 JIT 缓存键
└─ items[1]: LeafAnnotationNode(constant=False)    ← 普通 int，不进缓存键
```

#### 4.4.3 源码精读

三个节点类的定义：

[`_annotated_function.py:14-35`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L14-L35) —— `LeafAnnotationNode`，注意 `validate()` 列出 `given` 列表后用 `len(given) > 1` 拒绝组合，错误信息就是测试里期望的那句。

[`_annotated_function.py:38-42`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L38-L42) —— `HomogeneousTupleNode`，只有一个 `each` 字段。

[`_annotated_function.py:45-49`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L45-L49) —— `HeterogeneousTupleNode`，`items` 是子节点元组。

[`_annotated_function.py:52`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L52) —— 联合类型定义 `ParameterAnnotationNode = LeafAnnotationNode | HomogeneousTupleNode | HeterogeneousTupleNode`。

两个构建函数：

[`_annotated_function.py:73-78`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L73-L78) —— `_build_tuple_node` 用 `args[1] is ...` 区分变长同质（`tuple[T, ...]`）与定长异质。

[`_annotated_function.py:81-94`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L81-L94) —— `_build_annotation_node` 是主入口，三段分支处理 `Annotated`、裸 `tuple`、其它；注意第 85 行 `is_constant = outer_constant or any(...)`，这是常量性「或」传播的关键；第 94 行的兜底 `LeafAnnotationNode(constant=outer_constant)` 处理完全没有注解的参数（如裸 `int`）。

`_get_annotation` 辅助函数从元数据列表里挑出指定类型的注解：

[`_annotated_function.py:100-104`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L100-L104) —— 简单的 `isinstance` 过滤，返回第一个匹配项或 `None`。

可以对照测试理解这些节点对应的真实用法：

- `tuple[Tensor, int]`（异质）→ [`test_tuple_arguments.py:51-63`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tuple_arguments.py#L51-L63) 中的 `kernel_mixed_tuple`。
- `tuple[ct.Constant[int], int]`（部分常量）→ [`test_tuple_arguments.py:145-156`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tuple_arguments.py#L145-L156) 中的 `kernel_partial_const_first`。
- `ct.Constant[tuple[...]]`（整组常量）→ [`test_tuple_arguments.py:111-122`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tuple_arguments.py#L111-L122) 中的 `kernel_constant_tuple`。

#### 4.4.4 代码实践

**实践目标**：给一个含 tuple 参数的内核**手画** `ParameterAnnotationNode` 树，并用 `get_annotated_function` 验证。

**操作步骤**：

```python
# 示例代码：纯前端，不需要 GPU
import cuda.tile as ct
from cuda.tile._annotated_function import get_annotated_function

def k(pair: tuple[ct.Constant[int], int], out):
    pass

ann = get_annotated_function(k)
root = ann.parameter_annotations[0]      # pair 参数
print("根节点 KIND:", root.KIND)          # 期望 heterogeneous_tuple
print("items:", root.items)
for i, item in enumerate(root.items):
    print(f"  子节点 {i} KIND={item.KIND}, constant={getattr(item, 'constant', None)}")
```

**需要观察的现象**：根节点 `KIND == "heterogeneous_tuple"`，有两个子节点；子节点 0 是 `constant=True` 的叶子，子节点 1 是 `constant=False` 的叶子。

**预期结果**：依据 [`_build_annotation_node`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L81-L94)，`tuple[ct.Constant[int], int]` 会被建成：

```text
HeterogeneousTupleNode
├─ items[0]: LeafAnnotationNode(constant=True)    # ct.Constant[int]
└─ items[1]: LeafAnnotationNode(constant=False)   # 裸 int
```

这是确定的，但 dataclass `__repr__` 的具体字符串形式**待本地确认**。

**延伸练习**：把注解换成 `ct.Constant[tuple[int, int]]`（整组常量），再观察两个子节点的 `constant` 值——依据 `outer_constant` 的向下传播，两者都应是 `True`。

#### 4.4.5 小练习与答案

**练习 1**：`tuple[int, int]` 和 `tuple[int, ...]` 分别建成什么节点？区别在哪？

**答案**：前者建成 `HeterogeneousTupleNode`（`items` 是两个叶子节点的元组，定长 2）；后者建成 `HomogeneousTupleNode`（只有一个 `each` 字段，元素数量在运行时可变）。区分依据是 [`_build_tuple_node`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py#L73-L78) 里 `args[1] is ...` 的判定——`...`（Ellipsis）表示变长。

**练习 2**：`ct.Constant[tuple[int, float]]` 与 `tuple[ct.Constant[int], float]` 在树上的差别是什么？这会导致什么行为差异？

**答案**：前者是「整组常量」——`outer_constant=True` 向下传播，**两个**叶子都是 `constant=True`，整个元组的取值都进入 JIT 缓存键；后者是「部分常量」——只有第一个叶子 `constant=True`，第二个不是。行为差异：前者改变任何一个元素的取值都触发重新编译；后者只有改变第一个元素才触发，第二个元素的变化不重新编译。这正是 u3-l7 讲的「Constant 与 tuple 的两种组合」。

**练习 3**：为什么 `LeafAnnotationNode.validate()` 要禁止 `constant=True` 同时带 `ScalarAnnotation`？

**答案**：`Constant` 参数会被「常量嵌入」烘焙进 cubin、从运行时签名消失（见 u3-l5），而 `ScalarAnnotation`（如 `ScalarInt64`）是在描述运行时标量的 dtype。两者语义矛盾——一个已经不存在于运行时的参数，再去约束它的运行时 dtype 没有意义。测试 `test_constant_i64_scalar_tuple_arg` 正好验证这条禁令（[`test_tuple_arguments.py:125-142`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tuple_arguments.py#L125-L142)）。

---

### 4.5 TileDispatcher：把注解树桥接到 C++ 运行时

#### 4.5.1 概念说明

`kernel` 继承的 `TileDispatcher` 是一个 **C++ 扩展类型**（定义在 `cext` 里，见 u1-l3）。它是 Python 前端与 C++ 运行时之间的桥：装饰阶段把注解树交给它，启动阶段由它驱动签名推断、JIT 编译、`cuLaunchKernel`。

本次重构的一个重要外部表现是：**`TileDispatcher.__init__` 的签名简化了**。它现在只接收一个参数 `parameter_annotations`（注解树序列），而不再是过去那一堆布尔掩码。相应地，C++ 侧也镜像了同一棵 `ParameterAnnotationNode` 树。

另一个相关的桥接概念是 `CallingConvention`（调用约定）。`cutile_python_v1` 是原始调用约定，`cutile_python_v2` 是为支持 tuple 参数与静态形状特化而引入的新约定——它在 `KernelSignature` 构造期被门控（u8-l1、u8-l2 详讲）。本讲只需知道：注解树里的 tuple/静态形状信息，最终会触发 v2 调用约定。

#### 4.5.2 核心流程

注解树如何「跨过」Python/C++ 边界：

```text
kernel.__init__
        │
        ├─ ann_func = get_annotated_function(f)        # Python 侧建树
        └─ super().__init__(ann_func.parameter_annotations)   # 把树传给 C++
                    │
                    ▼
        TileDispatcher_init (C++, cext/tile_kernel.cpp)
                    │
                    ├─ 取出 py_parameter_annotations
                    └─ parse_parameter_annotation_nodes_seq(...)
                                │
                                ▼
                    对每个 Python 节点递归调用 parse_parameter_annotation_node
                    按 KIND 字符串 ("leaf"/"homogeneous_tuple"/"heterogeneous_tuple")
                    分派，构建对应的 C++ 节点，存进 C++ TileDispatcher
```

注意：Python 侧的节点用 `KIND` 字符串字段做「自描述」，C++ 侧靠读取这个字段来分派。这是一种轻量的、不依赖 pybind 类型系统的跨语言结构映射。

#### 4.5.3 源码精读

Python 侧的桥接签名（类型存根）：

[`_cext.pyi:55-57`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cext.pyi#L55-L57) —— `TileDispatcher.__init__(self, parameter_annotations: Sequence)`，只接收注解树这一个参数。

`kernel.__init__` 里的调用点：

[`_execution.py:121`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L121) —— `super().__init__(ann_func.parameter_annotations)`。

C++ 侧的镜像解析，按 `KIND` 分派：

[`cext/tile_kernel.cpp:2831-2891`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2831-L2891) —— `parse_parameter_annotation_node` 读取 `KIND`，对 `"leaf"` 取 `constant/scalar/array/list` 四个字段建 `LeafAnnotationNode`；对 `"homogeneous_tuple"` 取 `each` 递归；对 `"heterogeneous_tuple"` 遍历 `items` 递归。这正好是 Python 侧三种节点的镜像。

`TileDispatcher_init` 入口：

[`cext/tile_kernel.cpp:2979-2993`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2979-L2993) —— 从 Python 参数取出 `py_parameter_annotations`，调用 `parse_parameter_annotation_nodes_seq` 解析成 C++ 节点序列，存进 `TileDispatcher`。

调用约定的两个版本（类型存根）：

[`_cext.pyi:77-100`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cext.pyi#L77-L100) —— `CallingConvention` 提供 `cutile_python_v1()` / `cutile_python_v2()` 两个静态工厂，以及 `version` 属性。注解树里的 tuple 与静态形状最终要求 v2。

#### 4.5.4 代码实践

**实践目标**：验证 `TileDispatcher` 接收的是注解树（而非多个布尔掩码），并理解 C++ 镜像。

**操作步骤**（源码阅读型实践，不需要 GPU）：

1. 在 [`_execution.py:121`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L121) 处确认 `super().__init__` 只传了一个参数 `ann_func.parameter_annotations`。
2. 跟到 [`cext/tile_kernel.cpp:2831-2891`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2831-L2891)，对照三种 `KIND` 的分派分支，与 [`_annotated_function.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_annotated_function.py) 的三个节点类一一对应。
3. 想一个反例：如果 Python 侧传过来一个 `KIND="unknown"` 的节点，C++ 会在哪一行报错？

**需要观察的现象**：Python 三种节点与 C++ 三个分支严格一一对应；任何未知 `KIND` 都会落到 C++ 的 `else` 分支抛 `TypeError`。

**预期结果**：可确定 C++ 在 [`tile_kernel.cpp:2886-2889`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2886-L2889) 抛出 `expected a ParameterAnnotationNode (leaf/homogeneous_tuple/heterogeneous_tuple), got KIND=...`。

#### 4.5.5 小练习与答案

**练习 1**：为什么 Python 节点要带一个字符串字段 `KIND`，而不是靠 C++ 用 `isinstance` 判断 Python 类型？

**答案**：因为这些是普通 Python `dataclass`，没有注册到 C++ 的类型系统里（cuTile 的 cext 没有用 pybind11 自动绑定这些类）。用 `KIND` 字符串做自描述，让 C++ 侧只靠 `getattr(obj, "KIND")` + 字符串比较就能分派，是一种轻量、解耦的跨语言方案。

**练习 2**：`cutile_python_v1` 与 `cutile_python_v2` 调用约定的本质区别（本讲层面）是什么？

**答案**：v1 是面向「扁平参数 + 扁平布尔掩码」时代的调用约定；v2 才支持 tuple 参数与静态形状特化。当注解树里出现 tuple 节点或 `ArrayAnnotation(static_shape_dims=...)` 时，会要求 v2，由 `KernelSignature` 构造期的 `_validate_constraint_support` 门控（详见 u8-l1、u8-l2）。

---

## 5. 综合实践

把本讲的五个最小模块串起来，完成下面这个**追踪型**任务。

**任务背景**：下面这个内核取自测试（略有改写）：

```python
# 示例代码（改编自 test_tuple_arguments.py）
import cuda.tile as ct

@ct.kernel(occupancy=2)
def my_kernel(a: ct.Array, out: ct.Array, cfg: tuple[ct.Constant[int], int]):
    t = ct.load(a, (0,), (cfg[0],))
    ct.store(out, (0,), t + cfg[1])
```

**请完成**：

1. **追踪对象构造**：从 `@ct.kernel(occupancy=2)` 触发，按顺序列出 `kernel.__init__` 里发生的每一步，并指出最终 `my_kernel` 对象上保存的两个关键字段及其类型。
2. **手画注解树**：对三个参数 `a` / `out` / `cfg`，分别画出它们的 `ParameterAnnotationNode` 树。特别地，`cfg` 的树应当体现「部分常量」语义。
3. **预测行为**：如果用 `cfg = (8, 5)` 启动一次，再用 `cfg = (8, 9)` 启动第二次，会不会触发重新编译？为什么？如果换成 `cfg = (16, 5)` 呢？

**参考答案**：

1. 构造顺序见 [`_execution.py:101-123`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L101-L123)：校验函数 → `get_annotated_function` 解析注解树 → `CompilerOptions(occupancy=2, opt_level=3, ...)` → `super().__init__(parameter_annotations)` 把树交给 C++ → 存 `_annotated_function`（`AnnotatedFunction`）与 `_compiler_options`（`CompilerOptions`）。

2. 注解树：

   ```text
   参数 a (ct.Array)            → LeafAnnotationNode(constant=False, array=None)
   参数 out (ct.Array)          → LeafAnnotationNode(constant=False, array=None)
   参数 cfg (tuple[Constant[int], int])
         → HeterogeneousTupleNode(items=(
               LeafAnnotationNode(constant=True),     # Constant，进 JIT 缓存键
               LeafAnnotationNode(constant=False),    # 普通 int，不进缓存键
           ))
   ```

3. 因为只有 `cfg[0]` 是常量（进入 JIT 缓存键），`cfg[1]` 不是：所以 `(8,5)` → `(8,9)` 两次启动**不会**重新编译（`cfg[0]` 都是 8）；但 `(8,5)` → `(16,5)` 会触发重新编译（`cfg[0]` 从 8 变成 16）。这正是「部分常量元组」的核心收益——把不该常量化的维度排除出缓存键，避免不必要的重新编译。

## 6. 本讲小结

- `kernel` 是继承自 C++ 类型 `TileDispatcher` 的装饰器，装饰产物是一个**携带元数据的对象实例**，禁止直接调用，须经 `ct.launch` 启动。
- `kernel` 对象持久保存两个前端字段：`_annotated_function`（参数注解）与 `_compiler_options`（四个编译旋钮的不可变 dataclass）。
- `AnnotatedFunction` 用 `typing.get_type_hints(..., include_extras=True)` 解析参数注解，关键是保留 `Annotated` 元数据并解析字符串注解。
- 注解表示在本次重构中从「三个扁平布尔掩码」升级为统一的 **`ParameterAnnotationNode` 树**——`LeafAnnotationNode`（标量/数组角色，四字段互斥）+ `HomogeneousTupleNode`（变长同质）+ `HeterogeneousTupleNode`（定长异质），从而支持任意嵌套的 tuple 参数。
- 常量性通过 `outer_constant` 在树中**向下传播**，表达「整组常量」与「部分常量」两种组合，决定哪些值进入 JIT 缓存键。
- `TileDispatcher.__init__` 现在只接收 `parameter_annotations`（注解树），C++ 侧用 `KIND` 字符串分派镜像同一棵树；tuple 与静态形状最终要求 `cutile_python_v2` 调用约定。

## 7. 下一步学习建议

本讲只讲了「装饰阶段产出的对象模型」，还没讲这些对象**怎么被用起来**。建议按以下顺序继续：

1. **u5-l2 编译总流程：compile_tile 流水线**——看 `_compile` 被回调后，`_annotated_function` 与 `_compiler_options` 如何流入 `compile_tile`，以及 `_IrKeeper._create_kernel_parameters` 如何递归消费注解树生成内核参数 `Var`。
2. **u5-l5 IR 核心**与 **u5-l6 类型系统**——理解参数最终如何变成 IR 里的 `Var` 与 `ArrayTy`（含静态形状维度）。
3. **u3-l7 元组参数与静态形状特化**——从用户视角反过来理解本讲建的注解树到底解锁了什么能力。
4. **u8-l1 launch 与调度**与 **u8-l2 AOT 导出与签名**——看 `CallingConvention.cutile_python_v2` 与 `TupleConstraint` / `ArrayConstraint.shape_constant` 如何在运行时与导出阶段被门控使用。
