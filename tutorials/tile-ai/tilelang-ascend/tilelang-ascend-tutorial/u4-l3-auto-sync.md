# 自动同步插入

## 1. 本讲目标

u4-l2 我们学会了**手写** `T.set_flag / T.wait_flag / T.barrier_all` 等同步原语，也清楚 Ascend AI Core 内部 MTE2/MTE1/M/Fix/V/MTE3/S 多条硬件流水线**并行**推进，必须靠事件标志（HardEvent）显式约束先后。但手写 flag 既繁琐又易错：漏一个 `wait_flag` 就是数据竞争，多一个 `barrier_all` 就白白牺牲流水重叠。

本讲回答一个核心问题：**能不能让编译器替我们把同步插好？** 答案是能，这正是本讲两个 pass 的工作。

学完本讲你应当掌握：

1. 理解 tile-lang 自动同步插入的整体设计——三层同步机制（手写 flag、tile-lang 自动 pass、bisheng 自带 auto-sync）的关系与分工。
2. 掌握 `TL_ASCEND_AUTO_SYNC` 与 `TL_ASCEND_AUTO_SYNC_VS` 两个开关、以及「按 target 自动默认值」（pto 默认开 VS、ascendc 默认全关）的机制。
3. 读懂 `AscendSyncInsert`（全流水线、主 pass）与 `AscendSyncInsertVS`（V/S 轻量补充 pass）的核心算法：算子→流水线映射、冒险检测、循环展开/重建、`SyncGraph` 传递闭包优化。
4. 能对一个 `vec_add` kernel 开启自动同步、删除手写同步，并用 `get_kernel_source()` 对比生成代码里自动插入的 `SetFlag/WaitFlag/PipeBarrier`。

## 2. 前置知识

在进入自动同步前，请确认你已经理解以下概念（均来自前序讲义）：

- **Ascend 片上存储与流水线**（u1-l1、u3-l1、u3-l2）：GM、L1（Cube）、UB（Vector）、L0A/L0B/L0C；数据搬运与计算分属不同硬件流水线。
- **TIR 与 Pass**（u1-l3、u1-l5、u6-l1）：前端 DSL 被 `@T.prim_func` 解析成与后端无关的 TIR `PrimFunc`，再经 `LowerAndLegalize` 与 `OptimizeForTarget` 两阶段 Pass 流水线改写，最后 codegen 成 Ascend C 源码。
- **手写同步原语**（u4-l2）：`set_flag/wait_flag`（核内、按 HardEvent 配对）、`barrier_all/pipe_barrier`（屏障）、`set_cross_flag/wait_cross_flag`（核间）。本讲的自动 pass 生成的就是这些原语对应的 TIR intrinsic。
- **Developer 模式**（u3 系列、u4-l1）：用户只声明语义层操作（`T.copy`、`T.gemm_v0`、`T.tile.add`…），不关心物理流水线。自动同步正是为 Developer 模式量身打造——让你**完全不用手写 flag**。

一个关键直觉：**同步的本质是「跨硬件流水线的生产者-消费者依赖」**。同一条流水线内部一般是保序的（如标量流水线 PIPE_S、以及部分场景的搬运流水线），不需要显式同步；只有当「流水线 A 写了某块 buffer，流水线 B 要读/写同一块」时，才必须插同步。两个自动 pass 做的事，就是**扫描每条语句对 buffer 的读写，按所属流水线判断冒险，再在合适位置插入同步指令**。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/transform/ascend_sync_insert.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert.cc) | **主 pass** `AscendSyncInsert`：跟踪全部 7 条流水线，靠数据依赖 + `SyncGraph` 传递闭包插入同步。对应开关 `tl.ascend_auto_sync`。 |
| [src/transform/ascend_sync_insert_vs.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert_vs.cc) | **补充 pass** `AscendSyncInsertVS`：只跟踪 V/S/MTE2/MTE3，且仅当两侧至少一方是 V 或 S 才同步。对应开关 `tl.ascend_auto_sync_vs`。 |
| [src/transform/common/operation_config.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/common/operation_config.h) | **算子→流水线映射表** `GetOperationConfig()` 与 **HardEvent 命名表** `GetEventMapping()`。两个 pass 都靠它把每个算子归类到一条流水线。 |
| [tilelang/transform/pass_config.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/pass_config.py) | 配置项 `TL_ASCEND_AUTO_SYNC` / `TL_ASCEND_AUTO_SYNC_VS` 的定义，以及按 target 的默认值与「VS 关闭 bisheng 自带 auto-sync」的联动。 |
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py) | `OptimizeForTarget` 阶段，两个同步 pass 的**调用位置**（紧挨着、排在内存规划之后）。 |
| [src/target/codegen_ascend.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc) | codegen 端把自动 pass 产出的 `tl.ascend_auto_*` intrinsic 翻译成真实 `AscendC::PipeBarrier / SetFlag / WaitFlag` 调用。 |
| [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py) | 本讲实践基准：一个带**手写** `T.barrier_all()` 的 `vec_add` kernel。 |
| [testing/python/language/test_ascend_sync_insert_vs.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_ascend_sync_insert_vs.py) | VS pass 的全套测试，是验证「该插/不该插同步」最权威的参考。 |

> 说明：规格里列出的 `docs/tutorials/jit_compilation.md` 目前只有标题、无实质内容，故本讲以源码与测试为主要依据。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**① 配置与三层同步机制**、**② AscendSyncInsert 主 pass**、**③ AscendSyncInsertVS 补充 pass**。

