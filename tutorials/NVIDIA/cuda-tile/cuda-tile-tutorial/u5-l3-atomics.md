# 原子操作与内存序/作用域

## 1. 本讲目标

在上一讲（u5-l2）我们学会了用「视图 + 索引」从全局显存整块读写 tile。但当很多线程（或很多 tile 块）要**同时更新同一块显存**时——比如分布式训练里把各路梯度累加到同一个参数缓冲区——普通的 `load`/`store` 会产生数据竞争（data race），结果不确定。CUDA Tile 方言用一组 **Atomics（原子）操作**来解决这个问题。

本讲聚焦 Atomics 分组的三个操作：`atomic_rmw_tko`、`atomic_cas_tko`、`atomic_red_view_tko`。读完本讲，你应当能够：

1. 说出原子「读-改-写」（read-modify-write，RMW）事务为什么是不可分割的，以及它返回什么。
2. 写出三个原子操作各自的合法 MLIR 写法，知道它们各自的指针/视图输入、`mode`、可选 `mask`/`token`。
3. 解释 `MemoryOrderingSemantics`（weak/relaxed/acquire/release/acq_rel）与 `MemoryScope`（tl_blk/device/sys）如何共同描述「和谁同步、同步多强」。
4. 理解 `atomic_red_view_tko` 为什么**不返回旧值**，以及它为 TMA 硬件优化的额外限制（只允许 relaxed、只允许 tl_blk/device、禁止 xchg）。
5. 看懂 `.td` 里 `OnlyVariants`、C++ `verify()`、Python 三层校验是如何对同一个约束做「多重保险」的。

## 2. 前置知识

本讲承接 u5-l1（内存模型与 Token 顺序）与 u5-l2（视图加载与存储），请确认你已经掌握：

- **指针 tile 与 `offset`**：`tile<Nxptr<T>>` 是一组全局显存地址，`offset` 按元素位宽把整数偏移换算成地址增量（见 u3-l2、u4-l1）。
- **Token 顺序（`_tko` 后缀）**：名字带 `_tko` 的操作默认**不受程序顺序约束**，编译器可自由重排；要排序只能用 token 显式串接（u5-l1）。
- **内存序与作用域**：u5-l1 已经在 `load_ptr_tko`/`store_ptr_tko` 上引入了 `memory_ordering_semantics` 与 `memory_scope` 两个属性，本讲会复用这两个概念，并指出原子操作对它们的取值集合**不同**。
- **视图族**：`tensor_view` → `partition_view`/`strided_view`/`gather_scatter_view` 的几何关系，以及 `TileView` 接口（u3-l3、u5-l2）。

一个**新的**核心概念是**原子事务（atomic transaction）**。普通 store 是「写就完了」，而原子操作把「读旧值、算新值、写回」打包成一个对其他线程**不可分割**的整体：在它执行期间，没有别的线程能插进来改这个地址。形式上，对每一个 `(pointer, arg)` 元组，原子 RMW 等价于一段加锁的伪代码：

```
atomic {
  x = *pointer      // 读旧值
  y = mode(x, arg)  // 用 mode 计算新值
  *pointer = y      // 写回
  return x          // 返回旧值
}
```

「原子」保证这段伪代码对任意单个 `(pointer, arg)` 是整体完成的，从而把可能的数据竞争变成确定的结果。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/cuda_tile/Dialect/CudaTile/IR/Ops.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td) | 三个原子操作的声明：操作数/结果、汇编格式、`mlirExamples`、`OnlyVariants` 限定。 |
| [include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td) | 三个关键属性：`AtomicRMWModeAttr`、`MemoryScopeAttr`、`MemoryOrderingSemanticsAttr`。 |
| [include/cuda_tile/Dialect/CudaTile/IR/Dialect.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td) | `CudaTileAtomicsOpDef` 基类，把三个操作归入 `Atomics` 分组。 |
| [include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h) | 跨操作复用的校验工具 `verifyAtomicRMWMode`、`verifyAtomicMemoryOrdering`。 |
| [include/cuda_tile/Dialect/CudaTile/IR/Ops.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.h) | `verifyMemoryModelLoad`/`verifyMemoryModelStore` 的声明（load/store 的内存模型校验）。 |
| [lib/Dialect/CudaTile/IR/CudaTile.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp) | 三个操作各自的 `verify()` 实现，以及 `verifyMemoryModelLoad/Store` 实现。 |
| [test/Dialect/CudaTile/ops.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir) | 三个原子操作的合法（正例）测试。 |
| [test/Dialect/CudaTile/invalid.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/invalid.mlir) | 各类非法（反例）测试与期望报错信息。 |
| [python/cuda_tile/dialects/cuda_tile_ops.py](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py) | `atomic_rmw_tko`/`atomic_cas_tko`/`atomic_red_view_tko` 的高层 Python 包装与提前校验。 |

