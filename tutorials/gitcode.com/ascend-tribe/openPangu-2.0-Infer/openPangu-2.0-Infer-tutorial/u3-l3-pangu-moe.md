# MoE 层实现与专家并行

## 1. 本讲目标

上一讲（u3-l1）我们看完了 `pangu_v2_moe.py` 的整体骨架，知道了 DecoderLayer 里挂着一个 `OpenPanguV2MOE`。本讲钻进这个「FFN 替身」的内部，读完本文你应当能够：

1. 说出 `OpenPanguV2MOE` 如何把路由门控（gate）、共享专家（shared experts）与路由专家（routed experts）组装成一个 `NPUSharedFusedMoE` 层，并解释「Shared/Fused（共享+融合）」到底融合了什么。
2. 跟踪一次 MoE 前向的完整链路：gate 打分 → top-k 选专家 → token 按专家重排（dispatch）→ 专家计算 → 加权聚合回收（combine）。
3. 解释专家并行（Expert Parallelism, EP）下三种通信策略 `all2all`、`agrs`、`dispatch_combine` 的差异、各自的通信量结构，以及 `CommunicationStrategySelector` 如何按设备型号与 token 数自动选择。
4. 理解 NPU 侧的融合算子体系（`npu_moe_gating_top_k`、`npu_moe_init_routing_v2`、`npu_grouped_matmul`、`npu_swiglu`、`npu_moe_finalize_routing`）以及 W8A8 量化、FRACTAL_NZ 权重布局等适配点。
5. 回答部署问题：`--enable-expert-parallel` 打开后，每个 NPU rank 的显存里到底躺着哪些专家权重。

## 2. 前置知识

### 2.1 MoE（Mixture of Experts）在算什么

稠密 FFN 对每个 token 都做同样一次 `SwiLU(gate_up(x)) · down` 变换。MoE 则把 FFN 复制成 \( E \) 份「专家」，再由一个很小的路由网络（router/gate）为每个 token 挑出最相关的 \( k \) 个：

\[
y = \underbrace{\mathrm{FFN}_{shared}(x)}_{\text{共享专家，所有 token 都走}} \;+\; \underbrace{\sum_{i \in \mathrm{TopK}(g(x))} g_i(x)\cdot \mathrm{FFN}_{i}(x)}_{\text{路由专家，按需激活}}
\]

- \( g(x) \)：gate 输出的每个专家得分（本模型用 `sigmoid` 打分并叠加修正偏置 `e_score_correction_bias`）。
- TopK：每个 token 只激活 \( k \) 个专家，计算量近似 \( k/E \)，但参数量仍是全量 —— MoE 是「参数大、计算省」的结构。
- 共享专家（shared expert）：不参与路由、每个 token 必经的稠密 FFN，负责承载公共知识；openPangu-2.0 中它同时是一个普通的 `OpenPanguV2MLP`。

### 2.2 张量并行（TP）与专家并行（EP）

- **TP**：把单个矩阵按列/行切到多卡，每卡都有「半个专家」，结果需要 AllReduce/ReduceScatter 汇总。
- **EP**：把**整个专家**分配到不同卡，每卡只持有 \( E/\text{EP} \) 个完整专家。坏处是 token 必须在网络里「寄快递」——发给持有目标专家的那张卡，算完再寄回来。这就是本讲的主角：dispatch（分发）与 combine（回收）。

两者可以叠加：openPangu 部署里 `--enable-expert-parallel` 之后，EP 组通常与 TP 组重合（单机 16 卡即 TP16=EP16）。

### 2.3 需要认识的通信原语

| 原语 | 语义 | 在 MoE 里的用途 |
|---|---|---|
| AllGather | 每 rank 各出一份，拼成全量 | 让每张卡看到全部 token（AGRS 策略的「A」） |
| ReduceScatter | 求和后按段切回各 rank | 把各卡算出的部分和落回自己的 token 段（AGRS 的「RS」） |
| AllToAll | 每个 rank 给每个 rank 发不同的数据 | 把 token 直接寄给目标专家所在的卡（all2all 策略） |
| AllReduce | 全量求和广播 | TP 共享专家输出合并 |

### 2.4 与前面讲义的衔接

- u3-l1 讲过 `load_weights` 的「四分支路由器」，其中专家分支最终落到本讲的 `weight_loader`。
- u2-l2 讲过的 out-of-tree 插件思想在这里再次出现：`@FusedMoE.register_oot` 装饰器把 NPU 子类登记为 vLLM MoE 层的替换实现。
- 本仓库不含 vLLM 源码（它是部署镜像里的 `vllm 0.14.0+empty` 空壳 + 本插件），所以凡涉及「vLLM 原生类长什么样」，讲义会给出在部署容器里现场查证的方法，而不是凭空断言。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py) | 模型侧：`OpenPanguV2MOE` 组装 gate/共享专家/`NPUSharedFusedMoE`，并自带三条手写通信前向 |
| [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py) | 插件侧：`NPUFusedMoE`、`NPUSharedFusedMoE`、`NPUUnquantizedFusedMoEMethod`（apply 主流程）与自定义 op 注册 |
| [components/omni-npu/src/omni_npu/layers/fused_moe/fused_moe.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/fused_moe.py) | 纯 TP（无 EP）路径：`fused_experts_tp` 用 torch_npu 路由算子一次完成本地全专家计算 |
| [components/omni-npu/src/omni_npu/layers/fused_moe/fused_moe_method_base.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/fused_moe_method_base.py) | `NPUFusedMoEMethodBase`：把「prepare_permute → apply_experts → unpermute_finalize」三段式抽象成可复用的方法基类 |
| [components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py) | EP 通信核心：三种策略实现 + `CommunicationStrategySelector` 自动选择 |
| [components/omni-npu/src/omni_npu/layers/fused_moe/config.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/config.py) | 给 vLLM 的 `FusedMoEQuantConfig` 动态补上 hifloat8/mxfp8 判定属性（量化适配小补丁） |
| [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py) | MoE 相关开关默认值（`moe_comm_strategy`、`enable_moe_agrs`、`gmm_nz` 等） |
| [components/omni-npu/tests/unit/layers/fused_moe/](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/layers/fused_moe/test_layer.py) | 无需 NPU 的单测（把 torch_npu/vllm 全部打桩），本讲多数实践的靶场 |

## 4. 核心概念与源码讲解

### 4.1 MoE 层的组装：OpenPanguV2MOE 与 NPUSharedFusedMoE

#### 4.1.1 概念说明

`OpenPanguV2MOE`（模型文件里定义）是「装配车间」，它在 `__init__` 里造出三样东西：

