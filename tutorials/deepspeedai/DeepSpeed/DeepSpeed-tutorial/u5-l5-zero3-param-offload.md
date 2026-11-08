# ZeRO-3：参数卸载与参数协调器

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 ZeRO-3 的核心机制：参数本身也按数据并行度切分，module hook（pre/post forward）在每层执行前后按需 all-gather / release 参数，使得任意时刻 GPU 上只驻留「正在用 + 即将用」的那一小部分参数。
2. 描述参数 `ds_status` 的三态生命周期 `NOT_AVAILABLE → INFLIGHT → AVAILABLE → NOT_AVAILABLE`，以及每一步转换发生在哪个源码位置。
3. 理解参数切分的对齐数学：`aligned_size = ⌈ds_numel / W⌉ × W`，为什么每个 rank 的分区必须等长，coalesced all-gather 又如何用一块连续扁平缓冲同时聚合多个参数以减少碎片。
4. 解读本周期（PR #9014，对应 issue #4072）NVMe 预取去重修复：参数 trace 按「每次使用」记录一条，末层 / 梯度检查点重算层 / 共享层都会在预取窗口内出现两次，`__prefetch_nvme_param_partitions` 现在用集合跳过重复参数，避免 `swap_in()` 第二次分配缓冲触发 `param already assigned swap buffer` 断言。
5. 解读 `zero/linear.py` 中 ZeRO-3 定制 Linear 的 autocast 设备修复：autocast 状态按「操作数所在设备」判定而非当前加速器（CUDA 机器上的 CPU autocast 也必须正确生效），前向的设备选择被缓存到 `ctx` 供反向复用。

## 2. 前置知识

- **ZeRO 三阶段**（前置讲义 u5-l1）：Stage 1/2 只切优化器状态和梯度，参数始终整份驻留；Stage 3 把参数也切了，于是「参数什么时候在 GPU 上」第一次成为一个需要调度的问题——本讲全文都在回答它。
- **`zero.Init()`**（前置讲义 u5-l2）：模型构建期参数「出生即切分」，每个 rank 只拿到自己那一份 `ds_tensor` 分区；本讲讲的是构建期之后、训练期之内的事。
- **module hook**：PyTorch 允许给任何 `nn.Module` 注册 `forward_pre_hook`（在模块 `forward()` 执行前触发）、`forward_hook`（forward 之后触发）。hook 是 ZeRO-3 在不侵入用户模型代码的前提下拦截每一层执行时机的手段。
- **autograd 与 `torch.autograd.Function`**：自定义反传节点需要实现 `forward` / `setup_context` / `backward`；`ctx` 对象可在前向存状态、反向取回。ZeRO-3 用它包裹 Linear，也用它实现「pre/post backward hook」（见 4.2）。
- **autocast（混合精度自动转型）**：`torch.autocast(device_type, dtype=...)` 上下文让其中的算子自动按设备类型跑低精度。关键细节：**autocast 状态按 device_type 分设备保存**——`torch.autocast('cpu')` 只影响 CPU 算子，`torch.autocast('cuda')` 只影响 CUDA 算子，这是 4.5 节修复的出发点。
- **NVMe 卸载（ZeRO-Infinity 预备知识）**：`offload_param.device="nvme"` 时参数分区进一步沉到磁盘；`swap_in` / `swap_out` 是 CPU↔NVMe 的异步 I/O，需要预分配的 swap buffer。细节在 u5-l7 展开，本讲只涉及「预取窗口里谁会被 swap_in」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `deepspeed/runtime/zero/parameter_offload.py` | `DeepSpeedZeRoOffload`：ZeRO-3 参数卸载管理器，负责把普通参数转成 ZeRO 参数、标记持久参数、给全模型注册 hook，并把具体调度委托给协调器 |
| `deepspeed/runtime/zero/partitioned_param_coordinator.py` | `PartitionedParameterCoordinator`：参数协调器，记录网络 trace、驱动「立即 fetch / 预取 / 等待 / 释放」，**本周期去重修复所在地** |
| `deepspeed/runtime/zero/partition_parameters.py` | ZeRO-3 的地基（u5-l2 已精读）：`ZeroParamStatus` 枚举、参数切分与对齐、all-gather 的三条实现路径、`free_param`。本讲只引用与状态机相关片段 |
| `deepspeed/runtime/zero/linear.py` | `LinearFunctionForZeroStage3`：ZeRO-3 定制 Linear 的 autograd.Function，autocast 设备修复所在地 |
| `deepspeed/runtime/swap_tensor/partitioned_param_swapper.py` | NVMe 参数 swapper：`swap_in` 及其缓冲分配断言（去重修复要消除的崩溃点），完整机制在 u5-l7 |
| `tests/unit/v1/zero/test_zero3_nvme_param_prefetch.py` | 本周期新增回归：全部参数在 NVMe + 三种梯度检查点模式下与 CPU 卸载逐位一致 |
| `tests/unit/v1/zero/test_zero_functorch_linear.py` | Linear autocast 行为回归：CPU autocast、autocast 不泄漏进反向、functorch/vmap 兼容 |

## 4. 核心概念与源码讲解

### 4.1 DeepSpeedZeRoOffload：参数卸载的指挥官

#### 4.1.1 概念说明

ZeRO-3 把参数切掉之后，必须有一个角色回答一连串调度问题：每个子模块执行前把哪些参数聚合上来？执行完能不能立刻放掉？哪些参数太小、聚合的通信开销不值得，干脆常驻 GPU？`DeepSpeedZeRoOffload` 就是这个角色的「外壳」——它在引擎构造期被创建，做四件一次性的准备工作，然后把运行期的逐层调度全部委托给内部的 `PartitionedParameterCoordinator`（4.3 节）：

1. 把模型里所有普通 `torch.nn.Parameter` 转换成 ZeRO 参数（挂上 `ds_status`、`ds_tensor`、`ds_id` 等属性，必要时借道 `zero.Init()` 完成切分）；
2. 给每个 module 的 `_parameters` 字典换成 `ZeROOrderedDict`，让「在 forward 里直接访问被切分参数」也能触发聚合；
3. 按 `stage3_param_persistence_threshold` 标记**持久参数**（小参数常驻，避免为几百个元素的 bias 反复做集体通信）；
4. 创建协调器并给全模型注册 hook（4.2 节）。

#### 4.1.2 核心流程

```
引擎构造（stage=3）
  └─ DeepSpeedZeRoOffload(module, ..., offload_param_config, ...)
      ├─ offload_param_config.device != none → 记录卸载设备（cpu/nvme）与 pin_memory
      ├─ _convert_to_zero_parameters()   # 非 ZeRO 参数 → 调 Init() 切分
      ├─ _inject_parameters(module, ZeROOrderedDict)
      ├─ mark_persistent_parameters(param_threshold, model_threshold)
      │     └─ ds_numel <= 阈值且总量不超模型阈值 → param.ds_persist = True
      ├─ PartitionedParameterCoordinator(
      │     prefetch_bucket_sz, max_reuse_distance_in_numel,
      │     max_available_parameters_in_numel, prefetch_nvme=(device==nvme), ...)
      └─ setup_zero_stage3_hooks()  # 见 4.2
```

