# matvec_pipeline：load/compute/store 流水线

## 1. 本讲目标

在 [u8-l1](u8-l1-op-interface-noop-reference.md) 里，我们把一个 op 看成「五子结构填空题」，并用 `NoOp`（一个什么都不干、却让虚拟机正常运转的 op）当参照。但真实的 op 显然不是「全填空格都填立刻归还资源」——它要真正干活。本讲就打开仓库里**最常用、最有代表性**的一个真实 op 模板：把一次矩阵-向量乘（matvec）拆成 **load / compute / store** 三条并行流水线的 `matvec_pipeline`。

具体来说，学完本讲你应该能说清楚：

1. `matvec_pipeline` 用哪几个常量（`INPUT_PIPELINE_STAGES` / `OUTPUT_PIPELINE_STAGES` / `STAGE_PAGES`）描述流水线的「宽度」，又如何把这 13 个物理 page（`NUM_PAGES == 13`）划分成「1 个激活页 + 3 个 input 级 × 4 个权重页」。
2. `loader_loop` / `consumer_loop` / `storer_loop` 这三个函数各自跑在哪个角色上、用什么信号量相互握手、如何用**相位位（phase bit）**实现多级缓冲的轮转。
3. 信号量的命名约定：`*_arrived` 表示「数据已在共享内存就绪」，`*_finished` 表示「我已用完、你可以覆盖」；以及 `init_semaphore` 的初始计数为什么有的是 1、有的是 `NUM_CONSUMER_WARPS`。
4. **`release_lid` 这一格的精妙之处**：它不是固定的回收策略，而是一张随 `iters % 3` 变化的「页轮转表」`ret_order`——本讲会用算术推导把它彻底讲透。

本讲是「会读 op」到「会读懂一个真正的低延迟流水线 op」的关键一步。`matvec_pipeline` 不仅是 llama 推理里 RMSNorm/QKV/UpProj/Gate 等十几个 matvec 类 op 的共同基类，也是理解整个 HazyResearch 低延迟推理引擎「为什么能做到逐 token 极低延迟」的核心。

## 2. 前置知识

本讲是 advanced 阶段，默认你已经掌握以下内容（否则建议先读对应讲义）：

- **op 的五子结构与填空契约**（[u8-l1](u8-l1-op-interface-noop-reference.md)）：一个 op 提供 `controller` / `loader` / `launcher` / `consumer` / `storer` 五个子结构，分别被虚拟机的五条流水线调用。本讲的 `matvec_pipeline` 不是 op 本身，而是 op 的**基类模板**——真实 op（如 `rms_qkv_rope_append`）继承它后，把 `loader/consumer/storer::run` 直接转发给基类的 `loader_loop/consumer_loop/storer_loop`。
- **物理页 pid 与逻辑页 lid**（[u5-l3](u5-l3-vm-state-and-pages.md)）：`pid` 是 page 在共享内存里的物理下标；`lid` 是 op 眼里的「逻辑用途编号」。`state::pid(lid)` 用 `pid_order[lid]` 把逻辑页映射到物理页。
- **page 生命周期与相位位**（[u7-l1](u7-l1-shared-memory-page-lifecycle.md)）：每个 page 配一组 `page_finished` 信号量，用相位位实现「跨指令复用」。`wait_page_ready(pid)` 等到上一任拥有者放手，`finish_page(pid, count)` 以 `count` 次 arrive 宣告「我已用完」。关键代码在 [include/util.cuh:155-168](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L155-L168)，相位位由 `instruction_index` 的若干 bit 决定。
- **动态信号量与相位轮转**（[u7-l2](u7-l2-dynamic-semaphores.md)）：一个 `init` 为 `N` 的信号量，被 `arrive` 满 `N` 次后翻转相位；`wait(sem, bit)` 用 `bit` 选择等的是哪一「圈」。多级缓冲就是靠这种「每过 K 次翻转一次相位」来复用同一段共享内存。
- **controller 如何调用 `release_lid`**（[u6-l2](u6-l2-instruction-fetch-and-page-allocator.md)）：controller 在为「本条」指令分配物理页时，读「上一条」指令的 opcode，调 `op::controller::release_lid(g, last_instruction, lane)`，返回一个 `lid`，再令 `pid_order[lane] = last.pid_order[lid]`——即「本条的 lane 号物理页 = 上一条某个 lid 对应的物理页」。真实调用点在 [include/controller/controller.cuh:87-98](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L87-L98)。

一句话心智模型：**`matvec_pipeline` 把一次 matvec 拆成「装填弹药（load）→ 开火（compute）→ 收靶（store）」三个兵种，三者用一组信号量异步握手，从而让访存与计算重叠。** 而 `release_lid` 是这三者之外、由 controller 调用的「第四个兵种」——它负责在指令之间把 13 个物理页「轮转」到位，保证下一条指令一上来就有干净的页可写。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [demos/low-latency-llama/matvec_pipeline.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh) | **本讲主角**。定义 `matvec_pipeline` 模板（L7-L244）与 `rms_matvec_pipeline` 子模板（L246-L349），含全部常量、page 划分、信号量访问器、`release_lid`、`init_semaphores`、三个 `*_loop`。 |
| [demos/low-latency-llama/rms_matvec_rope_append.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu) | 一个**真实 op** `rms_qkv_rope_append`（L10-L231），继承 `rms_matvec_pipeline`，提供 `parsed_instruction`（含 `iters` 字段）与 `pipeline_specifics`（`load_iter` / `store` / `gmem_wait` 回调）。本讲多次用它当「宿主 op」举例。 |
| [include/util.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh) | `state<config>` 提供 `pid()` / `wait_page_ready()` / `finish_page()` / `warp_finish_page()` / `semaphores()` / `scratch()` 等 API（L150-L174 等）。 |
| [include/controller/controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) | controller 主循环 Step 2（L74-L99）调用 `release_lid`，把它的返回值 `lid` 翻译成物理页 `pid`。 |
| [include/config.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh) | `NUM_PAGES` 的定义与硬约束 `static_assert(NUM_PAGES == 13, ...)`（L43-L44），正好对应本流水线 1+3×4=13 个 lid。 |

## 4. 核心概念与源码讲解

### 4.1 流水线常量与 page 编号

#### 4.1.1 概念说明

一个 matvec op 要算的东西，本质是：给定一个激活向量 \(a\)（shape `[hidden_dim]`）和一摞权重块，算出若干个 16 元素的输出片段。但在低延迟推理里，「等全部权重加载完再算」会浪费大量时间——访存和计算应当**重叠**。`matvec_pipeline` 的做法是：把权重按时间顺序分成一批批「级（stage）」，一边加载第 N+1 批，一边计算第 N 批，一边写回第 N-1 批的结果。

为此它定义了几个描述「流水线宽度」的常量。看 [demos/low-latency-llama/matvec_pipeline.cuh:8-12](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L8-L12)：

```cpp
static constexpr int INPUT_PIPELINE_STAGES = 3;   // 输入（权重）缓冲级数
static constexpr int OUTPUT_PIPELINE_STAGES = 3;  // 输出缓冲级数
static constexpr int STAGE_PAGES = 4;             // 每个 input 级占几个权重 page
static constexpr int ACTIVATION_PAGE = 0;         // 激活向量的 lid
static constexpr int WEIGHTS_START_PAGE = 1;      // 权重 page 从 lid 1 开始
```

