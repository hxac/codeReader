# 代码外提、循环分裂与模式重写

## 1. 本讲目标

本讲聚焦 cuTile 优化流水线后半段的四个 IR 变换 pass：循环不变量外提（`hoist_loop_invariants`）、分区视图反外提（`unhoist_partition_views`）、循环分裂（`split_loops`）、模式重写（`rewrite_patterns`，重点是 FMA 融合）。

学完后你应该能够：

- 说清「为什么 `hoist_loop_invariants` 必须排在 token 排序 pass 之后」，以及它如何用一个「可移动性分类 + 数据依赖深度」的单遍算法决定每条指令去留。
- 解释 `unhoist_partition_views` 是为了修补 code motion 在旧字节码版本下造成的 `MakePartitionView` 错位。
- 描述 `split_loops` 如何用「循环不变量 `if` 条件」把一个带分支的循环拆成两段无分支循环。
- 读懂 `rewrite_patterns` 的「模式注册 + 匹配 + 安全校验 + 应用」框架，并理解 FMA 融合的安全边界（为何 `tmp = tx*tx; tmp + tmp` 不能融合）。

本讲承接 u6-l1（pass 流水线总览）与 u6-l2/u6-l3（数据流分析与 token 排序）。这三讲共享同一份 IR 与同一套「副作用 / 内存效应」概念，本讲是它们在「循环与算术局部优化」上的收尾。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，IR 的执行模型是「树状嵌套 Block」。** 一个内核函数体是根 `Block`，循环（`Loop`）、分支（`IfElse`）、归约（`TileReduce`）、扫描（`TileScan`）这类带嵌套 region 的操作会挂一棵子 `Block`（`nested_block`）。优化 pass 通常要遍历整棵树，并可能在树的各层之间搬动指令。本讲四个 pass 全是「重写树」的工作。

**第二，指令能不能搬，取决于「副作用」与「数据依赖」。** 纯计算指令（算术、形状操作）只读输入、只产结果，原则上可移动；带副作用的指令（`MemoryEffect.STORE`、`Return`）一旦移动会改变可观测行为，不可移动；跳转指令（`Continue`/`Break`）绑定了控制流结构，只能跟所在循环一起移动。这是 u6-l1 引入的 `MemoryEffect` 枚举在本讲的直接应用。

**第三，循环是性能热点，循环体里的「不变量」是免费午餐。** 若循环体里某条指令的结果不依赖任何循环携带值，那它每轮算的都是同一个值，把它提到循环外只算一次，就能省下 \(N-1\) 次计算。但「能不能安全地提」需要精确的数据流推理——这正是 `hoist_loop_invariants` 要解决的问题。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/cuda/tile/_passes/code_motion.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/code_motion.py) | 循环不变量外提 `hoist_loop_invariants`：单遍线性算法，按可移动性与依赖深度把指令搬到尽可能外层的循环体。 |
| [src/cuda/tile/_passes/unhoist_partition_views.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/unhoist_partition_views.py) | `unhoist_partition_views`：在旧字节码版本下，把被外提的 `MakePartitionView` 克隆回其消费者所在的块。 |
| [src/cuda/tile/_passes/loop_split.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/loop_split.py) | `split_loops`：识别「条件为归纳变量 vs 循环不变量」的 `if`，把一个循环拆成保留不同分支的两段循环。 |
| [src/cuda/tile/_passes/rewrite_patterns.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/rewrite_patterns.py) | `rewrite_patterns`：模式匹配框架与 FMA 融合，把 `mul`+`add/sub` 重写为单条 `fma`。 |
| [src/cuda/tile/_compile.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py) | `_transform_ir`：调用上述 pass 的总编排，决定了它们的执行顺序。 |

四个 pass 在 [_transform_ir 中的调用顺序](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L108-L120) 是：

```
rewrite_patterns          # 算术局部重写（FMA 等）
hoist_loop_invariants     # 循环不变量外提
unhoist_partition_views   # 仅当 bytecode_version < V_13_3
split_loops               # 循环分裂
dead_code_elimination_pass
```

注意两点：`rewrite_patterns` 在 `hoist_loop_invariants` 之前（先融合再外提，避免外提打断可融合的算子对）；`unhoist_partition_views` 是 `hoist_loop_invariants` 的「善后」，紧跟其后。本讲按概念分组讲解，顺序为 code motion → 反外提 → 循环分裂 → 模式重写。

## 4. 核心概念与源码讲解

### 4.1 循环不变量外提：hoist_loop_invariants

#### 4.1.1 概念说明

「循环不变量外提」（Loop-Invariant Code Motion, LICM）是经典编译优化：若循环体里某条指令的结果在所有迭代中都不变，就把它提到循环外面，只执行一次。

