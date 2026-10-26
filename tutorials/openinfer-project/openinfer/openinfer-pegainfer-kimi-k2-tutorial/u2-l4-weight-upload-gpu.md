# u2-l4 · mmap 上载与 GPU 权重包

> 讲义编号：u2-l4 ｜ 依赖：u2-l3（INT4 Marlin 权重打包）、u1-l4（构建与运行：feature、CLI 与引擎入口）
> 阅读方式：本讲沿一条真实数据通路走——「磁盘上的 safetensors 分片 → mmap → 切片 → H2D 拷入显存 → 组装成 GPU 权重包 → 整理成前向缓存」。

## 1. 本讲目标

学完本讲，你应该能够：

1. 跟踪 `load_rank_sliced_weights_to_gpu` 的完整数据通路：mmap 分片 → 按切片规格取字节 → H2D 拷贝 → 显存字典。
2. 说出三层 GPU 权重包的层次关系与分工：`KimiRankGpuWeights`（原始字节层）→ `KimiRankExpertMarlinWeights`（Marlin 专家层）→ `KimiOneTokenForwardCache`（前向类型化层），以及 `KimiRouterGpuWeights` 作为 raw→typed 的过渡视图。
3. 解释 `KimiRankGpuContext` 为什么「每 rank 一个 context/stream 对」，以及它如何被 rank worker 线程绑定。
4. 知道加载链路上每一步失败的报错如何带上 rank 与张量名上下文，方便定位坏权重。
5. 会用无 GPU 的纯 CPU 单测（`weights/tests.rs`）验证加载计划的正确性。

## 2. 前置知识

- **safetensors 布局**：每个 `.safetensors` 分片 = 文件头的 JSON 元数据（张量名 → dtype、shape、字节偏移）+ 连续的二进制数据段。张量数据按行主序（row-major）紧密排列。`model.safetensors.index.json` 的 `weight_map` 把每个张量名指到它所在的分片文件（u2-l1 已解析它）。
- **mmap（内存映射文件）**：操作系统把文件「映射」进进程地址空间，读文件变成读内存，页缓存由内核管理。对几 GiB 的分片，mmap 避免了「先整个读进 Vec」的双份内存与长尾 IO。
- **H2D / D2D 拷贝**：H2D（host-to-device）把主机内存字节拷入显存；D2D（device-to-device）在显存内部搬字节（Marlin 打包用的就是 D2D，见 u2-l3）。
- **CUDA context / stream**：context 是一块 GPU 上的执行环境（含 cuBLAS 句柄等），stream 是提交工作的队列。CUDA context 与线程绑定，跨线程使用前要显式 `bind_to_thread`。
- **承接前讲**：u2-l2 给出每个 rank 的切片计划 `KimiRankSlicedLoadPlan`（按 shard 分组的 `KimiTensorLoadSpec` 列表）；u2-l3 讲了 INT4 专家权重如何从 checkpoint 布局重排为 Marlin 布局并融合 w13。本讲把它们接到磁盘和显存上。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/weights/load.rs` | 切片加载主循环 `load_rank_sliced_weights_to_gpu`、`KimiTensorLoadSlice` 切片语义、`sliced_tensor_bytes` 字节抽取、`mmap_file` |
| `src/weights/context.rs` | `KimiRankGpuContext`：每 rank 的 CUDA context/stream/设备号封装 |
| `src/weights/package.rs` | 三层权重包结构定义、raw→typed 拷贝、流式 Marlin 打包入口、清理与校验 |
| `src/runner/worker/load.rs` | `load_layer_forward_cache`：把 raw 张量整理成前向用的层缓存并做形状检查 |
| `src/runner/worker/cache.rs` | `KimiOneTokenForwardCache::from_gpu_weights`：顶层 embedding/norm/lm_head + 逐层缓存组装 |
| `src/runner/worker/state.rs` | `load_sliced_weights`：rank 线程内的加载编排（权重 → 前向缓存 → KV 池 → arena → 报告） |
| `src/runner/worker.rs` | `KimiRankWorker::spawn` / `load_sliced_weights_async` / `KimiRankWeightLoadReport` 与前向缓存结构体 |
| `src/runner/bringup.rs` | `maybe_load_rank_weights`（8 rank 并行加载与汇合）、`ensure_weight_payload_available` 预检 |
| `src/weights/tests.rs` | 纯 CPU 单测：切片计划、列切片字节布局、缺名报错 |

模块树回顾（u1-l3）：`weights.rs` 是 `crate::weights` 的模块根，`weights/` 目录里是 `context` / `load` / `manifest` / `package` / `tests` 五个实现模块，见 [src/weights.rs:64-83](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights.rs#L64-L83)。

## 4. 核心概念与源码讲解

### 4.1 切片加载与 H2D：load_rank_sliced_weights_to_gpu 主循环

#### 4.1.1 概念说明

上一讲的产物是「计划」：每个 rank 拿到一份 `KimiRankSlicedLoadPlan`——按 shard 分组的张量清单，每个条目声明「读哪个张量的哪一段」。本模块回答：**这份计划如何变成显存里的字节**。

关键设计有三个：

1. **逐 shard 流式处理**：一次只 mmap 一个分片，处理完它的全部张量就进入下一个，不把 64 个分片同时映射。
2. **Full 与切片两条通路**：整张量直接从 mmap 视图 H2D；行/列切片先在主机端抽取成连续 `Vec<u8>` 再 H2D（一次额外的主机内存拷贝换取显存里直接可用）。
3. **加载与 Marlin 打包交错**：每处理完一个 shard 就尝试打包已凑齐的 MoE 层专家权重并删除 raw 副本——这是 u2-l3「压住峰值显存」的落点。

#### 4.1.2 核心流程

```text
load_rank_sliced_weights_to_gpu(ctx, model_path, load_plan, names)
  ├─ ctx.set_current()                        # 绑定 CUDA context 到当前线程
  ├─ 校验 load_plan.rank == names.rank        # 计划与名字表必须同 rank
  ├─ for shard in load_plan.shards:           # 逐分片
  │    ├─ mmap_file(shard)                    # 只读映射
  │    ├─ SafeTensors::deserialize(&mmap)     # 解析文件头，得到零拷贝张量视图
  │    ├─ for spec in shard.tensors:
  │    │    ├─ view = safetensors.tensor(spec.name)      # 缺名 → 报错带名字+路径
  │    │    ├─ shape/bytes = spec.slice.local_shape/local_bytes(...)
  │    │    ├─ Full    → ctx.stream().clone_htod(view.data())      # mmap 直达显存
  │    │    ├─ 切片    → sliced_tensor_bytes(...) 先抽取 → clone_htod # 主机中转
  │    │    └─ 插入 weights.tensors（BTreeMap），重名即报错
  │    ├─ weights.pack_loaded_expert_marlin_layers(...)  # 凑齐的 MoE 层立即打包+删 raw
  │    └─ 记录最慢 shard（观测用）
  ├─ 收尾校验：张量数 == plan.tensor_count；MoE 层数 == 60
  ├─ validate_non_expert_typed_view(names)    # 非专家张量逐个验 dtype
  ├─ 组装 KimiRankExpertMarlinWeights（60 层）
  ├─ ctx.sync()                               # 等 H2D 全部落定
  └─ 返回 {weights, expert_kernel_weights, loaded_tensor_count, loaded_total_bytes}
