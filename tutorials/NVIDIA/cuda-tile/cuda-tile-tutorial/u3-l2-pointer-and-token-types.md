# 指针类型与 Token 类型

## 1. 本讲目标

上一讲（u3-l1）我们认识了 `cuda_tile` 方言的「主角」`TileType`——落在寄存器里、形状编译期完全确定的小矩阵。但一个内核要能真正读写显存、要在并发的访存之间建立先后顺序，光有 `TileType` 是不够的。本讲介绍方言里的两个「配角」类型：

- **PointerType**：指向全局设备显存（global device memory）的**有类型指针**，是把 `Tile` 和外部显存连接起来的桥梁。
- **TokenType**：一个**没有运行时数据载荷**的类型，专门用来显式表达「某些操作必须按某个先后顺序执行」。

学完本讲，你应当能够：

1. 读懂 `ptr<f32>`、`tile<128xptr<f32>>`、`token` 这几类文本写法，并说出它们各自的语义。
2. 理解为什么 `token` 不携带任何运行时数据，却能约束操作之间的执行顺序。
3. 掌握 `isPointerLike`、`getI1SameShape` 等类型工具函数的作用，以及方言如何处理 `token` 这个与 MLIR 内建类型「撞名」的助记符。

## 2. 前置知识

在进入源码前，先用通俗语言把两个关键概念讲清楚。

**什么是指针？** 在 CUDA 里，GPU 全局显存是一大片连续编址的字节。一个「指针」就是这大片字节里某个位置的地址。CUDA Tile 的指针是**有类型的（typed）**：它不只说「这是一个地址」，还说「这个地址上的数据被当作什么类型来解释」，比如 `ptr<f32>` 表示「指向一个 f32 的地址」。这一点和 C 语言的 `float*` 是一样的直觉。

**什么是指针 tile？** 上一讲我们说 `TileType` 的元素可以是数（`NumberType`）或指针（`PointerType`）。当元素是指针时，就得到 `tile<128xptr<f32>>`——一个装了 128 个「指向 f32 的地址」的小矩阵。这是访存操作的核心输入：你给出一组地址，加载操作就按这组地址去显存里取数。

**什么是 Token？** GPU 上有成千上万个线程在并发执行，访存操作的完成顺序往往和它们在源码里书写的顺序不一致。有时候我们**必须**保证「先写后读」「两次写不重叠」——否则结果未定义。Token 就是为这类需求设计的：它像一根「顺序的线」，把需要排序的操作串起来。关键在于：**Token 不代表任何数据**，它纯粹是编译器/调度器看到的「依赖边」。你可以把它理解成接力赛里的「接力棒」——棒本身没有重量，但只有拿到棒的人才能跑下一棒。

> 直觉总结：**PointerType 携带「去哪里取数据」，TokenType 携带「等谁先做完」。** 一个关乎地址，一个关乎顺序。

本讲假定你已经学过 u3-l1，知道 `TileType` 的静态形状、元素类型集合，以及 `maxTileNumElements` 这一上限。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `include/cuda_tile/Dialect/CudaTile/IR/Types.td` | 用 TableGen 声明 `PointerType`、`TokenType`，以及 `CudaTile_PointerTileType` 等类型约束 |
| `include/cuda_tile/Dialect/CudaTile/IR/Types.h` | 类型工具函数声明：`isPointerLike`、`getI1SameShape`、`maxTileNumElements` 常量 |
| `lib/Dialect/CudaTile/IR/Types.cpp` | 上述工具函数的实现，以及 `token` 助记符与 MLIR 内建类型撞名的解析处理 |
| `include/cuda_tile/Dialect/CudaTile/IR/Ops.td` | 通过 `make_token`/`join_tokens`/`load_ptr_tko`/`offset` 等操作展示这两类类型的实际用法 |
| `README.md` | print 内核示例，展示 `tile<ptr<f32>>` 的端到端用法 |

## 4. 核心概念与源码讲解

