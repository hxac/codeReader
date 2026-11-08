# ZeRO-Offload：CPU 卸载与 cpu_adam

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出开启 `offload_optimizer` 后，模型参数、fp32 主权重、梯度分区与优化器状态分别驻留在哪一类内存（GPU / CPU 普通内存 / CPU pinned 内存）。
2. 解释 pinned（页锁定）内存在 CPU 卸载路径中的作用：为什么 `non_blocking=True` 的拷贝只有落在 pinned 缓冲上才真正异步。
3. 读懂 `DeepSpeedCPUAdam` 的构造与 `step()`：它如何惰性创建 Adam 状态、如何把更新委托给 C++ 的 `cpu_adam` 内核、内核又如何按「参数精度 × 状态精度」组合分发到 SIMD 向量化实现。
4. 说明本周期（PR #8974）新增的公共基类 `DeepSpeedCPUOptimizer`：为什么 torch 的 `load_state_dict` 会把 fp32 优化器状态悄悄降成 bf16/fp16，基类如何用 `restore_optimizer_state_dtype` 把它恢复回来，从而让 bf16 卸载恢复不再静默丢失精度。
5. 对比 fused CPU 优化器（`DeepSpeedCPUAdam`）与 `torch.optim.AdamW` 在 CPU 上的性能差异。

## 2. 前置知识

- **ZeRO Stage 1/2**（前置讲义 u5-l3）：优化器状态（Stage 1）与梯度（Stage 2）按数据并行度切分，每个 rank 只维护并更新属于自己的那一份分区；本讲只改「这份分区放在哪台设备上、由谁更新」。
- **加速器抽象层**（前置讲义 u3-l1）：所有设备相关操作都经 `get_accelerator()` 走抽象接口，本讲反复出现的 `get_accelerator().pin_memory(...)` 就是其中之一——CUDA 后端会真正调用 `cudaHostRegister` 式的页锁定，而 CPU 后端是历史性 no-op。
- **pinned（page-locked）memory**：操作系统默认可以把 CPU 内存页换出到磁盘（pageable）。GPU 的 DMA 引擎只能对「页锁定」的 CPU 地址做直接异步传输；对 pageable 内存，驱动必须先在内部把数据拷贝到一块临时 pinned 区域，拷贝也就无法与计算重叠。代价是 pinned 内存不能被换出、会钉死物理内存，所以 `pin_memory` 默认开、但允许显式关掉（内存紧张的主机）。
- **低精度训练**（u4 系列）：fp16/bf16 参数必须搭配 fp32 主权重（masters）与 fp32 优化器状态，moments（`exp_avg`、`exp_avg_sq`）在 fp32 中累积才不丢精度。
- **torch 优化器 `state_dict` 语义**：torch 的 `Optimizer.load_state_dict` 在加载时会把状态张量 cast 成**当前参数的 dtype**。这条「贴心」规则正是本周期修复的根源。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `deepspeed/runtime/zero/offload_config.py` | 卸载配置的 pydantic 模型：device、pin_memory、NVMe 相关字段 |
| `deepspeed/runtime/engine.py` | `zero_use_cpu_optimizer()` 判定与 `get_optimizer_configuration()`：何时装配 `DeepSpeedCPUAdam`/`DeepSpeedCPULion`/`DeepSpeedCPUAdagrad`，以及 `fp32_optimizer_states` 的默认值推导 |
| `deepspeed/runtime/zero/stage_1_and_2.py` | ZeRO-1/2 优化器：卸载数据布局的建立（pinned 分区、bit16 拷贝缓冲）、`step()` 的 CPU 分支 |
| `deepspeed/ops/cpu_optimizer.py` | **本周期新增**的公共基类 `DeepSpeedCPUOptimizer`（覆盖 `load_state_dict`） |
| `deepspeed/ops/adam/cpu_adam.py` | `DeepSpeedCPUAdam`：Python 侧状态管理与对内核的调用 |
| `deepspeed/utils/torch.py` | `restore_optimizer_state_dtype`：按保存值恢复浮点状态 dtype |
| `csrc/adam/cpu_adam.cpp` / `csrc/adam/cpu_adam_impl.cpp` | C++ 内核的 pybind 导出与实现（SIMD 分发） |
| `deepspeed/ops/lion/cpu_lion.py`、`deepspeed/ops/adagrad/cpu_adagrad.py` | 同样继承新基类的两个兄弟 CPU 优化器 |
| `tests/unit/ops/adam/test_cpu_adam.py` | 单测：参考实现对照、dtype 保持、bf16 状态开关 |
| `tests/unit/checkpoint/test_zero_optimizer.py` | 集成测试：bf16 master weights + CPU 卸载下的断点恢复 |

## 4. 核心概念与源码讲解

### 4.1 offload 配置解析与 CPU 优化器的装配

#### 4.1.1 概念说明

ZeRO-Offload 回答的问题是：显存不够、CPU 内存管够时，能不能把「优化器状态 + 优化器计算」搬到 CPU，同时不让训练吞吐崩掉？它的核心赌注是——数据搬运量与 CPU 端计算量都只有 \(O(\Psi/d)\)（\(\Psi\) 为参数总量、\(d\) 为数据并行度），只要 CPU 端用一个多线程向量化的 Adam 内核把这部分计算压到足够快，GPU 就几乎不用等。

「单一卸载」与「双重卸载」的区别可以直接从配置模型上读出来：