### 4.1 配置与三层同步机制：自动同步怎么开、和谁配合

#### 4.1.1 概念说明

先把全局看清楚。tile-lang 在昇腾上的「同步」其实有**三条互相独立又可叠加**的来源：

1. **手写同步**（u4-l2）：用户在 kernel 里直接写 `T.set_flag / T.wait_flag / T.barrier_all`，编译器原样保留。控制力最强、最繁琐。
2. **tile-lang 自动同步 pass**（本讲）：在 codegen **之前**的 TIR 上分析数据依赖，把同步作为 `tl.ascend_auto_barrier / tl.ascend_auto_set_flag / tl.ascend_auto_wait_flag` intrinsic 显式插入 IR。最终在生成的 Ascend C 源码里能看到对应的 `AscendC::PipeBarrier / SetFlag / WaitFlag`。
3. **bisheng 编译器自带的 cce-auto-sync**：tile-lang codegen 出 C++ 源码后，交给 CANN 的 bisheng 编译器（ascendc 用 `-xasc`、pto 用 `-xcce`）编成 `.so`。bisheng 自己也能在编译期做 auto-sync，默认开启。

这三层的关键关系是：**第 2 层（tile-lang pass）一旦启用更精细的 VS pass，就会顺手把第 3 层（bisheng cce-auto-sync）关掉**，并提升到 `-O3`——因为既然 tile-lang 已经精确插好了同步，就不再需要编译器再做一遍粗粒度的 auto-sync，关掉它能拿到更好的流水重叠（详见 4.1.3）。

两个 pass 的分工：

| Pass | 注册名 | 开关 | 覆盖流水线 | 默认状态 |
|------|--------|------|-----------|---------|
| `AscendSyncInsert`（主/「sibling」） | `tl.AscendSyncInsert` | `tl.ascend_auto_sync` | 全部 7 条（MTE2/MTE1/M/MTE3/FIX/V/S） | 任何 target 都**默认关** |
| `AscendSyncInsertVS`（补充） | `tl.AscendSyncInsertVS` | `tl.ascend_auto_sync_vs` | 仅 V/S/MTE2/MTE3，且需≥1 方为 V 或 S | **pto 默认开**；ascendc 默认关 |

> 社区代码里常把 `AscendSyncInsert` 叫「sibling」（兄弟 pass），把 `AscendSyncInsertVS` 叫「VS」。两个 pass **紧挨着执行、先 sibling 后 VS**，VS 会识别并消费 sibling 已插入的屏障（见 4.3）。

#### 4.1.2 核心流程

配置如何从 Python 传到 C++ pass：

```text
用户代码 @tilelang.jit(pass_configs={TL_ASCEND_AUTO_SYNC: True})
        │
        ├─ process_default_pass_config(target, pass_configs)
        │     └─ 按 target 填默认值（pto → VS=True）；用户显式设置优先
        │
        ├─ resolve_compile_flags(...)  # 派生 bisheng 选项
        │     └─ 若 VS=True → cce_auto_sync=False, opt_level=3
        │
        ├─ PassContext 携带配置进入 OptimizeForTarget(...)
        │     └─ ... AscendSyncInsert(target, platform)   # 读 tl.ascend_auto_sync
        │     └─ ... AscendSyncInsertVS(target, platform)  # 读 tl.ascend_auto_sync_vs
        │
        └─ ctx->GetConfig<Bool>("tl.ascend_auto_sync", Bool(false))
              └─ 未开 → Substitute 直接 return f，整个 pass 是 no-op
```

要点：**两个 pass 都是「自门控（self-gating）」**——在 `Substitute` 入口读自己的配置，未开就原样返回，不产生任何开销。

#### 4.1.3 源码精读

