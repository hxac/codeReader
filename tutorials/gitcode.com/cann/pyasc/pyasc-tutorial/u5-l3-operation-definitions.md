# Operation 定义与 API 接口约定

## 1. 本讲目标

本讲是第 5 单元第三讲。上一讲（u5-l2）精读了 ASC Dialect 四大件中的 **Type 与 Attribute**；本讲下钻第三件——**Operation（操作定义）**，并补齐与它共生的一件：**Op Interface（操作接口）**。

ASC Dialect 里有数百条镜像 Ascend C API 的 Operation。如果每条都从零手写 `def`，将是数百段高度相似的重复劳动。pyasc 的解法是**模板族 + 统一接口 + 参数顺序约定**三件套：

- **模板族**：Base.td 预置一组 C++ 模板式的 td 类与 multiclass，一条 `defm` 展开成同族 API 的 L0/L1/L2/L3 变体；
- **API Interface**：所有 Operation 挂上统一接口（`getAPIName`/`getDst`/`getSrc0`...），让**一个** C++ 发射模板服务**几十个** Op；
- **参数顺序约定**：把 Ascend C「运行时参数 × 模板参数 × 必选 × 可选」四象限的签名，按固定规则重排成 IR 的 `operands + attr-dict` 与 Python 的形参表。

学完后你应该能够：

1. 看懂 `defm Add : BinaryTemplateL0123Op<"add", "Add", "operator+">` 这一行如何展开成 `AddL0Op/AddL1Op/AddL2Op/AddL3Op` 四个 C++ 类、四个 `ascendc.add_l0/add_l1/add_l2/add_l3` 助记符与四个 `create_asc_AddL*Op` pybind 方法，并能对一个没见过的 `defm` 独立完成同样的展开；
2. 讲清 `APIOpInterface` 的声明（顶层 `Interfaces.td`）、默认实现（`Base.td` 的 `extraClassDeclarationBase`）、生成物（`AscendCOpInterfaces.cpp.inc`）与消费方（发射层 `VecBinary.h`）各自在哪，解释「统一接口对代码发射的意义」；
3. 拿到一条 Ascend C API 签名（如 `template <typename T, bool isSetMask = true> void Add(...)`），按四步法**反推出**对应 IR Op 的参数排列，并逐参数对照真实 mlir 实例验证；
4. 说出「可推导的类型模板参数不进 IR、不可推导的模板参数变成 UnitAttr 放在最后」这条规则，以及它在发射层如何被逆向还原成 `<float, true>`。

## 2. 前置知识

阅读本讲前，请确认以下概念（前几讲已建立，这里只做一句话回顾）：

- **TableGen 与 .td 文件**（u1-l3、u5-l1）：声明式描述 + 后端生成 C++。`class` 是模板（可被继承、带参数），`def` 是一条具体记录，`multiclass` + ``defm`` 是「一次定义、批量展开」的机制。一个 td 文件生成什么，由 CMakeLists 的 `mlir_tablegen`/`tablegen` 规则决定。
- **Operation 的组成**（u5-l1）：`arguments`（操作数+属性）、`results`、`assemblyFormat`（文本打印格式）、`traits`。打印形如 `ascendc.add_l2 %dst, %src0, %src1, %calCount : 类型...`，末尾可跟 `{isSetMask}` 这样的 attr-dict。
- **「四名合一」反查法**（u5-l1）：IR 助记符 `ascendc.类名.成员函数` ↔ td 记录 `AscendC_类名成员函数Op` ↔ C++ 类 ↔ Python 的 `create_asc_*`。注意 dump 文件里的方言前缀是 `ascendc.` 而非 `asc.`。
- **L0/L1/L2/L3 API 分级**（u2-l5）：同一种计算在 Ascend C 里有四种形态——L0 mask 连续模式、L1 mask 逐 bit 模式（mask 为数组）、L2 前 n 个数据计算（calCount）、L3 整 tensor 运算符重载。L2 最常用。
- **Type 与 Attribute**（u5-l2、u2-l4）：`!ascendc.local_tensor<1024xf32>` 是 Type；`UnitAttr`、`TPositionAttr` 等是编译期常量，挂在 attr-dict 上。
- **asc.add 三段式**（u2-l5）：Python 侧「overload 声明 + op_impl 统一委托 + builder 创建 IR」。

一个贯穿全讲的直觉：**Ascend C 的函数签名是「运行时参数与模板参数混排」的，而 MLIR Operation 与 Python 函数签名各自有一套更严格的语法约束。参数顺序约定就是两个世界之间的「海关申报单」——按固定规则申报，前端按单装箱（operands + attr-dict），发射层按单拆箱（函数实参 + 模板实参）。**

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
|------|------|----------|
| `include/ascir/Dialect/Asc/IR/Base.td` | Operation 模板族总装线：`AscendC_Op` → `APIOp` → `VectorOp` → `BinaryOp` → `BinaryL0/L1/L2/L3Op`，以及各 multiclass | 4.1 主战场、4.2 的 getAPIName 默认实现、4.3 的参数列表 |
| `include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td` | 全部向量双目算子的一行式登记表（`defm Add : ...`） | 4.1 的入口样本 |
| `include/ascir/Dialect/Asc/IR/Basic/OpDataCopy.td` | data_copy 家族：同一 apiName 的多种参数形态变体 | 4.3 的第二个样本 |
| `include/ascir/Dialect/Asc/IR/Interfaces.td` | **Op Interface** 声明：方法池、`APIOpInterface`、`DataCopyOpInterface` 等 | 4.2 主战场 |
| `include/ascir/Dialect/Asc/IR/Core/Interfaces.td` | **Type Interface** 声明：`BaseTensorType` 等 | 4.2 澄清两个 Interfaces.td 的分工 |
| `lib/Dialect/Asc/IR/OpInterfaces.cpp` | 仅 4 行：include 生成的 `AscendCOpInterfaces.cpp.inc` | 4.2 的生成物落点 |
| `include/ascir/Dialect/Asc/IR/CMakeLists.txt` | TableGen 生成规则：op 定义、接口定义、pybind、发射声明四条管线 | 4.2 的「传送带」 |
| `include/ascir/Target/Asc/Basic/VecBinary.h` | 双目算子发射模板（消费接口方法的典型样本） | 4.2、4.3 的发射侧验证 |
| `include/ascir/Target/Asc/Common.h` | 发射辅助：`LogicalResultForT`、`printIsSetMaskTemplate` | 4.2、4.3 的模板实参还原 |
| `python/asc/language/basic/vec_binary.py` | Python 侧 `asc.add` 等接口 | 4.3 的 Python 形参表 |
| `python/asc/language/basic/utils.py` | `op_impl`：L0/L1/L2 三个 builder 的重载分发 | 4.3 的装箱现场 |
| `docs/python-api/language/generated/asc.language.basic.add.md` | `asc.add` 的官方文档：三个 Python 签名 + 三条 Ascend C 原型 | 4.3 反推练习的「考题」 |
| `test/Target/AscendC/basic/vec_binary.mlir` | lit 测试：输入 mlir + CHECK 期望的 Ascend C 输出 | 4.1、4.3 的真值对照 |
| `test/Target/AscendC/basic/vec_vconv.mlir` | 含 `{isSetMask}` attr-dict 打印的实例 | 4.3 的 UnitAttr 证据 |
| `docs/developer_guide.md`、`docs/architecture_introduction.md` | 参数顺序约定与 paramTypeLists 语义的权威出处 | 4.3 的规则原文 |

## 4. 核心概念与源码讲解

### 4.1 Op 模板族：一条 defm 展开成 L0/L1/L2/L3

#### 4.1.1 概念说明

