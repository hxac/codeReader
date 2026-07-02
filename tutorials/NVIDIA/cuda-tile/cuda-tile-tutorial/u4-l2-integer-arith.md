# 整数算术与比较操作

## 1. 本讲目标

本讲承接 u4-l1（核心数据操作），进入 `cuda_tile` 方言 **Integer 分组**的整数算术与比较操作。读完本讲，你应当能够：

- 说出 Integer 分组包含哪些操作，并能按「是否需要 `signedness`」「是否带 `overflow` 提示」给它们分类。
- 区分 `signed` 与 `unsigned` 在 `divi`/`remi`/`maxi`/`mini`/`shri`/`cmpi` 中的语义差别，理解为何 `addi`/`subi`/`muli` 反而**不需要**符号标注。
- 理解 `NSW`/`NUW`/`NW` 三种溢出提示（overflow hint）是「编译期假设」而非运行时检查，以及违反它为何是未定义行为。
- 写出 `divi` 的有符号除法与「向下取整除法（floor div）」两种写法，并知道 `negative_inf` 与 `unsigned` 是非法组合。
- 用 `cmpi` 配合六种谓词写出整数比较，并能从 `arith_invalid.mlir` 中识别常见的验证报错。

## 2. 前置知识

- **Tile 与元素类型**（见 u3-l1）：本讲所有操作都作用在 `tile<...>` 上，元素类型只能是 `i1/i8/i16/i32/i64`。注意 `i4` 虽然是合法的 Tile 元素类型，却**不能**用于整数算术（后面会看到验证报错）。
- **操作分组**（见 u2-l2）：`cuda_tile` 把操作归入 11 个分组，本讲的操作都派生自 `CudaTileIntegerOpDef`，属于 **Integer** 分组。
- **TableGen 与 `.td`**（见 u2-l3）：操作用 `Ops.td` 声明，属性用 `AttrDefs.td` 声明，本讲会频繁引用这两份文件。
- **有符号性（signedness）**：计算机里同样一串二进制位，按「有符号」解读可能得到负数，按「无符号」解读则永远是正数。比如 8 位的 `0xFF`，无符号是 255，有符号是 −1。除法、比较、最大最小等操作的语义会随符号解读而变，因此需要一个显式的 `signedness` 属性。
- **溢出（overflow）**：N 位整数只能表示有限范围，超出范围就「绕回（wrap-around）」。例如 8 位下 `200 + 100` 会得到 44 而不是 300。加法/减法/乘法默认就是绕回语义；而 `NSW`/`NUW`/`NW` 是程序员向编译器作出的「我保证不会溢出」的承诺。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/cuda_tile/Dialect/CudaTile/IR/Ops.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td) | 声明所有 Integer 分组操作（`addi`/`subi`/`muli`/`divi`/`remi`/`maxi`/`mini`/`absi`/`negi`/`shli`/`shri`/`cmpi`/`mulhii`）。 |
| [include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td) | 声明本讲三个关键属性：`Signedness`、`IntegerOverflow`、`ComparisonPredicate`（以及 `RoundingMode`）。 |
| [include/cuda_tile/Dialect/CudaTile/IR/Dialect.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td) | 定义 `CudaTileIntegerOpDef` 基类，把操作归入 Integer 分组。 |
| [test/Dialect/CudaTile/arith.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/arith.mlir) | 合法用例的「黄金样本」，每个操作都能 round-trip。 |
| [test/Dialect/CudaTile/arith_invalid.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/arith_invalid.mlir) | 非法用例集，用 `-verify-diagnostics` 锁定每一条验证报错。 |

## 4. 核心概念与源码讲解

### 4.1 Integer 操作族与有符号性

#### 4.1.1 概念说明

Integer 分组是一组「逐元素（element-wise）」的整数运算：输入两个形状相同的整数 tile，按对应位置计算，输出一个同形状的整数 tile。它们的共同基底是 `CudaTileIntegerOpDef`，由它在元数据里把操作登记到 `Integer` 分组：

[include/cuda_tile/Dialect/CudaTile/IR/Dialect.td:147-148](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L147-L148) — `CudaTileIntegerOpDef` 基类，固定 `group = "Integer"`。

这一组操作最重要的设计取舍是：**有些操作需要显式声明 `signedness`，有些不需要**。判据是「位模式相同与否」：

