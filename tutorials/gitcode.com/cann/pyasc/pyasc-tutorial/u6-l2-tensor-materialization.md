# 张量物化与 UB 内存分配：MaterializeTensor、HoistUBAllocation 与 UnifyPipe

## 1. 本讲目标

上一讲（u6-l1）我们看完了 16 个 Pass 的全景图。本讲下钻 lowering 阶段中相互咬合的四个 Pass，学完后你应该能够：

1. 说清 `ascendc.local_tensor_auto` 这个「惰性张量」声明是如何被四个 Pass 协作改写成真实的 pipe/queue/tbuf 分配的，以及为什么用户因此**完全不用手写 UB 内存分配**。
2. 掌握 HoistUBAllocation 的提升判据（无 input/output 标记 + 支配关系成立），理解它对循环、嵌套循环、分支场景下分配正确性的处理。
3. 解释 UnifyPipe 在 01_add（手动风格）、02_add_framework（框架风格）、LocalTensorAuto（惰性风格）三条路径上分别做了什么——其中前两条它的答案是「什么都不做」，这背后的原因比「做了什么」更有教学价值。
4. 建立「Pass 执行顺序即设计意图」的读码意识：为什么必须先判方向（InputOutputTensor）、再提升（HoistUBAllocation）、再物化（MaterializeTensor）、最后合并 pipe（UnifyPipe）。

## 2. 前置知识

本讲默认你已读过 u2-l2（Tensor 抽象）、u2-l6（TPipe/TQue 框架）、u3-l4（Pass 流水线）与 u6-l1（Pass 全景）。用三段话把需要的背景补齐。

**三种创建 LocalTensor 的风格。** pyasc 前端有三条路得到一个 UB 上的 LocalTensor：

- 手动风格（01_add）：`asc.LocalTensor(dtype, pos, addr, tile_size)`，用户自己给出逻辑位置、字节偏移和长度，自己配 `set_flag/wait_flag` 同步；
- 框架风格（02_add_framework）：`asc.TPipe() + asc.TQue() + alloc_tensor/enque/deque/free_tensor`，队列框架接管内存与同步；
- 惰性风格（本讲主角）：`asc.LocalTensorAuto(dtype, shape)`，只声明「我要一块能放这个形状数据的 UB」，位置、偏移、同步全部交给编译器。

**惰性张量的 IR 形态。** `ascendc.local_tensor_auto` 是一个纯声明式的 Op：结果类型是 `!ascendc.local_tensor<形状xdtype>`，两个 UnitAttr `input`/`output` 记录数据流向，可变参数 `dynamicShape` 携带动态形状的各个维度（i64）。它**不对应任何 Ascend C 语句**，发射层无法直接处理它——必须先被 Pass 改写成 `pipe + queue/tbuf + init + alloc` 的组合。这个「改写」就是本讲的四个 Pass 做的事。

**MLIR 改写基础设施。** 本讲四个 Pass 里有三个走 `RewritePatternSet + applyPatternsAndFoldGreedily`（贪心模式重写驱动）：每个 Pattern 实现 `matchAndRewrite(op, rewriter)`，驱动器反复应用直到不动点。第四个（UnifyPipe）是全函数遍历的一次性改写。另外需要知道 MLIR 的**支配关系（dominance）**：一个 Value 的定义必须支配它的所有使用点；把 Op 移出区域（region）前必须检查其操作数在区域外仍然可用，否则破坏 SSA 合法性。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lib/Dialect/Asc/Transforms/InputOutputTensor.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InputOutputTensor.cpp) | 给 local_tensor_auto 打 input/output 方向标记，并修补跨区域、双向使用的边角情况 |
| [lib/Dialect/Asc/Transforms/HoistUBAllocation.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/HoistUBAllocation.cpp) | 把纯临时 local_tensor_auto 提升出循环/分支，到达函数体 |
| [lib/Dialect/Asc/Transforms/MaterializeTensor.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/MaterializeTensor.cpp) | 把 local_tensor_auto 物化成 pipe + queue/tbuf + init + alloc(+free) |
| [lib/Dialect/Asc/Transforms/UnifyPipe.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/UnifyPipe.cpp) | 把函数内多个 ascendc.pipe 合并为入口处的一个 |
| [include/ascir/Dialect/Asc/Utils/Utils.h](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Utils/Utils.h) | HoistOpPattern 泛型模板，提供「提升出区域」的通用骨架 |
| [include/ascir/Dialect/Asc/IR/Ops.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Ops.td) | `AscendC_LocalTensorAutoOp` 的 td 定义 |
| [include/ascir/Dialect/Asc/IR/Interfaces.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Interfaces.td) | `DataCopyOpInterface::getDirection`，按 dst/src 类型推导搬运方向 |
| [python/asc/runtime/compiler.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py) | 四个 Pass 的调度顺序（lowering 阶段）与 dump 时机 |
| [python/asc/language/core/tensor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py) | 前端侧：LocalTensorAuto 与手动 LocalTensor 的构造入口 |
| [python/test/kernels/insert_sync/test_vadd.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/kernels/insert_sync/test_vadd.py) | 惰性风格的完整可运行 vadd 示例（实践主素材） |
| [test/Dialect/AscendC/Transforms/](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/materialize-tensor.mlir) | 四个 Pass 各自的 lit 测试：输入 IR + FileCheck 期望输出，是最精确的「前后对照表」 |

