# 内存模型与 Token 顺序

## 1. 本讲目标

前面几讲我们处理的都是「落在寄存器里、编译期形状完全确定」的 `Tile`（u3-l1），以及把 `Tile` 与外部显存连起来的 `PointerType` / `TokenType`（u3-l2），还有构造地址的 `offset` 等核心操作（u4-l1）。但一个真正能跑的内核，必须能**读写显存**，而且要在**并发的访存之间建立可靠的先后顺序**——否则会出现「先读后写」或「两次写相互覆盖」这类未定义行为。本讲就来拆解 CUDA Tile 方言的 **Memory 分组操作**，把「访存」与「顺序」这两件事讲清楚。

学完本讲，你应当能够：

1. 读懂并写出 `load_ptr_tko` / `store_ptr_tko`，理解它的指针 tile 输入、可选 `mask` 与 `padding`、以及 `weak` / `relaxed` / `acquire` 等内存序语义。
2. 理解 **Token 排序模型**：为什么 token-ordered 操作不受程序顺序约束、以及如何用 `make_token` / `join_tokens` 显式串起访存的先后顺序。
3. 掌握 `alloca` 这种块内临时分配的语义：对齐必须是 2 的幂、生命周期限于所在 block、以及 `global` 标记的含义。
4. 在源码层面定位 `verifyLoadStoreType` / `verifyLoadStoreMask` / `verifyLoadPadding` 这三组校验，以及 `weak` 与 `memory_scope` 互斥等内存模型校验逻辑。

## 2. 前置知识

进入源码前，先用通俗语言把几个关键直觉建立起来。

**为什么 GPU 访存需要「顺序」？** GPU 上有成千上万个线程并发执行，访存延迟又很高，编译器和硬件都会**重排（reorder）**访存指令以提升吞吐。对纯计算的 tile 来说这无所谓，但一旦涉及显存读写，「先写后读」「两次写不重叠」这类约束就必须被显式表达出来，否则结果未定义。CUDA Tile 用 **Token** 来表达这种顺序约束。

**Token 是什么？** 这是 u3-l2 已介绍过的类型：它**不携带任何运行时数据**，纯粹是一条「依赖边」。一个 token-ordered 操作（名字带 `_tko` 后缀）会**产出**一个 token，并能**消费**一个可选的输入 token。当你把操作 B 的输入 token 接到操作 A 产出的 token 上时，就告诉编译器：「A 必须在 B 之前完成」。注意这是**顺序依赖**，不是数据依赖——A 的数据结果并没有喂给 B。

**什么是「token-ordered」？** 名字里带 `_tko` 的操作（如 `load_ptr_tko`、`store_ptr_tko`、`print_tko`）有一个共同特性：**它们不受程序顺序（program order）约束**。也就是说，编译器可以把它们前移或后移，除非你用 token 把它们的顺序钉死。这与「按出现顺序逐条执行」的传统 IR 形成鲜明对比——这也是本讲最核心的心智模型。

**mask 与 padding 是什么？** 访存时常常遇到「边界」「不规则」的情形：某些地址不该被访问。`mask` 是一个与结果同形状的 `tile<i1>`，掩码为 0 的元素不会被真正读写；被屏蔽的元素取 `padding` 提供的填充值（不提供则该位置值未定义）。

**alloca 是什么？** 类似 C 的栈上分配，在当前 block 内临时申请一块显存/局部存储，返回一个标量指针 tile（`tile<ptr<f32>>`）。它解决的问题是：内核有时需要一块临时的 scratch 缓冲，又不想走全局变量的沉重路径。

> 直觉总结：**`_tko` 操作默认「乱序」，靠 token 把需要排序的串起来；指针 tile + mask + padding 决定「访问哪些地址」；alloca 提供「块内临时缓冲」。**

本讲假定你已学过 u3-l2（`PointerType` / `TokenType`）、u4-l1（`iota` / `reshape` / `broadcast` / `offset` 构造地址链），并能用 `cuda-tile-opt` 验证一段 MLIR。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `include/cuda_tile/Dialect/CudaTile/IR/Ops.td` | 用 TableGen 声明本讲全部操作：`alloca`、`make_token`、`join_tokens`、`load_ptr_tko`、`store_ptr_tko`，以及 `LoadOpBase`/`StoreOpBase` 公共基类与文档 |
| `include/cuda_tile/Dialect/CudaTile/IR/Dialect.td` | 定义 `CudaTileMemOpDef` 基类，把上述操作统一归入 **Memory** 分组 |
| `include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td` | 定义 `MemoryOrderingSemanticsAttr`（weak/relaxed/acquire/release/acq_rel）与 `MemoryScopeAttr`（tl_blk/device/sys）两个枚举属性 |
| `include/cuda_tile/Dialect/CudaTile/IR/Traits.h` | 声明 `verifyLoadStoreType` / `verifyLoadStoreMask` / `verifyLoadPadding` 三个类型校验函数 |
| `lib/Dialect/CudaTile/IR/Traits.cpp` | 上述三个校验函数的实现，是 load/store 类型合法性的核心 |
| `lib/Dialect/CudaTile/IR/CudaTile.cpp` | `JoinTokensOp::verify`、`LoadPtrTkoOp::verify`、`StorePtrTkoOp::verify`，以及 `verifyMemoryModelLoad` / `verifyMemoryModelStore` 内存模型校验 |
| `include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h` | `verifyAlloca` 模板，校验 alignment 为 2 的幂且不小于元素自然大小 |
| `test/Dialect/CudaTile/memory_consistency_ops.mlir` | token、load/store、mask、ordering/scope 的 round-trip 测试 |
| `README.md` | print 内核示例，展示 `offset` + `load_ptr_tko` + `print_tko` 的端到端用法 |

