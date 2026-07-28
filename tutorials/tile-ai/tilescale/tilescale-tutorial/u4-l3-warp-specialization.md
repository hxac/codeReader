# Warp 特化与 Hopper wgmma

## 1. 本讲目标

本讲承接 u3-l4（`OptimizeForTarget`）和 u4-l2（软件流水线），深入 Hopper（SM90+）架构上一类最重要的优化——**warp 特化（warp specialization）**。

学完后你应该能够：

1. 理解 warp 特化的「生产者—消费者」模型：让一个 warp group 专门做数据搬运（TMA/cp.async），另一个专门做计算（wgmma），二者靠 mbarrier 同步重叠执行。
2. 掌握前端 `T.ws` 原语的语义，知道它在 TIR 里变成什么（一个带 `warp_specialize` 属性的 `if`）。
3. 看懂支撑手动 warp 特化的三道 pass：`IfStmtBinding`、`MultiVersionBuffer`、`WarpSpecialized`。
4. 理解 wgmma 的异步性与 `RewriteWgmmaSync` 如何把「立即等待」推迟到「最晚安全点」，以及 `InjectFenceProxy` 为何必须在 generic 与 async 代理切换处插入栅栏。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

### 2.1 Warp group（线程组）

NVIDIA Hopper 把一个 threadblock 里的线程按 128 个为一组划分成 **warp group**（每个 warp group = 4 个 warp = 128 个线程）。一个 256 线程的 block 恰好有 2 个 warp group。warp 特化的核心思想就是**让不同的 warp group 干不同的活**：group 0 做计算、group 1 做搬运。

### 2.2 生产者—消费者（producer–consumer）

把数据从 global memory 搬到 shared memory 的是「生产者」，从 shared memory 读取做矩阵乘的是「消费者」。普通写法里这两件事由同一批线程串行完成；warp 特化把它们拆给两组线程，靠共享内存里的 **mbarrier**（内存屏障）通知对方「数据好了 / 用完了」，从而让搬运和计算**重叠**起来。

### 2.3 wgmma 与异步代理

Hopper 的矩阵乘指令 `wgmma`（warp group MMA）是**异步**的：发出指令后不立刻拿到结果，结果落在累加器寄存器里，要等显式 `warpgroup_wait` 才保证可见。同时，`wgmma`、TMA、`cp.async` 都运行在硬件的「异步代理（async proxy）」上，而 `ldmatrix`、普通 shared store 等运行在「通用代理（generic proxy）」上。两类代理之间切换时，硬件需要一条 `fence.proxy.async` 指令来保证顺序——这正是 `InjectFenceProxy` 要解决的问题。

### 2.4 与软件流水（u4-l2）的关系

u4-l2 讲的 `T.Pipelined(num_stages=N)` 通过多缓冲隐藏访存延迟。warp 特化是另一种隐藏延迟的手段：它不靠「同一组线程交替 prefetch/compute」，而是靠「两组线程各司其职」。两者经常**配合使用**——在示例里你会看到 `T.ws` 出现在 `T.Pipelined` 的循环体内部。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/warpgroup.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/warpgroup.py) | 前端 `T.ws` 原语，把 warp group 索引翻译成线程条件 |
| [src/ir.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/ir.cc) | `WarpSpecialize` 的 C++ 实现，生成带 `warp_specialize` 属性的 `if` 帧 |
| [src/transform/if_stmt_binding.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/if_stmt_binding.cc) | `IfStmtBinding`：把 warp group 的 `if` 条件下放到每条子语句 |
| [src/transform/multi_version_buffer_rewriter.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/multi_version_buffer_rewriter.cc) | `MultiVersionBuffer`：为跨生产/消费的 shared buffer 加多版本维 |
| [src/transform/warp_specialized_rewriter.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/warp_specialized_rewriter.cc) | `WarpSpecialized`：自动 warp 特化的角色标记 + mbarrier 同步插入 |
| [src/transform/warp_specialized_rewriter.h](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/warp_specialized_rewriter.h) | `WarpSpecializedDetector`：判断走手动还是自动路径 |
| [src/transform/wgmma_sync_rewriter.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/wgmma_sync_rewriter.cc) | `RewriteWgmmaSync`：把 wgmma 的等待推迟到最晚安全点 |
| [src/transform/inject_fence_proxy.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc) | `InjectFenceProxy`：在 generic→async 代理切换处插入 `fence.proxy.async` |
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py) | `OptimizeForTarget` 中 Hopper 分支的 pass 编排 |
| [examples/warp_specialize/example_warp_specialize_gemm_copy_1_gemm_0.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/warp_specialize/example_warp_specialize_gemm_copy_1_gemm_0.py) | 手动 warp 特化的端到端示例 |

## 4. 核心概念与源码讲解

### 4.1 `T.ws` 与 warp 特化的生产-消费模型

#### 4.1.1 概念说明

`T.ws`（`ws` 是 `WarpSpecialize` 的缩写）是 TileLang 暴露给用户的 warp 特化入口。它的用法是一个上下文管理器：

