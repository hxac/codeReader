# u4-l3 BF16 优化器与低精度主权重

## 1. 本讲目标

学完本讲，你应该能够：

1. 从浮点格式的指数位/尾数位出发，解释 **bf16 为什么不需要 loss scaling**，而 fp16 必须（承接 u4-l1）。
2. 画出 `BF16_Optimizer` 的内存布局：bf16 扁平权重（全量）+ fp32 主权重分区（本 rank 份额）+ fp32 扁平梯度（全量），并按 DP 世界大小估算每参数字节数。
3. 说出引擎在什么条件下会选择 `BF16_Optimizer`（专门化 ZeRO-1 路径与 DDP 式路径），以及 `data_types.grad_accum_dtype` 在其中的作用。
4. 掌握 2025/12 引入的低精度 master states 配置：`bf16.bf16_master_weights_and_grads` 与 `bf16.bf16_optimizer_states` 的语义、依赖关系和卸载约束。
5. 理解 `keep_in_fp32` 精度策略的本更新周期演进（PR #8843）：`keep_buffers_in_fp32`/`buffers_to_keep_in_fp32` 更名为 **`keep_tensors_in_fp32`/`tensors_to_keep_in_fp32`** 并新增 `include_params`，名单同时覆盖参数与缓冲；但**参数级 fp32 保留仅在 ZeRO-3（无参数卸载）生效**——`BF16_Optimizer` 这类把参数组压平成单一 dtype 缓冲的优化器做不到，引擎在 ZeRO-3 以下把点名参数 cast 到训练 dtype（`auto` 告警、显式列表直接 `ValueError`），buffers 仍保留 fp32。
6. 解读 `load_state_dict` 的 masters 恢复语义：`load_from_fp32_weights=False` 时从 bit16 权重重建 fp32 主权重；引擎 `load_checkpoint` 正常恢复仍传 `True`；`load_optimizer_states=False` 保留 masters 并让 Adam moments 重新开始。
7. 解读上一更新周期（PR #9034，修 #7204）补全的销毁清理：`_update_hp_grad` 给每个 bf16 参数留下的 `_hp_grad` 视图会把整块 fp32 梯度缓冲（约 2 倍 bf16 模型大小）钉在内存里，`destroy()` 现在显式置空它——HF Trainer 这类「销毁引擎但保留模型」的用法不再泄漏。

## 2. 前置知识

### 2.1 bf16 与 fp16 的位布局差异

| 格式 | 符号位 | 指数位 | 尾数位 | 动态范围 | 有效精度 |
| --- | --- | --- | --- | --- | --- |
| fp32 | 1 | 8 | 23 | 约 \(2^{-126}\) ~ \(2^{127}\) | ~7 位十进制 |
| bf16 | 1 | **8** | **7** | 与 fp32 相同 | ~2~3 位十进制 |
| fp16 | 1 | **5** | 10 | \(2^{-14}\)（正规数）~ 65504 | ~3~4 位十进制 |

关键区别：

- **fp16 的指数位只有 5 个**，正规数最小约 \(6 \times 10^{-5}\)，比它小的梯度只能挤进精度极差的 subnormal 区，再小就下溢为 0。大模型梯度普遍很小，直接用 fp16 存梯度会大量"归零"，所以必须先把 loss 放大（loss scaling，见 u4-l1）再 backward，让梯度整体搬进可表示区间。
- **bf16 的指数位与 fp32 一样是 8 个**，动态范围完整覆盖 fp32 的区间，梯度几乎不会因为"太小"而下溢。它的代价是尾数只有 7 位，精度粗（相邻可表示数的间距大），但这个误差性质上是"舍入噪声"而不是"整段丢失"，对随机梯度下降是良性的。所以 **bf16 训练不需要 loss scaling**。

### 2.2 master weights 与混合精度训练

混合精度训练的前向/反向用低精度（bf16/fp16）算，但权重更新必须回到 fp32：学习率乘以梯度往往远小于 bf16 的舍入间距 \(w\) 本身，若直接在 bf16 权重上加更新量 \(w + \Delta w\) 可能被舍回 \(w\)，小更新完全丢失。因此优化器内部维护一份 **fp32 主权重（masters）**，低精度权重只是它同步出来的投影。u4-l2 讲过 fp16 包装优化器如何做这件事，本讲讲 bf16 的对应实现 `BF16_Optimizer`。

### 2.3 梯度累积 dtype

配置项 `data_types.grad_accum_dtype` 决定 backward 产生并累积的梯度用什么精度（默认跟随模型 dtype）。**bf16 模型 + fp32 梯度累积**是数值上最稳的组合，也是本讲的中心线索——它会触发专门化的 `BF16_Optimizer`（见 4.1）。

### 2.4 张量视图与存储的生命周期（为 4.5 做铺垫）

PyTorch 里一个张量对象由「元数据（形状/步长/dtype）+ 底层存储（storage）」组成；`narrow`/`view`/`split` 产生的**视图**是新的张量对象，但共享同一块 storage。关键规则是：**只要还有任何一个视图活着，整块 storage 就不能被回收**——哪怕你早已删掉了"原来那个"大张量。验证对象是否真正被回收的标准手法是 `weakref.ref(obj)` + `del` + `gc.collect()`：弱引用不阻止回收，回收后 `ref()` 返回 `None`。本讲 4.5 的销毁清理正是围绕"参数身上挂着一个视图"展开的。

### 2.5 参数（Parameter）与缓冲（Buffer）

`nn.Parameter` 参与梯度更新、会被优化器看到；`register_buffer` 登记的张量只是随模块保存/迁移的常量（如位置编码的 `inv_freq`、MoE 的路由偏置），没有梯度、优化器也不认识它。这个区别在 4.3 变得关键：**名单豁免（keep in fp32）对 buffer 与 parameter 的处理从本更新周期起不再相同**——buffer 在任何配置下都能留在 fp32，而 parameter 只有 ZeRO-3（无参数卸载）才保留。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspeed/runtime/bf16_optimizer.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py) | `BF16_Optimizer` 本体：内存布局、step 主循环、state_dict 保存/加载、destroy() 销毁清理（本讲主角） |
| [deepspeed/runtime/precision_config.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/precision_config.py) | `DeepSpeedBF16Config`：bf16 配置字段的 pydantic 解析，含低精度 master states 两个开关 |
| [deepspeed/runtime/keep_in_fp32.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py) | 按名字模式把指定**参数与缓冲**留在 fp32 的精度策略（#8843 更名后由 `keep_tensors_in_fp32` 统一处理两种张量） |
| [deepspeed/runtime/engine.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py) | 优化器包装分发（`_do_optimizer_sanity_check`）、`_configure_bf16_optimizer`、dtype 转换关卡（`_cast_module_mixed_precision`/`_keeps_params_in_fp32`）、`load_checkpoint` 调用点、`destroy()`/`__del__` 生命周期 |
| [deepspeed/runtime/base_optimizer.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/base_optimizer.py) | `_configure_master_weights`：低精度 master states 的公共校验与 master dtype 选择 |
| [deepspeed/runtime/zero/partition_parameters.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py) | `zero.Init`：模块 `__init__` 收尾时的 `_keep_in_fp32` 就地转换（参数级保留的 ZeRO-3 侧入口，深入见 u5-l9） |
| [deepspeed/runtime/zero/stage_1_and_2.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage_1_and_2.py) | ZeRO-1/2 优化器：`load_from_fp32_weights` 旧语义的参照系 |
| [tests/unit/checkpoint/test_fp32_master_stage3_bf16.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py) | masters 恢复语义回归测试，也是本讲 4.4 实践任务的模板 |
| [tests/unit/v1/half_precision/test_keep_in_fp32_modules.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py) | keep_in_fp32 全语义回归：模式编译、buffer/参数保留、量化分区豁免、ZeRO-3 训练精度、ZeRO-3 以下的 cast 关卡（本周期大幅扩充） |
| [tests/unit/v1/zero/test_zero.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py) | `TestDestroyReleasesEngine`：销毁释放回归（含 BF16_Optimizer 场景） |

## 4. 核心概念与源码讲解

### 4.1 BF16_Optimizer 主循环

#### 4.1.1 概念说明

`BF16_Optimizer` 是 bf16 训练的包装优化器（继承 `ZeROOptimizer` 接口），它解决的问题是：**前向/反向用 bf16 保吞吐，权重更新用 fp32 保精度，同时把 fp32 主权重按数据并行维度切分以省显存**——可以理解为"轻量版 ZeRO-1"：只切分主权重和优化器状态，梯度保持全量。

引擎在两个场景会选择它（见 [engine.py:L2367-L2396](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L2367-L2396) 的 `_do_optimizer_sanity_check`）：