难点不在「想」，而在「判断某条指令到底能提到哪一层」。考虑嵌套循环：

```
for i:              # 深度 1
  a = x + y         # x, y 是参数（定义在深度 0）→ 与 i 无关，可提到 for i 外
  for j:            # 深度 2
    b = a + j       # 依赖 a（若 a 已外提，定义在深度 0）和 j（深度 2）→ 可提到 for j 外但不依赖 j? 不，依赖 j，留原处
    store(out, b)   # STORE，整块不可移动
```

可见每条指令的「最高可外提层」由它**所有输入的定义深度**决定：只能提到比「最深的外部依赖」更深的那一层循环体里。同时，副作用、跳转会改变整块的「可移动性」。`hoist_loop_invariants` 用一个精巧的单遍算法同时算清这两件事。

#### 4.1.2 核心流程

算法用两个维度刻画「可移动性」：

**块的静态可移动性 `_BlockMobility`**（三档，值越小越不可动）：

| 取值 | 含义 | 触发条件 |
| --- | --- | --- |
| `IMMOVABLE` (0) | 本块及所有祖先块都不能动 | 含 `STORE` 或 `Return` |
| `CAN_MOVE_WITH_LOOP` (1) | 本块自身不能动，但**所在的循环**仍可能被整体处理 | 含 `Continue`/`Break` |
| `CAN_MOVE` (2) | 纯块，可动（最终能否动还看数据依赖） | 其余 |

**指令的动态依赖信息 `_DependencyInfo`**：

- `must_stay`：本指令不能离开当前块（但当前块作为整体仍可能被搬）。
- `max_outside_depth`：所有「定义在当前块之外」的输入里，最深的那个定义深度——即本指令能外提到的最浅层数。

判定一条指令能否外提的核心，是 `_DependencyInfo.update`：若某个输入与指令定义在**同一块**（依赖深度 ≥ 当前块深度），则指令 `must_stay`；否则更新 `max_outside_depth`。

主循环对每条指令算出 `target_depth`：

- 若 `must_stay`，留在原层，并把 `max_outside_depth` 上报给块的 `min_depth`。
- 否则，沿父链向上走，**只要父块是循环体（`is_loop_body`）且目标层仍在依赖深度之外**，就一直外提。

这里有一个关键约束：**只能把指令提出「循环」，不能提出 `IfElse` 分支**。因为把指令提出分支会改变分支语义（原本只在某条件成立时执行，外提后就总是执行）。

整棵树用一次深度优先遍历处理完毕，复杂度是线性的——这正是源码注释强调的「single linear-time pass, no matter how many nested loops」。

#### 4.1.3 源码精读