含义：

- **`INPUT_PIPELINE_STAGES = 3`**：权重加载用 3 级缓冲（三级深度流水）。任意时刻最多有 3 批权重「在路上 / 在算」。
- **`OUTPUT_PIPELINE_STAGES = 3`**：输出写回也用 3 级缓冲。输出存在 scratch 区（不是 page），下文 4.1.3 详述。
- **`STAGE_PAGES = 4`**：每个 input 级的权重又被切成 4 个 page（因为单个 page 放不下整个权重 tile，且要让 4 组 consumer warp 各管一页）。
- **`ACTIVATION_PAGE = 0` / `WEIGHTS_START_PAGE = 1`**：lid 0 专门放激活向量；权重从 lid 1 起排。

由此可推出**全部 13 个 lid 的布局**（也解释了为什么 `config.cuh` 要硬性要求 `NUM_PAGES == 13`）：

| lid | 用途 | 归属 |
| --- | --- | --- |
| 0 | 激活向量 \(a\)（以及 rms scale） | activation |
| 1, 2, 3, 4 | input 级 0 的 4 个权重 page | stage 0 |
| 5, 6, 7, 8 | input 级 1 的 4 个权重 page | stage 1 |
| 9, 10, 11, 12 | input 级 2 的 4 个权重 page | stage 2 |

把 lid 翻译成物理页用 `get_weight_page`，见 [demos/low-latency-llama/matvec_pipeline.cuh:33-36](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L33-L36)：

```cpp
static __device__ inline int get_weight_page(state &s, int stage, int offset) {
    return s.pid(WEIGHTS_START_PAGE + stage * STAGE_PAGES + offset);
}
// 例：stage=1, offset=2  →  lid = 1 + 1*4 + 2 = 7
```

#### 4.1.2 核心流程

整个 matvec 被拆成**三个并发循环**，它们各自跑在一个管理 warp（loader / storer）或 16 个 consumer warp 上，处理同一条指令里的 `inst.iters` 个迭代。每个迭代消费「一个 16 元素输出片段」对应的权重：

```
loader_loop (1 warp, lane 0 干活)          consumer_loop (16 warp)         storer_loop (1 warp)
  for iter in [0, iters):                    for i in [0, iters):             for i in [0, iters):
    等 weights_finished[input_stage]           等 weights_arrived[input_stage]   等 outputs_arrived[output_stage]
    TMA 加载 4 个权重页 → input_stage           等 outputs_finished[output_stage]
    arrive(weights_arrived[input_stage])       matvec(出部分和 → scratch)        store(scratch → gmem)
                                               arrive(outputs_arrived[...])
                                               arrive(weights_finished[...])    arrive(outputs_finished[...])
    input_stage = (input_stage+1) % 3          (最后 3 个迭代释放权重页)          output_stage = (output_stage+1) % 3
```

三者的 `*_stage` 都以 3 为模轮转（`% INPUT_PIPELINE_STAGES` / `% OUTPUT_PIPELINE_STAGES`），于是同一段物理页 / scratch 被 3 个迭代复用——这就是「3 级缓冲」。注意三者并非靠 `__syncthreads` 同步，而是靠**信号量**异步握手，所以 loader 加载下一批时，consumer 还在算上一批，storer 还在写更早的一批，三者真正并行。

#### 4.1.3 源码精读

先看「要多少信号量」。`SEM_COUNT` 在 [demos/low-latency-llama/matvec_pipeline.cuh:17-18](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L17-L18)：

```cpp
static constexpr int SEM_COUNT = 1 + (INPUT_PIPELINE_STAGES + OUTPUT_PIPELINE_STAGES) * 2;
// = 1 + (3 + 3) * 2 = 13
```

这 13 个信号量槽位的分配见 4.6 节的命名约定表。这里先记住「1 个给激活，其余 12 个 = (3 输入级 + 3 输出级) × 2（arrived/finished 各一）」。

再看输出怎么放。输出**不占 page**，而是放在专门的 scratch 区，避免和权重 page 抢共享内存。见 [demos/low-latency-llama/matvec_pipeline.cuh:20-26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L20-L26)：

```cpp
static constexpr int SCRATCH_BYTES_PER_WARP = 16 * sizeof(float);            // 每个 warp 一个 16 元 float 输出
static constexpr int SCRATCH_BYTES_PER_STAGE = SCRATCH_BYTES_PER_WARP * Config::NUM_CONSUMER_WARPS;
static constexpr int USED_SCRATCH_BYTES = OUTPUT_PIPELINE_STAGES * SCRATCH_BYTES_PER_STAGE;
static_assert(USED_SCRATCH_BYTES <= Config::SCRATCH_BYTES, "...");
```

`get_output_start(s, stage)`（[demos/low-latency-llama/matvec_pipeline.cuh:65-68](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L65-L68)）按 `output_stage` 把 scratch 切成 3 段，每段装一个级的「16 warp × 16 float」输出。所以输出三级缓冲复用的是 scratch，而非 page。

最后确认「物理页只有 13 个」这一硬约束：[include/config.cuh:43-44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L43-L44) 写死 `static_assert(NUM_PAGES == 13, ...)`，与本流水线的 `1 + 3*4` 完全吻合。

#### 4.1.4 代码实践

**实践目标**：用纸笔把 13 个 lid 的布局画出来，确认「物理页总数」与「流水线宽度」的等式关系。

**操作步骤**：

1. 打开 [demos/low-latency-llama/matvec_pipeline.cuh:8-12](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L8-L12)，把 `INPUT_PIPELINE_STAGES`、`STAGE_PAGES`、`ACTIVATION_PAGE`、`WEIGHTS_START_PAGE` 抄下来。
2. 用公式 `lid = WEIGHTS_START_PAGE + stage * STAGE_PAGES + offset` 填出一张 13 行的表：lid 0 是什么？lid 7 属于哪个 stage、哪个 offset？
3. 算出 `1 + INPUT_PIPELINE_STAGES * STAGE_PAGES`，对比 [include/config.cuh:43-44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L43-L44) 的 `NUM_PAGES == 13`。

**需要观察的现象**：lid 7 = `1 + 1*4 + 2`，属于 stage 1、offset 2；13 个 lid 恰好用满 `NUM_PAGES`，没有任何冗余。

**预期结果**：你得到一张「lid 0 = 激活；lid 1-4 = stage0；5-8 = stage1；9-12 = stage2」的表，并理解为什么 `config.cuh` 要把 `NUM_PAGES` 写死成 13——这是本流水线 page 布局的**不变量**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `STAGE_PAGES` 改成 5（保持 3 个 input 级），`NUM_PAGES` 需要是多少？还需要改哪些地方？

**答案**：需要 `1 + 3*5 = 16` 个物理页。需要同步修改 [include/config.cuh:43-44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L43-L44) 的 `static_assert(NUM_PAGES == 13)`，以及 `release_lid` 里所有 `ret_order[13]` 数组（4.2 节）都要扩成 `ret_order[16]` 并重新设计轮转表。这说明 `STAGE_PAGES`、`INPUT_PIPELINE_STAGES`、`NUM_PAGES`、`release_lid` 是**一套绑定在一起的不变量**。

**练习 2**：为什么输出用 scratch 而不用 page？