ASC Dialect 要镜像的 Ascend C API 数以千计，而其中大量 API 的「形状」高度相似。以向量双目算子为例：`Add/Sub/Mul/Div/Max/Min/And/Or/...` 十几个算子，每个都有 L0/L1/L2/L3 四种形态，参数列表几乎完全一样——只有 API 名不同。

如果逐条手写 `def AscendC_AddL0Op : ...`、`def AscendC_AddL1Op : ...`，要写 \( 17 \times 4 = 68 ) 条几乎相同的记录（仅 OpVecBinary.td 一个文件），而且后续任何参数调整都要霰弹式修改。TableGen 的 `class`（模板）+ `multiclass`（批量展开）正好治这个病：

- **class 继承链**把「公共部分」（方言、命名空间、assemblyFormat、接口、参数列表骨架）逐层沉淀；
- **multiclass** 把「L0/L1/L2/L3 四变体」的差异（mask 的类型、calCount 的有无）封装成一次展开；
- **一行 defm** 只负责登记「这个算子叫什么」：助记符 `add`、API 名 `Add`、L3 运算符 `operator+`。

于是 OpVecBinary.td 全文只有 41 行，却定义了 17 个算子、约 50 个 Operation。

#### 4.1.2 核心流程

以 `defm Add : BinaryTemplateL0123Op<"add", "Add", "operator+">;` 为例，展开链是：

```text
defm Add : BinaryTemplateL0123Op<"add","Add","operator+">
  ├── defm "" : BinaryTemplateL012Op<"add","Add">
  │     ├── def L0Op : BinaryTemplateL0Op<"add_l0","Add">      → C++ 类 AddL0Op
  │     ├── def L1Op : BinaryTemplateL1Op<"add_l1","Add">      → C++ 类 AddL1Op
  │     └── def L2Op : BinaryTemplateL2Op<"add_l2","Add">      → C++ 类 AddL2Op
  └── def L3Op : BinaryL3Op<"add_l3","operator+">              → C++ 类 AddL3Op
```

四条生成管线（在 IR 目录的 CMakeLists 里登记）分别消费这些记录：

1. `-gen-op-decls/-gen-op-defs` → `AscendCOps.h.inc/.cpp.inc`：C++ 类 `AddL0Op`...，含各参数访问器；
2. `-gen-op-interface-defs` → `AscendCOpInterfaces.cpp.inc`：接口方法实现（见 4.2）；
3. `-gen-pybind-defs` → `AscOpBindings.h.inc`：`create_asc_AddL0Op` 等 pybind 方法（详见 u5-l4）；
4. `-gen-opemit-decls/-gen-opemit-defs` → `AscendCOpEmit.h.inc/.cpp.inc`：自动发射函数声明/定义（详见 u5-l4、u6-l5）。

之后 Python 侧 `builder.create_asc_AddL2Op(...)` 创建的 Operation，dump 出来就是 `ascendc.add_l2 %dst, %src0, %src1, %calCount : ...`。

#### 4.1.3 源码精读

**（1）一行式登记表。** 整个 OpVecBinary.td 的「正文」只有一段按字母序排列的 defm/def：

