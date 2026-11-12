# Hybrid Engine：训练与推理一体化

## 1. 本讲目标

RLHF / RLVR 类训练的核心循环是「训练几步 → 用当前策略生成一批响应（rollout）→ 拿响应算奖励 → 继续训练」。如果训练用一套引擎、生成用另一套推理服务，权重就要在两套系统之间来回搬运；Hybrid Engine 的答案是让**同一份训练状态**直接具备推理能力。读完本讲，你应该能够：

1. 描述 `DeepSpeedHybridEngine` 在 `train()` / `eval()` / `generate()` / `step()` 之间切换权重布局的完整流程，包括 ZeRO-3 下的参数收集（`GatheredParameters`）与推理容器的双向转换。
2. 画出 `HybridEngineRollout.generate()` 的路径分发：HuggingFace `generate()` 默认路径、静态图捕获路径、continuous batching（含图捕获变体）三条路各走什么条件。
3. 解释图捕获能力的**加速器显式 opt-in** 设计：`validate_graph_capture_support` 与 rollout 的两条生成路径都查询 `is_graph_capture_supported()`，未 opt-in 的后端 `warning_once` 后回退 eager 而非报错；理解后端支持面是演进的——KLXPU 在 #9042 完成验证后删除子类的 default-deny 覆写、随 CUDA 一起进入图捕获路径。
4. 解读静态图 decode 前向为什么必须接收**预构建的 4-D 加性掩码**（`static_causal_attn`）：Transformers 从 2-D mask 构建 SDPA 掩码时的 host 端检查会同步 stream、破坏图捕获。
5. 解读非贪婪采样快路径：`_token_selector(sampling)` 统一 graph decode、continuous batching 与 continuous batching+graph 三条路径的 token 选择；采样发生在 host 端、图之外。
6. 解读 `DeepSpeedStaticCache` 的三个关键机制：写位置外部化（图回放兼容）、`get_query_offset`（支持 Transformers 5.20 的 cache query offsets）、AutoTP 下的**惰性定尺寸**（各层从本 rank 的 prefill 张量取形状，`memory_bytes()` 对未初始化层记 0）。
7. 解读 ZeRO-3 rollout 的 EOS 早停组内同步（#8390）：`GlobalWorkRemaining` 在 `optimizer.dp_process_group` 上 `all_reduce(MAX)`，`_GlobalEosStoppingCriteria` 让全组一致停在最晚 EOS；以及为什么 ZeRO-3 路径要显式传 `synced_gpus=False`。

## 2. 前置知识

- **rollout（铺开 / 生成）**：RL 训练术语，指「用当前模型对一批 prompt 自回归生成响应」的阶段。rollout 期间只有前向（`torch.no_grad()`），但要跑几十到几百次 decode step。
- **prefill 与 decode**：生成分两段。prefill 一次吃进整条 prompt（一次前向，产出第一个新 token 与全部 prompt 的 KV cache）；decode 之后每步只吃 1 个 token、读历史 KV、写 1 列新 KV。decode 步的计算量很小，瓶颈往往在 kernel launch 的 CPU 开销上——这正是图捕获要解决的问题。
- **KV cache**：注意力层缓存的 Key/Value 张量，形状为 `[batch, kv_heads, seq_len, head_dim]`。静态缓存（StaticCache）预先按最大长度分配、地址固定，这是图捕获的前提。
- **CUDA Graph（图捕获）**：把一段 GPU 操作**录制**成一张图，之后一次 `replay` 重放全部操作，省去逐 kernel 的 launch 开销。捕获期间 host 端的查询/同步调用是非法的（会破坏捕获）；回放时 host 代码完全不执行。这两点是本讲多个设计的根源。
- **ZeRO-3（u5-l5 已建立）**：参数按 rank 切分，用时经 `GatheredParameters` 上下文在进程组上收集。rollout 期间每个 rank 必须执行**同样次数**的收集通信，否则集体通信错位、互相卡死。
- **加速器抽象层（u3-l1 已建立）**：所有设备能力经 `get_accelerator()` 查询；`is_graph_capture_supported()` 是**默认 False 的能力位**，后端验证通过后显式覆写为 True。`deepspeed/accelerator/` 是指向顶层 `accelerator/` 的符号链接，永久链接统一用真实路径 `accelerator/...`。
- **Inference V2 与 ragged（u7-l2/u7-l3 已建立）**：独立推理引擎的模块化设计。本讲的 Hybrid Engine 是另一条推理加速路线——不换引擎，而是在训练引擎内部临时切换到推理实现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspeed/runtime/hybrid_engine.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/hybrid_engine.py) | `DeepSpeedHybridEngine`：继承训练引擎，负责推理容器创建、train/eval 双向切换、ZeRO-3 参数收集与 generate 总入口 |
| [deepspeed/runtime/hybrid_engine_graph.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/hybrid_engine_graph.py) | 内核注入路径的图捕获：`DecodeGraphCache`（按位置一张图）与 `validate_graph_capture_support`（opt-in 校验） |
| [deepspeed/runtime/rollout/base.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/base.py) | rollout 接口层：`RolloutEngine` ABC 与 `RolloutRequest` / `RolloutBatch` / `SamplingConfig` 三个数据类 |
| [deepspeed/runtime/rollout/hybrid_engine_rollout.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py) | `HybridEngineRollout`：三条生成路径的完整实现（HF generate / 静态图 / continuous batching），含 EOS 早停同步与采样快路径 |
| [deepspeed/runtime/rollout/continuous_batching.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/continuous_batching.py) | `ContinuousBatchScheduler`：有界槽位的 FIFO 请求调度（admission / retirement） |
| [deepspeed/utils/static_cache.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/utils/static_cache.py) | `DeepSpeedStaticCache` / `DeepSpeedStaticLayer`：写位置外部化、惰性定尺寸、compact/trim 的图兼容 KV cache |
| [accelerator/abstract_accelerator.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/abstract_accelerator.py) | 图捕获能力位定义：默认 False + `is_current_stream_capturing` 前置要求 |
| [accelerator/cuda_accelerator.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cuda_accelerator.py) | CUDA 参照实现：opt-in 返回 True |
| [accelerator/klxpu_accelerator.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/klxpu_accelerator.py) | 昆仑芯 XPU 后端：#9042 删除 default-deny 覆写后继承 CUDA 的 opt-in |
| [tests/unit/v1/test_hybrid_engine_rollout_v1.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/test_hybrid_engine_rollout_v1.py) | 加速器上的 rollout 契约测试：KV 本地定尺寸、采样与 HF generate 一致性 |
| [tests/unit/runtime/rollout/test_hybrid_engine_rollout.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/runtime/rollout/test_hybrid_engine_rollout.py) | mock 化单测 + 多进程分布式测试（EOS 早停、子组活性、eager 回退） |
| [tests/unit/hybrid_engine/](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/hybrid_engine) | HybridEngine 端到端训练-生成-训练循环测试（`test_he_all.py`、`test_he_graph.py` 等） |
| [docs/code-docs/source/inference-engine.rst](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/code-docs/source/inference-engine.rst) | 官方契约文档：图捕获 decode、continuous batching、cache 统计口径 |

## 4. 核心概念与源码讲解

### 4.1 模块一：DeepSpeedHybridEngine——训练态与推理态的双向切换

#### 4.1.1 概念说明

`DeepSpeedHybridEngine` 继承自 `DeepSpeedEngine`（u2-l3 的八阶段构造在这里全部生效），额外做三件事：

1. **创建推理容器**：按 policy 扫描模型，把可加速的 Transformer 层包装成 DeepSpeed 推理实现（`new_inference_container`），但权重**不复制**——`set_params_wo_copy` 直接把容器参数指到训练权重的视图上。
2. **切换 forward**：`eval()` 时把原层的 `forward` 换成推理容器的 `forward`（或 ZeRO-3 下的按层收集包装）；`train()` 时换回来。切换是 O(层数) 的指针操作，不搬数据。
3. **ZeRO-3 参数收集**：参数平时是分片的。`generate()` 前用 `GatheredParameters` 把推理需要的参数收集成完整副本，生成完 `release_memory()` 释放，训练继续用分片。

「双向转换」发生在两个层面：**前向函数层面**（`eval`/`train` 换 forward）与**权重视图层面**（容器钩子 `transform_for_inference` / `transform_for_training`，供 QKV 交错布局等「视图不兼容」的模型族做实质性变换；基类是空实现）。

#### 4.1.2 核心流程

一次「训练 → 生成 → 训练」循环中引擎的状态迁移：

```text
train(mode=True)
  └─ 每层 orig_module.forward = 原始 forward（训练路径）
  └─ container.transform_for_training()（权重视图还原，若有）

engine.eval()
  ├─ ZeRO-3 + pin_parameters=False：orig_module.forward = _zero3_forward(i)
  │     └─ 每次前向按层 GatheredParameters（惰性收集）
  ├─ 否则：orig_module.forward = 推理容器 forward
  ├─ container.transform_for_inference()（权重视图变换，若有）
  └─ 若启用图捕获：module.forward = DecodeGraphCache 实例（见 4.4/4.5）

engine.generate(...)              # 被 module.generate 替换后的入口
  ├─ ZeRO-3 + pin_parameters=True：
  │     GatheredParameters 收集全部推理参数 → fuse LoRA → retake workspace
  │     → 真正生成 → 容器 release_memory() 释放收集副本
  └─ 非 ZeRO-3：直接生成（权重本来就是完整视图）

engine.train()                    # 换回训练 forward
engine.step()                     # 优化器更新
  └─ 非 ZeRO-3：container.reset_params() 把训练权重拷回连续推理视图
```