一个容易混淆的点：类名叫 `Offload`，但它**无论是否配置 `offload_param` 都会创建**——ZeRO-3 本身就是「参数切分 + 按需聚合」，参数分区默认在 GPU 上；`offload_param` 只是决定分区驻留在 CPU 还是 NVMe。

#### 4.1.3 源码精读

构造函数签名集中体现了可调 knobs（对应 JSON 的 `stage3_prefetch_bucket_size`、`stage3_max_reuse_distance`、`stage3_max_live_parameters`、`stage3_param_persistence_threshold` 等）：

- [parameter_offload.py:136-158](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/parameter_offload.py#L136-L158)：`DeepSpeedZeRoOffload.__init__` 的参数表——预取桶大小、最大复用距离、最大常驻参数量、持久化阈值、卸载配置、`overlap_comm` 等。

- [parameter_offload.py:176-185](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/parameter_offload.py#L176-L185)：解析 `offload_param_config`（device 非 none 才记录卸载设备与 pin_memory）；`_convert_to_zero_parameters` 把漏切分的参数转成 ZeRO 参数；`_inject_parameters` 把每个 module 的 `_parameters` 换成 `ZeROOrderedDict`。

- [parameter_offload.py:317-335](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/parameter_offload.py#L317-L335)：`mark_persistent_parameters`——遍历命名参数，`ds_numel <= param_threshold` 且累计不超 `model_threshold` 的标 `ds_persist=True`。这些参数永远 `AVAILABLE`，不参与 fetch/release 循环（bias、LayerNorm 的 weight 之类的小参数聚合纯属浪费）。

- [parameter_offload.py:212-224](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/parameter_offload.py#L212-L224)：创建协调器，注意 `prefetch_nvme=self.offload_device == OffloadDeviceEnum.nvme`——只有参数真正卸到 NVMe 时才启用 NVMe 预取路径（4.4 节的舞台）。

- [parameter_offload.py:58-95](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/parameter_offload.py#L58-L95)：`ZeROOrderedDict.__getitem__`——在 eager forward 期间访问一个 `NOT_AVAILABLE` 的参数时，把它注册为当前栈顶 module 的 external parameter 并立即 `param.all_gather()`。这是「权重共享 / 在 forward 里手工取别的模块的参数」这类场景的兜底，保证读到的永远是完整张量。

- [parameter_offload.py:113-133](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/parameter_offload.py#L113-L133)：`ensure_zero_ordered_dict`——PyTorch 2.5+ 把 `_parameters` 默认换成普通 dict，引擎初始化之后才挂上的子模块会绕过注入，此函数幂等地补包一层。

参数出生时的属性挂载在 u5-l2 已精读，这里只列状态机相关的几个：[partition_parameters.py:1270-1297](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partition_parameters.py#L1270-L1297) 中 `ds_status` 初始为 `AVAILABLE`、`ds_numel` 记录未填充的真实元素数、`ds_persist` 由阈值决定。

#### 4.1.4 代码实践

**实践目标**：直观感受「持久参数阈值」对常驻参数量的影响。

1. 用 4.2 节实践里的两线性层小模型（或任意 stage=3 配置），先设 `"stage3_param_persistence_threshold": 0`，再设 `"stage3_param_persistence_threshold": 1000000`。
2. `deepspeed.initialize` 时用 `deepspeed.utils.logging.logger` 的输出（或直接在 `mark_persistent_parameters` 的 `print_rank_0` 处临时加断点/日志）观察 `Parameter Offload - Persistent parameters statistics` 一行的 `param_count` 与 `numel` 变化。
3. 对比两次实验中训练若干步后的峰值显存（GPU 环境用 `torch.cuda.max_memory_allocated()`；CPU 环境对比进程 RSS）。

**需要观察的现象**：阈值从 0 调大后，小参数（bias、一维权重）被计入 persistent，`param_count` 上升；这些参数不再出现在 4.3 节的 `-fetch:` / `-release:` 调试日志里。

**预期结果**：persistent 参数的总 numel 是所有「小于阈值」参数之和，且不超过 `stage3_model_persistence_threshold`。显存变化方向取决于你的模型里小参数占比——这正是该阈值需要调优的原因。（具体数字待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DeepSpeedZeRoOffload` 在没有配置 `offload_param` 时也会被创建？

**答案**：ZeRO-3 的定义就是「参数切分」：每个 rank 只持有 `ds_tensor` 分区，module 执行前必须 all-gather 出完整参数、执行后释放。这套 fetch/release 调度由 `DeepSpeedZeRoOffload` 和它内部的协调器负责，与分区放在 GPU、CPU 还是 NVMe 无关；`offload_param` 只决定 `ds_tensor` 的驻留设备（见 [parameter_offload.py:176-178](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/parameter_offload.py#L176-L178)）。

**练习 2**：把所有参数的 `ds_persist` 都设为 True 会发生什么？

**答案**：ZeRO-3 退化为「常驻全量参数」——显存收益归零（等价于不切分），只剩优化器状态切分（Stage 1/2 的效果）。持久化阈值就是「聚合小参数的通信开销 > 常驻它们的显存代价」这条权衡线的量化表达。

---

### 4.2 hook 注册：module hook 如何按需 all-gather

#### 4.2.1 概念说明

ZeRO-3 不能修改用户的模型代码，却必须知道「每一层什么时候开始算、什么时候算完、什么时候梯度流过」。解法是给**每一个子模块**注册四种 hook，在 hook 回调里驱动协调器。四个钩子两两成对：

| 钩子 | 注册方式 | 触发时机 | 做什么 |
| --- | --- | --- | --- |
| `_pre_forward_module_hook` | `register_forward_pre_hook` | 子模块 forward 执行前 | `fetch_sub_module(forward=True)`：聚合本模块参数 + 预取后面几层 |
| `_post_forward_module_hook` | `register_forward_hook` | 子模块 forward 执行后 | `release_submodule(forward=True)`：按复用距离释放；登记输出里的「悬空参数」 |
| `_pre_backward_module_hook` | **注册为 forward hook**（包输出） | 反向传播到达本模块输出的梯度时 | `fetch_sub_module(forward=False)` |
| `_post_backward_module_hook` | **注册为 forward_pre_hook**（包输入） | 反向传播穿过本模块输入之后 | `release_submodule(forward=False)` |

后两行的注册方式是精髓：PyTorch 没有原生的「模块级 backward hook」可靠实现，DeepSpeed 借道 autograd——**pre-backward 钩在模块的输出张量上**（输出张量的梯度最先到达），**post-backward 钩在模块的输入张量上**（梯度最后离开）。包裹动作发生在 forward 期间（所以用 forward hook / forward_pre_hook 注册），真正的回调在 backward 期间由 autograd 触发。

#### 4.2.2 核心流程

一次典型训练步的 hook 时序（`L1→L2→L3` 三层模型）：

```
engine(x)                              # 顶层 forward
 └─ _start_of_forward_hook → coordinator.reset_step()   # 每步重置：队列、step_id
forward:
 L1.pre_fwd_hook  → fetch(L1)          # all-gather L1 参数（+预取 L2）
 L1.forward
 L1.post_fwd_hook → release(L1)        # 若 trace 预测 L1 短期内不再用 → 分区释放
 L2.pre_fwd_hook  → fetch(L2) ...
 L3 ...
backward（与 forward 相反的顺序）:
 L3 输出梯度到达 → pre_bwd(L3) → fetch(L3, forward=False)
 L3 输入梯度算完 → post_bwd(L3) → release(L3)
 ...
```

于是参数在「要用前一瞬」物化、「用完一瞬」消失——这就是 ZeRO-3 显存节省在时间轴上的样子。

#### 4.2.3 源码精读

- [parameter_offload.py:299-315](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/parameter_offload.py#L299-L315)：`setup_zero_stage3_hooks` 给顶层模块注册 `_start_of_forward_hook`，每次顶层 forward 调 `coordinator.reset_step()`（重置 fetch 队列与 step 计数，推理模式下每次 forward 都算新一步）。

- [parameter_offload.py:337-356](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/parameter_offload.py#L337-L356)：`_register_deepspeed_module` 递归遍历所有子模块，给每个 module 挂 `ds_id` 与 `ds_recompute_parameters` 容器，然后定义四个 hook 闭包并注册（下述各行同在此函数内）。

- [parameter_offload.py:358-360](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/parameter_offload.py#L358-L360) 与 [parameter_offload.py:530-552](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/parameter_offload.py#L530-L552)：`_pre_forward_module_hook`（带 `@torch.compiler.disable`，防止 Dynamo 追踪进调度逻辑）→ `pre_sub_module_forward_function` → 记录 trace、`fetch_sub_module(sub_module, forward=True)`。

- [parameter_offload.py:362-403](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/parameter_offload.py#L362-L403)：`_post_forward_module_hook`——先扫描输出里的零参数/参数别名（模型把参数当输出返回的场景），登记为外层 module 的 external parameter 并 `all_gather()`；再调 `post_sub_module_forward_function` → `release_submodule(forward=True)`。

- [parameter_offload.py:466-487](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/parameter_offload.py#L466-L487)：`PreBackwardFunctionForModule`——一个「forward 原样 detach、backward 时先取参数再透传梯度」的 autograd.Function；引用计数 `applied_pre_backward_ref_cnt` 支持同一层被多次 forward 后再统一 backward（如 Albert 类模型）。注意第 487 行：它通过 `register_forward_hook` 挂在**输出**上。

- [parameter_offload.py:490-528](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/parameter_offload.py#L490-L528)：`PostBackwardFunctionModule`——对称地包**输入**，梯度计数归零时调 `post_sub_module_backward_function` → `release_submodule(forward=False)`；第 528 行用 `register_forward_pre_hook` 注册。

- [parameter_offload.py:554-609](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/parameter_offload.py#L554-L609)：`post_sub_module_forward_function` / `pre_sub_module_backward_function` / `post_sub_module_backward_function` 三个薄封装，核心都是一行协调器调用（zenflow 分支的转置操作可忽略，见 u9-l3）。

hook 与协调器的分工：hook 只回答「现在到了哪个模块、正向还是反向」；聚合谁、预取谁、放掉谁，全部是协调器根据 trace 做的决定。

#### 4.2.4 代码实践

**实践目标**：亲眼看到参数的按需物化——在自定义模块的 `forward_pre_hook` 里打印参数状态。

1. 写一个两线性层的小模型，用 `deepspeed.initialize`（stage=3，单进程即可，CPU 后端可跑）包装；
2. 在 `initialize` **之后**给其中一个子模块注册 `forward_pre_hook`（在 DeepSpeed 注册的 hook 之后，因此触发时参数已聚合）；
3. 分别在「initialize 完成后、forward 之前」与「hook 回调内」打印状态。

```python
# 示例代码：参数状态探针
import torch, deepspeed

class Net(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.fc1 = torch.nn.Linear(64, 64)
        self.fc2 = torch.nn.Linear(64, 64)
    def forward(self, x):
        return self.fc2(torch.relu(self.fc1(x)))

config = {"train_micro_batch_size_per_gpu": 2, "gradient_accumulation_steps": 1,
          "zero_optimization": {"stage": 3, "stage3_param_persistence_threshold": 0},
          "optimizer": {"type": "Adam", "params": {"lr": 1e-3}}}

model, optimizer, _, _ = deepspeed.initialize(model=Net(), model_parameters=None, config=config)

def probe(module, inputs):
    for n, p in module.named_parameters(recurse=False):
        print(f"[pre-fwd {type(module).__name__}.{n}] ds_status={p.ds_status.name}, "
              f"param.data numel={p.data.numel()}, ds_tensor.ds_numel={p.ds_tensor.ds_numel}")

model.fc1.register_forward_pre_hook(probe)   # 注册在 initialize 之后

w = dict(model.named_parameters())["fc1.weight"]
print(f"[before forward] ds_status={w.ds_status.name}, numel={w.data.numel()}")
x = torch.randn(2, 64).to(model.device)
out = model(x)
loss = out.sum(); model.backward(loss); model.step()
print(f"[after step] ds_status={w.ds_status.name}, numel={w.data.numel()}")
```

**操作步骤**：保存为 `probe_zero3.py`，单进程运行（GPU：`deepspeed --num_gpus 1 probe_zero3.py`；纯 CPU 环境直接 `python probe_zero3.py` 亦可，DeepSpeed 有 CPU 加速器后端）。

**需要观察的现象**：
- `[before forward]`：`ds_status=NOT_AVAILABLE`、`numel=0`——参数以空壳形态驻留，数据只在 `ds_tensor` 分区里；
- `[pre-fwd fc1.weight]`：`ds_status=AVAILABLE`、`numel=4096`——DeepSpeed 自己的 pre-forward hook 先于你的探针运行，参数已 all-gather 完毕；
- `[after step]`：又回到 `NOT_AVAILABLE`、`numel=0`——step 结束后全量释放。

**预期结果**：如上三阶段状态翻转。若单进程（partition world size = 1），`ds_tensor.ds_numel` 应等于参数完整 numel（切分退化）；两进程时约为一半（含 4.3 节的对齐填充）。GPU/CPU 双环境行为一致，具体打印值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 pre-backward 钩子要用 `register_forward_hook` 注册，post-backward 反而用 `register_forward_pre_hook`？

**答案**：它们包裹的是张量而非模块执行点。模块**输出**张量的梯度最先被算出——把 `PreBackwardFunctionForModule.apply` 施加在输出上（forward 时经 forward hook 注入），backward 一开始就能触发取参数；模块**输入**张量的梯度最后算完——包在输入上（forward 前经 forward_pre_hook 注入），梯度穿过它时正好本模块的反向全部结束，可以释放参数。

**练习 2**：如果用户模型在 `forward` 里直接读了另一个模块的参数（如手工权重共享 `self.w2.weight = self.w1.weight` 之外又直接索引），ZeRO-3 怎么保证读到的是完整张量？

**答案**：4.1 节的 `ZeROOrderedDict.__getitem__` 兜底：eager forward 期间（`_in_forward` 为真）发现 `ds_status == NOT_AVAILABLE` 就把该参数注册为当前栈顶模块的 external parameter 并立即 `all_gather()`（[parameter_offload.py:80-93](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/parameter_offload.py#L80-L93)）；`_post_forward_module_hook` 还会扫描输出中的悬空参数补登记（[parameter_offload.py:381-401](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/parameter_offload.py#L381-L401)）。

---

### 4.3 参数预取窗口与 ds_status 状态机

#### 4.3.1 概念说明

如果每层都「等参数到了再算」，GPU 会在每层边界空等 all-gather 通信。协调器的解法是**用第一步训练录下的 trace 预知未来**：第一步以 `RECORD` 模式记下「参数按使用顺序排成的序列」；之后每步以 `COMPLETE` 模式回放这个序列，当前模块还在算时就提前把**后面几层**的参数聚合好（预取窗口），通信与计算重叠。若实际执行偏离 trace（动态控制流），trace 失效回退 `RECORD` 重录——正确性永远不依赖预测。

参数本体则在三个状态间轮转：

```
NOT_AVAILABLE ──(提交 all-gather)──▶ INFLIGHT ──(handle.wait() 完成)──▶ AVAILABLE
      ▲                                                                   │
      └────────────────────(partition / free_param 释放)◀────────────────┘
```

- `NOT_AVAILABLE`：`param.data` 是空壳（`torch.empty(0)`），真实数据只在本 rank 的 `ds_tensor` 分区（或 NVMe）里；
- `INFLIGHT`：all-gather 已提交、未完成，参数登记在 `InflightParamRegistry`，任何试图重复聚合它的请求都会被注册表拒绝；
- `AVAILABLE`：`param.data` 持有完整张量，计算可用。

#### 4.3.2 核心流程

`fetch_sub_module`（每个模块 hook 都会调）固定做三件事，顺序不可变：

```
1. 立即 fetch：对当前模块 NOT_AVAILABLE 的参数提交异步 all-gather（进 allgather 专属 stream）
2. 预取：若 trace 已 COMPLETE，从 param_queue 头部弹出后续参数，
   填满 min(剩余常驻预算, prefetch_bucket_size) 的窗口后一次性 coalesced all-gather；
   遇到「分区在 NVMe 上」的参数则停止弹队列（保持顺序），交给 NVMe 预取（4.4）
3. 等待：阻塞当前模块参数的 handle 完成，断言每个参数 AVAILABLE
```

对齐数学：设参数元素数 \(N\)、分区组世界大小 \(W\)，则

\[ \text{aligned} = \left\lceil \frac{N}{W} \right\rceil \times W, \qquad \text{partition\_numel} = \frac{\text{aligned}}{W} \]

填充 \(\text{padding} = \text{aligned} - N\)。等长分区是 all-gather 的硬性要求（各 rank 拼同一块扁平缓冲）；聚合后的 `param.data` 从扁平缓冲 `narrow(0, 0, N)` 截掉填充再 `view(ds_shape)` 还原形状。Coalesced 路径更进一步：把窗口内**多个**参数的分区拼进同一块连续 `flat_tensor`，一次 collective 聚合一批参数，既减少通信次数，也把显存碎片压成整块分配。

#### 4.3.3 源码精读

状态枚举与注册表：

- [partition_parameters.py:242-250](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partition_parameters.py#L242-L250)：`ZeroParamStatus` 三态定义与注释（AVAILABLE=全 rank 完整、NOT_AVAILABLE=分区/远程、INFLIGHT=聚合中）。
- [partitioned_param_coordinator.py:62-70](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L62-L70)：`InflightParamRegistry`——`__setitem__` 拒绝重复登记、拒绝非 INFLIGHT 参数，是状态机的守门人。

trace 的录制与回放：

- [partitioned_param_coordinator.py:53-59](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L53-L59)：`ZeRoTraceMode` 三态（RECORD / COMPLETE / INVALID）。
- [partitioned_param_coordinator.py:221-248](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L221-L248)：`record_module` 每次模块执行都追加一条 `__submodule_order`；`record_parameters` 按模块展开成参数序列。**注意：同一模块每次使用都单独记录**——这是 4.4 节 bug 的根源。
- [partitioned_param_coordinator.py:250-290](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L250-L290)：`reset_step`——跨 rank 校验模块/参数序列一致（`assert_ints_same_as_other_ranks`），冻结为元组并把 `__trace_mode` 置 COMPLETE，最后用 trace 重建 `__param_queue`。

fetch 主流程：

- [partitioned_param_coordinator.py:307-351](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L307-L351)：`fetch_sub_module` 的 docstring 写明三步顺序；额外的 leaf-module 线程同步（反向时 autograd 可能多线程触发 hook）是并发正确性保障。
- [partitioned_param_coordinator.py:353-418](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L353-L418)：`_fetch_sub_module_impl`——第 364-381 行收集 `NOT_AVAILABLE` 参数提交 `__all_gather_params`（状态转 INFLIGHT）；第 390-412 行在 allgather stream 上等待 handle 并用 `__ongoing_fetch_events` 限制在途事件数（防 host 线程过度排队分配内存）；第 413 行断言 `AVAILABLE`。
- [partitioned_param_coordinator.py:420-448](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L420-L448)：预取窗口——先把「当前模块的参数」从队头弹出核对（不匹配则 `RuntimeError: tracing error`，见 436-442 行）；窗口大小受 `min(剩余常驻预算, prefetch_bucket_sz)` 约束。

预取与释放策略：

- [partitioned_param_coordinator.py:449-488](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L449-L488)：预取弹出循环——`_is_currently_on_nvme` 命中时把条目塞回队头并停止（保持顺序，NVMe 预取另行处理，第 460-463 行）；`params_to_prefetch` 是集合，第 466-468 行 `if param_in_trace.param in params_to_prefetch: do_prefetch = False` 显式注释 `Avoid duplicates`——**all-gather 预取本来就有去重，4.4 的修复是把同样的话补到 NVMe 预取**。
- [partitioned_param_coordinator.py:632-664](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L632-L664)：`__params_to_release`——释放决策：跳过被预取「越权提前聚合」的参数（否则 NVMe swap buffer 会乱序耗尽）、在 `max_reuse_dist_in_numel` 窗口内还会复用的参数不释放。
- [partitioned_param_coordinator.py:622-630](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L622-L630)：`__release_param`——`AVAILABLE` 且无活跃子模块时调 `param.partition()` 回到 `NOT_AVAILABLE`。

状态转换的另一半在 partition_parameters.py：

- [partition_parameters.py:1722-1763](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partition_parameters.py#L1722-L1763)：`_all_gather`——先经 `_ensure_availability_of_partitioned_params`（1707-1720 行）把还在 NVMe 的分区同步 swap_in 上来；异步路径提交后置 `INFLIGHT`，同步路径完成后置 `AVAILABLE`。
- [partition_parameters.py:735-796](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partition_parameters.py#L735-L796)：`AllGatherCoalescedHandle.wait`——等待 collective、从扁平缓冲切出各参数切片 `torch.cat(...).view(ds_shape)` 回填 `param.data`，第 783 行断言 INFLIGHT、第 796 行置 AVAILABLE。
- [partition_parameters.py:1690-1699](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partition_parameters.py#L1690-L1699)：`_aligned_size` / `_padding_size` / `_partition_numel`——对齐数学的落地，就三行。
- [partition_parameters.py:1816-1850](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partition_parameters.py#L1816-L1850)：`_partition_param`——`partition_size = aligned // world_size`；按 `final_location`（NVMe 用 swapper 缓冲、persist 留在本设备、其余按卸载配置）创建 `ds_tensor` 并标 `PartitionedParamStatus.AVAILABLE`。注意这里有两个状态系统：`param.ds_status`（完整参数在不在）与 `ds_tensor.status`（分区数据在不在 NVMe 缓冲里），4.4 节靠后者识别「还在 NVMe 上」。
- [partition_parameters.py:313-330](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partition_parameters.py#L313-L330)：`free_param`——释放时 `param.data = torch.empty(0)`、置 `NOT_AVAILABLE`，释放前确认没有活跃子模块并 `record_stream` 保证计算流已用完该缓冲。
- [partition_parameters.py:2029-2032](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partition_parameters.py#L2029-L2032)：单进程短路——分区世界大小为 1 时无需通信，`_no_gather_coalesced` 直接把分区数据当作完整参数。

#### 4.3.4 代码实践

**实践目标**：打开协调器的调试日志，看见 fetch / wait / prefetch / release 的完整节拍。

1. 在 4.2 的脚本开头加两行，把 deepspeed 日志调到 DEBUG：

```python
# 示例代码：打开协调器调试日志
import logging
deepspeed.utils.logging.logger.setLevel(logging.DEBUG)
logging.basicConfig(level=logging.INFO)   # 仅为让 handler 不拦截
```

2. 跑满 3 个训练步。协调器在 [partitioned_param_coordinator.py:355-362](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L355-L362)（`fetch_sub_module` 入口）、[partitioned_param_coordinator.py:378-380](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L378-L380)（`-fetch:`）、[partitioned_param_coordinator.py:481-483](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L481-L483)（`-prefetch:`）、[partitioned_param_coordinator.py:626-628](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L626-L628)（`-release:`）输出日志。
3. 对比第 1 步与第 2 步的日志差异。

**需要观察的现象**：第 1 步只有 `-fetch:` / `-wait:`（trace 处于 RECORD，无预取）；从第 2 步起出现 `-prefetch:`，且当前模块 fetch 时后面模块的参数已被提前提交。

**预期结果**：日志里 `M<ds_id>(Linear) P[参数id列表]` 的推进顺序第 1、2 步完全一致（同一 trace 回放）；第 2 步多出的 `-prefetch:` 行的参数集合正是 trace 中当前模块之后的下一层。若在第 3 步前向里加一个数据相关的 `if` 改变执行路径，应看到 `Invalidate trace cache` 警告并回退 RECORD。（具体行内容待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：64×64 的权重（N=4096）在 W=6 的分区组上，每个 rank 的 `ds_tensor.ds_numel` 是多少？

**答案**：\(\lceil 4096/6 \rceil = 683\)，aligned = 683×6 = 4098，padding = 2，每个分区 683 个元素。最后一个 rank 的分区里有 2 个填充元素，聚合后由 `narrow(0, 0, 4096)` 截掉（[partition_parameters.py:2018-2019](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partition_parameters.py#L2018-L2019)）。

**练习 2**：`stage3_max_live_parameters`（`max_available_parameters_in_numel`）和 `stage3_prefetch_bucket_size` 分别限制什么？一起调大为什么不一定更快？

**答案**：前者限制**任意时刻**物化参数的总量上限（预取窗口大小 = min(剩余常驻预算, prefetch 桶大小)，见 [partitioned_param_coordinator.py:452-454](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L452-L454)），后者限制**单次**预取的聚合量。调大能让通信藏得更深，但物化参数变多→显存峰值上升，且释放-重聚合的 churn 也可能增加；它们与 `max_reuse_distance` 共同决定「通信重叠度 vs 显存占用」的平衡点。

**练习 3**：为什么 `InflightParamRegistry` 要拒绝重复登记同一参数？

**答案**：一个参数同时只能有一个在途 all-gather handle；若两个模块的 fetch 同时提交同一参数，第二个 handle 完成时会与第一个的 `wait()` 竞争写 `param.data`。注册表在 `__setitem__` 里直接抛 `RuntimeError`，把这类调度错误拦在提交阶段（[partitioned_param_coordinator.py:65-70](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L65-L70)）。

---

### 4.4 NVMe 预取窗口的去重（PR #9014，issue #4072）

#### 4.4.1 概念说明

参数分区在 NVMe 上时，「预取」分成两段：先把**分区数据**从磁盘 swap_in 到 CPU/GPU 的 swap buffer（`ds_tensor.status: NOT_AVAILABLE → INFLIGHT/AVAILABLE`），之后 all-gather 才能拿得到分区。第二段（all-gather 预取，4.3 节）扫窗口时天然用集合去重；第一段（`__prefetch_nvme_param_partitions`）在修复前是**逐条收集到列表**——而参数 trace 是「每次使用一条」（4.3 节 `record_module` 的语义），同一个参数在窗口里出现两次就会被收集两次，`swap_in()` 对同一参数第二次分配缓冲时命中 swapper 的防御断言：

```
AssertionError: param N already assigned swap buffer id M
```

哪些参数会在窗口内被使用两次？三种典型情形（也是回归测试覆盖的三种）：

1. **末层**：它的 forward 结束后 backward 立刻从它开始，forward 用一次、backward 再用一次，两次使用在 trace 中紧邻，双双落入预取窗口；
2. **梯度检查点层**：backward 前要先重算 forward（recompute），重算用一次、紧跟着的 backward 又用一次——这正是 #4072 自 2023 年报出来的场景（HF gradient checkpointing）；
3. **共享层**：一次 forward 里同一层被连续应用两次。

为什么是「第二步开始崩」？因为 NVMe 预取只在 `is_complete_trace()` 时启用，而 trace 要第一步 fwd+bwd 走完才冻结（[partitioned_param_coordinator.py:250-277](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L250-L277)）——第一步没有预取，第二步起预取扫描双计参数触发断言。

#### 4.4.2 核心流程

修复前后对比（伪代码）：

```
修复前：
  for 条目 in 预取队列窗口:                # 每次使用一条
      if 分区在 NVMe 且 NOT_AVAILABLE:
          swap_in_params.append(param)     # 同一参数可能 append 两次！
  swap_in(swap_in_params)                  # 第二次分配缓冲 → AssertionError

修复后：
  seen = set()
  for 条目 in 预取队列窗口:
      if 分区在 NVMe 且 NOT_AVAILABLE and param not in seen:
          swap_in_params.append(param); seen.add(param)   # 每参数至多一次
  swap_in(swap_in_params)
```

窗口边界不变：扫过的元素量超过 `2 × numel_in_flight`（当前在途参数量的两倍）或凑满 `available_swap_in_buffers()`（可用 swap 缓冲数）即停。

#### 4.4.3 源码精读

- [partitioned_param_coordinator.py:666-694](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L666-L694)：`__prefetch_nvme_param_partitions` 全函数——第 677-678 行同时声明列表与集合 `swap_in_params_set`；第 686-690 行的注释写明意图：*The queue has a param once per use, e.g. the last layer's for the forward and again for the backward, but swap_in() takes each param once*，条件加上 `param not in swap_in_params_set`；第 694 行把去重后的列表交给 `nvme_swapper.swap_in(..., async_op=True)`。窗口边界在第 683-685 行。

- [partitioned_param_coordinator.py:444-449](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L444-L449)：`_is_currently_on_nvme`——识别「分区最终位置是 NVMe 且当前 NOT_AVAILABLE」的参数；all-gather 预取循环遇到它就停（[460-463 行](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L460-L463)），保持 fetch 顺序与 trace 一致，磁盘预取由本节的独立窗口负责。

- [partitioned_param_coordinator.py:466-468](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L466-L468)：对照组——all-gather 预取里早已存在的 `Avoid duplicates` 去重（`params_to_prefetch` 是集合）。修复就是让 NVMe 路径与之对齐。

- [partitioned_param_swapper.py:178-201](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/swap_tensor/partitioned_param_swapper.py#L178-L201)：`_allocate_and_return_buffers_for_swap_in`——崩溃现场：第 185-188 行断言参数尚未占用任何 swap buffer（`param_id not in param_id_to_buffer_id` / `not in param_id_to_swap_buffer`），命中即抛 `param N already assigned swap buffer id M`；随后从 `available_buffer_ids` 弹出空闲缓冲并按 IO 对齐宽度 `narrow` 出该参数的交换区。

- [tests/unit/v1/zero/test_zero3_nvme_param_prefetch.py:103-119](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/zero/test_zero3_nvme_param_prefetch.py#L103-L119)：回归测试主体——参数化 `checkpointing ∈ {none, reentrant, non_reentrant}`，分别以 `offload_param.device=cpu` 与 `=nvme` 训练同一模型，断言**逐步 loss 列表相等**且**每个权重逐位相等**（`torch.equal`，不是容差比较——同种子同实现下两条路径应当产生完全相同的数值）。

- [tests/unit/v1/zero/test_zero3_nvme_param_prefetch.py:24-59](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/zero/test_zero3_nvme_param_prefetch.py#L24-L59)：测试模型与配置——`_Net` 由 inp + 3 个 `_Block`（各含 fc1/fc2）+ out 组成，共 8 个线性层；HIDDEN=1024 使每个 bf16 权重恰 2 MiB——注释说明这样做的两个原因：权重超过 swapper 的 1 MiB 阈值才会真正走 NVMe 路径，且所有尺寸都是 512 字节倍数以走对齐直读。NVMe 配置 `max_in_cpu=0, buffer_count=32`——**不把参数留在 CPU**，缓冲池有 32 个缓冲、足够让每个权重各占一个；`stage3_param_persistence_threshold: 0` 排除持久参数干扰。注释还点明：默认 `stage3_max_reuse_distance` 下这个小模型的权重会全常驻、各持一个 swap buffer——正是最容易暴露双计的场景。

#### 4.4.4 代码实践

**实践目标**：通过阅读回归测试理解双计 bug 的形态与修复的验证方式（源码阅读型实践；有 GPU + NVMe 环境可实际运行）。

1. **读测试**：对照 [test_zero3_nvme_param_prefetch.py:44-51](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/zero/test_zero3_nvme_param_prefetch.py#L44-L51) 的 `_Net.forward`，手推 `checkpointing="non_reentrant"` 时 8 个线性层参数在 trace 中的完整使用序列（前向 8 次 + 反向前每层重算/反向各若干次），标出哪些参数的使用会**紧邻出现两次**。
2. **有条件则运行**（GPU + 可写 NVMe 路径，需 async_io 算子）：

```bash
pytest tests/unit/v1/zero/test_zero3_nvme_param_prefetch.py -v
```

3. **对照实验**：把 [partitioned_param_coordinator.py:688](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L688) 的 `and param not in swap_in_params_set` 临时改回只判 `param.ds_tensor.status == PartitionedParamStatus.NOT_AVAILABLE`（本地实验，勿提交），再跑测试。

**需要观察的现象**：步骤 3 应在第 2 个训练步复现 `AssertionError: param ... already assigned swap buffer ...`，报错栈落在 `_allocate_and_return_buffers_for_swap_in`；步骤 2 全绿。

**预期结果**：三种 checkpointing 模式下 NVMe 与 CPU 卸载的 losses 与权重逐位一致。步骤 1 的手推结论：末层 `out` 的参数（forward 用完紧跟 backward 用）与每个被 checkpoint 的 `_Block` 的参数（重算 + backward 相邻）都会双计；修复前它们各被 `swap_in_params` 收集两次。（无 GPU 环境时步骤 2、3 标注待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：为什么 all-gather 预取（4.3 节）从没出过这个 bug？

**答案**：它的收集容器 `params_to_prefetch` 本来就是 `set`，且循环里显式判断 `if param_in_trace.param in params_to_prefetch: do_prefetch = False`（[partitioned_param_coordinator.py:465-468](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L465-L468)）——集合语义天然吸收重复条目。NVMe 预取维护的是列表（顺序与缓冲分配相关），漏掉了 trace 的「每次使用一条」语义，修复补上平行集合。

**练习 2**：为什么修复选择在收集端去重，而不是让 `swap_in()` 自己跳过已分配缓冲的参数？

**答案**：swapper 的断言是刻意的防御：同一参数出现在一次 `swap_in` 调用的参数列表里两次，意味着调度层对窗口内容的认知有误——静默跳过会把这类错误变成难以察觉的半更新状态。在调用方（更了解 trace 语义的一方）去重，既消除了重复，又保住了 swapper 对「每次调用内参数唯一」的强约束。

**练习 3**：`numel_considered > 2 * numel_in_flight` 这个窗口上界为什么以「在途参数量」为基准？

**答案**：NVMe 预取的目的是让磁盘读与 all-gather 流水化：等当前在途的 all-gather 完成，磁盘预读的那批分区正好可用。窗口太大→抢占过多 swap buffer 且数据到了没人用；2×在途量大致对应「下一波 + 下下波」的需求节奏，是在吞吐与缓冲占用之间的经验平衡（[partitioned_param_coordinator.py:674-685](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partitioned_param_coordinator.py#L674-L685)）。

---

### 4.5 ZeRO-3 Linear 的 autocast 设备判定

#### 4.5.1 概念说明

`zero.Init()` 默认会把全局 `torch.nn.functional.linear` 替换成 `zero3_linear_wrap`（[partition_parameters.py:412-419](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partition_parameters.py#L412-L419)），模型里所有 `nn.Linear` 的前向都会流经 `LinearFunctionForZeroStage3`。它解决的内存问题是：普通 autograd 会经 `save_for_backward` 长期持有权重指针，ZeRO-3 希望 forward 一结束就把权重分区放掉、backward 时再 all-gather 回来。

autocast 与它的交互有个隐蔽的坑：**autocast 状态按 device_type 分设备保存**。DeepSpeed 的通用工具函数（`_get_autocast_decorators` 等）默认用 `get_accelerator().device_name()`——「当前加速器」的设备类型。但操作数的设备未必是加速器：在 CUDA 机器上完全可以有 CPU 张量跑在 `torch.autocast('cpu')` 里（例如 functorch/vmap 场景、部分 CPU 子图）。修复前的 backward 无条件以加速器设备类型重入 autocast，CPU 反向就得不到正确的低精度语义。修复引入 `_get_autocast_device_type(tensor)`：**设备类型跟随操作数（`tensor.device.type`）**，仅当该设备类型根本不支持 autocast 时才回退加速器；并把前向的决定（设备类型、dtype、是否启用）缓存到 `ctx`，backward 严格按缓存重放。

#### 4.5.2 核心流程

```
forward(input, weight, bias)
  └─ 纯数学计算（addmm / matmul），autocast 由外层上下文自然作用于算子
setup_context(ctx, inputs, output)
  ├─ device_type = _get_autocast_device_type(inputs[0])   # 跟随操作数设备！
  ├─ ctx._autocast_device_type / ctx._dtype / ctx._fwd_used_autocast = 当前 autocast 状态
  └─ ctx.save_for_backward(input, weight, bias)
backward(ctx, grad_output)
  ├─ with torch.autocast(device_type=ctx._autocast_device_type,
  │                      enabled=ctx._fwd_used_autocast,   # 前向没用就显式关闭，
  │                      dtype=ctx._dtype):                 # 防外层 autocast 泄漏
  ├─ 若 weight 已被分区（ds_status == NOT_AVAILABLE）→ weight.all_gather()
  ├─ 计算 grad_input / grad_weight / grad_bias
  └─ finally: weight.partition()                            # 用完立刻放掉
```

两个设计要点：其一，`enabled=ctx._fwd_used_autocast` 是**双向**对齐——前向用了 autocast 反向就重入，前向没用反向就显式关闭（哪怕 backward 调用点身处某个 autocast 区域，也不让它的低精度语义泄漏进梯度计算）；其二，权重在 backward 中「按需物化、用完即弃」，与 4.2/4.3 节的 hook 调度互补——hook 负责常规层，这里是 autograd.Function 内部的兜底（例如经 `torch.func.grad` 直接调用时没有 module hook）。

#### 4.5.3 源码精读

- [linear.py:80-87](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/linear.py#L80-L87)：`_get_autocast_device_type`——修复核心。注释写明：*Autocast state is per device type and only applies to ops on tensors of that type, so it must follow the operands (e.g. CPU autocast on a CUDA machine), not the accelerator*。第 85-86 行的回退：目标设备类型不支持 autocast（`torch.amp.is_autocast_available` 为假）时才用加速器设备名。

- [linear.py:109-116](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/linear.py#L109-L116)：`setup_context`——从 `inputs[0]` 取操作数设备类型，把 `_autocast_device_type`、`_dtype`、`_fwd_used_autocast` 三件套写入 `ctx`（供反向复用），并 `save_for_backward`。使用 `setup_context` 静态方法（而非旧式 `forward(ctx, ...)`）同时满足了 functorch 变换对 autograd.Function 的硬性要求（见测试文件头注释）。

- [linear.py:119-131](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/linear.py#L119-L131)：`backward` 开头——第 124 行按缓存重入 autocast（注释强调 *including explicitly disabling autocast when forward did not use it, to guard against outer autocast regions*）；第 128-131 行检测 `weight` 是否已被 ZeRO-3 分区（`is_zero_param(weight) and ds_status == "NOT_AVAILABLE"`），是则先 `weight.all_gather()`。

- [linear.py:133-155](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/linear.py#L133-L155)：梯度计算（按 `needs_input_grad` 与维度分支）与 `finally: weight.partition()`——无论梯度计算是否抛异常都把权重放回分区态。

- [linear.py:37-77](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/linear.py#L37-L77)：`_get_autocast_decorators` / `_is_autocast_enabled` / `_get_autocast_dtype`——兼容新旧 torch API 的一组探测函数；注意它们仍以加速器设备名为基准，这正是 `_get_autocast_device_type` 需要独立存在的原因。

- [linear.py:90-92](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/linear.py#L90-L92)：`generate_vmap_rule = True`——让 `torch.func.vmap` 能直接套在这个 autograd.Function 上（测试 [test_zero_functorch_linear.py:241-254](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/zero/test_zero_functorch_linear.py#L241-L254) 验证 vmap 结果与朴素实现一致）。

- [test_zero_functorch_linear.py:199-216](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/zero/test_zero_functorch_linear.py#L199-L216)：`test_cpu_autocast_backward_matches_functional_linear`——修复的靶点测试：纯 CPU 张量 + `torch.autocast("cpu", dtype=torch.bfloat16)`，断言 `zero3_linear_wrap` 的输出 dtype、值、以及三个梯度都与 `torch.nn.functional.linear` 参考实现一致。docstring 直说：*Backward must re-enter the autocast of the operands' device, which is not always the accelerator*。

- [test_zero_functorch_linear.py:166-197](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/zero/test_zero_functorch_linear.py#L166-L197)：`test_no_autocast_leak_into_backward`——前向无 autocast、反向包在 autocast 区域里，断言梯度仍为 float32（`_fwd_used_autocast=False` 的显式关闭生效）。

- [test_zero_functorch_linear.py:218-238](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/zero/test_zero_functorch_linear.py#L218-L238)：`test_setup_context_stores_autocast_attrs`——直接检查 `grad_fn._fwd_used_autocast is False`，把「缓存三件套」钉成接口契约。

#### 4.5.4 代码实践

**实践目标**：在 CPU 张量 + `torch.autocast('cpu')` 场景下验证 ZeRO-3 Linear 前后向 dtype 与数值一致（无需 GPU、无需初始化引擎，`zero3_linear_wrap` 可直接调用）。

```python
# 示例代码：cpu_autocast_probe.py —— 仿照 test_cpu_autocast_backward_matches_functional_linear
import torch
from deepspeed.runtime.zero.linear import zero3_linear_wrap

torch.manual_seed(0)
inp = torch.randn(2, 8, dtype=torch.bfloat16, requires_grad=True)
weight = torch.randn(4, 8, requires_grad=True)          # fp32 权重
bias = torch.randn(4, requires_grad=True)
ref = [t.detach().clone().requires_grad_() for t in (inp, weight, bias)]

with torch.autocast("cpu", dtype=torch.bfloat16):
    out = zero3_linear_wrap(inp, weight, bias)          # ZeRO-3 路径
    expected = torch.nn.functional.linear(*ref)         # 参考路径

out.float().sum().backward()
expected.float().sum().backward()

print("out.dtype =", out.dtype, "| expected.dtype =", expected.dtype)
for name, got, want in zip(("inp", "weight", "bias"), (inp, weight, bias), ref):
    print(f"{name}.grad.dtype = {got.grad.dtype} (ref {want.grad.dtype}), "
          f"allclose={torch.allclose(got.grad, want.grad)}")
```

**操作步骤**：`python cpu_autocast_probe.py`（任意有 torch + deepspeed 的环境；不涉及分布式与 CUDA）。

**需要观察的现象**：`out.dtype` 与参考一致（bfloat16——CPU autocast 把 fp32 权重提升为 bf16 参与计算）；三个梯度的 dtype 与参考路径逐一相同；`allclose=True`。

**预期结果**：全部一致。若把 deepspeed 回退到修复前的版本，反向会以错误的设备类型重入 autocast，梯度 dtype/数值与参考路径出现偏差（这正是 [test_zero_functorch_linear.py:199-216](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/zero/test_zero_functorch_linear.py#L199-L216) 守住的契约；具体差异表现待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：`backward` 里为什么要 `finally: weight.partition()`，而不是算完梯度自然返回？

**答案**：`weight.all_gather()` 已把完整权重物化到 GPU；若因异常或疏忽不分区，这次 backward 泄漏一份完整权重显存，多步累积后 ZeRO-3 的显存收益被吃光。`finally` 保证任何退出路径都执行释放（[linear.py:133-155](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/linear.py#L133-L155)）。

**练习 2**：`_get_autocast_device_type` 在什么条件下仍返回加速器设备名？

**答案**：仅当操作数的设备类型不支持 autocast 时（`torch.amp.is_autocast_available(device_type)` 为假），见 [linear.py:84-87](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/linear.py#L84-L87)。正常路径永远跟随操作数：CUDA 张量→'cuda'，CPU 张量→'cpu'，与机器上装没装 CUDA 无关。

**练习 3**：为什么不直接在 `backward` 里查询「当前 autocast 是否开启」，而要读前向缓存的 `_fwd_used_autocast`？

**答案**：backward 的调用点可能被用户包在任意上下文里（如测试 [166-197 行](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/zero/test_zero_functorch_linear.py#L166-L197)的反向 autocast 区域）。数值契约要求**前后向 autocast 语义对称**，唯一可靠的依据是前向当时的真实状态；查询当下得到的是调用点的状态，会产生前向 fp32、反向低精度（或反之）的不对称。缓存到 `ctx` 就是把「前向的事实」带过 autograd 边界。

---

## 5. 综合实践

把本讲内容串成一条可复现的观察链（单进程、CPU 或 GPU 均可，除步骤 4 外）：

**任务**：给一个 4 层小模型（`inp → 2×Block(fc1,fc2) → out`，可复用 [test_zero3_nvme_param_prefetch.py:24-51](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/tests/unit/v1/zero/test_zero3_nvme_param_prefetch.py#L24-L51) 的 `_Block`/`_Net` 定义）装上全套探针，跑 3 步训练并产出一份「状态机日志报告」。

1. **hook 探针**（4.2）：`initialize` 后给每个线性层注册 `forward_pre_hook`，打印 `ds_status`、`param.data.numel()`、`ds_tensor.ds_numel`；
2. **协调器日志**（4.3）：DEBUG 级别打开 `-fetch:` / `-prefetch:` / `-release:` 输出，对比第 1 步（RECORD，无预取）与第 2 步起（COMPLETE，有预取）的节拍差异；
3. **对齐验证**（4.3）：任选一个权重，手算 \(\lceil N/W \rceil \times W\) 与日志里 `ds_tensor.ds_numel` 对照；
4. **双计回归研读**（4.4）：手推 `checkpointing="non_reentrant"` 时哪些参数的使用在 trace 中紧邻出现两次，并写出「修复前第二步断言、修复后与 CPU 卸载逐位一致」的因果链（无法跑 NVMe 则以测试源码为依据作答）；
5. **autocast 探针**（4.5）：另起脚本运行 4.5.4 的 CPU autocast 对照实验。

**产出**：一份 Markdown 报告，每个步骤贴关键日志/输出，并给每行日志标注它对应本讲哪个源码位置（如 `-prefetch:` ↔ `__all_gather_params_` 的提交点）。完成后你应当能凭日志独立复述 ZeRO-3 一次 fwd+bwd 中任何一个参数的完整旅行路线。

## 6. 本讲小结

- ZeRO-3 的参数调度由 `DeepSpeedZeRoOffload`（外壳：参数转换、持久参数标记、hook 注册）与 `PartitionedParameterCoordinator`（内核：trace 录制回放、fetch/预取/释放）分工完成；hook 借道 autograd 实现模块级 pre/post backward 触发。
- 参数以 `NOT_AVAILABLE → INFLIGHT → AVAILABLE → NOT_AVAILABLE` 三态轮转：提交 all-gather 进 `InflightParamRegistry`，handle 完成后回填 `param.data`，模块用完即 `partition()` 释放为空壳。
- 第一步训练录制 trace（模块序列展开为「每次使用一条」的参数序列，跨 rank 校验后冻结），此后每步按 trace 预取：窗口 = min(剩余常驻预算, prefetch 桶)，参数在对齐切分（\(\lceil N/W \rceil \times W\)）与 coalesced 扁平缓冲下批量聚合。
- 本周期修复（PR #9014 / issue #4072）：NVMe 预取窗口对「每次使用一条」的 trace 逐条收集导致同一参数双计，`swap_in()` 第二次分配缓冲命中 `param already assigned swap buffer` 断言（末层、检查点重算层、共享层三种双用场景）；修复用平行集合去重，与 all-gather 预取的既有去重对齐，回归测试验证三种梯度检查点模式下 NVMe 与 CPU 卸载逐位一致。
- `LinearFunctionForZeroStage3` 的 autocast 设备修复：autocast 状态按 device_type 分设备保存，设备类型必须**跟随操作数**而非当前加速器（CUDA 机器上的 CPU autocast 也要正确）；前向把设备类型/dtype/是否启用缓存到 `ctx`，反向严格重放——包括前向未用 autocast 时显式关闭，防外层区域泄漏。

## 7. 下一步学习建议

- **u5-l6（ZeRO-3 优化器）**：参数协调器只管「参数的来去」，`stage3.py` 的 `DeepSpeedZeroOptimizer_Stage3` 在 step 边界做梯度 reduce-scatter 与分区更新，并接管 offload_states/reload_states 的子组搬运——那是 ZeRO-3 显存故事的另一半。
- **u5-l7（ZeRO-Infinity）**：本讲 4.4 只碰了 swapper 的缓冲分配断言；`swap_tensor` 包的异步 I/O 队列、线程模型与流水线化（以及本周期的对齐/错误检查修复）在那一讲展开。
- **u8-l4（DeepCompile）**：trace 驱动的运行时调度与编译期的 list scheduling 是同一思想（预知未来以重叠通信）在两个层面的应用，读完本讲再读 `compile/list_schedule.py` 会有明显既视感。
- 继续阅读建议：`deepspeed/runtime/zero/partition_parameters.py` 的 `GatheredParameters` 上下文（[partition_parameters.py:2337](https://github.com/deepspeedai/DeepSpeed/blob/02f7978481cb3d3297f111c02b16d768b890b256/deepspeed/runtime/zero/partition_parameters.py#L2337) 起）——手动 gather 参数检查/修改权重的正规入口，4.2 实践里它同样好用。