- **位模式与符号无关的操作**（`addi`/`subi`/`muli`/`shli`）：补码加法、减法、乘法、左移的「二进制结果」与符号解读无关，因此**不带** `signedness`。
- **位模式与符号有关的操作**（`divi`/`remi`/`maxi`/`mini`/`shri`/`cmpi`）：除法、取余、最大最小、算术右移、比较的结果会随符号解读而变，因此**必须**带 `signedness`。

`signedness` 由一个枚举属性 `Signedness` 给出，只有两个取值：

[include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td:45-52](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L45-L52) — `Signedness` 属性定义，`Unsigned=0`、`Signed=1`。

#### 4.1.2 核心流程

一个 Integer 操作在文本里的通用形态（以需要符号的 `divi` 为例）：

```
%result = divi %lhs, %rhs signed  [rounding<...>]  : tile<...>
                       ^^^^^^^                      ^^^^^^^^^^
                       signedness 属性              操作数/结果类型
```

- 解析时，custom assembly directive `custom<Signedness>(...)` 把关键字 `signed`/`unsigned` 翻译成 `SignednessAttr`。
- 验证时，类型约束 `CudaTile_IntTileType` 要求操作数与结果都是合法的整数 tile（`i1/i8/i16/i32/i64`）。
- `AllTypesMatch<["lhs","rhs","result"]>` 强制三者类型完全一致（形状 + 元素类型）。

#### 4.1.3 源码精读

各操作的归属与是否带符号、是否带溢出提示，可整理成下表（行号均指向 `Ops.td`）：

| 操作 | 行号 | `signedness` | `overflow` | 语义要点 |
| --- | --- | --- | --- | --- |
| `addi` | [123-140](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L123-L140) | 否 | 是 | \(x+y\) |
| `subi` | [4636-4654](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4636-L4654) | 否 | 是 | \(x-y\) |
| `muli` | [3461-3479](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3461-L3479) | 否 | 是 | \(x\times y\) |
| `mulhii` | [3485-3525](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3485-L3525) | 否（仅无符号定义） | 否 | 取 2N 位乘积的高 N 位 |
| `divi` | [1416-1446](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1416-L1446) | 是 | 否 | 除法，带舍入模式 |
| `remi` | [3976-4004](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3976-L4004) | 是 | 否 | 截断除法取余 |
| `maxi` | [3191-3240](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3191-L3240) | 是 | 否 | 逐元素最大 |
| `mini` | [3316-3360](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3316-L3360) | 是 | 否 | 逐元素最小 |
| `shli` | [4277-4297](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4277-L4297) | 否（移位量按无符号） | 是 | 左移，低位补 0 |
| `shri` | [4303-4327](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4303-L4327) | 是 | 否 | 右移，`signed` 算术右移 / `unsigned` 逻辑右移 |
| `absi` | [99-117](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L99-L117) | 否（输入有符号/输出无符号，固定） | 否 | 绝对值 |
| `negi` | [3531-3561](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3531-L3561) | 否（固定有符号） | 是 | 取负 |
| `cmpi` | [1096-1151](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1096-L1151) | 是 | 否 | 比较，结果为 i1 tile |

> 说明：`absi` 的特殊性在于——输入按**有符号**解读，输出按**无符号**解读（取绝对值后必然非负），见 [Ops.td:105-106](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L105-L106)；`mulhii` 则**只对无符号整数定义**，见 [Ops.td:3497](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3497)。

#### 4.1.4 代码实践

**实践目标**：通过阅读 `arith.mlir` 建立 Integer 操作的「合法形态」直觉。

**操作步骤**：