## 4. 核心概念与源码讲解

### 4.1 三大属性：RMW 模式、内存序、作用域

#### 4.1.1 概念说明

三个原子操作都靠三个属性来精确描述「**做什么运算**」和「**和谁、用什么强度同步**」：

- **`mode`（`AtomicRMWModeAttr`）**：读-改-写里那个 `mode(x, arg)` 用什么运算。取值有 `and/or/xor/add/addf/max/min/umax/umin/xchg` 共 10 种，默认值是 `add`。它决定了**接受什么元素类型**：整数运算（`add/and/or/xor/max/min/umax/umin`）只认 i32/i64；`addf` 只认浮点 f16/bf16/f32/f64；`xchg`（exchange，交换）只认 32 或 64 位的整数或浮点。
- **`memory_ordering_semantics`（`MemoryOrderingSemanticsAttr`）**：描述这次访问与其它线程的访问之间建立多强的同步关系。五个取值：`weak`（无并发）、`relaxed`（有并发但不建立 happens-before）、`acquire`/`release`（配对可建立 happens-before）、`acq_rel`（同时具有 release 与 acquire 效果）。**注意：原子操作不允许 `weak`**——因为原子操作天生就是为并发访问准备的。
- **`memory_scope`（`MemoryScopeAttr`）**：这次同步要覆盖多大的范围。三个取值：`tl_blk`（同一个 tile 块内并发）、`device`（同一块 GPU 内并发）、`sys`（整个系统、跨设备并发）。范围要「大到能涵盖所有参与通信的线程」，否则仍有数据竞争。

> 与 u5-l1 的对照：`load_ptr_tko` 允许 weak/relaxed/acquire，`store_ptr_tko` 允许 weak/relaxed/release；而**三个原子操作允许 relaxed/acquire/release/acq_rel**（不含 weak），且 `memory_scope` 是**必填**位置操作数。这正是下面 4.5 节校验逻辑的关键差异。

#### 4.1.2 核心流程

属性的取值在 `.td` 里用 `CudaTileI32EnumAttr` + 一串 `CudaTileI32EnumAttrCase` 列出，每个 case 带一个助记符字符串（如 `"addf"`）作为 MLIR 文本里的打印形式。`OnlyVariants<[...]>` 还能在**单个操作**上把可选取值再收窄。整体流程是：

1. 解析器读到文本里的 `relaxed`/`device`/`addf` 等关键字。
2. 查枚举表，转成内部 I32 值，挂到操作的对应属性上。
3. 打印时反向用助记符输出。
4. 校验阶段再检查「这个操作是否允许这个取值」。

#### 4.1.3 源码精读

`AtomicRMWModeAttr` 定义在 AttrDefs.td，列出 10 个模式及其助记符：

[AttrDefs.td:263-280](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L263-L280) —— 定义 `AtomicRMWModeAttr`，注意 `specSuffixDescription` 写明 `mode` 的默认值是 `add`。

`MemoryScopeAttr` 三个作用域，描述里点明「范围必须大到涵盖所有参与通信的线程，否则数据竞争」：

[AttrDefs.td:473-485](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L473-L485) —— 定义 `MemoryScopeAttr`（tl_blk=0 / device=1 / sys=2）。

`MemoryOrderingSemanticsAttr` 五个内存序，其中 `weak` 表示「假设没有并发访问」，这正是原子操作排除它的原因：

