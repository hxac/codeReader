# 自动同步插入：InsertSync、EraseSync 与 HoistQueBind

## 1. 本讲目标

上一讲（u6-l2）我们看到惰性张量如何被物化成真实的队列与缓冲——但那只解决了「内存从哪来」，还没解决「搬运、计算、搬出三条流水线谁等谁」。本讲精读 optimizing 阶段的同步重建链，学完后你应该能够：

1. 背出 `EraseSync → HoistQueBind → InsertSync (+ UnifyPipe)` 四步的调度顺序，并解释每一步为什么必须在它所在的位置。
2. 读懂 InsertSync 的核心算法：按「生产者写完就入队、最早消费者之前出队」重建队列纪律，而不是凭空生成裸的 `set_flag/wait_flag` 指令对。
3. 说清三类重建手段的分工：队列 EnQue/DeQue（主力）、标量 Get/Set 场景的 V_S/S_V 显式 SetFlag/WaitFlag（特例）、无队列张量的 `PipeBarrier<PIPE_V>`（兜底）。
4. 使用 `verify_sync=True` 编译选项校验自己算子的队列使用纪律，并知道它输出的是 warning 而非 error。

## 2. 前置知识

本讲默认你已读过 u2-l4（HardEvent 与三段流水）、u2-l6（TPipe/TQue 框架）、u3-l4（Pass 流水线与 insert_sync 三态语义）与 u6-l1（Pass 全景）。用四段话把背景补齐。

**同步问题的三种解法。** u2-l4 讲过，一个核内 MTE2（搬入）、V（计算）、MTE3（搬出）三条流水线异步并发，谁等谁必须显式表达。pyasc 里同步有三个来源：①用户手写 `asc.set_flag/asc.wait_flag`（01_add 风格）；②队列框架的隐式同步——`enque/deque` 在 Ascend C 库内部落实事件等待（02_add_framework 风格，u2-l6）；③毕昇编译器的 `--cce-auto-sync`，在机器码层按 API 依赖再补一层（u3-l5）。本讲的 Pass 链属于编译期 IR 层的「重建同步」，与 ③ 互补。

**insert_sync 的三态语义。** `insert_sync` 是 `CompileOptions` 的字段，默认 `None`（[compiler.py:41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L41)）。`None` 时在 `run_passes` 里自动判定：IR 中只要出现一个 `ascendc.local_tensor_auto`（惰性张量，u6-l2），就置为 `True`，否则保持 falsy、链不运行（[compiler.py:180-181](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L180-L181)）。判定逻辑在 pybind 侧：整个模块 walk 一遍，遇到 `LocalTensorAutoOp` 就中断返回真（[python/src/IR.cpp:569-575](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L569-L575)）。所以：**惰性风格默认走本讲链条；01/02 两种示例默认不走，必须显式传 `insert_sync=True` 强制开启**。作为 `CompileOptions` 字段，它参与文件缓存 key（u3-l8），切换取值必然触发重编译。

**IR 名称速查。** 前端 `asc.TQue(pos, depth)` 生成的是 `ascendc.queue` 构造 Op，而它继承自 `TQueBind`，`alloc_tensor/deque/enque/free_tensor` 全是继承来的方法，生成的 IR 是 `que_bind.*` 家族（[tpipe.py:540-566](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L540-L566)）。对照 td 定义：`ascendc.que_bind.alloc_tensor / deque_tensor / enque_tensor / free_tensor`（[Fwk/TQue.td:25-108](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td#L25-L108)）。手动同步则生成 `ascendc.set_flag / ascendc.wait_flag / ascendc.pipe_barrier`（[python/asc/language/basic/block_sync.py:35-51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/block_sync.py#L35-L51)）。本讲四个 Pass 操作的全是这些 IR Op。

**两个 MLIR 基础工具。** 一是 `op.walk(callback)`：按程序顺序（先序遍历）访问区域内所有 Op，是「扫一遍做统计/改写」的标准姿势。二是**支配关系（dominance）**：一个 Value 的定义必须支配它的所有使用点；把 Op 移出循环/分支前必须检查操作数在新位置仍可用。本讲的 `opPrecedes(lhs, rhs)` 是「lhs 是否排在 rhs 之前」的全序判定，是依赖分析的基石（详见 4.3.3）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lib/Dialect/Asc/Transforms/EraseSync.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/EraseSync.cpp) | 第一步：删除全部同步类 Op（enque/deque/set_flag/wait_flag/pipe_barrier） |
| [lib/Dialect/Asc/Transforms/HoistQueBind.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/HoistQueBind.cpp) | 第二步：把 queue/que_bind/tbuf 等六类基础设施 Op 提升到函数体根部 |
| [lib/Dialect/Asc/Transforms/InsertSync.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InsertSync.cpp) | 第三步（主角）：按生产者-消费者关系重建 enque/deque、标量同步与屏障 |
| [lib/Dialect/Asc/Transforms/VerifySync.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/VerifySync.cpp) | 校验器：检查 TQue 租借纪律，以 warning 输出（postprocessing 末尾可选） |
| [include/ascir/Dialect/Asc/Utils/Utils.h](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Utils/Utils.h) | `HoistOpPattern` 泛型提升模板与 `opPrecedes` 声明 |
| [lib/Dialect/Asc/Utils/Utils.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Utils/Utils.cpp) | `opPrecedes` 实现：同块看线性序，跨块看支配树 |
| [lib/Dialect/Asc/IR/Ops.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Ops.cpp) | `PipeBarrierOp::canonicalize`：相邻屏障折叠规则（InsertSync 收尾用到） |
| [include/ascir/Dialect/Asc/Transforms/Passes.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td) | 四个 Pass 的声明（注册名、作用域、构造函数） |
| [python/asc/runtime/compiler.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py) | 调度：重建链在 optimizing 阶段，VerifySync 在 postprocessing 末尾 |
| [test/Dialect/AscendC/Transforms/insert-sync.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/insert-sync.mlir) | InsertSync 的 lit 测试：官方「前后对照表」，本讲反复引用 |
| [test/Dialect/AscendC/Transforms/hoist-que-bind.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/hoist-que-bind.mlir) | HoistQueBind 的 lit 测试 |
| [examples/02_add_framework/add_framework.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py) | 实践主素材：队列框架风格的 Add |