四个 Pass 在 Passes.td 中的声明（依赖声明见 [Passes.td:49-69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L49-L69)），构造函数在 [Passes.h:28-35](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.h#L28-L35) 注册，均作用于 `func::FuncOp`（经 `addNestedPass` 挂到模块上，u6-l1 已讲）。

## 4. 核心概念与源码讲解

### 4.1 全景：一次惰性声明的四步旅程

#### 4.1.1 概念说明

先看调度代码，把四个 Pass 的执行顺序钉死。lowering 阶段（[compiler.py:120-131](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L120-L131)）：

```python
passes.ascendc.add_input_output_tensor(pm)    # ① 判方向
passes.ascendc.add_hoist_ub_allocation(pm)    # ② 提升
passes.ascendc.add_materialize_tensor(pm)     # ③ 物化
passes.ascendc.add_unify_pipe(pm)             # ④ 合并 pipe
```

这不是随便排的，而是严格的依赖链：

- ② 的提升判据是「没有 input/output 标记的张量才可提升」，标记由 ① 打上；
- ③ 物化时按标记选择路径（VECIN/VECOUT 队列 or VECCALC 缓冲），同样依赖 ①；
- ② 必须发生在 ③ 之前——提升一个单行的 `local_tensor_auto` 声明很容易，等它膨胀成 pipe+queue+init+alloc+free 五件套之后再想整体搬出循环就难了；
- ③ 每物化一个张量就新建一个 `ascendc.pipe`，于是 ④ 负责把 N 个 pipe 合成 1 个。

另外 optimizing 阶段在 insert_sync 链之后**又跑了一次 UnifyPipe**（[compiler.py:137-142](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L137-L142)），因为 InsertSync 自己也会新建 pipe（详见 4.5.1）。

#### 4.1.2 核心流程

以惰性风格的 vadd（`python/test/kernels/insert_sync/test_vadd.py`）为例，循环体里三个惰性声明经历四步：

```text
用户 Python:
    x_local = asc.LocalTensorAuto(x.dtype, tile_length)   # 搬入目的
    y_local = asc.LocalTensorAuto(y.dtype, tile_length)   # 搬入目的
    z_local = asc.LocalTensorAuto(z.dtype, tile_length)   # 搬出来源

codegen.mlir (Pass 前):                    scf.for {
    %x = ascendc.local_tensor_auto() : <2048xf32>
    %y = ascendc.local_tensor_auto() : <2048xf32>
    %z = ascendc.local_tensor_auto() : <2048xf32>
    ... data_copy_l2 %x, %gm ...           # gm_ubuf 方向
    ... data_copy_l2 %gm, %z ...           # ubuf_gm 方向
}

① InputOutputTensor  →  %x %y 加 `input` 标记、%z 加 `output` 标记
② HoistUBAllocation  →  三个都有标记，一个都不提升（保持原位）
③ MaterializeTensor  →  %x %y → pipe+queue<vecin,1>+init_queue+alloc(+free)
                         %z → pipe+queue<vecout,1>+init_queue+alloc(+free)
④ UnifyPipe          →  三个 pipe 合并为函数入口的一个
```

#### 4.1.3 源码精读

惰性张量在前端的诞生地是 [tensor.py:453-484](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L453-L484)：形状全是编译期 int 时走静态分支，只建类型不传维度；含运行期维度时把每个维度物化成 i64 值塞进 `dynamicShape`：

```python
handle = global_builder.get_ir_builder().create_asc_LocalTensorAutoOp(
    ir.get_local_tensor_type(dtype.to_ir(), shape))          # 静态形状
...
handle = global_builder.get_ir_builder().create_asc_LocalTensorAutoOp(
    ir.get_local_tensor_type(dtype.to_ir()), False, False, new_shape)  # 动态形状
```

（[tensor.py:475-481](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L475-L481)。这里的 `False, False` 正是 input/output 两个 UnitAttr 的初值——创建时无人知道方向。）

IR 侧的 Op 定义在 [Ops.td:127-140](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Ops.td#L127-L140)：

```tablegen
def AscendC_LocalTensorAutoOp : APIOp<"local_tensor_auto", "LocalTensorAuto"> {
  let summary = "Create virtual tensor with automatic allocation semantic";
  let arguments = (ins UnitAttr:$input, UnitAttr:$output, Variadic<I64>:$dynamicShape);
  let results = (outs AscendC_LocalTensor:$result);
```

对照手动风格：`asc.LocalTensor(dtype, pos, addr, tile_size)` 走的是 [tensor.py:236-244](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L236-L244)，生成的是 `ascendc.local_tensor_v2`，位置/地址/长度作为**操作数**直接写进 IR。两者最大的区别：local_tensor_v2 自带完整排布信息，Pass 无需（也无法）替它分配；local_tensor_auto 只有「形状 + 方向」，排布完全待定。

最后一条背景链路：`need_insert_sync`（[IR.cpp:570-574](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L570-L574)）就是靠「模块里是否存在 LocalTensorAutoOp」来判定要不要触发自动同步链——惰性张量是 insert_sync 三态语义（u3-l4）的信号源。[test_vadd.py:16](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/kernels/insert_sync/test_vadd.py#L16) 的注释也明说了这一点。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证「惰性风格触发 insert_sync，另两种风格不触发」。
2. **操作步骤**：
   - 阅读三个文件中 LocalTensor 的创建行：[examples/01_add/add.py:45-47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L45-L47)、[examples/02_add_framework/add_framework.py:54](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L54)、[test_vadd.py:27-29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/kernels/insert_sync/test_vadd.py#L27-L29)；
   - 对照 [compiler.py:180-181](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L180-L181) 中 `insert_sync is None` 时调用 `mod.need_insert_sync()` 的逻辑。
3. **需要观察的现象**：三个文件分别生成哪种 IR 声明（local_tensor_v2 / TQueBindAllocTensor / local_tensor_auto）。
4. **预期结果**：只有 test_vadd.py 含 local_tensor_auto，因此只有它会走 EraseSync→HoistQueBind→InsertSync 链。01_add 保留手写 set_flag/wait_flag，02_add_framework 依赖队列内置同步。
5. 运行层面的验证放到第 5 节综合实践，此处为源码阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：既然惰性风格这么省事，为什么 01_add 还要教手动排布？

**答案**：手动风格让用户精确控制双缓冲的地址排布（`buffer_size`、`buf_id * tile_length` 切片）与事件号配对，是理解硬件流水机制的教学入口，也是性能敏感场景（显式乒乓、跨队列复用缓冲）的手段；惰性风格把这一切交给编译器，正确性优先、省心优先。02_add_framework 的队列风格与惰性风格在 IR 后期形态上高度接近（都落到 queue/tbuf），差别是排布决策分别由用户与 Pass 做出。

**练习 2**：`dynamicShape` 参数为什么是 `Variadic<I64>` 而不是把形状编进类型？

**答案**：动态形状的维度是运行期值（如 Host 传入的维度参数），类型里只能放编译期属性；动态维必须以 SSA 值（操作数）形式携带，物化时才能用 `arith.muli` 现场算出字节数（见 4.4.2）。静态形状则直接编进 `!ascendc.local_tensor<64xf32>` 类型。

### 4.2 InputOutputTensor：先判方向、再打标记

#### 4.2.1 概念说明

MaterializeTensor 需要知道每个惰性张量该映射到哪个逻辑位置：从 GM 搬进来的应放 VECIN 队列，往 GM 搬出去的应放 VECOUT 队列，纯中间结果应放 VECCALC 缓冲。但前端创建张量时并不知道它日后被怎么用——方向信息只存在于**使用点**（data_copy 的方向）里。InputOutputTensor 的职责就是做一次使用点分析，把「搬入/搬出」回填成 `input`/`output` 两个 UnitAttr，并顺手修补两个会让「一张张量一个位置」策略失效的边角情况。

方向本身不是这个 Pass 发明的。`DataCopyOpInterface::getDirection`（[Interfaces.td:79-95](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Interfaces.td#L79-L95)）按 dst/src 类型组合推导出四种方向：

```cpp
if (isa<GlobalTensorType>(srcType)) {
    if (isa<GlobalTensorType>(dstType)) return CopyDirection::gm_gm;
    if (isa<LocalTensorType>(dstType)) return CopyDirection::gm_ubuf;   // 搬入
} else if (isa<LocalTensorType>(srcType)) {
    if (isa<GlobalTensorType>(dstType)) return CopyDirection::ubuf_gm;  // 搬出
    if (isa<LocalTensorType>(dstType)) return CopyDirection::ubuf_ubuf; // UB 内部倒腾
}
```

「方向由操作数类型推导」意味着任何一个持有 DataCopyOpInterface 的 Op 都能自报方向，InputOutputTensor 只是这个能力的消费方之一（InsertSync 也是，见 u6-l3）。

#### 4.2.2 核心流程

Pass 主体（[InputOutputTensor.cpp:101-118](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InputOutputTensor.cpp#L101-L118)）三步走：

```text
runOnOperation:
  1. setInOutTensors(funcOp)      遍历 local_tensor_auto 的每个 user：
       user 是 gm_ubuf 方向 data_copy → input = true
       user 是 ubuf_gm 方向 data_copy → output = true
     再遍历 scf.for / scf.if：若区域结果被外部的 ubuf_gm 拷贝使用
       → createDataCopyIfNeeded 补搭桥
  2. fixInOutTensor(funcOp)       既是 input 又被 ubuf_gm 用作源的张量：
       拆出新的 output 张量 + UB→UB 搭桥拷贝
  3. 跑 LocalTensorAutoOp 的规范化（canonicalization）清理残局
```

#### 4.2.3 源码精读

打标记的核心循环在 [InputOutputTensor.cpp:51-74](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InputOutputTensor.cpp#L51-L74)，`op->getUsers()` 拿到该张量的全部使用者，按方向置位后写回属性：

```cpp
funcOp.walk([](ascendc::LocalTensorAutoOp op) {
    bool input = false;
    bool output = false;
    for (Operation* user : op->getUsers()) {
        if (auto copyOp = dyn_cast<ascendc::DataCopyOp>(user)) {
            auto dir = copyOp.getDirection();
            if (dir == ascendc::CopyDirection::gm_ubuf) { input = true; continue; }
            if (dir == ascendc::CopyDirection::ubuf_gm) { output = true; continue; }
        }
    }
    op.setInput(input);
    op.setOutput(output);
});
```

这段代码解决 4.1 图里的第一问。对照 lit 测试 [input-output-tensor.mlir:15-24](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/input-output-tensor.mlir#L15-L24)：`%0`、`%1` 是 gm_ubuf 拷贝的目的地 → 得 `input`；`%2` 是 ubuf_gm 拷贝的源 → 得 `output`。第二个用例（[29-36 行](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/input-output-tensor.mlir#L29-L36)）里 `%1` 只参与 ubuf_ubuf 拷贝 → 两个标记都不设，之后会被物化成 VECCALC 缓冲。

两个「搭桥」修补函数处理的是更刁钻的场景。[createDataCopyIfNeeded（32-49 行）](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InputOutputTensor.cpp#L32-L49)在**区域操作**（scf.for/scf.if）的结果被外部 `ubuf_gm` 拷贝直接使用时介入：

```cpp
auto dst = builder.create<ascendc::LocalTensorAutoOp>(
    op->getLoc(), type, /*input*/ false, /*output*/ true, ValueRange{});
builder.setInsertionPointAfter(op);
builder.create<ascendc::DataCopyL2Op>(op->getLoc(), dst, use.get(), calCount);
copyOp.setSrc(dst);
```

它在区域**前**新建一个带 `output` 标记的张量，在区域**后**补一条 `区域结果 → 新张量` 的 UB→UB 拷贝，再把外部的 GM 拷贝改指新张量。这样「从区域里透传出来的值」就有了明确的外部落点，后续 MaterializeTensor 能把它物化成 VECOUT 队列。

[fixInOutTensor（76-99 行）](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InputOutputTensor.cpp#L76-L99)处理同一张量「既要搬入、又要搬出」的冲突：input 张量本该映射 VECIN，但它又被某条 `ubuf_gm` 拷贝当作源。因为一个张量只能映射一个位置，函数就保留 input 张量原样，为搬出方向**另建**一个 output 张量并插入 `DataCopyL2Op(inTensor → outTensor)` 搭桥，再把那条 GM 拷贝的源换掉。读这段代码有个陷阱值得提醒：89 行的 `return builder.setInsertionPoint(owner);` 不是「设置完插入点再返回」——`setInsertionPoint` 返回 void，这是 C++ 里 `return void表达式;` 的合法写法，语义是**遇到第一个不是 ubuf_gm 拷贝的使用者就立刻退出整个 lambda**（该张量没有双向冲突需要修补）。同理 36-37 行的 `if (!copyOp || ...) return;` 是放弃整个函数而非 continue。两处都是「读开源 C++ 容易看走眼」的典型。

#### 4.2.4 代码实践

1. **实践目标**：在不运行任何东西的前提下，手工预测 InputOutputTensor 的输出。
2. **操作步骤**：
   - 打开 [input-output-tensor.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/input-output-tensor.mlir)，遮住文件头部 `// CHECK:` 注释，只看 15-24 行与 29-36 行的输入函数；
   - 对每个 `%N = ascendc.local_tensor_auto()` 写下你预测的标记组合。
3. **需要观察的现象**：预测值 vs CHECK 行给出的真实输出。
4. **预期结果**：`@input_output_tensor_ub_gm` 中 `%0`→input、`%1`→input、`%2`→output；`@input_output_tensor_ub_ub` 中 `%0`→input、`%1`→无标记（将物化为 VECCALC）。
5. 全对说明你已掌握方向推导规则；此实践零成本、可随时重做。

#### 4.2.5 小练习与答案

**练习 1**：一个惰性张量先被 `data_copy(gm → 它)` 填充，又被 `data_copy(它 → gm)` 读出，还不止一处计算读它。InputOutputTensor 之后它的标记是什么？物化到哪？

**答案**：`input = true, output = true`。此时 fixInOutTensor 不会拆分它（拆分条件是 `input && !output` 修补路径，而 setInOutTensors 已直接把 output 置位，两张标记并存时 79 行的守卫 `if (!inTensor.getInput() || inTensor.getOutput()) return;` 直接跳过）。随后 MaterializeTensor 的 getPosition 优先看 output（44 行前先判 `getOutput()`），映射到 VECOUT——「输出侧语义优先」。

**练习 2**：为什么打标记的是 Pass 而不是前端在 `data_copy` 里顺手记录？

**答案**：方向是张量全部使用点的聚合属性，创建点无从知晓；data_copy 单次调用只知道自己的方向，且 IR 尚在构建中、使用关系未定型。放在 Pass 里做一次性全函数分析，还能顺带处理跨区域使用这类前端难以察觉的形态，也让 local_tensor_auto 的创建逻辑保持零负担。

### 4.3 HoistUBAllocation：把纯临时张量提升出循环与分支

#### 4.3.1 概念说明

惰性风格的张量经常写在循环体里（test_vadd.py 就是）。若原样物化，每圈迭代都要「建队列 + init + alloc + free」，且循环携带这些声明会让 InsertSync 的依赖分析变复杂。观察发现：**没有 input/output 标记的张量**（纯中间结果）的提升不改变语义——它每圈只是被重新算一遍，声明放循环外与循环内等价；而带标记的张量与「哪一圈搬进哪块数据」强绑定，绝不能提升。HoistUBAllocation 就做这一件事：把可提升的 `local_tensor_auto` 从 scf.for/scf.if 区域里搬到外层，直到函数体为止。

注意时序：此刻还没物化，搬的是一个单行声明，所以提升操作极其轻量——这正是「先提升、后物化」顺序的收益。

#### 4.3.2 核心流程

Pass 本体只有 17 行（[HoistUBAllocation.cpp:35-45](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/HoistUBAllocation.cpp#L35-L45)），真正的逻辑在泛型模板 `HoistOpPattern`（[Utils.h:23-45](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Utils/Utils.h#L23-L45)）：

```text
matchAndRewrite(op):
  parent = op 的父操作（区域宿主，如 scf.for / scf.if）
  ① parent 已是 FuncOp            → 失败（到顶了，无事可做）
  ② hoistable(op) 为假             → 失败（有 input/output 标记）
  ③ 存在操作数不支配 parent        → 失败（搬出去操作数就不可用）
  ④ 在 parent 之前克隆 op，用克隆结果替换原 op 的全部使用，删除原 op
```

每次重写把 Op 提升一层；贪心驱动反复应用直到不动点，于是嵌套循环里的张量会被逐层「爬」到函数体。判据③是 SSA 正确性的守门员：动态形状的 local_tensor_auto 带着维度操作数，如果某个维度定义在循环体内（比如循环归纳变量派生的值），把它搬出循环就会引用一个尚未定义的值。

#### 4.3.3 源码精读

Pass 侧只注册一个模式，可提升判据一行（[HoistUBAllocation.cpp:29-33](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/HoistUBAllocation.cpp#L29-L33)）：

```cpp
struct HoistTensor : ascendc::HoistOpPattern<ascendc::LocalTensorAutoOp> {
    bool hoistable(ascendc::LocalTensorAutoOp op) const override { return !op.getInput() && !op.getOutput(); }
};
```

通用骨架的关键三行（[Utils.h:31-43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Utils/Utils.h#L31-L43)）：

```cpp
if (isa<func::FuncOp>(parent))
    return failure();
if (!hoistable(op))
    return failure();
DominanceInfo di;
bool dominatedByOperands =
    llvm::all_of(op->getOperands(), [&](Value opnd) { return di.dominates(opnd, parent); });
if (!dominatedByOperands)
    return failure();
rewriter.setInsertionPoint(parent);
rewriter.replaceOp(op, rewriter.clone(*op.getOperation())->getResults());
```

`setInsertionPoint(parent)` 把插入点放在**父操作之前**（即父操作所在块中、父操作的前面），克隆后原操作被整体替换删除。lit 测试是最好的说明书——[hoist-ub-allocation.mlir:17-25](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/hoist-ub-allocation.mlir#L17-L25) 输入的循环体里有三个张量，无标记的 `%2` 被提到 `scf.for` 之前：

```text
// CHECK:      %0 = ascendc.local_tensor_auto() : <64xf32>     ← 提升（原 %2）
// CHECK-NEXT:  scf.for ... {
// CHECK-NEXT:    %1 = ascendc.local_tensor_auto() input ...   ← 留守
// CHECK-NEXT:    %2 = ascendc.local_tensor_auto() output ...  ← 留守
```

[嵌套循环用例（34-44 行）](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/hoist-ub-allocation.mlir#L34-L44)进一步显示无标记张量穿过**两层**循环直达函数体——证明贪心驱动的逐层爬升。

#### 4.3.4 代码实践

1. **实践目标**：验证「带标记张量留守、无标记张量爬到顶、支配不满足则放弃」三条规则。
2. **操作步骤**：
   - 精读 [hoist-ub-allocation.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/hoist-ub-allocation.mlir) 两个用例的输入与 CHECK；
   - 构造（纸面）第三个用例：`scf.for` 内放一个 `ascendc.local_tensor_auto(%arg)`，其中 `%arg` 是循环归纳变量——推演结果；
   - 若本机构建过 devtools（`PYASC_SETUP_DEVTOOLS=1`，见 u7-l5），可把纸面用例存成 `.mlir` 用 `ascir-opt -ascendc-hoist-ub-allocation` 实跑；未构建则标注「待本地验证」。
3. **需要观察的现象**：归纳变量作维度时，张量是否留在循环内。
4. **预期结果**：留守。`di.dominates(归纳变量, for)` 为假——归纳变量定义在循环体内，不支配循环操作，判据③拦截。这正是该判据存在的原因。
5. lit 测试文件本身即是标准答案，可对照。

#### 4.3.5 小练习与答案

**练习 1**：提升为什么以「父操作」为单位一层层做，而不直接判断「是否已在函数体」一步到位？

**答案**：`setInsertionPoint(parent)` 把克隆放到父操作之前，只保证新位置在当前区域宿主外一层、仍在父操作所在的块内。要一步到函数体，需要逐层确认每个外层区域的块结构并选好插入点，通用模板会复杂得多；交给贪心驱动迭代重写，每次只做「安全的一小步」，嵌套场景自动收敛，代码保持极简。

**练习 2**：把提升放在 MaterializeTensor **之后**会发生什么？

**答案**：届时张量已膨胀为 pipe+queue+init+alloc+free 五个操作，其中 alloc 的结果被循环体内的计算使用、free 要落在使用之后，整体搬出循环还要证明「每圈重新分配」与「循环外一次分配」语义等价（涉及队列状态、同步事件），几乎不可安全自动化。在声明层面提升则 trivially 等价——这是「先提升后物化」顺序的核心动机。

### 4.4 MaterializeTensor：惰性声明 → 真实的队列与缓冲

#### 4.4.1 概念说明

MaterializeTensor 是四连击的火力输出：把每个 `local_tensor_auto` 替换为一段完整的、与 Ascend C 框架 API 一一对应的 IR。它按下表分流（判据来自 InputOutputTensor 打好的标记）：

| 标记 | 逻辑位置 | 物化产物 |
| --- | --- | --- |
| output | VECOUT | `pipe + queue<vecout,1> + pipe.init_queue + que_bind.alloc_tensor`（函数末尾补 `que_bind.free_tensor`） |
| input | VECIN | 同上，位置为 vecin |
| 无标记（纯临时） | VECCALC | `pipe + tbuf<veccalc> + pipe.init_buffer + tbuf.get_tensor`（无需 free） |

VECCALC 走 TBuf 路线、无队列无释放：临时缓冲不参与生产者-消费者同步，借出即用。VECIN/VECOUT 走队列路线：队列天然承载「搬运与计算的重叠」语义，后续 InsertSync（u6-l3）会围绕队列插入 enque/deque 与事件。队列深度固定为 1（`QueueType::get(ctx, pos, 1)`），惰性风格不追求用户手排的双缓冲深度——那是手动/框架风格的长项。

#### 4.4.2 核心流程

单个张量的物化流程（[MaterializeTensor.cpp:46-77](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/MaterializeTensor.cpp#L46-L77)）：

```text
1. 算字节数 length:
     静态形状:  length = 元素总数 × 位宽 / 8                     (i64 常量)
     动态形状:  length = (位宽/8) × dynamicShape[0] × ... × [n-1]  (arith.muli 链)
2. 新建 pipe（每个张量一个！）
3. 无标记 → tbuf<veccalc> + pipe.init_buffer(pipe, tbuf, length)
            + tbuf.get_tensor(tbuf) 替换原张量
4. 有标记 → queue<pos,1> + pipe.init_queue(pipe, queue, 1, length)
            + que_bind.alloc_tensor(queue) 替换原张量
            + 在所在块终结符前补 que_bind.free_tensor(queue, tensor)
```

字节公式：设元素数为 \( N \)、元素位宽为 \( b \) 位，则

\[ \text{length} = N \times b \div 8 \ \text{（字节）} \]

例如 64 个 f32（位宽 32）：\( 64 \times 32 \div 8 = 256 \) 字节——这正是 lit 测试里 `%c256_i64` 的来源。

#### 4.4.3 源码精读

字节数计算（[MaterializeTensor.cpp:51-60](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/MaterializeTensor.cpp#L51-L60)）：

```cpp
if (type.hasStaticShape()) {
    length = consts.i64(type.getNumElements() * type.getElementTypeBitWidth() / CHAR_BIT);
} else {
    length = consts.i64(type.getElementTypeBitWidth() / CHAR_BIT);
    for (auto dim : op.getDynamicShape()) {
        length = rewriter.create<arith::MulIOp>(loc, length, dim);
    }
}
```

动态形状时先落一个「每元素字节数」常量，再与每个维度做乘法——注意操作数直接用 `local_tensor_auto` 的 `dynamicShape` 值，这就是 4.3 支配判据要保护的东西。

TBuf 分支（[62-68 行](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/MaterializeTensor.cpp#L62-L68)），三步建出 Ascend C 的 `TPipe::InitBuffer(buf, len)` + `TBuf::Get<T>()` 等价物：

```cpp
auto bufferTy = ascendc::TBufType::get(op.getContext(), ascendc::TPosition::VECCALC);
Value buffer = rewriter.create<ascendc::TBufOp>(loc, bufferTy);
rewriter.create<ascendc::TPipeInitBufferOp>(loc, pipe, buffer, length);
rewriter.replaceOpWithNewOp<ascendc::TBufGetTensorOp>(op, type, buffer);
```

队列分支（[69-76 行](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/MaterializeTensor.cpp#L69-L76)）多一步释放，位置取的是 **alloc 所在块的终结符之前**——张量在函数体则落在 return 前，在循环体则落在 yield 前（即每圈末尾释放、下圈重新 alloc，与队列的迭代语义吻合）：

```cpp
auto queueTy = ascendc::QueueType::get(op.getContext(), getPosition(op), 1);
Value queue = rewriter.create<ascendc::QueueOp>(loc, queueTy);
rewriter.create<ascendc::TPipeInitQueueOp>(loc, pipe, queue, num, length);
auto allocOp = rewriter.replaceOpWithNewOp<ascendc::TQueBindAllocTensorOp>(op, type, queue);
rewriter.setInsertionPoint(allocOp->getBlock()->getTerminator());
rewriter.create<ascendc::TQueBindFreeTensorOp>(allocOp->getLoc(), queue, allocOp.getTensor());
```

位置判定的优先级在 [getPosition（37-44 行）](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/MaterializeTensor.cpp#L37-L44)：先 output 后 input，两者皆无则本函数本不该被调到（assert 语义的 `llvm_unreachable`）。

与 lit 测试对账（[materialize-tensor.mlir:11-32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/materialize-tensor.mlir#L11-L32)）：静态形状用例里 4 个张量（input×2、output×2）各自展开成 `pipe → queue → init_queue → alloc` 四连，64×f32 与 4×32×f32 分别对应 `%c256_i64`、`%c512_i64`（\( 4 \times 32 \times 32 \div 8 = 512 \)）；末尾 4 条 `que_bind.free_tensor` 齐聚 return 之前——它们的相对顺序与 alloc 相反，这是贪心工作列表应用顺序的副产物，语义上只需保证「分配在前、释放在后」。[动态形状用例（45-58 行）](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/materialize-tensor.mlir#L45-L58)展示了 `%arg3 × %c16_i64` 的 muli 链（f32 每元素 4 字节，先与 4 相乘再乘动态维）；[VECCALC 用例（67-76 行）](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/materialize-tensor.mlir#L67-L76)则是无标记张量的 tbuf 三连。

产物中的每个 Op 都能在 fwk 目录找到定义：`pipe`（[TPipe.td:23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TPipe.td#L23)）、`pipe.init_queue`（[TPipe.td:44](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TPipe.td#L44)）、`pipe.init_buffer`（[TPipe.td:34](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TPipe.td#L34)）、`queue`（[TQue.td:181](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td#L181)）、`que_bind.alloc_tensor`（[TQue.td:25](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td#L25)）、`que_bind.free_tensor`（[TQue.td:100](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td#L100)）、`tbuf`/`tbuf.get_tensor`（[TBuf.td:25](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TBuf.td#L25)、[TBuf.td:31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TBuf.td#L31)）——物化后的 IR 与 02_add_framework 用户手写的框架 IR 完全同构，这也是「惰性风格后期与框架风格趋同」的物证。

#### 4.4.4 代码实践

1. **实践目标**：练「由形状算字节、由字节反查 IR」的双向能力。
2. **操作步骤**：
   - 打开 [materialize-tensor.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/materialize-tensor.mlir)，遮住 CHECK；
   - 对三个用例里的每个 local_tensor_auto 手算 length 常量、写出你预期的 `queue`/`tbuf` 类型与四连结构；
   - 与 CHECK 对账，重点核对 `%c256_i64`、`%c512_i64` 与 `%arg3 × %c16_i64`。
3. **需要观察的现象**：手算值与 CHECK 常量是否一致。
4. **预期结果**：静态 64×f32 → 256；4×32×f32 → 512；动态 `(%c4, %arg3)` 的 f32 → `%c4_i64 × %arg3`？——注意陷阱：init 前的 muli 是 `%arg3 × %c16_i64`，因为 f32 每元素 4 字节、而第一维 4 已并入 `%c16 = 4 × 4`（元素数 4×arg3 × 4 字节）。以 CHECK 为准。
5. 若构建了 devtools，可用 `ascir-opt -ascendc-materialize-tensor` 复跑验证；否则本实践为纯阅读型，答案即文件内 CHECK。

#### 4.4.5 小练习与答案

**练习 1**：为什么 VECCALC 路线不需要 free，而队列路线需要？

**答案**：TBuf 是「借出视图」——`tbuf.get_tensor` 只是从已分配的缓冲上取一个张量视图，缓冲生命周期由 pipe 统一管理，无需逐张量释放。队列的 alloc/free 则是资源记账：队列深度固定（这里为 1），alloc 占一块、free 还一块，配对才能在下一圈迭代重新分配；缺 free 会让队列耗尽（VerifySync/运行期都会暴露）。

**练习 2**：`pipe.init_queue` 的 `num` 实参是 `consts.i32(1)`，这个 1 与 `QueueType::get(ctx, pos, 1)` 里的 1 各是什么？

**答案**：都是队列缓冲块数（BUFFER_NUM 概念，u2-l4/u2-l6 讲过乒乓缓冲）。类型里的 1 编进 `!ascendc.queue<vecin, 1>` 成为编译期模板参数；实参里的 1 是 `TPipe::InitBuffer(que, num, len)` 的运行期入参。惰性风格固定用 1——不与用户抢双缓冲的调优空间，也不做超出语义保证的优化。

### 4.5 UnifyPipe：N 个 pipe 合一

#### 4.5.1 概念说明

Ascend C 的编程模型里，一个 Kernel 应由**一个** `TPipe` 统一管理 Device 内存与同步事件（u2-l6 的 TPipeManager 在前端层面强制了这一点）。但 MaterializeTensor 为了局部性，每物化一个张量就随手新建一个 `ascendc.pipe`——三个惰性张量就是三个 pipe。多个 pipe 并存不仅浪费（各自独立记账），更会在事件号分配、UB 排布上互相打架，发射层也无法把它们合法地翻译成单个 `TPipe` 对象的 C++ 代码。UnifyPipe 收尾：函数内多于一个 pipe 时，在入口块最前新建一个统一 pipe，替换全部使用并删除旧的。

它还会在 optimizing 阶段再被调度一次（[compiler.py:141](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L141)），因为 InsertSync 也会造 pipe：为 `get_value` 等标量读写插同步时要通过 `pipe.fetch_event_id` 申请事件号，那里直接 `create<ascendc::PipeOp>`（[InsertSync.cpp:61-69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InsertSync.cpp#L61-L69)）。同一函数里又一次出现了「多头 pipe」，需要再合一。

#### 4.5.2 核心流程

```text
unifyPipe(func):
  1. walk 收集函数内全部 ascendc.pipe
  2. 数量 ≤ 1 → 直接返回（无事可做）
  3. 在入口块最前（OpBuilder::atBlockBegin）新建 uniPipe
  4. 逐个 pipe.replaceAllUsesWith(uniPipe) 并删除
```

没有 Pattern、没有贪心驱动，一趟 walk 完事——这是「全函数一次性改写」型 Pass 的极简样本。

#### 4.5.3 源码精读

整个算法 14 行（[UnifyPipe.cpp:29-42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/UnifyPipe.cpp#L29-L42)）：

```cpp
void unifyPipe(func::FuncOp root)
{
    SmallVector<ascendc::PipeOp> pipes;
    root.walk([&pipes](ascendc::PipeOp op) { pipes.push_back(op); });
    if (pipes.size() <= 1) {
        return;
    }
    auto builder = OpBuilder::atBlockBegin(&root.getBody().front());
    Value uniPipe = builder.create<ascendc::PipeOp>(builder.getUnknownLoc());
    for (auto pipe : pipes) {
        pipe.replaceAllUsesWith(uniPipe);
        pipe.erase();
    }
}
```

三个细节：入口块最前插入保证统一 pipe 支配所有旧 pipe 的使用点；新 pipe 用 `unknownLoc`（它是编译器合成物，没有用户源码位置可言）；`replaceAllUsesWith + erase` 是 MLIR 里「合并等价值」的标准手法。lit 测试 [unify-pipe.mlir:15-25](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/unify-pipe.mlir#L15-L25) 里两条 `init_queue` 各自的 pipe 操作数在输出中变成同一个 `%0`，且 `CHECK-NOT: ascendc.pipe` 保证只剩一个。

现在回答本讲标题里那个最容易想当然的问题——**UnifyPipe 在 01_add 与 02_add_framework 上改写了什么？答案都是：什么都没改**：

- 01_add 手动风格：`asc.LocalTensor(...)` 生成 `local_tensor_v2`，无队列无 pipe；同步靠手写 set_flag/wait_flag，不触发 insert_sync 链。函数内 pipe 数为 0 ≤ 1，早退。
- 02_add_framework 框架风格：用户显式 `asc.TPipe()`（[tpipe.py:387](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L387) 的 `create_asc_PipeOp()`）恰好产生 1 个 pipe，`init_buffer` 直接生成 `pipe.init_queue`（[tpipe.py:465-471](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L465-L471)）；无 local_tensor_auto，MaterializeTensor 不改写、不新增 pipe。1 ≤ 1，早退。
- 真正被改写的是惰性风格（test_vadd.py）：3 个张量 → 3 个 pipe → 合 1。三种殊途同归：**无论用户用哪种风格，进入发射层的 IR 都收敛到「单 pipe + 队列/缓冲」的统一形态**——「Unify」之名，统一的不只是 pipe 个数，更是三种编程风格的终点形态。

#### 4.5.4 代码实践

1. **实践目标**：用三个示例的 dump 验证 4.5.3 的论断（pipe 数分别为 0、1、N→1）。
2. **操作步骤**：
   ```bash
   export PYASC_DUMP_PATH=/tmp/pyasc_dump/u6l2
   python3 examples/01_add/add.py -r Model
   mv /tmp/pyasc_dump/u6l2 /tmp/pyasc_dump/u6l2_add_manual   # 每个示例单独归档
   python3 examples/02_add_framework/add_framework.py -r Model
   mv /tmp/pyasc_dump/u6l2 /tmp/pyasc_dump/u6l2_add_framework
   python3 python/test/kernels/insert_sync/test_vadd.py
   mv /tmp/pyasc_dump/u6l2 /tmp/pyasc_dump/u6l2_add_auto
   grep -c "ascendc.pipe$\|ascendc.pipe " /tmp/pyasc_dump/*/ascir.mlir
   grep -c "local_tensor_auto" /tmp/pyasc_dump/*/codegen.mlir
   ```
3. **需要观察的现象**：三个 ascir.mlir 中 `ascendc.pipe` 出现次数；三个 codegen.mlir 中 `local_tensor_auto` 出现次数。
4. **预期结果**：01_add——codegen 与 ascir 均无 pipe、无 local_tensor_auto（它是 local_tensor_v2）；02_add_framework——恰 1 个 pipe、无 local_tensor_auto；test_vadd——codegen.mlir 有 3 个 local_tensor_auto 且 0 个 pipe，ascir.mlir 恰 1 个 pipe、0 个 local_tensor_auto。若环境无 Model 仿真器依赖，则标注「待本地验证」，改用读 lit 测试文件推演。
5. 注意：dump 的 codegen.mlir 是全部 Pass **前**、ascir.mlir 是全部 Pass **后**（[compiler.py:164-167](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L164-L167)），两个文件的差异是全部 Pass 的总效果；要精确归因到本讲四个 Pass，叠加 `print_ir_before_all=True` 或用 ascir-opt 单跑（u6-l1 方法）。

#### 4.5.5 小练习与答案

**练习 1**：既然 MaterializeTensor 知道会造出 N 个 pipe，为什么不直接复用第一个 pipe，省掉 UnifyPipe？

**答案**：Pattern 改写是局部的——`matchAndRewrite` 处理单个 local_tensor_auto 时，函数里可能还有其他尚未改写的同类 Op，甚至还没有「第一个 pipe」可复用；就算有，查找/传递它也会让 Pattern 带上跨 Op 状态，违背 MLIR 局部改写的惯例。先各造各的、再用一个全局遍历合一，是「局部 Pattern + 全局清扫」的干净分工。

**练习 2**：如果用户在同一个 Kernel 里既写了 `asc.TPipe()` 又用了 `asc.LocalTensorAuto`，会发生什么？

**答案**：框架的 TPipe 产生 1 个 pipe，MaterializeTensor 又为每个惰性张量造 pipe，总数 ≥ 2，UnifyPipe 照样全部合并成一个。这正是合一逻辑放在 MaterializeTensor 之后调度的好处——不管 pipe 来自用户还是编译器，终态唯一。（合并后 init_buffer/init_queue 都挂到同一 pipe 上，等价于用户把所有缓冲都交给同一个 TPipe 管理。）

## 5. 综合实践

把 spec 里的实践任务完整做一遍：**三种风格的 IR 前后对照摘录**。

1. 准备：按 4.5.4 的命令分别 dump 三个示例（01_add 手动、02_add_framework 框架、test_vadd 惰性），每个示例得到 codegen.mlir（Pass 前）与 ascir.mlir（Pass 后）。
2. 对每个示例，在 codegen.mlir 中定位张量声明行（`local_tensor_v2` / `que_bind.alloc_tensor` / `local_tensor_auto`），在 ascir.mlir 中找到它们 Pass 后的对应物，填出下面这张表（示例行按 test_vadd.py 的期望填写，请以你本地 dump 为准核对）：

   | 风格 | codegen.mlir 中的声明 | ascir.mlir 中的形态 | pipe 数变化 | input/output 标记 |
   | --- | --- | --- | --- | --- |
   | 01_add 手动 | `ascendc.local_tensor_v2` ×3（位置/地址为操作数） | 原样保留（无物化） | 0 → 0 | 无此概念 |
   | 02_add 框架 | `ascendc.pipe` ×1 + `que_bind.alloc_tensor`（子函数内） | 原样保留 + HoistQueBind（若触发 insert_sync 链则同步被重建） | 1 → 1 | 无此概念 |
   | test_vadd 惰性 | `ascendc.local_tensor_auto` ×3（循环体内） | `pipe + queue<vecin/vecout,1> + init_queue + alloc/free_tensor` | 0 → 3 → 1 | x/y=input，z=output |

3. 在 test_vadd 的 ascir.mlir 中完成三问验证：①三个张量是否仍留在 scf.for 内（提升是否如预期「全员留守」）；②free_tensor 落点是否在循环体 yield 前；③`ascendc.pipe` 是否只剩函数入口一个。顺带打开 ascendc.cpp 搜 `TPipe`/`TQue`，确认发射产物与框架风格同构。
4. 产出一份不超过一页的《前后 IR 对照摘录》：每个风格贴 3-5 行最关键的 IR 片段并加一句话注释；最后用三行总结三种风格如何在 Pass 链末端收敛到同一形态。
5. 若本地无法运行 Model 模式，替代方案：直接以四个 lit 测试文件（materialize-tensor / hoist-ub-allocation / unify-pipe / input-output-tensor.mlir）为「前后对照」素材完成同一张表，标注「基于 lit 测试推演，待本地验证」。

## 6. 本讲小结

- 惰性张量 `ascendc.local_tensor_auto` 是纯声明：形状进类型、方向待定（input/output 两个 UnitAttr）、动态维作操作数；四个 Pass 的使命是把它变成与用户手写框架风格同构的真分配。
- 执行顺序即设计：InputOutputTensor 按 data_copy 方向打标记（方向由 `DataCopyOpInterface::getDirection` 按 dst/src 类型推导）→ HoistUBAllocation 把无标记张量逐层提出循环/分支（判据：FuncOp 为止、无标记、操作数支配父操作）→ MaterializeTensor 按 VECOUT > VECIN > VECCALC 优先级物化为队列或 tbuf（字节数 = 元素数 × 位宽 / 8，动态形状现场 muli）→ UnifyPipe 把 N 个 pipe 合一。
- 「先提升后物化」是关键取舍：提升一行声明 trivially 等价，提升五件套分配几乎不可安全自动化。
- UnifyPipe 在 01_add（0 pipe）与 02_add_framework（1 pipe）上都是早退空操作，真正改写惰性风格；它的意义是让三种编程风格在发射前收敛到「单 pipe + 队列/缓冲」的统一形态，InsertSync 产生的新 pipe 也靠它在 optimizing 阶段末尾二次合一。
- 读 C++ Pass 源码的两个坑：`return void表达式;` 是早退不是顺序语句；`getUsers` 聚合分析说明「使用点属性」必须由 Pass 而非前端判定。

## 7. 下一步学习建议

四个 Pass 把惰性张量铺成了队列形态，但队列的 enque/deque 与事件同步还没有着落——这正是下一讲 u6-l3《自动同步插入：InsertSync、EraseSync 与 HoistQueBind》的领地：EraseSync 删手动同步、InsertSync 按 API 依赖分析自动重建 set_flag/wait_flag（本讲 4.5 已顺路见过它造 pipe 取事件号的代码）、VerifySync 校验合法性。建议阅读顺序：先读 InsertSync.cpp 的 `findQueue/enqueueTensors`，再回看本讲物化出的 `que_bind.alloc_tensor` 如何被它包上 enque/deque。若你想先缓一缓后端，也可跳到 u7-l1 看 Matmul 高阶 API 如何复用本讲的 TBuf 物化路径（无标记张量 → VECCALC）。