**答案**：page 的生命周期由 `page_finished` 相位信号量管理，且要参与 controller 的 `release_lid` 跨指令轮转（4.2 节）。输出是 consumer 产、storer 消的**临时中间量**，用专门的 `outputs_arrived/finished` 信号量就够了（4.6 节），没必要挤进 page 的轮转表。把它放进 scratch，让 page 这套复杂机制只服务于「需要跨指令长生命周期」的激活与权重。

### 4.2 release_lid：跨指令的 page 轮转表

#### 4.2.1 概念说明

`release_lid` 是本模板最烧脑、也最精妙的一格。回顾 [u8-l1](u8-l1-op-interface-noop-reference.md)：controller 为「本条」指令 R 分配物理页时，会问「上一条」指令 R-1 的 op：「你的第 `query` 个物理页槽位（`query` = lane 号），可以复用你哪个 lid 对应的物理页？」op 用 `release_lid(g, last_instruction, query)` 返回那个 `lid`。controller 再执行 `pid_order[lane] = last.pid_order[lid]`（见 [include/controller/controller.cuh:96-97](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L96-L97)）。

为什么不能简单地「本条 lid q 复用上一条 lid q」（即恒等映射）？因为指令 R 和 R-1 在**时间上重叠**：R 的 controller 分配页时，R-1 很可能还在跑（loader 还在加载、consumer 还在算）。如果 R 把自己的 lid q 映射到一个 R-1 **还没用完**的物理页，R 一写就冲掉了 R-1 还需要的数据。`release_lid` 的职责就是：**返回一张「把 R-1 最早放手的物理页，优先配给 R 最早需要写的 lid」的映射表**，让两者的页使用时间窗尽可能错开，把 `wait_page_ready` 的停顿降到最低。

#### 4.2.2 核心流程

先说结论（下文用算术验证）：`release_lid` 返回的 `ret_order` 是一张**随 `iters % 3` 旋转的置换表**。设 \(r = \text{iters} \bmod 3\)：

- 当 \(\text{iters} \ge 3\) 时，激活页（lid 0）映射到自己（R 的激活 ← R-1 的激活），而**R 的权重 stage \(s\) 复用 R-1 的权重 stage \((s + r) \bmod 3\)**。
- 当 \(\text{iters} \in \{1, 2\}\) 时，R-1 只用了 1~2 个权重 stage，其余 stage 的物理页**从未被写过**，于是把这些「未用页」排在表的最前面（它们立刻可复用、零等待），接着是激活页，最后才是 R-1 真正用过的权重页。

为什么是「stage \(s\) ← stage \((s+r)\bmod 3\)」？因为 R 填权重 stage 的顺序是 0→1→2（loader 的 `input_stage` 从 0 递增），而 R-1 **放手**权重 stage 的顺序取决于它最后 3 个迭代：R-1 在迭代 \(i = \text{iters}-3, \text{iters}-2, \text{iters}-1\) 分别放手 stage \((\text{iters}-3)\bmod3,\;(\text{iters}-2)\bmod3,\;(\text{iters}-1)\bmod3\)，即 \(r, r+1, r+2 \pmod 3\)——最早放手的是 stage \(r\)。于是把 R 最早填的 stage 0 配给 R-1 最早放手的 stage \(r\)，依此类推，正是「stage \(s\) ← stage \((s+r)\bmod 3\)」。

#### 4.2.3 源码精读

`release_lid` 全文在 [demos/low-latency-llama/matvec_pipeline.cuh:70-102](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L70-L102)。开头两行注释是理解全表的钥匙：

```cpp
// NOTE: assumes a three stage pipeline
// unused pages, then activation, then weights
```

「unused pages, then activation, then weights」描述的就是 `ret_order` 这个数组**从前到后**的排列优先级：先列出 R-1 未用过的页（最安全，零等待），再列激活页（较早放手），最后列真正用过的权重页（最晚放手）。这个顺序与「R 填自己 lid 的先后（lid 0 激活最先、权重 lid 1..12 其后）」对齐。

五个分支的 `ret_order` 如下（数组下标 = `query`/lane，值 = 要复用的 R-1 的 lid）：

| 条件 | `ret_order`（query → lid） | 物理含义 |
| --- | --- | --- |
| `iters == 1` | `{5,6,7,8, 9,10,11,12, 0, 1,2,3,4}` | R-1 只用了 stage0；stage1/2(lid5-12)未用→排最前，激活(lid0)居中，stage0(lid1-4)最后 |
| `iters == 2` | `{9,10,11,12, 0, 1,2,3,4, 5,6,7,8}` | R-1 用了 stage0/1；stage2(lid9-12)未用→最前，激活居中，stage0/1最后 |
| `remainder == 1` | `{0, 5,6,7,8, 9,10,11,12, 1,2,3,4}` | 激活自映；权重 stage 旋转 +1：R stage0←R-1 stage1(lid5-8)… |
| `remainder == 2` | `{0, 9,10,11,12, 1,2,3,4, 5,6,7,8}` | 激活自映；权重 stage 旋转 +2：R stage0←R-1 stage2(lid9-12)… |
| `remainder == 0` | `{0,1,2,3,4,5,6,7,8,9,10,11,12}` | 恒等：iters 是 3 的倍数时无需旋转 |

代码里这五张表就是字面常量，例如 [demos/low-latency-llama/matvec_pipeline.cuh:92-94](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L92-L94)（`remainder == 1`）：

```cpp
} else if (remainder == 1) {
    int ret_order[13] = {0, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3, 4};
    return ret_order[query];
}
```

**验证「stage 旋转」**：取 `remainder == 1`（即 `iters = 4, 7, …`）。R 的 lid 1-4（stage0）对应 `ret_order[1..4] = 5,6,7,8`，即 R-1 的 stage1；R 的 lid 5-8（stage1）← `9,10,11,12`（R-1 stage2）；R 的 lid 9-12（stage2）← `1,2,3,4`（R-1 stage0）。所以 R 的 stage \(s\) ← R-1 的 stage \((s+1)\bmod 3\)，正是 \(r=1\) 时的旋转量。✓

`iters` 来自哪里？来自上一条指令本身——`release_lid` 在 [demos/low-latency-llama/matvec_pipeline.cuh:75-82](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L75-L82) 用宿主 op 的 `parsed_instruction` 解析传入的 `instruction`，取出 `inst.iters`：

```cpp
parsed_instruction inst{instruction};
auto iters = inst.iters;
auto remainder = iters % INPUT_PIPELINE_STAGES;
```

例如宿主 `rms_qkv_rope_append::parsed_instruction` 在 [demos/low-latency-llama/rms_matvec_rope_append.cu:41](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L41) 把 `iters` 算成 `end_block_idx - start_block_idx`——即这条 matvec 要算多少个 16 元素片段。

> ⚠️ 注意 `release_lid` 用 `static_assert(INPUT_PIPELINE_STAGES == 3, ...)`（[matvec_pipeline.cuh:78-79](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L78-L79)）锁死了「必须 3 级」。这五张表是针对 3 级手写的；若改成 2 级或 4 级，整套表都要重设计。

#### 4.2.4 代码实践（本讲核心实践之一）

**实践目标**：亲手验证「不同 `remainder` 对应的 `ret_order`」的含义，把 4.2.2 的结论落到具体的数字上。

**操作步骤**：