```

切片的本地字节数由公式给出：

\[
\text{local\_bytes} = \prod \text{local\_shape} \times \text{dtype\_element\_bytes}
\]

其中 `RowRange` 取 \([\,\text{end}-\text{start},\ \text{cols}\,]\)，`ColRange` 取 \([\,\text{rows},\ \text{end}-\text{start}\,]\)。

#### 4.1.3 源码精读

先看三份数据结构——切片、规格、计划与输出（u2-l2 从 manifest 侧讲过前两者，这里看它们在 load 侧的完整定义）：

- [src/weights/load.rs:5-38](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L5-L38)：`KimiTensorLoadSlice` 枚举（`Full` / `RowRange` / `ColRange`），`local_shape` 校验切片区间合法并推导本地形状，`local_bytes` 算本地字节数。
- [src/weights/load.rs:40-58](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L40-L58)：`KimiTensorLoadSpec`（名字 + shard + 切片）、`KimiShardTensorLoadPlan`（一个 shard 的一组 spec）、`KimiRankSlicedLoadPlan`（rank + shards + tensor_count）。
- [src/weights/load.rs:60-65](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L60-L65)：`KimiRankSlicedLoadOutput` 是本函数的产出——**非专家** raw 权重包 + **专家** Marlin 包 + 两个统计量。

主循环入口与准备（绑定设备、rank 一致性、累加器、起始日志）：

- [src/weights/load.rs:75-103](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L75-L103)：函数签名与开头。`ctx.set_current()` 把 CUDA context 绑到当前 worker 线程；`ensure!(load_plan.rank == names.rank)` 让后续任何错配都能定位到 rank。

分片循环核心——mmap、取视图、两条 H2D 通路、登记：

- [src/weights/load.rs:105-114](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L105-L114)：打开并 mmap 分片，`SafeTensors::deserialize` 解析文件头；取不到张量时报错带「张量名 + 分片路径」。
- [src/weights/load.rs:115-128](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L115-L128)：`Full` 走 `ctx.stream().clone_htod(view.data())`（mmap 字节直达显存）；切片先 `sliced_tensor_bytes` 抽取再 H2D。两条支路的失败信息都带张量名。
- [src/weights/load.rs:129-145](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L129-L145)：构造 `KimiGpuRawTensor { name, dtype, shape, bytes, data }`，累加两个字节计数器（`weights.total_bytes` 与 `loaded_total_bytes`，二者之后会分叉），`BTreeMap::insert` 的返回值用于确保**没有重名张量**——重复即计划有错，fail fast。

流式 Marlin 打包的调用点（细节在 u2-l3，本讲看时机）：

- [src/weights/load.rs:146-156](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L146-L156)：每个 shard 处理完就调用 `pack_loaded_expert_marlin_layers`，并顺手记录最慢 shard。

收尾：对账、类型视图校验、组装专家包、同步、汇总日志：

- [src/weights/load.rs:158-178](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L158-L178)：张量数必须等于计划数、MoE Marlin 层数必须等于 60；`validate_non_expert_typed_view` 逐个核对非专家张量的 dtype；最后把 60 层 `KimiMoeLayerExpertMarlinWeights` 组装进 `KimiRankExpertMarlinWeights`。
- [src/weights/load.rs:179-204](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L179-L204)：`ctx.sync()` 等待全部异步 H2D 完成；汇总 debug 日志一次性打印 `loaded_tensors / loaded_bytes / resident_raw_bytes / expert_package_bytes / packed_moe_layers / slowest_shard`——这是本讲综合实践要对账的那行日志。

字节抽取 `sliced_tensor_bytes` 与 mmap、dtype 支持：

- [src/weights/load.rs:213-237](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L213-L237)：`RowRange` 是行主序下的连续区间，直接 `data[start_byte..end_byte].to_vec()`。
- [src/weights/load.rs:238-256](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L238-L256)：`ColRange` 在内存中**不连续**，必须逐行抽取 \( \text{rows} \) 段再拼紧成 \( \text{rows} \times \text{local\_cols} \) 的行主序缓冲——这就是「一次主机端 gather」的来源。
- [src/weights/load.rs:260-266](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L260-L266)：`mmap_file`：只读打开 + `Mmap::map`，SAFETY 注释说明映射只在读元数据和拷贝张量字节期间存活。
- [src/weights/load.rs:268-275](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L268-L275)：加载器只认 BF16/F32/I32/U8 四种 dtype，遇到别的（如 F16 权重）立即报错。

#### 4.1.4 代码实践：从 index.json 推导一个 rank 要打开的 shard 文件集合

**实践目标**：不启动引擎，只凭 `model.safetensors.index.json` 和 u2-l2 的切分规则，推导任一 rank 会 mmap 哪些分片文件（去重后的文件数），为后面的 GPU 对账做准备。

**操作步骤**：

1. 取一份真实 Kimi-K2 权重目录（含 `model.safetensors.index.json`），或先手写一个只含 `weight_map` 的最小 JSON 做流程验证。
2. 对 rank \( r \)（EP8），本地专家区间是 \( [\,48r,\ 48r+48\,) \)；名字集合 = 顶层 3 张量 + 每层 9 个注意力名 + 第 0 层 3 个 dense MLP 名 + 60 个 MoE 层的「router 2 + 共享专家 3 + 48 个专家 × 3 投影 × 2 张量」。注意 **`weight_shape` 永不被加载**（见 4.5 的 #234 注释）。
3. 运行下面脚本（**示例代码**，非项目自带）：

```python
#!/usr/bin/env python3
# 示例代码：推导 rank r 需要打开的 shard 文件集合
import json, sys