1. **专门化 ZeRO-1 路径**：开启了 ZeRO、stage 恰为 1、模型 dtype 是 bf16、梯度累积 dtype 是 fp32、且没有 CPU 卸载时，返回 `BFLOAT16` 分发标志——这就是"dedicated ZeRO-1 BF16 optimizer"的由来（[engine.py:L2378-L2383](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L2378-L2383)；该分支同时要求 `compute_grad_norm` 保持开启，否则直接 `ValueError`）。`tests/unit/checkpoint/test_fp32_master_stage3_bf16.py` 正是用 `zero_optimization.stage=1` + `data_types.grad_accum_dtype="fp32"` + `bf16.enabled=true` 组合出这条路径的。
2. **关闭 ZeRO 的流水线 bf16 路径**：没开 ZeRO、模型 dtype 与梯度累积 dtype 相同且都是 bf16、且开启了流水线并行时，同样返回 `BFLOAT16`（[engine.py:L2389-L2393](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L2389-L2393)，同时打 warning 提示大累积步数下 bf16 累积的数值风险）。

注意别混淆：**普通 DDP bf16（关 ZeRO、不开流水线）走的是 `DDP_BFLOAT16` 分发标志**，在 [engine.py:L2433-L2435](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L2433-L2435) 被交给 `_configure_fp16_optimizer`（即 u4-l2 讲的 FP16 家族包装器，只是低精度 dtype 换成 bf16），不会经过本讲的 `BF16_Optimizer`。

因为 bf16 不需要 loss scaling，这个类里与 loss scaler 相关的属性全部是为了 API 兼容而设的占位符。

#### 4.1.2 核心流程

一次完整的梯度更新周期：

```text
backward（引擎侧，每微批次）
  backward_prologue:  清零 lp(bf16) 梯度
  autograd 产生 bf16 param.grad
  backward_epilogue → update_hp_grads:
      把 lp.grad 累加进全量 fp32 扁平梯度缓冲（hp_grad）
step（梯度累积边界，每 gas 个微批次一次）
  1. 计算（专家/非专家分组的）全局梯度范数
  2. 范数无效（inf/nan）→ 判定溢出，清空 hp/lp 梯度，直接返回
  3. clip_grad > 0 → 按全局范数裁剪
  4. 把本 rank 的 fp32 梯度分区拷给 fp32 主权重分区的 .grad
  5. 调用包装的底层优化器 optimizer.step()（在 fp32 分区上更新 masters）
  6. update_lp_params: 把更新后的 fp32 分区拷回 bf16 分区，
     再 all-gather 拼出全量 bf16 扁平权重
  7. 清空 fp32 梯度缓冲
```

内存布局（设数据并行世界大小为 \(N\)，可训练参数量 \(\Psi\)，`grad_accum_dtype=fp32`）：

| 张量 | 精度 | 覆盖范围 | 每参数字节 |
| --- | --- | --- | --- |
| `bf16_groups_flat` | bf16 | 全量 | 2 |
| `fp32_groups_flat_partition` | fp32 | 本 rank 分区 | \(4/N\) |
| `fp32_groups_gradients_flat` | fp32 | 全量 | 4 |
| 底层优化器状态（如 Adam 两个矩） | fp32 | 本 rank 分区 | \(8/N\) |

请特别记住第一行：**每个参数组压平成"一块"单一 dtype 的 bf16 扁平缓冲**。这个看似平常的设计正是 4.3 中"参数级 fp32 保留为何在 ZeRO-3 以下做不到"的直接原因。

#### 4.1.3 源码精读

**① 构造入口与 loss scaler 占位符。** 引擎侧的构造在 [_configure_bf16_optimizer（engine.py:L2683-L2703）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L2683-L2703)，把 `bfloat16_config` 与 `grad_acc_dtype=self.get_data_types()[1]` 一起传入。类内部在 [bf16_optimizer.py:L75-L79](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L75-L79) 声明这些占位属性，注释明说"BF16 不用 loss scaling，这些属性只为 API 兼容"——对照 u4-l1 的 `DynamicLossScaler`，这里 `self.overflow = False` 只是一个普通标志位。

**② 内存布局的建立。** [_setup_for_real_optimizer（bf16_optimizer.py:L156-L241）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L156-L241) 逐参数组完成：

- [L169-L175](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L169-L175)：把组内 bf16 参数摊平成**单个**扁平张量（dtype 取决于参数本身），并让各参数的 `.data` 指向扁平存储的对应视图（之后 `update_lp_params` 改写扁平张量，模型参数随之更新）。一块缓冲一个 dtype——记住这一点，4.3 会回来引用。
- [L177-L183](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L177-L183)：按 DP 世界大小把扁平 bf16 权重等分为 `real_dp_world_size` 个分区（对齐填充记入 `group_paddings`）。
- [L186-L187](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L186-L187)：**fp32 主权重分区在本行诞生**——`bf16_dp_partitions[partition_id].clone().float().detach()`，即取本 rank 的 bf16 分区、升精度到 fp32、断开计算图。
- [L194-L206](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L194-L206)：**全量 fp32 扁平梯度缓冲与逐参数视图在本段建立**——L195-L196 分配 `fp32_groups_gradients_flat`（dtype 用 `grad_acc_dtype`），L203-L206 用 `_split_flat_tensor` 把它切成 `fp32_groups_gradients`（逐参数视图列表）。记住这两行的存在，4.5 的泄漏链就从这里出发。
- [L228-L229](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L228-L229)：把底层优化器的 `param_group['params']` **重绑定为 fp32 分区**——此后 `optimizer.step()` 看到的参数就是 fp32 masters，与模型里的 bf16 参数彻底解耦。

**③ 梯度从 bf16 到 fp32 的搬运。** [backward_epilogue / update_hp_grads（bf16_optimizer.py:L507-L542）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L507-L542)：backward 结束后（或开启 `immediate_grad_update` 时在每个 autograd hook 里），[_update_hp_grad 的核心一行是 L527](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L527)：`hp_grad.data.add_(lp.grad.data.to(hp_grad.dtype)...)`——把 bf16 梯度转成 fp32 后**累加**进全量 fp32 梯度缓冲。多次微批次的梯度在这里以 fp32 累积，这就是 `grad_accum_dtype=fp32` 的数值收益所在。注意 [L528](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L528) 顺手把这个 fp32 视图挂在了参数身上（`lp._hp_grad = hp_grad`）——训练期间这是无害的便利引用，销毁时却成为泄漏源，详见 4.5。

**④ step 主循环。** [step（bf16_optimizer.py:L387-L437）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L387-L437) 依次做：

- [L392-L404](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L392-L404)：分组计算全局梯度范数（MoE 模型的专家梯度走 `get_norm_with_moe_layers`）。
- [L406-L411](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L406-L411)：`is_invalid_grad_norm` 命中（inf/nan）则标记 `self.overflow = True` 并**跳过整个更新**——这是 bf16 版的"溢出检查"，检查的是梯度本身是否已坏，而不是 loss scale 是否越界（bf16 没有 scale 可调）。
- [L412-L422](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L412-L422)：按全局范数裁剪，然后把本 rank 的 fp32 梯度分区赋给 fp32 主权重分区的 `.grad`（若梯度累积 dtype 不是 fp32，先 cast）。
- [L426](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L426)：`self.optimizer.step()` 在 fp32 masters 上做真正的 Adam/SGD 更新。
- [L435](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L435)：`update_lp_params()` 回写 bf16。

**⑤ masters → bf16 的反向同步。** [update_lp_params（bf16_optimizer.py:L596-L607）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L596-L607)：先把更新后的 fp32 分区 `copy_` 进对应的 bf16 分区（隐式降精度），再用 `all_gather_dp_groups` 分桶 all-gather 各分区，拼回全量 bf16 扁平权重。由于各 bf16 参数的 `.data` 是扁平存储的视图（见 ②），模型下一轮前向立即用上新权重。

#### 4.1.4 代码实践

**实践目标**：亲眼确认配置如何决定"拿到的是 `BF16_Optimizer` 还是 ZeRO 优化器"，并观察 bf16 训练确实没有 loss scale 参与。

**操作步骤**：