### 4.1 PointerType：有类型的全局显存指针

#### 4.1.1 概念说明

`PointerType` 表示「全局设备显存中的单个位置」。它是**有类型**的：声明时必须带上它指向的元素类型（pointee type）。它解决的问题是——CUDA Tile 的计算都在寄存器里的 `Tile` 上进行，但数据最初在显存里，需要一个类型来描述「显存地址 + 该地址上的数据类型」，这正是 `ptr<f32>` 这类写法的含义。

只有 `CudaTile_NumberType`（即整数或浮点）能作为 pointee 类型。这意味着你可以写 `ptr<i8>`、`ptr<f32>`，但不能写 `ptr<ptr<f32>>`（指针不能指向指针），也不能写 `ptr<i4>`（`i4` 不在 `NumberType` 里，这是 u3-l1 讲过的特例）。

#### 4.1.2 核心流程

一个 `PointerType` 在 IR 里的生命周期大致是：

1. **来源**：指针值通常由三类操作产生——`get_global`（取全局变量的地址）、`alloca`（在块内栈上分配，返回标量指针 tile）、`int_to_ptr`（把一个整数地址重解释为指针）。
2. **塑形**：单个标量指针（`tile<ptr<f32>>`）经 `reshape`/`broadcast` 扩展成一维指针 tile，再用 `offset` 按元素加上字节偏移，得到一组真正要访问的地址。
3. **消费**：`load_ptr_tko` 按这组地址从显存读数据进 `Tile`，`store_ptr_tko` 把 `Tile` 写回这些地址。

`offset` 操作的语义是把「指针 + 整数偏移」按 pointee 类型的**存储位宽**换算成字节：

\[
\text{result}_i = \text{ptr}_i + \text{offset}_i \times \text{bitwidth(pointee)}
\]

其中 `ptr` 被当作无符号整数、`offset` 被当作有符号整数，乘法和加法都不得溢出，否则结果未定义。这正是 README 示例里 `offset %data_ptr_broadcasted, %offsets` 所做的事。

#### 4.1.3 源码精读

`PointerType` 的声明在 `Types.td` 中，先看它的核心定义：

PointerType 的 TableGen 定义，声明了助记符 `ptr`、pointee 约束为 `CudaTile_NumberType`，以及文本格式 `ptr<...>`：
[Types.td:92-112](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L92-L112)

关键点拆解：

- `CudaTileTypeDef<"Pointer", "ptr", "pointerType", "13.1">`：类名 `Pointer`、助记符 `ptr`、自 `13.1` 版本引入。
- `parameters = (ins CudaTileConstrainedTypeParam<CudaTile_NumberType, "13.1">:$pointeeType)`：唯一的参数是 pointee 类型，约束为 `NumberType`，这正是「指针有类型且只能指向数」的来源。
- `assemblyFormat = "`<` custom<CudaTileType>($pointeeType) `>`"`：文本写成 `ptr<f32>`，尖括号里递归打印 pointee 类型。

那么指针能不能作 `Tile` 的元素？答案在 `TileElementType` 的定义里，它明确把 `CudaTile_PointerType` 列入允许的元素集合：

`TileElementType` 允许 `NumberType`、`PointerType`、`Int4` 三者作为 tile 元素，这是 `tile<128xptr<f32>>` 合法的依据：
[Types.td:118-123](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L118-L123)

与之配套，方言提供了一个专门的类型约束 `CudaTile_PointerTileType`，供 `offset` 等操作限定「操作数必须是指针 tile」：

`PointerTileType` 约束：元素必须是指针的 tile，被 `OffsetOp` 等操作的参数类型直接引用：
[Types.td:818-L818](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L818)

而 `OffsetOp` 正是用它约束自己的 `ptr` 操作数，并要求结果与输入同类型：

`OffsetOp` 定义：参数 `ptr` 类型为 `CudaTile_PointerTileType`，且 `AllTypesMatch<["result","ptr"]>` 保证偏移后仍是同型指针 tile：
[Ops.td:3614-3638](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3614-L3638)