index_path, rank, world = sys.argv[1], int(sys.argv[2]), 8   # EP8
wm = json.load(open(index_path))["weight_map"]
LOCAL = 384 // world                       # 每 rank 本地专家数 = 48
lo, hi = rank * LOCAL, (rank + 1) * LOCAL
P = "language_model.model."

need = [P + "embed_tokens.weight", P + "norm.weight",
        "language_model.lm_head.weight"]
ATTN = ("input_layernorm.weight q_a_proj.weight q_a_layernorm.weight "
        "q_b_proj.weight kv_a_proj_with_mqa.weight kv_a_layernorm.weight "
        "kv_b_proj.weight o_proj.weight post_attention_layernorm.weight").split()
for l in range(61):
    need += [f"{P}layers.{l}.self_attn.{n}" for n in ATTN]
    if l == 0:
        need += [f"{P}layers.0.mlp.{p}.weight"
                 for p in ("gate_proj", "up_proj", "down_proj")]
    else:
        m = f"{P}layers.{l}.mlp."
        need += [m + "gate.weight", m + "gate.e_score_correction_bias"]
        need += [m + f"shared_experts.{p}.weight"
                 for p in ("gate_proj", "up_proj", "down_proj")]
        for e in range(lo, hi):
            for p in ("gate_proj", "up_proj", "down_proj"):
                need += [f"{m}experts.{e}.{p}.weight_packed",
                         f"{m}experts.{e}.{p}.weight_scale"]

missing = [n for n in need if n not in wm]
assert not missing, f"index 缺名: {missing[:3]}..."
shards = sorted({wm[n] for n in need})
print(f"rank {rank}: tensors={len(need)} distinct_shards={len(shards)}")
for s in shards:
    print(" ", s)
```

**需要观察的现象**：

- `tensors` 应等于 **18135**（与 4.5 的单测断言一致：\( 3 + 61\times9 + 3 + 60\times(5 + 48\times3\times2) = 18135 \)）。
- 名字集合对 TP8/DP1 与 TP1/DP8 **完全相同**（两种拓扑只差切片范围，不差名字），所以 shard 文件集合也相同。

**预期结果**：`distinct_shards` 的具体数值取决于分片布局，**待本地验证**。直觉预判：HF 的 64 分片布局把注意力/归一化等「每 rank 都要」的张量撒满全部分片，因此大概率得到 64（即每个 rank 几乎要 mmap 每个分片，只是各取所需的小切片）。

#### 4.1.5 小练习与答案

**练习 1**：为什么每个 shard 处理完就调用一次 `pack_loaded_expert_marlin_layers`，而不是全部加载完再统一打包？
**答案**：控制峰值显存。INT4 专家 raw 张量在打包成 Marlin 缓冲后会立即删除（见 4.3），如果等全部加载完再打包，某段时间内「raw 全量 + Marlin 全量」双份驻留，80/96 GiB 卡直接 OOM。逐 shard 打包让 raw 专家权重的生命周期最短。

**练习 2**：`Full` 张量与切片张量的数据通路差在哪一步？
**答案**：`Full` 直接从 mmap 视图 H2D（`clone_htod(view.data())`）；切片张量先在主机端 `sliced_tensor_bytes` 抽取/拼紧成连续 `Vec<u8>`，再多一次 H2D。多出的那次主机内存拷贝换来的是显存里直接可用的连续布局。

**练习 3**：`ColRange` 切片为什么不能像 `RowRange` 那样一次 `to_vec`？
**答案**：safetensors 是行主序，列区间在内存中不连续；必须按行循环 \( \text{rows} \) 次，每次抽 \( \text{local\_cols} \times \text{elem\_bytes} \) 字节拼进输出缓冲（[src/weights/load.rs:238-256](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L238-L256)）。

### 4.2 rank GPU 上下文：KimiRankGpuContext

#### 4.2.1 概念说明

八张卡、八个 rank，每个 rank 的所有 GPU 工作必须发生在**自己的** context/stream 上，否则内核会串卡、同步会互踩。`KimiRankGpuContext` 就是「一块 GPU 的全部句柄」的封装：

- `ctx: Arc<CudaContext>` —— 该设备上的 CUDA 执行环境；
- `stream: Arc<CudaStream>` —— 默认工作流（加载与大部分前向都走它）；
- `device_ordinal` —— 设备号（rank 与设备一一对应，见 u3）。

它由 bringup 在 `spawn_workers` 里为每个 placement 创建（[src/runner/bringup.rs:325-328](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L325-L328)），随后被 move 进该 rank 的 worker 线程（[src/runner/worker.rs:295-307](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker.rs#L295-L307)）。CUDA context 是线程绑定的，所以每个会碰 GPU 的命令 handler 第一件事都是 `ctx.set_current()`（如 [src/runner/worker/state.rs:21-22](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/state.rs#L21-L22) 中 `enable_deepep` 与 [src/runner/worker/state.rs:41-42](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/state.rs#L41-L42) 中 `init_tp_comm` 的开头；`load_sliced_weights` 则经由 4.1 加载器开头的 `set_current` 完成）。

#### 4.2.2 核心流程

`KimiRankGpuContext::new(device_ordinal)` 的初始化序列：

```text
set_current_device(ordinal)      # ffi::cuda_set_device，失败带 cudaError 码
→ CudaContext::new(ordinal)      # 建/取 context，失败带设备号
→ disable_event_tracking()       # 关闭逐操作事件登记（加载期 H2D 成千上万次）
→ ctx.new_stream()               # 默认流，失败带设备号
→ ffi::cublas_init()             # cuBLAS 句柄（GEMM 都靠它）
```

对外提供四个动作：`set_current`（绑线程）、`as_device_context`（导出给 kernels 层的 `DeviceContext` 视图）、`auxiliary_device_context(role)`（在同一 context 上**另开一条流**做旁路工作）、`sync`（等默认流排空）。

#### 4.2.3 源码精读

- [src/weights/context.rs:4-8](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/context.rs#L4-L8)：结构体三字段。
- [src/weights/context.rs:10-13](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/context.rs#L10-L13)：手写 `unsafe impl Send/Sync`，SAFETY 注释写明理由——**每个 rank 拥有一个 context/stream 对，且只由该 rank 的 worker 线程驱动**。这是 u3-l2「每 rank 一个线程」模型的类型面表达。
- [src/weights/context.rs:16-35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/context.rs#L16-L35)：`new` 的完整初始化序列，每一步失败信息都带设备号。
- [src/weights/context.rs:37-45](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/context.rs#L37-L45)：`set_current` = 设 `cuda_set_device` + `ctx.bind_to_thread()`。
- [src/weights/context.rs:47-67](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/context.rs#L47-L67)：`as_device_context` 把 `Arc` 克隆进 kernels 层用的 `DeviceContext`；`auxiliary_device_context(role)` 复用同一 `ctx`、新开一条流，role 只用于报错文案。
- [src/weights/context.rs:69-71](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/context.rs#L69-L71)：`stream()` 访问器的可见性收窄到 `pub(in crate::weights)`——只有 weights 子树能直接拿流，前向侧一律走 `as_device_context`（u1-l3 讲过的可见性分级实例）。
- [src/weights/context.rs:82-86](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/context.rs#L82-L86)：`sync` 只同步默认流，报错带设备号。

#### 4.2.4 代码实践：数一数一次启动创建了多少 CUDA 资源

1. **实践目标**：用只读搜索确认「每 rank 一 context + 一默认流 + N 条辅助流」的资源模型。
2. **操作步骤**：在 crate 根目录运行
   - `rg -n "KimiRankGpuContext::new" src/` —— 确认唯一的创建点在 `spawn_workers`；
   - `rg -n "auxiliary_device_context" src/` —— 列出所有辅助流的使用者及 role 名；
   - `rg -n "set_current\(\)\?" src/runner/worker` —— 数一数有多少个命令 handler 以绑定 context 开头。
3. **需要观察的现象**：`KimiRankGpuContext::new` 只出现一次（bringup）；`set_current` 出现在 `load_sliced_weights`、`init_tp_comm`、`enable_deepep`、`ensure_decode_arena` 等每个会碰 GPU 的 handler 里。
4. **预期结果**：能画出「context 层级图」：每设备 1 个 context、1 条默认流 + 若干按 role 命名的辅助流，全部由 1 个 rank worker 线程驱动。**待本地验证**辅助流的具体 role 集合（随代码演进可能增减）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `KimiRankGpuContext` 要手写 `unsafe impl Send/Sync`？
**答案**：CUDA 句柄类型本身不携带线程安全信息，Rust 无法自动判定；安全性由架构保证——每 rank 一个 context/stream 对，且只被该 rank 的 worker 线程驱动（[src/weights/context.rs:10-13](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/context.rs#L10-L13) 的 SAFETY 注释）。

**练习 2**：`as_device_context` 与 `auxiliary_device_context` 的区别是什么？
**答案**：前者导出默认流视图；后者在同一 context 上新开一条流。同一 context 内的流共享地址空间与 cuBLAS 句柄，可以并行提交工作而不必再建 context（建 context 很贵）。

**练习 3**：`load_rank_sliced_weights_to_gpu` 末尾为什么要 `ctx.sync()`？
**答案**：H2D 拷贝是异步提交的；返回前同步一次，保证「权重已就绪」是事实而不是假设，也让汇总日志里的耗时真实（[src/weights/load.rs:179-191](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L179-L191)）。

### 4.3 GPU 权重包的层次：raw → Marlin → typed

#### 4.3.1 概念说明

加载结束后，一个 rank 的显存里住着**三包**东西，层次与用途严格分开：

| 层 | 结构 | 内容 | 生命周期 |
|---|---|---|---|
| 原始层 | `KimiRankGpuWeights` | `BTreeMap<名字, KimiGpuRawTensor>`：字节级驻留，记录 dtype/shape/bytes 但不解释 | 长期（专家部分加载期即被删） |
| Marlin 专家层 | `KimiRankExpertMarlinWeights` | 60 层 `{w13 融合, down}` 的 Marlin 布局缓冲 | 长期，MoE GEMM 直读（u2-l3） |
| 前向类型化层 | `KimiOneTokenForwardCache` | 每层 `{attention, Dense\|Moe}` 的 GEMM 就绪矩阵 + 顶层 embedding/norm/lm_head | 长期，前向主链路消费（u5） |

另有一个**过渡视图** `KimiRouterGpuWeights<'a>`：它只是对 raw 字典里两个张量（`gate_weight`、`e_score_correction_bias`）的借用，通过 `copy_to_device_weights` 一步物化为类型化的 `KimiRouterDeviceWeights`。