1. 阅读并复用测试 [_config（test_fp32_master_stage3_bf16.py:L30-L53）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py#L30-L53) 的写法，写一个最小脚本（示例代码，非项目原有文件）：

   ```python
   import torch, deepspeed
   from deepspeed.runtime.bf16_optimizer import BF16_Optimizer

   def build(zero_stage, grad_accum_dtype):
       model = torch.nn.Linear(4, 4)
       cfg = {
           "train_micro_batch_size_per_gpu": 1,
           "optimizer": {"type": "Adam", "params": {"lr": 0.1}},
           "bf16": {"enabled": True},
           "zero_optimization": {"stage": zero_stage},
       }
       if grad_accum_dtype:
           cfg["data_types"] = {"grad_accum_dtype": grad_accum_dtype}
       engine, *_ = deepspeed.initialize(config=cfg, model=model,
                                         model_parameters=model.parameters())
       return engine

   for stage, gdt in [(1, "fp32"), (1, None), (2, "fp32"), (0, None)]:
       e = build(stage, gdt)
       print(f"stage={stage}, grad_accum_dtype={gdt} -> {type(e.optimizer).__name__}")
   ```

2. 在 CPU 单进程下运行（gloo 后端即可，不需要 GPU；`bf16.enabled=true` 在 CPU 上通常可用，若你的环境报 `is_bf16_supported` 相关跳过，则改在 GPU 机器上跑）。

**需要观察的现象**：四行输出分别是 `BF16_Optimizer`、`DeepSpeedZeroOptimizer`、`DeepSpeedZeroOptimizer`、`FP16_UnfusedOptimizer`（最后一行取决于底层优化器：FusedAdam 时为 `FP16_Optimizer`，普通 Adam 时为 `FP16_UnfusedOptimizer`）。

**预期结果**：只有"stage=1 且 grad_accum_dtype=fp32"命中专门化路径（对应 [engine.py:L2378-L2383](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L2378-L2383) 的条件）；不写 `grad_accum_dtype` 时 stage=1 落回通用 ZeRO 优化器；关闭 ZeRO 的纯 bf16 DDP 走 `DDP_BFLOAT16` → `_configure_fp16_optimizer`（[engine.py:L2433-L2435](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L2433-L2435)），得到的是 FP16 家族包装器而非 `BF16_Optimizer`。具体输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BF16_Optimizer.__init__` 里直接 `assert grad_acc_dtype in [torch.float32, torch.bfloat16]`，而不允许别的累积精度？

**答案**：梯度累积缓冲的 dtype 决定了 backward 期间梯度的驻留精度。允许的集合就是"至少与模型同精度"：fp32 是数值最优选择（专门化路径的触发条件），bf16 是省内存的默认行为；fp16 累积既没有 bf16 的动态范围优势，又比 fp32 精度差，属于被有意排除的组合。

**练习 2**：`update_lp_params` 里为什么必须先 `copy_` 再 all-gather，而不能直接 all-gather fp32 分区？

**答案**：模型参数是 bf16，前向读的是 bf16 扁平存储；all-gather 的目标张量就是这块 bf16 存储，所以必须先把 fp32 masters 的更新结果降精度写回本 rank 的 bf16 分区，再在 bf16 精度上聚合全量权重。若直接聚合 fp32，每卡要多付出 4 字节/参数的通信量，且模型侧还得再做一次降精度拷贝。

**练习 3**：`step` 中 [L406-L411](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L406-L411) 的溢出处理与 u4-l1 里 `DynamicLossScaler.update_scale` 的溢出处理有何本质区别？

**答案**：fp16 路径检测到溢出后**调小 scale 重试**（损失可恢复，训练继续）；BF16 路径没有 scale，梯度 inf/nan 意味着这一步本身坏了，只能**丢弃该步**（清空梯度直接返回），不存在"缩小放大倍数再来一次"的补救手段。

### 4.2 precision_config 解析与低精度 master states

#### 4.2.1 概念说明

`precision_config.py` 用 pydantic 模型把 JSON 里的 `"bf16": {...}` 块解析成 `DeepSpeedBF16Config`。除基础的 `enabled`/`immediate_grad_update`/`check_grad_overflow` 外，2025/12 引入了两个**低精度 master states** 开关，允许把 ZeRO 的 master 权重（乃至优化器状态）也压到 bf16，进一步换显存：

- `bf16_master_weights_and_grads`：master 参数/梯度用 bf16 而非 fp32（省 \(2+2\) 字节/参数的分区开销）。
- `bf16_optimizer_states`：Adam 矩也用 bf16（再省 \(4\) 字节/参数分区）。文档给出的账本：卸载场景下 bf16 master + 两个 bf16 矩约 6 字节/参数，矩保持 fp32 约 10 字节/参数。

注意区分：这两个开关作用于 **ZeRO 优化器**（stage 1/2/3 的 `DeepSpeedZeroOptimizer` / `_Stage3`），而不是 4.1 的 `BF16_Optimizer`——后者本身就是"bf16 权重 + fp32 masters"的专用实现。

#### 4.2.2 核心流程

```text
JSON "bf16" 块
  → get_bfloat16_config() 兼容旧键名 "bfloat16"
  → DeepSpeedBF16Config（pydantic 校验，未知字段报错）
  → DeepSpeedConfig._do_sanity_check 校验跨字段约束：
        低精度 master 开关要求 ZeRO 开启且 stage ∈ {1,2,3}
        bf16_optimizer_states 要求 bf16_master_weights_and_grads=true
  → 引擎把开关传给 ZeRO 优化器构造
  → base_optimizer._configure_master_weights 统一校验并返回 master dtype
```

#### 4.2.3 源码精读

**① 配置模型。** [DeepSpeedBF16Config（precision_config.py:L41-L71）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/precision_config.py#L41-L71) 定义全部五个字段：`enabled`（L46）、`immediate_grad_update`（L51）、`check_grad_overflow`（L56）、`bf16_master_weights_and_grads`（L63）、`bf16_optimizer_states`（L68），每个字段下方 docstring 就是文档来源。[get_bfloat16_config（L34-L38）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/precision_config.py#L34-L38) 先查 `"bf16"` 键，查不到再回退旧键 `"bfloat16"`（L31 注明为兼容保留）。

**② 跨字段校验。** [config.py:L706-L714](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/config.py#L706-L714)：低精度 master 开关必须搭配 ZeRO stage 1/2/3；`bf16_optimizer_states` 必须以 `bf16_master_weights_and_grads` 为前提。配置错误在引擎构造前就被拦下。

**③ 卸载约束。** [_configure_master_weights（base_optimizer.py:L515-L559）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/base_optimizer.py#L515-L559) 是 ZeRO stage 1/2/3 共用的收口：L530-L536 做互斥/依赖断言；L538-L549 处理卸载——若 master 用 bf16 而矩留 fp32（fp32 矩在 GPU 上放不下省下来的空间），必须走 ZeRO-Offload 且用 `DeepSpeedCPUAdam`；反过来"bf16 矩 + 卸载"则要求 `DeepSpeedCPUAdam` 以 `fp32_optimizer_states=False` 构造，否则 CPU 优化器会把矩**静默扩回 fp32**，省显存的意图落空。L554-L559 按 fp16/bf16/默认返回 master dtype。

**④ 文档矩阵。** [docs/_pages/config-json.md:L363-L368](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/docs/_pages/config-json.md#L363-L368) 给出 ZeRO stage × 两个开关的支持矩阵，可作为速查表。

#### 4.2.4 代码实践

**实践目标**：亲手触发两条校验，理解低精度 master states 的约束边界。

**操作步骤**：

1. 写一份 JSON（示例代码）：

   ```json
   {
     "bf16": {"enabled": true, "bf16_optimizer_states": true},
     "zero_optimization": {"stage": 2}
   }
   ```

   不设 `bf16_master_weights_and_grads`，加载进 `deepspeed.initialize`。

2. 把 `bf16_optimizer_states` 改为配套的 `"bf16_master_weights_and_grads": true, "bf16_optimizer_states": true`，同时把 `zero_optimization.stage` 改成 `0`，再次初始化。

3. 通过后，在 `zero_optimization` 里加上 `"offload_optimizer": {"device": "cpu"}`，用 torch 自带的 `torch.optim.Adam` 作为客户端优化器再试一次。

**需要观察的现象**：三步分别被三道不同防线拒绝/通过。

**预期结果**：第 1 步在 [config.py:L714](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/config.py#L714) 断言失败（`bf16_optimizer_states requires bf16_master_weights_and_grads`）；第 2 步在 [config.py:L711-L713](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/config.py#L711-L713) 失败（stage 0 不支持）；第 3 步会因未使用 `DeepSpeedCPUAdam` 被 [base_optimizer.py:L546-L549](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/base_optimizer.py#L546-L549) 附近的卸载校验拦截（具体报错文本以运行为准，待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`check_grad_overflow` 字段控制谁的行为？

**答案**：控制 **ZeRO stage 1/2 优化器**在 bf16 模式下是否做梯度溢出检测（[engine.py:L2711-L2715](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L2711-L2715) 读取后在 [L2778](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L2778) 传给 `_configure_zero_optimizer`）。专门化 `BF16_Optimizer` 的 `step` 则总是检查 `is_invalid_grad_norm`，不读这个字段。字段默认值 True 是为了与 fp16 的默认行为对齐（docstring 引用 issue #5242 / PR #6976）。

**练习 2**：为什么"bf16 master + fp32 矩"必须搭配 CPU 卸载？

**答案**：这个组合省的是 master 的 2 字节/参数（分区），但矩仍是 8 字节/参数（fp32）。如果不卸载，fp32 矩留在 GPU 上，显存大头没省；卸载到 CPU 后矩占用的是主机内存，GPU 上只剩 bf16 master 与梯度，显存收益才兑现——所以校验强制 `DeepSpeedCPUAdam`。反过来"全 bf16"时状态足够小，可以留在 GPU 也可以卸载（见文档矩阵）。

### 4.3 keep_in_fp32 精度策略：从 buffers 到 tensors（本周期 PR #8843）

#### 4.3.1 概念说明

`keep_in_fp32.py` 解决的是混合精度的一个"少数派报告"问题：**个别张量在低精度下会直接破坏模型行为，必须豁免于 dtype 转换**。最典型的是 MoE 模型的路由偏置 `e_score_correction_bias`（DeepSeek-V3、GLM-4.5/5 等都有）：它在 GLM-5.2 中取值约 5~20，相邻值差小于 0.01，而 bf16 在该量级的相邻可表示数间距是 0.03~0.125——转成 bf16 后 256 个专家只剩 3~16 个不同取值，**token 会被路由到错误的专家**。这类误差不像梯度噪声那样可平均掉，而是系统性的决策错误。

本更新周期（PR #8843）把这份豁免从 buffers 扩展到了**参数**，API 随之更名：

| 旧 API（上一周期） | 新 API（本周期） |
| --- | --- |
| `buffers_to_keep_in_fp32(module, pattern)` → 返回 buffer 列表 | `tensors_to_keep_in_fp32(module, pattern)` → 返回 `(全名, 张量, 是否 buffer)` 三元组列表，**同时覆盖参数与缓冲** |
| `keep_buffers_in_fp32(module, pattern)` → 返回转换计数 | `keep_tensors_in_fp32(module, pattern, include_params=True)` → 就地转换，返回 `(参数转换数, 缓冲转换数)` |

但"名单覆盖参数"不等于"参数总能保留"，语义分两层：

- **缓冲：任何配置下都保留 fp32**（这正是旧行为，也是 `include_params=False` 时仅存的行为）。
- **参数：仅在 ZeRO-3 且无参数卸载时保留 fp32**。ZeRO-3 是唯一"每个 dtype 一块分区缓冲"的优化器；而 `BF16_Optimizer` 和 ZeRO-1/2 都把每个参数组压平成**一块单一 dtype 缓冲**（见 4.1.② 的 `bf16_groups_flat`），组里混入 fp32 参数无处安放。参数卸载（offload_param 到 CPU/NVMe）的 ZeRO-3 同样如此——分区躺在单一 dtype 的平坦 CPU/NVMe 缓冲里。

于是引擎在 ZeRO-3 以下设了一道 cast 关卡：点名参数被 cast 到训练 dtype，**`auto` 模式打 warning，显式列表直接 `ValueError`**——显式点名意味着用户明确想要 fp32 参数，静默降级不可接受。参数级保留在 ZeRO-3 侧的完整机制（分区就地转换、按 dtype 拆子组、fp32 梯度归约）由 u5-l9 展开，本讲只讲清"为什么低于 ZeRO-3 做不到"。

#### 4.3.2 核心流程

```text
名单来源
  transformers 类属性 _keep_in_fp32_modules_strict（bf16/fp16 都保留）
            + _keep_in_fp32_modules（仅 fp16 时并入）
  / data_types.keep_in_fp32_modules（"auto" | 显式模式列表 | []）
      → keep_in_fp32_pattern(module, setting, dtype)：编译成一条正则；fp32 训练返回 None

引擎路径（普通方式构建的模型，bf16/fp16 启用时）
  keep_pattern = keep_in_fp32_pattern(...)
  _cast_module_mixed_precision(..., keep_pattern, keep_params=_keeps_params_in_fp32())
      keep_params = ZeRO-3 分区权重 且 参数未卸载到 cpu/nvme
      ├─ kept 集合 = 匹配的 buffer（+ 匹配的参数，仅当 keep_params）
      ├─ 参数整体 cast 到训练 dtype 时跳过 kept
      ├─ keep_tensors_in_fp32(include_params=keep_params) 就地恢复 fp32
      └─ keep_params=False 且名单点名了参数 → _warn_listed_params_not_kept
            "auto" → logger.warning；显式列表 → ValueError

ZeRO-3 路径（zero.Init 上下文内构建的模型）
  每个模块 __init__ 收尾 → Init._keep_in_fp32(module)
      include_params = not params_offloaded（remote_device 非本地 → 仅保 buffers 并告警）
      参数就地转 ds_tensor 分区（hpZ 的 ds_secondary_tensor 同步转换；
      zero_quantized_nontrainable_weights 的量化分区保持量化并 warning_once）
```

#### 4.3.3 源码精读

**① 名单与动机。** 模块 docstring [keep_in_fp32.py:L5-L27](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L5-L27) 完整交代了背景：transformers 自己加载模型时会尊重这两个列表，但在 ZeRO-3 下 `zero.Init` 把 `__init__` 里创建的每个浮点张量都转成训练 dtype，transformers 的加载器再把检查点拷进这些张量，名单就丢了——所以 DeepSpeed 必须在引擎侧重新实现这份豁免。[L25-L27](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L25-L27) 写明新的分层语义：buffers 永远保留；参数仅在"ZeRO-3 无参数卸载——唯一能持有多种 dtype 分区的优化器"下保留，并举了 DeepSeek-V4 的超连接权重/attention sinks/norms、gpt-oss 的 attention sinks 这些点名参数的例子。

**② 模式编译（未变）。** [keep_in_fp32_pattern（L39-L62）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L39-L62)：dtype 是 fp32 时直接返回 `None`（纯 fp32 训练无需豁免，L44-L45）；`auto` 模式下遍历模块收集名单，fp16 时额外并入 `_keep_in_fp32_modules`（L50-L51）；最后 `"|".join` 成单个正则（L62），匹配规则与 transformers 的 `build_glob_alternation` + `re.search` 一致——模式可以匹配完整张量名的任意位置。

**③ 匹配对象从 buffer 扩到张量。** [tensors_to_keep_in_fp32（L65-L76）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L65-L76)：与旧版只遍历 `named_buffers(recurse=False)` 不同，现在**先遍历参数再遍历缓冲**，每个匹配项记为 `(全名, 张量, is_buffer)`——`is_buffer` 标志让下游能区分对待（引擎关卡正是靠它决定谁被 cast、谁报警）。

**④ 就地转换。** [keep_tensors_in_fp32（L85-L120）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L85-L120) 是新核心：

- [L95-L96](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L95-L96)：`include_params=False` 时跳过所有参数——这就是"仅保 buffers"模式（ZeRO-3 以下与参数卸载场景的调用方式）。
- [_stored_dtype（L79-L82）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L79-L82)：判断实际存储 dtype 的小帮手——ZeRO-3 参数的 `.data` 只是占位符，真值在 `ds_tensor` 分区里，必须看分区的 dtype。
- [L98-L102](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L98-L102)：量化分区（`zero_quantized_nontrainable_weights` 冻结权重）**保持量化**——分区里存的是 int8 码与 scale，把码转成 fp32 并不会还原数值，反而让 scale 失配，所以只 `warning_once` 后跳过。
- [L105-L115](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L105-L115)：ZeRO-3 参数就地转**分区**（`partition.data = partition.data.float()`），hpZ 场景下 all-gather 读取的节点本地副本 `ds_secondary_tensor` 同步转换；最后把参数本体的 `.data`（占位符或已 gather 的全量张量）也转成 fp32，保证下一次 gather/free 都保持 fp32。
- 返回值从单一计数变成 `(num_params, num_buffers)` 二元组（L93、L116-L119）。

**⑤ 引擎侧能力判定与 cast 关卡。** 这是本周期引擎侧的三件套：

- [_keeps_params_in_fp32（engine.py:L1927-L1936）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L1927-L1936)：能力判定——`zero_optimization_partition_weights()`（即 ZeRO-3）且参数未卸载到 cpu/nvme 时为 True。docstring 一句话点破本讲的联系："**ZeRO-1/2 与 bf16/fp16 优化器把每个参数组压平成一块单一 dtype 缓冲**"——正是 4.1.② L169-L175 那段代码。
- [_warn_listed_params_not_kept（engine.py:L1938-L1947）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L1938-L1947)：名单点名了参数却不被支持时的两级反应——`auto` 只 `logger.warning`（模型自带的名单不该挡住用户训练），显式列表 `raise ValueError`（用户明确要 fp32 参数，静默 cast 等于欺骗）。
- [_cast_module_mixed_precision（engine.py:L1885-L1925）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L1885-L1925)：dtype 转换主体——L1896-L1902 用 `tensors_to_keep_in_fp32` 建 `kept` 集合（buffer 恒入、参数仅当 `keep_params`）；L1906-L1911 参数 cast 跳过 `kept`；L1913-L1917 调 `keep_tensors_in_fp32(include_params=keep_params)` 把抵达时已是低精度的匹配张量升回 fp32，`keep_params=False` 时随即触发 ⑤ 的告警/报错。调用点在 [engine.py:L2152-L2161](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L2152-L2161)：`keep_params` 实参就是 `self._keeps_params_in_fp32()`。

**⑥ ZeRO-3 路径：zero.Init 收尾转换。** [partition_parameters.py:L1079-L1080](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1079-L1080) 把配置存进 `Init`；每个模块 `__init__` 跑完后（此时 transformers 模型自己的 fp32 名单已完整、子模块已分区、任何检查点加载都还没发生），[L1250-L1253](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1250-L1253) 调用 [_keep_in_fp32（L1255-L1272）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1255-L1272)：`include_params=not params_offloaded`（L1262-L1263），参数卸载（`remote_device` 非本地）时只保 buffers 并 warning（L1264-L1267）。参数级保留在 ZeRO-3 训练中的完整图景（按分区 dtype 拆子组、fp32 梯度归约累积等）见 u5-l9。

**⑦ 配置文档。** [docs/_pages/config-json.md:L1798](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/docs/_pages/config-json.md#L1798) 的 `data_types.keep_in_fp32_modules` 词条本周期同步改写：明确"参数与缓冲都保留"，并写清"参数仅在 ZeRO stage 3 无参数卸载下保持 fp32（梯度同样以 fp32 归约与累积）；其他 stage 或有参数卸载时跟随训练 dtype——auto 告警、显式列表报错；量化存储的冻结参数保持量化并告警"。

#### 4.3.4 代码实践

**实践目标**：在 ZeRO-0/1/2 下验证 cast 关卡的三条行为（参数被 cast、buffers 保留、显式列表被拒），并直接体验新 API。

**操作步骤**：

1. 运行本周期新增的回归测试（在 `tests` 目录下执行；`DistributedTest` 会自行拉起 world_size=2 的进程，CPU 上可跑——该类只有 bf16 不支持的 skipif）：

   ```bash
   cd tests && python -m pytest \
     "unit/v1/half_precision/test_keep_in_fp32_modules.py::TestStagesBelowThreeKeptParams" -v
   ```

   对照两个用例阅读：[test_auto_casts_params_keeps_buffers（L527-L535）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L527-L535) 在 stage 0/1/2 下断言 `sink` 参数被 cast 成 `torch.bfloat16` 而 `router_bias` 缓冲仍是 `torch.float32`；[test_explicit_parameter_pattern_rejected（L537-L548）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L537-L548) 用 `keep=["sink"]` 断言 stage 2 抛 `ValueError`（测试用 try/except 而非 `pytest.raises`，注释解释了 DistributedTest 工作进程池会丢掉 BaseException）。

2. 观察引擎的告警文本：把 [ToyModelWithParams（L210-L221）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L210-L221)（`_keep_in_fp32_modules_strict = ["sink", "router_bias"]`，`sink` 是初值全 8.0 的参数）抄进自己的脚本，用 [_params_config（L236-L272）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L236-L272) 的 stage=1 配置初始化，捕获日志中的 warning；再换 `keep=["sink"]` 复现 ValueError。

3. 直接调用新 API 感受参数与缓冲的差别（示例代码，不需要初始化分布式）：

   ```python
   import torch
   from deepspeed.runtime.keep_in_fp32 import (
       keep_in_fp32_pattern, keep_tensors_in_fp32)

   m = torch.nn.Module()
   m.sink = torch.nn.Parameter(torch.full((8,), 8.0))
   m.register_buffer("router_bias", torch.linspace(5, 20, 256))
   m._keep_in_fp32_modules_strict = ["sink", "router_bias"]

   m.bfloat16()                       # 模拟引擎把模块整体 cast 到训练 dtype
   pat = keep_in_fp32_pattern(m, "auto", torch.bfloat16)
   n_params, n_buffers = keep_tensors_in_fp32(m, pat)
   print(n_params, n_buffers, m.sink.dtype, m.router_bias.dtype)

   m2 = m.bfloat16()                  # 再降回去，试 include_params=False
   keep_tensors_in_fp32(m2, pat, include_params=False)
   print(m2.sink.dtype, m2.router_bias.dtype)
   ```

**需要观察的现象**：第 1/2 步里 `sink` 在 stage<3 下是 `torch.bfloat16`、`router_bias` 是 `torch.float32`；`auto` 模式日志出现 "data_types.keep_in_fp32_modules names 1 parameters (first: sink), but only ZeRO stage 3 without parameter offload can keep parameters in fp32..."；显式列表抛出的 `ValueError` 带 " Remove the parameter patterns or use ZeRO stage 3."。第 3 步第一次打印 `1 1 torch.float32 torch.float32`（参数与缓冲都恢复），第二次打印 `torch.bfloat16 torch.float32`（只有缓冲恢复）。

**预期结果**：与上述断言一致。注意第 3 步直接调 API 时参数确实被转回 fp32——这演示的是原语本身的能力；引擎路径之所以不这么做，是因为 `BF16_Optimizer`/ZeRO-1/2 的单 dtype 扁平缓冲装不下混合 dtype（见 ⑤）。具体输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `BF16_Optimizer` 无法保留 fp32 参数，而 ZeRO-3 可以？

**答案**：`BF16_Optimizer` 在 `_setup_for_real_optimizer` 里把每个参数组压平成**一块** bf16 扁平缓冲（[L169-L175](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L169-L175)），fp32 masters 也是每组一块单一 dtype 分区——组内混入 fp32 参数意味着同一块缓冲要容纳两种 dtype，结构上不可能。ZeRO-3 的存储单元是**每个参数自己的分区**（`ds_tensor`），不同参数的分区可以各有各的 dtype，所以 `keep_tensors_in_fp32` 才能就地转分区。`_keeps_params_in_fp32` 的 docstring（[engine.py:L1927-L1936](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L1927-L1936)）就是这个理由的官方表述。

**练习 2**：为什么 `auto` 模式只告警，显式列表却直接 `ValueError`？

**答案**：`auto` 的名单来自模型类属性（transformers 作者为 fp32 硬件加载写的），用户未必知情、也未必有 ZeRO-3 的部署条件——挡住训练过重，告警 + 参数 cast 已是行为合理的降级。显式列表是用户手写的配置，点名参数却拿到 cast 结果属于"静默违背明确意图"，必须当场报错让用户改配置或换 stage 3。

**练习 3**：`keep_tensors_in_fp32` 转换 ZeRO-3 参数时为什么连占位符 `.data` 也要 `.float()`，而不是只转 `ds_tensor` 分区？

**答案**：参数本体的 `.data` 平时是 0 元素占位符，但参数被 gather 时（`GatheredParameters` 或 forward 的 all-gather）会以它为模板重建全量张量——占位符还是低精度的话，下一次 gather 出来的就是低精度张量、free 回去又会被降级。把占位符一起转成 fp32，"下一次 gather、下一次 free 都保持 fp32"才成立（[keep_in_fp32.py:L113-L115](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L113-L115) 的注释）。

### 4.4 load_state_dict 的 masters 重建（PR #8892）

#### 4.4.1 概念说明

检查点恢复时，fp32 masters 从哪里来是一个有损/无损的分叉：

- **无损**：从检查点里保存的 `SINGLE_PARTITION_OF_FP32_GROUPS`（fp32 分区副本）直接拷贝。
- **有损重建**：丢弃保存的 masters，从已加载的 bf16 权重升精度重建（`_restore_from_bit16_weights`）——bf16 只有 7 位尾数，重建必然丢掉 masters 中低于 bf16 分辨率的位。

PR #8892 补全了这个语义矩阵，核心变化是 `BF16_Optimizer._load_legacy_checkpoint` 新增 `load_from_fp32_weights=False` 分支：**此前该参数被静默忽略（无论如何都拷贝保存的 fp32 分区），False 时改为从 bit16 权重重建**，与 ZeRO stage 1/2 的既有行为（[stage_1_and_2.py:L3799-L3808](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage_1_and_2.py#L3799-L3808) 的 docstring 写明两种选项）对齐。ZeRO-3 的 `_rigid_load_state_dict` / `_load_universal_checkpoint` 同一 PR 也加了同样的分支（本讲聚焦 BF16 优化器，ZeRO-3 侧详见 u5-l8）。

完整的恢复矩阵（引擎 `load_checkpoint` 视角）：

| 加载方式 | masters 来源 | Adam moments |
| --- | --- | --- |
| 正常恢复（默认） | 检查点保存的 fp32 masters（引擎硬编码传 `load_from_fp32_weights=True`） | 检查点恢复 |
| `load_optimizer_states=False` | **保留**保存的 fp32 masters | 重新开始（fresh） |
| `load_module_only=True`（bf16 专用优化器） | 从加载的 bf16 权重**重建** | 重新开始 |
| 直接调 `optimizer.load_state_dict(..., load_from_fp32_weights=False)` | 从 bit16 权重**重建** | 由 `load_optimizer_states` 决定 |

另有两个配套语义：`load_optimizer_states=False` 时不恢复 `clip_grad` 等优化器全局量（保持当前引擎设置）；配置项 `zero_optimization.load_from_fp32_weights` 保持"已废弃、被忽略"状态。

#### 4.4.2 核心流程

```text
engine.load_checkpoint(tag, load_optimizer_states=?, load_module_only=?)
  │  硬编码 load_from_fp32_weights=True（engine.py L4861-L4867）
  ▼
optimizer.load_state_dict(state_dict_list, load_optimizer_states,
                          load_from_fp32_weights, ...)
  ├─ checkpoint_folder 非空 → _load_universal_checkpoint（universal 格式）
  └─ 否则 → _load_legacy_checkpoint
        ├─ load_optimizer_states=True:
        │     恢复 clip_grad；optimizer.load_state_dict(moments)
        ├─ load_from_fp32_weights=True:
        │     逐组把保存的 fp32 分区（经 _get_padded_tensor 补齐）copy_ 进当前分区
        ├─ else:
        │     _restore_from_bit16_weights()   # 从 bf16 分区升精度重建 masters
        └─ load_optimizer_states=True: 重新链接 lp↔hp 映射
```

#### 4.4.3 源码精读

**① 重建函数本体。** [_restore_from_bit16_weights（bf16_optimizer.py:L650-L654）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L650-L654)：对本 rank 的 bf16 分区做 `fp32_partition.data.copy_(bf16_partitions[partition_id].data)`——注意这是 4.1.⑤ `update_lp_params` 的**反方向**拷贝（那边是 fp32→bf16，这边是 bf16→fp32），升精度时低 16 位尾数填零。[refresh_fp32_params（L656-L657）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L656-L657) 是它面向 ZeRO-3 接口风格的外部别名。

**② 分发入口。** [load_state_dict（L659-L669）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L659-L669)：`checkpoint_folder` 存在走 universal（[_load_universal_checkpoint（L695-L698）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L695-L698)），否则走 legacy。

**③ else 分支。** [_load_legacy_checkpoint（L671-L693）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L671-L693)：

- [L680-L682](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L680-L682)：仅当 `load_optimizer_states=True` 才恢复 `clip_grad` 与 moments——这就是"masters 保留、moments 重开"语义的来源。
- [L684-L688](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L684-L688)：无损路径，保存的 fp32 分区经 [_get_padded_tensor（L738-L744）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L738-L744) 处理对齐填充后拷入。
- **[L689-L690](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L689-L690)：`else: self._restore_from_bit16_weights()`**——修复前这里什么都不做（保存的 masters 被无条件沿用，`load_from_fp32_weights=False` 形同虚设）。

**④ 引擎侧调用点。** [engine.py:L4861-L4867](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L4861-L4867)：`load_checkpoint` 调 `optimizer.load_state_dict` 时**硬编码 `load_from_fp32_weights=True`**——所以正常恢复永远保留保存的 masters；`False` 分支只有直接调用优化器接口才会走到（这正是测试 4.4.4 的做法）。

**⑤ 回归测试。** [test_fp32_master_stage3_bf16.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py) 用一个参数的迷你模型把整个矩阵钉死：

- [_train_and_detach_masters（L78-L87）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py#L78-L87) 是关键道具：训练一步后给 masters 手动加上 \(2^{-12}\)——这个增量**介于 fp32 与 bf16 分辨率之间**，使"保留的 masters"与"从 bf16 重建的 masters"可被 `torch.equal` 区分开（L86 先断言二者确实不等）。
- [L115-L125](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py#L115-L125)：`load_optimizer_states=False` → masters 逐元素等于保存值（L120），moments 不存在（L121），优化器全局量未被覆盖（L123-L125）。
- [L127-L136](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py#L127-L136)：`load_module_only=True` → bf16 优化器的 masters 等于"保存值经 bf16 舍入"的结果（L135），即从 bf16 权重重建；注释同时说明 ZeRO-3 因为模型检查点里存的就是 fp32 分区，module-only 仍读原值。
- [L138-L148](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py#L138-L148)：直接以 `load_from_fp32_weights=False` 调 `load_state_dict`，覆盖引擎永远走不到的重建分支——L141 先再扰动一次 masters 确保它们已偏离重建值，L146-L147 断言重建后回到 bf16 舍入值。

#### 4.4.4 代码实践

**实践目标**：用回归测试复现"保留 vs 重建"两种 masters 恢复语义。

**操作步骤**：

1. 运行回归测试（CPU、gloo、world_size=1 即可，与该测试的 CI 环境一致）：

   ```bash
   pytest tests/unit/checkpoint/test_fp32_master_stage3_bf16.py -v
   ```

2. 精读 [_train_and_detach_masters](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py#L78-L87)，理解 \(2^{-12}\) 扰动为什么能把两条路径区分开：master 初值为 1 附近的更新结果，bf16 在 1 附近的间距是 \(2^{-7}\) 量级，fp32 是 \(2^{-23}\) 量级，\(2^{-12}\) 落在两者之间。
3. 把测试抄成一个独立小脚本，改造最后一段：先 `load_optimizer_states=False, load_from_fp32_weights=True` 加载并断言 masters 保留；再对同一引擎直接调 `optimizer.load_state_dict(zero_sd_list, load_optimizer_states=False, load_from_fp32_weights=False)` 并断言 masters 变成 bf16 舍入值（对照 [L143-L147](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py#L143-L147)）。

**需要观察的现象**：两个断言一个 `torch.equal == True`（保留）、一个对 `bf16 舍入值 == True`（重建）；两次加载后 `_has_moments` 都是 False。

**预期结果**：与测试 L120/L135/L147 的断言一致。若在第 3 步独立脚本中观察 `fp32_groups_flat_partition` 的原始字节，保留路径与保存值逐位相同，重建路径低 16 位尾数为零。具体输出待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么引擎 `load_checkpoint` 硬编码传 `True`，而把 `False` 留给直接调用优化器接口？

**答案**：正常恢复的目标是"无损续训"，保存的 fp32 masters 是训练进度的最高保真副本，没有理由丢弃；`False`（从 bf16 重建）服务的是模块级热启动这类场景——你只关心模型权重，愿意接受精度损失换取与模块检查点的组合自由，这类组合由用户显式控制更安全。

**练习 2**：`load_optimizer_states=False` 时 `clip_grad` 不被恢复，这个细节有什么实际影响？

**答案**：masters-only 加载用于"保留权重、重置优化器"的场景；若顺带恢复了保存的 `clip_grad`，会悄悄改变当前运行已配置的裁剪行为，让"只动 moments"的预期落空。测试 L111-L125 显式断言这些全局量保持加载前的值。

**练习 3**：`load_module_only=True` 时，为什么 bf16 优化器选择重建而 ZeRO-3（kind="zero3"）仍能读到保存的 fp32 分区？

**答案**：module-only 加载只读模型检查点（bf16 权重），不碰优化器检查点文件；bf16 专用优化器的模型权重就是 bf16，masters 只能从中重建。ZeRO-3 的参数分区本身存放在 fp32 分片文件里（模型检查点包含分区权重），所以 module-only 也读得到 fp32 原值——这正是测试 L130-L135 注释里两者断言不同的原因。

### 4.5 destroy() 与 _hp_grad 视图清理（PR #9034）

#### 4.5.1 概念说明

`BF16_Optimizer` 的最后一个问题是**生命周期**问题。很多调用方（最典型的是 HuggingFace Trainer）的工作模式是「创建引擎 → 训练 → `engine.destroy()` → 保留模型继续用（或换配置再包一个新引擎）」。`destroy()` 的契约是：之后 `del engine` + `gc.collect()` 应当把引擎、优化器连同它们的大块缓冲全部回收，而模型完好无损。

被修复的 bug（issue #7204，PR #9034）是：`destroy()` 之后一切仍然活着，**每次"创建 → 训练 → 销毁"循环都泄漏一整套引擎**。对 `BF16_Optimizer` 而言，独有的泄漏链就是 4.1.③ 埋下的伏笔：

```text
模型参数 p（调用方保留，活着）
  └─ p._hp_grad = hp_grad        # _update_hp_grad L528 挂上的逐参数视图
        └─ 视图共享 storage
              └─ fp32_groups_gradients_flat 整块扁平缓冲（全量、fp32）
```

按 2.4 的规则，只要模型参数活着、参数身上还挂着这个视图，**整块** fp32 梯度缓冲就无法释放。账本很好算：fp32 梯度每参数 4 字节、全量；bf16 模型每参数 2 字节——缓冲恰为 **2 倍 bf16 模型大小**，每个销毁周期都被钉死一次。PR 实测（2 rank、`Sequential` 套 16 个 `Linear(4096,4096)`、bf16、AdamW、4 个周期取平均）：`BF16_Optimizer` 场景修复前每周期泄漏 +3072.8 MiB 显存，修复后只剩调用方主动保留的模型本身（+512.1 MiB）。

修复内容：`destroy()` 在清 `_hp_mapping` 的同时，把每个参数的 `p._hp_grad` 也置为 `None`。同一次提交还修了引擎与 ZeRO 侧的同类反向引用（forward hooks、`__del__` 二次拆除、ZeRO-1/2 的 `p._zero_optimizer`、ZeRO-3 的 `p._z3_optimizer` 与 `FWD_MODULE_STACK`）——完整销毁链在 u2-l3 展开，本讲只聚焦 bf16 侧的 `_hp_grad`。

#### 4.5.2 核心流程

```text
engine.destroy()                          # engine.py L1267 起
  ├─ self._destroyed = True               # 让 __del__ 跳过二次拆除（L1256-L1265）
  ├─ optimizer.destroy()                  # L1280-L1282 转发
  │     BF16_Optimizer.destroy()          # bf16_optimizer.py L126-L139
  │       ├─ 逐参数 p._hp_mapping = None  # 原有清理
  │       ├─ 逐参数 p._hp_grad = None     # ← 本修复新增（L133-L136）
  │       └─ 移除 _grad_acc_hooks         # immediate_grad_update 的 autograd hooks
  └─ 摘除模块 forward hooks 等引擎级引用   # 详见 u2-l3

之后 del engine + gc.collect()
  → 引擎、优化器、fp32 梯度缓冲全部可回收
  → 模型（含 bf16 参数）继续可用
```

为什么 `__del__` 要跳过已显式销毁的引擎：被销毁的旧引擎可能在**新引擎包装同一模型之后**才被垃圾回收，此时若 `__del__` 再跑一遍拆除，清掉的将是新引擎挂上去的 `_hp_mapping` 等状态（`safe_get_full_fp32_param` 会返回全零）。`_destroyed` 标记保证拆除至多完整发生一次。

#### 4.5.3 源码精读

**① 视图的诞生与挂载。** [_update_hp_grad（bf16_optimizer.py:L511-L529）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L511-L529)：L523 取出的 `hp_grad` 是 `fp32_groups_gradients[group_idx][param_idx]`——即 4.1.② L203-L206 里从扁平缓冲 split 出来的**逐参数视图**；L527 把 bf16 梯度累加进去之后，L528 `lp._hp_grad = hp_grad` 把视图挂到参数上。训练期间它无害（下次 backward 覆盖、step 后 `clear_hp_grads` 只清零不释放）；问题只出现在"参数活得比优化器久"的销毁场景。

**② destroy() 本体。** [BF16_Optimizer.destroy（bf16_optimizer.py:L126-L139）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L126-L139)：原有的 `_hp_mapping` 清理之后，[L133-L136](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/bf16_optimizer.py#L133-L136) 用 `getattr` 防御式检查 `_hp_grad` 并置空，注释直接写明动机："`_update_hp_grad()` leaves a view of the fp32 gradient buffer here; a model kept after destroy() would otherwise keep the whole buffer alive."（`_update_hp_grad()` 在这里留下 fp32 梯度缓冲的视图；destroy() 之后被保留的模型否则会钉住整块缓冲。）最后移除 `_grad_acc_hooks`。

**③ 引擎侧的转发与防重入。** [engine.destroy（engine.py:L1267-L1303）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L1267-L1303)：L1268 先置 `_destroyed`；[L1280-L1282](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L1280-L1282) 调 `optimizer.destroy()`；随后摘除闭包引用引擎的模块 hooks（这段属于 u2-l3 的主线）。[__del__（L1256-L1265）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L1256-L1265) 看到该标记即直接返回，注释点明原因：显式销毁的引擎可能在新引擎包装同一模型后才被回收，二次拆除会清掉新引擎的状态。

**④ 回归测试。** [TestDestroyReleasesEngine（test_zero.py:L2495-L2593）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py#L2495-L2593) 把销毁契约钉死，7 个参数化场景（[L2498-L2509](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py#L2498-L2509)）：ZeRO-1/2/3 各带/不带 CPU 卸载，再加 `(1, False, "fp32")`——注释明确写着"With bf16 parameters, this selects BF16_Optimizer"。与 `_hp_grad` 直接相关的两段：

- [L2550-L2563](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py#L2550-L2563)：`destroy()` 之后记录 `fp32_groups_gradients_flat` 各缓冲的 `untyped_storage().data_ptr()`，`del` + `gc.collect()` 后断言 `engine_ref()`/`optimizer_ref()` 为 `None`，且**没有任何参数还持有这些缓冲的 storage**。注释解释了为什么比较 storage 而不是张量对象：视图会在缓冲自己的张量对象被回收后继续钉住其 storage。
- [_storages_held_by_params（L2586-L2593）](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py#L2586-L2593)：遍历每个参数的 `param.grad` 与 `vars(param)` 里所有张量值，收集它们的 storage 指针——`_hp_grad` 就藏在 `vars(param)` 里，这正是能抓到本 bug 的原因。

测试后半段（[L2565-L2584](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py#L2565-L2584)）还验证了两条衍生契约：被销毁的旧引擎在被回收时不得破坏接替它包装同一模型的新引擎（`safe_get_full_fp32_param` 前后一致）；最后一个引擎销毁后模型本身也可被回收。

#### 4.5.4 代码实践

**实践目标**：亲眼看到 `destroy()` 后参数不再钉住 fp32 梯度缓冲，并能手动复现修复前的泄漏。

**操作步骤**：

1. 先跑回归测试的 BF16 场景（需要支持 bf16 的环境；CPU 可跑则用 `--forked` 对照 CI 方式）：

   ```bash
   cd tests && python -m pytest "unit/v1/zero/test_zero.py::TestDestroyReleasesEngine" -v
   ```

2. 再写独立脚本（示例代码，非项目原有文件），用「重新挂回视图」模拟修复前的行为：

   ```python
   import gc, weakref, torch, deepspeed

   model = torch.nn.Linear(64, 64)
   cfg = {
       "train_micro_batch_size_per_gpu": 2,
       "optimizer": {"type": "Adam", "params": {"lr": 1e-3, "torch_adam": True}},
       "bf16": {"enabled": True},
       "zero_optimization": {"stage": 1},
       "data_types": {"grad_accum_dtype": "fp32"},
   }
   engine, opt, _, _ = deepspeed.initialize(config=cfg, model=model,
                                            model_parameters=model.parameters())
   x = torch.randn(2, 64).to(torch.bfloat16)
   loss = engine(x, x).sum()
   engine.backward(loss); engine.step()

   p = next(model.parameters())
   saved_view = p._hp_grad                       # 修复代码会清掉它，先存一份
   buf_ref = weakref.ref(opt.fp32_groups_gradients_flat[0])
   storage_before = p._hp_grad.untyped_storage().data_ptr()

   engine.destroy()                              # 现在 p._hp_grad 是 None
   del engine, opt, loss, x
   gc.collect()
   print("修复后缓冲是否被回收:", buf_ref() is None)          # 预期 True

   p._hp_grad = saved_view                       # 模拟修复前：视图重新挂回参数
   gc.collect()
   print("挂回视图后缓冲是否复活:", buf_ref() is not None)     # 预期 True（泄漏复现）
   ```

3. 对照 [L2550-L2563](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py#L2550-L2563) 的断言方式，把第 2 步改成「比较 `storage_before` 与 `_storages_held_by_params(model)` 收集的指针集合」再验一遍。

**需要观察的现象**：`destroy()` + `gc.collect()` 后弱引用变 `None`（缓冲真正释放）；把视图挂回参数后，同样的 `del` + `gc.collect()` 不再释放——同一个视图、两种结局，唯一差别是参数身上那个属性。

**预期结果**：与 `TestDestroyReleasesEngine` 的 bf16 用例一致；模型参数在 `destroy()` 后仍可正常前向（`model(x)` 不报错）。具体输出待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`destroy()` 原本就清 `_hp_mapping`，为什么还必须再清 `_hp_grad`？

**答案**：两者引用的对象不同。`_hp_mapping` 是 DeepSpeed 的映射结构（描述 lp↔hp 的对应关系，间接关联 hp 分区）；`_hp_grad` 是**直接指向 fp32 梯度缓冲 storage 的张量视图**。PyTorch 的 storage 按"还有没有任何视图活着"整体决定生死，所以哪怕只漏一个参数上的一个视图，整块全量缓冲（约 2 倍模型大小）都释放不了。清理必须逐属性对症下药。

**练习 2**：测试为什么比较 `untyped_storage().data_ptr()`，而不是直接 `p._hp_grad is buffer`？

**答案**：视图是独立于缓冲张量对象的另一个张量对象，`is` 比较几乎永远为 False；而且 destroy 之后再想访问 `_hp_grad` 已经是 `None`。storage 的 `data_ptr()` 是底层内存块的稳定身份——只要任何视图钉着这块内存，新收集到的参数持有指针集合就会与缓冲的指针相交，泄漏无所遁形。

**练习 3**：为什么训练期间让 `lp._hp_grad` 一直挂在参数上是可接受的设计，销毁时却必须显式清理？

**答案**：训练期间优化器本身持有 `fp32_groups_gradients_flat`，缓冲本来就不该释放，多一个视图引用零成本，还省去每次 backward 重新查视图的麻烦——这是合理的缓存。问题只出在所有权交接时：`destroy()` 语义是"优化器放弃一切、模型独立存活"，所有从参数可达优化器资源的引用都必须在这一刻断开，否则"保留模型"这个再正常不过的用法就会变成内存泄漏。缓存与清理是同一枚硬币的两面。

## 5. 综合实践

把本讲的对比线索（bf16 vs fp16）、精度豁免线索（keep_in_fp32 的 stage 关卡）与生命周期线索（销毁不泄漏）落成一次可复现实验（GPU 环境最佳，CPU 可跑 bf16 部分）：

1. **搭建对照脚本**：以 [tests/unit/checkpoint/test_fp32_master_stage3_bf16.py 的 _OneParam](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py#L20-L27) 为蓝本放大成一个两层 MLP（隐藏维 1024），再仿照 [ToyModelWithParams](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L210-L221) 加一个 `sink` 参数（`_keep_in_fp32_modules_strict` 点名它）。写两份配置：`"fp16": {"enabled": true}` 与 `"bf16": {"enabled": true}` + `"data_types": {"grad_accum_dtype": "fp32"}`，其余字段（优化器、lr、batch size）完全相同，ZeRO stage=1。
2. **记录显存**：每步前后调用 `torch.cuda.max_memory_allocated()`，跑 50 步，输出两列峰值曲线。
3. **记录数值行为**：fp16 路径打印每步 loss scale（`engine.optimizer.cur_scale`，属性名以运行时为准）；bf16 路径确认没有任何 scale 变量参与。对比两者最终 loss。
4. **解释差异**：预期 bf16 峰值略高（fp32 梯度缓冲 4 字节/参数 vs fp16 的 2 字节，且 bf16 路径持有全量 fp32 梯度），但省去溢出重试；把每项差异对应回 4.1.② 的内存布局表逐项记账。
5. **验证 cast 关卡**：在两份配置里都保留 `data_types.keep_in_fp32_modules` 的默认 `auto`，观察 stage=1 下 `sink` 被 cast 到训练 dtype 的告警、`router_bias` 类 buffer 仍为 fp32；再换显式列表复现 `ValueError`（对照 4.3.4 第 2 步）。
6. **收尾（检查点）**：对 bf16 引擎 `save_checkpoint` 后，按 4.4.4 第 3 步分别验证保留/重建两种加载，把三条断言结果贴进实验报告。
7. **收尾（生命周期）**：训练结束后调用 `engine.destroy()`，按 4.5.4 第 2 步用 `weakref` 验证引擎、优化器、fp32 梯度缓冲都被回收且模型仍可前向；再循环「initialize → 训练 10 步 → destroy → del → gc.collect」4 次，用 `torch.cuda.max_memory_allocated` 对比第 1 轮与第 4 轮的增量，确认没有周期性泄漏。

报告模板：一页对照表（每步峰值显存、最终 loss、loss scale 轨迹、`sink`/buffer 的最终 dtype 与告警文本、加载语义断言、销毁后弱引用状态），加一段"差异来源"分析。全部数值待本地验证。

## 6. 本讲小结

- bf16 的 8 位指数带来与 fp32 等宽的动态范围，梯度不下溢，因此 **bf16 训练不需要 loss scaling**；`BF16_Optimizer` 里的 scaler 属性全是 API 兼容占位符，溢出处理退化为"梯度 inf/nan 即丢步"。
- `BF16_Optimizer` 的内存布局是"bf16 全量扁平权重 + 本 rank 的 fp32 master 分区 + fp32 全量梯度缓冲"，底层优化器的参数组被重绑定到 fp32 分区，更新后再降精度写回并 all-gather。
- 专门化路径的触发条件是 **ZeRO stage=1 + bf16 模型 + fp32 梯度累积 + 无卸载**（关 ZeRO 的 bf16 只有搭配流水线并行才走它；普通 DDP bf16 走 FP16 家族包装器）。
- 低精度 master states（`bf16_master_weights_and_grads` / `bf16_optimizer_states`）作用于 ZeRO 优化器，有"依赖 + stage 范围 + 卸载后端"三重校验。
- 本周期 #8843 让 `keep_in_fp32` 家族从 buffers 扩展到参数：`keep_tensors_in_fp32(include_params)` 统一处理两种张量，但**参数级 fp32 保留仅在 ZeRO-3 无参数卸载时成立**（唯一的多种 dtype 分区持有者）；ZeRO-3 以下引擎把点名参数 cast 到训练 dtype——`auto` 告警、显式列表 `ValueError`——buffers 在任何配置下都保留 fp32，量化分区保持量化。
- PR #8892 补全了 masters 恢复语义：`load_from_fp32_weights=False` 现在真正从 bit16 权重重建 masters（与 ZeRO-1/2 对齐）；引擎正常恢复硬编码 `True`；`load_optimizer_states=False` 保留 masters、重开 moments、不动优化器全局量。
- PR #9034 补全了销毁语义：`_update_hp_grad` 挂在参数上的 `_hp_grad` 视图会替被保留的模型钉住整块 fp32 梯度缓冲（约 2 倍 bf16 模型大小），`destroy()` 现在显式置空它——配合引擎级 hooks 摘除与 `_destroyed` 防重入，"创建 → 训练 → 销毁"循环不再泄漏。

## 7. 下一步学习建议

- **u5-l1（ZeRO 总览）**：`BF16_Optimizer` 本质是"只切 master/优化器状态"的极简 ZeRO-1，进入 ZeRO 单元后可以对照 `stage_1_and_2.py` 理解完整版实现（IPG bucket、overlap 通信）多了什么。
- **u5-l9（本周期新增：ZeRO-3 混合 dtype）**：本讲 4.3 只讲了"为什么低于 ZeRO-3 保留不了 fp32 参数"；参数级保留的完整机制——`zero.Init` 收尾就把点名参数的 `ds_tensor` 分区转 fp32、stage3 按分区 dtype 拆子组、按累积 dtype 分平坦梯度缓冲与 IPG bucket、fp32 参数的梯度全程 fp32 归约累积——在那篇系统展开。
- **u5-l8（ZeRO 检查点与权重导出）**：本讲 4.4 只讲了 BF16 优化器的加载语义，ZeRO-3 的 `_rigid_load_state_dict`、流水线串行加载的分片预检查（同属 PR #8892）在那篇展开。
- **u2-l3（引擎构造与销毁）**：本讲 4.5 只讲了 `BF16_Optimizer` 侧的 `_hp_grad` 清理；完整的 destroy 链——forward hooks 摘除、`__del__` 防重入、ZeRO-1/2 的 `p._zero_optimizer`、ZeRO-3 的 `p._z3_optimizer` 与 `FWD_MODULE_STACK` 弹栈、协调器 `lru_cache` 的实例化——在那篇系统展开。
- **源码延伸**：对照读 [stage_1_and_2.py 的 _load_legacy_checkpoint docstring](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage_1_and_2.py#L3799-L3808)，看"fp32 无损拷贝 vs bit16 有损重建"两种选项在完整 ZeRO 优化器里如何与弹性加载（改 DP 数量）交互。
