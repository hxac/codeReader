# 高阶 API（一）：Matmul 矩阵乘

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `asc.adv.MatmulType` + `asc.adv.Matmul` 正确描述一个矩阵乘（A/B/C/Bias 的位置、格式、转置、Layout），并说清它最终生成了什么 IR。
2. 说清 `MatmulConfig` 是什么、为什么它整体是编译期常量、`get_normal_config` 等工厂函数与它的关系。
3. 独立写出 `register_matmul -> set_tensor_a/b -> set_tail -> iterate_all -> end` 的标准调用范式，并解释 TPipe、workspace、TCubeTiling 三者在其中扮演的角色。
4. 区分 MIX 与 CubeOnly 两种模式：`matmul_cube_only=True` 如何一路改写 kernel_type 推导、Pass 调度、编译目标架构与启动 block 数。

本讲是「高阶 API」单元的第一讲。所谓高阶 API，是指 Ascend C 中以「对象 + 多步方法调用」方式封装完整计算流程（而不是一条指令级操作）的接口，Matmul 是其中最典型的一个。

## 2. 前置知识

本讲默认你已读过 u2-l6（TPipe/TQue 框架）与 u3-l4（CompileOptions 与 Pass 流水线），此外需要以下背景概念：

- **Cube 核与 Vector 核**：昇腾 AI Core 里的两类计算单元。Vector（矢量）核擅长逐元素运算（本手册前几讲的 `asc.add` 都发射到它）；Cube（矩阵）核擅长矩阵乘，内部有 L0A/L0B/L0C 等专用缓冲。`AIC` 指 AI Cube 核，`AIV` 指 AI Vector 核。
- **MIX 模式**：一个调度 block 内 Cube 核与 Vector 核成对出现、协同工作的形态；「纯 Cube」则只用 Cube 核。
- **矩阵格式 ND/NZ**：Cube 运算对数据排布有要求，`CubeFormat.ND` 是普通行主序，`NZ` 等是分块排布。本讲两个示例都用 ND，格式概念只需知道「它是 MatmulType 的必填项」。
- **tiling（切分）**：大矩阵 \( M \times N \)、内维 \( K \) 的乘法要拆到多个核、多轮分块上算，拆分方案（每核负责多大块、基本块多大、循环多少轮）就是 tiling。矩阵乘的计算量按乘加次数计为 \[ M \times N \times K \] 次 FMA，切分质量直接决定性能。
- **workspace**：Matmul 高阶 API 在 GM 上需要的一块工作缓冲（示例里开辟了 16 MiB），由用户分配、经 `register_matmul` 告知。
- **Struct 参数的三面性**（u3-l3 已讲）：`TCubeTiling` 是 `Struct` 子类——Host 侧是 ctypes 结构体、IR 侧是 `PyStructType`、设备侧生成成员读写操作。Host 填好、设备读，正是 tiling 的传递方式。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/asc/language/adv/matmul.py` | Matmul 高阶 API 前端：`MatmulType`、`Matmul` 类、`register_matmul`、`MatmulIterator`、各 `get_*_config` 工厂 |
| `python/asc/language/adv/types.py` | `MatmulConfig`（50 个策略开关的 IR 包装）与 `MatmulShapeParams` 等参数包 |
| `python/asc/language/adv/tiling.py` | `TCubeTiling`、`MatmulApiStaticTiling` 等 tiling 结构体定义 |
| `python/asc/language/core/struct.py` | `Struct`/`Field` 基类：tiling 结构体的 Host/设备双面机制 |
| `examples/03_matmul_mix/matmul_mix.py` | MIX 模式端到端示例 |
| `examples/04_matmul_cube_only/matmul_cube_only.py` | 纯 Cube 模式端到端示例 |
| `python/asc/runtime/compiler.py` | `matmul_cube_only` 选项的消费侧：kernel_type 推导与 Pass 调度 |
| `python/asc/runtime/config.py` | `KernelType` 枚举（8 种核类型） |
| `python/asc/language/basic/common.py` | `ascend_is_aic()`：设备侧判断当前核是否为 Cube 核 |
| `lib/Dialect/Asc/Transforms/DetectKernelTypePass.cpp` | 检测 `RegistMatmulObjOp` 并打 `asc.compile_mix` 属性 |
| `lib/Dialect/Asc/Transforms/DefineCubeOnlyPass.cpp` | CubeOnly 模式下插入 `#define ASCENDC_CUBE_ONLY` |
| `python/test/unit/language/adv/test_matmul.py` | Matmul 前端单元测试（Model 后端即可跑） |

## 4. 核心概念与源码讲解

### 4.1 Matmul 类与 MatmulType：矩阵乘对象的构造

#### 4.1.1 概念说明

前面学过的 `asc.add` 是「函数式」API：一次调用对应一条向量指令。`Matmul` 则是「对象式」API：先构造一个描述完整的矩阵乘对象，再通过一连串方法调用（放入 A、放入 B、迭代计算、取回 C、收尾）驱动完整的矩阵乘流水。它的计算公式为 `C = A * B + Bias`。

`MatmulType` 是描述**单个矩阵操作数**的四元组：

- `position`：数据所在逻辑位置（示例中 A/B/C 都在 `TPosition.GM`）；
- `format`：矩阵排布格式（`CubeFormat.ND` 等）；
- `dtype`：元素类型；
- `is_trans`：是否转置（默认 False）；
- `layout`：LayoutMode（默认 NONE，进阶场景用）。

`Matmul` 本身是 `IRValue` 的子类——这符合 u2-l3 建立的认知：JIT 编译期里，一个 Python 对象只要持有 IR 句柄、实现 `from_ir`/`to_ir` 协议，就能代表 IR 里的一个值。Matmul 对象的句柄来自一条 `ConstructOp`。

#### 4.1.2 核心流程

构造一个 Matmul 对象的流程：

```text
Matmul(a, b, c, bias=None, matmul_config=None)
  ├─ 校验 a/b/c 必须是 MatmulType，bias/config 可为 None
  ├─ bias 为 None 时：bias 的 position/format/dtype 直接取 c 的
  ├─ matmul_config 为 None 时：使用默认 MatmulConfig()
  ├─ builder.get_matmul_type(...)   # 把四组类型信息 + config 的 50 个字段
  │                                 #   打包成一个 MLIR 的 ascendc matmul 类型
  └─ builder.create_asc_ConstructOp(ir_type, [])  # 物化为 IR 值，存入 self.handle
```

