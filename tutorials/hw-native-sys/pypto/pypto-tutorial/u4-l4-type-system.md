# 类型系统（u4-l4）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 PyPTO 类型系统的分层结构：`Type` 基类 → `ScalarType` / `ShapedType`（`TensorType`、`TileType`）→ 各种句柄类型，以及 `DataType` 这个「元素类型」内核的设计。
2. 区分 `TensorLayout`（ND/DN/NZ/MX）与 `TileLayout`（none_box/row_major/col_major）两套布局枚举各自描述什么。
3. 理解 `Scalar[INDEX]`、`Scalar[TASK_ID]` 这两个特殊标量类型为什么存在、从哪里产生。
4. 理解 `TileView.compact` 的准确含义：**仅分形空间（Left/Right/Acc）有意义**的有效区打包表示，本质是一个 N-fractal 行距（pitch）；以及 `AccCompactValid` 验证器为什么只认这三个空间。
5. 能根据类型推断规则**预测**表达式的结果类型——例如两个 FP16 Tile 相乘，结果为什么一定是 FP32——并在源码中指出规则所在位置。

本讲是「IR 核心解剖」单元的第四讲。上一讲（u4-l3）我们读了语句节点；本讲往下钻一层，看挂在每个表达式和变量上的**类型**到底是什么。

## 2. 前置知识

用通俗语言先把几个术语说清楚，源码精读时不再重复解释：

- **dtype（元素类型）**：一个数组里单个元素是什么格式——FP16、INT8、BOOL……在 PyPTO 里由 `DataType` 类表示。它回答「一个元素占几个 bit、按什么编码」。
- **shape（形状）**：每个维度有多长。注意 PyPTO 的 shape 不是纯整数数组，而是 `ExprPtr`（表达式）数组——维度可以是 `ConstInt(128)`，也可以是符号 `Var("M")`（动态维度）。
- **布局（layout）**：同样 shape 的数据在内存里可以有不同的排布方式。行优先、列优先、分形（fractal）排布会直接决定硬件搬运效率。
- **分形（fractal）**：昇腾矩阵计算单元的固定装载格式。矩阵数据不是连续平铺，而是切成 16×16 的小盒子（box）按格摆放。`mad`（矩阵乘指令）的输入输出都活在分形空间里。
- **有效区（valid region / valid_shape）**：物理盒子比逻辑数据大时，「真正有数据」的那个子矩形。比如尾部 tile 物理上是 [16,64]，但只有 [5,64] 是有效数据。这是 PyPTO 处理不对齐维度的核心机制（u2-l4 已见过 `valid_shape` 的 DSL 用法）。
- **行距（pitch）**：从第 i 行走到第 i+1 行要跨过的元素/字节数。分形空间里行距是按「分形块」计算的，这就是本讲重点 `compact` 的舞台。
- **MemRef（内存引用）**：指向某块分配（allocation）的字节区间，`{base, byte_offset, size}`。类型通过可选的 `memref_` 字段知道自己落在哪块内存上。
- **内存空间（MemorySpace）**：片上存储的分层——`DDR`（全局内存）、`Vec`（向量统一缓冲）、`Mat`（L1）、`Left/Right`（L0A/L0B 矩阵操作数缓冲）、`Acc`（L0C 累加缓冲）等。Tile 必须知道自己住在哪个空间。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/pypto/ir/type.h](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type.h) | 类型节点总定义：`Type` 基类、`ScalarType`、`ShapedType`、`TensorType`、`TileType`、`TensorView`/`TileView`、两个布局枚举、`CompactMode`、各种句柄类型 |
| [include/pypto/core/dtype.h](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/core/dtype.h) | `DataType` 类：类型码分区、`INDEX`/`TASK_ID` 特殊类型、`GetBit`/`ToString`/`IsFloat` 等查询 |
| [src/ir/type.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/type.cpp) | 类型实现：视图规范化（canonicalize）、`TileView` 相等与哈希、int→Expr 构造 |
| [include/pypto/ir/type_inference.h](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type_inference.h) | 类型推断工具箱：广播、类型提升、有效区读取（`GetValidShape`）、compact 打标（`StampCompactForNarrowedAccRows`、`AccPitchesCoincide`） |
| [src/ir/op/tile_ops/matmul.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp) | matmul 族的 `f_deduce_type` 推断函数——「FP16×FP16 → FP32 累加」规则的所在地 |
| [docs/en/dev/ir/02-types.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/02-types.md) | 官方类型系统文档（英文为权威版），含每个类型的 Python 用例 |

## 4. 核心概念与源码讲解

### 4.1 类型分层与 DataType 内核

#### 4.1.1 概念说明

PyPTO 的「类型」要回答三个不同粒度的问题，所以分成三层：

1. **元素是什么格式** → `DataType`（dtype.h，不属于 IR 节点树）。
2. **值的结构是什么** → IR 类型节点（`ScalarType`、`TensorType`、`TileType`……），全部继承自 `Type`，靠 `ObjectKind` 做运行时分发——和 u4-l1 讲过的节点分发机制同构。
3. **值落在哪、怎么看** → 类型上的可选附件：`memref_`（落哪块内存）、`tensor_view_`/`tile_view_`（布局与有效区）。

一个值得先建立的直觉：**IR 类型节点是「不可变值对象」**。u4-l2 见过 `As<T>()` 精确匹配 ObjectKind 的规则，这里同样适用——`As<TensorType>(distributed_tensor)` 返回空，因为 `DistributedTensorType` 有自己的 kind。

#### 4.1.2 核心流程

- `Type` 是纯虚基类，只有两个虚方法：`GetKind()`（ObjectKind 分发）与 `TypeName()`（诊断用），外加空的反射描述符。
- `DataType` 不是 C++ `enum`，而是一个包着 `uint8_t code_` 的轻量类。类型码按区间划分：`0x00-0x0F` 布尔、`0x10-0x1F` 有符号整数、`0x20-0x2F` 无符号整数、`0x30-0x3F` IEEE 浮点、`0x40-0x4F` Brain/海思浮点、`0x50-0x5F` 标识句柄。区间设计让 `IsFloat()`/`IsInt()` 这类判断退化成两次整数比较。
- `ScalarType` = `Type` + 一个 `dtype_` 字段，是最简单的具体类型。DSL 注解 `pl.Scalar[pl.INT32]` 解析后落在它身上（解析链见 u2-l2）。

