# assume 操作与静态假设谓词

## 1. 本讲目标

学完本讲后，你应该能够：

- 理解 `cuda_tile.assume` 操作的本质——它**不产生任何运行时计算**，只是把一个 SSA 值透传出来，同时为它附加一条「静态假设」，供编译器在代码生成时利用。
- 掌握三种内置谓词属性：`div_by`（整除性，含 `every`/`along` 分组）、`same_elements`（同元素重复模式）、`bounded`（值域上下界），以及它们各自合法的输入类型与边界约束。
- 理解 `AssumePredicateAttrInterface` 接口如何把「谓词的合法性校验」下放到每个具体谓词属性，以及 `assume` 的规范化（fold）为何能把连续的同谓词 `assume` 链折叠成一条。
- 学会在 MLIR 文本与 Python 绑定两种途径下使用 `assume`，并能读懂校验器对非法谓词给出的报错。

## 2. 前置知识

本讲建立在以下已学概念之上（见前置讲义摘要）：

- **Tile 类型与元素类型（u3-l1）**：`tile<shapexelem>` 是落在寄存器里、编译期形状确定的小矩阵；元素类型包括整数 `i1/i8/i16/i32/i64`、浮点 `f16/bf16/f32/...` 等。
- **指针类型与 Token 类型（u3-l2）**：`ptr<f32>` 是有类型的全局显存指针，可作 Tile 元素得到「指针 tile」`tile<Nxptr<f32>>`；`offset` 按 pointee 位宽把整数偏移换算成字节地址增量。
- **内存模型与 Token 顺序（u5-l1）**：访存操作（`load_ptr_tko`/`store_ptr_tko`）基于指针 tile 读写显存。
- **属性系统与优化提示（u6-l1）**：`cuda_tile` 方言的属性由 `.td` 声明、`tblgen` 生成 `.inc`、`registerAttributes` 注册；属性可以是带参数自定义属性（`CudaTileAttrDef`）或枚举属性。

补充一个本讲要用到的 MLIR 基础概念：

- **接口（Interface）**：MLIR 的接口是一种「契约」，用一组方法刻画一类对象的共同行为。例如 `AssumePredicateAttrInterface` 规定「凡是能当 `assume` 谓词的属性，都必须实现 `verifyWithAssumeOp` 方法」。接口让 `assume` 操作本身不必知道每种谓词的细节，只需统一调用接口方法即可。

- **OpFoldResult / fold**：MLIR 的规范化（canonicalization）机制允许操作声明一个 `fold` 函数，在编译期把某些操作折叠成更简单的形式（如常量折叠、消除冗余操作）。本讲会看到 `assume` 如何用 `fold` 合并冗余的谓词链。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
| --- | --- |
| [include/cuda_tile/Dialect/CudaTile/IR/Ops.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td) | 声明 `assume` 操作本身（参数、结果、汇编格式、verifier、fold） |
| [include/cuda_tile/Dialect/CudaTile/IR/Interfaces.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Interfaces.td) | 定义 `AssumePredicateAttrInterface` 接口 |
| [include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td) | 定义 `div_by`/`same_elements`/`bounded` 三个谓词属性 |
| [lib/Dialect/CudaTile/IR/Attributes.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp) | 三个谓词的 `verifyWithAssumeOp` 合法性校验实现 |
| [lib/Dialect/CudaTile/IR/CudaTile.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp) | `assume` 的 verifier/fold 实现与谓词的自定义解析/打印 |
| [python/cuda_tile/dialects/cuda_tile_ops.py](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py) | Python 高层 API：`assume_div_by`/`assume_same_elements`/`assume_bounded` |

## 4. 核心概念与源码讲解

### 4.1 assume 操作与谓词接口：静态假设的载体

#### 4.1.1 概念说明

GPU 内核编译器在生成机器码时，常常因为「不知道某些运行时值的性质」而被迫保守地生成低效代码。例如：不知道一组地址是否按 16 字节对齐，就只能生成逐元素访问；不知道某个整数是否落在小范围内，就无法用更窄的寄存器。

**静态假设（static assumption）** 就是前端把这些「保证成立」的性质告诉编译器，换取更优代码。它的关键特征是：**它不是断言，编译器和运行时都不会去检查它是否成立**。前端必须保证其正确性；一旦假设错误，程序行为未定义（miscompilation）。这与 `assert` 操作（运行时检查）截然不同。

`cuda_tile.assume` 操作就是承载静态假设的语法单元。它的行为极简：

- 接收一个值 `value` 和一个谓词属性 `predicate`。
- **原样透传** `value` 作为结果 `result`（二者类型完全相同）。
- 把 `predicate` 作为「`result` 的一条性质」记录下来。

它本身不做任何计算，只起「贴标签」的作用。谓词必须实现 `AssumePredicateAttrInterface` 接口。

#### 4.1.2 核心流程