分层的目的：加载器只认「名字 + 字节」，前向只认「形状 + 类型」。中间这一跳把「checkpoint 是什么」与「kernel 要什么」解耦——错配的形状/dtype 在这一跳被拦下，而不是在第一次 GEMM 时炸出难以定位的段错误。

#### 4.3.2 核心流程

加载期（本讲主线）：

```text
BTreeMap<name, KimiGpuRawTensor>            # 4.1 的产出（raw 层）
   │ 每 shard 末尾：
   ├─ pack_loaded_expert_marlin_layers        # 凑齐 48 专家的 MoE 层 → u2-l3 四缓冲流水
   │    ├─ validate_local_expert_name_order    # 本地专家序号必须连续且对应全局区间
   │    ├─ pack gate/up/down → reorder → fuse w13
   │    └─ remove_packaged_routed_expert_raw_tensors   # 删 raw，total_bytes 回退
   └─ 收尾：validate_non_expert_typed_view    # 非专家张量逐个验 dtype
加载完成后（4.4 展开）：
   raw 字典 --copy_bf16_matrix/vec--> 类型化 DeviceMatrix/DeviceVec --> 前向缓存
   router 两个 raw --copy_to_device_weights--> KimiRouterDeviceWeights
```

字节记账的分叉是理解日志的关键：

\[
\text{loaded\_total\_bytes} = \text{weights.total\_bytes（清理后驻留 raw）} + \text{专家 raw（已删）}
\]

而 Marlin 打包「只换布局不压缩字节」（u2-l3），所以近似有 \(\text{loaded\_total\_bytes} \approx \text{resident\_raw\_bytes} + \text{expert\_package\_bytes}\)。

#### 4.3.3 源码精读