1. 打开 [demos/low-latency-llama/matvec_pipeline.cuh:86-101](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L86-L101)，把五张 `ret_order` 抄到表格里。
2. 对 `remainder == 2`（`iters = 5`）这一支，按 lid 布局（stage0=1-4, stage1=5-8, stage2=9-12）把 `ret_order` 分段，填出「R 的 stage0/1/2 ← R-1 的哪个 stage」。
3. 用「R-1 最后 3 个迭代放手的 stage 顺序」推出 `r=2` 时 R-1 最早放手的是哪个 stage，确认它被配给了 R 的 stage0。
4. 对 `iters == 1`，回答：R-1 实际用到了哪些 lid？表里前 8 个值（lid 5-12）为什么能「零等待」复用？（提示：看 4.3.4 的 helper-lane 机制。）

**需要观察的现象**：

- `remainder == 2`：R stage0(lid1-4) ← R-1 stage2(lid9-12)，R stage1(lid5-8) ← R-1 stage0(lid1-4)，R stage2(lid9-12) ← R-1 stage1(lid5-8)。即旋转 +2。
- `r=2` 时 R-1 最早放手 stage \((\text{iters}-3)\bmod3 = 2\)，正是配给 R stage0 的那个 stage。✓
- `iters == 1`：R-1 只用了 stage0（lid1-4）；lid5-12 从未被 loader 写过，被 helper lane 立即 `finish_page` 放手（4.3 节），所以排在表前、零等待。

**预期结果**：你能口头复述「\(r = \text{iters}\bmod 3\) 决定权重 stage 的旋转量，激活页自映；\(\text{iters}<3\) 时未用页排最前」这条规则，并对任意 `iters` 手写出对应的 `ret_order`。

> 说明：本实践为源码阅读型，无需运行 GPU。若要在机器上验证，可在 `release_lid` 末尾临时加一行打印 `query` 与返回值（仅调试用，勿提交），观察 controller 在不同 `iters` 指令间的实际映射——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`iters == 6`（`remainder == 0`）时 `ret_order` 是恒等映射 `{0,1,…,12}`。这是否意味着「R 的每个 lid 复用 R-1 同号 lid 的物理页」就一定安全？

**答案**：是恒等映射，但「安全」并非由 `release_lid` 单独保证，而是由 `release_lid`（决定**哪个**物理页给哪个 lid）与 `page_finished` 相位信号量（决定**何时**可写）共同保证。恒等映射之所以在这里安全，是因为 `iters` 是 3 的倍数时，R-1 放手权重 stage 的顺序恰好是 0→1→2，与 R 填 stage 的顺序 0→1→2 天然对齐，再加上 loader 写入前会 `wait_page_ready`（4.3 节），所以不会冲数据。

**练习 2**：为什么 `iters == 1` 和 `iters == 2` 要从 `remainder` 分支里单独拎出来特判？

**答案**：因为只有 \(\text{iters}<3\) 时，R-1 才会**留下未使用的权重 stage**（stage 全部或部分没被 loader 写过）。这些未用页的物理页被 loader 的 helper lane 在指令一开始就 `finish_page` 放手（[matvec_pipeline.cuh:155-159](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L155-L159)），比激活页还早可用，所以必须排在 `ret_order` 最前面。`iters >= 3` 时所有 stage 都被用过，没有「未用页」这一档，于是激活页就成了最靠前的。源码注释「only then do we free pages before the activation/rms scale (page 0)」说的正是这件事（[matvec_pipeline.cuh:84-85](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L84-L85)）。

### 4.3 loader_loop：load 阶段与「未用页」回收

#### 4.3.1 概念说明

`loader_loop` 跑在 loader warp 上（每个 block 1 个），职责是把权重从 global memory 经 TMA 加载进共享内存 page。但它还悄悄干了第二件事：**把本指令用不到的 page 立刻放手**——这正是 4.2 节里「未用页零等待」的来源。

关键在于：一条 matvec 指令的 `iters` 可能小于 3（只算 1~2 个片段），那它只需要 `1 + min(iters,3)*4` 个 page，剩余的物理页「虽然分给了本指令，却永远不会被写」。如果不主动放手，下一条指令复用这些页时就会卡在 `wait_page_ready` 上。所以 loader 用一部分 lane 专门去「等并放手」这些未用页。

#### 4.3.2 核心流程

loader warp 有 32 个 lane，按 `laneid` 分成两类：

```
needed_pages = 1 + min(iters, 3) * 4     // 本指令真正要用的页数
if laneid == 0:        // 主加载 lane
    for iter in [0, iters):
        等 weights_finished[input_stage]  (相位: iter%6 < 3)   // 等 consumer 用完上一圈的这一级
        TMA expect + 4 次 load_async → input_stage 的 4 个权重页
        input_stage = (input_stage+1) % 3
else if needed_pages <= laneid < NUM_PAGES:   // helper lane：回收未用页
    pid = s.pid(laneid)
    s.wait_page_ready(pid)        // 等上任拥有者放手
    s.finish_page(pid, NUM_CONSUMER_WARPS)   // 立刻放手给下一条指令
else:
    (laneid 在 [1, needed_pages) 内：本流水线里这些 lane 空闲)
```

主加载 lane 用 `weights_arrived[input_stage]` 通知 consumer「这一级权重好了」；它加载前先 `wait(weights_finished[input_stage])`，确保 consumer 已经读完了**上一圈**同一个 stage 的旧数据（否则 TMA 会覆盖还在被读的页）。helper lane 则把未用页提前 `finish_page`，让 controller 在下一条指令的 `release_lid` 里能把它们当「立即可用」的页排进表头。

#### 4.3.3 源码精读

`needed_pages` 的计算在 [demos/low-latency-llama/matvec_pipeline.cuh:121-122](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L121-L122)：

```cpp
auto needed_pages = 1 + min(inst.iters, INPUT_PIPELINE_STAGES) * STAGE_PAGES;
```

主加载循环见 [demos/low-latency-llama/matvec_pipeline.cuh:127-154](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L127-L154)，其中加载前的等待与相位位：

```cpp
kittens::wait(weights_finished(s, input_stage),
     (iter % (2 * INPUT_PIPELINE_STAGES)) < INPUT_PIPELINE_STAGES);  // iter%6 < 3
```

这个相位位 `(iter % 6) < 3` 是「每 3 个迭代翻转一次」的标准多级缓冲写法（详见 4.6.4）。真正的 TMA 加载由宿主 op 的回调 `pipeline_specifics::load_iter` 完成（[matvec_pipeline.cuh:149-150](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L149-L150)），例如宿主 `rms_qkv_rope_append` 在 [rms_matvec_rope_append.cu:62-70](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L62-L70) 用 `tma::load_async` 把 `g.qkv_weights` 的一块搬进 `weight_chunk`。

helper-lane 回收未用页见 [demos/low-latency-llama/matvec_pipeline.cuh:155-159](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L155-L159)：

```cpp
} else if (kittens::laneid() >= needed_pages && kittens::laneid() < Config::NUM_PAGES) {
    auto pid = s.pid(kittens::laneid());
    s.wait_page_ready(pid);
    s.finish_page(pid, Config::NUM_CONSUMER_WARPS);
}
```