要点：**Matmul 的全部类型与策略信息不是存在 Python 对象属性里，而是编进了 IR 类型本身**。这呼应 u5-l2 讲过的「Matmul 类型携带十余个参数充当模板实参包」——Ascend C 里 `matmul::Matmul<...>` 的尖括号模板参数，在 pyasc 里就由这个 IR 类型承载。

#### 4.1.3 源码精读

先看 `MatmulType`，它是一个不可变 dataclass：

```python
@dataclass(frozen=True)
class MatmulType:
    position: TPosition
    format: CubeFormat
    dtype: DataType
    is_trans: bool = False
    layout: LayoutMode = LayoutMode.NONE
```

这是 [python/asc/language/adv/matmul.py:43-L49](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L43-L49)，定义了操作数四元组。frozen 保证它只是「值描述」，构造后不可篡改。

再看 `Matmul.__init__` 的校验与缺省逻辑，源码位于 [python/asc/language/adv/matmul.py:L82-L96](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L82-L96)：先强制 `a/b/c` 必须是 `MatmulType` 且不能为 None；`bias` 与 `matmul_config` 允许为 None 但类型受限；随后：

```python
bias_pos = c.position
bias_format = c.format
bias_type = c.dtype
if bias is not None:
    bias_pos = bias.position
    ...
if matmul_config is None:
    matmul_config = MatmulConfig()
```

即 **bias 缺省时跟随 C 的位置/格式/类型**——直觉上合理：bias 与 C 同在输出侧，长度等于 N，格式自然与 C 对齐。

最后是 IR 生成，[python/asc/language/adv/matmul.py:L97-L118](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L97-L118)：

```python
ir_type = builder.get_matmul_type(
    a.position, a.format, a.dtype.to_ir(), a.is_trans, a.layout,  # a
    b.position, b.format, b.dtype.to_ir(), b.is_trans, b.layout,  # b
    c.position, c.format, c.dtype.to_ir(), c.is_trans, c.layout,  # c
    bias_pos, bias_format, bias_type.to_ir(), matmul_config.do_norm, ...
    # 此后连续传入 matmul_config 的约 50 个字段
)
self.handle = builder.create_asc_ConstructOp(ir_type, [])
self.c_dtype = c.dtype
self.a_dtype = a.dtype
self.b_dtype = b.dtype
```

三个观察点：