#### 4.1.3 源码精读

构造尾部决定图捕获是否启用——注意这里是**警告 + 降级**而不是报错（详见 4.4）：

[deepspeed/runtime/hybrid_engine.py:L64-L71](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/hybrid_engine.py#L64-L71) 先调 `validate_graph_capture_support` 检查配置与加速器能力，不支持时打印 `running without graph capture` 警告并跳过 `DecodeGraphCache` 的创建；支持时保存原 `module.forward` 并创建按位置缓存图的 `DecodeGraphCache`。

`eval()` 完成训练→推理的切换：

[deepspeed/runtime/hybrid_engine.py:L476-L490](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/hybrid_engine.py#L476-L490) 逐层替换 forward：ZeRO-3 且不 pin 参数时换成 `_zero3_forward(i)`（每次前向按层收集），否则换成推理容器的 forward；随后调用每个容器的 `transform_for_inference()`。最后若启用了图捕获，把 `module.forward` 整个换成 `self._decode_graphs`（一个可调用对象，内部自行决定捕获或 eager）。

`train()` 是严格对称的反向操作：

[deepspeed/runtime/hybrid_engine.py:L497-L507](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/hybrid_engine.py#L497-L507) 调 `transform_for_training()`、把每层 forward 换回保存的原始 `orig_fwd`，并把 `module.forward` 从图缓存换回 `self._orig_module_forward`。

ZeRO-3 + `pin_parameters=True` 时 `generate()` 的收集主路径：

[deepspeed/runtime/hybrid_engine.py:L308-L322](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/hybrid_engine.py#L308-L322) 用 `get_inactive_params` 找出当前未收集的参数，套 `GatheredParameters` 上下文收集全部层；期间按需 fuse LoRA、`retake_inference_cache()` 取回推理 workspace，然后调用 `self._generate`（真正的 HF `generate`）。生成后逐层 `release_memory()` 释放收集出来的完整副本（这是推理副本，训练分片不受影响），再 unfuse LoRA。

带 `inference_tp_size > 1` 时还要把各 rank 的输入拼接成完整 batch：

[deepspeed/runtime/hybrid_engine.py:L284-L306](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/hybrid_engine.py#L284-L306) 在 `mp_group` 上 `all_gather_into_tensor` 把每个 rank 的输入聚合为 TP 完整输入，生成结束后再按本 rank 的切片切回输出。

`step()` 在优化器更新后同步推理视图：

[deepspeed/runtime/hybrid_engine.py:L511-L521](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/hybrid_engine.py#L511-L521) 非 ZeRO-3 时对每个容器调 `reset_params()`——容器基类的注释说明其语义是「把训练侧 FP16 权重拷回连续的推理视图」，仅当容器参数不能直接用于推理时才需要（如 QKV 交错布局）。

引擎还内置了阶段耗时统计，`eval()` 时打印一次分解：

[deepspeed/runtime/hybrid_engine.py:L466-L472](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/hybrid_engine.py#L466-L472) 输出 `E2E latency / Gather latency / Generate time / Training time / Others` 五段占比——`Gather` 是参数收集、`Generate` 是生成、`Training` 是两次生成之间的训练累计。

#### 4.1.4 代码实践

**实践目标**：亲眼看一次 train→generate→train 的切换，并拿到官方计时分解。

1. 阅读 [tests/unit/hybrid_engine/test_he_all.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/hybrid_engine) 中任意一个用例，确认测试的循环骨架就是 `engine.train() → train_batch/step → engine.eval() → engine(input_ids) / generate → engine.train()`。
2. 以它为模板写一个最小脚本（配置 `hybrid_engine.enabled=true`，`zero_optimization.stage` 分别试 0 和 3、`pin_parameters` 试 True/False），跑两轮「训练 5 步 + 生成一次」。
3. 观察第二次 `engine.eval()` 时 stdout 打出的 `|E2E latency=... |Gather latency=... |Generate time=... |Training time=...` 分解行，对比 ZeRO-3 与 ZeRO-0 下 `Gather latency` 的占比差异。
4. 在 `eval()` 前后各打印一次 `type(engine.module.transformer.h[0].forward)`（以 GPT2 为例），确认 forward 对象被换成了容器方法/图缓存，`train()` 后又换回原对象。

**预期结果**：ZeRO-0 下 Gather 占比接近 0；ZeRO-3 + `pin_parameters=True` 下出现可观的 Gather 段，且生成结束后显存回落（`release_memory` 生效）。若你的环境不支持图捕获，日志会先出现 `running without graph capture` 警告（4.4 会解释）。实际数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `set_params_wo_copy` 要「指视图」而不是「拷贝」？什么情况下必须拷（提示：`reset_params` 的注释）？

**答案**：指视图让训练权重与推理权重共享存储，生成用的是**最新**训练结果，零拷贝开销。但当推理实现要求的内存布局与训练布局不兼容（如 QKV 交错排列）时，无法直接视图共享，只能 `transform_for_inference` 变换视图、`reset_params` 在每次 `step()` 后把训练权重**拷贝**进连续推理视图。

**练习 2**：ZeRO-3 下 `pin_parameters=True` 与 `False` 的生成路径差异是什么？

**答案**：`True` 时 `generate()` 入口一次性 `GatheredParameters` 收集全部层、整个生成期间参数常驻（省去每步收集），生成完统一 `release_memory()`；`False` 时 `eval()` 把每层 forward 包成 `_zero3_forward`，每次前向按层惰性收集。前者省通信、费显存；后者反之。

**练习 3**：`generate()` 里 `retake_inference_cache` 什么时候会失败重试？

**答案**：配置了 `release_inference_cache=true` 时，上一轮生成结束释放了推理 workspace；本轮要重新申请。若首次 `retake_workspace` 失败（显存碎片），会 `gc.collect()` + `empty_cache()` 后重试一次，再失败则抛 `RuntimeError`。

### 4.2 模块二：rollout 包——接口层与 HybridEngineRollout 的路径分发

#### 4.2.1 概念说明

rollout 包把「生成」从训练循环里抽象出来：训练器只面对三个数据类和一个 ABC，不关心背后是 hybrid engine 还是（未来的）独立推理后端。

- `RolloutRequest`：左填充的 prompt（`prompt_ids [B, T_p]` + 注意力掩码）；左填充保证因果生成能自然接在真 token 之后。
- `SamplingConfig`：采样旋钮（`max_new_tokens / temperature / top_p / top_k / n_samples_per_prompt / continuous_batch_size`）。
- `RolloutBatch`：输出 = prompt 与响应的拼接（右填充），带 `response_start_idx` 标出响应起点，供下游直接构造训练目标（u6-l7 的 `ResponseTokenBatch.from_rollout` 就是消费者之一）。
- `RolloutEngine.sync_weights`：对独立后端要推送权重；hybrid engine 与训练同址、直接读模型权重，所以是 no-op。

#### 4.2.2 核心流程

`HybridEngineRollout.generate()` 的分发逻辑：

```text
generate(request, sampling)
  ├─ sampling.continuous_batch_size 有值？
  │    ├─ 是 → 图捕获可用？ → 是：_generate_continuous_graph（固定容量图）
  │    │                     → 否：_generate_continuous（eager 连续批处理）
  │    └─ 否 ↓
  ├─ use_graph_capture 且 is_graph_capture_supported()？
  │    ├─ 是 → _generate_graph（静态 cache + 图捕获，绕过 HF generate）
  │    └─ 否 → HF module.generate()
  │            ├─ ZeRO-3：装 _GlobalEosStoppingCriteria + eos_token_id=None + synced_gpus=False
  │            └─ 非 ZeRO-3：原生 eos / 默认 synced_gpus
  └─ 收尾：_pad_after_eos 恢复「首个 EOS 后全 pad」语义 → 组装 RolloutBatch
```

#### 4.2.3 源码精读

接口层三个数据类：

[deepspeed/runtime/rollout/base.py:L27-L58](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/base.py#L27-L58) 定义 `SamplingConfig`（注意 `top_k <= 0` 表示关闭 top-k；`continuous_batch_size=None` 表示不走连续批处理）与 `RolloutRequest`（构造期即校验形状）。

分发入口与图捕获 opt-in 检查（第一条生成路径）：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L208-L215](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L208-L215) 若配置了图捕获但 `get_accelerator().is_graph_capture_supported()` 为 False，`warning_once` 打一条警告后**降级为 eager 生成**——注释明确说这是与 HybridEngine 图缓存及 continuous 路径相同的 opt-in 策略：只暴露图 API 的后端留在 eager 路径。

ZeRO-3 的 eager 生成参数组装：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L230-L254](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/hybrid_engine_rollout.py#L230-L254) 非图路径先取 `zero3_sync`（见 4.8）；ZeRO-3 时构造 `stopping_criteria=[_GlobalEosStoppingCriteria(...)]`、`eos_token_id=None`（decode 循环不看本 rank 的 EOS）、`synced_gpus=False`（关掉 Transformers 依默认 world size 的世界级 all-reduce）；采样参数统一由 `_hf_sampling_kwargs` 生成（见 4.7）。

收尾恢复常规语义：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L265-L289](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L265-L289) 注释解释：ZeRO-3 生成期间故意忽略 EOS，让所有 rank 跑同样多的参数收集通信；返回前 `_pad_after_eos` 把每条响应在**首个 EOS 之后**全部替换为 pad（保留首个 EOS 本身），再据此重建注意力掩码并组装 `RolloutBatch`。

#### 4.2.4 代码实践

**实践目标**：不写 RL 训练器，直接用 mock 引擎跑通 `HybridEngineRollout.generate()` 的 eager 路径。

1. 仿照 [tests/unit/runtime/rollout/test_hybrid_engine_rollout.py:L177-L217](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/runtime/rollout/test_hybrid_engine_rollout.py#L177-L217) 的写法：定义一个带 `_supports_cache_class = True` 的两行小模型（其 `forward` 用 `past_key_values.update` 写 KV、按 cache 内容决定下一个 token），用 `SimpleNamespace(module=model)` 冒充 engine、`SimpleNamespace(pad_token_id=0, eos_token_id=2)` 冒充 tokenizer。
2. 构造 `RolloutRequest(torch.tensor([[1,2,3],[1,2,4]]), 全 1 掩码)`，调 `rollout.generate(request, SamplingConfig(max_new_tokens=3, temperature=0))`。
3. 打印 `output.input_ids / output.attention_mask / output.response_start_idx`，对照你模型的确定性规则手工推出预期序列。

**预期结果**：`input_ids` 形状 `[2, 6]`，`response_start_idx == [3, 3]`；若某行在第 2 个新 token 出 EOS，其后位置为 pad 且 `attention_mask` 对应位置为 0。CPU 即可运行（该测试本身就是纯 CPU 的）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RolloutRequest` 约定左填充？

**答案**：因果语言模型从序列右端续写。prompt 右对齐（真 token 在右边缘）时，第一个生成 token 恰好接在最后一个真 token 后；若右填充，pad 会夹在 prompt 与响应之间，位置编码与注意力都要特判。

**练习 2**：`sync_weights` 为什么在 hybrid engine 里是 no-op？

**答案**：rollout 与训练共享同一个进程内的模型对象，`generate` 时读到的就是最新权重，不存在「推送到远端推理服务」这一步。这正是一体化引擎相对「训练集群 + 推理集群」架构省掉的那份数据搬运。

**练习 3**：`n_samples_per_prompt > 1` 时输入输出如何变化？

**答案**：输入端 `repeat_interleave` 把每条 prompt 复制 n 份；输出 `RolloutBatch` 的 batch 维变为 `B*n`，每份样本独立解码（若开启 `use_shared_prefill` 则共享一次 prompt 前向、复制其 KV cache，见 `prepare_shared_prefill`，但它与图捕获、ZeRO-3、continuous batching 互斥）。

### 4.3 模块三：continuous batching 调度循环——ContinuousBatchScheduler 与缓存行管理

#### 4.3.1 概念说明

普通批处理生成要等批内**最慢**的序列出完才能接下一批，batch 内计算浪费严重。continuous batching 的思路是：固定一个「槽位容量」（`continuous_batch_size`），完成的请求立刻退休、释放槽位，排队中的新请求马上 prefill 进空槽——解码步永远尽量满批。

DeepSpeed 的实现刻意停在「调度器 / cache」边界：调度器只报告**哪些旧行存活（keep_slots）、哪些请求退休（retired）、哪些新请求获准进入（admitted）**；模型后端（rollout 主体）自己负责 prefill 与 decode。这与 vLLM / SGLang 的调度-cache 分层同构，但不照抄它们的后端内核（官方文档明言）。

#### 4.3.2 核心流程

```text
submit(全部请求) → schedule() 得到首个 update
while update.active:
  ├─ 有 survivor（存在退休）：
  │    cache.compact(keep_slots)          # 存活行上移，行地址尽量稳定
  │    attention_mask 行同步重排
  │    （可选/被迫）trim_left(dead_prefix) # 回收最左死前缀列
  ├─ 无 survivor：cache.reset() 全清
  ├─ _continuous_prefill：新请求 prefill，KV 拷入目标行尾
  ├─ decode：幸存请求各吃 1 token 前向（eager 或 graph replay）
  ├─ 选下一个 token（_token_selector，见 4.7）；命中 EOS → finished
  └─ update = scheduler.advance(finished)  # 计数 +1，超预算的也退休
```

#### 4.3.3 源码精读

调度器的一次状态转移：

[deepspeed/runtime/rollout/continuous_batching.py:L88-L116](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/continuous_batching.py#L88-L116) `schedule()` 接收显式完成的请求 id，从 active 中筛出 survivor 与其槽位号（`keep_slots`）、算出 retired，再用空出的槽位数从 pending 队列 FIFO 放行 `admitted`。`advance()`（[L118-L133](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/continuous_batching.py#L118-L133)）代表「全体 active 各走一个 decode 步」，生成计数达到 `max_new_tokens` 的请求也视为完成，随后转回 `schedule`。

`ContinuousBatchUpdate` 的语义：

[deepspeed/runtime/rollout/continuous_batching.py:L24-L46](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/continuous_batching.py#L24-L46) 文档字符串写明契约：调用方应先用 `keep_slots` 压实 KV cache，再把 `admitted` 的请求 prefill 进压实后批的**尾部空行**（`admitted_slots` 属性直接给出这些行号）。

eager 连续批处理主循环中的缓存管理与解码：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L426-L466](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L426-L466) 有 survivor 时 `cache.compact(keep_slots)`、注意力行重排；`enable_cache_trimming` 开启或缓存将耗尽时计算 `dead_prefix` 并 `cache.trim_left` 回收列（拷贝活列、清零尾部、整体左移 `write_positions` 与 `cache_position`）；全批退休则整体 `cache.reset()`。

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L516-L552](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L516-L552) decode 段：把各幸存请求的下一 token 沿 batch 维拼起来，`write_positions` 填当前列、注意力掩码点亮该列，经 `_call_model` 前向（透传 `past_key_values=cache, cache_position, position_ids`），随后对 `logits[:, -1, :]` 做逐请求的 repetition penalty 与 token 选择。

prefill 的「只取最后位置 logits」优化：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L1256-L1263](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L1256-L1263) prefill 只采样一个 token，却会物化 `[batch, prompt_len, vocab]` 的完整 logits——这是 rollout 最大的激活。`_last_logits_kwargs` 在模型声明支持 `logits_to_keep` 参数时请求只算最后一个位置。

#### 4.3.4 代码实践

**实践目标**：用三个请求挤两个槽位，观察退休-再填充循环。

1. 运行（或精读）[tests/unit/v1/test_hybrid_engine_rollout_v1.py:L102-L126](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/test_hybrid_engine_rollout_v1.py#L102-L126)：三条 prompt、`continuous_batch_size=2`——注释指出这个组合刻意迫使第三条请求填进一个**已分配过的**缓存行。
2. 开 `HybridEngineRolloutConfig(enable_profiling=True)` 重新跑，打印 `rollout.get_last_profile()`，重点看 `num_prefill_forwards`（每个获准批次一次）、`num_decode_forwards`（有幸存行的解码步数）、`active_batch_size`（同时活跃峰值）。
3. 再打印 `rollout.get_last_continuous_stats()`，记录 `cache_capacity / peak_cache_length / trim_count`。

**预期结果**：`num_prefill_forwards >= 2`（首批 2 条 + 后补的 1 条），`input_ids` 与逐请求独立生成的期望序列一致（测试断言 `_local_kv_tokens` 的手工推导）。逐步数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么调度器不直接管理 KV cache？

**答案**：分层解耦。调度器只做纯逻辑（谁进谁出），缓存管理依赖具体 cache 实现的行/列语义（compact、trim、地址稳定性）。分成两层后，eager 路径与图捕获路径可以共用同一个调度器、配不同的 cache 管理策略。

**练习 2**：`enable_cache_trimming=False`（默认）时还会 trim 吗？

**答案**：会，但只在「不 trim 缓存就要耗尽」时兜底回收一个死前缀（`elif cache_position >= max_cache_len` 分支）；开启后则按策略周期性 trim。官方文档同样说明：trim 需要拷贝活列，默认关闭以省这次拷贝。

**练习 3**：为什么 continuous batching 要求「所有请求等宽 prompt、每请求一行、每请求一个样本」？

**答案**：都是当前实验性实现的简化：等宽使 `cache_position`（物理列游标）可以全局共享；一行一请求让「槽位 ↔ 缓存行」一一对应；单样本避免同一 prompt 占多行造成的行语义混乱。`_validate_continuous_inputs` 把这些约束逐条抛 `ValueError`。

### 4.4 模块四：图捕获的加速器显式 opt-in 与 eager 回退（含 KLXPU #9042）

#### 4.4.1 概念说明

「后端有图 API」≠「后端的图捕获与本引擎兼容」。捕获会把 host 端检查、stream 同步、动态形状全部冻结，任何一处行为不对都会**静默产出错误结果**。所以 DeepSpeed 把图捕获做成加速器能力位（u3-l1）：

- 抽象基类默认 `False`——只暴露图 API 的后端留在 eager 路径；
- 后端**验证通过后显式覆写** `True`，且必须先实现 `is_current_stream_capturing`（静态 KV cache 靠它在捕获期间跳过非法的 host 端边界检查）；
- 支持面是**演进的**：KLXPU（昆仑芯 XPU，经 torch_xmlir 伪装成 torch.cuda）最初在子类显式 default-deny；#9042 完成验证后**删除了覆写**，从此继承 CUDA 的 opt-in，随 CUDA 一起进入图捕获路径。

上层的三处检查点全部走这一个接口，且**不支持时一律 warning + eager 回退**，绝不报错中断训练。

#### 4.4.2 核心流程

```text
启用图捕获的请求（enable_graph_capture / use_graph_capture=True）
  ├─ HybridEngine 内核注入路径：
  │    validate_graph_capture_support(hybrid_config, zero_stage)
  │      ├─ accelerator 不可用或 is_graph_capture_supported() == False → 返回原因
  │      ├─ zero_stage == 3        → 收集缓冲每次 generate 都换，图会读到旧地址
  │      ├─ release_inference_cache → 释放的正是图写入的 workspace
  │      └─ inference_tp_size > 1  → 不支持
  │    返回原因 → logger.warning → 不建 DecodeGraphCache（eager forward 照常）
  └─ rollout 两条生成路径（普通 + continuous）：
       not is_graph_capture_supported() → warning_once → use_graph_capture = False
```

#### 4.4.3 源码精读

能力位的默认与前置要求：

[accelerator/abstract_accelerator.py:L218-L229](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/abstract_accelerator.py#L218-L229) 注释写明契约：后端显式 opt-in，且**只有在实现了 `is_current_stream_capturing` 之后**才许 opt-in——静态 KV cache 依赖它跳过捕获期间非法的 host 端检查。默认返回 `False`，`is_current_stream_capturing` 默认也返回 `False`。

CUDA 的 opt-in 声明：

[accelerator/cuda_accelerator.py:L291-L295](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cuda_accelerator.py#L291-L295) `is_graph_capture_supported` 返回 `True`，`is_current_stream_capturing` 直接映射 `torch.cuda.is_current_stream_capturing()`。

KLXPU 的演进（#9042）：

[accelerator/klxpu_accelerator.py:L12-L35](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/klxpu_accelerator.py#L12-L35) 类头注释说明 KLXPU 经 torch.cuda 自我呈现，设备/流/RNG/内存管理全部继承 CUDA；子类只覆写计时（`use_host_timers`，CUDA 事件在该硬件上恒返回 0）、Triton 支持等差异点。此处**不再有** `is_graph_capture_supported` 覆写——#9042 删掉了那个返回 False 的显式 default-deny（连同 `tests/unit/accelerator/test_accelerator_graph.py` 中固定该行为的测试 `test_klxpu_keeps_explicit_graph_capture_opt_out`；该文件现存的 `test_graph_capture_replay_roundtrip` 与 `test_graph_ops_run_eagerly_without_graph_support` 两个测试都不再提及 KLXPU），KLXPU 由此继承 CUDA 的 opt-in。这演示了能力位模型的两面：拒绝要显式（不能因为「恰好继承 CUDA」就隐式开启未验证的路径），开启也要显式（验证完成后删掉拒绝即恢复继承）。

rollout 侧的第二、三处检查：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L208-L215](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L208-L215) 与 [L344-L350](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L344-L350) 普通 generate 与 continuous 路径各自独立检查；后者注释强调「eager continuous 路径是完整的回退，所以保留它而不是中止」。

HybridEngine 内核注入路径的配置级校验：

[deepspeed/runtime/hybrid_engine_graph.py:L198-L218](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/hybrid_engine_graph.py#L198-L218) `validate_graph_capture_support` 依次检查加速器能力、ZeRO stage 3（推理容器无持久权重，图会反复回放捕获时的旧缓冲——「静默错而不是慢」）、`release_inference_cache`、`inference_tp_size > 1`，返回第一个不满足的原因字符串；调用方（[hybrid_engine.py:L64-L71](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/hybrid_engine.py#L64-L71)）据此打警告并放弃建图。

回退行为的契约测试：

[tests/unit/runtime/rollout/test_hybrid_engine_rollout.py:L173-L217](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/runtime/rollout/test_hybrid_engine_rollout.py#L173-L217) 用 `patch.object(get_accelerator(), "is_graph_capture_supported", return_value=False)` 模拟未 opt-in 的后端，断言：图路径函数**从未被调用**、`warning_once` **恰好一次**、输出仍正确（eager 全量生成）。

#### 4.4.4 代码实践

**实践目标**：在 CPU 上复现「后端未 opt-in → 警告 + eager 回退」全过程。

1. 运行该契约测试：`pytest tests/unit/runtime/rollout/test_hybrid_engine_rollout.py::test_continuous_generation_falls_back_to_eager_without_graph_capture_opt_in -q`（纯 CPU，模型是文件内定义的 mock）。
2. 在你自己的脚本里复刻：monkeypatch `get_accelerator().is_graph_capture_supported` 为 `False`，配 `HybridEngineRolloutConfig(use_graph_capture=True)` 跑一次生成，用 `caplog` 或日志观察警告恰好出现一次；第二次 generate 不应再打（`warning_once` 语义）。
3. 若本机是 CUDA/NPU：`python -c "from deepspeed.accelerator import get_accelerator; print(get_accelerator().is_graph_capture_supported())"` 打印当前后端的答案，并解释 KLXPU 机器上现在会打印什么、为什么。

**预期结果**：测试通过；警告只出现一次；CUDA/NPU 打印 `True`；KLXPU 在 #9042 之后同样打印 `True`（继承 CUDA）——第 3 步的 KLXPU 部分**待本地验证**（需昆仑芯硬件）。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接用「try 捕获、失败再 eager」的运行时探测？

**答案**：捕获失败常常不是异常而是**静默错误**（例如 host 端检查在捕获期间行为不定、回放读到旧缓冲）。能力位把「验证过」这个信息前置到后端声明里，运行时只做一次布尔判断，成本与风险都最低。运行时捕获异常确实也会触发 `_fall_back`（`DecodeGraphCache` 内），但那是第二道防线，不是准入策略。

**练习 2**：ZeRO-3 为什么被 `validate_graph_capture_support` 拒绝，而 rollout 的 `_generate_graph` 路径却又支持 ZeRO-3 模型？

**答案**：两者对象不同。前者针对**内核注入容器**：ZeRO-3 下容器没有持久权重，每次 generate 参数收进新缓冲，旧图回放旧地址。后者作用于 HF 原生模块：参数在生成期间保持完整（ZeRO-3 先收集、生成期间不更新），静态缓冲地址稳定，捕获安全。约束记录在 `validate_graph_capture_support` 的注释与文档里。

**练习 3**：`warning_once` 与普通 `warning` 的区别为什么在这里重要？

**答案**：rollout 每个 RL 迭代都会调用 generate；未 opt-in 是环境属性而非偶发错误，每个 step 重复刷屏会淹没真正有用的日志。`warning_once` 保证同类警告只提醒一次。

### 4.5 模块五：静态图 decode——预构建 4-D 加性掩码与捕获协议

#### 4.5.1 概念说明

decode 步的瓶颈是 launch 开销：一次 decode 前向要发射上千个 kernel，host 还没排完队 GPU 就算完了。把 decode 前向录成图、每步一次 replay，launch 开销归零。

但捕获有两条铁律，衍生出两个设计：

1. **host 代码在捕获时会执行，回放时完全不执行**。因此序列长度计数器这类「由 host 推进、Python 不可达」的状态不能依赖回放推进——`DecodeGraphCache`（内核注入路径）干脆**每个 decode 位置捕获一张图**，把正确偏移烘焙进各自的图；而 rollout 的 `_generate_graph` 把写位置放进**固定地址的张量**（`DeepSpeedStaticCache.write_position`），回放前由 host 原地更新。
2. **捕获期间不能有任何 stream 同步/host 查询**。Transformers 从 2-D `attention_mask` 构建SDPA 掩码时会做 host 端检查（如 `padding_mask.all()` 把设备张量同步到 host 判断），这会破坏捕获。所以捕获的前向直接接收**预构建好的 4-D 加性掩码** `static_causal_attn`：有效位置为 0、无效位置为 dtype 最小值，每步只由 host 「点亮一列」后原地改写。

#### 4.5.2 核心流程

`_generate_graph`（固定批、非 continuous）的完整协议：

```text
1. prefill：HF StaticCache + 正确注意力语义的 2-D mask，一次前向 → 首个新 token
2. 把 prefill KV 拷入 DeepSpeedStaticCache（各层惰性定尺寸，见 4.6）
3. 分配静态缓冲：static_token / static_attn(2-D) / static_causal_attn(4-D) /
   static_pos / static_cache_pos / static_pos_ids / write_pos —— 地址终身固定
4. 清空 module 的 forward hooks（它们会同步 stream，捕获期非法）
5. side stream 上 3 次 warmup 前向（初始化惰性分配、让内存池就绪）
6. capture_to_graph 上下文里跑一次前向 → 图 + 静态输出 logits
7. 恢复 hooks
8. decode 循环（max_new_tokens-1 次）：
   拷入新 token → write_pos/cache_pos/pos_ids 填当前位置
   → static_attn 点亮一列 → 由它重算 4-D 加性掩码
   → replay_graph → 从 static_logits[:, -1, :] 选下一个 token
```

continuous 图路径（`_generate_continuous_graph`）的差异：槽位与物理缓存行**终身绑定**，退休行只清注意力掩码、新请求 prefill 进原行——保证捕获的图永远看到同样的 batch 形状与张量地址；`static_write_positions.fill_(max_cache_len - 1)` 让非解码行写到无害位置。

#### 4.5.3 源码精读

4-D 掩码的构造与动机：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L1490-L1498](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L1490-L1498) 注释直接给出原因：Transformers 用 host 端检查（`_ignore_causal_mask_sdpa` 里的 `padding_mask.all()`）从 2-D mask 构建 SDPA 掩码，这些检查会同步 stream、使图捕获失效；因此捕获的前向接收预构建的 4-D 加性掩码，与 continuous 图路径一致，每个 decode 步点亮一列。

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L834-L837](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L834-L837) `_update_static_causal_attention` 把 2-D 掩码广播成 `[B,1,1,T]`，再 `neg→+1→乘 dtype 最小值`——掩码为 1 的位置变 0（不加惩罚）、为 0 的位置变 \(-\infty\)（近似），一次张量运算完成加性掩码更新，无任何 host 参与。

warmup 与捕获（固定批路径）：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L1502-L1536](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L1502-L1536) 先保存并清空 `module._forward_pre_hooks/_forward_hooks`（注释：它们会同步，捕获期非法）；在 side stream 上跑 3 次 warmup；随后 `capture_to_graph` 上下文捕获一次前向；`finally` 恢复 hooks。

decode 循环：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L1541-L1563](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L1541-L1563) 每步：`static_token.copy_`、`write_pos/static_cache_pos/static_pos_ids` 全部 `fill_` 当前位置、`static_attn[:, pos] = 1` 点亮新列、重算 4-D 掩码、`replay_graph`，然后从静态 logits 里选 token；本批全部命中 EOS 后只补 pad 不再回放。

continuous 图路径的「先初始化再 warmup」：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L849-L871](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L849-L871) `_capture_continuous_graph` 在 warmup **之前**、若还有未初始化的 KV 层，先在**当前 stream** 上跑一次前向完成各层分配——注释点明原因：避免张量落在 warmup 的 side stream 上（分配流的归属会影响图回放时的内存池）。

内核注入路径的 `DecodeGraphCache`（对照）：

[deepspeed/runtime/hybrid_engine_graph.py:L75-L105](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/hybrid_engine_graph.py#L75-L105) `begin_sequence` 在任何 decode 步之前**一次性**决定本序列是否用图——因为 eager 步会推进 host 侧序列计数器而回放不会，两者混用会读到过期序列长度、写坏 KV cache。[L183-L195](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/hybrid_engine_graph.py#L183-L195) `decode_steps_from_generate_kwargs` 只有 `max_new_tokens == min_new_tokens`（长度钉死）才可用图：开放式生成长度可能超过已捕获位置，而那时回放已发生、无法安全回退。

#### 4.5.4 代码实践

**实践目标**：验证「一次捕获、N-1 次回放」与 4-D 掩码逐列点亮。

1. 精读并（在有图支持的硬件上）运行 [tests/unit/v1/test_hybrid_engine_rollout_v1.py:L79-L99](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/test_hybrid_engine_rollout_v1.py#L79-L99)：`_count_replays` monkeypatch 了 `accelerator.replay_graph` 计数；断言 `new_tokens=4` 时恰好回放 `new_tokens - 1 = 3` 次（首个 token 由 prefill 产生）。
2. 无图硬件时改做源码阅读实践：在 `_generate_graph` 的 decode 循环里加一行打印（示例代码，仅本地实验）`print(step, static_attn[0, :pos+1].tolist())`，对照源码解释为什么掩码每步恰好多一个 1。
3. 对照 [tests/unit/runtime/rollout/test_hybrid_engine_rollout.py:L1782-L1799](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/runtime/rollout/test_hybrid_engine_rollout.py#L1782-L1799)（`test_static_graph_capture_receives_a_prebuilt_4d_mask`）：mock 加速器断言捕获到的前向收到的 `attention_mask` 是 4-D。

**预期结果**：replay 计数 == `max_new_tokens - 1`；捕获调用里的 mask 形状为 `[B, 1, 1, max_len]`。CPU 环境（`create_graph()` 返回 None）这些测试会 `pytest.skip`，属预期行为；GPU 上数值**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 warmup 要在 side stream 上、还要先 `wait_stream` 再反向 `wait_stream`？

**答案**：CUDA 图捕获要求捕获流与默认流无未决依赖错乱；标准协议是 side stream 先等待当前流（继承已有工作），warmup 若干次（完成惰性分配、预热缓存分配器），然后当前流等待 side stream（确保 warmup 全部落定）再开始捕获。

**练习 2**：`static_attn[:, pos] = 1` 与 `_update_static_causal_attention` 为什么拆成两步，而不是直接改 4-D 掩码？

**答案**：2-D 掩码是「事实源」（compact/trim/退休清零都作用于它），4-D 加性掩码是派生视图。每次全量重算 `[B,1,1,T]` 只有一次广播加一次乘法，却免去手工维护两份状态的一致性——continuous 图路径里退休行清零也只需改 2-D 源再统一重算。

**练习 3**：`DecodeGraphCache` 为什么每个位置一张图，而不是一张图 + 张量位置？

**答案**：内核注入路径的推理内核从 host 侧计数器（`InferenceContext::current_tokens()`）读当前序列长度并作为**launch 参数**传给 kernel——launch 参数在捕获时被冻结。所以每个位置需要各自的图把正确偏移烘焙进去；模块 docstring（[hybrid_engine_graph.py:L10-L25](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/hybrid_engine_graph.py#L10-L25)）完整解释了这两条约束。

### 4.6 模块六：DeepSpeedStaticCache——写位置外部化、get_query_offset 与惰性定尺寸

#### 4.6.1 概念说明

`DeepSpeedStaticCache` 派生自 HF `StaticCache`，改了一个关键点：**写位置由调用方经共享张量外部供给**（`set_write_position`），而不是层内自增计数器。HF 的 `StaticLayer.update()` 内部维护 `cumulative_length`，捕获时被冻结、回放时不前进——KV 会写错列并静默产出错误 logits。外部张量地址固定，host 在每次回放前原地更新，回放读到的永远是当前值。写位置可以是标量（固定批）或每行一个（continuous batching，支持 -1 表示未激活行）。

本周期（#9008）的另一半是**AutoTP 下的惰性定尺寸**：AutoTP（u6-l1）会把 KV head 按 rank 切分，全局 `module.config.num_key_value_heads` 是 4 而本 rank 可能只有 2 个 head——从 config 预分配会得到错误形状（拷贝失败或静默广播）。现在三条 decode 路径全部改为**从本 rank 的 prefill 张量读形状**惰性初始化：

\[ \text{KV bytes} = 2 \times L \times B \times H_{kv}^{\text{local}} \times T \times d_{head} \times s_{dtype} \]

（2 = keys+values 两份；\(L\) 层数、\(B\) 缓存行数、\(H_{kv}^{\text{local}}\) 本 rank KV head 数、\(T\) `max_cache_len`、\(d_{head}\) head 维、\(s_{dtype}\) 每元素字节数。）

#### 4.6.2 核心流程

三条 decode 路径的初始化策略对照：

| 路径 | 初始化时机 | 形状来源 | 容量（batch 维） |
| --- | --- | --- | --- |
| plain graph（`_generate_graph`） | prefill 后逐层 `lazy_initialization(hf_keys, hf_values)` | HF prefill cache 的本 rank 张量 | prefill 的 batch |
| continuous eager（`_generate_continuous`） | 首个 admitted 批 prefill 时经 `_continuous_cache_layer` | prefill keys/values | `max_batch_size`（槽位数，`lazy_initialization` 新增可选参数） |
| continuous graph（`_generate_continuous_graph`） | 捕获前：当前 stream 先跑一次 decode 前向完成各层分配 | 静态输入的 decode 前向 | `max_batch_size` |

`memory_bytes()` 的口径：每层未初始化记 0（未分配即无内存）；continuous 统计再把每行注意力掩码与写位置张量加上（`_continuous_cache_memory_bytes`），eager 与 graph 两种解码报告同一口径。

#### 4.6.3 源码精读

写位置外部化的写入路径：

[deepspeed/utils/static_cache.py:L100-L113](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/utils/static_cache.py#L100-L113) 每行写位置（1-D 张量）时，KV 写入用 `keys[row_indices, :, cache_position, :] = key_states[:, :, 0, :]` 的按行散射；[L106-L108](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/utils/static_cache.py#L106-L108) 在做越界检查前先问 `get_accelerator().is_current_stream_capturing()`——捕获中跳过 host 检查（把设备张量搬回 host 判断会破坏捕获），这正是 4.4 里「opt-in 前必须实现该接口」的原因。

惰性初始化与容量参数：

[deepspeed/utils/static_cache.py:L60-L86](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/utils/static_cache.py#L60-L86) `lazy_initialization` 从传入的 key/value 张量取 dtype、device、head 数与 head 维，分配 `[max_batch_size, num_heads, max_cache_len, head_dim]`；`max_batch_size` 参数允许容量大于首个 prefill 的 batch（continuous eager 需要槽位数）；分配后 `torch._dynamo.mark_static_address` 标记静态地址。

构造函数不再无条件预分配：

[deepspeed/utils/static_cache.py:L216-L236](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/utils/static_cache.py#L216-L236) 只有显式给出 `dtype`、`device` 且 `batch_size > 0` 才走 `early_initialization`；rollout 的三条路径如今都只传 `config` 与 `max_cache_len`（如 [hybrid_engine_rollout.py:L404](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L404) 的调用，旁边注释「层尺寸由第一个 prefill 决定，因为 AutoTP 按 rank 切分 KV head」）。

continuous eager 的按槽位定尺寸：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L1060-L1065](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L1060-L1065) `_continuous_cache_layer`：层未初始化时用 **prefill 的本 rank KV 张量**和 `max_batch_size=attention_mask.shape[0]`（槽位数）调 `lazy_initialization`。

plain graph 的 prefill 张量定尺寸：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L1476-L1483](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L1476-L1483) 注释「从 prefill 推断每层本地 KV 形状，再捕获图」；未初始化层用 HF prefill cache 的 keys/values 惰性初始化后，把前 `prompt_len` 列拷入。

continuous graph 的「先初始化再 warmup」已在 4.5 引用（[L849-L858](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L849-L858)）；其缓存构造同样只传 `max_cache_len`（[L783-L784](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L783-L784)，注释「捕获从模型本地 KV 张量取各层尺寸」）。

`get_query_offset`（Transformers 5.20 兼容）：

[deepspeed/utils/static_cache.py:L132-L136](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/utils/static_cache.py#L132-L136) 层级实现：未设写位置返回 0，否则返回写位置本身。注释强调与 `get_seq_length()` 的差一语义：掩码构建发生在**当前 KV 写入之前**，所以 offset 不含当前写（`get_seq_length` 返回 `write_position + 1`）。[L333-L340](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/utils/static_cache.py#L333-L340) 缓存级实现：每行写位置时取最大值作为共享前沿，行内有效性交给每行注意力掩码。文档（[inference-engine.rst:L172-L179](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/code-docs/source/inference-engine.rst#L172-L179)）说明这是定向兼容修复，查询它不会推进缓存、也不会把设备张量同步到 host。

`memory_bytes` 与统计口径：

[deepspeed/utils/static_cache.py:L148-L152](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/utils/static_cache.py#L148-L152) 层未初始化返回 0（惰性分配的层尚未占内存）；[L242-L244](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/utils/static_cache.py#L242-L244) 求和。[hybrid_engine_rollout.py:L1054-L1058](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L1054-L1058) 再加每行掩码与写位置的字节，注释声明 eager 与 graph 报告同一数量、graph 专属的静态 staging 缓冲不计入。

#### 4.6.4 代码实践

**实践目标**：验证「各层 KV cache 按本 rank prefill 形状定尺寸」与 memory_bytes 口径。

1. 精读 [tests/unit/v1/test_hybrid_engine_rollout_v1.py:L22-L53](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/test_hybrid_engine_rollout_v1.py#L22-L53)：`_LocalKVModel` 的 config 声明 4 个 KV head，但实际只产出 `local_heads` 个（1/2/4 参数化）——这正是 AutoTP 分片后的本地视角。断言（[L97-L99](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/test_hybrid_engine_rollout_v1.py#L97-L99)）生成结果与「全量 KV head」参考推导一致且 `model.config.num_key_value_heads` 保持 4（config 未被篡改）。
2. 有图硬件时运行 `pytest "tests/unit/v1/test_hybrid_engine_rollout_v1.py::test_static_graph_uses_prefill_kv_shape" -q`；TP=2 场景则运行 `TestAutoTPContinuousRollout::test_matches_generate_on_sharded_llama`（真实 Llama + AutoTP，两 rank），生成后打印 `engine.module` 中任一注意力层 KV cache 每层的 `keys.shape[1]`（本地 head 数）。
3. 口径验证：对照 [tests/unit/v1/test_hybrid_engine_rollout_v1.py:L353-L357](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/test_hybrid_engine_rollout_v1.py#L353-L357)：`expected_cache_bytes = 2*5*4*2 + 2*5*8 + 2*8`（2 行×5 列×fp32×(k+v) + int64 掩码 + int64 写位置），与 `stats["cache_memory_bytes"]` 用本节公式逐项核对。

**预期结果**：TP=2、config 4 head 时本地每层 `keys.shape[1] == 2`；`cache_memory_bytes` 与手算逐字节相等。GPU 数值**待本地验证**。

#### 4.6.5 小练习与答案

**练习 1**：为什么 `lazy_initialization` 要新增 `max_batch_size` 可选参数？

**答案**：continuous eager 路径里，首个 prefill 批可能只有 1 行，但缓存必须按槽位数（如 4 行）分配，否则后续 refill 无处可写。默认缺省取 `key_states.shape[0]` 保持旧行为（首帧即全量）。

**练习 2**：`get_query_offset` 与 `get_seq_length` 为什么差一？

**答案**：调用时机不同。Transformers 构建 causal 掩码在**写入当前 token 的 KV 之前**，此时可查询的位置前沿是 `write_position`（不含当前写）；而 `get_seq_length` 在写入后回答「缓存里已有多少 token」，是 `write_position + 1`。

**练习 3**：continuous graph 路径为什么不能像 continuous eager 那样在首个 prefill 时顺带初始化？

**答案**：图捕获要求全部静态张量在捕获前就存在于稳定地址。prefill 在图外、decode 在图内，若层在首个 prefill 才分配，捕获时机的缓冲归属与地址都无法保证。所以在捕获前用一次**当前 stream** 的 decode 前向强制完成分配（且不能落在 warmup side stream 上，否则分配进错误的内存池）。

### 4.7 模块七：采样快路径——_token_selector 与 HF warper 链

#### 4.7.1 概念说明

图捕获只录**前向**；下一个 token 的选择（argmax 或采样）必须留在 host——回放不执行 host 代码，而且 `torch.multinomial` 这类带随机性的操作放进图里会让采样不可控。于是三条 decode 路径（graph decode、continuous eager、continuous graph）统一用 `_token_selector(sampling)` 造出的同一个闭包选 token：

- `temperature <= 0`：greedy，`logits.argmax`；
- 否则按 **HF `generate()` 的顺序**应用 `TemperatureLogitsWarper → TopKLogitsWarper → TopPLogitsWarper`，再 `torch.multinomial(softmax(...))`。

与 `generate()` 对齐的不只是 warper 顺序，还有两处细节：**warp 与采样都在 float32 logits 上**；**repetition penalty 在 warper 之前**施加（`generate()` 的顺序）。为此 continuous 路径的 penalty 逐请求手工重放（`_continuous_next_tokens`），plain graph 路径在 `next_tokens` 闭包里做。反过来，主 `generate()` 调用（无 cache-class prefill 等）的采样参数由 `_hf_sampling_kwargs` 统一拼装，保证两条入口同一个分布。对齐的直接收益：**同一种子下三条快路径与 `model.generate()` 逐 token 一致**——有专门测试钉死这一点。

#### 4.7.2 核心流程

```text
_token_selector(sampling)
  ├─ temperature <= 0 → lambda logits: logits.argmax(dim=-1, keepdim=True)
  └─ temperature > 0：
       warpers = [Temperature?] + [TopK? top_k>0] + [TopP? top_p<1]
       def sample(logits):
           scores = logits.float()                  # generate() 同款 float32
           for w in warpers: scores = w(None, scores)
           return torch.multinomial(torch.softmax(scores, -1), 1)

每个 decode 步（图外、host 端）：
  logits（repetition penalty 已在 warp 前施加）→ select_next_tokens → 下一 token
```

#### 4.7.3 源码精读

选择器本体：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L1211-L1242](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L1211-L1242) docstring 写明契约：「按 `generate()` 应用 warper 的顺序应用 temperature/top-k/top-p，因此每条路径都与 `generate()` 抽同一个分布」。代码里 warper 的构造顺序、`logits.float()`、`torch.softmax → multinomial` 与 HF 的采样管线一一对应；注释补注「warpers 只读 scores，不读已生成 id」（所以 repetition penalty 必须自己在外面做）。

repetition penalty 的顺序与精度：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L1172-L1183](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L1172-L1183) `_apply_repetition_penalty` docstring：`generate()` 在 float32、采样 warper **之前**施加惩罚，因此这里也先 `logits.float()` 再 gather/scatter（正分数除、负分数乘），不修改原 logits。逐请求版本 [L1185-L1197](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L1185-L1197) 把每条请求的「prompt + 已生成」拼接成该请求的惩罚词表。

主 generate 入口的采样参数：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L1199-L1209](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L1199-L1209) `_hf_sampling_kwargs`：`temperature <= 0` 映射为 `do_sample=False`（greedy）；否则 `do_sample=True` 带全部三项。该函数同时服务主 `generate()` 调用（L252）与无 cache-class 模型的 prefill 回退（L1146），保证两条入口采样口径一致。

plain graph 路径的整合点：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L1448-L1455](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L1448-L1455) `next_tokens` 闭包先施加 repetition penalty（惩罚词表 = prompt + 已生成全部 token），再交给 `_token_selector` 产出的函数——prefill 后与每次 replay 后都走它。

分布一致性的回归测试：

[tests/unit/v1/test_hybrid_engine_rollout_v1.py:L515-L549](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/test_hybrid_engine_rollout_v1.py#L515-L549) `test_decode_sampling_matches_hf_generate` 参数化 \((\text{temperature}, \text{top\_k}, \text{top\_p})\) ×（graph / continuous+graph / continuous eager），同一种子下断言 rollout 输出与 `model.generate(do_sample=True, ...)` **逐 token 相等**。测试注释点出两个前提：tiny-llama 的 lm_head 乘 30 使分布尖锐（均匀分布下错误的温度/截断几乎不改变抽中的 token，测不出 bug）；continuous 用 `continuous_batch_size == batch_size` 使每步与 `generate()` 一样一次抽出全部行的 token、共享同一随机数流。

#### 4.7.4 代码实践

**实践目标**：验证三条路径的采样分布与 `generate()` 完全一致。

1. 在 CUDA 机器上运行：`pytest "tests/unit/v1/test_hybrid_engine_rollout_v1.py::test_decode_sampling_matches_hf_generate" -q`（CPU 会 skip）。
2. 手工对照 greedy 与非 greedy：用 `_tiny_llama` 同款小模型，`SamplingConfig(temperature=0.7, top_k=5, top_p=0.8)` 分别跑三条路径（`use_graph_capture` True/False × `continuous_batch_size` None/8），固定 `torch.manual_seed` 后比较输出是否与 HF `generate()` 相同；再跑 `temperature=0` 的 greedy 基线。
3. 记录各路径 `rollout.get_last_profile()["decode_forward_ms"]` 与总时长，对比「图捕获 + host 采样」与纯 eager 的耗时差。

**预期结果**：同种子下三条路径输出与 `generate()` 逐 token 相同；greedy 最快确定；图路径的 decode 前向耗时显著低于 eager（launch 开销消除）。耗时数值**待本地验证**。

#### 4.7.5 小练习与答案

**练习 1**：为什么 `torch.multinomial` 不能放进捕获的图里？

**答案**：两个原因。其一，回放不执行 host 代码，采样调度与随机数管理都在 host；其二，采样要可重现、可对照 `generate()`——放进图里随机数消耗顺序与 HF 对不齐，`test_decode_sampling_matches_hf_generate` 的一致性断言将无法成立。文档（[inference-engine.rst:L66-L76](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/code-docs/source/inference-engine.rst#L66-L76)）明确：只捕获前向，token 在每次回放后从输出 logits 里选。

**练习 2**：continuous batching 以前为什么拒绝 `temperature > 0`，现在为什么可以？

**答案**：旧实现逐请求选 token 时随机数消耗顺序与 `generate()` 不同，采样不可对照；现在 `_continuous_next_tokens` 把全部活跃请求的 last-position logits 沿 batch 维一次交给选择器，`multinomial` 一次抽全部行——与 `generate()` 的整批采样共享同一随机数流（测试注释所述前提）。

**练习 3**：`_hf_sampling_kwargs` 里 `top_k` 为什么要 `max(sampling.top_k, 0)`？

**答案**：`SamplingConfig.top_k` 默认 -1 表示「关闭」；HF `generate()` 约定 `top_k <= 0` 才是关闭（0 表示不做 top-k 过滤）。`max(-1, 0) = 0` 完成两种约定的翻译。

### 4.8 模块八：ZeRO-3 rollout 的 EOS 早停组内同步（#8390）

#### 4.8.1 概念说明

ZeRO-3 下生成不是「各 rank 独立前向」：`pin_parameters=True` 时 `generate()` 入口要在 DP 组上收集参数（4.1），`False` 时每个 decode 前向都按层收集。**通信是集体的**——组内每个 rank 必须发起同样次数、同样序列的集合通信。

若沿用 Transformers 默认的 rank 本地 EOS 停止：rank 0 的序列第 2 步出 EOS、提前返回，rank 1 要生成到第 8 步——rank 1 后续每步的参数收集在等一个已经离开生成循环的伙伴，**互相卡死**。修复分三件：

1. `eos_token_id=None`：decode 循环干脆不看 EOS，所有 rank 跑满 `max_new_tokens`；
2. `_GlobalEosStoppingCriteria`：把「是否还有本地工作」经 `GlobalWorkRemaining` 在**参数收集实际使用的组**（`optimizer.dp_process_group`）上 `all_reduce(MAX)`，全组一致停在最晚出现的 rank 本地 EOS；
3. `synced_gpus=False`：Transformers 观察到 ZeRO-3 时会依默认 world size 开启 `synced_gpus`，在 decode 循环里做**未分组的世界级 all-reduce**——若只有世界的一个 DP 子组进入 rollout（其余 rank 在干别的），这个全组通信会把子组与世界其余部分互相钉死。我们自己的 criterion 已经在正确的组上同步了，所以要显式关掉它。

生成结束后 `_pad_after_eos` 恢复常规语义（首个 EOS 保留、其后全 pad）。CUDA/HIP 图捕获 rollout 绕过 `generate()`、自管 decode 循环（eos_mask 局部判断即可，因为图路径不做参数收集），不受此改影响。

#### 4.8.2 核心流程

```text
ZeRO-3 eager 生成（每个 decode 步）：
  HF StoppingCriteria 调用 _GlobalEosStoppingCriteria(input_ids, scores)
    ├─ response_ids = input_ids[:, response_start:]
    ├─ sequence_has_eos = 每行是否出现过 EOS（本 rank 的局部事实）
    ├─ local_work_remaining = int(还存在没有 EOS 的行)
    ├─ global = work_remaining(local)        # dp_process_group 上 all_reduce(MAX)
    └─ return not global                     # 全组同样的停止决定
停止后：_pad_after_eos 按各自首个 EOS 截断（响应长度可以不同！）
```

关键认识：**停止决定是全组的，响应内容仍是各 rank 本地的**——全组跑到「最晚 EOS」那一步为止，早停的 rank 后面几步产出的是 EOS 后的 token，由 `_pad_after_eos` 清成 pad。

#### 4.8.3 源码精读

会合原语：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L81-L91](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L81-L91) `GlobalWorkRemaining` 把本地标志转成 int32 张量，在给定进程组上 `all_reduce(MAX)`；组内多于 1 进程才通信。任何 rank 还有工作 → 全组视为有工作。

组感知停止准则：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L94-L111](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L94-L111) `_GlobalEosStoppingCriteria.__call__` 先算每行的 EOS 命中，再取「还有未完行」的局部标志交给会合；返回**标量**布尔——注释说明这是 rank 全局决定，标量保持所有本地序列行为一致，并兼容旧版 Transformers 对准则结果做 Python `any()` 的用法。

组从哪里来：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L1349-L1361](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L1349-L1361) `_zero3_work_remaining` 用 `engine.zero_optimization_partition_weights()`（[deepspeed/runtime/engine.py:L1651-L1652](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/engine.py#L1651-L1652)，stage ≥ 3 为 True）判定 ZeRO-3，然后取 `engine.optimizer.dp_process_group`——docstring 点明「参数收集实际使用的组」；缺失即抛错。

generate 参数的三处联动：

[deepspeed/runtime/rollout/hybrid_engine_rollout.py:L240-L253](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L240-L253) ZeRO-3 时 `eos_token_id=None`（decode 循环不做本地早停）、`stopping_criteria` 装入组感知准则、`synced_gpus=False`；非 ZeRO-3 三者分别回到原生 EOS、`None`、`None`（`None` = 让 Transformers 自行决定）。三行注释逐一解释原因。

分布式回归测试：

[tests/unit/runtime/rollout/test_hybrid_engine_rollout.py:L1213-L1283](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/runtime/rollout/test_hybrid_engine_rollout.py#L1213-L1283) `TestZero3SynchronizedEarlyStop`：真实 ZeRO-3 引擎 + 两 rank，脚本化 forward 让 rank 0 在第 2 步、rank 1 在第 5 步「出 EOS」；断言两个 rank 的 forward 次数都是 **5**（跑到最晚 EOS），且各自的响应按本 rank 的首个 EOS 截断（`attention_mask` 计数 = 本 rank 的 target_eos_step）。

[tests/unit/runtime/rollout/test_hybrid_engine_rollout.py:L1143-L1210](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/runtime/rollout/test_hybrid_engine_rollout.py#L1143-L1210) `TestZero3SubgroupGeneration`：4 rank（Gloo/CPU），仅 rank 0-1 组成 DP 子组进入 rollout、rank 2-3 **从不进入**；用 `HfDeepSpeedConfig` 让 Transformers 能观察到 ZeRO-3（从而其 `synced_gpus` 默认本会是 True）。断言子组能独立完成生成——若没有 `synced_gpus=False`，decode 循环的世界级 all-reduce 会等 rank 2-3，死锁挂起（测试以「能返回」为断言）。

#### 4.8.4 代码实践

**实践目标**：亲眼确认「全组一致停止」与「子组活性」。

1. 运行（CPU/Gloo 即可，除 early-stop 需 2×GPU）：`pytest tests/unit/runtime/rollout/test_hybrid_engine_rollout.py::TestZero3SubgroupGeneration -q`。
2. 有 2 卡时运行 `pytest tests/unit/runtime/rollout/test_hybrid_engine_rollout.py::TestZero3SynchronizedEarlyStop -q`，对照源码理解断言 `[5, 5]`：为什么不是 `[2, 5]`？答案是 rank 0 第 2 步后的三次 forward 产出的是「EOS 后 token」，最终被 `_pad_after_eos` 清掉。
3. 单测级验证（CPU）：运行 `test_global_eos_stops_only_when_no_global_work_remains` 与 `test_zero3_generation_disables_native_synced_gpus`，阅读它们 mock 的边界（rendezvous 依次返回 True/False；`generate` kwargs 断言）。

**预期结果**：全部通过。第 1 步若把源码里 `synced_gpus=False` 改回 `None`（仅本地实验，勿提交），子组测试会挂起（可用 pytest 超时观察）——**待本地验证**。

#### 4.8.5 小练习与答案

**练习 1**：为什么在 `optimizer.dp_process_group` 上同步，而不是默认世界组？

**答案**：参数收集的集合通信发生在 DP 组上，停止准则必须与通信**同组**才有效。世界组可能包含没进 rollout 的 rank（数据并行 rollout 常见：各 DP 副本错峰生成），在世界组上 all_reduce 等于重蹈 `synced_gpus` 的死锁。`TestGlobalWorkRemainingSubgroups`（4 rank 两个子组）专门钉死「子组独立性」。

**练习 2**：`_pad_after_eos` 为什么要区分「首个 EOS 本身有效」？

**答案**：很多 tokenizer 的 `pad_token_id == eos_token_id`。若按 `!= pad` 一刀切，首个 EOS 也会被当 pad 从 `input_ids` 与掩码里抹掉，响应丢失终止符。`first_eos` 掩码保证首个 EOS 保留、其后的重复 EOS/pad 才清除（[L1422-L1428](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L1422-L1428)）。

**练习 3**：图捕获路径为什么不需要这套机制？

**答案**：`_generate_graph` 不走 `model.generate()`，自己驱动 decode 循环：每步 replay 后在 host 判 `eos_mask`，全部行完成后停止回放。它不做参数收集（ZeRO-3 参数已在生成前收集并常驻），没有集体通信对齐问题，本地 EOS 判断即可。

## 5. 综合实践

把本讲全部模块串成一个可运行的观察台（在单卡 CUDA 上完整可跑；纯 CPU 环境可跑第 1、2、5 步，图相关步骤按 4.4 的 skip 规则降级）：

**任务**：写一个 `hybrid_rollout_lab.py`，用一个 tiny Llama（`LlamaConfig(vocab_size=64, hidden_size=64, num_hidden_layers=2, num_attention_heads=4, num_key_value_heads=2)`）完成：

1. **train→generate→train**（4.1/4.2）：`deepspeed.initialize` 配 `hybrid_engine.enabled=true`、`zero_optimization.stage` 分别取 0 和 3（`pin_parameters=true`），训练 5 步 → `engine.eval()` → `HybridEngineRollout(engine, tokenizer).generate(...)` → `engine.train()`。捕获引擎自带的 `E2E/Gather/Generate/Training` 耗时分解行与 `rollout.get_last_profile()`，填一张对照表。
2. **greedy vs 采样**（4.7）：同一 prompt、同种子，`SamplingConfig(temperature=0)` 与 `(0.7, top_k=5, top_p=0.8)` 各跑一遍，比较输出与 `model.generate()` 的一致性（参照 `test_decode_sampling_matches_hf_generate` 的做法：`lm_head.weight.mul_(30)` 让分布尖锐）。
3. **opt-in 回退观察**（4.4）：monkeypatch `is_graph_capture_supported=False`，确认 `use_graph_capture=True` 时输出仍正确且 `warning_once` 只出现一次。
4. **continuous + 槽位**（4.3/4.6）：3 条请求、`continuous_batch_size=2`，开 profiling，记录 `num_prefill_forwards / num_decode_forwards / active_batch_size` 与 `cache_memory_bytes`（用 4.6 的公式手算对照）；若在 TP=2 下运行（`tensor_parallel.autotp_size=2`），打印每层 KV cache 的本地 head 数，验证按本 rank prefill 形状定尺寸。
5. **EOS 语义**（4.8）：tokenizer 配 `eos_token_id=2`，构造会早停的 prompt，ZeRO-0 下确认响应在首个 EOS 截断、其后为 pad；对照 `_pad_after_eos` 的三种情况（无 EOS / pad==eos / 多 EOS id）写三个小断言（参照 `test_pad_after_eos_*` 系列）。

**验收标准**：一张耗时分解表（ZeRO-0 vs ZeRO-3、greedy vs 采样、eager vs graph）、一张 cache 统计表（手算 vs 实测逐字节相等）、一组 EOS 语义断言全部通过。

## 6. 本讲小结

- **一体化切换**：`DeepSpeedHybridEngine` 靠「换 forward 指针 + 权重视图共享」在训练态与推理态之间低成本往返；ZeRO-3 下 `generate()` 用 `GatheredParameters` 收集参数、生成完 `release_memory`，`step()` 后 `reset_params` 同步推理视图。
- **rollout 分层**：`RolloutRequest/SamplingConfig → RolloutBatch` 三个数据类 + `RolloutEngine` ABC 把生成从训练器解耦；`HybridEngineRollout.generate` 按 `continuous_batch_size` 与图捕获可用性分发到三条路径。
- **continuous batching**：`ContinuousBatchScheduler` 只做 admission/retirement 决策（`keep_slots/retired/admitted`），KV 行压实、死前缀回收由 rollout 主体执行——调度与缓存分层，eager 与 graph 共用调度器。
- **图捕获是能力位**：三处检查点统一查询 `is_graph_capture_supported()`，未 opt-in 一律 `warning(_once)` + eager 回退；KLXPU 经 #9042 删除 default-deny 覆写后继承 CUDA 的 opt-in——支持面是后端显式决定且持续演进的。
- **捕获协议**：写位置放进固定地址张量、掩码用预构建 4-D 加性掩码（绕开 host 端 SDPA 检查的 stream 同步）、warmup 走 side stream、捕获前清 hooks；采样留在 host、`_token_selector` 按 `generate()` 的顺序与精度应用 warper，三条路径分布一致。
- **KV cache 本地化**（#9008）：`DeepSpeedStaticCache` 各层从本 rank 的 prefill 张量惰性定尺寸（AutoTP 下 config 的 head 数不可信），`get_query_offset` 支持 Transformers 5.20，`memory_bytes` 对未初始化层记 0 并统一 eager/graph 口径。
- **ZeRO-3 早停同步**（#8390）：`GlobalWorkRemaining` 在 `optimizer.dp_process_group` 上 `all_reduce(MAX)`，全组停在最晚 EOS、响应各自保留首个 EOS；`synced_gpus=False` 防止 Transformers 的世界级 all-reduce 把 DP 子组与世界其余 rank 钉死。

## 7. 下一步学习建议

- **向下游走**：rollout 产出的 `RolloutBatch` 如何变成训练目标——阅读 `deepspeed/runtime/rollout` 的 `ResponseTokenBatch.from_rollout` 与广义 JSD 损失（u6-l7 已讲词表并行算子，可顺流看它们在 RL 管线里的消费方式，文档 [inference-engine.rst:L185-L209](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/code-docs/source/inference-engine.rst#L185-L209)）。
- **向图捕获底层走**：`DecodeGraphCache` 的按位置图与 `DecodeGraphCache` 的 eager/回放不可混用约束（[hybrid_engine_graph.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/hybrid_engine_graph.py) 全文只有 218 行，值得整篇精读），以及 CUDA Graph 的内存池语义（`graph.pool()` 共享池）。
- **向加速器层回望**：结合 u3-l1 重读 `is_current_stream_capturing` 的全部调用点（`static_cache.py:L106` 是其一），理解「opt-in 前必须实现捕获中查询」这条前置约束如何落实到每个消费点。
- **对照工业推理系统**：把 `ContinuousBatchScheduler` + `DeepSpeedStaticCache`（compact/trim_left/每行写位置）与 vLLM 的 PagedAttention、SGLang 的 RadixAttention 对照——官方文档明言「镜像其调度-cache 分层而不复制其后端内核」，找出哪些设计点是刻意不同的（例如固定行地址 vs 分页）。
- **动手验证**：完成第 5 节综合实践后，尝试给 `_generate_continuous` 的 trim 路径加一个 `enable_cache_trimming` 的对照实验，量化「trim 拷贝开销 vs 缓存容量节省」的权衡——这正是 `get_last_continuous_stats()` 里 `trim_latency_ms / trim_bytes_moved` 存在的意义。