```python
with T.ws(0):   # 仅 warp group 0（线程 0~127）执行
    T.gemm(A_shared, B_shared, C_local)
with T.ws(1):   # 仅 warp group 1（线程 128~255）执行
    T.copy(A[...], A_shared)
```

参数是 warp group 的**编号**（从 0 开始）。`T.ws(0)` 等价于「`threadIdx.x < 128`」，`T.ws(1)` 等价于「`128 <= threadIdx.x < 256`」，`T.ws(0, 1)` 则合并为「`threadIdx.x < 256`」（两个组都执行）。

> 小提示：`warp group_size` 在代码里被硬编码为 128（见下方源码），这是 NVIDIA GPU 一个 warp group 的固定线程数。

#### 4.1.2 核心流程

`T.ws` 在前端做的事很简单：把 warp group 编号换算成 `threadIdx.x` 的区间条件，构造一个 `if`。它的目标是**让用户用「声明哪个组干这段活」的高层写法**，代替手写 `if threadIdx.x < 128` 这种底层判断。

```text
T.ws(group_ids)
  ├─ 取当前线程绑定 threadIdx（1D/2D/3D 合并成线性 tid）
  ├─ 对每个 group_id 计算 [id*128, (id+1)*128) 区间
  ├─ 合并连续 group（如 0,1 → [0,256)）
  └─ 产出 IR：if (tid 在区间内) { attr warp_specialize=1 { <body> } }
```

#### 4.1.3 源码精读