注意 `finish_page(pid, NUM_CONSUMER_WARPS)`——这里 `count = NUM_CONSUMER_WARPS` 是因为 `page_finished` 信号量按「consumer warp 数」计数（见 [include/util.cuh:163-168](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L163-L168) 的 `arrive(page_finished[pid][i], count)`）。未用页没有被任何 consumer 读，但相位协议要求「放手的 arrive 次数」与「正常读完的 arrive 次数」一致，所以这里一次性补足 `NUM_CONSUMER_WARPS` 次。

#### 4.3.4 代码实践

**实践目标**：跟踪 `iters == 2` 时 loader 的两类 lane，确认「未用页」被哪些 lane 回收。

**操作步骤**：

1. 读 [demos/low-latency-llama/matvec_pipeline.cuh:117-160](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L117-L160)。
2. 对 `iters == 2`，算 `needed_pages = 1 + min(2,3)*4 = 9`。
3. 判断：lane 0 干什么？lane 1-8 干什么？lane 9-12（共 4 个）干什么？lane 9-12 对应的 lid 是哪些？

**需要观察的现象**：lane 0 跑主加载循环（加载 stage0、stage1）；lane 1-8 空闲；lane 9-12 是 helper lane，回收 lid 9-12（stage2，未用）。这正是 `iters==2` 的 `ret_order` 把 lid 9-12 排在最前的原因。

**预期结果**：你验证了「未用页 = `needed_pages` 之后的 lid」，且这些页由 loader 的 helper lane 提前 `finish_page` 放手。**待本地验证**：若在 `finish_page` 前后各加一条 `printf`（仅调试），可看到 helper lane 在主加载之前就完成了回收。

#### 4.3.5 小练习与答案

**练习 1**：主加载 lane 在加载 `input_stage` 之前为什么要 `wait(weights_finished(input_stage))`？

**答案**：因为 `input_stage` 以 3 为模轮转，loader 在第 `iter+3` 圈会**重写**同一个物理 stage。必须等 consumer 把第 `iter` 圈的该 stage 读完（即 consumer arrive 满 `weights_finished` 的 `NUM_CONSUMER_WARPS` 次并翻转相位），才能安全覆盖。这就是 3 级缓冲的「读后再写」约束。

**练习 2**：helper lane 回收未用页时，`finish_page` 的 `count` 为什么是 `NUM_CONSUMER_WARPS` 而不是 1？

**答案**：`page_finished` 信号量被 consumer 以「每 warp arrive 一次、共 `NUM_CONSUMER_WARPS` 次」的方式翻转相位（见 consumer_loop 末尾的 `warp_finish_page`，4.4 节）。loader 端 `finish_page` 的 `count` 必须与 consumer 端的总 arrive 次数匹配，相位才能正确翻转。未用页虽无 consumer 真正读取，但相位协议要求一致的 arrive 次数，所以一次性 `arrive` 满 `NUM_CONSUMER_WARPS` 次。

### 4.4 consumer_loop：compute 阶段

#### 4.4.1 概念说明

`consumer_loop` 跑在 16 个 consumer warp 上（这是它与 loader/storer「单 warp」的最大区别），是真正做 matvec 计算的地方。每个迭代它要：等本级的权重到位 → 等输出缓冲可写 → 做一次 matvec，把部分和写进 scratch → 通知 storer「输出好了」、通知 loader「权重我读完了」→ 在最后 3 个迭代释放权重页。

它还做了一件和 `release_lid` 强相关的事：**权重页在最后 3 个迭代才被释放**，这决定了 R-1「放手 stage 的顺序」，也就是 4.2 节旋转表的物理来源。

#### 4.4.2 核心流程

```
WARPS_PER_PAGE = NUM_CONSUMER_WARPS / STAGE_PAGES = 16/4 = 4   // 每个权重页由 4 个 warp 分摊
page_index = warpid() / WARPS_PER_PAGE                          // 本 warp 读 stage 内的第几个页
for i in [0, iters):
    等 weights_arrived[input_stage]      (相位: i%6 >= 3)        // 等 loader 把本级权重搬好
    等 outputs_finished[output_stage]    (相位: i%6 <  3)        // 等 storer 把上一圈本级输出写走
    weights = pages[weight_page] 里的本 warp 那块 tile
    out_smem = scratch 里本 warp、本 output_stage 的 16 元 float
    matvec(out_smem, weights, activations_vec)                  // 真正的计算
    arrive(outputs_arrived[output_stage])                       // 告诉 storer：输出好了
    arrive(weights_finished[input_stage])                       // 告诉 loader：本级权重读完了
    if i >= iters - 3:   // 最后 3 个迭代：释放本级 4 个权重页
        for j in [0,4): warp_finish_page(get_weight_page(input_stage, j), 1)
    input_stage  = (input_stage +1) % 3
    output_stage = (output_stage+1) % 3
```

「最后 3 个迭代释放页」是关键：在迭代 \(i\) 释放的是 stage \(i\bmod 3\)。于是 R-1 在 \(i=\text{iters}-3,-2,-1\) 分别释放 stage \(r, r+1, r+2 \pmod 3\)（\(r=\text{iters}\bmod3\)）——最早释放 stage \(r\)，正对应 4.2.2 节「R stage0 ← R-1 stage \(r\)」的结论。

#### 4.4.3 源码精读

warp 到 tile 的映射在 [demos/low-latency-llama/matvec_pipeline.cuh:168-183](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L168-L183)：

```cpp
constexpr int WARPS_PER_PAGE = Config::NUM_CONSUMER_WARPS / STAGE_PAGES;  // 4
int page_index = kittens::warpid() / WARPS_PER_PAGE;
...
kittens::st_bf<16, REDUCTION_DIM_PER_WARP> &weights =
    reinterpret_cast<...>(s.pages[weight_page].ptr())[kittens::warpid() % WARPS_PER_PAGE];
```

即 16 个 warp 按 `warpid` 分成 4 组（每组 4 个 warp），分别读 stage 内 4 个页之一；每页内又按 `warpid % 4` 取自己的 tile。其中 `REDUCTION_DIM_PER_WARP = hidden_dim / NUM_CONSUMER_WARPS`（[matvec_pipeline.cuh:14-15](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L14-L15)），把权重的归约维（hidden_dim）切给 16 个 warp。

两个 wait 与两次 arrive 构成 consumer 的全部同步（[matvec_pipeline.cuh:177-199](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L177-L199)）：等权重到位、等输出可写；算完后再 arrive 通知 storer 与 loader。注意 consumer 是「16 个生产者」同时 arrive `outputs_arrived`/`weights_finished`，所以这两个信号量的 init 计数是 `NUM_CONSUMER_WARPS`（见 4.6 节）。

最后 3 个迭代释放权重页在 [demos/low-latency-llama/matvec_pipeline.cuh:201-207](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L201-L207)：

```cpp
if (i >= inst.iters - INPUT_PIPELINE_STAGES) {
    for (int j = 0; j < STAGE_PAGES; j++)
        s.warp_finish_page(get_weight_page(s, input_stage, j), 1);
}
```

`warp_finish_page(pid, 1)` 表示「本 warp 的 lane 0 代替整个 warp arrive 一次」（[include/util.cuh:170-173](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L170-L173)）。16 个 warp 各 arrive 1 次，凑齐 `NUM_CONSUMER_WARPS` 次，`page_finished` 翻转相位，loader 才能在下一圈安全覆盖。