1. **gate**：一个 `ReplicatedLinear`（不切 TP、fp32 计算），把 hidden_size 映射到 \( E \) 个专家得分；旁边还挂一个可学习参数 `e_score_correction_bias`，用于 sigmoid 打分后的偏置修正（DeepSeek 风格的负载均衡手段）。
2. **shared_experts**：一个 `OpenPanguV2MLP`，`disable_tp=True` —— 共享专家**整卡复制、不切 TP**（u3-l1 讲过这一点）。
3. **experts**：一个 `NPUSharedFusedMoE` 实例，构造时把上面的 `shared_experts` 模块**塞进自己肚子里**。

「SharedFusedMoE（共享融合 MoE）」的「融合」体现在三层含义：

- **模块融合**：共享专家作为成员传入 MoE 层，一次 `forward` 调用同时返回 `(shared_output, routed_output)`，权重加载也统一走 MoE 层的映射表。
- **算子融合的机会**：回收阶段的融合算子 `npu_grouped_matmul_finalize_routing` 预留了 `shared_input` 参数，设计上允许把共享专家的输出合并进「反量化 + 加权聚合」同一个内核（当前代码传 `None`，源码留有 TODO）。
- **流水重叠**：共享专家可以放到独立 stream 上与路由专家并行算（4.2 详述）。

`NPUSharedFusedMoE` 本身代码极短 —— 它只是通过**多继承**组合了 vLLM 的 `SharedFusedMoE` 与本插件的 `NPUFusedMoE`，再补一个 `gate` 属性。

#### 4.1.2 核心流程

```text
OpenPanguV2DecoderLayer
 └─ mlp = OpenPanguV2MOE(config, ...)
     ├─ gate: ReplicatedLinear(hidden → E, fp32)      # 路由打分
     ├─ e_score_correction_bias: Parameter(E)          # 打分修正偏置
     ├─ shared_experts: OpenPanguV2MLP(disable_tp=True) # 共享专家（整卡复制）
     └─ experts: NPUSharedFusedMoE(                     # 路由专家容器
             shared_experts=shared_experts,             # ← 共享专家被「融合」进来
             num_experts=n_routed_experts,              # E
             top_k=num_experts_per_tok,                 # k
             use_grouped_topk=True, num_expert_group=1, topk_group=1,
             scoring_func="sigmoid",
             e_score_correction_bias=..., enable_eplb=..., ...)
```

注意一个容易忽略的细节：`NPUFusedMoE.__init__` 额外接收 `gate=None` 关键字参数并立刻调用 `quant_method.make_communication_strategy_selector(self)` —— 也就是说**门控网络可以被搬进 MoE 层内部**（`is_internal_router` 为 True），此时 `apply()` 会替你算 router logits（见 4.2）。

#### 4.1.3 源码精读

模型侧装配（精简注释）：

- [components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:234-244](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L234-L244) —— 构造 fp32 的 `ReplicatedLinear` gate 与 `e_score_correction_bias` 参数：路由打分不切 TP、用 float32 保证数值稳定。
- [components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:250-261](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L250-L261) —— 共享专家：`intermediate_size = moe_intermediate_size × n_shared_experts`，`disable_tp=True` 表示每张卡持完整副本、不做 TP 切分。
- [components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:275-294](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L275-L294) —— 实例化 `NPUSharedFusedMoE`，把 `shared_experts` 传入；`use_grouped_topk=True, num_expert_group=1, topk_group=1` 使分组 top-k 退化为普通 top-k；`scoring_func="sigmoid"` 对应后文算子的 `norm_type=1`。
- [components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:295-307](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L295-L307) —— 逻辑/物理专家数换算：开 EPLB 时物理专家数 = 逻辑专家数 + 冗余专家数×EP，每卡本地专家数 = 物理专家数 ÷ EP（4.5 详述）。

插件侧两个类：

- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:310-323](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L310-L323) —— `@FusedMoE.register_oot class NPUFusedMoE(FusedMoE)`：通过 vLLM 的 out-of-tree 注册装饰器登记为 NPU 实现；`__init__` 存下 `gate` 并创建通信策略选择器。
- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:483-488](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L483-L488) —— `NPUSharedFusedMoE(SharedFusedMoE, NPUFusedMoE)`：全部行为来自多继承，自身只重声明 `gate` 属性。

多继承的 MRO 顺序是 `NPUSharedFusedMoE → SharedFusedMoE → NPUFusedMoE → FusedMoE`：`SharedFusedMoE` 未覆写的方法会落到 `NPUFusedMoE` 的实现。哪些方法最终生效，可以在部署容器里用 `NPUSharedFusedMoE.__mro__` 与 `inspect.getsource` 现场查证（本讲综合实践就做这件事）。

#### 4.1.4 代码实践

**实践目标**：不用 NPU，验证「装配车间」的类结构知识与单测一致。

1. 操作步骤（在仓库根目录，需本地装有 `torch` 与 `pytest`；`conftest.py` 已把 `torch_npu`、`vllm` 全部打桩成内存替身）：

   ```bash
   cd components/omni-npu
   python -m pytest tests/unit/layers/fused_moe/test_layer.py::test_fused_moe_init_sets_gate_and_strategy_selector -q
   ```

