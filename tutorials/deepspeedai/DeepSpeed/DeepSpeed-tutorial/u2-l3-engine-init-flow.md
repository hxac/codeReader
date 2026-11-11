# u2-l3 引擎构造与销毁：DeepSpeedEngine 生命周期全流程

## 1. 本讲目标

学完本讲，你应该能够：

1. 按顺序列出 `DeepSpeedEngine.__init__` 的 8 个关键阶段，并为每个阶段指出对应的源码方法与行号。
2. 理解三层优化器关系：用户传入的 `optimizer`（client optimizer）、`basic_optimizer`、`engine.optimizer` 分别是什么、在哪一步被包装。
3. 说明 `deterministic_mode` 为何必须在引擎构造的最早期应用，以及 `CUBLAS_WORKSPACE_CONFIG` 前置检查的必要性。
4. 弄清 AutoEP/AutoTP 模块注入、`_autotp_frozen_replicated_params` 冻结参数记录、`first_dataloader_check` 等「改变模型或挂 hook」的动作发生在初始化的哪一步。
5. 追踪本周期（提交 `f32e164b9`，PR #9034，修复 issue #7204）补全的销毁链：`destroy()` 如何摘除闭包引用引擎的 forward hooks、`__del__` 如何用 `_destroyed` 标记防止二次拆除、各优化器如何清理指回自己的参数属性（`p._z3_optimizer`、`lp._zero_optimizer`、`lp._hp_grad`、`FWD_MODULE_STACK` 弹栈、协调器的每实例 `lru_cache`）。

## 2. 前置知识

### 2.1 Python 对象生命周期与垃圾回收

- **引用计数**：Python 中每个对象记录「谁在引用我」。引用数归零，对象立即释放。只要还有一个引用活着（哪怕是一个已经「不用了」的 hook），对象及其持有的所有显存/内存就不会释放。
- **循环垃圾回收（gc）**：引用计数处理不了循环引用（A 引用 B、B 引用 A），由 `gc` 模块周期性回收。`gc.collect()` 可以强制立刻回收。
- **`weakref.ref(obj)`**：弱引用不增加引用计数。`ref()` 返回 `None` 就说明对象已被回收——这是验证「对象真的被释放了」的标准手法。
- **`__del__` 析构函数**：对象被回收前调用。它可能在解释器退出的任意时刻、甚至在新对象已经接管了旧资源之后才执行，所以析构逻辑必须容忍「我引用的世界已经变了」。

### 2.2 PyTorch 的 hook 机制

`module.register_forward_pre_hook(fn)` 会返回一个 `RemovableHandle`，调用它的 `remove()` 才能摘除 hook。关键陷阱：hook 函数通常是**闭包**——它捕获了定义它时的外部变量。如果闭包里捕获了引擎（`self`），那么只要 hook 还挂在模型上，**模型就把引擎（连同引擎持有的优化器、fp32 主权重、梯度缓冲）钉在内存里**。HF Trainer 的使用模式正是「反复销毁引擎但保留模型」，于是每次都泄漏一整个引擎的状态。

### 2.3 包装器模式

DeepSpeed 不会直接用你传入的优化器做 step。你的 `torch.optim.Adam` 是「最里层」；外面按配置包上 ZeRO 优化器（梯度切分/通信）或 fp16/bf16 优化器（主权重管理/loss scaling）。理解这条包装链，才能看懂 `engine.optimizer.step()` 到底执行了什么。

### 2.4 与上一讲的衔接

上一讲（u2-l2）我们看到 `DeepSpeedConfig` 在**配置层**解析 `deterministic_mode`（`configure_determinism` 只是把布尔值写到 `config.deterministic_mode` 上）。本讲看**引擎层**：`DeepSpeedEngine.__init__` 在拿到 config 后的第一时间调用 `apply_determinism` 真正生效。这种「解析在 config、应用在 engine」的两阶段设计是 DeepSpeed 的常见模式。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspeed/runtime/engine.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py) | 引擎主体：`__init__` 八阶段、`destroy()`/`__del__`、优化器包装分发 |
| [deepspeed/runtime/determinism.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/determinism.py) | `apply_determinism`：确定性策略的引擎侧应用点 |
| [deepspeed/runtime/zero/stage_1_and_2.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py) | ZeRO-1/2 优化器的 `destroy()`：清 `_hp_mapping`/`_zero_optimizer` 反向引用 |
| [deepspeed/runtime/zero/stage3.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage3.py) | ZeRO-3 优化器的 `destroy()`：清 `p._z3_optimizer`、unpin 卸载缓冲 |
| [deepspeed/runtime/zero/parameter_offload.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/parameter_offload.py) | ZeRO-3 参数卸载器的 `destroy()`：摘模块 hook、弹 `FWD_MODULE_STACK` |
| [deepspeed/runtime/zero/partitioned_param_coordinator.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/partitioned_param_coordinator.py) | 参数协调器：`__params_to_release` 的每实例 `lru_cache` |
| [deepspeed/runtime/bf16_optimizer.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py) | `BF16_Optimizer.destroy()`：清 `lp._hp_grad` 视图 |
| [tests/unit/v1/zero/test_zero.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero.py) | `TestDestroyReleasesEngine`：销毁后可回收性的七场景回归测试 |

## 4. 核心概念与源码讲解

### 4.1 引擎构造主线：八个阶段

#### 4.1.1 概念说明

`deepspeed.initialize()` 把模型、优化器、配置交给我们之后，真正干活的是 `DeepSpeedEngine.__init__`。这个构造函数长达约 270 行（[engine.py:518-786](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L518-L786)），但它有清晰的骨架。把骨架拆出来，就得到了后续所有讲义（ZeRO、AutoTP、混合精度）在初始化中的「插入点」地图。

为什么要强调顺序？因为这个函数里很多步骤**互相依赖**：AutoEP 必须在优化器之前替换模块（否则优化器持有的是被丢弃的旧专家权重）；确定性策略必须在任何 CUDA 算子跑起来之前应用；forward hooks 必须在优化器包装之后注册（否则 DeepCompile 无法改造它们）。

#### 4.1.2 核心流程

`__init__` 的八个阶段（按代码执行顺序）：