- [src/weights/package.rs:10-22](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L10-L22)：raw 层两结构。`KimiGpuRawTensor` 的 `data: CudaSlice<u8>` 是「显存里的裸字节」，dtype 只是记录。
- [src/weights/package.rs:24-32](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L24-L32)：`KimiRouterGpuWeights<'a>`（借用视图）与 `KimiRouterDeviceWeights`（类型化产物：`GpuWeight<384, 7168>` + `CudaSlice<f32>`）。注意 router **不量化、不参与 Marlin 打包**。
- [src/weights/package.rs:122-134](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L122-L134)：Marlin 专家层两级结构：每层 `{w13, down, total_bytes}`，rank 级再挂 `local_expert_range` 与 60 层列表。
- [src/weights/package.rs:230-259](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L230-L259)：流式打包入口。`packed_layers: BTreeSet` 防重复打包；`has_all_routed_expert_raw` 保证「48 个专家 × 3 投影 × 2 张量全齐」才打包——即使一个 MoE 层的张量散落在多个 shard，也能在凑齐的那一刻打包。
- [src/weights/package.rs:337-374](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L337-L374)：删除已打包层的 raw 张量，`total_bytes` 同步回退，且有「删除量 ≤ 总量」的防御校验——清理逻辑出错不会静默。
- [src/weights/package.rs:189-218](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L189-L218)：`validate_non_expert_typed_view` 按类型化名字表逐个 `expect_tensor`：顶层 3 张量、每层 9 个注意力、dense 3、MoE 的 router（gate=BF16、bias=**F32**）与共享专家 3。这是「名字表（u2-l2）↔ 显存字典」的最终对账。
- [src/weights/package.rs:376-388](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L376-L388)：`expect_tensor` 的两类报错都带名字：缺名（"missing Kimi GPU tensor {name}"）与 dtype 不符。
- [src/weights/package.rs:145-187](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L145-L187)：raw→typed 的三个拷贝助手 `copy_bf16_matrix / copy_bf16_matrix_from_shape / copy_bf16_vec`，各自先 `validate_raw_tensor` 再 `copy_raw_tensor_to_typed`。
- [src/weights/package.rs:650-679](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L650-L679)：`copy_raw_tensor_to_typed<T>` 把 raw 字节 D2D 进类型化缓冲，字节数必须整除元素大小，报错带张量名——raw 层向 typed 层迁移的统一通道。
- [src/weights/package.rs:765-794](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L765-L794)：`validate_raw_tensor` 三连校验：dtype、shape、以及 \( \text{bytes} = \prod \text{shape} \times \text{elem\_bytes} \)。
- [src/weights/package.rs:415-445](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L415-L445)：`KimiRouterGpuWeights::copy_to_device_weights`：先验形状（gate 必须是 `[384, 7168]` BF16，bias `[384]` F32），再物化。router 权重是**全量**（不随 EP 切）——每个 rank 都要能给任意专家打分（路由语义详见 u6-l1）。

#### 4.3.4 代码实践：手算一份「字节对账表」

1. **实践目标**：用常量手算一个 rank 的专家权重字节量，验证 \(\text{loaded} \approx \text{resident} + \text{package}\) 的记账关系。
2. **操作步骤**：按 u2-l3 的形状规则计算（以 gate 投影为例，checkpoint 布局 \([2048, 7168]\)，组大小 32）：
   - `weight_packed`：I32，形状 \([2048,\ 7168/8]\)，字节数 \( 2048 \times 896 \times 4 = 7{,}340{,}032 \)；
   - `weight_scale`：BF16，形状 \([2048,\ 7168/32]\)，字节数 \( 2048 \times 224 \times 2 = 917{,}504 \)；
   - 单投影合计 \( 8{,}257{,}536 \) B = **7.875 MiB**；down 投影形状互换但字节数相同；单个专家 \( 3 \times 7.875 = 23.625 \) MiB；一个 MoE 层本 rank 48 个专家 ≈ **1.108 GiB**。
3. **需要观察的现象**：在有 GPU 的机器上以 `RUST_LOG=debug` 启动引擎（或第 5 节的报告二进制），抓取 [src/weights/load.rs:193-204](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L193-L204) 的日志行，读出 `loaded_bytes / resident_raw_bytes / expert_package_bytes`。
4. **预期结果**：`expert_package_bytes` 应接近手算的 \( 60 \times 1.108\,\text{GiB} \approx 66.5\,\text{GiB} \)（数值口径见 u2-l3 的融合后 w13 字节数）；`loaded_bytes - resident_raw_bytes` 应≈ `expert_package_bytes`。精确等式**待本地验证**（Marlin 重排是否引入填充以实际日志为准）。

#### 4.3.5 小练习与答案

**练习 1**：`loaded_total_bytes` 与 `weights.total_bytes` 为什么会在加载结束后不相等？
**答案**：`loaded_total_bytes` 只增不减（累计 H2D 的原始字节）；`weights.total_bytes` 在删除已打包专家 raw 时同步回退（[src/weights/package.rs:365-371](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L365-L371)）。差值就是被 Marlin 包取代的专家 raw 字节。

**练习 2**：`BTreeMap::insert(...).is_none()` 这个断言防的是什么错？
**答案**：同一张量名出现两次——要么 index 解析出重复条目，要么加载计划构造有 bug。fail fast 并在报错里带上名字和 rank（[src/weights/load.rs:139-144](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L139-L144)）。

**练习 3**：`KimiRouterGpuWeights` 为什么设计成带生命周期的借用结构，而不是直接存拷贝？
**答案**：它只是「raw 字典 → 类型化权重」之间的一跳视图，避免在同构数据上多复制一份；真正长期持有的是 `KimiRouterDeviceWeights`（前向缓存里）。

### 4.4 层前向缓存构建：从 raw 张量到可前向的层缓存

#### 4.4.1 概念说明

raw 字典是「按 checkpoint 的粒度」组织的（q_a、kv_a 分开存，gate/up 分开存）；但 decode 前向希望**一次 GEMM 干完一串下投影**。`load_layer_forward_cache` 负责这最后一跳：

- 把 `q_a_proj [1536,7168]` 与 `kv_a_proj_with_mqa [576,7168]` **vstack 融合**成 `fused_qkv_a_proj [2112,7168]`（\( 1536 + 576 = 2112 \)）；
- 把 dense 层与共享专家的 `gate/up` vstack 成 `gate_up`；
- 归一化权重物化为按常量定维的 `NormWeight<CONST>`；
- 每一步都先 `validate_raw_tensor`，形状对不上模型几何立即报错并指出期望值。

顶层（embedding/final norm/lm_head）与逐层缓存的组装在 `KimiOneTokenForwardCache::from_gpu_weights`；整个流程由 rank 线程的 `load_sliced_weights` 编排，顺带急切分配 KV 页池（OOM 应该炸在启动而不是第一个请求）。