1. 打开 `test/Dialect/CudaTile/arith.mlir`，定位 `entry @addi()`（[第 10-35 行](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/arith.mlir#L10-L35)），观察 `addi` 没有 `signed`/`unsigned` 字样。
2. 定位 `entry @divi()`（[第 89-131 行](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/arith.mlir#L89-L131)），对比 `signed` 与 `unsigned` 两种写法。
3. 在构建产物可用时，运行 `cuda-tile-opt test/Dialect/CudaTile/arith.mlir`，确认能正常输出（该文件第一条 RUN 行即是此命令）。

**需要观察的现象**：`addi` 行只有 `addi %a, %b : tile<i32>`；`divi` 行则是 `divi %a, %b signed : tile<i32>` 与 `divi %a, %b unsigned : tile<i32>` 成对出现。

**预期结果**：`cuda-tile-opt` 退出码为 0，无诊断输出。若尚未构建项目，则此为「源码阅读型实践」，记住「需要符号的操作一定带 `signed`/`unsigned`」这一规律即可。

#### 4.1.5 小练习与答案

**练习 1**：下列哪些操作必须带 `signedness`？`addi`、`divi`、`muli`、`cmpi`、`shli`。

**答案**：`divi` 和 `cmpi` 必须带；`addi`/`muli`/`shli` 不带（位模式与符号无关）。注意 `shri`（右移）虽然也是移位，但它**带** `signedness`，因为算术右移与逻辑右移结果不同。

**练习 2**：`absi` 的输入和输出分别按什么符号解读？

**答案**：输入按有符号，输出按无符号（取绝对值后非负）。

---

### 4.2 加减乘与溢出提示 NSW / NUW / NW

#### 4.2.1 概念说明

`addi`/`subi`/`muli`/`shli`/`negi` 默认是**绕回（wrap-around）语义**，这一点写在一段被多个操作复用的描述串 `integer_arith_suffix` 里：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:52-55](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L52-L55) — 默认 wrap-around 语义说明。

由于这些操作的位结果与符号无关，它们没有 `signedness`，却可以挂一个**可选**的 `overflow` 提示属性，取自 `IntegerOverflow` 枚举：

[include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td:63-80](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L63-L80) — `IntegerOverflow` 四个取值 `NONE/NSW/NUW/NW`。

| 取值 | 文本拼写 | 含义 |
| --- | --- | --- |
| `NONE`(0) | `none` | 不做任何假设（默认） |
| `NSW`(1) | `no_signed_wrap` | 假设按**有符号**解读不会溢出 |
| `NUW`(2) | `no_unsigned_wrap` | 假设按**无符号**解读不会溢出 |
| `NW`(3) | `no_wrap` | 假设有符号和无符号都不会溢出 |

关键点：`overflow` 是**编译期假设**，编译器**可以**据此优化（例如利用「不会溢出」消除断言、推导值域）。它**不是**运行时检查。规范明确警告：

> If an overflow occurs at runtime despite the value of overflow stating otherwise, the behavior is undefined.（见 [AttrDefs.td:77](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L77)）

因此「对操作施加 NSW」本身不会触发验证错误——它只是程序员向编译器立下的「军令状」。

#### 4.2.2 核心流程

以 `muli` 为例，其声明带一个**带默认值**的 `overflow` 参数：

```
DefaultValuedAttr<CudaTile_IntegerOverflowAttr,
                  "::mlir::cuda_tile::IntegerOverflow::NONE">
```

解析与打印遵循可选 directive：当 `overflow` 等于默认值 `NONE` 时不打印，否则打印成 `overflow<no_signed_wrap>` 之类。文本里写法是：

```
%m = muli %a, %b overflow<no_signed_wrap> : tile<i32>
```

> 关于「无符号操作施加 NSW」：`addi`/`subi`/`muli` 等**没有** `signedness` 概念，它们是符号无关的。`NSW` 与 `NUW` 只是两种不同角度的「不溢出」承诺，可以自由组合，编译器据此推理——不会因为「这是乘法却加了 NSW」而报错。真正会报错的是**类型**与**符号/舍入搭配**问题，见 4.3 与 4.4。

#### 4.2.3 源码精读

`AddIOp` 的参数列表与 assembly format：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:133-139](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L133-L139) — `addi` 第三个参数是带默认 `NONE` 的 `overflow`，打印格式中 `(`overflow` `` $overflow^)?` 表示仅在非默认时输出。

`MulIOp` 与之同构：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:3472-3478](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3472-L3478) — `muli` 同样带可选 `overflow`。

`mulhii` 的对比价值：它取乘积的高位，因此描述里特别强调「与 `muli` 取低位相对」，且**只定义无符号**：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:3489-3497](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3489-L3497) — `mulhii` 取 2N 位乘积的高 N 位，且仅对无符号整数定义。

#### 4.2.4 代码实践