2. 需要观察的现象：测试通过；它断言 `NPUFusedMoE(gate=...)` 之后 `layer.gate` 被保存、且 `quant_method` 上挂好了 `communication_strategy_selector`。
3. 预期结果：1 passed。桩环境细节见 [components/omni-npu/tests/unit/layers/fused_moe/conftest.py:26-90](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/layers/fused_moe/conftest.py#L26-L90)（`_stub_fused_moe_deps` fixture 把 `model_extra_config`、`vllm.distributed` 等替换成 `SimpleNamespace`）。
4. 若本地没有 torch/pytest 或跑不通，本条标注「待本地验证」，改为纯阅读：打开 [components/omni-npu/tests/unit/layers/fused_moe/test_layer.py:795-811](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/layers/fused_moe/test_layer.py#L795-L811) 阅读断言即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么共享专家要 `disable_tp=True`，而路由专家按 EP 分卡？

答案：共享专家每个 token 都要走，如果按 TP 切分，每张卡都只算一部分、还必须通信合并；它本身参数量小（只有 1 份 `moe_intermediate_size`），整卡复制的显存代价可接受，换来的是**零通信**。路由专家共 \( E \) 份、参数量占绝对大头，必须靠 EP 分摊显存，通信代价由 dispatch/combine 承担。

**练习 2**：`num_expert_group=1, topk_group=1` 时，`use_grouped_topk=True` 还有什么意义？

答案：分组 top-k（先选组再在组内选专家，DeepSeek-V3 的细粒度负载均衡手段）在「1 个组、组内也选 1 组」时数学上退化为普通 top-k。保留这个开关让同一份代码能兼容分组路由的模型，同时把实现统一交给 `npu_moe_gating_top_k` 一个算子（见 4.2.3）。

### 4.2 路由与分发：从 router logits 到按专家排序的 token

#### 4.2.1 概念说明

MoE 前向的核心矛盾：**token 天然按序列连续存放，而 grouped_matmul 要求同一专家的 token 在内存里连续**。所以必须有一步 permutation（重排）：按 `topk_ids` 把 token 复制/搬运成「按专家分组、组内连续」的布局；算完再按路由权重加权聚合回原顺序（unpermute/finalize）。EP 之下，重排还与「跨卡寄送」合成为同一个 dispatch/combine 问题。

插件侧把这件事规范成**三段式**：

```text
prepare_permute(x, topk_ids)      →  dispatch + 按专家排序 → PreparePermuteResult
apply_experts(prepare_permute_result) → grouped_matmul×2 + swiglu
unpermute_finalize(output, topk_weights, ...) → combine + 加权聚合
```

`NPUFusedMoEMethodBase` 是这三段式的抽象基类，任何 MoE 量化方法（如未量化的 `NPUUnquantizedFusedMoEMethod`）继承它即可复用通信骨架，只实现 `apply_experts`。

#### 4.2.2 核心流程

`NPUUnquantizedFusedMoEMethod.apply()` 的完整流程（伪代码）：

```text
apply(hidden_states, router_logits):
  1  strategy = select_communication_strategy(num_tokens)        # 4.3 详述
  2  若未开序列并行且 TP>1: pad 后按 tp_rank 切出本卡的 x_slice   # 每 rank 1/TP 的 token
  3  若 layer.gate 非空: router_logits = gate(x_slice)            # 门控在层内部
  4  topk_weights, topk_ids = select_experts(router_logits, k)    # npu_moe_gating_top_k
  5  若未开 EP (moe_parallel_config.use_ep == False):
  6      return fused_experts_tp(layer, x_slice, topk_ids, topk_weights)  # 本地全专家，见 4.2.3 末
  7  prepare_permute_result = apply_prepare_permute(strategy_impl, ...)   # dispatch + 排序
  8  output = apply_experts(...)                    # 可与 finalize 元数据 / 共享专家重叠
  9  shared_output = shared_experts(x 或 x_slice)   # 可放到 sub_stream 并行
 10  routed_output = apply_unpermute_finalize(...)  # combine + 加权聚合
 11  合并: 共享专家 TP>1 时 all_reduce; 第 2 步切过时 all_gather 还原全序列
 12  return (shared_output, routed_output)
```

#### 4.2.3 源码精读

**入口与 token 切片**：

- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:86-106](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L86-L106) —— `apply()` 开头：先为策略选择器拿到 token 数，再在「未开序列并行且 TP>1」时把 hidden_states 补齐到 TP 的倍数并切出本 rank 的一段（`start = tp_rank * local_num_tokens`）。
- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:108-120](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L108-L120) —— 门控计算：`router_gating_in_fp32` 开启时把输入转 float32 再过 gate；外部已传入 logits 时同样按 rank 切片。

**top-k 选择**（`NPUFusedMoE.select_experts` 静态方法）：

- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:433-446](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L433-L446) —— **profile run 特殊分支**：`attn_metadata is None` 说明这是显存画像阶段（对应 u2-l3 讲过的 `determine_available_memory`），此时用 `ep_rank` 生成**周期循环的 topk_ids + 随机权重**，强制负载均匀，保证画像不偏科。
- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:448-471](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L448-L471) —— 分组与普通两条路径都汇聚到融合算子 `torch_npu.npu_moe_gating_top_k`：一个内核完成「打分（`norm_type=1` 即 sigmoid）+ 偏置修正 + 分组选 top-k + `routed_scaling_factor` 缩放」，替代 vLLM 在 GPU 上的多算子拼接；`renormalize=True` 时再补一步归一化 \( w_i \leftarrow w_i / \sum_j w_j \)。

**三段式骨架**：

- [components/omni-npu/src/omni_npu/layers/fused_moe/fused_moe_method_base.py:15-28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/fused_moe_method_base.py#L15-L28) —— `NPUFusedMoEMethodBase`：持有 `CommunicationStrategySelector`，把三段调用转发给具体策略实现。
- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:175-201](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L175-L201) —— `apply()` 中段：`apply_prepare_permute` 之后，若策略是 `agrs` 且开了 `enable_agrs_finalize_metadata_overlap`，专家计算挪到 `agrs_overlap_stream`、finalize 元数据同时留在主 stream —— 用双流隐藏 AllGather 元数据的延迟。
- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:203-220](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L203-L220) —— 共享专家执行：`shared_expert_multi_stream` 开启时放到 `sub_stream` 与路由专家并行；注意共享专家 TP>1 时必须吃**全量** `hidden_states`（因为它的 gate_up 按 TP 切了列，需要完整序列），输出随后 all_reduce。
- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:229-260](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L229-L260) —— 回收与合并：`apply_unpermute_finalize` 之后按三种情况收尾 —— 共享专家 TP>1 → `tensor_model_parallel_all_reduce(shared_output)`；`is_need_slice` → `all_gather` 还原全序列并裁掉 padding；`use_custom_model_add`（部署时 `VLLM_PLUGINS` 含 `omni_custom_models`，1P1D 模板正是如此）时把共享输出加进路由输出。

**纯 TP 路径（无 EP）**：

- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:143-163](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L143-L163) —— `use_ep=False` 时（未开 `--enable-expert-parallel`）每张卡都持有全部专家，直接走 `fused_experts_tp`，一步到位。
- [components/omni-npu/src/omni_npu/layers/fused_moe/fused_moe.py:86-107](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/fused_moe.py#L86-L107) —— `fused_experts_tp`：`npu_moe_init_routing`（排序）→ `npu_moe_compute_expert_tokens`（每组计数）→ 量化时 `npu_dynamic_quant` → `apply_experts` → `npu_moe_finalize_routing`（加权聚合），五个 torch_npu 融合算子串成一条无通信的本地流水线。

**自定义 op 封装**（为图编译服务）：

- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:405-418](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L405-L418) —— `NPUFusedMoE.forward` 被包成 `torch.ops.vllm.npu_moe_forward` / `npu_moe_forward_shared` 两个自定义算子（有无共享专家各一个），函数体里再用 `forward_context.no_compile_layers[layer_name]` 反查真实层对象。
- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:555-569](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L555-L569) —— `direct_register_custom_op` 注册到 `PrivateUse1` dispatch key（NPU 的分发键，u2-l2 讲过），并带 `fake_impl`。这一层「不透明包装」让 ACL Graph 捕获时把整个 MoE 当作单个算子节点，内部多流/动态分支不会破坏图结构（承接 u5-l2 图编译）。`forward` 上的 `@attn_decorator(type='moe_ffn')` 装饰器则给第三方插件留了 pre/post 钩子位（定义见 [components/omni-npu/src/omni_npu/plugin_decorators.py:171](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/plugin_decorators.py#L171)）。

#### 4.2.4 代码实践

**实践目标**：用单测验证 `select_experts` 的两条行为 —— profile run 的强制均衡、普通路径的 renormalize。

1. 操作步骤：

   ```bash
   cd components/omni-npu
   python -m pytest tests/unit/layers/fused_moe/test_layer.py \
       -k "select_experts" -q
   ```

2. 需要观察的现象：`test_select_experts_profile_mode`（桩环境里 `attn_metadata=None`）断言 topk_ids 呈周期循环、权重为随机；`test_select_experts_default_path_with_renormalize` 断言权重沿最后一维求和为 1。
3. 预期结果：全部通过（约 5 个用例）。对应断言源码见 [components/omni-npu/tests/unit/layers/fused_moe/test_layer.py:181-198](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/layers/fused_moe/test_layer.py#L181-L198) 与 [components/omni-npu/tests/unit/layers/fused_moe/test_layer.py:921-941](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/layers/fused_moe/test_layer.py#L921-L941)。
4. 若本地无法运行，标注「待本地验证」，改为阅读这两个测试函数，并在纸上写出 profile 分支中 `topk_ids` 的表达式：`(ep_rank*N*k ... ) % E` 为什么天然均匀。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `apply()` 里共享专家在 TP>1 时吃 `hidden_states` 而不是 `x_slice`？

答案：`x_slice` 是本 rank 负责的那 1/TP token 序列段（为序列并行准备）；而共享专家的 `gate_up_proj` 是按**列**切的 `MergedColumnParallelLinear`，列切意味着每个 token 的完整输入向量它都要（只是输出通道减半），所以必须喂全量序列，输出再靠 all_reduce 把各 rank 的部分和加起来。代码依据：[layer.py:208-220](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L208-L220) 的注释与 [layer.py:247-249](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L247-L249) 的 all_reduce。

**练习 2**：`USE_MOE_FORCE_LOAD_BALANCE` 环境变量打开后模型侧行为如何变化？在哪儿读的？

答案：[pangu_v2_moe.py:263-265](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L263-L265) 读取该环境变量；打开后 [pangu_v2_moe.py:462-465](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L462-L465) 会用 `aux_load_balance_tensor`（0..E-1 平铺重复）覆写 `topk_ids`，让每个专家被等概率访问 —— 压测/画像时用来抹平路由倾斜。

**练习 3**：`prepare_permute` 为什么必须发生在 `select_experts` 之后、`apply_experts` 之前？

答案：重排的依据是每个 token 的 `topk_ids`（token→专家的映射），没有它无法分组；而 `grouped_matmul` 的 `group_list`（每专家 token 计数）只有在重排后才是连续分段的有效描述。三段式正是把「依赖 topk_ids 的通信+重排」与「只看重排结果的计算」解耦，量化方法因此只需替换中间一段。

### 4.3 专家并行：三种通信策略与自动选择

#### 4.3.1 概念说明

EP 之下 token 与专家分居不同卡，必须回答「怎么把 token 送去、怎么把结果取回」。本仓库实现了三种策略，全部遵循 `prepare_permute → apply_experts → unpermute_finalize` 三段式，差别只在前后两段：

| 策略 | dispatch 方式 | combine 方式 | 特点 |
|---|---|---|---|
| `all2all` | 本地先展开，`dist.all_to_all_single` 直接把 token 寄给专家所在卡 | 反向 `all_to_all_single` 寄回 | 通用 PyTorch 分布式原语；发送量正比于路由命中 |
| `agrs`（AllGather-ReduceScatter） | `get_ep_group().all_gather` 全量 token 聚到每卡，本地只路由**自己那段专家** | `get_ep_group().reduce_scatter` 按段求和切回 | 无需按专家拆包；每卡看到全部 token，可本地过滤 `active_expert_range` |
| `dispatch_combine` | `npu_moe_distribute_dispatch_v2`（通信+重排融成单个 HCCL 算子） | `npu_moe_distribute_combine_v2` | 通信与重排在内核内融合、支持 `mc2_mask` 掩码；有 token 数上限 |

三者的通信量结构（\( N \) 为 EP 组内总 token 数，\( h \) 为 hidden size，\( b \) 为每元素字节数）：

- **agrs**：AllGather 通信量 \( \approx \frac{EP-1}{EP} \cdot N h b \)，ReduceScatter 同量级，合计约 \( 2\frac{EP-1}{EP} N h b \) —— 与一次 AllReduce 相当，但两次通信之间夹着计算，且都可用双流隐藏。
- **all2all**：理想情况下每卡只发送 \( \frac{k}{E} \) 量级的外流量（其余命中本地专家），小 batch 下反而可能更省；但需要两次逐 rank 拆包（先 counts 后 data）。
- **dispatch_combine**：把拆包/组包做进内核，省去多次算子启动；限制是单次 batch 的 token 数有上限（默认阈值 64，A2 上限 256、其余 512）。

#### 4.3.2 核心流程

`CommunicationStrategySelector.select_communication_strategy(num_tokens)` 的决策树：

```text
读取设备名（Ascend910B=A2 / Ascend950=A5）
├─ A5 或 enable_moe_agrs 打开            → agrs
├─ A2 (910B):
│   ├─ decode_moe_dispatch_combine 打开:
│   │     local_tokens > 阈值(默认64)  → 开了序列并行 ? agrs : all2all
│   │     否则                          → dispatch_combine
│   └─ 关闭: tp_size==1 或 dp_size==1  → agrs
│            否则（TP+DP 并存）          → 整图模式 ? agrs : all2all
└─ 其他 (A3 等):
    ├─ dp_size==1 或 tp_size==1: local_tokens > 阈值 ? all2all : dispatch_combine
    └─ 否则（TP+DP 并存）                → 整图模式 ? agrs : all2all
```

其中 `local_num_tokens = ceil(num_tokens / tp_size)`（未开序列并行时）。选中的策略实现按需懒构造并缓存（`_get_strategy_impl`）。

#### 4.3.3 源码精读

**选择器**：

- [components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py:589-613](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L589-L613) —— 构造：读设备名、TP/DP 规模、`MAX_DISPATCH_COMBINE_THRESHOLD` 环境变量（A2 断言 ≤256、非 A2/A5 断言 ≤512），登记三个策略类。
- [components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py:624-672](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L624-L672) —— `select_communication_strategy`：完整决策树，返回 `(strategy, impl)` 二元组。

**all2all**：

- [components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py:128-156](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L128-L156) —— 先 `npu_moe_init_routing_v2` 本地展开，再**两次** `dist.all_to_all_single`：第一次交换每专家 token 计数算出 `input_splits/output_splits`，第二次交换真实数据（量化时 scale 也单独交换一次）。
- [components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py:166-183](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L166-L183) —— 接收侧 `npu_moe_re_routing` 把收到的 token 再按本地专家排序，产出计算所需的连续布局。
- [components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py:185-214](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L185-L214) —— `unpermute_finalize`：对输出做 `argsort` 反序 → 反向 `all_to_all_single` 寄回原卡 → `npu_moe_finalize_routing` 完成加权聚合。

**agrs**：

- [components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py:224-252](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L224-L252) —— `prepare_permute`（未量化分支）：`get_ep_group().all_gather` 把输入与 `topk_ids` 聚成全量，随后 `npu_moe_init_routing_v2` 带 `active_expert_range=[本卡专家起点, 终点)` —— **只挑属于本卡的 (token, 专家) 对**展开，`w13_weight.shape[0]` 即本卡本地专家数。
- [components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py:253-322](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L253-L322) —— 量化分支：先 `npu_dynamic_quant`（或 hifloat8/mxfp8 专用量化，见 4.4）再 AllGather int8 数据与 scale；A2 设备 decode 或开启 prefill gmm-fr 时 `row_idx_type=1`，走后续 `npu_grouped_matmul_finalize_routing` 的融合回收路径。
- [components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py:394-403](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L394-L403) —— `prepare_finalize_metadata`：把 `topk_weights` 也 AllGather，供回收阶段在双流上提前准备（对应 4.2.3 的 overlap）。
- [components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py:405-504](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L405-L504) —— `unpermute_finalize`：按量化与否选择 `npu_grouped_matmul_finalize_routing`（W8A8 融合反量化+聚合）或 `npu_moe_finalize_routing`（`drop_pad_mode=3`），最后 [第 504 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L504) `get_ep_group().reduce_scatter(y, dim=0)` 收尾 —— 这就是策略名里「RS」的落点。

**dispatch_combine**：

- [components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py:507-520](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L507-L520) —— 构造时通过 `get_hccl_comm_name` 拿到 EP 组的 HCCL 通信域名交给算子；A5 上因该接口报错直接跳过初始化（这也解释了为什么 A5 强制走 agrs）。
- [components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py:533-559](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L533-L559) —— `npu_moe_distribute_dispatch_v2` 一个调用同时完成「跨卡分发 + 按专家重排」，附带 `x_active_mask`（mc2 掩码，来自 decode 批元数据，可跳过无效 token）。
- [components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py:561-586](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L561-L586) —— 对称的 `npu_moe_distribute_combine_v2`：传回 dispatch 阶段拿到的 `ep_recv_counts/tp_recv_counts` 完成回收聚合。

**模型侧的手写前向（另一条到达同一批算子的路）**：

- [components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:351-387](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L351-L387) —— `OpenPanguV2MOE._forward_single` 按 `moe_comm_strategy` 配置分发到四条前向；A5 与默认兜底走 `_forward_fused_moe`（即经 `NPUSharedFusedMoE.forward` 自定义 op → 本节插件管线），A2/A3 上 `allreduce`/`all2allv`/`dispatch_combine`/`allgather_reducescatter` 四种策略则由**模型文件手写**。
- [components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:611-641](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L611-L641) —— 手写前向的计算段示例（`_forward_allgather` 内）：两次 `npu_grouped_matmul` + `npu_swiglu` + `npu_moe_finalize_routing`，直接引用 `self.experts.w13_weight` —— 绕过层 forward、复用层的权重。
- [components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:651-676](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L651-L676) —— 通信段：`use_allreduce=True` 时 `all_reduce`，否则 `reduce_scatter`；共享专家在 side stream 上与主链路并行，最后 `routed_output + shared_output`。
- 默认值与最佳实践的对照：[components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:180](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L180) 里 `moe_comm_strategy` 默认 `"dispatch_combine"`，而 92B P 节点的最佳实践 [pangu_v2_moe_bf16_a3_92B_xp1d_p_open.json:10](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/low_latency/openpangu_v2/pangu_v2_moe_bf16_a3_92B_xp1d_p_open.json#L10) 覆盖为 `"allgather_reducescatter"` —— 模型配置系统（u5-l1）就是这样改变 MoE 通信形态的。

#### 4.3.4 代码实践

**实践目标**：用单测复现选择器的决策树，并观察阈值切换。

1. 操作步骤：

   ```bash
   cd components/omni-npu
   python -m pytest tests/unit/layers/fused_moe/test_fused_moe_prepare_permute_unpermute_finalize.py \
       -k "strategy_selector" -q
   ```

2. 需要观察的现象：约 15 个 `test_strategy_selector_*` 用例逐一覆盖决策树分支，例如 `..._a2_dispatch_combine_enabled_small_tokens`（小 batch → dispatch_combine）与 `..._a2_dispatch_combine_enabled_large_tokens`（超阈值 → all2all/agrs）。
3. 预期结果：全部通过。断言与 [prepare_permute_unpermute_finalize.py:624-672](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L624-L672) 的分支一一对应；测试位置 [components/omni-npu/tests/unit/layers/fused_moe/test_fused_moe_prepare_permute_unpermute_finalize.py:809-1073](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/layers/fused_moe/test_fused_moe_prepare_permute_unpermute_finalize.py#L809-L1073)。
4. 进阶（可选项，改环境变量属于测试内操作，不影响源码）：阅读 `test_strategy_selector_a2_dispatch_combine_enabled_small_tokens` 如何 mock 设备名与 `model_extra_config`，然后回答：把 `MAX_DISPATCH_COMBINE_THRESHOLD` 设为 8、`local_num_tokens=16`，A2 + `decode_moe_dispatch_combine=True` + 无序列并行时应选哪个策略？（答案在练习 1）

#### 4.3.5 小练习与答案

**练习 1**：接上文进阶题，答案是什么？

答案：`all2all`。A2 且 `decode_moe_dispatch_combine=True` 时，`local_num_tokens(16) > 阈值(8)` 成立，进入「开了序列并行 ? agrs : all2all」分支，无序列并行所以是 `all2all`（依据 [prepare_permute_unpermute_finalize.py:640-647](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L640-L647)）。

**练习 2**：为什么 A5（Ascend950）设备无条件选择 `agrs`，即使 `enable_moe_agrs=False`？

答案：两个原因叠加：其一，`dispatch_combine` 依赖的 `get_hccl_comm_name` 在 A5 上会报错（[prepare_permute_unpermute_finalize.py:510-515](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L510-L515) 有明确 warning）；其二，选择器首个判断 `is_a5_device or enable_moe_agrs` 直接短路（[第 636-638 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L636-L638)）。

**练习 3**：`agrs` 策略下，某张卡计算量由什么决定？这带来什么负载均衡问题？

答案：由「全组 token 中路由到本卡所持专家的 (token,专家) 对数」决定 —— 即 \( \sum_{i \in \text{本卡专家}} \text{hit}(i) \)。若路由天然倾斜（某些热门专家被高频命中），持热门专家的卡会成为长尾。这正是 u9（omni-eplb 专家负载均衡）要解决的问题：通过周期性重排专家放置，把 hit 均匀摊到各卡。

### 4.4 融合算子：两次 grouped_matmul 与量化、权重布局适配

#### 4.4.1 概念说明

NPU 上 MoE 的计算核心是**分组矩阵乘**（grouped matmul, GMM）：一组权重（\( E_{local} \) 个专家的 w13/w2）配一份 `group_list`（每专家 token 计数），一个内核内完成「按组取权重 × 按组取 token」的批量乘法，省去逐专家启动 kernel。标准专家结构展开为：

\[
\text{GMM}_1(x, W_{13}) \rightarrow \text{SwiGLU} \rightarrow \text{GMM}_2(\cdot, W_2)
\]

其中 \( W_{13} \) 把 gate/up 两个投影打包（维度 \( 2\times I \)），`npu_swiglu` 在一个算子里完成 SiLU 门控与逐元素乘。

NPU 特有的两个适配点：

1. **权重转置与 FRACTAL_NZ 布局**：Cube 单元的矩阵乘要求特定内存排布，`process_weights_after_loading` 会把权重 `transpose(1,2)`，`gmm_nz` 开启时再转成 `FRACTAL_NZ` 格式。
2. **量化 scale 的穿针引线**：W8A8（jointfix 产物）下激活是 per-token int8（`npu_dynamic_quant` 产出 `pertoken_scale`），权重是 per-channel int8；反量化可以推迟到回收阶段，由 `npu_grouped_matmul_finalize_routing` 在加权聚合的同一内核里完成 —— 通信与回收全程传 int8，带宽减半。

#### 4.4.2 核心流程

```text
apply_experts(prepare_permute_result):
    h      = hidden_states_sorted_by_experts     # 按专家连续排布（可能 int8 + scale）
    groups = expert_tokens (int64)               # 每本地专家的 token 数
    gate_up = npu_grouped_matmul([h], [w13_weight], group_list=groups,
                                 split_item=3, group_type=0,
                                 group_list_type=1 if EP else 0)
    inter   = npu_swiglu(gate_up)                # SiLU 门控融合
    out     = npu_grouped_matmul([inter], [w2_weight], ...)   # 同参数
    return out
```

量化下的回收（`agrs` + `row_idx_type=1` 路径）则把第二次 GMM 与反量化、加权聚合合并：

```text
y = npu_grouped_matmul_finalize_routing(
        x_int8, w2_weight, expert_tokens,
        scale=w2_scale, bias=w2_bias,          # 权重侧反量化因子
        pertoken_scale=..., logit=topk_weights, # 激活 scale + 路由权重
        row_index=..., output_bs=batch_size, ...)
```

#### 4.4.3 源码精读

- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:280-307](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L280-L307) —— `NPUUnquantizedFusedMoEMethod.apply_experts`：两次 `torch_npu.npu_grouped_matmul` 夹一个 `npu_swiglu`。注意 `group_list_type = int(layer.moe_parallel_config.use_ep)`：EP 开关决定 `group_list` 按本地专家还是全局专家解释。
- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:262-278](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L262-L278) —— `process_weights_after_loading`：加载完成后把 `w13_weight/w2_weight` 转置为内核期望布局并打上 `is_weight_transposed` 标记；`gmm_nz` 开启时再 `npu_format_cast(..., FRACTAL_NZ)` 并标 `is_weight_nz`。
- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:325-342](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L325-L342) —— `weight_loader` 开头的「布局临时还原」：NZ 权重先转回 ND、转置权重先转回来，走完父类加载逻辑后再转回去（[第 387-391 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L387-L391)），并在 NZ 分支触发 `set_aclgraph_recapture(True)` —— 权重地址布局变化使已捕获的 ACL 图失效，需重捕（承接 u5-l2）。
- [components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py:259-274](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L259-L274) —— 量化分派：`use_hifloat8_w8a8` → `npu_dtype_cast` 转 hifloat8；`use_mxfp8_w8a8` → 先保 bf16 路由、展开后再 `npu_dynamic_mx_quant`（源码注释说明了绕行原因：`npu_moe_init_routing_v2` 的 mxfp8 scale 路径在当前 torch_npu 上有缺陷）；默认 W8A8 → `npu_dynamic_quant` 得 int8 + per-token scale。
- [components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py:427-481](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/prepare_permute_unpermute_finalize.py#L427-L481) —— 量化回收：`npu_grouped_matmul_finalize_routing` 一次完成「GMM2 + 权重/激活双 scale 反量化 + 路由权重加权聚合」，`w2_scale`/`w2_bias` 直接取自层属性（W4A8 与 W8A8 的取法不同）。
- [components/omni-npu/src/omni_npu/layers/fused_moe/config.py:24-35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/config.py#L24-L35) —— 一个小而典型的 monkey patch：直接给 vLLM 的 `FusedMoEQuantConfig` 类挂 `use_hifloat8_w8a8` / `use_mxfp8_w8a8` 属性，让下游代码可以用统一接口询问量化形态（u2-l4 补丁思想的轻量版）。
- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L43) —— 模块导入即设置 `torch.npu.config.allow_internal_format = True`：允许 torch_npu 在底层自动选择内部数据格式（与 NZ 布局配合）。

#### 4.4.4 代码实践

**实践目标**：确认「加载后转置 + 打标」的行为，理解权重在显存里的真实形状。

1. 操作步骤（无 NPU 环境，走单测；有部署容器则二选一）：

   ```bash
   cd components/omni-npu
   python -m pytest tests/unit/layers/fused_moe/test_layer.py \
       -k "process_weights_after_loading or apply_experts" -q
   ```

   在部署容器内（服务已加载模型后，另开 python 或加日志）观察：

   ```python
   # 示例代码（在容器内以调试方式执行）
   layer = ...  # 任一 NPUSharedFusedMoE 层
   print(layer.w13_weight.shape, layer.w13_weight.stride())
   print(getattr(layer.w13_weight, "is_weight_transposed", None),
         getattr(layer.w13_weight, "is_weight_nz", None))
   ```

2. 需要观察的现象：单测 `test_process_weights_after_loading_transposes_and_marks` 断言权重被 `transpose(1,2)` 且属性置 True、`test_apply_experts_uses_grouped_matmul_twice` 断言 `npu_grouped_matmul` 恰被调用两次且中间夹 swiglu。
3. 预期结果：单测通过；容器内 `is_weight_transposed=True`（BF16 开局通常 `is_weight_nz` 仅在 `gmm_nz` 开启的量化/特定配置下为 True，以实际模型配置为准，待本地验证）。
4. 对照源码：[tests/unit/layers/fused_moe/test_layer.py:215-233](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/layers/fused_moe/test_layer.py#L215-L233) 与 [tests/unit/layers/fused_moe/test_layer.py:771-794](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/layers/fused_moe/test_layer.py#L771-L794)。

#### 4.4.5 小练习与答案

**练习 1**：`split_item=3` 与 `group_list_type` 分别控制什么？

答案：`split_item=3` 是 torch_npu grouped_matmul 的分组模式，表示「按 `group_list` 给出的累积边界把输入切成连续段，每段配一组权重」（对应 MoE 的按专家排序布局）；`group_list_type` 取 0/1 区分 `group_list` 语义为「每组的起始偏移」还是「每组的元素个数」——EP 开启时代码取 `int(use_ep)` 即 1，与 AllGather 后按全局专家计数相匹配（见 [layer.py:288-297](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L288-L297) 与 [pangu_v2_moe.py:611-631](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L611-L631) 的对照）。

**练习 2**：为什么 W8A8 下 `npu_grouped_matmul_finalize_routing` 能同时省通信和省计算？

答案：它把「GMM2 + 双侧反量化 + 路由加权求和」合为一个内核：一方面跨卡回收时张量仍是 int8（带宽减半），另一方面反量化乘子可以与路由权重 \( g_i \)、reduce_scatter 的求和合并成一次乘加，避免了「先反量化成 bf16 再聚合」的中间大张量。

**练习 3**：如果给你一个新激活函数（例如 GeGLU）要替换 SwiGLU，最小改动点在哪？

答案：`apply_experts` 中的 `torch_npu.npu_swiglu`（[layer.py:298](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L298)）与模型手写前向中的同款调用（如 [pangu_v2_moe.py:621](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L621)）；若 torch_npu 无对应融合算子则退化为「split + silu/gelu + mul」的组合实现，并评估对 aclgraph 捕获的影响。

### 4.5 EP 下的权重切分与加载：每个 rank 持有哪些专家

#### 4.5.1 概念说明

EP 的权重问题：checkpoint 里每个专家有完整的三份权重（`gate_proj/up_proj/down_proj`），加载时每张卡只应留下自己那段专家。关键概念：

- **逻辑专家 vs 物理专家**：不开 EPLB 时两者相等（\( E \) 个）；开 EPLB 后物理专家数 \( E_p = E + R \times \text{EP} \)（\( R \) 为每卡冗余专家数），冗余副本用于热点专家的多卡分摊与高可用。
- **本地专家数**：\( E_{local} = E_p / \text{EP} \)，本卡专家区间为 \( [\text{ep\_rank} \cdot E_{local},\; (\text{ep\_rank}+1)\cdot E_{local}) \)。
- **专家映射表 `expert_map`**：长度 \( E_p \) 的表，把全局专家 id 映射为本地槽位或 -1（不在本卡）。加载与路由都靠它判定归属。

#### 4.5.2 核心流程

```text
加载一个专家权重 (expert_id, gate/up/down 分片):
  weight_loader(param, loaded_weight, ..., expert_id)
    ├─ NZ/转置布局临时还原
    ├─ 若 enable_eplb:
    │     planner.is_expert_on_current_rank(expert_id) ?
    │       否 → 直接跳过 (return)
    │       是 → expert_id ← local_pos + E_local × ep_rank   # 物理槽位重排
    ├─ 若是 bias/int4_scale → 走 per-channel scale 分支
    └─ 否则 super().weight_loader(...)
          → _map_global_expert_id_to_local_expert_id(expert_id)
              == -1 → 跳过（专家不在我卡上）
              否则  → 写入 param.data[local_id] 对应槽位
```

权重名到 (param, expert_id, shard) 的翻译由 `SharedFusedMoE.make_expert_params_mapping` 生成的 `expert_params_mapping` 完成，模型 `load_weights` 用它把形如 `model.layers.5.mlp.experts.23.gate_proj.weight` 的 checkpoint 键翻译成融合张量 `experts.w13_weight` 的第 23 个槽、`shard_id=w1`。

#### 4.5.3 源码精读

- [components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:2288-2295](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2288-L2295) —— `load_weights` 里生成专家映射表：`num_experts=n_routed_experts`、`num_redundant_experts` 来自 EPLB 配置。
- [components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:2329-2359](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2329-L2359) —— `_try_load_expert`：对每个 checkpoint 键查表换名，调用 `param.weight_loader(..., expert_id=..., return_success=True)`；返回 False（专家不在本卡）则返回空串跳过 —— 这是「每卡只留本地专家」的落地处。
- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:344-355](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L344-L355) —— EPLB 分支：由 planner 判定专家是否在本 rank，并按物理槽位重写 `expert_id = local_pos + local_num_experts × ep_rank`。
- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:357-376](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L357-L376) —— bias / int4_scale 分支：先做全局→本地专家映射（-1 即跳过），只支持 per-channel 权重 scale（`FusedMoeWeightScaleSupported.CHANNEL`）。
- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:377-385](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L377-L385) —— 普通权重交回父类 `FusedMoE.weight_loader`（vLLM 实现，内部用 `expert_map` 决定落卡或跳过）。
- [components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:297-307](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L297-L307) —— EPLB 开启时把 `experts._expert_map` 重写为 `arange(E_p) % E_local` 的循环模式，并计算 `physical_expert_start/end` —— 供 4.3 的 `active_expert_range` 过滤使用。
- [components/omni-npu/src/omni_npu/layers/fused_moe/layer.py:394-400](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L394-L400) —— `maybe_all_reduce_tensor_model_parallel`：EP 开启时**不做** AllReduce —— 因为 combine 阶段的 `reduce_scatter` 已经完成求和（回答了一个常见疑惑：开了 EP 之后 MoE 出口为什么不再 AllReduce）。