最后看一个完整的真实用法——README 的 print 内核，展示了「标量指针 tile → reshape/broadcast → offset → load_ptr_tko」的最小链路（这是「待本地验证」可运行示例，u1-l3 有完整运行步骤）：

```mlir
entry @example_kernel(%data_pr : tile<ptr<f32>>) {
    %offsets = iota : tile<128xi32>
    %data_ptr_reshaped = reshape %data_pr : tile<ptr<f32>> -> tile<1xptr<f32>>
    %data_ptr_broadcasted = broadcast %data_ptr_reshaped : tile<1xptr<f32>> -> tile<128xptr<f32>>
    %data_ptr_tensor = offset %data_ptr_broadcasted, %offsets
        : tile<128xptr<f32>>, tile<128xi32> -> tile<128xptr<f32>>
    %data, %token = load_ptr_tko weak %data_ptr_tensor
        : tile<128xptr<f32>> -> tile<128xf32>, token
    print_tko "Data: %f\n", %data : tile<128xf32>
}
```

注意第 1 行 `%data_pr : tile<ptr<f32>>` 就是标量指针 tile（形状为空），`offset` 之后变成 `tile<128xptr<f32>>`（128 个地址），`load_ptr_tko` 据此读出 `tile<128xf32>`。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证 `ptr<...>` 与 `tile<...xptr<...>>` 的文本合法性，并理解 pointee 类型约束。
2. **操作步骤**：
   - 创建文件 `pointer_types.mlir`，写入下面内容（参考 `test/Dialect/CudaTile/ops.mlir` 中 `offset` 与 `get_global` 的写法）：

   ```mlir
   // 合法：指针只能指向 NumberType
   cuda_tile.module @m {
     entry @e(%p : tile<ptr<f32>>) {
       %i = iota : tile<4xi32>
       %pb = broadcast %p : tile<ptr<f32>> -> tile<4xptr<f32>>
       %q = offset %pb, %i : tile<4xptr<f32>>, tile<4xi32> -> tile<4xptr<f32>>
       print_tko "ok"
     }
   }
   ```

   - 用 `cuda-tile-opt pointer_types.mlir`（仅做解析/验证）跑一遍。
   - 再尝试把 `ptr<f32>` 改成 `ptr<ptr<f32>>`，重新运行。