```
输入: value (任意 SSA 值), predicate (实现 AssumePredicateAttrInterface 的属性)
  ↓
assume 操作:
  1. verifier 调用 predicate.verifyWithAssumeOp(this)  ← 把合法性校验下放给谓词
  2. result = value  (类型不变, 仅复制 SSA 引用)
  3. predicate 作为 result 的一条性质被下游/编译器感知
  ↓
输出: result (类型 == value 的类型), 携带静态假设
```

合法性校验的关键设计：`assume` 的 verifier 几乎是空的，它只做一件事——把 `predicate` 这个属性当作接口，调用其 `verifyWithAssumeOp`，由**谓词自己**决定「我这个假设能否合法地贴在这类值上」。这就是接口带来的解耦。

#### 4.1.3 源码精读

`assume` 操作定义在 Ops.td 中（属于 Miscellaneous 分组，sinceVersion 13.1）：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:300-354](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L300-L354) 定义 `assume` 操作。

其中几处关键：

```tablegen
// 结果类型与输入 value 完全一致（透传）
[AllTypesMatch<["value", "result"]>, ...]

let arguments = (ins ...:$value,
                     ...:$predicate);   // predicate 必须实现 AssumePredicateAttrInterface
let results = (outs ...:$result);
let assemblyFormat = "custom<AssumePredicate>($predicate) `,` $value  attr-dict `:` ...";
let hasVerifier = 1;
let hasFolder = 1;
```

描述里有一段至关重要的 `note`：

> `assume` does not check the correctness of the predicate. Incorrect predicates may inject incorrect static information and cause miscompilation. If an incorrect predicate is attached to an SSA value, the behavior of the program is undefined.

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:312-319](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L312-L319) 声明 `assume` 不检查谓词正确性，错误谓词导致未定义行为。

谓词接口 `AssumePredicateAttrInterface` 定义在 Interfaces.td：