四个 Pass 在 Passes.td 中的声明：EraseSync（[Passes.td:33-36](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L33-L36)）、HoistQueBind（[Passes.td:44-47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L44-L47)）、InsertSync（[Passes.td:59-63](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L59-L63)）、VerifySync（[Passes.td:95-98](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L95-L98)）。前三个作用于 `func::FuncOp`，VerifySync 也是——它们都经 `addNestedPass` 挂到模块上（u6-l1）。

## 4. 核心概念与源码讲解

先把整条链的调度钉死。optimizing 阶段（[compiler.py:133-142](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L133-L142)）：

```python
passes.common.add_licm(pm)            # 循环不变量外提（通用）
passes.common.add_sccp(pm)            # 稀疏条件常量传播（通用）
passes.common.add_canonicalizer(pm)   # 规范化（通用）
if self.options.insert_sync:
    passes.ascendc.add_erase_sync(pm)       # ① 拆除旧同步
    passes.ascendc.add_hoist_que_bind(pm)   # ② 基础设施上提
    passes.ascendc.add_insert_sync(pm)      # ③ 重建同步
    passes.ascendc.add_unify_pipe(pm)       # ④ 合并 pipe（u6-l2 已讲）
    passes.common.add_canonicalizer(pm)     # ⑤ 收尾规范化
```

「先全拆、再重装」是这个设计最有特点的地方：与其在用户手写的同步上修修补补，不如把同步全部抹掉，再依据算子间**真实的数据依赖**重新推导一遍。这也是惰性风格（`local_tensor_auto`）用户一行同步都不用写的底气所在。下面按执行顺序逐个精读。

### 4.1 EraseSync：拆除旧同步

#### 4.1.1 概念说明

EraseSync 是重建链的第一步，任务只有一个：把函数内所有**承担同步职责**的 IR Op 删干净，为 InsertSync 腾出一张白纸。删除清单共五类：

- `ascendc.que_bind.enque_tensor` / `ascendc.que_bind.deque_tensor`——队列的入队/出队（隐式同步的载体）；
- `ascendc.set_flag` / `ascendc.wait_flag`——用户手写的事件同步；
- `ascendc.pipe_barrier`——管线屏障。

注意它**保留** `que_bind.alloc_tensor` 和 `que_bind.free_tensor`：这两个管的是内存池租借（u2-l6 的四步生命周期中「借」与「还」），不承载跨流水线同步语义，删了程序就没有 UB 可用了；而且 alloc 的结果还是后续 InsertSync 找回队列的线索（见 4.3）。

deque 有一处特殊处理：它是**有结果值**的操作（出队得到一个张量），不能像 set_flag 那样一删了之——所有使用它的地方会悬空。解决办法是：同一队列上 alloc 出来的张量本来就是同一个缓冲，直接用 alloc 结果**替换** deque 结果的全部使用，SSA 数据流照样成立。

#### 4.1.2 核心流程

```
EraseSync(funcOp):
    allocTensors = {}                       # 队列 -> 该队列上 alloc 出的张量
    walk 所有 que_bind.alloc_tensor:
        allocTensors[queue] = tensor        # 同队列多次 alloc，后到者覆盖
    walk 所有 que_bind.deque_tensor:
        if queue 不在 allocTensors:
            报 op 错误 "doesn't have corresponding alloc_tensor op"
            Pass 失败（signalPassFailure）
        else:
            用 allocTensors[queue] 替换 deque 结果的全部使用
            删除 deque
    删除全部 que_bind.enque_tensor
    删除全部 set_flag / wait_flag / pipe_barrier
```

#### 4.1.3 源码精读

先看删除工具与 Pass 主体：