[AttrDefs.td:487-501](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L487-L501) —— 定义 `MemoryOrderingSemanticsAttr`（weak/relaxed/acquire/release/acq_rel）。

Python 侧用同名 `Enum` 暴露这三组取值，方便程序化构造：

[python/cuda_tile/dialects/cuda_tile_ops.py:190-239](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L190-L239) —— `AtomicRMWMode`、`MemoryScope`、`MemoryOrderingSemantics` 三个 Python 枚举。

#### 4.1.4 代码实践

1. **实践目标**：熟悉三个枚举属性的文本写法。
2. **操作步骤**：打开上面的三个源码链接，把每个枚举的「助记符」和「内部整数」抄成一张表。
3. **观察现象**：注意 `ADD=3`、`ADDF=4`、`XCHG=9` 这种内部编号与文本 `add`/`addf`/`xchg` 的对应。
4. **预期结果**：你能闭眼说出「整数加法是 `add`、浮点加法是 `addf`、交换是 `xchg`」，并知道 `weak` 在原子操作里不会出现。
5. 运行命令验证枚举解析：本步骤为「源码阅读型」，无需运行；如要运行见 4.2.4。

#### 4.1.5 小练习与答案

**练习 1**：为什么原子操作不允许 `memory_ordering_semantics = weak`？
**答案**：`weak` 的语义是「假设目标地址**没有并发访问**」，编译器可以据此省略同步。而原子操作的全部意义就是处理并发更新，用 `weak` 自相矛盾，所以校验器只接受 relaxed/acquire/release/acq_rel。

**练习 2**：`memory_scope` 写 `tl_blk` 还是 `sys` 更「安全」？为什么不能全写 `sys`？
**答案**：`sys` 覆盖范围最广（跨设备），最「保守安全」；但范围越大，硬件要插入的栅栏越重、性能越低。最佳实践是写「**刚好**覆盖所有可能并发访问的线程」的最小作用域：只有同块并发用 `tl_blk`，同 GPU 用 `device`，跨设备才用 `sys`。

---

### 4.2 atomic_rmw_tko：指针上的原子读-改-写

#### 4.2.1 概念说明

`atomic_rmw_tko` 是最通用的原子操作。它在**一组指针 tile** 指向的全局显存上，逐元素执行 `mode(x, arg)`，并把**旧值** `x` 作为结果返回，同时返回一个 token（因为它是 `_tko` 操作）。它是构造原子计数器、原子累加器的基础。

要素：
- 输入：指针 tile `%pointers`、`%mode`（如 `add`/`addf`）、值 tile `%arg`、可选 `%mask`（tile\<i1\>）、可选 `%token`。
- 输出：结果 tile `%result`（= 旧值）+ 结果 token。
- 约束：`pointers`/`arg`/`result` 形状一致；指针的 pointee 类型必须等于 `arg`/`result` 的元素类型；`mode` 必须与该元素类型兼容。

#### 4.2.2 核心流程

```
对 pointers 中的每个 (p, a) 元组（mask 为假的位置跳过）:
    原子事务 { x = *p; *p = mode(x, a); 返回 x }   // x 进入 result 对应位置
产生一个新 token（若有输入 token 则依赖之）
```

地址构造沿用了 u4-l1 / u5-l1 的标准链：`iota` 生成偏移 → `reshape`+`broadcast` 把单指针扩成指针 tile → `offset` 逐元素加偏移 → 喂给 `atomic_rmw_tko`。

#### 4.2.3 源码精读

操作声明与汇编格式（注意 `memory_ordering_semantics` 和 `memory_scope` 是位置操作数，`mask`、`token` 是可选的）：

[Ops.td:516-627](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L516-L627) —— `CudaTile_AtomicRMWTkoOp` 定义，含 `AllShapesMatch<["pointers","arg","result"]>` 与 `OnlyVariants<["RELAXED","ACQUIRE","RELEASE","ACQ_REL"]>`（明确排除 weak）。

合法用法正例（先构造 8 个指针，再用 `addf` 做浮点原子加）：