**实践目标**：写出带 `NSW` 提示的乘法，并确认它能被工具接受（不报错）。

**操作步骤**：

1. 新建文件 `mul_overflow.mlir`，内容如下（**示例代码**）：

   ```mlir
   cuda_tile.module @mul_ex {
     entry @f() {
       %a = constant <i32: 100> : !cuda_tile.tile<i32>
       %b = constant <i32: 7> : !cuda_tile.tile<i32>
       // 带 NSW 提示的乘法
       %m = muli %a, %b overflow<no_signed_wrap> : !cuda_tile.tile<i32>
     }
   }
   ```

2. 运行 `cuda-tile-opt mul_overflow.mlir`，观察输出。

**需要观察的现象**：输出里 `%m` 一行应回显为 `muli ... overflow<no_signed_wrap>`；如果把 `no_signed_wrap` 改成默认的省略写法（删掉 `overflow<...>`），回显里就不会再出现 `overflow` 字样。

**预期结果**：退出码 0，无诊断。若工具未构建，则按源码 [Ops.td:3474](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3474) 理解：`overflow` 默认 `NONE`，故省略等价于不立任何「不溢出」承诺。**待本地验证**：具体回显格式以本地 `cuda-tile-opt` 输出为准。

#### 4.2.5 小练习与答案

**练习 1**：`NSW` 和 `NUW` 的区别是什么？运行时若 `muli` 标了 `NSW` 却真的溢出了，会发生什么？

**答案**：`NSW` 承诺「按有符号解读不溢出」，`NUW` 承诺「按无符号解读不溢出」，`NW` 同时承诺两者。运行时若违反，行为是**未定义**（UB），编译器可能已经基于该假设做了改写，结果不可预测。

**练习 2**：为什么 `mulhii` 不需要也不允许 `overflow` 提示？

**答案**：`mulhii` 取的是 2N 位乘积的高 N 位，本身不发生「截断式溢出」，它就是用来获取 `muli` 丢掉的高位信息的；且它只对无符号定义，没有溢出假设可言。

---

### 4.3 divi / remi：有符号除法与舍入模式

#### 4.3.1 概念说明

`divi` 是 Integer 分组里语义最丰富的操作之一。它同时带有**必填**的 `signedness` 和**可选**的 `rounding`。默认舍入是「向零取整（truncation）」，另可指定 `positive_inf`（向上取整 / ceil）或 `negative_inf`（向下取整 / floor，即 Python 风格的 floordiv）。注意一条硬约束：**`negative_inf` 不能与 `unsigned` 同时使用**。

`remi` 是与 `divi` 配套的取余，固定采用截断除法（向零取整），其结果符号在有符号时跟随被除数 `lhs`。

#### 4.3.2 核心流程

`divi` 对两个整数 tile 逐元素做除法：

\[\text{div}(\text{lhs}, \text{rhs})_i = \text{lhs}_i / \text{rhs}_i\]

舍入模式决定「商不整时往哪个方向取」：

| 舍入 | 方向 | 对应数学语义 |
| --- | --- | --- |
| `zero`（默认） | 向零 | 截断除法（C/Java 语义） |
| `positive_inf` | 向上 | ceil 除法 |
| `negative_inf` | 向下 | floor 除法（Python 语义） |

合法组合校验由 `divi` 的 `hasVerifier = 1` 完成：当 `rounding == negative_inf` 且 `signedness == unsigned` 时报错。

#### 4.3.3 源码精读

`DivIOp` 的描述明确列出了舍入与非法组合：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:1419-1435](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1419-L1435) — 默认向零取整；可设 `positive_inf`/`negative_inf`；`negative_inf` 与 `unsigned` 不是合法组合；除以零、有符号「最小值 ÷ −1」溢出均为未定义行为。

参数与格式：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:1437-1445](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1437-L1445) — `divi` 带 `signedness`（必填）与 `rounding`（默认 `RoundingMode::ZERO`），并开启自定义 verifier。

`remi` 的描述给出取余符号规则与示例：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:3980-3996](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3980-L3996) — 截断除法取余；有符号时结果符号跟随被除数，例如 `remi(7,-3)=1`、`remi(-7,3)=-1`。

非法组合的真实报错形态，见 invalid 测试：