- **单一卸载**：只把优化器状态（含 fp32 主权重分区、梯度分区、moments）卸到 CPU。`DeepSpeedZeroOffloadOptimizerConfig` 的文档写明「Valid with stage 1, 2, and 3」。
- **双重卸载**：参数本身也卸载（`offload_param`），该配置块写明「Valid only with stage 3」——参数卸载需要按需 all-gather 的参数协调器，那是 ZeRO-3（u5-l5）与 ZeRO-Infinity（u5-l7）的内容。

还有一个容易忽略的细节：`device` 支持 `cpu` 和 `nvme` 两种取值，但「**优化器计算总是搬到 CPU**，无论 device 填什么」（NVMe 只是让状态再往下沉一层到磁盘）。因此只要 `offload_optimizer.device != none`，走到的都是本讲的 CPU 优化器路径。

#### 4.1.2 核心流程

配置到优化器实例的装配链：

```
JSON: zero_optimization.stage=2, offload_optimizer.device="cpu"
  └─ engine.zero_use_cpu_optimizer()           # device ∈ {cpu, nvme} → True
      └─ _configure_basic_optimizer
          ├─ optimizer=Adam/AdamW → get_optimizer_configuration()
          │     └─ 返回 DeepSpeedCPUAdam + {'adamw_mode', 'fp32_optimizer_states'}
          ├─ optimizer=Adagrad → DeepSpeedCPUAdagrad
          └─ optimizer=Lion   → DeepSpeedCPULion
  └─ DeepSpeedZeroOptimizer(init_optimizer=DeepSpeedCPUAdam, offload_optimizer_config=...)
      └─ self.cpu_offload = True; self.device = 'cpu'
```

#### 4.1.3 源码精读