#### 4.5.4 代码实践

**实践目标**：为你手上的模型权重算出「每张卡各持哪些专家」，并验证跳过逻辑。

1. 操作步骤：
   - 读 checkpoint 的 `config.json`，记下 `n_routed_experts`（记 \( E \)）、`num_experts_per_tok`（\( k \)）、`n_shared_experts`。
   - 确认部署命令：1P1D 模板的 `EXTRA_ARGS` 含 `--enable-expert-parallel`（[tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:92](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L92)），TP16 → EP16。
   - 推导：\( E_{local} = E / 16 \)；rank \( r \) 持专家 \( [r \cdot E_{local}, (r+1)\cdot E_{local}) \)，另有 1 份完整共享专家（`disable_tp=True`）。
   - 跑单测佐证跳过分支：

     ```bash
     cd components/omni-npu
     python -m pytest tests/unit/layers/fused_moe/test_layer.py \
         -k "weight_loader_skips_non_local_expert" -q
     ```

2. 需要观察的现象：测试构造 `expert_map` 含 -1 的桩，断言 `weight_loader` 对非本地专家直接返回、参数张量未被写入。
3. 预期结果：1 passed；你的推导表与 `w13_weight.shape[0] == E_local`（容器内可验）一致。
4. 显存直觉校验（容器内）：`w13_weight.numel() × 2字节 ≈ E_local × 2I × h × 2`，与该层实际占用对得上即为正确。