入口极其简短：用根 `Block` 的参数初始化 `def_depth`（每个参数定义为深度 0），然后启动递归 [_passes/code_motion.py:L14-L16](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/code_motion.py#L14-L16)。`def_depth[var.name]` 记录每个变量是在第几层被定义的，是整个算法的「地图」。

依赖深度的更新规则在 [_DependencyInfo.update](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/code_motion.py#L56-L63)：同块依赖置 `must_stay`，跨块依赖取最大深度。注意注释特别说明——`must_stay` 不影响 `max_outside_depth`，因为「整块可能被一起搬」，届时块内同块依赖会自然跟随。

主函数 `_hoist` 对每条 op 按类型分类处理 [_passes/code_motion.py:L81-L124](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/code_motion.py#L81-L124)，要点如下：

- **循环类（`Loop`/`TileReduce`/`TileScan`）**：先把循环体参数登记为「定义在 `depth+1`」，递归处理循环体；若循环体 `IMMOVABLE`，则整条循环 `IMMOVABLE`；循环自身的输入（`Loop.initial_values` 或归约/扫描的 `xs`）参与依赖计算。非循环块（`is_loop_body=False`）默认 `must_stay=True`，即一开始就禁止把指令提出 `IfElse` 分支——见 [_passes/code_motion.py:L83](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/code_motion.py#L83)。
- **副作用与返回**：`STORE` 或 `Return` 立即把整块标为 `IMMOVABLE` [_passes/code_motion.py:L108-L110](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/code_motion.py#L108-L110)。
- **跳转**：`Continue`/`Break` 标为 `CAN_MOVE_WITH_LOOP`，本块自身不能动 [_passes/code_motion.py:L111-L115](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/code_motion.py#L111-L115)。

外提决策与「写入新块」在这里完成 [_passes/code_motion.py:L126-L139](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/code_motion.py#L126-L139)：

```python
target_depth = depth
if depinfo.must_stay:
    ret.min_depth = max(ret.min_depth, depinfo.max_outside_depth)
else:
    while target_depth > depinfo.max_outside_depth and stack[target_depth].is_loop_body:
        target_depth -= 1
stack[target_depth].new_block.append(op)
for v in op.result_vars:
    def_depth[v.name] = target_depth   # 关键：用外提后的深度登记结果
```

最末一行是算法的灵魂：**结果变量的定义深度按「外提后的 `target_depth`」登记**，而非原深度。这样后续指令看到的就是「假设本指令已外提」后的世界，依赖计算才正确。`stack` 保存的是每层「正在构建的新块」，最后用 `block[:] = new_block.detach_all()` 一次性替换原块 [_passes/code_motion.py:L142](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/code_motion.py#L142)。

最后回答本讲开篇的问题——**为何必须排在 token 排序 pass 之后**？答案就在 [_compile.py 的注释](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L110-L112)：在 token pass 之前，load 操作还没有挂上定序 token；若先外提，可能把一个本应留在循环内参与内存定序的 load 错误地提出循环，破坏 u6-l3 讲的 memory token chain。token pass 先把每个访存操作钉在正确的定序位置上，外提再据此安全搬动纯计算。

#### 4.1.4 代码实践

**实践目标**：观察循环不变量外提的实际效果。

**操作步骤**（源码阅读型实践）：

1. 阅读 `code_motion.py`，找到 `STORE` 把块标为 `IMMOVABLE` 的那一行，确认：**只要循环体内有任何一次 `ct.store`，整个循环就不能被整体外提**。这与 u4-l1 的 persistent 内核（循环体内反复 store）相吻合。
2. 构造一个对照内核（仅作思考实验，不必运行）：

   ```python
   # 示例代码：仅用于说明，非项目原有文件
   @ct.kernel
   def k(x, out, TILE: ct.Constant[int]):
       bidx = ct.bid(0)
       # c 不依赖任何循环携带值，是不变量
       c = ct.load(x, index=(0,), shape=(TILE,))
       acc = c   # 累加器初值
       for i in range(4):
           acc = acc + c   # c 每轮都一样
       ct.store(out, index=(bidx,), tile=acc)
   ```

3. 预测：`c = ct.load(...)` 这条 load 的输入只有参数 `x` 与常量 `TILE`，二者 `def_depth=0`，故它的 `max_outside_depth=0`，可被外提到循环外。但由于循环体内有 `ct.store`，循环块本身 `IMMOVABLE`，因此 load 不会跑出最外层函数块——它会被提到 `for` 循环**之前**、函数体之内。

**需要观察的现象**：dump 出的 IR 中，`tile_load`（生成 `c`）应出现在 `loop` 操作之前，而循环体内只剩 `add` 与累加器回传。

**预期结果**：load 从循环体内上移到循环体正前方，循环体里不再有 load。

**待本地验证**：实际 dump 文本需在本地用 `CUDA_TILE_LOGS=log_cutile_ir` 启动内核后查看，本讲无法代为运行。

#### 4.1.5 小练习与答案

**练习 1**：若把上面内核里的 `ct.store` 删掉，`hoist_loop_invariants` 的行为会改变吗？

**答案**：load 仍会被外提到循环前。删掉 store 只影响「循环块是否 `IMMOVABLE`」（变为可整体移动），但 load 的外提由它自身的依赖深度决定，结论不变。

**练习 2**：为什么 `_hoist` 在登记结果变量定义深度时，用的是外提后的 `target_depth` 而不是原始 `depth`？

**答案**：因为后续指令的依赖计算应基于「外提后的实际位置」。若仍按原深度登记，依赖该结果的下一条指令会低估它的可达外提层，导致次优外提。

---

### 4.2 分区视图反外提：unhoist_partition_views

#### 4.2.1 概念说明

这个 pass 是 `hoist_loop_invariants` 的「善后修补」，理解它需要先知道 `MakePartitionView` 是什么。

在 cuTile 里，`TiledView`、`num_tiles` 等机制底层依赖一种「分区视图」（PartitionView）——它由 `MakePartitionView` 操作从一个 `Array` 派生出来，记录「这块数组按什么瓦片形状切分」。`TileLoad`/`TileStore`/`NumTiles` 都通过 `view` 操作数引用一个分区视图。

问题来了：在 **字节码版本 < V_13_3** 时，后端要求 `MakePartitionView` 必须**紧贴在它的消费者之前**被发射（inline）。但 `hoist_loop_invariants` 可能为了外提循环不变量，把一个 `MakePartitionView` 提到了外层块；这时深层的 `TileLoad` 引用的就是一个「定义在祖先块」的视图，违反了后端的顺序约束。

`unhoist_partition_views` 的职责就是：把这类「被外提的」`MakePartitionView` **克隆一份回消费者所在的块**，并改写消费者引用克隆出的新视图。

#### 4.2.2 核心流程

一次先序遍历，维护一张表 `def_info`：变量名 → (定义它的 op, 定义它的 block)。对每个块内的每条 op：

1. 若它是 `TileLoad`/`TileStore`/`NumTiles`，查它的 `view` 操作数在哪里定义。
2. 若定义者是个 `MakePartitionView` **且定义在别的块**（`def_block is not block`），说明它被外提了：克隆该 `MakePartitionView` 到当前块，用 `dataclasses.replace(op, view=...)` 改写消费者的 `view`。
3. 递归处理嵌套块，最后把当前 op 加入新块并登记其结果变量。

#### 4.2.3 源码精读

入口 [unhoist_partition_views.py:L10-L12](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/unhoist_partition_views.py#L10-L12) 仅启动遍历。核心逻辑在 [_unhoist](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/unhoist_partition_views.py#L15-L32)，关键三行：

```python
if isinstance(op, (TileLoad, TileStore, NumTiles)):
    view_def, def_block = def_info[op.view.name]
    if isinstance(view_def, MakePartitionView) and def_block is not block:
        mapper = Mapper(block.ctx)
        new_block.append(view_def.clone(mapper))
        op = dataclasses.replace(op, view=mapper.get_var(op.view))
```

注意三处细节：

- `view_def.clone(mapper)` 用 `Mapper`（u5-l5 讲过的变量重映射工具）克隆出一个全新的 `MakePartitionView`，其结果变量名也由 mapper 重命名，避免与原定义冲突。
- `dataclasses.replace` 生成一个仅 `view` 字段被替换的新 op——因为 cuTile 的 Operation 是 dataclass，可廉价复制。
- 这个 pass 只在新版字节码下不运行——见 [_compile.py:L114-L117](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L114-L117) 的版本门控 `if bytecode_version < BytecodeVersion.V_13_3`。

#### 4.2.4 代码实践

**实践目标**：理解版本门控与「克隆回填」语义。

**操作步骤**：

1. 在 [_compile.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L114-L117) 中确认 `unhoist_partition_views` 只在 `bytecode_version < V_13_3` 时调用。
2. 阅读本 pass源码，回答：克隆后的 `MakePartitionView` 是否会让原 `MakePartitionView` 变成死代码？由谁清理？

**需要观察的现象 / 预期结果**：克隆后，原本被外提的 `MakePartitionView` 若不再有任何消费者引用，将成为死代码；它由紧随 `split_loops` 之后的 [dead_code_elimination_pass](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L120) 清理（见 u6-l1）。这也解释了为何 `_transform_ir` 末尾要再跑一次 DCE。

**待本地验证**：在新版硬件上若默认字节码已是 V_13_3，本 pass 不触发；要观察其行为需强制降级字节码版本。

#### 4.2.5 小练习与答案

**练习**：为何用 `dataclasses.replace(op, view=...)` 而不是直接 `op.view = new_var`？

**答案**：因为 Operation 在 IR 中是不可变语义的值句柄，直接修改字段可能破坏「同一 op 对象在多处被引用」的一致性；`replace` 生成新对象更安全，且与算法「构建新块再整体替换」的风格一致（与 `_hoist` 末尾的 `block[:] = ...` 同构）。

---

### 4.3 循环分裂：split_loops

#### 4.3.1 概念说明

「循环分裂」处理一种常见模式：循环体内有一个 `if`，其条件是「归纳变量与某个循环不变量比较」，例如：

```python
for i in range(N):
    val = i
    if i >= 3:
        val *= 10
    store(x, i, val)
```

每轮迭代都要算一次 `i >= 3` 并分支。但当 `i` 从 0 走到 N-1 时，前若干轮 `i < 3`（走 else），后若干轮 `i >= 3`（走 then）。这个分支其实是**可预测的区间**。循环分裂把它拆成两段无分支循环：

```
第一段 for i in [0, 3):   只保留 else 分支逻辑
第二段 for i in [3, N):   只保留 then 分支逻辑
```

这样每段循环都没有 `if`，分支开销消失，对 GPU 这种对分支极敏感的硬件收益显著。

#### 4.3.2 核心流程

`split_loops` 分三步：

**第一步 `_find_splittable_loops`**：扫描 IR，找出「可分裂循环」。一个循环可分裂，需满足：

- 是 `for` 循环（`is_for_loop`，即有 `start`），且步长是常量 `1`。
- 循环体内有比较操作（`ge/gt/le/lt`），其中一侧是归纳变量，另一侧是**循环不变量**（定义深度小于循环深度）。
- 存在一个 `IfElse`，其 `cond` 正是上述比较的结果。

扫描时用 `equiv_map` 跟踪 `Assign` 别名（变量间的等价关系），用 `comparisons` 把「比较结果变量 → (比较函数, 不变量)」记录下来，再在遇到 `IfElse` 时关联到所属循环。

**第二步筛选**：`split_loops` 目前**只处理恰好含一个可分裂 `if` 的循环**（`if len(if_ops) != 1: continue`）。

**第三步 `_split_loop`**：把原循环克隆成两份。计算分裂点：

- 第一段 stop = \(\min(\text{原 stop}, \text{分裂值})\)
- 第二段 start = \(\max(\text{原 start}, \text{分裂值})\)

对 `gt`/`le` 要把分裂值 `+1`（因为 `i > 3` 等价于 `i >= 4`）。两段循环各自只保留 `if` 的一个分支（通过 `_clone_loop` 的 `branch_to_keep` 参数，把 `IfElse` 直接展平成单分支）。两段之间用一组「中间携带变量」衔接（第一段的结果作为第二段的初值）。

哪个分支留哪段，由 `_BRANCH_TO_KEEP` 决定：

| 比较函数 | 第一段保留 | 第二段保留 |
| --- | --- | --- |
| `ge`/`gt` | `else_block` | `then_block` |
| `le`/`lt` | `then_block` | `else_block` |

直觉：`if i >= 3` 中，第一段 `i < 3` 时条件为假走 else，第二段 `i >= 3` 时条件为真走 then。

#### 4.3.3 源码精读

候选识别在 [_find_splittable_loops](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/loop_split.py#L22-L66)。比较匹配的关键判断 [_passes/loop_split.py:L31-L38](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/loop_split.py#L31-L38)：

```python
if lhs.name == induction_var and def_depth[rhs.name] < depth:
    comparisons[op.result_var.name] = _Condition(op.fn, rhs)
elif rhs.name == induction_var and def_depth[lhs.name] < depth:
    comparisons[op.result_var.name] = _Condition(_FLIP[op.fn], lhs)
```

注意 `_FLIP`：当归纳变量在比较的右侧时，要翻转比较方向（`i <= v` 的 `v >= i` 翻成 `ge`），以统一成「归纳变量在左侧」的规范形式。

合法循环的判定 [_passes/loop_split.py:L51-L53](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/loop_split.py#L51-L53) 要求 `is_for_loop` 且步长常量等于 1。

分裂点的算术在 [_split_loop](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/loop_split.py#L93-L135)（注意：用 `min`/`max` 操作夹出两段边界）：

```python
new_block.append(RawBinaryArithmeticOperation(fn="min", lhs=loop.stop, rhs=split_value, ...))   # 第一段 stop
new_block.append(RawBinaryArithmeticOperation(fn="max", lhs=loop.start, rhs=split_value, ...))  # 第二段 start
```

中间携带变量的衔接 [_passes/loop_split.py:L124-L135](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/loop_split.py#L124-L135)：第一段循环的 `result_vars` 写入 `intermediate_vars`，第二段循环以 `intermediate_vars` 为 `initial_values`、以原 `loop.result_vars` 为最终结果，形成接力。

`_clone_loop` [_passes/loop_split.py:L138-L166](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/loop_split.py#L138-L166) 用 `Mapper` 克隆循环体；遇到要展平的 `IfElse` 时，只复制保留分支里的 op，并在 `EndBranch` 处把分支的输出变量映射回原 `IfElse` 的结果变量，从而正确衔接携带值。

#### 4.3.4 代码实践

**实践目标**：复现项目测试 `test_loop_split.py::test_split_ge` 的内核，确认循环被一分为二。

**操作步骤**：

1. 阅读 [test/test_loop_split.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_loop_split.py) 中的内核与断言。该测试断言 `len(loop_ops) == 2`，正是「分裂后变成两个 Loop」的直接证据。
2. 用 `compile_tile` 取 final IR 计数 Loop（参考测试写法）：

   ```python
   # 示例代码：改编自 test/test_loop_split.py
   from cuda.tile._ir.ops import Loop
   from cuda.tile._compile import compile_tile
   sig = ct.compilation.KernelSignature.from_kernel_args(
       split_ge_kernel, (x,),
       ct.compilation.CallingConvention.cutile_python_v1())
   [root_block] = compile_tile(split_ge_kernel._pyfunc, [sig],
                               return_final_ir=True, return_cubin=False).final_ir
   print(sum(1 for op in root_block.traverse() if isinstance(op, Loop)))
   ```

**需要观察的现象**：打印值为 `2`（分裂后两个循环），且 `ct.launch` 后 `x` 的内容为 `[0,1,2,30,40,...,90]`。

**预期结果**：`i < 3` 段写入原值，`i >= 3` 段写入 `i*10`，与未优化版本数值完全一致。

**待本地验证**：本讲未在 GPU 环境运行，数值与计数需本地执行确认。

#### 4.3.5 小练习与答案

**练习 1**：为何 `split_loops` 要求步长必须是常量 `1`？

**答案**：分裂点用 `min`/`max` 算两段边界，并假设 `i` 逐一递增。若步长非 1，分裂值的边界换算（`gt`/`le` 的 `+1` 调整）与「区间恰好覆盖原迭代点」的推理都会失效。

**练习 2**：若一个循环里有两个不同的可分裂 `if`，`split_loops` 会处理吗？

**答案**：不会。当前实现要求 `len(if_ops) == 1`（[loop_split.py:L177-L178](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/loop_split.py#L177-L178)），多个分支的情况被保守跳过，留待后续扩展。

---

### 4.4 模式重写与 FMA 融合：rewrite_patterns

#### 4.4.1 概念说明

「模式重写」（peephole rewrite）是一类局部优化：识别 IR 中固定形状的「指令子图」，用一条更优的指令替换。cuTile 的 `rewrite_patterns` 目前实现的最重要模式是 **FMA 融合**（Fused Multiply-Add）。

FMA 指令在 GPU 上是一条硬件指令，一次完成 \( a \times b + c \)，且**只舍入一次**——比「先 `mul` 再 `add`」两条指令更快、精度更高（两条指令会舍入两次）。所以把 `x*y + z` 融合成 `fma(x, y, z)` 是双赢：更快又更准。

但融合不能乱来。FMA 只对**非受限浮点**（unrestricted float，如 float32/float16/bfloat16，不含 tfloat32/float8 这类 RestrictedFloat）成立，且参与的两条指令的舍入模式（`rounding_mode`）和 flush-to-zero 标志必须一致，否则融合会改变数值结果。此外还要处理 `sub`：`x*y - z` 等价于 `fma(x, y, -z)`，需插入一个取负指令。

#### 4.4.2 核心流程

`rewrite_patterns` 是一个微型「模式匹配引擎」，三件套：

**注册（`@pattern`）**：把一个匹配函数挂到某种 op 类上。每个模式有唯一 `pattern_id`。

**匹配（driver 的第一遍）**：遍历所有 op，对每个 op 试所有适用模式；匹配成功就把「结果变量名 → 匹配信息」存进 `_matches[pattern_id]`。匹配失败用抛 `NoMatch` 表达（不是错误，是「这条不适用」）。

**应用（driver 的第二遍 + `_apply_rewrites`）**：对每条提议的重写做**安全校验**，通过后才真正替换。

FMA 融合由两个模式协作：

- `match_float_mul`：识别「非受限浮点的 `mul`」，记录其 op 供下游查找。
- `fuse_mul_addsub`：识别 `add`/`sub`，查它的某个操作数是不是一个被 `match_float_mul` 命中的 mul；若是，校验舍入/ftz 一致，对 `sub` 插 neg，最后提议用 `FusedMulAddOperation` 替换 (mul, add/sub) 两条 op。

安全校验是本 pass 的精华，专门防「重写破坏正确性」。四道关卡：

1. 被删的 op 不能已参与过其他重写。
2. 被删 op 的「已删除结果变量」若有外部使用者（不在本次 `to_remove` 内），不能重写——否则外部引用会悬空。
3. 新生成的 op 不能用到「被删除的结果变量」。
4. （当前简化）只在单结果子图上做。

#### 4.4.3 源码精读

两个核心 FMA 模式。先是浮点乘识别 [_passes/rewrite_patterns.py:L83-L90](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/rewrite_patterns.py#L83-L90)：

```python
@pattern(RawBinaryArithmeticOperation)
def match_float_mul(op, ctx):
    if op.fn != "mul":
        raise NoMatch("not a mul binop")
    if not datatype.is_unrestricted_float(get_dtype(ctx.typeof(op.result_var))):
        raise NoMatch("not an unrestricted float mul")
    return op
```

注意 `raise NoMatch` 就是「本模式不匹配」，驱动会静默跳过。

融合模式 [fuse_mul_addsub](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/rewrite_patterns.py#L93-L133) 查 add/sub 的操作数是否为浮点 mul，校验 `rounding_mode` 与 `flush_to_zero` 一致，对 `sub` 在合适一侧插 `Unary(fn="neg")`，最后发射 `FusedMulAddOperation` 并登记重写 `ctx.add_rewrite((mul_op, op), new_ops)`。`FusedMulAddOperation` 是一条三操作数 op（`lhs`/`rhs`/`acc`），见 [ops.py:L110-L115](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L110-L115)。

安全校验四关卡在 [rewrite_patterns 的第二遍](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/rewrite_patterns.py#L151-L173)，其中最微妙的是「新 op 不能使用被删除结果」[_passes/rewrite_patterns.py:L163-L166](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/rewrite_patterns.py#L163-L166)：

```python
new_inputs = set(v.name for op in r.to_add for v in op.all_inputs())
if deleted_results & new_inputs:
    # New operations use deleted results -- can't rewrite
    continue
```

这正解释了项目测试 `test_fma_skip_when_new_op_uses_deleted_var`：内核 `tmp = tx*tx; out = tmp + tmp`。这里 add 的两个操作数都指向同一个 `tmp`（即 mul 的结果）。融合时 `acc = op.rhs = tmp`，而 `tmp` 恰是 `to_remove` 中 mul_op 的结果——属于被删除结果。新生成的 fma 又要用 `tmp` 当 `acc`，于是 `deleted_results & new_inputs` 非空，重写被拒绝，回退为 mul+add。这条安全网保证了「自引用」情形不会被错误融合。

最后 `_apply_rewrites` [_passes/rewrite_patterns.py:L178-L192](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/rewrite_patterns.py#L178-L192) 重建每个块：保留未重写的 op，用新 op 序列替换被重写位置，丢弃已重写的旧 op。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：对比 FMA 融合前后的算术 IR，指出哪几条 `mul`+`add` 被合并成单条 `fma`。

**操作步骤**：

1. 复用 [test/test_fma.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_fma.py) 中的内核。重点看 `mul_add_kernel_local_var`：它写成两条语句 `tmp = tx * ty; output_tile = tmp + tz`，是 FMA 的典型输入。

2. **看「融合后」**：用 `CUDA_TILE_LOGS=log_cutile_ir` 启动内核，在 stderr 的 CuTile IR 中查找 `fma`：

   ```bash
   CUDA_TILE_LOGS=log_cutile_ir python -c "
   import torch, cuda.tile as ct
   # 导入或内联 mul_add_kernel_local_var 后：
   ...
   ct.launch(torch.cuda.current_stream(), grid, k, args)
   "
   ```

   或用程序化方式拿到 final IR 字符串：

   ```python
   # 示例代码
   from cuda.tile._compile import compile_tile
   sig = ct.compilation.KernelSignature.from_kernel_args(k, args, cc())
   [blk] = compile_tile(k._pyfunc, [sig], return_final_ir=True, return_cubin=False).final_ir
   print(blk.to_string())
   ```

3. **看「融合前」**（本地实验）：在一份临时副本里，把 [_compile.py 的 rewrite_patterns(func_body) 行](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L108) 注释掉，再 dump 同一内核的 IR，对比 mul/add 是否重新出现。**注意：这是本地临时修改，仅用于观察，勿提交。**

4. 对照测试的权威检查 [test_fma.py 的 filecheck 指令](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_fma.py#L113-L118)：`CHECK: fma`、`CHECK-NOT: mulf`、`CHECK-NOT: addf`、`CHECK-NOT: subf`。

**需要观察的现象**：

- 融合后：IR 中 `tx*ty` 的 `mul` 与 `+tz` 的 `add` **都不见了**，取而代之是一条 `fma(lhs=tx, rhs=ty, acc=tz)`。
- 融合前（注释 pass 后）：能看到一条 `mul`（产 `tmp`）紧接一条 `add`（消费 `tmp` 与 `tz`）。
- 对 `mul_sub_kernel`（`tx*ty - tz`）：融合后应是 `neg(tz)` + `fma(tx, ty, neg_tz)`，即多一条取负。
- 对 `mul_add_same_operand_kernel`（`tmp=tx*tx; tmp+tmp`）：融合被安全校验拒绝，仍保留 mul+add。

**预期结果**：`mul_add`/`add_mul` 融合为单条 fma；`mul_sub`/`sub_mul` 融合为 neg+fma；自引用内核不融合；所有内核数值与参考一致（atol=rtol=1e-3）。

**待本地验证**：本讲未在 GPU 环境运行，dump 文本与数值需本地执行确认。

#### 4.4.5 小练习与答案

**练习 1**：为什么 FMA 模式只匹配「非受限浮点」，而拒绝 tfloat32 的 `mul`？

**答案**：tfloat32 是 RestrictedFloat，不做隐式算术提升（u2-l4），且其算术语义与 FMA 硬件指令的舍入行为不能简单等同；贸然融合可能改变数值结果。`is_unrestricted_float` 守卫把这类类型排除在外。

**练习 2**：`fuse_mul_addsub` 为何要分别处理 `op.lhs` 和 `op.rhs` 两侧的 mul 匹配？

**答案**：因为 `add` 满足交换律，乘法可能在加号的左边（`(x*y)+z`）也可能在右边（`z+(x*y)`）。两侧各试一次才能覆盖 `mul_add_kernel` 与 `add_mul_kernel` 两种写法，并正确识别出「另一个操作数是累加器 `acc`」。

**练习 3**：若两条指令的 `rounding_mode` 不一致，会发生什么？

**答案**：`fuse_mul_addsub` 抛 `NoMatch("rounding mode mismatch")`（[rewrite_patterns.py:L108-L109](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/rewrite_patterns.py#L108-L109)），不融合。因为融合成单条 fma 只有一个舍入模式，无法同时还原两个不同的舍入行为。

## 5. 综合实践

把本讲四个 pass 串起来，跟踪一个含「循环 + 算术 + 分支」的内核，看它们如何接力改写 IR。

**任务内核**（综合 mul-add 与循环分裂）：

```python
# 示例代码：综合实践用，非项目原有文件
@ct.kernel
def combined(x, y, z, out, N: ct.Constant[int]):
    for i in range(N):
        a = ct.load(x, index=(i,), shape=(1,))
        b = ct.load(y, index=(i,), shape=(1,))
        c = ct.load(z, index=(i,), shape=(1,))
        r = a * b + c        # 候选 FMA
        if i >= 4:
            r = r * 2        # 候选循环分裂
        ct.store(out, index=(i,), tile=r)
```

**分析任务**（源码阅读 + 本地 dump 验证）：

1. **FMA**：`a * b + c` 应被 `rewrite_patterns` 融合为一条 `fma(a, b, c)`。
2. **循环分裂**：`if i >= 4`（归纳变量 vs 循环不变量 4）满足 `split_loops` 条件，循环应被拆成 `[0,4)` 与 `[4,N)` 两段，分别保留 else/then 分支。
3. **外提**：本内核循环体内有 store，循环块 `IMMOVABLE`，故循环不会被整体外提；但 `b`、`c` 等 load 的输入只有数组参数（不变量），会被 `hoist_loop_invariants` 提到循环前。
4. 用 `compile_tile(..., return_final_ir=True)` 打印 final IR，验证上述三点：是否存在 `fma`、是否出现两个 `loop`、load 是否在循环之前。

**预期结果**：final IR 中算术段只剩 fma（无独立 mul/add），结构段有两个循环，访存段 load 上移到循环外，整体数值与参考一致。

**待本地验证**：综合 dump 与数值需在本地 GPU 环境执行确认；本讲未代为运行。

## 6. 本讲小结

- `hoist_loop_invariants` 用「块可移动性三档 + 指令依赖深度」的单遍线性算法，把纯计算指令沿循环体向上外提；副作用（STORE/Return）锁死整块，跳转（Continue/Break）只能随循环整体移动；它必须排在 token 排序 pass 之后，否则会破坏内存定序。
- `unhoist_partition_views` 是 code motion 的善后：在字节码 < V_13_3 时把被外提的 `MakePartitionView` 克隆回消费者所在块，遗留的死代码由末尾 DCE 清理。
- `split_loops` 把「条件为归纳变量 vs 循环不变量」的单分支循环拆成两段无分支循环，用 `min`/`max` 夹边界、用中间携带变量接力，分支开销消失而数值不变。
- `rewrite_patterns` 是微型模式匹配引擎，FMA 融合把非受限浮点的 `mul`+`add/sub` 合成单条 `fma`，更快更准；四道安全校验（尤其「新 op 不能用被删结果」）守护正确性，使 `tmp=tx*tx; tmp+tmp` 这类自引用安全回退。
- 四个 pass 的执行顺序是 `rewrite_patterns → hoist → unhoist? → split → DCE`：先局部算术融合、再做循环变换，最后用 DCE 收尾。

## 7. 下一步学习建议

本讲完成了 u6「IR 优化 Pass」单元。建议下一步：

- 进入 **u7-l1（IR 到字节码）**，看这些被优化过的 IR 如何被 `generate_bytecode_for_kernel` 编码成线性字节码——你会重新遇到 `fma` opcode、`for` 循环的 region 编码，以及 `MakePartitionView` 的 inline 要求如何体现在字节码布局上。
- 若想再深入循环优化，可重读 [loop_split.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/loop_split.py) 的 `_clone_loop`，思考「多个可分裂 if」应如何扩展为多段分裂。
- 若关注算子融合的扩展点，可仿照 `@pattern` 在 `rewrite_patterns.py` 中注册新模式（如 `mul+mul` 的平方优化），并复用其 `MatchContext` 与安全校验框架。