#### 4.4.2 核心流程

```text
state.load_sliced_weights(model_path)                # rank worker 线程内
  ├─ load_rank_sliced_weights_to_gpu(...)            # 4.1：raw + Marlin 两包
  ├─ KimiOneTokenForwardCache::from_gpu_weights(...) # 本节
  │    ├─ token_embedding / lm_head : [vocab_rows, 7168]（vocab_rows 随拓扑：TP8=20480，TP1=163840）
  │    ├─ final_norm : NormWeight<7168>
  │    └─ for layer in names.layers: load_layer_forward_cache(...)
  │         ├─ attention：q_a + kv_a → vstack → fused_qkv_a_proj [2112, 7168]
  │         │    + 4 个 NormWeight + q_b / kv_b / o_proj 三个矩阵
  │         └─ Dense → gate/up vstack → gate_up；Moe → router + 共享专家 vstack
  ├─ KimiWorkerKvPool::new(...)                       # 急切分配 61 层 KV 页池
  ├─ KimiWorkerDecodeArenas::new(...)                 # 预登记 7 档批大小桶（惰性填充）
  └─ KimiRankWeightLoadReport + 存入 self.loaded
```

#### 4.4.3 源码精读

- [src/runner/worker/load.rs:3-35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/load.rs#L3-L35)：函数签名 + MLA 下投影融合。`q_a_proj` 行数必须是 `KIMI_K2_Q_LORA_RANK`（1536）、`kv_a_proj_with_mqa` 行数必须是 `KIMI_K2_MLA_KV_A_OUT`（576，= kv_lora 512 + k_rope 64，常量定义在 [pegainfer-kernels 的 ops/kimi_k2/mla.rs:19-23](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/kimi_k2/mla.rs#L19-L23)），随后 `DeviceMatrix::vstack` 融合。
- [src/runner/worker/load.rs:36-72](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/load.rs#L36-L72)：`KimiAttentionForwardCache` 的九件套组装：4 个按常量定长的 `NormWeight`（7168/1536/512/7168）+ 融合下投影 + `q_b_proj / kv_b_proj / o_proj` 三个矩阵（本 rank 的头切片，见下表）。
- [src/runner/worker/load.rs:74-115](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/load.rs#L74-L115)：`Dense` 与 `Moe` 两个分支。Dense：gate/up 校验后 vstack 成 `gate_up_proj`；MoE：先物化 router（借用视图 → `KimiRouterDeviceWeights`），再对共享专家做与 dense 完全相同的形状校验（`ensure_dense_mlp_shapes("shared_expert", ...)`——共享专家本质上就是一个"每层都激活"的小 MLP）与 vstack。**路由专家不在这里**——它们在 4.3 的 Marlin 包里。
- [src/runner/worker/load.rs:117-132](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/load.rs#L117-L132)：返回 `KimiLayerForwardCache { layer_idx, attention, kind }`；`raw_tensor` 查不到名字时报错带名字（"missing Kimi forward tensor {name}"）。
- [src/runner/worker/load.rs:134-163](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/load.rs#L134-L163)：`ensure_dense_mlp_shapes` 三条断言：gate 输入维必须是 7168；up 与 gate 同形；down 是 \([7168,\ \text{gate.rows}]\) 的反向投影。
- [src/runner/worker/cache.rs:5-62](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/cache.rs#L5-L62)：`from_gpu_weights`。先做 rank/层数一致性检查，再装顶层三件（`vocab_rows = names.plan.vocab_range.len()`），`vocab_start` 也记进缓存——词表分片后，全局 token id = `vocab_start + 本地 argmax`。
- [src/runner/worker/state.rs:51-83](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/state.rs#L51-L83)：`load_sliced_weights` 前半段：调 4.1 的加载器（外层再包一层「failed to load Kimi rank {} sliced weights to GPU」的上下文），随后构建 one-token 前向缓存。
- [src/runner/worker/state.rs:88-112](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/state.rs#L88-L112)：**急切分配 KV 页池**，注释写明理由：「OOM should kill bringup, not the first request that fills the pool」；decode arena 桶只在此登记、按需填充。
- [src/runner/worker/state.rs:113-146](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/state.rs#L113-L146)：组装 `KimiRankWeightLoadReport`、存入 `self.loaded`，末尾再对账两个计数（报告层数 == 实际 Marlin 层数）并打汇总日志（`tensors=..., bytes=..., expert_layers=...`）。
- [src/runner/worker.rs:574-614](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker.rs#L574-L614)：前向缓存的结构体定义总览（`KimiOneTokenForwardCache` / `KimiLayerForwardCache` / `KimiAttentionForwardCache` / `KimiLayerForwardKindCache::{Dense,Moe}`）。
- [src/runner/worker.rs:174-183](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker.rs#L174-L183)：`KimiRankWeightLoadReport` 的字段——`tensor_count / total_bytes / expert_kernel_layers / expert_kernel_total_bytes` 加三个布尔（加载、类型视图校验、打包完成），是 bringup 侧观测加载结果的标准载体。

**TP8 下每 rank 的注意力/MLP 矩阵形状速查**（由 u2-l2 的切片区间 + 本节的形状断言共同确定，可用 4.5 的单测数字核对）。注意 `kv_b_proj` 的输出同时包含 nope-k 与 v 两部分：全量行 = \( 64 \times (128 + 128) = 16384 \)：

| 张量 | 全量形状 | 本 rank（TP8）切片 |
|---|---|---|
| `fused_qkv_a_proj`（融合后） | \([2112, 7168]\) | 不切（每 rank 全量） |
| `q_b_proj` | \([12288, 1536]\) | 行切 → \([1536, 1536]\) |
| `kv_b_proj` | \([16384, 512]\) | 行切 → \([2048, 512]\) |
| `o_proj` | \([7168, 8192]\) | 列切 → \([7168, 1024]\) |
| dense `gate_up_proj`（融合后） | \([2\times18432, 7168]\) | 行切 → \([4608, 7168]\) |
| shared `gate_up_proj`（融合后） | \([2\times2048, 7168]\) | 行切 → \([512, 7168]\) |
| `token_embedding` / `lm_head` | \([163840, 7168]\) | 行切 → \([20480, 7168]\)（约 280 MiB BF16 each） |

#### 4.4.4 代码实践：推导并核对前向缓存形状表

1. **实践目标**：不跑代码，从 `config.rs` 常量推出上表每一行，再用单测断言核对。
2. **操作步骤**：
   - 常量来源：64 头、nope 128、rope 64、v_head 128（u1-l2）→ `q_b` 全量行 = \( 64\times(128+64)=12288 \)、`kv_b` 全量行 = \( 64\times(\text{nope }128 + \text{v }128)=16384 \)、`o_proj` 全量列 = \( 64\times128=8192 \)；
   - 对照 [src/weights/tests.rs:89-120](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L89-L120)：rank 3 的 `q_b_proj` 切片 `4608..6144`（= \( 3\times1536 \) 起）、`kv_b_proj` `6144..8192`（= \( 3\times2048 \) 起）、`o_proj` 列切 `3072..4096`（= \( 3\times1024 \) 起）；
   - 推演：若把 TP 从 8 改成 4（假想），`q_b` 本地行数变为 3072、`o_proj` 本地列数 2048、`kv_b` 本地行数 4096，其余全量项不变。
3. **需要观察的现象**：手算区间与单测里的 `start/end` 逐项吻合；`start = rank × local`，`end = start + local`。
4. **预期结果**：形状表全部对上；若某行对不上，说明对「行切（输出维）还是列切（输入维）」判断有误——`o_proj` 是列切因为它的输入是注意力输出（按头切），`q_b/kv_b` 是行切因为它们的输出是头。

#### 4.4.5 小练习与答案

**练习 1**：为什么要把 `q_a_proj` 与 `kv_a_proj_with_mqa` vstack 成一个矩阵？
**答案**：decode 单 token 时一次 GEMM 就得到完整的 `qkv_a`（2112 行），比两次 GEMM 少一次 kernel 启动与一次中间缓冲读写；这是 u5-l1/u5-l2 前向链路里 `fused_qkv_a_proj` 直接被消费的原因。

**练习 2**：词表分片后，本地 lm_head 输出怎么变成全局 token id？
**答案**：缓存里记了 `vocab_start`（[src/runner/worker/cache.rs:55-56](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/cache.rs#L55-L56)），全局 id = `vocab_start + 本地 argmax`；TP8 下各 rank 的局部结果还需跨 rank 合并成全局选择（具体聚合方式见 u5-l1 的前向链路）。

**练习 3**：路由专家权重为什么不在 `load_layer_forward_cache` 里？
**答案**：它们走的是 INT4 → Marlin 的独立打包管线（4.3 / u2-l3），产出 `KimiRankExpertMarlinWeights`，由 MoE 后端直接零拷贝消费（u6）；前向缓存里只有 router 与共享专家这两个 BF16 部分。

### 4.5 加载测试：无 GPU 也能验证的纯 CPU 测试模式

#### 4.5.1 概念说明

权重加载最容易出错的不是 CUDA，而是「名字 → 切片 → 字节」的纯逻辑。所以本 crate 把这部分逻辑做成**不依赖 GPU 和权重文件**的单测：`tests.rs` 用 `tiny_manifest()` 在内存里伪造一份「全部张量都指向同一个假分片文件」的清单，然后断言加载计划的每一类切片与排除规则。真实分片一个字节都不用读。

#### 4.5.2 核心流程

```text
tiny_manifest()
  = 61 层假名字（第 0 层 Dense，其余 MoE，384 专家齐全）
    + parallel = KimiK2ParallelShape::tp8_ep8()
    + 全部 entry.shard = "model-00001-of-000064.safetensors"
rank_sliced_load_plan(rank) → 断言：
  · tensor_count == 18_135（比 checkpoint 少 8640：weight_shape 从不加载，#234）
  · embed/lm_head 行切 61440..81920（rank 3，词表 163840/8 = 20480）
  · q_b/kv_b 行切、o_proj 列切、dense gate 行切、shared down 列切
  · 专家 143 缺席、144..191 全量、192 缺席（rank 3 本地区间 144..192）
sliced_tensor_bytes(ColRange) → 断言逐字节输出（行主序拼紧）
from_index_json 缺名 → 断言报错文案含缺失张量名
```

#### 4.5.3 源码精读

- [src/weights/tests.rs:62-68](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L62-L68)：rank 3 的张量总数断言 **18135**，注释点明比 checkpoint 少 8640 = 60 层 × 48 专家 × 3 投影的 `weight_shape`——它不参与任何 kernel，索性不加载（#234）。
- [src/weights/tests.rs:70-98](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L70-L98)：`embed_tokens` / `lm_head` 的行切（61440..81920）与 `final_norm` 的 Full；`q_b_proj`（4608..6144）与 `kv_b_proj`（6144..8192）的行切——这三个数字就是 4.4.4 实践的答案锚点。
- [src/weights/tests.rs:99-142](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L99-L142)：`o_proj` 的**列切**（3072..4096）、dense `gate_proj` 行切（6912..9216，= \( 3\times2304 \) 起）、共享专家 `down_proj` 列切（768..1024，= \( 3\times256 \) 起）。
- [src/weights/tests.rs:144-182](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L144-L182)：专家归属三连断言——非本地专家（143）不在计划、本地专家（144..191）全量、下一段（192）不在计划；`weight_shape` 必须缺席。
- [src/weights/tests.rs:185-195](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L185-L195)：列切片的逐字节断言：3×4 的 BF16 矩阵取列 1..3，输出恰为逐行抽取后的 12 字节——4.1 里那段 gather 循环的最小可读样例。
- [src/weights/tests.rs:198-207](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L198-L207)：只给 `embed_tokens` 一个张量的残缺 index，断言报错文案里出现缺失的 `language_model.model.norm.weight`——「报错必须点名」这一约定的测试化。
- [src/weights/tests.rs:223-298](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L223-L298)：`tiny_manifest` 与两个 fake 构造器——名字真实、shard 全部指向同一个假文件，`text_tensor_count` 也对齐真实 checkpoint 的 208215（u2-l1 的数字）。

#### 4.5.4 代码实践：跑通这组纯 CPU 测试

1. **实践目标**：在本地验证加载计划的切片规则（不需要 GPU 与权重，但需要 CUDA 工具链来编译 `pegainfer-kernels`，因为 `kimi-k2` feature 会传导 CUDA 编译，见 u1-l4）。
2. **操作步骤**：

```bash
# 在仓库根目录（需要 nvcc；不需要 GPU 设备与模型权重）
cargo test --release -p pegainfer-kimi-k2 --features kimi-k2 --lib weights:: -- --nocapture
```

3. **需要观察的现象**：5 个测试全部通过；`--nocapture` 下能看到断言失败时打印的具体切片区间（若有失败）。
4. **预期结果**：`rank_tensor_names_filter_local_experts`、`rank_weight_names_are_local_and_typed`、`rank_sliced_load_plan_applies_tp8_ep8_slices`、`sliced_tensor_bytes_packs_col_slice_as_row_major`、`scanner_rejects_missing_required_text_tensor` 全绿。若本机没有 CUDA 工具链，此命令无法编译，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：这些测试为什么可以完全不碰 GPU？
**答案**：`manifest → rank_weight_names / rank_sliced_load_plan → sliced_tensor_bytes` 整条链路是纯名字与字节布局计算；只有 `load_rank_sliced_weights_to_gpu` 之后的部分才需要 CUDA context（[src/weights/tests.rs:1-17](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L1-L17) 的 import 清单里没有任何 CUDA 符号）。

**练习 2**：18135 这个数怎么来的？
**答案**：\( 3\,(\text{顶层}) + 61\times9\,(\text{注意力}) + 3\,(\text{dense MLP}) + 60\times(5 + 48\times3\times2)\,(\text{MoE}) = 18135 \)。MoE 部分的 5 = router 2 + 共享专家 3；48 个专家各 3 投影 × 2 张量（packed + scale）。

**练习 3**：`scanner_rejects_missing_required_text_tensor` 防的是哪类真实事故？
**答案**：checkpoint 目录被截断或混入非文本（如 vision）模型时，index 缺关键张量。测试保证报错会**点名**第一个缺失的必需张量，而不是加载到一半才在 CUDA 层炸出无上下文的错误。

## 5. 综合实践：一次真实启动的「计划 vs 实况」对账

把本讲四个模块串起来：**纸上推导加载计划 → 观察真实加载日志 → 三处对账**。

**任务 A（无 GPU 也可完成）：计划侧推导**

1. 跑 4.1.4 的脚本，记录 rank 3 的 `tensors=18135` 与 `distinct_shards=N`。
2. 跑 4.5.4 的单测，确认切片区间与脚本假设一致。
3. 用 4.4.4 的形状表核对 `q_b/kv_b/o_proj/embed` 的切片方向（行切/列切）。

**任务 B（需 8 GPU + 权重 + CUDA 工具链）：实况侧观察**

`kimi_model_report` 的 runtime trace 会真正启动一次引擎（[src/batch_decode_trace.rs:119-136](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/batch_decode_trace.rs#L119-L136) 里调 `start_engine` 拉起 8 设备，权重加载随之发生）：

```bash
RUST_LOG=debug cargo run --release -p pegainfer-kimi-k2 \
  --features kernel-report --bin kimi_model_report -- \
  decode --batch-size 4 --kv-len 513 --model-path models/Kimi-K2
```

（`--batch-size/--kv-len` 是必填项且 `--kv-len > 1`；`--model-path` 不给时默认 `models/Kimi-K2.5`，见 [src/bin/kimi_model_report.rs:36-54](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/bin/kimi_model_report.rs#L36-L54)；bin 的 required-features 是 `kernel-report`，见 [Cargo.toml:42-50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L42-L50)。）

**三处对账**：

1. **张量数**：日志 `rank {r} weights loaded: tensors=...`（[src/runner/bringup.rs:300-305](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L300-L305)）应等于 18135；`expert_layers` 应等于 60（[src/runner/worker/state.rs:138-144](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/state.rs#L138-L144)）。
2. **字节记账**：`weight load cost` 日志（[src/weights/load.rs:193-204](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L193-L204)）里验证 `loaded_bytes ≈ resident_raw_bytes + expert_package_bytes`，并与 4.3.4 的手算对量级。
3. **shard 集合**：加载开始前 bringup 会先做预检 `ensure_weight_payload_available`（[src/runner/bringup.rs:411-429](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L411-L429)），缺分片会报「found X/Y planned shards」；对照任务 A 的 `distinct_shards=N`——若目录完整，Y 就是各 rank 计划的并集去重数。

**预期结果**：三处全部吻合即通过。任务 B 的具体数值随 checkpoint 与卡型变化，**待本地验证**。

## 6. 本讲小结

- **通路**：`load_rank_sliced_weights_to_gpu` 逐 shard mmap → 按切片规格取字节（Full 直达 / 切片先主机 gather）→ H2D → 显存 raw 字典；每个 shard 末尾流式打包凑齐的 MoE 层并删 raw，峰值显存被压住。
- **上下文**：`KimiRankGpuContext` 是「每 rank 一个 context/stream 对」的封装，由 bringup 创建、移交给 rank worker 线程，任何 GPU 操作前都要 `set_current` 绑线程。
- **三层权重包**：raw 字典（`KimiRankGpuWeights`）承载字节；Marlin 专家包（`KimiRankExpertMarlinWeights`）供 MoE GEMM 零拷贝；前向缓存（`KimiOneTokenForwardCache`）把 raw 整理成 vstack 融合、按常量定维的类型化矩阵；router 经借用视图一跳物化且不量化。
- **记账**：`loaded_total_bytes` 只增不减，`weights.total_bytes` 随专家 raw 删除回退，差值≈Marlin 包字节——日志三件套可对账。
- **报错纪律**：从缺名、切片越界、dtype 不符到 rank 不匹配，加载链路每一步的报错都带张量名或 rank，bringup 再包一层 rank 上下文。
- **测试**：切片计划、字节布局与排除规则全部有无 GPU 的纯 CPU 单测覆盖；`weight_shape` 从不加载（#234）是计划比 checkpoint 少 8640 个张量的原因。

## 7. 下一步学习建议

- **u3-l1（start_engine：双拓扑启动流程）**：本讲的 `maybe_load_rank_weights` 只是启动时序里的一环，下一讲看完整的 spawn_workers → 加载 → NCCL/DeepEP 集体初始化编排。
- **u3-l2（KimiRankWorker）**：本讲的 `load_sliced_weights` 是 worker 命令处理器之一，下一讲系统看「每 rank 一个线程 + 通道」的命令模型与绑核。
- **u5-l3（分页 KV 池）**：本讲在 state.rs 里急切分配的 `KimiWorkerKvPool`（61 层 × 页池）如何被 decode 使用，在 u5-l3 展开。
- 顺带阅读：[docs/models/kimi-k2/source-layout.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/kimi-k2/source-layout.md) 里 weights 部分的历史快照，对照本讲验证哪些描述仍然成立（u1-l3 教过的方法）。