1. `get_matmul_type` 一次吃进 4 组操作数信息 + 整个 config，返回一个 MLIR 类型；`create_asc_ConstructOp` 用这个类型物化出 IR 值。这正是 u5-l6 总结的「类型计算 + create_asc_XxxOp + 包装返回 IRValue」三段式。
2. `a_dtype/b_dtype/c_dtype` 被缓存在 Python 对象上，供后续 `set_bias` 等方法做类型校验（如「A、B 都是 int8 时 bias 必须是 int32」）。
3. dtype 从哪来？看 MIX 示例的用法（[examples/03_matmul_mix/matmul_mix.py:L34-L45](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/03_matmul_mix/matmul_mix.py#L34-L45)）：

```python
a_global = asc.GlobalTensor()
a_global.set_global_buffer(a + offset_a)     # 从 GM 指针取 dtype
matmul = asc.adv.Matmul(
    a=asc.adv.MatmulType(asc.TPosition.GM, asc.CubeFormat.ND, a_global.dtype, IS_TRANS_A),
    ...
)
```

先 `set_global_buffer` 让 GlobalTensor 获得 dtype，再把这个 dtype 填进 MatmulType——类型信息沿着「Host 指针类型 → GlobalTensor → MatmulType → IR 类型」传递。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 Matmul 构造生成的 IR。

**操作步骤**：

1. 打开 [python/test/unit/language/adv/test_matmul.py:L20-L33](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/language/adv/test_matmul.py#L20-L33) 的 `test_init`，把它抄成一个独立脚本（或直接在该测试上运行）：

```python
# 示例代码：基于 test_init 改写
import asc
from asc.runtime import config

config.set_platform(config.Backend.Model, check=False)

@asc.jit
def kernel_init(workspace: asc.GlobalAddress) -> None:
    pipe = asc.TPipe()
    a = asc.adv.MatmulType(position=asc.TPosition.GM, format=asc.CubeFormat.ND, dtype=asc.float16)
    b = asc.adv.MatmulType(position=asc.TPosition.GM, format=asc.CubeFormat.ND, dtype=asc.float16)
    c = asc.adv.MatmulType(position=asc.TPosition.GM, format=asc.CubeFormat.ND, dtype=asc.float16)
    matmul = asc.adv.Matmul(a, b, c)
    asc.adv.register_matmul(pipe, workspace, matmul)
```

2. 设置 `PYASC_DUMP_PATH=/tmp/matmul_dump` 后以 Model 后端触发一次编译（运行方式参考单测的 `kernel_init[1](workspace)` 调用，workspace 用单测同款 `MockTensor`）。
3. 打开导出的 `codegen.mlir`，搜索 `Construct` 与 `matmul` 关键字。

**需要观察的现象**：`codegen.mlir` 中应出现一条携带 ascendc matmul 类型（打印形如含 A/B/C 位置、格式与大量 config 参数的类型文本）的 Construct 操作；`ascendc.RegistMatmulObj` 一类的操作也应可见（可在 dump 中 grep `Matmul` 验证）。

**预期结果**：Matmul 对象在 IR 里体现为「一条 ConstructOp + 一个信息量巨大的类型」，而不是一堆散落的 Python 属性。具体类型打印文本**待本地验证**（其格式由 u5-l2 讲过的 assemblyFormat 决定）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bias=None` 时 bias 的 position/format/dtype 取 C 的值，而不是 A 的？
**答案**：bias 在计算 \( C = A \times B + Bias \) 中与 C 同为输出侧，长度等于 N、逐行广播，其存放位置与排布跟随 C 才能直接累加；取 A 的（M 侧、可能带转置的格式）在语义上不成立。对应源码 [python/asc/language/adv/matmul.py:L88-L94](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L88-L94)。

**练习 2**：Matmul 对象是运行时才创建的吗？
**答案**：不是。`@asc.jit` 函数体内的 `asc.adv.Matmul(...)` 在 JIT **编译期**执行，生成 ConstructOp 写入 IR；设备运行时对应的是 Ascend C 侧 matmul 对象的构造。判断依据：`__init__` 里直接使用 `global_builder.get_ir_builder()`（[python/asc/language/adv/matmul.py:L81](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L81)），而 builder 只在 codegen 阶段就位（u5-l6）。

**练习 3**：把 MIX 示例中 C 的 `CubeFormat.ND` 改成 `CubeFormat.NZ` 会导致哪层先报错？
**答案**：前端 `Matmul.__init__` 本身不校验格式组合（它只校验类型是 `MatmulType`），格式会进入 IR 类型；不兼容的组合通常在发射为 Ascend C 或毕昇编译阶段才暴露。此为合理推断，具体报错位置**待本地验证**。

### 4.2 MatmulConfig：五十个编译期开关与工厂函数

#### 4.2.1 概念说明

`MatmulConfig` 描述矩阵乘的**执行策略**：走普通范式还是 MDL（多数据加载）、基本块尺寸（basic_m/n/k）、迭代顺序（iterate_order）、调度类型（schedule_type）、批处理模式（batch_mode）、以及一系列 `enable_*` 能力开关（是否允许 end、是否允许 set_bias……）。它有约 50 个字段，全部有默认值，普通使用中**不传它也行**——两个示例都没有显式传 config，走的就是 `MatmulConfig()` 默认值（见 4.1.3 第 96 行）。

关键性质：这些字段**整体是编译期常量**。构造时生成 `isConstexpr=True, isStatic=True` 的 ConstructOp，也就是说 config 的每个取值都会被烘进生成的代码，改一个字段意味着重新编译（这与你学过的 ConstExpr 缓存语义一致）。

因为 50 个字段直接裸传不现实，前端提供了一组**工厂函数**按「预设模板」填字段：`get_normal_config`（普通范式）、`get_mdl_config`（MDL 范式）、`get_basic_config`/`get_special_basic_config`（基本块范式）、`get_special_mdl_config`、`get_ib_share_norm_config`（共享范数），以及接受参数包的 `get_mm_config`。

#### 4.2.2 核心流程

```text
用户调用 get_normal_config(iterate_order=..., ...)
  └─ 按预设填一个 MatmulConfig 子集，其余用默认值
       └─ MatmulConfig.__init__ 把 ~50 个值逐个 _mat(...).to_ir()
            └─ create_asc_ConstructOp(get_asc_MatmulConfigType(), [...], isConstexpr=True, isStatic=True)
```

`get_mm_config` 则更进一步：接受 `MatmulShapeParams`（形状切分参数）、`MatmulQuantParams`（量化）、`MatmulBatchParams`（批处理）、`MatmulFuncParams`（功能开关）四个参数包与 `MatmulConfigMode` 枚举（CONFIG_NORM/CONFIG_MDL/CONFIG_SPECIALMDL/CONFIG_IBSHARE），循环识别每个入参的类型后汇总成一个 config。

#### 4.2.3 源码精读

`MatmulConfig` 的字段清单见 [python/asc/language/adv/types.py:L19-L40](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/types.py#L19-L40)（overload 存根），摘开头的几项：

```python
class MatmulConfig(IRValue):
    @overload
    def __init__(self, do_norm: bool = True, do_basic_block: bool = False, do_multi_data_load: bool = False,
                 basic_m: int = 0, basic_n: int = 0, basic_k: int = 0, intrinsics_check: bool = False, ...
```

真正的组装在 [python/asc/language/adv/types.py:L77-L132](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/types.py#L77-L132)：每个字段经 `_mat(...).to_ir()` 物化，按固定顺序排成一个长实参列表，同时配一份逐字段的类型表（`get_i1_type()`、`get_ui32_type()`、`get_asc_IterateOrderType()` 等），最后：

```python
self.handle = builder.create_asc_ConstructOp(
    builder.get_asc_MatmulConfigType(), [...], builder.get_type_array_attr([...]),
    isConstexpr=True, isStatic=True)
```

`isConstexpr=True, isStatic=True` 出现在 [python/asc/language/adv/types.py:L182](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/types.py#L182)，这是「编译期常量结构」的标志。组装完后同名字段也存到 Python 属性上（[L183-L232](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/types.py#L183-L232)），所以 4.1.3 里 `Matmul.__init__` 才能直接读 `matmul_config.do_norm` 等值。

看一个典型工厂 `get_basic_config`（[python/asc/language/adv/matmul.py:L694-L714](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L694-L714)）：

```python
mm_config = MatmulConfig(basic_m=basic_m, basic_n=basic_n, basic_k=basic_k,
                         intrinsics_check=intrinsics_limit, is_n_batch=batch_loop, batch_mode=bmm_mode)
return mm_config
```

——它只是「按关键字填 6 个字段、其余吃默认值」，没有任何魔法。最复杂的 `get_mm_config`（[python/asc/language/adv/matmul.py:L814-L899](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L814-L899)）做的事则是：遍历 `args/kwargs`，用 `isinstance` 识别四类参数包（`MatmulShapeParams` 等定义在 [python/asc/language/adv/types.py:L275-L284](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/types.py#L275-L284) 起）与 `MatmulConfigMode` 枚举，把识别到的值摊平到局部变量，最后一次性 `return MatmulConfig(...)`。

#### 4.2.4 代码实践

**实践目标**：建立「工厂函数 = 填字段捷径」的手感，不运行也能完成。

**操作步骤**：

1. 打开 `get_basic_config`（[python/asc/language/adv/matmul.py:L701-L714](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L701-L714)）与 `get_special_basic_config`（[python/asc/language/adv/matmul.py:L950-L966](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L950-L966)）。
2. 制作一张两列对照表：两个函数分别给 `MatmulConfig` 显式赋了哪些字段。
3. 再对照 [python/asc/language/adv/types.py:L22-L40](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/types.py#L22-L40) 的默认值，标出「被覆盖的字段」与「仍取默认值的字段」。

**需要观察的现象**：`get_special_basic_config` 比 `get_basic_config` 多出 `single_core_m/n/k` 与 `step_m/step_n` 六个字段的赋值。

**预期结果**：得到一张约 6 行 vs 12 行的对照表；结论是工厂函数之间的差异完全等价于「填哪些字段」，没有任何隐藏逻辑。

**进阶（待本地验证）**：在 4.1.4 的最小 kernel 里把 `Matmul(a, b, c)` 改成 `Matmul(a, b, c, matmul_config=asc.adv.get_normal_config())`，重新 dump 并 diff `codegen.mlir`，观察 matmul 类型中 config 相关段的变化（预期：默认值路径与显式 normal 路径生成的值几乎一致，diff 应极小甚至为空——因为 `get_normal_config` 的缺省就是 `MatmulConfig` 的缺省）。

#### 4.2.5 小练习与答案

**练习 1**：`MatmulConfig` 为什么整体做成 constexpr，而不是像 tiling 那样做成运行时可读的 Struct？
**答案**：config 控制的是「生成什么样的矩阵乘代码」（范式、基本块、迭代顺序），这些在 Ascend C 侧是模板参数与编译分支，必须在编译期定死；而 tiling 描述的是「这次算多大的矩阵」，同一份代码要能服务不同规模，所以走运行时结构体传递。这也解释了为什么改 config 会触发重编译、改 tiling 参数值不会（tiling 是普通运行时 Struct 参数，只有类型进缓存 key——见 u3-l3/u3-l8）。

**练习 2**：`get_mm_config` 为什么用 `isinstance` 识别参数包，而不是像其他工厂那样用固定位置形参？
**答案**：参数包有四类且互相独立，用户可能只传其中一两类；`for arg in [args, kwargs]: if isinstance(arg, MatmulShapeParams): ...`（[python/asc/language/adv/matmul.py:L849-L888](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L849-L888)）允许任意组合、任意顺序传入，接口更宽松。

**练习 3**：`get_matmul_api_tiling`（[python/asc/language/adv/matmul.py:L750-L782](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L750-L782)）和 `get_mm_config` 是一类东西吗？
**答案**：不是。它同样先组装 matmul IR 类型，但最终创建的是 `asc_MatmulGetMatmulApiTilingOp`，产出 `MatmulApiStaticTiling`（[python/asc/language/adv/tiling.py:L15-L66](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/tiling.py#L15-L66)）——即「在设备上根据 config 反推静态 tiling」的操作，属于 tiling 计算而非 config 生成。注意它当前没有 return 该结果，这一点阅读时留心。

### 4.3 register_matmul、TCubeTiling 与 SetTensor/Iterate 调用范式

#### 4.3.1 概念说明

构造出的 Matmul 对象要「挂」到运行环境上才能工作。`register_matmul(pipe, workspace, matmul, tiling)` 一次性完成四件事的关联：

- `pipe`：TPipe，Device 内存与同步事件总管（u2-l6）——matmul 内部的缓冲要从它这里走；
- `workspace`：GM 上的一块工作内存（GlobalAddress）；
- `matmul`：刚构造的对象；
- `tiling`：切分策略（可选）。

`TCubeTiling` 是 Ascend C `TCubeTiling` 结构的 pyasc 镜像：约 50 个 int32 字段（M/N/Ka/Kb、singleCoreM/N/K、baseM/N/K、stepM/N、usedCoreNum……），**Host 侧算好填入、设备侧逐字段读取**。两个示例的 tiling 都由 Host 侧的 `host.MultiCoreMatmulTiling` 计算（这是 u7-l3 的主题，这里把它当黑盒：输入矩阵形状、类型与核数，输出填好的 TCubeTiling）。

#### 4.3.2 核心流程

以 MIX 示例为准的完整范式（两份示例完全同构）：

```text
Host 侧：
  generate_tiling(m, n, k)          # host.MultiCoreMatmulTiling 填 TCubeTiling
  分配 c、workspace（GM）
  kernel[block_num, stream](a, b, c, tiling, workspace)

Device 侧（kernel 体内）：
  1. calc_offsets(tiling, ...)       # 按 block_idx 算本核负责的子块偏移与尾巴
  2. GlobalTensor + set_global_buffer # 绑定各子块地址
  3. pipe = asc.TPipe()
  4. matmul = asc.adv.Matmul(a_type, b_type, c_type[, bias_type])
  5. asc.adv.register_matmul(pipe, workspace, matmul, tiling)
  6. if asc.get_block_idx() < tiling.used_core_num:
  7.     matmul.set_tensor_a(a_global, IS_TRANS_A)
  8.     matmul.set_tensor_b(b_global, IS_TRANS_B)
  9.     matmul.set_tail(tail_m, tail_n[, tail_k])
  10.    matmul.iterate_all(c_global)  # 一次完成全部迭代并写出 C
  11.    matmul.end()                  # 收尾（同步/资源释放语义）
  12. asc.pipe_barrier(asc.PipeID.PIPE_ALL)
```

多核切分的数学：设 \( M_{blocks} = \lceil M / \text{singleCoreM} \rceil \)，则第 `block_idx` 个核负责的子块为 \( m\_index = block\_idx \bmod M_{blocks} \)、\( n\_index = \lfloor block\_idx / M_{blocks} \rfloor \)，C 中偏移为 \( m\_index \times N \times \text{singleCoreM} + n\_index \times \text{singleCoreN} \)，行/列尾巴取 \( \min(M - m\_index \times \text{singleCoreM},\ \text{singleCoreM}) \)。

除 `iterate_all` 外还有手动迭代形态：`matmul.iterate()` 返回 `MatmulIterator`，用 `with` 语法逐轮推进，循环计数是设备侧 int32 值；以及 `get_tensor_c`（把 C 取到指定 Tensor 或返回 GlobalTensor）、`iterate_batch` 系列批处理接口。两个入门示例只用了最简单的 `iterate_all`。

#### 4.3.3 源码精读

`register_matmul` 本体极短（[python/asc/language/adv/matmul.py:L34-L40](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L34-L40)）：

```python
@require_jit
def register_matmul(pipe: TPipe, workspace: GlobalAddress, matmul: Matmul,
                    tiling: Optional[TCubeTiling] = None) -> None:
    ir_tiling = tiling.to_ir() if tiling is not None else None
    builder = global_builder.get_ir_builder()
    builder.create_asc_RegistMatmulObjOp(pipe.to_ir(), workspace.to_ir(), matmul.to_ir(), ir_tiling)
```

四个参数各自 `to_ir()` 后汇入一条 `RegistMatmulObjOp`。注意 **`RegistMatmulObjOp` 这个 Op 还有下游意义**：4.4 会讲到 DetectKernelTypePass 靠它的存在来判定「这是矩阵乘 kernel」。

`TCubeTiling` 的定义（[python/asc/language/adv/tiling.py:L111-L164](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/tiling.py#L111-L164)）就是一排 `Field`：

```python
class TCubeTiling(Struct):
    used_core_num = Field(dtype=KT.int32, default=0, name="usedCoreNum")
    m = Field(dtype=KT.int32, default=0, name="M")
    n = Field(dtype=KT.int32, default=0, name="N")
    k_a = Field(dtype=KT.int32, default=0, name="Ka")
    k_b = Field(dtype=KT.int32, default=0, name="Kb")
    single_core_m = Field(dtype=KT.int32, default=0, name="singleCoreM")
    ...
```

`name="M"` 等指定了 IR/发射层的成员名（与 Ascend C 结构体字段对齐）。`Struct` 基类的双面行为在 [python/asc/language/core/struct.py:L174-L198](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L174-L198)：Host 侧 `__getattribute__` 发现属性是 `BaseField` 时转读 `ctypes_struct`（所以 Host 上 `tiling.used_core_num` 就是普通 Python int）；JIT 侧 `__getattrjit__` 生成 `emitasc.MemberOp` 返回 `PlainValue`。于是 kernel 内这句话（[examples/03_matmul_mix/matmul_mix.py:L56-L76](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/03_matmul_mix/matmul_mix.py#L56-L76)）：

```python
m_single_blocks = tiling.m.ceildiv(tiling.single_core_m)
m_index = block_idx % m_single_blocks
```

里的 `tiling.m` 是设备侧 IR 值，`ceildiv`/`%` 是 IR 运算——**每个核在运行时根据自己的 block_idx 现场算偏移**，这就是 tiling 作为运行时 Struct 的价值：同一份 kernel 服务任意规模。

标准调用范式逐行对应 [examples/03_matmul_mix/matmul_mix.py:L40-L53](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/03_matmul_mix/matmul_mix.py#L40-L53)：

```python
pipe = asc.TPipe()
matmul = asc.adv.Matmul(...)
asc.adv.register_matmul(pipe, workspace, matmul, tiling)
if asc.get_block_idx() < tiling.used_core_num:
    matmul.set_tensor_a(a_global, IS_TRANS_A)
    matmul.set_tensor_b(b_global, IS_TRANS_B)
    matmul.set_tail(tail_m, tail_n)
    matmul.iterate_all(c_global)
    matmul.end()
asc.pipe_barrier(asc.PipeID.PIPE_ALL)
```

配套的方法实现各有看点：

- `set_tensor_a` 有两个重载（[python/asc/language/adv/matmul.py:L405-L424](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L405-L424)）：传 Tensor（`MatmulSetTensorAOp`，额外带 transpose 位）或传标量（`MatmulSetTensorAScalarOp`，A 为常数矩阵）；`set_tensor_b` 同构（[L434-L453](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L434-L453)）。
- `set_tail`（[python/asc/language/adv/matmul.py:L523-L530](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L523-L530)）把尾巴尺寸按 `KT.int_`（即 int32，见 [python/asc/language/core/dtype.py:L120](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/dtype.py#L120)）物化，生成 `MatmulSetTailOp`；MIX 示例传两参，CubeOnly 示例传三参（含 `tail_k=tiling.k_a`）。
- `iterate_all`（[python/asc/language/adv/matmul.py:L216-L248](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L216-L248)）先按输出 Tensor 是 GlobalTensor 还是 TSCM（LocalTensor）做参数裁剪与校验，再生成 `MatmulIterateAllOp`。
- `end`（[python/asc/language/adv/matmul.py:L130-L133](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L130-L133)）一行生成 `MatmulEndOp`。
- 手动迭代入口 `iterate` 返回 `MatmulIterator`（[python/asc/language/adv/matmul.py:L196-L208](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L196-L208)），其 `__enter__`/`__exit__`（[L629-L653](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L629-L653)）手工搭出 `scf.while` + `MatmulIterateOp` + `scf.condition` + `scf.yield` 的循环骨架——这是 u5-l6 提过的「手工搭 IR」形态的实例。

Host 侧配套看 MIX 示例：`generate_tiling`（[examples/03_matmul_mix/matmul_mix.py:L89-L105](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/03_matmul_mix/matmul_mix.py#L89-L105)）用 `host.MultiCoreMatmulTiling` 设置 A/B/C/Bias 类型、`set_dim(USE_CORE_NUM)`、`set_org_shape/set_shape(m, n, k)`、`enable_bias(False)`、`set_buffer_space(-1, -1, -1)`，最后 `matmul_tiling.get_tiling(tiling)` 把结果写进 `asc.adv.TCubeTiling()`。`matmul_launch`（[L79-L86](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/03_matmul_mix/matmul_mix.py#L79-L86)）分配 16 MiB workspace 并以 `matmul_kernel[USE_CORE_NUM // 2, rt.current_stream()]` 启动——block 数为什么除 2，见 4.4。

#### 4.3.4 代码实践

**实践目标**：不依赖任何硬件，人工执行一遍 `calc_offsets`，确证你读懂了 tiling 驱动的多核切分。

**操作步骤**：

1. 阅读 [examples/03_matmul_mix/matmul_mix.py:L56-L76](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/03_matmul_mix/matmul_mix.py#L56-L76) 的 `calc_offsets`（`IS_TRANS_A=IS_TRANS_B=False`）。
2. 假设 tiling 为：`m=512, n=1024, k_a=k_b=512, single_core_m=128, single_core_n=256, used_core_num=16`。
3. 手算 `block_idx=5` 时全部六个输出：`m_single_blocks、m_index、n_index、offset_a、offset_b、offset_c、tail_m、tail_n`。
4. 再算 `block_idx=15`（最后一个核）验证尾巴逻辑。

**需要观察的现象／预期结果**：

- `block_idx=5`：\( M_{blocks}=\lceil 512/128 \rceil=4 \)，`m_index=5%4=1`，`n_index=5//4=1`；`offset_a=1*512*128=65536`；`offset_b=1*256=256`；`offset_c=1*1024*128+1*256=131328`；`tail_m=512-128=384`，因 `384>=128` 截为 `128`；`tail_n=1024-256=768`，截为 `256`。
- `block_idx=15`：`m_index=3`，`tail_m=512-384=128`，仍为整块；说明只有当 M 不是 singleCoreM 整数倍时最后一个核才会拿到真尾巴。

对照源码逐行核对你的算式与 [L59-L75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/03_matmul_mix/matmul_mix.py#L59-L75) 一致即通过。注意 CubeOnly 版的 `calc_offsets`（[examples/04_matmul_cube_only/matmul_cube_only.py:L63-L84](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/04_matmul_cube_only/matmul_cube_only.py#L63-L84)）多算一个 `offset_bias`，且尾巴判断多了 `tail_m <= 0` 分支——想一想为什么（提示：CubeOnly 的启动 block 数直接取 `tiling.used_core_num`，边界条件不同）。

#### 4.3.5 小练习与答案

**练习 1**：`if asc.get_block_idx() < tiling.used_core_num:` 这行守卫在防什么？
**答案**：启动的 block 数与 tiling 实际切出的核数可能不一致（Host 上 `set_dim` 给的是期望核数，tiling 引擎按形状约束可能用得更少）。多出来的核不执行 set_tensor/iterate，只走到最后的 `pipe_barrier(PIPE_ALL)` 参与全核同步，避免越界读写别人的子块。

**练习 2**：为什么 workspace 要由用户在 Host 侧分配并一路传进 kernel，而不是像 UB 那样由 TPipe 管理？
**答案**：workspace 是 GM 上的大块中间空间（示例 16 MiB），生命周期跨整个 kernel 且由 Matmul 高阶 API 内部使用；TPipe 管理的是核内 UB/L1 等片上缓冲。GM 大缓冲由 Host 显式分配（[examples/03_matmul_mix/matmul_mix.py:L83](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/03_matmul_mix/matmul_mix.py#L83)），再以 GlobalAddress 参数传入，符合 u3-l3 讲过的指针参数机制。

**练习 3**：`matmul.iterate_all(c_global)` 与 `with matmul.iterate() as i:` 两种写法的本质区别是什么？
**答案**：`iterate_all` 生成一条 `MatmulIterateAllOp`，把「循环推进 + 尾块处理 + 结果写出」整体交给 Ascend C 的 IterateAll 语义；`iterate` 返回的 `MatmulIterator` 则在 IR 里搭出真实的 `scf.while` 循环（[python/asc/language/adv/matmul.py:L629-L653](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L629-L653)），每轮的搬运/计算/写出由用户在 with 体内自行组合，控制粒度更细（如分块做融合），代价是要自己处理同步与尾巴。

### 4.4 MIX 与 CubeOnly：两种核形态与 matmul_cube_only 选项

#### 4.4.1 概念说明

同一个 Matmul kernel 可以编成两种核形态：

- **MIX 模式**（示例 03）：kernel_type 推导为 `MIX_AIC_1_2`，一份二进制同时含 Cube 目标与 Vector 目标代码（分别编译再链接），以 AiCore 组调度。适合矩阵乘与向量前后处理融合的场景。
- **CubeOnly 模式**（示例 04）：装饰器加 `matmul_cube_only=True`，kernel_type 推导为 `AIC_ONLY`，只编 Cube 目标、以 CubeCore 调度，且发射的 Ascend C 源码里多一个 `#define ASCENDC_CUBE_ONLY` 宏。适合纯矩阵乘、超大规模 N（示例是 128×64×30720）这类纯粹吃 Cube 吞吐的负载。

回放 u3-l4 的结论：kernel_type 为 None 时按模块上的 `asc.compile_mix` 属性推导。本讲补上矩阵乘场景的完整闭环：**DetectKernelTypePass 在 IR 里看到 `RegistMatmulObjOp` 就打 `asc.compile_mix`**——所以「你调用了 register_matmul」这件事本身就是核类型切换的触发器，`matmul_cube_only` 则决定带这个属性的 kernel 落到 MIX 还是纯 Cube。

#### 4.4.2 核心流程

```text
run_passes 结束时（compiler.py）：
  模块有 asc.compile_mix 属性？（= IR 中存在 RegistMatmulObjOp，由 DetectKernelTypePass 打上）
    ├─ 是，且 matmul_cube_only=True  → kernel_type = AIC_ONLY
    ├─ 是，且 matmul_cube_only=False → kernel_type = MIX_AIC_1_2
    └─ 否                            → kernel_type = AIV_ONLY（普通向量 kernel）
随后：
  matmul_cube_only=True 时 postprocessing 额外插入 DefineCubeOnly Pass
    → 在模块头部插入 emitc.verbatim "#define ASCENDC_CUBE_ONLY"
    → 给模块打 asc.matmul_cube_only 属性
编译目标（CompilationTarget.get）：
  MIX_AIC_1_1/1_2 → vec_arch="dav-c220-vec" + cube_arch="dav-c220-cube"（双目标）
  AIC_ONLY        → common_arch="dav-c220-cube"（单目标）
产物（CompiledKernel.core_type）：
  MIX → AiCore；AIV 系 → VectorCore；AIC_ONLY → CubeCore
```

#### 4.4.3 源码精读

推导逻辑在 [python/asc/runtime/compiler.py:L184-L189](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L184-L189)：

```python
if self.options.kernel_type is None:
    if mod.op.has_unit_attr("asc.compile_mix"):
        self.options.kernel_type = KernelType.AIC_ONLY if self.options.matmul_cube_only else\
                                   KernelType.MIX_AIC_1_2
    else:
        self.options.kernel_type = KernelType.AIV_ONLY
```

`matmul_cube_only` 是 `CompileOptions` 的字段（[python/asc/runtime/compiler.py:L27-L41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L27-L41)，定义在 L40），从装饰器小括号进入并参与缓存 key（u3-l4）。

`asc.compile_mix` 的判定源头——DetectKernelTypePass 的全部逻辑（[lib/Dialect/Asc/Transforms/DetectKernelTypePass.cpp:L33-L38](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/DetectKernelTypePass.cpp#L33-L38)）：

```cpp
if (op.walk([](ascendc::RegistMatmulObjOp) { return WalkResult::interrupt(); }).wasInterrupted())
    op->setAttr(attr::compile_mix, UnitAttr::get(op->getContext()));
```

遍历模块，发现任何一个 `RegistMatmulObjOp` 就打属性。这就是 4.3 说「RegistMatmulObjOp 有下游意义」的出处。

DefineCubeOnly 的调度与实现：[python/asc/runtime/compiler.py:L219-L230](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L219-L230) 的 `_schedule_postprocessing` 里，`if self.options.matmul_cube_only: passes.ascendc.add_define_cube_only(pm)`；Pass 本体（[lib/Dialect/Asc/Transforms/DefineCubeOnlyPass.cpp:L34-L40](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/DefineCubeOnlyPass.cpp#L34-L40)）只做两件事：

```cpp
builder.create<emitc::VerbatimOp>("#define ASCENDC_CUBE_ONLY");
mod->setAttr(ascendc::attr::matmulCubeOnly, builder.getUnitAttr());
```

又见 u6-l4 的模式：**Pass 只在 IR 上种内容/属性，真正落到纸面（C 源码里的 `#define`）是发射层的事**。这个宏会被 Ascend C 头文件用来选择 cube-only 的实现分支。

编译目标与核类型映射（[python/asc/runtime/compiler.py:L59-L75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L59-L75) 与 [L201-L209](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L201-L209)）：MIX 系给 `vec_arch + cube_arch` 双架构（编两份再 `ld.lld` 链接，见 u3-l5），其余给单架构；CoreType 按 kernel_type 归为 AiCore/VectorCore/CubeCore。KernelType 全集见 [python/asc/runtime/config.py:L36-L45](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/config.py#L36-L45)。

最后看两份示例的形态差异。CubeOnly 的装饰器与守卫（[examples/04_matmul_cube_only/matmul_cube_only.py:L31-L35](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/04_matmul_cube_only/matmul_cube_only.py#L31-L35)）：

```python
@asc.jit(matmul_cube_only=True, always_compile=True)
def matmul_kernel(...):
    if asc.ascend_is_aic():
        ...整段矩阵乘路径...
```

`ascend_is_aic`（[python/asc/language/basic/common.py:L22-L26](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/common.py#L22-L26)）生成 `AscendIsAICOp` 返回设备侧 int1 的 `PlainValue`，用来把矩阵乘路径限定在 Cube 核分支执行。启动侧对比：

- MIX（[examples/03_matmul_mix/matmul_mix.py:L84-L85](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/03_matmul_mix/matmul_mix.py#L84-L85)）：`matmul_kernel[USE_CORE_NUM // 2, ...]`，代码注释明确写着「MIX 模式下 block 数应为 AIC-AIV 组数」——48 个核按 1:1 组队就是 24 个 block；
- CubeOnly（[examples/04_matmul_cube_only/matmul_cube_only.py:L93](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/04_matmul_cube_only/matmul_cube_only.py#L93)）：`matmul_kernel[tiling.used_core_num, ...]`，block 数由 tiling 实际切分决定。

汇总成表：

| 维度 | 03 MIX | 04 CubeOnly |
| --- | --- | --- |
| 装饰器 | `@asc.jit(always_compile=True)` | `@asc.jit(matmul_cube_only=True, always_compile=True)` |
| kernel_type | MIX_AIC_1_2 | AIC_ONLY |
| 编译架构 | dav-c220-vec + dav-c220-cube 双目标 | dav-c220-cube 单目标 |
| CoreType | AiCore | CubeCore |
| 启动 block 数 | `USE_CORE_NUM // 2`（AIC-AIV 组数） | `tiling.used_core_num` |
| kernel 体守卫 | `get_block_idx() < used_core_num` | 外层 `ascend_is_aic()` + 内层同款守卫 |
| 额外宏 | 无 | `#define ASCENDC_CUBE_ONLY`（DefineCubeOnlyPass 插入） |
| 矩阵规模 | M=512, K=512, N=1024 | M=128, K=64, N=30720 |

#### 4.4.4 代码实践

**实践目标**：用 dump 产物验证两种模式在 IR 与 Ascend C 源码层的差异。

**操作步骤**：

1. 准备可运行环境（Model 后端即可，需已装好 pyasc 与 bisheng 编译链，参考 u1-l2/u1-l4）。
2. `export PYASC_DUMP_PATH=/tmp/dump_mix`，运行 `python3 examples/03_matmul_mix/matmul_mix.py -r Model`。
3. `export PYASC_DUMP_PATH=/tmp/dump_cube`，运行 `python3 examples/04_matmul_cube_only/matmul_cube_only.py -r Model`。
4. 对比三处：
   - `ascir.mlir` 首行 module 属性：两份都应有 `asc.compile_mix`（因为都含 RegistMatmulObjOp）；
   - `ascendc.cpp`：只有 CubeOnly 一份在头部出现 `#define ASCENDC_CUBE_ONLY`；
   - `codegen.mlir`：grep `Matmul`，确认 RegistMatmulObj、SetTensorA/B、SetTail、IterateAll、End 各操作的形态一致（范式同构）。
5. 无运行环境时的替代方案（纯源码推演，必可完成）：沿 4.4.3 引用的六处源码，把「`matmul_cube_only=True` 从装饰器到二进制的传播路径」抄成一条链，标注每处文件与行号。

**需要观察的现象**：`ascendc.cpp` 的宏差异是最稳定的判据；`ascir.mlir` 的模块属性次之（属性打印位置与拼写以实际 dump 为准）。

**预期结果**：MIX 与 CubeOnly 的 IR 在矩阵乘操作层面几乎一致，差异集中在「模块属性 → 宏 → 编译架构 → 启动 block 数」这条元数据链上。运行输出**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：不设置 `matmul_cube_only` 的普通向量 kernel（如 01_add）的 kernel_type 是什么？为什么不会误判成 MIX？
**答案**：AIV_ONLY。因为它的 IR 里没有 `RegistMatmulObjOp`，DetectKernelTypePass 不打 `asc.compile_mix`，`run_passes` 走 else 分支（[python/asc/runtime/compiler.py:L188-L189](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L188-L189)）。触发 MIX 推导的唯一判据就是 register_matmul 生成的那个 Op。

**练习 2**：`matmul_cube_only=True` 一共影响编译链路的哪几处？
**答案**：四处：① kernel_type 推导从 MIX_AIC_1_2 改为 AIC_ONLY（compiler.py:L186）；② postprocessing 追加 define-cube-only Pass（compiler.py:L222-L223），进而在 ascendc.cpp 头部插入 `#define ASCENDC_CUBE_ONLY` 并打 `asc.matmul_cube_only` 属性；③ CompilationTarget 从双架构变单 cube 架构（compiler.py:L68-L74）；④ 产物 CoreType 变为 CubeCore（compiler.py:L207-L208）。另外它作为 CompileOptions 字段还参与文件缓存 key。

**练习 3**：MIX 示例为什么启动 `USE_CORE_NUM // 2` 个 block，而 CubeOnly 用 `tiling.used_core_num`？
**答案**：MIX 形态下一个 block 是一对 AIC+AIV（代码注释见 [examples/03_matmul_mix/matmul_mix.py:L84](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/03_matmul_mix/matmul_mix.py#L84)），48 核对半分组得 24 个 block，tiling 的 `set_dim(48)` 也是按核数给的；CubeOnly 下没有组队概念，block 就是 Cube 核，直接用 tiling 实际切分出的 `used_core_num`。

## 5. 综合实践

综合任务（对应本讲规格的实践要求）分三步，把 4.1～4.4 串起来：

**第一步：跑通并 dump 两个示例。** 在 Model 后端下分别运行示例 03 与 04（命令见 4.4.4），各导出一份 `codegen.mlir / ascir.mlir / ascendc.cpp`。完成下表：

| 对比项 | 03 matmul_mix | 04 matmul_cube_only |
| --- | --- | --- |
| module 是否有 `asc.compile_mix` | （填写） | （填写） |
| `ascendc.cpp` 是否有 `ASCENDC_CUBE_ONLY` 宏 | （填写） | （填写） |
| IR 中 Matmul 相关操作清单（grep `Matmul`） | （填写） | （填写） |

**第二步：改规模验证 tiling 联动。** 把示例 03 的 [L110](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/03_matmul_mix/matmul_mix.py#L110) `m, k, n = 512, 512, 1024` 改成一组新规模（建议 `256, 256, 512`，保持整除关系减少尾巴干扰），重新运行。注意 `generate_tiling(m, n, k)` 与断言用的 torch 参考实现都会自动跟随新规模，无需其他改动。运行前打印 tiling 关键字段（示例代码，加在 [L116](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/03_matmul_mix/matmul_mix.py#L116) `tiling = generate_tiling(m, n, k)` 之后）：

```python
# 示例代码：Host 侧直接读 TCubeTiling 的 ctypes 字段
print("used_core_num =", tiling.used_core_num)
print("M/N/Ka/Kb =", tiling.m, tiling.n, tiling.k_a, tiling.k_b)
print("singleCore M/N/K =", tiling.single_core_m, tiling.single_core_n, tiling.single_core_k)
print("base M/N/K =", tiling.base_m, tiling.base_n, tiling.base_k)
```

**第三步：记录与解释。** 对比改前改后两组打印：`used_core_num`、`single_core_*` 如何随 M/N/K 变化；用 4.3.4 的手算方法验证新规模下某个 block_idx 的偏移；最后确认 `torch.allclose` 通过（即结果与 torch 参考一致）。

**预期结果**：改小规模后 tiling 的 M/N/K 字段精确等于新输入，`used_core_num` 与 `single_core_*` 由 tiling 引擎重新权衡（具体数值**待本地验证**）；kernel 源码零改动即适配新规模——这正是「tiling 走运行时 Struct、config 走编译期常量」分层的直接收益。若因 `always_compile=True` 担心缓存干扰，可忽略（该选项本就强制重编，见 u3-l8）。

## 6. 本讲小结

- `MatmulType`（position/format/dtype/is_trans/layout）描述单个矩阵操作数；`Matmul(a, b, c, bias?, config?)` 在 JIT 编译期把四组类型信息加 config 打包成 ascendc matmul IR 类型，物化为一条 ConstructOp——信息在类型里，不在 Python 属性里。
- `MatmulConfig` 是约 50 个编译期开关（`isConstexpr=True, isStatic=True`），`get_normal_config` 等工厂只是「按预设填字段」的捷径，`get_mm_config` 用参数包 + `isinstance` 支持任意组合传入。
- 标准调用范式：Host 算 tiling（`host.MultiCoreMatmulTiling` 填 `TCubeTiling`）→ kernel 内 `set_global_buffer → TPipe → Matmul(...) → register_matmul(pipe, workspace, matmul, tiling) → 守卫 → set_tensor_a/b → set_tail → iterate_all → end → pipe_barrier`；多核偏移由各核在设备侧读 tiling 字段现场计算。
- `TCubeTiling` 是 `Struct` 三面体：Host 侧 ctypes 可直读直写、设备侧生成成员读写 IR，因此改 tiling 值不触发重编译。
- 核形态分水岭：IR 出现 `RegistMatmulObjOp` → DetectKernelTypePass 打 `asc.compile_mix` → `matmul_cube_only=False` 推导为 MIX_AIC_1_2（双架构、AiCore、block 数 = 核数一半），`=True` 推导为 AIC_ONLY（单 cube 架构、CubeCore、额外插入 `#define ASCENDC_CUBE_ONLY`）。
- 读高阶 API 源码的通用套路仍是三段式：校验 + `get_*_type`/物化 + `create_asc_*Op`，再叠加 OverloadDispatcher 处理同名多形态（set_tensor_a 的张量/标量两态、iterate_batch 的两态）。

## 7. 下一步学习建议

1. **u7-l2（高阶 API 二）**：学习激活/归一化高阶 API 与基础向量 API 组合开发融合算子的套路，本讲的「对象式 API」与「函数式 API」混用场景会在 gelu/rmsnorm 示例中大量出现。
2. **u7-l3（lib/host）**：本讲当黑盒用的 `host.MultiCoreMatmulTiling`、`host.get_ascendc_platform()` 的加载与代理机制是下一讲的主题，学完后你可以读懂 `set_dim/set_buffer_space/get_tiling` 的底层实现。
3. **回看 u5-l2**：带着本讲的使用经验重读 Matmul 的 TypeDef 定义（`Core/Types.td` 中携带十余参数的类型），验证「前端传参 → IR 类型参数 → Ascend C 模板实参」的完整对应。
4. 想动手的读者可以做一个自测：仿照 4.3 的范式，把 03 示例改造成带 bias 的版本（参考 04 的 `enable_bias` 与 `set_bias` 路径），这是检验本讲掌握程度最好的试金石。