## 4. 核心概念与源码讲解

### 4.1 Token 排序模型：make_token 与 join_tokens

#### 4.1.1 概念说明

`make_token` 产生一个**全新的、没有任何前置依赖**的 token；`join_tokens` 接收两个或更多 token，合成一个新的 token——这个新 token 依赖于**所有**输入 token。任何消费这个新 token 的 token-ordered 操作，都会被排到「所有被 join 的操作之后」。

为什么要单独造一个 token 类型系统？因为 `_tko` 操作**默认不受程序顺序约束**。如果不提供 token 依赖，编译器可以自由重排它们；当你确实需要「A 必须先于 B」时，唯一的办法就是把 A 的输出 token 喂给 B 的输入 token。`join_tokens` 则用于「合流」：当 B 需要等待多个操作（A1、A2……）都完成后才能执行，就把它们的 token join 成一个再传给 B。

这两个操作都被标记为 `Pure`（无副作用），因为它们只操纵顺序约束、不读写真实内存。

#### 4.1.2 核心流程

token 的「生产—汇合—消费」三步：

```
make_token ──┐
             ├── join_tokens ──→ 某个 _tko 操作的 input token
make_token ──┘
```

更典型的真实场景是：访存操作本身就会产出 token，于是可以串成链：

```
store_ptr_tko 产出 t1 ──┐
                        ├── join_tokens t1, t2 ──→ load_ptr_tko 的 input token
别的访存产出 t2 ────────┘
```

注意两点：(1) `join_tokens` 至少要两个输入 token，少于两个会被 verifier 拒绝；(2) 一个 `_tko` 操作的输入 token 是**可选**的——不写就等于「不强制任何顺序」。

#### 4.1.3 源码精读

`make_token` 没有任何操作数，只有一个 token 结果，文本写法极简：

[Ops.td:3599-3608](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3599-L3608) 声明 `make_token`：参数列表为空（`(ins)`），结果为单个 `TokenType`，且带 `Pure` trait。

`join_tokens` 接收**可变个数**的 token（`Variadic<CudaTile_TokenType>`），合成一个 token：

[Ops.td:2556-2571](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2556-L2571) 定义 `join_tokens`，描述里写明「produces a fresh token which depends on all input tokens」。

校验「至少两个 token」的逻辑在 verifier 里，逻辑非常直白：

[CudaTile.cpp:3563-3568](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L3563-L3568) `JoinTokensOp::verify`：当 `getTokens().size() < 2` 时报错 "expect two or more tokens"。

测试文件里给出了 token 的标准文本形态，两 token、三 token join 都覆盖了：