**配置项定义与按 target 的默认值**（[tilelang/transform/pass_config.py:L38-L42](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/pass_config.py#L38-L42)）：

```python
TL_ASCEND_AUTO_SYNC = "tl.ascend_auto_sync"
"""Enable/disable TileLang AscendSyncInsert pass. Default: False"""

TL_ASCEND_AUTO_SYNC_VS = "tl.ascend_auto_sync_vs"
"""Enable/disable TileLang AscendSyncInsertVS pass. Default value setted dynamically according target"""
```

**pto 默认开 VS**（[pass_config.py:L103-L108](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/pass_config.py#L103-L108)）：

```python
_TARGET_PASS_DEFAULTS: dict[str, dict[str, bool]] = {
    "ascendc": {},
    "pto": {
        PassConfigKey.TL_ASCEND_AUTO_SYNC_VS: True,
    },
}
```

这解释了测试里的现象：`test_pto_auto_enabled_by_default` 不显式开 VS 也能在 pto 上看到 `pipe_barrier(PIPE_V)`；而 `test_ascendc_default_vs_off` 在 ascendc 上不显式开则**完全没有自动同步**。

**VS 与 bisheng cce-auto-sync 的联动**（[pass_config.py:L204-L209](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/pass_config.py#L204-L209)）：

```python
# VS sync pairs with cce-auto-sync=off + O3 (#1346).
if pass_configs and pass_configs.get(PassConfigKey.TL_ASCEND_AUTO_SYNC_VS, False):
    resolved.update(cce_auto_sync=False, opt_level=3)
```

即：一旦开启 VS，框架会把传给 bisheng 的 `--cce-auto-sync` 关掉、优化级别提到 `-O3`。这正是「三层同步」里第 2 层接管第 3 层的体现。

**两个 pass 在流水线里的调用位置**（[tilelang/engine/phase.py:L118-L119](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L118-L119)）：

```python
mod = tilelang.transform.AscendSyncInsert(target, platform)(mod)
mod = tilelang.transform.AscendSyncInsertVS(target, platform)(mod)
```

它们排在 `OptimizeForTarget` 的**末尾**，紧跟在 `AscendMemoryPlanning`（缓冲复用/地址分配）之后。这个顺序很关键：地址分配完成后，pass 才能从函数属性里读到 `address_map` / `size_map`，从而做「物理地址区间重叠」的别名检测（4.2.3）。

#### 4.1.4 代码实践

**目标**：亲手开启 `TL_ASCEND_AUTO_SYNC`，确认它对生成代码的影响。

**操作步骤**（源码阅读 + 本地编译型实践）：

1. 打开 [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py)，注意它当前带**两个手写** `T.barrier_all()`（第 42、44 行）。
2. 复制一份为 `elementwise_add_autosync.py`，把装饰器改为显式传 pass_configs，并删掉两处 `T.barrier_all()`：

   ```python
   # 示例代码（基于 elementwise_add.py 改写，非项目原有文件）
   pass_configs = {
       tilelang.PassConfigKey.TL_ASCEND_AUTO_SYNC: True,   # 开主 pass
   }

   @tilelang.jit(out_idx=[-1], pass_configs=pass_configs)
   def vec_add(M, N, block_M, block_N, dtype="float"):
       ...
       with T.Scope("V"):
           T.copy(A[...], a_ub)
           T.copy(B[...], b_ub)
           # 删除 T.barrier_all()
           T.tile.add(c_ub, a_ub, b_ub)
           # 删除 T.barrier_all()
           T.copy(c_ub, C[...])
   ```

3. 分别对**原版**（带 barrier_all）与**改版**（开 auto_sync、无手写同步）调用 `func.get_kernel_source()`，把两份 Ascend C 源码 diff 出来。

**需要观察的现象**：

- 改版源码里，`copy_gm_to_ub`（搬入）与 `AscendC::Add`（向量加）之间，应出现自动插入的同步——可能是 `AscendC::PipeBarrier<PIPE_ALL>`（保守，因 GM 源是切片访问）或更细的 `SetFlag/WaitFlag<MTE2_V>`。
- `Add` 与 `copy_ub_to_gm`（搬出）之间同样应出现同步（`V_MTE3` 方向）。
- 原版只有 2 个手写的 `barrier_all`；改版由 pass 决定数量与粒度。

**预期结果**：两版都应跑出 `Kernel Output Match!`（数值正确）。精确插入的 flag 种类与数量**待本地验证**（取决于切片检测与别名检测的具体命中）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 pto 后端默认开 VS、而 ascendc 默认全关？

**参考答案**：pto（更新路线、支持 A5 仿真）需要 tile-lang 在源码层就给出精确同步，以便仿真器看到完整指令流；同时 VS 配套的 `cce_auto_sync=off + O3` 能拿到更优的流水重叠。ascendc（稳妥主线）历史上依赖 bisheng 自带的 cce-auto-sync（默认开），所以 tile-lang 侧默认不额外插，避免与编译器重复。这由 `_TARGET_PASS_DEFAULTS`（[pass_config.py:L103-L108](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/pass_config.py#L103-L108)）体现。

**练习 2**：如果同时开 `TL_ASCEND_AUTO_SYNC` 和 `TL_ASCEND_AUTO_SYNC_VS`，会不会插两遍同步？

**参考答案**：不会重复。两个 pass 按 sibling→VS 顺序执行，VS 在 `VisitStmt_(EvaluateNode)` 里会**识别**已存在的 `tl.ascend_auto_barrier` 并把它登记进历史，不会对同一依赖再插一遍（见 [ascend_sync_insert_vs.cc:L124-L168](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert_vs.cc#L124-L168)，对应测试 `test_preexisting_barrier_recognition`）。

---

### 4.2 AscendSyncInsert 主 pass：全流水线数据依赖同步

#### 4.2.1 概念说明

`AscendSyncInsert`（开关 `tl.ascend_auto_sync`）是覆盖面最广的同步 pass：它跟踪全部 7 条流水线（MTE2/MTE1/M/MTE3/FIX/V/S），按**真实数据依赖**（RAW/WAR/WAW + 物理地址重叠别名）决定要不要同步、插哪种同步。它的两个标志性设计是：

- **算子→流水线映射表**：每个底层算子（`copy_gm_to_ub`、`mma`、`AscendC::Add`…）都预先登记了「读写哪些参数、属于哪条流水线」。pass 不分析指令语义，只查表。
- **循环展开/重建 + `SyncGraph` 传递闭包**：为了处理「循环跨迭代的反向边依赖」，它把每个循环临时展开成两次迭代、跑同步分析、再把同步合并回单层循环；并用一张可达性图消去「已被传递满足」的冗余同步。

它产生的同步只有两类 TIR intrinsic：`tl.ascend_auto_barrier`（屏障，对应同流水线或保守全屏障）与 `tl.ascend_auto_set_flag`+`tl.ascend_auto_wait_flag`（跨流水线事件对）。

#### 4.2.2 核心流程

主 pass 的处理骨架：

```text
Substitute(f) 入口
  ├─ 读 tl.ascend_auto_sync；未开 → return f（no-op）
  ├─ 从 f 属性取 address_map / size_map；加载 operation_config / event_mapping
  ├─ PreprocessUnrollForLoops(body)
  │     └─ ForLoopUnroller：每个循环展开为 iter1+iter2，打 iteration_start/end 标记
  ├─ VisitStmt(展开后的 IR)            ← 核心改写
  │     └─ 逐条语句（EvaluateNode）:
  │          1. AnalyzeStmtAccesses   → 查表得 [buffer, read/write, pipeline]
  │          2. FindRelatedBuffers    → 物理地址重叠的别名 buffer
  │          3. HasDataDependency     → RAW/WAR/WAW 判定
  │          4. GetRequiredSyncType   → 同流水线=PipeBarrier；跨流水线=EventPair
  │          5. OptimizeSyncRequirements → SyncGraph 传递闭包，删冗余
  │          6. InsertSynchronization → 生成 ascend_auto_barrier / set/wait_flag
  │          7. UpdateSyncStatesAfterSync → 把新同步并入各 buffer 的 sync_graph
  └─ MergeAndRebuildForLoops → 把 iter1/iter2 同步取并集、重建单层循环
```

同步类型决策非常简洁：

| 依赖的两侧流水线 | 选择的同步 | 生成的 IR |
|------------------|-----------|-----------|
| 同一流水线（如 V→V） | `PipeBarrier_<pipe>` | `ascend_auto_barrier("PIPE_V")` |
| 不同流水线（如 MTE2→V） | `EventPair_<src>_<dst>` | `ascend_auto_set_flag("MTE2_V", id)` + `ascend_auto_wait_flag("MTE2_V", id)` |
| 切片访问（`is_sliced`） / `IfThenElse` 前后 | `PipeBarrier_ALL`（保守） | `ascend_auto_barrier("PIPE_ALL")` |

#### 4.2.3 源码精读

**配置注册与自门控入口**（[ascend_sync_insert.cc:L46-L71](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert.cc#L46-L71)）：

```cpp
static constexpr const char *kAscendAutoSync = "tl.ascend_auto_sync";
TVM_REGISTER_PASS_CONFIG_OPTION(kAscendAutoSync, Bool);
...
bool ascend_auto_sync =
    ctx->GetConfig<Bool>(kAscendAutoSync, Bool(false)).value();
if (!ascend_auto_sync) {
  return f;   // 未开 → 整个 pass no-op
}
```

**算子→流水线映射表**（核心数据基础，[operation_config.h:L43-L48](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/common/operation_config.h#L43-L48)）——每个算子登记「参数下标→读/写」与「默认流水线」：

```cpp
{"copy_gm_to_ub", {{{0, "read"}, {1, "write"}}, "PIPE_MTE2"}},
{"copy_ub_to_gm", {{{0, "read"}, {1, "write"}}, "PIPE_MTE3"}},
{"mma",  {{{0, "read"}, {1, "read"}, {2, "write"}}, "PIPE_M"}},
{"AscendC::Add", {{{0, "write"}, {1, "read"}, {2, "read"}}, "PIPE_V"}},
{"tl.ascend_scalar", {{{0, "write"}, {1, "read"}}, "PIPE_S"}},
```

这样 `AnalyzeStmtAccesses` 拿到一条 `copy_gm_to_ub(A, a_ub)` 调用，查表立刻知道「MTE2 写了 `a_ub`」，无需理解搬运语义。HardEvent 命名则在 [operation_config.h:L306-L339](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/common/operation_config.h#L306-L339) 的 `GetEventMapping()`，如 `PIPE_MTE2_PIPE_V → "MTE2_V"`。

**核心改写：逐语句分析→检测→插入**（[ascend_sync_insert.cc:L150-L196](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert.cc#L150-L196)，`VisitStmt_(EvaluateNode)`）的关键几步：

```cpp
auto current_accesses = AnalyzeStmtAccesses(GetRef<Stmt>(op));   // 1. 查表
...
for (const auto &buffer_name : related_buffers) {                 // 2. 含别名
  if (HasDataDependency(latest_access, current_access)) {        // 3. RAW/WAR/WAW
    std::string required_sync_type =
        GetRequiredSyncType(latest_access, current_access);      // 4. 选同步类型
    ...
  }
}
auto optimized_syncs = OptimizeSyncRequirements(sync_requirements); // 5. 去冗余
for (const auto &sync_type : optimized_syncs)
  InsertSynchronization(sync_type, stmts);                        // 6. 生成 IR
UpdateSyncStatesAfterSync(optimized_syncs);                       // 7. 更新图
```

**冒险判定 + 物理地址重叠别名**（[ascend_sync_insert.cc:L1369-L1395](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert.cc#L1369-L1395)）。两个 buffer 即使**名字不同**，只要物理地址区间重叠（经缓冲复用后很常见），就视为同一块内存。重叠判定是经典的区间相交：

\[
\text{overlap} \;=\; (\text{prev\_addr} < \text{curr\_end}) \;\land\; (\text{curr\_addr} < \text{prev\_end})
\]

满足重叠且存在写（WAW/RAW/WAR）即判为有依赖。

**同步类型选择**（[ascend_sync_insert.cc:L1404-L1418](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert.cc#L1404-L1418)）：同流水线且未被同管线屏障覆盖→`PipeBarrier_<pipe>`；否则查 `GetEventType` 得跨流水线 `EventPair`。

**`SyncGraph` 传递闭包去冗余**（[ascend_sync_insert.cc:L950-L1013](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert.cc#L950-L1013) 与 [L1510-L1554](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert.cc#L1510-L1554)）。图里每个 EventPair 是一条边（如 `MTE2→V`）。优化时对每条候选同步，先把它并入图、算传递闭包（Floyd-Warshall），若 `HasPath(src,dst)` 已为真就说明已被传递满足、不必再插。例：已插 `M→V` 与 `V→MTE3`，则候选 `M→MTE3` 因传递可达被删。

**生成同步 IR**（[ascend_sync_insert.cc:L1605-L1647](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert.cc#L1605-L1647)）：屏障发 `ascend_auto_barrier`，事件对发 `set/wait_flag`，event_id 用 `(counter+1)%8` 轮转分配（[L1624-L1627](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert.cc#L1624-L1627)）。

**循环展开/重建**（[ascend_sync_insert.cc:L128-L133](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert.cc#L128-L133) 与 [L463-L712](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert.cc#L463-L712)）。`ForLoopUnroller` 把每个循环复制成 iter1、iter2 两份（用 `iteration_start/end` AttrStmt 标记边界），让「上一轮末尾写 → 本轮开头读」的反向边依赖**变成 iter1→iter2 的普通顺序依赖**被检测到；之后 `LoopRebuilder::MergeStatementSequences` 按「执行语句」位置对齐 iter1/iter2，把各自需要的同步取**并集**、去重，重建为单层循环体。

**codegen 把 intrinsic 落到 AscendC 调用**（[codegen_ascend.cc:L2616-L2635](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L2616-L2635)）：

```cpp
// ascend_auto_barrier("PIPE_V")  →  AscendC::PipeBarrier<PIPE_V>();
// ascend_auto_set_flag("MTE2_V", 3) → AscendC::SetFlag<AscendC::HardEvent::MTE2_V>(3);
// ascend_auto_wait_flag("MTE2_V", 3)→ AscendC::WaitFlag<AscendC::HardEvent::MTE2_V>(3);
```

这正好接上 u4-l2 讲过的手写 flag——自动 pass 生成的是**同一族** AscendC 原语，只是由编译器代劳。

#### 4.2.4 代码实践

**目标**：跟踪一次「跨流水线 RAW」从依赖到生成代码的全过程。

**操作步骤**（源码阅读型实践）：

1. 阅读技术文档给出的最小例子（`.agents/skills/tilelang-pass-analyzer/references/pass-designs/ascend_sync_insert_technical_doc.md` 第 6.2 节）：`copy_gm_to_l1(A, a_l1)`（MTE2 写）紧跟 `mma(c_l0c, a_l1, b_l1)`（M 读同一 `a_l1`）。
2. 在 [ascend_sync_insert.cc:L150-L196](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert.cc#L150-L196) 里逐行对应：`AnalyzeStmtAccesses` 得到 mma 读 `a_l1`(PIPE_M)；`FindRelatedBuffers` 命中历史里的 `a_l1`(PIPE_MTE2 写)；`HasDataDependency` 为 RAW=True；`GetRequiredSyncType` 因 MTE2≠M 返回 `EventPair_MTE2_M`。
3. 写一个等价的最小 prim_func（A、B 在 L1，C 在 L0C，`T.copy` GM→L1 后接 `T.gemm_v0`），开 `TL_ASCEND_AUTO_SYNC`，用 `get_kernel_source()` 在 ascendc 产物里搜索 `SetFlag<AscendC::HardEvent::MTE2_M>`。

**需要观察的现象**：生成代码里 `copy_gm_to_l1` 之后、`mma` 之前出现成对的 `SetFlag/WaitFlag<...MTE2_M>(id)`。

**预期结果**：应能匹配到 `MTE2_M` 事件对（**待本地验证**，因为 `T.gemm_v0` 内部模板已含搬运/同步，最终可见的同步点取决于模板展开后的语句边界）。

#### 4.2.5 小练习与答案

**练习 1**：`PipeBarrier<PIPE_V>` 与 `SetFlag/WaitFlag<V_V>` 都是约束 V 流水线内部顺序，为什么主 pass 对同流水线依赖倾向用 `PipeBarrier`？

**参考答案**：`PipeBarrier<pipeline>` 是「一刀切」屏障，实现简单、语义清晰，对单条流水线内的 WAW/RAW 足够；`SetFlag/WaitFlag` 需要分配并管理 event_id（模 8 轮转），适合需要多缓冲复用、精确配对的软件流水场景。主 pass 默认对同流水线用屏障（[ascend_sync_insert.cc:L1404-L1418](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert.cc#L1404-L1418)），手写高性能 kernel 时再换更细的 flag（见 u4-l2）。

**练习 2**：`SyncGraph` 传递闭包能消去哪类冗余？给一个具体例子。

**参考答案**：消去「已被一连串已插同步传递满足」的同步。例：已插 `M→V` 与 `V→MTE3`，图里存在路径 `M→V→MTE3`，故候选 `M→MTE3` 的 `HasPath(M, MTE3)=true`，被 `OptimizeSyncRequirements` 跳过（见技术文档 5.4 节示例）。

**练习 3**：为什么 pass 要在循环上做「展开成两次迭代再合并」？

**参考答案**：循环跨迭代的反向边依赖（上一轮末尾写 buffer X → 下一轮开头读 X）在单层循环体里表现为「同一条语句读 X，但 X 在上一轮被写」。直接分析难以区分「迭代内依赖」与「迭代间依赖」。展开成 iter1+iter2 后，反向边变成 iter1 末→iter2 头的**顺序依赖**，可直接检测；合并时把两轮各自的同步取并集重建为单层体，既不漏掉跨迭代同步，又不致每轮重复插（见 [ascend_sync_insert.cc:L463-L712](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert.cc#L463-L712)）。

---

### 4.3 AscendSyncInsertVS 补充 pass：聚焦 V/S 的轻量同步

#### 4.3.1 概念说明

`AscendSyncInsertVS`（开关 `tl.ascend_auto_sync_vs`，简称 **VS**）是更晚加入、更聚焦的补充 pass。它的设计哲学是：**标量流水线 PIPE_S 与向量流水线 PIPE_V 是最容易出问题、也最值得精确同步的两条**，而 MTE2/MTE3 等搬运流水线之间在很多场景下硬件已有隐式保序或由 bisheng 兜底。因此 VS 只跟踪 `{V, S, MTE2, MTE3}` 四条流水线，且**仅当两侧至少有一方是 V 或 S 才插同步**（`ShouldSync` 规则）。

VS 相对主 pass 的几个显著不同：

- **更窄的冒险模型**：`ShouldSync` 明确规定 V→V 同步、S↔其他（非 S）同步；**不**对 MTE2→V、V→MTE3、MTE2→MTE3 等插同步（这些留给主 pass 或 bisheng）。这意味着单开 VS 时，纯搬运→向量、向量→搬运的依赖**不会**被它覆盖。
- **两遍扫描处理循环反向边**：用 `is_revisit_pass_` 标志做「首遍 + 重扫」两遍，首遍建循环内依赖，重扫时把上一轮末尾的访问作为 `is_back_edge` 注入历史，从而精确捕获跨迭代依赖。
- **`write_history` 双历史**：除了「最近一次访问」历史，额外维护「最近一次写」历史，用来恢复「被一次读覆盖后丢失的 RAW/WAW」。
- **A5 特例**：A5 平台不支持 `PipeBarrier<PIPE_V>`，VS 遇到 V→V 时改发 `SetFlag/WaitFlag<V_V>` 事件对。

#### 4.3.2 核心流程

VS 的处理骨架（与主 pass 同构但更轻）：

```text
Substitute(f) 入口
  ├─ 读 tl.ascend_auto_sync_vs；未开 → return f
  ├─ 加载 address_map/size_map、operation_config、event_mapping
  └─ mutator(f->body)
       └─ 逐语句:
            ├─ 若是 ascend_auto_barrier/set/wait_flag → 登记进历史、原样返回（消费 sibling 输出）
            ├─ AnalyzeStmtAccesses + ScanBufferLoads（含标量读）
            ├─ 过滤 supported（仅 V/S/MTE2/MTE3）
            └─ ProcessStatement:
                 for each buffer: CheckBufferDependency
                   ├─ 查 current_access_history（最近访问）
                   └─ 查 current_write_history（最近写，恢复 RAW/WAW）
                 HasDataDependency → GetRequiredSyncType(ShouldSync) → Dedup → Insert
       └─ For 循环: 首遍 + 重扫（is_revisit_pass_）处理反向边
```

`ShouldSync` 决策表（[ascend_sync_insert_vs.cc:L881-L894](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert_vs.cc#L881-L894)）：

| 两侧流水线 | 是否同步 | 典型场景 |
|-----------|---------|---------|
| V → V | ✅ 同步（屏障/事件） | exp(x) 后再 mul(x) |
| S ↔ 其他（非 S） | ✅ 同步 | 标量写 UB 后向量读 |
| MTE2 → V | ❌ 不同步 | GM→UB 后向量算（留给主 pass） |
| V → MTE3 | ❌ 不同步 | 向量算后 UB→GM（留给主 pass） |
| S → S | ❌ 不同步 | 标量流水线自身保序 |
| MTE2/MTE3 互相 | ❌ 不同步 | 搬运间不归 VS 管 |

> 这张表与测试里的「核心负样本」一一对应：`test_no_mte2_to_v_sync`、`test_no_v_to_mte3_sync`、`test_no_s_to_s_sync`、`test_mte2_to_mte2_no_sync` 等。

#### 4.3.3 源码精读

**配置与自门控**（[ascend_sync_insert_vs.cc:L41-L52](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert_vs.cc#L41-L52)）：

```cpp
static constexpr const char *kAscendAutoSyncVs = "tl.ascend_auto_sync_vs";
TVM_REGISTER_PASS_CONFIG_OPTION(kAscendAutoSyncVs, Bool);
...
bool enabled = ctx->GetConfig<Bool>(kAscendAutoSyncVs, Bool(false)).value();
if (!enabled) { return f; }
```

**`ShouldSync` 核心**（[ascend_sync_insert_vs.cc:L876-L894](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert_vs.cc#L876-L894)）：

```cpp
bool IsSupportedPipeline(const std::string &p) {
  return p == "PIPE_V" || p == "PIPE_S" || p == "PIPE_MTE2" || p == "PIPE_MTE3";
}
bool ShouldSync(const std::string &a, const std::string &b) {
  if (!IsSupportedPipeline(a) || !IsSupportedPipeline(b)) return false;
  if (a == "PIPE_V" && b == "PIPE_V") return true;            // V->V
  if ((a == "PIPE_S" || b == "PIPE_S") && a != b) return true; // S <-> 其他
  return false;
}
```

注意 `PIPE_M`（GEMM）**不在**支持集合，所以 VS 永远不会对 GEMM 插 `M_V/V_M`（测试 `test_pipe_m_ignored`）。

**双历史依赖检查**（[ascend_sync_insert_vs.cc:L323-L357](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert_vs.cc#L323-L357)）：先查 `current_access_history_`（最近访问），若最近访问不是写，再查 `current_write_history_`（最近写），以恢复被中间读覆盖掉的 RAW/WAW（测试 `test_mte3_to_s_scalar_read` 即靠此把 S 读直接同步到 MTE2 写，而非错误的 MTE3 读）。

**同步类型与 A5 特例**（[ascend_sync_insert_vs.cc:L756-L778](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert_vs.cc#L756-L778)）：

```cpp
if (pipeline == "PIPE_V" && platform_ == "A5") {
  // A5 不支持 pipe_barrier(PIPE_V)，改用 V_V 事件对
  int event_id = AllocateEventId();
  stmts.push_back(CreateSetFlag("V_V", event_id));
  stmts.push_back(CreateWaitFlag("V_V", event_id));
} else {
  stmts.push_back(CreatePipeBarrier(pipeline));
}
```

**循环两遍扫描**（[ascend_sync_insert_vs.cc:L278-L313](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert_vs.cc#L278-L313)）：首遍访问循环体，记下末尾历史；把末尾访问以 `is_back_edge=true` 注入历史；置 `is_revisit_pass_=true` 重扫一遍，捕获跨迭代依赖（测试 `test_loop_back_edge_v_to_v`、`test_loop_back_edge_s_to_v`）。重扫时 `GetRequiredSyncType` 里的 `if (is_revisit_pass_ && !prev_access.is_back_edge) return "";` 守卫确保**只**处理反向边、不重复插同迭代同步（[L579-L581](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert_vs.cc#L579-L581)），对应测试 `test_nested_loop_v_to_s_dedup`。

**注册**（[ascend_sync_insert_vs.cc:L910-L918](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert_vs.cc#L910-L918)）：

```cpp
tvm::transform::Pass AscendSyncInsertVS(Target target, std::string platform) { ... }
TVM_REGISTER_GLOBAL("tl.transform.AscendSyncInsertVS").set_body_typed(AscendSyncInsertVS);
```

#### 4.3.4 代码实践

**目标**：用 VS 的测试套件验证「该插/不该插」的边界，建立对 `ShouldSync` 的直觉。

**操作步骤**（阅读 + 本地运行型实践）：

1. 打开 [testing/python/language/test_ascend_sync_insert_vs.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_ascend_sync_insert_vs.py)，重点读两组用例：
   - **正样本**：`test_v_to_v_pipe_barrier`（V→V 插 `PipeBarrier<PIPE_V>`）、`test_s_to_v_event_pair`（S→V 插 `S_V`）。
   - **负样本**：`test_no_mte2_to_v_sync`（MTE2→V **不**插）、`test_no_v_to_mte3_sync`（V→MTE3 **不**插）。
2. 注意每个用例都做了**差分测试**：同一 kernel 分别用 `PASS_VS_ONLY`（VS 开）与 `PASS_NO_SYNC`（全关）编译，断言开 VS 多出来的同步。
3. （本地有 CANN 时）运行 `pytest testing/python/language/test_ascend_sync_insert_vs.py -v`，观察全部用例通过。

**需要观察的现象**：VS 开启时，正样本生成代码里多出 `AscendC::PipeBarrier<PIPE_V>` 或 `SetFlag<...S_V>`；负样本则 `_assert_no_auto_sync` 通过（无任何自动同步）。

**预期结果**：所有用例通过即证明 `ShouldSync` 边界正确。无 CANN/NPU 时为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：单开 VS（不开主 pass）处理「GM→UB 搬运后接向量加」，会出现什么问题？

**参考答案**：会**漏同步**。`copy_gm_to_ub` 是 MTE2 写、`add` 是 V 读，属 MTE2→V 依赖；而 `ShouldSync(MTE2, V)=false`，VS 不会插同步。这正是 VS 的设计取舍——它只管 V/S，MTE2→V 这类搬运→向量依赖要靠主 pass（`TL_ASCEND_AUTO_SYNC`，主 pass 的 `GetEventMapping` 含 `MTE2_V`）或 bisheng 的 cce-auto-sync 兜底。因此 4.1.4 的实践明确要求开**主 pass** 而非仅 VS。

**练习 2**：VS 为什么需要 `current_write_history_` 这第二份历史？

**参考答案**：`current_access_history_` 只记「最近一次访问」（读或写）。若序列是「MTE2 写 X → MTE3 读 X → S 读 X」，第二部的 MTE3 读会把 X 的历史从「写」覆盖成「读」，导致第三步的 S 读查不到之前的 MTE2 写，丢失 RAW 依赖。`current_write_history_` 单独保留「最近一次写」，`CheckBufferDependency` 在最近访问非写时回退查它（[L343-L356](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert_vs.cc#L343-L356)），从而把 S 读正确同步到 MTE2 写（测试 `test_mte3_to_s_scalar_read`）。

**练习 3**：A5 平台上 V→V 依赖为什么不能直接用 `PipeBarrier<PIPE_V>`？

**参考答案**：A5 的 `pipe_barrier` 第一个参数取值范围是 `[4,6]`，不接受 `PIPE_V`（直接发会报错）。VS 检测到 `platform_=="A5"` 时改发 `SetFlag/WaitFlag<V_V>` 事件对来约束 V 流水线内部顺序（[ascend_sync_insert_vs.cc:L762-L771](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_sync_insert_vs.cc#L762-L771)，测试 `test_a5_pto_pipe_v_v_barrier`、`test_a5_ascendc_pipe_v_v_barrier`）。注意 A5 只跳过 V 屏障，S_V 等事件对仍正常发（`test_a5_still_emits_event_pairs`）。

## 5. 综合实践

把本讲三个模块串起来，完成一个「从手写到全自动」的对照实验。

**任务**：以 [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py) 为基准，构造三个版本并对比生成代码：

| 版本 | 配置 | kernel 内同步 |
|------|------|--------------|
| A 基线 | 默认（ascendc：全关） | 保留 2 个手写 `T.barrier_all()` |
| B 主 pass | `TL_ASCEND_AUTO_SYNC=True` | **删除**全部手写同步 |
| C 仅 VS | `TL_ASCEND_AUTO_SYNC_VS=True` | **删除**全部手写同步 |

**步骤**：

1. 对三个版本分别 `func.get_kernel_source()`，保存为 `a.cpp / b.cpp / c.cpp`。
2. 用文本 diff 比较：
   - B 相对 A：手写的 `PipeBarrier<PIPE_ALL>` 消失，取而代之的是 pass 自动插入的同步（`PIPE_ALL` 屏障或 `MTE2_V/V_MTE3` 事件对）。
   - C 相对 B：C 版**大概率不正确或漏同步**——因为 elementwise_add 的核心依赖正是 MTE2→V 与 V→MTE3，而 VS 的 `ShouldSync` 明确不处理这两类（见 4.3.5 练习 1）。这正是「VS 是补充、不是替代」的实证。
3. 三个版本都跑 `torch.testing.assert_close`，记录哪些版本输出 `Kernel Output Match!`、哪些数值出错。
4. （进阶）把 kernel 换成「含标量写 UB + 向量读 UB」的形态（参考 `test_s_to_v_event_pair`），重做 B/C 对比，此时 C（VS）应能正确插 `S_V` 事件对。

**预期结论**：

- 主 pass（B）足以独立保证 elementwise_add 的正确性，是最省心的「全自动」选择。
- VS（C）只擅长 V/V、S↔其他的场景，必须与主 pass 或 bisheng auto-sync 配合，单用在搬运密集 kernel 上会漏同步。
- 自动 pass 生成的同步粒度通常比手写 `barrier_all` 更细（事件对 vs 全屏障），潜在性能更好，但精确数量**待本地验证**。

> 提示：若本地无 NPU/CANN，可退化为「源码阅读型」——直接读 `get_kernel_source` 无法跑时，改用 `func.ir_module['main']` 查看 pass 改写后的 TIR（技术文档 10.3 节给了 dump IR 的脚本片段），同样能看到 `tl.ascend_auto_*` intrinsic 的插入位置。

## 6. 本讲小结

- tile-lang 昇腾同步有**三层**：手写 flag、tile-lang 自动 pass（`AscendSyncInsert` / `AscendSyncInsertVS`）、bisheng 自带 cce-auto-sync。开启 VS 会联动关闭 cce-auto-sync 并升到 `-O3`。
- 两个自动 pass 都**自门控**：在 `Substitute` 入口读自己的配置，未开即 no-op。它们排在 `OptimizeForTarget` 末尾、内存规划之后，紧挨着执行（先 sibling 后 VS）。
- 默认值按 target 分配：**pto 默认开 VS**、ascendc 默认全关（`_TARGET_PASS_DEFAULTS`）。用户显式设置永远优先。
- **主 pass** 跟踪全部 7 条流水线，靠「算子→流水线映射表 + 数据依赖 + 物理地址重叠别名」检测冒险，用 `SyncGraph` 传递闭包消冗余，用「循环展开两轮再合并」处理反向边；同流水线插 `PipeBarrier`、跨流水线插 `EventPair`。
- **VS pass** 只跟踪 V/S/MTE2/MTE3 且仅当 ≥1 方为 V 或 S 才同步（`ShouldSync`），用双历史（access + write）恢复丢失的 RAW/WAW，用两遍扫描处理循环反向边，并对 A5 平台的 `PIPE_V` 屏障做特例改发 `V_V` 事件对。
- 自动 pass 产出的 `tl.ascend_auto_*` intrinsic 经 codegen 落成与手写完全同族的 `AscendC::PipeBarrier / SetFlag / WaitFlag`——自动与手写最终汇入同一套同步原语。

## 7. 下一步学习建议

- **u5-l1（Cube/Vector 分离与 CombineCV）**：自动同步是「核内」的；CV 分离后，Cube 与 Vector 核之间经 GM/L2 交换数据的「核间」同步由 `TL_ASCEND_AUTO_CV_SYNC` 与 `set/wait_cross_flag` 负责（codegen 里的 `ascend_auto_set/wait_cross_flag`），与本讲核内 pass 正交。
- **u6-l1（编译 Pass 全景）**：把本讲两个 pass 放回两阶段 Pass 全景里，理解它们为何必须排在 `AscendMemoryPlanning` / `AscendStorageRewrite` 之后（依赖地址分配结果）。
- **u7-l2（高性能 GEMM 优化）**：自动同步是「保正确」的兜底；追求极致性能时，仍需像 u4-l2 那样手写精细 flag 流水（`init_flag` → `k%S` 复用 → `clear_flag`），并用自动 pass 做对照基线。
- **继续阅读**：`.agents/skills/tilelang-pass-analyzer/references/pass-designs/ascend_sync_insert_technical_doc.md`（主 pass 的完整技术说明）与 `testing/python/language/test_ascend_sync_insert_vs.py`（VS 的权威行为规约）。