[include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td:23-39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td#L23-L39)

```tablegen
defm Add : BinaryTemplateL0123Op<"add", "Add", "operator+">;
defm AddDeqRelu : BinaryCastL012Op<"add_deq_relu", "AddDeqRelu">;
defm AddRelu : BinaryTemplateL012Op<"add_relu", "AddRelu">;
...
defm Sub : BinaryTemplateL0123Op<"sub", "Sub", "operator-">;
```

这一行说明：只写三个字符串——基础助记符 `add`（展开时自动拼上 `_l0/_l1/_l2/_l3` 后缀）、Ascend C API 名 `Add`（进 `getAPIName()`，发射时用）、L3 的运算符名 `operator+`（L3 形态的「API 名」）。

**（2）四变体的参数差异。** 模板族的实体在 Base.td。先看继承链底座：

[include/ascir/Dialect/Asc/IR/Base.td:23-26](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L23-L26)

```tablegen
class AscendC_Op<string mnemonic, list<Trait> traits = []>
    : Op<AscendC_Dialect, mnemonic, traits> {
  let cppNamespace = "::mlir::ascendc";
  let assemblyFormat = "operands attr-dict `:` qualified(type(operands))";
```

`AscendC_Op` 统一了命名空间与「操作数 + attr-dict + 类型」的打印格式——这正是后面 mlir 文本里每行末尾那一长串 `: !ascendc.local_tensor<...>, i32` 的来源。其上的 `genEmitter` 开关与 `paramTypeLists` 标记在 4.3 再讲。

再看三个 Template 变体的 `arguments`（普通 BinaryL0/L1/L2 与之只差一个 `UnitAttr:$isSetMask`，可对照阅读）：

[include/ascir/Dialect/Asc/IR/Base.td:150-171](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L150-L171)

```tablegen
class BinaryTemplateL0Op<...> {
  let arguments = (ins AnyType:$dst, AnyType:$src0, AnyType:$src1,
                   AnyType:$mask, AnyType:$repeatTimes, 
                  AscendC_BinaryRepeatParams:$repeatParams, UnitAttr:$isSetMask);
}
class BinaryTemplateL1Op<...> {
  let arguments = (ins AnyType:$dst, AnyType:$src0, AnyType:$src1,
                   Variadic<UI64>:$mask, AnyType:$repeatTimes, 
                  AscendC_BinaryRepeatParams:$repeatParams, UnitAttr:$isSetMask);
}
class BinaryTemplateL2Op<...> {
  let arguments = (ins AnyType:$dst, AnyType:$src0, AnyType:$src1,
                   AnyType:$calCount, UnitAttr:$isSetMask);
}
```

三者的差异恰好对应 4.3 要讲的 Ascend C 三条原型：

- **L0**：`mask` 是单个整数（AnyType，实际由前端物化为 int64）；
- **L1**：`mask` 是 `uint64_t mask[]` 数组——IR 里表达为 `Variadic<UI64>`（可变个数的 ui64 操作数）；
- **L2**：没有 mask/repeatTimes/repeatParams，只有 `calCount`（前 n 个数据计算）；
- 三者末尾都有 `UnitAttr:$isSetMask`——Ascend C 的模板可选参数 `bool isSetMask = true` 在 IR 中的形态（详见 4.3）。

**（3）L3 与 multiclass 展开。** L3 形态比较特殊——它是「整 tensor 运算符重载」，参数只剩三个 tensor，且 `apiName` 一栏填的不是 API 名而是运算符：

[include/ascir/Dialect/Asc/IR/Base.td:196-201](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L196-L201)

```tablegen
class BinaryL3Op<string mnemonic, string apiName, list<Trait> traits = []>
    : BinaryOp<mnemonic, apiName, [BinaryL3OpInterface] # traits> {
  let summary = "Call `LocalTensor::" # apiName # "` method";
  let arguments = (ins AnyType:$dst, AnyType:$src0, AnyType:$src1);
}
```

两个 multiclass 完成批量装配——`BinaryTemplateL0123Op` = `BinaryTemplateL012Op`（三个 Template 变体）再补一个 L3：

[include/ascir/Dialect/Asc/IR/Base.td:209-231](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L209-L231)

```tablegen
multiclass BinaryTemplateL012Op<string baseMnemonic, string apiName, list<Trait> traits = []> {
  def L0Op : BinaryTemplateL0Op<baseMnemonic # "_l0", apiName, traits>;
  def L1Op : BinaryTemplateL1Op<baseMnemonic # "_l1", apiName, traits>;
  def L2Op : BinaryTemplateL2Op<baseMnemonic # "_l2", apiName, traits>;
}
multiclass BinaryTemplateL0123Op<string baseMnemonic, string apiName, string l3operator,
                        list<Trait> traits = []> {
  defm "" : BinaryTemplateL012Op<baseMnemonic, apiName, traits>;
  def L3Op : BinaryL3Op<baseMnemonic # "_l3", l3operator, traits>;
}
```

注意 `baseMnemonic # "_l0"` 的字符串拼接：`"add"` → `"add_l0"`。所以最终助记符是 `ascendc.add_l0`，td 记录名是 `Add` + `L0Op` 后缀（`AddL0Op`），C++ 类同名。同目录还有 `BinaryL0123Op`（无 isSetMask 的版本，供 `And` 等走 `BinaryL012Op` 的算子用）与 `BinaryCastL012Op`（双类型模板参数版本，见 4.2 的 `printIsSetMaskCastTemplate`）。

**（4）真实 mlir 实例。** lit 测试给出了四变体的真实文本（这正是 `PYASC_DUMP_PATH` 导出的 codegen.mlir 里会看到的样子）：

[test/Target/AscendC/basic/vec_binary.mlir:116-117](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L116-L117)

```mlir
func.func @emit_vector_binary_l2_ops(%dst: !ascendc.local_tensor<1024xf32>, ...) {
  ascendc.add_l2 %dst, %src0, %src1, %calCount_i32 : !ascendc.local_tensor<1024xf32>, !ascendc.local_tensor<1024xf32>, !ascendc.local_tensor<1024xf32>, i32
```

L1 的 mask 数组打印成两个独立 ui64 操作数（`Variadic<UI64>` 的效果）：

[test/Target/AscendC/basic/vec_binary.mlir:44](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L44)

```mlir
ascendc.add_relu_cast_l1 %dst, %src0, %src1, %maskArray1_0, %maskArray1_1, %c1_i32, %params : ...
```

L3 只有三个 tensor 操作数：

[test/Target/AscendC/basic/vec_binary.mlir:146](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L146)

```mlir
ascendc.add_l3 %dst, %src0, %src1 : !ascendc.local_tensor<1024xf32>, ...
```

**（5）一个值得注意的事实。** 在 `python/asc` 全目录检索 `create_asc_*L3Op` 没有任何命中——**当前 Python 前端只创建 L0/L1/L2 三种变体**（`vec_binary.py` 的 `op_impl` 也只传三个 builder）；L3 目前由 IR 定义与 lit 测试覆盖、发射层可输出，但没有前端入口直接构造它。这说明「模板族展开四个」与「前端使用三个」是两回事——定义的完备性（对齐 Ascend C 全部形态）优先于前端的即时使用。阅读源码时不要因为「定义了」就推断「一定被前端用到」，反查一下 Python 侧即可确认。

#### 4.1.4 代码实践

**实践目标**：对一个陌生的 `defm` 行，独立完成「一行 → 四个 Op」的展开推演，并用仓库内 lit 测试验证。

**操作步骤**：

1. 打开 [OpVecBinary.td:32-33](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td#L32-L33)，选 `defm Max : BinaryTemplateL012Op<"max", "Max">;`（注意它用的是**不带 isSetMask 的** `BinaryL012Op` 族，不是 Template 版）。
2. 在纸上写出展开结果：四个 td 记录名（`Max` + `L0Op/L1Op/L2Op`）、四个助记符（`ascendc.max_l0/l1/l2`，此处只有 L0-L2）、三组参数列表（对照 [Base.td:127-148](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L127-L148) 的 `BinaryL0/L1/L2Op`，注意**没有** `UnitAttr:$isSetMask`）。
3. 打开 [test/Target/AscendC/basic/vec_binary.mlir:126](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L126) 与 [vec_binary.mlir:35](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L35)，找到 `ascendc.max_l2` 与 `ascendc.max_l0` 两行，逐参数核对你写的参数列表。
4. 再到 [python/asc/language/basic/vec_binary.py:249-252](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L249-L252) 确认 Python 侧 `asc.max` 委托的三个 builder 名。

**需要观察的现象**：`max_l0` 的 mlir 行末尾 attr-dict 位置**没有** `{isSetMask}`（因为 `BinaryL0Op` 无此 UnitAttr），而同文件的 Template 族算子（如 `vec_vconv.mlir` 里的 `cast_deq_l0 ... {isSetMask}`）有——模板族选择直接决定 IR 文本长相。

**预期结果**：展开表与 lit 测试、Python 委托三处完全对上；`max_l0` 形态为 `dst, src0, src1, mask, repeatTimes, repeatParams` 六个操作数、无属性。

本实践为纯源码阅读型，不依赖运行环境，可直接完成；若想运行验证（可选）：设置 `PYASC_DUMP_PATH` 后运行 `examples/01_add/add.py`，在导出的 codegen.mlir 里找一个 `ascendc.add_l2` 实例对照（此路径需要已按 u1-l2 完成安装，**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：`defm Div : BinaryTemplateL0123Op<"div", "Div", "operator/">;` 会生成哪些 C++ 类名与 IR 助记符？L3 的 `getAPIName()` 返回什么？

**答案**：生成 `DivL0Op/DivL1Op/DivL2Op/DivL3Op` 四个类，助记符 `ascendc.div_l0/div_l1/div_l2/div_l3`。L3 的 `getAPIName()` 返回 `"operator/"`（第三模板参数 `l3operator` 被当作 apiName 传入 `BinaryL3Op`），发射成 `v1 = v2.operator/(v3);`——见 [test/Target/AscendC/basic/vec_binary.mlir:140](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L140)。

**练习 2**：为什么 `FusedAbsSub` 用 `def FusedAbsSubL2Op : BinaryL2Op<...>` 单独 def，而不是 `defm`？

**答案**：看 [OpVecBinary.td:28-29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td#L28-L29)：`FusedAbsSub` 只有 L2 一种形态（Ascend C 只提供前 n 个数据计算版本），没有 L0/L1/L3 变体，所以直接继承 `BinaryL2Op` 定义单个 Op。模板族是「按需批量」，形态不全的 API 退回单 def。

**练习 3**：`defm AddDeqRelu : BinaryCastL012Op<...>` 与 `defm AddRelu : BinaryTemplateL012Op<...>` 都有 isSetMask，二者的差别在哪？

**答案**：差别在模板参数个数：`AddDeqRelu` 在 Ascend C 中是 `template <typename T, typename U, bool isSetMask>`（目的与源类型不同，涉及量化精度转换），发射时模板实参有两个类型（见 [test/Target/AscendC/basic/vec_binary.mlir:12](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L12) 的 `AscendC::AddDeqRelu<float, float, 0>`），因此走 `BinaryCast` 族，由 4.2 将读到的 `printIsSetMaskCastTemplate` 从 dst 与 src1 两个 tensor 各提取一个元素类型；`AddRelu` 只有一个类型模板参数，走 `printIsSetMaskTemplate`。IR 参数列表两者相同——差异被推迟到发射层处理。

### 4.2 AscendCOpInterface：统一接口与代码发射

#### 4.2.1 概念说明

MLIR 的 **Op Interface** 是挂在 Operation 上的一组方法契约：实现接口的每个 Op 都必须能回答接口里声明的问题。ASC Dialect 为全部 API Op 挂 `APIOpInterface` 及其子接口，声明的核心方法有两类：

- **元信息**：`getAPIName()`（返回 Ascend C 函数名，如 `"Add"`）、`getComment()`（返回注释）；
- **参数访问器**：`getDst()/getSrc()/getSrc0()/getSrc1()/getMask()/getRepeatTimes()/getCalCount()...`——注意这些方法名与 4.1 里 td `arguments` 的形参名**一一对应**。

它对代码发射的意义可以概括为一句话：**访问器统一了，发射函数就能写成 C++ 模板**。`VecBinary.h` 里一个 L2 发射模板函数服务 20 个 L2 Op（`AddL2Op、SubL2Op、MaxL2Op...`）——只要这些 Op 都能回答 `getDst()/getSrc0()/getSrc1()/getCalCount()`，模板就无需关心「这是哪个算子」；算子名由 `getAPIName()` 统一提供。没有接口，每个 Op 的访问器命名就可能漂移（`getDst` vs `getOutput`），模板复用立刻瓦解。

先澄清一个容易踩的坑——**仓库里有两个 Interfaces.td，职责不同**：

| 文件 | 定义什么 | 代表成员 |
|------|----------|----------|
| `include/ascir/Dialect/Asc/IR/Interfaces.td`（顶层） | **Op Interface**（本讲主角） | `APIOpInterface`、`DataCopyOpInterface`、`BinaryOpInterface` |
| `include/ascir/Dialect/Asc/IR/Core/Interfaces.td` | **Type Interface**（类型的接口，u5-l2 已接触） | `AscendC_BaseTensorTypeInterface`、`AscendC_BaseQueueTypeInterface` |

`getAPIName/getComment` 声明在**顶层** Interfaces.td 的 `APIOpInterface` 里；Core/Interfaces.td 里的 `BaseTensorType` 则被 OpDataCopy.td 拿来约束 `data_copy` 的操作数类型（`AscendC_BaseTensorTypeInterface:$dst`）——两个文件在本讲都会用到，但角色不同。

#### 4.2.2 核心流程

接口从声明到被消费的完整链路：

```text
Interfaces.td 声明（OpInterface + InterfaceMethod 池）
   │  mlir_tablegen -gen-op-interface-decls/-defs   （IR/CMakeLists.txt）
   ▼
AscendCOpInterfaces.h.inc / .cpp.inc（生成的 C++ 抽象基类与 trait 实现）
   │  #include（OpInterfaces.cpp 全部内容）
   ▼
C++ 侧可用 ascendc::APIOpInterface 多态访问任意 API Op
   ▲
   │  Base.td 的 APIOp 用 extraClassDeclarationBase 提供
   │  static StringRef getAPIName() { return "Add"; } 等默认实现
   ▼
发射层模板（VecBinary.h 等）调用 op.getAPIName() / op.getDst() ... 输出 Ascend C
```

两条细节值得强调：

1. **接口方法分两层实现**。`APIOpInterface` 声明了 `getAPIName/getComment`，而**返回什么值**由 `Base.td` 的 `APIOp` 通过 `extraClassDeclaration`（追加到生成类里的手写 C++ 片段）给每个 Op 生成一份静态实现——把 td 模板参数 `apiName` 字符串直接织进 C++ 代码。参数访问器（`getDst` 等）则不需要手写：TableGen 的 `-gen-op-defs` 已为 `arguments` 里每个形参生成同名访问器，接口声明与生成器产出天然对齐（前提是形参名守规矩，这正是方法池 `AscendC_InterfaceMethods` 注释「Method list should be kept sorted by name」维持的纪律）。
2. **接口可以带用 C++ 写的方法体**。`DataCopyOpInterface` 的 `getDirection` 直接在 td 里写了一段 C++：按 dst/src 的类型组合返回 `gm_ubuf/ubuf_gm/gm_gm/ubuf_ubuf`——一个「由类型推导语义」的活例子，InsertSync 等 Pass 判断数据搬运方向时就依赖这类接口方法。

#### 4.2.3 源码精读

**（1）方法池与接口基座。** 顶层 Interfaces.td 先用一个「方法池」类集中定义全部可复用的 InterfaceMethod（`GetMethod` 帮你少写样板）：

[include/ascir/Dialect/Asc/IR/Interfaces.td:18-25](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Interfaces.td#L18-L25)

```tablegen
class GetMethod<string fieldName, string methodName, string returnType = "::mlir::Value">
    : InterfaceMethod<"Obtain `" # fieldName # "` parameter", returnType, methodName>;

class AscendC_InterfaceMethods {
  // Method list should be kept sorted by name.
  InterfaceMethod getAPIName = InterfaceMethod<"Obtain Ascend C library name", "::llvm::StringRef", "getAPIName">;
  InterfaceMethod getCalCount = GetMethod<"calCount", "getCalCount">;
  InterfaceMethod getComment = InterfaceMethod<"Obtain describing comment", "::llvm::StringRef", "getComment">;
  InterfaceMethod getDst = GetMethod<"dst", "getDst">;
```

`AscendC_OpInterface` 继承 MLIR 的 `OpInterface` 并引入方法池，所有具体接口再从它派生：

[include/ascir/Dialect/Asc/IR/Interfaces.td:54-66](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Interfaces.td#L54-L66)

```tablegen
class AscendC_OpInterface<string name, list<Interface> baseInterfaces = []>
    : OpInterface<name, baseInterfaces>, AscendC_InterfaceMethods {
  let cppNamespace = "::mlir::ascendc";
}

def APIOpInterface : AscendC_OpInterface<"APIOp"> {
  let description = "Base interface for operations representing Ascend C API";
  let methods = [getAPIName, getComment];
}
```

**（2）接口继承树与 Op 模板族的对应。** 接口也构成一棵树：`VectorOpInterface` 继承 `APIOpInterface`；`BinaryOpInterface` 再继承 `VectorOpInterface + OpWithDstInterface` 并声明 `getSrc0/getSrc1`：

[include/ascir/Dialect/Asc/IR/Interfaces.td:73-75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Interfaces.td#L73-L75)、[Interfaces.td:129-137](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Interfaces.td#L129-L137)

```tablegen
def VectorOpInterface : AscendC_OpInterface<"VectorOp", [APIOpInterface]> { ... }

def BinaryOpInterface
    : AscendC_OpInterface<"BinaryOp", [VectorOpInterface, OpWithDstInterface]> {
  let methods = [getSrc0, getSrc1];
}
```

回看 Base.td 的继承链 `APIOp`（挂 `APIOpInterface`）→ `VectorOp`（挂 `VectorOpInterface`）→ `BinaryOp`（挂 `BinaryOpInterface`）——**Op 模板族的每一层与接口树的每一层平行推进**，这是「结构即契约」的直观体现。

**（3）带 C++ 方法体的接口。** `DataCopyOpInterface` 展示了接口方法的另一种写法——直接在 td 里给默认实现：

[include/ascir/Dialect/Asc/IR/Interfaces.td:77-103](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Interfaces.td#L77-L103)

```tablegen
def DataCopyOpInterface :
    AscendC_OpInterface<"DataCopyOp", [APIOpInterface, OpWithDstInterface]> {
  InterfaceMethod getDirection = InterfaceMethod<"Get copy direction",
    "::mlir::ascendc::CopyDirection", "getDirection", (ins), "", [{
    auto dstType = $_op.getDst().getType();
    auto srcType = $_op.getSrc().getType();
    if (isa<GlobalTensorType>(srcType)) {
      if (isa<GlobalTensorType>(dstType)) return CopyDirection::gm_gm;
      if (isa<LocalTensorType>(dstType)) return CopyDirection::gm_ubuf;
    } else if (isa<LocalTensorType>(srcType)) { ... }
    return CopyDirection::Unknown;
  }]>;
```

这正是 u2-l5 说过的「data_copy 方向由 dst/src 张量类型组合决定」在源码里的落点。

**（4）Type Interface 那一边。** Core/Interfaces.td 只有三个类型接口，供 td 的参数类型约束使用：

[include/ascir/Dialect/Asc/IR/Core/Interfaces.td:17-29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Interfaces.td#L17-L29)

```tablegen
class AscendC_TypeInterface<string name, list<Interface> baseInterfaces = []>
    : TypeInterface<name, baseInterfaces> {
  let cppNamespace = "::mlir::ascendc";
}

def AscendC_BaseTensorTypeInterface
    : AscendC_TypeInterface<"BaseTensorType", [ShapedTypeInterface]> {
  let summary = "base tensor type";
}
```

`OpDataCopy.td` 里所有 data_copy 变体的 `$dst/$src` 都用它约束（见 4.3），比裸 `AnyType` 更严格——只有实现了该接口的类型（LocalTensor/GlobalTensor）能当操作数。

**（5）生成与落点。** CMakeLists 登记了接口的生成规则：

[include/ascir/Dialect/Asc/IR/CMakeLists.txt:17-19](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/CMakeLists.txt#L17-L19)

```cmake
set(LLVM_TARGET_DEFINITIONS Interfaces.td)
mlir_tablegen(AscendCOpInterfaces.h.inc -gen-op-interface-decls)
mlir_tablegen(AscendCOpInterfaces.cpp.inc -gen-op-interface-defs)
```

而 `lib/Dialect/Asc/IR/OpInterfaces.cpp` 的全部内容就是把生成的 `.inc` 编进库：

[lib/Dialect/Asc/IR/OpInterfaces.cpp:11-14](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/OpInterfaces.cpp#L11-L14)

```cpp
#include "ascir/Dialect/Asc/IR/Asc.h"

#include "ascir/Dialect/Asc/IR/AscendCOpInterfaces.cpp.inc"
```

**（6）发射层如何消费接口。** `VecBinary.h` 是接口价值的最佳注脚——一个参数打印模板 + 一个按 Op 类型分派的 `printOperation` 重载：

[include/ascir/Target/Asc/Basic/VecBinary.h:41-47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Basic/VecBinary.h#L41-L47)

```cpp
template <typename BinaryOp>
auto printBinaryL2Params(CodeEmitter& emitter, BinaryOp op)
{
    auto& os = emitter.ostream();
    os << "(" << emitter.getOrCreateName(op.getDst()) << ", " << emitter.getOrCreateName(op.getSrc0()) << ", "
       << emitter.getOrCreateName(op.getSrc1()) << ", " << emitter.getOrCreateName(op.getCalCount()) << ")";
}
```

L2 主发射函数的返回类型用 `LogicalResultForT`（SFINAE 技巧，[Common.h:57-58](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Common.h#L57-L58)：仅当模板参数是名单内类型时该重载才存在）把**允许服务的 Op 白名单**写进签名：

[include/ascir/Target/Asc/Basic/VecBinary.h:68-80](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Basic/VecBinary.h#L68-L80)

```cpp
template <typename BinaryL2Op>
auto printOperation(CodeEmitter& emitter, BinaryL2Op op) -> LogicalResultForT<
    BinaryL2Op, ascendc::AddL2Op, ascendc::AddDeqReluL2Op, ascendc::AddReluL2Op, /* ...共 19 个 */>
{
    auto& os = emitter.ostream();
    os << ascNamespace << "::" << op.getAPIName();
    printBinaryL2Params(emitter, op);
    return success();
}
```

数一数名单：L2 这一个模板函数服务 20 个 Op，全部输出 `AscendC::<getAPIName()>(四个参数)`——`op.getAPIName()` 来自 `APIOpInterface`，`op.getDst()` 等来自接口树与 TableGen 生成的访问器。**一条接口纪律换来 20 倍复用**，这就是「统一接口对代码发射的意义」的定量表述。

#### 4.2.4 代码实践

**实践目标**：沿「td 声明 → 生成 → 消费」把 `getAPIName` 追踪到发射输出，量化一个发射模板的复用倍数。

**操作步骤**：

1. 在 [Interfaces.td:63-66](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Interfaces.td#L63-L66) 找到 `APIOpInterface` 声明的方法；再到 [Base.td:64-73](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L64-L73) 看 `APIOp` 如何把 `apiName` 织进 `extraClassDeclarationBase` 生成的静态方法。
2. 打开 [VecBinary.h:68-80](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Basic/VecBinary.h#L68-L80)，统计 `LogicalResultForT` 名单里 L2 Op 的个数（逐个数：Add、AddDeqRelu、AddRelu、AddReluCast、And、Div、FusedAbsSub、FusedExpSub、FusedMulAdd、FusedMulAddRelu、Max、Min、Mul、MulAddDst、MulCast、Or、Prelu、Sub、SubRelu、SubReluCast——注意区分名单与 OpVecBinary.td 里的 defm 行，名单还含其他文件定义的算子）。
3. 对照 [test/Target/AscendC/basic/vec_binary.mlir:95-114](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L95-L114) 的 CHECK 块：18 行输出全部形如 `AscendC::Xxx(v1, v2, v3, v4);`，验证「同一模板、不同 API 名」。
4. 若本地已构建 devtools（u7-l5 会讲 `PYASC_SETUP_DEVTOOLS=1`），可执行 `ascir-translate -mlir-to-ascendc test/Target/AscendC/basic/vec_binary.mlir` 观察实际输出；否则以 lit 测试的 CHECK 行为「已验证的期望输出」，**运行路径待本地验证**。

**需要观察的现象**：CHECK 块中每个算子的输出只有函数名不同、参数布局完全一致；`ascendc.add_l2` 的四个操作数按 `getDst(), getSrc0(), getSrc1(), getCalCount()` 的固定顺序落进 `AscendC::Add(...)` 的实参表。

**预期结果**：L2 发射模板名单约 20 个 Op（含 `AddReluCastL2Op、SubReluCastL2Op` 等定义在其他 td 文件的算子）；接口方法名与 td 形参名、发射模板调用三处完全一致。

#### 4.2.5 小练习与答案

**练习 1**：`APIOpInterface` 声明了 `getAPIName`，但 Interfaces.td 里找不到任何「返回 "Add"」的字样——这个返回值是哪里来的？

**答案**：来自 [Base.td:68-72](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L68-L72) `APIOp` 的 `extraClassDeclarationBase`：它把 td 模板参数 `apiName` 拼接进一段 C++ 代码（`static StringRef getAPIName() { return "Add"; }`），经 `extraClassDeclaration` 追加到每个生成的 Op 类里。接口负责「声明契约」，Op 类负责「给出答案」。

**练习 2**：为什么 `getDirection` 要写在 `DataCopyOpInterface` 的方法体里，而不是某个 Pass 里？

**答案**：搬运方向的推导规则（dst/src 的 Global/Local 组合 → 四种方向）是 data_copy 家族的**固有语义**，不是某个 Pass 的私有逻辑。放进接口后，任何持有 `DataCopyOpInterface` 的代码（InsertSync 等多个 Pass、发射层）都能直接调 `getDirection()`，规则只维护一处；且 td 里这段 C++ 与 Op 定义同文件版本管理，改签名时不会漏改。

**练习 3**：如果把 `BinaryL2Op` 的形参 `calCount` 改名为 `count`，哪些地方会连锁受影响？

**答案**：至少四处：(1) `BinaryL2OpInterface` 的 `getCalCount` 方法在实现该接口的 Op 上找不到同名访问器，编译失败（方法池 [Interfaces.td:24](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Interfaces.td#L24) 与形参名的对齐被破坏）；(2) 发射模板 [VecBinary.h:46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Basic/VecBinary.h#L46) 的 `op.getCalCount()` 编译失败；(3) Python 侧 `op_impl` 调 `build_l2` 时 pybind 生成的关键字参数名变化；(4) lit 测试与文档中的参数名说明需同步。这也是接口注释要求「方法按名排序、名字守纪律」的原因。

### 4.3 参数顺序约定：从 Ascend C 签名反推 IR 参数

#### 4.3.1 概念说明

Ascend C 的 API 签名把两类参数混排：

```cpp
template <typename T, bool isSetMask = true>
__aicore__ inline void Add(const LocalTensor<T>& dst, const LocalTensor<T>& src0,
                           const LocalTensor<T>& src1, uint64_t mask,
                           const uint8_t repeatTimes, const BinaryRepeatParams& repeatParams);
```

- **运行时参数**（dst/src0/src1/mask/repeatTimes/repeatParams）：C++ 函数实参，kernel 运行时传值；
- **模板参数**（`typename T`、`bool isSetMask = true`）：编译期常量，写成 `<...>`。

而两个「下游世界」各有更强的语法约束：Python 没有模板，且要求可选参数必须排在必选参数之后；MLIR Operation 把「值」放 operands、「编译期常量」放 attr-dict。于是 pyasc 定下**四块顺序约定**（[architecture_introduction.md:166](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/architecture_introduction.md#L166)、[developer_guide.md:923](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L923)）：

> **运行时必选参数 → 模板必选参数 → 运行时可选参数 → 模板可选参数**

外加两条配套规则（[architecture_introduction.md:235-237](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/architecture_introduction.md#L235-L237)、[developer_guide.md:918-922](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L918-L922)）：

- **可推导规则**：能从其他参数推导的类型模板参数（如 `typename T` 可从 `LocalTensor<T>` 的类型读出）**不进** IR 参数表；发射层再从 tensor 类型里把它提取出来拼回 `<float>`。
- **模板参数运行时化**：不可推导的模板参数（如 `bool isSetMask`）改为「常量形态的 IR 参数」——bool/枚举直接用，非常量类型用 `ConstExpr` 包装；落在 IR 里通常是 `UnitAttr`（属性，paramTypeLists 编码 -1），排在**操作数之后**（即整个参数表的最后）。

#### 4.3.2 核心流程

从 Ascend C 签名反推 IR Op 参数的四步算法：

```text
输入：一条 Ascend C API 签名
第 1 步  分类：把每个参数标为（运行时/模板）×（必选/可选）
第 2 步  删除：去掉「可从 tensor 参数推导的类型模板参数」（typename T 等）
第 3 步  重排：按「运行时必选 → 模板必选 → 运行时可选 → 模板可选」四块排序
第 4 步  落地：非常量参数 → operands（按序）；模板可选参数 → UnitAttr（attr-dict）
输出：td 的 arguments 列表 =（operands..., 可选 UnitAttr...）
```

以 `Add` 的 L0 原型跑一遍：

| Ascend C 参数 | 分类 | 处置 |
|---|---|---|
| `typename T` | 模板·必选·可推导 | **删除**（发射时从 dst 类型提取） |
| `dst/src0/src1/mask/repeatTimes/repeatParams` | 运行时·必选 | → operands（前六个） |
| `bool isSetMask = true` | 模板·可选·不可推导 | → `UnitAttr:$isSetMask`（attr-dict，最后） |

结果应与 [Base.td:150-156](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L150-L156) 的 `BinaryTemplateL0Op` 逐一吻合。发射层的逆向还原则在 [Common.h:98-107](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Common.h#L98-L107)：从 `op.getDst().getType()` 提取元素类型、读 `op.getIsSetMask()`，拼出 `AscendC::Add<float, 1>(...)`——「海关」一来一回，两侧签名都不会错位。

#### 4.3.3 源码精读

**（1）考题：asc.add 的三条 Ascend C 原型。** 官方文档列出了三种形态（L2/L1/L0 各一）：

[docs/python-api/language/generated/asc.language.basic.add.md:13-31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python-api/language/generated/asc.language.basic.add.md#L13-L31)

```cpp
template <typename T>
__aicore__ inline void Add(const LocalTensor<T>& dst, const LocalTensor<T>& src0,
                           const LocalTensor<T>& src1, const int32_t& count);        // L2

template <typename T, bool isSetMask = true>
__aicore__ inline void Add(const LocalTensor<T>& dst, ..., uint64_t mask[],
                           const uint8_t repeatTimes, const BinaryRepeatParams& repeatParams);  // L1

template <typename T, bool isSetMask = true>
__aicore__ inline void Add(const LocalTensor<T>& dst, ..., uint64_t mask,
                           const uint8_t repeatTimes, const BinaryRepeatParams& repeatParams);  // L0
```

**（2）答卷一：IR 侧。** 用四步法推导 L2：`T` 可推导删掉；`dst/src0/src1/count` 运行时必选成 operands；无模板可选。对照 [Base.td:166-171](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L166-L171)：

```tablegen
class BinaryTemplateL2Op<...> {
  let arguments = (ins AnyType:$dst, AnyType:$src0, AnyType:$src1,
                   AnyType:$calCount, UnitAttr:$isSetMask);
}
```

——`calCount` 对应原型里的 `count`（名字遵循 Ascend C 命名的小驼峰变体，[developer_guide.md:575](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L575) 建议「参数名建议和 Ascend C API 保持一致」）。`isSetMask` 在 td 里保留是因为 pyasc 的 L2 Python 接口也暴露了 `is_set_mask`（见下），它对应 Ascend C 中带 isSetMask 模板的 L2 变体。

**（3）答卷二：Python 侧。** Python 形参表同样按四块顺序组装（注意三个 overload 的形参排布）：

[python/asc/language/basic/vec_binary.py:21-43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L21-L43)

```python
@overload
def add(dst, src0, src1, count: int, is_set_mask: bool = True) -> None: ...
@overload
def add(dst, src0, src1, mask: int, repeat_times: int,
        repeat_params: BinaryRepeatParams, is_set_mask: bool = True) -> None: ...

@require_jit
def add(dst, src0, src1, *args, **kwargs) -> None:
    builder = global_builder.get_ir_builder()
    op_impl("add", dst, src0, src1, args, kwargs, builder.create_asc_AddL0Op, builder.create_asc_AddL1Op,
            builder.create_asc_AddL2Op)
```

`is_set_mask: bool = True` 排在**最后**且带默认值——「模板可选参数殿后」的直接体现；`typename T` 不见踪影——它藏在 `LocalTensor` 的 dtype 成员里（u2-l1 的 DataType）。

**（4）装箱现场：op_impl 的重载分发。** `op_impl` 按「剩余参数的形态」选择 L0/L1/L2 三个 builder 之一，并把 Python 值物化成 IR 值：

[python/asc/language/basic/utils.py:117-133](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/utils.py#L117-L133)

```python
@dispatcher.register(mask=RuntimeInt, repeat_times=RuntimeInt, repeat_params=BinaryRepeatParams,
                     is_set_mask=DefaultValued(bool, True))
def _(mask, repeat_times, repeat_params, is_set_mask=True):
    build_l0(dst.to_ir(), src0.to_ir(), src1.to_ir(),
             _mat(mask, KT.int64).to_ir(),
             _mat(repeat_times, KT.int8).to_ir(), repeat_params.to_ir(), is_set_mask)

@dispatcher.register(count=RuntimeInt, is_set_mask=DefaultValued(bool, True))
def _(count, is_set_mask=True):
    build_l2(dst.to_ir(), src0.to_ir(), src1.to_ir(), _mat(count, KT.int32).to_ir())
```

对照 Ascend C 原型可见物化类型即 C++ 形参类型：`mask → int64`（uint64 的有符号容纳）、`repeat_times → int8`（uint8_t）、`count → int32`（int32_t）。`is_set_mask` 作为**末位实参**传给 builder，最终落成 UnitAttr。

**（5）IR 文本与还原证据。** 带 `{isSetMask}` 的 attr-dict 打印（模板可选参数的真实形态）：

[test/Target/AscendC/basic/vec_vconv.mlir:91](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_vconv.mlir#L91)

```mlir
ascendc.cast_deq_l0 %dst, %src, %maskArray1_0, %repeatTime, %params {isSetMask} : ...
```

（UnitAttr 打印就是「属性名出现即真」；不出现即假。）发射层把被删掉的 `typename T` 从 tensor 类型里重新提取、把 UnitAttr 转成 bool，拼回模板实参：

[include/ascir/Target/Asc/Common.h:98-107](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Common.h#L98-L107)

```cpp
template <typename OpType>
LogicalResult printIsSetMaskTemplate(CodeEmitter& emitter, OpType op)
{
    auto& os = emitter.ostream();
    auto tensorType = cast<ascendc::LocalTensorType>(op.getDst().getType()).getElementType();
    os << ascNamespace << "::" << op.getAPIName() << "<";
    FAIL_OR(emitter.emitType(op.getLoc(), tensorType));
    os << ", " << op.getIsSetMask() << ">";
    return success();
}
```

对应 lit 测试的期望输出（`%dst` 是 `local_tensor<1024xf32>` → `<float, 0>`，此处手写 mlir 未带 isSetMask 属性故为 0；经 Python 前端调用时默认 True 会印 1）：

[test/Target/AscendC/basic/vec_binary.mlir:13](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L13)

```cpp
// CHECK-NEXT:   AscendC::AddRelu<float, 0>(v1, v2, v3, v4, v4, v5);
```

**（6）变体多的 API：data_copy 家族。** 参数顺序约定之上，形态差异大的 API 用「同一 apiName、多个 Op」表达——`data_copy` 有 7 个变体 Op，全部 `apiName = "DataCopy"`，按参数结构体（`DataCopyParams/Nd2NzParams/...`）或搬运形态区分：

[include/ascir/Dialect/Asc/IR/Basic/OpDataCopy.td:53-58](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpDataCopy.td#L53-L58)、[OpDataCopy.td:83-88](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpDataCopy.td#L83-L88)

```tablegen
def AscendC_DataCopyNd2NzOp : DataCopyOp<"data_copy_nd2nz", "DataCopy", [AscFunc]> {
  let arguments = (ins AscendC_BaseTensorTypeInterface:$dst,
                       AscendC_BaseTensorTypeInterface:$src,
                       AscendC_Nd2NzParams:$intriParams);
}
def AscendC_DataCopyL2Op : DataCopyOp<"data_copy_l2", "DataCopy", [AscFunc]> {
  let arguments = (ins AscendC_BaseTensorTypeInterface:$dst,
                       AscendC_BaseTensorTypeInterface:$src,
                       AnyType:$calCount);
}
```

注意 `$dst/$src` 的类型约束来自 4.2 的 **Type Interface** `AscendC_BaseTensorTypeInterface`（Core/Interfaces.td）——两种 Interfaces.td 在同一个 Op 定义里协同。这也解释了 u2-l5 的结论：「data_copy 以 7 个候选支持 count、块参数、切片、ND↔NZ 等搬运形态」= 7 个 Op 变体 + Python 侧 OverloadDispatcher 分发。

**（7）进阶：paramTypeLists 标记。** 少数 Op 的参数形态超出四步法的表达力，用 `paramTypeLists` 逐位编码「这个参数如何参与模板实参生成」（-3 指针 / 0 普通 / 1 提取模板类型 / 2 从模板类型提取元素类型 / 3 非类型模板·枚举 / 4 非类型模板·常规值 / 5 类型模板 / -1 属性，全文见 [Base.td:42-57](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L42-L57) 与 [developer_guide.md:497-508](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L497-L508)）。一个两参数的最小样本：

[include/ascir/Dialect/Asc/IR/Basic/OpDataCopy.td:150-155](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpDataCopy.td#L150-L155)

```tablegen
def AscendC_SetPadValueOp : APIOp<"set_pad_value", "SetPadValue", [AscFunc]> {
  let arguments = (ins AnyType:$paddingValue,
                   AscendC_TPositionAttr:$pos);
  let paramTypeLists = [1, 3];
}
```

含义：第 0 位参数 `paddingValue` 编码 1（从该参数提取模板类型 `<typename T>`），第 1 位参数 `pos` 编码 3（枚举值非类型模板参数）——发射 `AscendC::SetPadValue<T, TPosition::VECIN>(value)` 时两位各取所需。编码与四块顺序的关系：`paramTypeLists` 与 `arguments` 一一对应，其排列同样遵循四块顺序（[developer_guide.md:498](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L498)）。该标记如何被 GenOpEmitDefs 消费属于下一讲（u5-l4）。

#### 4.3.4 代码实践

**实践目标**：不看 td，仅凭 `asc.add` 文档中的 Ascend C 原型，推导出 IR Op 的参数列表；再逐参数对照真实 mlir 实例验证——这是「反推」能力的完整闭环。

**操作步骤**：

1. **读考题**：打开 [docs/python-api/language/generated/asc.language.basic.add.md:19-31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python-api/language/generated/asc.language.basic.add.md#L19-L31)，抄下 L0 原型：`template <typename T, bool isSetMask = true> void Add(dst, src0, src1, uint64_t mask, uint8_t repeatTimes, BinaryRepeatParams repeatParams)`。
2. **跑四步算法**（写在纸上）：
   - 分类：`T`=模板·必选·可推导；`dst/src0/src1/mask/repeatTimes/repeatParams`=运行时·必选；`isSetMask`=模板·可选；
   - 删除 `T`；
   - 重排：运行时必选（6 个）→ 模板必选（无）→ 运行时可选（无）→ 模板可选（`isSetMask`）；
   - 落地：`operands = dst, src0, src1, mask, repeatTimes, repeatParams`；`attr-dict = {isSetMask?}`。
3. **对照源码**：与 [Base.td:150-156](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L150-L156) 的 `BinaryTemplateL0Op` 逐位核对。
4. **对照真值**（两种途径任选）：
   - **离线**（无需环境）：看 [test/Target/AscendC/basic/vec_vconv.mlir:91](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_vconv.mlir#L91) 的 `{isSetMask}` 打印与 [vec_binary.mlir:29-30](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L29-L30) 的 L0 操作数排布（六个操作数 + 类型列表）。
   - **在线**（需按 u1-l2 装好环境）：把 [examples/01_add/add.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py) 中的 `asc.add(z_local, x_local, y_local, TILE_LENGTH)` 改为 mask 形态 `asc.add(z_local, x_local, y_local, mask=128, repeat_times=4, repeat_params=asc.BinaryRepeatParams(1,1,1,8,8,8))`，设置 `PYASC_DUMP_PATH` 运行，在 codegen.mlir 里找 `ascendc.add_l0`，核对操作数个数与顺序。**此运行路径待本地验证**（01_add 的 UB 空间是否够放 4 次 repeat 的中间量，需按实际长度调整）。
5. **对照 Python 形参**：与 [vec_binary.py:27-29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L27-L29) 的第二个 overload 比对，确认 `is_set_mask` 殿后带默认值。

**需要观察的现象**：推导表、td 定义、mlir 实例、Python 形参四处的参数个数与顺序完全一致；mlir 里 `isSetMask` 只以 attr-dict 形式出现、从不混进操作数序列。

**预期结果**：六操作数 + 一 UnitAttr 的结论在三处源码全部命中；L2 形态（`count` 版）重复步骤 1-4 可得「三/四参数」结论，与 [vec_binary.mlir:117](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L117) 一致。

#### 4.3.5 小练习与答案

**练习 1**：Ascend C 的 `AscendC::Add` L1 原型中 `uint64_t mask[]` 是数组。IR 里如何表达？为什么不用一个「数组类型的操作数」？

**答案**：用 `Variadic<UI64>:$mask`（[Base.td:158-164](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L158-L164)）——即允许传入**可变个数**的 ui64 操作数，打印成 `%maskArray1_0, %maskArray1_1` 两个独立 SSA 值。发射层再把这些值组装成 C 数组（[vec_binary.mlir:47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L47) CHECK 的 `uint64_t v1_mask_list0[] = {v6, v7};`）。MLIR 的 SSA 值没有「C 数组」形态，变长操作数是最贴近的建模；代价是这类 Op 暂不能走全自动发射（[developer_guide.md:515-518](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L515-L518) 列为数组入参是暂不支持自动生成的场景），需手写发射模板（VecBinary.h 的 `printBinaryL1Params` + `printMask`）。

**练习 2**：为什么 `typename T` 不进 IR，而 `bool isSetMask` 必须进？判断标准是什么？

**答案**：判断标准是**可推导性**（[developer_guide.md:918](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L918) 与 [Base.td:49-50](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L49-L50)）：`T` 能从 `LocalTensor<T>` 的 IR 类型直接读出（发射层 `getElementType()` 一行代码），冗余存一份反而引入不一致的可能；`isSetMask` 无法从任何其他参数推导，是用户必须能表达的独立自由度，所以必须成为 IR 参数（UnitAttr）。同理可解释 u2-l1 的 ConstExpr：编译期常量若不可推导，就得让用户显式传入。

**练习 3**：`data_copy` 有 7 个 Op 但只有一个 `apiName = "DataCopy"`；`Add` 有 L0/L1/L2/L3 四个 Op 也只有一个 `apiName = "Add"`。两者「多 Op 一名」的原因相同吗？

**答案**：不同。Add 的多变体源于**同一计算的四种调用形态**（分级），由模板族批量生成，发射时都输出 `AscendC::Add(...)`，差异只在参数布局与是否补模板实参；data_copy 的多变体源于**参数结构体不同的独立原型**（`DataCopyParams` 块参数版、`Nd2NzParams` 格式转换版、`SliceInfo` 切片版……），C++ 侧重载解析靠参数类型区分，IR 侧没有重载机制，只能拆成不同 mnemonic 的 Op、共享 apiName 供发射统一输出函数名。前者是「一族」，后者是「一批重载」。

## 5. 综合实践

**任务：为 `asc.max` 制作一张「一名四身」全链路对照表，并用四步法验证它的参数顺序。**

`asc.max` 与 `asc.add` 同族但你没在前面实践里展开过它，正好检验举一反三。请产出一张六列表格（每行一个层级），并完成验证：

| 层级 | 内容（以 max 为例，请自行填写后核对） |
|---|---|
| Ascend C 原型 | 查 [docs/python-api/language/generated/asc.language.basic.max.md](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python-api/language/generated/asc.language.basic.max.md) |
| td 登记行 | [OpVecBinary.td:32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td#L32)，注意它用的是哪个 multiclass |
| 展开的 Op 与助记符 | 推导后在 lit 测试里反查印证 |
| IR 参数表（四步法产物） | 分类→删除→重排→落地 |
| Python 形参表 | [vec_binary.py:231-252](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L231-L252) |
| 发射输出 | lit 测试 CHECK 行 |

具体步骤：

1. 对 `Max` 的 L0 原型跑一遍 4.3.2 的四步算法，写出预期的 operands 与 attr-dict；
2. 与 [Base.td:127-148](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L127-L148) 对照——注意 `defm Max : BinaryTemplateL012Op` 走的是**带 isSetMask 的 Template 族**，但 [vec_binary.mlir:35](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L35) 的 `max_l0` 行 attr-dict 为空、[vec_binary.mlir:19](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L19) 的 CHECK 输出却是 `AscendC::Max<float, 0>`——请解释这条「td 有 UnitAttr、mlir 未打印」与「发射仍输出模板实参」的完整因果链（提示：UnitAttr 缺省 = false）；
3. 用 4.2 的方法统计 `printOperation` 的 L0 Template 名单里有没有 `MaxL0Op`（[VecBinary.h:82-92](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Basic/VecBinary.h#L82-L92)），确认它走 `printIsSetMaskTemplate` 路径；
4. 把结论写成一段「若要新增 `AscendC::Foo` 双目算子，我需要动哪几个文件」的清单（提示：OpVecBinary.td 加一行 defm；若参数形态新法则 Base.td 加模板；发射名单登记；Python 侧加接口——完整流程 u7-l6 展开）。

**验收标准**：表格六行全部填出且与源码逐项吻合；第 2 步的因果链能说清「UnitAttr 的缺省语义如何让同一条 defm 同时服务 lit 手写 mlir 与 Python 前端两条路径」。本实践全程离线可完成；如环境可用，可再加一步：把 01_add 示例中的 `asc.add` 换成 `asc.max` 运行并 dump 验证（**待本地验证**）。

## 6. 本讲小结

- **模板族**：Base.td 用「class 继承链 + multiclass」把公共结构沉淀为模板，`defm Add : BinaryTemplateL0123Op<"add","Add","operator+">` 一行展开出 L0/L1/L2/L3 四个 Op；L0/L1/L2 的参数差异精确对应 Ascend C 三条原型（单 mask / 数组 mask / calCount），`UnitAttr:$isSetMask` 是模板可选参数的 IR 形态；定义完备性优先于前端使用（L3 目前无 Python 入口）。
- **API Interface**：顶层 `Interfaces.td` 声明 Op 接口（`APIOpInterface` 的 `getAPIName/getComment`、参数访问器池、带 C++ 方法体的 `getDirection`），`Core/Interfaces.td` 声明 Type 接口（`BaseTensorType` 等做操作数类型约束），两者职责不同；默认实现由 `Base.td` 的 `extraClassDeclaration` 织入，生成物经 `mlir_tablegen -gen-op-interface-defs` 进 `AscendCOpInterfaces.cpp.inc`，被 4 行的 `OpInterfaces.cpp` 编入库。
- **统一接口的发射收益**：接口方法名与 td 形参名对齐，使 `VecBinary.h` 里一个 C++ 发射模板经 `LogicalResultForT` 白名单服务约 20 个同族 Op——一条命名纪律换来一个数量级的复用。
- **参数顺序约定**：Ascend C 的「运行时 × 模板 × 必选 × 可选」四象限签名，按「运行时必选 → 模板必选 → 运行时可选 → 模板可选」重排成 Python 形参与 IR 参数；配套两条规则——可推导的类型模板参数（`typename T`）不进 IR、不可推导的模板参数以 UnitAttr 殿后。
- **反推四步法**：分类 → 删除（可推导模板参数）→ 重排（四块顺序）→ 落地（operands + attr-dict），可从任意 Ascend C 签名推出 IR 参数表；发射层逆向还原（`getElementType()` 提取 `T`、UnitAttr 转 bool）保证两个世界签名不错位。
- **两个补充机制**：形态差异大的 API 用「同一 apiName、多个 Op 变体」建模（data_copy 7 变体）；超出四步法表达力的参数用 `paramTypeLists` 逐位编码（-3/-1/0/1/2/3/4/5），其生成侧消费留待 u5-l4。

## 7. 下一步学习建议

- **下一讲 u5-l4（TableGen 代码生成）**：本讲反复出现的四条生成管线（`-gen-op-defs`、`-gen-op-interface-defs`、`-gen-pybind-defs`、`-gen-opemit-decls/-defs`）在 `lib/TableGen` 的 `main.cpp`、`GenPybindDefs.cpp`、`GenOpEmitDefs.cpp`、`GenOpEmitDecls.cpp` 里如何注册与实现——`create_asc_AddL2Op` 的绑定代码和 `paramTypeLists` 的消费逻辑都在那里揭晓。
- **u5-l5（pybind 桥接层）**：Python 侧 `builder.create_asc_AddL2Op(...)` 调用如何穿过 `python/src/OpBuilder.cpp` 到达 MLIR C++。
- **u6-l5（Ascend C 代码发射）**：本讲的 `printOperation`/`printIsSetMaskTemplate` 属于其「手写发射」分支；完整的 CodeEmitter 分发、EmitNameStack 与 EmitAsc 方言在那一讲展开。
- **随手练习**：下次在 dump 出的 codegen.mlir 里看到陌生的 `ascendc.xxx_l2`，先用「四名合一」反查 td 文件，再用四步法从文档原型推一遍参数表，最后看发射头文件（`include/ascir/Target/Asc/<象限>/<Api>.h`）验证——三分钟内完成的完整闭环，是最好的日常复习。
