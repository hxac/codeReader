# u4-l3 BF16 优化器与低精度主权重

## 1. 本讲目标

学完本讲，你应该能够：

1. 从浮点格式的指数位/尾数位出发，解释 **bf16 为什么不需要 loss scaling**，而 fp16 必须（承接 u4-l1）。
2. 画出 `BF16_Optimizer` 的内存布局：bf16 扁平权重（全量）+ fp32 主权重分区（本 rank 份额）+ fp32 扁平梯度（全量），并按 DP 世界大小估算每参数字节数。
3. 说出引擎在什么条件下会选择 `BF16_Optimizer`（专门化 ZeRO-1 路径与 DDP 式路径），以及 `data_types.grad_accum_dtype` 在其中的作用。
4. 掌握 2025/12 引入的低精度 master states 配置：`bf16.bf16_master_weights_and_grads` 与 `bf16.bf16_optimizer_states` 的语义、依赖关系和卸载约束。
5. 理解 `keep_in_fp32` 精度策略：为什么 MoE 路由偏置这类**缓冲区**在 bf16 训练中必须留在 fp32。
6. 解读 PR #8892 引入的 `load_state_dict` masters 恢复语义：`load_from_fp32_weights=False` 时从 bit16 权重重建 fp32 主权重；引擎 `load_checkpoint` 正常恢复仍传 `True`；`load_optimizer_states=False` 保留 masters 并让 Adam moments 重新开始。
7. 解读本周期（PR #9034，修 #7204）补全的销毁清理：`_update_hp_grad` 给每个 bf16 参数留下的 `_hp_grad` 视图会把整块 fp32 梯度缓冲（约 2 倍 bf16 模型大小）钉在内存里，`destroy()` 现在显式置空它——HF Trainer 这类「销毁引擎但保留模型」的用法不再泄漏。

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

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspeed/runtime/bf16_optimizer.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py) | `BF16_Optimizer` 本体：内存布局、step 主循环、state_dict 保存/加载、destroy() 销毁清理（本讲主角） |
| [deepspeed/runtime/precision_config.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/precision_config.py) | `DeepSpeedBF16Config`：bf16 配置字段的 pydantic 解析，含低精度 master states 两个开关 |
| [deepspeed/runtime/keep_in_fp32.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/keep_in_fp32.py) | 按名字模式把指定**缓冲区**留在 fp32 的精度策略 |
| [deepspeed/runtime/engine.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py) | 优化器包装分发（`_do_optimizer_sanity_check`）、`_configure_bf16_optimizer`、`load_checkpoint` 调用点、`destroy()`/`__del__` 生命周期 |
| [deepspeed/runtime/base_optimizer.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/base_optimizer.py) | `_configure_master_weights`：低精度 master states 的公共校验与 master dtype 选择 |
| [deepspeed/runtime/zero/stage_1_and_2.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py) | ZeRO-1/2 优化器：`load_from_fp32_weights` 旧语义的参照系 |
| [tests/unit/checkpoint/test_fp32_master_stage3_bf16.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py) | masters 恢复语义回归测试，也是本讲实践任务的模板 |
| [tests/unit/v1/zero/test_zero.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero.py) | `TestDestroyReleasesEngine`：本周期新增的销毁释放回归（含 BF16_Optimizer 场景） |

## 4. 核心概念与源码讲解

### 4.1 BF16_Optimizer 主循环

#### 4.1.1 概念说明

`BF16_Optimizer` 是 bf16 训练的包装优化器（继承 `ZeROOptimizer` 接口），它解决的问题是：**前向/反向用 bf16 保吞吐，权重更新用 fp32 保精度，同时把 fp32 主权重按数据并行维度切分以省显存**——可以理解为"轻量版 ZeRO-1"：只切分主权重和优化器状态，梯度保持全量。

引擎在两个场景会选择它（见 [engine.py:L2327-L2356](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2327-L2356) 的 `_do_optimizer_sanity_check`）：

1. **专门化 ZeRO-1 路径**：开启了 ZeRO、stage 恰为 1、模型 dtype 是 bf16、梯度累积 dtype 是 fp32、且没有 CPU 卸载时，返回 `BFLOAT16` 分发标志——这就是 PR 描述里"dedicated ZeRO-1 BF16 optimizer"的由来。`tests/unit/checkpoint/test_fp32_master_stage3_bf16.py` 正是用 `zero_optimization.stage=1` + `data_types.grad_accum_dtype="fp32"` + `bf16.enabled=true` 组合出这条路径的。
2. **关闭 ZeRO 的流水线 bf16 路径**：没开 ZeRO、模型 dtype 与梯度累积 dtype 相同且都是 bf16、且开启了流水线并行时，同样返回 `BFLOAT16`（同时打 warning 提示大累积步数下 bf16 累积的数值风险）。

注意别混淆：**普通 DDP bf16（关 ZeRO、不开流水线）走的是 `DDP_BFLOAT16` 分发标志**，在 [engine.py:L2393-L2395](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2393-L2395) 被交给 `_configure_fp16_optimizer`（即 u4-l2 讲的 FP16 家族包装器，只是低精度 dtype 换成 bf16），不会经过本讲的 `BF16_Optimizer`。

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