#### 4.4.4 代码实践

**实践目标**：跟踪 `iters == 4`（`remainder == 1`）时 consumer 在哪几个迭代释放哪几个 stage，验证 4.2 节的「R-1 放手顺序」。

**操作步骤**：

1. 读 [demos/low-latency-llama/matvec_pipeline.cuh:175-211](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L175-L211)。
2. 对 `iters = 4`：最后 3 个迭代是 \(i = 1, 2, 3\)（因为 `i >= 4-3 = 1`）。填出每个 \(i\) 释放的 `input_stage`（= \(i\bmod3\)）。
3. 把释放顺序与 4.2 节 `remainder==1` 的 `ret_order` 对照，确认「最早释放的 stage」被配给了 R 的 stage0。

**需要观察的现象**：\(i=1\) 释放 stage1，\(i=2\) 释放 stage2，\(i=3\) 释放 stage0。所以 R-1 放手顺序 = stage1 → stage2 → stage0，最早放手 stage1——正是 `remainder==1` 表里 R stage0 复用的 R-1 stage1。✓

**预期结果**：你亲眼看到「consumer 最后 3 个迭代的释放顺序」就是 `release_lid` 旋转表的物理依据。两者是**同一事实的两个侧面**：consumer 决定「何时放手」，`release_lid` 决定「如何把手中的页轮转给下一条指令」。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`iters == 2` 时，consumer 释放页的条件 `i >= iters - 3 = -1` 对所有 \(i \in \{0,1\}\) 都成立。这意味着什么？

**答案**：意味着 `iters < 3` 时，consumer 在**每一个**迭代都释放对应的 stage。\(i=0\) 释放 stage0，\(i=1\) 释放 stage1。stage2 从未被 loader 写过（4.3 节），所以它不靠 consumer 释放，而是靠 loader 的 helper lane 释放。两条路径合起来，保证了「未用页」与「已用页」都能被正确放手。

**练习 2**：为什么释放页用 `warp_finish_page(..., 1)` 而不是 `finish_page(..., NUM_CONSUMER_WARPS)`？

**答案**：`finish_page(pid, count)` 是「本 lane 一次性 arrive `count` 次」；`warp_finish_page(pid, 1)` 是「只让每个 warp 的 lane 0 arrive 1 次」。consumer 有 16 个 warp，每个 warp 的 lane 0 arrive 1 次，自然凑齐 16 次——这正是 `page_finished` 期望的 arrive 总数。用 `warp_finish_page` 表达「每个 warp 贡献一次」更贴合语义，也避免 32 个 lane 各 arrive 一次造成的 32 次（超量）。

### 4.5 storer_loop：store 阶段

#### 4.5.1 概念说明

`storer_loop` 跑在 storer warp 上（1 个），职责是把 consumer 算好的输出从 scratch 写回 global memory。它和 consumer 之间用 `outputs_arrived` / `outputs_finished` 这对信号量握手：等 consumer 算完（`outputs_arrived`），写回 gmem，再放手输出缓冲（`outputs_finished`）。

它还多了一个模板参数 `iter_scale`，允许「每 `iter_scale` 个迭代才真正 store 一次」——用于多个 matvec 输出需要先在 scratch 里累加、再一次性写回的场景。

#### 4.5.2 核心流程

```
output_stage = 0
for i in [0, iters):
    等 outputs_arrived[output_stage]   (相位: i%6 >= 3)      // 等 consumer 把本级输出算好
    pipeline_specifics::store(s, g, inst, i, output_stage)   // 宿主决定怎么写回（常含 TMA store）
    if (i+1) % iter_scale == 0:
        for j in [0, iter_scale): arrive(outputs_finished[(i-j) % 3])  // 放手这 iter_scale 个输出缓冲
    output_stage = (output_stage+1) % 3
```

默认 `iter_scale == 1`：每个迭代 store 一次、立刻放手一个输出缓冲。宿主 op 在 `store` 回调里实现具体写回逻辑（如 RoPE 变换 + TMA store 到 q/k/v cache）。

#### 4.5.3 源码精读

storer 主循环见 [demos/low-latency-llama/matvec_pipeline.cuh:219-242](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L219-L242)。等待与相位位（[matvec_pipeline.cuh:221-225](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L221-L225)）：

```cpp
auto &sem = outputs_arrived(s, output_stage);
auto bit = (i % (2 * OUTPUT_PIPELINE_STAGES)) >= OUTPUT_PIPELINE_STAGES;  // i%6 >= 3
kittens::wait(sem, bit);
```

注意这个相位位 `i%6 >= 3` 与 consumer 端等 `outputs_finished` 的相位位 `i%6 < 3`（[matvec_pipeline.cuh:179-180](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L179-L180)）**互补**——生产者与消费者用相反的相位，这是多级缓冲的标准配对（4.6.4 详述）。

`iter_scale` 的批量放手在 [demos/low-latency-llama/matvec_pipeline.cuh:235-240](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L235-L240)：

```cpp
if ((i + 1) % iter_scale == 0) {
    for (int j = 0; j < iter_scale; j++) {
        auto stage_to_arrive = (i - j) % OUTPUT_PIPELINE_STAGES;
        kittens::warp::arrive(outputs_finished(s, stage_to_arrive));
    }
}
```

即攒够 `iter_scale` 个输出后，一次性放手这 `iter_scale` 个 output_stage 的缓冲。`outputs_finished` 的 init 计数是 1（单生产者 = storer），所以 storer 的 `warp::arrive`（lane 0 arrive 一次）刚好。

#### 4.5.4 代码实践

**实践目标**：阅读一个真实宿主的 `store` 回调，理解 storer 如何把 scratch 输出写回 gmem。

**操作步骤**：

1. 打开 [demos/low-latency-llama/rms_matvec_rope_append.cu:72-167](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L72-L167)（`pipeline_specifics::store`）。
2. 找到它如何从 scratch 取出输出（`matvec_reduce`，[rms_matvec_rope_append.cu:90-92](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L90-L92)）、做 RoPE（L106-L121）、再用 `tma::store_async` 写到 q/k/v cache（L129-L152）。
3. 注意它默认走 `iter_scale == 1`（`rms_qkv_rope_append` 的 `storer::run` 直接调 `pipeline::storer_loop(s, g)`，见 [rms_matvec_rope_append.cu:227-229](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L227-L229)），所以每个迭代 store 一次。

**需要观察的现象**：`store` 回调内部并不直接 `arrive(outputs_finished)`——那是 `storer_loop` 在 `store` 返回后统一做的。回调只负责「把数据写走」。

**预期结果**：你理解了「storer_loop = 通用骨架（等/调 store 回调/放手）+ 宿主 store 回调（具体写回逻辑）」的分工。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`outputs_finished` 的 init 计数是 1，而 `outputs_arrived` 是 `NUM_CONSUMER_WARPS`。为什么不对称？

**答案**：因为「输出」的生产者是 16 个 consumer warp（每个算一个部分和，16 路 arrive 凑齐才算「输出好了」），所以 `outputs_arrived` 需要 `NUM_CONSUMER_WARPS` 次 arrive；而「输出缓冲可复用」的生产者是唯一的 storer warp（它写完就走，1 次 arrive 即可放手），所以 `outputs_finished` init 为 1。init 计数 = 该信号量的**生产者数量**（详见 4.6 节约定）。