```text
阶段 1  基础属性 + 确定性策略
        保存 config，若 deterministic_mode=True 则 apply_determinism()
阶段 2  参数与配置校验
        _do_args_sanity_check → _configure_with_arguments → _do_sanity_check
阶段 3  AutoEP / AutoTP 模块注入
        _configure_expert_parallel → （autotp_size>1 时）_configure_tensor_parallel
        含 _autotp_frozen_replicated_params 冻结参数记录、first_dataloader_check hook
阶段 4  分布式环境与监控
        _set_distributed_vars（绑定设备、rank）→ dist.configure → MonitorMaster
阶段 5  分布式模型配置与引擎 hooks
        _configure_distributed_model（混合精度 cast、设备迁移、MoE 探测）
        注册 module_forward_pre_hook / module_forward_post_hook
阶段 6  数据加载器与计时器
        training_data → deepspeed_io；SynchronizedWallClockTimer 等
阶段 7  优化器与调度器包装（三层包装链的核心）
        _configure_optimizer → basic_optimizer → engine.optimizer
        _configure_lr_scheduler
阶段 8  检查点引擎与收尾
        _configure_checkpointing（决定谁写检查点）
        engine_timers、compile pass 注册、torch 风格 backward 支持判定
```

注意：**检查点加载**（`load_checkpoint`）不发生在 `__init__` 里——它是用户在 `initialize()` 之后显式调用的公共方法；`__init__` 里只完成阶段 8 的「检查点引擎配置」（决定哪个 rank 写、写到哪里）。

#### 4.1.3 源码精读

构造函数签名与最初的属性初始化——引擎是 `torch.nn.Module` 的子类，把用户的模型挂在自己的 `module` 属性下：

- [engine.py:518-550](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L518-L550) — `__init__` 接收 `args/model/optimizer/model_parameters/config...`，先把步数计数器、MoE 容器、优化器占位（`self.optimizer = None`、`self.basic_optimizer = None`）等基础属性铺好，并保存 `config`（原始 JSON 侧）与 `config_class`（解析后的 `DeepSpeedConfig`）。

阶段 1 的确定性策略应用点（详见 4.2）：

- [engine.py:551-553](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L551-L553) — 只要 `deterministic_mode` 为真，立刻导入并调用 `apply_determinism(self._config)`。此时任何模型参数还没被 cast、任何 CUDA 上下文（通常）还没建立。

阶段 2 的校验链：

- [engine.py:588-591](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L588-L591) — 三连调用：`_do_args_sanity_check(args)`（要求 `LOCAL_RANK` 环境变量存在，见 [engine.py:2004-2016](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2004-L2016)）→ `_configure_with_arguments(args, mpu)`（把 `LOCAL_RANK` 对齐到 `args.local_rank`，处理 MPI 的 `OMPI_COMM_WORLD_LOCAL_RANK`，见 [engine.py:1986-2001](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1986-L2001)）→ `_do_sanity_check()`（校验 fp16/bf16 设备支持、优化器类型合法性、`managed_gradient_accumulation=False` 的限制等，见 [engine.py:2035-2069](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2035-L2069)）。
- [engine.py:590](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L590) — `self.pipeline_parallelism = isinstance(model, PipelineModule)`：模型是流水线模块时，后面会改用 `PipelineEngine`（u6-l4 的主题），本讲的通用路径不适用。

阶段 3 的模块注入（详见 4.4）：

- [engine.py:594-609](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L594-L609) — 先 `_model_parameters_need_remap` 判断用户是否显式传了参数列表（注入会替换参数对象，列表必须重映射）；然后 `_configure_expert_parallel` 做 AutoEP 注入；`autotp_size() > 1` 时再做 `_configure_tensor_parallel`。两步各自返回 `replacement_sources` 供重映射，用完立即 `del`——注释明确说明：留着会在整个 `__init__` 期间引用已废弃的预切分专家权重，而那正是 ZeRO 分配分区的时刻。

阶段 4 的分布式环境：

- [engine.py:614-618](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L614-L618) — `_set_distributed_vars(args)`（[engine.py:1973-1983](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1973-L1983)：`device_rank >= 0` 时 `get_accelerator().set_device()` 绑定本卡、从 `dist` 取 `world_size/global_rank`；否则退化为单进程 `world_size=1`）；`dist.configure(self._config)` 配通信层；`MonitorMaster` 建监控。

阶段 5 的模型配置与 hooks：

- [engine.py:629-633](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L629-L633) — `_configure_distributed_model(model)`（入口见 [engine.py:2107-2127](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2107-L2127)：把模型挂为 `self.module`、按混合精度配置只 cast 参数保留 fp32 buffer、迁移到设备、探测 MoE 层）；随后注册引擎级 forward hooks——`module_forward_pre_hook` / `module_forward_post_hook`，工厂在 [engine.py:2949-2961](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2949-L2961)，闭包捕获 `self`（引擎）。**记住这两个 hook，它们正是 4.5 中泄漏链条的起点**。
- [engine.py:636](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L636) — `self.param_names = {param: name ...}`：参数对象到名字的反查表，后续 `zero_to_fp32` 权重导出要用。

阶段 6 的数据加载器：

- [engine.py:655-658](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L655-L658) — 传了 `training_data` 就用 `self.deepspeed_io()` 包成 DeepSpeed 数据加载器（u2-l4 的主题）。

阶段 7 的优化器包装（详见 4.3）：

- [engine.py:689-697](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L689-L697) — 有优化器（用户传入或配置声明）时走 `_configure_optimizer` + `_configure_lr_scheduler`；没配优化器但开了 ZeRO/bf16 时也分别构建对应的包装优化器。

阶段 8 的检查点与收尾：

- [engine.py:701-704](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L701-L704) — `_configure_checkpointing()`（[engine.py:1932-1954](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1932-L1954)：创建 checkpoint engine，按 DP rank 决定本进程是否写模型/优化器检查点）。注意 ZeRO 卸载优化器（`DeepSpeedZeRoOffload`）的情形在这里被跳过——它的检查点配置由自己接管。
- [engine.py:732-754](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L732-L754) — DeepCompile 可用时注册全部预定义编译 pass（Z1/Z2 reduce、Z3 gather/release、prefetch 等，u8-l4 的主题）。
- [engine.py:761-780](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L761-L780) — 判定是否支持 PyTorch 风格 `loss.backward()`：仅当优化器是 `ZeROOptimizer` 且 torch 内部 API 可用时，向优化器注册 `register_grad_acc_post_hook(self._backward_post_hook)`（u2-l4 讲 backward 主循环时会再遇到它）。

#### 4.1.4 代码实践

**实践：整理初始化阶段清单**

1. **实践目标**：把 `DeepSpeedEngine.__init__` 的执行顺序固化成一份带行号的清单，作为你阅读 DeepSpeed 其他子系统的「插入点索引」。
2. **操作步骤**：
   - 打开 [deepspeed/runtime/engine.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py)，从 518 行开始通读到 786 行。
   - 仿照 4.1.2 的八阶段伪代码，建一张 Markdown 表：`阶段 | 起止行 | 关键调用 | 它依赖的前一阶段 | 依赖它的后一阶段`。
   - 用调试器验证顺序（不修改源码）：在训练脚本里 `import pdb; pdb.set_trace()`，或在 VS Code 里对 `_configure_expert_parallel`、`_configure_distributed_model`、`_configure_optimizer` 三个函数入口打断点，观察命中顺序。