#### 4.1.3 源码精读

**① 构造入口与 loss scaler 占位符。** 引擎侧的构造在 [_configure_bf16_optimizer（engine.py:L2643-L2663）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2643-L2663)，把 `bfloat16_config` 与 `grad_acc_dtype=self.get_data_types()[1]` 一起传入。类内部在 [bf16_optimizer.py:L75-L79](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L75-L79) 声明这些占位属性，注释明说"BF16 不用 loss scaling，这些属性只为 API 兼容"——对照 u4-l1 的 `DynamicLossScaler`，这里 `self.overflow = False` 只是一个普通标志位。

**② 内存布局的建立。** [_setup_for_real_optimizer（bf16_optimizer.py:L156-L241）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L156-L241) 逐参数组完成：

- [L169-L175](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L169-L175)：把组内 bf16 参数摊平成单个扁平张量，并让各参数的 `.data` 指向扁平存储的对应视图（之后 `update_lp_params` 改写扁平张量，模型参数随之更新）。
- [L177-L183](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L177-L183)：按 DP 世界大小把扁平 bf16 权重等分为 `real_dp_world_size` 个分区（对齐填充记入 `group_paddings`）。
- [L186-L187](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L186-L187)：**fp32 主权重分区在本行诞生**——`bf16_dp_partitions[partition_id].clone().float().detach()`，即取本 rank 的 bf16 分区、升精度到 fp32、断开计算图。
- [L194-L206](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L194-L206)：**全量 fp32 扁平梯度缓冲与逐参数视图在本段建立**——L195-L196 分配 `fp32_groups_gradients_flat`，L203-L206 用 `_split_flat_tensor` 把它切成 `fp32_groups_gradients`（逐参数视图列表）。记住这两行的存在，4.5 的泄漏链就从这里出发。
- [L228-L229](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L228-L229)：把底层优化器的 `param_group['params']` **重绑定为 fp32 分区**——此后 `optimizer.step()` 看到的参数就是 fp32 masters，与模型里的 bf16 参数彻底解耦。

**③ 梯度从 bf16 到 fp32 的搬运。** [backward_epilogue / update_hp_grads（bf16_optimizer.py:L507-L542）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L507-L542)：backward 结束后（或开启 `immediate_grad_update` 时在每个 autograd hook 里），[_update_hp_grad 的核心一行是 L527](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L527)：`hp_grad.data.add_(lp.grad.data.to(hp_grad.dtype)...)`——把 bf16 梯度转成 fp32 后**累加**进全量 fp32 梯度缓冲。多次微批次的梯度在这里以 fp32 累积，这就是 `grad_accum_dtype=fp32` 的数值收益所在。注意 [L528](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L528) 顺手把这个 fp32 视图挂在了参数身上（`lp._hp_grad = hp_grad`）——训练期间这是无害的便利引用，销毁时却成为泄漏源，详见 4.5。

**④ step 主循环。** [step（bf16_optimizer.py:L387-L437）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L387-L437) 依次做：

- [L392-L404](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L392-L404)：分组计算全局梯度范数（MoE 模型的专家梯度走 `get_norm_with_moe_layers`）。
- [L406-L411](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L406-L411)：`is_invalid_grad_norm` 命中（inf/nan）则标记 `self.overflow = True` 并**跳过整个更新**——这是 bf16 版的"溢出检查"，检查的是梯度本身是否已坏，而不是 loss scale 是否越界（bf16 没有 scale 可调）。
- [L412-L422](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L412-L422)：按全局范数裁剪，然后把本 rank 的 fp32 梯度分区赋给 fp32 主权重分区的 `.grad`（若梯度累积 dtype 不是 fp32，先 cast）。
- [L426](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L426)：`self.optimizer.step()` 在 fp32 masters 上做真正的 Adam/SGD 更新。
- [L435](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L435)：`update_lp_params()` 回写 bf16。

**⑤ masters → bf16 的反向同步。** [update_lp_params（bf16_optimizer.py:L596-L607）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L596-L607)：先把更新后的 fp32 分区 `copy_` 进对应的 bf16 分区（隐式降精度），再用 `all_gather_dp_groups` 分桶 all-gather 各分区，拼回全量 bf16 扁平权重。由于各 bf16 参数的 `.data` 是扁平存储的视图（见 ②），模型下一轮前向立即用上新权重。

#### 4.1.4 代码实践

**实践目标**：亲眼确认配置如何决定"拿到的是 `BF16_Optimizer` 还是 ZeRO 优化器"，并观察 bf16 训练确实没有 loss scale 参与。

**操作步骤**：