#### 4.1.3 源码精读

`Type` 基类只约定分发协议，不带任何数据：

- [include/pypto/ir/type.h:L42-L67](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type.h#L42-L67) — `Type` 基类：`GetKind()` 纯虚 + `TypeName()`，注释明确「所有类型不可变」。

`DataType` 的类型码分区（读这段注释就能画出整个 dtype 家族）：

- [include/pypto/core/dtype.h:L42-L97](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/core/dtype.h#L42-L97) — `DataType` 类与按区间组织的类型码常量：有符号 `0x10-`、无符号 `0x20-`、IEEE 浮点 `0x30-`、Brain/海思浮点 `0x40-`、句柄 `0x50-`。每个区间预留 16 个槽位供扩展。

两个「特殊标量」的(dtype 定义与语义说明：

- [include/pypto/core/dtype.h:L121-L128](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/core/dtype.h#L121-L128) — `INDEX` 是「机器字长的索引整数」，语义别名而非 `INT64` 的同义词；`TASK_ID` 是 `manual_scope` 中 `pl.submit` 返回的不透明 64 位任务句柄，**不参与任何算术**。
- [docs/en/dev/ir/02-types.md:L20-L22](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/02-types.md#L20-L22) — 官方文档强调 `INDEX != INT64`（codegen 抑制两者间的隐式转换），并说明 `TASK_ID` 由 `pl.submit` 的第二个返回元素产生、以 `None` 作「尚无生产者」哨兵。

区间判断如何变成 O(1)：

- [include/pypto/core/dtype.h:L311-L340](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/core/dtype.h#L311-L340) — `IsFloat()`（含 FP4/FP8/FP16/FP32/BF16/HF4/HF8）与 `IsInt()` 都只是码区间比较；记住这一点，4.4 节的累加器规则会用到。

`ScalarType` 与位宽/字符串查询：

- [include/pypto/ir/type.h:L102-L125](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type.h#L102-L125) — `ScalarType` 只有一个 `dtype_` 字段，反射描述符把它暴露给结构化比较与序列化（u4-l8 主题）。
- [include/pypto/core/dtype.h:L153-L196](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/core/dtype.h#L153-L196) — `GetBit()` 精确到亚字节类型（INT4 返回 4），`GetByte()` 对亚字节类型向上取整为 1。
- [include/pypto/core/dtype.h:L203-L252](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/core/dtype.h#L203-L252) — `ToString()` 给出打印/序列化用的规范名（`"fp16"`、`"task_id"`……）。

#### 4.1.4 代码实践

**实践目标**：用 Python 侧绑定核对 `DataType` 的分区行为，建立「区间判断」的手感。

1. 操作步骤（在仓库根目录，确保已按 u1-l2 完成开发模式安装）：

   ```python
   # 示例代码：探索 DataType（可存为临时脚本或 REPL 中逐行执行）
   from pypto import DataType

   for dt in (DataType.BOOL, DataType.INT4, DataType.FP16, DataType.BF16,
              DataType.INDEX, DataType.TASK_ID):
       print(f"{dt.ToString():10s} bit={dt.GetBit():3d} byte={dt.GetByte():3d} "
             f"float={dt.IsFloat()}")
   print(DataType.INDEX == DataType.INT64)   # 关键断言
   ```

2. 需要观察的现象：`INDEX` 与 `INT64` 的比较结果；`TASK_ID` 的 `GetBit()`。
3. 预期结果：`bool bit=1`、`int4 bit=4`、`fp16 bit=16`、`bfloat16 bit=16`、`index bit=64`、`task_id bit=64`；`IsFloat()` 对前四个里只有 `fp16`/`bfloat16` 为 True；`INDEX == INT64` 为 `False`（它们是不同类型码）。以上均由 dtype.h 的 switch 与常量定义直接决定，具体打印格式待本地验证。
4. 说明：`ToString`/`GetBit` 的每个取值都可对照 [include/pypto/core/dtype.h:L153-L252](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/core/dtype.h#L153-L252) 逐个核对——这是「读源码预测输出」的最小闭环。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `IsFloat()` 写成两次区间比较而不是列举所有浮点类型名？

**答案**：类型码按类别分区（`0x30-0x3F` IEEE 浮点、`0x40-0x4F` Brain/海思浮点），区间判断让新增同类别 dtype 无需改动判断函数；且区间里预留的空槽（如 `0x37-0x3F`）保证了扩展不与既有码冲突。见 [include/pypto/core/dtype.h:L71-L89](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/core/dtype.h#L71-L89) 的分区注释与 [L311-L315](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/core/dtype.h#L311-L315) 的实现。

**练习 2**：`Scalar[pl.INDEX]` 与 `Scalar[pl.INT64]` 在 IR 里是同一个类型吗？什么时候会踩坑？

**答案**：不是。两者是不同的 `DataType` 码（`0x15` vs `0x14`），`ScalarType` 按 `dtype_` 区分；文档明确 `INDEX != INT64` 且 codegen 抑制两者隐式互转（[docs/en/dev/ir/02-types.md:L20](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/02-types.md#L20)）。把循环变量/偏移（INDEX 语义）直接交给声明为 INT64 标量的算子时，需要显式转换——踩坑表现是类型检查报错而非静默通过。

**练习 3**：`GetBit()==8` 的 dtype 有哪几个？`GetByte()` 各是多少？

**答案**：HF8、FP8E4M3FN、FP8E5M2、FP8E8M0、UINT8、INT8（见 `GetBit` 的 `case` 列表 [include/pypto/core/dtype.h:L162-L168](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/core/dtype.h#L162-L168)）；`GetByte()` 对它们全部返回 1（`ceil(8/8)`）。

---

### 4.2 形状类型与视图：ShapedType、TensorType 与 TensorView

#### 4.2.1 概念说明

`TensorType` 与 `TileType` 共享「有形状的值」这一定位，所以抽出公共基类 `ShapedType`，装三个字段：`dtype_`（元素格式）、`shape_`（每维一个 `ExprPtr`）、`memref_`（可选内存引用）。

「视图（view）」是类型上的一块**可选元数据**，回答「这个类型的值在内存里怎么读」：

- `TensorView` 服务 Tensor（全局内存侧）：`stride`（每维步长）、`layout`（`TensorLayout` 枚举标签）、`valid_shape`（有效区）、`pad`（越界读时的填充模式）。
- Tensor 的内存空间恒为 `DDR`（`GetMemorySpace()` 直接返回），不需要单独存。

`TensorLayout` 五个值的分工：`ND`/`DN` 是两种常规排布（DN 即转置存放），`NZ` 是分形排布（**仅 Tile 侧合法**，视图校验器会拒绝 TensorType 上的 NZ），`MX_A_ZZ`/`MX_B_NN` 是 MX 块缩放数据的 GM 装载路径标签。

#### 4.2.2 核心流程

一个关键机制是**构造期规范化（canonicalization）**：`TensorType` 构造函数会做两件事——

```text
构造 TensorType(shape, dtype, memref, tensor_view)
  ├─ 1. valid_shape 与 shape 全等 → 清空 valid_shape（冗余）
  ├─ 2. 视图为「空 stride + ND + 无 valid_shape + pad=null」→ 整个视图重置为 nullopt
  └─ 结果：完全规整的 Tensor 在 IR 打印里根本看不到 view
```

这解释了打印 IR 时的现象：只有携带非默认信息的类型才出现 `TensorView(...)` 字样。RFC #1300 进一步规定 `(shape, stride, layout)` 三元组有唯一规范解释：`layout` 是可在 `(shape, stride)` 上推导的**标签**而非独立描述；`MaterializeTensorStrides` Pass（流水线 30 号）会把隐式形式改写成显式规范步长（详见 [docs/en/dev/ir/02-types.md:L130-L169](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/02-types.md#L130-L169)）。

#### 4.2.3 源码精读

公共基类与三个共享字段：

- [include/pypto/ir/type.h:L389-L445](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type.h#L389-L445) — `ShapedType`：`dtype_` / `shape_`（`std::vector<ExprPtr>`，支持符号维）/ `memref_`（可选）。注意它自己也是 `ObjectKind::ShapedType`，属于 u4-l2 讲过的「基类型带 kinds 数组」一族，`As<ShapedType>` 才能同时匹配 Tensor/Tile。

`TensorLayout` 枚举与 MX 判定：

- [include/pypto/ir/type.h:L129-L148](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type.h#L129-L148) — `ND/DN/NZ/MX_A_ZZ/MX_B_NN` 五个布局值，以及 `IsMxTensorLayout()` 判定函数。

`TensorView` 结构体（四个字段 + 反射）：

- [include/pypto/ir/type.h:L167-L236](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type.h#L167-L236) — `PadValue`（null/zero/max/min，`TensorView` 与 `TileView` 共用）与 `TensorView`；提供 int 重载构造函数，整数自动包成 `ConstInt`（INDEX dtype）。

`TensorType` 与 DDR 空间：

- [include/pypto/ir/type.h:L449-L532](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type.h#L449-L532) — `TensorType` = `ShapedType` + 可选 `tensor_view_`；`GetMemorySpace()` 恒返回 `MemorySpace::DDR`（L521），`IsDNLayout()` 是常见布局快捷判断（L523-L526）。

构造期规范化的实现：

- [src/ir/type.cpp:L72-L101](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/type.cpp#L72-L101) — `ClearRedundantFullValidShape` / `CanonicalizeTensorViewInPlace` / `CanonicalizeTileViewInPlace`：全等 valid_shape 清空、隐式视图塌缩为 `nullopt`（Tile 版按给定内存空间的隐式布局判定）。

配套文档（英文权威版）：

- [docs/en/dev/ir/02-types.md:L84-L128](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/02-types.md#L84-L128) — TensorView 的 Python 用例（stride/valid_shape/pad 均可传 int）与字段说明；ND/DN 的 packed 规范步长公式在 L144-L148 的表格。

#### 4.2.4 代码实践

**实践目标**：亲手构造两个只差 `valid_shape` 的 `TensorType`，观察规范化行为——「全等 valid_shape 会被吃掉」。

1. 操作步骤（示例代码）：

   ```python
   # 示例代码：观察 TensorView 规范化
   from pypto import ir

   # 显式给 valid_shape == shape：构造后应被清空
   tv_full = ir.TensorView(stride=[1, 128], layout=ir.TensorLayout.ND,
                           valid_shape=[64, 128])
   t_full = ir.TensorType([64, 128], ir.DataType.FP16, None, tv_full)
   print(ir.python_print(t_full))

   # 真正的子矩形：valid_shape 保留
   tv_part = ir.TensorView(stride=[1, 128], layout=ir.TensorLayout.ND,
                           valid_shape=[7, 128])
   t_part = ir.TensorType([64, 128], ir.DataType.FP16, None, tv_part)
   print(ir.python_print(t_full) == ir.python_print(t_part))
   ```

2. 需要观察的现象：第一个打印里 `valid_shape=[64, 128]` 是否消失；两个类型的打印是否相同。
3. 预期结果：`t_full` 的视图因 valid_shape 与 shape 全等被清空（[src/ir/type.cpp:L72-L76](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/type.cpp#L72-L76)），但由于本例还携带显式 stride，视图不会整体塌缩；`t_part` 保留 `valid_shape=[7, 128]`，因此两者打印不同、比较为 `False`。`python_print` 对 `Type` 对象走 `python_print_type` 分发（[python/pypto/ir/printer.py:L43-L45](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/printer.py#L43-L45)），具体打印文本待本地验证。
4. 若想同时验证「视图整体塌缩」：把 `stride` 留空、`layout=ND`、不给 valid_shape/pad，构造后视图应变成 `None`，打印里完全看不到 `TensorView` 字样。

#### 4.2.5 小练习与答案

**练习 1**：`NZ` 布局为什么不允许出现在 `TensorType` 上？

**答案**：NZ 是分形（fractal）排布，无法用「每维一个步长」的平铺 stride 表示——规范步长表格明确标注「not representable as flat strides — tile-only fractal」（[docs/en/dev/ir/02-types.md:L148](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/02-types.md#L148)）。Tensor 的 stride 体系装不下它，视图校验器两种模式都拒绝 TensorType 上的 NZ。

**练习 2**：`TensorType` 需要单独存内存空间字段吗？为什么？

**答案**：不需要。`GetMemorySpace()` 直接返回 `MemorySpace::DDR`（[include/pypto/ir/type.h:L521](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type.h#L521)）——Tensor 语义上就是全局内存数组，空间恒定；对照 `TileType` 则必须存 `memory_space_`（片上空间有 Vec/Mat/Left/Right/Acc 等多种可能）。

---

### 4.3 TileType、TileView 与 compact 分形打包（本讲重点）

#### 4.3.1 概念说明

`TileType` = `ShapedType` + 可选 `tile_view_` + 可选 `memory_space_`。Tile 是硬件感知数据块（u1-l4 讲过它与 Tensor 的分工），所以视图元数据比 Tensor 侧 richer：

| TileView 字段 | 含义 |
| --- | --- |
| `valid_shape` | 有效区（空 = 全有效） |
| `stride` | 每维步长 |
| `start_offset` | 起始偏移 |
| `blayout` | 块布局（`TileLayout`：none_box/row_major/col_major） |
| `slayout` | 散列布局（同枚举） |
| `fractal` | **分形盒大小，单位是字节不是元素** |
| `pad` | 越界填充模式 |
| `compact` | 有效区打包标记（`CompactMode`）——本节主角 |

`TileLayout` 与 `TensorLayout` 是两套不重叠的枚举：前者只表达「无约束/行主序/列主序」三种排布约束，后者是 GM 侧的存储格式标签。别混用。

**`compact` 到底是什么**：分形空间里的物理盒子按 16 行一个分形块摆放。当有效行数不是 16 的倍数（「行收窄」，row-narrowed）时，同一个 L0C 缓冲有两种读法：

- 普通读法（`CompactMode::null`）：行距取**物理行数**；
- 紧凑读法（`CompactMode::normal`）：行距取 \(\lceil \text{validRow}/16 \rceil \times 16\)，即只按有效行打包。

问题在于 `mad`（矩阵乘指令）**写入** L0C 时永远按紧凑行距写（M 取自左操作数的**有效**行数），而读取方默认按物理行数读——两边行距不一致时，第二个分形块开始的数据全部错位。这就是 issue #2470/#2510 的根因，`compact` 标记的存在就是让「写入行距」与「读取行距」重新对齐。

**为什么只在 Left/Right/Acc 三个空间有意义**：因为 compact 本质上**就是**一个 N-fractal 行距属性，只有分形空间（L0A/L0B/L0C）的行距才由分形块推导；Vec/Mat 等非分形空间根本没有这个概念，标了也无处生效——`AccCompactValid` 验证器因此拒绝分形空间之外的一切 compact 标记。

#### 4.3.2 核心流程

compact 标记的生命周期（谁写、谁读、谁验证）：

```text
mad 指令写 L0C：行距 = ceil(L0A有效行数 / 16) × 16     ← 硬件行为，不可配置
        │
        ▼
tile.matmul 的 f_deduce_type 推断结果类型
    StampCompactForNarrowedAccRows(view, physical_shape)
    └─ 「无法证明 valid_rows == physical_rows」→ compact = normal
       （ stamper 取安全方向：证明不了相等就当作收窄处理 ）
        │
        ├── tile.matmul_acc：不重新推导，直接【继承】acc 的 compact
        │   （ 结果别名 acc 缓冲，codegen 只在 TileBufSignature 一致时才别名 ）
        │
        ├── AutoTileMatmulL0：合成累加器种子时显式声明
        │   tile.create([m, n], dtype, target_memory=Acc, compact=True)
        │   （ kwarg 会在每次重新推导时被重读，比 Pass 事后改类型更耐久 ）
        │
        └── tile.extract 部分搬运进 L0A/L0B 时同样自动打标（#2232）
        │
        ▼
AccCompactValid 验证器（InferTileMemorySpace 之后可验证）：
    规则 1：matmul_acc / matmul_mx_acc 的 lhs 有效行使行距 ≠ acc 物理行数
           → acc 必须 compact
    规则 2：分形空间（Left/Right/Acc）之外的任何 Tile 不得携带 compact
```

判定「两种读法行距是否相同」的数学核心，设物理行数 \(R\)、有效行数 \(v\)、分形行块 \(F = 16\)：

\[ \text{两种行距一致} \iff \lceil v/16 \rceil \times 16 = R \]

它成立的两种情形：有效行填满物理盒（\(v = R\)），或物理盒恰好只有一个分形行块（\(R = 16\)，此时任意 \(v \le 16\) 打包后都是 16）。

#### 4.3.3 源码精读

`TileView` 与「fractal 是字节数」的关键注释：

- [include/pypto/ir/type.h:L284-L311](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type.h#L284-L311) — `TileView` 的文档注释：`fractal` 是**字节**数；矩阵乘路径两个值（512 = 16×16 FP16 操作数盒、1024 = 16×16 FP32/INT32 累加器盒）描述的是不同 dtype 下的同一 16×16 几何。
- [include/pypto/ir/type.h:L303-L340](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type.h#L303-L340) — 八个字段与全参构造函数（`compact` 默认 `CompactMode::null`）。
- [include/pypto/ir/type.h:L262-L282](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type.h#L262-L282) — `CompactMode` 枚举与字符串互转声明。

`TileType` 与内存空间约束：

- [include/pypto/ir/type.h:L608-L641](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type.h#L608-L641) — `TileType`：携带 MemRef 时**必须**显式给出 memory_space（构造经 `ValidateMemorySpace` 校验，见 [src/ir/type.cpp:L40-L51](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/type.cpp#L40-L51)）。
- [include/pypto/ir/memory_space.h:L35-L45](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/memory_space.h#L35-L45) — `MemorySpace` 枚举全表：DDR/Vec/Mat/Left/Right/Acc/Bias/ScalarLocal/LeftScale/RightScale。

打标与判定函数（type_inference.h，**本次增量更新的核心新增**）：

- [include/pypto/ir/type_inference.h:L677-L679](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type_inference.h#L677-L679) — `kAccFractalRows = 16`：L0C 分形行块常量，`mad` 把 M 向上取整到它。
- [include/pypto/ir/type_inference.h:L681-L709](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type_inference.h#L681-L709) — `AccPitchesCoincide`：判定紧凑/非紧凑两种读法行距是否**可证明**一致。注意注释里 stamper 与 checker 的方向差异——stamper「证明不了就打标」（安全），checker「证明不了差异就不能拒绝」（否则误杀合法 IR）；单个分形块盒 `[16, N]` 对任意有效行数都返回 true。
- [include/pypto/ir/type_inference.h:L711-L742](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type_inference.h#L711-L742) — `StampCompactForNarrowedAccRows`：给 L0C 视图打 compact 标记；文档注释完整解释了 #2470 的错位机理，并说明**只有行方向**参与判定（列收窄不改变行距，保持历史形态）。

打标与继承的两个使用现场：

- [src/ir/op/tile_ops/matmul.cpp:L108-L121](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L108-L121) — `DeduceTileMatMulType`：推断 `tile.matmul` 结果时调 `StampCompactForNarrowedAccRows`（L118），产出的 TileType 落在 `MemorySpace::Acc`，fractal=1024 的 Nz 布局。
- [src/ir/op/tile_ops/matmul.cpp:L194-L211](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L194-L211) — `DeduceTileMatMulAccType`：`tile.matmul_acc` 是 `set_output_reuses_input(0)` 的原地算子，结果**继承** acc 的 compact（L202-L208 注释解释了为何不重推导：codegen 仅在 `TileBufSignature`（含 compact）一致时才别名，继承保证别名按构造合法）。

`tile.create` 的 compact 声明路径（合成的累加器种子）：

- [src/ir/op/tile_ops/memory.cpp:L591-L624](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/memory.cpp#L591-L624) — `tile.create` 的 `compact` kwarg：文档注释说明「在创建时声明而非事后盖章」的原因——Pass 施加的类型细化会在任何 Pass 重新推导该调用时被丢弃（`InferTileMemorySpace` 就会），kwarg 则每次都被重读；L618-L624 守卫 `compact=true` 只允许 `target_memory=Acc`。
- [src/ir/transforms/auto_tile_matmul_l0_pass.cpp:L372-L391](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/auto_tile_matmul_l0_pass.cpp#L372-L391) — `BuildAccInit`：自动矩阵乘分块 Pass 合成累加器初值时，按需带上 `{"compact", true}` kwarg（L385-L387）。

验证器与打印：

- [include/pypto/ir/transforms/ir_property.h:L107-L114](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/ir_property.h#L107-L114) — `IRProperty::AccCompactValid` 的定义：两条规则（行收窄的 matmul_acc 必须 compact；分形空间之外不得有 compact），并注明需等 `InferTileMemorySpace` 解析完内存空间后才可验证。
- [src/ir/transforms/python_printer.cpp:L3301-L3313](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L3301-L3313) — 打印器对 compact 的处理：`null` 省略，`normal` 打成 `compact=pl.CompactMode.normal`——这就是它在 IR 文本里的样子。
- [docs/en/dev/ir/02-types.md:L203-L216](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/02-types.md#L203-L216) — 本次更新后的官方文档段落：明确 compact 仅在 Left/Right/Acc 有意义（因为它本身是 N-fractal 行距）、编译器自动设置的三条路径、普通用户代码无需手工选择。

#### 4.3.4 代码实践

**实践目标**：在测试里看到一个「行收窄累加器」的 compact 标记如何写进 `TileType`，并让验证器证明它守护的正是行距一致性。

1. 实践目标：读懂 `AccCompactValid` 的正反用例，理解 compact 在类型层面的承载方式。
2. 操作步骤：
   - 打开 [tests/ut/ir/verifier/test_acc_compact.py:L49-L86](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/verifier/test_acc_compact.py#L49-L86)。关注 `_matmul_acc_program` 如何构造 acc 类型：物理 `[64, 128]`、有效行仅 16、dtype INT32、`ir.TileView(valid_shape=[valid_rows, cols], compact=acc_compact)`、空间 `Acc`；lhs 是 `[64, 256]` 但有效行 16 的 **Left** 空间 Tile（同样带 `compact=normal`）。
   - 再看反向用例 [tests/ut/ir/verifier/test_acc_compact.py:L114-L120](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/verifier/test_acc_compact.py#L114-L120)：同一个构造把 `compact` 换成 `CompactMode::null`，验证器应报 `AccCompactValid` 诊断，消息里含 `tile.matmul_acc` 与 `compact` 字样——因为 `mad` 按 16 的行距写、读取方按物理 64 的行距读。
   - （可选，待本地验证）在仓库根目录运行：`source .claude/skills/testing/load-env.sh && python -m pytest tests/ut/ir/verifier/test_acc_compact.py -v`（并行度遵守 `PYPTO_TEST_JOBS`）。
3. 需要观察的现象：诊断的 `rule_name` 是否为 `AccCompactValid`；报错消息是否指向「mad 写入行距 vs 物理行数」的不一致。
4. 预期结果：`test_row_narrowed_non_compact_accumulator_is_rejected` 恰好产生 1 条诊断且断言通过（测试源码即为此断言）；文件头注释（L9-L30）把 #2470/#2510 两个 issue 的机理写得很完整，值得通读。测试运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：一个 `[16, 128]` 的 gemv 累加器，有效行只有 1，需要打 compact 吗？

**答案**：按 `AccPitchesCoincide` 的判定不需要强制——物理行数是常量 16，恰好等于单个分形行块 `kAccFractalRows`，函数最后一个 return 直接给出 true（[include/pypto/ir/type_inference.h:L708](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type_inference.h#L708)）：任何有效行打包后都还是 16，两种读法行距恒一致。文档注释专门点名了这个 `[16, N]` gemv 累加器的例子。反过来，stamper 侧因为「证明不了相等就打标」，可能仍给它标 compact——但这是安全方向（打包行距恰好等于原行距，读取不变）。

**练习 2**：`tile.matmul_acc` 为什么继承 acc 的 compact 而不像 `tile.matmul` 那样重新推导？

**答案**：`tile.matmul_acc` 的结果**就是** acc 的缓冲（`set_output_reuses_input(0)`），codegen 只在两者的 `TileBufSignature`（含 compact）一致时才做别名；继承保证别名按构造合法。而累加器的行距是在最初那次 `tile.matmul`（或 `tile.create(compact=True)` 种子）上确立的，`tile.matmul_acc` 不是确立行距的地方（[src/ir/op/tile_ops/matmul.cpp:L202-L208](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L202-L208) 注释原文）。

**练习 3**：一个 `MemorySpace::Vec` 的 Tile 声明了 `compact=normal`，验证器会怎么判？为什么这个约束是合理的？

**答案**：`AccCompactValid` 第二条规则直接拒绝（测试 `_vec_tile_program` 构造的就是这个场景，[tests/ut/ir/verifier/test_acc_compact.py:L89-L111](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/verifier/test_acc_compact.py#L89-L111)）。合理性：compact 本质是 N-fractal 行距，只有分形空间（Left/Right/Acc）的行距才由分形块推导；Vec 空间没有这个概念，标记既无意义又可能误导后续 Pass 与 codegen。

---

### 4.4 类型推断：从操作数类型推出结果类型

#### 4.4.1 概念说明

前面三节讲「类型长什么样」，这一节讲「类型从哪来」。答案分两层：

1. **通用规则层**（type_inference.h）：NumPy 风格的形状广播（`BroadcastShapes`）、数值类型提升（`PromoteDataTypes`）、维度等价判定（`DimensionsEqual`）、有效区读取（`GetValidShape`）。这些是所有算子共享的工具箱。
2. **算子专属层**（`f_deduce_type`）：每个算子注册自己的推断函数。`pl.matmul(a, b)` 被解析成 `Call` 时，OpRegistry 调用该算子的 deducer，由它构造结果 `TileType`——「FP16×FP16 → FP32」就住在 `DeduceTileMatMulType` 里。

还有一个贯穿所有推断的难点：**符号维度的等价证明**。shape 与 valid_shape 是表达式，`Var("M")` 和 `Var("M")` 相等容易，`(x + 64) - x` 和 64 相等呢？所以 PyPTO 用三值证明结果 `ProofResult { kTrue, kFalse, kUnknown }`——只有能**证明**的关系才参与决策，证明不了就取安全方向（compact 打标取「当作收窄」，验证拒绝取「不拒」）。

#### 4.4.2 核心流程

以 `tile.matmul(lhs, rhs)` 为例的完整推断流程：

```text
Call(tile.matmul, [lhs, rhs]) 创建
  │
  ├─ 1. DeduceMatmulProductInfo(lhs_type, rhs_type)
  │     ├─ 校验：两侧必须 2D；物理 K 必须相等（L0 提取直接按盒索引）
  │     ├─ 校验：rhs 有效 K 覆盖 lhs 有效 K（用 ProveValidExtentLessEqual）
  │     ├─ 校验：两侧 dtype 必须相同
  │     └─ 累加器 dtype = (lhs浮点 且 rhs浮点) ? FP32 : INT32   ★ 本讲实践要找的规则
  │
  ├─ 2. 组装 TileView
  │     ├─ 布局：Acc 空间的隐式 Nz 布局（col_major × row_major, fractal=1024）
  │     ├─ valid_shape = [lhs有效M, rhs有效N]     ← 逻辑矩形跟有效区
  │     └─ StampCompactForNarrowedAccRows(...)     ← 行收窄时 compact=normal
  │
  └─ 3. 返回 TileType(物理 shape=[lhsM, rhsN], 累加器dtype, 无memref, view, space=Acc)
```

注意「物理 shape 跟物理、有效区跟有效」的双轨：结果的**存储**大小由物理 M/N 决定，**逻辑**矩形由两侧有效 M/N 决定——这正是 4.3 节 compact 问题的土壤。

#### 4.4.3 源码精读

通用工具箱：

- [include/pypto/ir/type_inference.h:L71-L111](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type_inference.h#L71-L111) — `BroadcastShapes`（右对齐、大小 1 可扩、缺维当 1）与 `PromoteDataTypes`（浮点优先于整数、大类型优先、同宽有符号优先）的规则文档，含具体例子。
- [include/pypto/ir/type_inference.h:L146-L186](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type_inference.h#L146-L186) — `DimensionsEqual`（常数比值、符号经算术分析器证明，如 `(x + 64) - x` 认作 64）与三值 `ProofResult` / `ProveValidExtentEqual`。
- [include/pypto/ir/type_inference.h:L767-L802](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type_inference.h#L767-L802) — `GetValidShape` 的 Tile/Tensor 两个重载：视图里没有 valid_shape 就回退物理 shape。这是所有推断函数读有效区的标准入口（issue #1370：逐元素算子不传播有效区会导致 codegen 收到不匹配的 validRow/validCol）。
- [include/pypto/ir/type_inference.h:L659-L675](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type_inference.h#L659-L675) — `InheritTileViewLayout`：多数 tile 算子保留主输入的 blayout/slayout/pad/**compact**，一行式继承避免重复内联。
- [include/pypto/ir/type_inference.h:L804-L823](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type_inference.h#L804-L823) — `MakeFreshTensorType`：新计算（非别名）结果的 TensorType 只带自身元数据（默认布局、无 stride、无源 memref），全等 valid_shape 由构造器规范化掉。

matmul 族的专属推断：

- [src/ir/op/tile_ops/matmul.cpp:L56-L89](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L56-L89) — `DeduceMatmulProductInfo`：完整的矩阵积契约校验；**L85-L86 就是累加器 dtype 规则**——两侧都浮点给 `FP32`，否则 `INT32`。
- [src/ir/op/tile_ops/matmul.cpp:L93-L122](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L93-L122) — `DeduceTileMatMulType`：拼装 Acc 空间的 Nz 视图并打 compact 标（见 4.3.3）。

类型相等与哈希的粒度契约（衔接 u4-l8）：

- [src/ir/type.cpp:L105-L112](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/type.cpp#L105-L112) — `TileView::operator==`：逐字段比较，表达式字段走 `AreExprsEqual` 家族。
- [src/ir/type.cpp:L119-L157](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/type.cpp#L119-L157) — `HashExprForAreExprsEqual`：**哈希必须与相等粒度严格同步**——ConstInt 按值、二元/一元/Call 按结构、其余按指针。本次增量更新把一元表达式（`UnaryExpr`）纳入结构化比较与哈希（L140-L148 是新增分支：kind + 结果 dtype + 操作数三要素都得进哈希，否则两个相等的 TileView 会落进不同桶）。注释里的契约原话：`AreExprsEqual` 的任何扩展必须在这里得到对应分支。
- [src/ir/type.cpp:L161-L174](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/type.cpp#L161-L174) — `Hash(TileView)`：八字段逐个混入，表达式字段用上面的粒度哈希。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：让 `tile.load`（FP16 输入）与 `tile.matmul` 的结果类型被推断为 FP32，打印 IR 验证，并在源码中指出规则位置；再观察行收窄累加器的 compact 标记在 IR 类型中的体现。

1. 操作步骤：

   第一步，写一个 FP16 矩阵乘算子（参照 [examples/beginner/05_matmul.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/beginner/05_matmul.py) 的内存层级链，示例代码）：

   ```python
   # 示例代码：FP16 输入 → FP32 累加器
   import pypto.language as pl
   import torch

   @pl.jit
   def matmul_fp16(a: pl.Tensor, b: pl.Tensor, c: pl.Out[pl.Tensor]):
       with pl.at(level=pl.Level.CORE_GROUP):
           a_l1 = pl.load(a, [0, 0], [64, 64], target_memory=pl.MemorySpace.Mat)
           b_l1 = pl.load(b, [0, 0], [64, 64], target_memory=pl.MemorySpace.Mat)
           a_l0a = pl.move(a_l1, target_memory=pl.MemorySpace.Left)
           b_l0b = pl.move(b_l1, target_memory=pl.MemorySpace.Right)
           acc = pl.matmul(a_l0a, b_l0b)          # ← 推断目标：结果类型
           pl.store(acc, [0, 0], c)
       return c
   ```

   第二步，取降级后的 IR 并打印（`lower` 返回跑完 Pass 流水线的 `ir.Program`，见 [python/pypto/jit/decorator.py:L2263-L2281](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/jit/decorator.py#L2263-L2281)）：

   ```python
   # 示例代码：打印推断后的类型
   import torch
   from pypto import ir

   a = torch.randn(64, 64, dtype=torch.float16)
   b = torch.randn(64, 64, dtype=torch.float16)
   prog = matmul_fp16.lower(a, b)
   print(ir.python_print(prog))
   ```

   第三步，在源码中定位规则：两个 FP16 Tile 相乘结果为 FP32 的判定在 [src/ir/op/tile_ops/matmul.cpp:L85-L86](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L85-L86)（`IsFloat() && IsFloat() ? FP32 : INT32`）；推断函数沿 `GetValidShape`（[include/pypto/ir/type_inference.h:L779-L784](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type_inference.h#L779-L784)）读取有效区、沿 `StampCompactForNarrowedAccRows`（L735-L742）决定 compact。

   第四步，观察 compact 标记：本例 64×64 全有效，`AccPitchesCoincide` 成立，**不会**出现 `compact=`。要看标记，需读 4.3.4 的测试（行收窄场景），或写一个 M 维不被 16 整除、用 `valid_shape` 收窄的 load+matmul 组合再打印——打印文本中的形态由 [src/ir/transforms/python_printer.cpp:L3301-L3313](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/python_printer.cpp#L3301-L3313) 决定：`compact=pl.CompactMode.normal`。

2. 需要观察的现象：打印的 IR 中 `matmul` 结果 Tile 的 dtype 是否为 `fp32`；`tile.load` 结果是否仍是 `fp16`；全有效场景下是否没有 `compact=` 字样。
3. 预期结果：`acc = pl.matmul(a_l0a, b_l0b)` 一行的结果注解是 FP32 的 TileType（物理 [64,64]、Acc 空间、fractal=1024 的 Nz 视图），而两个 load 结果是 FP16。规则出处即第三步给出的两行源码。具体打印文本待本地验证（`lower` 的完整 Pass 后 IR 形态依赖流水线版本）。
4. 延伸验证（数值侧，待本地验证）：用 `matmul_fp16(a, b, c_out)` 执行并与 `torch.matmul(a.float(), b.float())` 做 `torch.allclose`（rtol/atol 放宽到 FP16 量级），确认 FP32 累加没有引入额外误差。

#### 4.4.5 小练习与答案

**练习 1**：`INT8` Tile × `INT8` Tile 的 `tile.matmul` 结果 dtype 是什么？依据是哪一行？

**答案**：`INT32`。规则是「两侧都 `IsFloat()` 才给 FP32，否则 INT32」（[src/ir/op/tile_ops/matmul.cpp:L85-L86](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L85-L86)）；INT8 落在 `0x10-0x1F` 整数区间，`IsFloat()` 为 false。

**练习 2**：`PromoteDataTypes(INT32, FP32)` 和 `PromoteDataTypes(UINT32, INT32)` 各返回什么？

**答案**：`FP32` 与 `INT32`。规则文档写明：浮点优先于整数、大类型优先、同宽有符号优先于无符号（[include/pypto/ir/type_inference.h:L92-L111](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/type_inference.h#L92-L111) 的 Examples 列表原样给出了这两组）。

**练习 3**：为什么 `HashExprForAreExprsEqual` 扩展一元表达式分支是必须的，而不是优化？

**答案**：哈希与相等必须同粒度，否则「相等的两个 TileView 落进不同哈希桶」——以 TileView 为键的哈希容器会把结构相等的类型当成不同键，缓存与结构化比较（u4-l8）静默失效。`AreExprsEqual` 把一元表达式改为按（kind + 结果 dtype + 操作数）结构比较后，哈希若不跟进，契约 `lhs == rhs ⇒ Hash(lhs) == Hash(rhs)` 被破坏。见 [src/ir/type.cpp:L125-L148](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/type.cpp#L125-L148) 的注释（「any extension to AreExprsEqual MUST get a corresponding branch here」）。

**练习 4（进阶）**：`tile.matmul` 结果的物理 shape 为什么取 `[lhs物理M, rhs物理N]` 而有效区取 `[lhs有效M, rhs有效N]`？

**答案**：结果的**存储分配**必须容纳下游 L0 提取按物理盒的直接索引（物理 K 必须相等就是同一理由，[src/ir/op/tile_ops/matmul.cpp:L66-L73](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/matmul.cpp#L66-L73)）；而**逻辑语义**（矩阵积的定义域）只覆盖有效矩形。两轨并行正是有效区机制的意义，也是行收窄（有效 M < 物理 M）这个 compact 场景的来源（L88 的返回值把两者分开打包）。

---

## 5. 综合实践

**任务：给一个「不对齐 GEMM」做类型观察笔记。**

写一个 M=100（不被 16 整除）的 FP16 矩阵乘算子：输入 `pl.Tensor[[100, 256]]` 与 `pl.Tensor[[256, 64]]`，用 `pl.load(..., valid_shape=...)`（尾部 tile 收窄）+ `pl.move` + `pl.matmul` + `pl.store` 完成，然后：

1. 用 `kernel.lower(a, b)` 取 IR 并 `ir.python_print` 打印。
2. 在打印文本中逐项找出并记录：
   - load 结果的 dtype 与 valid_shape（FP16、有效 M 是多少）；
   - `matmul` 结果的 dtype（应为 FP32）与所在内存空间（Acc）；
   - 是否出现 `compact=pl.CompactMode.normal`——按 `StampCompactForNarrowedAccRows` 的「证明不了相等就打标」策略，推断一下你会不会看到它；
   - store 目标的 dtype 与有效区。
3. 把每一项观察对应回源码：dtype 规则（matmul.cpp L85-L86）、有效区读取（type_inference.h `GetValidShape`）、compact 打标（type_inference.h L735-L742）、打印形态（python_printer.cpp L3301-L3313）。
4. 用 torch 对照验证数值正确（FP16 容差），确认「类型层收窄 + compact 打包」没有破坏语义。

预期产物：一张「IR 打印片段 → 类型字段 → 源码规则」三列对照表。这张表就是你对本讲的全部内容的自检——能填满它，类型系统这条链你就走通了。（运行结果待本地验证。）

## 6. 本讲小结

- PyPTO 类型分三层：`DataType`（元素格式的类型码内核，按区间分区）→ IR 类型节点（`Type` 基类 + ObjectKind 分发）→ 类型附件（`memref_`、视图、内存空间）。
- `ShapedType` 是 Tensor/Tile 的公共基座（dtype/shape/memref）；`TensorType` 恒在 DDR，`TileType` 必须显式管理片上内存空间；视图在构造期被规范化，冗余信息（全等 valid_shape、默认视图）会被吃掉。
- `TensorLayout`（ND/DN/NZ/MX，GM 存储格式）与 `TileLayout`（none_box/row_major/col_major，片上排布约束）是两套不重叠的枚举；`TileView.fractal` 的单位是**字节**（512=16×16 FP16 盒，1024=16×16 FP32 盒）。
- `Scalar[INDEX]` 是与 INT64 不同的索引整数类型；`Scalar[TASK_ID]` 是 `pl.submit` 产出的不透明任务句柄，不参与算术。
- `TileView.compact` 是**仅分形空间（Left/Right/Acc）有意义**的有效区打包标记，本质是 N-fractal 行距：`mad` 按 `ceil(validRow/16)*16` 写 L0C，读取方只有看到 compact 才会按同样公式重算行距；`tile.matmul` 推断时打标、`tile.matmul_acc` 继承、`tile.create(compact=True)` 声明、`AccCompactValid` 验证。
- 类型推断分通用工具箱（广播/提升/三值证明）与算子专属 deducer 两层；「FP16×FP16 → FP32 累加」的规则一行可查（matmul.cpp L85-L86）；相等与哈希必须同粒度，`AreExprsEqual` 扩展到哪里哈希就跟到哪里。

## 7. 下一步学习建议

- **下一讲（u4-l5）**：`Function` 与 `Program`——类型如何组成函数签名（参数方向 In/Out/InOut 与返回类型），`Submit` 的 `args_` 与被调函数 `params_` 的有界覆盖关系。
- **向后衔接（u4-l8）**：本讲埋的「相等与哈希同粒度」伏笔会在序列化与结构化比较一讲完整展开（`assert_structural_equal`、`.pto` 往返）。
- **验证器专题（u5-l1）**：`AccCompactValid` 只是 `IRProperty`/`PropertyVerifier` 注册表中的一员，去 `docs/en/dev/passes/99-verifier.md` 的表格里查它由哪些 Pass 产出与失效。
- **建议继续阅读的源码**：[include/pypto/ir/tile_view_semantics.h](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/tile_view_semantics.h)（隐式视图/有效视图的解析规则，本讲多处引用的 `GetEffectiveTileView` 在此）与 [src/ir/op/tile_ops/memory.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/op/tile_ops/memory.cpp)（`tile.create`/`tile.load` 的完整 deducer，看 `transpose`/`flat_layout`/`compact` 三个 kwarg 如何各管一种布局）。