[memory_consistency_ops.mlir:13-37](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/memory_consistency_ops.mlir#L13-L37) `join_tokens_two_tokens` 与 `join_tokens_three_tokens` 两个用例，演示 `%3 = join_tokens %0, %1, %2 : token` 的写法。

#### 4.1.4 代码实践

**目标**：亲手写出 make_token / join_tokens，并验证 round-trip。

**步骤**：

1. 在一个临时目录创建 `tokens.mlir`：

   ```mlir
   // RUN: cuda-tile-opt %s | cuda-tile-opt | FileCheck %s

   cuda_tile.module @kernels {
   // CHECK-LABEL: @my_join
   testing$func @my_join() -> !cuda_tile.token {
     %0 = make_token : token
     %1 = make_token : token
     %2 = join_tokens %0, %1 : token
     return %2 : token
   }
   } // end
   ```

   > 说明：`testing$func` 是仅在测试构建（`TILE_IR_INCLUDE_TESTS` 开启）下可用的函数容器，普通内核用 `entry`。这里沿用了官方测试文件的风格。

2. 用 `cuda-tile-opt` 跑一遍：`cuda-tile-opt tokens.mlir`，观察输出中 `make_token` 与 `join_tokens` 是否原样保留。

**需要观察的现象**：操作文本不变（round-trip 通过），说明语法合法。

**预期结果**：终端打印的 IR 中 `%2 = join_tokens %0, %1 : token` 与输入一致。

**待本地验证**：若你的构建未开启测试（`testing$func` 不可用），请把容器换成 `entry @my_join`（并去掉返回值，改用 `return`），再跑 `cuda-tile-opt`。

#### 4.1.5 小练习与答案

**练习 1**：如果只给 `join_tokens` 传一个 token 会怎样？

**参考答案**：verifier 报错 "expect two or more tokens"（[CudaTile.cpp:3565-3566](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L3565-L3566)）。如果只想传递单个 token 的依赖，直接把那个 token 喂给下游操作的输入 token 即可，不需要 join。

**练习 2**：`make_token` 和 `join_tokens` 为什么被标成 `Pure`？

**参考答案**：因为它们只操纵「顺序依赖关系」，既不读也不写真实内存，对程序的可观察状态没有副作用。`Pure` 让编译器可以更自由地处理它们（如消除冗余的 token 构造）。

---

### 4.2 指针访存：load_ptr_tko 与 store_ptr_tko

#### 4.2.1 概念说明

`load_ptr_tko` 是基于**指针 tile** 的 gather（聚集）读取：输入一个 `tile<Nxptr<T>>`（一组地址），从全局显存把这些地址上的数据取回，得到 `tile<NxT>`。`store_ptr_tko` 是对应的 scatter（散射）写入：把一个 `tile<NxT>` 的数据写到一组 `tile<Nxptr<T>>` 指定的地址里。

后缀 `_tko` = **token-ordered**，意味着它们「默认不受程序顺序约束，靠 token 排序」。二者都额外产出**一个 token**，供下游操作建立顺序依赖。`load_ptr_tko` 产出「数据 tile + token」两个结果；`store_ptr_tko` 只产出 token（写操作没有数据返回）。

可选的三个修饰：

- **mask**：`tile<i1>`，与结果同形状，掩码为 0 的元素不被访问。load 时被屏蔽位置取 padding（未给则未定义）；store 时被屏蔽位置不写入。
- **padding**（仅 load）：被 mask 屏蔽位置的填充值，类型与结果一致。i1 在内存中按整字节存取（非零字节规范化为 `0x01`，零字节为 `0x00`）。
- **token**：可选的输入 token，用于强制本操作排在某些操作之后。

#### 4.2.2 核心流程

**load 的数据流**（以 README 的 print 内核为例）：

```
单个基址 ptr<f32>
  └─ reshape → tile<1xptr<f32>>
       └─ broadcast → tile<128xptr<f32>>      // 128 个相同基址
            └─ offset + iota(0..127) → tile<128xptr<f32>>  // 128 个连续地址
                 └─ load_ptr_tko weak → (tile<128xf32>, token)
```

这就是 u4-l1 讲过的「地址构造链」的终点：用 `offset` 把基址扩成一组真正要访问的地址，再交给 `load_ptr_tko`。

**store 的数据流**类似但方向相反：`store_ptr_tko weak %dst_ptr_tile, %value_tile : ...`，把 `%value_tile` 写到 `%dst_ptr_tile` 指定的一组地址。

**两个结果的连接关系**（load）：

\[ 
\text{result\_tile},\ \text{result\_token} = \texttt{load\_ptr\_tko}\ \text{ordering}\ \text{source}\ [\text{, mask}\ [\text{, padding}]]\ [\text{token = } t_{\text{in}}]
\]

#### 4.2.3 源码精读

`load_ptr_tko` 派生自公共基类 `CudaTile_LoadOpBase`，后者又派生自 `CudaTileMemOpDef`（即 Memory 分组）：

[Ops.td:2783-2798](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2783-L2798) `CudaTile_LoadOpBase` 用三个 `TypesMatchWith`/`OptionalTypesMatchWith` trait 表达「source 是 result 的指针版」「mask 形状匹配 source」「padding 类型匹配 result」，并分别委托给 4.4 节要讲的三个校验函数。

`load_ptr_tko` 的关键描述点明它「without ordering guarantees」：

[Ops.td:2804-2866](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2804-L2866) `CudaTile_LoadPtrTkoOp`：摘要拼接 `LoadOpBaseDoc.summary + " without ordering guarantees"`，并在描述中强调「Token-ordered operations are not constrained by program order. The compiler may reorder them … unless further constrained by tokens.」

它的操作数/结果结构（这是本讲最该记住的骨架）：

| 成员 | 类型约束 | 含义 |
|------|----------|------|
| `memory_ordering_semantics` | `MemoryOrderingSemanticsAttr`（仅 WEAK/RELAXED/ACQUIRE） | 内存序语义，必填 |
| `memory_scope` | `OptionalAttr<MemoryScopeAttr>` | 作用域，仅非 weak 时填 |
| `source` | `CudaTile_PointerTileType` | 指针 tile（一组地址）|
| `mask` | `Optional<tile<i1>>` | 可选掩码 |
| `paddingValue` | `Optional<NumberTileType>` | 可选填充值 |
| `token` | `Optional<TokenType>` | 可选输入 token |
| `result` / `result_token` | `TileType` / `TokenType` | 数据结果 / 顺序结果 |

`store_ptr_tko` 派生自 `CudaTile_StoreOpBase`，结构对称但只产出 token：

[Ops.td:4433-4468](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4433-L4468) `CudaTile_StorePtrTkoOp`：操作数为 `destination`(指针 tile) + `value`(数据 tile) + 可选 `mask`/`token`，结果只有 `result_token`；ordering 仅允许 WEAK/RELAXED/RELEASE（注意 load 允许 ACQUIRE，store 允许 RELEASE，这与 C++ 内存模型一致）。

公共文档描述了 gather/scatter 的语义：

[Ops.td:2759-2781](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2759-L2781) `LoadOpBaseDoc`：说明 source 是「tile of pointers」，按这组地址 gather；mask 控制哪些元素被加载；padding 在 mask 存在时可选，未给则屏蔽元素值未定义。

[Ops.td:4397-4414](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4397-L4414) `StoreOpBaseDoc`：store 是 scatter，destination 为指针 tile，mask 控制选择性写入。

README 的 print 内核是这套数据流最完整的示例：

[README.md:234-245](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L234-L245) `entry @example_kernel` 用 `iota` 生成 0..127 偏移，`reshape`+`broadcast` 把单个 `ptr<f32>` 扩成 `tile<128xptr<f32>>`，`offset` 算出 128 个连续地址，最后 `load_ptr_tko weak` 取回 `tile<128xf32>` 并由 `print_tko` 输出。

测试文件覆盖了带 mask、带 mask+padding、带 scope 的多种写法：

[memory_consistency_ops.mlir:39-84](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/memory_consistency_ops.mlir#L39-L84) `load_ptr_tko`（无 token）、`load_ptr_tko_scoped`（带 `acquire device`）、`load_with_mask`、`load_with_mask_and_padding` 四个用例，逐一展示操作数的组合。

[memory_consistency_ops.mlir:86-104](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/memory_consistency_ops.mlir#L86-L104) `store` 与 `store_with_mask`，展示 `store_ptr_tko weak %dst, %value token = %t : ... -> token` 的写法。

#### 4.2.4 代码实践

**目标**：复现 README 的地址构造 + weak load，并加上 mask 与 padding 变体。

**步骤**：

1. 把 [README.md:234-245](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L234-L245) 的内核另存为 `weak_load.mlir`。
2. 运行 `cuda-tile-opt weak_load.mlir`，确认 IR 合法、`load_ptr_tko weak` 原样保留。
3. 在内核里追加一段带 mask 与 padding 的 load（参考 [memory_consistency_ops.mlir:76-84](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/memory_consistency_ops.mlir#L76-L84)）：

   ```mlir
   %mask = constant <i1: 1> : tile<128xi1>        // 示例代码：全 1 掩码
   %pad  = constant <f32: 0.0> : tile<128xf32>     // 示例代码：填充 0
   %m, %mt = load_ptr_tko weak %data_ptr_tensor, %mask, %pad
     : tile<128xptr<f32>>, tile<128xi1>, tile<128xf32> -> tile<128xf32>, token
   ```

**需要观察的现象**：带三个操作数时，`cuda-tile-opt` 仍能解析并通过；注意类型列表里 `source, mask, padding -> result, token` 的顺序必须与 [Ops.td:2857-2862](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2857-L2862) 的 assemblyFormat 一致。

**预期结果**：两段 load 都 round-trip 通过，无 verifier 报错。

**待本地验证**：若要真正看到 `print_tko` 输出的 128 个浮点数，需要按 README 的 host 程序（`cuLaunchKernel`）在真实 GPU 上运行；本步骤只验证 IR 合法性。

#### 4.2.5 小练习与答案

**练习 1**：`load_ptr_tko` 返回几个结果？`store_ptr_tko` 呢？

**参考答案**：`load_ptr_tko` 返回两个——数据 tile 与 token；`store_ptr_tko` 只返回一个 token（写操作没有数据返回）。见 [Ops.td:2847-2848](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2847-L2848) 与 [Ops.td:4453](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4453)。

**练习 2**：为什么 README 的 load 不需要写输入 token？

**参考答案**：因为输入 token 是**可选**的（`Optional<CudaTile_TokenType>`）。不写就意味着「本操作不强制排任何前置顺序」——这正是 weak、单次独立访存的常见场景。需要排序时才用 `token = %t` 接上前置操作产出的 token。

---

### 4.3 内存序与作用域：weak / relaxed / acquire / release 与 tl_blk / device / sys

#### 4.3.1 概念说明

`memory_ordering_semantics` 描述「本访存与其他并发访存之间的同步假设」，`memory_scope` 描述「可能并发访问同一地址的线程范围有多大」。二者共同决定编译器需要插入多强的同步栅栏（fence）。

**内存序五取值**（[AttrDefs.td:487-501](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L487-L501)）：

| 取值 | 含义 |
|------|------|
| `weak` | 假设**没有并发访问**该地址，编译器可最大自由重排 |
| `relaxed` | 可能有并发访问，但不建立 happens-before 关系 |
| `acquire` | 若观察到某 release 写，则建立 happens-before（仅 load）|
| `release` | 与 acquire 配对，建立 happens-before（仅 store）|
| `acq_rel` | 兼具 release 与 acquire（原子操作用，本讲两个 _ptr_tko 不直接用）|

**作用域三取值**（[AttrDefs.td:473-485](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L473-L485)）：`tl_blk`（同一 tile block 内并发）、`device`（同一 GPU 内并发）、`sys`（全系统、跨设备并发）。作用域必须宽到能覆盖所有参与通信的线程，否则会有数据竞争。

#### 4.3.2 核心流程

load 与 store 各自只允许内存序的一个子集，且 `weak` 与 `memory_scope` **互斥**：

- **load** 允许 `weak` / `relaxed` / `acquire`；`weak` 时**不得**带 scope，其余两个**必须**带 scope。
- **store** 允许 `weak` / `relaxed` / `release`；同样的 scope 规则。

这背后的直觉：`weak` 已经声明「无并发」，再谈 scope 毫无意义，因此禁止；`relaxed`/`acquire`/`release` 承认有并发，就必须明确「并发范围多大」，因此 scope 必填。

#### 4.3.3 源码精读

load 的内存模型校验（注意 `weak` 与 scope 的互斥）：

[CudaTile.cpp:3574-3601](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L3574-L3601) `verifyMemoryModelLoad`：先校验 ordering 属于 {WEAK, RELAXED, ACQUIRE}，再分两支——`WEAK` 时若有 scope 则报 "weak load must not have memory scope"；`RELAXED/ACQUIRE` 时若无 scope 则报 "memory scope is required for ... load"。

store 的校验结构对称，只是允许集合换成 {WEAK, RELAXED, RELEASE}：

[CudaTile.cpp:5366-5393](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L5366-L5393) `verifyMemoryModelStore`。

两个 op 的 `verify()` 都很薄，把活儿委托给上面两个公共函数（外加优化提示的公共校验）：

[CudaTile.cpp:4042-4047](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L4042-L4047) `LoadPtrTkoOp::verify` 调用 `verifyMemoryModelLoad`。

[CudaTile.cpp:5399-5404](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L5399-L5404) `StorePtrTkoOp::verify` 调用 `verifyMemoryModelStore`。

枚举属性本身的定义（每个 case 都带 `sinceVersion "13.1"` 与描述）：

[AttrDefs.td:473-485](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L473-L485) `CudaTile_MemoryScopeAttr`：TL_BLK=0 / DEVICE=1 / SYS=2。

[AttrDefs.td:487-501](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L487-L501) `CudaTile_MemoryOrderingSemanticsAttr`：WEAK=0 / RELAXED=1 / ACQUIRE=2 / RELEASE=3 / ACQ_REL=4，并说明 `weak` 允许编译器假设无并发访问。

测试里给出了带 ordering + scope 的标准写法：

[memory_consistency_ops.mlir:49-57](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/memory_consistency_ops.mlir#L49-L57) `load_ptr_tko_scoped`：`load_ptr_tko acquire device %arg0 token = %t : ...`，展示「acquire + device scope」的合法组合。

#### 4.3.4 代码实践

**目标**：亲手验证 weak 与 scope 的互斥规则。

**步骤**：

1. 复制 [memory_consistency_ops.mlir:39-47](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/memory_consistency_ops.mlir#L39-L47) 的 `load_ptr_tko` 用例，跑 `cuda-tile-opt`，确认 `weak`（不带 scope）合法。
2. 故意改成非法写法：`load_ptr_tko weak device %arg0 ...`（weak 却带 scope），再跑 `cuda-tile-opt`。

**需要观察的现象**：第 2 步应触发 verifier 报错。

**预期结果**：报错信息形如 "weak load must not have memory scope"（见 [CudaTile.cpp:3592-3593](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L3592-L3593)）。

**待本地验证**：具体报错文案以本地构建为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `load_ptr_tko acquire` 必须带 scope，而 `weak` 不行？

**参考答案**：`acquire` 承认有并发访问并要建立 happens-before，因此必须声明「并发范围多大」（scope 必填）；`weak` 已经声明「无并发访问」，scope 失去意义，故禁止（[CudaTile.cpp:3591-3599](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L3591-L3599)）。

**练习 2**：load 允许 `acquire` 但不允许 `release`，store 反之，为什么？

**参考答案**：这与 C++ 内存模型一致——读端用 acquire「获取」别人 release 写入的可见性，写端用 release「发布」自己对后续 acquire 的可见性。方向反了就没有意义，所以 verifier 把允许集合分开（[CudaTile.cpp:3579-3588](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L3579-L3588) 与 [CudaTile.cpp:5371-5380](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L5371-L5380)）。

---

### 4.4 类型校验三件套：verifyLoadStoreType / Mask / Padding

#### 4.4.1 概念说明

load/store 的类型合法性不是写在各自的 `verify()` 里，而是抽成了三个**可复用的类型校验函数**，作为 MLIR trait（`TypesMatchWith` / `OptionalTypesMatchWith`）挂到 `CudaTile_LoadOpBase` / `CudaTile_StoreOpBase` 上。这样 load 和 store（以及后续讲义要讲的 view 版本 `load_view_tko` / `store_view_tko`）共享同一套规则。三条规则：

1. **Type**：source 必须是「result 的指针版」——形状相同，且 source 的元素是 `ptr<T>`、result 的元素是 `T`。
2. **Mask**：mask 若存在，形状必须与 result（load）/ destination（store）相同，元素为 i1。
3. **Padding**（仅 load）：padding 若存在，形状与元素类型都必须与 result 相同。

#### 4.4.2 核心流程

校验在 MLIR 的 trait 推断阶段触发。以 load 为例，`CudaTile_LoadOpBase` 串了三条 `TypesMatchWith`：

```
result ──(verifyLoadStoreType)──→ source   // source = ptr<result 的元素>, 同形状
result ──(verifyLoadStoreMask)──→ mask     // mask 形状 = result 形状
result ──(verifyLoadPadding)────→ padding  // padding 形状 & 元素类型 = result
```

任何一条返回 `false`，MLIR 就会报「类型不匹配」并附带 trait 上写的人类可读说明。

#### 4.4.3 源码精读

三个函数的声明在头文件里，注释点明了各自职责：

[Traits.h:23-30](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Traits.h#L23-L30) 声明 `verifyLoadStoreType` / `verifyLoadStoreMask` / `verifyLoadPadding`。

实现集中在 Traits.cpp，逻辑都很短但很关键。`verifyLoadStoreType` 是核心——它用 u3-l2 提过的 `isPointerLike` 统一处理「标量指针」与「指针 tile」，再逐项比对：

[Traits.cpp:19-29](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Traits.cpp#L19-L29) 先确认 src 是 pointer-like、dst 是 `TileType`，再断言「形状相同」且「src 的 pointee 类型 == dst 的元素类型」。这正是「source 是 result 的指针版」的精确定义。

[Traits.cpp:31-38](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Traits.cpp#L31-L38) `verifyLoadStoreMask`：仅要求 mask 与结果同形状（i1 约束已在操作定义里用 `CudaTile_TileOf<[CudaTile_Int1]>` 限定）。

[Traits.cpp:40-50](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Traits.cpp#L40-L50) `verifyLoadPadding`：padding 的形状**与元素类型**都必须与 result 一致（比 mask 多比一项元素类型）。

它们被 trait 绑定到基类的方式：

[Ops.td:2783-2798](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2783-L2798) `CudaTile_LoadOpBase` 把三条校验挂上去，`CudaTile_LoadPtrTkoOp` 只需继承即可获得全部类型校验。

#### 4.4.4 代码实践

**目标**：通过构造非法类型触发三条校验，观察报错。

**步骤**：以 load 为例，依次构造三种非法 IR（每条单独试）：

1. **Type 不匹配**：result 元素类型与 source 的 pointee 不一致。
   ```mlir
   // 示例代码：source 指向 f32，却想读成 i32 —— 非法
   %r, %t = load_ptr_tko weak %ptr_f32 : tile<128xptr<f32>> -> tile<128xi32>, token
   ```
2. **Mask 形状不符**：mask 形状与 result 不同。
   ```mlir
   // 示例代码：result 是 128，mask 却是 64 —— 非法
   %r, %t = load_ptr_tko weak %ptr_f32, %mask64 : tile<128xptr<f32>>, tile<64xi1> -> tile<128xf32>, token
   ```
3. **Padding 类型不符**：padding 元素类型与 result 不同。
   ```mlir
   // 示例代码：result 是 f32，padding 却是 f16 —— 非法
   %r, %t = load_ptr_tko weak %ptr_f32, %mask128, %pad_f16 : tile<128xptr<f32>>, tile<128xi1>, tile<128xf16> -> tile<128xf32>, token
   ```

**需要观察的现象**：每种写法都应被 `cuda-tile-opt` 拒绝。

**预期结果**：分别报「source type is expected a pointer type of result type」「shape of mask must match」「type of paddingValue must match the type of result」（对应 [Ops.td:2788-2797](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2788-L2797) 的 trait 描述字符串）。

**待本地验证**：完整操作数（如 `%ptr_f32`、`%mask64` 的来源）需要先用 `constant` 等构造，建议直接参照 [memory_consistency_ops.mlir:76-84](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/memory_consistency_ops.mlir#L76-L84) 的合法版本改出一处不一致再观察。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `verifyLoadPadding` 要比 `verifyLoadStoreMask` 多比较一项「元素类型」？

**参考答案**：mask 只是「是否访问」的布尔标志，只需形状一致；padding 是被屏蔽位置要填入的**真实数值**，必须与结果同类型才能正确填充，所以还要比元素类型（[Traits.cpp:40-50](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Traits.cpp#L40-L50)）。

**练习 2**：load 的 source 是 `tile<16x32xptr<f32>>`，result 应该是什么类型？

**参考答案**：`tile<16x32xf32>`——形状 16x32 保持不变，元素由 `ptr<f32>` 换成 `f32`（见 [memory_consistency_ops.mlir:44-46](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/memory_consistency_ops.mlir#L44-L46)）。

---

### 4.5 alloca：块内临时分配与对齐

#### 4.5.1 概念说明

`alloca` 在当前 block 内临时分配一块内存，返回一个**标量指针 tile**（`tile<ptr<f32>>`）。它是 13.3 版本引入的（见 `sinceVersion "13.3"`），用于内核需要临时 scratch 缓冲的场景。

三个关键点：

- **元素数与对齐**：`num_elem` 指定分配多少个元素，`alignment` 指定对齐字节数——**必须是 2 的幂**，且不得小于元素类型的自然大小（如 `f32` 至少 4 字节对齐）。
- **生命周期**：仅在所在 block 内有效，block 结束即失效。
- **可见性**：默认返回地址只对当前 tile thread 可见；带 `global` 标记则可被其他 tile thread 访问。

#### 4.5.2 核心流程

```
alloca num_elem = N, alignment = A [global] : tile<ptr<T>>
   │
   ├─ 分配 N 个 T、对齐到 A 字节的块内缓冲
   ├─ 返回标量指针 tile（指向缓冲首地址）
   └─ 后续可用 offset 扩成指针 tile，再 load_ptr_tko / store_ptr_tko 访问
```

分配失败（如请求过大）会 trap。

#### 4.5.3 源码精读

操作定义，注意三个参数与结果类型：

[Ops.td:256-294](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L256-L294) `CudaTile_AllocaOp`：参数为 `num_elem`（非负 I64Attr）、`alignment`（非负 I64Attr）、`global`（可选 UnitAttr）；结果为 `CudaTile_ScalarTileOf<CudaTile_PointerType>`（即标量指针 tile）。描述里明确「lifetime of the allocation is limited to the block」。

校验逻辑在共享头里，模板化以备复用：

[SharedVerifiers.h:178-203](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h#L178-L203) `verifyAlloca`：先断言 alignment 是 2 的幂（`alignment > 0 && (alignment & (alignment-1)) == 0`），再按 pointee 类型算出自然字节大小（i1 算 1 字节、其他整数按位宽/8、浮点按 `APFloat::getSizeInBits/8`），最后要求 `alignment >= sizeInBytes`。

`AllocaOp::verify` 把调用委托给模板：

[CudaTile.cpp:1456-1457](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1456-L1457) `AllocaOp::verify` 调用 `verifyAlloca<AllocaOp, PointerType>(*this)`。

#### 4.5.4 代码实践

**目标**：写一个合法的 alloca，再构造对齐非法的版本观察报错。

**步骤**：

1. 合法版本（对齐 128 ≥ f32 自然大小 4）：
   ```mlir
   // 示例代码
   %0 = alloca num_elem = 64, alignment = 128 : tile<ptr<f32>>
   ```
   跑 `cuda-tile-opt`，确认合法。
2. 非法版本（alignment = 3，不是 2 的幂）：
   ```mlir
   // 示例代码：3 不是 2 的幂 —— 非法
   %0 = alloca num_elem = 64, alignment = 3 : tile<ptr<f32>>
   ```
3. 另一种非法（alignment 小于自然大小，如 `alignment = 1` 给 `ptr<f32>`，因为 f32 需要 ≥ 4）：
   ```mlir
   // 示例代码：f32 至少 4 字节对齐，1 不够 —— 非法
   %0 = alloca num_elem = 64, alignment = 1 : tile<ptr<f32>>
   ```

**需要观察的现象**：第 2、3 步被 verifier 拒绝。

**预期结果**：分别报 "'alignment' must be power of two" 与 "'alignment' (1) must be at least the natural size (4 bytes) ..."（[SharedVerifiers.h:183-184](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h#L183-L184) 与 [SharedVerifiers.h:196-200](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h#L196-L200)）。

**待本地验证**：alloca 是 13.3 引入，确保你的构建字节码版本 ≥ 13.3。

#### 4.5.5 小练习与答案

**练习 1**：`alloca num_elem = 64, alignment = 128 : tile<ptr<i1>>` 中，i1 的自然对齐是多少？

**参考答案**：1 字节（[SharedVerifiers.h:190-192](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h#L190-L192) 中 `isIntOne ? 1 : ...`），因为 i1 在内存中按整字节存取（与 load/store 的 i1 规范一致）。

**练习 2**：alloca 返回的是标量指针 tile 还是「指针 tile」？

**参考答案**：标量指针 tile `tile<ptr<T>>`（结果约束是 `CudaTile_ScalarTileOf<CudaTile_PointerType>`，[Ops.td:272-275](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L272-L275)）。要访问多个元素，需先用 `offset` 等手段把它扩成指针 tile。

---

## 5. 综合实践

把本讲五个操作（`make_token` / `join_tokens` / `load_ptr_tko` / `store_ptr_tko` / `alloca`）串成一个有真实顺序约束的小内核。

**任务**：写一个 `entry` 内核，完成下面三件事，并用 token 保证它们的先后顺序——

1. 用 `alloca` 申请一块 4 个 f32 的临时缓冲（对齐 16）。
2. 用 `iota` + `offset` 把一个**外部传入的** `tile<ptr<f32>>` 基址扩成 `tile<4xptr<f32>>`，用 `load_ptr_tko weak` 从全局显存读 4 个 f32。
3. 用 `store_ptr_tko weak` 把读到的数据写进 alloca 的缓冲（先 `offset` 出 4 个地址），并用 `make_token`/`join_tokens` 把「读」和「写」串成「先读后写」。

**参考骨架**（示例代码，需你补全类型与操作数）：

```mlir
cuda_tile.module @ex {
  entry @kernel(%base : tile<ptr<f32>>) {
    // 1. 临时缓冲
    %buf = alloca num_elem = 4, alignment = 16 : tile<ptr<f32>>

    // 2. 构造 4 个连续地址并 weak 读
    %idx = iota : tile<4xi32>
    %b1   = reshape %base : tile<ptr<f32>> -> tile<1xptr<f32>>
    %b4   = broadcast %b1 : tile<1xptr<f32>> -> tile<4xptr<f32>>
    %src  = offset %b4, %idx : tile<4xptr<f32>>, tile<4xi32> -> tile<4xptr<f32>>
    %data, %t_read = load_ptr_tko weak %src : tile<4xptr<f32>> -> tile<4xf32>, token

    // 3. 构造缓冲的 4 个写地址
    %dbuf1 = reshape %buf : tile<ptr<f32>> -> tile<1xptr<f32>>
    %dbuf4 = broadcast %dbuf1 : tile<1xptr<f32>> -> tile<4xptr<f32>>
    %dst   = offset %dbuf4, %idx : tile<4xptr<f32>>, tile<4xi32> -> tile<4xptr<f32>>

    // 用 token 强制「先读完再写」
    %t_write = store_ptr_tko weak %dst, %data token = %t_read
      : tile<4xptr<f32>>, tile<4xf32> -> token
    return
  }
}
```

**验证**：用 `cuda-tile-opt` 跑通，确认无 verifier 报错；再尝试**去掉** `token = %t_read`，体会「不接 token 时读和写就没有顺序约束」——这正是 token-ordered 模型的核心。

**思考题**：如果还想要「写完成后再做某个 print」，你会怎么接 token？（提示：把 `%t_write` 喂给 `print_tko` 不行，因为 print 不消费输入 token；正确做法见下一讲 u5-l2 或查阅 `print_tko` 的接口——本讲聚焦 ptr 访存与 token 排序。）

## 6. 本讲小结

- **token-ordered（`_tko`）操作默认不受程序顺序约束**，编译器可自由重排；需要排序时只能用 token 显式串接。`make_token` 造新 token，`join_tokens`（≥2 个）汇合多个 token。
- **`load_ptr_tko` / `store_ptr_tko`** 基于指针 tile 做 gather/scatter 访存，都产出 token；load 还返回数据 tile。二者都支持可选 `mask`（屏蔽）与 load 专属 `padding`（填充）。
- **内存序与作用域**：load 允许 weak/relaxed/acquire，store 允许 weak/relaxed/release；`weak` 与 `memory_scope` 互斥，其余必须带 scope，由 `verifyMemoryModelLoad` / `verifyMemoryModelStore` 强制。
- **类型校验三件套**抽成可复用函数：`verifyLoadStoreType`（source 是 result 的指针版）、`verifyLoadStoreMask`（mask 同形状）、`verifyLoadPadding`（padding 同形状同类型），以 trait 形式挂在 `LoadOpBase`/`StoreOpBase` 上。
- **`alloca`**（13.3）做块内临时分配，返回标量指针 tile；alignment 必须是 2 的幂且不小于元素自然大小，生命周期限于所在 block。
- **地址构造链**的终点在本讲：u4-l1 的 `iota → reshape+broadcast → offset` 产出的指针 tile，最终喂给 `load_ptr_tko` 读取。

## 7. 下一步学习建议

本讲只覆盖了「基于指针」的访存。CUDA Tile 还有一套基于**视图（View）**的访存 `load_view_tko` / `store_view_tko`，用 `partition_view` / `strided_view` / `gather_scatter_view` 描述全局张量的分块几何——这正是 **u5-l2（视图加载与存储：掩码与越界填充）** 的主题，它会复用本讲的 token、mask、padding、内存序等全部概念，请先掌握本讲再进入。

如果你对**原子操作**（`atomic_rmw_tko` / `atomic_cas_tko` / `atomic_red_view_tko`）和更深的内存模型感兴趣，那是 **u5-l3（原子操作与内存序/作用域）** 的内容，它在本讲的 weak/relaxed/acquire/release 基础上再引入 acq_rel 与更细的 RMW 语义。

若想从工程角度理解这些操作如何被序列化为字节码、又如何参与优化 Pass（如 FuseFMA、LoopSplit），可跳读 **u7（字节码二进制格式）** 与 **u9（优化器与变换 Pass）** 单元。