1. 阅读并复用测试 [_config（test_fp32_master_stage3_bf16.py:L30-L53）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py#L30-L53) 的写法，写一个最小脚本（示例代码，非项目原有文件）：

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

**预期结果**：只有"stage=1 且 grad_accum_dtype=fp32"命中专门化路径（对应 [engine.py:L2338-L2343](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2338-L2343) 的条件）；不写 `grad_accum_dtype` 时 stage=1 落回通用 ZeRO 优化器；关闭 ZeRO 的纯 bf16 DDP 走 `DDP_BFLOAT16` → `_configure_fp16_optimizer`（[engine.py:L2393-L2395](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2393-L2395)），得到的是 FP16 家族包装器而非 `BF16_Optimizer`。具体输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BF16_Optimizer.__init__` 里直接 `assert grad_acc_dtype in [torch.float32, torch.bfloat16]`，而不允许别的累积精度？

**答案**：梯度累积缓冲的 dtype 决定了 backward 期间梯度的驻留精度。允许的集合就是"至少与模型同精度"：fp32 是数值最优选择（专门化路径的触发条件），bf16 是省内存的默认行为；fp16 累积既没有 bf16 的动态范围优势，又比 fp32 精度差，属于被有意排除的组合。

**练习 2**：`update_lp_params` 里为什么必须先 `copy_` 再 all-gather，而不能直接 all-gather fp32 分区？

**答案**：模型参数是 bf16，前向读的是 bf16 扁平存储；all-gather 的目标张量就是这块 bf16 存储，所以必须先把 fp32 masters 的更新结果降精度写回本 rank 的 bf16 分区，再在 bf16 精度上聚合全量权重。若直接聚合 fp32，每卡要多付出 4 字节/参数的通信量，且模型侧还得再做一次降精度拷贝。

**练习 3**：`step` 中 [L406-L411](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L406-L411) 的溢出处理与 u4-l1 里 `DynamicLossScaler.update_scale` 的溢出处理有何本质区别？

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

**① 配置模型。** [DeepSpeedBF16Config（precision_config.py:L41-L71）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/precision_config.py#L41-L71) 定义全部五个字段：`enabled`（L46）、`immediate_grad_update`（L51）、`check_grad_overflow`（L56）、`bf16_master_weights_and_grads`（L63）、`bf16_optimizer_states`（L68），每个字段下方 docstring 就是文档来源。[get_bfloat16_config（L34-L38）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/precision_config.py#L34-L38) 先查 `"bf16"` 键，查不到再回退旧键 `"bfloat16"`（L31 注明为兼容保留）。

**② 跨字段校验。** [config.py:L706-L714](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/config.py#L706-L714)：低精度 master 开关必须搭配 ZeRO stage 1/2/3；`bf16_optimizer_states` 必须以 `bf16_master_weights_and_grads` 为前提。配置错误在引擎构造前就被拦下。

**③ 卸载约束。** [_configure_master_weights（base_optimizer.py:L498-L542）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/base_optimizer.py#L498-L542) 是 ZeRO stage 1/2/3 共用的收口：L513-L519 做互斥/依赖断言；L524-L532 处理卸载——若 master 用 bf16 而矩留 fp32（fp32 矩在 GPU 上放不下省下来的空间），必须走 ZeRO-Offload 且用 `DeepSpeedCPUAdam`；反过来"bf16 矩 + 卸载"则要求 `DeepSpeedCPUAdam` 以 `fp32_optimizer_states=False` 构造，否则 CPU 优化器会把矩**静默扩回 fp32**，省显存的意图落空。L537-L542 按 fp16/bf16/默认返回 master dtype。

**④ 文档矩阵。** [docs/_pages/config-json.md:L363-L368](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/docs/_pages/config-json.md#L363-L368) 给出 ZeRO stage × 两个开关的支持矩阵，可作为速查表。

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

**预期结果**：第 1 步在 [config.py:L714](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/config.py#L714) 断言失败（`bf16_optimizer_states requires bf16_master_weights_and_grads`）；第 2 步在 [config.py:L711-L713](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/config.py#L711-L713) 失败（stage 0 不支持）；第 3 步会因未使用 `DeepSpeedCPUAdam` 被 [base_optimizer.py:L529-L532](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/base_optimizer.py#L529-L532) 附近的卸载校验拦截（具体报错文本以运行为准，待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`check_grad_overflow` 字段控制谁的行为？

**答案**：控制 **ZeRO stage 1/2 优化器**在 bf16 模式下是否做梯度溢出检测（[engine.py:L2665-L2675](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2665-L2675) 读取后传给 `_configure_zero_optimizer`，在 stage_1_and_2.py L3134 附近消费）。专门化 `BF16_Optimizer` 的 `step` 则总是检查 `is_invalid_grad_norm`，不读这个字段。字段默认值 True 是为了与 fp16 的默认行为对齐（docstring 引用 issue #5242 / PR #6976）。

**练习 2**：为什么"bf16 master + fp32 矩"必须搭配 CPU 卸载？

**答案**：这个组合省的是 master 的 2 字节/参数（分区），但矩仍是 8 字节/参数（fp32）。如果不卸载，fp32 矩留在 GPU 上，显存大头没省；卸载到 CPU 后矩占用的是主机内存，GPU 上只剩 bf16 master 与梯度，显存收益才兑现——所以校验强制 `DeepSpeedCPUAdam`。反过来"全 bf16"时状态足够小，可以留在 GPU 也可以卸载（见文档矩阵）。

### 4.3 keep_in_fp32 精度策略

#### 4.3.1 概念说明

`keep_in_fp32.py` 解决的是混合精度的一个"少数派报告"问题：**个别张量在低精度下会直接破坏模型行为，必须豁免于 dtype 转换**。最典型的是 MoE 模型的路由偏置 `e_score_correction_bias`（DeepSeek-V3、GLM-4.5/5 等都有）：它在 GLM-5.2 中取值约 5~20，相邻值差小于 0.01，而 bf16 在该量级的相邻可表示数间距是 0.03~0.125——转成 bf16 后 256 个专家只剩 3~16 个不同取值，**token 会被路由到错误的专家**。这类误差不像梯度噪声那样可平均掉，而是系统性的决策错误。

两个要点：

- 豁免对象是 HuggingFace 模型类属性 `_keep_in_fp32_modules_strict`（bf16/fp16 下都保留）与 `_keep_in_fp32_modules`（仅 fp16 下保留）点名的**缓冲区（buffer）**；参数即使名字匹配也仍跟随训练 dtype（模块 docstring L25 明确此边界）。
- 配置项 `data_types.keep_in_fp32_modules` 决定名单来源：`"auto"`（默认，取模型自带的两个列表）、自定义通配模式列表、`[]`（全部不保留）。

#### 4.3.2 核心流程

```text
引擎构造期（bf16/fp16 启用时）
  keep_in_fp32_pattern(model, setting, dtype)
      → 遍历 named_modules，收集各子模块两个类属性里的名字
      → "*" 转正则 ".*"，加上模块名前缀，合成一个交替匹配的编译正则
  → 引擎 _cast_module_mixed_precision 转换模型 dtype 时跳过匹配的 buffer
  → keep_buffers_in_fp32 把已转换的匹配 buffer 就地 .float() 恢复
ZeRO-3 场景
  → zero.Init 构建期同样调用这两个函数（partition_parameters.py）
```

#### 4.3.3 源码精读

**① 名单与动机。** 模块 docstring [keep_in_fp32.py:L5-L26](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/keep_in_fp32.py#L5-L26) 完整交代了背景：transformers 自己加载模型时会尊重这两个列表，但在 ZeRO-3 下 `zero.Init` 把 `__init__` 里创建的每个浮点张量都转成训练 dtype，transformers 的加载器再把检查点拷进这些张量，名单就丢了——所以 DeepSpeed 必须在引擎侧重新实现这份豁免。

**② 模式编译。** [keep_in_fp32_pattern（L35-L58）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/keep_in_fp32.py#L35-L58)：dtype 是 fp32 时直接返回 `None`（纯 fp32 训练无需豁免，L40-L41）；`auto` 模式下遍历模块收集名单，fp16 时额外并入 `_keep_in_fp32_modules`（L46-L47）；最后 `"|".join` 成单个正则（L58），匹配规则与 transformers 的 `build_glob_glob_alternation` + `re.search` 一致——模式可以匹配完整 buffer 名的任意位置。

**③ buffer 的筛选与就地转换。** [buffers_to_keep_in_fp32（L61-L69）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/keep_in_fp32.py#L61-L69) 只挑**浮点 buffer**（`recurse=False` 防止重复计数）；[keep_buffers_in_fp32（L72-L79）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/keep_in_fp32.py#L72-L79) 把 dtype 不是 fp32 的匹配 buffer 用 `buf.data = buf.data.float()` 就地恢复并计数。

**④ 两个消费点。** 非 ZeRO-3 路径在 [engine.py:L2113-L2121](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2113-L2121)：dtype 转换只针对参数、默认保留 fp32 buffer（注释点名 rotary 的 `inv_freq`），`keep_pattern` 交给 `_cast_module_mixed_precision`；ZeRO-3 路径在 [partition_parameters.py:L1256-L1259](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/partition_parameters.py#L1256-L1259) 于 `zero.Init` 期间调用同一对函数。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：在不安装任何 MoE 模型的情况下，用一个小模块验证模式编译与 buffer 豁免逻辑。

**操作步骤**：

1. 写脚本（示例代码，仅依赖 deepspeed 可导入，不需要初始化分布式）：

   ```python
   import torch
   from deepspeed.runtime.keep_in_fp32 import (
       keep_in_fp32_pattern, keep_buffers_in_fp32)

   m = torch.nn.Module()
   m.register_buffer("e_score_correction_bias", torch.linspace(5, 20, 256))
   m._keep_in_fp32_modules_strict = ["e_score_correction_bias"]

   pat = keep_in_fp32_pattern(m, "auto", torch.bfloat16)
   print("pattern:", pat.pattern if pat else None)
   n = keep_buffers_in_fp32(m.bfloat16(), pat)   # 先整体转 bf16 再恢复
   print("converted:", n, "dtype:", m.e_score_correction_bias.dtype)
   print("bf16 可分辨取值数:", m.e_score_correction_bias.to(torch.bfloat16).unique().numel())
   ```

2. 对照第 2 节的数值分析，观察 `unique().numel()` 与 256 的差距。

**需要观察的现象**：pattern 是一条以模块上下文为前缀、包含 `e_score_correction_bias` 的正则；转换计数为 1，dtype 回到 `torch.float32`；bf16 化后唯一值数量骤降（量级在个位数到十几种）。

**预期结果**：与 docstring 中"256 个专家只剩 3~16 个不同取值"的描述同量级，直观感受路由偏置被 bf16 压扁的后果。具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么豁免名单只覆盖 buffer 而不含参数？

**答案**：参数参与梯度更新，主权重机制（fp32 masters）已经保证其更新精度，bf16 只是前向/反向的工作副本，噪声可被优化过程平均掉；而路由偏置这类 buffer 是**前向决策的只读查表输入**，没有 master 副本兜底，一旦量化就是确定性错误，所以必须整体留在 fp32。

**练习 2**：`keep_in_fp32_pattern` 在 `dtype=torch.float32` 时返回 `None`，下游为什么不会崩？

**答案**：`None` 表示"无需豁免"，两个消费点拿到 `None` 后跳过匹配逻辑（`pattern.search` 不会被调用），dtype 转换本身在 fp32 训练下也是空操作，语义自洽。

### 4.4 load_state_dict 的 masters 重建（PR #8892）

#### 4.4.1 概念说明

检查点恢复时，fp32 masters 从哪里来是一个有损/无损的分叉：

- **无损**：从检查点里保存的 `SINGLE_PARTITION_OF_FP32_GROUPS`（fp32 分区副本）直接拷贝。
- **有损重建**：丢弃保存的 masters，从已加载的 bf16 权重升精度重建（`_restore_from_bit16_weights`）——bf16 只有 7 位尾数，重建必然丢掉 masters 中低于 bf16 分辨率的位。

PR #8892（上一更新周期）补全了这个语义矩阵，核心变化是 `BF16_Optimizer._load_legacy_checkpoint` 新增 `load_from_fp32_weights=False` 分支：**此前该参数被静默忽略（无论如何都拷贝保存的 fp32 分区），False 时改为从 bit16 权重重建**，与 ZeRO stage 1/2 的既有行为（[stage_1_and_2.py:L3799-L3808](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3799-L3808) 的 docstring 写明两种选项）对齐。ZeRO-3 的 `_rigid_load_state_dict` / `_load_universal_checkpoint` 同一 PR 也加了同样的分支（本讲聚焦 BF16 优化器，ZeRO-3 侧详见 u5-l8）。

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
  │  硬编码 load_from_fp32_weights=True（engine.py L4821-L4826）
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

**① 重建函数本体。** [_restore_from_bit16_weights（bf16_optimizer.py:L650-L654）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L650-L654)：对本 rank 的 bf16 分区做 `fp32_partition.data.copy_(bf16_partitions[partition_id].data)`——注意这是 4.1.⑤ `update_lp_params` 的**反方向**拷贝（那边是 fp32→bf16，这边是 bf16→fp32），升精度时低 16 位尾数填零。[refresh_fp32_params（L656-L657）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L656-L657) 是它面向 ZeRO-3 接口风格的外部别名。

**② 分发入口。** [load_state_dict（L659-L669）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L659-L669)：`checkpoint_folder` 存在走 universal（[_load_universal_checkpoint（L695-L698）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L695-L698)），否则走 legacy。

**③ else 分支。** [_load_legacy_checkpoint（L671-L693）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L671-L693)：

- [L680-L682](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L680-L682)：仅当 `load_optimizer_states=True` 才恢复 `clip_grad` 与 moments——这就是"masters 保留、moments 重开"语义的来源。
- [L684-L688](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L684-L688)：无损路径，保存的 fp32 分区经 [_get_padded_tensor（L738-L744）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L738-L744) 处理对齐填充后拷入。
- **[L689-L690](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L689-L690)：`else: self._restore_from_bit16_weights()`**——修复前这里什么都不做（保存的 masters 被无条件沿用，`load_from_fp32_weights=False` 形同虚设）。

**④ 引擎侧调用点。** [engine.py:L4821-L4826](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L4821-L4826)：`load_checkpoint` 调 `optimizer.load_state_dict` 时**硬编码 `load_from_fp32_weights=True`**——所以正常恢复永远保留保存的 masters；`False` 分支只有直接调用优化器接口才会走到（这正是测试 4.4.4 的做法）。

**⑤ 回归测试。** [test_fp32_master_stage3_bf16.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py) 用一个参数的迷你模型把整个矩阵钉死：

- [_train_and_detach_masters（L78-L87）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py#L78-L87) 是关键道具：训练一步后给 masters 手动加上 \(2^{-12}\)——这个增量**介于 fp32 与 bf16 分辨率之间**，使"保留的 masters"与"从 bf16 重建的 masters"可被 `torch.equal` 区分开（L86 先断言二者确实不等）。
- [L115-L125](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py#L115-L125)：`load_optimizer_states=False` → masters 逐元素等于保存值（L120），moments 不存在（L121），优化器全局量未被覆盖（L123-L125）。
- [L127-L136](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py#L127-L136)：`load_module_only=True` → bf16 优化器的 masters 等于"保存值经 bf16 舍入"的结果（L135），即从 bf16 权重重建；注释同时说明 ZeRO-3 因为模型检查点里存的就是 fp32 分区，module-only 仍读原值。
- [L138-L148](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py#L138-L148)：直接以 `load_from_fp32_weights=False` 调 `load_state_dict`，覆盖引擎永远走不到的重建分支——L141 先再扰动一次 masters 确保它们已偏离重建值，L146-L147 断言重建后回到 bf16 舍入值。

#### 4.4.4 代码实践

**实践目标**：用回归测试复现"保留 vs 重建"两种 masters 恢复语义。

**操作步骤**：

1. 运行回归测试（CPU、gloo、world_size=1 即可，与该测试的 CI 环境一致）：

   ```bash
   pytest tests/unit/checkpoint/test_fp32_master_stage3_bf16.py -v
   ```

2. 精读 [_train_and_detach_masters](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py#L78-L87)，理解 \(2^{-12}\) 扰动为什么能把两条路径区分开：master 初值为 1 附近的更新结果，bf16 在 1 附近的间距是 \(2^{-7}\) 量级，fp32 是 \(2^{-23}\) 量级，\(2^{-12}\) 落在两者之间。
3. 把测试抄成一个独立小脚本，改造最后一段：先 `load_optimizer_states=False, load_from_fp32_weights=True` 加载并断言 masters 保留；再对同一引擎直接调 `optimizer.load_state_dict(zero_sd_list, load_optimizer_states=False, load_from_fp32_weights=False)` 并断言 masters 变成 bf16 舍入值（对照 [L143-L147](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py#L143-L147)）。

**需要观察的现象**：两个断言一个 `torch.equal == True`（保留）、一个对 `bf16 舍入值 == True`（重建）；两次加载后 `_has_moments` 都是 False。

**预期结果**：与测试 L120/L135/L147 的断言一致。若在第 3 步独立脚本中观察 `fp32_groups_flat_partition` 的原始字节，保留路径与保存值逐位相同，重建路径低 16 位尾数为零。具体输出待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么引擎 `load_checkpoint` 硬编码传 `True`，而把 `False` 留给直接调用优化器接口？

**答案**：正常恢复的目标是"无损续训"，保存的 fp32 masters 是训练进度的最高保真副本，没有理由丢弃；`False`（从 bf16 重建）服务的是模块级热启动这类场景——你只关心模型权重，愿意接受精度损失换取与模块检查点的组合自由，这类组合由用户显式控制更安全。

**练习 2**：`load_optimizer_states=False` 时 `clip_grad` 不被恢复，这个细节有什么实际影响？

**答案**：masters-only 加载用于"保留权重、重置优化器"的场景；若顺带恢复了保存的 `clip_grad`，会悄悄改变当前运行已配置的裁剪行为，让"只动 moments"的预期落空。测试 L111-L125 显式断言这些全局量保持加载前的值。

**练习 3**：`load_module_only=True` 时，为什么 bf16 优化器选择重建而 ZeRO-3（kind="zero3"）仍能读到保存的 fp32 分区？

**答案**：module-only 加载只读模型检查点（bf16 权重），不碰优化器检查点文件；bf16 专用优化器的模型权重就是 bf16，masters 只能从中重建。ZeRO-3 的参数分区本身存放在 fp32 分片文件里（模型检查点包含分区权重），所以 module-only 也读得到 fp32 原值——这正是测试 L130-L135 注释里两者断言不同的原因。

### 4.5 destroy() 与 _hp_grad 视图清理（本周期 PR #9034）

#### 4.5.1 概念说明

`BF16_Optimizer` 的最后一个问题是**生命周期**问题。很多调用方（最典型的是 HuggingFace Trainer）的工作模式是「创建引擎 → 训练 → `engine.destroy()` → 保留模型继续用（或换配置再包一个新引擎）」。`destroy()` 的契约是：之后 `del engine` + `gc.collect()` 应当把引擎、优化器连同它们的大块缓冲全部回收，而模型完好无损。

本周期之前的 bug（issue #7204，PR #9034 修复）是：`destroy()` 之后一切仍然活着，**每次"创建 → 训练 → 销毁"循环都泄漏一整套引擎**。对 `BF16_Optimizer` 而言，独有的泄漏链就是 4.1.③ 埋下的伏笔：

```text
模型参数 p（调用方保留，活着）
  └─ p._hp_grad = hp_grad        # _update_hp_grad L528 挂上的逐参数视图
        └─ 视图共享 storage
              └─ fp32_groups_gradients_flat 整块扁平缓冲（全量、fp32）
```

按 2.4 的规则，只要模型参数活着、参数身上还挂着这个视图，**整块** fp32 梯度缓冲就无法释放。账本很好算：fp32 梯度每参数 4 字节、全量；bf16 模型每参数 2 字节——缓冲恰为 **2 倍 bf16 模型大小**，每个销毁周期都被钉死一次。PR 实测（2 rank、`Sequential` 套 16 个 `Linear(4096,4096)`、bf16、AdamW、4 个周期取平均）：`BF16_Optimizer` 场景修复前每周期泄漏 +3072.8 MiB 显存，修复后只剩调用方主动保留的模型本身（+512.1 MiB）。

本周期修复：`destroy()` 在清 `_hp_mapping` 的同时，把每个参数的 `p._hp_grad` 也置为 `None`。同一次提交还修了引擎与 ZeRO 侧的同类反向引用（forward hooks、`__del__` 二次拆除、ZeRO-1/2 的 `p._zero_optimizer`、ZeRO-3 的 `p._z3_optimizer` 与 `FWD_MODULE_STACK`）——完整销毁链在 u2-l3 展开，本讲只聚焦 bf16 侧的 `_hp_grad`。

#### 4.5.2 核心流程

```text
engine.destroy()                          # engine.py L1266 起
  ├─ self._destroyed = True               # 让 __del__ 跳过二次拆除（L1255-L1264）
  ├─ optimizer.destroy()                  # L1279-L1281 转发
  │     BF16_Optimizer.destroy()          # bf16_optimizer.py L126-L139
  │       ├─ 逐参数 p._hp_mapping = None  # 原有清理
  │       ├─ 逐参数 p._hp_grad = None     # ← 本周期新增（L133-L136）
  │       └─ 移除 _grad_acc_hooks         # immediate_grad_update 的 autograd hooks
  └─ 摘除模块 forward hooks 等引擎级引用   # 详见 u2-l3

之后 del engine + gc.collect()
  → 引擎、优化器、fp32 梯度缓冲全部可回收
  → 模型（含 bf16 参数）继续可用
```

为什么 `__del__` 要跳过已显式销毁的引擎：被销毁的旧引擎可能在**新引擎包装同一模型之后**才被垃圾回收，此时若 `__del__` 再跑一遍拆除，清掉的将是新引擎挂上去的 `_hp_mapping` 等状态（`safe_get_full_fp32_param` 会返回全零）。`_destroyed` 标记保证拆除至多完整发生一次。

#### 4.5.3 源码精读

**① 视图的诞生与挂载。** [_update_hp_grad（bf16_optimizer.py:L511-L529）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L511-L529)：L523 取出的 `hp_grad` 是 `fp32_groups_gradients[group_idx][param_idx]`——即 4.1.② L203-L206 里从扁平缓冲 split 出来的**逐参数视图**；L527 把 bf16 梯度累加进去之后，L528 `lp._hp_grad = hp_grad` 把视图挂到参数上。训练期间它无害（下次 backward 覆盖、step 后 `clear_hp_grads` 只清零不释放）；问题只出现在"参数活得比优化器久"的销毁场景。

**② destroy() 本体。** [BF16_Optimizer.destroy（bf16_optimizer.py:L126-L139）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L126-L139)：原有的 `_hp_mapping` 清理之后，[新增的 L133-L136](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L133-L136) 用 `getattr` 防御式检查 `_hp_grad` 并置空，注释直接写明动机："`_update_hp_grad()` leaves a view of the fp32 gradient buffer here; a model kept after destroy() would otherwise keep the whole buffer alive."（`_update_hp_grad()` 在这里留下 fp32 梯度缓冲的视图；destroy() 之后被保留的模型否则会钉住整块缓冲。）最后移除 `_grad_acc_hooks`。

**③ 引擎侧的转发与防重入。** [engine.destroy（engine.py:L1266-L1292）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1266-L1292)：L1267 先置 `_destroyed`；[L1279-L1281](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1279-L1281) 调 `optimizer.destroy()`；随后摘除闭包引用引擎的模块 hooks（这段属于 u2-l3 的主线）。[__del__（L1255-L1264）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1255-L1264) 看到该标记即直接返回，注释点明原因：显式销毁的引擎可能在新引擎包装同一模型后才被回收，二次拆除会清掉新引擎的状态。

**④ 回归测试。** [TestDestroyReleasesEngine（test_zero.py:L2277-L2375）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero.py#L2277-L2375) 把销毁契约钉死，7 个参数化场景（[L2280-L2291](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero.py#L2280-L2291)）：ZeRO-1/2/3 各带/不带 CPU 卸载，再加 `(1, False, "fp32")`——注释明确写着"With bf16 parameters, this selects BF16_Optimizer"。与 `_hp_grad` 直接相关的两段：

- [L2334-L2344](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero.py#L2334-L2344)：`destroy()` 之后记录 `fp32_groups_gradients_flat` 各缓冲的 `untyped_storage().data_ptr()`，`del` + `gc.collect()` 后断言 `engine_ref()`/`optimizer_ref()` 为 `None`，且**没有任何参数还持有这些缓冲的 storage**。注释解释了为什么比较 storage 而不是张量对象：视图会在缓冲自己的张量对象被回收后继续钉住其 storage。
- [_storages_held_by_params（L2369-L2375）](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero.py#L2369-L2375)：遍历每个参数的 `param.grad` 与 `vars(param)` 里所有张量值，收集它们的 storage 指针——`_hp_grad` 就藏在 `vars(param)` 里，这正是能抓到本 bug 的原因。

测试后半段（L2346-L2366）还验证了两条衍生契约：被销毁的旧引擎在被回收时不得破坏接替它包装同一模型的新引擎（`safe_get_full_fp32_param` 前后一致）；最后一个引擎销毁后模型本身也可被回收。

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

3. 对照 [L2334-L2344](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero.py#L2334-L2344) 的断言方式，把第 2 步改成「比较 `storage_before` 与 `_storages_held_by_params(model)` 收集的指针集合」再验一遍。

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

把本讲的对比线索（bf16 vs fp16）与生命周期线索（销毁不泄漏）落成一次可复现实验（GPU 环境最佳，CPU 可跑 bf16 部分）：

1. **搭建对照脚本**：以 [tests/unit/checkpoint/test_fp32_master_stage3_bf16.py 的 _OneParam](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/checkpoint/test_fp32_master_stage3_bf16.py#L20-L27) 为蓝本放大成一个两层 MLP（隐藏维 1024），写两份配置：`"fp16": {"enabled": true}` 与 `"bf16": {"enabled": true}` + `"data_types": {"grad_accum_dtype": "fp32"}`，其余字段（优化器、lr、batch size）完全相同，ZeRO stage=1。
2. **记录显存**：每步前后调用 `torch.cuda.max_memory_allocated()`，跑 50 步，输出两列峰值曲线。
3. **记录数值行为**：fp16 路径打印每步 loss scale（`engine.optimizer.cur_scale`，属性名以运行时为准）；bf16 路径确认没有任何 scale 变量参与。对比两者最终 loss。
4. **解释差异**：预期 bf16 峰值略高（fp32 梯度缓冲 4 字节/参数 vs fp16 的 2 字节，且 bf16 路径持有全量 fp32 梯度），但省去溢出重试；把每项差异对应回 4.1.② 的内存布局表逐项记账。
5. **收尾（检查点）**：对 bf16 引擎 `save_checkpoint` 后，按 4.4.4 第 3 步分别验证保留/重建两种加载，把三条断言结果贴进实验报告。
6. **收尾（生命周期）**：训练结束后调用 `engine.destroy()`，按 4.5.4 第 2 步用 `weakref` 验证引擎、优化器、fp32 梯度缓冲都被回收且模型仍可前向；再循环「initialize → 训练 10 步 → destroy → del → gc.collect」4 次，用 `torch.cuda.max_memory_allocated` 对比第 1 轮与第 4 轮的增量，确认没有周期性泄漏。

报告模板：一页对照表（每步峰值显存、最终 loss、loss scale 轨迹、加载语义断言、销毁后弱引用状态），加一段"差异来源"分析。全部数值待本地验证。

## 6. 本讲小结

- bf16 的 8 位指数带来与 fp32 等宽的动态范围，梯度不下溢，因此 **bf16 训练不需要 loss scaling**；`BF16_Optimizer` 里的 scaler 属性全是 API 兼容占位符，溢出处理退化为"梯度 inf/nan 即丢步"。
- `BF16_Optimizer` 的内存布局是"bf16 全量扁平权重 + 本 rank 的 fp32 master 分区 + fp32 全量梯度缓冲"，底层优化器的参数组被重绑定到 fp32 分区，更新后再降精度写回并 all-gather。
- 专门化路径的触发条件是 **ZeRO stage=1 + bf16 模型 + fp32 梯度累积 + 无卸载**（关 ZeRO 的 bf16 只有搭配流水线并行才走它；普通 DDP bf16 走 FP16 家族包装器）。
- 低精度 master states（`bf16_master_weights_and_grads` / `bf16_optimizer_states`）作用于 ZeRO 优化器，有"依赖 + stage 范围 + 卸载后端"三重校验；`keep_in_fp32` 则反向保护少数必须留在 fp32 的 buffer（如 MoE 路由偏置）。
- PR #8892 补全了 masters 恢复语义：`load_from_fp32_weights=False` 现在真正从 bit16 权重重建 masters（与 ZeRO-1/2 对齐）；引擎正常恢复硬编码 `True`；`load_optimizer_states=False` 保留 masters、重开 moments、不动优化器全局量。
- 本周期 PR #9034 补全了销毁语义：`_update_hp_grad` 挂在参数上的 `_hp_grad` 视图会替被保留的模型钉住整块 fp32 梯度缓冲（约 2 倍 bf16 模型大小），`destroy()` 现在显式置空它——配合引擎级 hooks 摘除与 `_destroyed` 防重入，"创建 → 训练 → 销毁"循环不再泄漏。

## 7. 下一步学习建议

- **u5-l1（ZeRO 总览）**：`BF16_Optimizer` 本质是"只切 master/优化器状态"的极简 ZeRO-1，进入 ZeRO 单元后可以对照 `stage_1_and_2.py` 理解完整版实现（IPG bucket、overlap 通信）多了什么；本周期 ZeRO-1/2 CPU 卸载的梯度累计器也重构成"按分区组的 pinned 缓冲"，与本讲的 fp32 梯度缓冲是同类设计。
- **u5-l8（ZeRO 检查点与权重导出）**：本讲 4.4 只讲了 BF16 优化器的加载语义，ZeRO-3 的 `_rigid_load_state_dict`、流水线串行加载的分片预检查（同属 PR #8892）在那篇展开。
- **u2-l3（引擎构造与销毁）**：本讲 4.5 只讲了 `BF16_Optimizer` 侧的 `_hp_grad` 清理；完整的 destroy 链——forward hooks 摘除、`__del__` 防重入、ZeRO-1/2 的 `p._zero_optimizer`、ZeRO-3 的 `p._z3_optimizer` 与 `FWD_MODULE_STACK` 弹栈、协调器 `lru_cache` 的实例化——在那篇系统展开。
- **源码延伸**：对照读 [stage_1_and_2.py 的 _load_legacy_checkpoint docstring](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3799-L3808)，看"fp32 无损拷贝 vs bit16 有损重建"两种选项在完整 ZeRO 优化器里如何与弹性加载（改 DP 数量）交互。