3. **需要观察的现象**：合法版本能正常通过解析，IR 被原样打印（或规范化）；改成 `ptr<ptr<f32>>` 后，验证器应报出 pointee 类型不满足 `NumberType` 约束的错误。
4. **预期结果**：第一条通过、第二条报错，从而确认「指针不能指向指针」的约束来自 `CudaTile_NumberType` 这一 pointee 约束。
5. 若你当前环境未构建 `cuda-tile-opt`，本步骤的运行结果**待本地验证**；可退而阅读 `Types.td:92-112` 的 `parameters` 行作为静态依据。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tile<2xptr<i4>>` 是非法的？

**答案**：`PointerType` 的 pointee 被约束为 `CudaTile_NumberType`（见 `Types.td:108`），而 `i4` 不属于 `NumberType`（`AnyInt` 只含 i1/i8/i16/i32/i64，见 `Types.td:48-54`）。注意 `i4` 虽然能直接作 tile 元素（`TileElementType` 含 `CudaTile_Int4`），但不能作 `ptr` 的 pointee，这是两个不同的约束。

**练习 2**：`offset` 操作中，对 `ptr<f32>` 加上整数偏移 `1`，地址实际前进多少字节？

**答案**：前进 `1 × bitwidth(f32) = 1 × 32 = 32` 位 = 4 字节。因为 `offset` 按 pointee 类型的**存储位宽**换算（见 `Ops.td` 中 `offset` 的数学定义），而非按字节。

---

### 4.2 TokenType：用于排序的非运行时值

#### 4.2.1 概念说明

`TokenType` 是一种**特殊的类型**：它的值不是运行时数据。它的唯一用途是显式表达「token-ordered 操作」之间的先后顺序约束。换句话说，Token 是数据流图里的一条「依赖边」，不携带任何 payload。

为什么需要它？因为 GPU 上访存操作（load/store）的完成顺序与程序书写顺序不一定一致。如果不加约束，编译器或硬件可能重排它们，导致「写后读」变成「读到了旧值」。Token 提供了一种**显式、可控**的排序手段：你通过把一个 token 从「前一个操作」传给「后一个操作」，告诉编译器「后者必须等前者完成」。

关键设计取舍：Token 表达的是**操作排序**，而不是**数据依赖**。两个 load 之间如果只是想保证顺序，用 token 连接即可，不必让后一个 load「消费」前一个 load 的数据——后者会引入虚假的数据依赖，限制优化空间。

#### 4.2.2 核心流程

Token 在 IR 里的流转可以建模成一张「依赖图」：

1. **产生**：`make_token` 创建一个「无任何前置依赖」的新 token（图的源头）；`join_tokens` 把多个 token 合并成一个，新 token 依赖全部输入 token（汇合点）。此外，`load_ptr_tko`、`store_ptr_tko`、`print_tko` 等 token-ordered 操作会**额外产出一个 result token**，表示「本操作完成」。
2. **传递**：上述操作可以接受一个可选的 `input_token`，表示「必须等该 token 标记的操作完成后再执行」。
3. **汇合**：用 `join_tokens` 把多条依赖线收束成一根，再喂给后续操作。

用图论语言描述：每个 token-ordered 操作是图中的节点，token 值是有向边；`make_token` 是源点，`join_tokens` 是汇合点。一条边 `(A → B)` 表示「B 必须在 A 之后」。由于 token 不携带数据，这些边**只影响调度顺序，不参与数值计算**。

#### 4.2.3 源码精读

`TokenType` 的声明非常简洁——它没有任何参数：

`TokenType` 定义：助记符 `token`、自 `13.1` 引入，无任何参数，描述明确写出「Tokens are not runtime values」：
[Types.td:774-780](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L774-L780)

注意 description 里那句核心断言：「Tokens are not runtime values. Their purpose is to explicitly represent ordering constraints...」——这正是本节概念说明的出处。

再看产生 token 的两个原语操作。`make_token` 没有任何输入，只产出一个「全新、无依赖」的 token：

`MakeTokenOp` 定义：`arguments = (ins)`（无输入），结果为 `TokenType`，标注 `[Pure]`：
[Ops.td:3599-3608](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3599-L3608)

`join_tokens` 接受变长个 token、产出一个汇合 token，并带有自定义验证器：

`JoinTokensOp` 定义：输入为 `Variadic<CudaTile_TokenType>`，输出为单个 `TokenType`，描述说明「消费新 token 的操作将相对所有被合并 token 排序」：
[Ops.td:2556-2571](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2556-L2571)

最后看 token-ordered 操作如何「既消费又产出」token。以 `print_tko` 为例，它有一个可选输入 token 和一个结果 token（均自 `13.2` 引入，13.2 之前只有结果 token）：

`PrintTkoOp` 的 token 参数：可选输入 `$token` 与结果 `$result_token` 都是 `CudaTile_TokenType`，描述里强调「token-ordered print 不受程序顺序约束，除非用 token 限制」：
[Ops.td:3776-3815](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3776-L3815)

实际测试里能看到 token 的真实文本用法——`ops.mlir` 中 `print_tko` 返回 `!cuda_tile.token`，并通过 `token = %tok3` 把上一个 print 的 token 串到下一个：

```mlir
%tok3 = print_tko "val: %i", %c4_i32 : tile<i32> -> !cuda_tile.token
print_tko "next: %i", %c4_i32 token = %tok3 : tile<i32> -> !cuda_tile.token
```

第二行的 `token = %tok3` 就是「输入 token」，它强制第二条 print 排在 `%tok3`（即第一条 print）之后。

#### 4.2.4 代码实践

1. **实践目标**：用 `make_token` + `join_tokens` 手动构造一条 token 依赖链，体会「token 无数据载荷」。
2. **操作步骤**：
   - 创建 `token_chain.mlir`：

   ```mlir
   cuda_tile.module @m {
     entry @e() {
       // 两个互不依赖的 token
       %t0 = make_token : token
       %t1 = make_token : token
       // 汇合成一个 token，依赖 t0 和 t1
       %tj = join_tokens %t0, %t1 : token
       // 让 print 排在汇合之后
       print_tko "done" token = %tj -> token
     }
   }
   ```

   - 用 `cuda-tile-opt token_chain.mlir` 解析验证。
3. **需要观察的现象**：IR 能正常解析，`make_token`/`join_tokens` 的文本形式与上面一致；token 值在 IR 里只作为操作数/结果出现，没有任何「数值」属性。
4. **预期结果**：验证通过；你能直观看到 token 只是把操作「串」起来，不参与运算。
5. 本步骤运行结果**待本地验证**（依赖已构建的 `cuda-tile-opt`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么说 Token 是「非运行时值」？它和 `tile<i32>` 在 IR 里的根本区别是什么？

**答案**：`TokenType` 的 description 明确写「Tokens are not runtime values」。`tile<i32>` 在运行时对应寄存器里实际的整数数据，参与运算；而 token 值不对应任何寄存器数据，只存在于编译期的依赖图里，用于约束操作调度顺序。

**练习 2**：如果两个 `print_tko` 之间**没有**用 token 连接，它们的输出顺序是否确定？

**答案**：不确定。`PrintTkoOp` 的描述明确指出「Token-ordered print operations are not constrained by program order. The compiler may reorder them... unless further constrained by tokens.」因此不加 token 时，编译器/硬件可能重排两个 print，输出顺序不可预期。

---

### 4.3 类型工具函数与 token 撞名处理

#### 4.3.1 概念说明

除了类型本身的定义，方言还在 `Types.h/.cpp` 里提供了一组**工具函数**，供操作的定义、验证和规范化复用。本节聚焦三个最相关的：

- `isPointerLike(Type)`：判断一个类型「是不是指针或指针 tile」。
- `getI1SameShape(Type)`：给定一个 tile，返回一个**同形状、元素为 i1** 的 tile（常用于构造掩码 mask）。
- `parseCudaTileType` 中对 `token` 助记符的**特殊解析**：因为 MLIR 内建方言后来也引入了 `token` 类型，与 `cuda_tile` 的 `token` 助记符撞名，方言必须显式优先解析成自己的 token。

这些工具是「胶水」：它们不改变类型语义，但让操作定义（`Ops.td`）和验证逻辑（`Types.cpp`、`Traits.cpp`、`CudaTile.cpp`）能够方便地识别和处理指针/掩码/ token。

#### 4.3.2 核心流程

- **`isPointerLike`** 是递归的：若类型本身就是 `PointerType` 返回 true；若是 `TileType`，则看它的元素类型是否「pointer-like」。这样 `ptr<f32>` 和 `tile<128xptr<f32>>` 都会被判定为「pointer-like」，调用方不必区分标量指针与指针 tile 两种情况。
- **`getI1SameShape`** 取输入 tile 的形状，配以 1 位整数元素类型，构造一个新 `TileType`。它在 `if` 操作的规范化里被用来把条件转成同形状的 i1 掩码。
- **token 解析**：`parseCudaTileType` 在尝试通用解析之前，先探测是否是 `token` 关键字；若是，直接构造 `cuda_tile::TokenType`，避免被内建 `token` 抢走。

#### 4.3.3 源码精读

先看 `Types.h` 中的声明——`isPointerLike` 与 `getI1SameShape` 的签名，以及注释中关于 `maxTileNumElements`（上一讲讲过）的寄存器压力动机：

工具函数声明：`isPointerLike` 判断「指针或指针 tile」，`getI1SameShape` 返回同形状的 i1 tile：
[Types.h:48-52](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.h#L48-L52)

再看 `Types.cpp` 中 `isPointerLike` 的递归实现，它对 `TileType` 递归地检查元素类型：

`isPointerLike` 实现：对 `PointerType` 直接返回 true；对 `TileType` 递归检查其 `getElementType()`：
[Types.cpp:40-46](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L40-L46)

`getI1SameShape` 的实现——取出形状，配 1 位整数元素，构造新 tile：

`getI1SameShape` 实现：用输入 tile 的形状 + `IntegerType::get(ctx, 1)` 构造同形状 i1 tile：
[Types.cpp:214-219](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L214-L219)

最值得注意的是 token 的「撞名」处理。`parseCudaTileType` 在通用解析前，先专门识别 `cuda_tile::TokenType::getMnemonic()`（即 `token`）：

`parseCudaTileType` 中对 token 的优先解析：注释明说「MLIR 内建方言现在也提供了 `token` 类型，其拼写与 cuda_tile 的 `token` 助记符冲突，必须确保解析成 cuda_tile 的 token」：
[Types.cpp:90-97](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L90-L97)

这段代码解释了为什么在测试里写 `token`（无前缀）也能正确解析成 `cuda_tile.token` 而非内建类型——方言在解析入口就把它「截胡」了。

#### 4.3.4 代码实践

1. **实践目标**：通过阅读源码与测试，验证 `isPointerLike` 对标量指针和指针 tile 都返回 true，并理解 token 撞名处理的效果。
2. **操作步骤**：
   - 阅读 `lib/Dialect/CudaTile/IR/Traits.cpp:20-28`，看 `SameLoadStoreDataType` 之类的 trait 如何用 `isPointerLike` + `cast<PointerType>` 配合判断「源指针 tile 的 pointee 类型与目标 tile 元素类型一致」。
   - 在 `test/Dialect/CudaTile/ops.mlir` 中搜索 `-> token` 与 `token =`，观察 token 值既可作结果也可作输入。
   - 用 `cuda-tile-opt` 跑一段同时含 `tile<ptr<f32>>` 与 `token` 的 MLIR，确认两者都能被正确解析打印。
3. **需要观察的现象**：指针 tile 的 pointee 类型在 IR 打印中保留（如 `ptr<f32>>`）；`token` 无论带不带 `!cuda_tile.` 前缀都解析为同一类型。
4. **预期结果**：确认工具函数让操作定义可以「不区分标量指针与指针 tile」地统一处理，且 token 撞名不影响解析正确性。
5. 运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`isPointerLike(tile<4xptr<f32>>)` 的求值过程是怎样的？

**答案**：进入 `isPointerLike`，先 `isa<PointerType>` 为 false；再 `dyn_cast<TileType>` 成功，取出元素类型 `ptr<f32>`，递归调用 `isPointerLike(ptr<f32>)`；这次 `isa<PointerType>` 为 true，返回 true。因此整个调用返回 true。

**练习 2**：为什么 `parseCudaTileType` 要在通用类型解析之前，专门处理 `token` 关键字？

**答案**：因为较新版本的 MLIR 内建方言也引入了一个拼写同为 `token` 的类型（见 `Types.cpp:91-93` 的注释）。若不优先处理，通用解析器可能把 `token` 解析成内建类型，导致 `cuda_tile` 的 token 操作拿到错误类型的操作数。方言因此在解析入口先「截胡」，保证 `token` 一律解析为 `cuda_tile::TokenType`。

---

## 5. 综合实践

把本讲的两类类型串起来，完成一个「读源码 + 写 IR」的小任务：

**任务**：阅读 README 的 print 内核（`README.md:235-242`），然后自己写一段最小 MLIR，要求同时用到本讲的两类类型：

1. 声明一个标量指针 tile 入参 `tile<ptr<f32>>`（PointerType 作 tile 元素）。
2. 用 `iota` + `broadcast` + `offset` 把它扩展成 `tile<4xptr<f32>>`，模拟生成 4 个地址。
3. 用 `load_ptr_tko weak` 从这组地址读出 `tile<4xf32>`，并**接收它返回的 token**（TokenType 的产出方）。
4. 再用一个 `print_tko`，通过 `token = %tok` 让它排在 load 之后（TokenType 的消费方）。

参考骨架（需你补全类型注解）：

```mlir
cuda_tile.module @m {
  entry @e(%p : tile<ptr<f32>>) {
    %i  = iota : tile<4xi32>
    %pb = broadcast %p : tile<ptr<f32>> -> tile<4xptr<f32>>
    %q  = offset %pb, %i : tile<4xptr<f32>>, tile<4xi32> -> tile<4xptr<f32>>
    %data, %tok = load_ptr_tko weak %q : tile<4xptr<f32>> -> tile<4xf32>, token
    print_tko "v=%f\n", %data token = %tok : tile<4xf32> -> token
  }
}
```

完成后，请回答两个问题以自检：

- **PointerType 这一面**：从 `%p` 到 `%q`，指针 tile 的形状是如何从标量（空形状）变成 4 的？涉及哪几个操作？（答：`broadcast` 把空形状扩展为 `[4]`，`offset` 保持形状不变只改地址值。）
- **TokenType 这一面**：`%tok` 是由哪个操作产出的？又被哪个操作消费？如果删掉 `token = %tok`，`print_tko` 与 `load_ptr_tko` 之间是否还有顺序保证？（答：由 `load_ptr_tko` 产出，被 `print_tko` 消费；删掉后二者之间无顺序保证，可能被重排。）

用 `cuda-tile-opt` 验证你写的 IR 能被正确解析（运行结果**待本地验证**）。

## 6. 本讲小结

- **PointerType** 是有类型的全局显存指针，文本写作 `ptr<f32>`，pointee 类型被约束为 `NumberType`（不能指向指针或 `i4`）。
- 指针可以作 `Tile` 的元素，得到 `tile<Nxptr<T>>`（指针 tile），它是 `offset`、`load_ptr_tko`、`store_ptr_tko` 等访存操作的核心输入；`CudaTile_PointerTileType` 是对应的类型约束。
- **TokenType** 是非运行时值，唯一用途是显式表达 token-ordered 操作之间的排序约束；由 `make_token`/`join_tokens` 产生与汇合，由 load/store/print 操作产出与消费。
- Token 表达的是**操作排序**而非数据依赖，因此能在不引入虚假数据流的前提下约束调度顺序。
- 工具函数 `isPointerLike` 递归地统一处理「标量指针」与「指针 tile」；`getI1SameShape` 用于构造同形状 i1 掩码。
- 由于 MLIR 内建方言也引入了 `token` 类型，`parseCudaTileType` 在通用解析前优先把 `token` 截胡为 `cuda_tile::TokenType`，避免撞名误解析。

## 7. 下一步学习建议

本讲只讲了 PointerType 与 TokenType 两个「基础特殊类型」，尚未涉及「全局张量的分块访问」。后续建议：

1. **u3-l3 视图类型族**：学习 `TensorView`/`PartitionView`/`StridedView`/`GatherScatterView`——它们描述如何把全局显存里的张量切成一块块 tile，是 PointerType 之上的更高层访存抽象。
2. **u5-l1 内存模型与 Token 顺序**：本讲只点了 token 的「排序」直觉，u5-l1 会系统讲解 `load_ptr_tko`/`store_ptr_tko` 的 weak/relaxed 语义、`alloca` 栈分配，以及 token 如何在并发访存间建立内存一致性。
3. **延伸阅读源码**：可先浏览 `lib/Dialect/CudaTile/IR/Traits.cpp`（看 `isPointerLike` 的真实使用）和 `Ops.td` 中 `CudaTile_LoadOpBase`（看 load/store 如何同时返回数据 tile 与 token），为 u5 做铺垫。