**练习 2**：什么场景下会把 `iter_scale` 设为大于 1？

**答案**：当多个 matvec 输出需要先在 scratch 里**累加**，再一次性写回时。例如把若干个 16 元片段在 scratch 内累加成一个更大的输出再 store，就设 `iter_scale = 累加的片段数`，这样 storer 每 `iter_scale` 个迭代才 store 一次并批量放手缓冲。本讲的 `rms_qkv_rope_append` 用的是默认 `iter_scale == 1`（每片段独立 store）。

### 4.6 信号量命名约定与 init 计数

#### 4.6.1 概念说明

把前几节散落的信号量收拢来看，`matvec_pipeline` 用了 13 个信号量（`SEM_COUNT = 13`），它们遵循一套**极其规律的命名约定**：

- **`*_arrived` = 「数据已在共享内存就绪」**：由「把数据搬进/算进 smem 的一方」生产，由「要读这份数据的一方」消费。
- **`*_finished` = 「我已用完、你可以覆盖」**：由「读完这份数据的一方」生产，由「要复用这块缓冲的一方」消费。

而且 init 计数有一条铁律：**init 计数 = 该信号量的生产者数量**。单 warp 生产 → init 1；16 个 consumer warp 共同生产 → init `NUM_CONSUMER_WARPS`。

#### 4.6.2 核心流程

13 个槽位的布局（下标见 [matvec_pipeline.cuh:38-57](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L38-L57)）：

| 槽位 | 信号量 | 生产者 | 消费者 | init |
| --- | --- | --- | --- | --- |
| 0 | `activations_arrived` | loader（TMA 载入激活） | consumer | 1 |
| 1-3 | `weights_arrived[stage]` | loader（TMA 载入权重） | consumer | 1 |
| 4-6 | `weights_finished[stage]` | consumer（16 warp 各 arrive 一次） | loader | `NUM_CONSUMER_WARPS` |
| 7-9 | `outputs_arrived[stage]` | consumer（16 warp 各 arrive 一次） | storer | `NUM_CONSUMER_WARPS` |
| 10-12 | `outputs_finished[stage]` | storer（1 warp arrive） | consumer | 1 |

可以看到「arrived 多为 1（loader 单生产者），finished 多为 NUM_CONSUMER_WARPS 或 1」的规律并非绝对——关键看**谁是生产者**：`outputs_arrived` 虽叫 arrived，但生产者是 16 个 consumer，所以 init 是 `NUM_CONSUMER_WARPS`。

#### 4.6.3 源码精读

信号量下标访问器在 [demos/low-latency-llama/matvec_pipeline.cuh:38-57](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L38-L57)，例如：

```cpp
static auto &weights_arrived(state &s, int stage)   { return s.semaphores()[1 + stage]; }
static auto &weights_finished(state &s, int stage)  { return s.semaphores()[1 + INPUT_PIPELINE_STAGES + stage]; }
static auto &outputs_arrived(state &s, int stage)   { return s.semaphores()[1 + 2*INPUT_PIPELINE_STAGES + stage]; }
static auto &outputs_finished(state &s, int stage)  { return s.semaphores()[1 + 2*INPUT_PIPELINE_STAGES + OUTPUT_PIPELINE_STAGES + stage]; }
```

init 计数集中在 [demos/low-latency-llama/matvec_pipeline.cuh:104-115](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L104-L115)：

```cpp
init_semaphore(activations_arrived(s), 1);
for (int i = 0; i < INPUT_PIPELINE_STAGES; i++) {
    init_semaphore(weights_arrived(s, i), 1);                       // loader 单生产者
    init_semaphore(weights_finished(s, i), Config::NUM_CONSUMER_WARPS); // 16 个 consumer
}
for (int i = 0; i < OUTPUT_PIPELINE_STAGES; i++) {
    init_semaphore(outputs_arrived(s, i), Config::NUM_CONSUMER_WARPS);  // 16 个 consumer
    init_semaphore(outputs_finished(s, i), 1);                      // storer 单生产者
}
return SEM_COUNT;  // 13
```

子模板 `rms_matvec_pipeline` 在此基础上**再追加 1 个** `rms_scale_arrived`（[matvec_pipeline.cuh:257-261](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L257-L261) 与 [270-274](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L270-L274)），把 `SEM_COUNT` 从 13 扩到 14——这就是「op 可以在自己的 `init_semaphores` 里追加信号量」的实例，印证了 [u8-l1](u8-l1-op-interface-noop-reference.md) 讲的「`init_semaphores` 返回本指令用了几个信号量」。

#### 4.6.4 相位位的「每 K 翻转」约定

所有 `wait` 的第二个参数（相位位）都是 `(i % (2*STAGES)) < STAGES` 或其否定 `>= STAGES` 的形式。以 3 级为例：

\[
\text{bit}(i) = \begin{cases} 0 & i \bmod 6 \in \{0,1,2\} \\ 1 & i \bmod 6 \in \{3,4,5\} \end{cases}
\]

即每 3 个迭代翻转一次相位。配对规则：**同一对 arrived/finished 的生产者与消费者用互补相位**。例如 consumer 等 `weights_arrived` 用 `i%6 >= 3`，loader 等 `weights_finished` 用 `i%6 < 3`——loader 在「前半圈」等 consumer 放手，consumer 在「后半圈」等 loader 备好，两者错开半圈，正好让 3 级缓冲无缝轮转。这套相位机制与 [u7-l2](u7-l2-dynamic-semaphores.md) 讲的动态信号量相位完全一致，这里只是把它套在了一个 3 级、跨 3 个角色的流水线上。

#### 4.6.5 代码实践

**实践目标**：核对命名约定表与源码，确认「init 计数 = 生产者数量」。

**操作步骤**：

1. 打开 [demos/low-latency-llama/matvec_pipeline.cuh:104-115](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L104-L115) 的 `init_semaphores`。
2. 对每个 `init_semaphore`，去对应的 `*_loop` 里找「谁 arrive 了它、arrive 了几次/几个 warp」。
3. 验证：`weights_finished` 被 consumer 的 16 个 warp 各 arrive 一次（[matvec_pipeline.cuh:199](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L199)），与 init=`NUM_CONSUMER_WARPS` 一致。

**需要观察的现象**：每个信号量的 init 值都等于「生产它的 arrive 总次数」。若 init 偏大，`wait` 会永远等不到；若偏小，相位会提前翻转、读到脏数据。

**预期结果**：你能凭「生产者数量」一眼推出任意一个信号量的 init 值，不必死记。**待本地验证**。

#### 4.6.6 小练习与答案

**练习 1**：`activations_arrived` 为什么 init 是 1，而不是 `NUM_CONSUMER_WARPS`？激活不是 16 个 warp 都要读吗？

**答案**：init 计数看的是「**生产者**数量」，不是消费者数量。激活由 loader（单 warp，通过 TMA）一次性载入，载入完成 arrive 1 次即可，所以 init 为 1。至于 16 个 consumer 都要读它，那是「读」——消费侧只需 `wait` 到相位翻转即可，不需要每消费一次就 arrive。`weights_arrived` 同理（loader 单生产者，init 1）。

**练习 2**：如果有人误把 `outputs_arrived` 的 init 写成 1，会发生什么？