卸载配置的解析结果——device 枚举与 `pin_memory` 默认值，见 [deepspeed/runtime/zero/offload_config.py:L55-L82](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/offload_config.py#L55-L82)。这段定义了 `DeepSpeedZeroOffloadOptimizerConfig`：`device` 只能是 `none/cpu/nvme`，`pin_memory` 默认 True，文档字符串解释了为什么（异步满带宽传输、与计算重叠）以及何时该关（`ulimit -l` 紧张或物理内存很小，pinned 内存不能换页）。同文件 [L21-L28](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/offload_config.py#L21-L28) 的参数卸载配置则写明只对 stage 3 有效。

引擎侧的判定函数在 [deepspeed/runtime/engine.py:L1581-L1589](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/engine.py#L1581-L1589)：`zero_use_cpu_optimizer()` 对 cpu 与 nvme 都返回 True，`zero_cpu_offload()` 只对 cpu 返回 True——两个名字的差别正对应「优化器计算总在 CPU（含 NVMe 场景）」与「状态驻留 CPU」两件事。

CPU Adam 的实际选择与 `fp32_optimizer_states` 的默认值推导在 [deepspeed/runtime/engine.py:L2396-L2419](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/engine.py#L2396-L2419)。注意三层逻辑：用户显式传 `torch_adam: true` 则完全绕开（用 torch 的 Adam）；开启了 `bf16_optimizer_states` 会强制 `fp32_optimizer_states=False` 并打警告（moments 直接用 bf16 存，省内存但精度降）；否则默认 `fp32_optimizer_states=True`。Adagrad 与 Lion 的分支在 [deepspeed/runtime/engine.py:L2514-L2530](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/engine.py#L2514-L2530)，同样只在 `zero_use_cpu_optimizer()` 为真时换用 DeepSpeed 版。

最后，ZeRO-1/2 优化器构造函数里对卸载配置的消化在 [deepspeed/runtime/zero/stage_1_and_2.py:L220-L225](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/stage_1_and_2.py#L220-L225)：`cpu_offload` 与 `cpu_offload_pin_memory` 两个布尔位来自配置；[L276-L300](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/stage_1_and_2.py#L276-L300) 则把 `self.device` 切到 `'cpu'`，并强制 `contiguous_gradients=True`——CPU 卸载需要连续梯度桶（u5-l3 讲过的 IPG 缓冲）。

#### 4.1.4 代码实践

1. **实践目标**：亲手走一遍「JSON → 配置对象 → CPU 优化器选择」的判定链，验证 `bf16_optimizer_states` 对 `fp32_optimizer_states` 的覆盖关系。
2. **操作步骤**：写一个脚本（示例代码）：

```python
# 示例代码：纯配置层实验，无需 GPU
from deepspeed.runtime.config import DeepSpeedConfig

def make_cfg(offload_dev="cpu", bf16_states=False, user_fp32=None):
    optim_params = {"lr": 1e-3}
    if user_fp32 is not None:
        optim_params["fp32_optimizer_states"] = user_fp32
    return DeepSpeedConfig({
        "train_batch_size": 8,
        "bf16": {"enabled": True},
        "optimizer": {"type": "Adam", "params": optim_params},
        "zero_optimization": {
            "stage": 2,
            "offload_optimizer": {"device": offload_dev, "pin_memory": True},
            "bf16_optimizer_states": bf16_states,
        },
    })

cfg = make_cfg()
print(cfg.zero_config.offload_optimizer.device)      # 期望 cpu
print(cfg.zero_config.offload_optimizer.pin_memory)  # 期望 True
```

   再对照 [deepspeed/runtime/engine.py:L2407-L2419](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/engine.py#L2407-L2419) 手工推演三种组合下 `fp32_optimizer_states` 的最终取值：默认（None）+ bf16_optimizer_states=False；显式 True + bf16_optimizer_states=True；显式 False。
3. **需要观察的现象**：配置对象能成功构造；offload 字段被解析为枚举 `cpu`。
4. **预期结果**：三种组合的推导结果分别为 True / False（带覆盖）/ False。注意覆盖逻辑发生在 `get_optimizer_configuration()` 运行期，而非 `DeepSpeedConfig` 构造期，纯配置脚本只能验证前两行打印。
5. 引擎级完整验证依赖 GPU 与分布式环境，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`offload_optimizer.device` 填 `nvme` 时，Adam 的计算发生在哪里？状态放在哪里？

答案：计算仍在 CPU——`zero_use_cpu_optimizer()` 对 nvme 也返回 True，装配的仍是 `DeepSpeedCPUAdam`；nvme 只是让优化器状态（和参数）进一步沉到磁盘，读写由 swap_tensor 包的异步 I/O 完成（u5-l7 专题）。

**练习 2**：为什么参数卸载（`offload_param`）不支持 ZeRO-1/2？

答案：`DeepSpeedZeroOffloadParamConfig` 标注「Valid only with stage 3」。ZeRO-1/2 中参数始终保持完整地在 GPU 上（这也是它们与 Stage 3 的本质区别），没有按需聚合参数的机制（module hook、参数协调器），无从「卸载再取回」。

**练习 3**：`zero_use_cpu_optimizer()` 与 `zero_cpu_offload()` 的返回值在什么配置下不同？

答案：`offload_optimizer.device == "nvme"` 时——前者 True（优化器计算照旧搬 CPU），后者 False（状态不在 CPU 内存常驻，而在 NVMe）。

### 4.2 pinned buffer 管理：页锁定内存上的卸载数据布局

#### 4.2.1 概念说明

开启卸载后，ZeRO-1/2 每一步都在 GPU 与 CPU 之间搬运两类数据：

- **下行**（GPU→CPU）：DP 组 reduce-scatter 出的本 rank 梯度分区，落到 CPU 的 fp32 梯度分区；
- **上行**（CPU→GPU）：CPU 上更新完的 fp32 主权重分区，降精度为 bit16 后写回 GPU 参数分区。

这两条搬运若想与 GPU 计算重叠，落点就必须是 pinned 缓冲。设 \(\Psi\) 为参数元素总量、\(d\) 为 DP 世界大小，ZeRO-2 + CPU 卸载时的内存布局是：

| 数据 | 位置 | 大小（元素数） | 是否 pinned |
| --- | --- | --- | --- |
| bit16 模型参数（完整） | GPU | \(2\Psi\) 字节 | — |
| fp32 主权重分区 `single_partition_of_fp32_groups` | CPU | \(4\Psi/d\) 字节 | 可选（默认是） |
| fp32 梯度分区（挂在分区的 `.grad` 上） | CPU | \(4\Psi/d\) 字节 | 可选（默认是） |
| Adam moments（`exp_avg`/`exp_avg_sq`） | CPU | \(8\Psi/d\) 字节 | 否（内核自建，惰性） |
| bit16 拷贝缓冲 `param_buffer_of_bit16_for_cpu_offload_groups` | CPU | \(2\Psi/d\) 字节 | 是 |
| 临时梯度缓冲 `temp_grad_buffer_for_cpu_offload` | CPU | 最大单参数的 bit16 大小 | 可选 |

对比不卸载的 ZeRO-2：GPU 上省下的正是 \(12\Psi/d\) 字节（fp32 权重分区 + 梯度分区 + moments）的优化器状态显存——这就是 ZeRO-Offload 用 CPU 内存换 GPU 显存的基本账。

#### 4.2.2 核心流程

每个训练步（`step()` 的 CPU 卸载分支）：

```
反向结束
  └─ step():
      1. 溢出检查（在 CPU 梯度分区上）
      2. 全局范数计算 → unscale + clip（仍在 CPU 分区上）
      3. _optimizer_step(i) → DeepSpeedCPUAdam.step()     # CPU 计算
      4. fp32 分区 →(降精度)→ bit16 pinned 缓冲 →(non_blocking)→ GPU 参数分区
      5. 清理 averaged_gradients / 预展平缓冲
```

其中第 4 步的 `non_blocking=True` 拷贝之所以敢不等待，正是因为中转缓冲是 pinned 的。

#### 4.2.3 源码精读

**fp32 主权重分区与 bit16 拷贝缓冲的建立**：[deepspeed/runtime/zero/stage_1_and_2.py:L557-L577](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/stage_1_and_2.py#L557-L577)。这段代码先把本 rank 对应的 bit16 分区 `detach().clone().to(device='cpu')` 成 fp32 主权重分区（`pin_memory` 可选），再建一个同形状的 bit16 全零缓冲并在卸载时同样 pin 住；最后 `param_group['params'] = [分区]` 把底层优化器（`DeepSpeedCPUAdam`）的参数组替换为「只含我这一份扁平分区」——这就是 Stage 1/2 中优化器状态天然只按分区存在的原因。

**fp32 梯度分区的建立**：[deepspeed/runtime/zero/stage_1_and_2.py:L936-L949](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/stage_1_and_2.py#L936-L949)（`initialize_optimizer_states`）。`single_partition_of_fp32_groups[i].grad` 是在 CPU 上按分区大小新建的零张量，`cpu_offload_pin_memory` 开启时经 `get_accelerator().pin_memory()` 页锁定。这段同时包含 u5-l3 讲过的 Adagrad 构造期重建。

**临时梯度缓冲**：[deepspeed/runtime/zero/stage_1_and_2.py:L646-L665](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/stage_1_and_2.py#L646-L665)。`temp_grad_buffer_for_cpu_offload` 按「最大单参数」的 bit16 大小分配（可 pin），用于反向中把 GPU 梯度搬进 CPU 分区；旁边还有一个 GPU 侧的 `temp_grad_buffer_for_gpu_offload`。

**step() 的 CPU 分支**：[deepspeed/runtime/zero/stage_1_and_2.py:L3123-L3149](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/stage_1_and_2.py#L3123-L3149)。按组执行：CPU 分区上 `unscale_and_clip_grads` → `_optimizer_step(i)`（内部即调用优化器的 `step()`，见 [L3015-L3035](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/stage_1_and_2.py#L3015-L3035) 的 `_optimizer_step`，它临时把 `param_groups` 换成单组再调 `optimizer.step()`）→ 更新后的 fp32 分区先 `copy_` 进 bit16 pinned 缓冲，再以 `non_blocking=True` 拷回 GPU 参数分区。这就是 pinned 内存在数据布局中的「用武点」。

**pinned 内存的释放**：[deepspeed/runtime/zero/stage_1_and_2.py:L772-L792](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/stage_1_and_2.py#L772-L792)。析构时对每个曾经 pin 过的缓冲调用 `accelerator.unpin_memory()`，注释点明 DeepSpeed 自带的 pinned 分配器与 torch 的 pinned allocator 释放语义不同，必须显式 unpin。

#### 4.2.4 代码实践

1. **实践目标**：源码阅读型实践——把上表逐行落到源码，确认每块缓冲「在哪分配、在哪 pin、在哪释放」。
2. **操作步骤**：在 [stage_1_and_2.py](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/stage_1_and_2.py) 中 `grep -n "pin_memory"`，把命中的每一处归类到表中六行；再追 `get_accelerator().pin_memory` 到 [deepspeed/accelerator/cpu_accelerator.py:L430-L438](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/accelerator/cpu_accelerator.py#L430-L438)，观察 CPU 后端的 no-op 行为（无原生 pinned 通道时原样返回张量）。
3. **需要观察的现象**：约 6 处 `pin_memory` 调用点，全部以 `cpu_offload_pin_memory` 为开关；moments（优化器状态）不在其中——它们由 `DeepSpeedCPUAdam.step()` 惰性创建、从不 pin。
4. **预期结果**：得出「哪些缓冲 pin、哪些不 pin」的完整清单，与 4.2.1 的表格一致。
5. 数值行为与 GPU 相关的部分**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：moments 为什么不 pin？

答案：moments 只在 CPU 内部被 `cpu_adam` 内核读写，从不参与 GPU↔CPU 搬运，pin 对它没有收益，反而白钉 \(8\Psi/d\) 字节的物理内存。

**练习 2**：如果主机 `ulimit -l` 很小，pinned 分配会怎样？

答案：配置文档（offload_config.py L78-L82）给出的建议就是此时设 `pin_memory: false`——pinned 内存属于锁定的物理内存配额，分配失败会直接报错；关闭后缓冲退化为 pageable，拷贝变同步但训练仍可运行。

**练习 3**：上行拷贝为什么分两段（fp32→bit16 pinned 缓冲→GPU），而不是从 fp32 分区直接拷 GPU？

答案：`copy_` 跨 dtype 与跨设备同时进行时中间仍要经过 host 缓冲；显式经过 bit16 pinned 缓冲既完成了降精度，又保证最后一段是 pinned→GPU 的异步 DMA（`non_blocking=True`），可以与后续 GPU 计算重叠。

### 4.3 DeepSpeedCPUAdam 类与 cpu_adam 内核

#### 4.3.1 概念说明

`DeepSpeedCPUAdam` 是一个「薄 Python 壳 + 厚 C++ 内核」的优化器：Python 侧只做状态字典管理与参数校验，真正的 Adam/AdamW 更新在 C++ 中以 AVX512/AVX2/NEON/SVE/RVV 向量化实现并行完成。类文档声称相对 `torch.optim.Adam(W)` 有 5x–7x 加速——直觉是 torch 优化器逐参数张量调度（大量小 kernel 与 Python 循环），而 DeepSpeed 版面对的是 ZeRO-1/2 送来的「单个大扁平分区」，一次调用吃掉整个分区，且释放 GIL 由 C++ 多线程并行。

「参数精度 × 状态精度」是双自由度：分区本身可以是 fp16/bf16/fp32（跟随模型精度），moments 由 `fp32_optimizer_states` 决定 fp32 或与参数同精度。内核按 `(param_dtype, state_dtype)` 组合实例化模板，共五种合法组合。

#### 4.3.2 核心流程

`step()` 对每个有梯度的参数：

```
assert 参数在 CPU                     # 卸载配置错误的显式防线
state = self.state[p]
if state 为空:                         # 惰性初始化
    state['step'] = 0
    state_dtype = fp32 if fp32_optimizer_states else 参数dtype
    state['exp_avg']    = zeros_like(param, dtype=state_dtype)
    state['exp_avg_sq'] = zeros_like(param, dtype=state_dtype)
state['step'] += 1
ds_opt_adam.adam_update(opt_id, step, lr, β1, β2, eps, weight_decay,
                        bias_correction, param, grad, exp_avg, exp_avg_sq)
```

Adam 更新本身：

\[
\begin{aligned}
m_t &= \beta_1 m_{t-1} + (1-\beta_1) g_t \\
v_t &= \beta_2 v_{t-1} + (1-\beta_2) g_t^2 \\
\hat m_t &= m_t/(1-\beta_1^t),\quad \hat v_t = v_t/(1-\beta_2^t) \\
\theta_t &= \theta_{t-1} - \eta\,\hat m_t / (\sqrt{\hat v_t}+\epsilon)
\end{aligned}
\]

（AdamW 的解耦 weight decay：\(\theta_t \leftarrow \theta_t(1-\eta\lambda)\) 单独先行。）

#### 4.3.3 源码精读

**类声明与继承**：[deepspeed/ops/adam/cpu_adam.py:L13-L25](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/ops/adam/cpu_adam.py#L13-L25)——`class DeepSpeedCPUAdam(DeepSpeedCPUOptimizer)`，构造参数中 `fp32_optimizer_states=True` 是默认值；文档字符串（[L26-L39](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/ops/adam/cpu_adam.py#L26-L39)）写明它要求 master 参数常驻 CPU、定位是 ZeRO-Offload 的优化器组件、5x–7x 加速来自 [L31](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/ops/adam/cpu_adam.py#L31)。

**构造过程**：[deepspeed/ops/adam/cpu_adam.py:L67-L98](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/ops/adam/cpu_adam.py#L67-L98)。先把超参打包成 `default_args` 传给基类 `torch.optim.Optimizer`（经 `DeepSpeedCPUOptimizer.__init__`），再探测 CPU 厂商（AMD 上对 fp16 参数告警），登记全局 `opt_id`，然后 `CPUAdamBuilder().load()` 触发 JIT 编译/加载扩展（u1-l3、u9-l1 讲过的 op_builder 体系），最后 `create_adam(...)` 在 C++ 侧创建优化器对象并打印检测到的 SIMD 能力。

**step() 主体**：[deepspeed/ops/adam/cpu_adam.py:L116-L173](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/ops/adam/cpu_adam.py#L116-L173)。关键三处：[L148-L149](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/ops/adam/cpu_adam.py#L148-L149) 的 assert——参数不在 CPU 就报「make sure you enabled 'offload_optimizer': 'cpu'」；[L157-L165](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/ops/adam/cpu_adam.py#L157-L165) 的惰性状态初始化，`state_dtype` 按 `fp32_optimizer_states` 二选一；[L170-L172](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/ops/adam/cpu_adam.py#L170-L172) 把整条更新委托给 C++ 的 `adam_update`。此外还有两个特殊变体：`step_subgroup`（[L175-L210](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/ops/adam/cpu_adam.py#L175-L210)，按子组索引更新，服务 stage3 的分块卸载）与 `rollback_subgroup`（[L212-L254](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/ops/adam/cpu_adam.py#L212-L254)，回退一步，服务 SuperOffload 类回滚语义）。

**pybind 导出**：[csrc/adam/cpu_adam.cpp:L8-L14](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/adam/cpu_adam.cpp#L8-L14) 暴露 `adam_update`/`adam_rollback`/`create_adam`/`destroy_adam` 四个入口（同文件后半段还有 ZenFlow 专用的跨进程变体，属 u9-l3 内容）。

**C++ 实现与 SIMD 分发**：`create_adam_optimizer` 在 [csrc/adam/cpu_adam_impl.cpp:L114-L156](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/adam/cpu_adam_impl.cpp#L114-L156) 创建 `Adam_Optimizer` 并按编译期宏报告 AVX512/AVX2/NEON/SVE/RVV/scalar 能力。分发核心是「invoker 表」：[L200-L222](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/adam/cpu_adam_impl.cpp#L200-L222) 以 `(参数类型, 状态类型)` 二元组为键注册了五种模板实例——`(Half,float)`、`(Half,Half)`、`(BFloat16,float)`、`(BFloat16,BFloat16)`、`(float,float)`；`invoke()`（[L224-L249](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/adam/cpu_adam_impl.cpp#L224-L249)）在运行期按两个张量的实际 dtype 查表，查不到就抛错。`ds_adam_step`（[L251-L277](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/adam/cpu_adam_impl.cpp#L251-L277)）先把四个张量 `contiguous()`（允许非连续视图进入），再查表调用。向量化主体按 8 元素与 4 元素两级块走（如 [L158-L181](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/csrc/adam/cpu_adam_impl.cpp#L158-L181) 的 `Step_8`：先 SIMD 批量，余数落回 `Step_4`/标量尾部），并可用 `parallel` 标志开多线程。

**与 torch AdamW 的等价性证据**：[tests/unit/ops/adam/test_cpu_adam.py:L215-L240](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/ops/adam/test_cpu_adam.py#L215-L240) 的 `test_torch_adamw_equal` 用同一个随机序列驱动 `DeepSpeedCPUAdam` 与 `torch.optim.AdamW` 各 10 步，断言参数范数一致；[L82-L114](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/ops/adam/test_cpu_adam.py#L82-L114) 的 `test_cpu_adam_strict_state_updates` 更进一步：用纯 Python 参考实现逐元素对照参数与两个 moments（这正是练习素材）。

#### 4.3.4 代码实践

1. **实践目标**：直接使用 `DeepSpeedCPUAdam`，量化它相对 `torch.optim.AdamW` 的 CPU 端加速，并验证数值等价。
2. **操作步骤**：写基准脚本（示例代码，纯 CPU 可跑）：

```python
# 示例代码：CPU Adam 基准
import time, torch
from deepspeed.ops.adam import DeepSpeedCPUAdam

N, STEPS, LR = 1_000_000, 100, 1e-3
base = torch.randn(N)

def bench(opt_cls, tag, **kw):
    p = torch.nn.Parameter(base.clone())
    opt = opt_cls([p], lr=LR, **kw)
    grads = [torch.randn(N) for _ in range(8)]  # 预生成，避免把随机数生成算进耗时
    t0 = time.perf_counter()
    for i in range(STEPS):
        p.grad = grads[i % 8]
        opt.step()
    torch.cuda.synchronize() if torch.cuda.is_available() else None
    print(f"{tag}: {time.perf_counter() - t0:.3f}s")
    return p.detach().clone()

p_torch = bench(torch.optim.AdamW, "torch AdamW")
p_ds = bench(DeepSpeedCPUAdam, "DeepSpeed CPUAdam")  # 首次调用会 JIT 编译 cpu_adam
torch.testing.assert_close(p_ds, p_torch, rtol=1e-5, atol=1e-6)
print("数值一致")
```

3. **需要观察的现象**：首次运行会先编译扩展（u9-l1 讲的 `CPUAdamBuilder().load()` JIT 路径，日志会打印 `Adam Optimizer #0 is created with AVX512/... capability.`——取决于你的 CPU）；两段计时与最终一致性断言。
4. **预期结果**：两者数值一致（容差内）；`DeepSpeedCPUAdam` 明显更快（文档口径 5x–7x）。具体倍数随 CPU 与 SIMD 能力变化，**待本地验证**；若断言失败，检查 torch 版本差异导致的 AdamW 数值口径（可放宽 rtol）。
5. 想更严格时，可把 STEPS 拆成 warmup（触发 JIT 与惰性状态初始化）+ 计时两段，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `DeepSpeedCPUAdam` 需要 master 参数在 CPU？它怎么防呆？

答案：它的 C++ 内核直接在 CPU 内存上并行更新，参数在 GPU 时无从下手；`step()` 内的 assert（cpu_adam.py L148-L149）用报错信息引导用户检查 `offload_optimizer: cpu` 配置。配套测试 `test_cpu_adam_gpu_error`（test_cpu_adam.py L374-L388）专门验证 GPU 参数触发 AssertionError。

**练习 2**：invoker 表为什么恰好是五种组合？`(fp16, fp32)` 与 `(fp16, fp16)` 分别对应什么配置？

答案：参数 dtype ∈ {fp16, bf16, fp32} 与状态 dtype ∈ {fp32, 参数同精度} 的合法组合，去掉无意义的 `(fp32, 低精度)` 即五种。`(Half, float)` 对应默认 `fp32_optimizer_states=True` 的 fp16 卸载训练；`(Half, Half)` 对应低精度 master 开启（`fp16_master_weights_and_grads`）或显式 `fp32_optimizer_states=False`。

**练习 3**：`step_subgroup` 与 `step` 的状态键有什么不同？为什么要两个入口？

答案：`step` 以参数对象为键存 `self.state[p]`；`step_subgroup` 以子组整数下标为键存 `self.state[subgroup_id]`。后者让调用方（stage3/SuperOffload 的分块路径）能按子组推进、配合 `rollback_subgroup` 回退，而不必为每个子组构造独立的优化器实例。

### 4.4 DeepSpeedCPUOptimizer 基类与 fp32 优化器状态恢复（本周期修复）

#### 4.4.1 概念说明

本周期（PR #8974）之前的隐患：`DeepSpeedCPUAdam/CPULion/CPUAdagrad` 继承 torch 的 `Optimizer.load_state_dict`，而 torch 的实现会把加载的状态张量 **cast 成当前参数的 dtype**。于是这样一条流水线悄悄坏掉：

```
bf16 参数 + fp32_optimizer_states=True 卸载训练
  → step() 里 moments 一直是 fp32            # 内核走 (BFloat16, float)
  → 保存检查点（moments 以 fp32 落盘）
  → load_checkpoint / load_state_dict
  → torch 基类把 exp_avg/exp_avg_sq cast 成 bf16   # 静默降精度！
  → 恢复后的 step() 走 (BFloat16, BFloat16) 内核，moments 已被舍入
```

每一步都不报错：训练照常继续，只是从被舍入的 moments 出发。PR 描述给出了量化：131,584 个元素中除 3 个外全部被舍入（最大偏差约 0.4%），恢复后与不中断训练的对照最多差 1.0e-2。修复方式是抽出一个公共基类 `DeepSpeedCPUOptimizer`，覆盖 `load_state_dict`：先照常让 torch 完成 cast（保持设备/键结构语义不变），再把浮点状态从**保存值**重新读成 fp32。

#### 4.4.2 核心流程

```
load_state_dict(state_dict)
  ├─ super().load_state_dict(state_dict)      # torch：对齐结构、把状态 cast 成参数 dtype
  └─ if fp32_optimizer_states:
         restore_optimizer_state_dtype(self, state_dict, torch.float)
             ├─ zip(state_dict 的参数序列, 当前 param_groups 的参数序列)
             ├─ 对每个保存的浮点状态张量（跳过 "step" 等非浮点项）：
             │     if optimizer.state[param][key].dtype != fp32:
             │         用 saved_value.to(device=param.device, dtype=fp32) 覆盖
             └─ 逐张量按保存值恢复（非就地），设备跟随参数
```

要点：恢复的来源是 `state_dict` 里的**原始保存张量**，而不是被 cast 后的副本——精度信息从检查点直达内存；`fp32_optimizer_states=False` 时完全不介入（低精度是有意为之，尊重之）；fp32 参数场景 torch 的 cast 本就是恒等，也不受影响。

#### 4.4.3 源码精读

**新基类全文**（本文件本周期新建）：[deepspeed/ops/cpu_optimizer.py:L10-L25](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/ops/cpu_optimizer.py#L10-L25)。`__init__` 把 `fp32_optimizer_states` 存为实例属性；`load_state_dict` 先 `super()` 再条件性恢复。docstring 一句话概括契约：「`step()` 用什么 dtype 维护状态，`load_state_dict` 就用什么 dtype 还回来」。

**恢复函数**：[deepspeed/utils/torch.py:L50-L65](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/utils/torch.py#L50-L65) 的 `restore_optimizer_state_dtype`。三个细节：`zip(saved_ids, params)` 依赖 torch 的 `load_state_dict` 保证的参数顺序对齐；`key == "step"` 与 `not is_floating_point()` 的跳过条件——`step` 在新版本 torch 里是标量张量但整型，不能当浮点恢复；比较 `optimizer.state[param][key].dtype != dtype` 才动手，避免无谓拷贝。

**三个子类的接入**：[deepspeed/ops/adam/cpu_adam.py:L9-L13](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/ops/adam/cpu_adam.py#L9-L13)、[deepspeed/ops/lion/cpu_lion.py:L9-L13](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/ops/lion/cpu_lion.py#L9-L13)、[deepspeed/ops/adagrad/cpu_adagrad.py:L7-L11](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/ops/adagrad/cpu_adagrad.py#L7-L11)——三个 CPU 优化器的基类从 `torch.optim.Optimizer`（间接）换为 `DeepSpeedCPUOptimizer`，构造函数里 `super().__init__` 传三参（params、defaults、fp32_optimizer_states），例如 [deepspeed/ops/adam/cpu_adam.py:L67-L73](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/ops/adam/cpu_adam.py#L67-L73)。

**单元测试**：[tests/unit/ops/adam/test_cpu_adam.py:L129-L163](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/ops/adam/test_cpu_adam.py#L129-L163) 的 `test_cpu_optimizer_load_state_dict_keeps_state_dtype`，参数化覆盖 adam/lion/adagrad × fp32_optimizer_states True/False：bf16 参数跑 3 步，deep copy 的 `state_dict` 充当检查点，加载后断言每个张量状态的 dtype 等于 `step()` 所用的 dtype 且逐元素等于保存值，最后再各走一步断言参数比特级一致（`torch.equal`）——这正是「恢复后的 step 与不中断的 step 同轨」的契约化表达。集成侧，[tests/unit/checkpoint/test_zero_optimizer.py:L90-L126](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/checkpoint/test_zero_optimizer.py#L90-L126) 的 `test_load_fp32_optimizer_states_with_bf16_master_weights` 在 ZeRO stage 1/2/3 × bf16 master weights × CPU 卸载下跑完整的「训练-存档-恢复-对照」流程。

#### 4.4.4 代码实践

1. **实践目标**：复现修复前后的差异——验证 `load_state_dict` 后 moments 仍是 `torch.float`，且恢复后的下一步与不中断路径比特级一致。
2. **操作步骤**：脚本（示例代码，纯 CPU 可跑；cpu_adam 扩展需先编译成功）：

```python
# 示例代码：bf16 卸载恢复的 dtype 契约
import copy, torch
from deepspeed.ops.adam import DeepSpeedCPUAdam

g = torch.Generator().manual_seed(0)
param = torch.nn.Parameter(torch.randn(1024, generator=g).to(torch.bfloat16))
opt = DeepSpeedCPUAdam([param], fp32_optimizer_states=True)
for _ in range(3):
    param.grad = torch.randn(1024, generator=g).to(torch.bfloat16)
    opt.step()
assert opt.state[param]["exp_avg"].dtype == torch.float  # step 用 fp32 维护

resumed_param = torch.nn.Parameter(param.detach().clone())
resumed = DeepSpeedCPUAdam([resumed_param], fp32_optimizer_states=True)
resumed.load_state_dict(copy.deepcopy(opt.state_dict()))  # deep copy 充当检查点

# 断言 1：dtype 未被 torch 降成 bf16（本周期修复点）
for v in resumed.state[resumed_param].values():
    if torch.is_tensor(v) and v.is_floating_point():
        assert v.dtype == torch.float, v.dtype

# 断言 2：同梯度下两条路径走同一步
grad = torch.randn(1024, generator=g).to(torch.bfloat16)
param.grad, resumed_param.grad = grad.clone(), grad.clone()
opt.step(); resumed.step()
assert torch.equal(resumed_param, param)
print("fp32 moments 保持、恢复后同轨 ✔")
```

3. **需要观察的现象**：两个断言通过；把基类行为退回（想象 `load_state_dict` 不覆盖）时断言 1 会失败于 `torch.bfloat16`。
4. **预期结果**：全部通过。该脚本与上游测试 `test_cpu_optimizer_load_state_dict_keeps_state_dtype` 同构，可用 `pytest tests/unit/ops/adam/test_cpu_adam.py -k load_state_dict` 直接跑官方版本对照，**待本地验证**。
5. 附加实验：设 `fp32_optimizer_states=False` 重跑，此时状态本就是 bf16，基类不介入，断言 1 应改为 `torch.bfloat16` 仍通过——确认修复不破坏「有意低精度」的用法。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `restore_optimizer_state_dtype` 要跳过 `"step"`？

答案：`step` 是整型标量（torch 新版本中是 0 维张量），不是浮点状态；把它 `.to(torch.float)` 反而会篡改类型。跳过它也符合 torch 自身「step 保持保存值」的语义（torch.py L61-L63 的注释）。

**练习 2**：修复为什么放在基类而不是各优化器各写一份？

答案：三个 CPU 优化器（Adam/Lion/Adagrad）的 `load_state_dict` 语义完全同构——都由 `fp32_optimizer_states` 决定状态 dtype；AGENTS.md 的代码纪律也要求不为重复逻辑开三份副本。基类同时成为「`step()` 与 `load_state_dict()` 的 dtype 契约」的唯一文档化场所（cpu_optimizer.py 的类 docstring）。

**练习 3**：如果用户显式设 `fp32_optimizer_states=False`，加载 bf16 检查点后 moments 是什么 dtype？这条路径被破坏过吗？

答案：bf16——低精度是有意选择，基类的 `if self.fp32_optimizer_states` 直接不进入恢复逻辑，行为与修复前一致；测试参数化的 `False` 分支正是守住这条不回归。

## 5. 综合实践

把本讲四条线串成一个「配置 → 卸载布局 → CPU 优化器 → 断点恢复」的小实验（示例代码框架）：

1. 用 4.1.4 的方式构造两份配置，唯一差别是 `offload_optimizer.device`（`none` vs `cpu`），stage 均为 2；在 DeepSpeed 初始化后（需要 GPU 与 2 进程，可用 `tests/unit/simple_model.py` 的 `SimpleModel` 与 `deepspeed.initialize`），分别打印 `engine.optimizer.cpu_offload`、`single_partition_of_fp32_groups[0].device`、`.grad.device`，对照 4.2.1 的表格确认数据布局迁移到了 CPU。
2. 对 `cpu` 那份配置，`pin_memory` 切 True/False 各跑 10 步，记录每步 `wall_clock_breakdown` 中 optimizer step 的耗时差（pinned 影响的是搬运重叠，CPU-only 主机上差异可能不可见，此时注明环境限制）。
3. 训练中途 `engine.save_checkpoint`，然后分别以 `load_optimizer_states=True` 恢复，检查 `engine.optimizer.optimizer`（即 `DeepSpeedCPUAdam`）的 `state` 中 moments 的 dtype 是否为 `torch.float`——把 4.4 的断言放进真实引擎链路。
4. 用 4.3.4 的纯优化器基准给第 3 步的 CPU step 耗时提供解释：优化器 step 占比是否与 CPUAdam 的加速倍数一致。

GPU 部分在无卡环境无法执行时，完成 4.3.4 与 4.4.4 两个纯 CPU 实践并注明其余**待本地验证**。

## 6. 本讲小结

- `offload_optimizer.device ∈ {cpu, nvme}` 都会把优化器计算搬到 CPU；参数卸载只在 stage 3 有效——「单一卸载」vs「双重卸载」的配置分界线。
- ZeRO-2 + CPU 卸载的布局：GPU 留 bit16 参数（\(2\Psi\)），CPU 承接 \(12\Psi/d\) 的 fp32 权重分区、fp32 梯度分区与 moments；其中参与 GPU↔CPU 搬运的缓冲按 `pin_memory`（默认 True）页锁定，`non_blocking=True` 的上行拷贝依赖它才真异步。
- `DeepSpeedCPUAdam` 是薄壳重核：Python 侧惰性建状态（dtype 由 `fp32_optimizer_states` 决定）+ 设备防呆 assert，C++ 侧按五种 `(参数精度, 状态精度)` 组合分发到 AVX512/AVX2/NEON/SVE/RVV 向量化内核，对 `torch.optim.AdamW` 有 5x–7x 加速。
- 本周期新增基类 `DeepSpeedCPUOptimizer` 覆盖 `load_state_dict`：torch 基类会把状态 cast 成参数 dtype，bf16/fp16 卸载恢复因此静默丢失 fp32 moments；基类用 `restore_optimizer_state_dtype` 从保存值把浮点状态恢复为 fp32，Adam/Lion/Adagrad 三个 CPU 优化器同时受益。
- 修复的契约一句话：**`step()` 用什么 dtype 维护状态，`load_state_dict()` 就还回什么 dtype**——单测（dtype + 逐元素相等 + 恢复后同一步比特级一致）与 ZeRO 1/2/3 集成测试共同守住它。

## 7. 下一步学习建议

- **u5-l5（ZeRO-3 参数卸载）**：把 `offload_param` 加进来后的完整双重卸载——参数协调器与按需 all-gather。
- **u5-l7（ZeRO-Infinity）**：`device=nvme` 分支的下探——本讲的 CPU 只是三级存储（GPU/CPU/NVMe）的中间层，swap_tensor 包与异步 I/O 内核接管状态搬运。
- **u4-l3（BF16 优化器）**：`bf16_master_weights_and_grads` 与本讲 `fp32_optimizer_states` 的关系——同样是「低精度参数 + fp32 状态」的恢复语义，两条修复（#8892 与 #8974）互为镜像，建议对照阅读。
- 源码延伸：`deepspeed/runtime/superoffload/superoffload_utils.py` 中直接构造 `DeepSpeedCPUAdam` 的用法，以及 `csrc/adam/cpu_adam_impl.cpp` 中 ZenFlow 的跨进程变体（u9-l3）。