- [lib/Dialect/Asc/Transforms/EraseSync.cpp:28-32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/EraseSync.cpp#L28-L32)——`eraseOps<OpT>` 函数模板：walk 整个函数，把指定类型的 Op 全部 `erase()`。一个模板服务四种删除目标，类型安全且零重复。
- [lib/Dialect/Asc/Transforms/EraseSync.cpp:34-58](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/EraseSync.cpp#L34-L58)——`runOnOperation` 主体，与上面伪代码一一对应：第 41-43 行建 queue→tensor 映射；第 44-53 行处理 deque，其中第 47-49 行是**唯一的失败路径**——deque 所在队列从未 alloc 过时报错并 `signalPassFailure`；第 54-57 行四连删。
- [include/ascir/Dialect/Asc/IR/Fwk/TQue.td:43-50](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td#L43-L50)——被替换主体的 td 定义：`deque_tensor` 有结果 `%tensor`（`AscendC_LocalTensor`），这就是它不能直接 erase 的原因；对照 [Fwk/TQue.td:80-88](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td#L80-L88) 的 `enque_tensor`（无结果、纯副作用），理解「有值操作」与「纯动作操作」删除方式的差别。

两个容易误读的点：

1. **报错条件是「队列上没 alloc 过」，不是「没 enque 过」。** EraseSync 假定接下来 InsertSync 会把 enque/deque 全部重建，所以「deque 找不到配对的 enque」在本 Pass 眼里不是错误；它只关心替换 deque 结果所需的 alloc 值是否存在。
2. **映射是「队列级」的，不是「张量级」的。** `allocTensors[op.getQueue()] = op.getTensor()` 按队列记最后一个 alloc。若同一队列 alloc 多次，替换统一用最后一次的张量——这是一个保守近似，数据流的精确性由 InsertSync 重建时修正。

#### 4.1.4 代码实践

**实践目标**：亲眼看到手动同步从 Ascend C 产物中消失。01_add 是最好的素材，因为它有 6 行字面上的 `set_flag/wait_flag`（[examples/01_add/add.py:57-69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L57-L69)）。

**操作步骤**：

1. 进入示例目录，先跑默认配置（01_add 无 `local_tensor_auto`，`insert_sync` 自动判定为假，链不运行）：
   ```bash
   cd examples/01_add
   mkdir -p /tmp/sync_off
   PYASC_DUMP_PATH=/tmp/sync_off python3 add.py -r Model
   ```
2. 复制一份 `add.py`（例如 `add_erase.py`），把启动行改为显式开启重建链（编译选项从小括号传入，u3-l1）：
   ```python
   vadd_kernel[USE_CORE_NUM, rt.current_stream()](x, y, z, block_length, insert_sync=True)
   ```
   再跑一次，dump 到 `/tmp/sync_on`。
3. 对比两份产物：
   ```bash
   diff /tmp/sync_off/ascendc.cpp /tmp/sync_on/ascendc.cpp
   grep -n -E "SetFlag|WaitFlag|PipeBarrier" /tmp/sync_off/ascendc.cpp /tmp/sync_on/ascendc.cpp
   ```

**需要观察的现象**：关闭时 ascendc.cpp 里能 grep 到形如 `AscendC::SetFlag<HardEvent::MTE2_V>(...)`、`AscendC::WaitFlag<...>(...)` 的行（发射格式见 [lib/Target/AscendC/Basic/BlockSync.cpp:27-34](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Basic/BlockSync.cpp#L27-L34) 的 WaitFlag 同款写法）；开启后这 6 对 Set/Wait 全部消失，取而代之出现 `AscendC::PipeBarrier<PIPE_V>()` 与末尾的 `AscendC::PipeBarrier<PIPE_ALL>()`（来源见 4.3）。

**预期结果**：两次运行的 `assert torch.allclose` 都应通过（同步重建以正确性为先，屏障是保守但安全的替代）。逐行 diff 的具体形态待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 EraseSync 保留 `alloc_tensor/free_tensor`，却删 `enque/deque`？

**答案**：四个操作里只有 enque/deque 承担「跨流水线同步」语义（u2-l6：队列内置隐式同步），alloc/free 只管内存池的租借与归还。重建的目标是同步，所以只拆同步；而且 InsertSync 重建时还要靠 alloc 结果反查队列（`findQueue`），alloc 必须留着的另一个原因是它定义了张量这个 SSA 值本身。

**练习 2**：`insert_sync=True` 时用户手写的 `set_flag/wait_flag` 还会出现在最终 ascendc.cpp 里吗？

**答案**：不会。EraseSync 第 55-56 行无条件删除全部 `SetFlagOp/WaitFlagOp`，用户的手写同步被整体作废；最终产物里的 SetFlag/WaitFlag（如果有）只能来自 InsertSync 的标量 Get/Set 场景（V_S/S_V 方向，见 4.3.3），方向和位置都与用户写法无关。

**练习 3**：deque 替换为什么安全？「同队列的 alloc 张量」和「deque 出的张量」凭什么是同一个缓冲？

**答案**：因为 Ascend C 队列的语义就是「alloc 从队列私有缓冲池借出一块、enque 后由队列持有、deque 取回同一块」（u2-l6 生命周期）。EraseSync 删除 enque/deque 后，队列机制退化为「每次 alloc 固定拿到缓冲」，同一队列的 alloc 结果与 deque 结果指向同一 UB 内存，用值替换只是换了个 SSA 名字，不改变访存地址。

### 4.2 HoistQueBind：基础设施上提

#### 4.2.1 概念说明

EraseSync 之后，enque/deque 没了，但队列/缓冲的**构造与初始化**可能还埋在循环体或分支里——用户可能把 `asc.TQue()` 写在循环内，或者（更常见）队列创建在子函数里、内联后随着调用点落进了循环（u4-l4）。而下一步 InsertSync 要以「函数级」视角插 enque/deque：**插入点可能在循环外、分支外**，如果队列值本身定义在循环内，插到外面的 deque 就引用了一个不支配它的值，直接破坏 SSA。

所以第二步先把六类「基础设施」Op 提升到函数体根部：

- `ascendc.queue` / `ascendc.que_bind`——队列构造；
- `ascendc.tbuf`——TBuf 构造；
- `ascendc.pipe.init_buffer` / `ascendc.pipe.init_queue`——队列/缓冲初始化；
- `ascendc.tbuf.get_tensor`——从 TBuf 取固定视图。

注意提升名单里**没有** `que_bind.alloc_tensor/free_tensor`：alloc 是「本轮迭代借一块新缓冲」的循环语义操作，提出去就错了。这与 u6-l2 的 HoistUBAllocation 形成 对照——那边提升的是惰性分配声明，这边提升的是队列基础设施，判据不同但都用同一套支配检查。

#### 4.2.2 核心流程

HoistQueBind 是标准的贪心模式重写（u6-l2 前置知识里讲过 `applyPatternsAndFoldGreedily`）：对六类 Op 各注册一个 `HoistOpPattern<OpT>`，驱动器反复应用直到不动点。单个 pattern 的逻辑：

```
HoistOpPattern<OpT>::matchAndRewrite(op):
    if op 的父操作就是 func.func: 失败（已到顶）
    if !hoistable(op): 失败（子类可覆写的扩展点，默认恒真）
    if op 的任一操作数不支配 op 的父操作: 失败（提上去会用未定义的值）
    在父操作之前克隆 op，替换原 op 的全部使用
```

由于每次只提升一层（提到父操作之前），嵌套循环里的 Op 会被贪心驱动器一层层剥洋葱，直到进入函数体第一块。

#### 4.2.3 源码精读

- [lib/Dialect/Asc/Transforms/HoistQueBind.cpp:29-46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/HoistQueBind.cpp#L29-L46)——Pass 主体只有一件事：往 `RewritePatternSet` 里塞六个 `HoistOpPattern` 实例化（第 35-41 行），然后跑贪心重写；失败才 `signalPassFailure`。Pass 的全部逻辑都沉淀在泛型模板里，这是「声明式组装」风格的典型样本。
- [include/ascir/Dialect/Asc/Utils/Utils.h:23-45](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Utils/Utils.h#L23-L45)——`HoistOpPattern` 泛型模板，与上面伪代码逐行对应：第 31-33 行「父已是 FuncOp 则不动」；第 36-38 行用 `DominanceInfo` 检查**全部操作数都支配父操作**（`llvm::all_of`），任何一个不满足就放弃；第 41-42 行 `setInsertionPoint(parent)` + `clone` + `replaceOp` 完成上提。注意 `hoistable()` 是 virtual 的（第 27 行），留给未来按 Op 细化提升条件。
- [test/Dialect/AscendC/Transforms/hoist-que-bind.mlir:11-31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/hoist-que-bind.mlir#L11-L31)——官方前后对照：输入 IR 里两个 `ascendc.queue` 和两个 `pipe.init_queue` 都在 `scf.for` 体内，输出里它们被提到 for 之前，而 `que_bind.alloc_tensor/free_tensor` 原地不动。嵌套循环用例（第 33-56 行）展示逐层上提到函数体。

#### 4.2.4 代码实践

**实践目标**：不改一行 C++ 代码，用 `ascir-opt` 工具单跑 HoistQueBind，验证 lit 测试描述的行为。

**操作步骤**：

1. 若已按 u7-l5 的方式构建 devtools（`PYASC_SETUP_DEVTOOLS=1`），直接可用 `ascir-opt`；否则以阅读 lit 测试替代第 3 步。
2. 从 [hoist-que-bind.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/hoist-que-bind.mlir) 中截取 `@hoist_que_bind_for` 一个函数存成 `/tmp/hoist.mlir`。
3. 运行并观察：
   ```bash
   ascir-opt -ascendc-hoist-que-bind /tmp/hoist.mlir
   ```

**需要观察的现象**：输出 IR 中 `ascendc.queue` 与 `pipe.init_queue` 移动到 `scf.for` 之前，`alloc_tensor/free_tensor` 留在循环体内，与文件顶部 `// CHECK:` 注释一致。

**预期结果**：与 lit 测试期望逐行一致（lit 本身就是 CI 里跑的回归）。若未构建 devtools，此步待本地验证，可先人工比对 CHECK 行与输入。

#### 4.2.5 小练习与答案

**练习 1**：为什么这个 Pass 必须排在 EraseSync 之后、InsertSync 之前？

**答案**：三个原因层层递进。①若在 EraseSync 之前跑，enque/deque 还在循环里，提升队列构造不能顺带提升它们，做完 EraseSync 后 IR 结构又变了，白做；②InsertSync 插入 deque 的位置是「最早消费者之前」，可能在循环/分支外，若队列值还在循环内则不支配使用点，直接非法——必须先把队列本身提上去；③InsertSync 的 `reEnque` 分支（4.3.3）在 enque 不支配 deque 时会尝试上提 enque，基础设施先行到位能减少这种修补。

**练习 2**：支配检查失败（某个操作数只在循环内定义）时会发生什么？

**答案**：pattern 返回 `failure()`，该 Op 留在原地，不会强行提升（[Utils.h:37-40](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Utils/Utils.h#L37-L40)）。正确性优先于「提干净」，宁可让后续 InsertSync 走 `reEnque` 修补路径，也不生成非法 IR。

**练习 3**：`HoistOpPattern` 为什么设计成模板而不是六个手写 pattern？

**答案**：六类 Op 的提升逻辑完全同构（查父、查支配、克隆、替换），差异只在 Op 类型。模板 + 贪心驱动把「做什么提升」压缩成一行类型列表（[HoistQueBind.cpp:35-41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/HoistQueBind.cpp#L35-L41)），新增可提升 Op 类型只需加一个模板参数；u6-l2 的 HoistUBAllocation 复用的也是同一个模板——u1-l3 讲过的「目录镜像」之外，工具层同样在复用。

### 4.3 InsertSync：按生产者-消费者重建同步

#### 4.3.1 概念说明

InsertSync 是整条链的主角，也是本讲标题里「自动同步插入」的执行者。先纠正一个直觉误区：**它并不直接生成 MTE2_V/V_MTE3 方向的裸 `set_flag/wait_flag` 指令对**。它的策略是重建「队列纪律」，让 Ascend C 的 EnQue/DeQue 在运行时落实事件同步（u2-l6）：

- **主力**：对挂在队列上的张量，在每个**生产者算子**（写了 dst 的 API 调用）之后补 `enque`，在**最早的消费者**之前补 `deque`——「数据就绪」与「等待数据」两个事件由队列配对完成；
- **特例**：标量访问 `get_value/set_value` 涉及 V 管（向量）与 S 管（标量）两条管线的互斥，这里才插显式的 `SetFlag/WaitFlag`（方向 V_S 与 S_V）；
- **兜底**：不在任何队列上的张量（01_add 的手动 LocalTensor、TBuf 视图、切片视图），在每个生产者算子后插保守的 `PipeBarrier<PIPE_V>` 保序。

「依赖分析」体现在生产者/消费者的认定上：**写 dst 的算子即生产者，张量在 enque 之后的最早使用者即消费者**，先后关系由 `opPrecedes` 判定。这与毕昇的 `--cce-auto-sync`（u3-l5，按 API 依赖在机器码层插同步）是两层独立机制，前者作用于 Ascend C 源码生成之前，后者作用于其后。

#### 4.3.2 核心流程

`runOnOperation` 固定五步（[InsertSync.cpp:174-191](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InsertSync.cpp#L174-L191)）：

```
InsertSync(funcOp):
    ① enqueueTensors:  walk 所有 OpWithDst（有 dst 张量的算子）
         dst 为空 / 非 BaseTensor / dst 由 GlobalTensorOp 定义(GM 视图) → 跳过
         dst 直接由 que_bind.alloc_tensor 或 que_bind.deque_tensor 定义 → 在算子后插 enque(queue, dst)
         否则 → 在算子后插 pipe_barrier(PIPE_V)
    ② dequeueTensors:  对每个 enque（含①刚插的）:
         users = 张量的使用者中排在 enque 之后、且不是 free_tensor 的那些
         firstUser = users 中最早的那个（opPrecedes 取 min）
         在 firstUser（映射回 enque 所在区域的祖先）之前插 deque
         若 enque 不支配新 deque → reEnque 把 enque 上提到 deque 的区域（失败则 Pass 失败）
         把 users 对张量的使用改接新 deque 的结果
    ③ syncGetValueOp:   每个 get_value 前后各插一对 V_S / S_V 的 set_flag+wait_flag
    ④ syncSetValueOp:   每个 set_value 同上；特判「for 体内唯一语句」时同步对提到循环外
    ⑤ canonicalizeBarriers: 函数末尾补 pipe_barrier(PIPE_ALL)，再折叠相邻重复屏障
```

用 02_add_framework 的主循环画个图（Python 源码见 [add_framework.py:45-79](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L45-L79)，内联后同处一个循环体）：

```
data_copy(x_local ← x_gm)   ← 生产者(OpWithDst, dst=x_local 来自 alloc)
  └➤ enque(in_queue_x, x_local)        ①「x 就绪」
data_copy(y_local ← y_gm)   ← 生产者
  └➤ enque(in_queue_y, y_local)
deque(in_queue_x) → x'      ② 插在最早消费者之前
deque(in_queue_y) → y'
add(z_local, x', y')        ← 消费者(x,y)，同时又是 z 的生产者
  └➤ enque(out_queue_z, z_local)
deque(out_queue_z) → z'     ② 插在 data_copy(z_gm ← z') 之前
data_copy(z_gm ← z')        ← dst 是 GM 视图，①直接跳过
```

lit 测试 [insert-sync.mlir:77-98](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/insert-sync.mlir#L77-L98) 的 `@insert_enqueue_dequeue` 用例就是这个形状的官方版「标准答案」：`enque_tensor` 紧跟 `data_copy_l2`，`deque_tensor` 插在 `add_l2` 之前，CHECK 注释逐行写明。

#### 4.3.3 源码精读

**① enqueueTensors——认定生产者。**

- [lib/Dialect/Asc/Transforms/InsertSync.cpp:33-42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InsertSync.cpp#L33-L42)——`findQueue`：只认两种出身——dst 的定义 Op 是 `que_bind.alloc_tensor` 或 `que_bind.deque_tensor`，返回其队列；**切片视图**（`x_local[k:]` 生成的 subindex Op）、TBuf 取出的张量等都返回空，走屏障分支。
- [lib/Dialect/Asc/Transforms/InsertSync.cpp:44-59](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InsertSync.cpp#L44-L59)——`enqueueTensors`：walk 所有实现 `OpWithDst` 接口的 Op（`asc.add`、`data_copy` 等带 dst 的算子，接口声明见 [Interfaces.td:68-71](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Interfaces.td#L68-L71)）；第 48-50 行三个跳过条件；第 53-58 行二选一插入。dst 是 GM 视图（copy_out 的目标）被跳过的原因：GM 不是 UB 队列成员，无从入队，其同步由来源张量的 deque 保证。

**② dequeueTensors——认定消费者并插 deque。**

- [lib/Dialect/Asc/Transforms/InsertSync.cpp:123-163](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InsertSync.cpp#L123-L163)——主体：第 138-140 行筛 users（排除 `free_tensor`、只要 enque 之后的，判据 `opPrecedes(enq, user, di)`）；第 144-150 行取最早者并映射回 enque 所在区域的祖先（跨循环/分支时对齐层级）；第 152 行创建 `TQueBindDequeTensorOp`；第 156-159 行 `replaceUsesWithIf` 只把 users 的使用改接新结果——enque 之前的老使用（如有）不动。
- [lib/Dialect/Asc/Transforms/InsertSync.cpp:107-121](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InsertSync.cpp#L107-L121)——`reEnque` 修补：若原 enque 不支配新 deque（比如 enque 在循环里、deque 提到了循环外），把 enque 克隆到 deque 所在区域、删掉旧的；找不到共同祖先则报 `"failed to be hoisted to tensor deque op scope"` 并让 Pass 失败。
- [lib/Dialect/Asc/Utils/Utils.cpp:27-43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Utils/Utils.cpp#L27-L43)——`opPrecedes` 两层逻辑：同块内用 `isBeforeInBlock` 线性序（第 27 行单参重载）；跨块时用 `DominanceInfo::findNearestCommonDominator` 找最近公共支配块，比较双方在该块内的祖先先后（第 39-42 行）。直觉：它把「程序文本顺序」推广到嵌套区域，是本 Pass 全部依赖分析的时序基准。

**③④ 标量 Get/Set 的显式同步。**

- [lib/Dialect/Asc/Transforms/InsertSync.cpp:61-69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InsertSync.cpp#L61-L69)——`createSetGetValueSync`：一次同步 = 四连 Op：`pipe` → `pipe.fetch_event_id`（从 pipe 申请事件号，i8）→ `set_flag` → `wait_flag`。方向由 `isBefore` 决定：访问前用 `V_S`（等 V 管写完，S 管才能读），访问后用 `S_V`（等 S 管改完，V 管才能继续）。前端对应物是 [tensor.py:356-363](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L356-L363) 的 `get_value`。
- [lib/Dialect/Asc/Transforms/InsertSync.cpp:84-105](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InsertSync.cpp#L84-L105)——`syncSetValueOp` 的特判：若 `set_value` 是某个 `scf.for` 体内**唯一**的语句（第 91-92 行用「体长 == 2（一条 op + 一条 yield）」判定），把两对同步提到循环前后（第 93-97 行）——否则每圈迭代要申请两次事件号并做两次 Set/Wait，循环次数大时代价可观。lit 用例 [insert-sync.mlir:36-63](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/insert-sync.mlir#L36-L63) 的 `@get_set_value_for` 正是这个形状的期望输出。

**⑤ canonicalizeBarriers——收尾。**

- [lib/Dialect/Asc/Transforms/InsertSync.cpp:165-172](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InsertSync.cpp#L165-L172)——在函数体末尾补一个 `pipe_barrier(PIPE_ALL)`（保证核返回前所有管线的搬出指令完成），然后只跑 PipeBarrier 自己的规范化。
- [lib/Dialect/Asc/IR/Ops.cpp:58-75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Ops.cpp#L58-L75)——折叠规则：两个相邻屏障，前者是 `PIPE_ALL` → 删后者；属性相同或后者是 `PIPE_ALL` → 删前者。于是「连续多个 PIPE_V + 末尾 PIPE_ALL」收敛成一个 `PIPE_ALL`。lit 用例 [insert-sync.mlir:65-75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/insert-sync.mlir#L65-L75) 可见：`duplicate_l2` 后留一个 `pipe_barrier pipe_v`，`add_l2` 后插入的 `pipe_v` 与补的 `PIPE_ALL` 相邻，折叠后只剩 `pipe_barrier pipe_all`。

最后看一眼链条第四步的衔接：③每次申请事件号都会新建一个 `ascendc.pipe`（[InsertSync.cpp:64](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InsertSync.cpp#L64)），一个循环下来函数里可能出现一堆 pipe——这正是调度里紧跟的 UnifyPipe（u6-l2）要合并的对象。**Pass 执行顺序再次体现设计意图**。

#### 4.3.4 代码实践

**实践目标**：对 02_add_framework 分别以 `insert_sync=False`（默认）与 `insert_sync=True` 编译，diff 两份 ascendc.cpp，理解「重建」到底改了什么。

**操作步骤**：

1. 默认一轮（02 无惰性张量，`insert_sync` 自动判定为假，链不运行）：
   ```bash
   cd examples/02_add_framework
   mkdir -p /tmp/fw_off
   PYASC_DUMP_PATH=/tmp/fw_off python3 add_framework.py -r Model
   ```
2. 复制为 `add_framework_sync.py`，把启动行（[add_framework.py:87](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L87)）改为：
   ```python
   vadd_kernel[USE_CORE_NUM, rt.current_stream()](x, y, z, block_length, tile_length, insert_sync=True)
   ```
   再跑一轮，dump 到 `/tmp/fw_on`。
3. 对比：
   ```bash
   diff /tmp/fw_off/ascendc.cpp /tmp/fw_on/ascendc.cpp | head -60
   grep -n -E "EnQue|DeQue|AllocTensor|FreeTensor|SetFlag|WaitFlag|PipeBarrier" \
        /tmp/fw_off/ascendc.cpp | head -30
   grep -n -E "EnQue|DeQue|AllocTensor|FreeTensor|SetFlag|WaitFlag|PipeBarrier" \
        /tmp/fw_on/ascendc.cpp | head -30
   ```

**需要观察的现象**（基于源码与 lit 测试推断，逐行细节待本地验证）：

- 两份产物里都**搜不到** `AscendC::SetFlag<HardEvent::MTE2_V>` 这类行——02 的用户代码本就没写手动同步，队列风格下同步由 `EnQue/DeQue` 调用承载（发射形态如 `队列.DeQue<half>()`，见 [lib/Target/AscendC/Fwk/TQue.cpp:41-50](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Fwk/TQue.cpp#L41-L50)）；
- `insert_sync=True` 时，`EnQue/DeQue` 的**位置**变化：用户写的 enque 在 `copy_in` 末尾、deque 在 `compute` 开头；重建后 EnQue 紧跟每个生产算子（搬入 data_copy 之后、add 之后），DeQue 插在最早消费者之前——对照 4.3.2 的示意图；
- `AllocTensor/FreeTensor` 位置基本不变（EraseSync 保留它们，InsertSync 也不动）；
- 若想看到字面消失的 SetFlag/WaitFlag，请回到 4.1.4 的 01_add 实践。

**预期结果**：两次 `assert torch.allclose(z, x + y)` 均通过；`/tmp/fw_on/ascendc.cpp` 中 EnQue/DeQue 的分布与 lit 测试 `@insert_enqueue_dequeue` 的模式一致。insert_sync 参与缓存 key，第二轮必然重新编译（u3-l8）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 InsertSync 不直接为队列张量生成 MTE2_V/V_MTE3 的 `set_flag/wait_flag`，而要重建 enque/deque？

**答案**：队列的 EnQue/DeQue 在 Ascend C 库内部已经实现了正确的事件配对（含缓冲池互斥、方向选择，u2-l6），重建队列纪律可以复用这套经过验证的机制，IR 层只需表达「谁生产、谁消费」这一高层语义；直接生成裸事件对则要在 IR 层重新实现事件号分配、方向推导、双缓冲乒乓等全部细节，既容易错也难维护。裸 SetFlag/WaitFlag 只留给队列管不了的标量 V↔S 场景。

**练习 2**：`asc.add(z_local[buf_id * tile_length:], ...)` 这种 dst 是**切片视图**的调用，InsertSync 会怎么处理？

**答案**：走屏障分支。`findQueue` 只认 dst 直接由 `que_bind.alloc_tensor/deque_tensor` 定义（[InsertSync.cpp:33-42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InsertSync.cpp#L33-L42)），切片的定义 Op 是 subindex，返回空，于是算子后面插 `pipe_barrier(PIPE_V)`（[InsertSync.cpp:57](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InsertSync.cpp#L57)）。01_add 里所有 dst 都是切片，所以它的重建产物全是屏障——这与 4.1.4 实践的预期一致。

**练习 3**：`syncSetValueOp` 对「for 体内唯一语句」的特判省掉了什么？为什么不干脆把所有 set_value 的同步都提到循环外？

**答案**：省掉的是每圈迭代两次 `fetch_event_id + set_flag + wait_flag`。但提同步到循环外的正确性前提是「循环体内没有其他会碰该张量的操作」——体长为 2（一条 op + yield）正是这一前提的可判定形式；体内还有别的算子时同步必须留在迭代内，否则其他迭代间操作会失去保护。

### 4.4 VerifySync：队列纪律校验器

#### 4.4.1 概念说明

前三个 Pass 是「改写」，VerifySync 是「检查」：它不修改 IR，只检查 TQue 家族的使用纪律是否成立，问题以 **MLIR warning** 形式报告。它不进默认流水线，只在 `verify_sync=True` 时挂载，而且挂在 postprocessing 的**最末尾**（[compiler.py:227-228](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L227-L228)，紧邻 `strip_loc` 之前——先校验再剥离定位，保证警告带完整源码位置，u6-l1 练习 3 讲过这个排序）。

校验对象是 u2-l6 的四步生命周期纪律，共六类问题：

| 编号 | 纪律违反 | 警告文案（节选） |
| --- | --- | --- |
| 1 | deque 时队列里没有 enque | `deque_tensor: there is no corresponding call to enque_tensor` |
| 2 | enque 与 deque 之间还使用了张量 | `unexpected use of tensor between enque_tensor and deque_tensor` |
| 3 | free 时找不到配对的 alloc | `there is no corresponding call to alloc_tensor` |
| 4 | 张量在最后一次使用前就被 free | `tensor memory was freed before its last use` |
| 5 | 函数结束仍有 alloc 未 free | `there is no corresponding call to free_tensor for this tensor` |
| 6 | 函数结束仍有 enque 未 deque | `there is no corresponding call to deque_tensor for this tensor` |

两个定位要点：**warning 不是 error**——`emitWarning` 不会 `signalPassFailure`，编译继续（对比 EraseSync 的 `emitOpError` + 失败，一个是门禁一个是 lint）；**时序在重建链之后**——若 `insert_sync=True`，enque/deque 已被编译器重写，此时校验的是重建后的程序；想校验「自己写的纪律」，保持默认（不传 insert_sync，框架风格自动判定为假）即可。

#### 4.4.2 核心流程

算法是「按队列记账」：每个队列一本账（`queBinds: ValueMap<SmallVector<Operation*>>`），walk 按程序顺序处理四类事件：

```
VerifySync(funcOp):
    walk 程序顺序:
        alloc_tensor  → 该队列账上记一笔 [alloc]
        enque_tensor  → 该队列账上记一笔 [enque]
        deque_tensor  → 在账上找第一个 enque（FIFO 纪律）:
                            找到 → 记录 deque→enque 映射，销掉这笔账，
                                   并检查该张量在 enque 与 deque 之间的使用（问题 2）
                            没找到 → 警告（问题 1）
        free_tensor   → findDef 沿 deque→enque 链回溯找到原始 alloc：
                            账上有这笔 → 销账
                            没有 → 警告（问题 4：free 早于最后使用）
                            回溯失败 → 警告（问题 3）
    收尾扫描所有账本:
        剩余 alloc → 警告（问题 5）
        剩余 enque → 警告（问题 6）
```

`findDef` 的回溯是精髓：free 的对象通常是 **deque 的结果**而不是 alloc 的结果（02 的 compute 里 `x_local = ...deque(...)` 后 `free_tensor(x_local)`），所以要先沿「deque 的张量 → 配对 enque 的张量 → 递归」找回最初的 alloc，才能在账上销对那笔。

#### 4.4.3 源码精读

- [lib/Dialect/Asc/Transforms/VerifySync.cpp:30-40](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/VerifySync.cpp#L30-L40)——`findDef` 递归回溯：张量的定义 Op 是 `deque_tensor` 就跳到 `deqToEnq[op]` 配对的 enque 张量继续找；是 `alloc_tensor` 就返回。这就是「三面账本」中把 deque 结果归宗到 alloc 的那根线。
- [lib/Dialect/Asc/Transforms/VerifySync.cpp:77-112](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/VerifySync.cpp#L77-L112)——`dealTQueBindDequeTensorOp`：第 85 行在账上找**第一个** enque（FIFO）；第 88-89 行记 `deqToEnq` 映射并销账；第 92-95 行筛出 enque 与 deque 之间的张量使用者（`opPrecedes` 双向夹逼），第 96-103 行逐个警告（问题 2）；找不到 enque 走第 105-111 行（问题 1），警告还 `attachNote` 指向队列声明的位置。
- [lib/Dialect/Asc/Transforms/VerifySync.cpp:43-75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/VerifySync.cpp#L43-L75)——`dealTQueBindFreeTensorOp`：第 50 行 `findDef` 回溯；第 52-58 行在账上按张量身份找那笔 alloc 销账，找不到即问题 4（第 62-66 行，note 指向张量声明）；回溯本身失败即问题 3（第 68-74 行）。
- [lib/Dialect/Asc/Transforms/VerifySync.cpp:114-155](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/VerifySync.cpp#L114-L155)——主 walk 与收尾扫描：第 126-136 行按操作类型分派四类事件；第 137-154 行遍历剩余账本，分别报问题 5/6。整个 Pass 没有一处 `signalPassFailure`——它是纯告警器。

顺带指出一个源码级发现（读码训练）：`findDef` 里 `deqToEnq[op]` 用的是 `unordered_map::operator[]`，映射缺失时会默认构造一个空 Op 再取 `.getTensor()`——若某 deque 已因问题 1 警告过、没有配对记录，其后的 `free_tensor` 回溯会踩到这个无防护路径。这是「校验器自身假设被校验对象总是合规」的典型弱点，也正好说明下面的实践为什么要两个变体都做。

#### 4.4.4 代码实践

**实践目标**：人为制造队列纪律错误，看 `verify_sync=True` 能否检出、以何种形式报告。

**操作步骤（变体 A：删 free，行为确定，推荐先做）**：

1. 复制 02 示例为 `add_framework_verify.py`，把 compute 里的 [add_framework.py:71](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L71) `in_queue_x.free_tensor(x_local)` 删掉。
2. 启动行追加编译选项（不传 insert_sync，保持默认，校验的才是用户代码）：
   ```python
   vadd_kernel[USE_CORE_NUM, rt.current_stream()](x, y, z, block_length, tile_length, verify_sync=True)
   ```
3. 运行 `python3 add_framework_verify.py -r Model`，注意看 stderr。

**需要观察的现象（变体 A）**：每轮迭代账上都会多一笔未销的 alloc，编译期应出现 `alloc_tensor: there is no corresponding call to free_tensor for this tensor` 警告（附着源码位置 note）；编译不失败、继续走完。警告的具体呈现格式（是否带文件行号前缀）待本地验证。

**操作步骤（变体 B：删 enque，即大纲原始设计）**：把 [add_framework.py:58](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L58) 的 `in_queue_x.enque(x_local)` 删掉，同样加 `verify_sync=True` 运行。

**需要观察的现象（变体 B）**：预期 compute 的 deque 报问题 1 警告（`deque_tensor: there is no corresponding call to enque_tensor`）。但如 4.4.3 所析，其后 `free_tensor(x_local)` 的回溯依赖这条缺失的 deque→enque 映射，无防护路径可能引发进程异常退出——能否走到运行阶段、运行期 DeQue 空队列在 Model 仿真下的表现（死等或读脏数据），均待本地验证。若确实异常退出，这本身就是 VerifySync 实现边界的一次实证。

**预期结果**：变体 A 给出确定的编译期检出；变体 B 至少应产出问题 1 的警告（在其回溯路径出问题之前）。对照总结：`verify_sync` 是 lint 不是门禁——它提醒但不拦截，真正的硬失败只存在于 EraseSync 的 alloc 缺失检查。

#### 4.4.5 小练习与答案

**练习 1**：为什么 VerifySync 排在 `strip_debug_info` 之前？

**答案**：警告要用 `attachNote`/位置信息指认「哪一行 kernel 代码出的问题」，而 strip_loc 会把这些定位信息从 IR 上剥掉。先校验后剥离，诊断信息完整；这也是 u6-l1 讲过的通用原则——一切依赖源码定位的诊断都要在 strip 之前。

**练习 2**：deque 消费账上的**第一个** enque 而不是最后一个，体现了什么假设？

**答案**：队列 FIFO 语义——先入队的缓冲先被取出（双缓冲乒乓正是靠它保证缓冲不被提前覆写，u2-l4）。校验器按 FIFO 配对，等价于按程序顺序重放队列状态；若按任意配对，双缓冲场景下「free 早于最后使用」这类问题就会漏报。

**练习 3**：`verify_sync=True` 且 `insert_sync=True` 同时开启，校验的是谁的纪律？

**答案**：校验的是重建后的程序。insert_sync 链在 optimizing 阶段已把用户写的 enque/deque 全部删除并按依赖分析重插（4.1、4.3），postprocessing 末尾的 VerifySync 看到的是编译器自己生成的配对——通常天然合规。所以想用 VerifySync 检查**自己**的队列用法，就不要同时强制 insert_sync。

## 5. 综合实践

把本讲四个 Pass 串成一份「同步重建观察报告」。素材：01_add（手动风格）与 02_add_framework（框架风格）各复制三份，共做四组实验：

| 组 | 素材 | 改动 | dump 目录 | 关注点 |
| --- | --- | --- | --- | --- |
| A | 01_add | 无（默认） | `/tmp/gA` | 基线：产物中的 6 对 SetFlag/WaitFlag |
| B | 01_add | 启动行加 `insert_sync=True` | `/tmp/gB` | SetFlag/WaitFlag 消失，PipeBarrier(PIPE_V/ALL) 出现 |
| C | 02_add_framework | 无（默认） | `/tmp/gC` | 基线：EnQue/DeQue 在用户写的位置 |
| D | 02_add_framework | `insert_sync=True` | `/tmp/gD` | EnQue/DeQue 被重排到生产者后/消费者前 |

对每组导出 `ascendc.cpp` 与 `ascir.mlir`（`PYASC_DUMP_PATH` 一并导出，u1-l5），然后完成三张表：

1. **ascendc.cpp 同步指令对照表**：每行 `SetFlag/WaitFlag/PipeBarrier/EnQue/DeQue` 记一条（grep 即可），标注它来自哪个 Pass 的哪条规则（enqueueTensors / dequeueTensors / syncGetSetValue / canonicalizeBarriers / 用户原样保留）。
2. **IR 证据链**：在 `ascir.mlir`（Pass 后）里找 `ascendc.que_bind.enque_tensor / deque_tensor / set_flag v_s / pipe_barrier pipe_all`，与 lit 测试 [insert-sync.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/insert-sync.mlir) 的 CHECK 行互相印证。
3. **结论段**：用自己的话回答——「插入同步」这个名字里的「插入」到底插了什么？（预期答案：插的是队列纪律与屏障结构，显式事件对只覆盖标量场景；真正落到硬件的事件等待由 Ascend C 队列实现与毕昇 `--cce-auto-sync` 完成。）

若环境允许（`PYASC_SETUP_DEVTOOLS=1` 构建，见 u7-l5），再加一步：用 `ascir-opt -ascendc-erase-sync -ascendc-hoist-que-bind -ascendc-insert-sync` 依次跑 D 组 dump 出的 Pass 前 IR（`codegen.mlir`），对比与 Python 流程的 `ascir.mlir` 是否一致——这是把「Pass 链」从黑盒变成白盒的最后一块拼图。无 devtools 环境下此步待本地验证。

## 6. 本讲小结

- 同步重建链固定四步：**EraseSync 拆除 → HoistQueBind 上提基础设施 → InsertSync 重建 → UnifyPipe 合并 pipe**，由 `insert_sync` 开关控制；该开关三态（True/False/None 自动判定），`None` 时 IR 中出现 `local_tensor_auto` 才默认开启，故 01/02 示例须显式传 `insert_sync=True`。
- EraseSync 删五类同步 Op（enque/deque/set_flag/wait_flag/pipe_barrier）但保留 alloc/free；deque 结果用同队列的 alloc 张量替换以维持 SSA；唯一硬失败是「deque 的队列上从未 alloc」。
- HoistQueBind 用泛型 `HoistOpPattern` + 支配检查，把 queue/que_bind/tbuf/init 系六类基础设施 Op 逐层提升到函数体根部；alloc/free 因属循环语义而不提升。
- InsertSync 的依赖分析：**写 dst 的算子即生产者**（enque 紧随其后），**enque 之后最早的张量使用者即消费者**（deque 插在其前）；无队列张量退化为 `PipeBarrier<PIPE_V>`，标量 Get/Set 用 V_S/S_V 显式 SetFlag/WaitFlag，末尾补 PIPE_ALL 并折叠相邻屏障。先后判定的基石是 `opPrecedes`（块内线性序 + 跨块支配树）。
- VerifySync 是挂在 postprocessing 末尾的可选 lint：按队列记账检查 alloc↔free、enque↔deque 配对及 enque-deque 区间使用，输出 warning 而不拦截编译；它假设被校验程序基本合规，回溯路径缺防护（4.4.3），使用时注意变体选择。

## 7. 下一步学习建议

本讲走完了 optimizing 阶段的全部自定义内容。下一讲（u6-l4）进入 postprocessing：`GenerateBoilerplatePass` 如何生成 extern 声明与 kernel 入口样板、`LegalizeKernelArgs` 如何把 kernel 参数改写为 Ascend C 形参、`DeclarePyStructPass` 如何为 Struct 参数生成 C 结构体——你在本讲 D 组 dump 里看到的 ascendc.cpp 函数外壳，就是它们的产物。想先动手的读者，推荐两条支线：① 用 `print_ir_before_all=True`（u3-l4）重跑 D 组实验，逐 Pass 观察重建链每一步前后的 IR；② 阅读 [python/test/kernels/insert_sync/](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/kernels/insert_sync/test_vadd.py) 下的端到端用例（test_vadd/test_softmax/test_matmul 等），它们是「不写一行同步」的惰性风格实战样本，与本讲的自动判定逻辑互为印证。