**答案**：`outputs_arrived` 的生产者是 16 个 consumer warp，它们每个 arrive 一次共 16 次。若 init 错写成 1，那么**第一个** consumer warp arrive 后相位就翻转，storer 会立刻以为「输出好了」去读——但此时另外 15 个 warp 还没算完，storer 读到的是不完整的部分和。表现为数值错误（输出随机缺分量）。这正是「init = 生产者数量」这条铁律的用途。

## 5. 综合实践：画出 3 级 input + 3 级 output 流水时序甘特图（本讲核心实践之二）

**实践目标**：把 loader / consumer / storer 三条流水线在同一时间轴上画出来，直观看到「访存与计算如何重叠」，并标出每个信号量 wait/arrive 发生的时刻。这是检验你是否真正理解本讲的最有效方式。

**场景设定**：取 `iters = 6`（`remainder == 0`，`release_lid` 恒等映射，最简单）。三个循环并发运行，每个处理 6 个迭代，stage 以 3 为模轮转。设每个迭代在三条流水线上各占一个「时间槽」（实际三者耗时不同，但画甘特图时先假设等长，便于看清错位关系）。

**操作步骤**：

1. 先单独写出每条流水线前 6 个迭代的 `input_stage` / `output_stage` 序列：
   - loader `input_stage`：0,1,2,0,1,2
   - consumer `input_stage`（=读哪个权重级）：0,1,2,0,1,2；`output_stage`（=写哪个输出级）：0,1,2,0,1,2
   - storer `output_stage`：0,1,2,0,1,2
2. 按「consumer 必须等 loader 的 `weights_arrived`、storer 必须等 consumer 的 `outputs_arrived`」的依赖，把三者**错位一格**排开。
3. 在甘特图上标注每个槽位发生的关键 wait/arrive。

**参考甘特图**（时间槽 t0..t8，`L`=load、`C`=compute、`S`=store，下标为 stage）：

```
时间槽      t0    t1    t2    t3    t4    t5    t6    t7    t8
loader  :  L0    L1    L2    L0    L1    L2
            ↓arr  ↓arr  ↓arr  ↓arr  ↓arr  ↓arr
              weights_arrived[stage]
consumer:        C0    C1    C2    C0    C1    C2
                  ↑wait weights_arrived
                  ↓arr outputs_arrived / weights_finished
storer  :              S0    S1    S2    S0    S1    S2
                        ↑wait outputs_arrived
                        ↓arr outputs_finished
```

**需要观察的现象与解释**：

- **三级错位**：consumer 比 loader 晚 1 槽（要等权重到位），storer 比 consumer 晚 1 槽（要等输出算好）。于是同一时刻 t2，loader 在装 L2（第 3 批）、consumer 在算 C1（第 2 批）、storer 在写 S0（第 1 批）——**三级缓冲让三条流水真正并行**，这正是低延迟的来源。
- **stage 轮转与相位**：t3 时 loader 又回到 L0，但它先 `wait(weights_finished[0])`（相位 `3%6<3` 为假，即等相位 1）——确保 consumer 在 t1 读完的 stage0 已被放手。consumer 在 t3 读 stage0 时 `wait(weights_arrived[0])` 用相位 `3%6>=3`（等相位 1）。两者用**互补相位**安全地复用同一个 stage0 物理页。
- **`release_lid` 的角色**：本图是「单条指令内部」的时序；`release_lid` 描述的是「相邻两条指令之间」的页轮转（4.2 节）。两者配合：指令内部靠信号量相位保证「读后再写」，指令之间靠 `release_lid` 保证「最早放手的页配给最早需要的 lid」。

**预期结果**：你能徒手画出这张甘特图，并指着任意一个槽位说清楚「此刻 loader/consumer/storer 各在处理哪个 stage、在等哪个信号量、在 arrive 哪个信号量」。若做不到，回到 4.3-4.5 节对应的 `*_loop` 重读。

**延伸（可选）**：把场景换成 `iters = 4`（`remainder == 1`），重画甘特图，并额外画出「下一条指令 R 的 loader 如何复用 R-1 放手的 stage1」——把 4.2 节的轮转表与本甘特图对接。**待本地验证**：若能拿到 timing 输出（`TEVENT_FIRST_LOAD`/`TEVENT_FIRST_USE`/`TEVENT_FIRST_STORE` 等事件，见 [matvec_pipeline.cuh:144-146](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L144-L146)、[189-193](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L189-L193)、[227-231](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L227-L231)），可把它转成真实时间轴，验证三者的重叠关系。

## 6. 本讲小结

- `matvec_pipeline` 用 `INPUT_PIPELINE_STAGES=3` / `OUTPUT_PIPELINE_STAGES=3` / `STAGE_PAGES=4` 描述流水线宽度，把 `NUM_PAGES==13` 个物理页划成「1 激活 + 3 级 × 4 权重页」，输出则放在 scratch 里另做 3 级缓冲。
- 它把一次 matvec 拆成 `loader_loop` / `consumer_loop` / `storer_loop` 三个**异步并发**循环，分别跑在 loader warp、16 个 consumer warp、storer warp 上，靠信号量而非 `__syncthreads` 握手，从而让访存与计算真正重叠。
- 信号量遵循「`*_arrived` = 数据就绪、`*_finished` = 可复用」的命名约定，且 **init 计数 = 生产者数量**（单 warp→1，16 consumer→`NUM_CONSUMER_WARPS`）；配对的 arrived/finished 用**互补相位位** `(i%6)<3` 与 `(i%6)>=3` 实现 3 级轮转。
- `release_lid` 是一张随 `iters % 3` 旋转的页轮转表：`iters≥3` 时激活页自映、权重 stage \(s\) 复用上一条的 stage \((s+r)\bmod3\)；`iters<3` 时未用页（被 loader helper lane 提前放手）排最前。
- consumer 在**最后 3 个迭代**释放权重页，释放顺序 stage \(r,r+1,r+2\) 正是 `release_lid` 旋转表的物理依据——「consumer 决定何时放手、`release_lid` 决定如何轮转」是同一事实的两面。
- 真实 op（如 `rms_qkv_rope_append`）只需继承本模板、提供 `parsed_instruction`（含 `iters`）与 `pipeline_specifics`（`load_iter`/`store`/`gmem_wait`），即可复用整套流水线骨架——这就是模板化 op 设计的威力。

## 7. 下一步学习建议

- **横向对比其它流水线 op**：阅读 [demos/low-latency-llama/matvec_adds.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu)、[upgate.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/upgate.cu)、[rms_lm_head.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_lm_head.cu)，看它们如何复用 `matvec_pipeline` / `rms_matvec_pipeline`，又如何定制 `pipeline_specifics::store`（例如累加、残差加法）。这能巩固「模板骨架 + 宿主回调」的分工。
- **纵向深入 attention 流水线**：[attention_partial.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu) 与 [attention_reduction.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu) 是另一类流水线（用 `NUM_STAGES` 而非本讲的 3 级），对比两者的页回收与信号量约定，能看清「流水线级数」如何影响 `release_lid` 设计。
- **回到 controller 全景**：重读 [include/controller/controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) 的 Step 2，把本讲的 `release_lid` 放回「controller 分配页 → loader 等页 → consumer 用页 → consumer 放页 → 下一条 release_lid」的完整闭环里，确认你理解了 page 在指令之间的完整生命周期。