[test/Dialect/CudaTile/arith_invalid.mlir:597-603](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/arith_invalid.mlir#L597-L603) — `divi ... unsigned rounding<negative_inf>` 触发 `rounding mode 'negative_inf' is not allowed with 'unsigned' flag`。

缺失 `signedness` 的报错：

[test/Dialect/CudaTile/arith_invalid.mlir:671-678](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/arith_invalid.mlir#L671-L678) — 省略 `signed`/`unsigned` 触发 `expected signedness to be one of: {'signed', 'unsigned'}`。

#### 4.3.4 代码实践

**实践目标**：写出有符号除法与向下取整除法，并主动触发「`negative_inf` + `unsigned`」非法组合，观察验证差异。

**操作步骤**：

1. 准备合法文件 `div_ok.mlir`（**示例代码**）：

   ```mlir
   cuda_tile.module @div_ex {
     entry @f() {
       %a = constant <i32: [7, -7]> : !cuda_tile.tile<2xi32>
       %b = constant <i32: [2, 2]>  : !cuda_tile.tile<2xi32>
       // 有符号截断除法：7/2=3, -7/2=-3
       %t = divi %a, %b signed : !cuda_tile.tile<2xi32>
       // 有符号向下取整除法：7/2=3, -7/2=-4
       %f = divi %a, %b signed rounding<negative_inf> : !cuda_tile.tile<2xi32>
     }
   }
   ```

2. 准备非法文件 `div_bad.mlir`（**示例代码**，参考 `arith_invalid.mlir` 的 `floordivi_unsigned`）：

   ```mlir
   cuda_tile.module @div_bad {
     entry @f() {
       %a = constant <i32: 7> : !cuda_tile.tile<i32>
       %b = constant <i32: 2> : !cuda_tile.tile<i32>
       // 非法：negative_inf 与 unsigned 不能共存
       %x = divi %a, %b unsigned rounding<negative_inf> : !cuda_tile.tile<i32>
     }
   }
   ```

3. 分别运行 `cuda-tile-opt div_ok.mlir` 与 `cuda-tile-opt div_bad.mlir`。

**需要观察的现象**：合法文件正常输出；非法文件报 `rounding mode 'negative_inf' is not allowed with 'unsigned' flag`。

**预期结果**：与 [arith_invalid.mlir:600](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/arith_invalid.mlir#L600) 中的 `expected-error` 完全一致。**待本地验证**：确切的商值需在能执行 IR 的环境中观察，本实践只验证「合法/非法」的验证器行为。

#### 4.3.5 小练习与答案

**练习 1**：对 `i32` 的 `-7` 和 `2`，`divi signed`（默认向零）与 `divi signed rounding<negative_inf>` 结果分别是什么？

**答案**：向零取整得 −3（截断）；向下取整得 −4（floor）。

**练习 2**：`remi(-7, 3) signed` 等于多少？为什么不是 2？

**答案**：等于 −1。因为 `remi` 用截断除法，`-7 / 3` 向零取整为 −2，余数 `= -7 − (-2)*3 = -1`；符号跟随被除数 `lhs`（负），所以是 −1 而非正数。

---

### 4.4 cmpi：整数比较与谓词

#### 4.4.1 概念说明

`cmpi` 对两个同形状整数 tile 逐元素比较，输出一个**同形状、元素为 i1** 的 tile（真为 1，假为 0）。它同时需要两个属性：比较谓词 `comparison_predicate` 与 `signedness`。谓词取自 `ComparisonPredicate` 枚举，共六种：

[include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td:236-250](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L236-L250) — 六个谓词 `equal/not_equal/less_than/less_than_or_equal/greater_than/greater_than_or_equal`。

结果类型由一个 `TypesMatchWith` 推导：把操作数的元素类型替换成 i1，形状不变，即「`getI1SameShape`」。

#### 4.4.2 核心流程

`cmpi` 的逐元素语义：

\[\text{cmpi}(x, y, \text{pred})_i = \begin{cases}1 & \text{if } x_i \;\text{pred}\; y_i \\ 0 & \text{otherwise}\end{cases}\]

文本语法（custom assembly）：

```
%r = cmpi less_than %lhs, %rhs, signed : tile<2xi32> -> tile<2xi1>
```

等价的 generic 写法（属性以字典显式给出）：

```
%r = "cuda_tile.cmpi"(%lhs, %rhs) {
       comparison_predicate = #cuda_tile.comparison_predicate<less_than>,
       signedness = #cuda_tile.signedness<signed>
     } : (tile<2xi32>, tile<2xi32>) -> tile<2xi1>
```

两种写法在 `arith.mlir` 中成对出现，用来验证 custom parser/printer 与 generic 形式的 round-trip 一致。

#### 4.4.3 源码精读

`CmpIOp` 的声明，注意结果类型推导 `getI1SameShape`：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:1096-1098](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1096-L1098) — `cmpi` 带 `Pure`、`AllTypesMatch<["lhs","rhs"]>`，以及把结果元素类型替换为 i1 的 `TypesMatchWith`。

参数与格式：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:1136-1146](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1136-L1146) — 依次为 `comparison_predicate`、`lhs`、`rhs`、`signedness`，结果元素类型限定为 `Int1`。

合法用例（同时给出 custom 与 generic 写法）：

[test/Dialect/CudaTile/arith.mlir:42-43](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/arith.mlir#L42-L43) — `cmpi less_than ..., signed : tile<i1> -> tile<i1>` 的两种等价写法。

非法用例：结果形状与操作数不一致会触发 `TypesMatchWith` 校验失败：

[test/Dialect/CudaTile/arith_invalid.mlir:343-349](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/arith_invalid.mlir#L343-L349) — 操作数是 `tile<2x2xi32>` 却声明结果为标量 `tile<i1>`，报 `failed to verify that Result type has i1 element type and same shape as operands`。

非法谓词的报错：

[test/Dialect/CudaTile/arith_invalid.mlir:297-303](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/arith_invalid.mlir#L297-L303) — 写成 `cmpi invalid_predicate ...`，报 `'comparison_predicate' to be one of: {...}`。

#### 4.4.4 代码实践

**实践目标**：用 `cmpi` 写比较，并复现「结果形状与操作数不一致」的验证错误。

**操作步骤**：

1. 合法文件 `cmp_ok.mlir`（**示例代码**）：

   ```mlir
   cuda_tile.module @cmp_ex {
     entry @f() {
       %a = constant <i32: [1, 2, 3, 4]> : !cuda_tile.tile<4xi32>
       %b = constant <i32: [4, 3, 2, 1]> : !cuda_tile.tile<4xi32>
       // 逐元素 a < b，结果为 tile<4xi1>
       %r = cmpi less_than %a, %b, signed : !cuda_tile.tile<4xi32> -> !cuda_tile.tile<4xi1>
     }
   }
   ```

2. 非法文件 `cmp_bad.mlir`（**示例代码**，结果形状写错）：

   ```mlir
   cuda_tile.module @cmp_bad {
     entry @f() {
       %a = constant <i32: [1, 2, 3, 4]> : !cuda_tile.tile<4xi32>
       // 错误：操作数是一维 4 元素，结果却写成标量 tile<i1>
       %r = "cuda_tile.cmpi"(%a, %a) {
              comparison_predicate = #cuda_tile.comparison_predicate<equal>,
              signedness = #cuda_tile.signedness<signed>
            } : (!cuda_tile.tile<4xi32>, !cuda_tile.tile<4xi32>) -> !cuda_tile.tile<i1>
     }
   }
   ```

3. 分别运行 `cuda-tile-opt cmp_ok.mlir` 与 `cuda-tile-opt cmp_bad.mlir`。

**需要观察的现象**：合法文件正常；非法文件报 `failed to verify that Result type has i1 element type and same shape as operands`。

**预期结果**：与 [arith_invalid.mlir:346](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/arith_invalid.mlir#L346) 的 `expected-error` 一致。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`cmpi` 的结果元素类型是什么？形状由什么决定？

**答案**：元素类型恒为 `i1`；形状与操作数相同（由 `getI1SameShape` 推导）。

**练习 2**：`cmpi equal %a, %b, signed` 与 `cmpi equal %a, %b, unsigned` 结果有区别吗？

**答案**：没有区别。相等/不等比较只看位模式是否相同，与符号解读无关（`cmpi` 的描述在示例注释里也点明了这一点，见 [Ops.td:1130-1131](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1130-L1131)）；但语法上 `signedness` 仍是必填项。

---

## 5. 综合实践

把本讲四个模块串起来，写一个**包含全部要点的整数运算 entry 内核**，并用 `cuda-tile-opt` 验证合法形态、用 `arith_invalid.mlir` 的套路构造一个对应的非法形态。

**合法版本 `integer_demo.mlir`（示例代码）**：

```mlir
cuda_tile.module @demo {
  entry @f() {
    // 1) 构造两个整数 tile
    %a = constant <i32: [10, 20]> : !cuda_tile.tile<2xi32>
    %b = constant <i32: [3, 7]>   : !cuda_tile.tile<2xi32>

    // 2) 加法（无符号性，带 NSW 提示）
    %sum = addi %a, %b overflow<no_signed_wrap> : !cuda_tile.tile<2xi32>

    // 3) 乘法（带 NSW 提示）
    %prod = muli %a, %b overflow<no_signed_wrap> : !cuda_tile.tile<2xi32>

    // 4) 有符号除法（默认向零取整）
    %q = divi %a, %b signed : !cuda_tile.tile<2xi32>

    // 5) 有符号向下取整除法
    %fq = divi %a, %b signed rounding<negative_inf> : !cuda_tile.tile<2xi32>

    // 6) 比较 a < b，得到 i1 tile
    %lt = cmpi less_than %a, %b, signed : !cuda_tile.tile<2xi32> -> !cuda_tile.tile<2xi1>
  }
}
```

**配套任务**：

1. 运行 `cuda-tile-opt integer_demo.mlir`，确认无诊断、退出码 0。
2. 把第 5 步改成 `%fq = divi %a, %b unsigned rounding<negative_inf> ...`，重跑，确认复现 [arith_invalid.mlir:600](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/arith_invalid.mlir#L600) 的报错。
3. 把第 6 步的结果类型故意改成 `tile<i1>`（标量），重跑，确认复现「result shape 与操作数不一致」的报错。
4. 把 `%a`/`%b` 的元素类型从 `i32` 换成 `i4`，重跑 `addi`，确认复现「must be tile of i1 or i8 or i16 or i32 or i64 values」（参考 [arith_invalid.mlir:70-75](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/arith_invalid.mlir#L70-L75)）。

> 若尚未构建项目，本实践退化为「源码阅读 + 推理」型：依据 `Ops.td` 的声明与 `arith_invalid.mlir` 的 `expected-error`，逐条说明每个改动会命中哪条验证规则。

## 6. 本讲小结

- Integer 分组操作都派生自 `CudaTileIntegerOpDef`，归入 `Integer` 分组；默认 wrap-around 语义。
- **是否带 `signedness`** 取决于「位结果是否与符号有关」：`addi/subi/muli/shli` 不带；`divi/remi/maxi/mini/shri/cmpi` 必带。
- `NSW/NUW/NW` 是 `addi/subi/muli/shli/negi` 上的**编译期假设**（UB 若违反），不是运行时检查，施加它们不会触发验证错误。
- `divi` 默认向零取整，可改 `positive_inf`/`negative_inf`；`negative_inf` 与 `unsigned` 是非法组合，会被 verifier 拒绝。
- `remi` 用截断除法，有符号时结果符号跟随被除数。
- `cmpi` 结果是同形状的 i1 tile，谓词六种，`signedness` 必填（`equal`/`not_equal` 实际与符号无关）。
- `i4` 不能用于整数算术，合法元素类型仅 `i1/i8/i16/i32/i64`。

## 7. 下一步学习建议

- 下一讲 **u4-l3 浮点算术与 FMA**：把本讲的 `signedness`/`overflow` 心智模型迁移到浮点侧——浮点用 `rounding`（舍入模式）与 `flush_to_zero` 取代了整数的符号/溢出提示，对照学习会更轻松。
- 进阶可读 **u6-l2 assume 操作与静态假设谓词**：`div_by`/`bounded` 等假设属性能向编译器补充「这个整数 tile 一定满足某条件」的静态信息，与本讲的 `overflow` 提示一脉相承。
- 想了解这些操作如何被序列化进字节码，可先看 **u7-l1 字节码翻译管线**，理解 `.td` 里的 `sinceVersion`（如 `negi` 的 `overflow` 自 13.2 引入）如何影响版本兼容。