3. **需要观察的现象**：断点命中顺序应为 AutoEP → （可选 AutoTP）→ `_configure_distributed_model` → `_configure_optimizer`；日志中 `DeepSpeed Basic Optimizer = ...` 一定出现在 `DeepSpeed Final Optimizer = ...` 之前（见 [engine.py:2387](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2387) 与 [engine.py:2402](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2402)）。
4. **预期结果**：得到一份与 4.1.2 一致（或因版本差异略有出入）的清单，能回答「优化器包装发生在 AutoTP 注入之前还是之后」这类问题。
5. 断点验证依赖本地环境，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `del autoep_replacement_sources`（[engine.py:600-602](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L600-L602)）不能省？

**答案**：`replacement_sources` 记录的是注入前参数到注入后参数的映射，其中引用了被 AutoEP 替换掉的旧专家权重。重映射完成后它就没有用途了；不删掉的话，这些本应丢弃的张量会活过整个 `__init__`，而 `__init__` 正是 ZeRO 分配参数分区、显存峰值最高的时刻——白白多占一份专家权重的显存。

**练习 2**：`_configure_with_arguments` 里为什么要专门处理 `OMPI_COMM_WORLD_LOCAL_RANK`？

**答案**：MPI 启动（`mpirun`）不设置 `LOCAL_RANK`，而是设置 `OMPI_COMM_WORLD_LOCAL_RANK`。引擎要求 `LOCAL_RANK` 存在（`_do_args_sanity_check` 断言），所以这里把 MPI 变量对齐到 `LOCAL_RANK`，并在两者同时存在时断言一致，防止启动器信息冲突。

**练习 3**：把 `_configure_distributed_model` 挪到 `_configure_expert_parallel` 之前会发生什么？

**答案**：`_configure_distributed_model` 会在当前（未注入的）模型上做混合精度 cast 与设备迁移，并探测 MoE 层写入 `self.has_moe_layers`。AutoEP 注入发生在其后的话，cast/迁移作用在即将被丢弃的旧模块上，且 `has_moe_layers` 探测不到新注入的专家层，后续优化器分组、专家并行组都会出错。这个顺序是「先换模块、再配置模型、最后包优化器」的固定链条。

### 4.2 确定性策略的应用点：apply_determinism

#### 4.2.1 概念说明

`deterministic_mode`（u8-l8 会系统讲）是一个 opt-in 的确定性执行开关。它的难点在于**时机**：cuBLAS 的工作区配置（`CUBLAS_WORKSPACE_CONFIG`）必须在 CUDA 初始化之前由环境变量决定，`torch.use_deterministic_algorithms` 也应在任何算子执行前打开。一旦某个阶段跑过非确定性内核，再开开关也无法回头。因此引擎把应用点放在 `__init__` 的**第一批属性初始化之后、一切模型/优化器/通信配置之前**。

#### 4.2.2 核心流程

```text
JSON: "deterministic_mode": true
  └─> DeepSpeedConfig 构造期: configure_determinism() 解析布尔值（u2-l2 已讲）
        └─> DeepSpeedEngine.__init__ 早期: apply_determinism(config)
              ├─ config.deterministic_mode 为 False → 直接返回
              ├─ 非 NVIDIA CUDA 后端（KLXPU/ROCm/CPU…）→ ValueError
              ├─ CUBLAS_WORKSPACE_CONFIG 不是 :4096:8 或 :16:8 → ValueError
              └─ 全部通过:
                    torch.use_deterministic_algorithms(True)
                    关闭 TF32（matmul 与 cudnn）、关 cudnn.benchmark、开 cudnn.deterministic
```

#### 4.2.3 源码精读

引擎侧调用点：

- [engine.py:551-553](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L551-L553) — `if self._config.deterministic_mode: from .determinism import apply_determinism; apply_determinism(self._config)`。注意这是惰性导入：不用该功能就不付出导入成本，符合「`import deepspeed` 必须轻量」的项目纪律。

`apply_determinism` 的完整实现（该文件很短，值得整读）：