#### 4.5.5 小练习与答案

**练习 1**：256 个路由专家、EP=16，rank 5 持有哪些专家？开 EPLB（每卡冗余 1 个）后又如何变化？

答案：不开 EPLB：\( E_{local}=16 \)，rank 5 持专家 80..95（16 个）。开 EPLB：\( E_p = 256 + 1×16 = 272 \)，\( E_{local}=17 \)，rank 5 持物理专家 85..101 —— 其中哪些逻辑专家落在这一段由 planner 的重排决定（[pangu_v2_moe.py:297-304](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L297-L304)）。

**练习 2**：`weight_loader` 的 `return_success=True` 返回值设计是为了什么？

答案：为了让模型的 `_try_load_expert` 能区分「这个键是本卡该加载的专家（成功）」与「这个键属于别的卡（跳过）」—— 返回 False 时调用方记 `""` 并静默跳过（[pangu_v2_moe.py:2353-2358](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2353-L2358)），否则 vLLM 结束时会误报「unloaded weights」。

**练习 3**：EP 开启后，`NPUFusedMoE.maybe_all_reduce_tensor_model_parallel` 为何直接返回原张量？

答案：EP 模式下各卡计算的是**不同专家**对全组 token 的部分贡献，求和已由策略实现里的 `reduce_scatter`（agrs）或 `dispatch_combine`/`all2all` 的 combine 完成；再 AllReduce 会重复求和。见 [layer.py:394-400](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/fused_moe/layer.py#L394-L400) 的注释。

## 5. 综合实践

**任务**：产出两份可复查的文档 —— ① `NPUSharedFusedMoE` 相对 vLLM 原生 `SharedFusedMoE`/`FusedMoE` 的差异清单；② `--enable-expert-parallel` 打开后单机 16 卡的专家持有表。

**步骤一：导出 vLLM 原生类做对照（在部署容器内，vLLM 是真实现）**。

```python
# 示例代码：容器内执行
import inspect
from vllm.model_executor.layers.fused_moe.layer import FusedMoE
from vllm.model_executor.layers.fused_moe.shared_fused_moe import SharedFusedMoE
from omni_npu.layers.fused_moe.layer import NPUFusedMoE, NPUSharedFusedMoE

print(NPUSharedFusedMoE.__mro__)
for name in ("__init__", "forward", "weight_loader", "select_experts",
             "maybe_all_reduce_tensor_model_parallel"):
    owner = next(c for c in NPUSharedFusedMoE.__mro__ if name in vars(c))
    print(f"{name:<42} -> {owner.__name__}")
    print(inspect.getsource(getattr(owner, name)))
```

**步骤二：核对差异清单**。把打印结果与本讲列出的 NPU 侧覆写逐项对勾，预期至少覆盖：`__init__` 新增 `gate` 与策略选择器、`weight_loader` 的 NZ/转置/EPLB/return_success 处理、`maybe_all_reduce_tensor_model_parallel` 的 EP 短路、`maybe_init_modular_kernel` 返回 None、`forward` 的自定义 op 封装、`select_experts` 的 torch_npu 门控与 profile 均衡分支、`process_weights_after_loading` 的转置+NZ。凡 vLLM 侧与预期不符的，以容器内源码为准修订清单。

**步骤三：专家持有表**。按 4.5.4 的方法，用你 checkpoint 的 `config.json` 算出 16 张卡各自的专家区间（含共享专家整卡复制），在容器内用 `w13_weight.shape[0]` 验证；如部署开的是 INT8（w8a8）模板，再补一列「每卡 int8 专家权重的字节数」。

**步骤四：回归佐证**。在无 NPU 的开发机上跑：

```bash
cd components/omni-npu
python -m pytest tests/unit/layers/fused_moe/ -q
```

预期全绿（该目录测试全部基于桩环境，不依赖真机；`pytest.ini` 在 [components/omni-npu/pytest.ini](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pytest.ini)）。若任一环节本地条件不足，在文档中标注「待本地验证」并写明缺失的依赖。

## 6. 本讲小结

- **组装**：`OpenPanguV2MOE` = fp32 gate + e_score_correction_bias + 整卡复制的共享专家 + `NPUSharedFusedMoE`（经 `@register_oot` 多继承 vLLM `SharedFusedMoE` 与插件 `NPUFusedMoE`）；「融合」指模块合属、回收算子预留 shared_input、以及多流重叠三种含义。
- **三段式**：MoE 前向被规范为 `prepare_permute → apply_experts → unpermute_finalize`，量化方法只需替换中间的计算段（两次 `npu_grouped_matmul` 夹 `npu_swiglu`）。
- **EP 三策略**：`all2all`（按专家拆包直寄）、`agrs`（AllGather 全量+本地过滤+ReduceScatter）、`dispatch_combine`（HCCL 融合分发/回收算子），由 `CommunicationStrategySelector` 按设备型号、TP/DP、token 阈值与图模式自动挑选。
- **两条到达路径**：A5/默认走层自定义 op（`torch.ops.vllm.npu_moe_forward[_shared]`，利于 aclgraph 捕获）；A2/A3 上 `moe_comm_strategy` 配置可切换模型文件内的手写通信前向（92B P 节点最佳实践为 `allgather_reducescatter`）。
- **量化与布局适配**：W8A8/hifloat8/mxfp8 在 dispatch 前量化、回收时由 `npu_grouped_matmul_finalize_routing` 融合反量化与加权聚合；权重加载后转置、必要时转 FRACTAL_NZ 并触发图重捕。
- **权重切分**：每个 rank 只持 \( E_p/\text{EP} \) 个专家（EPLB 时含冗余）+ 1 份共享专家，靠 `expert_map`/planner 判归属，EP 模式下出口无需再 AllReduce。

## 7. 下一步学习建议

1. **u3-l4（自定义层）**：MoE 周边的 RMSNorm、mHC 残差流、logits 处理器如何配合本讲的层体系工作，理解一个 MoE block 的完整数据通路。
2. **u9（omni-eplb）**：本讲反复出现的 `enable_eplb`、`logical_to_physical_map`、`planner.is_expert_on_current_rank` 在调度侧的全貌 —— 专家如何在不停服的前提下重排。
3. **u8（jointfix 量化）**：本讲的 `npu_dynamic_quant`/`w2_weight_scale` 消费的 int8 权重是如何产出的，`quantization_config` 如何让 vLLM 选择 `NPUUnquantizedFusedMoEMethod` 之外的量化方法。
4. **源码延伸阅读**：[components/omni-npu/tests/unit/layers/fused_moe/test_layer.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/layers/fused_moe/test_layer.py) 的桩环境本身就是一份「本模块依赖面清单」；以及 [components/omni-npu/tests/integration/models/test_standalone_moe_profile_tp8.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/integration/models/test_standalone_moe_profile_tp8.py)（需真机），看 MoE 在 TP8 下的端到端画像如何跑。