前端入口 [tilelang/language/warpgroup.py:19-55](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/warpgroup.py#L19-L55) 负责「把多维线程索引压成一个线性 tid，再交给 C++」。关键点：它把 `threadIdx.x/y/z` 按 extents 合成线性 `tid`，并把 `warp_group_size` 固定为 128：

```python
id_x, id_y, id_z = get_thread_bindings()
ex_x, ex_y, ex_z = get_thread_extents()
tid = id_x
if ex_y > 1:
    tid = id_y * ex_x + tid
if ex_z > 1:
    tid = id_z * (ex_y * ex_x) + tid
warp_group_size = 128            # 固定：一个 warp group = 128 线程
return _ffi_api.WarpSpecialize(warp_group_ids, tid, warp_group_size)
```

第 59 行给了它一个更短的别名 [`ws = WarpSpecialize`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/warpgroup.py#L58-L59)，所以 `T.ws` 与 `T.WarpSpecialize` 完全等价。

真正生成 IR 的是 C++ 端 [src/ir.cc:365-404](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/ir.cc#L365-L404)。它先把连续的 group 合并成区间，再为每个区间生成 `(tid >= start*128) && (tid < end*128)` 的条件，最后组装出一个 `If` 帧并挂上 `warp_specialize=1` 属性：

```cpp
for (const auto &[start, end] : merged) {
  PrimExpr range_cond = (thread_idx >= start*128) && (thread_idx < end*128);
  condition = condition.defined() ? Or(condition, range_cond) : range_cond;
}
IfFrame if_frame = If(condition);
AttrFrame attr_frame = Attr(Integer(0), "warp_specialize", Integer(1));
n->frames.push_back(if_frame);
n->frames.push_back(Then());
n->frames.push_back(attr_frame);   // 关键：打上 warp_specialize 属性
```

也就是说，`T.ws(0): <body>` 最终在 TIR 里长这样（伪 IR）：

```text
if (threadIdx.x < 128) {
  attr "warp_specialize" = 1 {
    <body>     // 仅 group 0 执行
  }
}
```

那个 `warp_specialize` 属性是后续 `WarpSpecializedDetector` 识别「用户已手动特化」的依据（见 4.3.2）。

#### 4.1.4 代码实践：读懂示例里的生产-消费分工

打开 [examples/warp_specialize/example_warp_specialize_gemm_copy_1_gemm_0.py:16-39](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/warp_specialize/example_warp_specialize_gemm_copy_1_gemm_0.py#L16-L39)，这是一个手动 warp 特化的 GEMM。核心循环体如下：

```python
data_is_ready   = T.alloc_barrier(arrive_count=128)   # 生产者→消费者的通知
compute_is_done = T.alloc_barrier(arrive_count=128)   # 消费者→生产者的通知
...
for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=2):
    with T.ws(1):                                      # group 1 = 生产者
        T.barrier_wait(compute_is_done, (ko + 1) % 2)  # 等消费者用完上一轮缓冲
        T.copy(A[by*block_M, ko*block_K], A_shared)
        T.copy(B[ko*block_K, bx*block_N], B_shared)
        T.barrier_arrive(data_is_ready)                # 通知消费者：新数据好了
    with T.ws(0):                                      # group 0 = 消费者
        T.barrier_wait(data_is_ready, ko % 2)          # 等生产者搬好数据
        T.gemm(A_shared, B_shared, C_local)            # 计算
        T.barrier_arrive(compute_is_done)              # 通知生产者：我用完了
```

**一句话总结分工**：`T.ws(1)`（线程 128~255）做搬运（`T.copy`），`T.ws(0)`（线程 0~127）做计算（`T.gemm`），两组靠 `data_is_ready` / `compute_is_done` 两个 mbarrier 的 arrive/wait 握手同步。

实践步骤：

1. 实践目标：在源码里定位 `T.ws` 的两次出现，确认「谁搬运、谁计算」。
2. 操作步骤：阅读上述 16-39 行；注意 `barrier_wait` 的第二个参数 `ko % 2` 与 `(ko+1) % 2`（奇偶翻转，配合双缓冲）。
3. 需要观察的现象：生产者每轮先等 `compute_is_done`（消费者用完）、再搬数据、再 `arrive(data_is_ready)`；消费者先等 `data_is_ready`、再 `gemm`、再 `arrive(compute_is_done)`。
4. 预期结果：两组线程的搬运与计算在时间上重叠，整体延迟低于串行版本（待本地在 Hopper GPU 上验证具体数值）。
5. 本地无 Hopper GPU 时：可仅做源码阅读型实践，标注「待本地验证」。

#### 4.1.5 小练习与答案

- **练习 1**：如果把示例里的 `with T.ws(0)` 和 `with T.ws(1)` 整体对调（计算放 group 1、搬运放 group 0），结果会改变吗？
  - **答案**：数学结果不变（两组仍各自只执行属于自己的语句），但线程号区间对应的角色变了；只要 mbarrier 的 arrive_count（=128，一个 group 的线程数）和 wait 的奇偶翻转保持一致，逻辑就仍正确。
- **练习 2**：`T.ws(0, 1)` 会让哪些线程执行其内部语句？
  - **答案**：合并后区间为 `[0, 256)`，即整个 block（前两个 warp group）都执行。

### 4.2 `IfStmtBinding` 与 `MultiVersionBuffer`：搭好手动特化的脚手架

当用户写了 `T.ws`，编译器需要把「一个 `if` 包着一段语句序列」改造成下游 pass 能消化的形态。这由两道 pass 完成：`IfStmtBinding` 负责拆 `if`，`MultiVersionBuffer` 负责给跨边界的 shared buffer 加多版本。

#### 4.2.1 概念说明

考虑 `T.ws(0)` 包了三条语句 `s1; s2; s3`，IR 里是 `if cond { s1; s2; s3 }`。问题在于：下游需要**逐条**判断每条语句属于哪个 warp group（比如要做缓冲多版本、要做同步分析）。`IfStmtBinding` 把这个 `if` **下放**到每条子语句：变成 `if cond {s1}; if cond {s2}; if cond {s3}`。条件被「绑定」到每条语句上。

接着，`MultiVersionBuffer` 解决另一个问题：`A_shared` 被 group 1 写、被 group 0 读，两组线程并发访问同一块 shared memory 会冲突。解决办法是给这种「跨生产/消费边界」的 buffer 加一个**版本维**（大小 = `num_stages`），让不同迭代访问不同槽位——这正是 u4-l2 多缓冲思想在 warp 特化场景的落地。

#### 4.2.2 核心流程

`IfStmtBinding`：

```text
对每个无 else 分支的 IfThenElse：
  取 then_case（若是 SeqStmt，则逐条）
  把每条子语句各自包成 if(condition, stmt)
  重新拼成 SeqStmt
```

`MultiVersionBuffer`：

```text
找到每个带 num_stages 的流水线循环：
  ├─ 收集作用域内的 shared/shared.dyn buffer
  ├─ 用角色标记判定：哪些 buffer「被生产者写 且 被消费者读」（跨边界）
  ├─ 对这些 buffer：形状前面加一维 num_versions=num_stages
  └─ 改写所有访问：下标最前面插入 version_index = floormod(loop_iter, num_stages)
```

#### 4.2.3 源码精读

`IfStmtBinding` 的核心是 [src/transform/if_stmt_binding.cc:41-57](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/if_stmt_binding.cc#L41-L57) 的 `bind_if_stmt` lambda——它把一个语句序列里每条都包上同一个条件：

```cpp
auto bind_if_stmt = [](const Optional<Stmt> &body, const PrimExpr &condition) -> Stmt {
  if (auto seq_stmt = stmt.as<SeqStmtNode>()) {
    Array<Stmt> seq_;
    for (auto s : seq_stmt->seq) {
      seq_.push_back(IfThenElse(condition, s, Stmt()));  // 每条都包 if
    }
    return SeqStmt(std::move(seq_));
  } else {
    return IfThenElse(condition, stmt, Stmt());
  }
};
```

注意它只处理**没有 else 分支**的 `if`（[第 35-37 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/if_stmt_binding.cc#L35-L37)），因为 `T.ws` 生成的 `if` 本来就只有 then 分支。pass 注册见 [src/transform/if_stmt_binding.cc:77-82](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/if_stmt_binding.cc#L77-L82)。

`MultiVersionBuffer` 判定「哪些 buffer 该多版本」的逻辑在 [GetVersionedBuffers](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/multi_version_buffer_rewriter.cc#L142-L253)。它用一份内置的 `WarpSpecializedRoleMarker_`（[第 26-124 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/multi_version_buffer_rewriter.cc#L26-L124)）给每条语句标 producer/consumer 角色，然后挑选同时出现在 `producer_used`（被生产者写）和 `consumer_used`（被消费者读）里的 shared buffer：

```cpp
for (Buffer buffer : scoped_buffers) {
  if (consumer_used.count(buffer.get()) &&
      producer_used.count(buffer.get())) {
    versioned_buffers.push_back(buffer);   // 跨边界 → 需多版本
    continue;
  }
  ... // 兜底：通过 first_write < last_read 再判一次
}
```

确定要加版本的 buffer 后，[RewriteAllocBuffer](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/multi_version_buffer_rewriter.cc#L255-L265) 在 shape 最前面插入 `num_versions` 这一维：

```cpp
new_buffer->shape.insert(new_buffer->shape.begin(), PrimExpr(num_versions));
```

随后所有访问都被改写：[BufferLoad](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/multi_version_buffer_rewriter.cc#L409-L420) 与 [BufferStore](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/multi_version_buffer_rewriter.cc#L422-L433) 都在下标最前面插入 `version_index_`，而该索引由循环变量算出（[第 396-401 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/multi_version_buffer_rewriter.cc#L396-L401)）：

```cpp
version_index_ = FloorMod(linear_index, num_stages);
...
n->indices.insert(n->indices.begin(), version_index_);   // 读/写都加版本下标
```

这样，迭代 `ko` 写第 `ko % num_stages` 槽、读的也是对应槽，生产者与消费者自然错开，避免数据竞争。

> 说明：`MultiVersionBuffer` 只处理 `shared` / `shared.dyn`（[第 364-366 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/multi_version_buffer_rewriter.cc#L364-L366)）；寄存器 fragment 是线程私有的，不需要多版本。

#### 4.2.4 代码实践：观察多版本维的引入

1. 实践目标：亲眼看到 `MultiVersionBuffer` 给 `A_shared` 加了一维。
2. 操作步骤：写一个最小化的手动 warp 特化 GEMM（可基于示例裁剪），在 `OptimizeForTarget` 里单独调用 `tilelang.transform.MultiVersionBuffer()` 前后分别打印 IR（`mod.show()`）。
3. 需要观察的现象：`A_shared` 的声明由 `(block_M, block_K)` 变为 `(num_stages, block_M, block_K)`；其读写处多出一个 `floormod(ko, num_stages)` 下标。
4. 预期结果：跨边界的 shared buffer 维度 +1，本地访问多了版本下标；非跨边界的 buffer（如只被消费者用的 `C_local`）不变。
5. 若不便拆 pass：可直接阅读上述源码理解改写规则，标注「待本地验证」。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `IfStmtBinding` 只处理没有 else 的 `if`？
  - **答案**：`T.ws` 语义是「这个组才执行」，对应单分支 `if`；带 else 意味着两组都要各走一支，不属于简单绑定场景，保留原样交给后续 pass。
- **练习 2**：若 `num_stages=3`，`A_shared` 会被复制成几份？
  - **答案**：3 份（版本维大小 = `num_stages`），下标为 `floormod(linear_index, 3)`，轮转使用 0/1/2 三个槽。

### 4.3 `WarpSpecialized` 自动特化：角色标记与 mbarrier 同步

#### 4.3.1 概念说明

上一节讲的是**用户手动写 `T.ws`** 的路径。`WarpSpecialized` pass 还支持另一条路：**用户不写 `T.ws`**，只是写了普通的 `T.copy`（会被 lower 成 TMA）+ `T.gemm`，编译器**自动**识别哪些语句是搬运、哪些是计算，把它们拆给两个 warp group，并自动插入 mbarrier 同步。这叫「自动 warp 特化」。

无论哪条路，核心都是给每条语句打上 **Role**（角色）标签。角色只有三种：

| Role | 含义 |
| --- | --- |
| `kProducer` | 生产者：TMA load，或从 global 写 shared 的搬运 |
| `kConsumer` | 消费者：纯计算、寄存器/fragment 操作 |
| `kBoth` | 兼有两者，或无法单一归类 |

#### 4.3.2 核心流程：手动 vs 自动的分叉

`WarpSpecialized` pass 入口 [src/transform/warp_specialized_rewriter.cc:1298-1317](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/warp_specialized_rewriter.cc#L1298-L1317) 先用 `WarpSpecializedDetector::Detect` 判定走哪条路：

```cpp
bool warp_specialized = WarpSpecializedDetector::Detect(f->body);
if (!warp_specialized) {
  // 自动路径：用户没写 T.ws → 自动拆分
  return WarpSpecializedRewriter::Substitute(f, ...);
} else {
  // 手动路径：用户已写 T.ws（或 TMA+mbarrier）→ 仅打标记，不重写
  f.CopyOnWrite()->body =
      AttrStmt(node, attr::kCustomWarpSpecialization, 1, f->body);
  return f;
}
```

`Detect`（[src/transform/warp_specialized_rewriter.h:34-48](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/warp_specialized_rewriter.h#L34-L48)）返回 `true`（即「禁用自动、走手动」）的两种情况：

1. 检测到 `warp_specialize` 属性（用户写了 `T.ws`）；
2. 同时出现 TMA op 与 mbarrier op（用户在手写底层同步）。

```text
WarpSpecializedDetector::Detect
  ├─ has_warp_specialization_ (T.ws)        → true（手动）
  ├─ has_tma_op_ && has_mbarrier_op_        → true（手动）
  └─ 否则                                    → false（自动）
```

> 重要判断：示例里用户写了 `T.ws` + `barrier_arrive/wait`，`T.copy` 又会 lower 成 TMA，因此 `Detect` 返回 true，走**手动路径**——`WarpSpecialized` 只挂 `kCustomWarpSpecialization` 标记，真正搭脚手架的是 4.2 的 `IfStmtBinding`+`MultiVersionBuffer`。自动路径则是另一种用法（用户只写普通 copy/gemm）。

#### 4.3.3 源码精读：角色标记

角色标记器 `WarpSpecializedRoleMarker`（[src/transform/warp_specialized_rewriter.cc:139-259](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/warp_specialized_rewriter.cc#L139-L259)）按语句类型判定 Role。两条最关键的规则：

- TMA load 是生产者（[第 157-169 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/warp_specialized_rewriter.cc#L157-L169)）：

```cpp
if (call->op.same_as(tma_load()) || call->op.same_as(tma_load_im2col())) {
  role = Role::kProducer;
  has_bulk_copy_ = true;
}
```

- 写 shared、且读源来自 global 的 `BufferStore` 是生产者（SIMT 搬运）；否则是消费者（[第 171-200 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/warp_specialized_rewriter.cc#L171-L200)）：

```cpp
bool is_shared_store = scope.rank == StorageRank::kShared;
...
for (auto read : reads) {
  if (read->buffer.scope() != "global") { role = Role::kConsumer; break; }
}
if (role == Role::kProducer) has_simt_copy_ = true;
```

判定「能否自动特化」的前提是 `HasProducer()`（[第 248 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/warp_specialized_rewriter.cc#L248)）——检测不到任何生产者就直接返回，不特化。

#### 4.3.4 源码精读：自动路径的分区与同步插入

自动路径的核心在 [WarpSpecializedRewriter::VisitStmt_(BlockRealize)](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/warp_specialized_rewriter.cc#L1202-L1282)（[第 1202-1282 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/warp_specialized_rewriter.cc#L1202-L1282)）。它用 `WSCodeEmitter` 把同一份循环体分别「投影」成 producer 代码与 consumer 代码：

```cpp
WSCodeEmitter producer(true,  thread_iv_, buffer_data_to_buffer_, marker);
WSCodeEmitter consumer (false, thread_iv_, buffer_data_to_buffer_, marker, false);
Stmt producer_code = producer(block->body);   // 只保留生产者语句
Stmt consumer_code = consumer (block->body);  // 只保留消费者语句
```

随后把 block 的线程数扩成「消费者线程数 + 生产者线程数」，并用一个 `if` 把两组代码分到不同线程区间（[第 1240-1279 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/warp_specialized_rewriter.cc#L1240-L1279)）：

```cpp
updated_thread_extent_ = consumer_thread_extent + producer_thread_extent;
...
Stmt body = IfThenElse(GE(thread_iv_->var, consumer_thread_extent),
                       producer_code, consumer_code);
body = AttrStmt(ws_partition, attr::kWarpSpecializationScope, 0, body);
```

这里的 `kWarpSpecializationScope` 属性会告诉后续 `ThreadSync` pass：这个 block 里线程数被分成了两段，插同步时要按段计数。

两组线程如何同步？靠 mbarrier。`WSCodeEmitter` 的 [CreateBaseSyncPairs](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/warp_specialized_rewriter.cc#L946-L1019)（[第 946-1019 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/warp_specialized_rewriter.cc#L946-L1019)）分析语句间的读写依赖：只要生产者写某 buffer、消费者读同一 buffer，就配出一对 `(release, acquire)` 同步点——生产者在该 buffer 写完后 `arrive`，消费者在读前 `wait`。配对后再用 `RemoveUnusedSyncPatterns`（[第 1021-1060 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/warp_specialized_rewriter.cc#L1021-L1060)）合并冗余对（例如「Produce(A); Produce(B); Consume(A,B)」只需保留最晚的一个 release）。

自动生成的同步原语见文件上半段：[makeArriveBarrier](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/warp_specialized_rewriter.cc#L265-L274)（`ptx_arrive_barrier`）、[makeParityWait](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/warp_specialized_rewriter.cc#L282-L286)（`mbarrier_wait_parity`）。`parity`（奇偶）正是示例里 `ko % 2` 的来源——双缓冲下两套缓冲轮流使用，靠奇偶翻转区分。

#### 4.3.5 代码实践：触发自动特化

1. 实践目标：体会「不写 `T.ws`」也能被自动特化。
2. 操作步骤：写一个普通 `T.Pipelined` 的 GEMM（不写 `T.ws`，只用 `T.copy`+`T.gemm`），在 Hopper target 下编译，用 `kernel.get_kernel_source()` 查看生成的 CUDA。
3. 需要观察的现象：生成的 CUDA 里出现两组线程区间（`threadIdx.x >= consumer_extent` 分支）、`mbarrier` 相关 PTX、以及 `cp.async.bulk`（TMA）调用。
4. 预期结果：自动路径产出与手写 `T.ws` 结构相似的「搬运组 + 计算组」代码（待本地 Hopper GPU 验证）。
5. 若无 Hopper：阅读 4.3.3–4.3.4 的源码理解判定与分区逻辑即可，标注「待本地验证」。

#### 4.3.6 小练习与答案

- **练习 1**：为什么 `Detect` 在「同时出现 TMA 与 mbarrier」时要禁用自动特化？
  - **答案**：这说明用户已经在手写底层 TMA+mbarrier 同步，自动特化若再插一道同步会与之冲突或重复，故退出让用户主导。
- **练习 2**：自动路径里，`producer_thread_extent` 在什么情况下固定为 128？
  - **答案**：当 `!marker.HasSimtCopy()`（只有 TMA bulk copy、没有 SIMT 搬运）时，搬运只需一个 warp group，故生产者线程数取 128（[第 1243-1244 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/warp_specialized_rewriter.cc#L1243-L1244)）。

### 4.4 wgmma 异步等待重写与 `InjectFenceProxy`

前两节解决了「谁干搬运、谁干计算、怎么同步」。本节解决 wgmma 作为异步指令带来的两个收尾问题：**何时等待**、**代理切换处要不要加栅栏**。

#### 4.4.1 概念说明

wgmma 是异步的：发出后结果不立即可见，要 `warpgroup_wait`。朴素做法是每条 gemm 后立刻 wait，但这会让多条 wgmma 串行，丢失重叠机会。更好的做法是**连续发多条 wgmma**（结果累加进同一累加器），**到最后才 wait 一次**。`RewriteWgmmaSync` 就是把「立即 wait」自动推迟到「最晚安全点」。

`InjectFenceProxy` 解决的是另一类问题：Hopper 把指令分到 generic 与 async 两条代理路径。当一条 generic 指令（如 `ldmatrix`、shared store、descriptor 初始化）后面紧跟一条 async 指令（如 `wgmma`、TMA、`cp.async`）时，硬件要求中间插入 `fence.proxy.async` 才能保证顺序，否则可能竞争或未定义行为。

#### 4.4.2 核心流程

`RewriteWgmmaSync`（针对每个带 `tl_pipeline_order` 的流水线循环）：

```text
1. 收集循环体里所有 gemm 语句及其紧跟的 arrive_barrier（release）
2. 对每条 gemm，向后扫描，找到「不再安全」的第一条语句（与该 gemm 的读/写 buffer 冲突），
   它的前一条就是「最晚安全点」last_stmt
3. 把该 gemm 的 extern 名改成 "..., -1>"  → 告诉模板「不要立即 wait」
4. 删掉原来的 warpgroup_wait/arrive，改在 last_stmt 之后插入 cute::warpgroup_wait<N>
   （N = 当前已发出但未等待的 wgmma 数）
5. 合并多余的 wait（只保留 max_sync_index 那次，并算出正确等待计数）
```

`InjectFenceProxy`：

```text
顺序扫描每个语句序列，维护「上一条 / 当前条」的代理类型：
  generic（ldmatrix/stmatrix/descriptor init/普通 store）
  async（TMA load-store/wgmma/tl_gemm/cp.async）
  neutral（fence 本身，重置状态）
只要发现 generic → async 的切换，就在两者之间插一条 fence_proxy_async()
未知外部调用按 async 处理（保守策略，宁可多插）
```

#### 4.4.3 源码精读：RewriteWgmmaSync

判定函数 [isGemm](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/wgmma_sync_rewriter.cc#L22-L36) 与 [isGemmSync](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/wgmma_sync_rewriter.cc#L38-L52) 靠 extern 调用名字里是否含 `"gemm"` / `"warpgroup_wait"` 来识别。主逻辑在 [VisitStmt_(ForNode)](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/wgmma_sync_rewriter.cc#L116-L243)（[第 116-243 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/wgmma_sync_rewriter.cc#L116-L243)）。

把 gemm 改成「不立即等待」的关键，是改写其 extern 名——把结尾的 `>` 换成 `, -1>`（[第 181-191 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/wgmma_sync_rewriter.cc#L181-L191)）：

```cpp
std::string name = Downcast<StringImm>(call->args[0])->value;
std::string new_name = name.substr(0, name.size() - 1) + ", -1>";
// 例如 "cute::wgmma...>" → "cute::wgmma..., -1>"，-1 表示 accumulate 但不 wait
```

随后在「最晚安全点」之后插入等待（[第 196-211 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/wgmma_sync_rewriter.cc#L196-L211)）：

```cpp
if (stmt_node->seq[i].same_as(last_stmts_[j])) {
  new_seq.push_back(Evaluate(Call(..., StringImm("cute::warpgroup_wait<0>"), Integer(j))));
  ...
}
```

最后 [第 214-238 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/wgmma_sync_rewriter.cc#L214-L238) 计算正确的等待计数 `wait_count = gemm_count - sync_index - 1`，并把多个 wait 合并：只保留 `sync_index` 达到新高的那次，其余置为 no-op，从而用一次 `warpgroup_wait<N>` 等待所有已发出的 wgmma。

#### 4.4.4 源码精读：InjectFenceProxy

[ProxyKind](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L28-L35) 枚举定义了四种代理状态。何时需要插栅栏由 [NeedsFence](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L60-L68) 决定——只在 generic 紧跟 async 时才插：

```cpp
inline bool NeedsFence(ProxyKind prev, ProxyKind curr) {
  ...
  return IsGeneric(prev) && IsAsync(curr);   // 唯一需要插栅栏的切换
}
```

哪些算 async？[IsAsyncIntrinsic](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L76-L103) 列出了 TMA load/store、`ptx_wgmma_ss/rs`、`ptx_cp_async*`，以及 TileLang 的 `tl_gemm` / `tl_gemm_sp`。主遍历器 [ProxyFenceInjector::VisitStmt_(SeqStmtNode)](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L177-L202)（[第 177-202 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L177-L202)）逐条扫序列，发现切换就插：

```cpp
for (const Stmt &stmt : op->seq) {
  Stmt new_stmt = VisitStmt(stmt);
  ProxyKind current_kind = GetProxyKind(new_stmt);
  if (!seq.empty() && NeedsFence(prev_kind, current_kind)) {
    seq.push_back(MakeFenceStmt());        // 插 fence.proxy.async
    prev_kind = GetProxyKind(fence);
  }
  seq.push_back(new_stmt);
  ...
}
```

`docs/compiler_internals/inject_fence_proxy.md` 给了一个直观例子（[第 19-25 行的时间线图](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/compiler_internals/inject_fence_proxy.md#L19-L25)）：`descriptor init(generic) → shared-store(generic) → wgmma(async)`，栅栏插在 store 与 wgmma 之间。用户也可手动调 `T.fence_proxy_async()`（[tilelang/language/builtin.py:128-137](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/builtin.py#L128-L137)），但默认 lowering 会自动插，无需手写。

#### 4.4.5 代码实践：定位自动插入的栅栏

1. 实践目标：看到 `InjectFenceProxy` 自动插入的 `fence.proxy.async`。
2. 操作步骤：对一个 Hopper 上的 wgmma kernel，用 `kernel.get_kernel_source()` 取出 CUDA 源码，搜索 `fence.proxy.async`。
3. 需要观察的现象：在「shared store / ldmatrix（generic）」与「wgmma / cp.async（async）」之间出现 `fence.proxy.async` PTX 指令。
4. 预期结果：generic→async 切换处必有栅栏；async→async、generic→generic 处无多余栅栏（待本地验证）。
5. 无 Hopper 时：可对照 `docs/compiler_internals/inject_fence_proxy.md` 的 Before/After 例子理解，标注「待本地验证」。

#### 4.4.6 小练习与答案

- **练习 1**：`RewriteWgmmaSync` 为什么要把 wait 推迟到「最晚安全点」而不是循环末尾？
  - **答案**：循环末尾固然安全，但可能比必要点更晚，增加等待时延；「最晚安全点」是「下一条会与该 gemm 的读写 buffer 冲突的语句」之前，既保证正确又尽量延后，最大化 wgmma 之间的重叠。
- **练习 2**：`InjectFenceProxy` 遇到无法识别的外部调用时按哪种代理处理？为什么？
  - **答案**：按 async 处理（保守策略）。这样 generic→未知 会触发插栅栏，宁可多插也不漏，避免潜在竞争。

## 5. 综合实践：把 pass 编排放回脑子里的「调色盘」

把本讲所有 pass 串起来，回到 `OptimizeForTarget` 的 Hopper 分支 [tilelang/engine/phase.py:197-213](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L197-L213)：

```python
if allow_tma_and_warp_specialized(...):
    mod = tilelang.transform.IfStmtBinding()(mod)           # 4.2：拆 if
    mod = tilelang.transform.MultiVersionBuffer()(mod)       # 4.2：shared buffer 多版本
    mod = tilelang.transform.WarpSpecialized()(mod)          # 4.3：手动标记 / 自动特化
    mod = tilelang.transform.InjectTmaBarrier()(mod)         # TMA 载入与 mbarrier 接线
    mod = tilelang.transform.AnnotateWarpGroupRegAlloc()(mod)
    mod = tilelang.transform.PipelinePlanning()(mod)         # u4-l2：流水线规划
    mod = tilelang.transform.InjectSoftwarePipeline()(mod)
    mod = tilelang.transform.LowerOpaqueBlock()(mod)
    mod = tilelang.transform.MergeIfStmt()(mod)
    if is_hopper(target):
        mod = tilelang.transform.RewriteWgmmaSync()(mod)     # 4.4：wgmma 延迟等待
    mod = tilelang.transform.InjectFenceProxy()(mod)         # 4.4：代理栅栏
```

任务：以 `examples/warp_specialize/example_warp_specialize_gemm_copy_1_gemm_0.py` 为对象，完成下面的「pass 追踪表」——填出每个 pass 对示例循环体的关键改变：

| pass | 对示例（`T.ws` + 双 barrier + Pipelined）的作用 |
| --- | --- |
| `IfStmtBinding` | ? |
| `MultiVersionBuffer` | ? |
| `WarpSpecialized` | ?（提示：示例走手动路径，只打标记） |
| `RewriteWgmmaSync` | ? |
| `InjectFenceProxy` | ? |

参考答案（要点）：

- `IfStmtBinding`：把 `with T.ws(1): {wait; copy; copy; arrive}` 拆成四条各自带 `threadIdx.x >= 128` 条件的语句；`T.ws(0)` 同理拆成带 `< 128` 条件的语句。
- `MultiVersionBuffer`：`A_shared`/`B_shared` 因「被 group1 写、被 group0 读」而加版本维（大小=`num_stages=2`），访问处加 `floormod(ko, 2)` 下标；`C_local` 仅消费者用，不变。
- `WarpSpecialized`：检测到 `T.ws`（`warp_specialize` 属性），走手动路径，仅挂 `kCustomWarpSpecialization`，不重写分区。
- `RewriteWgmmaSync`：把 `T.gemm` 对应的 wgmma 改成不立即 wait，把 `warpgroup_wait` 推迟到该轮 `barrier_arrive(compute_is_done)` 之前的最晚安全点。
- `InjectFenceProxy`：在 shared store（generic）与 wgmma/TMA（async）切换处插入 `fence.proxy.async`。

> 注意：本实践以源码阅读与 IR 推理为主；若要看到精确的 IR 中间态，需要在 Hopper 环境下拆分调用各 pass 并 `mod.show()`，相关具体输出「待本地验证」。

## 6. 本讲小结

- **warp 特化**的核心是「生产者 warp group 做搬运、消费者 warp group 做计算，靠 mbarrier 同步重叠」，是 Hopper 上隐藏访存延迟的利器，常与 `T.Pipelined` 多缓冲配合。
- 前端 **`T.ws(group_id)`** 把 warp group 编号翻译成 `threadIdx.x` 区间条件，在 TIR 里生成一个带 `warp_specialize=1` 属性的单分支 `if`（[warpgroup.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/warpgroup.py)、[ir.cc:365-404](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/ir.cc#L365-L404)）。
- 手动特化路径靠 **`IfStmtBinding`**（拆 if）+ **`MultiVersionBuffer`**（跨边界 shared buffer 加版本维）搭脚手架，`WarpSpecialized` 只打 `kCustomWarpSpecialization` 标记。
- 自动特化路径由 **`WarpSpecialized`** 内部的 `WarpSpecializedRewriter` 完成：用 `WarpSpecializedRoleMarker` 给语句标 producer/consumer，把循环体投影成两组代码、扩线程数、插 `IfThenElse` 分区，并按读写依赖自动配出 mbarrier 的 arrive/wait。
- **`RewriteWgmmaSync`** 利用 wgmma 的异步性，把「立即等待」推迟到最晚安全点，让多条 wgmma 的累加与等待合并，提升重叠度。
- **`InjectFenceProxy`** 在 generic→async 代理切换处自动插入 `fence.proxy.async`，保守地把未知调用当 async，保证 Hopper 上的内存序正确。

## 7. 下一步学习建议

- 想看 wgmma/tma 的**指令模板**如何最终生成 CUDA：进入 u7-l2（CUDA 模板与 GEMM 内核族），阅读 `src/tl_templates/cuda/gemm_sm90.h` 与 `instruction/wgmma.h`。
- 想理解 `PipelinePlanning`/`InjectSoftwarePipeline` 与本讲 `MultiVersionBuffer` 的多缓冲异同：复习 u4-l2（软件流水线与异步拷贝）。
- 想动手扩展编译器：参考 u7-l4（Transform pass 深入与扩展），尝试仿照 `InjectFenceProxy` 写一个诊断 pass 并挂到 `OptimizeForTarget`。
- 想看 warp 特化在真实大模型里的应用：阅读 `examples/warp_specialize/example_warp_specialize_flashmla.py`，体会 FlashMLA 这类注意力 kernel 如何用 warp 特化压榨 Hopper。