- [determinism.py:24-38](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/determinism.py#L24-L38) — 五个动作：后端检查（`_is_nvidia_cuda`，[determinism.py:17-21](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/determinism.py#L17-L21)：设备名是 `cuda`、accelerator 名是 `cuda`、且无 HIP——KLXPU 会继承 `cuda` 设备名但自称 `klxpu`，ROCm 同样报告 `cuda`，所以要双重甄别）；环境变量检查；然后四个 torch 全局开关。第 31 行注释特别说明**为什么不在代码里 `os.environ` 设置**：此刻 CUDA 可能已经初始化过 cuBLAS 工作区，改了也无效，所以只检查、报错、要求用户在启动前设好。

配置侧的解析函数（衔接 u2-l2）：

- [determinism.py:10-14](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/determinism.py#L10-L14) — `configure_determinism(config, param_dict)` 只做类型校验（必须是 JSON 布尔）并写 `config.deterministic_mode`。

#### 4.2.4 代码实践

**实践：复现确定性开关的三种失败**

1. **实践目标**：亲手触发 `apply_determinism` 的两类报错，理解「检查为什么必须前置」。
2. **操作步骤**（CPU 机器即可完成第一项）：
   - 写一个最小脚本：构建含 `"deterministic_mode": true` 的 config 字典，调用 `deepspeed.initialize(config=..., model=SimpleModel(hidden_dim=8), model_parameters=...)`。
   - 场景 A（CPU 机器）：直接运行，预期抛出 `deterministic_mode currently supports NVIDIA CUDA only`。
   - 场景 B（NVIDIA 机器，**不设** `CUBLAS_WORKSPACE_CONFIG`）：先 `import torch; torch.zeros(1).cuda()` 再初始化 DeepSpeed，预期抛出 `requires CUBLAS_WORKSPACE_CONFIG=...`。
   - 场景 C（NVIDIA 机器）：`export CUBLAS_WORKSPACE_CONFIG=:4096:8` 后正常初始化，确认无报错。
3. **需要观察的现象**：A/B 分别在引擎构造的极早期（模型还没 cast、优化器还没建）抛 `ValueError`，而不是等到第一个 step 才失败。
4. **预期结果**：三种场景的行为与 [determinism.py:29-33](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/determinism.py#L29-L33) 的两个 `raise` 一一对应。
5. 本实践需要本地 Python/torch 环境，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `apply_determinism` 不顺手把 `os.environ["CUBLAS_WORKSPACE_CONFIG"]` 设上？

**答案**：因为 cuBLAS 工作区在 CUDA 初始化（第一个 CUDA 调用）时就读取该变量分配。引擎构造时 CUDA 往往已初始化（启动器、用户代码都可能先碰过 GPU），事后设置不会生效，只会制造「已开启」的假象。所以策略改为启动前检查、不满足即报错，把责任交给正确的时刻。

**练习 2**：`_is_nvidia_cuda` 为什么要同时检查 `device_name() == "cuda"`、`_name == "cuda"` 和 `torch.version.hip is None` 三个条件？

**答案**：这是多后端现实的防御：KLXPU 后端对外报告 torch 设备名 `cuda`（继承自 CUDA 实现）但 `_name` 是 `klxpu`；ROCm 的 accelerator `_name` 也报告 `cuda` 且设备名是 `cuda`，但带 `torch.version.hip`。三个条件与起来才能把「真 NVIDIA CUDA」从这些模仿者中筛出来。

### 4.3 优化器三层包装链

#### 4.3.1 概念说明

引擎里同时存在三个「优化器」名字，初学者极易混淆：

| 名字 | 是什么 | 谁创建 |
| --- | --- | --- |
| `optimizer`（`initialize()` 参数，即 client optimizer） | 用户传入的 `torch.optim.*` 实例、可调用对象，或 `None`（用 JSON 配置声明） | 用户 |
| `self.basic_optimizer` | 最里层的真实优化器：要么就是用户的优化器，要么由 JSON 的 `optimizer.type` 从零构建（如 `DeepSpeedCPUAdam`、`FusedAdam`） | `_configure_optimizer` |
| `self.optimizer` | 引擎对外暴露的最终优化器：在 basic 之外包上 ZeRO/fp16/bf16 包装器，实现梯度切分、主权重管理、loss scaling | `_configure_optimizer` 按配置分发 |

`engine.step()` 调用的是 `self.optimizer`；`self.optimizer` 内部在合适的时机调用 `self.basic_optimizer`（或其 param_groups）做真正的参数更新。

#### 4.3.2 核心流程

```text
_configure_optimizer(client_optimizer, model_parameters)
  ├─ client_optimizer is None:
  │     MoE 模型先 configure_moe_param_groups 分组
  │     basic = _configure_basic_optimizer(model_parameters)   # 从 JSON 构建最优实现
  ├─ client_optimizer 是实例/可调用:
  │     basic = client_optimizer（实例）或 client_optimizer(params)（可调用）
  │     ZeRO CPU 卸载 + 非 DeepSpeedCPUAdam/CPULion → 默认直接报错（zero_force_ds_cpu_optimizer）
  ├─ 过滤空 param_groups、查重
  ├─ self.basic_optimizer = basic
  ├─ wrapper = _do_optimizer_sanity_check(basic)   # 决定包装方向
  └─ 按 wrapper 分发:
        ZERO_OPTIMIZATION → _configure_zero_optimizer(basic)   # ZeRO-1/2/3
        FP16 / DDP_BFLOAT16 → _configure_fp16_optimizer(basic, lp_dtype)
        BFLOAT16           → _configure_bf16_optimizer(basic)
        其他               → self.optimizer = basic（裸用）
```

#### 4.3.3 源码精读

`_configure_optimizer` 主体：

- [engine.py:2361-2402](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2361-L2402) — 四条分支构建 basic（None→JSON 构建、实例直用、可调用调用、ZeRO 卸载时强制高性能 CPU 优化器）；[engine.py:2381-2387](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2381-L2387) 删掉空参数组并记录 `DeepSpeed Basic Optimizer` 日志；[engine.py:2389-2399](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2389-L2399) 由 `_do_optimizer_sanity_check`（[engine.py:2327](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2327)）返回包装方向并构建 `self.optimizer`；最后 [engine.py:2401-2402](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2401-L2402) 打印 `DeepSpeed Final Optimizer`——这两行日志就是验证三层关系的最直接证据。

ZeRO 分支（本周期有一处小增量）：

- [engine.py:2665-2699](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2665-L2699) — `_configure_zero_optimizer` 先根据 bf16/fp16 决定 `check_grad_overflow`；stage ≤ 2 走 `DeepSpeedZeroOptimizer`（ZenFlow 配置则换 `ZenFlowZeroOptimizer`），构造参数几乎全部来自 config 的 getter。
- [engine.py:2736-2740](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2736-L2740) — 构造调用末尾本周期新增 `retain_gpu_grad_partition_until_step=self.zero_retain_gpu_grad_partition_until_step()`：这是 ZeRO-2 CPU 卸载 + fp32 梯度累积路径的新开关（u5-l3 详讲），配置项经引擎 getter 透传进优化器。getter 本体在 [engine.py:1645-1646](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1645-L1646)。
- [engine.py:2742-2749](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2742-L2749) — stage 3 分支：先 `_validate_zero3_moe_compatibility`，MoE/AutoTP 场景解析参数放置组，`DummyOptim` 时构建 `DeepSpeedZeRoOffload`（u5-l5 的主题）。

没有优化器时的兜底：

- [engine.py:693-697](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L693-L697) — JSON 既没声明优化器、用户也没传，但开了 ZeRO/bf16：仍然分别构建 `_configure_zero_optimizer(optimizer=None)` / `_configure_bf16_optimizer(optimizer=None)`（内部用 `DummyOptim` 占位，见 [engine.py:2679-2680](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2679-L2680)）——因为 ZeRO 的价值（切分、通信）独立于具体优化算法。

#### 4.3.4 代码实践

**实践：用日志验证三层包装**

1. **实践目标**：在真实运行中看到 `client → basic → final` 的包装结果随配置变化。
2. **操作步骤**：
   - 基于 `tests/unit/simple_model.py` 的 `SimpleModel` 写训练脚本（参考 u2-l1 的接入三步法），分别用三种配置各跑一次：
     - (a) ZeRO stage 1 + JSON 声明 Adam（`torch_adam: true`）；
     - (b) ZeRO stage 2 + `offload_optimizer.device=cpu`（会选 DeepSpeedCPUAdam 一族）；
     - (c) 不开 ZeRO，只开 `bf16.enabled=true`。
   - 每次运行 grep 日志中的 `DeepSpeed Basic Optimizer` 与 `DeepSpeed Final Optimizer` 两行。
   - 再在脚本里打印 `type(engine.optimizer)`、`type(engine.basic_optimizer)`、`engine.optimizer.optimizer`（包装器持有的内层）。
3. **需要观察的现象**：
   - (a)：Basic = `Adam`，Final = `DeepSpeedZeroOptimizer`；
   - (b)：Basic 换成 CPU 优化器一族，Final 仍是 ZeRO 包装；
   - (c)：Final = `BF16_Optimizer`，其 `.optimizer` 才是你的 basic。
4. **预期结果**：三种配置的 (basic, final) 类型对与 4.3.2 的分发图一致；由此确认「引擎 step 打到的是包装器」。
5. 运行结果依赖本地硬件，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：用户在 ZeRO-Offload 下传入普通 `torch.optim.Adam` 会发生什么？如何绕过？

**答案**：[engine.py:2375-2379](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2375-L2379) 默认（`zero_force_ds_cpu_optimizer=true`）直接抛 `ZeRORuntimeException`：普通优化器在 CPU 卸载下性能极差。绕过方式是在 JSON 里设 `"zero_force_ds_cpu_optimizer": false`（明知性能代价仍要用时），或改用 `DeepSpeedCPUAdam` / JSON 声明优化器。

**练习 2**：为什么「没配优化器但开了 ZeRO」还要建包装优化器，而不是让 `engine.optimizer` 保持 `None`？

**答案**：ZeRO 优化器的职责远不止调用 Adam——它负责梯度 reduce-scatter/分桶/allgather、参数分区更新、loss scaling。这些能力与「用哪个优化算法」正交，所以用 `DummyOptim` 占位也要把包装层建起来（[engine.py:2679-2680](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2679-L2680)）。

**练习 3**：本周期在 `_configure_zero_optimizer` 里新增的 `retain_gpu_grad_partition_until_step` 参数属于哪一层？它如何从 JSON 到达优化器？

**答案**：属于 ZeRO-1/2 包装层的实现细节。链路是：JSON `zero_optimization.retain_gpu_grad_partition_until_step` → `DeepSpeedConfig` 的 zero 配置模型 → 引擎 getter `zero_retain_gpu_grad_partition_until_step()`（[engine.py:1645-1646](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1645-L1646)）→ 构造 `DeepSpeedZeroOptimizer` 时作为关键字参数传入（[engine.py:2740](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2740)）。

### 4.4 并行组初始化与模块注入的调用点

#### 4.4.1 概念说明

阶段 3 的两个注入器（AutoEP、AutoTP）都会**替换用户模型里的模块**，从而改变参数对象与进程组需求。这带来两个必须在引擎构造期解决的问题：

1. **参数重映射**：用户若把 `model.parameters()` 提前物化成列表传给 `initialize()`，注入替换参数后列表里全是失效对象，必须按 `replacement_sources` 重映射。
2. **冻结参数记录**：AutoTP 切分后，一部分参数在各 rank 间是复制（replicated）的。可训练的复制参数需要梯度 all-reduce hook；**冻结**（`requires_grad=False`）的则跳过——但引擎要记下清单（`_autotp_frozen_replicated_params`），因为用户之后若解冻它们，梯度不会走复制路径，训练会静默漂移，所以引擎选择在 backward 前直接报错（u6-l1 详讲）。

进程组方面：AutoEP 会创建专家并行（EP）与专家数据并行（EDP）组，AutoTP 会初始化 TP mesh——它们都发生在优化器包装**之前**，因为 ZeRO 构造时就要拿这些组来划分分区。

#### 4.4.2 核心流程

```text
__init__ 阶段 3
  ├─ _model_parameters_need_remap?            # 用户显式传了参数列表？
  ├─ _configure_expert_parallel(model)        # AutoEP：
  │     validate_autoep_config → build_folding_spec → validate_folding_global
  │     groups._create_expert_and_data_parallel(...)   # 建 EP/EDP 进程组
  │     替换 MoE 层，返回 replacement_sources
  ├─ 重映射 client optimizer 的 param_groups / 用户的参数列表
  ├─ autotp_size() > 1:
  │     _configure_tensor_parallel(model, tp_config)
  │       ├─ _configure_tensor_parallel_states: 初始化 TP mesh、注册 first_dataloader_check hook
  │       └─ _apply_autotp_partitioning: AutoTP 切分、finalize_autotp 里
  │            self._autotp_frozen_replicated_params = autotp.register_replicated_grad_hooks(model)
  └─ resolve_per_head_muon_after_sharding(model)   # Muon 按头数配置折算到切分后的参数
```

#### 4.4.3 源码精读

- [engine.py:824-878](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L824-L878) — `_configure_expert_parallel` 的前半段：校验 AutoEP 配置（TP 来自 mpu 时直接拒绝）、构建 folding spec 并做全局兼容性校验（offload 设备、ZeRO stage、DeepCompile 等矩阵，u6-l6 详讲）、把 `self._autoep_folding_spec` 存到引擎上，随后创建 EP/EDP 进程组。这就是「并行组初始化调用点」的第一个入口。
- [engine.py:948-951](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L948-L951) — `_configure_tensor_parallel` 是个薄壳：states + runtime + 实际切分。
- [engine.py:953-968](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L953-L968) — `_configure_tensor_parallel_states` 把 `groups.mpu` 指向进程组模块并 `_init_tp_mesh_device`；初始化 `self.first_dataloader_check = None`。
- [engine.py:1013-1015](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1013-L1015) — `first_dataloader_check` 真正注册处：给模型挂一个 `prepend=True` 的 forward pre-hook，第一次前向时广播并比对 TP 组各 rank 的 dataloader 输入，一致后自摘除（[engine.py:1011](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1011)）。它也是一个闭包引用引擎的 hook——同样被 4.5 的销毁链清理。
- [engine.py:1073-1075](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1073-L1075) — `finalize_autotp` 内部：`self._autotp_frozen_replicated_params = autotp.register_replicated_grad_hooks(model)`。`register_replicated_grad_hooks` 给可训练的复制参数注册梯度 all-reduce，并返回被跳过的冻结参数清单——引擎存下这份清单供防御检查。
- [engine.py:3152-3167](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L3152-L3167) — 防御检查的消费点：`_check_autotp_frozen_replicated_params` 找出「当初冻结、现在却 `requires_grad=True`」的参数，`_backward_prologue`（每次 backward 前调用的地方在同文件）发现即抛错，把「静默漂移」变成「显式失败」。
- [engine.py:2182-2186](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2182-L2186) — 阶段 5 里 AutoEP 模块的 `set_deepspeed_parallelism(...)` 调用：folding 组句柄在 `_configure_distributed_model` 中装配并传入，说明注入（阶段 3）与模型配置（阶段 5）之间还有一次「回填」。

#### 4.4.4 代码实践

**实践：观察 first_dataloader_check 的一次性生命周期**

1. **实践目标**：亲眼看到一个「自摘除 hook」的完整生命周期，并确认它挂在模型上且引用引擎。
2. **操作步骤**：
   - 写一个 `autotp_size=2`（单机两进程，`deepspeed --num_gpus 2` 启动）的 SimpleModel 训练脚本，跑两个 step。
   - 在第一个 step 前后分别检查 `engine.module._forward_pre_hooks` 的键数量（只读检查，不改源码）。
   - 观察日志中是否出现 `:The Dataloader has passed the TP group consistency check.`
3. **需要观察的现象**：第一次前向后 hook 数量减一（hook 自摘除），日志出现一致性检查通过信息；第二个 step 不再重复检查。
4. **预期结果**：与 [engine.py:1010-1015](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1010-L1015) 的逻辑一致——昂贵的一致性广播只做一次。
5. 需要 2 进程环境，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：AutoEP 注入为什么必须在优化器包装之前？给出两条理由。

**答案**：(1) 优化器的 param_groups 持有参数对象引用，注入替换参数后旧引用全部失效（所以需要 `replacement_sources` 重映射）；(2) ZeRO 优化器构造时要按进程组划分参数分区，EP/EDP 组必须先建好。

**练习 2**：`_autotp_frozen_replicated_params` 记录的是什么？为什么引擎要拒绝「事后解冻」？

**答案**：记录 AutoTP 切分后因 `requires_grad=False` 而被 `register_replicated_grad_hooks` 跳过梯度同步的复制参数。事后解冻它们，梯度不会走复制参数的 all-reduce 路径（hook 注册时就被跳过了），各 rank 梯度不再一致，训练静默漂移；引擎在 backward 前检查并在发现时抛错，宁可显式失败也不静默出错。

**练习 3**：`first_dataloader_check` 与 `module_forward_pre_hook` 都是 forward pre-hook，有何区别？

**答案**：`first_dataloader_check` 是 AutoTP 路径注册的**一次性**检查 hook（`prepend=True`，通过后自摘除），职责是保证 TP 组各 rank 收到相同输入；`module_forward_pre_hook` 是引擎级**常驻** hook（`_create_module_forward_pre_hook` 工厂创建，触发 `_forward_prologue` 做内存统计、profiler 启动等每步工作）。两者都会被 `destroy()` 摘除。

### 4.5 destroy()/__del__ 生命周期与引用清理（本周期 #9034）

#### 4.5.1 概念说明

HF Trainer 等上层框架的使用模式是：**反复对同一个模型 build → train → destroy → build → train → ...**（例如每个 epoch 重建引擎）。修复前（issue #7204）这会泄漏 GiB 级显存与主机内存，根因是一张「模型 → 引擎 → 优化器」的隐性引用网：

1. **模型挂着的 hooks 引用引擎**：`module_forward_pre_hook`/`module_forward_post_hook`（以及 AutoTP 路径的 `first_dataloader_check`）都是闭包，捕获 `self`（引擎）。模型活着 → hook 活着 → 引擎活着 → 优化器、fp32 主权重、扁平梯度缓冲全部活着。
2. **参数属性反向引用优化器**：ZeRO-3 给参数留 `p._z3_optimizer`；ZeRO-1/2 卸载留 `lp._zero_optimizer`；BF16 优化器的 `_update_hp_grad` 给每个 bf16 参数留 `lp._hp_grad`——指向整块扁平 fp32 梯度缓冲的**视图**（约 2 倍 bf16 模型大小），视图会让整个 storage 活着。
3. **进程级栈泄漏**：ZeRO-3 的 `setup_zero_stage3_hooks` 把顶层模块压入进程级 `FWD_MODULE_STACK`，从不弹出——每个销毁的模型（及其引擎）永久存活。
4. **类级缓存的隐性钉住**：参数协调器的 `__params_to_release` 原是**类级** `functools.lru_cache`，以 `self` 为 key、永不失效——所有协调器及其引用的模块、参数被钉住到进程结束。
5. **`__del__` 的二次拆除**：显式 `destroy()` 后，对象最终被 GC 回收时 `__del__` 会**再次**执行 `destroy()`。此时同一个模型可能已被**新引擎**包装：二次拆除会清掉属于新引擎的状态（典型如 `_hp_mapping`），把新引擎弄坏。

#9034 的修复思路：`destroy()` 幂等化（`_destroyed` 标记）+ 把上面每一条引用逐一斩断。

#### 4.5.2 核心流程

销毁链的执行顺序（顺序有意为之）：

```text
engine.destroy()                          # 用户显式调用
  └─ self._destroyed = True               # 先立标记（幂等 + __del__ 跳过依据）
  └─ destroy_exchanges(module)            # 只释放本引擎的 DeepEP 缓冲
  └─ 释放 DeepCompile 编译态 / dynamo 配置
  └─ optimizer.destroy()                  # 各优化器清理反向引用：
  │     ZeRO-3 (stage3):      parameter_offload.destroy()  → 摘模块 hook + 弹 FWD_MODULE_STACK（至多一次）
  │                            p._z3_optimizer = None、unpin 卸载缓冲
  │     ZeRO-1/2 (stage_1_and_2): p._hp_mapping = None、del p._zero_optimizer、unpin
  │     BF16:                  lp._hp_mapping = None、lp._hp_grad = None、摘 grad acc hooks
  └─ _deactivate_deepcompile()            # 若 DeepCompile 激活：先走它（会重新挂 forward hooks）
  └─ 摘除并置空三个引擎 hook:              # 必须在 _deactivate_deepcompile 之后！
        module_forward_pre_hook / module_forward_post_hook / first_dataloader_check
  └─ debug_clear_module_and_param_names()
  └─ decoupled checkpoint engine cleanup
  finally: 释放 python_gc 手持的代际

engine.__del__()                          # GC 兜底
  └─ if self._destroyed: return           # 已显式销毁 → 直接返回，防止二次拆除
  └─ 否则 try destroy()（异常仅 debug 日志）
```

#### 4.5.3 源码精读

`__del__` 与 `destroy`（本周期核心修改）：

- [engine.py:1255-1264](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1255-L1264) — `__del__` 开头新增 `_destroyed` 检查。注释写明动机：显式销毁的引擎可能在新引擎包装同一模型**之后**才被 GC 回收，此时再拆一遍会清掉现属新引擎的状态。
- [engine.py:1266-1291](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1266-L1291) — `destroy()` 全貌：第一行 `self._destroyed = True`；随后 DeepEP 缓冲释放（注释强调「只释放本引擎的缓冲，同进程其他引擎还需要自己的」）；优化器销毁；DeepCompile 停用；然后是本周期新增的 hook 摘除循环——遍历 `("module_forward_pre_hook", "module_forward_post_hook", "first_dataloader_check")` 三个属性，`hook.remove()` 后 `setattr(self, hook_attr, None)`。注释给出两个关键理由：调用方常在 `destroy()` 后保留模型（HF Trainer），这些 hook 闭包引用引擎，不摘引擎就活不过 GC；且必须放在 `_deactivate_deepcompile()` **之后**，因为后者会重新注册 forward hooks。用 `getattr(self, hook_attr, None)` 是因为 `first_dataloader_check` 只在 AutoTP 路径才被赋值。
- [engine.py:1297-1302](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1297-L1302) — `finally` 块释放 Python GC 代际：即使销毁中途异常也不泄漏这个全局手持。

各优化器的反向引用清理（`optimizer.destroy()` 的分发目标）：

- [stage_1_and_2.py:762-778](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L762-L778) — ZeRO-1/2：等待卸载拷贝完成后，对每个 bit16 参数清 `p._hp_mapping`、删 `p._zero_optimizer`（`is self` 的身份判断保证只清自己拥有的）；摘梯度累积 hooks；synchronize 后清 Muon allgather 缓冲；`_unpin_offload_buffers()` 释放页锁定主机内存（含本周期重构后的 `accumulated_grads_in_cpu` 按分区组缓冲，u5-l4 详讲）。
- [stage3.py:532-546](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage3.py#L532-L546) — ZeRO-3：先 `self.parameter_offload.destroy()`（见下条），再清 `p._z3_optimizer`（同样 `is self` 判断）、清 Muon 缓冲、摘 grad acc 与 leaf module hooks、清 IPG 桶、synchronize、unpin 全部卸载缓冲（[stage3.py:548-579](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage3.py#L548-L579)）。
- [parameter_offload.py:281-292](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/parameter_offload.py#L281-L292) — 参数卸载器：`_remove_module_hooks()` 摘模块 hooks；然后在进程级 `FWD_MODULE_STACK` 中找到 `is self.module` 的条目删除。注释解释「至多弹一次」（`_has_fwd_stack_entry` 标志）：新引擎包装同一模块后若重复弹栈，会误删新引擎压入的条目。
- [bf16_optimizer.py:126-138](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/bf16_optimizer.py#L126-L138) — BF16 优化器：清 `p._hp_mapping` 与 `p._hp_grad`。注释点明 `_hp_grad` 的危险：`_update_hp_grad()` 留下的是 fp32 梯度缓冲的**视图**，destroy 后仍保留模型的话整块缓冲被钉住。
- [partitioned_param_coordinator.py:161-163](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/partitioned_param_coordinator.py#L161-L163) — `__params_to_release` 的缓存从类级改为**每实例**：`self.__params_to_release = functools.lru_cache(maxsize=None)(self.__params_to_release)`。注释说明原因：类级 `lru_cache` 以 `self` 为 key 且永不失效，会把每个协调器（连同其引用的模块与参数）钉住整个进程生命周期。

回归测试（本周期新增）：

- [test_zero.py:2277-2292](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero.py#L2277-L2292) — `TestDestroyReleasesEngine`：参数化七种场景——ZeRO 1/2/3 × 是否 CPU 卸载（stage 3 卸载时参数也卸载），再加 bf16 参数 + `grad_accum_dtype=fp32`（选中 `BF16_Optimizer`）。
- [test_zero.py:2330-2345](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero.py#L2330-L2345) — 第一组断言：训练几个 step 后 `destroy()`，记录 `weakref`，`del` 全部局部变量并 `gc.collect()`，断言引擎与优化器都被回收；对 BF16 还比对「梯度缓冲 storage 指针」与「参数仍持有的 storage 指针」无交集——正是 `lp._hp_grad` 视图问题的直接测试（视图让 tensor 对象死了 storage 还活着，所以要比 storage 而不是比 tensor）。
- [test_zero.py:2347-2361](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero.py#L2347-L2361) — 第二组断言（针对 `__del__` 二次拆除）：建引擎 A → destroy → 建引擎 B（接管模型）→ A 被回收时不得改变 B 看到的 fp32 参数（`safe_get_full_fp32_param` 前后比对）。
- [test_zero.py:2363-2375](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero.py#L2363-L2375) — 第三组断言：连模型本身也可回收——`destroy()` 后 DeepSpeed 不再引用模型。`_storages_held_by_params` 辅助函数把循环变量挡在测试体之外（注释：遗留的 ZeRO-3 参数会把模型钉住）。

#### 4.5.4 代码实践

**实践：亲手复现并验证销毁可回收性**

1. **实践目标**：用 `weakref + gc.collect()` 手法验证 `destroy()` 后引擎、优化器、模型三层都可回收，且销毁旧引擎不破坏新引擎。
2. **操作步骤**：
   - 阅读 [test_zero.py:2277-2375](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero.py#L2277-L2375)，把 `TestDestroyReleasesEngine.test` 改写成独立脚本（不改测试文件本身）：单进程、`SimpleModel(hidden_dim=16)`、ZeRO stage 2、`torch_adam` 优化器。
   - 脚本流程：初始化引擎 → 用 `random_dataloader` 跑 4 个 batch（`engine.backward(loss); engine.step()`）→ `engine.destroy()` → 建 weakref → `del` 全部变量 → `gc.collect()` → 断言 `ref() is None`。
   - 再按测试的第二组：destroy 旧引擎 → initialize 新引擎 → 旧引擎被 GC 后，断言 `safe_get_full_fp32_param` 结果不变。
   - 对比实验（可选，需要旧代码）：`git stash` 当前工作区后 `git checkout 02f7978481cb3d3297f111c02b16d768b890b256` 运行同一脚本，观察断言失败（引擎/优化器未被回收）。**看完务必切回 `b9100c541`。**
   - 也可直接跑官方回归：`python -m pytest tests/unit/v1/zero/test_zero.py::TestDestroyReleasesEngine -v`（仓库内运行需按 `tests/pytest.ini` 的环境约定，u9-l4 详讲）。
3. **需要观察的现象**：
   - 当前 HEAD：所有 weakref 断言通过，控制台出现 `Removed grad acc hooks`；
   - 旧 HEAD（02f7978）：`the kept model still references the destroyed engine` 断言失败——直观看到泄漏。
4. **预期结果**：与 #9034 修复目标一致：销毁后的引擎/优化器/模型全部可被 GC 回收；HF Trainer 式反复建毁不再累积显存。
5. 本实践需要本地 torch 环境，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么摘 hooks 的循环必须放在 `_deactivate_deepcompile()` 之后？换成之前会怎样？

**答案**：`_deactivate_deepcompile()` 在停用 DeepCompile 时会把 forward hooks **重新注册**回 eager 模式（见 [engine.py:1282-1286](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1282-L1286) 的注释）。若先摘后停，摘掉的会被重挂回来，泄漏照旧；所以顺序必须是「先停 DeepCompile（它重挂 hooks）→ 再摘 hooks」。

**练习 2**：`p._hp_grad` 为什么危险系数特别高？测试里为什么用 storage 指针而不是 tensor 身份来判断？

**答案**：`_hp_grad` 是扁平 fp32 梯度缓冲的视图，体量约 2 倍 bf16 模型大小；视图对象本身很小，但它把整块缓冲的 **storage** 钉在内存里——即使缓冲的 tensor 对象已被 GC。所以测试比较 `untyped_storage().data_ptr()` 的集合交集（[test_zero.py:2334-2339](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero.py#L2334-L2339)），比 tensor 身份更能抓住这类泄漏。

**练习 3**：类级 `lru_cache` 为什么会钉住所有协调器？改成每实例后语义有何变化？

**答案**：类级 `functools.lru_cache` 装饰方法时，缓存的 key 包含 `self`，且缓存永不失效——每个调用过的实例都永久留在缓存里，实例又引用模块与参数。改成每实例（`__init__` 里对绑定方法再包一层 `lru_cache`）后，缓存生命周期与实例绑定：实例可回收，缓存随之消失，缓存效果（按 `(submodule, step_id)` 记忆待释放参数集）不变。

**练习 4**：`destroy()` 里清反向引用时为什么用 `getattr(p, '_z3_optimizer', None) is self` 而不是无脑置空？

**答案**：模型可能已被新引擎包装，新优化器可能已经把自己的引用写进同一属性。身份判断 `is self` 确保只清除「确实指向自己」的引用，不误伤后来者的状态——这正是二次拆除问题在优化器层面的对应防线。

## 5. 综合实践

**任务：为「引擎生命周期」写一份带证据的实验报告**

把本讲的两条主线串起来：构造期顺序 + 销毁期清理。建议流程：

1. **准备**：基于 `tests/unit/simple_model.py` 的 `SimpleModel` 写一个可配置的训练脚本（config 用字典传入），支持 `zero_stage`、`offload`、`bf16` 三个开关。
2. **构造期**：开 `torch` 日志或直接观察 stdout，收集 `DeepSpeed Basic Optimizer` 与 `DeepSpeed Final Optimizer` 两行日志，对 ZeRO-1/2/3、bf16 四种配置各记录一次 (basic, final) 类型对，对照 4.3.2 的分发图打勾。
3. **销毁期**：在脚本里实现「build → train 4 步 → destroy → build → train 4 步 → destroy」的循环，每轮结束后：
   - `gc.collect()`；
   - 用 `weakref` 断言上一轮引擎与优化器已被回收（仿 [test_zero.py:2332-2343](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero.py#L2332-L2343)）；
   - 若在 GPU 上：记录 `torch.cuda.memory_allocated()`，观察多轮循环后是否线性增长（增长即泄漏）。
4. **输出**：一页报告，含 (a) 八阶段清单及行号（4.1.4 的成果）、(b) 优化器包装类型表、(c) 各轮显存曲线与 weakref 断言结果。
5. 全部结果依赖本地环境，**待本地验证**；若暂无 GPU，步骤 3 的显存观察可换成 `tracemalloc` 统计主机内存，销毁断言部分在 CPU 后端同样可跑（ZeRO 1/2 不依赖 GPU）。

## 6. 本讲小结

- `DeepSpeedEngine.__init__` 有清晰八阶段骨架：基础属性+确定性 → 校验 → AutoEP/AutoTP 注入 → 分布式环境 → 模型配置+hooks → 数据加载 → 优化器包装 → 检查点与收尾；后续所有子系统都在这张地图上有明确插入点。
- 优化器是三层结构：client optimizer → `basic_optimizer`（真实更新者）→ `engine.optimizer`（ZeRO/fp16/bf16 包装器）；两行日志即可验证。
- `deterministic_mode` 在 `__init__` 最早期经 `apply_determinism` 生效：仅支持 NVIDIA CUDA、要求预先设置 `CUBLAS_WORKSPACE_CONFIG`，检查前置是因为 cuBLAS 工作区在 CUDA 初始化后无法更改。
- AutoEP/AutoTP 注入会替换模块与参数，因此必须先于优化器包装，并带出参数重映射、`_autotp_frozen_replicated_params` 冻结清单、`first_dataloader_check` 一次性 hook 等配套机制。
- 本周期 #9034 补全了销毁链：`destroy()` 先立 `_destroyed` 标记、摘除三个闭包引用引擎的 hooks（在 `_deactivate_deepcompile` 之后）、各优化器清理 `p._z3_optimizer`/`lp._zero_optimizer`/`lp._hp_grad` 反向引用、弹 `FWD_MODULE_STACK`、协调器缓存改为每实例；`__del__` 检查 `_destroyed` 防止 GC 期二次拆除破坏新引擎。
- `TestDestroyReleasesEngine` 用 weakref + storage 指针 + gc 的组合，把「销毁后可回收」固化为七场景回归契约。

## 7. 下一步学习建议

- **u2-l4（训练主循环）**：本讲注册的 `module_forward_pre_hook` 触发的 `_forward_prologue`、梯度累积边界判定、`register_grad_acc_post_hook` 支持的 PyTorch 风格 backward，都将在 `train_batch`/`backward`/`step` 的时序中展开。
- **u5-l3 / u5-l5（ZeRO 优化器内部）**：本讲看到的三层包装里，`DeepSpeedZeroOptimizer` 与 `DeepSpeedZeRoOffload` 的 step 内部（IPG bucket、参数协调器）是 ZeRO 单元的主菜；本讲的销毁链各落点会重新出现在它们的构造与 hook 注册处。
- **u8-l8（确定性执行）**：`apply_determinism` 只是入口，Triton 内核的 IEEE 精度路径、`stochastic_mode` 互斥校验等确定性边界问题在该讲系统展开。
- 想深入销毁链的原始动机，可读 issue #7204 与 PR #9034 的讨论，再对照 `tests/unit/v1/zero/test_zero.py::TestDestroyReleasesEngine` 的三组断言逐条映射到 4.5.3 的修复点。