[test/Dialect/CudaTile/ops.mlir:1111-1151](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir#L1111-L1151) —— `testing$func @kernel6` 演示全部 9 个 mode（and/or/xor/add/max/min/umax/umin/xchg）及带 mask 的写法。

`verify()` 做三件事：校验 pointee 与 arg 元素类型一致、mask 形状一致、`mode` 与元素类型兼容、内存序合法：

[CudaTile.cpp:1496-1522](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1496-L1522) —— `AtomicRMWTkoOp::verify()`，依次调用 `verifyAtomicRMWMode` 与 `verifyAtomicMemoryOrdering`。

#### 4.2.4 代码实践

1. **实践目标**：写一个 `relaxed` + `device` 的浮点原子加，验证合法。
2. **操作步骤**：在已构建环境里写一段最小 MLIR（参考 ops.mlir 的 `kernel6` 写法），用 `iota`+`offset` 造指针，对 `tile<8xptr<f32>>` 执行 `atomic_rmw_tko relaxed device %ptrs, addf, %vals`，再用 `cuda-tile-opt` 跑一遍。
3. **观察现象**：合法时 `cuda-tile-opt` 原样吐出该操作（round-trip 通过）。
4. **预期结果**：终端无报错，输出里能看到 `atomic_rmw_tko relaxed device ... addf ...`。
5. 若本地未构建 `cuda-tile-opt`，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：把上面例子的 `addf` 换成 `add`，但 `%vals` 仍是 f32，会怎样？
**答案**：`add` 是整数模式，只接受 i32/i64。校验器 `verifyAtomicRMWMode` 会报 `'add' works only with integers i32 and i64`（见 SharedVerifiers.h:214-227）。

**练习 2**：`atomic_rmw_tko` 的 `%result` 装的是什么？如果不关心旧值，有没有更省的写法？
**答案**：`%result` 装的是每个地址**更新前**的旧值。如果不需要旧值，应改用 `atomic_red_view_tko`（4.4 节），它**不返回旧值**，省去读取回传，并能为 TMA 优化。

---

### 4.3 atomic_cas_tko：比较交换（无锁原语之母）

#### 4.3.1 概念说明

`atomic_cas_tko`（compare-and-swap）是无锁数据结构的基石。它逐元素执行：「**当**内存里的值等于 `%cmp` **时**，把它换成 `%val`」，并返回**旧值**。通过判断返回的旧值是否等于 `cmp`，调用方就能知道这次替换是否成功，从而实现无锁队列、自旋锁等。

支持的类型只有 i32/i64/f32/f64。浮点 CAS 特别注意：比较用的是**按位相等**（bitwise equality），而不是 IEEE-754 语义——不同的 NaN 位模式算作不同值，`+0.0` 与 `-0.0` 在位不同时也算不同。

#### 4.3.2 核心流程

```
对每个 (p, cmp, val) 元组（mask 为假的位置跳过，结果填 cmp[i]）:
    原子事务 {
        x = *p
        if x == cmp: *p = val   // 比较成功才写
        return x
    }
```

注意：CAS 是「比较相等才写」，而 `atomic_rmw_tko` 是「无条件按 mode 写」。

#### 4.3.3 源码精读

操作声明，含比较-交换伪代码说明与浮点按位比较的告诫：

[Ops.td:403-510](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L403-L510) —— `CudaTile_AtomicCASTkoOp` 定义，约束 `AllShapesMatch<["pointers","cmp","val","result"]>` 与 `AllTypesMatch<["cmp","val","result"]>`。

合法用法正例（带 mask 与 input token 两种变体）：

[test/Dialect/CudaTile/ops.mlir:1153-1170](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir#L1153-L1170)（区间起始 1153 行的 `@kernel7`）—— `atomic_cas_tko relaxed device %arg0, %arg1, %arg2`。

`verify()` 校验 pointee 与 val 元素类型一致、必须是 32/64 位整数或浮点、mask 形状一致、内存序合法：

[CudaTile.cpp:1626-1653](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1626-L1653) —— `AtomicCASTkoOp::verify()`，注意 1635-1639 行显式要求 32 或 64 位。

反例：i8 不在支持范围内：

[test/Dialect/CudaTile/invalid.mlir:1255-1259](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/invalid.mlir#L1255-L1259)（区间起始 1255 行）—— 对 `ptr<i8>` 做 CAS，期望报 `expect only float or integer types with 32 or 64 bit`。

#### 4.3.4 代码实践

1. **实践目标**：写一个 i32 的比较交换并观察其约束。
2. **操作步骤**：仿照 ops.mlir 的 `@kernel7`，对 `tile<2xptr<i32>>` 写 `atomic_cas_tko relaxed device %ptrs, %cmp, %val : tile<2xptr<i32>>, tile<2xi32> -> tile<2xi32>, token`。
3. **观察现象**：合法写法 round-trip 通过。
4. **预期结果**：再把 pointee 改成 `i8`，`cuda-tile-opt` 报「only float or integer types with 32 or 64 bit」。
5. 标注「待本地验证」若未构建工具。

#### 4.3.5 小练习与答案

**练习 1**：CAS 与 RMW 的「写条件」有何不同？
**答案**：RMW 无条件按 `mode` 写回新值；CAS 只有当内存当前值**等于 `cmp`** 时才写入 `val`，是条件写。

**练习 2**：为什么 CAS 的浮点比较用「按位相等」而非 IEEE-754？
**答案**：硬件原子 CAS 比的是位模式。若用 IEEE-754 语义（如把 `+0.0` 与 `-0.0` 视为相等、所有 NaN 视为相等），就需要在原子事务里额外做位规格化，破坏原子性且无硬件支持。所以规定按位比较，把语义判定留给上层。

---

### 4.4 atomic_red_view_tko：视图上的分布式归约（不返回旧值）

#### 4.4.1 概念说明

`atomic_red_view_tko`（13.3 引入）是专为**分布式梯度累加**等场景设计的。与 `atomic_rmw_tko` 有三点关键不同：

1. **用视图而非指针寻址**：输入是 `view`（必须是 `partition_view` 或 `strided_view`，**不支持** `gather_scatter_view`）+ 一组标量整数索引，定位全局张量里的一个 tile。
2. **不返回旧值**：只返回一个 token。这省去了读回旧值的开销，正好契合「我只管把本地贡献累加上去，不关心原来是多少」的归约语义。
3. **为 TMA 硬件优化收紧约束**：只允许 `relaxed` 内存序、只允许 `tl_blk`/`device` 作用域（禁 `sys`）、禁 `xchg`、视图不能带 `padding_value`。这些限制让它在 Hopper+ 上能映射到 TMA 的 `cp.reduce.async.bulk`（REDG）指令。

#### 4.4.2 核心流程

```
view[index] 选中全局张量里的一个 tile（与 value 同形）
对 shape(value) 中的每个 (i,j,…):
    原子事务 { view[index][i,j,…] := mode(view[index][i,j,…], value[i,j,…]) }
返回一个 token（旧值被丢弃）
```

约束：`value` 的元素类型须等于视图的元素类型；`value` 的 tile 形状须等于视图的 tile 形状；索引个数等于视图 tile 的秩；每个索引都是 rank-0（标量）整数 tile。

#### 4.4.3 源码精读

操作声明，注意三个 `OnlyVariants` 把内存序、作用域、mode 都收窄了：

[Ops.td:633-742](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L633-L742) —— `CudaTile_AtomicRedViewTkoOp` 定义：`OnlyVariants<["RELAXED"]>`（内存序）、`OnlyVariants<["TL_BLK","DEVICE"]>`（作用域）、`OnlyVariants<[...9 种 mode...]`（mode，无 xchg）。

典型用法（把一个 64×64 的本地贡献原子累加进大矩阵的某个分块）：

[test/Dialect/CudaTile/ops.mlir:1344-1408](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir#L1344-L1408)（区间起始 1344 行）—— `atomic_red_view_tko` 的基本用法、全部 mode、带 token、非零索引。

`verify()` 实现了多重限制（视图类型、padding_value、XCHG、relaxed-only、scope≠sys、索引个数与标量性）：

[CudaTile.cpp:1528-1620](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1528-L1620) —— `AtomicRedViewTkoOp::verify()`，1583-1594 行专门解释「relaxed + 非 sys scope」是为了 TMA 的 `cp.reduce.async.bulk`。

反例：xchg 模式、sys 作用域、weak 内存序都会被拒：

[test/Dialect/CudaTile/invalid.mlir:2250-2285](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/invalid.mlir#L2250-L2285) —— 三条反例及对应期望报错。

Python 包装在调用 C++ 前先做一层提前校验（同样的 xchg/sys/relaxed 约束）：

[python/cuda_tile/dialects/cuda_tile_ops.py:2163-2273](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L2163-L2273) —— `atomic_red_view_tko` 高层函数，2228-2229 行提前拒绝 xchg，2264-2273 行提前拒绝非 relaxed 与 sys scope。

#### 4.4.4 代码实践

1. **实践目标**：触发 `atomic_red_view_tko` 的三种非法约束，对照三层报错。
2. **操作步骤**：分别写三条 MLIR——用 `weak`、用 `xchg`、用 `sys` 作用域（参考 invalid.mlir:2250-2285），各跑 `cuda-tile-opt`。
3. **观察现象**：
   - `weak` → `only 'relaxed' memory ordering is supported for view-based atomic reductions`
   - `xchg` → `atomic_red_view_tko op cannot use xchg operation`
   - `sys` → `... is not supported for view-based atomic reductions; use 'tl_blk' or 'device' for TMA compatibility`
4. **预期结果**：三条报错与源码 `verify()` 里的字符串逐一对应。
5. 若在 Python 里调用 `atomic_red_view_tko(..., memory_scope=MemoryScope.SYS)`，会在构造 IR **之前**就抛 `ValueError`（见 cuda_tile_ops.py:2269-2273），这是 Python 层的提前拦截，比 C++ verifier 更早报错。

#### 4.4.5 小练习与答案

**练习 1**：`atomic_red_view_tko` 为什么禁用 `gather_scatter_view` 和带 `padding_value` 的视图？
**答案**：它的目标是把一段**连续、对齐**的全局 tile 原子归约，以便映射到 TMA `cp.reduce.async.bulk`（要求规整、对齐的 bulk 传输）。稀疏采集（gather_scatter）和越界填充都破坏这种规整性，硬件指令不支持，因此在校验器里直接拒绝（见 CudaTile.cpp:1533-1550）。

**练习 2**：既然 `atomic_red_view_tko` 与 `atomic_rmw_tko` 都是原子累加，二者的核心取舍是什么？
**答案**：`atomic_rmw_tko` 返回旧值、按指针寻址、限制少；`atomic_red_view_tko` 不返回旧值（更省）、按视图寻址（更适合分块矩阵）、限制严（为 TMA 优化）。需要旧值用前者，纯归约（如梯度累加）用后者。

---

### 4.5 校验机制：从 `verifyAtomicRMWMode` 到 `verifyMemoryModelLoad/Store`

#### 4.5.1 概念说明

CUDA Tile 对原子操作有一套**分层校验**：

- **共享工具层**（`SharedVerifiers.h`）：`verifyAtomicRMWMode` 检查「mode 与元素类型是否兼容」，`verifyAtomicMemoryOrdering` 检查「内存序是否在 relaxed/acquire/release/acq_rel 内」。这两个是模板函数，三个原子操作复用。
- **操作自身 `verify()`**：每个操作再补自己特有的检查（pointee 一致、形状一致、CAS 的位宽、red_view 的视图类型/scope/索引等）。
- **`.td` 的 `OnlyVariants`**：在**解析阶段**就把不合法的枚举值挡掉，比如 red_view 的内存序只能写 relaxed。
- **Python 层**：在构造 IR 前再做一次提前校验，给出更友好的错误。

另外，u5-l1 讲过的 `verifyMemoryModelLoad`/`verifyMemoryModelStore` 是给 `load_ptr_tko`/`store_ptr_tko` 用的（允许 weak，且 weak 不能带 scope），它们与原子操作的 `verifyAtomicMemoryOrdering`（不允许 weak，scope 必填）是**并列的两套**内存模型校验。理解二者区别是本节重点。

#### 4.5.2 核心流程

```
解析阶段:   OnlyVariants 挡住非法枚举（red_view: 只 relaxed / 只 tl_blk,device）
校验阶段:   verifyAtomicRMWMode(mode, elemType)        // mode↔类型
            verifyAtomicMemoryOrdering(ordering)        // 必须非 weak
            操作自身 verify(): pointee/形状/位宽/scope/索引...
Python 层:  构造前提前 raise ValueError（更早、更友好）
```

`load/store` 走的是另一条路：`verifyMemoryModelLoad`（允许 weak/relaxed/acquire，weak 禁 scope，其余必须带 scope）和 `verifyMemoryModelStore`（允许 weak/relaxed/release，同样 weak 禁 scope）。

#### 4.5.3 源码精读

`verifyAtomicRMWMode`：整数模式只认 i32/i64，`addf` 只认 f16/bf16/f32/f64，`xchg` 只认 32/64 位的整数或浮点：

[SharedVerifiers.h:210-253](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h#L210-L253) —— `verifyAtomicRMWMode`，三个 case 分支对应三类 mode。

`verifyAtomicMemoryOrdering`：只接受 relaxed/acquire/release/acq_rel（即排除 weak 与任何未知值）：

[SharedVerifiers.h:255-267](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h#L255-L267) —— `verifyAtomicMemoryOrdering`。

对比 `verifyMemoryModelLoad`（load_ptr_tko 用）：允许 weak/relaxed/acquire，weak 时禁止 scope，其余要求 scope：

[CudaTile.cpp:3574-3601](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L3574-L3601) —— `verifyMemoryModelLoad`，与原子操作的校验对照看。

声明同在 Ops.h：

[Ops.h:36-42](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.h#L36-L42) —— `verifyMemoryModelLoad`/`verifyMemoryModelStore` 声明。

反例（解析阶段就被 `OnlyVariants` 挡下的非法内存序）：

[test/Dialect/CudaTile/invalid.mlir:1780-1810](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/invalid.mlir#L1780-L1810)（区间起始 1780 行）—— `invalid_sem`/`weak`/`seq_cst` 三种非法写法及期望报错。

#### 4.5.4 代码实践

1. **实践目标**：跟踪一条非法原子操作从「文本」到「报错」的完整路径。
2. **操作步骤**：写 `atomic_rmw_tko weak device %ptrs, add, %vals`（参考 invalid.mlir:1793-1798），跑 `cuda-tile-opt`。
3. **观察现象**：报 `memory ordering semantics must be one of: relaxed, acquire, release, acq_rel`。
4. **预期结果**：这条报错来自 `verifyAtomicMemoryOrdering`（SharedVerifiers.h:259-265），证明 weak 在原子操作里被拒。再对比 `load_ptr_tko weak ...` 是**合法**的（因为 load 用的是 `verifyMemoryModelLoad`，允许 weak）。
5. 若想验证 `OnlyVariants` 的解析拦截：写 `atomic_rmw_tko seq_cst device ...`，会在解析阶段就报「expected ... enum values ... [weak, relaxed, acquire, release, acq_rel]」（invalid.mlir:1803-1809），根本进不到 `verify()`。

#### 4.5.5 小练习与答案

**练习 1**：`OnlyVariants`、C++ `verify()`、Python 包装，三层校验为什么「重复」？
**答案**：不是冗余，而是**纵深防御**。`OnlyVariants` 在解析阶段就拒绝非法枚举（最早、最便宜）；C++ `verify()` 处理需要类型推断的复杂约束（如 mode↔元素类型、视图形状）；Python 层在构造 IR 前拦截，给 Python 用户更友好、更早的错误。不同入口（MLIR 文本 / C++ API / Python API）都能被正确拦截。

**练习 2**：`atomic_rmw_tko weak device ...` 与 `load_ptr_tko weak device ...`，哪个合法？为什么？
**答案**：`load_ptr_tko weak device ...` 其实也**不**合法——`weak` 时禁止带 scope（`weak load must not have memory scope`）。但 `load_ptr_tko weak ...`（不带 scope）合法；而原子操作**无论带不带 scope 都不接受 weak**，因为原子操作天生并发。两者对 weak 的处理逻辑不同：load/store 允许 weak 但禁止它带 scope，原子操作压根禁止 weak。

---

## 5. 综合实践

把 u5-l1（token）、u5-l2（视图）、本讲（原子）串起来，实现一个**分布式梯度累加内核**的 MLIR 骨架：

1. 用 `make_tensor_view %ptr, shape=[8192,128], strides=[128,1]` 描述一个全局矩阵。
2. 用 `make_partition_view` 把它切成 64×64 的分块网格。
3. 假设 `%local` 是本地算出的 64×64 贡献，用 `atomic_red_view_tko relaxed device %view[%c0,%c0], addf, %local` 把它原子累加进 (0,0) 分块。
4. 再写一次累加进 (0,1) 分块，并用 `make_token`/`join_tokens` 或 input token 强制两次累加的先后顺序。
5. 另写一个**指针式原子计数器**：对 `tile<1xptr<i32>>` 用 `iota`+`offset`+`atomic_rmw_tko relaxed device %ptrs, add, %ones` 做整数原子加，观察返回的旧值。

验证：用 `cuda-tile-opt` 跑通合法版本；再故意把第 3 步改成 `atomic_red_view_tko relaxed sys ...`，确认被 `verify()` 拒绝；把第 5 步的 `add` 用在 f32 上，确认被 `verifyAtomicRMWMode` 拒绝。把每条报错对应到本讲引用的具体源码行。若本地未构建工具链，至少完成「源码阅读型」部分：把上述每一步的合法/非法判定依据写在注释里，并附上对应的永久链接。

## 6. 本讲小结

- Atomics 分组用三个 `_tko` 操作处理并发更新：`atomic_rmw_tko`（指针 RMW，返回旧值）、`atomic_cas_tko`（比较交换，条件写）、`atomic_red_view_tko`（视图归约，**不返回旧值**）。
- 三者都用 token 表达排序、都默认不受程序顺序约束，这与 u5-l1 的内存模型一致。
- `AtomicRMWMode` 决定运算种类并约束元素类型（整数 mode→i32/i64，`addf`→f16/bf16/f32/f64，`xchg`→32/64 位整数或浮点）。
- 内存序上，**原子操作不允许 `weak`**，只接受 relaxed/acquire/release/acq_rel，且 `memory_scope` 必填；这与 load/store（允许 weak、weak 禁 scope）是两套并列的内存模型校验。
- `atomic_red_view_tko` 为 TMA 优化额外收紧：只 relaxed、只 tl_blk/device、禁 xchg、禁 gather_scatter_view 和 padding_value 视图。
- 校验是纵深防御：`.td` 的 `OnlyVariants`（解析阶段）+ C++ `verify()`/`SharedVerifiers`（校验阶段）+ Python 提前校验，三层对同一约束做保险。

## 7. 下一步学习建议

- 下一讲（u5-l4）将进入**控制流**（for/loop/if/break/continue），与原子操作配合可实现更复杂的无锁算法与归约循环。
- 想深入硬件映射，建议阅读 `lib/Dialect/CudaTile/IR/CudaTile.cpp` 中 `AtomicRedViewTkoOp::verify()` 注释里提到的 TMA `cp.reduce.async.bulk`，并对照 u4-l5（MMA）理解 Hopper+ 张量核与 TMA 的协同。
- 若关注 Python 程序化构造，可读 `python/cuda_tile/dialects/cuda_tile_ops.py` 中 `atomic_rmw_tko`/`atomic_cas_tko`/`atomic_red_view_tko` 三个函数的提前校验逻辑，体会「Python 层提前拦截 vs C++ verifier」的分工。
- 后续 u9（优化器与变换 Pass）会讲解这些原子操作在 `cuda-tile-opt`/`cuda-tile-optimize` 管线里如何被规范化与调度。