[include/cuda_tile/Dialect/CudaTile/IR/Interfaces.td:16-31](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Interfaces.td#L16-L31) 定义接口，只有一个方法 `verifyWithAssumeOp`。

```tablegen
let methods = [
  InterfaceMethod<[{
      Verifies this attribute in the context of the given `cuda_tile.assume`
      op. Returns "success" if the attribute is semantically valid on the op
      and "failure" otherwise.
    }],
    "LogicalResult", "verifyWithAssumeOp", (ins "::mlir::Operation *":$op)>
];
```

`assume` 的 C++ verifier 正是把工作委派给这个接口方法：

[lib/Dialect/CudaTile/IR/CudaTile.cpp:1464-1466](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1464-L1466) `AssumeOp::verify()` 仅调用 `predicate.verifyWithAssumeOp(getOperation())`。

```cpp
LogicalResult AssumeOp::verify() {
  return getPredicate().verifyWithAssumeOp(getOperation());
}
```

> 注意：`predicate` 参数的类型约束是 `CudaTile_AssumePredicateAttrInterface`，这意味着 **只有实现了该接口的属性才能作为谓词**。若传入普通整数属性，校验器会报 `expected assume predicate attribute`（见 invalid.mlir 的对应用例）。

#### 4.1.4 代码实践

**实践目标**：亲手体验 `assume` 的「透传 + 不检查」语义，并触发一次谓词类型错误。

**操作步骤**：

1. 创建文件 `assume_basic.mlir`：

   ```mlir
   cuda_tile.module @kernels {
     cuda_tile.entry @basic(%arg0: !cuda_tile.tile<16xi32>) {
       // 透传：result 类型与 %arg0 完全一致，仅附加 div_by 谓词
       %0 = cuda_tile.assume #cuda_tile.div_by<16>, %arg0 : tile<16xi32>
       cuda_tile.return
     }
   }
   ```

2. 用 `cuda-tile-opt` 跑一遍，观察 round-trip（解析后再打印，格式不变）：

   ```bash
   cuda-tile-opt assume_basic.mlir
   ```

3. 故意把谓词换成一个普通整数属性，触发接口校验错误：

   ```mlir
   cuda_tile.entry @bad(%arg0: !cuda_tile.tile<f32>) {
     // 错误：i32 不是 AssumePredicateAttrInterface 属性
     cuda_tile.assume 32 : i32, %arg0 : !cuda_tile.tile<f32>
   }
   ```

**需要观察的现象**：第 2 步输出与输入基本一致（结果名可能被规范化为 `assume_...`）；第 3 步报错 `expected assume predicate attribute`。

**预期结果**：`assume` 不改变类型、不产生指令；谓词必须是接口实现类。该错误用例对应真实测试 [test/Dialect/CudaTile/invalid.mlir:1136-1137](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/invalid.mlir#L1136-L1137)。运行命令的具体结果「待本地验证」（依赖你已按 u1-l2 构建出 `cuda-tile-opt`）。

#### 4.1.5 小练习与答案

**练习 1**：`assume` 操作的结果类型与输入类型有何关系？为什么？

**参考答案**：完全相同。`assume` 用 `AllTypesMatch<["value", "result"]>` 强制二者一致，因为它只是把 `value` 透传出来并贴上谓词标签，不做任何计算或类型转换。

**练习 2**：为什么 `assume` 的 verifier 几乎是空的？谓词的合法性校验在哪里完成？

**参考答案**：因为校验逻辑被下放到谓词属性自身。`assume` 的 verifier 只调用 `predicate.verifyWithAssumeOp(this)`，由每个具体谓词（`div_by`/`same_elements`/`bounded`）的 `verifyWithAssumeOp` 实现来决定它对哪类值合法。这是 `AssumePredicateAttrInterface` 接口带来的解耦。

### 4.2 div_by 谓词：整除性与分组对齐

#### 4.2.1 概念说明

`div_by` 是最常用的谓词，表达「某些元素（或地址）能被 `divisor` 整除」。它的典型用途是**地址对齐假设**：告诉编译器一组指针按 16/32/64 字节对齐，从而可以使用向量化加载或 TMA（Tensor Memory Accelerator）。

它有三种参数：

- `divisor`（必填）：必须是正的 2 的幂（power of 2）。如 `<16>` 表示「能被 16 整除」。
- `every` / `along`（可选，但必须成对出现）：表达「分组」——沿 `along` 维按大小 `every` 切组，**只有每组第一个元素**满足整除性，组内其余元素按某种单调规律递增（整数 tile 递增 1，指针 tile 按 pointee 字节宽度递增）。

`div_by` 可作用于三类值：

- **整数 tile**（如 `tile<16xi32>`）：假设元素的整数值能被 `divisor` 整除。
- **指针 tile**（如 `tile<Nxptr<f32>>`）：假设指针地址能被 `divisor` 整除。
- **tensor_view**（如 `tensor_view<...>`）：假设其**基地址**能被 `divisor` 整除，且此时**不允许**用 `every`/`along`。

#### 4.2.2 核心流程

```
div_by< divisor (, every E along D)? >

校验流程 (verifyWithAssumeOp):
  1. divisor 必须是正的 2 的幂，且 ≤ 2^62
  2. every / along 必须同时出现或同时缺失
  3. 分情况:
     - tensor_view: 不允许 every/along, 只校验基地址整除性语义  → OK
     - 0D tile:     不允许 every/along
     - 普通tile:    元素必须是 IntegerType 或 PointerType
  4. 若带 every/along:
     - 0 ≤ along < rank
     - 0 ≤ every ≤ dimSize[along]
```

一个关键直觉：当**不写** `every`/`along` 时，等价于 `every=1, along=0`，即「tile 中**每一个**元素都满足整除性」。这是最常见的「全部对齐」假设。

> 关于 `every` 的有符号性细节：若 tile 元素是整数，`every` 描述的是**有符号**解释下的等差数列；否则是无符号解释。文档特别提醒，对于 `i8` 序列 `[01111110, 01111111, 10000000, 10000001]`，由于有符号解释下 `10000000` 是负数导致回绕，`every=4` 是**错误**的，`every=2` 才正确。

#### 4.2.3 源码精读

`DivByAttr` 定义在 AttrDefs.td：

[include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td:282-385](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L282-L385) 定义 `div_by` 谓词属性，声明实现 `AssumePredicateAttrInterface`。

其参数为三个：

```tablegen
let parameters = (ins "uint64_t":$divisor,
                      "std::optional<int64_t>":$every,
                      "std::optional<int64_t>":$along);
```

[include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td:374-376](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L374-L376) `div_by` 的三个参数：`divisor` 与可选的 `every`/`along`。

`every`/`along` 用 `std::optional` 表示「可能缺失」，正是因为当前 MLIR 对可选参数的汇编格式支持有限，该项目改用手写的 `parse`/`print`（注释 `TODO: Specify assembly format instead of hand-written parsers/printers`）。

校验实现的核心在 Attributes.cpp。先看「2 的幂」与「成对」校验：

[lib/Dialect/CudaTile/IR/Attributes.cpp:422-438](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L422-L438) 校验 divisor 是正 2 的幂、`every`/`along` 成对、`divisor` 不超过上限。

```cpp
uint64_t divisor = getDivisor();
bool isPowerOfTwo = divisor > 0 && ((divisor & (divisor - 1)) == 0);
if (!isPowerOfTwo)
  return op->emitOpError() << "..." << " divisor must be a power of 2";

if (!llvm::all_equal({getEvery().has_value(), getAlong().has_value()}))
  return op->emitOpError() << "..." << " 'every'/'along' must be used in combination";
```

注意位运算技巧 `(divisor & (divisor - 1)) == 0`：只有 2 的幂的二进制只有一个 1，减 1 后会把那个 1 借位，二者按位与为 0。这是判断 2 的幂的标准写法。

接着是分情况的类型校验：tensor_view 不允许 every/along；普通 tile 必须是整数或指针；0D tile 不允许 every/along：

[lib/Dialect/CudaTile/IR/Attributes.cpp:440-464](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L440-L464) 分情况校验 tensor_view / 0D tile / 普通 tile 的元素类型。

最后校验 `along` 与 `every` 的取值范围：

[lib/Dialect/CudaTile/IR/Attributes.cpp:466-478](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L466-L478) 校验 `0 ≤ along < rank` 且 `0 ≤ every ≤ dimSize[along]`。

`div_by` 的真实测试用例很丰富。ops.mlir 里的合法用例展示了整数 tile、指针 tile、tensor_view 与带分组等多种形态：

[test/Dialect/CudaTile/ops.mlir:1032-1043](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir#L1032-L1043) `div_by` 的 round-trip 合法用例。

非法用例覆盖了每一条校验规则（非 2 的幂、divisor 过大、0D tile 用 every、tensor_view 用 every、every 超维、along 超秩、浮点 tile 等）：

[test/Dialect/CudaTile/invalid.mlir:1009-1065](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/invalid.mlir#L1009-L1065) `div_by` 的非法用例与期望报错。

#### 4.2.4 代码实践

**实践目标**：对一个整数指针 tile 假设「按 16 字节对齐」，并验证合法/非法写法。

**操作步骤**：

1. 创建 `assume_div_by.mlir`：

   ```mlir
   cuda_tile.module @kernels {
     // 指针 tile：假设每个指针按 16 字节对齐
     cuda_tile.entry @aligned_ptrs(%ptrs: !cuda_tile.tile<8x!cuda_tile.ptr<f32>>) {
       %0 = cuda_tile.assume #cuda_tile.div_by<16>, %ptrs : tile<8xptr<f32>>
       cuda_tile.return
     }
     // 分组：沿 dim 1 每 4 个一组，组首对齐 4 的倍数
     cuda_tile.entry @grouped(%t: !cuda_tile.tile<4x8xi32>) {
       %0 = cuda_tile.assume #cuda_tile.div_by<4, every 4 along 1>, %t : tile<4x8xi32>
       cuda_tile.return
     }
   }
   ```

2. 运行并观察输出：

   ```bash
   cuda-tile-opt assume_div_by.mlir
   ```

3. 尝试一个非法写法（divisor 非 2 的幂），观察报错：

   ```mlir
   cuda_tile.entry @bad(%t: !cuda_tile.tile<16xi32>) {
     // 错误：7 不是 2 的幂
     cuda_tile.assume #cuda_tile.div_by<7>, %t : !cuda_tile.tile<16xi32>
   }
   ```

**需要观察的现象**：第 2 步 round-trip 成功，`div_by<16>` 与 `div_by<4, every 4 along 1>` 均被保留；第 3 步报 `'cuda_tile.div_by' divisor must be a power of 2`。

**预期结果**：合法谓词通过；非法 divisor 被拒。具体运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`div_by<16>` 作用在 `tile<8xptr<f32>>` 上，表达了什么假设？等价的带 `every`/`along` 写法是什么？

**参考答案**：假设这 8 个指针的地址都能被 16 整除（16 字节对齐）。等价写法是 `div_by<16, every 1 along 0>`（不写时默认 `every=1, along=0`，即每个元素都满足）。

**练习 2**：为什么对 `tensor_view` 不允许使用 `every`/`along`？

**参考答案**：`div_by` 对 `tensor_view` 只假设其**基地址**能被 `divisor` 整除（一个标量性质），没有「逐元素分组」的概念，因此 `every`/`along` 无意义，校验器在 [Attributes.cpp:443-446](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L443-L446) 直接拒绝。

### 4.3 same_elements 谓词：同元素重复模式

#### 4.3.1 概念说明

`same_elements` 表达「沿某些维度，元素以固定大小的块重复」。它的典型场景是：当多个线程/通道实际上读取相同的值时，告诉编译器「这几路其实相等」，从而合并广播、消除冗余加载。

它对 tile 的**每一维**各给一个组大小 `C`，含义是：把该维（大小 `N`）切成 `N/C` 个大小为 `C` 的组，**每组内部元素都相同**。若某维不满足该性质，就填 `1`（大小为 1 的组天然「元素相同」）。

> `#same_elements<[1,1,...,1]>`（1 的个数等于秩）对**任意**整数/指针 tile 都永远成立——这是一个「安全」的默认谓词。

`same_elements` 只作用于**整数或指针 tile**（不能用于 `tensor_view`，也不能用于浮点 tile）。

#### 4.3.2 核心流程

```
same_elements< [c0, c1, ..., c_{rank-1}] >

校验流程:
  1. 值必须是 TileType, 元素必须是 IntegerType 或 PointerType
  2. values 数组长度必须 == tile 的秩 (rank)
  3. 对每一维 i: 0 ≤ values[i] ≤ dimSize[i]
```

例：`tile<4x8xi16>` 上 `same_elements<[2, 4]>` 表示 dim0 每 2 个一组同值、dim1 每 4 个一组同值。

#### 4.3.3 源码精读

[include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td:387-437](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L387-L437) 定义 `same_elements` 谓词，参数是 `DenseI64ArrayAttr`。

```tablegen
let parameters = (ins "DenseI64ArrayAttr":$values);
let assemblyFormat =  "`<` $values `>`";
```

注意它**用了自动生成的汇编格式**（`` `<` $values `>` ``），所以语法是 `same_elements<[2, 4]>`。对 0D tile（标量 tile）则是空数组 `same_elements<[]>`，这能在 operationsTest.mlir 里看到。

校验实现：

[lib/Dialect/CudaTile/IR/Attributes.cpp:522-547](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L522-L547) 校验 `same_elements`：元素类型、数组长度与秩一致、每组大小在维度范围内。

```cpp
if (getValues().size() != tileType.getRank())
  return op->emitOpError() << "expected number of values in '" << name
      << "' (" << getValues().size() << ") to match rank of constrained tile ("
      << tileType.getRank() << ")";
for (int64_t i = 0, e = tileType.getRank(); i < e; ++i) {
  if (getValues()[i] < 0 || getValues()[i] > tileType.getDimSize(i))
    return op->emitOpError() << "expected '" << name << "' value " << i ...;
}
```

合法用例见 [test/Dialect/CudaTile/ops.mlir:1045-1048](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir#L1045-L1048)，非法用例（长度不匹配、浮点 tile、组大小超维）见 [test/Dialect/CudaTile/invalid.mlir:1072-1092](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/invalid.mlir#L1072-L1092)。

#### 4.3.4 代码实践

**实践目标**：为一个「相邻元素相同」的整数 tile 附加 `same_elements` 谓词。

**操作步骤**：

1. 创建 `assume_same.mlir`（参考 AttrDefs.td 中给出的标准示例）：

   ```mlir
   cuda_tile.module @kernels {
     cuda_tile.entry @same(%arg0: !cuda_tile.tile<4x8xi16>) {
       // 假设 dim0 每 2 个、dim1 每 4 个元素各自相同
       %0 = cuda_tile.assume #cuda_tile.same_elements<[2, 4]>, %arg0 : tile<4x8xi16>
       cuda_tile.return
     }
   }
   ```

2. 运行 `cuda-tile-opt assume_same.mlir`。

**需要观察的现象**：谓词被原样保留；若把数组长度改成 `[2]`（与秩 2 不匹配），会报 `expected number of values ... to match rank`。

**预期结果**：合法则通过，长度不匹配则报错。具体运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：对一个 `tile<16xi32>`（秩 1）使用 `same_elements`，合法的「最保守」写法是什么？

**参考答案**：`same_elements<[1]>`。大小为 1 的组天然满足「元素相同」，因此对任意整数 tile 都成立，是最安全的不施加实际约束的谓词。

**练习 2**：为什么 `same_elements` 不能用于浮点 tile？

**参考答案**：其校验器 [Attributes.cpp:529-533](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L529-L533) 要求元素类型是 `IntegerType` 或 `PointerType`，浮点 tile 会被拒绝（报 `'cuda_tile.same_elements' is valid only for tile of integer/pointer values`）。

### 4.4 bounded 谓词：值域上下界

#### 4.4.1 概念说明

`bounded` 表达「tile 中所有元素（按有符号解释）都落在 `[lb, ub]` 闭区间内」。它的典型用途是范围约束：告诉编译器索引/计数值很小，从而选用更紧凑的编码或避免溢出处理。

上下界都是**可选**的：用 `?` 表示「该侧不约束」。因此有四种组合：

- `bounded<0, 42>`：元素 ∈ [0, 42]
- `bounded<?, 42>`：只约束上界
- `bounded<-4, ?>`：只约束下界
- `bounded<?, ?>`：两侧都不约束（几乎无意义，但语法合法）

约束规则：

- 只作用于**整数 tile**（注意：**不能用于指针 tile**，这是它与 `div_by`/`same_elements` 的关键区别）。
- 给定的 `lb`/`ub` 必须落在该整数位宽的有符号范围内（如 `i8` ∈ [-128, 127]）。
- 若同时给出 `lb` 和 `ub`，必须 `lb ≤ ub`。

#### 4.4.2 核心流程

```
bounded< (lb|?) , (ub|?) >

校验流程:
  1. 值必须是 TileType, 元素必须是 IntegerType (不允许指针!)
  2. 计算 intType 位宽对应的 [minSigned, maxSigned]
  3. 若给定 lb: minSigned ≤ lb ≤ maxSigned
  4. 若给定 ub: minSigned ≤ ub ≤ maxSigned
  5. 若 lb 与 ub 都给定: lb ≤ ub
```

#### 4.4.3 源码精读

[include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td:439-471](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L439-L471) 定义 `bounded` 谓词，两个 `OptionalParameter`，汇编格式用 `?` 表示缺失。

```tablegen
let parameters = (ins OptionalParameter<"std::optional<int64_t>">:$lb,
                      OptionalParameter<"std::optional<int64_t>">:$ub);
let assemblyFormat = `{` `<` ($lb^) : (`?`)? `,` ($ub^) : (`?`)? `>` `}`;
```

汇编格式 `($lb^) : (\`?\`)?` 的含义是：「优先尝试解析实际参数 `lb`，失败则解析字面量 `?`」。这正是 `?` 缺省语法的来源。

校验实现：

[lib/Dialect/CudaTile/IR/Attributes.cpp:549-574](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L549-L574) 校验 `bounded`：仅整数、上下界在位宽范围内、`lb ≤ ub`。

```cpp
int64_t minVal = getMinSignedValueForBitwidth(intType.getWidth());
int64_t maxVal = getMaxSignedValueForBitwidth(intType.getWidth());
if (getLb().has_value() && (*getLb() > maxVal || *getLb() < minVal))
  return op->emitOpError() << "..." << " expects lower bound to be within [" << minVal << ", " << maxVal << "]";
...
if (getLb().has_value() && getUb().has_value() && *getLb() > *getUb())
  return op->emitOpError() << "..." << " expects lower bound to be less than or equal to upper bound";
```

注意 `getMinSignedValueForBitwidth`/`getMaxSignedValueForBitwidth` 是 MLIR 提供的工具，按位宽算出有符号极值（如 i8 → -128/127）。

合法用例（含 `?` 四种组合）见 [test/Bytecode/operationsTest.mlir:68-75](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/operationsTest.mlir#L68-L75)，非法用例（浮点 tile、界超范围、`lb > ub`）见 [test/Dialect/CudaTile/invalid.mlir:1099-1128](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/invalid.mlir#L1099-L1128)。

#### 4.4.4 代码实践

**实践目标**：为一个整数 tile 同时附加 `bounded` 下界与上界，并触发一次界超范围错误。

**操作步骤**：

1. 创建 `assume_bounded.mlir`：

   ```mlir
   cuda_tile.module @kernels {
     // 假设所有 i16 值 ∈ [5, +∞)
     cuda_tile.entry @ge5(%arg0: !cuda_tile.tile<8xi16>) {
       %0 = cuda_tile.assume #cuda_tile.bounded<5, ?>, %arg0 : tile<8xi16>
       cuda_tile.return
     }
   }
   ```

2. 运行 `cuda-tile-opt assume_bounded.mlir`。

3. 对 `tile<8xi8>` 故意写 `bounded<0, 128>`（128 超出 i8 上界 127），观察报错。

**需要观察的现象**：第 2 步通过；第 3 步报 `'cuda_tile.bounded' expects upper bound to be within [-128, 127]`。

**预期结果**：界在范围内则通过，越界则被拒。具体运行结果「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`bounded` 能否用于指针 tile？为什么它与 `div_by` 不同？

**参考答案**：不能。`bounded` 校验器 [Attributes.cpp:555-558](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L555-L558) 要求元素是 `IntegerType`；指针的「数值」语义（地址）不参与有符号范围推理。而 `div_by`/`same_elements` 都允许指针元素。

**练习 2**：对 `tile<8xi8>`，`bounded<-129, 6>` 为什么非法？

**参考答案**：i8 的有符号下界是 -128，-129 超出该范围，校验器报 `'cuda_tile.bounded' expects lower bound to be within [-128, 127]`。

### 4.5 谓词的解析/打印与 assume 的规范化（fold）

#### 4.5.1 概念说明

两个值得了解的工程细节：

**简写语法**：在 `assume` 操作里，谓词可以省略 `#cuda_tile.` 前缀，直接写 `div_by<16>` 而非 `#cuda_tile.div_by<16>`。这是因为 `assume` 用了自定义的解析/打印（`custom<AssumePredicate>`），让常见写法更简洁。

**规范化折叠**：当两个 `assume` 操作串联、且谓词**完全相同**时，fold 会把后者折叠掉，直接引用前者的结果。例如 `%y = assume div_by<16>, %x` 紧接 `%z = assume div_by<16>, %y`，后者冗余，会被消除。但**不同谓词**的串联（如先 `div_by<16>` 再 `div_by<8>`）不会折叠——因为它们携带不同的信息。

#### 4.5.2 核心流程

解析（parse）优先尝试完整属性语法（`#cuda_tile.div_by<...>`），失败再尝试简写关键字 `div_by`/`same_elements`/`bounded`：

```
parseAssumePredicate:
  1. 尝试 parseOptionalAttribute → 若成功且是 AssumePredicateAttrInterface, 接受
  2. 否则读关键字 attrName:
       "div_by"        → DivByAttr::parse
       "same_elements" → SameElementsAttr::parse
       "bounded"       → BoundedAttr::parse
       其它           → 报 "unknown assume predicate attribute"
```

fold 逻辑：

```
AssumeOp::fold:
  if (value 的定义操作也是 AssumeOp) 且 (二者 predicate 相同):
      返回前一个 assume 的结果   ← 消除冗余
  否则:
      不折叠
```

#### 4.5.3 源码精读

[lib/Dialect/CudaTile/IR/CudaTile.cpp:938-991](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L938-L991) `parseAssumePredicate`：先试完整属性语法，再分派到三种谓词的 `parse`。

打印对应 [lib/Dialect/CudaTile/IR/CudaTile.cpp:993-1010](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L993-L1010)，它把属性打印成字符串后剥掉 `#cuda_tile.` 前缀，从而得到简写形式。

fold 实现：

[lib/Dialect/CudaTile/IR/CudaTile.cpp:1484-1490](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1484-L1490) `AssumeOp::fold`：当上游也是同谓词 `assume` 时返回上游结果。

```cpp
OpFoldResult AssumeOp::fold(FoldAdaptor adaptor) {
  if (auto producerOp = this->getValue().getDefiningOp<AssumeOp>()) {
    if (producerOp.getPredicate() == this->getPredicate())
      return producerOp.getResult();
  }
  return {};
}
```

对应的真实 fold 测试用例（注意 `CHECK-NOT` 断言「不存在」被折叠掉的冗余 assume）：

[test/Dialect/CudaTile/canonicalize.mlir:1093-1119](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/canonicalize.mlir#L1093-L1119) 连续同谓词 `assume` 的折叠用例。

可以看到：连续两个 `div_by<16>` 只保留一个；连续三个 `bounded<?, 42>` 只保留一个；而 `div_by<16>` 之后接 `div_by<8>`（不同谓词）则**都保留**。

#### 4.5.4 代码实践

**实践目标**：用 `--canonicalize` 观察 assume 链的折叠行为。

**操作步骤**：

1. 创建 `assume_fold.mlir`：

   ```mlir
   cuda_tile.module @kernels {
     cuda_tile.entry @fold(%arg0: !cuda_tile.tile<ptr<f32>>) -> !cuda_tile.tile<ptr<f32>> {
       %a = assume div_by<16>, %arg0 : tile<ptr<f32>>
       // 与上一条谓词相同 → 应被折叠掉
       %b = assume div_by<16>, %a : tile<ptr<f32>>
       // 谓词不同 → 应保留
       %c = assume div_by<8>, %b : tile<ptr<f32>>
       return %c : tile<ptr<f32>>
     }
   }
   ```

2. 运行规范化并查看输出：

   ```bash
   cuda-tile-opt assume_fold.mlir --canonicalize
   ```

**需要观察的现象**：输出里只剩**两个** `assume`（一个 `div_by<16>`、一个 `div_by<8>`），中间冗余的 `div_by<16>` 被消除，`%c` 直接引用第一个 assume 的结果。

**预期结果**：相同谓词的连续 assume 被合并为一个。具体运行结果「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`assume div_by<16>, ... ; assume div_by<8>, ...`（同 tile 上先后两个不同谓词）会被折叠吗？

**参考答案**：不会。fold 只在**谓词完全相同**时才折叠（见 [CudaTile.cpp:1486](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1486) 的 `producerOp.getPredicate() == this->getPredicate()`）。`div_by<16>` 与 `div_by<8>` 携带不同信息，都需保留。

**练习 2**：为什么 `assume` 的汇编格式允许省略 `#cuda_tile.` 前缀？

**参考答案**：因为 `assume` 用 `custom<AssumePredicate>` 自定义了解析/打印。解析器先尝试完整属性语法，失败再识别 `div_by`/`same_elements`/`bounded` 关键字并调用各谓词的 `parse`；打印机则把属性字符串的 `#cuda_tile.` 前缀剥掉。这是为可读性做的语法糖，语义完全等价。

## 5. 综合实践

把本讲的三个谓词与 u5-l1 的访存串起来，完成一个「为访存提供对齐与范围假设」的小任务。

**任务**：编写一个 entry 内核，接收一个指针 tile，依次完成：

1. 用 `assume` + `div_by<16>` 声明该指针 tile 按 16 字节对齐。
2. 用 `offset` 构造一组偏移地址（参考 u4-l1 / u3-l2 的地址构造链），偏移量来自一个 `tile<8xi32>`。
3. 用 `assume` + `bounded<0, 128>` 声明这些偏移量 ∈ [0, 128]。
4. 用 `load_ptr_tko` 读取数据。

参考骨架（需要你补全类型与对齐假设）：

```mlir
cuda_tile.module @kernels {
  cuda_tile.entry @load_aligned(%base: !cuda_tile.tile<!cuda_tile.ptr<f32>>,
                                %offs: !cuda_tile.tile<8xi32>,
                                %mask: !cuda_tile.tile<8xi1>) {
    // 1. 假设 base 按 16 字节对齐
    %aligned = assume div_by<16>, %base : tile<ptr<f32>>
    // 3. 假设偏移 ∈ [0, 128]
    %bounded = assume bounded<0, 128>, %offs : tile<8xi32>
    // 2. 构造偏移地址（你需要根据 u4-l1 的 offset 语义补全）
    // %ptrs = offset %aligned, %bounded : ...
    // 4. 带掩码加载（参考 u5-l1）
    // %data, %tok = load_ptr_tko %ptrs, mask %mask : ...
    return
  }
}
```

**操作步骤**：

1. 按 u4-l1 的 `offset` 操作语义补全指针偏移，按 u5-l1 的 `load_ptr_tko` 语义补全加载。
2. 用 `cuda-tile-opt assume_combined.mlir` 验证整体合法性。
3. 实验对比：分别删除第 1 步、第 3 步的 `assume`，观察 `cuda-tile-opt` 解析/校验是否仍通过（应当通过，因为 assume 是可选优化信息），并思考编译器失去了哪些优化机会。

**Python 版本**（结合代码实践任务的第二部分）：

```python
from cuda_tile.dialects import cuda_tile as ct
from cuda_tile.dialects.cuda_tile_ops import (
    assume_div_by, assume_bounded, make_tile_type
)

# 构造 tile<8xptr<f32>> 与 tile<8xi32>
ptr_ty = ct.PointerType.get(ct._ods_ir.F32Type.get())
# ... 用 make_tile_type 构造 tile 类型（见 u3-l2）...

# div_by: 内部把 16 拼成 "#cuda_tile.div_by<16>" 再 Attribute.parse
# bounded: 内部把 lb/ub 拼成 "#cuda_tile.bounded<0, ?>"
```

**需要观察的现象与预期结果**：

- 完整版应能通过 `cuda-tile-opt` 校验。
- 阅读 [python/cuda_tile/dialects/cuda_tile_ops.py:1959-2011](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L1959-L2011)，理解 Python 端 `assume_div_by`/`assume_bounded` 因为「暂无 div_by/bounded 的专门 Python 绑定」，采用**拼接文本属性字符串再 `Attribute.parse`** 的变通实现（代码里的 `TODO` 注释明确说明了这一点）。

具体运行结果「待本地验证」。

## 6. 本讲小结

- `cuda_tile.assume` 是一个**零运行时开销**的「贴标签」操作：它原样透传输入值（`AllTypesMatch<["value","result"]>`），仅把一条谓词属性附加到结果上，供编译器生成更优代码。它**不检查谓词正确性**，错误假设会导致未定义行为。
- `AssumePredicateAttrInterface` 是连接 `assume` 与各谓词的契约，只有一个方法 `verifyWithAssumeOp`。`assume` 的 verifier 把全部合法性校验委派给谓词自身，实现解耦。
- 三种谓词各有适用范围与约束：
  - `div_by`：整除性（divisor 必须为正 2 的幂），支持整数 tile、指针 tile、tensor_view，可选 `every`/`along` 分组（必须成对，且对 tensor_view / 0D tile 禁用）。
  - `same_elements`：逐维同元素组大小，仅整数/指针 tile，数组长度须等于秩。
  - `bounded`：有符号值域 `[lb,ub]`（两侧均可用 `?` 省略），**仅整数 tile**（不支持指针），上下界须在位宽范围内且 `lb ≤ ub`。
- 谓词可用简写语法（`div_by<16>` 而非 `#cuda_tile.div_by<16>`），由 `parseAssumePredicate`/`printAssumePredicate` 实现。
- `assume` 的 fold 会消除**相同谓词**的连续冗余链（不同谓词不折叠），规范化测试在 canonicalize.mlir 中有完整覆盖。
- Python 端 `assume_div_by`/`assume_same_elements`/`assume_bounded` 因缺少专门绑定，采用拼接文本属性字符串再 `Attribute.parse` 的变通实现。

## 7. 下一步学习建议

- **继续属性与语义方向**：阅读 u6-l3「调试信息属性与位置」，了解 `DICompileUnit`/`DIFile`/`DISubprogram` 等属性如何像谓词一样经 `.td` 定义并通过 `verifyFuncDebugInfo` 校验，巩固「属性 + 接口 + 校验」的设计范式。
- **回到内存主线**：把本讲的 `div_by` 对齐假设与 u5-l2 的视图访存结合，思考 `tensor_view` 的 `div_by` 基地址假设如何配合 TMA 加载（见 u6-l1 的 `allow_tma` 优化提示）。
- **深入源码**：若想新增一种谓词，需要 (1) 在 AttrDefs.td 用 `CudaTileAttrDef` 声明并 `DeclareAttrInterfaceMethods<CudaTile_AssumePredicateAttrInterface>`，(2) 在 Attributes.cpp 实现 `verifyWithAssumeOp`，(3) 若需要简写语法则在 `parseAssumePredicate`/`printAssumePredicate` 加上分派分支。这可作为学习 u8 章节字节码/规范代码生成的练手入口。
